"""Phase 3 tests — pipeline stages with stubbed LLM.

Each pipeline module (`digest`, `questions`, `reviews`, `regions`,
`truth_check`, `briefing`) is exercised against a monkeypatched
`server.compass.llm.call` that returns canned text.

We're testing:
  - JSON shape sanitization (drop malformed entries, coerce types)
  - Hard caps (max 2 new statements per digest, 1–3 targets)
  - Pre-filter purity (settle/stale candidate selection without LLM)
  - Flag honoring (don't re-propose, don't propose archived)
  - Region-merge `from_` cleanup (drop unknown / self-target)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from server.compass import llm as llm_mod
from server.compass.pipeline import (
    briefing as briefing_mod,
    digest as digest_mod,
    questions as questions_mod,
    regions as regions_mod,
    reviews as reviews_mod,
    truth_check as truth_check_mod,
)
from server.compass.store import (
    LatticeState,
    Region,
    Statement,
    TruthFact,
)


# --------------------------------------------------------- LLM stub


@dataclass
class _FakeResult:
    text: str
    is_error: bool = False
    cost_usd: float | None = 0.001
    duration_ms: int | None = 50
    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    session_id: str | None = "stub"
    stop_reason: str | None = "end_turn"
    errors: list[str] = field(default_factory=list)


def _stub_llm(monkeypatch: pytest.MonkeyPatch, response_text: str) -> list[dict[str, Any]]:
    """Replace `llm.call` with a stub that returns `response_text` and
    records the (system, user, label) tuple per invocation. Returns
    the recording list so tests can assert on what was sent."""
    invocations: list[dict[str, Any]] = []

    async def _fake(system: str, user: str, **kwargs: Any) -> _FakeResult:
        invocations.append({"system": system, "user": user, **kwargs})
        return _FakeResult(text=response_text)

    monkeypatch.setattr(llm_mod, "call", _fake)
    # Also patch in each pipeline module's `llm` reference, since they
    # imported it at module load time.
    for mod in (digest_mod, questions_mod, reviews_mod, regions_mod,
                truth_check_mod, briefing_mod):
        monkeypatch.setattr(mod.llm, "call", _fake)
    return invocations


def _make_state(**kwargs: Any) -> LatticeState:
    return LatticeState(project_id=kwargs.get("project_id", "alpha"))


def _add(state: LatticeState, **fields) -> Statement:
    s = Statement(
        id=fields.pop("id"),
        text=fields.pop("text", "x"),
        region=fields.pop("region", "general"),
        weight=fields.pop("weight", 0.5),
        created_at=fields.pop("created_at", "t"),
        **fields,
    )
    state.statements.append(s)
    return s


# --------------------------------------------------------- digest


@pytest.mark.asyncio
async def test_passive_digest_parses_and_sanitizes(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _add(state, id="s1", weight=0.5)
    _stub_llm(monkeypatch, json.dumps({
        "updates": [
            {"id": "s1", "delta": 0.08, "rationale": "chat hint"},
            {"id": "garbage", "delta": "not a number"},
            "ignore me",
        ],
        "new_statements": [
            {"text": "New claim", "region": "pricing", "rationale": "gap"},
            {"text": "", "region": "x"},  # empty text, drop
            {"text": "ok", "region": ""},  # empty region, drop
        ],
        "truth_candidates": ["this could be truth"],
        "summary": "one update, one new statement",
    }))

    res = await digest_mod.passive(state, signals=[
        {"kind": "chat", "ts": "t", "body": "..."}
    ])
    assert res.surprise is None
    assert len(res.updates) == 1
    assert res.updates[0]["id"] == "s1"
    assert len(res.new_statements) == 1
    assert res.new_statements[0]["text"] == "New claim"
    assert res.truth_candidates == ["this could be truth"]
    assert "one update" in res.summary


@pytest.mark.asyncio
async def test_answer_digest_captures_surprise(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _add(state, id="s1", weight=0.5)
    _stub_llm(monkeypatch, json.dumps({
        "surprise": 0.7,
        "updates": [{"id": "s1", "delta": 0.4, "rationale": "answer"}],
        "summary": "high surprise",
    }))
    res = await digest_mod.answer(
        state,
        question_text="Will customers self-serve?",
        prediction="Yes",
        targets=["s1"],
        answer_text="No, they expect onboarding.",
    )
    assert res.surprise == pytest.approx(0.7)
    assert len(res.updates) == 1
    assert res.updates[0]["delta"] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_digest_handles_garbage_response(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _stub_llm(monkeypatch, "not even close to JSON")
    res = await digest_mod.passive(state, signals=[])
    assert res.updates == []
    assert res.new_statements == []
    assert res.summary == ""


# --------------------------------------------------------- questions


@pytest.mark.asyncio
async def test_generate_batch_filters_missing_predictions(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _add(state, id="s1", weight=0.5)
    _stub_llm(monkeypatch, json.dumps({
        "questions": [
            {"q": "Q1", "prediction": "Yes",
             "targets": ["s1", "s2", "s3", "s4"], "rationale": "r"},
            {"q": "Q2 (no prediction)", "targets": ["s1"]},
            {"q": "", "prediction": "x"},  # empty q
            {"q": "Q3", "prediction": "No", "targets": ["s1"], "rationale": ""},
        ]
    }))
    out = await questions_mod.generate_batch(state, count=5)
    assert [q.q for q in out] == ["Q1", "Q3"]
    # Targets capped at 3.
    assert out[0].targets == ["s1", "s2", "s3"]


@pytest.mark.asyncio
async def test_generate_single_returns_none_on_empty(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _stub_llm(monkeypatch, json.dumps({"q": "", "prediction": ""}))
    out = await questions_mod.generate_single(state, asked_in_session=[])
    assert out is None


# ------------------------------------------------------ reviews


def test_settle_pre_filter_picks_high_and_low() -> None:
    state = _make_state()
    _add(state, id="s1", weight=0.91)
    _add(state, id="s2", weight=0.05)
    _add(state, id="s3", weight=0.5)
    _add(state, id="s4", weight=0.92, settle_proposed=True)  # already proposed
    out = reviews_mod._settle_candidates(state)
    assert {s.id for s in out} == {"s1", "s2"}


def test_stale_pre_filter_requires_history_and_low_movement() -> None:
    state = _make_state()
    a = _add(state, id="s1", weight=0.5)
    a.history = [
        {"run_id": "r1", "delta": 0.01, "rationale": "x", "source": "passive"},
        {"run_id": "r2", "delta": 0.01, "rationale": "x", "source": "passive"},
        {"run_id": "r3", "delta": 0.01, "rationale": "x", "source": "passive"},
        {"run_id": "r4", "delta": 0.01, "rationale": "x", "source": "passive"},
    ]
    b = _add(state, id="s2", weight=0.5)
    b.history = [
        {"run_id": "r1", "delta": 0.2, "rationale": "x", "source": "passive"},
    ]
    c = _add(state, id="s3", weight=0.5, kept_stale=True)
    c.history = [
        {"run_id": "r1", "delta": 0.0, "rationale": "x", "source": "passive"},
        {"run_id": "r2", "delta": 0.0, "rationale": "x", "source": "passive"},
        {"run_id": "r3", "delta": 0.0, "rationale": "x", "source": "passive"},
        {"run_id": "r4", "delta": 0.0, "rationale": "x", "source": "passive"},
    ]
    out = reviews_mod._stale_candidates(state)
    assert {s.id for s in out} == {"s1"}


@pytest.mark.asyncio
async def test_propose_calls_llm_only_with_candidates(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _add(state, id="s1", weight=0.91)  # settle candidate
    _stub_llm(monkeypatch, json.dumps({
        "settle": [
            {"id": "s1", "direction": "yes", "question": "Confirm?",
             "reasoning": "weight 0.91"}
        ],
        "stale": [],
    }))
    out = await reviews_mod.propose(state, run_id="r1", run_iso="2026-05-01T09:00:00Z")
    assert len(out.settle) == 1
    assert out.settle[0].statement_id == "s1"
    assert out.settle[0].direction == "yes"
    assert out.stale == []


@pytest.mark.asyncio
async def test_propose_skips_unknown_ids(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM hallucinated an id that wasn't a candidate — drop."""
    state = _make_state()
    _add(state, id="s1", weight=0.91)
    _stub_llm(monkeypatch, json.dumps({
        "settle": [
            {"id": "s99", "direction": "yes", "question": "?", "reasoning": ""}
        ],
        "stale": [],
    }))
    out = await reviews_mod.propose(state, run_id="r1", run_iso="2026-05-01T09:00:00Z")
    assert out.settle == []


@pytest.mark.asyncio
async def test_detect_duplicates_validates_clusters(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _add(state, id="s1", text="customers are technical", region="customers")
    _add(state, id="s2", text="our customers are engineers", region="customers")
    _add(state, id="s3", text="distinct claim", region="auth")
    _stub_llm(monkeypatch, json.dumps({
        "duplicates": [
            {"ids": ["s1", "s2"], "merged_text": "Customers are technical",
             "merged_weight": 0.65, "region": "customers", "reasoning": "same claim"},
            {"ids": ["s99", "s100"], "merged_text": "x", "merged_weight": 0.5,
             "region": "x"},  # unknown ids, drop
            {"ids": ["s1"], "merged_text": "x", "merged_weight": 0.5},  # too small
        ]
    }))
    out = await reviews_mod.detect_duplicates(
        state, run_id="r1", run_iso="2026-05-01T09:00:00Z",
    )
    assert len(out) == 1
    assert sorted(out[0].cluster_ids) == ["s1", "s2"]
    assert out[0].region == "customers"


@pytest.mark.asyncio
async def test_detect_duplicates_skips_already_proposed(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _add(state, id="s1", dupe_proposed=True)
    _add(state, id="s2", dupe_proposed=True)
    invocations = _stub_llm(monkeypatch, '{"duplicates": []}')
    out = await reviews_mod.detect_duplicates(
        state, run_id="r1", run_iso="2026-05-01T09:00:00Z",
    )
    # Both eligible candidates are flagged → < 2 fresh ones → skip the LLM.
    assert out == []
    assert invocations == []


def test_expire_old_proposals_drops_at_threshold() -> None:
    from server.compass import config
    from server.compass.store import SettleProposal, StaleProposal, DuplicateProposal

    settle = [
        SettleProposal(statement_id="s1", direction="yes", question="?",
                       reasoning="", proposed_at="t", proposed_in_run="r1",
                       pending_runs=config.PROPOSAL_EXPIRY_RUNS),
        SettleProposal(statement_id="s2", direction="yes", question="?",
                       reasoning="", proposed_at="t", proposed_in_run="r1",
                       pending_runs=0),
    ]
    stale: list[StaleProposal] = []
    dupes: list[DuplicateProposal] = []
    s2, st2, d2 = reviews_mod.expire_old_proposals(settle, stale, dupes)
    assert [p.statement_id for p in s2] == ["s2"]


def test_mark_proposed_flags_sets_in_state() -> None:
    state = _make_state()
    _add(state, id="s1")
    _add(state, id="s2")
    _add(state, id="s3")
    from server.compass.store import SettleProposal, DuplicateProposal

    reviews_mod.mark_proposed_flags(
        state,
        settle=[SettleProposal(
            statement_id="s1", direction="yes", question="?",
            reasoning="", proposed_at="t", proposed_in_run="r1",
        )],
        stale=[],
        dupes=[DuplicateProposal(
            id="dupe1",
            cluster_ids=["s2", "s3"],
            merged_text="x", merged_weight=0.5, region="r",
            reasoning="", proposed_at="t", proposed_in_run="r1",
        )],
    )
    by_id = {s.id: s for s in state.statements}
    assert by_id["s1"].settle_proposed is True
    assert by_id["s2"].dupe_proposed is True
    assert by_id["s3"].dupe_proposed is True


# ------------------------------------------------------ regions


@pytest.mark.asyncio
async def test_auto_merge_no_op_below_soft_cap(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    state.regions.append(Region(name="pricing", created_at="t"))
    invocations = _stub_llm(monkeypatch, '{"merges": []}')
    out = await regions_mod.auto_merge(state)
    assert out == []
    assert invocations == []


@pytest.mark.asyncio
async def test_auto_merge_drops_self_target_and_unknowns(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    # Build > soft cap regions.
    from server.compass import config
    for i in range(config.REGION_SOFT_CAP + 2):
        state.regions.append(Region(name=f"r{i}", created_at="t"))
    _stub_llm(monkeypatch, json.dumps({
        "merges": [
            # Valid: r1 -> r0
            {"from": ["r1"], "to": "r0", "reasoning": "close"},
            # Self-target should be filtered out of `from_`.
            {"from": ["r2", "r2"], "to": "r2", "reasoning": "self"},
            # Unknown source should be filtered.
            {"from": ["nope"], "to": "r0", "reasoning": "x"},
        ]
    }))
    out = await regions_mod.auto_merge(state)
    assert len(out) == 1
    assert out[0].from_ == ["r1"]
    assert out[0].to == "r0"


# ------------------------------------------------- truth_check


@pytest.mark.asyncio
async def test_truth_check_empty_truth_skips_llm(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocations = _stub_llm(monkeypatch, "{}")
    res = await truth_check_mod.check(
        truth=[],
        question_text="Q",
        prediction="P",
        answer_text="A",
    )
    assert res.contradiction is False
    assert invocations == []


@pytest.mark.asyncio
async def test_truth_check_requires_conflicts_for_contradiction(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM said `contradiction=true` but listed no conflicts. We
    downgrade to no-contradiction — without a referenced fact the
    modal has nothing to show."""
    _stub_llm(monkeypatch, json.dumps({
        "contradiction": True,
        "conflicts": [],
        "summary": "vague",
    }))
    res = await truth_check_mod.check(
        truth=[TruthFact(index=1, text="Some fact", added_at="t")],
        question_text="Q",
        prediction="P",
        answer_text="A",
    )
    assert res.contradiction is False


@pytest.mark.asyncio
async def test_truth_check_real_contradiction(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_llm(monkeypatch, json.dumps({
        "contradiction": True,
        "conflicts": [
            {"truth_index": 1, "explanation": "Answer says we have many operators."}
        ],
        "summary": "conflicts with truth #1",
    }))
    res = await truth_check_mod.check(
        truth=[TruthFact(index=1, text="There is one human operator", added_at="t")],
        question_text="Q",
        prediction="P",
        answer_text="A",
    )
    assert res.contradiction is True
    assert res.conflicts[0]["truth_index"] == 1
    assert "operators" in res.conflicts[0]["explanation"]


# --------------------------------------------------------- briefing


@pytest.mark.asyncio
async def test_briefing_empty_lattice_returns_placeholder(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    invocations = _stub_llm(monkeypatch, "ignored")
    out = await briefing_mod.generate(state, recent={})
    assert "Lattice is empty" in out or "placeholder" in out
    assert invocations == []  # short-circuit


@pytest.mark.asyncio
async def test_briefing_renders_llm_text(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _add(state, id="s1", weight=0.91)
    _stub_llm(monkeypatch, "## CONFIRMED YES\n- s1 (0.91)\n")
    out = await briefing_mod.generate(state, recent={"updates": 0})
    assert out.startswith("## CONFIRMED YES")

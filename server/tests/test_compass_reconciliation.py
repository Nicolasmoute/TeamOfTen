"""Reconciliation pass (spec §3.0.1) — unit tests for the pipeline,
mutate helpers, and storage layer.

End-to-end runner integration is covered in
`test_compass_runner.py::test_truth_derive_*` plus the dedicated
runner tests added below. API endpoint resolution lives in
`test_compass_api.py`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from server.compass import config as cmp_config
from server.compass import llm as llm_mod
from server.compass import mutate
from server.compass import store
from server.compass.pipeline import reconciliation as pl_reconcile
from server.compass.store import (
    LatticeState,
    ReconciliationProposal,
    Statement,
)


# -------------------------------------------------------- LLM stubs


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
    invocations: list[dict[str, Any]] = []

    async def _fake(system: str, user: str, **kwargs: Any) -> _FakeResult:
        invocations.append({"system": system, "user": user, **kwargs})
        return _FakeResult(text=response_text)

    monkeypatch.setattr(llm_mod, "call", _fake)
    monkeypatch.setattr(pl_reconcile.llm, "call", _fake)
    return invocations


def _make_state() -> LatticeState:
    return LatticeState(project_id="alpha")


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


def _add_truth(state: LatticeState, text: str = "T1: stable", index: int = 1) -> None:
    state.truth.append(store.TruthFact(index=index, text=text, added_at="t"))


# -------------------------------------------------- pipeline.detect_conflicts


@pytest.mark.asyncio
async def test_detect_conflicts_returns_empty_without_truth(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _add(state, id="s1", weight=0.9)
    invocations = _stub_llm(monkeypatch, '{"conflicts": []}')
    out = await pl_reconcile.detect_conflicts(
        state, run_id="r1", run_iso="2026-05-01T09:00:00Z",
    )
    assert out == []
    assert invocations == []  # no truth → no LLM call


@pytest.mark.asyncio
async def test_detect_conflicts_returns_empty_without_lattice(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _add_truth(state)
    invocations = _stub_llm(monkeypatch, '{"conflicts": []}')
    out = await pl_reconcile.detect_conflicts(
        state, run_id="r1", run_iso="2026-05-01T09:00:00Z",
    )
    assert out == []
    assert invocations == []  # no lattice → no LLM call


@pytest.mark.asyncio
async def test_detect_conflicts_skips_already_flagged_statements(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _add_truth(state)
    _add(state, id="s1", weight=0.9, reconciliation_proposed=True)
    _add(state, id="s2", weight=0.9, reconciliation_ambiguity=True)
    invocations = _stub_llm(monkeypatch, '{"conflicts": []}')
    out = await pl_reconcile.detect_conflicts(
        state, run_id="r1", run_iso="t",
    )
    assert out == []
    assert invocations == []  # all eligible rows already in human-resolution state


@pytest.mark.asyncio
async def test_detect_conflicts_parses_and_sanitizes(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _add_truth(state, text="T1: per-task billing is required")
    s = _add(
        state, id="s7",
        text="Billing is per-second",
        weight=1.0, archived=True, settled_as="yes",
    )
    _stub_llm(monkeypatch, json.dumps({
        "conflicts": [
            {
                "statement_id": "s7",
                "corpus_paths": ["billing.md"],
                "explanation": "Corpus says per-task; s7 settled at per-second.",
                "suggested_resolution": "update_lattice",
            },
            {  # garbage entry — drop
                "statement_id": "doesnt-exist",
                "explanation": "x",
            },
            {  # missing explanation — drop
                "statement_id": "s7",
                "corpus_paths": ["x.md"],
            },
        ]
    }))
    out = await pl_reconcile.detect_conflicts(
        state, run_id="r1", run_iso="2026-05-01T09:00:00Z",
    )
    assert len(out) == 1
    p = out[0]
    assert p.statement_id == "s7"
    assert p.corpus_paths == ["billing.md"]
    assert p.statement_archived is True
    assert p.suggested_resolution == "update_lattice"
    assert p.proposed_in_run == "r1"
    assert p.id.startswith("rec")
    # Stub-allocator stubs were cleaned up.
    assert state.reconciliation_proposals == []


@pytest.mark.asyncio
async def test_detect_conflicts_collapses_invalid_resolution(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _make_state()
    _add_truth(state)
    _add(state, id="s1", weight=0.92, archived=True, settled_as="yes")
    _stub_llm(monkeypatch, json.dumps({
        "conflicts": [{
            "statement_id": "s1",
            "corpus_paths": ["a.md"],
            "explanation": "x",
            "suggested_resolution": "totally-bogus",
        }]
    }))
    out = await pl_reconcile.detect_conflicts(
        state, run_id="r1", run_iso="t",
    )
    assert len(out) == 1
    assert out[0].suggested_resolution == "either"


def test_expire_old_proposals() -> None:
    keep = ReconciliationProposal(
        id="rec1", statement_id="s1", statement_archived=True,
        corpus_paths=[], explanation="", pending_runs=0,
    )
    drop = ReconciliationProposal(
        id="rec2", statement_id="s2", statement_archived=False,
        corpus_paths=[], explanation="",
        pending_runs=cmp_config.PROPOSAL_EXPIRY_RUNS,
    )
    out = pl_reconcile.expire_old_proposals([keep, drop])
    assert [p.id for p in out] == ["rec1"]


def test_increment_pending_runs() -> None:
    p = ReconciliationProposal(
        id="rec1", statement_id="s1", statement_archived=False,
        corpus_paths=[], explanation="", pending_runs=2,
    )
    pl_reconcile.increment_pending_runs([p])
    assert p.pending_runs == 3


# ------------------------------------------------------- mutate helpers


def test_reconcile_unarchive_resets_to_active() -> None:
    state = _make_state()
    s = _add(
        state, id="s1", weight=1.0, archived=True, archived_at="t",
        settled_as="yes", settled_by_human=True, reconciliation_proposed=True,
    )
    out = mutate.reconcile_unarchive(state, "s1", run_id="human", new_weight=0.5)
    assert out is s
    assert s.archived is False
    assert s.archived_at is None
    assert s.settled_as is None
    assert s.weight == pytest.approx(0.5)
    assert s.reconciliation_proposed is False
    assert s.history[-1]["source"] == "reconcile:unarchive"


def test_reconcile_flip_archive_changes_direction() -> None:
    state = _make_state()
    s = _add(
        state, id="s1", weight=1.0, archived=True,
        settled_as="yes", settled_by_human=True,
    )
    out = mutate.reconcile_flip_archive(state, "s1", run_id="human")
    assert out is s
    assert s.settled_as == "no"
    assert s.weight == pytest.approx(0.0)
    assert s.archived is True
    assert s.history[-1]["source"] == "reconcile:flip"


def test_reconcile_flip_archive_rejects_non_directional_settle() -> None:
    state = _make_state()
    _add(
        state, id="s1", weight=0.0, archived=True,
        settled_as="merged",  # not yes/no
    )
    out = mutate.reconcile_flip_archive(state, "s1", run_id="human")
    assert out is None  # caller falls back to unarchive + override


def test_reconcile_reformulate_clears_history() -> None:
    state = _make_state()
    s = _add(
        state, id="s1", text="old wording", weight=0.92, archived=True,
        settled_as="yes",
    )
    s.history = [{"run_id": "r0", "delta": 0.1, "rationale": "x", "source": "passive"}]
    out = mutate.reconcile_reformulate(
        state, "s1", "Fresh framing per corpus", run_id="human",
    )
    assert out is s
    assert s.text == "Fresh framing per corpus"
    assert s.weight == pytest.approx(0.5)
    assert s.archived is False
    assert s.reformulated is True
    assert len(s.history) == 1
    assert s.history[0]["source"] == "reconcile:reformulate"


def test_reconcile_replace_archives_old_and_inserts_new() -> None:
    state = _make_state()
    _add(
        state, id="s5", text="old claim", weight=1.0, archived=True,
        settled_as="yes", region="pricing",
    )
    out = mutate.reconcile_replace(
        state, "s5",
        new_text="Pricing is per-task per the corpus",
        region="pricing",
        run_id="human",
    )
    assert out is not None
    old, new_stmt = out
    assert old.id == "s5"
    assert old.archived is True
    assert old.settled_as == "reconciled"
    assert new_stmt.id != "s5"
    assert new_stmt.text == "Pricing is per-task per the corpus"
    assert new_stmt.weight == pytest.approx(0.75)
    assert new_stmt.created_by == "compass-truth"
    assert new_stmt.merged_from == ["s5"]


def test_reconcile_accept_ambiguity_sets_flag() -> None:
    state = _make_state()
    s = _add(state, id="s1", reconciliation_proposed=True)
    out = mutate.reconcile_accept_ambiguity(state, "s1")
    assert out is s
    assert s.reconciliation_proposed is False
    assert s.reconciliation_ambiguity is True


def test_mark_reconciliation_proposed_clears_ambiguity() -> None:
    """When the LLM re-flags a row after a corpus shift, the
    ambiguity-accepted flag should clear so the new proposal is
    actionable."""
    state = _make_state()
    _add(state, id="s1", reconciliation_ambiguity=True)
    _add(state, id="s2")
    mutate.mark_reconciliation_proposed(state, ["s1"])
    by_id = {s.id: s for s in state.statements}
    assert by_id["s1"].reconciliation_proposed is True
    assert by_id["s1"].reconciliation_ambiguity is False
    assert by_id["s2"].reconciliation_proposed is False


# ----------------------------------------------------- storage round-trip


@pytest.mark.asyncio
async def test_save_proposals_reconcile_roundtrip(fresh_db: str) -> None:
    await store.bootstrap_state("alpha")
    p = ReconciliationProposal(
        id="rec1",
        statement_id="s7",
        statement_archived=True,
        corpus_paths=["specs.md"],
        explanation="specs say A; lattice settled B.",
        suggested_resolution="update_lattice",
        proposed_at="t",
        proposed_in_run="r1",
        pending_runs=1,
    )
    await store.save_proposals(
        "alpha", settle=None, stale=None, dupes=None, reconcile=[p],
    )
    loaded = store.load_state("alpha")
    assert len(loaded.reconciliation_proposals) == 1
    got = loaded.reconciliation_proposals[0]
    assert got.id == "rec1"
    assert got.statement_id == "s7"
    assert got.corpus_paths == ["specs.md"]
    assert got.suggested_resolution == "update_lattice"
    assert got.pending_runs == 1


@pytest.mark.asyncio
async def test_bootstrap_seeds_empty_reconciliation_file(fresh_db: str) -> None:
    cp = await store.bootstrap_state("alpha")
    assert cp.reconciliation_proposals.exists()
    state = store.load_state("alpha")
    assert state.reconciliation_proposals == []


def test_next_reconciliation_id_is_monotonic() -> None:
    state = _make_state()
    assert store.next_reconciliation_id(state) == "rec1"
    state.reconciliation_proposals.append(ReconciliationProposal(
        id="rec3", statement_id="s1", statement_archived=False,
        corpus_paths=[], explanation="",
    ))
    assert store.next_reconciliation_id(state) == "rec4"

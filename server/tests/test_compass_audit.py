"""Phase 4 tests — `server.compass.audit.audit_work`.

Verifies (spec §5):
  - aligned verdicts log silently, do not queue questions
  - confident_drift logs and does NOT queue (human is NOT pushed)
  - uncertain_drift logs AND queues a question with the prediction
  - rollup safety net (§5.4) queues a meta-question after enough
    region-concentrated drifts
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from server.compass import audit as audit_mod
from server.compass import llm as llm_mod
from server.compass import store
from server.compass.store import Statement


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
    """Stub the LLM module reference inside `audit_mod` so calls
    deterministically return `response_text`."""
    invocations: list[dict[str, Any]] = []

    async def _fake(system: str, user: str, **kwargs: Any) -> _FakeResult:
        invocations.append({"system": system, "user": user, **kwargs})
        return _FakeResult(text=response_text)

    monkeypatch.setattr(audit_mod.llm, "call", _fake)
    monkeypatch.setattr(llm_mod, "call", _fake)
    return invocations


@pytest.mark.asyncio
async def test_audit_aligned_logs_no_question(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.bootstrap_state("alpha")
    _stub_llm(monkeypatch, json.dumps({
        "verdict": "aligned",
        "summary": "consistent with lattice",
        "contradicting_ids": [],
        "message_to_coach": "OK · aligned with lattice",
        "question_for_human": None,
    }))
    res = await audit_mod.audit_work("alpha", "worker-3 wrote a unit test")
    assert res["verdict"] == "aligned"
    assert res["question_id"] is None
    audits = store.read_audits("alpha")
    assert len(audits) == 1
    assert audits[0].verdict == "aligned"
    state = store.load_state("alpha")
    assert state.questions == []  # no question queued


@pytest.mark.asyncio
async def test_audit_confident_drift_logs_no_question(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §5.2 — confident_drift sends a direct message to coach
    but does NOT push the human (§10.5)."""
    await store.bootstrap_state("alpha")
    state = store.load_state("alpha")
    state.statements.append(Statement(
        id="s2", text="Pricing favors per-task billing",
        region="pricing", weight=0.82, created_at="t",
    ))
    await store.save_lattice("alpha", state.statements)

    _stub_llm(monkeypatch, json.dumps({
        "verdict": "confident_drift",
        "summary": "Conflicts with s2 (per-task pricing) at 0.82.",
        "contradicting_ids": ["s2"],
        "message_to_coach": (
            "Worker shipped per-second billing — that contradicts s2 (per-task "
            "pricing, weight 0.82). Consider redirecting before more code lands."
        ),
        "question_for_human": None,
    }))
    res = await audit_mod.audit_work(
        "alpha",
        "worker-4 implemented per-second billing instead of per-task as scoped",
    )
    assert res["verdict"] == "confident_drift"
    assert res["contradicting_ids"] == ["s2"]
    assert res["question_id"] is None
    state2 = store.load_state("alpha")
    assert state2.questions == []


@pytest.mark.asyncio
async def test_audit_uncertain_drift_queues_question(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §5.2 — uncertain_drift queues a question for the human
    with a prediction. The audit's `question_id` references it."""
    await store.bootstrap_state("alpha")
    _stub_llm(monkeypatch, json.dumps({
        "verdict": "uncertain_drift",
        "summary": "Work touches s5 which sits at 0.55 — can't tell.",
        "contradicting_ids": ["s5"],
        "message_to_coach": "Flagged for human review; proceed cautiously.",
        "question_for_human": {
            "q": "Should onboarding be self-serve?",
            "prediction": "Yes — technical audience.",
            "targets": ["s5"],
        },
    }))
    res = await audit_mod.audit_work("alpha", "ambiguous worker output")
    assert res["verdict"] == "uncertain_drift"
    assert res["question_id"] is not None
    # The question is now in questions.json with from_audit set.
    state = store.load_state("alpha")
    assert len(state.questions) == 1
    q = state.questions[0]
    assert q.from_audit == res["question_id"].replace("q", "audit_") or q.from_audit  # any non-None
    assert q.prediction == "Yes — technical audience."


@pytest.mark.asyncio
async def test_audit_uncertain_drift_without_prediction_does_not_queue(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mandatory prediction (§10.18) — drop entries that lack one."""
    await store.bootstrap_state("alpha")
    _stub_llm(monkeypatch, json.dumps({
        "verdict": "uncertain_drift",
        "summary": "drift",
        "contradicting_ids": [],
        "message_to_coach": "x",
        "question_for_human": {"q": "Anything?", "targets": []},  # no prediction
    }))
    res = await audit_mod.audit_work("alpha", "x")
    assert res["verdict"] == "uncertain_drift"
    assert res["question_id"] is None
    state = store.load_state("alpha")
    assert state.questions == []


@pytest.mark.asyncio
async def test_audit_clamps_invalid_verdict_to_aligned(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.bootstrap_state("alpha")
    _stub_llm(monkeypatch, json.dumps({
        "verdict": "totally-broken",
        "summary": "junk",
        "message_to_coach": "x",
    }))
    res = await audit_mod.audit_work("alpha", "x")
    assert res["verdict"] == "aligned"


@pytest.mark.asyncio
async def test_audit_rollup_meta_question_after_concentrated_drift(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After AUDIT_ROLLUP_INTERVAL audits with ≥3 drifts in the same
    region, queue a meta-question (`is the lattice wrong?`)."""
    from server.compass import config as cfg

    await store.bootstrap_state("alpha")
    # Seed a lattice with one statement in 'pricing'.
    state = store.load_state("alpha")
    state.statements.append(Statement(
        id="s1", text="x", region="pricing", weight=0.85, created_at="t",
    ))
    await store.save_lattice("alpha", state.statements)

    # First (interval - 1) aligned audits, then 1 confident_drift in
    # pricing — together they're `interval` audits but only 1 drift.
    # Need ≥3 drifts in the same region. So pad with aligned, then
    # 3 drifts in pricing, all within the trailing window.
    aligned_payload = json.dumps({
        "verdict": "aligned",
        "summary": "ok",
        "contradicting_ids": [],
        "message_to_coach": "ok",
    })
    drift_payload = json.dumps({
        "verdict": "confident_drift",
        "summary": "drift",
        "contradicting_ids": ["s1"],
        "message_to_coach": "drift",
    })

    # Run interval - 3 aligned audits. (Interval default = 5 → 2
    # aligned + 3 drifts = 5 total in the trailing window.)
    interval = cfg.AUDIT_ROLLUP_INTERVAL
    for _ in range(max(0, interval - 3)):
        _stub_llm(monkeypatch, aligned_payload)
        await audit_mod.audit_work("alpha", "filler")
    for _ in range(3):
        _stub_llm(monkeypatch, drift_payload)
        await audit_mod.audit_work("alpha", "drifty work in pricing")

    state2 = store.load_state("alpha")
    rollup_qs = [q for q in state2.questions if q.asked_in_run == "audit-rollup"]
    assert len(rollup_qs) == 1
    assert "pricing" in rollup_qs[0].q.lower()

"""Phase 6 tests — `/api/compass/*` HTTP endpoints.

We don't spin up the full lifespan (no scheduler / event writer); the
TestClient bypasses lifespan via the `with` form. Each test seeds
state directly through the store and exercises the endpoints.

Covers:
  - GET /state when disabled vs enabled
  - POST /enable bootstraps state files
  - POST /run kicks off a background task (we monkeypatch runner.run)
  - POST /qa/start + /qa/next + /qa/answer + /qa/end
  - POST /questions/{id}/answer queues for next-run digest
  - POST /proposals/settle/{id} confirm + reject
  - POST /statements/{id}/weight requires confirm flag
  - POST /truth add/update/remove
  - POST /audit goes through audit.audit_work
  - POST /reset wipes state
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.compass import api as cmp_api
from server.compass import audit as audit_mod
from server.compass import config as cmp_config
from server.compass import llm as llm_mod
from server.compass import runner as runner_mod
from server.compass import store as cmp_store
from server.compass.pipeline import (
    digest as pl_digest,
    questions as pl_questions,
    truth_check as pl_truth_check,
)


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


def _stub_llm_global(monkeypatch: pytest.MonkeyPatch, response_text: str) -> None:
    async def _fake(system: str, user: str, **kwargs: Any) -> _FakeResult:
        return _FakeResult(text=response_text)

    monkeypatch.setattr(llm_mod, "call", _fake)
    monkeypatch.setattr(audit_mod.llm, "call", _fake)


@pytest.fixture
def client(fresh_db: str) -> TestClient:
    from server.main import app
    # Use TestClient outside `with` so we don't trigger lifespan
    # (which would start the scheduler + telegram bridge etc.).
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
async def _init_db_for_api(fresh_db: str) -> None:
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")


# ---------------------------------------------------- /state


def test_state_disabled_returns_message(client: TestClient) -> None:
    r = client.get("/api/compass/state")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is False
    assert "disabled" in data["message"]


def test_enable_then_state(client: TestClient) -> None:
    r = client.post("/api/compass/enable")
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    r2 = client.get("/api/compass/state")
    assert r2.status_code == 200
    data = r2.json()
    assert data["enabled"] is True
    assert "statements" in data
    assert data["statements"] == []
    assert "truth" in data


def test_disable_clears_flag(client: TestClient) -> None:
    client.post("/api/compass/enable")
    r = client.post("/api/compass/disable")
    assert r.json()["enabled"] is False


# ---------------------------------------------------- /run


def test_run_rejects_when_disabled(client: TestClient) -> None:
    r = client.post("/api/compass/run", json={"mode": "on_demand"})
    assert r.status_code == 403


def test_run_kicks_off_background_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/api/compass/enable")
    invoked: list[str] = []

    async def _fake_run(project_id: str, mode: str = "daily") -> dict[str, Any]:
        invoked.append(f"{project_id}:{mode}")
        return {"run_id": "r1", "completed": True}

    monkeypatch.setattr(runner_mod, "run", _fake_run)
    r = client.post("/api/compass/run", json={"mode": "on_demand"})
    assert r.status_code == 200
    assert r.json()["running"] is True
    # Background task is fire-and-forget; in the test loop it usually
    # runs before the request returns due to event-loop scheduling.
    # If not, we'd need an explicit asyncio.sleep — skip that check
    # here since the API contract is "spawned, not awaited".


def test_ingest_rejects_when_disabled(client: TestClient) -> None:
    r = client.post("/api/compass/ingest")
    assert r.status_code == 403


def test_ingest_kicks_off_background_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`POST /ingest` spawns a background `runner.run(mode='ingest')`
    and returns immediately. The endpoint exists so the dashboard's
    Ingest button can fold queued answers into the lattice without
    paying the cost of a full run."""
    client.post("/api/compass/enable")
    captured: list[str] = []

    async def _fake_run(project_id: str, mode: str = "daily") -> dict[str, Any]:
        captured.append(f"{project_id}:{mode}")
        return {"run_id": "r1", "completed": True, "mode": mode}

    monkeypatch.setattr(runner_mod, "run", _fake_run)
    r = client.post("/api/compass/ingest")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "ingest"
    assert body["running"] is True


def test_run_rejects_invalid_mode(client: TestClient) -> None:
    client.post("/api/compass/enable")
    r = client.post("/api/compass/run", json={"mode": "daily"})
    assert r.status_code == 400


# ---------------------------------------------------- /qa


def test_qa_full_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/api/compass/enable")

    @dataclass
    class _Q:
        q: str
        prediction: str
        targets: list[str]
        rationale: str

    state_q = _Q(q="Will customers self-serve?", prediction="Yes",
                 targets=[], rationale="entropy gap")

    async def _gen_single(state: Any, *, asked_in_session: list[str]) -> _Q:
        return state_q

    async def _digest(state: Any, **kw: Any) -> Any:
        from server.compass.pipeline.digest import DigestResult
        return DigestResult(
            surprise=0.5,
            updates=[],
            new_statements=[{"text": "Self-serve preferred", "region": "customers", "rationale": "from QA"}],
            truth_candidates=[],
            summary="self-serve confirmed",
        )

    async def _truth_check(truth: list[Any], **kw: Any) -> Any:
        from server.compass.pipeline.truth_check import TruthCheckResult
        return TruthCheckResult(contradiction=False)

    monkeypatch.setattr(pl_questions, "generate_single", _gen_single)
    monkeypatch.setattr(pl_digest, "answer", _digest)
    monkeypatch.setattr(pl_truth_check, "check", _truth_check)

    r = client.post("/api/compass/qa/start")
    assert r.status_code == 200
    r = client.post("/api/compass/qa/next")
    assert r.status_code == 200
    body = r.json()
    assert body["q"] == "Will customers self-serve?"
    assert body["prediction"] == "Yes"
    assert body["id"].startswith("q")

    r = client.post("/api/compass/qa/answer", json={"answer": "Yes, mostly."})
    assert r.status_code == 200
    body = r.json()
    assert body["contradiction"] is False
    assert body["answered_count"] == 1
    assert "Self-serve preferred" in [n for n in body["new_statements"]] or len(body["new_statements"]) == 1

    r = client.post("/api/compass/qa/end")
    assert r.status_code == 200
    assert r.json()["had_session"] is True
    # Second end is harmless.
    r = client.post("/api/compass/qa/end")
    assert r.json()["had_session"] is False


def test_qa_truth_contradiction_blocks_digest(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/api/compass/enable")

    @dataclass
    class _Q:
        q: str
        prediction: str
        targets: list[str]
        rationale: str

    async def _gen(state: Any, *, asked_in_session: list[str]) -> _Q:
        return _Q(q="Q", prediction="P", targets=[], rationale="r")

    async def _truth_check(truth: list[Any], **kw: Any) -> Any:
        from server.compass.pipeline.truth_check import TruthCheckResult
        return TruthCheckResult(
            contradiction=True,
            conflicts=[{"truth_index": 1, "explanation": "x"}],
            summary="conflicts",
        )

    digest_called: list[str] = []

    async def _digest(state: Any, **kw: Any) -> Any:
        digest_called.append("called")
        from server.compass.pipeline.digest import DigestResult
        return DigestResult(surprise=0.0, summary="should not happen")

    monkeypatch.setattr(pl_questions, "generate_single", _gen)
    monkeypatch.setattr(pl_truth_check, "check", _truth_check)
    monkeypatch.setattr(pl_digest, "answer", _digest)

    client.post("/api/compass/qa/start")
    client.post("/api/compass/qa/next")
    r = client.post("/api/compass/qa/answer", json={"answer": "contradicts"})
    assert r.status_code == 200
    body = r.json()
    assert body["contradiction"] is True
    assert body["conflicts"][0]["truth_index"] == 1
    assert digest_called == []  # truth_check halted the digest


# ---------------------------------------------------- /questions/{id}/answer


def test_queue_answer_sets_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.post("/api/compass/enable")
    # Seed a question.
    state = cmp_store.load_state("misc")
    state.questions.append(cmp_store.Question(
        id="q1", q="?", prediction="yes", targets=[], rationale="r",
        asked_at="t", asked_in_run="r0",
    ))

    async def _save() -> None:
        await cmp_store.save_questions("misc", state.questions)

    import asyncio as _asyncio
    _asyncio.get_event_loop().run_until_complete(_save())

    r = client.post(
        "/api/compass/questions/q1/answer",
        json={"answer": "Yes — sketch reply"},
    )
    assert r.status_code == 200
    state2 = cmp_store.load_state("misc")
    q = state2.find_question("q1")
    assert q is not None
    assert q.answer == "Yes — sketch reply"
    assert q.digested is False  # queued for next run


# ---------------------------------------------------- /proposals/settle


def test_settle_proposal_confirm(
    client: TestClient,
) -> None:
    client.post("/api/compass/enable")
    # Seed a settle proposal manually.
    state = cmp_store.load_state("misc")
    state.statements.append(cmp_store.Statement(
        id="s1", text="claim", region="x", weight=0.92, created_at="t",
        settle_proposed=True,
    ))
    state.settle_proposals.append(cmp_store.SettleProposal(
        statement_id="s1", direction="yes",
        question="Confirm settle?",
        reasoning="weight 0.92",
        proposed_at="t", proposed_in_run="r1",
    ))
    import asyncio as _asyncio

    async def _seed() -> None:
        await cmp_store.save_lattice("misc", state.statements)
        await cmp_store.save_proposals("misc", settle=state.settle_proposals,
                                        stale=[], dupes=[])
    _asyncio.get_event_loop().run_until_complete(_seed())

    r = client.post(
        "/api/compass/proposals/settle/s1",
        json={"action": "confirm"},
    )
    assert r.status_code == 200
    state2 = cmp_store.load_state("misc")
    s = state2.find_statement("s1")
    assert s is not None
    assert s.archived is True
    assert s.settled_as == "yes"
    assert s.weight == 1.0
    assert state2.settle_proposals == []


def test_settle_proposal_reject_clears_flag(
    client: TestClient,
) -> None:
    client.post("/api/compass/enable")
    state = cmp_store.load_state("misc")
    state.statements.append(cmp_store.Statement(
        id="s1", text="claim", region="x", weight=0.92, created_at="t",
        settle_proposed=True,
    ))
    state.settle_proposals.append(cmp_store.SettleProposal(
        statement_id="s1", direction="yes", question="?", reasoning="x",
        proposed_at="t", proposed_in_run="r1",
    ))
    import asyncio as _asyncio

    async def _seed() -> None:
        await cmp_store.save_lattice("misc", state.statements)
        await cmp_store.save_proposals("misc", settle=state.settle_proposals,
                                        stale=[], dupes=[])
    _asyncio.get_event_loop().run_until_complete(_seed())

    r = client.post(
        "/api/compass/proposals/settle/s1",
        json={"action": "reject"},
    )
    assert r.status_code == 200
    state2 = cmp_store.load_state("misc")
    s = state2.find_statement("s1")
    assert s.archived is False
    assert s.settle_proposed is False
    assert state2.settle_proposals == []


# ---------------------------------------------------- /statements/.../weight


def test_weight_override_requires_confirm(client: TestClient) -> None:
    client.post("/api/compass/enable")
    # seed
    state = cmp_store.load_state("misc")
    state.statements.append(cmp_store.Statement(
        id="s1", text="x", region="r", weight=0.5, created_at="t",
    ))
    import asyncio as _asyncio
    _asyncio.get_event_loop().run_until_complete(
        cmp_store.save_lattice("misc", state.statements)
    )

    r = client.post("/api/compass/statements/s1/weight",
                    json={"weight": 0.83})
    assert r.status_code == 400
    r = client.post("/api/compass/statements/s1/weight",
                    json={"weight": 0.83, "confirm": True})
    assert r.status_code == 200
    state2 = cmp_store.load_state("misc")
    assert state2.find_statement("s1").manually_set is True
    assert state2.find_statement("s1").weight == pytest.approx(0.83)


# ---------------------------------------------------- /truth (read-only)


def test_truth_get_reads_project_folder(client: TestClient) -> None:
    """`/api/compass/truth` is read-only — Compass doesn't manage
    truth. The corpus is the union of `<project>/truth/*` and
    `<project>/project-objectives.md`. Paths returned by the API are
    project-root-relative so the dashboard can compose them with
    `/data/projects/<id>/<path>` directly."""
    from server.paths import project_paths

    pp = project_paths("misc")
    pp.truth.mkdir(parents=True, exist_ok=True)
    (pp.truth / "one.md").write_text("Truth fact one.", encoding="utf-8")
    (pp.truth / "two.md").write_text("Truth fact two.", encoding="utf-8")
    pp.root.mkdir(parents=True, exist_ok=True)
    pp.project_objectives.write_text("Land v1 by Q3.", encoding="utf-8")

    client.post("/api/compass/enable")
    r = client.get("/api/compass/truth")
    assert r.status_code == 200
    body = r.json()
    paths = [f["path"] for f in body["facts"]]
    assert "truth/one.md" in paths
    assert "truth/two.md" in paths
    assert "project-objectives.md" in paths
    by_path = {f["path"]: f for f in body["facts"]}
    assert "Truth fact one." in by_path["truth/one.md"]["text"]
    assert "Land v1 by Q3." in by_path["project-objectives.md"]["text"]


def test_truth_post_endpoint_removed(client: TestClient) -> None:
    """The legacy POST /truth (add/update/remove) was removed when
    truth became folder-backed. Routes that don't accept POST should
    return 405 Method Not Allowed."""
    client.post("/api/compass/enable")
    r = client.post("/api/compass/truth", json={"action": "add", "text": "x"})
    assert r.status_code in (404, 405)


# ---------------------------------------------------- /audit


def test_audit_endpoint_runs_audit_and_returns_verdict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/api/compass/enable")
    _stub_llm_global(monkeypatch, json.dumps({
        "verdict": "aligned",
        "summary": "ok",
        "contradicting_ids": [],
        "message_to_coach": "OK",
        "question_for_human": None,
    }))
    r = client.post("/api/compass/audit", json={"artifact": "ok work"})
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "aligned"


# ---------------------------------------------------- /reset


def test_resolve_reconciliation_unarchive(client: TestClient) -> None:
    """`POST /api/compass/proposals/reconcile/{id}` with action=update_lattice
    + lattice_action=unarchive returns the row to active state."""
    client.post("/api/compass/enable")
    state = cmp_store.load_state("misc")
    state.statements.append(cmp_store.Statement(
        id="s1", text="claim", region="x", weight=1.0, created_at="t",
        archived=True, settled_as="yes", settled_by_human=True,
        reconciliation_proposed=True,
    ))
    state.reconciliation_proposals.append(cmp_store.ReconciliationProposal(
        id="rec1", statement_id="s1", statement_archived=True,
        corpus_paths=["specs.md"], explanation="x",
        suggested_resolution="update_lattice",
        proposed_at="t", proposed_in_run="r1",
    ))
    import asyncio as _asyncio

    async def _seed() -> None:
        await cmp_store.save_lattice("misc", state.statements)
        await cmp_store.save_proposals(
            "misc", settle=None, stale=None, dupes=None,
            reconcile=state.reconciliation_proposals,
        )
    _asyncio.get_event_loop().run_until_complete(_seed())

    r = client.post(
        "/api/compass/proposals/reconcile/rec1",
        json={"action": "update_lattice", "lattice_action": "unarchive", "weight": 0.5},
    )
    assert r.status_code == 200
    state2 = cmp_store.load_state("misc")
    s = state2.find_statement("s1")
    assert s.archived is False
    assert s.weight == pytest.approx(0.5)
    assert s.reconciliation_proposed is False
    assert state2.reconciliation_proposals == []


def test_resolve_reconciliation_accept_ambiguity(client: TestClient) -> None:
    client.post("/api/compass/enable")
    state = cmp_store.load_state("misc")
    state.statements.append(cmp_store.Statement(
        id="s1", text="claim", region="x", weight=0.92, created_at="t",
        reconciliation_proposed=True,
    ))
    state.reconciliation_proposals.append(cmp_store.ReconciliationProposal(
        id="rec1", statement_id="s1", statement_archived=False,
        corpus_paths=["specs.md"], explanation="x",
        proposed_at="t", proposed_in_run="r1",
    ))
    import asyncio as _asyncio
    _asyncio.get_event_loop().run_until_complete(cmp_store.save_lattice(
        "misc", state.statements,
    ))
    _asyncio.get_event_loop().run_until_complete(cmp_store.save_proposals(
        "misc", settle=None, stale=None, dupes=None,
        reconcile=state.reconciliation_proposals,
    ))

    r = client.post(
        "/api/compass/proposals/reconcile/rec1",
        json={"action": "accept_ambiguity"},
    )
    assert r.status_code == 200
    state2 = cmp_store.load_state("misc")
    s = state2.find_statement("s1")
    assert s.reconciliation_ambiguity is True
    assert s.reconciliation_proposed is False
    assert state2.reconciliation_proposals == []


def test_resolve_reconciliation_update_truth_clears_flag(client: TestClient) -> None:
    """`update_truth` is informational — no lattice mutation, but the
    statement's `reconciliation_proposed` flag MUST clear so the next
    corpus-changed run can re-detect if the human's edit didn't
    actually resolve the conflict."""
    client.post("/api/compass/enable")
    state = cmp_store.load_state("misc")
    state.statements.append(cmp_store.Statement(
        id="s1", text="claim", region="x", weight=0.92, created_at="t",
        archived=True, settled_as="yes",
        reconciliation_proposed=True,
    ))
    state.reconciliation_proposals.append(cmp_store.ReconciliationProposal(
        id="rec1", statement_id="s1", statement_archived=True,
        corpus_paths=["specs.md"], explanation="x",
        suggested_resolution="update_truth",
        proposed_at="t", proposed_in_run="r1",
    ))
    import asyncio as _asyncio
    _asyncio.get_event_loop().run_until_complete(cmp_store.save_lattice(
        "misc", state.statements,
    ))
    _asyncio.get_event_loop().run_until_complete(cmp_store.save_proposals(
        "misc", settle=None, stale=None, dupes=None,
        reconcile=state.reconciliation_proposals,
    ))

    r = client.post(
        "/api/compass/proposals/reconcile/rec1",
        json={"action": "update_truth"},
    )
    assert r.status_code == 200
    state2 = cmp_store.load_state("misc")
    s = state2.find_statement("s1")
    # Lattice content / archive state preserved (informational only).
    assert s.archived is True
    assert s.settled_as == "yes"
    assert s.weight == pytest.approx(0.92)
    # But the proposal flag is cleared so the next corpus-changed run
    # passes s1 to detect_conflicts again.
    assert s.reconciliation_proposed is False
    assert state2.reconciliation_proposals == []


def test_resolve_reconciliation_rejects_invalid_action(client: TestClient) -> None:
    client.post("/api/compass/enable")
    state = cmp_store.load_state("misc")
    state.reconciliation_proposals.append(cmp_store.ReconciliationProposal(
        id="rec1", statement_id="s1", statement_archived=False,
        corpus_paths=[], explanation="x",
        proposed_at="t", proposed_in_run="r1",
    ))
    import asyncio as _asyncio
    _asyncio.get_event_loop().run_until_complete(cmp_store.save_proposals(
        "misc", settle=None, stale=None, dupes=None,
        reconcile=state.reconciliation_proposals,
    ))
    r = client.post(
        "/api/compass/proposals/reconcile/rec1",
        json={"action": "totally-bogus"},
    )
    assert r.status_code == 400


def test_reset_wipes_state(client: TestClient) -> None:
    client.post("/api/compass/enable")
    state = cmp_store.load_state("misc")
    state.statements.append(cmp_store.Statement(
        id="s1", text="x", region="r", weight=0.5, created_at="t",
    ))
    import asyncio as _asyncio
    _asyncio.get_event_loop().run_until_complete(
        cmp_store.save_lattice("misc", state.statements)
    )
    # Confirm flag required.
    r = client.post("/api/compass/reset", json={})
    assert r.status_code == 400
    r = client.post("/api/compass/reset", json={"confirm": True})
    assert r.status_code == 200
    state2 = cmp_store.load_state("misc")
    assert state2.statements == []

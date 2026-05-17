from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable

import pytest

from server.db import configured_conn, init_db
from server.events import bus
from server.paths import ensure_project_scaffold
from server.tools import build_coord_server
from server.truth import resolve_file_write_proposal


TASK_ID = "t-2026-05-16-44444444"
TASK_ID_2 = "t-2026-05-16-66666666"


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok(result: dict[str, Any]) -> str:
    assert not result.get("is_error"), result
    return result["content"][0]["text"]


def _err(result: dict[str, Any]) -> str:
    assert result.get("is_error"), result
    return result["content"][0]["text"]


def _proposal_id(text: str) -> int:
    m = re.search(r"#(\d+)", text)
    assert m, text
    return int(m.group(1))


async def _seed_truthgate_task(task_id: str = TASK_ID) -> None:
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks "
            "(id, project_id, title, description, status, created_by, "
            "trajectory, workflow, truthgate_verdict, blocked, blocked_reason) "
            "VALUES (?, 'misc', 'needs truth', 'missing truth rule', "
            "'truthgate', 'coach', '[{\"stage\":\"execute\",\"to\":[\"p2\"]}]', "
            "'code', 'truthgate_needs_truth_change', 1, ?)",
            (task_id, "TruthGate requires a protected truth amendment."),
        )
        await c.commit()
    finally:
        await c.close()


async def _task_row(task_id: str = TASK_ID) -> dict[str, Any]:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT truthgate_verdict, truthgate_pending_proposal_id, "
            "blocked, blocked_reason FROM tasks WHERE id = ?",
            (task_id,),
        )
        return dict(await cur.fetchone())
    finally:
        await c.close()


async def _proposal_row(proposal_id: int) -> dict[str, Any]:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT scope, path, status, metadata_json, originating_task_id "
            "FROM file_write_proposals WHERE id = ?",
            (proposal_id,),
        )
        return dict(await cur.fetchone())
    finally:
        await c.close()


async def _call_with_events(
    call: Callable[[], Awaitable[dict[str, Any]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    q = bus.subscribe()
    try:
        result = await call()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        events: list[dict[str, Any]] = []
        while True:
            try:
                events.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
        return result, events
    finally:
        bus.unsubscribe(q)


@pytest.mark.asyncio
async def test_coord_propose_truth_amendment_records_metadata_and_approval(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_truthgate_task()
    import server.agents as agents_mod

    wake_calls: list[tuple[str, str]] = []

    async def fake_wake(agent_id: str, reason: str = "", **_: Any) -> bool:
        wake_calls.append((agent_id, reason))
        return True

    monkeypatch.setattr(agents_mod, "maybe_wake_agent", fake_wake)
    pp = ensure_project_scaffold("misc")
    target = pp.truth / "phase4-approval.md"
    if target.exists():
        target.unlink()

    coach = _server_for("coach")
    text = _ok(await _handler(coach, "propose_truth_amendment")({
        "task_id": TASK_ID,
        "path": "truth/phase4-approval.md",
        "content": "# Phase 4\n\nApproved truth.\n",
        "summary": "Add Phase 4 approval rule",
        "rationale": "current truth lacks the rule",
        "evidence": "task t-1",
        "affected_docs": json.dumps(["Docs/truthgate-approach.md"]),
        "provisional_impl": "true",
        "rejection_consequence": "stop task",
        "draft_model": "latest_gpt",
        "drafted": "true",
    }))
    proposal_id = _proposal_id(text)

    proposal = await _proposal_row(proposal_id)
    assert proposal["scope"] == "truth"
    assert proposal["path"] == "phase4-approval.md"
    assert proposal["originating_task_id"] == TASK_ID
    metadata = json.loads(proposal["metadata_json"])
    assert metadata["kind"] == "truthgate_truth_amendment"
    assert metadata["originating_task_id"] == TASK_ID
    assert metadata["rationale"] == "current truth lacks the rule"
    assert metadata["affected_docs"] == ["Docs/truthgate-approach.md"]
    assert metadata["provisional_impl"] is True
    assert metadata["draft_model"] == "latest_gpt"
    assert metadata["drafted"] is True

    task = await _task_row()
    assert task["truthgate_pending_proposal_id"] == proposal_id

    res, events = await _call_with_events(
        lambda: resolve_file_write_proposal(
            proposal_id,
            new_status="approved",
            note="approved",
            actor={"source": "test"},
        )
    )
    assert res["status"] == "approved"
    assert target.read_text(encoding="utf-8") == "# Phase 4\n\nApproved truth.\n"
    resolved = [e for e in events if e.get("type") == "truth_amendment_resolved"]
    assert resolved
    assert resolved[0]["proposal_id"] == proposal_id
    assert resolved[0]["task_id"] == TASK_ID
    assert resolved[0]["status"] == "approved"
    assert resolved[0]["affected_docs"] == ["Docs/truthgate-approach.md"]

    task = await _task_row()
    assert task["truthgate_pending_proposal_id"] is None
    assert task["truthgate_verdict"] is None
    assert task["blocked"] == 0
    assert task["blocked_reason"] is None
    c = await configured_conn()
    try:
        message = dict(await (await c.execute(
            "SELECT from_id, to_id, subject, body, priority FROM messages "
            "WHERE to_id = 'coach' ORDER BY id DESC LIMIT 1",
        )).fetchone())
    finally:
        await c.close()
    assert message["from_id"] == "truthgate"
    assert message["priority"] == "interrupt"
    assert f"rerun TruthGate for {TASK_ID}" in message["subject"]
    assert "Approved truth proposal" in message["body"]
    assert "coord_run_truthgate(force=true)" in message["body"]
    assert wake_calls and wake_calls[-1][0] == "coach"


@pytest.mark.asyncio
async def test_coord_propose_truth_amendment_denial_keeps_truth_unchanged(
    fresh_db: str,
) -> None:
    await _seed_truthgate_task("t-2026-05-16-55555555")
    pp = ensure_project_scaffold("misc")
    target = pp.truth / "phase4-denial.md"
    if target.exists():
        target.unlink()

    coach = _server_for("coach")
    text = _ok(await _handler(coach, "propose_truth_amendment")({
        "task_id": "t-2026-05-16-55555555",
        "path": "phase4-denial.md",
        "content": "denied truth",
        "summary": "Try denied rule",
        "rationale": "current truth lacks the denied rule",
        "rejection_consequence": "rewrite task",
    }))
    proposal_id = _proposal_id(text)

    res, events = await _call_with_events(
        lambda: resolve_file_write_proposal(
            proposal_id,
            new_status="denied",
            note="not accepted",
            actor={"source": "test"},
        )
    )
    assert res["status"] == "denied"
    assert not target.exists()
    resolved = [e for e in events if e.get("type") == "truth_amendment_resolved"]
    assert resolved
    assert resolved[0]["proposal_id"] == proposal_id
    assert resolved[0]["task_id"] == "t-2026-05-16-55555555"
    assert resolved[0]["status"] == "denied"

    task = await _task_row("t-2026-05-16-55555555")
    assert task["truthgate_pending_proposal_id"] is None
    assert task["truthgate_verdict"] == "truthgate_needs_truth_change"
    assert task["blocked"] == 1
    assert "rewrite task" in task["blocked_reason"]


@pytest.mark.asyncio
async def test_coord_propose_truth_amendment_player_queues_existing_flow(
    fresh_db: str,
) -> None:
    await _seed_truthgate_task()
    player = _server_for("p2")
    text = _ok(await _handler(player, "propose_truth_amendment")({
        "task_id": TASK_ID,
        "path": "phase4.md",
        "content": "body",
        "summary": "why",
        "rationale": "because",
    }))
    proposal_id = _proposal_id(text)
    proposal = await _proposal_row(proposal_id)
    assert proposal["scope"] == "truth"
    assert proposal["originating_task_id"] == TASK_ID
    task = await _task_row()
    assert task["truthgate_pending_proposal_id"] == proposal_id


@pytest.mark.asyncio
async def test_coord_propose_truth_amendment_supersede_clears_old_task_pointer(
    fresh_db: str,
) -> None:
    await _seed_truthgate_task(TASK_ID)
    await _seed_truthgate_task(TASK_ID_2)
    coach = _server_for("coach")

    old_id = _proposal_id(_ok(await _handler(coach, "propose_truth_amendment")({
        "task_id": TASK_ID,
        "path": "phase4-supersede.md",
        "content": "old",
        "summary": "old",
        "rationale": "old rationale",
    })))
    new_id = _proposal_id(_ok(await _handler(coach, "propose_truth_amendment")({
        "task_id": TASK_ID_2,
        "path": "phase4-supersede.md",
        "content": "new",
        "summary": "new",
        "rationale": "new rationale",
    })))

    old = await _proposal_row(old_id)
    new = await _proposal_row(new_id)
    assert old["status"] == "superseded"
    assert new["status"] == "pending"
    old_metadata = json.loads(old["metadata_json"])
    assert old_metadata["superseded_by"] == new_id
    assert old_metadata["originating_task_id"] == TASK_ID

    old_task = await _task_row(TASK_ID)
    new_task = await _task_row(TASK_ID_2)
    assert old_task["truthgate_pending_proposal_id"] is None
    assert f"superseded by #{new_id}" in old_task["blocked_reason"]
    assert new_task["truthgate_pending_proposal_id"] == new_id


@pytest.mark.asyncio
async def test_list_file_write_proposals_exposes_truthgate_correlation(
    fresh_db: str,
) -> None:
    await _seed_truthgate_task()
    coach = _server_for("coach")
    proposal_id = _proposal_id(_ok(await _handler(coach, "propose_truth_amendment")({
        "task_id": TASK_ID,
        "path": "phase4-list-api.md",
        "content": "body",
        "summary": "why",
        "rationale": "because",
        "affected_docs": ["Docs/truthgate-approach.md"],
    })))

    from server.main import list_file_write_proposals

    data = await list_file_write_proposals(status="pending", scope="truth")
    row = next(p for p in data["proposals"] if p["id"] == proposal_id)
    assert row["originating_task_id"] == TASK_ID
    metadata = json.loads(row["metadata_json"])
    assert metadata["originating_task_id"] == TASK_ID
    assert metadata["affected_docs"] == ["Docs/truthgate-approach.md"]

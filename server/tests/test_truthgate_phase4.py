from __future__ import annotations

import json
import re
from typing import Any

import pytest

from server.db import configured_conn, init_db
from server.paths import ensure_project_scaffold
from server.tools import build_coord_server
from server.truth import resolve_file_write_proposal


TASK_ID = "t-2026-05-16-44444444"


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


@pytest.mark.asyncio
async def test_coord_propose_truth_amendment_records_metadata_and_approval(
    fresh_db: str,
) -> None:
    await _seed_truthgate_task()
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

    res = await resolve_file_write_proposal(
        proposal_id,
        new_status="approved",
        note="approved",
        actor={"source": "test"},
    )
    assert res["status"] == "approved"
    assert target.read_text(encoding="utf-8") == "# Phase 4\n\nApproved truth.\n"

    task = await _task_row()
    assert task["truthgate_pending_proposal_id"] is None
    assert task["truthgate_verdict"] is None
    assert task["blocked"] == 0
    assert task["blocked_reason"] is None


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

    res = await resolve_file_write_proposal(
        proposal_id,
        new_status="denied",
        note="not accepted",
        actor={"source": "test"},
    )
    assert res["status"] == "denied"
    assert not target.exists()

    task = await _task_row("t-2026-05-16-55555555")
    assert task["truthgate_pending_proposal_id"] is None
    assert task["truthgate_verdict"] == "truthgate_needs_truth_change"
    assert task["blocked"] == 1
    assert "rewrite task" in task["blocked_reason"]


@pytest.mark.asyncio
async def test_coord_propose_truth_amendment_is_coach_only(
    fresh_db: str,
) -> None:
    await _seed_truthgate_task()
    player = _server_for("p2")
    err = _err(await _handler(player, "propose_truth_amendment")({
        "task_id": TASK_ID,
        "path": "phase4.md",
        "content": "body",
        "summary": "why",
        "rationale": "because",
    }))
    assert "Coach-only" in err

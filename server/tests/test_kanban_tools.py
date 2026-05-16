"""Audit tests for surviving kanban MCP tools after the v2 cutover.

Covers the behaviors of:
  - coord_create_task / coord_triage_backlog TruthGate pre-dispatch
  - coord_write_task_spec (with planner-row planted directly)
  - coord_submit_audit_report (records-only, no auto-revert)
  - coord_set_task_blocked
  - coord_set_task_trajectory (v2 §4.3 — loose constraints)
  - coord_my_assignments (basic shape)
  - coord_update_task (legacy stage/status mutator carried forward)

V1-only tests (coord_assign_*, coord_claim_task, coord_accept_role,
coord_complete_execution, coord_mark_shipped, coord_advance_task_stage)
were deleted with the cutover; their v2 equivalents are covered in
test_coord_approve_stage.py / test_coord_archive_task.py /
test_coord_role_complete.py / test_coord_request_plan_review.py /
test_message_to_coach.py.
"""

from __future__ import annotations

import json
import re
from typing import Any

from server.db import configured_conn, init_db
from server.tools import build_coord_server


_STANDARD_TRAJECTORY = (
    '[{"stage":"plan","to":[]},'
    '{"stage":"execute","to":[]},'
    '{"stage":"audit_syntax","to":[]},'
    '{"stage":"audit_semantics","to":["p4"],"focus":"check x"},'
    '{"stage":"ship","to":[]}]'
)


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok_text(result: dict[str, Any]) -> str:
    assert not result.get("is_error"), f"tool returned error: {result}"
    return result["content"][0]["text"]


def _err_text(result: dict[str, Any]) -> str:
    assert result.get("is_error"), f"expected error, got {result}"
    return result["content"][0]["text"]


def _extract_task_id(body: str) -> str:
    m = re.search(r"t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}", body)
    assert m, f"no task id in body: {body}"
    return m.group(0)


def _extract_backlog_id(body: str) -> int:
    m = re.search(r"Backlog entry #(\d+)", body)
    assert m, f"no backlog id in body: {body}"
    return int(m.group(1))


async def _create_and_promote(server: Any, args: dict[str, Any]) -> str:
    create_text = _ok_text(await _handler(server, "create_task")(args))
    backlog_id = _extract_backlog_id(create_text)
    promote_text = _ok_text(await _handler(server, "triage_backlog")({
        "id": str(backlog_id),
        "action": "promote",
    }))
    return _extract_task_id(promote_text)


async def _mark_truthgate_pass(task_id: str) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE tasks SET truthgate_verdict = 'truthgate_pass', "
            "truthgate_method = 'manual_record', truth_basis = '[]' "
            "WHERE id = ?",
            (task_id,),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_task(
    *,
    task_id: str = "t-2026-05-03-abc12345",
    title: str = "demo task",
    status: str = "plan",
    trajectory: str = _STANDARD_TRAJECTORY,
    owner: str | None = None,
    spec_path: str | None = None,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, spec_path) "
            "VALUES (?, 'misc', ?, ?, ?, 'coach', ?, ?)",
            (task_id, title, status, owner, trajectory, spec_path),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_backlog(
    *,
    title: str = "demo backlog",
    proposed_by: str = "p1",
    status: str = "pending",
    priority: str = "normal",
    proposed_at: str = "2026-05-14T10:00:00.000Z",
) -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO backlog_tasks "
            "(title, proposed_by, proposed_at, status, priority) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, proposed_by, proposed_at, status, priority),
        )
        await c.commit()
        return int(cur.lastrowid)
    finally:
        await c.close()


async def _plant_role(
    *,
    task_id: str,
    role: str,
    owner: str,
    focus: str | None = None,
) -> None:
    """Direct-SQL plant of an active role row. v2 plants happen via
    coord_approve_stage / coord_create_task; tests that need a
    pre-existing role row use this helper instead of going through
    the deleted v1 assign tools."""
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, "
            " assigned_at, claimed_at, focus) "
            "VALUES (?, ?, '[]', ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?)",
            (task_id, role, owner, focus),
        )
        await c.commit()
    finally:
        await c.close()


# ----------------------------------------------------------------
# coord_create_task / coord_triage_backlog — TruthGate pre-dispatch
# ----------------------------------------------------------------

async def test_create_task_single_name_first_stage_does_not_plant_role_row(
    fresh_db: str,
) -> None:
    await init_db()
    server = _server_for("coach")
    await _create_and_promote(server, {
        "title": "demo",
        "trajectory": [
            {"stage": "execute", "to": "p2"},
            {"stage": "audit_syntax", "to": ["p4", "p5"]},
        ],
    })
    # Promotion enters TruthGate. Even a single-name first `to` is only
    # advisory until Coach approves the post-gate transition.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT role, owner FROM task_role_assignments "
            "ORDER BY id"
        )
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()
    assert rows == []


async def test_post_gate_first_stage_assignment_sets_role_tools(
    fresh_db: str,
) -> None:
    await init_db()
    server = _server_for("coach")
    create_text = _ok_text(await _handler(server, "create_task")({
        "title": "demo",
        "trajectory": [{"stage": "execute", "to": "p2"}],
    }))
    backlog_id = _extract_backlog_id(create_text)
    await _seed_task(
        task_id="t-2026-05-03-11111111",
        status="archive",
        owner="p2",
    )
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET current_task_id = ? WHERE id = 'p2'",
            ("t-2026-05-03-11111111",),
        )
        await c.commit()
    finally:
        await c.close()
    promote_text = _ok_text(await _handler(server, "triage_backlog")({
        "id": str(backlog_id),
        "action": "promote",
    }))
    task_id = _extract_task_id(promote_text)
    await _mark_truthgate_pass(task_id)
    _ok_text(await _handler(server, "approve_stage")({
        "task_id": task_id,
        "next_stage": "execute",
        "assignee": "p2",
        "note": "dispatch after gate",
    }))

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT allowed_tools, current_task_id FROM agents WHERE id = 'p2'"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()

    tools = set(json.loads(row["allowed_tools"]))
    assert "mcp__coord__coord_commit_push" in tools
    assert "mcp__coord__coord_role_complete" in tools
    assert row["current_task_id"] == task_id


async def test_set_agent_role_tools_evicts_codex_client(
    fresh_db: str,
) -> None:
    """_set_agent_role_tools must schedule a Codex client eviction so the
    next Codex spawn picks up the new allowed_tools from the DB, not the
    stale cached subprocess config."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    await init_db()
    evicted: list[str] = []

    async def fake_evict(slot: str) -> None:
        evicted.append(slot)

    with patch("server.runtimes.codex.evict_client", new=fake_evict):
        # Import inside the patch so the lazy import in _set_agent_role_tools
        # picks up the patched version.
        from server.tools import _set_agent_role_tools
        from server.db import configured_conn as _cc

        c = await _cc()
        try:
            await _set_agent_role_tools(c, "p3", "executor")
            await c.commit()
        finally:
            await c.close()

        # Let the ensure_future task run.
        await asyncio.sleep(0)

    assert "p3" in evicted, "evict_client must be called after role change"


async def test_create_task_pool_first_stage_goes_to_backlog(
    fresh_db: str,
) -> None:
    """TruthGate flow: pool first-stage `to` is allowed pre-dispatch."""
    await init_db()
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "create_task")({
        "title": "demo",
        "trajectory": [
            {"stage": "execute", "to": ["p2", "p3"]},
        ],
    }))
    assert "Backlog entry #" in text
    assert "Task is NOT yet on the kanban" in text


async def test_create_task_empty_first_stage_goes_to_backlog(
    fresh_db: str,
) -> None:
    """TruthGate flow: empty first-stage `to` is allowed pre-dispatch."""
    await init_db()
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "create_task")({
        "title": "demo",
        "trajectory": [{"stage": "execute", "to": []}],
    }))
    assert "Backlog entry #" in text
    assert "Task is NOT yet on the kanban" in text


# ----------------------------------------------------------------
# coord_write_task_spec
# ----------------------------------------------------------------

async def test_write_task_spec_coach_writes_spec(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    result = await _handler(server, "write_task_spec")({
        "task_id": "t-2026-05-03-abc12345",
        "body": "## Goal\nDo the thing.\n",
    })
    text = _ok_text(result)
    assert "wrote spec" in text.lower()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT spec_path, spec_written_at FROM tasks "
            "WHERE id = 't-2026-05-03-abc12345'"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["spec_path"] is not None
    assert row["spec_path"].endswith("/spec.md")
    assert row["spec_written_at"] is not None


async def test_write_task_spec_player_without_role_rejected(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("p3")
    err = _err_text(await _handler(server, "write_task_spec")({
        "task_id": "t-2026-05-03-abc12345",
        "body": "x",
    }))
    assert "can't spec task" in err


async def test_write_task_spec_planner_completes_role(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task()
    await _plant_role(
        task_id="t-2026-05-03-abc12345",
        role="planner",
        owner="p3",
    )
    server = _server_for("p3")
    _ok_text(await _handler(server, "write_task_spec")({
        "task_id": "t-2026-05-03-abc12345",
        "body": "## Goal\np3 wrote this.\n",
    }))
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'planner' AND owner = 'p3'",
            ("t-2026-05-03-abc12345",),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["completed_at"] is not None


async def test_write_task_spec_on_behalf_of_records_player_as_author(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task()
    await _plant_role(
        task_id="t-2026-05-03-abc12345",
        role="planner",
        owner="p3",
    )
    coach = _server_for("coach")
    text = _ok_text(await _handler(coach, "write_task_spec")({
        "task_id": "t-2026-05-03-abc12345",
        "body": "## Goal\nrelay\n",
        "on_behalf_of": "p3",
    }))
    assert "p3" in text
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'planner' AND owner = 'p3'",
            ("t-2026-05-03-abc12345",),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["completed_at"] is not None


async def test_write_task_spec_on_behalf_of_player_rejected(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task()
    p4 = _server_for("p4")
    err = _err_text(await _handler(p4, "write_task_spec")({
        "task_id": "t-2026-05-03-abc12345",
        "body": "x",
        "on_behalf_of": "p3",
    }))
    assert "Coach-only" in err


async def test_write_task_spec_on_behalf_of_unassigned_player_rejected(
    fresh_db: str,
) -> None:
    """Coach can only write on behalf of a Player who has an active
    planner role on the task — otherwise the override would credit a
    spec to an unrelated slot."""
    await init_db()
    await _seed_task()
    coach = _server_for("coach")
    err = _err_text(await _handler(coach, "write_task_spec")({
        "task_id": "t-2026-05-03-abc12345",
        "body": "x",
        "on_behalf_of": "p3",
    }))
    assert "no active planner" in err


# ----------------------------------------------------------------
# coord_submit_audit_report (record-only in v2 — no auto-revert)
# ----------------------------------------------------------------

async def test_submit_audit_report_no_assignment_rejected(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="audit_syntax", owner="p2")
    server = _server_for("p4")
    err = _err_text(await _handler(server, "submit_audit_report")({
        "task_id": "t-2026-05-03-abc12345",
        "kind": "syntax",
        "body": "## Summary\nlooks fine\n",
        "verdict": "pass",
    }))
    assert "no active" in err.lower()


async def test_submit_audit_report_pass_completes_role(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="audit_syntax", owner="p2")
    await _plant_role(
        task_id="t-2026-05-03-abc12345",
        role="auditor_syntax",
        owner="p4",
    )
    server = _server_for("p4")
    _ok_text(await _handler(server, "submit_audit_report")({
        "task_id": "t-2026-05-03-abc12345",
        "kind": "syntax",
        "body": "## Summary\nlooks fine\n",
        "verdict": "pass",
    }))
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status FROM tasks WHERE id = ?",
            ("t-2026-05-03-abc12345",),
        )
        t = dict(await cur.fetchone())
        # v2: no auto-advance. Stage stays where it was.
        assert t["status"] == "audit_syntax"
        cur = await c.execute(
            "SELECT completed_at, verdict FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'auditor_syntax'",
            ("t-2026-05-03-abc12345",),
        )
        r = dict(await cur.fetchone())
        assert r["completed_at"]
        assert r["verdict"] == "pass"
    finally:
        await c.close()


async def test_submit_audit_report_fail_does_not_revert(
    fresh_db: str,
) -> None:
    """v2 §7.2: verdict='fail' does NOT auto-revert; the kanban
    subscriber inserts a deviations_log row + emits
    audit_fail_notification, but the task stays in audit_syntax until
    Coach calls coord_approve_stage."""
    await init_db()
    await _seed_task(status="audit_syntax", owner="p2")
    await _plant_role(
        task_id="t-2026-05-03-abc12345",
        role="auditor_syntax",
        owner="p4",
    )
    server = _server_for("p4")
    _ok_text(await _handler(server, "submit_audit_report")({
        "task_id": "t-2026-05-03-abc12345",
        "kind": "syntax",
        "body": "## Summary\nbroken\n",
        "verdict": "fail",
    }))
    # Drain the bus so the subscriber processes the event.
    import asyncio
    await asyncio.sleep(0.05)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status FROM tasks WHERE id = ?",
            ("t-2026-05-03-abc12345",),
        )
        t = dict(await cur.fetchone())
        # Task did NOT auto-revert.
        assert t["status"] == "audit_syntax"
    finally:
        await c.close()


async def test_submit_audit_report_on_behalf_of_records_player_as_auditor(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="audit_syntax", owner="p2")
    await _plant_role(
        task_id="t-2026-05-03-abc12345",
        role="auditor_syntax",
        owner="p4",
    )
    coach = _server_for("coach")
    text = _ok_text(await _handler(coach, "submit_audit_report")({
        "task_id": "t-2026-05-03-abc12345",
        "kind": "syntax",
        "body": "## Summary\nrelay\n",
        "verdict": "pass",
        "on_behalf_of": "p4",
    }))
    assert "p4" in text
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'auditor_syntax' AND owner = 'p4'",
            ("t-2026-05-03-abc12345",),
        )
        r = dict(await cur.fetchone())
    finally:
        await c.close()
    assert r["completed_at"]


async def test_submit_audit_report_coach_without_on_behalf_of_rejected(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="audit_syntax", owner="p2")
    coach = _server_for("coach")
    err = _err_text(await _handler(coach, "submit_audit_report")({
        "task_id": "t-2026-05-03-abc12345",
        "kind": "syntax",
        "body": "x",
        "verdict": "pass",
    }))
    assert "Coach doesn't audit" in err


# ----------------------------------------------------------------
# coord_set_task_blocked + coord_set_task_trajectory
# ----------------------------------------------------------------

async def test_set_task_blocked_owner_can_set(fresh_db: str) -> None:
    await init_db()
    await _seed_task(status="execute", owner="p3")
    server = _server_for("p3")
    _ok_text(await _handler(server, "set_task_blocked")({
        "task_id": "t-2026-05-03-abc12345",
        "blocked": "true",
        "reason": "waiting on review",
    }))
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT blocked, blocked_reason FROM tasks WHERE id = ?",
            ("t-2026-05-03-abc12345",),
        )
        r = dict(await cur.fetchone())
    finally:
        await c.close()
    assert r["blocked"] == 1
    assert r["blocked_reason"] == "waiting on review"


async def test_set_task_blocked_unrelated_player_rejected(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="execute", owner="p3")
    p7 = _server_for("p7")
    err = _err_text(await _handler(p7, "set_task_blocked")({
        "task_id": "t-2026-05-03-abc12345",
        "blocked": "true",
    }))
    assert "owner" in err.lower()


async def test_set_task_trajectory_drops_removed_stage_role_rows(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="plan")
    await _plant_role(
        task_id="t-2026-05-03-abc12345",
        role="auditor_semantics",
        owner="p4",
        focus="check x",
    )
    server = _server_for("coach")
    _ok_text(await _handler(server, "set_task_trajectory")({
        "task_id": "t-2026-05-03-abc12345",
        "trajectory": [
            {"stage": "plan", "to": []},
            {"stage": "execute", "to": []},
            {"stage": "audit_syntax", "to": []},
            {"stage": "ship", "to": []},
        ],
    }))
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'auditor_semantics'",
            ("t-2026-05-03-abc12345",),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()
    assert all(r["completed_at"] for r in rows)


async def test_set_task_trajectory_rejects_removing_already_entered_stage(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="audit_syntax", owner="p2")
    server = _server_for("coach")
    err = _err_text(await _handler(server, "set_task_trajectory")({
        "task_id": "t-2026-05-03-abc12345",
        "trajectory": [
            {"stage": "execute", "to": []},
            {"stage": "ship", "to": []},
        ],
    }))
    assert "already-entered" in err.lower()


async def test_set_task_trajectory_allows_adding_future_stage(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="execute", owner="p2")
    server = _server_for("coach")
    _ok_text(await _handler(server, "set_task_trajectory")({
        "task_id": "t-2026-05-03-abc12345",
        "trajectory": [
            {"stage": "plan", "to": []},
            {"stage": "execute", "to": []},
            {"stage": "audit_syntax", "to": []},
            {"stage": "audit_semantics", "to": ["p5"], "focus": "later"},
            {"stage": "ship", "to": []},
        ],
    }))
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT trajectory FROM tasks WHERE id = ?",
            ("t-2026-05-03-abc12345",),
        )
        traj = json.loads(dict(await cur.fetchone())["trajectory"])
    finally:
        await c.close()
    stages = [s["stage"] for s in traj]
    assert "audit_semantics" in stages


# ----------------------------------------------------------------
# coord_my_assignments + coord_update_task
# ----------------------------------------------------------------

async def test_my_assignments_empty_plate(fresh_db: str) -> None:
    await init_db()
    server = _server_for("p3")
    text = _ok_text(await _handler(server, "my_assignments")({}))
    assert "empty" in text.lower() or "no" in text.lower() or "nothing" in text.lower()


async def test_my_assignments_coach_rejected(fresh_db: str) -> None:
    await init_db()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "my_assignments")({}))
    assert "player" in err.lower() or "coach" in err.lower()


async def _complete_role(
    *,
    task_id: str,
    role: str,
    verdict: str | None = None,
) -> None:
    """Direct-SQL: mark a role row as completed (with optional verdict).
    Simulates the state coord_approve_stage / coord_submit_audit_report
    would leave behind."""
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE task_role_assignments "
            "SET completed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "    verdict = ? "
            "WHERE task_id = ? AND role = ? "
            "  AND completed_at IS NULL AND superseded_by IS NULL",
            (verdict, task_id, role),
        )
        await c.commit()
    finally:
        await c.close()


# ----------------------------------------------------------------
# coord_list_tasks — stage_role field: 4-state transitions for audit
# ----------------------------------------------------------------


async def test_list_tasks_stage_role_four_state_transitions(
    fresh_db: str,
) -> None:
    """stage_role covers four distinct states for audit_syntax:

      (b) auditor:-           — task in audit_syntax, NO role row planted yet
                                (genuine unassigned: both active_owner and
                                 role_done_owner are NULL)
      (a) auditor:p5          — active auditor_syntax role row owned by p5
      (c) complete:p5:pass    — audit role completed with pass verdict
      (d) complete:p5:fail    — audit role completed with fail verdict

    The ordering (b before a) reflects the natural lifecycle: Coach advances
    the task before planting the auditor role, leaving a window where no role
    row exists at all.  That "genuine unassigned" state is distinct from
    `auditor:done` (a completed row with no verdict) — the latter requires
    a row to exist; the former requires its absence.
    """
    async def _list_text(slot: str) -> str:
        server = _server_for(slot)
        return _ok_text(await _handler(server, "list_tasks")({}))

    await init_db()

    # ---- (b) genuine unassigned: task in audit_syntax, no role row ----
    # Represents the state immediately after Coach calls coord_approve_stage
    # to advance to audit_syntax before planting the auditor role.
    TASK_ID = "t-2026-05-13-aabbcc01"
    await _seed_task(task_id=TASK_ID, status="audit_syntax")
    text = await _list_text("coach")
    # active_owner=NULL, role_done_owner=NULL  →  falls to `else` branch
    assert "stage_role=auditor:-" in text, (
        f"(b) expected stage_role=auditor:-; got:\n{text}"
    )

    # ---- (a) assigned: plant active role row for p5 ----
    await _plant_role(task_id=TASK_ID, role="auditor_syntax", owner="p5")
    text = await _list_text("coach")
    assert "stage_role=auditor:p5" in text, (
        f"(a) expected stage_role=auditor:p5; got:\n{text}"
    )

    # ---- (c) complete:pass — fresh task, plant role, submit pass verdict ----
    TASK_PASS = "t-2026-05-13-aabbcc02"
    await _seed_task(task_id=TASK_PASS, status="audit_syntax")
    await _plant_role(task_id=TASK_PASS, role="auditor_syntax", owner="p5")
    server_p5 = _server_for("p5")
    _ok_text(await _handler(server_p5, "submit_audit_report")({
        "task_id": TASK_PASS,
        "kind": "syntax",
        "body": "## Summary\nAll good.\n",
        "verdict": "pass",
    }))
    text = await _list_text("coach")
    assert "stage_role=complete:p5:pass" in text, (
        f"(c) expected stage_role=complete:p5:pass; got:\n{text}"
    )

    # ---- (d) complete:fail — fresh task, plant role, submit fail verdict ----
    TASK_FAIL = "t-2026-05-13-aabbcc03"
    await _seed_task(task_id=TASK_FAIL, status="audit_syntax")
    await _plant_role(task_id=TASK_FAIL, role="auditor_syntax", owner="p5")
    _ok_text(await _handler(server_p5, "submit_audit_report")({
        "task_id": TASK_FAIL,
        "kind": "syntax",
        "body": "## Summary\nBroken.\n",
        "verdict": "fail",
    }))
    text = await _list_text("coach")
    assert "stage_role=complete:p5:fail" in text, (
        f"(d) expected stage_role=complete:p5:fail; got:\n{text}"
    )


async def test_list_tasks_stage_role_verify_transitions(
    fresh_db: str,
) -> None:
    """Verify-stage rows render the verifier role and completed state."""
    async def _list_text(slot: str) -> str:
        server = _server_for(slot)
        return _ok_text(await _handler(server, "list_tasks")({}))

    await init_db()

    task_id = "t-2026-05-13-aabbcc04"
    await _seed_task(task_id=task_id, status="verify")
    text = await _list_text("coach")
    assert "stage_role=verifier:-" in text, (
        f"expected stage_role=verifier:-; got:\n{text}"
    )

    await _plant_role(task_id=task_id, role="verifier", owner="p6")
    text = await _list_text("coach")
    assert "stage_role=verifier:p6" in text, (
        f"expected stage_role=verifier:p6; got:\n{text}"
    )

    await _complete_role(task_id=task_id, role="verifier", verdict="pass")
    text = await _list_text("coach")
    assert "stage_role=verified:p6:pass" in text, (
        f"expected stage_role=verified:p6:pass; got:\n{text}"
    )


async def test_list_tasks_status_verify_matches_verify_stage(
    fresh_db: str,
) -> None:
    """coord_list_tasks(status='verify') is a first-class board filter."""
    await init_db()
    await _seed_task(
        task_id="t-2026-05-13-verify01",
        title="verify me",
        status="verify",
    )
    await _seed_task(
        task_id="t-2026-05-13-ship0001",
        title="ship me",
        status="ship",
    )

    server = _server_for("coach")
    text = _ok_text(await _handler(server, "list_tasks")({
        "status": "verify",
        "include_backlog": True,
    }))

    assert "t-2026-05-13-verify01  kind=task  [verify]" in text
    assert "verify me" in text
    assert "t-2026-05-13-ship0001" not in text
    assert "stage_role=verifier:-" in text


async def test_list_tasks_include_backlog_shows_active_board_not_archive(
    fresh_db: str,
) -> None:
    """Full board starts at pending Backlog and excludes archive by default."""
    await init_db()
    backlog_id = await _seed_backlog(
        title="pending board idea",
        priority="high",
        proposed_at="2026-05-16T10:00:00.000Z",
    )
    await _seed_backlog(
        title="promoted old idea",
        status="promoted",
        proposed_at="2026-05-16T09:00:00.000Z",
    )
    await _seed_task(
        task_id="t-2026-05-13-arch0001",
        title="archived history",
        status="archive",
    )
    await _seed_task(
        task_id="t-2026-05-13-plan0001",
        title="active plan",
        status="plan",
    )
    await _seed_task(
        task_id="t-2026-05-13-verify02",
        title="active verify",
        status="verify",
    )

    server = _server_for("coach")
    text = _ok_text(await _handler(server, "list_tasks")({}))

    assert f"#{backlog_id}  kind=backlog  [pending]" in text
    assert "pri=high  pending board idea" in text
    assert "promoted old idea" not in text
    assert "t-2026-05-13-plan0001  kind=task  [plan]" in text
    assert "t-2026-05-13-verify02  kind=task  [verify]" in text
    assert "t-2026-05-13-arch0001" not in text
    assert "archived history" not in text
    assert text.index(f"#{backlog_id}") < text.index("t-2026-05-13-plan0001")

    no_backlog = _ok_text(await _handler(server, "list_tasks")({
        "include_backlog": False,
    }))
    assert "pending board idea" not in no_backlog
    assert "t-2026-05-13-plan0001  kind=task  [plan]" in no_backlog


async def test_update_task_cancel_stamps_last_stage_change_at(
    fresh_db: str,
) -> None:
    """coord_update_task is the legacy stage/status mutator carried
    forward in v2 (used by HTTP endpoints + a few legacy callers).
    Cancelling a task must stamp last_stage_change_at."""
    await init_db()
    await _seed_task(status="execute", owner="p3")
    server = _server_for("coach")
    _ok_text(await _handler(server, "update_task")({
        "task_id": "t-2026-05-03-abc12345",
        "status": "cancelled",
    }))
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, last_stage_change_at FROM tasks WHERE id = ?",
            ("t-2026-05-03-abc12345",),
        )
        r = dict(await cur.fetchone())
    finally:
        await c.close()
    assert r["status"] == "archive"
    assert r["last_stage_change_at"]

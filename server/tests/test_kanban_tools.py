"""Audit tests for surviving kanban MCP tools after the v2 cutover.

Covers the behaviors of:
  - coord_create_task (first-stage-only role-row planting per v2 §7.1)
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
    assert not result.get("isError"), f"tool returned error: {result}"
    return result["content"][0]["text"]


def _err_text(result: dict[str, Any]) -> str:
    assert result.get("isError"), f"expected error, got {result}"
    return result["content"][0]["text"]


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
# coord_create_task — v2 first-stage-only planting
# ----------------------------------------------------------------

async def test_create_task_single_name_first_stage_plants_role_row(
    fresh_db: str,
) -> None:
    await init_db()
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "create_task")({
        "title": "demo",
        "trajectory": [
            {"stage": "execute", "to": "p2"},
            {"stage": "audit_syntax", "to": ["p4", "p5"]},
        ],
    }))
    assert "Planted executor role" in text
    # Role row exists for the first stage's named owner; NOT for the
    # second stage (pool — auto-plant skipped per v2 §7.1).
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT role, owner FROM task_role_assignments "
            "ORDER BY id"
        )
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()
    assert len(rows) == 1
    assert rows[0]["role"] == "executor"
    assert rows[0]["owner"] == "p2"


async def test_create_task_pool_first_stage_does_not_plant(
    fresh_db: str,
) -> None:
    await init_db()
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "create_task")({
        "title": "demo",
        "trajectory": [
            {"stage": "execute", "to": ["p2", "p3"]},
        ],
    }))
    assert "No role row planted" in text
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) AS n FROM task_role_assignments"
        )
        n = dict(await cur.fetchone())["n"]
    finally:
        await c.close()
    assert n == 0


async def test_create_task_empty_first_stage_does_not_plant(
    fresh_db: str,
) -> None:
    await init_db()
    server = _server_for("coach")
    _ok_text(await _handler(server, "create_task")({
        "title": "demo",
        "trajectory": [{"stage": "execute", "to": []}],
    }))
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) AS n FROM task_role_assignments"
        )
        n = dict(await cur.fetchone())["n"]
    finally:
        await c.close()
    assert n == 0


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

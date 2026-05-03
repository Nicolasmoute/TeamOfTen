"""Audit tests for the new kanban MCP tools.

Covers the happy-path + key error-path branches for:
  - coord_write_task_spec
  - coord_assign_planner
  - coord_assign_auditor (incl. self-review warning)
  - coord_assign_shipper
  - coord_submit_audit_report
  - coord_mark_shipped
  - coord_my_assignments
  - coord_set_task_complexity
  - coord_advance_task_stage
  - coord_set_task_blocked

We exercise the tools through the in-process MCP server (build_coord_server)
the same way coord_mcp dispatches them at runtime — invoking each handler
directly so the tests run fast and don't need the full FastAPI app.
"""

from __future__ import annotations

import json
from typing import Any

from server.db import configured_conn, init_db
from server.tools import build_coord_server


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def _server_for(slot: str) -> Any:
    """Build a coord server with proxy metadata so we can grab handlers."""
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    h = server["_handlers"].get(f"coord_{name}") or server["_handlers"].get(name)
    if h is None:
        raise KeyError(f"no handler for coord_{name}")
    return h


def _ok_text(result: dict[str, Any]) -> str:
    """Extract the text content from a tool result. Raises if it's an error."""
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
    complexity: str = "standard",
    owner: str | None = None,
    spec_path: str | None = None,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, complexity, spec_path) "
            "VALUES (?, 'misc', ?, ?, ?, 'coach', ?, ?)",
            (task_id, title, status, owner, complexity, spec_path),
        )
        await c.commit()
    finally:
        await c.close()


# ------------------------------------------------------------
# coord_write_task_spec
# ------------------------------------------------------------

async def test_write_task_spec_coach_writes_spec(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    result = await _handler(server, "write_task_spec")({
        "task_id": "t-2026-05-03-abc12345",
        "body": "## Goal\nDo the thing.\n",
    })
    text = _ok_text(result)
    assert "wrote spec" in text
    # tasks.spec_path is set.
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


async def test_write_task_spec_player_without_role_rejected(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("p3")
    result = await _handler(server, "write_task_spec")({
        "task_id": "t-2026-05-03-abc12345",
        "body": "x",
    })
    err = _err_text(result)
    assert "can't spec task" in err


async def test_write_task_spec_planner_can_write_then_role_completes(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task()
    # Coach assigns p3 as planner.
    coach = _server_for("coach")
    await _handler(coach, "assign_planner")({
        "task_id": "t-2026-05-03-abc12345",
        "to": "p3",
    })
    # p3 now has an active planner role and can call write_task_spec.
    p3 = _server_for("p3")
    result = await _handler(p3, "write_task_spec")({
        "task_id": "t-2026-05-03-abc12345",
        "body": "## Goal\np3 wrote this.\n",
    })
    _ok_text(result)
    # Planner role row was marked completed.
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


# ------------------------------------------------------------
# coord_assign_planner / auditor / shipper
# ------------------------------------------------------------

async def test_assign_planner_inserts_role_row(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    _ok_text(await _handler(server, "assign_planner")({
        "task_id": "t-2026-05-03-abc12345",
        "to": "p3",
    }))
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT role, owner, eligible_owners FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'planner'",
            ("t-2026-05-03-abc12345",),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["owner"] == "p3"
    # Hard-assign: eligible_owners is empty array.
    assert row["eligible_owners"] == "[]"


async def test_assign_planner_pool_form(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    _ok_text(await _handler(server, "assign_planner")({
        "task_id": "t-2026-05-03-abc12345",
        "to": "p1,p2,p3",
    }))
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT owner, eligible_owners FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'planner'",
            ("t-2026-05-03-abc12345",),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    # Pool: owner is NULL, eligible_owners has the list.
    assert row["owner"] is None
    eligible = json.loads(row["eligible_owners"])
    assert eligible == ["p1", "p2", "p3"]


async def test_assign_planner_player_rejected(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("p3")
    err = _err_text(await _handler(server, "assign_planner")({
        "task_id": "t-2026-05-03-abc12345",
        "to": "p4",
    }))
    assert "Only Coach" in err


async def test_assign_auditor_self_review_warning(fresh_db: str) -> None:
    """Coach assigning the executor as their own auditor emits an
    `audit_self_review_warning` event but doesn't block. Verify by
    draining the bus queue directly — the events table is only
    populated by the lifespan-installed batched-writer task, which
    isn't running in unit tests."""
    from server.events import bus

    await init_db()
    # Task in execute stage with p3 as executor.
    await _seed_task(status="execute", owner="p3")

    queue = bus.subscribe()
    try:
        server = _server_for("coach")
        # Coach assigns p3 as syntax auditor of their own work.
        text = _ok_text(await _handler(server, "assign_auditor")({
            "task_id": "t-2026-05-03-abc12345",
            "to": "p3",
            "kind": "syntax",
        }))
        assert "p3" in text

        # Drain the queue (non-blocking) and collect event types.
        types_seen: list[str] = []
        while not queue.empty():
            ev = queue.get_nowait()
            types_seen.append(ev.get("type", ""))
    finally:
        bus.unsubscribe(queue)

    assert "audit_self_review_warning" in types_seen, (
        f"expected audit_self_review_warning in {types_seen}"
    )


async def test_assign_auditor_kind_validation(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "assign_auditor")({
        "task_id": "t-2026-05-03-abc12345",
        "to": "p4",
        "kind": "garbage",
    }))
    assert "syntax" in err and "semantics" in err


async def test_assign_shipper_basic(fresh_db: str) -> None:
    await init_db()
    await _seed_task(status="ship", owner="p3")
    server = _server_for("coach")
    _ok_text(await _handler(server, "assign_shipper")({
        "task_id": "t-2026-05-03-abc12345",
        "to": "p3",
    }))
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT owner FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'shipper'",
            ("t-2026-05-03-abc12345",),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["owner"] == "p3"


# ------------------------------------------------------------
# coord_submit_audit_report
# ------------------------------------------------------------

async def test_submit_audit_report_no_assignment_rejected(fresh_db: str) -> None:
    """Player can't audit a task they weren't assigned to."""
    await init_db()
    await _seed_task(status="audit_syntax")
    server = _server_for("p4")
    err = _err_text(await _handler(server, "submit_audit_report")({
        "task_id": "t-2026-05-03-abc12345",
        "kind": "syntax",
        "body": "x",
        "verdict": "pass",
    }))
    assert "no active syntax auditor assignment" in err


async def test_submit_audit_report_writes_md_and_updates_row(fresh_db: str) -> None:
    await init_db()
    await _seed_task(status="audit_syntax", owner="p3")
    coach = _server_for("coach")
    await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-03-abc12345",
        "to": "p4",
        "kind": "syntax",
    })
    p4 = _server_for("p4")
    text = _ok_text(await _handler(p4, "submit_audit_report")({
        "task_id": "t-2026-05-03-abc12345",
        "kind": "syntax",
        "body": "## Findings\nLooks good.\n",
        "verdict": "pass",
    }))
    assert "round 1" in text and "pass" in text
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT report_path, verdict, completed_at "
            "FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'auditor_syntax' AND owner = 'p4'",
            ("t-2026-05-03-abc12345",),
        )
        row = dict(await cur.fetchone())
        cur2 = await c.execute(
            "SELECT latest_audit_report_path, latest_audit_kind, "
            "latest_audit_verdict FROM tasks "
            "WHERE id = 't-2026-05-03-abc12345'"
        )
        trow = dict(await cur2.fetchone())
    finally:
        await c.close()
    assert row["report_path"].endswith("audit_1_syntax.md")
    assert row["verdict"] == "pass"
    assert row["completed_at"] is not None
    # tasks.latest_audit_* surface columns are mirrored.
    assert trow["latest_audit_report_path"] == row["report_path"]
    assert trow["latest_audit_kind"] == "syntax"
    assert trow["latest_audit_verdict"] == "pass"


async def test_submit_audit_report_round_increments(fresh_db: str) -> None:
    """A second audit assignment for the same kind produces round 2."""
    await init_db()
    await _seed_task(status="audit_syntax", owner="p3")
    coach = _server_for("coach")

    # Round 1: p4 audits, fails.
    await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-03-abc12345",
        "to": "p4",
        "kind": "syntax",
    })
    p4 = _server_for("p4")
    await _handler(p4, "submit_audit_report")({
        "task_id": "t-2026-05-03-abc12345",
        "kind": "syntax",
        "body": "fails: ...",
        "verdict": "fail",
    })

    # Coach reassigns p4 (or another auditor) for round 2.
    await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-03-abc12345",
        "to": "p4",
        "kind": "syntax",
    })
    text = _ok_text(await _handler(p4, "submit_audit_report")({
        "task_id": "t-2026-05-03-abc12345",
        "kind": "syntax",
        "body": "passes now",
        "verdict": "pass",
    }))
    assert "round 2" in text


# ------------------------------------------------------------
# coord_mark_shipped
# ------------------------------------------------------------

async def test_mark_shipped_validates_assignment(fresh_db: str) -> None:
    await init_db()
    await _seed_task(status="ship", owner="p3")
    p4 = _server_for("p4")
    err = _err_text(await _handler(p4, "mark_shipped")({
        "task_id": "t-2026-05-03-abc12345",
    }))
    assert "no active shipper assignment" in err


async def test_mark_shipped_completes_role(fresh_db: str) -> None:
    await init_db()
    await _seed_task(status="ship", owner="p3")
    coach = _server_for("coach")
    await _handler(coach, "assign_shipper")({
        "task_id": "t-2026-05-03-abc12345",
        "to": "p3",
    })
    p3 = _server_for("p3")
    text = _ok_text(await _handler(p3, "mark_shipped")({
        "task_id": "t-2026-05-03-abc12345",
        "note": "merged in a1b2c3",
    }))
    assert "shipped" in text
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'shipper' AND owner = 'p3'",
            ("t-2026-05-03-abc12345",),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["completed_at"] is not None


# ------------------------------------------------------------
# coord_my_assignments
# ------------------------------------------------------------

async def test_my_assignments_empty_plate(fresh_db: str) -> None:
    await init_db()
    server = _server_for("p3")
    text = _ok_text(await _handler(server, "my_assignments")({}))
    assert "## Executor: (none" in text
    assert "Pending audits:" in text
    assert "Pending ship assignments:" in text
    assert "Available to claim" in text


async def test_my_assignments_full_plate(fresh_db: str) -> None:
    await init_db()
    # p3 is the executor of task A.
    await _seed_task(
        task_id="t-2026-05-03-aaaaaaaa", title="exec-task",
        status="execute", owner="p3", spec_path="x",
    )
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET current_task_id = 't-2026-05-03-aaaaaaaa' "
            "WHERE id = 'p3'"
        )
        await c.commit()
    finally:
        await c.close()

    # p3 is the syntax auditor for task B.
    await _seed_task(
        task_id="t-2026-05-03-bbbbbbbb", title="audit-task",
        status="audit_syntax", owner="p7",
    )
    coach = _server_for("coach")
    await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-03-bbbbbbbb",
        "to": "p3",
        "kind": "syntax",
    })

    # p3 is the shipper for task C.
    await _seed_task(
        task_id="t-2026-05-03-cccccccc", title="ship-task",
        status="ship", owner="p7",
    )
    await _handler(coach, "assign_shipper")({
        "task_id": "t-2026-05-03-cccccccc",
        "to": "p3",
    })

    # p3 is in an eligible pool for task D.
    await _seed_task(
        task_id="t-2026-05-03-dddddddd", title="pool-task",
        status="plan", spec_path="x",
    )
    await _handler(coach, "assign_task")({
        "task_id": "t-2026-05-03-dddddddd",
        "to": "p3,p5",
    })

    server = _server_for("p3")
    text = _ok_text(await _handler(server, "my_assignments")({}))
    assert "t-2026-05-03-aaaaaaaa" in text
    assert "exec-task" in text
    assert "t-2026-05-03-bbbbbbbb" in text
    assert "syntax" in text
    assert "t-2026-05-03-cccccccc" in text
    assert "t-2026-05-03-dddddddd" in text


async def test_my_assignments_coach_rejected(fresh_db: str) -> None:
    await init_db()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "my_assignments")({}))
    assert "Player-only" in err


# ------------------------------------------------------------
# coord_set_task_complexity / advance_task_stage / set_task_blocked
# ------------------------------------------------------------

async def test_set_task_complexity(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "set_task_complexity")({
        "task_id": "t-2026-05-03-abc12345",
        "complexity": "simple",
    }))
    assert "simple" in text
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT complexity FROM tasks WHERE id = 't-2026-05-03-abc12345'"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["complexity"] == "simple"


async def test_set_task_complexity_player_rejected(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("p3")
    err = _err_text(await _handler(server, "set_task_complexity")({
        "task_id": "t-2026-05-03-abc12345",
        "complexity": "simple",
    }))
    assert "Only Coach" in err


async def test_advance_task_stage_validates_transition(fresh_db: str) -> None:
    await init_db()
    await _seed_task()  # plan stage
    server = _server_for("coach")
    # plan → ship is invalid.
    err = _err_text(await _handler(server, "advance_task_stage")({
        "task_id": "t-2026-05-03-abc12345",
        "stage": "ship",
    }))
    assert "invalid transition" in err


async def test_advance_task_stage_archive_clears_owner_current_task(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="execute", owner="p3")
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET current_task_id = 't-2026-05-03-abc12345' "
            "WHERE id = 'p3'"
        )
        await c.commit()
    finally:
        await c.close()
    server = _server_for("coach")
    _ok_text(await _handler(server, "advance_task_stage")({
        "task_id": "t-2026-05-03-abc12345",
        "stage": "archive",
    }))
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT current_task_id FROM agents WHERE id = 'p3'"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["current_task_id"] is None


async def test_set_task_blocked_owner_can_set(fresh_db: str) -> None:
    await init_db()
    await _seed_task(status="execute", owner="p3")
    server = _server_for("p3")
    text = _ok_text(await _handler(server, "set_task_blocked")({
        "task_id": "t-2026-05-03-abc12345",
        "blocked": "true",
        "reason": "waiting on stakeholder",
    }))
    assert "blocked=true" in text
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT blocked, blocked_reason FROM tasks "
            "WHERE id = 't-2026-05-03-abc12345'"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["blocked"] == 1
    assert row["blocked_reason"] == "waiting on stakeholder"


async def test_set_task_blocked_unrelated_player_rejected(fresh_db: str) -> None:
    await init_db()
    await _seed_task(status="execute", owner="p3")
    server = _server_for("p7")
    err = _err_text(await _handler(server, "set_task_blocked")({
        "task_id": "t-2026-05-03-abc12345",
        "blocked": "true",
    }))
    assert "only the task's owner" in err


async def test_set_task_blocked_validates_input(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "set_task_blocked")({
        "task_id": "t-2026-05-03-abc12345",
        "blocked": "maybe",
    }))
    assert "must be" in err

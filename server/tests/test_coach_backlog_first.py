"""Tests for the Coach-created tasks → Backlog first flow.

Coverage:
  - Coach coord_create_task (top-level, no parent_id) → inserts into
    backlog_tasks, NOT tasks.
  - Returns the backlog id with a triage-prompt message.
  - Priority is stored on the backlog entry.
  - trajectory/note/success_criteria are stored on the backlog entry.
  - Player coord_create_task (subtask) still plants directly on kanban.
  - coord_triage_backlog promote picks up stored trajectory from backlog
    when no trajectory arg is passed.
  - coord_triage_backlog promote picks up stored priority from backlog.
"""

from __future__ import annotations

import json

import pytest

from server.db import configured_conn, init_db
from server.tools import build_coord_server


# --------------------------------------------------------------------------- helpers

def _handler(caller_id: str, tool_name: str):
    """Return the raw async handler for a coord tool."""
    server = build_coord_server(caller_id, include_proxy_metadata=True)
    return server["_handlers"][tool_name]


def _text(result: dict) -> str:
    parts = result.get("content") or []
    return " ".join(p.get("text", "") for p in parts if p.get("type") == "text")


def _is_ok(result: dict) -> bool:
    return result.get("isError") is not True


async def _backlog_row(backlog_id: int) -> dict | None:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT * FROM backlog_tasks WHERE id = ?", (backlog_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await c.close()


async def _tasks_count() -> int:
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT COUNT(*) FROM tasks")
        row = await cur.fetchone()
        return row[0]
    finally:
        await c.close()


async def _task_row(task_id: str) -> dict | None:
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await c.close()


def _extract_backlog_id(msg: str) -> int:
    """Parse 'Backlog entry #N ...' from tool message."""
    return int(msg.split("Backlog entry #")[1].split(" ")[0].rstrip(":,").rstrip(")"))


# --------------------------------------------------------------------------- Coach top-level → backlog

async def test_coach_create_task_goes_to_backlog(fresh_db: str) -> None:
    """Coach coord_create_task without parent_id inserts into backlog_tasks,
    NOT into tasks."""
    await init_db()
    tasks_before = await _tasks_count()

    result = await _handler("coach", "coord_create_task")({
        "title": "Implement dark-mode toggle",
        "trajectory": [{"stage": "execute", "to": ["p2"]}],
    })

    assert _is_ok(result), f"expected ok, got {_text(result)}"
    # No new task row
    assert await _tasks_count() == tasks_before
    # Message mentions backlog and triage
    msg = _text(result)
    assert "Backlog entry #" in msg, msg
    assert "coord_triage_backlog" in msg, msg


async def test_coach_create_task_backlog_stores_priority(fresh_db: str) -> None:
    """Priority passed to coord_create_task is stored on the backlog entry."""
    await init_db()

    result = await _handler("coach", "coord_create_task")({
        "title": "Urgent fix",
        "priority": "urgent",
        "trajectory": [{"stage": "execute", "to": ["p3"]}],
    })
    assert _is_ok(result), _text(result)

    bid = _extract_backlog_id(_text(result))
    row = await _backlog_row(bid)
    assert row is not None
    assert row["priority"] == "urgent"


async def test_coach_create_task_backlog_stores_trajectory(fresh_db: str) -> None:
    """trajectory is stored on the backlog entry as trajectory_json."""
    await init_db()
    traj = [{"stage": "plan", "to": "p5"}, {"stage": "execute", "to": "p2"}]

    result = await _handler("coach", "coord_create_task")({
        "title": "Plan + execute task",
        "trajectory": traj,
    })
    assert _is_ok(result), _text(result)
    bid = _extract_backlog_id(_text(result))
    row = await _backlog_row(bid)
    stored = json.loads(row["trajectory_json"])
    assert stored[0]["stage"] == "plan"
    assert stored[1]["stage"] == "execute"


async def test_coach_create_task_backlog_stores_note(fresh_db: str) -> None:
    """note is stored on the backlog entry."""
    await init_db()
    result = await _handler("coach", "coord_create_task")({
        "title": "Task with note",
        "note": "Focus on the retry logic in sync.py",
        "trajectory": [{"stage": "execute", "to": ["p2"]}],
    })
    assert _is_ok(result), _text(result)
    bid = _extract_backlog_id(_text(result))
    row = await _backlog_row(bid)
    assert row["note"] == "Focus on the retry logic in sync.py"


# --------------------------------------------------------------------------- Player subtask → kanban direct

async def test_player_subtask_still_plants_on_kanban(fresh_db: str) -> None:
    """Player coord_create_task with a valid parent_id still creates a task
    row directly on the kanban (backlog-first only applies to Coach top-level)."""
    await init_db()
    # Seed a parent task owned by p2
    parent_id = "t-2026-05-14-parent01"
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) VALUES (?, 'misc', 'Parent', 'execute', "
            "'p2', 'coach', '[]')",
            (parent_id,),
        )
        await c.execute(
            "UPDATE agents SET current_task_id = ? WHERE id = 'p2'",
            (parent_id,),
        )
        await c.commit()
    finally:
        await c.close()

    tasks_before = await _tasks_count()

    result = await _handler("p2", "coord_create_task")({
        "title": "Subtask of parent",
        "parent_id": parent_id,
    })
    assert _is_ok(result), f"expected ok, got {_text(result)}"
    # A task row was created directly on kanban
    assert await _tasks_count() == tasks_before + 1


# --------------------------------------------------------------------------- triage promote uses stored trajectory

async def test_triage_promote_uses_stored_trajectory(fresh_db: str) -> None:
    """coord_triage_backlog promote without a trajectory arg reads it from
    the backlog entry's stored trajectory_json."""
    await init_db()
    traj = [{"stage": "execute", "to": ["p3"]}]
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO backlog_tasks (title, proposed_by, proposed_at, "
            "priority, trajectory_json) VALUES (?, 'coach', "
            "'2026-05-14T00:00:00Z', 'normal', ?)",
            ("Stored-traj task", json.dumps(traj)),
        )
        bid = cur.lastrowid
        await c.commit()
    finally:
        await c.close()

    result = await _handler("coach", "coord_triage_backlog")({
        "id": str(bid),
        "action": "promote",
        # No trajectory arg — should read from stored
    })
    assert _is_ok(result), f"expected ok, got {_text(result)}"
    assert "promoted" in _text(result)


async def test_triage_promote_inherits_priority(fresh_db: str) -> None:
    """Task created via promote inherits priority from the backlog entry."""
    await init_db()
    traj = [{"stage": "execute", "to": ["p4"]}]
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO backlog_tasks (title, proposed_by, proposed_at, "
            "priority, trajectory_json) VALUES (?, 'coach', "
            "'2026-05-14T00:00:00Z', 'high', ?)",
            ("High-priority task", json.dumps(traj)),
        )
        bid = cur.lastrowid
        await c.commit()
    finally:
        await c.close()

    result = await _handler("coach", "coord_triage_backlog")({
        "id": str(bid),
        "action": "promote",
    })
    assert _is_ok(result), _text(result)
    msg = _text(result)
    # "Backlog entry #N promoted → task t-XXXX ..."
    task_id = msg.split("→ task ")[1].split(" ")[0]
    task = await _task_row(task_id)
    assert task is not None
    assert task["priority"] == "high"

"""Regression tests for the kanban stall sweeper (server/idle_poller.py).

Covers the v0.3.4 bug-fix from the production trace where a stuck
audit_semantics task surfaced the executor (p8) as the blocker
instead of the auditor (p3) — Coach received a misleading "no
activity from p8" stall notice and nudged the wrong Player.

The fix: read the active role row for the CURRENT stage and surface
its owner as the stall blocker, falling back to tasks.owner only when
no role row exists.
"""

from __future__ import annotations

import asyncio

from server.db import configured_conn, init_db
from server.events import bus
from server.idle_poller import stall_sweep_once


async def _seed_task_in_stage(
    *,
    task_id: str,
    status: str,
    executor: str,
    last_change: str,
    project_id: str = "misc",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, spec_path, last_stage_change_at) "
            "VALUES (?, ?, 't', ?, ?, 'coach', "
            "'[{\"stage\":\"plan\",\"to\":[]},"
            "{\"stage\":\"execute\",\"to\":[]},"
            "{\"stage\":\"audit_semantics\",\"to\":[]},"
            "{\"stage\":\"ship\",\"to\":[]}]', 'x', ?)",
            (task_id, project_id, status, executor, last_change),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_role_row(
    *, task_id: str, role: str, owner: str, completed_at: str | None = None
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, completed_at) "
            "VALUES (?, ?, '[]', ?, '2026-05-03T10:00:00Z', ?)",
            (task_id, role, owner, completed_at),
        )
        await c.commit()
    finally:
        await c.close()


async def _drain(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


async def test_stall_sweeper_names_current_stage_assignee_not_executor(
    fresh_db: str, monkeypatch,
) -> None:
    """Production trace: task in audit_semantics with executor=p8 and
    auditor=p3. Stall sweeper used to emit owner=p8 (the executor),
    misleading Coach. Now it emits owner=p3 (the active auditor) and
    keeps p8 visible separately as task_executor."""
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_STALL_REALERT_SECONDS", "86400")
    await init_db()
    # Seed a task that's been in audit_semantics for >1 minute.
    # 2026-05-04 was today's date in production traces; pick something
    # comfortably in the past.
    await _seed_task_in_stage(
        task_id="t-2026-05-04-aaaaaaaa",
        status="audit_semantics",
        executor="p8",  # tasks.owner = the executor
        last_change="2026-05-04T00:00:00Z",
    )
    # Active auditor_semantics role row owned by p3 (the actual blocker).
    await _seed_role_row(
        task_id="t-2026-05-04-aaaaaaaa",
        role="auditor_semantics",
        owner="p3",
    )
    captured: list[dict] = []
    queue = bus.subscribe()
    try:
        n = await stall_sweep_once()
        assert n == 1
        await _drain()
        while True:
            try:
                captured.append(queue.get_nowait())
            except Exception:
                break
    finally:
        bus.unsubscribe(queue)
    stall_events = [
        e for e in captured if e.get("type") == "task_stage_stale"
    ]
    assert len(stall_events) == 1
    ev = stall_events[0]
    # The stall blocker is the auditor, not the executor.
    assert ev["owner"] == "p3"
    # Executor stays visible separately for full context.
    assert ev["task_executor"] == "p8"
    assert ev["stage"] == "audit_semantics"


async def test_stall_sweeper_falls_back_to_executor_when_no_role_row(
    fresh_db: str, monkeypatch,
) -> None:
    """If a stage has no active role row at all (broken state — the
    role was completed and the kanban somehow didn't advance), the
    sweeper falls back to tasks.owner so Coach still gets a stall
    event with someone named."""
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_STALL_REALERT_SECONDS", "86400")
    await init_db()
    await _seed_task_in_stage(
        task_id="t-2026-05-04-bbbbbbbb",
        status="audit_semantics",
        executor="p8",
        last_change="2026-05-04T00:00:00Z",
    )
    # Role row exists but is COMPLETED — not "active" — so the
    # current-stage assignee lookup returns None.
    await _seed_role_row(
        task_id="t-2026-05-04-bbbbbbbb",
        role="auditor_semantics",
        owner="p3",
        completed_at="2026-05-04T00:30:00Z",
    )
    captured: list[dict] = []
    queue = bus.subscribe()
    try:
        n = await stall_sweep_once()
        assert n == 1
        await _drain()
        while True:
            try:
                captured.append(queue.get_nowait())
            except Exception:
                break
    finally:
        bus.unsubscribe(queue)
    stall_events = [
        e for e in captured if e.get("type") == "task_stage_stale"
    ]
    assert len(stall_events) == 1
    # Falls back to tasks.owner (the executor) since no live auditor row.
    assert stall_events[0]["owner"] == "p8"


async def test_stall_nudge_for_audit_semantics_names_submit_audit_report(
    fresh_db: str,
) -> None:
    """The stall reminder text for an audit_semantics-stuck task must
    name `coord_submit_audit_report`, not `coord_commit_push`. The
    previous version hardcoded executor tools regardless of stage."""
    from server.idle_poller import _stall_nudge_for_stage
    nudge = _stall_nudge_for_stage(
        task_id="t-2026-05-04-aaaaaaaa",
        stage="audit_semantics",
        age_min=5,
    )
    assert "coord_submit_audit_report" in nudge
    assert "kind='semantics'" in nudge
    assert "coord_commit_push" not in nudge


async def test_stall_nudge_for_ship_names_role_complete(
    fresh_db: str,
) -> None:
    """v2: ship-stage stall nudge points at coord_role_complete (the
    v2 collapsed completion tool); v1's coord_mark_shipped is gone."""
    from server.idle_poller import _stall_nudge_for_stage
    nudge = _stall_nudge_for_stage(
        task_id="t-2026-05-04-aaaaaaaa",
        stage="ship",
        age_min=5,
    )
    assert "coord_role_complete" in nudge
    assert "coord_mark_shipped" not in nudge
    assert "coord_commit_push" not in nudge


async def test_stall_nudge_includes_tool_not_visible_escape(
    fresh_db: str,
) -> None:
    """Production trace: Player wrote audit to disk and stopped because
    they couldn't see the named tool. The nudge must explicitly tell
    them to message Coach in that case."""
    from server.idle_poller import _stall_nudge_for_stage
    nudge = _stall_nudge_for_stage(
        task_id="t-2026-05-04-aaaaaaaa",
        stage="audit_syntax",
        age_min=5,
    )
    assert "not visible" in nudge
    assert "message Coach IMMEDIATELY" in nudge

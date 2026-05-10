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


async def test_stall_nudge_is_v2_fact_only(fresh_db: str) -> None:
    """v2 strip: `_stall_nudge_for_stage` returns the fact line and
    nothing else — no per-stage tool enumeration, no
    tool-not-visible escape clause, no procedural ladder. The
    canonical turn-end reminder is appended by the caller via
    `_with_player_reminder`; the per-stage tool names + Codex
    fallback discipline live in the system prompt (project
    CLAUDE.md template + role baseline). Wakes are facts the
    Player can't derive otherwise; rules are loaded once per turn.
    """
    from server.idle_poller import _stall_nudge_for_stage
    for stage in (
        "plan", "execute", "audit_syntax", "audit_semantics", "ship",
    ):
        nudge = _stall_nudge_for_stage(
            task_id="t-2026-05-04-aaaaaaaa",
            stage=stage,
            age_min=5,
        )
        # Fact MUST be there.
        assert "t-2026-05-04-aaaaaaaa" in nudge
        assert stage in nudge
        assert "5 minutes" in nudge
        # Tool enumeration MUST NOT be there.
        assert "coord_commit_push" not in nudge
        assert "coord_submit_audit_report" not in nudge
        assert "coord_role_complete" not in nudge
        assert "coord_write_task_spec" not in nudge
        # Tool-not-visible escape MUST NOT be there (system prompt).
        assert "not visible" not in nudge
        assert "message Coach IMMEDIATELY" not in nudge


async def test_stall_nudge_unknown_stage_still_returns_fact(
    fresh_db: str,
) -> None:
    """Defensive: any stage value (including unrecognized ones)
    produces a fact-line nudge — no `else: ...` ladder branch
    exists in v2 to add procedural fallback content."""
    from server.idle_poller import _stall_nudge_for_stage
    nudge = _stall_nudge_for_stage(
        task_id="t-1", stage="some_future_stage", age_min=10,
    )
    assert "t-1" in nudge
    assert "some_future_stage" in nudge
    assert "10 minutes" in nudge
    # Even fallback is fact-only — no "Call coord_my_assignments"
    # imperative as in the v1 ladder.
    assert "coord_my_assignments" not in nudge

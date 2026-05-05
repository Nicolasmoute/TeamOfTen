"""v0.3.9 trajectory-completion notification.

When a task hits `archive` via a natural-completion path (the
trajectory played out end-to-end via shipper / executor / auditor
signals), Coach gets:

  1. A `task_completed` event routed `to: 'coach'`.
  2. A wake with an explicit "send a summary of the outcome to the
     user" prompt.

Coach is NOT woken for:

  - `reason='manual'` — Coach forced the archive themselves; they
    already know and decide what to tell the user.
  - `reason='auto_archive_stalled'` — rung 4 of the stall ladder
    fires `human_attention` (Telegram + EnvPane), so the user
    already hears about the failure path. Coach summarizing a
    forced kill is misleading.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import server.agents as agents_mod
from server.db import configured_conn, init_db
from server.events import bus
from server.kanban import _transition


# ---------------------------------------------------------------- helpers

class WakeRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self, slot: str, prompt: str, *,
        bypass_debounce: bool = False, **kw: Any,
    ) -> bool:
        self.calls.append((slot, prompt))
        return True


@pytest.fixture
async def wake_stub(monkeypatch: pytest.MonkeyPatch) -> WakeRecorder:
    rec = WakeRecorder()
    monkeypatch.setattr(agents_mod, "maybe_wake_agent", rec)
    return rec


_FULL_TRAJECTORY = (
    '[{"stage":"plan","to":[]},'
    '{"stage":"execute","to":[]},'
    '{"stage":"audit_syntax","to":[]},'
    '{"stage":"audit_semantics","to":[]},'
    '{"stage":"ship","to":[]}]'
)


async def _seed_task(
    *,
    task_id: str = "t-2026-05-06-00000010",
    title: str = "complete me",
    status: str,
    trajectory: str = _FULL_TRAJECTORY,
    owner: str | None = "p2",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, spec_path) "
            "VALUES (?, 'misc', ?, ?, ?, 'coach', ?, 'x')",
            (task_id, title, status, owner, trajectory),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_role(
    *, task_id: str, role: str, owner: str,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at) "
            "VALUES (?, ?, '[]', ?, '2026-05-06T00:00:00Z')",
            (task_id, role, owner),
        )
        await c.commit()
    finally:
        await c.close()


def _drain(queue: Any) -> list[dict]:
    out: list[dict] = []
    while True:
        try:
            out.append(queue.get_nowait())
        except Exception:
            break
    return out


# ---------------------------------------------------------------- natural completion

async def test_shipped_archive_notifies_coach(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """ship → archive (`reason='shipped'`) is the canonical happy path
    for full-trajectory tasks. Coach must hear about it + be told to
    summarize for the user."""
    await init_db()
    task_id = "t-2026-05-06-00000010"
    await _seed_task(status="ship")
    await _seed_role(task_id=task_id, role="shipper", owner="p4")

    queue = bus.subscribe()
    try:
        await _transition(
            task_id=task_id, new_status="archive", reason="shipped",
            owner="p2", project_id="misc",
        )
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    completed = [e for e in events if e.get("type") == "task_completed"]
    assert len(completed) == 1, events
    ev = completed[0]
    assert ev["task_id"] == task_id
    assert ev["title"] == "complete me"
    assert ev["from_stage"] == "ship"
    assert ev["reason"] == "shipped"
    assert ev["executor"] == "p2"
    assert ev["last_stage_owner"] == "p4"
    assert ev["to"] == "coach"
    # Coach was woken with a summary-the-outcome prompt.
    coach_wakes = [b for s, b in wake_stub.calls if s == "coach"]
    assert coach_wakes, wake_stub.calls
    body = coach_wakes[0]
    assert "Send a summary of the outcome to the user" in body
    assert task_id in body
    assert "broadcast" in body


async def test_simple_execute_only_archive_notifies_coach(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """Execute-only trajectory: when the executor commits, the task
    archives directly via `reason='commit_pushed'`. Coach should
    still get the completion notify (the trajectory wrapped, just at
    a different exit)."""
    await init_db()
    task_id = "t-2026-05-06-00000011"
    traj = '[{"stage":"execute","to":["p2"]}]'
    await _seed_task(
        task_id=task_id, status="execute", trajectory=traj, owner="p2",
    )
    queue = bus.subscribe()
    try:
        await _transition(
            task_id=task_id, new_status="archive",
            reason="commit_pushed", owner="p2", project_id="misc",
        )
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    completed = [e for e in events if e.get("type") == "task_completed"]
    assert len(completed) == 1
    assert completed[0]["reason"] == "commit_pushed"


async def test_audit_pass_terminal_notifies_coach(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """`reason='audit_pass'` when audit is the last trajectory entry
    (rare but valid). Coach gets notified."""
    await init_db()
    task_id = "t-2026-05-06-00000012"
    traj = (
        '[{"stage":"execute","to":[]},'
        '{"stage":"audit_semantics","to":[]}]'
    )
    await _seed_task(
        task_id=task_id, status="audit_semantics",
        trajectory=traj, owner="p2",
    )
    queue = bus.subscribe()
    try:
        await _transition(
            task_id=task_id, new_status="archive", reason="audit_pass",
            owner="p2", project_id="misc",
        )
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    completed = [e for e in events if e.get("type") == "task_completed"]
    assert len(completed) == 1


# ---------------------------------------------------------------- skipped paths

async def test_manual_archive_does_not_notify_coach(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """Coach-forced archive (`coord_advance_task_stage(stage='archive')`)
    fires `_transition` with `reason='manual'`. Coach already knows
    they pressed the button — re-notifying with a summary prompt
    would be confusing noise."""
    await init_db()
    task_id = "t-2026-05-06-00000013"
    await _seed_task(task_id=task_id, status="execute")
    queue = bus.subscribe()
    try:
        await _transition(
            task_id=task_id, new_status="archive", reason="manual",
            owner="p2", project_id="misc",
        )
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    completed = [e for e in events if e.get("type") == "task_completed"]
    assert completed == []
    coach_wakes = [b for s, b in wake_stub.calls if s == "coach"]
    assert coach_wakes == []


async def test_non_archive_transition_does_not_notify_coach(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """Mid-trajectory transitions (execute → audit_syntax,
    plan → execute, etc.) must NOT fire task_completed. The
    notification is reserved for trajectory-end."""
    await init_db()
    task_id = "t-2026-05-06-00000014"
    await _seed_task(task_id=task_id, status="execute")
    queue = bus.subscribe()
    try:
        await _transition(
            task_id=task_id, new_status="audit_syntax",
            reason="commit_pushed", owner="p2", project_id="misc",
        )
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    completed = [e for e in events if e.get("type") == "task_completed"]
    assert completed == []


async def test_event_carries_trajectory_marker(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """The event payload includes a `trajectory_marker` like
    'P → E → AY → AS → S' so Coach's prompt can render the path
    inline. Verifies the abbreviation logic."""
    await init_db()
    task_id = "t-2026-05-06-00000015"
    await _seed_task(task_id=task_id, status="ship")
    queue = bus.subscribe()
    try:
        await _transition(
            task_id=task_id, new_status="archive", reason="shipped",
            owner="p2", project_id="misc",
        )
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    completed = [e for e in events if e.get("type") == "task_completed"]
    assert completed
    marker = completed[0].get("trajectory_marker", "")
    assert marker == "P → E → AY → AS → S"
    # Same marker is rendered into Coach's wake body.
    coach_wakes = [b for s, b in wake_stub.calls if s == "coach"]
    assert any(marker in b for b in coach_wakes), wake_stub.calls

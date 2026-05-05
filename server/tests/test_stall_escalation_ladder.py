"""v0.3.8 stall-escalation ladder tests.

The legacy stall sweeper was a single-fire event that nudged once,
then re-alerted only after 24h. v0.3.8 replaces this with a four-rung
ladder so the system always makes some progress instead of silently
waiting for an assignee whose session may be gone:

  rung 1 (30m): nudge the current-stage assignee
  rung 2 (1h):  notify Coach with explicit 'intervene now' framing
  rung 3 (2h):  auto-reassign to another eligible Player, OR
                fire human_attention if no alternative
  rung 4 (4h):  auto-archive + human_attention

Each rung is per-task idempotent via `tasks.stall_escalation_level`,
which is reset to 0 whenever the task progresses (any code path that
clears `stale_alert_at` also resets the level — see kanban.py /
tools.py / main.py).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import server.agents as agents_mod
from server.db import configured_conn, init_db
from server.events import bus
from server.idle_poller import stall_sweep_once


def _ago(seconds: int) -> str:
    """ISO timestamp `seconds` ago. Tests parameterize stall age by
    seeding `last_stage_change_at` at a known offset from now."""
    return (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat()


# ---------------------------------------------------------------- helpers

class WakeRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self, slot: str, prompt: str, *, bypass_debounce: bool = False,
        **kw: Any,
    ) -> bool:
        self.calls.append((slot, prompt))
        return True


@pytest.fixture
async def wake_stub(monkeypatch: pytest.MonkeyPatch) -> WakeRecorder:
    rec = WakeRecorder()
    monkeypatch.setattr(agents_mod, "maybe_wake_agent", rec)
    return rec


async def _seed_stuck_task(
    *,
    task_id: str = "t-2026-05-06-stallesc",
    status: str = "audit_semantics",
    stage_owner: str = "p3",
    eligible: list[str] | None = None,
    role: str = "auditor_semantics",
    age_seconds: int = 200,
    executor: str = "p8",
) -> None:
    """Seed a task that's been stuck for ages so threshold math fires
    every rung."""
    import json as _json
    eligible = eligible if eligible is not None else [stage_owner]
    last_change = _ago(age_seconds)
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, spec_path, last_stage_change_at) "
            "VALUES (?, 'misc', 'stalled', ?, ?, 'coach', "
            "'[{\"stage\":\"plan\",\"to\":[]},"
            "{\"stage\":\"execute\",\"to\":[]},"
            "{\"stage\":\"audit_syntax\",\"to\":[]},"
            "{\"stage\":\"audit_semantics\",\"to\":[]},"
            "{\"stage\":\"ship\",\"to\":[]}]', 'x', ?)",
            (task_id, status, executor, last_change),
        )
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, claimed_at) "
            "VALUES (?, ?, ?, ?, '2020-01-01T00:00:00Z', "
            "'2020-01-01T00:00:00Z')",
            (task_id, role, _json.dumps(eligible), stage_owner),
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


# ---------------------------------------------------------------- rung 1

async def test_rung_1_nudge_at_threshold(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, wake_stub: WakeRecorder,
) -> None:
    """At rung 1, the sweeper emits `task_stage_stale` (legacy event,
    routed to Coach), wakes the current-stage assignee, and stamps
    `stall_escalation_level = 1`."""
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_COACH_SECONDS", "999999")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS", "999999")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS", "999999")
    await init_db()
    await _seed_stuck_task()
    queue = bus.subscribe()
    try:
        n = await stall_sweep_once()
        assert n == 1
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    stale = [e for e in events if e.get("type") == "task_stage_stale"]
    assert len(stale) == 1, events
    assert stale[0]["owner"] == "p3"
    persisting = [e for e in events if e.get("type") == "task_stall_persisting"]
    assert persisting == []
    # The current-stage assignee was woken with a nudge.
    assert any(slot == "p3" for slot, _ in wake_stub.calls), wake_stub.calls

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT stall_escalation_level FROM tasks WHERE id = ?",
            ("t-2026-05-06-stallesc",),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["stall_escalation_level"] == 1


async def test_rung_1_idempotent_does_not_re_emit(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, wake_stub: WakeRecorder,
) -> None:
    """A second sweep at the same rung does NOT re-emit
    task_stage_stale or wake the assignee again. Without
    idempotence, the sweeper would spam Coach + the Player every
    cycle once the threshold was crossed."""
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_COACH_SECONDS", "999999")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS", "999999")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS", "999999")
    await init_db()
    await _seed_stuck_task()
    await stall_sweep_once()
    wake_stub.calls.clear()
    queue = bus.subscribe()
    try:
        n = await stall_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)
    assert n == 0
    assert not any(e.get("type") == "task_stage_stale" for e in events)
    assert wake_stub.calls == []


# ---------------------------------------------------------------- rung 2

async def test_rung_2_coach_intervention_call(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, wake_stub: WakeRecorder,
) -> None:
    """Past the rung-2 threshold, sweeper walks rungs 1+2: emits
    `task_stage_stale` AND `task_stall_persisting` (latter routed to
    Coach), wakes both the assignee and Coach."""
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_COACH_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS", "999999")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS", "999999")
    await init_db()
    await _seed_stuck_task()
    queue = bus.subscribe()
    try:
        await stall_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    persisting = [e for e in events if e.get("type") == "task_stall_persisting"]
    assert len(persisting) == 1, events
    ev = persisting[0]
    assert ev["next_action"] == "auto_reassign"
    assert ev["to"] == "coach"
    # Coach was woken.
    assert any(slot == "coach" for slot, _ in wake_stub.calls), wake_stub.calls

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT stall_escalation_level FROM tasks WHERE id = ?",
            ("t-2026-05-06-stallesc",),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["stall_escalation_level"] == 2


# ---------------------------------------------------------------- rung 3 reassign

async def test_rung_3_auto_reassigns_to_eligible_alternative(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, wake_stub: WakeRecorder,
) -> None:
    """Past rung 3 with at least one alternative in eligible_owners,
    the sweeper swaps the role row's owner to an unlocked alternative
    and emits `task_stall_auto_reassigned`."""
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_COACH_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS", "999999")
    await init_db()
    await _seed_stuck_task(
        stage_owner="p3", eligible=["p3", "p7"],  # p7 is the alternative
    )
    queue = bus.subscribe()
    try:
        await stall_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    reassigned = [e for e in events if e.get("type") == "task_stall_auto_reassigned"]
    assert len(reassigned) == 1, events
    ev = reassigned[0]
    assert ev["from_owner"] == "p3"
    assert ev["to_owner"] == "p7"
    assert ev["role"] == "auditor_semantics"

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT owner FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'auditor_semantics' "
            "AND completed_at IS NULL AND superseded_by IS NULL",
            ("t-2026-05-06-stallesc",),
        )
        rrow = dict(await cur.fetchone())
        cur = await c.execute(
            "SELECT stall_escalation_level, stale_alert_at "
            "FROM tasks WHERE id = ?",
            ("t-2026-05-06-stallesc",),
        )
        trow = dict(await cur.fetchone())
    finally:
        await c.close()
    assert rrow["owner"] == "p7"
    # AUDIT FIX: rung-3 success resets the stall window (level=0,
    # stale_alert_at=NULL) so the new owner gets a fresh ladder
    # starting at rung 1. Without this, the next sweep would archive
    # the freshly-reassigned task seconds after handoff.
    assert trow["stall_escalation_level"] == 0
    assert trow["stale_alert_at"] is None


async def test_rung_3_no_alternative_fires_human_attention(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, wake_stub: WakeRecorder,
) -> None:
    """When eligible_owners has no alternative, rung 3 doesn't
    reassign — it fires `human_attention` so the human can step in
    before rung 4 archives the task."""
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_COACH_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS", "999999")
    await init_db()
    await _seed_stuck_task(
        stage_owner="p3", eligible=["p3"],  # only the stuck Player
    )
    queue = bus.subscribe()
    try:
        await stall_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    no_alt = [e for e in events if e.get("type") == "task_stall_no_alternative"]
    human = [e for e in events if e.get("type") == "human_attention"]
    reassigned = [e for e in events if e.get("type") == "task_stall_auto_reassigned"]
    assert len(no_alt) == 1, events
    assert len(human) == 1, events
    assert reassigned == []  # no reassign happened


# ---------------------------------------------------------------- rung 4 archive

async def test_rung_4_auto_archives_with_human_attention(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, wake_stub: WakeRecorder,
) -> None:
    """Rung 4 archives the task + fires human_attention. After
    archive the task can no longer stall — its stall_escalation_level
    is reset to 0 and stale_alert_at is cleared."""
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_COACH_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS", "60")
    await init_db()
    # Single eligible Player so rung 3 falls through to human_attention,
    # then rung 4 archives.
    await _seed_stuck_task(stage_owner="p3", eligible=["p3"])
    queue = bus.subscribe()
    try:
        await stall_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    archived = [e for e in events if e.get("type") == "task_stall_auto_archived"]
    stage_changed = [
        e for e in events if e.get("type") == "task_stage_changed"
        and e.get("reason") == "auto_archive_stalled"
    ]
    assert len(archived) == 1, events
    assert len(stage_changed) == 1, events

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, stall_escalation_level, stale_alert_at "
            "FROM tasks WHERE id = ?",
            ("t-2026-05-06-stallesc",),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["status"] == "archive"
    # Reset on archive — a re-opened task starts fresh.
    assert row["stall_escalation_level"] == 0
    assert row["stale_alert_at"] is None


# ---------------------------------------------------------------- progress reset

async def test_rung_3_success_breaks_walk_so_rung_4_does_not_archive(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, wake_stub: WakeRecorder,
) -> None:
    """AUDIT FIX (critical): a task stalled long enough to hit
    target_level=4 must NOT be archived on the same sweep that
    auto-reassigns it. The rung-3 success path resets the stall
    window and the walk loop breaks before rung 4 fires.

    Without this, age >= 4h satisfied target_level=4. The walk
    fires rungs 1, 2, 3, 4 in one sweep — handing the task to a
    new Player and archiving it in the same tick.
    """
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_COACH_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS", "60")
    await init_db()
    # All four thresholds at 60s; age 200s satisfies target_level=4.
    await _seed_stuck_task(stage_owner="p3", eligible=["p3", "p7"])
    queue = bus.subscribe()
    try:
        await stall_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    archived = [e for e in events if e.get("type") == "task_stall_auto_archived"]
    reassigned = [e for e in events if e.get("type") == "task_stall_auto_reassigned"]
    # Reassign DID happen, archive did NOT (the rung-3 break protects).
    assert len(reassigned) == 1, events
    assert archived == [], events
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status FROM tasks WHERE id = ?",
            ("t-2026-05-06-stallesc",),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["status"] == "audit_semantics"  # not 'archive'


async def test_rung_3_skips_busy_alternatives(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, wake_stub: WakeRecorder,
) -> None:
    """AUDIT FIX (high): a Player with `agents.current_task_id` set
    is busy on another task. Auto-reassigning to them would yank
    them off whatever they were doing — same shape as the v0.3.6
    raw-git-bypass problem at a different layer. The alternatives
    filter must skip busy Players."""
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_COACH_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS", "999999")
    await init_db()
    # p7 is the only eligible alternative but they're already on
    # another active (non-archive) task.
    await _seed_stuck_task(stage_owner="p3", eligible=["p3", "p7"])
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, spec_path) "
            "VALUES ('t-2026-05-06-busyword', 'misc', 'busy', "
            "'execute', 'p7', 'coach', "
            "'[{\"stage\":\"execute\",\"to\":[]}]', 'x')"
        )
        await c.execute(
            "UPDATE agents SET current_task_id = 't-2026-05-06-busyword' "
            "WHERE id = 'p7'"
        )
        await c.commit()
    finally:
        await c.close()

    queue = bus.subscribe()
    try:
        await stall_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    reassigned = [e for e in events if e.get("type") == "task_stall_auto_reassigned"]
    no_alt = [e for e in events if e.get("type") == "task_stall_no_alternative"]
    # No reassign happened (p7 was busy). human_attention via no_alt.
    assert reassigned == [], events
    assert len(no_alt) == 1, events


async def test_rung_2_wake_text_uses_threshold_minutes_not_divided_age(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, wake_stub: WakeRecorder,
) -> None:
    """AUDIT FIX (low): rung 2's Coach-wake body refers to the
    rung-1 NUDGE INTERVAL (e.g. '5-min nudge' when threshold=300s),
    not (age_min // 60). The old wording said 'didn't move on the
    1-min nudge' at age=65min — divided-age math, nonsense."""
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "300")  # 5 min
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_COACH_SECONDS", "300")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS", "999999")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS", "999999")
    await init_db()
    # age 4000s satisfies rung-1 + rung-2 thresholds; rung-1 of
    # 5min becomes the wording target.
    await _seed_stuck_task(age_seconds=4000)
    await stall_sweep_once()
    coach_wakes = [b for s, b in wake_stub.calls if s == "coach"]
    assert coach_wakes, wake_stub.calls
    body = coach_wakes[0]
    assert "5-min nudge" in body
    # Sanity: should NOT contain divided-age artefacts. With age=4000
    # (~66min), the old code emitted '1-min nudge' (66 // 60).
    assert "1-min nudge" not in body
    assert "66-min nudge" not in body


async def test_rung_3_supersede_race_aborts_to_no_alt(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """AUDIT-2 FIX: if the role row got completed/superseded between
    the main loop's read and rung 3's UPDATE, the UPDATE's WHERE
    clause (now `AND completed_at IS NULL AND superseded_by IS NULL`)
    matches 0 rows. The function detects this via rowcount and
    aborts with `reason='role_row_changed'`. We exercise the
    function directly with a stale `role_row_id` to simulate the
    race (the main-loop SELECT would have returned this id but the
    row is now inactive)."""
    from server.idle_poller import _fire_rung_3
    await init_db()
    await _seed_stuck_task(stage_owner="p3", eligible=["p3", "p7"])
    # Read the active role row's id, then mark it completed —
    # simulating "Coach assigned a new auditor between read and
    # write". The sweep's main loop already snapshotted role_row_id;
    # the UPDATE will fire against an inactive row.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'auditor_semantics'",
            ("t-2026-05-06-stallesc",),
        )
        row_id = dict(await cur.fetchone())["id"]
        await c.execute(
            "UPDATE task_role_assignments "
            "SET completed_at = '2026-05-06T01:00:00Z' WHERE id = ?",
            (row_id,),
        )
        await c.commit()
    finally:
        await c.close()
    queue = bus.subscribe()
    try:
        ok = await _fire_rung_3(
            task={"id": "t-2026-05-06-stallesc", "owner": "p8"},
            stage="audit_semantics",
            role="auditor_semantics",
            role_row_id=row_id,
            eligible=["p3", "p7"],
            stall_owner="p3",
            now_iso="2026-05-06T02:00:00Z",
        )
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    assert ok is False
    reassigned = [e for e in events if e.get("type") == "task_stall_auto_reassigned"]
    no_alt = [e for e in events if e.get("type") == "task_stall_no_alternative"]
    assert reassigned == [], events
    assert len(no_alt) == 1
    assert no_alt[0]["reason"] == "role_row_changed"


async def test_rung_4_archive_marks_role_rows_complete(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, wake_stub: WakeRecorder,
) -> None:
    """AUDIT-2 FIX: rung 4 archive must close every active role row
    on the task. Without this, queries that look at 'any active
    row' without a status filter still see them — orphaned rows
    that confuse downstream logic."""
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_COACH_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS", "60")
    await init_db()
    # Single eligible — rung 3 falls through to no_alt; rung 4 archives.
    await _seed_stuck_task(stage_owner="p3", eligible=["p3"])
    await stall_sweep_once()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) AS n FROM task_role_assignments "
            "WHERE task_id = ? "
            "AND completed_at IS NULL AND superseded_by IS NULL",
            ("t-2026-05-06-stallesc",),
        )
        active_rows = dict(await cur.fetchone())["n"]
    finally:
        await c.close()
    assert active_rows == 0


async def test_rung_4_archive_fires_stand_down_to_assignee(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, wake_stub: WakeRecorder,
) -> None:
    """AUDIT-2 FIX: a Player who's actively working when rung 4
    fires must get a stand-down wake. Otherwise they keep working
    on a task the kanban no longer tracks."""
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_COACH_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS", "60")
    await init_db()
    await _seed_stuck_task(stage_owner="p3", eligible=["p3"])
    queue = bus.subscribe()
    try:
        await stall_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    stand_down = [e for e in events if e.get("type") == "task_role_stand_down"]
    assert len(stand_down) == 1, events
    assert "p3" in stand_down[0].get("displaced", [])
    # The stand-down wake itself goes to the displaced Player.
    p3_wakes = [
        b for s, b in wake_stub.calls
        if s == "p3" and "STOP work" in b
    ]
    assert p3_wakes, wake_stub.calls


async def test_stage_progression_resets_escalation_level(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kanban subscriber clears stale_alert_at + stall_escalation_level
    on every status change. Verified end-to-end via coord_advance_task_stage."""
    from server.tools import build_coord_server
    monkeypatch.setenv("HARNESS_KANBAN_STALL_SECONDS", "60")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_COACH_SECONDS", "999999")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS", "999999")
    monkeypatch.setenv("HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS", "999999")
    await init_db()
    await _seed_stuck_task()
    await stall_sweep_once()  # → level 1

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT stall_escalation_level FROM tasks WHERE id = ?",
            ("t-2026-05-06-stallesc",),
        )
        before = dict(await cur.fetchone())
    finally:
        await c.close()
    assert before["stall_escalation_level"] == 1

    # Coach forces a stage move via coord_advance_task_stage.
    coach = build_coord_server("coach", include_proxy_metadata=True)
    handler = (
        coach["_handlers"].get("coord_advance_task_stage")
        or coach["_handlers"].get("advance_task_stage")
    )
    result = await handler({
        "task_id": "t-2026-05-06-stallesc",
        "stage": "ship",
    })
    assert not result.get("isError"), result

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT stall_escalation_level, stale_alert_at "
            "FROM tasks WHERE id = ?",
            ("t-2026-05-06-stallesc",),
        )
        after = dict(await cur.fetchone())
    finally:
        await c.close()
    # Reset on progress.
    assert after["stall_escalation_level"] == 0
    assert after["stale_alert_at"] is None

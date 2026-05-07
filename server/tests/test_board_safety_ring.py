"""v2 §10.4 board safety ring tests.

Sibling tick pass that catches "Coach went to sleep on the entire
kanban" — no `task_stage_changed` event in N minutes despite ≥1
non-archive task on the board.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from server.db import configured_conn, init_db
from server.events import bus
from server.idle_poller import board_safety_ring_once


def _drain(queue: Any) -> list[dict]:
    out: list[dict] = []
    while True:
        try:
            out.append(queue.get_nowait())
        except Exception:
            break
    return out


async def _seed_task(
    *, task_id: str, status: str = "execute", project_id: str = "misc",
    created_minutes_ago: int = 0,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) VALUES (?, ?, 'demo', ?, 'p3', "
            "'coach', '[{\"stage\":\"execute\",\"to\":[]}]')",
            (task_id, project_id, status),
        )
        if created_minutes_ago > 0:
            old = _iso_minutes_ago(created_minutes_ago)
            await c.execute(
                "UPDATE tasks SET created_at = ?, "
                "last_stage_change_at = ? WHERE id = ?",
                (old, old, task_id),
            )
        await c.commit()
    finally:
        await c.close()


async def _seed_project_event(
    *, project_id: str, type_: str, ts: str,
    actor: str = "coach", task_id: str | None = None,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO project_events "
            "(project_id, ts, actor, type, task_id, payload_json) "
            "VALUES (?, ?, ?, ?, ?, '{}')",
            (project_id, ts, actor, type_, task_id),
        )
        await c.commit()
    finally:
        await c.close()


async def _set_team_config(key: str, value: str) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT OR REPLACE INTO team_config (key, value) VALUES (?, ?)",
            (key, value),
        )
        await c.commit()
    finally:
        await c.close()


async def _get_team_config(key: str) -> str | None:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
        return dict(row)["value"] if row else None
    finally:
        await c.close()


def _iso_minutes_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=n)).isoformat()


# ---------------------------------------------------------------- happy path

async def test_fires_when_board_has_active_task_and_no_recent_stage_change(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_SECONDS", "1800")
    await init_db()
    await _seed_task(
        task_id="t-2026-05-07-aaaaaaa1", created_minutes_ago=120,
    )
    # last stage change 45 min ago
    await _seed_project_event(
        project_id="misc", type_="task_stage_changed",
        ts=_iso_minutes_ago(45),
    )

    q = bus.subscribe()
    try:
        emitted = await board_safety_ring_once()
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    assert emitted == 1
    types = [e.get("type") for e in events]
    assert "kanban_board_stalled" in types
    stall = next(e for e in events if e.get("type") == "kanban_board_stalled")
    assert stall["project_id"] == "misc"
    assert stall["active_task_count"] == 1
    assert stall["age_seconds"] >= 1800
    assert stall["to"] == "coach"


async def test_does_not_fire_when_recent_stage_change(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_SECONDS", "1800")
    await init_db()
    await _seed_task(task_id="t-2026-05-07-aaaaaaa2")
    await _seed_project_event(
        project_id="misc", type_="task_stage_changed",
        ts=_iso_minutes_ago(5),
    )

    q = bus.subscribe()
    try:
        emitted = await board_safety_ring_once()
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    assert emitted == 0
    assert all(e.get("type") != "kanban_board_stalled" for e in events)


async def test_does_not_fire_on_empty_board(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero non-archive tasks → silent skip."""
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_SECONDS", "1800")
    await init_db()
    # no tasks seeded
    q = bus.subscribe()
    try:
        emitted = await board_safety_ring_once()
        events = _drain(q)
    finally:
        bus.unsubscribe(q)
    assert emitted == 0
    assert all(e.get("type") != "kanban_board_stalled" for e in events)


async def test_archive_only_board_does_not_fire(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_SECONDS", "1800")
    await init_db()
    await _seed_task(task_id="t-2026-05-07-aaaaaaa3", status="archive")
    q = bus.subscribe()
    try:
        emitted = await board_safety_ring_once()
    finally:
        bus.unsubscribe(q)
    assert emitted == 0


async def test_only_task_stage_changed_resets_timer(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recent `commit_pushed` row must not silence the ring — only
    a `task_stage_changed` event counts."""
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_SECONDS", "1800")
    await init_db()
    await _seed_task(
        task_id="t-2026-05-07-aaaaaaa4", created_minutes_ago=120,
    )
    # stage change long ago
    await _seed_project_event(
        project_id="misc", type_="task_stage_changed",
        ts=_iso_minutes_ago(60),
    )
    # commit 1 min ago — should NOT reset
    await _seed_project_event(
        project_id="misc", type_="commit_pushed",
        ts=_iso_minutes_ago(1), actor="p3",
    )

    q = bus.subscribe()
    try:
        emitted = await board_safety_ring_once()
    finally:
        bus.unsubscribe(q)
    assert emitted == 1


async def test_old_task_with_no_stage_change_event_treated_as_stale(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Project with an old non-archive task and no `task_stage_changed`
    event should fire the ring (board has work but no movement). Uses
    a backdated `created_at` so the ring sees the task as old."""
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_SECONDS", "1800")
    await init_db()
    await _seed_task(task_id="t-2026-05-07-aaaaaaa5")
    # Backdate the task so the fallback signals (created_at,
    # last_stage_change_at) are both stale.
    old_ts = _iso_minutes_ago(120)
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE tasks SET created_at = ?, last_stage_change_at = ? "
            "WHERE id = ?",
            (old_ts, old_ts, "t-2026-05-07-aaaaaaa5"),
        )
        await c.commit()
    finally:
        await c.close()
    q = bus.subscribe()
    try:
        emitted = await board_safety_ring_once()
    finally:
        bus.unsubscribe(q)
    assert emitted == 1


async def test_freshly_created_task_does_not_fire(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task created seconds ago — even with no `task_stage_changed`
    event yet — should NOT fire the ring. The board just moved (a
    task was created)."""
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_SECONDS", "1800")
    await init_db()
    await _seed_task(task_id="t-2026-05-07-aaaaaaab")
    q = bus.subscribe()
    try:
        emitted = await board_safety_ring_once()
    finally:
        bus.unsubscribe(q)
    assert emitted == 0


# ---------------------------------------------------------------- re-arm

async def test_realert_cooldown_suppresses_repeat_alert(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_SECONDS", "1800")
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_REALERT_SECONDS", "3600")
    await init_db()
    await _seed_task(
        task_id="t-2026-05-07-aaaaaaa6", created_minutes_ago=120,
    )
    await _seed_project_event(
        project_id="misc", type_="task_stage_changed",
        ts=_iso_minutes_ago(60),
    )
    # First fire stamps the alert.
    q = bus.subscribe()
    try:
        first = await board_safety_ring_once()
        # Second tick a moment later should be silenced.
        second = await board_safety_ring_once()
    finally:
        bus.unsubscribe(q)
    assert first == 1
    assert second == 0


async def test_realert_after_cooldown_elapsed(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_SECONDS", "1800")
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_REALERT_SECONDS", "3600")
    await init_db()
    await _seed_task(
        task_id="t-2026-05-07-aaaaaaa7", created_minutes_ago=180,
    )
    await _seed_project_event(
        project_id="misc", type_="task_stage_changed",
        ts=_iso_minutes_ago(120),
    )
    # Pre-stamp an old alert (2h ago) so cooldown has elapsed.
    old_alert = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat()
    await _set_team_config(
        "kanban_board_silence_alerted_at:misc", old_alert,
    )

    q = bus.subscribe()
    try:
        emitted = await board_safety_ring_once()
    finally:
        bus.unsubscribe(q)
    assert emitted == 1


async def test_recent_stage_change_clears_alert_stamp(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the board moves again, the alert stamp must clear so a
    future stagnation can fire fresh."""
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_SECONDS", "1800")
    await init_db()
    await _seed_task(task_id="t-2026-05-07-aaaaaaa8")
    await _set_team_config(
        "kanban_board_silence_alerted_at:misc",
        _iso_minutes_ago(5),
    )
    # Recent stage change.
    await _seed_project_event(
        project_id="misc", type_="task_stage_changed",
        ts=_iso_minutes_ago(2),
    )
    emitted = await board_safety_ring_once()
    assert emitted == 0
    stamp = await _get_team_config(
        "kanban_board_silence_alerted_at:misc",
    )
    assert stamp is None


# ---------------------------------------------------------------- env knobs

async def test_disabled_via_env(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SAFETY_ENABLED", "false")
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_SECONDS", "1")
    await init_db()
    await _seed_task(task_id="t-2026-05-07-aaaaaaa9")
    emitted = await board_safety_ring_once()
    assert emitted == 0


async def test_custom_silence_threshold(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tighter threshold — even a recent stage change is now stale."""
    monkeypatch.setenv("HARNESS_KANBAN_BOARD_SILENCE_SECONDS", "60")
    await init_db()
    await _seed_task(
        task_id="t-2026-05-07-aaaaaaaa", created_minutes_ago=10,
    )
    await _seed_project_event(
        project_id="misc", type_="task_stage_changed",
        ts=_iso_minutes_ago(5),
    )
    emitted = await board_safety_ring_once()
    assert emitted == 1

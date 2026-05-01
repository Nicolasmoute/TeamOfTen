"""Phase 4 tests — `server.compass.presence`.

Verifies:
  - Empty messages + no heartbeat → not reachable
  - Recent human message → reachable
  - Recent heartbeat → reachable
  - Wrong-project message → still not reachable
  - Old heartbeat (outside window) → not reachable
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.compass import config, presence


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.mark.asyncio
async def test_no_signal_means_not_reachable(fresh_db: str) -> None:
    from server.db import init_db
    await init_db()
    assert await presence.human_reachable("misc") is False


@pytest.mark.asyncio
async def test_recent_human_message_unlocks(fresh_db: str) -> None:
    from server.db import configured_conn, init_db
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO messages (project_id, from_id, to_id, subject, body) "
            "VALUES (?, 'human', 'coach', 's', 'hi')",
            ("misc",),
        )
        await c.commit()
    finally:
        await c.close()
    assert await presence.human_reachable("misc") is True


@pytest.mark.asyncio
async def test_message_for_other_project_does_not_unlock(fresh_db: str) -> None:
    from server.db import configured_conn, init_db
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            ("alpha", "Alpha"),
        )
        await c.execute(
            "INSERT INTO messages (project_id, from_id, to_id, subject, body) "
            "VALUES (?, 'human', 'coach', 's', 'hi')",
            ("alpha",),
        )
        await c.commit()
    finally:
        await c.close()
    assert await presence.human_reachable("misc") is False
    assert await presence.human_reachable("alpha") is True


@pytest.mark.asyncio
async def test_heartbeat_unlocks(fresh_db: str) -> None:
    from server.db import init_db
    await init_db()
    await presence.update_heartbeat("misc")
    assert await presence.human_reachable("misc") is True


@pytest.mark.asyncio
async def test_old_heartbeat_does_not_unlock(fresh_db: str) -> None:
    from server.db import init_db, configured_conn
    await init_db()
    # Plant a stale heartbeat (older than the window).
    old = datetime.now(timezone.utc) - timedelta(hours=config.HUMAN_PRESENCE_WINDOW_HOURS + 5)
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO team_config (key, value) VALUES (?, ?)",
            (config.heartbeat_key("misc"), _iso(old)),
        )
        await c.commit()
    finally:
        await c.close()
    assert await presence.human_reachable("misc") is False


@pytest.mark.asyncio
async def test_send_reminder_publishes_event(fresh_db: str) -> None:
    from server.db import init_db
    from server.events import bus
    await init_db()
    queue = bus.subscribe()
    try:
        await presence.send_reminder("misc")
        # Drain queue with a short wait.
        msg = await queue.get()
        assert msg["type"] == "compass_reminder"
        assert msg["project_id"] == "misc"
    finally:
        bus.unsubscribe(queue)

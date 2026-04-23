"""Event bus round-trip tests.

Covers:
- publish() fans out to live subscribers via asyncio.Queue
- publish() also writes to the events table (fire-and-forget)
- subscribers added AFTER a publish don't receive it (backlog replay
  is intentionally off — that's the DB's job)
"""

from __future__ import annotations

import asyncio

from server.db import configured_conn, init_db
from server.events import EventBus, _persist


async def test_subscriber_receives_published_events(fresh_db: str) -> None:
    await init_db()
    bus = EventBus()
    q = bus.subscribe()
    await bus.publish({"ts": "2026-04-23T10:00:00Z", "agent_id": "coach", "type": "test"})
    ev = await asyncio.wait_for(q.get(), timeout=1.0)
    assert ev["type"] == "test"
    assert ev["agent_id"] == "coach"


async def test_persist_inserts_into_events_table(fresh_db: str) -> None:
    await init_db()
    await _persist(
        {
            "ts": "2026-04-23T10:05:00Z",
            "agent_id": "p3",
            "type": "memory_updated",
            "topic": "notes",
            "version": 2,
        }
    )
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT agent_id, type, payload FROM events WHERE type = 'memory_updated'"
        )
        rows = await cur.fetchall()
    finally:
        await c.close()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["agent_id"] == "p3"
    # payload is a JSON blob; inspecting shape would require json.loads,
    # which tools.py already does — here we just assert it's non-empty.
    assert row["payload"] and "memory_updated" in row["payload"]


async def test_late_subscriber_does_not_get_backlog(fresh_db: str) -> None:
    # Per EventBus docstring: backlog replay was removed to avoid
    # duplicates when UI merges history + live. Pin that behavior.
    await init_db()
    bus = EventBus()
    await bus.publish({"ts": "t", "agent_id": "coach", "type": "before"})
    q = bus.subscribe()
    assert q.empty()

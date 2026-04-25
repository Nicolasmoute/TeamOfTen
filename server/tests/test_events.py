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


async def test_batched_writer_persists_burst(fresh_db: str) -> None:
    """A burst of publish() calls should land in the events table after
    the writer flushes — even when nobody manually awaits _persist."""
    from server.events import (
        EventBus,
        start_event_writer,
        stop_event_writer,
    )

    await init_db()
    await start_event_writer()
    try:
        bus = EventBus()
        # 30 events, well below BATCH_SIZE (50) — should flush in one batch.
        for i in range(30):
            await bus.publish(
                {
                    "ts": f"2026-04-25T10:00:{i:02d}Z",
                    "agent_id": "p1",
                    "type": "burst_test",
                    "i": i,
                }
            )
        # Stop the writer — drains pending events and flushes deterministically.
    finally:
        await stop_event_writer()

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) AS n FROM events WHERE type = 'burst_test'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row)["n"] == 30


async def test_batched_writer_handles_above_batch_size(fresh_db: str) -> None:
    """More events than BATCH_SIZE should still all land — multiple
    flushes get exercised."""
    from server.events import (
        BATCH_SIZE,
        EventBus,
        start_event_writer,
        stop_event_writer,
    )

    await init_db()
    await start_event_writer()
    try:
        bus = EventBus()
        n = BATCH_SIZE * 3 + 7  # forces multi-batch
        for i in range(n):
            await bus.publish(
                {
                    "ts": f"2026-04-25T11:{i // 60:02d}:{i % 60:02d}Z",
                    "agent_id": "p2",
                    "type": "multi_batch_test",
                }
            )
    finally:
        await stop_event_writer()

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) AS n FROM events WHERE type = 'multi_batch_test'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row)["n"] == n

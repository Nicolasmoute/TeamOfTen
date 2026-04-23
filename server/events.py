from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import deque
from typing import Any

from server.db import configured_conn

BACKLOG_SIZE = 500
QUEUE_SIZE = 500

# Event types that fire many-per-second during a streaming turn (one per
# token-level delta from the model). Skipping them from the SQLite mirror
# + in-memory backlog keeps the events table from exploding and keeps
# reload-history cheap. They still fan out to live WS subscribers because
# that's how the UI shows a "typing" effect.
_TRANSIENT_EVENT_TYPES: frozenset[str] = frozenset({
    "text_delta",
    "thinking_delta",
})

logger = logging.getLogger("harness.events")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


async def _persist(event: dict[str, Any]) -> None:
    """Fire-and-forget insert into the events table.

    Swallow errors so a DB hiccup never takes down publish. The in-memory
    fan-out to WebSocket subscribers already happened before we got here.
    """
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO events (ts, agent_id, type, payload) "
                "VALUES (?, ?, ?, ?)",
                (
                    event.get("ts", ""),
                    event.get("agent_id", "system"),
                    event.get("type", "unknown"),
                    json.dumps(event),
                ),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("event persist failed: %r", event.get("type"))


class EventBus:
    """In-process fan-out event bus with SQLite mirror.

    Publishers call `publish()`; subscribers hold an asyncio.Queue. New
    subscribers receive the recent backlog so a page refresh still sees
    context. Every event is also persisted to SQLite for durable replay
    across reloads / redeploys.
    """

    def __init__(self, backlog: int = BACKLOG_SIZE) -> None:
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._backlog: deque[dict[str, Any]] = deque(maxlen=backlog)

    async def publish(self, event: dict[str, Any]) -> None:
        transient = event.get("type") in _TRANSIENT_EVENT_TYPES
        if not transient:
            self._backlog.append(event)
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
        if not transient:
            # Fire-and-forget DB mirror — never blocks publish
            asyncio.create_task(_persist(event))

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Subscribe to live events only.

        Historical context comes from GET /api/events (DB-backed) since v2a;
        replaying the in-memory backlog here caused duplicate events in the
        UI when a pane combined /api/events history with WS events.
        """
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.discard(q)

    @property
    def subscriber_count(self) -> int:
        """Number of currently-subscribed WebSocket clients."""
        return len(self._queues)


bus = EventBus()

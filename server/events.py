from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

BACKLOG_SIZE = 500
QUEUE_SIZE = 500


class EventBus:
    """In-process fan-out event bus.

    Publishers call `publish()`; subscribers hold an asyncio.Queue and read
    from it. New subscribers receive the recent backlog on subscribe so a
    page refresh still sees context.

    M1: in-memory only. M3 will add durable mirror to SQLite/kDrive.
    """

    def __init__(self, backlog: int = BACKLOG_SIZE) -> None:
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._backlog: deque[dict[str, Any]] = deque(maxlen=backlog)

    async def publish(self, event: dict[str, Any]) -> None:
        self._backlog.append(event)
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop for that slow subscriber rather than block everyone
                pass

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_SIZE)
        for event in self._backlog:
            q.put_nowait(event)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.discard(q)


bus = EventBus()

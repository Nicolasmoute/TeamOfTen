from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections import deque
from typing import Any

from server.db import MISC_PROJECT_ID, configured_conn, resolve_active_project

BACKLOG_SIZE = 500
QUEUE_SIZE = 500

# Batched-writer tunables. The writer task pulls events off `_write_queue`,
# drains up to `BATCH_SIZE` per flush, and forces a flush every
# `BATCH_INTERVAL` seconds even if the batch isn't full. With per-event
# writes (the prior model) every non-transient event opened/closed a
# connection and committed solo, which on a multi-agent storm could be
# 10+ writes/sec, each contending for the same write handle in DELETE
# journal mode. Batching collapses that into one INSERT-many + one
# commit per ~100 ms window.
BATCH_SIZE = int(os.environ.get("HARNESS_EVENTS_BATCH_SIZE", "50"))
BATCH_INTERVAL = float(os.environ.get("HARNESS_EVENTS_BATCH_INTERVAL", "0.1"))
WRITE_QUEUE_SIZE = int(os.environ.get("HARNESS_EVENTS_WRITE_QUEUE_SIZE", "10000"))

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


def _row_for(event: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Project a publish() event dict into the events-table column tuple.
    Centralized so the single-event _persist() and the batched writer
    use exactly the same shape — no risk of column-order drift.

    `project_id` is taken off the event when present, otherwise falls
    back to MISC_PROJECT_ID. The events row schema requires NOT NULL
    project_id; callers that already know the active project should
    set it explicitly via _emit so the event lands in the right
    project tree.
    """
    return (
        event.get("ts", ""),
        event.get("agent_id", "system"),
        event.get("project_id") or MISC_PROJECT_ID,
        event.get("type", "unknown"),
        json.dumps(event),
    )


async def _persist(event: dict[str, Any]) -> None:
    """Single-event fallback persist. Used directly by tests and as a
    safety net when the batched writer's queue is full.

    Swallow errors so a DB hiccup never takes down publish. The in-memory
    fan-out to WebSocket subscribers already happened before we got here.
    """
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO events (ts, agent_id, project_id, type, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                _row_for(event),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("event persist failed: %r", event.get("type"))


# ----------------------------------------------------------------------
# Batched event writer
# ----------------------------------------------------------------------
#
# Single long-lived async task that drains a queue, batches into one
# executemany + commit per flush. Started by main.py's lifespan during
# startup and stopped on shutdown so the final batch flushes cleanly.
# Tests that use EventBus directly don't need it — _persist still works
# stand-alone.

_write_queue: asyncio.Queue[dict[str, Any]] | None = None
_writer_task: asyncio.Task[None] | None = None
_writer_stopping = False


async def _writer_loop() -> None:
    """Drain `_write_queue`, batch, persist via executemany.

    Owns its own connection for the loop's lifetime (single-writer
    discipline per CLAUDE.md). On any exception we log + close the
    connection and let the outer `while` reopen it next iteration —
    a transient DB error must not kill the writer permanently.
    """
    global _write_queue
    assert _write_queue is not None
    queue = _write_queue
    while not _writer_stopping:
        # Block until at least one event is available, then opportunistically
        # drain up to BATCH_SIZE-1 more without waiting. This keeps idle
        # CPU at zero (we only wake when there's work) and gives the
        # batch a chance to grow during a publish burst.
        try:
            first = await queue.get()
        except asyncio.CancelledError:
            return
        batch: list[dict[str, Any]] = [first]
        deadline = asyncio.get_running_loop().time() + BATCH_INTERVAL
        while len(batch) < BATCH_SIZE:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            except asyncio.CancelledError:
                # Mid-flush cancellation — finish what we have, then exit.
                break
            batch.append(ev)
        await _flush_batch(batch)


async def _flush_batch(batch: list[dict[str, Any]]) -> None:
    if not batch:
        return
    try:
        c = await configured_conn()
        try:
            await c.executemany(
                "INSERT INTO events (ts, agent_id, project_id, type, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                [_row_for(ev) for ev in batch],
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        # Don't lose the batch silently — log each affected type so an
        # operator can correlate with the application log. Falling back
        # to single-event _persist would loop the same failure, so just
        # drop and move on. The WS already saw these.
        types = sorted({ev.get("type", "?") for ev in batch})
        logger.exception(
            "batched event persist failed (size=%d, types=%s)", len(batch), types
        )


async def start_event_writer() -> None:
    """Bring up the writer task. Idempotent — safe to call twice."""
    global _write_queue, _writer_task, _writer_stopping
    _writer_stopping = False
    if _write_queue is None:
        _write_queue = asyncio.Queue(maxsize=WRITE_QUEUE_SIZE)
    if _writer_task is None or _writer_task.done():
        _writer_task = asyncio.get_running_loop().create_task(
            _writer_loop(), name="harness.events.writer"
        )


async def stop_event_writer(timeout: float = 2.0) -> None:
    """Drain pending events and stop the writer task.

    Called from main.py lifespan teardown so a redeploy doesn't drop
    the last few seconds of audit history. We set the stopping flag,
    drain whatever's already queued, cancel the writer, then drain
    once more to catch anything published during the cancel window
    (publish() fires from many call sites and one might land between
    our first drain and the writer terminating).
    """
    global _writer_task, _writer_stopping
    _writer_stopping = True

    async def _drain_and_flush() -> None:
        if _write_queue is None:
            return
        drained: list[dict[str, Any]] = []
        try:
            while True:
                drained.append(_write_queue.get_nowait())
        except asyncio.QueueEmpty:
            pass
        if drained:
            await _flush_batch(drained)

    await _drain_and_flush()
    if _writer_task is not None and not _writer_task.done():
        _writer_task.cancel()
        try:
            await asyncio.wait_for(_writer_task, timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    # Second drain: covers events that publish()'d after the first
    # drain but before the writer fully stopped. Tiny window but it's
    # the difference between "shutdown is clean" and "occasionally
    # loses the last event of a turn".
    await _drain_and_flush()
    _writer_task = None


def _enqueue_for_persist(event: dict[str, Any]) -> None:
    """Best-effort enqueue. Falls through to a single-event task if the
    queue is missing (writer never started) or full (rare under normal
    load — sized 10k by default). Either way the event is durable.
    """
    if _write_queue is None:
        asyncio.create_task(_persist(event))
        return
    try:
        _write_queue.put_nowait(event)
    except asyncio.QueueFull:
        # Backpressure escape hatch: spawn a one-off task. If we ever
        # hit this in production it's a sign the queue size or batch
        # interval needs tuning, so log it.
        logger.warning(
            "event write queue full (size=%d); falling back to single insert",
            WRITE_QUEUE_SIZE,
        )
        asyncio.create_task(_persist(event))


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
        # Stamp project_id if the caller didn't. Tolerated DB error →
        # falls back to misc, matching resolve_active_project's contract.
        if "project_id" not in event:
            try:
                event["project_id"] = await resolve_active_project()
            except Exception:
                event["project_id"] = MISC_PROJECT_ID
        transient = event.get("type") in _TRANSIENT_EVENT_TYPES
        if not transient:
            self._backlog.append(event)
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Backpressure: a subscriber's queue is full because
                # the WS read loop fell behind (slow network, browser
                # tab throttled, …). Default behaviour was to drop the
                # NEW event silently, which left that client stuck
                # showing stale state and growing more out of sync
                # with each storm. Drop the OLDEST queued event for
                # this client instead — they'll see a gap rather than
                # a long lag, and on the next heartbeat the UI's
                # since_id polling will catch them up from the DB.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass
        if not transient:
            # Hand off to the batched writer task — never blocks publish.
            # Falls back to a single-insert task if the writer isn't
            # running (e.g. during tests that build an EventBus directly).
            _enqueue_for_persist(event)

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

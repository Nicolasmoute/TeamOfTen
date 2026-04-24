"""Pending-interaction registry for agent-initiated requests that pause
a turn until the host responds.

Covers two kinds today:
  - "question" — AskUserQuestion: structured multiple-choice ask.
  - "plan"     — ExitPlanMode: request to leave plan mode, approve/reject/
                  approve-with-comments.

Both go through the same Future/watcher/timeout primitive — only the
payload shape + the answer shape differ. `kind` is carried on every
entry and surfaced on all events so the UI can render the right form.

Resolution paths:
  - host POSTs /api/questions/<id>/answer or /api/plans/<id>/decision, OR
  - Coach calls coord_answer_question / coord_answer_plan, OR
  - the deadline passes without resolution — the watcher task rejects
    the Future with InteractionRejected, can_use_tool catches and
    translates to PermissionResultDeny.

Deadline design: each entry carries a mutable `deadline_ts` (epoch
seconds). A per-entry watcher task sleeps until the deadline, checks
the Future, rejects on timeout. Extension just updates deadline_ts
and reschedules the watcher — no cancellation dance for callers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("harness.interactions")


def _timeout_seconds() -> int:
    """Default deadline window when a new entry is registered. The
    default dropped from 3600 → 1800 after we observed operators
    missing 5-minute-env-overridden windows during plan review —
    1800 is a forgiving middle ground between 'don't strand the
    agent' and 'give the human time to think'. Override via
    HARNESS_INTERACTION_TIMEOUT_SECONDS (new name) or legacy
    HARNESS_QUESTION_TIMEOUT_SECONDS."""
    for key in ("HARNESS_INTERACTION_TIMEOUT_SECONDS", "HARNESS_QUESTION_TIMEOUT_SECONDS"):
        raw = os.environ.get(key)
        if raw is None:
            continue
        try:
            return max(30, min(int(raw), 86_400))
        except ValueError:
            continue
    return 1800


@dataclass
class PendingInteraction:
    correlation_id: str
    agent_id: str
    # "question" | "plan". Both share the Future-based wait; payloads
    # and answer shapes diverge.
    kind: str
    # Question: {"questions": [...]}
    # Plan:     {"plan": "<markdown>"}
    payload: dict[str, Any]
    # "human" | "coach" — where the host should surface the interaction.
    route: str
    created_at: str
    # Mutable epoch-seconds deadline. extend() updates this + restarts
    # the watcher; resolve() and reject() ignore it.
    deadline_ts: float
    future: asyncio.Future = field(repr=False)


_pending: dict[str, PendingInteraction] = {}
_timeout_tasks: dict[str, asyncio.Task[Any]] = {}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def list_pending(kind: str | None = None) -> list[dict[str, Any]]:
    """Metadata for UI / debug; Future excluded. Filter by kind when
    given (the /api/questions/pending and /api/plans/pending endpoints
    each pass their own)."""
    out = []
    for p in _pending.values():
        if kind is not None and p.kind != kind:
            continue
        out.append({
            "correlation_id": p.correlation_id,
            "agent_id": p.agent_id,
            "kind": p.kind,
            "route": p.route,
            "created_at": p.created_at,
            "deadline_at": _iso(p.deadline_ts),
            "payload": p.payload,
        })
    return out


async def _watch_timeout(entry: PendingInteraction) -> None:
    """Background task that rejects entry.future once deadline_ts
    passes, unless cancelled first (by extend() rescheduling, or by
    forget()/resolve() cleanup). Polls in short chunks so extension
    is observed without a fresh wake-up dance."""
    while True:
        if entry.future.done():
            return
        now = time.time()
        remaining = entry.deadline_ts - now
        if remaining <= 0:
            if not entry.future.done():
                entry.future.set_exception(
                    InteractionRejected(
                        f"deadline reached at {_iso(entry.deadline_ts)}"
                    )
                )
            return
        try:
            # Chunked sleep — short enough that an extend() which
            # restarts us doesn't leave the old wake pending for long;
            # long enough to avoid busy-looping when deadline is far.
            await asyncio.sleep(min(remaining, 10.0))
        except asyncio.CancelledError:
            # Normal path on extend() / forget(). Just exit.
            return


def _schedule_watcher(entry: PendingInteraction) -> None:
    """Start (or restart) the timeout watcher for an entry. Called
    from register() on creation and from extend() after bumping the
    deadline."""
    existing = _timeout_tasks.pop(entry.correlation_id, None)
    if existing is not None and not existing.done():
        existing.cancel()
    _timeout_tasks[entry.correlation_id] = asyncio.create_task(
        _watch_timeout(entry)
    )


def register(
    agent_id: str,
    kind: str,
    payload: dict[str, Any],
    route: str,
) -> PendingInteraction:
    """Create a pending entry, return the record (Future included).
    Caller awaits the Future and removes the entry in its finally."""
    if kind not in ("question", "plan"):
        raise ValueError(f"unknown interaction kind {kind!r}")
    correlation_id = uuid.uuid4().hex
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    deadline_ts = time.time() + _timeout_seconds()
    entry = PendingInteraction(
        correlation_id=correlation_id,
        agent_id=agent_id,
        kind=kind,
        payload=payload,
        route=route,
        created_at=datetime.now(timezone.utc).isoformat(),
        deadline_ts=deadline_ts,
        future=future,
    )
    _pending[correlation_id] = entry
    _schedule_watcher(entry)
    logger.info(
        "interactions: registered %s %s for %s via %s (deadline %s)",
        kind, correlation_id, agent_id, route, _iso(deadline_ts),
    )
    return entry


def forget(correlation_id: str) -> None:
    """Remove an entry without resolving. Cancels the watcher so it
    doesn't wake up and mutate a future that's already cleaned up."""
    _pending.pop(correlation_id, None)
    task = _timeout_tasks.pop(correlation_id, None)
    if task is not None and not task.done():
        task.cancel()


def get(correlation_id: str) -> PendingInteraction | None:
    """Lookup by id. Returns None when missing."""
    return _pending.get(correlation_id)


def resolve(correlation_id: str, result: Any) -> bool:
    """Resolve the Future with the supplied result (shape depends on
    kind: question → answers dict, plan → decision dict). Returns True
    when the id existed and wasn't already resolved, False otherwise."""
    entry = _pending.get(correlation_id)
    if entry is None:
        return False
    if entry.future.done():
        return False
    entry.future.set_result(result)
    # Cancel the watcher so it doesn't fire a spurious timeout on an
    # already-resolved Future.
    task = _timeout_tasks.pop(correlation_id, None)
    if task is not None and not task.done():
        task.cancel()
    logger.info("interactions: resolved %s (%s)", correlation_id, entry.kind)
    return True


def reject(correlation_id: str, reason: str) -> bool:
    """Resolve the Future with an error so the callback returns a deny."""
    entry = _pending.get(correlation_id)
    if entry is None or entry.future.done():
        return False
    entry.future.set_exception(InteractionRejected(reason))
    task = _timeout_tasks.pop(correlation_id, None)
    if task is not None and not task.done():
        task.cancel()
    return True


def extend(correlation_id: str, seconds: int) -> dict[str, Any] | None:
    """Push the deadline out by `seconds` (relative to NOW, not to the
    current deadline — an extension from a nearly-expired entry gets a
    full fresh window). Returns the new deadline_at metadata on
    success, None when the entry is missing / already resolved. The
    watcher is rescheduled so the new deadline is observed immediately."""
    entry = _pending.get(correlation_id)
    if entry is None or entry.future.done():
        return None
    seconds = max(30, min(int(seconds), 86_400))
    entry.deadline_ts = time.time() + seconds
    _schedule_watcher(entry)
    logger.info(
        "interactions: extended %s by %ds → %s",
        correlation_id, seconds, _iso(entry.deadline_ts),
    )
    return {
        "correlation_id": correlation_id,
        "deadline_at": _iso(entry.deadline_ts),
        "seconds_from_now": seconds,
    }


class InteractionRejected(Exception):
    """Raised into the waiter on cancel/timeout/internal failure. The
    can_use_tool callback catches this and translates to a
    PermissionResultDeny with the reason as the message."""


# Backward-compat alias so any callers that imported the old name keep
# working through the rename + kind-field addition. New code should
# catch InteractionRejected.
QuestionRejected = InteractionRejected


async def wait_for(entry: PendingInteraction) -> Any:
    """Await the entry's Future. Timeout is handled by the watcher
    task spawned in register() — when the deadline passes, the
    watcher sets an InteractionRejected exception on the Future,
    which bubbles out here."""
    return await entry.future

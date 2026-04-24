"""Pending-interaction registry for agent-initiated requests that pause
a turn until the host responds.

Covers two kinds today:
  - "question" — AskUserQuestion: structured multiple-choice ask.
  - "plan"     — ExitPlanMode: request to leave plan mode, approve/reject/
                  approve-with-comments.

Both go through the same Future/resolve/timeout primitive — only the
payload shape + the answer shape differ. `kind` is carried on every
entry and surfaced on all events so the UI can render the right form.

Resolution paths:
  - host POSTs /api/questions/<id>/answer or /api/plans/<id>/decision, OR
  - Coach calls coord_answer_question / coord_answer_plan, OR
  - HARNESS_INTERACTION_TIMEOUT_SECONDS elapses and the callback
    returns a deny.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("harness.interactions")


def _timeout_seconds() -> int:
    # HARNESS_QUESTION_TIMEOUT_SECONDS is preserved as an alias so
    # existing deployments don't need an env rename. Either var works;
    # the new name (INTERACTION) takes precedence when both are set.
    for key in ("HARNESS_INTERACTION_TIMEOUT_SECONDS", "HARNESS_QUESTION_TIMEOUT_SECONDS"):
        raw = os.environ.get(key)
        if raw is None:
            continue
        try:
            return max(30, min(int(raw), 86_400))
        except ValueError:
            continue
    return 3600


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
    future: asyncio.Future = field(repr=False)


_pending: dict[str, PendingInteraction] = {}


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
            "payload": p.payload,
        })
    return out


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
    entry = PendingInteraction(
        correlation_id=correlation_id,
        agent_id=agent_id,
        kind=kind,
        payload=payload,
        route=route,
        created_at=datetime.now(timezone.utc).isoformat(),
        future=future,
    )
    _pending[correlation_id] = entry
    logger.info(
        "interactions: registered %s %s for %s via %s",
        kind, correlation_id, agent_id, route,
    )
    return entry


def forget(correlation_id: str) -> None:
    """Remove an entry without resolving. Used in the caller's finally
    after await so a resolved/cancelled Future doesn't leak."""
    _pending.pop(correlation_id, None)


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
    logger.info("interactions: resolved %s (%s)", correlation_id, entry.kind)
    return True


def reject(correlation_id: str, reason: str) -> bool:
    """Resolve the Future with an error so the callback returns a deny."""
    entry = _pending.get(correlation_id)
    if entry is None or entry.future.done():
        return False
    entry.future.set_exception(InteractionRejected(reason))
    return True


class InteractionRejected(Exception):
    """Raised into the waiter on cancel/timeout/internal failure. The
    can_use_tool callback catches this and translates to a
    PermissionResultDeny with the reason as the message."""


# Backward-compat alias so any callers that imported the old name keep
# working through the rename + kind-field addition. New code should
# catch InteractionRejected.
QuestionRejected = InteractionRejected


async def wait_for(entry: PendingInteraction) -> Any:
    """Await the entry's Future with HARNESS_INTERACTION_TIMEOUT_SECONDS
    timeout. On timeout raises InteractionRejected("timeout after Ns")."""
    timeout = _timeout_seconds()
    try:
        return await asyncio.wait_for(entry.future, timeout=timeout)
    except asyncio.TimeoutError:
        forget(entry.correlation_id)
        raise InteractionRejected(f"timeout after {timeout}s")

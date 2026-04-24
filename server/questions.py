"""Pending-question registry.

When an agent calls AskUserQuestion, our can_use_tool callback pauses
the turn and parks an asyncio.Future in this registry. The Future
resolves when:
  - the human submits the form (POST /api/questions/<id>/answer), OR
  - Coach calls coord_answer_question with matching correlation_id, OR
  - HARNESS_QUESTION_TIMEOUT_SECONDS elapses (registry cancels and the
    callback returns a deny so the agent sees an error rather than
    hanging forever).

The correlation_id is generated fresh per pending question and carried
through every routing path (UI form, Coach's inbox message, event
bus) so the right Future gets resolved.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("harness.questions")


def _timeout_seconds() -> int:
    try:
        n = int(os.environ.get("HARNESS_QUESTION_TIMEOUT_SECONDS", "3600"))
    except ValueError:
        return 3600
    return max(30, min(n, 86_400))


@dataclass
class PendingQuestion:
    correlation_id: str
    agent_id: str
    questions: list[dict[str, Any]]
    route: str  # "human" | "coach"
    created_at: str
    future: asyncio.Future = field(repr=False)


_pending: dict[str, PendingQuestion] = {}


def list_pending() -> list[dict[str, Any]]:
    """Metadata for UI / debug; Future excluded."""
    return [
        {
            "correlation_id": p.correlation_id,
            "agent_id": p.agent_id,
            "route": p.route,
            "created_at": p.created_at,
            "questions": p.questions,
        }
        for p in _pending.values()
    ]


def register(
    agent_id: str,
    questions: list[dict[str, Any]],
    route: str,
) -> PendingQuestion:
    """Create a pending entry, return the record (Future included).
    Caller awaits the Future and removes the entry in its finally."""
    correlation_id = uuid.uuid4().hex
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    entry = PendingQuestion(
        correlation_id=correlation_id,
        agent_id=agent_id,
        questions=questions,
        route=route,
        created_at=datetime.now(timezone.utc).isoformat(),
        future=future,
    )
    _pending[correlation_id] = entry
    logger.info(
        "questions: registered %s for %s via %s (%d questions)",
        correlation_id, agent_id, route, len(questions),
    )
    return entry


def forget(correlation_id: str) -> None:
    """Remove an entry without resolving. Used in the caller's finally
    after await so a resolved/cancelled Future doesn't leak."""
    _pending.pop(correlation_id, None)


def resolve(correlation_id: str, answers: dict[str, str]) -> bool:
    """Resolve the Future with the supplied answers. Returns True when
    the id existed and wasn't already resolved, False otherwise (bad
    id, double-answer, or already timed out)."""
    entry = _pending.get(correlation_id)
    if entry is None:
        return False
    if entry.future.done():
        return False
    entry.future.set_result(answers)
    logger.info(
        "questions: resolved %s (%d answer keys)",
        correlation_id, len(answers),
    )
    return True


def reject(correlation_id: str, reason: str) -> bool:
    """Resolve the Future with a deny so the callback returns an error
    to the agent rather than hanging. Used by explicit cancellation
    (e.g. agent cancelled mid-wait) and internal failure paths."""
    entry = _pending.get(correlation_id)
    if entry is None or entry.future.done():
        return False
    entry.future.set_exception(QuestionRejected(reason))
    return True


class QuestionRejected(Exception):
    """Raised into the waiter when a question is cancelled or times
    out. The can_use_tool callback catches this and returns a
    PermissionResultDeny with the reason as the message."""


async def wait_for(entry: PendingQuestion) -> dict[str, str]:
    """Await the entry's Future with HARNESS_QUESTION_TIMEOUT_SECONDS
    timeout. On timeout raises QuestionRejected("timeout after Ns").
    Always pairs with forget() in a finally — the entry is otherwise
    removed by the resolver path."""
    timeout = _timeout_seconds()
    try:
        return await asyncio.wait_for(entry.future, timeout=timeout)
    except asyncio.TimeoutError:
        # wait_for cancels the underlying future; our reject() would
        # now fail silently. Clean up here so the map doesn't leak.
        forget(entry.correlation_id)
        raise QuestionRejected(f"timeout after {timeout}s")

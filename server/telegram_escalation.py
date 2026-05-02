"""Telegram escalation watcher — pings the user's phone when a
pending-attention item goes unanswered for too long.

Three event types open an "unattended" timer:

  - `pending_question` (route='human')   — AskUserQuestion routed at
    the human (Coach asked).
  - `pending_plan` (route='human')       — ExitPlanMode plan approval
    routed at the human.
  - `file_write_proposal_created`        — Coach proposed a truth or
    project-CLAUDE.md edit; human reviews in the EnvPane.

These already surface in the EnvPane "needs attention" section but
have no out-of-band signal today. With the watcher enabled, each
pending item gets a per-(kind, key) `asyncio.Task` that:

  - Sleeps `HARNESS_TELEGRAM_ESCALATION_SECONDS` (default 300) when
    at least one WebSocket client is connected — the user is
    plausibly watching, so give them time to react.
  - Sleeps a small grace window (5s) when no WS client is connected —
    the harness is unattended; ping the phone almost immediately.

When a matching resolution event arrives, the timer is cancelled and
no Telegram message is sent. The bridge config is resolved at fire
time via `server.telegram.send_outbound`, so a Clear in the UI is
respected without restarting the watcher.

`human_attention` (from `coord_request_human`) is **not** routed
through this module — the bridge's outbound loop already pushes
those to Telegram immediately, since the agent has explicitly
declared "I can't proceed". Adding a delay there would only slow
the signal the user wants fastest.

Configuration:
  HARNESS_TELEGRAM_ESCALATION_SECONDS  — delay (seconds) when web
                                         is active. 0 disables the
                                         watcher entirely.
  HARNESS_TELEGRAM_ESCALATION_GRACE    — delay when web is inactive.
                                         Default 5s; lets a quick
                                         page-reload still catch the
                                         item before the phone pings.

Restart behavior: this watcher is purely in-memory. File-write
proposals open before a server restart will keep their `status='pending'`
row in the DB but won't re-arm a timer on next boot. Acceptable for
v1 — the EnvPane still surfaces them on reconnect; the auto-pop
behaviour ensures the human sees them. Replay-on-boot is a possible
v2 extension.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from server.events import bus
from server.telegram import send_outbound

logger = logging.getLogger("harness.telegram_escalation")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


def _delay_seconds() -> int:
    """Read the active delay from env. 0 disables the watcher.

    Re-read on every event so a deploy that bumps the env takes
    effect without restart (matches the Compass watcher's pattern).
    """
    raw = os.environ.get("HARNESS_TELEGRAM_ESCALATION_SECONDS", "300").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 300


def _grace_seconds() -> int:
    """Tiny delay applied when no WS client is connected. Gives a
    quick page-reload a chance to catch the item before the phone
    pings, without making the unattended path feel sluggish."""
    raw = os.environ.get("HARNESS_TELEGRAM_ESCALATION_GRACE", "5").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 5


# Cap on inline body content per item — Telegram's per-message cap is
# 4096 chars; we cap each section so multiple items in a burst don't
# blow the limit. The bridge's `_split_chunks` handles any spillover.
_BODY_PREVIEW = 1500


# ---------------------------------------------------------------- state

# Live timers per (kind, key). Cancelled on the matching resolution
# event. Read + written from the consumer task only, so no lock needed.
_pending: dict[tuple[str, str], asyncio.Task[None]] = {}

# Module-level lifecycle handles, mirroring telegram + audit_watcher.
_current_task: asyncio.Task[None] | None = None
_stopping = False


def is_running() -> bool:
    """True iff the watcher background task is alive."""
    return _current_task is not None and not _current_task.done()


def pending_count() -> int:
    """Number of in-flight escalation timers. For tests + diagnostics."""
    return sum(1 for t in _pending.values() if not t.done())


# ---------------------------------------------------------------- event keys


def _key_for_pending(ev: dict[str, Any]) -> tuple[str, str] | None:
    """Map a pending-attention event to a (kind, key) handle. Returns
    None when the event isn't one we escalate (or is a route='coach'
    pending_question/plan that Coach handles itself).
    """
    etype = ev.get("type") or ""
    if etype == "pending_question" and ev.get("route") == "human":
        cid = ev.get("correlation_id")
        if cid:
            return ("question", str(cid))
    elif etype == "pending_plan" and ev.get("route") == "human":
        cid = ev.get("correlation_id")
        if cid:
            return ("plan", str(cid))
    elif etype == "file_write_proposal_created":
        pid = ev.get("proposal_id")
        if pid is not None:
            return ("proposal", str(pid))
    return None


def _key_for_resolution(ev: dict[str, Any]) -> tuple[str, str] | None:
    """Map a resolution event to the (kind, key) it cancels."""
    etype = ev.get("type") or ""
    if etype in ("question_answered", "question_cancelled"):
        cid = ev.get("correlation_id")
        if cid:
            return ("question", str(cid))
    elif etype in ("plan_decided", "plan_cancelled"):
        cid = ev.get("correlation_id")
        if cid:
            return ("plan", str(cid))
    elif etype in (
        "file_write_proposal_approved",
        "file_write_proposal_denied",
        "file_write_proposal_cancelled",
        "file_write_proposal_superseded",
    ):
        pid = ev.get("proposal_id")
        if pid is not None:
            return ("proposal", str(pid))
    return None


# ---------------------------------------------------------------- context


async def _agent_label(agent_id: str) -> str:
    """Return a one-line "<slot> (<name>, <role>)" label for the
    pending item's source agent. Falls back to the slot id when the
    project-roles row is missing or system.

    Best-effort — never raises. Wraps `_get_agent_identity` (which
    already swallows errors) and adds a defensive try/except so a
    transient DB hiccup at fire time doesn't drop the whole
    escalation.
    """
    if not agent_id or agent_id == "system":
        return agent_id or "system"
    try:
        from server.agents import _get_agent_identity  # noqa: PLC0415
        ident = await _get_agent_identity(agent_id)
    except Exception:
        return agent_id
    name = (ident.get("name") or "").strip()
    role = (ident.get("role") or "").strip()
    parts = [agent_id]
    if name and role:
        parts.append(f"({name}, {role})")
    elif name:
        parts.append(f"({name})")
    elif role:
        parts.append(f"({role})")
    return " ".join(parts)


def _truncate(text: str, n: int = _BODY_PREVIEW) -> str:
    """Trim long bodies with an ellipsis marker so the user knows
    they're seeing a preview, not the full content."""
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------- formatters


def _short_iso(ts: str | None) -> str:
    """Render an ISO timestamp as HH:MM UTC. Returns empty string on
    parse failure so the message body just omits the line."""
    if not ts:
        return ""
    try:
        # Tolerate 'Z' suffix and fractional seconds.
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%H:%M UTC")
    except Exception:
        return ""


async def _format_question_msg(ev: dict[str, Any]) -> str:
    """Compose the Telegram message body for an unanswered
    AskUserQuestion."""
    agent_id = ev.get("agent_id") or "system"
    label = await _agent_label(agent_id)
    qs = ev.get("questions") or []
    body = ev.get("body") or ""
    when = _short_iso(ev.get("ts"))
    deadline = _short_iso(ev.get("deadline_at"))

    lines: list[str] = []
    lines.append(f"[?] Question from {label}")
    if when:
        lines.append(f"asked at {when}" + (f", deadline {deadline}" if deadline else ""))
    elif deadline:
        lines.append(f"deadline {deadline}")
    lines.append("")
    if isinstance(qs, list) and qs:
        # `questions` is the structured array AskUserQuestion accepts.
        # Render each in plain text — Telegram's basic message format
        # doesn't support markdown without explicit `parse_mode`.
        for i, q in enumerate(qs, start=1):
            qtext = (q.get("question") or "").strip() if isinstance(q, dict) else str(q).strip()
            if not qtext:
                continue
            lines.append(f"{i}. {qtext}")
            options = q.get("options") if isinstance(q, dict) else None
            if isinstance(options, list) and options:
                opt_strs = [
                    (o.get("label") if isinstance(o, dict) else str(o)) or ""
                    for o in options
                ]
                opt_strs = [s for s in opt_strs if s]
                if opt_strs:
                    lines.append("   options: " + " / ".join(opt_strs))
    elif body:
        lines.append(_truncate(body))
    lines.append("")
    lines.append("Open the web UI to answer.")
    return "\n".join(lines).strip()


async def _format_plan_msg(ev: dict[str, Any]) -> str:
    """Compose the Telegram message body for an unanswered
    ExitPlanMode plan approval."""
    agent_id = ev.get("agent_id") or "system"
    label = await _agent_label(agent_id)
    plan = ev.get("plan") or ""
    when = _short_iso(ev.get("ts"))
    deadline = _short_iso(ev.get("deadline_at"))

    lines: list[str] = []
    lines.append(f"[plan] Plan approval from {label}")
    if when:
        lines.append(f"requested at {when}" + (f", deadline {deadline}" if deadline else ""))
    elif deadline:
        lines.append(f"deadline {deadline}")
    lines.append("")
    if plan:
        lines.append(_truncate(plan))
    lines.append("")
    lines.append("Approve / reject in the web UI.")
    return "\n".join(lines).strip()


async def _format_proposal_msg(ev: dict[str, Any]) -> str:
    """Compose the Telegram message body for a file-write proposal."""
    agent_id = ev.get("agent_id") or "system"
    label = await _agent_label(agent_id)
    scope = ev.get("scope") or "?"
    path = ev.get("path") or "?"
    summary = ev.get("summary") or ""
    size = ev.get("size")
    when = _short_iso(ev.get("ts"))

    if scope == "truth":
        display_path = f"truth/{path}"
    elif scope == "project_claude_md":
        display_path = "CLAUDE.md"
    else:
        display_path = path

    lines: list[str] = []
    lines.append(f"[file] File-write proposal from {label}")
    if when:
        lines.append(f"proposed at {when}")
    parts = [display_path, f"scope={scope}"]
    if isinstance(size, int):
        parts.append(f"{size} chars")
    lines.append(" — ".join(parts))
    if summary:
        lines.append("")
        lines.append(_truncate(summary))
    lines.append("")
    lines.append("Review the diff in the web UI.")
    return "\n".join(lines).strip()


async def _format_message(kind: str, ev: dict[str, Any]) -> str:
    if kind == "question":
        return await _format_question_msg(ev)
    if kind == "plan":
        return await _format_plan_msg(ev)
    if kind == "proposal":
        return await _format_proposal_msg(ev)
    return ""


# ---------------------------------------------------------------- timers


async def _fire_after_delay(
    kind: str, key: str, ev: dict[str, Any]
) -> None:
    """Sleep until the right moment, then send to Telegram if still
    pending. Cancellation by the resolution path is the normal exit
    — the resolution event arrives, the consumer cancels this task,
    and `asyncio.CancelledError` propagates through the sleep.
    """
    try:
        delay = _delay_seconds()
        if delay <= 0:
            # Watcher is off; shouldn't have been scheduled. Defensive.
            return
        # Decide which delay applies based on whether anyone is watching.
        # Re-checking at fire time would add complexity (subscriber
        # could connect/disconnect during the wait); the create-time
        # check is good enough — if the user wasn't watching when the
        # item came in, we promised a quick ping.
        if bus.subscriber_count > 0:
            wait = delay
        else:
            wait = _grace_seconds()
        await asyncio.sleep(wait)

        text = await _format_message(kind, ev)
        if not text:
            return
        try:
            sent = await send_outbound(text)
        except Exception:
            logger.exception(
                "telegram_escalation: send failed (kind=%s, key=%s)", kind, key,
            )
            return
        if not sent:
            # Bridge disabled / unconfigured — silent no-op, matches
            # the spec.
            return
        logger.info(
            "telegram_escalation: pinged for %s/%s (waited %ds, agent=%s)",
            kind, key, wait, ev.get("agent_id") or "?",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "telegram_escalation: timer crashed (kind=%s, key=%s)", kind, key,
        )
    finally:
        # Whether we fired, no-op'd, or were cancelled: drop our handle
        # so the dict doesn't grow unboundedly across long-lived
        # processes.
        existing = _pending.get((kind, key))
        if existing is asyncio.current_task():
            _pending.pop((kind, key), None)


def _schedule(kind: str, key: str, ev: dict[str, Any]) -> None:
    """Replace any prior timer for this key with a fresh one.

    A duplicate `pending_question` / `pending_plan` for the same
    correlation_id shouldn't happen in practice (registries dedupe
    upstream), but if it does we'd rather have one fresh timer than
    two competing ones.
    """
    existing = _pending.get((kind, key))
    if existing is not None and not existing.done():
        existing.cancel()
    task = asyncio.create_task(
        _fire_after_delay(kind, key, ev),
        name=f"harness.telegram_escalation.{kind}:{key}",
    )
    _pending[(kind, key)] = task


def _cancel(kind: str, key: str) -> bool:
    """Cancel the pending timer for this key. Returns True if there
    was one to cancel. Idempotent — a stray resolution event with no
    matching pending item is harmless."""
    task = _pending.pop((kind, key), None)
    if task is None or task.done():
        return False
    task.cancel()
    return True


# ---------------------------------------------------------------- consumer


async def _handle_event(ev: dict[str, Any]) -> None:
    """Dispatch a bus event to the schedule/cancel paths."""
    if _delay_seconds() <= 0:
        # Watcher disabled at runtime — drop everything. We still
        # consume events from the queue (otherwise they'd back up and
        # trip the queue-full backpressure) but do nothing with them.
        return
    pkey = _key_for_pending(ev)
    if pkey is not None:
        _schedule(pkey[0], pkey[1], ev)
        return
    rkey = _key_for_resolution(ev)
    if rkey is not None:
        _cancel(rkey[0], rkey[1])


async def _run(queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Drain the pre-subscribed queue, dispatch matching events.

    Mirrors the audit_watcher pattern: per-event errors are logged
    and swallowed so a single malformed event doesn't kill the
    subscriber. CancelledError exits cleanly via the outer try.
    """
    try:
        while not _stopping:
            try:
                ev = await queue.get()
            except asyncio.CancelledError:
                return
            try:
                await _handle_event(ev)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "telegram_escalation: handler crashed on event %r",
                    ev.get("type"),
                )
    finally:
        bus.unsubscribe(queue)


# ---------------------------------------------------------------- lifecycle


async def start_escalation_watcher() -> None:
    """Start the watcher. Idempotent. No-op when the env disables it
    (delay = 0).

    Subscribes synchronously before scheduling the consumer task —
    same race-avoidance as the Compass audit watcher.
    """
    global _current_task, _stopping
    if _delay_seconds() <= 0:
        logger.info(
            "telegram_escalation: disabled (HARNESS_TELEGRAM_ESCALATION_SECONDS=0)"
        )
        return
    if is_running():
        return
    _stopping = False
    queue = bus.subscribe()
    loop = asyncio.get_running_loop()
    _current_task = loop.create_task(
        _run(queue), name="harness.telegram_escalation",
    )
    logger.info(
        "telegram_escalation: started (delay=%ss, grace=%ss)",
        _delay_seconds(), _grace_seconds(),
    )


async def stop_escalation_watcher(timeout: float = 2.0) -> None:
    """Stop the watcher and cancel all in-flight timers. Idempotent."""
    global _current_task, _stopping
    _stopping = True
    # Cancel pending timers first so they don't fire in the cleanup
    # window. They each clean themselves out of `_pending` in their
    # finally block.
    for key, task in list(_pending.items()):
        if not task.done():
            task.cancel()
    _pending.clear()

    task = _current_task
    if task is None:
        return
    if not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _current_task = None

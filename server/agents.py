from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from server.context import build_system_prompt_suffix
from server.db import configured_conn
from server.events import bus
from server.mcp_config import load_external_servers
from server.tools import ALLOWED_COACH_TOOLS, ALLOWED_PLAYER_TOOLS, build_coord_server
from server.workspaces import workspace_dir

logger = logging.getLogger("harness.agents")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _emit(agent_id: str, event_type: str, **payload: Any) -> None:
    await bus.publish(
        {"ts": _now(), "agent_id": agent_id, "type": event_type, **payload}
    )


_TOOL_RESULT_CAP = 4000


def _stringify_tool_result(content: Any) -> str:
    """Flatten ToolResultBlock.content (str | list[block] | None) to a
    single string, capped to keep event payloads reasonable.

    Non-text blocks (e.g. images returned by Read) are summarized as
    `[ImageBlock]` placeholders so the UI shows that something came back
    without dumping base64 into the event log.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:_TOOL_RESULT_CAP]
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            text = getattr(b, "text", None)
            if isinstance(text, str):
                parts.append(text)
            else:
                parts.append(f"[{type(b).__name__}]")
        joined = "\n".join(parts)
        return joined[:_TOOL_RESULT_CAP]
    return str(content)[:_TOOL_RESULT_CAP]


async def _insert_turn_row(
    *,
    agent_id: str,
    started_at: str,
    ended_at: str,
    duration_ms: int | None,
    cost_usd: float | None,
    session_id: str | None,
    num_turns: int | None,
    stop_reason: str | None,
    is_error: bool,
    model: str | None,
    plan_mode: bool,
    effort: int | None,
) -> None:
    """Insert one row into the `turns` ledger — cheap analytics table
    (one row per SDK ResultMessage). Errors are swallowed: losing a
    ledger row should never break the live turn, the event log is
    still the source of truth for audit."""
    if agent_id == "system":
        return
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO turns ("
                "agent_id, started_at, ended_at, duration_ms, cost_usd, "
                "session_id, num_turns, stop_reason, is_error, "
                "model, plan_mode, effort"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent_id,
                    started_at,
                    ended_at,
                    duration_ms,
                    cost_usd,
                    session_id,
                    num_turns,
                    stop_reason,
                    1 if is_error else 0,
                    model,
                    1 if plan_mode else 0,
                    effort,
                ),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("insert_turn_row failed: agent=%s", agent_id)


async def _handle_message(
    agent_id: str,
    msg: Any,
    turn_ctx: dict[str, Any] | None = None,
) -> None:
    """Turn one SDK message into one or more bus events.

    Extracted from run_agent so the stale-session retry path can
    reuse it without duplicating the dispatch chain. Unknown message
    types are silently skipped — future SDK additions won't break us.

    When a ResultMessage arrives AND turn_ctx is passed, also appends
    a row to the `turns` analytics ledger. turn_ctx carries the
    per-turn inputs that aren't on the ResultMessage itself (model
    override, plan_mode flag, effort level, started_at stamp).
    """
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                await _emit(agent_id, "text", content=block.text)
                # Compact-mode accumulator: the final assistant text
                # becomes the continuity note. We keep appending so a
                # multi-block response still coalesces.
                if turn_ctx is not None and turn_ctx.get("compact_mode"):
                    prev = turn_ctx.get("compact_text") or ""
                    turn_ctx["compact_text"] = (
                        prev + ("\n\n" if prev else "") + (block.text or "")
                    )
            elif isinstance(block, ThinkingBlock):
                # Final consolidated thinking content — surfaces as a
                # collapsible card in the UI.
                await _emit(agent_id, "thinking", content=block.thinking)
            elif isinstance(block, ToolUseBlock):
                # Stash the attempted tool name so the error handler
                # in run_agent can recognize disallowed-tool
                # ProcessErrors and emit a friendlier message.
                if turn_ctx is not None:
                    turn_ctx["last_tool"] = block.name
                await _emit(
                    agent_id, "tool_use",
                    id=block.id, name=block.name, input=block.input,
                )
    elif isinstance(msg, StreamEvent):
        # Partial-message deltas. Only the raw Anthropic streaming
        # event types we care about get mirrored to WS — the rest
        # (message_start, content_block_start, message_stop, …) just
        # consolidate into the AssistantMessage we already handle.
        evt = getattr(msg, "event", None)
        if not isinstance(evt, dict):
            return
        if evt.get("type") != "content_block_delta":
            return
        delta = evt.get("delta") or {}
        dt = delta.get("type")
        if dt == "text_delta":
            text = delta.get("text", "")
            if text:
                await _emit(
                    agent_id, "text_delta",
                    block_index=evt.get("index"), delta=text,
                )
        elif dt == "thinking_delta":
            text = delta.get("thinking", "")
            if text:
                await _emit(
                    agent_id, "thinking_delta",
                    block_index=evt.get("index"), delta=text,
                )
    elif isinstance(msg, UserMessage):
        # Carries tool results; we surface them so the UI can pair
        # each tool_use with its output.
        for block in msg.content:
            if isinstance(block, ToolResultBlock):
                content = _stringify_tool_result(block.content)
                await _emit(
                    agent_id, "tool_result",
                    tool_use_id=block.tool_use_id,
                    content=content,
                    is_error=bool(getattr(block, "is_error", False)),
                )
    elif isinstance(msg, ResultMessage):
        cost = getattr(msg, "total_cost_usd", None)
        session_id = getattr(msg, "session_id", None)
        duration_ms = getattr(msg, "duration_ms", None)
        num_turns = getattr(msg, "num_turns", None)
        stop_reason = getattr(msg, "stop_reason", None)
        # Mark the turn as having emitted a terminal result. The SDK
        # occasionally raises a ProcessError during subprocess teardown
        # (exit=1 with empty stderr) AFTER delivering ResultMessage —
        # the turn's actual work completed fine, but the async
        # generator still reports failure. The run_agent error handler
        # uses this flag to downgrade that specific case to a log.
        if turn_ctx is not None:
            turn_ctx["got_result"] = True
        await _emit(
            agent_id, "result",
            duration_ms=duration_ms,
            cost_usd=cost,
            is_error=msg.is_error,
            session_id=session_id,
        )
        await _add_cost(agent_id, cost)
        # Compact turn completed successfully: commit the assistant's
        # summary as the continuity note and null session_id so the
        # NEXT turn starts on a fresh conversation with the summary
        # injected into its system prompt. A compact turn that errored
        # leaves both fields alone — user retries or falls back to a
        # plain /clear.
        if (
            turn_ctx is not None
            and turn_ctx.get("compact_mode")
            and not msg.is_error
        ):
            summary = (turn_ctx.get("compact_text") or "").strip()
            if summary:
                await _set_continuity_note(agent_id, summary)
                await _set_session_id(agent_id, None)
                await _emit(
                    agent_id,
                    "session_compacted",
                    chars=len(summary),
                )
            else:
                # Model didn't produce any text — don't wipe the session
                # just because we asked it to. Same as a compact failure.
                await _set_session_id(agent_id, session_id)
        else:
            await _set_session_id(agent_id, session_id)
            # First fresh turn AFTER a compact: the handoff has been
            # consumed via the system prompt. Clear it so subsequent
            # turns (now resuming this new session_id) don't keep
            # re-injecting a stale summary.
            if (
                turn_ctx is not None
                and not turn_ctx.get("compact_mode")
                and not msg.is_error
                and turn_ctx.get("had_handoff_on_entry")
            ):
                await _set_continuity_note(agent_id, None)
        if turn_ctx is not None:
            await _insert_turn_row(
                agent_id=agent_id,
                started_at=turn_ctx.get("started_at") or _now(),
                ended_at=_now(),
                duration_ms=duration_ms,
                cost_usd=cost,
                session_id=session_id,
                num_turns=num_turns,
                stop_reason=str(stop_reason) if stop_reason is not None else None,
                is_error=bool(msg.is_error),
                model=turn_ctx.get("model"),
                plan_mode=bool(turn_ctx.get("plan_mode")),
                effort=turn_ctx.get("effort"),
            )


async def _set_status(agent_id: str, status: str) -> None:
    if agent_id == "system":
        return
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE agents SET status = ?, last_heartbeat = ? WHERE id = ?",
                (status, _now(), agent_id),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("set_status failed: agent=%s status=%s", agent_id, status)


AGENT_DAILY_CAP_USD = float(os.environ.get("HARNESS_AGENT_DAILY_CAP", "5.0"))
TEAM_DAILY_CAP_USD = float(os.environ.get("HARNESS_TEAM_DAILY_CAP", "20.0"))

# Currently-running agent tasks, keyed by slot id. Used by the cancel
# endpoint to abort a spiraling run without waiting for max_turns / cap.
# Populated by run_agent; cleared on completion (success or error).
_running_tasks: dict[str, asyncio.Task[Any]] = {}

# Global pause switch: when True, run_agent rejects new starts (emits
# a 'paused' event) and coach_tick_loop skips its tick. In-flight turns
# are NOT cancelled — to stop those, use POST /api/agents/<id>/cancel.
# In-memory only: a restart lifts the pause automatically.
_paused = False


def is_paused() -> bool:
    return _paused


def set_paused(v: bool) -> None:
    global _paused
    _paused = bool(v)


def is_agent_running(agent_id: str) -> bool:
    """True when there's a live in-flight SDK turn for this slot."""
    task = _running_tasks.get(agent_id)
    return task is not None and not task.done()


async def cancel_agent(agent_id: str) -> bool:
    """Cancel the in-flight SDK query for `agent_id`, if any. Returns
    True if a task was cancelled, False if the agent wasn't running.

    The cancellation propagates as asyncio.CancelledError up through
    the `async for msg in query(...)` loop — run_agent's exception
    handler catches it, emits an 'error' event, and sets status=error."""
    task = _running_tasks.get(agent_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


async def cancel_all_agents() -> list[str]:
    """Cancel every in-flight run. Returns the list of agent ids that
    were actually cancelled (skips already-finished tasks)."""
    cancelled: list[str] = []
    for agent_id in list(_running_tasks.keys()):
        if await cancel_agent(agent_id):
            cancelled.append(agent_id)
    return cancelled

# Standard prompt the autonomous loop and POST /api/coach/tick both
# send to Coach. Kept here so callers stay in sync.
COACH_TICK_PROMPT = (
    "Routine tick. Read your inbox for new human goals and Player updates. "
    "If there's nothing actionable, end the turn without calling tools. "
    "Otherwise decompose goals into tasks, assign or reassign as needed, "
    "and reply to Players who need direction. Be terse."
)
COACH_TICK_INTERVAL_SECONDS = int(
    os.environ.get("HARNESS_COACH_TICK_INTERVAL", "0")
)

# Mutable Coach tick interval — initialized from the env var, but
# changeable at runtime via set_coach_interval() (POST /api/coach/loop
# or the /loop slash command). The loop reads this each iteration, so
# changes take effect on the NEXT tick without restart.
_coach_tick_interval: int = COACH_TICK_INTERVAL_SECONDS


def get_coach_interval() -> int:
    return _coach_tick_interval


def set_coach_interval(seconds: int) -> None:
    """Update the Coach autoloop cadence at runtime. 0 disables. The
    loop polls this every few seconds so changes take effect promptly."""
    global _coach_tick_interval
    _coach_tick_interval = max(0, int(seconds))
    logger.info("coach autoloop interval set to %ds", _coach_tick_interval)


# Auto-wake: when Coach assigns a task to p3 or messages p3, we start
# a turn for p3 with a wake prompt so the Player actually engages —
# without this, assignments just sit in the DB doing nothing. Debounce
# prevents tight ping-pong loops: if an agent finished a turn within
# AUTOWAKE_DEBOUNCE_SECONDS, skip. Independent of the Coach autoloop.
AUTOWAKE_DEBOUNCE_SECONDS = int(
    os.environ.get("HARNESS_AUTOWAKE_DEBOUNCE", "10")
)
_last_turn_ended_at: dict[str, float] = {}


def _today_utc_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


async def _today_spend(agent_id: str | None = None) -> float:
    """Sum `turns.cost_usd` for rows that ended today (UTC). Pass
    agent_id to scope to one slot, or None for the whole team.

    Uses the turns ledger (indexed on agent_id + ended_at) rather
    than the events table (which would require JSON extraction on
    every row). Same answer, single-index lookup. The ledger is
    populated by every ResultMessage via _insert_turn_row.
    """
    start_ts = _today_utc_start_iso()
    where = "WHERE ended_at >= ?"
    params: list[Any] = [start_ts]
    if agent_id:
        where += " AND agent_id = ?"
        params.append(agent_id)
    c = await configured_conn()
    try:
        cur = await c.execute(
            f"SELECT COALESCE(SUM(cost_usd), 0) AS total FROM turns {where}",
            params,
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    return float(dict(row)["total"] or 0.0) if row else 0.0


async def _check_cost_caps(agent_id: str) -> tuple[bool, str]:
    """Returns (allowed, reason_if_denied)."""
    if AGENT_DAILY_CAP_USD > 0:
        agent_today = await _today_spend(agent_id)
        if agent_today >= AGENT_DAILY_CAP_USD:
            return (
                False,
                f"agent {agent_id} has spent "
                f"${agent_today:.3f} today, "
                f"at or above its daily cap of "
                f"${AGENT_DAILY_CAP_USD:.2f}. Override with "
                f"HARNESS_AGENT_DAILY_CAP env var.",
            )
    if TEAM_DAILY_CAP_USD > 0:
        team_today = await _today_spend()
        if team_today >= TEAM_DAILY_CAP_USD:
            return (
                False,
                f"team has spent ${team_today:.3f} today, "
                f"at or above the team daily cap of "
                f"${TEAM_DAILY_CAP_USD:.2f}. Override with "
                f"HARNESS_TEAM_DAILY_CAP env var.",
            )
    return True, ""


async def _add_cost(agent_id: str, cost_usd: float | None) -> None:
    if not cost_usd or agent_id == "system":
        return
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE agents SET cost_estimate_usd = cost_estimate_usd + ? "
                "WHERE id = ?",
                (cost_usd, agent_id),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("add_cost failed: agent=%s cost=%s", agent_id, cost_usd)


async def _get_session_id(agent_id: str) -> str | None:
    """Read agent.session_id (from the last turn's ResultMessage).
    None when the agent has never run, or DELETE /api/agents/<id>/session
    has cleared it for a fresh-context restart."""
    if agent_id == "system":
        return None
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT session_id FROM agents WHERE id = ?", (agent_id,)
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("get_session_id failed: agent=%s", agent_id)
        return None
    if not row:
        return None
    v = dict(row).get("session_id")
    return v if v else None


async def _get_agent_brief(agent_id: str) -> str | None:
    """Read agent.brief — the human-authored context string for this
    specific slot, injected into the system prompt on every turn.

    Returns None if the column is NULL / empty or the read failed.
    """
    if agent_id == "system":
        return None
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT brief FROM agents WHERE id = ?", (agent_id,)
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("get_agent_brief failed: agent=%s", agent_id)
        return None
    if not row:
        return None
    v = dict(row).get("brief")
    return v if v else None


async def _get_continuity_note(agent_id: str) -> str | None:
    """Read agent.continuity_note — a compact-generated summary of the
    prior session, injected into the next fresh turn's system prompt."""
    if agent_id == "system":
        return None
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT continuity_note FROM agents WHERE id = ?", (agent_id,)
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("get_continuity_note failed: agent=%s", agent_id)
        return None
    if not row:
        return None
    v = dict(row).get("continuity_note")
    return v if v else None


async def _set_continuity_note(agent_id: str, text: str | None) -> None:
    """Write (or clear) the continuity note. Pass None/empty to clear."""
    payload = (text or "").strip() or None
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET continuity_note = ? WHERE id = ?",
            (payload, agent_id),
        )
        await c.commit()
    finally:
        await c.close()


async def _locked_players() -> list[str]:
    """Return sorted list of Player slot ids that have locked=1.

    Used to inject a Coach-side hint into the system prompt so Coach
    plans around the constraint instead of burning turns hitting the
    tool-layer rejection. Swallows DB errors (returns empty) — the
    tool-layer enforcement is authoritative; this is just guidance.
    """
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id FROM agents WHERE kind = 'player' AND locked = 1 ORDER BY id"
            )
            rows = await cur.fetchall()
        finally:
            await c.close()
    except Exception:
        return []
    return [dict(r)["id"] for r in rows]


async def _get_role_default_model(agent_id: str) -> str | None:
    """Read the per-role default model from team_config.

    Keys:
      coach_default_model   applies when agent_id == 'coach'
      players_default_model applies to p1..p10

    Returns None when the row is missing / empty / unreadable so the
    caller can fall back to the SDK default (or the per-pane override
    that takes precedence upstream).
    """
    key = "coach_default_model" if agent_id == "coach" else "players_default_model"
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT value FROM team_config WHERE key = ?", (key,)
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("get_role_default_model failed: agent=%s", agent_id)
        return None
    if not row:
        return None
    val = (dict(row).get("value") or "").strip()
    # team_config values are stored as JSON; unwrap if so, but tolerate
    # a raw string too (future-proof for a manual DB edit).
    if val.startswith('"') and val.endswith('"'):
        try:
            val = json.loads(val)
        except Exception:
            pass
    return val or None


async def _get_team_extra_tools() -> list[str]:
    """Read the team-wide extra-tools allow-list from team_config.

    One setting for the whole team — WebSearch / WebFetch etc. that
    the human toggled on in the Settings drawer apply to every agent
    on every turn. Returns [] when the row is missing / empty / malformed
    so a bad value can't brick spawning.
    """
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT value FROM team_config WHERE key = 'extra_tools'"
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("get_team_extra_tools failed")
        return []
    if not row:
        return []
    raw = dict(row).get("value")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        logger.warning("team_config.extra_tools is not valid JSON, ignoring")
        return []
    if not isinstance(parsed, list):
        return []
    return [t for t in parsed if isinstance(t, str) and t]


async def _set_session_id(agent_id: str, session_id: str | None) -> None:
    """Persist the SDK's session_id for this agent's last turn. Pure
    instrumentation right now — actual resume-from-session-id lands in
    a later M5 step once we confirm the SDK API surface."""
    if not session_id or agent_id == "system":
        return
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE agents SET session_id = ? WHERE id = ?",
                (session_id, agent_id),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("set_session_id failed: agent=%s", agent_id)


async def _clear_session_id(agent_id: str) -> None:
    """Forget a stored session_id so the next turn starts fresh.

    Used when a `resume=<session>` attempt fails (stale session — e.g.
    after a CLI re-login or CLI version bump) so we auto-heal instead
    of staying stuck forever on a bad reference.
    """
    if agent_id == "system":
        return
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE agents SET session_id = NULL WHERE id = ?", (agent_id,)
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("clear_session_id failed: agent=%s", agent_id)


# Men's Field Lacrosse last names — fits the "team of ten" metaphor
# (lacrosse puts 10 players on the field, vs 11 for soccer). Pool is
# larger than 10 so the picker can avoid collisions; all ASCII so the
# pane label is safe everywhere.
_LACROSSE_SURNAMES: tuple[str, ...] = (
    "Rabil", "Powell", "Gait", "Harrison", "Merrill", "Thompson",
    "Pannell", "Schreiber", "Sowers", "Teat", "Rambo", "Grant",
    "Crotty", "Danowski", "Hubbard", "Millon", "Ament", "Spallina",
    "Spencer", "Gray", "Fields", "Galloway", "Durkin", "Ward",
    "Coffman", "Boyle", "Stanwick", "Colsey", "Queener", "Poskay",
    "Nardella", "Peyser", "Walters", "Starsia", "Tierney", "Pressler",
    "Flynn", "Pietramala", "Whipple", "Hogan", "Rodgers", "Greer",
    "Tucker", "Williams", "Gurenlian", "Riordan", "Manos", "Hurley",
    "Byrne", "Seibald", "Dunn", "Casey",
)


# Serializes _autoname_player across concurrent spawns. Without this,
# two Players waking at the same millisecond (e.g. Coach broadcasts a
# batch of task_assigned events) can each see the same 'taken' set and
# pick the same surname. The lock is per-process only — fine since our
# harness is single-process by design.
_AUTONAME_LOCK = asyncio.Lock()

# Serializes the concurrent-spawn guard's check + register. Without
# this, two parallel run_agent coroutines can both pass the 'already
# running?' check before either has claimed the slot in _running_tasks,
# and we end up with two simultaneous Claude subprocesses for the
# same agent. Held only for check + register + (if rejected) one emit.
_SPAWN_LOCK = asyncio.Lock()


async def _autoname_player(agent_id: str) -> str | None:
    """If this Player slot has no name yet, pick an unused lacrosse
    surname and persist it. Returns the chosen name, or None if the
    slot already had one / isn't a player / we ran out of names.

    Runs once per slot lifetime (becomes a no-op after first call).
    Serialized by _AUTONAME_LOCK so concurrent first-spawns can't
    pick the same surname before either has committed.
    """
    import random

    async with _AUTONAME_LOCK:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT kind, name FROM agents WHERE id = ?", (agent_id,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
            if d["kind"] != "player" or d["name"]:
                return None
            cur = await c.execute(
                "SELECT name FROM agents WHERE kind = 'player' AND name IS NOT NULL"
            )
            taken = {dict(r)["name"] for r in await cur.fetchall()}
            candidates = [n for n in _LACROSSE_SURNAMES if n not in taken]
            if not candidates:
                return None
            pick = random.choice(candidates)
            await c.execute(
                "UPDATE agents SET name = ? WHERE id = ?", (pick, agent_id)
            )
            await c.commit()
        finally:
            await c.close()
    await bus.publish(
        {
            "ts": _now(),
            "agent_id": agent_id,
            "type": "player_assigned",
            "name": pick,
            "auto": True,
        }
    )
    return pick


def _system_prompt_for(agent_id: str) -> str:
    if agent_id == "coach":
        return (
            "You are Coach, the captain of the TeamOfTen team. Your job is to "
            "decompose human goals into tasks, assign them to Players (slots "
            "p1..p10), and orchestrate progress.\n\n"
            "Coordination tools:\n"
            "  - coord_list_tasks(status?, owner?): see the team board\n"
            "  - coord_create_task(title, description?, priority?): add top-level tasks\n"
            "  - coord_assign_task(task_id, to): push-assign an open task directly "
            "to a Player (faster than waiting for them to self-claim)\n"
            "  - coord_update_task(task_id, status, note?): you can cancel any task\n"
            "  - coord_send_message(to, body, subject?, priority?): message a Player "
            "or 'broadcast' to the whole team\n"
            "  - coord_read_inbox(): read messages addressed to you or the team\n"
            "  - coord_list_memory / coord_read_memory / coord_update_memory: "
            "shared scratchpad for the team — drop conventions, "
            "gotchas here so Players don't have to ask twice\n"
            "  - coord_write_decision(title, body): append a dated, immutable "
            "architectural decision record. Use for 'we chose X over Y because Z' "
            "— these never get overwritten (unlike memory).\n"
            "  - coord_write_context(kind, name, body): Coach-only governance "
            "docs. kind='root' writes CLAUDE.md; 'skills' and 'rules' write "
            "per-file entries under those folders. Every agent loads these "
            "into their system prompt at the next turn — no restart needed.\n"
            "  - coord_write_knowledge(path, body) / coord_read_knowledge(path) "
            "/ coord_list_knowledge(): the team's durable output bucket. "
            "Free-form paths under knowledge/ for reports, research, specs. "
            "Agent-produced artifacts you want readable weeks later.\n"
            "  - coord_save_output(path, content_base64): ship BINARY "
            "deliverables (docx, pdf, xlsx, png, zip, …). Text reports go to "
            "knowledge/; finished binaries the human asked for go here. "
            "Mirrored to kDrive outputs/ so the human sees them there.\n"
            "  - coord_list_team(): current roster (name, role, brief, "
            "status, current task) for every agent. Call this at the "
            "start of a turn if you need to remember who's on the team.\n"
            "  - coord_set_player_role(player_id, name, role): assign a "
            "Player their name + role (e.g. p3 → 'Alice' / 'Frontend developer'). "
            "Do this once per Player when forming the team — the UI labels "
            "their pane from these values.\n"
            "  - coord_request_human(subject, body, urgency?): escalate to the "
            "human when a decision exceeds your authority or the team is "
            "stuck. urgency='blocker' for whole-team gating.\n"
            "\n"
            "Data paths:\n"
            "  - ./uploads/ (Players only; symlinked per-workspace): "
            "human-uploaded reference material — PDFs, specs, screenshots. "
            "Auto-synced from kDrive every ~60s. Tell Players to Read "
            "./uploads/<filename> when the human mentions a document.\n"
            "  - knowledge/<path>.md: team-authored text (reports, research, "
            "specs). Written via coord_write_knowledge.\n"
            "  - outputs/<path>.<ext>: team-authored binaries (docx, pdf, …). "
            "Written via coord_save_output. Mirrors to kDrive outputs/.\n"
            "  - memory/<topic>.md: scratchpad (overwritable, versioned).\n"
            "  - decisions/<date>-slug.md: immutable ADRs (Coach-only write).\n"
            "\n"
            "Rules:\n"
            "  - You never write code; you delegate.\n"
            "  - Only you can create top-level tasks — Players can only subtask.\n"
            "  - You are the sole source of assignments; Players claim them.\n"
            "  - Start every turn by reading your inbox for new human goals.\n"
            "  - Be terse."
        )
    return (
        f"You are Player {agent_id} on the TeamOfTen team. Your name and role "
        f"will be assigned by Coach; for now work with your slot id.\n\n"
        f"Coordination tools:\n"
        f"  - coord_list_tasks(status?, owner?): see the team board\n"
        f"  - coord_claim_task(task_id): claim an open task (one at a time)\n"
        f"  - coord_update_task(task_id, status, note?): report progress\n"
        f"      valid next states: in_progress, blocked, done, cancelled\n"
        f"  - coord_create_task(title, ...): create SUBTASKS of tasks you own\n"
        f"      (you cannot create top-level tasks — that's Coach's job)\n"
        f"  - coord_send_message(to, body, ...): message Coach or a peer for info\n"
        f"      (you CANNOT use this to assign work — only Coach assigns)\n"
        f"  - coord_read_inbox(): read messages addressed to you or the team\n"
        f"  - coord_list_memory / coord_read_memory / coord_update_memory:\n"
        f"      shared scratchpad. Read it to see what other agents found; "
        f"write to it when you learn something worth preserving.\n"
        f"  - coord_list_knowledge / coord_read_knowledge / coord_write_knowledge:\n"
        f"      durable artifact bucket. Check existing paths before producing "
        f"a report to avoid duplicating work. Write long-form output here "
        f"(e.g. 'reports/2026-04-23-api-audit.md') — not into memory.\n"
        f"  - coord_save_output(path, content_base64): ship BINARY deliverables "
        f"(docx, pdf, xlsx, png, zip). Text reports belong in knowledge/; use "
        f"this only for finished binaries the human asked for. Encode with "
        f"`base64 -w0 file.ext` via Bash. Mirrored to kDrive outputs/.\n"
        f"  - coord_commit_push(message, push?): when you have code changes "
        f"to ship, use this instead of driving git through Bash — it does "
        f"git add -A + commit + push and emits a commit_pushed event.\n"
        f"  - coord_request_human(subject, body, urgency?): escalate to the "
        f"human when blocked on something only they can decide. Prefer this "
        f"over going silent — say what you tried.\n"
        f"\n"
        f"Data paths (in your workspace cwd):\n"
        f"  - ./uploads/: human-uploaded reference material — PDFs, specs, "
        f"screenshots. Read-only, auto-synced from kDrive every ~60s. When "
        f"Coach or the human refers to 'the doc I uploaded', look here first.\n"
        f"  - ./project/: your git worktree (if HARNESS_PROJECT_REPO is set). "
        f"This is where you edit code; use coord_commit_push to ship.\n"
        f"  - ./attachments/: pasted images from the UI (symlink to a shared "
        f"store).\n"
        f"Team-wide paths (outside your workspace):\n"
        f"  - knowledge/<path>.md: long-form text artifacts — write via "
        f"coord_write_knowledge.\n"
        f"  - outputs/<path>.<ext>: binary deliverables — write via "
        f"coord_save_output (base64-encoded).\n"
        f"  - memory/<topic>.md: team scratchpad via coord_*_memory tools.\n"
        f"\n"
        f"Rules:\n"
        f"  - You execute and report. You do not assign work to other Players.\n"
        f"  - Start every turn by reading your inbox for new orders from Coach.\n"
        f"  - Before starting complex work, check memory for prior findings.\n"
        f"  - When you finish, mark the task done — that frees you for the next.\n"
        f"  - If blocked, mark blocked with a note explaining why.\n"
        f"  - Be terse."
    )


# UI effort levels (1..4) map directly onto the SDK's Literal values.
_EFFORT_LEVELS = {1: "low", 2: "medium", 3: "high", 4: "max"}


async def run_agent(
    agent_id: str,
    prompt: str,
    *,
    model: str | None = None,
    plan_mode: bool = False,
    effort: int | None = None,
    compact_mode: bool = False,
) -> None:
    """Spawn one SDK query for the given slot and stream its events.

    Optional per-turn overrides:
    - model: SDK `model` kwarg (e.g. "claude-opus-4-7"). None = SDK default.
    - plan_mode: sets permission_mode="plan" so the agent outlines an
      approach before touching tools.
    - effort: 1..4 → "low" | "medium" | "high" | "max" thinking budget.
    """
    # Global pause short-circuits before the cost check; users pausing
    # the harness shouldn't also burn a DB write counting cost.
    if _paused:
        await _emit(agent_id, "paused", prompt=prompt)
        logger.info("paused: refused to spawn %s", agent_id)
        return

    # Concurrent-spawn guard: check + claim the slot atomically under
    # _SPAWN_LOCK so two parallel run_agent coroutines can't both pass
    # the check before either has registered in _running_tasks. The
    # lock also means maybe_wake_agent and direct /api/agents/start
    # callers see the same ordering — no duplicated subprocesses for
    # the same slot regardless of entry path.
    rejected = False
    this_task = asyncio.current_task()
    async with _SPAWN_LOCK:
        existing = _running_tasks.get(agent_id)
        if existing is not None and not existing.done():
            rejected = True
        elif this_task is not None:
            # Claim the slot synchronously with the check so no other
            # coroutine can race past its own guard. The full setup
            # (autoname / cost cap / session read) continues AFTER the
            # lock releases, which is fine — we already hold the slot.
            _running_tasks[agent_id] = this_task
    if rejected:
        await _emit(
            agent_id,
            "spawn_rejected",
            reason="already running a turn — wait or ⏹ cancel first",
            prompt=prompt,
        )
        logger.info("spawn_rejected: %s already running", agent_id)
        return

    # First-spawn auto-name: if Coach hasn't assigned this Player a
    # name, pick an unused soccer surname so the pane header reads
    # "p3 — Mbappe" instead of "p3 — unassigned". Coach's
    # coord_set_player_role still overrides at any time.
    await _autoname_player(agent_id)

    # Enforce daily cost caps BEFORE emitting agent_started — if the
    # caller is over budget we want the rejection visible in the
    # timeline and no SDK work done.
    allowed, reason = await _check_cost_caps(agent_id)
    if not allowed:
        await _emit(agent_id, "cost_capped", reason=reason, prompt=prompt)
        logger.warning("cost cap blocked spawn: %s", reason)
        # Release the slot we claimed under _SPAWN_LOCK so the next
        # attempt (after the user raises the cap / tomorrow rolls
        # over) isn't rejected for "already running".
        _running_tasks.pop(agent_id, None)
        return

    # Read prior session BEFORE emitting agent_started so the event
    # carries the resume flag — the UI can visually distinguish fresh
    # turns from continuations.
    prior_session = await _get_session_id(agent_id)

    # Status flip BEFORE the agent_started WS event, not after: the UI
    # handler refetches /api/agents on agent_started to repaint the
    # left-rail slot, and it needs to see status='working' on that
    # fetch — otherwise it paints the amber pulse one event late.
    await _set_status(agent_id, "working")
    await _emit(
        agent_id,
        "agent_started",
        prompt=prompt,
        resumed_session=bool(prior_session),
    )

    coord_server = build_coord_server(agent_id)
    allowed = list(
        ALLOWED_COACH_TOOLS if agent_id == "coach" else ALLOWED_PLAYER_TOOLS
    )
    # Team-wide extras the human opted into via the Settings drawer
    # (e.g. WebSearch / WebFetch). Applies to every agent on every
    # turn — read fresh each spawn so toggles take effect on the
    # next run with no restart. Duplicates from the baseline are harmless.
    team_extras = await _get_team_extra_tools()
    if team_extras:
        allowed.extend(team_extras)
    # External MCP servers (GitHub / Linear / Notion / …) come from
    # HARNESS_MCP_CONFIG. Loaded fresh each spawn so edits to the
    # config file take effect on the next turn with no restart. Empty
    # when no config — existing coord-only behavior preserved.
    external_servers, external_tools = load_external_servers()
    allowed.extend(external_tools)
    mcp_servers: dict[str, Any] = {"coord": coord_server, **external_servers}

    # Governance-layer docs (CLAUDE.md / skills / rules) from kDrive/disk.
    # Appended to the hardcoded role brief so context edits take effect on
    # the next turn with no restart required. Empty string when no
    # context is configured — agents behave as before.
    context_suffix = await build_system_prompt_suffix()
    # Per-agent brief — free-form context the human set via
    # PUT /api/agents/{id}/brief. Injected AFTER the governance layer so
    # it can narrow / specialize without being overwhelmed by team-wide
    # rules. Empty / NULL column → no suffix.
    brief_suffix = ""
    brief_text = await _get_agent_brief(agent_id)
    if brief_text:
        brief_suffix = (
            "\n\n## Agent brief (specific to you, set by the human)\n\n"
            + brief_text.strip()
        )
    # Continuity note — summary written by a prior /compact turn. Only
    # consumed on the FIRST fresh turn after compact (session_id was
    # nulled at compact time, so prior_session is None here). We still
    # inject it whenever present; it stays until the agent is
    # explicitly compacted again or the human clears it via the API.
    # Keep this block BEFORE the lock suffix so the handoff reads
    # naturally — "here's where you left off, here's the current
    # roster state".
    handoff_suffix = ""
    handoff_text = await _get_continuity_note(agent_id)
    if handoff_text:
        handoff_suffix = (
            "\n\n## Handoff from your prior session (via /compact)\n\n"
            + handoff_text.strip()
            + "\n\n(Your previous conversation history has been cleared "
            "to free context. The summary above is your memory of what "
            "came before.)"
        )
    # Lock-state suffix — Coach-only. Tells Coach up-front which
    # Players are off-limits so they plan around the constraint
    # instead of wasting turns hitting the tool-layer rejection.
    # Only injected when at least one Player is locked; a fully-
    # unlocked team gets no extra text.
    lock_suffix = ""
    if agent_id == "coach":
        locked_list = await _locked_players()
        if locked_list:
            lock_suffix = (
                "\n\n## Roster availability (right now)\n\n"
                "The human has LOCKED the following Player(s): "
                + ", ".join(locked_list)
                + ". Do NOT assign tasks to them, do NOT direct-message "
                "them, and remember broadcasts also skip them. Work "
                "around this constraint — pick other Players or tell "
                "the human if no suitable unlocked Player remains."
            )
    system_prompt = (
        _system_prompt_for(agent_id)
        + context_suffix
        + brief_suffix
        + handoff_suffix
        + lock_suffix
    )
    if context_suffix or brief_suffix or handoff_suffix:
        # Emit sizes (not content) so the user can see "yes my stuff was
        # picked up" without drowning the timeline in prompt text.
        await _emit(
            agent_id,
            "context_applied",
            chars=len(context_suffix) + len(brief_suffix) + len(handoff_suffix),
            brief_chars=len(brief_suffix),
            handoff_chars=len(handoff_suffix),
        )

    options_kwargs: dict[str, Any] = dict(
        system_prompt=system_prompt,
        cwd=str(workspace_dir(agent_id)),
        max_turns=10,
        mcp_servers=mcp_servers,
        allowed_tools=allowed,
    )
    # Partial-message streaming (token-by-token text + thinking deltas)
    # is off by default: the option is understood by recent SDK
    # versions but the corresponding CLI flag crashes exit=1 on some
    # Claude Code CLI builds (confirmed against 2.1.118). Flip
    # HARNESS_STREAM_TOKENS=true once you've verified your CLI handles
    # it (e.g. `claude --help | grep partial`). Turns still complete
    # fine without streaming — you just don't get the typing cursor.
    if os.environ.get("HARNESS_STREAM_TOKENS", "").lower() in ("1", "true", "yes"):
        options_kwargs["include_partial_messages"] = True
    # Model resolution precedence (highest -> lowest):
    #   1. per-pane override (`model` arg, from the gear popover)
    #   2. per-role team default (team_config: coach_default_model /
    #      players_default_model), set in the Settings drawer
    #   3. SDK default (no kwarg passed)
    if not model:
        model = await _get_role_default_model(agent_id)
    if model:
        options_kwargs["model"] = model
    if plan_mode:
        options_kwargs["permission_mode"] = "plan"
    if effort and effort in _EFFORT_LEVELS:
        options_kwargs["effort"] = _EFFORT_LEVELS[effort]

    # Resume: if the last turn captured a session_id (loaded above),
    # hand it back to the SDK so this turn continues that conversation.
    if prior_session:
        options_kwargs["resume"] = prior_session

    options = ClaudeAgentOptions(**options_kwargs)

    # (slot already claimed under _SPAWN_LOCK above — the old
    # register-here block has been hoisted; we're past the concurrent-
    # spawn guard AND the cost cap early return by this point.)

    # Per-turn context the ResultMessage handler needs to append a row
    # to the turns ledger. started_at is stamped fresh on each _iterate
    # call so a stale-session retry doesn't reuse the first try's clock.
    turn_ctx: dict[str, Any] = {
        "model": model,
        "plan_mode": plan_mode,
        "effort": effort,
        # When True, the ResultMessage handler saves the accumulated
        # assistant text to agents.continuity_note and nulls session_id,
        # implementing a CLI-/compact-equivalent: summarize, then drop
        # the history so the next turn starts on a clean context.
        "compact_mode": compact_mode,
        # True when the system prompt we just built embedded a handoff
        # section — the ResultMessage handler uses this to clear the
        # note on the first successful non-compact turn after a
        # compact, so stale handoffs don't re-inject forever.
        "had_handoff_on_entry": bool(handoff_suffix) and not compact_mode,
    }

    async def _iterate(opts: ClaudeAgentOptions) -> None:
        # Tiny indirection so we can retry the whole iteration once
        # after stale-session cleanup without duplicating the body.
        turn_ctx["started_at"] = _now()
        async for msg in query(prompt=prompt, options=opts):
            await _handle_message(agent_id, msg, turn_ctx)

    try:
        try:
            await _iterate(options)
        except Exception as e:
            # Stale session auto-heal: when we tried to resume a prior
            # session and the CLI rejected it (happens after /login
            # rotation or CLI upgrade — the session id is a reference
            # the new CLI can't validate), clear the stored id and
            # retry once without resume. Only retry when the failure
            # matches the narrow pattern: we had a prior_session AND
            # the error came from the SDK subprocess layer.
            is_process_err = type(e).__name__ == "ProcessError"
            if prior_session and is_process_err:
                logger.warning(
                    "agent %s: resume of session=%s failed, clearing and retrying fresh",
                    agent_id, prior_session,
                )
                await _emit(
                    agent_id,
                    "session_resume_failed",
                    session_id=prior_session,
                    error=f"{type(e).__name__}: {e}",
                )
                await _clear_session_id(agent_id)
                options_kwargs.pop("resume", None)
                retry_options = ClaudeAgentOptions(**options_kwargs)
                await _iterate(retry_options)
            else:
                raise
    except asyncio.CancelledError:
        # User (or the cost cap) asked us to stop. Emit a distinct
        # event so the timeline shows "cancelled" rather than a
        # generic error, set status back to idle, and re-raise so the
        # task ends in the cancelled state.
        await _emit(agent_id, "agent_cancelled")
        await _set_status(agent_id, "idle")
        _running_tasks.pop(agent_id, None)
        await _emit(agent_id, "agent_stopped")
        raise
    except Exception as e:
        is_process_err = type(e).__name__ == "ProcessError"
        # Noisy-teardown suppression: if the SDK already delivered a
        # ResultMessage this turn, the turn's work completed — any
        # exception bubbling out of the async iterator afterwards is
        # subprocess teardown, not a real failure. Suppress regardless
        # of exception type: earlier CLI builds raised ProcessError
        # here, newer ones (2.1.12x) raise a bare Exception with
        # "Command failed with exit code 1". Either way the 'result'
        # event already carries the is_error flag if the turn genuinely
        # went sideways, so a second red 'error' event is pure noise.
        if turn_ctx.get("got_result"):
            logger.warning(
                "run_agent: suppressed post-result exception (%s) for %s: %s",
                type(e).__name__, agent_id, e,
            )
            await _set_status(agent_id, "idle")
        else:
            # Log the full traceback to stdout so Zeabur captures it;
            # the event only carries a summary so the UI doesn't drown
            # in stack frames, but operators can correlate via the
            # timestamp.
            logger.exception(
                "run_agent failed: agent=%s cwd=%s",
                agent_id, options_kwargs.get("cwd"),
            )
            # Friendlier message when a ProcessError was caused by the
            # model invoking a tool we didn't allow: the CLI exits 1
            # with stderr swallowed, which is opaque. We can spot this
            # case by looking at the last tool_use this turn and
            # checking whether it's in the `allowed` list we passed to
            # the SDK. Any other exception path falls through to the
            # original summary.
            err_msg = f"{type(e).__name__}: {e}"
            last_tool = turn_ctx.get("last_tool")
            if is_process_err and last_tool and last_tool not in allowed:
                err_msg = (
                    f"Agent tried to use tool '{last_tool}' but it is "
                    f"not in allowed_tools for this role. Add it via "
                    f"Options → Team tools or server/tools.py. "
                    f"Original error: {err_msg}"
                )
            await _emit(
                agent_id,
                "error",
                error=err_msg,
                cwd=options_kwargs.get("cwd"),
            )
            await _set_status(agent_id, "error")
    else:
        await _set_status(agent_id, "idle")
    finally:
        _running_tasks.pop(agent_id, None)
        # Stamp when this turn ended so the auto-wake debounce can see
        # it on the next incoming event. Pure in-memory — a restart
        # clears the record, which is fine (first post-restart wake
        # just fires immediately).
        _last_turn_ended_at[agent_id] = time.monotonic()

    await _emit(agent_id, "agent_stopped")


async def maybe_wake_agent(
    agent_id: str,
    reason: str,
    *,
    bypass_debounce: bool = False,
) -> bool:
    """Spawn a turn for `agent_id` with `reason` as the prompt, if and
    only if all guards pass:

      - harness not paused
      - agent not already running (don't stack turns)
      - this agent's previous turn ended more than
        AUTOWAKE_DEBOUNCE_SECONDS ago, UNLESS bypass_debounce=True

    The debounce exists to prevent tight Coach↔Player ping-pong when
    agents chat-reply to each other. Discrete actions (task assignment,
    human message) are NOT ping-pongy and should wake the target even
    if they just finished a turn — callers pass bypass_debounce=True
    for those paths.

    Returns True if a spawn was scheduled, False otherwise. Cost caps
    are checked here AND inside run_agent — the early check avoids a
    storm of cost_capped events when a cap is hit (e.g. Coach assigns
    10 tasks while team-capped, each assign would otherwise spawn a
    turn that immediately fails).
    """
    if agent_id == "system":
        return False
    if _paused:
        return False
    if agent_id in _running_tasks:
        return False
    if not bypass_debounce:
        last_end = _last_turn_ended_at.get(agent_id, 0.0)
        if last_end and (time.monotonic() - last_end) < AUTOWAKE_DEBOUNCE_SECONDS:
            logger.info(
                "auto-wake skipped: %s ended a turn %.1fs ago (<%ds debounce)",
                agent_id,
                time.monotonic() - last_end,
                AUTOWAKE_DEBOUNCE_SECONDS,
            )
            return False
    # Cost cap: if we'd just spawn a turn that would fail the cap check
    # in run_agent, skip silently instead of burning an event + DB write.
    # A manual spawn through the UI / API still goes through run_agent
    # and emits cost_capped the usual way — this is a noise guard for
    # the automatic path only.
    allowed, _reason = await _check_cost_caps(agent_id)
    if not allowed:
        logger.info("auto-wake skipped for %s: cost cap hit", agent_id)
        return False
    logger.info("auto-wake: spawning %s — %s", agent_id, reason[:80])
    asyncio.create_task(run_agent(agent_id, reason))
    return True


async def _coach_is_working() -> bool:
    """Read coach.status. Treat any error or missing row as 'not working'
    so a transient DB hiccup doesn't permanently silence the loop."""
    try:
        c = await configured_conn()
        try:
            cur = await c.execute("SELECT status FROM agents WHERE id = 'coach'")
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("coach autoloop: status read failed")
        return False
    return bool(row) and dict(row)["status"] == "working"


async def coach_tick_loop() -> None:
    """Background task: periodically nudge Coach to drain inbox.

    Reads _coach_tick_interval each iteration so set_coach_interval()
    (from /api/coach/loop or the /loop slash command) can toggle the
    cadence at runtime with no restart. When the interval is <= 0,
    the loop idles with a short poll until it's re-enabled."""
    logger.info(
        "coach autoloop running (initial interval %ds; 0=disabled)",
        _coach_tick_interval,
    )
    while True:
        interval = _coach_tick_interval
        try:
            if interval <= 0:
                # Idle poll — wake every 5s to check if someone enabled
                # us via set_coach_interval().
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        try:
            if _coach_tick_interval <= 0:
                # Disabled between sleep start and now — skip this tick.
                continue
            if _paused:
                logger.info("coach autoloop: skipping — harness paused")
                continue
            if await _coach_is_working():
                logger.info("coach autoloop: skipping — coach is working")
                continue
            await run_agent("coach", COACH_TICK_PROMPT)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("coach autoloop: tick failed")

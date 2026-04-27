from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from server import interactions as interactions_registry
from server.context import build_system_prompt_suffix
from server.db import configured_conn, resolve_active_project
from server.events import bus
from server.webdav import webdav
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


async def _deliver_system_message(
    *,
    from_id: str,
    to_id: str,
    subject: str | None,
    body: str,
    priority: str = "normal",
    wake: bool = True,
) -> None:
    """Insert a harness-generated message into the inbox, publish a
    message_sent event, and (optionally) wake the recipient.

    Used by reliability paths (error notifications, task-done pings,
    stale-task watchdog) that don't route through coord_send_message.
    Silent on failure — the originating flow should continue even if
    notification bookkeeping fails."""
    if not body or not to_id:
        return
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "INSERT INTO messages (project_id, from_id, to_id, subject, body, priority) "
                "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                (project_id, from_id, to_id, subject, body, priority),
            )
            row = await cur.fetchone()
            msg_id = dict(row)["id"] if row else None
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("_deliver_system_message insert failed")
        return
    try:
        await bus.publish({
            "ts": _now(),
            "agent_id": from_id,
            "type": "message_sent",
            "message_id": msg_id,
            "to": to_id,
            "subject": subject,
            "body_preview": body[:120],
            "priority": priority,
        })
    except Exception:
        logger.exception("_deliver_system_message publish failed")
    if wake and to_id != "broadcast":
        try:
            preview = body.strip().replace("\n", " ")[:240]
            subj = f" (subject: {subject})" if subject else ""
            await maybe_wake_agent(
                to_id,
                f"New message from {from_id}{subj}: \"{preview}\"\n\n"
                f"Call coord_read_inbox to mark it read and see any "
                f"other queued messages, then respond as appropriate.",
            )
        except Exception:
            logger.exception("_deliver_system_message wake failed")


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
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
) -> None:
    """Insert one row into the `turns` ledger — cheap analytics table
    (one row per SDK ResultMessage). Errors are swallowed: losing a
    ledger row should never break the live turn, the event log is
    still the source of truth for audit."""
    if agent_id == "system":
        return
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO turns ("
                "agent_id, project_id, started_at, ended_at, duration_ms, cost_usd, "
                "session_id, num_turns, stop_reason, is_error, "
                "model, plan_mode, effort, "
                "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent_id,
                    project_id,
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
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_creation_tokens,
                ),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("insert_turn_row failed: agent=%s", agent_id)


def _extract_usage(msg: Any) -> dict[str, int]:
    """Pull token counts from a ResultMessage's usage block. Defensive
    against SDK shape drift: `usage` may be a dict, a Pydantic model,
    or an anthropic.types.Usage. Fields we want may or may not exist
    depending on whether prompt caching kicked in. Missing fields
    become 0 so aggregation stays well-defined."""
    u = getattr(msg, "usage", None)
    if u is None:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    def _get(name: str) -> int:
        v = None
        if isinstance(u, dict):
            v = u.get(name)
        else:
            v = getattr(u, name, None)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0
    return {
        "input": _get("input_tokens"),
        "output": _get("output_tokens"),
        "cache_read": _get("cache_read_input_tokens"),
        "cache_creation": _get("cache_creation_input_tokens"),
    }


# Per-model context window in tokens. Used by the auto-compact check
# and the pane ContextBar. Seeded from known-at-build-time values for
# current Claude models, with a conservative observed fallback for
# unknown future model ids. Do not infer windows from ResultMessage
# usage: that usage is aggregated across all API calls in a turn and
# can exceed the real model window during tool-heavy turns.
_CONTEXT_WINDOWS = {
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-7[1m]": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-4-6[1m]": 1_000_000,
    "claude-haiku-4-5-20251001": 200_000,
}

# Observed ceilings learned from one assistant API call's prompt usage.
# Ratchets upward only. Persisted to team_config on every bump so a
# restart doesn't lose the learning. In-memory cache mirrors the DB
# row; refreshed at startup via _load_observed_windows().
_OBSERVED_CONTEXT_WINDOWS: dict[str, int] = {}


async def _load_observed_windows() -> None:
    """Seed _OBSERVED_CONTEXT_WINDOWS from team_config on startup.
    Called from the lifespan hook so reloads don't relearn from turn 1."""
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT value FROM team_config WHERE key = 'context_observed'"
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("context: observed-windows load failed")
        return
    if not row:
        return
    try:
        parsed = json.loads(dict(row).get("value") or "{}")
    except Exception:
        return
    if isinstance(parsed, dict):
        for k, v in parsed.items():
            if isinstance(k, str) and isinstance(v, int) and v > 0:
                table = _CONTEXT_WINDOWS.get(k, 0)
                # Older builds learned this value from ResultMessage.usage,
                # which is cumulative billing usage for the whole turn, not
                # a single prompt size. Discard obviously impossible legacy
                # values so one long tool turn doesn't permanently shrink the
                # pane percentage by pretending a model has a multi-million
                # token context window.
                if table and v > int(table * 1.05):
                    continue
                if not table and v > 2_000_000:
                    continue
                _OBSERVED_CONTEXT_WINDOWS[k] = v


async def _persist_observed_windows() -> None:
    """Write the in-memory observed map to team_config. Called after
    any bump — small JSON, infrequent writes, no need to batch."""
    payload = json.dumps(_OBSERVED_CONTEXT_WINDOWS, ensure_ascii=False)
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO team_config (key, value) VALUES "
                "('context_observed', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (payload,),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("context: observed-windows persist failed")


async def _observe_context_usage(
    model: str | None,
    latest_prompt_tokens: int | None,
) -> None:
    """Ratchet the observed window upward from one API call's prompt.

    The caller must pass the latest per-assistant usage from the
    session jsonl, not ResultMessage.usage. ResultMessage.usage is a
    billing aggregate over every tool round in the turn and is not a
    valid context-window observation.
    """
    if not model or not latest_prompt_tokens:
        return
    try:
        prompt_tokens = int(latest_prompt_tokens)
    except (TypeError, ValueError):
        return
    if prompt_tokens <= 0:
        return
    current = max(
        _CONTEXT_WINDOWS.get(model, 0),
        _OBSERVED_CONTEXT_WINDOWS.get(model, 0),
    )
    if prompt_tokens > current:
        _OBSERVED_CONTEXT_WINDOWS[model] = prompt_tokens
        logger.info(
            "context: learned %s window >= %d (was %d)",
            model, prompt_tokens, current,
        )
        await _persist_observed_windows()


def _context_window_for(model: str | None) -> int:
    """Resolve the context window for a model id.

    Precedence: observed-max > table > 1M fallback. The observed map
    ratchets upward, so a new model shipping with a bigger window
    than we assumed self-corrects after one real turn. For unknown
    smaller-than-1M models, the fallback over-reports — acceptable
    because the compact threshold then fires later than ideal rather
    than prematurely (premature compact is the worse failure)."""
    if not model:
        return max(
            1_000_000,
            max(_OBSERVED_CONTEXT_WINDOWS.values(), default=0),
        )
    observed = _OBSERVED_CONTEXT_WINDOWS.get(model, 0)
    table = _CONTEXT_WINDOWS.get(model, 0)
    resolved = max(observed, table)
    return resolved if resolved > 0 else 1_000_000


def _usage_int(usage: Any, name: str) -> int:
    """Read one token field from a dict/Pydantic-ish usage object."""
    if usage is None:
        return 0
    if isinstance(usage, dict):
        v = usage.get(name)
    else:
        v = getattr(usage, name, None)
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _prompt_tokens_from_usage(usage: Any) -> int:
    """Tokens in one API request's input context."""
    return (
        _usage_int(usage, "input_tokens")
        + _usage_int(usage, "cache_read_input_tokens")
        + _usage_int(usage, "cache_creation_input_tokens")
    )


def _count_message_chars(obj: Any) -> int:
    """Walk a parsed jsonl message object and sum the length of its
    text-bearing fields: `text`, `content`, `input`, `result`,
    `output`, `message`. Recurse into lists / dicts so a nested tool
    result or assistant content-block list still contributes.
    Everything else (metadata, timestamps, ids) is ignored."""
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj)
    if isinstance(obj, list):
        return sum(_count_message_chars(x) for x in obj)
    if isinstance(obj, dict):
        total = 0
        for k in ("text", "content", "input", "result", "output", "message"):
            if k in obj:
                total += _count_message_chars(obj[k])
        return total
    return 0


def _session_context_metrics_from_jsonl(jsonl_path: Path) -> tuple[int, int | None]:
    """Return (current_context_tokens, latest_prompt_tokens).

    The latest assistant `message.usage` row is the best signal we
    have: it is one API request's prompt size, split across uncached
    input + cache read + cache creation tokens. That avoids both bad
    approximations:
      - ResultMessage.usage overcounts by aggregating every tool round.
      - chars/4 undercounts cached prompts and tool definitions badly.

    We add the latest assistant output tokens because those tokens will
    be part of the next resumed prompt. If a turn stopped after a tool
    result without a follow-up assistant message, add a small chars/4
    tail fallback for messages after the latest usage row.
    """
    fallback_chars = 0
    latest_prompt_tokens: int | None = None
    latest_context_tokens: int | None = None
    tail_chars_after_latest_usage = 0
    have_latest_usage = False

    with jsonl_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                line_chars = len(s)
                fallback_chars += line_chars
                if have_latest_usage:
                    tail_chars_after_latest_usage += line_chars
                continue

            line_chars = _count_message_chars(obj)
            fallback_chars += line_chars
            if have_latest_usage:
                tail_chars_after_latest_usage += line_chars

            msg = obj.get("message") if isinstance(obj, dict) else None
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            prompt_tokens = _prompt_tokens_from_usage(usage)
            if prompt_tokens <= 0:
                continue

            latest_prompt_tokens = prompt_tokens
            latest_context_tokens = prompt_tokens + _usage_int(
                usage, "output_tokens"
            )
            tail_chars_after_latest_usage = 0
            have_latest_usage = True

    if latest_context_tokens is not None:
        return (
            latest_context_tokens + (tail_chars_after_latest_usage // 4),
            latest_prompt_tokens,
        )
    return fallback_chars // 4, None


async def _session_context_metrics(session_id: str) -> tuple[int, int | None]:
    """Estimate current context from Claude Code's session jsonl.

    Returns (used_tokens, latest_prompt_tokens). Both are 0/None when
    the jsonl can't be found (fresh session, compact just ran,
    CLAUDE_CONFIG_DIR unset in dev).
    """
    if not session_id:
        return 0, None
    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if not claude_dir:
        return 0, None
    projects_root = Path(claude_dir) / "projects"
    if not projects_root.is_dir():
        return 0, None
    try:
        jsonl_path: Path | None = None
        # Sessions are sharded by encoded-cwd sub-dirs; one session_id
        # is unique across the tree, so the first match is the file.
        for p in projects_root.rglob(f"{session_id}.jsonl"):
            jsonl_path = p
            break
        if jsonl_path is None:
            return 0, None
        return _session_context_metrics_from_jsonl(jsonl_path)
    except Exception:
        logger.exception("session_context_estimate: jsonl parse failed")
        return 0, None


async def _session_context_estimate(session_id: str) -> int:
    """Return estimated tokens already occupied by a resumed session."""
    used, _latest_prompt = await _session_context_metrics(session_id)
    return used


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
                if turn_ctx is not None:
                    # Compact-mode accumulator: the final assistant text
                    # becomes the continuity note. Multi-block responses
                    # coalesce via append.
                    if turn_ctx.get("compact_mode"):
                        prev = turn_ctx.get("compact_text") or ""
                        turn_ctx["compact_text"] = (
                            prev + ("\n\n" if prev else "") + (block.text or "")
                        )
                    else:
                        # Non-compact: also accumulate so we can
                        # snapshot this turn's full assistant reply into
                        # agents.last_exchange_json after the
                        # ResultMessage lands. Preserves the "most recent
                        # exchange" verbatim for the NEXT compact to
                        # quote, CLI-/compact style.
                        prev = turn_ctx.get("response_text") or ""
                        turn_ctx["response_text"] = (
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
        subtype = getattr(msg, "subtype", None)
        # SDK exposes a structured `errors` list on ResultMessage (one
        # entry per per-step failure). Stringify defensively — shape
        # varies across SDK versions and we never want a render crash
        # to hide the result line.
        errors_raw = getattr(msg, "errors", None) or []
        errors_summary: list[str] = []
        try:
            for err in errors_raw[:3]:  # cap noise — first 3 is plenty.
                if isinstance(err, str):
                    errors_summary.append(err[:300])
                elif isinstance(err, dict):
                    msg_field = err.get("message") or err.get("error") or str(err)
                    errors_summary.append(str(msg_field)[:300])
                else:
                    errors_summary.append(str(err)[:300])
        except Exception:
            errors_summary = []
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
            stop_reason=str(stop_reason) if stop_reason is not None else None,
            subtype=str(subtype) if subtype is not None else None,
            num_turns=num_turns,
            errors=errors_summary or None,
        )
        # Stash the error info so the NEXT spawn can prepend a system-
        # prompt note and the agent stops confabulating reasons for
        # the prior failure ("the harness paused me", etc.). Compact
        # turns are internal — leaking their error into the user-
        # facing turn would confuse the user, so skip them. Cleared
        # on the next spawn that consumes it (one-shot).
        if msg.is_error and not (turn_ctx and turn_ctx.get("compact_mode")):
            _last_turn_error_info[agent_id] = {
                "stop_reason": str(stop_reason) if stop_reason else None,
                "subtype": str(subtype) if subtype else None,
                "num_turns": num_turns,
                "duration_ms": duration_ms,
                "errors": errors_summary or [],
            }
        elif not msg.is_error:
            # Clean turn — clear any stale entry so the next prompt
            # doesn't see an obsolete "your prior turn errored" note.
            _last_turn_error_info.pop(agent_id, None)
            _consecutive_auto_continues.pop(agent_id, None)
        await _add_cost(agent_id, cost)
        # Player soft-error → DM Coach so the workflow doesn't hang on
        # a silent failure. A ResultMessage with is_error=True came
        # back fine from the SDK but the turn itself reports failure
        # (max_turns, max_budget, error_during_execution, etc.) — this
        # does NOT trigger auto-retry (that path is for hard errors
        # before ResultMessage), so Coach is the only one who can
        # react. Skip Coach's own errors (nothing to notify) and
        # compact turns (internal). Debounced per-agent.
        if (
            msg.is_error
            and agent_id != "coach"
            and agent_id != "system"
            and (turn_ctx is None or not turn_ctx.get("compact_mode"))
        ):
            now_m = time.monotonic()
            last = _last_error_dm_to_coach.get(agent_id, 0.0)
            if now_m - last >= ERROR_DM_DEBOUNCE_SECONDS:
                _last_error_dm_to_coach[agent_id] = now_m
                reason = str(stop_reason or "error")
                await _deliver_system_message(
                    from_id=agent_id,
                    to_id="coach",
                    subject=f"{agent_id} turn errored ({reason})",
                    body=(
                        f"My last turn ended with is_error=True "
                        f"(stop_reason={reason}, duration="
                        f"{int((duration_ms or 0) / 1000)}s). I won't "
                        f"auto-retry — soft errors don't trigger the "
                        f"retry loop. Decide whether to re-prompt me, "
                        f"reassign, or investigate via /api/events."
                    ),
                    priority="normal",
                )
        # Auto-continue on max-turns hit. Distinct from the soft-error
        # DM-to-Coach above (which routes a Player's hard failure
        # through Coach) — this fires for *any* agent (Coach included)
        # when the SDK cut the turn off mid-task instead of letting
        # it finish. Capped via _consecutive_auto_continues so a
        # genuinely stuck workflow escalates to the human rather than
        # looping forever and chewing the cost cap. Skip compact
        # turns (internal).
        if (
            msg.is_error
            and (turn_ctx is None or not turn_ctx.get("compact_mode"))
            and _looks_like_max_turns(subtype, stop_reason)
        ):
            await _maybe_schedule_auto_continue(
                agent_id=agent_id,
                subtype=str(subtype) if subtype else None,
                stop_reason=str(stop_reason) if stop_reason else None,
                num_turns=num_turns,
            )
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
                # Auto-appended footer pointing at the session jsonl +
                # the handoff file. The model's summary is lossy by
                # design; this footer tells fresh-you where to find the
                # full record if they need exact strings. session_id is
                # known here (captured off the ResultMessage above).
                footer = _build_compact_footer(session_id)
                summary_with_footer = summary.rstrip() + "\n\n" + footer
                # Full long-form handoff lives on disk (kDrive-mirrored).
                # The continuity_note stored in the DB is a short pointer
                # that gets injected into the next system prompt.
                handoff_file = await _write_handoff_file(
                    agent_id, summary_with_footer,
                )
                # The summary itself is what gets injected into fresh-you's
                # system prompt. The file is a durable copy (kDrive-
                # mirrored) and lets other agents / the human reference
                # this handoff later via ./handoffs/<file> from any
                # workspace. No need to re-read it yourself — the text
                # below is already in-context.
                if handoff_file:
                    pointer = (
                        f"_This handoff is also saved to "
                        f"handoffs/{handoff_file} for audit + cross-agent "
                        f"reference; the text below is the full content._"
                        f"\n\n"
                    ) + summary_with_footer
                else:
                    pointer = summary_with_footer
                await _set_continuity_note(agent_id, pointer)
                await _set_session_id(agent_id, None)
                # Freeze the exchange log we just quoted into the
                # handoff; the next session starts with an empty buffer
                # so a later compact doesn't re-quote pre-compact turns.
                await _clear_exchange_log(agent_id)
                await _emit(
                    agent_id,
                    "session_compacted",
                    chars=len(summary),
                    handoff_file=handoff_file,
                )
            else:
                # Model produced no text despite being asked to summarize.
                # If this was an AUTO-compact (triggered because context
                # was over threshold), keeping the same session_id means
                # the very next turn will trip the same threshold and
                # loop forever. Force-clear and flag it so the next turn
                # at least starts fresh — lost continuity beats deadlock.
                # Manual /compact leaves the session intact so the user
                # can retry without losing state.
                if turn_ctx.get("auto_compact"):
                    await _set_session_id(agent_id, None)
                    await _clear_exchange_log(agent_id)
                    await _emit(
                        agent_id,
                        "compact_empty_forced",
                        reason=(
                            "auto-compact turn produced no summary; "
                            "session cleared to escape threshold loop"
                        ),
                    )
                else:
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
            # Snapshot THIS turn's (prompt, response) pair for the next
            # compact to preserve verbatim. Skipped on error / compact
            # turns / no text — we only want real user-facing exchanges.
            if (
                turn_ctx is not None
                and not turn_ctx.get("compact_mode")
                and not msg.is_error
            ):
                response_text = (turn_ctx.get("response_text") or "").strip()
                entry_prompt = (turn_ctx.get("entry_prompt") or "").strip()
                if response_text and entry_prompt:
                    await _append_exchange(agent_id, entry_prompt, response_text)
        usage = _extract_usage(msg)
        if turn_ctx is not None:
            # Self-adapting window estimate: if this turn read more
            # tokens in one API call than our table says the model's
            # window is, bump our stored value. ResultMessage.usage is
            # aggregate billing usage, so the observation comes from
            # the latest assistant usage row in the session jsonl.
            if session_id:
                _, latest_prompt_tokens = await _session_context_metrics(
                    session_id
                )
            else:
                latest_prompt_tokens = None
            await _observe_context_usage(
                turn_ctx.get("model"),
                latest_prompt_tokens,
            )
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
                input_tokens=usage["input"],
                output_tokens=usage["output"],
                cache_read_tokens=usage["cache_read"],
                cache_creation_tokens=usage["cache_creation"],
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

# The compact prompt used by both the manual /compact endpoint and the
# auto-compact trip-wire. The handoff is written to
# /data/handoffs/<agent>-<ts>.md (and mirrored to WebDAV) so a future
# instance of this agent can Read() the full file on demand — the
# inline continuity note injected into the next system prompt is only
# a short pointer. That means the handoff itself CAN and SHOULD be
# long: the cost is one file read, not recurring prompt bloat.
COMPACT_PROMPT = (
    "Your session context is about to be cleared. Write a DETAILED "
    "handoff document for the fresh-session version of yourself who "
    "will pick up after this compact. That future-you has NO memory "
    "of anything we just did — treat them as a well-briefed colleague "
    "who joined the project today. The most recent exchanges are "
    "preserved verbatim separately; this document covers EVERYTHING "
    "ELSE from the session.\n\n"
    "Target length: 1500-3000 words. Err on the side of MORE detail "
    "rather than less — this file will be saved to disk, not replayed "
    "in every prompt, so length is cheap and missing context is "
    "expensive. Do not abbreviate. Do not write 'see above' or 'as "
    "discussed' — spell it out.\n\n"
    "Use exactly these markdown sections, in this order. Within each "
    "section, use sub-headings, bullet lists, and code blocks freely. "
    "If a section genuinely doesn't apply, write 'None this session.' "
    "rather than omitting it — fresh-you needs to know you considered "
    "it.\n\n"
    "## Current task\n"
    "What you are actively working on, who asked for it, the state "
    "right now (investigating / blocked / implementing / reviewing / "
    "…), and the next 1-3 concrete steps you were about to take. "
    "Include the original goal as it was stated to you, verbatim if "
    "possible.\n\n"
    "## How we got here\n"
    "Chronological narrative of the session's arc: what was tried, "
    "what worked, what didn't, dead ends you ruled out and why. "
    "Fresh-you reading this should understand the path of reasoning, "
    "not just the final state. Include the PURPOSE this session serves "
    "in the operator's broader work (not just what we did, but WHY "
    "they need it). If this session has been running inside a recurring "
    "workflow pattern (stepping through a list, iterating on revisions, "
    "babysitting a long job), describe that pattern explicitly so "
    "fresh-you knows whether to resume in the same mode. 2-5 "
    "paragraphs.\n\n"
    "## Open questions\n"
    "Everything still undecided or pending clarification. Quote each "
    "question VERBATIM as asked (by the human or by you to them). "
    "Include enough surrounding context that fresh-you can answer "
    "without asking 'wait, which X?'.\n\n"
    "## Key findings & decisions\n"
    "Facts established, decisions made, and conclusions reached this "
    "session that aren't already in memory/ or decisions/. For each: "
    "what, why, and who agreed. These are the things that would be "
    "LOST if this handoff fails.\n\n"
    "## References\n"
    "URLs fetched, file paths, exact error messages, commit hashes, "
    "command output snippets, code fragments.\n\n"
    "**Graduated detail**: the ~5 most-recent / most-relevant items "
    "get verbatim code blocks or quoted strings (paraphrase destroys "
    "these). Older references get one line each — just enough that "
    "fresh-you can locate them. Don't flatten everything to verbatim "
    "— the goal is that recent work is replayable and old work is "
    "findable.\n\n"
    "**Inherited vs. changed**: when listing files, tag each as "
    "**touched** (you modified it this session) or **read-only** "
    "(you only read it — pre-existing state you inherited). This "
    "lets fresh-you tell their own work apart from what was already "
    "there.\n\n"
    "## People & roles\n"
    "Who participated, what each person (or agent) is responsible "
    "for, any stated preferences or pet-peeves that came up, and "
    "anyone fresh-you should reach out to first.\n\n"
    "## Context quirks & gotchas\n"
    "Anything peculiar about the current setup — tools that "
    "misbehaved, assumptions we agreed to, user preferences mentioned "
    "in passing, environmental weirdness. One paragraph each.\n\n"
    "## What fresh-you should do first\n"
    "Concrete first actions: 'read file X', 'check Y', 'resume by "
    "asking Z'. Write this as if you are leaving voicemail for "
    "tomorrow-morning-you.\n\n"
    "Reply with ONLY the markdown — no preamble, no sign-off, no "
    "'Here is the handoff:'."
)


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


# Independent "repeat" loop: same shape as the tick loop but with an
# arbitrary caller-supplied prompt. Driven by /repeat slash command
# (POST /api/coach/repeat). Coach-only. Runs in parallel with the
# routine tick loop — both can fire independently, coach_is_working
# guard prevents actual concurrent turns.
_coach_repeat_interval: int = 0
_coach_repeat_prompt: str | None = None


def get_coach_repeat() -> tuple[int, str | None]:
    return _coach_repeat_interval, _coach_repeat_prompt


def set_coach_repeat(seconds: int, prompt: str | None) -> None:
    """Set the Coach repeat-loop cadence + prompt at runtime. Passing
    seconds=0 disables the loop (prompt is cleared). Changes take
    effect on the next iteration of coach_repeat_loop."""
    global _coach_repeat_interval, _coach_repeat_prompt
    _coach_repeat_interval = max(0, int(seconds))
    _coach_repeat_prompt = (prompt or "").strip() or None
    if _coach_repeat_interval <= 0:
        _coach_repeat_prompt = None
    logger.info(
        "coach repeat loop set: %ds, prompt=%r",
        _coach_repeat_interval,
        (_coach_repeat_prompt or "")[:60],
    )


# Auto-wake: when Coach assigns a task to p3 or messages p3, we start
# a turn for p3 with a wake prompt so the Player actually engages —
# without this, assignments just sit in the DB doing nothing. Debounce
# prevents tight ping-pong loops: if an agent finished a turn within
# AUTOWAKE_DEBOUNCE_SECONDS, skip. Independent of the Coach autoloop.
AUTOWAKE_DEBOUNCE_SECONDS = int(
    os.environ.get("HARNESS_AUTOWAKE_DEBOUNCE", "10")
)
_last_turn_ended_at: dict[str, float] = {}

# Post-error auto-retry tracking. After a hard error (not the
# suppressed post-result teardown variety), we schedule a single wake
# after HARNESS_ERROR_RETRY_DELAY seconds so the agent doesn't sit
# idle if no external event happens to nudge it. _retry_pending marks
# agents whose retry task is already queued — prevents stacking
# multiple retries on a burst of errors. _consecutive_errors counts
# errors-without-a-successful-turn-in-between; after
# HARNESS_ERROR_RETRY_MAX_CONSECUTIVE (default 3) we escalate to the
# human instead of retrying forever and chewing the cost cap.
_retry_pending: set[str] = set()
_consecutive_errors: dict[str, int] = {}
ERROR_RETRY_DELAY_SECONDS = int(
    os.environ.get("HARNESS_ERROR_RETRY_DELAY", "45")
)
ERROR_RETRY_MAX_CONSECUTIVE = int(
    os.environ.get("HARNESS_ERROR_RETRY_MAX_CONSECUTIVE", "3")
)

# SDK ClaudeAgentOptions.max_turns — the per-spawn ceiling on the
# model's tool-use roundtrips. The SDK terminates with is_error=True
# / subtype="error_max_turns" when this trips, even if the agent
# would otherwise have finished cleanly. The previous default of 10
# was too tight for an autonomous Coach (read inbox + list tasks +
# plan + write a TodoWrite already burns most of it). 50 leaves
# plenty of headroom for ordinary workflows; the daily cost cap
# remains the brake against a runaway turn.
MAX_TURNS_PER_SPAWN = int(os.environ.get("HARNESS_MAX_TURNS", "50"))

# Soft-error tracking — last-turn diagnostics surfaced into the next
# spawn's system prompt so a follow-up turn knows why the previous
# one ended with is_error=True. Cleared after the next successful
# spawn consumes it (one-shot — we don't want the note to haunt
# every future turn). In-memory only: a process restart wipes it,
# which is fine because a restart implies a fresh CLI session anyway.
_last_turn_error_info: dict[str, dict[str, Any]] = {}

# Max-turns auto-continue. When a turn ends with is_error=True AND
# the SDK indicates the cause was max_turns (subtype="error_max_turns"
# or stop_reason in {"max_turns", "max_tokens"}), the agent was very
# likely cut off mid-task rather than failing — schedule a follow-up
# turn that prompts it to continue. Capped per-agent so a genuinely
# stuck task (every continuation also hits the limit) escalates to
# the human via human_attention instead of looping forever.
AUTO_CONTINUE_DELAY_SECONDS = int(
    os.environ.get("HARNESS_AUTO_CONTINUE_DELAY", "5")
)
AUTO_CONTINUE_MAX_CONSECUTIVE = int(
    os.environ.get("HARNESS_AUTO_CONTINUE_MAX_CONSECUTIVE", "2")
)
# Per-agent counter — number of consecutive max-turns auto-continues
# without an intervening clean turn. Reset to 0 by any non-error
# result; bumped on each auto-continue trigger.
_consecutive_auto_continues: dict[str, int] = {}
# Per-agent set: agents with a pending auto-continue task already
# queued. Prevents stacking multiple continuations on a burst of
# events (the result event fires once but other code might also try
# to react).
_auto_continue_pending: set[str] = set()

# Per-agent debounce for "Player errored → DM Coach" notifications.
# Without this, a burst of retries spams Coach's inbox with 3×
# near-identical messages. Map: agent_id → monotonic ts of last DM.
_last_error_dm_to_coach: dict[str, float] = {}
ERROR_DM_DEBOUNCE_SECONDS = int(
    os.environ.get("HARNESS_ERROR_DM_DEBOUNCE", "300")
)


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


async def _get_agent_identity(agent_id: str) -> dict[str, str | None]:
    """Read (name, role, brief) from agent_project_roles for the
    active project. Returns {} for system. Missing row → all None.
    """
    if agent_id == "system":
        return {}
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT name, role, brief FROM agent_project_roles "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("get_agent_identity failed: agent=%s", agent_id)
        return {}
    if not row:
        return {"name": None, "role": None, "brief": None}
    d = dict(row)
    return {"name": d.get("name"), "role": d.get("role"), "brief": d.get("brief")}


async def _ensure_session_row(c: Any, agent_id: str, project_id: str) -> None:
    """INSERT OR IGNORE a row in agent_sessions so subsequent UPDATEs
    have a target. Cheap — primary key is (slot, project_id)."""
    await c.execute(
        "INSERT OR IGNORE INTO agent_sessions (slot, project_id) VALUES (?, ?)",
        (agent_id, project_id),
    )


async def _get_session_id(agent_id: str) -> str | None:
    """Read agent.session_id (from the last turn's ResultMessage).
    None when the agent has never run, or DELETE /api/agents/<id>/session
    has cleared it for a fresh-context restart."""
    if agent_id == "system":
        return None
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT session_id FROM agent_sessions "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
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
    """Read brief from agent_project_roles for the active project."""
    ident = await _get_agent_identity(agent_id)
    v = ident.get("brief") if ident else None
    return v if v else None


async def _get_continuity_note(agent_id: str) -> str | None:
    """Read agent_sessions.continuity_note for the active (slot, project)."""
    if agent_id == "system":
        return None
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT continuity_note FROM agent_sessions "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
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
    """Write (or clear) the continuity note for the active project."""
    if agent_id == "system":
        return
    payload = (text or "").strip() or None
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        await _ensure_session_row(c, agent_id, project_id)
        await c.execute(
            "UPDATE agent_sessions SET continuity_note = ? "
            "WHERE slot = ? AND project_id = ?",
            (payload, agent_id, project_id),
        )
        await c.commit()
    finally:
        await c.close()


def _build_compact_footer(session_id: str | None) -> str:
    """Render the 'Where to find more' footer appended to every compact
    summary. The model's summary is lossy by design — this footer
    points fresh-you at the authoritative record if they need exact
    strings / code / URLs the summary smoothed over.

    Paths:
    - The handoff .md file itself (kDrive-mirrored when configured).
    - The CLI session jsonl under CLAUDE_CONFIG_DIR/projects/, by
      session id. We name the id explicitly so the operator can find
      the file without having to replay any cwd-encoding logic.
    """
    try:
        retention = int(os.environ.get("HARNESS_SESSION_RETENTION_DAYS", "30"))
    except ValueError:
        retention = 30
    if retention <= 0:
        retention_note = "kept indefinitely (retention disabled)"
    else:
        retention_note = f"auto-pruned after {retention} days"

    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")
    if session_id:
        jsonl_hint = (
            f"`{claude_dir}/projects/<encoded-cwd>/{session_id}.jsonl`"
        )
    else:
        jsonl_hint = (
            f"`{claude_dir}/projects/<encoded-cwd>/<session-id>.jsonl` "
            "(session id was not captured for this turn)"
        )

    return (
        "---\n"
        "## Where to find more _(auto-appended by the harness)_\n\n"
        "The summary above is lossy by design. If you need exact "
        "strings, tool outputs, URLs, or code that isn't preserved "
        "below, the full record exists:\n\n"
        f"- **Full session transcript** (every message, tool call, "
        f"response): {jsonl_hint}. {retention_note.capitalize()}. "
        "Ask the operator to surface it if you can't read it directly "
        "from your workspace.\n"
        "- **This handoff itself** is saved at `handoffs/<agent>-"
        "<timestamp>.md` (the exact filename is in the one-line "
        "pointer above, if any)."
    )


async def _write_handoff_file(agent_id: str, summary: str) -> str | None:
    """Persist the full compact summary as a markdown file under
    HANDOFFS_DIR (kDrive-mirrored when available) and return the
    relative filename. The continuity_note stored in the DB is only a
    pointer — this file is the authoritative, long-form handoff that
    fresh-you can Read() on demand.

    Filename: <agent_id>-<YYYYMMDD-HHMMSS>.md. Returns None if both
    kDrive and the local write failed — we log but don't raise,
    because an empty handoff doesn't justify losing the compact turn
    itself."""
    ts_utc = datetime.now(timezone.utc)
    stamp = ts_utc.strftime("%Y%m%d-%H%M%S")
    filename = f"{agent_id}-{stamp}.md"
    frontmatter = (
        f"---\n"
        f"agent: {agent_id}\n"
        f"ts: {ts_utc.isoformat()}\n"
        f"kind: compact-handoff\n"
        f"---\n\n"
    )
    content = frontmatter + summary.strip() + "\n"

    project_id = await resolve_active_project()
    wrote_webdav = False
    if webdav.enabled:
        try:
            wrote_webdav = bool(await webdav.write_text(
                f"projects/{project_id}/working/handoffs/{filename}", content
            ))
        except Exception:
            logger.exception("handoff kDrive write failed: %s", filename)
            wrote_webdav = False

    from server.paths import project_paths
    local_dir = project_paths(project_id).working_handoffs
    try:
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / filename).write_text(content, encoding="utf-8")
        return filename
    except Exception:
        logger.exception("handoff local write failed: %s", filename)
        return filename if wrote_webdav else None


async def _get_recent_exchanges(agent_id: str) -> list[dict[str, str]]:
    """Read the rolling list of recent (prompt, response) pairs.

    Stored as a JSON array in agents.last_exchange_json. Returns [] on
    missing / unparseable. Defensively accepts the legacy single-dict
    shape that earlier builds wrote, promoting it to a one-element
    list so a mid-deploy upgrade doesn't lose the last exchange.
    """
    if agent_id == "system":
        return []
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT last_exchange_json FROM agent_sessions "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        return []
    if not row:
        return []
    raw = dict(row).get("last_exchange_json")
    if not raw:
        return []
    try:
        d = json.loads(raw)
    except Exception:
        return []
    if isinstance(d, dict):
        return [d]
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    return []


# Token budget for the rolling exchange log preserved verbatim across
# a compact. Older exchanges are trimmed from the head until the total
# fits. Default 20K tokens — enough for a warm-start on the post-
# compact turn without hoarding. The full session transcript is kept
# separately in the CLI's jsonl file (see session retention sweep in
# sync.py + the "Where to find more" footer in every handoff), so this
# log doesn't need to double as the long-term record. Token counts use
# a rough chars/4 estimate; tokenizer-exact would be overkill for a
# budget knob.
_CHARS_PER_TOKEN = 4


def _handoff_token_budget() -> int:
    try:
        n = int(os.environ.get("HARNESS_HANDOFF_TOKEN_BUDGET", "20000"))
    except ValueError:
        return 20_000
    if n < 1_000:
        return 1_000
    if n > 500_000:
        return 500_000
    return n


def _est_tokens(*parts: str) -> int:
    """Rough token count: sum of chars / 4. Defensive against None."""
    total = 0
    for p in parts:
        if p:
            total += len(p)
    return (total + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


async def _append_exchange(agent_id: str, prompt: str, response: str) -> None:
    """Push this turn's pair onto the rolling exchange log, trimming
    from the head so the total stays under HARNESS_HANDOFF_TOKEN_BUDGET
    tokens. Unlike the prior count-based cap, exchanges are kept at
    full length (no per-entry clip) — you get fewer-but-complete
    recent turns instead of many-but-truncated ones."""
    if agent_id == "system":
        return

    existing = await _get_recent_exchanges(agent_id)
    existing.append({
        "prompt": prompt or "",
        "response": response or "",
        "ts": _now(),
    })

    # Trim from the head until we fit the token budget. The newest
    # exchange is always kept — if a single exchange exceeds the
    # budget, we keep it alone rather than storing nothing.
    budget = _handoff_token_budget()
    while len(existing) > 1:
        total = sum(
            _est_tokens(e.get("prompt", ""), e.get("response", ""))
            for e in existing
        )
        if total <= budget:
            break
        existing.pop(0)

    payload = json.dumps(existing, ensure_ascii=False)
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            await _ensure_session_row(c, agent_id, project_id)
            await c.execute(
                "UPDATE agent_sessions SET last_exchange_json = ? "
                "WHERE slot = ? AND project_id = ?",
                (payload, agent_id, project_id),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("append_exchange failed: agent=%s", agent_id)


async def _clear_exchange_log(agent_id: str) -> None:
    """Null the exchange log. Called after compact commit so the FIRST
    fresh turn's exchange history starts clean — otherwise the second
    compact would retain exchanges from before the first, which is
    confusing and bloats the handoff."""
    if agent_id == "system":
        return
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE agent_sessions SET last_exchange_json = NULL "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        pass


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


async def _build_coach_coordination_block() -> str:
    """Phase 7 (PROJECTS_SPEC.md §10): Coach-only per-turn coordination
    block. Built fresh on every Coach turn from `projects`,
    `agent_project_roles`, `agents.locked`, `tasks`, `messages`, and
    the latest entry in `decisions/`.

    Returns the rendered markdown block, or "" on any read failure
    (Coach's turn is more valuable than getting this perfectly — fall
    back to a quieter prompt rather than crash the whole spawn).

    Layout matches the spec excerpt:
      ## Coordinating: <Project Name>
      Goal: ...
      ## Team composition (this project)
      ## Current state
        Open tasks, Inbox count, Last decision, Wiki paths
    """
    from server.paths import project_paths

    try:
        active = await resolve_active_project()
    except Exception:
        return ""

    # ---- Project name + goal ---------------------------------------
    project_name = active
    project_goal = ""
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT name, description FROM projects WHERE id = ?",
                (active,),
            )
            row = await cur.fetchone()
            if row:
                d = dict(row)
                project_name = d.get("name") or active
                project_goal = (d.get("description") or "").strip()

            # ---- Team composition (active project) -----------------
            cur = await c.execute(
                "SELECT id, locked FROM agents WHERE kind = 'player' "
                "ORDER BY id"
            )
            player_rows = [dict(r) for r in await cur.fetchall()]
            cur = await c.execute(
                "SELECT slot, name, role FROM agent_project_roles "
                "WHERE project_id = ?",
                (active,),
            )
            role_map = {dict(r)["slot"]: dict(r) for r in await cur.fetchall()}

            # ---- Open tasks ----------------------------------------
            cur = await c.execute(
                "SELECT id, title, status, owner "
                "FROM tasks WHERE project_id = ? "
                "AND status IN ('claimed', 'in_progress', 'open') "
                "ORDER BY CASE status "
                "  WHEN 'in_progress' THEN 0 "
                "  WHEN 'claimed' THEN 1 "
                "  ELSE 2 END, id ASC LIMIT 20",
                (active,),
            )
            open_tasks = [dict(r) for r in await cur.fetchall()]

            # ---- Coach inbox unread count --------------------------
            # Schema convention: messages have a `to_id` and the harness
            # tracks per-recipient reads in `message_reads`. Unread =
            # messages targeted at coach (not by coach) without a read
            # row, scoped to the active project.
            cur = await c.execute(
                """
                SELECT COUNT(*) AS n FROM messages m
                WHERE m.project_id = ?
                  AND m.to_id = 'coach'
                  AND m.from_id != 'coach'
                  AND NOT EXISTS (
                      SELECT 1 FROM message_reads r
                      WHERE r.message_id = m.id AND r.agent_id = 'coach'
                  )
                """,
                (active,),
            )
            row = await cur.fetchone()
            unread = int(dict(row)["n"]) if row else 0
        finally:
            await c.close()
    except Exception:
        return ""

    # ---- Last decision ---------------------------------------------
    last_decision_line = "(none yet)"
    try:
        pp = project_paths(active)
        dec_dir = pp.decisions
        if dec_dir.is_dir():
            files = sorted(
                dec_dir.glob("*.md"), key=lambda p: p.name, reverse=True
            )
            if files:
                latest = files[0]
                title = latest.stem
                try:
                    text = latest.read_text(encoding="utf-8")
                    if text.startswith("---\n"):
                        end = text.find("\n---\n", 4)
                        if end > 0:
                            for line in text[4:end].splitlines():
                                if line.startswith("title:"):
                                    title = line[len("title:"):].strip()
                                    break
                except OSError:
                    pass
                # Pull the date prefix from the filename if present
                # ("2026-04-23-foo.md" → "2026-04-23"); fall back to
                # the bare stem otherwise.
                date_part = latest.stem.split("-", 3)
                if (
                    len(date_part) >= 3
                    and date_part[0].isdigit()
                    and date_part[1].isdigit()
                    and date_part[2].isdigit()
                ):
                    date_str = "-".join(date_part[:3])
                else:
                    date_str = ""
                if date_str:
                    last_decision_line = (
                        f"{date_str} — {title}\n  ({latest})"
                    )
                else:
                    last_decision_line = f"{title}\n  ({latest})"
    except Exception:
        pass

    # ---- Render -----------------------------------------------------
    lines: list[str] = []
    lines.append(f"## Coordinating: {project_name}")
    lines.append("")
    if project_goal:
        lines.append(f"Goal: {project_goal}")
    else:
        lines.append("Goal: (not set — edit projects.description to fill)")
    pp = project_paths(active)
    lines.append(
        f"(For full project context, read {pp.claude_md} and update "
        "it as the project evolves.)"
    )
    lines.append("")

    lines.append("## Team composition (this project)")
    lines.append("")
    lines.append("- coach   — you")
    unassigned: list[str] = []
    locked_named: list[str] = []
    for prow in player_rows:
        slot = prow["id"]
        rec = role_map.get(slot)
        is_locked = bool(prow.get("locked"))
        if rec and (rec.get("name") or rec.get("role")):
            name = (rec.get("name") or "").strip() or "(unnamed)"
            role = (rec.get("role") or "").strip() or "(no role)"
            tag = " (LOCKED — unavailable)" if is_locked else ""
            lines.append(f"- {slot:<7} — {name:<14} | role: {role}{tag}")
            if is_locked:
                locked_named.append(slot)
        else:
            unassigned.append(slot)
            if is_locked:
                locked_named.append(slot)
    if unassigned:
        if len(unassigned) > 1:
            label = f"{unassigned[0]}..{unassigned[-1]}"
        else:
            label = unassigned[0]
        lines.append(
            f"- {label:<7} — unassigned (auto-name on first activation; "
            "assign via coord_set_player_role)"
        )
    lines.append("")

    # Phase 7 audit: "Roster availability" prose folded in as a
    # sub-section per spec §10. Inline LOCKED tags carry the fact;
    # this carries the explicit "do NOT assign / do NOT message /
    # broadcasts skip them" rule so Coach plans around it instead
    # of hitting the tool-layer rejection.
    if locked_named:
        lines.append("### Roster availability (right now)")
        lines.append("")
        lines.append(
            "The human has LOCKED the following Player(s): "
            + ", ".join(locked_named)
            + ". Do NOT assign tasks to them, do NOT direct-message "
            "them, and remember broadcasts also skip them. Work "
            "around this constraint — pick other Players or tell "
            "the human if no suitable unlocked Player remains."
        )
        lines.append("")

    lines.append("## Current state")
    lines.append("")
    if open_tasks:
        lines.append(f"Open tasks ({len(open_tasks)}):")
        for t in open_tasks:
            tid = t["id"]
            title = (t.get("title") or "").strip()[:80]
            status = t.get("status") or "?"
            owner = t.get("owner") or "—"
            lines.append(f"- {tid} ({status}) — {owner} — {title}")
    else:
        lines.append("Open tasks: (none)")
    lines.append("")
    lines.append(f"Inbox: {unread} unread message{'s' if unread != 1 else ''}")
    lines.append("")
    lines.append(f"Last decision: {last_decision_line}")
    lines.append("")
    from server.paths import global_paths
    gp = global_paths()
    lines.append(
        f"Wiki: {gp.wiki / active}/  (master index: {gp.wiki_index})"
    )

    return "\n".join(lines) + "\n"


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
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            await _ensure_session_row(c, agent_id, project_id)
            await c.execute(
                "UPDATE agent_sessions SET session_id = ?, last_active = ? "
                "WHERE slot = ? AND project_id = ?",
                (session_id, _now(), agent_id, project_id),
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
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE agent_sessions SET session_id = NULL "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
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

    project_id = await resolve_active_project()
    async with _AUTONAME_LOCK:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT kind FROM agents WHERE id = ?", (agent_id,)
            )
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
            if d["kind"] != "player":
                return None
            cur = await c.execute(
                "SELECT name FROM agent_project_roles "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
            )
            cur_row = await cur.fetchone()
            if cur_row and dict(cur_row).get("name"):
                return None
            cur = await c.execute(
                "SELECT name FROM agent_project_roles "
                "WHERE project_id = ? AND name IS NOT NULL",
                (project_id,),
            )
            taken = {dict(r)["name"] for r in await cur.fetchall()}
            candidates = [n for n in _LACROSSE_SURNAMES if n not in taken]
            if not candidates:
                return None
            pick = random.choice(candidates)
            # Upsert into agent_project_roles for the active project.
            await c.execute(
                "INSERT INTO agent_project_roles (slot, project_id, name) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(slot, project_id) DO UPDATE SET name = excluded.name",
                (agent_id, project_id, pick),
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
            "  - To edit team-wide rules: open /data/CLAUDE.md (global) or "
            "/data/projects/<active>/CLAUDE.md (this project) with the "
            "standard Write tool; both files are read fresh into every "
            "agent's system prompt at the next turn — no restart needed. "
            "(Coach-only by convention.)\n"
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
            "  - AskUserQuestion (built-in): ask the human a structured "
            "multiple-choice question. Your turn PAUSES until they submit "
            "the form in the UI, then resumes with the answers in the tool "
            "result — same turn continues. Prefer this over free-text asks "
            "when you have 2-4 concrete options.\n"
            "  - coord_answer_question(correlation_id, answers): resolve a "
            "Player's paused question. When a Player calls AskUserQuestion "
            "while working for you, the question lands in your inbox with "
            "a correlation_id — call this tool to unblock them.\n"
            "  - coord_answer_plan(correlation_id, decision, comments?): "
            "resolve a Player's paused ExitPlanMode. decision is "
            "'approve' | 'reject' | 'approve_with_comments'. Reject and "
            "approve_with_comments need a `comments` body.\n"
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
            "  - ./handoffs/<agent>-<timestamp>.md: compact-handoff files "
            "(auto-written when a session is compacted). Read when you "
            "need context across a compact boundary for any agent.\n"
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
        f"  - AskUserQuestion (built-in): ask Coach a structured multiple-"
        f"choice question (1-4 questions, 2-4 options each). Your turn "
        f"PAUSES until Coach resolves it via coord_answer_question, then "
        f"resumes with the answers in the tool result. The harness routes "
        f"AskUserQuestion to Coach by default — if you need the human "
        f"directly, escalate via coord_request_human instead.\n"
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
        f"  - ./handoffs/<agent>-<timestamp>.md (symlinked in your "
        f"workspace): full compact-handoff files. Auto-written when a "
        f"session is compacted; read them if you need to know what "
        f"another agent was doing across a compact boundary.\n"
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


async def _wake_after_turn_for_plan_comments(
    agent_id: str,
    current_turn_task: asyncio.Task[Any],
) -> None:
    """Fire-and-forget: await the currently-running turn, then wake the
    agent so it reads the freshly-queued plan-comment inbox message.
    Called from the approve_with_comments path of ExitPlanMode handling.

    Without this, maybe_wake_agent would no-op (slot is busy during the
    plan-execution turn) and the comments would sit unread until some
    other external event nudged the agent. Awaiting the current task
    cleanly defers the wake to post-turn without polling."""
    try:
        await current_turn_task
    except (asyncio.CancelledError, Exception):
        # Even if the plan-execution turn errored/was cancelled, we
        # still want the comments surfaced on the next live turn.
        pass
    try:
        await maybe_wake_agent(
            agent_id,
            "The operator left notes on the approved plan. Read your "
            "inbox — the most recent message from 'human' carries "
            "notes to keep in mind going forward.",
            bypass_debounce=True,
        )
    except Exception:
        logger.exception(
            "post-turn wake for plan comments failed for %s", agent_id,
        )


async def _pretool_continue_hook(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """SDK workaround: can_use_tool needs streaming mode + a PreToolUse
    hook that returns continue_ to keep the stream open. Without this
    the stream closes before the permission callback fires. Doesn't
    modify tool behavior — just keeps the channel alive."""
    return {"continue_": True}


def _normalize_question_payload(input_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Take AskUserQuestion's input (may be dict[{"questions": [...]}]
    OR already just a list) and return a list of normalised question
    dicts. Tolerates minor SDK shape drift."""
    raw = input_data.get("questions") if isinstance(input_data, dict) else input_data
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for q in raw:
        if not isinstance(q, dict):
            continue
        opts = q.get("options") or []
        clean_opts: list[dict[str, str]] = []
        for o in opts:
            if not isinstance(o, dict):
                continue
            label = str(o.get("label") or "").strip()
            if not label:
                continue
            clean_opts.append({
                "label": label,
                "description": str(o.get("description") or ""),
            })
        out.append({
            "question": str(q.get("question") or "").strip(),
            "header": str(q.get("header") or "").strip(),
            # SDK uses multiSelect, Coach's answer_question uses
            # multi_select — keep both for forward-compat.
            "multi_select": bool(q.get("multiSelect") or q.get("multi_select")),
            "options": clean_opts,
        })
    return out


def _format_questions_md(questions: list[dict[str, Any]]) -> str:
    """Human-readable version of the question set, for inbox bodies."""
    lines: list[str] = []
    for i, q in enumerate(questions, 1):
        head = f"### {q['header']}\n" if q.get("header") else ""
        multi = " _(multi-select)_" if q.get("multi_select") else ""
        lines.append(f"{head}**Q{i}: {q['question']}**{multi}")
        for j, o in enumerate(q.get("options") or [], 1):
            letter = chr(ord("A") + j - 1)
            desc = f" — {o['description']}" if o.get("description") else ""
            lines.append(f"- **{letter}) {o['label']}**{desc}")
        lines.append("")
    return "\n".join(lines).strip()


def _build_can_use_tool(agent_id: str):
    """Return a can_use_tool callback closed over the calling agent_id.
    Intercepts AskUserQuestion and routes by role; everything else is
    auto-approved (our allowed_tools list is exhaustive, so anything
    that reaches here via some future SDK change gets a permissive
    pass rather than a false deny)."""
    caller_is_coach = agent_id == "coach"

    async def _cb(
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name == "AskUserQuestion":
            return await _handle_ask_user_question(agent_id, caller_is_coach, input_data)
        if tool_name == "ExitPlanMode":
            return await _handle_exit_plan_mode(agent_id, caller_is_coach, input_data)
        return PermissionResultAllow(updated_input=input_data)

    return _cb


async def _handle_ask_user_question(
    agent_id: str,
    caller_is_coach: bool,
    input_data: dict[str, Any],
) -> PermissionResultAllow | PermissionResultDeny:
    questions = _normalize_question_payload(input_data)
    if not questions:
        return PermissionResultDeny(
            message="AskUserQuestion: 'questions' array is required and must be non-empty"
        )

    route = "human" if caller_is_coach else "coach"
    entry = interactions_registry.register(
        agent_id, "question", {"questions": questions}, route,
    )
    correlation_id = entry.correlation_id
    md_body = _format_questions_md(questions)

    try:
        if route == "human":
            subject = f"Question from {agent_id}"
            if len(questions) == 1 and len(questions[0]["question"]) < 80:
                subject = f"{agent_id}: {questions[0]['question']}"
            await bus.publish(
                {
                    "ts": _now(),
                    "agent_id": agent_id,
                    "type": "pending_question",
                    "correlation_id": correlation_id,
                    "route": "human",
                    "subject": subject,
                    "questions": questions,
                    "body": md_body,
                    "deadline_at": interactions_registry._iso(entry.deadline_ts),
                }
            )
        else:
            subject = f"Q from {agent_id}: correlation_id={correlation_id}"
            body = (
                f"Player {agent_id} is paused on AskUserQuestion. "
                f"Respond via coord_answer_question with correlation_id "
                f"{correlation_id!r} and an 'answers' object mapping each "
                f"question text to your chosen label.\n\n"
                + md_body
            )
            project_id = await resolve_active_project()
            c = await configured_conn()
            try:
                cur = await c.execute(
                    "INSERT INTO messages (project_id, from_id, to_id, subject, body, priority) "
                    "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                    (project_id, agent_id, "coach", subject, body, "interrupt"),
                )
                row = await cur.fetchone()
                msg_id = dict(row)["id"] if row else None
                await c.commit()
            finally:
                await c.close()
            await bus.publish(
                {
                    "ts": _now(),
                    "agent_id": agent_id,
                    "type": "pending_question",
                    "correlation_id": correlation_id,
                    "route": "coach",
                    "subject": subject,
                    "questions": questions,
                    "message_id": msg_id,
                    "deadline_at": interactions_registry._iso(entry.deadline_ts),
                }
            )
            await maybe_wake_agent(
                "coach",
                f"Player {agent_id} is paused on a question. Read your inbox, "
                f"pick answers, then call coord_answer_question with "
                f"correlation_id={correlation_id!r} to unblock them.",
                bypass_debounce=True,
            )

        answers = await interactions_registry.wait_for(entry)
        await bus.publish(
            {
                "ts": _now(),
                "agent_id": agent_id,
                "type": "question_answered",
                "correlation_id": correlation_id,
                "route": route,
                "answer_count": len(answers),
            }
        )
        return PermissionResultAllow(
            updated_input={
                "questions": input_data.get("questions", questions),
                "answers": answers,
            }
        )
    except interactions_registry.InteractionRejected as e:
        await bus.publish(
            {
                "ts": _now(),
                "agent_id": agent_id,
                "type": "question_cancelled",
                "correlation_id": correlation_id,
                "route": route,
                "reason": str(e),
            }
        )
        return PermissionResultDeny(
            message=f"Question cancelled: {e}. Proceed without the answer "
            "(reformulate, escalate via coord_request_human, or mark your "
            "task blocked)."
        )
    except asyncio.CancelledError:
        interactions_registry.forget(correlation_id)
        raise
    except Exception as e:
        logger.exception("can_use_tool: unexpected failure for %s", correlation_id)
        interactions_registry.forget(correlation_id)
        return PermissionResultDeny(
            message=f"Question routing failed: {type(e).__name__}: {e}"
        )
    finally:
        interactions_registry.forget(correlation_id)


async def _handle_exit_plan_mode(
    agent_id: str,
    caller_is_coach: bool,
    input_data: dict[str, Any],
) -> PermissionResultAllow | PermissionResultDeny:
    """ExitPlanMode: agent is requesting to leave plan mode and execute.
    Three possible human/Coach decisions:
      - approve       → PermissionResultAllow, plan executes as-is.
      - reject        → PermissionResultDeny with comments phrased as
                        "approved, but revise to include: <comments>"
                        so the agent stays in plan mode and revises.
      - approve_with_comments → PermissionResultAllow + queue a human
                        message in the agent's inbox carrying the
                        comments so they land on the next inbox read.
    """
    plan_text = (input_data or {}).get("plan") or ""
    if not isinstance(plan_text, str) or not plan_text.strip():
        return PermissionResultDeny(
            message="ExitPlanMode: 'plan' is required and must be a non-empty string."
        )

    route = "human" if caller_is_coach else "coach"
    entry = interactions_registry.register(
        agent_id, "plan", {"plan": plan_text}, route,
    )
    correlation_id = entry.correlation_id

    try:
        if route == "human":
            await bus.publish(
                {
                    "ts": _now(),
                    "agent_id": agent_id,
                    "type": "pending_plan",
                    "correlation_id": correlation_id,
                    "route": "human",
                    "plan": plan_text,
                    "deadline_at": interactions_registry._iso(entry.deadline_ts),
                }
            )
        else:
            subject = f"Plan approval from {agent_id}: correlation_id={correlation_id}"
            body = (
                f"Player {agent_id} is paused on ExitPlanMode. Review the "
                f"plan below, then call coord_answer_plan with "
                f"correlation_id={correlation_id!r} and decision "
                f"'approve' | 'reject' | 'approve_with_comments' "
                f"(comments optional on approve, required on the other two).\n\n"
                f"---\n\n"
                + plan_text
            )
            project_id = await resolve_active_project()
            c = await configured_conn()
            try:
                cur = await c.execute(
                    "INSERT INTO messages (project_id, from_id, to_id, subject, body, priority) "
                    "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                    (project_id, agent_id, "coach", subject, body, "interrupt"),
                )
                row = await cur.fetchone()
                msg_id = dict(row)["id"] if row else None
                await c.commit()
            finally:
                await c.close()
            await bus.publish(
                {
                    "ts": _now(),
                    "agent_id": agent_id,
                    "type": "pending_plan",
                    "correlation_id": correlation_id,
                    "route": "coach",
                    "subject": subject,
                    "plan": plan_text,
                    "message_id": msg_id,
                    "deadline_at": interactions_registry._iso(entry.deadline_ts),
                }
            )
            await maybe_wake_agent(
                "coach",
                f"Player {agent_id} is paused awaiting plan approval. Read "
                f"the inbox, then call coord_answer_plan with "
                f"correlation_id={correlation_id!r} to decide.",
                bypass_debounce=True,
            )

        # Wait for decision. Expected shape:
        #   {"decision": "approve" | "reject" | "approve_with_comments",
        #    "comments": str | None}
        decision_result = await interactions_registry.wait_for(entry)
        decision = (decision_result.get("decision") or "").strip().lower()
        comments = (decision_result.get("comments") or "").strip()

        await bus.publish(
            {
                "ts": _now(),
                "agent_id": agent_id,
                "type": "plan_decided",
                "correlation_id": correlation_id,
                "route": route,
                "decision": decision,
                "has_comments": bool(comments),
            }
        )

        if decision == "approve":
            return PermissionResultAllow(updated_input={"plan": plan_text})

        if decision == "approve_with_comments":
            # Inbox delivery of comments so they land on the next read —
            # the plan executes in-flight on THIS turn, and the notes
            # become available shortly after via the post-turn wake we
            # schedule below.
            if comments:
                try:
                    project_id = await resolve_active_project()
                    c = await configured_conn()
                    try:
                        await c.execute(
                            "INSERT INTO messages (project_id, from_id, to_id, subject, body, priority) "
                            "VALUES (?, 'human', ?, ?, ?, 'normal')",
                            (
                                project_id,
                                agent_id,
                                "Notes on the approved plan",
                                (
                                    "Your plan was approved. Operator notes to "
                                    "keep in mind as you execute:\n\n" + comments
                                ),
                            ),
                        )
                        await c.commit()
                    finally:
                        await c.close()
                except Exception:
                    logger.exception(
                        "plan approve_with_comments: inbox insert failed for %s",
                        agent_id,
                    )
                else:
                    # Fire-and-forget post-turn wake: awaits the current
                    # run_agent task so the wake fires AFTER plan
                    # execution ends (otherwise maybe_wake_agent no-ops
                    # because the slot is already busy). Without this,
                    # the comments sit unread until some other external
                    # event nudges the agent.
                    current_turn_task = _running_tasks.get(agent_id)
                    if current_turn_task is not None:
                        asyncio.create_task(
                            _wake_after_turn_for_plan_comments(
                                agent_id, current_turn_task,
                            )
                        )
            return PermissionResultAllow(updated_input={"plan": plan_text})

        # Reject path — default. Any non-recognised decision also falls
        # here so the SDK doesn't silently approve on a bad payload.
        # Phrasing per user spec: "approved, but revise to include …"
        # — framed constructively so the agent revises rather than
        # starts over.
        reason = comments or "operator did not approve; revise and exit plan mode again."
        return PermissionResultDeny(
            message=(
                "Approved in principle, but please revise the plan to "
                f"include: {reason} Stay in plan mode, update the plan, "
                "and call ExitPlanMode again."
            )
        )

    except interactions_registry.InteractionRejected as e:
        await bus.publish(
            {
                "ts": _now(),
                "agent_id": agent_id,
                "type": "plan_cancelled",
                "correlation_id": correlation_id,
                "route": route,
                "reason": str(e),
            }
        )
        return PermissionResultDeny(
            message=(
                f"Plan review cancelled: {e}. Stay in plan mode, revise "
                "if possible, or escalate via coord_request_human."
            )
        )
    except asyncio.CancelledError:
        interactions_registry.forget(correlation_id)
        raise
    except Exception as e:
        logger.exception("can_use_tool: ExitPlanMode failure for %s", correlation_id)
        interactions_registry.forget(correlation_id)
        return PermissionResultDeny(
            message=f"Plan routing failed: {type(e).__name__}: {e}"
        )
    finally:
        interactions_registry.forget(correlation_id)


async def run_agent(
    agent_id: str,
    prompt: str,
    *,
    model: str | None = None,
    plan_mode: bool = False,
    effort: int | None = None,
    compact_mode: bool = False,
    auto_compact: bool = False,
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

    # Auto-compact trip-wire: if this agent has a prior session and its
    # estimated context is above HARNESS_AUTO_COMPACT_THRESHOLD of the
    # model's window, run a compact turn FIRST (which nulls session_id
    # and writes a continuity note), then fall through to run the
    # user's original prompt on the fresh session. The original prompt
    # is not lost — both turns run sequentially and both show up in
    # the timeline. Skipped when compact_mode is already True (this
    # call *is* the compact turn, avoid recursion) or when the env
    # threshold is unset / 0 (feature off).
    if not compact_mode:
        threshold_env = os.environ.get("HARNESS_AUTO_COMPACT_THRESHOLD", "0.7")
        try:
            threshold = float(threshold_env)
        except ValueError:
            threshold = 0.7
        if 0.0 < threshold < 1.0:
            prior = await _get_session_id(agent_id)
            if prior:
                used = await _session_context_estimate(prior)
                ctx_max = _context_window_for(model)
                if ctx_max > 0 and used / ctx_max >= threshold:
                    await _emit(
                        agent_id,
                        "auto_compact_triggered",
                        used_tokens=used,
                        context_window=ctx_max,
                        ratio=round(used / ctx_max, 3),
                        threshold=threshold,
                        deferred_prompt=prompt,
                    )
                    try:
                        await run_agent(
                            agent_id,
                            COMPACT_PROMPT,
                            model=model,
                            compact_mode=True,
                            auto_compact=True,
                        )
                    except Exception:
                        # Compact failure shouldn't block the user's
                        # actual work. Log, emit, proceed anyway — the
                        # worst case is the context-pressure error
                        # they'd have hit without the feature.
                        logger.exception(
                            "auto-compact failed for %s; proceeding on original session",
                            agent_id,
                        )
                        await _emit(agent_id, "auto_compact_failed")

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
        compact_mode=compact_mode,
        auto_compact=auto_compact,
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
    # Prior-turn-error suffix — one-shot context so the agent doesn't
    # confabulate a reason for the previous failure. Captured by the
    # ResultMessage handler when is_error=True; popped + cleared here
    # so it appears in exactly one follow-up turn. Lives between brief
    # and handoff so the agent reads "here's your role, here's how
    # last turn ended, here's the long-term continuity".
    #
    # Compact-mode turns must NOT consume the entry — auto-compact can
    # fire between the failed turn and the user's actual follow-up,
    # and we want the note to reach the user-facing turn that comes
    # after the compaction, not the internal compact summarizer.
    prior_error_suffix = ""
    prior_err = (
        _last_turn_error_info.pop(agent_id, None)
        if not compact_mode
        else None
    )
    if prior_err:
        bits: list[str] = []
        sub = prior_err.get("subtype")
        sr = prior_err.get("stop_reason")
        nt = prior_err.get("num_turns")
        if sub:
            bits.append(f"subtype={sub}")
        if sr:
            bits.append(f"stop_reason={sr}")
        if isinstance(nt, int) and nt > 0:
            bits.append(f"num_turns={nt}")
        meta = ", ".join(bits) if bits else "no further details available"
        was_max = _looks_like_max_turns(sub, sr)
        if was_max:
            guidance = (
                "The SDK cut your previous turn off because it ran out "
                "of internal turns (max_turns), not because the harness "
                "paused you. If the work isn't complete, continue from "
                "where you left off."
            )
        else:
            guidance = (
                "If the failure looks recoverable, retry the failing "
                "step. If it doesn't, mark the task blocked or escalate "
                "via coord_request_human."
            )
        prior_error_suffix = (
            "\n\n## Prior turn note\n\n"
            f"Your previous turn ended with `is_error=True` ({meta}). "
            "The harness did NOT pause you. "
            + guidance
        )
        errs = prior_err.get("errors") or []
        if errs:
            prior_error_suffix += "\n\nReported errors (truncated):\n"
            for e in errs[:3]:
                prior_error_suffix += f"- {str(e)[:240]}\n"

    handoff_suffix = ""
    handoff_text = await _get_continuity_note(agent_id)
    if handoff_text:
        handoff_suffix = (
            "\n\n## Handoff from your prior session (via /compact)\n\n"
            + handoff_text.strip()
        )
        # Append the recent exchanges verbatim — CLI /compact keeps
        # recent turns intact, and 1 exchange was too thin (tool
        # chains span multiple turns). The rolling list is maintained
        # by _append_exchange on every successful non-compact turn,
        # trimmed from the head so total size stays under
        # HARNESS_HANDOFF_TOKEN_BUDGET (default 20K tokens — the full
        # session transcript lives in the jsonl file, so this injected
        # log is just a warm-start, not the long-term record).
        recent = await _get_recent_exchanges(agent_id)
        # Drop empty/malformed entries defensively.
        recent = [
            e for e in recent
            if isinstance(e, dict)
            and isinstance(e.get("prompt"), str)
            and isinstance(e.get("response"), str)
            and (e.get("prompt") or e.get("response"))
        ]
        if recent:
            handoff_suffix += (
                f"\n\n### Recent exchanges (verbatim, last "
                f"{len(recent)} turn{'s' if len(recent) != 1 else ''} "
                "before compact, oldest first)\n"
            )
            for i, e in enumerate(recent, start=1):
                handoff_suffix += (
                    f"\n#### Exchange {i} of {len(recent)}\n\n"
                    "**User asked:**\n\n"
                    + (e["prompt"] or "").strip()
                    + "\n\n**You replied:**\n\n"
                    + (e["response"] or "").strip()
                    + "\n"
                )
        handoff_suffix += (
            "\n\n(Your previous conversation history has been cleared "
            "to free context. The summary + verbatim exchanges above "
            "are your memory of what came before.)"
        )
    # Phase 7 audit (PROJECTS_SPEC.md §10): "Roster availability"
    # used to be a standalone Coach-only suffix; the spec folds it
    # into the coordination block as a sub-section. The block now
    # owns both the inline LOCKED tag and the prose reminder — no
    # standalone lock_suffix is appended for Coach. Players never
    # had this suffix in the first place, so there's nothing to
    # preserve there either.
    lock_suffix = ""
    # Identity injection (PROJECTS_SPEC.md §8). Built from
    # agent_project_roles for the active project + the agent's slot.
    # Prepended so the agent reads "who am I in this project" before
    # anything else.
    identity_prefix = ""
    try:
        ident = await _get_agent_identity(agent_id)
    except Exception:
        ident = {}
    if ident:
        active_pid = await resolve_active_project()
        ident_lines = [f"## Your identity (active project: {active_pid})", ""]
        ident_lines.append(f"- Slot: `{agent_id}`")
        if ident.get("name"):
            ident_lines.append(f"- Name: {ident['name']}")
        if ident.get("role"):
            ident_lines.append(f"- Role: {ident['role']}")
        if ident.get("brief"):
            ident_lines.append("")
            ident_lines.append("### Brief (project-specific)")
            ident_lines.append("")
            ident_lines.append(ident["brief"].strip())
        identity_prefix = "\n".join(ident_lines) + "\n\n"

    # Coach coordination block (PROJECTS_SPEC.md §10). Distinct from
    # identity so the on-the-wire layout matches spec:
    #   [identity] + [coordination block] + [role prompt + global CLAUDE.md]
    # Phase 7: built fresh on every Coach turn from `projects`,
    # `agent_project_roles`, `agents.locked`, `tasks`, `messages`,
    # and the latest entry in `decisions/` so a project switch, a
    # `coord_set_player_role` update, a new task, or a fresh decision
    # all show up immediately on Coach's next turn.
    coordination_block = ""
    if agent_id == "coach":
        try:
            body = await _build_coach_coordination_block()
        except Exception:
            logger.exception("coach coordination block build failed")
            body = ""
        if body:
            coordination_block = body + "\n"

    system_prompt = (
        identity_prefix
        + coordination_block
        + _system_prompt_for(agent_id)
        + context_suffix
        + brief_suffix
        + prior_error_suffix
        + handoff_suffix
        + lock_suffix
    )
    if context_suffix or brief_suffix or handoff_suffix or prior_error_suffix:
        # Emit sizes (not content) so the user can see "yes my stuff was
        # picked up" without drowning the timeline in prompt text.
        await _emit(
            agent_id,
            "context_applied",
            chars=len(context_suffix) + len(brief_suffix) + len(handoff_suffix),
            brief_chars=len(brief_suffix),
            handoff_chars=len(handoff_suffix),
        )

    # can_use_tool callback: intercepts AskUserQuestion and routes per
    # agent role. Coach → human form (via pending-question event + POST
    # /api/questions/<id>/answer). Player → Coach's inbox (via send_message
    # + coord_answer_question tool). Callback closes over agent_id so each
    # spawn gets a callback scoped to its caller.
    can_use_tool_cb = _build_can_use_tool(agent_id)

    options_kwargs: dict[str, Any] = dict(
        system_prompt=system_prompt,
        cwd=str(workspace_dir(agent_id)),
        max_turns=MAX_TURNS_PER_SPAWN,
        mcp_servers=mcp_servers,
        allowed_tools=allowed,
        # can_use_tool requires streaming mode + a PreToolUse hook that
        # returns continue_ (documented workaround — without it the
        # stream closes before the permission callback can fire).
        can_use_tool=can_use_tool_cb,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_pretool_continue_hook])]},
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
    # permission_mode: the SDK api-reference documents that
    # can_use_tool only fires reliably when permission_mode="default"
    # is explicitly set. We set that as the baseline so our
    # AskUserQuestion routing actually triggers, and let plan_mode
    # override to "plan" when the pane requested it.
    options_kwargs["permission_mode"] = "plan" if plan_mode else "default"
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
        # True when this compact turn was auto-triggered (context over
        # threshold) vs invoked manually via /compact. Used by the
        # stuck-session guard: an empty auto-compact force-clears
        # session_id to escape the retry loop, whereas an empty manual
        # /compact leaves state alone so the user can retry.
        "auto_compact": auto_compact,
        # True when the system prompt we just built embedded a handoff
        # section — the ResultMessage handler uses this to clear the
        # note on the first successful non-compact turn after a
        # compact, so stale handoffs don't re-inject forever.
        "had_handoff_on_entry": bool(handoff_suffix) and not compact_mode,
        # User prompt this turn was started with. Snapshot here so a
        # post-result write to agents.last_exchange_json has the pair
        # together, even if the SDK later mutates prompt in streaming
        # mode.
        "entry_prompt": prompt,
    }

    async def _prompt_stream():
        # Streaming-mode prompt required for can_use_tool (Python SDK).
        # We yield a single user message carrying the turn's prompt —
        # identical payload the SDK would build internally for a plain
        # string prompt, just wrapped in the streaming envelope so the
        # permission callback can fire.
        yield {
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }

    async def _iterate(opts: ClaudeAgentOptions) -> None:
        # Tiny indirection so we can retry the whole iteration once
        # after stale-session cleanup without duplicating the body.
        turn_ctx["started_at"] = _now()
        async for msg in query(prompt=_prompt_stream(), options=opts):
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
            # Suppressed post-result teardown noise isn't a real
            # failure — the turn's work completed. Reset the
            # consecutive-error counter so retries don't falsely
            # accumulate toward the escalation cap.
            _consecutive_errors.pop(agent_id, None)
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
            # Auto-retry: bump the consecutive-error counter and
            # schedule a delayed wake. The scheduler itself handles the
            # cap (escalates to human after N consecutive errors
            # without an intervening success) and the no-op-if-
            # recovered check.
            _consecutive_errors[agent_id] = _consecutive_errors.get(agent_id, 0) + 1
            await _schedule_post_error_retry(agent_id)
    else:
        await _set_status(agent_id, "idle")
        # Success path: reset the consecutive-error counter so a later
        # transient error isn't immediately treated as "attempt N+1".
        _consecutive_errors.pop(agent_id, None)
    finally:
        _running_tasks.pop(agent_id, None)
        # Stamp when this turn ended so the auto-wake debounce can see
        # it on the next incoming event. Pure in-memory — a restart
        # clears the record, which is fine (first post-restart wake
        # just fires immediately).
        _last_turn_ended_at[agent_id] = time.monotonic()

    await _emit(agent_id, "agent_stopped")


async def _get_status_of(agent_id: str) -> str | None:
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status FROM agents WHERE id = ?", (agent_id,)
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        return None
    if not row:
        return None
    return dict(row).get("status")


def _looks_like_max_turns(subtype: Any, stop_reason: Any) -> bool:
    """True iff the SDK's terminal flags indicate the turn was cut
    off because it ran out of internal turns (vs. a real error).

    SDK shape varies across versions:
      - `subtype` = "error_max_turns" on recent builds.
      - `stop_reason` = "max_turns" / "max_tokens" on the underlying
        Anthropic API result.

    We match either, case-insensitive substring on the canonical name,
    so a near-rename in a future SDK release still trips us.
    """
    s = str(subtype or "").lower()
    if "max_turn" in s:  # 'error_max_turns', 'max_turns', etc.
        return True
    r = str(stop_reason or "").lower()
    if r in ("max_turns", "max_tokens"):
        return True
    return False


async def _maybe_schedule_auto_continue(
    *,
    agent_id: str,
    subtype: str | None,
    stop_reason: str | None,
    num_turns: int | None,
) -> None:
    """Schedule a follow-up turn that prompts the agent to resume
    from where it was cut off. Only fired when _looks_like_max_turns
    returned True — soft errors (error_during_execution etc.) take
    the existing Player→Coach DM path instead.

    Cap behavior: each consecutive max-turns hit bumps the counter.
    At the cap (HARNESS_AUTO_CONTINUE_MAX_CONSECUTIVE) we publish a
    human_attention event and stop continuing — the workflow has
    plausibly diverged into a loop and only the human can decide
    whether to re-scope or kill the task. Counter resets on any
    clean ResultMessage (handled in the result branch above).
    """
    if agent_id in _auto_continue_pending:
        return
    if _paused:
        logger.info(
            "auto-continue: skipping %s — harness paused",
            agent_id,
        )
        return
    attempt = _consecutive_auto_continues.get(agent_id, 0)
    if attempt >= AUTO_CONTINUE_MAX_CONSECUTIVE:
        # Cap reached — escalate and stop. Don't reset the counter
        # here; a clean turn (any non-error result) clears it.
        await _emit(
            agent_id,
            "auto_continue_gave_up",
            consecutive=attempt,
            cap=AUTO_CONTINUE_MAX_CONSECUTIVE,
            subtype=subtype,
            stop_reason=stop_reason,
        )
        try:
            await bus.publish({
                "ts": _now(),
                "agent_id": agent_id,
                "type": "human_attention",
                "subject": (
                    f"{agent_id}: hit max_turns "
                    f"{attempt} turns in a row — auto-continue stopped"
                ),
                "body": (
                    f"Each of the last {attempt} continuations also ran "
                    "out of internal turns before finishing. The task "
                    "is plausibly looping or under-scoped. Investigate, "
                    "re-prompt with a smaller chunk, or cancel the task "
                    "via the UI."
                ),
                "urgency": "high",
            })
        except Exception:
            logger.exception(
                "auto-continue: human_attention publish failed for %s",
                agent_id,
            )
        return

    _consecutive_auto_continues[agent_id] = attempt + 1
    _auto_continue_pending.add(agent_id)
    reason_label = subtype or stop_reason or "max_turns"
    nturns = num_turns if num_turns is not None else "?"

    async def _delayed_continue() -> None:
        try:
            await asyncio.sleep(AUTO_CONTINUE_DELAY_SECONDS)
            # If a clean turn arrived during the delay, the
            # ResultMessage handler cleared _last_turn_error_info
            # (and the auto-continue counter). Firing the wake now
            # would inject a stale "your previous turn was cut off"
            # prompt into a fresh conversation. Bail out instead.
            if agent_id not in _last_turn_error_info:
                return
            # If the human (or another wake path) already kicked off a
            # fresh turn during the delay, skip — we don't want to
            # double-fire and confuse the agent with a stale "continue"
            # prompt landing mid-conversation.
            if agent_id in _running_tasks:
                return
            if _paused:
                logger.info(
                    "auto-continue: skipping %s — paused during delay",
                    agent_id,
                )
                return
            await _emit(
                agent_id,
                "auto_continue_scheduled",
                attempt=attempt + 1,
                cap=AUTO_CONTINUE_MAX_CONSECUTIVE,
                delay=AUTO_CONTINUE_DELAY_SECONDS,
                subtype=subtype,
                stop_reason=stop_reason,
            )
            await maybe_wake_agent(
                agent_id,
                (
                    f"Your previous turn was cut off by the SDK "
                    f"({reason_label} after {nturns} internal turns) "
                    "before you could finish. The harness did NOT "
                    "pause you. Continue from where you left off — "
                    "no need to re-explain context. If you actually "
                    "completed the work, just confirm so."
                ),
                bypass_debounce=True,
            )
        except Exception:
            logger.exception("auto-continue delayed task failed for %s", agent_id)
        finally:
            _auto_continue_pending.discard(agent_id)

    asyncio.create_task(_delayed_continue())


async def _schedule_post_error_retry(agent_id: str) -> None:
    """Schedule a single auto-wake after a hard error so the agent
    doesn't sit idle indefinitely. Caps consecutive retries and
    escalates to the human when the cap trips.

    The retry is cancelled (logically — the task still runs but no-ops)
    if the agent has recovered to idle/working by the time the delay
    elapses, so user/other-event wakes during the window don't double-
    fire.
    """
    if agent_id in _retry_pending:
        return
    attempt = _consecutive_errors.get(agent_id, 0)
    if attempt >= ERROR_RETRY_MAX_CONSECUTIVE:
        # Escalate once, then stop retrying. Leaves the agent in error
        # state so the UI shows something's wrong; human /clear or a
        # manual prompt is required to resume.
        await _emit(
            agent_id,
            "auto_retry_gave_up",
            consecutive=attempt,
            hint="Too many consecutive errors — manually clear the "
                 "session or investigate logs before retrying.",
        )
        try:
            # coord_request_human emits a human_attention event that
            # pins a red banner in the EnvPane. Fire it directly here
            # rather than via a tool call — the agent isn't running.
            await bus.publish({
                "ts": _now(),
                "agent_id": agent_id,
                "type": "human_attention",
                "subject": f"{agent_id}: auto-retry gave up after {attempt} errors",
                "body": "The agent's last several turns all errored. "
                        "Investigate /api/health, check CLI auth, or "
                        "clear the session and retry manually.",
                "urgency": "high",
            })
        except Exception:
            logger.exception("escalation publish failed for %s", agent_id)
        # Also DM Coach so coordination doesn't hang while waiting for
        # the human to clear the escalation. Skip when the stuck agent
        # IS Coach (no one to DM). This is the final notification —
        # bypass the error-DM debounce so it always lands.
        if agent_id != "coach":
            try:
                await _deliver_system_message(
                    from_id=agent_id,
                    to_id="coach",
                    subject=f"{agent_id}: auto-retry gave up ({attempt} errors)",
                    body=(
                        f"I've errored {attempt} turns in a row and the "
                        f"harness stopped retrying. Any task I own is "
                        f"stuck until the human investigates or you "
                        f"reassign it. Treat my slot as unavailable."
                    ),
                    priority="interrupt",
                )
            except Exception:
                logger.exception("gave-up Coach DM failed for %s", agent_id)
        return
    _retry_pending.add(agent_id)

    async def _delayed_retry() -> None:
        try:
            await asyncio.sleep(ERROR_RETRY_DELAY_SECONDS)
            # If the agent self-recovered (human intervened, new wake
            # fired, etc.) skip — we don't want to poke a healthy
            # agent with a stale "your turn errored" prompt.
            status = await _get_status_of(agent_id)
            if status != "error":
                return
            if agent_id in _running_tasks:
                return
            await _emit(
                agent_id,
                "auto_retry_scheduled",
                attempt=attempt + 1,
                max_attempts=ERROR_RETRY_MAX_CONSECUTIVE,
                delay=ERROR_RETRY_DELAY_SECONDS,
            )
            await maybe_wake_agent(
                agent_id,
                "Your previous turn errored before completing. "
                "If you had a task in progress, resume it. If the "
                "error looks persistent, use coord_update_task(..., "
                "status='blocked') to park the task, or "
                "coord_request_human to escalate. Otherwise, carry on "
                "where you left off.",
                bypass_debounce=True,
            )
        except Exception:
            logger.exception("delayed retry failed for %s", agent_id)
        finally:
            _retry_pending.discard(agent_id)

    asyncio.create_task(_delayed_retry())


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
    """True if Coach is mid-turn — checked from two angles to cover
    every part of the spawn lifecycle:

      1. ``_running_tasks['coach']`` is the in-process asyncio task
         registered before any DB write. Catches the brief window
         between slot-claim under ``_SPAWN_LOCK`` and the
         ``_set_status('working')`` that follows it (see run_agent).
         Also catches a queued spawn whose DB status flip is still
         pending behind another awaitable.
      2. ``agents.status == 'working'`` is the persistent flag —
         survives module reloads and cross-process visibility.

    Either signal is enough to skip the next /loop or /repeat fire.
    A transient DB hiccup falls back to the in-memory check, so a
    DB outage doesn't silently let the loops stack turns.
    """
    task = _running_tasks.get("coach")
    if task is not None and not task.done():
        return True
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
            await bus.publish(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "agent_id": "coach",
                    "type": "coach_tick_fired",
                    "interval_seconds": _coach_tick_interval,
                }
            )
            await run_agent("coach", COACH_TICK_PROMPT)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("coach autoloop: tick failed")


async def coach_repeat_loop() -> None:
    """Background task: periodically send a user-supplied prompt to
    Coach. Independent of coach_tick_loop — both can be active at
    once, each with its own cadence. Idles when interval<=0."""
    logger.info("coach repeat loop running (disabled until /repeat set)")
    while True:
        interval = _coach_repeat_interval
        try:
            if interval <= 0:
                await asyncio.sleep(5)
                continue
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        try:
            if _coach_repeat_interval <= 0:
                continue
            prompt = _coach_repeat_prompt
            if not prompt:
                continue
            if _paused:
                logger.info("coach repeat loop: skipping — harness paused")
                continue
            if await _coach_is_working():
                logger.info("coach repeat loop: skipping — coach is working")
                continue
            await bus.publish(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "agent_id": "coach",
                    "type": "coach_repeat_fired",
                    "interval_seconds": _coach_repeat_interval,
                }
            )
            await run_agent("coach", prompt)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("coach repeat loop: tick failed")


# Stale-task watchdog config. Env-tunable so the default can be safe
# (fires rarely) and aggressive setups can dial in tighter.
#   HARNESS_STALE_TASK_MINUTES: how long an in-progress task must sit
#     without owner activity before it's flagged. Default 15.
#   HARNESS_STALE_TASK_NOTIFY_INTERVAL_MINUTES: re-notify cadence for
#     the same task if it stays stalled. Default 30.
#   HARNESS_STALE_TASK_CHECK_INTERVAL_SECONDS: how often the loop runs.
#     Default 60.
# Setting STALE_TASK_MINUTES to 0 disables the watchdog entirely.
STALE_TASK_MINUTES = int(os.environ.get("HARNESS_STALE_TASK_MINUTES", "15"))
STALE_TASK_NOTIFY_INTERVAL_MIN = int(
    os.environ.get("HARNESS_STALE_TASK_NOTIFY_INTERVAL_MINUTES", "30")
)
STALE_TASK_CHECK_INTERVAL_SEC = int(
    os.environ.get("HARNESS_STALE_TASK_CHECK_INTERVAL_SECONDS", "60")
)
# task_id → monotonic ts of the last DM we sent Coach about this task.
# Keeps the same stuck task from pinging Coach every 60s.
_stale_task_last_notify: dict[str, float] = {}


async def stale_task_watch_loop() -> None:
    """Background task: find tasks claimed/in_progress whose owner
    hasn't emitted an event in STALE_TASK_MINUTES minutes and DM Coach
    so the workflow doesn't silently hang. Re-notifies at most every
    STALE_TASK_NOTIFY_INTERVAL_MIN minutes per task. Also emits a
    `task_stalled` event for audit.

    Detection query: left-join tasks against the max(events.ts) per
    owner; filter on (now - last_activity) > cutoff. Excludes tasks
    where the owner has ZERO events in history (fresh system never
    been used — avoids firing on an empty DB). Locked or cancelled
    agents are skipped because they're a known-stall case, not a bug.

    Includes `claimed` AND `in_progress` because crash_recover() on
    every container boot demotes in_progress → claimed; without
    this the watchdog would go blind to every active task across a
    redeploy. A `claimed` task with no recent activity is also a
    stall worth surfacing — either the owner never started, or the
    boot reset wiped state.
    """
    if STALE_TASK_MINUTES <= 0:
        logger.info("stale-task watchdog disabled (HARNESS_STALE_TASK_MINUTES=0)")
        return
    logger.info(
        "stale-task watchdog running (stale after %dm; re-notify every %dm; check every %ds)",
        STALE_TASK_MINUTES, STALE_TASK_NOTIFY_INTERVAL_MIN, STALE_TASK_CHECK_INTERVAL_SEC,
    )
    while True:
        try:
            await asyncio.sleep(STALE_TASK_CHECK_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
        try:
            if _paused:
                continue
            cutoff_minutes = STALE_TASK_MINUTES
            # Phase 3 audit follow-up: scope the watchdog to the
            # active project. Without this filter Coach gets DM'd
            # about every project's stale tasks regardless of which
            # one is currently active — confusing.
            active_project_id = await resolve_active_project()
            c = await configured_conn()
            try:
                # julianday returns fractional days; × 1440 → minutes.
                # HAVING filters tasks with NO activity in cutoff min.
                cur = await c.execute(
                    """
                    SELECT t.id, t.title, t.owner, t.created_by, t.status,
                           MAX(e.ts) AS last_activity
                    FROM tasks t
                    LEFT JOIN events e
                      ON e.agent_id = t.owner AND e.project_id = t.project_id
                    WHERE t.project_id = ?
                      AND t.status IN ('in_progress', 'claimed')
                      AND t.owner IS NOT NULL
                    GROUP BY t.id
                    HAVING last_activity IS NOT NULL
                       AND (julianday('now') - julianday(last_activity)) * 1440 > ?
                    """,
                    (active_project_id, cutoff_minutes),
                )
                rows = await cur.fetchall()
            finally:
                await c.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("stale-task watchdog: DB query failed")
            continue
        if not rows:
            continue
        now_m = time.monotonic()
        notify_interval_sec = STALE_TASK_NOTIFY_INTERVAL_MIN * 60
        for row in rows:
            d = dict(row)
            task_id = d.get("id")
            if not task_id:
                continue
            last = _stale_task_last_notify.get(task_id, 0.0)
            if last and (now_m - last) < notify_interval_sec:
                continue
            _stale_task_last_notify[task_id] = now_m
            owner = d.get("owner") or "?"
            title = d.get("title") or "(no title)"
            last_act = d.get("last_activity") or "unknown"
            status = d.get("status") or "in_progress"
            try:
                await _emit(
                    "system",
                    "task_stalled",
                    task_id=task_id,
                    owner=owner,
                    last_activity=last_act,
                    stale_minutes=cutoff_minutes,
                    task_status=status,
                )
            except Exception:
                logger.exception("stale-task: emit failed for %s", task_id)
            # Skip the DM if Coach IS the owner of the stalled task —
            # Coach can't nudge themselves via inbox. The task_stalled
            # event still lands in the UI timeline for the human.
            if owner == "coach":
                continue
            try:
                await _deliver_system_message(
                    from_id="system",
                    to_id="coach",
                    subject=f"task {task_id} stalled ({owner})",
                    body=(
                        f"Task {task_id} \"{title[:100]}\" has been "
                        f"{status} for {cutoff_minutes}+ minutes with no "
                        f"activity from {owner} (last event: {last_act}). "
                        f"Decide whether to nudge them, reassign, or mark "
                        f"the task blocked."
                    ),
                    priority="normal",
                )
            except Exception:
                logger.exception("stale-task: DM to Coach failed for %s", task_id)

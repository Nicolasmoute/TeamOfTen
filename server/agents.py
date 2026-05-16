from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
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
)

from server import interactions as interactions_registry
from server.context import build_system_prompt_suffix
from server.db import configured_conn, resolve_active_project
from server.events import bus
from server.webdav import webdav
from server.mcp_config import load_external_servers
from server.models_catalog import MODEL_GUIDANCE
from server.tools import ALLOWED_COACH_TOOLS, ALLOWED_PLAYER_TOOLS
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
            "body_preview": body[:4000],
            "body_full_len": len(body),
            "body_truncated": len(body) > 4000,
            "priority": priority,
        })
    except Exception:
        logger.exception("_deliver_system_message publish failed")
    if wake and to_id != "broadcast":
        try:
            preview = body.strip().replace("\n", " ")[:240]
            subj = f" (subject: {subject})" if subject else ""
            wake_body = f"New message from {from_id}{subj}: \"{preview}\""
            if to_id != "coach":
                from server.tools import _with_player_reminder
                wake_body = _with_player_reminder(wake_body)
            # bypass_debounce=True matches every other discrete-action wake site
            await maybe_wake_agent(to_id, wake_body, bypass_debounce=True)
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
    runtime: str = "claude",
    cost_basis: str | None = "token_priced",
) -> None:
    """Insert one row into the `turns` ledger — cheap analytics table
    (one row per SDK ResultMessage). Errors are swallowed: losing a
    ledger row should never break the live turn, the event log is
    still the source of truth for audit.

    `runtime` — 'claude' (default) or 'codex'. Recorded for
    by-runtime analytics + the `cost_basis` split (§G).
    `cost_basis` — 'token_priced' (cost_usd populated from a pricing
    table or ResultMessage.total_cost_usd) or 'plan_included' (Codex
    on ChatGPT auth; cost_usd = 0 by design). Pass None to defer.
    """
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
                "input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
                "runtime, cost_basis"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    runtime,
                    cost_basis,
                ),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("insert_turn_row failed: agent=%s", agent_id)


def _extract_usage_claude(msg: Any) -> dict[str, int]:
    """Pull token counts from a Claude `ResultMessage.usage`. Defensive
    against SDK shape drift: `usage` may be a dict, a Pydantic model,
    or an `anthropic.types.Usage`. Fields we want may or may not exist
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


def _extract_usage_codex(usage: Any) -> dict[str, int]:
    """Pull token counts from a Codex `Turn.usage` block.

    Codex shape per spec §E.5:
      - `input_tokens`           — prompt tokens (uncached)
      - `cached_input_tokens`    — prompt tokens served from cache
                                   (cheaper) → mapped to cache_read
      - `output_tokens`          — completion + reasoning tokens
    Codex caching has no separate creation cost, so cache_creation = 0.

    Accepts the usage block directly (not the wrapping message) so the
    caller can pass either `turn.usage` or a manually-shaped dict.
    Missing fields → 0.
    """
    if usage is None:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}

    def _get(name: str) -> int:
        v = None
        if isinstance(usage, dict):
            v = usage.get(name)
        else:
            v = getattr(usage, name, None)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    return {
        "input": _get("input_tokens"),
        "output": _get("output_tokens"),
        "cache_read": _get("cached_input_tokens"),
        "cache_creation": 0,
    }


# Backwards-compatible alias — older code paths call `_extract_usage`
# expecting Claude-shape input. New callers should pick the explicit
# variant. Remove this alias once every call site is updated.
_extract_usage = _extract_usage_claude


# Per-model context window in tokens. Used by the auto-compact check
# and the pane ContextBar. Seeded from known-at-build-time values for
# current Claude models, with a conservative observed fallback for
# unknown future model ids. Do not infer windows from ResultMessage
# usage: that usage is aggregated across all API calls in a turn and
# can exceed the real model window during tool-heavy turns.
_CONTEXT_WINDOWS = {
    # Claude (Max plan)
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-7[1m]": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-4-6[1m]": 1_000_000,
    "claude-haiku-4-5-20251001": 200_000,
    # OpenAI / Codex (per developers.openai.com 2026-04). Frontier
    # general models (gpt-5.5 / gpt-5.4) ship at ~1.05M; the smaller
    # mini and the codex-tuned variants are pinned at 400K. Numbers
    # here are conservative against the published max so the auto-
    # compact threshold trips before the real wall.
    "gpt-5.5": 1_050_000,
    "gpt-5.4": 1_050_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.4-nano": 400_000,
    "gpt-5.3-codex": 400_000,
    "gpt-5.2-codex": 400_000,
    "gpt-5.1-codex": 400_000,
    "gpt-5.1-codex-max": 400_000,
    "gpt-5.1-codex-mini": 400_000,
    "gpt-5-codex": 400_000,
}

# Observed ceilings learned from one assistant API call's prompt usage.
# Ratchets upward only. Separately, Codex app-server token_count events
# can report an exact model_context_window; that value may be lower
# than the public API table for the same model id, so it must be able
# to override downward for CTX % and auto-compact.
_OBSERVED_CONTEXT_WINDOWS: dict[str, int] = {}
_REPORTED_CONTEXT_WINDOWS: dict[str, int] = {}


async def _load_observed_windows() -> None:
    """Seed _OBSERVED_CONTEXT_WINDOWS from team_config on startup.
    Called from the lifespan hook so reloads don't relearn from turn 1."""
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT key, value FROM team_config "
                "WHERE key IN ('context_observed', 'context_reported')"
            )
            rows = await cur.fetchall()
        finally:
            await c.close()
    except Exception:
        logger.exception("context: observed-windows load failed")
        return
    if not rows:
        return
    for row in rows:
        r = dict(row)
        try:
            parsed = json.loads(r.get("value") or "{}")
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        if r.get("key") == "context_reported":
            for k, v in parsed.items():
                if isinstance(k, str) and isinstance(v, int) and 1_000 <= v <= 2_000_000:
                    _REPORTED_CONTEXT_WINDOWS[k] = v
            continue
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


async def _persist_reported_windows() -> None:
    """Persist exact provider-reported context windows."""
    payload = json.dumps(_REPORTED_CONTEXT_WINDOWS, ensure_ascii=False)
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO team_config (key, value) VALUES "
                "('context_reported', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (payload,),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("context: reported-windows persist failed")


async def _observe_reported_context_window(
    model: str | None,
    context_window: int | None,
) -> None:
    """Record an exact context window reported by the runtime/provider.

    Unlike _observe_context_usage, this is allowed to move downward:
    Codex app-server can expose a runtime-specific effective window for
    a model id whose public API maximum is larger.
    """
    if not model or context_window is None:
        return
    from server.models_catalog import resolve_model_alias
    resolved_id = resolve_model_alias(model)
    try:
        window = int(context_window)
    except (TypeError, ValueError):
        return
    if not (1_000 <= window <= 2_000_000):
        return
    if _REPORTED_CONTEXT_WINDOWS.get(resolved_id) == window:
        return
    _REPORTED_CONTEXT_WINDOWS[resolved_id] = window
    logger.info("context: runtime reported %s window = %d", resolved_id, window)
    await _persist_reported_windows()


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

    Precedence: provider-reported exact window > observed-max > table
    > 1M fallback. The observed map ratchets upward, so a new model
    shipping with a bigger window than we assumed self-corrects after
    one real turn. The reported map can also correct downward when a
    runtime has a smaller effective window than the generic model id.

    Tier aliases (`latest_opus`, `latest_gpt`, …) are resolved to
    their concrete id before lookup so external callers (the
    `/api/agents/{id}/context` endpoint, defensive uses elsewhere)
    don't need to know about the aliasing layer."""
    if not model:
        return max(
            1_000_000,
            max(_REPORTED_CONTEXT_WINDOWS.values(), default=0),
            max(_OBSERVED_CONTEXT_WINDOWS.values(), default=0),
        )
    from server.models_catalog import resolve_model_alias
    resolved_id = resolve_model_alias(model)
    reported = _REPORTED_CONTEXT_WINDOWS.get(resolved_id, 0)
    if reported > 0:
        return reported
    observed = _OBSERVED_CONTEXT_WINDOWS.get(resolved_id, 0)
    table = _CONTEXT_WINDOWS.get(resolved_id, 0)
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


# Cache of session_id → resolved jsonl path. The `rglob` below walks
# CLAUDE_CONFIG_DIR/projects recursively and is the dominant cost of
# the metric — sessions don't move, so the result is sticky once
# resolved. ContextBar mount + every result event re-enter this path,
# so without the cache every metric request pays the full walk.
_SESSION_JSONL_PATHS: dict[str, Path] = {}

# Cache of parsed jsonl metrics keyed by (path, mtime_ns, size). The
# whole-file parse is the second-largest cost; the file only grows
# (assistant rows are appended), so a stat-mismatch is the right
# invalidation signal.
_SESSION_METRICS_CACHE: dict[
    tuple[str, int, int], tuple[int, int | None]
] = {}
_SESSION_METRICS_MAX = 64


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
        # Path cache: trust the prior resolution if the file is still
        # there. If it's gone (compact rotated, session deleted) drop
        # the cache entry and fall through to re-rglob.
        jsonl_path = _SESSION_JSONL_PATHS.get(session_id)
        if jsonl_path is not None and not jsonl_path.is_file():
            _SESSION_JSONL_PATHS.pop(session_id, None)
            jsonl_path = None
        if jsonl_path is None:
            # Sessions are sharded by encoded-cwd sub-dirs; one session_id
            # is unique across the tree, so the first match is the file.
            for p in projects_root.rglob(f"{session_id}.jsonl"):
                jsonl_path = p
                break
            if jsonl_path is None:
                return 0, None
            _SESSION_JSONL_PATHS[session_id] = jsonl_path

        # Metrics cache: parse only when (mtime, size) differs from a
        # prior parse. The file is append-only between parses, so size
        # alone is usually a sufficient invalidator; mtime catches the
        # rare in-place rewrite.
        try:
            st = jsonl_path.stat()
        except OSError:
            return 0, None
        cache_key = (str(jsonl_path), st.st_mtime_ns, st.st_size)
        cached = _SESSION_METRICS_CACHE.get(cache_key)
        if cached is not None:
            return cached
        result = _session_context_metrics_from_jsonl(jsonl_path)
        # Cap-evict only on insert of a NEW key — never drop an entry
        # just to overwrite the same one (defensive; the working set
        # is normally small).
        if (
            cache_key not in _SESSION_METRICS_CACHE
            and len(_SESSION_METRICS_CACHE) >= _SESSION_METRICS_MAX
        ):
            _SESSION_METRICS_CACHE.pop(next(iter(_SESSION_METRICS_CACHE)), None)
        _SESSION_METRICS_CACHE[cache_key] = result
        return result
    except Exception:
        logger.exception("session_context_estimate: jsonl parse failed")
        return 0, None


async def _session_context_estimate(session_id: str) -> int:
    """Return estimated tokens already occupied by a resumed session."""
    used, _latest_prompt = await _session_context_metrics(session_id)
    return used


async def _codex_session_context_estimate(thread_id: str) -> int:
    """Estimate current context for a resumed Codex thread.

    Codex doesn't write a per-session jsonl in `CLAUDE_CONFIG_DIR`,
    so the Claude path returns 0 for codex sessions. Instead we read
    the most recent `turns` row whose `session_id` equals the codex
    thread_id and reconstruct prompt size from the recorded usage:
    `input + cache_read + cache_creation + latest output` (output
    will be part of the next resumed prompt). Same shape as the
    Claude jsonl path so the UI percentage stays comparable across
    runtimes.
    """
    if not thread_id:
        return 0
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT input_tokens, output_tokens, "
                "       cache_read_tokens, cache_creation_tokens "
                "FROM turns "
                "WHERE session_id = ? AND runtime = 'codex' "
                "ORDER BY id DESC LIMIT 1",
                (thread_id,),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("codex_session_context_estimate: DB read failed")
        return 0
    if not row:
        return 0
    r = dict(row)
    prompt = (
        int(r.get("input_tokens") or 0)
        + int(r.get("cache_read_tokens") or 0)
        + int(r.get("cache_creation_tokens") or 0)
    )
    output = int(r.get("output_tokens") or 0)
    return prompt + output


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
        #
        # 2026-05-11: sticky-fingerprint dedup. When a prior_error_suffix
        # with shape (subtype, stop_reason, num_turns) was injected into
        # any recent turn's prompt AND the same shape errors again,
        # don't re-arm `_last_turn_error_info` — the agent saw the note
        # once; further repetition is noise. Observed live: recurrence
        # ticks looping with prior_error=289 chars unchanged across 3.5
        # hours of identical Coach prompts.
        #
        # `_last_shown_prior_error_fp[agent_id]` is the sticky tracker
        # (cross-turn, cleared only on clean turn). A per-turn-only
        # consumed_fp would just oscillate ON/OFF every other turn,
        # since the spawn pops _last_turn_error_info even when it
        # consumed a prior_err — so turn N+1 wouldn't see the note,
        # turn N+1 errors, turn N+2 re-shows. The sticky tracker
        # remembers across the gap.
        if msg.is_error and not (turn_ctx and turn_ctx.get("compact_mode")):
            new_fp = (
                str(subtype) if subtype else None,
                str(stop_reason) if stop_reason else None,
                num_turns if isinstance(num_turns, int) else None,
            )
            already_shown_fp = _last_shown_prior_error_fp.get(agent_id)
            if already_shown_fp != new_fp:
                _last_turn_error_info[agent_id] = {
                    "stop_reason": str(stop_reason) if stop_reason else None,
                    "subtype": str(subtype) if subtype else None,
                    "num_turns": num_turns,
                    "duration_ms": duration_ms,
                    "errors": errors_summary or [],
                }
            # else: identical to last-shown shape → don't re-arm. Stays
            # quiet until a clean turn clears the sticky tracker.
        elif not msg.is_error:
            # Clean turn — clear any stale entry so the next prompt
            # doesn't see an obsolete "your prior turn errored" note.
            # Also clear the sticky shown-fp tracker so a future error
            # (potentially with a different shape) gets shown again.
            _last_turn_error_info.pop(agent_id, None)
            _last_shown_prior_error_fp.pop(agent_id, None)
            _consecutive_auto_continues.pop(agent_id, None)
        await _add_cost(agent_id, cost)
        # Player soft-error: either auto-retry (transient shapes like
        # `stop_sequence` or short `tool_use` timeouts) or DM Coach
        # with an actionable summary (longer timeouts, unknown shapes).
        # A `ResultMessage(is_error=True)` returned cleanly from the
        # SDK but the model's turn reported failure. Skip Coach's own
        # errors (nothing to notify) and compact turns (internal).
        if (
            msg.is_error
            and agent_id != "coach"
            and agent_id != "system"
            and (turn_ctx is None or not turn_ctx.get("compact_mode"))
            # max_turns / max_tokens has its own auto-continue path
            # below; don't double-handle.
            and not _looks_like_max_turns(subtype, stop_reason)
        ):
            policy = _soft_error_retry_policy(
                stop_reason, subtype, duration_ms,
            )
            if policy["retry"]:
                # Auto-retry path: bump the consecutive-error counter
                # for cap accounting, schedule a delayed wake. Mark
                # turn_ctx so the post-result suppress block (line
                # ~5717) doesn't reset the counter while a retry is
                # in flight.
                _consecutive_errors[agent_id] = (
                    _consecutive_errors.get(agent_id, 0) + 1
                )
                if turn_ctx is not None:
                    turn_ctx["soft_error_retry_scheduled"] = True
                last_tool = (
                    turn_ctx.get("last_tool") if turn_ctx else None
                )
                await _emit(
                    agent_id,
                    "soft_error_retry_scheduled",
                    stop_reason=str(stop_reason or ""),
                    subtype=str(subtype or ""),
                    duration_ms=duration_ms,
                    delay_s=policy["delay_s"],
                    attempt=_consecutive_errors[agent_id],
                    last_tool=last_tool,
                )
                await _schedule_post_error_retry(
                    agent_id,
                    delay_s_override=policy["delay_s"],
                    accept_idle_status=True,
                )
            else:
                # No-retry path: send the enriched DM so Coach has
                # enough context to decide. Debounced per-agent.
                now_m = time.monotonic()
                last = _last_error_dm_to_coach.get(agent_id, 0.0)
                if now_m - last >= ERROR_DM_DEBOUNCE_SECONDS:
                    _last_error_dm_to_coach[agent_id] = now_m
                    reason = str(stop_reason or "error")
                    last_tool = (
                        turn_ctx.get("last_tool") if turn_ctx else None
                    )
                    tool_hint = (
                        f" Last tool: `{last_tool}`."
                        if last_tool else ""
                    )
                    await _deliver_system_message(
                        from_id=agent_id,
                        to_id="coach",
                        subject=f"{agent_id} turn errored ({reason})",
                        body=(
                            f"My last turn ended with is_error=True "
                            f"(stop_reason={reason}, duration="
                            f"{int((duration_ms or 0) / 1000)}s). The "
                            f"harness didn't classify this shape as "
                            f"transient — no auto-retry.{tool_hint} "
                            f"Decide whether to re-prompt me, reassign, "
                            f"or investigate via /api/events."
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
                # Full long-form handoff lives on disk (cloud-drive-mirrored).
                # The continuity_note stored in the DB is a short pointer
                # that gets injected into the next system prompt.
                handoff_file = await _write_handoff_file(
                    agent_id, summary_with_footer,
                )
                # The summary itself is what gets injected into fresh-you's
                # system prompt. The file is a durable copy (cloud-drive-
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
                # Use _clear_session_id (issues UPDATE … = NULL) rather
                # than _set_session_id(None) — the latter early-returns
                # on falsy values so the column would NOT actually be
                # cleared, and the next turn would resume the prior
                # session AND inject the handoff (defeats /compact's
                # whole point of freeing context).
                await _clear_session_id(agent_id)
                # Freeze the exchange log we just quoted into the
                # handoff; the next session starts with an empty buffer
                # so a later compact doesn't re-quote pre-compact turns.
                await _clear_exchange_log(agent_id)
                # Transfer-mode (compact + flip): apply the runtime
                # change now that the handoff is durable, and emit
                # `session_transferred` instead of `session_compacted`
                # so the UI labels the boundary correctly. The
                # continuity_note we just wrote will be injected into
                # the new runtime's first system prompt.
                _xfer_to = (turn_ctx.get("transfer_to_runtime") or "").strip().lower()
                if _xfer_to in ("claude", "codex"):
                    _xfer_from = await _resolve_runtime_for(agent_id)
                    await _perform_runtime_transfer_flip(agent_id, _xfer_to)
                    await _emit(
                        agent_id,
                        "session_transferred",
                        from_runtime=_xfer_from,
                        to_runtime=_xfer_to,
                        chars=len(summary),
                        handoff_file=handoff_file,
                    )
                else:
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
                    # _clear_session_id issues an explicit NULL update;
                    # _set_session_id(None) early-returns and leaves the
                    # column intact, which would re-trip the threshold
                    # loop on the very next turn — the exact deadlock
                    # this branch is meant to escape.
                    await _clear_session_id(agent_id)
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
                # Transfer requested but the compact yielded no summary
                # — refuse to flip the runtime. The semantic of transfer
                # is "carry forward via summary"; flipping with empty
                # context is just a destructive blind switch. Emit
                # session_transfer_failed so the UI tells the user
                # what happened and the runtime stays put.
                _xfer_to = (turn_ctx.get("transfer_to_runtime") or "").strip().lower()
                if _xfer_to in ("claude", "codex"):
                    await _emit(
                        agent_id,
                        "session_transfer_failed",
                        to_runtime=_xfer_to,
                        reason=(
                            "compact turn produced no summary; runtime "
                            "not flipped. Retry the transfer or use "
                            "PUT /api/agents/{id}/runtime for a blunt flip."
                        ),
                    )
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
                # Kanban v2 (§9.3): stamp `read_by_coach_at` on every
                # project_events row Coach's prompt surfaced this turn.
                # Failed-turn ids stay unread (we never reach this
                # branch) and roll forward to the next tick.
                surfaced_ids = turn_ctx.get("surfaced_event_ids") or []
                if surfaced_ids:
                    try:
                        await _stamp_events_read_by_coach(
                            list(surfaced_ids)
                        )
                    except Exception:
                        logger.exception(
                            "stamp_events_read_by_coach call failed"
                        )
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
                runtime=turn_ctx.get("runtime") or "claude",
                cost_basis=turn_ctx.get("cost_basis") or "token_priced",
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


AGENT_DAILY_CAP_USD = float(os.environ.get("HARNESS_AGENT_DAILY_CAP") or "5.0")
TEAM_DAILY_CAP_USD = float(os.environ.get("HARNESS_TEAM_DAILY_CAP") or "20.0")

# Currently-running agent tasks, keyed by slot id. Used by the cancel
# endpoint to abort a spiraling run without waiting for max_turns / cap.
# Populated by run_agent; cleared on completion (success or error).
_running_tasks: dict[str, asyncio.Task[Any]] = {}

# Global pause switch: when True, run_agent rejects new starts (emits
# a 'paused' event) and the recurrence scheduler skips firing rows.
# In-flight turns are NOT cancelled — to stop those, use POST
# /api/agents/<id>/cancel. In-memory only: a restart lifts the pause
# automatically.
_paused = False


AGENT_WORKING_STALE_SECONDS = int(
    os.environ.get("HARNESS_AGENT_WORKING_STALE_SECONDS", "900")
)


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
        return await repair_stale_working_status(agent_id, force=True)
    task.cancel()
    return True


async def cancel_all_agents() -> list[str]:
    """Cancel every in-flight run. Returns the list of agent ids that
    were actually cancelled (skips already-finished tasks)."""
    cancelled: list[str] = []
    for agent_id in list(_running_tasks.keys()):
        if await cancel_agent(agent_id):
            cancelled.append(agent_id)
    for sid in ["coach"] + [f"p{i}" for i in range(1, 11)]:
        if sid not in cancelled:
            await repair_stale_working_status(sid, force=True)
    return cancelled


def _heartbeat_age_seconds(value: Any) -> float | None:
    if not value:
        return None
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (
            datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        ).total_seconds()
    except Exception:
        return None


async def repair_stale_working_status(agent_id: str, *, force: bool = False) -> bool:
    """Clear a persisted working flag when no in-process turn exists.

    Transport crashes can leave ``agents.status='working'`` even after the
    asyncio task is gone. That blocks Coach ticks and makes the UI stop
    button return "not running". The in-memory task map is authoritative
    inside this process; when it has no live task, a working DB row is
    repairable once stale, or immediately for an explicit cancel request.
    """
    if is_agent_running(agent_id):
        return False
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, last_heartbeat FROM agents WHERE id = ?",
            (agent_id,),
        )
        row = await cur.fetchone()
        if not row:
            return False
        data = dict(row)
        if data.get("status") != "working":
            return False
        age = _heartbeat_age_seconds(data.get("last_heartbeat"))
        if not force and (
            age is None or age < AGENT_WORKING_STALE_SECONDS
        ):
            return False
        now = _now()
        await c.execute(
            "UPDATE agents SET status = 'idle', last_heartbeat = ? WHERE id = ?",
            (now, agent_id),
        )
        await c.commit()
    finally:
        await c.close()
    await _emit(
        agent_id,
        "agent_stale_status_repaired",
        force=force,
        stale_seconds=age,
    )
    await _emit(agent_id, "agent_stopped")
    return True

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
    "who joined the project today.\n\n"
    "Two safety nets exist outside this document, so you can lean on "
    "them rather than try to substitute for them:\n"
    "1. The harness preserves the most recent exchanges verbatim and "
    "injects them into the next session's system prompt automatically.\n"
    "2. The FULL session transcript (every message, tool call, and "
    "tool result) is still on disk as a JSONL file. Fresh-you can "
    "read it on demand. The harness auto-appends a footer pointing at "
    "the exact path — do NOT include such a footer yourself.\n\n"
    "This document is the index + summary; the JSONL is the source of "
    "truth. Optimize for scannability — fresh-you should be able to "
    "find any piece of context in seconds, then drop into the JSONL "
    "for verbatim detail when the summary glosses over it.\n\n"
    "Target length: 1500-3000 words. Err on the side of MORE detail "
    "rather than less — this file is saved to disk, not replayed in "
    "every prompt, so length is cheap and missing context is "
    "expensive. Do not abbreviate. Do not write 'see above' or 'as "
    "discussed' — spell it out.\n\n"
    "Use exactly these markdown sections, in this order. Within each "
    "section, use sub-headings, bullet lists, and code blocks freely. "
    "If a section genuinely doesn't apply, write 'None this session.' "
    "rather than omitting it — fresh-you needs to know you considered "
    "it.\n\n"
    "## Primary request and intent\n"
    "What the operator originally asked for, in their words; the "
    "broader purpose this session serves in their work; and any "
    "side-requests or scope additions layered on during the session. "
    "Capture pivots explicitly — not just the final scope. Quote the "
    "original ask verbatim if possible.\n\n"
    "## Key technical concepts\n"
    "Glossary of domain terms, frameworks, custom abbreviations, "
    "invariants, and named entities used during the session. One line "
    "per term — just enough that fresh-you can read the rest of this "
    "doc fluently. Include both project-wide vocabulary that came up "
    "and any session-specific nicknames or shorthand.\n\n"
    "## All operator messages (verbatim, in order)\n"
    "Every message the operator (human) sent during this session, "
    "QUOTED VERBATIM, in chronological order, numbered. Include "
    "interruptions and one-word replies ('go', 'continue', 'no'). "
    "This preserves their voice — corrections, pivots, exact phrasing "
    "— that paraphrase loses. The harness only preserves the last few "
    "exchanges verbatim by default; everything older is lost without "
    "this section.\n\n"
    "## How we got here\n"
    "Chronological narrative of the session's arc: what was tried, "
    "what worked, what didn't, dead ends ruled out and why. Fresh-you "
    "reading this should understand the path of reasoning, not just "
    "the final state. If this session has been running inside a "
    "recurring workflow pattern (stepping through a list, iterating "
    "on revisions, babysitting a long job), describe that pattern "
    "explicitly so fresh-you knows whether to resume in the same "
    "mode. 2-5 paragraphs.\n\n"
    "## Files touched\n"
    "Per-file inventory of every file you read or modified this "
    "session. For each file:\n"
    "- **Path**.\n"
    "- **State**: **touched** (you modified it) or **read-only** "
    "(you only read it — pre-existing state you inherited).\n"
    "- **What changed / what's relevant**: one paragraph. For touched "
    "files, include the actual diff or key snippet inline as a code "
    "block when it matters.\n\n"
    "Recent / most-relevant files get verbatim snippets; older ones "
    "get one line each. The goal is that recent work is replayable "
    "and old work is findable.\n\n"
    "## Errors & fixes\n"
    "One entry per failure encountered. For each:\n"
    "- **Symptom**: the exact error message or wrong behavior, quoted.\n"
    "- **Root cause**: what was actually wrong.\n"
    "- **Fix**: what you changed.\n"
    "- **Regression test** (if any): the test name or path.\n\n"
    "Don't summarize — these are the things fresh-you will hit again "
    "if the diagnosis is lost.\n\n"
    "## Key findings & decisions\n"
    "Facts established and decisions made this session that aren't "
    "already in decisions/ or knowledge/. For each: **what**, **why**, "
    "**who agreed**. These are the things that would be LOST if this "
    "handoff fails.\n\n"
    "## Open questions\n"
    "Everything still undecided or pending clarification. Quote each "
    "question VERBATIM as asked (by the human or by you to them). "
    "Include enough surrounding context that fresh-you can answer "
    "without asking 'wait, which X?'.\n\n"
    "## References\n"
    "Non-file artifacts: URLs fetched, exact error messages not "
    "already in Errors & fixes, commit hashes, command output "
    "snippets, external ticket / spec / PR links. One line each "
    "unless verbatim is essential.\n\n"
    "## People & roles\n"
    "Who participated (operator + any agents you messaged), what each "
    "is responsible for, stated preferences or pet-peeves that came "
    "up, and anyone fresh-you should reach out to first.\n\n"
    "## Context quirks & gotchas\n"
    "Anything peculiar about the current setup that isn't a discrete "
    "error — tools that misbehaved, assumptions you agreed to, user "
    "preferences mentioned in passing, environmental weirdness, SDK "
    "version sensitivities. One paragraph each.\n\n"
    "## In-flight state at compact\n"
    "What was happening in the VERY LAST turn before this compact:\n"
    "- The literal text of your most recent assistant message before "
    "this prompt (quote it verbatim — even if it was mid-sentence).\n"
    "- The last tool call you made or were about to make.\n"
    "- The exact next action you would have taken if not for the "
    "compact.\n\n"
    "Distinct from 'Primary request and intent' (that's the goal); "
    "this is the immediate suspended state.\n\n"
    "## Pending — concrete checklist\n"
    "Structured todo list, in execution order. Format each item as:\n"
    "`- [ ] Action — owner — blocking? / ready / follow-up`\n\n"
    "Include items waiting on the operator, items waiting on other "
    "agents, and items you can pick up immediately.\n\n"
    "Reply with ONLY the markdown — no preamble, no sign-off, no "
    "'Here is the handoff:'. Do NOT include a footer pointing at the "
    "session JSONL or this handoff file — the harness appends that "
    "automatically."
)


# Recurrence v2 (Docs/recurrence-specs.md): the legacy
# `coach_tick_loop` / `coach_repeat_loop` pair, the
# `_coach_tick_interval` / `_coach_repeat_*` module globals, the
# `set/get_coach_interval` and `set/get_coach_repeat` accessors, and
# the COACH_TICK_PROMPT constant were all deleted in phase 8. The
# unified `recurrence_scheduler_loop` (server/recurrences.py) now
# drives every Coach tick / repeat / cron from rows in the
# `coach_recurrence` table; per-fire prompts come from
# `compose_tick_prompt(project_id)`.
#
# `HARNESS_COACH_TICK_INTERVAL` is honored only on first migration
# (db._seed_recurrence_from_env) — see CLAUDE.md "Known gotchas" for
# the deprecation note.


# Auto-wake: when Coach assigns a task to p3 or messages p3, we start
# a turn for p3 with a wake prompt so the Player actually engages —
# without this, assignments just sit in the DB doing nothing. Debounce
# prevents tight ping-pong loops: if an agent finished a turn within
# AUTOWAKE_DEBOUNCE_SECONDS, skip. Independent of the Coach autoloop.
AUTOWAKE_DEBOUNCE_SECONDS = int(
    os.environ.get("HARNESS_AUTOWAKE_DEBOUNCE", "10")
)
_last_turn_ended_at: dict[str, float] = {}

# Queue-on-busy: when maybe_wake_agent fires for a slot that is
# already mid-turn, we stash the args here instead of dropping the
# wake. After the turn ends (in run_agent's finally), the deferred
# fire calls maybe_wake_agent again so a fresh turn picks up the
# queued reason. Latest-wins coalescing — multiple wakes during a
# single busy stretch fold into one follow-up turn (the inbox /
# project_events tables retain the actual content; the prompt is
# just the most recent trigger). Tuple shape:
#   (reason, wake_source, plan_mode)
# The deferred fire ALWAYS bypasses debounce: the wake was generated
# by an event arriving during the turn, not in reaction to the turn's
# own output, so the ping-pong guard doesn't apply. The just-stamped
# `_last_turn_ended_at` value would otherwise drop the deferred fire
# at microsecond age.
_pending_wakes: dict[str, tuple[str, str | None, bool | None]] = {}

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
    os.environ.get("HARNESS_ERROR_RETRY_DELAY") or "45"
)
ERROR_RETRY_MAX_CONSECUTIVE = int(
    os.environ.get("HARNESS_ERROR_RETRY_MAX_CONSECUTIVE") or "3"
)

# SDK ClaudeAgentOptions.max_turns — the per-spawn ceiling on the
# model's tool-use roundtrips. The SDK terminates with is_error=True
# / subtype="error_max_turns" when this trips, even if the agent
# would otherwise have finished cleanly. The previous default of 10
# was too tight for an autonomous Coach (read inbox + list tasks +
# plan + write a TodoWrite already burns most of it). 50 leaves
# plenty of headroom for ordinary workflows; the daily cost cap
# remains the brake against a runaway turn.
MAX_TURNS_PER_SPAWN = int(os.environ.get("HARNESS_MAX_TURNS") or "50")

# Soft-error tracking — last-turn diagnostics surfaced into the next
# spawn's system prompt so a follow-up turn knows why the previous
# one ended with is_error=True. Cleared after the next successful
# spawn consumes it (one-shot — we don't want the note to haunt
# every future turn). In-memory only: a process restart wipes it,
# which is fine because a restart implies a fresh CLI session anyway.
_last_turn_error_info: dict[str, dict[str, Any]] = {}

# Sticky fingerprint of the most recent prior_error_suffix surfaced into
# an agent's prompt. Set when run_agent injects the suffix, cleared
# only when a clean (non-error) ResultMessage arrives. The
# ResultMessage handler reads this to suppress re-arming
# `_last_turn_error_info` when a fresh error has the same shape as the
# one we already showed once — kills the recurrence-tick loop observed
# 2026-05-11 (prior_error=289 chars persisted across 3.5 hours of
# identical Coach prompts). Cross-turn lifetime is essential here:
# the spawn POPS _last_turn_error_info, so a per-turn-only tracker
# would only suppress every OTHER turn, producing an ON/OFF
# oscillation instead of the desired show-once-then-quiet.
_last_shown_prior_error_fp: dict[str, tuple[str | None, str | None, int | None]] = {}

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


async def _load_cost_resets() -> tuple[str, dict[str, str]]:
    """Read cost-reset timestamps from team_config.

    Returns `(global_reset, per_project_reset_map)` where each value
    is an ISO timestamp string (or "" if unset). The semantics: a
    turn row counts toward "today" when ended_at >=
    MAX(today_utc_start, global_reset, per_project_reset[row.project_id]).

    `cost_reset_at` (global) — set by POST /api/turns/reset {scope:"all"}.
    `cost_reset_at_<project_id>` — set by POST /api/turns/reset
    {scope:"<project_id>"}.
    """
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT key, value FROM team_config WHERE key = 'cost_reset_at' "
            "OR key LIKE 'cost_reset_at_%'"
        )
        rows = await cur.fetchall()
    finally:
        await c.close()
    global_reset = ""
    per_project: dict[str, str] = {}
    for r in rows:
        d = dict(r)
        k, v = d["key"], d["value"] or ""
        if k == "cost_reset_at":
            global_reset = v
        elif k.startswith("cost_reset_at_"):
            per_project[k[len("cost_reset_at_"):]] = v
    return global_reset, per_project


async def _today_spend(
    agent_id: str | None = None,
    project_id: str | None = None,
) -> float:
    """Sum `turns.cost_usd` for rows that ended today (UTC). Pass
    agent_id to scope to one slot. Pass project_id to scope to one
    project. Both can be combined.

    Honors `cost_reset_at` (global) and `cost_reset_at_<project>`
    (per-project) timestamps from team_config — a row only counts
    when its `ended_at` is >= the latest applicable reset (or the
    UTC day start, whichever is later). The reset is a "give myself
    fresh budget for the rest of today" knob; historical turn rows
    stay intact.

    Uses the turns ledger (indexed on agent_id + ended_at) rather
    than the events table (which would require JSON extraction on
    every row). Same answer, single-index lookup. The ledger is
    populated by every ResultMessage via _insert_turn_row.
    """
    today_start = _today_utc_start_iso()
    global_reset, per_project = await _load_cost_resets()

    base_window = max(today_start, global_reset) if global_reset else today_start
    where_parts: list[str] = []
    params: list[Any] = []

    if project_id:
        # Single-project query: compute that project's effective
        # window directly. No CASE needed since the WHERE filter
        # excludes every other project's rows anyway.
        pp = per_project.get(project_id) or ""
        window = pp if pp and pp > base_window else base_window
        where_parts.append("ended_at >= ?")
        params.append(window)
        where_parts.append("project_id = ?")
        params.append(project_id)
    else:
        # Team-wide query (with or without agent_id filter): per-row
        # CASE picks each row's project's effective window so the
        # team total naturally equals the sum of per-project today
        # values. Per-project reset only contributes a CASE branch
        # when LATER than base_window (otherwise base wins anyway).
        case_branches: list[str] = []
        for pid, ts in per_project.items():
            if ts and ts > base_window:
                case_branches.append("WHEN project_id = ? THEN ?")
                params.extend([pid, ts])
        if case_branches:
            window_expr = "CASE " + " ".join(case_branches) + " ELSE ? END"
            params.append(base_window)
        else:
            window_expr = "?"
            params.append(base_window)
        where_parts.append(f"ended_at >= {window_expr}")

    if agent_id:
        where_parts.append("agent_id = ?")
        params.append(agent_id)
    where = "WHERE " + " AND ".join(where_parts)

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


async def _get_agent_identity(agent_id: str) -> dict[str, Any]:
    """Read identity columns from agent_project_roles for the active
    project. Returns {} for system. Missing row → all None.

    Returned keys: name, role, brief, model_override, effort_override
    (int|None), plan_mode_override (int|None — 1/0/None tri-state),
    thinking_override (int|None — 1/0/None tri-state).
    """
    if agent_id == "system":
        return {}
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT name, role, brief, model_override, "
                "effort_override, plan_mode_override, thinking_override "
                "FROM agent_project_roles "
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
        return {
            "name": None, "role": None, "brief": None,
            "model_override": None, "effort_override": None,
            "plan_mode_override": None, "thinking_override": None,
        }
    d = dict(row)
    return {
        "name": d.get("name"),
        "role": d.get("role"),
        "brief": d.get("brief"),
        "model_override": d.get("model_override"),
        "effort_override": d.get("effort_override"),
        "plan_mode_override": d.get("plan_mode_override"),
        "thinking_override": d.get("thinking_override"),
    }


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


async def _resolve_runtime_for(agent_id: str) -> str:
    """Resolve which runtime to use for a slot.

    Order: agents.runtime_override (per-slot) → team_config role
    default (`coach_default_runtime` / `players_default_runtime`) →
    `'claude'`. See Docs/CODEX_RUNTIME_SPEC.md §B.1.

    Always returns a non-empty string so callers can safely call
    `get_runtime(name)` without a default.
    """
    role_key = "coach_default_runtime" if agent_id == "coach" else "players_default_runtime"
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT runtime_override FROM agents WHERE id = ?",
                (agent_id,),
            )
            row = await cur.fetchone()
            override = (dict(row).get("runtime_override") if row else None) or ""
            if override in ("claude", "codex"):
                return override
            cur = await c.execute(
                "SELECT value FROM team_config WHERE key = ?",
                (role_key,),
            )
            row = await cur.fetchone()
            default = (dict(row).get("value") if row else "") or ""
            if default in ("claude", "codex"):
                return default
        finally:
            await c.close()
    except Exception:
        logger.exception("runtime resolution failed for %s; defaulting to claude", agent_id)
    return "claude"


async def _get_agent_brief(agent_id: str) -> str | None:
    """Read brief from agent_project_roles for the active project."""
    ident = await _get_agent_identity(agent_id)
    v = ident.get("brief") if ident else None
    return v if v else None


async def _get_agent_model_override(agent_id: str) -> str | None:
    """Read agent_project_roles.model_override for the active project.

    Coach-set per-(slot, project) model preference. Sits between the
    per-pane override (request `model` arg) and the runtime-aware role
    default in the resolution chain. None when unset / cleared.

    Dedicated query (does not piggyback on `_get_agent_identity`) so a
    SQLite hiccup logs against this lookup specifically rather than
    being attributed to the broader identity read.
    """
    if agent_id == "system":
        return None
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT model_override FROM agent_project_roles "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception(
            "get_agent_model_override failed: agent=%s project=%s",
            agent_id, project_id,
        )
        return None
    if not row:
        return None
    v = dict(row).get("model_override")
    return v if v else None


async def _get_agent_effort_override(agent_id: str) -> int | None:
    """Read agent_project_roles.effort_override (1..4) for the active project.

    Coach-set per-(slot, project) effort tier. Returned int feeds straight
    into the existing `EFFORT_LITERALS` mapping. None when unset / cleared
    / invalid (defensive coercion — out-of-range values are treated as
    unset rather than raising).
    """
    if agent_id == "system":
        return None
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT effort_override FROM agent_project_roles "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception(
            "get_agent_effort_override failed: agent=%s project=%s",
            agent_id, project_id,
        )
        return None
    if not row:
        return None
    raw = dict(row).get("effort_override")
    if raw is None:
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if 1 <= v <= 4 else None


async def _get_agent_plan_mode_override(agent_id: str) -> bool | None:
    """Read agent_project_roles.plan_mode_override for the active project.

    Tri-state: True = plan mode forced on, False = plan mode forced off
    (overrides a per-pane true), None = no override (fall through to per-pane
    request → False default).
    """
    if agent_id == "system":
        return None
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT plan_mode_override FROM agent_project_roles "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception(
            "get_agent_plan_mode_override failed: agent=%s project=%s",
            agent_id, project_id,
        )
        return None
    if not row:
        return None
    raw = dict(row).get("plan_mode_override")
    if raw is None:
        return None
    return bool(int(raw)) if str(raw) in ("0", "1") else bool(raw)


async def _get_agent_thinking_override(agent_id: str) -> bool | None:
    """Read agent_project_roles.thinking_override for the active project.

    Tri-state: True = extended thinking forced on, False = explicit off,
    None = no override (default — thinking stays off). Claude runtime
    only; Codex spawn ignores this value silently. Budget comes from
    HARNESS_THINKING_BUDGET_TOKENS at runtime kwarg-build time.
    """
    if agent_id == "system":
        return None
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT thinking_override FROM agent_project_roles "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception(
            "get_agent_thinking_override failed: agent=%s project=%s",
            agent_id, project_id,
        )
        return None
    if not row:
        return None
    raw = dict(row).get("thinking_override")
    if raw is None:
        return None
    return bool(int(raw)) if str(raw) in ("0", "1") else bool(raw)


def _model_fits_runtime(model: str, runtime_name: str) -> bool:
    """True when `model` belongs to `runtime_name`'s family.

    Positive enumeration via `models_catalog.model_is_claude` /
    `model_is_codex` so a future Anthropic id without the `claude-`
    prefix isn't silently misclassified. Used at spawn time to drop a
    Coach-set override that no longer matches the player's current
    runtime — a soft fallback rather than a hard error.
    """
    from server.models_catalog import model_is_claude, model_is_codex
    if not model:
        return False
    if runtime_name == "claude":
        return model_is_claude(model)
    if runtime_name == "codex":
        return model_is_codex(model)
    return False


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
    - The handoff .md file itself (cloud-drive-mirrored when configured).
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
    HANDOFFS_DIR (cloud-drive-mirrored when available) and return the
    relative filename. The continuity_note stored in the DB is only a
    pointer — this file is the authoritative, long-form handoff that
    fresh-you can Read() on demand.

    Filename: <agent_id>-<YYYYMMDD-HHMMSS>.md. Returns None if both
    the cloud-drive and the local write failed — we log but don't
    raise, because an empty handoff doesn't justify losing the compact
    turn itself."""
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
            logger.exception("handoff cloud-drive write failed: %s", filename)
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


async def _compose_handoff_suffix(agent_id: str) -> str:
    """Format the post-compact handoff block for an agent's system prompt.

    Reads `agent_sessions.continuity_note` (a short summary written by
    `/compact` or by the auto-heal path when a session_id was nulled
    after a `ProcessError` on resume) and appends the recent exchanges
    from `agent_sessions.last_exchange_json`. Returns "" when no
    continuity note is set — caller should not append anything.

    Used both by `run_agent` (normal post-compact handoff) and by the
    Claude runtime's stale-session auto-heal (synthetic note path) so
    a freshly-cleared session still carries memory across the boundary.
    """
    note = await _get_continuity_note(agent_id)
    if not note:
        return ""
    suffix = (
        "\n\n## Handoff from your prior session (via /compact)\n\n"
        + note.strip()
    )
    # Append the recent exchanges verbatim — CLI /compact keeps recent
    # turns intact, and 1 exchange was too thin (tool chains span
    # multiple turns). The rolling list is maintained by _append_exchange
    # on every successful non-compact turn, trimmed from the head so the
    # total stays under the handoff token budget. The full session
    # transcript lives in the jsonl file; this injected log is just a
    # warm-start, not the long-term record.
    recent = await _get_recent_exchanges(agent_id)
    recent = [
        e for e in recent
        if isinstance(e, dict)
        and isinstance(e.get("prompt"), str)
        and isinstance(e.get("response"), str)
        and (e.get("prompt") or e.get("response"))
    ]
    if recent:
        suffix += (
            f"\n\n### Recent exchanges (verbatim, last "
            f"{len(recent)} turn{'s' if len(recent) != 1 else ''} "
            "before compact, oldest first)\n"
        )
        for i, e in enumerate(recent, start=1):
            suffix += (
                f"\n#### Exchange {i} of {len(recent)}\n\n"
                "**User asked:**\n\n"
                + (e["prompt"] or "").strip()
                + "\n\n**You replied:**\n\n"
                + (e["response"] or "").strip()
                + "\n"
            )
    suffix += (
        "\n\n(Your previous conversation history has been cleared "
        "to free context. The summary + verbatim exchanges above "
        "are your memory of what came before.)"
    )
    return suffix


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


_TRAJECTORY_TOKENS = {
    "plan": "P",
    "execute": "E",
    "audit_syntax": "AY",
    "audit_semantics": "AE",
    "ship": "S",
}


def _trajectory_marker(trajectory_json: Any, current_stage: str | None) -> str:
    """Render a trajectory like `P → [E] → AY → S` with the current
    stage in brackets. Returns '' if the trajectory column is empty
    or unparseable. Pure rendering helper — used in Coach's open-tasks
    rollup and (mirrored shape) on the kanban card UI."""
    try:
        traj = json.loads(trajectory_json or "[]")
    except (TypeError, ValueError):
        return ""
    tokens: list[str] = []
    for stage_obj in traj:
        if not isinstance(stage_obj, dict):
            continue
        stage = stage_obj.get("stage")
        token = _TRAJECTORY_TOKENS.get(stage)
        if not token:
            continue
        if stage == current_stage:
            tokens.append(f"[{token}]")
        else:
            tokens.append(token)
    if not tokens:
        return ""
    return "→".join(tokens)


# Maximum number of tasks shown in the `## Active task health` Coach block.
# Caps O(N_tasks) growth to O(1) — H1 efficiency fix (2026-05-14).
ACTIVE_TASK_HEALTH_CAP = 3


async def _build_active_task_health_rows(
    project_id: str,
) -> list[dict[str, Any]]:
    """Surface tasks where the SAME audit kind has failed >= 2 times.
    Coach reads this to decide whether to bump the executor's effort,
    flip extended thinking on, or bump the model tier. First fail =
    expected correction noise (per the quality-feedback policy); 2nd+
    fail = signal."""
    rows: list[dict[str, Any]] = []
    try:
        c = await configured_conn()
        try:
            # Read every non-archived task with an executor + count fails per kind.
            cur = await c.execute(
                """
                SELECT t.id AS task_id, t.title, t.owner AS executor,
                       t.last_stage_change_at,
                       SUM(CASE WHEN tra.role = 'auditor_syntax'
                                AND tra.verdict = 'fail'
                           THEN 1 ELSE 0 END) AS syntax_fails,
                       SUM(CASE WHEN tra.role = 'auditor_semantics'
                                AND tra.verdict = 'fail'
                           THEN 1 ELSE 0 END) AS semantics_fails,
                       MAX(CASE WHEN tra.role IN ('auditor_syntax', 'auditor_semantics')
                                THEN tra.verdict END) AS latest_verdict
                FROM tasks t
                LEFT JOIN task_role_assignments tra
                  ON tra.task_id = t.id
                WHERE t.project_id = ? AND t.status != 'archive'
                GROUP BY t.id
                """,
                (project_id,),
            )
            raw = [dict(r) for r in await cur.fetchall()]

            # Resolve executor effort/model overrides per row.
            for r in raw:
                syn = int(r.get("syntax_fails") or 0)
                sem = int(r.get("semantics_fails") or 0)
                kind = None
                fail_count = 0
                if syn >= 2 and syn >= sem:
                    kind, fail_count = "audit_syntax", syn
                elif sem >= 2:
                    kind, fail_count = "audit_semantics", sem
                if not kind:
                    continue
                executor = r.get("executor") or "—"
                effort = (
                    await _get_agent_effort_override(executor)
                    if executor and executor != "—"
                    else None
                )
                model = (
                    await _get_agent_model_override(executor)
                    if executor and executor != "—"
                    else None
                )
                rows.append({
                    "task_id": r["task_id"],
                    "title": (r.get("title") or "").strip(),
                    "executor": executor,
                    "kind": kind,
                    "kind_fail_count": fail_count,
                    "latest_verdict": r.get("latest_verdict") or "?",
                    "executor_effort": effort,
                    "executor_model": model,
                    "last_stage_change_at": r.get("last_stage_change_at") or "",
                })
        finally:
            await c.close()
    except Exception:
        return []
    # Sort by fail_count DESC, tiebreak by last_stage_change_at DESC.
    rows.sort(
        key=lambda r: (r["kind_fail_count"], r.get("last_stage_change_at") or ""),
        reverse=True,
    )
    return rows


_STAGE_TO_ROLE = {
    "plan": "planner",
    "execute": "executor",
    "audit_syntax": "auditor_syntax",
    "audit_semantics": "auditor_semantics",
    "ship": "shipper",
}


async def _build_stalled_tasks_rows(
    project_id: str,
) -> list[dict[str, Any]]:
    """Tasks that crossed the stall threshold and have not progressed
    since (`stale_alert_at IS NOT NULL AND last_stage_change_at =
    stale_alert_at`).

    v0.3.4 bug-fix: the surfaced 'owner' field is the CURRENT STAGE's
    assignee — the Player actually responsible for the next move —
    not `tasks.owner`, which is always the executor. Previously a
    stuck audit_semantics task would name the executor as the
    blocker, leading Coach to nudge the wrong Player.

    v0.3.8: rows now carry `escalation_level` so the Coach rollup can
    label which rung of the auto-action ladder each stall is on.
    """
    rows: list[dict[str, Any]] = []
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id, title, status, owner, last_stage_change_at, "
                "stale_alert_at, stall_escalation_level FROM tasks "
                "WHERE project_id = ? AND status != 'archive' "
                "AND stale_alert_at IS NOT NULL "
                "AND last_stage_change_at IS NOT NULL "
                "ORDER BY last_stage_change_at ASC LIMIT 10",
                (project_id,),
            )
            raw = [dict(r) for r in await cur.fetchall()]
            # Resolve each task's current-stage assignee in a single
            # extra round-trip per row (cheap; capped at 10).
            stage_owner_map: dict[str, str | None] = {}
            for r in raw:
                role = _STAGE_TO_ROLE.get(r.get("status") or "")
                if not role:
                    stage_owner_map[r["id"]] = None
                    continue
                cur = await c.execute(
                    "SELECT owner FROM task_role_assignments "
                    "WHERE task_id = ? AND role = ? "
                    "AND completed_at IS NULL "
                    "AND superseded_by IS NULL "
                    "ORDER BY assigned_at DESC LIMIT 1",
                    (r["id"], role),
                )
                rrow = await cur.fetchone()
                stage_owner_map[r["id"]] = (
                    dict(rrow).get("owner") if rrow else None
                )
        finally:
            await c.close()
    except Exception:
        return []

    now = datetime.now(timezone.utc)
    for r in raw:
        try:
            last = datetime.fromisoformat(
                (r.get("last_stage_change_at") or "").replace("Z", "+00:00")
            )
            age_hours = max(0.0, (now - last).total_seconds() / 3600.0)
        except (TypeError, ValueError):
            age_hours = 0.0
        stage_owner = stage_owner_map.get(r["id"])
        rows.append({
            "task_id": r["id"],
            "title": (r.get("title") or "").strip(),
            "stage": r.get("status") or "?",
            # The Player actually responsible for the next move at
            # this stage. Falls back to tasks.owner when no role row
            # exists (broken state — Coach should fix the trajectory).
            "owner": stage_owner or r.get("owner") or "(unassigned)",
            # Keep the executor visible separately so Coach has full
            # context (e.g., "stuck in audit_semantics owned by p3,
            # original executor was p8").
            "task_executor": r.get("owner") or "(unassigned)",
            "age_hours": round(age_hours, 1),
            # v0.3.8 escalation rung: 0 fresh, 1 nudged, 2 coach-
            # notified, 3 auto-reassigned, 4 auto-archived (terminal).
            "escalation_level": int(
                r.get("stall_escalation_level") or 0
            ),
        })
    return rows


async def _build_unrecorded_artifacts_rows(
    project_id: str,
) -> list[dict[str, Any]]:
    """Recent reconciliation findings: artifacts on disk that the
    kanban hasn't recorded. Read from the events log so they survive
    process restarts (the in-memory dedupe in idle_poller doesn't).

    Each row is one finding (spec OR audit) with the path + the
    suggested Coach override tool. Window: last 24h, capped at 10
    rows so the Coach prompt stays bounded.

    AUDIT FIX (v0.3.8): cross-check current DB state before
    surfacing each finding. The events table keeps fired events
    around for 24h+, but Coach may have already submitted via
    `coord_write_task_spec(on_behalf_of=...)` or
    `coord_submit_audit_report(on_behalf_of=...)` in the meantime.
    Without this cross-check, Coach sees stale findings and
    re-attempts the override (which then errors with "already
    submitted"). For specs: skip when `tasks.spec_path` is set.
    For audits: skip when any `task_role_assignments` row records
    the named `report_path`.
    """
    rows: list[dict[str, Any]] = []
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT type, payload FROM events "
                "WHERE type IN ('task_spec_unrecorded', "
                "'task_audit_unrecorded') "
                "AND (julianday('now') - julianday(ts)) * 86400.0 < 86400 "
                "AND project_id = ? "
                "ORDER BY id DESC LIMIT 30",
                (project_id,),
            )
            raw = await cur.fetchall()
        finally:
            await c.close()
    except Exception:
        return []

    seen: set[tuple[str, str]] = set()
    for r in raw:
        try:
            d = dict(r)
            etype = d.get("type") or ""
            payload = d.get("payload") or "{}"
            if isinstance(payload, str):
                import json as _json
                try:
                    payload = _json.loads(payload)
                except Exception:
                    continue
            task_id = payload.get("task_id") or ""
            if not task_id:
                continue
            if etype == "task_spec_unrecorded":
                k = (task_id, "spec")
                if k in seen:
                    continue
                seen.add(k)
                # Cross-check: if spec_path is now set on the task,
                # the finding is stale.
                if await _spec_path_already_recorded(task_id):
                    continue
                rows.append({
                    "kind": "spec",
                    "task_id": task_id,
                    "path": payload.get("spec_path") or "",
                    "owner": payload.get("planner") or "(unassigned)",
                    "fix": (
                        f"coord_write_task_spec(task_id={task_id!r}, "
                        f"body=<paste from disk>, on_behalf_of="
                        f"{(payload.get('planner') or '<planner>')!r})"
                    ),
                })
            elif etype == "task_audit_unrecorded":
                kind = payload.get("kind") or "?"
                round_num = payload.get("round") or 0
                report_path = payload.get("report_path") or ""
                k = (task_id, f"audit:{round_num}:{kind}")
                if k in seen:
                    continue
                seen.add(k)
                # Cross-check: if any role row recorded this exact
                # report_path, the audit was submitted in the
                # meantime — drop the stale finding.
                if await _audit_report_path_already_recorded(
                    task_id=task_id, report_path=report_path,
                ):
                    continue
                rows.append({
                    "kind": f"audit_{kind}",
                    "task_id": task_id,
                    "path": report_path,
                    "owner": payload.get("auditor") or "(unassigned)",
                    "fix": (
                        f"coord_submit_audit_report(task_id={task_id!r}, "
                        f"kind={kind!r}, body=<paste from disk>, "
                        f"verdict='pass'|'fail', on_behalf_of="
                        f"{(payload.get('auditor') or '<auditor>')!r})"
                    ),
                })
            if len(rows) >= 10:
                break
        except Exception:
            continue
    return rows


async def _spec_path_already_recorded(task_id: str) -> bool:
    """True when `tasks.spec_path` is set for this task (Coach
    already submitted the spec via override). Used by
    `_build_unrecorded_artifacts_rows` to drop stale findings."""
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT spec_path FROM tasks WHERE id = ?",
                (task_id,),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        return False
    if not row:
        return False
    p = dict(row).get("spec_path") or ""
    return bool(p.strip())


async def _audit_report_path_already_recorded(
    *, task_id: str, report_path: str,
) -> bool:
    """True when any auditor role row records the exact report_path
    (Coach already submitted via override). Used by
    `_build_unrecorded_artifacts_rows` to drop stale findings."""
    if not report_path:
        return False
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT 1 FROM task_role_assignments "
                "WHERE task_id = ? AND report_path = ? "
                "AND role IN ('auditor_syntax', 'auditor_semantics') "
                "LIMIT 1",
                (task_id, report_path),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        return False
    return row is not None


async def _build_soft_stalls_rows(
    project_id: str,
) -> list[dict[str, Any]]:
    """Recent watchdog findings (Docs/kanban-specs-v2.md §10.7) — soft
    stalls flagged by the Haiku-tiered watchdog: agents that declared
    completion in chat without advancing the kanban, agents looping
    or erroring, etc.

    Read from the events log so findings survive process restarts
    (the in-memory dedup map in kanban_watchdog doesn't). Window:
    last 1h, capped at 10 rows so the Coach prompt stays bounded.
    Deduped per `(subject_agent, verdict)` so the most recent finding
    per agent wins on rapid re-fires.

    Stale findings (already resolved): if the flagged agent is no
    longer holding the named task — `tasks.status` advanced past the
    stage they were stuck in, or the task got reassigned — the
    finding is dropped before surfacing. Better to under-surface than
    to send Coach chasing already-fixed problems.
    """
    rows: list[dict[str, Any]] = []
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT type, payload, ts FROM events "
                "WHERE type = 'watchdog_finding' "
                "AND (julianday('now') - julianday(ts)) * 86400.0 < 3600 "
                "AND project_id = ? "
                "ORDER BY id DESC LIMIT 30",
                (project_id,),
            )
            raw = await cur.fetchall()
        finally:
            await c.close()
    except Exception:
        return []

    seen: set[tuple[str, str]] = set()
    for r in raw:
        try:
            d = dict(r)
            payload = d.get("payload") or "{}"
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    continue
            slot = (payload.get("subject_agent") or "").strip()
            verdict = (payload.get("verdict") or "").strip()
            if not slot or not verdict:
                continue
            k = (slot, verdict)
            if k in seen:
                continue
            seen.add(k)

            task_id = (payload.get("task_id") or "").strip() or None
            # Cross-check: drop the finding if the task already moved
            # off the stage where the agent was stuck. Same-shape
            # protection as `_build_unrecorded_artifacts_rows`.
            if task_id and await _watchdog_finding_already_resolved(
                slot=slot, task_id=task_id,
            ):
                continue

            rows.append({
                "agent": slot,
                "verdict": verdict,
                "reason": (payload.get("reason") or "").strip()[:200],
                "task_id": task_id,
                "signal": payload.get("signal") or "",
                "ts": d.get("ts"),
            })
            if len(rows) >= 10:
                break
        except Exception:
            continue
    return rows


async def _watchdog_finding_already_resolved(
    *, slot: str, task_id: str,
) -> bool:
    """True when the watchdog finding is stale: either the agent is
    no longer holding the task (`current_task_id != task_id`), or the
    task was archived in the meantime. Coach will see a churn-y rollup
    if we surface findings that are already history."""
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT current_task_id FROM agents WHERE id = ?",
                (slot,),
            )
            agent_row = await cur.fetchone()
            cur = await c.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (task_id,),
            )
            task_row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        return False
    if task_row and (dict(task_row).get("status") == "archive"):
        return True
    if agent_row:
        if dict(agent_row).get("current_task_id") != task_id:
            return True
    return False


# ----------------------------------------------------------------------
# Kanban v2 prompt blocks (Docs/kanban-specs-v2.md §11.1, §11.2, §11.3,
# §9.3). Each builder returns a markdown string (or "" when there's
# nothing to render). They're called from `_build_coach_coordination_block`
# in the §14 order.
# ----------------------------------------------------------------------


async def compute_player_health_counters(
    project_id: str,
) -> list[dict[str, Any]]:
    """Player health counters (§11.1, §15.3). Last 30 days. Returns a
    list of {slot, deviations, push_before_audit, off_spec_completions}
    rows, one per Player with at least one non-zero counter. Empty list
    when every Player's three counters are zero. Shared between the
    Coach prompt block and the `/api/team/player_health` endpoint."""
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT t.owner AS slot, COUNT(*) AS n "
                "FROM task_role_assignments r "
                "JOIN tasks t ON t.id = r.task_id "
                "WHERE t.project_id = ? "
                "AND r.role IN ('auditor_syntax', 'auditor_semantics') "
                "AND r.verdict = 'fail' "
                "AND r.completed_at >= datetime('now', '-30 days') "
                "AND t.owner IS NOT NULL "
                "GROUP BY t.owner",
                (project_id,),
            )
            deviations = {
                dict(r)["slot"]: int(dict(r)["n"]) for r in await cur.fetchall()
            }
            cur = await c.execute(
                "SELECT executor AS slot, COUNT(*) AS n "
                "FROM deviations_log "
                "WHERE project_id = ? "
                "AND noticed_at IN ('push', 'audit') "
                "AND ts >= datetime('now', '-30 days') "
                "GROUP BY executor",
                (project_id,),
            )
            off_spec = {
                dict(r)["slot"]: int(dict(r)["n"]) for r in await cur.fetchall()
            }
            cur = await c.execute(
                """
                SELECT e.agent_id AS slot, COUNT(*) AS n
                FROM events e
                WHERE e.type = 'commit_pushed'
                  AND e.project_id = ?
                  AND e.ts >= datetime('now', '-30 days')
                  AND json_extract(e.payload, '$.task_id') IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM task_role_assignments r2
                      WHERE r2.task_id = json_extract(e.payload, '$.task_id')
                      AND r2.role IN ('auditor_syntax', 'auditor_semantics')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM task_role_assignments r3
                      WHERE r3.task_id = json_extract(e.payload, '$.task_id')
                      AND r3.role IN ('auditor_syntax', 'auditor_semantics')
                      AND r3.verdict = 'pass'
                      AND r3.completed_at >= e.ts
                  )
                GROUP BY e.agent_id
                """,
                (project_id,),
            )
            push_before = {
                dict(r)["slot"]: int(dict(r)["n"]) for r in await cur.fetchall()
            }
        finally:
            await c.close()
    except Exception:
        logger.exception("player_health: query failed")
        return []
    out: list[dict[str, Any]] = []
    for slot in sorted(set(deviations) | set(push_before) | set(off_spec)):
        d = int(deviations.get(slot, 0))
        p = int(push_before.get(slot, 0))
        o = int(off_spec.get(slot, 0))
        if d == 0 and p == 0 and o == 0:
            continue
        out.append({
            "slot": slot,
            "deviations": d,
            "push_before_audit": p,
            "off_spec_completions": o,
        })
    return out


async def _build_player_health_block(project_id: str) -> str:
    """Player health counters (§11.1). Computed at prompt-build from
    existing tables — no separate counter table. Last 30 days, active
    project. Returns "" when every Player's three counters are zero
    so the prompt stays quiet on a healthy team.
    """
    try:
        c = await configured_conn()
        try:
            # deviations: distinct audit FAIL rounds per executor
            cur = await c.execute(
                "SELECT t.owner AS slot, COUNT(*) AS n "
                "FROM task_role_assignments r "
                "JOIN tasks t ON t.id = r.task_id "
                "WHERE t.project_id = ? "
                "AND r.role IN ('auditor_syntax', 'auditor_semantics') "
                "AND r.verdict = 'fail' "
                "AND r.completed_at >= datetime('now', '-30 days') "
                "AND t.owner IS NOT NULL "
                "GROUP BY t.owner",
                (project_id,),
            )
            deviations = {dict(r)["slot"]: int(dict(r)["n"]) for r in await cur.fetchall()}

            # off_spec_completion_count: deviations_log rows with
            # noticed_at IN ('push', 'audit') for this Player as
            # executor.
            cur = await c.execute(
                "SELECT executor AS slot, COUNT(*) AS n "
                "FROM deviations_log "
                "WHERE project_id = ? "
                "AND noticed_at IN ('push', 'audit') "
                "AND ts >= datetime('now', '-30 days') "
                "GROUP BY executor",
                (project_id,),
            )
            off_spec = {dict(r)["slot"]: int(dict(r)["n"]) for r in await cur.fetchall()}

            # push_before_audit_count: commit_pushed events from this
            # slot where the task had any auditor role row planted but
            # no audit_report_submitted{verdict='pass'} for the current
            # execute round at commit time. Approximated via the events
            # table — the precise read would require timeline cross-
            # referencing per commit, which is expensive at prompt-build.
            # The approximation: count `commit_pushed` events whose
            # task has ANY auditor role row planted, filtered by ts.
            cur = await c.execute(
                """
                SELECT e.agent_id AS slot, COUNT(*) AS n
                FROM events e
                WHERE e.type = 'commit_pushed'
                  AND e.project_id = ?
                  AND e.ts >= datetime('now', '-30 days')
                  AND json_extract(e.payload, '$.task_id') IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM task_role_assignments r2
                      WHERE r2.task_id = json_extract(e.payload, '$.task_id')
                      AND r2.role IN ('auditor_syntax', 'auditor_semantics')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM task_role_assignments r3
                      WHERE r3.task_id = json_extract(e.payload, '$.task_id')
                      AND r3.role IN ('auditor_syntax', 'auditor_semantics')
                      AND r3.verdict = 'pass'
                      AND r3.completed_at >= e.ts
                  )
                GROUP BY e.agent_id
                """,
                (project_id,),
            )
            push_before = {dict(r)["slot"]: int(dict(r)["n"]) for r in await cur.fetchall()}
        finally:
            await c.close()
    except Exception:
        logger.exception("player_health: query failed")
        return ""

    slots = sorted(set(deviations) | set(push_before) | set(off_spec))
    if not slots:
        return ""
    rows: list[tuple[str, int, int, int]] = []
    for slot in slots:
        d = int(deviations.get(slot, 0))
        p = int(push_before.get(slot, 0))
        o = int(off_spec.get(slot, 0))
        if d == 0 and p == 0 and o == 0:
            continue
        rows.append((slot, d, p, o))
    if not rows:
        return ""
    out: list[str] = []
    out.append("## Player health (last 30 days, active project)")
    out.append("")
    out.append("| Slot | Deviations | Pushes-before-audit | Off-spec completions |")
    out.append("|------|------------|---------------------|----------------------|")
    for slot, d, p, o in rows:
        out.append(f"| {slot:<4} | {d:<10} | {p:<19} | {o:<20} |")
    out.append("")
    out.append(
        "Counters surface for proactive effort/thinking/model bumps. "
        "From deviations >= 2 on a Player, treat quality as the "
        "bottleneck and walk the ladder one rung at a time: (1) bump "
        "effort via coord_set_player_effort, (2) if at max or unhelpful, "
        "flip extended thinking on via coord_set_player_thinking "
        "(Claude runtime only), (3) only then bump model tier via "
        "coord_set_player_model. NEVER change runtime."
    )
    out.append("")
    return "\n".join(out)


async def _build_audit_aggregator_rows(project_id: str) -> str:
    """Audit aggregator (§11.2). For every active task with audit
    history, render a compact audit-trajectory block. Capped at 8
    active tasks. Returns "" when no active task has audit history.
    """
    try:
        c = await configured_conn()
        try:
            # Find up to 8 active tasks that have at least one auditor
            # role row.
            cur = await c.execute(
                """
                SELECT t.id, t.title, t.owner
                FROM tasks t
                WHERE t.project_id = ?
                  AND t.status != 'archive'
                  AND EXISTS (
                      SELECT 1 FROM task_role_assignments r
                      WHERE r.task_id = t.id
                      AND r.role IN ('auditor_syntax', 'auditor_semantics')
                  )
                ORDER BY t.created_at DESC
                LIMIT 8
                """,
                (project_id,),
            )
            tasks = [dict(r) for r in await cur.fetchall()]
            if not tasks:
                return ""
            task_ids = [t["id"] for t in tasks]
            placeholders = ",".join("?" * len(task_ids))
            cur = await c.execute(
                f"SELECT task_id, role, owner, verdict, report_path, "
                f"completed_at, assigned_at "
                f"FROM task_role_assignments "
                f"WHERE task_id IN ({placeholders}) "
                f"AND role IN ('auditor_syntax', 'auditor_semantics') "
                f"ORDER BY task_id, role, assigned_at",
                task_ids,
            )
            rows_by_task: dict[str, list[dict[str, Any]]] = {tid: [] for tid in task_ids}
            for r in await cur.fetchall():
                d = dict(r)
                rows_by_task[d["task_id"]].append(d)
        finally:
            await c.close()
    except Exception:
        logger.exception("audit_aggregator: query failed")
        return ""

    out: list[str] = []
    out.append("## Audit history (active tasks)")
    out.append("")
    rendered_any = False
    for t in tasks:
        rows = rows_by_task.get(t["id"], [])
        if not rows:
            continue
        rendered_any = True
        out.append(
            f"- {t['id']} \"{(t.get('title') or '').strip()}\" "
            f"(executor {t.get('owner') or '—'}):"
        )
        round_counter: dict[str, int] = {"auditor_syntax": 0, "auditor_semantics": 0}
        for r in rows:
            role = r.get("role") or "?"
            kind = "syntax" if role == "auditor_syntax" else "semantic"
            round_counter[role] = round_counter.get(role, 0) + 1
            verdict = (r.get("verdict") or "pending").upper()
            owner = r.get("owner") or "—"
            summary = _read_audit_summary(r.get("report_path") or "")
            summary_clause = f' — "{summary}"' if summary else ""
            if r.get("completed_at"):
                out.append(
                    f"  - {kind} round {round_counter[role]}: "
                    f"{verdict}{summary_clause}"
                )
            else:
                out.append(
                    f"  - {kind} round {round_counter[role]}: "
                    f"pending (auditor {owner})"
                )
    out.append("")
    return "\n".join(out) if rendered_any else ""


def _read_audit_summary(report_path: str) -> str:
    """Best-effort read of the `## Summary` (or first heading body) from
    an audit report markdown file. Returns up to 160 chars with no
    newlines, or "" on any failure."""
    if not report_path:
        return ""
    try:
        from pathlib import Path
        from server.paths import DATA_ROOT
        p = Path(report_path)
        if not p.is_absolute():
            p = DATA_ROOT / report_path
        if not p.is_file():
            return ""
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    needle = "## Summary"
    idx = text.find(needle)
    if idx < 0:
        # No `## Summary` heading — pick the first non-empty paragraph
        # after the frontmatter as a fallback.
        if text.startswith("---\n"):
            close = text.find("\n---\n", 4)
            if close > 0:
                text = text[close + 5 :]
        text = text.lstrip()
    else:
        text = text[idx + len(needle) :].lstrip()
    body = text.split("\n\n", 1)[0]
    body = " ".join(line.strip() for line in body.splitlines() if line.strip())
    if not body:
        return ""
    return body[:160] + ("…" if len(body) > 160 else "")


async def _build_recent_patterns_block(project_id: str) -> str:
    """Recent patterns (§11.3). Last `HARNESS_RECENT_PATTERNS_WINDOW_HOURS`
    (default 24h). Bounded at 5 lines. Returns "" when there's nothing
    to flag.
    """
    try:
        hours = int(os.environ.get("HARNESS_RECENT_PATTERNS_WINDOW_HOURS", "24"))
    except (TypeError, ValueError):
        hours = 24
    if hours < 1:
        hours = 1
    cutoff = f"-{hours} hours"
    findings: list[str] = []
    try:
        c = await configured_conn()
        try:
            # Repeat audit fails: same task, same kind, ≥ 2 rounds.
            cur = await c.execute(
                """
                SELECT t.id AS task_id, t.title, r.role, COUNT(*) AS n
                FROM task_role_assignments r
                JOIN tasks t ON t.id = r.task_id
                WHERE t.project_id = ?
                  AND r.role IN ('auditor_syntax', 'auditor_semantics')
                  AND r.verdict = 'fail'
                  AND r.completed_at >= datetime('now', ?)
                GROUP BY t.id, r.role
                HAVING n >= 2
                ORDER BY n DESC
                LIMIT 3
                """,
                (project_id, cutoff),
            )
            for r in await cur.fetchall():
                d = dict(r)
                kind = "syntax" if d["role"] == "auditor_syntax" else "semantic"
                title = (d.get("title") or "").strip()
                findings.append(
                    f"- {d['task_id']} \"{title}\" — {d['n']} {kind} fails; "
                    f"escalate via effort bump or re-spec."
                )

            # Players with deviations >= 3 in the window (any executor
            # who took >= 3 audit FAILs across all their tasks).
            cur = await c.execute(
                """
                SELECT t.owner AS slot, COUNT(*) AS n
                FROM task_role_assignments r
                JOIN tasks t ON t.id = r.task_id
                WHERE t.project_id = ?
                  AND r.role IN ('auditor_syntax', 'auditor_semantics')
                  AND r.verdict = 'fail'
                  AND r.completed_at >= datetime('now', ?)
                  AND t.owner IS NOT NULL
                GROUP BY t.owner
                HAVING n >= 3
                ORDER BY n DESC
                LIMIT 3
                """,
                (project_id, cutoff),
            )
            for r in await cur.fetchall():
                d = dict(r)
                findings.append(
                    f"- {d['slot']} has {d['n']} audit fails in the window; "
                    f"consider effort bump."
                )

            # commit_without_task_id_warning from same Player.
            cur = await c.execute(
                """
                SELECT json_extract(payload, '$.committer') AS slot,
                       COUNT(*) AS n
                FROM events
                WHERE type = 'commit_without_task_id_warning'
                  AND project_id = ?
                  AND ts >= datetime('now', ?)
                GROUP BY json_extract(payload, '$.committer')
                HAVING n >= 2
                ORDER BY n DESC
                LIMIT 2
                """,
                (project_id, cutoff),
            )
            for r in await cur.fetchall():
                d = dict(r)
                slot = d.get("slot") or "(unknown)"
                findings.append(
                    f"- {slot} pushed without a task_id {d['n']} times in "
                    f"the window; clarify their workflow."
                )

            # Compass confident_drift in the same region.
            cur = await c.execute(
                """
                SELECT json_extract(payload, '$.region') AS region,
                       COUNT(*) AS n
                FROM events
                WHERE type = 'compass_audit_logged'
                  AND project_id = ?
                  AND json_extract(payload, '$.verdict') = 'confident_drift'
                  AND ts >= datetime('now', ?)
                GROUP BY json_extract(payload, '$.region')
                HAVING n >= 2
                ORDER BY n DESC
                LIMIT 2
                """,
                (project_id, cutoff),
            )
            for r in await cur.fetchall():
                d = dict(r)
                region = d.get("region") or "(unknown)"
                findings.append(
                    f"- {d['n']} confident_drift verdicts in the {region!r} "
                    f"region; the lattice may be wrong about it."
                )

            # Multiple deviations_log rows for same executor across
            # distinct tasks.
            cur = await c.execute(
                """
                SELECT executor AS slot, COUNT(DISTINCT task_id) AS n
                FROM deviations_log
                WHERE project_id = ?
                  AND ts >= datetime('now', ?)
                GROUP BY executor
                HAVING n >= 2
                ORDER BY n DESC
                LIMIT 2
                """,
                (project_id, cutoff),
            )
            for r in await cur.fetchall():
                d = dict(r)
                findings.append(
                    f"- {d['slot']} flagged for deviations on {d['n']} "
                    f"distinct tasks; pattern, not one-off."
                )
        finally:
            await c.close()
    except Exception:
        logger.exception("recent_patterns: query failed")
        return ""

    if not findings:
        return ""
    findings = findings[:5]
    out: list[str] = []
    out.append(f"## Recent patterns (last {hours}h)")
    out.append("")
    out.extend(findings)
    out.append("")
    return "\n".join(out)


def _render_event_log_line(row: dict[str, Any]) -> str:
    """Compact one-line summary for a project_events row. Hard-cap at
    240 chars per §9.3."""
    actor = row.get("actor") or "?"
    etype = row.get("type") or "?"
    task_id = row.get("task_id") or ""
    pointer = row.get("payload_pointer") or ""
    ts = row.get("ts") or ""
    # ts → HH:MM short-form; defensive against unparseable.
    short_ts = ts
    if "T" in ts:
        short_ts = ts.split("T", 1)[1][:5]
    head = f"[{short_ts}] {actor} {etype}"
    if task_id:
        head += f" ({task_id})"
    if pointer:
        head += f": {pointer}"
    if len(head) > 240:
        head = head[:237] + "..."
    return head


async def _build_recent_events_block(
    project_id: str,
    surfaced_event_ids: list[int],
) -> str:
    """Recent events (§9.3). Reads the unread tail of `project_events`
    for the active project, capped at `HARNESS_PROJECT_EVENTS_PER_TICK`
    (default 50). Renders one line per row plus an overflow footer when
    the unread count exceeded the cap.

    Mutates `surfaced_event_ids` in place — appending the ids of every
    row surfaced. The post-turn handler stamps `read_by_coach_at` on
    these ids once Coach's ResultMessage lands successfully. If the
    turn fails (no ResultMessage), the ids stay unread and roll forward
    to the next tick.
    """
    try:
        cap = int(os.environ.get("HARNESS_PROJECT_EVENTS_PER_TICK", "50"))
    except (TypeError, ValueError):
        cap = 50
    if cap < 1:
        cap = 1
    rows: list[dict[str, Any]] = []
    older_count = 0
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id, ts, actor, type, task_id, payload_pointer "
                "FROM project_events "
                "WHERE project_id = ? AND read_by_coach_at IS NULL "
                "ORDER BY ts ASC, id ASC LIMIT ?",
                (project_id, cap),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            if len(rows) >= cap:
                # There may be more older-than-the-cap unread rows;
                # count them for the footer.
                cur = await c.execute(
                    "SELECT COUNT(*) AS n FROM project_events "
                    "WHERE project_id = ? AND read_by_coach_at IS NULL",
                    (project_id,),
                )
                total = int(dict(await cur.fetchone())["n"])
                older_count = max(0, total - len(rows))
        finally:
            await c.close()
    except Exception:
        logger.exception("recent_events: query failed")
        return ""

    if not rows:
        return ""
    surfaced_event_ids.extend(int(r["id"]) for r in rows)
    out: list[str] = []
    out.append("## Recent events")
    out.append("")
    for r in rows:
        out.append(_render_event_log_line(r))
    if older_count:
        out.append("")
        out.append(
            f"*+ {older_count} older unread event"
            + ("s" if older_count != 1 else "")
            + " — query `/api/projects/{id}/event_log` to browse.*"
        )
    out.append("")
    return "\n".join(out)


async def _stamp_events_read_by_coach(event_ids: list[int]) -> None:
    """Post-turn handler: stamps `read_by_coach_at = now()` on every
    project_events row in `event_ids`. Called from `run_agent`'s
    ResultMessage success path when the turn was Coach's and the surfaced
    ids list is non-empty."""
    if not event_ids:
        return
    placeholders = ",".join("?" * len(event_ids))
    now = datetime.now(timezone.utc).isoformat()
    try:
        c = await configured_conn()
        try:
            await c.execute(
                f"UPDATE project_events SET read_by_coach_at = ? "
                f"WHERE id IN ({placeholders}) "
                f"AND read_by_coach_at IS NULL",
                [now, *event_ids],
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception(
            "stamp_events_read_by_coach: failed for %d ids", len(event_ids)
        )


async def _build_coach_coordination_block(
    surfaced_event_ids: list[int] | None = None,
) -> str:
    """Phase 7 (PROJECTS_SPEC.md §10): Coach-only per-turn coordination
    block. Built fresh on every Coach turn from `projects`,
    `agent_project_roles`, `agents.locked`, `tasks`, `messages`, and
    the latest entry in `decisions/`.

    Returns the rendered markdown block, or "" on any read failure
    (Coach's turn is more valuable than getting this perfectly — fall
    back to a quieter prompt rather than crash the whole spawn).

    Layout matches the spec excerpt:
      ## Coordinating: <Project Name>
      (Project context pointer — full CLAUDE.md path + objectives file)
      ## Team composition (this project)
      ## Current state
        Open tasks, Inbox count, Last decision, Wiki paths

    Goals / objectives are injected as a separate `## Project
    objectives` section later in the system prompt — see
    recurrence-specs.md §3.3 + §6. Don't render them here too.
    """
    from server.paths import project_paths

    try:
        active = await resolve_active_project()
    except Exception:
        return ""

    # ---- Project name ----------------------------------------------
    # `projects.description` is intentionally NOT read here anymore —
    # goals/objectives flow through `project-objectives.md` (injected
    # later as a separate section). Rendering the DB description in
    # the coordination block AND injecting the objectives file
    # produced two stale-prone copies of the same goal in every turn.
    project_name = active
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT name FROM projects WHERE id = ?",
                (active,),
            )
            row = await cur.fetchone()
            if row:
                d = dict(row)
                project_name = d.get("name") or active

            # ---- Team composition (active project) -----------------
            cur = await c.execute(
                "SELECT id, locked, runtime_override FROM agents "
                "WHERE kind = 'player' ORDER BY id"
            )
            player_rows = [dict(r) for r in await cur.fetchall()]
            cur = await c.execute(
                "SELECT slot, name, role, model_override, "
                "effort_override, plan_mode_override, thinking_override "
                "FROM agent_project_roles "
                "WHERE project_id = ?",
                (active,),
            )
            role_map = {dict(r)["slot"]: dict(r) for r in await cur.fetchall()}

            # ---- Open tasks (kanban: any non-archive stage) ---------
            # Sort order matches the kanban flow: tasks deepest in the
            # pipeline first (ship → audit_* → execute → plan), so Coach
            # sees what's nearest delivery at the top of the rollup.
            cur = await c.execute(
                "SELECT id, title, status, owner, trajectory, blocked, "
                "success_criteria "
                "FROM tasks WHERE project_id = ? "
                "AND status != 'archive' "
                "ORDER BY CASE status "
                "  WHEN 'ship' THEN 0 "
                "  WHEN 'audit_semantics' THEN 1 "
                "  WHEN 'audit_syntax' THEN 2 "
                "  WHEN 'execute' THEN 3 "
                "  WHEN 'plan' THEN 4 "
                "  ELSE 5 END, id ASC LIMIT 20",
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
                    last_decision_line = f"{date_str} — {title}"
                else:
                    last_decision_line = title
    except Exception:
        pass

    # ---- Render -----------------------------------------------------
    lines: list[str] = []
    # §1 Project pointer (generic, one line — goals injected as a separate
    # `## Project objectives` section below; team roster lives in the
    # project CLAUDE.md auto-loaded by the SDK).
    lines.append(f"## Coordinating: {project_name}")
    lines.append("")
    lines.append("Goals: see ## Project objectives below.")
    lines.append("")

    # §2 Roster availability — only when ≥1 Player is locked. Quiet on
    # the common all-available case.
    locked_pre: list[str] = [
        prow["id"] for prow in player_rows if bool(prow.get("locked"))
    ]
    if locked_pre:
        lines.append("## Roster availability")
        lines.append("")
        lines.append(
            f"LOCKED: {', '.join(locked_pre)}. "
            "Skip in task assignment, DMs, broadcasts."
        )
        lines.append("")

    # Override collection (formerly part of §3 team-composition rendering;
    # the rendering itself was dropped 2026-05-11 since the project
    # CLAUDE.md `## Team` table is the source of truth and auto-loads via
    # the SDK). The override list is still consumed by §4 below.
    overridden: list[dict[str, Any]] = []
    for prow in player_rows:
        slot = prow["id"]
        rec = role_map.get(slot)
        rt_o = (prow.get("runtime_override") or "").lower() or None
        if rec is None:
            mo = ef_o = pm_o = th_o = None
        else:
            mo = rec.get("model_override") or None
            ef_o = rec.get("effort_override")
            pm_o = rec.get("plan_mode_override")
            th_o = rec.get("thinking_override")
        if rt_o or mo or ef_o is not None or pm_o is not None or th_o is not None:
            overridden.append({
                "slot": slot,
                "runtime": rt_o,
                "model": mo,
                "effort": ef_o,
                "plan_mode": pm_o,
                "thinking": th_o,
            })

    # Active per-Player overrides — only emit when at least one
    # override is set. Gives Coach a single line per Player for the
    # current state of the four knobs they control via
    # coord_set_player_{runtime,model,effort,plan_mode}, so Coach
    # doesn't have to call coord_get_player_settings to know what's
    # already in place. Defaults stay implicit (no line = no override).
    if overridden:
        lines.append("### Active overrides")
        lines.append("")
        for o in overridden:
            parts: list[str] = []
            if o.get("runtime"):
                parts.append(f"runtime={o['runtime']}")
            if o.get("model"):
                parts.append(f"model={o['model']}")
            ef_o = o.get("effort")
            if ef_o is not None:
                try:
                    label = _EFFORT_LEVELS.get(int(ef_o))
                except (TypeError, ValueError):
                    label = None
                if label:
                    parts.append(f"effort={label}")
            pm_o = o.get("plan_mode")
            if pm_o is not None:
                parts.append(
                    "plan_mode=on" if int(pm_o) == 1 else "plan_mode=off"
                )
            th_o = o.get("thinking")
            if th_o is not None:
                parts.append(
                    "thinking=on" if int(th_o) == 1 else "thinking=off"
                )
            if parts:
                lines.append(f"- {o['slot']}: {', '.join(parts)}")
        lines.append("")

    # PR 6: Roster runtimes — only emit when the team is mixed (any
    # Player on Codex). Saves prompt tokens on the default all-Claude
    # deploy. Codex agents have a different native toolset (`shell`,
    # `apply_patch`, `web_search` instead of Bash/Edit/WebSearch);
    # coord_* is identical on both. Coach plans differently when a
    # task needs apply_patch-shaped editing.
    codex_players = [
        p["id"] for p in player_rows
        if (p.get("runtime_override") or "").lower() == "codex"
    ]
    if codex_players:
        lines.append("### Codex Players")
        lines.append("")
        lines.append(
            f"{', '.join(codex_players)} (use shell / apply_patch / "
            "web_search instead of Bash / Edit / WebSearch)"
        )
        lines.append("")

    lines.append("## Current state")
    lines.append("")
    if open_tasks:
        lines.append(f"Open tasks ({len(open_tasks)}):")
        # Window where Coach's "definition of done" is load-bearing —
        # after they wrote it (at create or plan→execute) and before
        # archive. Rendered as a one-line sub-row beneath the task so
        # the bar stays in front of Coach without re-reading the spec.
        _criteria_visible_stages = {
            "execute", "audit_syntax", "audit_semantics", "ship",
        }
        for t in open_tasks:
            tid = t["id"]
            title = (t.get("title") or "").strip()
            status = t.get("status") or "?"
            owner = t.get("owner") or "—"
            traj_marker = _trajectory_marker(t.get("trajectory"), status)
            blocked = " BLOCKED" if t.get("blocked") else ""
            lines.append(
                f"- {tid} {status} {traj_marker}{blocked} {owner} — {title}"
            )
            criteria = (t.get("success_criteria") or "").strip()
            if criteria and status in _criteria_visible_stages:
                if len(criteria) > 120:
                    criteria = criteria[:117] + "..."
                lines.append(f"  → done when: {criteria}")
    else:
        lines.append("Open tasks: (none)")
    lines.append("")
    lines.append(
        f"Inbox: {unread} unread.  Last decision: {last_decision_line}"
    )
    lines.append("")

    # ---- Player health (§11.1, position 3 in v2 §14 ordering) -----
    player_health_block = await _build_player_health_block(active)
    if player_health_block:
        lines.append(player_health_block.rstrip())
        lines.append("")

    # ---- Active task health (kind_fail_count >= 2) -----------------
    # Rows already sorted by fail_count DESC, tiebreak recency DESC.
    # Cap to top-3 to keep Coach's coordination block O(1) regardless
    # of how many active tasks exist (H1 efficiency fix, 2026-05-14).
    health_rows = await _build_active_task_health_rows(active)
    if health_rows:
        shown = health_rows[:ACTIVE_TASK_HEALTH_CAP]
        overflow = len(health_rows) - len(shown)
        lines.append("## Active task health")
        lines.append("")
        for row in shown:
            effort = row.get("executor_effort") or "default"
            model = row.get("executor_model") or "default"
            lines.append(
                f"- {row['task_id']} \"{row['title']}\" — executor "
                f"{row['executor']} (effort={effort}, model={model}) — "
                f"{row['kind']} fail count {row['kind_fail_count']} "
                f"(latest verdict: {row['latest_verdict']})"
            )
        if overflow:
            lines.append(f"(+{overflow} more)")
        lines.append("")

    # ---- Audit history (§11.2, position 5 in v2 §14 ordering) -----
    audit_history_block = await _build_audit_aggregator_rows(active)
    if audit_history_block:
        lines.append(audit_history_block.rstrip())
        lines.append("")

    # ---- Stalled tasks (stage_change >= threshold, no progress) ----
    stalled_rows = await _build_stalled_tasks_rows(active)
    if stalled_rows:
        lines.append("## Stalled tasks")
        lines.append("")
        # v0.3.8 escalation labels — Coach sees which auto-action is
        # imminent and can intervene in time.
        rung_label = {
            0: "fresh",
            1: "nudged",
            2: "Coach-notified — auto-reassign next",
            3: "auto-reassigned — auto-archive next",
            4: "auto-archived",
        }
        for row in stalled_rows:
            blocker = row['owner']
            executor = row.get('task_executor')
            who = (
                f"blocker {blocker} (executor {executor})"
                if executor and executor != blocker
                else f"blocker {blocker}"
            )
            level = row.get('escalation_level', 0)
            label = rung_label.get(level, str(level))
            lines.append(
                f"- {row['task_id']} \"{row['title']}\" — stage "
                f"{row['stage']}, {who}, stale for "
                f"{row['age_hours']}h [escalation: {label}]"
            )
        lines.append("")
        lines.append(
            "Ladder: 30m nudge / 1h Coach-notify / 2h auto-reassign / "
            "4h auto-archive. Intervene before the next rung fires."
        )
        lines.append("")

    # ---- Soft stalls (watchdog findings, §10.7) -------------------
    soft_stall_rows = await _build_soft_stalls_rows(active)
    if soft_stall_rows:
        lines.append("## Soft stalls (watchdog)")
        lines.append("")
        for row in soft_stall_rows:
            tail = f" on {row['task_id']}" if row.get("task_id") else ""
            reason = row.get("reason") or "(no reason captured)"
            lines.append(
                f"- {row['agent']} {row['verdict']}{tail} — {reason}"
            )
        lines.append("")
        lines.append(
            "Verdicts: finished_not_reported→submit on_behalf_of; "
            "blocked→clarify; erroring→retry/escalate; looping→bump "
            "effort. Auto-clears in 1h."
        )
        lines.append("")

    # ---- Unrecorded artifacts (reconciliation findings) -----------
    artifact_rows = await _build_unrecorded_artifacts_rows(active)
    if artifact_rows:
        lines.append("## Unrecorded artifacts on disk")
        lines.append("")
        for row in artifact_rows:
            lines.append(
                f"- {row['task_id']} ({row['kind']}) {row['path']}, owner {row['owner']}"
            )
            lines.append(f"  fix: {row['fix']}")
        lines.append("")
        lines.append("(Junk/superseded → ignore; finding re-emits in 1h.)")
        lines.append("")

    # ---- Recent patterns (§11.3, position 9 in v2 §14 ordering) ----
    recent_patterns_block = await _build_recent_patterns_block(active)
    if recent_patterns_block:
        lines.append(recent_patterns_block.rstrip())
        lines.append("")

    # ---- Recent events (§9.3, position 10 in v2 §14 ordering) ------
    # Mutates the surfaced_event_ids list passed in. The post-turn
    # handler in run_agent stamps read_by_coach_at on those rows
    # only when the turn lands a successful ResultMessage.
    if surfaced_event_ids is not None:
        recent_events_block = await _build_recent_events_block(
            active, surfaced_event_ids,
        )
        if recent_events_block:
            lines.append(recent_events_block.rstrip())
            lines.append("")

    # §15-§17 (trajectory examples / lifecycle policy / wiki line) dropped
    # 2026-05-11 — content lives in the project CLAUDE.md (auto-loaded via
    # SDK setting_sources for Claude turns; manually injected for Codex).
    # The kanban v2 rules + trajectory shape + wiki paths are all in the
    # canonical project template; injecting them here too was pure
    # duplication. See git history for prior content.

    # ---- Backlog (§4.0.5): top-5 oldest pending ideas ---------------
    # Omit entirely when empty — zero token cost in the common case.
    try:
        bl_conn = await configured_conn()
        try:
            bl_cur = await bl_conn.execute(
                "SELECT id, title, proposed_by, proposed_at "
                "FROM backlog_tasks WHERE status='pending' "
                "ORDER BY proposed_at ASC LIMIT 5"
            )
            backlog_rows = [dict(r) for r in await bl_cur.fetchall()]
        finally:
            await bl_conn.close()
        if backlog_rows:
            lines.append("## Backlog")
            lines.append("")
            now_ts = datetime.now(timezone.utc)
            for br in backlog_rows:
                proposer = br.get("proposed_by") or "?"
                # Render a short slot label: coach→C, p1→1, human→human
                if proposer == "coach":
                    proposer_label = "C"
                elif proposer == "human":
                    proposer_label = "human"
                elif proposer.startswith("p") and proposer[1:].isdigit():
                    proposer_label = proposer[1:]
                else:
                    proposer_label = proposer
                try:
                    proposed_ts = datetime.fromisoformat(
                        br["proposed_at"].replace("Z", "+00:00")
                    )
                    age_s = int((now_ts - proposed_ts).total_seconds())
                    if age_s < 3600:
                        age = f"{age_s // 60}m"
                    elif age_s < 86400:
                        age = f"{age_s // 3600}h"
                    else:
                        age = f"{age_s // 86400}d"
                except Exception:
                    age = "?"
                title = (br.get("title") or "").strip()
                lines.append(
                    f"[#{br['id']}] \"{title}\" — {proposer_label}, {age} ago"
                )
            lines.append("")
            lines.append(
                "Use coord_triage_backlog(id, action='promote', "
                "trajectory=[...]) or action='reject'."
            )
            lines.append("")
    except Exception:
        pass  # Backlog section is best-effort; never block a Coach turn.

    return "\n".join(lines) + "\n"


async def _get_role_default_model(agent_id: str, runtime_name: str = "claude") -> str | None:
    """Read the per-role default model.

    Lookup order:
      1. team_config (human-set in the Settings drawer):
         coach_default_model          Claude Coach default
         players_default_model        Claude p1..p10 default
         coach_default_model_codex    Codex Coach default
         players_default_model_codex  Codex p1..p10 default
      2. Hardcoded role default in `models_catalog._ROLE_MODEL_DEFAULTS`
         (or `_ROLE_CODEX_MODEL_DEFAULTS` under runtime='codex').
         Stored as tier aliases so a model bump only touches the alias
         map; spawn-time `resolve_model_alias` translates to a concrete id.

    Returns None only when the role has no hardcoded default for the
    given runtime, so the caller can fall back to the SDK default.
    Currently every (role, runtime) combination has a concrete
    default, so None is unreachable in practice — kept for forward
    compatibility if a future runtime opts out of role defaults.
    """
    role = "coach" if agent_id == "coach" else "players"
    suffix = "_codex" if runtime_name == "codex" else ""
    key = f"{role}_default_model{suffix}"
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
        row = None
    val = ""
    if row:
        val = (dict(row).get("value") or "").strip()
        # team_config values are stored as JSON; unwrap if so, but tolerate
        # a raw string too (future-proof for a manual DB edit).
        if val.startswith('"') and val.endswith('"'):
            try:
                val = json.loads(val)
            except Exception:
                pass
    if val:
        return val
    # Fall through to the hardcoded role default (alias form).
    from server.models_catalog import role_default_model
    fallback = role_default_model(agent_id, runtime_name)
    return fallback or None


async def _get_agent_allowed_tools_override(
    agent_id: str,
    runtime_name: str,
) -> list[str] | None:
    """Return a Codex Player tool allowlist planted by kanban routing.

    `agents.allowed_tools` is a JSON array of SDK-facing tool names.
    NULL, empty, or malformed values fall back to the dispatcher role
    defaults so a bad row cannot brick spawning. The override is Codex
    Player-only: Claude keeps its broader legacy tool surface until it
    has an equivalent schema-cost problem and migration plan.
    """
    if runtime_name != "codex" or agent_id == "coach":
        return None
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT allowed_tools FROM agents WHERE id = ?",
                (agent_id,),
            )
            row = await cur.fetchone()
            cur = await c.execute(
                "SELECT r.role "
                "FROM task_role_assignments r "
                "JOIN tasks t ON t.id = r.task_id "
                "JOIN agents a ON a.id = ? "
                "WHERE r.owner = ? "
                "AND r.completed_at IS NULL "
                "AND r.superseded_by IS NULL "
                "AND ("
                "  (t.status = 'plan' AND r.role = 'planner') OR "
                "  (t.status = 'execute' AND r.role = 'executor') OR "
                "  (t.status = 'audit_syntax' AND r.role = 'auditor_syntax') OR "
                "  (t.status = 'audit_semantics' AND r.role = 'auditor_semantics') OR "
                "  (t.status = 'ship' AND r.role = 'shipper')"
                ") "
                "ORDER BY "
                "  CASE WHEN a.current_task_id = r.task_id THEN 0 ELSE 1 END, "
                "  r.assigned_at DESC, r.id DESC "
                "LIMIT 1",
                (agent_id, agent_id),
            )
            role_row = await cur.fetchone()
            active_role = dict(role_row).get("role") if role_row else None
        finally:
            await c.close()
    except Exception:
        logger.exception("get_agent_allowed_tools_override failed: agent=%s", agent_id)
        return None
    raw = (dict(row).get("allowed_tools") if row else None) or ""
    parsed: Any = []
    try:
        if raw:
            parsed = json.loads(raw)
    except Exception:
        logger.exception("invalid agents.allowed_tools JSON for agent=%s", agent_id)
    if not isinstance(parsed, list):
        parsed = []
    tools = [t for t in parsed if isinstance(t, str) and t]
    tools = list(dict.fromkeys(tools))
    if active_role:
        from server.role_tool_allowlists import tools_for_role, tools_json_for_role
        expected = tools_for_role(active_role)
        if set(tools) != set(expected):
            try:
                c = await configured_conn()
                try:
                    await c.execute(
                        "UPDATE agents SET allowed_tools = ? WHERE id = ?",
                        (tools_json_for_role(active_role), agent_id),
                    )
                    await c.commit()
                finally:
                    await c.close()
            except Exception:
                logger.exception(
                    "refresh agent allowed_tools failed: agent=%s role=%s",
                    agent_id, active_role,
                )
            else:
                logger.info(
                    "refreshed stale allowed_tools for agent=%s role=%s",
                    agent_id, active_role,
                )
            return expected
    return tools or None


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


async def _set_runtime_override(agent_id: str, runtime: str | None) -> None:
    """Update agents.runtime_override and best-effort evict any cached
    Codex client for the slot.

    Used by the runtime-transfer flow (post-compact flip) and by
    `coord_set_player_runtime` / `PUT /api/agents/{id}/runtime` when
    they need to write the column without re-implementing the eviction
    dance. `runtime` of 'claude' / 'codex' / None — None reverts to the
    role default at the next spawn.

    Caller is responsible for emitting the `runtime_updated` bus event
    (the audit / actor shape varies by entry path).
    """
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE agents SET runtime_override = ? WHERE id = ?",
                (runtime, agent_id),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("set_runtime_override failed: agent=%s", agent_id)
        return
    # Drop any cached Codex subprocess for this slot. Same rationale as
    # in coord_set_player_runtime — the cached client captured its
    # mcp_servers / proxy token at first spawn; a runtime change makes
    # those stale. Best-effort; a failure here is a tidy-up regression
    # only (next turn rebuilds the client anyway).
    try:
        from server.runtimes.codex import evict_client as _codex_evict
        await _codex_evict(agent_id)
    except Exception:
        pass


async def _perform_runtime_transfer_flip(
    agent_id: str,
    target_runtime: str,
) -> None:
    """Apply a successful session-transfer: flip runtime_override AND
    null both runtime session columns (session_id + codex_thread_id).

    Why null both: the user asked for a transfer-via-summary, so
    continuity carries forward via `continuity_note` (already written
    by the compact handler). Leaving the target runtime's stored
    session id around would make the next turn try to resume an
    orphaned session/thread from a prior life — the opposite of "fresh
    start with handoff." The source runtime's id is also cleared
    (Claude's compact handler already nulled session_id, so this is
    idempotent for that direction; Codex compact already cleared its
    thread id).

    Also emits a `runtime_updated` event so UI refresh hooks that key
    on `agents.runtime_override` changes (LeftRail badges,
    `refreshAgents()` on the WS feed) update consistently regardless
    of whether the flip came from a blunt PUT or a queued transfer.

    Note on helper choice: we use `_clear_session_id` here, NOT
    `_set_session_id(None)`. The latter early-returns on falsy values
    so the column is never actually written; the former issues an
    explicit `UPDATE … SET session_id = NULL`. Mixing them up was a
    long-standing bug in `/compact` itself (sessions weren't being
    freed) — the audit caught it during this transfer rollout.
    """
    await _set_runtime_override(agent_id, target_runtime)
    await _clear_session_id(agent_id)
    try:
        from server.runtimes.codex import _clear_codex_thread_id
        await _clear_codex_thread_id(agent_id)
    except Exception:
        logger.exception(
            "runtime_transfer: clear codex_thread_id failed agent=%s", agent_id,
        )
    # Reset the idle-poller debounce clock (Option A) AND stamp a
    # transfer timestamp for the per-transfer cooldown check (Option B).
    # Together these suppress the idle-poller false wake that fires when
    # the compact/transfer turn completes and status returns to 'idle'
    # while a queued assign-time wake hasn't fired yet.
    #   • last_idle_wake_at = now  →  extends the debounce window from
    #     now, giving the queued assign-time wake ~DEBOUNCE_SECONDS to
    #     fire and close its role row before the poller ticks again.
    #   • last_runtime_transfer_at = now  →  independent 60s cooldown
    #     in _maybe_wake_idle (belt-and-suspenders for the case where the
    #     debounce window has already expired but the transfer just fired).
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE agents"
                " SET last_idle_wake_at = ?, last_runtime_transfer_at = ?"
                " WHERE id = ?",
                (now_iso, now_iso, agent_id),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception(
            "runtime_transfer: stamp idle debounce failed agent=%s", agent_id,
        )
    await _emit(
        agent_id,
        "runtime_updated",
        runtime_override=target_runtime,
        source="session_transfer",
    )


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
    # 2026-05-11: prose tool catalogue replaced with one-line index. The
    # canonical per-tool descriptions (name, params, semantics) are
    # injected into the SDK tool schema automatically by the @tool
    # decorator in server/tools.py — duplicating them as prose here
    # cost ~7K chars/Coach-turn and ~4K chars/Player-turn for zero
    # information gain. What remains is what's NOT in the per-tool
    # schemas: cross-tool precedence, identity / role framing, Data
    # paths, Rules, MODEL_GUIDANCE (Coach).
    if agent_id == "coach":
        return (
            "You are Coach, the captain of the TeamOfTen team. Your job "
            "is to decompose human goals into tasks, assign them to "
            "Players (slots p1..p10), and orchestrate progress. You "
            "never write code; you delegate.\n\n"
            "Tools: a `coord_*` MCP catalogue is available — read each "
            "tool's description for parameters and semantics. AskUserQuestion "
            "(built-in) pauses your turn to ask the human a structured "
            "multiple-choice question; resumes when they submit.\n\n"
            "Cross-tool precedence (not in individual tool descriptions):\n"
            "  - Stage transitions ALWAYS go through coord_approve_stage. "
            "No auto-advance, no implicit assignment — every transition "
            "is your decision. coord_update_task is deprecated except "
            "for fast cancellation (status='archive' with no summary); "
            "prefer coord_archive_task with a user-facing summary.\n"
            "  - coord_set_player_runtime must be called BEFORE "
            "coord_set_player_model when picking a model from the other "
            "family (the model tool validates against the Player's "
            "current runtime).\n"
            "  - coord_get_player_settings BEFORE any coord_set_player_* "
            "— don't re-set what's already correct.\n"
            "  - coord_write_task_spec / coord_submit_audit_report accept "
            "`on_behalf_of=<slot>` for EMERGENCY OVERRIDE only (when a "
            "Player can't reach the tool in their runtime). Default path: "
            "assign the Player via coord_approve_stage and let them call "
            "the tool.\n"
            "  - When approving a stage on an artifact you reviewed and "
            "noticed drift, prefix `note` with `[deviation: <one-line "
            "reason>]` — feeds push-time validation instrumentation.\n"
            "  - coord_request_human is the ONLY path for spontaneous "
            "outreach to the human (pings Telegram when configured). "
            "Replies to inbound human messages (UI or Telegram) "
            "auto-forward back on the same turn. Ignore PushNotification "
            "— it's a generic CLI affordance, not how this harness "
            "reaches the human.\n"
            "  - To edit team-wide rules: Write /data/CLAUDE.md (global) "
            "or /data/projects/<active>/CLAUDE.md (this project) with "
            "the standard Write tool — read fresh into every agent's "
            "system prompt next turn. (Coach-only by convention.)\n\n"
            "Data paths:\n"
            "  - knowledge/<path>.md — durable text via coord_write_knowledge.\n"
            "  - outputs/<path>.<ext> — binary deliverables via coord_save_output (mirrored to the cloud drive).\n"
            "  - memory/<topic>.md — scratchpad (overwritable).\n"
            "  - decisions/<date>-slug.md — immutable ADRs (Coach-only write).\n"
            "  - ./handoffs/<agent>-<ts>.md — compact-handoff files for cross-compact context.\n\n"
            "Rules:\n"
            "  - You never write code; you delegate.\n"
            "  - Only you can create top-level tasks — Players can only subtask.\n"
            "  - You are the sole source of assignments. Pools are FYI "
            "only; Players never claim. Explicitly name one slot per "
            "stage via coord_approve_stage.\n"
            "  - The kanban records and surfaces; it does NOT route. "
            "Read every entry in `## Recent events` before composing "
            "the next move.\n"
            "  - Audit FAIL never auto-reverts. Read the verdict, "
            "decide, then call coord_approve_stage.\n"
            "  - No auto-archive. Every task ends with a Coach-written "
            "summary via coord_archive_task.\n"
            "  - Start every turn by reading your inbox for new human goals.\n"
            "  - Be terse.\n"
            "\n"
            + MODEL_GUIDANCE
        )
    return (
        f"You are Player {agent_id} on the TeamOfTen team. Your name and "
        f"role will be assigned by Coach; for now work with your slot id.\n\n"
        f"Tasks flow: plan → execute → audit_syntax → audit_semantics → "
        f"ship → archive. Your role per task is one of: executor (default), "
        f"formal/semantic reviewer, shipper, planner. You take work only "
        f"when Coach explicitly assigns you via coord_approve_stage. "
        f"Don't claim from pools — pools are FYI only. Call "
        f"coord_my_assignments at the start of any turn when unsure.\n\n"
        f"**You report to Coach, not to the kanban.** The kanban is "
        f"Coach's log of what you and your peers told them. The "
        f"completion tools (coord_commit_push, coord_submit_audit_report, "
        f"coord_write_task_spec, coord_role_complete) ARE your message "
        f"to Coach — call one with `task_id` + `message_to_coach=...` to "
        f"tell Coach 'I'm done, here's what I produced.' Until you call "
        f"it, your work is invisible — Coach has no idea you finished, "
        f"the kanban has no record, the team assumes you're still working.\n\n"
        f"**NEVER finish a turn without a `coord_*` update message to "
        f"Coach.** Even if you called one earlier — if you did anything "
        f"since, call one more; Coach reads your LAST signal. Nothing "
        f"material to add? `coord_send_message(to='coach', body='ack — "
        f"<one line>')` is the right answer.\n\n"
        f"After you signal Coach, your turn ends. Coach reviews next "
        f"tick. v2 does NOT auto-advance and audit FAIL does NOT "
        f"auto-revert — Coach is the next mover.\n\n"
        f"Tools: a `coord_*` MCP catalogue is available — read each "
        f"tool's description for parameters and semantics. "
        f"AskUserQuestion (built-in) routes to Coach by default and "
        f"pauses your turn until Coach resolves it. Cross-tool "
        f"constraints (not in individual descriptions):\n"
        f"  - coord_update_task is NOT for Players — stage transitions "
        f"are Coach's job (coord_approve_stage). Use it only if Coach "
        f"explicitly directs you.\n"
        f"  - coord_create_task: SUBTASKS only (parent must be a task "
        f"you own). Top-level tasks are Coach's and land in Backlog "
        f"first — Coach triages via coord_triage_backlog to start work.\n"
        f"  - coord_send_message cannot assign work to peers — only "
        f"Coach assigns.\n"
        f"  - coord_save_output for BINARY deliverables; text reports "
        f"go to coord_write_knowledge.\n"
        f"  - coord_request_human: escalate when blocked on something "
        f"only the human can decide. Replies to inbound human messages "
        f"auto-forward back. Ignore PushNotification — it's a generic "
        f"CLI affordance, not how this harness reaches the human.\n\n"
        f"Data paths:\n"
        f"  - your cwd is your per-slot git worktree (when the active "
        f"project has a repo configured). All edits land here; ship "
        f"via coord_commit_push.\n"
        f"  - ./uploads/ — human-uploaded reference material (PDFs, "
        f"specs, screenshots; read-only, auto-synced from the cloud drive ~60s).\n"
        f"  - ./attachments/ — pasted images from the UI.\n"
        f"  - knowledge/<path>.md, outputs/<path>.<ext>, memory/<topic>.md "
        f"— team-wide via coord_* tools.\n"
        f"  - ./handoffs/<agent>-<ts>.md — full compact-handoff files "
        f"for cross-compact context.\n\n"
        f"Rules:\n"
        f"  - You execute and report. You do not assign work to other Players.\n"
        f"  - Start every turn by reading your inbox for new orders from Coach.\n"
        f"  - Before complex work, check memory for prior findings.\n"
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
        from server.tools import _with_player_reminder
        wake_body = "The operator left notes on the approved plan."
        if agent_id != "coach":
            wake_body = _with_player_reminder(wake_body)
        await maybe_wake_agent(
            agent_id,
            wake_body,
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


def _classify_protected_path(path_str: str) -> str | None:
    """Classify a write target against the harness's protected paths.

    Returns:
      - 'truth'             — `path_str` resolves under any project's
                              `truth/` lane.
      - 'project_claude_md' — `path_str` resolves to a project's
                              top-level `CLAUDE.md`
                              (`/data/projects/<slug>/CLAUDE.md`).
      - None                — unprotected; the hook lets the write
                              through.

    Used by the file-guard hook to short-circuit agent writes
    regardless of which project is currently active, since an agent
    could in theory pass an absolute path into another project's tree.
    """
    if not path_str:
        return None
    try:
        target = Path(path_str).resolve()
    except OSError:
        return None
    from server.paths import DATA_ROOT

    projects_root = (DATA_ROOT / "projects").resolve()
    try:
        rel = target.relative_to(projects_root)
    except ValueError:
        return None
    parts = rel.parts
    # `<slug>/truth/...` — at least 2 parts and second is exactly "truth".
    if len(parts) >= 2 and parts[1] == "truth":
        return "truth"
    # `<slug>/CLAUDE.md` — exactly two parts, second is the literal
    # filename. Anything deeper (e.g. `<slug>/repo/CLAUDE.md` inside the
    # worktree) is not the project-instruction file and stays writable.
    if len(parts) == 2 and parts[1] == "CLAUDE.md":
        return "project_claude_md"
    return None


# Bash heuristics — match path components anywhere in the command
# string. Conservative: false-positive on a literal mention (e.g. an
# `echo "truth/foo"` log line) is acceptable because (a) agents have
# no reason to type these, and (b) over-deny just makes the agent ask
# Coach for help, which is exactly the intended flow.
_BASH_TRUTH_PATTERN = re.compile(r"(?:^|[\s/=:'\"])truth/")
# Project CLAUDE.md: match `projects/<anything>/CLAUDE.md` so abs and
# rel paths both trip. The `[^/\s'"]+` segment matches the slug.
_BASH_PROJECT_CLAUDE_MD_PATTERN = re.compile(
    r"projects/[^/\s'\"]+/CLAUDE\.md"
)


async def _posttool_wiki_index_hook(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,
) -> None:
    """Compatibility PostToolUse hook for wiki writes.

    Wiki writes through the file browser rebuild the index in
    `server.files`. Agent SDK writes bypass that path, so this hook
    preserves the Phase 7 guarantee that a wiki write refreshes
    `wiki/INDEX.md`.
    """
    try:
        tool_name = input_data.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return
        tool_input = input_data.get("tool_input") or {}
        path_str = (
            tool_input.get("file_path")
            or tool_input.get("notebook_path")
            or ""
        )
        if not path_str:
            return
        from server.paths import global_paths, update_wiki_index

        gp = global_paths()
        target = Path(path_str).resolve()
        wiki_root = gp.wiki.resolve()
        wiki_index = gp.wiki_index.resolve()
        if target == wiki_index:
            return
        try:
            target.relative_to(wiki_root)
        except ValueError:
            return
        await asyncio.to_thread(update_wiki_index)
    except Exception:
        logger.exception("wiki-index posttool hook error (failing open)")


async def _pretool_file_guard_hook(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """Block any agent write to harness-managed files.

    Two protected categories, each routed through its own approval
    flow (`coord_propose_file_write`, Coach-only):

      - **truth/** — `/data/projects/<slug>/truth/*`. The user's
        validated source-of-truth (specs, brand guidelines, etc.).
      - **project CLAUDE.md** — `/data/projects/<slug>/CLAUDE.md`.
        The project's instruction file, read fresh into every agent's
        system prompt every turn.

    These files must NEVER be mutated by an agent without explicit
    human approval. Coach proposes via `coord_propose_file_write`
    (scope='truth' or 'project_claude_md'); Players cannot propose
    directly — they message Coach. See
    `server/templates/global_claude_md.md`.
    """
    try:
        tool_name = input_data.get("tool_name") or ""
        tool_input = input_data.get("tool_input") or {}
        category: str | None = None
        if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            path_str = (
                tool_input.get("file_path")
                or tool_input.get("notebook_path")
                or ""
            )
            category = _classify_protected_path(path_str)
        elif tool_name == "Bash":
            cmd = str(tool_input.get("command") or "")
            if _BASH_TRUTH_PATTERN.search(cmd):
                category = "truth"
            elif _BASH_PROJECT_CLAUDE_MD_PATTERN.search(cmd):
                category = "project_claude_md"
        if category is None:
            return {}
        if category == "truth":
            reason = (
                "truth/ is read-only for agents. The user maintains "
                "it as the source-of-truth for the project (specs, "
                "brand guidelines, etc.). To propose a change, Coach "
                "calls coord_propose_file_write(scope='truth', path, "
                "content, summary); Players ask Coach to relay. The "
                "user reviews the diff and approves in the EnvPane's "
                "'File-write proposals' section."
            )
        else:
            reason = (
                "Project CLAUDE.md is read-only for agents. It's the "
                "project's instruction file (read fresh into every "
                "agent's system prompt every turn) and must only "
                "change with explicit human approval. To propose a "
                "change, Coach calls coord_propose_file_write("
                "scope='project_claude_md', path='CLAUDE.md', "
                "content, summary); Players ask Coach to relay. The "
                "user reviews the diff and approves in the EnvPane's "
                "'File-write proposals' section."
            )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    except Exception:
        logger.exception("file-guard hook error (failing open)")
        return {}


# ----------------------------------------------------------------------
# Secret-path guard
# ----------------------------------------------------------------------
#
# Defense-in-depth on top of Item 1 (env scrub) and Item 2 (Files API
# denylist): block agent SDK tool calls (Read/Edit/Write/MultiEdit/
# NotebookEdit/Bash) that target the same set of sensitive paths
# (Claude/Codex OAuth dirs, harness.db, /proc/<pid>/environ).
#
# This is best-effort. A determined attacker can read these via a
# Python one-liner that doesn't textually mention the path
# (constructing the string at runtime), and the hook only sees the
# `command` field for Bash. Acceptable trade-off vs the architectural
# fix (per-slot uid sandboxing, out of scope per threat model).
#
# Conservative patterns: a literal mention of `/data/claude/` in an
# echo or comment trips the deny — agents have no legitimate reason
# to type these strings, and over-deny just makes the agent escalate.

def _denied_secret_paths() -> tuple[Path, ...]:
    """Resolved absolute paths the secret-guard hook refuses to expose.

    Mirrors `server.files._denied_paths()` plus:
      - `/proc/<pid>/environ` is matched separately by `_path_is_secret`
        because it doesn't necessarily exist on disk to resolve.
      - Home-directory variants of the Claude and Codex CLI config
        directories (`~/.claude`, `~/.codex`). The Dockerfile sets
        `CLAUDE_CONFIG_DIR=/data/claude` and `CODEX_HOME=/data/codex`
        so production deploys put OAuth in those, but a dev machine
        run without env overrides falls back to home-dir defaults —
        defense-in-depth.
    """
    from server.paths import DATA_ROOT
    paths: list[Path] = []
    claude_env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if claude_env:
        paths.append(Path(claude_env).resolve())
    paths.append((DATA_ROOT / "claude").resolve())
    codex_env = os.environ.get("CODEX_HOME", "").strip()
    if codex_env:
        paths.append(Path(codex_env).resolve())
    paths.append((DATA_ROOT / "codex").resolve())
    # Home-dir defaults — only matter when the env overrides are unset
    # (dev machine without Dockerfile env), but cheap to include.
    try:
        home = Path.home()
        paths.append((home / ".claude").resolve())
        paths.append((home / ".codex").resolve())
    except (RuntimeError, OSError):
        # Path.home() raises if HOME is unset and pwd lookup fails.
        # Skip — production always has HOME set.
        pass
    db_default = (DATA_ROOT / "harness.db").resolve()
    paths.append(db_default)
    db_override = os.environ.get("HARNESS_DB_PATH", "").strip()
    if db_override:
        paths.append(Path(db_override).resolve())
    # Dedupe.
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return tuple(out)


def _claude_root_paths() -> tuple[Path, ...]:
    """Subset of `_denied_secret_paths()` that represents Claude CLI
    config roots specifically. Used to scope the `plans/` carveout —
    we only want to allow plan-mode writes under a Claude root, not
    under the Codex root or the DB path.
    """
    from server.paths import DATA_ROOT
    paths: list[Path] = []
    claude_env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if claude_env:
        paths.append(Path(claude_env).resolve())
    paths.append((DATA_ROOT / "claude").resolve())
    try:
        home = Path.home()
        paths.append((home / ".claude").resolve())
    except (RuntimeError, OSError):
        pass
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return tuple(out)


def _path_is_secret(path_str: str) -> bool:
    """True if `path_str` refers to a denied secret path. Resolves to
    follow symlinks before comparing, so a symlink at
    /workspaces/p1/foo → /data/claude won't slip past.

    Carveout: the Claude CLI's plan-mode workspace lives under
    `$CLAUDE_CONFIG_DIR/plans/` (resolves to `/data/claude/plans/`
    in production). Plan mode tells agents to write spec files
    there; without an exception the guard hard-blocks the agent's
    own plan workflow. Codex doesn't have an equivalent plan-mode
    path, so the carveout is Claude-only — scoped to roots returned
    by `_claude_root_paths()`. The resolve() pass above means a
    symlink-based escape like `/data/claude/plans/../.credentials.json`
    collapses to `/data/claude/.credentials.json`, which is still
    rejected.
    """
    if not path_str:
        return False
    # /proc/<pid>/environ — match before the resolve() call because the
    # path may not exist on the filesystem if pid is wrong. Also covers
    # /proc/self/environ.
    if re.search(r"/proc/(?:\d+|self|thread-self)/environ\b", path_str):
        return True
    try:
        target = Path(path_str).resolve()
    except OSError:
        return False
    claude_roots = set(_claude_root_paths())
    for denied in _denied_secret_paths():
        if target == denied:
            return True
        try:
            relative = target.relative_to(denied)
        except ValueError:
            continue
        # Carveout: `<claude_root>/plans/...` is the Claude CLI
        # plan-mode scratchpad. Allow reads/writes there. Codex root
        # and DB path have no equivalent carveout.
        parts = relative.parts
        if denied in claude_roots and parts and parts[0] == "plans":
            return False
        return True
    return False


# Bash heuristics — same conservative philosophy as the file-guard
# hook above. Match path components anywhere in the command string.
# The `plans/` carveout in `_path_is_secret` is mirrored here via a
# negative lookahead: `/data/claude` matches UNLESS immediately
# followed by `/plans` and a boundary (so `plans/` or `plans` at
# end-of-arg are exempt, but a sibling file named `plansomething`
# would still trip).
_BASH_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Default-deploy paths.
    re.compile(r"/data/claude(?!/plans(?:/|$))(?:/|$|\b)"),
    re.compile(r"/data/codex(?:/|$|\b)"),
    re.compile(r"/data/harness\.db\b"),
    # Anywhere agents try to grab a process's env.
    re.compile(r"/proc/(?:\d+|self|thread-self)/environ\b"),
)


async def _pretool_secret_guard_hook(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """Block agent tool calls that read or write sensitive harness state.

    Targets:
      - Claude/Codex OAuth directories (`/data/claude/`, `/data/codex/`).
      - SQLite database (`/data/harness.db` + WAL/SHM/journal sidecars).
      - `/proc/<pid>/environ` — env exfil (the harness's own env still
        contains live values on the harness process even after Item 1
        scrubbed agent subprocesses).

    Best-effort. Item 11's threat-model section documents the residual
    risk (Bash one-liners that construct paths at runtime can bypass).
    """
    try:
        tool_name = input_data.get("tool_name") or ""
        tool_input = input_data.get("tool_input") or {}
        denied = False
        if tool_name in (
            "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
            "Grep", "Glob",
        ):
            # Grep / Glob expose content via match output / file lists,
            # so a search rooted at /data/claude/ leaks just as much as
            # a Read does. Check the same path-bearing fields uniformly.
            for key in ("file_path", "notebook_path", "path"):
                v = tool_input.get(key)
                if v and _path_is_secret(str(v)):
                    denied = True
                    break
        elif tool_name == "Bash":
            cmd = str(tool_input.get("command") or "")
            for pat in _BASH_SECRET_PATTERNS:
                if pat.search(cmd):
                    denied = True
                    break
        if not denied:
            return {}
        reason = (
            "harness-managed secret path. The Claude/Codex OAuth "
            "directories, the SQLite DB, and /proc/<pid>/environ are "
            "off-limits to agent tool calls — they hold credentials or "
            "leak the harness process env. If you genuinely need data "
            "from one of these (e.g. to investigate a deploy issue), "
            "ask the human directly via coord_request_human."
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    except Exception:
        logger.exception("secret-guard hook error (failing open)")
        return {}



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


def _codex_external_mcp_enabled() -> bool:
    return os.environ.get("HARNESS_CODEX_EXTERNAL_MCP", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _filter_external_mcp_servers_for_allowed_tools(
    external_servers: dict[str, Any],
    external_tools: list[str],
    allowed_tools: list[str] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Keep only external MCP servers explicitly named by an allowlist."""
    allowed = set(allowed_tools or [])
    if not allowed:
        return {}, []
    kept_names = {
        name
        for name in external_servers
        if any(tool.startswith(f"mcp__{name}__") for tool in allowed)
    }
    if not kept_names:
        return {}, []
    return (
        {name: cfg for name, cfg in external_servers.items() if name in kept_names},
        [
            tool
            for tool in external_tools
            if any(tool.startswith(f"mcp__{name}__") for name in kept_names)
        ],
    )


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
    plan_mode: bool | None = None,
    effort: int | None = None,
    thinking: bool | None = None,
    compact_mode: bool = False,
    auto_compact: bool = False,
    transfer_to_runtime: str | None = None,
    wake_source: str | None = None,
) -> None:
    """Spawn one SDK query for the given slot and stream its events.

    Optional per-turn overrides (per-pane request — highest precedence
    after compact_mode). Each falls through to the Coach-set per-(slot,
    project) override on `agent_project_roles` when None, then to the
    role/SDK default.

    - model: SDK `model` kwarg (e.g. "claude-opus-4-7"). None = no override.
    - plan_mode: True/False explicit; None = no per-pane override
      (consult `plan_mode_override`). True ⇒ permission_mode="plan".
    - effort: 1..4 → "low" | "medium" | "high" | "max" thinking budget.
      None = no per-pane override (consult `effort_override`).
    - thinking: True/False explicit; None = no per-pane override
      (consult `thinking_override`). True ⇒ Claude runtime passes
      thinking={"type":"enabled","budget_tokens":HARNESS_THINKING_BUDGET_TOKENS}
      to ClaudeAgentOptions. Codex ignores silently.
    - transfer_to_runtime: only meaningful with compact_mode=True. When
      set to 'claude' / 'codex', a successful compact triggers a
      runtime flip + `session_transferred` event in place of the
      ordinary `session_compacted` event. The next turn runs on the
      target runtime with the freshly-written continuity_note injected
      into its system prompt (i.e. compact + flip = "session transfer").
    """
    # Global pause short-circuits before the cost check; users pausing
    # the harness shouldn't also burn a DB write counting cost.
    if _paused:
        await _emit(agent_id, "paused", prompt=prompt)
        logger.info("paused: refused to spawn %s", agent_id)
        return

    # Auto-compact trip-wire is delegated to the runtime: each runtime
    # knows its own session-state column and context-pressure signal
    # (Claude probes the JSONL session file; Codex reads the latest
    # `turns` row for the thread — see CODEX_RUNTIME_SPEC.md §A.5).
    # When the runtime returns True it already ran a compact (Claude
    # via COMPACT_PROMPT turn, Codex via native client.compact_thread),
    # so we fall through to run the user's original prompt on the
    # now-fresh session.
    from server.runtimes import get_runtime
    from server.runtimes.base import TurnContext

    _runtime_name = await _resolve_runtime_for(agent_id)
    _runtime = get_runtime(_runtime_name)
    _tc_compact = TurnContext(
        agent_id=agent_id,
        project_id="",  # not consumed by maybe_auto_compact
        prompt=prompt,
        system_prompt="",
        workspace_cwd="",
        allowed_tools=[],
        external_mcp_servers={},
        model=model,
        plan_mode=plan_mode,
        effort=effort,
        compact_mode=compact_mode,
        auto_compact=auto_compact,
        transfer_to_runtime=transfer_to_runtime,
    )
    await _runtime.maybe_auto_compact(_tc_compact)

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
        # A wake that landed in the brief window between our slot-
        # claim and this early exit would have been queued; clear it
        # (the cap is still hit so re-firing would cap again, and
        # holding the entry would risk it firing later under a stale
        # trigger context).
        _pending_wakes.pop(agent_id, None)
        return

    # Read prior continuation before the start event. Codex may later
    # prove this stale during its optional pre-start prepare; the final
    # `agent_started.resumed_session` value is emitted below.
    if _runtime_name == "codex":
        from server.runtimes.codex import _get_codex_thread_id
        prior_session = await _get_codex_thread_id(agent_id)
    else:
        prior_session = await _get_session_id(agent_id)

    # Build the allowed-tools list and discover external MCP servers
    # in the dispatcher (runtime-agnostic — both runtimes need a list
    # of permitted tool names + the discovered MCP server configs).
    # The runtime then merges with its runtime-specific MCP servers
    # (Claude attaches a coord SDK MCP; Codex would attach the stdio
    # coord proxy).
    allowed_override = await _get_agent_allowed_tools_override(
        agent_id,
        _runtime_name,
    )
    allowed = list(
        allowed_override
        or (ALLOWED_COACH_TOOLS if agent_id == "coach" else ALLOWED_PLAYER_TOOLS)
    )
    team_extras = await _get_team_extra_tools()
    if team_extras:
        allowed.extend(team_extras)
    external_servers, external_tools = load_external_servers()
    if _runtime_name == "codex" and not _codex_external_mcp_enabled():
        # Codex hosts MCP servers inside its app-server subprocess. One
        # noisy or crashing external stdio server can poison the whole
        # receiver loop, even when the turn only uses native tools and
        # coord_*. Keep Codex external MCP opt-in unless a per-agent
        # role allowlist explicitly names an external mcp__server__tool.
        external_servers, external_tools = _filter_external_mcp_servers_for_allowed_tools(
            external_servers,
            external_tools,
            allowed_override,
        )
    else:
        allowed.extend(external_tools)

    # Governance-layer docs (CLAUDE.md / skills / rules) from cloud-drive / disk.
    # Appended to the hardcoded role brief so context edits take effect on
    # the next turn with no restart required. Empty string when no
    # context is configured — agents behave as before.
    context_suffix = await build_system_prompt_suffix(agent_id, runtime=_runtime_name)
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
    # Sticky fingerprint write — see _last_shown_prior_error_fp comment
    # at module scope. When the prompt actually carries the suffix, we
    # stamp the fingerprint so the post-result handler can refuse to
    # re-arm an identical note. Cleared only on a clean turn (see
    # ResultMessage handler at line ~853). The compact-mode branch
    # above keeps prior_err = None so we never overwrite during compact.
    if prior_err:
        nt = prior_err.get("num_turns")
        _last_shown_prior_error_fp[agent_id] = (
            prior_err.get("subtype") or None,
            prior_err.get("stop_reason") or None,
            nt if isinstance(nt, int) else None,
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

    handoff_suffix = await _compose_handoff_suffix(agent_id)
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

    # Coach coordination block. Built fresh on every Coach turn from `projects`,
    # `agent_project_roles`, `agents.locked`, `tasks`, `messages`,
    # and the latest entry in `decisions/` so a project switch, a
    # `coord_set_player_role` update, a new task, or a fresh decision
    # all show up immediately on Coach's next turn.
    coordination_block = ""
    # Populated by `_build_recent_events_block` when Coach's prompt
    # surfaces unread project_events rows; stamped post-ResultMessage
    # by the success path so a failed turn rolls them forward.
    surfaced_event_ids: list[int] = []
    if agent_id == "coach":
        try:
            body = await _build_coach_coordination_block(
                surfaced_event_ids=surfaced_event_ids,
            )
        except Exception:
            logger.exception("coach coordination block build failed")
            body = ""
        if body:
            coordination_block = body + "\n"

    # Recurrence v2 (recurrence-specs.md §6): Coach gets two extra
    # sections after CLAUDE.md + brief — project objectives (phase 4)
    # then open coach todos (phase 3), in that order. Both are read
    # fresh each turn. Players' system prompts are not modified.
    coach_supplement = ""
    if agent_id == "coach":
        try:
            from server.coach_objectives import objectives_block
            from server.coach_todos import open_todos_block
            project_id = await resolve_active_project()
            sections: list[str] = []
            ob = objectives_block(project_id)
            if ob:
                sections.append(ob)
            tb = open_todos_block(project_id)
            if tb:
                sections.append(tb)
            if sections:
                coach_supplement = "\n\n" + "\n\n".join(sections)
        except Exception:
            logger.exception(
                "coach supplement: system-prompt build failed"
            )

    role_baseline = _system_prompt_for(agent_id)
    # Section order is tuned for Anthropic prompt-cache stability: the
    # Claude CLI applies cache breakpoints automatically, but only the
    # longest stable byte-prefix is reused across turns. So the rule is
    # STABLE BLOCKS FIRST, DYNAMIC BLOCKS LAST. Per-agent stability
    # (most stable → most volatile):
    #   identity        — per (slot, project), changes on rare edits
    #   role_baseline   — constant per slot
    #   context_suffix  — per (global+project CLAUDE.md mtime; +playbook for Coach)
    #   brief           — per agents.brief edit
    #   coach_supplement— per objectives/todos change (sub-hourly; Coach only)
    #   prior_error     — present after a failed turn; stable while present
    #   handoff         — present only on the first turn after /compact
    #   lock_suffix     — currently always ""; kept for shape parity
    #   coordination    — per Coach turn (Coach only; "" for Players)
    #
    # 2026-05-11: coordination_block moved to LAST. Previously it sat
    # before coach_supplement/prior_error/handoff, which meant any
    # per-turn coordination delta busted ~3.5K of otherwise-stable
    # cached prefix on every Coach turn. Putting it last keeps
    # everything stable cached and isolates the volatile region to
    # the trailing block.
    system_prompt = (
        identity_prefix
        + role_baseline
        + context_suffix
        + brief_suffix
        + coach_supplement
        + prior_error_suffix
        + handoff_suffix
        + lock_suffix
        + coordination_block
    )

    # Prompt-size telemetry — JSONL log under <DATA_ROOT>/prompt_log/
    # for offline analysis. Section names mirror the concatenation
    # above. Disable via HARNESS_PROMPT_LOG=false. Compact-mode spawns
    # don't reach this branch; their prompt is composed at the
    # alt-path (see TurnContext at line 5322).
    #
    # Also probes SDK-injected payload sizes the harness doesn't
    # build but the API DOES see on the wire:
    #   - sdk_global_claude_md / sdk_project_claude_md: only on Claude
    #     turns (the Agent SDK auto-loads CLAUDE.md via setting_sources);
    #     0 on Codex turns where context.py manually folds CLAUDE.md
    #     into context_suffix.
    #   - sdk_coord_schema: approximate MCP coord tool-definition
    #     payload size for both runtimes.
    # These are sizes-on-disk / sizes-of-schema, not exact wire bytes,
    # but tight enough for trending. See server/prompt_log.py.
    try:
        from server.prompt_log import record as _prompt_log_record  # noqa: PLC0415
        from server.tools import coord_schema_chars as _coord_schema_chars  # noqa: PLC0415

        sdk_global_md = 0
        sdk_project_md = 0
        if _runtime_name == "claude":
            try:
                from server.paths import global_paths, project_paths  # noqa: PLC0415

                gp_md = global_paths().claude_md
                if gp_md.is_file():
                    sdk_global_md = gp_md.stat().st_size
                active_pid = await resolve_active_project()
                if active_pid:
                    pp_md = project_paths(active_pid).claude_md
                    if pp_md.is_file():
                        sdk_project_md = pp_md.stat().st_size
            except Exception:
                pass
        try:
            sdk_coord_schema = _coord_schema_chars(agent_id)
        except Exception:
            sdk_coord_schema = 0

        # Tool Search probe — when the runtime injects ENABLE_TOOL_SEARCH
        # the SDK serves tool definitions on demand instead of shipping
        # the full schema on every turn. Log whether THIS spawn would
        # have enabled it. Gating mirrors ClaudeRuntime exactly so the
        # observable matches behaviour.
        _ts_active = False
        if _runtime_name == "claude":
            _ts_env = os.environ.get("HARNESS_TOOL_SEARCH", "true").lower()
            if _ts_env not in ("0", "false", "no", "off"):
                if "haiku" not in (model or "").lower():
                    _ts_active = True

        # When tool search is active, the SDK ships ~3–5 retrieved
        # tools per agent search instead of the full registered set.
        # `sdk_coord_schema_effective` is a crude estimate of the
        # actual wire-payload (assume ~1.2K avg per loaded tool × 5)
        # so trend analyses can compare the savings vs the un-gated
        # registration size. When tool search is off the effective
        # equals the registered count.
        _sdk_coord_schema_effective = (
            min(sdk_coord_schema, 6000) if _ts_active else sdk_coord_schema
        )

        _prompt_log_record(
            agent_id=agent_id,
            runtime=_runtime_name,
            model=model,
            sections={
                "identity": len(identity_prefix),
                "coordination": len(coordination_block),
                "role_baseline": len(role_baseline),
                "context_suffix": len(context_suffix),
                "brief": len(brief_suffix),
                "coach_supplement": len(coach_supplement),
                "prior_error": len(prior_error_suffix),
                "handoff": len(handoff_suffix),
                "lock": len(lock_suffix),
                "sdk_global_claude_md": sdk_global_md,
                "sdk_project_claude_md": sdk_project_md,
                "sdk_coord_schema": sdk_coord_schema,
                "sdk_coord_schema_effective": _sdk_coord_schema_effective,
                "sdk_tool_search_active": 1 if _ts_active else 0,
                "total": len(system_prompt),
            },
        )
    except Exception:
        logger.exception("prompt_log: invocation failed (non-fatal)")

    context_applied_payload = None
    if context_suffix or brief_suffix or handoff_suffix or prior_error_suffix:
        # Emit sizes (not content) below, after agent_started, so the
        # visible turn boundary remains the first event for this spawn.
        context_applied_payload = {
            "chars": len(context_suffix) + len(brief_suffix) + len(handoff_suffix),
            "brief_chars": len(brief_suffix),
            "handoff_chars": len(handoff_suffix),
        }

    # Model resolution precedence (highest → lowest):
    #   1. per-pane override (`model` arg from the gear popover)
    #   2. Coach-set per-(slot, project) override
    #      (`agent_project_roles.model_override`, written via
    #      `coord_set_player_model`)
    #   3. runtime-aware per-role team default, set in Settings
    #   4. SDK default (no kwarg)
    # Resolved here in the dispatcher because the turn ledger row
    # (turns.model) records what we actually told the SDK to use.
    # The Coach override is silently dropped if it doesn't fit the
    # current runtime — protects against the case where Coach picked a
    # Claude model and the player later flipped to Codex.
    #
    # The fit-check happens BEFORE alias resolution because aliases
    # carry runtime info (`latest_opus` is unambiguously Claude); the
    # SDK call below sees the post-resolution concrete id.
    if not model:
        slot_override = await _get_agent_model_override(agent_id)
        if slot_override and _model_fits_runtime(slot_override, _runtime_name):
            model = slot_override
    if not model:
        model = await _get_role_default_model(agent_id, _runtime_name)
    # Resolve tier alias → concrete id. No-op for concrete inputs and
    # for None/empty (returns "" which the SDK reads as "no override").
    # All downstream consumers — turns ledger, runtime fit checks,
    # context-window estimates — see the concrete id.
    from server.models_catalog import resolve_model_alias
    if model:
        model = resolve_model_alias(model)

    # plan_mode / effort resolution mirror the model chain:
    #   1. per-pane explicit value (the request kwarg, or None for "no
    #      override")
    #   2. Coach-set per-(slot, project) override
    #      (`agent_project_roles.{plan_mode,effort}_override`, written
    #      via `coord_set_player_plan_mode` / `coord_set_player_effort`)
    #   3. role-level default from `models_catalog`
    #      (medium effort, plan_mode off — see `_ROLE_EFFORT_DEFAULTS` /
    #      `_ROLE_PLAN_MODE_DEFAULTS`).
    # Fallbacks happen here in the dispatcher so the turn ledger row
    # records the effective values, and the same precedence applies to
    # both human-driven /api/agents/start spawns and Coach-triggered
    # auto-wakes (which call run_agent with plan_mode=None / effort=None).
    from server.models_catalog import (
        role_default_effort,
        role_default_plan_mode,
    )
    if plan_mode is None:
        plan_mode = await _get_agent_plan_mode_override(agent_id)
    if plan_mode is None:
        plan_mode = role_default_plan_mode(agent_id)
    plan_mode = bool(plan_mode)
    if effort is None:
        effort = await _get_agent_effort_override(agent_id)
    if effort is None:
        effort = role_default_effort(agent_id)
    # thinking has no role default — off unless explicitly set on the
    # Coach-managed override column. Per-turn request can also force it.
    if thinking is None:
        thinking = await _get_agent_thinking_override(agent_id)
    thinking = bool(thinking) if thinking is not None else False

    # Per-turn context the ResultMessage handler appends to the turns
    # ledger. The runtime stamps started_at on each iterate call so
    # a stale-session retry doesn't reuse the first try's clock.
    turn_ctx: dict[str, Any] = {
        "model": model,
        "plan_mode": plan_mode,
        "effort": effort,
        "thinking": thinking,
        "compact_mode": compact_mode,
        "auto_compact": auto_compact,
        "transfer_to_runtime": transfer_to_runtime,
        "had_handoff_on_entry": bool(handoff_suffix) and not compact_mode,
        "entry_prompt": prompt,
        # Runtime tag flows through to the turns ledger row so
        # by-runtime analytics are accurate even on Codex turns. The
        # ResultMessage handler reads this when calling
        # _insert_turn_row. cost_basis defaults inside that function;
        # CodexRuntime sets it explicitly when auth is plan_included.
        "runtime": _runtime_name,
        # Kanban v2 (§9.3): Coach's prompt surfaces unread project_events
        # rows for this tick. The post-ResultMessage success path stamps
        # `read_by_coach_at` on these ids; a failed turn leaves them
        # unread so they roll forward to the next tick.
        "surfaced_event_ids": list(surfaced_event_ids),
    }

    # Hand the runtime a TurnContext with everything it needs for one
    # turn. The runtime owns SDK options, hooks, MCP wiring, the
    # query loop, and stale-session retry. The dispatcher's outer
    # try/except below owns post-result exception suppression and
    # the auto-retry counter (universal to all runtimes).
    from server.runtimes import get_runtime
    from server.runtimes.base import TurnContext

    runtime_name = await _resolve_runtime_for(agent_id)
    runtime = get_runtime(runtime_name)
    # Pre-flight: refuse to spawn when the workspace dir doesn't exist
    # (and `workspace_dir`'s self-heal mkdir failed — typically a
    # /data volume mount issue, FS read-only, or a path collision).
    # Bailing here keeps the SDK from raising CLIConnectionError, which
    # on retry has historically escalated to a ProcessError on resume
    # and triggered the stale-session auto-heal — silently nuking the
    # session_id even though the underlying problem was the workspace.
    ws_path = await workspace_dir(agent_id)
    if not ws_path.exists():
        active_project = await resolve_active_project()
        err_msg = (
            f"workspace directory does not exist and could not be "
            f"created: {ws_path}. Check the volume mount and FS "
            f"permissions, then call POST /api/projects/{active_project}/"
            f"repo/provision."
        )
        logger.error(
            "run_agent: workspace_missing for agent=%s cwd=%s",
            agent_id, ws_path,
        )
        await _emit(
            agent_id,
            "error",
            error=err_msg,
            cwd=str(ws_path),
            reason="workspace_missing",
        )
        await _set_status(agent_id, "error")
        try:
            await bus.publish({
                "ts": _now(),
                "agent_id": agent_id,
                "type": "human_attention",
                "subject": f"{agent_id}: workspace dir missing",
                "body": err_msg,
                "urgency": "normal",
            })
        except Exception:
            logger.exception(
                "workspace_missing escalation failed: agent=%s", agent_id,
            )
        _running_tasks.pop(agent_id, None)
        _last_turn_ended_at[agent_id] = time.monotonic()
        # Mirror the canonical end-of-turn block below: reset tick rows
        # on Coach activity so the timer-from-last-outbound semantics
        # holds for every turn-end path, including pre-flight errors.
        if agent_id == "coach":
            try:
                from server.recurrences import (
                    reset_tick_next_fire_after_coach_activity,
                )
                project_id = await resolve_active_project()
                await reset_tick_next_fire_after_coach_activity(project_id)
            except Exception:
                logger.exception(
                    "recurrence tick-reset failed after coach pre-flight error"
                )
        await _emit(agent_id, "agent_stopped")
        return
    tc = TurnContext(
        agent_id=agent_id,
        project_id=await resolve_active_project(),
        prompt=prompt,
        system_prompt=system_prompt,
        workspace_cwd=str(ws_path),
        allowed_tools=list(allowed),
        external_mcp_servers=external_servers,
        model=model,
        plan_mode=plan_mode,
        effort=effort,
        thinking=thinking,
        compact_mode=compact_mode,
        auto_compact=auto_compact,
        transfer_to_runtime=transfer_to_runtime,
        prior_session=prior_session,
        turn_ctx=turn_ctx,
    )

    # Coord-MCP-proxy token plumbing is owned by CodexRuntime: the
    # codex app-server subprocess is cached per-slot across turns and
    # captures its env (including HARNESS_COORD_PROXY_TOKEN) at first
    # spawn, so a per-turn dispatcher mint/revoke would invalidate the
    # token used by the running subprocess after turn 1. The runtime
    # mints in `get_client` and revokes in `close_client` so the
    # token's lifetime matches the subprocess's. ClaudeRuntime's
    # in-process MCP doesn't need this.

    # Status flips before `agent_started` so UI refetches see the slot
    # as working. Some runtimes can prepare just enough before the
    # event to make the resume flag exact; Codex uses this to auto-heal
    # stale `codex_thread_id` values before the turn boundary renders.
    await _set_status(agent_id, "working")
    resumed_session = bool(prior_session)
    prepare_turn_start = getattr(runtime, "prepare_turn_start", None)
    if callable(prepare_turn_start) and not compact_mode:
        resumed_session = bool(await prepare_turn_start(tc))
    await _emit(
        agent_id,
        "agent_started",
        prompt=prompt,
        resumed_session=resumed_session,
        compact_mode=compact_mode,
        auto_compact=auto_compact,
        runtime=runtime_name,
        wake_source=wake_source,
    )
    if context_applied_payload is not None:
        await _emit(agent_id, "context_applied", **context_applied_payload)

    try:
        # Dispatcher routes manual /compact (and recursive auto-compact)
        # turns through the runtime's compact-specific entry point so
        # CodexRuntime can call a native `thread.compact()` instead of
        # running a COMPACT_PROMPT turn. ClaudeRuntime's
        # `run_manual_compact` simply delegates to `run_turn` because
        # for Claude the compact is structurally identical (same SDK
        # query loop, just with COMPACT_PROMPT and the
        # `turn_ctx["compact_mode"]` flag for the ResultMessage handler).
        if compact_mode:
            await runtime.run_manual_compact(tc)
        else:
            await runtime.run_turn(tc)
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
            try:
                _err_cwd = str(await workspace_dir(agent_id))
            except Exception:
                _err_cwd = "<workspace_dir failed>"
            logger.exception(
                "run_agent failed: agent=%s cwd=%s",
                agent_id, _err_cwd,
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
                cwd=_err_cwd,
            )
            await _set_status(agent_id, "error")
            # Auto-retry: bump the consecutive-error counter and
            # schedule a delayed wake. The scheduler itself handles the
            # cap (escalates to human after N consecutive errors
            # without an intervening success) and the no-op-if-
            # recovered check.
            _consecutive_errors[agent_id] = _consecutive_errors.get(agent_id, 0) + 1
            if _runtime_name == "codex":
                try:
                    from server.runtimes.codex import (
                        looks_like_codex_transport_error,
                        recover_codex_thread_after_transport_error,
                    )

                    if looks_like_codex_transport_error(e):
                        await recover_codex_thread_after_transport_error(
                            agent_id,
                            consecutive_errors=_consecutive_errors[agent_id],
                            error=err_msg,
                        )
                except Exception:
                    logger.exception(
                        "run_agent: Codex repeated-transport recovery failed "
                        "for %s",
                        agent_id,
                    )
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
        # Coord-proxy token revocation is owned by CodexRuntime
        # (`close_client`), bound to the cached subprocess lifetime
        # rather than the per-turn cycle. See the matching comment
        # at the top of run_agent.
        # recurrence-specs §11: Coach's tick cadence is measured from
        # last OUTBOUND activity (turn end), not from the last fire.
        # Push every enabled tick row's next_fire_at to now+cadence so
        # the operator gets a clean N-minute idle window after each
        # Coach turn. Best-effort — failures are logged and swallowed
        # by the helper. Skipped for Players + system/human turns.
        if agent_id == "coach":
            try:
                from server.recurrences import (
                    reset_tick_next_fire_after_coach_activity,
                )
                project_id = await resolve_active_project()
                await reset_tick_next_fire_after_coach_activity(project_id)
            except Exception:
                logger.exception(
                    "recurrence tick-reset failed after coach turn end"
                )

    await _emit(agent_id, "agent_stopped")

    # Queue-on-busy: any wake that landed while this turn was running
    # was stashed in _pending_wakes. Fire it now — calls back into
    # maybe_wake_agent so the pause + cost-cap guards still apply.
    # Always bypasses debounce: the wake came from an event during
    # the turn, not in reaction to the turn's own output, so the
    # just-stamped _last_turn_ended_at would wrongly drop it.
    # Latest-wins: only the most recent queued wake fires. The inbox
    # + project_events tables retain the actual message / event
    # payloads, so coalescing the prompt doesn't lose information —
    # Coach reads inbox in the next turn and sees everything that
    # arrived while it was busy.
    #
    # Skip when auto_compact=True: this is the recursive compact
    # preamble fired by `maybe_auto_compact` BEFORE the outer turn
    # claims the slot, so the outer turn (running the user's actual
    # prompt) is about to handle the queue itself. Firing here would
    # race against the outer slot-claim and either steal the slot or
    # spawn_reject. Manual /compact (no auto_compact flag) still
    # drains the queue normally.
    if not auto_compact:
        queued = _pending_wakes.pop(agent_id, None)
        if queued is not None:
            q_reason, q_source, q_plan = queued
            try:
                await maybe_wake_agent(
                    agent_id,
                    q_reason,
                    bypass_debounce=True,
                    wake_source=q_source,
                    plan_mode=q_plan,
                )
            except Exception:
                logger.exception(
                    "post-turn deferred wake failed for %s", agent_id
                )


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


def _soft_error_retry_policy(
    stop_reason: Any, subtype: Any, duration_ms: Any,
) -> dict[str, Any]:
    """Decide whether to auto-retry a soft error.

    A soft error is a `ResultMessage(is_error=True)` — the SDK returned
    cleanly but the model's turn reported failure (`stop_reason` /
    `subtype` indicate why). Distinct from hard errors which throw
    before any `ResultMessage` lands and are always retried.

    Coach's 2026-05-12 report flagged real production cost: every
    `stop_sequence` (sometimes 7s into a turn) and `tool_use` timeout
    routed silently to a Coach DM, requiring manual recovery. Most are
    transient and recover on a single retry.

    Returns `{"retry": bool, "delay_s": int}`.

    Policy:
      - `stop_sequence` → retry, 0s delay (model-side truncation; usually
        not a real failure).
      - `tool_use` with duration < 5 min → retry, 30s delay (likely a
        transient tool error or shell flake).
      - `tool_use` with duration ≥ 5 min → no retry (probable tool loop
        or stuck shell; needs Coach attention).
      - `max_turns` / `max_tokens` → no retry (handled by the separate
        auto-continue path).
      - anything else → no retry; Coach handles via DM.

    Cap accounting reuses `_consecutive_errors` so repeated retriable
    shapes escalate via the gave-up path after `ERROR_RETRY_MAX_CONSECUTIVE`
    consecutive failures — eventually a misbehaving agent still trips
    `human_attention`.
    """
    if _looks_like_max_turns(subtype, stop_reason):
        return {"retry": False, "delay_s": 0}
    sr = str(stop_reason or "").strip().lower()
    if sr == "stop_sequence":
        return {"retry": True, "delay_s": 0}
    if sr == "tool_use":
        try:
            ms = int(duration_ms) if duration_ms is not None else None
        except (TypeError, ValueError):
            ms = None
        if ms is not None and ms < 300_000:  # < 5 min
            return {"retry": True, "delay_s": 30}
    return {"retry": False, "delay_s": 0}


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
            from server.tools import _with_player_reminder
            cutoff_body = (
                f"Your previous turn was cut off by the SDK "
                f"({reason_label} after {nturns} internal turns) "
                "before you could finish. The harness did NOT "
                "pause you. Continue from where you left off — "
                "no need to re-explain context. If you actually "
                "completed the work, just confirm so."
            )
            if agent_id != "coach":
                cutoff_body = _with_player_reminder(cutoff_body)
            await maybe_wake_agent(
                agent_id,
                cutoff_body,
                bypass_debounce=True,
            )
        except Exception:
            logger.exception("auto-continue delayed task failed for %s", agent_id)
        finally:
            _auto_continue_pending.discard(agent_id)

    asyncio.create_task(_delayed_continue())


async def _schedule_post_error_retry(
    agent_id: str,
    *,
    delay_s_override: int | None = None,
    accept_idle_status: bool = False,
) -> None:
    """Schedule a single auto-wake after an error so the agent doesn't
    sit idle indefinitely. Caps consecutive retries and escalates to
    the human when the cap trips.

    Two callers:
      - Hard errors (default): turn threw before `ResultMessage`. Status
        is `"error"`; the retry only fires if status remains `"error"`
        when the delay elapses.
      - Soft errors (`accept_idle_status=True`): `ResultMessage(is_error=True)`
        landed but the post-result handler reset status to `"idle"`.
        We retry as long as the slot isn't actively `"working"` (a
        user/peer wake during the window takes precedence).

    `delay_s_override` lets the soft-error policy pick a delay that
    differs from `ERROR_RETRY_DELAY_SECONDS` (default 45s for hard
    errors). Stop-sequence retries use 0s; tool-use timeouts use 30s.
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

    actual_delay = (
        delay_s_override if delay_s_override is not None
        else ERROR_RETRY_DELAY_SECONDS
    )

    async def _delayed_retry() -> None:
        try:
            await asyncio.sleep(actual_delay)
            # If the agent self-recovered (human intervened, new wake
            # fired, etc.) skip — we don't want to poke a healthy
            # agent with a stale "your turn errored" prompt.
            status = await _get_status_of(agent_id)
            if accept_idle_status:
                # Soft-error path: status was reset to `"idle"` by the
                # post-result handler; only bail if the slot has since
                # started a different turn.
                if status == "working":
                    return
            else:
                # Hard-error path: status stays `"error"` until either
                # a recovery wake fires or the user manually clears.
                if status != "error":
                    return
            if agent_id in _running_tasks:
                return
            await _emit(
                agent_id,
                "auto_retry_scheduled",
                attempt=attempt + 1,
                max_attempts=ERROR_RETRY_MAX_CONSECUTIVE,
                delay=actual_delay,
                soft=bool(accept_idle_status),
            )
            from server.tools import _with_player_reminder
            error_body = (
                "Your previous turn errored before completing. "
                "If you had a task in progress, resume it. If the "
                "error looks persistent, use coord_update_task(..., "
                "status='blocked') to park the task, or "
                "coord_request_human to escalate. Otherwise, carry on "
                "where you left off."
            )
            if agent_id != "coach":
                error_body = _with_player_reminder(error_body)
            await maybe_wake_agent(
                agent_id,
                error_body,
                bypass_debounce=True,
                wake_source="system_recovery",
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
    wake_source: str | None = None,
    plan_mode: bool | None = None,
) -> bool:
    """Spawn a turn for `agent_id` with `reason` as the prompt, if and
    only if all guards pass:

      - harness not paused
      - this agent's previous turn ended more than
        AUTOWAKE_DEBOUNCE_SECONDS ago, UNLESS bypass_debounce=True

    If the agent is already mid-turn, the wake is QUEUED instead of
    dropped: the args are stashed in `_pending_wakes` and a fresh turn
    fires automatically when the current one ends. Latest-wins
    coalescing — multiple wakes during a single busy stretch fold into
    one follow-up turn. The inbox + project_events tables retain the
    actual content, so coalescing doesn't lose information.

    The debounce exists to prevent tight Coach↔Player ping-pong when
    agents chat-reply to each other. Discrete actions (task assignment,
    human message) are NOT ping-pongy and should wake the target even
    if they just finished a turn — callers pass bypass_debounce=True
    for those paths.

    `wake_source` tags the origin of an automatic wake — the UI uses
    it to compact "system-triggered" turn headers (kanban role calls,
    task-completion summaries, stall ladder rungs, etc.) so the full
    instructional prompt body doesn't visually clutter the conversation
    timeline. Conventional values: 'kanban_assignment', 'kanban_pool',
    'kanban_role', 'kanban_audit_fail', 'kanban_stand_down',
    'kanban_completion', 'kanban_stall', 'system_recovery'. None for
    user-originated wakes (UI composer, Telegram inbound, peer chat).

    Returns True if a spawn was scheduled (immediately OR queued for
    post-turn), False otherwise. Cost caps are checked here AND inside
    run_agent — the early check avoids a storm of cost_capped events
    when a cap is hit (e.g. Coach assigns 10 tasks while team-capped,
    each assign would otherwise spawn a turn that immediately fails).
    """
    if agent_id == "system":
        return False
    if _paused:
        return False
    if agent_id in _running_tasks:
        # Queue-on-busy: stash the latest wake args so a fresh turn
        # fires once the current one ends. Replaces any prior queued
        # entry for this slot — latest-wins. `bypass_debounce` is NOT
        # recorded: the deferred fire always bypasses (see the
        # _pending_wakes comment above).
        _pending_wakes[agent_id] = (reason, wake_source, plan_mode)
        logger.info(
            "auto-wake queued (slot %s busy): %s", agent_id, reason[:80]
        )
        return True
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
    asyncio.create_task(run_agent(
        agent_id, reason, wake_source=wake_source, plan_mode=plan_mode,
    ))
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
    if not row or dict(row)["status"] != "working":
        return False
    repaired = await repair_stale_working_status("coach")
    return not repaired


# Stale-task watchdog config. Env-tunable so the default can be safe
# (fires rarely) and aggressive setups can dial in tighter.
#   HARNESS_STALE_TASK_MINUTES: how long an in-progress task must sit
#     without owner activity before it's flagged. Default 15.
#   HARNESS_STALE_TASK_NOTIFY_INTERVAL_MINUTES: re-notify cadence for
#     the same task if it stays stalled. Default 30.
#   HARNESS_STALE_TASK_CHECK_INTERVAL_SECONDS: how often the loop runs.
#     Default 60.
# Setting STALE_TASK_MINUTES to 0 disables the watchdog entirely.
STALE_TASK_MINUTES = int(os.environ.get("HARNESS_STALE_TASK_MINUTES") or "15")
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
                      AND t.status IN ('execute', 'audit_syntax', 'audit_semantics', 'ship')
                      AND t.owner IS NOT NULL
                      AND t.blocked = 0
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

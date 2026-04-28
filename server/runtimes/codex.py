"""CodexRuntime — OpenAI Codex via the codex-app-server-sdk (Python).

PR 5 ships this gated behind `HARNESS_CODEX_ENABLED=true`. SDK shape
confirmed against the live SDK on Zeabur 2026-04-28 — see
`Docs/CODEX_PROBE_OUTPUT.md` for the captured method surface.

Real entry point is `CodexClient.connect_stdio(command=["codex",
"app-server"], ...)` followed by `start()` + `initialize()`. Threads
go through `client.start_thread(config) -> ThreadHandle` (or
`resume_thread(thread_id)`). The turn stream is
`thread.chat(text) -> AsyncIterator[ConversationStep]`. Native
compact via `thread.compact()`.

See `Docs/CODEX_RUNTIME_SPEC.md` §E for the design + §I.1 for SDK
sourcing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Mapping
from typing import Any

from server.runtimes.base import TurnContext

logger = logging.getLogger(__name__)


# Module-level cache of `CodexClient` instances per slot. The harness
# already serializes turns per slot via `_SPAWN_LOCK` (see agents.py),
# satisfying the SDK's "one active turn consumer per client" rule.
# Closed and re-opened on auth-error / transport error.
_codex_clients: dict[str, Any] = {}

# Per-slot async locks to serialize get-or-create. The dispatcher's
# _SPAWN_LOCK already serializes whole turns per slot, but a defensive
# lock here lets `get_client` / `close_client` be safely called from
# health probes / shutdown handlers that don't hold the spawn lock.
_client_locks: dict[str, asyncio.Lock] = {}


def _slot_lock(slot: str) -> asyncio.Lock:
    lock = _client_locks.get(slot)
    if lock is None:
        lock = asyncio.Lock()
        _client_locks[slot] = lock
    return lock


async def get_client(
    slot: str,
    *,
    cwd: str,
    env_overrides: dict[str, str] | None = None,
) -> Any:
    """Return a started, initialized `CodexClient` for `slot`.

    Spawns `codex app-server` via stdio on first call; reuses the cached
    client thereafter. Callers who hit a `CodexTransportError` /
    `CodexProtocolError` should call `close_client(slot)` and retry —
    that drops the cached client so the next `get_client` rebuilds it.

    Confirmed against live SDK 0.3.2 on 2026-04-28; see
    Docs/CODEX_PROBE_OUTPUT.md for the surface this calls into.
    """
    async with _slot_lock(slot):
        cached = _codex_clients.get(slot)
        if cached is not None:
            return cached

        sdk = _import_codex_sdk()
        env = dict(os.environ)
        if env_overrides:
            env.update(env_overrides)

        client = sdk.CodexClient.connect_stdio(
            command=["codex", "app-server"],
            cwd=cwd,
            env=env,
        )
        # `connect_stdio` is sync in 0.3.2 (returns CodexClient directly,
        # not a coroutine), but the spec calls out that some early
        # builds returned awaitables. Accept both shapes.
        if hasattr(client, "__await__"):
            client = await client  # type: ignore[misc]

        try:
            r = client.start()
            if hasattr(r, "__await__"):
                await r
            r = client.initialize()
            if hasattr(r, "__await__"):
                await r
        except Exception as exc:
            # Construction failed mid-handshake; don't leave a half-open
            # client cached. Close best-effort and re-raise.
            try:
                close = client.close()
                if hasattr(close, "__await__"):
                    await close
            except Exception:
                logger.exception(
                    "CodexRuntime: close() during failed handshake raised "
                    "for slot %s", slot,
                )
            raise

        _codex_clients[slot] = client
        logger.info("CodexRuntime: opened client for slot=%s", slot)
        return client


async def close_client(slot: str) -> None:
    """Close + drop the cached client for `slot`. Safe if no client is
    cached. Called on auth-error / transport-error / shutdown."""
    async with _slot_lock(slot):
        client = _codex_clients.pop(slot, None)
        if client is None:
            return
        try:
            r = client.close()
            if hasattr(r, "__await__"):
                await r
        except Exception as exc:
            logger.exception(
                "CodexRuntime: close() raised for slot %s — dropping "
                "from cache anyway", slot,
            )
        else:
            logger.info("CodexRuntime: closed client for slot=%s", slot)


async def close_all_clients() -> None:
    """Close every cached client. Called on harness shutdown."""
    slots = list(_codex_clients.keys())
    for slot in slots:
        await close_client(slot)


# ---------------------------------------------------------------------
# Thread persistence (audit item #9 — Docs/CODEX_RUNTIME_SPEC.md §E.2)
#
# Mirrors the Claude `_get/_set/_clear_session_id` helpers in agents.py
# but reads/writes `agent_sessions.codex_thread_id` instead. The
# (slot, project_id) composite key matches the Claude path so a single
# row can hold both a Claude session_id and a Codex thread_id (an agent
# that switches runtimes preserves both).
# ---------------------------------------------------------------------


async def _get_codex_thread_id(agent_id: str) -> str | None:
    """Read `agent_sessions.codex_thread_id` for the active project."""
    if agent_id == "system":
        return None
    from server.db import resolve_active_project, configured_conn
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT codex_thread_id FROM agent_sessions "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("get_codex_thread_id failed: agent=%s", agent_id)
        return None
    if not row:
        return None
    v = dict(row).get("codex_thread_id")
    return v if v else None


async def _set_codex_thread_id(agent_id: str, thread_id: str | None) -> None:
    """Persist a thread id after a successful first chat step. Pass
    None to no-op (mirrors `_set_session_id`)."""
    if not thread_id or agent_id == "system":
        return
    from server.db import resolve_active_project, configured_conn
    from server.agents import _ensure_session_row, _now
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            await _ensure_session_row(c, agent_id, project_id)
            await c.execute(
                "UPDATE agent_sessions SET codex_thread_id = ?, last_active = ? "
                "WHERE slot = ? AND project_id = ?",
                (thread_id, _now(), agent_id, project_id),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("set_codex_thread_id failed: agent=%s", agent_id)


async def _clear_codex_thread_id(agent_id: str) -> None:
    """Null the stored thread id. Used on stale-thread auto-heal
    (resume_thread raised) and on /compact success (§E.6)."""
    if agent_id == "system":
        return
    from server.db import resolve_active_project, configured_conn
    project_id = await resolve_active_project()
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE agent_sessions SET codex_thread_id = NULL "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("clear_codex_thread_id failed: agent=%s", agent_id)


async def open_thread(
    agent_id: str,
    client: Any,
    *,
    config: Any | None = None,
) -> tuple[Any, bool]:
    """Return a `ThreadHandle` for `agent_id`, creating or resuming as
    appropriate. Implements §E.2's stale-thread auto-heal: if a stored
    `codex_thread_id` fails to resume (any non-cancellation exception),
    null it and fall back to `start_thread` once.

    Returns `(thread_handle, resumed: bool)` so the dispatcher can stamp
    the `agent_started` event with the right `resumed_session` flag.

    Persistence is the caller's responsibility: this function does not
    write the freshly-started thread's id to `agent_sessions`. The
    dispatcher should call `_set_codex_thread_id(agent_id, thread.thread_id)`
    after the first successful chat step (§E.2 — persist on success, not
    on construction, so a thread that fails its first turn isn't sticky).

    `asyncio.CancelledError` inherits from `BaseException` in Py 3.12+
    so `except Exception` correctly lets cancellations propagate.
    """
    existing = await _get_codex_thread_id(agent_id)
    if existing:
        try:
            r = client.resume_thread(existing, overrides=config)
            if hasattr(r, "__await__"):
                r = await r
            return (r, True)
        except Exception as exc:
            logger.exception(
                "CodexRuntime: resume_thread failed for slot=%s "
                "thread_id=%s — clearing and retrying with start_thread",
                agent_id, existing,
            )
            try:
                from server.agents import _emit
                await _emit(
                    agent_id,
                    "session_resume_failed",
                    session_id=existing,
                    error=f"{type(exc).__name__}: {exc}",
                    runtime="codex",
                )
            except Exception:
                logger.exception(
                    "CodexRuntime: session_resume_failed emit failed for slot=%s",
                    agent_id,
                )
            await _clear_codex_thread_id(agent_id)

    r = client.start_thread(config)
    if hasattr(r, "__await__"):
        r = await r
    return (r, False)


# ---------------------------------------------------------------------
# ConversationStep → harness event translator
# (audit item #10 — Docs/CODEX_RUNTIME_SPEC.md §E.3)
#
# Translates each step yielded by `thread.chat()` into one or more
# harness events via `_emit`. Confirmed shapes (live spike 2026-04-28):
#
#   step.step_type='userMessage', item_type='userMessage'
#       → skip (already persisted by the dispatcher when it took the
#          prompt)
#   step.step_type='codex',       item_type='agentMessage', text=<str>
#       → emit text=<...>; phase='final_answer' marks the turn-ending
#          message
#
# Inferred shapes (need a tool-using prompt to validate; passed through
# to `_emit` with a permissive arg extractor that doesn't assume keys):
#
#   item_type='shell'        → tool_use(tool='Bash')
#   item_type='apply_patch'  → tool_use(tool='Edit')
#   item_type='web_search'   → tool_use(tool='WebSearch')
#   item_type='reasoning'    → thinking
#   item_type='mcp_tool_call'→ tool_use(tool='mcp__<server>__<name>')
#
# Unknown item_types log + skip rather than crashing the turn — newer
# SDKs may add categories we haven't seen yet.
# ---------------------------------------------------------------------


# Mapping from Codex item_type → (harness event_type, harness tool name).
# Tool name is None for non-tool events; the renderer keys off the
# canonical Claude tool names so the existing UI cards keep working.
# `mcp_tool_call` is special-cased in handle_step (not in this table)
# because its tool name resolves dynamically from the payload —
# putting a sentinel string here would hurt the table's inspectability.
#
# Notably absent (deliberate): `tool_result` / `shell_output` /
# `apply_patch_result` shapes. Probe-1 didn't capture a tool-using
# turn, so item-type names for tool RESULTS are still unknown. Until
# a follow-up probe (scripts/codex_probe_tools.py) captures them,
# those steps fall through to the unknown-type skip path — degraded
# UI (tool_use card without paired result) but no crash. Add entries
# here once names are known.
_ITEM_TYPE_TO_HARNESS: dict[str, tuple[str, str | None]] = {
    "userMessage": ("_skip", None),
    "agentMessage": ("text", None),
    "reasoning": ("thinking", None),
    # Draft names from Docs/CODEX_PROBE_OUTPUT.md plus the names used
    # by codex-app-server-sdk 0.3.2's normalizer.
    "shell": ("tool_use", "Bash"),
    "commandExecution": ("tool_use", "Bash"),
    "apply_patch": ("tool_use", "Edit"),
    "fileChange": ("tool_use", "Edit"),
    "web_search": ("tool_use", "WebSearch"),
    "webSearch": ("tool_use", "WebSearch"),
}


def _resolve_mcp_tool_name(item_payload: dict[str, Any]) -> str:
    """Build the Claude-convention `mcp__<server>__<name>` string from
    a Codex `mcp_tool_call` item payload. The exact payload keys are
    provisional pending probe-2; this looks up several plausible
    spellings and falls back to a marker name on miss.
    """
    server = (
        item_payload.get("server")
        or item_payload.get("server_name")
        or item_payload.get("serverName")
        or item_payload.get("mcp_server")
        or item_payload.get("mcpServer")
        or "unknown"
    )
    name = (
        item_payload.get("name")
        or item_payload.get("tool_name")
        or item_payload.get("toolName")
        or item_payload.get("tool")
        or "unknown"
    )
    return f"mcp__{server}__{name}"


def _step_item_payload(step: Any) -> dict[str, Any]:
    """Pull the raw item dict out of a ConversationStep.data.

    The live spike showed `data` carries the item under BOTH
    `data['params']['item']` (the JSON-RPC param wrapper) and
    `data['item']` (a convenience top-level copy). Prefer the wrapped
    location so we read what the SDK protocol promised; fall back to
    the bare key if a future SDK build drops the wrapper.

    Returns an empty dict if neither shape is available so callers can
    safely .get() into it without an AttributeError.
    """
    data = getattr(step, "data", None) or {}
    if not isinstance(data, dict):
        return {}
    params = data.get("params")
    if isinstance(params, dict):
        item = params.get("item")
        if isinstance(item, dict):
            return item
    fallback = data.get("item")
    if isinstance(fallback, dict):
        return fallback
    return {}


async def handle_step(step: Any, agent_id: str, turn_ctx: dict[str, Any]) -> None:
    """Translate one ConversationStep to harness events via `_emit`.

    Pure function over `step` and `turn_ctx`; the only side effect is
    `_emit` calls. Caller (the dispatcher in run_turn) supplies
    `turn_ctx` so accumulated text + got_result can survive across
    steps within a single turn.
    """
    from server.agents import _emit

    item_type = getattr(step, "item_type", None) or ""
    item_id = getattr(step, "item_id", None)
    text = getattr(step, "text", None)
    item_payload = _step_item_payload(step)

    # mcp_tool_call resolves its tool name from payload, so it lives
    # outside the static table.
    if item_type in ("mcp_tool_call", "mcpToolCall"):
        await _emit(
            agent_id,
            "tool_use",
            tool=_resolve_mcp_tool_name(item_payload),
            id=item_id,
            input=item_payload,
        )
        return

    mapping = _ITEM_TYPE_TO_HARNESS.get(item_type)

    if mapping is None:
        logger.info(
            "CodexRuntime: unmapped item_type=%s step_type=%s — skipping",
            item_type, getattr(step, "step_type", None),
        )
        return

    event_type, tool_name = mapping

    if event_type == "_skip":
        return

    if event_type == "text":
        # Set got_result FIRST so a tool-only turn that ends with an
        # empty final_answer step still flips the dispatcher's flag.
        # The Claude path's ResultMessage works the same way (presence
        # of the marker matters, not whether content is non-empty).
        phase = item_payload.get("phase")
        if phase == "final_answer":
            turn_ctx["got_result"] = True
        if not text:
            return
        accumulated = turn_ctx.get("accumulated_text", "") + text
        turn_ctx["accumulated_text"] = accumulated
        await _emit(agent_id, "text", text=text)
        return

    if event_type == "thinking":
        # Reasoning items may be ['summary'] or ['text']; pass through
        # whatever's in the payload so the UI renderer can render
        # whichever shape the SDK emits.
        await _emit(
            agent_id,
            "thinking",
            text=text or item_payload.get("text") or item_payload.get("summary"),
            id=item_id,
        )
        return

    if event_type == "tool_use":
        # Args extraction is permissive: pass the full item payload
        # through under `input` so the existing per-tool renderers (Bash
        # card, Edit diff, WebSearch card) can pick the keys they want
        # without the dispatcher pre-flattening.
        await _emit(
            agent_id,
            "tool_use",
            tool=tool_name,
            id=item_id,
            input=item_payload,
        )
        result_text = _extract_step_tool_result(item_payload)
        if result_text:
            await _emit(
                agent_id,
                "tool_result",
                tool_use_id=item_id,
                content=result_text,
                is_error=bool(_step_payload_is_error(item_payload)),
            )
        return


async def _await_if_needed(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _coord_proxy_url() -> str:
    explicit = os.environ.get("HARNESS_COORD_PROXY_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    port = os.environ.get("PORT", "").strip() or "8000"
    return f"http://127.0.0.1:{port}"


def _build_mcp_servers(tc: TurnContext) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    servers["coord"] = {
        "command": sys.executable,
        "args": [
            "-m",
            "server.coord_mcp",
            "--caller-id",
            tc.agent_id,
            "--proxy-url",
            _coord_proxy_url(),
        ],
        "env": {
            "HARNESS_COORD_PROXY_TOKEN": tc.turn_ctx.get("coord_proxy_token", ""),
        },
    }
    for name, cfg in (tc.external_mcp_servers or {}).items():
        if name == "coord":
            continue
        servers[name] = cfg
    return servers


def _build_thread_config(sdk: Any, tc: TurnContext) -> Any:
    """Build the SDK ThreadConfig while tolerating fake SDKs in tests."""
    kwargs: dict[str, Any] = {
        "cwd": tc.workspace_cwd or None,
        "developer_instructions": tc.system_prompt or None,
        "approval_policy": "never",
        "sandbox": "danger-full-access",
        "config": {"mcp_servers": _build_mcp_servers(tc)},
    }
    if tc.model:
        kwargs["model"] = tc.model
    cls = getattr(sdk, "ThreadConfig", None)
    if cls is None:
        return kwargs
    return cls(**kwargs)


_CODEX_EFFORT_LEVELS = {
    1: "low",
    2: "medium",
    3: "high",
    4: "xhigh",
}


def _build_turn_overrides(sdk: Any, tc: TurnContext) -> Any | None:
    kwargs: dict[str, Any] = {}
    if tc.workspace_cwd:
        kwargs["cwd"] = tc.workspace_cwd
    if tc.model:
        kwargs["model"] = tc.model
    effort = _CODEX_EFFORT_LEVELS.get(tc.effort or 0)
    if effort:
        kwargs["effort"] = effort
    if not kwargs:
        return None
    cls = getattr(sdk, "TurnOverrides", None)
    if cls is None:
        return kwargs
    return cls(**kwargs)


def _extract_step_tool_result(item_payload: Mapping[str, Any]) -> str | None:
    """Pull a concise result body from completed Codex tool items.

    The live SDK emits completed items rather than Claude's separate
    tool_use/tool_result pair. Known keys are still drifting, so accept
    common output shapes and leave unknown payloads as invocation-only.
    """
    for key in (
        "output",
        "stdout",
        "stderr",
        "result",
        "content",
        "diff",
        "message",
    ):
        value = item_payload.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text[:12000]
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, Mapping):
                    t = item.get("text") or item.get("content") or item.get("output")
                    if t is not None:
                        parts.append(str(t))
            text = "\n".join(p for p in parts if p).strip()
            if text:
                return text[:12000]
        if isinstance(value, Mapping):
            bits: list[str] = []
            for nested_key in ("stdout", "stderr", "output", "text", "message"):
                nested = value.get(nested_key)
                if nested:
                    bits.append(str(nested))
            text = "\n".join(bits).strip()
            if text:
                return text[:12000]
    return None


def _step_payload_is_error(item_payload: Mapping[str, Any]) -> bool:
    status = str(item_payload.get("status") or item_payload.get("state") or "").lower()
    if "error" in status or "fail" in status:
        return True
    exit_code = (
        item_payload.get("exit_code")
        or item_payload.get("exitCode")
        or item_payload.get("returncode")
    )
    try:
        return int(exit_code) != 0
    except (TypeError, ValueError):
        return False


def _to_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            return dumped if isinstance(dumped, Mapping) else None
        except Exception:
            return None
    if hasattr(value, "__dict__"):
        return vars(value)
    return None


def _find_first_mapping_by_key(payload: Any, key_lower: str) -> Mapping[str, Any] | None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).lower() == key_lower:
                mapped = _to_mapping(value)
                if mapped is not None:
                    return mapped
        for value in payload.values():
            found = _find_first_mapping_by_key(value, key_lower)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_first_mapping_by_key(item, key_lower)
            if found is not None:
                return found
    return None


def _find_turn_payload(thread_state: Any, turn_id: str | None) -> Mapping[str, Any] | None:
    mapped = _to_mapping(thread_state)
    if mapped is None:
        return None
    turns = None
    thread_obj = mapped.get("thread")
    if isinstance(thread_obj, Mapping):
        turns = thread_obj.get("turns")
    if turns is None:
        turns = mapped.get("turns")
    if not isinstance(turns, list) or not turns:
        return None
    if turn_id:
        for turn in turns:
            turn_map = _to_mapping(turn)
            if turn_map is not None and turn_map.get("id") == turn_id:
                return turn_map
    return _to_mapping(turns[-1])


def _extract_codex_usage_from_thread_state(
    thread_state: Any,
    turn_id: str | None,
) -> Any:
    turn = _find_turn_payload(thread_state, turn_id)
    if turn is None:
        return None
    usage = turn.get("usage")
    if usage is not None:
        return usage
    metrics = turn.get("metrics")
    if isinstance(metrics, Mapping) and metrics.get("usage") is not None:
        return metrics.get("usage")
    token_usage = _find_first_mapping_by_key(turn, "usage")
    return token_usage


def _extract_compact_summary(raw: Any) -> str:
    mapped = _to_mapping(raw)
    if mapped is not None:
        for key in ("summary", "text", "content", "message"):
            value = mapped.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        nested = _find_first_mapping_by_key(mapped, "summary")
        if nested is not None:
            for key in ("text", "content", "message"):
                value = nested.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    if isinstance(raw, str):
        return raw.strip()
    return str(raw).strip() if raw is not None else ""


def is_enabled() -> bool:
    """Feature-flag gate. Default off — PR 5 ships the runtime
    structurally; flipping the env var enables actual Codex turns."""
    return os.environ.get("HARNESS_CODEX_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


async def resolve_auth() -> tuple[str, dict[str, str]]:
    """Resolve which Codex auth path to use, and any env overrides
    the runtime body should apply when spawning the SDK subprocess.

    Resolution (matches `Docs/CODEX_RUNTIME_SPEC.md` §D.4):

      1. ChatGPT session present at $CODEX_HOME/auth.json — let the
         Codex CLI read it directly. No env override needed.
      2. Else, encrypted `secrets.openai_api_key` is set — return
         `OPENAI_API_KEY=<value>` so the runtime can inject it into
         the subprocess env (never argv).
      3. Else, return `('none', {})` — caller emits human_attention
         and aborts the spawn.

    Returns `(method, env_overrides)`:
      - method ∈ {'chatgpt', 'api_key', 'none'}
      - env_overrides: dict to merge into the subprocess env (always
        empty for 'chatgpt' and 'none').
    """
    from pathlib import Path

    codex_dir = os.environ.get("CODEX_HOME", "").strip()
    if codex_dir:
        auth_path = Path(codex_dir) / "auth.json"
        try:
            if auth_path.exists() and auth_path.stat().st_size > 0:
                return ("chatgpt", {})
        except OSError:
            # Filesystem error reading auth.json — fall through to
            # API-key path rather than crashing the spawn.
            logger.warning("CodexRuntime: failed to stat %s", auth_path)

    try:
        from server.secrets import get_secret
        api_key = await get_secret("openai_api_key")
    except Exception:
        logger.exception("CodexRuntime: secrets store unavailable")
        api_key = None
    if api_key:
        return ("api_key", {"OPENAI_API_KEY": api_key})

    return ("none", {})


def _import_codex_sdk() -> Any:
    """Lazy SDK import.

    Raises ImportError with a friendly message if the package isn't
    installed (pinned in pyproject.toml as `codex-app-server-sdk>=0.3.2`,
    confirmed live 2026-04-28 — see Docs/CODEX_PROBE_OUTPUT.md).
    """
    try:
        import codex_app_server_sdk as _sdk  # type: ignore[import]
        return _sdk
    except ImportError as exc:
        raise ImportError(
            "Codex SDK not installed. Add `codex-app-server-sdk>=0.3.2` to "
            "pyproject.toml dependencies. See Docs/CODEX_RUNTIME_SPEC.md §I.1."
        ) from exc


class CodexRuntime:
    """Per the AgentRuntime protocol; OpenAI Codex backed."""

    name: str = "codex"

    async def prepare_turn_start(self, tc: TurnContext) -> bool:
        """Prepare a non-compact turn before `agent_started`.

        Stale Codex thread IDs are only discovered by calling
        `resume_thread()`. Preparing here lets the dispatcher publish
        `agent_started.resumed_session` from the actual thread handle
        outcome instead of the optimistic DB value.
        """
        if tc.compact_mode:
            return bool(tc.prior_session)

        method, env_overrides = await resolve_auth()
        tc.turn_ctx["codex_auth_method"] = method
        tc.turn_ctx["codex_env_overrides"] = env_overrides
        if method == "none":
            tc.turn_ctx["codex_resumed_session"] = False
            return False

        try:
            sdk = _import_codex_sdk()
        except ImportError as exc:
            tc.turn_ctx["_codex_prepare_import_error"] = exc
            tc.turn_ctx["codex_resumed_session"] = False
            return False

        try:
            client = await get_client(
                tc.agent_id,
                cwd=tc.workspace_cwd,
                env_overrides=env_overrides,
            )
            config = _build_thread_config(sdk, tc)
            turn_overrides = _build_turn_overrides(sdk, tc)

            if hasattr(client, "set_approval_handler"):
                from server.agents import _emit

                async def _approval_handler(request: Any) -> str:
                    await _emit(
                        tc.agent_id,
                        "human_attention",
                        subject="Codex requested an unsupported approval",
                        body=(
                            "Codex requested approval for a command or file "
                            "change. TeamOfTen v1 declines these side-channel "
                            "approvals; use coord_request_human from the agent "
                            "conversation when human input is needed."
                        ),
                        urgency="high",
                        request_type=type(request).__name__,
                    )
                    return "decline"

                client.set_approval_handler(_approval_handler)

            thread, resumed = await open_thread(tc.agent_id, client, config=config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await close_client(tc.agent_id)
            tc.turn_ctx["_codex_prepare_error"] = exc
            tc.turn_ctx["codex_resumed_session"] = False
            return False

        tc.turn_ctx["_codex_prepared_turn"] = {
            "method": method,
            "env_overrides": env_overrides,
            "sdk": sdk,
            "client": client,
            "config": config,
            "turn_overrides": turn_overrides,
            "thread": thread,
            "resumed": resumed,
        }
        tc.turn_ctx["codex_resumed_session"] = resumed
        return bool(resumed)

    async def run_turn(self, tc: TurnContext) -> None:
        if not is_enabled():
            await self._emit_disabled_attention(tc)
            return

        # Lazy import the rest of the harness machinery — agents.py is
        # the dispatcher and its imports cycle through us.
        from server.agents import (
            _add_cost,
            _append_exchange,
            _emit,
            _extract_usage_codex,
            _insert_turn_row,
            _now,
            _set_continuity_note,
            _set_status,
        )
        from server.pricing import codex_cost_usd

        prepared = tc.turn_ctx.pop("_codex_prepared_turn", None)
        prepare_error = tc.turn_ctx.pop("_codex_prepare_error", None)
        if prepare_error is not None:
            raise prepare_error

        # Auth resolution per §D.4 — ChatGPT session, then API-key
        # fallback, then human_attention abort. Stash the resolved
        # method + env overrides on turn_ctx so the SDK subprocess
        # body (when wired) can inject OPENAI_API_KEY without
        # re-querying the secrets store.
        if prepared:
            method = prepared["method"]
            env_overrides = prepared["env_overrides"]
        else:
            method, env_overrides = await resolve_auth()
        if method == "none":
            await _emit(
                tc.agent_id,
                "human_attention",
                subject="Codex runtime has no auth configured",
                body=(
                    "Neither a ChatGPT session at $CODEX_HOME/auth.json nor "
                    "a saved OPENAI_API_KEY was found. Run `codex login` "
                    "inside the container, or save an API key via the "
                    "Options drawer → Codex auth section."
                ),
                urgency="high",
            )
            await _emit(tc.agent_id, "error", error="Codex auth unavailable")
            await _set_status(tc.agent_id, "error")
            return
        tc.turn_ctx["codex_auth_method"] = method
        tc.turn_ctx["codex_env_overrides"] = env_overrides

        import_error = tc.turn_ctx.pop("_codex_prepare_import_error", None)
        try:
            if import_error is not None:
                raise import_error
            sdk = prepared["sdk"] if prepared else _import_codex_sdk()
        except ImportError as exc:
            logger.exception("CodexRuntime: SDK import failed for %s", tc.agent_id)
            await _emit(
                tc.agent_id,
                "human_attention",
                subject="Codex runtime unavailable",
                body=str(exc),
                urgency="high",
            )
            await _emit(tc.agent_id, "error", error=f"ImportError: {exc}")
            await _set_status(tc.agent_id, "error")
            return

        if prepared:
            client = prepared["client"]
            config = prepared["config"]
            turn_overrides = prepared["turn_overrides"]
        else:
            client = await get_client(
                tc.agent_id,
                cwd=tc.workspace_cwd,
                env_overrides=env_overrides,
            )
            config = _build_thread_config(sdk, tc)
            turn_overrides = _build_turn_overrides(sdk, tc)

        # Codex exposes approval requests as a side channel. The thread
        # config asks for "never", but if an SDK/server mismatch still
        # produces an approval, surface it and decline so the turn does
        # not hang behind an invisible prompt.
        if not prepared and hasattr(client, "set_approval_handler"):
            async def _approval_handler(request: Any) -> str:
                await _emit(
                    tc.agent_id,
                    "human_attention",
                    subject="Codex requested an unsupported approval",
                    body=(
                        "Codex requested approval for a command or file "
                        "change. TeamOfTen v1 declines these side-channel "
                        "approvals; use coord_request_human from the agent "
                        "conversation when human input is needed."
                    ),
                    urgency="high",
                    request_type=type(request).__name__,
                )
                return "decline"
            client.set_approval_handler(_approval_handler)

        thread = None
        resumed = False
        final_turn_id: str | None = None
        started_at = _now()
        started_monotonic = time.monotonic()

        try:
            if prepared:
                thread = prepared["thread"]
                resumed = bool(prepared["resumed"])
            else:
                thread, resumed = await open_thread(tc.agent_id, client, config=config)
            tc.turn_ctx["codex_resumed_session"] = resumed

            stream = thread.chat(
                tc.prompt,
                user=tc.agent_id,
                metadata={"project_id": tc.project_id},
                turn_overrides=turn_overrides,
            )
            stream = await _await_if_needed(stream)
            async for step in stream:
                if getattr(step, "turn_id", None):
                    final_turn_id = getattr(step, "turn_id")
                await handle_step(step, tc.agent_id, tc.turn_ctx)

            thread_id = getattr(thread, "thread_id", None)
            if thread_id:
                await _set_codex_thread_id(tc.agent_id, thread_id)

            if not tc.turn_ctx.get("got_result"):
                # Some tool-only turns may complete without an
                # agentMessage final_answer in the stream. Exhaustion
                # of thread.chat() is the SDK's terminal signal.
                tc.turn_ctx["got_result"] = True

            usage_raw = None
            try:
                read = thread.read(include_turns=True)
                thread_state = await _await_if_needed(read)
                usage_raw = _extract_codex_usage_from_thread_state(
                    thread_state,
                    final_turn_id,
                )
            except Exception:
                logger.exception(
                    "CodexRuntime: failed to read thread usage for slot=%s",
                    tc.agent_id,
                )
            usage = _extract_usage_codex(usage_raw)

            cost_basis = "plan_included" if method == "chatgpt" else "token_priced"
            if cost_basis == "plan_included":
                cost_usd = 0.0
            else:
                cost_usd = codex_cost_usd(
                    tc.model,
                    {
                        "input_tokens": usage["input"],
                        "cached_input_tokens": usage["cache_read"],
                        "output_tokens": usage["output"],
                    },
                )
            duration_ms = int((time.monotonic() - started_monotonic) * 1000)
            await _emit(
                tc.agent_id,
                "result",
                duration_ms=duration_ms,
                cost_usd=cost_usd,
                is_error=False,
                session_id=thread_id,
                stop_reason=None,
                subtype=None,
                num_turns=None,
                errors=None,
            )
            if not tc.compact_mode:
                if tc.turn_ctx.get("had_handoff_on_entry"):
                    await _set_continuity_note(tc.agent_id, None)
                response_text = (
                    tc.turn_ctx.get("accumulated_text")
                    or tc.turn_ctx.get("response_text")
                    or ""
                ).strip()
                entry_prompt = (tc.turn_ctx.get("entry_prompt") or "").strip()
                if response_text and entry_prompt:
                    await _append_exchange(tc.agent_id, entry_prompt, response_text)
            await _insert_turn_row(
                agent_id=tc.agent_id,
                started_at=started_at,
                ended_at=_now(),
                duration_ms=duration_ms,
                cost_usd=cost_usd,
                session_id=thread_id,
                num_turns=None,
                stop_reason=None,
                is_error=False,
                model=tc.model,
                runtime="codex",
                cost_basis=cost_basis,
                plan_mode=tc.plan_mode,
                effort=tc.effort,
                input_tokens=usage["input"],
                output_tokens=usage["output"],
                cache_read_tokens=usage["cache_read"],
                cache_creation_tokens=usage["cache_creation"],
            )
            await _add_cost(tc.agent_id, cost_usd)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Transport/protocol failures often poison the app-server
            # stdio session. Drop the cached client so the dispatcher
            # retry gets a fresh subprocess.
            await close_client(tc.agent_id)
            raise
        finally:
            if hasattr(client, "set_approval_handler"):
                try:
                    client.set_approval_handler(None)
                except Exception:
                    logger.exception(
                        "CodexRuntime: clearing approval handler failed for %s",
                        tc.agent_id,
                    )

    async def maybe_auto_compact(self, tc: TurnContext) -> bool:
        """Disabled in v1 — Codex app-server doesn't expose a usable
        context-pressure signal yet. Return False so the dispatcher
        proceeds straight to run_turn. See spec §A.5 / §E.6."""
        return False

    async def run_manual_compact(self, tc: TurnContext) -> None:
        """Compact the agent's Codex thread via the native SDK call.

        Live spike confirmed `client.compact_thread(thread_id)` exists
        and `ThreadHandle.compact()` exists. Use the client form so we
        don't need to materialize a ThreadHandle just to call compact.

        Audit item #14 — Docs/CODEX_RUNTIME_SPEC.md §E.6.

        Flow:
          1. Auth resolution — if no auth, emit human_attention + error.
          2. Read codex_thread_id; if null, no-op success (nothing to
             compact, but the user invoked /compact so flip got_result
             to keep the dispatcher happy).
          3. get_client (cached or fresh) and call compact_thread(id).
          4. Defensively extract a summary from the opaque return shape
             (dict.summary / .text / repr fallback).
          5. Persist via `_set_continuity_note`, then null
             `codex_thread_id` so the next non-compact turn starts a
             fresh Codex thread that picks up the continuity note from
             the system prompt.
          6. Emit `session_compacted` and flip got_result.
        """
        if not is_enabled():
            await self._emit_disabled_attention(tc)
            return

        from server.agents import _emit, _set_status, _set_continuity_note
        from server.workspaces import workspace_dir

        method, env_overrides = await resolve_auth()
        if method == "none":
            await _emit(
                tc.agent_id,
                "human_attention",
                subject="Codex /compact: no auth configured",
                body=(
                    "Run `codex login` inside the container or save an "
                    "API key in the Options drawer → Codex auth section."
                ),
                urgency="high",
            )
            await _emit(tc.agent_id, "error", error="Codex auth unavailable")
            await _set_status(tc.agent_id, "error")
            return

        thread_id = await _get_codex_thread_id(tc.agent_id)
        if not thread_id:
            # No prior thread → nothing to compact. Treat as no-op success
            # so the dispatcher's /compact slash command doesn't loop.
            await _emit(
                tc.agent_id,
                "session_compacted",
                note="no codex thread to compact (fresh session)",
            )
            tc.turn_ctx["got_result"] = True
            return

        try:
            client = await get_client(
                tc.agent_id,
                cwd=str(workspace_dir(tc.agent_id)),
                env_overrides=env_overrides,
            )
        except ImportError as exc:
            await _emit(
                tc.agent_id,
                "human_attention",
                subject="Codex /compact: SDK unavailable",
                body=str(exc),
                urgency="high",
            )
            await _emit(tc.agent_id, "error", error=f"ImportError: {exc}")
            await _set_status(tc.agent_id, "error")
            return

        try:
            raw = client.compact_thread(thread_id)
            if hasattr(raw, "__await__"):
                raw = await raw
        except Exception as exc:
            logger.exception(
                "CodexRuntime: compact_thread failed for slot=%s thread=%s",
                tc.agent_id, thread_id,
            )
            # Drop the cached client — compact failures often correlate
            # with stale thread state on the subprocess side.
            await close_client(tc.agent_id)
            await _emit(tc.agent_id, "error", error=f"compact failed: {exc}")
            await _set_status(tc.agent_id, "error")
            return

        summary = _extract_compact_summary(raw)
        if summary:
            await _set_continuity_note(tc.agent_id, summary)
        await _clear_codex_thread_id(tc.agent_id)

        await _emit(
            tc.agent_id,
            "session_compacted",
            summary_preview=(summary[:200] if summary else None),
        )
        tc.turn_ctx["got_result"] = True

    async def _emit_disabled_attention(self, tc: TurnContext) -> None:
        from server.agents import _emit, _set_status
        await _emit(
            tc.agent_id,
            "error",
            error=(
                "Codex runtime selected but HARNESS_CODEX_ENABLED is "
                "unset. Either flip the env or change runtime back to "
                "Claude via the pane settings popover."
            ),
        )
        await _set_status(tc.agent_id, "error")

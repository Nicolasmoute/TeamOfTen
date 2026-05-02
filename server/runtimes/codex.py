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
import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from server.runtimes.base import TurnContext

logger = logging.getLogger(__name__)


# Module-level cache of `CodexClient` instances per slot. The harness
# already serializes turns per slot via `_SPAWN_LOCK` (see agents.py),
# satisfying the SDK's "one active turn consumer per client" rule.
# Closed and re-opened on auth-error / transport error.
_codex_clients: dict[str, Any] = {}

# Per-slot coord-MCP-proxy tokens. The codex app-server subprocess is
# long-lived (cached across turns), and the env it inherits — including
# `HARNESS_COORD_PROXY_TOKEN` — is captured at spawn time. A per-turn
# mint/revoke would invalidate the token used by the running subprocess
# after turn 1, causing 401s on every subsequent turn's MCP call. So
# the runtime mints a token bound to the client's lifetime and revokes
# it in `close_client` — same identity-binding guarantees, scoped to
# the subprocess instead of the turn.
_codex_client_tokens: dict[str, str] = {}

# Per-slot async locks to serialize get-or-create. The dispatcher's
# _SPAWN_LOCK already serializes whole turns per slot, but a defensive
# lock here lets `get_client` / `close_client` be safely called from
# health probes / shutdown handlers that don't hold the spawn lock.
_client_locks: dict[str, asyncio.Lock] = {}

# Bump when the Codex-visible coord tool contract changes in a way that
# old persisted Codex threads might not pick up on resume.
_CODEX_TOOL_CONTRACT_VERSION = "2026-05-02.coord-set-player-runtime"


class _CapturedStdioTransport:
    """SDK-compatible stdio transport that keeps a stderr tail.

    codex-app-server-sdk 0.3.2's bundled StdioTransport sends stderr to
    DEVNULL. That makes app-server crashes show up as the opaque
    "failed reading from stdio transport" error. This transport mirrors
    the SDK behavior but pipes stderr into a bounded in-memory tail so
    the harness error event has something actionable.
    """

    def __init__(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        connect_timeout: float = 30.0,
        transport_error_cls: type[Exception] = RuntimeError,
        stderr_limit: int = 12000,
    ) -> None:
        if not command:
            raise ValueError("stdio command must not be empty")
        self._command = list(command)
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._connect_timeout = connect_timeout
        self._transport_error_cls = transport_error_cls
        self._stderr_limit = stderr_limit
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_tail = ""
        self._stderr_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *self._command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self._cwd,
                    env=self._env,
                ),
                timeout=self._connect_timeout,
            )
        except Exception as exc:  # pragma: no cover
            raise self._transport_error_cls(
                f"failed to start stdio transport command: {self._command!r}"
            ) from exc
        if self._proc.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace")
            self._stderr_tail = (self._stderr_tail + text)[-self._stderr_limit:]

    def _message_with_diagnostics(self, message: str) -> str:
        bits = [message]
        proc = self._proc
        if proc is not None and proc.returncode is not None:
            bits.append(f"process exit code: {proc.returncode}")
        tail = self._stderr_tail.strip()
        if tail:
            bits.append("stderr tail:\n" + tail)
        return "\n".join(bits)

    def _raise_transport(self, message: str, exc: Exception | None = None) -> None:
        raise self._transport_error_cls(
            self._message_with_diagnostics(message)
        ) from exc

    async def send(self, payload: Mapping[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            self._raise_transport("stdio transport is not connected")
        line = json_dumps_compact(dict(payload)) + "\n"
        try:
            self._proc.stdin.write(line.encode("utf-8"))
            await self._proc.stdin.drain()
        except Exception as exc:
            self._raise_transport("failed writing to stdio transport", exc)

    async def recv(self) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            self._raise_transport("stdio transport is not connected")
        try:
            line = await self._proc.stdout.readline()
        except Exception as exc:
            self._raise_transport("failed reading from stdio transport", exc)
        if not line:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._proc.wait(), timeout=0.05)
            if self._stderr_task is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        asyncio.shield(self._stderr_task),
                        timeout=0.05,
                    )
            self._raise_transport("stdio transport closed")
        try:
            import json

            return json.loads(line.decode("utf-8"))
        except Exception as exc:
            self._raise_transport("received invalid JSON from stdio transport", exc)

    async def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=0.5)
            except asyncio.TimeoutError:
                self._stderr_task.cancel()


def json_dumps_compact(value: Mapping[str, Any]) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))


def _install_captured_stdio_transport(sdk: Any) -> None:
    """Patch the SDK's connect_stdio factory to use stderr capture."""
    transport_error_cls = getattr(sdk, "CodexTransportError", None)
    client_cls = getattr(sdk, "CodexClient", None)
    module_name = getattr(client_cls, "__module__", "")
    module = sys.modules.get(module_name)
    if transport_error_cls is None or module is None:
        return
    if getattr(module, "_harness_stdio_capture_installed", False):
        return

    class _HarnessCapturedTransport(_CapturedStdioTransport):
        def __init__(
            self,
            command: list[str],
            *,
            cwd: str | None = None,
            env: Mapping[str, str] | None = None,
            connect_timeout: float = 30.0,
        ) -> None:
            super().__init__(
                command,
                cwd=cwd,
                env=env,
                connect_timeout=connect_timeout,
                transport_error_cls=transport_error_cls,
            )

    setattr(module, "StdioTransport", _HarnessCapturedTransport)
    setattr(module, "_harness_stdio_capture_installed", True)


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

        # Mint the coord-proxy token here, not in the dispatcher: the
        # subprocess we're about to spawn captures its env once and
        # uses that token forever. The dispatcher's per-turn mint
        # would be invalidated as soon as turn 1 ended, breaking
        # every subsequent turn's `coord_*` call with HTTP 401.
        from server.spawn_tokens import mint as _mint_proxy_token
        token = _mint_proxy_token(slot)
        _codex_client_tokens[slot] = token
        env["HARNESS_COORD_PROXY_TOKEN"] = token

        _install_captured_stdio_transport(sdk)
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
            # client cached. Close best-effort, revoke the token we
            # just minted (the subprocess that would have used it never
            # came up), and re-raise.
            try:
                close = client.close()
                if hasattr(close, "__await__"):
                    await close
            except Exception:
                logger.exception(
                    "CodexRuntime: close() during failed handshake raised "
                    "for slot %s", slot,
                )
            from server.spawn_tokens import revoke as _revoke_proxy_token
            stale = _codex_client_tokens.pop(slot, None)
            if stale:
                _revoke_proxy_token(stale)
            raise

        _codex_clients[slot] = client
        logger.info("CodexRuntime: opened client for slot=%s", slot)
        return client


async def close_client(slot: str) -> None:
    """Close + drop the cached client for `slot`. Safe if no client is
    cached. Called on auth-error / transport-error / shutdown."""
    async with _slot_lock(slot):
        client = _codex_clients.pop(slot, None)
        # Revoke the proxy token bound to this subprocess. Done
        # whether or not the client object existed — defensive cleanup.
        token = _codex_client_tokens.pop(slot, None)
        if token:
            from server.spawn_tokens import revoke as _revoke_proxy_token
            _revoke_proxy_token(token)
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


async def evict_client(slot: str) -> None:
    """Drop the cached app-server client so the next turn rebuilds it.

    Use case: MCP config / session state changed and the long-lived
    subprocess (which captured `mcp_servers` at spawn time via
    `_codex_config_overrides`) needs to be replaced before the agent
    can see the new tool surface.

    Behavior splits on whether a turn is in flight:
    - Idle slot → full `close_client` (closes subprocess, revokes token).
    - In-flight turn → pop from `_codex_clients` only; leave the running
      subprocess + its token intact so the live turn can complete. The
      next turn's `get_client` lookup creates a fresh subprocess that
      picks up current MCP config. The orphaned subprocess is a small
      leak until container restart — acceptable trade-off vs killing a
      live turn from an admin-side config change.
    """
    try:
        from server.agents import is_agent_running
    except Exception:
        is_agent_running = lambda _slot: False  # noqa: E731

    if is_agent_running(slot):
        async with _slot_lock(slot):
            _codex_clients.pop(slot, None)
        logger.info(
            "CodexRuntime: evicted cache entry for slot=%s "
            "(turn in flight; subprocess kept alive)", slot,
        )
        return
    await close_client(slot)


async def evict_all_clients() -> None:
    """Evict every cached client. Idle slots get a full close; slots
    with an in-flight turn get cache-popped only. Called from MCP
    server save/patch/delete so config changes propagate without a
    full server restart."""
    slots = list(_codex_clients.keys())
    for slot in slots:
        await evict_client(slot)


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


async def ensure_codex_tool_contract_current() -> int:
    """Clear stale Codex thread ids after a tool-contract bump.

    Codex app-server can preserve thread-local tool state across
    resumes. When TeamOfTen changes how coord MCP tools are exposed or
    described, old thread ids can keep telling the model a coord tool is
    unavailable "in this session". This one-time boot migration forces
    the next Codex turn to start with the current MCP config.
    """
    from server.db import configured_conn

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = ?",
            ("codex_tool_contract_version",),
        )
        row = await cur.fetchone()
        current = dict(row)["value"] if row else None
        if current == _CODEX_TOOL_CONTRACT_VERSION:
            return 0
        cur = await c.execute(
            "UPDATE agent_sessions SET codex_thread_id = NULL "
            "WHERE codex_thread_id IS NOT NULL"
        )
        cleared = cur.rowcount if cur.rowcount is not None else 0
        await c.execute(
            "INSERT INTO team_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("codex_tool_contract_version", _CODEX_TOOL_CONTRACT_VERSION),
        )
        await c.commit()
        return int(cleared or 0)
    finally:
        await c.close()


# Number of extra resume attempts on `CodexTimeoutError` before we give
# up and fall back to `start_thread`. The SDK's default `request_timeout`
# is 30s; under load (cold app-server subprocess, slow Codex backend, or
# a thread with substantial state — Coach is the usual victim) `thread/
# resume` can transiently exceed it. Treating every timeout as a stale
# thread loses continuity unnecessarily; retrying first preserves it.
# Genuine stale-thread errors raise `CodexProtocolError` immediately and
# don't go through this path.
_CODEX_RESUME_TIMEOUT_RETRIES = 2
_CODEX_RESUME_TIMEOUT_RETRY_DELAY = 1.0


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

    `CodexTimeoutError` on `thread/resume` is retried a small number of
    times before falling back, since it's typically a transient backend
    blip rather than a real stale-thread signal — see §E.2.

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
        sdk = _import_codex_sdk()
        timeout_cls = getattr(sdk, "CodexTimeoutError", None)
        last_exc: Exception | None = None
        for attempt in range(_CODEX_RESUME_TIMEOUT_RETRIES + 1):
            try:
                r = client.resume_thread(existing, overrides=config)
                if hasattr(r, "__await__"):
                    r = await r
                return (r, True)
            except Exception as exc:
                last_exc = exc
                is_timeout = (
                    timeout_cls is not None and isinstance(exc, timeout_cls)
                )
                if is_timeout and attempt < _CODEX_RESUME_TIMEOUT_RETRIES:
                    logger.warning(
                        "CodexRuntime: resume_thread timed out for slot=%s "
                        "thread_id=%s (attempt %d/%d) — retrying",
                        agent_id, existing,
                        attempt + 1, _CODEX_RESUME_TIMEOUT_RETRIES + 1,
                    )
                    await asyncio.sleep(_CODEX_RESUME_TIMEOUT_RETRY_DELAY)
                    continue
                break

        assert last_exc is not None
        logger.exception(
            "CodexRuntime: resume_thread failed for slot=%s "
            "thread_id=%s — clearing and retrying with start_thread",
            agent_id, existing,
            exc_info=last_exc,
        )
        try:
            from server.agents import _emit
            await _emit(
                agent_id,
                "session_resume_failed",
                session_id=existing,
                error=f"{type(last_exc).__name__}: {last_exc}",
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


_MCP_TOOL_METADATA_KEYS = {
    "type",
    "id",
    "server",
    "server_name",
    "serverName",
    "mcp_server",
    "mcpServer",
    "name",
    "tool_name",
    "toolName",
    "tool",
    "args",
    "arguments",
    "input",
    "params",
    "result",
    "output",
    "response",
    "content",
    "status",
    "state",
    "durationMs",
    "duration_ms",
}


def _mcp_payload_views(item_payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    views: list[Mapping[str, Any]] = [item_payload]
    for key in ("call", "toolCall", "tool_call", "mcp", "mcpToolCall"):
        nested = item_payload.get(key)
        if isinstance(nested, Mapping):
            views.append(nested)
    return views


def _json_object_from_string(value: str) -> dict[str, Any] | None:
    text = value.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _resolve_mcp_tool_name(item_payload: Mapping[str, Any]) -> str:
    """Build the Claude-convention `mcp__<server>__<name>` string from
    a Codex `mcp_tool_call` item payload. SDK payload keys have varied
    across probes, so this looks up several plausible spellings and
    falls back to a marker name on miss.
    """
    server: Any = None
    name: Any = None
    for view in _mcp_payload_views(item_payload):
        server = server or (
            view.get("server")
            or view.get("server_name")
            or view.get("serverName")
            or view.get("mcp_server")
            or view.get("mcpServer")
        )
        name = name or (
            view.get("tool_name")
            or view.get("toolName")
            or view.get("tool")
            or view.get("name")
        )
    server_s = str(server or "unknown")
    name_s = str(name or "unknown")
    if name_s.startswith("mcp__"):
        return name_s
    return f"mcp__{server_s}__{name_s}"


def _extract_mcp_tool_input(item_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the actual MCP arguments, not the Codex wrapper object."""
    for view in _mcp_payload_views(item_payload):
        for key in ("args", "arguments", "input", "params"):
            value = view.get(key)
            if isinstance(value, Mapping):
                return dict(value)
            if isinstance(value, str):
                parsed = _json_object_from_string(value)
                if parsed is not None:
                    return parsed
                if value.strip():
                    return {"arguments": value}

    # Some SDK/protocol builds may flatten MCP arguments at the top
    # level. Preserve those user fields while dropping wrapper/result
    # metadata so coord_* renderers can still summarize the call.
    flattened: dict[str, Any] = {}
    for k, v in item_payload.items():
        if isinstance(v, (dict, list)) or v is None:
            continue
        key = str(k)
        if key == "name":
            # `name` is ambiguous: some protocol drafts use it for the
            # MCP tool name, while coord_set_player_role uses it for the
            # player's human name. Keep likely argument values.
            if isinstance(v, str) and (v.startswith("coord_") or v.startswith("mcp__")):
                continue
        elif key in _MCP_TOOL_METADATA_KEYS:
            continue
        flattened[key] = v
    return flattened


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
        tool_name = _resolve_mcp_tool_name(item_payload)
        tool_input = _extract_mcp_tool_input(item_payload)
        await _emit(
            agent_id,
            "tool_use",
            name=tool_name,
            tool=tool_name,
            id=item_id,
            input=tool_input,
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
        await _emit(agent_id, "text", content=text, text=text)
        return

    if event_type == "thinking":
        # Reasoning items may be ['summary'] or ['text']; pass through
        # whatever's in the payload so the UI renderer can render
        # whichever shape the SDK emits.
        await _emit(
            agent_id,
            "thinking",
            content=text or item_payload.get("text") or item_payload.get("summary"),
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
            name=tool_name,
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


def _harness_root() -> str:
    return str(Path(__file__).resolve().parents[2])


def _coord_mcp_env(tc: TurnContext) -> dict[str, str]:
    root = _harness_root()
    pythonpath = os.environ.get("PYTHONPATH", "").strip()
    # Prefer an explicit `turn_ctx["coord_proxy_token"]` so unit tests
    # that synthesise a TurnContext can drive what lands in env. In
    # production the dispatcher no longer populates turn_ctx, so we
    # fall back to the runtime-owned token cached against this slot's
    # codex app-server subprocess by `get_client`.
    token = tc.turn_ctx.get("coord_proxy_token") or _codex_client_tokens.get(tc.agent_id, "")
    return {
        "HARNESS_COORD_PROXY_TOKEN": token,
        "PYTHONPATH": root if not pythonpath else os.pathsep.join([root, pythonpath]),
    }


def _build_mcp_servers(tc: TurnContext) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    root = _harness_root()
    servers["coord"] = {
        "type": "stdio",
        "command": sys.executable,
        "cwd": root,
        "args": [
            "-m",
            "server.coord_mcp",
            "--caller-id",
            tc.agent_id,
            "--proxy-url",
            _coord_proxy_url(),
        ],
        "env": _coord_mcp_env(tc),
        # Pre-approve every coord_* tool. Without this, Codex routes
        # MCP calls through the elicitation/approval path under
        # restrictive sandboxes (Coach is read-only) and the embedded
        # client has no user-input handler — the call is auto-cancelled
        # with "user rejected MCP tool call". coord_* is harness-trusted
        # by definition (single write-handle invariant), so blanket
        # approval is the right default. See openai/codex issue #16685
        # and PR #16632 for the upstream context.
        "default_tools_approval_mode": "approve",
    }
    for name, cfg in (tc.external_mcp_servers or {}).items():
        if name == "coord":
            continue
        # Mirror the approval policy applied to `coord`: external MCP
        # servers added through the Options drawer are user-authorized
        # by the act of adding them, so pre-approve their tool calls.
        # Without this, Coach (read-only sandbox) can't invoke any
        # external MCP tool — Codex routes the call through the
        # elicitation/approval path, the embedded app-server client
        # has no `request_user_input` handler, and the call is
        # auto-cancelled with "user rejected MCP tool call". Players
        # (danger-full-access) skip approval so they're unaffected
        # either way. Caller-provided `default_tools_approval_mode` is
        # respected if present (so a user who explicitly wants
        # approval-on-use can keep it).
        if isinstance(cfg, dict) and "default_tools_approval_mode" not in cfg:
            cfg = {**cfg, "default_tools_approval_mode": "approve"}
        servers[name] = cfg
    return servers


_CODEX_CLAUDE_COMPAT_INSTRUCTIONS = """## Codex compatibility note

This harness predates AGENTS.md naming. When working in any repository
or TeamOfTen project, treat every CLAUDE.md file exactly as you would
AGENTS.md/agents.md: read it and follow it as agent instructions for
the applicable directory tree. Treat .claude/ directories exactly as
.agents/ directories: look there for project or agent instructions,
skills, commands, and related metadata. Do not ignore these files or
directories because they use Claude naming.
"""


def _codex_developer_instructions(system_prompt: str | None) -> str:
    body = (system_prompt or "").strip()
    compat = (
        _CODEX_CLAUDE_COMPAT_INSTRUCTIONS
        + "\n\n"
        + _codex_coord_tool_instructions()
        + "\n\n"
        + _codex_web_tool_instructions()
    )
    if body:
        return body + "\n\n" + compat
    return compat


def _codex_web_tool_instructions() -> str:
    """Tell the Codex agent how Claude-shaped web tools map onto its
    native ones. Without this, agents read 'WebSearch is enabled' in
    their context and try to invoke a tool that doesn't exist in
    Codex, then mistakenly conclude the web is unreachable."""
    return (
        "## Web access in Codex\n\n"
        "Claude-shaped tool names (`WebSearch`, `WebFetch`) do not exist "
        "in this runtime. Use Codex's native `web_search` tool when "
        "you would have used `WebSearch` or `WebFetch` — `web_search` "
        "is enabled iff the team-wide WebSearch toggle is on, which "
        "you can assume to be the case if your context lists "
        "`WebSearch` among the allowed tools. There is no per-URL "
        "fetch tool: pass the URL through `web_search` as a query "
        "rather than reaching for `curl` (the read-only sandbox "
        "blocks it for Coach anyway). Do not say 'web access is "
        "unavailable' before attempting `web_search`."
    )


def _codex_coord_tool_instructions() -> str:
    try:
        from server.tools import coord_tool_names
        names = coord_tool_names()
    except Exception:
        logger.exception("CodexRuntime: failed to build coord tool instruction list")
        names = []
    if names:
        tool_list = ", ".join(f"`{name}`" for name in names)
        list_line = f"Current coord MCP tools: {tool_list}."
    else:
        list_line = "Current coord MCP tools are exposed by the `coord` MCP server."
    return (
        "## TeamOfTen coord tools in Codex\n\n"
        "TeamOfTen coord_* tools are exposed through the MCP server named "
        "`coord`. In Codex they may appear as MCP tools named like "
        "`coord_read_inbox`, or internally as `mcp__coord__coord_read_inbox`. "
        "Use those MCP tools directly for board, inbox, memory, role, todo, "
        "and human-escalation work.\n\n"
        + list_line
        + "\n\nDo not use shell commands, direct SQLite/database access, "
        "or HTTP API fallbacks for harness state when a coord_* tool exists. "
        "Do not say a coord_* tool is unavailable unless an attempted MCP "
        "tool call returns an explicit tool-not-found error; if that happens, "
        "report the exact tool error."
    )


def _codex_sandbox_for(agent_id: str) -> str:
    # Coach coordinates through coord_* MCP tools and must not mutate
    # code or harness state through shell/database fallbacks. Players
    # still need full access for repo/test work until a narrower Codex
    # write policy is implemented.
    return "read-only" if agent_id == "coach" else "danger-full-access"


def _codex_config_overrides(tc: TurnContext) -> dict[str, Any]:
    overrides: dict[str, Any] = {
        "mcp_servers": _build_mcp_servers(tc),
    }
    # Translate the team-wide web-access toggle into Codex's native
    # switch. The Settings drawer toggle is stored under the legacy
    # Claude SDK tool names ("WebSearch" / "WebFetch") for backwards
    # compatibility; semantically it means "the team is allowed to
    # use the web". For Codex that maps to `config.web_search = "live"`
    # — the documented setting that gates the model's built-in search.
    # When the operator explicitly enabled the toggle they want fresh
    # results; `cached` doesn't materially reduce prompt-injection
    # risk so it's not a useful default. There's no Codex analogue
    # for per-URL fetch — the developer instructions tell the agent
    # to pass URLs through `web_search` instead of reaching for curl.
    allowed = set(tc.allowed_tools or [])
    if "WebSearch" in allowed or "WebFetch" in allowed:
        overrides["web_search"] = "live"
    return overrides


def _build_thread_config(sdk: Any, tc: TurnContext) -> Any:
    """Build the SDK ThreadConfig while tolerating fake SDKs in tests."""
    kwargs: dict[str, Any] = {
        "cwd": tc.workspace_cwd or None,
        "developer_instructions": _codex_developer_instructions(tc.system_prompt),
        "approval_policy": "never",
        "sandbox": _codex_sandbox_for(tc.agent_id),
        "config": _codex_config_overrides(tc),
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
    # `cancel` / `reject` cover the OpenAI Codex safety-monitor path: a
    # tool call the monitor refuses lands as a "completed" item with
    # status='cancelled' (or similar) and a prose explanation in the
    # body. Without these patterns the result renders green, which made
    # past monitor cancellations indistinguishable from a real success
    # in the UI — and Coach paraphrased them as generic "rejected by
    # the coordination layer" because there was no clean error signal.
    if (
        "error" in status
        or "fail" in status
        or "cancel" in status
        or "reject" in status
    ):
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


def _rollout_path_from_thread_state(thread_state: Any) -> Path | None:
    """Pull the on-disk rollout JSONL path out of a `thread/read` response.

    The Codex CLI writes one JSONL per thread under
    `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*-<thread_id>.jsonl`.
    `thread.read(include_turns=True)` exposes that path on the thread
    object — that's where token usage actually lives in SDK 0.3.2,
    since `thread.turns[*].usage` is empty.
    """
    if isinstance(thread_state, Mapping):
        thread_obj = thread_state.get("thread")
    else:
        thread_obj = getattr(thread_state, "thread", None)
    if isinstance(thread_obj, Mapping):
        path = thread_obj.get("path")
    else:
        path = getattr(thread_obj, "path", None)
    if not path:
        return None
    try:
        p = Path(str(path))
    except (TypeError, ValueError):
        return None
    return p if p.is_file() else None


def _read_codex_token_count_from_rollout(rollout_path: Path) -> Mapping[str, Any] | None:
    """Parse the most recent `token_count` event from a Codex rollout JSONL.

    Codex SDK 0.3.2's `Thread.read(include_turns=True)` returns turn
    objects with no `usage` field, so we go to the on-disk event log
    instead. Each model call writes one or more lines shaped:

        {"timestamp": "...", "type": "event_msg", "payload": {
          "type": "token_count",
          "info": {
            "last_token_usage": {"input_tokens": ..., "cached_input_tokens": ...,
                                 "output_tokens": ..., "reasoning_output_tokens": ...},
            "total_token_usage": {...},
            "model_context_window": ...
          }
        }}

    Returns the `info` block of the latest `token_count` event, or None
    if the file is unreadable / contains no such event. Caller decides
    whether to use `last_token_usage` (per-turn) or `total_token_usage`
    (cumulative).
    """
    try:
        latest_info: Mapping[str, Any] | None = None
        with rollout_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                # Cheap pre-filter — most lines aren't token_count, and
                # JSON parsing every line of a long session would burn
                # CPU per /context poll.
                if not s or '"token_count"' not in s:
                    continue
                try:
                    obj = json.loads(s)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if isinstance(info, dict):
                    latest_info = info
        return latest_info
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("CodexRuntime: failed reading rollout %s", rollout_path)
        return None


def _codex_usage_from_rollout_info(info: Mapping[str, Any]) -> dict[str, int]:
    """Translate a Codex `token_count.info` block to the harness usage shape.

    Codex JSONL convention: `last_token_usage.input_tokens` is the
    *total* prompt tokens (uncached + cached). Harness convention
    (mirrors Anthropic's): `input_tokens` is uncached only and
    `cache_read_tokens` is the cached subset. We translate by
    subtracting cached from total. `reasoning_output_tokens` rolls
    into output for billing-equivalent counts.
    """
    last = info.get("last_token_usage") if isinstance(info, Mapping) else None
    if not isinstance(last, Mapping):
        return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}

    def _i(name: str) -> int:
        v = last.get(name)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    total_in = _i("input_tokens")
    cached = _i("cached_input_tokens")
    out = _i("output_tokens")
    reasoning = _i("reasoning_output_tokens")
    return {
        "input": max(0, total_in - cached),
        "output": out + reasoning,
        "cache_read": cached,
        "cache_creation": 0,
    }


def _model_from_rollout(rollout_path: Path) -> str | None:
    """Last-resort model lookup for turns where `tc.model` was None.

    The rollout's `turn_context` events carry the model id that Codex
    actually used (e.g. "gpt-5.5"). We surface it so the turns ledger
    + context-bar window resolution find a real value when the per-role
    Codex default in team_config is unset.
    """
    try:
        latest_model: str | None = None
        with rollout_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s or '"turn_context"' not in s:
                    continue
                try:
                    obj = json.loads(s)
                except Exception:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "turn_context":
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                m = payload.get("model")
                if isinstance(m, str) and m.strip():
                    latest_model = m.strip()
        return latest_model
    except FileNotFoundError:
        return None
    except Exception:
        logger.exception("CodexRuntime: failed reading rollout for model %s", rollout_path)
        return None


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

            # Token usage extraction — see _read_codex_token_count_from_rollout
            # for why we go to the on-disk JSONL instead of thread.turns.
            # Fall back to the legacy thread-state walker only if the
            # rollout file isn't reachable; that keeps us forward-compat
            # with any future SDK that ships usage on Turn directly.
            usage_raw = None
            usage_from_rollout: dict[str, int] | None = None
            rollout_model: str | None = None
            try:
                read = thread.read(include_turns=True)
                thread_state = await _await_if_needed(read)
                rollout_path = _rollout_path_from_thread_state(thread_state)
                if rollout_path is not None:
                    rollout_info = _read_codex_token_count_from_rollout(rollout_path)
                    if rollout_info is not None:
                        usage_from_rollout = _codex_usage_from_rollout_info(rollout_info)
                    if not tc.model:
                        rollout_model = _model_from_rollout(rollout_path)
                if usage_from_rollout is None:
                    usage_raw = _extract_codex_usage_from_thread_state(
                        thread_state,
                        final_turn_id,
                    )
            except Exception:
                logger.exception(
                    "CodexRuntime: failed to read thread usage for slot=%s",
                    tc.agent_id,
                )
            usage = usage_from_rollout if usage_from_rollout is not None else _extract_usage_codex(usage_raw)
            effective_model = tc.model or rollout_model

            cost_basis = "plan_included" if method == "chatgpt" else "token_priced"
            if cost_basis == "plan_included":
                cost_usd = 0.0
            else:
                cost_usd = codex_cost_usd(
                    effective_model,
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
                model=effective_model,
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
        """Auto-compact trip-wire — Codex shape.

        Mirrors `ClaudeRuntime.maybe_auto_compact` but uses the native
        `client.compact_thread(thread_id)` path (via
        `run_manual_compact`) instead of running a `COMPACT_PROMPT`
        turn. Reads the same `HARNESS_AUTO_COMPACT_THRESHOLD` env
        (default 0.7) so behavior is symmetric across runtimes.

        Context-pressure signal comes from
        `_codex_session_context_estimate(thread_id)` — reads the latest
        `turns` row for the thread and reconstructs prompt+output
        tokens. That signal didn't exist when this runtime first
        shipped (hence the original "disabled" caveat in §A.5/§E.6);
        it does now, so the trip-wire matches Claude's.

        Returns False when:
          - this call is itself the compact turn (avoid recursion),
          - the env threshold is unset / 0 / unparseable,
          - there is no prior Codex thread,
          - the used/window ratio is below threshold,
          - or the compact attempt fails (logged + emitted; the user's
            original turn proceeds on the original thread).
        """
        if tc.compact_mode:
            return False
        threshold_env = os.environ.get("HARNESS_AUTO_COMPACT_THRESHOLD", "0.7")
        try:
            threshold = float(threshold_env)
        except ValueError:
            threshold = 0.7
        if not (0.0 < threshold < 1.0):
            return False

        from server.agents import (
            _codex_session_context_estimate,
            _context_window_for,
            _emit,
        )

        prior_thread = await _get_codex_thread_id(tc.agent_id)
        if not prior_thread:
            return False
        used = await _codex_session_context_estimate(prior_thread)
        ctx_max = _context_window_for(tc.model)
        if ctx_max <= 0 or used / ctx_max < threshold:
            return False

        await _emit(
            tc.agent_id,
            "auto_compact_triggered",
            used_tokens=used,
            context_window=ctx_max,
            ratio=round(used / ctx_max, 3),
            threshold=threshold,
            deferred_prompt=tc.prompt,
        )
        # Mark the throwaway TurnContext compact-mode for symmetry with
        # Claude's run_manual_compact (defensive against any future code
        # path that consults `tc.compact_mode` to avoid recursion). The
        # dispatcher creates a fresh TurnContext for the actual user
        # turn after maybe_auto_compact returns, so these mutations
        # don't bleed into the deferred prompt.
        tc.compact_mode = True
        tc.auto_compact = True
        tc.turn_ctx["compact_mode"] = True
        tc.turn_ctx["auto_compact"] = True
        try:
            await self.run_manual_compact(tc)
        except Exception:
            logger.exception(
                "codex auto-compact failed for %s; proceeding on original thread",
                tc.agent_id,
            )
            await _emit(tc.agent_id, "auto_compact_failed")
            return False
        # run_manual_compact swallows its own auth / import / SDK
        # errors with an `error` emit + status=error and a silent
        # return. Use got_result as the success signal to surface the
        # symmetric `auto_compact_failed` event in those cases too.
        if not tc.turn_ctx.get("got_result"):
            await _emit(tc.agent_id, "auto_compact_failed")
            return False
        return True

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

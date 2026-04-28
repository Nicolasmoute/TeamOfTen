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

The body in `run_turn` below is the next milestone — this file still
emits `human_attention` until the dispatcher carve-out lands.

See `Docs/CODEX_RUNTIME_SPEC.md` §E for the design + §I.1 for SDK
sourcing.
"""

from __future__ import annotations

import asyncio
import logging
import os
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
        except Exception:
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
        except Exception:
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
        except Exception:
            logger.exception(
                "CodexRuntime: resume_thread failed for slot=%s "
                "thread_id=%s — clearing and retrying with start_thread",
                agent_id, existing,
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
_ITEM_TYPE_TO_HARNESS: dict[str, tuple[str, str | None]] = {
    "userMessage": ("_skip", None),
    "agentMessage": ("text", None),
    "reasoning": ("thinking", None),
    "shell": ("tool_use", "Bash"),
    "apply_patch": ("tool_use", "Edit"),
    "web_search": ("tool_use", "WebSearch"),
}


def _step_item_payload(step: Any) -> dict[str, Any]:
    """Pull the raw `params.item` dict out of a ConversationStep.data.
    Falls back to an empty dict when the SDK changes shape so callers
    can still safely .get() into it."""
    data = getattr(step, "data", None) or {}
    if isinstance(data, dict):
        params = data.get("params") or {}
        if isinstance(params, dict):
            item = params.get("item")
            if isinstance(item, dict):
                return item
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
        if not text:
            return
        accumulated = turn_ctx.get("accumulated_text", "") + text
        turn_ctx["accumulated_text"] = accumulated
        # Final-answer steps mark the turn-end. The dispatcher uses
        # got_result to drive the post-result exception suppression and
        # to skip the auto-retry counter increment on success — same
        # discipline as the Claude path.
        phase = item_payload.get("phase")
        if phase == "final_answer":
            turn_ctx["got_result"] = True
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
        # through under `args` so the existing per-tool renderers (Bash
        # card, Edit diff, WebSearch card) can pick the keys they want
        # without the dispatcher pre-flattening.
        await _emit(
            agent_id,
            "tool_use",
            tool=tool_name,
            id=item_id,
            input=item_payload,
        )
        return


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

    async def run_turn(self, tc: TurnContext) -> None:
        if not is_enabled():
            await self._emit_disabled_attention(tc)
            return

        # Lazy import the rest of the harness machinery — agents.py is
        # the dispatcher and its imports cycle through us.
        from server.agents import _emit, _set_status

        # Auth resolution per §D.4 — ChatGPT session, then API-key
        # fallback, then human_attention abort. Stash the resolved
        # method + env overrides on turn_ctx so the SDK subprocess
        # body (when wired) can inject OPENAI_API_KEY without
        # re-querying the secrets store.
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

        try:
            _import_codex_sdk()
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

        # Once the SDK is in place, the body of run_turn:
        #   1. Resolve auth (chatgpt session via CODEX_HOME, fallback to
        #      OPENAI_API_KEY from secrets).
        #   2. Build mcp_servers config (coord proxy via stdio,
        #      external MCPs).
        #   3. Read agent_sessions.codex_thread_id for (slot, project_id);
        #      start_thread or resume_thread accordingly.
        #   4. Stream notifications and translate to harness events
        #      (see spec §E.3 for the table).
        #   5. On turn.completed: extract usage, compute cost via
        #      server.pricing.codex_cost_usd, insert turns row with
        #      runtime='codex' and the appropriate cost_basis.
        #   6. Persist codex_thread_id; clear on stale-thread errors
        #      with a fresh-thread retry.
        #
        # The body is left as a structured TODO until the SDK spike
        # confirms signatures. Surface a clear error so the dispatcher
        # records the failure rather than the agent appearing to hang.
        await _emit(
            tc.agent_id,
            "error",
            error=(
                "CodexRuntime.run_turn body is provisional pending the "
                "PR 1 SDK spike. Set HARNESS_CODEX_ENABLED=false to "
                "force runtime selection back to Claude."
            ),
        )
        await _set_status(tc.agent_id, "error")

    async def maybe_auto_compact(self, tc: TurnContext) -> bool:
        """Disabled in v1 — Codex app-server doesn't expose a usable
        context-pressure signal yet. Return False so the dispatcher
        proceeds straight to run_turn. See spec §A.5 / §E.6."""
        return False

    async def run_manual_compact(self, tc: TurnContext) -> None:
        """Provisional pending the PR 1 spike. The spec says we either
        call a native `thread.compact()` or fall back to a manual
        COMPACT_PROMPT turn. Until the SDK signature is confirmed, we
        emit human_attention so the user knows the action wasn't
        completed."""
        from server.agents import _emit
        await _emit(
            tc.agent_id,
            "human_attention",
            subject="Codex /compact not yet wired",
            body=(
                "CodexRuntime.run_manual_compact is provisional — the "
                "PR 1 SDK spike must confirm whether thread.compact() "
                "exists or we fall back to a COMPACT_PROMPT turn. See "
                "Docs/CODEX_RUNTIME_SPEC.md §E.6."
            ),
            urgency="normal",
        )

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

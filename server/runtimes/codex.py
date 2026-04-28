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


# Module-level cache of `AsyncCodex` instances per slot. The harness
# already serializes turns per slot via `_SPAWN_LOCK` (see agents.py),
# satisfying the SDK's "one active turn consumer per client" rule.
# Closed and re-opened on auth-error.
_codex_clients: dict[str, Any] = {}


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

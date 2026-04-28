"""Runtime protocol — the contract every per-agent runtime implements.

See `Docs/CODEX_RUNTIME_SPEC.md` §A for the design rationale. The
dispatcher in `server.agents.run_agent` owns runtime-agnostic work
(pause check, spawn lock, cost caps, system prompt assembly, post-
result exception suppression, auto-retry counter). Everything below
is owned by the runtime.

`HarnessEvent` is intentionally not a struct — runtimes call the
existing `_emit(agent_id, type, **payload)` bus vocabulary already
documented in `server/agents.py`. CodexRuntime maps Codex notifications
onto the same vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class TurnContext:
    """Shared state for one model turn.

    The dispatcher constructs this and passes it to the runtime by
    reference; the runtime mutates `turn_ctx` (the inner dict) so the
    dispatcher can inspect `got_result` / `accumulated_text` after the
    turn returns.

    The dispatcher supplies `prior_session` after reading the
    runtime-specific session column (Claude `session_id`, Codex
    `codex_thread_id`). Runtimes may still re-check or prepare their
    native handle before the turn starts when a stored id can go stale.
    """

    agent_id: str
    project_id: str
    prompt: str
    system_prompt: str
    workspace_cwd: str
    allowed_tools: list[str]
    external_mcp_servers: dict[str, Any]
    model: str | None = None
    plan_mode: bool = False
    effort: int | None = None
    compact_mode: bool = False
    auto_compact: bool = False
    # Prior-session continuation id, runtime-shaped. ClaudeRuntime
    # reads this as the SDK `resume` kwarg and as the stale-session
    # retry detector. CodexRuntime receives the stored codex_thread_id
    # here, then may prepare/open the thread before `agent_started`.
    prior_session: str | None = None
    turn_ctx: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AgentRuntime(Protocol):
    """Contract for per-agent runtime adapters.

    The dispatcher does not branch on runtime name beyond
    `get_runtime(name)` and the optional pre-start preparation hook.
    """

    name: str

    async def prepare_turn_start(self, tc: TurnContext) -> bool:
        """Return the exact `agent_started.resumed_session` value.

        Claude returns `bool(tc.prior_session)`. Codex opens/resumes
        its thread here so stale `codex_thread_id` values can emit
        `session_resume_failed` and downgrade the start event to fresh.
        """
        ...

    async def run_turn(self, tc: TurnContext) -> None:
        """Execute one model turn.

        Emits harness events via the in-process bus
        (`server.events.bus.publish`) using the shared event vocabulary
        (`tool_use`, `tool_result`, `text`, `thinking`, `result`,
        `error`, `agent_stopped`, `context_applied`,
        `auto_compact_triggered`, `session_compacted`,
        `session_resume_failed`, `cost_capped`, `agent_cancelled`,
        `paused`, `spawn_rejected`).

        Mutates `tc.turn_ctx` in place — at minimum sets
        `got_result: bool` once a `ResultMessage`-equivalent has been
        observed so the dispatcher's post-result exception suppression
        can do its job.
        """
        ...

    async def maybe_auto_compact(self, tc: TurnContext) -> bool:
        """Auto-compact trip-wire.

        Return True if a compact turn was actually run (so the
        dispatcher proceeds to run the user's original prompt on the
        now-fresh session). Return False to skip compaction.

        ClaudeRuntime preserves the existing JSONL-probe + threshold
        check. CodexRuntime returns False in v1 — auto-compact is
        disabled until app-server exposes a usable context-pressure
        signal.
        """
        ...

    async def run_manual_compact(self, tc: TurnContext) -> None:
        """Execute a manual `/compact` request.

        ClaudeRuntime runs a `COMPACT_PROMPT` turn. CodexRuntime uses
        the native app-server compact call, stores the returned summary
        as continuity, and clears `codex_thread_id`.
        """
        ...

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

    Prior-session state is NOT on TurnContext. Each runtime knows
    which DB column to read (Claude → `agent_sessions.session_id`;
    Codex → `agent_sessions.codex_thread_id`). Putting it on the
    dispatcher would force a tagged-union or a clear-on-runtime-change
    rule; both fail when a slot switches runtime and switches back.
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
    # retry detector. CodexRuntime would read its codex_thread_id
    # equivalent. Set by the dispatcher before calling run_turn so
    # the runtime doesn't re-query the DB.
    prior_session: str | None = None
    turn_ctx: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class AgentRuntime(Protocol):
    """Contract for per-agent runtime adapters.

    Both runtimes implement all three methods on day one (Codex stubs
    in PR 5). The dispatcher does not branch on runtime name beyond
    `get_runtime(name)`.
    """

    name: str

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

        ClaudeRuntime runs a `COMPACT_PROMPT` turn. CodexRuntime is
        provisional pending the PR 1 SDK spike — may use a native
        compact call or fall back to a manual COMPACT_PROMPT turn
        against Codex.
        """
        ...

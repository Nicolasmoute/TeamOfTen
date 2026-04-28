"""Per-agent runtime adapters.

The dispatcher in `server.agents.run_agent` is runtime-agnostic at the
edges (pause check, spawn lock, cost caps, agent_started emit, system
prompt assembly, post-result exception suppression, auto-retry counter).
The runtime-specific work — model-side I/O, MCP wiring, compact —
lives behind the `AgentRuntime` protocol in `base.py`.

PR 2 ships the protocol and `ClaudeRuntime`; `CodexRuntime` lands in
PR 5 (see `Docs/CODEX_RUNTIME_SPEC.md`).
"""

from server.runtimes.base import AgentRuntime, TurnContext
from server.runtimes.claude import ClaudeRuntime
from server.runtimes.codex import CodexRuntime, is_enabled as is_codex_enabled

__all__ = [
    "AgentRuntime",
    "TurnContext",
    "ClaudeRuntime",
    "CodexRuntime",
    "get_runtime",
    "is_codex_enabled",
]


def get_runtime(name: str) -> AgentRuntime:
    """Resolve a runtime name to its singleton instance.

    Names: 'claude' (default), 'codex' (PR 5+).
    Raises ValueError on unknown names so a stale `runtime_override`
    column value surfaces loudly instead of silently picking the
    wrong runtime.
    """
    if name == "claude":
        return _CLAUDE_SINGLETON
    if name == "codex":
        return _CODEX_SINGLETON
    raise ValueError(f"unknown runtime: {name!r}")


_CLAUDE_SINGLETON: AgentRuntime = ClaudeRuntime()
_CODEX_SINGLETON: AgentRuntime = CodexRuntime()

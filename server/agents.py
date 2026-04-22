from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from server.events import bus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _emit(agent_id: str, event_type: str, **payload: Any) -> None:
    await bus.publish(
        {"ts": _now(), "agent_id": agent_id, "type": event_type, **payload}
    )


async def run_agent(agent_id: str, prompt: str) -> None:
    """Spawn one SDK query, stream its messages onto the event bus.

    M1: one-shot, default model, no MCP tools, no persistence. The SDK
    shells out to the `claude` CLI, which must already be logged in on
    this host via `/login` (device-code flow).
    """
    await _emit(agent_id, "agent_started", prompt=prompt)

    options = ClaudeAgentOptions(
        system_prompt=(
            f"You are {agent_id}, a test worker in the TeamOfTen harness. "
            f"Be terse."
        ),
        cwd="/workspaces/default",
        max_turns=10,
    )

    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        await _emit(agent_id, "text", content=block.text)
                    elif isinstance(block, ToolUseBlock):
                        await _emit(
                            agent_id,
                            "tool_use",
                            name=block.name,
                            input=block.input,
                        )
            elif isinstance(msg, ResultMessage):
                await _emit(
                    agent_id,
                    "result",
                    duration_ms=getattr(msg, "duration_ms", None),
                    cost_usd=getattr(msg, "total_cost_usd", None),
                    is_error=msg.is_error,
                )
    except Exception as e:  # broad catch is intentional for M1 surface
        await _emit(agent_id, "error", error=f"{type(e).__name__}: {e}")

    await _emit(agent_id, "agent_stopped")

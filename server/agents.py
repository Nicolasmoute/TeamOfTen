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
from server.tools import ALLOWED_COORD_TOOLS, build_coord_server


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _emit(agent_id: str, event_type: str, **payload: Any) -> None:
    await bus.publish(
        {"ts": _now(), "agent_id": agent_id, "type": event_type, **payload}
    )


def _system_prompt_for(agent_id: str) -> str:
    if agent_id == "coach":
        return (
            "You are Coach, the captain of the TeamOfTen team. Your job is to "
            "decompose human goals into tasks, assign them to Players (slots "
            "p1..p10), and orchestrate progress. You have these tools:\n"
            "  - coord_list_tasks: see the team board\n"
            "  - coord_create_task: add top-level tasks (Coach-only privilege)\n"
            "Rule: you never write code; you delegate. Be terse."
        )
    return (
        f"You are Player {agent_id} on the TeamOfTen team. Your name and role "
        f"will be assigned by Coach; for now work with your slot id. You have "
        f"these tools:\n"
        f"  - coord_list_tasks: see the team board\n"
        f"  - coord_create_task: create SUBTASKS of tasks you own (not top-level; "
        f"only Coach does top-level)\n"
        f"Rule: you execute and report. You do not assign work to other Players. "
        f"Be terse."
    )


async def run_agent(agent_id: str, prompt: str) -> None:
    """Spawn one SDK query for the given slot and stream its events.

    The SDK shells out to the `claude` CLI, which must already be logged in
    on this host via `/login` (device-code flow).
    """
    await _emit(agent_id, "agent_started", prompt=prompt)

    coord_server = build_coord_server(agent_id)

    options = ClaudeAgentOptions(
        system_prompt=_system_prompt_for(agent_id),
        cwd=f"/workspaces/{agent_id}",
        max_turns=10,
        mcp_servers={"coord": coord_server},
        allowed_tools=ALLOWED_COORD_TOOLS,
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
    except Exception as e:  # broad catch is intentional for M2a surface
        await _emit(agent_id, "error", error=f"{type(e).__name__}: {e}")

    await _emit(agent_id, "agent_stopped")

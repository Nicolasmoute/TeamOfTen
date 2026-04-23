from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from server.db import configured_conn
from server.events import bus
from server.tools import ALLOWED_COACH_TOOLS, ALLOWED_PLAYER_TOOLS, build_coord_server
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


AGENT_DAILY_CAP_USD = float(os.environ.get("HARNESS_AGENT_DAILY_CAP", "5.0"))
TEAM_DAILY_CAP_USD = float(os.environ.get("HARNESS_TEAM_DAILY_CAP", "20.0"))


def _today_utc_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


async def _today_spend(agent_id: str | None = None) -> float:
    """Sum cost_usd from 'result' events emitted today (UTC). Pass
    agent_id to scope to one slot, or None for the whole team."""
    start_ts = _today_utc_start_iso()
    where = "WHERE type = 'result' AND ts >= ?"
    params: list[Any] = [start_ts]
    if agent_id:
        where += " AND agent_id = ?"
        params.append(agent_id)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COALESCE("
            "  SUM(CAST(json_extract(payload, '$.cost_usd') AS REAL)), 0"
            f") AS total FROM events {where}",
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


def _system_prompt_for(agent_id: str) -> str:
    if agent_id == "coach":
        return (
            "You are Coach, the captain of the TeamOfTen team. Your job is to "
            "decompose human goals into tasks, assign them to Players (slots "
            "p1..p10), and orchestrate progress.\n\n"
            "Coordination tools:\n"
            "  - coord_list_tasks(status?, owner?): see the team board\n"
            "  - coord_create_task(title, description?, priority?): add top-level tasks\n"
            "  - coord_update_task(task_id, status, note?): you can cancel any task\n"
            "  - coord_send_message(to, body, subject?, priority?): message a Player "
            "or 'broadcast' to the whole team\n"
            "  - coord_read_inbox(): read messages addressed to you or the team\n"
            "  - coord_list_memory / coord_read_memory / coord_update_memory: "
            "shared scratchpad for the team — drop design decisions, conventions, "
            "gotchas here so Players don't have to ask twice\n"
            "\n"
            "Rules:\n"
            "  - You never write code; you delegate.\n"
            "  - Only you can create top-level tasks — Players can only subtask.\n"
            "  - You are the sole source of assignments; Players claim them.\n"
            "  - Start every turn by reading your inbox for new human goals.\n"
            "  - Be terse."
        )
    return (
        f"You are Player {agent_id} on the TeamOfTen team. Your name and role "
        f"will be assigned by Coach; for now work with your slot id.\n\n"
        f"Coordination tools:\n"
        f"  - coord_list_tasks(status?, owner?): see the team board\n"
        f"  - coord_claim_task(task_id): claim an open task (one at a time)\n"
        f"  - coord_update_task(task_id, status, note?): report progress\n"
        f"      valid next states: in_progress, blocked, done, cancelled\n"
        f"  - coord_create_task(title, ...): create SUBTASKS of tasks you own\n"
        f"      (you cannot create top-level tasks — that's Coach's job)\n"
        f"  - coord_send_message(to, body, ...): message Coach or a peer for info\n"
        f"      (you CANNOT use this to assign work — only Coach assigns)\n"
        f"  - coord_read_inbox(): read messages addressed to you or the team\n"
        f"  - coord_list_memory / coord_read_memory / coord_update_memory:\n"
        f"      shared scratchpad. Read it to see what other agents found; "
        f"write to it when you learn something worth preserving.\n"
        f"  - coord_commit_push(message, push?): when you have code changes "
        f"to ship, use this instead of driving git through Bash — it does "
        f"git add -A + commit + push and emits a commit_pushed event.\n"
        f"\n"
        f"Rules:\n"
        f"  - You execute and report. You do not assign work to other Players.\n"
        f"  - Start every turn by reading your inbox for new orders from Coach.\n"
        f"  - Before starting complex work, check memory for prior findings.\n"
        f"  - When you finish, mark the task done — that frees you for the next.\n"
        f"  - If blocked, mark blocked with a note explaining why.\n"
        f"  - Be terse."
    )


async def run_agent(agent_id: str, prompt: str) -> None:
    """Spawn one SDK query for the given slot and stream its events."""
    # Enforce daily cost caps BEFORE emitting agent_started — if the
    # caller is over budget we want the rejection visible in the
    # timeline and no SDK work done.
    allowed, reason = await _check_cost_caps(agent_id)
    if not allowed:
        await _emit(agent_id, "cost_capped", reason=reason, prompt=prompt)
        logger.warning("cost cap blocked spawn: %s", reason)
        return

    await _emit(agent_id, "agent_started", prompt=prompt)
    await _set_status(agent_id, "working")

    coord_server = build_coord_server(agent_id)
    allowed = ALLOWED_COACH_TOOLS if agent_id == "coach" else ALLOWED_PLAYER_TOOLS

    options = ClaudeAgentOptions(
        system_prompt=_system_prompt_for(agent_id),
        cwd=str(workspace_dir(agent_id)),
        max_turns=10,
        mcp_servers={"coord": coord_server},
        allowed_tools=allowed,
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
                            id=block.id,
                            name=block.name,
                            input=block.input,
                        )
            elif isinstance(msg, UserMessage):
                # Carries tool results; we surface them so the UI can pair
                # each tool_use with its output.
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        content = _stringify_tool_result(block.content)
                        await _emit(
                            agent_id,
                            "tool_result",
                            tool_use_id=block.tool_use_id,
                            content=content,
                            is_error=bool(getattr(block, "is_error", False)),
                        )
            elif isinstance(msg, ResultMessage):
                cost = getattr(msg, "total_cost_usd", None)
                await _emit(
                    agent_id,
                    "result",
                    duration_ms=getattr(msg, "duration_ms", None),
                    cost_usd=cost,
                    is_error=msg.is_error,
                )
                await _add_cost(agent_id, cost)
    except Exception as e:
        await _emit(agent_id, "error", error=f"{type(e).__name__}: {e}")
        await _set_status(agent_id, "error")
    else:
        await _set_status(agent_id, "idle")

    await _emit(agent_id, "agent_stopped")

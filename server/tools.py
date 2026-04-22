from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from server.db import configured_conn
from server.events import bus


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_task_id() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"t-{today}-{uuid.uuid4().hex[:8]}"


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"ERROR: {text}"}],
        "isError": True,
    }


def build_coord_server(caller_id: str) -> Any:
    """Build an in-process MCP server whose tools know which agent is calling.

    Each SDK query gets its own server so hierarchy enforcement (Coach can
    give orders, Players cannot) operates without the LLM needing to pass
    its own identity as a param.
    """

    caller_is_coach = caller_id == "coach"

    @tool(
        "coord_list_tasks",
        (
            "List tasks on the team board. Optional filters:\n"
            "- status: one of 'open', 'claimed', 'in_progress', 'blocked', 'done', 'cancelled'\n"
            "- owner: agent id ('coach', 'p1'..'p10'), or 'null' for unassigned\n"
            "Returns up to 100 most recent tasks."
        ),
        {"status": str, "owner": str},
    )
    async def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
        status = (args.get("status") or "").strip() or None
        owner_arg = args.get("owner")
        owner = owner_arg.strip() if isinstance(owner_arg, str) else None

        where_parts: list[str] = []
        params: list[Any] = []
        if status:
            where_parts.append("status = ?")
            params.append(status)
        if owner is not None and owner != "":
            if owner.lower() in ("null", "none", "unassigned"):
                where_parts.append("owner IS NULL")
            else:
                where_parts.append("owner = ?")
                params.append(owner)
        clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

        c = await configured_conn()
        try:
            cur = await c.execute(
                f"SELECT id, title, status, owner, created_by, parent_id, "
                f"priority, created_at FROM tasks{clause} "
                f"ORDER BY created_at DESC LIMIT 100",
                params,
            )
            rows = await cur.fetchall()
        finally:
            await c.close()

        if not rows:
            return _ok("(no tasks match)")
        lines = []
        for r in rows:
            d = dict(r)
            parent = f" ↳{d['parent_id']}" if d["parent_id"] else ""
            lines.append(
                f"{d['id']}  [{d['status']}]  owner={d['owner'] or '-'}  "
                f"pri={d['priority']}  {d['title']}{parent}"
            )
        return _ok("\n".join(lines))

    @tool(
        "coord_create_task",
        (
            "Create a task on the team board.\n"
            "Hierarchy rule: only Coach can create top-level tasks. "
            "Players can only create SUBTASKS of tasks they own — pass the "
            "parent_id explicitly (must match a task you own), or leave it "
            "blank to auto-nest under your current task.\n"
            "Params:\n"
            "- title: short summary (required)\n"
            "- description: longer explanation (optional)\n"
            "- parent_id: parent task id (optional; Players: required unless you have a current task)\n"
            "- priority: 'low', 'normal', 'high', 'urgent' (default 'normal')"
        ),
        {"title": str, "description": str, "parent_id": str, "priority": str},
    )
    async def create_task(args: dict[str, Any]) -> dict[str, Any]:
        title = (args.get("title") or "").strip()
        if not title:
            return _err("title is required")
        description = args.get("description") or ""
        parent_id_arg = args.get("parent_id")
        parent_id = parent_id_arg.strip() if isinstance(parent_id_arg, str) and parent_id_arg.strip() else None
        priority = (args.get("priority") or "normal").strip().lower()
        if priority not in ("low", "normal", "high", "urgent"):
            return _err(
                f"invalid priority '{priority}' "
                "(must be low, normal, high, or urgent)"
            )

        c = await configured_conn()
        try:
            if not caller_is_coach:
                # Player: enforce hierarchy
                cur = await c.execute(
                    "SELECT current_task_id FROM agents WHERE id = ?",
                    (caller_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return _err(f"caller '{caller_id}' not in agents table")
                current_task = dict(row)["current_task_id"]
                if parent_id is None:
                    if current_task is None:
                        return _err(
                            "Players can only create subtasks of a task they own. "
                            "You have no active task — ask Coach to assign one, or "
                            "pass parent_id explicitly to a task you own."
                        )
                    parent_id = current_task
                else:
                    cur = await c.execute(
                        "SELECT owner FROM tasks WHERE id = ?", (parent_id,)
                    )
                    prow = await cur.fetchone()
                    if prow is None:
                        return _err(f"parent_id '{parent_id}' not found")
                    parent_owner = dict(prow)["owner"]
                    if parent_owner != caller_id:
                        return _err(
                            f"Players can only subtask their own tasks. "
                            f"Task {parent_id} is owned by "
                            f"{parent_owner or 'nobody'}. To suggest new "
                            f"top-level work, message Coach."
                        )

            task_id = _new_task_id()
            await c.execute(
                "INSERT INTO tasks (id, title, description, parent_id, "
                "priority, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, title, description, parent_id, priority, caller_id),
            )
            await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_created",
                "task_id": task_id,
                "title": title,
                "parent_id": parent_id,
                "priority": priority,
            }
        )
        return _ok(
            f"Created task {task_id}"
            + (f" (subtask of {parent_id})" if parent_id else " (top-level)")
            + f", priority={priority}"
        )

    return create_sdk_mcp_server(
        name="coord",
        version="0.2.0",
        tools=[list_tasks, create_task],
    )


ALLOWED_COORD_TOOLS = [
    "mcp__coord__coord_list_tasks",
    "mcp__coord__coord_create_task",
]

# Read-only tools: see the world, touch nothing. Coach uses these + coord.
STANDARD_READ_TOOLS = ["Read", "Grep", "Glob", "ToolSearch"]

# Mutating tools: Players get these too so they can actually do work.
STANDARD_WRITE_TOOLS = ["Write", "Edit", "Bash"]

# Coach = read + coord. Matches the spec rule "you never write code, you
# delegate" — enforced structurally (not just by prompt).
ALLOWED_COACH_TOOLS = STANDARD_READ_TOOLS + ALLOWED_COORD_TOOLS

# Players get the full standard set + coord.
ALLOWED_PLAYER_TOOLS = (
    STANDARD_READ_TOOLS + STANDARD_WRITE_TOOLS + ALLOWED_COORD_TOOLS
)

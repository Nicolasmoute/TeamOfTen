from __future__ import annotations

import re
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


# Task state machine. Reject transitions not listed here.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "open":        {"claimed", "cancelled"},
    "claimed":     {"in_progress", "blocked", "done", "cancelled"},
    "in_progress": {"blocked", "done", "cancelled"},
    "blocked":     {"in_progress", "cancelled"},
    "done":        set(),
    "cancelled":   set(),
}


def _valid_transition(old: str, new: str) -> bool:
    return new in VALID_TRANSITIONS.get(old, set())


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

    @tool(
        "coord_claim_task",
        (
            "Claim an open task — sets you as its owner and moves it to "
            "status=claimed. Only Players can claim (Coach delegates, never "
            "executes). Fails if: task is not status=open, you're Coach, or "
            "you already own another task (finish or cancel it first)."
        ),
        {"task_id": str},
    )
    async def claim_task(args: dict[str, Any]) -> dict[str, Any]:
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return _err("task_id is required")
        if caller_is_coach:
            return _err(
                "Coach delegates; only Players claim tasks. If you want to "
                "assign this task to a Player, message them via "
                "coord_send_message (that tool lands in M2c)."
            )

        c = await configured_conn()
        try:
            # One-task-at-a-time for Players — enforces focus and keeps
            # current_task_id well-defined for subtask nesting.
            cur = await c.execute(
                "SELECT current_task_id FROM agents WHERE id = ?",
                (caller_id,),
            )
            row = await cur.fetchone()
            if row and dict(row)["current_task_id"]:
                return _err(
                    f"you already own task {dict(row)['current_task_id']}; "
                    f"complete or cancel it first."
                )

            # Atomic claim — race-safe via status='open' guard.
            cur = await c.execute(
                "UPDATE tasks SET owner = ?, status = 'claimed', "
                "claimed_at = ? WHERE id = ? AND status = 'open' "
                "RETURNING id",
                (caller_id, _now_iso(), task_id),
            )
            updated = await cur.fetchone()
            if not updated:
                cur = await c.execute(
                    "SELECT status, owner FROM tasks WHERE id = ?",
                    (task_id,),
                )
                current = await cur.fetchone()
                if not current:
                    return _err(f"task {task_id} not found")
                d = dict(current)
                return _err(
                    f"task {task_id} is not open (status={d['status']}, "
                    f"owner={d['owner'] or '-'})"
                )

            await c.execute(
                "UPDATE agents SET current_task_id = ? WHERE id = ?",
                (task_id, caller_id),
            )
            await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_claimed",
                "task_id": task_id,
            }
        )
        return _ok(f"claimed {task_id}")

    @tool(
        "coord_update_task",
        (
            "Update task status. Valid transitions:\n"
            "  open → claimed, cancelled\n"
            "  claimed → in_progress, blocked, done, cancelled\n"
            "  in_progress → blocked, done, cancelled\n"
            "  blocked → in_progress, cancelled\n"
            "  done/cancelled: terminal\n"
            "Only the current owner can update the task; Coach can also "
            "cancel any task. Players: when you mark a task done or "
            "cancelled, your current_task_id is cleared so you can claim "
            "the next one. Optional 'note' is logged in the event stream."
        ),
        {"task_id": str, "status": str, "note": str},
    )
    async def update_task(args: dict[str, Any]) -> dict[str, Any]:
        task_id = (args.get("task_id") or "").strip()
        new_status = (args.get("status") or "").strip().lower()
        note = args.get("note") or ""
        if not task_id:
            return _err("task_id is required")
        if new_status not in ("claimed", "in_progress", "blocked", "done", "cancelled"):
            return _err(
                f"invalid status '{new_status}' (must be claimed, "
                "in_progress, blocked, done, or cancelled)"
            )

        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT owner, status FROM tasks WHERE id = ?",
                (task_id,),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            d = dict(row)
            current_owner: str | None = d["owner"]
            old_status: str = d["status"]

            # Permission check.
            if current_owner is None:
                # task has no owner yet (still 'open'). Only Coach (or
                # 'cancelled' moves by anyone) — actually still only Coach
                # can touch unowned tasks.
                if not caller_is_coach:
                    return _err(
                        f"task {task_id} has no owner; only Coach can "
                        f"change an open task's status."
                    )
            elif current_owner != caller_id:
                # task owned by someone else. Only Coach can cancel.
                if not (caller_is_coach and new_status == "cancelled"):
                    return _err(
                        f"only the task's owner ({current_owner}) can "
                        f"update it. Coach can additionally cancel any task."
                    )

            if not _valid_transition(old_status, new_status):
                return _err(
                    f"invalid transition: {old_status} → {new_status}"
                )

            now = _now_iso()
            if new_status in ("done", "cancelled"):
                await c.execute(
                    "UPDATE tasks SET status = ?, completed_at = ? "
                    "WHERE id = ?",
                    (new_status, now, task_id),
                )
                # Free up the player who was on this task.
                if current_owner is not None:
                    await c.execute(
                        "UPDATE agents SET current_task_id = NULL "
                        "WHERE id = ? AND current_task_id = ?",
                        (current_owner, task_id),
                    )
            else:
                await c.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?",
                    (new_status, task_id),
                )
            await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_updated",
                "task_id": task_id,
                "old_status": old_status,
                "new_status": new_status,
                "note": note,
            }
        )
        suffix = f" — {note}" if note else ""
        return _ok(f"updated {task_id}: {old_status} → {new_status}{suffix}")

    @tool(
        "coord_send_message",
        (
            "Send a message to another agent or to the whole team.\n"
            "Params:\n"
            "- to: recipient slot id ('coach' or 'p1'..'p10'), or 'broadcast' "
            "to reach the whole team.\n"
            "- body: message text (required, max 5000 chars)\n"
            "- subject: optional short subject line (max 200 chars)\n"
            "- priority: 'normal' (default) or 'interrupt' for urgent items\n"
            "Messaging is free-form — anyone can message anyone for info "
            "sharing. Assigning work still only happens through the task "
            "board (Coach creates + assigns, Players claim)."
        ),
        {"to": str, "body": str, "subject": str, "priority": str},
    )
    async def send_message(args: dict[str, Any]) -> dict[str, Any]:
        to = (args.get("to") or "").strip().lower()
        body = args.get("body") or ""
        subject = (args.get("subject") or "").strip() or None
        priority = (args.get("priority") or "normal").strip().lower()

        if not to:
            return _err("'to' is required (agent id or 'broadcast')")
        if to not in VALID_RECIPIENTS:
            return _err(
                f"invalid recipient '{to}' — must be 'coach', 'p1'..'p10', "
                "or 'broadcast'"
            )
        if to == caller_id:
            return _err("you can't send a message to yourself")
        if not body.strip():
            return _err("body cannot be empty")
        if len(body) > 5000:
            return _err(f"body too long ({len(body)} chars, max 5000)")
        if subject and len(subject) > 200:
            return _err(f"subject too long ({len(subject)} chars, max 200)")
        if priority not in ("normal", "interrupt"):
            return _err(
                f"invalid priority '{priority}' (must be 'normal' or 'interrupt')"
            )

        c = await configured_conn()
        try:
            cur = await c.execute(
                "INSERT INTO messages (from_id, to_id, subject, body, priority) "
                "VALUES (?, ?, ?, ?, ?) RETURNING id",
                (caller_id, to, subject, body, priority),
            )
            row = await cur.fetchone()
            msg_id = dict(row)["id"] if row else None
            await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "message_sent",
                "message_id": msg_id,
                "to": to,
                "subject": subject,
                "body_preview": body[:120],
                "priority": priority,
            }
        )
        preview = body.strip().replace("\n", " ")[:60]
        return _ok(
            f"sent to {to}"
            + (f" (subject: {subject})" if subject else "")
            + f": \"{preview}\""
        )

    @tool(
        "coord_read_inbox",
        (
            "Read and drain your unread messages. Returns every message "
            "targeted at you (direct or broadcast) that you haven't yet "
            "seen, in chronological order, then marks them as read FOR "
            "YOU (broadcasts stay unread for other recipients)."
        ),
        {},
    )
    async def read_inbox(args: dict[str, Any]) -> dict[str, Any]:
        c = await configured_conn()
        try:
            # Per-recipient unread via NOT EXISTS on message_reads — avoids
            # the broadcast bug where the first reader hides the message
            # from everyone else.
            cur = await c.execute(
                "SELECT m.id, m.from_id, m.to_id, m.subject, m.body, "
                "       m.sent_at, m.priority "
                "FROM messages m "
                "WHERE (m.to_id = ? OR m.to_id = 'broadcast') "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM message_reads r "
                "    WHERE r.message_id = m.id AND r.agent_id = ?"
                "  ) "
                "ORDER BY m.sent_at ASC",
                (caller_id, caller_id),
            )
            rows = await cur.fetchall()
            if not rows:
                return _ok("(no unread messages)")

            # Mark each message read by this caller only.
            await c.executemany(
                "INSERT OR IGNORE INTO message_reads (message_id, agent_id) "
                "VALUES (?, ?)",
                [(dict(r)["id"], caller_id) for r in rows],
            )
            await c.commit()
        finally:
            await c.close()

        lines = [
            f"{len(rows)} unread message{'s' if len(rows) != 1 else ''}:"
        ]
        for i, r in enumerate(rows, 1):
            d = dict(r)
            broadcast_note = " [broadcast]" if d["to_id"] == "broadcast" else ""
            priority_note = " [INTERRUPT]" if d["priority"] == "interrupt" else ""
            subject_line = f"\n  Subject: {d['subject']}" if d["subject"] else ""
            lines.append(
                f"\n[{i}] from {d['from_id']}{broadcast_note}{priority_note} "
                f"({d['sent_at']}):{subject_line}\n  {d['body']}"
            )
        return _ok("\n".join(lines))

    @tool(
        "coord_list_memory",
        (
            "List all topics in the shared memory scratchpad, newest first. "
            "Returns topic name, version, last_updated, last_updated_by, and "
            "content length so you can see what's there without reading it."
        ),
        {},
    )
    async def list_memory(args: dict[str, Any]) -> dict[str, Any]:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT topic, version, last_updated, last_updated_by, "
                "length(content) AS size FROM memory_docs "
                "ORDER BY last_updated DESC LIMIT 200"
            )
            rows = await cur.fetchall()
        finally:
            await c.close()
        if not rows:
            return _ok("(memory is empty)")
        lines = [f"{len(rows)} memory topic{'s' if len(rows) != 1 else ''}:"]
        for r in rows:
            d = dict(r)
            lines.append(
                f"  {d['topic']}  (v{d['version']}, {d['size']} chars, "
                f"updated {d['last_updated']} by {d['last_updated_by']})"
            )
        return _ok("\n".join(lines))

    @tool(
        "coord_read_memory",
        (
            "Read a shared memory doc by topic. Returns the current content "
            "plus metadata (version, last_updated, last_updated_by). "
            "Fails if the topic doesn't exist."
        ),
        {"topic": str},
    )
    async def read_memory(args: dict[str, Any]) -> dict[str, Any]:
        topic = (args.get("topic") or "").strip().lower()
        if not topic:
            return _err("topic is required")
        if not MEMORY_TOPIC_RE.match(topic):
            return _err(
                f"invalid topic '{topic}' — must be lowercase alphanumeric "
                "with dashes, 1-64 chars, starting with a letter or digit"
            )
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT content, version, last_updated, last_updated_by "
                "FROM memory_docs WHERE topic = ?",
                (topic,),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
        if not row:
            return _err(
                f"no memory topic '{topic}'. Use coord_list_memory to see "
                "what's available."
            )
        d = dict(row)
        return _ok(
            f"[{topic}  v{d['version']}  updated {d['last_updated']} "
            f"by {d['last_updated_by']}]\n\n{d['content']}"
        )

    @tool(
        "coord_update_memory",
        (
            "Write or overwrite a shared memory doc. Any agent can update "
            "any topic — it's a commons. Last write wins; the event log "
            "preserves the history of all updates. Use this to drop notes "
            "for other agents (findings, design decisions, gotchas, "
            "conventions). Topic is the filename-style key; content is the "
            "full markdown body."
        ),
        {"topic": str, "content": str},
    )
    async def update_memory(args: dict[str, Any]) -> dict[str, Any]:
        topic = (args.get("topic") or "").strip().lower()
        content = args.get("content") or ""
        if not topic:
            return _err("topic is required")
        if not MEMORY_TOPIC_RE.match(topic):
            return _err(
                f"invalid topic '{topic}' — must be lowercase alphanumeric "
                "with dashes, 1-64 chars, starting with a letter or digit"
            )
        if not content.strip():
            return _err("content cannot be empty (use a delete tool later if needed)")
        if len(content) > MEMORY_CONTENT_MAX:
            return _err(
                f"content too long ({len(content)} chars, max {MEMORY_CONTENT_MAX})"
            )
        now = _now_iso()
        c = await configured_conn()
        try:
            # UPSERT: insert with version=1, or increment on conflict.
            cur = await c.execute(
                "INSERT INTO memory_docs "
                "(topic, content, last_updated, last_updated_by, version) "
                "VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(topic) DO UPDATE SET "
                "  content = excluded.content, "
                "  last_updated = excluded.last_updated, "
                "  last_updated_by = excluded.last_updated_by, "
                "  version = memory_docs.version + 1 "
                "RETURNING version",
                (topic, content, now, caller_id),
            )
            row = await cur.fetchone()
            version = dict(row)["version"] if row else 1
            await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": now,
                "agent_id": caller_id,
                "type": "memory_updated",
                "topic": topic,
                "version": version,
                "size": len(content),
            }
        )
        return _ok(
            f"saved memory[{topic}] v{version} ({len(content)} chars)"
        )

    return create_sdk_mcp_server(
        name="coord",
        version="0.5.0",
        tools=[
            list_tasks,
            create_task,
            claim_task,
            update_task,
            send_message,
            read_inbox,
            list_memory,
            read_memory,
            update_memory,
        ],
    )


ALLOWED_COORD_TOOLS = [
    "mcp__coord__coord_list_tasks",
    "mcp__coord__coord_create_task",
    "mcp__coord__coord_claim_task",
    "mcp__coord__coord_update_task",
    "mcp__coord__coord_send_message",
    "mcp__coord__coord_read_inbox",
    "mcp__coord__coord_list_memory",
    "mcp__coord__coord_read_memory",
    "mcp__coord__coord_update_memory",
]

MEMORY_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")
MEMORY_CONTENT_MAX = 20_000

VALID_RECIPIENTS: set[str] = (
    {"coach", "broadcast"} | {f"p{i}" for i in range(1, 11)}
)

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

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from server import context as ctxmod
from server import knowledge as knowmod
from server.db import configured_conn
from server.events import bus
from server.kdrive import kdrive
from server.workspaces import project_configured, workspace_dir


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
                "Coach delegates; only Players claim tasks. Use "
                "coord_assign_task(task_id, to) to push-assign this "
                "to a specific Player, or coord_send_message to nudge "
                "one to claim it themselves."
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
        "coord_assign_task",
        (
            "Coach-only. Directly assign an open task to a specific Player — "
            "sets owner + status='claimed' without waiting for the Player to "
            "self-claim via coord_claim_task. Useful for push-assignment "
            "workflows.\n"
            "Params:\n"
            "- task_id: the task to assign (required)\n"
            "- to: target Player slot id ('p1'..'p10'; not 'coach', not 'broadcast')\n"
            "Fails if: you're a Player (Players report, don't assign), the "
            "task isn't status=open, the Player already owns another task, "
            "or the target isn't a valid Player slot."
        ),
        {"task_id": str, "to": str},
    )
    async def assign_task(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach can push-assign tasks. Players report and claim "
                "open tasks themselves via coord_claim_task."
            )
        task_id = (args.get("task_id") or "").strip()
        to = (args.get("to") or "").strip().lower()
        if not task_id:
            return _err("task_id is required")
        if not to:
            return _err("'to' is required (Player slot id)")
        if to == "coach" or to == "broadcast":
            return _err("can only assign to a Player (p1..p10), not coach or broadcast")
        if to not in VALID_RECIPIENTS:
            return _err(f"invalid target '{to}' — must be p1..p10")

        c = await configured_conn()
        try:
            # Target Player must exist and be free.
            cur = await c.execute(
                "SELECT current_task_id FROM agents WHERE id = ?", (to,)
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"Player '{to}' not found")
            busy_with = dict(row)["current_task_id"]
            if busy_with:
                return _err(
                    f"Player {to} already owns task {busy_with}; cancel or "
                    f"complete it before reassigning."
                )

            # Atomic assign — status='open' guard ensures we don't
            # clobber a task that was claimed between our SELECT and UPDATE.
            cur = await c.execute(
                "UPDATE tasks SET owner = ?, status = 'claimed', "
                "claimed_at = ? WHERE id = ? AND status = 'open' "
                "RETURNING id",
                (to, _now_iso(), task_id),
            )
            updated = await cur.fetchone()
            if not updated:
                cur = await c.execute(
                    "SELECT status, owner FROM tasks WHERE id = ?", (task_id,)
                )
                current = await cur.fetchone()
                if not current:
                    return _err(f"task {task_id} not found")
                d = dict(current)
                return _err(
                    f"task {task_id} is not open "
                    f"(status={d['status']}, owner={d['owner'] or '-'})"
                )

            await c.execute(
                "UPDATE agents SET current_task_id = ? WHERE id = ?",
                (task_id, to),
            )
            await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_assigned",
                "task_id": task_id,
                "to": to,
            }
        )
        # Auto-wake the assignee so the task actually starts moving
        # instead of waiting for someone to poke them. Debounced +
        # pause-respecting inside maybe_wake_agent; late import to
        # avoid a circular tools.py ↔ agents.py dependency at load
        # time.
        try:
            from server.agents import maybe_wake_agent
            await maybe_wake_agent(
                to,
                f"Coach just assigned you task {task_id}. "
                f"Use coord_read_inbox + coord_list_tasks to see your work, "
                f"claim what's yours, and start.",
            )
        except Exception:
            pass
        return _ok(f"assigned {task_id} → {to}")

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
        # Auto-wake direct recipients so they actually read + respond.
        # Skip broadcasts — waking every agent on every team announcement
        # would spiral costs. If you want broadcasts to nudge everyone,
        # Coach can @-mention specific slots instead.
        if to != "broadcast":
            try:
                from server.agents import maybe_wake_agent
                subj = f" (subject: {subject})" if subject else ""
                await maybe_wake_agent(
                    to,
                    f"New message from {caller_id}{subj}. "
                    f"Use coord_read_inbox to read it and respond.",
                )
            except Exception:
                pass
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

        # Fire-and-forget mirror to kDrive as a plain .md file under
        # /harness/memory/<topic>.md. Failures are swallowed and logged
        # inside KDriveClient — they never block the tool call.
        if kdrive.enabled:
            header = (
                f"<!-- auto-mirrored from the harness memory table\n"
                f"     topic: {topic}\n"
                f"     version: {version}\n"
                f"     last_updated: {now}\n"
                f"     last_updated_by: {caller_id}\n"
                f"-->\n\n"
            )
            asyncio.create_task(
                kdrive.write_text(f"memory/{topic}.md", header + content)
            )

        return _ok(
            f"saved memory[{topic}] v{version} ({len(content)} chars)"
            + (" · mirrored to kDrive" if kdrive.enabled else "")
        )

    @tool(
        "coord_write_knowledge",
        (
            "Write a durable artifact to the team knowledge bucket at "
            "kDrive knowledge/<path> (+ local /data/knowledge cache). "
            "Distinct from memory (overwritable scratchpad keyed by topic) "
            "and context (governance docs, Coach-only): knowledge is the "
            "free-form output bucket for reports, research, specs, and "
            "designs — anything worth reading again weeks from now.\n"
            "\n"
            "Path is agent-chosen within limits:\n"
            "  - must end in .md or .txt\n"
            "  - at most 4 path segments (e.g. 'reports/2026/04/weekly.md')\n"
            "  - each segment must start with alphanumeric; no spaces or /..\n"
            "\n"
            "Overwrites without warning if the path exists — date-stamp "
            "your filenames if you want history (reports/2026-04-23-review.md).\n"
            "\n"
            "Params:\n"
            "- path: POSIX relative path under knowledge/ (required)\n"
            "- body: full markdown or plain text content (required)"
        ),
        {"path": str, "body": str},
    )
    async def write_knowledge(args: dict[str, Any]) -> dict[str, Any]:
        path = (args.get("path") or "").strip()
        body = args.get("body") or ""
        try:
            ok = await knowmod.write(path, body, author=caller_id)
        except ValueError as e:
            return _err(str(e))
        if not ok:
            return _err("knowledge write failed — check server logs")
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "knowledge_written",
                "path": path,
                "size": len(body),
            }
        )
        return _ok(
            f"saved knowledge[{path}] ({len(body)} chars)"
            + (" · mirrored to kDrive" if kdrive.enabled else "")
        )

    @tool(
        "coord_read_knowledge",
        (
            "Read a knowledge doc by path. Local cache first, kDrive "
            "fallback. Returns the full body as text; caller should "
            "chunk or summarize for prompt brevity if needed.\n"
            "\n"
            "Use coord_list_knowledge to discover what's already been "
            "written before asking a Player to redo similar work.\n"
            "\n"
            "Params:\n"
            "- path: POSIX relative path under knowledge/ (required)"
        ),
        {"path": str},
    )
    async def read_knowledge(args: dict[str, Any]) -> dict[str, Any]:
        path = (args.get("path") or "").strip()
        try:
            body = await knowmod.read(path)
        except ValueError as e:
            return _err(str(e))
        if body is None:
            return _err(f"knowledge[{path}] not found")
        return _ok(
            f"knowledge[{path}] ({len(body)} chars):\n\n{body}"
        )

    @tool(
        "coord_list_knowledge",
        (
            "List every knowledge doc currently stored (POSIX paths, "
            "sorted). Cheap — reads a disk directory. No params."
        ),
        {},
    )
    async def list_knowledge(args: dict[str, Any]) -> dict[str, Any]:
        paths = knowmod.list_paths()
        if not paths:
            return _ok("(no knowledge docs yet)")
        return _ok("\n".join(paths))

    @tool(
        "coord_commit_push",
        (
            "Commit staged+unstaged changes in your worktree and push the "
            "branch. Players only (Coach never writes code). Runs:\n"
            "  git add -A\n"
            "  git commit -m <message>\n"
            "  git push origin HEAD    (unless push='false')\n"
            "Params:\n"
            "- message: commit message (required)\n"
            "- push: 'true' (default) or 'false' to skip the push.\n"
            "Returns 'nothing to commit' as a soft-OK if the working tree "
            "is clean. Requires HARNESS_PROJECT_REPO to be configured; "
            "push also needs pushable credentials (typically a PAT "
            "embedded in the project repo URL)."
        ),
        {"message": str, "push": str},
    )
    async def commit_push(args: dict[str, Any]) -> dict[str, Any]:
        if caller_is_coach:
            return _err(
                "Coach delegates; only Players commit code. If you want "
                "Coach to trigger a commit, message a Player with the task."
            )
        if not project_configured():
            return _err(
                "HARNESS_PROJECT_REPO is not set; no git worktree to "
                "commit into. Ask the operator to configure it and redeploy."
            )

        message = (args.get("message") or "").strip()
        if not message:
            return _err("message is required")
        if len(message) > 2000:
            return _err(f"message too long ({len(message)} chars, max 2000)")

        push_raw = str(args.get("push") or "true").strip().lower()
        do_push = push_raw not in ("false", "0", "no", "off")

        cwd = workspace_dir(caller_id)
        if not (cwd / ".git").exists():
            return _err(
                f"worktree at {cwd} is not a git checkout — something "
                "went wrong during workspace provisioning. Check "
                "/api/status workspaces section."
            )

        async def run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
            def _do() -> tuple[int, str, str]:
                p = subprocess.run(
                    cmd,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                return p.returncode, p.stdout, p.stderr
            return await asyncio.to_thread(_do)

        code, _out, err = await run(["git", "add", "-A"])
        if code != 0:
            return _err(f"git add failed: {err.strip()[:300]}")

        code, status_out, _ = await run(["git", "status", "--porcelain"])
        if not status_out.strip():
            return _ok("nothing to commit (working tree clean)")

        code, out, err = await run(["git", "commit", "-m", message])
        if code != 0:
            return _err(
                f"git commit failed: {(err or out).strip()[:300]}"
            )

        code, sha_out, _ = await run(["git", "rev-parse", "--short", "HEAD"])
        sha = sha_out.strip() or "?"

        push_note = ""
        pushed_ok = False
        if do_push:
            code, _out, err = await run(
                ["git", "push", "origin", "HEAD"], timeout=120
            )
            if code != 0:
                push_note = f" (PUSH FAILED: {err.strip()[:200]})"
            else:
                push_note = " (pushed)"
                pushed_ok = True
        else:
            push_note = " (local only)"

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "commit_pushed",
                "sha": sha,
                "message": message,
                "pushed": pushed_ok,
                "push_requested": do_push,
            }
        )
        return _ok(f"committed {sha}: {message}{push_note}")

    @tool(
        "coord_write_decision",
        (
            "Coach-only. Append a dated, immutable architectural decision "
            "record to /harness/decisions/ on kDrive (or /data/decisions/ "
            "if kDrive is disabled).\n"
            "\n"
            "Unlike memory (which is overwritable scratch), decisions are "
            "the durable 'we chose X because Y' record. Filename format: "
            "YYYY-MM-DD-<slug>.md. If a decision with the same slug for "
            "today already exists, a numeric suffix is appended.\n"
            "\n"
            "Params:\n"
            "- title: short human title (required; becomes the filename slug)\n"
            "- body: full markdown content (required; context, options, choice, rationale)"
        ),
        {"title": str, "body": str},
    )
    async def write_decision(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach writes decisions (durable architectural records). "
                "Players post findings to memory via coord_update_memory."
            )
        title = (args.get("title") or "").strip()
        body = (args.get("body") or "").strip()
        if not title:
            return _err("title is required")
        if not body:
            return _err("body is required (empty decisions are not useful)")
        if len(body) > 40_000:
            return _err(f"body too long ({len(body)} chars, max 40000)")

        # Slugify the title: lowercase, alphanumerics + dashes, max 48 chars.
        slug_raw = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        slug = slug_raw[:48].strip("-") or "decision"

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now_iso = _now_iso()
        base_filename = f"{today}-{slug}.md"

        frontmatter = (
            f"---\n"
            f"title: {title}\n"
            f"date: {today}\n"
            f"ts: {now_iso}\n"
            f"author: {caller_id}\n"
            f"---\n\n"
        )
        content = frontmatter + body + ("\n" if not body.endswith("\n") else "")

        # Prefer kDrive (the human-readable durable store). Fall back to the
        # local /data volume so offline agents still get a record.
        location = None
        filename = base_filename
        if kdrive.enabled:
            ok = await kdrive.write_text(f"decisions/{filename}", content)
            if ok:
                location = f"kDrive:decisions/{filename}"
        if location is None:
            # Local fallback when kDrive disabled or write failed.
            local_dir = Path(
                os.environ.get("HARNESS_DECISIONS_DIR", "/data/decisions")
            )
            try:
                local_dir.mkdir(parents=True, exist_ok=True)
                # Collision check: append a numeric suffix if needed
                target = local_dir / filename
                n = 2
                while target.exists():
                    filename = f"{today}-{slug}-{n}.md"
                    target = local_dir / filename
                    n += 1
                target.write_text(content, encoding="utf-8")
                location = f"local:{target}"
            except Exception as e:
                return _err(f"decision write failed: {type(e).__name__}: {e}")

        await bus.publish(
            {
                "ts": now_iso,
                "agent_id": caller_id,
                "type": "decision_written",
                "title": title,
                "filename": filename,
                "location": location,
                "size": len(body),
            }
        )
        return _ok(
            f"decision '{title}' saved to {location} ({len(body)} chars of body)"
        )

    @tool(
        "coord_write_context",
        (
            "Coach-only. Write or overwrite a governance-layer context doc "
            "at kDrive context/<kind>/<name>.md (+ local /data/context cache). "
            "These docs are concatenated into every agent's system prompt on "
            "their NEXT turn — no restart needed — so use this to set team-wide "
            "conventions, skills, and hard rules.\n"
            "\n"
            "Kinds:\n"
            "  - root: the special single CLAUDE.md top-level brief. "
            "Pass kind='root' and name='' (or 'CLAUDE').\n"
            "  - skills: reusable how-tos (e.g. 'debug-via-logs', 'commit-style').\n"
            "  - rules: hard invariants the team must not violate.\n"
            "\n"
            "Distinct from memory (free scratchpad, anyone writes) and decisions "
            "(append-only architectural records). Context is the rules-of-the-game "
            "layer: editable by Coach + human only, read by everyone.\n"
            "\n"
            "Params:\n"
            "- kind: 'root' | 'skills' | 'rules' (required)\n"
            "- name: file name without the .md suffix (empty/'CLAUDE' for kind='root')\n"
            "- body: full markdown content (required)"
        ),
        {"kind": str, "name": str, "body": str},
    )
    async def write_context(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach writes context docs. Players read them via their "
                "system prompt; if you think a rule or skill is missing, send "
                "Coach a message proposing the edit."
            )
        kind = (args.get("kind") or "").strip()
        name = (args.get("name") or "").strip()
        body = args.get("body") or ""
        try:
            ok = await ctxmod.write(kind, name, body)
        except ValueError as e:
            return _err(str(e))
        if not ok:
            return _err("context write failed — check server logs")
        effective_name = "CLAUDE" if kind == "root" else name
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "context_updated",
                "kind": kind,
                "name": effective_name,
                "size": len(body),
            }
        )
        return _ok(
            f"context saved: {kind}/{effective_name} ({len(body)} chars). "
            "Takes effect on every agent's next turn."
        )

    @tool(
        "coord_set_player_role",
        (
            "Coach-only. Assign a Player their human-readable name and "
            "role description. Stored on the agents row so the UI can "
            "label the pane (e.g. 'p3 — Alice — Frontend developer').\n"
            "\n"
            "Re-callable: overwrites prior values. Pass empty strings to "
            "clear. Emits a 'player_assigned' event so the UI refreshes "
            "the LeftRail / pane header immediately.\n"
            "\n"
            "Params:\n"
            "- player_id: one of p1..p10 (required)\n"
            "- name: short human name like 'Alice' (required)\n"
            "- role: one-line role description (required)"
        ),
        {"player_id": str, "name": str, "role": str},
    )
    async def set_player_role(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach assigns Player names / roles.")
        pid = (args.get("player_id") or "").strip()
        name = (args.get("name") or "").strip()
        role = (args.get("role") or "").strip()
        if not re.fullmatch(r"p([1-9]|10)", pid):
            return _err(f"invalid player_id '{pid}' — expected p1..p10")
        if len(name) > 80:
            return _err(f"name too long ({len(name)} chars, max 80)")
        if len(role) > 300:
            return _err(f"role too long ({len(role)} chars, max 300)")

        c = await configured_conn()
        try:
            cur = await c.execute(
                "UPDATE agents SET name = ?, role = ? WHERE id = ?",
                (name or None, role or None, pid),
            )
            if cur.rowcount == 0:
                return _err(f"player '{pid}' not found")
            await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "player_assigned",
                "player_id": pid,
                "name": name,
                "role": role,
            }
        )
        return _ok(f"{pid} → {name or '(no name)'} — {role or '(no role)'}")

    @tool(
        "coord_request_human",
        (
            "Escalate to the human operator. Use when stuck, blocked on a "
            "decision only the human can make, or when something looks "
            "wrong enough that the team should pause.\n"
            "\n"
            "Emits a high-visibility 'human_attention' event the UI surfaces "
            "prominently. Does NOT block the agent — you should still mark "
            "your task blocked / cancelled / done as appropriate.\n"
            "\n"
            "Params:\n"
            "- subject: short headline (required, max 200 chars)\n"
            "- body: longer explanation incl. what you tried (required)\n"
            "- urgency: 'normal' (default) or 'blocker' (whole-team gating)"
        ),
        {"subject": str, "body": str, "urgency": str},
    )
    async def request_human(args: dict[str, Any]) -> dict[str, Any]:
        subject = (args.get("subject") or "").strip()
        body = (args.get("body") or "").strip()
        urgency = (args.get("urgency") or "normal").strip().lower()
        if not subject:
            return _err("subject is required")
        if not body:
            return _err("body is required (explain what you tried)")
        if len(subject) > 200:
            return _err(f"subject too long ({len(subject)} chars, max 200)")
        if len(body) > 10_000:
            return _err(f"body too long ({len(body)} chars, max 10000)")
        if urgency not in ("normal", "blocker"):
            return _err("urgency must be 'normal' or 'blocker'")

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "human_attention",
                "subject": subject,
                "body": body,
                "urgency": urgency,
            }
        )
        return _ok(
            f"human notified ({urgency}): {subject}. "
            "Continue or pause your current task as appropriate."
        )

    return create_sdk_mcp_server(
        name="coord",
        version="0.8.0",
        tools=[
            list_tasks,
            create_task,
            claim_task,
            update_task,
            assign_task,
            send_message,
            read_inbox,
            list_memory,
            read_memory,
            update_memory,
            commit_push,
            write_decision,
            write_context,
            write_knowledge,
            read_knowledge,
            list_knowledge,
            set_player_role,
            request_human,
        ],
    )


ALLOWED_COORD_TOOLS = [
    "mcp__coord__coord_list_tasks",
    "mcp__coord__coord_create_task",
    "mcp__coord__coord_claim_task",
    "mcp__coord__coord_update_task",
    "mcp__coord__coord_assign_task",
    "mcp__coord__coord_send_message",
    "mcp__coord__coord_read_inbox",
    "mcp__coord__coord_list_memory",
    "mcp__coord__coord_read_memory",
    "mcp__coord__coord_update_memory",
    "mcp__coord__coord_commit_push",
    "mcp__coord__coord_write_decision",
    "mcp__coord__coord_write_context",
    "mcp__coord__coord_write_knowledge",
    "mcp__coord__coord_read_knowledge",
    "mcp__coord__coord_list_knowledge",
    "mcp__coord__coord_set_player_role",
    "mcp__coord__coord_request_human",
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

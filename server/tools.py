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

from server import knowledge as knowmod
from server import outputs as outmod
from server.db import configured_conn, resolve_active_project
from server.events import bus
from server.webdav import webdav
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


async def _is_locked(agent_id: str) -> bool:
    """Return True if the agent's `locked` flag is set. Missing row /
    DB error returns False — lock is a safety restriction, not a
    correctness invariant, so failing open is preferable to blocking
    the whole tool on a hiccup."""
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT locked FROM agents WHERE id = ?", (agent_id,)
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        return False
    if not row:
        return False
    return bool(dict(row).get("locked"))


def build_coord_server(caller_id: str, *, include_proxy_metadata: bool = False) -> Any:
    """Build an in-process MCP server whose tools know which agent is calling.

    Each SDK query gets its own server so hierarchy enforcement (Coach can
    give orders, Players cannot) operates without the LLM needing to pass
    its own identity as a param.

    By default the returned server is safe to hand to ClaudeAgentOptions.
    The coord loopback proxy can opt into `_handlers` / `_tool_names`
    metadata, which contains Python callables and must never be passed to
    the Claude SDK because its options path JSON-serializes MCP config.
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
        project_id = await resolve_active_project()
        where_parts.insert(0, "project_id = ?")
        params.insert(0, project_id)
        clause = " WHERE " + " AND ".join(where_parts)

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

        project_id = await resolve_active_project()
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
                        "SELECT owner FROM tasks WHERE id = ? AND project_id = ?",
                        (parent_id, project_id),
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
                "INSERT INTO tasks (id, project_id, title, description, parent_id, "
                "priority, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, project_id, title, description, parent_id, priority, caller_id),
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

            project_id = await resolve_active_project()
            # Atomic claim — race-safe via status='open' guard.
            cur = await c.execute(
                "UPDATE tasks SET owner = ?, status = 'claimed', "
                "claimed_at = ? WHERE id = ? AND status = 'open' "
                "AND project_id = ? RETURNING id",
                (caller_id, _now_iso(), task_id, project_id),
            )
            updated = await cur.fetchone()
            if not updated:
                cur = await c.execute(
                    "SELECT status, owner FROM tasks WHERE id = ? AND project_id = ?",
                    (task_id, project_id),
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

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT owner, status, created_by, title FROM tasks "
                "WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            d = dict(row)
            current_owner: str | None = d["owner"]
            old_status: str = d["status"]
            created_by: str = d.get("created_by") or ""
            task_title: str = d.get("title") or ""

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
                    "WHERE id = ? AND project_id = ?",
                    (new_status, now, task_id, project_id),
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
                    "UPDATE tasks SET status = ? WHERE id = ? AND project_id = ?",
                    (new_status, task_id, project_id),
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
                # Include owner so the UI can fan out this event to the
                # owner's pane — Coach cancelling/blocking a task assigned
                # to p3 should be visible from p3 even when the update
                # didn't originate there.
                "owner": current_owner,
            }
        )
        # Notify the creator when a Player finishes work they didn't
        # assign to themselves. Without this, Coach has to poll the
        # board to notice done/blocked/cancelled transitions. Skip the
        # self-notify case (a Player both creating and completing a
        # subtask) and the creator-is-caller case. We fire on done,
        # blocked, and cancelled — all three are moments Coach cares
        # about since they change the available work pool.
        if (
            new_status in ("done", "blocked", "cancelled")
            and created_by
            and created_by != caller_id
            and created_by != "human"
        ):
            try:
                from server.agents import _deliver_system_message
                verb = {
                    "done": "finished",
                    "blocked": "marked blocked",
                    "cancelled": "cancelled",
                }[new_status]
                note_line = f"\nNote: {note}" if note else ""
                await _deliver_system_message(
                    from_id=caller_id,
                    to_id=created_by,
                    subject=f"{task_id} {verb}",
                    body=(
                        f"I {verb} {task_id} \"{task_title[:100]}\"."
                        f"{note_line}"
                    ),
                    priority="normal",
                )
            except Exception:
                pass
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

        # Lock: human can mark a Player off-limits for Coach. When set,
        # Coach cannot push work; Player still reads docs + answers
        # human prompts. Fail explicitly so the LLM knows to pick a
        # different Player rather than retrying.
        if await _is_locked(to):
            return _err(
                f"Player {to} is locked (human marked them off-limits "
                f"for Coach orchestration). Pick an unlocked Player, or "
                f"ask the human to unlock {to}."
            )

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

            project_id = await resolve_active_project()
            # Atomic assign — status='open' guard ensures we don't
            # clobber a task that was claimed between our SELECT and UPDATE.
            cur = await c.execute(
                "UPDATE tasks SET owner = ?, status = 'claimed', "
                "claimed_at = ? WHERE id = ? AND status = 'open' "
                "AND project_id = ? RETURNING id",
                (to, _now_iso(), task_id, project_id),
            )
            updated = await cur.fetchone()
            if not updated:
                cur = await c.execute(
                    "SELECT status, owner FROM tasks WHERE id = ? AND project_id = ?",
                    (task_id, project_id),
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
            # Task assignment is a discrete action (not conversational)
            # so bypass the ping-pong debounce — Coach should be able to
            # push a new task to a Player even if the Player just
            # finished the previous turn a few seconds ago. The task is
            # already at status='claimed' with owner=<to> (atomic UPDATE
            # above), so the wake prompt tells the Player to move it to
            # in_progress + start — not to "claim".
            await maybe_wake_agent(
                to,
                f"Coach assigned you task {task_id} (status=claimed, "
                f"you're the owner). Use coord_list_tasks to see the "
                f"title/description, coord_update_task to move it to "
                f"in_progress, then start the work.",
                bypass_debounce=True,
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

        # Lock enforcement: Coach cannot direct-message a locked
        # Player. Broadcasts still go through at send-time (the delivery
        # filter is in read_inbox) so Coach doesn't have to know the
        # lock state of every team member when pushing a broadcast.
        if caller_is_coach and to != "broadcast" and await _is_locked(to):
            return _err(
                f"{to} is locked (human marked them off-limits for Coach). "
                f"Message not sent."
            )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "INSERT INTO messages (project_id, from_id, to_id, subject, body, priority) "
                "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                (project_id, caller_id, to, subject, body, priority),
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
                # Include a body preview inline so the recipient doesn't
                # have to spend a tool-call to see what the message was —
                # keeps the conversation snappy. Full body + mark-read
                # still requires coord_read_inbox for anything longer.
                preview_snippet = body.strip().replace("\n", " ")[:240]
                await maybe_wake_agent(
                    to,
                    f"New message from {caller_id}{subj}: \"{preview_snippet}\"\n\n"
                    f"Call coord_read_inbox to mark it read and see any "
                    f"other queued messages, then respond as appropriate.",
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
        # Locked Players ignore every Coach-sourced message (direct or
        # broadcast). The filter lives at the read layer so a locked
        # Player flipping back to unlocked still gets the message —
        # it's queued but invisible while locked. Human and peer-Player
        # messages pass through unaffected.
        reader_locked = await _is_locked(caller_id)
        project_id = await resolve_active_project()

        c = await configured_conn()
        try:
            # Per-recipient unread via NOT EXISTS on message_reads — avoids
            # the broadcast bug where the first reader hides the message
            # from everyone else.
            sql = (
                "SELECT m.id, m.from_id, m.to_id, m.subject, m.body, "
                "       m.sent_at, m.priority "
                "FROM messages m "
                "WHERE m.project_id = ? "
                "  AND (m.to_id = ? OR m.to_id = 'broadcast') "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM message_reads r "
                "    WHERE r.message_id = m.id AND r.agent_id = ?"
                "  ) "
            )
            params: tuple[Any, ...] = (project_id, caller_id, caller_id)
            if reader_locked:
                sql += "  AND m.from_id != 'coach' "
            sql += "ORDER BY m.sent_at ASC"
            cur = await c.execute(sql, params)
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
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT topic, version, last_updated, last_updated_by, "
                "length(content) AS size FROM memory_docs "
                "WHERE project_id = ? "
                "ORDER BY last_updated DESC LIMIT 200",
                (project_id,),
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
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT content, version, last_updated, last_updated_by "
                "FROM memory_docs WHERE topic = ? AND project_id = ?",
                (topic, project_id),
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
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            # UPSERT: insert with version=1, or increment on conflict.
            cur = await c.execute(
                "INSERT INTO memory_docs "
                "(project_id, topic, content, last_updated, last_updated_by, version) "
                "VALUES (?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(project_id, topic) DO UPDATE SET "
                "  content = excluded.content, "
                "  last_updated = excluded.last_updated, "
                "  last_updated_by = excluded.last_updated_by, "
                "  version = memory_docs.version + 1 "
                "RETURNING version",
                (project_id, topic, content, now, caller_id),
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
        # inside WebDAVClient — they never block the tool call.
        if webdav.enabled:
            header = (
                f"<!-- auto-mirrored from the harness memory table\n"
                f"     topic: {topic}\n"
                f"     version: {version}\n"
                f"     last_updated: {now}\n"
                f"     last_updated_by: {caller_id}\n"
                f"-->\n\n"
            )
            asyncio.create_task(
                webdav.write_text(
                    f"projects/{project_id}/memory/{topic}.md", header + content
                )
            )

        return _ok(
            f"saved memory[{topic}] v{version} ({len(content)} chars)"
            + (" · mirrored to WebDAV" if webdav.enabled else "")
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
            + (" · mirrored to WebDAV" if webdav.enabled else "")
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
        paths = await knowmod.list_paths()
        if not paths:
            return _ok("(no knowledge docs yet)")
        return _ok("\n".join(paths))

    @tool(
        "coord_save_output",
        (
            "Save a binary deliverable (docx / pdf / png / zip / …) to "
            "the team outputs bucket at kDrive outputs/<path> (+ local "
            "/data/outputs cache). Use for final artifacts the human "
            "asked for — reports, charts, exports. Text deliverables "
            "usually belong in knowledge/ instead.\n"
            "\n"
            "Path is agent-chosen within limits:\n"
            "  - at most 4 path segments\n"
            "  - each segment starts with alphanumeric, no spaces / ..\n"
            "  - leaf extension must be in the outputs allow-list "
            "(docx, xlsx, pptx, pdf, png, jpg, gif, webp, svg, zip, "
            "tar, gz, csv, tsv, md, txt, html, json)\n"
            "\n"
            "Content is base64-encoded — read the file with Bash "
            "(`base64 -w0 foo.docx`) or have your process write base64 "
            "directly. 20 MB size cap after decoding.\n"
            "\n"
            "Overwrites without warning if the path exists — date-stamp "
            "your filenames if you want history.\n"
            "\n"
            "Params:\n"
            "- path: POSIX relative path under outputs/ (required)\n"
            "- content_base64: base64-encoded bytes of the file (required)"
        ),
        {"path": str, "content_base64": str},
    )
    async def save_output(args: dict[str, Any]) -> dict[str, Any]:
        path = (args.get("path") or "").strip()
        b64 = args.get("content_base64") or ""
        try:
            data = outmod.decode_base64(b64)
        except ValueError as e:
            return _err(str(e))
        try:
            ok = await outmod.save(path, data, author=caller_id)
        except ValueError as e:
            return _err(str(e))
        if not ok:
            return _err("outputs write failed — check server logs")
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "output_saved",
                "path": path,
                "bytes": len(data),
            }
        )
        return _ok(
            f"saved outputs[{path}] ({len(data)} bytes)"
            + (" · mirrored to WebDAV" if webdav.enabled else "")
        )

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
        project_id = await resolve_active_project()
        from server.paths import project_paths
        location = None
        filename = base_filename
        if webdav.enabled:
            ok = await webdav.write_text(
                f"projects/{project_id}/decisions/{filename}", content
            )
            if ok:
                location = f"kDrive:projects/{project_id}/decisions/{filename}"
        if location is None:
            # Local fallback when kDrive disabled or write failed.
            local_dir = project_paths(project_id).decisions
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

    # Coach edits the global CLAUDE.md at /data/CLAUDE.md and
    # per-project CLAUDE.md at /data/projects/<active>/CLAUDE.md via
    # the standard Write tool; both files are read fresh on every
    # agent turn (server/context.py).

    @tool(
        "coord_propose_truth_update",
        (
            "Coach-only. Propose an update to a file under the active "
            "project's truth/ folder. truth/ is the user's signed-off "
            "source-of-truth (specs, brand guidelines, contracts); "
            "agents NEVER write to it directly — the harness's "
            "PreToolUse guard hook denies any direct Write/Edit/Bash "
            "to truth/. This tool is the ONLY path to change truth/, "
            "and approval is gated on an explicit human click in the "
            "UI's 'Truth proposals' section. Players cannot call this "
            "tool — they must ask Coach to relay.\n"
            "\n"
            "What happens:\n"
            "  1. The proposal is queued (status=pending).\n"
            "  2. The user reviews the diff in the UI and clicks "
            "approve or deny.\n"
            "  3. On approve, the harness writes the file with the "
            "proposed content. On deny, the file is left as-is.\n"
            "  4. A `truth_proposal_resolved` event fires so you'll "
            "see the outcome on your next turn.\n"
            "\n"
            "Params:\n"
            "- path: relative path under truth/ (e.g. 'specs.md' or "
            "  'brand/colors.md'). Required. No leading slash, no "
            "  '..' segments.\n"
            "- content: full new file body (required). This is a full "
            "  REPLACE — include the parts you're keeping verbatim, "
            "  not just a diff.\n"
            "- summary: one-line 'why' the user reads next to "
            "  approve/deny (required, ≤ 200 chars). Be specific: "
            "  'Add launch-date constraint to specs.md §3' beats "
            "  'update specs'."
        ),
        {"path": str, "content": str, "summary": str},
    )
    async def propose_truth_update(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach can propose truth/ updates. Players: send "
                "a coord_send_message to coach describing the proposed "
                "change; Coach will relay it as a proposal."
            )
        rel = (args.get("path") or "").strip()
        content = args.get("content")
        summary = (args.get("summary") or "").strip()
        if not rel:
            return _err("path is required")
        # Defensive strip: Coach is told to pass paths relative to truth/
        # (e.g. "specs.md", not "truth/specs.md"), but accept the prefixed
        # form too rather than fail confusingly. Strip a single leading
        # "truth/" so "truth/specs.md" and "specs.md" both resolve to
        # /data/projects/<slug>/truth/specs.md.
        if rel.startswith("truth/"):
            rel = rel[len("truth/"):]
        if rel.startswith("/") or ".." in rel.split("/"):
            return _err("path must be relative under truth/, no '..' segments")
        if not isinstance(content, str):
            return _err("content is required (string)")
        if len(content) > 200_000:
            return _err(
                f"content too long ({len(content)} chars, max 200000)"
            )
        if not summary:
            return _err("summary is required (one-line 'why' the user reads)")
        if len(summary) > 200:
            return _err(f"summary too long ({len(summary)} chars, max 200)")

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "INSERT INTO truth_proposals "
                "(project_id, proposer_id, path, proposed_content, summary) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, caller_id, rel, content, summary),
            )
            await c.commit()
            proposal_id = cur.lastrowid
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "truth_proposal_created",
                "proposal_id": proposal_id,
                "path": rel,
                "summary": summary,
                "size": len(content),
            }
        )
        return _ok(
            f"proposal #{proposal_id} queued for truth/{rel} "
            f"({len(content)} chars). The user will review and "
            f"approve or deny in the UI; you'll see "
            f"`truth_proposal_resolved` on your next turn."
        )

    @tool(
        "coord_list_team",
        (
            "Read the current team roster: slot id, name, role, brief, "
            "status, and currently-claimed task (if any) for every agent "
            "on the team (Coach + p1..p10).\n"
            "\n"
            "Useful at the start of a fresh turn to remember who's on "
            "the team, what they're working on, and what their domain "
            "is — agents don't carry cross-turn memory of this without "
            "reading it. No params."
        ),
        {},
    )
    async def list_team(args: dict[str, Any]) -> dict[str, Any]:
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            # JOIN agents with agent_project_roles for the active project
            # so the response carries this project's name/role/brief.
            cur = await c.execute(
                "SELECT a.id, a.kind, r.name AS name, r.role AS role, "
                "       r.brief AS brief, a.status, a.current_task_id, a.locked "
                "FROM agents a "
                "LEFT JOIN agent_project_roles r "
                "  ON r.slot = a.id AND r.project_id = ? "
                "ORDER BY CASE a.kind WHEN 'coach' THEN 0 ELSE 1 END, a.id",
                (project_id,),
            )
            rows = await cur.fetchall()
        finally:
            await c.close()
        if not rows:
            return _ok("(no agents in the roster — init_db never ran)")
        lines: list[str] = []
        for r in rows:
            d = dict(r)
            bits = [d["id"]]
            if d.get("name") and d["name"] != d["id"]:
                bits.append(d["name"])
            if d.get("role"):
                bits.append(f"({d['role']})")
            bits.append(f"· {d['status']}")
            if d.get("current_task_id"):
                bits.append(f"· on {d['current_task_id']}")
            # LOCKED marker: render loudly so the model skims it. The
            # enforcement is also at the tool layer, but telling Coach
            # up-front saves wasted turns trying to assign to a locked
            # slot.
            if d.get("locked"):
                bits.append("· 🔒 LOCKED (off-limits for Coach)")
            header = " ".join(bits)
            if d.get("brief"):
                # Keep it terse in the listing — full brief is retrievable
                # via /api/agents if needed.
                preview = str(d["brief"])[:140].replace("\n", " ")
                lines.append(f"{header}\n    brief: {preview}")
            else:
                lines.append(header)
        return _ok("\n".join(lines))

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
        # Squash any internal whitespace (newlines / tabs / multiple
        # spaces) to single spaces. name and role render inline in the
        # pane header — a multi-line name would break layout.
        def _single_line(s: str) -> str:
            return " ".join(s.split()).strip()
        name = _single_line(args.get("name") or "")
        role = _single_line(args.get("role") or "")
        if not re.fullmatch(r"p([1-9]|10)", pid):
            return _err(f"invalid player_id '{pid}' — expected p1..p10")
        if len(name) > 80:
            return _err(f"name too long ({len(name)} chars, max 80)")
        if len(role) > 300:
            return _err(f"role too long ({len(role)} chars, max 300)")

        # Verify the player slot exists in the global agents roster
        # before writing per-project identity.
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (pid,))
            if not await cur.fetchone():
                return _err(f"player '{pid}' not found")
            # Upsert into agent_project_roles for the active project.
            await c.execute(
                "INSERT INTO agent_project_roles (slot, project_id, name, role) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(slot, project_id) DO UPDATE SET "
                "  name = excluded.name, role = excluded.role",
                (pid, project_id, name or None, role or None),
            )
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
        "coord_answer_question",
        (
            "Coach-only. Resolve a pending AskUserQuestion from a Player. "
            "When a Player calls AskUserQuestion, their turn pauses and "
            "the question is routed to your inbox with a correlation_id; "
            "call this tool with that id plus your picks to unblock them.\n"
            "\n"
            "Params:\n"
            "- correlation_id: the id from the question message (required).\n"
            "- answers: object mapping each exact question text to the "
            "  selected option label. Example: "
            "  {'How should I format the output?': 'Summary', "
            "   'Which sections?': 'Introduction, Conclusion'}. "
            "  For multi-select, join labels with ', '. For free-text, use "
            "  the user's literal string."
        ),
        {"correlation_id": str, "answers": dict},
    )
    async def answer_question(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach answers Player questions. Players get answers "
                "back automatically when Coach resolves."
            )
        correlation_id = (args.get("correlation_id") or "").strip()
        answers = args.get("answers") or {}
        if not correlation_id:
            return _err("correlation_id is required")
        if not isinstance(answers, dict) or not answers:
            return _err("answers must be a non-empty object (question → label)")
        # Normalise all values to strings — SDK expects record<string,string>.
        clean: dict[str, str] = {}
        for k, v in answers.items():
            if not isinstance(k, str) or not k.strip():
                continue
            clean[k] = str(v) if v is not None else ""
        if not clean:
            return _err("no valid (question, answer) pairs in answers")
        from server import interactions as interactions_registry
        entry = interactions_registry.get(correlation_id)
        if entry is None or entry.kind != "question":
            return _err(
                f"correlation_id {correlation_id!r} not found, wrong kind, "
                "or already resolved / timed out"
            )
        ok = interactions_registry.resolve(correlation_id, clean)
        if not ok:
            return _err(
                f"correlation_id {correlation_id!r} already resolved / timed out"
            )
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "question_answered",
                "correlation_id": correlation_id,
                "route": "coach",
                "answer_keys": list(clean.keys()),
            }
        )
        return _ok(
            f"answered {correlation_id} ({len(clean)} keys). The Player "
            "resumes on their paused turn."
        )

    @tool(
        "coord_answer_plan",
        (
            "Coach-only. Resolve a pending ExitPlanMode from a Player. "
            "When a Player in plan mode calls ExitPlanMode, their turn "
            "pauses and the plan is routed to your inbox with a "
            "correlation_id; call this tool with that id plus your "
            "decision to unblock them.\n"
            "\n"
            "Params:\n"
            "- correlation_id: the id from the plan approval message "
            "(required).\n"
            "- decision: 'approve' | 'reject' | 'approve_with_comments' "
            "(required). `approve` lets the plan execute as-is. "
            "`reject` keeps the Player in plan mode and phrases your "
            "comments as 'approved, but revise to include X' so they "
            "revise and exit plan mode again. `approve_with_comments` "
            "lets the plan execute AND queues your comments as an inbox "
            "message the Player reads on their next turn.\n"
            "- comments: required for 'reject' and 'approve_with_comments', "
            "optional for 'approve'. Max 10k chars."
        ),
        {"correlation_id": str, "decision": str, "comments": str},
    )
    async def answer_plan(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach decides on Player plans. Players stay in "
                "plan mode until Coach resolves."
            )
        correlation_id = (args.get("correlation_id") or "").strip()
        decision = (args.get("decision") or "").strip().lower()
        comments = (args.get("comments") or "").strip()
        if not correlation_id:
            return _err("correlation_id is required")
        if decision not in ("approve", "reject", "approve_with_comments"):
            return _err(
                "decision must be 'approve', 'reject', or 'approve_with_comments'"
            )
        if decision in ("reject", "approve_with_comments") and not comments:
            return _err(
                f"'{decision}' requires non-empty comments explaining what "
                "to revise / keep in mind"
            )
        if len(comments) > 10_000:
            return _err(f"comments too long ({len(comments)} chars, max 10000)")
        from server import interactions as interactions_registry
        entry = interactions_registry.get(correlation_id)
        if entry is None or entry.kind != "plan":
            return _err(
                f"correlation_id {correlation_id!r} not found, wrong kind, "
                "or already resolved / timed out"
            )
        ok = interactions_registry.resolve(
            correlation_id,
            {"decision": decision, "comments": comments},
        )
        if not ok:
            return _err(
                f"correlation_id {correlation_id!r} already resolved / timed out"
            )
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "plan_decided",
                "correlation_id": correlation_id,
                "route": "coach",
                "decision": decision,
                "has_comments": bool(comments),
            }
        )
        return _ok(
            f"plan {decision} for {correlation_id}. The Player resumes on "
            "their paused turn."
        )

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

    _tools = [
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
        propose_truth_update,
        write_knowledge,
        read_knowledge,
        list_knowledge,
        list_team,
        set_player_role,
        answer_question,
        answer_plan,
        request_human,
    ]
    server = create_sdk_mcp_server(name="coord", version="0.8.0", tools=_tools)
    # Stash a name → handler map so the coord_mcp proxy endpoint
    # (server.coord_mcp + POST /api/_coord/{tool}) can dispatch by
    # name without re-importing SDK internals. This metadata is not
    # present by default because Claude serializes MCP config to JSON.
    if include_proxy_metadata:
        server["_handlers"] = {t.name: t.handler for t in _tools}
        server["_tool_names"] = [t.name for t in _tools]
    return server


def coord_tool_names() -> list[str]:
    """Stable list of registered coord tool names — used by the proxy
    catalog (`server.coord_mcp`) and by the contract test that
    asserts the proxy enumeration matches the live registry.
    Builds a coord server for an arbitrary caller and pulls its names.
    """
    server = build_coord_server("coach", include_proxy_metadata=True)
    return list(server["_tool_names"])


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
    "mcp__coord__coord_propose_truth_update",
    "mcp__coord__coord_write_knowledge",
    "mcp__coord__coord_read_knowledge",
    "mcp__coord__coord_list_knowledge",
    "mcp__coord__coord_save_output",
    "mcp__coord__coord_list_team",
    "mcp__coord__coord_set_player_role",
    "mcp__coord__coord_answer_question",
    "mcp__coord__coord_answer_plan",
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

# AskUserQuestion is routed by our can_use_tool callback in agents.py:
# Coach → form in the UI, Player → Coach's inbox. Must be in the allow
# list (the SDK won't run it otherwise) even though callback mediates
# the actual flow.
_INTERACTIVE_TOOLS = ["AskUserQuestion"]

# Coach = read + coord + interactive. Matches the spec rule "you never
# write code, you delegate" — enforced structurally (not just by prompt).
ALLOWED_COACH_TOOLS = STANDARD_READ_TOOLS + ALLOWED_COORD_TOOLS + _INTERACTIVE_TOOLS

# Players get the full standard set + coord + interactive.
ALLOWED_PLAYER_TOOLS = (
    STANDARD_READ_TOOLS + STANDARD_WRITE_TOOLS + ALLOWED_COORD_TOOLS + _INTERACTIVE_TOOLS
)

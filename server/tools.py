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


# Kanban-shaped task state machine (Docs/kanban-specs.md §2). Reject
# transitions not listed here. The legacy enum values (open/claimed/
# in_progress/blocked/done/cancelled) are accepted as input aliases
# during a one-release deprecation window: 'done'/'cancelled' map to
# 'archive', 'in_progress' is treated as a no-op for tasks already in
# 'execute', etc. See `_normalize_status_alias`.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "plan":            {"execute", "archive"},
    "execute":         {"audit_syntax", "archive"},
    "audit_syntax":    {"audit_semantics", "execute"},
    "audit_semantics": {"ship", "execute"},
    "ship":            {"archive"},
    "archive":         set(),
}

# Stages the kanban subscriber and Coach see as "the audit loop".
AUDIT_STAGES: frozenset[str] = frozenset({"audit_syntax", "audit_semantics"})

# All valid kanban stages (used by validators that accept any of them).
ALL_KANBAN_STAGES: frozenset[str] = frozenset(VALID_TRANSITIONS.keys())

# Legacy → kanban status aliases. Accepted for one release so existing
# Coach prompts and external scripts that still call coord_update_task
# with 'done' or 'cancelled' don't break. The alias resolver is applied
# on tool input; the resolved value is what the state-machine validates
# against. Cancellation has a side-effect (cancelled_at is stamped),
# handled at the call site.
_LEGACY_STATUS_ALIASES: dict[str, str] = {
    "open": "plan",
    "claimed": "execute",
    "in_progress": "execute",
    "blocked": "execute",  # blocked is now an orthogonal flag, not a status
    "done": "archive",
    "cancelled": "archive",
}


def _normalize_status_alias(status: str) -> str:
    """Map a legacy status value to its kanban equivalent. No-op for
    already-kanban values."""
    return _LEGACY_STATUS_ALIASES.get(status, status)


def _valid_transition(old: str, new: str) -> bool:
    return new in VALID_TRANSITIONS.get(old, set())


async def _check_kanban_role_gate(
    c: Any,
    project_id: str,
    task_id: str,
    old: str,
    new: str,
    *,
    was_cancellation: bool,
) -> str | None:
    """Role-completion gate enforcer for non-force stage transitions
    (Docs/kanban-specs.md §2.3).

    Returns an error string when the requested transition would skip an
    artifact / role-completion event that should drive it (commit_pushed,
    audit_report_submitted{pass|fail}, task_shipped). Returns None when
    the transition is allowed under the gate.

    Bypass paths (must NOT call this function):
    - `coord_advance_task_stage` (Coach-only override)
    - `POST /api/tasks/{id}/stage` with `force=true`

    Cancellation (`was_cancellation=True` AND target is `archive`) skips
    every gate by design — cancelling is always allowed at any stage.
    """
    if new == "archive" and was_cancellation:
        return None

    cur = await c.execute(
        "SELECT complexity, spec_path FROM tasks "
        "WHERE id = ? AND project_id = ?",
        (task_id, project_id),
    )
    row = await cur.fetchone()
    if row is None:
        # Caller paths already SELECT the row; getting here means a
        # race or programmer error. Caller will surface the missing
        # task in its own error message.
        return None
    t = dict(row)
    complexity = (t.get("complexity") or "standard").lower()
    spec_path = t.get("spec_path")

    if old == "plan" and new == "execute":
        if complexity == "standard" and not spec_path:
            return (
                f"task {task_id} has no spec — write one with "
                f"coord_write_task_spec (or delegate via "
                f"coord_assign_planner) before moving plan → execute. "
                f"Use coord_advance_task_stage to force."
            )
        cur = await c.execute(
            "SELECT 1 FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'executor' "
            "AND owner IS NOT NULL AND superseded_by IS NULL "
            "ORDER BY assigned_at DESC LIMIT 1",
            (task_id,),
        )
        if not await cur.fetchone():
            return (
                f"task {task_id} has no claimed executor; assign via "
                f"coord_assign_task or have a Player call "
                f"coord_claim_task before moving plan → execute. Use "
                f"coord_advance_task_stage to force."
            )
        return None

    if old == "execute" and new == "audit_syntax":
        return (
            f"manual execute → audit_syntax is not allowed; the kanban "
            f"subscriber auto-advances when the executor calls "
            f"coord_commit_push(task_id={task_id!r}). Use "
            f"coord_advance_task_stage to force."
        )

    if old == "execute" and new == "archive":
        return (
            f"manual execute → archive (delivery) is not allowed. "
            f"Simple-complexity tasks auto-archive when the executor "
            f"calls coord_commit_push(task_id={task_id!r}); standard "
            f"tasks must traverse audit + ship. Use "
            f"coord_advance_task_stage to force, or pass "
            f"status='cancelled' if the intent is cancellation."
        )

    if old == "audit_syntax" and new == "audit_semantics":
        if not await _has_passing_auditor(c, task_id, "auditor_syntax"):
            return (
                f"audit_syntax → audit_semantics requires the active "
                f"syntax auditor to submit verdict='pass' via "
                f"coord_submit_audit_report. Use "
                f"coord_advance_task_stage to force."
            )
        return None

    if old == "audit_semantics" and new == "ship":
        if not await _has_passing_auditor(c, task_id, "auditor_semantics"):
            return (
                f"audit_semantics → ship requires the active semantic "
                f"auditor to submit verdict='pass' via "
                f"coord_submit_audit_report. Use "
                f"coord_advance_task_stage to force."
            )
        return None

    if old == "ship" and new == "archive":
        if not await _has_completed_shipper(c, task_id):
            return (
                f"ship → archive requires the assigned shipper to call "
                f"coord_mark_shipped(task_id={task_id!r}). Use "
                f"coord_advance_task_stage to force."
            )
        return None

    # audit_* → execute (manual revert) is allowed: it mirrors the
    # subscriber's auto-revert on a fail verdict, and Coach occasionally
    # needs to flag work as needing rework even when no auditor verdict
    # has landed yet.
    return None


async def _has_passing_auditor(c: Any, task_id: str, role: str) -> bool:
    """True if the active (un-superseded) auditor row for this task +
    role has `verdict='pass'`."""
    cur = await c.execute(
        "SELECT verdict FROM task_role_assignments "
        "WHERE task_id = ? AND role = ? AND superseded_by IS NULL "
        "ORDER BY assigned_at DESC LIMIT 1",
        (task_id, role),
    )
    row = await cur.fetchone()
    if not row:
        return False
    return (dict(row).get("verdict") or "").lower() == "pass"


async def _has_completed_shipper(c: Any, task_id: str) -> bool:
    """True if the active shipper assignment has `completed_at` set."""
    cur = await c.execute(
        "SELECT completed_at FROM task_role_assignments "
        "WHERE task_id = ? AND role = 'shipper' AND superseded_by IS NULL "
        "ORDER BY assigned_at DESC LIMIT 1",
        (task_id,),
    )
    row = await cur.fetchone()
    if not row:
        return False
    return bool(dict(row).get("completed_at"))


# Auditor / shipper / planner roles that Coach can assign. Mirror of
# the task_role_assignments.role CHECK constraint.
ROLE_NAMES: frozenset[str] = frozenset({
    "planner", "executor", "auditor_syntax", "auditor_semantics", "shipper",
})


def _resolve_audit_role_kind(kind: str) -> str | None:
    """Convert a Coach-facing kind ('syntax' / 'semantics') to the
    underlying role-assignment row's role value."""
    if kind == "syntax":
        return "auditor_syntax"
    if kind == "semantics":
        return "auditor_semantics"
    return None


def _audit_kind_from_role(role: str) -> str | None:
    """Inverse of `_resolve_audit_role_kind`."""
    if role == "auditor_syntax":
        return "syntax"
    if role == "auditor_semantics":
        return "semantics"
    return None


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
            "- status: kanban stage — one of 'plan', 'execute', "
            "'audit_syntax', 'audit_semantics', 'ship', 'archive'. "
            "Legacy values (open/claimed/in_progress/blocked/done/cancelled) "
            "are translated to their kanban equivalent for back-compat.\n"
            "- owner: agent id ('coach', 'p1'..'p10'), or 'null' for unassigned\n"
            "Returns up to 100 most recent tasks. Each row shows stage, "
            "complexity (SIMPLE chip if simple), blocked flag, owner, "
            "priority, and title."
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
            # Translate legacy aliases for back-compat. The DB CHECK only
            # accepts kanban values now; passing 'in_progress' would
            # quietly return nothing without this.
            normalized = _normalize_status_alias(status)
            where_parts.append("status = ?")
            params.append(normalized)
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
                f"priority, complexity, blocked, blocked_reason, created_at "
                f"FROM tasks{clause} "
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
            parent = f" sub-of:{d['parent_id']}" if d["parent_id"] else ""
            simple = " SIMPLE" if d.get("complexity") == "simple" else ""
            blocked = ""
            if d.get("blocked"):
                reason = d.get("blocked_reason") or ""
                blocked = (
                    f" BLOCKED({reason})" if reason else " BLOCKED"
                )
            lines.append(
                f"{d['id']}  [{d['status']}]{simple}{blocked}  "
                f"owner={d['owner'] or '-'}  pri={d['priority']}  "
                f"{d['title']}{parent}"
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
            "- priority: 'low', 'normal', 'high', 'urgent' (default 'normal')\n"
            "- complexity: 'standard' (full pipeline) or 'simple' (skip audit + ship; "
            "executor self-audits and the board archives directly on commit). "
            "Default 'standard'. Coach-only — Players inherit the parent's complexity."
        ),
        {
            "title": str, "description": str, "parent_id": str,
            "priority": str, "complexity": str,
        },
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
        # Complexity: Coach can pick simple/standard; Players don't get
        # to flip an inherited subtask's complexity here (use
        # coord_set_task_complexity if needed — which is Coach-only).
        complexity_raw = (args.get("complexity") or "").strip().lower()
        if complexity_raw and complexity_raw not in ("simple", "standard"):
            return _err(
                f"invalid complexity '{complexity_raw}' "
                "(must be 'simple' or 'standard')"
            )
        if complexity_raw and not caller_is_coach:
            return _err(
                "Only Coach can set complexity at create time. "
                "Subtasks inherit; if you need this changed, ask Coach to "
                "call coord_set_task_complexity."
            )
        complexity = complexity_raw or "standard"

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

            # Subtask complexity inherits from parent unless Coach
            # explicitly overrode at create-time.
            if parent_id and not complexity_raw:
                cur = await c.execute(
                    "SELECT complexity FROM tasks WHERE id = ? AND project_id = ?",
                    (parent_id, project_id),
                )
                prow = await cur.fetchone()
                if prow:
                    parent_complexity = dict(prow).get("complexity") or "standard"
                    complexity = parent_complexity

            task_id = _new_task_id()
            await c.execute(
                "INSERT INTO tasks (id, project_id, title, description, parent_id, "
                "priority, complexity, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, project_id, title, description, parent_id,
                 priority, complexity, caller_id),
            )
            await c.commit()
        finally:
            await c.close()

        ts = _now_iso()
        await bus.publish(
            {
                "ts": ts,
                "agent_id": caller_id,
                "type": "task_created",
                "task_id": task_id,
                "title": title,
                "parent_id": parent_id,
                "priority": priority,
                "complexity": complexity,
            }
        )
        return _ok(
            f"Created task {task_id}"
            + (f" (subtask of {parent_id})" if parent_id else " (top-level)")
            + f", priority={priority}, complexity={complexity}"
        )

    @tool(
        "coord_claim_task",
        (
            "Claim a plan-stage task — sets you as its executor and moves "
            "it from `plan` → `execute`. Only Players can claim (Coach "
            "delegates, never executes). Fails if:\n"
            "  - task is not status=plan\n"
            "  - you're Coach\n"
            "  - you already own another task (finish or cancel it first)\n"
            "  - the task has an executor pool (eligible_owners) and you're "
            "    not in it\n"
            "  - the task is standard-complexity and has no spec yet "
            "    (Coach must call coord_write_task_spec first)\n"
            "Self-claim is one atomic step: claimed_at + started_at are "
            "both set to now() because claim IS starting from the Player's "
            "perspective."
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
            # Pre-checks: task exists in plan stage, has a spec (if standard),
            # and (if posted to a pool) the caller is eligible.
            cur = await c.execute(
                "SELECT status, owner, complexity, spec_path "
                "FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            existing = await cur.fetchone()
            if not existing:
                return _err(f"task {task_id} not found")
            ed = dict(existing)
            if ed["status"] != "plan":
                return _err(
                    f"task {task_id} is not claimable "
                    f"(status={ed['status']}, owner={ed['owner'] or '-'}). "
                    f"Only plan-stage tasks can be claimed."
                )
            if (
                ed["complexity"] == "standard"
                and not ed.get("spec_path")
            ):
                return _err(
                    f"task {task_id} has no spec — Coach must call "
                    f"coord_write_task_spec before this can move to execute. "
                    f"(Simple-complexity tasks skip this gate.)"
                )

            # Pool eligibility: if there's an active executor role row
            # with non-empty eligible_owners, the caller must be in the
            # list. If no active executor row exists yet (back-compat
            # path: Coach pushed via legacy coord_assign_task that didn't
            # write a row), allow the claim — production migrations
            # don't require pre-existing role rows.
            import json as _json
            cur = await c.execute(
                "SELECT id, eligible_owners, owner FROM task_role_assignments "
                "WHERE task_id = ? AND role = 'executor' "
                "AND completed_at IS NULL AND superseded_by IS NULL "
                "ORDER BY assigned_at DESC LIMIT 1",
                (task_id,),
            )
            role_row = await cur.fetchone()
            if role_row:
                rd = dict(role_row)
                if rd.get("owner") and rd["owner"] != caller_id:
                    return _err(
                        f"task {task_id} already has executor "
                        f"{rd['owner']} on the role-assignment row."
                    )
                eligible = []
                try:
                    eligible = _json.loads(rd.get("eligible_owners") or "[]")
                except Exception:
                    eligible = []
                if eligible and caller_id not in eligible:
                    return _err(
                        f"task {task_id} is posted to executors "
                        f"{eligible}; {caller_id} is not in the pool."
                    )

            # Atomic claim — race-safe via status='plan' guard. Both
            # claimed_at AND started_at are set to now (self-claim IS
            # starting; no separate "assigned but not picked up" window).
            now = _now_iso()
            cur = await c.execute(
                "UPDATE tasks SET owner = ?, status = 'execute', "
                "claimed_at = ?, started_at = ? "
                "WHERE id = ? AND status = 'plan' AND project_id = ? "
                "RETURNING id",
                (caller_id, now, now, task_id, project_id),
            )
            updated = await cur.fetchone()
            if not updated:
                # Race-loss path — someone else claimed between our pre-check
                # and the UPDATE. Re-read to give a precise error.
                cur = await c.execute(
                    "SELECT status, owner FROM tasks "
                    "WHERE id = ? AND project_id = ?",
                    (task_id, project_id),
                )
                current = await cur.fetchone()
                if not current:
                    return _err(f"task {task_id} not found")
                d = dict(current)
                return _err(
                    f"task {task_id} race-lost during claim "
                    f"(status={d['status']}, owner={d['owner'] or '-'})"
                )

            # Update or insert the executor role-assignment row. If
            # there's a posted-pool row, claim it; otherwise insert a
            # fresh hard-assign row so the audit trail is complete.
            if role_row:
                await c.execute(
                    "UPDATE task_role_assignments "
                    "SET owner = ?, claimed_at = ?, started_at = ? "
                    "WHERE id = ?",
                    (caller_id, now, now, dict(role_row)["id"]),
                )
            else:
                await c.execute(
                    "INSERT INTO task_role_assignments "
                    "(task_id, role, eligible_owners, owner, "
                    "assigned_at, claimed_at, started_at) "
                    "VALUES (?, 'executor', '[]', ?, ?, ?, ?)",
                    (task_id, caller_id, now, now, now),
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
            "Update a task's kanban stage. Valid transitions:\n"
            "  plan → execute, archive\n"
            "  execute → audit_syntax, archive\n"
            "  audit_syntax → audit_semantics, execute\n"
            "  audit_semantics → ship, execute\n"
            "  ship → archive\n"
            "  archive: terminal\n"
            "\n"
            "For most transitions you should NOT call this tool directly — "
            "the kanban subscriber auto-advances on the right events "
            "(coord_commit_push, coord_submit_audit_report, "
            "coord_mark_shipped). Use this for cancellation (any stage → "
            "archive with note) or other manual one-off moves.\n"
            "\n"
            "Legacy aliases accepted for one release: 'open'→'plan', "
            "'claimed'/'in_progress'/'blocked'→'execute', 'done'→'archive', "
            "'cancelled'→'archive' (sets cancelled_at).\n"
            "\n"
            "Only the current owner can update the task; Coach can also "
            "cancel any task. When a task moves to archive, the owner's "
            "current_task_id is cleared. Optional 'note' is logged."
        ),
        {"task_id": str, "status": str, "note": str},
    )
    async def update_task(args: dict[str, Any]) -> dict[str, Any]:
        task_id = (args.get("task_id") or "").strip()
        raw_status = (args.get("status") or "").strip().lower()
        note = args.get("note") or ""
        if not task_id:
            return _err("task_id is required")
        # Cancellation has a side-effect: the original input is preserved
        # so we can stamp cancelled_at iff the caller asked for "cancelled"
        # specifically, vs. a clean "archive" move (delivery).
        was_cancellation = raw_status == "cancelled"
        new_status = _normalize_status_alias(raw_status)
        if new_status not in ALL_KANBAN_STAGES:
            return _err(
                f"invalid status '{raw_status}' (must be one of "
                f"{sorted(ALL_KANBAN_STAGES)} or a legacy alias)"
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
            is_archive_move = new_status == "archive"
            if current_owner is None:
                # No owner yet — only Coach can touch.
                if not caller_is_coach:
                    return _err(
                        f"task {task_id} has no owner; only Coach can "
                        f"change a plan-stage task's status."
                    )
            elif current_owner != caller_id:
                # Owned by someone else. Only Coach can cancel/archive.
                if not (caller_is_coach and is_archive_move):
                    return _err(
                        f"only the task's owner ({current_owner}) can "
                        f"update it. Coach can additionally archive any task."
                    )

            if not _valid_transition(old_status, new_status):
                return _err(
                    f"invalid transition: {old_status} → {new_status}"
                )

            # Role-completion gate (Docs/kanban-specs.md §2.3). Rejects
            # manual transitions that should be event-driven (commit /
            # audit / ship). Coach uses coord_advance_task_stage to
            # force; cancellation paths skip the gate.
            gate_err = await _check_kanban_role_gate(
                c,
                project_id,
                task_id,
                old_status,
                new_status,
                was_cancellation=was_cancellation,
            )
            if gate_err is not None:
                return _err(gate_err)

            now = _now_iso()
            if is_archive_move:
                if was_cancellation:
                    # Distinguish cancellation from delivery — both land
                    # in archive but the archive view's "show cancelled"
                    # toggle keys on cancelled_at.
                    await c.execute(
                        "UPDATE tasks SET status = 'archive', "
                        "completed_at = ?, archived_at = ?, cancelled_at = ? "
                        "WHERE id = ? AND project_id = ?",
                        (now, now, now, task_id, project_id),
                    )
                else:
                    await c.execute(
                        "UPDATE tasks SET status = 'archive', "
                        "completed_at = ?, archived_at = ? "
                        "WHERE id = ? AND project_id = ?",
                        (now, now, task_id, project_id),
                    )
                # Free the executor.
                if current_owner is not None:
                    await c.execute(
                        "UPDATE agents SET current_task_id = NULL "
                        "WHERE id = ? AND current_task_id = ?",
                        (current_owner, task_id),
                    )
            else:
                await c.execute(
                    "UPDATE tasks SET status = ? "
                    "WHERE id = ? AND project_id = ?",
                    (new_status, task_id, project_id),
                )
            await c.commit()
        finally:
            await c.close()

        # Emit both the kanban-shaped event AND the legacy back-compat
        # event so downstream listeners that haven't migrated still work.
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_stage_changed",
                "task_id": task_id,
                "from": old_status,
                "to": new_status,
                "reason": "manual",
                "note": note,
                "owner": current_owner,
            }
        )
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_updated",
                "task_id": task_id,
                "old_status": old_status,
                "new_status": new_status,
                "note": note,
                "owner": current_owner,
            }
        )
        # Notify the creator when work they handed off finishes (or is
        # cancelled). Same heuristic as before — skip self-notify and
        # human-creator. The "blocked" branch is gone (blocked is now
        # an orthogonal flag, not a status); a Player blocking a task
        # should call coord_set_task_blocked, which has its own event.
        if (
            is_archive_move
            and created_by
            and created_by != caller_id
            and created_by != "human"
        ):
            try:
                from server.agents import _deliver_system_message
                verb = "cancelled" if was_cancellation else "finished"
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
            "Coach-only. Assign a plan-stage task as the EXECUTOR.\n"
            "Params:\n"
            "- task_id: the task to assign (required)\n"
            "- to: either a single Player slot id (e.g. 'p3') for hard-assign, "
            "OR a comma-separated list (e.g. 'p1,p2,p3') to post the task to "
            "an executor pool — the first eligible Player to call "
            "coord_claim_task wins via atomic UPDATE.\n"
            "\n"
            "Hard-assign: task moves plan→execute immediately, owner is set, "
            "the assignee is auto-woken. Pool: task stays in `plan` with the "
            "eligible_owners list set on a task_role_assignments row; all "
            "eligible Players are auto-woken with an 'available task' prompt.\n"
            "\n"
            "Standard-complexity tasks must have a spec.md before they can "
            "move to execute (write one with coord_write_task_spec, or "
            "delegate via coord_assign_planner). Simple-complexity tasks "
            "skip the spec gate.\n"
            "\n"
            "Fails if: you're a Player (Players report, don't assign), the "
            "task isn't status=plan, the target isn't a valid Player slot, "
            "the spec gate trips for a standard task, or (hard-assign) the "
            "Player already owns another task."
        ),
        {"task_id": str, "to": str},
    )
    async def assign_task(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach can push-assign tasks. Players report and claim "
                "plan-stage tasks themselves via coord_claim_task."
            )
        task_id = (args.get("task_id") or "").strip()
        to_raw = (args.get("to") or "").strip()
        if not task_id:
            return _err("task_id is required")
        if not to_raw:
            return _err("'to' is required (Player slot id, or comma-list for pool)")

        # Parse `to`: comma → pool; otherwise single hard-assign.
        if "," in to_raw:
            pool = [p.strip().lower() for p in to_raw.split(",") if p.strip()]
        else:
            pool = [to_raw.lower()]
        if not pool:
            return _err("'to' resolved to an empty list of Players")
        for slot in pool:
            if slot in ("coach", "broadcast"):
                return _err(
                    f"can only assign to Players (p1..p10), not {slot!r}"
                )
            if slot not in VALID_RECIPIENTS:
                return _err(f"invalid target '{slot}' — must be p1..p10")
        # Dedupe + locked check.
        seen: set[str] = set()
        deduped: list[str] = []
        for slot in pool:
            if slot in seen:
                continue
            seen.add(slot)
            if await _is_locked(slot):
                return _err(
                    f"Player {slot} is locked (human marked them off-limits "
                    f"for Coach orchestration). Pick unlocked Players."
                )
            deduped.append(slot)
        is_pool = len(deduped) > 1

        c = await configured_conn()
        try:
            project_id = await resolve_active_project()
            # Read the task: must be in plan, must have spec if standard.
            cur = await c.execute(
                "SELECT status, owner, complexity, spec_path "
                "FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            existing = await cur.fetchone()
            if not existing:
                return _err(f"task {task_id} not found")
            ed = dict(existing)
            if ed["status"] != "plan":
                return _err(
                    f"task {task_id} is not in plan stage "
                    f"(status={ed['status']}, owner={ed['owner'] or '-'}). "
                    f"Use coord_advance_task_stage to force a transition, "
                    f"or send a message if you want to nudge an in-flight task."
                )
            if (
                ed["complexity"] == "standard"
                and not ed.get("spec_path")
            ):
                return _err(
                    f"task {task_id} has no spec — write one with "
                    f"coord_write_task_spec or delegate via "
                    f"coord_assign_planner before assigning the executor. "
                    f"(Simple-complexity tasks skip this gate.)"
                )

            now = _now_iso()
            import json as _json

            if is_pool:
                # Pool form: leave tasks.owner NULL, status stays `plan`,
                # but record the eligible_owners on a fresh executor
                # role-assignment row. The first eligible Player to
                # call coord_claim_task wins.
                eligible_json = _json.dumps(deduped)
                # Don't preemptively reject if some pool members are
                # busy — the auto-wake will still fire for everyone in
                # the list and busy Players will simply ignore it. The
                # idle poller picks up posted-pool work later for any
                # Player that becomes free.
                await c.execute(
                    "INSERT INTO task_role_assignments "
                    "(task_id, role, eligible_owners, owner, assigned_at) "
                    "VALUES (?, 'executor', ?, NULL, ?)",
                    (task_id, eligible_json, now),
                )
                await c.commit()
            else:
                # Hard-assign: single Player.
                slot = deduped[0]
                # Target Player must exist and be free.
                cur = await c.execute(
                    "SELECT current_task_id FROM agents WHERE id = ?", (slot,)
                )
                row = await cur.fetchone()
                if not row:
                    return _err(f"Player '{slot}' not found")
                busy_with = dict(row)["current_task_id"]
                if busy_with:
                    return _err(
                        f"Player {slot} already owns task {busy_with}; cancel "
                        f"or complete it before reassigning."
                    )
                # Atomic transition plan → execute. status='plan' guard
                # is race-safe.
                cur = await c.execute(
                    "UPDATE tasks SET owner = ?, status = 'execute', "
                    "claimed_at = ?, started_at = NULL "
                    "WHERE id = ? AND status = 'plan' "
                    "AND project_id = ? RETURNING id",
                    (slot, now, task_id, project_id),
                )
                updated = await cur.fetchone()
                if not updated:
                    cur = await c.execute(
                        "SELECT status, owner FROM tasks "
                        "WHERE id = ? AND project_id = ?",
                        (task_id, project_id),
                    )
                    current = await cur.fetchone()
                    if not current:
                        return _err(f"task {task_id} not found")
                    d = dict(current)
                    return _err(
                        f"task {task_id} race-lost during assign "
                        f"(status={d['status']}, owner={d['owner'] or '-'})"
                    )
                await c.execute(
                    "INSERT INTO task_role_assignments "
                    "(task_id, role, eligible_owners, owner, "
                    "assigned_at, claimed_at) "
                    "VALUES (?, 'executor', '[]', ?, ?, ?)",
                    (task_id, slot, now, now),
                )
                await c.execute(
                    "UPDATE agents SET current_task_id = ? WHERE id = ?",
                    (task_id, slot),
                )
                await c.commit()
        finally:
            await c.close()

        # Read task complexity again for the wake prompt branching.
        # Cheap second-read is fine; the bus.publish below already
        # closed our connection.
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT complexity FROM tasks WHERE id = ?", (task_id,)
            )
            crow = await cur.fetchone()
            complexity = (dict(crow)["complexity"] if crow else "standard")
        finally:
            await c.close()

        # Emit task_role_assigned (kanban event family). The legacy
        # task_assigned is also emitted for back-compat; the kanban
        # subscriber listens for both.
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_role_assigned",
                "task_id": task_id,
                "role": "executor",
                "eligible_owners": deduped,
                "owner": (None if is_pool else deduped[0]),
                "to": (deduped[0] if not is_pool else None),
            }
        )
        if not is_pool:
            await bus.publish(
                {
                    "ts": _now_iso(),
                    "agent_id": caller_id,
                    "type": "task_assigned",
                    "task_id": task_id,
                    "to": deduped[0],
                }
            )
        # Auto-wake. Late import to avoid the tools↔agents circular dep.
        try:
            from server.agents import maybe_wake_agent
            simple_hint = ""
            if complexity == "simple":
                simple_hint = (
                    " This task is marked SIMPLE — self-audit (run tests / "
                    "sanity-check the change) before coord_commit_push because "
                    "the board archives directly on commit, no separate audit "
                    "pass."
                )
            if is_pool:
                pool_str = ", ".join(deduped)
                wake_prompt = (
                    f"Coach posted task {task_id} to a pool of {pool_str}. "
                    f"Read the spec + decide if you want to take it; first "
                    f"to call coord_claim_task wins.{simple_hint}"
                )
                for slot in deduped:
                    try:
                        await maybe_wake_agent(
                            slot, wake_prompt, bypass_debounce=True
                        )
                    except Exception:
                        pass
            else:
                slot = deduped[0]
                wake_prompt = (
                    f"Coach assigned you task {task_id} as executor "
                    f"(status=execute, owner={slot}). Read spec.md from "
                    f"the task folder, do the work, then call "
                    f"coord_commit_push(task_id={task_id!r}, ...) when "
                    f"the work is in.{simple_hint}"
                )
                await maybe_wake_agent(slot, wake_prompt, bypass_debounce=True)
        except Exception:
            pass
        if is_pool:
            return _ok(
                f"posted {task_id} to executor pool: {', '.join(deduped)}"
            )
        return _ok(f"assigned {task_id} → {deduped[0]}")

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

        # Body cap — keep events from carrying multi-MB tool dumps
        # while still showing the user enough to read the message in
        # context. The full text always lives in the `messages` table
        # and surfaces in EnvPane → Inbox; this preview is just the
        # in-pane render. 4000 chars covers practically every agent
        # message without bloating the events stream.
        body_preview = body[:4000]
        body_truncated = len(body) > 4000
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "message_sent",
                "message_id": msg_id,
                "to": to,
                "subject": subject,
                "body_preview": body_preview,
                "body_full_len": len(body),
                "body_truncated": body_truncated,
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
            "- task_id: the kanban task this commit is delivering against "
            "(optional but STRONGLY RECOMMENDED). When provided, the "
            "kanban subscriber sees `commit_pushed` with task_id and "
            "auto-advances the task: standard → audit_syntax, simple → "
            "archive. Without task_id the commit still works but the "
            "kanban board doesn't move (Coach has to advance manually).\n"
            "Returns 'nothing to commit' as a soft-OK if the working tree "
            "is clean. Requires HARNESS_PROJECT_REPO to be configured; "
            "push also needs pushable credentials (typically a PAT "
            "embedded in the project repo URL)."
        ),
        {"message": str, "push": str, "task_id": str},
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

        # Optional task_id — empty string treated as None.
        task_id_raw = (args.get("task_id") or "").strip()
        task_id_in: str | None = task_id_raw or None

        # Bind task_id to the caller's executor role at entry — before
        # we run any git work — so a Player can't pass another Player's
        # task_id (or a stale id) and ride it into the kanban
        # subscriber. Validation: task is in the active project, sits
        # in `execute`, has `owner=caller_id`, and an active executor
        # role assignment owned by caller exists with completed_at NULL.
        if task_id_in:
            project_id = await resolve_active_project()
            c = await configured_conn()
            try:
                cur = await c.execute(
                    "SELECT t.status, t.owner, "
                    "(SELECT 1 FROM task_role_assignments "
                    " WHERE task_id = t.id AND role = 'executor' "
                    " AND owner = ? AND completed_at IS NULL "
                    " AND superseded_by IS NULL "
                    " ORDER BY assigned_at DESC LIMIT 1) AS has_role "
                    "FROM tasks t "
                    "WHERE t.id = ? AND t.project_id = ?",
                    (caller_id, task_id_in, project_id),
                )
                row = await cur.fetchone()
            finally:
                await c.close()
            if row is None:
                return _err(
                    f"task {task_id_in} not found in the active project. "
                    f"Pass a valid task_id from your active executor "
                    f"assignment, or omit task_id to commit without "
                    f"driving the kanban."
                )
            t = dict(row)
            if t.get("status") != "execute":
                return _err(
                    f"task {task_id_in} is in stage "
                    f"'{t.get('status')}', not 'execute'; "
                    f"coord_commit_push can only deliver against an "
                    f"executor task currently in execute."
                )
            if t.get("owner") != caller_id:
                return _err(
                    f"task {task_id_in} is owned by "
                    f"{t.get('owner') or 'no one'}, not {caller_id}. "
                    f"You can only call coord_commit_push for your own "
                    f"executor task."
                )
            if not t.get("has_role"):
                return _err(
                    f"task {task_id_in} has no active uncompleted "
                    f"executor role for {caller_id}. The role may have "
                    f"been superseded by a re-assignment, or already "
                    f"completed by a prior commit."
                )

        cwd = workspace_dir(caller_id)
        if not (cwd / ".git").exists():
            return _err(
                f"worktree at {cwd} is not a git checkout — something "
                "went wrong during workspace provisioning. Check "
                "/api/status workspaces section."
            )

        # Env scrub for git subprocesses — git can run worktree-supplied
        # hooks (.git/hooks/pre-commit, etc.) which an agent could plant
        # to dump inherited env. Build a clean env so HARNESS_TOKEN /
        # secret material isn't readable from a hook. Push auth uses
        # the PAT embedded in the remote URL (see _expand_placeholders
        # in workspaces.py), so we don't need GITHUB_TOKEN in env.
        from server.agent_env import build_clean_agent_env
        clean_env = build_clean_agent_env()

        async def run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
            def _do() -> tuple[int, str, str]:
                p = subprocess.run(
                    cmd,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=clean_env,
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

        # Auto-advance only when the push actually succeeded (or the
        # caller explicitly asked for local-only via push=false — that
        # is the documented escape hatch). A failed push must not
        # drive the kanban; otherwise a Player whose creds are broken
        # could ride a local-only commit into archive.
        push_failed = do_push and not pushed_ok
        kanban_task_id = None if push_failed else task_id_in

        # Mark the executor role-assignment row complete only when the
        # commit is going to drive auto-advance. Entry validation
        # guarantees the UPDATE will match exactly one row when
        # task_id_in is set.
        if kanban_task_id:
            c = await configured_conn()
            try:
                await c.execute(
                    "UPDATE task_role_assignments "
                    "SET completed_at = ? "
                    "WHERE task_id = ? AND role = 'executor' "
                    "AND owner = ? AND completed_at IS NULL "
                    "AND superseded_by IS NULL",
                    (_now_iso(), kanban_task_id, caller_id),
                )
                await c.commit()
            finally:
                await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "commit_pushed",
                "sha": sha,
                "message": message,
                "pushed": pushed_ok,
                "push_requested": do_push,
                # task_id drives the kanban auto-advance subscriber.
                # Cleared on push failure so a broken push can't ride a
                # local-only commit into the audit pipeline. The
                # original task_id is preserved on the row above for
                # the explicit `push=false` mode.
                "task_id": kanban_task_id,
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

    # Coach proposes edits to harness-managed files (truth/* and the
    # per-project CLAUDE.md) via `coord_propose_file_write` below; the
    # human reviews a diff and approves in the UI. The global
    # /data/CLAUDE.md is NOT proposeable — only the user edits the
    # harness-wide instructions. All these files are read fresh on
    # every agent turn (server/context.py).

    @tool(
        "coord_propose_file_write",
        (
            "Coach-only. Propose a write to a harness-managed file in "
            "the **currently active** project. The user reviews a diff "
            "and approves/denies in the UI's 'File-write proposals' "
            "section; on approve the harness writes the file. To "
            "target a different project, ask the user to switch the "
            "active project first.\n"
            "\n"
            "Two scopes:\n"
            "  - 'truth': `path` is relative under the active project's "
            "    `truth/` folder (e.g. 'specs.md', 'brand/colors.md'). "
            "    truth/ is the user's signed-off source-of-truth "
            "    (specs, brand guidelines, contracts); agents NEVER "
            "    write to it directly — the harness's PreToolUse guard "
            "    hook denies any direct Write/Edit/Bash to truth/.\n"
            "  - 'project_claude_md': `path` must be exactly 'CLAUDE.md'. "
            "    Targets `/data/projects/<active-slug>/CLAUDE.md` — the "
            "    project's instruction file, read fresh into every "
            "    agent's system prompt. Use this to keep the project's "
            "    Goal / Stakeholders / Team / Glossary / Conventions / "
            "    truth-section paragraphs current.\n"
            "\n"
            "Players cannot call this tool — they must ask Coach to "
            "relay. The global /data/CLAUDE.md (harness-wide) is NOT a "
            "valid scope; only the user edits that file directly.\n"
            "\n"
            "What happens:\n"
            "  1. The proposal is queued (status=pending).\n"
            "  2. ANY prior pending proposal for the same (scope, "
            "path) is auto-superseded — only your latest proposal is "
            "offered to the user for approval. So your new proposal "
            "must include EVERY change you still want for that file "
            "(the prior proposal's content is discarded). If unsure "
            "what's currently pending, look for "
            "`file_write_proposal_*` events in your timeline before "
            "composing.\n"
            "  3. The user reviews the diff in the UI and clicks "
            "approve or deny.\n"
            "  4. On approve, the harness writes the file with the "
            "proposed content. On deny, the file is left as-is.\n"
            "  5. A `file_write_proposal_resolved` event fires so "
            "you'll see the outcome on your next turn.\n"
            "\n"
            "Reorganizing the truth folder (e.g. splitting a growing "
            "specs.md into specs.md + architecture.md + scope.md): "
            "send the changes as a SERIES of proposals — (1) the new "
            "dependency files with their content, (2) the original "
            "file with extracted content removed, (3) "
            "`truth-index.md` updated to list the new files. Each is "
            "a separate call; the user approves them in order.\n"
            "\n"
            "Params:\n"
            "- scope: 'truth' or 'project_claude_md' (required).\n"
            "- path: scope-relative path. For 'truth': relative under "
            "  the active project's truth/ (e.g. 'specs.md' or "
            "  'brand/colors.md'; NOT 'projects/<slug>/...' or "
            "  '/data/...'). For 'project_claude_md': must be exactly "
            "  'CLAUDE.md'. Required.\n"
            "- content: full new file body (required). This is a full "
            "  REPLACE — include the parts you're keeping verbatim, "
            "  not just a diff. The user reviews a diff against the "
            "  current file content in the UI.\n"
            "- summary: one-line 'why' the user reads next to "
            "  approve/deny (required, ≤ 200 chars). Be specific: "
            "  'Add launch-date constraint to specs.md §3' beats "
            "  'update specs'."
        ),
        {"scope": str, "path": str, "content": str, "summary": str},
    )
    async def propose_file_write(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach can propose file writes. Players: send a "
                "coord_send_message to coach describing the proposed "
                "change; Coach will relay it as a proposal."
            )
        scope = (args.get("scope") or "").strip()
        rel = (args.get("path") or "").strip()
        content = args.get("content")
        summary = (args.get("summary") or "").strip()

        if scope not in ("truth", "project_claude_md"):
            return _err(
                f"scope must be 'truth' or 'project_claude_md' (got "
                f"{scope!r}). The global /data/CLAUDE.md is not a "
                "valid scope."
            )
        if not rel:
            return _err("path is required")

        if scope == "truth":
            # Defensive strip: Coach is told to pass paths relative to
            # truth/ (e.g. "specs.md", not "truth/specs.md"), but
            # accept the prefixed form too rather than fail
            # confusingly. Strip a single leading "truth/" so
            # "truth/specs.md" and "specs.md" both resolve to
            # /data/projects/<slug>/truth/specs.md.
            if rel.startswith("truth/"):
                rel = rel[len("truth/"):]
            if rel.startswith("/") or ".." in rel.split("/"):
                return _err(
                    "path must be relative under the active project's "
                    "truth/ folder, no leading slash, no '..' segments"
                )
            # Catch a recurrent Coach mistake: encoding the target
            # project slug in the path (e.g.
            # "projects/dynamichypergraph/CLAUDE.md"). truth/ is
            # rooted at the active project — there's no way to
            # cross-project from the path. Detect when the first
            # segment matches an existing project slug and tell Coach
            # to switch active project instead. Also catches a literal
            # "projects/" prefix even when the second segment isn't a
            # known slug.
            first_seg = rel.split("/", 1)[0]
            if first_seg == "projects":
                return _err(
                    f"path '{rel}' starts with 'projects/' — truth/ "
                    "is rooted at the active project's truth folder, "
                    "not anywhere under /data/projects/. To target a "
                    "different project, ask the user to switch the "
                    "active project first, then pass the path "
                    "relative to that project's truth/ (e.g. "
                    "'CLAUDE.md', not 'projects/<slug>/CLAUDE.md')."
                )
            c_slug_check = await configured_conn()
            try:
                cur = await c_slug_check.execute(
                    "SELECT 1 FROM projects WHERE id = ? LIMIT 1",
                    (first_seg,),
                )
                slug_match = await cur.fetchone()
            finally:
                await c_slug_check.close()
            if slug_match:
                return _err(
                    f"path '{rel}' starts with project slug "
                    f"'{first_seg}' — the path is rooted at the "
                    "*currently active* project's truth/ folder, not "
                    "anywhere under /data/projects/. To target the "
                    f"'{first_seg}' project, ask the user to switch "
                    "active project to it first, then pass the path "
                    "relative to its truth/ (e.g. drop the leading "
                    f"'{first_seg}/')."
                )
        else:
            # project_claude_md: the only legal path is the project's
            # CLAUDE.md at the project root. Reject anything else so a
            # malformed call can't write to a sibling file.
            if rel != "CLAUDE.md":
                return _err(
                    f"for scope 'project_claude_md', path must be "
                    f"exactly 'CLAUDE.md' (got {rel!r}). The target "
                    "is the active project's "
                    "/data/projects/<slug>/CLAUDE.md."
                )

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
        # Supersede + insert in one transaction so a crash mid-flight
        # leaves the table coherent (either both old superseded + new
        # pending, or no change at all). Auto-supersede guarantees the
        # invariant "at most one pending proposal per (project, scope,
        # path)" so the EnvPane never shows a stale stack of
        # duplicates. The scope filter is load-bearing: a hypothetical
        # truth/CLAUDE.md and a project_claude_md proposal at path
        # 'CLAUDE.md' must not supersede each other.
        now_iso = _now_iso()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id FROM file_write_proposals "
                "WHERE project_id = ? AND scope = ? AND path = ? "
                "AND status = 'pending'",
                (project_id, scope, rel),
            )
            superseded_ids = [row[0] for row in await cur.fetchall()]
            cur = await c.execute(
                "INSERT INTO file_write_proposals "
                "(project_id, proposer_id, scope, path, "
                "proposed_content, summary) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, caller_id, scope, rel, content, summary),
            )
            proposal_id = cur.lastrowid
            for sid in superseded_ids:
                await c.execute(
                    "UPDATE file_write_proposals SET status = 'superseded', "
                    "resolved_at = ?, resolved_by = 'system', "
                    "resolved_note = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (now_iso, f"superseded by #{proposal_id}", sid),
                )
            await c.commit()
        finally:
            await c.close()

        for sid in superseded_ids:
            await bus.publish(
                {
                    "ts": now_iso,
                    "agent_id": "system",
                    "type": "file_write_proposal_superseded",
                    "proposal_id": sid,
                    "superseded_by": proposal_id,
                    "scope": scope,
                    "path": rel,
                }
            )
        await bus.publish(
            {
                "ts": now_iso,
                "agent_id": caller_id,
                "type": "file_write_proposal_created",
                "proposal_id": proposal_id,
                "scope": scope,
                "path": rel,
                "summary": summary,
                "size": len(content),
                "superseded": superseded_ids,
            }
        )
        if superseded_ids:
            note = (
                f" (superseded {len(superseded_ids)} prior pending "
                f"proposal{'s' if len(superseded_ids) > 1 else ''} for "
                f"the same path: #{', #'.join(str(i) for i in superseded_ids)})"
            )
        else:
            note = ""
        if scope == "truth":
            display_path = f"truth/{rel}"
        else:
            display_path = "CLAUDE.md"
        return _ok(
            f"proposal #{proposal_id} queued for {display_path} "
            f"({len(content)} chars, scope={scope}){note}. The user "
            f"will review and approve or deny in the UI; you'll see "
            f"`file_write_proposal_resolved` on your next turn."
        )

    @tool(
        "coord_read_file",
        (
            "Read a text file from the **currently active** project's "
            "tree at `/data/projects/<active-slug>/`. Available to all "
            "agents (Coach AND Players) — the read path bypasses the "
            "Codex sandbox / bwrap layer that breaks `shell cat` in "
            "nested-container deploys, so this is the canonical way "
            "for any agent to inspect project files at any time, "
            "even when their native read tool is unavailable.\n"
            "\n"
            "Path is RELATIVE to the active project's root. Examples: "
            "`'CLAUDE.md'`, `'truth/specs.md'`, `'decisions/0001-foo.md'`, "
            "`'working/knowledge/notes.md'`, `'outputs/report.md'`. "
            "Absolute paths and `..` segments are rejected.\n"
            "\n"
            "Project CLAUDE.md is also auto-injected into your system "
            "prompt every turn — calling this for `CLAUDE.md` is fine "
            "but redundant for read-only inspection. Use it when you "
            "want the latest body during a long turn (the system-"
            "prompt copy is frozen at turn start).\n"
            "\n"
            "Limits:\n"
            "- Returns up to 200 KB of file content; larger files are "
            "  rejected with a size error (use `coord_list_knowledge` / "
            "  the Files pane for an index of large trees).\n"
            "- Text-only: files that aren't valid UTF-8 are rejected. "
            "  Binary deliverables under `outputs/` cannot be read "
            "  through this tool.\n"
            "\n"
            "Params:\n"
            "- path: relative path under the active project root "
            "  (required, no leading slash, no `..`)."
        ),
        {"path": str},
    )
    async def read_file(args: dict[str, Any]) -> dict[str, Any]:
        rel = (args.get("path") or "").strip()
        if not rel:
            return _err("path is required")
        if rel.startswith("/"):
            return _err(
                "path must be relative under the active project's "
                "root (no leading slash)"
            )
        if ".." in rel.split("/"):
            return _err("path must not contain '..' segments")
        # Anchor + re-validate so a clever path that resolves outside
        # the project root (symlink, weird casing) is rejected even
        # if the literal-segment check above passes.
        from server.paths import project_paths
        project_id = await resolve_active_project()
        project_root = project_paths(project_id).root.resolve()
        target = (project_root / rel).resolve()
        try:
            target.relative_to(project_root)
        except ValueError:
            return _err(
                f"resolved path '{rel}' escapes the active project's "
                "root — refusing read"
            )
        if not target.exists():
            return _err(f"file not found: {rel}")
        if not target.is_file():
            return _err(f"not a regular file: {rel}")
        try:
            size = target.stat().st_size
        except OSError as e:
            return _err(f"stat failed: {e}")
        if size > 200_000:
            return _err(
                f"file too large ({size} chars, max 200000); use the "
                "Files pane or chunk via Read tool / shell"
            )
        try:
            body = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _err(
                f"file is not valid UTF-8: {rel} (binary files cannot "
                "be read through this tool)"
            )
        except OSError as e:
            return _err(f"read failed: {e}")
        return _ok(
            f"file[{rel}] ({len(body)} chars):\n\n{body}"
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
        "coord_set_player_runtime",
        (
            "Coach-only. Flip a Player between the Claude and Codex "
            "runtimes WITH session transfer. The Player's current "
            "session (if any) is summarized via /compact on the source "
            "runtime, the runtime then flips, and the next turn on the "
            "new runtime reads that summary as a handoff in its system "
            "prompt. Continuity preserved across the flip without "
            "mid-conversation memory loss.\n"
            "\n"
            "Stored on `agents.runtime_override`; resolution at spawn "
            "time is per-slot override → role default in team_config → "
            "'claude'.\n"
            "\n"
            "Use this BEFORE coord_set_player_model when you want to set "
            "a model from the other runtime family — the model tool "
            "validates against the Player's currently-resolved runtime, "
            "so a Codex model on a Claude-runtime Player is rejected "
            "until you flip the runtime here first.\n"
            "\n"
            "An existing `model_override` is preserved across the flip "
            "(spawn-time silently drops it if it doesn't fit the new "
            "runtime, then re-applies it if you flip back). Set the "
            "new model explicitly via coord_set_player_model after "
            "flipping if you don't want fall-through to the role default.\n"
            "\n"
            "Codex requires the HARNESS_CODEX_ENABLED env flag — without "
            "it, runtime='codex' is rejected. Mid-turn flips are also "
            "rejected (the in-flight turn would be on the old runtime "
            "while subsequent turns use the new one); cancel the Player "
            "first if they're working.\n"
            "\n"
            "Behavior:\n"
            "- runtime equals current → no-op (returns ok with note='noop').\n"
            "- no prior session on the source runtime → flip is "
            "immediate; one 'runtime_updated' + one 'session_transferred' "
            "event with note='no_prior_session'.\n"
            "- has a prior session → a compact turn is queued on the "
            "current runtime. The runtime flips on compact success and "
            "'session_transferred' fires; if the compact returns no "
            "summary 'session_transfer_failed' fires and the runtime "
            "stays put. The MCP call returns IMMEDIATELY (queued=True) — "
            "watch the Player's pane for completion.\n"
            "- runtime='' (empty string) keeps the legacy blunt clear: "
            "writes runtime_override=NULL and emits 'runtime_updated'. "
            "No transfer/compact is run; use this only when you "
            "explicitly want a fresh start on the role default.\n"
            "\n"
            "Params:\n"
            "- player_id: one of p1..p10 (required; cannot flip Coach's runtime)\n"
            "- runtime: 'claude', 'codex', or empty string to clear "
            "(revert to the role default; blunt — no transfer)."
        ),
        {"player_id": str, "runtime": str},
    )
    async def set_player_runtime(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach flips Player runtimes.")
        pid = (args.get("player_id") or "").strip()
        raw = (args.get("runtime") or "").strip().lower()
        if not re.fullmatch(r"p([1-9]|10)", pid):
            return _err(
                f"invalid player_id '{pid}' — expected p1..p10 "
                "(cannot flip Coach's runtime via MCP; ask the human)"
            )
        if raw == "":
            runtime_value: str | None = None
        elif raw in ("claude", "codex"):
            runtime_value = raw
        else:
            return _err(
                f"invalid runtime '{args.get('runtime')}' — must be "
                "'claude', 'codex', or empty (clear)"
            )

        if runtime_value == "codex":
            from server.runtimes import is_codex_enabled
            if not is_codex_enabled():
                return _err(
                    "Codex runtime is gated behind HARNESS_CODEX_ENABLED. "
                    "The human must set that env var on the deployment "
                    "before any Player can run on Codex. Use "
                    "coord_request_human to ask."
                )

        # Mid-turn flip rejection mirrors PUT /api/agents/{id}/runtime —
        # an in-flight turn would be on the old runtime while subsequent
        # turns use the new one, leaving the timeline incoherent.
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status FROM agents WHERE id = ?", (pid,)
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"player '{pid}' not found")
            current_status = dict(row).get("status")
            if current_status == "working":
                return _err(
                    f"{pid} is mid-turn — cancel their turn first (or "
                    "wait for it to finish), then flip the runtime."
                )
        finally:
            await c.close()

        # Empty/clear path — keep the legacy blunt behavior. Coach asks
        # for "clear" when they explicitly want a fresh start with no
        # transfer (revert to role default + drop any continuity).
        if runtime_value is None:
            from server.agents import _set_runtime_override
            await _set_runtime_override(pid, None)
            await bus.publish(
                {
                    "ts": _now_iso(),
                    "agent_id": pid,
                    "type": "runtime_updated",
                    "player_id": pid,
                    "runtime_override": None,
                }
            )
            return _ok(f"{pid} runtime cleared (will use role default)")

        # Transfer path. Re-resolve the current runtime now that we
        # know we're moving to a concrete target.
        from server.agents import (
            _resolve_runtime_for,
            _get_session_id,
            _set_runtime_override,
            run_agent as _run_agent,
            COMPACT_PROMPT as _COMPACT_PROMPT,
        )
        from_runtime = await _resolve_runtime_for(pid)
        if from_runtime == runtime_value:
            return _ok(
                f"{pid} runtime is already {runtime_value} — no flip needed"
            )

        if from_runtime == "claude":
            prior = await _get_session_id(pid)
        else:
            from server.runtimes.codex import _get_codex_thread_id
            prior = await _get_codex_thread_id(pid)

        if not prior:
            # No prior session — flip immediately, emit the same event
            # pair the HTTP endpoint produces.
            await _set_runtime_override(pid, runtime_value)
            ts_iso = _now_iso()
            await bus.publish(
                {
                    "ts": ts_iso,
                    "agent_id": pid,
                    "type": "runtime_updated",
                    "player_id": pid,
                    "runtime_override": runtime_value,
                }
            )
            await bus.publish(
                {
                    "ts": ts_iso,
                    "agent_id": pid,
                    "type": "session_transferred",
                    "from_runtime": from_runtime,
                    "to_runtime": runtime_value,
                    "note": "no_prior_session",
                }
            )
            return _ok(
                f"{pid} runtime → {runtime_value} (no prior session — "
                "flipped directly)"
            )

        # Schedule the transfer-mode compact. The message handler in
        # the source runtime applies the flip on success. We use
        # asyncio.create_task here (not BackgroundTasks) because tool
        # callbacks have no FastAPI request context — the run_agent
        # coroutine self-cleans even when its parent task isn't awaited.
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": pid,
                "type": "session_transfer_requested",
                "from_runtime": from_runtime,
                "to_runtime": runtime_value,
            }
        )
        asyncio.create_task(
            _run_agent(
                pid,
                _COMPACT_PROMPT,
                compact_mode=True,
                transfer_to_runtime=runtime_value,
            )
        )
        return _ok(
            f"{pid} runtime transfer queued: {from_runtime} → "
            f"{runtime_value} via /compact (watch pane for "
            "session_transferred event)"
        )

    @tool(
        "coord_set_player_model",
        (
            "Coach-only. Set or clear the model a Player runs on. "
            "Stored as a per-(slot, project) override on "
            "`agent_project_roles.model_override`.\n"
            "\n"
            "Prefer TIER ALIASES over version-pinned ids — they survive "
            "model bumps without rewriting your overrides:\n"
            "  Claude runtime: 'latest_opus', 'latest_sonnet', "
            "'latest_haiku'\n"
            "  Codex runtime:  'latest_gpt' (top-tier), 'latest_mini'\n"
            "Concrete ids (e.g. 'claude-opus-4-7', 'gpt-5.4-mini') are "
            "still accepted for cases where you specifically need a "
            "version pin, but aliases should be your default.\n"
            "\n"
            "Resolution order at spawn time (highest first): per-pane "
            "human override → this Coach override → per-role team "
            "default → SDK default. The override is silently dropped "
            "if it doesn't match the Player's current runtime "
            "(Claude vs Codex), so a stale value never breaks a turn. "
            "Aliases are resolved to the current concrete id at spawn "
            "time, so a stored 'latest_sonnet' picks up the next "
            "Sonnet release without re-running the tool.\n"
            "\n"
            "Read the 'Model selection policy' section of your system "
            "prompt FIRST — model changes are the exception, not the "
            "rule. Pass an empty `model` to clear and revert to the "
            "role default. Emits an 'agent_model_set' event so the UI "
            "refreshes immediately.\n"
            "\n"
            "Params:\n"
            "- player_id: one of p1..p10 (required)\n"
            "- model: tier alias ('latest_opus' etc.) or concrete id. "
            "Empty string clears."
        ),
        {"player_id": str, "model": str},
    )
    async def set_player_model(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach sets Player models.")
        pid = (args.get("player_id") or "").strip()
        model = (args.get("model") or "").strip()
        if not re.fullmatch(r"p([1-9]|10)", pid):
            return _err(f"invalid player_id '{pid}' — expected p1..p10")

        # Lazy import to avoid a top-level cycle: server.agents already
        # imports from this module at startup.
        from server.agents import _resolve_runtime_for
        from server.models_catalog import (
            _CLAUDE_MODEL_WHITELIST,
            _CODEX_MODEL_WHITELIST,
            model_is_claude,
            model_is_codex,
        )

        if model:
            runtime = await _resolve_runtime_for(pid)
            whitelist = (
                _CODEX_MODEL_WHITELIST if runtime == "codex"
                else _CLAUDE_MODEL_WHITELIST
            )
            if model not in whitelist:
                # Distinguish "wrong runtime" (model is real, just on
                # the other family) from "typo" so Coach knows whether
                # to flip the runtime first or pick a different id.
                # Without this split Coach reads the rejection as
                # "harness blocked me" and stops, missing that the
                # fix is `runtime_override` not a different model id.
                other_runtime = (
                    "codex" if runtime == "claude" and model_is_codex(model)
                    else "claude" if runtime == "codex" and model_is_claude(model)
                    else None
                )
                if other_runtime is not None:
                    same_runtime_aliases = (
                        "latest_opus / latest_sonnet / latest_haiku"
                        if runtime == "claude"
                        else "latest_gpt / latest_mini"
                    )
                    return _err(
                        f"'{model}' is a {other_runtime} model, but {pid} "
                        f"is on the {runtime} runtime. Flip the runtime "
                        f"first via "
                        f"coord_set_player_runtime(player_id='{pid}', "
                        f"runtime='{other_runtime}'), then call "
                        f"coord_set_player_model again. Or pick a "
                        f"{runtime} model ({same_runtime_aliases}) to "
                        f"keep the current runtime."
                    )
                family = "Codex" if runtime == "codex" else "Claude"
                allowed = sorted(m for m in whitelist if m)
                return _err(
                    f"unknown {family} model '{model}' for {pid} "
                    f"(runtime={runtime}). Allowed: {', '.join(allowed)}"
                )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (pid,))
            if not await cur.fetchone():
                return _err(f"player '{pid}' not found")
            # Empty-string clear on a player that has never had a row
            # is a no-op — don't create an all-NULL orphan. Any other
            # combination (existing row OR setting a non-empty value)
            # falls through to the upsert.
            cur = await c.execute(
                "SELECT 1 FROM agent_project_roles "
                "WHERE slot = ? AND project_id = ?",
                (pid, project_id),
            )
            row_exists = await cur.fetchone() is not None
            if not model and not row_exists:
                pass  # nothing to clear
            else:
                await c.execute(
                    "INSERT INTO agent_project_roles "
                    "(slot, project_id, model_override) VALUES (?, ?, ?) "
                    "ON CONFLICT(slot, project_id) DO UPDATE SET "
                    "  model_override = excluded.model_override",
                    (pid, project_id, model or None),
                )
                await c.commit()
        finally:
            await c.close()

        # `to: pid` lets the UI fan-out machinery render this event in
        # the target Player's pane too — Coach changing my model is
        # context I want to see when watching p3, not just buried in
        # Coach's timeline. Mirrors the message_sent / task_assigned
        # shape that the existing fan-out filter already understands.
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "agent_model_set",
                "player_id": pid,
                "to": pid,
                "model": model,
            }
        )
        if model:
            return _ok(f"{pid} model override → {model}")
        return _ok(f"{pid} model override cleared (will use role default)")

    @tool(
        "coord_set_player_effort",
        (
            "Coach-only. Set or clear a Player's reasoning-effort tier. "
            "Stored on `agent_project_roles.effort_override` for the "
            "active project. Maps directly to the SDK's thinking-budget "
            "Literal (low / medium / high / max).\n"
            "\n"
            "Resolution order at spawn time (highest first): per-pane "
            "request value (the human's pane gear popover) → this "
            "Coach override → no override (SDK default).\n"
            "\n"
            "Effort is the EXCEPTION, not the rule. Default is no "
            "override — the SDK picks a sensible thinking budget. "
            "Bump up for genuinely hard reasoning ('high' for tricky "
            "design / refactoring; 'max' for one-shot deep analysis); "
            "stay default for execution work. Sustained 'high' / 'max' "
            "burns the Max-plan token budget fast — review your active "
            "overrides periodically.\n"
            "\n"
            "Params:\n"
            "- player_id: one of p1..p10 (required; cannot set Coach's "
            "  effort via MCP — ask the human).\n"
            "- effort: one of 'low' | 'medium' | 'high' | 'max'. Empty "
            "  string clears (revert to no override). Aliases accepted: "
            "  'med' → 'medium'. Numeric 1..4 also accepted (1=low … "
            "  4=max) for symmetry with the UI slider."
        ),
        {"player_id": str, "effort": str},
    )
    async def set_player_effort(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach sets Player effort tiers.")
        pid = (args.get("player_id") or "").strip()
        raw = str(args.get("effort") or "").strip().lower()
        if not re.fullmatch(r"p([1-9]|10)", pid):
            return _err(f"invalid player_id '{pid}' — expected p1..p10")
        effort_value: int | None
        if raw in ("", "default", "clear", "none"):
            effort_value = None
        elif raw in ("low", "1"):
            effort_value = 1
        elif raw in ("medium", "med", "2"):
            effort_value = 2
        elif raw in ("high", "3"):
            effort_value = 3
        elif raw in ("max", "maximum", "4"):
            effort_value = 4
        else:
            return _err(
                f"invalid effort '{args.get('effort')}' — expected one of "
                "'low' | 'medium' | 'high' | 'max' (or empty to clear)"
            )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (pid,))
            if not await cur.fetchone():
                return _err(f"player '{pid}' not found")
            cur = await c.execute(
                "SELECT 1 FROM agent_project_roles "
                "WHERE slot = ? AND project_id = ?",
                (pid, project_id),
            )
            row_exists = await cur.fetchone() is not None
            if effort_value is None and not row_exists:
                pass  # nothing to clear; don't create an all-NULL orphan
            else:
                await c.execute(
                    "INSERT INTO agent_project_roles "
                    "(slot, project_id, effort_override) VALUES (?, ?, ?) "
                    "ON CONFLICT(slot, project_id) DO UPDATE SET "
                    "  effort_override = excluded.effort_override",
                    (pid, project_id, effort_value),
                )
                await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "agent_effort_set",
                "player_id": pid,
                "to": pid,
                "effort": effort_value,
            }
        )
        if effort_value is None:
            return _ok(f"{pid} effort override cleared")
        label = _EFFORT_VALUE_LABELS[effort_value]
        return _ok(f"{pid} effort override → {label}")

    @tool(
        "coord_set_player_plan_mode",
        (
            "Coach-only. Set or clear a Player's plan-mode default. "
            "When plan mode is on, every spawn for the Player runs with "
            "permission_mode='plan' — the Player drafts an outline and "
            "uses ExitPlanMode to surface it for human review BEFORE "
            "touching tools. Stored on "
            "`agent_project_roles.plan_mode_override`.\n"
            "\n"
            "Resolution order at spawn time (highest first): per-pane "
            "request value (the human's pane gear popover) → this "
            "Coach override → off.\n"
            "\n"
            "Plan mode is a heavy constraint — every turn pauses for "
            "human approval. Use sparingly: only set it on Players doing "
            "destructive / hard-to-undo work where you want the human "
            "to review the approach first. For most work, leave it off "
            "and rely on per-turn coord_request_human / AskUserQuestion "
            "checkpoints instead.\n"
            "\n"
            "Params:\n"
            "- player_id: one of p1..p10 (required; cannot set Coach's "
            "  plan mode via MCP).\n"
            "- plan_mode: 'on' | 'off' to set explicitly. Empty string "
            "  clears (revert to no override → off unless the human "
            "  toggled it per-pane). Aliases: 'true'/'1'/'yes' → on, "
            "  'false'/'0'/'no' → off."
        ),
        {"player_id": str, "plan_mode": str},
    )
    async def set_player_plan_mode(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach sets Player plan-mode defaults.")
        pid = (args.get("player_id") or "").strip()
        raw = str(args.get("plan_mode") or "").strip().lower()
        if not re.fullmatch(r"p([1-9]|10)", pid):
            return _err(f"invalid player_id '{pid}' — expected p1..p10")
        plan_value: int | None
        if raw in ("", "default", "clear", "none"):
            plan_value = None
        elif raw in ("on", "true", "1", "yes", "y"):
            plan_value = 1
        elif raw in ("off", "false", "0", "no", "n"):
            plan_value = 0
        else:
            return _err(
                f"invalid plan_mode '{args.get('plan_mode')}' — expected "
                "'on' | 'off' (or empty to clear)"
            )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (pid,))
            if not await cur.fetchone():
                return _err(f"player '{pid}' not found")
            cur = await c.execute(
                "SELECT 1 FROM agent_project_roles "
                "WHERE slot = ? AND project_id = ?",
                (pid, project_id),
            )
            row_exists = await cur.fetchone() is not None
            if plan_value is None and not row_exists:
                pass
            else:
                await c.execute(
                    "INSERT INTO agent_project_roles "
                    "(slot, project_id, plan_mode_override) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(slot, project_id) DO UPDATE SET "
                    "  plan_mode_override = excluded.plan_mode_override",
                    (pid, project_id, plan_value),
                )
                await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "agent_plan_mode_set",
                "player_id": pid,
                "to": pid,
                "plan_mode": plan_value,
            }
        )
        if plan_value is None:
            return _ok(f"{pid} plan-mode override cleared")
        return _ok(f"{pid} plan-mode override → {'on' if plan_value else 'off'}")

    @tool(
        "coord_get_player_settings",
        (
            "Coach-only. Read the current per-Player overrides in one "
            "call: runtime, model, effort, plan-mode. For each Player "
            "the response shows BOTH the override value (what you set "
            "via coord_set_player_*) AND the resolved value (what the "
            "Player will actually run with on next spawn, after "
            "fall-through to role defaults).\n"
            "\n"
            "Use before changing settings — confirms what's already in "
            "place so you don't re-set what's already correct, and so "
            "you can see at a glance whether a Player has any active "
            "overrides at all.\n"
            "\n"
            "Params:\n"
            "- player_id: optional. One of p1..p10 to scope to a single "
            "  Player; omit for the whole roster (incl. Coach for "
            "  runtime/model — Coach has no effort/plan-mode override "
            "  surface)."
        ),
        {"player_id": str},
    )
    async def get_player_settings(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach reads team settings via MCP.")
        from server.agents import (
            _get_agent_identity,
            _get_role_default_model,
            _resolve_runtime_for,
        )
        from server.models_catalog import (
            resolve_model_alias,
            role_default_effort,
            role_default_plan_mode,
        )

        scope_id = (args.get("player_id") or "").strip()
        if scope_id and not re.fullmatch(r"(coach|p([1-9]|10))", scope_id):
            return _err(
                f"invalid player_id '{scope_id}' — expected p1..p10 or "
                "'coach' (or omit for the full roster)"
            )

        slots = [scope_id] if scope_id else (
            ["coach"] + [f"p{i}" for i in range(1, 11)]
        )

        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id, runtime_override FROM agents WHERE id IN ("
                + ",".join("?" * len(slots)) + ")",
                slots,
            )
            agent_rows = {dict(r)["id"]: dict(r) for r in await cur.fetchall()}
        finally:
            await c.close()

        out_rows: list[dict[str, Any]] = []
        for sid in slots:
            ident = await _get_agent_identity(sid) or {}
            runtime_override = (
                (agent_rows.get(sid, {}).get("runtime_override") or "").lower()
                or None
            )
            resolved_runtime = await _resolve_runtime_for(sid)
            model_override = ident.get("model_override")
            effort_override = ident.get("effort_override")
            plan_override = ident.get("plan_mode_override")

            resolved_model = (
                model_override
                or await _get_role_default_model(sid, resolved_runtime)
                or None
            )
            if resolved_model:
                resolved_model = resolve_model_alias(resolved_model)

            out_rows.append({
                "slot": sid,
                "name": ident.get("name"),
                "runtime": {
                    "override": runtime_override,
                    "resolved": resolved_runtime,
                },
                "model": {
                    "override": model_override,
                    "resolved": resolved_model,
                },
                "effort": {
                    "override": (
                        _EFFORT_VALUE_LABELS.get(int(effort_override))
                        if effort_override is not None else None
                    ),
                    "resolved": _EFFORT_VALUE_LABELS.get(
                        role_default_effort(sid)
                        if effort_override is None
                        else int(effort_override)
                    ),
                },
                "plan_mode": {
                    "override": (
                        None if plan_override is None
                        else bool(int(plan_override))
                    ),
                    "resolved": (
                        bool(int(plan_override))
                        if plan_override is not None
                        else role_default_plan_mode(sid)
                    ),
                },
            })

        # Render a compact text table — easier for Coach to scan than
        # raw JSON, and keeps the response under the SDK's per-tool
        # text limit even at full roster (~11 rows).
        lines = [
            "slot   name           runtime          model                          effort      plan",
            "-----  -------------  ---------------  -----------------------------  ----------  -----",
        ]
        for r in out_rows:
            slot = (r["slot"] or "").ljust(5)
            name = (r.get("name") or "").ljust(13)[:13]
            ro = r["runtime"]["override"]
            rr = r["runtime"]["resolved"]
            rt_cell = (
                f"{ro} (override)" if ro
                else f"{rr} (default)"
            ).ljust(15)[:15]
            mo = r["model"]["override"]
            mr = r["model"]["resolved"] or "(SDK default)"
            md_cell = (
                f"{mo} (override)" if mo
                else f"{mr} (default)"
            ).ljust(29)[:29]
            ef_cell = (
                f"{r['effort']['override']} (override)"
                if r["effort"]["override"]
                else f"{r['effort']['resolved']} (default)"
            ).ljust(10)[:10]
            pm_cell = (
                "on" if r["plan_mode"]["override"] is True
                else "off" if r["plan_mode"]["override"] is False
                else ("on (default)" if r["plan_mode"]["resolved"] else "off (default)")
            )
            lines.append(f"{slot}  {name}  {rt_cell}  {md_cell}  {ef_cell}  {pm_cell}")
        text = "\n".join(lines)
        return {"content": [{"type": "text", "text": text}]}

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

    @tool(
        "coord_add_todo",
        (
            "Coach-only. Append a new entry to the project's "
            "coach-todos.md — the finite, strikeable backlog injected "
            "into your system prompt every turn (recurrence-specs.md "
            "§3.1).\n"
            "\n"
            "Use this for items YOU need to do in future turns: a "
            "follow-up to check, a small task too thin to assign to a "
            "Player, an objective-driven action to advance next time. "
            "DO NOT use it as a Player task board — that's `tasks` "
            "via coord_create_task.\n"
            "\n"
            "Params:\n"
            "- title: short imperative title (required)\n"
            "- description: optional free markdown, can span lines\n"
            "- due: optional 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MMZ'"
        ),
        {"title": str, "description": str, "due": str},
    )
    async def add_todo(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach manages coach-todos. Players: send a "
                "coord_send_message to coach if you want them to "
                "queue a follow-up."
            )
        title = (args.get("title") or "").strip()
        description = (args.get("description") or "").strip()
        due = (args.get("due") or "").strip() or None
        if not title:
            return _err("title is required")
        from server import coach_todos as todos_mod
        project_id = await resolve_active_project()
        try:
            todo = await todos_mod.add_todo(
                project_id, title=title,
                description=description, due=due,
            )
        except ValueError as e:
            return _err(str(e))
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": caller_id,
            "type": "coach_todo_added",
            "id": todo.id,
            "title": todo.title,
            "due": todo.due,
        })
        return _ok(
            f"todo {todo.id} added: {todo.title}"
            + (f" (due {todo.due})" if todo.due else "")
        )

    @tool(
        "coord_complete_todo",
        (
            "Coach-only. Mark a coach-todos.md entry done. The entry "
            "moves to working/coach-todos-archive.md (still readable "
            "via Read but not injected into the system prompt). The "
            "id is permanent — completed entries keep theirs.\n"
            "\n"
            "Params:\n"
            "- id: the t-N identifier from coach-todos.md (required)"
        ),
        {"id": str},
    )
    async def complete_todo(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach manages coach-todos.")
        tid = (args.get("id") or "").strip()
        if not tid:
            return _err("id is required (e.g. 't-3')")
        from server import coach_todos as todos_mod
        project_id = await resolve_active_project()
        try:
            todo = await todos_mod.complete_todo(project_id, tid)
        except KeyError as e:
            return _err(str(e))
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": caller_id,
            "type": "coach_todo_completed",
            "id": todo.id,
            "title": todo.title,
        })
        return _ok(f"todo {todo.id} completed: {todo.title}")

    @tool(
        "coord_update_todo",
        (
            "Coach-only. Edit a coach-todos.md entry in place. Pass "
            "only the fields you want to change. Useful when the "
            "scope of a planned action shifts or a deadline moves.\n"
            "\n"
            "Params:\n"
            "- id: the t-N identifier (required)\n"
            "- title: new title (optional)\n"
            "- description: new description (optional)\n"
            "- due: new 'YYYY-MM-DD' or empty string to clear"
        ),
        {"id": str, "title": str, "description": str, "due": str},
    )
    async def update_todo(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach manages coach-todos.")
        tid = (args.get("id") or "").strip()
        if not tid:
            return _err("id is required (e.g. 't-3')")
        # Distinguish "field omitted" from "field passed as empty string".
        # Pydantic-free MCP args mean we read the dict directly.
        kwargs: dict[str, Any] = {}
        if "title" in args:
            kwargs["title"] = args["title"]
        if "description" in args:
            kwargs["description"] = args["description"]
        if "due" in args:
            # Empty due clears the deadline.
            due_val = args.get("due")
            kwargs["due"] = None if (
                due_val is None or str(due_val).strip() == ""
            ) else str(due_val)
        if not kwargs:
            return _err("pass at least one of: title, description, due")
        from server import coach_todos as todos_mod
        project_id = await resolve_active_project()
        try:
            todo = await todos_mod.update_todo(
                project_id, tid, **kwargs,
            )
        except (KeyError, ValueError) as e:
            return _err(str(e))
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": caller_id,
            "type": "coach_todo_updated",
            "id": todo.id,
            "fields": list(kwargs.keys()),
        })
        return _ok(f"todo {todo.id} updated: {todo.title}")

    # ============================================================
    # Compass tools — Coach-only strategy-engine surface.
    # All four reject Player calls with the same canonical message
    # so coach-only behavior is discoverable from any error reply.
    # Each tool also rejects when Compass is disabled for the active
    # project (team_config['compass_enabled_<id>'] not truthy).
    # ============================================================

    async def _compass_gate(project_id: str) -> str | None:
        """Return None if Compass is enabled for this project, else
        an error message. Lazy import to dodge the tools↔compass.api
        back-edge."""
        if not caller_is_coach:
            return (
                "Compass tools are Coach-only. "
                "Players read Compass via the CLAUDE.md block."
            )
        from server.compass import config as cmp_config  # noqa: PLC0415

        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT value FROM team_config WHERE key = ?",
                (cmp_config.enabled_key(project_id),),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
        val = (dict(row).get("value") if row else "") or ""
        if val.strip().lower() not in ("1", "true", "yes"):
            return (
                "Compass is disabled for this project. "
                "Enable it from the Compass dashboard before using "
                "compass_ask / compass_audit / compass_brief / compass_status."
            )
        return None

    @tool(
        "compass_ask",
        (
            "Coach-only. Interrogate Compass on any project topic. "
            "Compass answers based strictly on its lattice and "
            "truth-protected facts, citing statement ids and weights. "
            "Treats >0.8 as confirmed YES, <0.2 as confirmed NO "
            "(negation is binding), 0.4–0.6 as genuinely uncertain. "
            "Read-only — does not modify the lattice.\n"
            "\n"
            "Params:\n"
            "- query: free-text question, e.g. "
            "'should we build for usage or flat pricing?'"
        ),
        {"query": str},
    )
    async def compass_ask(args: dict[str, Any]) -> dict[str, Any]:
        query_text = (args.get("query") or "").strip()
        if not query_text:
            return _err("query is required")
        project_id = await resolve_active_project()
        gate = await _compass_gate(project_id)
        if gate:
            return _err(gate)
        from server.compass import llm as cmp_llm  # noqa: PLC0415
        from server.compass import prompts as cmp_prompts  # noqa: PLC0415
        from server.compass import store as cmp_store  # noqa: PLC0415

        # load_with_meta — anchors the prompt on the project's
        # identity so harness chatter (player slot names, model
        # overrides) doesn't pollute Coach's view via compass_ask.
        state = await cmp_store.load_with_meta(project_id)
        try:
            res = await cmp_llm.call(
                cmp_prompts.COACH_QUERY_SYSTEM,
                cmp_prompts.coach_query_user(state, query_text),
                project_id=project_id,
                label="compass:ask",
            )
        except Exception as e:
            return _err(f"compass_ask LLM call failed: {type(e).__name__}: {e}")
        body = (res.text or "").strip()
        if not body:
            return _ok("(Compass had no response — lattice may be empty.)")
        return _ok(body)

    @tool(
        "compass_audit",
        (
            "Coach-only. Submit a work artifact (commit message, "
            "decision, worker output, design choice) for audit "
            "against the lattice. Compass returns one of three "
            "verdicts: 'aligned' (proceed silently), "
            "'confident_drift' (work clearly contradicts a high-"
            "confidence statement), or 'uncertain_drift' (work "
            "seems off but the relevant statements are mid-weight; "
            "a question is queued for the human). Audits are "
            "advisory — they never block work.\n"
            "\n"
            "Params:\n"
            "- artifact: the work to audit, e.g. "
            "'worker-4 implemented per-second billing instead of "
            "per-task as originally scoped'"
        ),
        {"artifact": str},
    )
    async def compass_audit(args: dict[str, Any]) -> dict[str, Any]:
        artifact = (args.get("artifact") or "").strip()
        if not artifact:
            return _err("artifact is required")
        project_id = await resolve_active_project()
        gate = await _compass_gate(project_id)
        if gate:
            return _err(gate)
        from server.compass import audit as cmp_audit  # noqa: PLC0415

        try:
            verdict = await cmp_audit.audit_work(project_id, artifact)
        except Exception as e:
            return _err(f"compass_audit failed: {type(e).__name__}: {e}")
        # Render as a compact markdown block — Coach is reading this.
        lines = [f"**Verdict:** `{verdict['verdict']}`"]
        if verdict.get("summary"):
            lines.append(f"**Summary:** {verdict['summary']}")
        if verdict.get("contradicting_ids"):
            lines.append(
                "**Contradicts:** " + ", ".join(verdict["contradicting_ids"])
            )
        if verdict.get("message_to_coach"):
            lines.append("")
            lines.append(verdict["message_to_coach"])
        if verdict.get("question_id"):
            lines.append("")
            lines.append(
                f"_A question for the human has been queued ({verdict['question_id']})._"
            )
        return _ok("\n".join(lines))

    @tool(
        "compass_brief",
        (
            "Coach-only. Fetch the most recent daily briefing — a "
            "structured markdown digest with sections: CONFIRMED "
            "YES, CONFIRMED NO, LEANING, OPEN, COVERAGE, DRIFT, "
            "RECOMMENDATION. Read-only. If no briefing has been "
            "generated yet (fresh project pre-bootstrap), returns "
            "an explanatory placeholder."
        ),
        {},
    )
    async def compass_brief(args: dict[str, Any]) -> dict[str, Any]:
        del args  # no-arg tool; signature is part of the SDK contract
        project_id = await resolve_active_project()
        gate = await _compass_gate(project_id)
        if gate:
            return _err(gate)
        from server.compass import store as cmp_store  # noqa: PLC0415

        text = cmp_store.latest_briefing_text(project_id)
        if not text:
            return _ok(
                "_No Compass briefing exists for this project yet. "
                "Either Compass hasn't run, or this is a bootstrap-only "
                "project. The dashboard's RUN button generates one._"
            )
        return _ok(text)

    @tool(
        "compass_status",
        (
            "Coach-only. Quick status snapshot of Compass for the "
            "active project. Returns counts of active and archived "
            "statements, regions, pending questions, pending "
            "settle/stale/dupe proposals, and the timestamp of the "
            "last run + last briefing. Read-only."
        ),
        {},
    )
    async def compass_status(args: dict[str, Any]) -> dict[str, Any]:
        del args  # no-arg tool; signature is part of the SDK contract
        project_id = await resolve_active_project()
        gate = await _compass_gate(project_id)
        if gate:
            return _err(gate)
        from server.compass import config as cmp_config  # noqa: PLC0415
        from server.compass import store as cmp_store  # noqa: PLC0415

        state = cmp_store.load_state(project_id)
        briefing_dates = cmp_store.list_briefing_dates(project_id)
        c2 = await configured_conn()
        try:
            cur = await c2.execute(
                "SELECT value FROM team_config WHERE key = ?",
                (cmp_config.last_run_key(project_id),),
            )
            row = await cur.fetchone()
        finally:
            await c2.close()
        last_run = (dict(row).get("value") if row else "") or "(never)"
        active = state.active_statements()
        archived = state.archived_statements()
        regions = [r.name for r in state.active_regions()]
        pending_qs = [
            q for q in state.questions
            if not q.digested and not q.contradicted and not q.ambiguity_accepted
        ]
        unanswered_qs = [q for q in pending_qs if q.answer is None]

        lines = [
            "**Compass status**",
            f"- active statements: {len(active)}",
            f"- archived: {len(archived)}",
            f"- regions ({len(regions)}): "
            + (", ".join(regions) if regions else "—"),
            f"- pending questions: {len(pending_qs)} "
            f"({len(unanswered_qs)} unanswered)",
            f"- pending settle proposals: {len(state.settle_proposals)}",
            f"- pending stale proposals: {len(state.stale_proposals)}",
            f"- pending dupe proposals: {len(state.duplicate_proposals)}",
            f"- last run: {last_run}",
            f"- last briefing: {briefing_dates[0] if briefing_dates else '(none)'}",
        ]
        return _ok("\n".join(lines))

    # ====================================================================
    # Kanban tools (Docs/kanban-specs.md). The state-machine + existing
    # tool updates (claim_task, assign_task, update_task, commit_push)
    # live above; the new tools below add the role-assignment surface
    # (planner / auditor / shipper), the spec/audit artifact writers,
    # and the introspection / meta knobs.
    # ====================================================================

    @tool(
        "coord_write_task_spec",
        (
            "Write the spec.md for a task. Required before a standard-"
            "complexity task can move plan→execute (gate enforced in "
            "coord_assign_task / coord_claim_task). Simple tasks don't "
            "need a spec — title + description on the row are enough.\n"
            "\n"
            "Permission: Coach can spec any task. A Player can spec a "
            "task if they (a) have an active planner role assignment, "
            "(b) are the executor (re-spec during a fail loop), or "
            "(c) it's a subtask of their current task.\n"
            "\n"
            "Body is a full markdown document. Frontmatter is added "
            "automatically (task_id / title / created_by / priority / "
            "complexity / spec_author / spec_written_at). The body "
            "should cover Goal, 'Done looks like', Constraints, References. "
            "Existing spec is overwritten — rolling history lives in events.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- body: full markdown body, required (max 40000 chars)"
        ),
        {"task_id": str, "body": str},
    )
    async def write_task_spec(args: dict[str, Any]) -> dict[str, Any]:
        task_id = (args.get("task_id") or "").strip()
        body = args.get("body") or ""
        if not task_id:
            return _err("task_id is required")
        if not body.strip():
            return _err("body is required (empty specs are not useful)")
        if len(body) > 40_000:
            return _err(f"body too long ({len(body)} chars, max 40000)")

        from server.tasks import (
            is_valid_task_id, write_task_spec as _write_spec,
            spec_relative_path,
        )
        if not is_valid_task_id(task_id):
            return _err(f"invalid task_id format: {task_id!r}")

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            # Read task metadata + permission check.
            cur = await c.execute(
                "SELECT title, owner, created_by, created_at, priority, "
                "complexity, status, parent_id "
                "FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            t = dict(row)

            # Permission: Coach can always spec. Players need one of:
            #   (a) active planner role for this task
            #   (b) executor (== tasks.owner)
            #   (c) parent is the caller's current task
            allowed = False
            if caller_is_coach:
                allowed = True
            elif t["owner"] == caller_id:
                allowed = True
            else:
                # Active planner row check.
                cur = await c.execute(
                    "SELECT 1 FROM task_role_assignments "
                    "WHERE task_id = ? AND role = 'planner' AND owner = ? "
                    "AND completed_at IS NULL AND superseded_by IS NULL "
                    "LIMIT 1",
                    (task_id, caller_id),
                )
                if await cur.fetchone():
                    allowed = True
                else:
                    # Subtask under caller's current task?
                    cur = await c.execute(
                        "SELECT current_task_id FROM agents WHERE id = ?",
                        (caller_id,),
                    )
                    arow = await cur.fetchone()
                    cur_task = dict(arow)["current_task_id"] if arow else None
                    if cur_task and t["parent_id"] == cur_task:
                        allowed = True

            if not allowed:
                return _err(
                    f"you can't spec task {task_id} — Coach can spec any "
                    f"task; Players need an active planner assignment, to "
                    f"be the executor, or to spec a subtask of their "
                    f"current task."
                )
        finally:
            await c.close()

        try:
            target, rel, written_at = await _write_spec(
                project_id=project_id,
                task_id=task_id,
                title=t["title"],
                body=body,
                author=caller_id,
                created_by=t["created_by"],
                created_at=t["created_at"],
                priority=t["priority"],
                complexity=t["complexity"],
            )
        except ValueError as exc:
            return _err(str(exc))
        except Exception as exc:
            return _err(f"spec write failed: {exc}")

        # Update tasks.spec_path + spec_written_at; mark planner role
        # complete if there's an active row owned by the caller.
        completed_planner = False
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE tasks SET spec_path = ?, spec_written_at = ? "
                "WHERE id = ? AND project_id = ?",
                (rel, written_at, task_id, project_id),
            )
            cur = await c.execute(
                "UPDATE task_role_assignments SET completed_at = ? "
                "WHERE task_id = ? AND role = 'planner' AND owner = ? "
                "AND completed_at IS NULL AND superseded_by IS NULL",
                (written_at, task_id, caller_id),
            )
            completed_planner = bool(cur.rowcount)
            await c.commit()
        finally:
            await c.close()

        ts = _now_iso()
        await bus.publish(
            {
                "ts": ts,
                "agent_id": caller_id,
                "type": "task_spec_written",
                "task_id": task_id,
                "spec_path": rel,
                "to": t["owner"],
            }
        )
        if completed_planner:
            await bus.publish(
                {
                    "ts": ts,
                    "agent_id": caller_id,
                    "type": "task_role_completed",
                    "task_id": task_id,
                    "role": "planner",
                    "owner": caller_id,
                    "artifact_path": rel,
                    "to": t["owner"],
                }
            )
        return _ok(
            f"wrote spec for {task_id} ({len(body)} chars) → {rel}"
        )

    # --------------------------------------------------------------
    # Role assignment tools (Coach-only). Pattern shared across
    # planner / auditor / shipper: accept single Player or list-as-pool;
    # validate target slots; insert task_role_assignments row(s); auto-wake.
    # --------------------------------------------------------------

    async def _assign_role_helper(
        c,
        *,
        task_id: str,
        role: str,
        targets: list[str],
        wake_prompt_for_role: str,
    ) -> tuple[bool, str, list[str]]:
        """Insert a task_role_assignments row for the given role and
        wake eligible Players. Returns (ok, message, woken_slots).

        For hard-assign (single target) the row's `owner` is set
        immediately. For pool (multi-target) `owner` stays NULL until
        a Player claims via coord_claim_task (executor pool only —
        for planner / auditor / shipper, the first to act on the
        wake prompt by writing the artifact "wins" implicitly via
        the role's completed_at column).
        """
        import json as _json
        is_pool = len(targets) > 1
        now = _now_iso()
        eligible_json = _json.dumps(targets)
        if is_pool:
            await c.execute(
                "INSERT INTO task_role_assignments "
                "(task_id, role, eligible_owners, owner, assigned_at) "
                "VALUES (?, ?, ?, NULL, ?)",
                (task_id, role, eligible_json, now),
            )
        else:
            await c.execute(
                "INSERT INTO task_role_assignments "
                "(task_id, role, eligible_owners, owner, "
                "assigned_at, claimed_at) "
                "VALUES (?, ?, '[]', ?, ?, ?)",
                (task_id, role, targets[0], now, now),
            )
        await c.commit()

        # Auto-wake eligible Players. Late import to dodge circular dep.
        try:
            from server.agents import maybe_wake_agent
            for slot in targets:
                try:
                    await maybe_wake_agent(
                        slot, wake_prompt_for_role, bypass_debounce=True
                    )
                except Exception:
                    pass
        except Exception:
            pass

        if is_pool:
            return True, (
                f"posted {task_id} to {role} pool: {', '.join(targets)}"
            ), targets
        return True, f"assigned {task_id} {role} → {targets[0]}", targets

    async def _validate_role_targets(
        targets_raw: str, *, role_label: str
    ) -> tuple[list[str] | None, str | None]:
        """Parse and validate a `to=` argument: comma-list → pool;
        single → hard-assign. Returns `(slots, None)` or `(None, error)`."""
        if not targets_raw:
            return None, f"'to' is required (Player slot id, or comma-list for {role_label} pool)"
        if "," in targets_raw:
            parts = [p.strip().lower() for p in targets_raw.split(",") if p.strip()]
        else:
            parts = [targets_raw.strip().lower()]
        if not parts:
            return None, "'to' resolved to empty list"
        for slot in parts:
            if slot in ("coach", "broadcast"):
                return None, f"can only assign Players (p1..p10), not {slot!r}"
            if slot not in VALID_RECIPIENTS:
                return None, f"invalid target '{slot}' — must be p1..p10"
        seen: set[str] = set()
        deduped: list[str] = []
        for slot in parts:
            if slot in seen:
                continue
            seen.add(slot)
            if await _is_locked(slot):
                return None, (
                    f"Player {slot} is locked. Pick unlocked Players."
                )
            deduped.append(slot)
        return deduped, None

    @tool(
        "coord_assign_planner",
        (
            "Coach-only. Delegate writing the spec for a task. Both "
            "single-Player hard-assign and comma-list pool are accepted "
            "(same shape as coord_assign_task).\n"
            "\n"
            "Optional: if Coach is happy writing the spec themselves, "
            "they SKIP this tool and just call coord_write_task_spec "
            "directly. Both flows are valid.\n"
            "\n"
            "Standard-complexity tasks need a spec to leave the plan "
            "stage; simple tasks can skip the planner role entirely.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- to: 'p3' or 'p1,p2,p3' (planner pool)"
        ),
        {"task_id": str, "to": str},
    )
    async def assign_planner(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach assigns planners.")
        task_id = (args.get("task_id") or "").strip()
        to_raw = (args.get("to") or "").strip()
        if not task_id:
            return _err("task_id is required")
        targets, err = await _validate_role_targets(to_raw, role_label="planner")
        if err:
            return _err(err)
        if targets is None:
            return _err("internal: target validation returned None")
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status, complexity FROM tasks "
                "WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            t = dict(row)
            if t["status"] != "plan":
                return _err(
                    f"planner only makes sense in `plan` stage; "
                    f"task is currently {t['status']}."
                )
            wake_prompt = (
                f"Coach asked you to draft the spec for task {task_id}. "
                f"Read coord_list_tasks output for the title/description, "
                f"then call coord_write_task_spec(task_id={task_id!r}, "
                f"body=...) with the goal, 'done looks like', constraints, "
                f"and references."
            )
            ok, msg, slots = await _assign_role_helper(
                c, task_id=task_id, role="planner", targets=targets,
                wake_prompt_for_role=wake_prompt,
            )
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_role_assigned",
                "task_id": task_id,
                "role": "planner",
                "eligible_owners": targets,
                "owner": (None if len(targets) > 1 else targets[0]),
                "to": (targets[0] if len(targets) == 1 else None),
            }
        )
        return _ok(msg)

    @tool(
        "coord_assign_auditor",
        (
            "Coach-only. Assign a Player to audit a task. Two kinds:\n"
            "  - 'syntax':    tests/CI/lint/code review (mechanical correctness)\n"
            "  - 'semantics': alignment with spec / project goals / lattice\n"
            "\n"
            "Single-Player or comma-list pool. The auditor reads the "
            "spec + commit + (semantics only) Compass audit report, then "
            "calls coord_submit_audit_report(verdict='pass'|'fail').\n"
            "\n"
            "If you assign the executor as their own auditor, an "
            "audit_self_review_warning event fires (does NOT block — "
            "useful when the team is small).\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- to: 'p4' or 'p4,p5'\n"
            "- kind: 'syntax' or 'semantics' (required)"
        ),
        {"task_id": str, "to": str, "kind": str},
    )
    async def assign_auditor(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach assigns auditors.")
        task_id = (args.get("task_id") or "").strip()
        to_raw = (args.get("to") or "").strip()
        kind = (args.get("kind") or "").strip().lower()
        if not task_id:
            return _err("task_id is required")
        role = _resolve_audit_role_kind(kind)
        if role is None:
            return _err("kind must be 'syntax' or 'semantics'")
        targets, err = await _validate_role_targets(to_raw, role_label=f"{kind} auditor")
        if err:
            return _err(err)
        if targets is None:
            return _err("internal: target validation returned None")
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status, owner FROM tasks "
                "WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            t = dict(row)
            executor = t.get("owner")
            self_review = bool(executor and executor in targets)
            wake_prompt = (
                f"Coach asked you to audit task {task_id} ({kind}). "
                f"Read the spec at /data/projects/.../tasks/{task_id}/spec.md, "
                f"the commit history, and (for semantics) any Compass "
                f"audit report linked from the kanban card. When done, "
                f"call coord_submit_audit_report(task_id={task_id!r}, "
                f"kind={kind!r}, verdict='pass'|'fail', body=...)"
            )
            ok, msg, slots = await _assign_role_helper(
                c, task_id=task_id, role=role, targets=targets,
                wake_prompt_for_role=wake_prompt,
            )
            if ok:
                cur = await c.execute(
                    "SELECT id FROM task_role_assignments "
                    "WHERE task_id = ? AND role = ? "
                    "ORDER BY assigned_at DESC, id DESC LIMIT 1",
                    (task_id, role),
                )
                new_row = await cur.fetchone()
                if new_row:
                    new_id = dict(new_row)["id"]
                    await c.execute(
                        "UPDATE task_role_assignments SET superseded_by = ? "
                        "WHERE task_id = ? AND role = ? AND id <> ? "
                        "AND verdict = 'fail' AND completed_at IS NOT NULL "
                        "AND superseded_by IS NULL",
                        (new_id, task_id, role, new_id),
                    )
                    await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_role_assigned",
                "task_id": task_id,
                "role": role,
                "eligible_owners": targets,
                "owner": (None if len(targets) > 1 else targets[0]),
                "to": (targets[0] if len(targets) == 1 else None),
            }
        )
        if self_review:
            await bus.publish(
                {
                    "ts": _now_iso(),
                    "agent_id": caller_id,
                    "type": "audit_self_review_warning",
                    "task_id": task_id,
                    "kind": kind,
                    "auditor_id": targets[0] if len(targets) == 1 else None,
                    "executor_id": executor,
                }
            )
        return _ok(msg)

    @tool(
        "coord_assign_shipper",
        (
            "Coach-only. Assign a Player to handle the merge for a task "
            "(after both audits pass). Single Player or pool.\n"
            "\n"
            "The shipper opens the PR / runs the merge / calls "
            "coord_mark_shipped(task_id) when the work is in main. "
            "Coach doesn't merge — that's Player work.\n"
            "\n"
            "Often the shipper is the executor (they know the change "
            "best); pick someone different if you want a second pair "
            "of eyes on the merge.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- to: 'p3' or 'p3,p4'"
        ),
        {"task_id": str, "to": str},
    )
    async def assign_shipper(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach assigns shippers.")
        task_id = (args.get("task_id") or "").strip()
        to_raw = (args.get("to") or "").strip()
        if not task_id:
            return _err("task_id is required")
        targets, err = await _validate_role_targets(to_raw, role_label="shipper")
        if err:
            return _err(err)
        if targets is None:
            return _err("internal: target validation returned None")
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status FROM tasks "
                "WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            wake_prompt = (
                f"Coach assigned you to ship task {task_id}. Open the PR, "
                f"run the merge, then call coord_mark_shipped("
                f"task_id={task_id!r}). The kanban will then archive "
                f"the task."
            )
            ok, msg, slots = await _assign_role_helper(
                c, task_id=task_id, role="shipper", targets=targets,
                wake_prompt_for_role=wake_prompt,
            )
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_role_assigned",
                "task_id": task_id,
                "role": "shipper",
                "eligible_owners": targets,
                "owner": (None if len(targets) > 1 else targets[0]),
                "to": (targets[0] if len(targets) == 1 else None),
            }
        )
        return _ok(msg)

    @tool(
        "coord_submit_audit_report",
        (
            "Player-only. Submit your audit report for a task. Validates "
            "you have an active auditor assignment (matching kind) for "
            "this task — you can't audit something you weren't assigned to.\n"
            "\n"
            "Writes the markdown report to "
            "/data/projects/<id>/working/tasks/<task_id>/audits/"
            "audit_<round>_<kind>.md and triggers the kanban subscriber:\n"
            "  - verdict='pass' on syntax → task moves to audit_semantics\n"
            "  - verdict='pass' on semantics → task moves to ship\n"
            "  - verdict='fail' on either → task reverts to execute "
            "(executor gets the spec + your report attached on auto-wake)\n"
            "\n"
            "Body should explain what you checked, what you found, and "
            "(on fail) specifically what needs to be fixed.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- kind: 'syntax' or 'semantics' (required, must match assignment)\n"
            "- body: full markdown report (required, max 40000 chars)\n"
            "- verdict: 'pass' or 'fail' (required)"
        ),
        {"task_id": str, "kind": str, "body": str, "verdict": str},
    )
    async def submit_audit_report(args: dict[str, Any]) -> dict[str, Any]:
        if caller_is_coach:
            return _err(
                "Coach doesn't audit — assign a Player auditor with "
                "coord_assign_auditor and let them submit."
            )
        task_id = (args.get("task_id") or "").strip()
        kind = (args.get("kind") or "").strip().lower()
        body = args.get("body") or ""
        verdict = (args.get("verdict") or "").strip().lower()
        if not task_id:
            return _err("task_id is required")
        role = _resolve_audit_role_kind(kind)
        if role is None:
            return _err("kind must be 'syntax' or 'semantics'")
        if verdict not in ("pass", "fail"):
            return _err("verdict must be 'pass' or 'fail'")
        if not body.strip():
            return _err("body is required")
        if len(body) > 40_000:
            return _err(f"body too long ({len(body)} chars, max 40000)")

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            # Find the active auditor assignment for the caller. Must
            # exist, be uncompleted, and not superseded.
            cur = await c.execute(
                "SELECT id FROM task_role_assignments "
                "WHERE task_id = ? AND role = ? AND owner = ? "
                "AND completed_at IS NULL AND superseded_by IS NULL "
                "ORDER BY assigned_at DESC LIMIT 1",
                (task_id, role, caller_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(
                    f"no active {kind} auditor assignment for {caller_id} "
                    f"on task {task_id}. Coach must call "
                    f"coord_assign_auditor before you can submit."
                )
            assignment_id = dict(row)["id"]

            # Compute round number = count of prior assignments for this
            # (task, kind) PLUS 1. Includes this one (we just SELECTed it).
            cur = await c.execute(
                "SELECT COUNT(*) AS n FROM task_role_assignments "
                "WHERE task_id = ? AND role = ?",
                (task_id, role),
            )
            count_row = await cur.fetchone()
            round_num = int(dict(count_row)["n"])
        finally:
            await c.close()

        # Write the report .md.
        from server.tasks import (
            write_audit_report as _write_audit, audit_report_relative_path,
        )
        try:
            target, rel, submitted_at = await _write_audit(
                project_id=project_id,
                task_id=task_id,
                kind=kind,
                round_num=round_num,
                body=body,
                auditor=caller_id,
                verdict=verdict,
            )
        except ValueError as exc:
            return _err(str(exc))
        except Exception as exc:
            return _err(f"audit report write failed: {exc}")

        # Update the role row + tasks.latest_audit_* surface fields.
        executor_owner = None
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE task_role_assignments "
                "SET report_path = ?, verdict = ?, completed_at = ? "
                "WHERE id = ?",
                (rel, verdict, submitted_at, assignment_id),
            )
            await c.execute(
                "UPDATE tasks SET latest_audit_report_path = ?, "
                "latest_audit_kind = ?, latest_audit_verdict = ? "
                "WHERE id = ? AND project_id = ?",
                (rel, kind, verdict, task_id, project_id),
            )
            cur = await c.execute(
                "SELECT owner FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            task_row = await cur.fetchone()
            if task_row:
                executor_owner = dict(task_row).get("owner")
            await c.commit()
        finally:
            await c.close()

        # Emit the kanban-driving event. The auto-advance subscriber
        # (server/kanban.py — landing in a future iteration) listens.
        ts = _now_iso()
        await bus.publish(
            {
                "ts": ts,
                "agent_id": caller_id,
                "type": "task_role_completed",
                "task_id": task_id,
                "role": role,
                "owner": caller_id,
                "artifact_path": rel,
                "verdict": verdict,
                "to": executor_owner,
            }
        )
        await bus.publish(
            {
                "ts": ts,
                "agent_id": caller_id,
                "type": "audit_report_submitted",
                "task_id": task_id,
                "kind": kind,
                "verdict": verdict,
                "report_path": rel,
                "round": round_num,
                "auditor_id": caller_id,
                "to": executor_owner,
                # 'to' = executor — surfaces the event in their pane so
                # they see fail verdicts immediately. Read from tasks.owner.
            }
        )
        return _ok(
            f"submitted {kind} audit (round {round_num}, {verdict}) "
            f"for {task_id} → {rel}"
        )

    @tool(
        "coord_mark_shipped",
        (
            "Player-only. Call after merging the task's PR / pushing the "
            "release. Validates you have an active shipper assignment for "
            "this task. Triggers the kanban subscriber to move the task "
            "from `ship` → `archive`.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- note: optional one-line note (e.g. merge SHA / PR URL)"
        ),
        {"task_id": str, "note": str},
    )
    async def mark_shipped(args: dict[str, Any]) -> dict[str, Any]:
        if caller_is_coach:
            return _err("Coach doesn't ship — assign a shipper Player.")
        task_id = (args.get("task_id") or "").strip()
        note = (args.get("note") or "").strip()
        if not task_id:
            return _err("task_id is required")

        project_id = await resolve_active_project()
        executor_owner = None
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id FROM task_role_assignments "
                "WHERE task_id = ? AND role = 'shipper' AND owner = ? "
                "AND completed_at IS NULL AND superseded_by IS NULL "
                "ORDER BY assigned_at DESC LIMIT 1",
                (task_id, caller_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(
                    f"no active shipper assignment for {caller_id} on "
                    f"task {task_id}. Coach must call coord_assign_shipper."
                )
            assignment_id = dict(row)["id"]
            cur = await c.execute(
                "SELECT owner FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            task_row = await cur.fetchone()
            if task_row:
                executor_owner = dict(task_row).get("owner")
            now = _now_iso()
            await c.execute(
                "UPDATE task_role_assignments SET completed_at = ? "
                "WHERE id = ?",
                (now, assignment_id),
            )
            await c.commit()
        finally:
            await c.close()

        ts = _now_iso()
        await bus.publish(
            {
                "ts": ts,
                "agent_id": caller_id,
                "type": "task_role_completed",
                "task_id": task_id,
                "role": "shipper",
                "owner": caller_id,
                "to": executor_owner,
            }
        )
        await bus.publish(
            {
                "ts": ts,
                "agent_id": caller_id,
                "type": "task_shipped",
                "task_id": task_id,
                "shipper_id": caller_id,
                "note": note or None,
                "to": executor_owner,
            }
        )
        return _ok(f"marked {task_id} shipped" + (f" — {note}" if note else ""))

    @tool(
        "coord_my_assignments",
        (
            "Player-only. Returns your full plate in four buckets:\n"
            "  1. Active executor task (the one in agents.current_task_id)\n"
            "  2. Pending auditor assignments (syntax + semantics)\n"
            "  3. Pending shipper assignments\n"
            "  4. Eligible-pool tasks you could claim\n"
            "\n"
            "Call at turn start when you're not sure what to do. Returns "
            "an empty plate if nothing is on you (and the idle poller "
            "will eventually wake you when there's pool work)."
        ),
        {},
    )
    async def my_assignments(args: dict[str, Any]) -> dict[str, Any]:
        if caller_is_coach:
            return _err(
                "coord_my_assignments is Player-only — Coach uses "
                "coord_list_tasks to see the full board."
            )
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            # Bucket 1: active executor task.
            cur = await c.execute(
                "SELECT a.current_task_id, t.title, t.status, t.priority, "
                "t.complexity, t.spec_path "
                "FROM agents a LEFT JOIN tasks t ON t.id = a.current_task_id "
                "WHERE a.id = ?",
                (caller_id,),
            )
            arow = await cur.fetchone()
            executor_task = None
            if arow:
                ad = dict(arow)
                if ad.get("current_task_id"):
                    executor_task = {
                        "id": ad["current_task_id"],
                        "title": ad.get("title") or "(unknown)",
                        "status": ad.get("status") or "?",
                        "priority": ad.get("priority") or "normal",
                        "complexity": ad.get("complexity") or "standard",
                        "has_spec": bool(ad.get("spec_path")),
                    }

            # Bucket 2 + 3: pending auditor + shipper assignments.
            cur = await c.execute(
                "SELECT r.task_id, r.role, r.assigned_at, t.title, t.priority "
                "FROM task_role_assignments r "
                "JOIN tasks t ON t.id = r.task_id "
                "WHERE r.owner = ? AND r.role IN "
                "  ('auditor_syntax','auditor_semantics','shipper') "
                "AND r.completed_at IS NULL AND r.superseded_by IS NULL "
                "AND t.project_id = ? "
                "ORDER BY r.assigned_at",
                (caller_id, project_id),
            )
            pending_audits: list[dict[str, Any]] = []
            pending_ships: list[dict[str, Any]] = []
            for r in await cur.fetchall():
                rd = dict(r)
                entry = {
                    "task_id": rd["task_id"],
                    "title": rd["title"],
                    "priority": rd["priority"],
                    "assigned_at": rd["assigned_at"],
                }
                if rd["role"] == "shipper":
                    pending_ships.append(entry)
                else:
                    entry["kind"] = _audit_kind_from_role(rd["role"])
                    pending_audits.append(entry)

            # Bucket 4: eligible-pool tasks. JSON1 json_each scans the
            # eligible_owners array; cheap because we already filter on
            # role + status.
            cur = await c.execute(
                "SELECT DISTINCT r.task_id, r.role, r.eligible_owners, "
                "t.title, t.priority, t.complexity "
                "FROM task_role_assignments r "
                "JOIN tasks t ON t.id = r.task_id, "
                "json_each(r.eligible_owners) je "
                "WHERE je.value = ? "
                "AND r.owner IS NULL "
                "AND r.completed_at IS NULL AND r.superseded_by IS NULL "
                "AND t.project_id = ? "
                "ORDER BY r.assigned_at",
                (caller_id, project_id),
            )
            eligible: list[dict[str, Any]] = []
            for r in await cur.fetchall():
                rd = dict(r)
                eligible.append({
                    "task_id": rd["task_id"],
                    "title": rd["title"],
                    "priority": rd["priority"],
                    "complexity": rd["complexity"],
                    "role": rd["role"],
                })
        finally:
            await c.close()

        # Compose a concise text response.
        lines: list[str] = []
        if executor_task:
            spec_marker = "" if executor_task["has_spec"] else " [no spec]"
            lines.append(
                f"## Executor: {executor_task['id']} "
                f"\"{executor_task['title']}\" "
                f"(stage={executor_task['status']}, "
                f"pri={executor_task['priority']}, "
                f"complexity={executor_task['complexity']}{spec_marker})"
            )
        else:
            lines.append("## Executor: (none — you have no active task)")

        lines.append("")
        lines.append("## Pending audits:")
        if pending_audits:
            for e in pending_audits:
                lines.append(
                    f"  - {e['task_id']} ({e['kind']}, pri={e['priority']}): "
                    f"{e['title']}"
                )
        else:
            lines.append("  (none)")

        lines.append("")
        lines.append("## Pending ship assignments:")
        if pending_ships:
            for e in pending_ships:
                lines.append(
                    f"  - {e['task_id']} (pri={e['priority']}): {e['title']}"
                )
        else:
            lines.append("  (none)")

        lines.append("")
        lines.append("## Available to claim (eligible pools):")
        if eligible:
            for e in eligible:
                role_label = {
                    "executor": "executor",
                    "auditor_syntax": "syntax auditor",
                    "auditor_semantics": "semantic auditor",
                    "shipper": "shipper",
                    "planner": "planner",
                }.get(e["role"], e["role"])
                simple = " SIMPLE" if e["complexity"] == "simple" else ""
                lines.append(
                    f"  - {e['task_id']} ({role_label}, "
                    f"pri={e['priority']}{simple}): {e['title']}"
                )
        else:
            lines.append("  (none)")

        return _ok("\n".join(lines))

    @tool(
        "coord_set_task_complexity",
        (
            "Coach-only. Mark a task simple or standard. Simple tasks "
            "skip audit + ship — they go plan → execute → archive on "
            "commit, and the executor self-audits. Use simple for typo "
            "fixes, log-message tweaks, single-line bug fixes — anything "
            "well-bounded enough that the executor's diligence is enough "
            "review. Default at create time is 'standard'.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- complexity: 'simple' or 'standard'"
        ),
        {"task_id": str, "complexity": str},
    )
    async def set_task_complexity(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach can set task complexity.")
        task_id = (args.get("task_id") or "").strip()
        complexity = (args.get("complexity") or "").strip().lower()
        if not task_id:
            return _err("task_id is required")
        if complexity not in ("simple", "standard"):
            return _err("complexity must be 'simple' or 'standard'")
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT owner FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            owner = dict(row).get("owner")
            await c.execute(
                "UPDATE tasks SET complexity = ? "
                "WHERE id = ? AND project_id = ?",
                (complexity, task_id, project_id),
            )
            await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_complexity_set",
                "task_id": task_id,
                "complexity": complexity,
                "to": owner,
            }
        )
        return _ok(f"task {task_id} → complexity={complexity}")

    @tool(
        "coord_advance_task_stage",
        (
            "Coach-only. Manually move a task to a new kanban stage, "
            "bypassing the role-completion gate. Use when an audit or "
            "ship assignment is stuck and you need to push the task "
            "through (e.g. an auditor went silent and you've decided "
            "the work is good enough to advance).\n"
            "\n"
            "Validates the transition against the state machine "
            "(plan → execute → audit_syntax → audit_semantics → ship → "
            "archive, plus revert audit_*→execute and execute→archive).\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- stage: target stage (kanban value)\n"
            "- note: optional reason; rendered on the timeline + carried "
            "in the task_stage_changed event"
        ),
        {"task_id": str, "stage": str, "note": str},
    )
    async def advance_task_stage(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach can force a stage transition.")
        task_id = (args.get("task_id") or "").strip()
        stage = (args.get("stage") or "").strip().lower()
        note = (args.get("note") or "").strip()
        if not task_id:
            return _err("task_id is required")
        if stage not in ALL_KANBAN_STAGES:
            return _err(
                f"invalid stage '{stage}' (must be one of "
                f"{sorted(ALL_KANBAN_STAGES)})"
            )
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status, owner FROM tasks "
                "WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            t = dict(row)
            old_status = t["status"]
            owner = t.get("owner")
            if not _valid_transition(old_status, stage):
                return _err(
                    f"invalid transition: {old_status} → {stage}"
                )
            now = _now_iso()
            if stage == "archive":
                await c.execute(
                    "UPDATE tasks SET status = 'archive', "
                    "completed_at = ?, archived_at = ? "
                    "WHERE id = ? AND project_id = ?",
                    (now, now, task_id, project_id),
                )
                if owner:
                    await c.execute(
                        "UPDATE agents SET current_task_id = NULL "
                        "WHERE id = ? AND current_task_id = ?",
                        (owner, task_id),
                    )
            else:
                await c.execute(
                    "UPDATE tasks SET status = ? "
                    "WHERE id = ? AND project_id = ?",
                    (stage, task_id, project_id),
                )
            await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_stage_changed",
                "task_id": task_id,
                "from": old_status,
                "to": stage,
                "reason": "manual",
                "note": note or None,
                "owner": owner,
            }
        )
        return _ok(
            f"advanced {task_id}: {old_status} → {stage}"
            + (f" — {note}" if note else "")
        )

    @tool(
        "coord_set_task_blocked",
        (
            "Toggle the orthogonal blocked flag on a task. Owner + Coach "
            "only. Blocked tasks stay in their current stage but are "
            "rendered with a BLOCKED badge so the human / Coach knows "
            "to unstick them. Use for 'waiting on stakeholder', "
            "'external API down', etc.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- blocked: 'true' or 'false'\n"
            "- reason: short note shown on the card (max 500 chars)"
        ),
        {"task_id": str, "blocked": str, "reason": str},
    )
    async def set_task_blocked(args: dict[str, Any]) -> dict[str, Any]:
        task_id = (args.get("task_id") or "").strip()
        blocked_raw = str(args.get("blocked") or "").strip().lower()
        reason = (args.get("reason") or "").strip()
        if not task_id:
            return _err("task_id is required")
        if blocked_raw in ("true", "1", "yes", "on"):
            blocked = 1
        elif blocked_raw in ("false", "0", "no", "off"):
            blocked = 0
        else:
            return _err("blocked must be 'true' or 'false'")
        if len(reason) > 500:
            return _err(f"reason too long ({len(reason)} chars, max 500)")
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT owner, status FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            task_row = dict(row)
            owner = task_row.get("owner")
            if task_row.get("status") == "archive":
                return _err(
                    f"task {task_id} is archived; archived tasks are read-only."
                )
            if not caller_is_coach and owner != caller_id:
                return _err(
                    f"only the task's owner ({owner or 'nobody'}) or "
                    f"Coach can set the blocked flag."
                )
            await c.execute(
                "UPDATE tasks SET blocked = ?, "
                "blocked_reason = ? "
                "WHERE id = ? AND project_id = ?",
                (blocked, reason or None, task_id, project_id),
            )
            await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_blocked_changed",
                "task_id": task_id,
                "blocked": bool(blocked),
                "reason": reason or None,
                "to": owner,
            }
        )
        return _ok(
            f"task {task_id} blocked={'true' if blocked else 'false'}"
            + (f" — {reason}" if reason else "")
        )

    _tools = [
        list_tasks,
        create_task,
        claim_task,
        update_task,
        assign_task,
        # Kanban additions
        write_task_spec,
        assign_planner,
        assign_auditor,
        assign_shipper,
        submit_audit_report,
        mark_shipped,
        my_assignments,
        set_task_complexity,
        advance_task_stage,
        set_task_blocked,
        send_message,
        read_inbox,
        list_memory,
        read_memory,
        update_memory,
        commit_push,
        write_decision,
        propose_file_write,
        read_file,
        write_knowledge,
        read_knowledge,
        list_knowledge,
        list_team,
        set_player_role,
        set_player_runtime,
        set_player_model,
        set_player_effort,
        set_player_plan_mode,
        get_player_settings,
        answer_question,
        answer_plan,
        request_human,
        add_todo,
        complete_todo,
        update_todo,
        compass_ask,
        compass_audit,
        compass_brief,
        compass_status,
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


# Reasoning-effort tier labels — keyed by the int stored on
# agent_project_roles.effort_override (and on the per-pane request).
# Mirrors agents._EFFORT_LEVELS but lives here so the coord-tool layer
# can render labels without importing from agents (cyclic).
_EFFORT_VALUE_LABELS = {1: "low", 2: "medium", 3: "high", 4: "max"}


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
    "mcp__coord__coord_propose_file_write",
    "mcp__coord__coord_read_file",
    "mcp__coord__coord_write_knowledge",
    "mcp__coord__coord_read_knowledge",
    "mcp__coord__coord_list_knowledge",
    "mcp__coord__coord_save_output",
    "mcp__coord__coord_list_team",
    "mcp__coord__coord_set_player_role",
    "mcp__coord__coord_set_player_runtime",
    "mcp__coord__coord_set_player_model",
    "mcp__coord__coord_set_player_effort",
    "mcp__coord__coord_set_player_plan_mode",
    "mcp__coord__coord_get_player_settings",
    "mcp__coord__coord_answer_question",
    "mcp__coord__coord_answer_plan",
    "mcp__coord__coord_request_human",
    "mcp__coord__coord_add_todo",
    "mcp__coord__coord_complete_todo",
    "mcp__coord__coord_update_todo",
    # Compass — Coach-only at runtime; included in the allowlist for
    # both roles so the SDK doesn't pre-reject the call. The
    # caller_is_coach gate inside each handler is what enforces the
    # Coach-only invariant.
    "mcp__coord__compass_ask",
    "mcp__coord__compass_audit",
    "mcp__coord__compass_brief",
    "mcp__coord__compass_status",
    # Kanban lifecycle — Docs/kanban-specs.md §6. Same convention as
    # compass: Coach-only assignment/meta tools are listed here too so
    # the SDK doesn't pre-reject; the caller_is_coach gate inside each
    # handler enforces the Coach-only invariant. Player-only tools
    # (coord_my_assignments / coord_submit_audit_report /
    # coord_mark_shipped) validate the caller has the relevant role
    # assignment in their handler.
    "mcp__coord__coord_write_task_spec",
    "mcp__coord__coord_assign_planner",
    "mcp__coord__coord_assign_auditor",
    "mcp__coord__coord_assign_shipper",
    "mcp__coord__coord_submit_audit_report",
    "mcp__coord__coord_mark_shipped",
    "mcp__coord__coord_my_assignments",
    "mcp__coord__coord_set_task_complexity",
    "mcp__coord__coord_advance_task_stage",
    "mcp__coord__coord_set_task_blocked",
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

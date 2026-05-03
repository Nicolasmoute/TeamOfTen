"""Kanban auto-advance subscriber (Docs/kanban-specs.md §9).

A small bus subscriber wired in `main.py:lifespan` next to the
audit-watcher / telegram bridge. Watches the event bus for the
events that should drive a stage transition and applies the
transition atomically + emits `task_stage_changed`. The MCP tools
never call into this module directly — Coach + Players just emit
events through their normal tool flow, and this subscriber does
the bookkeeping.

Triggers (mirroring §9 of the spec):

  1. `commit_pushed` with `task_id` → executor's commit signals
     end-of-execute. Standard task → audit_syntax. Simple task →
     archive (skips audit + ship per the simple-complexity policy).

  2. `audit_report_submitted{kind=syntax, verdict=pass}` →
     audit_syntax → audit_semantics.

  3. `audit_report_submitted{kind=syntax, verdict=fail}` →
     audit_syntax → execute. The executor is auto-woken with the
     spec + the latest audit report attached.

  4. `audit_report_submitted{kind=semantics, verdict=pass}` →
     audit_semantics → ship.

  5. `audit_report_submitted{kind=semantics, verdict=fail}` →
     audit_semantics → execute.

  6. `task_shipped` (shipper called coord_mark_shipped) →
     ship → archive.

  7. `compass_audit_logged` is informational. The subscriber writes
     the `compass_audit_report_path` + `compass_audit_verdict`
     columns onto the task whose latest commit was being audited
     (correlated by `commit_pushed` → audit chain), but does NOT
     change the task's stage. The Player auditor is the gate.

Failure isolation: per-event `try/except` so a single bad event
doesn't kill the subscriber. Feature flag
`HARNESS_KANBAN_AUTO_ADVANCE` (default true) can disable the whole
subscriber on cost-constrained deploys — events still emit, the
subscriber just doesn't react. Lifecycle owned by the module
(`start_kanban_subscriber` / `stop_kanban_subscriber` / `is_running`)
mirrors the audit-watcher pattern.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

from server.db import configured_conn
from server.events import bus

logger = logging.getLogger("harness.kanban")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


WATCHED_EVENT_TYPES: frozenset[str] = frozenset({
    "commit_pushed",
    "audit_report_submitted",
    "task_shipped",
    "compass_audit_logged",
})


# ---------------------------------------------------------------- state

_current_task: asyncio.Task[None] | None = None
_stopping = False
# Cache the most-recent commit_pushed task_id per (sha) so a later
# compass_audit_logged event for the same artifact can be correlated
# back to the task. Compass audits the artifact text, not the sha
# directly, but its event payload includes the audit_id and the
# kanban subscriber can read the mapping the audit-watcher sets up.
# (Simple in-memory cache — a missed correlation just means the
# `compass_audit_*` columns on the task stay NULL until a fresh
# commit + audit cycle.)
_recent_commit_task: dict[str, str] = {}


def is_running() -> bool:
    return _current_task is not None and not _current_task.done()


def _flag_enabled() -> bool:
    """`HARNESS_KANBAN_AUTO_ADVANCE` defaults to true. Anything that
    parses to a falsy boolean disables the subscriber."""
    raw = os.environ.get("HARNESS_KANBAN_AUTO_ADVANCE", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


# ---------------------------------------------------------------- lifecycle


async def start_kanban_subscriber() -> None:
    """Start the background subscriber. Idempotent. No-op when the
    feature flag is off.

    Subscribes synchronously before scheduling the consumer task so
    events fired in the create_task race window aren't dropped —
    same pattern as the audit-watcher.
    """
    global _current_task, _stopping
    if not _flag_enabled():
        logger.info(
            "kanban: subscriber disabled (HARNESS_KANBAN_AUTO_ADVANCE)"
        )
        return
    if is_running():
        return
    _stopping = False
    queue = bus.subscribe()
    loop = asyncio.get_running_loop()
    _current_task = loop.create_task(
        _run(queue), name="harness.kanban.subscriber",
    )
    logger.info(
        "kanban: subscriber started (types=%s)",
        sorted(WATCHED_EVENT_TYPES),
    )


async def stop_kanban_subscriber(timeout: float = 2.0) -> None:
    """Stop the subscriber. Idempotent."""
    global _current_task, _stopping
    _stopping = True
    task = _current_task
    if task is None:
        return
    if not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _current_task = None


# ---------------------------------------------------------------- core


async def _run(queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Drain the queue, dispatch matching events. Per-event isolation."""
    try:
        while not _stopping:
            try:
                ev = await queue.get()
            except asyncio.CancelledError:
                return
            try:
                await _handle_event(ev)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "kanban: handler crashed on event %r", ev.get("type")
                )
    finally:
        bus.unsubscribe(queue)


async def _handle_event(ev: dict[str, Any]) -> None:
    etype = ev.get("type") or ""
    if etype not in WATCHED_EVENT_TYPES:
        return
    if etype == "commit_pushed":
        await _on_commit_pushed(ev)
    elif etype == "audit_report_submitted":
        await _on_audit_submitted(ev)
    elif etype == "task_shipped":
        await _on_task_shipped(ev)
    elif etype == "compass_audit_logged":
        await _on_compass_audit_logged(ev)


# ---------------------------------------------------------------- handlers


async def _on_commit_pushed(ev: dict[str, Any]) -> None:
    """Auto-advance execute → audit_syntax (standard) or → archive
    (simple). Cache the (sha, task_id) mapping so a subsequent
    `compass_audit_logged` event can attach itself to the right task.
    """
    task_id = (ev.get("task_id") or "").strip()
    sha = (ev.get("sha") or "").strip()
    if not task_id:
        return
    if sha:
        # Bound the cache to ~200 entries; old shas are very unlikely
        # to receive a Compass audit so eviction is cheap.
        if len(_recent_commit_task) > 200:
            _recent_commit_task.pop(next(iter(_recent_commit_task)), None)
        _recent_commit_task[sha] = task_id

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, complexity, owner, project_id "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return
    t = dict(row)
    if t["status"] != "execute":
        # Commit landed but the task isn't in the execute stage — could
        # be a follow-up commit on an already-archived task, or a
        # standard task that was force-advanced past audit. Don't move.
        return

    if t["complexity"] == "simple":
        await _transition(
            task_id=task_id,
            new_status="archive",
            reason="commit_pushed",
            owner=t["owner"],
            project_id=t["project_id"],
        )
    else:
        await _transition(
            task_id=task_id,
            new_status="audit_syntax",
            reason="commit_pushed",
            owner=t["owner"],
            project_id=t["project_id"],
        )
        # Nudge Coach to assign the syntax auditor if no active row exists.
        if not await _has_active_role(task_id, "auditor_syntax"):
            await _emit_assignment_needed(
                task_id=task_id, role="auditor_syntax", to_owner=t["owner"]
            )


async def _on_audit_submitted(ev: dict[str, Any]) -> None:
    """Pass → next stage; fail → revert to execute. Auto-wake the
    next assignee or executor."""
    task_id = (ev.get("task_id") or "").strip()
    kind = (ev.get("kind") or "").strip().lower()
    verdict = (ev.get("verdict") or "").strip().lower()
    if not task_id or kind not in ("syntax", "semantics") or verdict not in ("pass", "fail"):
        return

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, owner, project_id FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return
    t = dict(row)

    expected_stage = f"audit_{kind}"
    if t["status"] != expected_stage:
        # The task moved to a different stage (force-advance, cancel,
        # etc.) since the audit was assigned — drop the transition.
        return

    if verdict == "pass":
        if kind == "syntax":
            new_status = "audit_semantics"
            next_role = "auditor_semantics"
        else:
            new_status = "ship"
            next_role = "shipper"
        await _transition(
            task_id=task_id,
            new_status=new_status,
            reason="audit_pass",
            owner=t["owner"],
            project_id=t["project_id"],
        )
        if not await _has_active_role(task_id, next_role):
            await _emit_assignment_needed(
                task_id=task_id, role=next_role, to_owner=t["owner"]
            )
    else:
        # Fail → revert to execute. Reset started_at so the card
        # flips to "assigned, not started" again until the executor's
        # auto-wake actually fires.
        await _transition(
            task_id=task_id,
            new_status="execute",
            reason="audit_fail",
            owner=t["owner"],
            project_id=t["project_id"],
            reset_started_at=True,
        )
        # Re-wake the executor with the spec + latest report.
        await _wake_executor_for_revert(
            task_id=task_id, owner=t["owner"], kind=kind,
            report_path=(ev.get("report_path") or ""),
            round_num=int(ev.get("round") or 1),
        )


async def _on_task_shipped(ev: dict[str, Any]) -> None:
    """ship → archive."""
    task_id = (ev.get("task_id") or "").strip()
    if not task_id:
        return
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, owner, project_id FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return
    t = dict(row)
    if t["status"] != "ship":
        return
    await _transition(
        task_id=task_id,
        new_status="archive",
        reason="shipped",
        owner=t["owner"],
        project_id=t["project_id"],
    )


async def _on_compass_audit_logged(ev: dict[str, Any]) -> None:
    """Informational mirror — writes `compass_audit_report_path` +
    `compass_audit_verdict` onto the task whose most recent commit
    was being audited. Does NOT change task stage (the Player auditor
    is the gate).

    Correlation: the `compass_audit_logged` event doesn't carry
    `task_id` directly, but `audit_work` is currently invoked from
    the audit-watcher on `commit_pushed`, and we cached the
    `(sha, task_id)` mapping when that commit_pushed fired. Lookup is
    best-effort — a missed correlation just means the task's
    `compass_audit_*` columns stay NULL.
    """
    audit_id = (ev.get("audit_id") or "").strip()
    verdict = (ev.get("verdict") or "").strip()
    report_path = (ev.get("report_path") or "").strip()
    if not audit_id or not verdict:
        return

    # The audit-watcher sets cmp_audit's project_id; we don't have a
    # direct sha mapping here. As a heuristic, attach the latest
    # Compass result to whichever task corresponds to the most recent
    # cached commit. If the cache is empty (no commit has been seen
    # since boot), the result lands as orphan informational metadata
    # in audits.jsonl + the .md file but no task gets its column
    # updated. That's acceptable — the dashboard's audit log still
    # shows the verdict; the kanban card just won't link Compass's
    # take.
    if not _recent_commit_task:
        return
    # Correlate to the most-recently cached commit for the project.
    # Without per-project segregation in the cache, this attaches to
    # the latest commit globally — defensible for a single-active-
    # project deploy; revisit if multi-project parallelism becomes
    # the norm.
    last_sha = next(reversed(_recent_commit_task))
    task_id = _recent_commit_task[last_sha]

    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE tasks SET compass_audit_report_path = ?, "
            "compass_audit_verdict = ? WHERE id = ?",
            (report_path or None, verdict, task_id),
        )
        await c.commit()
    finally:
        await c.close()


# ---------------------------------------------------------------- helpers


async def _transition(
    *,
    task_id: str,
    new_status: str,
    reason: str,
    owner: str | None,
    project_id: str,
    reset_started_at: bool = False,
) -> None:
    """Apply a stage change atomically + emit task_stage_changed.

    `reset_started_at=True` on audit-fail reverts so the card flips to
    'assigned, not started' (executor's avatar goes hollow) until the
    auto-wake actually spawns the next turn.
    """
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status FROM tasks WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        )
        row = await cur.fetchone()
        if not row:
            return
        old_status = dict(row)["status"]
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        if new_status == "archive":
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
        elif reset_started_at:
            await c.execute(
                "UPDATE tasks SET status = ?, started_at = NULL "
                "WHERE id = ? AND project_id = ?",
                (new_status, task_id, project_id),
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

    await bus.publish({
        "ts": now,
        "agent_id": "system",
        "type": "task_stage_changed",
        "task_id": task_id,
        "from": old_status,
        "to": new_status,
        "reason": reason,
        "owner": owner,
    })


async def _has_active_role(task_id: str, role: str) -> bool:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT 1 FROM task_role_assignments "
            "WHERE task_id = ? AND role = ? "
            "AND completed_at IS NULL AND superseded_by IS NULL "
            "LIMIT 1",
            (task_id, role),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    return row is not None


async def _emit_assignment_needed(
    *, task_id: str, role: str, to_owner: str | None
) -> None:
    from datetime import datetime, timezone
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "audit_assignment_needed",
        "task_id": task_id,
        "role": role,
        # Nudge Coach — the assignment-needed surface is in their pane.
        "to": "coach",
        "owner": to_owner,
    })


async def _wake_executor_for_revert(
    *, task_id: str, owner: str | None, kind: str,
    report_path: str, round_num: int,
) -> None:
    """Re-wake the executor with the spec + the latest audit report
    attached. Late import to avoid the kanban↔agents circular dep."""
    if not owner:
        return
    try:
        from server.agents import maybe_wake_agent
        report_hint = (
            f"\n\nLatest audit report: {report_path}"
            if report_path else ""
        )
        wake_prompt = (
            f"Audit failed for {task_id} ({kind}, round {round_num}). "
            f"Read your spec.md and the latest audit report, fix what "
            f"the auditor flagged, then commit again with "
            f"coord_commit_push(task_id={task_id!r}, ...).{report_hint}"
        )
        await maybe_wake_agent(owner, wake_prompt, bypass_debounce=True)
    except Exception:
        logger.exception(
            "kanban: failed to wake executor %s for revert on %s",
            owner, task_id,
        )


__all__ = [
    "start_kanban_subscriber",
    "stop_kanban_subscriber",
    "is_running",
    "WATCHED_EVENT_TYPES",
]

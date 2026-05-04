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
     end-of-execute. The next stage is read from the task's
     trajectory column via `_next_stage(stages, "execute")` —
     archives directly when the trajectory has no stage after
     execute.

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
import json
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
    "task_execution_completed",
    "audit_report_submitted",
    "task_shipped",
    "compass_audit_logged",
    "task_stage_changed",
    "task_spec_written",
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

# v0.3 audit-2026-05-04 item 12: per-project tail of recent commits
# so a multi-project deploy attaches each Compass result to the
# correct project's latest commit instead of the global tail. Each
# entry is a (sha, task_id) pair; we only need the last one per
# project for the heuristic.
_recent_commit_per_project: dict[str, tuple[str, str]] = {}

# Last event timestamp the subscriber processed (any watched event,
# successfully or not). In-memory only — read by the /api/tasks/flow_health
# endpoint to surface "is the engine moving" without scraping events.
_subscriber_last_event_at: str | None = None


def is_running() -> bool:
    return _current_task is not None and not _current_task.done()


def subscriber_last_event_at() -> str | None:
    """ISO timestamp of the last event the subscriber consumed since
    process start, or None if it hasn't seen one yet. Read by the
    flow-health endpoint."""
    return _subscriber_last_event_at


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
    global _subscriber_last_event_at
    try:
        while not _stopping:
            try:
                ev = await queue.get()
            except asyncio.CancelledError:
                return
            etype = ev.get("type") or ""
            if etype in WATCHED_EVENT_TYPES:
                from datetime import datetime, timezone
                _subscriber_last_event_at = (
                    datetime.now(timezone.utc).isoformat()
                )
            try:
                await _handle_event(ev)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "kanban: handler crashed on event %r", etype
                )
    finally:
        bus.unsubscribe(queue)


async def _handle_event(ev: dict[str, Any]) -> None:
    etype = ev.get("type") or ""
    if etype not in WATCHED_EVENT_TYPES:
        return
    if etype == "commit_pushed":
        await _on_commit_pushed(ev)
    elif etype == "task_execution_completed":
        await _on_task_execution_completed(ev)
    elif etype == "audit_report_submitted":
        await _on_audit_submitted(ev)
    elif etype == "task_shipped":
        await _on_task_shipped(ev)
    elif etype == "compass_audit_logged":
        await _on_compass_audit_logged(ev)
    elif etype == "task_stage_changed":
        await _on_stage_changed(ev)
    elif etype == "task_spec_written":
        await _on_spec_written(ev)


# ---------------------------------------------------------------- handlers


async def _on_commit_pushed(ev: dict[str, Any]) -> None:
    """Auto-route after code execution completes.

    The current workflow route can skip formal review, skip semantic
    review, or archive simple self-audit tasks directly.
    """
    task_id = (ev.get("task_id") or "").strip()
    sha = (ev.get("sha") or "").strip()
    project_id = (ev.get("project_id") or "").strip()
    if not task_id:
        return
    if sha:
        if len(_recent_commit_task) > 200:
            _recent_commit_task.pop(next(iter(_recent_commit_task)), None)
        _recent_commit_task[sha] = task_id
        # Remember the most-recent commit per project for the Compass
        # correlation heuristic (audit-2026-05-04 item 12).
        if project_id:
            _recent_commit_per_project[project_id] = (sha, task_id)
    await _advance_after_execute_completion(task_id, reason="commit_pushed")


async def _on_task_execution_completed(ev: dict[str, Any]) -> None:
    """Auto-route after non-git execution completion."""
    task_id = (ev.get("task_id") or "").strip()
    if not task_id:
        return
    await _advance_after_execute_completion(
        task_id, reason="task_execution_completed"
    )


async def _advance_after_execute_completion(
    task_id: str, *, reason: str
) -> None:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, trajectory, owner, project_id "
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
        return
    stages = _trajectory_stages(t)
    next_stage = _next_stage(stages, "execute")
    await _transition(
        task_id=task_id,
        new_status=next_stage,
        reason=reason,
        owner=t["owner"],
        project_id=t["project_id"],
    )


async def _on_audit_submitted(ev: dict[str, Any]) -> None:
    """Pass → next stage; fail → revert to execute. Auto-wake the
    next assignee or executor.

    On every fail also publishes an `audit_fail_notification` event
    routed to Coach. Coach treats the first fail of any kind as
    expected correction noise; the `escalate=True` flag fires on the
    second fail of the same kind, signalling Coach to consider an
    effort/model bump on the executor (see kanban-specs.md §17)."""
    task_id = (ev.get("task_id") or "").strip()
    kind = (ev.get("kind") or "").strip().lower()
    verdict = (ev.get("verdict") or "").strip().lower()
    if not task_id or kind not in ("syntax", "semantics") or verdict not in ("pass", "fail"):
        return

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, owner, project_id, trajectory "
            "FROM tasks WHERE id = ?",
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
    stages = _trajectory_stages(t)

    if verdict == "pass":
        new_status = _next_stage(stages, expected_stage)
        await _transition(
            task_id=task_id,
            new_status=new_status,
            reason="audit_pass",
            owner=t["owner"],
            project_id=t["project_id"],
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
        # Count prior fails of the same kind to compute kind_round +
        # escalate flag for the Coach-bound notification.
        kind_round, escalate = await _count_fails_for_kind(
            task_id=task_id, kind=kind,
        )
        await _emit_audit_fail_notification(
            task_id=task_id,
            kind=kind,
            kind_round=kind_round,
            escalate=escalate,
            auditor_id=ev.get("auditor_id"),
            executor_id=t["owner"],
            report_path=ev.get("report_path") or "",
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


async def _on_spec_written(ev: dict[str, Any]) -> None:
    """`coord_write_task_spec` (or human-side spec write) wrote the
    spec.md, completing the planner role. If the task is in `plan` and
    the trajectory has a stage after `plan`, advance to it.

    Spec gate (kanban-specs.md §3.5): the planner-completion → next-stage
    transition only fires when the next stage exists in the trajectory.
    The wake of the next-stage assignee is handled by the
    `task_stage_changed` listener (`_on_stage_changed` →
    `_wake_role_or_emit_needed`)."""
    task_id = (ev.get("task_id") or "").strip()
    if not task_id:
        return
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, trajectory, owner, project_id "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return
    t = dict(row)
    if t["status"] != "plan":
        # Spec was rewritten on a downstream stage (e.g. mid-execute);
        # nothing to advance.
        return
    stages = _trajectory_stages(t)
    if "plan" not in stages:
        return
    next_stage = _next_stage(stages, "plan")
    # Defensive: validator guarantees execute follows plan in canonical
    # order, but skip plan→archive jumps from corrupted trajectories.
    if next_stage == "archive":
        return
    await _transition(
        task_id=task_id,
        new_status=next_stage,
        reason="spec_written",
        owner=t["owner"],
        project_id=t["project_id"],
    )


async def _on_stage_changed(ev: dict[str, Any]) -> None:
    """Stage-entry activation: wake the current-stage owner/candidates.

    This is the piece that makes the board flow. Coach can reserve later
    roles up front, but Players are only called once the card actually
    reaches their stage.
    """
    task_id = (ev.get("task_id") or "").strip()
    new_stage = (ev.get("to") or "").strip()
    reason = (ev.get("reason") or "").strip()
    if not task_id or new_stage in ("", "archive"):
        return
    if new_stage == "execute" and reason == "audit_fail":
        # _on_audit_submitted sends a richer wake with report context.
        return
    role = _role_for_stage(new_stage)
    if not role:
        return
    await _wake_role_or_emit_needed(task_id=task_id, role=role)


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
    # v0.3 audit-2026-05-04 item 12: prefer per-project correlation.
    # `compass_audit_logged` carries `project_id` (set by audit.py),
    # which lets us attach to the project's most-recent commit
    # instead of the global tail — fixes the wrong-project mis-attach
    # under parallel-project commit activity. Fall back to the global
    # tail only when the event lacks project_id (defensive).
    project_id = (ev.get("project_id") or "").strip()
    task_id: str | None = None
    if project_id and project_id in _recent_commit_per_project:
        task_id = _recent_commit_per_project[project_id][1]
    elif _recent_commit_task:
        last_sha = next(reversed(_recent_commit_task))
        task_id = _recent_commit_task[last_sha]
    if not task_id:
        return

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


_VALID_TRAJECTORY_STAGES = {
    "plan", "execute", "audit_syntax", "audit_semantics", "ship",
}


def _trajectory_stages(task: dict[str, Any]) -> list[str]:
    """Return the ordered list of stage names from `tasks.trajectory`.
    Defensive against malformed JSON; an empty / unparseable trajectory
    yields []. The walker treats absence as "execute is the only stage"
    by routing execute → archive."""
    raw = task.get("trajectory")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for entry in parsed:
        if isinstance(entry, dict):
            stage = str(entry.get("stage", ""))
            if stage in _VALID_TRAJECTORY_STAGES:
                out.append(stage)
    return out


def _next_stage(stages: list[str], current: str) -> str:
    """Walk the trajectory list. Returns the stage that follows
    `current`, or 'archive' if `current` is the last stage in the list
    (or absent)."""
    try:
        idx = stages.index(current)
    except ValueError:
        return "archive"
    if idx + 1 >= len(stages):
        return "archive"
    return stages[idx + 1]


def _role_for_stage(stage: str) -> str | None:
    return {
        "plan": "planner",
        "execute": "executor",
        "audit_syntax": "auditor_syntax",
        "audit_semantics": "auditor_semantics",
        "ship": "shipper",
    }.get(stage)


def _role_label(role: str) -> str:
    return {
        "planner": "planner",
        "executor": "executor",
        "auditor_syntax": "formal reviewer",
        "auditor_semantics": "semantic reviewer",
        "shipper": "shipper",
    }.get(role, role)


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
        # Every transition stamps last_stage_change_at + clears any
        # stale_alert_at (the task is moving again, so the stall
        # sweeper should re-arm rather than suppress next alert).
        if new_status == "archive":
            await c.execute(
                "UPDATE tasks SET status = 'archive', "
                "completed_at = ?, archived_at = ?, "
                "last_stage_change_at = ?, stale_alert_at = NULL "
                "WHERE id = ? AND project_id = ?",
                (now, now, now, task_id, project_id),
            )
            if owner:
                await c.execute(
                    "UPDATE agents SET current_task_id = NULL "
                    "WHERE id = ? AND current_task_id = ?",
                    (owner, task_id),
                )
        elif reset_started_at:
            await c.execute(
                "UPDATE tasks SET status = ?, started_at = NULL, "
                "last_stage_change_at = ?, stale_alert_at = NULL "
                "WHERE id = ? AND project_id = ?",
                (new_status, now, task_id, project_id),
            )
        else:
            await c.execute(
                "UPDATE tasks SET status = ?, "
                "last_stage_change_at = ?, stale_alert_at = NULL "
                "WHERE id = ? AND project_id = ?",
                (new_status, now, task_id, project_id),
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
    *, task_id: str, role: str, stage: str, to_owner: str | None
) -> None:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    payload = {
        "ts": ts,
        "agent_id": "system",
        # v0.3 rename: was `audit_assignment_needed` (audit-only);
        # now covers plan / execute / audit / ship gaps.
        "type": "stage_assignment_needed",
        "task_id": task_id,
        "role": role,
        "stage": stage,
        # Nudge Coach — the assignment-needed surface is in their pane.
        "to": "coach",
        "owner": to_owner,
    }
    await bus.publish(payload)
    # Spec §18 promises a one-release back-compat alias under the
    # legacy name. Subscribers that filtered on the old name still
    # work; new code listens for `stage_assignment_needed`.
    if role in ("auditor_syntax", "auditor_semantics", "shipper"):
        alias = dict(payload)
        alias["type"] = "audit_assignment_needed"
        await bus.publish(alias)


async def _count_fails_for_kind(
    *, task_id: str, kind: str
) -> tuple[int, bool]:
    """Count the number of `verdict='fail'` rows for a given task and
    audit kind across all rounds (active + superseded). Returns
    `(kind_round, escalate)` where `kind_round` is the count and
    `escalate` is True when `kind_round >= 2` — the spec.md §17 trigger
    that surfaces the task in Coach's `## Active task health` rollup."""
    role = f"auditor_{kind}"
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) FROM task_role_assignments "
            "WHERE task_id = ? AND role = ? AND verdict = 'fail'",
            (task_id, role),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    n = int(row[0]) if row else 0
    return n, n >= 2


async def _emit_audit_fail_notification(
    *,
    task_id: str,
    kind: str,
    kind_round: int,
    escalate: bool,
    auditor_id: Any,
    executor_id: str | None,
    report_path: str,
) -> None:
    """Coach-bound notification on every audit fail. See kanban-specs.md
    §8 + §17 for the contract: visibility on every fail, escalation
    only on the second fail of the same kind."""
    from datetime import datetime, timezone
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "audit_fail_notification",
        "task_id": task_id,
        "kind": kind,
        "kind_round": kind_round,
        "escalate": escalate,
        "auditor_id": auditor_id,
        "executor_id": executor_id,
        "report_path": report_path or "",
        "to": "coach",
    })


async def _wake_role_or_emit_needed(*, task_id: str, role: str) -> None:
    """Wake the active owner/candidate pool for a just-entered stage.

    If Coach reserved a role before the stage was active, this is where
    the reservation becomes a call. If there is no active row, Coach gets
    an assignment-needed event.
    """
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT owner, project_id FROM tasks WHERE id = ?",
            (task_id,),
        )
        task_row = await cur.fetchone()
        if not task_row:
            return
        task = dict(task_row)
        cur = await c.execute(
            "SELECT id, owner, eligible_owners FROM task_role_assignments "
            "WHERE task_id = ? AND role = ? "
            "AND completed_at IS NULL AND superseded_by IS NULL "
            "ORDER BY assigned_at DESC LIMIT 1",
            (task_id, role),
        )
        row = await cur.fetchone()
    finally:
        await c.close()

    if not row:
        # v0.3: every stage (not just audit/ship) emits when the role
        # row is missing. Coach needs visibility on plan/execute gaps too.
        stage_for_role = {
            "planner": "plan",
            "executor": "execute",
            "auditor_syntax": "audit_syntax",
            "auditor_semantics": "audit_semantics",
            "shipper": "ship",
        }
        await _emit_assignment_needed(
            task_id=task_id,
            role=role,
            stage=stage_for_role.get(role, ""),
            to_owner=task.get("owner"),
        )
        return

    assignment = dict(row)
    targets: list[str] = []
    if assignment.get("owner"):
        targets = [assignment["owner"]]
    else:
        try:
            parsed = json.loads(assignment.get("eligible_owners") or "[]")
            if isinstance(parsed, list):
                targets = [str(p) for p in parsed if str(p).startswith("p")]
        except Exception:
            targets = []
    if not targets:
        stage_for_role = {
            "planner": "plan",
            "executor": "execute",
            "auditor_syntax": "audit_syntax",
            "auditor_semantics": "audit_semantics",
            "shipper": "ship",
        }
        await _emit_assignment_needed(
            task_id=task_id,
            role=role,
            stage=stage_for_role.get(role, ""),
            to_owner=task.get("owner"),
        )
        return

    role_label = _role_label(role)
    if assignment.get("owner"):
        prompt = (
            f"Task {task_id} has entered your active {role_label} stage. "
            f"Call coord_my_assignments for context, do the role work, "
            f"then use the matching completion tool."
        )
    else:
        prompt = (
            f"Task {task_id} has entered {role_label}. You are in the "
            f"candidate call. If you can take it, call "
            f"coord_accept_role(task_id={task_id!r}, role={role!r}); "
            f"first accepted claim wins. If it is already claimed by the "
            f"time you answer, there is nothing to do."
        )

    try:
        from server.agents import maybe_wake_agent
        for slot in targets:
            try:
                await maybe_wake_agent(slot, prompt, bypass_debounce=True)
            except Exception:
                pass
    except Exception:
        logger.exception("kanban: failed waking %s targets for %s", role, task_id)

    from datetime import datetime, timezone
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "task_role_called",
        "task_id": task_id,
        "role": role,
        "owner": assignment.get("owner"),
        "eligible_owners": targets if not assignment.get("owner") else [],
        "to": assignment.get("owner"),
    })


async def _wake_executor_for_revert(
    *, task_id: str, owner: str | None, kind: str,
    report_path: str, round_num: int,
) -> None:
    """Re-wake the executor with the spec + the latest audit report
    attached. Late import to avoid the kanban↔agents circular dep.

    v0.3 audit-2026-05-04 item 11: read tasks.spec_path from the row
    (was previously implicit "your spec.md") and fall back to the
    `latest_audit_report_path` denorm column when the event payload
    didn't carry the report path. Failed acceptance criteria, when
    extractable from the audit report, are appended verbatim — saves
    the executor from re-reading the whole report when only one
    criterion failed.
    """
    if not owner:
        return
    spec_path: str | None = None
    fallback_report: str | None = None
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT spec_path, latest_audit_report_path "
                "FROM tasks WHERE id = ?",
                (task_id,),
            )
            row = await cur.fetchone()
            if row:
                d = dict(row)
                spec_path = d.get("spec_path")
                fallback_report = d.get("latest_audit_report_path")
        finally:
            await c.close()
    except Exception:
        # Best-effort enrichment — fall back to the bare wake on read failure.
        logger.exception(
            "kanban: failed to enrich revert wake for %s", task_id
        )
    effective_report = report_path or (fallback_report or "")
    failed_criteria = _extract_failed_criteria(effective_report)
    try:
        from server.agents import maybe_wake_agent
        spec_hint = (
            f"\nSpec: {spec_path}" if spec_path else ""
        )
        report_hint = (
            f"\nLatest audit report: {effective_report}"
            if effective_report else ""
        )
        criteria_hint = (
            f"\n\nFailed acceptance criteria:\n{failed_criteria}"
            if failed_criteria else ""
        )
        wake_prompt = (
            f"Audit failed for {task_id} ({kind}, round {round_num}). "
            f"Read the spec and the latest audit report, fix what the "
            f"reviewer flagged, then deliver again with "
            f"coord_commit_push(task_id={task_id!r}, ...) for code or "
            f"coord_complete_execution(task_id={task_id!r}, ...) for "
            f"non-code artifacts."
            f"{spec_hint}{report_hint}{criteria_hint}"
        )
        await maybe_wake_agent(owner, wake_prompt, bypass_debounce=True)
    except Exception:
        logger.exception(
            "kanban: failed to wake executor %s for revert on %s",
            owner, task_id,
        )


def _extract_failed_criteria(report_path: str) -> str:
    """Best-effort: read the audit report markdown and pull out a
    `## Failed criteria` (or similar) section. Returns the section
    body text, or empty string when no such section exists / the
    file can't be read. Conservative on what counts as the section
    header so we don't misclassify content."""
    if not report_path:
        return ""
    # Resolve relative paths against the project root (kanban runs
    # outside any per-project context, so use the data dir prefix).
    try:
        from pathlib import Path
        candidate = Path(report_path)
        if not candidate.is_absolute():
            from server.paths import DATA_ROOT
            candidate = DATA_ROOT / report_path
        if not candidate.is_file():
            return ""
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    headings = (
        "## Failed criteria",
        "## Failed acceptance criteria",
        "## Acceptance criteria failed",
        "### Failed criteria",
    )
    for heading in headings:
        idx = text.find(heading)
        if idx == -1:
            continue
        body_start = idx + len(heading)
        rest = text[body_start:]
        # Stop at the next markdown heading of equal-or-higher level.
        # Both "## " and "# " end the section.
        end_idx = len(rest)
        for marker in ("\n## ", "\n# "):
            m = rest.find(marker)
            if m != -1 and m < end_idx:
                end_idx = m
        section = rest[:end_idx].strip()
        # Cap at a reasonable length so a giant report doesn't blow
        # the wake prompt budget.
        if len(section) > 1500:
            section = section[:1500].rstrip() + "\n... (truncated)"
        return section
    return ""


__all__ = [
    "start_kanban_subscriber",
    "stop_kanban_subscriber",
    "is_running",
    "subscriber_last_event_at",
    "WATCHED_EVENT_TYPES",
]

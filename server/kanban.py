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


async def collect_superseded_role_owners(
    c, *, task_id: str, role: str, new_row_id: int | None
) -> list[str]:
    """Return slot ids that own (or are pooled into) prior active rows
    for this (task_id, role) — i.e. the owners about to be displaced
    by a new assignment. Caller is expected to issue the supersede
    UPDATE itself; this is just a pre-read so the caller can wake the
    displaced slots after committing.

    Empty `new_row_id` means "treat all active rows as displaced"
    (e.g. a hard cancel). When provided, the row with that id is
    excluded so a same-id refresh isn't flagged.
    """
    import json as _json
    if new_row_id is None:
        cur = await c.execute(
            "SELECT owner, eligible_owners FROM task_role_assignments "
            "WHERE task_id = ? AND role = ? "
            "AND completed_at IS NULL AND superseded_by IS NULL",
            (task_id, role),
        )
    else:
        cur = await c.execute(
            "SELECT owner, eligible_owners FROM task_role_assignments "
            "WHERE task_id = ? AND role = ? AND id != ? "
            "AND completed_at IS NULL AND superseded_by IS NULL",
            (task_id, role, new_row_id),
        )
    rows = await cur.fetchall()
    out: list[str] = []
    for r in rows:
        d = dict(r)
        if d.get("owner"):
            out.append(str(d["owner"]))
        else:
            try:
                lst = _json.loads(d.get("eligible_owners") or "[]")
                if isinstance(lst, list):
                    out.extend(str(s) for s in lst if isinstance(s, str))
            except Exception:
                pass
    return out


async def send_role_stand_down(
    *, task_id: str, role: str, displaced: list[str], new_owners: list[str]
) -> list[str]:
    """Wake the displaced role assignees with a stop-work message and
    emit a `task_role_stand_down` event so the supersede is visible in
    the timeline (not just an invisible row update).

    Same-slot refresh is filtered out: a slot in `new_owners` is not
    woken (their next wake comes from `_wake_role_or_emit_needed` for
    the new row anyway). De-dups across multiple displaced rows.

    Returns the list of slots actually woken (post-filter, post-dedup)
    so callers / tests can assert on it.
    """
    if not displaced:
        return []
    new_set = {s for s in (new_owners or []) if isinstance(s, str)}
    woken: list[str] = []
    seen: set[str] = set()
    for slot in displaced:
        if slot in seen or slot in new_set:
            continue
        seen.add(slot)
        woken.append(slot)
    if not woken:
        return []
    role_label = _role_label(role)
    new_label = ", ".join(new_owners) if new_owners else "(unassigned)"
    body = (
        f"Coach reassigned the {role_label} role on task {task_id} "
        f"from you to {new_label}. STOP work on {task_id} now: do not "
        f"edit, commit, push, or publish anything for this task. The "
        f"kanban will not credit further work from you on it. If you "
        f"have local uncommitted changes that may matter, message "
        f"Coach via coord_send_message(to='coach', body='task "
        f"{task_id} stand-down: I had local changes — keep / discard?') "
        f"BEFORE discarding them."
    )
    try:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        await bus.publish({
            "ts": ts,
            "type": "task_role_stand_down",
            "task_id": task_id,
            "role": role,
            "displaced": woken,
            "new_owners": list(new_set),
            "to": "coach",
        })
    except Exception:
        pass
    try:
        from server.agents import maybe_wake_agent
        for slot in woken:
            try:
                await maybe_wake_agent(slot, body, bypass_debounce=True)
            except Exception:
                pass
    except Exception:
        pass
    return woken


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

    v0.3.9: when the transition is `<live_stage> → archive` AND the
    reason is a natural-completion path (the trajectory played out
    end-to-end via shipper / executor / auditor signals — NOT
    Coach-forced 'manual', NOT rung-4 'auto_archive_stalled'), also
    emit a `task_completed` event routed to Coach and wake Coach with
    a prompt to summarize the outcome to the user.
    """
    title: str | None = None
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, title FROM tasks "
            "WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        )
        row = await cur.fetchone()
        if not row:
            return
        rd = dict(row)
        old_status = rd["status"]
        title = rd.get("title")
        # AUDIT FIX (v0.3.9.1): no-op short-circuit. If old_status ==
        # new_status the transition is buggy retry / replay; without
        # this guard the bus.publish would emit a phantom
        # `task_stage_changed{from: X, to: X}` and (for archive) a
        # duplicate `task_completed`, double-waking Coach. Bail
        # silently — caller upstream guards (e.g. _on_task_shipped's
        # `if t["status"] != "ship": return`) already prevent the
        # canonical case; this is the defensive backstop. Connection
        # is closed by the surrounding `finally`.
        if old_status == new_status:
            return
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        # Every transition stamps last_stage_change_at + clears any
        # stale_alert_at (the task is moving again, so the stall
        # sweeper should re-arm rather than suppress next alert).
        if new_status == "archive":
            await c.execute(
                "UPDATE tasks SET status = 'archive', "
                "completed_at = ?, archived_at = ?, "
                "last_stage_change_at = ?, stale_alert_at = NULL, stall_escalation_level = 0 "
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
                "last_stage_change_at = ?, stale_alert_at = NULL, stall_escalation_level = 0 "
                "WHERE id = ? AND project_id = ?",
                (new_status, now, task_id, project_id),
            )
        else:
            await c.execute(
                "UPDATE tasks SET status = ?, "
                "last_stage_change_at = ?, stale_alert_at = NULL, stall_escalation_level = 0 "
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

    # v0.3.9 trajectory-completion notification. Fires only on
    # natural completion paths so Coach is told to summarize for the
    # user. Skipped on Coach-forced manual archives (Coach already
    # knows + decides what to tell the user) and on rung-4
    # auto-archive (the human_attention escalation already informs
    # the user, and Coach summarizing a forced kill is misleading).
    if new_status == "archive" and reason in _NATURAL_ARCHIVE_REASONS:
        try:
            await _emit_task_completed(
                task_id=task_id,
                title=title or "",
                from_stage=old_status,
                reason=reason,
                owner=owner,
                ts=now,
            )
        except Exception:
            logger.exception(
                "kanban: task_completed notify failed (task_id=%s)", task_id
            )


# Reasons that represent a trajectory playing out end-to-end.
# `shipped` — shipper called coord_mark_shipped after ship-stage work.
# `commit_pushed` / `task_execution_completed` — execute was the last
#   trajectory entry, so the work itself terminates the trajectory.
# `audit_pass` — audit was the last trajectory entry (rare but valid).
_NATURAL_ARCHIVE_REASONS: frozenset[str] = frozenset({
    "shipped",
    "commit_pushed",
    "task_execution_completed",
    "audit_pass",
})


async def _emit_task_completed(
    *,
    task_id: str,
    title: str,
    from_stage: str,
    reason: str,
    owner: str | None,
    ts: str,
) -> None:
    """Publish `task_completed` event + wake Coach with a summary
    prompt. Coach's reply lands in the chat panel + (if Telegram is
    configured + this turn was user-triggered) flushes to the user's
    phone via the existing user-initiated-turn filter.
    """
    # Pull the trajectory + executor + last-stage assignee for the
    # event payload (lets the Coach prompt render the full path).
    trajectory_str = ""
    executor: str | None = None
    last_stage_owner: str | None = None
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT trajectory, owner FROM tasks WHERE id = ?",
                (task_id,),
            )
            r = await cur.fetchone()
            if r:
                rd = dict(r)
                trajectory_str = rd.get("trajectory") or ""
                executor = rd.get("owner")
            # Look up who handled the final pre-archive stage so the
            # summary names them. For shipped → ship's owner; for
            # commit_pushed → execute's owner; etc.
            role_for_last = {
                "ship": "shipper",
                "audit_syntax": "auditor_syntax",
                "audit_semantics": "auditor_semantics",
                "execute": "executor",
                "plan": "planner",
            }.get(from_stage)
            if role_for_last:
                cur = await c.execute(
                    "SELECT owner FROM task_role_assignments "
                    "WHERE task_id = ? AND role = ? "
                    "ORDER BY assigned_at DESC LIMIT 1",
                    (task_id, role_for_last),
                )
                rr = await cur.fetchone()
                if rr:
                    last_stage_owner = dict(rr).get("owner")
        finally:
            await c.close()
    except Exception:
        pass

    # Render a compact trajectory marker the prompt can show inline.
    trajectory_marker = _trajectory_marker_from_json(trajectory_str)

    # AUDIT FIX (v0.3.9.1): isolate publish vs wake. Previously the
    # bus.publish was outside the wake's try/except — a publish
    # failure would silently skip the wake too, so Coach got
    # neither the event nor the prompt. Now each is independently
    # wrapped: a publish failure still lets the wake fire (Coach
    # still hears about the completion via the prompt), and a
    # wake failure doesn't undo the published event.
    try:
        await bus.publish({
            "ts": ts,
            "agent_id": "system",
            "type": "task_completed",
            "task_id": task_id,
            "title": title,
            "trajectory": trajectory_str,
            "trajectory_marker": trajectory_marker,
            "from_stage": from_stage,
            "reason": reason,
            "executor": executor,
            "last_stage_owner": last_stage_owner,
            "owner": owner,
            "to": "coach",
        })
    except Exception:
        logger.exception(
            "kanban: task_completed publish failed (task_id=%s)", task_id
        )

    # Wake Coach with a summary prompt. The prompt names the task,
    # the path, the executor + last-stage assignee, and asks Coach
    # to send a summary to the user (broadcast / Telegram). Coach
    # decides the right channel based on who was asking.
    last_label = (
        f"the {from_stage} stage was completed by {last_stage_owner}"
        if last_stage_owner else f"the task wrapped after {from_stage}"
    )
    exec_label = f"executor: {executor}" if executor else "no executor recorded"
    # AUDIT FIX (v0.3.9.1): the prior wording said the Telegram
    # bridge would auto-forward Coach's reply "if this turn was
    # user-triggered." Misleading: this wake IS a system-triggered
    # turn (the trigger event is `task_completed`, not a
    # `message_sent{from=human}`), so the bridge's outbound filter
    # blocks the reply. To reach the user on Telegram, Coach must
    # explicitly use coord_send_message(to='broadcast') — which the
    # bridge accumulates and flushes when the OWNING (originating)
    # turn was user-triggered, OR Coach calls coord_request_human
    # for unconditional Telegram delivery on important completions.
    body = (
        f"Task {task_id} completed: {title!r}. Trajectory: "
        f"{trajectory_marker or '(unknown)'}. {last_label}; {exec_label}. "
        f"Final reason: {reason}.\n\n"
        f"Send a summary of the outcome to the user. Cover: "
        f"(1) what was delivered, (2) any caveats / known limitations / "
        f"open questions, (3) whether follow-up tasks are needed. Keep "
        f"it concise (3-6 sentences) unless the work is complex enough "
        f"to need more.\n\n"
        f"Channel rules:\n"
        f"- If the user is watching the harness UI: call "
        f"coord_send_message(to='broadcast', body=<your summary>). "
        f"They see it in the chat panel.\n"
        f"- If the user is on Telegram and wants to be pinged on "
        f"completion: call coord_request_human(subject='Task "
        f"{task_id} done', body=<your summary>, urgency='normal'). "
        f"This unconditionally forwards to Telegram + the EnvPane "
        f"attention strip — use it for completions the user "
        f"actually asked for, not routine internal cleanup.\n"
        f"- Plain text in your reply WITHOUT the tools above stays "
        f"in your chat panel only — this wake is system-triggered, "
        f"so the Telegram bridge does NOT auto-forward."
    )
    try:
        from server.agents import maybe_wake_agent
        await maybe_wake_agent("coach", body, bypass_debounce=True)
    except Exception:
        logger.exception(
            "kanban: failed to wake Coach for task_completed %s", task_id
        )


def _trajectory_marker_from_json(traj_json: str) -> str:
    """`P → E → AY → AS → S` style abbreviation. Returns empty
    string on parse failure — the prompt then falls back to a
    generic '(unknown)' label."""
    if not traj_json:
        return ""
    try:
        parsed = json.loads(traj_json)
    except Exception:
        return ""
    if not isinstance(parsed, list):
        return ""
    tokens = {
        "plan": "P",
        "execute": "E",
        "audit_syntax": "AY",
        "audit_semantics": "AS",
        "ship": "S",
    }
    parts = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        stage = entry.get("stage")
        if stage in tokens:
            parts.append(tokens[stage])
    return " → ".join(parts)


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


def _executor_worktree_boundary(role: str, slot: str) -> str:
    """Per-slot worktree-boundary suffix appended to executor wakes.

    v0.3.7 (production trace 2026-05-04, p8 wrote to /workspaces/.project
    instead of their own worktree, hit the opaque 'nothing to commit'
    soft-OK in coord_commit_push, marked the task blocked).

    Names the slot's worktree path explicitly and reminds the executor
    that the shared seed checkout is off-limits. Empty string for
    non-executor roles (auditors / shippers don't edit code).
    """
    if role != "executor" or not slot:
        return ""
    return (
        f"\n\nWorktree boundary: your edits MUST land in "
        f"/workspaces/{slot}/project (your own git worktree on branch "
        f"work/{slot}). Do NOT edit /workspaces/.project — that is the "
        f"shared seed checkout used to provision worktrees and belongs "
        f"to no slot. Editing it strands your work on a tree the "
        f"kanban can't see; coord_commit_push will report 'nothing to "
        f"commit' from your own worktree because the changes never "
        f"reached it. If your tooling defaulted to .project, move your "
        f"changes into /workspaces/{slot}/project before committing."
    )


_TOOL_NOT_VISIBLE_ESCAPE = (
    "\n\nIf the named tool is NOT visible in your runtime — i.e. you "
    "look at your tool list and don't see it — DO NOT just write the "
    "deliverable to disk and stop. The kanban will never see your "
    "work. Instead message Coach IMMEDIATELY via "
    "coord_send_message(to='coach', body='need to deliver task ... "
    "but the named coord_* tool is not visible to me') so Coach can "
    "advance the task on your behalf. coord_request_human() is the "
    "human-facing escalation if Coach is also unreachable.\n\n"
    "AND: do NOT route around the missing coord_* tool by using raw "
    "git/Bash/Edit to commit, push, or publish the deliverable "
    "yourself. Those bypass every kanban guardrail — your work lands "
    "on a branch the board has no record of, the assignee in the "
    "kanban (which may already be someone else after a reassignment) "
    "stays uncredited, and the next stage never wakes. Stop and "
    "message Coach instead."
)


_DEFAULT_SYNTAX_FOCUS = (
    "Match the contract above; verify internal soundness "
    "(no bugs, no inconsistencies, no broken interfaces)."
)


async def _load_active_role_focus(task_id: str, role: str) -> str | None:
    """Read the `focus` from the most-recent active task_role_assignments
    row for `(task_id, role)`. Returns None when no active row exists or
    the row has no focus. Used by `_wake_role_or_emit_needed` so the
    stage-entry wake prompt can render Coach's framing."""
    if role not in ("auditor_syntax", "auditor_semantics"):
        return None
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT focus FROM task_role_assignments "
            "WHERE task_id = ? AND role = ? "
            "AND completed_at IS NULL AND superseded_by IS NULL "
            "ORDER BY assigned_at DESC LIMIT 1",
            (task_id, role),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return None
    val = dict(row).get("focus")
    return val if isinstance(val, str) and val.strip() else None


async def inherit_audit_focus(task_id: str, role: str) -> str | None:
    """When Coach re-assigns an auditor without re-providing `focus`,
    inherit from the prior superseded row so a quick reassignment
    doesn't lose the framing. Walks back through superseded rows
    (newest first) and returns the first non-empty focus.

    Returns None when no prior audit assignment carried a focus.
    """
    if role not in ("auditor_syntax", "auditor_semantics"):
        return None
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT focus FROM task_role_assignments "
            "WHERE task_id = ? AND role = ? AND focus IS NOT NULL "
            "ORDER BY assigned_at DESC LIMIT 1",
            (task_id, role),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return None
    val = dict(row).get("focus")
    return val if isinstance(val, str) and val.strip() else None


async def _build_audit_contract_block(task_id: str) -> str:
    """Build the `## Contract` block for syntax-audit wake prompts
    (kanban-specs §4.6.1). Cascades whatever rungs exist:
      1. spec.md (when readable)
      2. task title + description
      3. executor's wake prompt (best-effort from event log)
      4. latest commit_pushed message / task_execution_completed summary

    Each present rung is rendered with a sub-heading. A task always
    has at least #2, so this never returns empty for a real task.
    """
    from pathlib import Path

    parts: list[str] = []
    spec_path: str | None = None
    title: str = ""
    description: str = ""
    executor_id: str | None = None
    project_id: str | None = None
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT title, description, spec_path, owner, project_id "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cur.fetchone()
        if row:
            d = dict(row)
            title = (d.get("title") or "").strip()
            description = (d.get("description") or "").strip()
            spec_path = d.get("spec_path")
            executor_id = d.get("owner")
            project_id = d.get("project_id")
    finally:
        await c.close()

    # Rung 1: spec.md (when present and readable)
    if spec_path:
        try:
            candidate = Path(spec_path)
            if not candidate.is_absolute():
                from server.paths import DATA_ROOT
                candidate = DATA_ROOT / spec_path
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8", errors="replace")
                if len(text) > 4000:
                    text = text[:4000].rstrip() + "\n... (truncated; read full file at " + str(candidate) + ")"
                parts.append(f"### Spec ({spec_path})\n\n{text}")
        except Exception:
            # Spec file unreadable — skip rung, surface the path so
            # the auditor can find it manually if needed.
            parts.append(
                f"### Spec\n\nspec_path={spec_path} (could not be read; "
                f"check the path on disk)."
            )

    # Rung 2: task title + description (always present)
    title_block = f"### Task framing\n\n**Title:** {title}"
    if description:
        if len(description) > 1500:
            description = description[:1500].rstrip() + "..."
        title_block += f"\n\n**Description:** {description}"
    parts.append(title_block)

    # Rung 3: executor's most recent wake prompt (best-effort).
    if executor_id and project_id:
        try:
            c = await configured_conn()
            try:
                cur = await c.execute(
                    "SELECT payload FROM events "
                    "WHERE project_id = ? AND agent_id = ? AND type = 'agent_started' "
                    "ORDER BY ts DESC LIMIT 5",
                    (project_id, executor_id),
                )
                rows = await cur.fetchall()
            finally:
                await c.close()
            for r in rows:
                try:
                    payload = json.loads(dict(r).get("payload") or "{}")
                except Exception:
                    continue
                prompt = payload.get("prompt") or payload.get("entry_prompt") or ""
                if isinstance(prompt, str) and task_id in prompt:
                    if len(prompt) > 1500:
                        prompt = prompt[:1500].rstrip() + "..."
                    parts.append(
                        f"### Executor's wake prompt ({executor_id})\n\n{prompt}"
                    )
                    break
        except Exception:
            logger.exception(
                "kanban: failed reading executor wake for %s", task_id
            )

    # Rung 4: latest commit_pushed message / task_execution_completed summary.
    if project_id:
        try:
            c = await configured_conn()
            try:
                cur = await c.execute(
                    "SELECT type, payload FROM events "
                    "WHERE project_id = ? AND type IN ('commit_pushed', 'task_execution_completed') "
                    "ORDER BY ts DESC LIMIT 30",
                    (project_id,),
                )
                rows = await cur.fetchall()
            finally:
                await c.close()
            for r in rows:
                d = dict(r)
                try:
                    payload = json.loads(d.get("payload") or "{}")
                except Exception:
                    continue
                if payload.get("task_id") != task_id:
                    continue
                if d.get("type") == "commit_pushed":
                    sha = (payload.get("sha") or "")[:12]
                    msg = payload.get("message") or ""
                    if len(msg) > 1000:
                        msg = msg[:1000].rstrip() + "..."
                    parts.append(
                        f"### Executor's commit ({sha})\n\n{msg}"
                    )
                else:
                    summary = payload.get("summary") or ""
                    artifact = payload.get("artifact_path") or ""
                    if len(summary) > 1000:
                        summary = summary[:1000].rstrip() + "..."
                    body = f"**Summary:** {summary}"
                    if artifact:
                        body += f"\n\n**Artifact:** {artifact}"
                    parts.append(f"### Executor's deliverable\n\n{body}")
                break
        except Exception:
            logger.exception(
                "kanban: failed reading executor deliverable for %s", task_id
            )

    return "\n\n".join(parts)


def _build_semantic_context_block(task_id: str, project_id: str | None) -> str:
    """Build the `## Project context` block for semantic-audit wake
    prompts (kanban-specs §4.6.2). Names the binding sources
    (truth/, project-objectives.md, wiki/, Compass) the auditor must
    read instead of treating spec.md as binding."""
    project_id = project_id or "<active-project>"
    return (
        f"### Project context (binding sources for semantic audit)\n\n"
        f"Read these BEFORE judging the deliverable. The spec is "
        f"supplementary background — the audit verdict judges against "
        f"the world (truth + intent + domain), not against the planner's "
        f"interpretation of it.\n\n"
        f"- **Truth corpus** — `/data/projects/{project_id}/truth/` "
        f"(human-vetted binding facts) + "
        f"`/data/projects/{project_id}/project-objectives.md` "
        f"(authored objectives).\n"
        f"- **Wiki** — `/data/wiki/{project_id}/` (gotchas, glossary, "
        f"stakeholder preferences, domain rules — agent-curated but "
        f"binding for semantic alignment).\n"
        f"- **Compass surface** — the Compass-derived block injected "
        f"into this project's `CLAUDE.md` already lists settled lattice "
        f"directions; read that section. The Compass auto-audit's most "
        f"recent verdict on this task's commit is at "
        f"`tasks.compass_audit_report_path` (when present — link will "
        f"appear on the kanban card).\n"
        f"- **Compass MCP tools** are Coach-only. If the CLAUDE.md "
        f"Compass block + the auto-audit report are insufficient for "
        f"your focus, message Coach via "
        f"`coord_send_message(to='coach', body='need compass_ask on "
        f"<question> for {task_id} semantic audit')` so Coach can run "
        f"`compass_ask` on your behalf — better than guessing a verdict."
    )


async def build_auditor_wake_body(
    *,
    task_id: str,
    role: str,
    focus: str | None,
    is_pool: bool,
) -> str:
    """Build the full body of an auditor wake prompt (kanban-specs §4.6).

    Centralised so `coord_assign_auditor` (initial assignment wake) and
    `_wake_role_or_emit_needed` (stage-entry wake when Coach reserved
    the role earlier) produce identical wakes. Layout:

        ## Focus
        <Coach's words, or default-syntax stub>

        ## Contract                                  (syntax only)
        ### Spec ...                                 (when present)
        ### Task framing
        ### Executor's wake prompt                   (when found)
        ### Executor's commit                        (when found)

        ## Project context                           (semantic only)
        <truth/wiki/Compass paths + escalation note>

        ## Tool to call when done
        coord_submit_audit_report(task_id=..., kind=..., verdict=...)
    """
    if role == "auditor_syntax":
        kind = "syntax"
        review_label = "formal"
    else:
        kind = "semantics"
        review_label = "semantic"

    effective_focus = (focus or "").strip()
    if not effective_focus:
        if role == "auditor_syntax":
            effective_focus = _DEFAULT_SYNTAX_FOCUS
        else:
            # Semantic with no focus is a configuration bug — caller
            # validation should have caught it. Render an explicit
            # "no focus set, ask Coach" stub so the auditor fails
            # loudly rather than guessing.
            effective_focus = (
                "(no focus set — Coach has not named what to check. "
                "STOP and message Coach via coord_send_message(to='coach', "
                f"body='need focus for semantic audit on {task_id}') before "
                "submitting any verdict.)"
            )

    sections: list[str] = []
    sections.append(f"## Focus\n\n{effective_focus}")

    if role == "auditor_syntax":
        contract = await _build_audit_contract_block(task_id)
        if contract:
            sections.append(f"## Contract\n\n{contract}")
    else:
        # Resolve project_id for the semantic context block.
        project_id: str | None = None
        try:
            c = await configured_conn()
            try:
                cur = await c.execute(
                    "SELECT project_id FROM tasks WHERE id = ?",
                    (task_id,),
                )
                row = await cur.fetchone()
            finally:
                await c.close()
            if row:
                project_id = dict(row).get("project_id")
        except Exception:
            logger.exception(
                "kanban: failed reading project_id for semantic wake on %s",
                task_id,
            )
        sections.append(
            f"## Project context\n\n"
            f"{_build_semantic_context_block(task_id, project_id)}"
        )
        # Spec only as supplementary background for semantic audit.
        try:
            c = await configured_conn()
            try:
                cur = await c.execute(
                    "SELECT spec_path FROM tasks WHERE id = ?",
                    (task_id,),
                )
                row = await cur.fetchone()
            finally:
                await c.close()
            if row:
                sp = dict(row).get("spec_path") or ""
                if sp:
                    sections.append(
                        f"## Spec (supplementary — what was meant to be built)\n\n"
                        f"`{sp}` — read for context but judge against project "
                        f"context above; a spec that drifted from intent is a "
                        f"bug the semantic audit must catch."
                    )
        except Exception:
            pass

    pool_note = ""
    if is_pool:
        pool_note = (
            f"This was a pool call — first call "
            f"coord_accept_role(task_id={task_id!r}, role={role!r}); first "
            f"accepted claim wins. Then do the audit and submit."
        )
    sections.append(
        "## Submit the audit\n\n"
        + (pool_note + "\n\n" if pool_note else "")
        + f"coord_submit_audit_report(task_id={task_id!r}, "
        f"kind={kind!r}, verdict='pass'|'fail', body=<your review>)"
        + _TOOL_NOT_VISIBLE_ESCAPE
    )

    intro = (
        f"Coach assigned you the {review_label} review on task {task_id}."
    )
    return intro + "\n\n" + "\n\n".join(sections)


async def _completion_hint_for_role(task_id: str, role: str) -> str:
    """Per-role wake-prompt instruction line that NAMES the completion
    tool with the actual `task_id` baked in. The kanban only advances
    when the assignee calls the matching tool with `task_id` — vague
    'use the matching completion tool' wording was leaving Players
    committing without driving the board (see audit-2026-05-04 §gap-1).

    v0.3.4: every hint now ends with the tool-not-visible escape
    paragraph. Real production trace (2026-05-04) had a Player write
    audit_<kind>.md to disk and stop because they couldn't see
    `coord_submit_audit_report` in their runtime — kanban silently
    stuck for 15+ min, stall sweeper named the wrong Player. The
    escape tells the Player to message Coach instead of failing
    quietly."""
    if role == "planner":
        return (
            f"Write the spec by calling "
            f"coord_write_task_spec(task_id={task_id!r}, body=<spec>). "
            f"On success the task auto-advances plan -> the next stage "
            f"in its trajectory.{_TOOL_NOT_VISIBLE_ESCAPE}"
        )
    if role == "executor":
        # Trajectory-aware: when no audit stage follows execute, the
        # executor must self-audit before signalling done.
        try:
            c = await configured_conn()
            try:
                cur = await c.execute(
                    "SELECT trajectory FROM tasks WHERE id = ?",
                    (task_id,),
                )
                row = await cur.fetchone()
            finally:
                await c.close()
        except Exception:
            row = None
        stages = _trajectory_stages(dict(row)) if row else []
        has_audit = any(
            s in ("audit_syntax", "audit_semantics") for s in stages
        )
        self_audit = "" if has_audit else (
            " Since this trajectory has no audit stage after execute, "
            "SELF-AUDIT first: run the relevant tests / sanity checks, "
            "verify the change does what the spec says, THEN call the "
            "tool below."
        )
        return (
            f"For code changes: "
            f"coord_commit_push(message=<msg>, task_id={task_id!r}). "
            f"For non-code deliverables: "
            f"coord_complete_execution(task_id={task_id!r}, "
            f"summary=<what you delivered>, artifact_path=<path?>). "
            f"You MUST pass `task_id={task_id!r}` — without it the "
            f"kanban does not advance.{self_audit}"
            f"{_TOOL_NOT_VISIBLE_ESCAPE}"
        )
    if role in ("auditor_syntax", "auditor_semantics"):
        kind = "syntax" if role == "auditor_syntax" else "semantics"
        return (
            f"Read the spec + the executor's commit/artifact, then call "
            f"coord_submit_audit_report(task_id={task_id!r}, "
            f"kind={kind!r}, body=<your review>, "
            f"verdict='pass' or 'fail'). Pass advances the stage; fail "
            f"reverts the task to execute and re-wakes the executor "
            f"with your report attached.{_TOOL_NOT_VISIBLE_ESCAPE}"
        )
    if role == "shipper":
        return (
            f"Merge / publish / hand-off the deliverable, then call "
            f"coord_mark_shipped(task_id={task_id!r}, note=<optional>). "
            f"The task auto-archives.{_TOOL_NOT_VISIBLE_ESCAPE}"
        )
    return (
        f"Call coord_my_assignments(); it will print the next "
        f"actionable step + the completion tool to call."
    )


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
    is_pool = not assignment.get("owner")
    if role in ("auditor_syntax", "auditor_semantics"):
        # Auditor wakes use the focus + context cascade (kanban-specs §4.6).
        focus = await _load_active_role_focus(task_id, role)
        prompt = await build_auditor_wake_body(
            task_id=task_id, role=role, focus=focus, is_pool=is_pool,
        )
    else:
        completion_hint = await _completion_hint_for_role(task_id, role)
        if not is_pool:
            prompt = (
                f"Task {task_id} has entered your active {role_label} stage. "
                f"BEFORE editing, committing, or publishing anything: call "
                f"coord_my_assignments and confirm task {task_id} appears "
                f"under your active roles with role={role!r}. If you do NOT "
                f"see it there, you have been reassigned or the task moved "
                f"on — STOP and message Coach via coord_send_message(to="
                f"'coach', body='clarify status of {task_id}'). Do not act "
                f"on this wake message alone — it can be stale by the time "
                f"you read it.\n\nIf you ARE the active assignee, do the "
                f"role work, then call the completion tool below — the "
                f"kanban does NOT advance until you do.\n\n{completion_hint}"
            )
        else:
            prompt = (
                f"Task {task_id} has entered {role_label}. You are in the "
                f"candidate call. If you can take it, call "
                f"coord_accept_role(task_id={task_id!r}, role={role!r}); "
                f"first accepted claim wins. If it is already claimed by the "
                f"time you answer, there is nothing to do — do NOT do the "
                f"role work without an accepted claim, the kanban will not "
                f"credit it.\n\nOnce you have the role, the next step is:"
                f"\n{completion_hint}"
            )

    try:
        from server.agents import maybe_wake_agent
        for slot in targets:
            try:
                slot_prompt = prompt + _executor_worktree_boundary(role, slot)
                await maybe_wake_agent(slot, slot_prompt, bypass_debounce=True)
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
    "build_auditor_wake_body",
    "inherit_audit_focus",
]

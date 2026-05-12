"""Kanban v2 record-only subscriber (Docs/kanban-specs-v2.md §9).

In v1 this module auto-advanced stages on commit/audit/spec events
and auto-reverted on audit FAIL. v2 inverts that: every transition
is an explicit Coach call to `coord_approve_stage` (or
`coord_archive_task`). The subscriber's job is now narrower:

  1. Mirror every v2-mappable bus event into `project_events` via
     `server.project_events.maybe_write_from_bus(ev)`. This is the
     unified context surface Coach reads on its tick (§9).

  2. Maintain in-process correlation caches the rest of the harness
     reads at runtime — specifically the most-recent commit-per-
     project map used by the Compass audit watcher to attach a
     verdict to the right task.

  3. On `audit_report_submitted{verdict='fail'}`, emit the
     Coach-bound `audit_fail_notification` and insert a
     `deviations_log{noticed_at='audit'}` row so Coach sees the
     fail on the next tick (§22.1) — but the task does NOT
     auto-revert. Coach reads the report, decides, and calls
     `coord_approve_stage(next_stage='execute', assignee=<slot>,
     note=<composed prompt>)` if rework is the right call.

  4. `compass_audit_logged` writes the `compass_audit_report_path`
     + `compass_audit_verdict` columns on the correlated task.

The subscriber NEVER transitions stages, NEVER calls
`maybe_wake_agent`, NEVER inserts task_role_assignments rows.
Those are MCP-tool concerns now (`coord_approve_stage`,
`coord_archive_task`, `coord_role_complete`).

Failure isolation: per-event try/except so a single bad event
doesn't kill the subscriber. Feature flag
`HARNESS_KANBAN_AUTO_ADVANCE` (default true) can disable the whole
subscriber on cost-constrained deploys — Phase-1 mirroring still
runs (it's the bus subscription itself). Lifecycle owned by the
module (`start_kanban_subscriber` / `stop_kanban_subscriber` /
`is_running`) mirrors the audit-watcher pattern.
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
    # v2 cares about commit_pushed (commit cache + warning), audit
    # results (fail notification + deviations_log), and Compass
    # verdicts (correlation columns). Everything else just gets
    # mirrored to project_events via maybe_write_from_bus.
    "commit_pushed",
    "audit_report_submitted",
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
    # Phase 1 (kanban v2): every bus event also gets mirrored to the
    # per-project event log when its type is v2-mappable. Independent
    # of the v1 dispatch below — this just records, it doesn't drive
    # transitions. Failure-isolated inside the helper, so a DB hiccup
    # never breaks the v1 dispatch.
    project_event_id: int | None = None
    try:
        from server.project_events import maybe_write_from_bus
        project_event_id = await maybe_write_from_bus(ev)
    except Exception:
        logger.exception(
            "kanban: project_events mirror failed on event %r", etype
        )
    if etype not in WATCHED_EVENT_TYPES:
        return
    if etype == "commit_pushed":
        await _on_commit_pushed(ev)
    elif etype == "audit_report_submitted":
        # Pass the project_events row id so audit-FAIL deviations_log
        # rows can carry source_event_id back to the triggering event.
        await _on_audit_submitted(ev, project_event_id=project_event_id)
    elif etype == "compass_audit_logged":
        await _on_compass_audit_logged(ev)


# ---------------------------------------------------------------- handlers


async def _on_commit_pushed(ev: dict[str, Any]) -> None:
    """v2 record-only handler. Updates the in-process commit cache so
    the Compass audit watcher can correlate a verdict to the right
    task. NEVER advances the stage — Coach calls coord_approve_stage
    after reading the commit in the event log.
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


async def _on_audit_submitted(
    ev: dict[str, Any],
    *,
    project_event_id: int | None = None,
) -> None:
    """v2 record-only handler. On verdict='fail' emits the Coach-bound
    `audit_fail_notification` (kind_round + escalate flag computed from
    fail history) and inserts a `deviations_log{noticed_at='audit'}`
    row so Coach sees the FAIL on the next tick and the §22.1
    instrumentation has the data point. NEVER reverts the stage.

    `project_event_id` is the id of the `audit_report_submitted`
    project_events row that triggered this handler — passed in from
    `_handle_event` so the deviations_log row's `source_event_id`
    column can carry the causal pointer per §22.1.

    Pass-verdict events get NO subscriber action — the project_events
    mirror in maybe_write_from_bus already records them. Coach reads
    the row and calls `coord_approve_stage` to advance.
    """
    task_id = (ev.get("task_id") or "").strip()
    kind = (ev.get("kind") or "").strip().lower()
    verdict = (ev.get("verdict") or "").strip().lower()
    if not task_id or kind not in ("syntax", "semantics") or verdict != "fail":
        return

    # Read the executor + project for the notification + deviations row.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT owner, project_id FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return
    t = dict(row)
    executor = t.get("owner")
    project_id = t.get("project_id") or ""

    # Coach-bound notification — same payload shape as v1; Coach reads
    # this in the project_events log on the next tick.
    kind_round, escalate = await _count_fails_for_kind(
        task_id=task_id, kind=kind,
    )
    await _emit_audit_fail_notification(
        task_id=task_id,
        kind=kind,
        kind_round=kind_round,
        escalate=escalate,
        auditor_id=ev.get("auditor_id"),
        executor_id=executor,
        report_path=ev.get("report_path") or "",
    )

    # Deviations log instrumentation (§22.1). The audit's body summary
    # would be richer but we don't read the report file here — Coach
    # gets the path via the audit_fail_notification + project_events
    # row. The description records the kind / round / report pointer.
    if executor and project_id:
        try:
            from datetime import datetime, timezone
            description = (
                f"audit FAIL kind={kind} round={kind_round} "
                f"report={ev.get('report_path') or '(no path)'}"
            )
            c = await configured_conn()
            try:
                await c.execute(
                    "INSERT INTO deviations_log "
                    "(project_id, ts, task_id, executor, "
                    " noticed_at, description, source_event_id) "
                    "VALUES (?, ?, ?, ?, 'audit', ?, ?)",
                    (
                        project_id,
                        datetime.now(timezone.utc).isoformat(),
                        task_id, executor, description,
                        project_event_id,
                    ),
                )
                await c.commit()
            finally:
                await c.close()
        except Exception:
            logger.exception(
                "kanban: deviations_log insert failed for task=%s", task_id
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
    from server.tools import _with_player_reminder
    body = _with_player_reminder(
        f"Coach reassigned the {role_label} role on task {task_id} "
        f"from you to {new_label}. STOP — do not edit, commit, push, "
        f"or publish for this task. If you have local uncommitted "
        f"changes worth preserving, message Coach before discarding."
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
                await maybe_wake_agent(
                    slot, body,
                    bypass_debounce=True,
                    wake_source="kanban_stand_down",
                )
            except Exception:
                pass
    except Exception:
        pass
    return woken


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
    """v2: a defensive surface that fires when something tried to wake
    a role but no active row exists at the target stage.

    Per spec §18, the `stage_assignment_needed` event of v0.3 is gone:
    `coord_approve_stage` plants the role row + assignee atomically, so
    the "assignment gap" failure mode shouldn't happen in normal flow.
    The path is still reachable defensively (e.g. a trajectory rewrite
    that inserted a stage Coach hasn't yet approved into; the rung-3
    auto-reassign branch failing to match a role row). When it does,
    surface as a `human_attention` event — a real fault someone needs
    to see — rather than the removed v0.3 type.
    """
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).isoformat()
    body = (
        f"Task {task_id} is in stage {stage!r} with no {role} role row. "
        f"The harness tried to wake the role but found nothing planted. "
        f"Coach should call coord_approve_stage(task_id={task_id!r}, "
        f"next_stage={stage!r}, assignee=<slot>, note=<brief>) to plant, "
        f"OR rewrite the trajectory via coord_set_task_trajectory if "
        f"the stage was inserted by mistake. owner={to_owner!r}."
    )
    payload = {
        "ts": ts,
        "agent_id": "system",
        "type": "human_attention",
        "subject": f"kanban: {role} role missing on task {task_id}",
        "body": body,
        "task_id": task_id,
        "role": role,
        "stage": stage,
        "urgency": "medium",
        "to": "human",
    }
    await bus.publish(payload)


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
    """Coach-bound notification on every audit fail. See kanban-specs-v2.md
    §8 + §17 for the contract: visibility on every fail, escalation
    only on the second fail of the same kind.

    v0.3.11: include an imperative `body` field so the event row in
    Coach's pane is actionable instead of a bare type. Branches on
    `escalate` so first-fail noise reads as "expected, watch round 2"
    while subsequent same-kind fails carry the explicit bump-effort/
    bump-thinking/bump-model ladder.
    """
    if escalate:
        body = (
            f"Audit failed (ESCALATION): task {task_id} failed "
            f"kind={kind} round {kind_round}. Same kind has now "
            f"failed {kind_round} times. The executor "
            f"({executor_id or '(unknown)'}) was re-woken, but the "
            f"loop suggests quality is the bottleneck. Inspect their "
            f"effort/model with coord_get_player_settings"
            + (
                f"(player_id={executor_id!r})"
                if executor_id else "()"
            )
            + " and walk the bump ladder one rung at a time: "
            + "coord_set_player_effort"
            + (
                f"({executor_id!r}, 'high'|'max')"
                if executor_id else "(...)"
            )
            + ", then coord_set_player_thinking"
            + (
                f"({executor_id!r}, 'on')"
                if executor_id else "(...)"
            )
            + " (Claude only), then coord_set_player_model"
            + (
                f"({executor_id!r}, 'latest_opus')"
                if executor_id else "(...)"
            )
            + ". NEVER change runtime — that's a human decision."
        )
    else:
        body = (
            f"Audit failed: task {task_id} failed kind={kind} "
            f"round {kind_round}. The executor "
            f"({executor_id or '(unknown)'}) was auto-re-woken with "
            f"the report attached. First fail of this kind is "
            f"expected correction noise; no action needed from you "
            f"yet. Watch for round 2."
        )
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
        "body": body,
        "to": "coach",
    })


async def _executor_worktree_boundary(role: str, slot: str) -> str:
    """Per-slot worktree-boundary suffix appended to executor wakes.

    v0.3.7 (production trace 2026-05-04, p8 wrote to the seed checkout
    instead of their own worktree, hit the opaque 'nothing to commit'
    soft-OK in coord_commit_push, marked the task blocked).

    Names the slot's worktree path explicitly (resolved against the
    active project) and reminds the executor that the shared seed
    checkout is off-limits. Empty string for non-executor roles
    (auditors / shippers don't edit code).
    """
    if role != "executor" or not slot:
        return ""
    from server.db import resolve_active_project
    from server.paths import project_paths
    pp = project_paths(await resolve_active_project())
    worktree = pp.worktree(slot)
    seed = pp.bare_clone
    return (
        f"\n\nWorktree boundary: your edits MUST land in {worktree} "
        f"(your own git worktree on branch work/{slot}). Do NOT edit "
        f"{seed} — that is the shared seed checkout used to provision "
        f"worktrees and belongs to no slot. Editing it strands your "
        f"work on a tree the kanban can't see; coord_commit_push will "
        f"report 'nothing to commit' from your own worktree because "
        f"the changes never reached it. If your tooling defaulted to "
        f"the seed checkout, move your changes into {worktree} before "
        f"committing."
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
    """Build the full body of an auditor wake prompt (kanban-specs-v2.md §5.4).

    Centralised so the v2 `coord_approve_stage` path (initial assignment
    wake when Coach approves into audit_*) and `_wake_role_or_emit_needed`
    (stage-entry wake when Coach reserved the role earlier) produce
    identical wakes. Layout:

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

    # Coach's "definition of done" for this task — captured at
    # coord_create_task and/or coord_approve_stage(plan→execute).
    # Surfacing it here gives the auditor an explicit prior to
    # evaluate against, on top of the spec / context blocks below.
    # Empty string = unset = no injection.
    success_criteria: str = ""
    project_id: str | None = None
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT project_id, success_criteria FROM tasks WHERE id = ?",
                (task_id,),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
        if row:
            d = dict(row)
            project_id = d.get("project_id")
            success_criteria = (d.get("success_criteria") or "").strip()
    except Exception:
        logger.exception(
            "kanban: failed reading task row for auditor wake on %s",
            task_id,
        )

    if success_criteria:
        sections.append(
            f"## Coach's acceptance criteria\n\n{success_criteria}"
        )

    if role == "auditor_syntax":
        contract = await _build_audit_contract_block(task_id)
        if contract:
            sections.append(f"## Contract\n\n{contract}")
    else:
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
            f"This wake landed via a legacy pool entry. In v2 pools "
            f"are FYI only — wait for Coach to assign explicitly via "
            f"coord_approve_stage. If Coach already named you in a "
            f"recent note, proceed with the audit; otherwise message "
            f"Coach to confirm."
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
            f"Draft the spec, then SIGNAL COACH by calling "
            f"coord_write_task_spec(task_id={task_id!r}, body=<spec>, "
            f"message_to_coach=<one-line summary>). That tool call "
            f"IS your message to Coach — the kanban only knows you're "
            f"done when you call it. Writing the spec to disk is not "
            f"enough; the disk-write + skipped-call pattern is the "
            f"#1 stall cause. The call wakes Coach in real time with "
            f"your message_to_coach as the wake reason; Coach reads, "
            f"may reply, and approves the next stage via "
            f"coord_approve_stage."
            f"{_TOOL_NOT_VISIBLE_ESCAPE}"
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
            f"Do the work, then SIGNAL COACH by calling the matching "
            f"completion tool. That tool call IS your message to Coach "
            f"— without it your work is invisible to the team and the "
            f"kanban can't record it. Writing the deliverable to disk "
            f"is not enough on its own.\n\n"
            f"For code changes: coord_commit_push(message=<msg>, "
            f"task_id={task_id!r}, message_to_coach=<one-line summary>). "
            f"For non-code deliverables: coord_role_complete("
            f"task_id={task_id!r}, message_to_coach=<one-line summary>, "
            f"artifact_path=<path?>).{self_audit}\n\n"
            f"Your turn ends only after the call lands. The call wakes "
            f"Coach in real time with your message_to_coach as context, "
            f"and the event surfaces in Coach's pane immediately — "
            f"expect Coach to read, decide the next stage, and possibly "
            f"reply to you directly.{_TOOL_NOT_VISIBLE_ESCAPE}"
        )
    if role in ("auditor_syntax", "auditor_semantics"):
        kind = "syntax" if role == "auditor_syntax" else "semantics"
        return (
            f"Read the spec + the executor's commit/artifact, then "
            f"SIGNAL COACH with your verdict by calling "
            f"coord_submit_audit_report(task_id={task_id!r}, "
            f"kind={kind!r}, body=<your review>, "
            f"verdict='pass' or 'fail', "
            f"message_to_coach=<one-line summary>). That tool call IS "
            f"your message to Coach — writing audit_<kind>.md to disk "
            f"and stopping is the #1 stall cause; don't fail silently.\n\n"
            f"The call wakes Coach in real time and the row lands in "
            f"Coach's pane immediately. FAIL does NOT auto-revert in "
            f"v2 — wait for Coach's reply / next-stage approval; do "
            f"not start fixing things based on a FAIL you "
            f"saw.{_TOOL_NOT_VISIBLE_ESCAPE}"
        )
    if role == "shipper":
        return (
            f"Merge / publish / hand-off the deliverable, then SIGNAL "
            f"COACH by calling coord_role_complete(task_id={task_id!r}, "
            f"message_to_coach='shipped at <ref>'). That tool call IS "
            f"your message to Coach — without it Coach has no idea "
            f"the ship landed and won't archive the task. The call "
            f"wakes Coach in real time; Coach archives with a "
            f"user-facing summary via "
            f"coord_archive_task.{_TOOL_NOT_VISIBLE_ESCAPE}"
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
        if not is_pool:
            prompt = (
                f"Task {task_id} has entered your active {role_label} stage."
            )
        else:
            prompt = (
                f"Task {task_id} has entered {role_label}. You're in "
                f"its FYI pool — Coach hasn't picked an assignee. "
                f"Don't start work; wait for an explicit assignment."
            )

    try:
        from server.agents import maybe_wake_agent
        from server.tools import _with_player_reminder
        for slot in targets:
            try:
                slot_prompt = _with_player_reminder(
                    prompt + await _executor_worktree_boundary(role, slot)
                )
                await maybe_wake_agent(
                    slot, slot_prompt,
                    bypass_debounce=True,
                    wake_source="kanban_role",
                )
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

"""Compass audit watcher — auto-fires `compass_audit` on kanban plan exits.

Compass is a COMPASS OF INTENT — its job is to check that a task's
PLAN aligns with the project's intent (the lattice + corpus). The
kanban v0.3 lifecycle (Docs/kanban-specs-v2.md) already runs syntactic +
semantic Player audits and shipper review on every stage transition;
those check that EXECUTION aligns with the plan. Compass sits one
layer up: the upstream check that the plan even pursues the right
direction.

So this watcher subscribes to a single event family — `task_stage_changed`
— and acts only on the `from='plan' to='execute'` transition: the
moment a planner has finished writing spec.md and the executor is
about to start. We read the spec body, hand it to `audit_work` along
with the task's title + trajectory, and let the audit verdict surface
to coach (or queue a question for the human on uncertain drift).

Pre-2026-05-04 design (deprecated): the watcher fired on every
`commit_pushed` / `decision_written` / `knowledge_written` /
`output_saved` event, auditing each artifact against the lattice.
That was duplicative — kanban's own auditor stages already check
execution alignment with the plan, so Compass was double-paying for
downstream review. The refocus moved Compass upstream: one strategic
check per task plan, only when there's a plan stage to check.

Design constraints:
  - **Project-scoped, not actor-scoped.** Each event carries a
    `project_id` (auto-stamped by `EventBus.publish`); the watcher
    audits that project regardless of which project is currently
    active in the UI. So an inactive project still gets audited when
    its plan exits land. We don't audit when Compass is disabled
    for the event's project (`compass_enabled_<id>` flag).
  - **Cost-gated.** Auto-audits respect the team daily cap
    (`HARNESS_TEAM_DAILY_CAP`) and the per-project enable flag.
    `audit_work` itself doesn't gate on cost (the only prior caller
    was Coach via MCP, which already saw the cap in its own prompts).
  - **Trajectory gate.** The watcher reads the task row and skips
    when `trajectory` doesn't include a `plan` stage. By construction
    a `from='plan'` transition can only happen on tasks with a plan
    stage, but the guard is cheap and protects against future
    trajectory-shape changes.
  - **Debounced per task.** Debounce key is `(project, task_id)` —
    one plan-audit per task ever in the natural flow. Window stays
    at 30s; effectively a one-shot guard against weird re-emits.
  - **Fire-and-forget.** The watcher pushes audit_work onto a
    background task so a slow LLM call doesn't backpressure the bus.
    Audit failures are logged and dropped — never block the
    originating tool call (Compass §10.6: audits never block work).
  - **Opt-out.** Set `HARNESS_COMPASS_AUTO_AUDIT=false` to disable
    the watcher entirely (e.g. for cost-constrained deploys that
    only want manual audits via the MCP tool).

The dashboard's manual paste UI was removed earlier — audits are
event-driven, the audit log is read-only on the human's side.
The `POST /api/compass/audit` HTTP endpoint stays as a debug
backstop (curl-able for testing) but is not surfaced in the UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

from server.events import bus

from server.compass import audit as cmp_audit
from server.compass import config

logger = logging.getLogger("harness.compass.audit_watcher")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Single event type. Compass audits the PLAN of a kanban task — the
# upstream check. Kanban's own auditor stages handle execution-vs-plan
# downstream.
WATCHED_EVENT_TYPES: frozenset[str] = frozenset({
    "task_stage_changed",
})


# Cap on the artifact text we shove into the audit prompt. Plan specs
# are typically a few hundred to a few thousand chars; ~16k char body
# room (matching output_extractor.MAX_BODY_CHARS) plus header is enough
# headroom for any reasonable plan. Larger specs get truncated with a
# marker.
_PLAN_BODY_MAX = 16_000
_ARTIFACT_TRUNCATE = 18_000


# ---------------------------------------------------------------- state


# Module-level lifecycle handles, mirroring the telegram bridge pattern.
_current_task: asyncio.Task[None] | None = None
_stopping = False
# Per-(project, task_id) monotonic timestamp of the last audit fired.
# Used by the debounce window. Read + written from the bus-consumer
# task only, so no lock needed.
_last_fire: dict[tuple[str, str], float] = {}
# Per-project ISO timestamp of the most recent audit fire. Distinct
# from `_last_fire` (which is keyed by (project, task_id) and uses
# monotonic time, not wall-clock). Surfaces via the health endpoint
# so operators can answer "is the watcher actually firing for this
# project?" without trawling event logs.
_last_fire_iso: dict[str, str] = {}
# Per-project most recent skip — {reason, ts, task_id}. Operators ask
# "why isn't the watcher firing?" — this answers it directly.
_last_skip: dict[str, dict[str, Any]] = {}


def is_running() -> bool:
    """True iff the watcher background task is alive."""
    return _current_task is not None and not _current_task.done()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _emit_skip(
    *,
    project_id: str,
    task_id: str,
    reason: str,
    **extras: Any,
) -> None:
    """Record + publish a `compass_audit_skipped` event for a gate-close.

    Three observers consume this:
      - module state (`_last_skip`) → health endpoint + MCP tool.
      - bus event → dashboard live counter + audit log.
      - logs (debug level) → post-mortem during an outage.

    Silent gate-closes (wrong event type, missing project_id from a
    malformed event) are NOT routed here — only legitimate "we
    considered firing but a gate stopped us" reasons surface, so the
    skip log stays signal-rich.
    """
    ts = _now_iso()
    payload: dict[str, Any] = {
        "ts": ts,
        "agent_id": "system",
        "type": "compass_audit_skipped",
        "project_id": project_id,
        "task_id": task_id,
        "reason": reason,
    }
    payload.update(extras)
    _last_skip[project_id] = {
        "ts": ts,
        "reason": reason,
        "task_id": task_id,
        **extras,
    }
    try:
        await bus.publish(payload)
    except Exception:
        logger.exception(
            "compass.audit_watcher: failed to publish skip event "
            "(project=%s, task=%s, reason=%s)",
            project_id, task_id, reason,
        )
    logger.debug(
        "compass.audit_watcher: skipped (project=%s, task=%s, reason=%s)",
        project_id, task_id, reason,
    )


def snapshot_health() -> dict[str, Any]:
    """Return the watcher's current health state as a JSON-safe dict.

    Read-only view for the HTTP endpoint + Coach MCP tool. Includes:
      - enabled: bool — config.AUTO_AUDIT_ENABLED.
      - running: bool — background task alive.
      - watched_event_types: sorted list — the bus filter.
      - debounce_seconds: int — current window.
      - last_fire_by_project: dict[project_id, iso] — most recent fire.
      - last_skip_by_project: dict[project_id, {reason, ts, task_id}] —
        most recent gate-close per project.
      - debounce_keys_active: int — entries in _last_fire (rough
        in-flight indicator).
    """
    return {
        "enabled": bool(config.AUTO_AUDIT_ENABLED),
        "running": is_running(),
        "watched_event_types": sorted(WATCHED_EVENT_TYPES),
        "debounce_seconds": int(config.AUTO_AUDIT_DEBOUNCE_SECONDS),
        "last_fire_by_project": dict(_last_fire_iso),
        "last_skip_by_project": {
            k: dict(v) for k, v in _last_skip.items()
        },
        "debounce_keys_active": len(_last_fire),
    }


# ---------------------------------------------------------------- lifecycle


async def start_audit_watcher() -> None:
    """Start the background subscriber. Idempotent. No-op when the
    feature flag is off.

    Subscribes to the bus **synchronously** before scheduling the
    consumer task. If we deferred the subscribe to inside the task,
    any event published in the window between `create_task` returning
    and the task actually running would be lost — easy to hit in
    tests, possible under load.
    """
    global _current_task, _stopping
    if not config.AUTO_AUDIT_ENABLED:
        logger.info("compass.audit_watcher: disabled via env (HARNESS_COMPASS_AUTO_AUDIT)")
        return
    if is_running():
        return
    _stopping = False
    _last_fire.clear()
    _last_fire_iso.clear()
    _last_skip.clear()
    queue = bus.subscribe()
    loop = asyncio.get_running_loop()
    _current_task = loop.create_task(
        _run(queue), name="harness.compass.audit_watcher",
    )
    logger.info(
        "compass.audit_watcher: started (debounce=%ss, types=%s)",
        config.AUTO_AUDIT_DEBOUNCE_SECONDS,
        sorted(WATCHED_EVENT_TYPES),
    )


async def stop_audit_watcher(timeout: float = 2.0) -> None:
    """Stop the background subscriber and drain. Idempotent."""
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
    """Drain the pre-subscribed queue, dispatch matching events."""
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
                    "compass.audit_watcher: handler crashed on event %r",
                    ev.get("type"),
                )
    finally:
        bus.unsubscribe(queue)


async def _handle_event(ev: dict[str, Any]) -> None:
    """Filter an event and spawn an audit task if appropriate."""
    etype = ev.get("type") or ""
    if etype not in WATCHED_EVENT_TYPES:
        return

    # Respect harness-wide pause: drop the event without firing the
    # audit LLM call. The kanban transition still landed; once
    # unpaused, future transitions audit normally.
    try:
        from server.agents import is_paused  # noqa: PLC0415
        if is_paused():
            return
    except Exception:
        pass

    # Only the plan→execute transition is a Compass-relevant event.
    # Any other stage change is execution-side, handled by kanban's
    # own auditors / shipper.
    if (ev.get("from") or "") != "plan" or (ev.get("to") or "") != "execute":
        return

    task_id = (ev.get("task_id") or "").strip()
    if not task_id:
        return
    project_id = (ev.get("project_id") or "").strip()

    if not project_id:
        # Defensive: kanban transitions normally carry project_id, but
        # we look it up from the task row anyway in case a future emit
        # path skips the auto-stamp.
        task = await _fetch_task(task_id)
        if task is None:
            return
        project_id = task.get("project_id") or ""
        if not project_id:
            return
    else:
        task = await _fetch_task(task_id)
        if task is None:
            return

    if not await _is_compass_enabled(project_id):
        await _emit_skip(
            project_id=project_id,
            task_id=task_id,
            reason="project_disabled",
        )
        return

    # Trajectory gate: only audit when the task's trajectory actually
    # has a plan stage. By construction this is always true for a
    # from='plan' transition, but the guard is cheap and future-proofs
    # against trajectory-shape changes.
    if "plan" not in _trajectory_stages(task):
        await _emit_skip(
            project_id=project_id,
            task_id=task_id,
            reason="trajectory_no_plan",
        )
        return

    if not _debounce_ok(project_id, task_id):
        await _emit_skip(
            project_id=project_id,
            task_id=task_id,
            reason="debounced",
        )
        return

    # Cost gate — read live so a deploy bumping the cap mid-day takes
    # effect without restart. We're pessimistic: if reading the spend
    # fails for any reason, we DON'T fire (better to skip an audit
    # than to blow past the cap when the bookkeeping is broken).
    if not await _within_cost_cap():
        await _emit_skip(
            project_id=project_id,
            task_id=task_id,
            reason="cost_capped",
        )
        return

    artifact = _compose_plan_artifact(task)
    if not artifact:
        await _emit_skip(
            project_id=project_id,
            task_id=task_id,
            reason="artifact_empty",
        )
        return

    # Stamp the wall-clock fire timestamp for the health endpoint
    # (monotonic timestamp in `_last_fire` is debounce-only and not
    # human-readable).
    _last_fire_iso[project_id] = _now_iso()
    asyncio.create_task(
        _safe_audit(project_id, artifact, task_id),
        name=f"compass.audit_watcher.fire:plan:{project_id}:{task_id}",
    )


async def _safe_audit(project_id: str, artifact: str, task_id: str) -> None:
    """Run audit_work in a background task and never raise.

    `audit_work` itself catches LLM errors and degrades to an
    'aligned' verdict; this wrapper is a final safety net so a
    background task failure doesn't go unnoticed in logs.
    """
    try:
        await cmp_audit.audit_work(project_id, artifact)
    except Exception:
        logger.exception(
            "compass.audit_watcher: audit_work failed (project=%s, task=%s)",
            project_id, task_id,
        )


# --------------------------------------------------------- predicates


def _debounce_ok(project_id: str, task_id: str) -> bool:
    """Return True if enough time has passed since the last fire for
    this (project, task_id) tuple. Updates the timestamp on success.
    Window irrelevant in steady state — plan→execute fires once per
    task by construction; debounce just guards against weird re-emits.
    """
    window = config.AUTO_AUDIT_DEBOUNCE_SECONDS
    if window <= 0:
        return True
    key = (project_id, task_id)
    now = time.monotonic()
    last = _last_fire.get(key, 0.0)
    if now - last < window:
        return False
    _last_fire[key] = now
    return True


async def _is_compass_enabled(project_id: str) -> bool:
    """Read `compass_enabled_<id>` from team_config. Same shape as the
    scheduler check — mirrored here to avoid a dependency on the
    scheduler module's internal helper.
    """
    from server.db import configured_conn  # noqa: PLC0415

    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT value FROM team_config WHERE key = ?",
                (config.enabled_key(project_id),),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("compass.audit_watcher: enable-check query failed")
        return False
    if not row:
        return False
    val = (dict(row).get("value") or "").strip().lower()
    return val in ("1", "true", "yes")


async def _within_cost_cap() -> bool:
    """True iff the team's daily spend is below the configured cap.
    A 0/negative cap disables the check (matching agents.py's
    convention)."""
    from server.agents import TEAM_DAILY_CAP_USD, _today_spend  # noqa: PLC0415

    if TEAM_DAILY_CAP_USD <= 0:
        return True
    try:
        spent = await _today_spend()
    except Exception:
        logger.exception("compass.audit_watcher: spend lookup failed; skipping audit")
        return False
    return spent < TEAM_DAILY_CAP_USD


# --------------------------------------------------------- task lookup


async def _fetch_task(task_id: str) -> dict[str, Any] | None:
    """Read the task row needed to compose a plan artifact. Returns
    None if the task doesn't exist (already deleted, race) — the
    handler then drops the event silently.
    """
    from server.db import configured_conn  # noqa: PLC0415

    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id, project_id, title, description, trajectory, "
                "spec_path FROM tasks WHERE id = ?",
                (task_id,),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception(
            "compass.audit_watcher: task lookup failed for %s", task_id,
        )
        return None
    if not row:
        return None
    return dict(row)


def _trajectory_stages(task: dict[str, Any]) -> list[str]:
    """Mirror of `kanban._trajectory_stages` — kept local to avoid an
    import cycle (kanban imports from compass elsewhere). Returns the
    ordered list of stage names from the task's trajectory column."""
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
            if stage:
                out.append(stage)
    return out


# --------------------------------------------------------- artifact prep


def _compose_plan_artifact(task: dict[str, Any]) -> str:
    """Render a one-shot plan artifact for the audit prompt.

    Shape:
        [task-plan] task <id>: <title>

        Trajectory: stage1 → stage2 → stage3

        --- spec ---
        <spec.md body, truncated>

    When spec.md is missing or unreadable we still fire the audit with
    a header-only artifact — the title + trajectory alone gives the
    auditor signal about whether the task pursues the right direction.
    spec.md may be absent on tasks that genuinely skipped the plan
    stage (shouldn't happen — we already gated on trajectory having
    'plan' — but defensive).
    """
    task_id = (task.get("id") or "").strip()
    title = (task.get("title") or "").strip() or "(no title)"
    trajectory = _trajectory_stages(task)
    trajectory_str = " → ".join(trajectory) if trajectory else "(unknown)"

    head = f"[task-plan] task {task_id}: {title}\n\nTrajectory: {trajectory_str}"

    body = _read_spec_body(task)
    if body:
        artifact = f"{head}\n\n--- spec ---\n{body}"
    else:
        # No spec body — include description as a fallback so the
        # auditor isn't reading title alone. Description is set on
        # task creation and usually has the gist.
        desc = (task.get("description") or "").strip()
        if desc:
            artifact = f"{head}\n\n--- description (no spec.md available) ---\n{desc}"
        else:
            artifact = head

    return _truncate(artifact)


def _read_spec_body(task: dict[str, Any]) -> str | None:
    """Read spec.md for the task. Returns the body (truncated to
    `_PLAN_BODY_MAX`) or None when the spec doesn't exist / can't
    be read.

    spec.md lives at `/data/projects/<project_id>/working/tasks/<task_id>/spec.md`
    — outside OUTPUTS_DIR, so we can't reuse `output_extractor.extract_body`
    (its boundary check refuses paths outside the outputs lane).
    spec.md is always .md / UTF-8 text by construction (kanban writers
    only emit markdown), so a direct read is sufficient.
    """
    from server.tasks import is_valid_task_id, spec_path  # noqa: PLC0415

    task_id = (task.get("id") or "").strip()
    project_id = (task.get("project_id") or "").strip()
    if not task_id or not project_id:
        return None
    if not is_valid_task_id(task_id):
        return None

    try:
        path = spec_path(project_id, task_id)
    except ValueError:
        return None

    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        logger.exception(
            "compass.audit_watcher: spec read failed for task %s", task_id,
        )
        return None

    if len(text) > _PLAN_BODY_MAX:
        return (
            text[:_PLAN_BODY_MAX]
            + f"\n\n[truncated — spec was {len(text)} chars]"
        )
    return text


def _truncate(s: str) -> str:
    s = (s or "").strip()
    if len(s) <= _ARTIFACT_TRUNCATE:
        return s
    return s[:_ARTIFACT_TRUNCATE] + f"\n\n[truncated — was {len(s)} chars]"


__all__ = [
    "WATCHED_EVENT_TYPES",
    "is_running",
    "snapshot_health",
    "start_audit_watcher",
    "stop_audit_watcher",
]

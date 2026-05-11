"""Idle-Player polling (Docs/kanban-specs-v2.md §10).

Background loop that wakes Players who could be doing pool / pending
work but aren't. Sibling of the audit-watcher and kanban subscriber;
runs forever until `stop_idle_poller` is called from `lifespan`.

The bus-driven auto-wake on `coord_assign_*` tools is the primary
signal — when Coach assigns or posts to a pool, eligible Players are
woken immediately. The poller is the **safety net**: it catches the
cases where the initial wake missed (Player was over the cost cap,
harness was paused, debounce ate it) and pulls Players in when
Coach forgets to follow up.

The loop:

  - Every `HARNESS_IDLE_POLL_INTERVAL_SECONDS` (default 300s = 5 min):
  - For each Player slot (`p1`..`p10`):
    - Skip if locked.
    - Skip if `current_task_id` is set (already on something).
    - Skip if `agents.status` is currently `'working'` or `'waiting'`
      (a turn is in flight).
    - Skip if the per-Player debounce window hasn't elapsed since
      the last poll-wake (`agents.last_idle_wake_at`).
    - Query for available work for this Player: any
      `task_role_assignments` row where the caller is in
      `eligible_owners` AND `owner IS NULL` AND not completed —
      with a small grace period after `assigned_at` so the initial
      wake gets the obvious chance before the poller fallbacks kick
      in (avoids flooding when Coach just posted).
    - Also query: any `task_role_assignments` row where `owner` is
      this Player AND `completed_at IS NULL` AND the assignment
      hasn't been auto-woken in the last interval (defensive: catches
      missed wakes from the original assign).
    - If any match, call `maybe_wake_agent(slot, …)` and stamp
      `last_idle_wake_at = now`.

Feature flag `HARNESS_IDLE_POLL_ENABLED` (default true) — set false
on cost-constrained deploys; the consumer task still drains its
no-op cycle so nothing backs up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from server.db import configured_conn
from server.events import bus

logger = logging.getLogger("harness.idle_poller")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Player slots the poller considers (Coach is excluded — Coach is
# always-on and has its own recurrence machinery).
PLAYER_SLOTS: tuple[str, ...] = tuple(f"p{i}" for i in range(1, 11))


# ---------------------------------------------------------------- env

def _interval_seconds() -> int:
    raw = os.environ.get("HARNESS_IDLE_POLL_INTERVAL_SECONDS", "300").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 300


def _grace_seconds() -> int:
    """Don't poll-wake on a freshly-assigned pool task — give the
    initial assign-time wake a head start."""
    raw = os.environ.get("HARNESS_IDLE_POLL_GRACE_SECONDS", "60").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 60


def _debounce_seconds() -> int:
    """Per-Player debounce: don't re-wake the same Player within
    this window. Default 30 min so a Player who declined a wake
    isn't pestered every cycle."""
    raw = os.environ.get("HARNESS_IDLE_POLL_DEBOUNCE_SECONDS", "1800").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 1800


def _flag_enabled() -> bool:
    raw = os.environ.get("HARNESS_IDLE_POLL_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _board_silence_seconds() -> int:
    """Board safety ring (§10.4) trigger threshold. Default 30 min."""
    raw = os.environ.get("HARNESS_KANBAN_BOARD_SILENCE_SECONDS", "1800").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 1800


def _board_silence_realert_seconds() -> int:
    """Board safety ring re-alert cooldown. Default 1h."""
    raw = os.environ.get("HARNESS_KANBAN_BOARD_SILENCE_REALERT_SECONDS", "3600").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 3600


def _board_safety_flag_enabled() -> bool:
    raw = os.environ.get("HARNESS_KANBAN_BOARD_SAFETY_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


# Stall sweeper (Docs/kanban-specs-v2.md §10.5). Sibling pass in this
# tick loop — detects tasks whose `last_stage_change_at` is older than
# `HARNESS_KANBAN_STALL_SECONDS` and walks the escalation ladder
# (v0.3.8): rung 1 (nudge assignee) → rung 2 (notify Coach) → rung 3
# (auto-reassign or human_attention) → rung 4 (auto-archive +
# human_attention). `tasks.stall_escalation_level` records which rung
# has been fired so each rung is idempotent across ticks.
def _stall_threshold_seconds() -> int:
    """Rung 1 — nudge the current-stage assignee. Default 30min
    (v0.3.8, halved from 1h). The original 4h default was too long
    for active sessions; the 1h default still let the kanban sit
    silently for an hour before Coach saw anything."""
    raw = os.environ.get("HARNESS_KANBAN_STALL_SECONDS", "1800").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 1800


def _escalate_coach_seconds() -> int:
    """Rung 2 — notify Coach with a 'stall persisting' event so Coach
    intervenes (reassign / advance / archive) before the auto-action
    rungs fire. Default 1h."""
    raw = os.environ.get(
        "HARNESS_KANBAN_ESCALATE_COACH_SECONDS", "3600"
    ).strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 3600


def _escalate_reassign_seconds() -> int:
    """Rung 3 — auto-reassign to another eligible Player from the
    trajectory's `to` list, or fire human_attention if no
    alternative exists. Default 2h."""
    raw = os.environ.get(
        "HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS", "7200"
    ).strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 7200


def _escalate_archive_seconds() -> int:
    """Rung 4 — auto-archive with note + human_attention. The system
    always makes some progress; it never sits silently waiting for an
    assignee who isn't coming back. Default 4h."""
    raw = os.environ.get(
        "HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS", "14400"
    ).strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 14400


def _stall_flag_enabled() -> bool:
    raw = os.environ.get(
        "HARNESS_KANBAN_STALL_ENABLED", "true"
    ).strip().lower()
    return raw not in ("0", "false", "no", "off")


def _stall_target_level(age_seconds: int) -> int:
    """Compute the target escalation rung for a stalled task at this
    age. Rung 0 = no action. Walks the ladder thresholds in order.
    """
    if age_seconds >= _escalate_archive_seconds():
        return 4
    if age_seconds >= _escalate_reassign_seconds():
        return 3
    if age_seconds >= _escalate_coach_seconds():
        return 2
    if age_seconds >= _stall_threshold_seconds():
        return 1
    return 0


# ---------------------------------------------------------------- state

_current_task: asyncio.Task[None] | None = None
_stopping = False


def is_running() -> bool:
    return _current_task is not None and not _current_task.done()


# ---------------------------------------------------------------- lifecycle


async def start_idle_poller() -> None:
    """Start the background poller. Idempotent. No-op when disabled."""
    global _current_task, _stopping
    if not _flag_enabled():
        logger.info("idle_poller: disabled (HARNESS_IDLE_POLL_ENABLED=false)")
        return
    if is_running():
        return
    _stopping = False
    loop = asyncio.get_running_loop()
    _current_task = loop.create_task(_run(), name="harness.idle_poller")
    logger.info(
        "idle_poller: started (interval=%ss, grace=%ss, debounce=%ss)",
        _interval_seconds(), _grace_seconds(), _debounce_seconds(),
    )


async def stop_idle_poller(timeout: float = 2.0) -> None:
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


async def _run() -> None:
    """Forever-loop. Sleeps `interval` between sweeps. Per-sweep
    exception isolation — one bad pass doesn't kill the loop."""
    interval = _interval_seconds()
    try:
        # Initial small delay so a fresh boot doesn't fire before the
        # rest of lifespan finishes wiring (telegram bridge, db mig,
        # etc.). Doesn't change the steady-state cadence.
        await asyncio.sleep(min(15, interval))
        while not _stopping:
            try:
                await sweep_once()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("idle_poller: sweep crashed")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
    except asyncio.CancelledError:
        return


async def sweep_once() -> int:
    """Run a single sweep. Returns the number of wake-up calls made
    (player-side; the stall sweeper's + reconciliation's emits aren't
    counted here). Exposed for tests so they can drive the loop
    deterministically instead of waiting for the asyncio sleep cycle."""
    from server.agents import is_paused  # noqa: PLC0415
    if is_paused():
        return 0
    woken = 0
    for slot in PLAYER_SLOTS:
        try:
            if await _maybe_wake_idle(slot):
                woken += 1
        except Exception:
            logger.exception(
                "idle_poller: per-slot wake failed (slot=%s)", slot
            )
    # Stall sweeper runs after the per-Player wakes so a freshly woken
    # Player doesn't simultaneously trigger a stale alert (tiny race
    # but worth avoiding). The remaining four sweeps read disjoint
    # data (stall: tasks+role_assignments; reconciliation: tasks+events;
    # board safety: events; watchdog: agents+events) so we run them
    # concurrently — the slowest one no longer blocks the others, and
    # the tick wall-time approaches max(sweeps) instead of sum(sweeps).
    # Each sweep already catches its own exceptions internally; the
    # outer return_exceptions guard is a defense-in-depth backstop.
    async def _safe(coro_factory, label):
        try:
            await coro_factory()
        except Exception:
            logger.exception("idle_poller: %s crashed", label)

    from server.kanban_watchdog import sweep_once as _watchdog_sweep
    await asyncio.gather(
        _safe(stall_sweep_once, "stall sweep"),
        _safe(reconciliation_sweep_once, "reconciliation sweep"),
        _safe(board_safety_ring_once, "board safety ring"),
        # Soft-stall watchdog (v0.3.9): tier 1 SQL filter + bundled
        # Haiku call to catch agents stuck in ways the deterministic
        # ladder misses (declared done but didn't advance the kanban;
        # looping; erroring without recovery). Cost-capped + dedup-
        # gated; most ticks short-circuit at tier 1 with zero candidates.
        # See Docs/kanban-specs-v2.md §10.7.
        _safe(_watchdog_sweep, "watchdog sweep"),
    )
    return woken


async def stall_sweep_once() -> int:
    """Walk the v0.3.8 stall-escalation ladder for every non-archive,
    non-blocked task whose `last_stage_change_at` exceeds the rung-1
    threshold.

    Ladder (env-overridable):
      rung 1 — `_stall_threshold_seconds()`        default 30min
        Nudge the current-stage assignee + emit `task_stage_stale`
        routed to Coach (legacy event preserved for back-compat).
      rung 2 — `_escalate_coach_seconds()`         default 1h
        Emit `task_stall_persisting` and wake Coach with an explicit
        "intervene before auto-action" message.
      rung 3 — `_escalate_reassign_seconds()`      default 2h
        Try auto-reassign to another eligible Player from the
        stage's `eligible_owners` (excluding the current owner +
        locked Players). If no alternative exists, fire
        `human_attention`. Emits `task_stall_auto_reassigned` or
        `task_stall_no_alternative`.
      rung 4 — `_escalate_archive_seconds()`       default 4h
        Auto-archive via `_transition` + fire `human_attention`.
        Emits `task_stall_auto_archived`.

    Per-rung idempotence: `tasks.stall_escalation_level` records the
    last fired rung. We walk current_level+1 → target_level firing
    each in order, stamping the level after each successful action so
    a crash mid-walk leaves coherent state. The legacy
    `tasks.stale_alert_at` is also stamped on every rung-fire (used
    by `_build_stalled_tasks_rows` for the Coach prompt rollup;
    cleared together with the level when a stage advances).

    Returns the number of tasks where AT LEAST ONE rung fired this
    sweep (matches the legacy "alerted count" semantics)."""
    if not _stall_flag_enabled():
        return 0
    rung1_threshold = _stall_threshold_seconds()

    c = await configured_conn()
    try:
        cur = await c.execute(
            """
            SELECT id, status, owner, project_id, title,
                   last_stage_change_at, stale_alert_at,
                   stall_escalation_level
              FROM tasks
             WHERE status NOT IN ('archive')
               AND blocked = 0
               AND last_stage_change_at IS NOT NULL
               AND (julianday('now') - julianday(last_stage_change_at))
                   * 86400.0 > ?
            """,
            (rung1_threshold,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()

    if not rows:
        return 0

    # Batch-fetch the active role row for every stalled (task_id, role)
    # pair in one query. The prior code opened a fresh connection per
    # stalled task and ran a separate query, which is O(N) DB round-
    # trips per sweep — unnecessary when one IN-clause query covers
    # the whole working set. We over-fetch (all roles for these tasks,
    # not just the per-task active role) and filter in Python below.
    task_id_to_role: dict[str, str] = {}
    for r in rows:
        role = _role_for_stage(r["status"])
        if role:
            task_id_to_role[r["id"]] = role
    role_rows_by_key: dict[tuple[str, str], dict] = {}
    if task_id_to_role:
        placeholders = ",".join("?" * len(task_id_to_role))
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT task_id, role, id, eligible_owners, owner "
                "FROM task_role_assignments "
                f"WHERE task_id IN ({placeholders}) "
                "AND completed_at IS NULL AND superseded_by IS NULL "
                "ORDER BY task_id, role, assigned_at DESC",
                tuple(task_id_to_role.keys()),
            )
            # Collapse to the first (most recent) row per (task_id, role).
            for raw in await cur.fetchall():
                rrow = dict(raw)
                key = (rrow["task_id"], rrow["role"])
                if key not in role_rows_by_key:
                    role_rows_by_key[key] = rrow
        finally:
            await c.close()

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    progressed = 0
    for r in rows:
        try:
            last_change = _parse_iso(r.get("last_stage_change_at"))
            if last_change is None:
                continue
            age_seconds = int((now_dt - last_change).total_seconds())
            current_level = int(r.get("stall_escalation_level") or 0)
            target_level = _stall_target_level(age_seconds)
            if target_level <= current_level:
                continue

            # Resolve the active role row for the CURRENT stage so
            # we ladder-fire against the actual blocker, not always
            # tasks.owner (v0.3.4 bug-fix preserved). Looked up from
            # the batch-fetched map above (no per-task DB round-trip).
            stage = r["status"]
            role = _role_for_stage(stage)
            eligible: list[str] = []
            stage_owner: str | None = None
            role_row_id: int | None = None
            if role:
                rrow = role_rows_by_key.get((r["id"], role))
                if rrow is not None:
                    role_row_id = rrow.get("id")
                    stage_owner = rrow.get("owner")
                    try:
                        parsed = json.loads(rrow.get("eligible_owners") or "[]")
                        if isinstance(parsed, list):
                            eligible = [str(x) for x in parsed]
                    except Exception:
                        eligible = []
            stall_owner = stage_owner or r.get("owner")

            # Walk every unfired rung up to the target. Each rung
            # commits its level before the next runs so a crash
            # leaves coherent state.
            for rung in range(current_level + 1, target_level + 1):
                if rung == 1:
                    await _fire_rung_1(
                        task=r, stage=stage, age_seconds=age_seconds,
                        stall_owner=stall_owner, eligible=eligible,
                        now_iso=now_iso,
                    )
                elif rung == 2:
                    await _fire_rung_2(
                        task=r, stage=stage, age_seconds=age_seconds,
                        stall_owner=stall_owner, now_iso=now_iso,
                    )
                elif rung == 3:
                    reassigned = await _fire_rung_3(
                        task=r, stage=stage, role=role,
                        role_row_id=role_row_id, eligible=eligible,
                        stall_owner=stall_owner, now_iso=now_iso,
                    )
                    if reassigned:
                        # AUDIT FIX: rung 3 success resets the
                        # task's stall window inside _fire_rung_3
                        # (last_stage_change_at = now,
                        # stall_escalation_level = 0). Break out so
                        # rung 4 doesn't fire on the same sweep when
                        # target_level was 4 — that would archive
                        # the freshly-reassigned task seconds after
                        # handoff, defeating the whole rung.
                        break
                    # No alternative was reachable — stamp level=3 so
                    # the next sweep walks rung 4 if still stuck.
                    await _stamp_escalation_level(
                        task_id=r["id"], level=rung, now_iso=now_iso,
                    )
                    continue
                elif rung == 4:
                    await _fire_rung_4(
                        task=r, stage=stage, age_seconds=age_seconds,
                        now_iso=now_iso,
                    )
                    # Rung 4 archives the task and resets the level
                    # to 0 itself (so a re-opened task starts fresh).
                    # Skip the post-stamp — it would overwrite that.
                    break
                # Stamp progress after each rung so per-rung
                # idempotence holds across crashes.
                await _stamp_escalation_level(
                    task_id=r["id"], level=rung, now_iso=now_iso,
                )
            progressed += 1
        except Exception:
            logger.exception(
                "idle_poller: stall ladder failed for task %s", r.get("id")
            )
    return progressed


async def _stamp_escalation_level(
    *, task_id: str, level: int, now_iso: str,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE tasks SET stall_escalation_level = ?, "
            "stale_alert_at = ? WHERE id = ?",
            (level, now_iso, task_id),
        )
        await c.commit()
    finally:
        await c.close()


async def _fire_rung_1(
    *, task: dict, stage: str, age_seconds: int,
    stall_owner: str | None, eligible: list[str], now_iso: str,
) -> None:
    """Rung 1 — nudge the current-stage assignee + emit
    `task_stage_stale`. Same as the v0.3.4 behavior, just at 30min
    instead of 1h."""
    await bus.publish({
        "ts": now_iso,
        "agent_id": "system",
        "type": "task_stage_stale",
        "task_id": task["id"],
        "stage": stage,
        "age_seconds": age_seconds,
        "owner": stall_owner,
        "task_executor": task.get("owner"),
        "eligible_owners": eligible,
        "to": "coach",
    })
    if stall_owner:
        try:
            from server.agents import maybe_wake_agent
            from server.tools import _with_player_reminder
            age_min = max(1, age_seconds // 60)
            nudge = _with_player_reminder(_stall_nudge_for_stage(
                task_id=task["id"], stage=stage, age_min=age_min,
            ))
            await maybe_wake_agent(
                stall_owner, nudge,
                bypass_debounce=False,
                wake_source="kanban_stall",
            )
        except Exception:
            pass


async def _fire_rung_2(
    *, task: dict, stage: str, age_seconds: int,
    stall_owner: str | None, now_iso: str,
) -> None:
    """Rung 2 — Coach intervention call. Different from rung 1's
    `task_stage_stale` (which can be conflated with first-time
    stalls): this event names the persistence and the next auto-
    action so Coach knows the deadline."""
    age_min = max(1, age_seconds // 60)
    rung1_min = max(1, _stall_threshold_seconds() // 60)
    next_reassign_min = max(0, _escalate_reassign_seconds() // 60 - age_min)
    await bus.publish({
        "ts": now_iso,
        "agent_id": "system",
        "type": "task_stall_persisting",
        "task_id": task["id"],
        "stage": stage,
        "age_seconds": age_seconds,
        "owner": stall_owner,
        "task_executor": task.get("owner"),
        "next_action": "auto_reassign",
        "next_action_in_min": next_reassign_min,
        "to": "coach",
    })
    try:
        from server.agents import maybe_wake_agent
        body = (
            f"Stall persisting on task {task['id']!r} (stage {stage}, "
            f"blocker {stall_owner or '(unassigned)'}). The Player "
            f"didn't move on the {rung1_min}-min nudge. Auto-"
            f"reassign fires in ~{next_reassign_min} min unless you "
            f"intervene."
        )
        # AUDIT FIX: bypass debounce so the escalation reaches Coach
        # even if Coach's wake debounce window is currently active
        # (recently woke for unrelated traffic). Rung 2 IS the
        # escalation point — silently dropping it because Coach was
        # busy 60s ago defeats the whole rung.
        await maybe_wake_agent(
            "coach", body,
            bypass_debounce=True,
            wake_source="kanban_stall",
        )
    except Exception:
        pass


async def _fire_rung_3(
    *, task: dict, stage: str, role: str | None,
    role_row_id: int | None, eligible: list[str],
    stall_owner: str | None, now_iso: str,
) -> bool:
    """Rung 3 — auto-reassign to an alternative Player from the
    stage's eligible_owners, excluding the current owner + locked
    Players + Players already busy on another task. If no
    alternative is reachable, fire `human_attention` so the human
    can step in.

    Returns True when the auto-reassign succeeded (caller should
    `break` out of the rung walk because the task got a fresh
    window and rung 4 must NOT fire on the same sweep). Returns
    False when no_alternative fallback fired (caller should stamp
    level=3 and let rung 4 fire on a later sweep if still stuck).

    Auto-reassign is intentionally narrow: it only swaps `owner` on
    the existing role row (not a full supersede), and only when
    `eligible_owners` lists at least one alternative. Coach can
    rewrite the trajectory or pick a different Player at any rung;
    this rung is the fallback when Coach also went silent.
    """
    if not role or not role_row_id or not stall_owner:
        await _fire_human_attention_no_alt(
            task=task, stage=stage, reason="no_role_row", now_iso=now_iso,
        )
        return False
    alternatives: list[str] = []
    for slot in eligible:
        if slot == stall_owner:
            continue
        try:
            if await _is_locked(slot):
                continue
            # AUDIT FIX: also skip Players who are already on
            # another task. Without this, the auto-reassign yanks
            # them off whatever they were doing — same shape as
            # the v0.3.6 reassignment-without-stand-down problem.
            if await _has_active_task(slot):
                continue
        except Exception:
            continue
        alternatives.append(slot)
    if not alternatives:
        await _fire_human_attention_no_alt(
            task=task, stage=stage, reason="no_alternative",
            now_iso=now_iso,
        )
        return False
    new_owner = alternatives[0]
    c = await configured_conn()
    try:
        # AUDIT-2 FIX: guard the role-row UPDATE against concurrent
        # supersede. If Coach assigned a new auditor between the
        # main loop's read and this UPDATE, our `role_row_id` may
        # already be `completed_at IS NOT NULL` or `superseded_by
        # IS NOT NULL`. Without the guard we'd write owner to an
        # inactive row + emit a misleading auto_reassigned event.
        # On race-loss (rowcount == 0), abort and fire no_alt so
        # the next sweep can re-evaluate against the fresh state.
        cur = await c.execute(
            "UPDATE task_role_assignments "
            "SET owner = ?, claimed_at = ? "
            "WHERE id = ? "
            "AND completed_at IS NULL AND superseded_by IS NULL",
            (new_owner, now_iso, role_row_id),
        )
        if cur.rowcount == 0:
            await c.commit()
            await c.close()
            await _fire_human_attention_no_alt(
                task=task, stage=stage, reason="role_row_changed",
                now_iso=now_iso,
            )
            return False
        if role == "executor":
            await c.execute(
                "UPDATE tasks SET owner = ? WHERE id = ?",
                (new_owner, task["id"]),
            )
            await c.execute(
                "UPDATE agents SET current_task_id = NULL "
                "WHERE id = ? AND current_task_id = ?",
                (stall_owner, task["id"]),
            )
            await c.execute(
                "UPDATE agents SET current_task_id = ? WHERE id = ?",
                (task["id"], new_owner),
            )
        # AUDIT FIX (v0.3.8.1): reset the stall window so the new
        # owner gets a fresh ladder. Without this, the next sweep
        # would see age > rung-4 threshold, fire rung 4, and
        # archive the task seconds after the handoff.
        await c.execute(
            "UPDATE tasks SET last_stage_change_at = ?, "
            "stale_alert_at = NULL, stall_escalation_level = 0 "
            "WHERE id = ?",
            (now_iso, task["id"]),
        )
        await c.commit()
    finally:
        await c.close()
    await bus.publish({
        "ts": now_iso,
        "agent_id": "system",
        "type": "task_stall_auto_reassigned",
        "task_id": task["id"],
        "stage": stage,
        "role": role,
        "from_owner": stall_owner,
        "to_owner": new_owner,
        "to": "coach",
    })
    # Wake the new owner with the same role-entry framing the
    # subscriber would use on a normal stage entry.
    try:
        from server.kanban import _wake_role_or_emit_needed
        await _wake_role_or_emit_needed(task_id=task["id"], role=role)
    except Exception:
        pass
    # Also stand-down the old owner so they stop work cleanly.
    try:
        from server.kanban import send_role_stand_down
        await send_role_stand_down(
            task_id=task["id"], role=role,
            displaced=[stall_owner], new_owners=[new_owner],
        )
    except Exception:
        pass
    return True


async def _has_active_task(slot: str) -> bool:
    """True when `slot`'s `agents.current_task_id` points at a
    non-archive task. Used by `_fire_rung_3` to skip Players who'd
    be yanked off their current work by an auto-reassign."""
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT a.current_task_id, t.status FROM agents a "
            "LEFT JOIN tasks t ON t.id = a.current_task_id "
            "WHERE a.id = ?",
            (slot,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return False
    d = dict(row)
    if not d.get("current_task_id"):
        return False
    # If the row joined a task and it's archived, treat as free.
    status = d.get("status")
    if status == "archive":
        return False
    return True


async def _fire_human_attention_no_alt(
    *, task: dict, stage: str, reason: str, now_iso: str,
) -> None:
    """Rung 3 fallback when auto-reassign isn't viable: fire
    `human_attention` (surfaces in EnvPane + Telegram) so the human
    can intervene before rung 4 archives the task."""
    next_archive_min = max(
        0,
        (_escalate_archive_seconds() - _escalate_reassign_seconds()) // 60,
    )
    await bus.publish({
        "ts": now_iso,
        "agent_id": "system",
        "type": "human_attention",
        "subject": f"Task {task['id']} stalled, no alternative assignee",
        "body": (
            f"Task {task['id']!r} (stage {stage}) is past the "
            f"auto-reassign window but no alternative Player is "
            f"available (reason: {reason}). Auto-archive will fire "
            f"in ~{next_archive_min} min unless you intervene."
        ),
        "urgency": "high",
        "to": "human",
    })
    await bus.publish({
        "ts": now_iso,
        "agent_id": "system",
        "type": "task_stall_no_alternative",
        "task_id": task["id"],
        "stage": stage,
        "reason": reason,
        "to": "coach",
    })


async def _fire_rung_4(
    *, task: dict, stage: str, age_seconds: int, now_iso: str,
) -> None:
    """Rung 4 — auto-archive past the deadline. The system always
    makes some progress; it never sits silently waiting. Fires
    `human_attention` so the human knows a task was sacrificed.

    AUDIT-2 FIX: also (a) mark every active role row on the task as
    completed_at = now (otherwise they're orphaned — visible to
    queries that look at "any active row" without a status filter),
    and (b) fire stand-down to the current-stage assignee so a
    Player who's actively working when rung 4 hits gets an explicit
    stop-work signal instead of silently working into the void.
    """
    age_hours = max(1, age_seconds // 3600)
    # Pre-read: who is the current-stage assignee (so we can fire
    # stand-down after commit) and what role rows are about to be
    # orphaned.
    role = _role_for_stage(stage)
    stage_owner: str | None = None
    if role:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT owner FROM task_role_assignments "
                "WHERE task_id = ? AND role = ? "
                "AND completed_at IS NULL AND superseded_by IS NULL "
                "ORDER BY assigned_at DESC LIMIT 1",
                (task["id"], role),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
        if row:
            stage_owner = dict(row).get("owner")
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE tasks SET status = 'archive', "
            "completed_at = ?, archived_at = ?, "
            "last_stage_change_at = ?, "
            "stale_alert_at = NULL, stall_escalation_level = 0 "
            "WHERE id = ? AND status != 'archive'",
            (now_iso, now_iso, now_iso, task["id"]),
        )
        # AUDIT-2 FIX: close every active role row so they don't
        # show up as "still active" in subsequent queries that
        # don't also filter on tasks.status.
        await c.execute(
            "UPDATE task_role_assignments SET completed_at = ? "
            "WHERE task_id = ? "
            "AND completed_at IS NULL AND superseded_by IS NULL",
            (now_iso, task["id"]),
        )
        # Release any agent that was holding it.
        owner = task.get("owner")
        if owner:
            await c.execute(
                "UPDATE agents SET current_task_id = NULL "
                "WHERE id = ? AND current_task_id = ?",
                (owner, task["id"]),
            )
        await c.commit()
    finally:
        await c.close()
    # AUDIT-2 FIX: stand-down the current-stage assignee with the
    # canonical "STOP work" wake so a Player who's actively
    # working at rung-4 fire-time gets an explicit stop signal.
    if stage_owner and role:
        try:
            from server.kanban import send_role_stand_down
            await send_role_stand_down(
                task_id=task["id"], role=role,
                displaced=[stage_owner], new_owners=[],
            )
        except Exception:
            pass
    await bus.publish({
        "ts": now_iso,
        "agent_id": "system",
        "type": "task_stage_changed",
        "task_id": task["id"],
        "from": stage,
        "to": "archive",
        "reason": "auto_archive_stalled",
        "note": f"auto-archived after {age_hours}h with no progress",
        "owner": task.get("owner"),
    })
    await bus.publish({
        "ts": now_iso,
        "agent_id": "system",
        "type": "task_stall_auto_archived",
        "task_id": task["id"],
        "stage_before": stage,
        "age_seconds": age_seconds,
        "to": "coach",
    })
    await bus.publish({
        "ts": now_iso,
        "agent_id": "system",
        "type": "human_attention",
        "subject": f"Task {task['id']} auto-archived (stalled {age_hours}h)",
        "body": (
            f"Task {task['id']!r} ({(task.get('title') or '')[:80]}) "
            f"was auto-archived after sitting in stage {stage} for "
            f"{age_hours}h past every nudge + Coach escalation + "
            f"reassignment attempt. Re-create the task if the work "
            f"still matters."
        ),
        "urgency": "high",
        "to": "human",
    })


async def _is_locked(slot: str) -> bool:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT locked FROM agents WHERE id = ?", (slot,)
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    return bool(dict(row).get("locked")) if row else False


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _role_for_stage(stage: str) -> str | None:
    return {
        "plan": "planner",
        "execute": "executor",
        "audit_syntax": "auditor_syntax",
        "audit_semantics": "auditor_semantics",
        "ship": "shipper",
    }.get(stage)


def _stall_nudge_for_stage(
    *, task_id: str, stage: str, age_min: int
) -> str:
    """Stage-aware reminder text for the stall sweeper. v2 strip:
    the matching completion tool + blocked-clause + tool-not-visible
    escape are all in the system prompt (project CLAUDE.md template
    + role baseline) — the wake just delivers the fact. The canonical
    turn-end reminder is appended by the caller via
    `_with_player_reminder`."""
    return (
        f"Reminder: task {task_id} has been in {stage} for "
        f"{age_min} minutes with no progress signal."
    )


async def _maybe_wake_idle(slot: str) -> bool:
    """Decide whether `slot` should be woken; if yes, fire the wake
    + stamp `last_idle_wake_at`. Returns True if we woke them."""
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT locked, current_task_id, status, last_idle_wake_at "
            "FROM agents WHERE id = ?",
            (slot,),
        )
        agent = await cur.fetchone()
    finally:
        await c.close()
    if agent is None:
        return False
    a = dict(agent)
    if a.get("locked"):
        return False
    if a.get("current_task_id"):
        return False
    if a.get("status") in ("working", "waiting"):
        return False

    # Per-Player debounce.
    last_at = a.get("last_idle_wake_at")
    if last_at and _debounce_seconds() > 0:
        try:
            last_dt = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if (now - last_dt).total_seconds() < _debounce_seconds():
                return False
        except Exception:
            # Unparseable timestamp — treat as never-woken.
            pass

    available = await _has_available_work(slot)
    if not available:
        return False
    reason, task_id = available

    try:
        from server.agents import maybe_wake_agent
        from server.tools import _with_player_reminder
        # v2 strip: the wake had a long "call coord_my_assignments,
        # read the Next-action footer, do the work, don't treat the
        # response as a status report" trailer that's now in the
        # system prompt + the canonical turn-end reminder. One
        # short pointer is all the wake needs (no specific event
        # triggered this — it's an idle catch-all).
        wake_text = _with_player_reminder(
            "You have actionable work pending — check coord_my_assignments."
        )
        did_wake = await maybe_wake_agent(
            slot, wake_text,
            bypass_debounce=False,
            wake_source="kanban_idle_poller",
        )
    except Exception:
        logger.exception("idle_poller: maybe_wake_agent failed for %s", slot)
        return False
    if not did_wake:
        return False

    now_iso = datetime.now(timezone.utc).isoformat()
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET last_idle_wake_at = ? WHERE id = ?",
            (now_iso, slot),
        )
        await c.commit()
    finally:
        await c.close()

    await bus.publish({
        "ts": now_iso,
        "agent_id": slot,
        "type": "idle_player_woken",
        "reason": reason,
        "task_id": task_id,
    })
    return True


async def _has_available_work(slot: str) -> tuple[str, str | None] | None:
    """Return `(reason, task_id)` if `slot` has something they could
    work on, else None.

    v2 (Docs/kanban-specs-v2.md §10.1): pools are FYI only. There is
    no claim path; Coach assigns named slots via `coord_approve_stage`.
    The only legitimate idle-wake reason is a HARD-assigned role row
    whose stage just became active and whose original assign-time
    wake was rejected (cost cap, paused). Coach assigns again, fine —
    but the safety net catches the missed-wake edge case so a Player
    doesn't sit idle on a real assignment.
    """
    c = await configured_conn()
    try:
        # Hard-assigned but not started: there's an active role row
        # owned by `slot` that hasn't completed. The first assign-time
        # wake might have missed; we re-fire here.
        cur = await c.execute(
            "SELECT r.task_id FROM task_role_assignments r "
            "JOIN tasks t ON t.id = r.task_id "
            "WHERE r.owner = ? AND r.completed_at IS NULL "
            "AND r.superseded_by IS NULL "
            "AND ("
            "  (r.role = 'planner' AND t.status = 'plan') "
            "  OR (r.role = 'auditor_syntax' AND t.status = 'audit_syntax') "
            "  OR (r.role = 'auditor_semantics' AND t.status = 'audit_semantics') "
            "  OR (r.role = 'shipper' AND t.status = 'ship')"
            ") "
            "ORDER BY assigned_at LIMIT 1",
            (slot,),
        )
        row = await cur.fetchone()
        if row:
            return ("pending_role_assignment", dict(row)["task_id"])
    finally:
        await c.close()
    return None


# ---------------------------------------------------------------- reconciliation
#
# v0.3.8 reconciliation sweep — catches the "Player did the work but
# the kanban didn't notice" failure mode (the recurring p1/p3/p8
# trace shape). Read-only: walks each non-archive task's folder on
# disk, diffs against `tasks.spec_path` / `task_role_assignments.report_path`,
# emits a structured event to Coach when an artifact is on disk but
# unrecorded. NEVER mutates DB rows itself — Coach uses the existing
# `coord_write_task_spec(on_behalf_of=...)` /
# `coord_submit_audit_report(on_behalf_of=...)` overrides to commit
# the artifact through normal channels.
#
# Per-finding dedupe: in-memory map (sha256 of finding key → last
# emit ts), TTL = `HARNESS_KANBAN_RECONCILE_TTL_SECONDS` (default 1h).
# Restarts re-emit, which is the right behavior — humans probably
# want the reminder again after a deploy if the artifact still sits.

_reconcile_emitted: dict[str, str] = {}  # finding_key → ISO timestamp


def _reconcile_flag_enabled() -> bool:
    raw = os.environ.get(
        "HARNESS_KANBAN_RECONCILE_ENABLED", "true"
    ).strip().lower()
    return raw not in ("0", "false", "no", "off")


def _reconcile_ttl_seconds() -> int:
    raw = os.environ.get(
        "HARNESS_KANBAN_RECONCILE_TTL_SECONDS", "3600"
    ).strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 3600


def _reconcile_should_emit(key: str, now_dt: datetime) -> bool:
    """Per-finding TTL dedupe so we don't spam Coach every 5min for
    the same disk artifact. Returns True + stamps the timestamp when
    the finding is fresh; False when within TTL."""
    ttl = _reconcile_ttl_seconds()
    last = _reconcile_emitted.get(key)
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if (now_dt - last_dt).total_seconds() < ttl:
                return False
        except Exception:
            pass
    _reconcile_emitted[key] = now_dt.isoformat()
    return True


async def reconciliation_sweep_once() -> int:
    """Walk every non-archive task; for each, check disk for spec.md
    and audits/audit_*.md; emit a structured event to Coach when the
    artifact exists but the kanban hasn't recorded it. Returns the
    number of fresh findings emitted this sweep.

    Spec check:
      `<task_dir>/spec.md` exists AND `tasks.spec_path` IS NULL
        → emit `task_spec_unrecorded{task_id, spec_path, planner?}`

    Audit check:
      `<task_dir>/audits/audit_<round>_<kind>.md` exists AND no
      `task_role_assignments` row for the matching kind has
      `report_path = <relative_path>`
        → emit `task_audit_unrecorded{task_id, kind, round,
          report_path, auditor?}`
    """
    if not _reconcile_flag_enabled():
        return 0
    from pathlib import Path
    from server.tasks import (
        audit_report_filename,
        audit_report_relative_path,
        is_valid_task_id,
        spec_path as _spec_path_helper,
        spec_relative_path,
    )

    # Pull every active task. Cap at 200 — a project with more open
    # tasks than that has bigger problems than reconciliation.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, project_id, status, spec_path "
            "FROM tasks WHERE status != 'archive' "
            "ORDER BY last_stage_change_at DESC LIMIT 200"
        )
        tasks = [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()
    if not tasks:
        return 0

    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    emitted = 0
    for t in tasks:
        task_id = t["id"]
        project_id = t.get("project_id") or "misc"
        if not is_valid_task_id(task_id):
            continue
        try:
            spec_abs = _spec_path_helper(project_id, task_id)
        except ValueError:
            continue

        # ---- spec.md unrecorded ----
        try:
            spec_on_disk = spec_abs.is_file()
        except Exception:
            spec_on_disk = False
        if spec_on_disk and not (t.get("spec_path") or ""):
            key = f"spec:{project_id}:{task_id}"
            if _reconcile_should_emit(key, now_dt):
                planner = await _resolve_active_role_owner(
                    task_id=task_id, role="planner",
                )
                await bus.publish({
                    "ts": now_iso,
                    "agent_id": "system",
                    "type": "task_spec_unrecorded",
                    "task_id": task_id,
                    "project_id": project_id,
                    "spec_path": spec_relative_path(project_id, task_id),
                    "planner": planner,
                    "to": "coach",
                })
                emitted += 1

        # ---- audits/*.md unrecorded ----
        audits_dir = (
            spec_abs.parent / "audits"
            if spec_abs.parent.name == task_id
            else None
        )
        if audits_dir and audits_dir.is_dir():
            try:
                audit_files = sorted(audits_dir.glob("audit_*.md"))
            except Exception:
                audit_files = []
            recorded_paths = await _audit_report_paths_for_task(task_id)
            for af in audit_files:
                rel = audit_report_relative_path_from_filename(
                    project_id, task_id, af.name,
                )
                if rel is None:
                    continue
                if rel in recorded_paths:
                    continue
                # Parse round/kind back out for the event body.
                rk = _parse_audit_filename(af.name)
                if rk is None:
                    continue
                round_num, kind = rk
                key = f"audit:{project_id}:{task_id}:{round_num}:{kind}"
                if not _reconcile_should_emit(key, now_dt):
                    continue
                role = (
                    "auditor_syntax" if kind == "syntax"
                    else "auditor_semantics"
                )
                auditor = await _resolve_active_role_owner(
                    task_id=task_id, role=role,
                )
                await bus.publish({
                    "ts": now_iso,
                    "agent_id": "system",
                    "type": "task_audit_unrecorded",
                    "task_id": task_id,
                    "project_id": project_id,
                    "kind": kind,
                    "round": round_num,
                    "report_path": rel,
                    "auditor": auditor,
                    "to": "coach",
                })
                emitted += 1
    return emitted


def _parse_audit_filename(fname: str) -> tuple[int, str] | None:
    """Parse `audit_<round>_<kind>.md` back into `(round, kind)`.
    Returns None on malformed input — non-canonical files are
    ignored rather than triggering a spurious unrecorded finding.
    """
    import re as _re
    m = _re.fullmatch(r"audit_(\d+)_(syntax|semantics)\.md", fname)
    if not m:
        return None
    try:
        return (int(m.group(1)), m.group(2))
    except ValueError:
        return None


def audit_report_relative_path_from_filename(
    project_id: str, task_id: str, filename: str,
) -> str | None:
    rk = _parse_audit_filename(filename)
    if rk is None:
        return None
    return (
        f"projects/{project_id}/working/tasks/{task_id}/"
        f"audits/{filename}"
    )


async def _audit_report_paths_for_task(task_id: str) -> set[str]:
    """All `report_path` values recorded on auditor role rows for
    this task (active or completed). Used to determine which on-disk
    audits are 'unrecorded'."""
    out: set[str] = set()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT report_path FROM task_role_assignments "
            "WHERE task_id = ? AND role IN ('auditor_syntax', "
            "'auditor_semantics') AND report_path IS NOT NULL",
            (task_id,),
        )
        rows = await cur.fetchall()
    finally:
        await c.close()
    for r in rows:
        path = dict(r).get("report_path")
        if isinstance(path, str) and path:
            out.add(path)
    return out


async def _resolve_active_role_owner(
    *, task_id: str, role: str,
) -> str | None:
    """The current active assignee for a (task, role). Used in
    reconciliation event payloads so Coach knows which Player wrote
    the artifact (Coach's `on_behalf_of` argument)."""
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT owner FROM task_role_assignments "
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
    return dict(row).get("owner")


async def board_safety_ring_once(project_id: str | None = None) -> int:
    """Board safety ring (§10.4). Detects board-wide stagnation: no
    `task_stage_changed` event written for the project in
    `HARNESS_KANBAN_BOARD_SILENCE_SECONDS` AND ≥1 non-archive task on
    the board. Emits `kanban_board_stalled` + wakes Coach with
    `bypass_debounce=True`. Stamps `team_config['kanban_board_silence_alerted_at']`
    so it doesn't re-fire every tick — re-armed once a fresh
    `task_stage_changed` event lands or after the realert cooldown.
    Returns 1 if a fresh alert was emitted this sweep, 0 otherwise.
    """
    if not _board_safety_flag_enabled():
        return 0
    if project_id is None:
        from server.db import resolve_active_project
        try:
            project_id = await resolve_active_project()
        except Exception:
            return 0
    if not project_id:
        return 0

    silence_threshold = _board_silence_seconds()
    realert_cooldown = _board_silence_realert_seconds()
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    alert_key = f"kanban_board_silence_alerted_at:{project_id}"

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) AS n, "
            "       MAX(last_stage_change_at) AS last_change, "
            "       MAX(created_at) AS last_created "
            "FROM tasks WHERE project_id = ? AND status != 'archive'",
            (project_id,),
        )
        row = await cur.fetchone()
        if not row:
            return 0
        rd = dict(row)
        active_count = int(rd["n"] or 0)
        if active_count == 0:
            return 0
        task_last_change = rd.get("last_change")
        task_last_created = rd.get("last_created")

        cur = await c.execute(
            "SELECT ts FROM project_events "
            "WHERE project_id = ? AND type = 'task_stage_changed' "
            "ORDER BY ts DESC LIMIT 1",
            (project_id,),
        )
        row = await cur.fetchone()
        event_last_change = dict(row)["ts"] if row else None

        # Pick the freshest of the three references — project_events
        # is the canonical signal but tasks.last_stage_change_at /
        # created_at are reliable fallbacks for boards where the v2
        # event log has barely started being written.
        candidates = [
            ts for ts in (event_last_change, task_last_change, task_last_created)
            if ts
        ]
        last_change_ts = max(candidates) if candidates else None

        last_dt: datetime | None = None
        if last_change_ts:
            try:
                last_dt = datetime.fromisoformat(
                    last_change_ts.replace("Z", "+00:00")
                )
            except Exception:
                last_dt = None

        if last_dt is not None:
            age_seconds = int((now_dt - last_dt).total_seconds())
        else:
            # No reference timestamp at all — treat as stale.
            age_seconds = silence_threshold + 1

        if age_seconds < silence_threshold:
            # Board moved recently — re-arm by clearing any prior stamp.
            await c.execute(
                "DELETE FROM team_config WHERE key = ?", (alert_key,)
            )
            await c.commit()
            return 0

        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = ?", (alert_key,)
        )
        row = await cur.fetchone()
        prior_alert_iso = dict(row)["value"] if row else None
        if prior_alert_iso:
            try:
                prior_dt = datetime.fromisoformat(prior_alert_iso.replace("Z", "+00:00"))
                if (now_dt - prior_dt).total_seconds() < realert_cooldown:
                    return 0
            except Exception:
                pass

        await c.execute(
            "INSERT OR REPLACE INTO team_config (key, value) VALUES (?, ?)",
            (alert_key, now_iso),
        )
        await c.commit()
    finally:
        await c.close()

    age_min = max(1, age_seconds // 60)
    await bus.publish({
        "ts": now_iso,
        "agent_id": "system",
        "type": "kanban_board_stalled",
        "project_id": project_id,
        "last_stage_change_at": last_change_ts,
        "age_seconds": age_seconds,
        "active_task_count": active_count,
        "to": "coach",
    })

    try:
        from server.agents import maybe_wake_agent
        body = (
            f"Kanban hasn't moved in {age_min} min. "
            f"Active tasks: {active_count}."
        )
        await maybe_wake_agent(
            "coach", body,
            bypass_debounce=True,
            wake_source="kanban_board_safety",
        )
    except Exception:
        logger.exception("board_safety_ring: coach wake failed")
    return 1


__all__ = [
    "start_idle_poller",
    "stop_idle_poller",
    "is_running",
    "sweep_once",
    "PLAYER_SLOTS",
    "reconciliation_sweep_once",
    "board_safety_ring_once",
]

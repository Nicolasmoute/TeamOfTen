"""Idle-Player polling (Docs/kanban-specs.md §10).

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
    """Run a single sweep. Returns the number of wake-up calls made.
    Exposed for tests so they can drive the loop deterministically
    instead of waiting for the asyncio sleep cycle."""
    woken = 0
    for slot in PLAYER_SLOTS:
        try:
            if await _maybe_wake_idle(slot):
                woken += 1
        except Exception:
            logger.exception(
                "idle_poller: per-slot wake failed (slot=%s)", slot
            )
    return woken


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
        wake_text = (
            "There may be tasks waiting for you. Call coord_my_assignments "
            "to see your full plate (active executor task, pending audits, "
            "pending ship, eligible pools)."
        )
        did_wake = await maybe_wake_agent(
            slot, wake_text, bypass_debounce=False
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

    Two paths:
      - eligible-pool task whose `assigned_at` is older than the
        grace window
      - hard-assigned (owner=slot) role row that's still uncompleted
        — this catches the case where the original assign-time wake
        was rejected (cost cap, paused) and Coach hasn't followed up
    """
    grace = _grace_seconds()
    c = await configured_conn()
    try:
        # Pool eligibility: scan task_role_assignments for rows where
        # eligible_owners contains the slot AND owner IS NULL AND
        # assigned_at is older than the grace window. JSON1 json_each
        # gives us the array unrolling.
        cur = await c.execute(
            """
            SELECT r.task_id
              FROM task_role_assignments r,
                   json_each(r.eligible_owners) je
             WHERE je.value = ?
               AND r.owner IS NULL
               AND r.completed_at IS NULL
               AND r.superseded_by IS NULL
               AND (julianday('now') - julianday(r.assigned_at)) * 86400.0 > ?
             ORDER BY r.assigned_at
             LIMIT 1
            """,
            (slot, grace),
        )
        row = await cur.fetchone()
        if row:
            return ("pool_task_available", dict(row)["task_id"])

        # Hard-assigned but not started: there's an active role row
        # owned by `slot` that hasn't completed. The first assign-time
        # wake might have missed; we re-fire here.
        cur = await c.execute(
            "SELECT task_id FROM task_role_assignments "
            "WHERE owner = ? AND completed_at IS NULL "
            "AND superseded_by IS NULL "
            "ORDER BY assigned_at LIMIT 1",
            (slot,),
        )
        row = await cur.fetchone()
        if row:
            return ("pending_role_assignment", dict(row)["task_id"])
    finally:
        await c.close()
    return None


__all__ = [
    "start_idle_poller",
    "stop_idle_poller",
    "is_running",
    "sweep_once",
    "PLAYER_SLOTS",
]

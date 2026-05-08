"""Playbook background scheduler.

Polls every `HARNESS_PLAYBOOK_SCHEDULER_TICK_SECONDS` (default 300).
On each tick:
  1. Skip if `playbook_disabled` flag set.
  2. Skip if no active project.
  3. If `playbook_bootstrap_done` unset:
     - Skip if `playbook_bootstrap_blocked` is set.
     - Otherwise call `bootstrap.run_bootstrap()` (acquires `_run_lock`).
  4. Otherwise, run daily reflection if past run-hour + new UTC date
     + activity-gate + cost-gate (gates checked inside `runner.run_daily_reflection`).

Lifecycle wrappers `start_playbook_scheduler()` / `stop_playbook_scheduler()`
mirror the Compass audit-watcher pattern (own task handle, error-tolerant
startup, asyncio cancellation on shutdown).

Spec §10.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any

from server.playbook import bootstrap, config, runner

logger = logging.getLogger("harness.playbook.scheduler")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


_task: asyncio.Task[None] | None = None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _read_team_config(key: str) -> str:
    try:
        from server.db import DB_PATH  # noqa: PLC0415

        conn = sqlite3.connect(DB_PATH, timeout=2.0)
        try:
            cur = conn.execute(
                "SELECT value FROM team_config WHERE key = ?", (key,)
            )
            row = cur.fetchone()
            return str(row[0]) if row else ""
        finally:
            conn.close()
    except Exception:
        return ""


async def _has_active_project() -> bool:
    """Resolve active project. Skip the tick if none configured."""
    try:
        from server.db import resolve_active_project  # noqa: PLC0415

        pid = await resolve_active_project()
        return bool(pid)
    except Exception:
        return False


def _should_run_daily(now: datetime) -> bool:
    """Past run-hour AND last_run_at is from a different UTC date."""
    if now.hour < config.RUN_HOUR_UTC_DEFAULT:
        return False
    last_raw = _read_team_config(config.PLAYBOOK_LAST_RUN_AT_KEY)
    if not last_raw:
        return True
    try:
        last = datetime.fromisoformat(last_raw)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last.date() != now.date()


async def _tick() -> None:
    """One scheduler iteration. Errors are caught and logged so a
    single bad tick doesn't kill the loop."""
    if _read_team_config(config.PLAYBOOK_DISABLED_KEY) == "1":
        return
    if not await _has_active_project():
        return

    bootstrap_done = _read_team_config(config.PLAYBOOK_BOOTSTRAP_DONE_KEY) == "1"

    if not bootstrap_done:
        if _read_team_config(config.PLAYBOOK_BOOTSTRAP_BLOCKED_KEY) == "1":
            # Operator must reset to clear the block (spec §G1).
            return
        async with runner._run_lock:
            try:
                await bootstrap.run_bootstrap()
            except Exception:
                logger.exception("playbook.scheduler: bootstrap raised")
        return

    # Daily run gating
    now = _now_utc()
    if not _should_run_daily(now):
        return

    async with runner._run_lock:
        try:
            await runner.run_daily_reflection(manual=False)
        except Exception:
            logger.exception("playbook.scheduler: daily reflection raised")


async def _loop() -> None:
    """Polling loop. Sleep + tick. Cancellation cleanly exits."""
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("playbook.scheduler: tick raised (continuing)")
        try:
            await asyncio.sleep(config.SCHEDULER_TICK_SECONDS)
        except asyncio.CancelledError:
            raise


async def start_playbook_scheduler() -> None:
    """Start the background scheduler task. Idempotent — re-calling
    when already running is a no-op. Called from `lifespan` startup
    in main.py.
    """
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="playbook_scheduler_loop")
    logger.info("playbook.scheduler: started")


async def stop_playbook_scheduler() -> None:
    """Cancel the background scheduler task and await its exit.
    Called from `lifespan` shutdown in main.py."""
    global _task
    if _task is None:
        return
    if not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None
    logger.info("playbook.scheduler: stopped")


__all__ = ["start_playbook_scheduler", "stop_playbook_scheduler"]

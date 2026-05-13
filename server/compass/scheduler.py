"""Compass scheduler — fires daily runs across enabled projects.

Wired into `main.py:lifespan` alongside `recurrence_scheduler_loop`
and the cloud-drive sync loops. Walks every project where Compass is
enabled and fires a run when:

  - The project has never been bootstrapped → fire `bootstrap`
    (once-only; flag set after success).
  - The project's last run was earlier than today's
    `DAILY_RUN_HOUR_UTC` mark → fire `daily`.

Sleep cadence is adaptive: when nothing is due the loop sleeps until
the next daily-run hour (capped at `MAX_IDLE_SLEEP_SECONDS` so a
mid-day project enable still wakes within the hour). When work fires
the loop falls back to `SCHEDULER_TICK_SECONDS` to drain any backlog.

Presence is enforced by the runner itself (not the scheduler) so a
manual on-demand run can still fire without presence — only daily
runs require it. The scheduler does NOT trigger Q&A sessions; those
are interactive and human-driven.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

from server.db import configured_conn

from server.compass import config, runner

logger = logging.getLogger("harness.compass.scheduler")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Cap on idle sleep — when the loop has nothing to do until tomorrow's
# daily-run hour, we still want to wake periodically to pick up newly-
# enabled projects (which become bootstrap-eligible immediately). One
# hour is short enough that a freshly-enabled project bootstraps within
# the hour and long enough to cut from 288 wakes/day (5-min poll) to
# ~25 — about 12× cheaper at rest.
MAX_IDLE_SLEEP_SECONDS = 3600


async def compass_scheduler_loop() -> None:
    """Background task. Adaptively sleeps until the next due event
    instead of polling every `SCHEDULER_TICK_SECONDS`. A 0 tick still
    disables — the loop waits forever on cancellation."""
    if config.SCHEDULER_TICK_SECONDS <= 0:
        logger.info("compass scheduler disabled (SCHEDULER_TICK_SECONDS=0)")
        # Still wait on cancellation so lifespan teardown can drain us.
        await asyncio.Event().wait()
        return

    logger.info(
        "compass scheduler running (tick=%ss, daily_hour_utc=%s, idle_cap=%ss)",
        config.SCHEDULER_TICK_SECONDS,
        config.DAILY_RUN_HOUR_UTC,
        MAX_IDLE_SLEEP_SECONDS,
    )
    while True:
        try:
            fired_work = await _scheduler_iteration()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("compass scheduler iteration failed")
            fired_work = False

        # When work fired we use the regular tick to drain backlog
        # (next due project, etc.). When nothing fired we sleep until
        # the next concrete deadline, capped so newly-enabled projects
        # still get picked up.
        if fired_work:
            sleep_for = float(config.SCHEDULER_TICK_SECONDS)
        else:
            sleep_for = _seconds_until_next_due(datetime.now(timezone.utc))
            sleep_for = min(sleep_for, float(MAX_IDLE_SLEEP_SECONDS))
            sleep_for = max(sleep_for, float(config.SCHEDULER_TICK_SECONDS))
        try:
            await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            raise


def _seconds_until_next_due(now: datetime) -> float:
    """Time until today's (or tomorrow's) DAILY_RUN_HOUR_UTC mark."""
    today_start = now.replace(
        hour=config.DAILY_RUN_HOUR_UTC, minute=0, second=0, microsecond=0,
    )
    if today_start <= now:
        today_start = today_start + timedelta(days=1)
    return max(0.0, (today_start - now).total_seconds())


async def _scheduler_iteration() -> bool:
    """One pass: list enabled projects, fire bootstrap or daily as
    appropriate. Bounded — only one project's run is launched per
    iteration (the next iteration picks up the next due project).
    Prevents the scheduler from spawning a storm if the loop slept
    long.

    Returns True iff a run actually fired (caller uses this to pick
    the next sleep duration — short tick when work landed, sleep-
    until-next-due when idle).
    """
    # Respect harness-wide pause: skip the daily/bootstrap fire so
    # Compass doesn't keep spending tokens while the team is held.
    try:
        from server.agents import is_paused  # noqa: PLC0415
        if is_paused():
            return False
    except Exception:
        pass

    enabled = await _enabled_projects()
    if not enabled:
        return False

    now = datetime.now(timezone.utc)
    today_start = now.replace(
        hour=config.DAILY_RUN_HOUR_UTC, minute=0, second=0, microsecond=0,
    )

    for project_id in enabled:
        if runner.is_running(project_id):
            continue
        last = await _read_team_config(config.last_run_key(project_id))
        bootstrapped = await _read_team_config(config.bootstrapped_key(project_id))
        if not bootstrapped:
            logger.info("compass: bootstrapping project %s", project_id)
            try:
                await runner.run(project_id, mode="bootstrap")
            except Exception:
                logger.exception("compass: bootstrap %s failed", project_id)
            return True  # one project per iteration

        if not last:
            # Bootstrapped but no last_run timestamp — fire daily.
            logger.info("compass: daily run for %s (no prior last_run)", project_id)
            try:
                await runner.run(project_id, mode="daily")
            except Exception:
                logger.exception("compass: daily %s failed", project_id)
            return True

        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except ValueError:
            last_dt = None
        if last_dt is None or last_dt < today_start <= now:
            logger.info("compass: daily run due for %s", project_id)
            try:
                await runner.run(project_id, mode="daily")
            except Exception:
                logger.exception("compass: daily %s failed", project_id)
            return True
    return False


async def _enabled_projects() -> list[str]:
    """Return ids of every project with `compass_enabled_<id>` truthy."""
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT key, value FROM team_config WHERE key LIKE 'compass_enabled_%'"
            )
            rows = [dict(r) for r in await cur.fetchall()]
        finally:
            await c.close()
    except Exception:
        logger.exception("compass scheduler: enabled-projects query failed")
        return []
    out: list[str] = []
    for row in rows:
        key = row.get("key") or ""
        val = row.get("value") or ""
        if not key.startswith("compass_enabled_"):
            continue
        if val.strip().lower() in ("1", "true", "yes"):
            out.append(key[len("compass_enabled_") :])
    return out


async def _read_team_config(key: str) -> str:
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT value FROM team_config WHERE key = ?", (key,)
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        return ""
    return (dict(row).get("value") if row else "") or ""


__all__ = ["compass_scheduler_loop"]

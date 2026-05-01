"""Compass scheduler — fires daily runs across enabled projects.

Wired into `main.py:lifespan` alongside `recurrence_scheduler_loop`
and the kDrive sync loops. Polls every `SCHEDULER_TICK_SECONDS`,
walks every project where Compass is enabled, and fires a run when:

  - The project has never been bootstrapped → fire `bootstrap`
    (once-only; flag set after success).
  - The project's last run was earlier than today's
    `DAILY_RUN_HOUR_UTC` mark → fire `daily`.

Presence is enforced by the runner itself (not the scheduler) so a
manual on-demand run can still fire without presence — only daily
runs require it. The scheduler does NOT trigger Q&A sessions; those
are interactive and human-driven.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

from server.db import configured_conn

from server.compass import config, runner

logger = logging.getLogger("harness.compass.scheduler")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


async def compass_scheduler_loop() -> None:
    """Background task. Sleeps `SCHEDULER_TICK_SECONDS` between
    iterations. A 0 value disables — the loop sleeps forever (the
    main.py lifespan still creates the task so cancellation is
    consistent)."""
    if config.SCHEDULER_TICK_SECONDS <= 0:
        logger.info("compass scheduler disabled (SCHEDULER_TICK_SECONDS=0)")
        # Still wait on cancellation so lifespan teardown can drain us.
        await asyncio.Event().wait()
        return

    logger.info(
        "compass scheduler running (tick=%ss, daily_hour_utc=%s)",
        config.SCHEDULER_TICK_SECONDS, config.DAILY_RUN_HOUR_UTC,
    )
    while True:
        try:
            await asyncio.sleep(config.SCHEDULER_TICK_SECONDS)
        except asyncio.CancelledError:
            raise
        try:
            await _scheduler_iteration()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("compass scheduler iteration failed")


async def _scheduler_iteration() -> None:
    """One pass: list enabled projects, fire bootstrap or daily as
    appropriate. Bounded — only one project's run is launched per
    iteration (the next iteration picks up the next due project).
    Prevents the scheduler from spawning a storm if the loop slept
    long."""
    enabled = await _enabled_projects()
    if not enabled:
        return

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
            return  # one project per iteration

        if not last:
            # Bootstrapped but no last_run timestamp — fire daily.
            logger.info("compass: daily run for %s (no prior last_run)", project_id)
            try:
                await runner.run(project_id, mode="daily")
            except Exception:
                logger.exception("compass: daily %s failed", project_id)
            return

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
            return


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

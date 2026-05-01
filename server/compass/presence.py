"""Human-reachable detection (spec §2.2).

Daily/bootstrap runs only fire when a human is reachable. Sources:
  - Recent rows in the `messages` table with `from_id='human'`
  - A heartbeat key in `team_config` set by the dashboard's
    `/api/compass/heartbeat` endpoint each time the human opens
    or interacts with the Compass dashboard
  - Recent commits aren't queryable cheaply, but the dashboard
    heartbeat covers the typical case (human is at the keyboard)

The signal is per-project: a human active on project Beta does not
unblock daily runs on project Alpha. This keeps the world-model
loop honestly anchored to who's working on what.

`send_reminder(project_id)` publishes a `compass_reminder` event
that the dashboard / Telegram bridge can react to.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone, timedelta

from server.db import configured_conn
from server.events import bus

from server.compass import config

logger = logging.getLogger("harness.compass.presence")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


async def human_reachable(project_id: str) -> bool:
    """Return True if the human has been active on this project in
    the last `HUMAN_PRESENCE_WINDOW_HOURS`.

    Active = either:
      - A message row with `from_id='human'` and `project_id` matching,
        sent within the window
      - A heartbeat row in team_config newer than the window
    """
    window = timedelta(hours=config.HUMAN_PRESENCE_WINDOW_HOURS)
    cutoff = _now() - window
    cutoff_iso = cutoff.isoformat()

    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT 1 FROM messages "
                "WHERE from_id = 'human' AND project_id = ? AND sent_at >= ? "
                "LIMIT 1",
                (project_id, cutoff_iso),
            )
            row = await cur.fetchone()
            if row is not None:
                return True
            cur = await c.execute(
                "SELECT value FROM team_config WHERE key = ?",
                (config.heartbeat_key(project_id),),
            )
            hb = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("compass.presence: query failed")
        return False

    if hb:
        ts_str = (dict(hb).get("value") or "").strip()
        try:
            hb_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if hb_dt >= cutoff:
                return True
        except ValueError:
            return False
    return False


async def update_heartbeat(project_id: str) -> None:
    """Record the current time as the human's heartbeat for `project_id`.
    Called from `POST /api/compass/heartbeat` and also from each
    user-driven endpoint (run trigger, Q&A submit, proposal resolve)
    so any human action implicitly counts as presence."""
    try:
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO team_config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (config.heartbeat_key(project_id), _now_iso()),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("compass.presence: heartbeat update failed")


async def send_reminder(project_id: str) -> None:
    """Publish a reminder event so the dashboard / external channels
    can prompt the human to attend to Compass. We don't touch Slack
    / email directly here — the existing Telegram bridge already
    forwards `human_attention` events; Compass uses its own
    `compass_reminder` type so reminders can be styled differently
    without polluting the human-attention pipe."""
    try:
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": "compass",
            "project_id": project_id,
            "type": "compass_reminder",
            "reason": "no human signal in the last 24h — Compass run skipped",
        })
    except Exception:
        logger.exception("compass.presence: reminder publish failed")


__all__ = ["human_reachable", "update_heartbeat", "send_reminder"]

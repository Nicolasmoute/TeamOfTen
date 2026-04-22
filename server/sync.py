"""Background sync: local SQLite → kDrive WebDAV.

v1 of this only does event-log daily rotation. Memory docs mirror
synchronously from the coord_update_memory tool (server/tools.py).
Snapshots + decisions + digests come in later M3 ticks.

Every HARNESS_KDRIVE_FLUSH_INTERVAL seconds (default 300 = 5 min):
- pull every event whose ts >= today's UTC-midnight from SQLite
- write them as JSONL to kdrive events/YYYY-MM-DD.jsonl (overwrite)

Yesterday's file stops being rewritten once UTC midnight passes —
it stays as of the last flush before midnight. Acceptable sub-minute
loss for personal use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from server.db import configured_conn
from server.kdrive import kdrive

logger = logging.getLogger("harness.sync")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


FLUSH_INTERVAL_SECONDS = int(
    os.environ.get("HARNESS_KDRIVE_FLUSH_INTERVAL", "300")
)


def _utc_midnight_of(day: datetime) -> datetime:
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


async def flush_day(date_str: str) -> int:
    """Upload all events whose ts falls on `date_str` (YYYY-MM-DD, UTC)
    to kDrive events/<date>.jsonl, overwriting any prior version.

    Returns count on success, 0 if no events (file not touched),
    -1 on upload failure.
    """
    if not kdrive.enabled:
        return 0

    # Half-open window [start, next_day_start) so exactly one day's
    # events are captured regardless of microsecond-resolution timestamps.
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, ts, agent_id, type, payload FROM events "
            "WHERE ts >= ? AND ts < ? ORDER BY id ASC",
            (start.isoformat(), end.isoformat()),
        )
        rows = await cur.fetchall()
    finally:
        await c.close()

    if not rows:
        return 0

    parts: list[str] = []
    for r in rows:
        d = dict(r)
        try:
            payload: Any = json.loads(d["payload"])
        except Exception:
            payload = {"raw": d["payload"]}
        parts.append(
            json.dumps(
                {
                    "id": d["id"],
                    "ts": d["ts"],
                    "agent_id": d["agent_id"],
                    "type": d["type"],
                    "payload": payload,
                },
                ensure_ascii=False,
            )
        )

    content = "\n".join(parts) + "\n"
    remote = f"events/{date_str}.jsonl"
    ok = await kdrive.write_text(remote, content)
    if ok:
        logger.info("flushed %d event(s) → kdrive %s", len(rows), remote)
        return len(rows)
    return -1


async def flush_today_events() -> int:
    """Flush today's events. Also re-flush yesterday for the first two
    hours after UTC midnight, so late events emitted right before the
    boundary don't fall into a file that's already been frozen."""
    if not kdrive.enabled:
        return 0

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    total = await flush_day(today_str)

    if now.hour < 2:
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        yd = await flush_day(yesterday_str)
        if yd > 0:
            total = (total if total > 0 else 0) + yd

    return total


async def flush_loop() -> None:
    """Background task: flush events every FLUSH_INTERVAL_SECONDS."""
    if not kdrive.enabled:
        logger.info(
            "sync loop idle: kdrive disabled (%s). Start loop to retry once "
            "kdrive config appears.",
            kdrive.reason,
        )
    else:
        logger.info(
            "sync loop starting: flush every %ds", FLUSH_INTERVAL_SECONDS
        )
    while True:
        try:
            if kdrive.enabled:
                await flush_today_events()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("flush cycle failed")
        try:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

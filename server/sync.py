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
from datetime import datetime, timezone
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


def _today_utc_midnight_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _today_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def flush_today_events() -> int:
    """Upload today's events to kDrive events/YYYY-MM-DD.jsonl.

    Returns count of events written (0 if no events today or if kDrive
    is disabled; -1 on upload failure).
    """
    if not kdrive.enabled:
        return 0

    start_iso = _today_utc_midnight_iso()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, ts, agent_id, type, payload FROM events "
            "WHERE ts >= ? ORDER BY id ASC",
            (start_iso,),
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
    remote = f"events/{_today_date_str()}.jsonl"
    ok = await kdrive.write_text(remote, content)
    if ok:
        logger.info("flushed %d event(s) → kdrive %s", len(rows), remote)
        return len(rows)
    return -1


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

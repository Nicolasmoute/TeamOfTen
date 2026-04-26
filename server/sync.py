"""Background sync: local SQLite → WebDAV.

v1 of this only does event-log daily rotation. Memory docs mirror
synchronously from the coord_update_memory tool (server/tools.py).
Snapshots + decisions + digests come in later M3 ticks.

Every HARNESS_WEBDAV_FLUSH_INTERVAL seconds (default 300 = 5 min):
- pull every event whose ts >= today's UTC-midnight from SQLite
- write them as JSONL to webdav events/YYYY-MM-DD.jsonl (overwrite)

Yesterday's file stops being rewritten once UTC midnight passes —
it stays as of the last flush before midnight. Acceptable sub-minute
loss for personal use.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from server.db import DB_PATH, configured_conn
from server.webdav import webdav

logger = logging.getLogger("harness.sync")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


FLUSH_INTERVAL_SECONDS = int(
    os.environ.get("HARNESS_WEBDAV_FLUSH_INTERVAL", "300")
)
SNAPSHOT_INTERVAL_SECONDS = int(
    os.environ.get("HARNESS_WEBDAV_SNAPSHOT_INTERVAL", "300")
)
# How many DB snapshots to keep on the mirror. Older ones are deleted
# after each successful upload. 144 = ~12 h of 5-min snapshots. 5 min
# cadence means a crash loses ≤ 5 min of state. Snapshots are
# single-digit KB at this scale so the bandwidth is trivial.
SNAPSHOT_RETENTION = int(
    os.environ.get("HARNESS_WEBDAV_SNAPSHOT_RETENTION", "144")
)

# How long events stay in the SQLite `events` table. Older rows are
# deleted by events_trim_loop (runs daily). the WebDAV mirror's daily JSONL
# mirrors keep the full permanent history, so trimming the hot DB
# just keeps it performant — it's not data loss. Set to 0 to disable
# trimming (events grow forever).
EVENTS_RETENTION_DAYS = int(
    os.environ.get("HARNESS_EVENTS_RETENTION_DAYS", "30")
)
# Seconds between trim passes. 24 h by default — events are small so
# more-frequent trimming has no benefit.
EVENTS_TRIM_INTERVAL_SECONDS = int(
    os.environ.get("HARNESS_EVENTS_TRIM_INTERVAL", "86400")
)

# Pasted images (see POST /api/attachments) accumulate at
# /data/attachments. At ~200 KB / paste the volume stays small for
# months, but on long-running deploys disk fills eventually. Same
# retention pattern as events: default 30 days, 0 disables.
ATTACHMENTS_RETENTION_DAYS = int(
    os.environ.get("HARNESS_ATTACHMENTS_RETENTION_DAYS", "30")
)

# Claude CLI session jsonl files under CLAUDE_CONFIG_DIR/projects/
# hold the full per-session transcript the SDK writes. They pile up
# over time — each long session is single-digit MB. 30 days is the
# default, matching events + attachments. 0 disables the sweep (keep
# forever). The compact handoff footer tells fresh-you these files
# are auto-pruned at this cadence, so keep this env in sync with the
# footer copy if you change it.
SESSION_RETENTION_DAYS = int(
    os.environ.get("HARNESS_SESSION_RETENTION_DAYS", "30")
)

# WebDAV → local uploads pull. Users drop reference docs at
# <webdav>/uploads/ via the web UI or sync client; we mirror them
# into /data/uploads (which each agent workspace symlinks as
# ./uploads) so Players can Read ./uploads/foo.pdf. Default 60s —
# it's user-driven so a minute is snappy enough.
UPLOADS_PULL_INTERVAL_SECONDS = int(
    os.environ.get("HARNESS_UPLOADS_PULL_INTERVAL", "60")
)
UPLOADS_LOCAL_DIR = Path(
    os.environ.get("HARNESS_UPLOADS_DIR", "/data/uploads")
)

# Local → WebDAV outputs push. coord_save_output mirrors synchronously
# but an agent that drops a file under /data/outputs via the Write /
# Bash tools (bypassing the coord tool) wouldn't trigger that mirror.
# This loop catches those writes: every N seconds it walks the local
# outputs dir and pushes anything not already on the mirror. Upload is
# by basename (not size) — once a file exists upstream we assume it's
# in sync; rename locally if you overwrite and want the new bytes
# pushed.
OUTPUTS_PUSH_INTERVAL_SECONDS = int(
    os.environ.get("HARNESS_OUTPUTS_PUSH_INTERVAL", "60")
)
OUTPUTS_LOCAL_DIR = Path(
    os.environ.get("HARNESS_OUTPUTS_DIR", "/data/outputs")
)


def _utc_midnight_of(day: datetime) -> datetime:
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


async def flush_day(date_str: str) -> int:
    """Upload all events whose ts falls on `date_str` (YYYY-MM-DD, UTC)
    to webdav events/<date>.jsonl, overwriting any prior version.

    Returns count on success, 0 if no events (file not touched),
    -1 on upload failure.
    """
    if not webdav.enabled:
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
    ok = await webdav.write_text(remote, content)
    if ok:
        logger.info("flushed %d event(s) → webdav %s", len(rows), remote)
        return len(rows)
    return -1


async def flush_today_events() -> int:
    """Flush today's events. Also re-flush yesterday for the first two
    hours after UTC midnight, so late events emitted right before the
    boundary don't fall into a file that's already been frozen."""
    if not webdav.enabled:
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


def _snapshot_db_sync() -> bytes:
    """Run SQLite VACUUM INTO a tempfile and return the file contents.

    VACUUM INTO is atomic and safe against concurrent readers/writers
    (SQLite grabs its own locks). Writing to a tempfile under /tmp
    (container-local, not the /data volume) avoids pressuring the
    persistent volume with transient snapshot files.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="harness-snap-", dir="/tmp")
    os.close(tmp_fd)
    try:
        # VACUUM INTO doesn't accept parameter binding; the path is a
        # tempfile we just created so it's safe to embed. Still quote-escape
        # defensively.
        safe = tmp_path.replace("'", "''")
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        try:
            conn.execute(f"VACUUM INTO '{safe}'")
        finally:
            conn.close()
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def snapshot_db() -> int:
    """Create a point-in-time DB snapshot and upload to WebDAV.

    Returns byte count on success, 0 if WebDAV disabled, -1 on failure.
    """
    if not webdav.enabled:
        return 0
    try:
        data = await asyncio.to_thread(_snapshot_db_sync)
    except Exception:
        logger.exception("VACUUM INTO failed")
        return -1

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    remote = f"snapshots/{ts}.db"
    ok = await webdav.write_bytes(remote, data)
    if ok:
        logger.info("snapshot: %d bytes → webdav %s", len(data), remote)
        await _prune_snapshots()
        return len(data)
    return -1


async def _prune_snapshots() -> None:
    """Keep at most SNAPSHOT_RETENTION snapshots on the mirror. Filenames
    sort lexicographically by ISO timestamp, so newest = greatest.

    Best-effort; failures are logged and ignored — the next cycle will
    try again. SNAPSHOT_RETENTION <= 0 disables pruning."""
    if SNAPSHOT_RETENTION <= 0:
        return
    try:
        names = await webdav.list_dir("snapshots")
    except Exception:
        logger.exception("snapshot prune: list failed")
        return
    snaps = sorted(n for n in names if n.endswith(".db"))
    excess = len(snaps) - SNAPSHOT_RETENTION
    if excess <= 0:
        return
    to_delete = snaps[:excess]
    for name in to_delete:
        ok = await webdav.remove(f"snapshots/{name}")
        if ok:
            logger.info("snapshot prune: removed %s", name)


async def snapshot_loop() -> None:
    """Background task: periodic DB snapshots to the WebDAV mirror."""
    if not webdav.enabled:
        logger.info("snapshot loop idle: webdav disabled")
    else:
        logger.info(
            "snapshot loop starting: snapshot every %ds",
            SNAPSHOT_INTERVAL_SECONDS,
        )
    while True:
        try:
            if webdav.enabled:
                await snapshot_db()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("snapshot cycle failed")
        try:
            await asyncio.sleep(SNAPSHOT_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def pull_uploads_once() -> dict[str, int]:
    """Mirror <webdav>/uploads/ → UPLOADS_LOCAL_DIR.

    - Lists WebDAV uploads/ (empty list if disabled or missing).
    - Downloads every file not already present locally (size-based
      heuristic, cheap — the WebDAV server's `ls` doesn't give us cheap
      per-file mtime, and re-downloading a 100MB PDF every minute
      would be silly).
    - Removes local files no longer on the mirror (so deleting a file on
      your phone removes it from agents' view within 60s).

    Returns {added, removed, kept}.
    """
    if not webdav.enabled:
        return {"added": 0, "removed": 0, "kept": 0}
    try:
        remote_names = await webdav.list_dir("uploads")
    except Exception:
        logger.exception("uploads pull: remote list failed")
        return {"added": 0, "removed": 0, "kept": 0}
    # Normalize: the WebDAV server may return "uploads/foo.pdf" or "foo.pdf"
    # depending on server — strip to basename.
    remote_set = {Path(n).name for n in remote_names if n}
    UPLOADS_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    local_files = {p.name: p for p in UPLOADS_LOCAL_DIR.iterdir() if p.is_file() and not p.is_symlink()}
    added = kept = removed = 0
    # Delete local files that vanished upstream.
    for name, lp in local_files.items():
        if name not in remote_set:
            try:
                lp.unlink()
                removed += 1
            except OSError:
                logger.exception("uploads pull: failed to remove %s", lp)
    # Pull new files (anything we don't already have by name). This
    # does NOT refresh an edited file — rename on the mirror (e.g. add
    # version suffix) if you update a document and want agents to see
    # the new content.
    for name in remote_set:
        if name in local_files:
            kept += 1
            continue
        # Binary download — handles pdf / docx / images alongside
        # text. We don't decode; just stream bytes to disk so agents
        # can Read/Bash the file with the correct byte content.
        try:
            data = await webdav.read_bytes(f"uploads/{name}")
        except Exception:
            logger.exception("uploads pull: download failed: %s", name)
            continue
        if data is None:
            logger.warning("uploads pull: %s not found on re-fetch", name)
            continue
        target = UPLOADS_LOCAL_DIR / name
        try:
            target.write_bytes(data)
            added += 1
        except Exception:
            logger.exception("uploads pull: local write failed: %s", target)
    if added or removed:
        logger.info(
            "uploads pull: +%d -%d (kept %d)", added, removed, kept,
        )
    return {"added": added, "removed": removed, "kept": kept}


async def uploads_pull_loop() -> None:
    """Background task: poll <webdav>/uploads/ every
    HARNESS_UPLOADS_PULL_INTERVAL seconds (default 60)."""
    if not webdav.enabled:
        logger.info("uploads pull loop idle: webdav disabled")
    else:
        logger.info(
            "uploads pull loop starting: every %ds → %s",
            UPLOADS_PULL_INTERVAL_SECONDS, UPLOADS_LOCAL_DIR,
        )
    while True:
        try:
            if webdav.enabled:
                await pull_uploads_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("uploads pull cycle failed")
        try:
            await asyncio.sleep(UPLOADS_PULL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def push_outputs_once() -> dict[str, int]:
    """Mirror /data/outputs → <webdav>/outputs/ for anything not
    already there. Catches agents that wrote via Write/Bash instead
    of going through coord_save_output (which mirrors synchronously).

    Walks the local tree (up to a reasonable depth), diffs against
    the flat list of basenames on the mirror, and uploads the missing
    ones. We compare by POSIX relative path, so nested outputs
    (reports/2026/foo.pdf) work.

    Returns {pushed, kept, skipped}.
    """
    if not webdav.enabled:
        return {"pushed": 0, "kept": 0, "skipped": 0}
    if not OUTPUTS_LOCAL_DIR.exists():
        return {"pushed": 0, "kept": 0, "skipped": 0}

    # Collect local relative paths.
    local_paths: list[Path] = []
    for p in OUTPUTS_LOCAL_DIR.rglob("*"):
        if p.is_file() and not p.is_symlink():
            local_paths.append(p)

    if not local_paths:
        return {"pushed": 0, "kept": 0, "skipped": 0}

    # Build a set of relative POSIX strings of what's already on the mirror.
    # Limitation: webdav.list_dir is flat; we'd need a recursive walk
    # to see nested files. For now list the top level + any immediate
    # sub-dirs the local side uses so we don't re-upload flat files.
    # A true recursive diff across deep trees is overkill for
    # personal-scale use — the worst case is we re-upload a 1 MB PDF
    # on every tick, which is still cheap.
    try:
        top_level = await webdav.list_dir("outputs")
    except Exception:
        logger.exception("outputs push: webdav list failed")
        return {"pushed": 0, "kept": 0, "skipped": 0}
    remote_top = {Path(n).name for n in top_level if n}

    pushed = kept = skipped = 0
    for lp in local_paths:
        rel = lp.relative_to(OUTPUTS_LOCAL_DIR).as_posix()
        leaf = Path(rel).name
        # Cheap skip for flat-rooted files we already see upstream.
        # Nested files fall through and get pushed unconditionally —
        # see limitation note above.
        if "/" not in rel and leaf in remote_top:
            kept += 1
            continue
        try:
            data = lp.read_bytes()
        except Exception:
            logger.exception("outputs push: local read failed: %s", lp)
            skipped += 1
            continue
        ok = await webdav.write_bytes(f"outputs/{rel}", data)
        if ok:
            pushed += 1
        else:
            skipped += 1
    if pushed:
        logger.info(
            "outputs push: +%d (kept %d, skipped %d)", pushed, kept, skipped,
        )
    return {"pushed": pushed, "kept": kept, "skipped": skipped}


async def outputs_push_loop() -> None:
    """Background task: push /data/outputs → mirror every
    HARNESS_OUTPUTS_PUSH_INTERVAL seconds (default 60)."""
    if not webdav.enabled:
        logger.info("outputs push loop idle: webdav disabled")
    else:
        logger.info(
            "outputs push loop starting: every %ds from %s",
            OUTPUTS_PUSH_INTERVAL_SECONDS, OUTPUTS_LOCAL_DIR,
        )
    while True:
        try:
            if webdav.enabled:
                await push_outputs_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("outputs push cycle failed")
        try:
            await asyncio.sleep(OUTPUTS_PUSH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def trim_events_once() -> int:
    """Delete rows from `events` older than EVENTS_RETENTION_DAYS.

    Returns the number of rows deleted, or 0 if trimming is disabled
    / nothing old enough existed. Safe to run concurrently with
    writes — SQLite serializes.

    The WebDAV daily JSONL mirror is the source of truth for
    permanent history; this function just keeps the hot SQLite table
    from growing unbounded.
    """
    if EVENTS_RETENTION_DAYS <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=EVENTS_RETENTION_DAYS)
    cutoff_iso = cutoff.isoformat()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "DELETE FROM events WHERE ts < ?", (cutoff_iso,)
        )
        deleted = cur.rowcount
        await c.commit()
    finally:
        await c.close()
    if deleted:
        logger.info(
            "events trim: deleted %d rows older than %s (retention=%dd)",
            deleted, cutoff_iso, EVENTS_RETENTION_DAYS,
        )
    return deleted


async def events_trim_loop() -> None:
    """Background task: prune old events daily. Runs once shortly after
    boot so fresh deploys don't accumulate before first trim, then
    every EVENTS_TRIM_INTERVAL_SECONDS thereafter."""
    if EVENTS_RETENTION_DAYS <= 0:
        logger.info(
            "events trim loop disabled (HARNESS_EVENTS_RETENTION_DAYS=0)"
        )
        return
    logger.info(
        "events trim loop starting: retention=%dd, interval=%ds",
        EVENTS_RETENTION_DAYS, EVENTS_TRIM_INTERVAL_SECONDS,
    )
    # Kick the first pass shortly after boot (not immediately, so db
    # init and workspaces settle first).
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            await trim_events_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("events trim cycle failed")
        try:
            await asyncio.sleep(EVENTS_TRIM_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def trim_attachments_once() -> int:
    """Delete files under each project's attachments dir older than
    ATTACHMENTS_RETENTION_DAYS. Returns total number deleted across
    every project.

    Per PROJECTS_SPEC.md §4 attachments live at
    `/data/projects/<slug>/attachments/` (Phase 1). The legacy
    `HARNESS_ATTACHMENTS_DIR` env override still pins the trim sweep
    to one directory when set — used by tests + pinned deploys.

    Safe to run concurrently with uploads — we stat each file and only
    unlink if its mtime is old enough. A file being written right now
    has a fresh mtime so we skip it.
    """
    if ATTACHMENTS_RETENTION_DAYS <= 0:
        return 0
    cutoff_ts = (
        datetime.now(timezone.utc) - timedelta(days=ATTACHMENTS_RETENTION_DAYS)
    ).timestamp()
    deleted = 0

    def _sweep(d: Path) -> int:
        n = 0
        if not d.is_dir():
            return 0
        for entry in d.iterdir():
            if not entry.is_file():
                continue
            try:
                if entry.stat().st_mtime < cutoff_ts:
                    entry.unlink()
                    n += 1
            except OSError:
                logger.exception("attachments trim: failed on %s", entry)
        return n

    override = os.environ.get("HARNESS_ATTACHMENTS_DIR")
    if override:
        deleted = _sweep(Path(override))
    else:
        # Walk every project's attachments dir.
        from server.paths import project_paths
        c = await configured_conn()
        try:
            cur = await c.execute("SELECT id FROM projects")
            project_ids = [dict(r)["id"] for r in await cur.fetchall()]
        finally:
            await c.close()
        for pid in project_ids:
            deleted += _sweep(project_paths(pid).attachments)
    if deleted:
        logger.info(
            "attachments trim: deleted %d file(s) older than %dd",
            deleted, ATTACHMENTS_RETENTION_DAYS,
        )
    return deleted


async def attachments_trim_loop() -> None:
    """Daily-ish sweep of /data/attachments. Same cadence / first-delay
    convention as events_trim_loop."""
    if ATTACHMENTS_RETENTION_DAYS <= 0:
        logger.info(
            "attachments trim loop disabled (HARNESS_ATTACHMENTS_RETENTION_DAYS=0)"
        )
        return
    logger.info(
        "attachments trim loop starting: retention=%dd",
        ATTACHMENTS_RETENTION_DAYS,
    )
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            await trim_attachments_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("attachments trim cycle failed")
        try:
            await asyncio.sleep(EVENTS_TRIM_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def trim_sessions_once() -> int:
    """Delete Claude CLI session jsonl files older than
    SESSION_RETENTION_DAYS under CLAUDE_CONFIG_DIR/projects/. The SDK
    writes one .jsonl per session here and never prunes them itself.
    No-op when the directory doesn't exist yet (fresh deploy pre-first-
    turn). Returns count of files deleted."""
    if SESSION_RETENTION_DAYS <= 0:
        return 0
    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if not claude_dir:
        return 0
    projects_root = Path(claude_dir) / "projects"
    if not projects_root.is_dir():
        return 0
    cutoff_ts = (
        datetime.now(timezone.utc) - timedelta(days=SESSION_RETENTION_DAYS)
    ).timestamp()
    deleted = 0
    # Recurse because sessions are sharded by encoded-cwd sub-dirs.
    for jsonl in projects_root.rglob("*.jsonl"):
        if not jsonl.is_file():
            continue
        try:
            if jsonl.stat().st_mtime < cutoff_ts:
                jsonl.unlink()
                deleted += 1
        except OSError:
            logger.exception("sessions trim: failed on %s", jsonl)
    if deleted:
        logger.info(
            "sessions trim: deleted %d jsonl file(s) older than %dd",
            deleted, SESSION_RETENTION_DAYS,
        )
    return deleted


async def sessions_trim_loop() -> None:
    """Daily sweep of Claude CLI session jsonl files. Same cadence +
    first-delay convention as the other trim loops."""
    if SESSION_RETENTION_DAYS <= 0:
        logger.info(
            "sessions trim loop disabled (HARNESS_SESSION_RETENTION_DAYS=0)"
        )
        return
    logger.info(
        "sessions trim loop starting: retention=%dd",
        SESSION_RETENTION_DAYS,
    )
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            await trim_sessions_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sessions trim cycle failed")
        try:
            await asyncio.sleep(EVENTS_TRIM_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


async def flush_loop() -> None:
    """Background task: flush events every FLUSH_INTERVAL_SECONDS."""
    if not webdav.enabled:
        logger.info(
            "sync loop idle: webdav disabled (%s). Start loop to retry once "
            "webdav config appears.",
            webdav.reason,
        )
    else:
        logger.info(
            "sync loop starting: flush every %ds", FLUSH_INTERVAL_SECONDS
        )
    while True:
        try:
            if webdav.enabled:
                await flush_today_events()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("flush cycle failed")
        try:
            await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

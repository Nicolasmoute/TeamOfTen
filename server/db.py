from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import aiosqlite

# Route logging to stdout so Zeabur's log collector picks it up even when
# startup hangs — the M2a deploy showed "no logs at all" when init_db
# silently blocked on a volume-filesystem incompatibility.
logger = logging.getLogger("harness.db")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


DB_PATH = os.environ.get("HARNESS_DB_PATH", "/var/lib/harness/harness.db")

# Schema is idempotent — safe to run every startup.
SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id                    TEXT PRIMARY KEY,
    kind                  TEXT NOT NULL CHECK (kind IN ('coach', 'player')),
    name                  TEXT,
    role                  TEXT,
    status                TEXT NOT NULL DEFAULT 'stopped'
                          CHECK (status IN ('stopped', 'idle', 'working', 'waiting', 'error')),
    current_task_id       TEXT,
    model                 TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
    workspace_path        TEXT NOT NULL,
    session_id            TEXT,
    cost_estimate_usd     REAL NOT NULL DEFAULT 0.0,
    started_at            TEXT,
    last_heartbeat        TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'claimed', 'in_progress', 'blocked', 'done', 'cancelled')),
    owner         TEXT REFERENCES agents(id),
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    claimed_at    TEXT,
    completed_at  TEXT,
    parent_id     TEXT REFERENCES tasks(id),
    priority      TEXT NOT NULL DEFAULT 'normal'
                  CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    tags          TEXT NOT NULL DEFAULT '[]',
    artifacts     TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_owner   ON tasks(owner);
CREATE INDEX IF NOT EXISTS idx_tasks_parent  ON tasks(parent_id);
"""

# Seed agents — idempotent via INSERT OR IGNORE.
SEED_AGENTS: list[tuple[str, str, str | None, str | None, str]] = [
    ("coach", "coach", "Coach", "Team captain", "/workspaces/coach"),
] + [
    (f"p{i}", "player", None, None, f"/workspaces/p{i}")
    for i in range(1, 11)
]


async def init_db() -> None:
    """Create schema + seed agents. Called once on FastAPI startup.

    Logs every step to stdout so a silent hang is immediately diagnosable.
    """
    logger.info("init_db: start (DB_PATH=%s)", DB_PATH)

    parent = Path(DB_PATH).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        st = parent.stat()
        logger.info(
            "init_db: dir ok (path=%s, uid=%s, gid=%s, mode=%o)",
            parent, st.st_uid, st.st_gid, st.st_mode & 0o777,
        )
    except Exception:
        logger.exception("init_db: mkdir failed for %s", parent)
        raise

    # Verify we can actually write a file in the directory — on some
    # networked volume backends the dir appears to exist but writes hang.
    probe = parent / ".write-probe"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
        logger.info("init_db: write probe passed")
    except Exception:
        logger.exception("init_db: directory not writable at %s", parent)
        raise

    try:
        logger.info("init_db: opening sqlite connection")
        async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
            # Use DELETE journal mode (boring, works on every filesystem).
            # WAL requires mmap + fcntl ops that some container / networked
            # volume FS backends don't support — on Zeabur volumes, WAL
            # initialization hangs with no error, which wedges startup.
            # DELETE serializes writes, which is fine at this scale.
            await db.execute("PRAGMA journal_mode = DELETE")
            await db.execute("PRAGMA foreign_keys = ON")
            logger.info("init_db: pragmas set, running schema")
            await db.executescript(SCHEMA)
            logger.info("init_db: schema ok, seeding agents")
            await db.executemany(
                "INSERT OR IGNORE INTO agents "
                "(id, kind, name, role, workspace_path) VALUES (?, ?, ?, ?, ?)",
                SEED_AGENTS,
            )
            await db.commit()
            logger.info("init_db: complete")
    except Exception:
        logger.exception("init_db: sqlite operations failed")
        raise


def open_conn() -> aiosqlite.Connection:
    """Return an uninitialized aiosqlite connection context manager."""
    return aiosqlite.connect(DB_PATH, timeout=10.0)


async def configured_conn() -> aiosqlite.Connection:
    """Open a connection with Row factory + FK enforcement."""
    c = await aiosqlite.connect(DB_PATH, timeout=10.0)
    c.row_factory = aiosqlite.Row
    await c.execute("PRAGMA foreign_keys = ON")
    return c

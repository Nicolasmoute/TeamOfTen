from __future__ import annotations

import os
from pathlib import Path

import aiosqlite

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
    tags          TEXT NOT NULL DEFAULT '[]',    -- JSON array
    artifacts     TEXT NOT NULL DEFAULT '[]'     -- JSON array
);

CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_owner   ON tasks(owner);
CREATE INDEX IF NOT EXISTS idx_tasks_parent  ON tasks(parent_id);
"""

# Seed agents — idempotent via INSERT OR IGNORE.
# Coach has fixed name; Players start with null name/role until Coach assigns.
SEED_AGENTS: list[tuple[str, str, str | None, str | None, str]] = [
    ("coach", "coach", "Coach", "Team captain", "/workspaces/coach"),
] + [
    (f"p{i}", "player", None, None, f"/workspaces/p{i}")
    for i in range(1, 11)
]


async def init_db() -> None:
    """Create schema + seed agents. Called once on FastAPI startup."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA foreign_keys = ON")
        await db.executescript(SCHEMA)
        await db.executemany(
            "INSERT OR IGNORE INTO agents (id, kind, name, role, workspace_path) "
            "VALUES (?, ?, ?, ?, ?)",
            SEED_AGENTS,
        )
        await db.commit()


def open_conn() -> aiosqlite.Connection:
    """Return an uninitialized aiosqlite connection.

    Caller uses it as an async context manager:
        async with open_conn() as db: ...
    """
    return aiosqlite.connect(DB_PATH)


async def configured_conn() -> aiosqlite.Connection:
    """Open a connection with sensible defaults: Row factory, FK enforcement."""
    c = await aiosqlite.connect(DB_PATH)
    c.row_factory = aiosqlite.Row
    await c.execute("PRAGMA foreign_keys = ON")
    return c

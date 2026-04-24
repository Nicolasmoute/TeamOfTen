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


# Default path matches Zeabur's volume convention: mount a volume with the
# UI's "Mount Directory" set to "/data", and this is where the DB lives.
# Override with HARNESS_DB_PATH env var for other deploy targets.
# Known gotcha (learned the hard way in M2a): do NOT pre-create the mount
# path in the Dockerfile. On Zeabur, bind-mounting a volume over an
# already-existing directory causes SQLite's file probe to hang silently.
DB_PATH = os.environ.get("HARNESS_DB_PATH", "/data/harness.db")

# Schema is idempotent — safe to run every startup.
SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id                    TEXT PRIMARY KEY,
    kind                  TEXT NOT NULL CHECK (kind IN ('coach', 'player')),
    name                  TEXT,
    role                  TEXT,
    brief                 TEXT,
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

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL                   -- JSON string
);

CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id, id);
CREATE INDEX IF NOT EXISTS idx_events_type  ON events(type);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id      TEXT NOT NULL,          -- 'human', 'coach', 'p1'..'p10'
    to_id        TEXT NOT NULL,          -- agent id or 'broadcast'
    subject      TEXT,
    body         TEXT NOT NULL,
    sent_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    read_at      TEXT,                    -- legacy, unused after v0.4.1
    in_reply_to  INTEGER REFERENCES messages(id),
    priority     TEXT NOT NULL DEFAULT 'normal'
                 CHECK (priority IN ('normal', 'interrupt'))
);

CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_id);
CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_id);

-- Per-recipient read tracking. Necessary for broadcasts: the first
-- recipient to drain must NOT mark the message read for everyone else.
CREATE TABLE IF NOT EXISTS message_reads (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    agent_id   TEXT NOT NULL,
    read_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (message_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_msgreads_agent ON message_reads(agent_id);

-- Shared scratchpad. Overwrite-on-update; event log is the history.
CREATE TABLE IF NOT EXISTS memory_docs (
    topic            TEXT PRIMARY KEY,
    content          TEXT NOT NULL,
    last_updated     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_updated_by  TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1
);

-- Per-turn analytics ledger. One row per SDK ResultMessage — cheap
-- indexed queries for 'how much did p3 spend this week'. Parallel to
-- the events table but narrower: just the numbers, no free text. The
-- events table still has the full turn trail for audit; this is for
-- charts.
CREATE TABLE IF NOT EXISTS turns (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id       TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    ended_at       TEXT NOT NULL,
    duration_ms    INTEGER,
    cost_usd       REAL,
    session_id     TEXT,
    num_turns      INTEGER,     -- SDK's own internal turn counter (tool roundtrips)
    stop_reason    TEXT,
    is_error       INTEGER NOT NULL DEFAULT 0,
    model          TEXT,
    plan_mode      INTEGER NOT NULL DEFAULT 0,
    effort         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_turns_agent      ON turns(agent_id, id);
CREATE INDEX IF NOT EXISTS idx_turns_ended_at   ON turns(ended_at);

-- Team-wide settings (applies to every agent). Simple key/value store
-- so we can grow without schema churn. Value is a JSON string — the
-- caller decides the shape. Current keys:
--   extra_tools  → JSON array of SDK tool names (WebSearch, WebFetch)
CREATE TABLE IF NOT EXISTS team_config (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- External MCP servers configured via the UI (alongside the existing
-- file-based HARNESS_MCP_CONFIG path — DB entries are loaded AFTER
-- the file so UI edits win on name collision).
--
-- config_json: the single-server object pulled from the user's paste
--   ({ "type": "stdio"|"http", "command": ..., "args": [...], ... }).
-- allowed_tools_json: JSON array of bare tool names (no mcp__<name>__
--   prefix). UI-managed; server can't use tools that aren't in here.
-- enabled: 0/1 — disabled entries are kept but not merged into spawns.
-- last_ok / last_error / last_tested_at: populated by the test
--   endpoint + the periodic health loop.
CREATE TABLE IF NOT EXISTS mcp_servers (
    name             TEXT PRIMARY KEY,
    config_json      TEXT NOT NULL,
    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_ok          INTEGER,
    last_error       TEXT,
    last_tested_at   TEXT
);
"""

# Seed agents — idempotent via INSERT OR IGNORE.
SEED_AGENTS: list[tuple[str, str, str | None, str | None, str]] = [
    ("coach", "coach", "Coach", "Team captain", "/workspaces/coach"),
] + [
    (f"p{i}", "player", None, None, f"/workspaces/p{i}")
    for i in range(1, 11)
]


async def crash_recover() -> dict[str, int]:
    """Reset orphaned state left behind by an unclean shutdown.

    If the server died mid-turn, the DB still says agents are
    `working` and tasks are `in_progress`, but no subprocess is
    actually running any of that. Reset:
      - agents.status ∈ {working, waiting} → idle
      - tasks.status = 'in_progress' → claimed (owner kept so the
        Player knows what they were doing when next spawned)

    Returns a dict of how many rows were touched for logging. Safe
    to call repeatedly — a no-op on a clean DB.
    """
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        cur = await db.execute(
            "UPDATE agents SET status = 'idle' "
            "WHERE status IN ('working', 'waiting')"
        )
        agents_reset = cur.rowcount
        cur = await db.execute(
            "UPDATE tasks SET status = 'claimed' WHERE status = 'in_progress'"
        )
        tasks_reset = cur.rowcount
        await db.commit()
    return {"agents_reset": agents_reset, "tasks_reset": tasks_reset}


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
            # Lightweight manual migrations for columns added after the
            # original schema shipped. Each ALTER is wrapped so an
            # already-migrated DB (column already exists) is silently
            # skipped — SQLite raises OperationalError with 'duplicate
            # column' in that case. Keep entries append-only; never
            # remove or re-order.
            for col_name, col_ddl in (
                ("brief", "ALTER TABLE agents ADD COLUMN brief TEXT"),
                # JSON array of SDK tool names the human granted this
                # slot in addition to the role baseline (e.g.
                # ["WebSearch", "WebFetch"]). NULL / empty = baseline
                # only. Merged in server.agents.run_agent at spawn.
                ("allowed_extra_tools", "ALTER TABLE agents ADD COLUMN allowed_extra_tools TEXT"),
                # Per-Player lock flag. When set, Coach cannot assign
                # tasks to or direct-message this agent, and the agent
                # skips Coach-originated broadcasts. The agent still
                # reads all shared docs and responds to human prompts.
                # INTEGER 0/1 (SQLite has no bool); default 0.
                ("locked", "ALTER TABLE agents ADD COLUMN locked INTEGER NOT NULL DEFAULT 0"),
                # /compact output. Populated by a compact turn that
                # summarizes the current session; then session_id is
                # nulled so the next spawn starts fresh with this note
                # injected into the system prompt as "prior session
                # handoff". NULL / empty = no handoff text.
                ("continuity_note", "ALTER TABLE agents ADD COLUMN continuity_note TEXT"),
                # Most recent (user prompt, assistant response) pair for
                # this agent, serialized as JSON. Kept alongside
                # continuity_note so a compact can preserve the LAST
                # exchange verbatim — CLI-/compact style — rather than
                # paraphrasing everything. Overwritten by every
                # successful non-compact turn.
                ("last_exchange_json", "ALTER TABLE agents ADD COLUMN last_exchange_json TEXT"),
            ):
                try:
                    await db.execute(col_ddl)
                    logger.info("init_db: migration applied: agents.%s", col_name)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            # Token-usage columns on the turns ledger. Populated from
            # ResultMessage.usage on every successful turn; drives the
            # auto-compact threshold (HARNESS_AUTO_COMPACT_THRESHOLD).
            # input_tokens = new uncached input; cache_read = cached
            # prefix re-sent; cache_creation = this turn's tokens being
            # written to cache; output_tokens = assistant reply. Sum of
            # all four on the latest turn for a session ≈ conversation
            # size going into the next turn.
            for col_name, col_ddl in (
                ("input_tokens", "ALTER TABLE turns ADD COLUMN input_tokens INTEGER"),
                ("output_tokens", "ALTER TABLE turns ADD COLUMN output_tokens INTEGER"),
                ("cache_read_tokens", "ALTER TABLE turns ADD COLUMN cache_read_tokens INTEGER"),
                ("cache_creation_tokens", "ALTER TABLE turns ADD COLUMN cache_creation_tokens INTEGER"),
            ):
                try:
                    await db.execute(col_ddl)
                    logger.info("init_db: migration applied: agents.%s", col_name)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
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

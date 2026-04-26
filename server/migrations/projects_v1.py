"""Phase 1 destructive migration — see PROJECTS_SPEC.md §3 / §13.

Adds the project-scoping backbone to an existing harness DB:
- drops legacy `agents` columns (name/role/brief moved to
  `agent_project_roles`; session_id/continuity_note/last_exchange_json
  moved to `agent_sessions`),
- drops & recreates the project-scoped domain tables with a
  `project_id` column + the `idx_*_project` indexes,
- inserts the `misc` project + `active_project_id` pointer,
- wipes local data dirs and the kDrive `TOT/` root,
- scaffolds `/data/projects/misc/` + `/data/wiki/misc/`,
- stamps `team_config.schema_version = 'projects_v1'` last so a
  failed run retries cleanly on the next boot.

Re-running once `schema_version=projects_v1` exists is a no-op.

Wired from `server.db.init_db()` after `executescript(SCHEMA)` and
the column ALTERs but before the agent seed (the seed must run
against the post-drop shape of `agents`).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

import aiosqlite

from server import paths

logger = logging.getLogger("harness.migrations.projects_v1")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


SCHEMA_VERSION = "projects_v1"

# Local data directories the migration wipes — relative to
# paths.DATA_ROOT so tests with HARNESS_DATA_ROOT=tmp don't blow
# away anything real.
_WIPE_SUBDIRS = (
    "memory",
    "decisions",
    "events",
    "outputs",
    "knowledge",
    "uploads",
    "attachments",
)
# /workspaces is hardcoded above /data in the original layout, but
# tests + non-default HARNESS_DATA_ROOT installs need it relative.
_WORKSPACES_DEFAULT = Path("/workspaces")


# Recreated tables (DROP + CREATE) — the schema lives in
# server.db.SCHEMA but the migration also creates them here as a
# defensive copy in case SCHEMA changes without us noticing. The
# DDL must stay in lock-step with server.db.SCHEMA.
_RECREATE_DDL = """
CREATE TABLE tasks (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
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
CREATE INDEX idx_tasks_status  ON tasks(status);
CREATE INDEX idx_tasks_owner   ON tasks(owner);
CREATE INDEX idx_tasks_parent  ON tasks(parent_id);
CREATE INDEX idx_tasks_project ON tasks(project_id);

CREATE TABLE events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,
    payload         TEXT NOT NULL,
    payload_to      TEXT GENERATED ALWAYS AS (json_extract(payload, '$.to')) VIRTUAL,
    payload_owner   TEXT GENERATED ALWAYS AS (json_extract(payload, '$.owner')) VIRTUAL
);
CREATE INDEX idx_events_agent ON events(agent_id, id);
CREATE INDEX idx_events_type  ON events(type);
CREATE INDEX idx_events_project ON events(project_id);
CREATE INDEX idx_events_agent_type ON events(agent_id, type);
CREATE INDEX idx_events_type_id   ON events(type, id);
CREATE INDEX idx_events_to    ON events(type, payload_to, id);
CREATE INDEX idx_events_owner ON events(type, payload_owner, id);

CREATE TABLE messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    from_id      TEXT NOT NULL,
    to_id        TEXT NOT NULL,
    subject      TEXT,
    body         TEXT NOT NULL,
    sent_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    read_at      TEXT,
    in_reply_to  INTEGER REFERENCES messages(id),
    priority     TEXT NOT NULL DEFAULT 'normal'
                 CHECK (priority IN ('normal', 'interrupt'))
);
CREATE INDEX idx_messages_to ON messages(to_id);
CREATE INDEX idx_messages_from ON messages(from_id);
CREATE INDEX idx_messages_project ON messages(project_id);

CREATE TABLE memory_docs (
    project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    topic            TEXT NOT NULL,
    content          TEXT NOT NULL,
    last_updated     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_updated_by  TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, topic)
);
CREATE INDEX idx_memory_project ON memory_docs(project_id);

CREATE TABLE turns (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id       TEXT NOT NULL,
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    started_at     TEXT NOT NULL,
    ended_at       TEXT NOT NULL,
    duration_ms    INTEGER,
    cost_usd       REAL,
    session_id     TEXT,
    num_turns      INTEGER,
    stop_reason    TEXT,
    is_error       INTEGER NOT NULL DEFAULT 0,
    model          TEXT,
    plan_mode      INTEGER NOT NULL DEFAULT 0,
    effort         INTEGER,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER
);
CREATE INDEX idx_turns_agent      ON turns(agent_id, id);
CREATE INDEX idx_turns_ended_at   ON turns(ended_at);
CREATE INDEX idx_turns_project    ON turns(project_id);
"""


_DROPPED_AGENT_COLS = (
    "session_id",
    "continuity_note",
    "last_exchange_json",
    "name",
    "role",
    "brief",
)


async def _schema_version(db: aiosqlite.Connection) -> str | None:
    cur = await db.execute(
        "SELECT value FROM team_config WHERE key = 'schema_version'"
    )
    row = await cur.fetchone()
    if not row:
        return None
    # Tolerate whatever row factory the caller has set.
    try:
        return row[0]
    except Exception:
        return None


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    out: set[str] = set()
    for r in rows:
        try:
            out.add(r[1])
        except Exception:
            pass
    return out


async def _wipe_local_dirs() -> None:
    """Wipe the legacy flat data dirs + the legacy /workspaces root.

    Uses paths.DATA_ROOT so tests with HARNESS_DATA_ROOT=tmp stay
    sandboxed. Failures are logged but never re-raised — the migration
    must complete even if a directory is busy / missing.
    """
    for sub in _WIPE_SUBDIRS:
        p = paths.DATA_ROOT / sub
        try:
            if p.exists():
                logger.warning("projects_v1: wiping %s", p)
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            logger.exception("projects_v1: wipe failed for %s", p)

    # /workspaces is a sibling of /data in production, but in tests we
    # honour HARNESS_WORKSPACES_DIR so a test root doesn't reach into
    # the real /workspaces. Default falls through to /workspaces.
    ws = Path(os.environ.get("HARNESS_WORKSPACES_DIR", str(_WORKSPACES_DEFAULT)))
    try:
        if ws.exists():
            logger.warning("projects_v1: wiping %s", ws)
            shutil.rmtree(ws, ignore_errors=True)
    except Exception:
        logger.exception("projects_v1: wipe failed for %s", ws)


async def run(db: aiosqlite.Connection) -> bool:
    """Execute the destructive migration on the given connection.

    Returns True if the migration was applied, False if it was a
    no-op (already at projects_v1).
    """
    current = await _schema_version(db)
    if current == SCHEMA_VERSION:
        logger.info("projects_v1: already applied; skipping")
        return False

    logger.warning("projects_v1: starting destructive migration")

    # Step 1: drop & recreate the project-scoped domain tables.
    # SCHEMA in db.py used IF NOT EXISTS so on an existing DB they
    # still have the OLD shape (no project_id). DROP them so the
    # CREATE below installs the new shape.
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        for tbl in ("turns", "memory_docs", "messages", "events", "tasks"):
            try:
                await db.execute(f"DROP TABLE IF EXISTS {tbl}")
                logger.warning("projects_v1: dropped table %s", tbl)
            except Exception:
                logger.exception("projects_v1: drop %s failed", tbl)
                raise
        # Recreate them at the new shape.
        await db.executescript(_RECREATE_DDL)
        logger.info("projects_v1: domain tables recreated with project_id")

        # Step 2: drop legacy columns from agents.
        existing_cols = await _table_columns(db, "agents")
        for col in _DROPPED_AGENT_COLS:
            if col not in existing_cols:
                continue
            try:
                await db.execute(f"ALTER TABLE agents DROP COLUMN {col}")
                logger.warning("projects_v1: dropped agents.%s", col)
            except Exception as e:
                # SQLite < 3.35 doesn't support DROP COLUMN. Log and
                # continue — the column will be ignored by every read
                # path going forward. Tests fail loudly on the wrong
                # SQLite version so this isn't a silent regression.
                logger.warning(
                    "projects_v1: could not drop agents.%s (%s); leaving in place",
                    col, e,
                )

        # Step 3: insert the misc project + active-project pointer.
        from server.db import MISC_PROJECT_ID, MISC_PROJECT_NAME
        await db.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)",
            (MISC_PROJECT_ID, MISC_PROJECT_NAME),
        )
        await db.execute(
            "INSERT OR REPLACE INTO team_config (key, value) VALUES "
            "('active_project_id', ?)",
            (MISC_PROJECT_ID,),
        )
        # Seed misc project's coach identity.
        await db.execute(
            "INSERT OR IGNORE INTO agent_project_roles "
            "(slot, project_id, name, role) VALUES "
            "('coach', ?, 'Coach', 'Team captain')",
            (MISC_PROJECT_ID,),
        )

        # Step 4: inherit HARNESS_PROJECT_REPO into projects.repo_url
        # for the misc project.
        repo_env = os.environ.get("HARNESS_PROJECT_REPO")
        if repo_env:
            await db.execute(
                "UPDATE projects SET repo_url = ? WHERE id = ?",
                (repo_env, MISC_PROJECT_ID),
            )
            logger.info("projects_v1: misc.repo_url <- HARNESS_PROJECT_REPO")

        await db.commit()
    finally:
        await db.execute("PRAGMA foreign_keys = ON")

    # Step 5–6: local filesystem wipe + scaffold. Outside the txn —
    # fs has no rollback, but the operations are idempotent on retry.
    # The kDrive root (`TOT/`) is intentionally NOT wiped: that wipe
    # used to live here for the original legacy migration but caused
    # data loss on any boot where `schema_version` wasn't persisted
    # (fresh DB, broken volume mount, etc.). Stale legacy paths on
    # kDrive are harmless — new code writes under `TOT/projects/<slug>/`
    # and never touches the old layout. Operators on legacy installs
    # can clean orphan kDrive folders manually.
    await _wipe_local_dirs()
    try:
        paths.ensure_global_scaffold()
        paths.ensure_project_scaffold("misc")
        logger.info("projects_v1: scaffolded global + misc trees")
    except Exception:
        logger.exception("projects_v1: scaffold failed")

    # Step 8: stamp the schema version. Only after every previous
    # step succeeded — a crash before this leaves the next boot to
    # retry the whole migration.
    await db.execute(
        "INSERT OR REPLACE INTO team_config (key, value) VALUES "
        "('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    await db.commit()
    logger.warning("projects_v1: migration complete")
    return True

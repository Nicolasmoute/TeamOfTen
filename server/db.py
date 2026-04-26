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
-- Projects backbone — declared first so the FK references on the
-- domain tables below resolve at create time. SQLite is lazy on FK
-- target validation but reordering keeps the dependency graph obvious.
CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    repo_url     TEXT,
    description  TEXT,
    archived     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agents (
    id                    TEXT PRIMARY KEY,
    kind                  TEXT NOT NULL CHECK (kind IN ('coach', 'player')),
    status                TEXT NOT NULL DEFAULT 'stopped'
                          CHECK (status IN ('stopped', 'idle', 'working', 'waiting', 'error')),
    current_task_id       TEXT,
    model                 TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
    workspace_path        TEXT NOT NULL,
    cost_estimate_usd     REAL NOT NULL DEFAULT 0.0,
    started_at            TEXT,
    last_heartbeat        TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
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

CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_owner   ON tasks(owner);
CREATE INDEX IF NOT EXISTS idx_tasks_parent  ON tasks(parent_id);
-- Note: idx_tasks_project deliberately NOT in SCHEMA. The
-- project_id column is added by the projects_v1 migration which
-- drops + recreates this table; on a legacy DB (pre-refactor)
-- the column doesn't exist when executescript() runs and the
-- CREATE INDEX would fail the whole script. The index is created
-- in init_db's post-migration loop instead.

-- payload_to / payload_owner: virtual generated columns over the
-- two JSON fields the pane-history fan-out filter cares about. Lets
-- us index them — the prior query used json_extract() in the WHERE
-- clause, which is unindexable and forced a full scan of the events
-- table on every pane open. Virtual columns aren't stored in the
-- row, only in the index, so disk overhead is just the index size.
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,
    payload         TEXT NOT NULL,                   -- JSON string
    payload_to      TEXT GENERATED ALWAYS AS (json_extract(payload, '$.to')) VIRTUAL,
    payload_owner   TEXT GENERATED ALWAYS AS (json_extract(payload, '$.owner')) VIRTUAL
);

CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id, id);
CREATE INDEX IF NOT EXISTS idx_events_type  ON events(type);
-- Note: idx_events_project deferred to post-migration loop (same
-- reason as idx_tasks_project — column doesn't exist on legacy DBs).
-- (agent_id, type) for fan-out queries that decompose into "events
-- where I am the actor of any type, OR events of a specific type
-- targeting me". The OR-branch with type='message_sent' / 'task_*'
-- benefits from filtering on type first, then narrowing within. With
-- only the (agent_id, id) index the planner can't use it for the
-- type-specific OR branches and falls back to a scan.
CREATE INDEX IF NOT EXISTS idx_events_agent_type ON events(agent_id, type);
-- (type, id) for type-only queries with id-ordered pagination, e.g.
-- /api/events?type=human_attention&since_id=N — the existing
-- idx_events_type covers WHERE type=? but not the ORDER BY id DESC
-- LIMIT N step, forcing a temp-table sort.
CREATE INDEX IF NOT EXISTS idx_events_type_id   ON events(type, id);
-- Indexes on payload_to / payload_owner are NOT in SCHEMA — they
-- depend on the generated columns existing, which on an existing DB
-- require the ALTER TABLE migration in init_db() to have run first.
-- We CREATE them after the ALTER step instead.

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
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
-- Note: idx_messages_project deferred to post-migration loop.

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
    project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    topic            TEXT NOT NULL,
    content          TEXT NOT NULL,
    last_updated     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_updated_by  TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, topic)
);

-- Note: idx_memory_project deferred to post-migration loop.

-- Per-turn analytics ledger. One row per SDK ResultMessage — cheap
-- indexed queries for 'how much did p3 spend this week'. Parallel to
-- the events table but narrower: just the numbers, no free text. The
-- events table still has the full turn trail for audit; this is for
-- charts.
CREATE TABLE IF NOT EXISTS turns (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id       TEXT NOT NULL,
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
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
-- Note: idx_turns_project deferred to post-migration loop.

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

-- Encrypted secrets. ciphertext is Fernet-encrypted bytes; the master
-- key lives in env (HARNESS_SECRETS_KEY) and never touches the DB. A
-- lost master key makes this table unrecoverable, which is the point —
-- a stolen DB snapshot without the key is useless.
CREATE TABLE IF NOT EXISTS secrets (
    name             TEXT PRIMARY KEY,
    ciphertext       BLOB NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Projects refactor (PROJECTS_SPEC.md §3). The `projects` table is
-- declared at the top of the schema; the per-(slot, project) tables
-- below are additive.
CREATE TABLE IF NOT EXISTS agent_sessions (
    slot                TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id          TEXT,
    last_active         TEXT,
    continuity_note     TEXT,
    last_exchange_json  TEXT,
    PRIMARY KEY (slot, project_id)
);

CREATE TABLE IF NOT EXISTS agent_project_roles (
    slot         TEXT NOT NULL,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name         TEXT,
    role         TEXT,
    brief        TEXT,
    PRIMARY KEY (slot, project_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- 'global' added in Phase 2 to track the global tree (CLAUDE.md,
    -- skills/, mcp/, wiki/INDEX.md, cross-project wiki/*.md). Existing
    -- pre-Phase-2 DBs are evolved in-place by init_db via
    -- _evolve_sync_state_check below.
    tree             TEXT NOT NULL CHECK (tree IN ('project', 'wiki', 'global')),
    path             TEXT NOT NULL,
    mtime            REAL NOT NULL,
    size_bytes       INTEGER NOT NULL,
    sha256           TEXT NOT NULL,
    last_synced_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, tree, path)
);
"""

# Seed agents — idempotent via INSERT OR IGNORE. After the projects
# refactor (§3) name/role/brief moved to agent_project_roles; the
# seed only writes id/kind/workspace_path. Identity rows for the
# misc project are seeded in init_db after the projects row exists.
SEED_AGENTS: list[tuple[str, str, str]] = [
    ("coach", "coach", "/workspaces/coach"),
] + [
    (f"p{i}", "player", f"/workspaces/p{i}")
    for i in range(1, 11)
]

# The fallback active project. Created on every fresh DB so
# resolve_active_project() never returns None and project-scoped
# inserts never violate the FK. The destructive Phase-1 migration
# also creates it (idempotent).
MISC_PROJECT_ID = "misc"
MISC_PROJECT_NAME = "Misc"


async def _evolve_sync_state_check(db: aiosqlite.Connection) -> None:
    """If the existing `sync_state.tree` CHECK constraint doesn't allow
    'global' (pre-Phase-2 DBs), recreate the table in-place with the
    expanded CHECK while preserving any rows. Idempotent."""
    cur = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sync_state'"
    )
    row = await cur.fetchone()
    if not row:
        return
    sql_stmt = ""
    try:
        sql_stmt = row[0] or ""
    except Exception:
        return
    if "'global'" in sql_stmt:
        return  # already evolved
    logger.info("evolving sync_state CHECK constraint to allow tree='global'")
    await db.executescript(
        """
        CREATE TABLE sync_state__new (
            project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            tree             TEXT NOT NULL CHECK (tree IN ('project', 'wiki', 'global')),
            path             TEXT NOT NULL,
            mtime            REAL NOT NULL,
            size_bytes       INTEGER NOT NULL,
            sha256           TEXT NOT NULL,
            last_synced_at   TEXT NOT NULL,
            PRIMARY KEY (project_id, tree, path)
        );
        INSERT INTO sync_state__new
          SELECT project_id, tree, path, mtime, size_bytes, sha256, last_synced_at
          FROM sync_state;
        DROP TABLE sync_state;
        ALTER TABLE sync_state__new RENAME TO sync_state;
        """
    )


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
                # JSON array of SDK tool names the human granted this
                # slot in addition to the role baseline. Survives the
                # Phase 1 projects refactor (still per-agent, global).
                ("allowed_extra_tools", "ALTER TABLE agents ADD COLUMN allowed_extra_tools TEXT"),
                # Per-Player lock flag. Same.
                ("locked", "ALTER TABLE agents ADD COLUMN locked INTEGER NOT NULL DEFAULT 0"),
            ):
                try:
                    await db.execute(col_ddl)
                    logger.info("init_db: migration applied: agents.%s", col_name)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            # Generated columns + indexes on the events table for
            # pane-history fan-out filtering. Old DBs created before
            # these columns existed need them ALTERed in; new DBs
            # picked them up via SCHEMA above and the ALTERs are no-ops.
            # SQLite supports VIRTUAL generated columns via ALTER TABLE
            # ADD COLUMN; the expression must be deterministic (which
            # json_extract is) and the column can't be the rowid.
            for col_name, col_ddl in (
                ("payload_to",
                 "ALTER TABLE events ADD COLUMN payload_to TEXT "
                 "GENERATED ALWAYS AS (json_extract(payload, '$.to')) VIRTUAL"),
                ("payload_owner",
                 "ALTER TABLE events ADD COLUMN payload_owner TEXT "
                 "GENERATED ALWAYS AS (json_extract(payload, '$.owner')) VIRTUAL"),
            ):
                try:
                    await db.execute(col_ddl)
                    logger.info("init_db: migration applied: events.%s", col_name)
                except Exception as e:
                    msg = str(e).lower()
                    if "duplicate column" in msg or "already exists" in msg:
                        continue
                    # ALTER TABLE GENERATED requires SQLite >= 3.31.
                    # If the build is older, log + skip — queries fall
                    # back to json_extract via main.py's defensive code.
                    if "near \"generated\"" in msg or "syntax error" in msg:
                        logger.warning(
                            "init_db: SQLite build does not support generated "
                            "columns; events.%s skipped, queries will use "
                            "json_extract fallback", col_name
                        )
                        continue
                    raise
            # Indexes over the generated columns. Run AFTER the
            # ALTER TABLE migrations above so the columns exist (on
            # old DBs) by the time we try to index them. CREATE INDEX
            # IF NOT EXISTS is a no-op on a fresh / already-indexed
            # DB. Building over a large events table can take a few
            # seconds — acceptable on startup. If the SQLite build
            # didn't support generated columns and we skipped the
            # migration, these CREATE INDEX statements will fail
            # ("no such column"); catch and skip.
            for idx_name, idx_ddl in (
                ("idx_events_to",
                 "CREATE INDEX IF NOT EXISTS idx_events_to "
                 "ON events(type, payload_to, id)"),
                ("idx_events_owner",
                 "CREATE INDEX IF NOT EXISTS idx_events_owner "
                 "ON events(type, payload_owner, id)"),
            ):
                try:
                    await db.execute(idx_ddl)
                except Exception as e:
                    if "no such column" in str(e).lower():
                        logger.warning(
                            "init_db: %s skipped (generated column missing)",
                            idx_name,
                        )
                        continue
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
            # Run the destructive Phase-1 projects migration BEFORE
            # the agent seed. The migration drops legacy columns from
            # agents (name/role/brief/session_id/continuity_note/
            # last_exchange_json) and recreates the project-scoped
            # domain tables; the seed must then run against the
            # migrated shape.
            try:
                from server.migrations.projects_v1 import run as _run_projects_v1
                await _run_projects_v1(db)
            except Exception:
                logger.exception("init_db: projects_v1 migration failed")
                raise

            # Phase 2 follow-up: extend sync_state.tree CHECK to allow
            # 'global'. Pre-Phase-2 DBs were stamped with the old CHECK
            # in projects_v1.py; recreate the table in place when the
            # constraint is missing the new value. Idempotent — a no-op
            # once the new constraint is present.
            try:
                await _evolve_sync_state_check(db)
            except Exception:
                logger.exception("init_db: sync_state evolution failed")
                raise

            # projects_v2 layout migration (PROJECTS_SPEC.md §4): wipe
            # legacy flat root dirs left over from before projects_v1,
            # move /data/skills/ -> /data/.claude/skills/, and rename
            # per-project inputs/ -> uploads/. Idempotent; only runs
            # once schema_version is stamped 'projects_v1'.
            try:
                from server.migrations.projects_v2 import run as _run_projects_v2
                await _run_projects_v2(db)
            except Exception:
                logger.exception("init_db: projects_v2 migration failed")
                raise

            # Phase 1 deploy hot-fix: the project_id indexes used to live
            # in SCHEMA, but on a legacy DB (events/tasks/messages/etc.
            # exist from before the refactor without project_id) the
            # `CREATE INDEX … ON tasks(project_id)` would fail the whole
            # executescript() before projects_v1 could drop+recreate the
            # tables. Now we create them here, AFTER projects_v1 has
            # installed the new shape. Each is wrapped in try/except so
            # an oddball state (column still missing for some reason)
            # logs + skips instead of crashing boot.
            for idx_name, idx_ddl in (
                ("idx_tasks_project",
                 "CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)"),
                ("idx_events_project",
                 "CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id)"),
                ("idx_messages_project",
                 "CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project_id)"),
                ("idx_memory_project",
                 "CREATE INDEX IF NOT EXISTS idx_memory_project ON memory_docs(project_id)"),
                ("idx_turns_project",
                 "CREATE INDEX IF NOT EXISTS idx_turns_project ON turns(project_id)"),
            ):
                try:
                    await db.execute(idx_ddl)
                except Exception as e:
                    msg = str(e).lower()
                    if "no such column" in msg or "no such table" in msg:
                        logger.warning(
                            "init_db: %s skipped (%s) — projects_v1 may not "
                            "have run; investigate", idx_name, e,
                        )
                        continue
                    raise

            logger.info("init_db: schema ok, ensuring misc project")
            # Ensure the fallback project + active-project pointer
            # exist on every fresh boot (idempotent — the migration
            # also creates these on existing DBs).
            await db.execute(
                "INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)",
                (MISC_PROJECT_ID, MISC_PROJECT_NAME),
            )
            await db.execute(
                "INSERT OR IGNORE INTO team_config (key, value) VALUES "
                "('active_project_id', ?)",
                (MISC_PROJECT_ID,),
            )

            logger.info("init_db: seeding agents")
            await db.executemany(
                "INSERT OR IGNORE INTO agents "
                "(id, kind, workspace_path) VALUES (?, ?, ?)",
                SEED_AGENTS,
            )

            # Seed identity for the misc project so Coach has a name
            # on first spawn. Players start nameless (lacrosse
            # autonamer fills them in on first spawn).
            await db.execute(
                "INSERT OR IGNORE INTO agent_project_roles "
                "(slot, project_id, name, role) VALUES "
                "('coach', ?, 'Coach', 'Team captain')",
                (MISC_PROJECT_ID,),
            )

            await db.commit()

            # Phase 7 (PROJECTS_SPEC.md §8): write per-project CLAUDE.md
            # stub for misc on first boot. First-write-only — preserves
            # any user / Coach edits across restarts. Best-effort: a
            # disk failure here doesn't crash init_db.
            try:
                from server.paths import write_project_claude_md_stub
                write_project_claude_md_stub(
                    MISC_PROJECT_ID, MISC_PROJECT_NAME
                )
            except Exception:
                logger.exception(
                    "init_db: write_project_claude_md_stub failed for misc"
                )
            logger.info("init_db: complete")
    except Exception:
        logger.exception("init_db: sqlite operations failed")
        raise


# Phase 3 TOCTOU mitigation (PROJECTS_SPEC.md §13 Phase 3 follow-up).
# The activate handler in server.projects_api pins the new project via
# pin_active_project() during the swap so any tool call / event publish
# that begins mid-switch sees a coherent view. Outside the pinned
# context resolve_active_project reads team_config as before.
import contextvars as _ctx

_pinned_project: _ctx.ContextVar[str | None] = _ctx.ContextVar(
    "harness_pinned_project", default=None
)


class pin_active_project:
    """Context manager: while active, resolve_active_project() returns
    the pinned slug regardless of team_config. Stack-safe via
    contextvars — nested pins restore the outer one on exit."""

    def __init__(self, project_id: str) -> None:
        self._project_id = project_id
        self._token: _ctx.Token[str | None] | None = None

    def __enter__(self) -> str:
        self._token = _pinned_project.set(self._project_id)
        return self._project_id

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._token is not None:
            _pinned_project.reset(self._token)
            self._token = None


async def resolve_active_project(db: aiosqlite.Connection | None = None) -> str:
    """Return the active project_id.

    Order of precedence:
      1. The slug pinned via `pin_active_project(...)` if any.
      2. `team_config.active_project_id`.
      3. Fallback to the `misc` project.

    The pin path is the TOCTOU mitigation — the activate handler holds
    a pin while it swaps the team_config row + reloads context, so
    coord_* tools and bus.publish observe a single coherent project
    across the whole switch.
    """
    pinned = _pinned_project.get()
    if pinned:
        return pinned
    own = False
    if db is None:
        db = await configured_conn()
        own = True
    try:
        cur = await db.execute(
            "SELECT value FROM team_config WHERE key = 'active_project_id'"
        )
        row = await cur.fetchone()
    finally:
        if own:
            await db.close()
    if not row:
        return MISC_PROJECT_ID
    try:
        v = row[0]
    except Exception:
        v = None
    return v or MISC_PROJECT_ID


async def set_active_project(project_id: str) -> None:
    """Update team_config.active_project_id. Caller is responsible for
    holding any concurrency lock (the activate handler in
    server.projects_api uses an asyncio.Lock)."""
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT OR REPLACE INTO team_config (key, value) "
            "VALUES ('active_project_id', ?)",
            (project_id,),
        )
        await c.commit()
    finally:
        await c.close()


def open_conn() -> aiosqlite.Connection:
    """Return an uninitialized aiosqlite connection context manager."""
    return aiosqlite.connect(DB_PATH, timeout=10.0)


async def configured_conn() -> aiosqlite.Connection:
    """Open a connection with Row factory + FK enforcement."""
    c = await aiosqlite.connect(DB_PATH, timeout=10.0)
    c.row_factory = aiosqlite.Row
    await c.execute("PRAGMA foreign_keys = ON")
    return c

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
    last_heartbeat        TEXT,
    allowed_extra_tools   TEXT,
    locked                INTEGER NOT NULL DEFAULT 0,
    -- Slot-level runtime preference. Nullable so role defaults can
    -- apply (resolution: agents.runtime_override → team_config role
    -- default → 'claude'). NOT NULL with a default would silently
    -- ignore role defaults. See Docs/CODEX_RUNTIME_SPEC.md §B.1.
    runtime_override      TEXT
                          CHECK (runtime_override IS NULL
                                 OR runtime_override IN ('claude','codex'))
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
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);

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
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id);
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
CREATE INDEX IF NOT EXISTS idx_events_type_id ON events(type, id);
-- Indexes over the virtual generated columns for pane-history
-- fan-out filtering ("events targeting me by type", "events I own
-- by type"). They sit alongside payload_to / payload_owner above.
CREATE INDEX IF NOT EXISTS idx_events_to    ON events(type, payload_to, id);
CREATE INDEX IF NOT EXISTS idx_events_owner ON events(type, payload_owner, id);

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
CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project_id);

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

CREATE INDEX IF NOT EXISTS idx_memory_project ON memory_docs(project_id);

-- Per-turn analytics ledger. One row per SDK ResultMessage — cheap
-- indexed queries for 'how much did p3 spend this week'. Parallel to
-- the events table but narrower: just the numbers, no free text. The
-- events table still has the full turn trail for audit; this is for
-- charts.
-- Per-turn analytics ledger. Token columns track billing usage from
-- ResultMessage.usage on every successful turn. Context pressure is
-- estimated separately from the latest per-assistant usage row in
-- Claude Code's session jsonl because ResultMessage.usage aggregates
-- every tool round in the turn.
CREATE TABLE IF NOT EXISTS turns (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id              TEXT NOT NULL,
    project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    started_at            TEXT NOT NULL,
    ended_at              TEXT NOT NULL,
    duration_ms           INTEGER,
    cost_usd              REAL,
    session_id            TEXT,
    num_turns             INTEGER,     -- SDK's own internal turn counter (tool roundtrips)
    stop_reason           TEXT,
    is_error              INTEGER NOT NULL DEFAULT 0,
    model                 TEXT,
    plan_mode             INTEGER NOT NULL DEFAULT 0,
    effort                INTEGER,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    -- Which runtime executed this turn. No CHECK constraint so future
    -- runtimes don't require a schema migration to record turns.
    runtime               TEXT NOT NULL DEFAULT 'claude',
    -- 'token_priced' (cost_usd populated from a pricing table) or
    -- 'plan_included' (ChatGPT-auth Codex; cost_usd = 0, tokens
    -- populated for visibility). NULL on legacy rows. See
    -- Docs/CODEX_RUNTIME_SPEC.md §G.
    cost_basis            TEXT
);

CREATE INDEX IF NOT EXISTS idx_turns_agent    ON turns(agent_id, id);
CREATE INDEX IF NOT EXISTS idx_turns_ended_at ON turns(ended_at);
CREATE INDEX IF NOT EXISTS idx_turns_project  ON turns(project_id);

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
    -- Codex thread id for this (slot, project). Separate from
    -- session_id (Claude) because each runtime has its own continuation
    -- state — a single field would force tagging or clear-on-runtime-
    -- change and break the symmetric switch-back case. See
    -- Docs/CODEX_RUNTIME_SPEC.md §B.1.
    codex_thread_id     TEXT,
    PRIMARY KEY (slot, project_id)
);

CREATE TABLE IF NOT EXISTS agent_project_roles (
    slot           TEXT NOT NULL,
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name           TEXT,
    role           TEXT,
    brief          TEXT,
    model_override TEXT,
    PRIMARY KEY (slot, project_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- 'project': per-project files under /data/projects/<slug>/
    -- 'wiki':    cross-project wiki entries under /data/wiki/
    -- 'global':  shared root (CLAUDE.md, skills/, mcp/, wiki/INDEX.md)
    tree             TEXT NOT NULL CHECK (tree IN ('project', 'wiki', 'global')),
    path             TEXT NOT NULL,
    mtime            REAL NOT NULL,
    size_bytes       INTEGER NOT NULL,
    sha256           TEXT NOT NULL,
    last_synced_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, tree, path)
);

-- File-write proposals — Coach proposes changes to harness-managed
-- files, the human approves/denies, the harness applies the approved
-- write server-side. Players cannot propose; the
-- `coord_propose_file_write` MCP tool is Coach-only. The PreToolUse
-- file-guard hook denies any direct agent Write/Edit/Bash on the
-- protected paths regardless of role, so this table is the only path
-- through which they ever change. Scopes:
--   'truth'              — relative `path` under /data/projects/<slug>/truth/
--   'project_claude_md'  — `path` must be 'CLAUDE.md'; target is
--                          /data/projects/<slug>/CLAUDE.md.
CREATE TABLE IF NOT EXISTS file_write_proposals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    proposer_id       TEXT NOT NULL,                 -- 'coach' (enforced at tool layer)
    scope             TEXT NOT NULL DEFAULT 'truth', -- see header for valid values; validated at tool/resolver layer
    -- scope-relative path (truth: under truth/; project_claude_md: 'CLAUDE.md')
    path              TEXT NOT NULL,
    proposed_content  TEXT NOT NULL,                 -- full new file body
    summary           TEXT NOT NULL,                 -- one-line "why" the user reads
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'approved', 'denied', 'cancelled', 'superseded')),
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at       TEXT,
    resolved_by       TEXT,                          -- 'human' (only legal value today)
    resolved_note     TEXT
);

CREATE INDEX IF NOT EXISTS idx_file_write_proposals_project_status
    ON file_write_proposals(project_id, status);

-- Coach recurrences (recurrence-specs.md §10). Three flavors share one
-- table: tick (singleton per project, harness-composed prompt), repeat
-- (many per project, fixed-minute cadence + user prompt), cron (many
-- per project, friendly DSL + TZ + user prompt). `cadence` holds
-- minutes-as-string for tick/repeat and the DSL string for cron.
-- `next_fire_at` is recomputed after each fire; the scheduler reads
-- it as its only due-row signal.
CREATE TABLE IF NOT EXISTS coach_recurrence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('tick', 'repeat', 'cron')),
    cadence       TEXT NOT NULL,
    tz            TEXT,
    prompt        TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    next_fire_at  TEXT,
    last_fired_at TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by    TEXT
);

CREATE INDEX IF NOT EXISTS idx_recurrence_project
    ON coach_recurrence(project_id, enabled);
-- One tick per project — enforced via partial unique index. Repeat /
-- cron rows are unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS idx_recurrence_one_tick
    ON coach_recurrence(project_id) WHERE kind = 'tick';
"""

# Seed agents — idempotent via INSERT OR IGNORE. Per-(slot, project)
# identity rows (name/role/brief) live in agent_project_roles; the
# seed only writes id/kind/workspace_path. The misc-project Coach
# identity is seeded in init_db after the projects row exists.
SEED_AGENTS: list[tuple[str, str, str]] = [
    ("coach", "coach", "/workspaces/coach"),
] + [
    (f"p{i}", "player", f"/workspaces/p{i}")
    for i in range(1, 11)
]

# The fallback active project. Created on every fresh DB so
# resolve_active_project() never returns None and project-scoped
# inserts never violate the FK.
MISC_PROJECT_ID = "misc"
MISC_PROJECT_NAME = "Misc"


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


async def _ensure_columns(
    db: aiosqlite.Connection,
    table: str,
    cols: list[tuple[str, str]],
) -> None:
    """Add missing columns to an existing table.

    `cols` is a list of `(column_name, full_ddl_fragment)` pairs where
    `full_ddl_fragment` is everything after `ADD COLUMN` (e.g.
    `runtime TEXT NOT NULL DEFAULT 'claude'`).

    SQLite's `ALTER TABLE … ADD COLUMN … NOT NULL DEFAULT …` populates
    existing rows with the default automatically — no UPDATE needed.
    CHECK constraints can't be added by ALTER TABLE without a full
    table rebuild, so validate those at the API layer instead.
    """
    cur = await db.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cur.fetchall()}
    for name, ddl in cols:
        if name not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


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

            # Pre-schema rename: truth_proposals → file_write_proposals.
            # Has to run BEFORE executescript(SCHEMA) — otherwise the
            # CREATE TABLE IF NOT EXISTS file_write_proposals would
            # leave us with both tables on an upgraded DB. Only renames
            # when the old table exists and the new one does not, so
            # it's idempotent and a no-op on fresh installs.
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('truth_proposals', 'file_write_proposals')"
            )
            existing_proposal_tables = {row[0] for row in await cur.fetchall()}
            if (
                "truth_proposals" in existing_proposal_tables
                and "file_write_proposals" not in existing_proposal_tables
            ):
                await db.execute(
                    "ALTER TABLE truth_proposals RENAME TO file_write_proposals"
                )
                # The old index follows the renamed table but keeps its
                # own old name; drop it so the new SCHEMA's CREATE INDEX
                # IF NOT EXISTS lands cleanly under the new name.
                await db.execute(
                    "DROP INDEX IF EXISTS idx_truth_proposals_project_status"
                )

            await db.executescript(SCHEMA)

            # Inline migration runner — CREATE TABLE IF NOT EXISTS
            # only creates missing tables, it doesn't add columns to
            # existing ones. ALTER TABLE … ADD COLUMN populates new
            # columns with the DEFAULT for existing rows; CHECK
            # constraints can't be added retroactively without a table
            # rebuild, so per-column CHECKs are validated at the API
            # layer instead. Idempotent on re-run.
            await _ensure_columns(
                db,
                "agents",
                [("runtime_override", "runtime_override TEXT")],
            )
            await _ensure_columns(
                db,
                "agent_sessions",
                [("codex_thread_id", "codex_thread_id TEXT")],
            )
            # Coach-set per-(slot, project) model override. NULL = no
            # override (fall through to per-pane / role-default / SDK).
            # Validated at SET time and re-validated at SPAWN time
            # against the player's current runtime, so a stale value
            # (set when the player was on Claude, then flipped to
            # Codex) silently no-ops instead of breaking the spawn.
            await _ensure_columns(
                db,
                "agent_project_roles",
                [("model_override", "model_override TEXT")],
            )
            await _ensure_columns(
                db,
                "turns",
                [
                    ("runtime", "runtime TEXT NOT NULL DEFAULT 'claude'"),
                    ("cost_basis", "cost_basis TEXT"),
                ],
            )
            # Existing rows from the old truth_proposals table get
            # scope='truth' automatically via the DEFAULT, so the rename
            # path keeps existing pending proposals queryable as truth
            # scope without a manual UPDATE.
            await _ensure_columns(
                db,
                "file_write_proposals",
                [("scope", "scope TEXT NOT NULL DEFAULT 'truth'")],
            )

            logger.info("init_db: schema ok, ensuring misc project")
            # Ensure the fallback project + active-project pointer
            # exist on every fresh boot. INSERT OR IGNORE — never
            # overwrites a user-chosen active project.
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

            # Write the per-project CLAUDE.md stub for misc on first
            # boot. First-write-only — preserves any user / Coach edits
            # across restarts. Best-effort: a disk failure here doesn't
            # crash init_db.
            try:
                from server.paths import write_project_claude_md_stub
                write_project_claude_md_stub(
                    MISC_PROJECT_ID, MISC_PROJECT_NAME
                )
            except Exception:
                logger.exception(
                    "init_db: write_project_claude_md_stub failed for misc"
                )
            await _seed_recurrence_from_env(db)

            logger.info("init_db: complete")
    except Exception:
        logger.exception("init_db: sqlite operations failed")
        raise


async def _seed_recurrence_from_env(db: aiosqlite.Connection) -> None:
    """One-shot migration: if HARNESS_COACH_TICK_INTERVAL is non-zero,
    seed a tick row for every existing project (so the new scheduler
    fires on the same cadence as the legacy in-memory loop did).

    Idempotent via the `recurrence_v1_seeded` team_config flag —
    later boots skip this entirely. Per `recurrence-specs.md` §14, the
    env var is honored on first migration only and is documented as
    deprecated thereafter.

    Cadence is converted seconds→minutes, rounded up, min 1, capped at
    a sane upper bound. The legacy var was seconds; the new schema is
    minutes (`recurrence-specs.md` §1: "durations are in minutes
    everywhere").
    """
    cur = await db.execute(
        "SELECT value FROM team_config WHERE key = 'recurrence_v1_seeded'"
    )
    if await cur.fetchone():
        return
    raw = os.environ.get("HARNESS_COACH_TICK_INTERVAL", "0").strip()
    try:
        seconds = max(0, int(raw))
    except ValueError:
        seconds = 0
    if seconds > 0:
        # Round up so a sub-minute env value (e.g. 30s) yields a 1-min
        # tick. Floor would silently drop the recurrence.
        minutes = max(1, (seconds + 59) // 60)
        cur = await db.execute("SELECT id FROM projects")
        rows = await cur.fetchall()
        for (project_id,) in rows:
            await db.execute(
                "INSERT OR IGNORE INTO coach_recurrence "
                "(project_id, kind, cadence, prompt, enabled, "
                "next_fire_at, created_by) "
                "VALUES (?, 'tick', ?, NULL, 1, "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'env_migration')",
                (project_id, str(minutes)),
            )
        logger.info(
            "init_db: seeded recurrence tick rows from "
            "HARNESS_COACH_TICK_INTERVAL=%s (minutes=%d, projects=%d)",
            raw, minutes, len(rows),
        )
    await db.execute(
        "INSERT OR REPLACE INTO team_config (key, value) "
        "VALUES ('recurrence_v1_seeded', '1')"
    )
    # Spec §10: migration recurrence_v1 stamps team_config.schema_version.
    # We don't otherwise track schema versions in this codebase (CREATE
    # TABLE IF NOT EXISTS handles the rest), but stamping here gives a
    # cheap signal that the v1 migration ran and is forward-compatible
    # with a future versioned-migration runner.
    await db.execute(
        "INSERT OR REPLACE INTO team_config (key, value) "
        "VALUES ('schema_version', 'recurrence_v1')"
    )
    await db.commit()


# TOCTOU mitigation. The activate handler in server.projects_api
# pins the new project via pin_active_project() during the swap so
# any tool call / event publish that begins mid-switch sees a coherent
# view. Outside the pinned context resolve_active_project reads
# team_config as before.
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

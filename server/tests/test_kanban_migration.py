"""Tests for the kanban-lifecycle migration.

The migration rebuilds the legacy `tasks` table (status enum
open/claimed/in_progress/blocked/done/cancelled, no spec/audit columns)
into the kanban shape (plan/execute/audit_*/ship/archive + lifecycle
columns). Idempotent via team_config['tasks_kanban_v1_migrated'].

Each test exercises the rebuild path by manually creating a legacy DB
file (skipping init_db's CREATE TABLE IF NOT EXISTS step), then calling
init_db() to trigger the migration.
"""

from __future__ import annotations

import aiosqlite

import server.db as dbmod
from server.db import configured_conn, init_db


_LEGACY_TASKS_SCHEMA = """
CREATE TABLE projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    repo_url     TEXT,
    description  TEXT,
    archived     INTEGER NOT NULL DEFAULT 0
);
INSERT INTO projects (id, name) VALUES ('misc', 'misc');

-- Production upgrades always have agents pre-seeded from previous
-- boots; mirror that so the FK check on tasks.owner during the
-- migration's foreign_key_check finds them.
CREATE TABLE agents (
    id                    TEXT PRIMARY KEY,
    kind                  TEXT NOT NULL CHECK (kind IN ('coach', 'player')),
    status                TEXT NOT NULL DEFAULT 'stopped',
    workspace_path        TEXT NOT NULL DEFAULT ''
);
INSERT INTO agents (id, kind, workspace_path) VALUES
    ('coach', 'coach', '/workspaces/coach'),
    ('p3', 'player', '/workspaces/p3'),
    ('p4', 'player', '/workspaces/p4'),
    ('p5', 'player', '/workspaces/p5'),
    ('p6', 'player', '/workspaces/p6'),
    ('p7', 'player', '/workspaces/p7');

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
    parent_id     TEXT,
    priority      TEXT NOT NULL DEFAULT 'normal'
                  CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    tags          TEXT NOT NULL DEFAULT '[]',
    artifacts     TEXT NOT NULL DEFAULT '[]'
);
"""


async def _seed_legacy_db(path: str) -> None:
    async with aiosqlite.connect(path) as db:
        await db.executescript(_LEGACY_TASKS_SCHEMA)
        # Insert one row per legacy status value so we can verify the
        # mapping table for every case in a single migration run.
        rows = [
            ("t-open", "open task", "open", None, None, None),
            (
                "t-claimed",
                "assigned, not started",
                "claimed",
                "p3",
                "2026-04-01T10:00:00Z",
                None,
            ),
            (
                "t-progress",
                "actively working",
                "in_progress",
                "p4",
                "2026-04-01T11:00:00Z",
                None,
            ),
            (
                "t-blocked",
                "stuck on dep",
                "blocked",
                "p5",
                "2026-04-01T09:00:00Z",
                None,
            ),
            (
                "t-done",
                "delivered",
                "done",
                "p6",
                "2026-04-01T08:00:00Z",
                "2026-04-01T12:00:00Z",
            ),
            (
                "t-cancelled",
                "stop digging",
                "cancelled",
                "p7",
                "2026-04-01T07:00:00Z",
                "2026-04-01T07:30:00Z",
            ),
        ]
        for tid, title, status, owner, claimed_at, completed_at in rows:
            await db.execute(
                "INSERT INTO tasks (id, project_id, title, status, owner, "
                "created_by, claimed_at, completed_at) "
                "VALUES (?, 'misc', ?, ?, ?, 'coach', ?, ?)",
                (tid, title, status, owner, claimed_at, completed_at),
            )
        await db.commit()


async def test_migration_maps_every_legacy_status(fresh_db: str) -> None:
    """Every legacy status value migrates to the right kanban stage +
    derived columns are populated correctly."""
    import json as _json

    await _seed_legacy_db(dbmod.DB_PATH)
    # init_db detects the legacy schema and runs both rebuilds (v0.1→v0.2
    # and v0.2→v0.3).
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, status, owner, started_at, archived_at, "
            "cancelled_at, blocked, trajectory, last_stage_change_at "
            "FROM tasks ORDER BY id"
        )
        rows = {dict(r)["id"]: dict(r) for r in await cur.fetchall()}
    finally:
        await c.close()

    # open → plan; no derived state.
    assert rows["t-open"]["status"] == "plan"
    assert rows["t-open"]["started_at"] is None
    assert rows["t-open"]["archived_at"] is None
    assert rows["t-open"]["blocked"] == 0

    # claimed → execute; started_at NULL ("assigned, not started").
    assert rows["t-claimed"]["status"] == "execute"
    assert rows["t-claimed"]["owner"] == "p3"
    assert rows["t-claimed"]["started_at"] is None

    # in_progress → execute; started_at backfilled from claimed_at.
    assert rows["t-progress"]["status"] == "execute"
    assert rows["t-progress"]["owner"] == "p4"
    assert rows["t-progress"]["started_at"] == "2026-04-01T11:00:00Z"

    # blocked → execute + blocked=1; started_at carried from claimed_at
    # (best-effort: the agent had picked it up before getting stuck).
    assert rows["t-blocked"]["status"] == "execute"
    assert rows["t-blocked"]["blocked"] == 1
    assert rows["t-blocked"]["started_at"] == "2026-04-01T09:00:00Z"

    # done → archive; archived_at = completed_at; cancelled_at NULL.
    assert rows["t-done"]["status"] == "archive"
    assert rows["t-done"]["archived_at"] == "2026-04-01T12:00:00Z"
    assert rows["t-done"]["cancelled_at"] is None

    # cancelled → archive; cancelled_at = archived_at = completed_at.
    assert rows["t-cancelled"]["status"] == "archive"
    assert rows["t-cancelled"]["cancelled_at"] == "2026-04-01T07:30:00Z"
    assert rows["t-cancelled"]["archived_at"] == "2026-04-01T07:30:00Z"

    # Every row gets a non-null trajectory. The v0.1→v0.2 step seeds
    # complexity='standard' + required_reviews=['formal','semantic'] +
    # ship_required=1 for legacy rows; the v0.2→v0.3 step then derives
    # the trajectory: no spec_path so no `plan` stage, then execute,
    # audit_syntax, audit_semantics, ship.
    for row in rows.values():
        traj = _json.loads(row["trajectory"])
        stages = [s["stage"] for s in traj]
        assert stages == ["execute", "audit_syntax", "audit_semantics", "ship"]


async def test_v3_marks_migrated(fresh_db: str) -> None:
    """Fresh installs and post-migration boots stamp the v3 marker."""
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT value FROM team_config "
            "WHERE key = 'tasks_kanban_v3_migrated'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is not None
    assert row[0] == "1"


async def test_v3_drops_legacy_columns(fresh_db: str) -> None:
    """Post-migration the dropped columns must not exist."""
    await _seed_legacy_db(dbmod.DB_PATH)
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute("PRAGMA table_info(tasks)")
        cols = {dict(r)["name"] for r in await cur.fetchall()}
    finally:
        await c.close()
    # v0.3 schema: trajectory replaces these
    assert "complexity" not in cols
    assert "required_reviews" not in cols
    assert "ship_required" not in cols
    # New v0.3 columns
    assert "trajectory" in cols
    assert "last_stage_change_at" in cols
    assert "stale_alert_at" in cols


async def test_migration_idempotent(fresh_db: str) -> None:
    """Running init_db twice doesn't break — the marker
    team_config['tasks_kanban_v1_migrated'] short-circuits the rebuild
    on subsequent boots."""
    await _seed_legacy_db(dbmod.DB_PATH)
    await init_db()
    # Capture the post-migration row count for comparison.
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT COUNT(*) FROM tasks")
        first = (await cur.fetchone())[0]
        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = 'tasks_kanban_v1_migrated'"
        )
        marker_first = (await cur.fetchone())[0]
    finally:
        await c.close()

    # Second init_db call should be a no-op for tasks.
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT COUNT(*) FROM tasks")
        second = (await cur.fetchone())[0]
    finally:
        await c.close()

    assert first == 6  # 6 legacy rows seeded
    assert second == first
    assert marker_first == "1"


async def test_fresh_db_marks_migrated(fresh_db: str) -> None:
    """A fresh install (no legacy schema) marks the migration as done
    so subsequent boots don't re-check on an unrelated DB."""
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = 'tasks_kanban_v1_migrated'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is not None
    assert row[0] == "1"


_PARTIAL_V2_TASKS_SCHEMA = """
CREATE TABLE projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    repo_url     TEXT,
    description  TEXT,
    archived     INTEGER NOT NULL DEFAULT 0
);
INSERT INTO projects (id, name) VALUES ('misc', 'misc');

CREATE TABLE agents (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('coach', 'player')),
    status          TEXT NOT NULL DEFAULT 'stopped',
    workspace_path  TEXT NOT NULL DEFAULT ''
);
INSERT INTO agents (id, kind, workspace_path) VALUES
    ('coach', 'coach', '/workspaces/coach'),
    ('p1', 'player', '/workspaces/p1');

CREATE TABLE team_config (
    key   TEXT PRIMARY KEY,
    value TEXT
);
INSERT INTO team_config (key, value) VALUES ('tasks_kanban_v1_migrated', '1');

-- Mirrors the kanban v0.2 schema as it shipped in commit 2130c48:
-- `complexity` was added but `required_reviews` / `ship_required`
-- were not. A later commit retroactively added them to the v0.2
-- rebuild, but the v1_migrated marker short-circuits the rebuild
-- so they never landed on DBs upgraded between those two commits.
CREATE TABLE tasks (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'plan'
                  CHECK (status IN ('plan', 'execute', 'audit_syntax', 'audit_semantics', 'ship', 'archive')),
    owner         TEXT REFERENCES agents(id),
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    claimed_at    TEXT,
    started_at    TEXT,
    completed_at  TEXT,
    archived_at   TEXT,
    cancelled_at  TEXT,
    parent_id     TEXT,
    priority      TEXT NOT NULL DEFAULT 'normal'
                  CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    complexity    TEXT NOT NULL DEFAULT 'standard'
                  CHECK (complexity IN ('simple', 'standard')),
    blocked       INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT,
    spec_path     TEXT,
    spec_written_at TEXT,
    latest_audit_report_path TEXT,
    latest_audit_kind TEXT,
    latest_audit_verdict TEXT,
    compass_audit_report_path TEXT,
    compass_audit_verdict TEXT,
    tags          TEXT NOT NULL DEFAULT '[]',
    artifacts     TEXT NOT NULL DEFAULT '[]'
);
INSERT INTO tasks (id, project_id, title, status, owner, created_by, complexity)
VALUES
    ('t-x1', 'misc', 'in-flight execute', 'execute', 'p1', 'coach', 'standard'),
    ('t-x2', 'misc', 'simple ticket', 'execute', 'p1', 'coach', 'simple');
"""


async def test_v3_handles_partial_v2_schema(fresh_db: str) -> None:
    """Regression: a DB that ran the original 2130c48 v0.2 rebuild has
    `complexity` but not `required_reviews` / `ship_required`. The v0.3
    rebuild used to crash with `no such column: required_reviews`. It
    must now defensively backfill the missing columns and complete."""
    import json as _json

    async with aiosqlite.connect(dbmod.DB_PATH) as db:
        await db.executescript(_PARTIAL_V2_TASKS_SCHEMA)
        await db.commit()

    await init_db()

    c = await configured_conn()
    try:
        cur = await c.execute("PRAGMA table_info(tasks)")
        cols = {dict(r)["name"] for r in await cur.fetchall()}
        cur = await c.execute(
            "SELECT id, status, trajectory FROM tasks ORDER BY id"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        cur = await c.execute(
            "SELECT value FROM team_config "
            "WHERE key = 'tasks_kanban_v3_migrated'"
        )
        marker = await cur.fetchone()
    finally:
        await c.close()

    assert "complexity" not in cols
    assert "required_reviews" not in cols
    assert "ship_required" not in cols
    assert "trajectory" in cols
    assert marker is not None and marker[0] == "1"

    by_id = {r["id"]: r for r in rows}
    assert by_id["t-x1"]["status"] == "execute"
    standard_traj = _json.loads(by_id["t-x1"]["trajectory"])
    assert [s["stage"] for s in standard_traj] == [
        "execute", "audit_syntax", "audit_semantics", "ship",
    ]
    simple_traj = _json.loads(by_id["t-x2"]["trajectory"])
    assert [s["stage"] for s in simple_traj] == ["execute"]


async def test_role_assignments_table_created(fresh_db: str) -> None:
    """Fresh installs get the task_role_assignments table from the
    canonical SCHEMA."""
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='task_role_assignments'"
        )
        row = await cur.fetchone()
        # Sanity-check the columns we depend on.
        cur = await c.execute("PRAGMA table_info(task_role_assignments)")
        cols = {dict(r)["name"] for r in await cur.fetchall()}
    finally:
        await c.close()
    assert row is not None
    expected = {
        "id", "task_id", "role", "eligible_owners", "owner",
        "assigned_at", "claimed_at", "started_at", "completed_at",
        "report_path", "verdict", "superseded_by",
    }
    assert expected.issubset(cols)

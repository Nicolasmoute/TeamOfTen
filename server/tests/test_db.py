"""Smoke tests for server.db: schema creation + agent seeding.

These catch the class of regressions where a schema change silently
breaks fresh-boot — the harness persists enough state to mask
"CREATE TABLE IF NOT EXISTS" collisions locally but not on a clean VPS.
"""

from __future__ import annotations

from server.db import configured_conn, init_db


async def test_init_db_creates_expected_tables(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "ORDER BY name"
        )
        rows = await cur.fetchall()
    finally:
        await c.close()
    names = {dict(r)["name"] for r in rows}
    for required in {
        "agents",
        "tasks",
        "events",
        "messages",
        "message_reads",
        "memory_docs",
    }:
        assert required in names, f"missing table: {required}"


async def test_init_db_seeds_eleven_agents(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT id, kind FROM agents ORDER BY id")
        rows = await cur.fetchall()
    finally:
        await c.close()
    ids = {dict(r)["id"] for r in rows}
    kinds = {dict(r)["id"]: dict(r)["kind"] for r in rows}
    assert ids == {"coach"} | {f"p{i}" for i in range(1, 11)}
    assert kinds["coach"] == "coach"
    for i in range(1, 11):
        assert kinds[f"p{i}"] == "player"


async def test_init_db_is_idempotent(fresh_db: str) -> None:
    """Second init_db call must not duplicate seed rows or error."""
    await init_db()
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT COUNT(*) AS n FROM agents")
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row)["n"] == 11


async def test_init_db_survives_legacy_pre_refactor_shape(fresh_db: str) -> None:
    """Regression for the production crash where a legacy DB (with
    pre-refactor `tasks`/`events`/etc. that don't have project_id)
    crashed `executescript(SCHEMA)` because SCHEMA tried to create
    `idx_tasks_project ON tasks(project_id)` before `projects_v1`
    could drop+recreate the table with the column.

    This test seeds the legacy shape directly via sqlite3 (bypassing
    init_db) then runs init_db and asserts the migration completed.
    """
    import sqlite3

    import server.db as dbmod

    # Seed the legacy shape — tasks/events/messages/memory_docs/turns
    # without project_id. Mirrors what a pre-refactor production DB
    # looks like on the first refactor deploy.
    legacy = sqlite3.connect(dbmod.DB_PATH)
    try:
        # Mirrors the pre-refactor shape: domain tables exist with
        # their original columns (owner, parent_id, etc.) but NO
        # project_id. SCHEMA's CREATE INDEX … ON tasks(project_id)
        # would crash here pre-fix.
        legacy.executescript(
            """
            CREATE TABLE tasks (
                id            TEXT PRIMARY KEY,
                title         TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL DEFAULT 'open',
                owner         TEXT,
                created_by    TEXT NOT NULL DEFAULT 'coach',
                parent_id     TEXT
            );
            CREATE TABLE events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              TEXT NOT NULL,
                agent_id        TEXT NOT NULL,
                type            TEXT NOT NULL,
                payload         TEXT NOT NULL
            );
            CREATE TABLE messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id      TEXT NOT NULL,
                to_id        TEXT NOT NULL,
                subject      TEXT,
                body         TEXT NOT NULL
            );
            CREATE TABLE memory_docs (
                topic            TEXT PRIMARY KEY,
                content          TEXT NOT NULL,
                last_updated_by  TEXT NOT NULL
            );
            CREATE TABLE turns (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id       TEXT NOT NULL,
                started_at     TEXT NOT NULL,
                ended_at       TEXT NOT NULL
            );
            """
        )
        legacy.commit()
    finally:
        legacy.close()

    # init_db must not crash — it should run executescript(SCHEMA)
    # without referencing project_id on these legacy tables, then let
    # projects_v1 drop+recreate them with the new shape.
    await init_db()

    # After migration, the project_id columns + indexes exist.
    c = await configured_conn()
    try:
        for table in ("tasks", "events", "messages", "memory_docs", "turns"):
            cur = await c.execute(f"PRAGMA table_info({table})")
            cols = {dict(r)["name"] for r in await cur.fetchall()}
            assert "project_id" in cols, f"{table}.project_id missing after migration"
        cur = await c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name LIKE 'idx_%_project'"
        )
        idx = {dict(r)["name"] for r in await cur.fetchall()}
        for required in (
            "idx_tasks_project", "idx_events_project", "idx_messages_project",
            "idx_memory_project", "idx_turns_project",
        ):
            assert required in idx, f"missing index: {required}"
    finally:
        await c.close()

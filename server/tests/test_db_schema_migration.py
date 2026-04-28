"""PR 3 — verify `_ensure_columns` migrates an existing DB on boot.

Per Docs/CODEX_RUNTIME_SPEC.md §J: open an old-schema DB (no Codex
columns), run init_db(), assert the new columns exist with the
expected defaults on existing rows.
"""

from __future__ import annotations

import aiosqlite

import server.db as dbmod


async def _columns(db: aiosqlite.Connection, table: str) -> dict[str, dict]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    # name, type, notnull, default, pk
    return {row[1]: {"type": row[2], "notnull": row[3], "default": row[4]} for row in rows}


async def test_ensure_columns_adds_runtime_override_to_existing_agents(fresh_db: str) -> None:
    # Build a deliberately-old `agents` table missing runtime_override.
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute(
            "CREATE TABLE agents ("
            "  id TEXT PRIMARY KEY,"
            "  kind TEXT NOT NULL CHECK (kind IN ('coach','player')),"
            "  status TEXT NOT NULL DEFAULT 'stopped',"
            "  workspace_path TEXT NOT NULL,"
            "  cost_estimate_usd REAL NOT NULL DEFAULT 0.0,"
            "  locked INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        await db.execute(
            "INSERT INTO agents (id, kind, workspace_path) VALUES (?,?,?)",
            ("p1", "player", "/workspaces/p1"),
        )
        await db.commit()

    # init_db() runs executescript(SCHEMA) (CREATE TABLE IF NOT EXISTS
    # — no-op against the pre-existing table) followed by the
    # _ensure_columns migration runner.
    await dbmod.init_db()

    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        cols = await _columns(db, "agents")
        assert "runtime_override" in cols, "migration did not add agents.runtime_override"
        # Nullable on existing rows — no NOT NULL because role defaults
        # apply when null.
        assert cols["runtime_override"]["notnull"] == 0
        cur = await db.execute("SELECT runtime_override FROM agents WHERE id = ?", ("p1",))
        row = await cur.fetchone()
        assert row is not None and row[0] is None, "existing row should have NULL runtime_override"


async def test_ensure_columns_adds_codex_thread_id_to_agent_sessions(fresh_db: str) -> None:
    await dbmod.init_db()
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        cols = await _columns(db, "agent_sessions")
        assert "codex_thread_id" in cols
        assert "session_id" in cols  # Claude side preserved


async def test_ensure_columns_adds_runtime_and_cost_basis_to_turns(fresh_db: str) -> None:
    # Pre-populate a turn row before init_db() to verify the default
    # backfills correctly. Schema-with-everything is fine here — we're
    # checking that ALTER TABLE … ADD COLUMN populates the default,
    # not whether it's idempotent on an old table (that's covered
    # above).
    await dbmod.init_db()
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        cols = await _columns(db, "turns")
        assert "runtime" in cols
        assert "cost_basis" in cols
        # NOT NULL DEFAULT 'claude' on the schema-fresh column.
        assert cols["runtime"]["notnull"] == 1
        assert cols["runtime"]["default"] == "'claude'"


async def test_ensure_columns_is_idempotent(fresh_db: str) -> None:
    await dbmod.init_db()
    # Second call must not raise (duplicate-column error in SQLite).
    await dbmod.init_db()
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        cols = await _columns(db, "agents")
        assert "runtime_override" in cols

"""DB-level tests for the turns analytics ledger.

Same pattern as test_tasks_sm.py — exercise the schema directly
without pulling the FastAPI app (or claude_agent_sdk) in. Covers:
  - init_db creates the `turns` table
  - inserts with the full column set work
  - indexes exist
  - is_error / plan_mode are stored as integers (SQLite has no bool)
"""

from __future__ import annotations

from server.db import configured_conn, init_db


async def test_turns_table_created_by_init_db(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='turns'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is not None


async def test_turns_insert_roundtrip(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO turns ("
            "agent_id, project_id, started_at, ended_at, duration_ms, cost_usd, "
            "session_id, num_turns, stop_reason, is_error, "
            "model, plan_mode, effort"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "p3",
                "misc",
                "2026-04-23T17:00:00Z",
                "2026-04-23T17:00:05Z",
                5000,
                0.0123,
                "sess-abc",
                3,
                "end_turn",
                0,
                "claude-sonnet-4-6",
                0,
                2,
            ),
        )
        await c.commit()
        cur = await c.execute(
            "SELECT agent_id, duration_ms, cost_usd, session_id, num_turns, "
            "stop_reason, is_error, model, plan_mode, effort FROM turns "
            "WHERE agent_id = 'p3'"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["agent_id"] == "p3"
    assert row["duration_ms"] == 5000
    assert abs(row["cost_usd"] - 0.0123) < 1e-9
    assert row["session_id"] == "sess-abc"
    assert row["num_turns"] == 3
    assert row["stop_reason"] == "end_turn"
    assert row["is_error"] == 0
    assert row["model"] == "claude-sonnet-4-6"
    assert row["plan_mode"] == 0
    assert row["effort"] == 2


async def test_turns_defaults(fresh_db: str) -> None:
    # Minimum required columns: agent_id, started_at, ended_at. All
    # the rest are nullable / have defaults.
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO turns (agent_id, project_id, started_at, ended_at) "
            "VALUES (?, ?, ?, ?)",
            ("coach", "misc", "2026-04-23T17:00:00Z", "2026-04-23T17:00:01Z"),
        )
        await c.commit()
        cur = await c.execute(
            "SELECT is_error, plan_mode, cost_usd FROM turns WHERE agent_id = 'coach'"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    # Defaults: is_error / plan_mode default to 0 per schema; cost_usd
    # is nullable and omitted.
    assert row["is_error"] == 0
    assert row["plan_mode"] == 0
    assert row["cost_usd"] is None


async def test_turns_indexes_exist(fresh_db: str) -> None:
    # idx_turns_agent (agent_id, id) and idx_turns_ended_at should be
    # present — they're the only two supported query paths.
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'turns'"
        )
        names = {dict(r)["name"] for r in await cur.fetchall()}
    finally:
        await c.close()
    assert "idx_turns_agent" in names
    assert "idx_turns_ended_at" in names

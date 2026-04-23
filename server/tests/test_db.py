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

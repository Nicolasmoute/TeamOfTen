"""Tests for db.crash_recover — orphaned-state cleanup on boot."""

from __future__ import annotations

from server.db import configured_conn, crash_recover, init_db


async def test_clean_db_is_a_noop(fresh_db: str) -> None:
    await init_db()
    reset = await crash_recover()
    assert reset == {"agents_reset": 0, "tasks_reset": 0}


async def test_resets_working_agents_to_idle(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        await c.execute("UPDATE agents SET status = 'working' WHERE id = 'coach'")
        await c.execute("UPDATE agents SET status = 'waiting' WHERE id = 'p3'")
        await c.commit()
    finally:
        await c.close()
    reset = await crash_recover()
    assert reset["agents_reset"] == 2
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, status FROM agents WHERE id IN ('coach', 'p3')"
        )
        rows = {dict(r)["id"]: dict(r)["status"] for r in await cur.fetchall()}
    finally:
        await c.close()
    assert rows == {"coach": "idle", "p3": "idle"}


async def test_resets_in_progress_tasks_to_claimed(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, title, status, owner, created_by) "
            "VALUES ('t-live', 't', 'in_progress', 'p5', 'coach')"
        )
        await c.execute(
            "INSERT INTO tasks (id, title, status, owner, created_by) "
            "VALUES ('t-done', 'd', 'done', 'p5', 'coach')"
        )
        await c.execute(
            "INSERT INTO tasks (id, title, status, created_by) "
            "VALUES ('t-open', 'o', 'open', 'coach')"
        )
        await c.commit()
    finally:
        await c.close()
    reset = await crash_recover()
    # Only the in_progress row should flip.
    assert reset["tasks_reset"] == 1
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT id, status, owner FROM tasks")
        rows = {dict(r)["id"]: dict(r) for r in await cur.fetchall()}
    finally:
        await c.close()
    # Owner preserved so the Player knows what they were doing.
    assert rows["t-live"]["status"] == "claimed"
    assert rows["t-live"]["owner"] == "p5"
    # Done / open rows untouched.
    assert rows["t-done"]["status"] == "done"
    assert rows["t-open"]["status"] == "open"


async def test_idempotent(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        await c.execute("UPDATE agents SET status = 'working' WHERE id = 'p1'")
        await c.commit()
    finally:
        await c.close()
    first = await crash_recover()
    second = await crash_recover()
    assert first["agents_reset"] == 1
    assert second["agents_reset"] == 0

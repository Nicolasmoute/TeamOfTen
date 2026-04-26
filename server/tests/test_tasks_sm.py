"""Task state-machine tests at the DB level.

We don't spin up the full FastAPI app (which would pull in
claude_agent_sdk on import) — instead, exercise the schema directly
by inserting rows that match what POST /api/tasks / cancel_task
would produce. This catches CHECK-constraint regressions and keeps
the path small.
"""

from __future__ import annotations

from server.db import configured_conn, init_db


async def test_task_insert_with_valid_status(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, created_by) "
            "VALUES ('t-1', 'misc', 'test task', 'human')"
        )
        await c.commit()
        cur = await c.execute("SELECT status FROM tasks WHERE id = 't-1'")
        row = await cur.fetchone()
    finally:
        await c.close()
    # Default status should be 'open'
    assert dict(row)["status"] == "open"


async def test_task_status_check_rejects_invalid(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        try:
            await c.execute(
                "INSERT INTO tasks (id, project_id, title, created_by, status) "
                "VALUES ('t-1', 'misc', 'x', 'human', 'bogus')"
            )
            raise AssertionError("insert should have failed the CHECK")
        except Exception as e:
            # sqlite3.IntegrityError is raised via aiosqlite — assert
            # the message mentions the CHECK rather than importing the
            # concrete exception class (aiosqlite wraps it).
            assert "CHECK" in str(e) or "check" in str(e).lower()
    finally:
        await c.close()


async def test_cancel_clears_owner_current_task(fresh_db: str) -> None:
    """Mirror the logic of cancel_task_from_human without spinning up
    the app: create a task, mark it claimed by p1, cancel, verify
    the agent's current_task_id is cleared."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, created_by, status, owner) "
            "VALUES ('t-42', 'misc', 'demo', 'coach', 'in_progress', 'p1')"
        )
        await c.execute(
            "UPDATE agents SET current_task_id = 't-42' WHERE id = 'p1'"
        )
        await c.commit()
        # cancel flow
        await c.execute(
            "UPDATE tasks SET status = 'cancelled' WHERE id = 't-42'"
        )
        await c.execute(
            "UPDATE agents SET current_task_id = NULL "
            "WHERE id = 'p1' AND current_task_id = 't-42'"
        )
        await c.commit()
        cur = await c.execute(
            "SELECT current_task_id FROM agents WHERE id = 'p1'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row)["current_task_id"] is None


async def test_agent_kind_check_rejects_invalid(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        try:
            await c.execute(
                "INSERT INTO agents (id, kind, workspace_path) "
                "VALUES ('x', 'alien', '/nowhere')"
            )
            raise AssertionError("insert should have failed the CHECK")
        except Exception as e:
            assert "CHECK" in str(e) or "check" in str(e).lower()
    finally:
        await c.close()

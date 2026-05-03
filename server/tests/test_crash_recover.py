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


async def test_clears_started_at_on_zombie_executor_tasks(fresh_db: str) -> None:
    """Kanban-shaped crash_recover: zombie agents (status='working' or
    'waiting' at boot) get demoted to idle, and any task they were
    actively working (status='execute' AND started_at non-NULL) has its
    started_at cleared so the next auto-wake cleanly re-flips the avatar
    from hollow → filled. Owner stays so the Player knows what they
    were doing.

    Tasks not owned by zombies, and tasks the zombie owns but in other
    stages (plan / archive), should be untouched.
    """
    await init_db()
    c = await configured_conn()
    try:
        # Zombie executor with a task they were actively working.
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, claimed_at, started_at) "
            "VALUES ('t-live', 'misc', 't', 'execute', 'p5', 'coach', "
            "'2026-05-01T10:00:00Z', '2026-05-01T10:05:00Z')"
        )
        # Same agent's archived task — terminal, no started_at to clear.
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, archived_at) "
            "VALUES ('t-arch', 'misc', 'a', 'archive', 'p5', 'coach', "
            "'2026-05-01T11:00:00Z')"
        )
        # Plan-stage task — no executor yet, untouched.
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, created_by) "
            "VALUES ('t-plan', 'misc', 'p', 'plan', 'coach')"
        )
        # Different (non-zombie) Player's executor task — untouched.
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, claimed_at, started_at) "
            "VALUES ('t-other', 'misc', 'o', 'execute', 'p7', 'coach', "
            "'2026-05-01T09:00:00Z', '2026-05-01T09:05:00Z')"
        )
        # Mark p5 as a zombie ('working'). p7 stays idle.
        await c.execute("UPDATE agents SET status = 'working' WHERE id = 'p5'")
        # Mirror the executor row into task_role_assignments so we can
        # assert the parallel reset there too.
        await c.execute(
            "INSERT INTO task_role_assignments (task_id, role, owner, "
            "assigned_at, claimed_at, started_at) "
            "VALUES ('t-live', 'executor', 'p5', "
            "'2026-05-01T10:00:00Z', '2026-05-01T10:00:00Z', "
            "'2026-05-01T10:05:00Z')"
        )
        await c.commit()
    finally:
        await c.close()

    reset = await crash_recover()
    # Only t-live (p5's executor task in 'execute') should be touched.
    assert reset["tasks_reset"] == 1

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, status, owner, started_at FROM tasks ORDER BY id"
        )
        rows = {dict(r)["id"]: dict(r) for r in await cur.fetchall()}
        cur = await c.execute(
            "SELECT task_id, started_at FROM task_role_assignments"
        )
        roles = {dict(r)["task_id"]: dict(r) for r in await cur.fetchall()}
    finally:
        await c.close()

    # Zombie's active task: started_at cleared, owner preserved, status unchanged.
    assert rows["t-live"]["status"] == "execute"
    assert rows["t-live"]["owner"] == "p5"
    assert rows["t-live"]["started_at"] is None
    # Mirror reset on role-assignment row.
    assert roles["t-live"]["started_at"] is None
    # Untouched rows.
    assert rows["t-arch"]["status"] == "archive"
    assert rows["t-plan"]["status"] == "plan"
    assert rows["t-other"]["status"] == "execute"
    assert rows["t-other"]["started_at"] == "2026-05-01T09:05:00Z"


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

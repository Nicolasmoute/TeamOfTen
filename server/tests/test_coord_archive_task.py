"""Audit tests for coord_archive_task (Docs/kanban-specs-v2.md §7.1).

Covers:
- Coach-only enforcement (Players rejected).
- summary required.
- summary length cap.
- Archive transition + role completion.
- agents.current_task_id cleared on owning Player.
- task_archived event carries the summary.
- task_stage_changed emitted alongside.
- Already-archived task rejected.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from server.db import configured_conn, init_db
from server.events import bus
from server.tools import build_coord_server


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok_text(result: dict[str, Any]) -> str:
    assert not result.get("is_error"), f"tool returned error: {result}"
    return result["content"][0]["text"]


def _err_text(result: dict[str, Any]) -> str:
    assert result.get("is_error"), f"expected error, got {result}"
    return result["content"][0]["text"]


async def _seed_task(
    *,
    task_id: str = "t-2026-05-07-bbbb2222",
    status: str = "ship",
    owner: str = "p2",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) "
            "VALUES (?, 'misc', 'demo', ?, ?, 'coach', '[]')",
            (task_id, status, owner),
        )
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, "
            " assigned_at, claimed_at) "
            "VALUES (?, 'shipper', '[]', ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (task_id, owner),
        )
        await c.execute(
            "UPDATE agents SET current_task_id = ? WHERE id = ?",
            (task_id, owner),
        )
        await c.commit()
    finally:
        await c.close()


async def test_archive_task_rejects_player(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("p2")
    err = _err_text(await _handler(server, "archive_task")({
        "task_id": "t-2026-05-07-bbbb2222",
        "summary": "shipped",
    }))
    assert "coach" in err.lower()


async def test_archive_task_requires_summary(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "archive_task")({
        "task_id": "t-2026-05-07-bbbb2222",
        "summary": "",
    }))
    assert "summary" in err.lower()


async def test_archive_task_summary_too_long(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "archive_task")({
        "task_id": "t-2026-05-07-bbbb2222",
        "summary": "x" * 5001,
    }))
    assert "too long" in err.lower()


async def test_archive_task_unknown_id(fresh_db: str) -> None:
    await init_db()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "archive_task")({
        "task_id": "t-no-such-id",
        "summary": "should fail",
    }))
    assert "not found" in err.lower()


async def test_archive_task_already_archived_rejected(fresh_db: str) -> None:
    await init_db()
    await _seed_task(status="archive")
    server = _server_for("coach")
    err = _err_text(await _handler(server, "archive_task")({
        "task_id": "t-2026-05-07-bbbb2222",
        "summary": "double archive",
    }))
    assert "archive" in err.lower()


async def test_archive_task_happy_path(fresh_db: str) -> None:
    await init_db()
    await _seed_task()

    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("coach")
        text = _ok_text(await _handler(server, "archive_task")({
            "task_id": "t-2026-05-07-bbbb2222",
            "summary": "Delivered the demo feature; ready for the user.",
        }))
        assert "Archived" in text
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while True:
            try:
                captured.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    types = [e.get("type") for e in captured]
    assert "task_stage_changed" in types
    assert "task_archived" in types

    sc = next(e for e in captured if e["type"] == "task_stage_changed")
    assert sc["from"] == "ship"
    assert sc["to"] == "archive"

    arch = next(e for e in captured if e["type"] == "task_archived")
    assert arch["summary"].startswith("Delivered the demo feature")

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, archived_at, completed_at FROM tasks WHERE id = ?",
            ("t-2026-05-07-bbbb2222",),
        )
        t = dict(await cur.fetchone())
        assert t["status"] == "archive"
        assert t["archived_at"]
        assert t["completed_at"]

        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments WHERE task_id = ?",
            ("t-2026-05-07-bbbb2222",),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        assert all(r["completed_at"] for r in rows)

        cur = await c.execute(
            "SELECT current_task_id FROM agents WHERE id = 'p2'"
        )
        a = dict(await cur.fetchone())
        assert a["current_task_id"] is None
    finally:
        await c.close()


async def test_archive_task_accepts_verify_stage(fresh_db: str) -> None:
    await init_db()
    await _seed_task(status="verify", owner="p2")
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, claimed_at) "
            "VALUES ('t-2026-05-07-bbbb2222', 'verifier', '[]', 'p4', "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
        )
        await c.execute(
            "UPDATE agents SET current_task_id = ? WHERE id = 'p4'",
            ("t-2026-05-07-bbbb2222",),
        )
        await c.commit()
    finally:
        await c.close()

    server = _server_for("coach")
    _ok_text(await _handler(server, "archive_task")({
        "task_id": "t-2026-05-07-bbbb2222",
        "summary": "Verified in dev; ready to close.",
    }))

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, archived_at FROM tasks WHERE id = ?",
            ("t-2026-05-07-bbbb2222",),
        )
        task = dict(await cur.fetchone())
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'verifier'",
            ("t-2026-05-07-bbbb2222",),
        )
        verifier = dict(await cur.fetchone())
        cur = await c.execute("SELECT current_task_id FROM agents WHERE id = 'p4'")
        agent = dict(await cur.fetchone())
    finally:
        await c.close()

    assert task["status"] == "archive"
    assert task["archived_at"]
    assert verifier["completed_at"]
    assert agent["current_task_id"] is None

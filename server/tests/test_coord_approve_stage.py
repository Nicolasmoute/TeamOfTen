"""Audit tests for coord_approve_stage (Docs/kanban-specs-v2.md §7.1).

Covers:
- Coach-only enforcement (Players rejected).
- Parameter validation (task_id, next_stage, assignee).
- Pool / multi-assignee rejection (v2 takes a single slot).
- Archive path drops `assignee`.
- Locked-Player rejection.
- Atomic supersede + plant of role row.
- Source-stage stand-down when Coach overrides without source completion.
- Stage transition validation (state machine).
- Bus events (`task_stage_changed`, `task_role_assigned`).
- Wake fires on the new assignee.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from server.db import configured_conn, init_db
from server.events import bus
from server.tools import build_coord_server


_STANDARD_TRAJECTORY = (
    '[{"stage":"plan","to":[]},'
    '{"stage":"execute","to":[]},'
    '{"stage":"audit_syntax","to":[]},'
    '{"stage":"audit_semantics","to":["p4"],"focus":"check x"},'
    '{"stage":"ship","to":[]}]'
)


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok_text(result: dict[str, Any]) -> str:
    assert not result.get("isError"), f"tool returned error: {result}"
    return result["content"][0]["text"]


def _err_text(result: dict[str, Any]) -> str:
    assert result.get("isError"), f"expected error, got {result}"
    return result["content"][0]["text"]


async def _seed_task(
    *,
    task_id: str = "t-2026-05-07-aaaa1111",
    status: str = "plan",
    owner: str | None = None,
    trajectory: str = _STANDARD_TRAJECTORY,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) "
            "VALUES (?, 'misc', 'demo', ?, ?, 'coach', ?)",
            (task_id, status, owner, trajectory),
        )
        await c.commit()
    finally:
        await c.close()


async def _plant_role(
    *,
    task_id: str,
    role: str,
    owner: str | None = None,
    eligible: list[str] | None = None,
) -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, "
            "assigned_at, claimed_at) "
            "VALUES (?, ?, ?, ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "CASE WHEN ? IS NULL THEN NULL ELSE "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now') END)",
            (
                task_id, role,
                json.dumps(eligible or [], separators=(",", ":")),
                owner, owner,
            ),
        )
        await c.commit()
        return cur.lastrowid
    finally:
        await c.close()


# ---------------------------------------------------------------------
# Coach-only + validation
# ---------------------------------------------------------------------

async def test_approve_stage_rejects_player(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("p1")
    err = _err_text(await _handler(server, "approve_stage")({
        "task_id": "t-2026-05-07-aaaa1111",
        "next_stage": "execute",
        "assignee": "p2",
    }))
    assert "coach" in err.lower()


async def test_approve_stage_rejects_invalid_next_stage(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "approve_stage")({
        "task_id": "t-2026-05-07-aaaa1111",
        "next_stage": "bogus",
        "assignee": "p2",
    }))
    assert "next_stage" in err.lower()


async def test_approve_stage_rejects_pool_assignee(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "approve_stage")({
        "task_id": "t-2026-05-07-aaaa1111",
        "next_stage": "execute",
        "assignee": "p2,p3",
    }))
    assert "single" in err.lower() or "pool" in err.lower()


async def test_approve_stage_rejects_assignee_for_archive(fresh_db: str) -> None:
    await init_db()
    await _seed_task(status="execute", owner="p2")
    server = _server_for("coach")
    err = _err_text(await _handler(server, "approve_stage")({
        "task_id": "t-2026-05-07-aaaa1111",
        "next_stage": "archive",
        "assignee": "p3",
    }))
    assert "archive" in err.lower() and "assignee" in err.lower()


async def test_approve_stage_requires_assignee_for_non_archive(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "approve_stage")({
        "task_id": "t-2026-05-07-aaaa1111",
        "next_stage": "execute",
    }))
    assert "assignee" in err.lower()


async def test_approve_stage_rejects_coach_or_broadcast(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    for bogus in ("coach", "broadcast", "p99", "p0"):
        err = _err_text(await _handler(server, "approve_stage")({
            "task_id": "t-2026-05-07-aaaa1111",
            "next_stage": "execute",
            "assignee": bogus,
        }))
        assert "p1..p10" in err.lower() or "slot" in err.lower()


async def test_approve_stage_rejects_locked_assignee(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    c = await configured_conn()
    try:
        await c.execute("UPDATE agents SET locked = 1 WHERE id = 'p2'")
        await c.commit()
    finally:
        await c.close()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "approve_stage")({
        "task_id": "t-2026-05-07-aaaa1111",
        "next_stage": "execute",
        "assignee": "p2",
    }))
    assert "locked" in err.lower()


async def test_approve_stage_invalid_transition_rejected(fresh_db: str) -> None:
    await init_db()
    await _seed_task(status="ship", owner="p2")
    server = _server_for("coach")
    err = _err_text(await _handler(server, "approve_stage")({
        "task_id": "t-2026-05-07-aaaa1111",
        "next_stage": "execute",
        "assignee": "p2",
    }))
    assert "invalid transition" in err.lower()


async def test_approve_stage_archived_task_rejected(fresh_db: str) -> None:
    await init_db()
    await _seed_task(status="archive")
    server = _server_for("coach")
    err = _err_text(await _handler(server, "approve_stage")({
        "task_id": "t-2026-05-07-aaaa1111",
        "next_stage": "execute",
        "assignee": "p2",
    }))
    assert "archive" in err.lower()


# ---------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------

async def test_approve_stage_plants_executor_and_emits_events(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    # Spec gate doesn't apply to coord_approve_stage (it's a Coach
    # override path). Pre-completing the planner mirrors the natural
    # flow but isn't required.
    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("coach")
        text = _ok_text(await _handler(server, "approve_stage")({
            "task_id": "t-2026-05-07-aaaa1111",
            "next_stage": "execute",
            "assignee": "p2",
            "note": "[deviation: scope crept] please refocus",
        }))
        assert "p2" in text
        # Drain async event publications.
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
    assert "task_role_assigned" in types

    sc = next(e for e in captured if e["type"] == "task_stage_changed")
    assert sc["from"] == "plan"
    assert sc["to"] == "execute"
    assert sc["assignee"] == "p2"
    assert sc["note"].startswith("[deviation:")

    ra = next(e for e in captured if e["type"] == "task_role_assigned")
    assert ra["role"] == "executor"
    assert ra["owner"] == "p2"

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, owner, last_stage_change_at FROM tasks "
            "WHERE id = ?",
            ("t-2026-05-07-aaaa1111",),
        )
        t = dict(await cur.fetchone())
        assert t["status"] == "execute"
        assert t["owner"] == "p2"
        assert t["last_stage_change_at"]

        cur = await c.execute(
            "SELECT role, owner, completed_at, superseded_by "
            "FROM task_role_assignments WHERE task_id = ?",
            ("t-2026-05-07-aaaa1111",),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        active = [r for r in rows if r["completed_at"] is None and r["superseded_by"] is None]
        assert any(r["role"] == "executor" and r["owner"] == "p2" for r in active)

        cur = await c.execute(
            "SELECT current_task_id FROM agents WHERE id = 'p2'"
        )
        a = dict(await cur.fetchone())
        assert a["current_task_id"] == "t-2026-05-07-aaaa1111"
    finally:
        await c.close()


async def test_approve_stage_supersedes_prior_target_role_with_stand_down(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="plan")
    # An earlier (stale) executor assignment exists — Coach is now
    # picking a different Player. The prior owner should be superseded
    # and stood down.
    await _plant_role(task_id="t-2026-05-07-aaaa1111", role="executor", owner="p3")

    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("coach")
        _ok_text(await _handler(server, "approve_stage")({
            "task_id": "t-2026-05-07-aaaa1111",
            "next_stage": "execute",
            "assignee": "p2",
            "note": "switching executor",
        }))
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
    assert "task_role_stand_down" in types
    sd = next(e for e in captured if e["type"] == "task_role_stand_down")
    assert "p3" in sd.get("displaced", [])

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT owner, completed_at, superseded_by "
            "FROM task_role_assignments WHERE task_id = ? AND role = 'executor'",
            ("t-2026-05-07-aaaa1111",),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        # New active row owned by p2.
        active = [r for r in rows if r["completed_at"] is None and r["superseded_by"] is None]
        assert len(active) == 1
        assert active[0]["owner"] == "p2"
        # Prior p3 row superseded.
        prior = [r for r in rows if r["owner"] == "p3"]
        assert prior and prior[0]["superseded_by"] is not None
    finally:
        await c.close()


async def test_approve_stage_archive_no_assignee_marks_roles_complete(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="execute", owner="p2")
    await _plant_role(task_id="t-2026-05-07-aaaa1111", role="executor", owner="p2")
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "approve_stage")({
        "task_id": "t-2026-05-07-aaaa1111",
        "next_stage": "archive",
    }))
    assert "archive" in text.lower()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, archived_at FROM tasks WHERE id = ?",
            ("t-2026-05-07-aaaa1111",),
        )
        t = dict(await cur.fetchone())
        assert t["status"] == "archive"
        assert t["archived_at"]
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments "
            "WHERE task_id = ?",
            ("t-2026-05-07-aaaa1111",),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        assert all(r["completed_at"] for r in rows)
        # current_task_id cleared on the executor.
        cur = await c.execute(
            "SELECT current_task_id FROM agents WHERE id = 'p2'"
        )
        a = dict(await cur.fetchone())
        assert a["current_task_id"] is None
    finally:
        await c.close()


async def test_approve_stage_overrides_uncompleted_source_with_stand_down(
    fresh_db: str,
) -> None:
    """When Coach forces a transition while the source-stage role row
    is still active, the source row is closed and its owner gets a
    stand-down wake (per v2 §7.1)."""
    await init_db()
    await _seed_task(status="execute", owner="p2")
    # Active executor row that has NOT been completed.
    await _plant_role(task_id="t-2026-05-07-aaaa1111", role="executor", owner="p2")

    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("coach")
        _ok_text(await _handler(server, "approve_stage")({
            "task_id": "t-2026-05-07-aaaa1111",
            "next_stage": "audit_syntax",
            "assignee": "p4",
            "note": "stuck executor; auditing what we have",
        }))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while True:
            try:
                captured.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    # Source-stage stand-down emitted with p2 displaced.
    sds = [e for e in captured if e.get("type") == "task_role_stand_down"]
    assert any(
        e.get("role") == "executor" and "p2" in e.get("displaced", [])
        for e in sds
    )

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT role, owner, completed_at FROM task_role_assignments "
            "WHERE task_id = ?",
            ("t-2026-05-07-aaaa1111",),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        # Executor row was force-completed (or superseded).
        executor_rows = [r for r in rows if r["role"] == "executor"]
        assert all(
            r["completed_at"] is not None
            for r in executor_rows
        )
        # New auditor row exists.
        active_aud = [
            r for r in rows
            if r["role"] == "auditor_syntax" and r["completed_at"] is None
        ]
        assert len(active_aud) == 1
        assert active_aud[0]["owner"] == "p4"
    finally:
        await c.close()

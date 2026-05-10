"""Audit tests for the v2 `message_to_coach` field on completion tools.

Round-trips the optional Player→Coach note through:
  - coord_write_task_spec  → task_spec_written
  - coord_submit_audit_report → audit_report_submitted

(coord_commit_push needs a real worktree; its event-payload wiring is
covered by reading the source — the field is added to the same dict
that the other two tools populate.)

Also asserts the field is rejected when too long.
"""

from __future__ import annotations

import asyncio
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


async def _seed(*, status: str = "plan", owner: str | None = None) -> str:
    task_id = "t-2026-05-07-abcdef01"
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) "
            "VALUES (?, 'misc', 'demo', ?, ?, 'coach', '[]')",
            (task_id, status, owner),
        )
        await c.commit()
    finally:
        await c.close()
    return task_id


# ---------------------------------------------------------------------
# coord_write_task_spec round-trip
# ---------------------------------------------------------------------

async def test_write_task_spec_message_to_coach_in_event(fresh_db: str) -> None:
    await init_db()
    task_id = await _seed()
    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("coach")
        _ok_text(await _handler(server, "write_task_spec")({
            "task_id": task_id,
            "body": "## Goal\nDo it.\n",
            "message_to_coach": "draft is rough — heads up",
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
    spec = next(e for e in captured if e["type"] == "task_spec_written")
    assert spec["message_to_coach"] == "draft is rough — heads up"


async def test_write_task_spec_message_to_coach_too_long(fresh_db: str) -> None:
    await init_db()
    task_id = await _seed()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "write_task_spec")({
        "task_id": task_id,
        "body": "## Goal\nDo it.\n",
        "message_to_coach": "x" * 2001,
    }))
    assert "too long" in err.lower()


async def test_write_task_spec_omits_message_to_coach_when_blank(fresh_db: str) -> None:
    await init_db()
    task_id = await _seed()
    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("coach")
        _ok_text(await _handler(server, "write_task_spec")({
            "task_id": task_id,
            "body": "## Goal\nDo it.\n",
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
    spec = next(e for e in captured if e["type"] == "task_spec_written")
    assert spec["message_to_coach"] is None


# ---------------------------------------------------------------------
# coord_submit_audit_report round-trip
# ---------------------------------------------------------------------

async def _seed_for_audit() -> str:
    task_id = "t-2026-05-07-abcdef01"
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) "
            "VALUES (?, 'misc', 'demo', 'audit_syntax', 'p2', 'coach', '[]')",
            (task_id,),
        )
        # Active auditor_syntax row owned by p4.
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, "
            " assigned_at, claimed_at) "
            "VALUES (?, 'auditor_syntax', '[]', 'p4', "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (task_id,),
        )
        await c.commit()
    finally:
        await c.close()
    return task_id


async def test_submit_audit_report_message_to_coach_in_event(fresh_db: str) -> None:
    await init_db()
    task_id = await _seed_for_audit()
    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("p4")
        _ok_text(await _handler(server, "submit_audit_report")({
            "task_id": task_id,
            "kind": "syntax",
            "body": "## Summary\nlooks good\n",
            "verdict": "pass",
            "message_to_coach": "lgtm — minor style nits inline",
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
    rep = next(e for e in captured if e["type"] == "audit_report_submitted")
    assert rep["message_to_coach"] == "lgtm — minor style nits inline"


async def test_submit_audit_report_message_to_coach_too_long(fresh_db: str) -> None:
    await init_db()
    task_id = await _seed_for_audit()
    server = _server_for("p4")
    err = _err_text(await _handler(server, "submit_audit_report")({
        "task_id": task_id,
        "kind": "syntax",
        "body": "## Summary\nlooks good\n",
        "verdict": "pass",
        "message_to_coach": "x" * 2001,
    }))
    assert "too long" in err.lower()

"""Audit tests for coord_request_plan_review (Docs/kanban-specs-v2.md §7.1).

Covers:
- Coach-only enforcement.
- task_id required.
- slot validation (must be p1..p10; coach/broadcast rejected).
- Locked Player rejected.
- Unknown task_id rejected.
- maybe_wake_agent called with plan_mode=True.
- plan_review_requested bus event emitted.
"""

from __future__ import annotations

import asyncio
from typing import Any

import server.agents as agentsmod
from server.db import configured_conn, init_db
from server.events import bus
from server.tools import build_coord_server


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


async def _seed_task(*, task_id: str = "t-2026-05-07-dddd4444") -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, spec_path) "
            "VALUES (?, 'misc', 'demo', 'plan', NULL, 'coach', '[]', NULL)",
            (task_id,),
        )
        await c.commit()
    finally:
        await c.close()


async def test_request_plan_review_rejects_player(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("p1")
    err = _err_text(await _handler(server, "request_plan_review")({
        "task_id": "t-2026-05-07-dddd4444",
        "slot": "p2",
    }))
    assert "coach" in err.lower()


async def test_request_plan_review_requires_task_id(fresh_db: str) -> None:
    await init_db()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "request_plan_review")({
        "task_id": "",
        "slot": "p2",
    }))
    assert "task_id" in err.lower()


async def test_request_plan_review_rejects_invalid_slot(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    for bad in ("coach", "broadcast", "p99", "p0", "human"):
        err = _err_text(await _handler(server, "request_plan_review")({
            "task_id": "t-2026-05-07-dddd4444",
            "slot": bad,
        }))
        assert "p1..p10" in err.lower() or "slot" in err.lower()


async def test_request_plan_review_rejects_locked(fresh_db: str) -> None:
    await init_db()
    await _seed_task()
    c = await configured_conn()
    try:
        await c.execute("UPDATE agents SET locked = 1 WHERE id = 'p3'")
        await c.commit()
    finally:
        await c.close()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "request_plan_review")({
        "task_id": "t-2026-05-07-dddd4444",
        "slot": "p3",
    }))
    assert "locked" in err.lower()


async def test_request_plan_review_unknown_task(fresh_db: str) -> None:
    await init_db()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "request_plan_review")({
        "task_id": "t-no-such",
        "slot": "p2",
    }))
    assert "not found" in err.lower()


async def test_request_plan_review_invokes_wake_with_plan_mode(
    fresh_db: str, monkeypatch
) -> None:
    """The tool must call maybe_wake_agent with plan_mode=True."""
    await init_db()
    await _seed_task()
    captures: list[dict[str, Any]] = []

    async def fake_wake(slot, body, *, bypass_debounce=False, wake_source=None,
                        plan_mode=None):
        captures.append({
            "slot": slot,
            "body": body,
            "bypass_debounce": bypass_debounce,
            "wake_source": wake_source,
            "plan_mode": plan_mode,
        })
        return True

    monkeypatch.setattr(agentsmod, "maybe_wake_agent", fake_wake)

    q = bus.subscribe()
    captured_events: list[dict[str, Any]] = []
    try:
        server = _server_for("coach")
        text = _ok_text(await _handler(server, "request_plan_review")({
            "task_id": "t-2026-05-07-dddd4444",
            "slot": "p2",
        }))
        assert "p2" in text
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while True:
            try:
                captured_events.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    assert len(captures) == 1
    assert captures[0]["slot"] == "p2"
    assert captures[0]["plan_mode"] is True
    assert captures[0]["wake_source"] == "kanban_plan_review"
    assert captures[0]["bypass_debounce"] is True
    assert "ExitPlanMode" in captures[0]["body"]

    types = [e.get("type") for e in captured_events]
    assert "plan_review_requested" in types


async def test_request_plan_review_logs_when_wake_skipped(
    fresh_db: str, monkeypatch
) -> None:
    await init_db()
    await _seed_task()

    async def fake_wake(*args, **kwargs):
        return False  # busy / paused / cost-capped

    monkeypatch.setattr(agentsmod, "maybe_wake_agent", fake_wake)
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "request_plan_review")({
        "task_id": "t-2026-05-07-dddd4444",
        "slot": "p2",
    }))
    assert "didn't fire" in text.lower() or "did not fire" in text.lower()

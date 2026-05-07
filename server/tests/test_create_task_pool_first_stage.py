"""Phase 1 — pool/empty first-stage create flow (v2 §7.1).

Verifies the same-stage allowance fix in `coord_approve_stage`:

- A task created with `to=["p2","p3"]` (pool) on the first trajectory
  entry has no role row planted; `tasks.status` is the first stage.
  Coach's first follow-up call `coord_approve_stage(next_stage=<same
  stage>, assignee=<slot>)` must succeed and plant the role row.
- The same-stage path emits only `task_role_assigned`, not
  `task_stage_changed` (the board didn't move; only an assignee got
  picked).
- A second `coord_approve_stage` at the same stage with an active role
  row is rejected (the explicit "use next stage instead" error).
- Single-name first-stage tasks behave unchanged (role row already
  planted at create time; no same-stage call needed).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest

import server.agents as agents_mod
from server.db import configured_conn, init_db
from server.events import bus
from server.tools import build_coord_server


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok(result: dict[str, Any]) -> str:
    assert not result.get("isError"), (
        f"tool returned error: {result.get('content')}"
    )
    return result["content"][0]["text"]


def _err(result: dict[str, Any]) -> str:
    assert result.get("isError"), f"expected error, got {result}"
    return result["content"][0]["text"]


def _drain(queue: Any) -> list[dict]:
    out: list[dict] = []
    while True:
        try:
            out.append(queue.get_nowait())
        except Exception:
            break
    return out


def _extract_task_id(body: str) -> str:
    m = re.search(r"t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}", body)
    assert m, f"no task id in body: {body}"
    return m.group(0)


async def _stub_wake(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def _rec(slot: str, prompt: str = "", **kw: Any) -> bool:
        calls.append((slot, prompt))
        return True

    monkeypatch.setattr(agents_mod, "maybe_wake_agent", _rec)
    return calls


async def _active_role_owner(task_id: str, role: str) -> str | None:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT owner FROM task_role_assignments "
            "WHERE task_id = ? AND role = ? "
            "AND completed_at IS NULL AND superseded_by IS NULL "
            "ORDER BY assigned_at DESC LIMIT 1",
            (task_id, role),
        )
        row = await cur.fetchone()
        return dict(row).get("owner") if row else None
    finally:
        await c.close()


# ---------------------------------------------------------------- pool create

async def test_pool_first_stage_then_approve_same_stage_succeeds(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §7.1 happy path: pool first-stage trajectory creates task in
    that stage with no role row; Coach approves the same stage with an
    explicit assignee → role row plants, wake fires, no
    `task_stage_changed` event emits (board didn't move)."""
    await init_db()
    wakes = await _stub_wake(monkeypatch)
    coach = _server_for("coach")

    create = _handler(coach, "create_task")
    res = await create({
        "title": "pool-create demo",
        "description": "x",
        "trajectory": (
            '[{"stage":"execute","to":["p2","p3"]},'
            '{"stage":"audit_syntax","to":["p4"],"focus":"check"}]'
        ),
    })
    body = _ok(res)
    tid = _extract_task_id(body)

    # No role row planted at create time (pool entry).
    assert await _active_role_owner(tid, "executor") is None
    # Status set to first stage.
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT status FROM tasks WHERE id = ?", (tid,))
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row)["status"] == "execute"

    # Coach picks an assignee via same-stage approve_stage.
    q = bus.subscribe()
    try:
        approve = _handler(coach, "approve_stage")
        res = await approve({
            "task_id": tid,
            "next_stage": "execute",
            "assignee": "p2",
            "note": "go",
        })
        _ok(res)
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    # Role row planted now.
    assert await _active_role_owner(tid, "executor") == "p2"

    # task_role_assigned fires; task_stage_changed does NOT (same stage).
    types = [e.get("type") for e in events]
    assert "task_role_assigned" in types
    assert "task_stage_changed" not in types

    # Wake fired.
    assert any(slot == "p2" for slot, _ in wakes)


async def test_empty_pool_first_stage_same_stage_succeeds(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty `to=[]` is the same code path: no plant, then Coach approves
    same stage."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")

    create = _handler(coach, "create_task")
    body = _ok(await create({
        "title": "empty-first demo",
        "description": "x",
        "trajectory": '[{"stage":"plan","to":[]},{"stage":"execute","to":[]}]',
    }))
    tid = _extract_task_id(body)
    assert await _active_role_owner(tid, "planner") is None

    res = await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "plan", "assignee": "p5", "note": "x",
    })
    _ok(res)
    assert await _active_role_owner(tid, "planner") == "p5"


# ---------------------------------------------------------------- already-planted

async def test_same_stage_with_active_role_row_rejected(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the first same-stage plant, a SECOND same-stage approve
    must be rejected — Coach should advance to the next stage, not
    re-plant the current one. The error names the next-stage path."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")

    body = _ok(await _handler(coach, "create_task")({
        "title": "double-same-stage demo",
        "description": "x",
        "trajectory": '[{"stage":"execute","to":["p2","p3"]}]',
    }))
    tid = _extract_task_id(body)

    # First same-stage plant succeeds.
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "execute", "assignee": "p2", "note": "go",
    }))
    # Second same-stage call is rejected.
    err = _err(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "execute", "assignee": "p3", "note": "x",
    }))
    assert "already in 'execute'" in err
    assert "executor" in err


# ---------------------------------------------------------------- single-name first stage

async def test_single_name_first_stage_unchanged(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: when the first-stage `to` is a single name, the role
    row plants at create time. Same-stage approve_stage must still be
    rejected because there's an active row (the create-time plant)."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")

    body = _ok(await _handler(coach, "create_task")({
        "title": "single-name demo",
        "description": "x",
        "trajectory": '[{"stage":"execute","to":["p2"]}]',
    }))
    tid = _extract_task_id(body)
    # Create-time plant.
    assert await _active_role_owner(tid, "executor") == "p2"

    # Same-stage call rejected (active row from create).
    err = _err(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "execute", "assignee": "p3", "note": "x",
    }))
    assert "already in 'execute'" in err


# ---------------------------------------------------------------- normal next-stage path

async def test_normal_next_stage_after_same_stage_plant(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a same-stage plant, the normal next-stage path
    (`execute → audit_syntax`) still works."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")

    body = _ok(await _handler(coach, "create_task")({
        "title": "normal-next demo",
        "description": "x",
        "trajectory": (
            '[{"stage":"execute","to":["p2","p3"]},'
            '{"stage":"audit_syntax","to":["p4"]}]'
        ),
    }))
    tid = _extract_task_id(body)

    # Same-stage first plant.
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "execute", "assignee": "p2", "note": "go",
    }))
    # Normal next-stage approval.
    q = bus.subscribe()
    try:
        _ok(await _handler(coach, "approve_stage")({
            "task_id": tid, "next_stage": "audit_syntax",
            "assignee": "p4", "note": "review",
        }))
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    types = [e.get("type") for e in events]
    assert "task_stage_changed" in types  # Real transition this time.
    assert "task_role_assigned" in types

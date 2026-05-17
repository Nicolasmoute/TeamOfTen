"""TruthGate-backed first-stage assignment behavior.

Backlog promotion enters `truthgate` without planting a Player role,
even when the first trajectory entry names a single candidate. The first
real assignment happens only after a TruthGate pass/override and an
explicit `coord_approve_stage` call.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

import server.agents as agents_mod
from server.db import configured_conn, init_db
from server.tools import build_coord_server


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok(result: dict[str, Any]) -> str:
    assert not result.get("is_error"), (
        f"tool returned error: {result.get('content')}"
    )
    return result["content"][0]["text"]


def _err(result: dict[str, Any]) -> str:
    assert result.get("is_error"), f"expected error, got {result}"
    return result["content"][0]["text"]


def _extract_task_id(body: str) -> str:
    m = re.search(r"t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}", body)
    assert m, f"no task id in body: {body}"
    return m.group(0)


def _extract_backlog_id(body: str) -> int:
    m = re.search(r"Backlog entry #(\d+)", body)
    assert m, f"no backlog id in body: {body}"
    return int(m.group(1))


async def _create_and_promote(coach: Any, args: dict[str, Any]) -> str:
    body = _ok(await _handler(coach, "create_task")(args))
    backlog_id = _extract_backlog_id(body)
    promoted = _ok(await _handler(coach, "triage_backlog")({
        "id": str(backlog_id),
        "action": "promote",
    }))
    return _extract_task_id(promoted)


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


async def _mark_truthgate_pass(task_id: str) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE tasks SET truthgate_verdict = 'truthgate_pass', "
            "truthgate_method = 'manual_record', truth_basis = '[]' "
            "WHERE id = ?",
            (task_id,),
        )
        await c.commit()
    finally:
        await c.close()


async def test_single_name_first_stage_does_not_plant_before_truthgate_exit(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-name first `to` remains advisory while in TruthGate."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")

    tid = await _create_and_promote(coach, {
        "title": "single-name demo",
        "description": "x",
        "trajectory": '[{"stage":"execute","to":["p2"]}]',
    })
    assert await _active_role_owner(tid, "executor") is None


async def test_same_stage_approve_rejected_when_role_active(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After post-gate dispatch, duplicate same-stage approve is rejected."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")

    tid = await _create_and_promote(coach, {
        "title": "double-same-stage demo",
        "description": "x",
        "trajectory": '[{"stage":"execute","to":["p2"]}]',
    })
    await _mark_truthgate_pass(tid)
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid,
        "next_stage": "execute",
        "assignee": "p2",
        "note": "dispatch",
    }))

    err = _err(await _handler(coach, "approve_stage")({
        "task_id": tid,
        "next_stage": "execute",
        "assignee": "p3",
        "note": "x",
    }))
    assert "already in 'execute'" in err


async def test_normal_next_stage_after_create(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: normal next-stage transition works after dispatch."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")

    tid = await _create_and_promote(coach, {
        "title": "normal-next demo",
        "description": "x",
        "trajectory": (
            '[{"stage":"execute","to":["p2"]},'
            '{"stage":"audit_syntax","to":["p4"]}]'
        ),
    })
    await _mark_truthgate_pass(tid)
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid,
        "next_stage": "execute",
        "assignee": "p2",
        "note": "dispatch",
    }))

    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid,
        "next_stage": "audit_syntax",
        "assignee": "p4",
        "note": "review",
    }))
    assert await _active_role_owner(tid, "auditor_syntax") == "p4"

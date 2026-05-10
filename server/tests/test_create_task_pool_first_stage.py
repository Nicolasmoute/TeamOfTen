"""v2.0.1 (2026-05-08) — same-stage approve_stage allowance is now
defensive-only.

The original premise of this file (pool/empty first-stage create →
follow-up same-stage approve_stage to plant the role row) is gone:
v2.0.1 rejects pool/empty first-stage at create time so the
two-step shape can't happen on the create path. Coverage of the
rejection lives in
[test_create_task_requires_single_first_stage.py](test_create_task_requires_single_first_stage.py).

What remains: the same-stage allowance in `coord_approve_stage` is
kept as defensive code for the (rare) edge where a `coord_set_task_trajectory`
rewrite inserts a stage with no active role row. We test the
single-name first-stage happy path here so the rejection logic is
exercised against a known-good trajectory.
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


async def test_single_name_first_stage_plants_role_at_create(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2.0.1: trajectory[0].to=['p2'] plants the executor role row at
    create time. tasks.owner is set; agents.current_task_id propagates."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")

    body = _ok(await _handler(coach, "create_task")({
        "title": "single-name demo",
        "description": "x",
        "trajectory": '[{"stage":"execute","to":["p2"]}]',
    }))
    tid = _extract_task_id(body)
    assert await _active_role_owner(tid, "executor") == "p2"


async def test_same_stage_approve_rejected_when_role_active(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Phase 1 same-stage allowance only fires when no active row
    exists at the target. After a single-name first-stage create
    plants the executor row, a same-stage approve_stage call must be
    rejected — the role is already filled."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")

    body = _ok(await _handler(coach, "create_task")({
        "title": "double-same-stage demo",
        "description": "x",
        "trajectory": '[{"stage":"execute","to":["p2"]}]',
    }))
    tid = _extract_task_id(body)

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
    """Regression: the normal next-stage transition path
    (`execute → audit_syntax`) still works after a single-name
    first-stage create."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")

    body = _ok(await _handler(coach, "create_task")({
        "title": "normal-next demo",
        "description": "x",
        "trajectory": (
            '[{"stage":"execute","to":["p2"]},'
            '{"stage":"audit_syntax","to":["p4"]}]'
        ),
    }))
    tid = _extract_task_id(body)

    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid,
        "next_stage": "audit_syntax",
        "assignee": "p4",
        "note": "review",
    }))
    assert await _active_role_owner(tid, "auditor_syntax") == "p4"

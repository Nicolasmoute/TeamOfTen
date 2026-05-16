from __future__ import annotations

import json
import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

import server.agents as agents_mod
from server.db import configured_conn, init_db
from server.main import app
from server.tools import build_coord_server


_TRAJECTORY = (
    '[{"stage":"execute","to":["p2"]},'
    '{"stage":"audit_syntax","to":[]},'
    '{"stage":"ship","to":[]}]'
)


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok(result: dict[str, Any]) -> str:
    assert not result.get("is_error"), result
    return result["content"][0]["text"]


def _err(result: dict[str, Any]) -> str:
    assert result.get("is_error"), result
    return result["content"][0]["text"]


def _extract_backlog_id(body: str) -> int:
    m = re.search(r"Backlog entry #(\d+)", body)
    assert m, body
    return int(m.group(1))


def _extract_task_id(body: str) -> str:
    m = re.search(r"t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}", body)
    assert m, body
    return m.group(0)


async def _promote_to_truthgate(monkeypatch: pytest.MonkeyPatch) -> str:
    wake_calls: list[tuple[str, str]] = []

    async def _wake(slot: str, prompt: str = "", **_: Any) -> bool:
        wake_calls.append((slot, prompt))
        return True

    monkeypatch.setattr(agents_mod, "maybe_wake_agent", _wake)
    coach = _server_for("coach")
    created = _ok(await _handler(coach, "create_task")({
        "title": "truthgate demo",
        "description": "prove promotion enters the gate",
        "trajectory": _TRAJECTORY,
    }))
    backlog_id = _extract_backlog_id(created)
    promoted = _ok(await _handler(coach, "triage_backlog")({
        "id": str(backlog_id),
        "action": "promote",
    }))
    assert "initial stage: truthgate" in promoted
    assert "No Player role was planted" in promoted
    assert wake_calls == []
    return _extract_task_id(promoted)


@pytest.mark.asyncio
async def test_truthgate_columns_and_status_exist(fresh_db: str) -> None:  # noqa: ARG001
    await init_db()
    c = await configured_conn()
    try:
        cols = {
            r[1]
            for r in await (await c.execute("PRAGMA table_info(tasks)")).fetchall()
        }
        for col in (
            "truthgate_verdict",
            "truth_basis",
            "truth_concerns",
            "truthgate_method",
            "truthgate_pending_proposal_id",
            "provisional",
            "closure_reference",
        ):
            assert col in cols
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, created_by) "
            "VALUES ('t-2026-05-16-11111111', 'misc', 'gate', 'truthgate', 'coach')"
        )
        await c.commit()
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_backlog_promotion_enters_truthgate_without_role_or_wake(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
) -> None:
    await init_db()
    task_id = await _promote_to_truthgate(monkeypatch)
    c = await configured_conn()
    try:
        task = dict(await (await c.execute(
            "SELECT status, owner, trajectory FROM tasks WHERE id = ?",
            (task_id,),
        )).fetchone())
        roles = await (await c.execute(
            "SELECT * FROM task_role_assignments WHERE task_id = ?",
            (task_id,),
        )).fetchall()
    finally:
        await c.close()
    assert task["status"] == "truthgate"
    assert task["owner"] is None
    assert [r["stage"] for r in json.loads(task["trajectory"])] == [
        "execute", "audit_syntax", "ship",
    ]
    assert roles == []


@pytest.mark.asyncio
async def test_truthgate_tasks_are_not_player_assignments(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
) -> None:
    await init_db()
    task_id = await _promote_to_truthgate(monkeypatch)
    p2 = _server_for("p2")
    text = _ok(await _handler(p2, "my_assignments")({}))
    assert "Executor: (none" in text
    assert task_id not in text


@pytest.mark.asyncio
async def test_truthgate_exit_requires_pass_or_override(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001
) -> None:
    await init_db()
    task_id = await _promote_to_truthgate(monkeypatch)
    coach = _server_for("coach")
    approve = _handler(coach, "approve_stage")

    err = _err(await approve({
        "task_id": task_id,
        "next_stage": "execute",
        "assignee": "p2",
        "note": "go",
    }))
    assert "requires a TruthGate pass or override" in err

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

    ok = _ok(await approve({
        "task_id": task_id,
        "next_stage": "execute",
        "assignee": "p2",
        "note": "go",
    }))
    assert "truthgate → execute" in ok


def test_board_serializes_truthgate_bucket(fresh_db: str) -> None:  # noqa: ARG001
    import asyncio

    async def seed() -> None:
        await init_db()
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO tasks "
                "(id, project_id, title, status, created_by, truthgate_verdict) "
                "VALUES ('t-2026-05-16-22222222', 'misc', 'gate', "
                "'truthgate', 'coach', NULL)"
            )
            await c.commit()
        finally:
            await c.close()

    asyncio.run(seed())
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/tasks/board")
    assert r.status_code == 200, r.text
    board = r.json()["board"]
    assert "truthgate" in board
    assert any(t["id"] == "t-2026-05-16-22222222" for t in board["truthgate"])

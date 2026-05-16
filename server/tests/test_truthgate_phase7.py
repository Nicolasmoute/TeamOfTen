from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.db import configured_conn, init_db, set_active_project
from server.events import bus
from server.main import app
from server.tools import build_coord_server


TASK_ID = "t-2026-05-16-77777777"
ROLLBACK_ID = "t-2026-05-16-88888888"


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


async def _seed_task(
    task_id: str = TASK_ID,
    *,
    status: str = "ship",
    provisional: bool = True,
    closure_reference: str | None = None,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, truthgate_verdict, truthgate_method, "
            "provisional, closure_reference) "
            "VALUES (?, 'misc', 'provisional gate', ?, 'p2', 'coach', "
            "'[{\"stage\":\"execute\",\"to\":[\"p2\"]},{\"stage\":\"ship\",\"to\":[]}]', "
            "'truthgate_emergency_override', 'emergency_override', ?, ?)",
            (task_id, status, 1 if provisional else 0, closure_reference),
        )
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, claimed_at) "
            "VALUES (?, 'shipper', '[]', 'p2', "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (task_id,),
        )
        await c.execute(
            "UPDATE agents SET current_task_id = ? WHERE id = 'p2'",
            (task_id,),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_truth_proposal(status: str = "pending") -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO file_write_proposals "
            "(project_id, proposer_id, scope, path, proposed_content, "
            "summary, status, originating_task_id, metadata_json) "
            "VALUES ('misc', 'coach', 'truth', 'specs.md', '# specs\n', "
            "'truth closure', ?, ?, '{}')",
            (status, TASK_ID),
        )
        await c.commit()
        return int(cur.lastrowid)
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_emergency_override_marks_task_provisional(
    fresh_db: str,
) -> None:
    await init_db()
    coach = _server_for("coach")
    created = _ok(await _handler(coach, "create_task")({
        "title": "emergency task",
        "trajectory": '[{"stage":"execute","to":["p2"]}]',
    }))
    backlog_id = created.split("Backlog entry #", 1)[1].split()[0]
    promoted = _ok(await _handler(coach, "triage_backlog")({
        "id": backlog_id,
        "action": "promote",
    }))
    task_id = promoted.split("task ", 1)[1].split()[0]

    _ok(await _handler(coach, "record_truthgate_override")({
        "task_id": task_id,
        "kind": "emergency_override",
        "rationale": "human authorized emergency work",
    }))

    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT provisional, closure_reference FROM tasks WHERE id = ?",
            (task_id,),
        )).fetchone())
    finally:
        await c.close()
    assert row["provisional"] == 1
    assert row["closure_reference"] is None


@pytest.mark.asyncio
async def test_record_provisional_closure_rejects_player_and_invalid_refs(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task()

    player = _server_for("p2")
    err = _err(await _handler(player, "record_provisional_closure")({
        "task_id": TASK_ID,
        "closure_reference": "none_needed:done",
    }))
    assert "Coach-only" in err

    coach = _server_for("coach")
    err = _err(await _handler(coach, "record_provisional_closure")({
        "task_id": TASK_ID,
        "closure_reference": "none_needed:",
    }))
    assert "non-empty rationale" in err

    err = _err(await _handler(coach, "record_provisional_closure")({
        "task_id": TASK_ID,
        "closure_reference": "rollback:t-2026-05-16-missing0",
    }))
    assert "unknown task" in err


@pytest.mark.asyncio
async def test_record_provisional_closure_persists_and_emits_event(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task()
    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        coach = _server_for("coach")
        text = _ok(await _handler(coach, "record_provisional_closure")({
            "task_id": TASK_ID,
            "closure_reference": "none_needed:truth already covered elsewhere",
        }))
        assert "Recorded provisional closure" in text
        await asyncio.sleep(0)
        while True:
            try:
                captured.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    assert any(e.get("type") == "task_provisional_closure_recorded" for e in captured)
    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT closure_reference FROM tasks WHERE id = ?",
            (TASK_ID,),
        )).fetchone())
    finally:
        await c.close()
    assert row["closure_reference"].startswith("none_needed:")


@pytest.mark.asyncio
async def test_provisional_coord_archive_task_requires_closure(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task()
    coach = _server_for("coach")
    err = _err(await _handler(coach, "archive_task")({
        "task_id": TASK_ID,
        "summary": "Delivered the emergency work.",
    }))
    assert "provisional task cannot be delivered to archive" in err


@pytest.mark.asyncio
async def test_provisional_archive_allows_none_needed_closure(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(closure_reference="none_needed:human accepted no truth change")
    coach = _server_for("coach")
    text = _ok(await _handler(coach, "archive_task")({
        "task_id": TASK_ID,
        "summary": "Delivered after provisional closure.",
    }))
    assert "Archived" in text


@pytest.mark.asyncio
async def test_amendment_closure_must_be_approved_before_delivered_archive(
    fresh_db: str,
) -> None:
    await init_db()
    proposal_id = await _seed_truth_proposal(status="pending")
    await _seed_task(closure_reference=f"amendment:{proposal_id}")
    coach = _server_for("coach")

    err = _err(await _handler(coach, "archive_task")({
        "task_id": TASK_ID,
        "summary": "Delivered after amendment.",
    }))
    assert "must be approved" in err

    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE file_write_proposals SET status = 'approved' WHERE id = ?",
            (proposal_id,),
        )
        await c.commit()
    finally:
        await c.close()

    text = _ok(await _handler(coach, "archive_task")({
        "task_id": TASK_ID,
        "summary": "Delivered after approved amendment.",
    }))
    assert "Archived" in text


@pytest.fixture
def client(fresh_db: str) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
async def _init_project(fresh_db: str) -> None:
    await init_db()
    await set_active_project("misc")


def test_human_cancel_remains_available_for_provisional_without_closure(
    client: TestClient,
) -> None:
    asyncio.run(_seed_task())
    r = client.post(f"/api/tasks/{TASK_ID}/cancel")
    assert r.status_code == 200
    assert r.json()["old_status"] == "ship"


def test_human_approve_archive_blocks_provisional_without_closure(
    client: TestClient,
) -> None:
    asyncio.run(_seed_task())
    r = client.post(
        f"/api/tasks/{TASK_ID}/approve_stage",
        json={"next_stage": "archive", "note": "close"},
    )
    assert r.status_code == 400
    assert "provisional task cannot be delivered to archive" in r.json()["detail"]

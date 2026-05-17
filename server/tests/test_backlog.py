"""Tests for the Backlog feature (Docs/kanban-specs-v2.md §4.0).

Coverage:
  - Schema: backlog_tasks table exists with correct columns.
  - coord_propose_task: inserts row, emits event, returns id.
  - coord_triage_backlog promote: creates task row, archives backlog
    entry, emits correct events.
  - coord_triage_backlog reject: archives with reason, emits event.
  - Human-rejection notification: human-proposed reject inserts message.
  - Coordination block: present / absent / capped at 5 / oldest-first.
  - POST /api/backlog: happy path, missing title → 400.
  - GET /api/backlog: returns pending items; status filter works.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from server.db import configured_conn, init_db
from server.events import bus


# ---------------------------------------------------------------- helpers


async def _ensure_project(pid: str = "misc") -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)",
            (pid, pid),
        )
        await c.execute(
            "UPDATE team_config SET value = ? WHERE key = 'active_project'",
            (pid,),
        )
        rows = await (
            await c.execute(
                "SELECT COUNT(*) FROM team_config WHERE key = 'active_project'"
            )
        ).fetchone()
        if dict(rows)["COUNT(*)"] == 0:
            await c.execute(
                "INSERT INTO team_config (key, value) VALUES ('active_project', ?)",
                (pid,),
            )
        await c.commit()
    finally:
        await c.close()


async def _insert_backlog(
    title: str,
    proposed_by: str = "p1",
    proposed_at: str | None = None,
    status: str = "pending",
) -> int:
    now = proposed_at or datetime.now(timezone.utc).isoformat()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO backlog_tasks (title, proposed_by, proposed_at, status) "
            "VALUES (?, ?, ?, ?)",
            (title, proposed_by, now, status),
        )
        row_id = cur.lastrowid
        await c.commit()
    finally:
        await c.close()
    return row_id  # type: ignore[return-value]


async def _insert_task(
    title: str,
    task_id: str = "t-2026-05-15-00000001",
    status: str = "execute",
) -> str:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) "
            "VALUES (?, 'misc', ?, ?, 'p1', 'coach', ?)",
            (
                task_id,
                title,
                status,
                '[{"stage":"execute","to":["p1"]}]',
            ),
        )
        await c.commit()
    finally:
        await c.close()
    return task_id


async def _get_backlog(row_id: int) -> dict[str, Any] | None:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT * FROM backlog_tasks WHERE id = ?", (row_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await c.close()


def _handler(slot: str, tool_name: str):
    """Return the raw async handler for a coord tool."""
    from server.tools import build_coord_server
    server = build_coord_server(slot, include_proxy_metadata=True)
    return server["_handlers"][tool_name]


def _drain(q: Any) -> list[dict]:
    out: list[dict] = []
    while True:
        try:
            out.append(q.get_nowait())
        except Exception:
            break
    return out


def _text(result: dict) -> str:
    """Extract text from MCP tool result {content: [{type, text}]}."""
    parts = result.get("content") or []
    return " ".join(p.get("text", "") for p in parts if p.get("type") == "text")


def _is_ok(result: dict) -> bool:
    return not result.get("is_error", False)


def _is_err(result: dict) -> bool:
    return bool(result.get("is_error", False))


# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
async def _isolate(fresh_db: str) -> None:  # noqa: ARG001
    await init_db()
    await _ensure_project()


# ---------------------------------------------------------------- schema


@pytest.mark.asyncio
async def test_backlog_table_exists() -> None:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='backlog_tasks'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is not None, "backlog_tasks table missing"


@pytest.mark.asyncio
async def test_backlog_table_columns() -> None:
    c = await configured_conn()
    try:
        cur = await c.execute("PRAGMA table_info(backlog_tasks)")
        cols = {dict(r)["name"] for r in await cur.fetchall()}
    finally:
        await c.close()
    expected = {
        "id", "title", "description", "proposed_by", "proposed_at",
        "status", "reject_reason", "promoted_task_id",
    }
    assert expected <= cols, f"Missing columns: {expected - cols}"


# ---------------------------------------------------------------- coord_propose_task


@pytest.mark.asyncio
async def test_propose_task_inserts_row() -> None:
    q = bus.subscribe()
    try:
        fn = _handler("p1", "coord_propose_task")
        result = await fn({"title": "Add dark mode"})
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    assert _is_ok(result), _text(result)
    assert "Backlog entry" in _text(result)

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT * FROM backlog_tasks WHERE title = 'Add dark mode'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is not None
    d = dict(row)
    assert d["proposed_by"] == "p1"
    assert d["status"] == "pending"

    proposed_events = [e for e in events if e.get("type") == "backlog_task_proposed"]
    assert len(proposed_events) == 1
    assert proposed_events[0]["title"] == "Add dark mode"


@pytest.mark.asyncio
async def test_propose_task_requires_title() -> None:
    fn = _handler("coach", "coord_propose_task")
    result = await fn({"title": ""})
    assert _is_err(result)

# ---------------------------------------------------------------- coord_list_tasks / coord_list_backlog


@pytest.mark.asyncio
async def test_list_tasks_default_include_backlog_includes_both_kinds() -> None:
    # Regression: the no-args path must still include pending backlog rows.
    await _insert_task("Implement search")
    backlog_id = await _insert_backlog("Plan roadmap")

    fn = _handler("coach", "coord_list_tasks")
    result = await fn({})
    text = _text(result)

    assert _is_ok(result), text
    assert "kind=task" in text
    assert "kind=backlog" in text
    assert f"#{backlog_id}" in text


@pytest.mark.asyncio
async def test_list_tasks_pending_returns_backlog_only() -> None:
    await _insert_task("Implement search")
    await _insert_backlog("Plan roadmap")

    fn = _handler("coach", "coord_list_tasks")
    result = await fn({"status": "pending"})
    text = _text(result)

    assert _is_ok(result), text
    assert "kind=backlog" in text
    assert "kind=task" not in text


@pytest.mark.asyncio
async def test_list_tasks_task_stage_stays_task_only() -> None:
    await _insert_task("Implement search")
    await _insert_backlog("Plan roadmap")

    fn = _handler("coach", "coord_list_tasks")
    result = await fn({"status": "execute", "include_backlog": "true"})
    text = _text(result)

    assert _is_ok(result), text
    assert "kind=task" in text
    assert "kind=backlog" not in text


@pytest.mark.asyncio
async def test_list_backlog_shim_keeps_backlog_shape() -> None:
    backlog_id = await _insert_backlog("Plan roadmap")

    fn = _handler("coach", "coord_list_backlog")
    result = await fn({"status": "pending", "limit": "10"})
    text = _text(result)

    assert _is_ok(result), text
    assert f"#{backlog_id}" in text
    assert "kind=backlog" in text
    assert "kind=task" not in text


# ---------------------------------------------------------------- coord_triage_backlog


@pytest.mark.asyncio
async def test_triage_backlog_player_rejected() -> None:
    """Players cannot call coord_triage_backlog."""
    backlog_id = await _insert_backlog("Refactor auth")
    fn = _handler("p3", "coord_triage_backlog")
    result = await fn(
        {"id": str(backlog_id), "action": "reject", "reason": "not now"}
    )
    assert _is_err(result)
    assert "Coach-only" in _text(result)


@pytest.mark.asyncio
async def test_triage_backlog_reject() -> None:
    q = bus.subscribe()
    backlog_id = await _insert_backlog("Migrate to Postgres")
    try:
        fn = _handler("coach", "coord_triage_backlog")
        result = await fn({
            "id": str(backlog_id),
            "action": "reject",
            "reason": "out of scope",
        })
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    assert _is_ok(result), _text(result)
    row = await _get_backlog(backlog_id)
    assert row is not None
    assert row["status"] == "rejected"
    assert row["reject_reason"] == "out of scope"

    rejected = [e for e in events if e.get("type") == "backlog_task_rejected"]
    assert len(rejected) == 1
    assert rejected[0]["id"] == backlog_id


@pytest.mark.asyncio
async def test_triage_backlog_reject_human_inserts_message() -> None:
    """When proposed_by='human', reject inserts a message row."""
    backlog_id = await _insert_backlog("Build iOS app", proposed_by="human")
    fn = _handler("coach", "coord_triage_backlog")
    await fn({
        "id": str(backlog_id),
        "action": "reject",
        "reason": "not in scope for v1",
    })

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT * FROM messages WHERE subject LIKE '%Build iOS app%'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is not None, "Expected rejection message for human-proposed entry"
    d = dict(row)
    assert "not in scope" in d["body"]


@pytest.mark.asyncio
async def test_triage_backlog_reject_agent_no_message() -> None:
    """Agent-proposed rejections do NOT insert a message."""
    backlog_id = await _insert_backlog("Refactor X", proposed_by="p2")
    fn = _handler("coach", "coord_triage_backlog")
    await fn({
        "id": str(backlog_id),
        "action": "reject",
        "reason": "too risky",
    })

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE subject LIKE '%Refactor X%'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row)["n"] == 0


@pytest.mark.asyncio
async def test_triage_backlog_promote() -> None:
    q = bus.subscribe()
    backlog_id = await _insert_backlog("Ship v2")
    trajectory = [{"stage": "execute", "to": ["p1"]}]
    try:
        fn = _handler("coach", "coord_triage_backlog")
        result = await fn({
            "id": str(backlog_id),
            "action": "promote",
            "trajectory": trajectory,
        })
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    assert _is_ok(result), _text(result)

    # Backlog entry archived
    row = await _get_backlog(backlog_id)
    assert row is not None
    assert row["status"] == "promoted"
    promoted_task_id = row["promoted_task_id"]
    assert promoted_task_id

    # Task created
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT * FROM tasks WHERE id = ?", (promoted_task_id,)
        )
        task = await cur.fetchone()
    finally:
        await c.close()
    assert task is not None
    d = dict(task)
    assert d["title"] == "Ship v2"
    assert d["status"] == "truthgate"

    promoted = [e for e in events if e.get("type") == "backlog_task_promoted"]
    assert len(promoted) == 1
    assert promoted[0]["task_id"] == promoted_task_id
    created = [e for e in events if e.get("type") == "task_created"]
    assert len(created) == 1
    assert created[0]["task_id"] == promoted_task_id
    assert created[0]["tracking_reason"] == "backlog"
    stage_changed = [e for e in events if e.get("type") == "task_stage_changed"]
    assert len(stage_changed) == 1
    assert stage_changed[0]["task_id"] == promoted_task_id
    assert stage_changed[0]["from"] is None
    assert stage_changed[0]["to"] == "truthgate"
    assert stage_changed[0]["reason"] == "backlog_promoted"
    role_assigned = [e for e in events if e.get("type") == "task_role_assigned"]
    assert role_assigned == []


@pytest.mark.asyncio
async def test_triage_backlog_promote_modified_title() -> None:
    backlog_id = await _insert_backlog("vague idea")
    fn = _handler("coach", "coord_triage_backlog")
    await fn({
        "id": str(backlog_id),
        "action": "promote",
        "modified_title": "Implement feature X per spec",
        "trajectory": [{"stage": "execute", "to": ["p2"]}],
    })

    row = await _get_backlog(backlog_id)
    task_id = row["promoted_task_id"]
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT title FROM tasks WHERE id = ?", (task_id,))
        task = await cur.fetchone()
    finally:
        await c.close()
    assert dict(task)["title"] == "Implement feature X per spec"


@pytest.mark.asyncio
async def test_triage_backlog_promote_requires_trajectory() -> None:
    backlog_id = await _insert_backlog("No trajectory idea")
    fn = _handler("coach", "coord_triage_backlog")
    result = await fn({
        "id": str(backlog_id),
        "action": "promote",
    })
    assert _is_err(result)
    assert "trajectory" in _text(result).lower()


@pytest.mark.asyncio
async def test_triage_backlog_already_resolved() -> None:
    backlog_id = await _insert_backlog(
        "Already rejected", status="rejected"
    )
    fn = _handler("coach", "coord_triage_backlog")
    result = await fn({
        "id": str(backlog_id),
        "action": "reject",
        "reason": "again",
    })
    assert _is_err(result)
    assert "already" in _text(result).lower()


# ---------------------------------------------------------------- coordination block


@pytest.mark.asyncio
async def test_coordination_block_empty_when_no_backlog() -> None:
    from server.agents import _build_coach_coordination_block

    block = await _build_coach_coordination_block()
    assert "## Backlog" not in block


@pytest.mark.asyncio
async def test_coordination_block_present_when_pending() -> None:
    from server.agents import _build_coach_coordination_block

    await _insert_backlog("Urgent idea", proposed_by="p1")
    block = await _build_coach_coordination_block()
    assert "## Backlog" in block
    assert "Urgent idea" in block


@pytest.mark.asyncio
async def test_coordination_block_capped_at_five() -> None:
    from server.agents import _build_coach_coordination_block

    for i in range(8):
        await _insert_backlog(f"Idea {i}", proposed_by="p1")
    block = await _build_coach_coordination_block()
    # At most 5 items listed
    assert block.count("[#") <= 5


@pytest.mark.asyncio
async def test_coordination_block_oldest_first() -> None:
    from server.agents import _build_coach_coordination_block

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(3):
        t = (base + timedelta(hours=i)).isoformat()
        await _insert_backlog(f"Idea at {i}h", proposed_at=t)

    block = await _build_coach_coordination_block()
    pos_0 = block.find("Idea at 0h")
    pos_2 = block.find("Idea at 2h")
    assert pos_0 < pos_2, "oldest item should appear before newest"


@pytest.mark.asyncio
async def test_coordination_block_excludes_resolved() -> None:
    from server.agents import _build_coach_coordination_block

    await _insert_backlog("Done idea", proposed_by="p1", status="promoted")
    block = await _build_coach_coordination_block()
    assert "Done idea" not in block


# ---------------------------------------------------------------- HTTP endpoints


@pytest.mark.asyncio
async def test_post_backlog_happy_path(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/backlog",
            json={"title": "Fix login bug"},
        )
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "pending"
    assert d["title"] == "Fix login bug"
    assert isinstance(d["id"], int)


@pytest.mark.asyncio
async def test_post_backlog_empty_title(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/backlog", json={"title": "  "})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_get_backlog_default_pending(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    await _insert_backlog("Idea A", proposed_by="human")
    await _insert_backlog("Idea B", proposed_by="p2", status="promoted")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/backlog")
    assert r.status_code == 200
    items = r.json()["backlog"]
    titles = [i["title"] for i in items]
    assert "Idea A" in titles
    assert "Idea B" not in titles  # promoted, not pending


@pytest.mark.asyncio
async def test_get_backlog_all_status(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    await _insert_backlog("Pending one", proposed_by="p1")
    await _insert_backlog("Promoted one", proposed_by="p2", status="promoted")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/backlog?status=all")
    assert r.status_code == 200
    titles = [i["title"] for i in r.json()["backlog"]]
    assert "Pending one" in titles
    assert "Promoted one" in titles


@pytest.mark.asyncio
async def test_get_backlog_invalid_status(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/backlog?status=bogus")
    assert r.status_code == 400


# ---------------------------------------------------------------- PATCH /api/backlog/{id}


@pytest.mark.asyncio
async def test_patch_backlog_happy(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("Original title", proposed_by="p1")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.patch(
            f"/api/backlog/{row_id}",
            json={"title": "Renamed title"},
        )
    assert r.status_code == 200
    d = r.json()
    assert d["id"] == row_id
    assert d["title"] == "Renamed title"

    row = await _get_backlog(row_id)
    assert row is not None
    assert row["title"] == "Renamed title"


@pytest.mark.asyncio
async def test_patch_backlog_404(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.patch("/api/backlog/99999", json={"title": "X"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_backlog_409_promoted(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("Promoted idea", proposed_by="p1", status="promoted")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.patch(
            f"/api/backlog/{row_id}",
            json={"title": "New name"},
        )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_patch_backlog_empty_title(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("Some idea", proposed_by="p1")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.patch(f"/api/backlog/{row_id}", json={"title": "  "})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_patch_backlog_emits_event(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("Event idea", proposed_by="p1")
    q = bus.subscribe()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.patch(
                f"/api/backlog/{row_id}",
                json={"title": "Event idea renamed"},
            )
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    updated = [e for e in events if e.get("type") == "backlog_entry_updated"]
    assert len(updated) == 1
    assert updated[0]["id"] == row_id
    assert updated[0]["old_title"] == "Event idea"
    assert updated[0]["new_title"] == "Event idea renamed"


# ---------------------------------------------------------------- DELETE /api/backlog/{id}


@pytest.mark.asyncio
async def test_delete_backlog_happy(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("To be deleted", proposed_by="p1")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.delete(f"/api/backlog/{row_id}")
    assert r.status_code == 200
    d = r.json()
    assert d["id"] == row_id
    assert d["deleted"] is True

    row = await _get_backlog(row_id)
    assert row is None


@pytest.mark.asyncio
async def test_delete_backlog_404(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.delete("/api/backlog/99999")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_backlog_409_rejected(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("Rejected idea", proposed_by="p1", status="rejected")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.delete(f"/api/backlog/{row_id}")
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_delete_backlog_emits_event(fresh_db: str) -> None:  # noqa: ARG001
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("Delete event idea", proposed_by="p1")
    q = bus.subscribe()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.delete(f"/api/backlog/{row_id}")
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    deleted = [e for e in events if e.get("type") == "backlog_entry_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["id"] == row_id
    assert deleted[0]["title"] == "Delete event idea"


# ---------------------------------------------------------------- Auth gate (token required)


def test_patch_backlog_auth_gate(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH /api/backlog/{id} requires a valid Bearer token when HARNESS_TOKEN is set."""
    import asyncio
    from fastapi.testclient import TestClient
    asyncio.run(init_db())
    row_id = asyncio.run(_insert_backlog("Auth test entry", proposed_by="p1"))
    monkeypatch.setattr("server.main.HARNESS_TOKEN", "secret123")
    from server.main import app
    client = TestClient(app, raise_server_exceptions=False)
    r = client.patch(f"/api/backlog/{row_id}", json={"title": "New"})
    assert r.status_code == 401
    r = client.patch(
        f"/api/backlog/{row_id}",
        json={"title": "New"},
        headers={"Authorization": "Bearer wrongtoken"},
    )
    assert r.status_code == 403


def test_delete_backlog_auth_gate(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELETE /api/backlog/{id} requires a valid Bearer token when HARNESS_TOKEN is set."""
    import asyncio
    from fastapi.testclient import TestClient
    asyncio.run(init_db())
    row_id = asyncio.run(_insert_backlog("Auth delete entry", proposed_by="p1"))
    monkeypatch.setattr("server.main.HARNESS_TOKEN", "secret123")
    from server.main import app
    client = TestClient(app, raise_server_exceptions=False)
    r = client.delete(f"/api/backlog/{row_id}")
    assert r.status_code == 401
    r = client.delete(
        f"/api/backlog/{row_id}",
        headers={"Authorization": "Bearer wrongtoken"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------- description field


@pytest.mark.asyncio
async def test_post_backlog_with_description(fresh_db: str) -> None:  # noqa: ARG001
    """POST /api/backlog accepts description; GET returns it."""
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/backlog",
            json={"title": "Add OAuth", "description": "Use Google OAuth for SSO"},
        )
    assert r.status_code == 200
    d = r.json()
    assert d["description"] == "Use Google OAuth for SSO"
    assert d["status"] == "pending"

    # GET round-trip
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/backlog")
    items = {i["title"]: i for i in r.json()["backlog"]}
    assert items["Add OAuth"]["description"] == "Use Google OAuth for SSO"


@pytest.mark.asyncio
async def test_patch_backlog_description_only(fresh_db: str) -> None:  # noqa: ARG001
    """PATCH with description only (no title) updates the description field."""
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("Idea without desc", proposed_by="p1")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.patch(
            f"/api/backlog/{row_id}",
            json={"description": "Now it has context"},
        )
    assert r.status_code == 200
    d = r.json()
    assert d["title"] == "Idea without desc"  # title unchanged
    assert d["description"] == "Now it has context"

    row = await _get_backlog(row_id)
    assert row is not None
    assert row["description"] == "Now it has context"


@pytest.mark.asyncio
async def test_post_backlog_description_length_cap(fresh_db: str) -> None:  # noqa: ARG001
    """POST with description > 8000 chars returns 400."""
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    long_desc = "x" * 8001
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/backlog",
            json={"title": "Big idea", "description": long_desc},
        )
    assert r.status_code == 400
    assert "8000" in r.json()["detail"]


@pytest.mark.asyncio
async def test_patch_backlog_description_length_cap(fresh_db: str) -> None:  # noqa: ARG001
    """PATCH with description > 8000 chars returns 400."""
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("Idea for cap test", proposed_by="p1")
    long_desc = "y" * 8001
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.patch(
            f"/api/backlog/{row_id}",
            json={"description": long_desc},
        )
    assert r.status_code == 400
    assert "8000" in r.json()["detail"]


@pytest.mark.asyncio
async def test_get_backlog_returns_description(fresh_db: str) -> None:  # noqa: ARG001
    """GET /api/backlog returns the description column for each entry."""
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    # insert via helper (no description)
    row_id_no_desc = await _insert_backlog("No desc idea", proposed_by="p1")
    # insert with description via POST
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/backlog",
            json={"title": "With desc idea", "description": "details here"},
        )
        r = await client.get("/api/backlog")
    items = {i["title"]: i for i in r.json()["backlog"]}
    assert items["No desc idea"].get("description") is None
    assert items["With desc idea"]["description"] == "details here"


@pytest.mark.asyncio
async def test_patch_backlog_clear_description(fresh_db: str) -> None:  # noqa: ARG001
    """PATCH with description='' (empty string) clears description to NULL."""
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("Idea with desc", proposed_by="p1")
    # First add a description
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.patch(
            f"/api/backlog/{row_id}",
            json={"description": "Some context"},
        )
        # Now clear it
        r = await client.patch(
            f"/api/backlog/{row_id}",
            json={"description": ""},
        )
    assert r.status_code == 200
    assert r.json()["description"] is None

    row = await _get_backlog(row_id)
    assert row is not None
    assert row["description"] is None


# ---------------------------------------------------------------- priority field


@pytest.mark.asyncio
async def test_post_backlog_with_priority(fresh_db: str) -> None:
    """POST /api/backlog accepts priority; response and DB row carry it."""
    await init_db()
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/backlog",
            json={"title": "Urgent thing", "priority": "urgent"},
        )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["priority"] == "urgent"
    row = await _get_backlog(d["id"])
    assert row is not None
    assert row["priority"] == "urgent"


@pytest.mark.asyncio
async def test_post_backlog_default_priority(fresh_db: str) -> None:
    """POST /api/backlog defaults to normal when priority is omitted."""
    await init_db()
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/backlog", json={"title": "Some idea"})
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == "normal"


@pytest.mark.asyncio
async def test_post_backlog_invalid_priority(fresh_db: str) -> None:
    """POST /api/backlog rejects unknown priority values with 400."""
    await init_db()
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/backlog", json={"title": "x", "priority": "critical"}
        )
    assert r.status_code == 400


# ---------------------------------------------------------------- PATCH priority


@pytest.mark.asyncio
async def test_patch_backlog_priority_only(fresh_db: str) -> None:  # noqa: ARG001
    """PATCH /api/backlog/{id} with only priority updates priority, returns it."""
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("Low priority idea", proposed_by="human")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.patch(
            f"/api/backlog/{row_id}",
            json={"priority": "urgent"},
        )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["priority"] == "urgent"
    row = await _get_backlog(row_id)
    assert row is not None
    assert row["priority"] == "urgent"


@pytest.mark.asyncio
async def test_patch_backlog_priority_with_title(fresh_db: str) -> None:  # noqa: ARG001
    """PATCH can update priority and title together."""
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("Old title", proposed_by="p3")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.patch(
            f"/api/backlog/{row_id}",
            json={"title": "New title", "priority": "high"},
        )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["title"] == "New title"
    assert d["priority"] == "high"
    row = await _get_backlog(row_id)
    assert row is not None
    assert row["title"] == "New title"
    assert row["priority"] == "high"


@pytest.mark.asyncio
async def test_patch_backlog_invalid_priority_rejected(fresh_db: str) -> None:  # noqa: ARG001
    """PATCH with an unknown priority value returns 400."""
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("Some idea", proposed_by="p1")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.patch(
            f"/api/backlog/{row_id}",
            json={"priority": "critical"},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_patch_backlog_no_fields_rejected(fresh_db: str) -> None:  # noqa: ARG001
    """PATCH with no recognized fields returns 400."""
    from httpx import ASGITransport, AsyncClient
    from server.main import app

    row_id = await _insert_backlog("Some idea", proposed_by="p1")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.patch(f"/api/backlog/{row_id}", json={})
    assert r.status_code == 400

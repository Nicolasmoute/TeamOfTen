"""Tests for the kanban HTTP endpoints (Docs/kanban-specs.md §7).

Endpoints under test:
  - GET /api/tasks/board (active 5 buckets)
  - GET /api/tasks/archive (paginated)
  - GET /api/tasks/{id}/assignments (full role history)
  - POST /api/tasks/{id}/stage
  - POST /api/tasks/{id}/complexity
  - POST /api/tasks/{id}/blocked
  - POST /api/tasks/{id}/spec
  - POST /api/tasks/{id}/assign

Uses FastAPI's TestClient outside `with` so lifespan (scheduler +
telegram + kanban subscriber) doesn't run; the endpoints don't depend
on those background tasks for their own correctness.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from server.db import configured_conn


@pytest.fixture
def client(fresh_db: str) -> TestClient:
    from server.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
async def _init_db_for_api(fresh_db: str) -> None:
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")


async def _seed(
    *,
    task_id: str,
    title: str = "demo",
    status: str = "plan",
    complexity: str = "standard",
    priority: str = "normal",
    owner: str | None = None,
    archived_at: str | None = None,
    cancelled_at: str | None = None,
    spec_path: str | None = None,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, complexity, priority, archived_at, cancelled_at, "
            "spec_path) "
            "VALUES (?, 'misc', ?, ?, ?, 'human', ?, ?, ?, ?, ?)",
            (task_id, title, status, owner, complexity, priority,
             archived_at, cancelled_at, spec_path),
        )
        await c.commit()
    finally:
        await c.close()


# ----------------------------------------------------------------- /board

def test_board_groups_by_stage(client: TestClient) -> None:
    """Tasks land in their kanban-stage bucket; archived tasks
    are excluded from the active board response."""
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa", status="plan"))
    asyncio.run(_seed(task_id="t-2026-05-03-bbbbbbbb", status="execute", owner="p3"))
    asyncio.run(_seed(
        task_id="t-2026-05-03-cccccccc",
        status="archive",
        archived_at="2026-05-01T00:00:00Z",
    ))
    r = client.get("/api/tasks/board")
    assert r.status_code == 200
    board = r.json()["board"]
    assert {"plan", "execute", "audit_syntax", "audit_semantics", "ship"} == set(board.keys())
    assert any(t["id"] == "t-2026-05-03-aaaaaaaa" for t in board["plan"])
    assert any(t["id"] == "t-2026-05-03-bbbbbbbb" for t in board["execute"])
    # Archived task is excluded.
    all_active_ids = [t["id"] for stage_tasks in board.values() for t in stage_tasks]
    assert "t-2026-05-03-cccccccc" not in all_active_ids


def test_board_priority_sort(client: TestClient) -> None:
    """Within a column, urgent floats above normal which floats above low."""
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-11111111", status="plan", priority="low"))
    asyncio.run(_seed(task_id="t-2026-05-03-22222222", status="plan", priority="urgent"))
    asyncio.run(_seed(task_id="t-2026-05-03-33333333", status="plan", priority="normal"))
    r = client.get("/api/tasks/board")
    plan_ids = [t["id"] for t in r.json()["board"]["plan"]]
    assert plan_ids[0] == "t-2026-05-03-22222222"  # urgent first
    assert plan_ids[-1] == "t-2026-05-03-11111111"  # low last


# ----------------------------------------------------------------- /archive

def test_archive_returns_only_archived(client: TestClient) -> None:
    import asyncio
    asyncio.run(_seed(
        task_id="t-2026-05-03-aaaaaaaa", status="archive",
        archived_at="2026-05-02T00:00:00Z",
    ))
    asyncio.run(_seed(
        task_id="t-2026-05-03-bbbbbbbb", status="execute",
    ))
    r = client.get("/api/tasks/archive")
    assert r.status_code == 200
    body = r.json()
    ids = {t["id"] for t in body["tasks"]}
    assert "t-2026-05-03-aaaaaaaa" in ids
    assert "t-2026-05-03-bbbbbbbb" not in ids
    assert body["total"] >= 1


def test_archive_pagination(client: TestClient) -> None:
    import asyncio
    for i in range(5):
        asyncio.run(_seed(
            task_id=f"t-2026-05-03-{i:08x}",
            status="archive",
            archived_at=f"2026-05-{2 + i:02d}T00:00:00Z",
        ))
    r1 = client.get("/api/tasks/archive?limit=2&offset=0")
    r2 = client.get("/api/tasks/archive?limit=2&offset=2")
    assert r1.status_code == 200 and r2.status_code == 200
    body1 = r1.json()
    body2 = r2.json()
    assert body1["limit"] == 2 and body1["offset"] == 0
    assert body2["limit"] == 2 and body2["offset"] == 2
    # Page 1 + page 2 don't overlap.
    ids1 = {t["id"] for t in body1["tasks"]}
    ids2 = {t["id"] for t in body2["tasks"]}
    assert ids1.isdisjoint(ids2)


def test_archive_hides_cancelled_by_default(client: TestClient) -> None:
    import asyncio
    asyncio.run(_seed(
        task_id="t-2026-05-03-aaaaaaaa",
        status="archive",
        archived_at="2026-05-02T00:00:00Z",
        cancelled_at="2026-05-02T00:00:00Z",
    ))
    asyncio.run(_seed(
        task_id="t-2026-05-03-bbbbbbbb",
        status="archive",
        archived_at="2026-05-02T00:00:00Z",
    ))
    r = client.get("/api/tasks/archive")
    ids = {t["id"] for t in r.json()["tasks"]}
    assert "t-2026-05-03-aaaaaaaa" not in ids
    assert "t-2026-05-03-bbbbbbbb" in ids
    # With include_cancelled=true the cancelled task appears.
    r = client.get("/api/tasks/archive?include_cancelled=true")
    ids = {t["id"] for t in r.json()["tasks"]}
    assert "t-2026-05-03-aaaaaaaa" in ids


def test_archive_search_q(client: TestClient) -> None:
    import asyncio
    asyncio.run(_seed(
        task_id="t-2026-05-03-aaaaaaaa", title="Refactor header layout",
        status="archive", archived_at="2026-05-02T00:00:00Z",
    ))
    asyncio.run(_seed(
        task_id="t-2026-05-03-bbbbbbbb", title="Fix typo in docs",
        status="archive", archived_at="2026-05-02T00:00:00Z",
    ))
    r = client.get("/api/tasks/archive?q=header")
    ids = {t["id"] for t in r.json()["tasks"]}
    assert ids == {"t-2026-05-03-aaaaaaaa"}


def test_archive_includes_role_history(client: TestClient) -> None:
    """Archive rows include completed role assignments, not just active
    assignments, so the drawer can show the full lifecycle."""
    import asyncio
    task_id = "t-2026-05-03-abcddcba"
    asyncio.run(_seed(
        task_id=task_id,
        status="archive",
        archived_at="2026-05-02T00:00:00Z",
    ))

    async def add_completed_role() -> None:
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO task_role_assignments "
                "(task_id, role, eligible_owners, owner, assigned_at, "
                "completed_at, verdict, report_path) "
                "VALUES (?, 'auditor_syntax', '[]', 'p4', "
                "'2026-05-02T10:00:00Z', '2026-05-02T11:00:00Z', "
                "'pass', 'audits/audit_1_syntax.md')",
                (task_id,),
            )
            await c.commit()
        finally:
            await c.close()

    asyncio.run(add_completed_role())
    r = client.get("/api/tasks/archive")
    assert r.status_code == 200
    row = next(t for t in r.json()["tasks"] if t["id"] == task_id)
    assert row["assignments"][0]["completed_at"] is not None
    assert row["assignments"][0]["verdict"] == "pass"


# ----------------------------------------------------------------- /assignments

def test_assignments_returns_full_history(client: TestClient) -> None:
    """All assignment rows for the task — including superseded +
    completed — to support the fail-loop history view."""
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa", status="audit_syntax"))

    async def add_rows() -> None:
        c = await configured_conn()
        try:
            # Round 1 syntax fail (completed but kept for history).
            await c.execute(
                "INSERT INTO task_role_assignments "
                "(task_id, role, eligible_owners, owner, "
                "assigned_at, completed_at, verdict, report_path) "
                "VALUES (?, 'auditor_syntax', '[]', 'p4', "
                "'2026-05-01T10:00:00Z', '2026-05-01T11:00:00Z', "
                "'fail', 'audits/audit_1_syntax.md')",
                ("t-2026-05-03-aaaaaaaa",),
            )
            # Round 2 syntax (active).
            await c.execute(
                "INSERT INTO task_role_assignments "
                "(task_id, role, eligible_owners, owner, assigned_at) "
                "VALUES (?, 'auditor_syntax', '[]', 'p4', "
                "'2026-05-02T10:00:00Z')",
                ("t-2026-05-03-aaaaaaaa",),
            )
            await c.commit()
        finally:
            await c.close()

    asyncio.run(add_rows())
    r = client.get("/api/tasks/t-2026-05-03-aaaaaaaa/assignments")
    assert r.status_code == 200
    body = r.json()
    assert len(body["assignments"]) == 2
    # Sorted by assigned_at — oldest first.
    assert body["assignments"][0]["verdict"] == "fail"
    assert body["assignments"][1]["verdict"] is None  # active row


def test_assignments_404_for_unknown_task(client: TestClient) -> None:
    r = client.get("/api/tasks/t-2026-05-03-99999999/assignments")
    assert r.status_code == 404


# ----------------------------------------------------------------- /stage

def test_stage_post_advances(client: TestClient) -> None:
    import asyncio
    task_id = "t-2026-05-03-aaaaaaaa"
    asyncio.run(_seed(task_id=task_id, status="plan", spec_path="x"))

    async def add_executor() -> None:
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO task_role_assignments "
                "(task_id, role, eligible_owners, owner, assigned_at) "
                "VALUES (?, 'executor', '[]', 'p3', '2026-05-03T10:00:00Z')",
                (task_id,),
            )
            await c.commit()
        finally:
            await c.close()

    asyncio.run(add_executor())
    r = client.post(
        f"/api/tasks/{task_id}/stage",
        json={"stage": "execute", "note": "kicking off"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["from"] == "plan"
    assert body["to"] == "execute"


def test_stage_post_invalid_transition_rejected_without_force(
    client: TestClient,
) -> None:
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa", status="plan"))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/stage",
        json={"stage": "ship"},  # plan → ship is illegal
    )
    assert r.status_code == 400
    assert "invalid transition" in r.json()["detail"]


def test_stage_post_force_bypasses_state_machine(client: TestClient) -> None:
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa", status="plan"))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/stage",
        json={"stage": "ship", "force": True},
    )
    assert r.status_code == 200


# ----------------------------------------------------------------- /complexity / /blocked

def test_complexity_post(client: TestClient) -> None:
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa"))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/complexity",
        json={"complexity": "simple"},
    )
    assert r.status_code == 200
    assert r.json()["complexity"] == "simple"


def test_blocked_post(client: TestClient) -> None:
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa", status="execute", owner="p3"))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/blocked",
        json={"blocked": True, "reason": "waiting on stakeholder"},
    )
    assert r.status_code == 200
    assert r.json()["blocked"] is True


def test_blocked_post_rejects_archived_task(client: TestClient) -> None:
    import asyncio
    asyncio.run(_seed(
        task_id="t-2026-05-03-fedcba98",
        status="archive",
        owner="p3",
        archived_at="2026-05-02T00:00:00Z",
    ))
    r = client.post(
        "/api/tasks/t-2026-05-03-fedcba98/blocked",
        json={"blocked": True, "reason": "too late"},
    )
    assert r.status_code == 400
    assert "archived" in r.json()["detail"]


# ----------------------------------------------------------------- /spec

def test_spec_post_writes_md_and_updates_row(client: TestClient) -> None:
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-abc12345"))
    r = client.post(
        "/api/tasks/t-2026-05-03-abc12345/spec",
        json={"body": "## Goal\nDo the thing."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["spec_path"].endswith("/spec.md")


def test_spec_post_invalid_task_id_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/tasks/..%2F..%2Fetc%2Fpasswd/spec",
        json={"body": "x"},
    )
    # Either 400 from validator or 404 from task lookup; both are rejection.
    assert r.status_code in (400, 404)


# ----------------------------------------------------------------- /assign

def test_assign_post_single_player_creates_role_row(client: TestClient) -> None:
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa", status="audit_syntax", owner="p3"))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/assign",
        json={"role": "auditor_syntax", "to": "p4"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["to"] == ["p4"]


def test_assign_post_pool_form_creates_eligible_owners(
    client: TestClient,
) -> None:
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa", status="plan"))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/assign",
        json={"role": "executor", "to": ["p3", "p4", "p5"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert sorted(body["to"]) == ["p3", "p4", "p5"]

    async def check() -> dict:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT eligible_owners, owner FROM task_role_assignments "
                "WHERE task_id = ? AND role = 'executor'",
                ("t-2026-05-03-aaaaaaaa",),
            )
            return dict(await cur.fetchone())
        finally:
            await c.close()

    row = asyncio.run(check())
    assert sorted(json.loads(row["eligible_owners"])) == ["p3", "p4", "p5"]
    assert row["owner"] is None  # pool: no claim yet


def test_assign_post_invalid_slot_rejected(client: TestClient) -> None:
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa"))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/assign",
        json={"role": "executor", "to": "coach"},
    )
    assert r.status_code == 400


def test_assign_post_supersedes_failed_audit_round(client: TestClient) -> None:
    import asyncio
    task_id = "t-2026-05-03-1234abcd"
    asyncio.run(_seed(task_id=task_id, status="audit_syntax", owner="p3"))

    async def add_failed_round() -> int:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "INSERT INTO task_role_assignments "
                "(task_id, role, eligible_owners, owner, assigned_at, "
                "completed_at, verdict, report_path) "
                "VALUES (?, 'auditor_syntax', '[]', 'p4', "
                "'2026-05-02T10:00:00Z', '2026-05-02T11:00:00Z', "
                "'fail', 'audits/audit_1_syntax.md')",
                (task_id,),
            )
            await c.commit()
            return int(cur.lastrowid)
        finally:
            await c.close()

    old_id = asyncio.run(add_failed_round())
    r = client.post(
        f"/api/tasks/{task_id}/assign",
        json={"role": "auditor_syntax", "to": "p5"},
    )
    assert r.status_code == 200

    async def read_superseded() -> tuple[int | None, int]:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT superseded_by FROM task_role_assignments WHERE id = ?",
                (old_id,),
            )
            old_row = dict(await cur.fetchone())
            cur = await c.execute(
                "SELECT MAX(id) AS id FROM task_role_assignments "
                "WHERE task_id = ? AND role = 'auditor_syntax'",
                (task_id,),
            )
            new_row = dict(await cur.fetchone())
            return old_row["superseded_by"], new_row["id"]
        finally:
            await c.close()

    superseded_by, new_id = asyncio.run(read_superseded())
    assert superseded_by == new_id


def test_assign_post_404_for_unknown_task(client: TestClient) -> None:
    r = client.post(
        "/api/tasks/t-2026-05-03-99999999/assign",
        json={"role": "executor", "to": "p3"},
    )
    assert r.status_code == 404

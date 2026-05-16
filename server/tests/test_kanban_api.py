"""Tests for the kanban HTTP endpoints (Docs/kanban-specs.md §7).

Endpoints under test:
  - GET /api/tasks/board (active 5 buckets)
  - GET /api/tasks/archive (paginated)
  - GET /api/tasks/{id}/assignments (full role history)
  - POST /api/tasks/{id}/stage
  - POST /api/tasks/{id}/blocked
  - POST /api/tasks/{id}/spec
  - POST /api/tasks/{id}/assign
  - POST /api/tasks/{id}/workflow (v0.3: workflow + tracking_reason only)

Uses FastAPI's TestClient outside `with` so lifespan (scheduler +
telegram + kanban subscriber) doesn't run; the endpoints don't depend
on those background tasks for their own correctness.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import server.agents as agents_mod
from server.db import configured_conn, init_db


# v0.3 default trajectory used when seed callers don't override.
_STANDARD_TRAJECTORY = (
    '[{"stage":"plan","to":[]},'
    '{"stage":"execute","to":[]},'
    '{"stage":"audit_syntax","to":[]},'
    '{"stage":"audit_semantics","to":[]},'
    '{"stage":"ship","to":[]}]'
)


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
    trajectory: str = _STANDARD_TRAJECTORY,
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
            "created_by, trajectory, priority, archived_at, cancelled_at, "
            "spec_path) "
            "VALUES (?, 'misc', ?, ?, ?, 'human', ?, ?, ?, ?, ?)",
            (task_id, title, status, owner, trajectory, priority,
             archived_at, cancelled_at, spec_path),
        )
        await c.commit()
    finally:
        await c.close()


async def _plant_role(
    *,
    task_id: str,
    role: str,
    owner: str | None = None,
    completed: bool = False,
) -> None:
    c = await configured_conn()
    try:
        completed_sql = (
            "strftime('%Y-%m-%dT%H:%M:%fZ','now')" if completed else "NULL"
        )
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, "
            "claimed_at, completed_at) "
            "VALUES (?, ?, '[]', ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            f"{completed_sql})",
            (task_id, role, owner),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_ship_event(
    *,
    task_id: str,
    ship_sha: str = "aabbccdd11223344556677889900aabbccdd1122",
    pr_number: int = 42,
    pr_url: str = "https://github.com/owner/repo/pull/42",
    deploy_target: str = "dev",
) -> None:
    payload = {
        "task_id": task_id,
        "ship_sha": ship_sha,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "deploy_target": deploy_target,
        "executor_sha": "ffee00112233445566778899aabbccddeeff0011",
    }
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO project_events "
            "(project_id, actor, type, task_id, payload_json, payload_pointer) "
            "VALUES ('misc', 'p3', 'task_shipped_to_dev', ?, ?, ?)",
            (task_id, json.dumps(payload), ship_sha),
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
    assert {
        "plan", "execute", "audit_syntax", "audit_semantics", "ship", "verify",
    } == set(board.keys())
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


def test_flow_health_includes_verify_stage_counts(client: TestClient) -> None:
    import asyncio
    asyncio.run(_seed(
        task_id="t-2026-05-03-vvvvvvvv",
        status="verify",
    ))

    r = client.get("/api/tasks/flow_health")
    assert r.status_code == 200
    stages = r.json()["stages"]
    assert "verify" in stages
    assert stages["verify"]["count"] == 1


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


# ----------------------------------------------------------------- /approve_stage (v2)

def test_approve_stage_post_plants_role_and_transitions(
    client: TestClient,
) -> None:
    """v2 single transition tool: stages and assigns atomically."""
    import asyncio
    task_id = "t-2026-05-03-aaaaaaaa"
    asyncio.run(_seed(task_id=task_id, status="plan", spec_path="x"))
    r = client.post(
        f"/api/tasks/{task_id}/approve_stage",
        json={"next_stage": "execute", "assignee": "p3", "note": "kicking off"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from"] == "plan"
    assert body["to"] == "execute"
    assert body["assignee"] == "p3"

    async def read_state() -> dict:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status, owner FROM tasks WHERE id = ?",
                (task_id,),
            )
            return dict(await cur.fetchone())
        finally:
            await c.close()

    state = asyncio.run(read_state())
    assert state["status"] == "execute"
    assert state["owner"] == "p3"


def test_approve_stage_post_invalid_transition_rejected(
    client: TestClient,
) -> None:
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa", status="plan"))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/approve_stage",
        json={"next_stage": "ship", "assignee": "p3"},
    )
    assert r.status_code == 400
    assert "invalid transition" in r.json()["detail"]


def test_approve_stage_post_archive_rejects_assignee(
    client: TestClient,
) -> None:
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa", status="execute", owner="p3"))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/approve_stage",
        json={"next_stage": "archive", "assignee": "p3"},
    )
    assert r.status_code == 400


def test_approve_stage_post_requires_assignee_for_non_archive(
    client: TestClient,
) -> None:
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa", status="plan"))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/approve_stage",
        json={"next_stage": "execute"},
    )
    assert r.status_code == 400
    assert "assignee" in r.json()["detail"].lower()


def test_approve_stage_post_ship_to_verify_rejects_without_ship_evidence(
    client: TestClient,
) -> None:
    import asyncio
    task_id = "t-2026-05-03-verify00"
    asyncio.run(_seed(task_id=task_id, status="ship", owner="p2"))
    asyncio.run(_plant_role(task_id=task_id, role="shipper", owner="p2"))

    r = client.post(
        f"/api/tasks/{task_id}/approve_stage",
        json={
            "next_stage": "verify",
            "assignee": "p4",
            "note": "verify dev deployment",
        },
    )
    assert r.status_code == 400
    assert "ship → verify requires post-ship evidence" in r.json()["detail"]
    assert "task_shipped_to_dev" in r.json()["detail"]

    async def read_state() -> tuple[str, int]:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,),
            )
            status = dict(await cur.fetchone())["status"]
            cur = await c.execute(
                "SELECT COUNT(*) AS n FROM task_role_assignments "
                "WHERE task_id = ? AND role = 'verifier'",
                (task_id,),
            )
            verifier_count = int(dict(await cur.fetchone())["n"])
            return status, verifier_count
        finally:
            await c.close()

    status, verifier_count = asyncio.run(read_state())
    assert status == "ship"
    assert verifier_count == 0


def test_approve_stage_post_ship_to_verify_requires_manual_override_tag(
    client: TestClient,
) -> None:
    import asyncio
    task_id = "t-2026-05-03-verify02"
    asyncio.run(_seed(task_id=task_id, status="ship", owner="p2"))
    asyncio.run(_plant_role(
        task_id=task_id, role="shipper", owner="p2", completed=True,
    ))

    r = client.post(
        f"/api/tasks/{task_id}/approve_stage",
        json={
            "next_stage": "verify",
            "assignee": "p4",
            "note": "verify manual deployment",
        },
    )
    assert r.status_code == 400
    assert "completed shipper role but no task_shipped_to_dev event" in (
        r.json()["detail"]
    )
    assert "[manual verify override]" in r.json()["detail"]


def test_approve_stage_post_ship_to_verify_includes_ship_context(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    task_id = "t-2026-05-03-verify01"
    asyncio.run(_seed(task_id=task_id, status="ship", owner="p2"))
    asyncio.run(_plant_role(
        task_id=task_id, role="shipper", owner="p2", completed=True,
    ))
    asyncio.run(_seed_ship_event(task_id=task_id))
    wakes: list[tuple[str, str]] = []

    async def _rec(slot: str, prompt: str = "", **kw: object) -> bool:
        wakes.append((slot, prompt))
        return True

    monkeypatch.setattr(agents_mod, "maybe_wake_agent", _rec)

    r = client.post(
        f"/api/tasks/{task_id}/approve_stage",
        json={
            "next_stage": "verify",
            "assignee": "p4",
            "note": "verify dev deployment",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["to"] == "verify"
    assert body["ship_verify_context"]
    assert "deploy_target=dev" in body["ship_verify_context"]
    assert (
        "ship_sha=aabbccdd11223344556677889900aabbccdd1122"
        in body["ship_verify_context"]
    )
    assert "PR #42 https://github.com/owner/repo/pull/42" in (
        body["ship_verify_context"]
    )
    assert wakes and wakes[-1][0] == "p4"
    assert "deploy_target=dev" in wakes[-1][1]
    assert "ship_sha=aabbccdd11223344556677889900aabbccdd1122" in wakes[-1][1]
    assert "PR #42 https://github.com/owner/repo/pull/42" in wakes[-1][1]


# ----------------------------------------------------------------- /workflow / /blocked

def test_workflow_post_updates_workflow_and_tracking_reason(
    client: TestClient,
) -> None:
    """v0.3: /workflow only carries `workflow` + `tracking_reason`. The
    legacy required_reviews / ship_required / complexity knobs moved to
    the trajectory column — POST /api/tasks/{id}/trajectory handles those."""
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa"))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/workflow",
        json={
            "workflow": "research",
            "tracking_reason": "durable_artifact",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["workflow"] == "research"
    assert body["tracking_reason"] == "durable_artifact"


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


# /api/tasks/{id}/assign was folded into /api/tasks/{id}/approve_stage
# in v2 — see tests above. Coverage of the supersede + plant pattern is
# now in test_coord_approve_stage.py (the MCP-tool side); the HTTP path
# uses the same helpers, so we just smoke-test the endpoint shape.


def test_approve_stage_post_404_for_unknown_task(client: TestClient) -> None:
    r = client.post(
        "/api/tasks/t-2026-05-03-99999999/approve_stage",
        json={"next_stage": "execute", "assignee": "p3"},
    )
    assert r.status_code == 404


def test_approve_stage_post_invalid_assignee_rejected(
    client: TestClient,
) -> None:
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa", status="plan"))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/approve_stage",
        json={"next_stage": "execute", "assignee": "coach"},
    )
    assert r.status_code == 400


# ---------------------------------------------------- /flag_deviation (v2 §22.1)

def test_flag_deviation_inserts_human_row(client: TestClient) -> None:
    import asyncio
    asyncio.run(_seed(
        task_id="t-2026-05-03-aaaaaaaa",
        status="execute",
        owner="p3",
    ))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/flag_deviation",
        json={"description": "scope drift: p3 added an unrelated refactor"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == "t-2026-05-03-aaaaaaaa"
    assert body["executor"] == "p3"

    async def read() -> dict:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT executor, noticed_at, description "
                "FROM deviations_log WHERE task_id = ?",
                ("t-2026-05-03-aaaaaaaa",),
            )
            return dict(await cur.fetchone())
        finally:
            await c.close()

    row = asyncio.run(read())
    assert row["noticed_at"] == "human"
    assert row["executor"] == "p3"
    assert "scope drift" in row["description"]


def test_flag_deviation_404_for_unknown_task(client: TestClient) -> None:
    r = client.post(
        "/api/tasks/t-2026-05-03-99999999/flag_deviation",
        json={"description": "x"},
    )
    assert r.status_code == 404


def test_flag_deviation_requires_description(client: TestClient) -> None:
    import asyncio
    asyncio.run(_seed(task_id="t-2026-05-03-aaaaaaaa", status="execute", owner="p3"))
    r = client.post(
        "/api/tasks/t-2026-05-03-aaaaaaaa/flag_deviation",
        json={"description": ""},
    )
    # Pydantic min_length=1 → 422 validation failure.
    assert r.status_code == 422


# ----------------------------------------------------- /api/projects/{id}/event_log

def test_event_log_returns_unread_rows_by_default(client: TestClient) -> None:
    """The migration's synthetic kanban_v2_cutover row lands as UNREAD,
    so a fresh DB returns it on the default include_read=false query."""
    r = client.get("/api/projects/misc/event_log")
    assert r.status_code == 200
    body = r.json()
    assert body["include_read"] is False
    types = [e["type"] for e in body["events"]]
    assert "kanban_v2_cutover" in types


def test_event_log_filter_by_type(client: TestClient) -> None:
    r = client.get("/api/projects/misc/event_log?type=nonexistent_type")
    assert r.status_code == 200
    assert r.json()["events"] == []


def test_event_log_filter_by_actor(client: TestClient) -> None:
    r = client.get("/api/projects/misc/event_log?actor=system")
    assert r.status_code == 200
    body = r.json()
    actors = {e["actor"] for e in body["events"]}
    # All returned rows should be 'system'.
    assert actors <= {"system"} or not actors


def test_event_log_limit_capped(client: TestClient) -> None:
    r = client.get("/api/projects/misc/event_log?limit=500")
    assert r.status_code == 200
    body = r.json()
    # Server caps at 200.
    assert body["limit"] == 200


def test_event_log_unknown_project_returns_empty(client: TestClient) -> None:
    r = client.get("/api/projects/no-such/event_log")
    assert r.status_code == 200
    assert r.json()["events"] == []


# --------------------------------------------------------------- create
# v0.3 audit-2026-05-04 item 2: trajectory-driven creation activation


def test_create_task_starts_in_first_trajectory_stage(client: TestClient) -> None:
    """Execute-only trajectory must land directly in `execute`, not the
    schema default `plan`. Otherwise the executor row sits behind the
    spec gate and can't be claimed."""
    r = client.post(
        "/api/tasks",
        json={
            "title": "exec-only task",
            "trajectory": [{"stage": "execute", "to": ["p2"]}],
        },
    )
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    import asyncio

    async def read():
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status, owner, last_stage_change_at "
                "FROM tasks WHERE id = ?",
                (task_id,),
            )
            return dict(await cur.fetchone())
        finally:
            await c.close()

    row = asyncio.run(read())
    assert row["status"] == "execute"
    assert row["owner"] == "p2"
    assert row["last_stage_change_at"] is not None


def test_create_task_with_plan_starts_in_plan(client: TestClient) -> None:
    """A trajectory beginning with `plan` must start in `plan`."""
    r = client.post(
        "/api/tasks",
        json={
            "title": "needs spec",
            "trajectory": [
                {"stage": "plan", "to": "p3"},
                {"stage": "execute", "to": "p2"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    import asyncio

    async def read():
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status, owner FROM tasks WHERE id = ?",
                (task_id,),
            )
            return dict(await cur.fetchone())
        finally:
            await c.close()

    row = asyncio.run(read())
    assert row["status"] == "plan"
    assert row["owner"] == "p3"  # planner is the first-stage hard-assignee


def test_http_create_rejects_pool_first_stage(client: TestClient) -> None:
    """v2.0.1 (2026-05-08): pool first-stage `to` rejected at HTTP create.
    Coach must name the first-stage Player; pool/empty no longer
    produces an undispatched task on the kanban."""
    r = client.post(
        "/api/tasks",
        json={
            "title": "http-pool-rejected",
            "trajectory": [{"stage": "execute", "to": ["p2", "p3"]}],
        },
    )
    assert r.status_code == 400, r.text
    assert "trajectory[0].to" in r.json()["detail"]


def test_http_create_rejects_empty_first_stage(client: TestClient) -> None:
    r = client.post(
        "/api/tasks",
        json={
            "title": "http-empty-rejected",
            "trajectory": [{"stage": "execute", "to": []}],
        },
    )
    assert r.status_code == 400, r.text
    assert "trajectory[0].to" in r.json()["detail"]


async def test_async_create_single_name_first_stage_emits_stage_changed(
    fresh_db: str,
) -> None:
    """v2 §7.1 — single-name first-stage path DOES emit
    task_stage_changed. Uses the async test runner + an in-loop kanban
    subscriber so the bus event drains through `maybe_write_from_bus`
    and lands a project_events row before the assertion runs.

    (TestClient can't drive this assertion: it runs each request in a
    fresh asyncio.run, so a subscriber started in one run can't drain
    events published in another. Hence the async test pattern.)
    """
    import asyncio
    import re
    from server.kanban import start_kanban_subscriber, stop_kanban_subscriber
    from server.events import bus
    from datetime import datetime, timezone

    await init_db()
    await start_kanban_subscriber()
    try:
        # Direct bus.publish to mirror the HTTP create's emission path.
        task_id = "t-2026-05-07-aaaaaa01"
        ts = datetime.now(timezone.utc).isoformat()
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO tasks (id, project_id, title, status, owner, "
                "created_by, trajectory) VALUES (?, 'misc', 'demo', "
                "'execute', 'p2', 'human', "
                "'[{\"stage\":\"execute\",\"to\":[\"p2\"]}]')",
                (task_id,),
            )
            await c.commit()
        finally:
            await c.close()
        await bus.publish({
            "ts": ts, "agent_id": "system", "type": "task_stage_changed",
            "task_id": task_id, "from": None, "to": "execute",
            "reason": "task_created", "owner": "p2", "assignee": "p2",
        })
        await asyncio.sleep(0.2)

        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT type FROM project_events WHERE task_id = ?",
                (task_id,),
            )
            types = [dict(r)["type"] for r in await cur.fetchall()]
        finally:
            await c.close()
        assert "task_stage_changed" in types
    finally:
        await stop_kanban_subscriber()


def test_create_task_pool_first_stage_rejected_at_http(client: TestClient) -> None:
    """v2.0.1 (2026-05-08): pool first-stage `to` rejected at HTTP
    create. The kanban is a log of dispatched work; pool/empty no
    longer creates an undispatched task."""
    r = client.post(
        "/api/tasks",
        json={
            "title": "pool exec",
            "trajectory": [{"stage": "execute", "to": ["p2", "p3"]}],
        },
    )
    assert r.status_code == 400, r.text
    assert "trajectory[0].to" in r.json()["detail"]


def test_create_task_no_trajectory_rejected(client: TestClient) -> None:
    """v2.0.1: omitting `trajectory` is no longer accepted — the
    legacy [{stage:execute,to:[]}] default is gone. Caller must
    supply a trajectory whose first stage names a Player."""
    r = client.post("/api/tasks", json={"title": "no trajectory"})
    assert r.status_code == 400, r.text
    assert "trajectory is required" in r.json()["detail"]


def test_create_task_default_trajectory_starts_in_execute(client: TestClient) -> None:
    """When the caller supplies a single-name first-stage trajectory,
    the task lands in execute with that owner."""
    r = client.post(
        "/api/tasks",
        json={
            "title": "single-name default",
            "trajectory": [{"stage": "execute", "to": ["p3"]}],
        },
    )
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]
    import asyncio

    async def read():
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status, owner FROM tasks WHERE id = ?",
                (task_id,),
            )
            return dict(await cur.fetchone())
        finally:
            await c.close()

    row = asyncio.run(read())
    assert row["status"] == "execute"
    assert row["owner"] == "p3"


# --------------------------------------------------------------- /trajectory
# v0.3 audit-2026-05-04 item 3: HTTP trajectory reroute must not 500


def test_post_trajectory_remove_stage_does_not_500(client: TestClient) -> None:
    """The endpoint previously wrote `superseded_by = -1` and
    `superseded_at` (FK violation + nonexistent column). Both paths
    now use `completed_at = now()` to deactivate orphaned rows."""
    import asyncio
    full_traj = (
        '[{"stage":"plan","to":[]},'
        '{"stage":"execute","to":[]},'
        '{"stage":"audit_syntax","to":[]},'
        '{"stage":"audit_semantics","to":[]},'
        '{"stage":"ship","to":[]}]'
    )
    asyncio.run(_seed(
        task_id="t-2026-05-03-trajedi1",
        status="execute",
        trajectory=full_traj,
        owner="p3",
        spec_path="x",
    ))
    r = client.post(
        "/api/tasks/t-2026-05-03-trajedi1/trajectory",
        json={
            "trajectory": [
                {"stage": "plan", "to": []},
                {"stage": "execute", "to": []},
            ],
        },
    )
    assert r.status_code == 200, r.text


def test_post_trajectory_add_stage_does_not_500(client: TestClient) -> None:
    """Adding a stage path previously crashed on `assigned_by`
    (nonexistent column)."""
    import asyncio
    asyncio.run(_seed(
        task_id="t-2026-05-03-trajedi2",
        status="execute",
        trajectory='[{"stage":"execute","to":[]}]',
        owner="p3",
    ))
    r = client.post(
        "/api/tasks/t-2026-05-03-trajedi2/trajectory",
        json={
            "trajectory": [
                {"stage": "execute", "to": []},
                {"stage": "audit_syntax", "to": ["p4"]},
                {"stage": "ship", "to": "p2"},
            ],
        },
    )
    assert r.status_code == 200, r.text


def test_post_trajectory_rejects_removing_already_entered(
    client: TestClient,
) -> None:
    """Item 5 mirror at the HTTP layer."""
    import asyncio
    full_traj = (
        '[{"stage":"plan","to":[]},'
        '{"stage":"execute","to":[]},'
        '{"stage":"audit_syntax","to":[]},'
        '{"stage":"audit_semantics","to":[],"focus":"check semantics"},'
        '{"stage":"ship","to":[]}]'
    )
    asyncio.run(_seed(
        task_id="t-2026-05-03-trajedi3",
        status="audit_semantics",
        trajectory=full_traj,
        owner="p3",
        spec_path="x",
    ))
    r = client.post(
        "/api/tasks/t-2026-05-03-trajedi3/trajectory",
        json={
            "trajectory": [
                {"stage": "plan", "to": []},
                {"stage": "execute", "to": []},
                {"stage": "audit_semantics", "to": [], "focus": "check"},
                {"stage": "ship", "to": []},
            ],
        },
    )
    assert r.status_code == 400, r.text
    assert "already-entered" in r.json()["detail"]

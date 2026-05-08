"""v2 §15.3 — `/api/team/player_health` for EnvPlayerHealthSection."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.db import configured_conn, init_db
from server.main import app


@pytest.fixture
def client(fresh_db: str) -> TestClient:
    return TestClient(app)


async def _seed_deviation(
    *, project_id: str, executor: str, noticed_at: str = "audit",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO deviations_log "
            "(project_id, ts, task_id, executor, noticed_at, description) "
            "VALUES (?, datetime('now'), 't-2026-05-07-aaaaaaaa', ?, ?, "
            "'demo')",
            (project_id, executor, noticed_at),
        )
        await c.commit()
    finally:
        await c.close()


def test_empty_returns_empty_rows(client: TestClient) -> None:
    """Healthy team — no deviations, endpoint returns empty rows so
    the UI hides the section."""
    import asyncio
    asyncio.new_event_loop().run_until_complete(init_db())
    r = client.get("/api/team/player_health")
    assert r.status_code == 200
    body = r.json()
    assert body["project_id"] == "misc"
    assert body["rows"] == []


def test_seeded_deviation_surfaces_in_rows(client: TestClient) -> None:
    """A seeded deviation produces a row with the right counter."""
    import asyncio
    asyncio.new_event_loop().run_until_complete(init_db())
    asyncio.new_event_loop().run_until_complete(
        _seed_deviation(project_id="misc", executor="p3", noticed_at="audit")
    )
    r = client.get("/api/team/player_health")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert any(
        row["slot"] == "p3" and row["off_spec_completions"] >= 1
        for row in rows
    )


def test_row_shape(client: TestClient) -> None:
    """Schema contract: each row has slot/deviations/push_before_audit/
    off_spec_completions int fields."""
    import asyncio
    asyncio.new_event_loop().run_until_complete(init_db())
    asyncio.new_event_loop().run_until_complete(
        _seed_deviation(project_id="misc", executor="p7", noticed_at="push")
    )
    rows = client.get("/api/team/player_health").json()["rows"]
    assert rows
    r0 = rows[0]
    for k in ("slot", "deviations", "push_before_audit", "off_spec_completions"):
        assert k in r0
    assert isinstance(r0["slot"], str)
    assert isinstance(r0["deviations"], int)
    assert isinstance(r0["push_before_audit"], int)
    assert isinstance(r0["off_spec_completions"], int)

"""Tests for the project-scoped objectives + coach-todos HTTP endpoints
(`recurrence-specs.md` §9), wired in `server/projects_api.py` for
phase 7 of the recurrence rewrite.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.db import init_db
from server.paths import ensure_project_scaffold, project_paths


@pytest.fixture
async def client(fresh_db: str):
    from fastapi.testclient import TestClient
    import server.main as mainmod
    mainmod.HARNESS_TOKEN = ""
    await init_db()
    ensure_project_scaffold("misc")
    with TestClient(mainmod.app) as c:
        yield c


# ---- objectives -----------------------------------------------------


async def test_get_objectives_empty(client: Any) -> None:
    r = client.get("/api/projects/misc/objectives")
    assert r.status_code == 200
    assert r.json()["text"] == ""


async def test_put_objectives_persists_to_disk(client: Any) -> None:
    r = client.put(
        "/api/projects/misc/objectives", json={"text": "## Goals\n\nShip."},
    )
    assert r.status_code == 200, r.text
    pp = project_paths("misc")
    assert pp.project_objectives.read_text(encoding="utf-8") == (
        "## Goals\n\nShip."
    )
    # GET round-trips.
    r = client.get("/api/projects/misc/objectives")
    assert r.json()["text"] == "## Goals\n\nShip."


async def test_put_objectives_rejects_missing_field(client: Any) -> None:
    r = client.put("/api/projects/misc/objectives", json={})
    assert r.status_code == 400


async def test_put_objectives_404_unknown_project(client: Any) -> None:
    r = client.put(
        "/api/projects/does-not-exist/objectives",
        json={"text": "x"},
    )
    assert r.status_code == 404


# ---- coach todos ----------------------------------------------------


async def test_post_coach_todo_creates_open_entry(client: Any) -> None:
    r = client.post(
        "/api/projects/misc/coach-todos",
        json={"title": "Plan launch", "description": "stakeholders"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "t-1"
    assert body["title"] == "Plan launch"

    r = client.get("/api/projects/misc/coach-todos")
    assert r.status_code == 200
    assert len(r.json()["todos"]) == 1


async def test_post_coach_todo_rejects_empty_title(client: Any) -> None:
    r = client.post(
        "/api/projects/misc/coach-todos",
        json={"title": "   "},
    )
    assert r.status_code == 400


async def test_complete_coach_todo_moves_to_archive(client: Any) -> None:
    r = client.post(
        "/api/projects/misc/coach-todos",
        json={"title": "Done me"},
    )
    rid = r.json()["id"]

    r = client.post(f"/api/projects/misc/coach-todos/{rid}/complete")
    assert r.status_code == 200
    body = r.json()
    assert body["completed"]

    r = client.get("/api/projects/misc/coach-todos")
    assert len(r.json()["todos"]) == 0
    r = client.get("/api/projects/misc/coach-todos/archive")
    assert len(r.json()["todos"]) == 1


async def test_complete_unknown_todo_404(client: Any) -> None:
    r = client.post("/api/projects/misc/coach-todos/t-999/complete")
    assert r.status_code == 404


async def test_patch_updates_fields(client: Any) -> None:
    r = client.post(
        "/api/projects/misc/coach-todos",
        json={"title": "old", "due": "2026-05-01"},
    )
    rid = r.json()["id"]
    r = client.patch(
        f"/api/projects/misc/coach-todos/{rid}",
        json={"title": "new", "due": ""},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "new"
    assert body["due"] is None


async def test_patch_rejects_empty_body(client: Any) -> None:
    r = client.post(
        "/api/projects/misc/coach-todos",
        json={"title": "x"},
    )
    rid = r.json()["id"]
    r = client.patch(f"/api/projects/misc/coach-todos/{rid}", json={})
    assert r.status_code == 400

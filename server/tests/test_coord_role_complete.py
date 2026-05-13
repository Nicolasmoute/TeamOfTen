"""Audit tests for coord_role_complete (Docs/kanban-specs-v2.md §7.2).

Covers:
- Coach rejected (Players-only).
- task_id required.
- message_to_coach required + length cap.
- artifact_path traversal / outside-project / missing-on-disk rejected.
- Role inferred from caller's active role row at task's current stage.
- No active role row → clear error.
- Archive task rejected.
- Stage with no role rejected.
- Role row marked complete on success.
- task_role_completed event emitted with role + message_to_coach.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from server.db import configured_conn, init_db
from server.events import bus
from server.tools import build_coord_server


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok_text(result: dict[str, Any]) -> str:
    assert not result.get("is_error"), f"tool returned error: {result}"
    return result["content"][0]["text"]


def _err_text(result: dict[str, Any]) -> str:
    assert result.get("is_error"), f"expected error, got {result}"
    return result["content"][0]["text"]


async def _seed_with_role(
    *,
    task_id: str = "t-2026-05-07-cccc3333",
    status: str = "execute",
    owner: str = "p2",
    role: str = "executor",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) "
            "VALUES (?, 'misc', 'demo', ?, ?, 'coach', '[]')",
            (task_id, status, owner),
        )
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, "
            " assigned_at, claimed_at) "
            "VALUES (?, ?, '[]', ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (task_id, role, owner),
        )
        await c.commit()
    finally:
        await c.close()


async def test_role_complete_rejects_coach(fresh_db: str) -> None:
    await init_db()
    await _seed_with_role()
    server = _server_for("coach")
    err = _err_text(await _handler(server, "role_complete")({
        "task_id": "t-2026-05-07-cccc3333",
        "message_to_coach": "shipped",
    }))
    assert "coach" in err.lower()


async def test_role_complete_requires_task_id(fresh_db: str) -> None:
    await init_db()
    server = _server_for("p2")
    err = _err_text(await _handler(server, "role_complete")({
        "task_id": "",
        "message_to_coach": "shipped",
    }))
    assert "task_id" in err.lower()


async def test_role_complete_requires_message_to_coach(fresh_db: str) -> None:
    await init_db()
    await _seed_with_role()
    server = _server_for("p2")
    err = _err_text(await _handler(server, "role_complete")({
        "task_id": "t-2026-05-07-cccc3333",
        "message_to_coach": "",
    }))
    assert "message_to_coach" in err.lower()


async def test_role_complete_message_too_long(fresh_db: str) -> None:
    await init_db()
    await _seed_with_role()
    server = _server_for("p2")
    err = _err_text(await _handler(server, "role_complete")({
        "task_id": "t-2026-05-07-cccc3333",
        "message_to_coach": "x" * 2001,
    }))
    assert "too long" in err.lower()


async def test_role_complete_no_active_role_rejected(fresh_db: str) -> None:
    """Caller has no active role row on the task → clear error."""
    await init_db()
    await _seed_with_role(owner="p3")
    server = _server_for("p2")
    err = _err_text(await _handler(server, "role_complete")({
        "task_id": "t-2026-05-07-cccc3333",
        "message_to_coach": "shipped",
    }))
    assert "no active" in err.lower() and "executor" in err.lower()


async def test_role_complete_archived_with_open_role_accepted(fresh_db: str) -> None:
    """Post-archive graceful path: a Player whose deploy-poll runs
    past Coach's archive call should still be able to record their
    verification (kanban v2 §10.7). The role's completion is
    recorded, the event emits with post_archive=true, and the
    response is success-with-note rather than the old hard reject."""
    await init_db()
    await _seed_with_role(status="archive")

    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("p2")
        text = _ok_text(await _handler(server, "role_complete")({
            "task_id": "t-2026-05-07-cccc3333",
            "message_to_coach": "deploy verified; archive landed before me",
        }))
        assert "already archived" in text.lower()
        assert "no stage advance" in text.lower()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while True:
            try:
                captured.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    completed = [
        e for e in captured if e.get("type") == "task_role_completed"
    ]
    assert completed, f"no task_role_completed event captured: {captured}"
    assert completed[0]["post_archive"] is True
    assert completed[0]["role"] == "executor"
    assert completed[0]["owner"] == "p2"


async def test_role_complete_archived_with_no_role_rejected(fresh_db: str) -> None:
    """Archived task + caller has never had a role on it → reject.
    Distinct from the graceful path: there's nothing to record."""
    await init_db()
    await _seed_with_role(status="archive", owner="p3")  # p3 owns; p2 doesn't
    server = _server_for("p2")
    err = _err_text(await _handler(server, "role_complete")({
        "task_id": "t-2026-05-07-cccc3333",
        "message_to_coach": "shipped",
    }))
    err_lower = err.lower()
    assert "archived" in err_lower
    assert "no active role" in err_lower


async def test_role_complete_archived_role_superseded_rejected(fresh_db: str) -> None:
    """Archived task + caller's role was superseded by Coach → reject.
    Distinguishes "Player finished real work" from "Coach reassigned
    away; the caller has no claim on the verification."""
    await init_db()
    await _seed_with_role(status="archive")
    # Mark p2's role row as superseded (Coach reassigned away).
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE task_role_assignments SET superseded_by = 1 "
            "WHERE task_id = 't-2026-05-07-cccc3333' AND role = 'executor'"
        )
        await c.commit()
    finally:
        await c.close()
    server = _server_for("p2")
    err = _err_text(await _handler(server, "role_complete")({
        "task_id": "t-2026-05-07-cccc3333",
        "message_to_coach": "shipped",
    }))
    err_lower = err.lower()
    assert "archived" in err_lower
    assert "no active role" in err_lower


async def test_role_complete_artifact_outside_project_rejected(fresh_db: str) -> None:
    await init_db()
    await _seed_with_role()
    server = _server_for("p2")
    err = _err_text(await _handler(server, "role_complete")({
        "task_id": "t-2026-05-07-cccc3333",
        "message_to_coach": "shipped",
        "artifact_path": "/etc/passwd",
    }))
    assert "outside" in err.lower() or "resolve" in err.lower()


async def test_role_complete_artifact_missing_on_disk_rejected(fresh_db: str) -> None:
    await init_db()
    await _seed_with_role()
    server = _server_for("p2")
    err = _err_text(await _handler(server, "role_complete")({
        "task_id": "t-2026-05-07-cccc3333",
        "message_to_coach": "shipped",
        "artifact_path": "knowledge/reports/never_written.md",
    }))
    assert "does not exist" in err.lower()


async def test_role_complete_happy_path_no_artifact(fresh_db: str) -> None:
    await init_db()
    await _seed_with_role()

    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("p2")
        text = _ok_text(await _handler(server, "role_complete")({
            "task_id": "t-2026-05-07-cccc3333",
            "message_to_coach": "Decision recorded; nothing to ship.",
        }))
        assert "executor" in text.lower()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while True:
            try:
                captured.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    completed = [
        e for e in captured if e.get("type") == "task_role_completed"
    ]
    assert completed and completed[0]["role"] == "executor"
    assert completed[0]["owner"] == "p2"
    assert completed[0]["message_to_coach"].startswith("Decision recorded")

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'executor'",
            ("t-2026-05-07-cccc3333",),
        )
        r = dict(await cur.fetchone())
        assert r["completed_at"]
    finally:
        await c.close()


async def test_role_complete_happy_path_with_artifact(fresh_db: str) -> None:
    """Artifact gate happy path — file exists under project root, gets
    appended to tasks.artifacts."""
    await init_db()
    await _seed_with_role()
    # Place a file under the project root so the gate passes.
    from server.paths import project_paths
    pp = project_paths("misc")
    pp.root.mkdir(parents=True, exist_ok=True)
    (pp.root / "report.md").write_text("done\n", encoding="utf-8")

    server = _server_for("p2")
    text = _ok_text(await _handler(server, "role_complete")({
        "task_id": "t-2026-05-07-cccc3333",
        "message_to_coach": "saved the report",
        "artifact_path": "report.md",
    }))
    assert "report.md" in text

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT artifacts FROM tasks WHERE id = ?",
            ("t-2026-05-07-cccc3333",),
        )
        t = dict(await cur.fetchone())
        artifacts = json.loads(t["artifacts"] or "[]")
        assert "report.md" in artifacts
    finally:
        await c.close()


async def test_role_complete_infers_shipper_at_ship_stage(fresh_db: str) -> None:
    await init_db()
    await _seed_with_role(status="ship", role="shipper")
    server = _server_for("p2")
    text = _ok_text(await _handler(server, "role_complete")({
        "task_id": "t-2026-05-07-cccc3333",
        "message_to_coach": "merged + tagged",
    }))
    assert "shipper" in text.lower()

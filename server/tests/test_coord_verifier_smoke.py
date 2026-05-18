from __future__ import annotations

import json
from typing import Any

from server.db import configured_conn, init_db
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


async def _seed_task(
    *,
    task_id: str = "t-2026-05-17-6d918984",
    status: str = "verify",
    verifier: str = "p4",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) VALUES (?, 'misc', 'verify smoke', "
            "?, 'p3', 'coach', '[]')",
            (task_id, status),
        )
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, claimed_at) "
            "VALUES (?, 'verifier', '[]', ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (task_id, verifier),
        )
        await c.execute(
            "UPDATE agents SET current_task_id = ?, allowed_tools = ? WHERE id = ?",
            (
                task_id,
                json.dumps([
                    "mcp__coord__coord_run_verifier_smoke",
                    "mcp__coord__coord_submit_verification_report",
                ]),
                verifier,
            ),
        )
        await c.commit()
    finally:
        await c.close()


async def test_active_verifier_can_run_allowlisted_smoke(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task()

    server = _server_for("p4")
    text = _ok_text(await _handler(server, "run_verifier_smoke")({
        "task_id": "t-2026-05-17-6d918984",
        "target": "local",
        "smoke": "task_board_read",
        "params": {"expected_task_id": "t-2026-05-17-6d918984"},
    }))
    evidence = json.loads(text)

    assert evidence["status"] == "PASS"
    assert evidence["task_id"] == "t-2026-05-17-6d918984"
    assert evidence["observed"]["stage"] == "verify"


async def test_coach_executor_wrong_task_and_wrong_stage_reject(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(verifier="p4")
    await _seed_task(
        task_id="t-2026-05-17-aaaaaaaa",
        status="execute",
        verifier="p5",
    )

    coach_err = _err_text(await _handler(_server_for("coach"), "run_verifier_smoke")({
        "task_id": "t-2026-05-17-6d918984",
        "target": "local",
        "smoke": "health_detail",
    }))
    assert "coach doesn't run verifier smokes" in coach_err.lower()

    wrong_slot = _err_text(await _handler(_server_for("p6"), "run_verifier_smoke")({
        "task_id": "t-2026-05-17-6d918984",
        "target": "local",
        "smoke": "health_detail",
    }))
    assert "no active verifier assignment" in wrong_slot.lower()

    wrong_task = _err_text(await _handler(_server_for("p4"), "run_verifier_smoke")({
        "task_id": "t-2026-05-17-aaaaaaaa",
        "target": "local",
        "smoke": "health_detail",
    }))
    assert "verification is not active" in wrong_task.lower()


async def test_tool_rejects_arbitrary_request_shape(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task()

    err = _err_text(await _handler(_server_for("p4"), "run_verifier_smoke")({
        "task_id": "t-2026-05-17-6d918984",
        "target": "local",
        "smoke": "health_detail",
        "params": {"url": "https://example.test", "headers": {"x": "y"}},
    }))

    assert "arbitrary request fields" in err.lower()

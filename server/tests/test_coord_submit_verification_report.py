"""Tests for coord_submit_verification_report."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from server.db import configured_conn, init_db
from server.events import bus
from server.tasks import verification_report_path
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


async def _seed_verify_task(
    *,
    status: str = "verify",
    verifier: str = "p4",
    task_id: str = "t-2026-05-16-abcdef01",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) VALUES (?, 'misc', 'verify demo', "
            "?, 'p2', 'coach', ?)",
            (
                task_id,
                status,
                json.dumps([
                    {"stage": "execute", "to": ["p2"]},
                    {"stage": "ship", "to": ["p3"]},
                    {"stage": "verify", "to": [verifier]},
                ]),
            ),
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
            "UPDATE agents SET current_task_id = ?, allowed_tools = ? "
            "WHERE id = ?",
            (
                task_id,
                json.dumps(["mcp__coord__coord_submit_verification_report"]),
                verifier,
            ),
        )
        await c.commit()
    finally:
        await c.close()


async def test_submit_verification_report_records_artifact_and_events(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_verify_task()

    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("p4")
        text = _ok_text(await _handler(server, "submit_verification_report")({
            "task_id": "t-2026-05-16-abcdef01",
            "verdict": "pass",
            "body": "## Checks\n- Dev deployment responds 200.\n",
            "message_to_coach": "verified dev deployment",
            "evidence": {"url": "https://dev.example.test", "sha": "abc123"},
        }))
        assert "verification report" in text.lower()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while True:
            try:
                captured.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    report = verification_report_path("misc", "t-2026-05-16-abcdef01", 1)
    assert report.exists()
    content = report.read_text(encoding="utf-8")
    assert "verifier: p4" in content
    assert "verdict: pass" in content
    assert "Dev deployment responds 200" in content

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT report_path, verdict, completed_at FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'verifier'",
            ("t-2026-05-16-abcdef01",),
        )
        role = dict(await cur.fetchone())
        cur = await c.execute("SELECT status FROM tasks WHERE id = ?",
                              ("t-2026-05-16-abcdef01",))
        task = dict(await cur.fetchone())
        cur = await c.execute("SELECT allowed_tools FROM agents WHERE id = 'p4'")
        agent = dict(await cur.fetchone())
    finally:
        await c.close()

    assert role["report_path"].endswith("/verifications/verification_1.md")
    assert role["verdict"] == "pass"
    assert role["completed_at"]
    assert task["status"] == "verify"
    assert "mcp__coord__coord_my_assignments" in set(json.loads(agent["allowed_tools"]))

    types = [e.get("type") for e in captured]
    assert "task_role_completed" in types
    assert "verification_report_submitted" in types
    vr = next(e for e in captured if e.get("type") == "verification_report_submitted")
    assert vr["verdict"] == "pass"
    assert vr["report_path"] == role["report_path"]
    assert vr["verifier_id"] == "p4"


async def test_submit_verification_report_rejects_wrong_stage(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_verify_task(status="ship")

    server = _server_for("p4")
    err = _err_text(await _handler(server, "submit_verification_report")({
        "task_id": "t-2026-05-16-abcdef01",
        "verdict": "pass",
        "body": "looks good",
    }))
    assert "verification is not active" in err.lower()
    assert "ship" in err.lower()


async def test_submit_verification_report_requires_active_verifier_assignment(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_verify_task(verifier="p4")

    server = _server_for("p5")
    err = _err_text(await _handler(server, "submit_verification_report")({
        "task_id": "t-2026-05-16-abcdef01",
        "verdict": "fail",
        "body": "deployment is down",
    }))
    assert "no active verifier assignment" in err.lower()
    assert "p5" in err


async def test_submit_verification_report_rejects_exact_live_secret(
    fresh_db: str,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HARNESS_TOKEN", "live-harness-token")
    await init_db()
    await _seed_verify_task()

    server = _server_for("p4")
    err = _err_text(await _handler(server, "submit_verification_report")({
        "task_id": "t-2026-05-16-abcdef01",
        "verdict": "pass",
        "body": "token live-harness-token should not be here",
    }))
    assert "contains live harness secret material" in err.lower()

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'verifier'",
            ("t-2026-05-16-abcdef01",),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["completed_at"] is None


async def test_submit_verification_report_redacts_bearer_and_cookie_material(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_verify_task()

    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("p4")
        _ok_text(await _handler(server, "submit_verification_report")({
            "task_id": "t-2026-05-16-abcdef01",
            "verdict": "pass",
            "body": (
                "Authorization: Bearer rawbearersecret\n"
                "Cookie: sid=rawcookie\n"
                "Request header Cookie: sid=rawinlinebodycookie\n"
            ),
            "message_to_coach": (
                "Bearer rawcoachbearer; "
                "curl -H 'Cookie: sid=rawmessagecookie' https://example"
            ),
            "evidence": {
                "headers": {"Authorization": "Bearer rawevidencebearer"},
                "cookie": "sid=rawevidencecookie",
                "response": "Response Set-Cookie: sid=rawevidenceinlinecookie",
            },
        }))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while True:
            try:
                captured.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    report = verification_report_path("misc", "t-2026-05-16-abcdef01", 1)
    content = report.read_text(encoding="utf-8")
    assert "rawbearersecret" not in content
    assert "rawcookie" not in content
    assert "rawinlinebodycookie" not in content
    assert "rawmessagecookie" not in content
    assert "[REDACTED]" in content

    dumped_events = json.dumps(captured)
    assert "rawcoachbearer" not in dumped_events
    assert "rawmessagecookie" not in dumped_events
    assert "rawevidencebearer" not in dumped_events
    assert "rawevidencecookie" not in dumped_events
    assert "rawevidenceinlinecookie" not in dumped_events

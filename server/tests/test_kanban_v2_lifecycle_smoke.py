"""Phase 10 — kanban v2 end-to-end lifecycle smoke test.

Walks the §20.3 verification checklist against the assembled v2
surface: coord_create_task → planner role → coord_write_task_spec →
coord_approve_stage(execute) → coord_commit_push → coord_approve_stage
(audit_syntax) → coord_submit_audit_report(pass) → coord_approve_stage
(ship) → coord_role_complete → coord_archive_task. Plus the audit-
FAIL-no-revert + pool-discipline cross-checks.

Exercises the MCP tools via build_coord_server and asserts the
project_events table reflects the full stream of v2 events. Does NOT
spawn agents — runs entirely against the harness's data plane.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import server.agents as agents_mod
import server.tools as tools_mod
from server.db import configured_conn, init_db
from server.tools import build_coord_server


@pytest.fixture
def stub_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub project_repo_configured + workspace_dir + subprocess.run so
    coord_commit_push doesn't hit a real git tree. Mirrors the pattern
    in test_coord_commit_push_gate.py."""
    cwd = tmp_path / "p3" / "project"
    (cwd / ".git").mkdir(parents=True)

    async def _configured() -> bool:
        return True

    async def _workspace_dir(_slot: str) -> Path:
        return cwd

    monkeypatch.setattr(tools_mod, "project_repo_configured", _configured)
    monkeypatch.setattr(tools_mod, "workspace_dir", _workspace_dir)

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if cmd[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(cmd, 0, "M file.py\n", "")
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, "abc123\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)


_TRAJECTORY = (
    '[{"stage":"plan","to":["p5"]},'
    '{"stage":"execute","to":["p3"]},'
    '{"stage":"audit_syntax","to":["p4"],"focus":"sound"},'
    '{"stage":"ship","to":["p3"]}]'
)


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok(result: dict[str, Any]) -> str:
    assert not result.get("isError"), (
        f"tool returned error: {result.get('content')}"
    )
    return result["content"][0]["text"]


async def _project_event_types(project_id: str) -> list[str]:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT type FROM project_events "
            "WHERE project_id = ? ORDER BY id ASC",
            (project_id,),
        )
        return [dict(r)["type"] for r in await cur.fetchall()]
    finally:
        await c.close()


async def _task_status(task_id: str) -> str:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cur.fetchone()
        return dict(row)["status"]
    finally:
        await c.close()


async def _stub_wake(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def _rec(slot: str, prompt: str = "", **kw: Any) -> bool:
        calls.append((slot, prompt))
        return True

    monkeypatch.setattr(agents_mod, "maybe_wake_agent", _rec)
    return calls


# ---------------------------------------------------------------- happy path


async def test_full_v2_lifecycle_smoke(
    fresh_db: str, monkeypatch, stub_git: None,
) -> None:
    """End-to-end walk of §20.3.1: every stage transition, every
    Player completion event, every artifact ends up in project_events."""
    from server.kanban import start_kanban_subscriber, stop_kanban_subscriber
    await init_db()
    await _stub_wake(monkeypatch)
    await start_kanban_subscriber()

    coach = _server_for("coach")
    p3 = _server_for("p3")
    p4 = _server_for("p4")
    p5 = _server_for("p5")

    # 1. Coach creates the task. First-stage `to` is single-name → role row plants.
    create = _handler(coach, "create_task")
    res = await create({
        "title": "Build feature X",
        "description": "Spec + commit + audit + ship",
        "trajectory": _TRAJECTORY,
    })
    text = _ok(res)
    # Pull task_id back from the response body.
    import re
    m = re.search(r"t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}", text)
    assert m, f"no task id in response: {text}"
    tid = m.group(0)

    # 2. Planner submits spec with message_to_coach.
    write_spec = _handler(p5, "write_task_spec")
    _ok(await write_spec({
        "task_id": tid,
        "body": "## Goal\nBuild it.\n\n## Acceptance criteria\n- works\n",
        "message_to_coach": "draft is rough — wanted to ship something to react to",
    }))

    # 3. Coach approves plan → execute, hard-assigning p3.
    approve = _handler(coach, "approve_stage")
    _ok(await approve({
        "task_id": tid, "next_stage": "execute", "assignee": "p3",
        "note": "build it; lean on the spec",
    }))
    assert await _task_status(tid) == "execute"

    # 4. Executor commits with message_to_coach.
    commit = _handler(p3, "commit_push")
    _ok(await commit({
        "message": "feat: implement X",
        "task_id": tid,
        "push": False,  # don't actually push in the test
        "message_to_coach": "committed at <sha>; lints + tests pass",
    }))

    # 5. Coach approves execute → audit_syntax with focus.
    _ok(await approve({
        "task_id": tid, "next_stage": "audit_syntax", "assignee": "p4",
        "note": "formal review please; pay attention to error paths",
    }))
    assert await _task_status(tid) == "audit_syntax"

    # 6. Auditor submits PASS verdict.
    submit_audit = _handler(p4, "submit_audit_report")
    _ok(await submit_audit({
        "task_id": tid,
        "kind": "syntax",
        "body": "## Summary\nLooks good.\n",
        "verdict": "pass",
        "message_to_coach": "lgtm — cleanly structured",
    }))

    # 7. Coach approves audit_syntax → ship.
    _ok(await approve({
        "task_id": tid, "next_stage": "ship", "assignee": "p3",
        "note": "ship it",
    }))
    assert await _task_status(tid) == "ship"

    # 8. Shipper signals role complete.
    role_complete = _handler(p3, "role_complete")
    _ok(await role_complete({
        "task_id": tid,
        "message_to_coach": "shipped to main; done",
    }))
    # Task stays in `ship` until Coach archives explicitly (no auto-archive in v2).
    assert await _task_status(tid) == "ship"

    # 9. Coach archives with a user-facing summary.
    archive = _handler(coach, "archive_task")
    _ok(await archive({
        "task_id": tid,
        "summary": "Delivered feature X. Tests cover error paths. No follow-up needed.",
    }))
    assert await _task_status(tid) == "archive"

    # Give the bus subscriber a moment to drain so project_events
    # rows are flushed before we assert on them.
    import asyncio
    await asyncio.sleep(0.2)

    # ---- Verify project_events stream covers every step ----
    types = await _project_event_types("misc")
    await stop_kanban_subscriber()
    assert "task_stage_changed" in types
    assert "task_spec_written" in types
    assert "task_role_assigned" in types
    assert "commit_pushed" in types
    assert "audit_report_submitted" in types
    assert "task_role_completed" in types
    assert "task_archived" in types


# ---------------------------------------------------------------- audit FAIL


async def test_audit_fail_does_not_auto_revert(
    fresh_db: str, monkeypatch, stub_git: None,
) -> None:
    """v2 §3.2 invariant: audit FAIL records the verdict + emits
    `audit_fail_notification` + writes a `deviations_log` row, but
    does NOT auto-revert. The task stays in audit_syntax until Coach
    explicitly approves a transition back to execute."""
    from server.kanban import start_kanban_subscriber, stop_kanban_subscriber
    await init_db()
    await _stub_wake(monkeypatch)
    await start_kanban_subscriber()

    coach = _server_for("coach")
    p3 = _server_for("p3")
    p4 = _server_for("p4")
    p5 = _server_for("p5")

    create = _handler(coach, "create_task")
    text = _ok(await create({
        "title": "fail-revert demo", "description": "x",
        "trajectory": _TRAJECTORY,
    }))
    import re
    tid = re.search(r"t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}", text).group(0)
    _ok(await _handler(p5, "write_task_spec")({
        "task_id": tid, "body": "## Goal\nx\n",
    }))
    approve = _handler(coach, "approve_stage")
    _ok(await approve({
        "task_id": tid, "next_stage": "execute", "assignee": "p3",
        "note": "go",
    }))
    _ok(await _handler(p3, "commit_push")({
        "message": "wip", "task_id": tid, "push": False,
    }))
    _ok(await approve({
        "task_id": tid, "next_stage": "audit_syntax", "assignee": "p4",
        "note": "review",
    }))
    _ok(await _handler(p4, "submit_audit_report")({
        "task_id": tid, "kind": "syntax",
        "body": "## Summary\nbroken\n", "verdict": "fail",
    }))

    # Drain bus.
    import asyncio
    await asyncio.sleep(0.2)

    # Task did NOT revert to execute on its own.
    assert await _task_status(tid) == "audit_syntax"

    # audit_fail_notification + deviations_log row both fired.
    types = await _project_event_types("misc")
    await stop_kanban_subscriber()
    assert "audit_report_submitted" in types
    assert "audit_fail_notification" in types

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT noticed_at FROM deviations_log WHERE task_id = ?",
            (tid,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()
    assert any(r["noticed_at"] == "audit" for r in rows), rows


# ---------------------------------------------------------------- pool discipline


async def test_pool_to_does_not_plant_role_or_wake(
    fresh_db: str, monkeypatch,
) -> None:
    """v2 §7.1 + §10.1: a multi-name `to` on the first trajectory
    entry is FYI only — no role row plants, no Player wakes. Coach
    must call coord_approve_stage to actually pick one."""
    await init_db()
    wakes = await _stub_wake(monkeypatch)

    coach = _server_for("coach")
    create = _handler(coach, "create_task")
    text = _ok(await create({
        "title": "pool demo", "description": "x",
        "trajectory": (
            '[{"stage":"plan","to":["p3","p7"]},'
            '{"stage":"execute","to":[]}]'
        ),
    }))
    import re
    tid = re.search(r"t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}", text).group(0)

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT role, owner FROM task_role_assignments WHERE task_id = ?",
            (tid,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()
    # No active role row should have an owner — pool entries don't plant.
    for r in rows:
        assert r["owner"] is None, f"pool plant leaked: {r}"

    # No wake fired for either pool member.
    pool_wakes = [s for s, _ in wakes if s in ("p3", "p7")]
    assert pool_wakes == [], f"pool wake fired unexpectedly: {pool_wakes}"

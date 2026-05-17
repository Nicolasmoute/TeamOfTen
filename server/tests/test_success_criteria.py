"""Tests for the `tasks.success_criteria` field — Coach's first-class
"definition of done" captured at coord_create_task / coord_approve_stage
(plan→execute) and surfaced in the auditor wake, the Coach coordination
block, and the coord_approve_stage tool result on advance to ship.

Plan: C:\\Users\\nicol\\.claude\\plans\\go-cozy-thunder.md
"""

from __future__ import annotations

import json
import re
from typing import Any

import server.agents as agents_mod
import server.tools as tools_mod
from server.db import configured_conn, init_db
from server.kanban import build_auditor_wake_body
from server.tools import build_coord_server


# --- helpers ----------------------------------------------------------


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok(result: dict[str, Any]) -> str:
    assert not result.get("is_error"), (
        f"tool returned error: {result.get('content')}"
    )
    return result["content"][0]["text"]


def _extract_task_id(text: str) -> str:
    m = re.search(r"t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}", text)
    assert m, f"no task id in response: {text}"
    return m.group(0)


def _extract_backlog_id(text: str) -> int:
    """Parse 'Backlog entry #N' from a coord_create_task (Coach) response."""
    m = re.search(r"Backlog entry #(\d+)", text)
    assert m, f"no backlog id in response: {text}"
    return int(m.group(1))


async def _create_and_promote(
    coach_server,
    create_args: dict,
) -> str:
    """Two-step helper for the new Coach backlog-first flow:
    1. coord_create_task  → backlog entry (trajectory stored automatically)
    2. coord_triage_backlog action='promote' → kanban task id

    Returns the task_id string ready for subsequent tool calls.
    """
    create_text = _ok(await _handler(coach_server, "create_task")(create_args))
    bid = _extract_backlog_id(create_text)
    promote_text = _ok(await _handler(coach_server, "triage_backlog")({
        "id": str(bid),
        "action": "promote",
        # trajectory/priority/note/success_criteria already stored at creation
    }))
    tid = _extract_task_id(promote_text)
    trajectory = json.loads(create_args["trajectory"])
    first = trajectory[0]
    first_to = first.get("to") or []
    assignee = first_to[0]
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE tasks SET truthgate_verdict = 'truthgate_pass', "
            "truthgate_method = 'manual_record', truth_basis = '[]' "
            "WHERE id = ?",
            (tid,),
        )
        await c.commit()
    finally:
        await c.close()
    _ok(await _handler(coach_server, "approve_stage")({
        "task_id": tid,
        "next_stage": first["stage"],
        "assignee": assignee,
        "note": "test fixture TruthGate pass; dispatch first stage",
    }))
    return tid


async def _stub_wake(monkeypatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def _rec(slot: str, prompt: str = "", **kw: Any) -> bool:
        calls.append((slot, prompt))
        return True

    monkeypatch.setattr(agents_mod, "maybe_wake_agent", _rec)
    return calls


async def _read_criteria(task_id: str) -> str:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT success_criteria FROM tasks WHERE id = ?", (task_id,),
        )
        row = await cur.fetchone()
        return (dict(row).get("success_criteria") or "")
    finally:
        await c.close()


_TRAJECTORY_FULL = (
    '[{"stage":"plan","to":["p5"]},'
    '{"stage":"execute","to":["p3"]},'
    '{"stage":"audit_syntax","to":["p4"],"focus":"sound"},'
    '{"stage":"ship","to":["p3"]}]'
)


# --- 1. schema migration ----------------------------------------------


async def test_schema_has_success_criteria_column(fresh_db: str) -> None:
    """Column exists with empty-string default after init_db."""
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute("PRAGMA table_info(tasks)")
        cols = {dict(r)["name"]: dict(r) for r in await cur.fetchall()}
    finally:
        await c.close()
    assert "success_criteria" in cols
    # SQLite stores DEFAULT as a string literal; check it's non-NULL with
    # an empty-string default.
    info = cols["success_criteria"]
    assert info["notnull"] == 1
    assert info["dflt_value"] in ("''", "")


# --- 2. coord_create_task round-trip ----------------------------------


async def test_create_task_with_success_criteria_persists(
    fresh_db: str, monkeypatch,
) -> None:
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    tid = await _create_and_promote(coach, {
        "title": "x",
        "description": "y",
        "trajectory": _TRAJECTORY_FULL,
        "success_criteria": "ships when API returns 200 and one happy-path test exists",
    })
    stored = await _read_criteria(tid)
    assert stored == "ships when API returns 200 and one happy-path test exists"


async def test_create_task_without_success_criteria_defaults_empty(
    fresh_db: str, monkeypatch,
) -> None:
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    tid = await _create_and_promote(coach, {
        "title": "x", "description": "y", "trajectory": _TRAJECTORY_FULL,
    })
    assert await _read_criteria(tid) == ""


# --- 3. coord_approve_stage plan→execute updates -----------------------


async def test_approve_plan_to_execute_updates_criteria(
    fresh_db: str, monkeypatch,
) -> None:
    """Coach can refine success_criteria at the plan→execute moment —
    overwriting any value set at creation time."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    p5 = _server_for("p5")
    tid = await _create_and_promote(coach, {
        "title": "x", "description": "y", "trajectory": _TRAJECTORY_FULL,
        "success_criteria": "initial",
    })
    # Planner writes spec (required gate).
    _ok(await _handler(p5, "write_task_spec")({
        "task_id": tid, "body": "## Goal\nx\n",
    }))
    # Coach approves with refined criteria.
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "execute", "assignee": "p3",
        "note": "go", "success_criteria": "refined after reading spec",
    }))
    assert await _read_criteria(tid) == "refined after reading spec"


async def test_approve_non_plan_transition_ignores_criteria(
    fresh_db: str, monkeypatch,
) -> None:
    """Passing success_criteria at any other transition is a silent
    no-op — the field stays stable across execute/audit/ship."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    p3 = _server_for("p3")
    p5 = _server_for("p5")
    tid = await _create_and_promote(coach, {
        "title": "x", "description": "y", "trajectory": _TRAJECTORY_FULL,
        "success_criteria": "initial",
    })
    _ok(await _handler(p5, "write_task_spec")({
        "task_id": tid, "body": "## Goal\nx\n",
    }))
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "execute", "assignee": "p3",
        "note": "go",
    }))
    # Stub git for commit_push.
    import subprocess
    from pathlib import Path
    cwd = Path(fresh_db).parent / "p3" / "project"
    (cwd / ".git").mkdir(parents=True, exist_ok=True)

    async def _configured() -> bool:
        return True

    async def _workspace_dir(_slot: str) -> Path:
        return cwd

    monkeypatch.setattr(tools_mod, "project_repo_configured", _configured)
    monkeypatch.setattr(tools_mod, "workspace_dir", _workspace_dir)

    def _fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "branch", "--show-current"]:
            return subprocess.CompletedProcess(cmd, 0, "work/p3\n", "")
        if cmd[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(cmd, 0, "M f.py\n", "")
        if cmd[:2] == ["git", "rev-parse"] and "--abbrev-ref" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "work/p3\n", "")
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, "abc\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    _ok(await _handler(p3, "commit_push")({
        "message": "wip", "task_id": tid, "push": False,
    }))
    # Try to "update" criteria at execute→audit_syntax — should be no-op.
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "audit_syntax", "assignee": "p4",
        "note": "review", "success_criteria": "tampered",
    }))
    assert await _read_criteria(tid) == "initial"


# --- 4. ship-stage tool-result echo -----------------------------------


async def test_approve_to_ship_echoes_criteria(
    fresh_db: str, monkeypatch,
) -> None:
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    p3 = _server_for("p3")
    p4 = _server_for("p4")
    p5 = _server_for("p5")

    # Stub git for commit_push.
    import subprocess
    from pathlib import Path
    cwd = Path(fresh_db).parent / "p3" / "project"
    (cwd / ".git").mkdir(parents=True, exist_ok=True)

    async def _configured() -> bool:
        return True

    async def _workspace_dir(_slot: str) -> Path:
        return cwd

    monkeypatch.setattr(tools_mod, "project_repo_configured", _configured)
    monkeypatch.setattr(tools_mod, "workspace_dir", _workspace_dir)

    def _fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "branch", "--show-current"]:
            return subprocess.CompletedProcess(cmd, 0, "work/p3\n", "")
        if cmd[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(cmd, 0, "M f.py\n", "")
        if cmd[:2] == ["git", "rev-parse"] and "--abbrev-ref" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "work/p3\n", "")
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, "abc\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    tid = await _create_and_promote(coach, {
        "title": "x", "description": "y", "trajectory": _TRAJECTORY_FULL,
        "success_criteria": "tests green and 200 on /foo",
    })
    _ok(await _handler(p5, "write_task_spec")({
        "task_id": tid, "body": "## Goal\nx\n",
    }))
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "execute", "assignee": "p3",
        "note": "go",
    }))
    _ok(await _handler(p3, "commit_push")({
        "message": "wip", "task_id": tid, "push": False,
    }))
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "audit_syntax", "assignee": "p4",
        "note": "review",
    }))
    _ok(await _handler(p4, "submit_audit_report")({
        "task_id": tid, "kind": "syntax",
        "body": "## Summary\nlgtm\n", "verdict": "pass",
    }))
    ship_text = _ok(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "ship", "assignee": "p3",
        "note": "ship it",
    }))
    assert "You defined done as: tests green and 200 on /foo" in ship_text


async def test_approve_to_ship_without_criteria_no_echo(
    fresh_db: str, monkeypatch,
) -> None:
    """When criteria is unset the echo line is silent — preserves the
    optional-everywhere posture."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    p3 = _server_for("p3")
    p4 = _server_for("p4")
    p5 = _server_for("p5")

    import subprocess
    from pathlib import Path
    cwd = Path(fresh_db).parent / "p3" / "project"
    (cwd / ".git").mkdir(parents=True, exist_ok=True)

    async def _configured() -> bool:
        return True

    async def _workspace_dir(_slot: str) -> Path:
        return cwd

    monkeypatch.setattr(tools_mod, "project_repo_configured", _configured)
    monkeypatch.setattr(tools_mod, "workspace_dir", _workspace_dir)

    def _fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "branch", "--show-current"]:
            return subprocess.CompletedProcess(cmd, 0, "work/p3\n", "")
        if cmd[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(cmd, 0, "M f.py\n", "")
        if cmd[:2] == ["git", "rev-parse"] and "--abbrev-ref" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "work/p3\n", "")
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, "abc\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    tid = await _create_and_promote(coach, {
        "title": "x", "description": "y", "trajectory": _TRAJECTORY_FULL,
    })
    _ok(await _handler(p5, "write_task_spec")({
        "task_id": tid, "body": "## Goal\nx\n",
    }))
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "execute", "assignee": "p3",
        "note": "go",
    }))
    _ok(await _handler(p3, "commit_push")({
        "message": "wip", "task_id": tid, "push": False,
    }))
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "audit_syntax", "assignee": "p4",
        "note": "review",
    }))
    _ok(await _handler(p4, "submit_audit_report")({
        "task_id": tid, "kind": "syntax",
        "body": "## Summary\nlgtm\n", "verdict": "pass",
    }))
    ship_text = _ok(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "ship", "assignee": "p3",
        "note": "ship it",
    }))
    assert "You defined done as" not in ship_text


# --- 5. auditor wake injection ---------------------------------------


async def test_auditor_wake_injects_acceptance_criteria(
    fresh_db: str,
) -> None:
    """When the task has success_criteria, the auditor's wake body
    contains the `## Coach's acceptance criteria` section right after
    `## Focus`."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, description, "
            "status, owner, created_by, trajectory, success_criteria) "
            "VALUES ('t-x', 'misc', 'demo', 'd', 'audit_syntax', 'p3', "
            "'coach', '[]', 'must pass tests and lint clean')",
        )
        await c.commit()
    finally:
        await c.close()
    body = await build_auditor_wake_body(
        task_id="t-x", role="auditor_syntax",
        focus="check error paths", is_pool=False,
    )
    assert "## Coach's acceptance criteria" in body
    assert "must pass tests and lint clean" in body
    # Order: Focus → Coach's acceptance criteria → Contract.
    focus_at = body.index("## Focus")
    crit_at = body.index("## Coach's acceptance criteria")
    assert focus_at < crit_at


async def test_auditor_wake_no_criteria_no_section(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, description, "
            "status, owner, created_by, trajectory) "
            "VALUES ('t-y', 'misc', 'demo', 'd', 'audit_syntax', 'p3', "
            "'coach', '[]')",
        )
        await c.commit()
    finally:
        await c.close()
    body = await build_auditor_wake_body(
        task_id="t-y", role="auditor_syntax",
        focus="check error paths", is_pool=False,
    )
    assert "## Coach's acceptance criteria" not in body


# --- 6. coordination block sub-line ----------------------------------


async def test_coordination_block_renders_done_when_in_window(
    fresh_db: str,
) -> None:
    """Tasks in execute/audit/ship show the `→ done when:` sub-line
    when criteria is set."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, description, "
            "status, owner, created_by, trajectory, success_criteria) "
            "VALUES ('t-exec', 'misc', 'in flight', 'd', 'execute', "
            "'p2', 'coach', '[]', 'all tests pass on CI')",
        )
        await c.commit()
    finally:
        await c.close()
    block = await agents_mod._build_coach_coordination_block()
    assert "t-exec" in block
    assert "→ done when: all tests pass on CI" in block


async def test_coordination_block_no_subline_in_plan_stage(
    fresh_db: str,
) -> None:
    """Plan-stage tasks don't show the sub-line — Coach is still
    deciding the bar; rendering a stale or missing one is noise."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, description, "
            "status, owner, created_by, trajectory, success_criteria) "
            "VALUES ('t-plan', 'misc', 'planning', 'd', 'plan', "
            "'p5', 'coach', '[]', 'all tests pass on CI')",
        )
        await c.commit()
    finally:
        await c.close()
    block = await agents_mod._build_coach_coordination_block()
    assert "t-plan" in block
    assert "→ done when:" not in block


async def test_coordination_block_truncates_long_criteria(
    fresh_db: str,
) -> None:
    """Long criteria strings get truncated at ~120 chars to keep the
    coordination block compact."""
    await init_db()
    long_crit = "A" * 200
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, description, "
            "status, owner, created_by, trajectory, success_criteria) "
            "VALUES ('t-long', 'misc', 'long', 'd', 'execute', "
            "'p2', 'coach', '[]', ?)",
            (long_crit,),
        )
        await c.commit()
    finally:
        await c.close()
    block = await agents_mod._build_coach_coordination_block()
    assert "→ done when: " + ("A" * 117) + "..." in block


async def test_coordination_block_no_subline_when_criteria_empty(
    fresh_db: str,
) -> None:
    """Default empty-string criteria ⇒ no sub-line for tasks even in
    the visible window."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, description, "
            "status, owner, created_by, trajectory) "
            "VALUES ('t-empty', 'misc', 'empty', 'd', 'execute', "
            "'p2', 'coach', '[]')",
        )
        await c.commit()
    finally:
        await c.close()
    block = await agents_mod._build_coach_coordination_block()
    assert "t-empty" in block
    assert "→ done when:" not in block

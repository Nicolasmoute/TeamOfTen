"""v0.3.11 — every kanban tool response and Coach-bound event payload
ends with an imperative "what's next" line.

Backstory: production trace 2026-05-06 surfaced a Player who got a
planner assignment, called coord_my_assignments, saw the pending
plan, and stopped — treating the descriptive response as a status
report. We then audited every kanban-touching tool and every
Coach-bound event, found 15 places with weak/missing follow-ups,
and added imperative call-to-action footers to each.

These tests anchor the new wording so future refactors don't
regress to the old descriptive-only style.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from server.db import configured_conn, init_db
from server.events import bus
from server.tools import build_coord_server


# ---------------------------------------------------------------- helpers

_FULL_TRAJECTORY = (
    '[{"stage":"plan","to":[]},'
    '{"stage":"execute","to":[]},'
    '{"stage":"audit_syntax","to":[]},'
    '{"stage":"audit_semantics","to":[]},'
    '{"stage":"ship","to":[]}]'
)


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"].get(f"coord_{name}") or server["_handlers"].get(name)


def _ok_text(result: dict[str, Any]) -> str:
    assert not result.get("isError"), f"unexpected error: {result}"
    return result["content"][0]["text"]


async def _seed_task(
    *,
    task_id: str,
    status: str,
    owner: str | None = None,
    spec_path: str | None = None,
    trajectory: str = _FULL_TRAJECTORY,
    title: str = "demo",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, spec_path) "
            "VALUES (?, 'misc', ?, ?, ?, 'coach', ?, ?)",
            (task_id, title, status, owner, trajectory, spec_path),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_role(
    *, task_id: str, role: str, owner: str | None = None,
    eligible: list[str] | None = None,
) -> None:
    import json as _json
    elig = _json.dumps(eligible or [])
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, claimed_at) "
            "VALUES (?, ?, ?, ?, '2026-05-06T10:00:00Z', ?)",
            (
                task_id, role, elig, owner,
                "2026-05-06T10:00:00Z" if owner else None,
            ),
        )
        await c.commit()
    finally:
        await c.close()


def _drain(queue: Any) -> list[dict]:
    out: list[dict] = []
    while True:
        try:
            out.append(queue.get_nowait())
        except Exception:
            break
    return out


# ---------------------------------------------------------------- 1. coord_assign_task

async def test_assign_task_hard_assign_includes_no_followup_reminder(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(task_id="t-2026-05-06-00000030", status="plan", spec_path="x")
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "assign_task")({
        "task_id": "t-2026-05-06-00000030",
        "to": "p2",
    }))
    assert "Do NOT follow up with coord_send_message" in text
    assert "auto-wakes p2" in text
    assert "task_completed" in text or "task_stage_stale" in text


async def test_assign_task_pool_includes_first_to_claim_wins(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(task_id="t-2026-05-06-00000031", status="plan", spec_path="x")
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "assign_task")({
        "task_id": "t-2026-05-06-00000031",
        "to": "p1,p2,p3",
    }))
    assert "first to call coord_claim_task wins" in text
    assert "Do NOT follow up" in text


# ---------------------------------------------------------------- 2. coord_assign_planner

async def test_assign_planner_includes_no_followup(fresh_db: str) -> None:
    await init_db()
    await _seed_task(task_id="t-2026-05-06-00000032", status="plan")
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "assign_planner")({
        "task_id": "t-2026-05-06-00000032",
        "to": "p3",
    }))
    assert "auto-wakes p3" in text
    assert "Do NOT follow up" in text


async def test_assign_auditor_future_stage_says_safely_move_on(
    fresh_db: str,
) -> None:
    """Auditor reservation while task is in plan: the wake hasn't fired
    yet (future-stage), so the response says 'safely move on'."""
    await init_db()
    await _seed_task(task_id="t-2026-05-06-00000033", status="plan")
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "assign_auditor")({
        "task_id": "t-2026-05-06-00000033",
        "to": "p4",
        "kind": "syntax",
        "focus": "race conditions",
    }))
    assert "Reserved" in text
    assert "safely move on" in text


# ---------------------------------------------------------------- 3. coord_write_task_spec

async def test_write_task_spec_says_planner_role_complete(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(task_id="t-2026-05-06-00000034", status="plan")
    coach = _server_for("coach")
    await _handler(coach, "assign_planner")({
        "task_id": "t-2026-05-06-00000034", "to": "p3",
    })
    p3 = _server_for("p3")
    text = _ok_text(await _handler(p3, "write_task_spec")({
        "task_id": "t-2026-05-06-00000034",
        "body": "## Goal\nDo it.\n",
    }))
    assert "planner role is now complete" in text
    assert "auto-advances plan" in text
    assert "You're done" in text


# ---------------------------------------------------------------- 4. coord_submit_audit_report

async def test_audit_report_pass_says_role_complete(fresh_db: str) -> None:
    await init_db()
    await _seed_task(
        task_id="t-2026-05-06-00000035", status="audit_syntax",
        owner="p2", spec_path="x",
    )
    await _seed_role(
        task_id="t-2026-05-06-00000035", role="auditor_syntax", owner="p4",
    )
    p4 = _server_for("p4")
    text = _ok_text(await _handler(p4, "submit_audit_report")({
        "task_id": "t-2026-05-06-00000035",
        "kind": "syntax",
        "body": "Looks good",
        "verdict": "pass",
    }))
    assert "reviewer role is now complete" in text
    assert "auto-advances" in text


async def test_audit_report_fail_says_executor_re_woken(fresh_db: str) -> None:
    await init_db()
    await _seed_task(
        task_id="t-2026-05-06-00000036", status="audit_syntax",
        owner="p2", spec_path="x",
    )
    await _seed_role(
        task_id="t-2026-05-06-00000036", role="auditor_syntax", owner="p4",
    )
    p4 = _server_for("p4")
    text = _ok_text(await _handler(p4, "submit_audit_report")({
        "task_id": "t-2026-05-06-00000036",
        "kind": "syntax",
        "body": "Issues...",
        "verdict": "fail",
    }))
    assert "reverts the task to execute" in text
    assert "re-wakes the executor" in text
    assert "stall threshold" in text


# ---------------------------------------------------------------- 5+6. commit_push / complete_execution

async def test_complete_execution_says_role_done(fresh_db: str) -> None:
    await init_db()
    await _seed_task(
        task_id="t-2026-05-06-00000037", status="execute",
        owner="p2", spec_path="x",
    )
    await _seed_role(
        task_id="t-2026-05-06-00000037", role="executor", owner="p2",
    )
    p2 = _server_for("p2")
    text = _ok_text(await _handler(p2, "complete_execution")({
        "task_id": "t-2026-05-06-00000037",
        "summary": "research delivered",
    }))
    assert "executor role is now complete" in text
    assert "auto-advances execute" in text
    assert "audit fails" in text or "re-woken" in text


@pytest.fixture
def stub_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import server.tools as tools_mod
    cwd = tmp_path / "p2" / "project"
    (cwd / ".git").mkdir(parents=True)

    async def _configured() -> bool:
        return True

    async def _workspace_dir(_slot: str) -> Path:
        return cwd

    monkeypatch.setattr(tools_mod, "project_repo_configured", _configured)
    monkeypatch.setattr(tools_mod, "workspace_dir", _workspace_dir)
    return cwd


def _stub_subprocess(
    monkeypatch: pytest.MonkeyPatch, push_returncode: int = 0,
) -> None:
    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if cmd[:2] == ["git", "add"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(cmd, 0, "M file.py\n", "")
        if cmd[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, "abc1234\n", "")
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(
                cmd, push_returncode, "",
                "rejected" if push_returncode else "",
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", _fake_run)


async def test_commit_push_with_task_id_says_role_done(
    fresh_db: str, stub_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_subprocess(monkeypatch, push_returncode=0)
    await init_db()
    await _seed_task(
        task_id="t-2026-05-06-00000038", status="execute",
        owner="p2", spec_path="x",
    )
    await _seed_role(
        task_id="t-2026-05-06-00000038", role="executor", owner="p2",
    )
    p2 = _server_for("p2")
    text = _ok_text(await _handler(p2, "commit_push")({
        "message": "fix bug",
        "task_id": "t-2026-05-06-00000038",
    }))
    assert "Linked to task" in text
    assert "executor role is now complete" in text
    assert "auto-advances execute" in text


# ---------------------------------------------------------------- 7. coord_mark_shipped

async def test_mark_shipped_says_coach_will_summarize(fresh_db: str) -> None:
    await init_db()
    await _seed_task(
        task_id="t-2026-05-06-00000039", status="ship", owner="p2",
        spec_path="x",
    )
    await _seed_role(
        task_id="t-2026-05-06-00000039", role="shipper", owner="p4",
    )
    p4 = _server_for("p4")
    text = _ok_text(await _handler(p4, "mark_shipped")({
        "task_id": "t-2026-05-06-00000039",
    }))
    assert "auto-archives" in text
    assert "summarize" in text or "summarise" in text
    assert "shipper role is complete" in text


# ---------------------------------------------------------------- 8. coord_create_task

async def test_create_task_response_names_auto_wake(
    fresh_db: str,
) -> None:
    await init_db()
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "create_task")({
        "title": "demo",
        "trajectory": [
            {"stage": "plan", "to": "p3"},
            {"stage": "execute", "to": "p2"},
        ],
    }))
    assert "auto-wakes" in text
    assert "p3" in text or "first-stage" in text
    assert "Do NOT follow up" in text


async def test_create_task_with_no_owner_warns_about_idle_poller(
    fresh_db: str,
) -> None:
    await init_db()
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "create_task")({
        "title": "no-owner demo",
        "trajectory": [{"stage": "execute", "to": []}],
    }))
    assert "WARNING" in text
    assert "no eligible owners" in text


# ---------------------------------------------------------------- 9. coord_update_task

async def test_update_task_cancelled_says_no_auto_summary(fresh_db: str) -> None:
    """coord_update_task rejects direct manual archive (forces use of
    coord_advance_task_stage); the legacy `cancelled` alias maps to
    archive though, so we use that path."""
    await init_db()
    await _seed_task(
        task_id="t-2026-05-06-00000040", status="execute", owner="p2",
        spec_path="x",
    )
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "update_task")({
        "task_id": "t-2026-05-06-00000040",
        "status": "cancelled",
    }))
    assert "NO auto-summary" in text or "No auto-summary" in text
    assert "you decide what to tell the user" in text


# ---------------------------------------------------------------- 10. coord_advance_task_stage

async def test_advance_task_stage_archive_says_no_auto_summary(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(
        task_id="t-2026-05-06-00000041", status="execute", owner="p2",
        spec_path="x",
    )
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "advance_task_stage")({
        "task_id": "t-2026-05-06-00000041",
        "stage": "archive",
    }))
    assert "NO auto-summary" in text or "No auto-summary" in text
    assert "you decided to kill" in text


# ---------------------------------------------------------------- 11. coord_set_task_trajectory

async def test_set_task_trajectory_says_displaced_get_stand_down(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(task_id="t-2026-05-06-00000042", status="plan")
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "set_task_trajectory")({
        "task_id": "t-2026-05-06-00000042",
        "trajectory": [
            {"stage": "plan", "to": []},
            {"stage": "execute", "to": []},
            {"stage": "audit_semantics", "to": []},
        ],
    }))
    assert "stand-down wake" in text
    assert "Do NOT follow up" in text


# ---------------------------------------------------------------- 12. coord_set_task_blocked

async def test_set_task_blocked_true_says_sweeper_ignores(fresh_db: str) -> None:
    await init_db()
    await _seed_task(
        task_id="t-2026-05-06-00000043", status="execute", owner="p2",
        spec_path="x",
    )
    server = _server_for("coach")
    text = _ok_text(await _handler(server, "set_task_blocked")({
        "task_id": "t-2026-05-06-00000043",
        "blocked": "true",
        "reason": "waiting on external dep",
    }))
    assert "stall sweeper now ignores" in text
    assert "blocked=false" in text  # fix path


async def test_set_task_blocked_false_says_ladder_restarts(fresh_db: str) -> None:
    await init_db()
    await _seed_task(
        task_id="t-2026-05-06-00000044", status="execute", owner="p2",
        spec_path="x",
    )
    server = _server_for("coach")
    # Set blocked first.
    await _handler(server, "set_task_blocked")({
        "task_id": "t-2026-05-06-00000044", "blocked": "true",
    })
    text = _ok_text(await _handler(server, "set_task_blocked")({
        "task_id": "t-2026-05-06-00000044", "blocked": "false",
    }))
    assert "ladder restarts" in text


# ---------------------------------------------------------------- 13. stage_assignment_needed body

async def test_stage_assignment_needed_event_has_imperative_body(
    fresh_db: str,
) -> None:
    """The event payload now includes a `body` field with the
    coord_assign_<role> tool call template baked in."""
    from server.kanban import _emit_assignment_needed
    await init_db()
    queue = bus.subscribe()
    try:
        await _emit_assignment_needed(
            task_id="t-2026-05-06-00000045", role="auditor_semantics",
            stage="audit_semantics", to_owner=None,
        )
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)
    e = next(e for e in events if e.get("type") == "stage_assignment_needed")
    body = e.get("body", "")
    assert "coord_assign_auditor" in body
    assert "kind='semantics'" in body
    assert "t-2026-05-06-00000045" in body
    assert "coord_set_task_trajectory" in body  # rewrite-path


# ---------------------------------------------------------------- 14. commit_without_task_id_warning body

async def test_commit_without_task_id_warning_has_imperative_body(
    fresh_db: str, stub_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_subprocess(monkeypatch, push_returncode=0)
    await init_db()
    # No task seeded — caller has no active executor task and didn't
    # pass task_id, so the warning fires.
    p2 = _server_for("p2")
    queue = bus.subscribe()
    try:
        await _handler(p2, "commit_push")({"message": "scratch"})
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)
    e = next(
        e for e in events
        if e.get("type") == "commit_without_task_id_warning"
    )
    body = e.get("body", "")
    assert "p2" in body
    assert "coord_advance_task_stage" in body
    assert "scratch" in body or "ignore" in body


# ---------------------------------------------------------------- 15. audit_fail_notification body

async def test_audit_fail_notification_first_fail_body_is_calm(
    fresh_db: str,
) -> None:
    """First fail of any kind: body says 'expected correction noise,
    no action needed yet, watch for round 2'."""
    from server.kanban import _emit_audit_fail_notification
    await init_db()
    queue = bus.subscribe()
    try:
        await _emit_audit_fail_notification(
            task_id="t-2026-05-06-00000046", kind="syntax", kind_round=1,
            escalate=False, auditor_id="p4", executor_id="p2",
            report_path="audit_1_syntax.md",
        )
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)
    e = next(e for e in events if e.get("type") == "audit_fail_notification")
    body = e.get("body", "")
    assert "expected correction noise" in body
    assert "no action needed" in body
    assert "round 2" in body
    # The escalation pointer is NOT present on first fail.
    assert "coord_set_player_effort" not in body


async def test_audit_fail_notification_escalation_body_names_bump_ladder(
    fresh_db: str,
) -> None:
    from server.kanban import _emit_audit_fail_notification
    await init_db()
    queue = bus.subscribe()
    try:
        await _emit_audit_fail_notification(
            task_id="t-2026-05-06-00000047", kind="semantics",
            kind_round=2, escalate=True, auditor_id="p4",
            executor_id="p2", report_path="audit_2_semantics.md",
        )
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)
    e = next(e for e in events if e.get("type") == "audit_fail_notification")
    body = e.get("body", "")
    assert "ESCALATION" in body
    assert "coord_set_player_effort" in body
    assert "coord_set_player_model" in body
    assert "p2" in body
    assert "NEVER change runtime" in body

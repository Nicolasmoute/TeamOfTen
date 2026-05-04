"""Tests for the kanban auto-advance subscriber.

The subscriber lives in `server.kanban` and watches the bus for four
event types. We exercise its handlers directly (`_on_commit_pushed`,
`_on_audit_submitted`, `_on_task_shipped`, `_on_compass_audit_logged`)
to keep the tests deterministic — no need to spin up the full
asyncio queue plumbing for unit verification.
"""

from __future__ import annotations

import os

from server.db import configured_conn, init_db
from server.kanban import (
    _flag_enabled,
    _on_audit_submitted,
    _on_commit_pushed,
    _on_compass_audit_logged,
    _on_spec_written,
    _on_task_shipped,
    _recent_commit_task,
)


# v0.3 trajectories. Each element is a {stage, to} dict per the new schema.
# `[execute, audit_syntax, audit_semantics, ship]` is the standard full path
# (no `plan` because tests seed without spec_path).
_STANDARD_TRAJECTORY = (
    '[{"stage":"execute","to":[]},'
    '{"stage":"audit_syntax","to":[]},'
    '{"stage":"audit_semantics","to":[]},'
    '{"stage":"ship","to":[]}]'
)
_SIMPLE_TRAJECTORY = '[{"stage":"execute","to":[]}]'
_SEMANTIC_ONLY_TRAJECTORY = (
    '[{"stage":"execute","to":[]},'
    '{"stage":"audit_semantics","to":[]},'
    '{"stage":"ship","to":[]}]'
)
_FORMAL_ONLY_TRAJECTORY = (
    '[{"stage":"execute","to":[]},'
    '{"stage":"audit_syntax","to":[]},'
    '{"stage":"ship","to":[]}]'
)


async def _seed(
    *,
    task_id: str = "t-2026-05-03-abc12345",
    status: str,
    trajectory: str = _STANDARD_TRAJECTORY,
    owner: str | None = None,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) "
            "VALUES (?, 'misc', 't', ?, ?, 'coach', ?)",
            (task_id, status, owner, trajectory),
        )
        await c.commit()
    finally:
        await c.close()


async def _read_status(task_id: str) -> dict:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, archived_at, started_at, "
            "compass_audit_report_path, compass_audit_verdict "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )
        return dict(await cur.fetchone())
    finally:
        await c.close()


# ------------------------------------------------------------
# commit_pushed
# ------------------------------------------------------------

async def test_commit_pushed_standard_advances_to_audit_syntax(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed(status="execute", trajectory=_STANDARD_TRAJECTORY, owner="p3")
    await _on_commit_pushed({
        "type": "commit_pushed",
        "task_id": "t-2026-05-03-abc12345",
        "sha": "8a3f2c0",
    })
    assert (await _read_status("t-2026-05-03-abc12345"))["status"] == "audit_syntax"


async def test_execution_complete_semantic_only_skips_formal(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed(
        status="execute",
        trajectory=_SEMANTIC_ONLY_TRAJECTORY,
        owner="p3",
    )
    from server.kanban import _on_task_execution_completed

    await _on_task_execution_completed({
        "type": "task_execution_completed",
        "task_id": "t-2026-05-03-abc12345",
    })
    assert (await _read_status("t-2026-05-03-abc12345"))["status"] == "audit_semantics"


async def test_commit_pushed_simple_jumps_to_archive(fresh_db: str) -> None:
    await init_db()
    await _seed(
        status="execute", trajectory=_SIMPLE_TRAJECTORY, owner="p3",
    )
    # Mirror the executor's current_task_id so the archive transition
    # has someone to free up.
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET current_task_id = 't-2026-05-03-abc12345' "
            "WHERE id = 'p3'"
        )
        await c.commit()
    finally:
        await c.close()

    await _on_commit_pushed({
        "type": "commit_pushed",
        "task_id": "t-2026-05-03-abc12345",
        "sha": "8a3f2c1",
    })
    row = await _read_status("t-2026-05-03-abc12345")
    assert row["status"] == "archive"
    assert row["archived_at"] is not None

    # Owner's current_task_id is cleared.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT current_task_id FROM agents WHERE id = 'p3'"
        )
        agent = dict(await cur.fetchone())
    finally:
        await c.close()
    assert agent["current_task_id"] is None


async def test_commit_pushed_without_task_id_is_noop(fresh_db: str) -> None:
    await init_db()
    await _seed(status="execute", owner="p3")
    await _on_commit_pushed({
        "type": "commit_pushed",
        "sha": "8a3f2c2",
        # task_id absent
    })
    assert (await _read_status("t-2026-05-03-abc12345"))["status"] == "execute"


async def test_commit_pushed_caches_sha_to_task(fresh_db: str) -> None:
    """Cache enables compass_audit_logged correlation back to the task."""
    await init_db()
    await _seed(status="execute", owner="p3")
    _recent_commit_task.clear()
    await _on_commit_pushed({
        "type": "commit_pushed",
        "task_id": "t-2026-05-03-abc12345",
        "sha": "deadbee",
    })
    assert _recent_commit_task["deadbee"] == "t-2026-05-03-abc12345"


async def test_commit_pushed_wrong_stage_is_noop(fresh_db: str) -> None:
    """Commit landing on a task that's already in audit_syntax (e.g.
    a follow-up commit during an audit) doesn't double-advance."""
    await init_db()
    await _seed(status="audit_syntax", owner="p3")
    await _on_commit_pushed({
        "type": "commit_pushed",
        "task_id": "t-2026-05-03-abc12345",
        "sha": "abc1234",
    })
    assert (await _read_status("t-2026-05-03-abc12345"))["status"] == "audit_syntax"


# ------------------------------------------------------------
# audit_report_submitted
# ------------------------------------------------------------

async def test_audit_pass_syntax_advances_to_semantics(fresh_db: str) -> None:
    await init_db()
    await _seed(status="audit_syntax", owner="p3")
    await _on_audit_submitted({
        "type": "audit_report_submitted",
        "task_id": "t-2026-05-03-abc12345",
        "kind": "syntax",
        "verdict": "pass",
        "report_path": "x",
        "round": 1,
    })
    assert (await _read_status("t-2026-05-03-abc12345"))["status"] == "audit_semantics"


async def test_formal_only_review_routes_to_ship(fresh_db: str) -> None:
    await init_db()
    await _seed(
        status="audit_syntax",
        trajectory=_FORMAL_ONLY_TRAJECTORY,
        owner="p3",
    )
    await _on_audit_submitted({
        "type": "audit_report_submitted",
        "task_id": "t-2026-05-03-abc12345",
        "kind": "syntax",
        "verdict": "pass",
        "round": 1,
    })
    assert (await _read_status("t-2026-05-03-abc12345"))["status"] == "ship"


async def test_audit_pass_semantics_advances_to_ship(fresh_db: str) -> None:
    await init_db()
    await _seed(status="audit_semantics", owner="p3")
    await _on_audit_submitted({
        "type": "audit_report_submitted",
        "task_id": "t-2026-05-03-abc12345",
        "kind": "semantics",
        "verdict": "pass",
        "round": 1,
    })
    assert (await _read_status("t-2026-05-03-abc12345"))["status"] == "ship"


async def test_audit_fail_reverts_to_execute_resets_started_at(
    fresh_db: str,
) -> None:
    await init_db()
    # Seed a task already in audit_syntax with started_at populated
    # (the executor was actively working before submitting for review).
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, started_at) "
            "VALUES ('t-2026-05-03-abc12345', 'misc', 't', "
            "'audit_syntax', 'p3', 'coach', ?, "
            "'2026-05-01T10:00:00Z')",
            (_STANDARD_TRAJECTORY,),
        )
        await c.commit()
    finally:
        await c.close()
    await _on_audit_submitted({
        "type": "audit_report_submitted",
        "task_id": "t-2026-05-03-abc12345",
        "kind": "syntax",
        "verdict": "fail",
        "report_path": "audits/audit_1_syntax.md",
        "round": 1,
    })
    row = await _read_status("t-2026-05-03-abc12345")
    assert row["status"] == "execute"
    # started_at reset on fail revert so the card flips to
    # "assigned, not started" until the executor's auto-wake fires.
    assert row["started_at"] is None


async def test_audit_for_wrong_stage_is_noop(fresh_db: str) -> None:
    """An `audit_report_submitted` arriving when the task has already
    moved past the matching audit stage (force-advance, cancel) is
    dropped — we don't undo a Coach override."""
    await init_db()
    await _seed(status="ship", owner="p3")
    await _on_audit_submitted({
        "type": "audit_report_submitted",
        "task_id": "t-2026-05-03-abc12345",
        "kind": "syntax",
        "verdict": "fail",
    })
    # Stage unchanged.
    assert (await _read_status("t-2026-05-03-abc12345"))["status"] == "ship"


async def test_audit_invalid_kind_is_noop(fresh_db: str) -> None:
    await init_db()
    await _seed(status="audit_syntax", owner="p3")
    await _on_audit_submitted({
        "type": "audit_report_submitted",
        "task_id": "t-2026-05-03-abc12345",
        "kind": "garbage",
        "verdict": "pass",
    })
    assert (await _read_status("t-2026-05-03-abc12345"))["status"] == "audit_syntax"


# ------------------------------------------------------------
# task_shipped
# ------------------------------------------------------------

async def test_task_shipped_archives(fresh_db: str) -> None:
    await init_db()
    await _seed(status="ship", owner="p3")
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET current_task_id = 't-2026-05-03-abc12345' "
            "WHERE id = 'p3'"
        )
        await c.commit()
    finally:
        await c.close()
    await _on_task_shipped({
        "type": "task_shipped",
        "task_id": "t-2026-05-03-abc12345",
        "shipper_id": "p3",
    })
    row = await _read_status("t-2026-05-03-abc12345")
    assert row["status"] == "archive"
    assert row["archived_at"] is not None


async def test_task_shipped_wrong_stage_is_noop(fresh_db: str) -> None:
    """If the task isn't in `ship` yet (Coach hasn't advanced it past
    audit), the shipped event doesn't force the transition."""
    await init_db()
    await _seed(status="execute", owner="p3")
    await _on_task_shipped({
        "type": "task_shipped",
        "task_id": "t-2026-05-03-abc12345",
    })
    assert (await _read_status("t-2026-05-03-abc12345"))["status"] == "execute"


# ------------------------------------------------------------
# compass_audit_logged (informational)
# ------------------------------------------------------------

async def test_compass_audit_logged_attaches_to_recent_commit_task(
    fresh_db: str,
) -> None:
    """compass_audit_logged populates the task's compass_audit_*
    columns by correlating to the most-recently-cached commit_pushed."""
    await init_db()
    await _seed(status="audit_semantics", owner="p3")
    _recent_commit_task.clear()
    _recent_commit_task["8a3f2c0"] = "t-2026-05-03-abc12345"
    await _on_compass_audit_logged({
        "type": "compass_audit_logged",
        "audit_id": "audit_1700",
        "verdict": "aligned",
        "report_path": "projects/misc/working/compass/audit_reports/audit_1700.md",
    })
    row = await _read_status("t-2026-05-03-abc12345")
    assert row["compass_audit_verdict"] == "aligned"
    assert row["compass_audit_report_path"].endswith("/audit_1700.md")


async def test_compass_audit_logged_does_not_change_stage(fresh_db: str) -> None:
    """Compass is informational — verdict 'confident_drift' must NOT
    revert the task. Only the assigned Player auditor's verdict moves
    the kanban (verified by the audit_report_submitted handlers)."""
    await init_db()
    await _seed(status="audit_semantics", owner="p3")
    _recent_commit_task.clear()
    _recent_commit_task["8a3f2c1"] = "t-2026-05-03-abc12345"
    await _on_compass_audit_logged({
        "type": "compass_audit_logged",
        "audit_id": "audit_1701",
        "verdict": "confident_drift",
        "report_path": "x",
    })
    # Stage unchanged.
    assert (await _read_status("t-2026-05-03-abc12345"))["status"] == "audit_semantics"


async def test_compass_audit_logged_without_recent_commit_is_noop(
    fresh_db: str,
) -> None:
    """If no commit_pushed has been seen since boot, Compass results
    have nothing to attach to — the .md is still on disk (audit_work
    wrote it) but the task's columns stay NULL."""
    await init_db()
    await _seed(status="audit_semantics", owner="p3")
    _recent_commit_task.clear()
    await _on_compass_audit_logged({
        "type": "compass_audit_logged",
        "audit_id": "audit_1702",
        "verdict": "aligned",
        "report_path": "x",
    })
    row = await _read_status("t-2026-05-03-abc12345")
    assert row["compass_audit_verdict"] is None


# ------------------------------------------------------------
# task_spec_written → plan→execute (audit-2026-05-04 item 1 fix)
# ------------------------------------------------------------

_PLAN_EXECUTE_TRAJECTORY = (
    '[{"stage":"plan","to":[]},'
    '{"stage":"execute","to":[]}]'
)


async def test_spec_written_advances_plan_to_execute(fresh_db: str) -> None:
    """coord_write_task_spec emits task_spec_written; the subscriber
    transitions plan-stage tasks to the next trajectory stage."""
    await init_db()
    await _seed(
        status="plan",
        trajectory=_PLAN_EXECUTE_TRAJECTORY,
        owner="p3",
    )
    await _on_spec_written({
        "type": "task_spec_written",
        "task_id": "t-2026-05-03-abc12345",
        "spec_path": "projects/misc/working/tasks/t-2026-05-03-abc12345/spec.md",
    })
    row = await _read_status("t-2026-05-03-abc12345")
    assert row["status"] == "execute"


async def test_spec_written_noop_when_already_past_plan(fresh_db: str) -> None:
    """A re-spec on an executing task does not bump it backwards or
    re-trigger the transition."""
    await init_db()
    await _seed(
        status="execute",
        trajectory=_PLAN_EXECUTE_TRAJECTORY,
        owner="p3",
    )
    await _on_spec_written({
        "type": "task_spec_written",
        "task_id": "t-2026-05-03-abc12345",
        "spec_path": "projects/misc/working/tasks/t-2026-05-03-abc12345/spec.md",
    })
    row = await _read_status("t-2026-05-03-abc12345")
    assert row["status"] == "execute"


async def test_spec_written_noop_when_no_plan_stage(fresh_db: str) -> None:
    """A trajectory without a plan stage means spec writes are
    informational — no transition fires."""
    await init_db()
    await _seed(
        status="plan",
        trajectory=_SIMPLE_TRAJECTORY,  # only execute, no plan
        owner="p3",
    )
    await _on_spec_written({
        "type": "task_spec_written",
        "task_id": "t-2026-05-03-abc12345",
        "spec_path": "x",
    })
    row = await _read_status("t-2026-05-03-abc12345")
    # Status unchanged — the task was in plan because of seed data,
    # not because of a planned route. Nothing for the handler to do.
    assert row["status"] == "plan"


async def test_spec_written_stamps_last_stage_change_at(fresh_db: str) -> None:
    """The transition runs through _transition() which updates
    last_stage_change_at — the stall sweeper relies on this signal."""
    await init_db()
    await _seed(
        status="plan",
        trajectory=_PLAN_EXECUTE_TRAJECTORY,
        owner="p3",
    )
    await _on_spec_written({
        "type": "task_spec_written",
        "task_id": "t-2026-05-03-abc12345",
        "spec_path": "x",
    })
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT last_stage_change_at FROM tasks WHERE id = ?",
            ("t-2026-05-03-abc12345",),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["last_stage_change_at"] is not None


# ------------------------------------------------------------
# v0.3.2 kanban-flow audit gap 1: stage-entry wake names the tool
# ------------------------------------------------------------


async def test_completion_hint_executor_names_commit_push_with_task_id(
    fresh_db: str,
) -> None:
    """The executor wake hint must name coord_commit_push with the
    actual task_id baked in, plus coord_complete_execution. Vague
    'matching completion tool' wording was the #1 cause of Players
    finishing work but the kanban not moving."""
    from server.kanban import _completion_hint_for_role
    await init_db()
    await _seed(
        status="execute",
        trajectory=_STANDARD_TRAJECTORY,
        owner="p3",
    )
    hint = await _completion_hint_for_role(
        "t-2026-05-03-abc12345", "executor"
    )
    assert "coord_commit_push" in hint
    assert "coord_complete_execution" in hint
    assert "t-2026-05-03-abc12345" in hint
    assert "MUST pass" in hint


async def test_completion_hint_executor_includes_self_audit_when_no_audit_stage(
    fresh_db: str,
) -> None:
    """Trajectory with no audit stage after execute → hint reminds
    the executor to self-audit before signalling done."""
    from server.kanban import _completion_hint_for_role
    await init_db()
    await _seed(
        status="execute",
        trajectory=_SIMPLE_TRAJECTORY,  # only execute, no audit
        owner="p3",
    )
    hint = await _completion_hint_for_role(
        "t-2026-05-03-abc12345", "executor"
    )
    assert "SELF-AUDIT" in hint


async def test_completion_hint_executor_omits_self_audit_when_audit_stage_present(
    fresh_db: str,
) -> None:
    """Trajectory with an audit stage → no self-audit reminder
    (the configured auditor is the gate)."""
    from server.kanban import _completion_hint_for_role
    await init_db()
    await _seed(
        status="execute",
        trajectory=_FORMAL_ONLY_TRAJECTORY,
        owner="p3",
    )
    hint = await _completion_hint_for_role(
        "t-2026-05-03-abc12345", "executor"
    )
    assert "SELF-AUDIT" not in hint


async def test_completion_hint_planner_names_write_task_spec(
    fresh_db: str,
) -> None:
    from server.kanban import _completion_hint_for_role
    await init_db()
    await _seed(status="plan", owner=None)
    hint = await _completion_hint_for_role(
        "t-2026-05-03-abc12345", "planner"
    )
    assert "coord_write_task_spec" in hint
    assert "t-2026-05-03-abc12345" in hint


async def test_completion_hint_auditor_names_submit_audit_report(
    fresh_db: str,
) -> None:
    from server.kanban import _completion_hint_for_role
    await init_db()
    await _seed(status="audit_syntax", owner="p3")
    hint = await _completion_hint_for_role(
        "t-2026-05-03-abc12345", "auditor_syntax"
    )
    assert "coord_submit_audit_report" in hint
    assert "kind='syntax'" in hint or 'kind="syntax"' in hint
    assert "t-2026-05-03-abc12345" in hint


async def test_completion_hint_shipper_names_mark_shipped(
    fresh_db: str,
) -> None:
    from server.kanban import _completion_hint_for_role
    await init_db()
    await _seed(status="ship", owner="p3")
    hint = await _completion_hint_for_role(
        "t-2026-05-03-abc12345", "shipper"
    )
    assert "coord_mark_shipped" in hint
    assert "t-2026-05-03-abc12345" in hint


# ------------------------------------------------------------
# audit-2026-05-04 item 12: per-project Compass commit correlation
# ------------------------------------------------------------


async def test_compass_audit_attaches_to_correct_project_commit(
    fresh_db: str,
) -> None:
    """When two projects each commit, the next compass_audit_logged
    must attach to the project named in the event's project_id, not
    to the global tail."""
    import asyncio
    from server import kanban as kanban_mod
    await init_db()
    # Reset module-level caches.
    kanban_mod._recent_commit_task.clear()
    kanban_mod._recent_commit_per_project.clear()
    # Seed two tasks under different projects.
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO projects (id, name) VALUES ('alpha', 'Alpha')"
        )
        await c.execute(
            "INSERT INTO projects (id, name) VALUES ('beta', 'Beta')"
        )
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) VALUES "
            "('t-2026-05-04-aaaaaaaa', 'alpha', 't', 'execute', 'p3', "
            "'coach', '[{\"stage\":\"execute\",\"to\":[]}]')"
        )
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) VALUES "
            "('t-2026-05-04-bbbbbbbb', 'beta', 't', 'execute', 'p3', "
            "'coach', '[{\"stage\":\"execute\",\"to\":[]}]')"
        )
        await c.commit()
    finally:
        await c.close()
    # Both projects commit. Beta is last globally.
    await _on_commit_pushed({
        "type": "commit_pushed",
        "task_id": "t-2026-05-04-aaaaaaaa",
        "sha": "alpha_sha",
        "project_id": "alpha",
    })
    await _on_commit_pushed({
        "type": "commit_pushed",
        "task_id": "t-2026-05-04-bbbbbbbb",
        "sha": "beta_sha",
        "project_id": "beta",
    })
    # Compass logs an audit naming alpha — should NOT attach to beta
    # just because beta was the global tail.
    await kanban_mod._on_compass_audit_logged({
        "type": "compass_audit_logged",
        "audit_id": "audit_alpha",
        "verdict": "aligned",
        "report_path": "alpha_report.md",
        "project_id": "alpha",
    })
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT compass_audit_verdict, compass_audit_report_path "
            "FROM tasks WHERE id = 't-2026-05-04-aaaaaaaa'"
        )
        alpha_row = dict(await cur.fetchone())
        cur = await c.execute(
            "SELECT compass_audit_verdict, compass_audit_report_path "
            "FROM tasks WHERE id = 't-2026-05-04-bbbbbbbb'"
        )
        beta_row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert alpha_row["compass_audit_verdict"] == "aligned"
    assert alpha_row["compass_audit_report_path"] == "alpha_report.md"
    # Beta is untouched — different project_id.
    assert beta_row["compass_audit_verdict"] is None
    assert beta_row["compass_audit_report_path"] is None


# ------------------------------------------------------------
# audit-2026-05-04 item 11: revert wake reads spec_path + criteria
# ------------------------------------------------------------


def test_extract_failed_criteria_from_report(tmp_path) -> None:
    """The helper pulls a `## Failed criteria` section verbatim from
    a markdown audit report. Other sections are not included."""
    from server.kanban import _extract_failed_criteria
    report = tmp_path / "report.md"
    report.write_text(
        "## Summary\n"
        "Looks bad.\n\n"
        "## Failed criteria\n"
        "- Login button is misaligned\n"
        "- Help text overflows on mobile\n\n"
        "## Other notes\n"
        "Will revisit.\n",
        encoding="utf-8",
    )
    out = _extract_failed_criteria(str(report))
    assert "Login button is misaligned" in out
    assert "Help text overflows on mobile" in out
    assert "Other notes" not in out  # next-section marker stops extraction
    assert "Will revisit" not in out


def test_extract_failed_criteria_missing_section_returns_empty(tmp_path) -> None:
    from server.kanban import _extract_failed_criteria
    report = tmp_path / "report.md"
    report.write_text("## Summary\nNo failed-criteria section.", encoding="utf-8")
    assert _extract_failed_criteria(str(report)) == ""


def test_extract_failed_criteria_missing_file_returns_empty() -> None:
    from server.kanban import _extract_failed_criteria
    assert _extract_failed_criteria("does/not/exist.md") == ""
    assert _extract_failed_criteria("") == ""


# ------------------------------------------------------------
# Feature flag
# ------------------------------------------------------------

def test_feature_flag_default_enabled(monkeypatch) -> None:
    """The subscriber is on by default. Setting the env to a falsy
    value disables it."""
    monkeypatch.delenv("HARNESS_KANBAN_AUTO_ADVANCE", raising=False)
    assert _flag_enabled() is True
    monkeypatch.setenv("HARNESS_KANBAN_AUTO_ADVANCE", "false")
    assert _flag_enabled() is False
    monkeypatch.setenv("HARNESS_KANBAN_AUTO_ADVANCE", "0")
    assert _flag_enabled() is False
    monkeypatch.setenv("HARNESS_KANBAN_AUTO_ADVANCE", "true")
    assert _flag_enabled() is True

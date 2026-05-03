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
    _on_task_shipped,
    _recent_commit_task,
)


async def _seed(
    *,
    task_id: str = "t-2026-05-03-abc12345",
    status: str,
    complexity: str = "standard",
    owner: str | None = None,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, complexity) VALUES (?, 'misc', 't', ?, ?, 'coach', ?)",
            (task_id, status, owner, complexity),
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
    await _seed(status="execute", complexity="standard", owner="p3")
    await _on_commit_pushed({
        "type": "commit_pushed",
        "task_id": "t-2026-05-03-abc12345",
        "sha": "8a3f2c0",
    })
    assert (await _read_status("t-2026-05-03-abc12345"))["status"] == "audit_syntax"


async def test_commit_pushed_simple_jumps_to_archive(fresh_db: str) -> None:
    await init_db()
    await _seed(status="execute", complexity="simple", owner="p3")
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
            "created_by, complexity, started_at) "
            "VALUES ('t-2026-05-03-abc12345', 'misc', 't', "
            "'audit_syntax', 'p3', 'coach', 'standard', "
            "'2026-05-01T10:00:00Z')"
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

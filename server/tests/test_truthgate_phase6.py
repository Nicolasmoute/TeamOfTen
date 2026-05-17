from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from server.db import configured_conn, init_db
from server.kanban import build_auditor_wake_body
from server.paths import ensure_project_scaffold
from server.tools import build_coord_server


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok(result: dict[str, Any]) -> str:
    assert not result.get("is_error"), result
    return result["content"][0]["text"]


def _err(result: dict[str, Any]) -> str:
    assert result.get("is_error"), result
    return result["content"][0]["text"]


async def _seed_audit_task(
    *,
    task_id: str = "t-2026-05-16-a0d10001",
    status: str = "audit_syntax",
    role: str = "auditor_syntax",
    auditor: str = "p4",
    basis: list[str] | None = None,
    basis_raw: str | None = None,
    concerns: list[str] | None = None,
    warning: str | None = None,
    method: str = "classifier",
    provisional: bool = False,
    closure_reference: str | None = None,
) -> str:
    pp = ensure_project_scaffold("misc")
    (pp.truth / "audit.md").write_text(
        "# Audit Truth\n\nThe implementation must preserve the approved lifecycle.\n",
        encoding="utf-8",
    )
    truth_basis = basis if basis is not None else ["truth/audit.md"]
    truth_basis_value = basis_raw if basis_raw is not None else json.dumps(truth_basis)
    truth_concerns = concerns if concerns is not None else [
        "preserve the approved lifecycle",
    ]
    c = await configured_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        await c.execute(
            "INSERT INTO tasks "
            "(id, project_id, title, status, owner, created_by, "
            "truthgate_verdict, truth_basis, truth_concerns, "
            "truthgate_at, truthgate_method, truthgate_warning, "
            "provisional, closure_reference, trajectory) "
            "VALUES (?, 'misc', 'audit target', ?, 'p2', "
            "'coach', 'truthgate_pass', ?, ?, ?, ?, ?, ?, ?, "
            "'[{\"stage\":\"execute\",\"to\":[\"p2\"]},"
            "{\"stage\":\"audit_syntax\",\"to\":[\"p4\"]}]')",
            (
                task_id,
                status,
                truth_basis_value,
                json.dumps(truth_concerns),
                now,
                method,
                warning,
                1 if provisional else 0,
                closure_reference,
            ),
        )
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, claimed_at) "
            "VALUES (?, ?, '[]', ?, ?, ?)",
            (task_id, role, auditor, now, now),
        )
        await c.commit()
    finally:
        await c.close()
    return task_id


@pytest.mark.asyncio
async def test_auditor_wake_includes_truthgate_context(fresh_db: str) -> None:
    await init_db()
    task_id = await _seed_audit_task(
        provisional=True,
        closure_reference="amendment:12",
    )

    body = await build_auditor_wake_body(
        task_id=task_id,
        role="auditor_syntax",
        focus="check targeted TruthGate context",
        is_pool=False,
    )

    assert "## TruthGate context" in body
    assert "`truthgate_pass`" in body
    assert "preserve the approved lifecycle" in body
    assert "truth/audit.md" in body
    assert "The implementation must preserve" in body
    assert "Provisional: emergency override is active" in body
    assert "amendment:12" in body


@pytest.mark.asyncio
async def test_empty_basis_wake_displays_skip_warning(fresh_db: str) -> None:
    await init_db()
    task_id = await _seed_audit_task(
        basis=[],
        concerns=[],
        warning="sparse truth corpus; targeted check skipped",
        method="classifier_sparse",
    )

    body = await build_auditor_wake_body(
        task_id=task_id,
        role="auditor_syntax",
        focus="check sparse warning",
        is_pool=False,
    )

    assert "Targeted truth check: skipped" in body
    assert "sparse truth corpus" in body


@pytest.mark.asyncio
async def test_audit_pass_rejected_when_body_reports_truth_violation(
    fresh_db: str,
) -> None:
    await init_db()
    task_id = await _seed_audit_task()
    p4 = _server_for("p4")

    err = _err(await _handler(p4, "submit_audit_report")({
        "task_id": task_id,
        "kind": "syntax",
        "verdict": "pass",
        "body": "Checked the patch. TruthGate violation: it contradicts truth/audit.md.",
    }))
    assert "targeted TruthGate check blocks PASS" in err
    assert "truth violation" in err.lower()

    ok = _ok(await _handler(p4, "submit_audit_report")({
        "task_id": task_id,
        "kind": "syntax",
        "verdict": "fail",
        "body": "TruthGate violation: it contradicts truth/audit.md.",
    }))
    assert "Submitted syntax audit" in ok
    assert "fail" in ok


@pytest.mark.asyncio
async def test_audit_pass_rejected_when_cited_basis_missing(
    fresh_db: str,
) -> None:
    await init_db()
    task_id = await _seed_audit_task(basis=["truth/missing.md"])
    p4 = _server_for("p4")

    err = _err(await _handler(p4, "submit_audit_report")({
        "task_id": task_id,
        "kind": "syntax",
        "verdict": "pass",
        "body": "Looks good.",
    }))
    assert "targeted TruthGate check blocks PASS" in err
    assert "truth_basis file does not exist" in err


@pytest.mark.asyncio
async def test_malformed_truth_basis_flags_review_and_rejects_pass(
    fresh_db: str,
) -> None:
    await init_db()
    task_id = await _seed_audit_task(basis_raw="{not valid json")
    p4 = _server_for("p4")

    body = await build_auditor_wake_body(
        task_id=task_id,
        role="auditor_syntax",
        focus="check malformed basis",
        is_pool=False,
    )
    assert "truth_basis is malformed/unparseable" in body
    assert "requires Coach review" in body
    assert "Targeted truth check: skipped" not in body

    err = _err(await _handler(p4, "submit_audit_report")({
        "task_id": task_id,
        "kind": "syntax",
        "verdict": "pass",
        "body": "Looks good.",
    }))
    assert "targeted TruthGate check blocks PASS" in err
    assert "truth_basis is malformed/unparseable" in err


@pytest.mark.asyncio
async def test_stale_truthgate_basis_blocks_audit_pass_with_coach_refresh_path(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_db()
    task_id = await _seed_audit_task(
        task_id="t-2026-05-17-5a1e0001",
        status="audit_semantics",
        role="auditor_semantics",
        auditor="p8",
    )
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE tasks SET truthgate_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", task_id),
        )
        await c.commit()
    finally:
        await c.close()

    import server.agents as agents_mod

    wake_calls: list[tuple[str, str]] = []

    async def fake_wake(agent_id: str, reason: str = "", **_: Any) -> bool:
        wake_calls.append((agent_id, reason))
        return True

    monkeypatch.setattr(agents_mod, "maybe_wake_agent", fake_wake)

    p8 = _server_for("p8")
    err = _err(await _handler(p8, "submit_audit_report")({
        "task_id": task_id,
        "kind": "semantics",
        "verdict": "pass",
        "body": "PASS: implementation matches the approved reply affordance contract.",
    }))
    assert "targeted TruthGate check blocks PASS" in err
    assert "truth_basis file changed after the recorded TruthGate run" in err
    assert "Do not submit FAIL solely for administrative staleness" in err
    assert "coord_refresh_truthgate_basis" in err

    c = await configured_conn()
    try:
        message = dict(await (await c.execute(
            "SELECT from_id, to_id, subject, body, priority FROM messages "
            "WHERE to_id = 'coach' ORDER BY id DESC LIMIT 1",
        )).fetchone())
        role_row = dict(await (await c.execute(
            "SELECT completed_at, verdict FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'auditor_semantics'",
            (task_id,),
        )).fetchone())
    finally:
        await c.close()
    assert message["from_id"] == "truthgate"
    assert message["priority"] == "interrupt"
    assert f"TruthGate basis stale during audit: {task_id}" in message["subject"]
    assert "coord_refresh_truthgate_basis" in message["body"]
    assert wake_calls and wake_calls[-1][0] == "coach"
    assert not any(call[0] == "p8" for call in wake_calls)
    assert role_row["completed_at"] is None
    assert role_row["verdict"] is None

    coach = _server_for("coach")
    rerun_err = _err(await _handler(coach, "run_truthgate")({
        "task_id": task_id,
        "force": "true",
    }))
    assert "TruthGate assessment only runs while status='truthgate'" in rerun_err

    refresh = _ok(await _handler(coach, "refresh_truthgate_basis")({
        "task_id": task_id,
        "rationale": "truth/reply-affordance.md amendment only clarified the audit refresh workflow.",
    }))
    assert "Refreshed TruthGate basis checkpoint" in refresh
    assert "No audit PASS was recorded" in refresh

    c = await configured_conn()
    try:
        task = dict(await (await c.execute(
            "SELECT status, latest_audit_verdict, truthgate_warning "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )).fetchone())
    finally:
        await c.close()
    assert task["status"] == "audit_semantics"
    assert task["latest_audit_verdict"] is None
    assert "TruthGate basis refreshed by Coach during audit" in task["truthgate_warning"]

    ok = _ok(await _handler(p8, "submit_audit_report")({
        "task_id": task_id,
        "kind": "semantics",
        "verdict": "pass",
        "body": "PASS: implementation matches the approved reply affordance contract.",
    }))
    assert "Submitted semantics audit" in ok
    assert "pass" in ok


@pytest.mark.asyncio
async def test_refresh_truthgate_basis_is_coach_only_and_audit_stage_only(
    fresh_db: str,
) -> None:
    await init_db()
    task_id = await _seed_audit_task(task_id="t-2026-05-17-5a1e0002")
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE tasks SET status = 'execute', truthgate_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", task_id),
        )
        await c.commit()
    finally:
        await c.close()

    p4 = _server_for("p4")
    assert "Coach-only" in _err(await _handler(p4, "refresh_truthgate_basis")({
        "task_id": task_id,
        "rationale": "reviewed",
    }))

    coach = _server_for("coach")
    err = _err(await _handler(coach, "refresh_truthgate_basis")({
        "task_id": task_id,
        "rationale": "reviewed",
    }))
    assert "only applies while" in err
    assert "stage=execute" in err

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
    basis: list[str] | None = None,
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
            "VALUES (?, 'misc', 'audit target', 'audit_syntax', 'p2', "
            "'coach', 'truthgate_pass', ?, ?, ?, ?, ?, ?, ?, "
            "'[{\"stage\":\"execute\",\"to\":[\"p2\"]},"
            "{\"stage\":\"audit_syntax\",\"to\":[\"p4\"]}]')",
            (
                task_id,
                json.dumps(truth_basis),
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
            "VALUES (?, 'auditor_syntax', '[]', 'p4', ?, ?)",
            (task_id, now, now),
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

"""Role-completion gate tests (Docs/kanban-specs.md §2.3 — fix for
audit item 2).

Coverage:
  - coord_update_task non-force paths reject manual moves that should
    be event-driven (commit / audit / ship)
  - cancellation (status='cancelled') still works
  - coord_advance_task_stage (Coach-only override) bypasses the gate
  - the gate honors active vs superseded role assignments
  - the gate respects spec_path on plan → execute (standard)
"""

from __future__ import annotations

from typing import Any

from server.db import configured_conn, init_db
from server.tools import build_coord_server


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    h = server["_handlers"].get(f"coord_{name}") or server["_handlers"].get(name)
    if h is None:
        raise KeyError(f"no handler for coord_{name}")
    return h


def _ok_text(result: dict[str, Any]) -> str:
    assert not result.get("isError"), f"unexpected error: {result}"
    return result["content"][0]["text"]


def _err_text(result: dict[str, Any]) -> str:
    assert result.get("isError"), f"expected error, got {result}"
    return result["content"][0]["text"]


async def _seed_task(
    *,
    task_id: str = "t-2026-05-03-abc12345",
    status: str = "plan",
    complexity: str = "standard",
    owner: str | None = None,
    spec_path: str | None = None,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, complexity, spec_path) "
            "VALUES (?, 'misc', 'demo', ?, ?, 'coach', ?, ?)",
            (task_id, status, owner, complexity, spec_path),
        )
        await c.commit()
    finally:
        await c.close()


async def _insert_role_row(
    *,
    task_id: str = "t-2026-05-03-abc12345",
    role: str,
    owner: str | None = None,
    verdict: str | None = None,
    completed_at: str | None = None,
    superseded_by: int | None = None,
) -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, "
            "verdict, completed_at, superseded_by) "
            "VALUES (?, ?, '[]', ?, '2026-05-03T00:00:00', ?, ?, ?)",
            (task_id, role, owner, verdict, completed_at, superseded_by),
        )
        await c.commit()
        return cur.lastrowid
    finally:
        await c.close()


# ---------- coord_update_task: gate rejects manual moves ----------


async def test_update_task_execute_to_audit_syntax_rejected_without_commit(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(
        status="execute", owner="p3",
        spec_path="projects/misc/working/tasks/t-2026-05-03-abc12345/spec.md",
    )
    server = _server_for("p3")
    result = await _handler(server, "update_task")({
        "task_id": "t-2026-05-03-abc12345",
        "status": "audit_syntax",
    })
    msg = _err_text(result)
    assert "coord_commit_push" in msg
    assert "coord_advance_task_stage" in msg


async def test_update_task_audit_syntax_to_audit_semantics_requires_pass_verdict(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="audit_syntax", owner="p3", spec_path="x")
    # No active syntax-auditor verdict yet. Owner (p3) attempts the move
    # — the role-completion gate rejects.
    server = _server_for("p3")
    result = await _handler(server, "update_task")({
        "task_id": "t-2026-05-03-abc12345",
        "status": "audit_semantics",
    })
    msg = _err_text(result)
    assert "syntax auditor" in msg
    assert "verdict='pass'" in msg


async def test_update_task_audit_syntax_passes_when_auditor_has_verdict_pass(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="audit_syntax", owner="p3", spec_path="x")
    await _insert_role_row(
        role="auditor_syntax", owner="p4",
        verdict="pass", completed_at="2026-05-03T01:00:00",
    )
    server = _server_for("p3")
    result = await _handler(server, "update_task")({
        "task_id": "t-2026-05-03-abc12345",
        "status": "audit_semantics",
    })
    _ok_text(result)


async def test_update_task_ship_to_archive_requires_completed_shipper(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="ship", owner="p3", spec_path="x")
    # Shipper assigned but hasn't called coord_mark_shipped (no completed_at).
    await _insert_role_row(role="shipper", owner="p5")
    # Owner (p3) attempts the move — gate rejects (delivery, not cancel).
    server = _server_for("p3")
    result = await _handler(server, "update_task")({
        "task_id": "t-2026-05-03-abc12345",
        "status": "archive",
    })
    msg = _err_text(result)
    assert "coord_mark_shipped" in msg


async def test_update_task_ship_to_archive_passes_when_shipper_completed(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="ship", owner="p3", spec_path="x")
    await _insert_role_row(
        role="shipper", owner="p5", completed_at="2026-05-03T02:00:00",
    )
    server = _server_for("p3")
    result = await _handler(server, "update_task")({
        "task_id": "t-2026-05-03-abc12345",
        "status": "archive",
    })
    _ok_text(result)


# ---------- cancellation skips the gate ----------


async def test_update_task_cancellation_bypasses_gate(fresh_db: str) -> None:
    await init_db()
    # Even at ship stage with no completed shipper, cancellation works.
    # Coach can cancel any task (existing permission rule).
    await _seed_task(status="ship", owner="p3", spec_path="x")
    await _insert_role_row(role="shipper", owner="p5")  # incomplete
    server = _server_for("coach")
    result = await _handler(server, "update_task")({
        "task_id": "t-2026-05-03-abc12345",
        "status": "cancelled",
    })
    _ok_text(result)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, cancelled_at FROM tasks "
            "WHERE id = 't-2026-05-03-abc12345'"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["status"] == "archive"
    assert row["cancelled_at"] is not None


# ---------- spec gate on plan → execute ----------


async def test_update_task_plan_to_execute_rejected_without_spec(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="plan", complexity="standard", spec_path=None)
    await _insert_role_row(role="executor", owner="p3")
    server = _server_for("coach")
    result = await _handler(server, "update_task")({
        "task_id": "t-2026-05-03-abc12345",
        "status": "execute",
    })
    msg = _err_text(result)
    assert "no spec" in msg
    assert "coord_write_task_spec" in msg


async def test_update_task_plan_to_execute_rejected_without_executor(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(
        status="plan", complexity="standard",
        spec_path="projects/misc/working/tasks/t-2026-05-03-abc12345/spec.md",
    )
    server = _server_for("coach")
    result = await _handler(server, "update_task")({
        "task_id": "t-2026-05-03-abc12345",
        "status": "execute",
    })
    msg = _err_text(result)
    assert "no claimed executor" in msg


# ---------- Coach override (advance_task_stage) bypasses the gate ----------


async def test_advance_task_stage_bypasses_role_gate(fresh_db: str) -> None:
    await init_db()
    # Force ship → archive without a completed shipper.
    await _seed_task(status="ship", owner="p3", spec_path="x")
    await _insert_role_row(role="shipper", owner="p5")  # incomplete
    server = _server_for("coach")
    result = await _handler(server, "advance_task_stage")({
        "task_id": "t-2026-05-03-abc12345",
        "stage": "archive",
        "note": "shipper went silent",
    })
    _ok_text(result)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status FROM tasks WHERE id = 't-2026-05-03-abc12345'"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["status"] == "archive"


# ---------- Gate sees only the active (non-superseded) auditor row ----------


async def test_gate_uses_only_active_auditor_row(fresh_db: str) -> None:
    """A prior failed auditor row that's been superseded must not
    satisfy the pass-verdict gate. The gate checks the active row
    (most recent assigned_at, superseded_by IS NULL) only."""
    await init_db()
    await _seed_task(status="audit_syntax", owner="p3", spec_path="x")
    # Round 1: failed and superseded.
    old_id = await _insert_role_row(
        role="auditor_syntax", owner="p4",
        verdict="fail", completed_at="2026-05-03T01:00:00",
    )
    # Round 2: assigned but no verdict yet.
    new_id = await _insert_role_row(role="auditor_syntax", owner="p4")
    # Mark round 1 as superseded by round 2.
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE task_role_assignments SET superseded_by = ? WHERE id = ?",
            (new_id, old_id),
        )
        await c.commit()
    finally:
        await c.close()

    server = _server_for("p3")
    result = await _handler(server, "update_task")({
        "task_id": "t-2026-05-03-abc12345",
        "status": "audit_semantics",
    })
    msg = _err_text(result)
    assert "verdict='pass'" in msg

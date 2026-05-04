"""Tests for the audit-focus discipline (kanban-specs §4.6).

Covers:
- _validate_trajectory accepts optional `focus` per audit entry.
- audit_semantics with non-empty `to` and no `focus` is rejected at
  validation time.
- Empty-pool semantic stage is allowed without focus (deferred to
  coord_assign_auditor time).
- Non-audit stages silently drop focus.
- coord_assign_auditor accepts focus, requires it for kind='semantics',
  inherits from prior superseded row when omitted on re-assign.
- task_role_assignments.focus column is populated on insert.
- The auditor wake prompt body contains:
  - syntax: ## Focus + ## Contract (cascade with title+description
    when no spec.md).
  - semantic: ## Focus + ## Project context (truth/wiki/Compass).
  - semantic spec section labelled "supplementary background".
- coord_create_task plants `focus` from trajectory entries.
- coord_set_task_trajectory preserves prior focus when entry omits it,
  overwrites when entry provides one.
"""

from __future__ import annotations

import json
from typing import Any

from server.db import configured_conn, init_db
from server.tools import _validate_trajectory, build_coord_server


# ------------------------------------------------------------ helpers


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    h = server["_handlers"].get(f"coord_{name}") or server["_handlers"].get(name)
    if h is None:
        raise KeyError(f"no handler for coord_{name}")
    return h


def _ok_text(result: dict[str, Any]) -> str:
    assert not result.get("isError"), f"tool returned error: {result}"
    return result["content"][0]["text"]


def _err_text(result: dict[str, Any]) -> str:
    assert result.get("isError"), f"expected error, got {result}"
    return result["content"][0]["text"]


async def _seed_task(
    *,
    task_id: str = "t-2026-05-04-aud00001",
    title: str = "demo audit task",
    description: str = "demo description body",
    status: str = "audit_syntax",
    trajectory: str | None = None,
    owner: str | None = "p3",
    spec_path: str | None = None,
) -> None:
    if trajectory is None:
        trajectory = (
            '[{"stage":"execute","to":[]},'
            '{"stage":"audit_syntax","to":[]},'
            '{"stage":"audit_semantics","to":[]},'
            '{"stage":"ship","to":[]}]'
        )
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, description, status, "
            "owner, created_by, trajectory, spec_path) "
            "VALUES (?, 'misc', ?, ?, ?, ?, 'coach', ?, ?)",
            (task_id, title, description, status, owner, trajectory,
             spec_path),
        )
        await c.commit()
    finally:
        await c.close()


async def _row_focus(task_id: str, role: str) -> str | None:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT focus FROM task_role_assignments "
            "WHERE task_id = ? AND role = ? "
            "AND completed_at IS NULL AND superseded_by IS NULL "
            "ORDER BY assigned_at DESC LIMIT 1",
            (task_id, role),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    return dict(row).get("focus") if row else None


# ------------------------------------------------------------ validator


def test_validate_trajectory_accepts_focus_on_audit_stages() -> None:
    traj, err = _validate_trajectory([
        {"stage": "execute", "to": "p2"},
        {"stage": "audit_syntax", "to": "p4", "focus": "race conditions in lock path"},
        {"stage": "audit_semantics", "to": "p7", "focus": "verify math invariants"},
    ])
    assert err is None, err
    assert traj is not None
    # focus persisted on audit entries.
    assert traj[1]["focus"] == "race conditions in lock path"
    assert traj[2]["focus"] == "verify math invariants"


def test_validate_trajectory_rejects_semantic_with_assignees_and_no_focus() -> None:
    traj, err = _validate_trajectory([
        {"stage": "execute", "to": "p2"},
        {"stage": "audit_semantics", "to": "p7"},
    ])
    assert traj is None
    assert err is not None
    assert "audit_semantics requires a 'focus'" in err


def test_validate_trajectory_allows_empty_pool_semantic_without_focus() -> None:
    """Empty-pool semantic stages are allowed without focus — the
    enforcement deferral matches the create-time-vs-assign-time policy
    (focus is required when an auditor is actually named)."""
    traj, err = _validate_trajectory([
        {"stage": "execute", "to": "p2"},
        {"stage": "audit_semantics", "to": []},
    ])
    assert err is None, err
    assert traj is not None
    # Empty pool: no focus stored.
    assert "focus" not in traj[1]


def test_validate_trajectory_silently_drops_focus_on_non_audit_stage() -> None:
    """A focus on plan/execute/ship is silently dropped (defensive
    against Coach paste-mistakes; doesn't bounce the whole call)."""
    traj, err = _validate_trajectory([
        {"stage": "plan", "to": "p5", "focus": "should be dropped"},
        {"stage": "execute", "to": "p2", "focus": "also dropped"},
    ])
    assert err is None, err
    assert traj is not None
    assert "focus" not in traj[0]
    assert "focus" not in traj[1]


def test_validate_trajectory_rejects_non_string_focus() -> None:
    traj, err = _validate_trajectory([
        {"stage": "execute", "to": "p2"},
        {"stage": "audit_syntax", "to": "p4", "focus": 42},
    ])
    assert traj is None
    assert err is not None
    assert "'focus' must be a string" in err


def test_validate_trajectory_audit_syntax_without_focus_ok() -> None:
    """Syntax audits work without a focus — default kicks in at wake time."""
    traj, err = _validate_trajectory([
        {"stage": "execute", "to": "p2"},
        {"stage": "audit_syntax", "to": "p4"},
    ])
    assert err is None, err
    assert traj is not None


# ------------------------------------------------------------ schema


async def test_focus_column_exists_on_role_assignments(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute("PRAGMA table_info(task_role_assignments)")
        cols = {row[1] for row in await cur.fetchall()}
    finally:
        await c.close()
    assert "focus" in cols


# ------------------------------------------------------------ create_task


async def test_create_task_propagates_focus_to_role_row(fresh_db: str) -> None:
    await init_db()
    coach = _server_for("coach")
    text = _ok_text(await _handler(coach, "create_task")({
        "title": "math derivation update",
        "trajectory": [
            {"stage": "execute", "to": ["p2"]},
            {"stage": "audit_semantics", "to": "p7",
             "focus": "verify rule-3a derivation matches glossary"},
        ],
    }))
    # Pull task id from the response (best-effort token grep).
    import re
    m = re.search(r"t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}", text)
    assert m, f"no task id in response: {text}"
    task_id = m.group(0)
    focus = await _row_focus(task_id, "auditor_semantics")
    assert focus == "verify rule-3a derivation matches glossary"


async def test_create_task_rejects_semantic_without_focus(fresh_db: str) -> None:
    await init_db()
    coach = _server_for("coach")
    err = _err_text(await _handler(coach, "create_task")({
        "title": "math derivation update",
        "trajectory": [
            {"stage": "execute", "to": ["p2"]},
            {"stage": "audit_semantics", "to": "p7"},
        ],
    }))
    assert "audit_semantics requires a 'focus'" in err


# ------------------------------------------------------------ assign_auditor


async def test_assign_auditor_semantic_without_focus_rejected(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="audit_semantics", spec_path="x")
    coach = _server_for("coach")
    err = _err_text(await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-04-aud00001",
        "to": "p7",
        "kind": "semantic",
    }))
    assert "semantic audits require a focus" in err


async def test_assign_auditor_syntax_without_focus_accepted(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="audit_syntax", spec_path="x")
    coach = _server_for("coach")
    _ok_text(await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-04-aud00001",
        "to": "p4",
        "kind": "syntax",
    }))
    # Focus is NULL (default applied at wake time, not stored).
    focus = await _row_focus("t-2026-05-04-aud00001", "auditor_syntax")
    assert focus is None


async def test_assign_auditor_semantic_with_focus_persists(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="audit_semantics", spec_path="x")
    coach = _server_for("coach")
    _ok_text(await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-04-aud00001",
        "to": "p7",
        "kind": "semantic",
        "focus": "math invariants on the new derivation",
    }))
    focus = await _row_focus("t-2026-05-04-aud00001", "auditor_semantics")
    assert focus == "math invariants on the new derivation"


async def test_assign_auditor_inherits_focus_on_reassign(
    fresh_db: str,
) -> None:
    """Coach re-assigning a semantic auditor without re-providing focus
    inherits from the prior superseded row — quick reassignment must
    not lose Coach's earlier framing."""
    await init_db()
    await _seed_task(status="audit_semantics", spec_path="x")
    coach = _server_for("coach")
    _ok_text(await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-04-aud00001",
        "to": "p7",
        "kind": "semantic",
        "focus": "math invariants on the new derivation",
    }))
    # Reassign p8 without a focus — inheritance kicks in.
    _ok_text(await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-04-aud00001",
        "to": "p8",
        "kind": "semantic",
    }))
    focus = await _row_focus("t-2026-05-04-aud00001", "auditor_semantics")
    assert focus == "math invariants on the new derivation"


async def test_assign_auditor_focus_overrides_inherited(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(status="audit_semantics", spec_path="x")
    coach = _server_for("coach")
    _ok_text(await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-04-aud00001",
        "to": "p7",
        "kind": "semantic",
        "focus": "first focus",
    }))
    _ok_text(await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-04-aud00001",
        "to": "p8",
        "kind": "semantic",
        "focus": "second focus (sharper)",
    }))
    focus = await _row_focus("t-2026-05-04-aud00001", "auditor_semantics")
    assert focus == "second focus (sharper)"


async def test_assign_auditor_mirrors_focus_to_trajectory(
    fresh_db: str,
) -> None:
    """The mirror back into tasks.trajectory.to also carries focus
    so the stored trajectory matches the role row."""
    await init_db()
    await _seed_task(
        status="audit_semantics",
        trajectory=(
            '[{"stage":"execute","to":["p2"]},'
            '{"stage":"audit_semantics","to":[],"focus":"old focus"}]'
        ),
        spec_path="x",
    )
    coach = _server_for("coach")
    _ok_text(await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-04-aud00001",
        "to": "p7",
        "kind": "semantic",
        "focus": "new focus",
    }))
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT trajectory FROM tasks WHERE id = ?",
            ("t-2026-05-04-aud00001",),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    traj = json.loads(row["trajectory"])
    sem = next(e for e in traj if e["stage"] == "audit_semantics")
    assert sem["to"] == ["p7"]
    assert sem["focus"] == "new focus"


# ------------------------------------------------------------ wake-prompt body


async def test_auditor_wake_body_syntax_includes_focus_and_contract(
    fresh_db: str,
) -> None:
    """Syntax audit wake should render Coach's focus + the contract
    cascade. With no spec.md, the cascade falls back to title+description."""
    from server.kanban import build_auditor_wake_body

    await init_db()
    await _seed_task(status="audit_syntax", spec_path=None)
    body = await build_auditor_wake_body(
        task_id="t-2026-05-04-aud00001",
        role="auditor_syntax",
        focus="race-condition review on the lock path",
        is_pool=False,
    )
    assert "## Focus" in body
    assert "race-condition review on the lock path" in body
    assert "## Contract" in body
    # Contract includes title+description (rung 2) since spec is missing.
    assert "demo audit task" in body
    assert "demo description body" in body
    # Tool hint at the bottom.
    assert "coord_submit_audit_report" in body
    assert "kind='syntax'" in body


async def test_auditor_wake_body_syntax_default_focus_when_none(
    fresh_db: str,
) -> None:
    """Syntax audit with no focus uses the default focus stub."""
    from server.kanban import build_auditor_wake_body, _DEFAULT_SYNTAX_FOCUS

    await init_db()
    await _seed_task(status="audit_syntax")
    body = await build_auditor_wake_body(
        task_id="t-2026-05-04-aud00001",
        role="auditor_syntax",
        focus=None,
        is_pool=False,
    )
    assert _DEFAULT_SYNTAX_FOCUS in body


async def test_auditor_wake_body_semantic_uses_project_context_not_spec(
    fresh_db: str,
) -> None:
    """Semantic audit wake should NOT have the contract cascade —
    instead it has the project-context block (Compass + truth/ + wiki/)."""
    from server.kanban import build_auditor_wake_body

    await init_db()
    await _seed_task(status="audit_semantics", spec_path="working/tasks/x/spec.md")
    body = await build_auditor_wake_body(
        task_id="t-2026-05-04-aud00001",
        role="auditor_semantics",
        focus="verify math correctness",
        is_pool=False,
    )
    assert "## Focus" in body
    assert "verify math correctness" in body
    assert "## Project context" in body
    # Semantic context names truth/, wiki/, Compass.
    assert "truth" in body.lower()
    assert "wiki" in body.lower()
    assert "compass" in body.lower()
    # Spec is supplementary background only, NOT the binding contract.
    assert "supplementary" in body.lower()
    # Tool hint.
    assert "coord_submit_audit_report" in body
    assert "kind='semantics'" in body


async def test_auditor_wake_body_semantic_no_focus_renders_stop_stub(
    fresh_db: str,
) -> None:
    """A semantic wake without focus is a configuration bug — the wake
    must render an explicit STOP stub so the auditor doesn't guess."""
    from server.kanban import build_auditor_wake_body

    await init_db()
    await _seed_task(status="audit_semantics")
    body = await build_auditor_wake_body(
        task_id="t-2026-05-04-aud00001",
        role="auditor_semantics",
        focus=None,
        is_pool=False,
    )
    assert "no focus set" in body.lower()
    assert "STOP" in body or "stop" in body.lower()


async def test_auditor_wake_body_pool_includes_accept_role(
    fresh_db: str,
) -> None:
    from server.kanban import build_auditor_wake_body

    await init_db()
    await _seed_task(status="audit_syntax")
    body = await build_auditor_wake_body(
        task_id="t-2026-05-04-aud00001",
        role="auditor_syntax",
        focus="check the lock path",
        is_pool=True,
    )
    assert "coord_accept_role" in body


# ------------------------------------------------------------ inherit_audit_focus


async def test_inherit_audit_focus_returns_none_when_no_prior(
    fresh_db: str,
) -> None:
    from server.kanban import inherit_audit_focus

    await init_db()
    await _seed_task(status="audit_semantics")
    assert await inherit_audit_focus(
        "t-2026-05-04-aud00001", "auditor_semantics"
    ) is None


async def test_inherit_audit_focus_returns_prior_focus(
    fresh_db: str,
) -> None:
    from server.kanban import inherit_audit_focus

    await init_db()
    await _seed_task(status="audit_semantics", spec_path="x")
    coach = _server_for("coach")
    _ok_text(await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-04-aud00001",
        "to": "p7",
        "kind": "semantic",
        "focus": "the prior focus",
    }))
    inherited = await inherit_audit_focus(
        "t-2026-05-04-aud00001", "auditor_semantics"
    )
    assert inherited == "the prior focus"


# ------------------------------------------------------------ set_task_trajectory


async def test_set_trajectory_preserves_focus_when_entry_omits(
    fresh_db: str,
) -> None:
    """Re-routing without re-providing focus must preserve the existing
    row's focus on audit_syntax (focus is optional there)."""
    await init_db()
    await _seed_task(
        status="audit_syntax",
        trajectory=(
            '[{"stage":"execute","to":["p2"]},'
            '{"stage":"audit_syntax","to":["p4"],"focus":"keep me"}]'
        ),
        spec_path="x",
    )
    # Plant a row matching the current trajectory.
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, focus, "
            "assigned_at, claimed_at) "
            "VALUES ('t-2026-05-04-aud00001', 'auditor_syntax', '[]', "
            "'p4', 'keep me', '2026-05-04T10:00:00Z', '2026-05-04T10:00:00Z')",
        )
        await c.commit()
    finally:
        await c.close()

    coach = _server_for("coach")
    # Reroute, omit focus on audit_syntax.
    _ok_text(await _handler(coach, "set_task_trajectory")({
        "task_id": "t-2026-05-04-aud00001",
        "trajectory": [
            {"stage": "execute", "to": ["p2"]},
            {"stage": "audit_syntax", "to": ["p4"]},
        ],
    }))
    focus = await _row_focus("t-2026-05-04-aud00001", "auditor_syntax")
    assert focus == "keep me"


async def test_set_trajectory_overwrites_focus_when_entry_provides(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(
        status="audit_syntax",
        trajectory=(
            '[{"stage":"execute","to":["p2"]},'
            '{"stage":"audit_syntax","to":["p4"],"focus":"old"}]'
        ),
        spec_path="x",
    )
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, focus, "
            "assigned_at, claimed_at) "
            "VALUES ('t-2026-05-04-aud00001', 'auditor_syntax', '[]', "
            "'p4', 'old', '2026-05-04T10:00:00Z', '2026-05-04T10:00:00Z')",
        )
        await c.commit()
    finally:
        await c.close()
    coach = _server_for("coach")
    _ok_text(await _handler(coach, "set_task_trajectory")({
        "task_id": "t-2026-05-04-aud00001",
        "trajectory": [
            {"stage": "execute", "to": ["p2"]},
            {"stage": "audit_syntax", "to": ["p4"], "focus": "new"},
        ],
    }))
    focus = await _row_focus("t-2026-05-04-aud00001", "auditor_syntax")
    assert focus == "new"

"""v0.3.10 — `coord_my_assignments` Next-action footer.

Production trace 2026-05-06: a Player assigned a planning task woke,
called coord_my_assignments, saw "## Pending planner assignments:
- t-... Normalize MWC realized-red display..." and STOPPED — treated
the response as a status report. The four buckets were descriptive
with no imperative call to action.

Fix: the response now ends with a "## Next action:" section that
names the completion tool with the task_id baked in. Mirrors the
kanban subscriber's stage-entry wake hint pattern.

Priority order: executor > pending reviews > pending ships >
pending plans > eligible pools. The tool picks the highest-
priority actionable item and surfaces just that one.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.db import configured_conn, init_db
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
    assert not result.get("is_error"), f"unexpected error: {result}"
    return result["content"][0]["text"]


async def _seed_task(
    *,
    task_id: str,
    status: str,
    owner: str | None = None,
    spec_path: str | None = None,
    trajectory: str = _FULL_TRAJECTORY,
    title: str = "next-action demo",
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
    *,
    task_id: str,
    role: str,
    owner: str | None = None,
    eligible: list[str] | None = None,
) -> None:
    import json as _json
    elig = _json.dumps(eligible or [])
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, "
            "claimed_at) "
            "VALUES (?, ?, ?, ?, '2026-05-06T10:00:00Z', ?)",
            (
                task_id, role, elig, owner,
                "2026-05-06T10:00:00Z" if owner else None,
            ),
        )
        await c.commit()
    finally:
        await c.close()


# ---------------------------------------------------------------- planner case (production trace)

async def test_planner_assignment_surfaces_imperative_next_action(
    fresh_db: str,
) -> None:
    """The exact production trace: Player has one pending planner
    assignment with no other active work. Response must end with
    a '## Next action:' line naming `coord_write_task_spec` with
    the task_id baked in, NOT just descriptively listing the
    pending plan."""
    await init_db()
    await _seed_task(task_id="t-2026-05-06-73dad121", status="plan")
    await _seed_role(
        task_id="t-2026-05-06-73dad121", role="planner", owner="p3",
    )
    server = _server_for("p3")
    text = _ok_text(await _handler(server, "my_assignments")({}))
    # The Next action footer is present.
    assert "## Next action:" in text
    # It names the completion tool with the task_id baked in.
    assert "coord_write_task_spec" in text
    assert "t-2026-05-06-73dad121" in text.split("## Next action:")[1]
    # It tells the Player to ACT, not describe.
    assert "DO NOT just describe the task back to Coach" in text


async def test_executor_task_takes_priority_over_pending_planner(
    fresh_db: str,
) -> None:
    """If the Player has BOTH an active executor task AND a pending
    planner assignment (different tasks), the Next action surfaces
    the executor work — that's the unblocked work."""
    await init_db()
    # Active executor.
    await _seed_task(
        task_id="t-2026-05-06-00000020", status="execute", owner="p3",
        spec_path="x", title="exec task",
    )
    await _seed_role(
        task_id="t-2026-05-06-00000020", role="executor", owner="p3",
    )
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET current_task_id = 't-2026-05-06-00000020' "
            "WHERE id = 'p3'"
        )
        await c.commit()
    finally:
        await c.close()
    # Pending planner.
    await _seed_task(
        task_id="t-2026-05-06-00000021", status="plan", title="plan task",
    )
    await _seed_role(
        task_id="t-2026-05-06-00000021", role="planner", owner="p3",
    )

    server = _server_for("p3")
    text = _ok_text(await _handler(server, "my_assignments")({}))
    next_action = text.split("## Next action:")[1]
    # Executor work surfaces, not the planner task.
    assert "coord_commit_push" in next_action
    assert "t-2026-05-06-00000020" in next_action
    assert "t-2026-05-06-00000021" not in next_action


async def test_stale_archived_current_task_does_not_hide_executor_role(
    fresh_db: str,
) -> None:
    """Stale archived current_task_id must not mask live executor work."""
    await init_db()
    await _seed_task(
        task_id="t-2026-05-06-00000030",
        status="archive",
        owner="p3",
        title="old archived task",
    )
    await _seed_task(
        task_id="t-2026-05-06-00000031",
        status="execute",
        owner="p3",
        spec_path="tasks/t/spec.md",
        title="live executor task",
    )
    await _seed_role(
        task_id="t-2026-05-06-00000031", role="executor", owner="p3",
    )
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET current_task_id = ?, allowed_tools = ? "
            "WHERE id = 'p3'",
            (
                "t-2026-05-06-00000030",
                json.dumps(["mcp__coord__coord_my_assignments"]),
            ),
        )
        await c.commit()
    finally:
        await c.close()

    server = _server_for("p3")
    text = _ok_text(await _handler(server, "my_assignments")({}))
    next_action = text.split("## Next action:")[1]

    assert "t-2026-05-06-00000031" in next_action
    assert "coord_commit_push" in next_action
    assert "t-2026-05-06-00000030" not in text

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT current_task_id, allowed_tools FROM agents WHERE id = 'p3'"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()

    assert row["current_task_id"] == "t-2026-05-06-00000031"
    assert "mcp__coord__coord_commit_push" in set(json.loads(row["allowed_tools"]))


async def test_pending_review_surfaces_audit_report(
    fresh_db: str,
) -> None:
    """Reviewer assignment → coord_submit_audit_report with the
    right kind."""
    await init_db()
    await _seed_task(
        task_id="t-2026-05-06-00000022", status="audit_semantics",
    )
    await _seed_role(
        task_id="t-2026-05-06-00000022", role="auditor_semantics",
        owner="p3",
    )
    server = _server_for("p3")
    text = _ok_text(await _handler(server, "my_assignments")({}))
    next_action = text.split("## Next action:")[1]
    assert "coord_submit_audit_report" in next_action
    assert "kind='semantics'" in next_action
    assert "t-2026-05-06-00000022" in next_action


async def test_pending_ship_surfaces_role_complete(
    fresh_db: str,
) -> None:
    """v2: ship-stage Next action surfaces coord_role_complete (the
    v2 collapsed completion tool)."""
    await init_db()
    await _seed_task(task_id="t-2026-05-06-00000023", status="ship")
    await _seed_role(
        task_id="t-2026-05-06-00000023", role="shipper", owner="p3",
    )
    server = _server_for("p3")
    text = _ok_text(await _handler(server, "my_assignments")({}))
    next_action = text.split("## Next action:")[1]
    assert "coord_role_complete" in next_action
    assert "coord_mark_shipped" not in next_action
    assert "t-2026-05-06-00000023" in next_action


async def test_eligible_pool_surfaces_wait_for_coach(
    fresh_db: str,
) -> None:
    """v2: pools are FYI only. The Next action tells the Player to
    wait for Coach's coord_approve_stage wake — there is no claim
    path. coord_accept_role is removed."""
    await init_db()
    await _seed_task(task_id="t-2026-05-06-00000024", status="plan")
    await _seed_role(
        task_id="t-2026-05-06-00000024", role="planner",
        eligible=["p3", "p7"],
    )
    server = _server_for("p3")
    text = _ok_text(await _handler(server, "my_assignments")({}))
    next_action = text.split("## Next action:")[1]
    assert "coord_accept_role" not in next_action
    # v2 messaging: pools FYI only, wait for Coach.
    assert "FYI" in next_action or "Wait" in next_action or "coord_approve_stage" in next_action


async def test_empty_plate_surfaces_idle_message(
    fresh_db: str,
) -> None:
    """When the plate is empty, the footer acknowledges that and
    points at the inbox / poller, not pretending there's actionable
    work."""
    await init_db()
    server = _server_for("p3")
    text = _ok_text(await _handler(server, "my_assignments")({}))
    assert "## Next action:" in text
    assert "Your plate is empty" in text


async def test_executor_with_missing_spec_warns_to_wait(
    fresh_db: str,
) -> None:
    """If the Player is executor on a `plan`-stage task with no
    spec yet, the gate is closed — the Next action explicitly
    says wait + names the planner role as the missing piece."""
    await init_db()
    await _seed_task(
        task_id="t-2026-05-06-00000025",
        status="plan",  # task hasn't reached execute yet
        owner="p3",
        spec_path=None,
        trajectory=_FULL_TRAJECTORY,
    )
    # Executor role row exists but task is still in plan with no spec.
    await _seed_role(
        task_id="t-2026-05-06-00000025", role="executor", owner="p3",
    )
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET current_task_id = 't-2026-05-06-00000025' "
            "WHERE id = 'p3'"
        )
        await c.commit()
    finally:
        await c.close()
    server = _server_for("p3")
    text = _ok_text(await _handler(server, "my_assignments")({}))
    next_action = text.split("## Next action:")[1]
    assert "Wait" in next_action
    assert "no spec.md" in next_action
    assert "coord_write_task_spec" in next_action

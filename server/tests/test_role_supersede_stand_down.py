"""Regression tests for the v0.3.6 role-supersede stand-down wake.

Production trace (2026-05-04, p1 incident): Coach reassigned a planner
role from p1 to p5, but p1 never received a stop-work signal. p1
continued, hit a missing coord_* tool, fell through to raw
`git commit && push` via Bash, and corrupted the branch the kanban
thought belonged to p5.

The fix: any code path that supersedes a `task_role_assignments` row
must wake the displaced slot(s) with an explicit STOP message before
the new owner is woken. Same-slot refresh is filtered (no spurious
ping when Coach re-pokes the existing assignee). Covers:

  * coord_assign_task hard-assign (executor)
  * coord_assign_task pool form (executor)
  * coord_assign_planner / auditor / shipper (via _assign_role_helper)
  * coord_set_task_trajectory removed-stage path
  * coord_set_task_trajectory in-place eligible_owners change
  * Same-slot refresh is silent

Plus the prompt-content regressions for the verify-first gate and the
no-raw-shell escape.
"""

from __future__ import annotations

from typing import Any

import pytest

import server.agents as agents_mod
from server.db import configured_conn, init_db
from server.tools import build_coord_server


# ---------------------------------------------------------------- helpers

class WakeRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self,
        slot: str,
        prompt: str,
        *,
        bypass_debounce: bool = False,
        **kwargs: Any,
    ) -> bool:
        self.calls.append((slot, prompt))
        return True


@pytest.fixture
async def wake_stub(monkeypatch: pytest.MonkeyPatch) -> WakeRecorder:
    rec = WakeRecorder()
    monkeypatch.setattr(agents_mod, "maybe_wake_agent", rec)
    return rec


_STANDARD_TRAJECTORY = (
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
    assert not result.get("isError"), f"tool returned error: {result}"
    return result["content"][0]["text"]


async def _seed_task(
    *,
    task_id: str = "t-2026-05-06-stnd0001",
    title: str = "supersede demo",
    status: str = "plan",
    trajectory: str = _STANDARD_TRAJECTORY,
    owner: str | None = None,
    spec_path: str | None = None,
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


def _stand_down_calls(rec: WakeRecorder) -> list[tuple[str, str]]:
    """Filter for the stand-down wakes (their body starts with the
    distinctive 'Coach reassigned the' phrase from send_role_stand_down).
    """
    return [(slot, body) for slot, body in rec.calls
            if body.startswith("Coach reassigned the ")]


# ---------------------------------------------------------------- prompt content

async def test_wake_prompt_includes_verify_first_gate(fresh_db: str) -> None:
    """The hard-assigned wake must instruct the player to verify
    ownership via coord_my_assignments BEFORE editing anything."""
    from server.kanban import _wake_role_or_emit_needed
    await init_db()
    await _seed_task(status="execute")
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, claimed_at) "
            "VALUES (?, 'executor', '[]', 'p2', '2026-05-06T00:00:00Z', "
            "'2026-05-06T00:00:00Z')",
            ("t-2026-05-06-stnd0001",),
        )
        await c.commit()
    finally:
        await c.close()

    rec = WakeRecorder()
    import server.agents as agents_mod_local
    orig = agents_mod_local.maybe_wake_agent
    agents_mod_local.maybe_wake_agent = rec
    try:
        await _wake_role_or_emit_needed(
            task_id="t-2026-05-06-stnd0001", role="executor"
        )
    finally:
        agents_mod_local.maybe_wake_agent = orig

    assert any(slot == "p2" for slot, _ in rec.calls), rec.calls
    body = next(b for s, b in rec.calls if s == "p2")
    # The verify-first gate is mandatory wording: must mention
    # coord_my_assignments BEFORE editing/committing.
    assert "BEFORE editing" in body
    assert "coord_my_assignments" in body
    assert "STOP" in body or "stop" in body
    assert "reassigned" in body  # the "you may have been reassigned" frame


async def test_tool_not_visible_escape_forbids_raw_shell(fresh_db: str) -> None:
    """The escape paragraph appended to every wake hint must explicitly
    forbid routing around a missing coord_* tool with raw git/Bash/Edit.
    The p1 incident did exactly that — the prompt is the only place we
    can name it."""
    from server.kanban import _TOOL_NOT_VISIBLE_ESCAPE
    text = _TOOL_NOT_VISIBLE_ESCAPE
    assert "DO NOT" in text or "do not" in text.lower()
    # The forbidden workaround is named explicitly, not paraphrased.
    assert "raw" in text.lower() and "git" in text.lower()
    assert "Bash" in text or "bash" in text.lower()
    # Direction: stop and message Coach.
    assert "Coach" in text


async def test_pool_wake_prompt_warns_against_unaccepted_work(
    fresh_db: str,
) -> None:
    """Pool calls must explicitly say 'do NOT do the role work without
    an accepted claim' — the kanban does not credit unclaimed work."""
    from server.kanban import _wake_role_or_emit_needed
    await init_db()
    await _seed_task(status="execute")
    c = await configured_conn()
    try:
        import json as _json
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at) "
            "VALUES (?, 'executor', ?, NULL, '2026-05-06T00:00:00Z')",
            ("t-2026-05-06-stnd0001", _json.dumps(["p2", "p4"])),
        )
        await c.commit()
    finally:
        await c.close()

    rec = WakeRecorder()
    import server.agents as agents_mod_local
    orig = agents_mod_local.maybe_wake_agent
    agents_mod_local.maybe_wake_agent = rec
    try:
        await _wake_role_or_emit_needed(
            task_id="t-2026-05-06-stnd0001", role="executor"
        )
    finally:
        agents_mod_local.maybe_wake_agent = orig

    bodies = [b for _, b in rec.calls]
    assert bodies, "expected at least one pool wake"
    body = bodies[0]
    assert "do NOT do the role work" in body
    assert "coord_accept_role" in body


# ---------------------------------------------------------------- supersede wakes

async def test_assign_planner_reassignment_wakes_displaced_planner(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """The exact p1 case: Coach assigns planner=p1, later reassigns to
    p5. p1 must receive a stand-down wake — without it, p1 has no
    signal to stop work, which is what corrupted the branch in prod."""
    await init_db()
    await _seed_task()
    coach = _server_for("coach")
    _ok_text(await _handler(coach, "assign_planner")({
        "task_id": "t-2026-05-06-stnd0001",
        "to": "p1",
    }))
    wake_stub.calls.clear()
    _ok_text(await _handler(coach, "assign_planner")({
        "task_id": "t-2026-05-06-stnd0001",
        "to": "p5",
    }))
    sd = _stand_down_calls(wake_stub)
    assert any(slot == "p1" for slot, _ in sd), wake_stub.calls
    body = next(body for slot, body in sd if slot == "p1")
    assert "STOP work on t-2026-05-06-stnd0001" in body
    assert "p5" in body  # names the new owner so p1 knows who to defer to


async def test_assign_executor_hard_assign_wakes_displaced(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """Hard-assign path of coord_assign_task. Same-slot supersede
    semantics as planner reassignment but goes through a different
    code path, so it needs its own coverage."""
    await init_db()
    # Seed with a spec so the executor gate doesn't trip.
    await _seed_task(spec_path="/tmp/spec.md")
    coach = _server_for("coach")
    # Pre-existing executor row pointing at p2.
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, claimed_at) "
            "VALUES (?, 'executor', '[]', 'p2', "
            "'2026-05-06T00:00:00Z', '2026-05-06T00:00:00Z')",
            ("t-2026-05-06-stnd0001",),
        )
        await c.commit()
    finally:
        await c.close()
    wake_stub.calls.clear()
    _ok_text(await _handler(coach, "assign_task")({
        "task_id": "t-2026-05-06-stnd0001",
        "to": "p4",
    }))
    sd = _stand_down_calls(wake_stub)
    assert any(slot == "p2" for slot, _ in sd), wake_stub.calls


async def test_same_slot_refresh_does_not_wake_stand_down(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """Re-assigning the same Player to the same role must NOT trigger
    a stand-down wake — the assignee is unchanged. Otherwise Coach
    refreshing eligible_owners or fixing a typo would spam the
    Player with confusing 'you've been reassigned' messages."""
    await init_db()
    await _seed_task()
    coach = _server_for("coach")
    _ok_text(await _handler(coach, "assign_planner")({
        "task_id": "t-2026-05-06-stnd0001",
        "to": "p3",
    }))
    wake_stub.calls.clear()
    # Re-assign to the same Player.
    _ok_text(await _handler(coach, "assign_planner")({
        "task_id": "t-2026-05-06-stnd0001",
        "to": "p3",
    }))
    sd = _stand_down_calls(wake_stub)
    assert sd == [], f"unexpected stand-down on same-slot refresh: {sd}"


async def test_pool_form_wakes_displaced_old_owner(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """Switching from a hard-assigned executor to a pool call must
    wake the displaced slot."""
    await init_db()
    await _seed_task(spec_path="/tmp/spec.md")
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, claimed_at) "
            "VALUES (?, 'executor', '[]', 'p2', "
            "'2026-05-06T00:00:00Z', '2026-05-06T00:00:00Z')",
            ("t-2026-05-06-stnd0001",),
        )
        await c.commit()
    finally:
        await c.close()
    wake_stub.calls.clear()
    coach = _server_for("coach")
    _ok_text(await _handler(coach, "assign_task")({
        "task_id": "t-2026-05-06-stnd0001",
        "to": "p4,p5,p6",
    }))
    sd = _stand_down_calls(wake_stub)
    assert any(slot == "p2" for slot, _ in sd)


async def test_trajectory_reroute_removed_stage_wakes_dropped_owner(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """Coach removes audit_syntax from a trajectory while the task is
    still in plan. The previously-reserved auditor must hear about it."""
    await init_db()
    await _seed_task()
    coach = _server_for("coach")
    # Reserve a syntax auditor (future-stage reservation, not woken now).
    _ok_text(await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-06-stnd0001",
        "to": "p7",
        "kind": "syntax",
    }))
    wake_stub.calls.clear()
    # Reroute: drop audit_syntax entirely.
    _ok_text(await _handler(coach, "set_task_trajectory")({
        "task_id": "t-2026-05-06-stnd0001",
        "trajectory": [
            {"stage": "plan", "to": []},
            {"stage": "execute", "to": []},
            {"stage": "audit_semantics", "to": []},
            {"stage": "ship", "to": []},
        ],
    }))
    sd = _stand_down_calls(wake_stub)
    assert any(slot == "p7" for slot, _ in sd), wake_stub.calls


async def test_trajectory_reroute_swaps_eligible_owners(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """In-place change of `to` on a remaining stage drops the prior
    candidate(s) without going through supersede. Stand-down must
    still fire for them."""
    await init_db()
    await _seed_task()
    coach = _server_for("coach")
    # Reserve a syntax auditor for p7.
    _ok_text(await _handler(coach, "assign_auditor")({
        "task_id": "t-2026-05-06-stnd0001",
        "to": "p7",
        "kind": "syntax",
    }))
    wake_stub.calls.clear()
    # Reroute: keep audit_syntax but swap the assignee to p4.
    _ok_text(await _handler(coach, "set_task_trajectory")({
        "task_id": "t-2026-05-06-stnd0001",
        "trajectory": [
            {"stage": "plan", "to": []},
            {"stage": "execute", "to": []},
            {"stage": "audit_syntax", "to": ["p4"]},
            {"stage": "audit_semantics", "to": []},
            {"stage": "ship", "to": []},
        ],
    }))
    sd = _stand_down_calls(wake_stub)
    assert any(slot == "p7" for slot, _ in sd), wake_stub.calls


async def test_stand_down_emits_event_with_displaced_and_new_owners(
    fresh_db: str, wake_stub: WakeRecorder,
) -> None:
    """Beyond the wake, the supersede emits a `task_role_stand_down`
    event so the timeline records the boundary. Coach also subscribes
    via `to: 'coach'` so it appears in their pane."""
    from server.events import bus
    await init_db()
    await _seed_task()
    coach = _server_for("coach")
    _ok_text(await _handler(coach, "assign_planner")({
        "task_id": "t-2026-05-06-stnd0001",
        "to": "p1",
    }))
    queue = bus.subscribe()
    try:
        _ok_text(await _handler(coach, "assign_planner")({
            "task_id": "t-2026-05-06-stnd0001",
            "to": "p5",
        }))
        captured: list[dict] = []
        import asyncio
        await asyncio.sleep(0.05)
        while True:
            try:
                captured.append(queue.get_nowait())
            except Exception:
                break
    finally:
        bus.unsubscribe(queue)
    stand_down = [e for e in captured
                  if e.get("type") == "task_role_stand_down"]
    assert len(stand_down) >= 1, captured
    ev = stand_down[0]
    assert ev["task_id"] == "t-2026-05-06-stnd0001"
    assert ev["role"] == "planner"
    assert "p1" in ev["displaced"]
    assert "p5" in ev["new_owners"]
    assert ev.get("to") == "coach"

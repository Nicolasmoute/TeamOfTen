"""Tests for pure-DB helpers in server/agents.py.

After the projects refactor (PROJECTS_SPEC.md §3) brief / session_id
moved out of the agents row — the tests verify the new
agent_project_roles + agent_sessions tables instead.
"""

from __future__ import annotations

import pytest

from server.db import configured_conn, init_db, resolve_active_project


@pytest.fixture(autouse=True)
async def _init(fresh_db: str) -> None:
    await init_db()


# ---------- _today_spend ----------


async def _insert_turn(
    agent_id: str, ended_at: str, cost_usd: float
) -> None:
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO turns (agent_id, project_id, started_at, ended_at, cost_usd) "
            "VALUES (?, ?, ?, ?, ?)",
            (agent_id, project_id, ended_at, ended_at, cost_usd),
        )
        await c.commit()
    finally:
        await c.close()


async def test_today_spend_sums_today_only() -> None:
    from server.agents import _today_spend
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    today = now.replace(hour=12, minute=0).isoformat()
    yesterday = (now - timedelta(days=1)).replace(hour=12).isoformat()
    await _insert_turn("p1", today, 0.10)
    await _insert_turn("p1", today, 0.05)
    await _insert_turn("p1", yesterday, 9.99)  # should NOT count
    assert abs(await _today_spend("p1") - 0.15) < 1e-9


async def test_today_spend_team_aggregate_no_filter() -> None:
    from server.agents import _today_spend
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).replace(hour=12).isoformat()
    await _insert_turn("p1", today, 0.10)
    await _insert_turn("p2", today, 0.25)
    await _insert_turn("coach", today, 0.05)
    total = await _today_spend()  # no agent_id → team total
    assert abs(total - 0.40) < 1e-9


async def test_today_spend_empty_returns_zero() -> None:
    from server.agents import _today_spend
    assert await _today_spend("p1") == 0.0
    assert await _today_spend() == 0.0


# ---------- _get_agent_brief / _clear_session_id ----------


async def _set_brief(slot: str, brief: str | None) -> None:
    pid = await resolve_active_project()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO agent_project_roles (slot, project_id, brief) "
            "VALUES (?, ?, ?) ON CONFLICT(slot, project_id) DO UPDATE SET "
            "brief = excluded.brief",
            (slot, pid, brief),
        )
        await c.commit()
    finally:
        await c.close()


async def test_get_brief_returns_column_value() -> None:
    from server.agents import _get_agent_brief
    await _set_brief("p3", "hello\nworld")
    assert await _get_agent_brief("p3") == "hello\nworld"


async def test_get_brief_null_returns_none() -> None:
    from server.agents import _get_agent_brief
    assert await _get_agent_brief("p3") is None


async def test_get_brief_system_agent_returns_none() -> None:
    from server.agents import _get_agent_brief
    assert await _get_agent_brief("system") is None


async def test_clear_session_id_wipes_the_row() -> None:
    from server.agents import _clear_session_id
    pid = await resolve_active_project()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO agent_sessions (slot, project_id, session_id) "
            "VALUES ('p5', ?, 'sess-xyz')",
            (pid,),
        )
        await c.commit()
    finally:
        await c.close()
    await _clear_session_id("p5")
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT session_id FROM agent_sessions "
            "WHERE slot = 'p5' AND project_id = ?",
            (pid,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    # Either no row, or row with NULL session_id — both indicate cleared.
    if row is not None:
        assert dict(row)["session_id"] is None


async def test_clear_session_id_idempotent() -> None:
    from server.agents import _clear_session_id
    # Running against an agent that already has no session row must not raise.
    await _clear_session_id("p7")
    await _clear_session_id("p7")


# ---------- _coach_is_working ----------


async def test_coach_is_working_status_path() -> None:
    """Status='working' alone is enough to skip a tick — covers the
    common case where a turn is mid-execution."""
    import server.agents as agents_mod

    # Default seed leaves coach.status='stopped'.
    assert await agents_mod._coach_is_working() is False

    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET status = 'working' WHERE id = 'coach'"
        )
        await c.commit()
    finally:
        await c.close()
    assert await agents_mod._coach_is_working() is True

    # Idle / stopped / error / waiting → not working.
    for status in ("idle", "stopped", "waiting", "error"):
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE agents SET status = ? WHERE id = 'coach'", (status,)
            )
            await c.commit()
        finally:
            await c.close()
        assert await agents_mod._coach_is_working() is False, status


async def test_coach_is_working_running_tasks_path() -> None:
    """A live `_running_tasks['coach']` entry trips the check even if
    the DB status row hasn't flipped yet — covers the brief race
    between slot-claim under _SPAWN_LOCK and the _set_status flip
    inside run_agent. Without this guard, a /loop or /repeat fire
    that lands in this window would stack a second turn behind the
    first via spawn_rejected."""
    import asyncio

    import server.agents as agents_mod

    # DB says idle (not yet flipped to working).
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET status = 'idle' WHERE id = 'coach'"
        )
        await c.commit()
    finally:
        await c.close()
    assert await agents_mod._coach_is_working() is False

    # Plant a still-pending task in the registry — simulates run_agent
    # having claimed the slot but not yet set status='working'.
    started = asyncio.Event()
    release = asyncio.Event()

    async def _fake_turn() -> None:
        started.set()
        await release.wait()

    fake_task = asyncio.create_task(_fake_turn())
    agents_mod._running_tasks["coach"] = fake_task
    try:
        await started.wait()  # ensure the task is actually live.
        assert await agents_mod._coach_is_working() is True

        # Once the task completes, the check goes back to consulting
        # the DB row only.
        release.set()
        await fake_task
        assert fake_task.done()
        assert await agents_mod._coach_is_working() is False
    finally:
        agents_mod._running_tasks.pop("coach", None)


# ---------- _looks_like_max_turns ----------


def test_looks_like_max_turns_via_subtype() -> None:
    """SDK 'subtype' field is the most specific signal — match any
    string containing 'max_turn' so a near-rename in a future SDK
    release ('error_max_turn', 'max_turns_exceeded') still trips."""
    from server.agents import _looks_like_max_turns

    assert _looks_like_max_turns("error_max_turns", None) is True
    assert _looks_like_max_turns("max_turns", None) is True
    assert _looks_like_max_turns("ERROR_MAX_TURNS", None) is True
    # Other error subtypes do NOT match.
    assert _looks_like_max_turns("error_during_execution", None) is False
    assert _looks_like_max_turns("success", None) is False
    assert _looks_like_max_turns(None, None) is False


def test_looks_like_max_turns_via_stop_reason() -> None:
    """When subtype is unset, fall back to the Anthropic-API
    stop_reason. 'max_turns' and 'max_tokens' are both terminal-by-
    cutoff signals worth auto-continuing on."""
    from server.agents import _looks_like_max_turns

    assert _looks_like_max_turns(None, "max_turns") is True
    assert _looks_like_max_turns(None, "max_tokens") is True
    assert _looks_like_max_turns(None, "MAX_TURNS") is True
    assert _looks_like_max_turns(None, "end_turn") is False
    assert _looks_like_max_turns(None, "stop_sequence") is False
    assert _looks_like_max_turns(None, "tool_use") is False


# ---------- auto-continue scheduler ----------


async def test_maybe_schedule_auto_continue_caps_consecutive() -> None:
    """At the configured cap, _maybe_schedule_auto_continue must NOT
    schedule a continuation — it must publish auto_continue_gave_up
    and a human_attention escalation instead."""
    import server.agents as agents_mod

    captured: list[dict] = []

    class _StubBus:
        async def publish(self, ev: dict) -> None:
            captured.append(ev)

    captured_emits: list[tuple] = []

    async def _stub_emit(slot, type_, **kwargs):
        captured_emits.append((slot, type_, kwargs))

    # Force the cap to 0 so the very first call is over the limit.
    orig_cap = agents_mod.AUTO_CONTINUE_MAX_CONSECUTIVE
    orig_bus = agents_mod.bus
    orig_emit = agents_mod._emit
    agents_mod.AUTO_CONTINUE_MAX_CONSECUTIVE = 0
    agents_mod.bus = _StubBus()
    agents_mod._emit = _stub_emit
    # Defensive isolation: a previous test leaving anything in either
    # set/dict would short-circuit the function before reaching the
    # cap branch.
    agents_mod._consecutive_auto_continues["coach"] = 0
    agents_mod._auto_continue_pending.discard("coach")
    agents_mod._last_turn_error_info.pop("coach", None)

    try:
        await agents_mod._maybe_schedule_auto_continue(
            agent_id="coach",
            subtype="error_max_turns",
            stop_reason="max_turns",
            num_turns=10,
        )
    finally:
        agents_mod.AUTO_CONTINUE_MAX_CONSECUTIVE = orig_cap
        agents_mod.bus = orig_bus
        agents_mod._emit = orig_emit
        agents_mod._consecutive_auto_continues.pop("coach", None)
        agents_mod._auto_continue_pending.discard("coach")

    # No continuation scheduled — counter stayed at 0.
    assert agents_mod._consecutive_auto_continues.get("coach", 0) == 0
    # Emitted the "gave up" signal.
    types = [t for _, t, _ in captured_emits]
    assert "auto_continue_gave_up" in types
    # And a human_attention event landed.
    assert any(
        e.get("type") == "human_attention" for e in captured
    )


async def test_maybe_schedule_auto_continue_skips_when_paused() -> None:
    """Don't schedule when the harness is paused — the user's intent
    is 'stop everything'. Counter must NOT be bumped (otherwise an
    unpause + retry would burn auto-continue budget without ever
    actually firing)."""
    import server.agents as agents_mod

    orig_paused = agents_mod._paused
    agents_mod._paused = True
    agents_mod._consecutive_auto_continues.pop("coach", None)
    agents_mod._auto_continue_pending.discard("coach")
    try:
        await agents_mod._maybe_schedule_auto_continue(
            agent_id="coach",
            subtype="error_max_turns",
            stop_reason="max_turns",
            num_turns=10,
        )
    finally:
        agents_mod._paused = orig_paused
    assert agents_mod._consecutive_auto_continues.get("coach", 0) == 0
    assert "coach" not in agents_mod._auto_continue_pending


async def test_maybe_schedule_auto_continue_delayed_skips_after_clean_turn() -> None:
    """Race fix: if a clean turn arrives during the AUTO_CONTINUE_DELAY
    window, the ResultMessage handler clears _last_turn_error_info.
    The delayed task must observe that clearance and bail — otherwise
    it fires a stale 'your previous turn was cut off' prompt against
    a fresh conversation."""
    import asyncio

    import server.agents as agents_mod

    captured_emits: list[tuple] = []

    async def _stub_emit(slot, type_, **kwargs):
        captured_emits.append((slot, type_, kwargs))

    waked: list[tuple] = []

    async def _stub_wake(slot, prompt, **kwargs):
        waked.append((slot, prompt, kwargs))

    orig_delay = agents_mod.AUTO_CONTINUE_DELAY_SECONDS
    orig_emit = agents_mod._emit
    orig_wake = agents_mod.maybe_wake_agent
    agents_mod.AUTO_CONTINUE_DELAY_SECONDS = 0  # fire effectively immediately
    agents_mod._emit = _stub_emit
    agents_mod.maybe_wake_agent = _stub_wake
    # Setup: a prior errored turn (max_turns) was recorded.
    agents_mod._consecutive_auto_continues.pop("coach", None)
    agents_mod._auto_continue_pending.discard("coach")
    agents_mod._last_turn_error_info["coach"] = {
        "subtype": "error_max_turns",
        "stop_reason": "max_turns",
        "num_turns": 10,
    }
    try:
        await agents_mod._maybe_schedule_auto_continue(
            agent_id="coach",
            subtype="error_max_turns",
            stop_reason="max_turns",
            num_turns=10,
        )
        # Simulate a clean turn landing during the delay: the result
        # handler clears _last_turn_error_info on a non-error result.
        agents_mod._last_turn_error_info.pop("coach", None)
        # Drain the scheduled task — `delay=0` means it's already
        # ready; one yield gives it a chance to run.
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        agents_mod.AUTO_CONTINUE_DELAY_SECONDS = orig_delay
        agents_mod._emit = orig_emit
        agents_mod.maybe_wake_agent = orig_wake
        agents_mod._consecutive_auto_continues.pop("coach", None)
        agents_mod._auto_continue_pending.discard("coach")
        agents_mod._last_turn_error_info.pop("coach", None)

    # The wake must NOT have fired — error info was cleared first.
    assert waked == []
    # The auto_continue_scheduled emit must NOT have fired either.
    types = [t for _, t, _ in captured_emits]
    assert "auto_continue_scheduled" not in types

"""Tests for pure-DB helpers in server/agents.py.

After the projects refactor (PROJECTS_SPEC.md §3) brief / session_id
moved out of the agents row — the tests verify the new
agent_project_roles + agent_sessions tables instead.
"""

from __future__ import annotations

import json

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


# ---------- context usage estimation ----------


def _jsonl_line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def test_session_context_metrics_prefers_latest_assistant_usage(tmp_path) -> None:
    from server.agents import _session_context_metrics_from_jsonl

    p = tmp_path / "sess.jsonl"
    p.write_text(
        _jsonl_line({
            "type": "user",
            "message": {"role": "user", "content": "x" * 20_000},
        })
        + _jsonl_line({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "old"}],
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 90,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 7,
                },
            },
        })
        + _jsonl_line({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "new"}],
                "usage": {
                    "input_tokens": 1,
                    "cache_read_input_tokens": 200,
                    "cache_creation_input_tokens": 9,
                    "output_tokens": 11,
                },
            },
        }),
        encoding="utf-8",
    )

    used, latest_prompt = _session_context_metrics_from_jsonl(p)
    assert latest_prompt == 210
    assert used == 221


def test_session_context_metrics_adds_tail_after_latest_usage(tmp_path) -> None:
    from server.agents import _session_context_metrics_from_jsonl

    p = tmp_path / "sess.jsonl"
    p.write_text(
        _jsonl_line({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": "done",
                "usage": {
                    "input_tokens": 80,
                    "cache_read_input_tokens": 20,
                    "output_tokens": 5,
                },
            },
        })
        + _jsonl_line({
            "type": "user",
            "message": {"role": "user", "content": "z" * 40},
        }),
        encoding="utf-8",
    )

    used, latest_prompt = _session_context_metrics_from_jsonl(p)
    assert latest_prompt == 100
    assert used == 115


async def test_session_context_estimate_finds_claude_project_jsonl(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.agents import _session_context_estimate

    session_id = "sess-abc"
    session_dir = tmp_path / "projects" / "encoded-cwd"
    session_dir.mkdir(parents=True)
    (session_dir / f"{session_id}.jsonl").write_text(
        _jsonl_line({
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": "ok",
                "usage": {
                    "input_tokens": 3,
                    "cache_read_input_tokens": 40,
                    "cache_creation_input_tokens": 7,
                    "output_tokens": 6,
                },
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    assert await _session_context_estimate(session_id) == 56


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


# ---------- maybe_wake_agent: prompt passthrough + queue-on-busy ----


async def _capture_run_agent_prompt(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub run_agent to capture the prompt passed by maybe_wake_agent
    without spawning a real turn. Returns a list that the caller can
    inspect after invoking maybe_wake_agent."""
    import server.agents as agents_mod

    captured: list[str] = []

    async def _stub_run(slot, prompt, **kwargs):
        captured.append(prompt)

    monkeypatch.setattr(agents_mod, "run_agent", _stub_run)

    # Cost cap allow + harness un-paused, no in-flight turn.
    async def _allow_caps(_slot):
        return True, None

    monkeypatch.setattr(agents_mod, "_check_cost_caps", _allow_caps)
    monkeypatch.setattr(agents_mod, "_paused", False)
    agents_mod._running_tasks.pop("coach", None)
    agents_mod._last_turn_ended_at.pop("coach", None)
    agents_mod._pending_wakes.pop("coach", None)
    agents_mod._pending_wakes.pop("p3", None)
    return captured


async def test_maybe_wake_agent_passes_prompt_unmodified_for_coach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even with open Coach todos, the wake prompt passes through
    untouched — the harness no longer appends a "scan your todos"
    nudge. Coach's system prompt + per-tick coordination block
    handle todo discipline; piggybacking on every reactive wake was
    interfering noise."""
    import asyncio

    import server.agents as agents_mod
    import server.coach_todos as todos_mod
    from server.db import resolve_active_project

    captured = await _capture_run_agent_prompt(monkeypatch)

    project_id = await resolve_active_project()
    await todos_mod.add_todo(project_id, title="Wire the launch deck")
    await todos_mod.add_todo(project_id, title="Review p3's audit report")

    await agents_mod.maybe_wake_agent(
        "coach", "New message from the human: hi"
    )
    for _ in range(3):
        await asyncio.sleep(0)

    assert captured == ["New message from the human: hi"]


async def test_maybe_wake_agent_passes_prompt_unmodified_no_todos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import server.agents as agents_mod

    captured = await _capture_run_agent_prompt(monkeypatch)

    await agents_mod.maybe_wake_agent("coach", "Player p3 finished t-7")
    for _ in range(3):
        await asyncio.sleep(0)

    assert captured == ["Player p3 finished t-7"]


async def test_maybe_wake_agent_passes_prompt_unmodified_for_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    import server.agents as agents_mod

    captured = await _capture_run_agent_prompt(monkeypatch)
    agents_mod._last_turn_ended_at.pop("p3", None)
    agents_mod._running_tasks.pop("p3", None)

    await agents_mod.maybe_wake_agent("p3", "you have a new task")
    for _ in range(3):
        await asyncio.sleep(0)

    assert captured == ["you have a new task"]


# ---------- maybe_wake_agent: queue-on-busy --------------------


async def test_maybe_wake_agent_queues_when_target_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wake landing while the target is mid-turn is QUEUED (not
    dropped). The queue entry stays parked in `_pending_wakes` until
    the post-turn deferred-fire picks it up."""
    import asyncio

    import server.agents as agents_mod

    captured = await _capture_run_agent_prompt(monkeypatch)

    # Simulate Coach being mid-turn: insert a placeholder task.
    loop = asyncio.get_running_loop()
    fake_task = loop.create_task(asyncio.sleep(60))
    agents_mod._running_tasks["coach"] = fake_task
    try:
        result = await agents_mod.maybe_wake_agent(
            "coach", "p2 done on t-42", bypass_debounce=True,
        )
        for _ in range(3):
            await asyncio.sleep(0)

        # No spawn happened (run_agent stub uncalled) but the queue
        # entry exists and the call returned True — the wake is
        # accepted, just deferred.
        assert result is True
        assert captured == []
        assert "coach" in agents_mod._pending_wakes
        reason, source, plan = agents_mod._pending_wakes["coach"]
        assert reason == "p2 done on t-42"
    finally:
        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
        agents_mod._running_tasks.pop("coach", None)
        agents_mod._pending_wakes.pop("coach", None)


async def test_maybe_wake_agent_queue_latest_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple wakes landing during a single busy stretch fold to
    the most recent — the inbox + project_events tables retain the
    actual content, so coalescing the prompt doesn't lose anything."""
    import asyncio

    import server.agents as agents_mod

    captured = await _capture_run_agent_prompt(monkeypatch)

    loop = asyncio.get_running_loop()
    fake_task = loop.create_task(asyncio.sleep(60))
    agents_mod._running_tasks["coach"] = fake_task
    try:
        await agents_mod.maybe_wake_agent("coach", "first wake")
        await agents_mod.maybe_wake_agent("coach", "second wake")
        await agents_mod.maybe_wake_agent(
            "coach", "third wake", wake_source="kanban_completion",
        )
        for _ in range(3):
            await asyncio.sleep(0)

        assert captured == []
        reason, source, _plan = agents_mod._pending_wakes["coach"]
        assert reason == "third wake"
        assert source == "kanban_completion"
    finally:
        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
        agents_mod._running_tasks.pop("coach", None)
        agents_mod._pending_wakes.pop("coach", None)


async def test_maybe_wake_agent_no_queue_when_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the slot is free, the wake fires immediately and nothing
    is parked in the queue."""
    import asyncio

    import server.agents as agents_mod

    captured = await _capture_run_agent_prompt(monkeypatch)
    agents_mod._pending_wakes.pop("coach", None)

    await agents_mod.maybe_wake_agent("coach", "fresh wake")
    for _ in range(3):
        await asyncio.sleep(0)

    assert captured == ["fresh wake"]
    assert "coach" not in agents_mod._pending_wakes


async def test_deferred_fire_bypasses_debounce_after_turn_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-turn deferred fire MUST bypass debounce. Otherwise
    `_last_turn_ended_at` (stamped microseconds before the deferred
    fire) would drop the queued wake — losing exactly the wakes the
    queue exists to preserve. This test simulates the run_agent
    finally-block ordering: pop _running_tasks → stamp last-turn-end
    → emit agent_stopped → pop _pending_wakes and re-fire."""
    import asyncio
    import time as time_mod

    import server.agents as agents_mod

    captured = await _capture_run_agent_prompt(monkeypatch)

    # Stage 1: agent is mid-turn; a wake lands and queues.
    loop = asyncio.get_running_loop()
    fake_task = loop.create_task(asyncio.sleep(60))
    agents_mod._running_tasks["coach"] = fake_task
    try:
        await agents_mod.maybe_wake_agent(
            "coach", "queued during busy turn",
        )
        assert "coach" in agents_mod._pending_wakes
    finally:
        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
        agents_mod._running_tasks.pop("coach", None)

    # Stage 2: simulate the finally-block: pop _running_tasks (already
    # done above), stamp the just-ended timestamp, then run the
    # post-turn deferred-fire block. The stamp is the booby trap we
    # are checking for — without bypass on the deferred fire, this
    # would drop the wake.
    agents_mod._last_turn_ended_at["coach"] = time_mod.monotonic()

    queued = agents_mod._pending_wakes.pop("coach", None)
    assert queued is not None
    q_reason, q_source, q_plan = queued
    await agents_mod.maybe_wake_agent(
        "coach",
        q_reason,
        bypass_debounce=True,
        wake_source=q_source,
        plan_mode=q_plan,
    )
    for _ in range(3):
        await asyncio.sleep(0)

    # The deferred fire must have spawned a real turn — captured by
    # the run_agent stub.
    assert captured == ["queued during busy turn"]


async def test_auto_compact_preamble_skips_deferred_fire() -> None:
    """The recursive compact preamble fired inside `maybe_auto_compact`
    must NOT drain `_pending_wakes` on its own finally — the outer
    turn (running the user's actual prompt right after) is about to
    claim the slot and will handle the queue. If the inner preamble
    drained the queue, its deferred fire would race the outer
    slot-claim. Manual /compact (without `auto_compact=True`) still
    drains, since there's no outer turn waiting.

    The check lives in run_agent's post-turn block. We verify the
    specific gate (`if not auto_compact:`) by inspecting the source
    rather than spinning up a real run_agent — auto-compact requires
    a live runtime + cost cap + token estimation, which is much more
    machinery than a focused regression deserves."""
    import inspect

    import server.agents as agents_mod

    src = inspect.getsource(agents_mod.run_agent)
    # The skip MUST be present and gated on auto_compact, otherwise
    # the race documented above fires every time auto-compact runs.
    assert "if not auto_compact:" in src
    assert "_pending_wakes.pop(agent_id, None)" in src
    # Sanity: there is exactly one drain site (the post-turn block);
    # any additional drain would defeat the gate.
    drain_sites = src.count("_pending_wakes.pop(agent_id, None)")
    # Two pops: one in cost-capped early-exit (always discards), one
    # in the post-turn deferred-fire block (gated by auto_compact).
    assert drain_sites == 2, (
        f"unexpected number of _pending_wakes drain sites: {drain_sites}"
    )


async def test_deferred_fire_dropped_without_bypass_documents_bug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the exact failure mode the bypass guards against: if the
    deferred fire passed `bypass_debounce=False` (the value some
    callers might naturally use), the just-stamped end-of-turn time
    would drop the wake. The production code path always passes
    True; this test exists so a future refactor that switches that
    flag visibly fails CI rather than silently regressing."""
    import asyncio
    import time as time_mod

    import server.agents as agents_mod

    captured = await _capture_run_agent_prompt(monkeypatch)
    agents_mod._last_turn_ended_at["coach"] = time_mod.monotonic()

    result = await agents_mod.maybe_wake_agent(
        "coach", "would-be-dropped wake", bypass_debounce=False,
    )
    for _ in range(3):
        await asyncio.sleep(0)

    assert result is False
    assert captured == []


async def test_with_player_reminder_appends_canonical_text() -> None:
    """The canonical turn-end reminder is appended verbatim to a
    non-empty body. Idempotent: a body already carrying the
    reminder is returned unchanged."""
    from server.tools import (
        COACH_TO_PLAYER_TURN_END_REMINDER,
        _with_player_reminder,
    )

    out = _with_player_reminder("Task t-42 has entered execute.")
    assert out.endswith(COACH_TO_PLAYER_TURN_END_REMINDER)
    assert out.startswith("Task t-42 has entered execute.")

    # Idempotent — passing the same body twice doesn't double-append.
    twice = _with_player_reminder(out)
    assert twice == out

    # Empty body → reminder still lands (sans leading newlines).
    empty = _with_player_reminder("")
    assert "coord_*" in empty
    assert "Don't end work turn" in empty


async def test_with_player_reminder_constant_shape() -> None:
    """The canonical reminder text exists, mentions coord_*, and is
    short enough to be cheap on every Player wake."""
    from server.tools import COACH_TO_PLAYER_TURN_END_REMINDER

    assert "coord_*" in COACH_TO_PLAYER_TURN_END_REMINDER
    assert "Coach" in COACH_TO_PLAYER_TURN_END_REMINDER
    # ~80 chars cap as a sanity guardrail — token cost discipline.
    assert len(COACH_TO_PLAYER_TURN_END_REMINDER) < 100


async def test_maybe_wake_agent_paused_does_not_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Harness pause beats every other guard — a wake landing while
    paused is dropped, not queued (otherwise an unpause would unleash
    a flood of stale wakes)."""
    import asyncio

    import server.agents as agents_mod

    captured = await _capture_run_agent_prompt(monkeypatch)
    monkeypatch.setattr(agents_mod, "_paused", True)

    loop = asyncio.get_running_loop()
    fake_task = loop.create_task(asyncio.sleep(60))
    agents_mod._running_tasks["coach"] = fake_task
    try:
        result = await agents_mod.maybe_wake_agent(
            "coach", "while-paused wake"
        )
        for _ in range(3):
            await asyncio.sleep(0)

        assert result is False
        assert captured == []
        assert "coach" not in agents_mod._pending_wakes
    finally:
        fake_task.cancel()
        try:
            await fake_task
        except asyncio.CancelledError:
            pass
        agents_mod._running_tasks.pop("coach", None)

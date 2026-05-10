"""Tests for `server.kanban_watchdog` (Docs/kanban-specs.md §10.7).

What we cover:
  - Tier 1 SQL: working+no-tool-use, idle+with-task, locked-skip,
    coach-skip, recent-tool-use-skip, max_candidates cap.
  - Tier 2 prompt composition: includes task state + recent events.
  - Tier 3 routing: emits `watchdog_finding` for actionable verdicts,
    drops progressing/idle_ok, dedups same-hash within TTL,
    `erroring` also fires `human_attention`, `wake_coach_on_high`
    flag honored.
  - Cost-cap gate.
  - Master kill-switch.
  - JSON parser: bare / fenced / brace-balanced; unknown verdict
    coerces to `idle_ok`.
  - `_build_soft_stalls_rows` (in agents.py): dedup per (agent,
    verdict), cross-check task ownership, drop on task archive.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from server import kanban_watchdog as wd
from server.db import configured_conn, init_db
from server.events import bus


# ---------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def _set_agent(
    *,
    slot: str,
    status: str = "idle",
    current_task_id: str | None = None,
    locked: int = 0,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET status = ?, current_task_id = ?, "
            "locked = ? WHERE id = ?",
            (status, current_task_id, locked, slot),
        )
        await c.commit()
    finally:
        await c.close()


async def _add_event(
    *,
    agent_id: str,
    type_: str,
    ts: datetime | None = None,
    payload: dict[str, Any] | None = None,
    project_id: str = "misc",
) -> int:
    """Direct events-table insert, bypassing the bus (so the SQL
    queries find the rows on the test's schedule, not whenever the
    batched writer flushes)."""
    if ts is None:
        ts = _now()
    if payload is None:
        payload = {}
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO events (ts, agent_id, project_id, type, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (_iso(ts), agent_id, project_id, type_, json.dumps(payload)),
        )
        eid = cur.lastrowid
        await c.commit()
    finally:
        await c.close()
    return eid


async def _ensure_project(project_id: str = "misc") -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (project_id, project_id),
        )
        await c.commit()
    finally:
        await c.close()


async def _create_task(
    *,
    task_id: str,
    project_id: str = "misc",
    status: str = "execute",
    title: str = "Test task",
) -> None:
    await _ensure_project(project_id)
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, "
            "created_by, trajectory) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, project_id, title, status, "coach",
             json.dumps([{"stage": "execute", "to": ["p1"]}])),
        )
        await c.commit()
    finally:
        await c.close()


async def _archive_task(task_id: str) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE tasks SET status = 'archive' WHERE id = ?",
            (task_id,),
        )
        await c.commit()
    finally:
        await c.close()


# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
async def _isolate(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Fresh DB + reset module dedup state + disable cost cap."""
    await init_db()
    await _ensure_project("misc")
    wd._dedup_emitted.clear()

    # Disable cost cap so the sweep doesn't fail-closed.
    from server import agents as agents_mod
    monkeypatch.setattr(agents_mod, "TEAM_DAILY_CAP_USD", 0.0)

    monkeypatch.setenv("HARNESS_WATCHDOG_ENABLED", "true")
    monkeypatch.setenv("HARNESS_WATCHDOG_NO_TOOL_USE_SECONDS", "600")
    monkeypatch.setenv("HARNESS_WATCHDOG_IDLE_WITH_TASK_SECONDS", "600")
    monkeypatch.setenv("HARNESS_WATCHDOG_DEDUP_TTL_SECONDS", "3600")
    monkeypatch.setenv("HARNESS_WATCHDOG_MAX_CANDIDATES", "5")
    monkeypatch.setenv("HARNESS_WATCHDOG_RECENT_EVENTS", "10")
    monkeypatch.setenv("HARNESS_WATCHDOG_WAKE_COACH_ON_HIGH", "false")
    yield
    wd._dedup_emitted.clear()


def _stub_llm(
    monkeypatch: pytest.MonkeyPatch, response_text: str,
    *, is_error: bool = False,
) -> list[dict[str, Any]]:
    """Replace `_call_llm` so we don't actually hit Haiku. Returns the
    `calls` list — appended to per fire with {system, user,
    candidates_count}."""
    calls: list[dict[str, Any]] = []

    async def _fake(
        system: str, user: str, *,
        candidates_count: int, project_id: str | None = None,
    ):
        calls.append({
            "system": system, "user": user,
            "candidates_count": candidates_count,
            "project_id": project_id,
        })
        return wd.WatchdogLLMResult(
            text=response_text,
            is_error=is_error,
            cost_usd=0.005,
            duration_ms=120,
            input_tokens=1500,
            output_tokens=200,
            cache_read_tokens=0,
            cache_creation_tokens=0,
        )

    monkeypatch.setattr(wd, "_call_llm", _fake)
    return calls


# ============================================================ tier 1 SQL


@pytest.mark.asyncio
async def test_tier1_working_no_tool_use_is_candidate() -> None:
    """A working agent whose last tool_use is older than the threshold
    is flagged as a candidate."""
    await _set_agent(slot="p1", status="working")
    # Last tool_use 20 minutes ago (well past the 10-minute default).
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p1", type_="tool_use", ts=old)

    candidates = await wd._tier1_candidates()
    slots = [c.slot for c in candidates]
    assert "p1" in slots
    sig = next(c.signal for c in candidates if c.slot == "p1")
    assert sig == "working_no_tool_use"


@pytest.mark.asyncio
async def test_tier1_working_recent_tool_use_is_not_candidate() -> None:
    """A working agent that called a tool 30s ago is not stuck."""
    await _set_agent(slot="p1", status="working")
    recent = _now() - timedelta(seconds=30)
    await _add_event(agent_id="p1", type_="tool_use", ts=recent)
    candidates = await wd._tier1_candidates()
    assert "p1" not in [c.slot for c in candidates]


@pytest.mark.asyncio
async def test_tier1_working_no_events_skipped() -> None:
    """A working agent with no events at all = freshly spawned. Don't
    flag it on first tick."""
    await _set_agent(slot="p2", status="working")
    candidates = await wd._tier1_candidates()
    assert "p2" not in [c.slot for c in candidates]


@pytest.mark.asyncio
async def test_tier1_idle_with_task_is_candidate() -> None:
    """idle + current_task_id + last event old → candidate."""
    await _create_task(task_id="t-2026-01-01-aaaaaaaa")
    await _set_agent(
        slot="p3", status="idle",
        current_task_id="t-2026-01-01-aaaaaaaa",
    )
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p3", type_="text", ts=old,
                     payload={"text": "wrote spec.md ready for review"})

    candidates = await wd._tier1_candidates()
    slots = [(c.slot, c.signal) for c in candidates]
    assert ("p3", "idle_with_task") in slots


@pytest.mark.asyncio
async def test_tier1_idle_without_task_is_not_candidate() -> None:
    await _set_agent(slot="p3", status="idle", current_task_id=None)
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p3", type_="text", ts=old)
    candidates = await wd._tier1_candidates()
    assert "p3" not in [c.slot for c in candidates]


@pytest.mark.asyncio
async def test_tier1_locked_players_skipped() -> None:
    await _set_agent(slot="p4", status="working", locked=1)
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p4", type_="tool_use", ts=old)
    candidates = await wd._tier1_candidates()
    assert "p4" not in [c.slot for c in candidates]


@pytest.mark.asyncio
async def test_tier1_coach_skipped() -> None:
    """Coach is the watchdog's TARGET, never a candidate."""
    await _set_agent(slot="coach", status="working")
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="coach", type_="tool_use", ts=old)
    candidates = await wd._tier1_candidates()
    assert "coach" not in [c.slot for c in candidates]


@pytest.mark.asyncio
async def test_tier1_working_chatty_no_tool_use_is_candidate() -> None:
    """AUDIT-2026-05-06 regression: a working agent that's been
    emitting `text` events for 20 min without ever calling a tool MUST
    be flagged. Earlier code did `ref = last_tool_dt or last_event_dt`
    which let chatty-but-tool-silent agents slip through."""
    await _set_agent(slot="p1", status="working")
    # Recent text events (5s ago) but no tool_use ever.
    recent = _now() - timedelta(seconds=5)
    older = _now() - timedelta(minutes=15)
    await _add_event(agent_id="p1", type_="text", ts=older,
                     payload={"text": "let me think about this"})
    await _add_event(agent_id="p1", type_="text", ts=recent,
                     payload={"text": "still thinking"})

    candidates = await wd._tier1_candidates()
    slots = [c.slot for c in candidates]
    assert "p1" in slots
    sig = next(c.signal for c in candidates if c.slot == "p1")
    assert sig == "working_no_tool_use"


@pytest.mark.asyncio
async def test_tier1_working_old_tool_use_recent_text_is_candidate() -> None:
    """A working agent whose last tool_use was 15 min ago, even
    if they emitted `text` 5s ago, IS a candidate. Recent text
    events do NOT reset the tool_use timer."""
    await _set_agent(slot="p1", status="working")
    old_tool = _now() - timedelta(minutes=15)
    recent_text = _now() - timedelta(seconds=5)
    await _add_event(agent_id="p1", type_="tool_use", ts=old_tool,
                     payload={"name": "Read", "input": {}})
    await _add_event(agent_id="p1", type_="text", ts=recent_text,
                     payload={"text": "I'll keep going"})

    candidates = await wd._tier1_candidates()
    slots = [c.slot for c in candidates]
    assert "p1" in slots


@pytest.mark.asyncio
async def test_tier1_max_candidates_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive cap — when 8 Players all match, only the configured
    max are returned (oldest-first)."""
    monkeypatch.setenv("HARNESS_WATCHDOG_MAX_CANDIDATES", "3")
    base = _now() - timedelta(minutes=30)
    for idx, slot in enumerate(("p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8")):
        await _set_agent(slot=slot, status="working")
        await _add_event(
            agent_id=slot, type_="tool_use",
            ts=base + timedelta(minutes=idx),
        )
    candidates = await wd._tier1_candidates()
    assert len(candidates) == 3
    # Oldest events first.
    assert [c.slot for c in candidates] == ["p1", "p2", "p3"]


# ============================================================ tier 2 prompt


@pytest.mark.asyncio
async def test_tier2_prompt_includes_task_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composed user prompt names task id + stage + recent events."""
    task_id = "t-2026-01-01-bbbbbbbb"
    await _create_task(task_id=task_id, status="plan", title="Write the API spec")
    await _set_agent(slot="p5", status="idle", current_task_id=task_id)
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p5", type_="text", ts=old,
                     payload={"text": "I've finished, ready for review"})

    calls = _stub_llm(
        monkeypatch,
        '{"verdicts":[{"agent_id":"p5","verdict":"idle_ok","reason":""}]}',
    )
    await wd.sweep_once()

    assert len(calls) == 1
    user = calls[0]["user"]
    assert "p5" in user
    assert task_id in user
    assert "Write the API spec" in user
    assert "stage=plan" in user
    assert "I've finished" in user


# ============================================================ tier 3 routing


@pytest.mark.asyncio
async def test_actionable_verdict_emits_watchdog_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`finished_not_reported` produces a `watchdog_finding` event."""
    task_id = "t-2026-01-01-cccccccc"
    await _create_task(task_id=task_id, status="plan")
    await _set_agent(slot="p6", status="idle", current_task_id=task_id)
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p6", type_="text", ts=old,
                     payload={"text": "wrote the spec"})

    fired: list[dict[str, Any]] = []
    q = bus.subscribe()

    async def _drain():
        while True:
            try:
                ev = await q.get()
                fired.append(ev)
            except Exception:
                return

    import asyncio
    drain_task = asyncio.create_task(_drain())

    _stub_llm(
        monkeypatch,
        '{"verdicts":[{"agent_id":"p6","verdict":"finished_not_reported",'
        '"reason":"declared done in chat, never called coord_write_task_spec"}]}',
    )
    emitted = await wd.sweep_once()
    await asyncio.sleep(0.05)
    drain_task.cancel()
    bus.unsubscribe(q)

    assert emitted == 1
    findings = [e for e in fired if e.get("type") == "watchdog_finding"]
    assert len(findings) == 1
    f = findings[0]
    assert f["subject_agent"] == "p6"
    assert f["verdict"] == "finished_not_reported"
    assert f["task_id"] == task_id
    assert "coord_write_task_spec" in f["reason"]
    assert f["to"] == "coach"


@pytest.mark.asyncio
async def test_finding_event_carries_pinned_project_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUDIT-2026-05-06: project_id is captured at sweep start and
    stamped explicitly on `watchdog_finding`, so a mid-sweep project
    switch can't land findings on the wrong project's Coach rollup."""
    import asyncio as _asyncio

    task_id = "t-2026-01-01-99999991"
    await _ensure_project("alpha")
    await _create_task(task_id=task_id, project_id="alpha", status="plan")
    await _set_agent(slot="p1", status="idle", current_task_id=task_id)
    old = _now() - timedelta(minutes=20)
    await _add_event(
        agent_id="p1", type_="text", ts=old,
        payload={"text": "wrote the spec"}, project_id="alpha",
    )

    # Force resolve_active_project to "alpha" at sweep start so we
    # observe the pinned id even when bus.publish would auto-stamp
    # something else (the active project in this test fixture is the
    # default "misc").
    from server import db as dbmod

    async def _fake_active(*args, **kwargs):
        return "alpha"

    monkeypatch.setattr(dbmod, "resolve_active_project", _fake_active)

    fired: list[dict[str, Any]] = []
    q = bus.subscribe()

    async def _drain():
        while True:
            try:
                ev = await q.get()
                fired.append(ev)
            except Exception:
                return

    drain_task = _asyncio.create_task(_drain())
    calls = _stub_llm(
        monkeypatch,
        '{"verdicts":[{"agent_id":"p1","verdict":"finished_not_reported",'
        '"reason":"declared done in chat"}]}',
    )
    await wd.sweep_once()
    await _asyncio.sleep(0.05)
    drain_task.cancel()
    bus.unsubscribe(q)

    # The pinned id reaches `_call_llm` (and thus `_call_haiku` →
    # `watchdog_llm_call` event in real usage).
    assert calls and calls[0]["project_id"] == "alpha"

    # The published `watchdog_finding` event carries the explicit
    # project_id, overriding bus.publish's auto-stamp from the
    # currently-active project.
    findings = [e for e in fired if e.get("type") == "watchdog_finding"]
    assert len(findings) == 1
    assert findings[0]["project_id"] == "alpha"


@pytest.mark.asyncio
async def test_progressing_and_idle_ok_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drop verdicts that don't warrant Coach attention."""
    await _set_agent(slot="p7", status="working")
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p7", type_="tool_use", ts=old)

    _stub_llm(
        monkeypatch,
        '{"verdicts":[{"agent_id":"p7","verdict":"progressing","reason":"fine"}]}',
    )
    emitted = await wd.sweep_once()
    assert emitted == 0


@pytest.mark.asyncio
async def test_dedup_blocks_repeat_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same observation hashes the same → can't re-fire within TTL."""
    task_id = "t-2026-01-01-dddddddd"
    await _create_task(task_id=task_id, status="plan")
    await _set_agent(slot="p1", status="idle", current_task_id=task_id)
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p1", type_="text", ts=old,
                     payload={"text": "done"})

    _stub_llm(
        monkeypatch,
        '{"verdicts":[{"agent_id":"p1","verdict":"blocked","reason":"x"}]}',
    )
    first = await wd.sweep_once()
    second = await wd.sweep_once()
    assert first == 1
    assert second == 0  # same hash → blocked by dedup


@pytest.mark.asyncio
async def test_erroring_fires_human_attention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`erroring` always fires human_attention regardless of the
    high-severity wake flag."""
    await _set_agent(slot="p2", status="working")
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p2", type_="tool_use", ts=old)

    fired: list[dict[str, Any]] = []
    q = bus.subscribe()

    async def _drain():
        while True:
            try:
                ev = await q.get()
                fired.append(ev)
            except Exception:
                return

    import asyncio
    drain_task = asyncio.create_task(_drain())

    _stub_llm(
        monkeypatch,
        '{"verdicts":[{"agent_id":"p2","verdict":"erroring","reason":"hung on tool err"}]}',
    )
    await wd.sweep_once()
    await asyncio.sleep(0.05)
    drain_task.cancel()
    bus.unsubscribe(q)

    assert any(e.get("type") == "human_attention" for e in fired)
    assert any(e.get("type") == "watchdog_finding" for e in fired)


@pytest.mark.asyncio
async def test_wake_coach_flag_off_no_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `HARNESS_WATCHDOG_WAKE_COACH_ON_HIGH=false` (default),
    no out-of-band Coach wake fires."""
    await _set_agent(slot="p1", status="working")
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p1", type_="tool_use", ts=old)

    wake_calls: list[tuple[str, ...]] = []

    async def _fake_wake(slot: str, body: str, **kwargs: Any) -> bool:
        wake_calls.append((slot, body))
        return True

    from server import agents as agents_mod
    monkeypatch.setattr(agents_mod, "maybe_wake_agent", _fake_wake)

    _stub_llm(
        monkeypatch,
        '{"verdicts":[{"agent_id":"p1","verdict":"blocked","reason":"x"}]}',
    )
    await wd.sweep_once()
    assert wake_calls == []


@pytest.mark.asyncio
async def test_wake_coach_flag_on_fires_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag on, blocked + erroring trigger a Coach wake."""
    monkeypatch.setenv("HARNESS_WATCHDOG_WAKE_COACH_ON_HIGH", "true")
    await _set_agent(slot="p1", status="working")
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p1", type_="tool_use", ts=old)

    wake_calls: list[tuple[str, str]] = []

    async def _fake_wake(slot: str, body: str, **kwargs: Any) -> bool:
        wake_calls.append((slot, body))
        return True

    from server import agents as agents_mod
    monkeypatch.setattr(agents_mod, "maybe_wake_agent", _fake_wake)

    _stub_llm(
        monkeypatch,
        '{"verdicts":[{"agent_id":"p1","verdict":"blocked","reason":"x"}]}',
    )
    await wd.sweep_once()
    assert wake_calls and wake_calls[0][0] == "coach"
    assert "p1" in wake_calls[0][1]


@pytest.mark.asyncio
async def test_finished_not_reported_self_nudge_to_assignee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2.0.2 (2026-05-08) — when the watchdog detects
    `finished_not_reported`, fire a self-correction wake DIRECTLY to
    the Player so they get a chance to call the completion tool
    before Coach has to use the on_behalf_of override path. The
    user's recurring failure mode: Players write the deliverable to
    disk and return idle without messaging Coach.

    The self-nudge must (post-2026-05-10 wake-strip):
    - target the assignee (cand.slot), NOT Coach
    - call out the disk-write misconception (load-bearing — the
      recurring production failure pattern the watchdog exists
      to catch; v2 strip kept this one corrective sentence)
    - point at the completion tool generically (the four specific
      tool names live in the system prompt now)
    - fire even when `HARNESS_WATCHDOG_WAKE_COACH_ON_HIGH=false`
      (the default) since this isn't a high-severity Coach wake
      — it's a Player self-correction nudge
    - end with the canonical turn-end reminder
    """
    await _set_agent(slot="p1", status="working")
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p1", type_="tool_use", ts=old)

    wake_calls: list[tuple[str, str]] = []

    async def _fake_wake(slot: str, body: str, **kwargs: Any) -> bool:
        wake_calls.append((slot, body))
        return True

    from server import agents as agents_mod
    monkeypatch.setattr(agents_mod, "maybe_wake_agent", _fake_wake)

    _stub_llm(
        monkeypatch,
        '{"verdicts":[{"agent_id":"p1","verdict":"finished_not_reported",'
        '"reason":"wrote spec.md to disk; never called coord_write_task_spec"}]}',
    )
    await wd.sweep_once()

    # Self-nudge fired AT p1 (not Coach).
    p1_wakes = [(s, b) for s, b in wake_calls if s == "p1"]
    assert p1_wakes, f"expected p1 self-nudge wake; got {wake_calls}"
    body = p1_wakes[0][1]
    # Disk-write misconception correction stays.
    assert "disk-write" in body.lower() or "disk write" in body.lower()
    # Points at the completion tool (generic — names live in the
    # system prompt now, not the wake).
    assert "completion tool" in body.lower()
    # Canonical turn-end reminder is appended.
    from server.tools import COACH_TO_PLAYER_TURN_END_REMINDER
    assert COACH_TO_PLAYER_TURN_END_REMINDER.strip() in body


# ============================================================ gates


@pytest.mark.asyncio
async def test_master_killswitch_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`HARNESS_WATCHDOG_ENABLED=false` → sweep returns 0 and no LLM call."""
    monkeypatch.setenv("HARNESS_WATCHDOG_ENABLED", "false")
    await _set_agent(slot="p1", status="working")
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p1", type_="tool_use", ts=old)

    calls = _stub_llm(
        monkeypatch,
        '{"verdicts":[{"agent_id":"p1","verdict":"blocked","reason":"x"}]}',
    )
    emitted = await wd.sweep_once()
    assert emitted == 0
    assert calls == []  # tier 2 never invoked


@pytest.mark.asyncio
async def test_cost_cap_gate_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When team daily spend >= cap, the watchdog skips."""
    from server import agents as agents_mod
    monkeypatch.setattr(agents_mod, "TEAM_DAILY_CAP_USD", 1.0)

    async def _fake_spend(*args, **kwargs):
        return 5.0  # over cap

    monkeypatch.setattr(agents_mod, "_today_spend", _fake_spend)

    await _set_agent(slot="p1", status="working")
    old = _now() - timedelta(minutes=20)
    await _add_event(agent_id="p1", type_="tool_use", ts=old)

    calls = _stub_llm(
        monkeypatch,
        '{"verdicts":[{"agent_id":"p1","verdict":"blocked","reason":"x"}]}',
    )
    emitted = await wd.sweep_once()
    assert emitted == 0
    assert calls == []  # LLM never invoked when over cap


@pytest.mark.asyncio
async def test_no_candidates_no_llm_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All agents healthy → tier 1 returns nothing, no Haiku call."""
    calls = _stub_llm(monkeypatch, "{}")
    emitted = await wd.sweep_once()
    assert emitted == 0
    assert calls == []


# ============================================================ json parser


def test_parse_verdicts_bare_json() -> None:
    text = '{"verdicts":[{"agent_id":"p1","verdict":"blocked","reason":"r"}]}'
    candidates = [wd.Candidate(
        slot="p1", signal="working_no_tool_use", status="working",
        current_task_id=None, last_event_ts=None, last_tool_use_ts=None,
    )]
    out = wd._parse_verdicts_response(text, candidates)
    assert len(out) == 1
    assert out[0]["verdict"] == "blocked"
    assert out[0]["reason"] == "r"


def test_parse_verdicts_code_fence() -> None:
    text = (
        "Here you go:\n```json\n"
        '{"verdicts":[{"agent_id":"p1","verdict":"looping","reason":"loops"}]}'
        "\n```"
    )
    candidates = [wd.Candidate(
        slot="p1", signal="working_no_tool_use", status="working",
        current_task_id=None, last_event_ts=None, last_tool_use_ts=None,
    )]
    out = wd._parse_verdicts_response(text, candidates)
    assert out[0]["verdict"] == "looping"


def test_parse_verdicts_brace_balance() -> None:
    text = (
        "After thinking, "
        '{"verdicts":[{"agent_id":"p1","verdict":"erroring","reason":"err"}]}'
        " — that's my call."
    )
    candidates = [wd.Candidate(
        slot="p1", signal="working_no_tool_use", status="working",
        current_task_id=None, last_event_ts=None, last_tool_use_ts=None,
    )]
    out = wd._parse_verdicts_response(text, candidates)
    assert out[0]["verdict"] == "erroring"


def test_parse_verdicts_unknown_coerced_to_idle_ok() -> None:
    text = '{"verdicts":[{"agent_id":"p1","verdict":"hallucinated","reason":""}]}'
    candidates = [wd.Candidate(
        slot="p1", signal="working_no_tool_use", status="working",
        current_task_id=None, last_event_ts=None, last_tool_use_ts=None,
    )]
    out = wd._parse_verdicts_response(text, candidates)
    assert out[0]["verdict"] == "idle_ok"


def test_parse_verdicts_missing_candidates_default_idle_ok() -> None:
    text = '{"verdicts":[]}'
    candidates = [wd.Candidate(
        slot="p1", signal="working_no_tool_use", status="working",
        current_task_id=None, last_event_ts=None, last_tool_use_ts=None,
    )]
    out = wd._parse_verdicts_response(text, candidates)
    assert out[0]["verdict"] == "idle_ok"


def test_parse_verdicts_handles_garbage() -> None:
    candidates = [wd.Candidate(
        slot="p1", signal="working_no_tool_use", status="working",
        current_task_id=None, last_event_ts=None, last_tool_use_ts=None,
    )]
    out = wd._parse_verdicts_response("not json at all", candidates)
    assert out[0]["verdict"] == "idle_ok"


# ============================================================ coach rollup


@pytest.mark.asyncio
async def test_soft_stalls_rows_dedup_per_agent_verdict() -> None:
    """When two findings emit for the same (agent, verdict), the
    rollup builder shows the most recent one only."""
    from server.agents import _build_soft_stalls_rows

    task_id = "t-2026-01-01-eeeeeeee"
    await _create_task(task_id=task_id)
    # Bind the agent to this task so the cross-check passes.
    await _set_agent(slot="p1", current_task_id=task_id)
    # Two findings, same (agent, verdict). The most recent (latest id)
    # should win.
    await _add_event(
        agent_id="system", type_="watchdog_finding",
        payload={
            "subject_agent": "p1", "verdict": "blocked",
            "reason": "old reason", "task_id": task_id,
        },
    )
    await _add_event(
        agent_id="system", type_="watchdog_finding",
        payload={
            "subject_agent": "p1", "verdict": "blocked",
            "reason": "new reason", "task_id": task_id,
        },
    )

    rows = await _build_soft_stalls_rows("misc")
    blocked = [r for r in rows if r["agent"] == "p1" and r["verdict"] == "blocked"]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "new reason"


@pytest.mark.asyncio
async def test_soft_stalls_rows_drop_when_task_changed() -> None:
    """A finding referencing task X is dropped when the agent moved
    on to task Y in the meantime."""
    from server.agents import _build_soft_stalls_rows

    old_task = "t-2026-01-01-ffffff11"
    new_task = "t-2026-01-01-ffffff22"
    await _create_task(task_id=old_task)
    await _create_task(task_id=new_task)
    await _set_agent(slot="p1", current_task_id=new_task)

    await _add_event(
        agent_id="system", type_="watchdog_finding",
        payload={
            "subject_agent": "p1", "verdict": "finished_not_reported",
            "reason": "finished but never advanced", "task_id": old_task,
        },
    )
    rows = await _build_soft_stalls_rows("misc")
    assert all(r["task_id"] != old_task for r in rows)


@pytest.mark.asyncio
async def test_soft_stalls_rows_drop_when_task_archived() -> None:
    """A finding for an archived task is stale; drop it."""
    from server.agents import _build_soft_stalls_rows

    task_id = "t-2026-01-01-ffffff33"
    await _create_task(task_id=task_id)
    await _set_agent(slot="p1", current_task_id=task_id)
    await _add_event(
        agent_id="system", type_="watchdog_finding",
        payload={
            "subject_agent": "p1", "verdict": "looping",
            "reason": "spinning", "task_id": task_id,
        },
    )
    await _archive_task(task_id)

    rows = await _build_soft_stalls_rows("misc")
    assert all(r["task_id"] != task_id for r in rows)


@pytest.mark.asyncio
async def test_soft_stalls_rows_no_task_id_pass_through() -> None:
    """Findings without a task_id (rare — should still surface)."""
    from server.agents import _build_soft_stalls_rows

    await _set_agent(slot="p2")
    await _add_event(
        agent_id="system", type_="watchdog_finding",
        payload={
            "subject_agent": "p2", "verdict": "looping",
            "reason": "spinning forever", "task_id": "",
        },
    )
    rows = await _build_soft_stalls_rows("misc")
    looping = [r for r in rows if r["agent"] == "p2" and r["verdict"] == "looping"]
    assert len(looping) == 1
    assert looping[0]["task_id"] is None

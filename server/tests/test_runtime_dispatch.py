"""Runtime protocol contract tests.

PR 2 — this is the structural contract. A FakeRuntime exercises the
protocol shape the dispatcher relies on. The full dispatcher contract
test (cost-cap rejects before run_turn, agent_started before, etc.)
lands once the run_agent carve-out completes.
"""

from __future__ import annotations

import pytest

from server.runtimes import AgentRuntime, ClaudeRuntime, TurnContext, get_runtime


class FakeRuntime:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_turn(self, tc: TurnContext) -> None:
        self.calls.append("run_turn")
        tc.turn_ctx["got_result"] = True

    async def maybe_auto_compact(self, tc: TurnContext) -> bool:
        self.calls.append("maybe_auto_compact")
        return False

    async def run_manual_compact(self, tc: TurnContext) -> None:
        self.calls.append("run_manual_compact")


def _make_tc() -> TurnContext:
    return TurnContext(
        agent_id="p1",
        project_id="default",
        prompt="hello",
        system_prompt="sys",
        workspace_cwd="/tmp",
        allowed_tools=["Read"],
        external_mcp_servers={},
    )


def test_fake_runtime_satisfies_protocol() -> None:
    rt = FakeRuntime()
    assert isinstance(rt, AgentRuntime)


def test_claude_runtime_satisfies_protocol() -> None:
    rt = ClaudeRuntime()
    assert isinstance(rt, AgentRuntime)
    assert rt.name == "claude"


def test_get_runtime_resolves_claude() -> None:
    rt = get_runtime("claude")
    assert rt.name == "claude"


def test_get_runtime_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        get_runtime("definitely-not-a-runtime")


async def test_fake_runtime_records_calls() -> None:
    rt = FakeRuntime()
    tc = _make_tc()
    assert await rt.maybe_auto_compact(tc) is False
    await rt.run_turn(tc)
    assert tc.turn_ctx["got_result"] is True
    await rt.run_manual_compact(tc)
    assert rt.calls == ["maybe_auto_compact", "run_turn", "run_manual_compact"]


async def test_claude_maybe_auto_compact_short_circuits_in_compact_mode() -> None:
    rt = ClaudeRuntime()
    tc = _make_tc()
    tc.compact_mode = True
    # No DB hit, no _emit — must return False before any side effect.
    assert await rt.maybe_auto_compact(tc) is False


async def test_claude_maybe_auto_compact_off_when_threshold_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_AUTO_COMPACT_THRESHOLD", "0.0")
    rt = ClaudeRuntime()
    tc = _make_tc()
    assert await rt.maybe_auto_compact(tc) is False


def test_dispatcher_does_not_import_sdk_symbols() -> None:
    """The audit-item-1 carve-out moved all SDK-bound code into
    ClaudeRuntime. agents.py should no longer reference
    `ClaudeAgentOptions`, `HookMatcher`, `query`, or `build_coord_server`
    directly. Catches accidental re-imports during future edits.
    """
    import server.agents as agentsmod

    # Module-level globals only — `_pretool_continue_hook` etc. are
    # still defined as locals but the SDK option/iter classes must
    # not be globally bound here.
    assert not hasattr(agentsmod, "ClaudeAgentOptions"), (
        "ClaudeAgentOptions leaked back into server.agents — the "
        "carve-out must keep SDK options inside ClaudeRuntime."
    )
    assert not hasattr(agentsmod, "build_coord_server"), (
        "build_coord_server leaked back into server.agents."
    )


async def test_claude_run_manual_compact_sets_compact_mode_and_delegates() -> None:
    """ClaudeRuntime.run_manual_compact must ensure compact_mode is set
    on both the dataclass field and the turn_ctx dict, then delegate
    to run_turn. This pins the contract that the runtime — not the
    dispatcher — owns the manual-compact entry point.
    """
    rt = ClaudeRuntime()
    tc = TurnContext(
        agent_id="p1",
        project_id="default",
        prompt="please summarize",
        system_prompt="sys",
        workspace_cwd="/tmp",
        allowed_tools=["Read"],
        external_mcp_servers={},
        compact_mode=False,
    )
    captured: dict = {}

    async def _fake_run_turn(self_, tc_inner):
        captured["compact_mode_field"] = tc_inner.compact_mode
        captured["compact_mode_dict"] = tc_inner.turn_ctx.get("compact_mode")

    # Monkey-patch on the instance to capture the call without
    # actually invoking the SDK.
    import types
    rt.run_turn = types.MethodType(_fake_run_turn, rt)
    await rt.run_manual_compact(tc)

    assert captured["compact_mode_field"] is True, (
        "run_manual_compact must set tc.compact_mode=True before delegating"
    )
    assert captured["compact_mode_dict"] is True, (
        "run_manual_compact must mirror compact_mode into turn_ctx so "
        "_handle_message's ResultMessage path writes continuity_note"
    )


async def test_resolve_runtime_for_per_slot_override(fresh_db: str) -> None:
    """Resolution order: agents.runtime_override → role default → 'claude'."""
    import server.db as dbmod
    await dbmod.init_db()
    from server.agents import _resolve_runtime_for

    # Default — no override, no team_config → 'claude'.
    assert await _resolve_runtime_for("p1") == "claude"

    # Per-slot override wins over absent team default.
    import aiosqlite
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "UPDATE agents SET runtime_override = 'codex' WHERE id = 'p1'"
        )
        await db.commit()
    assert await _resolve_runtime_for("p1") == "codex"

    # Clear the override; team default should take effect.
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "UPDATE agents SET runtime_override = NULL WHERE id = 'p1'"
        )
        await db.execute(
            "INSERT INTO team_config (key, value) VALUES "
            "('players_default_runtime', 'codex')"
        )
        await db.commit()
    assert await _resolve_runtime_for("p1") == "codex"

    # Coach reads its own role default key.
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "INSERT INTO team_config (key, value) VALUES "
            "('coach_default_runtime', 'claude')"
        )
        await db.commit()
    assert await _resolve_runtime_for("coach") == "claude"


async def test_claude_maybe_auto_compact_off_when_threshold_unparseable(
    monkeypatch: pytest.MonkeyPatch,
    fresh_db: str,
) -> None:
    # Unparseable falls back to default 0.7 — which is in (0, 1) — so
    # the runtime falls through to _get_session_id. In a fresh DB
    # there is no session for "p1", so it returns False from the
    # no-prior branch. The real coverage is that the lazy import path
    # to server.agents resolves without ImportError.
    import server.db as dbmod
    await dbmod.init_db()
    monkeypatch.setenv("HARNESS_AUTO_COMPACT_THRESHOLD", "not-a-number")
    rt = ClaudeRuntime()
    tc = _make_tc()
    result = await rt.maybe_auto_compact(tc)
    assert result is False


async def test_run_agent_uses_codex_thread_id_for_started_resume_flag(
    monkeypatch: pytest.MonkeyPatch,
    fresh_db: str,
) -> None:
    """Codex agent_started.resumed_session must read codex_thread_id,
    not Claude's session_id column."""
    import server.db as dbmod
    await dbmod.init_db()

    import server.agents as agentsmod
    import server.runtimes as runtimes_mod
    import server.runtimes.codex as codex_mod
    from server.events import bus

    class _Runtime:
        name = "codex"

        async def maybe_auto_compact(self, tc):
            return False

        async def run_turn(self, tc):
            tc.turn_ctx["got_result"] = True

        async def run_manual_compact(self, tc):
            tc.turn_ctx["got_result"] = True

    async def fail_get_session(agent_id):
        raise AssertionError("Claude session_id should not be read for Codex")

    async def get_codex_thread(agent_id):
        return "codex_thread_existing"

    async def runtime_for(agent_id):
        return "codex"

    monkeypatch.setattr(agentsmod, "_resolve_runtime_for", runtime_for)
    monkeypatch.setattr(runtimes_mod, "get_runtime", lambda name: _Runtime())
    monkeypatch.setattr(agentsmod, "_get_session_id", fail_get_session)
    monkeypatch.setattr(codex_mod, "_get_codex_thread_id", get_codex_thread)

    q = bus.subscribe()
    try:
        await agentsmod.run_agent("p1", "hello")
        started = None
        while True:
            ev = await q.get()
            if ev.get("type") == "agent_started":
                started = ev
                break
        assert started["runtime"] == "codex"
        assert started["resumed_session"] is True
    finally:
        bus.unsubscribe(q)


async def test_run_agent_uses_codex_prepared_resume_flag_for_started_event(
    monkeypatch: pytest.MonkeyPatch,
    fresh_db: str,
) -> None:
    """Codex pre-start preparation can downgrade a stale stored thread
    to a fresh-start `agent_started` event before the turn renders."""
    import server.db as dbmod
    await dbmod.init_db()

    import server.agents as agentsmod
    import server.runtimes as runtimes_mod
    import server.runtimes.codex as codex_mod
    from server.events import bus

    calls: list[str] = []

    class _Runtime:
        name = "codex"

        async def maybe_auto_compact(self, tc):
            calls.append("maybe_auto_compact")
            return False

        async def prepare_turn_start(self, tc):
            calls.append("prepare_turn_start")
            assert tc.prior_session == "codex_thread_stale"
            return False

        async def run_turn(self, tc):
            calls.append("run_turn")
            tc.turn_ctx["got_result"] = True

        async def run_manual_compact(self, tc):
            calls.append("run_manual_compact")
            tc.turn_ctx["got_result"] = True

    runtime = _Runtime()

    async def get_codex_thread(agent_id):
        return "codex_thread_stale"

    async def runtime_for(agent_id):
        return "codex"

    monkeypatch.setattr(agentsmod, "_resolve_runtime_for", runtime_for)
    monkeypatch.setattr(runtimes_mod, "get_runtime", lambda name: runtime)
    monkeypatch.setattr(codex_mod, "_get_codex_thread_id", get_codex_thread)

    q = bus.subscribe()
    try:
        await agentsmod.run_agent("p1", "hello")
        started = None
        while True:
            ev = await q.get()
            if ev.get("type") == "agent_started":
                started = ev
                break
        assert started["runtime"] == "codex"
        assert started["resumed_session"] is False
        assert calls == ["maybe_auto_compact", "prepare_turn_start", "run_turn"]
    finally:
        bus.unsubscribe(q)

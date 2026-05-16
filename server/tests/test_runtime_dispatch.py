"""Runtime protocol contract tests.

PR 2 — this is the structural contract. A FakeRuntime exercises the
protocol shape the dispatcher relies on. The full dispatcher contract
test (cost-cap rejects before run_turn, agent_started before, etc.)
lands once the run_agent carve-out completes.
"""

from __future__ import annotations

import json

import pytest

from server.runtimes import AgentRuntime, ClaudeRuntime, TurnContext, get_runtime


class FakeRuntime:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def prepare_turn_start(self, tc: TurnContext) -> bool:
        self.calls.append("prepare_turn_start")
        return bool(tc.prior_session)

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
    assert await rt.prepare_turn_start(tc) is False
    await rt.run_turn(tc)
    assert tc.turn_ctx["got_result"] is True
    await rt.run_manual_compact(tc)
    assert rt.calls == [
        "maybe_auto_compact",
        "prepare_turn_start",
        "run_turn",
        "run_manual_compact",
    ]


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


async def test_get_role_default_model_is_runtime_aware(fresh_db: str) -> None:
    """Codex defaults are stored separately from Claude defaults."""
    import server.db as dbmod
    await dbmod.init_db()
    from server.agents import _get_role_default_model

    import aiosqlite
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "INSERT INTO team_config (key, value) VALUES "
            "('coach_default_model', 'claude-opus-4-7')"
        )
        await db.execute(
            "INSERT INTO team_config (key, value) VALUES "
            "('coach_default_model_codex', 'gpt-5.5')"
        )
        await db.execute(
            "INSERT INTO team_config (key, value) VALUES "
            "('players_default_model', 'claude-sonnet-4-6')"
        )
        await db.execute(
            "INSERT INTO team_config (key, value) VALUES "
            "('players_default_model_codex', 'gpt-5.4-mini')"
        )
        await db.commit()

    assert await _get_role_default_model("coach", "claude") == "claude-opus-4-7"
    assert await _get_role_default_model("coach", "codex") == "gpt-5.5"
    assert await _get_role_default_model("p1", "claude") == "claude-sonnet-4-6"
    assert await _get_role_default_model("p1", "codex") == "gpt-5.4-mini"


async def test_get_role_default_model_does_not_fall_back_to_claude_for_codex(
    fresh_db: str,
) -> None:
    """A Codex turn with no Codex `team_config` row uses the hardcoded
    Codex Players default (`latest_mini`) — never the Claude default,
    even if the Claude side is set."""
    import server.db as dbmod
    await dbmod.init_db()
    from server.agents import _get_role_default_model

    import aiosqlite
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "INSERT INTO team_config (key, value) VALUES "
            "('players_default_model', 'claude-sonnet-4-6')"
        )
        await db.commit()

    assert await _get_role_default_model("p1", "codex") == "latest_mini"


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


async def test_run_agent_passes_codex_role_allowlist_to_mcp_config(
    monkeypatch: pytest.MonkeyPatch,
    fresh_db: str,
) -> None:
    """A Codex Player spawn must carry the role-scoped allowlist all the
    way into the runtime MCP config, not just store it on agents."""
    import server.db as dbmod
    await dbmod.init_db()

    import aiosqlite
    import server.agents as agentsmod
    import server.runtimes as runtimes_mod
    from server.role_tool_allowlists import tools_json_for_role

    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "UPDATE agents SET allowed_tools = ? WHERE id = 'p2'",
            (tools_json_for_role("executor"),),
        )
        await db.commit()

    captured: dict[str, object] = {}

    class _Runtime:
        name = "codex"

        async def maybe_auto_compact(self, tc):
            return False

        async def run_turn(self, tc):
            from server.runtimes.codex import _build_mcp_servers

            servers = _build_mcp_servers(tc)
            args = servers["coord"]["args"]
            allowed_arg = args[args.index("--allowed-tools") + 1]
            captured["allowed_tools"] = tc.allowed_tools
            captured["coord_enabled_tools"] = servers["coord"]["enabled_tools"]
            captured["coord_allowed_arg"] = json.loads(allowed_arg)
            tc.turn_ctx["got_result"] = True

        async def run_manual_compact(self, tc):
            await self.run_turn(tc)

    async def runtime_for(agent_id):
        return "codex"

    monkeypatch.setattr(agentsmod, "_resolve_runtime_for", runtime_for)
    monkeypatch.setattr(runtimes_mod, "get_runtime", lambda name: _Runtime())

    await agentsmod.run_agent("p2", "hello")

    allowed_tools = set(captured["allowed_tools"])
    assert "mcp__coord__coord_commit_push" in allowed_tools
    assert "mcp__coord__coord_approve_stage" not in allowed_tools
    assert captured["coord_enabled_tools"] == captured["coord_allowed_arg"]
    assert "coord_commit_push" in captured["coord_enabled_tools"]
    assert "coord_approve_stage" not in captured["coord_enabled_tools"]


async def test_run_agent_passes_codex_shipper_gate_to_mcp_config(
    monkeypatch: pytest.MonkeyPatch,
    fresh_db: str,
) -> None:
    """Ship-stage Codex spawns must expose the normal gated ship tool."""
    import server.db as dbmod
    await dbmod.init_db()

    import aiosqlite
    import server.agents as agentsmod
    import server.runtimes as runtimes_mod
    from server.role_tool_allowlists import tools_json_for_role

    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "UPDATE agents SET allowed_tools = ? WHERE id = 'p3'",
            (tools_json_for_role("shipper"),),
        )
        await db.commit()

    captured: dict[str, object] = {}

    class _Runtime:
        name = "codex"

        async def maybe_auto_compact(self, tc):
            return False

        async def run_turn(self, tc):
            from server.runtimes.codex import _build_mcp_servers

            servers = _build_mcp_servers(tc)
            args = servers["coord"]["args"]
            allowed_arg = args[args.index("--allowed-tools") + 1]
            captured["allowed_tools"] = tc.allowed_tools
            captured["coord_enabled_tools"] = servers["coord"]["enabled_tools"]
            captured["coord_allowed_arg"] = json.loads(allowed_arg)
            tc.turn_ctx["got_result"] = True

        async def run_manual_compact(self, tc):
            await self.run_turn(tc)

    async def runtime_for(agent_id):
        return "codex"

    monkeypatch.setattr(agentsmod, "_resolve_runtime_for", runtime_for)
    monkeypatch.setattr(runtimes_mod, "get_runtime", lambda name: _Runtime())

    await agentsmod.run_agent("p3", "hello")

    allowed_tools = set(captured["allowed_tools"])
    assert "mcp__coord__coord_ship_to_dev" in allowed_tools
    assert "mcp__coord__coord_role_complete" in allowed_tools
    assert "mcp__coord__coord_approve_stage" not in allowed_tools
    assert captured["coord_enabled_tools"] == captured["coord_allowed_arg"]
    assert "coord_ship_to_dev" in captured["coord_enabled_tools"]
    assert "coord_approve_stage" not in captured["coord_enabled_tools"]


async def test_run_agent_refreshes_stale_shipper_allowed_tools(
    monkeypatch: pytest.MonkeyPatch,
    fresh_db: str,
) -> None:
    """Existing ship rows must pick up newly-added role tools.

    Role assignments persist a JSON snapshot on agents.allowed_tools. When
    the role allowlist changes after assignment, a later Codex turn must not
    keep spawning from the stale snapshot.
    """
    import server.db as dbmod
    await dbmod.init_db()

    import aiosqlite
    import server.agents as agentsmod
    import server.runtimes as runtimes_mod
    from server.role_tool_allowlists import tools_for_role

    stale_shipper_tools = [
        t for t in tools_for_role("shipper")
        if t != "mcp__coord__coord_ship_to_dev"
    ]

    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "UPDATE agents SET allowed_tools = ? WHERE id = 'p3'",
            (json.dumps(stale_shipper_tools),),
        )
        await db.execute(
            "INSERT INTO tasks "
            "(id, project_id, title, status, owner, created_by, trajectory) "
            "VALUES (?, 'default', 'ship stale tools', 'ship', 'p2', "
            "'coach', ?)",
            (
                "t-2026-05-15-staletls",
                json.dumps([
                    {"stage": "execute", "to": ["p2"]},
                    {"stage": "audit_syntax", "to": ["p4"]},
                    {"stage": "ship", "to": ["p3"]},
                ]),
            ),
        )
        await db.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, claimed_at) "
            "VALUES (?, 'shipper', '[]', 'p3', "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            ("t-2026-05-15-staletls",),
        )
        await db.commit()

    captured: dict[str, object] = {}

    class _Runtime:
        name = "codex"

        async def maybe_auto_compact(self, tc):
            return False

        async def run_turn(self, tc):
            from server.runtimes.codex import _build_mcp_servers

            servers = _build_mcp_servers(tc)
            captured["allowed_tools"] = tc.allowed_tools
            captured["coord_enabled_tools"] = servers["coord"]["enabled_tools"]
            tc.turn_ctx["got_result"] = True

        async def run_manual_compact(self, tc):
            await self.run_turn(tc)

    async def runtime_for(agent_id):
        return "codex"

    monkeypatch.setattr(agentsmod, "_resolve_runtime_for", runtime_for)
    monkeypatch.setattr(runtimes_mod, "get_runtime", lambda name: _Runtime())

    await agentsmod.run_agent("p3", "hello")

    allowed_tools = set(captured["allowed_tools"])
    assert "mcp__coord__coord_ship_to_dev" in allowed_tools
    assert "coord_ship_to_dev" in captured["coord_enabled_tools"]

    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        cur = await db.execute(
            "SELECT allowed_tools FROM agents WHERE id = 'p3'"
        )
        row = await cur.fetchone()
    assert "mcp__coord__coord_ship_to_dev" in set(json.loads(row[0]))


async def test_run_agent_prefers_current_task_role_when_refreshing_tools(
    monkeypatch: pytest.MonkeyPatch,
    fresh_db: str,
) -> None:
    """A pending ship role must not leak ship tools into an executor turn.

    Existing Codex sessions can carry stale role-tool snapshots. When a
    slot has multiple active role rows, the dispatcher must refresh from
    the row for agents.current_task_id first, not whichever row was most
    recently assigned.
    """
    import server.db as dbmod
    await dbmod.init_db()

    import aiosqlite
    import server.agents as agentsmod
    import server.runtimes as runtimes_mod
    from server.role_tool_allowlists import tools_json_for_role

    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "UPDATE agents SET current_task_id = ?, allowed_tools = ? "
            "WHERE id = 'p3'",
            ("t-2026-05-15-execrow", tools_json_for_role("idle")),
        )
        await db.execute(
            "INSERT INTO tasks "
            "(id, project_id, title, status, owner, created_by, trajectory) "
            "VALUES (?, 'default', 'current executor', 'execute', 'p3', "
            "'coach', ?)",
            (
                "t-2026-05-15-execrow",
                json.dumps([
                    {"stage": "execute", "to": ["p3"]},
                    {"stage": "ship", "to": ["p3"]},
                ]),
            ),
        )
        await db.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, claimed_at, assigned_at) "
            "VALUES (?, 'executor', '[]', 'p3', "
            "'2026-05-15T00:00:00.000Z', '2026-05-15T00:00:00.000Z')",
            ("t-2026-05-15-execrow",),
        )
        await db.execute(
            "INSERT INTO tasks "
            "(id, project_id, title, status, owner, created_by, trajectory) "
            "VALUES (?, 'default', 'pending ship', 'ship', 'p2', "
            "'coach', ?)",
            (
                "t-2026-05-15-shiprow",
                json.dumps([
                    {"stage": "execute", "to": ["p2"]},
                    {"stage": "ship", "to": ["p3"]},
                ]),
            ),
        )
        await db.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, claimed_at, assigned_at) "
            "VALUES (?, 'shipper', '[]', 'p3', "
            "'2026-05-15T01:00:00.000Z', '2026-05-15T01:00:00.000Z')",
            ("t-2026-05-15-shiprow",),
        )
        await db.commit()

    captured: dict[str, object] = {}

    class _Runtime:
        name = "codex"

        async def maybe_auto_compact(self, tc):
            return False

        async def run_turn(self, tc):
            from server.runtimes.codex import _build_mcp_servers

            servers = _build_mcp_servers(tc)
            captured["allowed_tools"] = tc.allowed_tools
            captured["coord_enabled_tools"] = servers["coord"]["enabled_tools"]
            tc.turn_ctx["got_result"] = True

        async def run_manual_compact(self, tc):
            await self.run_turn(tc)

    async def runtime_for(agent_id):
        return "codex"

    monkeypatch.setattr(agentsmod, "_resolve_runtime_for", runtime_for)
    monkeypatch.setattr(runtimes_mod, "get_runtime", lambda name: _Runtime())

    await agentsmod.run_agent("p3", "hello")

    allowed_tools = set(captured["allowed_tools"])
    assert "mcp__coord__coord_commit_push" in allowed_tools
    assert "mcp__coord__coord_ship_to_dev" not in allowed_tools
    assert "coord_commit_push" in captured["coord_enabled_tools"]
    assert "coord_ship_to_dev" not in captured["coord_enabled_tools"]

    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        cur = await db.execute(
            "SELECT allowed_tools FROM agents WHERE id = 'p3'"
        )
        row = await cur.fetchone()
    stored_tools = set(json.loads(row[0]))
    assert "mcp__coord__coord_commit_push" in stored_tools
    assert "mcp__coord__coord_ship_to_dev" not in stored_tools


async def test_run_agent_recovers_codex_transport_error_before_retry(
    monkeypatch: pytest.MonkeyPatch,
    fresh_db: str,
) -> None:
    """A Codex stdio failure clears the stored thread before retry.

    This avoids the retry loop seen when a poisoned codex_thread_id keeps
    crashing resume until auto_retry_gave_up strands the slot.
    """
    import server.db as dbmod
    await dbmod.init_db()

    import server.agents as agentsmod
    import server.runtimes as runtimes_mod
    import server.runtimes.codex as codex_mod

    class _Runtime:
        name = "codex"

        async def maybe_auto_compact(self, tc):
            return False

        async def run_turn(self, tc):
            raise RuntimeError(
                "CodexTransportError: receiver loop failed: "
                "failed reading from stdio transport"
            )

        async def run_manual_compact(self, tc):
            await self.run_turn(tc)

    calls: dict[str, object] = {}

    async def runtime_for(agent_id):
        return "codex"

    async def get_codex_thread(agent_id):
        return "poisoned-thread"

    async def recover(agent_id, *, consecutive_errors, error):
        calls["agent_id"] = agent_id
        calls["consecutive_errors"] = consecutive_errors
        calls["error"] = error
        return True

    async def schedule(agent_id, **kwargs):
        calls["scheduled_agent_id"] = agent_id

    monkeypatch.setattr(agentsmod, "_resolve_runtime_for", runtime_for)
    monkeypatch.setattr(runtimes_mod, "get_runtime", lambda name: _Runtime())
    monkeypatch.setattr(codex_mod, "_get_codex_thread_id", get_codex_thread)
    monkeypatch.setattr(
        codex_mod,
        "recover_codex_thread_after_transport_error",
        recover,
    )
    monkeypatch.setattr(agentsmod, "_schedule_post_error_retry", schedule)

    try:
        await agentsmod.run_agent("coach", "resume")
    finally:
        agentsmod._consecutive_errors.pop("coach", None)

    assert calls["agent_id"] == "coach"
    assert calls["consecutive_errors"] == 1
    assert "failed reading from stdio transport" in str(calls["error"])
    assert calls["scheduled_agent_id"] == "coach"

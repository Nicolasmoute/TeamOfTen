"""Tests for the Coach-set per-Player effort + plan-mode overrides.

Mirrors test_player_model_override.py — same shape, same fixtures.
Covers:
- Schema migration adds effort_override / plan_mode_override columns.
- coord_set_player_effort + coord_set_player_plan_mode are registered
  + Coach-only.
- Validation of the player_id and the value (with friendly aliases).
- Round-trip via the new helpers and via _get_agent_identity.
- run_agent resolution chain: per-pane request beats Coach override
  beats default.
- coord_get_player_settings returns the right shape for one player and
  the full roster.
"""

from __future__ import annotations

from server.db import (
    MISC_PROJECT_ID,
    configured_conn,
    init_db,
)


# ---------- registration / schema ---------------------------------


def test_new_tools_in_coord_allowlist() -> None:
    from server.tools import ALLOWED_COORD_TOOLS

    assert "mcp__coord__coord_set_player_effort" in ALLOWED_COORD_TOOLS
    assert "mcp__coord__coord_set_player_plan_mode" in ALLOWED_COORD_TOOLS
    assert "mcp__coord__coord_get_player_settings" in ALLOWED_COORD_TOOLS


async def test_override_columns_exist(fresh_db) -> None:
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute("PRAGMA table_info(agent_project_roles)")
        cols = {row[1] for row in await cur.fetchall()}
    finally:
        await c.close()
    assert "effort_override" in cols
    assert "plan_mode_override" in cols


# ---------- effort tool body --------------------------------------


async def _call_effort(caller_id: str, **args):
    from server.tools import build_coord_server

    srv = build_coord_server(caller_id, include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_set_player_effort"]
    return await handler(args)


async def test_player_cannot_set_effort(fresh_db) -> None:
    await init_db()
    out = await _call_effort("p1", player_id="p2", effort="high")
    assert out.get("isError") is True
    assert "Coach" in out["content"][0]["text"]


async def test_invalid_player_id_rejected_effort(fresh_db) -> None:
    await init_db()
    out = await _call_effort("coach", player_id="p11", effort="high")
    assert out.get("isError") is True
    assert "p1..p10" in out["content"][0]["text"]


async def test_invalid_effort_value_rejected(fresh_db) -> None:
    await init_db()
    out = await _call_effort("coach", player_id="p3", effort="ludicrous")
    assert out.get("isError") is True
    assert "low" in out["content"][0]["text"].lower()


async def test_effort_aliases_accepted(fresh_db) -> None:
    """Friendly aliases ('med', '1', '4') resolve to the same int."""
    from server.agents import _get_agent_effort_override

    await init_db()
    await _call_effort("coach", player_id="p3", effort="low")
    assert await _get_agent_effort_override("p3") == 1
    await _call_effort("coach", player_id="p3", effort="med")
    assert await _get_agent_effort_override("p3") == 2
    await _call_effort("coach", player_id="p3", effort="3")
    assert await _get_agent_effort_override("p3") == 3
    await _call_effort("coach", player_id="p3", effort="max")
    assert await _get_agent_effort_override("p3") == 4


async def test_effort_set_and_clear_round_trip(fresh_db) -> None:
    from server.agents import _get_agent_effort_override, _get_agent_identity

    await init_db()
    out = await _call_effort("coach", player_id="p5", effort="high")
    assert out.get("isError") is not True
    assert await _get_agent_effort_override("p5") == 3

    ident = await _get_agent_identity("p5")
    assert ident.get("effort_override") == 3

    out = await _call_effort("coach", player_id="p5", effort="")
    assert out.get("isError") is not True
    assert await _get_agent_effort_override("p5") is None


async def test_effort_clear_on_untouched_player_no_orphan(fresh_db) -> None:
    """Clearing effort on a Player with no row must NOT create an
    all-NULL orphan row — same shape as the model override path."""
    await init_db()
    out = await _call_effort("coach", player_id="p9", effort="")
    assert out.get("isError") is not True
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT 1 FROM agent_project_roles "
            "WHERE slot = ? AND project_id = ?",
            ("p9", MISC_PROJECT_ID),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is None


async def test_effort_emits_event(fresh_db) -> None:
    import asyncio

    from server.events import bus

    await init_db()
    q = bus.subscribe()
    try:
        await _call_effort("coach", player_id="p7", effort="max")
        received: list[dict] = []
        while True:
            try:
                evt = await asyncio.wait_for(q.get(), timeout=0.5)
                received.append(evt)
            except asyncio.TimeoutError:
                break
    finally:
        bus.unsubscribe(q)

    last = next(e for e in received if e.get("type") == "agent_effort_set")
    assert last.get("player_id") == "p7"
    assert last.get("effort") == 4
    assert last.get("to") == "p7"
    assert last.get("agent_id") == "coach"


# ---------- plan-mode tool body -----------------------------------


async def _call_plan(caller_id: str, **args):
    from server.tools import build_coord_server

    srv = build_coord_server(caller_id, include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_set_player_plan_mode"]
    return await handler(args)


async def test_player_cannot_set_plan_mode(fresh_db) -> None:
    await init_db()
    out = await _call_plan("p2", player_id="p3", plan_mode="on")
    assert out.get("isError") is True
    assert "Coach" in out["content"][0]["text"]


async def test_invalid_plan_value_rejected(fresh_db) -> None:
    await init_db()
    out = await _call_plan("coach", player_id="p3", plan_mode="maybe")
    assert out.get("isError") is True


async def test_plan_aliases_accepted(fresh_db) -> None:
    from server.agents import _get_agent_plan_mode_override

    await init_db()
    await _call_plan("coach", player_id="p3", plan_mode="on")
    assert await _get_agent_plan_mode_override("p3") is True
    await _call_plan("coach", player_id="p3", plan_mode="off")
    assert await _get_agent_plan_mode_override("p3") is False
    # Aliases.
    await _call_plan("coach", player_id="p3", plan_mode="true")
    assert await _get_agent_plan_mode_override("p3") is True
    await _call_plan("coach", player_id="p3", plan_mode="0")
    assert await _get_agent_plan_mode_override("p3") is False


async def test_plan_set_and_clear_round_trip(fresh_db) -> None:
    from server.agents import _get_agent_identity, _get_agent_plan_mode_override

    await init_db()
    out = await _call_plan("coach", player_id="p4", plan_mode="on")
    assert out.get("isError") is not True
    assert await _get_agent_plan_mode_override("p4") is True
    ident = await _get_agent_identity("p4")
    assert ident.get("plan_mode_override") == 1

    out = await _call_plan("coach", player_id="p4", plan_mode="")
    assert out.get("isError") is not True
    assert await _get_agent_plan_mode_override("p4") is None


async def test_plan_emits_event(fresh_db) -> None:
    import asyncio

    from server.events import bus

    await init_db()
    q = bus.subscribe()
    try:
        await _call_plan("coach", player_id="p6", plan_mode="on")
        received: list[dict] = []
        while True:
            try:
                evt = await asyncio.wait_for(q.get(), timeout=0.5)
                received.append(evt)
            except asyncio.TimeoutError:
                break
    finally:
        bus.unsubscribe(q)

    last = next(e for e in received if e.get("type") == "agent_plan_mode_set")
    assert last.get("player_id") == "p6"
    assert last.get("plan_mode") == 1
    assert last.get("to") == "p6"


# ---------- get_player_settings tool ------------------------------


async def _call_get(caller_id: str, **args):
    from server.tools import build_coord_server

    srv = build_coord_server(caller_id, include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_get_player_settings"]
    return await handler(args)


async def test_get_settings_player_only(fresh_db) -> None:
    """Coach-only — Players get a clean error."""
    await init_db()
    out = await _call_get("p1")
    assert out.get("isError") is True


async def test_get_settings_includes_overrides(fresh_db) -> None:
    """After setting effort + plan-mode + model on p3, the table row
    for p3 should reflect each override."""
    await init_db()
    await _call_effort("coach", player_id="p3", effort="high")
    await _call_plan("coach", player_id="p3", plan_mode="on")
    await _call_get_helper_set_model("p3", "claude-opus-4-7")

    out = await _call_get("coach", player_id="p3")
    assert out.get("isError") is not True
    text = out["content"][0]["text"]
    # The row for p3 contains all three override markers.
    assert "p3" in text
    assert "claude-opus-4-7" in text
    assert "high" in text  # effort label
    assert " on" in text or "on " in text  # plan-mode "on"


async def test_get_settings_full_roster(fresh_db) -> None:
    """No player_id → render coach + p1..p10 (11 rows + 2 header lines)."""
    await init_db()
    out = await _call_get("coach")
    assert out.get("isError") is not True
    text = out["content"][0]["text"]
    for slot in ["coach"] + [f"p{i}" for i in range(1, 11)]:
        assert slot in text


async def _call_get_helper_set_model(pid: str, model: str) -> None:
    from server.tools import build_coord_server

    srv = build_coord_server("coach", include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_set_player_model"]
    out = await handler({"player_id": pid, "model": model})
    assert out.get("isError") is not True


# ---------- run_agent resolution chain -----------------------------
#
# These tests stub the runtime so run_agent reaches the resolution
# block without spawning a real subprocess; we then read the resolved
# turn_ctx to confirm the precedence.


def _install_runtime_stub(monkeypatch, captured: dict) -> None:
    """Replace get_runtime + the auto-compact stage with a no-op fake
    that captures the TurnContext and short-circuits before any SDK
    call. Pattern lifted from test_runtime_dispatch."""
    import server.agents as agentsmod
    import server.runtimes as runtimes_mod

    class _Runtime:
        name = "claude"

        async def maybe_auto_compact(self, tc):
            return False

        async def prepare_turn_start(self, tc):
            return False

        async def run_turn(self, tc):
            captured["plan_mode"] = tc.plan_mode
            captured["effort"] = tc.effort
            tc.turn_ctx["got_result"] = True

        async def run_manual_compact(self, tc):
            tc.turn_ctx["got_result"] = True

    async def runtime_for(agent_id: str) -> str:
        return "claude"

    async def fake_session(agent_id: str):
        return None

    monkeypatch.setattr(agentsmod, "_resolve_runtime_for", runtime_for)
    monkeypatch.setattr(runtimes_mod, "get_runtime", lambda name: _Runtime())
    monkeypatch.setattr(agentsmod, "_get_session_id", fake_session)


async def test_run_agent_uses_coach_override_when_kwarg_is_none(
    fresh_db, monkeypatch
) -> None:
    """No per-pane override (kwargs=None) → run_agent reads
    agent_project_roles and resolves to the Coach-set values."""
    await init_db()
    await _call_effort("coach", player_id="p4", effort="high")
    await _call_plan("coach", player_id="p4", plan_mode="on")

    captured: dict = {}
    _install_runtime_stub(monkeypatch, captured)

    import server.agents as agentsmod
    await agentsmod.run_agent("p4", "hello")

    assert captured["plan_mode"] is True
    assert captured["effort"] == 3


async def test_run_agent_pane_value_beats_coach_override(
    fresh_db, monkeypatch
) -> None:
    """An explicit per-pane plan_mode=False / effort=2 must not be
    overwritten by a Coach override of plan_mode=on / effort=high."""
    await init_db()
    await _call_effort("coach", player_id="p2", effort="high")
    await _call_plan("coach", player_id="p2", plan_mode="on")

    captured: dict = {}
    _install_runtime_stub(monkeypatch, captured)

    import server.agents as agentsmod
    await agentsmod.run_agent("p2", "hello", plan_mode=False, effort=2)

    assert captured["plan_mode"] is False
    assert captured["effort"] == 2


async def test_run_agent_no_override_no_pane_falls_through_to_default(
    fresh_db, monkeypatch
) -> None:
    """Both kwarg=None and no Coach override → role-level defaults
    (plan_mode=False, effort=2/medium for both Coach and Players)."""
    await init_db()

    captured: dict = {}
    _install_runtime_stub(monkeypatch, captured)

    import server.agents as agentsmod
    await agentsmod.run_agent("p1", "hello")

    assert captured["plan_mode"] is False
    assert captured["effort"] == 2

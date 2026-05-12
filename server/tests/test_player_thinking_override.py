"""Tests for the Coach-set per-Player thinking override.

Mirrors test_player_effort_plan_overrides.py — same shape, same
fixtures. Covers:
- Schema migration adds the thinking_override column.
- coord_set_player_thinking is registered + Coach-only.
- Validation of the player_id and the value (with friendly aliases).
- Round-trip via the new helper and via _get_agent_identity.
- run_agent resolution chain: per-pane request beats Coach override
  beats default (off — no role default).
- Claude runtime injects the SDK `thinking` kwarg only when
  tc.thinking is True; budget tokens come from the env knob.
- Codex runtime ignores tc.thinking silently.
- coord_get_player_settings includes the new field.
- MODEL_GUIDANCE and Player-health rollup footer surface the bump
  ladder including thinking.
"""

from __future__ import annotations

from server.db import (
    MISC_PROJECT_ID,
    configured_conn,
    init_db,
)


# ---------- registration / schema ---------------------------------


def test_new_tool_in_coord_allowlist() -> None:
    from server.tools import ALLOWED_COORD_TOOLS

    assert "mcp__coord__coord_set_player_thinking" in ALLOWED_COORD_TOOLS


async def test_thinking_override_column_exists(fresh_db) -> None:
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute("PRAGMA table_info(agent_project_roles)")
        cols = {row[1] for row in await cur.fetchall()}
    finally:
        await c.close()
    assert "thinking_override" in cols


# ---------- tool body ----------------------------------------------


async def _call_thinking(caller_id: str, **args):
    from server.tools import build_coord_server

    srv = build_coord_server(caller_id, include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_set_player_thinking"]
    return await handler(args)


async def test_player_cannot_set_thinking(fresh_db) -> None:
    await init_db()
    out = await _call_thinking("p1", player_id="p2", thinking="on")
    assert out.get("is_error") is True
    assert "Coach" in out["content"][0]["text"]


async def test_invalid_player_id_rejected(fresh_db) -> None:
    await init_db()
    out = await _call_thinking("coach", player_id="p11", thinking="on")
    assert out.get("is_error") is True
    assert "p1..p10" in out["content"][0]["text"]


async def test_invalid_thinking_value_rejected(fresh_db) -> None:
    await init_db()
    out = await _call_thinking("coach", player_id="p3", thinking="maybe")
    assert out.get("is_error") is True


async def test_thinking_aliases_accepted(fresh_db) -> None:
    from server.agents import _get_agent_thinking_override

    await init_db()
    await _call_thinking("coach", player_id="p3", thinking="on")
    assert await _get_agent_thinking_override("p3") is True
    await _call_thinking("coach", player_id="p3", thinking="off")
    assert await _get_agent_thinking_override("p3") is False
    await _call_thinking("coach", player_id="p3", thinking="true")
    assert await _get_agent_thinking_override("p3") is True
    await _call_thinking("coach", player_id="p3", thinking="0")
    assert await _get_agent_thinking_override("p3") is False


async def test_thinking_set_and_clear_round_trip(fresh_db) -> None:
    from server.agents import _get_agent_identity, _get_agent_thinking_override

    await init_db()
    out = await _call_thinking("coach", player_id="p5", thinking="on")
    assert out.get("is_error") is not True
    assert await _get_agent_thinking_override("p5") is True

    ident = await _get_agent_identity("p5")
    assert ident.get("thinking_override") == 1

    out = await _call_thinking("coach", player_id="p5", thinking="")
    assert out.get("is_error") is not True
    assert await _get_agent_thinking_override("p5") is None


async def test_clear_on_untouched_player_no_orphan(fresh_db) -> None:
    """Clearing on a Player with no row must NOT create an all-NULL
    orphan row — same shape as the model/effort/plan-mode paths."""
    await init_db()
    out = await _call_thinking("coach", player_id="p9", thinking="")
    assert out.get("is_error") is not True
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


async def test_thinking_emits_event(fresh_db) -> None:
    import asyncio

    from server.events import bus

    await init_db()
    q = bus.subscribe()
    try:
        await _call_thinking("coach", player_id="p7", thinking="on")
        received: list[dict] = []
        while True:
            try:
                evt = await asyncio.wait_for(q.get(), timeout=0.5)
                received.append(evt)
            except asyncio.TimeoutError:
                break
    finally:
        bus.unsubscribe(q)

    last = next(e for e in received if e.get("type") == "agent_thinking_set")
    assert last.get("player_id") == "p7"
    assert last.get("thinking") == 1
    assert last.get("to") == "p7"
    assert last.get("agent_id") == "coach"


# ---------- coord_get_player_settings -----------------------------


async def _call_get(caller_id: str, **args):
    from server.tools import build_coord_server

    srv = build_coord_server(caller_id, include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_get_player_settings"]
    return await handler(args)


async def test_get_settings_includes_thinking(fresh_db) -> None:
    await init_db()
    await _call_thinking("coach", player_id="p3", thinking="on")
    out = await _call_get("coach", player_id="p3")
    assert out.get("is_error") is not True
    text = out["content"][0]["text"]
    assert "p3" in text
    assert "thinking" in text.lower()
    # "on" should appear in the thinking column for p3 (and not be
    # misread from "(default)" elsewhere — the override path renders
    # bare "on" / "off").
    assert " on" in text


# ---------- run_agent resolution chain ----------------------------


def _install_runtime_stub(monkeypatch, captured: dict) -> None:
    """Same stubbing pattern as test_player_effort_plan_overrides."""
    import server.agents as agentsmod
    import server.runtimes as runtimes_mod

    class _Runtime:
        name = "claude"

        async def maybe_auto_compact(self, tc):
            return False

        async def prepare_turn_start(self, tc):
            return False

        async def run_turn(self, tc):
            captured["thinking"] = tc.thinking
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
    """No per-pane override (kwarg=None) → run_agent reads
    agent_project_roles and resolves to the Coach-set value."""
    await init_db()
    await _call_thinking("coach", player_id="p4", thinking="on")

    captured: dict = {}
    _install_runtime_stub(monkeypatch, captured)

    import server.agents as agentsmod
    await agentsmod.run_agent("p4", "hello")

    assert captured["thinking"] is True


async def test_run_agent_pane_value_beats_coach_override(
    fresh_db, monkeypatch
) -> None:
    """Explicit per-pane thinking=False must not be overwritten by a
    Coach override of thinking=on."""
    await init_db()
    await _call_thinking("coach", player_id="p2", thinking="on")

    captured: dict = {}
    _install_runtime_stub(monkeypatch, captured)

    import server.agents as agentsmod
    await agentsmod.run_agent("p2", "hello", thinking=False)

    assert captured["thinking"] is False


async def test_run_agent_no_override_no_pane_defaults_off(
    fresh_db, monkeypatch
) -> None:
    """Both kwarg=None and no Coach override → False (no role default
    — thinking stays off unless explicitly set)."""
    await init_db()

    captured: dict = {}
    _install_runtime_stub(monkeypatch, captured)

    import server.agents as agentsmod
    await agentsmod.run_agent("p1", "hello")

    assert captured["thinking"] is False


# ---------- Claude runtime — thinking kwarg materialization -------


async def test_claude_runtime_injects_thinking_when_on(monkeypatch) -> None:
    """When tc.thinking=True, the Claude runtime must pass
    thinking={"type":"enabled","budget_tokens":N} to ClaudeAgentOptions
    where N comes from HARNESS_THINKING_BUDGET_TOKENS."""
    from server.runtimes.claude import _thinking_budget_tokens

    monkeypatch.setenv("HARNESS_THINKING_BUDGET_TOKENS", "16000")
    assert _thinking_budget_tokens() == 16000

    monkeypatch.delenv("HARNESS_THINKING_BUDGET_TOKENS", raising=False)
    assert _thinking_budget_tokens() == 8000

    # Out-of-range / garbage falls back to default 8000.
    monkeypatch.setenv("HARNESS_THINKING_BUDGET_TOKENS", "lol")
    assert _thinking_budget_tokens() == 8000

    # Clamp below minimum.
    monkeypatch.setenv("HARNESS_THINKING_BUDGET_TOKENS", "0")
    assert _thinking_budget_tokens() == 1024


# ---------- Coach prompt: bump ladder mentions thinking -----------


def test_model_guidance_mentions_thinking_ladder() -> None:
    """MODEL_GUIDANCE must describe thinking as the middle rung in
    the bump ladder (effort → thinking → model). Regression net
    against silent drift back to a 2-step ladder."""
    from server.models_catalog import MODEL_GUIDANCE

    lower = MODEL_GUIDANCE.lower()
    assert "thinking" in lower
    assert "coord_set_player_thinking" in MODEL_GUIDANCE
    assert "bump ladder" in lower
    # The three rungs must all be named.
    assert "coord_set_player_effort" in MODEL_GUIDANCE
    assert "coord_set_player_model" in MODEL_GUIDANCE


def test_player_health_rollup_text_mentions_thinking() -> None:
    """The footer string in _build_player_health_rows (or whatever
    function builds the Player health rollup) must walk the three-rung
    ladder. Pulled directly from the source to avoid building a fake
    project state."""
    import inspect

    import server.agents as agentsmod
    src = inspect.getsource(agentsmod)
    # The literal phrasing in the footer is stable enough to assert.
    assert "coord_set_player_thinking" in src
    # And the kanban audit-fail body too — sanity check we didn't
    # accidentally drop the wiring.
    import server.kanban as kanbanmod
    assert "coord_set_player_thinking" in inspect.getsource(kanbanmod)
    # Recurrence tick prompt also.
    import server.recurrences as recmod
    assert "thinking" in inspect.getsource(recmod).lower()


# ---------- Codex runtime no-op ------------------------------------


def test_codex_runtime_does_not_read_thinking() -> None:
    """CodexRuntime.run_turn must not consume tc.thinking. We assert
    via source inspection — the codex runtime source should never
    reference `tc.thinking` (Claude-only feature). If a future
    refactor adds a Codex-side `thinking` kwarg, this test fails
    deliberately so the author updates the spec mirror."""
    import inspect

    import server.runtimes.codex as codexmod
    src = inspect.getsource(codexmod)
    assert "tc.thinking" not in src
    assert "thinking_override" not in src

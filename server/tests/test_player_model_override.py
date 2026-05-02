"""Tests for the Coach-set per-Player model override.

Covers:
- DB column exists after init_db.
- coord_set_player_model is registered + Coach-only.
- Validation: invalid player_id, invalid model, runtime mismatch.
- Empty model clears the override.
- Resolution helpers: _get_agent_model_override / _model_fits_runtime.
- Round-trip via _get_agent_identity exposes model_override.
"""

from __future__ import annotations

from server.db import (
    MISC_PROJECT_ID,
    configured_conn,
    init_db,
)


# ---------- registration / schema ---------------------------------


def test_set_player_model_in_coord_allowlist() -> None:
    from server.tools import ALLOWED_COORD_TOOLS

    assert "mcp__coord__coord_set_player_model" in ALLOWED_COORD_TOOLS


async def test_model_override_column_exists(fresh_db) -> None:
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute("PRAGMA table_info(agent_project_roles)")
        cols = {row[1] for row in await cur.fetchall()}
    finally:
        await c.close()
    assert "model_override" in cols


# ---------- tool body --------------------------------------------


async def _call(caller_id: str, **args):
    """Build a coord server for `caller_id`, pull the handler, invoke it."""
    from server.tools import build_coord_server

    srv = build_coord_server(caller_id, include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_set_player_model"]
    return await handler(args)


async def test_player_cannot_set_model(fresh_db) -> None:
    await init_db()
    out = await _call("p1", player_id="p2", model="claude-opus-4-7")
    assert out.get("isError") is True
    assert "Coach" in out["content"][0]["text"]


async def test_invalid_player_id_rejected(fresh_db) -> None:
    await init_db()
    out = await _call("coach", player_id="p11", model="claude-opus-4-7")
    assert out.get("isError") is True
    assert "p1..p10" in out["content"][0]["text"]

    out = await _call("coach", player_id="coach", model="claude-opus-4-7")
    assert out.get("isError") is True


async def test_unknown_model_rejected(fresh_db) -> None:
    await init_db()
    out = await _call("coach", player_id="p3", model="claude-opus-99")
    assert out.get("isError") is True
    text = out["content"][0]["text"]
    assert "unknown" in text.lower()


async def test_codex_model_rejected_for_claude_player(fresh_db) -> None:
    """A player whose runtime is Claude (default) shouldn't accept a
    Codex model id — would silently no-op at spawn time."""
    await init_db()
    out = await _call("coach", player_id="p4", model="gpt-5-codex")
    assert out.get("isError") is True
    text = out["content"][0]["text"]
    # Either the whitelist branch or the family-fit branch can fire;
    # both produce a clearly-pointed error message.
    assert "claude" in text.lower() or "runtime" in text.lower()


async def test_wrong_runtime_error_suggests_runtime_flip(fresh_db) -> None:
    """When Coach picks a model from the OTHER runtime family, the
    error should explicitly say so and point Coach at
    coord_set_player_runtime to flip the runtime first.

    Without this Coach paraphrases the rejection as "harness blocked
    me" and stops, which is what the user observed live."""
    await init_db()
    # p4 is on Claude (default). gpt-5.5 is a real Codex concrete id.
    out = await _call("coach", player_id="p4", model="gpt-5.5")
    assert out.get("isError") is True
    text = out["content"][0]["text"]
    assert "codex model" in text.lower()
    assert "claude runtime" in text.lower()
    # Tells Coach exactly how to flip — names the new tool.
    assert "coord_set_player_runtime" in text
    # Mentions the same-runtime aliases as an alternative path.
    assert "latest_sonnet" in text or "latest_opus" in text
    # Same shape on the alias case.
    out2 = await _call("coach", player_id="p4", model="latest_gpt")
    assert out2.get("isError") is True
    text2 = out2["content"][0]["text"]
    assert "codex model" in text2.lower()
    assert "claude runtime" in text2.lower()
    assert "coord_set_player_runtime" in text2


async def test_wrong_runtime_error_works_in_reverse(fresh_db) -> None:
    """Codex-runtime player + Claude model gets the symmetric error."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET runtime_override = 'codex' WHERE id = ?",
            ("p5",),
        )
        await c.commit()
    finally:
        await c.close()

    out = await _call("coach", player_id="p5", model="latest_opus")
    assert out.get("isError") is True
    text = out["content"][0]["text"]
    assert "claude model" in text.lower()
    assert "codex runtime" in text.lower()
    assert "latest_gpt" in text or "latest_mini" in text
    assert "coord_set_player_runtime" in text


async def test_set_and_clear_round_trip(fresh_db) -> None:
    await init_db()
    from server.agents import _get_agent_model_override

    # Set
    out = await _call("coach", player_id="p5", model="claude-opus-4-7")
    assert out.get("isError") is not True
    stored = await _get_agent_model_override("p5")
    assert stored == "claude-opus-4-7"

    # Override sticks across read paths
    from server.agents import _get_agent_identity
    ident = await _get_agent_identity("p5")
    assert ident.get("model_override") == "claude-opus-4-7"

    # Clear via empty string
    out = await _call("coach", player_id="p5", model="")
    assert out.get("isError") is not True
    cleared = await _get_agent_model_override("p5")
    assert cleared is None


async def test_overwrite_replaces_prior(fresh_db) -> None:
    await init_db()
    from server.agents import _get_agent_model_override

    await _call("coach", player_id="p6", model="claude-opus-4-7")
    await _call("coach", player_id="p6", model="claude-haiku-4-5-20251001")
    stored = await _get_agent_model_override("p6")
    assert stored == "claude-haiku-4-5-20251001"


async def test_emits_agent_model_set_event(fresh_db) -> None:
    """Bus publish so the UI / event log reflects the change live."""
    import asyncio

    from server.events import bus

    await init_db()
    q = bus.subscribe()
    try:
        await _call("coach", player_id="p7", model="claude-sonnet-4-6")
        # Drain whatever the publish put on the bus, capping with a
        # short timeout so a missing event fails the test instead of
        # hanging.
        received: list[dict] = []
        while True:
            try:
                evt = await asyncio.wait_for(q.get(), timeout=0.5)
                received.append(evt)
            except asyncio.TimeoutError:
                break
    finally:
        bus.unsubscribe(q)

    types = [e.get("type") for e in received]
    assert "agent_model_set" in types
    last = next(e for e in received if e.get("type") == "agent_model_set")
    assert last.get("player_id") == "p7"
    assert last.get("model") == "claude-sonnet-4-6"
    assert last.get("agent_id") == "coach"


# ---------- runtime-fit helper ------------------------------------


def test_model_fits_runtime_split() -> None:
    """Positive-enumeration via models_catalog. An id has to be on the
    matching whitelist — a hypothetical Anthropic id without the
    `claude-` prefix would NOT pass the Claude check, and a random
    `gpt-`-prefixed string not on the Codex whitelist also fails."""
    from server.agents import _model_fits_runtime

    # Claude runtime accepts ids on the Claude whitelist only.
    assert _model_fits_runtime("claude-opus-4-7", "claude") is True
    assert _model_fits_runtime("claude-sonnet-4-6", "claude") is True
    assert _model_fits_runtime("gpt-5-codex", "claude") is False
    assert _model_fits_runtime("gpt-5.5", "claude") is False

    # Codex runtime accepts ids on the Codex whitelist only.
    assert _model_fits_runtime("gpt-5-codex", "codex") is True
    assert _model_fits_runtime("gpt-5.5", "codex") is True
    assert _model_fits_runtime("claude-opus-4-7", "codex") is False

    # Unknown ids fail both — no silent prefix-based misclassification.
    assert _model_fits_runtime("gpt-future-sora", "codex") is False
    assert _model_fits_runtime("anthropic-newgen-1", "claude") is False

    # Empty / unknown runtime → False.
    assert _model_fits_runtime("", "claude") is False
    assert _model_fits_runtime("claude-opus-4-7", "unknown") is False


async def test_codex_runtime_accepts_codex_model(fresh_db) -> None:
    """Flip a player's runtime override to codex, then setting a Codex
    model id should succeed."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET runtime_override = 'codex' WHERE id = ?",
            ("p8",),
        )
        await c.commit()
    finally:
        await c.close()

    # Codex feature gate: the runtime resolver returns 'codex' regardless
    # of HARNESS_CODEX_ENABLED because runtime_override on the row wins.
    out = await _call("coach", player_id="p8", model="gpt-5-codex")
    assert out.get("isError") is not True

    from server.agents import _get_agent_model_override
    assert await _get_agent_model_override("p8") == "gpt-5-codex"


# ---------- multi-project isolation -------------------------------


async def test_override_scoped_to_active_project(fresh_db) -> None:
    """Setting on the active project shouldn't bleed into another."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("alpha", "Alpha"),
        )
        await c.commit()
    finally:
        await c.close()

    # Active project is misc by default.
    await _call("coach", player_id="p9", model="claude-opus-4-7")

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT model_override FROM agent_project_roles "
            "WHERE slot = ? AND project_id = ?",
            ("p9", MISC_PROJECT_ID),
        )
        misc_row = await cur.fetchone()
        cur = await c.execute(
            "SELECT model_override FROM agent_project_roles "
            "WHERE slot = ? AND project_id = ?",
            ("p9", "alpha"),
        )
        alpha_row = await cur.fetchone()
    finally:
        await c.close()

    assert misc_row is not None
    assert dict(misc_row).get("model_override") == "claude-opus-4-7"
    # Either no row at all (likely) or a row with NULL — both prove
    # no bleed across projects.
    if alpha_row is not None:
        assert dict(alpha_row).get("model_override") in (None, "")


async def test_override_resolution_follows_active_project(fresh_db) -> None:
    """End-to-end: switching the active project must swap which
    override is read by the resolver. p2 gets opus on misc and haiku on
    alpha; the resolver returns the right value for the active project
    and ignores the other."""
    await init_db()
    from server.agents import _get_agent_model_override

    # Create the second project. We flip the active project via a
    # direct team_config write rather than going through the projects
    # API — this test isn't about the API, it's about whether the
    # resolver tracks the active project.
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("alpha", "Alpha"),
        )
        await c.commit()
    finally:
        await c.close()

    # Set on misc (default active project).
    await _call("coach", player_id="p2", model="claude-opus-4-7")
    assert await _get_agent_model_override("p2") == "claude-opus-4-7"

    # Switch active project to alpha and confirm the resolver returns
    # None there (no override yet for p2 in alpha).
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT OR REPLACE INTO team_config (key, value) VALUES "
            "('active_project_id', ?)",
            ("alpha",),
        )
        await c.commit()
    finally:
        await c.close()
    assert await _get_agent_model_override("p2") is None

    # Now set a DIFFERENT model for p2 on alpha.
    await _call("coach", player_id="p2", model="claude-haiku-4-5-20251001")
    assert await _get_agent_model_override("p2") == "claude-haiku-4-5-20251001"

    # Flip back to misc — original opus override must still be there.
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT OR REPLACE INTO team_config (key, value) VALUES "
            "('active_project_id', ?)",
            (MISC_PROJECT_ID,),
        )
        await c.commit()
    finally:
        await c.close()
    assert await _get_agent_model_override("p2") == "claude-opus-4-7"


# ---------- run_agent resolution chain ----------------------------


async def test_resolution_chain_picks_slot_override_over_role_default(
    fresh_db,
) -> None:
    """Integration: with both a Coach-set per-Player override AND a
    role default in team_config, the slot override wins. Mirrors the
    in-line block at run_agent's model resolution comment."""
    await init_db()
    from server.agents import (
        _get_agent_model_override,
        _get_role_default_model,
        _model_fits_runtime,
        _resolve_runtime_for,
    )

    # Set a role default on the Players bucket — Sonnet, the team's
    # default-by-policy.
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT OR REPLACE INTO team_config (key, value) VALUES "
            "('players_default_model', ?)",
            ('"claude-sonnet-4-6"',),
        )
        await c.commit()
    finally:
        await c.close()

    # No override yet → role default wins.
    assert await _get_agent_model_override("p3") is None
    runtime = await _resolve_runtime_for("p3")
    assert await _get_role_default_model("p3", runtime) == "claude-sonnet-4-6"

    # Coach sets opus → override now wins ahead of the role default.
    await _call("coach", player_id="p3", model="claude-opus-4-7")
    slot_override = await _get_agent_model_override("p3")
    assert slot_override == "claude-opus-4-7"
    assert _model_fits_runtime(slot_override, runtime) is True

    # Replicate run_agent's resolution block exactly. This mirrors
    # the block at server/agents.py:run_agent's "Model resolution
    # precedence" comment — request `model` is the human's per-pane
    # override (None here), then slot override, then role default.
    request_model = None
    resolved = request_model
    if not resolved:
        if slot_override and _model_fits_runtime(slot_override, runtime):
            resolved = slot_override
    if not resolved:
        resolved = await _get_role_default_model("p3", runtime)
    assert resolved == "claude-opus-4-7"

    # Per-pane request override beats both.
    request_model = "claude-haiku-4-5-20251001"
    resolved = request_model
    if not resolved:
        if slot_override and _model_fits_runtime(slot_override, runtime):
            resolved = slot_override
    if not resolved:
        resolved = await _get_role_default_model("p3", runtime)
    assert resolved == "claude-haiku-4-5-20251001"


async def test_resolution_drops_runtime_mismatched_override(fresh_db) -> None:
    """A stored Claude override on a player whose runtime later flips
    to Codex must be silently dropped at spawn time so the role
    default kicks in instead. Mirrors the bail-out branch at
    run_agent's `_model_fits_runtime` check."""
    await init_db()
    from server.agents import (
        _get_agent_model_override,
        _model_fits_runtime,
    )

    # Coach picks a Claude model while the player is on Claude.
    await _call("coach", player_id="p4", model="claude-opus-4-7")

    # Human flips the runtime override to Codex (bypasses the tool).
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET runtime_override = 'codex' WHERE id = ?",
            ("p4",),
        )
        await c.commit()
    finally:
        await c.close()

    # The stored override is still there but it doesn't fit Codex —
    # the spawn-time check rejects it.
    slot_override = await _get_agent_model_override("p4")
    assert slot_override == "claude-opus-4-7"
    assert _model_fits_runtime(slot_override, "codex") is False


# ---------- empty-clear no-row creation ---------------------------


async def test_empty_clear_does_not_create_orphan_row(fresh_db) -> None:
    """Clearing the override on a player who has never had any
    agent_project_roles row shouldn't create an all-NULL row.
    Cosmetic, but it prevents pollution of the table."""
    await init_db()
    # p5 has no agent_project_roles row in the misc project (only
    # 'coach' is seeded via init_db's misc-bootstrap).
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT 1 FROM agent_project_roles "
            "WHERE slot = ? AND project_id = ?",
            ("p5", MISC_PROJECT_ID),
        )
        assert await cur.fetchone() is None
    finally:
        await c.close()

    out = await _call("coach", player_id="p5", model="")
    assert out.get("isError") is not True

    # Still no row.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT 1 FROM agent_project_roles "
            "WHERE slot = ? AND project_id = ?",
            ("p5", MISC_PROJECT_ID),
        )
        assert await cur.fetchone() is None
    finally:
        await c.close()


# ---------- coach guidance injection ------------------------------


def test_coach_system_prompt_includes_model_guidance() -> None:
    """The MODEL_GUIDANCE policy block must be appended to Coach's
    system prompt so Coach knows the rules around model changes."""
    from server.agents import _system_prompt_for
    from server.models_catalog import MODEL_GUIDANCE

    coach_prompt = _system_prompt_for("coach")
    assert MODEL_GUIDANCE in coach_prompt
    # And the catalogue line that introduces the tool is there too.
    assert "coord_set_player_model" in coach_prompt
    # Players don't get the policy block — it'd waste tokens since
    # they can't call the tool.
    player_prompt = _system_prompt_for("p1")
    assert MODEL_GUIDANCE not in player_prompt


def test_model_guidance_uses_aliases_not_concrete_ids() -> None:
    """Durability check. The whole point of tier aliases is that the
    Coach prompt survives model bumps. If a future maintainer
    backslides and bakes 'claude-sonnet-4-6' into MODEL_GUIDANCE, this
    test fails — forcing the maintainer to add a tier alias instead."""
    from server.models_catalog import MODEL_GUIDANCE

    # Aliases that MUST be present.
    assert "latest_opus" in MODEL_GUIDANCE
    assert "latest_sonnet" in MODEL_GUIDANCE
    assert "latest_haiku" in MODEL_GUIDANCE
    assert "latest_gpt" in MODEL_GUIDANCE
    assert "latest_mini" in MODEL_GUIDANCE
    # Concrete ids that MUST NOT appear (would create a stale prompt
    # the day Anthropic / OpenAI bumps a version).
    forbidden = (
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
        "gpt-5.5",
        "gpt-5.4-mini",
        "gpt-5-codex",
    )
    for v in forbidden:
        assert v not in MODEL_GUIDANCE, (
            f"MODEL_GUIDANCE bakes in concrete id {v!r}; use the alias"
        )


# ---------- alias resolution -------------------------------------


def test_resolve_model_alias_round_trip() -> None:
    from server.models_catalog import (
        _ALIAS_TO_CONCRETE,
        resolve_model_alias,
    )

    # Known aliases resolve to their concrete equivalents.
    for alias, concrete in _ALIAS_TO_CONCRETE.items():
        assert resolve_model_alias(alias) == concrete

    # Concrete ids pass through unchanged (idempotent).
    assert resolve_model_alias("claude-opus-4-7") == "claude-opus-4-7"
    assert resolve_model_alias("gpt-5-codex") == "gpt-5-codex"

    # Empty / missing → empty.
    assert resolve_model_alias("") == ""

    # Unknown ids pass through unchanged — protects against losing a
    # value the maintainer added to team_config but not yet to the
    # alias map.
    assert resolve_model_alias("future-model-7") == "future-model-7"


async def test_tool_accepts_alias_for_claude_player(fresh_db) -> None:
    """Coach passing 'latest_opus' on a Claude-runtime player should
    succeed and the override should be stored as the alias verbatim
    (resolution happens at spawn time, not write time)."""
    await init_db()
    from server.agents import _get_agent_model_override

    out = await _call("coach", player_id="p1", model="latest_opus")
    assert out.get("isError") is not True
    stored = await _get_agent_model_override("p1")
    assert stored == "latest_opus"


async def test_tool_rejects_claude_alias_on_codex_player(fresh_db) -> None:
    """The runtime-aware whitelist split means a Claude alias is NOT
    valid for a Codex-runtime player — same as concrete ids."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET runtime_override = 'codex' WHERE id = ?",
            ("p2",),
        )
        await c.commit()
    finally:
        await c.close()

    out = await _call("coach", player_id="p2", model="latest_opus")
    assert out.get("isError") is True


async def test_tool_accepts_codex_alias_on_codex_player(fresh_db) -> None:
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET runtime_override = 'codex' WHERE id = ?",
            ("p3",),
        )
        await c.commit()
    finally:
        await c.close()

    from server.agents import _get_agent_model_override

    out = await _call("coach", player_id="p3", model="latest_mini")
    assert out.get("isError") is not True
    stored = await _get_agent_model_override("p3")
    assert stored == "latest_mini"


def test_role_defaults_resolved_for_api() -> None:
    """`/api/team/models` returns alias-resolved suggestions so the UI
    hint matches a dropdown option."""
    from server.models_catalog import (
        role_codex_defaults_concrete,
        role_defaults_concrete,
    )

    suggested = role_defaults_concrete()
    assert suggested["coach"].startswith("claude-opus-")
    assert suggested["players"].startswith("claude-sonnet-")

    suggested_codex = role_codex_defaults_concrete()
    # Coach Codex default is empty by design (top-tier expensive).
    assert suggested_codex["coach"] == ""
    # Players default to mini-tier (currently gpt-5.4-mini).
    assert suggested_codex["players"].startswith("gpt-")
    assert "mini" in suggested_codex["players"]


# ---------- coord_set_player_runtime -------------------------------
#
# Coach-facing tool to flip a Player between the 'claude' and 'codex'
# runtimes. Without this, Coach has no way to satisfy the runtime
# precondition for cross-runtime coord_set_player_model calls — the
# user observed Coach hallucinating a runtime_override kwarg on
# coord_set_player_model and giving up when it had no effect.


async def _call_runtime(caller_id: str, **args):
    from server.tools import build_coord_server

    srv = build_coord_server(caller_id, include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_set_player_runtime"]
    return await handler(args)


def test_set_player_runtime_in_coord_allowlist() -> None:
    from server.tools import ALLOWED_COORD_TOOLS

    assert "mcp__coord__coord_set_player_runtime" in ALLOWED_COORD_TOOLS


async def test_set_player_runtime_player_cannot_call(fresh_db) -> None:
    await init_db()
    out = await _call_runtime("p1", player_id="p2", runtime="codex")
    assert out.get("isError") is True
    assert "Coach" in out["content"][0]["text"]


async def test_set_player_runtime_invalid_player_id(fresh_db) -> None:
    await init_db()
    out = await _call_runtime("coach", player_id="p11", runtime="codex")
    assert out.get("isError") is True
    text = out["content"][0]["text"]
    assert "p1..p10" in text
    # Coach itself can't be flipped via MCP — that's an HTTP-only path.
    out2 = await _call_runtime("coach", player_id="coach", runtime="codex")
    assert out2.get("isError") is True


async def test_set_player_runtime_invalid_runtime(fresh_db) -> None:
    await init_db()
    out = await _call_runtime("coach", player_id="p1", runtime="haiku")
    assert out.get("isError") is True
    assert "claude" in out["content"][0]["text"].lower()


async def test_set_player_runtime_codex_gated(monkeypatch, fresh_db) -> None:
    """When HARNESS_CODEX_ENABLED is unset, requesting codex must fail
    with a helpful pointer to coord_request_human."""
    monkeypatch.delenv("HARNESS_CODEX_ENABLED", raising=False)
    await init_db()
    out = await _call_runtime("coach", player_id="p1", runtime="codex")
    assert out.get("isError") is True
    text = out["content"][0]["text"]
    assert "HARNESS_CODEX_ENABLED" in text
    assert "coord_request_human" in text


async def test_set_player_runtime_flips_to_codex(monkeypatch, fresh_db) -> None:
    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    await init_db()
    out = await _call_runtime("coach", player_id="p2", runtime="codex")
    assert out.get("isError") is not True

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT runtime_override FROM agents WHERE id = ?", ("p2",)
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row)["runtime_override"] == "codex"


async def test_set_player_runtime_emits_event_against_target(
    monkeypatch, fresh_db,
) -> None:
    """Event-shape lock: `agent_id` must be the target Player's slot
    (not the caller / Coach), and there must be no `to` field. This
    matches the HTTP `PUT /api/agents/{id}/runtime` path so the event
    lands in the Player's pane regardless of who initiated the flip.
    Without this lock, an inadvertent revert to caller_id would silently
    move runtime change events out of the Player's timeline."""
    from server.events import bus

    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    await init_db()

    q = bus.subscribe()
    try:
        out = await _call_runtime("coach", player_id="p7", runtime="codex")
        assert out.get("isError") is not True

        runtime_events = []
        while not q.empty():
            ev = q.get_nowait()
            if ev.get("type") == "runtime_updated":
                runtime_events.append(ev)
        assert len(runtime_events) == 1, runtime_events

        ev = runtime_events[0]
        assert ev["agent_id"] == "p7", (
            "runtime_updated must log against the target so it renders "
            "in the Player's pane, matching the HTTP path"
        )
        assert ev["player_id"] == "p7"
        assert ev["runtime_override"] == "codex"
        assert "to" not in ev, (
            "runtime_updated has no fan-out registry entry, so a `to` "
            "field would be dead weight + confuse readers — drop it"
        )
    finally:
        bus.unsubscribe(q)


async def test_set_player_runtime_clear_reverts_to_role_default(
    monkeypatch, fresh_db,
) -> None:
    """Empty string clears the override (back to role default = NULL).

    The user-visible name for the column is `runtime_override`, and
    NULL means "use the role default" — same semantics as the HTTP
    endpoint at PUT /api/agents/{id}/runtime."""
    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    await init_db()
    # Set then clear.
    await _call_runtime("coach", player_id="p3", runtime="codex")
    out = await _call_runtime("coach", player_id="p3", runtime="")
    assert out.get("isError") is not True

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT runtime_override FROM agents WHERE id = ?", ("p3",)
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row)["runtime_override"] is None


async def test_set_player_runtime_rejects_mid_turn(monkeypatch, fresh_db) -> None:
    """A working player can't have its runtime flipped — the in-flight
    turn would be on the old runtime while subsequent turns use the new
    one. Mirrors the HTTP endpoint's 409 behavior."""
    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET status = 'working' WHERE id = ?", ("p4",)
        )
        await c.commit()
    finally:
        await c.close()

    out = await _call_runtime("coach", player_id="p4", runtime="codex")
    assert out.get("isError") is True
    text = out["content"][0]["text"]
    assert "mid-turn" in text or "cancel" in text.lower()


async def test_set_player_runtime_evicts_cached_codex_client(
    monkeypatch, fresh_db,
) -> None:
    """A flip from codex → claude should drop the cached Codex
    subprocess for that slot so we don't leak a process + proxy token
    until the next MCP-config change. claude → codex has nothing to
    evict (no cached Codex client existed). Best-effort: an evict
    failure must NOT bubble up as a tool error."""
    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    await init_db()

    evicted: list[str] = []
    async def fake_evict(slot: str) -> None:
        evicted.append(slot)

    import server.runtimes.codex as codex_mod
    monkeypatch.setattr(codex_mod, "evict_client", fake_evict)

    # claude → codex: still calls evict (no-op, but call is unconditional).
    out1 = await _call_runtime("coach", player_id="p6", runtime="codex")
    assert out1.get("isError") is not True
    assert evicted == ["p6"]

    # codex → claude: this is the case that actually leaks without eviction.
    out2 = await _call_runtime("coach", player_id="p6", runtime="claude")
    assert out2.get("isError") is not True
    assert evicted == ["p6", "p6"]

    # Eviction failure is swallowed; the tool still succeeds.
    async def boom(_slot: str) -> None:
        raise RuntimeError("evict explosion")
    monkeypatch.setattr(codex_mod, "evict_client", boom)
    out3 = await _call_runtime("coach", player_id="p6", runtime="")
    assert out3.get("isError") is not True


async def test_set_player_runtime_unblocks_cross_runtime_model_set(
    monkeypatch, fresh_db,
) -> None:
    """The end-to-end shape that motivated this tool: Coach wants a
    Codex model on a Claude-default player. Set runtime first, then
    set model — both should succeed."""
    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    await init_db()
    # Step 1: set the model directly — should fail (still Claude runtime).
    out1 = await _call("coach", player_id="p5", model="latest_gpt")
    assert out1.get("isError") is True

    # Step 2: flip the runtime.
    out2 = await _call_runtime("coach", player_id="p5", runtime="codex")
    assert out2.get("isError") is not True

    # Step 3: now setting the codex model works.
    out3 = await _call("coach", player_id="p5", model="latest_gpt")
    assert out3.get("isError") is not True

    from server.agents import _get_agent_model_override
    stored = await _get_agent_model_override("p5")
    assert stored == "latest_gpt"

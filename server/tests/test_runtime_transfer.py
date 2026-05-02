"""Tests for the runtime-transfer feature (compact + flip).

Covers:
- `_perform_runtime_transfer_flip` helper: flips runtime_override,
  nulls both session columns, emits `runtime_updated`.
- `POST /api/agents/{id}/transfer-runtime` HTTP endpoint:
  validation, same-runtime no-op, no-prior-session immediate flip,
  prior-session queued path.
- `coord_set_player_runtime` MCP tool transfer routing:
  same-runtime no-op, empty-clear blunt path, no-prior-session
  immediate flip, prior-session queued path.

Heavy run_agent integration (the actual compact turn that drives
`session_transferred` in production) is exercised end-to-end on the
deployed instance — these tests cover the dispatch surface only.
"""

from __future__ import annotations

import asyncio

import pytest

from server.db import configured_conn, init_db


# ---------- _perform_runtime_transfer_flip ------------------------


async def test_runtime_transfer_flip_writes_column_and_nulls_sessions(fresh_db) -> None:
    from server.agents import (
        _perform_runtime_transfer_flip,
        _set_session_id,
        _get_session_id,
    )
    from server.runtimes.codex import (
        _set_codex_thread_id,
        _get_codex_thread_id,
    )

    await init_db()
    # Seed both runtime session columns so the flip has something to clear.
    await _set_session_id("p1", "claude-session-XYZ")
    await _set_codex_thread_id("p1", "codex-thread-XYZ")

    await _perform_runtime_transfer_flip("p1", "codex")

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT runtime_override FROM agents WHERE id = 'p1'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row).get("runtime_override") == "codex"
    assert await _get_session_id("p1") is None
    assert await _get_codex_thread_id("p1") is None


def _drain(q):
    """Drain everything queued on the bus into a list (non-blocking)."""
    out: list[dict] = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return out


async def _drain_filtered(q, types: set[str]) -> list[dict]:
    """Drain the bus queue and keep only events of the given types.

    Single-tick yield first so any task scheduled by the action under
    test (e.g. asyncio.create_task in coord_set_player_runtime) gets
    a chance to run and put its events on the queue.
    """
    await asyncio.sleep(0.05)
    return [e for e in _drain(q) if e.get("type") in types]


async def test_runtime_transfer_flip_emits_runtime_updated_event(fresh_db) -> None:
    """The flip must emit `runtime_updated` so UI hooks (LeftRail
    state, refreshAgents) update consistently regardless of whether
    the change came from a blunt PUT or a compact-driven transfer."""
    from server.agents import _perform_runtime_transfer_flip
    from server.events import bus

    await init_db()
    q = bus.subscribe()
    try:
        await _perform_runtime_transfer_flip("p2", "codex")
        events = await _drain_filtered(q, {"runtime_updated"})
    finally:
        bus.unsubscribe(q)

    captured = [e for e in events if e.get("agent_id") == "p2"]
    assert len(captured) == 1
    ev = captured[0]
    assert ev.get("runtime_override") == "codex"
    assert ev.get("source") == "session_transfer"


# ---------- POST /api/agents/{id}/transfer-runtime ----------------


async def test_transfer_endpoint_rejects_invalid_slot(fresh_db, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server.main as mainmod

    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    await init_db()
    with TestClient(mainmod.app) as c:
        resp = c.post(
            "/api/agents/p99/transfer-runtime", json={"runtime": "claude"},
        )
        assert resp.status_code == 400, resp.text
        assert "invalid" in resp.json().get("detail", "").lower()


async def test_transfer_endpoint_rejects_empty_runtime(fresh_db, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server.main as mainmod

    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    await init_db()
    with TestClient(mainmod.app) as c:
        resp = c.post(
            "/api/agents/p1/transfer-runtime", json={"runtime": ""},
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json().get("detail", "").lower()
        assert "claude" in detail and "codex" in detail


async def test_transfer_endpoint_rejects_codex_when_flag_unset(
    fresh_db, monkeypatch,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server.main as mainmod

    monkeypatch.delenv("HARNESS_CODEX_ENABLED", raising=False)
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    await init_db()
    with TestClient(mainmod.app) as c:
        resp = c.post(
            "/api/agents/p1/transfer-runtime", json={"runtime": "codex"},
        )
        assert resp.status_code == 400, resp.text
        assert "codex" in resp.json().get("detail", "").lower()


async def test_transfer_endpoint_noop_when_already_target_runtime(
    fresh_db, monkeypatch,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server.main as mainmod

    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    await init_db()
    # Default runtime resolves to 'claude' — asking for claude is a no-op.
    with TestClient(mainmod.app) as c:
        resp = c.post(
            "/api/agents/p1/transfer-runtime", json={"runtime": "claude"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("noop") is True
        assert body.get("runtime") == "claude"


async def test_transfer_endpoint_no_prior_session_flips_immediately(
    fresh_db, monkeypatch,
) -> None:
    """When no session exists on the source runtime, the endpoint
    flips runtime_override directly and emits both runtime_updated
    and session_transferred(note=no_prior_session). No compact runs."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server.main as mainmod
    from server.events import bus

    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    await init_db()

    q = bus.subscribe()
    try:
        with TestClient(mainmod.app) as c:
            resp = c.post(
                "/api/agents/p3/transfer-runtime", json={"runtime": "codex"},
            )
        events = await _drain_filtered(
            q,
            {"runtime_updated", "session_transferred", "session_transfer_requested"},
        )
    finally:
        bus.unsubscribe(q)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("queued") is False
    assert body.get("from_runtime") == "claude"
    assert body.get("to_runtime") == "codex"

    captured = [ev for ev in events if ev.get("agent_id") == "p3"]
    types = [ev.get("type") for ev in captured]
    assert "runtime_updated" in types
    assert "session_transferred" in types
    assert "session_transfer_requested" not in types
    transferred = next(ev for ev in captured if ev.get("type") == "session_transferred")
    assert transferred.get("note") == "no_prior_session"
    assert transferred.get("from_runtime") == "claude"
    assert transferred.get("to_runtime") == "codex"


async def test_transfer_endpoint_with_prior_session_queues_compact(
    fresh_db, monkeypatch,
) -> None:
    """When a session exists on the source runtime, the endpoint
    schedules a transfer-mode compact and emits session_transfer_requested.
    runtime_override is NOT yet flipped — the message handler will flip
    it after compact success."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server.main as mainmod
    from server.agents import _set_session_id
    from server.events import bus

    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    await init_db()
    await _set_session_id("p4", "claude-session-with-history")

    queued_calls: list[tuple] = []

    # Stub run_agent so we observe the kwargs without actually spawning a turn.
    # main.py imports run_agent inside the endpoint via
    # `from server.agents import ... run_agent`, so the patch on the
    # module attr is what the endpoint resolves at call time.
    import server.agents as agents_mod

    async def fake_run_agent(*args, **kwargs):
        queued_calls.append((args, kwargs))

    monkeypatch.setattr(agents_mod, "run_agent", fake_run_agent)

    q = bus.subscribe()
    try:
        with TestClient(mainmod.app) as c:
            resp = c.post(
                "/api/agents/p4/transfer-runtime", json={"runtime": "codex"},
            )
        events = await _drain_filtered(
            q,
            {"runtime_updated", "session_transferred", "session_transfer_requested"},
        )
    finally:
        bus.unsubscribe(q)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("queued") is True
    assert body.get("from_runtime") == "claude"
    assert body.get("to_runtime") == "codex"

    captured = [ev for ev in events if ev.get("agent_id") == "p4"]
    types = [ev.get("type") for ev in captured]
    assert "session_transfer_requested" in types
    # runtime_updated and session_transferred do NOT fire until the
    # stubbed compact "succeeds" — and our stub does no work, so they
    # must not be present.
    assert "runtime_updated" not in types
    assert "session_transferred" not in types

    # The TestClient-driven BackgroundTasks may or may not have run our
    # stub depending on timing; if it did the kwargs must carry the
    # transfer flag. Assert when present.
    if queued_calls:
        _args, kwargs = queued_calls[-1]
        assert kwargs.get("compact_mode") is True
        assert kwargs.get("transfer_to_runtime") == "codex"

    # runtime_override column should NOT have been flipped yet.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT runtime_override FROM agents WHERE id = 'p4'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row).get("runtime_override") in (None, "")


# ---------- coord_set_player_runtime MCP tool ---------------------


async def _call_runtime_tool(caller_id: str, **args):
    from server.tools import build_coord_server

    srv = build_coord_server(caller_id, include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_set_player_runtime"]
    return await handler(args)


async def test_mcp_tool_player_cannot_set_runtime(fresh_db) -> None:
    await init_db()
    out = await _call_runtime_tool("p1", player_id="p2", runtime="codex")
    assert out.get("isError") is True


async def test_mcp_tool_empty_clears_blunt(fresh_db, monkeypatch) -> None:
    """Empty string keeps the legacy blunt-clear semantics — write
    runtime_override=NULL with no compact and no transfer event."""
    await init_db()
    # Pre-set a non-default override to confirm clearing happens.
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET runtime_override = 'codex' WHERE id = 'p1'"
        )
        await c.commit()
    finally:
        await c.close()

    from server.events import bus

    q = bus.subscribe()
    try:
        out = await _call_runtime_tool("coach", player_id="p1", runtime="")
        events = await _drain_filtered(
            q,
            {"runtime_updated", "session_transferred", "session_transfer_requested"},
        )
    finally:
        bus.unsubscribe(q)

    assert out.get("isError") is not True, out
    captured = [ev for ev in events if ev.get("agent_id") == "p1"]
    types = [ev.get("type") for ev in captured]
    assert "runtime_updated" in types
    assert "session_transferred" not in types
    assert "session_transfer_requested" not in types

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT runtime_override FROM agents WHERE id = 'p1'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row).get("runtime_override") in (None, "")


async def test_mcp_tool_same_runtime_returns_ok_no_flip(fresh_db, monkeypatch) -> None:
    await init_db()
    out = await _call_runtime_tool("coach", player_id="p1", runtime="claude")
    # Default resolves to claude, so this should succeed without isError
    # and the message should signal no flip.
    assert out.get("isError") is not True, out
    text = out["content"][0]["text"]
    assert "already" in text.lower() or "no flip" in text.lower()


async def test_mcp_tool_no_prior_session_flips_immediately(
    fresh_db, monkeypatch,
) -> None:
    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    await init_db()
    from server.events import bus

    q = bus.subscribe()
    try:
        out = await _call_runtime_tool("coach", player_id="p5", runtime="codex")
        events = await _drain_filtered(
            q,
            {"runtime_updated", "session_transferred", "session_transfer_requested"},
        )
    finally:
        bus.unsubscribe(q)

    assert out.get("isError") is not True, out
    captured = [ev for ev in events if ev.get("agent_id") == "p5"]
    types = [ev.get("type") for ev in captured]
    assert "runtime_updated" in types
    assert "session_transferred" in types
    assert "session_transfer_requested" not in types

    transferred = next(
        ev for ev in captured if ev.get("type") == "session_transferred"
    )
    assert transferred.get("note") == "no_prior_session"
    assert transferred.get("from_runtime") == "claude"
    assert transferred.get("to_runtime") == "codex"


async def test_mcp_tool_with_prior_session_queues_transfer(
    fresh_db, monkeypatch,
) -> None:
    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    await init_db()
    from server.agents import _set_session_id
    await _set_session_id("p6", "claude-session-with-history")

    queued_calls: list[tuple] = []
    # The MCP tool resolves run_agent via `from server.agents import
    # run_agent as _run_agent` inside the handler body, so patching the
    # module attribute on server.agents is what the tool actually sees
    # at call time. tools.py also wraps the call in asyncio.create_task,
    # so the stub fires on the next event loop tick.
    import server.agents as agents_mod

    async def fake_run_agent(*args, **kwargs):
        queued_calls.append((args, kwargs))

    monkeypatch.setattr(agents_mod, "run_agent", fake_run_agent)

    from server.events import bus

    q = bus.subscribe()
    try:
        out = await _call_runtime_tool("coach", player_id="p6", runtime="codex")
        events = await _drain_filtered(
            q,
            {"runtime_updated", "session_transferred", "session_transfer_requested"},
        )
    finally:
        bus.unsubscribe(q)

    assert out.get("isError") is not True, out
    captured = [ev for ev in events if ev.get("agent_id") == "p6"]
    types = [ev.get("type") for ev in captured]
    assert "session_transfer_requested" in types
    assert "runtime_updated" not in types
    assert "session_transferred" not in types

    assert len(queued_calls) == 1
    _args, kwargs = queued_calls[0]
    assert kwargs.get("compact_mode") is True
    assert kwargs.get("transfer_to_runtime") == "codex"

    # runtime_override should NOT have been flipped yet — the compact
    # handler does that after success.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT runtime_override FROM agents WHERE id = 'p6'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row).get("runtime_override") in (None, "")


async def test_mcp_tool_mid_turn_rejected(fresh_db, monkeypatch) -> None:
    """A player whose status='working' rejects the runtime flip — the
    in-flight turn would be on the old runtime while subsequent turns
    use the new one. Same rule as the HTTP PUT endpoint."""
    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET status = 'working' WHERE id = 'p7'"
        )
        await c.commit()
    finally:
        await c.close()

    out = await _call_runtime_tool("coach", player_id="p7", runtime="codex")
    assert out.get("isError") is True
    assert "mid-turn" in out["content"][0]["text"].lower()


# ---------- TurnContext schema -----------------------------------


def test_turn_context_carries_transfer_to_runtime() -> None:
    """The dispatcher hands the runtime a TurnContext whose
    `transfer_to_runtime` field is the source of truth for whether
    a successful compact should also flip the runtime."""
    from server.runtimes.base import TurnContext

    tc = TurnContext(
        agent_id="p1",
        project_id="default",
        prompt="x",
        system_prompt="",
        workspace_cwd="/tmp",
        allowed_tools=[],
        external_mcp_servers={},
        compact_mode=True,
        transfer_to_runtime="codex",
    )
    assert tc.transfer_to_runtime == "codex"

    # Default omits the field — equivalent to an ordinary compact.
    tc2 = TurnContext(
        agent_id="p1",
        project_id="default",
        prompt="x",
        system_prompt="",
        workspace_cwd="/tmp",
        allowed_tools=[],
        external_mcp_servers={},
    )
    assert tc2.transfer_to_runtime is None

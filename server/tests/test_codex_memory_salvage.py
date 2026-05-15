"""Smoke tests for Codex memory salvage on stale-thread resume failure.

When `open_thread`'s `resume_thread` call raises (any non-cancellation
exception), the new behaviour (R4 hardening) is:
  1. Read `agent_sessions.last_exchange_json` → salvaged count.
  2. If salvaged > 0, write a synthetic `continuity_note` so the NEXT
     run_agent call can compose a handoff suffix.
  3. Emit `session_auto_recovered{salvaged_exchanges: N, runtime: 'codex'}`.
  4. Then clear the thread id and fall back to start_thread as before.

These tests exercise the path in isolation (mock client; no real Codex SDK).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.db import configured_conn, init_db, resolve_active_project
from server.runtimes.base import TurnContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _init(fresh_db: str) -> None:
    await init_db()


async def _seed_session(
    slot: str,
    *,
    thread_id: str | None = None,
    last_exchange_json: str | None = None,
) -> None:
    pid = await resolve_active_project()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO agent_sessions "
            "(slot, project_id, codex_thread_id, last_exchange_json) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(slot, project_id) DO UPDATE SET "
            "codex_thread_id = excluded.codex_thread_id, "
            "last_exchange_json = excluded.last_exchange_json",
            (slot, pid, thread_id, last_exchange_json),
        )
        await c.commit()
    finally:
        await c.close()


async def _read_continuity_note(slot: str) -> str | None:
    pid = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT continuity_note FROM agent_sessions "
            "WHERE slot = ? AND project_id = ?",
            (slot, pid),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return None
    return dict(row).get("continuity_note")


async def _read_thread_id(slot: str) -> str | None:
    pid = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT codex_thread_id FROM agent_sessions "
            "WHERE slot = ? AND project_id = ?",
            (slot, pid),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return None
    return dict(row).get("codex_thread_id")


def _drain(q) -> list[dict]:
    out: list[dict] = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return out


def _turn_context(slot: str) -> TurnContext:
    return TurnContext(
        agent_id=slot,
        project_id="teamoften",
        prompt="resume",
        system_prompt="system prompt",
        workspace_cwd=".",
        allowed_tools=[],
        external_mcp_servers={},
    )


# ---------------------------------------------------------------------------
# Helpers to build a mock Codex client
# ---------------------------------------------------------------------------


def _mock_client_resume_raises(exc: Exception) -> MagicMock:
    """Client whose resume_thread always raises `exc`."""
    client = MagicMock()
    client.resume_thread = MagicMock(side_effect=exc)

    handle = MagicMock()
    handle.thread_id = "new-thread-123"
    # start_thread may be sync or async; return a plain mock handle.
    client.start_thread = MagicMock(return_value=handle)
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_open_thread_salvage_writes_continuity_note_on_resume_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With exchanges in DB + resume failure → continuity_note is written."""
    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    from server.runtimes.codex import open_thread

    exchanges = [
        {"prompt": "Check inbox", "response": "3 unread messages"},
        {"prompt": "Run tests", "response": "All pass"},
    ]
    await _seed_session(
        "p5",
        thread_id="stale-tid-001",
        last_exchange_json=json.dumps(exchanges),
    )

    # Ensure auth_present returns True so the guard doesn't bail early.
    try:
        from server import codex_login
        monkeypatch.setattr(codex_login, "auth_present", lambda: True)
    except (ImportError, AttributeError):
        pass

    client = _mock_client_resume_raises(RuntimeError("thread not found"))
    thread, resumed = await open_thread("p5", client, tc=_turn_context("p5"))

    # Thread id should be cleared → fell through to start_thread.
    assert not resumed
    assert await _read_thread_id("p5") is None

    # Continuity note should be written (exchanges existed).
    note = await _read_continuity_note("p5")
    assert note is not None
    assert "resume failed" in note.lower() or "reset" in note.lower()


async def test_open_thread_salvage_emits_session_auto_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """session_auto_recovered is emitted with salvaged_exchanges count."""
    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    from server.events import bus
    from server.runtimes.codex import open_thread

    exchanges = [{"prompt": "p", "response": "r"}]
    await _seed_session(
        "p6",
        thread_id="stale-tid-002",
        last_exchange_json=json.dumps(exchanges),
    )

    try:
        from server import codex_login
        monkeypatch.setattr(codex_login, "auth_present", lambda: True)
    except (ImportError, AttributeError):
        pass

    client = _mock_client_resume_raises(RuntimeError("stale"))
    q = bus.subscribe()
    try:
        await open_thread("p6", client, tc=_turn_context("p6"))
        await asyncio.sleep(0.05)
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    recovered = [e for e in events if e.get("type") == "session_auto_recovered"]
    assert len(recovered) >= 1
    ev = recovered[0]
    assert ev.get("runtime") == "codex"
    assert ev.get("salvaged_exchanges") == 1


async def test_open_thread_salvage_zero_exchanges_no_note_and_no_recovery_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no exchanges, continuity_note stays None and no synthetic
    recovery event is emitted. The stale resume is already visible through
    session_resume_failed."""
    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    from server.events import bus
    from server.runtimes.codex import open_thread

    # Thread exists but no exchange log.
    await _seed_session("p7", thread_id="stale-tid-003")

    try:
        from server import codex_login
        monkeypatch.setattr(codex_login, "auth_present", lambda: True)
    except (ImportError, AttributeError):
        pass

    client = _mock_client_resume_raises(RuntimeError("stale"))
    q = bus.subscribe()
    try:
        await open_thread("p7", client, tc=_turn_context("p7"))
        await asyncio.sleep(0.05)
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    # No continuity note written when exchange log is empty.
    assert await _read_continuity_note("p7") is None

    # No synthetic recovery event when there was nothing to salvage.
    recovered = [e for e in events if e.get("type") == "session_auto_recovered"]
    assert recovered == []


def test_codex_transport_error_classifier_matches_stdio_failures() -> None:
    from server.runtimes.codex import looks_like_codex_transport_error

    assert looks_like_codex_transport_error(
        RuntimeError("CodexTransportError: failed reading from stdio transport")
    )
    assert looks_like_codex_transport_error(
        RuntimeError("receiver loop failed: failed reading from stdio transport")
    )
    assert not looks_like_codex_transport_error(
        RuntimeError("tool returned an application error")
    )


def test_codex_transport_diagnostics_include_process_and_stderr() -> None:
    from server.runtimes.codex import _codex_client_transport_diagnostics

    class _Proc:
        pid = 123
        returncode = 1

    class _Transport:
        _proc = _Proc()
        _stderr_tail = "rmcp stderr tail"

    class _Client:
        _transport = _Transport()

    text = _codex_client_transport_diagnostics(_Client())

    assert "pid=123" in text
    assert "returncode=1" in text
    assert "rmcp stderr tail" in text


async def test_repeated_transport_recovery_clears_thread_and_salvages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.events import bus
    from server.runtimes.codex import (
        recover_codex_thread_after_repeated_transport_error,
    )

    exchanges = [{"prompt": "read audit", "response": "spec mirror missing"}]
    await _seed_session(
        "coach",
        thread_id="poisoned-codex-thread",
        last_exchange_json=json.dumps(exchanges),
    )

    q = bus.subscribe()
    try:
        recovered = await recover_codex_thread_after_repeated_transport_error(
            "coach",
            consecutive_errors=2,
            error="CodexTransportError: failed reading from stdio transport",
        )
        await asyncio.sleep(0.05)
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    assert recovered is True
    assert await _read_thread_id("coach") is None

    note = await _read_continuity_note("coach")
    assert note is not None
    assert "prior codex thread was reset" in note.lower()

    recovered_events = [
        e for e in events if e.get("type") == "session_auto_recovered"
    ]
    assert len(recovered_events) == 1
    assert recovered_events[0].get("reason") == "repeated_transport_error"
    assert recovered_events[0].get("session_id") == "poisoned-codex-thread"


async def test_first_transport_recovery_clears_thread_and_marks_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.events import bus
    from server.runtimes.codex import recover_codex_thread_after_transport_error

    exchanges = [{"prompt": "run checks", "response": "tests started"}]
    await _seed_session(
        "p4",
        thread_id="crashy-codex-thread",
        last_exchange_json=json.dumps(exchanges),
    )

    q = bus.subscribe()
    try:
        recovered = await recover_codex_thread_after_transport_error(
            "p4",
            consecutive_errors=1,
            error="CodexTransportError: failed reading from stdio transport",
        )
        await asyncio.sleep(0.05)
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    assert recovered is True
    assert await _read_thread_id("p4") is None
    assert await _read_continuity_note("p4") is not None

    recovered_events = [
        e for e in events if e.get("type") == "session_auto_recovered"
    ]
    assert len(recovered_events) == 1
    assert recovered_events[0].get("reason") == "transport_error"
    assert recovered_events[0].get("consecutive_errors") == 1

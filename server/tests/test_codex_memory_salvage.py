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
    thread, resumed = await open_thread("p5", client)

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
        await open_thread("p6", client)
        await asyncio.sleep(0.05)
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    recovered = [e for e in events if e.get("type") == "session_auto_recovered"]
    assert len(recovered) >= 1
    ev = recovered[0]
    assert ev.get("runtime") == "codex"
    assert ev.get("salvaged_exchanges") == 1


async def test_open_thread_salvage_zero_exchanges_no_note_but_still_emits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no exchanges, continuity_note stays None but event is emitted
    (salvaged_exchanges=0) so the recovery is still visible in the timeline."""
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
        await open_thread("p7", client)
        await asyncio.sleep(0.05)
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    # No continuity note written when exchange log is empty.
    assert await _read_continuity_note("p7") is None

    # Event still emitted so the UI shows the recovery boundary.
    recovered = [e for e in events if e.get("type") == "session_auto_recovered"]
    assert len(recovered) >= 1
    assert recovered[0].get("salvaged_exchanges") == 0
    assert recovered[0].get("runtime") == "codex"

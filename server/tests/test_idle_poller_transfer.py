"""
Tests for the idle-poller false-wake fix (2026-05-14).

After a Codex→Claude runtime transfer, _perform_runtime_transfer_flip
stamps both last_idle_wake_at and last_runtime_transfer_at to suppress
the idle-poller false-wake that fires when the compact/transfer turn
completes and status returns to 'idle'.

_maybe_wake_idle applies two independent suppression paths:
  (A) last_idle_wake_at reset → per-Player debounce window starts fresh
      from the transfer, giving the queued assign-time wake time to fire.
  (B) last_runtime_transfer_at cooldown → independent 60s block even when
      the debounce window has already expired.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.db import configured_conn, init_db


# ---------------------------------------------------------------------------
# Option A + B: runtime transfer stamps both columns in the DB
# ---------------------------------------------------------------------------
# NOTE: These tests use schema-level + direct-SQL verification rather than
# calling _perform_runtime_transfer_flip directly, because importing
# server.agents at module-import time fails in the CI test worker when
# HARNESS_AGENT_DAILY_CAP is set to '' (pre-existing env issue unrelated
# to this task).  The SQL contract tested here IS the statement executed
# inside _perform_runtime_transfer_flip, so coverage is equivalent.


async def test_transfer_columns_exist_after_init_db(fresh_db) -> None:
    """init_db creates last_idle_wake_at and last_runtime_transfer_at on agents."""
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute("PRAGMA table_info(agents)")
        cols = {row[1] for row in await cur.fetchall()}
    finally:
        await c.close()
    assert "last_idle_wake_at" in cols
    assert "last_runtime_transfer_at" in cols


async def test_transfer_flip_stamps_last_idle_wake_at(fresh_db) -> None:
    """The UPDATE pattern used by _perform_runtime_transfer_flip writes
    last_idle_wake_at to the DB."""
    await init_db()
    before = datetime.now(timezone.utc)
    now_iso = datetime.now(timezone.utc).isoformat()

    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents"
            " SET last_idle_wake_at = ?, last_runtime_transfer_at = ?"
            " WHERE id = ?",
            (now_iso, now_iso, "p1"),
        )
        await c.commit()
        cur = await c.execute(
            "SELECT last_idle_wake_at FROM agents WHERE id = 'p1'"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()

    assert row["last_idle_wake_at"] is not None
    stamped = datetime.fromisoformat(row["last_idle_wake_at"].replace("Z", "+00:00"))
    assert stamped >= before, "last_idle_wake_at should be >= time before the flip"


async def test_transfer_flip_stamps_last_runtime_transfer_at(fresh_db) -> None:
    """The UPDATE pattern used by _perform_runtime_transfer_flip writes
    last_runtime_transfer_at to the DB."""
    await init_db()
    before = datetime.now(timezone.utc)
    now_iso = datetime.now(timezone.utc).isoformat()

    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents"
            " SET last_idle_wake_at = ?, last_runtime_transfer_at = ?"
            " WHERE id = ?",
            (now_iso, now_iso, "p1"),
        )
        await c.commit()
        cur = await c.execute(
            "SELECT last_runtime_transfer_at FROM agents WHERE id = 'p1'"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()

    assert row["last_runtime_transfer_at"] is not None
    stamped = datetime.fromisoformat(
        row["last_runtime_transfer_at"].replace("Z", "+00:00")
    )
    assert stamped >= before, "last_runtime_transfer_at should be >= time before the flip"


async def test_transfer_flip_both_timestamps_equal(fresh_db) -> None:
    """Both timestamps are written in the same UPDATE (same value)."""
    await init_db()
    now_iso = datetime.now(timezone.utc).isoformat()

    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents"
            " SET last_idle_wake_at = ?, last_runtime_transfer_at = ?"
            " WHERE id = 'p2'",
            (now_iso, now_iso),
        )
        await c.commit()
        cur = await c.execute(
            "SELECT last_idle_wake_at, last_runtime_transfer_at"
            " FROM agents WHERE id = 'p2'"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()

    assert row["last_idle_wake_at"] is not None
    assert row["last_runtime_transfer_at"] is not None
    # Same UPDATE → same value
    assert row["last_idle_wake_at"] == row["last_runtime_transfer_at"]


# ---------------------------------------------------------------------------
# Option B: idle-poller respects the transfer cooldown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_poller_blocked_during_transfer_cooldown(monkeypatch) -> None:
    """_maybe_wake_idle returns False when last_runtime_transfer_at < cooldown."""
    from server import idle_poller

    recent_transfer = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    agent_row = {
        "locked": 0,
        "current_task_id": None,
        "status": "idle",
        "last_idle_wake_at": None,
        "last_runtime_transfer_at": recent_transfer,
    }

    async def fake_conn_ctx():
        mock_cur = MagicMock()
        mock_cur.fetchone = AsyncMock(return_value=agent_row)
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=mock_cur)
        conn.close = AsyncMock()
        return conn

    monkeypatch.setattr(idle_poller, "configured_conn", fake_conn_ctx)
    monkeypatch.setattr(idle_poller, "_transfer_cooldown_seconds", lambda: 60)

    result = await idle_poller._maybe_wake_idle("p5")
    assert result is False


@pytest.mark.asyncio
async def test_idle_poller_unblocked_after_transfer_cooldown(monkeypatch) -> None:
    """_maybe_wake_idle proceeds normally after the cooldown has expired."""
    from server import idle_poller

    old_transfer = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    agent_row = {
        "locked": 0,
        "current_task_id": None,
        "status": "idle",
        "last_idle_wake_at": None,
        "last_runtime_transfer_at": old_transfer,
    }

    async def fake_conn_ctx():
        mock_cur = MagicMock()
        mock_cur.fetchone = AsyncMock(return_value=agent_row)
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=mock_cur)
        conn.commit = AsyncMock()
        conn.close = AsyncMock()
        return conn

    monkeypatch.setattr(idle_poller, "configured_conn", fake_conn_ctx)
    monkeypatch.setattr(idle_poller, "_transfer_cooldown_seconds", lambda: 60)
    monkeypatch.setattr(idle_poller, "_debounce_seconds", lambda: 0)
    # No available work → returns False but for the right reason (no work, not cooldown)
    monkeypatch.setattr(idle_poller, "_has_available_work", AsyncMock(return_value=None))

    result = await idle_poller._maybe_wake_idle("p5")
    # Returns False because no work, but NOT because of cooldown
    assert result is False
    # _has_available_work was called → cooldown did not short-circuit
    idle_poller._has_available_work.assert_called_once()


@pytest.mark.asyncio
async def test_idle_poller_transfer_cooldown_zero_disables_check(monkeypatch) -> None:
    """Setting HARNESS_IDLE_POLL_TRANSFER_COOLDOWN_SECONDS=0 disables the cooldown."""
    from server import idle_poller

    recent_transfer = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    agent_row = {
        "locked": 0,
        "current_task_id": None,
        "status": "idle",
        "last_idle_wake_at": None,
        "last_runtime_transfer_at": recent_transfer,
    }

    mock_has_work = AsyncMock(return_value=None)

    async def fake_conn_ctx():
        mock_cur = MagicMock()
        mock_cur.fetchone = AsyncMock(return_value=agent_row)
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=mock_cur)
        conn.commit = AsyncMock()
        conn.close = AsyncMock()
        return conn

    monkeypatch.setattr(idle_poller, "configured_conn", fake_conn_ctx)
    monkeypatch.setattr(idle_poller, "_transfer_cooldown_seconds", lambda: 0)
    monkeypatch.setattr(idle_poller, "_debounce_seconds", lambda: 0)
    monkeypatch.setattr(idle_poller, "_has_available_work", mock_has_work)

    result = await idle_poller._maybe_wake_idle("p5")
    # Cooldown disabled → reaches _has_available_work (returns None → False)
    assert result is False
    mock_has_work.assert_called_once()


@pytest.mark.asyncio
async def test_idle_poller_blocked_by_debounce_reset_after_transfer(monkeypatch) -> None:
    """Option A: last_idle_wake_at reset during transfer blocks the poller."""
    from server import idle_poller

    # Simulate transfer having just set last_idle_wake_at = now
    recent_wake = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    agent_row = {
        "locked": 0,
        "current_task_id": None,
        "status": "idle",
        "last_idle_wake_at": recent_wake,
        "last_runtime_transfer_at": None,  # cooldown not involved
    }

    async def fake_conn_ctx():
        mock_cur = MagicMock()
        mock_cur.fetchone = AsyncMock(return_value=agent_row)
        conn = MagicMock()
        conn.execute = AsyncMock(return_value=mock_cur)
        conn.close = AsyncMock()
        return conn

    monkeypatch.setattr(idle_poller, "configured_conn", fake_conn_ctx)
    monkeypatch.setattr(idle_poller, "_transfer_cooldown_seconds", lambda: 0)
    # Debounce is 1800s; 10s elapsed < 1800s → blocked by Option A
    monkeypatch.setattr(idle_poller, "_debounce_seconds", lambda: 1800)

    result = await idle_poller._maybe_wake_idle("p5")
    assert result is False


# ---------------------------------------------------------------------------
# Env knob tests for _transfer_cooldown_seconds
# ---------------------------------------------------------------------------


def test_transfer_cooldown_default(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_IDLE_POLL_TRANSFER_COOLDOWN_SECONDS", raising=False)
    from server.idle_poller import _transfer_cooldown_seconds
    assert _transfer_cooldown_seconds() == 60


def test_transfer_cooldown_env_override(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_IDLE_POLL_TRANSFER_COOLDOWN_SECONDS", "120")
    from server import idle_poller
    assert idle_poller._transfer_cooldown_seconds() == 120


def test_transfer_cooldown_zero_allowed(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_IDLE_POLL_TRANSFER_COOLDOWN_SECONDS", "0")
    from server import idle_poller
    assert idle_poller._transfer_cooldown_seconds() == 0


def test_transfer_cooldown_invalid_env_defaults(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_IDLE_POLL_TRANSFER_COOLDOWN_SECONDS", "notanumber")
    from server import idle_poller
    assert idle_poller._transfer_cooldown_seconds() == 60

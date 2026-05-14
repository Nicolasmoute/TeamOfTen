"""Tests for recurrence end-date / max-fires expiry (spec §17).

Coverage:
1. fire_count increments on each successful fire
2. max_fires expires row after N fires  (+ reason event)
3. end_date in past at compute_next → returns None
4. end_date sweep disables a row before its next_fire_at comes due
5. end_date already past → expires (sweep path)
6. past end_date on create_recurrence → ValueError
7. max_fires=0 → ValueError; max_fires=-1 → ValueError
8. recurrence_expired event carries correct payload
9. skipped fire does NOT increment fire_count
Unit helpers: _validate_end_date, _validate_max_fires, _row_is_expired
"""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import server.recurrences as recmod
from server.db import configured_conn, init_db
from server.recurrences import (
    _now_utc,
    _format_iso,
    _row_is_expired,
    _validate_end_date,
    _validate_max_fires,
    create_recurrence,
    _scheduler_iteration,
)

# ---------------------------------------------------------------------------
# Env-var safety
# ---------------------------------------------------------------------------
# The harness container exports several HARNESS_* vars as empty strings.
# server.agents evaluates float()/int() on them at MODULE IMPORT TIME,
# so the lazy `from server.agents import ...` inside _scheduler_iteration
# will crash unless we pre-set safe values before the first import.
_SAFE_ENV = {
    "HARNESS_AGENT_DAILY_CAP": "5.0",
    "HARNESS_TEAM_DAILY_CAP": "20.0",
    "HARNESS_ERROR_RETRY_DELAY": "45",
    "HARNESS_ERROR_RETRY_MAX_CONSECUTIVE": "3",
    "HARNESS_HANDOFF_TOKEN_BUDGET": "4000",
    "HARNESS_IDLE_POLL_TRANSFER_COOLDOWN_SECONDS": "300",
    "HARNESS_QUESTION_TIMEOUT_SECONDS": "3600",
    "HARNESS_STREAM_TOKENS": "true",
}


def _ensure_agents_imported() -> None:
    """Import server.agents with safe env vars if not already loaded."""
    if "server.agents" not in sys.modules:
        with patch.dict(os.environ, _SAFE_ENV, clear=False):
            import server.agents  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _future(seconds: int = 3600) -> str:
    return _format_iso(_now_utc() + timedelta(seconds=seconds))


def _past(seconds: int = 3600) -> str:
    return _format_iso(_now_utc() - timedelta(seconds=seconds))


async def _insert_repeat(
    db: Any,
    project_id: str = "misc",
    cadence: int = 1,
    end_date: str | None = None,
    max_fires: int | None = None,
    fire_count: int = 0,
    next_fire_at: str | None = None,
) -> int:
    if next_fire_at is None:
        next_fire_at = _format_iso(_now_utc() - timedelta(seconds=1))
    cur = await db.execute(
        "INSERT INTO coach_recurrence "
        "(project_id, kind, cadence, prompt, enabled, next_fire_at, "
        "end_date, max_fires, fire_count) "
        "VALUES (?, 'repeat', ?, 'test prompt', 1, ?, ?, ?, ?)",
        (project_id, str(cadence), next_fire_at, end_date, max_fires, fire_count),
    )
    await db.commit()
    return int(cur.lastrowid)


def _run_scheduler(stack: ExitStack, *, busy: bool = False, bus_capture: list | None = None):
    """Push all scheduler patches onto an ExitStack.

    Call inside `with ExitStack() as stack: _run_scheduler(stack, ...)`.
    Returns the _fire_row AsyncMock so callers can assert call count.
    """
    _ensure_agents_imported()
    stack.enter_context(patch.dict(os.environ, _SAFE_ENV, clear=False))
    stack.enter_context(patch("server.agents._coach_is_working", return_value=busy))
    stack.enter_context(patch("server.agents.is_paused", return_value=False))
    stack.enter_context(
        patch("server.agents._check_cost_caps", new=AsyncMock(return_value=(True, "ok")))
    )
    stack.enter_context(
        patch("server.recurrences.resolve_active_project", new=AsyncMock(return_value="misc"))
    )
    fire_mock = AsyncMock()
    stack.enter_context(patch.object(recmod, "_fire_row", new=fire_mock))
    if bus_capture is not None:
        async def _cap(ev: dict) -> None:
            bus_capture.append(ev)
        stack.enter_context(patch.object(recmod.bus, "publish", new=AsyncMock(side_effect=_cap)))
    return fire_mock


# ---------------------------------------------------------------------------
# 1. fire_count increments on successful fire
# ---------------------------------------------------------------------------


async def test_fire_count_increments_on_each_fire(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        rid = await _insert_repeat(c, fire_count=0)
    finally:
        await c.close()

    with ExitStack() as stack:
        _run_scheduler(stack)
        await _scheduler_iteration()

    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT fire_count FROM coach_recurrence WHERE id = ?", (rid,)
        )).fetchone())
    finally:
        await c.close()
    assert row["fire_count"] == 1

    # Advance next_fire_at for second fire
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE coach_recurrence SET next_fire_at = ? WHERE id = ?",
            (_format_iso(_now_utc() - timedelta(seconds=1)), rid),
        )
        await c.commit()
    finally:
        await c.close()

    with ExitStack() as stack:
        _run_scheduler(stack)
        await _scheduler_iteration()

    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT fire_count FROM coach_recurrence WHERE id = ?", (rid,)
        )).fetchone())
    finally:
        await c.close()
    assert row["fire_count"] == 2


# ---------------------------------------------------------------------------
# 2. max_fires expires row after N fires
# ---------------------------------------------------------------------------


async def test_max_fires_expires_after_n_fires(fresh_db: str) -> None:
    await init_db()
    events: list[dict] = []

    c = await configured_conn()
    try:
        rid = await _insert_repeat(c, max_fires=2)
    finally:
        await c.close()

    with ExitStack() as stack:
        _run_scheduler(stack, bus_capture=events)
        await _scheduler_iteration()
        # Advance for second fire
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE coach_recurrence SET next_fire_at = ? WHERE id = ?",
                (_format_iso(_now_utc() - timedelta(seconds=1)), rid),
            )
            await c.commit()
        finally:
            await c.close()
        await _scheduler_iteration()

    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT enabled, fire_count FROM coach_recurrence WHERE id = ?", (rid,)
        )).fetchone())
    finally:
        await c.close()

    assert row["enabled"] == 0
    assert row["fire_count"] == 2

    expired = [e for e in events if e.get("type") == "recurrence_expired"]
    assert expired
    assert expired[-1]["reason"] == "max_fires_reached"
    assert expired[-1]["id"] == rid


# ---------------------------------------------------------------------------
# 3. end_date in past → _compute_next_for_row returns None
# ---------------------------------------------------------------------------


async def test_end_date_in_past_at_compute_next_returns_none(fresh_db: str) -> None:
    await init_db()
    row = {
        "id": 1,
        "project_id": "misc",
        "kind": "repeat",
        "cadence": "1",
        "tz": None,
        "end_date": _past(seconds=1),
        "max_fires": None,
        "fire_count": 0,
    }
    result = await recmod._compute_next_for_row(row, _now_utc())
    assert result is None


# ---------------------------------------------------------------------------
# 4. end_date sweep disables row before its next_fire_at comes due
# ---------------------------------------------------------------------------


async def test_end_date_sweep_expires_row_between_fires(fresh_db: str) -> None:
    await init_db()
    events: list[dict] = []

    c = await configured_conn()
    try:
        rid = await _insert_repeat(
            c,
            next_fire_at=_future(3600),  # not yet due
            end_date=_past(10),          # already past
        )
    finally:
        await c.close()

    with ExitStack() as stack:
        fire_mock = _run_scheduler(stack, bus_capture=events)
        await _scheduler_iteration()

    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT enabled FROM coach_recurrence WHERE id = ?", (rid,)
        )).fetchone())
    finally:
        await c.close()

    assert row["enabled"] == 0
    expired = [e for e in events if e.get("type") == "recurrence_expired"]
    assert expired
    assert expired[0]["reason"] == "end_date_reached"
    fire_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 5. end_date already past → sweep expires (post-fire expiry path)
# ---------------------------------------------------------------------------


async def test_end_date_reached_causes_expiry(fresh_db: str) -> None:
    await init_db()
    events: list[dict] = []

    c = await configured_conn()
    try:
        rid = await _insert_repeat(c, end_date=_past(1))
    finally:
        await c.close()

    with ExitStack() as stack:
        _run_scheduler(stack, bus_capture=events)
        await _scheduler_iteration()

    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT enabled FROM coach_recurrence WHERE id = ?", (rid,)
        )).fetchone())
    finally:
        await c.close()

    assert row["enabled"] == 0
    expired = [e for e in events if e.get("type") == "recurrence_expired"]
    assert expired
    assert expired[0]["reason"] == "end_date_reached"


# ---------------------------------------------------------------------------
# 6. past end_date on creation → ValueError
# ---------------------------------------------------------------------------


async def test_past_end_date_on_creation_rejected(fresh_db: str) -> None:
    await init_db()
    with pytest.raises(ValueError, match="end_date must be in the future"):
        await create_recurrence(
            project_id="misc",
            kind="repeat",
            cadence=5,
            prompt="test",
            end_date=_past(60),
        )


# ---------------------------------------------------------------------------
# 7. max_fires validation: 0 and negative rejected
# ---------------------------------------------------------------------------


async def test_max_fires_zero_rejected(fresh_db: str) -> None:
    await init_db()
    with pytest.raises(ValueError, match="max_fires must be >= 1"):
        await create_recurrence(
            project_id="misc",
            kind="repeat",
            cadence=5,
            prompt="test",
            max_fires=0,
        )


async def test_max_fires_negative_rejected(fresh_db: str) -> None:
    await init_db()
    with pytest.raises(ValueError, match="max_fires must be >= 1"):
        await create_recurrence(
            project_id="misc",
            kind="repeat",
            cadence=5,
            prompt="test",
            max_fires=-3,
        )


# ---------------------------------------------------------------------------
# 8. recurrence_expired event payload
# ---------------------------------------------------------------------------


async def test_recurrence_expired_event_payload(fresh_db: str) -> None:
    await init_db()
    events: list[dict] = []

    c = await configured_conn()
    try:
        rid = await _insert_repeat(c, max_fires=1)
    finally:
        await c.close()

    with ExitStack() as stack:
        _run_scheduler(stack, bus_capture=events)
        await _scheduler_iteration()

    expired = [e for e in events if e.get("type") == "recurrence_expired"]
    assert expired
    ev = expired[0]
    assert "id" in ev
    assert "project_id" in ev
    assert "kind" in ev
    assert "cadence" in ev
    assert ev["reason"] in ("max_fires_reached", "end_date_reached")


# ---------------------------------------------------------------------------
# 9. skipped fire does NOT increment fire_count
# ---------------------------------------------------------------------------


async def test_skipped_fire_does_not_increment_fire_count(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        rid = await _insert_repeat(c, max_fires=3, fire_count=0)
    finally:
        await c.close()

    with ExitStack() as stack:
        _run_scheduler(stack, busy=True)
        with patch.object(recmod.bus, "publish", new=AsyncMock()):
            await _scheduler_iteration()

    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT fire_count FROM coach_recurrence WHERE id = ?", (rid,)
        )).fetchone())
    finally:
        await c.close()

    assert row["fire_count"] == 0


# ---------------------------------------------------------------------------
# Unit tests for validation helpers
# ---------------------------------------------------------------------------


def test_validate_end_date_past_raises() -> None:
    with pytest.raises(ValueError, match="must be in the future"):
        _validate_end_date(_past(60))


def test_validate_end_date_future_ok() -> None:
    result = _validate_end_date(_future(3600))
    assert result is not None


def test_validate_end_date_none_returns_none() -> None:
    assert _validate_end_date(None) is None


def test_validate_max_fires_zero_raises() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        _validate_max_fires(0)


def test_validate_max_fires_negative_raises() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        _validate_max_fires(-1)


def test_validate_max_fires_positive_ok() -> None:
    assert _validate_max_fires(5) == 5


def test_validate_max_fires_none_returns_none() -> None:
    assert _validate_max_fires(None) is None


def test_row_is_expired_max_fires() -> None:
    now = _now_utc()
    row = {"max_fires": 3, "fire_count": 3, "end_date": None}
    expired, reason = _row_is_expired(row, now)
    assert expired
    assert reason == "max_fires_reached"


def test_row_is_expired_not_yet() -> None:
    now = _now_utc()
    row = {"max_fires": 3, "fire_count": 2, "end_date": _future(3600)}
    expired, _ = _row_is_expired(row, now)
    assert not expired


def test_row_is_expired_end_date() -> None:
    now = _now_utc()
    row = {"max_fires": None, "fire_count": 0, "end_date": _past(1)}
    expired, reason = _row_is_expired(row, now)
    assert expired
    assert reason == "end_date_reached"

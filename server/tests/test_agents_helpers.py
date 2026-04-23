"""Tests for pure-DB helpers in server/agents.py.

Each helper is a thin wrapper around a SQL statement, but they're
load-bearing: _today_spend is the cost-cap engine, _*_session_id is
used by stale-session auto-heal, _get_agent_brief feeds every
turn's system prompt.
"""

from __future__ import annotations

import pytest

from server.db import configured_conn, init_db


@pytest.fixture(autouse=True)
async def _init(fresh_db: str) -> None:
    await init_db()


# ---------- _today_spend ----------


async def _insert_turn(
    agent_id: str, ended_at: str, cost_usd: float
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO turns (agent_id, started_at, ended_at, cost_usd) "
            "VALUES (?, ?, ?, ?)",
            (agent_id, ended_at, ended_at, cost_usd),
        )
        await c.commit()
    finally:
        await c.close()


async def test_today_spend_sums_today_only() -> None:
    from server.agents import _today_spend
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    today = now.replace(hour=12, minute=0).isoformat()
    yesterday = (now - timedelta(days=1)).replace(hour=12).isoformat()
    await _insert_turn("p1", today, 0.10)
    await _insert_turn("p1", today, 0.05)
    await _insert_turn("p1", yesterday, 9.99)  # should NOT count
    assert abs(await _today_spend("p1") - 0.15) < 1e-9


async def test_today_spend_team_aggregate_no_filter() -> None:
    from server.agents import _today_spend
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).replace(hour=12).isoformat()
    await _insert_turn("p1", today, 0.10)
    await _insert_turn("p2", today, 0.25)
    await _insert_turn("coach", today, 0.05)
    total = await _today_spend()  # no agent_id → team total
    assert abs(total - 0.40) < 1e-9


async def test_today_spend_empty_returns_zero() -> None:
    from server.agents import _today_spend
    assert await _today_spend("p1") == 0.0
    assert await _today_spend() == 0.0


# ---------- _get_agent_brief / _clear_session_id ----------


async def test_get_brief_returns_column_value() -> None:
    from server.agents import _get_agent_brief
    c = await configured_conn()
    try:
        await c.execute("UPDATE agents SET brief = ? WHERE id = 'p3'", ("hello\nworld",))
        await c.commit()
    finally:
        await c.close()
    assert await _get_agent_brief("p3") == "hello\nworld"


async def test_get_brief_null_returns_none() -> None:
    from server.agents import _get_agent_brief
    assert await _get_agent_brief("p3") is None


async def test_get_brief_system_agent_returns_none() -> None:
    from server.agents import _get_agent_brief
    assert await _get_agent_brief("system") is None


async def test_clear_session_id_wipes_the_column() -> None:
    from server.agents import _clear_session_id
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET session_id = 'sess-xyz' WHERE id = 'p5'"
        )
        await c.commit()
    finally:
        await c.close()
    await _clear_session_id("p5")
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT session_id FROM agents WHERE id = 'p5'")
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["session_id"] is None


async def test_clear_session_id_idempotent() -> None:
    from server.agents import _clear_session_id
    # Running against an agent that already has NULL session_id must
    # not raise.
    await _clear_session_id("p7")
    await _clear_session_id("p7")

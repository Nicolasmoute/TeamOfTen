"""Tests for pure-DB helpers in server/agents.py.

After the projects refactor (PROJECTS_SPEC.md §3) brief / session_id
moved out of the agents row — the tests verify the new
agent_project_roles + agent_sessions tables instead.
"""

from __future__ import annotations

import pytest

from server.db import configured_conn, init_db, resolve_active_project


@pytest.fixture(autouse=True)
async def _init(fresh_db: str) -> None:
    await init_db()


# ---------- _today_spend ----------


async def _insert_turn(
    agent_id: str, ended_at: str, cost_usd: float
) -> None:
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO turns (agent_id, project_id, started_at, ended_at, cost_usd) "
            "VALUES (?, ?, ?, ?, ?)",
            (agent_id, project_id, ended_at, ended_at, cost_usd),
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


async def _set_brief(slot: str, brief: str | None) -> None:
    pid = await resolve_active_project()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO agent_project_roles (slot, project_id, brief) "
            "VALUES (?, ?, ?) ON CONFLICT(slot, project_id) DO UPDATE SET "
            "brief = excluded.brief",
            (slot, pid, brief),
        )
        await c.commit()
    finally:
        await c.close()


async def test_get_brief_returns_column_value() -> None:
    from server.agents import _get_agent_brief
    await _set_brief("p3", "hello\nworld")
    assert await _get_agent_brief("p3") == "hello\nworld"


async def test_get_brief_null_returns_none() -> None:
    from server.agents import _get_agent_brief
    assert await _get_agent_brief("p3") is None


async def test_get_brief_system_agent_returns_none() -> None:
    from server.agents import _get_agent_brief
    assert await _get_agent_brief("system") is None


async def test_clear_session_id_wipes_the_row() -> None:
    from server.agents import _clear_session_id
    pid = await resolve_active_project()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO agent_sessions (slot, project_id, session_id) "
            "VALUES ('p5', ?, 'sess-xyz')",
            (pid,),
        )
        await c.commit()
    finally:
        await c.close()
    await _clear_session_id("p5")
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT session_id FROM agent_sessions "
            "WHERE slot = 'p5' AND project_id = ?",
            (pid,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    # Either no row, or row with NULL session_id — both indicate cleared.
    if row is not None:
        assert dict(row)["session_id"] is None


async def test_clear_session_id_idempotent() -> None:
    from server.agents import _clear_session_id
    # Running against an agent that already has no session row must not raise.
    await _clear_session_id("p7")
    await _clear_session_id("p7")

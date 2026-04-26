"""Tests for server.agents._autoname_player.

Per the projects refactor (PROJECTS_SPEC.md §3) names live in
agent_project_roles per (slot, project), not on the agents row.
"""

from __future__ import annotations

import asyncio

import pytest

from server.db import configured_conn, init_db, resolve_active_project


@pytest.fixture(autouse=True)
async def _init(fresh_db: str) -> None:
    """Every test starts from a schema + seeded roster."""
    await init_db()


async def _name_of(agent_id: str) -> str | None:
    pid = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT name FROM agent_project_roles "
            "WHERE slot = ? AND project_id = ?",
            (agent_id, pid),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    return dict(row)["name"] if row else None


async def test_autoname_assigns_from_pool() -> None:
    from server.agents import _LACROSSE_SURNAMES, _autoname_player
    name = await _autoname_player("p3")
    assert name is not None
    assert name in _LACROSSE_SURNAMES
    assert await _name_of("p3") == name


async def test_autoname_is_noop_if_already_named() -> None:
    from server.agents import _autoname_player
    await _autoname_player("p3")
    first = await _name_of("p3")
    result = await _autoname_player("p3")
    assert result is None, "second call should not pick a new name"
    assert await _name_of("p3") == first


async def test_autoname_skips_coach() -> None:
    from server.agents import _autoname_player
    # Coach is seeded with kind='coach'. _autoname_player only assigns
    # to kind='player' slots.
    result = await _autoname_player("coach")
    assert result is None


async def test_autoname_produces_distinct_names_serially() -> None:
    from server.agents import _autoname_player
    names = []
    for i in range(1, 11):
        name = await _autoname_player(f"p{i}")
        assert name is not None, f"p{i} should have gotten a name"
        names.append(name)
    # All 10 Players should end up with distinct surnames.
    assert len(set(names)) == 10


async def test_autoname_is_race_safe_concurrent() -> None:
    from server.agents import _autoname_player
    results = await asyncio.gather(*[_autoname_player(f"p{i}") for i in range(1, 11)])
    assert all(r is not None for r in results), "every slot should get a name"
    assert len(set(results)) == 10, f"names must be distinct, got {results!r}"

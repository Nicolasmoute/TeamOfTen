"""Tests for server.agents._autoname_player.

Can run in CI because the Dockerless `uv sync --extra dev` pulls
claude-agent-sdk as a dep — importing server.agents then works. The
tests only exercise DB paths; no subprocess spawn.
"""

from __future__ import annotations

import asyncio

import pytest

from server.db import configured_conn, init_db


@pytest.fixture(autouse=True)
async def _init(fresh_db: str) -> None:
    """Every test starts from a schema + seeded roster."""
    await init_db()


async def _name_of(agent_id: str) -> str | None:
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT name FROM agents WHERE id = ?", (agent_id,))
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
    # Coach is seeded with kind='coach' and name='Coach'. _autoname_player
    # only assigns to kind='player' slots with no current name.
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
    # Fire all 10 auto-names at once. Without the asyncio.Lock this
    # regressed to duplicates because each SELECT saw the same
    # 'taken' snapshot before any UPDATE committed. Lock serializes
    # the read-pick-commit window.
    results = await asyncio.gather(*[_autoname_player(f"p{i}") for i in range(1, 11)])
    assert all(r is not None for r in results), "every slot should get a name"
    assert len(set(results)) == 10, f"names must be distinct, got {results!r}"

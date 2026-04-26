"""Phase 1 isolation gate (PROJECTS_SPEC.md §13 / §16).

Creates two projects, writes domain rows to project A, switches the
active pointer to project B, asserts every project-scoped query
returns 0 rows from A. Then flips back and asserts A's rows reappear.
"""

from __future__ import annotations

import pytest

from server.db import configured_conn, init_db, resolve_active_project


PROJECT_A = "alpha"
PROJECT_B = "beta"


@pytest.fixture(autouse=True)
async def _init(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO projects (id, name) VALUES (?, 'Alpha')", (PROJECT_A,)
        )
        await c.execute(
            "INSERT INTO projects (id, name) VALUES (?, 'Beta')", (PROJECT_B,)
        )
        await c.commit()
    finally:
        await c.close()


async def _set_active(project_id: str) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT OR REPLACE INTO team_config (key, value) "
            "VALUES ('active_project_id', ?)",
            (project_id,),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_alpha_rows() -> None:
    """Drop one row into each project-scoped table under PROJECT_A."""
    c = await configured_conn()
    try:
        # tasks
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, created_by) "
            "VALUES ('t-alpha-1', ?, 'alpha task', 'human')",
            (PROJECT_A,),
        )
        # messages
        await c.execute(
            "INSERT INTO messages (project_id, from_id, to_id, body) "
            "VALUES (?, 'human', 'coach', 'alpha note')",
            (PROJECT_A,),
        )
        # memory_docs
        await c.execute(
            "INSERT INTO memory_docs (project_id, topic, content, last_updated_by) "
            "VALUES (?, 'alpha-topic', 'alpha body', 'human')",
            (PROJECT_A,),
        )
        # events
        await c.execute(
            "INSERT INTO events (ts, agent_id, project_id, type, payload) "
            "VALUES ('2026-04-25T00:00:00Z', 'coach', ?, 'demo', '{}')",
            (PROJECT_A,),
        )
        # turns
        await c.execute(
            "INSERT INTO turns (agent_id, project_id, started_at, ended_at) "
            "VALUES ('coach', ?, '2026-04-25T00:00:00Z', '2026-04-25T00:00:01Z')",
            (PROJECT_A,),
        )
        await c.commit()
    finally:
        await c.close()


async def _count_active_scoped() -> dict[str, int]:
    """Run a SELECT on each project-scoped table filtered by the
    currently active project_id. Returns a dict {table: count}.
    """
    pid = await resolve_active_project()
    out: dict[str, int] = {}
    c = await configured_conn()
    try:
        for table in ("tasks", "messages", "memory_docs", "events", "turns"):
            cur = await c.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE project_id = ?",
                (pid,),
            )
            row = await cur.fetchone()
            out[table] = int(dict(row)["n"])
    finally:
        await c.close()
    return out


async def test_isolation_alpha_invisible_when_beta_is_active() -> None:
    await _seed_alpha_rows()

    # Sanity: with alpha active, every table sees its row.
    await _set_active(PROJECT_A)
    counts = await _count_active_scoped()
    for tbl, n in counts.items():
        assert n >= 1, f"{tbl}: expected ≥1 alpha row, got {n}"

    # Switch to beta — every project-scoped query should now see zero
    # alpha rows. (Beta has no rows seeded.)
    await _set_active(PROJECT_B)
    counts_beta = await _count_active_scoped()
    for tbl, n in counts_beta.items():
        assert n == 0, (
            f"isolation breach: {tbl} returned {n} rows under beta "
            "(should be 0 — alpha rows must be invisible)"
        )

    # Flip back. Alpha rows must reappear (they were never deleted).
    await _set_active(PROJECT_A)
    counts_alpha_again = await _count_active_scoped()
    for tbl, n in counts_alpha_again.items():
        assert n >= 1, f"{tbl}: alpha row vanished after switch-back, got {n}"


# Phase 3 audit follow-up: extend isolation gate to the three Phase-1
# project-keyed tables that test_isolation_alpha_invisible_when_beta_is_active
# omitted (agent_sessions, agent_project_roles, sync_state).


async def _seed_phase1_keyed_alpha_rows() -> None:
    """Drop one row into each (slot, project) and (project, tree, path)
    table under PROJECT_A."""
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO agent_sessions "
            "(slot, project_id, session_id, last_active) "
            "VALUES ('coach', ?, 'sess-alpha', '2026-04-25T00:00:00Z')",
            (PROJECT_A,),
        )
        await c.execute(
            "INSERT INTO agent_project_roles "
            "(slot, project_id, name, role) "
            "VALUES ('p1', ?, 'Alpha-Alice', 'lead')",
            (PROJECT_A,),
        )
        await c.execute(
            "INSERT INTO sync_state "
            "(project_id, tree, path, mtime, size_bytes, sha256, last_synced_at) "
            "VALUES (?, 'project', 'memory/alpha.md', 1.0, 1, 'aa', "
            "        '2026-04-25T00:00:00Z')",
            (PROJECT_A,),
        )
        await c.commit()
    finally:
        await c.close()


async def _count_phase1_keyed_for(project_id: str) -> dict[str, int]:
    out: dict[str, int] = {}
    c = await configured_conn()
    try:
        for table in ("agent_sessions", "agent_project_roles", "sync_state"):
            cur = await c.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE project_id = ?",
                (project_id,),
            )
            row = await cur.fetchone()
            out[table] = int(dict(row)["n"])
    finally:
        await c.close()
    return out


async def test_isolation_phase1_keyed_tables() -> None:
    """agent_sessions, agent_project_roles, and sync_state must respect
    project_id boundaries the same way the §3 domain tables do."""
    await _seed_phase1_keyed_alpha_rows()

    # Alpha sees its rows.
    counts_alpha = await _count_phase1_keyed_for(PROJECT_A)
    for tbl, n in counts_alpha.items():
        assert n >= 1, f"{tbl}: expected ≥1 alpha row, got {n}"

    # Beta has no rows.
    counts_beta = await _count_phase1_keyed_for(PROJECT_B)
    for tbl, n in counts_beta.items():
        assert n == 0, (
            f"isolation breach: {tbl} returned {n} rows under beta "
            "(should be 0 — alpha rows must be project-scoped)"
        )


async def test_pin_active_project_overrides_team_config() -> None:
    """Phase 3 TOCTOU mitigation: pin_active_project() short-circuits
    resolve_active_project() so a switch mid-tool-call is coherent."""
    from server.db import pin_active_project

    await _set_active(PROJECT_A)
    assert await resolve_active_project() == PROJECT_A
    with pin_active_project(PROJECT_B):
        assert await resolve_active_project() == PROJECT_B
    # Pin lifted: back to whatever team_config says.
    assert await resolve_active_project() == PROJECT_A


async def test_set_active_project_writes_team_config() -> None:
    from server.db import set_active_project

    await set_active_project(PROJECT_B)
    assert await resolve_active_project() == PROJECT_B
    await set_active_project(PROJECT_A)
    assert await resolve_active_project() == PROJECT_A


# Phase 3 audit fix #6 (Phase-1-audit-follow-up #5): the original
# isolation test seeded rows directly via SQL, so it would have
# missed the four bare `INSERT INTO messages` callers fixed in
# Phase 1's audit. These tests exercise the production code that
# resolves the active project then INSERTs a row, asserting the
# row lands under the active project.


async def _set_active(project_id: str) -> None:  # noqa: F811 — re-using helper
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT OR REPLACE INTO team_config (key, value) "
            "VALUES ('active_project_id', ?)",
            (project_id,),
        )
        await c.commit()
    finally:
        await c.close()


async def _count_messages_for(project_id: str) -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE project_id = ?",
            (project_id,),
        )
        row = await cur.fetchone()
        return int(dict(row)["n"])
    finally:
        await c.close()


async def test_deliver_system_message_lands_in_active_project() -> None:
    """`server.agents._deliver_system_message` resolves the active
    project + INSERTs into messages. Switching active mid-call
    should route subsequent messages under the new project."""
    from server.agents import _deliver_system_message

    await _set_active(PROJECT_A)
    await _deliver_system_message(
        from_id="system",
        to_id="coach",
        subject="alpha-msg",
        body="for alpha",
        priority="normal",
    )
    await _set_active(PROJECT_B)
    await _deliver_system_message(
        from_id="system",
        to_id="coach",
        subject="beta-msg",
        body="for beta",
        priority="normal",
    )
    assert await _count_messages_for(PROJECT_A) >= 1
    assert await _count_messages_for(PROJECT_B) >= 1
    # Cross-project leak check: alpha's msg shouldn't be counted
    # under beta and vice versa.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT subject, project_id FROM messages "
            "WHERE subject IN ('alpha-msg', 'beta-msg')"
        )
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()
    by_subj = {r["subject"]: r["project_id"] for r in rows}
    assert by_subj.get("alpha-msg") == PROJECT_A
    assert by_subj.get("beta-msg") == PROJECT_B


async def test_pin_active_project_overrides_team_config_during_write(
) -> None:
    """If a tool call begins mid-switch, pin_active_project must make
    its INSERT land under the pinned id even though team_config has
    the old value (this is the TOCTOU mitigation in action)."""
    from server.agents import _deliver_system_message
    from server.db import pin_active_project

    await _set_active(PROJECT_A)
    # Without the pin, the INSERT goes under A.
    with pin_active_project(PROJECT_B):
        await _deliver_system_message(
            from_id="system",
            to_id="coach",
            subject="pinned-msg",
            body="under pin",
            priority="normal",
        )
    # team_config still says A, but the message must be under B.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT project_id FROM messages WHERE subject = 'pinned-msg'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is not None
    assert dict(row)["project_id"] == PROJECT_B

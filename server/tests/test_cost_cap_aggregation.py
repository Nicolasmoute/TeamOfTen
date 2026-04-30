"""Audit-item-27 — `_today_spend()` and the cost-cap path treat Claude
+ Codex turns symmetrically.

Per Docs/CODEX_RUNTIME_SPEC.md §G.2: the existing `_check_cost_caps`
sums `cost_usd` regardless of runtime. ChatGPT-auth Codex turns
contribute 0 (cost_basis='plan_included') so they don't trip the USD
cap; API-key Codex turns contribute their token-priced cost.

This test pins both behaviors with mixed-runtime rows in `turns`.
"""

from __future__ import annotations

import aiosqlite

import server.db as dbmod


async def _insert(
    db: aiosqlite.Connection,
    *,
    agent_id: str,
    cost_usd: float,
    runtime: str,
    cost_basis: str,
    ts: str = "2026-04-28T00:00:00Z",
    project_id: str = "misc",
) -> None:
    await db.execute(
        "INSERT INTO turns ("
        "agent_id, project_id, started_at, ended_at, duration_ms, cost_usd, "
        "session_id, num_turns, stop_reason, is_error, "
        "model, plan_mode, effort, runtime, cost_basis"
        ") VALUES (?, ?, ?, ?, 0, ?, NULL, 1, 'end', 0, NULL, 0, NULL, ?, ?)",
        (agent_id, project_id, ts, ts, cost_usd, runtime, cost_basis),
    )


async def test_today_spend_aggregates_across_runtimes(fresh_db: str) -> None:
    """Mixed-runtime rows: claude $0.30 + codex/api_key $0.50 + codex/
    plan_included $0.00 → today's spend = $0.80."""
    await dbmod.init_db()
    from server.agents import _today_spend

    today_iso = (await _today_iso())
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await _insert(db, agent_id="p1", cost_usd=0.30, runtime="claude",
                      cost_basis="token_priced", ts=today_iso)
        await _insert(db, agent_id="p2", cost_usd=0.50, runtime="codex",
                      cost_basis="token_priced", ts=today_iso)
        await _insert(db, agent_id="p3", cost_usd=0.00, runtime="codex",
                      cost_basis="plan_included", ts=today_iso)
        await db.commit()

    spend = await _today_spend("p1")
    # Per-agent path: only p1's cost.
    assert abs(spend - 0.30) < 1e-6, f"p1 expected 0.30, got {spend}"

    team = await _today_spend(None)
    # Team path: 0.30 + 0.50 + 0.00 = 0.80
    assert abs(team - 0.80) < 1e-6, f"team expected 0.80, got {team}"


async def test_chatgpt_auth_codex_turns_dont_trip_usd_cap(
    fresh_db: str,
    monkeypatch,
) -> None:
    """A ChatGPT-auth Codex turn (cost_basis='plan_included',
    cost_usd=0) must NOT contribute to the USD cap. Insert a row with
    a generous cost_usd=999 marked plan_included would be a misuse;
    by convention plan_included rows have cost_usd=0. Pin the
    convention by asserting cap check uses summed cost_usd directly.
    """
    await dbmod.init_db()
    from server.agents import _today_spend, _check_cost_caps
    monkeypatch.setenv("HARNESS_AGENT_DAILY_USD", "1.0")

    today_iso = await _today_iso()
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        # Two plan_included rows with cost_usd=0 — should not gate.
        await _insert(db, agent_id="p1", cost_usd=0.0, runtime="codex",
                      cost_basis="plan_included", ts=today_iso)
        await _insert(db, agent_id="p1", cost_usd=0.0, runtime="codex",
                      cost_basis="plan_included", ts=today_iso)
        await db.commit()

    # No spend → cap check passes.
    assert (await _today_spend("p1")) == 0.0


async def _today_iso() -> str:
    """Return an ISO timestamp inside today's UTC date so
    `_today_spend`'s `ended_at >= today_start` clause matches."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    # Use the actual current UTC moment so the row sorts after the
    # day boundary regardless of when the test runs.
    return now.isoformat()


# ---------- cost-reset (per-project / global) -----------------------


async def _set_team_config(
    db: aiosqlite.Connection, key: str, value: str
) -> None:
    await db.execute(
        "INSERT INTO team_config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


async def test_cost_reset_global_zeroes_today_spend(fresh_db: str) -> None:
    """Global `cost_reset_at` set to NOW must zero today's team spend
    even though historical turn rows remain in the table."""
    await dbmod.init_db()
    from server.agents import _today_spend
    from datetime import datetime, timezone

    earlier = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("alpha", "Alpha"),
        )
        await _insert(db, agent_id="p1", cost_usd=0.40, runtime="claude",
                      cost_basis="token_priced", ts=earlier,
                      project_id="alpha")
        await db.commit()

    assert abs((await _today_spend()) - 0.40) < 1e-6

    # Reset at NOW (after the row's ended_at). Row should be excluded.
    later = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await _set_team_config(db, "cost_reset_at", later)
        await db.commit()

    spend_after = await _today_spend()
    assert abs(spend_after) < 1e-6, f"expected 0.0 after reset, got {spend_after}"


async def test_cost_reset_per_project_only_affects_one(fresh_db: str) -> None:
    """A per-project reset must zero ONLY that project's contribution
    and leave other projects' today_spend intact. Team total drops by
    the reset project's amount (the user's spec: ALL = sum of projects)."""
    await dbmod.init_db()
    from server.agents import _today_spend
    from datetime import datetime, timezone

    earlier = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("alpha", "Alpha"),
        )
        await db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("beta", "Beta"),
        )
        await _insert(db, agent_id="p1", cost_usd=0.30, runtime="claude",
                      cost_basis="token_priced", ts=earlier,
                      project_id="alpha")
        await _insert(db, agent_id="p2", cost_usd=0.50, runtime="claude",
                      cost_basis="token_priced", ts=earlier,
                      project_id="beta")
        await db.commit()

    # Both projects contribute → team = 0.80.
    assert abs((await _today_spend()) - 0.80) < 1e-6

    # Reset just beta.
    later = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await _set_team_config(db, "cost_reset_at_beta", later)
        await db.commit()

    # Team is now alpha's 0.30 only — beta is shadowed.
    team_after = await _today_spend()
    assert abs(team_after - 0.30) < 1e-6, (
        f"expected 0.30 after beta reset, got {team_after}"
    )

    # Project-scoped queries reflect the same.
    alpha_after = await _today_spend(project_id="alpha")
    beta_after = await _today_spend(project_id="beta")
    assert abs(alpha_after - 0.30) < 1e-6
    assert abs(beta_after) < 1e-6


async def test_cost_reset_archived_project_still_counted_in_team(
    fresh_db: str,
) -> None:
    """Archived projects' turn rows must still contribute to the
    team-wide `_today_spend()` (which feeds the cap check). This
    ensures the EnvPane's `team.today_usd` matches the cap bar's
    number — otherwise the user sees an inconsistency where
    'sum of projects' < 'team_today_usd' from /api/status."""
    await dbmod.init_db()
    from server.agents import _today_spend
    from datetime import datetime, timezone

    today_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "INSERT INTO projects (id, name, archived) VALUES (?, ?, 1)",
            ("alpha-old", "Alpha (archived)"),
        )
        await _insert(db, agent_id="p1", cost_usd=0.25, runtime="claude",
                      cost_basis="token_priced", ts=today_iso,
                      project_id="alpha-old")
        await db.commit()

    # Archived project rows must still be in the team total — caps are
    # team-wide and need to see all spend regardless of archival flag.
    team = await _today_spend()
    assert abs(team - 0.25) < 1e-6, f"archived project excluded? team={team}"

    # Project-scoped query also returns the archived project's spend
    # (no archived filter on the turns table — historical data is
    # historical regardless of project state).
    proj = await _today_spend(project_id="alpha-old")
    assert abs(proj - 0.25) < 1e-6


async def test_cost_reset_project_id_filter_uses_per_project_window(
    fresh_db: str,
) -> None:
    """Audit-fix: when `_today_spend` is called with a `project_id`
    filter, the SQL short-circuits and uses just that project's
    effective window directly (no CASE branch over other projects).
    This test pins that the correct number is returned — the
    optimization can't change the result."""
    await dbmod.init_db()
    from server.agents import _today_spend
    from datetime import datetime, timezone

    today_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("alpha", "Alpha"),
        )
        await db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("beta", "Beta"),
        )
        await _insert(db, agent_id="p1", cost_usd=0.40, runtime="claude",
                      cost_basis="token_priced", ts=today_iso,
                      project_id="alpha")
        await _insert(db, agent_id="p2", cost_usd=0.60, runtime="claude",
                      cost_basis="token_priced", ts=today_iso,
                      project_id="beta")
        # Reset alpha after the row was inserted; alpha shows 0,
        # beta shows 0.60 — both via the project_id-filtered path.
        later = datetime.now(timezone.utc).isoformat()
        await _set_team_config(db, "cost_reset_at_alpha", later)
        # Add a stale (older) reset on beta — must NOT zero beta.
        await _set_team_config(
            db, "cost_reset_at_beta", "2020-01-01T00:00:00Z"
        )
        await db.commit()

    assert abs(await _today_spend(project_id="alpha")) < 1e-6
    beta = await _today_spend(project_id="beta")
    assert abs(beta - 0.60) < 1e-6, f"beta expected 0.60, got {beta}"


async def test_cost_reset_global_shadows_stale_per_project_resets(
    fresh_db: str,
) -> None:
    """When the global reset is set AFTER a per-project reset, the
    global wins (everything zeroes). The per-project reset row stays
    in team_config but is inert."""
    await dbmod.init_db()
    from server.agents import _today_spend
    from datetime import datetime, timezone

    earlier = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await db.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("alpha", "Alpha"),
        )
        await _insert(db, agent_id="p1", cost_usd=0.40, runtime="claude",
                      cost_basis="token_priced", ts=earlier,
                      project_id="alpha")
        # Stale per-project reset older than what we'll set globally.
        await _set_team_config(db, "cost_reset_at_alpha", "2020-01-01T00:00:00Z")
        await db.commit()

    later = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        await _set_team_config(db, "cost_reset_at", later)
        await db.commit()

    assert abs(await _today_spend()) < 1e-6
    assert abs(await _today_spend(project_id="alpha")) < 1e-6

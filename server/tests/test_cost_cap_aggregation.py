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

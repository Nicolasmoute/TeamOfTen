"""PR 5 — Codex pricing table tests.

Per Docs/CODEX_RUNTIME_SPEC.md §J: table of (model, usage, expected
USD) cases plus an unknown-model fallback.
"""

from __future__ import annotations

import pytest

from server.pricing import CODEX_PRICING, codex_cost_usd


def test_gpt_5_4_basic_token_math() -> None:
    # 1M input + 1M output → $5 + $15 = $20
    cost = codex_cost_usd("gpt-5.4", {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    })
    assert cost == pytest.approx(20.0, abs=1e-6)


def test_gpt_5_4_cached_input_priced_separately() -> None:
    # 1M cached input → $0.50 (vs $5 uncached)
    cost = codex_cost_usd("gpt-5.4", {"cached_input_tokens": 1_000_000})
    assert cost == pytest.approx(0.50, abs=1e-6)


def test_gpt_5_4_mini_basic() -> None:
    # 1M input + 1M output → $0.25 + $1.0 = $1.25
    cost = codex_cost_usd("gpt-5.4-mini", {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
    })
    assert cost == pytest.approx(1.25, abs=1e-6)


def test_zero_usage_zero_cost() -> None:
    assert codex_cost_usd("gpt-5.4", {}) == 0.0


def test_unknown_model_uses_pessimistic_fallback() -> None:
    # The fallback should NOT be free — under-billing trips the cap
    # late and burns budget. 1M input on the fallback should cost
    # the same as gpt-5.4 input (the fallback we picked).
    cost_unknown = codex_cost_usd("gpt-7-future", {"input_tokens": 1_000_000})
    cost_known = codex_cost_usd("gpt-5.4", {"input_tokens": 1_000_000})
    assert cost_unknown == cost_known
    assert cost_unknown > 0


def test_none_model_treated_as_unknown() -> None:
    cost = codex_cost_usd(None, {"input_tokens": 1_000})
    assert cost > 0  # not crashed


def test_garbage_token_values_dont_crash() -> None:
    cost = codex_cost_usd("gpt-5.4", {
        "input_tokens": "not-a-number",
        "output_tokens": None,
        "cached_input_tokens": 100,
    })
    # Cached: 100 * 0.50 / 1M = 0.00005
    assert cost == pytest.approx(5e-5, abs=1e-9)


def test_extract_usage_codex_basic() -> None:
    """Codex Turn.usage shape → harness {input, output, cache_read,
    cache_creation} dict. cached_input_tokens maps to cache_read;
    cache_creation is always 0 (Codex caching has no creation cost)."""
    from server.agents import _extract_usage_codex

    out = _extract_usage_codex({
        "input_tokens": 1500,
        "output_tokens": 300,
        "cached_input_tokens": 100,
    })
    assert out == {
        "input": 1500,
        "output": 300,
        "cache_read": 100,
        "cache_creation": 0,
    }


def test_extract_usage_codex_handles_none() -> None:
    """Codex sometimes returns usage=None on streamed turns (early SDK
    builds). The extractor must default to all-zeros instead of crashing."""
    from server.agents import _extract_usage_codex
    assert _extract_usage_codex(None) == {
        "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0
    }


def test_extract_usage_codex_garbage_values() -> None:
    """Non-int values → 0 (defensive against SDK typing drift)."""
    from server.agents import _extract_usage_codex
    out = _extract_usage_codex({
        "input_tokens": "not-a-number",
        "output_tokens": None,
        "cached_input_tokens": 50,
    })
    assert out == {"input": 0, "output": 0, "cache_read": 50, "cache_creation": 0}


def test_extract_usage_claude_still_works_via_alias() -> None:
    """The legacy `_extract_usage` alias must still resolve to the
    Claude-shape extractor for any unmigrated callers."""
    from server.agents import _extract_usage, _extract_usage_claude
    assert _extract_usage is _extract_usage_claude


async def test_insert_turn_row_records_runtime_and_cost_basis(fresh_db) -> None:
    """`_insert_turn_row` accepts `runtime` and `cost_basis` kwargs and
    persists them on the turns row. Default is 'claude' / 'token_priced'."""
    import aiosqlite
    import server.db as dbmod
    await dbmod.init_db()
    from server.agents import _insert_turn_row

    # Default kwargs → claude/token_priced.
    await _insert_turn_row(
        agent_id="p1",
        started_at="2026-04-28T00:00:00Z",
        ended_at="2026-04-28T00:00:01Z",
        duration_ms=1000,
        cost_usd=0.5,
        session_id="sess1",
        num_turns=1,
        stop_reason="end_turn",
        is_error=False,
        model="claude-sonnet-4-6",
        plan_mode=False,
        effort=None,
    )

    # Codex turn — explicit kwargs.
    await _insert_turn_row(
        agent_id="p1",
        started_at="2026-04-28T00:00:02Z",
        ended_at="2026-04-28T00:00:03Z",
        duration_ms=1000,
        cost_usd=0.0,
        session_id=None,
        num_turns=1,
        stop_reason="end_turn",
        is_error=False,
        model="gpt-5.4",
        plan_mode=False,
        effort=None,
        runtime="codex",
        cost_basis="plan_included",
    )

    async with aiosqlite.connect(fresh_db, timeout=10.0) as db:
        cur = await db.execute(
            "SELECT runtime, cost_basis FROM turns "
            "WHERE agent_id = 'p1' ORDER BY id"
        )
        rows = [tuple(row) for row in await cur.fetchall()]
    assert rows == [
        ("claude", "token_priced"),
        ("codex", "plan_included"),
    ]


def test_pricing_table_has_required_models() -> None:
    """Floor sanity — the table must list the two flagship Codex
    models. If OpenAI renames them, update CODEX_PRICING + this list
    in lockstep."""
    assert "gpt-5.4" in CODEX_PRICING
    assert "gpt-5.4-mini" in CODEX_PRICING
    for entry in CODEX_PRICING.values():
        assert {"input", "cached", "output"} <= set(entry.keys())

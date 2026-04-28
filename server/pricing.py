"""Token-priced cost calculation for runtimes that report token counts
but not USD.

Claude turns: `ResultMessage.total_cost_usd` is authoritative — we
record it directly into `turns.cost_usd` and don't touch this module.

Codex turns: the SDK reports tokens via `Turn.usage` but no USD. For
API-key Codex (cost_basis='token_priced') we compute USD here. For
ChatGPT-auth Codex (cost_basis='plan_included') we record `cost_usd=0`
and rely on tokens for visibility — so this module isn't called.

See `Docs/CODEX_RUNTIME_SPEC.md` §E.5 / §G.
"""

from __future__ import annotations

from collections.abc import Mapping

# Per OpenAI's Codex pricing page. Numbers are USD per 1M tokens.
# https://developers.openai.com/codex/pricing
#
# Update procedure when OpenAI revises rates: edit this table, bump
# `PRICING_VERSION`, run `pytest server/tests/test_codex_pricing.py`.
PRICING_VERSION = "2026-04-28"

CODEX_PRICING: dict[str, dict[str, float]] = {
    # Standard model ids. Lowercase canonical; lookup normalizes.
    "gpt-5.4":      {"input": 5.0,   "cached": 0.50,  "output": 15.0},
    "gpt-5.4-mini": {"input": 0.25,  "cached": 0.025, "output": 1.0},
}

# Fallback when an unknown model id is recorded — keeps the cost cap
# functional rather than crashing on a model the table hasn't seen
# yet. Numbers are intentionally pessimistic so a missing entry
# over-estimates spend rather than under-estimating.
_UNKNOWN_MODEL_PRICE = {"input": 5.0, "cached": 0.50, "output": 15.0}


def _normalize_model(model: str | None) -> str:
    if not model:
        return ""
    return model.strip().lower()


def codex_cost_usd(model: str | None, usage: Mapping[str, int]) -> float:
    """Compute USD cost for one Codex turn from its `Turn.usage` dict.

    `usage` is expected to carry one or more of:
      - input_tokens             — uncached prompt tokens
      - cached_input_tokens      — cache-read prompt tokens (cheaper)
      - output_tokens            — completion + reasoning tokens

    Missing keys default to 0. Unknown model ids fall back to a
    pessimistic priced-similar-to-flagship table — a missed entry
    over-bills (cost cap trips early) rather than under-bills.
    """
    rates = CODEX_PRICING.get(_normalize_model(model), _UNKNOWN_MODEL_PRICE)

    def _get(key: str) -> int:
        v = usage.get(key, 0)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    inp = _get("input_tokens")
    cached = _get("cached_input_tokens")
    out = _get("output_tokens")

    cost = (
        (inp * rates["input"])
        + (cached * rates["cached"])
        + (out * rates["output"])
    ) / 1_000_000.0
    # Round to 6 decimals to stay below the column's REAL precision
    # noise floor; the pre-existing turns.cost_usd column is REAL so
    # we follow the same convention.
    return round(cost, 6)

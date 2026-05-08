"""Playbook configuration — caps, thresholds, schema version, env knobs.

All values are spec-derived (Docs/playbook-specs.md §11). Where the
harness expects to override per-deploy, an env var is honored; otherwise
the constant is the source of truth.

Read this module directly:
    from server.playbook import config
    if active + new_creations > config.SOFT_STATEMENT_CAP: ...

Don't shadow values at call sites — env-var resolution happens once
at import time so a process-wide override is consistent.
"""

from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v >= 0 else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# Schema version stamped on every state file. Bump only when a
# migration script lands under server/playbook/migrations/.
PLAYBOOK_SCHEMA_VERSION = 1

# ----------------------------------------------------------- weights
# Bootstrap-time seed weight: leaning YES, conservatively (spec §1.2).
BOOTSTRAP_WEIGHT = _env_float("HARNESS_PLAYBOOK_BOOTSTRAP_WEIGHT", 0.75)

# Coach-mid-turn creation weight: slightly under bootstrap since
# not yet seeded by the original prose corpus (spec §1.2).
COACH_CREATION_WEIGHT = _env_float("HARNESS_PLAYBOOK_COACH_CREATION_WEIGHT", 0.60)

# Single-proposal delta cap (spec §5.5/§5.6 — prevents one noisy day
# from flipping a stable consensus).
ADJUST_DELTA_CAP = _env_float("HARNESS_PLAYBOOK_ADJUST_DELTA_CAP", 0.25)

# ----------------------------------------------------------- caps
# Statement-count caps. Soft cap is the working ceiling; hard cap is
# the operator-review trigger (spec §5.7).
SOFT_STATEMENT_CAP = _env_int("HARNESS_PLAYBOOK_SOFT_CAP", 100)
HARD_STATEMENT_CAP = _env_int("HARNESS_PLAYBOOK_HARD_CAP", 110)

# ----------------------------------------------- settle / stale thresholds
# A statement settles or stales after WEIGHT crosses the threshold AND
# remains there for STABLE_DAYS without an excursion (spec §5.8).
SETTLE_THRESHOLD = _env_float("HARNESS_PLAYBOOK_SETTLE_THRESHOLD", 0.95)
STALE_THRESHOLD = _env_float("HARNESS_PLAYBOOK_STALE_THRESHOLD", 0.15)
SETTLE_STABLE_DAYS = _env_int("HARNESS_PLAYBOOK_SETTLE_STABLE_DAYS", 7)
STALE_STABLE_DAYS = _env_int("HARNESS_PLAYBOOK_STALE_STABLE_DAYS", 7)

# Stale-unused: never-fired statements past N days get archived (spec §5.8).
STALE_UNUSED_DAYS = _env_int("HARNESS_PLAYBOOK_STALE_UNUSED_DAYS", 30)

# ------------------------------------------------------- evidence bundle
# Total bundle target (soft) and hard cap; runner truncates sections
# in §5.4-defined order if over (spec §5.4).
EVIDENCE_BUNDLE_TARGET_BYTES = _env_int(
    "HARNESS_PLAYBOOK_EVIDENCE_BUNDLE_TARGET_BYTES", 6_000
)
EVIDENCE_BUNDLE_MAX_BYTES = _env_int(
    "HARNESS_PLAYBOOK_EVIDENCE_BUNDLE_MAX_BYTES", 10_000
)

# Median-window for cost-outlier classification + per-trajectory cost
# bucket comparison (spec §5.4 + §S2).
EVIDENCE_MEDIAN_WINDOW_DAYS = _env_int(
    "HARNESS_PLAYBOOK_EVIDENCE_MEDIAN_WINDOW_DAYS", 30
)
EVIDENCE_MEDIAN_MIN_SAMPLES = _env_int(
    "HARNESS_PLAYBOOK_EVIDENCE_MEDIAN_MIN_SAMPLES", 5
)

# ---------------------------------------------------------- runner
# Activity gate: if last-24h activity count is below this, the daily
# reflection skips (spec §5.2).
MIN_ACTIVITY_DEFAULT = _env_int("HARNESS_PLAYBOOK_MIN_ACTIVITY", 3)

# Max ops per `coord_propose_playbook_changes` call (spec §7.1).
COACH_PROPOSAL_OPS_CAP = _env_int("HARNESS_PLAYBOOK_COACH_PROPOSAL_OPS_CAP", 5)

# Bootstrap retry cap. After this many consecutive failures, set
# `playbook_bootstrap_blocked` (spec §4.4 / G1).
BOOTSTRAP_MAX_RETRIES = _env_int("HARNESS_PLAYBOOK_BOOTSTRAP_MAX_RETRIES", 3)

# Near-duplicate detection threshold — Jaccard similarity over
# lowercased word-tokens with stopwords stripped (spec §5.6 N5).
DUPLICATE_JACCARD_THRESHOLD = _env_float(
    "HARNESS_PLAYBOOK_DUPLICATE_JACCARD_THRESHOLD", 0.7
)

# Render budget for the `## Orchestration playbook` section in agent
# system prompts (spec §6.2). Over budget → drop "Uncertain" bucket.
RENDER_MAX_BYTES = _env_int("HARNESS_PLAYBOOK_RENDER_MAX_BYTES", 8_000)

# ---------------------------------------------------------- scheduler
# Background loop tick. 0 disables the scheduler entirely (manual runs
# still work via /api/playbook/run + /api/playbook/bootstrap).
SCHEDULER_TICK_SECONDS = _env_int("HARNESS_PLAYBOOK_SCHEDULER_TICK_SECONDS", 300)

# Default daily-run hour in UTC. Chosen to land before Compass's 09:00
# so they don't compete for the same Anthropic plan-block window.
RUN_HOUR_UTC_DEFAULT = _env_int("HARNESS_PLAYBOOK_RUN_HOUR_UTC", 4)

# runs.jsonl retention — trim to last N lines on each write (spec §3.3).
RUNS_RETENTION_DEFAULT = _env_int("HARNESS_PLAYBOOK_RUNS_RETENTION", 90)

# ---------------------------------------------------------- LLM
# Default model alias for both bootstrap and reflection runs (spec §11).
# Sonnet — capable enough for lattice reasoning over evidence bundle,
# cheap enough that a daily run is pennies. Resolved through
# `models_catalog.resolve_model_alias` at call time so a future
# Sonnet bump is a single-edit change in the catalog.
LLM_MODEL_DEFAULT_ALIAS = "latest_sonnet"
LLM_EFFORT = "medium"

# Codex fallback: same shape as Compass — the cheap mini tier (spec §11.3).
LLM_FALLBACK_MODEL_ALIAS = "latest_mini"
LLM_FALLBACK_EFFORT = "medium"
LLM_FALLBACK_ENABLED = _env_bool("HARNESS_PLAYBOOK_FALLBACK_ENABLED", True)

# ---------------------------------------------------- team_config keys
# Idempotency / state flags (spec §3.4).

PLAYBOOK_BOOTSTRAP_DONE_KEY = "playbook_bootstrap_done"
PLAYBOOK_BOOTSTRAP_RETRIES_KEY = "playbook_bootstrap_retries"
PLAYBOOK_BOOTSTRAP_BLOCKED_KEY = "playbook_bootstrap_blocked"
PLAYBOOK_RESET_AT_KEY = "playbook_reset_at"
PLAYBOOK_LAST_RUN_AT_KEY = "playbook_last_run_at"
PLAYBOOK_DISABLED_KEY = "playbook_disabled"


__all__ = [
    "PLAYBOOK_SCHEMA_VERSION",
    "BOOTSTRAP_WEIGHT",
    "COACH_CREATION_WEIGHT",
    "ADJUST_DELTA_CAP",
    "SOFT_STATEMENT_CAP",
    "HARD_STATEMENT_CAP",
    "SETTLE_THRESHOLD",
    "STALE_THRESHOLD",
    "SETTLE_STABLE_DAYS",
    "STALE_STABLE_DAYS",
    "STALE_UNUSED_DAYS",
    "EVIDENCE_BUNDLE_TARGET_BYTES",
    "EVIDENCE_BUNDLE_MAX_BYTES",
    "EVIDENCE_MEDIAN_WINDOW_DAYS",
    "EVIDENCE_MEDIAN_MIN_SAMPLES",
    "MIN_ACTIVITY_DEFAULT",
    "COACH_PROPOSAL_OPS_CAP",
    "BOOTSTRAP_MAX_RETRIES",
    "DUPLICATE_JACCARD_THRESHOLD",
    "RENDER_MAX_BYTES",
    "SCHEDULER_TICK_SECONDS",
    "RUN_HOUR_UTC_DEFAULT",
    "RUNS_RETENTION_DEFAULT",
    "LLM_MODEL_DEFAULT_ALIAS",
    "LLM_EFFORT",
    "LLM_FALLBACK_MODEL_ALIAS",
    "LLM_FALLBACK_EFFORT",
    "LLM_FALLBACK_ENABLED",
    "PLAYBOOK_BOOTSTRAP_DONE_KEY",
    "PLAYBOOK_BOOTSTRAP_RETRIES_KEY",
    "PLAYBOOK_BOOTSTRAP_BLOCKED_KEY",
    "PLAYBOOK_RESET_AT_KEY",
    "PLAYBOOK_LAST_RUN_AT_KEY",
    "PLAYBOOK_DISABLED_KEY",
]

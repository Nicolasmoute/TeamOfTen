"""Compass configuration — caps, thresholds, schema version, env knobs.

All values are spec-derived (Docs/compass-specs.md §9.1). Where the
harness expects to override per-deploy, an env var is honored;
otherwise the constant is the source of truth.

Read this module directly:
    from server.compass import config
    if len(active_regions) > config.REGION_SOFT_CAP: ...

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


# Schema version stamped on every state file. Bump only when a
# migration script lands under server/compass/migrations/.
COMPASS_SCHEMA_VERSION = "0.2"

# ---------------------------------------------------------------- caps
# Statement-count caps. The lattice is meant to converge — too many
# statements means compass is noise, not signal.
STMT_SOFT_CAP = _env_int("HARNESS_COMPASS_STMT_SOFT_CAP", 50)
STMT_HARD_CAP = _env_int("HARNESS_COMPASS_STMT_HARD_CAP", 70)

# Region taxonomy caps. Above SOFT, compass auto-merges on the next
# run with no human approval (spec §3.3, §10.9).
REGION_SOFT_CAP = _env_int("HARNESS_COMPASS_REGION_SOFT_CAP", 15)
REGION_HARD_CAP = _env_int("HARNESS_COMPASS_REGION_HARD_CAP", 20)

# ------------------------------------------------------ settle thresholds
# Weight crossing these bounds makes a statement settle-eligible (spec
# §1.2, §3.4). The settle itself is human-confirmed; never automatic.
SETTLED_YES = _env_float("HARNESS_COMPASS_SETTLED_YES", 0.85)
SETTLED_NO = _env_float("HARNESS_COMPASS_SETTLED_NO", 0.15)

# ------------------------------------------------------- stale detection
# A statement is stale if it sits in the unsettled middle (spec §3.5)
# for STALE_MIN_RUNS runs without cumulative absolute movement of
# STALE_MAX_MOVEMENT. The triage lets the human retire / keep / reformulate.
STALE_MIN_RUNS = _env_int("HARNESS_COMPASS_STALE_MIN_RUNS", 4)
STALE_MAX_MOVEMENT = _env_float("HARNESS_COMPASS_STALE_MAX_MOVEMENT", 0.10)
STALE_WEIGHT_BAND_LOW = _env_float("HARNESS_COMPASS_STALE_BAND_LOW", 0.35)
STALE_WEIGHT_BAND_HIGH = _env_float("HARNESS_COMPASS_STALE_BAND_HIGH", 0.65)

# Pending settle/stale/dupe proposals expire after N runs of being
# ignored (spec §10.19) — clear flag, allow re-trigger.
PROPOSAL_EXPIRY_RUNS = _env_int("HARNESS_COMPASS_PROPOSAL_EXPIRY_RUNS", 5)

# --------------------------------------------------- question generation
QUESTIONS_PER_DAILY_RUN = _env_int("HARNESS_COMPASS_QUESTIONS_DAILY", 3)
QUESTIONS_PER_BOOTSTRAP_RUN = _env_int("HARNESS_COMPASS_QUESTIONS_BOOTSTRAP", 5)

# Q&A session limits (spec §12.2).
QA_WARN_AFTER = _env_int("HARNESS_COMPASS_QA_WARN_AFTER", 20)
QA_HARD_CAP = _env_int("HARNESS_COMPASS_QA_HARD_CAP", 50)

# ----------------------------------------------------------- update bounds
# Passive digests apply smaller deltas than answer-driven digests
# (spec §3.2 vs §3.5). The runner clamps proposed deltas to these.
PASSIVE_DELTA_MAX = _env_float("HARNESS_COMPASS_PASSIVE_DELTA_MAX", 0.15)
ANSWER_DELTA_MAX = _env_float("HARNESS_COMPASS_ANSWER_DELTA_MAX", 0.50)

# ------------------------------------------------------------- audits
# Every Nth audit, compass reviews recent verdicts and asks a meta-
# question if a region drifted (spec §5.4). 0 disables.
AUDIT_ROLLUP_INTERVAL = _env_int("HARNESS_COMPASS_AUDIT_ROLLUP", 5)

# ------------------------------------------------------------ presence
# Human-reachable window. If no human signal in this many hours, daily
# runs skip and post a reminder (spec §2.2).
HUMAN_PRESENCE_WINDOW_HOURS = _env_int("HARNESS_COMPASS_PRESENCE_HOURS", 24)

# ---------------------------------------------------------- scheduler
# Background loop tick. Polls all projects with compass_enabled and
# fires daily runs when due. 0 disables the scheduler entirely
# (on-demand runs still work via /api/compass/run).
SCHEDULER_TICK_SECONDS = _env_int("HARNESS_COMPASS_SCHEDULER_TICK", 300)

# Default daily-run hour in UTC. Compass runs once per project per
# UTC day, on or after this hour. The human can trigger on_demand
# at any time.
DAILY_RUN_HOUR_UTC = _env_int("HARNESS_COMPASS_DAILY_HOUR_UTC", 9)

# ----------------------------------------------------------- LLM
# Default token budgets per call type. Generous; the SDK's
# `query()` honors the model's own max but truncates if the prompt
# blows past these.
LLM_MAX_TOKENS_DEFAULT = _env_int("HARNESS_COMPASS_LLM_MAX_TOKENS", 1500)
LLM_MAX_TOKENS_BRIEFING = _env_int("HARNESS_COMPASS_LLM_MAX_TOKENS_BRIEFING", 2000)
LLM_MAX_TOKENS_AUDIT = _env_int("HARNESS_COMPASS_LLM_MAX_TOKENS_AUDIT", 1200)

# Override the model used for compass calls. Falls through to the
# Coach default model in team_config when unset.
LLM_MODEL_OVERRIDE = os.environ.get("HARNESS_COMPASS_MODEL", "").strip() or None

# ---------------------------------------------------- CLAUDE.md markers
# Marker pair delimiting Compass's managed block in the project
# CLAUDE.md (spec §3.10). Anything between the markers is rewritten
# on every run; everything else is preserved.
CLAUDE_MD_START_MARKER = "<!-- compass:start -->"
CLAUDE_MD_END_MARKER = "<!-- compass:end -->"


# ------------------------------------------------------ feature flag key
# Per-project enable flag in team_config. Compass is opt-in: this row
# defaults to false (i.e. absent), and the human flips it via the
# dashboard. MCP tools and the scheduler both consult it.
def enabled_key(project_id: str) -> str:
    return f"compass_enabled_{project_id}"


# Last-run timestamp keyed per project. The scheduler reads this
# before firing a daily run. ISO 8601 UTC string in team_config.
def last_run_key(project_id: str) -> str:
    return f"compass_last_run_{project_id}"


# Bootstrap-completed flag — set once after the first successful
# bootstrap run. Distinguishes "freshly enabled, never bootstrapped"
# from "enabled and running daily" so the scheduler can pick the
# right mode on activation.
def bootstrapped_key(project_id: str) -> str:
    return f"compass_bootstrapped_{project_id}"


# Heartbeat key — UI sets this on each /api/compass/heartbeat hit so
# presence detection has a hook beyond the messages table.
def heartbeat_key(project_id: str) -> str:
    return f"compass_heartbeat_{project_id}"


__all__ = [
    "COMPASS_SCHEMA_VERSION",
    "STMT_SOFT_CAP",
    "STMT_HARD_CAP",
    "REGION_SOFT_CAP",
    "REGION_HARD_CAP",
    "SETTLED_YES",
    "SETTLED_NO",
    "STALE_MIN_RUNS",
    "STALE_MAX_MOVEMENT",
    "STALE_WEIGHT_BAND_LOW",
    "STALE_WEIGHT_BAND_HIGH",
    "PROPOSAL_EXPIRY_RUNS",
    "QUESTIONS_PER_DAILY_RUN",
    "QUESTIONS_PER_BOOTSTRAP_RUN",
    "QA_WARN_AFTER",
    "QA_HARD_CAP",
    "PASSIVE_DELTA_MAX",
    "ANSWER_DELTA_MAX",
    "AUDIT_ROLLUP_INTERVAL",
    "HUMAN_PRESENCE_WINDOW_HOURS",
    "SCHEDULER_TICK_SECONDS",
    "DAILY_RUN_HOUR_UTC",
    "LLM_MAX_TOKENS_DEFAULT",
    "LLM_MAX_TOKENS_BRIEFING",
    "LLM_MAX_TOKENS_AUDIT",
    "LLM_MODEL_OVERRIDE",
    "CLAUDE_MD_START_MARKER",
    "CLAUDE_MD_END_MARKER",
    "enabled_key",
    "last_run_key",
    "bootstrapped_key",
    "heartbeat_key",
]

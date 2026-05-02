"""Single source of truth for model ids the harness will accept.

Pulled out of `server.main` so `server.tools` (and the per-Player
`coord_set_player_model` tool) can validate without a lazy import
cycle. Both `main` and `tools` import from here.

Two id forms coexist intentionally:

1. **Tier aliases** (`latest_opus`, `latest_sonnet`, `latest_haiku`,
   `latest_gpt`, `latest_mini`) — what Coach's `MODEL_GUIDANCE` and
   `coord_set_player_model` tool description reference. The whole
   point is durability across model bumps: when Anthropic ships
   Sonnet 4.7, only `_ALIAS_TO_CONCRETE` here changes; Coach's
   prompts and any stored `agent_project_roles.model_override` rows
   keep working without rewrites. Aliases are resolved to concrete
   ids by `resolve_model_alias` at spawn time in `run_agent`.

2. **Concrete ids** (`claude-opus-4-7`, `gpt-5.5`, …) — what the
   SDK actually consumes, what the UI gear popover offers as power-
   user options, and what shows up in the turns ledger. Still
   accepted by the tool for the case where someone explicitly wants
   a specific version (testing, regressions).

Every concrete id below is also reflected in the UI dropdowns
(`MODEL_OPTIONS` / `CODEX_MODEL_OPTIONS` in [server/static/app.js]).
Keep them in sync — a model id missing from the UI list shows up as
"unknown model" in the gear popover even if the server accepts it.
"""

from __future__ import annotations


# Tier aliases — the durable, version-free identifiers Coach uses.
# Update the right-hand side when a new model in that tier ships;
# everything else (Coach's prompt, stored overrides, role defaults
# below) stays the same.
_ALIAS_TO_CONCRETE = {
    "latest_opus":   "claude-opus-4-7",
    "latest_sonnet": "claude-sonnet-4-6",
    "latest_haiku":  "claude-haiku-4-5-20251001",
    "latest_gpt":    "gpt-5.5",
    "latest_mini":   "gpt-5.4-mini",
}

# Reverse map for the UI / API hint endpoint, so the human can see
# "claude-opus-4-7 (latest_opus)" rather than two unrelated rows.
_CONCRETE_TO_ALIAS = {v: k for k, v in _ALIAS_TO_CONCRETE.items()}

# Per-alias runtime — used by `coord_set_player_model` to validate
# that Coach isn't pinning a Claude alias on a Codex-runtime player.
_ALIAS_RUNTIME = {
    "latest_opus":   "claude",
    "latest_sonnet": "claude",
    "latest_haiku":  "claude",
    "latest_gpt":    "codex",
    "latest_mini":   "codex",
}


def resolve_model_alias(model_id: str) -> str:
    """Map a tier alias to its concrete model id; pass concrete ids
    through unchanged. Empty string → empty string ("SDK default").

    Stable across model bumps: when `latest_sonnet` flips from
    `claude-sonnet-4-6` to `claude-sonnet-4-7`, callers passing
    `latest_sonnet` automatically pick up the new id without a
    DB migration or prompt rewrite."""
    if not model_id:
        return ""
    return _ALIAS_TO_CONCRETE.get(model_id, model_id)


# Per-role default model — used when neither a per-pane override nor a
# Coach-set per-(slot, project) override is present.
#
# Stored as tier aliases (resolved at spawn time) so a new Sonnet /
# Opus version automatically becomes the team default without a code
# bump. Concrete-id role defaults still work if someone writes them
# directly to `team_config` via `PUT /api/team/models`; that wins.
_ROLE_MODEL_DEFAULTS = {
    "coach": "latest_opus",
    "players": "latest_sonnet",
}

# Codex equivalents.
#   - Coach defaults to `latest_gpt`: the Opus-equivalent top-tier
#     model. Mirrors the Claude side (Coach=Opus, Players=Sonnet) so
#     a fresh Codex Coach has a concrete model from first spawn —
#     same cost ratio as Claude Coach on Opus, which is the existing
#     accepted default. Override in the Settings drawer if you want
#     Coach on a cheaper tier.
#   - Players default to `latest_mini`: the Sonnet-equivalent
#     mini-tier model. When Coach flips a Player to Codex (Claude
#     rate limits, frequent compactions), this is the right default.
#     The "use the top model only for heavy work" rule lives in
#     MODEL_GUIDANCE and Coach uses `coord_set_player_model` to
#     escalate per Player.
_ROLE_CODEX_MODEL_DEFAULTS = {
    "coach": "latest_gpt",
    "players": "latest_mini",
}

# Per-role default reasoning effort tier (1..4 → low / medium / high / max).
# Medium for both Coach and Players — the Sonnet/Opus pairing benefits from
# a small thinking budget on most turns; the human flips to low/high in the
# pane gear popover or via `coord_set_player_effort` when a specific Player
# needs a different tier. Used by `agents.run_agent` after per-pane and
# Coach-set overrides resolve to None.
_ROLE_EFFORT_DEFAULTS = {
    "coach": 2,
    "players": 2,
}

# Per-role default plan-mode flag. False for both — plan mode is opt-in
# per turn (pane toggle) or per Player (`coord_set_player_plan_mode`).
# Kept as a declarative table for symmetry with the model and effort
# defaults; `agents.run_agent` consults it via `role_default_plan_mode`.
_ROLE_PLAN_MODE_DEFAULTS = {
    "coach": False,
    "players": False,
}


def _role_for(agent_id: str) -> str:
    """Map an agent slot id to its role bucket. `coach` → 'coach',
    `p1..p10` (and any future extension) → 'players'."""
    return "coach" if agent_id == "coach" else "players"


def role_default_model(agent_id: str, runtime_name: str = "claude") -> str:
    """Return the role-level default model alias for `agent_id` under
    the given runtime. Empty string when no default is set (currently
    only Codex Coach). Resolved to a concrete id by
    `resolve_model_alias` at spawn time."""
    table = (
        _ROLE_CODEX_MODEL_DEFAULTS if runtime_name == "codex"
        else _ROLE_MODEL_DEFAULTS
    )
    return table.get(_role_for(agent_id), "")


def role_default_effort(agent_id: str) -> int:
    """Return the role-level default reasoning-effort tier (1..4)."""
    return _ROLE_EFFORT_DEFAULTS[_role_for(agent_id)]


def role_default_plan_mode(agent_id: str) -> bool:
    """Return the role-level default plan-mode flag."""
    return _ROLE_PLAN_MODE_DEFAULTS[_role_for(agent_id)]

# Allowlist for Claude. Empty string means "fall through to SDK
# default" — the gear popover models that as the "(default)" option.
# Aliases included so `coord_set_player_model` can validate either
# form against the same set; the spawn-time resolver maps alias →
# concrete before the value reaches the SDK.
_CLAUDE_CONCRETE_IDS = {
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
}
_CLAUDE_ALIASES = {
    a for a, r in _ALIAS_RUNTIME.items() if r == "claude"
}
_CLAUDE_MODEL_WHITELIST = {""} | _CLAUDE_CONCRETE_IDS | _CLAUDE_ALIASES

_CODEX_CONCRETE_IDS = {
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.3-codex",
    "gpt-5.2-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex",
    "gpt-5.1-codex-mini",
    "gpt-5-codex",
}
_CODEX_ALIASES = {
    a for a, r in _ALIAS_RUNTIME.items() if r == "codex"
}
_CODEX_MODEL_WHITELIST = {""} | _CODEX_CONCRETE_IDS | _CODEX_ALIASES

# Union for callers that don't care about the runtime split (rare —
# `coord_set_player_model` does the runtime-aware split).
_MODEL_WHITELIST = _CLAUDE_MODEL_WHITELIST | _CODEX_MODEL_WHITELIST


def model_is_claude(model_id: str) -> bool:
    """True when `model_id` is on the Claude allowlist (excluding empty).

    Used at spawn time in [server/agents.py:_model_fits_runtime] to
    drop a stored Coach override that no longer matches the player's
    current runtime. Positive enumeration (rather than a `claude-*`
    prefix heuristic) so a future Anthropic id without that prefix
    isn't silently misclassified. Accepts both aliases and concrete
    ids — the lookup happens against `_CLAUDE_MODEL_WHITELIST` which
    contains both forms."""
    return bool(model_id) and model_id in _CLAUDE_MODEL_WHITELIST


def model_is_codex(model_id: str) -> bool:
    """True when `model_id` is on the Codex allowlist (excluding empty).
    Accepts both aliases and concrete ids."""
    return bool(model_id) and model_id in _CODEX_MODEL_WHITELIST


def role_defaults_concrete() -> dict[str, str]:
    """Resolve the alias-keyed `_ROLE_MODEL_DEFAULTS` to concrete ids.

    Used by `/api/team/models` to populate `suggested` so the UI hint
    ("suggested: claude-opus-4-7") matches the dropdown option label.
    The dict itself stays alias-keyed so a model bump only touches
    `_ALIAS_TO_CONCRETE`."""
    return {role: resolve_model_alias(v) for role, v in _ROLE_MODEL_DEFAULTS.items()}


def role_codex_defaults_concrete() -> dict[str, str]:
    """Codex equivalent of `role_defaults_concrete`."""
    return {
        role: resolve_model_alias(v)
        for role, v in _ROLE_CODEX_MODEL_DEFAULTS.items()
    }


# What the UI dropdown / API `available` field exposes — concrete ids
# only, sorted. Tier aliases are an LLM-facing convenience for Coach's
# tool; the human UI prefers explicit versions.
_CLAUDE_AVAILABLE = sorted(_CLAUDE_CONCRETE_IDS)
_CODEX_AVAILABLE = sorted(_CODEX_CONCRETE_IDS)


# Coach guidance the harness injects into Coach's system prompt at
# spawn time. Uses tier ALIASES (latest_opus, latest_sonnet, …) not
# concrete version numbers — this prompt has to stay durable across
# model bumps. When a new top-tier Claude or GPT ships, only the
# `_ALIAS_TO_CONCRETE` map above changes; this text stays as-is.
#
# Thrust:
#   - "model change is the EXCEPTION" — Coach should avoid changing
#     Player models for routine work.
#   - Cost-tier ordering is explicit so Coach has a mental model for
#     "this task warrants opus" vs "this task warrants haiku".
#   - Codex is positioned as a fallback when Claude rate-limits, with
#     `latest_mini` as the Sonnet equivalent and `latest_gpt` as the
#     Opus equivalent.
#
# Keep this short — every line costs prompt tokens on every Coach turn.
MODEL_GUIDANCE = """\
## Model selection policy (for `coord_set_player_model`)

Changing a Player's model is the EXCEPTION, not the rule. The team
defaults below are sized to be the right answer ~95% of the time.
Only set a per-Player override when you have a concrete reason — and
clear it (`coord_set_player_model(p, "")`) when that reason is gone.

Use TIER ALIASES, not version numbers. The harness resolves them to
the current concrete model at spawn time — your override stays
correct when a new model ships.

**Claude tier (default runtime)**

- `latest_sonnet` — **default for Players**. Use it for everything
  unless you have a specific reason not to.
- `latest_opus` — Coach's own model. For Players, use SPARINGLY:
  hard reasoning, complex architecture decisions, debugging across
  many files. Several × more expensive per token than Sonnet, so a
  Player parked on Opus burns the daily team cap fast.
- `latest_haiku` — only for the SIMPLEST tasks: pattern matching,
  formatting, single-file mechanical edits. Fast and cheap, but
  loses on multi-step reasoning.

**Codex tier (OpenAI runtime)**

Use Codex when a Player is blocked on Claude rate limits or has been
hitting `/compact` repeatedly (their Claude allowance is gone for
the day). Flip them with
`coord_set_player_runtime(player_id, runtime='codex')` FIRST — the
model tool validates against the Player's current runtime, so a
Codex model on a Claude-runtime Player is rejected until the
runtime flip lands. Mirror the Claude tier ordering:

- `latest_mini` — **default for Players** on Codex. Sonnet equivalent.
- `latest_gpt` — top-tier OpenAI. Opus equivalent. Use only for
  heavy reasoning work, same as Opus.

When in doubt, leave the model unset (cleared override) and let the
team default kick in.
"""

"""Playbook LLM prompts — bootstrap extraction + daily reflection.

Both prompts ask for strict JSON output; the runner / bootstrap modules
parse with `parse_json_safe` (tolerant first-balanced-array / -object
extraction).

Spec references:
  - Bootstrap: §4.3 (extracts seed statements from the prose corpus
    in `server/templates/app_dev_playbook.md`).
  - Reflection: §5.5 (proposes adjustments / creations / merges +
    `relevant_ids` after reading the evidence bundle).

Both prompts are pinned to Sonnet medium (config.LLM_MODEL_DEFAULT_ALIAS,
config.LLM_EFFORT) per the team-wide policy. Codex fallback (mini /
medium) kicks in only when Claude raises or returns is_error=True.
"""

from __future__ import annotations


# ---------------------------------------------------------------- bootstrap


BOOTSTRAP_SYSTEM = """You extract orchestration patterns from a prose playbook.

The output is consumed by an automated engine, not a human. Return ONLY a JSON list. No markdown, no prose, no headers, no explanation. The first character of your reply MUST be `[`.
"""


BOOTSTRAP_USER_TEMPLATE = """Below is a prose playbook on coordinating a multi-agent team. Extract every distinct, actionable orchestration pattern as a single conceptual statement.

Brevity (load-bearing — these statements are injected into every agent's system prompt on every turn):
- Hard cap: 160 characters. Anything longer is rejected.
- One line, imperative form. "When X -> do Y" or "X needs Y." No enumerated sub-items, no parenthetical clauses listing what-goes-in.
- The WEIGHT carries confidence; the text just needs to trigger recall. Detail / rationale belongs in the prose corpus, not the lattice statement.
- Aim for ~120 chars typical, 160 only when the trigger genuinely needs context.

Constraints:
- Statement must be conceptual and runtime-agnostic — no specific Player slot ids (p1..p10), no specific tool names from the harness (`coord_*`, `compass_*`), no specific tech-stack references.
- Each statement must be observable in action — Coach should be able to look at a day's events and tell whether the pattern fired and whether it paid off.
- Each statement's negation must be meaningful (a confident NO would also be useful direction).
- Skip anything that is a hardcoded harness rule (process plumbing, file paths, tool signatures) — the playbook is for learned strategy, not procedural plumbing.

Return a JSON list of `{{"text": str, "suggested_weight": float}}` objects.

Use:
- 0.85 for patterns the prose explicitly calls out as load-bearing.
- 0.75 for default.
- 0.65 for patterns the prose hedges on.

Skip the prose. Return only the JSON list. The first character must be `[`.

Prose playbook:
{corpus}
"""


# ---------------------------------------------------------------- reflection


REFLECTION_SYSTEM = """You review yesterday's team activity to update an orchestration playbook.

The output is consumed by an automated engine, not a human. Return ONLY a JSON object. No markdown, no prose, no headers, no explanation. The first character of your reply MUST be `{`.
"""


REFLECTION_USER_TEMPLATE = """The playbook is a list of weighted statements about how to coordinate a multi-agent team. Each statement has a weight in [0, 1]:
- > 0.85: validated YES (established discipline)
- 0.5: genuine uncertainty
- < 0.15: validated NO (anti-pattern)

Below is the current playbook (active statements only) and the last 24h of team activity. Your job is to (a) note which statements the day's events touched on, (b) propose changes that move weights closer to the truth based on what actually happened.

For each high-confidence statement (weight ≥ 0.85), look through the evidence for VIOLATIONS — events where the rule should have applied but didn't (e.g. a code commit shipped without an audit when the rule says "audit every code change"). Violations are evidence for downward adjustment.

For each anti-pattern statement (weight ≤ 0.15), look for evidence the anti-pattern fired anyway and produced bad outcomes (further evidence for keeping it low) OR fired and produced GOOD outcomes (counter-evidence that may justify upward adjustment).

# Current playbook
{rendered_lattice}

# Evidence bundle (last 24h)
{evidence_bundle}

# Your task

Return a JSON object with four lists:

{{
  "relevant_ids": ["pb-XXX", "pb-YYY"],
  "adjustments": [
    {{"id": "pb-XXX", "delta": 0.10, "reason": "validated by 3 clean outcomes in t-abc, t-def, t-ghi"}}
  ],
  "creations": [
    {{"text": "<one line, imperative, <=160 chars, conceptual, runtime-agnostic, observable>", "weight": 0.6, "reason": "pattern observed in 3 archived tasks t-..., t-..., t-..."}}
  ],
  "merges": [
    {{"keep_id": "pb-XXX", "drop_id": "pb-YYY", "reason": "say the same thing"}}
  ]
}}

Rules:
- relevant_ids: every statement the day's events touched on — whether or not weight changed. Used to track which patterns are actually firing.
- Each adjustment delta absolute value <= 0.25 (so a single noisy day cannot flip a stable consensus).
- Justification must reference specific task ids / event types from the evidence bundle.
- Creations should be supported by >= 3 distinct observations (instruction, not enforced — be honest).
- **Creation text is hard-capped at 160 chars; longer creations are rejected.** One line, imperative ("When X -> do Y" or "X needs Y"), no enumerated sub-items, no parenthetical lists. The WEIGHT carries confidence; the text just triggers recall. Aim for ~120 chars typical.
- Skip statements that are runtime-specific, project-specific, procedural-plumbing, or unobservable.
- Empty lists are valid — return all four as `[]` if no real signal.

Return ONLY the JSON object. The first character must be `{{`.
"""


__all__ = [
    "BOOTSTRAP_SYSTEM",
    "BOOTSTRAP_USER_TEMPLATE",
    "REFLECTION_SYSTEM",
    "REFLECTION_USER_TEMPLATE",
]

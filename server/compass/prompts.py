"""Compass prompt templates — the 13 load-bearing prompts from spec §8.

Each prompt has two parts:
  - **System prompt** — long, defines the role and the strict JSON
    output contract. The shared semantics block is prepended so every
    call starts with the same world-model framing.
  - **User prompt** — short, parameterized with the current state.

Builders below produce both parts. Treat the language carefully — the
spec calls out small word choices (e.g. "deltas", "surprise", "binding
constraint") as load-bearing. Don't paraphrase without re-reading §8.

The shared semantics block is prepended to every system prompt.

Output contracts:
  - Every prompt asks for STRICT JSON with no preamble, no fence.
  - Compass parses the response via `llm.parse_json_safe` which
    tolerates fences anyway, so the LLM disobeying the "no fence"
    rule is recoverable. We still ask for it because compliant
    output is faster + cheaper.
"""

from __future__ import annotations

import json
from typing import Any

from server.compass import config
from server.compass.store import LatticeState, Statement, TruthFact


# ----------------------------------------------------- shared semantics

SHARED_SEMANTICS = """\
WORLD-MODEL SEMANTICS:
- The model is a LATTICE OF STATEMENTS, each with a weight = P(statement is true).
- 1.0 = certain YES, 0.0 = certain NO, 0.5 = genuine ignorance.
- Confident NO is just as actionable as YES — its negation is the binding fact.
- The lattice maps the project's TERRITORY at moderate granularity. Good lattices are
  coarse enough that the LLM can infer fine specifics, fine enough that constraints
  are actionable.
- Each statement belongs to ONE region (a short tag like "pricing", "auth").
  Pick from existing regions; only invent a new region when none fits.
"""


def _system(role_block: str) -> str:
    """Standard system-prompt assembly: role-specific block first, then
    the shared semantics, then the JSON-output-contract reminder."""
    return f"{role_block}\n\n{SHARED_SEMANTICS}\n\nReturn ONLY the requested JSON. No markdown fences, no preamble, no commentary."


# ---------------------------------------------------- state serializers
# Keep state digests compact — the LLM doesn't need the full history,
# just enough for the current decision.


def _statements_brief(statements: list[Statement], *, include_archived: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in statements:
        if s.archived and not include_archived:
            continue
        out.append({
            "id": s.id,
            "text": s.text,
            "region": s.region,
            "weight": round(s.weight, 3),
            **({"archived": True, "settled_as": s.settled_as} if s.archived else {}),
        })
    return out


def _truth_brief(facts: list[TruthFact]) -> list[dict[str, Any]]:
    return [{"index": t.index, "text": t.text} for t in facts]


def _regions_brief(state: LatticeState) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for s in state.active_statements():
        counts[s.region] = counts.get(s.region, 0) + 1
    return [
        {"name": r.name, "active_count": counts.get(r.name, 0)}
        for r in state.active_regions()
    ]


def _state_payload(
    state: LatticeState,
    *,
    include_archived: bool = False,
    include_pending_questions: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "statements": _statements_brief(state.statements, include_archived=include_archived),
        "truth": _truth_brief(state.truth),
        "regions": _regions_brief(state),
    }
    if include_pending_questions:
        payload["pending_questions"] = [
            {
                "id": q.id,
                "q": q.q,
                "targets": q.targets,
                "answered": q.answer is not None,
            }
            for q in state.questions
            if not q.digested and not q.contradicted
        ]
    return payload


def _json_block(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


# ----------------------------------------------------- 8.2 passive digest


PASSIVE_DIGEST_SYSTEM = _system(f"""\
You are Compass. The human has produced signals (chat, commits, notes) — not formal
answers. Use them to passively update the lattice.

Rules:
- Adjust weight only when a signal directly bears on a statement.
  Deltas in [-{config.PASSIVE_DELTA_MAX}, +{config.PASSIVE_DELTA_MAX}] (passive updates are smaller than answer-driven ones).
- Propose new statements when signals reveal lattice gaps (max 2). Pick a region from
  the existing list if any fits; only invent a new region when truly none fits.
  Phrase so YES = the affirmative reading. Start at weight 0.5.
- Never amend truth — flag candidates only.

Output ONLY:
{{
  "updates": [{{"id": string, "delta": number, "rationale": string}}],
  "new_statements": [{{"text": string, "region": string, "rationale": string}}],
  "truth_candidates": [string],
  "summary": string
}}""")


def passive_digest_user(state: LatticeState, signals: list[dict[str, Any]]) -> str:
    """`signals` is a list of `{kind: 'chat'|'commit'|'note', ts: ..., body: ...}`."""
    return (
        "## Current state\n"
        f"{_json_block(_state_payload(state, include_pending_questions=False))}\n\n"
        "## New signals since last run\n"
        f"{_json_block(signals or [])}\n"
    )


# ----------------------------------------------- 8.3 question generation (batch)


QUESTION_BATCH_SYSTEM = _system("""\
You are Compass. Generate up to {N} questions to ask the human. Maximize information
gain across the lattice.

Question selection priorities, in order:
1. Statements with weights in 0.35–0.65 (max entropy) — biggest info gain per answer.
2. Under-populated regions (few statements relative to apparent project importance) —
   coverage gaps.
3. Contested clusters — multiple related statements all hovering near 0.5 suggest a
   structural ambiguity.

For each question: commit to a specific, falsifiable prediction. Don't repeat pending
questions. Cite which statement ids the question targets (1–3 ids).

Output ONLY:
{
  "questions": [
    {"q": string, "prediction": string, "targets": [string], "rationale": string}
  ]
}""")


def question_batch_system(n: int) -> str:
    return QUESTION_BATCH_SYSTEM.replace("{N}", str(n))


def question_batch_user(state: LatticeState, *, count: int) -> str:
    return (
        f"## Generate up to {count} questions\n\n"
        "## Current lattice state\n"
        f"{_json_block(_state_payload(state))}\n"
    )


# --------------------------------- 8.4 question generation (single, Q&A)


QUESTION_SINGLE_SYSTEM = _system("""\
You are Compass running an interactive Q&A session with the human. Pick the SINGLE
next-best question to ask. Maximize information gain given the current lattice and
the questions already asked this session.

Question selection priorities, in order:
1. Statements with weights in 0.35–0.65 (max entropy) — biggest info gain per answer.
2. Under-populated regions (few statements relative to apparent project importance).
3. Contested clusters — multiple related statements hovering near 0.5.

Commit to a specific, falsifiable prediction. Cite 1–3 target statement ids.

Output ONLY:
{
  "q": string,
  "prediction": string,
  "targets": [string],
  "rationale": string
}""")


def question_single_user(state: LatticeState, asked_in_session: list[str]) -> str:
    return (
        "## Current lattice state\n"
        f"{_json_block(_state_payload(state))}\n\n"
        "## Questions already asked this session (don't repeat)\n"
        f"{_json_block(asked_in_session)}\n"
    )


# --------------------------------------------------------- 8.5 digest answer


ANSWER_DIGEST_SYSTEM = _system(f"""\
You are Compass digesting a human's answer.

Rules:
- Estimate surprise 0–1 (0 = matches prediction, 1 = total contradiction).
- Targeted statements: delta in [-{config.ANSWER_DELTA_MAX}, +{config.ANSWER_DELTA_MAX}]. Magnitude depends on how decisively
  the answer settles the statement (clear yes/no = 0.4+, hedged = 0.1).
- Direction: support → toward 1, contradict → toward 0.
- Non-targeted statements: only adjust if directly implicated.
- Propose new statements when the answer reveals lattice gaps (max 2). Pick from
  existing regions; new region only if none fits.
- Flag truth_candidates if the answer reveals something worth promoting (human decides).

Output ONLY:
{{
  "surprise": number,
  "updates": [{{"id": string, "delta": number, "rationale": string}}],
  "new_statements": [{{"text": string, "region": string, "rationale": string}}],
  "truth_candidates": [string],
  "summary": string
}}""")


def answer_digest_user(
    state: LatticeState,
    *,
    question_text: str,
    prediction: str,
    targets: list[str],
    answer_text: str,
) -> str:
    return (
        "## Question\n"
        f"{question_text}\n\n"
        "## Compass prediction (committed before answer was seen)\n"
        f"{prediction}\n\n"
        "## Targeted statement ids\n"
        f"{_json_block(targets)}\n\n"
        "## Human's answer\n"
        f"{answer_text}\n\n"
        "## Current lattice state (for context)\n"
        f"{_json_block(_state_payload(state, include_pending_questions=False))}\n"
    )


# ------------------------------------------- 8.6 truth contradiction check


TRUTH_CHECK_SYSTEM = _system("""\
You are Compass. Check whether the human's answer contradicts any truth-protected
fact. Truth is sacred — never reweight in the face of contradiction; surface for
human review.

Output ONLY:
{
  "contradiction": boolean,
  "conflicts": [{"truth_index": number, "explanation": string}],
  "summary": string
}
Truth indices are 1-based.""")


def truth_check_user(
    truth: list[TruthFact],
    *,
    question_text: str,
    prediction: str,
    answer_text: str,
) -> str:
    return (
        "## Truth-protected facts (1-based indices)\n"
        f"{_json_block(_truth_brief(truth))}\n\n"
        "## Question\n"
        f"{question_text}\n\n"
        "## Compass prediction\n"
        f"{prediction}\n\n"
        "## Human's answer\n"
        f"{answer_text}\n"
    )


# -------------------------------------------------- 8.7 settle / stale review


SETTLE_STALE_SYSTEM = _system(f"""\
You are Compass. Surface statements for human review — you do not act unilaterally.

For SETTLE candidates (weight crossed {config.SETTLED_YES} or dropped below {config.SETTLED_NO}):
- Phrase a confirmation question. Human will: confirm (settle and archive at 0/1),
  adjust (pick a different value), or reject (keep active).

For STALE candidates (near 0.5 with no movement across many runs):
- Phrase a triage question with three paths: irrelevant (retire entirely),
  genuinely-unsettled-but-important (keep active), badly-phrased (offer a reformulation).
- Provide a reformulation candidate when phrasing seems off.

Output ONLY:
{{
  "settle": [{{"id": string, "direction": "yes"|"no", "question": string, "reasoning": string}}],
  "stale":  [{{"id": string, "question": string, "reformulation": string|null, "reasoning": string}}]
}}""")


def settle_stale_user(
    state: LatticeState,
    *,
    settle_candidates: list[Statement],
    stale_candidates: list[Statement],
) -> str:
    return (
        "## Settle candidates (weight crossed 0.85 toward YES or below 0.15 toward NO)\n"
        f"{_json_block(_statements_brief(settle_candidates))}\n\n"
        "## Stale candidates (long-running 0.35–0.65 with no movement)\n"
        f"{_json_block(_statements_brief(stale_candidates))}\n\n"
        "## Current regions (for context when phrasing reformulations)\n"
        f"{_json_block(_regions_brief(state))}\n"
    )


# ------------------------------------------------ 8.8 duplicate detection


DUPLICATE_SYSTEM = _system("""\
You are Compass. Detect statements that are near-duplicates of each other and should
be MERGED into a single sharper statement.

Two statements are duplicates if they would be falsified by the same evidence —
they're claiming the same thing in different words. Distinct angles are NOT duplicates
even if topically related.

For each duplicate cluster:
- List the redundant statement ids
- Propose a merged_text that captures the claim crisply
- Propose a merged_weight (a sensible blend of the originals' weights — usually closer
  to the higher-confidence one if they don't conflict)
- Choose the region — usually the most common region across the cluster

Output ONLY:
{"duplicates": [{"ids": [string,...], "merged_text": string, "merged_weight": number, "region": string, "reasoning": string}]}

If no duplicates, return {"duplicates": []}.""")


def duplicate_user(state: LatticeState) -> str:
    return (
        "## Active lattice (duplicate detection target)\n"
        f"{_json_block(_statements_brief(state.active_statements()))}\n"
    )


# ------------------------------------------------ 8.9 region auto-merge


REGION_MERGE_SYSTEM_TEMPLATE = """\
You are Compass. The region taxonomy has too many regions ({N} > {SOFT_CAP}).
MERGE close regions into broader ones.

Region merging is COMPASS HOUSEKEEPING — autonomous, no human approval.
All statements (active and archived) using a deprecated region get re-tagged.

Identify region pairs/clusters that are conceptually close (e.g., "billing" and
"payments", "auth" and "authentication"). Pick one to keep (usually the broader/clearer
name) and merge the other(s) into it.

Bring total active region count to ≤ {TARGET}.

Output ONLY:
{{"merges": [{{"from": [string,...], "to": string, "reasoning": string}}]}}"""


def region_merge_system(active_count: int, target: int) -> str:
    role = REGION_MERGE_SYSTEM_TEMPLATE.format(
        N=active_count, SOFT_CAP=config.REGION_SOFT_CAP, TARGET=target,
    )
    return _system(role)


def region_merge_user(state: LatticeState) -> str:
    counts: dict[str, int] = {}
    for s in state.active_statements():
        counts[s.region] = counts.get(s.region, 0) + 1
    payload = [{"name": r.name, "active_count": counts.get(r.name, 0)} for r in state.active_regions()]
    return (
        "## Current active regions with statement counts\n"
        f"{_json_block(payload)}\n"
    )


# ---------------------------------------------------------- 8.10 audit


AUDIT_SYSTEM = _system("""\
You are Compass auditing a piece of work against the lattice. Coach has submitted a
work artifact (commit, decision, worker output) and wants to know if it aligns with
current beliefs about the project.

Verdict rules:
- "aligned": work is consistent with the lattice, or touches no high-stakes statements.
- "confident_drift": work clearly contradicts at least one HIGH-CONFIDENCE statement
  (>0.8 or <0.2). You're sure something is wrong. Coach gets a direct message;
  human is NOT bothered.
- "uncertain_drift": work seems off but the relevant statements are at 0.3–0.7 — you
  can't tell if work is wrong or if the lattice is wrong. Coach proceeds cautiously;
  a question is generated for the human (with a prediction).

Be conservative — most work should come back "aligned." Only flag drift when there's
real evidence of contradiction.

Output ONLY:
{
  "verdict": "aligned" | "confident_drift" | "uncertain_drift",
  "summary": string,
  "contradicting_ids": [string],
  "message_to_coach": string,
  "question_for_human": {"q": string, "prediction": string, "targets": [string]} | null
}

If "aligned", message_to_coach is short ("OK · aligned with lattice"), question_for_human
is null. If "confident_drift", message_to_coach explains the conflict directly to coach.
If "uncertain_drift", message_to_coach tells coach you've flagged it for human review,
and question_for_human is the question to queue.""")


def audit_user(state: LatticeState, artifact: str) -> str:
    return (
        "## Active lattice + truth\n"
        f"{_json_block(_state_payload(state))}\n\n"
        "## Work artifact submitted by coach\n"
        f"{artifact}\n"
    )


# ---------------------------------------------------------- 8.11 briefing


BRIEFING_SYSTEM = _system(f"""\
You are Compass producing a daily briefing for the coach. Be terse and useful.

Sections:
1. CONFIRMED YES (>{config.SETTLED_YES - 0.05}) — binding constraints. List as-is.
2. CONFIRMED NO (<{config.SETTLED_NO + 0.05}) — surface NEGATION as binding (e.g. "s5 at 0.10 → customers
   are NOT technical").
3. LEANING (0.2–0.4 or 0.6–0.8) — working hypotheses, verify when cheap.
4. OPEN (0.4–0.6) — genuine uncertainty, no expensive commits here.
5. COVERAGE — which regions have meaningful coverage, which look thin.
6. DRIFT — recent events contradicting the lattice, or significant shifts.
7. RECOMMENDATION — one sentence, where coach should focus.

Plain markdown. No preamble.""")


def briefing_user(state: LatticeState, recent_events: dict[str, Any]) -> str:
    return (
        "## Current lattice state\n"
        f"{_json_block(_state_payload(state, include_archived=True))}\n\n"
        "## Recent run summary\n"
        f"{_json_block(recent_events)}\n"
    )


# ---------------------------------------------------- 8.12 CLAUDE.md block


CLAUDE_MD_BLOCK_SYSTEM = _system("""\
You maintain Compass's managed section in CLAUDE.md so coord and workers discover
you naturally.

TWO PARTS:

PART 1 — Static-ish paragraph (3–5 sentences) explaining:
- Compass is a side-engine that maintains a lattice of statements about the project,
  each with P(true) weight
- It's queried by the coach via compass_ask, never edited by workers
- Settled (archived) statements are facts coord can rely on
- Only the human answers questions and amends truth

PART 2 — Forward-looking briefing (5–8 lines max) titled "Where we stand · next steps":
- Compass's best guess at where the project stands (1–2 sentences)
- 2–3 concrete next-step suggestions for the coach, derived from confident-YES
  statements and lattice momentum
- 1–2 things to avoid (from confident-NO archive)
- 1 line on what's currently uncertain

Plain markdown. No fences. Use exactly these headings: "## Compass" and
"### Where we stand · next steps".""")


def claude_md_block_user(state: LatticeState) -> str:
    return (
        "## Lattice state\n"
        f"{_json_block(_state_payload(state, include_archived=True))}\n"
    )


# ----------------------------------------------- 8.13 coach query (compass_ask)


COACH_QUERY_SYSTEM = _system("""\
You are Compass. The coach is interrogating you. Answer based strictly on the lattice
and truth.

Cite statement ids and weights. Treat >0.8 as confirmed yes, <0.2 as confirmed no
(surface negation), 0.4–0.6 as genuinely uncertain. Be terse.

Plain markdown. No fences. No preamble.""")


def coach_query_user(state: LatticeState, query_text: str) -> str:
    return (
        "## Coach's query\n"
        f"{query_text}\n\n"
        "## Current lattice + truth\n"
        f"{_json_block(_state_payload(state, include_archived=True))}\n"
    )


__all__ = [
    "SHARED_SEMANTICS",
    "PASSIVE_DIGEST_SYSTEM",
    "passive_digest_user",
    "QUESTION_BATCH_SYSTEM",
    "question_batch_system",
    "question_batch_user",
    "QUESTION_SINGLE_SYSTEM",
    "question_single_user",
    "ANSWER_DIGEST_SYSTEM",
    "answer_digest_user",
    "TRUTH_CHECK_SYSTEM",
    "truth_check_user",
    "SETTLE_STALE_SYSTEM",
    "settle_stale_user",
    "DUPLICATE_SYSTEM",
    "duplicate_user",
    "REGION_MERGE_SYSTEM_TEMPLATE",
    "region_merge_system",
    "region_merge_user",
    "AUDIT_SYSTEM",
    "audit_user",
    "BRIEFING_SYSTEM",
    "briefing_user",
    "CLAUDE_MD_BLOCK_SYSTEM",
    "claude_md_block_user",
    "COACH_QUERY_SYSTEM",
    "coach_query_user",
]

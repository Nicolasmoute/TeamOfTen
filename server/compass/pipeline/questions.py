"""Question generation — batch (daily run) and single (Q&A session).

Both produce typed proposals; the runner / Q&A endpoint persist them
via `store.save_questions`. The committed prediction is mandatory
(spec §10.18) — without it the loop loses its trainable property.

If the lattice has nothing uncertain (every active statement above
SETTLED_YES or below SETTLED_NO, with no proposals pending), batch
generation may legitimately return zero questions (spec §10.7). The
LLM decides this; we don't pre-filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.compass import config, llm, prompts
from server.compass.store import LatticeState


@dataclass
class QuestionProposal:
    q: str
    prediction: str
    targets: list[str]
    rationale: str


async def generate_batch(state: LatticeState, *, count: int) -> list[QuestionProposal]:
    """Generate up to `count` questions for the daily run."""
    if count <= 0:
        return []
    res = await llm.call(
        prompts.question_batch_system(count),
        prompts.question_batch_user(state, count=count),
        max_tokens=config.LLM_MAX_TOKENS_DEFAULT,
        project_id=state.project_id,
        label="compass:questions:batch",
    )
    parsed = llm.parse_json_safe(res.text) or {}
    raw_qs = parsed.get("questions") if isinstance(parsed, dict) else parsed
    return _materialize(raw_qs, cap=count)


async def generate_single(
    state: LatticeState, *, asked_in_session: list[str]
) -> QuestionProposal | None:
    """Pick the next-best question for an interactive Q&A session.

    Returns None if the LLM returns garbage or an empty proposal —
    the Q&A overlay then ends gracefully ("nothing left to ask").
    """
    res = await llm.call(
        prompts.QUESTION_SINGLE_SYSTEM,
        prompts.question_single_user(state, asked_in_session),
        max_tokens=config.LLM_MAX_TOKENS_DEFAULT,
        project_id=state.project_id,
        label="compass:questions:single",
    )
    parsed = llm.parse_json_safe(res.text) or {}
    if not isinstance(parsed, dict):
        return None
    q = str(parsed.get("q") or "").strip()
    prediction = str(parsed.get("prediction") or "").strip()
    if not q or not prediction:
        return None
    return QuestionProposal(
        q=q,
        prediction=prediction,
        targets=_sanitize_targets(parsed.get("targets")),
        rationale=str(parsed.get("rationale") or "").strip(),
    )


def _materialize(raw: Any, *, cap: int) -> list[QuestionProposal]:
    if not isinstance(raw, list):
        return []
    out: list[QuestionProposal] = []
    for item in raw[:cap]:
        if not isinstance(item, dict):
            continue
        q = str(item.get("q") or "").strip()
        prediction = str(item.get("prediction") or "").strip()
        if not q or not prediction:
            # Spec §10.18: prediction is mandatory. Drop entries that
            # fail this so the queue never accumulates anchorless Qs.
            continue
        out.append(
            QuestionProposal(
                q=q,
                prediction=prediction,
                targets=_sanitize_targets(item.get("targets")),
                rationale=str(item.get("rationale") or "").strip(),
            )
        )
    return out


def _sanitize_targets(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for x in raw:
        if isinstance(x, str):
            sid = x.strip()
            if sid:
                out.append(sid)
    return out[:3]  # spec: 1–3 ids

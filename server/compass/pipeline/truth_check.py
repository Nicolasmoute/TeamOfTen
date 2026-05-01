"""Truth contradiction subroutine.

Called from §3.1 (digest answered questions) and from the Q&A
session's submit endpoint, BEFORE any digest reweighting. If the
human's answer contradicts a truth-protected fact, the digest is
halted and the conflict surfaces to the human via the truth-conflict
modal (spec §3.7).

If the truth list is empty, returns no contradiction without
calling the LLM — saves a token round-trip on fresh projects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from server.compass import config, llm, prompts
from server.compass.store import TruthFact


@dataclass
class TruthCheckResult:
    contradiction: bool
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


async def check(
    truth: list[TruthFact],
    *,
    question_text: str,
    prediction: str,
    answer_text: str,
    project_id: str | None = None,
) -> TruthCheckResult:
    if not truth:
        return TruthCheckResult(contradiction=False, conflicts=[], summary="")
    res = await llm.call(
        prompts.TRUTH_CHECK_SYSTEM,
        prompts.truth_check_user(
            truth,
            question_text=question_text,
            prediction=prediction,
            answer_text=answer_text,
        ),
        max_tokens=config.LLM_MAX_TOKENS_DEFAULT,
        project_id=project_id,
        label="compass:truth_check",
    )
    parsed = llm.parse_json_safe(res.text) or {}
    if not isinstance(parsed, dict):
        return TruthCheckResult(contradiction=False, conflicts=[], summary="")
    contradiction = bool(parsed.get("contradiction"))
    conflicts_raw = parsed.get("conflicts") or []
    conflicts: list[dict[str, Any]] = []
    if isinstance(conflicts_raw, list):
        for item in conflicts_raw:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("truth_index") or 0)
            except (TypeError, ValueError):
                continue
            if idx < 1:
                continue
            conflicts.append({
                "truth_index": idx,
                "explanation": str(item.get("explanation") or "").strip(),
            })
    # Defensive: if the LLM said `contradiction=True` but provided no
    # conflicts, treat as no contradiction. The conflicts list is the
    # actionable signal — without it the modal has nothing to show.
    if contradiction and not conflicts:
        contradiction = False
    return TruthCheckResult(
        contradiction=contradiction,
        conflicts=conflicts,
        summary=str(parsed.get("summary") or "").strip(),
    )

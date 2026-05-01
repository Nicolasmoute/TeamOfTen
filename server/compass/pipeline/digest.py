"""Digest stages — passive (signals) and answer (Q&A response).

Both produce the same shape: a list of weight updates, a list of new
statement proposals, a list of truth candidates the human might want
to promote, and a one-sentence summary. Runner applies via mutate.

Passive deltas are clamped to `±config.PASSIVE_DELTA_MAX` (default
0.15); answer deltas to `±config.ANSWER_DELTA_MAX` (default 0.5).
The clamp is enforced at *apply* time in `mutate.apply_statement_updates`,
not here — the LLM might propose 0.6 even after we ask for 0.5,
and the runner is the right place to discipline that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from server.compass import config, llm, prompts
from server.compass.store import LatticeState


@dataclass
class DigestResult:
    surprise: float | None  # only set on answer digest
    updates: list[dict[str, Any]] = field(default_factory=list)
    new_statements: list[dict[str, Any]] = field(default_factory=list)
    truth_candidates: list[str] = field(default_factory=list)
    summary: str = ""

    def summary_dict(self) -> dict[str, Any]:
        """Compact form for run-log payload."""
        return {
            "updates": len(self.updates),
            "new_statements": len(self.new_statements),
            "truth_candidates": len(self.truth_candidates),
            "summary": self.summary,
            **({"surprise": self.surprise} if self.surprise is not None else {}),
        }


async def passive(
    state: LatticeState,
    signals: list[dict[str, Any]],
) -> DigestResult:
    """Passive digest from human signals (chat / commit / note rows).

    `signals` is `[{kind, ts, body}, ...]`. If empty, we still call
    the LLM with an empty list — spec §10.20 says a no-signal run
    is not a no-op (the lattice can drift on age alone). The LLM is
    expected to return zero updates in that case; we don't short-
    circuit because the humanness of the prompt matters for the
    self-consistency test.
    """
    res = await llm.call(
        prompts.PASSIVE_DIGEST_SYSTEM,
        prompts.passive_digest_user(state, signals),
        max_tokens=config.LLM_MAX_TOKENS_DEFAULT,
        project_id=state.project_id,
        label="compass:passive",
    )
    parsed = llm.parse_json_safe(res.text) or {}
    return DigestResult(
        surprise=None,
        updates=_sanitize_updates(parsed.get("updates")),
        new_statements=_sanitize_new_statements(parsed.get("new_statements")),
        truth_candidates=_sanitize_str_list(parsed.get("truth_candidates")),
        summary=str(parsed.get("summary") or ""),
    )


async def answer(
    state: LatticeState,
    *,
    question_text: str,
    prediction: str,
    targets: list[str],
    answer_text: str,
) -> DigestResult:
    """Digest one human answer. Surprise is captured for analytics.

    The runner is responsible for the truth-check BEFORE calling this
    (spec §3.1 step 1: truth-check then digest). Don't combine the
    two here — separation lets the contradiction modal surface
    cleanly without partial state changes.
    """
    res = await llm.call(
        prompts.ANSWER_DIGEST_SYSTEM,
        prompts.answer_digest_user(
            state,
            question_text=question_text,
            prediction=prediction,
            targets=targets,
            answer_text=answer_text,
        ),
        max_tokens=config.LLM_MAX_TOKENS_DEFAULT,
        project_id=state.project_id,
        label="compass:answer",
    )
    parsed = llm.parse_json_safe(res.text) or {}
    surprise_raw = parsed.get("surprise")
    try:
        surprise = float(surprise_raw) if surprise_raw is not None else None
    except (TypeError, ValueError):
        surprise = None
    return DigestResult(
        surprise=surprise,
        updates=_sanitize_updates(parsed.get("updates")),
        new_statements=_sanitize_new_statements(parsed.get("new_statements")),
        truth_candidates=_sanitize_str_list(parsed.get("truth_candidates")),
        summary=str(parsed.get("summary") or ""),
    )


# ----------------------------------------------------------- helpers


def _sanitize_updates(raw: Any) -> list[dict[str, Any]]:
    """Drop malformed update entries; coerce types defensively."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or "").strip()
        if not sid:
            continue
        try:
            delta = float(item.get("delta") if item.get("delta") is not None else 0.0)
        except (TypeError, ValueError):
            continue
        out.append({
            "id": sid,
            "delta": delta,
            "rationale": str(item.get("rationale") or "").strip(),
        })
    return out


def _sanitize_new_statements(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        region = str(item.get("region") or "").strip()
        if not text or not region:
            continue
        out.append({
            "text": text,
            "region": region,
            "rationale": str(item.get("rationale") or "").strip(),
        })
    return out


def _sanitize_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if isinstance(x, (str, int, float)) and str(x).strip()]

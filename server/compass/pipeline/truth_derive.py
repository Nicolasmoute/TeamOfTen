"""Truth-derive — seed / enrich the lattice from the project's truth corpus.

Runs as Stage 0 of every Compass run, before any digest. The runner
short-circuits the LLM call when the truth corpus hash is unchanged
since the last successful run AND the lattice is non-empty (i.e.
"truth didn't change, no derivation work to do" — the user's
explicit principle).

The LLM is told to skip statements already represented in the active
lattice, so even when the runner doesn't short-circuit (e.g. a fresh
truth file was added), repeat statements aren't proposed.

Truth-derived statements start at weight 0.75 — high but not pinned.
Spec §1.2 normally puts new statements at 0.5 (genuine ignorance);
truth-grounded claims sit higher because they're well-supported, but
not at 1.0 because the lattice's representation of truth is COMPASS'S
INTERPRETATION, not truth itself. The settle proposal flow can still
push them to 1.0 once the human confirms.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from server.compass import config, llm, prompts
from server.compass.store import LatticeState, TruthFact


# Initial weight for truth-derived statements. Sits in the LEANING-YES
# band so the settle proposal flow ignores them until they earn it.
TRUTH_DERIVED_WEIGHT = 0.75


@dataclass
class TruthDeriveResult:
    statements: list[dict[str, Any]]  # [{text, region, rationale}, ...]
    summary: str = ""


def truth_corpus_hash(truth: list[TruthFact]) -> str:
    """SHA-256 over the truth corpus. Stable across runs as long as
    truth files don't change. Used as the idempotency key — the runner
    skips the LLM call when the hash is unchanged AND the lattice
    already has truth-derived content."""
    h = hashlib.sha256()
    for t in truth:
        # Index isn't included — it's a synthesized 1-based ordinal
        # that depends on file enumeration order and would shift if
        # the OS reordered. The text already includes the relpath
        # prefix (see compass.truth.read_truth_facts), so file
        # identity is captured.
        h.update(t.text.encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return h.hexdigest()


async def derive_from_truth(state: LatticeState) -> TruthDeriveResult:
    """Ask the LLM to propose lattice statements grounded in truth.

    Returns sanitized proposal dicts the runner can pass directly to
    `mutate.apply_new_statements`. The runner is responsible for
    setting the initial weight (TRUTH_DERIVED_WEIGHT) and for the
    short-circuit hash check; this module always calls the LLM.
    """
    if not state.truth:
        return TruthDeriveResult(statements=[], summary="no truth — nothing to derive")

    res = await llm.call(
        prompts.TRUTH_DERIVE_SYSTEM,
        prompts.truth_derive_user(state, state.truth),
        max_tokens=config.LLM_MAX_TOKENS_DEFAULT,
        project_id=state.project_id,
        label="compass:truth_derive",
    )
    parsed = llm.parse_json_safe(res.text) or {}
    raw = parsed.get("statements") if isinstance(parsed, dict) else parsed
    if not isinstance(raw, list):
        return TruthDeriveResult(statements=[], summary="LLM returned no statements")

    # Cap + sanitize. Higher cap than passive/answer digest (which is
    # 2) because truth-derive on a fresh project is expected to
    # produce the lattice's initial spine — 8 statements is the
    # documented soft cap in TRUTH_DERIVE_SYSTEM.
    out: list[dict[str, Any]] = []
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        region = str(item.get("region") or "").strip()
        if not text or not region:
            continue
        out.append({
            "text": text,
            "region": region,
            "rationale": str(item.get("rationale") or "").strip() or "derived from truth",
        })
    return TruthDeriveResult(statements=out, summary=f"derived {len(out)} statement(s)")

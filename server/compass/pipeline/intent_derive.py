"""Intent-derive — seed / enrich the lattice from the project's corpus.

Compass is a COMPASS OF INTENT — the lattice represents what the
project is TRYING TO ACHIEVE (and trying to AVOID), not a list of
facts about the codebase. This stage walks the project's corpus
(specs in `truth/`, `project-objectives.md`, `wiki/`) and asks the
LLM to extract intent statements through one lens: "what are we
trying to achieve, what should we NOT do, what's implied beyond
what's written."

Runs as Stage 0a of every Compass run, before any digest. The runner
short-circuits the LLM call when the corpus hash is unchanged since
the last successful run AND the lattice already has intent-derived
content (i.e. "corpus didn't change, no derivation work to do").

The LLM is told to skip statements already represented in the active
lattice, so even when the runner doesn't short-circuit (e.g. a fresh
spec was added), duplicates aren't proposed.

Intent-derived statements start at weight 0.75 — high but not pinned.
Spec §1.2 normally puts new statements at 0.5 (genuine ignorance);
intent-grounded claims sit higher because they're well-supported by
the corpus, but not at 1.0 because the lattice's representation is
COMPASS'S INTERPRETATION, not the corpus itself. The settle proposal
flow can still push them to 1.0 once the human confirms.

Historically named `truth_derive` — renamed when Compass refocused
toward intent. Existing rows tagged `created_by="compass-truth"` stay
in the lattice; new rows use `created_by="compass-intent"`. The
`compass_truth_hash_<id>` team_config key keeps its name (it's still
a hash over the same corpus contents — only the prompt-side
interpretation changed). Old `truth_derive` symbols stay aliased for
back-compat.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from server.compass import config, llm, prompts
from server.compass.store import LatticeState, TruthFact


# Initial weight for intent-derived statements. Sits in the LEANING-YES
# band so the settle proposal flow ignores them until they earn it.
INTENT_DERIVED_WEIGHT = 0.75

# Provenance tag on new statements produced by this stage. Old rows
# (pre-refocus) stay tagged `compass-truth`; the runner accepts both.
INTENT_DERIVED_CREATED_BY = "compass-intent"


# Back-compat alias — keep the old constant name available.
TRUTH_DERIVED_WEIGHT = INTENT_DERIVED_WEIGHT


@dataclass
class IntentDeriveResult:
    statements: list[dict[str, Any]]  # [{text, region, rationale}, ...]
    summary: str = ""


# Back-compat alias for the old result type.
TruthDeriveResult = IntentDeriveResult


def corpus_hash(truth: list[TruthFact]) -> str:
    """SHA-256 over the corpus. Stable across runs as long as the
    corpus files don't change. Used as the idempotency key — the runner
    skips the LLM call when the hash is unchanged AND the lattice
    already has intent-derived content."""
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


# Back-compat alias for the old name.
truth_corpus_hash = corpus_hash


async def derive_from_corpus(state: LatticeState) -> IntentDeriveResult:
    """Ask the LLM to propose lattice statements representing INTENT
    derived from the project corpus.

    Returns sanitized proposal dicts the runner can pass directly to
    `mutate.apply_new_statements`. The runner is responsible for
    setting the initial weight (INTENT_DERIVED_WEIGHT), tagging rows
    with `INTENT_DERIVED_CREATED_BY`, and the short-circuit hash check;
    this module always calls the LLM.
    """
    if not state.truth:
        return IntentDeriveResult(
            statements=[], summary="no corpus — nothing to derive",
        )

    res = await llm.call(
        prompts.INTENT_DERIVE_SYSTEM,
        prompts.intent_derive_user(state, state.truth),
        max_tokens=config.LLM_MAX_TOKENS_DEFAULT,
        project_id=state.project_id,
        label="compass:intent_derive",
    )
    parsed = llm.parse_json_safe(res.text) or {}
    raw = parsed.get("statements") if isinstance(parsed, dict) else parsed
    if not isinstance(raw, list):
        return IntentDeriveResult(
            statements=[], summary="LLM returned no statements",
        )

    # Cap + sanitize. Higher cap than passive/answer digest (which is
    # 2) because intent-derive on a fresh project is expected to
    # produce the lattice's initial spine — 8 statements is the
    # documented soft cap in INTENT_DERIVE_SYSTEM.
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
            "rationale": (
                str(item.get("rationale") or "").strip()
                or "derived from corpus"
            ),
        })
    return IntentDeriveResult(
        statements=out, summary=f"derived {len(out)} statement(s)",
    )


# Back-compat alias for the old function name.
derive_from_truth = derive_from_corpus

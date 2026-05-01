"""Reconciliation — Stage 0b of the Compass pipeline (spec §3.0.1).

Fires after truth-derive on runs where the corpus hash changed AND
the pre-derive lattice was non-empty. Asks the LLM to identify
existing lattice rows (active OR archived/settled) that the new
corpus now contradicts. Each conflict becomes a `ReconciliationProposal`
the human resolves on the dashboard.

Not in the original spec §8 — added during the truth-folder
integration. The runner sequences this immediately after Stage 0a
(truth-derive); both share the corpus hash for idempotency.
"""

from __future__ import annotations

from typing import Any

from server.compass import config, llm, prompts
from server.compass.store import (
    LatticeState,
    ReconciliationProposal,
    next_reconciliation_id,
)


# Resolutions the LLM may suggest; anything else collapses to "either".
VALID_RESOLUTIONS = {"update_lattice", "update_truth", "either"}


async def detect_conflicts(
    state: LatticeState,
    *,
    run_id: str,
    run_iso: str,
) -> list[ReconciliationProposal]:
    """Ask the LLM to compare lattice rows against the truth corpus.
    Returns sanitized `ReconciliationProposal` instances ready to
    persist. Empty list when no conflicts (or no truth, or no lattice).

    The runner is responsible for:
      - Calling this only when the corpus hash changed.
      - Skipping when the pre-derive lattice was empty (nothing to
        reconcile against on first bootstrap).
      - Persisting via `store.save_proposals(reconcile=…)` and
        marking the cited statements with
        `mutate.mark_reconciliation_proposed`.
    """
    if not state.truth or not state.statements:
        return []

    # Skip detection entirely if every lattice row already has an
    # open proposal — no point re-flagging what the human is already
    # deciding on. The runner clears `reconciliation_ambiguity` on
    # corpus_changed before this is called, so we don't need to
    # filter it here; ambiguity-accepted rows are eligible again
    # whenever truth shifts.
    eligible = [
        s for s in state.statements
        if not s.reconciliation_proposed
    ]
    if not eligible:
        return []

    res = await llm.call(
        prompts.RECONCILIATION_SYSTEM,
        prompts.reconciliation_user(state, state.truth),
        max_tokens=config.LLM_MAX_TOKENS_DEFAULT,
        project_id=state.project_id,
        label="compass:reconcile",
    )
    parsed = llm.parse_json_safe(res.text) or {}
    raw = parsed.get("conflicts") if isinstance(parsed, dict) else parsed
    if not isinstance(raw, list):
        return []

    eligible_ids = {s.id for s in eligible}
    out: list[ReconciliationProposal] = []
    # Pre-allocate ids while building so multiple proposals in one
    # batch get monotonic recN values without re-scanning state per
    # entry. We'll temporarily insert stubs into state.reconciliation_proposals
    # for the id allocator and remove them at the end — same trick
    # used in `pipeline.reviews.detect_duplicates`.
    stubs_inserted = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("statement_id") or "").strip()
        if sid not in eligible_ids:
            continue
        explanation = str(item.get("explanation") or "").strip()
        if not explanation:
            continue
        corpus_paths_raw = item.get("corpus_paths")
        if not isinstance(corpus_paths_raw, list):
            corpus_paths_raw = []
        corpus_paths = [
            str(p).strip()
            for p in corpus_paths_raw
            if isinstance(p, (str, int, float)) and str(p).strip()
        ]
        suggested = str(item.get("suggested_resolution") or "either").strip()
        if suggested not in VALID_RESOLUTIONS:
            suggested = "either"
        # Look up the row to capture its archive state at proposal time.
        stmt = state.find_statement(sid)
        archived_now = bool(stmt.archived) if stmt else False

        proposal_id = next_reconciliation_id(state)
        # Stub for monotonic allocation.
        state.reconciliation_proposals.append(ReconciliationProposal(
            id=proposal_id,
            statement_id="",
            statement_archived=False,
            corpus_paths=[],
            explanation="",
        ))
        stubs_inserted += 1

        out.append(ReconciliationProposal(
            id=proposal_id,
            statement_id=sid,
            statement_archived=archived_now,
            corpus_paths=corpus_paths,
            explanation=explanation,
            suggested_resolution=suggested,
            proposed_at=run_iso,
            proposed_in_run=run_id,
        ))

    # Drop the allocator stubs — caller will overwrite the proposal
    # list with the real `out`.
    if stubs_inserted:
        del state.reconciliation_proposals[-stubs_inserted:]
    return out


def expire_old_proposals(
    proposals: list[ReconciliationProposal],
) -> list[ReconciliationProposal]:
    """Drop proposals that have been ignored for ≥ PROPOSAL_EXPIRY_RUNS
    runs (spec §10.19 generalized to reconciliation). The runner
    increments `pending_runs` each run BEFORE calling this; the LLM
    then re-detects so still-relevant ones come back fresh."""
    cap = config.PROPOSAL_EXPIRY_RUNS
    return [p for p in proposals if p.pending_runs < cap]


def increment_pending_runs(proposals: list[ReconciliationProposal]) -> None:
    for p in proposals:
        p.pending_runs += 1


__all__ = [
    "VALID_RESOLUTIONS",
    "detect_conflicts",
    "expire_old_proposals",
    "increment_pending_runs",
]

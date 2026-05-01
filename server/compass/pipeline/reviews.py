"""Settle / stale / duplicate proposal generators.

Pre-filtering happens here (in pure Python — cheap), then the LLM
phrases the human-facing question. Spec §3.4-§3.6.

Settle/stale flag rules:
  - Don't re-propose a statement whose `settle_proposed` is already
    True until the human resolves (which clears the flag).
  - Same for `stale_proposed`. `kept_stale=True` blocks re-proposal
    until weight moves out of the stale band again.
  - Pending proposals expire after `PROPOSAL_EXPIRY_RUNS` runs of
    being ignored (spec §10.19) — runner increments
    `pending_runs` each run; this module respects the count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.compass import config, llm, prompts
from server.compass.store import (
    LatticeState,
    SettleProposal,
    StaleProposal,
    DuplicateProposal,
    Statement,
    next_dupe_proposal_id,
)


@dataclass
class ReviewProposals:
    settle: list[SettleProposal]
    stale: list[StaleProposal]


# ----------------------------------------------------- settle / stale


async def propose(state: LatticeState, *, run_id: str, run_iso: str) -> ReviewProposals:
    """Pre-filter then call the LLM for phrasing.

    Pre-filter (pure):
      - Settle candidates: active, weight > SETTLED_YES OR
        weight < SETTLED_NO, settle_proposed False.
      - Stale candidates: active, weight in [STALE_BAND_LOW,
        STALE_BAND_HIGH], stale_proposed False, kept_stale False,
        and either: (a) ≥ STALE_MIN_RUNS history entries OR (b) the
        statement was created ≥ STALE_MIN_RUNS runs ago AND total
        |delta| < STALE_MAX_MOVEMENT.

    The LLM only phrases questions for the candidates we identified
    — no LLM judgement on candidacy itself.
    """
    settle_candidates = _settle_candidates(state)
    stale_candidates = _stale_candidates(state)
    if not settle_candidates and not stale_candidates:
        return ReviewProposals(settle=[], stale=[])

    res = await llm.call(
        prompts.SETTLE_STALE_SYSTEM,
        prompts.settle_stale_user(
            state,
            settle_candidates=settle_candidates,
            stale_candidates=stale_candidates,
        ),
        max_tokens=config.LLM_MAX_TOKENS_DEFAULT,
        project_id=state.project_id,
        label="compass:review",
    )
    parsed = llm.parse_json_safe(res.text) or {}
    if not isinstance(parsed, dict):
        return ReviewProposals(settle=[], stale=[])

    settle_out: list[SettleProposal] = []
    settle_ids = {s.id for s in settle_candidates}
    for item in (parsed.get("settle") or []):
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or "").strip()
        if sid not in settle_ids:
            continue
        direction = str(item.get("direction") or "").strip().lower()
        if direction not in ("yes", "no"):
            stmt = state.find_statement(sid)
            direction = "yes" if (stmt and stmt.weight >= config.SETTLED_YES) else "no"
        settle_out.append(
            SettleProposal(
                statement_id=sid,
                direction=direction,
                question=str(item.get("question") or "").strip(),
                reasoning=str(item.get("reasoning") or "").strip(),
                proposed_at=run_iso,
                proposed_in_run=run_id,
            )
        )

    stale_out: list[StaleProposal] = []
    stale_ids = {s.id for s in stale_candidates}
    for item in (parsed.get("stale") or []):
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or "").strip()
        if sid not in stale_ids:
            continue
        reform_raw = item.get("reformulation")
        reform = str(reform_raw).strip() if isinstance(reform_raw, str) else None
        stale_out.append(
            StaleProposal(
                statement_id=sid,
                question=str(item.get("question") or "").strip(),
                reasoning=str(item.get("reasoning") or "").strip(),
                proposed_at=run_iso,
                proposed_in_run=run_id,
                reformulation=reform or None,
            )
        )

    return ReviewProposals(settle=settle_out, stale=stale_out)


def _settle_candidates(state: LatticeState) -> list[Statement]:
    out: list[Statement] = []
    for s in state.active_statements():
        if s.settle_proposed:
            continue
        if s.weight >= config.SETTLED_YES or s.weight <= config.SETTLED_NO:
            out.append(s)
    return out


def _stale_candidates(state: LatticeState) -> list[Statement]:
    """Pure pre-filter for stale proposals.

    A statement is stale if:
      - In the unsettled middle [STALE_BAND_LOW, STALE_BAND_HIGH]
      - Has at least STALE_MIN_RUNS history entries
      - Cumulative absolute movement of those entries < STALE_MAX_MOVEMENT
      - No pending stale proposal flag
      - Human hasn't already said "keep" (kept_stale)
    """
    out: list[Statement] = []
    for s in state.active_statements():
        if s.stale_proposed or s.kept_stale:
            continue
        if not (config.STALE_WEIGHT_BAND_LOW <= s.weight <= config.STALE_WEIGHT_BAND_HIGH):
            continue
        if len(s.history) < config.STALE_MIN_RUNS:
            continue
        cumulative = sum(abs(float(h.get("delta") or 0.0)) for h in s.history)
        if cumulative >= config.STALE_MAX_MOVEMENT:
            continue
        out.append(s)
    return out


# ---------------------------------------------------- duplicate detection


async def detect_duplicates(
    state: LatticeState, *, run_id: str, run_iso: str
) -> list[DuplicateProposal]:
    """Find clusters of redundant statements.

    Skips statements that are already `dupe_proposed=True` (existing
    pending proposal — let the human resolve before re-detecting).
    """
    eligible = [s for s in state.active_statements() if not s.dupe_proposed]
    if len(eligible) < 2:
        return []

    res = await llm.call(
        prompts.DUPLICATE_SYSTEM,
        prompts.duplicate_user(state),
        max_tokens=config.LLM_MAX_TOKENS_DEFAULT,
        project_id=state.project_id,
        label="compass:dupes",
    )
    parsed = llm.parse_json_safe(res.text) or {}
    raw = parsed.get("duplicates") if isinstance(parsed, dict) else parsed
    if not isinstance(raw, list):
        return []

    eligible_ids = {s.id for s in eligible}
    out: list[DuplicateProposal] = []
    counter = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        ids = item.get("ids")
        if not isinstance(ids, list) or len(ids) < 2:
            continue
        cluster = [str(x).strip() for x in ids if isinstance(x, str) and x.strip()]
        # Each id must be active, not already proposed.
        if any(sid not in eligible_ids for sid in cluster):
            continue
        if len(set(cluster)) < 2:
            continue
        text = str(item.get("merged_text") or "").strip()
        if not text:
            continue
        try:
            weight = float(item.get("merged_weight") if item.get("merged_weight") is not None else 0.5)
        except (TypeError, ValueError):
            weight = 0.5
        weight = max(0.0, min(1.0, weight))
        region = str(item.get("region") or "").strip()
        if not region:
            # Default to the most-common region across the cluster.
            counts: dict[str, int] = {}
            for sid in cluster:
                stmt = state.find_statement(sid)
                if stmt:
                    counts[stmt.region] = counts.get(stmt.region, 0) + 1
            region = max(counts, key=counts.get) if counts else "general"  # type: ignore[arg-type]
        # Stable id allocator with carryover.
        # We can't call next_dupe_proposal_id once because subsequent
        # items in the same loop need fresh ids. The store helper
        # reads from state.duplicate_proposals; mock-extend to keep ids
        # monotonic within this batch.
        proposal_id = next_dupe_proposal_id(state)
        # Fake-add a stub so the next call returns a unique id; we
        # remove these stubs at the end (we don't actually want them
        # in state.duplicate_proposals — runner persists the real list).
        from server.compass.store import DuplicateProposal as _DP  # noqa: PLC0415
        state.duplicate_proposals.append(_DP(
            id=proposal_id,
            cluster_ids=[],
            merged_text="",
            merged_weight=0.0,
            region="",
            reasoning="",
            proposed_at="",
            proposed_in_run="",
        ))
        counter += 1
        out.append(
            DuplicateProposal(
                id=proposal_id,
                cluster_ids=cluster,
                merged_text=text,
                merged_weight=weight,
                region=region,
                reasoning=str(item.get("reasoning") or "").strip(),
                proposed_at=run_iso,
                proposed_in_run=run_id,
            )
        )
    # Strip the stubs we inserted only for id allocation. Caller
    # will overwrite duplicate_proposals with the real `out` list.
    if counter:
        del state.duplicate_proposals[-counter:]
    return out


# ---------------------------------------------------- expiry & flag mgmt


def expire_old_proposals(
    settle: list[SettleProposal],
    stale: list[StaleProposal],
    dupes: list[DuplicateProposal],
) -> tuple[list[SettleProposal], list[StaleProposal], list[DuplicateProposal]]:
    """Drop proposals that have been ignored for ≥ PROPOSAL_EXPIRY_RUNS
    runs (spec §10.19). The runner increments `pending_runs` each run
    BEFORE calling this, then re-runs detection so genuinely-still-
    relevant ones come back fresh.
    """
    cap = config.PROPOSAL_EXPIRY_RUNS
    settle_kept = [p for p in settle if p.pending_runs < cap]
    stale_kept = [p for p in stale if p.pending_runs < cap]
    dupes_kept = [p for p in dupes if p.pending_runs < cap]
    return settle_kept, stale_kept, dupes_kept


def increment_pending_runs(
    settle: list[SettleProposal],
    stale: list[StaleProposal],
    dupes: list[DuplicateProposal],
) -> None:
    for p in settle:
        p.pending_runs += 1
    for p in stale:
        p.pending_runs += 1
    for p in dupes:
        p.pending_runs += 1


def mark_proposed_flags(
    state: LatticeState,
    *,
    settle: list[SettleProposal],
    stale: list[StaleProposal],
    dupes: list[DuplicateProposal],
) -> None:
    """After persisting, set the in-state `*_proposed` flag on each
    proposed statement so we don't re-propose next run."""
    settle_ids = {p.statement_id for p in settle}
    stale_ids = {p.statement_id for p in stale}
    dupe_ids: set[str] = set()
    for p in dupes:
        dupe_ids.update(p.cluster_ids)
    for s in state.statements:
        if s.id in settle_ids:
            s.settle_proposed = True
        if s.id in stale_ids:
            s.stale_proposed = True
        if s.id in dupe_ids:
            s.dupe_proposed = True

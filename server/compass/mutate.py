"""In-memory mutation helpers for `LatticeState`.

The pipeline modules under `server.compass.pipeline` are pure: they
take state and return proposed changes. This module owns the
machinery that *applies* those changes to a `LatticeState` in
memory. The runner / API layer then persists the mutated state via
`store.save_*`.

Why a separate module: the API layer (e.g. `POST
/api/compass/proposals/settle/{id}`) needs the same primitives the
runner uses (settle a statement, merge a duplicate cluster). Keeping
them here lets both call sites share the implementation.

Every mutation:
  - Clamps weights to [0, 1] when applying deltas.
  - Records history entries with `run_id`, computed delta, rationale,
    source — the lattice's audit trail.
  - Honors the spec's caps (e.g. max 2 new statements per digest).
  - Never persists; that's the caller's responsibility.

Stable invariants enforced here (NOT in pipeline modules):
  - `Statement.archived` blocks mutation; archived statements are
    immutable except for region re-tagging on merge (§10.11) and
    re-archive of merge losers.
  - `Statement.region` is always one of the active regions when set;
    `ensure_region` adds a new region row when needed.
  - `Region.merged_into` is set when a region is merged away — the
    region row stays so historical references resolve.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from server.compass.store import (
    LatticeState,
    Region,
    RegionMergeEvent,
    Statement,
    next_statement_id,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# -------------------------------------------------------- weight updates


def apply_statement_updates(
    state: LatticeState,
    updates: Iterable[dict],
    *,
    run_id: str,
    source: str,
    delta_max: float,
) -> int:
    """Apply a list of `{id, delta, rationale}` updates in place.

    Each update:
      - Looks up the statement; missing or archived → skip.
      - Clamps `delta` to `[-delta_max, +delta_max]`.
      - Computes new weight clamped to `[0, 1]`.
      - Records the actual applied delta (which may differ from
        the LLM's request after clamping) in the statement's history.
      - Skips no-op deltas (< 1e-6).

    Returns the count of updates actually applied (after skip).
    """
    applied = 0
    for raw in updates:
        sid = str(raw.get("id") or "")
        try:
            delta = float(raw.get("delta") if raw.get("delta") is not None else 0.0)
        except (TypeError, ValueError):
            continue
        rationale = str(raw.get("rationale") or "")
        s = state.find_statement(sid)
        if s is None or s.archived:
            continue
        clamped = max(-delta_max, min(delta_max, delta))
        old = float(s.weight)
        new = max(0.0, min(1.0, old + clamped))
        actual = new - old
        if abs(actual) < 1e-6:
            continue
        s.weight = new
        s.history.append({
            "run_id": run_id,
            "delta": round(actual, 4),
            "rationale": rationale,
            "source": source,
        })
        applied += 1
    return applied


# ---------------------------------------------------------- new statements


def apply_new_statements(
    state: LatticeState,
    proposals: Iterable[dict],
    *,
    run_id: str,
    source: str,
    cap: int = 2,
    created_by: str = "compass",
) -> list[Statement]:
    """Materialize proposed new statements. Honors:
      - Per-call cap (spec: max 2 per digest).
      - Region creation: if the proposed region isn't in the active
        list, add it as a fresh `compass`-created Region.
      - `weight = 0.5` start (spec §8.2).
    """
    added: list[Statement] = []
    now = _now_iso()
    for raw in list(proposals)[:cap]:
        text = str(raw.get("text") or "").strip()
        region = str(raw.get("region") or "").strip()
        rationale = str(raw.get("rationale") or "")
        if not text or not region:
            continue
        ensure_region(state, region, created_by="compass")
        sid = next_statement_id(state)
        s = Statement(
            id=sid,
            text=text,
            region=region,
            weight=0.5,
            created_at=now,
            created_by=created_by,
            history=[{
                "run_id": run_id,
                "delta": 0.0,
                "rationale": rationale or "newly proposed",
                "source": source,
            }],
        )
        state.statements.append(s)
        added.append(s)
    return added


def ensure_region(
    state: LatticeState, name: str, *, created_by: str = "compass"
) -> Region:
    """Ensure a region with `name` exists in the active list.

    If it exists but was previously merged into another, restore it
    by clearing `merged_into` — the LLM has decided this region is
    relevant again.
    """
    name = name.strip()
    if not name:
        raise ValueError("region name required")
    for r in state.regions:
        if r.name == name:
            if r.merged_into is not None:
                r.merged_into = None
            return r
    new_region = Region(name=name, created_at=_now_iso(), created_by=created_by)
    state.regions.append(new_region)
    return new_region


# --------------------------------------------------------- region merge


def apply_region_merge(
    state: LatticeState,
    *,
    from_: list[str],
    to: str,
    run_id: str,
) -> int:
    """Merge `from_` regions into `to`. Re-tags every statement (active
    AND archived per spec §10.11) and marks each `from_` region's
    `merged_into = to`. Returns count of statements re-tagged.

    No-ops if `to` doesn't exist in the active region list — the
    caller should ensure_region(to) first.
    """
    if to not in {r.name for r in state.active_regions()}:
        ensure_region(state, to)
    losers = {n for n in from_ if n and n != to}
    if not losers:
        return 0
    for r in state.regions:
        if r.name in losers and r.merged_into is None:
            r.merged_into = to
    retagged = 0
    for s in state.statements:
        if s.region in losers:
            s.region = to
            retagged += 1
    state.region_merge_history.append(
        RegionMergeEvent(
            from_=sorted(losers),
            to=to,
            merged_at=_now_iso(),
            run_id=run_id,
        )
    )
    return retagged


# ----------------------------------------------------------- settle


def settle_statement(
    state: LatticeState,
    sid: str,
    *,
    weight: float,
    direction: str,  # "yes" | "no" | "partial"
    run_id: str,
    by_human: bool = True,
) -> Statement | None:
    """Archive a statement at the human-confirmed final weight.
    Clears any pending settle proposal flag. Records a history entry
    so the lattice trail shows when settle happened."""
    s = state.find_statement(sid)
    if s is None or s.archived:
        return None
    final = max(0.0, min(1.0, float(weight)))
    delta = final - float(s.weight)
    s.weight = final
    s.archived = True
    s.archived_at = _now_iso()
    s.settled_as = direction
    s.settled_by_human = bool(by_human)
    s.settle_proposed = False
    s.stale_proposed = False
    s.dupe_proposed = False
    s.history.append({
        "run_id": run_id,
        "delta": round(delta, 4),
        "rationale": f"settled as {direction}",
        "source": "settle:human" if by_human else "settle:compass",
    })
    return s


# ----------------------------------------------------------- reformulate


def reformulate_statement(
    state: LatticeState,
    sid: str,
    new_text: str,
    *,
    run_id: str,
    new_region: str | None = None,
) -> Statement | None:
    """Reformulate a stale-flagged statement: replace text, reset
    weight to 0.5, clear history (per spec §3.5). Optionally move to
    a different region. Treated as a fresh statement going forward."""
    s = state.find_statement(sid)
    if s is None or s.archived:
        return None
    new_text = (new_text or "").strip()
    if not new_text:
        return None
    s.text = new_text
    if new_region:
        ensure_region(state, new_region)
        s.region = new_region
    s.weight = 0.5
    s.history = [{
        "run_id": run_id,
        "delta": 0.0,
        "rationale": "reformulated by human",
        "source": "reformulation",
    }]
    s.reformulated = True
    s.stale_proposed = False
    s.kept_stale = False
    return s


def keep_stale(state: LatticeState, sid: str) -> Statement | None:
    """Human chose 'keep' on a stale proposal: clear `stale_proposed`,
    set `kept_stale` so we don't re-propose for a while."""
    s = state.find_statement(sid)
    if s is None or s.archived:
        return None
    s.stale_proposed = False
    s.kept_stale = True
    return s


def retire_statement(
    state: LatticeState,
    sid: str,
    *,
    run_id: str,
) -> Statement | None:
    """Human chose 'irrelevant' on a stale proposal: archive without
    a settled direction. Distinct from a YES/NO settle — this means
    the lattice claim isn't worth tracking, not that it's true/false.
    """
    s = state.find_statement(sid)
    if s is None or s.archived:
        return None
    s.archived = True
    s.archived_at = _now_iso()
    s.settled_as = "retired"
    s.settled_by_human = True
    s.stale_proposed = False
    s.history.append({
        "run_id": run_id,
        "delta": 0.0,
        "rationale": "retired (human marked irrelevant)",
        "source": "stale:retire",
    })
    return s


# -------------------------------------------------------- duplicate merge


def merge_duplicate_cluster(
    state: LatticeState,
    cluster_ids: list[str],
    *,
    merged_text: str,
    merged_weight: float,
    region: str,
    run_id: str,
) -> Statement | None:
    """Replace a cluster of duplicates with one sharper statement.
    Each cluster member is archived with `settled_as='merged'`. The
    new statement's `merged_from` lists the originals so the trail
    is preserved."""
    losers: list[Statement] = []
    for sid in cluster_ids:
        s = state.find_statement(sid)
        if s is None or s.archived:
            continue
        losers.append(s)
    if len(losers) < 2:
        return None  # not really a cluster
    ensure_region(state, region)
    new_id = next_statement_id(state)
    now = _now_iso()
    new_stmt = Statement(
        id=new_id,
        text=(merged_text or "").strip() or losers[0].text,
        region=region,
        weight=max(0.0, min(1.0, float(merged_weight))),
        created_at=now,
        created_by="compass",
        merged=True,
        merged_from=[s.id for s in losers],
        history=[{
            "run_id": run_id,
            "delta": 0.0,
            "rationale": f"merged from cluster: {', '.join(s.id for s in losers)}",
            "source": "merge",
        }],
    )
    state.statements.append(new_stmt)
    for s in losers:
        s.archived = True
        s.archived_at = now
        s.settled_as = "merged"
        s.dupe_proposed = False
        s.history.append({
            "run_id": run_id,
            "delta": 0.0,
            "rationale": f"merged into {new_id}",
            "source": "merge",
        })
    return new_stmt


# ----------------------------------------------------- manual override


def manual_weight_override(
    state: LatticeState,
    sid: str,
    new_weight: float,
    *,
    run_id: str,
) -> Statement | None:
    """Human-driven manual weight set. Marks `manually_set=True` so
    the lattice retains the audit trail. Does NOT archive — settle is
    a separate flow per spec §14.17.2 / §10.2."""
    s = state.find_statement(sid)
    if s is None or s.archived:
        return None
    new = max(0.0, min(1.0, float(new_weight)))
    delta = new - float(s.weight)
    s.weight = new
    s.manually_set = True
    s.history.append({
        "run_id": run_id,
        "delta": round(delta, 4),
        "rationale": "human override",
        "source": "manual",
    })
    return s


# Truth has no mutate helpers — Compass never writes truth. Truth
# lives in the project's `truth/` folder, edited by humans via the
# Files pane and proposed by Coach via `coord_propose_truth_update`.
# Compass reads it via `server.compass.truth.read_truth_facts`.


# --------------------------------------------------- restore (un-archive)


def restore_statement(state: LatticeState, sid: str) -> Statement | None:
    """Move a settled / archived statement back to active at its
    current weight. Rare but possible — spec §14.6 archive list has a
    RESTORE button."""
    s = state.find_statement(sid)
    if s is None or not s.archived:
        return None
    s.archived = False
    s.archived_at = None
    s.settled_as = None
    s.settled_by_human = False
    return s


__all__ = [
    "apply_statement_updates",
    "apply_new_statements",
    "ensure_region",
    "apply_region_merge",
    "settle_statement",
    "reformulate_statement",
    "keep_stale",
    "retire_statement",
    "merge_duplicate_cluster",
    "manual_weight_override",
    "restore_statement",
]

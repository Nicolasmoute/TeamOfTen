"""Playbook mutation primitives — pure, no I/O, no event bus.

Every mutation goes through one of these functions. The runner /
bootstrap / API / MCP-tool callers compose them, persist via
`store.save_lattice` + `store.save_archive`, then emit bus events
upstream. Pure separation: this module is mockable, deterministic,
and trivially unit-testable.

Spec references:
  - §5.6 op apply order (merges → creates → adjusts on post-merge state)
  - §5.7 soft/hard cap branches (deterministic drop-from-end)
  - §5.8 settle/stale/stale-unused predicates (immutable=false guard)
  - §5.6 N5 near-duplicate detection (Jaccard ≥ 0.7 over word tokens)
  - §3.1 weight_history append + last_validated_at update
"""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from server.playbook import config
from server.playbook.store import (
    Archive,
    ArchivedStatement,
    Lattice,
    Statement,
    WeightHistoryEntry,
)

logger = logging.getLogger("harness.playbook.mutate")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------- helpers


_PB_ID_RE = re.compile(r"^pb-\d+$")
_STOPWORDS = frozenset({
    "a", "an", "the",
    "and", "or", "of",
    "to", "for", "in", "on", "at", "with",
    "is", "are", "be",
})
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _tokenize(text: str) -> set[str]:
    """Lowercased word tokens, stopwords stripped — used for Jaccard
    near-duplicate detection (spec §5.6 N5)."""
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _next_id(lattice: Lattice, archive: Archive) -> str:
    """Mint the next `pb-NNN` id. Scans both active + archived to
    avoid id reuse across an entry's lifecycle."""
    max_n = 0
    for s in lattice.statements:
        m = _PB_ID_RE.match(s.id)
        if m:
            n = int(s.id.split("-", 1)[1])
            if n > max_n:
                max_n = n
    for s in archive.statements:
        m = _PB_ID_RE.match(s.id)
        if m:
            n = int(s.id.split("-", 1)[1])
            if n > max_n:
                max_n = n
    return f"pb-{max_n + 1:03d}"


def _find(lattice: Lattice, sid: str) -> Statement | None:
    for s in lattice.statements:
        if s.id == sid:
            return s
    return None


def _archive(
    archive: Archive,
    stmt: Statement,
    *,
    reason: str,
    merged_into: str | None = None,
) -> None:
    """Move `stmt` into the archive container. Caller removes it from
    the active lattice."""
    archive.statements.append(
        ArchivedStatement(
            id=stmt.id,
            text=stmt.text,
            final_weight=stmt.weight,
            archived_at=_now_iso(),
            archive_reason=reason,
            merged_into=merged_into,
            history=list(stmt.weight_history),
            created_at=stmt.created_at,
            created_by=stmt.created_by,
        )
    )


# ---------------------------------------------------------------- create


def find_near_duplicate(lattice: Lattice, text: str) -> Statement | None:
    """Return an existing active statement whose text is a near-duplicate
    of `text` per the §5.6-N5 Jaccard rule, or None.

    Threshold is `config.DUPLICATE_JACCARD_THRESHOLD` (default 0.7).
    Tokenization strips stopwords and ASCII punctuation.
    """
    new_tokens = _tokenize(text)
    if not new_tokens:
        return None
    threshold = config.DUPLICATE_JACCARD_THRESHOLD
    for s in lattice.statements:
        if _jaccard(_tokenize(s.text), new_tokens) >= threshold:
            return s
    return None


def insert_statement(
    lattice: Lattice,
    *,
    text: str,
    weight: float,
    created_by: str,
    immutable: bool = False,
    new_id: str | None = None,
    archive_for_id_minting: Archive | None = None,
) -> Statement:
    """Mint a new active statement and append to lattice. Caller is
    responsible for cap enforcement upstream (use `apply_caps` /
    `apply_coach_proposals`); this function inserts unconditionally."""
    archive = archive_for_id_minting or Archive(
        schema_version=config.PLAYBOOK_SCHEMA_VERSION, statements=[]
    )
    sid = new_id or _next_id(lattice, archive)
    now = _now_iso()
    weight_clamped = max(0.0, min(1.0, float(weight)))
    stmt = Statement(
        id=sid,
        text=text.strip()[:500],
        weight=weight_clamped,
        weight_history=[
            WeightHistoryEntry(
                ts=now,
                from_=None,
                to=weight_clamped,
                reason=f"created (created_by={created_by})",
            )
        ],
        created_at=now,
        created_by=created_by,
        last_validated_at=now,
        applied_count=0,
        immutable=immutable,
    )
    lattice.statements.append(stmt)
    return stmt


# ---------------------------------------------------------------- adjust


def apply_op_adjust(
    lattice: Lattice,
    *,
    sid: str,
    delta: float,
    reason: str,
) -> tuple[bool, str | None]:
    """Apply an `adjust` op. Returns (success, error_reason_or_None).

    Validation (spec §5.6):
      - id exists in active lattice
      - not immutable
      - |delta| <= ADJUST_DELTA_CAP
      - clamped target weight stays in [0, 1]
    """
    stmt = _find(lattice, sid)
    if stmt is None:
        return False, "id_not_found"
    if stmt.immutable:
        return False, "immutable"
    delta = float(delta)
    if abs(delta) > config.ADJUST_DELTA_CAP + 1e-9:
        return False, "delta_exceeds_cap"
    new_weight = max(0.0, min(1.0, stmt.weight + delta))
    if new_weight == stmt.weight:
        return False, "no_op_after_clamp"
    now = _now_iso()
    stmt.weight_history.append(
        WeightHistoryEntry(
            ts=now,
            from_=stmt.weight,
            to=new_weight,
            reason=reason or "(no reason given)",
        )
    )
    stmt.weight = new_weight
    stmt.last_validated_at = now
    return True, None


# ---------------------------------------------------------------- merge


def apply_op_merge(
    lattice: Lattice,
    archive: Archive,
    *,
    keep_id: str,
    drop_id: str,
    reason: str,
) -> tuple[bool, str | None]:
    """Collapse `drop_id` into `keep_id`. Returns (success, error).

    Per spec §5.6:
      - both ids must exist in active lattice
      - neither immutable
      - keep_id retains weight = max(keep, drop)
      - keep_id.applied_count += drop.applied_count
      - keep_id.last_validated_at = max(keep, drop) [NULL-safe]
      - drop_id moved to archive with archive_reason="merged",
        merged_into=keep_id
    """
    if keep_id == drop_id:
        return False, "self_merge"
    keep = _find(lattice, keep_id)
    drop = _find(lattice, drop_id)
    if keep is None or drop is None:
        return False, "id_not_found"
    if keep.immutable or drop.immutable:
        return False, "immutable"

    new_weight = max(keep.weight, drop.weight)
    now = _now_iso()
    if new_weight != keep.weight:
        keep.weight_history.append(
            WeightHistoryEntry(
                ts=now,
                from_=keep.weight,
                to=new_weight,
                reason=f"merge from {drop_id}: {reason}",
            )
        )
        keep.weight = new_weight
    else:
        # Record the merge even when weight didn't move so the audit
        # trail captures the structural change.
        keep.weight_history.append(
            WeightHistoryEntry(
                ts=now,
                from_=keep.weight,
                to=keep.weight,
                reason=f"merge from {drop_id} (no weight change): {reason}",
            )
        )

    keep.applied_count += drop.applied_count

    # NULL-safe max of last_validated_at strings (ISO-8601 sorts
    # lexicographically). Treat NULL as oldest.
    keep_lv = keep.last_validated_at or ""
    drop_lv = drop.last_validated_at or ""
    keep.last_validated_at = max(keep_lv, drop_lv) or None

    # Archive the drop side.
    _archive(archive, drop, reason="merged", merged_into=keep_id)
    lattice.statements = [s for s in lattice.statements if s.id != drop_id]

    return True, None


# ---------------------------------------------------------------- caps (§5.7)


def resolve_cap_pressure(
    *,
    active_count: int,
    creation_count: int,
) -> tuple[int, int, bool]:
    """Spec §5.7 three-branch cap resolution. Returns
    (survivor_count, dropped_count, hard_cap_hit).

    Branches:
      A: pressure ≤ SOFT (100) — apply all creations, survivor=creation_count.
      B: SOFT < pressure ≤ HARD (110) — drop from end until at SOFT;
         survivor = SOFT - active_count (clamped to ≥0).
      C: pressure > HARD — drop ALL creations; survivor=0; hard_cap_hit=True.

    Caller is responsible for applying the drop deterministically
    (input-list order, drop later-listed first).
    """
    pressure = active_count + creation_count
    soft = config.SOFT_STATEMENT_CAP
    hard = config.HARD_STATEMENT_CAP
    if pressure <= soft:
        return creation_count, 0, False
    if pressure <= hard:
        survivors = max(0, soft - active_count)
        dropped = creation_count - survivors
        return survivors, dropped, False
    return 0, creation_count, True


# ---------------------------------------------------------------- proposal apply


def apply_coach_proposals(
    lattice: Lattice,
    archive: Archive,
    operations: list[dict[str, Any]],
    *,
    creation_weight: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Apply Coach-side proposals (from MCP tool or daily reflection).

    Returns (applied, rejected, hard_cap_hit). Each `applied` /
    `rejected` entry mirrors the input op shape with extra fields:
      - applied: includes `from`/`to`/`new_id` as relevant
      - rejected: includes `reason` string

    Op apply order (spec §5.6 — fixed):
      1. Merges first.
      2. Creations next (against post-merge state). Soft/hard cap
         enforced here per §5.7.
      3. Adjustments last (against post-merge, post-creation state).

    Cross-op conflict (spec §5.6): any op targeting an id archived by
    an earlier merge is rejected with reason `"id_archived_in_same_run"`.
    """
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    archived_in_run: set[str] = set()  # ids archived by step-1 merges

    # Partition ops by type, preserving input order.
    merges = [op for op in operations if op.get("op") == "merge"]
    creations = [op for op in operations if op.get("op") == "create"]
    adjusts = [op for op in operations if op.get("op") == "adjust"]
    other = [op for op in operations if op.get("op") not in ("merge", "create", "adjust")]
    for op in other:
        rejected.append({**op, "reason": "unknown_op"})

    # ---- Step 1: merges
    for op in merges:
        keep_id = str(op.get("keep_id") or "")
        drop_id = str(op.get("drop_id") or "")
        reason = str(op.get("reason") or "")
        if not _PB_ID_RE.match(keep_id) or not _PB_ID_RE.match(drop_id):
            rejected.append({**op, "reason": "malformed_id"})
            continue
        if keep_id in archived_in_run or drop_id in archived_in_run:
            rejected.append({**op, "reason": "id_archived_in_same_run"})
            continue
        ok, err = apply_op_merge(
            lattice, archive,
            keep_id=keep_id, drop_id=drop_id, reason=reason,
        )
        if ok:
            archived_in_run.add(drop_id)
            applied.append({**op, "merged_into": keep_id})
        else:
            rejected.append({**op, "reason": err or "merge_failed"})

    # ---- Step 2: creations (soft/hard cap)
    valid_creations: list[dict[str, Any]] = []
    for op in creations:
        text = str(op.get("text") or "").strip()
        if not text:
            rejected.append({**op, "reason": "empty_text"})
            continue
        if len(text) > config.STATEMENT_MAX_CHARS:
            rejected.append({
                **op,
                "reason": (
                    f"text_too_long: {len(text)} chars > "
                    f"{config.STATEMENT_MAX_CHARS}-char cap. "
                    "One line, imperative, no enumerated sub-items. "
                    "Rationale + detail belong in the prose corpus, "
                    "not the per-turn lattice injection."
                ),
            })
            continue
        try:
            weight = float(op.get("weight") if op.get("weight") is not None else creation_weight)
        except (TypeError, ValueError):
            rejected.append({**op, "reason": "invalid_weight"})
            continue
        if not (0.0 <= weight <= 1.0):
            rejected.append({**op, "reason": "weight_out_of_range"})
            continue
        dup = find_near_duplicate(lattice, text)
        if dup is not None:
            rejected.append({**op, "reason": "near_duplicate", "near_duplicate_id": dup.id})
            continue
        valid_creations.append({**op, "_text": text, "_weight": weight})

    survivors_n, dropped_n, hard_cap_hit = resolve_cap_pressure(
        active_count=len(lattice.statements),
        creation_count=len(valid_creations),
    )

    # Drop from end of input list (deterministic — Coach can prioritize
    # by ordering its `creations` array per §5.7 / §S3).
    if hard_cap_hit:
        # Drop ALL valid creations atomically (§5.7 branch C).
        for op in valid_creations:
            rejected.append({**{k: v for k, v in op.items() if not k.startswith("_")},
                             "reason": "hard_cap_pressure"})
        valid_creations = []
    elif dropped_n > 0:
        for op in valid_creations[survivors_n:]:
            rejected.append({**{k: v for k, v in op.items() if not k.startswith("_")},
                             "reason": "soft_cap_pressure"})
        valid_creations = valid_creations[:survivors_n]

    for op in valid_creations:
        stmt = insert_statement(
            lattice,
            text=op["_text"],
            weight=op["_weight"],
            created_by="reflection",
            archive_for_id_minting=archive,
        )
        clean = {k: v for k, v in op.items() if not k.startswith("_")}
        applied.append({**clean, "new_id": stmt.id, "weight": stmt.weight})

    # ---- Step 3: adjusts
    for op in adjusts:
        sid = str(op.get("id") or "")
        if not _PB_ID_RE.match(sid):
            rejected.append({**op, "reason": "malformed_id"})
            continue
        if sid in archived_in_run:
            rejected.append({**op, "reason": "id_archived_in_same_run"})
            continue
        try:
            delta = float(op.get("delta"))
        except (TypeError, ValueError):
            rejected.append({**op, "reason": "invalid_delta"})
            continue
        reason = str(op.get("reason") or "")
        before = _find(lattice, sid)
        before_weight = before.weight if before else None
        ok, err = apply_op_adjust(lattice, sid=sid, delta=delta, reason=reason)
        if ok:
            after = _find(lattice, sid)
            applied.append({
                **op,
                "from": before_weight,
                "to": after.weight if after else None,
            })
        else:
            rejected.append({**op, "reason": err or "adjust_failed"})

    return applied, rejected, hard_cap_hit


# ---------------------------------------------------------------- relevant_ids


def increment_relevant_ids(
    lattice: Lattice,
    relevant_ids: Any,
) -> int:
    """Walk Coach's `relevant_ids` list (§5.5) and increment
    `applied_count += 1` for each valid + extant id.

    Validation (spec §5.6 + §N5):
      - regex `^pb-\\d+$` (skip non-string, empty, malformed)
      - dedupe (one increment per statement per run)
      - skip ids not in active lattice (e.g. archived earlier in same run)

    Returns the total number of increments applied.
    """
    if not isinstance(relevant_ids, list):
        return 0
    seen: set[str] = set()
    increments = 0
    now = _now_iso()
    for entry in relevant_ids:
        if not isinstance(entry, str):
            continue
        sid = entry.strip()
        if not _PB_ID_RE.match(sid):
            continue
        if sid in seen:
            continue
        seen.add(sid)
        stmt = _find(lattice, sid)
        if stmt is None:
            continue
        stmt.applied_count += 1
        stmt.last_validated_at = now
        increments += 1
    return increments


# ---------------------------------------------------------------- settle / stale (§5.8)


def _has_history_at_least(stmt: Statement, days: int) -> bool:
    """True iff at least one weight_history entry has `ts ≤ now - days`."""
    if not stmt.weight_history:
        return False
    cutoff = _now() - timedelta(days=days)
    for h in stmt.weight_history:
        try:
            ts = datetime.fromisoformat(h.ts)
        except ValueError:
            continue
        if ts <= cutoff:
            return True
    return False


def _no_excursion_within(stmt: Statement, *, days: int, threshold_floor: float | None = None,
                         threshold_ceil: float | None = None) -> bool:
    """True iff no weight_history entry within the last `days` days
    crossed below `threshold_floor` (for settle) or above
    `threshold_ceil` (for stale-low).

    Use threshold_floor for settle: weight ≥ threshold consistently.
    Use threshold_ceil for stale_low: weight ≤ threshold consistently.
    """
    cutoff = _now() - timedelta(days=days)
    for h in stmt.weight_history:
        try:
            ts = datetime.fromisoformat(h.ts)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if threshold_floor is not None and h.to < threshold_floor:
            return False
        if threshold_ceil is not None and h.to > threshold_ceil:
            return False
    return True


def is_settle_eligible(stmt: Statement) -> bool:
    """Spec §5.8: weight ≥ SETTLE_THRESHOLD; ≥1 history entry ≥
    SETTLE_STABLE_DAYS old; no excursion below threshold within window;
    not immutable."""
    if stmt.immutable:
        return False
    if stmt.weight < config.SETTLE_THRESHOLD:
        return False
    if not _has_history_at_least(stmt, config.SETTLE_STABLE_DAYS):
        return False
    return _no_excursion_within(
        stmt,
        days=config.SETTLE_STABLE_DAYS,
        threshold_floor=config.SETTLE_THRESHOLD,
    )


def is_stale_low_eligible(stmt: Statement) -> bool:
    """Spec §5.8: weight ≤ STALE_THRESHOLD; ≥1 history entry ≥
    STALE_STABLE_DAYS old; no excursion above threshold within window;
    not immutable."""
    if stmt.immutable:
        return False
    if stmt.weight > config.STALE_THRESHOLD:
        return False
    if not _has_history_at_least(stmt, config.STALE_STABLE_DAYS):
        return False
    return _no_excursion_within(
        stmt,
        days=config.STALE_STABLE_DAYS,
        threshold_ceil=config.STALE_THRESHOLD,
    )


def is_stale_unused_eligible(stmt: Statement) -> bool:
    """Spec §5.8: applied_count == 0 AND created_at ≥
    STALE_UNUSED_DAYS ago; not immutable."""
    if stmt.immutable:
        return False
    if stmt.applied_count != 0:
        return False
    try:
        created = datetime.fromisoformat(stmt.created_at)
    except (ValueError, TypeError):
        return False
    return created <= (_now() - timedelta(days=config.STALE_UNUSED_DAYS))


def sweep_engine_actions(
    lattice: Lattice,
    archive: Archive,
) -> list[dict[str, Any]]:
    """Run all three engine-driven archive predicates (§5.8).
    Returns a list of action records for the runs.jsonl `engine_actions`
    field. Mutates lattice + archive in place.
    """
    actions: list[dict[str, Any]] = []

    # Settle pass
    settled = [s for s in lattice.statements if is_settle_eligible(s)]
    for s in settled:
        _archive(archive, s, reason="settled")
        actions.append({"action": "settle", "id": s.id, "final_weight": s.weight})
    if settled:
        kept_ids = {s.id for s in settled}
        lattice.statements = [s for s in lattice.statements if s.id not in kept_ids]

    # Stale-low pass
    stale_low = [s for s in lattice.statements if is_stale_low_eligible(s)]
    for s in stale_low:
        _archive(archive, s, reason="stale_low")
        actions.append({"action": "stale_low", "id": s.id, "final_weight": s.weight})
    if stale_low:
        kept_ids = {s.id for s in stale_low}
        lattice.statements = [s for s in lattice.statements if s.id not in kept_ids]

    # Stale-unused pass
    stale_unused = [s for s in lattice.statements if is_stale_unused_eligible(s)]
    for s in stale_unused:
        _archive(archive, s, reason="stale_unused")
        actions.append({"action": "stale_unused", "id": s.id, "final_weight": s.weight})
    if stale_unused:
        kept_ids = {s.id for s in stale_unused}
        lattice.statements = [s for s in lattice.statements if s.id not in kept_ids]

    return actions


# ---------------------------------------------------------------- override / restore / delete


def override_weight(
    lattice: Lattice,
    sid: str,
    *,
    weight: float,
    actor: str,
) -> tuple[bool, str | None]:
    """Direct human override (NO/½/YES button). Bypasses the delta cap
    but still respects immutable flag (§7 / §8 / §13.1).

    `weight` MUST be one of {0.0, 0.5, 1.0} per the dashboard convention;
    accepts any value in [0,1] for API flexibility.
    """
    stmt = _find(lattice, sid)
    if stmt is None:
        return False, "id_not_found"
    if stmt.immutable:
        return False, "immutable"
    new_weight = max(0.0, min(1.0, float(weight)))
    if new_weight == stmt.weight:
        return False, "no_op"
    now = _now_iso()
    stmt.weight_history.append(
        WeightHistoryEntry(
            ts=now,
            from_=stmt.weight,
            to=new_weight,
            reason=f"human_override (actor={actor})",
        )
    )
    stmt.weight = new_weight
    stmt.last_validated_at = now
    return True, None


def soft_delete(
    lattice: Lattice,
    archive: Archive,
    sid: str,
) -> tuple[bool, str | None]:
    """Move a statement to archived with archive_reason='deleted'.
    Rejects when immutable=true (§8)."""
    stmt = _find(lattice, sid)
    if stmt is None:
        return False, "id_not_found"
    if stmt.immutable:
        return False, "immutable"
    _archive(archive, stmt, reason="deleted")
    lattice.statements = [s for s in lattice.statements if s.id != sid]
    return True, None


def restore_from_archive(
    lattice: Lattice,
    archive: Archive,
    sid: str,
    *,
    weight: float | None = None,
) -> tuple[bool, str | None]:
    """Move an archived statement back to the active lattice. `weight`
    defaults to `final_weight` at archive time (§8)."""
    found = None
    for s in archive.statements:
        if s.id == sid:
            found = s
            break
    if found is None:
        return False, "id_not_found_in_archive"
    # Don't restore an id that's somehow back in the active lattice.
    if _find(lattice, sid) is not None:
        return False, "id_already_active"
    chosen_weight = weight if weight is not None else found.final_weight
    chosen_weight = max(0.0, min(1.0, float(chosen_weight)))
    now = _now_iso()
    stmt = Statement(
        id=found.id,
        text=found.text,
        weight=chosen_weight,
        weight_history=list(found.history) + [
            WeightHistoryEntry(
                ts=now,
                from_=found.final_weight,
                to=chosen_weight,
                reason=f"restored from archive (was {found.archive_reason})",
            )
        ],
        created_at=found.created_at or now,
        created_by=found.created_by or "restored",
        last_validated_at=now,
        applied_count=0,  # reset — restored statements re-validate from zero
        immutable=False,
    )
    lattice.statements.append(stmt)
    archive.statements = [s for s in archive.statements if s.id != sid]
    return True, None


__all__ = [
    "_PB_ID_RE",
    "find_near_duplicate",
    "insert_statement",
    "apply_op_adjust",
    "apply_op_merge",
    "resolve_cap_pressure",
    "apply_coach_proposals",
    "increment_relevant_ids",
    "is_settle_eligible",
    "is_stale_low_eligible",
    "is_stale_unused_eligible",
    "sweep_engine_actions",
    "override_weight",
    "soft_delete",
    "restore_from_archive",
]

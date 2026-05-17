"""Playbook mutate tests — spec §18.1.

Pure-function tests; no DB, no LLM, no event bus.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from server.playbook import config
from server.playbook.mutate import (
    _PB_ID_RE,
    _jaccard,
    _tokenize,
    apply_coach_proposals,
    apply_op_adjust,
    apply_op_merge,
    find_near_duplicate,
    increment_relevant_ids,
    insert_statement,
    is_settle_eligible,
    is_stale_low_eligible,
    is_stale_unused_eligible,
    override_weight,
    resolve_cap_pressure,
    restore_from_archive,
    soft_delete,
    sweep_engine_actions,
)
from server.playbook.store import (
    Archive,
    ArchivedStatement,
    Lattice,
    Statement,
    WeightHistoryEntry,
)


def _empty_pair() -> tuple[Lattice, Archive]:
    return (
        Lattice(schema_version=1, updated_at="now", statements=[]),
        Archive(schema_version=1, statements=[]),
    )


def _seed(weight: float = 0.5, immutable: bool = False, sid: str = "pb-001",
          created_at: str | None = None) -> Statement:
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    return Statement(
        id=sid, text=f"seed {sid}", weight=weight,
        weight_history=[
            WeightHistoryEntry(ts=created_at, from_=None, to=weight, reason="created"),
        ],
        created_at=created_at, created_by="test",
        last_validated_at=created_at, applied_count=0, immutable=immutable,
    )


# ---------------------------------------------------------------- adjust


def test_adjust_delta_cap_rejected() -> None:
    lat, _ = _empty_pair()
    lat.statements.append(_seed(weight=0.5))
    ok, err = apply_op_adjust(lat, sid="pb-001", delta=0.30, reason="too big")
    assert not ok
    assert err == "delta_exceeds_cap"


def test_adjust_immutable_rejected() -> None:
    lat, _ = _empty_pair()
    lat.statements.append(_seed(weight=1.0, immutable=True))
    ok, err = apply_op_adjust(lat, sid="pb-001", delta=0.10, reason="x")
    assert not ok
    assert err == "immutable"


def test_adjust_unknown_id_rejected() -> None:
    lat, _ = _empty_pair()
    ok, err = apply_op_adjust(lat, sid="pb-999", delta=0.10, reason="x")
    assert not ok
    assert err == "id_not_found"


def test_adjust_clamps_to_unit_interval() -> None:
    lat, _ = _empty_pair()
    lat.statements.append(_seed(weight=0.95))
    ok, err = apply_op_adjust(lat, sid="pb-001", delta=0.20, reason="x")
    assert ok
    assert err is None
    assert lat.statements[0].weight == 1.0  # clamped


def test_adjust_appends_history_entry() -> None:
    lat, _ = _empty_pair()
    lat.statements.append(_seed(weight=0.5))
    initial_history_len = len(lat.statements[0].weight_history)
    apply_op_adjust(lat, sid="pb-001", delta=0.10, reason="rationale")
    assert len(lat.statements[0].weight_history) == initial_history_len + 1
    assert lat.statements[0].weight_history[-1].reason == "rationale"


# ---------------------------------------------------------------- merge


def test_merge_keeps_max_weight_and_sums_applied_count() -> None:
    lat, arch = _empty_pair()
    a = _seed(weight=0.7, sid="pb-001"); a.applied_count = 5
    b = _seed(weight=0.85, sid="pb-002"); b.applied_count = 3
    lat.statements.extend([a, b])
    ok, err = apply_op_merge(lat, arch, keep_id="pb-001", drop_id="pb-002", reason="dupe")
    assert ok and err is None
    assert len(lat.statements) == 1
    assert lat.statements[0].id == "pb-001"
    assert lat.statements[0].weight == 0.85  # max of 0.7 and 0.85
    assert lat.statements[0].applied_count == 8  # 5 + 3
    assert len(arch.statements) == 1
    assert arch.statements[0].id == "pb-002"
    assert arch.statements[0].archive_reason == "merged"
    assert arch.statements[0].merged_into == "pb-001"


def test_merge_immutable_rejected() -> None:
    lat, arch = _empty_pair()
    a = _seed(weight=0.7, sid="pb-001", immutable=True)
    b = _seed(weight=0.85, sid="pb-002")
    lat.statements.extend([a, b])
    ok, err = apply_op_merge(lat, arch, keep_id="pb-001", drop_id="pb-002", reason="x")
    assert not ok
    assert err == "immutable"


def test_merge_self_rejected() -> None:
    lat, arch = _empty_pair()
    lat.statements.append(_seed(sid="pb-001"))
    ok, err = apply_op_merge(lat, arch, keep_id="pb-001", drop_id="pb-001", reason="x")
    assert not ok
    assert err == "self_merge"


def test_merge_last_validated_at_max() -> None:
    lat, arch = _empty_pair()
    a = _seed(weight=0.7, sid="pb-001")
    a.last_validated_at = "2026-05-01T00:00:00Z"
    b = _seed(weight=0.6, sid="pb-002")
    b.last_validated_at = "2026-05-08T00:00:00Z"
    lat.statements.extend([a, b])
    apply_op_merge(lat, arch, keep_id="pb-001", drop_id="pb-002", reason="x")
    assert lat.statements[0].last_validated_at == "2026-05-08T00:00:00Z"


# ---------------------------------------------------------------- caps (§5.7)


def test_resolve_cap_pressure_branch_a_within_soft() -> None:
    survivors, dropped, hard = resolve_cap_pressure(active_count=50, creation_count=10)
    assert survivors == 10
    assert dropped == 0
    assert not hard


def test_resolve_cap_pressure_branch_b_soft_to_hard() -> None:
    survivors, dropped, hard = resolve_cap_pressure(active_count=55, creation_count=8)
    # active=55, creation=8 → pressure=63 (between soft 60 and hard 80)
    # survivors = 60 - 55 = 5
    assert survivors == 5
    assert dropped == 3
    assert not hard


def test_resolve_cap_pressure_branch_c_hard_cap() -> None:
    survivors, dropped, hard = resolve_cap_pressure(active_count=100, creation_count=20)
    assert survivors == 0
    assert dropped == 20
    assert hard


@pytest.mark.parametrize(
    ("active", "creations", "survivors", "dropped", "hard"),
    [
        (59, 2, 1, 1, False),
        (60, 1, 0, 1, False),
        (79, 1, 0, 1, False),
        (79, 2, 0, 2, True),
        (80, 1, 0, 1, True),
    ],
)
def test_resolve_cap_pressure_boundaries(
    active: int,
    creations: int,
    survivors: int,
    dropped: int,
    hard: bool,
) -> None:
    got = resolve_cap_pressure(active_count=active, creation_count=creations)
    assert got == (survivors, dropped, hard)


# ---------------------------------------------------------------- proposals (§5.6)


def test_apply_coach_proposals_op_order_merge_first() -> None:
    """Merges apply BEFORE creates and adjusts, even if input order is different."""
    lat, arch = _empty_pair()
    lat.statements.extend([
        _seed(weight=0.5, sid="pb-001"),
        _seed(weight=0.6, sid="pb-002"),
    ])
    operations = [
        # adjust ordered first in input but should apply after merge
        {"op": "adjust", "id": "pb-001", "delta": 0.10, "reason": "x"},
        {"op": "merge", "keep_id": "pb-001", "drop_id": "pb-002", "reason": "dupe"},
    ]
    applied, rejected, hard = apply_coach_proposals(
        lat, arch, operations, creation_weight=0.6,
    )
    assert hard is False
    assert len(applied) == 2
    # pb-001 should have absorbed pb-002 (weight max=0.6) AND been adjusted
    # by +0.10 → 0.7
    assert lat.statements[0].weight == pytest.approx(0.7)


def test_apply_coach_proposals_cross_op_conflict() -> None:
    """Adjust on an id archived by an earlier merge is rejected."""
    lat, arch = _empty_pair()
    lat.statements.extend([
        _seed(weight=0.5, sid="pb-001"),
        _seed(weight=0.6, sid="pb-002"),
    ])
    operations = [
        {"op": "merge", "keep_id": "pb-001", "drop_id": "pb-002", "reason": "x"},
        {"op": "adjust", "id": "pb-002", "delta": 0.10, "reason": "x"},
    ]
    applied, rejected, _ = apply_coach_proposals(
        lat, arch, operations, creation_weight=0.6,
    )
    assert any(op.get("op") == "adjust" and op.get("reason") == "id_archived_in_same_run"
               for op in rejected)


def test_apply_coach_proposals_creation_with_duplicate_text() -> None:
    """Near-duplicate (Jaccard ≥ 0.7) creations are rejected."""
    lat, arch = _empty_pair()
    lat.statements.append(
        Statement(id="pb-001", text="audit every code change", weight=0.85,
                  weight_history=[], created_at="now", created_by="b",
                  applied_count=0, immutable=False),
    )
    operations = [
        {"op": "create", "text": "audit every code change please",
         "weight": 0.6, "reason": "x"},
    ]
    applied, rejected, _ = apply_coach_proposals(
        lat, arch, operations, creation_weight=0.6,
    )
    assert applied == []
    assert any(r.get("reason") == "near_duplicate" for r in rejected)


def test_apply_coach_proposals_branch_b_drops_creations_from_end() -> None:
    lat, arch = _empty_pair()
    lat.statements.extend(_seed(sid=f"pb-{i:03d}") for i in range(1, 60))
    operations = [
        {"op": "create", "text": f"new branch b pattern {i}", "weight": 0.6, "reason": "x"}
        for i in range(3)
    ]
    applied, rejected, hard = apply_coach_proposals(
        lat, arch, operations, creation_weight=0.6,
    )
    assert hard is False
    assert [op["text"] for op in applied if op.get("op") == "create"] == [
        "new branch b pattern 0",
    ]
    assert [op["text"] for op in rejected if op.get("reason") == "soft_cap_pressure"] == [
        "new branch b pattern 1",
        "new branch b pattern 2",
    ]


def test_apply_coach_proposals_branch_c_rejects_all_creations() -> None:
    lat, arch = _empty_pair()
    lat.statements.extend(_seed(sid=f"pb-{i:03d}") for i in range(1, 80))
    operations = [
        {"op": "create", "text": "first hard pressure creation", "weight": 0.6, "reason": "x"},
        {"op": "create", "text": "second hard pressure creation", "weight": 0.6, "reason": "x"},
    ]
    applied, rejected, hard = apply_coach_proposals(
        lat, arch, operations, creation_weight=0.6,
    )
    assert hard is True
    assert not [op for op in applied if op.get("op") == "create"]
    assert [op.get("reason") for op in rejected] == [
        "hard_cap_pressure",
        "hard_cap_pressure",
    ]


def test_pre_creation_hygiene_frees_capacity_before_trimming() -> None:
    lat, arch = _empty_pair()
    old = (datetime.now(timezone.utc) - timedelta(days=config.STALE_UNUSED_DAYS + 1)).isoformat()
    lat.statements.append(_seed(sid="pb-001", created_at=old))
    lat.statements.extend(_seed(sid=f"pb-{i:03d}") for i in range(2, 61))
    engine_actions: list[dict[str, Any]] = []

    applied, rejected, hard = apply_coach_proposals(
        lat,
        arch,
        [{"op": "create", "text": "capacity freed by stale hygiene", "weight": 0.6, "reason": "x"}],
        creation_weight=0.6,
        engine_actions=engine_actions,
    )

    assert hard is False
    assert not rejected
    assert [a["action"] for a in engine_actions] == ["stale_unused"]
    assert applied[0]["op"] == "create"
    assert len(lat.statements) == config.SOFT_STATEMENT_CAP


def test_apply_coach_proposals_unknown_op_rejected() -> None:
    lat, arch = _empty_pair()
    operations = [{"op": "spaceship", "id": "pb-001"}]
    applied, rejected, _ = apply_coach_proposals(
        lat, arch, operations, creation_weight=0.6,
    )
    assert applied == []
    assert rejected[0]["reason"] == "unknown_op"


# ---------------------------------------------------------------- relevant_ids


def test_increment_relevant_ids_dedupes() -> None:
    lat, _ = _empty_pair()
    lat.statements.append(_seed(sid="pb-001"))
    lat.statements.append(_seed(sid="pb-002"))
    n = increment_relevant_ids(lat, ["pb-001", "pb-001", "pb-002"])
    assert n == 2  # de-duplicated
    assert lat.statements[0].applied_count == 1
    assert lat.statements[1].applied_count == 1


def test_increment_relevant_ids_skips_malformed() -> None:
    lat, _ = _empty_pair()
    lat.statements.append(_seed(sid="pb-001"))
    n = increment_relevant_ids(lat, [
        "pb-001",          # valid
        "",                # empty
        "PB-002",          # wrong case
        "not-a-pb-id",     # bad shape
        123,               # non-string
        {"id": "pb-001"},  # nested object
    ])
    assert n == 1
    assert lat.statements[0].applied_count == 1


def test_increment_relevant_ids_skips_unknown_id() -> None:
    lat, _ = _empty_pair()
    lat.statements.append(_seed(sid="pb-001"))
    n = increment_relevant_ids(lat, ["pb-001", "pb-999"])
    assert n == 1
    assert lat.statements[0].applied_count == 1


# ---------------------------------------------------------------- settle / stale


def _old_history(stmt: Statement, days_ago: int) -> None:
    """Backdate the first weight_history entry."""
    old_ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    if stmt.weight_history:
        stmt.weight_history[0].ts = old_ts
    stmt.created_at = old_ts


def test_settle_eligible_requires_old_history() -> None:
    """Same-day-created high-weight statement must NOT settle."""
    lat, _ = _empty_pair()
    s = _seed(weight=0.97, sid="pb-001")
    # `created_at` is now; weight_history has one entry from now
    assert not is_settle_eligible(s)


def test_settle_eligible_with_old_history() -> None:
    lat, _ = _empty_pair()
    s = _seed(weight=0.97, sid="pb-001")
    _old_history(s, days_ago=10)
    assert is_settle_eligible(s)


def test_settle_skips_immutable() -> None:
    s = _seed(weight=1.0, sid="pb-001", immutable=True)
    _old_history(s, days_ago=10)
    assert not is_settle_eligible(s)


def test_stale_low_requires_old_history() -> None:
    s = _seed(weight=0.10, sid="pb-001")
    assert not is_stale_low_eligible(s)
    _old_history(s, days_ago=10)
    assert is_stale_low_eligible(s)


def test_stale_unused_30_days_threshold() -> None:
    s = _seed(weight=0.5, sid="pb-001")
    s.applied_count = 0
    # Recent: created today
    assert not is_stale_unused_eligible(s)
    # 31 days ago + 0 applied
    _old_history(s, days_ago=31)
    assert is_stale_unused_eligible(s)


def test_stale_unused_skipped_when_applied() -> None:
    s = _seed(weight=0.5, sid="pb-001")
    s.applied_count = 1
    _old_history(s, days_ago=31)
    assert not is_stale_unused_eligible(s)


def test_sweep_engine_actions_archives_and_returns_records() -> None:
    lat, arch = _empty_pair()
    a = _seed(weight=0.97, sid="pb-001"); _old_history(a, days_ago=10)
    b = _seed(weight=0.10, sid="pb-002"); _old_history(b, days_ago=10)
    c = _seed(weight=0.5, sid="pb-003"); _old_history(c, days_ago=31)  # unused
    lat.statements.extend([a, b, c])
    actions = sweep_engine_actions(lat, arch)
    action_kinds = sorted({a["action"] for a in actions})
    assert action_kinds == ["settle", "stale_low", "stale_unused"]
    assert len(lat.statements) == 0
    assert len(arch.statements) == 3


# ---------------------------------------------------------------- override / restore / delete


def test_override_weight_immutable_rejected() -> None:
    lat, _ = _empty_pair()
    lat.statements.append(_seed(weight=1.0, sid="pb-001", immutable=True))
    ok, err = override_weight(lat, "pb-001", weight=0.0, actor="human")
    assert not ok
    assert err == "immutable"


def test_override_weight_records_human_override_reason() -> None:
    lat, _ = _empty_pair()
    lat.statements.append(_seed(weight=0.5, sid="pb-001"))
    override_weight(lat, "pb-001", weight=0.0, actor="dashboard")
    assert lat.statements[0].weight == 0.0
    last = lat.statements[0].weight_history[-1]
    assert "human_override" in last.reason


def test_soft_delete_archives_with_reason() -> None:
    lat, arch = _empty_pair()
    lat.statements.append(_seed(sid="pb-001"))
    ok, err = soft_delete(lat, arch, "pb-001")
    assert ok
    assert lat.statements == []
    assert arch.statements[0].archive_reason == "deleted"


def test_restore_from_archive_round_trip() -> None:
    lat, arch = _empty_pair()
    arch.statements.append(ArchivedStatement(
        id="pb-001", text="x", final_weight=0.7,
        archived_at="2026-05-08T00:00:00Z",
        archive_reason="stale_low",
        history=[],
        created_at="2026-04-01T00:00:00Z", created_by="b",
    ))
    ok, err = restore_from_archive(lat, arch, "pb-001", weight=None)
    assert ok
    assert len(lat.statements) == 1
    assert lat.statements[0].weight == 0.7  # default to final_weight
    assert lat.statements[0].applied_count == 0  # reset
    assert arch.statements == []


# ---------------------------------------------------------------- helpers


def test_pb_id_regex() -> None:
    assert _PB_ID_RE.match("pb-001")
    assert _PB_ID_RE.match("pb-12345")
    assert not _PB_ID_RE.match("PB-001")
    assert not _PB_ID_RE.match("pb-")
    assert not _PB_ID_RE.match("playbook-001")


def test_tokenize_strips_stopwords() -> None:
    toks = _tokenize("The quick brown fox in the field")
    assert "the" not in toks
    assert "in" not in toks
    assert "quick" in toks
    assert "fox" in toks


def test_jaccard_basic() -> None:
    a = {"audit", "code", "change"}
    b = {"audit", "code", "modification"}
    j = _jaccard(a, b)
    # Intersection = 2 (audit, code), union = 4
    assert j == pytest.approx(0.5)


def test_jaccard_self_one() -> None:
    a = {"x", "y"}
    assert _jaccard(a, a) == 1.0


def test_find_near_duplicate_threshold() -> None:
    lat, _ = _empty_pair()
    lat.statements.append(
        Statement(id="pb-001", text="audit every code change", weight=0.85,
                  weight_history=[], created_at="now", created_by="b",
                  applied_count=0, immutable=False),
    )
    # Very similar — should find
    dup = find_near_duplicate(lat, "audit every code")
    assert dup is not None
    # Quite different — should NOT find
    unrel = find_near_duplicate(lat, "use plan-mode for big tasks")
    assert unrel is None

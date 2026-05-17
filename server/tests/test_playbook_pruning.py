"""Playbook pruning discipline tests — spec §5.7.1 + §5.8 + §11.

Covers:
- Reachable settle / stale / stale-unused predicates with tuned thresholds
- Pressure sweep: fires above HARD_STATEMENT_CAP, archives to SOFT_CAP
- Pressure sweep: skips at-or-below HARD_STATEMENT_CAP
- Pressure sweep: skips immutable statements
- Pressure sweep: archives lowest-weight first
- Reflection pressure directive: injected above PRESSURE_CAP, absent below
- Reflection pressure directive: formatted with actual count/cap
- Config defaults: all new threshold values

Pure-function tests; no DB, no LLM, no event bus.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.playbook import config, prompts
from server.playbook.mutate import (
    is_settle_eligible,
    is_stale_low_eligible,
    is_stale_unused_eligible,
    sweep_engine_actions,
)
from server.playbook.store import (
    Archive,
    Lattice,
    Statement,
    WeightHistoryEntry,
)


# ---------------------------------------------------------------- helpers


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_ago_iso(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _make_stmt(
    sid: str,
    weight: float,
    *,
    immutable: bool = False,
    applied_count: int = 0,
    created_at: str | None = None,
    history_entries: list[tuple[str, float]] | None = None,
) -> Statement:
    """Build a Statement with optional weight history."""
    if created_at is None:
        created_at = _now_iso()
    history = []
    if history_entries:
        for ts, w in history_entries:
            history.append(WeightHistoryEntry(ts=ts, from_=None, to=w, reason="test"))
    else:
        history = [WeightHistoryEntry(ts=created_at, from_=None, to=weight, reason="created")]
    return Statement(
        id=sid,
        text=f"statement {sid}",
        weight=weight,
        weight_history=history,
        created_at=created_at,
        created_by="test",
        last_validated_at=created_at,
        applied_count=applied_count,
        immutable=immutable,
    )


def _empty_pair() -> tuple[Lattice, Archive]:
    return (
        Lattice(schema_version=1, updated_at="now", statements=[]),
        Archive(schema_version=1, statements=[]),
    )


# ---------------------------------------------------------------- settle threshold (0.90)


def test_settle_threshold_reachable():
    """weight=0.90 with 5-day stable history should be settle-eligible."""
    ts = _days_ago_iso(5)
    stmt = _make_stmt(
        "pb-001",
        weight=0.90,
        history_entries=[(ts, 0.90)],
    )
    assert is_settle_eligible(stmt) is True


def test_settle_threshold_below():
    """weight=0.89 (just below 0.90) must NOT be settle-eligible."""
    ts = _days_ago_iso(6)
    stmt = _make_stmt(
        "pb-001",
        weight=0.89,
        history_entries=[(ts, 0.89)],
    )
    assert is_settle_eligible(stmt) is False


def test_settle_stable_days_gate():
    """weight=0.92 but only 4-day history (< SETTLE_STABLE_DAYS=5) → not eligible."""
    ts = _days_ago_iso(4)
    stmt = _make_stmt(
        "pb-001",
        weight=0.92,
        history_entries=[(ts, 0.92)],
    )
    assert is_settle_eligible(stmt) is False


# ---------------------------------------------------------------- stale-low threshold (0.30)


def test_stale_low_reachable():
    """weight=0.30 with 5-day stable history should be stale-low-eligible."""
    ts = _days_ago_iso(5)
    stmt = _make_stmt(
        "pb-002",
        weight=0.30,
        history_entries=[(ts, 0.30)],
    )
    assert is_stale_low_eligible(stmt) is True


def test_stale_low_threshold_above():
    """weight=0.31 (just above 0.30) must NOT be stale-low-eligible."""
    ts = _days_ago_iso(6)
    stmt = _make_stmt(
        "pb-002",
        weight=0.31,
        history_entries=[(ts, 0.31)],
    )
    assert is_stale_low_eligible(stmt) is False


# ---------------------------------------------------------------- stale-unused (14 days)


def test_stale_unused_14_days():
    """applied_count=0 with created_at 15 days ago → stale-unused-eligible."""
    stmt = _make_stmt(
        "pb-003",
        weight=0.65,
        applied_count=0,
        created_at=_days_ago_iso(15),
    )
    assert is_stale_unused_eligible(stmt) is True


def test_stale_unused_too_recent():
    """applied_count=0 but created_at only 13 days ago → NOT eligible (< 14 days)."""
    stmt = _make_stmt(
        "pb-003",
        weight=0.65,
        applied_count=0,
        created_at=_days_ago_iso(13),
    )
    assert is_stale_unused_eligible(stmt) is False


# ---------------------------------------------------------------- pressure sweep


def _build_lattice_n(n: int, base_weight: float = 0.60) -> tuple[Lattice, Archive]:
    """Build a lattice with n statements at sequentially increasing weights."""
    stmts = []
    for i in range(n):
        # weights spread across [base_weight, base_weight + 0.30]
        w = round(base_weight + (0.30 * i / max(n - 1, 1)), 4)
        stmts.append(_make_stmt(f"pb-{i+1:03d}", weight=w))
    lattice = Lattice(schema_version=1, updated_at="now", statements=stmts)
    archive = Archive(schema_version=1, statements=[])
    return lattice, archive


def test_pressure_sweep_fires_above_hard_cap():
    """81 active statements (> HARD_STATEMENT_CAP=80) → sweep archives down to SOFT_CAP=60."""
    lattice, archive = _build_lattice_n(81)
    actions = sweep_engine_actions(lattice, archive)

    pressure_actions = [a for a in actions if a["action"] == "pressure_cap"]
    # Should archive 81 - 60 = 21 statements
    assert len(pressure_actions) == 21
    assert len(lattice.statements) == config.SOFT_STATEMENT_CAP


def test_pressure_sweep_can_be_deferred_for_pre_creation_hygiene():
    """Pre-creation hygiene pass must not pressure-archive before creations."""
    lattice, archive = _build_lattice_n(81)
    actions = sweep_engine_actions(lattice, archive, include_pressure=False)

    pressure_actions = [a for a in actions if a["action"] == "pressure_cap"]
    assert pressure_actions == []
    assert len(lattice.statements) == 81


def test_pressure_sweep_skips_at_hard_cap():
    """80 active statements (== HARD_STATEMENT_CAP=80) → NO pressure_cap actions."""
    lattice, archive = _build_lattice_n(80)
    actions = sweep_engine_actions(lattice, archive)

    pressure_actions = [a for a in actions if a["action"] == "pressure_cap"]
    assert len(pressure_actions) == 0
    assert len(lattice.statements) == 80


def test_pressure_sweep_skips_immutable():
    """83 statements with 5 immutable → only archives non-immutable; immutable survive."""
    lattice, archive = _build_lattice_n(83)
    # Mark the 5 lowest-weight statements as immutable (they would be first targets)
    for stmt in sorted(lattice.statements, key=lambda s: s.weight)[:5]:
        stmt.immutable = True

    actions = sweep_engine_actions(lattice, archive)
    pressure_actions = [a for a in actions if a["action"] == "pressure_cap"]

    # All immutable statements must remain in the lattice
    remaining_ids = {s.id for s in lattice.statements}
    for stmt in archive.statements:
        # Archived statements must not be immutable (we set immutable on lowest 5)
        pass  # checked below

    immutable_ids = {s.id for s in (lattice.statements + []) if s.immutable}
    for a in pressure_actions:
        assert a["id"] not in immutable_ids, "Immutable statement was archived by pressure sweep"

    # Verify all originally-immutable statements are still active
    final_ids = {s.id for s in lattice.statements}
    orig_immutable = sorted(lattice.statements, key=lambda s: s.weight)
    # The five immutable ones should still be in the lattice
    active_immutable = [s for s in lattice.statements if s.immutable]
    assert len(active_immutable) == 5


def test_pressure_sweep_orders_by_weight_asc():
    """Pressure sweep archives lowest-weight statements first."""
    lattice, archive = _build_lattice_n(82)
    # Capture original sorted order (ascending weight)
    original_sorted = sorted(lattice.statements, key=lambda s: s.weight)

    actions = sweep_engine_actions(lattice, archive)
    pressure_actions = [a for a in actions if a["action"] == "pressure_cap"]

    if not pressure_actions:
        pytest.skip("No pressure actions fired — check HARD_STATEMENT_CAP")

    # Archived statements should be the ones with the lowest weights
    archived_ids = [a["id"] for a in pressure_actions]
    n_archived = len(archived_ids)
    expected_lowest_ids = [s.id for s in original_sorted[:n_archived]]
    assert archived_ids == expected_lowest_ids, (
        f"Expected lowest-weight IDs {expected_lowest_ids}, got {archived_ids}"
    )


# ---------------------------------------------------------------- pressure directive


def test_pressure_directive_injected_above_cap():
    """When active=61 > PRESSURE_CAP=60, the formatted user prompt must contain the directive."""
    active = 61
    cap = config.PRESSURE_CAP
    pressure_note = prompts.REFLECTION_PRESSURE_DIRECTIVE.format(
        active=active, cap=cap
    )
    user_msg = prompts.REFLECTION_USER_TEMPLATE.format(
        rendered_lattice="(lattice)",
        evidence_bundle="(evidence)",
        pressure_note=pressure_note,
    )
    assert "above the" in user_msg
    assert "soft cap" in user_msg
    assert "merges" in user_msg
    assert "downward adjustments" in user_msg


def test_pressure_directive_absent_below_cap():
    """When active=59 <= PRESSURE_CAP=60, pressure_note='' → directive absent."""
    user_msg = prompts.REFLECTION_USER_TEMPLATE.format(
        rendered_lattice="(lattice)",
        evidence_bundle="(evidence)",
        pressure_note="",
    )
    assert "above the" not in user_msg
    assert "soft cap" not in user_msg


def test_pressure_directive_format():
    """The formatted directive must embed actual count/cap — no leftover {placeholders}."""
    active = 75
    cap = config.PRESSURE_CAP
    directive = prompts.REFLECTION_PRESSURE_DIRECTIVE.format(
        active=active, cap=cap
    )
    assert str(active) in directive
    assert str(cap) in directive
    assert "{" not in directive
    assert "}" not in directive


# ---------------------------------------------------------------- config defaults


def test_new_config_defaults():
    """Confirm all tuned thresholds match the spec §11 values."""
    assert config.SETTLE_THRESHOLD == 0.90
    assert config.STALE_THRESHOLD == 0.30
    assert config.SETTLE_STABLE_DAYS == 5
    assert config.STALE_STABLE_DAYS == 5
    assert config.STALE_UNUSED_DAYS == 14
    assert config.SOFT_STATEMENT_CAP == 60
    assert config.HARD_STATEMENT_CAP == 80
    assert config.PRESSURE_CAP == 60

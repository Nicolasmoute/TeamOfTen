"""Phase 3 tests — `server.compass.mutate` in-memory mutation helpers.

Pure-state operations; no LLM, no DB. Verifies:
  - Weight clamping (delta and absolute) honors caps
  - History entries are recorded with run_id + source
  - Archived statements are immune to update / settle / reformulate
  - Region merge re-tags BOTH active and archived statements
  - Duplicate cluster merge archives losers + creates merged stmt
  - Truth helpers manage the 1-based index list
"""

from __future__ import annotations

import pytest

from server.compass import mutate
from server.compass.store import (
    LatticeState,
    Region,
    Statement,
)


def _make_state(**kwargs) -> LatticeState:
    return LatticeState(project_id=kwargs.get("project_id", "alpha"))


def _add(state: LatticeState, **fields) -> Statement:
    s = Statement(
        id=fields.pop("id"),
        text=fields.pop("text", "x"),
        region=fields.pop("region", "general"),
        weight=fields.pop("weight", 0.5),
        created_at=fields.pop("created_at", "t"),
        **fields,
    )
    state.statements.append(s)
    return s


# ----------------------------------------------------------- updates


def test_apply_statement_updates_clamps_delta() -> None:
    state = _make_state()
    s = _add(state, id="s1", weight=0.5)
    n = mutate.apply_statement_updates(
        state,
        [{"id": "s1", "delta": 0.99, "rationale": "huge"}],
        run_id="r1",
        source="passive",
        delta_max=0.15,
    )
    assert n == 1
    assert s.weight == pytest.approx(0.65)  # clamped to +0.15
    assert s.history[0]["delta"] == pytest.approx(0.15)
    assert s.history[0]["source"] == "passive"
    assert s.history[0]["run_id"] == "r1"
    assert s.history[0]["rationale"] == "huge"


def test_apply_statement_updates_clamps_to_unit_interval() -> None:
    state = _make_state()
    s = _add(state, id="s1", weight=0.95)
    mutate.apply_statement_updates(
        state,
        [{"id": "s1", "delta": 0.5, "rationale": "overrun"}],
        run_id="r1",
        source="answer",
        delta_max=0.5,
    )
    assert s.weight == pytest.approx(1.0)  # clamped at the ceiling
    assert s.history[0]["delta"] == pytest.approx(0.05)


def test_apply_statement_updates_skips_archived() -> None:
    state = _make_state()
    s = _add(state, id="s1", weight=0.5, archived=True)
    n = mutate.apply_statement_updates(
        state,
        [{"id": "s1", "delta": 0.1, "rationale": "x"}],
        run_id="r1",
        source="passive",
        delta_max=0.15,
    )
    assert n == 0
    assert s.weight == 0.5
    assert s.history == []


def test_apply_statement_updates_skips_unknown_id() -> None:
    state = _make_state()
    n = mutate.apply_statement_updates(
        state,
        [{"id": "s99", "delta": 0.1, "rationale": "x"}],
        run_id="r1",
        source="passive",
        delta_max=0.15,
    )
    assert n == 0


def test_apply_statement_updates_skips_zero_delta_after_clamp() -> None:
    state = _make_state()
    s = _add(state, id="s1", weight=0.0)
    n = mutate.apply_statement_updates(
        state,
        [{"id": "s1", "delta": -0.5, "rationale": "would go below 0"}],
        run_id="r1",
        source="answer",
        delta_max=0.5,
    )
    # Floor-clamp leaves weight at 0.0; effective delta = 0; skip.
    assert n == 0
    assert s.weight == 0.0
    assert s.history == []


# ----------------------------------------------------- new statements


def test_apply_new_statements_caps_at_two() -> None:
    state = _make_state()
    proposals = [
        {"text": "A", "region": "x"},
        {"text": "B", "region": "y"},
        {"text": "C", "region": "z"},
    ]
    added = mutate.apply_new_statements(
        state, proposals, run_id="r1", source="passive", cap=2,
    )
    assert len(added) == 2
    assert {s.text for s in added} == {"A", "B"}
    assert added[0].weight == 0.5
    assert added[0].history[0]["source"] == "passive"
    # Regions x and y were created on demand.
    assert {r.name for r in state.active_regions()} == {"x", "y"}


def test_apply_new_statements_assigns_monotonic_ids() -> None:
    state = _make_state()
    _add(state, id="s5")
    _add(state, id="s9", archived=True)
    proposals = [
        {"text": "A", "region": "x"},
        {"text": "B", "region": "x"},
    ]
    added = mutate.apply_new_statements(state, proposals, run_id="r1", source="x")
    assert [s.id for s in added] == ["s10", "s11"]


def test_ensure_region_idempotent() -> None:
    state = _make_state()
    r1 = mutate.ensure_region(state, "pricing")
    r2 = mutate.ensure_region(state, "pricing")
    assert r1 is r2
    assert len([r for r in state.regions if r.name == "pricing"]) == 1


def test_ensure_region_un_merges() -> None:
    """An LLM proposal targeting a previously-merged region restores
    it (clears `merged_into`)."""
    state = _make_state()
    state.regions.append(
        Region(name="billing", created_at="t", created_by="compass", merged_into="pricing")
    )
    r = mutate.ensure_region(state, "billing")
    assert r.merged_into is None


# ----------------------------------------------------- region merge


def test_apply_region_merge_retags_active_and_archived() -> None:
    state = _make_state()
    state.regions.append(Region(name="pricing", created_at="t"))
    state.regions.append(Region(name="billing", created_at="t"))
    state.regions.append(Region(name="payments", created_at="t"))
    a1 = _add(state, id="s1", region="billing")
    a2 = _add(state, id="s2", region="payments")
    a3 = _add(state, id="s3", region="billing", archived=True)
    a4 = _add(state, id="s4", region="auth")  # untouched

    n = mutate.apply_region_merge(
        state, from_=["billing", "payments"], to="pricing", run_id="r5",
    )
    assert n == 3
    assert a1.region == "pricing"
    assert a2.region == "pricing"
    assert a3.region == "pricing"
    assert a4.region == "auth"
    # The merged-away regions stay in the list with merged_into set.
    by_name = {r.name: r for r in state.regions}
    assert by_name["billing"].merged_into == "pricing"
    assert by_name["payments"].merged_into == "pricing"
    assert by_name["pricing"].merged_into is None
    assert state.region_merge_history[0].from_ == ["billing", "payments"]
    assert state.region_merge_history[0].to == "pricing"


def test_apply_region_merge_creates_destination_if_missing() -> None:
    state = _make_state()
    state.regions.append(Region(name="billing", created_at="t"))
    _add(state, id="s1", region="billing")
    mutate.apply_region_merge(state, from_=["billing"], to="pricing", run_id="r1")
    assert "pricing" in {r.name for r in state.active_regions()}


# ---------------------------------------------------------- settle


def test_settle_statement_archives_and_records_history() -> None:
    state = _make_state()
    s = _add(state, id="s1", weight=0.91)
    s.settle_proposed = True
    out = mutate.settle_statement(
        state, "s1", weight=1.0, direction="yes", run_id="r5", by_human=True,
    )
    assert out is s
    assert s.archived is True
    assert s.archived_at is not None
    assert s.weight == 1.0
    assert s.settled_as == "yes"
    assert s.settled_by_human is True
    assert s.settle_proposed is False
    last = s.history[-1]
    assert last["source"] == "settle:human"
    assert last["delta"] == pytest.approx(0.09)


def test_settle_no_op_on_already_archived() -> None:
    state = _make_state()
    _add(state, id="s1", archived=True)
    out = mutate.settle_statement(
        state, "s1", weight=1.0, direction="yes", run_id="r1",
    )
    assert out is None


# ------------------------------------------------------ reformulate


def test_reformulate_resets_weight_and_history() -> None:
    state = _make_state()
    s = _add(state, id="s1", weight=0.42, region="pricing")
    s.history = [{"run_id": "r1", "delta": 0.1, "rationale": "stale entry", "source": "passive"}]
    s.stale_proposed = True
    out = mutate.reformulate_statement(
        state, "s1", "Pricing favors per-task billing", run_id="r5",
    )
    assert out is s
    assert s.text == "Pricing favors per-task billing"
    assert s.weight == 0.5
    assert s.reformulated is True
    assert s.stale_proposed is False
    assert len(s.history) == 1  # cleared except for the reformulation entry
    assert s.history[0]["source"] == "reformulation"


def test_keep_stale_clears_flag_and_marks() -> None:
    state = _make_state()
    s = _add(state, id="s1")
    s.stale_proposed = True
    out = mutate.keep_stale(state, "s1")
    assert out is s
    assert s.stale_proposed is False
    assert s.kept_stale is True


def test_retire_statement_archives_with_retired_marker() -> None:
    state = _make_state()
    s = _add(state, id="s1")
    s.stale_proposed = True
    out = mutate.retire_statement(state, "s1", run_id="r1")
    assert out is s
    assert s.archived is True
    assert s.settled_as == "retired"
    assert s.stale_proposed is False
    assert s.history[-1]["source"] == "stale:retire"


# -------------------------------------------------- merge duplicates


def test_merge_duplicate_cluster_archives_losers() -> None:
    state = _make_state()
    state.regions.append(Region(name="customers", created_at="t"))
    a = _add(state, id="s1", text="Customers are technical", weight=0.7, region="customers")
    b = _add(state, id="s2", text="Our customers are engineers", weight=0.6, region="customers")
    out = mutate.merge_duplicate_cluster(
        state, ["s1", "s2"],
        merged_text="Customers are technical (engineers)",
        merged_weight=0.65,
        region="customers",
        run_id="r9",
    )
    assert out is not None
    assert out.merged is True
    assert sorted(out.merged_from) == ["s1", "s2"]
    assert out.weight == pytest.approx(0.65)
    assert a.archived and a.settled_as == "merged"
    assert b.archived and b.settled_as == "merged"
    assert a.history[-1]["rationale"].endswith(out.id)


def test_merge_duplicate_cluster_requires_at_least_two_alive() -> None:
    state = _make_state()
    _add(state, id="s1", archived=True)
    _add(state, id="s2")
    out = mutate.merge_duplicate_cluster(
        state, ["s1", "s2"],
        merged_text="x", merged_weight=0.5, region="r", run_id="r1",
    )
    assert out is None


# -------------------------------------------------- manual override


def test_manual_weight_override_marks_manually_set() -> None:
    state = _make_state()
    s = _add(state, id="s1", weight=0.5)
    out = mutate.manual_weight_override(state, "s1", 0.83, run_id="r1")
    assert out is s
    assert s.weight == pytest.approx(0.83)
    assert s.manually_set is True
    assert s.history[-1]["source"] == "manual"
    # Critical: override must NOT archive (spec §10.2 + §14.17.2).
    assert s.archived is False


# ----------------------------------------------------- restore


def test_restore_statement_clears_archive_flags() -> None:
    state = _make_state()
    s = _add(state, id="s1", archived=True, archived_at="t", settled_as="yes",
             settled_by_human=True)
    out = mutate.restore_statement(state, "s1")
    assert out is s
    assert s.archived is False
    assert s.archived_at is None
    assert s.settled_as is None
    assert s.settled_by_human is False

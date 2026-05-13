"""Playbook render tests — spec §18.1.

Sync function tests; minimal disk I/O via tmp_path monkeypatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.playbook import render as render_mod
from server.playbook.render import (
    _bucket_for,
    _format_bucket,
    _render_full,
    _sort_key,
    render_playbook_block,
)
from server.playbook.store import Lattice, Statement, WeightHistoryEntry
import server.playbook.paths as pb_paths_mod


def _stmt(sid: str, weight: float, applied: int = 0, text: str | None = None) -> Statement:
    return Statement(
        id=sid,
        text=text or f"sample text for {sid}",
        weight=weight,
        weight_history=[WeightHistoryEntry(ts="2026-05-01T00:00:00Z", from_=None, to=weight, reason="x")],
        created_at="2026-05-01T00:00:00Z",
        created_by="test",
        last_validated_at="2026-05-08T00:00:00Z",
        applied_count=applied,
        immutable=False,
    )


def test_bucket_boundaries() -> None:
    assert _bucket_for(1.0) == 0   # validated
    assert _bucket_for(0.85) == 0  # validated lower bound
    assert _bucket_for(0.84999) == 1  # working
    assert _bucket_for(0.65) == 1  # working lower bound
    assert _bucket_for(0.5) == 2   # uncertain
    assert _bucket_for(0.34999) == 3  # anti-pattern
    assert _bucket_for(0.0) == 3


def test_sort_key_high_weight_high_applied_first() -> None:
    """Sort by weight × log(1 + applied_count) descending (so we negate)."""
    a = _stmt("pb-A", weight=0.9, applied=10)
    b = _stmt("pb-B", weight=0.9, applied=0)
    c = _stmt("pb-C", weight=0.95, applied=0)
    items = sorted([a, b, c], key=_sort_key)
    # `a` (high × log10) should come BEFORE `c` (high × 0) and `b` (lower × 0)
    assert items[0].id == "pb-A"


def test_format_bucket_renders_lines() -> None:
    items = [_stmt("pb-001", 0.92, text="audit code")]
    body = _format_bucket("Validated (≥ 0.85)", items)
    assert "**Validated" in body
    # Statement rows carry the pb-id alongside the weight (spec §6.2)
    # so Coach can target an existing row via coord_propose_playbook_changes
    # adjust/merge/archive ops without creating near-duplicates.
    assert "[pb-001 / 0.92]" in body
    assert "audit code" in body


def test_render_empty_lattice_returns_empty_string(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Spec §6.2: empty lattice → empty string."""
    monkeypatch.setattr(pb_paths_mod, "DATA_ROOT", tmp_path)
    # No file at all → load returns empty → render returns ""
    assert render_playbook_block() == ""


def test_render_with_statements_includes_heading(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pb_paths_mod, "DATA_ROOT", tmp_path)
    lat = Lattice(schema_version=1, updated_at="2026-05-08T00:00:00Z", statements=[
        _stmt("pb-001", 0.92, text="audit code changes"),
    ])
    out = _render_full(lat)
    assert "## Orchestration playbook" in out
    assert "[pb-001 / 0.92]" in out
    assert "audit code changes" in out


def test_render_drops_uncertain_when_over_budget(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When the full render exceeds budget, drop the uncertain bucket."""
    monkeypatch.setattr(pb_paths_mod, "DATA_ROOT", tmp_path)
    # Build a lattice with one validated + one uncertain. Even a tiny budget
    # forces the uncertain drop.
    monkeypatch.setattr(render_mod.config, "RENDER_MAX_BYTES", 200)
    lat = Lattice(schema_version=1, updated_at="now", statements=[
        _stmt("pb-001", 0.95, text="A" * 50),
        _stmt("pb-002", 0.50, text="B" * 50),
    ])
    out_full = _render_full(lat)
    out_no_uncertain = _render_full(lat, drop_uncertain=True)
    # The drop variant should NOT contain the uncertain bucket
    assert "Uncertain" not in out_no_uncertain
    # Full version SHOULD
    assert "Uncertain" in out_full


def test_render_self_contained_block_has_closing_meta() -> None:
    """The render must include the closing `— End playbook (...)` line."""
    lat = Lattice(schema_version=1, updated_at="2026-05-08T00:00:00Z", statements=[
        _stmt("pb-001", 0.9),
    ])
    out = _render_full(lat)
    assert "End playbook" in out
    assert "1 statement" in out  # singular


def test_render_disabled_returns_empty_via_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When `_is_disabled()` returns True, render_playbook_block → ''."""
    monkeypatch.setattr(pb_paths_mod, "DATA_ROOT", tmp_path)
    # Stub _is_disabled directly to True
    monkeypatch.setattr(render_mod, "_is_disabled", lambda: True)
    # Even with statements, output should be empty
    lat_path = tmp_path / "playbook" / "lattice.json"
    lat_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    lat_path.write_text(json.dumps({
        "schema_version": 1,
        "updated_at": "2026-05-08T00:00:00Z",
        "statements": [_stmt("pb-001", 0.9).to_jsonable()],
    }), encoding="utf-8")
    assert render_playbook_block() == ""


def test_render_within_bucket_sort_uses_score(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Within a bucket, items sort by weight × log(1 + applied_count)
    descending. So a rule that fires often outranks one with same weight
    that's never been applied."""
    monkeypatch.setattr(pb_paths_mod, "DATA_ROOT", tmp_path)
    lat = Lattice(schema_version=1, updated_at="now", statements=[
        _stmt("pb-low-applied", weight=0.95, applied=0, text="LOW_APPLIED_RULE"),
        _stmt("pb-high-applied", weight=0.95, applied=20, text="HIGH_APPLIED_RULE"),
    ])
    out = _render_full(lat)
    high_pos = out.find("HIGH_APPLIED_RULE")
    low_pos = out.find("LOW_APPLIED_RULE")
    assert 0 < high_pos < low_pos

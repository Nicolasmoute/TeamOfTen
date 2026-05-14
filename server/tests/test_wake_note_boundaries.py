"""
Tests for execute/ship stage boundary wording in wake notes (2026-05-14).

Root cause: ambiguous wake notes ("commit + push, then coord_role_complete"
in execute, "cherry-pick to dev" in ship) led Players to generalise
ship-stage patterns onto execute turns, bypassing the audit gate.

Fix: _completion_hint_for_role in server/kanban.py appends explicit
stage-boundary text (_EXECUTE_STAGE_BOUNDARY / _SHIP_STAGE_BOUNDARY)
to executor and shipper wake hints respectively.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Constant surface tests — these run without needing a DB (kanban is
# importable at module level; the constants are module-level strings).
# ---------------------------------------------------------------------------


def test_execute_stage_boundary_constant_exists() -> None:
    from server.kanban import _EXECUTE_STAGE_BOUNDARY
    assert isinstance(_EXECUTE_STAGE_BOUNDARY, str)
    assert len(_EXECUTE_STAGE_BOUNDARY) > 0


def test_ship_stage_boundary_constant_exists() -> None:
    from server.kanban import _SHIP_STAGE_BOUNDARY
    assert isinstance(_SHIP_STAGE_BOUNDARY, str)
    assert len(_SHIP_STAGE_BOUNDARY) > 0


def test_execute_boundary_mentions_origin_work_slot() -> None:
    """Execute boundary must name origin/work/<your_slot> as the only push target."""
    from server.kanban import _EXECUTE_STAGE_BOUNDARY
    assert "origin/work/" in _EXECUTE_STAGE_BOUNDARY


def test_execute_boundary_forbids_dev_push() -> None:
    """Execute boundary must explicitly forbid pushing to dev."""
    from server.kanban import _EXECUTE_STAGE_BOUNDARY
    text = _EXECUTE_STAGE_BOUNDARY.lower()
    # Must say "do not" + "dev" in some form
    assert "not" in text
    assert "dev" in text


def test_execute_boundary_mentions_coord_commit_push() -> None:
    """Execute boundary must reference coord_commit_push as the committing path."""
    from server.kanban import _EXECUTE_STAGE_BOUNDARY
    assert "coord_commit_push" in _EXECUTE_STAGE_BOUNDARY


def test_ship_boundary_mentions_coord_ship_to_dev() -> None:
    """Ship boundary must reference coord_ship_to_dev as the merging path."""
    from server.kanban import _SHIP_STAGE_BOUNDARY
    assert "coord_ship_to_dev" in _SHIP_STAGE_BOUNDARY


def test_ship_boundary_forbids_raw_git_push_to_dev() -> None:
    """Ship boundary must explicitly forbid raw git push to dev."""
    from server.kanban import _SHIP_STAGE_BOUNDARY
    text = _SHIP_STAGE_BOUNDARY.lower()
    assert "not" in text
    assert "dev" in text


# ---------------------------------------------------------------------------
# _completion_hint_for_role integration: executor hint must contain boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_completion_hint_contains_execute_boundary(
    fresh_db,
) -> None:
    """_completion_hint_for_role for executor includes _EXECUTE_STAGE_BOUNDARY."""
    from server.db import init_db
    from server.kanban import _completion_hint_for_role, _EXECUTE_STAGE_BOUNDARY

    await init_db()
    hint = await _completion_hint_for_role("t-test-123", "executor")
    # The hint must contain the boundary fragment (or at least the key phrase)
    assert "origin/work/" in hint or "[execute-stage rules]" in hint
    assert _EXECUTE_STAGE_BOUNDARY in hint


@pytest.mark.asyncio
async def test_shipper_completion_hint_contains_ship_boundary(
    fresh_db,
) -> None:
    """_completion_hint_for_role for shipper includes _SHIP_STAGE_BOUNDARY."""
    from server.db import init_db
    from server.kanban import _completion_hint_for_role, _SHIP_STAGE_BOUNDARY

    await init_db()
    hint = await _completion_hint_for_role("t-test-123", "shipper")
    assert "coord_ship_to_dev" in hint
    assert _SHIP_STAGE_BOUNDARY in hint


@pytest.mark.asyncio
async def test_auditor_syntax_hint_has_no_execute_boundary(fresh_db) -> None:
    """Audit hints must NOT contain the execute boundary."""
    from server.db import init_db
    from server.kanban import _completion_hint_for_role

    await init_db()
    hint = await _completion_hint_for_role("t-test-123", "auditor_syntax")
    assert "[execute-stage rules]" not in hint


@pytest.mark.asyncio
async def test_auditor_semantics_hint_has_no_ship_boundary(fresh_db) -> None:
    """Semantic audit hints must NOT contain the ship boundary."""
    from server.db import init_db
    from server.kanban import _completion_hint_for_role

    await init_db()
    hint = await _completion_hint_for_role("t-test-123", "auditor_semantics")
    assert "[ship-stage rules]" not in hint

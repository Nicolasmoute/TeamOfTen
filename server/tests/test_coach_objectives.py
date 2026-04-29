"""Tests for the project objectives surface (recurrence-specs.md §3.3, §6).

Phase 4 cover:
  * read_objectives returns "" for missing / empty / whitespace-only.
  * has_objectives correctly detects content.
  * objectives_block formats the system-prompt section, omits when
    empty (spec §6: "no 'None this session' placeholder").
  * Truncation kicks in past the sanity ceiling.
  * Elicitation-prompt constant is the verbatim spec §14 wording.
"""

from __future__ import annotations

import pytest

import server.coach_objectives as objs
from server.db import init_db
from server.paths import ensure_project_scaffold, project_paths


async def test_read_returns_empty_when_missing(fresh_db: str) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    pp = project_paths("misc")
    assert not pp.project_objectives.exists()
    assert objs.read_objectives("misc") == ""
    assert objs.has_objectives("misc") is False
    assert objs.objectives_block("misc") == ""


async def test_read_returns_body_when_present(fresh_db: str) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    pp = project_paths("misc")
    pp.project_objectives.write_text(
        "## Goals\n\nShip recurrence v2 by 2026-05-15.\n",
        encoding="utf-8",
    )
    body = objs.read_objectives("misc")
    assert "Ship recurrence v2" in body
    assert objs.has_objectives("misc") is True


async def test_block_formats_section(fresh_db: str) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    pp = project_paths("misc")
    pp.project_objectives.write_text(
        "Be brilliant.\n", encoding="utf-8",
    )
    block = objs.objectives_block("misc")
    assert block.startswith("## Project objectives\n\n")
    assert "Be brilliant" in block


async def test_block_empty_file_omitted(fresh_db: str) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    pp = project_paths("misc")
    pp.project_objectives.write_text("   \n\n", encoding="utf-8")
    assert objs.objectives_block("misc") == ""
    assert objs.has_objectives("misc") is False


async def test_truncation_kicks_in(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    pp = project_paths("misc")
    monkeypatch.setattr(objs, "_MAX_OBJECTIVES_CHARS", 100)
    pp.project_objectives.write_text("X" * 500, encoding="utf-8")
    body = objs.read_objectives("misc")
    assert body.endswith("[truncated]")
    assert len(body) < 500


def test_elicitation_prompt_matches_spec() -> None:
    # Verbatim from spec §14 — keeps the bootstrap UX consistent.
    assert objs.OBJECTIVES_ELICITATION_PROMPT == (
        "This project has no objectives defined. What are we trying "
        "to accomplish? Once you reply, I'll save them to "
        "project-objectives.md."
    )

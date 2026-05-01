"""Phase 3 tests — `pipeline.claude_md.inject`.

The injection rules (per spec §3.10):
  - Replace text between `<!-- compass:start -->` and `<!-- compass:end -->`
  - If markers absent, append the block at end of file
  - Do not modify content outside the markers
  - Idempotent: running twice with the same body produces no change

Also persists a copy under `compass/claude_md_block.md`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.compass import config
from server.compass import store as store_mod
from server.compass.pipeline import claude_md as cm_mod
from server.paths import project_paths


# ----------------------------------------------------- helpers


def _read_claude_md(project_id: str) -> str:
    return project_paths(project_id).claude_md.read_text(encoding="utf-8")


def _ensure_project_dir(project_id: str) -> Path:
    pp = project_paths(project_id)
    pp.root.mkdir(parents=True, exist_ok=True)
    return pp.root


# ----------------------------------------------------- tests


@pytest.mark.asyncio
async def test_inject_creates_file_when_missing(fresh_db: str) -> None:
    _ensure_project_dir("alpha")
    body = "## Compass\n\nbody-1\n"
    ok = await cm_mod.inject("alpha", body)
    assert ok is True
    text = _read_claude_md("alpha")
    assert text.startswith(config.CLAUDE_MD_START_MARKER)
    assert config.CLAUDE_MD_END_MARKER in text
    assert "body-1" in text
    # The block-only mirror is also written.
    saved = store_mod.read_claude_md_block("alpha")
    assert saved is not None
    assert "body-1" in saved


@pytest.mark.asyncio
async def test_inject_replaces_marker_block_only(fresh_db: str) -> None:
    pp_root = _ensure_project_dir("alpha")
    project_md = pp_root / "CLAUDE.md"
    project_md.write_text(
        "# Project: alpha\n\n"
        "## Goal\nbuild stripe billing\n\n"
        f"{config.CLAUDE_MD_START_MARKER}\n"
        "## Compass\n\nold body\n"
        f"{config.CLAUDE_MD_END_MARKER}\n\n"
        "## Conventions\nuse semver\n",
        encoding="utf-8",
    )

    new_body = "## Compass\n\nfresh body\n\n### Where we stand · next steps\n\nnew steps\n"
    ok = await cm_mod.inject("alpha", new_body)
    assert ok is True

    text = _read_claude_md("alpha")
    # Outside-markers content preserved verbatim.
    assert "# Project: alpha" in text
    assert "build stripe billing" in text
    assert "use semver" in text
    # Inside-markers content replaced.
    assert "old body" not in text
    assert "fresh body" in text
    assert "### Where we stand" in text


@pytest.mark.asyncio
async def test_inject_appends_block_when_markers_missing(fresh_db: str) -> None:
    pp_root = _ensure_project_dir("alpha")
    project_md = pp_root / "CLAUDE.md"
    project_md.write_text(
        "# Project: alpha\n\n## Goal\ndo work\n",
        encoding="utf-8",
    )
    body = "## Compass\n\nappended\n"
    ok = await cm_mod.inject("alpha", body)
    assert ok is True
    text = _read_claude_md("alpha")
    # Original goal still there.
    assert "## Goal" in text
    assert "do work" in text
    # Markers + body appended.
    assert config.CLAUDE_MD_START_MARKER in text
    assert config.CLAUDE_MD_END_MARKER in text
    assert "appended" in text
    # Markers come AFTER the original content.
    assert text.index("## Goal") < text.index(config.CLAUDE_MD_START_MARKER)


@pytest.mark.asyncio
async def test_inject_is_idempotent(fresh_db: str) -> None:
    """Running inject twice with the same body must produce identical
    file contents — critical for not churning the project CLAUDE.md
    on every Compass run."""
    _ensure_project_dir("alpha")
    body = "## Compass\n\nstable body\n"
    await cm_mod.inject("alpha", body)
    text_first = _read_claude_md("alpha")
    await cm_mod.inject("alpha", body)
    text_second = _read_claude_md("alpha")
    assert text_first == text_second


@pytest.mark.asyncio
async def test_inject_handles_multiple_runs_with_changing_body(fresh_db: str) -> None:
    _ensure_project_dir("alpha")
    await cm_mod.inject("alpha", "## Compass\n\nv1\n")
    await cm_mod.inject("alpha", "## Compass\n\nv2\n")
    text = _read_claude_md("alpha")
    assert "v1" not in text
    assert "v2" in text
    # Only one marker pair in the file.
    assert text.count(config.CLAUDE_MD_START_MARKER) == 1
    assert text.count(config.CLAUDE_MD_END_MARKER) == 1


@pytest.mark.asyncio
async def test_inject_preserves_content_with_special_regex_chars(fresh_db: str) -> None:
    pp_root = _ensure_project_dir("alpha")
    project_md = pp_root / "CLAUDE.md"
    # Includes `.` `*` `(` `)` — the marker pattern must escape these.
    surrounding = (
        "# Project: a.b\n\n"
        "## Code style\nUse [snake_case] for `vars` and (parens) for tuples.\n\n"
    )
    project_md.write_text(
        surrounding
        + f"{config.CLAUDE_MD_START_MARKER}\nold\n{config.CLAUDE_MD_END_MARKER}\n",
        encoding="utf-8",
    )
    await cm_mod.inject("alpha", "## Compass\n\nnew\n")
    text = _read_claude_md("alpha")
    assert "Use [snake_case] for `vars` and (parens) for tuples." in text
    assert "old" not in text
    assert "new" in text

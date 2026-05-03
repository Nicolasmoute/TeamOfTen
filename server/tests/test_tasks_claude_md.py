"""Tests for the kanban CLAUDE.md block injector.

The injector lives in `server.tasks_claude_md`. It writes a
marker-delimited block into per-project `CLAUDE.md` so both Coach
and Players see the kanban lifecycle baseline every turn. The same
pattern Compass uses for its claude_md block (different markers).
"""

from __future__ import annotations

from server.db import init_db
from server.paths import project_paths
from server.tasks_claude_md import (
    KANBAN_MD_END_MARKER,
    KANBAN_MD_START_MARKER,
    inject_into_all_projects,
    inject_kanban_block,
    render_kanban_block,
)


# ---------------------------------------------------------------- render_kanban_block

def test_render_block_starts_and_ends_with_markers() -> None:
    out = render_kanban_block()
    assert out.startswith(KANBAN_MD_START_MARKER)
    assert out.endswith(KANBAN_MD_END_MARKER)


def test_render_block_includes_role_boundaries() -> None:
    out = render_kanban_block()
    # Static text must mention the strict separation rule + the new
    # role-assignment tools so an agent reading the per-project
    # CLAUDE.md sees the kanban surface verbatim.
    assert "Coach** plans" in out
    assert "Players** execute, audit, and ship" in out
    assert "coord_my_assignments" in out
    assert "coord_assign_planner" in out
    assert "coord_assign_auditor" in out
    assert "coord_assign_shipper" in out
    assert "coord_submit_audit_report" in out
    assert "coord_mark_shipped" in out


def test_render_block_describes_audit_routing() -> None:
    out = render_kanban_block()
    assert "Pass → next stage" in out  # → unicode arrow
    assert "Fail → reverts to execute" in out
    # Compass is informational, not the gate.
    assert "Compass auto-audit fires informationally" in out


def test_render_block_describes_simple_self_audit() -> None:
    out = render_kanban_block()
    assert "Simple-task discipline" in out
    assert "executor SELF-AUDITS" in out


# ---------------------------------------------------------------- inject_kanban_block

async def test_inject_creates_claude_md_when_missing(fresh_db: str) -> None:
    """Fresh project with no CLAUDE.md yet — inject creates it with
    just the block."""
    await init_db()
    pp = project_paths("misc")
    target = pp.claude_md
    # init_db writes a project_claude_md_stub for misc; remove it so
    # we exercise the from-scratch path.
    if target.exists():
        target.unlink()
    ok = await inject_kanban_block("misc")
    assert ok is True
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert KANBAN_MD_START_MARKER in text
    assert KANBAN_MD_END_MARKER in text


async def test_inject_appends_when_markers_absent(fresh_db: str) -> None:
    """Existing CLAUDE.md without the kanban markers gets the block
    appended at end-of-file with a blank-line separator. Existing
    content is preserved verbatim."""
    await init_db()
    pp = project_paths("misc")
    target = pp.claude_md
    pre_existing = "# Misc project\n\nProject-specific stuff here.\n"
    target.write_text(pre_existing, encoding="utf-8", newline="\n")
    ok = await inject_kanban_block("misc")
    assert ok is True
    text = target.read_text(encoding="utf-8")
    # Existing content untouched.
    assert "# Misc project" in text
    assert "Project-specific stuff here." in text
    # Block landed after.
    assert KANBAN_MD_START_MARKER in text
    assert KANBAN_MD_END_MARKER in text
    # Block comes after existing content.
    assert text.index("Project-specific stuff here.") < text.index(
        KANBAN_MD_START_MARKER
    )


async def test_inject_replaces_marker_region(fresh_db: str) -> None:
    """Existing CLAUDE.md with stale block content gets the marker
    region replaced; surrounding text is preserved."""
    await init_db()
    pp = project_paths("misc")
    target = pp.claude_md
    stale_block = (
        "## Header above\n\n"
        f"{KANBAN_MD_START_MARKER}\n"
        "stale content goes here\n"
        f"{KANBAN_MD_END_MARKER}\n\n"
        "## Footer below\n"
    )
    target.write_text(stale_block, encoding="utf-8", newline="\n")
    ok = await inject_kanban_block("misc")
    assert ok is True
    text = target.read_text(encoding="utf-8")
    # Stale content is gone.
    assert "stale content goes here" not in text
    # Surrounding sections untouched.
    assert "## Header above" in text
    assert "## Footer below" in text
    # Canonical block landed.
    assert "Task lifecycle (kanban)" in text


async def test_inject_idempotent(fresh_db: str) -> None:
    """Re-running inject with the same canonical body produces no
    file change."""
    await init_db()
    pp = project_paths("misc")
    target = pp.claude_md
    # Ensure the file starts with the canonical block so the second
    # call can be a true no-op (without surrounding-content drift).
    target.write_text(
        render_kanban_block() + "\n", encoding="utf-8", newline="\n"
    )
    mtime_before = target.stat().st_mtime
    text_before = target.read_text(encoding="utf-8")
    ok = await inject_kanban_block("misc")
    assert ok is True
    text_after = target.read_text(encoding="utf-8")
    assert text_after == text_before
    # mtime might be touched by the OS even on no-write, but the
    # text equality is the strong invariant.


async def test_inject_into_all_projects_returns_count(fresh_db: str) -> None:
    """The bulk injector should hit every non-archived project and
    return a count of successful injections."""
    await init_db()
    count = await inject_into_all_projects()
    # Fresh DB has the seeded `misc` project, so at least 1.
    assert count >= 1
    # Misc project's CLAUDE.md now has the block.
    pp = project_paths("misc")
    text = pp.claude_md.read_text(encoding="utf-8")
    assert KANBAN_MD_START_MARKER in text

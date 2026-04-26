"""Tests for Phase 7 (PROJECTS_SPEC.md §10 + §8 + §14):

  1. Coach coordination block — built per-turn from projects /
     agent_project_roles / tasks / messages / decisions.
  2. Per-project CLAUDE.md stub — auto-written on project creation
     with Goal + Repo pre-filled.
  3. Wiki INDEX.md auto-update on every wiki write event.
"""

from __future__ import annotations

import server.paths as pathsmod
from server.db import (
    MISC_PROJECT_ID,
    configured_conn,
    init_db,
)
from server.paths import (
    bootstrap_global_resources,
    global_paths,
    project_paths,
    update_wiki_index,
)
from server.projects_api import _write_project_claude_md_stub


# ---------- Coach coordination block ------------------------------


async def _seed_misc_project_with_team_and_tasks() -> None:
    """Helper: seed misc with one named Player + a couple of tasks +
    an unread message — enough to exercise every section of the
    coordination block."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT OR REPLACE INTO agent_project_roles "
            "(slot, project_id, name, role, brief) VALUES (?, ?, ?, ?, ?)",
            ("p1", MISC_PROJECT_ID, "Alice Rabil", "Lead Developer", None),
        )
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "T-1", MISC_PROJECT_ID, "Refresh deck template",
                "in_progress", "p1", "coach",
            ),
        )
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "T-2", MISC_PROJECT_ID, "Snapshot tests",
                "claimed", "p1", "coach",
            ),
        )
        await c.execute(
            "UPDATE projects SET description = ? WHERE id = ?",
            ("Rebrand misc landing page", MISC_PROJECT_ID),
        )
        # An unread message from human to coach.
        await c.execute(
            "INSERT INTO messages "
            "(from_id, to_id, project_id, subject, body) "
            "VALUES (?, ?, ?, ?, ?)",
            ("human", "coach", MISC_PROJECT_ID, "hi", "kick off plz"),
        )
        await c.commit()
    finally:
        await c.close()


async def test_coordination_block_includes_project_name_and_goal(
    fresh_db,
) -> None:
    from server.agents import _build_coach_coordination_block

    await _seed_misc_project_with_team_and_tasks()
    block = await _build_coach_coordination_block()
    # Spec example shows "## Coordinating: <Name>" header. Misc's
    # display name is seeded as "Misc" (capitalized) in init_db.
    assert block.startswith("## Coordinating: Misc")
    assert "Goal: Rebrand misc landing page" in block


async def test_coordination_block_lists_named_player_and_unassigned(
    fresh_db,
) -> None:
    from server.agents import _build_coach_coordination_block

    await _seed_misc_project_with_team_and_tasks()
    block = await _build_coach_coordination_block()
    assert "## Team composition (this project)" in block
    assert "coach" in block and "you" in block
    assert "Alice Rabil" in block
    assert "Lead Developer" in block
    # p2..p10 should be flagged unassigned.
    assert "unassigned" in block
    assert "coord_set_player_role" in block


async def test_coordination_block_includes_open_tasks_and_inbox(
    fresh_db,
) -> None:
    from server.agents import _build_coach_coordination_block

    await _seed_misc_project_with_team_and_tasks()
    block = await _build_coach_coordination_block()
    assert "Open tasks (2)" in block
    assert "T-1 (in_progress)" in block
    assert "T-2 (claimed)" in block
    assert "Inbox: 1 unread message" in block


async def test_coordination_block_marks_locked_player(fresh_db) -> None:
    from server.agents import _build_coach_coordination_block

    await _seed_misc_project_with_team_and_tasks()
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET locked = 1 WHERE id = 'p1'"
        )
        await c.commit()
    finally:
        await c.close()
    block = await _build_coach_coordination_block()
    assert "LOCKED" in block


async def test_coordination_block_includes_last_decision(fresh_db) -> None:
    from server.agents import _build_coach_coordination_block

    await _seed_misc_project_with_team_and_tasks()
    pp = project_paths(MISC_PROJECT_ID)
    pp.decisions.mkdir(parents=True, exist_ok=True)
    decision = pp.decisions / "2026-04-25-adopt-tailwind-v4.md"
    decision.write_text(
        "---\ntitle: Adopt Tailwind v4\n---\n\nBody.\n",
        encoding="utf-8",
    )

    block = await _build_coach_coordination_block()
    assert "Last decision: 2026-04-25 — Adopt Tailwind v4" in block


async def test_coordination_block_includes_wiki_paths(fresh_db) -> None:
    from server.agents import _build_coach_coordination_block

    await _seed_misc_project_with_team_and_tasks()
    block = await _build_coach_coordination_block()
    assert "Wiki:" in block
    assert "INDEX.md" in block


async def test_coordination_block_returns_empty_on_db_error(
    fresh_db, monkeypatch
) -> None:
    """A DB read failure mid-build should swallow and return "" so the
    Coach turn proceeds (just without the coordination context)."""
    from server.agents import _build_coach_coordination_block

    await init_db()

    async def boom():
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(
        "server.agents.resolve_active_project", boom
    )
    block = await _build_coach_coordination_block()
    assert block == ""


# ---------- Per-project CLAUDE.md stub ----------------------------


def test_claude_md_stub_writes_with_goal_and_repo_pre_filled(
    fresh_db,
) -> None:
    pathsmod.ensure_project_scaffold("alpha")
    _write_project_claude_md_stub(
        "alpha",
        "Alpha Project",
        "Refresh the website",
        "https://github.com/foo/alpha.git",
    )
    pp = project_paths("alpha")
    body = pp.claude_md.read_text(encoding="utf-8")
    assert "# Project: Alpha Project" in body
    assert "Refresh the website" in body
    assert "https://github.com/foo/alpha.git" in body
    # Sections left blank for Coach to fill.
    assert "## Stakeholders" in body
    assert "## Team" in body
    assert "## Glossary" in body
    assert "## Conventions" in body


def test_claude_md_stub_skips_when_file_already_exists(fresh_db) -> None:
    """First-write-only — re-creation paths preserve user / Coach
    edits. The stub function should never overwrite."""
    pathsmod.ensure_project_scaffold("beta")
    pp = project_paths("beta")
    pp.claude_md.write_text("CUSTOM CONTENT", encoding="utf-8")
    _write_project_claude_md_stub("beta", "Beta", "x", "y")
    assert pp.claude_md.read_text(encoding="utf-8") == "CUSTOM CONTENT"


def test_claude_md_stub_uses_placeholders_when_blank(fresh_db) -> None:
    """A project created without description / repo gets sentinel
    placeholders so a future reader sees they're intentionally
    empty rather than data-missing."""
    pathsmod.ensure_project_scaffold("gamma")
    _write_project_claude_md_stub("gamma", "Gamma", None, None)
    pp = project_paths("gamma")
    body = pp.claude_md.read_text(encoding="utf-8")
    assert "<short description, from creation modal>" in body
    assert "<no repo configured>" in body


# ---------- Wiki INDEX.md auto-update ------------------------------


def test_update_wiki_index_with_no_entries(fresh_db) -> None:
    bootstrap_global_resources()
    ok = update_wiki_index()
    assert ok is True
    body = global_paths().wiki_index.read_text(encoding="utf-8")
    assert "## Cross-project entries" in body
    assert "## Per-project entries" in body
    assert "_(none yet)_" in body


def test_update_wiki_index_lists_cross_project_entries(fresh_db) -> None:
    bootstrap_global_resources()
    gp = global_paths()
    (gp.wiki / "shared-concept.md").write_text(
        "---\ntitle: Shared concept\n---\n\nbody.\n", encoding="utf-8"
    )
    (gp.wiki / "another-thing.md").write_text(
        "# Another thing\n\nbody.\n", encoding="utf-8"
    )
    update_wiki_index()
    body = gp.wiki_index.read_text(encoding="utf-8")
    assert "[Shared concept](shared-concept.md)" in body
    assert "[Another thing](another-thing.md)" in body


def test_update_wiki_index_groups_per_project(fresh_db) -> None:
    bootstrap_global_resources()
    gp = global_paths()
    (gp.wiki / "alpha").mkdir()
    (gp.wiki / "alpha" / "design-decision.md").write_text(
        "# Design decision\n\nbody.\n", encoding="utf-8"
    )
    (gp.wiki / "beta").mkdir()
    (gp.wiki / "beta" / "rollout-plan.md").write_text(
        "# Rollout plan\n\nbody.\n", encoding="utf-8"
    )
    update_wiki_index()
    body = gp.wiki_index.read_text(encoding="utf-8")
    assert "### alpha" in body
    assert "### beta" in body
    assert "[Design decision](alpha/design-decision.md)" in body
    assert "[Rollout plan](beta/rollout-plan.md)" in body


def test_update_wiki_index_excludes_index_md_itself(fresh_db) -> None:
    """INDEX.md must not list itself — that would be self-referential
    noise on every rebuild."""
    bootstrap_global_resources()
    gp = global_paths()
    update_wiki_index()
    body = gp.wiki_index.read_text(encoding="utf-8")
    # The link target should never be `INDEX.md`
    assert "](INDEX.md)" not in body


def test_update_wiki_index_extracts_title_from_first_heading(
    fresh_db,
) -> None:
    bootstrap_global_resources()
    gp = global_paths()
    (gp.wiki / "no-frontmatter.md").write_text(
        "# Heading-driven title\n\nbody.\n", encoding="utf-8"
    )
    update_wiki_index()
    body = gp.wiki_index.read_text(encoding="utf-8")
    assert "[Heading-driven title](no-frontmatter.md)" in body


def test_update_wiki_index_falls_back_to_stem_when_no_title(
    fresh_db,
) -> None:
    bootstrap_global_resources()
    gp = global_paths()
    (gp.wiki / "no-heading.md").write_text("just plain text\n", encoding="utf-8")
    update_wiki_index()
    body = gp.wiki_index.read_text(encoding="utf-8")
    assert "[no-heading](no-heading.md)" in body


def test_update_wiki_index_returns_false_when_wiki_missing(
    fresh_db, monkeypatch
) -> None:
    """Missing wiki tree → False, no exception."""
    import shutil

    bootstrap_global_resources()
    gp = global_paths()
    shutil.rmtree(gp.wiki)
    assert update_wiki_index() is False


def test_update_wiki_index_atomic_overwrite(fresh_db) -> None:
    """Calling update twice with different content → final body
    reflects the second call (no leftover from the first)."""
    bootstrap_global_resources()
    gp = global_paths()
    (gp.wiki / "first.md").write_text("# First\n", encoding="utf-8")
    update_wiki_index()
    (gp.wiki / "first.md").unlink()
    (gp.wiki / "second.md").write_text("# Second\n", encoding="utf-8")
    update_wiki_index()
    body = gp.wiki_index.read_text(encoding="utf-8")
    assert "[Second](second.md)" in body
    assert "[First](first.md)" not in body


# ---------- Phase 7 audit fixes -----------------------------------


async def test_audit_init_db_writes_misc_claude_md_stub(fresh_db) -> None:
    """Audit fix: init_db's misc-project seed should also write the
    per-project CLAUDE.md stub on first boot. The fresh_db fixture
    is a freshly-mkdtemp'd DATA_ROOT with no pre-existing files —
    init_db (called via the migration in projects_v1) should leave
    misc with its stub present."""
    await init_db()
    pp = project_paths(MISC_PROJECT_ID)
    assert pp.claude_md.is_file(), (
        "init_db did not write CLAUDE.md stub for misc"
    )
    body = pp.claude_md.read_text(encoding="utf-8")
    assert "# Project: Misc" in body
    assert "## Stakeholders" in body


async def test_audit_init_db_preserves_existing_misc_claude_md(
    fresh_db,
) -> None:
    """First-write-only — re-running init_db on a DATA_ROOT that
    already has a misc/CLAUDE.md (e.g. user / Coach edits) does not
    overwrite the file."""
    await init_db()
    pp = project_paths(MISC_PROJECT_ID)
    pp.claude_md.write_text("CUSTOM EDITS", encoding="utf-8")
    # init_db is called by fresh_db indirectly — re-run the seeding
    # path explicitly to confirm the second-call branch.
    await init_db()
    assert pp.claude_md.read_text(encoding="utf-8") == "CUSTOM EDITS"


async def test_audit_coord_block_locked_player_includes_prose(
    fresh_db,
) -> None:
    """Audit fix: spec §10 says the 'Roster availability' block
    becomes a sub-section of the larger coordination block. A locked
    Player should produce both the inline (LOCKED) tag AND the prose
    'Do NOT assign / do NOT direct-message' reminder, all inside the
    coord block. There should NOT be a standalone lock_suffix
    appended after."""
    from server.agents import _build_coach_coordination_block

    await _seed_misc_project_with_team_and_tasks()
    c = await configured_conn()
    try:
        await c.execute("UPDATE agents SET locked = 1 WHERE id = 'p1'")
        await c.commit()
    finally:
        await c.close()
    block = await _build_coach_coordination_block()
    # Inline tag still present.
    assert "(LOCKED — unavailable)" in block
    # Prose folded into block as a sub-section (### not ##).
    assert "### Roster availability" in block
    assert "Do NOT assign tasks to them" in block


async def test_audit_coord_block_skips_locked_prose_when_none_locked(
    fresh_db,
) -> None:
    """No locked Players → no Roster availability sub-section
    (avoids the prose noise on a clean roster)."""
    from server.agents import _build_coach_coordination_block

    await _seed_misc_project_with_team_and_tasks()
    block = await _build_coach_coordination_block()
    assert "(LOCKED — unavailable)" not in block
    assert "### Roster availability" not in block


def test_audit_update_wiki_index_handles_empty_subfolder(fresh_db) -> None:
    """Audit edge case: a project sub-folder created by
    ensure_project_scaffold but with no entries yet should not
    appear under '## Per-project entries' (would render an empty
    section header). Triggered after project_created on a fresh
    project."""
    bootstrap_global_resources()
    gp = global_paths()
    (gp.wiki / "empty-project").mkdir()
    update_wiki_index()
    body = gp.wiki_index.read_text(encoding="utf-8")
    assert "### empty-project" not in body

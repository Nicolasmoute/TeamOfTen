"""Tests for Phase 7 (PROJECTS_SPEC.md §10 + §8 + §14):

  1. Coach coordination block — built per-turn from projects /
     agent_project_roles / tasks / messages / decisions.
  2. Per-project CLAUDE.md stub — auto-written on project creation
     with Goal + Repo pre-filled.
  3. Wiki INDEX.md auto-update on every wiki write event.
"""

from __future__ import annotations

from typing import Any

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


async def test_coordination_block_includes_project_name_and_objectives_pointer(
    fresh_db,
) -> None:
    from server.agents import _build_coach_coordination_block

    await _seed_misc_project_with_team_and_tasks()
    block = await _build_coach_coordination_block()
    # Spec example shows "## Coordinating: <Name>" header. Misc's
    # display name is seeded as "Misc" (capitalized) in init_db.
    assert block.startswith("## Coordinating: Misc")
    # `projects.description` is NOT rendered here anymore —
    # goals/objectives flow through `project-objectives.md` to avoid
    # two stale-prone copies of the same goal in every Coach turn.
    assert "Rebrand misc landing page" not in block
    assert "Goal:" not in block
    # The pointer to project-objectives.md should still be present
    # so Coach knows where to read / update goals.
    assert "project-objectives.md" in block
    assert "## Project objectives" in block


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


def test_claude_md_stub_writes_with_repo_and_objectives_pointer(
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
    assert "https://github.com/foo/alpha.git" in body
    # `description` from the creation modal is intentionally NOT
    # injected — goals/objectives flow through `project-objectives.md`
    # only.
    assert "Refresh the website" not in body
    assert "## Goal" not in body
    # Pointer to the canonical objectives file must be present so
    # Coach reads / edits there.
    assert "## Project objectives" in body
    assert "project-objectives.md" in body
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
    """A project created without repo gets a sentinel placeholder so
    a future reader sees it's intentionally empty rather than
    data-missing. `description` is no longer injected — the
    objectives pointer covers the goal-content surface."""
    pathsmod.ensure_project_scaffold("gamma")
    _write_project_claude_md_stub("gamma", "Gamma", None, None)
    pp = project_paths("gamma")
    body = pp.claude_md.read_text(encoding="utf-8")
    assert "<no repo configured>" in body
    # `description` placeholder is gone with the `## Goal` section.
    assert "<short description, from creation modal>" not in body
    assert "## Project objectives" in body


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
    """init_db's misc-project seed should also write the per-project
    CLAUDE.md stub on first boot. The fresh_db fixture is a freshly-
    mkdtemp'd DATA_ROOT with no pre-existing files — init_db should
    leave misc with its stub present."""
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


# ---------- PostToolUse hook: agent Write rebuilds INDEX.md --------


async def test_posttool_wiki_index_hook_rebuilds_on_agent_write(fresh_db) -> None:
    """The wiki skill promises 'auto-maintained on every wiki write
    event'. Agent Write tool calls go through the SDK directly to disk
    and bypass the harness's HTTP write endpoint, so before this hook
    existed those writes never triggered a rebuild. Simulate the SDK
    posting a PostToolUse for a Write that landed under /data/wiki/
    and confirm INDEX.md picks up the new entry."""
    from server.agents import _posttool_wiki_index_hook

    bootstrap_global_resources()
    gp = global_paths()
    (gp.wiki / "alpha").mkdir(exist_ok=True)
    entry = gp.wiki / "alpha" / "new-page.md"
    entry.write_text("# New page\n\nbody.\n", encoding="utf-8")

    # Pre-condition: the old INDEX.md (whatever bootstrap wrote) does
    # not yet list new-page.md.
    pre = gp.wiki_index.read_text(encoding="utf-8")
    assert "new-page.md" not in pre

    await _posttool_wiki_index_hook(
        {"tool_name": "Write", "tool_input": {"file_path": str(entry)}},
        "tu_1",
        None,
    )
    post = gp.wiki_index.read_text(encoding="utf-8")
    assert "[New page](alpha/new-page.md)" in post


async def test_posttool_wiki_index_hook_skips_non_wiki_paths(fresh_db) -> None:
    """A Write to anywhere outside /data/wiki/ must NOT trigger a
    rebuild — otherwise every code edit in a worktree would thrash
    the index file."""
    from server.agents import _posttool_wiki_index_hook

    bootstrap_global_resources()
    gp = global_paths()
    update_wiki_index()
    before = gp.wiki_index.stat().st_mtime_ns

    await _posttool_wiki_index_hook(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "/workspaces/p1/project/foo.py"},
        },
        "tu_1",
        None,
    )
    after = gp.wiki_index.stat().st_mtime_ns
    assert after == before


async def test_posttool_wiki_index_hook_skips_index_itself(fresh_db) -> None:
    """A Write whose target IS INDEX.md must not trigger a rebuild
    (would loop). The hook resolves both paths so symlinks / relative
    forms still match."""
    from server.agents import _posttool_wiki_index_hook

    bootstrap_global_resources()
    gp = global_paths()
    update_wiki_index()
    before = gp.wiki_index.stat().st_mtime_ns

    await _posttool_wiki_index_hook(
        {"tool_name": "Write", "tool_input": {"file_path": str(gp.wiki_index)}},
        "tu_1",
        None,
    )
    after = gp.wiki_index.stat().st_mtime_ns
    assert after == before


# ---------- Truth folder + PreToolUse guard hook ------------------


def test_truth_folder_in_project_paths(fresh_db) -> None:
    """ProjectPaths exposes a `truth` field at /data/projects/<slug>/truth/."""
    from server.paths import ensure_project_scaffold

    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    assert pp.truth.name == "truth"
    assert pp.truth.parent == pp.root
    assert pp.truth.is_dir()


async def test_truth_guard_denies_write_under_truth(fresh_db) -> None:
    """Agent Write to /data/projects/<slug>/truth/anything must be denied."""
    from server.agents import _pretool_file_guard_hook
    from server.paths import ensure_project_scaffold

    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    target = pp.truth / "specs.md"
    res = await _pretool_file_guard_hook(
        {"tool_name": "Write", "tool_input": {"file_path": str(target)}},
        "tu_1",
        None,
    )
    out = res.get("hookSpecificOutput") or {}
    assert out.get("permissionDecision") == "deny"
    assert "truth/" in (out.get("permissionDecisionReason") or "")


async def test_truth_guard_allows_writes_outside_truth(fresh_db) -> None:
    """Writes to non-truth paths (working/, knowledge/, etc.) pass through."""
    from server.agents import _pretool_file_guard_hook
    from server.paths import ensure_project_scaffold

    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    for safe in (
        pp.working_workspace / "scratch.txt",
        pp.knowledge / "notes.md",
        pp.decisions / "0001-something.md",
        pp.outputs / "deck.pdf",
    ):
        res = await _pretool_file_guard_hook(
            {"tool_name": "Write", "tool_input": {"file_path": str(safe)}},
            "tu_1",
            None,
        )
        assert res == {}, f"unexpected deny for {safe}: {res}"


async def test_truth_guard_denies_edit_and_multiedit(fresh_db) -> None:
    """All four file-mutating tools route through file_path the same way."""
    from server.agents import _pretool_file_guard_hook
    from server.paths import ensure_project_scaffold

    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    target = str(pp.truth / "brand.md")
    for tool_name in ("Edit", "MultiEdit", "NotebookEdit"):
        key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
        res = await _pretool_file_guard_hook(
            {"tool_name": tool_name, "tool_input": {key: target}},
            "tu_1",
            None,
        )
        out = res.get("hookSpecificOutput") or {}
        assert out.get("permissionDecision") == "deny", \
            f"{tool_name} should have been denied"


async def test_truth_guard_denies_bash_writing_into_truth(fresh_db) -> None:
    """Bash redirects into truth/ are caught by the substring heuristic."""
    from server.agents import _pretool_file_guard_hook
    from server.paths import ensure_project_scaffold

    ensure_project_scaffold(MISC_PROJECT_ID)
    cases = [
        "echo hello > truth/specs.md",
        "cat input.txt >> /data/projects/misc/truth/brand.md",
        "cp deck.pdf truth/deck.pdf",
    ]
    for cmd in cases:
        res = await _pretool_file_guard_hook(
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            "tu_1",
            None,
        )
        out = res.get("hookSpecificOutput") or {}
        assert out.get("permissionDecision") == "deny", f"cmd not denied: {cmd}"


async def test_truth_guard_lets_innocuous_bash_through(fresh_db) -> None:
    """Bash commands that don't reference truth/ pass through (no false-deny)."""
    from server.agents import _pretool_file_guard_hook

    for cmd in (
        "ls -la",
        "git status",
        "echo trustworthy",       # contains 'trust' but not 'truth/'
        "cat /data/projects/misc/working/notes.md",
    ):
        res = await _pretool_file_guard_hook(
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            "tu_1",
            None,
        )
        assert res == {}, f"unexpected deny for cmd: {cmd!r} -> {res}"


# ---------- Truth proposals (Coach proposes → human approves) -----


def test_coord_propose_file_write_in_coord_allowlist() -> None:
    """The new tool is registered so the SDK will actually accept calls.
    Coach-only enforcement is in the tool body (caller_is_coach
    rejection); the tool is in the shared coord allowlist alongside
    other Coach-rejecting tools like coord_write_decision."""
    from server.tools import ALLOWED_COORD_TOOLS

    assert "mcp__coord__coord_propose_file_write" in ALLOWED_COORD_TOOLS


async def _propose_truth(handler, **kwargs):
    """Test helper — wraps a coord_propose_file_write handler call
    with scope='truth' (the truth-scope-specific tests in this file
    keep using this helper; project_claude_md scope tests pass scope
    explicitly)."""
    args = {"scope": kwargs.get("scope", "truth"),
            "path": kwargs.get("path", ""),
            "content": kwargs.get("content", "body"),
            "summary": kwargs.get("summary", "why")}
    return await handler(args)


async def test_propose_truth_update_rejects_projects_prefix(fresh_db) -> None:
    """Coach passing 'projects/<slug>/...' as path is the recurrent
    mistake — truth/ is rooted at the active project, not anywhere
    under /data/projects/. The tool must reject with a hint about
    switching active project, not silently accept and queue a
    nested-path proposal."""
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold("misc")
    from server.tools import build_coord_server
    srv = build_coord_server("coach", include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_propose_file_write"]

    out = await _propose_truth(
        handler,
        path="projects/dynamichypergraph/CLAUDE.md",
        content="rule body",
        summary="add zeabur scope rule",
    )
    assert out.get("isError") is True
    text = out["content"][0]["text"]
    assert "projects/" in text
    assert "switch" in text.lower() or "active project" in text.lower()


async def test_propose_truth_update_rejects_known_project_slug_prefix(
    fresh_db,
) -> None:
    """Coach passing '<existing-slug>/CLAUDE.md' should be rejected
    with a hint to switch active project. Catches the case where
    Coach drops the literal 'projects/' prefix but still names a
    sibling project as the first path segment."""
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold("misc")
    # Insert a second project so its slug is detectable.
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?)",
            ("dynamichypergraph", "Dynamic Hypergraph"),
        )
        await c.commit()
    finally:
        await c.close()

    from server.tools import build_coord_server
    srv = build_coord_server("coach", include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_propose_file_write"]

    out = await _propose_truth(
        handler,
        path="dynamichypergraph/CLAUDE.md",
        content="rule body",
        summary="add zeabur scope rule",
    )
    assert out.get("isError") is True
    text = out["content"][0]["text"]
    assert "dynamichypergraph" in text
    assert "switch" in text.lower() or "active project" in text.lower()


async def test_propose_truth_update_accepts_bare_filename(fresh_db) -> None:
    """Sanity: a bare filename still works — the new validation only
    rejects project-prefixed paths, not legitimate truth/ files."""
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold("misc")
    from server.tools import build_coord_server
    srv = build_coord_server("coach", include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_propose_file_write"]

    out = await _propose_truth(
        handler,
        path="CLAUDE.md",
        content="rule body",
        summary="add zeabur scope rule",
    )
    assert out.get("isError") is not True
    text = out["content"][0]["text"]
    assert "queued" in text or "proposal" in text.lower()


async def test_file_write_proposals_schema_smoke(fresh_db) -> None:
    """file_write_proposals table exists, accepts an insert, status defaults
    to 'pending', and the CHECK constraint rejects bad statuses."""
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO file_write_proposals "
            "(project_id, proposer_id, path, proposed_content, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            (MISC_PROJECT_ID, "coach", "specs.md", "body", "summary"),
        )
        await c.commit()
        row_id = cur.lastrowid
        cur = await c.execute(
            "SELECT status FROM file_write_proposals WHERE id = ?", (row_id,),
        )
        row = await cur.fetchone()
        assert row[0] == "pending"

        import sqlite3
        try:
            await c.execute(
                "INSERT INTO file_write_proposals "
                "(project_id, proposer_id, path, proposed_content, "
                "summary, status) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    MISC_PROJECT_ID, "coach", "x.md", "b", "s",
                    "bogus-status",
                ),
            )
            assert False, "CHECK constraint should have rejected"
        except sqlite3.IntegrityError:
            pass
    finally:
        await c.close()


async def test_resolve_file_write_proposal_approve_writes_file_and_marks_row(
    fresh_db,
) -> None:
    """Approve flow: pending row → file written under truth/ → row
    marked approved. The actor goes into the event payload."""
    from server.truth import resolve_file_write_proposal
    from server.paths import ensure_project_scaffold

    await init_db()
    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO file_write_proposals "
            "(project_id, proposer_id, path, proposed_content, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                MISC_PROJECT_ID, "coach", "specs.md",
                "# Specs\n\nbody.\n",
                "Add launch-date constraint",
            ),
        )
        await c.commit()
        proposal_id = cur.lastrowid
    finally:
        await c.close()

    res = await resolve_file_write_proposal(
        proposal_id,
        new_status="approved",
        note="LGTM",
        actor={"source": "ui", "ip": "127.0.0.1", "ua": "test"},
    )
    assert res["ok"] is True
    assert res["status"] == "approved"

    written = pp.truth / "specs.md"
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == "# Specs\n\nbody.\n"

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, resolved_by, resolved_note FROM file_write_proposals "
            "WHERE id = ?", (proposal_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row[0] == "approved"
    assert row[1] == "human"
    assert row[2] == "LGTM"


async def test_resolve_file_write_proposal_deny_does_not_write(fresh_db) -> None:
    """Deny: row marked denied, NO file write under truth/."""
    from server.truth import resolve_file_write_proposal
    from server.paths import ensure_project_scaffold

    await init_db()
    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO file_write_proposals "
            "(project_id, proposer_id, path, proposed_content, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                MISC_PROJECT_ID, "coach", "rejected.md",
                "should not appear", "no thanks",
            ),
        )
        await c.commit()
        proposal_id = cur.lastrowid
    finally:
        await c.close()

    res = await resolve_file_write_proposal(
        proposal_id, new_status="denied", note="not now",
        actor={"source": "ui", "ip": "127.0.0.1", "ua": "test"},
    )
    assert res["status"] == "denied"

    assert not (pp.truth / "rejected.md").exists()

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, resolved_note FROM file_write_proposals WHERE id = ?",
            (proposal_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row[0] == "denied"
    assert row[1] == "not now"


async def test_resolve_file_write_proposal_approves_yaml_and_oversize_md(
    fresh_db,
) -> None:
    """Regression: the resolver originally delegated to filesmod.write_text
    which only allows .md/.txt and caps at 100 KB. Truth/ holds specs,
    brand guidelines, contracts — often .yaml/.json/.toml, sometimes
    >100 KB. Both must approve cleanly now that truth.py does its own
    write."""
    from server.truth import resolve_file_write_proposal
    from server.paths import ensure_project_scaffold

    await init_db()
    pp = ensure_project_scaffold(MISC_PROJECT_ID)

    yaml_body = "primary_color: '#0066cc'\nsecondary_color: '#003366'\n"
    big_md = "# Spec\n\n" + ("paragraph. " * 12_000)  # ~144 KB > 100 KB
    assert len(big_md) > 100_000

    for path, content in (
        ("brand-colors.yaml", yaml_body),
        ("massive-spec.md",   big_md),
    ):
        c = await configured_conn()
        try:
            cur = await c.execute(
                "INSERT INTO file_write_proposals "
                "(project_id, proposer_id, path, proposed_content, summary) "
                "VALUES (?, ?, ?, ?, ?)",
                (MISC_PROJECT_ID, "coach", path, content, "test"),
            )
            await c.commit()
            proposal_id = cur.lastrowid
        finally:
            await c.close()

        res = await resolve_file_write_proposal(
            proposal_id, new_status="approved", note=None,
            actor={"source": "ui", "ip": "127.0.0.1", "ua": "test"},
        )
        assert res["status"] == "approved", f"failed for {path}: {res}"
        written = pp.truth / path
        assert written.is_file()
        assert written.read_text(encoding="utf-8") == content


async def test_truth_proposal_path_traversal_rejected(fresh_db) -> None:
    """A maliciously-crafted DB row with `../` segments must fail the
    truth_root.relative_to() check rather than escape into other
    project subdirs. (The MCP tool already rejects '..' at insert
    time, but defense-in-depth: a manual sqlite insert / migration
    bug shouldn't be able to bypass.)"""
    from server.truth import (
        FileWriteProposalBadRequest, resolve_file_write_proposal,
    )
    from server.paths import ensure_project_scaffold

    await init_db()
    ensure_project_scaffold(MISC_PROJECT_ID)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO file_write_proposals "
            "(project_id, proposer_id, path, proposed_content, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                MISC_PROJECT_ID, "coach",
                "../decisions/sneaky.md",  # would land in decisions/
                "x", "s",
            ),
        )
        await c.commit()
        proposal_id = cur.lastrowid
    finally:
        await c.close()

    try:
        await resolve_file_write_proposal(
            proposal_id, new_status="approved", note=None,
            actor={"source": "ui", "ip": "127.0.0.1", "ua": "test"},
        )
        assert False, "traversal should have raised FileWriteProposalBadRequest"
    except FileWriteProposalBadRequest as e:
        assert "truth/" in str(e)


async def test_resolve_file_write_proposal_idempotent_conflict(fresh_db) -> None:
    """A non-pending proposal can't be re-resolved — raises Conflict."""
    from server.truth import (
        FileWriteProposalConflict, resolve_file_write_proposal,
    )
    from server.paths import ensure_project_scaffold

    await init_db()
    ensure_project_scaffold(MISC_PROJECT_ID)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO file_write_proposals "
            "(project_id, proposer_id, path, proposed_content, summary, "
            "status, resolved_at, resolved_by) "
            "VALUES (?, ?, ?, ?, ?, 'approved', ?, 'human')",
            (
                MISC_PROJECT_ID, "coach", "already.md", "x", "s",
                "2026-04-28T00:00:00Z",
            ),
        )
        await c.commit()
        proposal_id = cur.lastrowid
    finally:
        await c.close()

    try:
        await resolve_file_write_proposal(
            proposal_id, new_status="denied", note=None,
            actor={"source": "ui", "ip": "127.0.0.1", "ua": "test"},
        )
        assert False, "second resolve should have raised FileWriteProposalConflict"
    except FileWriteProposalConflict as e:
        assert e.status == "approved"


# ---------- Auto-supersede on duplicate path -----------------------


async def _insert_pending_proposal(
    project_id: str, path: str, content: str = "x", summary: str = "s",
    proposer: str = "coach",
) -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO file_write_proposals "
            "(project_id, proposer_id, path, proposed_content, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, proposer, path, content, summary),
        )
        await c.commit()
        return cur.lastrowid
    finally:
        await c.close()


async def _row_status(proposal_id: int) -> tuple[str, str | None]:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, resolved_note FROM file_write_proposals WHERE id = ?",
            (proposal_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    return row[0], row[1]


async def test_supersede_status_accepted_by_check_constraint(
    fresh_db,
) -> None:
    """The schema CHECK now allows the new 'superseded' value."""
    await init_db()
    pid = await _insert_pending_proposal(MISC_PROJECT_ID, "specs.md")
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE file_write_proposals SET status='superseded' WHERE id=?",
            (pid,),
        )
        await c.commit()
    finally:
        await c.close()
    status, _ = await _row_status(pid)
    assert status == "superseded"


def test_propose_truth_update_in_coord_allowlist_unchanged(fresh_db) -> None:
    """Sanity: the supersede refactor didn't drop the tool from the
    allow list."""
    from server.tools import ALLOWED_COORD_TOOLS
    assert "mcp__coord__coord_propose_file_write" in ALLOWED_COORD_TOOLS


# ---------- truth-index.md template + scaffold --------------------


def test_truth_index_seeded_on_scaffold(fresh_db) -> None:
    """ensure_project_scaffold writes truth/truth-index.md from the
    checked-in template. The seeded body explains the lane and the
    proposal flow but imposes no expected-files manifest — the user
    populates it per project."""
    from server.paths import ensure_project_scaffold

    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    target = pp.truth / "truth-index.md"
    assert target.is_file(), "truth-index.md must be seeded"
    body = target.read_text(encoding="utf-8")
    # The template explains the lane and the propose flow.
    assert "coord_propose_file_write" in body
    # Crucially: NO `specs.md` (or any other) bullet — a hardcoded
    # default would make the harness pick a project type. The body
    # ships intentionally bullet-free.
    assert "`specs.md`" not in body


def test_truth_index_first_write_only(fresh_db) -> None:
    """Re-running scaffold doesn't clobber user edits to
    truth-index.md."""
    from server.paths import ensure_project_scaffold

    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    target = pp.truth / "truth-index.md"
    target.write_text("# my edits\n", encoding="utf-8")
    ensure_project_scaffold(MISC_PROJECT_ID)
    assert target.read_text(encoding="utf-8") == "# my edits\n"


def test_truth_index_seeded_when_truth_dir_missing(fresh_db) -> None:
    """Boot rescue scenario: a project that was created before
    truth/ shipped has no truth/ directory; running scaffold creates
    both the directory and truth-index.md."""
    from server.paths import ensure_project_scaffold, project_paths

    # Simulate "project created without truth/" — wipe the folder
    # the first scaffold call creates, then re-run.
    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    import shutil
    shutil.rmtree(pp.truth)
    assert not pp.truth.exists()
    ensure_project_scaffold(MISC_PROJECT_ID)
    assert (project_paths(MISC_PROJECT_ID).truth / "truth-index.md").is_file()


# ---------- Files-pane write_text editable allowlist --------------


async def test_write_text_accepts_editable_extensions(fresh_db) -> None:
    """The broadened EDITABLE_EXTS set lets users author code/config
    files (yaml, json, py, etc.) through the standard write endpoint —
    not just .md / .txt as the prior allowlist allowed."""
    from server import files as filesmod
    from server.paths import ensure_project_scaffold

    await init_db()
    ensure_project_scaffold(MISC_PROJECT_ID)
    for rel, body in (
        ("working/workspace/notes.yaml", "primary: '#0066cc'\n"),
        ("working/workspace/config.json", "{\"k\": 1}"),
        ("working/workspace/snippet.py", "x = 1\n"),
        ("working/workspace/Dockerfile", "FROM python:3.12\n"),
        ("truth/brand-colors.yaml", "primary: '#0066cc'\n"),
    ):
        result = await filesmod.write_text("project", rel, body)
        assert result["size"] == len(body.encode("utf-8"))


async def test_write_text_rejects_binary_extensions(fresh_db) -> None:
    """Binary formats stay out of the allowlist so a textarea write
    can't corrupt them. Users drop binary via kDrive instead."""
    import pytest

    from server import files as filesmod
    from server.paths import ensure_project_scaffold

    await init_db()
    ensure_project_scaffold(MISC_PROJECT_ID)
    for rel in (
        "truth/contract.pdf",
        "outputs/logo.png",
        "working/workspace/archive.zip",
    ):
        with pytest.raises(ValueError, match="not in editable allowlist"):
            await filesmod.write_text("project", rel, "x")


async def test_write_text_accepts_empty_body_for_stub_creation(
    fresh_db,
) -> None:
    """The Files-pane '+ new file' button posts an empty body to
    create a stub. write_text must accept this."""
    from server import files as filesmod
    from server.paths import ensure_project_scaffold, project_paths

    await init_db()
    ensure_project_scaffold(MISC_PROJECT_ID)
    await filesmod.write_text("project", "truth/specs.md", "")
    target = project_paths(MISC_PROJECT_ID).truth / "specs.md"
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == ""


async def test_write_text_create_only_refuses_existing(fresh_db) -> None:
    """Regression for the 'silent overwrite' footgun: with
    create_only=True, write_text raises FileAlreadyExists rather than
    truncating an existing file to the new (possibly empty) content.
    The Files-pane '+ new file' button passes create_only=True so a
    user typing the path of an existing file gets a 409 + error
    instead of losing their content."""
    import pytest

    from server import files as filesmod
    from server.paths import ensure_project_scaffold, project_paths

    await init_db()
    ensure_project_scaffold(MISC_PROJECT_ID)
    pp = project_paths(MISC_PROJECT_ID)
    target = pp.working_workspace / "existing.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("important content\n", encoding="utf-8")

    with pytest.raises(filesmod.FileAlreadyExists):
        await filesmod.write_text(
            "project",
            "working/workspace/existing.md",
            "",
            create_only=True,
        )
    # Existing content untouched.
    assert target.read_text(encoding="utf-8") == "important content\n"

    # Default (create_only=False) still overwrites — the save button's
    # contract is unchanged.
    await filesmod.write_text(
        "project",
        "working/workspace/existing.md",
        "new content\n",
    )
    assert target.read_text(encoding="utf-8") == "new content\n"


# ---------- Supersede SQL flow (mirrors propose tool's body) ------


async def test_supersede_invariant_at_most_one_pending_per_path(
    fresh_db,
) -> None:
    """End-to-end SQL test of the supersede sequence the tool uses:
    SELECT pending → INSERT new pending → UPDATE matched ones to
    'superseded'. Asserts the resulting state matches the documented
    invariant: at most one pending proposal per (project, path)."""
    await init_db()
    pid_v1 = await _insert_pending_proposal(MISC_PROJECT_ID, "specs.md", "v1")
    pid_other = await _insert_pending_proposal(
        MISC_PROJECT_ID, "brand.yaml", "other"
    )

    # Mirror the tool's body: SELECT, INSERT, UPDATE all in one txn.
    # The scope filter is load-bearing: a hypothetical truth/CLAUDE.md
    # proposal must NOT supersede a project_claude_md proposal at path
    # 'CLAUDE.md' (and vice versa). Tool body adds `AND scope = ?` for
    # exactly this reason.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id FROM file_write_proposals "
            "WHERE project_id = ? AND scope = ? AND path = ? "
            "AND status = 'pending'",
            (MISC_PROJECT_ID, "truth", "specs.md"),
        )
        ids = [r[0] for r in await cur.fetchall()]
        cur = await c.execute(
            "INSERT INTO file_write_proposals "
            "(project_id, proposer_id, scope, path, proposed_content, summary) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (MISC_PROJECT_ID, "coach", "truth", "specs.md", "v2", "s"),
        )
        pid_v2 = cur.lastrowid
        for sid in ids:
            await c.execute(
                "UPDATE file_write_proposals SET status = 'superseded', "
                "resolved_at = ?, resolved_by = 'system', "
                "resolved_note = ? WHERE id = ? AND status = 'pending'",
                ("2026-04-30T00:00:00Z", f"superseded by #{pid_v2}", sid),
            )
        await c.commit()
    finally:
        await c.close()

    # v1 (same path) is now superseded; v2 is the sole pending for specs.md.
    assert (await _row_status(pid_v1)) == (
        "superseded", f"superseded by #{pid_v2}"
    )
    assert (await _row_status(pid_v2))[0] == "pending"
    # An unrelated path's pending proposal is untouched.
    assert (await _row_status(pid_other))[0] == "pending"

    # Pending count for specs.md is exactly 1.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) FROM file_write_proposals "
            "WHERE project_id = ? AND scope = ? AND path = ? "
            "AND status = 'pending'",
            (MISC_PROJECT_ID, "truth", "specs.md"),
        )
        (count,) = await cur.fetchone()
    finally:
        await c.close()
    assert count == 1, f"invariant broken: {count} pending for specs.md"


async def test_supersede_does_not_touch_resolved_rows(fresh_db) -> None:
    """A previously approved/denied proposal for the same path must NOT
    be flipped to superseded — the UPDATE has WHERE status='pending'
    so resolved rows are protected."""
    await init_db()
    # Insert a row already in 'approved' state (simulate a past
    # approval), then run the supersede SELECT/UPDATE pattern.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO file_write_proposals "
            "(project_id, proposer_id, path, proposed_content, summary, "
            "status, resolved_at, resolved_by) "
            "VALUES (?, ?, ?, ?, ?, 'approved', ?, 'human')",
            (
                MISC_PROJECT_ID, "coach", "specs.md", "v0", "s",
                "2026-04-29T00:00:00Z",
            ),
        )
        await c.commit()
        pid_approved = cur.lastrowid
    finally:
        await c.close()

    pid_v1 = await _insert_pending_proposal(MISC_PROJECT_ID, "specs.md", "v1")

    # Simulate a fresh propose: the supersede UPDATE should only flip
    # pid_v1 (the pending row), not pid_approved.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id FROM file_write_proposals "
            "WHERE project_id = ? AND path = ? AND status = 'pending'",
            (MISC_PROJECT_ID, "specs.md"),
        )
        ids = [r[0] for r in await cur.fetchall()]
        for sid in ids:
            await c.execute(
                "UPDATE file_write_proposals SET status = 'superseded' "
                "WHERE id = ? AND status = 'pending'",
                (sid,),
            )
        await c.commit()
    finally:
        await c.close()

    assert (await _row_status(pid_approved))[0] == "approved", \
        "approved row must NOT have been flipped to superseded"
    assert (await _row_status(pid_v1))[0] == "superseded"


# ---------- Per-project CLAUDE.md stub gains truth section --------


def test_project_claude_md_stub_includes_truth_section(fresh_db) -> None:
    """New projects' per-project CLAUDE.md mentions the truth/ lane and
    the proposal-flow tool name, so a fresh Coach has the project-scoped
    reminder on every turn."""
    from server.paths import write_project_claude_md_stub, project_paths

    pp = project_paths(MISC_PROJECT_ID)
    if pp.claude_md.exists():
        pp.claude_md.unlink()
    pp.root.mkdir(parents=True, exist_ok=True)
    write_project_claude_md_stub(
        MISC_PROJECT_ID, "Misc", "test desc", None,
    )
    body = pp.claude_md.read_text(encoding="utf-8")
    assert "## truth/" in body
    assert "coord_propose_file_write" in body
    assert "truth-index.md" in body
    assert MISC_PROJECT_ID in body  # slug interpolated
    # New `## Updating this CLAUDE.md` section explaining the
    # project_claude_md scope; agents reading the stub on first turn
    # learn the proposal flow for editing CLAUDE.md itself.
    assert "## Updating this CLAUDE.md" in body
    assert "project_claude_md" in body


# ---------- File-guard hook covers project CLAUDE.md too -----------


async def test_file_guard_denies_write_to_project_claude_md(fresh_db) -> None:
    """The hook now hard-denies any agent Write/Edit/MultiEdit/
    NotebookEdit whose path resolves to a project's top-level
    CLAUDE.md, with a deny reason that names the right tool."""
    from server.agents import _pretool_file_guard_hook
    from server.paths import ensure_project_scaffold

    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    target = pp.claude_md
    res = await _pretool_file_guard_hook(
        {"tool_name": "Write", "tool_input": {"file_path": str(target)}},
        "tu_1",
        None,
    )
    out = res.get("hookSpecificOutput") or {}
    assert out.get("permissionDecision") == "deny"
    reason = out.get("permissionDecisionReason") or ""
    assert "Project CLAUDE.md" in reason or "project CLAUDE.md" in reason.lower()
    assert "coord_propose_file_write" in reason
    assert "project_claude_md" in reason


async def test_file_guard_allows_worktree_internal_claude_md(fresh_db) -> None:
    """A Player's worktree-internal repo CLAUDE.md
    (`<slug>/repo/<slot>/CLAUDE.md`) lives in the project tree but
    isn't the protected instruction file; the hook must let it
    through. Players' own repo files stay writable."""
    from server.agents import _pretool_file_guard_hook
    from server.paths import ensure_project_scaffold, project_paths

    ensure_project_scaffold(MISC_PROJECT_ID)
    pp = project_paths(MISC_PROJECT_ID)
    worktree_claude = pp.repo / "p1" / "CLAUDE.md"
    worktree_claude.parent.mkdir(parents=True, exist_ok=True)
    res = await _pretool_file_guard_hook(
        {"tool_name": "Write",
         "tool_input": {"file_path": str(worktree_claude)}},
        "tu_1",
        None,
    )
    assert res == {}, f"unexpected deny for {worktree_claude}: {res}"


async def test_file_guard_denies_bash_against_project_claude_md(
    fresh_db,
) -> None:
    """Bash commands containing `projects/<slug>/CLAUDE.md` are caught
    by the substring heuristic, mirroring the existing truth/ pattern."""
    from server.agents import _pretool_file_guard_hook
    from server.paths import ensure_project_scaffold

    ensure_project_scaffold(MISC_PROJECT_ID)
    cases = [
        "echo hello > /data/projects/misc/CLAUDE.md",
        "sed -i 's/foo/bar/' projects/misc/CLAUDE.md",
        "cp draft.md /data/projects/misc/CLAUDE.md",
    ]
    for cmd in cases:
        res = await _pretool_file_guard_hook(
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            "tu_1",
            None,
        )
        out = res.get("hookSpecificOutput") or {}
        assert out.get("permissionDecision") == "deny", \
            f"cmd not denied: {cmd}"


# ---------- coord_propose_file_write — project_claude_md scope -----


async def test_propose_file_write_project_claude_md_accepts_canonical_path(
    fresh_db,
) -> None:
    """scope='project_claude_md' with path='CLAUDE.md' queues a
    proposal with the right scope recorded."""
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold("misc")
    from server.tools import build_coord_server
    srv = build_coord_server("coach", include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_propose_file_write"]

    out = await _propose_truth(
        handler,
        scope="project_claude_md",
        path="CLAUDE.md",
        content="# Project: misc\n\n## Goal\nrebuilt body\n",
        summary="rebuild project CLAUDE.md",
    )
    assert out.get("isError") is not True
    text = out["content"][0]["text"]
    assert "scope=project_claude_md" in text

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT scope, path FROM file_write_proposals "
            "WHERE project_id = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1",
            (MISC_PROJECT_ID,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is not None
    assert row[0] == "project_claude_md"
    assert row[1] == "CLAUDE.md"


async def test_propose_file_write_project_claude_md_rejects_other_path(
    fresh_db,
) -> None:
    """For scope='project_claude_md' the only legal path is exactly
    'CLAUDE.md'. Anything else is rejected at propose time so a
    malformed call can't write to a sibling file."""
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold("misc")
    from server.tools import build_coord_server
    srv = build_coord_server("coach", include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_propose_file_write"]

    for bad in ("specs.md", "../truth/specs.md", "subdir/CLAUDE.md", ""):
        out = await _propose_truth(
            handler,
            scope="project_claude_md",
            path=bad,
            content="x",
            summary="s",
        )
        assert out.get("isError") is True, f"path {bad!r} should be rejected"


async def test_propose_file_write_rejects_unknown_scope(fresh_db) -> None:
    """Unknown scopes (incl. 'global_claude_md', which is not a valid
    proposal scope) are rejected at propose time."""
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold("misc")
    from server.tools import build_coord_server
    srv = build_coord_server("coach", include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_propose_file_write"]

    for bad_scope in ("global_claude_md", "knowledge", "decisions", ""):
        out = await _propose_truth(
            handler,
            scope=bad_scope,
            path="CLAUDE.md",
            content="x",
            summary="s",
        )
        assert out.get("isError") is True, \
            f"scope {bad_scope!r} should be rejected"


async def test_propose_file_write_supersede_isolated_by_scope(
    fresh_db,
) -> None:
    """A truth-scope proposal at path 'CLAUDE.md' (hypothetical) and a
    project_claude_md-scope proposal at path 'CLAUDE.md' must NOT
    supersede each other — the scope filter is load-bearing."""
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold("misc")
    from server.tools import build_coord_server
    srv = build_coord_server("coach", include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_propose_file_write"]

    # Truth-scope proposal at path 'CLAUDE.md' (oddly named truth file).
    out_truth = await _propose_truth(
        handler,
        scope="truth",
        path="CLAUDE.md",
        content="# Truth-side claude.md\n",
        summary="truth-side",
    )
    assert out_truth.get("isError") is not True

    # project_claude_md proposal at the canonical CLAUDE.md path.
    out_pcm = await _propose_truth(
        handler,
        scope="project_claude_md",
        path="CLAUDE.md",
        content="# Project: misc\n",
        summary="pcm",
    )
    assert out_pcm.get("isError") is not True

    # Both proposals should still be pending — neither superseded the
    # other since their scopes differ.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT scope, status FROM file_write_proposals "
            "WHERE project_id = ? ORDER BY id ASC",
            (MISC_PROJECT_ID,),
        )
        rows = await cur.fetchall()
    finally:
        await c.close()
    assert len(rows) == 2
    statuses = {(r[0], r[1]) for r in rows}
    assert ("truth", "pending") in statuses
    assert ("project_claude_md", "pending") in statuses


# ---------- Resolver: scope-aware writes --------------------------


async def test_resolve_file_write_proposal_project_claude_md_writes_target(
    fresh_db,
) -> None:
    """Approve flow for project_claude_md scope writes the project's
    CLAUDE.md (not anything under truth/) and marks the row."""
    from server.truth import resolve_file_write_proposal
    from server.paths import ensure_project_scaffold, project_paths

    await init_db()
    ensure_project_scaffold(MISC_PROJECT_ID)
    pp = project_paths(MISC_PROJECT_ID)
    # Make the file already exist with old content so we verify a true
    # overwrite (the typical edit case).
    pp.claude_md.write_text("old body\n", encoding="utf-8")

    new_body = "# Project: misc\n\n## Goal\nnew\n"
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO file_write_proposals "
            "(project_id, proposer_id, scope, path, "
            "proposed_content, summary) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (MISC_PROJECT_ID, "coach", "project_claude_md",
             "CLAUDE.md", new_body, "rebuild"),
        )
        await c.commit()
        proposal_id = cur.lastrowid
    finally:
        await c.close()

    res = await resolve_file_write_proposal(
        proposal_id, new_status="approved", note=None,
        actor={"source": "ui", "ip": "127.0.0.1", "ua": "test"},
    )
    assert res["status"] == "approved"
    assert res["scope"] == "project_claude_md"
    assert pp.claude_md.read_text(encoding="utf-8") == new_body


async def test_resolve_file_write_proposal_rejects_tampered_pcm_path(
    fresh_db,
) -> None:
    """If a row was tampered with so a project_claude_md proposal has
    a path other than 'CLAUDE.md', the resolver refuses the write
    (defense-in-depth even though the propose tool also rejects)."""
    from server.truth import (
        FileWriteProposalBadRequest, resolve_file_write_proposal,
    )
    from server.paths import ensure_project_scaffold

    await init_db()
    ensure_project_scaffold(MISC_PROJECT_ID)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO file_write_proposals "
            "(project_id, proposer_id, scope, path, "
            "proposed_content, summary) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (MISC_PROJECT_ID, "coach", "project_claude_md",
             "../truth/specs.md", "tampered", "x"),
        )
        await c.commit()
        proposal_id = cur.lastrowid
    finally:
        await c.close()

    try:
        await resolve_file_write_proposal(
            proposal_id, new_status="approved", note=None,
            actor={"source": "ui", "ip": "127.0.0.1", "ua": "test"},
        )
        assert False, "tampered path should have raised"
    except FileWriteProposalBadRequest as e:
        assert "CLAUDE.md" in str(e)


async def test_resolve_file_write_proposal_unknown_scope_rejected(
    fresh_db,
) -> None:
    """A row whose scope column was set to an unknown value (e.g. via
    a future schema migration that adds a scope, then is rolled back)
    raises rather than silently skipping the write."""
    from server.truth import (
        FileWriteProposalBadRequest, resolve_file_write_proposal,
    )
    from server.paths import ensure_project_scaffold

    await init_db()
    ensure_project_scaffold(MISC_PROJECT_ID)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO file_write_proposals "
            "(project_id, proposer_id, scope, path, "
            "proposed_content, summary) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (MISC_PROJECT_ID, "coach", "future_scope",
             "anywhere", "x", "s"),
        )
        await c.commit()
        proposal_id = cur.lastrowid
    finally:
        await c.close()

    try:
        await resolve_file_write_proposal(
            proposal_id, new_status="approved", note=None,
            actor={"source": "ui", "ip": "127.0.0.1", "ua": "test"},
        )
        assert False, "unknown scope should have raised"
    except FileWriteProposalBadRequest as e:
        assert "scope" in str(e).lower()


# ---------- resolve_target_path is exported ------------------------


def test_resolve_target_path_dispatches_by_scope() -> None:
    """The diff endpoint in main.py imports resolve_target_path
    from server.truth — verify it's exposed and dispatches correctly
    without going through the full resolver path."""
    from server.truth import resolve_target_path
    from server.paths import project_paths

    pp = project_paths(MISC_PROJECT_ID)
    truth_target = resolve_target_path({
        "scope": "truth",
        "project_id": MISC_PROJECT_ID,
        "path": "specs.md",
    })
    assert truth_target == (pp.truth / "specs.md").resolve()

    pcm_target = resolve_target_path({
        "scope": "project_claude_md",
        "project_id": MISC_PROJECT_ID,
        "path": "CLAUDE.md",
    })
    assert pcm_target == pp.claude_md


# ---------- coord_read_file (universal project-file reader) -------


async def _read_via_handler(caller_id: str, path: str) -> dict[str, Any]:
    from server.tools import build_coord_server
    srv = build_coord_server(caller_id, include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_read_file"]
    return await handler({"path": path})


async def test_coord_read_file_in_allowlist(fresh_db) -> None:
    """coord_read_file is registered + in the MCP allowlist so the
    SDK actually accepts calls."""
    from server.tools import ALLOWED_COORD_TOOLS
    assert "mcp__coord__coord_read_file" in ALLOWED_COORD_TOOLS


async def test_coord_read_file_coach_reads_truth(fresh_db) -> None:
    """Coach can read a truth file even though Coach has no `Read`
    tool on Codex (the whole reason this tool exists). Body comes
    back verbatim with size annotation in the wrapper text."""
    await init_db()
    from server.paths import ensure_project_scaffold, project_paths
    ensure_project_scaffold(MISC_PROJECT_ID)
    pp = project_paths(MISC_PROJECT_ID)
    target = pp.truth / "specs.md"
    target.write_text("# Specs\nbody\n", encoding="utf-8")

    out = await _read_via_handler("coach", "truth/specs.md")
    assert out.get("isError") is not True
    text = out["content"][0]["text"]
    assert "# Specs" in text
    assert "body" in text


async def test_coord_read_file_player_reads_truth(fresh_db) -> None:
    """Players can call coord_read_file too — same handler, no
    caller_is_coach gate."""
    await init_db()
    from server.paths import ensure_project_scaffold, project_paths
    ensure_project_scaffold(MISC_PROJECT_ID)
    pp = project_paths(MISC_PROJECT_ID)
    target = pp.truth / "brand.md"
    target.write_text("brand\n", encoding="utf-8")

    out = await _read_via_handler("p1", "truth/brand.md")
    assert out.get("isError") is not True
    assert "brand" in out["content"][0]["text"]


async def test_coord_read_file_reads_decisions_knowledge_outputs(
    fresh_db,
) -> None:
    """Any file under the project root is readable: decisions/,
    working/knowledge/, outputs/. A single tool covers the whole tree
    so agents don't have to learn lane-specific tools just to read."""
    await init_db()
    from server.paths import ensure_project_scaffold, project_paths
    ensure_project_scaffold(MISC_PROJECT_ID)
    pp = project_paths(MISC_PROJECT_ID)
    cases = [
        (pp.decisions / "0001-foo.md", "## Decision 0001\nfoo\n", "decisions/0001-foo.md"),
        (pp.knowledge / "notes.md", "knowledge body\n", "working/knowledge/notes.md"),
        (pp.outputs / "report.md", "report body\n", "outputs/report.md"),
        (pp.claude_md, "# Project body\n", "CLAUDE.md"),
    ]
    for target, body, rel in cases:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        out = await _read_via_handler("coach", rel)
        assert out.get("isError") is not True, f"failed for {rel}: {out}"
        assert body.strip() in out["content"][0]["text"]


async def test_coord_read_file_rejects_traversal(fresh_db) -> None:
    """A path that resolves outside the project root via `..` is
    rejected by both the literal-segment check and the resolved-
    path anchoring (defense-in-depth)."""
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold(MISC_PROJECT_ID)

    for bad in ("../etc/passwd", "truth/../../../etc/passwd"):
        out = await _read_via_handler("coach", bad)
        assert out.get("isError") is True, f"path {bad!r} should be rejected"


async def test_coord_read_file_rejects_absolute_path(fresh_db) -> None:
    """Leading slash means the caller is trying to read a global
    path; rejected so this tool stays project-scoped."""
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold(MISC_PROJECT_ID)
    out = await _read_via_handler("coach", "/data/CLAUDE.md")
    assert out.get("isError") is True
    assert "leading slash" in out["content"][0]["text"].lower()


async def test_coord_read_file_missing_file(fresh_db) -> None:
    """A path under the project root that doesn't exist returns a
    clear 'file not found' error rather than a stack trace."""
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold(MISC_PROJECT_ID)
    out = await _read_via_handler("coach", "truth/nonexistent.md")
    assert out.get("isError") is True
    assert "not found" in out["content"][0]["text"].lower()


async def test_coord_read_file_oversize_rejected(fresh_db) -> None:
    """Files over 200 KB are refused — the tool isn't a streaming
    reader, and the surrounding system prompt has a budget."""
    await init_db()
    from server.paths import ensure_project_scaffold, project_paths
    ensure_project_scaffold(MISC_PROJECT_ID)
    pp = project_paths(MISC_PROJECT_ID)
    big = pp.knowledge / "big.md"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_text("x" * 200_001, encoding="utf-8")
    out = await _read_via_handler("coach", "working/knowledge/big.md")
    assert out.get("isError") is True
    assert "too large" in out["content"][0]["text"].lower()


async def test_coord_read_file_rejects_binary(fresh_db) -> None:
    """Non-UTF-8 files (binary outputs, images) are rejected with a
    clear error rather than returning garbage characters."""
    await init_db()
    from server.paths import ensure_project_scaffold, project_paths
    ensure_project_scaffold(MISC_PROJECT_ID)
    pp = project_paths(MISC_PROJECT_ID)
    binary = pp.outputs / "logo.png"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe\xfd")
    out = await _read_via_handler("coach", "outputs/logo.png")
    assert out.get("isError") is True
    assert "utf-8" in out["content"][0]["text"].lower()


async def test_coord_read_file_directory_rejected(fresh_db) -> None:
    """A path that points to a directory (not a file) returns a
    clear error."""
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold(MISC_PROJECT_ID)
    out = await _read_via_handler("coach", "truth")
    assert out.get("isError") is True
    assert "not a regular file" in out["content"][0]["text"].lower() \
        or "not found" in out["content"][0]["text"].lower()


# ---------- CHECK-constraint upgrade for legacy DBs --------------


async def test_init_db_rebuilds_file_write_proposals_check_constraint(
    fresh_db,
) -> None:
    """Simulate the historical truth_proposals table shape on disk
    (4-value status CHECK, no scope column, no `file_write_proposals`
    name), call init_db(), and verify the rebuild migration ran:
      - table is now `file_write_proposals` with 'superseded' in CHECK
      - existing row preserved with scope='truth' default
      - `superseded` insert succeeds (was the trip-wire from
        production where the rename had carried the old CHECK forward)
    """
    import aiosqlite
    from server.db import DB_PATH

    # First do a real init_db so the projects/agents tables and the
    # `misc` project row exist (mirrors production: legacy proposal
    # rows ALWAYS reference projects that already exist, because the
    # FK was enforced when those rows were originally inserted).
    await init_db()

    # Then nuke the proposal table and replant it under the legacy
    # shape: name=truth_proposals, no scope column, 4-value CHECK.
    # This is the precise on-disk shape Zeabur deploys hit before the
    # rebuild migration shipped.
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("DROP TABLE IF EXISTS file_write_proposals")
        await db.execute("DROP TABLE IF EXISTS truth_proposals")
        await db.execute(
            """
            CREATE TABLE truth_proposals (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                proposer_id       TEXT NOT NULL,
                path              TEXT NOT NULL,
                proposed_content  TEXT NOT NULL,
                summary           TEXT NOT NULL,
                status            TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending', 'approved', 'denied', 'cancelled')),
                created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                resolved_at       TEXT,
                resolved_by       TEXT,
                resolved_note     TEXT
            )
            """
        )
        await db.execute(
            "INSERT INTO truth_proposals "
            "(project_id, proposer_id, path, proposed_content, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            (MISC_PROJECT_ID, "coach", "specs.md", "old body", "legacy"),
        )
        await db.commit()

    # Run the boot sequence — should rename, ensure scope, then rebuild
    # to add 'superseded' to CHECK.
    await init_db()

    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        # Table renamed.
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('truth_proposals', 'file_write_proposals')"
        )
        names = {r[0] for r in await cur.fetchall()}
        assert "file_write_proposals" in names
        assert "truth_proposals" not in names

        # CHECK constraint includes the new value.
        cur = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='file_write_proposals'"
        )
        create_sql = (await cur.fetchone())[0]
        assert "'superseded'" in create_sql

        # Legacy row preserved with scope auto-defaulted to 'truth'.
        cur = await db.execute(
            "SELECT scope, status, path FROM file_write_proposals "
            "ORDER BY id ASC"
        )
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "truth"
        assert rows[0][1] == "pending"
        assert rows[0][2] == "specs.md"

        # Trip-wire: 'superseded' status now accepted.
        await db.execute(
            "UPDATE file_write_proposals SET status='superseded' WHERE id=?",
            (rows[0][0] if False else 1,),  # legacy row id is 1
        )
        await db.commit()

        cur = await db.execute(
            "SELECT status FROM file_write_proposals WHERE id=1"
        )
        assert (await cur.fetchone())[0] == "superseded"

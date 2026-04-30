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
    from server.agents import _pretool_truth_guard_hook
    from server.paths import ensure_project_scaffold

    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    target = pp.truth / "specs.md"
    res = await _pretool_truth_guard_hook(
        {"tool_name": "Write", "tool_input": {"file_path": str(target)}},
        "tu_1",
        None,
    )
    out = res.get("hookSpecificOutput") or {}
    assert out.get("permissionDecision") == "deny"
    assert "truth/" in (out.get("permissionDecisionReason") or "")


async def test_truth_guard_allows_writes_outside_truth(fresh_db) -> None:
    """Writes to non-truth paths (working/, knowledge/, etc.) pass through."""
    from server.agents import _pretool_truth_guard_hook
    from server.paths import ensure_project_scaffold

    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    for safe in (
        pp.working_workspace / "scratch.txt",
        pp.knowledge / "notes.md",
        pp.decisions / "0001-something.md",
        pp.outputs / "deck.pdf",
    ):
        res = await _pretool_truth_guard_hook(
            {"tool_name": "Write", "tool_input": {"file_path": str(safe)}},
            "tu_1",
            None,
        )
        assert res == {}, f"unexpected deny for {safe}: {res}"


async def test_truth_guard_denies_edit_and_multiedit(fresh_db) -> None:
    """All four file-mutating tools route through file_path the same way."""
    from server.agents import _pretool_truth_guard_hook
    from server.paths import ensure_project_scaffold

    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    target = str(pp.truth / "brand.md")
    for tool_name in ("Edit", "MultiEdit", "NotebookEdit"):
        key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
        res = await _pretool_truth_guard_hook(
            {"tool_name": tool_name, "tool_input": {key: target}},
            "tu_1",
            None,
        )
        out = res.get("hookSpecificOutput") or {}
        assert out.get("permissionDecision") == "deny", \
            f"{tool_name} should have been denied"


async def test_truth_guard_denies_bash_writing_into_truth(fresh_db) -> None:
    """Bash redirects into truth/ are caught by the substring heuristic."""
    from server.agents import _pretool_truth_guard_hook
    from server.paths import ensure_project_scaffold

    ensure_project_scaffold(MISC_PROJECT_ID)
    cases = [
        "echo hello > truth/specs.md",
        "cat input.txt >> /data/projects/misc/truth/brand.md",
        "cp deck.pdf truth/deck.pdf",
    ]
    for cmd in cases:
        res = await _pretool_truth_guard_hook(
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            "tu_1",
            None,
        )
        out = res.get("hookSpecificOutput") or {}
        assert out.get("permissionDecision") == "deny", f"cmd not denied: {cmd}"


async def test_truth_guard_lets_innocuous_bash_through(fresh_db) -> None:
    """Bash commands that don't reference truth/ pass through (no false-deny)."""
    from server.agents import _pretool_truth_guard_hook

    for cmd in (
        "ls -la",
        "git status",
        "echo trustworthy",       # contains 'trust' but not 'truth/'
        "cat /data/projects/misc/working/notes.md",
    ):
        res = await _pretool_truth_guard_hook(
            {"tool_name": "Bash", "tool_input": {"command": cmd}},
            "tu_1",
            None,
        )
        assert res == {}, f"unexpected deny for cmd: {cmd!r} -> {res}"


# ---------- Truth proposals (Coach proposes → human approves) -----


def test_coord_propose_truth_update_in_coord_allowlist() -> None:
    """The new tool is registered so the SDK will actually accept calls.
    Coach-only enforcement is in the tool body (caller_is_coach
    rejection); the tool is in the shared coord allowlist alongside
    other Coach-rejecting tools like coord_write_decision."""
    from server.tools import ALLOWED_COORD_TOOLS

    assert "mcp__coord__coord_propose_truth_update" in ALLOWED_COORD_TOOLS


async def _propose_truth(handler, **kwargs):
    """Test helper — wraps a coord_propose_truth_update handler call."""
    args = {"path": kwargs.get("path", ""),
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
    handler = srv["_handlers"]["coord_propose_truth_update"]

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
    handler = srv["_handlers"]["coord_propose_truth_update"]

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
    handler = srv["_handlers"]["coord_propose_truth_update"]

    out = await _propose_truth(
        handler,
        path="CLAUDE.md",
        content="rule body",
        summary="add zeabur scope rule",
    )
    assert out.get("isError") is not True
    text = out["content"][0]["text"]
    assert "queued" in text or "proposal" in text.lower()


async def test_truth_proposals_schema_smoke(fresh_db) -> None:
    """truth_proposals table exists, accepts an insert, status defaults
    to 'pending', and the CHECK constraint rejects bad statuses."""
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO truth_proposals "
            "(project_id, proposer_id, path, proposed_content, summary) "
            "VALUES (?, ?, ?, ?, ?)",
            (MISC_PROJECT_ID, "coach", "specs.md", "body", "summary"),
        )
        await c.commit()
        row_id = cur.lastrowid
        cur = await c.execute(
            "SELECT status FROM truth_proposals WHERE id = ?", (row_id,),
        )
        row = await cur.fetchone()
        assert row[0] == "pending"

        import sqlite3
        try:
            await c.execute(
                "INSERT INTO truth_proposals "
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


async def test_resolve_truth_proposal_approve_writes_file_and_marks_row(
    fresh_db,
) -> None:
    """Approve flow: pending row → file written under truth/ → row
    marked approved. The actor goes into the event payload."""
    from server.truth import resolve_truth_proposal
    from server.paths import ensure_project_scaffold

    await init_db()
    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO truth_proposals "
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

    res = await resolve_truth_proposal(
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
            "SELECT status, resolved_by, resolved_note FROM truth_proposals "
            "WHERE id = ?", (proposal_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row[0] == "approved"
    assert row[1] == "human"
    assert row[2] == "LGTM"


async def test_resolve_truth_proposal_deny_does_not_write(fresh_db) -> None:
    """Deny: row marked denied, NO file write under truth/."""
    from server.truth import resolve_truth_proposal
    from server.paths import ensure_project_scaffold

    await init_db()
    pp = ensure_project_scaffold(MISC_PROJECT_ID)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO truth_proposals "
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

    res = await resolve_truth_proposal(
        proposal_id, new_status="denied", note="not now",
        actor={"source": "ui", "ip": "127.0.0.1", "ua": "test"},
    )
    assert res["status"] == "denied"

    assert not (pp.truth / "rejected.md").exists()

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status, resolved_note FROM truth_proposals WHERE id = ?",
            (proposal_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row[0] == "denied"
    assert row[1] == "not now"


async def test_resolve_truth_proposal_approves_yaml_and_oversize_md(
    fresh_db,
) -> None:
    """Regression: the resolver originally delegated to filesmod.write_text
    which only allows .md/.txt and caps at 100 KB. Truth/ holds specs,
    brand guidelines, contracts — often .yaml/.json/.toml, sometimes
    >100 KB. Both must approve cleanly now that truth.py does its own
    write."""
    from server.truth import resolve_truth_proposal
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
                "INSERT INTO truth_proposals "
                "(project_id, proposer_id, path, proposed_content, summary) "
                "VALUES (?, ?, ?, ?, ?)",
                (MISC_PROJECT_ID, "coach", path, content, "test"),
            )
            await c.commit()
            proposal_id = cur.lastrowid
        finally:
            await c.close()

        res = await resolve_truth_proposal(
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
        TruthProposalBadRequest, resolve_truth_proposal,
    )
    from server.paths import ensure_project_scaffold

    await init_db()
    ensure_project_scaffold(MISC_PROJECT_ID)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO truth_proposals "
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
        await resolve_truth_proposal(
            proposal_id, new_status="approved", note=None,
            actor={"source": "ui", "ip": "127.0.0.1", "ua": "test"},
        )
        assert False, "traversal should have raised TruthProposalBadRequest"
    except TruthProposalBadRequest as e:
        assert "truth/" in str(e)


async def test_resolve_truth_proposal_idempotent_conflict(fresh_db) -> None:
    """A non-pending proposal can't be re-resolved — raises Conflict."""
    from server.truth import (
        TruthProposalConflict, resolve_truth_proposal,
    )
    from server.paths import ensure_project_scaffold

    await init_db()
    ensure_project_scaffold(MISC_PROJECT_ID)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO truth_proposals "
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
        await resolve_truth_proposal(
            proposal_id, new_status="denied", note=None,
            actor={"source": "ui", "ip": "127.0.0.1", "ua": "test"},
        )
        assert False, "second resolve should have raised TruthProposalConflict"
    except TruthProposalConflict as e:
        assert e.status == "approved"

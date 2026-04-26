"""Tests for Phase 6 (PROJECTS_SPEC.md §9): wiki + LLM-Wiki skill +
global CLAUDE.md bootstrap.

Exercise `bootstrap_global_resources()` directly. The /api/health
integration is covered by the existing health endpoint logic — these
tests focus on file-write behavior + status reporting since that's
what the spec asks for.
"""

from __future__ import annotations

import server.paths as pathsmod
from server.paths import (
    bootstrap_global_resources,
    bootstrap_status,
    global_paths,
)


def test_bootstrap_writes_all_three_files_on_first_boot(fresh_db) -> None:
    """First boot of a fresh data root: INDEX.md, SKILL.md, and
    CLAUDE.md are all written from templates. Status: bootstrapped."""
    status = bootstrap_global_resources()
    assert status == "bootstrapped"
    assert bootstrap_status() == "bootstrapped"

    gp = global_paths()
    assert gp.wiki.is_dir()
    assert gp.wiki_index.is_file()
    assert gp.claude_md.is_file()

    skill_dir = gp.skills / "llm-wiki"
    assert skill_dir.is_dir()
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file()


def test_bootstrap_idempotent_on_second_boot(fresh_db) -> None:
    """Second boot with files already present: status flips to
    'present', files are unchanged."""
    bootstrap_global_resources()
    gp = global_paths()
    # Mutate the existing files so we can confirm they aren't rewritten.
    gp.wiki_index.write_text("CUSTOM INDEX", encoding="utf-8")
    gp.claude_md.write_text("CUSTOM CLAUDE", encoding="utf-8")
    skill_md = gp.skills / "llm-wiki" / "SKILL.md"
    skill_md.write_text("CUSTOM SKILL", encoding="utf-8")

    status = bootstrap_global_resources()
    assert status == "present"
    assert bootstrap_status() == "present"

    assert gp.wiki_index.read_text(encoding="utf-8") == "CUSTOM INDEX"
    assert gp.claude_md.read_text(encoding="utf-8") == "CUSTOM CLAUDE"
    assert skill_md.read_text(encoding="utf-8") == "CUSTOM SKILL"


def test_bootstrap_partial_writes_status_bootstrapped(fresh_db) -> None:
    """Pre-create one file, boot: only the missing files are written,
    status is still 'bootstrapped' because *something* was written."""
    gp = global_paths()
    gp.wiki.mkdir(parents=True, exist_ok=True)
    gp.wiki_index.write_text("PRE-EXISTING", encoding="utf-8")

    status = bootstrap_global_resources()
    assert status == "bootstrapped"

    # The existing file is preserved.
    assert gp.wiki_index.read_text(encoding="utf-8") == "PRE-EXISTING"
    # The missing files were written.
    assert gp.claude_md.is_file()
    assert (gp.skills / "llm-wiki" / "SKILL.md").is_file()


def test_bootstrap_index_stub_has_required_sections(fresh_db) -> None:
    """The auto-written INDEX.md stub must contain the section
    headers that Phase 7's auto-maintain logic will append under."""
    bootstrap_global_resources()
    body = global_paths().wiki_index.read_text(encoding="utf-8")
    assert "# Wiki Index" in body
    assert "## Cross-project entries" in body
    assert "## Per-project entries" in body


def test_bootstrap_skill_md_has_yaml_frontmatter(fresh_db) -> None:
    """SKILL.md must start with a `---` YAML block so Claude Code's
    skill-matcher can parse the `description` for trigger matching."""
    bootstrap_global_resources()
    body = (global_paths().skills / "llm-wiki" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert body.startswith("---\n")
    assert "name: llm-wiki" in body
    assert "description:" in body


def test_bootstrap_claude_md_has_active_project_block(fresh_db) -> None:
    """The injected global CLAUDE.md must contain the
    `## Active project (this conversation)` block — agents read the
    placeholders to find slug/name/repo."""
    bootstrap_global_resources()
    body = global_paths().claude_md.read_text(encoding="utf-8")
    assert "## Active project (this conversation)" in body
    assert "<injected_slug>" in body
    assert "<injected_name>" in body


def test_bootstrap_status_missing_when_template_unreadable(
    fresh_db, monkeypatch
) -> None:
    """If a template can't be read (corrupted install, missing file),
    status is 'missing' so /api/health can flag the failure instead of
    silently leaving the harness in a half-bootstrapped state."""
    monkeypatch.setattr(pathsmod, "_read_template", lambda name: "")
    status = bootstrap_global_resources()
    assert status == "missing"
    assert bootstrap_status() == "missing"


def test_reset_bootstrap_status(fresh_db) -> None:
    """`reset_bootstrap_status()` flips the cache back to 'missing'
    so a fresh test in the same process doesn't see leftover state."""
    bootstrap_global_resources()
    assert bootstrap_status() == "bootstrapped"
    pathsmod.reset_bootstrap_status()
    assert bootstrap_status() == "missing"


def test_bootstrap_health_style_restat_detects_missing_after_boot(
    fresh_db,
) -> None:
    """Phase 6 audit (PROJECTS_SPEC.md §9): /api/health re-stats the
    three sentinel files on each call so an out-of-band rm flips the
    health status to 'missing' immediately — without waiting for a
    server restart. This test mirrors the /api/health logic without
    needing FastAPI/TestClient.

    The cached `bootstrap_status()` stays at 'bootstrapped' (boot-time
    verb), but the re-stat downgrades the live status to 'missing'."""
    bootstrap_global_resources()
    gp = global_paths()
    skill_md = gp.skills / "llm-wiki" / "SKILL.md"
    sentinels = [gp.wiki_index, skill_md, gp.claude_md]

    # All present immediately after bootstrap.
    assert all(p.exists() for p in sentinels)

    # Simulate an out-of-band delete (admin cleanup, disk fault).
    skill_md.unlink()

    # Live re-stat detects the missing file even though the cached
    # status is still "bootstrapped".
    assert bootstrap_status() == "bootstrapped"
    live_missing = [p for p in sentinels if not p.exists()]
    assert live_missing == [skill_md]


def test_bootstrap_health_style_restat_recovers_when_files_reappear(
    fresh_db,
) -> None:
    """Phase 6 audit (PROJECTS_SPEC.md §9 + main.py /api/health logic):
    if `bootstrap_status()` is 'missing' from a prior failed boot but
    the sentinel files were later created (manual fix), /api/health
    should report green by re-stating and downgrading 'missing' to
    'present'."""
    # Simulate a prior failed boot.
    pathsmod.reset_bootstrap_status()
    assert bootstrap_status() == "missing"

    # Manually stage the sentinel files (admin-side recovery).
    gp = global_paths()
    gp.wiki.mkdir(parents=True, exist_ok=True)
    gp.wiki_index.write_text("manually-added", encoding="utf-8")
    gp.claude_md.write_text("manually-added", encoding="utf-8")
    skill_dir = gp.skills / "llm-wiki"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("manually-added", encoding="utf-8")

    # Live sentinel check passes — /api/health would flip to "present".
    sentinels = [gp.wiki_index, skill_dir / "SKILL.md", gp.claude_md]
    assert all(p.exists() for p in sentinels)
    # Cache is still 'missing' (boot-time history) but the health
    # endpoint's re-stat-and-downgrade is what users see.
    assert bootstrap_status() == "missing"

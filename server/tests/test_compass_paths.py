"""Tests for server.compass.paths — pure path resolution + scaffold.

Phase 0 of the Compass build. Verifies that:
  - Per-project paths resolve under the harness's per-project layout
  - Scaffolding creates the expected directory structure
  - The kDrive remote-path mapper produces the documented shape
  - Test fixtures that sandbox `paths.DATA_ROOT` flow through
"""

from __future__ import annotations

import server.paths as pathsmod
from server.compass import paths as cpaths


def test_compass_paths_resolve_under_project_working_tree(fresh_db: str) -> None:
    cp = cpaths.compass_paths("alpha")
    pp_root = pathsmod.project_paths("alpha").working
    assert cp.root == pp_root / "compass"
    assert cp.lattice == cp.root / "lattice.json"
    assert cp.regions == cp.root / "regions.json"
    assert cp.questions == cp.root / "questions.json"
    assert cp.audits == cp.root / "audits.jsonl"
    assert cp.runs == cp.root / "runs.jsonl"
    assert cp.claude_md_block == cp.root / "claude_md_block.md"
    assert cp.briefings_dir == cp.root / "briefings"
    assert cp.proposals_dir == cp.root / "proposals"
    assert cp.settle_proposals == cp.proposals_dir / "settle.json"
    assert cp.stale_proposals == cp.proposals_dir / "stale.json"
    assert cp.duplicate_proposals == cp.proposals_dir / "duplicates.json"
    # Truth lives outside compass/ entirely (in the project's truth/ lane).
    assert not hasattr(cp, "truth")


def test_compass_paths_isolated_per_project(fresh_db: str) -> None:
    a = cpaths.compass_paths("alpha")
    b = cpaths.compass_paths("beta")
    assert a.root != b.root
    assert "alpha" in str(a.root) and "beta" not in str(a.root)
    assert "beta" in str(b.root) and "alpha" not in str(b.root)


def test_briefing_path_uses_iso_date(fresh_db: str) -> None:
    cp = cpaths.compass_paths("alpha")
    p = cp.briefing_for("2026-05-01")
    assert p == cp.briefings_dir / "briefing-2026-05-01.md"
    # No validation: the helper trusts the caller and a synthetic
    # string flows through unchanged so test fixtures stay simple.
    p2 = cp.briefing_for("zzzz-zz-zz")
    assert p2.name == "briefing-zzzz-zz-zz.md"


def test_ensure_compass_scaffold_is_idempotent(fresh_db: str) -> None:
    cp1 = cpaths.ensure_compass_scaffold("alpha")
    assert cp1.root.is_dir()
    assert cp1.briefings_dir.is_dir()
    assert cp1.proposals_dir.is_dir()
    # Run again — must not raise and must produce the same paths.
    cp2 = cpaths.ensure_compass_scaffold("alpha")
    assert cp1 == cp2
    assert cp2.root.is_dir()


def test_ensure_compass_scaffold_does_not_seed_state(fresh_db: str) -> None:
    """State files (lattice.json etc.) are owned by `store.bootstrap_state`.
    The scaffold only creates directories so the storage layer can
    distinguish "never bootstrapped" from "empty lattice"."""
    cp = cpaths.ensure_compass_scaffold("alpha")
    assert not cp.lattice.exists()
    assert not cp.regions.exists()
    assert not cp.questions.exists()
    assert not cp.audits.exists()
    assert not cp.runs.exists()
    assert not cp.claude_md_block.exists()


def test_remote_root_drops_working_segment() -> None:
    """kDrive remote tree mirrors the knowledge/ convention — flat
    `projects/<id>/compass/` for the human-facing cloud view, not
    the local `working/compass/` lane."""
    assert cpaths.remote_root("alpha") == "projects/alpha/compass"
    assert cpaths.remote_root("misc") == "projects/misc/compass"


def test_remote_path_joins_segments_safely() -> None:
    assert cpaths.remote_path("alpha", "lattice.json") == "projects/alpha/compass/lattice.json"
    assert (
        cpaths.remote_path("alpha", "briefings", "briefing-2026-05-01.md")
        == "projects/alpha/compass/briefings/briefing-2026-05-01.md"
    )
    # Leading slashes on segments must not bypass the base path —
    # webdav.py:_resolve has a hard-won comment about this footgun.
    assert (
        cpaths.remote_path("alpha", "/proposals/settle.json")
        == "projects/alpha/compass/proposals/settle.json"
    )
    # Empty segments are dropped, not turned into "//".
    assert cpaths.remote_path("alpha", "", "lattice.json") == "projects/alpha/compass/lattice.json"
    # Backslashes get normalized so a Windows-style path doesn't
    # confuse the WebDAV layer.
    assert (
        cpaths.remote_path("alpha", "briefings\\briefing-2026-05-01.md")
        == "projects/alpha/compass/briefings/briefing-2026-05-01.md"
    )

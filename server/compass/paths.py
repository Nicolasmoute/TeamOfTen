"""Per-project Compass directory resolver.

Two paths matter:
  - **Local** under `<project>/working/compass/` — same lane as
    knowledge / memory (mutable working state). Source of truth.
  - **Remote** under `projects/<id>/compass/` on kDrive — flatter
    user-facing tree, mirrors the knowledge/decisions convention.

Layout (per spec §6, adapted to the harness):

    /data/projects/<id>/working/compass/
      lattice.json                  active + archived statements
      truth.json                    truth-protected facts
      regions.json                  region taxonomy + merge history
      questions.json                pending + answered + digested
      audits.jsonl                  append-only audit log
      runs.jsonl                    append-only run log
      claude_md_block.md            last-rendered CLAUDE.md block
      briefings/
        briefing-YYYY-MM-DD.md
      proposals/
        settle.json                 pending settle proposals
        stale.json                  pending stale proposals
        duplicates.json             pending duplicate-merge proposals

Functions are sync (no DB or kDrive lookup). The active project must
be passed in by the caller, which already resolves it via
`server.db.resolve_active_project()`. Keeping these sync makes path
helpers trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from server.paths import project_paths


@dataclass(frozen=True)
class CompassPaths:
    """All filesystem paths Compass uses for one project.

    Note: there is NO `truth` path here — truth lives in the
    project-level `<project>/truth/` folder (resolved via
    `server.paths.project_paths(project_id).truth`), not under
    `working/compass/`. Compass reads truth via
    `server.compass.truth.read_truth_facts` and never writes it.
    """

    project_id: str
    root: Path
    lattice: Path
    regions: Path
    questions: Path
    audits: Path
    runs: Path
    claude_md_block: Path
    briefings_dir: Path
    proposals_dir: Path
    settle_proposals: Path
    stale_proposals: Path
    duplicate_proposals: Path

    def briefing_for(self, date_iso: str) -> Path:
        """Path to a single day's briefing. `date_iso` should be
        YYYY-MM-DD; we don't validate here so test fixtures can pass
        synthetic strings without a real date."""
        return self.briefings_dir / f"briefing-{date_iso}.md"


def compass_paths(project_id: str) -> CompassPaths:
    """Resolve Compass's per-project local layout.

    Routes through `server.paths.project_paths` so test fixtures that
    monkeypatch `paths.DATA_ROOT` flow through automatically — no
    duplicate root-resolution logic.
    """
    pp = project_paths(project_id)
    root = pp.working / "compass"
    proposals = root / "proposals"
    return CompassPaths(
        project_id=project_id,
        root=root,
        lattice=root / "lattice.json",
        regions=root / "regions.json",
        questions=root / "questions.json",
        audits=root / "audits.jsonl",
        runs=root / "runs.jsonl",
        claude_md_block=root / "claude_md_block.md",
        briefings_dir=root / "briefings",
        proposals_dir=proposals,
        settle_proposals=proposals / "settle.json",
        stale_proposals=proposals / "stale.json",
        duplicate_proposals=proposals / "duplicates.json",
    )


def ensure_compass_scaffold(project_id: str) -> CompassPaths:
    """Create the per-project Compass folder tree on first use.

    Idempotent — re-running on an existing tree is a no-op. Does NOT
    seed any state files; that's `store.bootstrap_state()`.
    """
    cp = compass_paths(project_id)
    cp.root.mkdir(parents=True, exist_ok=True)
    cp.briefings_dir.mkdir(parents=True, exist_ok=True)
    cp.proposals_dir.mkdir(parents=True, exist_ok=True)
    return cp


def remote_root(project_id: str) -> str:
    """kDrive path for a project's Compass tree.

    Note: drops the `working/` segment for the remote path — kDrive
    is human-facing and a flatter tree is friendlier. Matches the
    knowledge/ and memory/ conventions in `server.knowledge` /
    `server.tools.coord_update_memory`.

    Returns a posix-style relative string (no leading slash). The
    webdav client treats paths as relative to `HARNESS_WEBDAV_URL`.
    """
    return str(PurePosixPath("projects") / project_id / "compass")


def remote_path(project_id: str, *segments: str) -> str:
    """Join a relative path under the project's Compass remote root.

    Use for state files, briefings, proposals — anything that needs a
    remote address. `segments` are joined with posix separators and
    stripped of leading slashes so the caller can pass `"lattice.json"`
    or `"briefings/briefing-2026-05-01.md"` without worrying about
    leading-slash quirks (httpx treats those as host-root-relative,
    bypassing the WebDAV base URL — see `webdav.py:_resolve`).
    """
    parts = [remote_root(project_id)]
    for seg in segments:
        if not seg:
            continue
        clean = str(seg).replace("\\", "/").lstrip("/")
        if clean:
            parts.append(clean)
    return str(PurePosixPath(*parts))


__all__ = [
    "CompassPaths",
    "compass_paths",
    "ensure_compass_scaffold",
    "remote_root",
    "remote_path",
]

"""Harness-wide Playbook directory resolver.

Two locations matter:
  - **Local** at `${HARNESS_DATA_ROOT}/playbook/` — source of truth.
  - **Remote** at `TOT/playbook/` on kDrive — durability mirror.

Layout (per spec §2):

    /data/playbook/
      lattice.json    active statements (cap soft 100, hard 110)
      archived.json   settled / stale_low / stale_unused / merged
                      / superseded / deleted
      runs.jsonl      one line per reflection / bootstrap run

Functions are sync (no DB lookup, no kDrive I/O). Lazy `mkdir` on
access so a fresh deploy works without an explicit init step (spec
§4.5 step 1).

Distinct from Compass:
  - Compass paths are per-project; Playbook is harness-wide (no
    project_id parameter).
  - Compass writes briefings + proposals subfolders; Playbook does
    not (no briefings, no persisted proposals — both deferred to v2
    or out of scope per spec §1.5).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from server.paths import DATA_ROOT


@dataclass(frozen=True)
class PlaybookPaths:
    """All filesystem paths Playbook uses. Single instance per process —
    harness-wide, no project scoping."""

    root: Path
    lattice: Path
    archived: Path
    runs: Path


def playbook_paths() -> PlaybookPaths:
    """Resolve Playbook's local layout. Idempotent — does NOT mkdir.

    Routes through `server.paths.DATA_ROOT` so test fixtures that
    monkeypatch the data root flow through automatically.
    """
    root = DATA_ROOT / "playbook"
    return PlaybookPaths(
        root=root,
        lattice=root / "lattice.json",
        archived=root / "archived.json",
        runs=root / "runs.jsonl",
    )


def ensure_playbook_dir() -> PlaybookPaths:
    """Create `/data/playbook/` on first use. Idempotent — re-running
    on an existing tree is a no-op. Does NOT seed any state files;
    that's `store.load_lattice()` (which tolerates missing files per
    spec §4.5 step 2) and `bootstrap.run_bootstrap()`.
    """
    pp = playbook_paths()
    pp.root.mkdir(parents=True, exist_ok=True)
    return pp


def remote_root() -> str:
    """kDrive path for the Playbook tree.

    Matches the harness convention: kDrive root is `TOT/`, then
    subsystem subfolder. Posix-style relative string (no leading
    slash) — `webdav.write_text` treats paths as relative to the
    configured WebDAV base URL.
    """
    return str(PurePosixPath("TOT") / "playbook")


def remote_path(*segments: str) -> str:
    """Join a relative path under the Playbook remote root.

    Use for state files: `remote_path("lattice.json")` →
    `"TOT/playbook/lattice.json"`. Segments are normalized (backslashes
    → forward slashes, leading slashes stripped) so callers can pass
    raw filenames.
    """
    parts = [remote_root()]
    for seg in segments:
        if not seg:
            continue
        clean = str(seg).replace("\\", "/").lstrip("/")
        if clean:
            parts.append(clean)
    return str(PurePosixPath(*parts))


__all__ = [
    "PlaybookPaths",
    "playbook_paths",
    "ensure_playbook_dir",
    "remote_root",
    "remote_path",
]

"""Harness-wide Playbook directory resolver.

Two locations matter:
  - **Local** at `${HARNESS_DATA_ROOT}/playbook/` — source of truth.
  - **Remote** at `playbook/` relative to the WebDAV base URL (which
    already points at whatever folder the operator chose on their
    cloud drive), so the effective on-disk path is
    `<webdav-base>/playbook/` — durability mirror.

Layout (per spec §2):

    /data/playbook/
      lattice.json    active statements (cap soft 100, hard 110)
      archived.json   settled / stale_low / stale_unused / merged
                      / superseded / deleted
      runs.jsonl      one line per reflection / bootstrap run

Functions are sync (no DB lookup, no cloud-drive I/O). Lazy `mkdir` on
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
    """Cloud-drive path for the Playbook tree.

    Returns a posix-style path relative to the configured WebDAV base
    URL, which already points at whatever folder the operator chose
    on their cloud drive — so this returns just `"playbook"`, never
    with a leading wrapper segment. Mirrors the convention used by
    project_sync / compass / decisions / knowledge / outputs.
    """
    return "playbook"


def remote_path(*segments: str) -> str:
    """Join a relative path under the Playbook remote root.

    Use for state files: `remote_path("lattice.json")` →
    `"playbook/lattice.json"`. The effective on-disk path on the cloud
    drive is `<webdav-base>/playbook/lattice.json` (everything above
    `playbook/` comes from the WebDAV base URL, not from this string).
    Segments are normalized (backslashes → forward slashes, leading
    slashes stripped) so callers can pass raw filenames.
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

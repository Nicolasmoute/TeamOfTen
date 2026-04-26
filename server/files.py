"""Browsable-file backend for the UI's file explorer pane.

Whitelists a small set of named roots on disk (context, knowledge,
decisions) and exposes three operations: walk the tree, read a file,
write a file. Writes are routed through the owning module so side
effects — cache invalidation, kDrive mirror, event emission — fire
the same way they would if the edit came from an MCP tool.

Every path goes through _resolve() which:
  - joins the root's real path with the relative path,
  - calls Path.resolve(),
  - re-checks that the result is inside the root,
refusing anything that would escape via `..`, symlinks, or Windows
drive prefixes.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server import knowledge as knowmod
from server.paths import DATA_ROOT, project_paths

logger = logging.getLogger("harness.files")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Default text-file-size cap for reads — anything bigger is probably
# a binary blob an agent dropped, and rendering it in a textarea would
# just lock the UI.
READ_MAX_BYTES = 256_000
# Skip directories that have no business being exposed — they'd be noise
# and in some cases security-relevant (.git config, SQLite's WAL sidecars).
SKIP_DIRNAMES = {".git", "__pycache__", ".venv", "node_modules"}


@dataclass(frozen=True)
class Root:
    key: str
    path: Path
    writable: bool  # True = we have a safe write path for this root
    scope: str = "legacy"  # 'global' | 'project' | 'legacy'
    project_id: str | None = None
    label: str | None = None


def _resolve_active_sync() -> str:
    """Synchronous active-project read so _roots() (called from sync
    code paths like /api/files/tree) doesn't need to await."""
    import sqlite3

    from server.db import DB_PATH, MISC_PROJECT_ID

    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        try:
            cur = conn.execute(
                "SELECT value FROM team_config WHERE key = 'active_project_id'"
            )
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        return MISC_PROJECT_ID
    if not row or not row[0]:
        return MISC_PROJECT_ID
    return str(row[0])


def _project_label(project_id: str) -> str:
    """Look up a project's display name. Falls back to the slug on
    any DB hiccup so the files pane never shows an empty label."""
    import sqlite3

    from server.db import DB_PATH

    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        try:
            cur = conn.execute(
                "SELECT name FROM projects WHERE id = ?", (project_id,)
            )
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        return project_id
    if not row:
        return project_id
    return str(row[0]) or project_id


def _roots() -> dict[str, Root]:
    """Whitelist of roots, computed each call so env-var changes and
    project switches are picked up without a restart.

    Phase 5 (PROJECTS_SPEC.md §7) reshapes the legacy 8-root flat list
    into two scoped roots:
      - `global` → /data (parent of projects/, holds CLAUDE.md, skills/,
                  mcp/, wiki/, snapshots/, etc.)
      - `project` → /data/projects/<active>/ for the active project.

    Legacy roots (context/knowledge/decisions/workspaces/outputs/
    uploads/plans/handoffs) stay registered so existing read/write
    callers and tests keep working — they overlap with the new scoped
    roots at the path level, which is fine because every root just
    sandboxes accesses to its sub-tree.
    """
    active = _resolve_active_sync()
    pp = project_paths(active)
    roots: dict[str, Root] = {
        # Phase 5: top-level scoped roots.
        "global": Root(
            "global",
            DATA_ROOT,
            writable=True,
            scope="global",
            label="Root (global)",
        ),
        "project": Root(
            "project",
            pp.root,
            writable=True,
            scope="project",
            project_id=active,
            label=_project_label(active),
        ),
        # Legacy flat roots (context/knowledge/decisions/workspaces/
        # outputs/uploads/plans/handoffs) were retired in projects_v2:
        # the corresponding /data/<flat>/ dirs were wiped, the writers
        # now route through `project_paths(active)`, and the UI only
        # renders the two scoped roots above.
    }
    return roots


def list_roots() -> list[dict[str, Any]]:
    """Phase 5 (PROJECTS_SPEC.md §7) payload — only the two scoped
    roots are surfaced to the UI. Legacy roots are accessible via the
    other endpoints (tree/read/write) by key but not enumerated here.

    Each entry carries:
      - `id` — unique handle the UI uses for tree/read/write calls.
      - `key` — alias of `id` for older clients still keying off `key`.
      - `label` — human-readable header text.
      - `path` — absolute on-disk path (for the file-link resolver).
      - `scope` — 'global' | 'project'.
      - `project_id` — set on `scope='project'` only.
      - `writable` / `exists` — UI permission + missing-dir hints.
    """
    out: list[dict[str, Any]] = []
    for r in _roots().values():
        if r.scope == "legacy":
            continue
        exists = r.path.exists()
        out.append(
            {
                "id": r.key,
                "key": r.key,
                "writable": r.writable,
                "exists": exists,
                "label": r.label or r.key,
                "path": str(r.path),
                "scope": r.scope,
                "project_id": r.project_id,
            }
        )
    return out


def _resolve(root_key: str, relative: str) -> Path:
    """Resolve a user-supplied relative path under a named root, refusing
    anything that escapes via .., symlinks, or root-absolute paths.

    Empty relative means 'the root itself'.
    """
    roots = _roots()
    if root_key not in roots:
        raise ValueError(f"unknown root: {root_key}")
    base = roots[root_key].path.resolve()
    # Normalize the relative — strip leading / and any drive letter.
    rel = (relative or "").lstrip("/\\")
    target = (base / rel).resolve() if rel else base
    # The target must be `base` itself or sit strictly below it.
    try:
        target.relative_to(base)
    except ValueError:
        raise ValueError("path escapes root")
    return target


def tree(root_key: str) -> dict[str, Any]:
    """Recursive tree under a root. Directories sort before files; names
    are lexicographic within each group. Missing root → empty tree."""
    root = _resolve(root_key, "")
    if not root.exists():
        return {"name": root_key, "type": "dir", "children": []}
    # Phase 5 (§7): the `global` root is /data itself, which contains
    # `projects/` (other projects' trees — only the active one surfaces
    # under the `project` root, not via `global`), `harness.db` (and
    # its WAL sidecars), `claude/` (OAuth tokens), and `attachments/`
    # for legacy deploys. Skip these so the global tree shows what
    # the spec lists: CLAUDE.md, skills/, mcp/, wiki/, plus snapshots/
    # if present.
    extra_skip: set[str] = set()
    if root_key == "global":
        extra_skip = {
            "projects",       # active project enumerated via the `project` root
            "claude",          # OAuth tokens
            "attachments",     # legacy flat dir; per-project is under project tree
            "harness.db",      # SQLite (binary)
            "harness.db-journal",
            "harness.db-wal",
            "harness.db-shm",
        }
    return _walk(root, root, extra_skip=extra_skip)


def _walk(base: Path, current: Path, *, extra_skip: set[str] | None = None) -> dict[str, Any]:
    try:
        entries = list(current.iterdir())
    except OSError:
        return {"name": current.name, "type": "dir", "children": []}

    children: list[dict[str, Any]] = []
    skip = SKIP_DIRNAMES | (extra_skip or set())
    # Skip symlinks entirely. The workspaces tree has per-slot
    # `attachments/` symlinks pointing at /data/attachments which is
    # cross-root; following them would leak upload content into every
    # Player's tree. Regular files + real directories only.
    dirs = sorted(
        (e for e in entries
         if e.is_dir() and not e.is_symlink() and e.name not in skip),
        key=lambda e: e.name.lower(),
    )
    files = sorted(
        (e for e in entries
         if e.is_file() and not e.is_symlink() and e.name not in skip),
        key=lambda e: e.name.lower(),
    )
    for d in dirs:
        # The extra_skip set only applies at the top level — once we
        # descend into a child dir, normal SKIP_DIRNAMES rules govern.
        children.append(_walk(base, d))
    for f in files:
        try:
            st = f.stat()
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        children.append(
            {
                "name": f.name,
                "type": "file",
                "size": size,
                "mtime": mtime,
                "path": str(f.relative_to(base)).replace("\\", "/"),
            }
        )
    name = "" if current == base else current.name
    return {
        "name": name,
        "type": "dir",
        "path": str(current.relative_to(base)).replace("\\", "/") if current != base else "",
        "children": children,
    }


def read_text(root_key: str, relative: str) -> dict[str, Any]:
    """Return file contents as UTF-8 text (with replacement for bad
    bytes). Binary files still decode — the UI can choose to hide them
    — but oversize files are refused."""
    target = _resolve(root_key, relative)
    if not target.is_file():
        raise FileNotFoundError(f"not a file: {root_key}/{relative}")
    st = target.stat()
    if st.st_size > READ_MAX_BYTES:
        raise ValueError(
            f"file too large for inline view: {st.st_size} bytes (cap {READ_MAX_BYTES})"
        )
    data = target.read_bytes()
    text = data.decode("utf-8", errors="replace")
    return {
        "root": root_key,
        "path": relative,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "content": text,
    }


async def write_text(root_key: str, relative: str, content: str) -> dict[str, Any]:
    """Save `content` to the target file under `root_key`.

    All writes are now plain disk writes — the legacy ctxmod routing
    (which validated + mirrored to kDrive on its own) is gone with
    projects_v2. The two surviving roots (`global`, `project`) cover
    every editable path; per-project sync handles the kDrive mirror.
    """
    roots = _roots()
    if root_key not in roots or not roots[root_key].writable:
        raise PermissionError(f"root not writable: {root_key}")

    target = _resolve(root_key, relative)

    # Refuse non-text writes — binary through a textarea would corrupt.
    # Size cap aligns with knowmod.MAX_BODY_CHARS so an agent writing
    # via the MCP tool and a human writing via the file browser hit the
    # same ceiling.
    if target.suffix.lower() not in {".md", ".txt"}:
        raise ValueError("only .md and .txt files are editable through this endpoint")
    if len(content) > knowmod.MAX_BODY_CHARS:
        raise ValueError(
            f"body too long ({len(content)} chars, max {knowmod.MAX_BODY_CHARS})"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {
        "root": root_key,
        "path": relative,
        "size": len(content.encode("utf-8")),
        "routed_through": "disk",
    }

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
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server import context as ctxmod
from server import knowledge as knowmod

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


def _roots() -> dict[str, Root]:
    """Whitelist of roots, computed each call so env-var changes are
    picked up without a restart (useful in tests)."""
    return {
        "context": Root(
            "context",
            Path(os.environ.get("HARNESS_CONTEXT_DIR", "/data/context")),
            writable=True,
        ),
        "knowledge": Root(
            "knowledge",
            Path(os.environ.get("HARNESS_KNOWLEDGE_DIR", "/data/knowledge")),
            writable=True,
        ),
        "decisions": Root(
            "decisions",
            Path(os.environ.get("HARNESS_DECISIONS_DIR", "/data/decisions")),
            writable=False,  # decisions are append-only records, not editable
        ),
        "workspaces": Root(
            "workspaces",
            Path(os.environ.get("HARNESS_WORKSPACES_DIR", "/workspaces")),
            # Read-only from the UI. Each Player has a git worktree at
            # /workspaces/<slot>/project/ on branch work/<slot>; edits
            # from the browser would fight the agent's git operations
            # (auto-commit, pull, etc). Use coord_commit_push or git
            # CLI to change files, then reload the tree here.
            writable=False,
        ),
    }


def list_roots() -> list[dict[str, Any]]:
    """Lightweight metadata for the UI to render the top level."""
    out: list[dict[str, Any]] = []
    for r in _roots().values():
        exists = r.path.exists()
        out.append(
            {
                "key": r.key,
                "writable": r.writable,
                "exists": exists,
                "label": r.key,
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
    return _walk(root, root)


def _walk(base: Path, current: Path) -> dict[str, Any]:
    try:
        entries = list(current.iterdir())
    except OSError:
        return {"name": current.name, "type": "dir", "children": []}

    children: list[dict[str, Any]] = []
    # Skip symlinks entirely. The workspaces tree has per-slot
    # `attachments/` symlinks pointing at /data/attachments which is
    # cross-root; following them would leak upload content into every
    # Player's tree. Regular files + real directories only.
    dirs = sorted(
        (e for e in entries
         if e.is_dir() and not e.is_symlink() and e.name not in SKIP_DIRNAMES),
        key=lambda e: e.name.lower(),
    )
    files = sorted(
        (e for e in entries if e.is_file() and not e.is_symlink()),
        key=lambda e: e.name.lower(),
    )
    for d in dirs:
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
    """Save `content` to the target file. Routes through owning modules
    when the root has special side effects (context → ctxmod.write for
    kDrive mirror + cache bust); raw disk write for plain roots."""
    roots = _roots()
    if root_key not in roots or not roots[root_key].writable:
        raise PermissionError(f"root not writable: {root_key}")

    target = _resolve(root_key, relative)

    if root_key == "context":
        # Route through ctxmod so we get validation, kDrive mirror, and
        # list-cache invalidation. We just need to reconstruct (kind,
        # name) from the relative path.
        kind, name = _context_kind_name(relative)
        await ctxmod.write(kind, name, content)
        return {
            "root": root_key,
            "path": relative,
            "size": len(content.encode("utf-8")),
            "routed_through": "ctxmod",
        }

    # Generic writable root — ensure parent dir, refuse non-.md for now
    # (binary writes through a textarea would corrupt). Size cap aligns
    # with knowmod.MAX_BODY_CHARS so an agent writing via the MCP tool
    # and a human writing via the file browser hit the same ceiling.
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


def _context_kind_name(relative: str) -> tuple[str, str]:
    """Map a path inside the context root to ctxmod's (kind, name) tuple.

    Examples:
      "CLAUDE.md"                → ("root",   "CLAUDE")
      "skills/debug.md"          → ("skills", "debug")
      "rules/no-mocks.md"        → ("rules",  "no-mocks")
    """
    p = Path(relative.replace("\\", "/"))
    parts = p.parts
    if len(parts) == 1:
        # Top-level file — only CLAUDE.md is valid today.
        if parts[0] != "CLAUDE.md":
            raise ValueError(
                "only CLAUDE.md is allowed at the root of the context tree"
            )
        return "root", "CLAUDE"
    if len(parts) == 2 and parts[0] in ("skills", "rules") and parts[1].endswith(".md"):
        return parts[0], parts[1][:-3]
    raise ValueError(f"unsupported context path: {relative}")

"""Knowledge bucket — agent-produced durable text artifacts.

Where decisions/ is append-only ADRs ("we chose X because Y") and
memory/ is overwrite-on-update scratchpad, knowledge is free-form
text that lives longer than scratchpad but doesn't need ADR-style
formality: reports, research notes, specs, design docs, references,
anything an agent decides is worth keeping.

**Project-scoped.** Per PROJECTS_SPEC.md §4, knowledge lives at
`/data/projects/<active_project>/knowledge/` and mirrors to kDrive at
`TOT/projects/<active_project>/knowledge/`. The active project is
resolved at write/read time, so a project switch routes new entries
to the new project automatically.

Write is fan-in: both Coach and Players can call coord_write_knowledge.
Read/list is anybody. The file explorer pane shows the tree under the
active project's `knowledge/` and lets the human edit too.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path, PurePosixPath

from server.db import resolve_active_project
from server.paths import project_paths
from server.webdav import webdav

logger = logging.getLogger("harness.knowledge")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Per-component name rule: rejects traversal, shell specials, whitespace,
# and anything longer than 64 chars per segment.
COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Max 4 levels so the tree stays navigable. "reports/2026/04/weekly.md"
# is fine; deeper than that is almost certainly a mistake.
MAX_DEPTH = 4

# Reports and research can legitimately be long; ceiling prevents UI
# freezes / kDrive pain.
MAX_BODY_CHARS = 100_000

# Only text. Binaries (images, pdfs) should use the attachments
# endpoint or coord_save_output, not knowledge.
ALLOWED_SUFFIXES = {".md", ".txt"}


def validate(relative_path: str) -> str | None:
    """Return None if the path is acceptable, else a human-readable error."""
    if not relative_path:
        return "path is required"
    raw_parts = relative_path.replace("\\", "/").split("/")
    if any(seg in ("", ".", "..") for seg in raw_parts):
        return "path must not contain empty, '.' or '..' segments"
    p = PurePosixPath(relative_path.replace("\\", "/"))
    parts = p.parts
    if any(seg in ("", ".", "..") for seg in parts):
        return "path must not contain . or .. segments"
    if len(parts) > MAX_DEPTH:
        return f"path too deep (max {MAX_DEPTH} segments)"
    for seg in parts[:-1]:
        if not COMPONENT_RE.match(seg):
            return f"invalid directory name: {seg!r}"
    leaf = parts[-1]
    stem, sep, ext = leaf.rpartition(".")
    if not sep:
        return "leaf must include an extension (.md or .txt)"
    if ("." + ext.lower()) not in ALLOWED_SUFFIXES:
        return f"only .md / .txt are allowed (got .{ext})"
    if not COMPONENT_RE.match(stem + "." + ext):
        return f"invalid filename: {leaf!r}"
    return None


async def _local(relative_path: str) -> Path:
    """Resolve to the active project's knowledge dir + relative path."""
    project_id = await resolve_active_project()
    pp = project_paths(project_id)
    return pp.knowledge / PurePosixPath(relative_path.replace("\\", "/"))


async def _remote(relative_path: str) -> str:
    """kDrive remote path under the active project's knowledge tree."""
    project_id = await resolve_active_project()
    return str(
        PurePosixPath("projects") / project_id / "knowledge"
        / relative_path.replace("\\", "/")
    )


async def write(relative_path: str, content: str, author: str = "agent") -> bool:
    """Write to local + mirror to kDrive. Returns True on local success.

    Raises ValueError on invalid path, empty body, or oversize body.
    Routes to the active project's `knowledge/` (PROJECTS_SPEC.md §4).
    """
    err = validate(relative_path)
    if err:
        raise ValueError(err)
    if not content or not content.strip():
        raise ValueError("body is required (empty knowledge docs are not useful)")
    if len(content) > MAX_BODY_CHARS:
        raise ValueError(f"body too long ({len(content)} chars, max {MAX_BODY_CHARS})")
    lp = await _local(relative_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    try:
        lp.write_text(content, encoding="utf-8")
    except Exception:
        logger.exception("knowledge write failed locally: %s", lp)
        return False
    if webdav.enabled:
        await webdav.write_text(await _remote(relative_path), content)
    logger.info("knowledge write: %s by=%s (%d chars)", relative_path, author, len(content))
    return True


async def read(relative_path: str) -> str | None:
    err = validate(relative_path)
    if err:
        raise ValueError(err)
    lp = await _local(relative_path)
    if lp.exists():
        try:
            return lp.read_text(encoding="utf-8")
        except Exception:
            logger.exception("knowledge read failed locally: %s", lp)
    if webdav.enabled:
        body = await webdav.read_text(await _remote(relative_path))
        if body is not None:
            try:
                lp.parent.mkdir(parents=True, exist_ok=True)
                lp.write_text(body, encoding="utf-8")
            except Exception:
                logger.exception("knowledge cache write failed: %s", lp)
            return body
    return None


async def list_paths() -> list[str]:
    """Flat list of every knowledge doc in the active project,
    POSIX-relative paths, sorted. Used by the coord_list_knowledge tool
    so agents can discover what's already been written. The file
    explorer pane reads this via /api/files endpoints instead.
    """
    project_id = await resolve_active_project()
    pp = project_paths(project_id)
    if not pp.knowledge.exists():
        return []
    out: list[str] = []
    for p in pp.knowledge.rglob("*"):
        if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES:
            out.append(str(p.relative_to(pp.knowledge)).replace("\\", "/"))
    out.sort()
    return out

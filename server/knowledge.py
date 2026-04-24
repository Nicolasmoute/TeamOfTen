"""Knowledge bucket — agent-produced durable artifacts.

Where context is the "rules of the team" layer (fixed shape:
CLAUDE.md + skills/* + rules/*), knowledge is free-form: reports,
research notes, specs, design docs, anything an agent decides is
worth keeping. Paths are agent-chosen within sane limits.

Source of truth: local `/data/knowledge/` (HARNESS_KNOWLEDGE_DIR).
Mirror: kDrive `knowledge/<path>`, synchronous on write.

Write is fan-in: both Coach and Players can call coord_write_knowledge.
Read/list is anybody. The file explorer pane shows the tree and lets
the human edit too.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path, PurePosixPath

from server.webdav import webdav

logger = logging.getLogger("harness.knowledge")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


KNOWLEDGE_DIR = Path(os.environ.get("HARNESS_KNOWLEDGE_DIR", "/data/knowledge"))

# Per-component name rule: same safe alphabet as context, plus the .md/.txt
# extension on the leaf. Rejects traversal, shell specials, whitespace,
# and anything longer than 64 chars per segment.
COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Max 4 levels so the tree stays navigable. "reports/2026/04/weekly.md"
# is fine; deeper than that is almost certainly a mistake.
MAX_DEPTH = 4

# Bigger than context — reports and research can legitimately be long,
# but we still need a ceiling to prevent UI freezes / kDrive pain.
MAX_BODY_CHARS = 100_000

# Only text for now. Binaries (images, pdfs) should use the attachments
# endpoint, not knowledge.
ALLOWED_SUFFIXES = {".md", ".txt"}


def validate(relative_path: str) -> str | None:
    """Return None if the path is acceptable, else a human-readable error."""
    if not relative_path:
        return "path is required"
    # Check raw segments BEFORE handing to PurePosixPath: that class
    # silently strips '.' and normalizes, so './foo.md' would otherwise
    # pass validation and land at knowledge/foo.md (harmless but
    # surprising — reject explicitly so paths are always taken at face
    # value). Leading '/' also split into an empty first segment.
    raw_parts = relative_path.replace("\\", "/").split("/")
    if any(seg in ("", ".", "..") for seg in raw_parts):
        return "path must not contain empty, '.' or '..' segments"
    # Normalize separators.
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


def _local(relative_path: str) -> Path:
    return KNOWLEDGE_DIR / PurePosixPath(relative_path.replace("\\", "/"))


def _remote(relative_path: str) -> str:
    return str(PurePosixPath("knowledge") / relative_path.replace("\\", "/"))


async def write(relative_path: str, content: str, author: str = "agent") -> bool:
    """Write to local + mirror to kDrive. Returns True on local success.

    Raises ValueError on invalid path, empty body, or oversize body.
    """
    err = validate(relative_path)
    if err:
        raise ValueError(err)
    if not content or not content.strip():
        raise ValueError("body is required (empty knowledge docs are not useful)")
    if len(content) > MAX_BODY_CHARS:
        raise ValueError(f"body too long ({len(content)} chars, max {MAX_BODY_CHARS})")
    lp = _local(relative_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    try:
        lp.write_text(content, encoding="utf-8")
    except Exception:
        logger.exception("knowledge write failed locally: %s", lp)
        return False
    if webdav.enabled:
        await webdav.write_text(_remote(relative_path), content)
    logger.info("knowledge write: %s by=%s (%d chars)", relative_path, author, len(content))
    return True


async def read(relative_path: str) -> str | None:
    err = validate(relative_path)
    if err:
        raise ValueError(err)
    lp = _local(relative_path)
    if lp.exists():
        try:
            return lp.read_text(encoding="utf-8")
        except Exception:
            logger.exception("knowledge read failed locally: %s", lp)
    if webdav.enabled:
        body = await webdav.read_text(_remote(relative_path))
        if body is not None:
            try:
                lp.parent.mkdir(parents=True, exist_ok=True)
                lp.write_text(body, encoding="utf-8")
            except Exception:
                logger.exception("knowledge cache write failed: %s", lp)
            return body
    return None


def list_paths() -> list[str]:
    """Flat list of every knowledge doc, POSIX-relative paths, sorted.
    Tree view in the UI reads this via the /api/files endpoints instead
    — this helper is for the MCP coord_list_knowledge tool so agents
    can discover what's already been written without reaching for the
    Glob tool (which is scoped to their worktree cwd, not /data).
    """
    if not KNOWLEDGE_DIR.exists():
        return []
    out: list[str] = []
    for p in KNOWLEDGE_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES:
            out.append(str(p.relative_to(KNOWLEDGE_DIR)).replace("\\", "/"))
    out.sort()
    return out

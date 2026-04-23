"""Outputs bucket — binary deliverables the team ships.

Distinct from `knowledge` (text-only: .md / .txt, 100KB cap) and
`attachments` (UI paste-target for images, not mirrored anywhere):
`outputs` is where Players drop the finished binary artifacts —
docx, pdf, png charts, zip bundles, anything the human asked for.

Source of truth: local `/data/outputs/` (HARNESS_OUTPUTS_DIR).
Mirror: kDrive `outputs/<path>`, synchronous on write.

Write via coord_save_output (base64-encoded content, since MCP tool
arguments are JSON strings). Read from the files pane (UI) or via
the `outputs` root in the file explorer. No MCP read tool — agents
producing work don't usually need to read each other's binary
outputs; text handoffs go through knowledge/.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import sys
from pathlib import Path, PurePosixPath

from server.kdrive import kdrive

logger = logging.getLogger("harness.outputs")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


OUTPUTS_DIR = Path(os.environ.get("HARNESS_OUTPUTS_DIR", "/data/outputs"))

# Same validation rules as knowledge: safe alphabet, bounded depth.
COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_DEPTH = 4

# 20 MB hard ceiling — big enough for typical docx / pdf / png, small
# enough that a single runaway agent can't flood the volume.
MAX_BYTES = 20 * 1024 * 1024

# Narrow allow-list. Add here before agents can ship new formats; any
# format outside this list is rejected even if base64 is valid.
ALLOWED_SUFFIXES = {
    ".docx", ".xlsx", ".pptx",
    ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".zip", ".tar", ".gz",
    ".csv", ".tsv",
    ".md", ".txt", ".html", ".json",
}


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
        return "leaf must include an extension"
    ext_dot = "." + ext.lower()
    if ext_dot not in ALLOWED_SUFFIXES:
        return f"extension {ext_dot} not in outputs allow-list"
    if not COMPONENT_RE.match(stem + "." + ext):
        return f"invalid filename: {leaf!r}"
    return None


def _local(relative_path: str) -> Path:
    return OUTPUTS_DIR / PurePosixPath(relative_path.replace("\\", "/"))


def _remote(relative_path: str) -> str:
    return str(PurePosixPath("outputs") / relative_path.replace("\\", "/"))


async def save(relative_path: str, data: bytes, author: str = "agent") -> bool:
    """Write binary `data` to local + mirror to kDrive. Returns True
    on local success. Raises ValueError on invalid path or oversize."""
    err = validate(relative_path)
    if err:
        raise ValueError(err)
    if not data:
        raise ValueError("content is required")
    if len(data) > MAX_BYTES:
        raise ValueError(
            f"content too large ({len(data)} bytes, max {MAX_BYTES})"
        )
    lp = _local(relative_path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    try:
        lp.write_bytes(data)
    except Exception:
        logger.exception("outputs write failed locally: %s", lp)
        return False
    if kdrive.enabled:
        await kdrive.write_bytes(_remote(relative_path), data)
    logger.info(
        "outputs write: %s by=%s (%d bytes)", relative_path, author, len(data),
    )
    return True


def decode_base64(b64: str) -> bytes:
    """Decode a base64 string permissively — strip whitespace, accept
    url-safe or standard alphabet. Raises ValueError on bad input."""
    if not b64:
        raise ValueError("base64 content is empty")
    cleaned = re.sub(r"\s+", "", b64)
    # Auto-detect url-safe variant.
    if "-" in cleaned or "_" in cleaned:
        try:
            return base64.urlsafe_b64decode(cleaned + "=" * (-len(cleaned) % 4))
        except Exception as e:
            raise ValueError(f"urlsafe-base64 decode failed: {e}") from e
    try:
        return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4), validate=False)
    except Exception as e:
        raise ValueError(f"base64 decode failed: {e}") from e

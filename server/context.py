"""Context docs — governance layer above the shared scratchpad.

Three buckets of markdown files that shape how every agent behaves:

    context/CLAUDE.md          — single top-level doc, always injected
    context/skills/<name>.md   — reusable how-tos, all always injected
    context/rules/<name>.md    — hard rules, all always injected

Source of truth: kDrive (so a volume loss doesn't erase governance).
Local cache: `/data/context/` (subject to `HARNESS_CONTEXT_DIR`) so
agent spawns don't hit the network every turn and so the app still
works when kDrive is unreachable.

Write ACL:
    - Coach writes via `coord_write_context` MCP tool (see tools.py).
    - Human writes via `POST /api/context/{kind}/{name}` (see main.py).
    - Players CANNOT write — they have read-only access via the loader.

On any successful write we refresh the local cache synchronously and
emit a `context_updated` event so open UIs re-render live.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path, PurePosixPath

from server.kdrive import kdrive

logger = logging.getLogger("harness.context")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


CONTEXT_DIR = Path(os.environ.get("HARNESS_CONTEXT_DIR", "/data/context"))

# Only names matching this pattern are allowed — keeps the path safe
# against traversal and keeps filenames readable on kDrive's WebDAV.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# The three kinds we support. Anything else is rejected at the API edge.
VALID_KINDS: tuple[str, ...] = ("root", "skills", "rules")

# Size cap — same as decisions. A single context doc over 40 KB is
# almost certainly a mistake and would bloat every agent's system
# prompt for the rest of its life.
MAX_BODY_CHARS = 40_000

# TTL cache for list_all(). build_system_prompt_suffix() is called on
# every agent spawn; without this the WebDAV round trips would
# dominate turn latency. Cache is busted on every write/delete from
# this process. External edits (e.g. via the Infomaniak web UI) get
# picked up within the TTL window.
_LIST_TTL_SECONDS = 60.0
_list_cache: dict[str, list[str]] | None = None
_list_cache_at: float = 0.0


def _local_path(kind: str, name: str) -> Path:
    """Resolve a kind+name pair to a disk path under CONTEXT_DIR.

    kind=='root' is the special single-file case for CLAUDE.md.
    """
    if kind == "root":
        return CONTEXT_DIR / "CLAUDE.md"
    return CONTEXT_DIR / kind / (name + ".md")


def _remote_path(kind: str, name: str) -> str:
    """Same as _local_path but as a POSIX path relative to kDrive root."""
    if kind == "root":
        return "context/CLAUDE.md"
    return str(PurePosixPath("context") / kind / (name + ".md"))


def validate(kind: str, name: str) -> str | None:
    """Return None if kind+name is valid, else a human-readable error."""
    if kind not in VALID_KINDS:
        return f"invalid kind: {kind}"
    if kind == "root":
        if name and name != "CLAUDE":
            return "root kind only accepts name='CLAUDE' or empty"
        return None
    if not NAME_RE.match(name or ""):
        return f"invalid name: {name}"
    return None


async def write(kind: str, name: str, content: str) -> bool:
    """Write to both local cache and kDrive mirror. Returns True if the
    local write succeeds (kDrive is best-effort per our usual model —
    failures there are logged, not raised).

    Raises ValueError for invalid kind/name, empty body, or oversize
    body — so every caller (MCP tool, HTTP endpoint) is protected
    without each one having to re-validate.
    """
    err = validate(kind, name)
    if err:
        raise ValueError(err)
    if not content or not content.strip():
        raise ValueError("body is required (empty context docs are not useful)")
    if len(content) > MAX_BODY_CHARS:
        raise ValueError(f"body too long ({len(content)} chars, max {MAX_BODY_CHARS})")
    lp = _local_path(kind, name)
    lp.parent.mkdir(parents=True, exist_ok=True)
    try:
        lp.write_text(content, encoding="utf-8")
    except Exception:
        logger.exception("context write failed locally: %s", lp)
        return False
    # Fire the kDrive mirror inline — write volume is tiny (< 1/min
    # expected) and we want the remote copy in sync immediately so a
    # crash between local-write and next-flush doesn't lose edits.
    if kdrive.enabled:
        await kdrive.write_text(_remote_path(kind, name), content)
    _invalidate_list_cache()
    return True


async def read(kind: str, name: str) -> str | None:
    """Local first, kDrive fallback. Returns None if missing everywhere."""
    err = validate(kind, name)
    if err:
        raise ValueError(err)
    lp = _local_path(kind, name)
    if lp.exists():
        try:
            return lp.read_text(encoding="utf-8")
        except Exception:
            logger.exception("context read failed locally: %s", lp)
    if kdrive.enabled:
        remote = await kdrive.read_text(_remote_path(kind, name))
        if remote is not None:
            # Populate the local cache so next read is fast.
            try:
                lp.parent.mkdir(parents=True, exist_ok=True)
                lp.write_text(remote, encoding="utf-8")
            except Exception:
                logger.exception("context cache write failed: %s", lp)
            return remote
    return None


async def delete(kind: str, name: str) -> bool:
    err = validate(kind, name)
    if err:
        raise ValueError(err)
    lp = _local_path(kind, name)
    try:
        if lp.exists():
            lp.unlink()
    except Exception:
        logger.exception("context delete failed locally: %s", lp)
    if kdrive.enabled:
        await kdrive.remove(_remote_path(kind, name))
    _invalidate_list_cache()
    return True


def _invalidate_list_cache() -> None:
    """Clear the list_all() TTL cache. Called after every successful
    write/delete from this process. External kDrive edits still have
    to wait out the TTL."""
    global _list_cache, _list_cache_at
    _list_cache = None
    _list_cache_at = 0.0


def _clean_basename(entry: str) -> str:
    """Normalize a kDrive ls() entry to a bare filename.

    webdav4's ls() returns basenames already in most cases, but some
    servers include trailing slashes for directories or whole paths —
    so strip both to be safe.
    """
    s = str(entry).rstrip("/")
    return PurePosixPath(s).name


async def list_all() -> dict[str, list[str]]:
    """Enumerate every context doc currently available (local union kDrive).

    Shape: {"root": ["CLAUDE"] or [], "skills": [...], "rules": [...]}.
    Names are returned without the .md suffix.

    Result cached for _LIST_TTL_SECONDS so build_system_prompt_suffix()
    (called once per agent spawn) doesn't repeat three WebDAV round
    trips per turn. Cache is busted by any write/delete in this process.
    """
    global _list_cache, _list_cache_at
    now = time.monotonic()
    if _list_cache is not None and (now - _list_cache_at) < _LIST_TTL_SECONDS:
        # Return a defensive copy so callers can't mutate the cached value.
        return {k: list(v) for k, v in _list_cache.items()}

    out: dict[str, list[str]] = {"root": [], "skills": [], "rules": []}
    # Local pass
    root = CONTEXT_DIR / "CLAUDE.md"
    if root.exists():
        out["root"] = ["CLAUDE"]
    for kind in ("skills", "rules"):
        d = CONTEXT_DIR / kind
        if d.exists():
            for f in d.iterdir():
                if f.is_file() and f.suffix == ".md":
                    out[kind].append(f.stem)
    # kDrive pass — union in anything we don't have locally yet.
    # Normalize basenames so dir entries with trailing slashes
    # ("skills/", "rules/") don't accidentally match "CLAUDE.md".
    if kdrive.enabled:
        try:
            root_entries = await kdrive.list_dir("context")
            root_names = {_clean_basename(e) for e in root_entries}
            if "CLAUDE.md" in root_names and "CLAUDE" not in out["root"]:
                out["root"].append("CLAUDE")
        except Exception:
            logger.exception("context kdrive root list failed")
        for kind in ("skills", "rules"):
            try:
                entries = await kdrive.list_dir(f"context/{kind}")
            except Exception:
                logger.exception("context kdrive list failed: %s", kind)
                continue
            for raw in entries:
                n = _clean_basename(raw)
                if n.endswith(".md") and len(n) > 3:
                    stem = n[:-3]
                    if stem not in out[kind]:
                        out[kind].append(stem)
    for k in out:
        out[k].sort()
    _list_cache = out
    _list_cache_at = now
    return {k: list(v) for k, v in out.items()}


async def build_system_prompt_suffix() -> str:
    """Concatenate every context doc into a block suitable for appending
    to an agent's system prompt. Called on every turn spawn so changes
    propagate without any agent restart.

    Returns an empty string when no context is configured — this is the
    harness's pre-§5.2 behavior, so agents keep working unchanged.
    """
    parts: list[str] = []
    listing = await list_all()
    # CLAUDE.md first — top-level harness direction.
    if "CLAUDE" in listing["root"]:
        body = await read("root", "CLAUDE")
        if body:
            parts.append("## Project conventions (CLAUDE.md)\n\n" + body.strip())
    # Then rules (hard) so agents read them before skills (soft).
    for name in listing["rules"]:
        body = await read("rules", name)
        if body:
            parts.append(f"## Rule — {name}\n\n" + body.strip())
    for name in listing["skills"]:
        body = await read("skills", name)
        if body:
            parts.append(f"## Skill — {name}\n\n" + body.strip())
    if not parts:
        return ""
    return "\n\n" + "\n\n---\n\n".join(parts)

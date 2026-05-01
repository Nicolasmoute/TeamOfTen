"""Adapter from the project's `truth/` folder to Compass `TruthFact`s.

Compass does not own a separate truth list — the project already has a
canonical, human-managed `truth/` directory under
`/data/projects/<id>/truth/`. The harness's existing flow:

  - Humans edit truth files via the Files pane.
  - Coach proposes new / updated truth via `coord_propose_truth_update`,
    which queues a row in `truth_proposals` for human approval.
  - Agents are blocked from writing under `truth/` by a PreToolUse hook
    in `server/agents.py`.

Compass reads truth fresh on every run (no caching) — when truth changes
on disk between runs, the next run picks it up automatically. Each
`.md` / `.txt` file becomes one `TruthFact`; the synthesized `index` is
1-based and stable for the duration of a single read (sorted by path),
so the LLM's `truth_index` reply can be mapped back to a file path.

Other formats accepted by `truth/` (yaml, json, toml, csv per
`server/truth.py`) are NOT exposed as truth facts — they're reference
documents, not statements the LLM should be checking answers against.
The dashboard's Files pane is the right place to view them.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from server.paths import project_paths

logger = logging.getLogger("harness.compass.truth")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Cap on per-file content to keep prompts compact. A truth file longer
# than this is almost certainly a reference doc (spec, brand guide,
# contract) rather than a single short fact — pass the head only.
MAX_FACT_CHARS = 8000

# Allowed extensions (subset of truth.py's accepted formats — the rest
# are structured docs, not truth-check candidates).
ALLOWED_SUFFIXES = {".md", ".markdown", ".txt"}


def _read_text_safe(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        logger.exception("compass.truth: read failed: %s", p)
        return None


def _mtime_iso(p: Path) -> str:
    try:
        ts = p.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except OSError:
        return ""


def read_truth_facts(project_id: str):
    """Walk `<project>/truth/` and return a list of `TruthFact` instances,
    one per allowed file, ordered by relative path.

    The import is local so this module can be imported without pulling
    `compass.store` into a top-level import cycle.
    """
    from server.compass.store import TruthFact  # noqa: PLC0415

    pp = project_paths(project_id)
    root = pp.truth
    if not root.exists() or not root.is_dir():
        return []

    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in ALLOWED_SUFFIXES:
            files.append(p)
    files.sort(key=lambda x: str(x.relative_to(root)).replace("\\", "/"))

    out = []
    for i, p in enumerate(files, start=1):
        body = _read_text_safe(p)
        if body is None:
            continue
        body = body.strip()
        if not body:
            continue
        relpath = str(p.relative_to(root)).replace("\\", "/")
        # Prefix with the file's relpath so the LLM has a name handle —
        # it makes the reasoning surface ("conflicts with brand-tone.md")
        # but the LLM still answers with the integer truth_index.
        text = body
        if len(text) > MAX_FACT_CHARS:
            text = text[:MAX_FACT_CHARS] + f"\n\n[truncated — file is {len(body)} chars total]"
        out.append(TruthFact(
            index=i,
            text=f"({relpath}) {text}",
            added_at=_mtime_iso(p),
            added_by="human",
        ))
    return out


def read_truth_index_to_path(project_id: str) -> dict[int, str]:
    """Return the same 1-based index → path mapping `read_truth_facts`
    uses internally. Useful for the truth-conflict modal so the human
    can be pointed at the right file when amending."""
    pp = project_paths(project_id)
    root = pp.truth
    if not root.exists() or not root.is_dir():
        return {}
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() in ALLOWED_SUFFIXES:
            files.append(p)
    files.sort(key=lambda x: str(x.relative_to(root)).replace("\\", "/"))
    return {
        i: str(p.relative_to(root)).replace("\\", "/")
        for i, p in enumerate(files, start=1)
    }


__all__ = ["read_truth_facts", "read_truth_index_to_path", "MAX_FACT_CHARS", "ALLOWED_SUFFIXES"]

"""Adapter from the project's `truth/` folder to Compass `TruthFact`s.

Compass does not own a separate truth list — the project already has a
canonical, human-managed `truth/` directory under
`/data/projects/<id>/truth/`. The harness's existing flow:

  - Humans edit truth files via the Files pane.
  - Coach proposes new / updated truth via
    `coord_propose_file_write(scope='truth', ...)`, which queues a row
    in `file_write_proposals` for human approval.
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


def _collect_truth_files(project_id: str) -> list[tuple[Path, str]]:
    """Walk every truth-corpus source for the project and return a
    sorted list of `(path, project_relative_relpath)` pairs.

    Two sources combined into one corpus:
    1. `<project>/truth/**/*.{md,markdown,txt}` — the dedicated truth
       lane (specs, brand guidelines, contracts, role docs, etc.).
    2. `<project>/project-objectives.md` — the human's authored
       objectives file (sits at project root, surfaced in the EnvPane).

    Both are human-authored / Coach-proposed-then-human-approved, both
    drive the lattice the same way, and both should anchor truth-check
    contradictions. Treating them uniformly avoids two parallel "what
    the human believes" worlds.

    Relative paths are PROJECT-ROOT relative, not truth-root relative —
    so a file under `truth/` shows as `truth/specs.md` and the
    objectives file shows as `project-objectives.md`. The dashboard
    builds links via `/data/projects/<id>/<relpath>` directly, which
    handles both shapes uniformly.
    """
    pp = project_paths(project_id)
    project_root = pp.root
    out: list[tuple[Path, str]] = []

    # 1. <project>/truth/**
    truth_root = pp.truth
    if truth_root.exists() and truth_root.is_dir():
        for p in truth_root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            relpath = str(p.relative_to(project_root)).replace("\\", "/")
            out.append((p, relpath))

    # 2. <project>/project-objectives.md (canonical, single file)
    obj = pp.project_objectives
    if (
        obj.exists()
        and obj.is_file()
        and obj.suffix.lower() in ALLOWED_SUFFIXES
    ):
        relpath = str(obj.relative_to(project_root)).replace("\\", "/")
        out.append((obj, relpath))

    out.sort(key=lambda pair: pair[1])
    return out


def read_truth_facts(project_id: str):
    """Read every truth-corpus file (truth/ + project-objectives.md)
    and return a list of `TruthFact` instances, one per allowed file,
    ordered by project-root-relative path.

    The import is local so this module can be imported without pulling
    `compass.store` into a top-level import cycle.
    """
    from server.compass.store import TruthFact  # noqa: PLC0415

    files = _collect_truth_files(project_id)
    out = []
    for i, (p, relpath) in enumerate(files, start=1):
        body = _read_text_safe(p)
        if body is None:
            continue
        body = body.strip()
        if not body:
            continue
        # Prefix with the file's relpath so the LLM has a name handle —
        # makes the reasoning surface ("conflicts with brand-tone.md")
        # while the LLM still answers with the integer truth_index.
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
    """Return the same 1-based index → project-root-relative path
    mapping `read_truth_facts` uses internally. Used by the
    truth-conflict modal so the human can be pointed at the right
    file when amending. Path shape:
      - `truth/<subpath>/<file>.md` for files under truth/
      - `project-objectives.md` for the objectives file
    Either form composes correctly with `/data/projects/<id>/<path>`.
    """
    files = _collect_truth_files(project_id)
    return {i: relpath for i, (_, relpath) in enumerate(files, start=1)}


__all__ = [
    "read_truth_facts",
    "read_truth_index_to_path",
    "MAX_FACT_CHARS",
    "ALLOWED_SUFFIXES",
]

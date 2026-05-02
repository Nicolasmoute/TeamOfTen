"""Adapter from the project's truth-corpus sources to Compass `TruthFact`s.

Compass does not own a separate truth list — the project already has a
canonical, human-managed body of truth-bearing material spread across
three lanes:

  1. `<project>/truth/**/*.{md,markdown,txt}` — the dedicated truth
     directory under `/data/projects/<id>/truth/`. Humans edit via the
     Files pane; Coach proposes new / updated truth via
     `coord_propose_file_write(scope='truth', ...)`, which queues a
     row in `file_write_proposals` for human approval. Agents are
     blocked from writing under `truth/` by a PreToolUse hook in
     `server/agents.py`. This is the **strongest** lane — fully vetted.
  2. `<project>/project-objectives.md` — the human's authored
     objectives file at the project root. Same authority as truth/.
  3. `/data/wiki/<id>/**/*.{md,markdown,txt}` — the per-project wiki
     tree. Agent-curated knowledge that compounds across sessions
     (gotchas, stakeholder preferences, glossary entries, domain
     rules). Not as strong as truth/ — wiki entries are written by
     agents — but the human keeps a curating role and the corpus
     captures intent / users / UX / context that truth/ rarely covers.
     Folding it in lets Compass anchor the lattice in the same
     material the team consults at runtime.

Compass reads the corpus fresh on every run (no caching) — when
anything changes on disk between runs, the next run picks it up
automatically. Each `.md` / `.txt` file becomes one `TruthFact`; the
synthesized `index` is 1-based and stable for the duration of a single
read (sorted by relpath), so the LLM's `truth_index` reply can be
mapped back to a file path.

Path-shape contract for the dashboard:
  - In-project files (truth/ + project-objectives.md): relpath is
    project-root-relative — e.g. `truth/specs.md`,
    `project-objectives.md`. Dashboard composes
    `/data/projects/<id>/<relpath>`.
  - Wiki files: relpath uses the synthetic `wiki/<sub>/<file>.md`
    prefix (relative to `/data/wiki/<id>/`). Dashboard branches on
    the `wiki/` prefix and composes `/data/wiki/<id>/<rest>`.

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

from server.paths import global_paths, project_paths

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
    sorted list of `(path, display_relpath)` pairs.

    Three sources combined into one corpus:
    1. `<project>/truth/**/*.{md,markdown,txt}` — the dedicated truth
       lane (specs, brand guidelines, contracts, role docs, etc.).
       relpath: `truth/<sub>/<file>.md`.
    2. `<project>/project-objectives.md` — the human's authored
       objectives file (sits at project root, surfaced in the EnvPane).
       relpath: `project-objectives.md`.
    3. `/data/wiki/<project_id>/**/*.{md,markdown,txt}` — the
       per-project wiki tree. Agent-curated knowledge that compounds
       across sessions and captures intent / users / UX / context
       material that the dedicated truth lane rarely covers.
       Synthetic relpath: `wiki/<sub>/<file>.md` (NOT under the project
       root on disk — the prefix is a label that the dashboard branches
       on to compose links to `/data/wiki/<id>/<rest>`).

    All three drive the lattice the same way, and all three anchor
    truth-check contradictions. Treating them uniformly avoids parallel
    "what the team believes" worlds. Wiki entries lean less vetted
    than truth/ — agents wrote them — but the human's curating role
    keeps them within the trust envelope, and the alternative (loose
    side-channel that doesn't drive lattice updates) is strictly worse
    for keeping the lattice grounded in the project's working memory.

    Sorted by relpath so ordering is deterministic across calls;
    the synthetic `wiki/...` prefix sorts after `project-objectives.md`
    and after `truth/...` paths.
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

    # 3. /data/wiki/<project_id>/** — synthetic `wiki/...` prefix.
    #    Wiki INDEX.md (the auto-maintained catalog at the global wiki
    #    root) is NOT in the per-project tree, so we don't need to
    #    filter for it here. Per-project wiki only.
    wiki_root = global_paths().wiki / project_id
    if wiki_root.exists() and wiki_root.is_dir():
        for p in wiki_root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            sub = str(p.relative_to(wiki_root)).replace("\\", "/")
            out.append((p, f"wiki/{sub}"))

    out.sort(key=lambda pair: pair[1])
    return out


def read_truth_facts(project_id: str):
    """Read every truth-corpus file (truth/, project-objectives.md, and
    the per-project wiki tree) and return a list of `TruthFact`
    instances, one per allowed file, ordered by display relpath.

    Wiki files are folded in alongside `truth/` and `project-objectives.md`.
    The dashboard distinguishes them by the `wiki/` relpath prefix; the
    LLM treats them all as truth-corpus material with the relpath visible
    so it can attribute reasoning to a specific source.

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
        # makes the reasoning surface ("conflicts with brand-tone.md"
        # or "per wiki/customer-personas.md") while the LLM still
        # answers with the integer truth_index.
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
    """Return the same 1-based index → display relpath mapping that
    `read_truth_facts` uses internally. Used by the truth-conflict
    modal and the reconciliation card so the human can be pointed at
    the right file when amending. Path shapes:
      - `truth/<subpath>/<file>.md` for files under truth/
      - `project-objectives.md` for the objectives file
      - `wiki/<subpath>/<file>.md` for wiki entries (NOT under the
        project root on disk — dashboard handles the prefix specially)
    """
    files = _collect_truth_files(project_id)
    return {i: relpath for i, (_, relpath) in enumerate(files, start=1)}


__all__ = [
    "read_truth_facts",
    "read_truth_index_to_path",
    "MAX_FACT_CHARS",
    "ALLOWED_SUFFIXES",
]

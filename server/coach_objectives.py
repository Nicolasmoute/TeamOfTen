"""Project objectives — the project's north star, what "good" looks
like (`recurrence-specs.md` §3.3).

Free-form markdown at ``/data/projects/<slug>/project-objectives.md``.
No mandated sections — the operator describes goals however they like.
The harness mirrors writes to kDrive but Coach edits the file via the
standard Write tool (no MCP wrapper needed; see spec §7.5).

This module provides:

  * :func:`read_objectives` — synchronous read.
  * :func:`has_objectives` — "is there anything to inject?".
  * :func:`objectives_block` — formatted section for Coach's system
    prompt. Returns ``""`` when the file is missing or empty so the
    section is omitted entirely (per spec §6).
  * :data:`OBJECTIVES_ELICITATION_PROMPT` — the wording phase 5's
    smart tick uses when inbox + todos are empty AND objectives are
    missing.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from server.paths import project_paths

logger = logging.getLogger("harness.coach_objectives")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s | %(message)s"
    ))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Sanity ceiling — same shape as CLAUDE.md: long objectives are
# legitimate but a runaway file shouldn't dominate every Coach turn.
_MAX_OBJECTIVES_CHARS = 50_000


# Verbatim from spec §14 ("Bootstrapping a new project"). Phase 5's
# smart-tick prompt composer falls back to this when the inbox is
# empty AND coach-todos.md has nothing AND the objectives file is
# missing/empty.
OBJECTIVES_ELICITATION_PROMPT = (
    "This project has no objectives defined. What are we trying to "
    "accomplish? Once you reply, I'll save them to "
    "project-objectives.md."
)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError:
        logger.exception("objectives: read failed for %s", path)
        return ""


def read_objectives(project_id: str) -> str:
    """Return the raw markdown body, stripped. ``""`` when missing."""
    pp = project_paths(project_id)
    body = _read_text(pp.project_objectives).strip()
    if len(body) > _MAX_OBJECTIVES_CHARS:
        logger.warning(
            "objectives: %s exceeds %d chars; truncating",
            pp.project_objectives, _MAX_OBJECTIVES_CHARS,
        )
        body = body[:_MAX_OBJECTIVES_CHARS] + "\n\n…[truncated]"
    return body


def has_objectives(project_id: str) -> bool:
    return bool(read_objectives(project_id))


def objectives_block(project_id: str) -> str:
    """Render the ``## Project objectives`` section for Coach's
    system prompt. Returns ``""`` when the file is missing or empty
    so the section header is omitted (spec §6: "no 'None this
    session' placeholder")."""
    body = read_objectives(project_id)
    if not body:
        return ""
    return "## Project objectives\n\n" + body

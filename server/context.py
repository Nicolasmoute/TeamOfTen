"""System-prompt suffix — reads the global + per-project CLAUDE.md.

Pre-refactor this module owned a `/data/context/` store with three
buckets (root/skills/rules) and a write API. The projects refactor
(PROJECTS_SPEC.md §8) replaced that with:

  - `/data/CLAUDE.md`              — global rules, written by Phase 6
                                     bootstrap + edited by Coach via
                                     the standard Write tool.
  - `/data/projects/<active>/CLAUDE.md`
                                   — per-project rules, written by
                                     Phase 7 stub on project creation
                                     + edited by Coach.
  - `/data/.claude/skills/`        — Claude Code skills, loaded by the
                                     SDK directly (we don't inject).

This module's only remaining job is to concatenate the two CLAUDE.md
files into the per-turn system-prompt suffix so any edit takes effect
on every agent's next turn without a restart.
"""

from __future__ import annotations

import logging
import sys

from server.db import resolve_active_project
from server.paths import global_paths, project_paths

logger = logging.getLogger("harness.context")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Sanity ceiling — a CLAUDE.md over 200 KB would dominate every turn's
# system prompt. Keep the read but truncate with a warning so a
# runaway doc doesn't silently bloat costs.
_MAX_CLAUDE_MD_CHARS = 200_000


def _read_text_safe(path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) > _MAX_CLAUDE_MD_CHARS:
        logger.warning(
            "CLAUDE.md at %s exceeds %d chars; truncating",
            path, _MAX_CLAUDE_MD_CHARS,
        )
        text = text[:_MAX_CLAUDE_MD_CHARS] + "\n\n…[truncated]"
    return text.strip()


async def build_system_prompt_suffix() -> str:
    """Concatenate global + per-project CLAUDE.md into a system-prompt
    suffix. Re-read on every turn so edits take effect without a
    restart. Returns "" when both files are empty / missing.

    Layout matches PROJECTS_SPEC.md §10:
      [identity]            ← prepended in agents.py
      [coordination block]  ← prepended in agents.py (Coach only)
      [role prompt]         ← role baseline in agents.py
      [global CLAUDE.md]    ← from this function
      [project CLAUDE.md]   ← from this function (active project)
    """
    parts: list[str] = []

    gp = global_paths()
    global_body = _read_text_safe(gp.claude_md)
    if global_body:
        parts.append("## Global rules (CLAUDE.md)\n\n" + global_body)

    try:
        active = await resolve_active_project()
    except Exception:
        active = None
    if active:
        try:
            pp = project_paths(active)
            proj_body = _read_text_safe(pp.claude_md)
        except Exception:
            proj_body = ""
        if proj_body:
            parts.append(
                f"## Project rules ({active}/CLAUDE.md)\n\n" + proj_body
            )

    if not parts:
        return ""
    return "\n\n" + "\n\n---\n\n".join(parts)

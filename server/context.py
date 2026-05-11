"""System-prompt suffix — runtime-aware harness context block.

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

Runtime split (2026-05-10):
  - **Claude runtime** — the Agent SDK auto-loads CLAUDE.md via
    `setting_sources` (default `["user", "project", "local"]`). The
    SDK walks up from `cwd` (the per-Player worktree) and finds both
    `/data/projects/<active>/CLAUDE.md` and `/data/CLAUDE.md`. Manual
    injection of those files here would double-count against the
    token budget (verified 2026-05-10 via sentinel test, 2/2 hits per
    turn). So for Claude we skip the CLAUDE.md reads — only the
    Coach-only playbook block (which the SDK does NOT auto-load) goes
    through this function.
  - **Codex runtime** — no `setting_sources` equivalent. We must
    manually inject CLAUDE.md or Codex Players have no project
    context. Same shape as before for Codex.
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

# Cache of `_read_text_safe` results, keyed by (resolved-path-string,
# mtime_ns, size). On Codex turns this function is called twice per
# turn × 11 agents; caching cuts the per-turn cost to a single stat()
# when nothing has changed. The dict is small (only CLAUDE.md global +
# per-project) and never needs eviction at expected scale, but cap at
# 64 entries defensively in case a misuse calls with many paths.
_CACHE: dict[tuple[str, int, int], str] = {}
_CACHE_MAX = 64


def _read_text_safe(path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
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
    result = text.strip()
    # Cap-evict only when adding a NEW key — never drop an entry just
    # to overwrite the same key (the working set is normally 2 files,
    # so this almost never fires; the guard matters only on pathological
    # cache churn).
    if key not in _CACHE and len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)), None)
    _CACHE[key] = result
    return result


async def build_system_prompt_suffix(agent_id: str, runtime: str = "codex") -> str:
    """Compose the runtime-aware harness context suffix.

    For Claude turns the SDK auto-loads CLAUDE.md via `setting_sources`,
    so this function returns ONLY the Coach-only playbook block (the
    SDK has no equivalent for that). For Codex turns the SDK does not
    auto-load CLAUDE.md, so this function manually injects the global
    and per-project CLAUDE.md plus the Coach-only playbook.

    The default runtime is `"codex"` (the conservative full-injection
    path) so any future caller that forgets to pass the arg gets the
    safer over-injection rather than silently dropping context.

    Re-read on every turn so edits take effect without a restart.
    Returns "" when nothing applies.

    Layout (Codex):
      [identity]            ← prepended in agents.py
      [coordination block]  ← prepended in agents.py (Coach only)
      [role prompt]         ← role baseline in agents.py
      [global CLAUDE.md]    ← from this function (Codex only)
      [project CLAUDE.md]   ← from this function (Codex only)
      [orchestration playbook] ← Coach only, both runtimes

    Layout (Claude):
      [identity]            ← prepended in agents.py
      [coordination block]  ← prepended in agents.py (Coach only)
      [role prompt]         ← role baseline in agents.py
      [orchestration playbook] ← Coach only, this function
      ↳ SDK then appends auto-loaded CLAUDE.md (global + project) via
        `setting_sources` — the harness does not see this content.

    The orchestration playbook is Coach-only context: it is Coach's
    coordination memory (the lattice Coach mutates via
    `coord_propose_playbook_changes` and the daily reflection runner).
    Players don't need it — coordination discipline flows to them
    through the wake prompts Coach composes per stage.
    """
    parts: list[str] = []

    # Codex has no setting_sources auto-load — manually inject CLAUDE.md
    # to give Codex Players the same project context Claude Players get
    # via the SDK. Skip for Claude to avoid double-counting (verified
    # via sentinel test 2026-05-10).
    if runtime != "claude":
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

    # Playbook — Coach's coordination-strategy lattice. Injected only
    # when the caller is Coach; Players don't see it. Sync render
    # returns a self-contained markdown block (already includes the
    # `## Orchestration playbook` heading) or empty string when the
    # lattice is empty / disabled / file missing. Sync I/O is
    # acceptable from this async caller — same pattern as
    # `_read_text_safe` above. See Docs/playbook-specs.md §6.
    if agent_id == "coach":
        try:
            from server.playbook.render import render_playbook_block  # noqa: PLC0415

            playbook_body = render_playbook_block()
            if playbook_body:
                parts.append(playbook_body)
        except Exception:
            # Render failure is non-fatal — the playbook is read-only
            # context for the agent. Log + continue without it.
            logger.exception("playbook render failed (continuing without)")

    if not parts:
        return ""
    return "\n\n" + "\n\n---\n\n".join(parts)

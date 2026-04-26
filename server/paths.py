"""Filesystem layout helpers for the projects refactor (PROJECTS_SPEC.md §4).

Two trees per project — `projects/<slug>/` for project-private working
data and `wiki/<slug>/` for the per-project sub-tree of the global wiki —
plus a small global tier (CLAUDE.md, skills/, mcp/, wiki/INDEX.md).

Spec said these helpers would live in server/main.py; they're a
standalone module here so they're testable in isolation and don't
bloat main.py further. Importers should treat the two functions as
the single source of truth for paths — never hardcode `/data/...`
strings at call sites.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Mirrors HARNESS_DB_PATH's convention in server/db.py: /data is the
# Zeabur volume mount point. Override per-deploy via HARNESS_DATA_ROOT.
DATA_ROOT = Path(os.environ.get("HARNESS_DATA_ROOT", "/data"))


@dataclass(frozen=True)
class GlobalPaths:
    root: Path
    claude_md: Path
    skills: Path
    mcp: Path
    wiki: Path
    wiki_index: Path


@dataclass(frozen=True)
class ProjectPaths:
    """Per-project filesystem layout under /data/projects/<slug>/.

    `worktree(slot)` returns the per-Player worktree path under
    `repo/`; Coach has no worktree (Coach never writes code).
    """

    project_id: str
    root: Path
    claude_md: Path
    memory: Path
    decisions: Path
    knowledge: Path
    working: Path
    working_conversations: Path
    working_handoffs: Path
    working_plans: Path
    working_workspace: Path
    outputs: Path
    inputs: Path
    attachments: Path
    repo: Path
    bare_clone: Path

    def worktree(self, slot: str) -> Path:
        return self.repo / slot


def global_paths() -> GlobalPaths:
    root = DATA_ROOT
    wiki = root / "wiki"
    return GlobalPaths(
        root=root,
        claude_md=root / "CLAUDE.md",
        skills=root / "skills",
        mcp=root / "mcp",
        wiki=wiki,
        wiki_index=wiki / "INDEX.md",
    )


def project_paths(project_id: str) -> ProjectPaths:
    root = DATA_ROOT / "projects" / project_id
    working = root / "working"
    repo = root / "repo"
    return ProjectPaths(
        project_id=project_id,
        root=root,
        claude_md=root / "CLAUDE.md",
        memory=root / "memory",
        decisions=root / "decisions",
        knowledge=root / "knowledge",
        working=working,
        working_conversations=working / "conversations",
        working_handoffs=working / "handoffs",
        working_plans=working / "plans",
        working_workspace=working / "workspace",
        outputs=root / "outputs",
        inputs=root / "inputs",
        attachments=root / "attachments",
        repo=repo,
        bare_clone=repo / ".project",
    )


# Subdirectories created by ensure_project_scaffold(). Matches the
# §4 layout — wiki sub-folder is created via global_paths().wiki / id
# rather than belonging to ProjectPaths because the wiki tree lives
# outside projects/ on purpose (cross-project hyperlinks resolve from
# one shared root).
_PROJECT_SUBDIRS = (
    "memory",
    "decisions",
    "knowledge",
    "working/conversations",
    "working/handoffs",
    "working/plans",
    "working/workspace",
    "outputs",
    "inputs",
    "attachments",
    "repo",
)


def ensure_project_scaffold(project_id: str) -> ProjectPaths:
    """Create the per-project folder tree on first use.

    Idempotent — re-running on an existing project tree is a no-op.
    Does NOT write CLAUDE.md (Phase 6/7 owns the template); does NOT
    clone the repo (Phase 11 / activation does that).
    """
    pp = project_paths(project_id)
    pp.root.mkdir(parents=True, exist_ok=True)
    for sub in _PROJECT_SUBDIRS:
        (pp.root / sub).mkdir(parents=True, exist_ok=True)
    # Wiki sub-folder lives in the global wiki tree.
    (global_paths().wiki / project_id).mkdir(parents=True, exist_ok=True)
    return pp


def ensure_global_scaffold() -> GlobalPaths:
    """Create the global folder tree on boot. Idempotent."""
    gp = global_paths()
    for d in (gp.root, gp.skills, gp.mcp, gp.wiki):
        d.mkdir(parents=True, exist_ok=True)
    return gp


# Phase 6 (PROJECTS_SPEC.md §9): wiki + LLM-Wiki skill + global
# CLAUDE.md bootstrap. Status string is exposed via /api/health under
# `wiki`. Cached on the module so subsequent /api/health hits don't
# re-stat the filesystem.
_BOOTSTRAP_STATUS: str = "missing"


def bootstrap_status() -> str:
    """Last result of bootstrap_global_resources() — `"present"` if
    everything was already on disk, `"bootstrapped"` if any file was
    written this boot, `"missing"` if a write failed (perm/disk).

    Note: this is a process-level cache. /api/health re-stats the
    sentinel files anyway and downgrades a cached `"missing"` to
    `"present"` if the files are now on disk; the cache only carries
    the boot-time verb (`bootstrapped` vs `present`) for the UI."""
    return _BOOTSTRAP_STATUS


# Phase 7 (PROJECTS_SPEC.md §8): per-project CLAUDE.md stub. Goal +
# Repo pre-filled from creation modal (or blank placeholders for
# misc / template invocations); Stakeholders / Team / Glossary /
# Conventions left for Coach to fill as the project unfolds.
_PROJECT_CLAUDE_MD_STUB = """# Project: {name}

## Goal
{goal}

## Repo
{repo}

## Stakeholders
<filled in by Coach>

## Team
<filled in by Coach as roles are assigned via coord_set_player_role —
record the intent ("p1 = lead developer, p2 = QA") so future you can
reconstruct why each Player was named what they were named>

## Glossary
<filled in by Coach>

## Conventions
<project-specific rules, code style, terminology, do/don't lists>
"""


def write_project_claude_md_stub(
    project_id: str,
    name: str,
    description: str | None = None,
    repo_url: str | None = None,
) -> bool:
    """Phase 7 (PROJECTS_SPEC.md §8) — write a per-project CLAUDE.md
    stub if absent. First-write-only — preserves Coach edits across
    re-creation paths and re-runs of `init_db`. Returns True if a
    file was written, False if one already existed or the write
    failed.

    Lives in paths.py (not projects_api.py) so both `init_db` (which
    seeds misc) and `create_project` (which scaffolds new projects)
    can call it without a circular import.
    """
    pp = project_paths(project_id)
    if pp.claude_md.exists():
        return False
    goal = (description or "").strip() or "<short description, from creation modal>"
    repo = (repo_url or "").strip() or "<no repo configured>"
    body = _PROJECT_CLAUDE_MD_STUB.format(
        name=name,
        goal=goal,
        repo=repo,
    )
    try:
        pp.root.mkdir(parents=True, exist_ok=True)
        pp.claude_md.write_text(body, encoding="utf-8")
    except OSError:
        return False
    return True


def reset_bootstrap_status() -> None:
    """Reset the module-level bootstrap status cache. Used by test
    fixtures so a test that reads `bootstrap_status()` without first
    calling `bootstrap_global_resources()` doesn't see leftover state
    from a previous test in the same process."""
    global _BOOTSTRAP_STATUS
    _BOOTSTRAP_STATUS = "missing"


# Phase 7 (PROJECTS_SPEC.md §14 Resolved: INDEX.md maintenance):
# the wiki INDEX.md is auto-rebuilt on every wiki write event. The
# rebuild scans /data/wiki/ for *.md files (excluding INDEX.md
# itself), groups them into "Cross-project entries" (root-level)
# and "Per-project entries" (one sub-section per project sub-tree),
# and rewrites the file atomically.
_WIKI_INDEX_HEADER = """# Wiki Index

_Auto-maintained by the harness on every wiki write event (the v1
implementation choice — see PROJECTS_SPEC.md §14 Resolved: INDEX.md
maintenance). Agents do not edit this file directly._

"""


def _read_wiki_entry_title(path: Path) -> str:
    """Best-effort title extraction:
      - YAML frontmatter `title:` field if present.
      - Else first `# heading` line.
      - Else the file stem.
    Falls back to the stem on any read error so a corrupted entry
    doesn't break the whole index rebuild.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return path.stem
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            for line in text[4:end].splitlines():
                if line.startswith("title:"):
                    t = line[len("title:"):].strip().strip('"').strip("'")
                    if t:
                        return t
    for line in text.splitlines()[:50]:
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip() or path.stem
    return path.stem


def update_wiki_index() -> bool:
    """Rebuild `/data/wiki/INDEX.md` from the current wiki tree.

    Scans:
      - `/data/wiki/*.md` (excluding INDEX.md) → "Cross-project entries".
      - `/data/wiki/<slug>/*.md` for each sub-folder → "Per-project entries"
        with one sub-section per project (slug as header).

    Returns True if the file was rewritten, False if the wiki tree
    is missing or write failed. First-time-only behavior is *not*
    applied — INDEX.md is overwritten every call. The first paragraph
    (which says "Auto-maintained by the harness") makes that explicit.
    Atomic via tempfile + replace so concurrent reads don't see a
    half-written file.
    """
    gp = global_paths()
    if not gp.wiki.is_dir():
        return False

    cross_project: list[Path] = []
    per_project: dict[str, list[Path]] = {}
    try:
        for entry in sorted(gp.wiki.iterdir()):
            if entry.is_file():
                if entry.suffix == ".md" and entry.name != "INDEX.md":
                    cross_project.append(entry)
            elif entry.is_dir():
                slug = entry.name
                files = sorted(
                    p for p in entry.glob("*.md") if p.is_file()
                )
                if files:
                    per_project[slug] = files
    except OSError:
        return False

    lines: list[str] = [_WIKI_INDEX_HEADER.rstrip(), "", "## Cross-project entries", ""]
    if cross_project:
        for p in cross_project:
            title = _read_wiki_entry_title(p)
            lines.append(f"- [{title}]({p.name})")
    else:
        lines.append("_(none yet)_")
    lines.append("")
    lines.append("## Per-project entries")
    lines.append("")
    if per_project:
        for slug in sorted(per_project.keys()):
            lines.append(f"### {slug}")
            lines.append("")
            for p in per_project[slug]:
                title = _read_wiki_entry_title(p)
                lines.append(f"- [{title}]({slug}/{p.name})")
            lines.append("")
    else:
        lines.append("_(none yet)_")
        lines.append("")

    body = "\n".join(lines).rstrip() + "\n"

    # Atomic write — tempfile in the same dir + os.replace so a
    # concurrent reader sees either the old or new file but never
    # a half-written one.
    import tempfile

    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".INDEX.md.", dir=str(gp.wiki)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        os.replace(tmp_path, gp.wiki_index)
    except OSError:
        return False
    return True


# Wiki INDEX.md stub — minimal markdown that the auto-maintain
# logic (Phase 7) can extend later. Wraps the spec §9 step-2 example.
_WIKI_INDEX_STUB = """# Wiki Index

_Auto-maintained by the harness on every wiki write event (the v1
implementation choice — see PROJECTS_SPEC.md §14 Resolved: INDEX.md
maintenance). Agents do not edit this file directly._

## Cross-project entries

## Per-project entries
"""


def _templates_dir() -> Path:
    """Path to checked-in `server/templates/` next to paths.py."""
    return Path(__file__).resolve().parent / "templates"


def _read_template(name: str) -> str:
    """Load a checked-in template by filename. Returns the contents,
    or an empty string if the template is missing — letting the
    bootstrap surface `missing` for the dependent file rather than
    crashing the harness boot."""
    p = _templates_dir() / name
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def bootstrap_global_resources() -> str:
    """Phase 6 boot sequence (PROJECTS_SPEC.md §9 + §8):

      1. Ensure /data/wiki/ exists.
      2. Ensure /data/wiki/INDEX.md exists; write stub if missing.
      3. Ensure /data/skills/llm-wiki/ exists.
      4. Ensure /data/skills/llm-wiki/SKILL.md; copy from
         server/templates/llm_wiki_skill.md if missing.
      5. Ensure /data/CLAUDE.md; copy from
         server/templates/global_claude_md.md if missing.

    First-write-only — once a file exists the harness leaves it
    alone (users / Coach can edit and we don't want boot to revert).

    Returns the status string (also cached in `bootstrap_status()`):
      - `"present"` — every file was already on disk.
      - `"bootstrapped"` — at least one file was written this boot.
      - `"missing"` — a write failed (permissions, disk full, missing
        template); agents can't record knowledge until resolved.
    """
    global _BOOTSTRAP_STATUS

    gp = ensure_global_scaffold()
    skill_dir = gp.skills / "llm-wiki"

    wrote_anything = False
    failed = False

    # Step 1 — /data/wiki/ — already covered by ensure_global_scaffold.

    # Step 2 — INDEX.md
    if not gp.wiki_index.exists():
        try:
            gp.wiki_index.write_text(_WIKI_INDEX_STUB, encoding="utf-8")
            wrote_anything = True
        except OSError:
            failed = True

    # Step 3 — /data/skills/llm-wiki/
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        failed = True

    # Step 4 — SKILL.md
    skill_md = skill_dir / "SKILL.md"
    if not failed and not skill_md.exists():
        body = _read_template("llm_wiki_skill.md")
        if not body:
            failed = True
        else:
            try:
                skill_md.write_text(body, encoding="utf-8")
                wrote_anything = True
            except OSError:
                failed = True

    # Step 5 — /data/CLAUDE.md
    if not failed and not gp.claude_md.exists():
        body = _read_template("global_claude_md.md")
        if not body:
            failed = True
        else:
            try:
                gp.claude_md.write_text(body, encoding="utf-8")
                wrote_anything = True
            except OSError:
                failed = True

    if failed:
        _BOOTSTRAP_STATUS = "missing"
    elif wrote_anything:
        _BOOTSTRAP_STATUS = "bootstrapped"
    else:
        _BOOTSTRAP_STATUS = "present"

    return _BOOTSTRAP_STATUS

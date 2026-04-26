"""Phase post-Phase-8 layout migration — PROJECTS_SPEC.md §4.

Phase 1 (`projects_v1`) shipped the project-scoping backbone but left
several writers still hitting flat `/data/<legacy>/` paths from the
pre-refactor era — `/data/handoffs/`, `/data/uploads/`, `/data/context/`,
`/data/knowledge/`, plus orphaned typo dirs. This migration cleans the
root and brings the on-disk layout in line with what the spec says
should exist.

Steps (all idempotent on retry; status stamped only after every step):

1. Wipe legacy flat dirs at the data root that are no longer written
   by any code path:
     - `handoffs/`, `uploads/`, `context/`, `knowledge/`, `memory/`,
       `decisions/`, `outputs/`, `attachments/` (all moved per-project).
     - `output/`, `upload/`, `uplods/` (typo orphans observed in prod).
   Skips anything that doesn't exist; logs each removal.

2. Move `/data/skills/` → `/data/.claude/skills/` to match Claude
   Code's canonical project layout. The dotted parent dir lets us
   group future CC-managed bits (`settings.json`, `agents/`,
   `commands/`) cleanly. If `/data/.claude/skills/` already exists
   (re-run, or migration ran in a prior boot), merge contents and
   delete the old `/data/skills/`.

3. For every project under `/data/projects/<slug>/`:
     - rename `inputs/` → `uploads/` if `inputs/` exists.
     - move `knowledge/` → `working/knowledge/` (knowledge is working
       material that evolves, not a final deliverable — sits next to
       conversations/handoffs/plans/workspace).
     - move `memory/` → `working/memory/` (memory is the shared
       mutable scratchpad — same "in-progress state" tier as the
       rest of working/).
   Renames are atomic on POSIX; skip when the destination already
   exists (treat as already migrated).

4. Stamp `team_config.schema_version = 'projects_v2'`.

Wired from `server.db.init_db()` AFTER `projects_v1` runs (so the
project tree exists to enumerate) and AFTER the post-migration index
loop (so we don't disturb the schema work).
"""

from __future__ import annotations

import logging
import shutil
import sys

import aiosqlite

from server import paths

logger = logging.getLogger("harness.migrations.projects_v2")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


SCHEMA_VERSION = "projects_v2"

# Legacy flat dirs at the data root to wipe. Every entry was either:
#   - moved per-project in projects_v1 (memory, decisions, outputs,
#     attachments, knowledge, uploads, handoffs, context),
#   - typo orphans never used (output, upload, uplods).
# `events/` was already cleaned by projects_v1 so it isn't listed.
_LEGACY_ROOT_DIRS = (
    "handoffs",
    "context",
    "knowledge",
    "uploads",
    "memory",
    "decisions",
    "outputs",
    "attachments",
    # Typo orphans:
    "output",
    "upload",
    "uplods",
)


async def _schema_version(db: aiosqlite.Connection) -> str | None:
    cur = await db.execute(
        "SELECT value FROM team_config WHERE key = 'schema_version'"
    )
    row = await cur.fetchone()
    if not row:
        return None
    try:
        return row[0]
    except Exception:
        return None


def _wipe_legacy_root_dirs() -> None:
    """Step 1 — delete the flat legacy dirs at /data root."""
    for sub in _LEGACY_ROOT_DIRS:
        p = paths.DATA_ROOT / sub
        try:
            if p.exists():
                logger.warning("projects_v2: wiping legacy root dir %s", p)
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            logger.exception("projects_v2: wipe failed for %s", p)


def _move_skills_under_claude() -> None:
    """Step 2 — relocate /data/skills/ → /data/.claude/skills/.

    Three on-disk shapes to handle:
      a. Only /data/skills/ exists (legacy) → move it.
      b. Only /data/.claude/skills/ exists (already migrated) → no-op.
      c. Both exist (manual / partial run) → merge old into new file
         by file, then drop old. We don't try to resolve content
         conflicts; the new location wins.
    """
    legacy = paths.DATA_ROOT / "skills"
    new_root = paths.DATA_ROOT / ".claude"
    target = new_root / "skills"

    if not legacy.exists():
        # Nothing to move; ensure new path exists for downstream code.
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("projects_v2: mkdir %s failed", target)
        return

    try:
        new_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("projects_v2: mkdir %s failed", new_root)
        return

    if not target.exists():
        # Clean rename — fastest path.
        try:
            legacy.rename(target)
            logger.warning(
                "projects_v2: moved %s -> %s", legacy, target
            )
        except Exception:
            # Fall back to copy + remove if rename fails (cross-fs etc).
            logger.exception(
                "projects_v2: rename %s -> %s failed; copying", legacy, target
            )
            try:
                shutil.copytree(legacy, target, dirs_exist_ok=True)
                shutil.rmtree(legacy, ignore_errors=True)
            except Exception:
                logger.exception(
                    "projects_v2: copy %s -> %s failed; legacy left in place",
                    legacy, target,
                )
        return

    # Both exist — merge.
    logger.warning(
        "projects_v2: %s and %s both exist; merging legacy into new",
        legacy, target,
    )
    try:
        for entry in legacy.iterdir():
            dst = target / entry.name
            if dst.exists():
                # Conflict — new location wins. Log so users notice.
                logger.warning(
                    "projects_v2: skipping %s (already exists at %s)",
                    entry, dst,
                )
                continue
            entry.rename(dst)
        shutil.rmtree(legacy, ignore_errors=True)
    except Exception:
        logger.exception(
            "projects_v2: merge of %s into %s failed", legacy, target
        )


def _rename_inputs_to_uploads() -> None:
    """Step 3a — for each project, rename inputs/ -> uploads/.

    Skips projects where uploads/ already exists (already migrated)
    and projects without an inputs/ dir (created post-v2)."""
    projects_root = paths.DATA_ROOT / "projects"
    if not projects_root.is_dir():
        return
    for proj_dir in projects_root.iterdir():
        if not proj_dir.is_dir():
            continue
        old = proj_dir / "inputs"
        new = proj_dir / "uploads"
        if not old.exists():
            continue
        if new.exists():
            # Both exist — surface so the user can resolve manually.
            logger.warning(
                "projects_v2: %s exists alongside %s; leaving both in place",
                old, new,
            )
            continue
        try:
            old.rename(new)
            logger.warning(
                "projects_v2: renamed %s -> %s", old, new
            )
        except Exception:
            logger.exception(
                "projects_v2: rename %s -> %s failed", old, new
            )


def _move_subdir_into_working(subdir: str) -> None:
    """Step 3b/3c helper — for each project, move <subdir>/ ->
    working/<subdir>/. Used to relocate `knowledge` and `memory` —
    both are mutable in-progress state and sit alongside the other
    working/* lanes (conversations, handoffs, plans, workspace).

    Skips projects where the destination already exists or where the
    source doesn't exist (treats as already migrated).
    """
    projects_root = paths.DATA_ROOT / "projects"
    if not projects_root.is_dir():
        return
    for proj_dir in projects_root.iterdir():
        if not proj_dir.is_dir():
            continue
        old = proj_dir / subdir
        new = proj_dir / "working" / subdir
        if not old.exists():
            continue
        if new.exists():
            logger.warning(
                "projects_v2: %s exists alongside %s; leaving both in place",
                old, new,
            )
            continue
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
            logger.warning(
                "projects_v2: moved %s -> %s", old, new
            )
        except Exception:
            logger.exception(
                "projects_v2: move %s -> %s failed", old, new
            )


async def run(db: aiosqlite.Connection) -> bool:
    """Execute the layout migration on the given connection.

    Returns True if applied, False if already at projects_v2.
    """
    current = await _schema_version(db)
    if current == SCHEMA_VERSION:
        logger.info("projects_v2: already applied; skipping")
        return False
    if current != "projects_v1":
        # We only run after projects_v1 has stamped its version. If
        # the stamp is missing or unexpected, surface and skip — the
        # boot path will retry next time.
        logger.warning(
            "projects_v2: prerequisite schema_version='projects_v1' missing "
            "(found %r); skipping. projects_v1 will run first on a fresh "
            "DB and projects_v2 follows on the next boot.",
            current,
        )
        return False

    logger.warning("projects_v2: starting layout migration")

    _wipe_legacy_root_dirs()
    _move_skills_under_claude()
    _rename_inputs_to_uploads()
    _move_subdir_into_working("knowledge")
    _move_subdir_into_working("memory")

    # Stamp the new version last so a partial run retries cleanly.
    await db.execute(
        "INSERT OR REPLACE INTO team_config (key, value) VALUES "
        "('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    await db.commit()
    logger.warning("projects_v2: layout migration complete")
    return True

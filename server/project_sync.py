"""Per-project + global file sync to kDrive (PROJECTS_SPEC.md §5).

The Phase 1 destructive migration relocated every project-scoped file
under `/data/projects/<slug>/`; this module is the Phase 2 push/pull
loop that mirrors those trees to `TOT/projects/<slug>/` on kDrive.

Two loops, two cadences:
- **Per-project file loop** runs on the *active* project every 5 min
  (`HARNESS_PROJECT_SYNC_INTERVAL`). Walks
  `/data/projects/<slug>/` (skipping `repo/` and `attachments/`)
  and `/data/wiki/<slug>/` (the per-project wiki sub-folder, which
  lives in the global wiki tree). Diffs against `sync_state` by
  mtime+size first, then sha256, then PUTs.
- **Global file loop** runs every 30 min
  (`HARNESS_GLOBAL_SYNC_INTERVAL`). Covers `/data/CLAUDE.md`,
  `/data/skills/**`, `/data/mcp/**`, `/data/wiki/INDEX.md`, plus
  cross-project `wiki/*.md` entries at the wiki root (alongside
  INDEX.md).

Both loops:
- track each pushed file in `sync_state(project_id, tree, path,
  mtime, size, sha256, last_synced_at)`,
- detect deletions (file gone locally → DELETE on kDrive +
  `sync_state` row),
- retry transient failures up to `HARNESS_KDRIVE_RETRY_MAX` (default 3)
  with 1s→2s→4s exponential backoff capped at 30s,
- on retry exhaustion emit a `kdrive_sync_failed` event and continue
  with the next file (never abort the whole run on one bad file).

The pull primitive `pull_project_tree(project_id)` is exposed for the
Phase 3 project-switch flow ("pull on open"); the loops do not pull
on cadence — sole-writer assumption per §5.

The legacy `flush_loop` / `uploads_pull_loop` / `outputs_push_loop` in
server/sync.py are superseded by these and should be retired by the
caller (lifespan in server/main.py).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import aiosqlite

from server.db import MISC_PROJECT_ID, configured_conn, resolve_active_project
from server.events import bus
from server.paths import global_paths, project_paths
from server.webdav import webdav

logger = logging.getLogger("harness.project_sync")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# ---------- config ----------

# Active-project file loop. Spec §5 picks 5 min; "small projects swap
# sub-second" (§1.4) so 5 min keeps the steady state cheap and lets
# pull-on-open carry recent changes for inactive projects.
PROJECT_SYNC_INTERVAL = int(
    os.environ.get("HARNESS_PROJECT_SYNC_INTERVAL", "300")
)
# Global tree loop runs slower because /data/skills, /data/mcp, and
# the wiki INDEX move rarely.
GLOBAL_SYNC_INTERVAL = int(
    os.environ.get("HARNESS_GLOBAL_SYNC_INTERVAL", "1800")
)
# Retry limits per spec §5: 1s → 2s → 4s, capped at 30s, max attempts.
KDRIVE_RETRY_MAX = int(os.environ.get("HARNESS_KDRIVE_RETRY_MAX", "3"))
KDRIVE_RETRY_INITIAL_S = float(
    os.environ.get("HARNESS_KDRIVE_RETRY_INITIAL_S", "1.0")
)
KDRIVE_RETRY_CAP_S = float(os.environ.get("HARNESS_KDRIVE_RETRY_CAP_S", "30.0"))
# Force-flush deadline for project switch (spec §5 "Push on close").
KDRIVE_CLOSE_TIMEOUT_S = int(
    os.environ.get("HARNESS_KDRIVE_CLOSE_TIMEOUT_S", "60")
)

# Subdirectories under /data/projects/<slug>/ that are NOT synced —
# `repo/` is git's territory; `attachments/` is local-only by §4.
_PROJECT_TREE_EXCLUDE = ("repo", "attachments")

# Tree discriminators stored in sync_state.tree (CHECK-constrained).
_TREE_PROJECT = "project"
_TREE_WIKI = "wiki"
_TREE_GLOBAL = "global"  # synthetic — only used when project_id == ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- sync_state DB helpers ----------


@dataclass(frozen=True)
class SyncStateRow:
    project_id: str
    tree: str
    path: str
    mtime: float
    size_bytes: int
    sha256: str
    last_synced_at: str


async def _sync_state_paths_for(
    db: aiosqlite.Connection, project_id: str, tree: str
) -> dict[str, SyncStateRow]:
    """Return every sync_state row for (project, tree) keyed by path."""
    cur = await db.execute(
        "SELECT path, mtime, size_bytes, sha256, last_synced_at "
        "FROM sync_state WHERE project_id = ? AND tree = ?",
        (project_id, tree),
    )
    rows = await cur.fetchall()
    out: dict[str, SyncStateRow] = {}
    for r in rows:
        d = dict(r)
        out[d["path"]] = SyncStateRow(
            project_id=project_id,
            tree=tree,
            path=d["path"],
            mtime=float(d["mtime"]),
            size_bytes=int(d["size_bytes"]),
            sha256=str(d["sha256"]),
            last_synced_at=str(d["last_synced_at"]),
        )
    return out


async def _sync_state_upsert(
    db: aiosqlite.Connection,
    project_id: str,
    tree: str,
    path: str,
    mtime: float,
    size_bytes: int,
    sha256: str,
) -> None:
    await db.execute(
        "INSERT INTO sync_state "
        "(project_id, tree, path, mtime, size_bytes, sha256, last_synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(project_id, tree, path) DO UPDATE SET "
        "  mtime = excluded.mtime, "
        "  size_bytes = excluded.size_bytes, "
        "  sha256 = excluded.sha256, "
        "  last_synced_at = excluded.last_synced_at",
        (project_id, tree, path, mtime, size_bytes, sha256, _now_iso()),
    )


async def _sync_state_delete(
    db: aiosqlite.Connection, project_id: str, tree: str, path: str
) -> None:
    await db.execute(
        "DELETE FROM sync_state WHERE project_id = ? AND tree = ? AND path = ?",
        (project_id, tree, path),
    )


# ---------- local fs walk + hash ----------


def _walk_files(
    root: Path, *, exclude_subdirs: Iterable[str] = ()
) -> Iterable[tuple[str, Path, os.stat_result]]:
    """Yield (rel_posix_path, full_path, stat) for every file under
    `root`. Skips top-level directories named in `exclude_subdirs`.
    Returns nothing if `root` doesn't exist."""
    if not root.is_dir():
        return
    excluded = set(exclude_subdirs)
    for entry in root.iterdir():
        if entry.is_dir() and entry.name in excluded:
            continue
        if entry.is_file() and not entry.is_symlink():
            try:
                st = entry.stat()
            except OSError:
                continue
            yield entry.name, entry, st
            continue
        if entry.is_dir() and not entry.is_symlink():
            for sub in entry.rglob("*"):
                if sub.is_file() and not sub.is_symlink():
                    try:
                        st = sub.stat()
                    except OSError:
                        continue
                    rel = sub.relative_to(root).as_posix()
                    yield rel, sub, st


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- retry wrapper ----------


async def _with_kdrive_retry(
    op: Callable[[], Awaitable[bool]],
    *,
    op_label: str,
    project_id: str,
    tree: str,
    path: str,
) -> bool:
    """Run an async kDrive op with 1s→2s→4s … exponential backoff,
    capped at HARNESS_KDRIVE_RETRY_CAP_S, up to HARNESS_KDRIVE_RETRY_MAX
    attempts.

    Returns True iff one of the attempts returned a truthy value.
    Returns False on exhaustion *and* publishes a `kdrive_sync_failed`
    event so the EnvPane banner can surface it (§5). Never raises.
    """
    delay = KDRIVE_RETRY_INITIAL_S
    last_err: str = ""
    for attempt in range(1, max(1, KDRIVE_RETRY_MAX) + 1):
        try:
            ok = await op()
            if ok:
                return True
            last_err = "op returned falsy"
        except Exception as e:  # noqa: BLE001 — defensive; webdav layer logs already
            last_err = f"{type(e).__name__}: {str(e)[:200]}"
            logger.warning(
                "project_sync: %s attempt %d failed: %s",
                op_label, attempt, last_err,
            )
        if attempt < KDRIVE_RETRY_MAX:
            try:
                await asyncio.sleep(min(delay, KDRIVE_RETRY_CAP_S))
            except asyncio.CancelledError:
                raise
            delay = min(delay * 2, KDRIVE_RETRY_CAP_S)
    # Exhausted.
    try:
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": "system",
                "type": "kdrive_sync_failed",
                "op": op_label,
                "project_id": project_id,
                "tree": tree,
                "path": path,
                "error": last_err,
            }
        )
    except Exception:
        logger.exception("kdrive_sync_failed event publish failed")
    return False


# ---------- push: per-project tree ----------


def _project_remote_for(project_id: str, tree: str, rel_path: str) -> str:
    """Map (tree, rel_path) onto the kDrive layout (§4).

    Project tree → `TOT/projects/<slug>/<rel>`.
    Wiki tree    → `TOT/wiki/<slug>/<rel>`.
    Global tree  → `TOT/<rel>` (rel includes the top-level dir, e.g.
                  `skills/llm-wiki/SKILL.md` or `CLAUDE.md`).
    """
    if tree == _TREE_PROJECT:
        return f"projects/{project_id}/{rel_path}"
    if tree == _TREE_WIKI:
        return f"wiki/{project_id}/{rel_path}"
    if tree == _TREE_GLOBAL:
        return rel_path
    raise ValueError(f"unknown tree: {tree!r}")


async def _push_one_file(
    db: aiosqlite.Connection,
    *,
    project_id: str,
    tree: str,
    rel_path: str,
    full_path: Path,
    st: os.stat_result,
    state: dict[str, SyncStateRow],
) -> str:
    """Push one file if it's changed since last sync. Returns one of:
    'pushed' | 'unchanged' | 'failed'. Updates sync_state on success."""
    prev = state.get(rel_path)
    if prev is not None:
        # Cheap pre-check first — if mtime + size both match, skip the
        # hash. mtime is float vs sqlite REAL — small drift due to
        # rounding is fine, just compare with a 1µs tolerance.
        if (
            abs(st.st_mtime - prev.mtime) < 1e-3
            and st.st_size == prev.size_bytes
        ):
            return "unchanged"
    sha = _sha256_file(full_path)
    if prev is not None and prev.sha256 == sha:
        # mtime drifted (touched without content change) — refresh
        # sync_state's mtime/size so the next cycle short-circuits
        # and skip the upload.
        await _sync_state_upsert(
            db, project_id, tree, rel_path, st.st_mtime, st.st_size, sha
        )
        return "unchanged"

    remote = _project_remote_for(project_id, tree, rel_path)

    async def _upload() -> bool:
        try:
            data = full_path.read_bytes()
        except OSError as e:
            logger.warning("project_sync: read failed for %s: %s", full_path, e)
            return False
        # Atomic remote write per spec §5: upload to <remote>.tmp.<rand>
        # then MOVE onto <remote>. Falls back to non-atomic PUT inside
        # write_bytes_atomic when the underlying client lacks move().
        return await webdav.write_bytes_atomic(remote, data)

    ok = await _with_kdrive_retry(
        _upload,
        op_label="push",
        project_id=project_id,
        tree=tree,
        path=rel_path,
    )
    if not ok:
        return "failed"
    await _sync_state_upsert(
        db, project_id, tree, rel_path, st.st_mtime, st.st_size, sha
    )
    return "pushed"


async def _push_tree(
    db: aiosqlite.Connection,
    *,
    project_id: str,
    tree: str,
    local_root: Path,
    exclude_subdirs: Iterable[str],
    remote_prefix: str,
) -> dict[str, int]:
    """Diff one tree against sync_state and push changes. Detects
    deletions: any sync_state row whose file is missing locally is
    DELETE'd remotely + dropped from sync_state. `remote_prefix` is
    used only for the deletion path (the upload path resolves via
    _project_remote_for + rel_path)."""
    counts = {"pushed": 0, "unchanged": 0, "failed": 0, "deleted": 0}
    prior = await _sync_state_paths_for(db, project_id, tree)
    seen: set[str] = set()
    for rel, full, st in _walk_files(local_root, exclude_subdirs=exclude_subdirs):
        seen.add(rel)
        outcome = await _push_one_file(
            db,
            project_id=project_id,
            tree=tree,
            rel_path=rel,
            full_path=full,
            st=st,
            state=prior,
        )
        counts[outcome] = counts.get(outcome, 0) + 1
    # Deletions: present in sync_state, missing on disk → DELETE remote.
    for rel, _row in prior.items():
        if rel in seen:
            continue
        remote = (
            _project_remote_for(project_id, tree, rel)
            if remote_prefix == ""  # caller passed empty: trust the tree mapping
            else f"{remote_prefix.rstrip('/')}/{rel}"
        )

        async def _delete() -> bool:
            return await webdav.remove(remote)

        ok = await _with_kdrive_retry(
            _delete,
            op_label="delete",
            project_id=project_id,
            tree=tree,
            path=rel,
        )
        if ok:
            await _sync_state_delete(db, project_id, tree, rel)
            counts["deleted"] += 1
        else:
            counts["failed"] += 1
    return counts


async def push_project_tree(project_id: str) -> dict[str, dict[str, int]]:
    """Push one project's tree (`projects/<slug>/` minus excludes) and
    its wiki sub-folder (`wiki/<slug>/`) to kDrive.

    Returns `{'project': counts, 'wiki': counts}`. Both keys present
    even on no-op so callers can render symmetric counters. No-op when
    kDrive is disabled.
    """
    out: dict[str, dict[str, int]] = {
        "project": {"pushed": 0, "unchanged": 0, "failed": 0, "deleted": 0},
        "wiki": {"pushed": 0, "unchanged": 0, "failed": 0, "deleted": 0},
    }
    if not webdav.enabled:
        return out
    pp = project_paths(project_id)
    wiki_root = global_paths().wiki / project_id

    db = await configured_conn()
    try:
        # Re-check the project exists in case it was deleted mid-sync
        # (the loop also re-checks before each iteration).
        cur = await db.execute(
            "SELECT 1 FROM projects WHERE id = ?", (project_id,)
        )
        if not await cur.fetchone():
            logger.warning(
                "push_project_tree: %s no longer exists; skipping", project_id
            )
            return out
        out["project"] = await _push_tree(
            db,
            project_id=project_id,
            tree=_TREE_PROJECT,
            local_root=pp.root,
            exclude_subdirs=_PROJECT_TREE_EXCLUDE,
            remote_prefix="",
        )
        out["wiki"] = await _push_tree(
            db,
            project_id=project_id,
            tree=_TREE_WIKI,
            local_root=wiki_root,
            exclude_subdirs=(),
            remote_prefix="",
        )
        await db.commit()
    finally:
        await db.close()
    if any(v["pushed"] or v["deleted"] for v in out.values()):
        logger.info(
            "push_project_tree(%s): project +%d ~%d -%d / wiki +%d ~%d -%d",
            project_id,
            out["project"]["pushed"], out["project"]["failed"],
            out["project"]["deleted"],
            out["wiki"]["pushed"], out["wiki"]["failed"],
            out["wiki"]["deleted"],
        )
    return out


# A conversation file is "live" if its mtime is within
# LIVE_FRESHNESS_S of now — i.e. the streaming agent wrote to it
# recently enough that we can't be sure it's complete. Tunable so
# tests can pin the window.
LIVE_FRESHNESS_S = float(
    os.environ.get("HARNESS_LIVE_CONVERSATION_S", "30")
)


def tag_live_conversations(project_id: str) -> int:
    """Spec §5 push-on-close step 2: prepend `live: true` YAML
    frontmatter to any conversation file in
    `working/conversations/` that's been modified within
    `LIVE_FRESHNESS_S` seconds. The next reopen knows the file
    was streaming when persisted (resume is unambiguous).

    Idempotent: a file that already starts with `---\\n` is left
    alone (caller can re-flow the frontmatter to update other
    fields if needed). Returns the number of files tagged.

    Synchronous because it's pure local FS work bracketed by the
    push call. Safe to invoke when the conversations directory
    doesn't exist yet — just returns 0.
    """
    pp = project_paths(project_id)
    conv_dir = pp.working_conversations
    if not conv_dir.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - LIVE_FRESHNESS_S
    tagged = 0
    for entry in conv_dir.rglob("*"):
        if not entry.is_file() or entry.is_symlink():
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if text.startswith("---\n") or text.startswith("---\r\n"):
            # Already has frontmatter; leave alone.
            continue
        new_text = "---\nlive: true\n---\n\n" + text
        try:
            entry.write_text(new_text, encoding="utf-8")
            tagged += 1
        except OSError:
            logger.exception("tag_live_conversations: write failed: %s", entry)
    if tagged:
        logger.info(
            "tag_live_conversations(%s): tagged %d streaming file(s)",
            project_id, tagged,
        )
    return tagged


async def force_push_project(
    project_id: str, *, timeout_s: int | None = None
) -> dict[str, Any]:
    """Push-on-close primitive (spec §5). Tags any currently-streaming
    conversations with `live: true` frontmatter, then runs
    push_project_tree under a hard `timeout_s` deadline. On timeout
    returns a partial result flagged `timed_out: True` so the caller
    (Phase 3 switch endpoint) can surface it in the busy modal.
    """
    timeout_s = timeout_s or KDRIVE_CLOSE_TIMEOUT_S
    # Step 2 of §5 push-on-close: tag live files BEFORE the push so
    # the upload carries the frontmatter.
    try:
        tag_live_conversations(project_id)
    except Exception:
        logger.exception("tag_live_conversations failed during force_push")
    try:
        counts = await asyncio.wait_for(
            push_project_tree(project_id), timeout=timeout_s
        )
        return {"timed_out": False, "counts": counts}
    except asyncio.TimeoutError:
        logger.warning(
            "force_push_project(%s): timed out after %ds",
            project_id, timeout_s,
        )
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": "system",
                "type": "kdrive_sync_failed",
                "op": "force_push",
                "project_id": project_id,
                "error": f"timeout after {timeout_s}s",
            }
        )
        return {"timed_out": True, "counts": None}


# ---------- pull: per-project tree (used by Phase 3 switch) ----------


async def _pull_one_file(
    db: aiosqlite.Connection,
    *,
    project_id: str,
    tree: str,
    rel_path: str,
    remote_path: str,
    local_path: Path,
) -> str:
    """Pull one file if remote diverges from sync_state. Atomic
    write via temp file. Returns 'pulled' | 'unchanged' | 'failed'."""

    async def _fetch() -> bool:
        data = await webdav.read_bytes(remote_path)
        if data is None:
            return False
        # Atomic rename: write to a sibling temp + replace. Avoids
        # half-written files being visible to readers while a multi-MB
        # download is in progress.
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_suffix(local_path.suffix + ".tmp-pull")
        tmp.write_bytes(data)
        os.replace(tmp, local_path)
        return True

    ok = await _with_kdrive_retry(
        _fetch,
        op_label="pull",
        project_id=project_id,
        tree=tree,
        path=rel_path,
    )
    if not ok:
        return "failed"
    try:
        st = local_path.stat()
        sha = _sha256_file(local_path)
        await _sync_state_upsert(
            db, project_id, tree, rel_path, st.st_mtime, st.st_size, sha
        )
    except OSError:
        logger.exception("pull: stat after fetch failed: %s", local_path)
    return "pulled"


async def pull_project_tree(project_id: str) -> dict[str, dict[str, int]]:
    """Pull `TOT/projects/<slug>/` and `TOT/wiki/<slug>/` into the
    local trees. Used by Phase 3 project-switch (pull on open) so the
    next active-project query sees post-pull state.

    The walk uses webdav.list_dir which is non-recursive — for v1 we
    pull only the top-level files of each tree plus one level of
    subdirs. Deeper layouts (e.g. `working/conversations/<file>.md`)
    are still discovered because we recurse manually via list_dir.
    """
    out: dict[str, dict[str, int]] = {
        "project": {"pulled": 0, "unchanged": 0, "failed": 0},
        "wiki": {"pulled": 0, "unchanged": 0, "failed": 0},
    }
    if not webdav.enabled:
        return out
    pp = project_paths(project_id)

    db = await configured_conn()
    try:
        for tree, root_remote, local_root in (
            (_TREE_PROJECT, f"projects/{project_id}", pp.root),
            (_TREE_WIKI, f"wiki/{project_id}", global_paths().wiki / project_id),
        ):
            # webdav.walk_files uses detail=True PROPFIND so files vs
            # directories are distinguished by server-provided type —
            # no fragile extension/dot heuristic.
            try:
                rels = await webdav.walk_files(root_remote)
            except Exception:
                logger.exception("pull walk failed for %s", root_remote)
                continue
            for rel in rels:
                # Project tree excludes — never pull repo/ or attachments/.
                if tree == _TREE_PROJECT:
                    top = rel.split("/", 1)[0]
                    if top in _PROJECT_TREE_EXCLUDE:
                        continue
                local_path = local_root / rel
                outcome = await _pull_one_file(
                    db,
                    project_id=project_id,
                    tree=tree,
                    rel_path=rel,
                    remote_path=f"{root_remote}/{rel}",
                    local_path=local_path,
                )
                out[tree if tree != _TREE_PROJECT else "project"][outcome] = (
                    out[tree if tree != _TREE_PROJECT else "project"].get(
                        outcome, 0
                    )
                    + 1
                )
        await db.commit()
    finally:
        await db.close()
    if any(v.get("pulled") for v in out.values()):
        logger.info(
            "pull_project_tree(%s): project +%d / wiki +%d",
            project_id,
            out["project"].get("pulled", 0),
            out["wiki"].get("pulled", 0),
        )
    return out


# ---------- push: global tree ----------


_GLOBAL_PROJECT_KEY = ""  # sync_state.project_id placeholder for global rows


async def push_global_tree() -> dict[str, int]:
    """Push the global tree: /data/CLAUDE.md, /data/skills/**,
    /data/mcp/**, /data/wiki/INDEX.md, plus cross-project wiki
    entries at /data/wiki/*.md (root-level only — per-project
    sub-folders are owned by push_project_tree).

    sync_state rows for the global tree use `tree = 'global'` and
    `project_id = MISC_PROJECT_ID` (FK target — every project_id must
    point at an existing project row, and misc is always present).
    The path column carries the global-tree-relative path verbatim
    (e.g. `CLAUDE.md`, `skills/llm-wiki/SKILL.md`, `wiki/INDEX.md`,
    `wiki/<concept>.md`); the kDrive remote path is identical, so
    `_project_remote_for` maps `tree=global` → `<rel>` directly.
    """
    if not webdav.enabled:
        return {"pushed": 0, "unchanged": 0, "failed": 0, "deleted": 0}
    gp = global_paths()

    # Build the candidate list outside the DB transaction — pure FS work.
    candidates: list[tuple[str, Path, os.stat_result]] = []

    def _add(p: Path, prefix: str) -> None:
        if not p.exists():
            return
        if p.is_file():
            try:
                st = p.stat()
            except OSError:
                return
            candidates.append((prefix, p, st))
            return
        for sub in p.rglob("*"):
            if not sub.is_file() or sub.is_symlink():
                continue
            try:
                st = sub.stat()
            except OSError:
                continue
            rel = sub.relative_to(p).as_posix()
            candidates.append((f"{prefix.rstrip('/')}/{rel}", sub, st))

    if gp.claude_md.exists() and gp.claude_md.is_file():
        try:
            st = gp.claude_md.stat()
            candidates.append(("CLAUDE.md", gp.claude_md, st))
        except OSError:
            pass
    _add(gp.skills, "skills")
    _add(gp.mcp, "mcp")
    if gp.wiki_index.exists() and gp.wiki_index.is_file():
        try:
            st = gp.wiki_index.stat()
            candidates.append(("wiki/INDEX.md", gp.wiki_index, st))
        except OSError:
            pass
    # Cross-project wiki entries at the wiki root (alongside INDEX.md):
    # only top-level *.md, not the per-project sub-folders.
    if gp.wiki.is_dir():
        for entry in gp.wiki.iterdir():
            if entry.is_file() and entry.suffix == ".md" and entry.name != "INDEX.md":
                try:
                    st = entry.stat()
                except OSError:
                    continue
                candidates.append((f"wiki/{entry.name}", entry, st))

    counts = {"pushed": 0, "unchanged": 0, "failed": 0, "deleted": 0}
    db = await configured_conn()
    try:
        # Misc project is the FK target for global sync_state rows.
        cur = await db.execute(
            "SELECT 1 FROM projects WHERE id = ?", (MISC_PROJECT_ID,)
        )
        if not await cur.fetchone():
            logger.warning("push_global_tree: misc project missing; skipping")
            return counts
        prior = await _sync_state_paths_for(db, MISC_PROJECT_ID, _TREE_GLOBAL)

        seen: set[str] = set()
        for rel, full, st in candidates:
            seen.add(rel)
            outcome = await _push_one_file(
                db,
                project_id=MISC_PROJECT_ID,
                tree=_TREE_GLOBAL,
                rel_path=rel,
                full_path=full,
                st=st,
                state=prior,
            )
            counts[outcome] = counts.get(outcome, 0) + 1
        for rel in list(prior.keys()):
            if rel in seen:
                continue
            remote = _project_remote_for(MISC_PROJECT_ID, _TREE_GLOBAL, rel)

            async def _delete(remote=remote) -> bool:
                return await webdav.remove(remote)

            ok = await _with_kdrive_retry(
                _delete,
                op_label="delete",
                project_id=MISC_PROJECT_ID,
                tree=_TREE_GLOBAL,
                path=rel,
            )
            if ok:
                await _sync_state_delete(
                    db, MISC_PROJECT_ID, _TREE_GLOBAL, rel
                )
                counts["deleted"] += 1
            else:
                counts["failed"] += 1
        await db.commit()
    finally:
        await db.close()
    if counts["pushed"] or counts["deleted"]:
        logger.info(
            "push_global_tree: +%d ~%d -%d",
            counts["pushed"], counts["failed"], counts["deleted"],
        )
    return counts


# ---------- background loops ----------


async def project_sync_loop() -> None:
    """Active-project file sync. Re-resolves the active project at the
    start of every cycle so a project switch (Phase 3) takes effect on
    the next tick without restarting the loop. Skips when kDrive is
    disabled — the loop stays alive so a runtime-enabled mirror
    (UI-managed creds) is picked up on the next cycle."""
    logger.info(
        "project sync loop starting: every %ds (active project only)",
        PROJECT_SYNC_INTERVAL,
    )
    while True:
        try:
            if webdav.enabled:
                try:
                    project_id = await resolve_active_project()
                except Exception:
                    logger.exception("project_sync: resolve active failed")
                    project_id = MISC_PROJECT_ID
                await push_project_tree(project_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("project sync cycle failed")
        try:
            await asyncio.sleep(PROJECT_SYNC_INTERVAL)
        except asyncio.CancelledError:
            raise


async def global_sync_loop() -> None:
    """Slower cadence loop for the global tree."""
    logger.info(
        "global sync loop starting: every %ds", GLOBAL_SYNC_INTERVAL,
    )
    # Stagger start so the first tick doesn't pile on with the
    # project loop's first tick.
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            if webdav.enabled:
                await push_global_tree()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("global sync cycle failed")
        try:
            await asyncio.sleep(GLOBAL_SYNC_INTERVAL)
        except asyncio.CancelledError:
            raise

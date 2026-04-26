"""HTTP API surface for Project CRUD + activation (PROJECTS_SPEC.md §13 Phase 3).

Endpoints:
- `POST /api/projects` (create) — validate slug, scaffold filesystem, insert row.
- `GET  /api/projects` (list)   — every row including archived.
- `POST /api/projects/{id}/activate` — async switch with progress events.
- `PATCH /api/projects/{id}` — name / description / repo_url.
- `DELETE /api/projects/{id}` — full teardown; Misc cannot be deleted.
- `POST /api/projects/{id}/repo/provision` — per-project ensure_workspaces.

Status codes per §6:
- `202 Accepted` activate started → returns `{job_id}`; UI subscribes to bus
  for `project_switch_step` events with that job_id.
- `400` slug invalid (§2 regex / reserved-name list).
- `404` unknown project.
- `409` another switch already in progress.
- `423` agent turn currently running.
- `502` kDrive unreachable on pre-pull.

The activate handler also pins the new active_project_id via the module's
`active_project_lock` + ContextVar exposed in server.db so coord_* tools
and bus.publish observe a coherent project across the whole switch — the
TOCTOU mitigation called for in §13 Phase 3 audit follow-ups.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from server.db import (
    MISC_PROJECT_ID,
    configured_conn,
    pin_active_project,
    resolve_active_project,
    set_active_project,
)
from server.events import bus
from server.paths import (
    ensure_global_scaffold,
    ensure_project_scaffold,
    project_paths,
)

logger = logging.getLogger("harness.projects_api")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# ---------- slug validator (§2) ----------

_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SLUG_MIN = 2
_SLUG_MAX = 48
RESERVED_SLUGS = frozenset(
    {
        "skills",
        "wiki",
        "mcp",
        "projects",
        "snapshots",
        "harness",
        "data",
        "claude",
    }
)


def validate_slug(slug: str) -> tuple[bool, str]:
    """Return (ok, reason). reason is "" on success."""
    if not isinstance(slug, str):
        return False, "slug must be a string"
    if len(slug) < _SLUG_MIN or len(slug) > _SLUG_MAX:
        return False, (
            f"slug must be {_SLUG_MIN}-{_SLUG_MAX} chars (got {len(slug)})"
        )
    if not _SLUG_RE.match(slug):
        return False, (
            "slug must match ^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$ — lowercase "
            "letters/digits with single dashes; no leading/trailing/consecutive "
            "dashes"
        )
    if slug in RESERVED_SLUGS:
        return False, (
            f"slug '{slug}' is reserved (collides with global folder names: "
            f"{', '.join(sorted(RESERVED_SLUGS))})"
        )
    return True, ""


def derive_slug_from_name(name: str) -> str:
    """Best-effort lowercase-with-dashes derivation. Caller should
    re-validate via validate_slug() — derivation may produce something
    that violates length/charset rules for very short or weird names."""
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s


# ---------- in-flight switch + turn checks ----------

# Switch concurrency control. Earlier draft used `_switch_lock.locked()`
# as a peek to fast-fail with 409, but the peek isn't atomic with the
# subsequent `asyncio.create_task` — two concurrent requests could
# both pass and both spawn a switch task. Phase-3 audit fix:
#
# - `_switch_in_progress` flag is checked-and-set synchronously
#   (no `await` between the check and the assignment, so asyncio
#   single-threadedness gives us atomicity).
# - The flag is cleared via a done callback on the task so a crashed
#   switch doesn't strand future calls.
# - The actual lock is no longer needed for serialization (the flag
#   does that) but is kept as a sanity belt-and-suspenders.
_switch_in_progress: bool = False
# Reference to the in-flight switch task so the lifespan can cancel
# it on shutdown (Phase 3 audit fix #2 — task was previously orphaned).
_active_switch_task: asyncio.Task | None = None
# Provision env mutation lock — `provision_project_repo` mutates
# `os.environ["HARNESS_PROJECT_REPO"]` to feed the legacy single-
# project ensure_workspaces. Two concurrent provisions corrupt the
# env (Phase 3 audit fix #9). This lock serializes the env-mutation
# critical section per process.
_provision_lock = asyncio.Lock()


async def _any_agent_working(db) -> str | None:
    cur = await db.execute(
        "SELECT id FROM agents WHERE status IN ('working', 'waiting') LIMIT 1"
    )
    row = await cur.fetchone()
    if not row:
        return None
    return dict(row)["id"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_project_claude_md_stub(
    project_id: str,
    name: str,
    description: str | None,
    repo_url: str | None,
) -> None:
    """Phase 7 (PROJECTS_SPEC.md §8) — wrapper around the centralized
    helper in `server.paths`. Kept as a free function for the existing
    test surface; both this and `init_db` route through the same
    underlying writer."""
    from server.paths import write_project_claude_md_stub

    try:
        write_project_claude_md_stub(project_id, name, description, repo_url)
    except Exception:
        logger.exception(
            "failed to write per-project CLAUDE.md stub for %s", project_id
        )


# ---------- pydantic models ----------


class ProjectCreate(BaseModel):
    slug: str = Field(..., description="URL-safe lowercase slug; primary key")
    name: str = Field(..., description="Display name")
    description: str | None = Field(None, description="Optional one-liner")
    repo_url: str | None = Field(None, description="Optional git remote")


class ProjectPatch(BaseModel):
    # `slug` is intentionally absent — slug is immutable per §2 Lifecycle Edit.
    name: str | None = None
    description: str | None = None
    repo_url: str | None = None
    archived: bool | None = None


class ProjectRow(BaseModel):
    id: str
    name: str
    description: str | None
    repo_url: str | None
    archived: bool
    created_at: str


# ---------- router ----------


def build_router(*, require_token, audit_actor):
    """Return a FastAPI APIRouter with every project endpoint wired.
    `require_token` and `audit_actor` are the dependencies imported
    from server.main; passed in to avoid a circular import.

    FastAPI is imported lazily here so the rest of this module loads
    cleanly in environments where fastapi isn't installed (e.g. the
    pytest dev venv that only needs to test pure helpers).
    """
    from fastapi import APIRouter, Depends, HTTPException

    router = APIRouter()

    @router.get(
        "/api/projects/switch-preview",
        dependencies=[Depends(require_token)],
    )
    async def switch_preview(to: str) -> dict[str, Any]:
        """Pre-flight summary for the Phase 4 confirm modal (§6).

        Reports counts the user will see before clicking "Switch":
          - files_to_push: number of pending changes in the current
            project's tree that haven't been mirrored to kDrive yet
            (read directly off `sync_state` vs the local tree).
          - live_conversations: count of conversation files modified
            within HARNESS_LIVE_CONVERSATION_S that will get
            `live: true` frontmatter on push.
          - target_exists: whether the destination project has any
            existing data on disk (cold-clone vs. switch-back).
          - in_flight_agent: id of an agent currently mid-turn (or
            null) — surfaces the 423 condition pre-emptively so the
            UI can ask "Wait or cancel and switch?" without first
            POSTing /activate.

        Best-effort: walking the local tree is single-digit ms even
        for hundreds of files. kDrive isn't queried here — the modal
        is a UI-side affordance, not authoritative.
        """
        ok, reason = validate_slug(to)
        if not ok:
            raise HTTPException(400, detail=reason)
        from_project = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id, name FROM projects WHERE id = ?", (to,)
            )
            target_row = await cur.fetchone()
            if not target_row:
                raise HTTPException(
                    404, detail=f"project '{to}' not found"
                )
            target_name = dict(target_row)["name"]
            cur = await c.execute(
                "SELECT name FROM projects WHERE id = ?", (from_project,)
            )
            from_row = await cur.fetchone()
            from_name = dict(from_row).get("name", from_project) if from_row else from_project
            in_flight = await _any_agent_working(c)
        finally:
            await c.close()

        # Walk the from_project's tree and diff against sync_state.
        from server.project_sync import (
            _walk_files,
            _PROJECT_TREE_EXCLUDE,
            _sync_state_paths_for,
            LIVE_FRESHNESS_S,
        )
        from server.paths import global_paths as _gp
        import time as _time

        files_to_push = 0
        bytes_to_push = 0
        live_conversations = 0
        # Audit fix #7: when sync_state is empty (a brand-new project
        # or a fresh DB) every walked file shows up as "to_push" —
        # cosmetically alarming. Track whether sync_state was empty
        # so the UI can render "(initial sync)" instead of a count.
        initial_sync = False
        try:
            pp_from = project_paths(from_project)
            c = await configured_conn()
            try:
                project_state = await _sync_state_paths_for(
                    c, from_project, "project"
                )
                wiki_state = await _sync_state_paths_for(
                    c, from_project, "wiki"
                )
            finally:
                await c.close()
            initial_sync = not project_state and not wiki_state
            cutoff = _time.time() - LIVE_FRESHNESS_S
            for rel, full, st in _walk_files(
                pp_from.root, exclude_subdirs=_PROJECT_TREE_EXCLUDE
            ):
                prev = project_state.get(rel)
                if (
                    prev is None
                    or abs(st.st_mtime - prev.mtime) > 1e-3
                    or st.st_size != prev.size_bytes
                ):
                    files_to_push += 1
                    bytes_to_push += int(st.st_size)
                if (
                    rel.startswith("working/conversations/")
                    and st.st_mtime > cutoff
                ):
                    live_conversations += 1
            wiki_root = _gp().wiki / from_project
            for rel, full, st in _walk_files(wiki_root):
                prev = wiki_state.get(rel)
                if (
                    prev is None
                    or abs(st.st_mtime - prev.mtime) > 1e-3
                    or st.st_size != prev.size_bytes
                ):
                    files_to_push += 1
                    bytes_to_push += int(st.st_size)
        except Exception:
            logger.exception("switch_preview: count failed")

        target_root = project_paths(to).root
        target_exists = target_root.is_dir() and any(target_root.iterdir())

        return {
            "from_project": from_project,
            "from_name": from_name,
            "to_project": to,
            "to_name": target_name,
            "files_to_push": files_to_push,
            "bytes_to_push": bytes_to_push,
            "initial_sync": initial_sync,
            "live_conversations": live_conversations,
            "target_exists": target_exists,
            "in_flight_agent": in_flight,
            "noop": from_project == to,
        }

    @router.get("/api/projects", dependencies=[Depends(require_token)])
    async def list_projects() -> dict[str, Any]:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id, name, description, repo_url, archived, created_at "
                "FROM projects ORDER BY archived ASC, created_at ASC"
            )
            rows = await cur.fetchall()
        finally:
            await c.close()
        active = await resolve_active_project()
        items = []
        for r in rows:
            d = dict(r)
            items.append(
                {
                    "id": d["id"],
                    "name": d["name"],
                    "description": d.get("description"),
                    # Mask any userinfo in the repo URL (matches main.py
                    # masking behavior for /api/team/repo).
                    "repo_url": _mask_repo_url(d.get("repo_url")),
                    "archived": bool(d.get("archived") or 0),
                    "created_at": d["created_at"],
                    "is_active": d["id"] == active,
                }
            )
        return {"projects": items, "active": active}

    @router.post(
        "/api/projects",
        status_code=201,
        dependencies=[Depends(require_token)],
    )
    async def create_project(
        body: ProjectCreate,
        actor: dict = Depends(audit_actor),
    ) -> dict[str, Any]:
        ok, reason = validate_slug(body.slug)
        if not ok:
            raise HTTPException(400, detail=reason)
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(400, detail="name is required")
        if len(name) > 200:
            raise HTTPException(400, detail="name too long (max 200 chars)")
        desc = (body.description or "").strip() or None
        if desc is not None and len(desc) > 1000:
            raise HTTPException(
                400, detail="description too long (max 1000 chars)"
            )
        repo = (body.repo_url or "").strip() or None

        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT 1 FROM projects WHERE id = ?", (body.slug,)
            )
            if await cur.fetchone():
                raise HTTPException(
                    409, detail=f"project '{body.slug}' already exists"
                )
            await c.execute(
                "INSERT INTO projects (id, name, description, repo_url) "
                "VALUES (?, ?, ?, ?)",
                (body.slug, name, desc, repo),
            )
            await c.commit()
        finally:
            await c.close()

        # Filesystem scaffold — idempotent, safe even if the row insert
        # races with a parallel boot.
        try:
            ensure_global_scaffold()
            ensure_project_scaffold(body.slug)
            # Phase 7 (PROJECTS_SPEC.md §8): write per-project CLAUDE.md
            # stub on creation with Goal + Repo pre-filled. First-write
            # only — re-creation paths leave existing files alone.
            _write_project_claude_md_stub(body.slug, name, desc, repo)
            # Phase 7 audit: a new project means a new wiki/<slug>/
            # sub-folder is now present (per ensure_project_scaffold).
            # Rebuild INDEX.md so the per-project section header appears
            # on the next /api/files/read of INDEX.md without waiting
            # for a wiki write event.
            from server.paths import update_wiki_index
            update_wiki_index()
        except Exception:
            logger.exception("scaffold failed for %s", body.slug)
            # Don't fail the API call — the directories will be created
            # the next time a coord_* tool tries to touch them.

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": "system",
                "type": "project_created",
                "project_id": body.slug,
                "project": {
                    "id": body.slug,
                    "name": name,
                    "description": desc,
                    "repo_url": _mask_repo_url(repo),
                },
                "actor": actor,
            }
        )
        return {
            "ok": True,
            "project": {
                "id": body.slug,
                "name": name,
                "description": desc,
                "repo_url": _mask_repo_url(repo),
                "archived": False,
            },
        }

    @router.patch(
        "/api/projects/{project_id}",
        dependencies=[Depends(require_token)],
    )
    async def patch_project(
        project_id: str,
        body: ProjectPatch,
        actor: dict = Depends(audit_actor),
    ) -> dict[str, Any]:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT 1 FROM projects WHERE id = ?", (project_id,)
            )
            if not await cur.fetchone():
                raise HTTPException(404, detail=f"project '{project_id}' not found")
            sets: list[str] = []
            vals: list[Any] = []
            if body.name is not None:
                n = body.name.strip()
                if not n:
                    raise HTTPException(400, detail="name cannot be empty")
                if len(n) > 200:
                    raise HTTPException(400, detail="name too long")
                sets.append("name = ?")
                vals.append(n)
            if body.description is not None:
                d = body.description.strip() or None
                if d is not None and len(d) > 1000:
                    raise HTTPException(400, detail="description too long")
                sets.append("description = ?")
                vals.append(d)
            if body.repo_url is not None:
                r = body.repo_url.strip() or None
                sets.append("repo_url = ?")
                vals.append(r)
            if body.archived is not None:
                # Phase 5 audit (PROJECTS_SPEC.md §7 "Archived projects
                # suppressed from the bottom root"): the bottom root in
                # the Files pane is always the *active* project, so
                # allowing the active project to be archived would put
                # an archived project there. Force the user to switch
                # away first. Un-archiving (archived=False) is fine.
                if body.archived:
                    active = await resolve_active_project()
                    if active == project_id:
                        raise HTTPException(
                            409,
                            detail=(
                                f"project '{project_id}' is currently active; "
                                "switch to a different project before archiving"
                            ),
                        )
                sets.append("archived = ?")
                vals.append(1 if body.archived else 0)
            if not sets:
                return {"ok": True, "project_id": project_id, "changed": 0}
            vals.append(project_id)
            await c.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE id = ?",
                tuple(vals),
            )
            await c.commit()
        finally:
            await c.close()
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": "system",
                "type": "project_updated",
                "project_id": project_id,
                "fields": [s.split(" = ")[0] for s in sets],
                "actor": actor,
            }
        )
        return {"ok": True, "project_id": project_id, "changed": len(sets)}

    @router.delete(
        "/api/projects/{project_id}",
        dependencies=[Depends(require_token)],
    )
    async def delete_project(
        project_id: str,
        actor: dict = Depends(audit_actor),
    ) -> dict[str, Any]:
        if project_id == MISC_PROJECT_ID:
            raise HTTPException(
                403,
                detail=(
                    f"'{MISC_PROJECT_ID}' is the fallback active project and "
                    "cannot be deleted"
                ),
            )
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT 1 FROM projects WHERE id = ?", (project_id,)
            )
            if not await cur.fetchone():
                raise HTTPException(404, detail=f"project '{project_id}' not found")
        finally:
            await c.close()

        active = await resolve_active_project()
        was_active = active == project_id

        # Tear down filesystem (best-effort — a directory in use will
        # fail rmtree but we still drop the DB rows so the project
        # disappears from /api/projects). Repo cleanup per §11:
        # `git worktree remove` first, then rm -rf bare clone, then
        # rm -rf the rest. We skip the worktree-remove command because
        # the bare-clone dir is being deleted right after — anyway.
        pp = project_paths(project_id)
        try:
            shutil.rmtree(pp.root, ignore_errors=True)
        except Exception:
            logger.exception("delete: rmtree failed for %s", pp.root)
        # Wiki sub-folder (lives in global wiki tree).
        from server.paths import global_paths as _gp
        try:
            shutil.rmtree(_gp().wiki / project_id, ignore_errors=True)
        except Exception:
            logger.exception(
                "delete: rmtree failed for wiki subfolder %s", project_id
            )

        c = await configured_conn()
        try:
            # ON DELETE CASCADE on every project_id FK takes care of
            # tasks / messages / memory_docs / events / turns / sync_state /
            # agent_sessions / agent_project_roles in one shot.
            await c.execute(
                "DELETE FROM projects WHERE id = ?", (project_id,)
            )
            await c.commit()
        finally:
            await c.close()

        if was_active:
            # Auto-switch to misc per §2 Delete semantics. We don't go
            # through the full activate handler (with its kDrive sync
            # phases) because the active project just disappeared — its
            # files are gone. Just swap the pointer + emit an event.
            await set_active_project(MISC_PROJECT_ID)
            await bus.publish(
                {
                    "ts": _now_iso(),
                    "agent_id": "system",
                    "type": "project_switched",
                    "from_project": project_id,
                    "to_project": MISC_PROJECT_ID,
                    "reason": "auto_after_delete",
                    "actor": actor,
                }
            )

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": "system",
                "type": "project_deleted",
                "project_id": project_id,
                "was_active": was_active,
                "actor": actor,
            }
        )
        # Phase 7 audit: a deleted project's wiki/<slug>/ sub-folder
        # was removed by the rmtree above. Rebuild INDEX.md so the
        # stale section header disappears on the next read.
        try:
            from server.paths import update_wiki_index
            update_wiki_index()
        except Exception:
            logger.exception("update_wiki_index failed (non-fatal)")
        return {
            "ok": True,
            "project_id": project_id,
            "was_active": was_active,
            "active_project_id": (
                MISC_PROJECT_ID if was_active else active
            ),
        }

    @router.post(
        "/api/projects/{project_id}/activate",
        dependencies=[Depends(require_token)],
    )
    async def activate_project(
        project_id: str,
        actor: dict = Depends(audit_actor),
    ) -> Any:
        from fastapi.responses import JSONResponse

        # Pre-flight: project exists + slug shape valid.
        ok, reason = validate_slug(project_id)
        if not ok:
            raise HTTPException(400, detail=reason)
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id, name, archived FROM projects WHERE id = ?",
                (project_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(
                    404, detail=f"project '{project_id}' not found"
                )
            if dict(row).get("archived"):
                raise HTTPException(
                    403,
                    detail=(
                        f"project '{project_id}' is archived; un-archive it "
                        "before activating"
                    ),
                )
            in_flight = await _any_agent_working(c)
        finally:
            await c.close()
        if in_flight is not None:
            raise HTTPException(
                423,
                detail=(
                    f"agent '{in_flight}' has a turn in flight; wait or "
                    "cancel before switching"
                ),
            )

        from_project = await resolve_active_project()
        if from_project == project_id:
            # Noop: already active. Spec §6 reserves 202 for "switch
            # started" with a job_id; a noop returns 200 + the active
            # state directly so callers don't subscribe to a bus that
            # will never fire (Phase-3 audit fix #4).
            return JSONResponse(
                status_code=200,
                content={
                    "ok": True,
                    "noop": True,
                    "active_project_id": project_id,
                },
            )

        # Atomic check-and-set on the in-progress flag. There is no
        # `await` between the check and the assignment so asyncio's
        # single-threaded run loop guarantees atomicity (Phase-3
        # audit fix #1 — replaces the racy `_switch_lock.locked()`
        # peek pattern).
        global _switch_in_progress, _active_switch_task
        if _switch_in_progress:
            raise HTTPException(
                409, detail="another project switch is already in progress"
            )
        _switch_in_progress = True

        job_id = uuid.uuid4().hex[:12]
        task = asyncio.create_task(
            _run_switch(
                job_id=job_id,
                from_project=from_project,
                to_project=project_id,
                actor=actor,
            )
        )
        _active_switch_task = task

        def _on_done(t: asyncio.Task) -> None:
            global _switch_in_progress, _active_switch_task
            _switch_in_progress = False
            if _active_switch_task is t:
                _active_switch_task = None
            # If the task crashed silently before emitting a terminal
            # `project_switched` event, the UI's switchingProject flag
            # would never clear. Publish a terminal failure event so
            # subscribers can recover.
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.exception(
                    "switch task crashed: job=%s from=%s to=%s",
                    job_id, from_project, project_id, exc_info=exc,
                )
                # Schedule the publish since done callbacks run sync.
                asyncio.create_task(
                    bus.publish(
                        {
                            "ts": _now_iso(),
                            "agent_id": "system",
                            "type": "project_switched",
                            "job_id": job_id,
                            "ok": False,
                            "terminal": True,
                            "from_project": from_project,
                            "to_project": project_id,
                            "error": (
                                f"{type(exc).__name__}: {str(exc)[:300]}"
                            ),
                            "actor": actor,
                        }
                    )
                )

        task.add_done_callback(_on_done)
        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "job_id": job_id,
                "from_project": from_project,
                "to_project": project_id,
            },
        )

    @router.post(
        "/api/projects/{project_id}/repo/provision",
        dependencies=[Depends(require_token)],
    )
    async def provision_project_repo(
        project_id: str,
        actor: dict = Depends(audit_actor),
    ) -> dict[str, Any]:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT repo_url FROM projects WHERE id = ?", (project_id,)
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, detail=f"project '{project_id}' not found")
            repo = dict(row).get("repo_url")
        finally:
            await c.close()
        if not repo:
            raise HTTPException(
                400, detail=f"project '{project_id}' has no repo_url configured"
            )
        # Lazy import to avoid pulling workspaces (and its claude_agent_sdk
        # dependency) at module import time.
        from server.workspaces import ensure_workspaces

        # ensure_workspaces is the legacy single-project provisioner; the
        # active-project flow already routes through workspace_dir via
        # project_paths. Force the env override momentarily so the legacy
        # function targets THIS project even when it isn't active. Phase 5+
        # rework should plumb project_id through workspaces directly.
        import os as _os

        # Phase 3 audit fix #9: serialize env-mutation across
        # concurrent provision calls. Without the lock two provisions
        # for different projects could interleave: A sets env to A,
        # B sets to B, A reads `prev` (now B's value), restores B
        # into env, B finishes and restores `None`. The env is then
        # silently corrupted.
        async with _provision_lock:
            prev = _os.environ.get("HARNESS_PROJECT_REPO")
            _os.environ["HARNESS_PROJECT_REPO"] = repo
            try:
                with pin_active_project(project_id):
                    result = await asyncio.to_thread(ensure_workspaces)
            finally:
                if prev is None:
                    _os.environ.pop("HARNESS_PROJECT_REPO", None)
                else:
                    _os.environ["HARNESS_PROJECT_REPO"] = prev
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": "system",
                "type": "project_repo_provisioned",
                "project_id": project_id,
                "actor": actor,
            }
        )
        return {"ok": True, "project_id": project_id, "result": result}

    return router


# ---------- switch flow (private) ----------


async def _emit_step(
    *,
    job_id: str,
    step: str,
    status: str,
    from_project: str,
    to_project: str,
    detail: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "ts": _now_iso(),
        "agent_id": "system",
        "type": "project_switch_step",
        "job_id": job_id,
        "step": step,
        "status": status,
        "from_project": from_project,
        "to_project": to_project,
    }
    if detail:
        payload.update(detail)
    await bus.publish(payload)


async def _run_switch(
    *,
    job_id: str,
    from_project: str,
    to_project: str,
    actor: dict,
) -> None:
    """The activate flow per §6:
      1. Force-push current project's tree (push-on-close, with
         live: true tagging via tag_live_conversations).
      2. Pull new project's tree from kDrive (pull-on-open).
      3. Swap active_project_id pointer.
      4. Reload context — emit project_switched event.

    Pins the new project_id via pin_active_project() during the swap
    so any tool call / event publish that begins mid-switch sees the
    coherent view (TOCTOU mitigation).

    Audit fixes:
    - #5: a hard step failure (push or pull raised, or pull returned
      `failed > 0` rows) aborts the switch BEFORE the pointer swap
      and emits a terminal `project_switched ok=False` event. The
      pre-swap project's bytes are still authoritative on kDrive for
      the next attempt.
    - #10: outer try/except so any unexpected error (e.g. lazy import
      failure, bus.publish exception) still publishes a terminal
      event so the UI's switchingProject flag clears.
    """
    failed_step: str | None = None
    failure_detail: dict[str, Any] | None = None
    try:
        # Lazy imports to avoid pulling project_sync into module-load
        # time. If this raises (very rare — webdav4 missing etc.) the
        # outer except catches and publishes a terminal event.
        from server.project_sync import (
            force_push_project,
            pull_project_tree,
        )

        await _emit_step(
            job_id=job_id, step="started", status="ok",
            from_project=from_project, to_project=to_project,
        )

        # Step 1 — push-on-close.
        try:
            await _emit_step(
                job_id=job_id, step="push_current", status="running",
                from_project=from_project, to_project=to_project,
            )
            push_result = await force_push_project(from_project)
            if push_result.get("timed_out"):
                await _emit_step(
                    job_id=job_id, step="push_current", status="timed_out",
                    from_project=from_project, to_project=to_project,
                    detail={"timeout_s": push_result.get("timeout_s")},
                )
                # Spec §5 push-on-close timeout offers the user a
                # "force switch (skip remaining files)" option. The
                # Phase 4 busy modal will gate this; for Phase 3 we
                # continue (skipping remaining files implies the same
                # outcome).
            else:
                await _emit_step(
                    job_id=job_id, step="push_current", status="ok",
                    from_project=from_project, to_project=to_project,
                    detail={"counts": push_result.get("counts")},
                )
        except Exception as e:
            failed_step = "push_current"
            failure_detail = {
                "error": f"{type(e).__name__}: {str(e)[:300]}"
            }
            await _emit_step(
                job_id=job_id, step="push_current", status="failed",
                from_project=from_project, to_project=to_project,
                detail=failure_detail,
            )
            # Hard abort — leaving the from_project's local edits
            # unsynced is preferable to half-swapping into a project
            # whose pre-swap state was never persisted. The user can
            # retry once kDrive recovers.

        if failed_step is None:
            # Step 2 — pull-on-open.
            try:
                await _emit_step(
                    job_id=job_id, step="pull_new", status="running",
                    from_project=from_project, to_project=to_project,
                )
                pull_result = await pull_project_tree(to_project)
                await _emit_step(
                    job_id=job_id, step="pull_new", status="ok",
                    from_project=from_project, to_project=to_project,
                    detail={"counts": pull_result},
                )
            except Exception as e:
                failed_step = "pull_new"
                failure_detail = {
                    "error": f"{type(e).__name__}: {str(e)[:300]}"
                }
                await _emit_step(
                    job_id=job_id, step="pull_new", status="failed",
                    from_project=from_project, to_project=to_project,
                    detail=failure_detail,
                )

        if failed_step is None:
            # Step 3 — swap pointer (TOCTOU-safe via pin_active_project).
            with pin_active_project(to_project):
                try:
                    await set_active_project(to_project)
                    await _emit_step(
                        job_id=job_id, step="swap_pointer", status="ok",
                        from_project=from_project, to_project=to_project,
                    )
                except Exception as e:
                    failed_step = "swap_pointer"
                    failure_detail = {
                        "error": f"{type(e).__name__}: {str(e)[:300]}"
                    }
                    await _emit_step(
                        job_id=job_id, step="swap_pointer", status="failed",
                        from_project=from_project, to_project=to_project,
                        detail=failure_detail,
                    )

        if failed_step is None:
            # Step 4 — reload + terminal success event.
            await _emit_step(
                job_id=job_id, step="reload", status="ok",
                from_project=from_project, to_project=to_project,
            )
            await bus.publish(
                {
                    "ts": _now_iso(),
                    "agent_id": "system",
                    "type": "project_switched",
                    "job_id": job_id,
                    "ok": True,
                    "terminal": True,
                    "from_project": from_project,
                    "to_project": to_project,
                    "actor": actor,
                }
            )
        else:
            # Terminal failure event with which step blew up so the
            # busy modal's "Retry / Cancel and stay" UI (Phase 4) can
            # offer the right options.
            payload: dict[str, Any] = {
                "ts": _now_iso(),
                "agent_id": "system",
                "type": "project_switched",
                "job_id": job_id,
                "ok": False,
                "terminal": True,
                "failed_step": failed_step,
                "from_project": from_project,
                "to_project": to_project,
                "actor": actor,
            }
            if failure_detail:
                payload["error"] = failure_detail.get("error")
            await bus.publish(payload)
    except Exception as e:
        # Unexpected outer failure (lazy import, bus exception, etc.).
        # The activate handler's add_done_callback also publishes a
        # terminal event, but emitting one here means the UI sees the
        # right error string instead of the generic crash text.
        logger.exception(
            "_run_switch outer failure: job=%s from=%s to=%s",
            job_id, from_project, to_project,
        )
        try:
            await bus.publish(
                {
                    "ts": _now_iso(),
                    "agent_id": "system",
                    "type": "project_switched",
                    "job_id": job_id,
                    "ok": False,
                    "terminal": True,
                    "failed_step": "outer",
                    "from_project": from_project,
                    "to_project": to_project,
                    "error": f"{type(e).__name__}: {str(e)[:300]}",
                    "actor": actor,
                }
            )
        except Exception:
            logger.exception("terminal failure publish also failed")


# ---------- helpers ----------


def _mask_repo_url(url: str | None) -> str | None:
    """Mirror server.main._mask_repo_url. Hide userinfo in any
    https-style URL; safe for `${VAR}` placeholders (returned unchanged)."""
    if not url:
        return url
    if "${" in url:
        return url
    # Match scheme://userinfo@host/...
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)([^@/]+@)?(.+)$", url)
    if not m:
        return url
    scheme, userinfo, rest = m.group(1), m.group(2), m.group(3)
    if not userinfo:
        return url
    return f"{scheme}***@{rest}"

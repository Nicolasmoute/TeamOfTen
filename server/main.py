from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import (
    BackgroundTasks,
    Body,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from server.agents import (
    AGENT_DAILY_CAP_USD,
    TEAM_DAILY_CAP_USD,
    _today_spend,
    cancel_agent,
    cancel_all_agents,
    stale_task_watch_loop,
    is_paused,
    run_agent,
    set_paused,
)
from server import files as filesmod
from server import truth as truthmod
from server.paths import project_paths
from server.db import configured_conn, crash_recover, init_db, resolve_active_project
from server.events import bus
from server.webdav import webdav
from server.sync import (
    attachments_trim_loop,
    sessions_trim_loop,
    events_trim_loop,
    snapshot_loop,
)
from server.project_sync import (
    global_sync_loop,
    project_sync_loop,
)
from server.recurrences import recurrence_scheduler_loop
from server.workspaces import ensure_workspaces, get_status as get_workspaces_status

logger = logging.getLogger("harness.main")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)

STARTED_AT = datetime.now(timezone.utc)
STATIC_DIR = Path(__file__).parent / "static"

# Optional bearer-token auth. If unset, the API is wide open (current
# behavior). If set, every /api/* call (except /api/health) and the
# WebSocket must present `Authorization: Bearer <token>` (or for WS,
# `?token=<token>` in the URL since browsers can't add headers to WS
# connections).
HARNESS_TOKEN = os.environ.get("HARNESS_TOKEN", "").strip()


async def require_token(
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency. No-op when HARNESS_TOKEN is unset."""
    if not HARNESS_TOKEN:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = authorization[len("Bearer "):].strip()
    if presented != HARNESS_TOKEN:
        raise HTTPException(status_code=403, detail="invalid bearer token")


def audit_actor(request: Request) -> dict[str, str]:
    """Attach source metadata to destructive-action events.

    Single-user deploy: source is always "human". But the harness
    token can be shared across devices or (accidentally) leaked; when
    it is, the IP + UA on each audit event is the only way to tell
    which device fired a destructive change after the fact. Cheap to
    attach, impossible to recover later.
    """
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")[:120]
    return {"source": "human", "ip": ip, "ua": ua}

# If package-data shipped /static correctly, INDEX_HTML is the real page.
# If not, we want a visible error page, not an import crash that makes
# the container restart-loop with zero logs.
#
# Cache-busting: stamp app.js + style.css with a mtime-derived `?v=`
# query string so every redeploy invalidates mobile-browser caches.
# Without this, Chrome on Android holds onto the old asset for hours
# even after a hard reload of the HTML, because the asset URL itself
# never changes.
try:
    _index_raw = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    def _stamp(filename: str) -> str:
        try:
            return f"{int((STATIC_DIR / filename).stat().st_mtime)}"
        except Exception:
            return "1"
    # Cache-bust the ES-module import of compass.js inside app.js
    # FIRST — browsers cache module URLs aggressively, and a stale
    # compass.js produces hard-to-reproduce bugs (e.g. dashboard
    # hitting an old API contract). We rewrite the import path in
    # app.js with compass.js's mtime; then app.js's own mtime is
    # captured for _v_app AFTER that write. The regex matches both
    # the first-boot form (no query) and re-boots (already stamped).
    _v_compass = _stamp("compass.js")
    try:
        import re as _re
        app_path = STATIC_DIR / "app.js"
        _app_raw = app_path.read_text(encoding="utf-8")
        _app_busted = _re.sub(
            r'from\s+"/static/compass\.js(?:\?v=\d+)?"',
            f'from "/static/compass.js?v={_v_compass}"',
            _app_raw,
        )
        if _app_busted != _app_raw:
            app_path.write_text(_app_busted, encoding="utf-8")
    except Exception:
        logger.exception("compass.js cache-bust rewrite failed (non-fatal)")

    _v_app = _stamp("app.js")
    _v_css = _stamp("style.css")
    _v_compass_css = _stamp("compass.css")
    _v_playbook_css = _stamp("playbook.css")
    INDEX_HTML = (
        _index_raw
        .replace('"/static/app.js"', f'"/static/app.js?v={_v_app}"')
        .replace('"/static/style.css"', f'"/static/style.css?v={_v_css}"')
        .replace('"/static/compass.css"', f'"/static/compass.css?v={_v_compass_css}"')
        .replace('"/static/playbook.css"', f'"/static/playbook.css?v={_v_playbook_css}"')
    )
except Exception as e:
    logger.error("static/index.html missing (%s): UI will show a fallback page", e)
    INDEX_HTML = (
        "<!doctype html><meta charset=utf-8>"
        "<title>TeamOfTen — UI missing</title>"
        "<body style='font-family:monospace;padding:2em;background:#0d1117;color:#e6edf3'>"
        "<h1>UI assets not packaged</h1>"
        "<p>server/static/index.html is not present in the installed package. "
        "Check pyproject.toml <code>[tool.setuptools.package-data]</code>.</p>"
        "</body>"
    )

# Per-project attachment store (PROJECTS_SPEC.md §4). Resolved at
# request time from the active project so a fresh project switch
# routes pastes into the new project's tree. The
# `HARNESS_ATTACHMENTS_DIR` env override stays for tests + legacy
# deploys; when set it pins ALL projects to one directory (used in
# server/sync.py's retention loop too — kept for compatibility).
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}


def _attachments_dir_for(project_id: str) -> Path:
    override = os.environ.get("HARNESS_ATTACHMENTS_DIR")
    if override:
        return Path(override)
    return project_paths(project_id).attachments

# Binary deliverables the team ships (docx, pdf, png, zip, …). Written
# by agents via coord_save_output; mirrored to WebDAV under outputs/;
# also surfaced read-only in the files pane. Lives on /data so it
# survives restarts.
#
# Note: outputs are per-project at `/data/projects/<slug>/outputs/`.
# The legacy global `/data/outputs/` is still here to back the legacy
# coord_save_output writer; a future cleanup will route that through
# `project_paths(active).outputs`.
OUTPUTS_DIR = Path(os.environ.get("HARNESS_OUTPUTS_DIR", "/data/outputs"))

# Uploads + handoffs are per-project under
# `/data/projects/<active>/uploads/` and
# `/data/projects/<active>/working/handoffs/`. The /api/attachments
# handler resolves the active project at request time; agents reach
# uploads/handoffs via absolute paths documented in CLAUDE.md.


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Phase 6 (PROJECTS_SPEC.md §9): wiki + LLM-Wiki skill + global
    # CLAUDE.md bootstrap. First-write-only — once a file exists the
    # harness leaves it alone so user / Coach edits aren't reverted.
    # Surfaces in /api/health under `wiki`.
    try:
        from server.paths import bootstrap_global_resources
        status = bootstrap_global_resources()
        logger.info("global bootstrap: %s", status)
    except Exception:
        logger.exception("bootstrap_global_resources failed (non-fatal)")
    # Phase 7: rebuild wiki/INDEX.md from the current tree so any
    # out-of-band wiki edits since last boot (manual file copy, sync
    # from the cloud drive) are reflected on the first /api/files/tree hit.
    try:
        from server.paths import update_wiki_index
        update_wiki_index()
    except Exception:
        logger.exception("update_wiki_index failed (non-fatal)")
    # Boot rescue: re-run ensure_project_scaffold for every project so
    # directories added to _PROJECT_SUBDIRS after a project's creation
    # (e.g. truth/ + the truth-index.md seed) are materialized
    # retroactively. Idempotent — only mkdir+write where missing.
    try:
        from server.paths import ensure_project_scaffold

        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id FROM projects WHERE archived = 0"
            )
            project_rows = await cur.fetchall()
        finally:
            await c.close()
        rescued = 0
        for (pid,) in project_rows:
            try:
                pp = ensure_project_scaffold(pid)
                if (pp.truth / "truth-index.md").exists():
                    rescued += 1
            except Exception:
                logger.exception("scaffold rescue failed for %s", pid)
        logger.info(
            "scaffold rescue: %d projects scanned, %d have truth-index.md",
            len(project_rows), rescued,
        )
    except Exception:
        logger.exception("scaffold rescue loop failed (non-fatal)")
    # Crash recovery — reset orphaned state left over from an unclean
    # shutdown. Happens right after init_db so subsequent reads see
    # consistent status. Cheap no-op on a clean DB.
    try:
        reset = await crash_recover()
        if reset["agents_reset"] or reset["tasks_reset"]:
            logger.info(
                "crash recovery: agents_reset=%d tasks_reset=%d",
                reset["agents_reset"], reset["tasks_reset"],
            )
    except Exception:
        logger.exception("crash_recover failed (non-fatal)")
    try:
        from server.runtimes.codex import ensure_codex_tool_contract_current
        cleared = await ensure_codex_tool_contract_current()
        if cleared:
            logger.info(
                "codex tool contract changed: cleared %d persisted thread ids",
                cleared,
            )
    except Exception:
        logger.exception("codex tool-contract refresh failed (non-fatal)")
    # Attachments are per-project (PROJECTS_SPEC.md §4); the upload
    # handler lazily creates the directory under the active project.
    # Honor the legacy env override at boot if set so tests + pinned
    # deploys see an existing directory.
    _att_override = os.environ.get("HARNESS_ATTACHMENTS_DIR")
    if _att_override:
        Path(_att_override).mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    # uploads/ and handoffs/ are per-project under
    # `/data/projects/<active>/uploads/` and
    # `/data/projects/<active>/working/handoffs/`. Agents reach
    # them via the absolute paths documented in the global CLAUDE.md
    # template. A future workspaces.py refactor to per-project
    # worktrees can re-introduce relative symlinks.
    # Claude CLI credential dir. Set via CLAUDE_CONFIG_DIR in the image
    # so OAuth tokens written by `claude /login` land on the /data
    # volume and survive Zeabur redeploys. We mkdir at runtime (not in
    # the Dockerfile) because Zeabur mounts /data over the image FS,
    # and pre-created subpaths under the mount point can race / hang —
    # same rule that applies to /data itself.
    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if claude_dir:
        try:
            Path(claude_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        except Exception:
            logger.exception("failed to mkdir CLAUDE_CONFIG_DIR=%s", claude_dir)
    # Project-repo clone + per-slot worktrees for the active project.
    # No-op if the active project has no repo_url. Logged but errors
    # don't abort startup — agents can still run for non-code work
    # (chat, research, doc writing) when provisioning fails.
    from server.db import resolve_active_project
    active_project_id = await resolve_active_project()
    workspaces_status = await ensure_workspaces(active_project_id)
    logger.info("workspaces: %r", workspaces_status)
    # Migration alarm — flag deploys that still carry the legacy
    # global repo settings but haven't populated the active project's
    # `projects.repo_url`. Provisioning silently no-ops in that case
    # (the new code only reads the per-project column), so an
    # operator who hasn't migrated would see "agents work but
    # coord_commit_push fails" with no obvious cause. One-shot warning
    # at boot — shows up in Zeabur logs.
    if not workspaces_status.get("configured"):
        try:
            legacy_repo = (await _read_team_config_str("project_repo")).strip()
            legacy_env_repo = os.environ.get("HARNESS_PROJECT_REPO", "").strip()
            if legacy_repo or legacy_env_repo:
                logger.warning(
                    "workspaces: active project '%s' has no repo_url but "
                    "legacy %s is set. Copy the URL into the project row "
                    "(Options → Projects → edit) so worktrees provision.",
                    active_project_id,
                    "team_config.project_repo" if legacy_repo
                    else "HARNESS_PROJECT_REPO env",
                )
        except Exception:
            logger.exception("workspaces: legacy-config check failed")
    # Restore observed-context-window map from team_config so we don't
    # relearn from turn 1 on every redeploy.
    from server.agents import _load_observed_windows
    await _load_observed_windows()
    # Batched event-table writer. Single long-lived task that drains
    # events.bus.publish() into batched executemany INSERTs. Replaces
    # the prior model where every non-transient publish spawned its
    # own one-off task that opened/closed a connection per event.
    from server.events import start_event_writer
    await start_event_writer()
    # Background tasks: cloud-drive snapshot + project / global file sync
    # (PROJECTS_SPEC.md §5). The legacy flush_loop / uploads_pull_loop /
    # outputs_push_loop are retired — per-project sync covers the same
    # surface under the spec's `projects/<slug>/` layout (relative to
    # the WebDAV base URL).
    snapshot_task = asyncio.create_task(snapshot_loop())
    project_sync_task = asyncio.create_task(project_sync_loop())
    global_sync_task = asyncio.create_task(global_sync_loop())
    recurrence_task = asyncio.create_task(recurrence_scheduler_loop())
    stale_task_task = asyncio.create_task(stale_task_watch_loop())
    trim_task = asyncio.create_task(events_trim_loop())
    att_trim_task = asyncio.create_task(attachments_trim_loop())
    sessions_trim_task = asyncio.create_task(sessions_trim_loop())
    # Compass — fires daily / bootstrap runs on every project where
    # team_config['compass_enabled_<id>'] is truthy. See
    # Docs/compass-specs.md and server/compass/scheduler.py.
    from server.compass.scheduler import compass_scheduler_loop
    compass_task = asyncio.create_task(compass_scheduler_loop())
    # Compass auto-audit watcher — subscribes to the bus and fires
    # `compass_audit` on artifact events (commit_pushed /
    # decision_written / knowledge_written). See
    # server/compass/audit_watcher.py and Docs/compass-specs.md §5.5.
    # Owns its own task handle (mirrors the telegram pattern); lifespan
    # only kicks it off + tears it down.
    from server.compass.audit_watcher import (
        start_audit_watcher, stop_audit_watcher,
    )
    try:
        await start_audit_watcher()
    except Exception:
        logger.exception("compass audit watcher failed to start (non-fatal)")
    # Playbook scheduler — fires daily reflection + bootstrap runs.
    # See Docs/playbook-specs.md §10 and server/playbook/scheduler.py.
    # Owns its own task handle (mirrors telegram + audit_watcher pattern).
    from server.playbook.scheduler import (
        start_playbook_scheduler, stop_playbook_scheduler,
    )
    try:
        await start_playbook_scheduler()
    except Exception:
        logger.exception("playbook scheduler failed to start (non-fatal)")
    from server.telegram import start_telegram_bridge, stop_telegram_bridge
    # Telegram bridge owns its own task handle (so the UI can reload it
    # live via /api/team/telegram). Lifespan only kicks it off + tears
    # it down — it isn't tracked in bg_tasks.
    try:
        await start_telegram_bridge()
    except Exception:
        logger.exception("telegram bridge failed to start (non-fatal)")
    # Telegram escalation watcher — pings the phone when a pending
    # AskUserQuestion / ExitPlanMode / file-write proposal goes
    # unanswered for too long (or right away if the web UI isn't
    # connected). Independent of the bridge: silently no-ops when
    # Telegram isn't configured, but registers timers regardless so
    # turning the bridge on later picks up future items. Owns its own
    # task handle (mirrors the bridge + audit watcher patterns).
    from server.telegram_escalation import (
        start_escalation_watcher, stop_escalation_watcher,
    )
    try:
        await start_escalation_watcher()
    except Exception:
        logger.exception("telegram escalation watcher failed to start (non-fatal)")
    # Kanban auto-advance subscriber — listens for commit_pushed /
    # audit_report_submitted / task_shipped / compass_audit_logged
    # events and applies the resulting stage transitions. Sibling of
    # the audit-watcher; same own-task-handle pattern.
    from server.kanban import (
        start_kanban_subscriber, stop_kanban_subscriber,
    )
    try:
        await start_kanban_subscriber()
    except Exception:
        logger.exception("kanban subscriber failed to start (non-fatal)")
    # Idle-Player poller — periodic safety-net wake for Players who
    # could be doing pool / pending work but aren't.
    from server.idle_poller import start_idle_poller, stop_idle_poller
    try:
        await start_idle_poller()
    except Exception:
        logger.exception("idle player poller failed to start (non-fatal)")
    # Claude in-app OAuth login reaper — drops orphaned login sessions
    # (CLI subprocess + pty fd) after SESSION_TTL. Non-POSIX hosts
    # short-circuit this; the start_login_reaper call is still safe to
    # make because the underlying loop is just an asyncio.sleep + dict
    # walk — the actual subprocess work only happens inside
    # claude_login.start_login(), which the HTTP endpoint guards.
    from server.claude_login import start_login_reaper, stop_login_reaper
    try:
        await start_login_reaper()
    except Exception:
        logger.exception("claude_login reaper failed to start (non-fatal)")
    # Codex in-app OAuth login reaper — drops orphaned login sessions
    # (subprocess + monitor task) after SESSION_TTL. Mirrors the claude_login
    # reaper immediately above. Non-POSIX guard lives inside start_login().
    from server.codex_login import (
        start_codex_login_reaper,
        stop_codex_login_reaper,
    )
    try:
        await start_codex_login_reaper()
    except Exception:
        logger.exception("codex_login reaper failed to start (non-fatal)")
    # Project CLAUDE.md reconciliation — fire a hidden Coach-driven
    # update for the currently-pinned active project so a redeploy
    # that changes the canonical template at
    # `server/templates/app_dev_claude_md.md` propagates without
    # waiting for the human to re-click activate. Hash-gated, so a
    # boot with no template change is a no-op. Fire-and-forget —
    # don't block the HTTP listener coming up. Activations also
    # trigger this path via `server.projects_api._run_switch`.
    try:
        from server.db import resolve_active_project
        from server.project_claude_md import update_claude_md_via_coach
        active_pid = await resolve_active_project()
        if active_pid:
            asyncio.create_task(
                update_claude_md_via_coach(active_pid, source="boot")
            )
    except Exception:
        logger.exception(
            "project_claude_md: boot update scheduling failed (non-fatal)"
        )
    bg_tasks = (snapshot_task, project_sync_task, global_sync_task, recurrence_task, stale_task_task, trim_task, att_trim_task, sessions_trim_task, compass_task)
    try:
        yield
    finally:
        try:
            await stop_idle_poller()
        except Exception:
            logger.exception("idle player poller shutdown failed")
        try:
            await stop_login_reaper()
        except Exception:
            logger.exception("claude_login reaper shutdown failed")
        try:
            await stop_codex_login_reaper()
        except Exception:
            logger.exception("codex_login reaper shutdown failed")
        try:
            await stop_kanban_subscriber()
        except Exception:
            logger.exception("kanban subscriber shutdown failed")
        try:
            await stop_escalation_watcher()
        except Exception:
            logger.exception("telegram escalation watcher shutdown failed")
        try:
            await stop_telegram_bridge()
        except Exception:
            logger.exception("telegram bridge shutdown failed")
        try:
            await stop_audit_watcher()
        except Exception:
            logger.exception("compass audit watcher shutdown failed")
        try:
            await stop_playbook_scheduler()
        except Exception:
            logger.exception("playbook scheduler shutdown failed")
        # Cancel any in-flight project-provisioning tasks (auto-fired
        # on POST/PATCH /api/projects). Without this, a clone in
        # progress at SIGTERM gets abandoned by the GC and leaves a
        # half-cloned bare directory that the next deploy trips over.
        try:
            from server.projects_api import cancel_in_flight_provision_tasks
            await cancel_in_flight_provision_tasks()
        except Exception:
            logger.exception("provision-task cancel failed")
        for t in bg_tasks:
            t.cancel()
        for t in bg_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        # Drain any events still queued and stop the writer last so the
        # final batch from background-task teardown lands on disk.
        try:
            from server.events import stop_event_writer
            await stop_event_writer()
        except Exception:
            logger.exception("event writer shutdown failed")


app = FastAPI(
    title="TeamOfTen harness",
    version="0.3.0",
    description="Personal orchestration harness — Coach + 10 Players.",
    lifespan=lifespan,
)
# Compress responses ≥ 1 KB. /api/events, /api/turns, /api/tasks, and
# the static JS/CSS are the big wins — JSON arrays and minified JS
# both compress to ~20% original size. Skip tiny responses so we don't
# pay the compression overhead for a {"ok": true} reply.
app.add_middleware(GZipMiddleware, minimum_size=1024)


# Content Security Policy — layered defense alongside the markdown
# DOMPurify pass and the Mermaid SVG sanitize. CSP closes the
# script-injection escape hatch even if a sanitizer hook is bypassed:
# the browser refuses to execute inline / external scripts that don't
# match `script-src`. Header is set on every response (including
# /static and JSON API replies — extra coverage is free; CSP only
# applies meaningfully to HTML the browser parses, but having it
# everywhere costs nothing).
#
# Sources match what the UI actually loads:
#   - script-src 'self' https://esm.sh — preact + preact/hooks load
#     from esm.sh; everything else is vendored under /static/vendor/.
#   - style-src 'self' 'unsafe-inline' — JSX `style={{...}}` props
#     produce inline style attributes that CSP otherwise blocks.
#   - font-src 'self' https://cdn.jsdelivr.net data: — KaTeX CSS
#     references font URLs on jsdelivr; vendor pipeline rewrites
#     relative font URLs to absolute jsdelivr URLs.
#   - connect-src 'self' — covers same-origin fetch + WebSocket.
#   - img-src 'self' data: blob: — local + clipboard-paste images.
#   - object-src 'none' — block <object>/<embed>/<applet>.
#   - frame-ancestors 'none' — refuse to be iframed (clickjacking).
#   - form-action 'self' — block hijacked form posts to external URLs.
#
# Audit follow-up: if a future feature genuinely needs an external
# script source (Stripe, Sentry, etc.), add it here AND document why
# in the threat-model section of Docs/TOT-specs.md.
_CSP_HEADER_VALUE = (
    "default-src 'self'; "
    "script-src 'self' https://esm.sh; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' https://cdn.jsdelivr.net data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)


@app.middleware("http")
async def _csp_middleware(request, call_next):
    response = await call_next(request)
    # Don't overwrite a more specific policy if some other code path
    # has already set one; otherwise apply the default.
    response.headers.setdefault("Content-Security-Policy", _CSP_HEADER_VALUE)
    # Belt-and-braces — a misconfigured browser ignoring CSP still gets
    # the equivalent of frame-ancestors via X-Frame-Options.
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
else:
    logger.error("static dir not found at %s — /static routes will 404", STATIC_DIR)

# Phase 3 — Project CRUD + activation API. The router is built lazily
# from server.projects_api so we can pass the auth dependencies defined
# above (avoids circular imports).
from server.projects_api import build_router as _build_projects_router
app.include_router(_build_projects_router(
    require_token=require_token, audit_actor=audit_actor,
))

# Compass — strategy-engine HTTP API. Same lazy-build pattern as the
# projects router (passes in the auth + audit_actor deps to avoid
# circular imports). The router lives under /api/compass/*.
from server.compass.api import build_router as _build_compass_router
app.include_router(_build_compass_router(
    require_token=require_token, audit_actor=audit_actor,
))

# Playbook — orchestration-strategy engine HTTP API. Mirrors the
# Compass mount pattern. Lives under /api/playbook/*.
from server.playbook.api import build_router as _build_playbook_router
app.include_router(_build_playbook_router(
    require_token=require_token, audit_actor=audit_actor,
))


# ------------------------------------------------------------------
# TruthScore — on-demand project-fidelity evaluator. Single endpoint
# (no router), so we mount it inline. See `Docs/truthscore-specs.md`.
# ------------------------------------------------------------------


@app.post("/api/truthscore")
async def post_truthscore(
    body: dict[str, Any] = Body(default={}),
    _token: None = Depends(require_token),
    actor: dict[str, str] = Depends(audit_actor),
) -> dict[str, Any]:
    """Run TruthScore for the active project. Returns the §2.3
    response shape on success. See `Docs/truthscore-specs.md` §2.3
    for the failure-status mapping (400/409/429/502)."""
    from server import truthscore as ts  # noqa: PLC0415
    from server.db import resolve_active_project as _rap  # noqa: PLC0415

    project_id = await _rap()
    if not project_id:
        raise HTTPException(400, "no active project")
    commentary_raw = body.get("commentary") if isinstance(body, dict) else None
    commentary = (
        commentary_raw.strip() if isinstance(commentary_raw, str) else None
    ) or None
    try:
        return await ts.run_truth_score(project_id, commentary, actor)
    except ts.TruthScoreError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e))


# ------------------------------------------------------------------
# Request models
# ------------------------------------------------------------------


class StartAgentRequest(BaseModel):
    agent_id: str = Field(default="p1", pattern=r"^(coach|p([1-9]|10))$")
    prompt: str = Field(min_length=1, max_length=20_000)
    # Per-turn overrides set via the pane settings popover. Any omitted
    # falls back to the SDK / Dockerfile defaults.
    model: str | None = Field(default=None, max_length=120)
    # None for both means "no per-pane override" — run_agent then
    # consults the Coach-set per-(slot, project) plan_mode_override /
    # effort_override on agent_project_roles before falling back to
    # the implicit default. The UI omits the field from the request body
    # whenever its toggle is off, so missing-field semantics already line
    # up with "no per-pane override."
    plan_mode: bool | None = None
    effort: int | None = Field(default=None, ge=1, le=4)
    # Extended-thinking trigger. None = no per-pane override (consult
    # thinking_override → default off). Claude runtime only; Codex
    # ignores at spawn time.
    thinking: bool | None = None


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=10_000)
    parent_id: str | None = None
    priority: str = Field(default="normal", pattern=r"^(low|normal|high|urgent)$")
    workflow: str = Field(
        default="generic",
        pattern=r"^(code|research|writing|marketing|ops|generic)$",
    )
    # Optional informational tag in v0.3 (no enum gate).
    tracking_reason: str | None = Field(default=None, max_length=80)
    # Trajectory: ordered list of {stage, to} objects. None → default
    # `[{"stage":"execute","to":[]}]`. The harness validates via
    # tools.py:_validate_trajectory.
    trajectory: list[dict[str, Any]] | None = None


# ------- kanban-specific request models (Docs/kanban-specs-v2.md §8) -------


class TaskTrajectoryRequest(BaseModel):
    """POST /api/tasks/{id}/trajectory. Human-side mid-flight reroute,
    sibling of `coord_set_task_trajectory`."""
    trajectory: list[dict[str, Any]] = Field(min_length=1)


class TaskWorkflowRequest(BaseModel):
    workflow: str | None = Field(
        default=None,
        pattern=r"^(code|research|writing|marketing|ops|generic)$",
    )
    tracking_reason: str | None = Field(default=None, max_length=80)


class TaskBlockedRequest(BaseModel):
    blocked: bool
    reason: str = Field(default="", max_length=500)


class TaskSpecRequest(BaseModel):
    """POST /api/tasks/{id}/spec. Human-side spec writer (no
    permission check beyond HARNESS_TOKEN). Same effect as
    coord_write_task_spec — writes spec.md and updates spec_path."""
    body: str = Field(min_length=1, max_length=40_000)


# Kanban v2 (Docs/kanban-specs-v2.md §8)


class TaskApproveStageRequest(BaseModel):
    """POST /api/tasks/{id}/approve_stage. Human-side equivalent of
    `coord_approve_stage` — single transition tool that authorizes the
    next stage, names the assignee, and provides the wake prompt."""
    next_stage: str = Field(
        pattern=r"^(plan|execute|audit_syntax|audit_semantics|ship|archive)$"
    )
    # Required for any non-archive next_stage; rejected on archive.
    assignee: str | None = Field(default=None, max_length=10)
    note: str = Field(default="", max_length=4000)


class TaskFlagDeviationRequest(BaseModel):
    """POST /api/tasks/{id}/flag_deviation. Human-side row insertion
    into deviations_log with noticed_at='human' (Docs/kanban-specs-v2
    §22.1). Lets the human flag drift the kanban didn't catch via
    Coach's approve_stage note or the audit FAIL path."""
    description: str = Field(min_length=1, max_length=2000)


# ------------------------------------------------------------------
# Pages + health
# ------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return INDEX_HTML


# Health-check caches. Avoid hammering the cloud drive / spawning subprocesses on
# every probe (Zeabur or external monitors may poll every 30s).
# Sentinel for "field not supplied" (distinct from None = "clear/unset").
_SENTINEL: Any = object()

_CLAUDE_VERSION_CACHE: dict[str, object] = {}  # populated once per process
_WEBDAV_PROBE_CACHE: dict[str, object] = {"ts": 0.0, "ok": None}
_WEBDAV_PROBE_TTL_SECONDS = 60.0


@app.get("/api/health")
async def health() -> JSONResponse:
    """Public liveness probe. Minimal — returns `{ok: bool,
    auth_required: bool}` and an HTTP status of 200 (DB reachable) or
    503 (DB read failed). Used by the Docker HEALTHCHECK and the
    Zeabur platform probe; deliberately leaks no deployment detail.

    The verbose subsystem report (CLI versions, paths, credential
    presence, WebDAV probe, MCP server names, …) lives on the
    auth-protected `/api/health/detail` endpoint — see audit finding
    "Public health endpoint leaks deployment details" + the threat
    model section in Docs/TOT-specs.md.
    """
    db_ok = True
    try:
        c = await configured_conn()
        try:
            await c.execute("SELECT 1")
        finally:
            await c.close()
    except Exception:
        db_ok = False
    body: dict[str, object] = {
        "ok": db_ok,
        "auth_required": bool(HARNESS_TOKEN),
    }
    return JSONResponse(body, status_code=200 if db_ok else 503)


@app.get("/api/health/detail", dependencies=[Depends(require_token)])
async def health_detail() -> JSONResponse:
    """Per-subsystem readiness probe. Returns 200 if everything required
    is green, 503 if any subsystem is failing, with a `checks` object
    detailing each. Skipped subsystems (webdav/workspaces when unconfigured)
    don't fail the overall ok flag.

    Auth-protected since it leaks deployment detail (CLI versions,
    on-disk paths, credential presence flags, MCP server names).
    """
    checks: dict[str, dict[str, object]] = {}
    overall_ok = True

    # 1. Database writability
    try:
        c = await configured_conn()
        try:
            await c.execute("SELECT 1")
        finally:
            await c.close()
        checks["db"] = {"ok": True}
    except Exception as e:
        checks["db"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        overall_ok = False

    # 2. Static files present
    static_ok = STATIC_DIR.is_dir() and (STATIC_DIR / "index.html").exists()
    checks["static"] = {
        "ok": static_ok,
        "path": str(STATIC_DIR),
    }
    if not static_ok:
        overall_ok = False

    # 3. claude CLI installed — cached for process lifetime (version
    # doesn't change at runtime).
    if not _CLAUDE_VERSION_CACHE:
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                raise
            if proc.returncode == 0:
                _CLAUDE_VERSION_CACHE.update(
                    {"ok": True, "version": stdout_b.decode().strip()}
                )
            else:
                _CLAUDE_VERSION_CACHE.update(
                    {"ok": False, "exit_code": proc.returncode}
                )
        except Exception as e:
            _CLAUDE_VERSION_CACHE.update(
                {"ok": False, "error": f"{type(e).__name__}: {e}"}
            )
    checks["claude_cli"] = dict(_CLAUDE_VERSION_CACHE)
    if not _CLAUDE_VERSION_CACHE.get("ok"):
        overall_ok = False

    # 3b. Claude CLI OAuth token persistence. If CLAUDE_CONFIG_DIR is
    # set and lives on the /data volume, the token survives redeploys.
    # We report {set, dir, credentials_present} so the UI / a check
    # script can say "yes auth will persist" at a glance.
    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if claude_dir:
        cred = Path(claude_dir) / ".credentials.json"
        checks["claude_auth"] = {
            "ok": True,  # informational — doesn't fail health overall
            "config_dir": claude_dir,
            "credentials_present": cred.exists(),
            "hint": (
                "run `claude /login` inside the container to populate"
                if not cred.exists() else "persisted via /data volume"
            ),
        }
    else:
        checks["claude_auth"] = {
            "ok": True,
            "skipped": True,
            "reason": "CLAUDE_CONFIG_DIR not set — auth lives in default ~/.claude and will NOT survive redeploy",
        }

    # 3c. Codex CLI auth persistence. Mirrors the claude_auth probe.
    # CODEX_HOME on /data → auth.json (ChatGPT session) or api-key
    # fallback survives redeploys. method = chatgpt | api_key | none.
    codex_dir = os.environ.get("CODEX_HOME")
    if codex_dir:
        cdir = Path(codex_dir)
        auth_file = cdir / "auth.json"
        # Detect method: ChatGPT session leaves auth.json; API key
        # fallback comes from the encrypted secrets table at runtime
        # (CodexRuntime injects OPENAI_API_KEY env). Health only sees
        # what is on disk plus the secret presence flag.
        method = "none"
        if auth_file.exists() and auth_file.stat().st_size > 0:
            method = "chatgpt"
        else:
            try:
                from server.secrets import get_secret  # lazy import
                if await get_secret("openai_api_key"):
                    method = "api_key"
            except Exception:
                pass
        checks["codex_auth"] = {
            "ok": True,  # informational
            "config_dir": codex_dir,
            "credentials_present": auth_file.exists(),
            "method": method,
            "hint": (
                "run `codex login` inside the container, or save an API key in Options drawer"
                if method == "none" else "persisted via /data volume"
            ),
        }
    else:
        checks["codex_auth"] = {
            "ok": True,
            "skipped": True,
            "reason": "CODEX_HOME not set — Codex runtime unavailable until set on a /data path",
        }

    # 4. Cloud drive — only check if configured. Cached for 60s to avoid
    # writing a probe file on every health hit. We cache the full
    # detail dict (not just a bool) so the UI can keep rendering the
    # error / URL / root between fresh probes.
    if webdav.enabled:
        now = time.monotonic()
        last_ts = float(_WEBDAV_PROBE_CACHE["ts"])
        cached = _WEBDAV_PROBE_CACHE["ok"]
        if isinstance(cached, dict) and (now - last_ts) < _WEBDAV_PROBE_TTL_SECONDS:
            checks["webdav"] = {**cached, "cached": True}
            if not cached.get("ok"):
                overall_ok = False
        else:
            detail = await webdav.probe()
            _WEBDAV_PROBE_CACHE["ts"] = now
            _WEBDAV_PROBE_CACHE["ok"] = detail
            checks["webdav"] = {**detail, "cached": False}
            if not detail.get("ok"):
                overall_ok = False
    else:
        checks["webdav"] = {
            "ok": True,
            "skipped": True,
            "reason": webdav.reason,
            "url": webdav.url,
        }

    # 5. External MCP servers — reports what's loaded from BOTH sources:
    # the legacy `HARNESS_MCP_CONFIG` file path (optional) and the
    # UI-managed `mcp_servers` DB table. New servers are added through
    # the Options drawer; the file path is kept for boot-time bootstrap.
    # Always probes the merged list so DB-sourced servers surface in
    # health regardless of whether the env var is set.
    from server.mcp_config import load_external_servers
    mcp_cfg_path = os.environ.get("HARNESS_MCP_CONFIG", "").strip()
    mcp_status: dict[str, Any] = {}
    file_error: str | None = None
    if mcp_cfg_path:
        mcp_status["config_path"] = mcp_cfg_path
        cfg_file = Path(mcp_cfg_path)
        if not cfg_file.is_file():
            file_error = "file does not exist"
        else:
            try:
                raw = cfg_file.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("top-level must be a JSON object")
                servers_in = parsed.get("servers")
                if servers_in is not None and not isinstance(servers_in, dict):
                    raise ValueError("'servers' key must be an object")
            except Exception as e:
                file_error = f"{type(e).__name__}: {str(e)[:200]}"

    servers, tool_names = load_external_servers()
    mcp_status.update({
        "server_count": len(servers),
        "servers": sorted(servers.keys()),
        "allowed_tool_count": len(tool_names),
    })
    if file_error:
        mcp_status["ok"] = False
        mcp_status["error"] = file_error
        overall_ok = False
    else:
        mcp_status["ok"] = True
        if not servers:
            mcp_status["skipped"] = True
            if mcp_cfg_path:
                mcp_status["reason"] = (
                    "config file parsed but yielded no servers — "
                    "only the in-process 'coord' server is active"
                )
            else:
                mcp_status["reason"] = (
                    "no external MCP servers configured — only the "
                    "in-process 'coord' server is active"
                )
        elif not mcp_cfg_path:
            mcp_status["reason"] = (
                f"{len(servers)} server(s) loaded from DB "
                "(Options drawer); HARNESS_MCP_CONFIG not set"
            )
    checks["mcp_external"] = mcp_status

    # 6. Secrets store — reports master-key readiness + current count.
    # Non-fatal for overall health: an unset HARNESS_SECRETS_KEY is a
    # valid state (user hasn't opted in yet), just means the UI secrets
    # feature is disabled and `${VAR}` placeholders fall back to env.
    from server import secrets as secrets_store
    key_status = secrets_store.status()
    secrets_info: dict[str, Any] = {
        "ok": True,  # informational — see `configured` for actual state
        "configured": bool(key_status.get("ok")),
    }
    if key_status.get("ok"):
        rows = await secrets_store.list_secrets()
        secrets_info["count"] = len(rows)
    else:
        secrets_info["reason"] = key_status.get("reason")
    checks["secrets"] = secrets_info

    # 7. Workspaces — checks the active project's per-slot worktrees.
    # Skipped (and reported ok) when the active project has no repo_url
    # (intended state for content-only projects).
    ws_status = await get_workspaces_status()
    if ws_status.get("configured"):
        slot_states = ws_status.get("slots") or {}
        all_git = bool(slot_states) and all(
            isinstance(s, dict) and s.get("is_git") for s in slot_states.values()
        )
        checks["workspaces"] = {"ok": all_git, "slot_count": len(slot_states)}
        if not all_git:
            overall_ok = False
    else:
        checks["workspaces"] = {"ok": True, "skipped": True}

    # 8. Wiki + LLM-Wiki skill + global CLAUDE.md (Phase 6, §9). Status
    # is set on every boot by `bootstrap_global_resources()`. We re-stat
    # the three sentinel files here so an out-of-band rm shows as
    # "missing" without requiring a restart. Cheap (3 stats).
    from server.paths import bootstrap_status, global_paths
    gp = global_paths()
    skill_md = gp.skills / "llm-wiki" / "SKILL.md"
    sentinels = [gp.wiki_index, skill_md, gp.claude_md]
    if all(p.exists() for p in sentinels):
        # Either present-from-prior-boot or bootstrapped this boot —
        # both are operationally fine; UI distinguishes via the verb.
        wiki_status = bootstrap_status() or "present"
        if wiki_status == "missing":
            wiki_status = "present"  # files appeared after boot fail
        checks["wiki"] = {"ok": True, "status": wiki_status}
    else:
        missing = [str(p) for p in sentinels if not p.exists()]
        checks["wiki"] = {
            "ok": False,
            "status": "missing",
            "missing_files": missing,
        }
        overall_ok = False

    body: dict[str, object] = {
        "ok": overall_ok,
        "auth_required": bool(HARNESS_TOKEN),
        "checks": checks,
    }
    return JSONResponse(body, status_code=200 if overall_ok else 503)


# ------------------------------------------------------------------
# Claude Code auth — accept a pasted credentials JSON blob so operators
# don't need shell access to run `claude /login` inside the container.
# ------------------------------------------------------------------
@app.post("/api/auth/claude", dependencies=[Depends(require_token)])
async def set_claude_auth(
    payload: dict[str, Any] = Body(...),
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Write a pasted .credentials.json blob to
    $CLAUDE_CONFIG_DIR/.credentials.json so the CLI picks it up on the
    next agent spawn. The caller should run `claude /login` on a device
    that has the CLI installed, then paste ~/.claude/.credentials.json
    (macOS) or the equivalent file.

    Body: {"credentials_json": "<raw JSON string>"} OR {"credentials":
    {...parsed object...}}. We re-serialize either way so the file on
    disk is well-formed JSON. Minimal validation — we check that it
    parses and contains `claudeAiOauth` (the OAuth-flow shape). An API
    key setup goes through the secrets store instead, not this path.
    """
    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if not claude_dir:
        raise HTTPException(
            400,
            detail=(
                "CLAUDE_CONFIG_DIR env var is not set, so credentials have "
                "nowhere durable to land. Set it to a path on your "
                "persistent volume (e.g. /data/claude) and redeploy."
            ),
        )
    raw = payload.get("credentials_json")
    parsed_obj = payload.get("credentials")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed_obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise HTTPException(400, detail=f"credentials_json is not valid JSON: {e}")
    if not isinstance(parsed_obj, dict):
        raise HTTPException(
            400,
            detail=(
                "Provide either `credentials_json` (string) or "
                "`credentials` (object)."
            ),
        )
    if "claudeAiOauth" not in parsed_obj:
        raise HTTPException(
            400,
            detail=(
                "JSON is missing the `claudeAiOauth` key — this doesn't "
                "look like a Claude CLI credentials file. Run `claude "
                "/login` on a machine with the CLI, then copy "
                "~/.claude/.credentials.json verbatim."
            ),
        )
    target_dir = Path(claude_dir)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(500, detail=f"could not create {claude_dir}: {e}")
    target_file = target_dir / ".credentials.json"
    try:
        tmp = target_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(parsed_obj, indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(target_file)
    except OSError as e:
        raise HTTPException(500, detail=f"write failed: {e}")
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "claude_auth_updated",
        "path": str(target_file),
        "actor": actor,
    })
    logger.info("claude auth written to %s (actor=%s)", target_file, actor)
    return {
        "ok": True,
        "path": str(target_file),
        "credentials_present": True,
    }


@app.delete("/api/auth/claude", dependencies=[Depends(require_token)])
async def delete_claude_auth(
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Wipe the persisted .credentials.json so the next agent spawn (or
    next /login flow) starts from zero. Lets the operator switch
    accounts without rotating from inside the previously-authenticated
    CLI session. We also drop any in-flight pty login session — its
    credential context is stale once the file is gone.
    """
    claude_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if not claude_dir:
        raise HTTPException(
            400,
            detail=(
                "CLAUDE_CONFIG_DIR is not set, so there is no persisted "
                "credentials file to delete."
            ),
        )
    cred_file = Path(claude_dir) / ".credentials.json"
    deleted = False
    try:
        cred_file.unlink()
        deleted = True
    except FileNotFoundError:
        pass
    except OSError as e:
        raise HTTPException(500, detail=f"could not delete {cred_file}: {e}")
    # Drop any in-flight pty login session — its credential context is
    # tied to the old account, no point keeping it warm.
    from server import claude_login as _cl
    await _cl.cancel_all_sessions()
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "claude_auth_cleared",
        "path": str(cred_file),
        "actor": actor,
    })
    logger.info(
        "claude auth cleared at %s (deleted=%s, actor=%s)",
        cred_file, deleted, actor,
    )
    return {
        "ok": True,
        "path": str(cred_file),
        "deleted": deleted,
        "credentials_present": False,
    }


# ------------------------------------------------------------------
# Claude Code in-app OAuth login. Drives `claude /login` as a pty
# subprocess inside the container so the operator can complete the
# OAuth dance from the harness UI without shelling in or running the
# CLI on a separate laptop. Three-step flow:
#   1. POST /api/auth/claude/login/start  → returns {session_id, url}
#   2. (User opens URL in browser, authorizes, copies code)
#   3. POST /api/auth/claude/login/submit → {ok: true} once the CLI
#      writes .credentials.json
# Cancel + reaper handle teardown. POSIX-only; Windows hosts get a
# 501 + paste-fallback hint. See server/claude_login.py.
# ------------------------------------------------------------------
@app.post("/api/auth/claude/login/start", dependencies=[Depends(require_token)])
async def claude_login_start(
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    from server import claude_login as _cl
    if sys.platform == "win32":
        raise HTTPException(
            501,
            detail=(
                "pty-driven login is POSIX-only — this harness is running on "
                "a Windows host. Use the paste-credentials fallback below."
            ),
        )
    if not os.environ.get("CLAUDE_CONFIG_DIR", "").strip():
        raise HTTPException(
            400,
            detail=(
                "CLAUDE_CONFIG_DIR is not set, so the CLI's tokens would have "
                "nowhere durable to land. Set it to a persistent path "
                "(e.g. /data/claude) and redeploy."
            ),
        )
    try:
        result = await _cl.start_login()
    except RuntimeError as e:
        raise HTTPException(502, detail=str(e))
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "claude_login_started",
        "actor": actor,
    })
    return result  # {session_id, url}


@app.post("/api/auth/claude/login/submit", dependencies=[Depends(require_token)])
async def claude_login_submit(
    payload: dict[str, Any] = Body(...),
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    from server import claude_login as _cl
    sid = str(payload.get("session_id") or "").strip()
    code = str(payload.get("code") or "").strip()
    if not sid or not code:
        raise HTTPException(400, detail="session_id and code are required")
    try:
        result = await _cl.submit_code(sid, code)
    except RuntimeError as e:
        raise HTTPException(400, detail=str(e))
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "claude_login_completed",
        "actor": actor,
    })
    return result  # {ok: true}


@app.post("/api/auth/claude/login/cancel", dependencies=[Depends(require_token)])
async def claude_login_cancel(
    payload: dict[str, Any] = Body(...),
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    from server import claude_login as _cl
    sid = str(payload.get("session_id") or "").strip()
    await _cl.cancel_login(sid)
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "claude_login_cancelled",
        "actor": actor,
    })
    return {"ok": True}


# ------------------------------------------------------------------
# Codex in-app OAuth login. Drives `codex login --device-auth` as a
# plain subprocess (no pty — Codex stdout is clean ASCII). Device-code
# flow: UI shows URL + code, user opens URL and types code there, no
# submit step back to the harness. Completion detected by polling
# $CODEX_HOME/auth.json mtime in a background task.
#   1. POST /api/auth/codex/login/start  → {session_id, url, device_code}
#   2. (User opens URL, enters device code at OpenAI's page)
#   3. Bus event codex_login_completed when auth.json lands
#   4. POST /api/auth/codex/login/cancel  → {ok: true}
# See server/codex_login.py.
# ------------------------------------------------------------------

@app.post("/api/auth/codex/login/start", dependencies=[Depends(require_token)])
async def codex_login_start(
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    from server import codex_login as _cdl
    if sys.platform == "win32":
        raise HTTPException(
            501,
            detail=(
                "Device-code login is POSIX-only — this harness is running on "
                "a Windows host. Use the paste-auth.json fallback instead."
            ),
        )
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if not codex_home:
        raise HTTPException(
            400,
            detail=(
                "CODEX_HOME is not set, so the CLI's auth.json would have "
                "nowhere durable to land. Set it to a persistent path "
                "(e.g. /data/codex) and redeploy."
            ),
        )
    if not Path(codex_home).is_dir():
        raise HTTPException(
            400,
            detail=(
                f"CODEX_HOME directory does not exist: {codex_home}. "
                "Create it on the persistent volume and redeploy."
            ),
        )
    try:
        import shutil
        if not shutil.which("codex"):
            raise HTTPException(
                502,
                detail=(
                    "codex binary not found on PATH inside the container. "
                    "Verify the Dockerfile installs @openai/codex via npm."
                ),
            )
    except HTTPException:
        raise
    except Exception:
        pass
    try:
        result = await _cdl.start_login()
    except RuntimeError as e:
        raise HTTPException(502, detail=str(e))
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "codex_login_started",
        "actor": actor,
    })
    return result  # {session_id, url, device_code}


@app.post("/api/auth/codex/login/cancel", dependencies=[Depends(require_token)])
async def codex_login_cancel(
    payload: dict[str, Any] = Body(...),
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    from server import codex_login as _cdl
    sid = str(payload.get("session_id") or "").strip()
    await _cdl.cancel_login(sid)
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "codex_login_cancelled",
        "actor": actor,
    })
    return {"ok": True}


@app.delete("/api/auth/codex", dependencies=[Depends(require_token)])
async def delete_codex_auth(
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Wipe $CODEX_HOME/auth.json. Also cancels any in-flight login sessions.
    deleted=False (not an error) when the file already didn't exist.
    """
    from server import codex_login as _cdl
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if not codex_home:
        raise HTTPException(
            400,
            detail=(
                "CODEX_HOME is not set, so there is no persisted auth file "
                "to delete."
            ),
        )
    auth_file = Path(codex_home) / "auth.json"
    deleted = False
    try:
        auth_file.unlink()
        deleted = True
    except FileNotFoundError:
        pass
    except OSError as e:
        raise HTTPException(500, detail=f"could not delete {auth_file}: {e}")
    await _cdl.cancel_all_sessions()
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "codex_auth_cleared",
        "path": str(auth_file),
        "actor": actor,
    })
    logger.info(
        "codex auth cleared at %s (deleted=%s, actor=%s)",
        auth_file, deleted, actor,
    )
    return {
        "ok": True,
        "path": str(auth_file),
        "deleted": deleted,
        "credentials_present": False,
    }


@app.post("/api/auth/codex", dependencies=[Depends(require_token)])
async def set_codex_auth(
    payload: dict[str, Any] = Body(...),
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Paste-fallback: accept a raw auth.json blob and write it to
    $CODEX_HOME/auth.json. For operators who can't use the device-code
    flow (e.g. running on Windows, or the UI flow failed).

    Body: {"auth_json": "<raw JSON string>"} OR {"auth": {...object...}}.
    """
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if not codex_home:
        raise HTTPException(
            400,
            detail=(
                "CODEX_HOME is not set, so auth.json has nowhere durable "
                "to land."
            ),
        )
    raw = payload.get("auth_json")
    parsed_obj = payload.get("auth")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed_obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise HTTPException(400, detail=f"auth_json is not valid JSON: {e}")
    if not isinstance(parsed_obj, dict):
        raise HTTPException(
            400,
            detail="Provide either `auth_json` (string) or `auth` (object).",
        )
    target_dir = Path(codex_home)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HTTPException(500, detail=f"could not create {codex_home}: {e}")
    target_file = target_dir / "auth.json"
    try:
        tmp = target_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(parsed_obj, indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(target_file)
    except OSError as e:
        raise HTTPException(500, detail=f"write failed: {e}")
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "codex_auth_pasted",
        "path": str(target_file),
        "actor": actor,
    })
    logger.info("codex auth written to %s (actor=%s)", target_file, actor)
    return {
        "ok": True,
        "path": str(target_file),
        "credentials_present": True,
    }


@app.get("/api/status", dependencies=[Depends(require_token)])
async def status() -> dict[str, object]:
    # Import lazily: avoids pulling these private names into the module
    # surface and keeps the import graph unchanged for non-status paths.
    from server.agents import _running_tasks
    from server.events import bus

    now = datetime.now(timezone.utc)
    team_today = await _today_spend()
    running_slots = [aid for aid, t in _running_tasks.items() if not t.done()]
    return {
        "ok": True,
        "version": app.version,
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": int((now - STARTED_AT).total_seconds()),
        "host": os.environ.get("HOSTNAME", "unknown"),
        "paused": is_paused(),
        "running_slots": running_slots,
        "ws_subscribers": bus.subscriber_count,
        "caps": {
            "agent_daily_usd": AGENT_DAILY_CAP_USD,
            "team_daily_usd": TEAM_DAILY_CAP_USD,
            "team_today_usd": round(team_today, 4),
        },
        "webdav": {
            "enabled": webdav.enabled,
            "reason": webdav.reason,
            "url": webdav.url,
        },
        "workspaces": await get_workspaces_status(),
    }


# ------------------------------------------------------------------
# Agents
# ------------------------------------------------------------------


@app.get("/api/agents", dependencies=[Depends(require_token)])
async def list_agents() -> dict[str, list[dict[str, Any]]]:
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        # JOIN agents with the active-project identity + session rows
        # so the UI sees this project's name/role/brief/session_id.
        cur = await c.execute(
            "SELECT a.id, a.kind, "
            "       r.name AS name, r.role AS role, r.brief AS brief, "
            "       r.model_override AS model_override, "
            "       r.effort_override AS effort_override, "
            "       r.plan_mode_override AS plan_mode_override, "
            "       r.thinking_override AS thinking_override, "
            "       a.status, a.current_task_id, a.model, a.workspace_path, "
            "       s.session_id AS session_id, "
            "       s.codex_thread_id AS codex_thread_id, "
            "       a.cost_estimate_usd, a.started_at, a.last_heartbeat, a.locked, "
            "       a.runtime_override "
            "FROM agents a "
            "LEFT JOIN agent_project_roles r "
            "  ON r.slot = a.id AND r.project_id = ? "
            "LEFT JOIN agent_sessions s "
            "  ON s.slot = a.id AND s.project_id = ? "
            "ORDER BY CASE a.kind WHEN 'coach' THEN 0 ELSE 1 END, a.id",
            (project_id, project_id),
        )
        rows = await cur.fetchall()
    finally:
        await c.close()
    return {"agents": [dict(r) for r in rows]}


@app.post("/api/agents/start", dependencies=[Depends(require_token)])
async def start_agent(
    req: StartAgentRequest, background: BackgroundTasks
) -> dict[str, object]:
    background.add_task(
        run_agent,
        req.agent_id,
        req.prompt,
        model=req.model,
        plan_mode=req.plan_mode,
        effort=req.effort,
        thinking=req.thinking,
    )
    return {"ok": True, "agent_id": req.agent_id}


class PauseRequest(BaseModel):
    paused: bool


@app.get("/api/pause", dependencies=[Depends(require_token)])
async def get_pause_state() -> dict[str, bool]:
    """Read the global pause flag. In-memory; restarts clear it."""
    return {"paused": is_paused()}


@app.post("/api/pause", dependencies=[Depends(require_token)])
async def set_pause_state(req: PauseRequest) -> dict[str, bool]:
    """Flip the global pause flag. When paused:
    - run_agent rejects new starts and emits 'paused' events
    - the Coach autoloop skips its ticks
    - in-flight turns are NOT cancelled (use /cancel for that)
    """
    was = is_paused()
    set_paused(req.paused)
    if was != req.paused:
        await bus.publish(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "agent_id": "human",
                "type": "pause_toggled",
                "paused": req.paused,
            }
        )
    return {"paused": is_paused()}


# Recurrence v2 (Docs/recurrence-specs.md): the legacy
# `/api/coach/loop` and `/api/coach/repeat` endpoints + their
# `CoachLoopRequest` / `CoachRepeatRequest` bodies were removed in
# phase 8. Recurring tick → `PUT /api/coach/tick` (body
# `{minutes, enabled}`); recurring custom prompts → POST/PATCH
# /api/recurrences with `kind: "repeat"`. Manual one-off tick remains
# `POST /api/coach/tick`.


# --- Recurrences (Docs/recurrence-specs.md §9) -----------------------


class RecurrenceCreateRequest(BaseModel):
    kind: str = Field(..., pattern="^(repeat|cron)$")
    cadence: str
    prompt: str
    tz: str | None = None


class RecurrencePatchRequest(BaseModel):
    cadence: str | None = None
    prompt: str | None = None
    tz: str | None = None
    enabled: bool | None = None


class CoachTickPutRequest(BaseModel):
    # ge=0 enables the "fire continuously when Coach is idle" mode
    # introduced in recurrence-specs.md §2.
    minutes: int | None = Field(default=None, ge=0, le=525_600)
    enabled: bool | None = None


@app.get("/api/recurrences", dependencies=[Depends(require_token)])
async def list_recurrences_endpoint(
    actor: dict = Depends(audit_actor),
) -> list[dict[str, object]]:
    """Return all recurrence rows for the active project. Disabled
    rows are included so the UI can show 'turned off' state."""
    from server.recurrences import list_recurrences
    project_id = await resolve_active_project()
    return await list_recurrences(project_id)


@app.post("/api/recurrences", dependencies=[Depends(require_token)])
async def create_recurrence_endpoint(
    req: RecurrenceCreateRequest,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Create a repeat or cron recurrence for the active project."""
    from server.recurrences import create_recurrence
    project_id = await resolve_active_project()
    try:
        row = await create_recurrence(
            project_id=project_id,
            kind=req.kind,
            cadence=req.cadence,
            prompt=req.prompt,
            tz=req.tz,
            created_by=actor.get("source", "human"),
            actor=actor,
        )
    except PermissionError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return row


async def _ensure_active_project_recurrence(rec_id: int) -> dict:
    """Enforce the project-scoping invariant: PATCH/DELETE on a
    recurrence row must target the active project. Otherwise a
    cross-project mutation could fire scheduled work against the
    wrong project (or silently disable rows the operator can't see
    in the pane). Returns the row dict for the caller's reuse;
    raises HTTPException on miss / mismatch."""
    from server.recurrences import get_recurrence
    active = await resolve_active_project()
    row = await get_recurrence(rec_id)
    if row is None:
        raise HTTPException(404, detail="recurrence not found")
    if row["project_id"] != active:
        raise HTTPException(
            404,
            detail=(
                "recurrence belongs to a different project; "
                "switch to that project to edit it"
            ),
        )
    return row


@app.patch(
    "/api/recurrences/{rec_id}", dependencies=[Depends(require_token)]
)
async def patch_recurrence_endpoint(
    rec_id: int, req: RecurrencePatchRequest,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Update a recurrence's cadence / prompt / tz / enabled. Pass
    only the fields you want changed."""
    await _ensure_active_project_recurrence(rec_id)
    from server.recurrences import update_recurrence
    try:
        row = await update_recurrence(
            rec_id,
            cadence=req.cadence,
            prompt=req.prompt,
            tz=req.tz,
            enabled=req.enabled,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(404, detail="recurrence not found")
    return row


@app.delete(
    "/api/recurrences/{rec_id}", dependencies=[Depends(require_token)]
)
async def delete_recurrence_endpoint(
    rec_id: int, actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    await _ensure_active_project_recurrence(rec_id)
    from server.recurrences import delete_recurrence
    ok = await delete_recurrence(rec_id, actor=actor)
    if not ok:
        raise HTTPException(404, detail="recurrence not found")
    return {"ok": True, "id": rec_id}


@app.put("/api/coach/tick", dependencies=[Depends(require_token)])
async def put_coach_tick(
    req: CoachTickPutRequest, actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Set the recurring tick interval (minutes). Body shapes:
      * ``{"minutes": 60}`` — set or update interval; auto-enables.
      * ``{"enabled": false}`` — disable the tick (preserves the row).
      * Both — set and toggle in one call.
    """
    from server.recurrences import upsert_tick
    if req.minutes is None and req.enabled is None:
        raise HTTPException(400, detail="must pass minutes or enabled")
    project_id = await resolve_active_project()
    try:
        row = await upsert_tick(
            project_id=project_id,
            minutes=req.minutes,
            enabled=req.enabled,
            created_by=actor.get("source", "human"),
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    if row is None:
        return {"ok": True, "row": None}
    return {"ok": True, "row": row}


@app.post("/api/coach/tick", dependencies=[Depends(require_token)])
async def coach_tick(background: BackgroundTasks) -> dict[str, object]:
    """Nudge Coach to drain its inbox. Foundation of the autonomous
    loop — a cron or background task can hit this at intervals.

    Rejects if Coach is already working (prevents tick stacking under
    load). Caller can retry on the next interval."""
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status FROM agents WHERE id = 'coach'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if row and dict(row)["status"] == "working":
        raise HTTPException(409, detail="coach is already working")
    # Phase 5 (recurrence-specs.md §4): manual ticks use the same smart
    # composer as the recurrence scheduler, so the priority order is
    # consistent across both fire paths.
    from server.recurrences import compose_tick_prompt
    project_id = await resolve_active_project()
    tick_prompt = await compose_tick_prompt(project_id)
    background.add_task(run_agent, "coach", tick_prompt)
    return {"ok": True, "prompt": tick_prompt}


@app.post("/api/agents/cancel-all", dependencies=[Depends(require_token)])
async def cancel_all_runs() -> dict[str, object]:
    """Cancel every currently-running agent. Returns the list of ids
    that were actually cancelled (finished tasks are skipped)."""
    cancelled = await cancel_all_agents()
    return {"ok": True, "cancelled": cancelled}


@app.post("/api/agents/{agent_id}/cancel", dependencies=[Depends(require_token)])
async def cancel_agent_run(agent_id: str) -> dict[str, object]:
    """Abort an in-flight SDK query. Returns 409 if the agent isn't
    currently running, 200 if the cancellation was delivered."""
    if not (
        agent_id == "coach"
        or (
            agent_id.startswith("p")
            and agent_id[1:].isdigit()
            and 1 <= int(agent_id[1:]) <= 10
        )
    ):
        raise HTTPException(400, detail=f"invalid agent_id '{agent_id}'")
    cancelled = await cancel_agent(agent_id)
    if not cancelled:
        raise HTTPException(409, detail="agent is not currently running")
    return {"ok": True, "agent_id": agent_id}


class AgentIdentityWrite(BaseModel):
    name: str | None = Field(None, description="Short display name; '' clears.")
    role: str | None = Field(None, description="One-line role tag shown in the pane header; '' clears.")


@app.put("/api/agents/{agent_id}/identity", dependencies=[Depends(require_token)])
async def set_agent_identity(
    agent_id: str,
    req: AgentIdentityWrite,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Human upsert for name + role. Either field omitted → left alone;
    passed as empty string → cleared (column set to NULL). Emits
    'player_assigned' with auto:false so the UI refreshes live."""
    if not (agent_id == "coach" or (agent_id.startswith("p") and agent_id[1:].isdigit() and 1 <= int(agent_id[1:]) <= 10)):
        raise HTTPException(400, detail=f"invalid agent_id '{agent_id}'")
    # Same single-line normalization coord_set_player_role uses — these
    # fields render inline in the pane header, any newlines break layout.
    def _single_line(s: str | None) -> str | None:
        return " ".join(s.split()).strip() if s else (None if s is None else "")
    name_arg: str | None = None
    role_arg: str | None = None
    update_name = False
    update_role = False
    if req.name is not None:
        name = _single_line(req.name) or ""
        if len(name) > 60:
            raise HTTPException(400, detail="name too long (max 60 chars)")
        name_arg = name if name else None
        update_name = True
    if req.role is not None:
        role = _single_line(req.role) or ""
        if len(role) > 120:
            raise HTTPException(400, detail="role too long (max 120 chars)")
        role_arg = role if role else None
        update_role = True
    if not (update_name or update_role):
        return {"ok": True, "agent_id": agent_id, "changed": 0}
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,))
        if not await cur.fetchone():
            raise HTTPException(404, detail=f"agent {agent_id} not found")
        # Upsert per-project identity. INSERT covers fields the caller
        # provided; ON CONFLICT updates only those fields, leaving
        # untouched ones intact so a partial PUT (just name, just role)
        # behaves correctly.
        await c.execute(
            "INSERT INTO agent_project_roles (slot, project_id, name, role) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(slot, project_id) DO UPDATE SET "
            + ", ".join(
                clause for clause, keep in (
                    ("name = excluded.name", update_name),
                    ("role = excluded.role", update_role),
                ) if keep
            ),
            (agent_id, project_id, name_arg, role_arg),
        )
        changed = 1
        await c.commit()
    finally:
        await c.close()
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "type": "player_assigned",
            "name": req.name,
            "role": req.role,
            "auto": False,
            "actor": actor,
        }
    )
    return {"ok": True, "agent_id": agent_id, "changed": changed}


class AgentBriefWrite(BaseModel):
    brief: str = Field(..., description="Free-form context text; empty string clears.")


@app.put("/api/agents/{agent_id}/brief", dependencies=[Depends(require_token)])
async def set_agent_brief(
    agent_id: str,
    req: AgentBriefWrite,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Human-supplied context text for a specific agent. Appended to
    the agent's system prompt on every subsequent turn so you can give
    Coach goals / house style, or a Player domain context, without
    touching the global CLAUDE.md / skills / rules.

    Empty string clears the brief.
    """
    if not (agent_id == "coach" or (agent_id.startswith("p") and agent_id[1:].isdigit() and 1 <= int(agent_id[1:]) <= 10)):
        raise HTTPException(400, detail=f"invalid agent_id '{agent_id}'")
    body = req.brief or ""
    if len(body) > 8000:
        raise HTTPException(400, detail=f"brief too long ({len(body)} chars, max 8000)")
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        # Verify slot exists in the global agents roster.
        cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,))
        if not await cur.fetchone():
            raise HTTPException(404, detail=f"agent {agent_id} not found")
        # Upsert the per-project brief.
        await c.execute(
            "INSERT INTO agent_project_roles (slot, project_id, brief) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(slot, project_id) DO UPDATE SET brief = excluded.brief",
            (agent_id, project_id, body if body else None),
        )
        await c.commit()
    finally:
        await c.close()
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "type": "brief_updated",
            "size": len(body),
            "actor": actor,
        }
    )
    return {"ok": True, "agent_id": agent_id, "size": len(body)}


# Allowed extras the UI can toggle — kept narrow so the human can't
# paste an arbitrary tool name and get silently rejected by the SDK.
# Add to this list alongside any new safe-to-expose tool. These are
# SDK-native names (not role-baseline ones) that are OFF by default.
# Applied team-wide via /api/team/tools — one toggle, every agent.
_EXTRA_TOOL_WHITELIST = {"WebSearch", "WebFetch"}


class TeamToolsWrite(BaseModel):
    tools: list[str] = Field(
        default_factory=list,
        description="Extra SDK tool names to grant to every agent (empty = baseline only).",
    )


async def _read_team_extra_tools() -> list[str]:
    """Read team_config['extra_tools'] as a list. Empty on missing /
    malformed — the setter guarantees validity, so malformed should
    only happen from manual DB edits."""
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = 'extra_tools'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return []
    raw = dict(row).get("value") or ""
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [t for t in parsed if isinstance(t, str)]


@app.get("/api/team/tools", dependencies=[Depends(require_token)])
async def get_team_tools() -> dict[str, object]:
    """Return the team-wide extra-tools allowlist + the menu of
    toggleable tools so the UI can render checkboxes without a
    second round-trip."""
    return {
        "tools": await _read_team_extra_tools(),
        "available": sorted(_EXTRA_TOOL_WHITELIST),
    }


@app.get("/api/team/player_health", dependencies=[Depends(require_token)])
async def get_team_player_health() -> dict[str, object]:
    """v2 §15.3 — counters for the EnvPlayerHealthSection. Last 30
    days, active project. Empty `rows` when every Player's counters
    are zero so the UI hides the section."""
    from server.agents import compute_player_health_counters
    from server.db import resolve_active_project
    project_id = await resolve_active_project()
    rows = await compute_player_health_counters(project_id)
    return {"project_id": project_id, "rows": rows}


# Per-role defaults + allowlists are owned by `server.models_catalog`
# so `server.tools` can validate without a circular lazy import.
from server.models_catalog import (
    _CLAUDE_AVAILABLE,
    _CLAUDE_MODEL_WHITELIST,
    _CODEX_AVAILABLE,
    _CODEX_MODEL_WHITELIST,
    role_codex_defaults_concrete,
    role_defaults_concrete,
)


class TeamModelsWrite(BaseModel):
    coach: str = Field("", description="Default model for Coach. Empty = use hardcoded role default.")
    players: str = Field("", description="Default model for p1..p10. Empty = use hardcoded role default.")
    coach_codex: str = Field("", description="Default Codex model for Coach. Empty = use hardcoded role default.")
    players_codex: str = Field("", description="Default Codex model for p1..p10. Empty = use hardcoded role default.")


async def _read_team_config_str(key: str) -> str:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = ?", (key,)
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return ""
    raw = (dict(row).get("value") or "").strip()
    # Stored as JSON string ("claude-opus-4-7"); unwrap if so.
    if raw.startswith('"') and raw.endswith('"'):
        try:
            v = json.loads(raw)
            if isinstance(v, str):
                return v
        except Exception:
            pass
    return raw


async def _write_team_config_str(key: str, value: str) -> None:
    payload = json.dumps(value)
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO team_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "  updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (key, payload),
        )
        await c.commit()
    finally:
        await c.close()


@app.get("/api/team/models", dependencies=[Depends(require_token)])
async def get_team_models() -> dict[str, object]:
    """Return current per-role default models + the suggested defaults.

    The UI shows `suggested` as inline hints ("default: Opus 4.7") so
    users know what to revert to. Empty string means "SDK default"
    (whatever `DEFAULT_MODEL` env gives us)."""
    return {
        "coach": await _read_team_config_str("coach_default_model"),
        "players": await _read_team_config_str("players_default_model"),
        "coach_codex": await _read_team_config_str("coach_default_model_codex"),
        "players_codex": await _read_team_config_str("players_default_model_codex"),
        # Role-default dicts internally store tier aliases
        # (`latest_opus`, …) so they auto-track new model versions.
        # Resolve to concrete ids before returning so the UI hint
        # ("suggested: claude-opus-4-7") matches the dropdown options.
        "suggested": role_defaults_concrete(),
        "suggested_codex": role_codex_defaults_concrete(),
        # `available` exposes concrete ids only — humans pick versions
        # from the dropdown; tier aliases are an LLM-facing convenience
        # for `coord_set_player_model`.
        "available": _CLAUDE_AVAILABLE,
        "available_codex": _CODEX_AVAILABLE,
    }


class TeamRuntimesWrite(BaseModel):
    coach: str | None = Field(None, description="Coach default runtime: 'claude' | 'codex' | empty.")
    players: str | None = Field(None, description="Players default runtime: 'claude' | 'codex' | empty.")


@app.get("/api/team/runtimes", dependencies=[Depends(require_token)])
async def get_team_runtimes() -> dict[str, object]:
    """Return per-role default runtimes. Empty string means "fall
    through to the hardcoded 'claude' default" — the lowest tier of
    the resolution order described in CODEX_RUNTIME_SPEC.md §B.1.

    `codex_enabled` reflects HARNESS_CODEX_ENABLED so the UI can
    disable the Codex radio when the gate is off, instead of letting
    the user pick something the API will reject.
    """
    from server.runtimes import is_codex_enabled
    return {
        "coach": await _read_team_config_str("coach_default_runtime"),
        "players": await _read_team_config_str("players_default_runtime"),
        "codex_enabled": is_codex_enabled(),
    }


@app.put("/api/team/runtimes", dependencies=[Depends(require_token)])
async def set_team_runtimes(
    req: TeamRuntimesWrite,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Set per-role default runtimes. Empty clears (falls back to
    'claude'). `codex` is rejected with 400 when HARNESS_CODEX_ENABLED
    is unset — same gate as the per-slot endpoint."""
    from server.runtimes import is_codex_enabled
    clean: dict[str, str] = {}
    for role, value in (("coach", req.coach or ""), ("players", req.players or "")):
        normalized = (value or "").strip().lower()
        if normalized not in ("", "claude", "codex"):
            raise HTTPException(
                400,
                detail=f"runtime must be 'claude', 'codex', or empty — got {value!r} for {role}",
            )
        if normalized == "codex" and not is_codex_enabled():
            raise HTTPException(
                400,
                detail="Codex runtime is gated behind HARNESS_CODEX_ENABLED.",
            )
        clean[role] = normalized
        await _write_team_config_str(f"{role}_default_runtime", normalized)
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "team_runtimes_updated",
            "coach": clean["coach"],
            "players": clean["players"],
            "actor": actor,
        }
    )
    return {"ok": True, **clean}


@app.put("/api/team/models", dependencies=[Depends(require_token)])
async def set_team_models(
    req: TeamModelsWrite,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Set both per-role defaults. Empty string clears (reverts to SDK
    default)."""
    claude_values = (("coach", req.coach or ""), ("players", req.players or ""))
    codex_values = (
        ("coach", req.coach_codex or ""),
        ("players", req.players_codex or ""),
    )
    for role, value in claude_values:
        if value not in _CLAUDE_MODEL_WHITELIST:
            raise HTTPException(400, detail=f"unknown model '{value}' for {role}")
        await _write_team_config_str(f"{role}_default_model", value)
    for role, value in codex_values:
        if value not in _CODEX_MODEL_WHITELIST:
            raise HTTPException(400, detail=f"unknown Codex model '{value}' for {role}")
        await _write_team_config_str(f"{role}_default_model_codex", value)
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "team_models_updated",
            "coach": req.coach or "",
            "players": req.players or "",
            "coach_codex": req.coach_codex or "",
            "players_codex": req.players_codex or "",
            "actor": actor,
        }
    )
    return {
        "ok": True,
        "coach": req.coach or "",
        "players": req.players or "",
        "coach_codex": req.coach_codex or "",
        "players_codex": req.players_codex or "",
    }


@app.put("/api/team/tools", dependencies=[Depends(require_token)])
async def set_team_tools(
    req: TeamToolsWrite,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Replace the team-wide extra-tools allowlist. Applies to every
    agent on their next turn. Entries outside the whitelist are
    rejected — the UI only offers checkboxes we've vetted."""
    clean: list[str] = []
    seen: set[str] = set()
    for t in req.tools or []:
        if not isinstance(t, str) or t in seen:
            continue
        if t not in _EXTRA_TOOL_WHITELIST:
            raise HTTPException(400, detail=f"tool '{t}' not in extras whitelist")
        clean.append(t)
        seen.add(t)
    payload = json.dumps(clean)
    c = await configured_conn()
    try:
        # UPSERT — team_config.key is PK.
        await c.execute(
            "INSERT INTO team_config (key, value) VALUES ('extra_tools', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "  updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (payload,),
        )
        await c.commit()
    finally:
        await c.close()
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "team_tools_updated",
            "tools": clean,
            "actor": actor,
        }
    )
    return {"ok": True, "tools": clean}


# ------------------------------------------------------------------
# MCP external servers (DB-backed, editable from the Settings drawer)
# ------------------------------------------------------------------


def _normalize_mcp_paste(raw: str) -> dict[str, dict[str, Any]]:
    """Accept one of several paste shapes and return a dict of
    {server_name: config_dict}. Shapes supported:

      1. Claude-Desktop:   { "mcpServers": { "github": {...}, "notion": {...} } }
      2. Our file format:  { "servers":    { "github": {...}, "notion": {...} } }
      3. Flat single:      { "command": "npx", "args": [...], ... }   (name required separately)
      4. Bare named:       { "github": { "command": ..., ... } }

    For shapes (1) and (2) we return every defined server. For (3) the
    caller must supply the name. (4) is ambiguous — we treat it as (4)
    only when every value is a dict-with-command/url and no known
    reserved keys are present.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("paste is empty")
    try:
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("top-level must be a JSON object")

    # (1) Claude-Desktop
    if "mcpServers" in data and isinstance(data["mcpServers"], dict):
        return {n: c for n, c in data["mcpServers"].items() if isinstance(c, dict)}
    # (2) our format
    if "servers" in data and isinstance(data["servers"], dict):
        return {n: c for n, c in data["servers"].items() if isinstance(c, dict)}
    # (3) flat single — sentinel name includes '.' so it can never pass
    # _validate_mcp_name (which requires an ASCII identifier). The save
    # endpoint rewrites this before persisting.
    if "command" in data or "url" in data:
        return {"<<FLAT>>": data}
    # (4) bare named — validate all values look like configs
    looks_named = all(
        isinstance(v, dict) and ("command" in v or "url" in v)
        for v in data.values()
    )
    if looks_named and data:
        return {n: c for n, c in data.items() if isinstance(c, dict)}
    raise ValueError(
        "couldn't detect server config shape — expected 'mcpServers', 'servers', "
        "or a single {command/url, ...} object"
    )


# Strict ASCII identifier — matches what the SDK accepts for
# mcp__<server>__<tool> templating. str.isidentifier() accepts
# Unicode (café, 変数) which would fail downstream.
_MCP_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _validate_mcp_name(name: str) -> None:
    if not isinstance(name, str) or not _MCP_NAME_RE.match(name):
        raise HTTPException(
            400,
            detail=f"server name {name!r} must be ASCII: [A-Za-z_][A-Za-z0-9_]*, max 64 chars",
        )


def _merge_redacted_config(
    new_cfg: dict[str, Any], stored_cfg: dict[str, Any]
) -> dict[str, Any]:
    """Restore secrets from `stored_cfg` whenever `new_cfg` carries the
    redaction sentinel. Without this, round-tripping a config through
    GET (which masks env/headers values to `"***"` and URL userinfo via
    `_mask_repo_url`) → edit → PATCH would overwrite the user's stored
    token with the literal sentinel string. Only env/headers/url are
    redacted by `_redact_mcp_config`, so only those are merged back.
    """
    if not isinstance(new_cfg, dict):
        return {}
    if not isinstance(stored_cfg, dict):
        return new_cfg
    out: dict[str, Any] = {}
    for k, v in new_cfg.items():
        if k in ("env", "headers") and isinstance(v, dict):
            stored_section = stored_cfg.get(k)
            merged: dict[str, Any] = {}
            for kk, vv in v.items():
                if (
                    isinstance(vv, str)
                    and vv == "***"
                    and isinstance(stored_section, dict)
                    and kk in stored_section
                ):
                    merged[kk] = stored_section[kk]
                else:
                    merged[kk] = vv
            out[k] = merged
        elif k == "url" and isinstance(v, str):
            stored_url = stored_cfg.get("url")
            if isinstance(stored_url, str) and v == _mask_repo_url(stored_url):
                out[k] = stored_url
            else:
                out[k] = v
        else:
            out[k] = v
    return out


def _redact_mcp_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a server config with sensitive fields masked,
    so GET endpoints never leak stored tokens.

    - Any string value inside `env` / `headers` that isn't a pure
      `${VAR}` placeholder is redacted.
    - `url` is masked via _mask_repo_url (hides userinfo).
    - `command` / `args` / `type` pass through unchanged — they're
      public identifiers, not secrets.
    """
    if not isinstance(cfg, dict):
        return cfg
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        if k in ("env", "headers") and isinstance(v, dict):
            red: dict[str, Any] = {}
            for kk, vv in v.items():
                if isinstance(vv, str) and vv.strip().startswith("${") and vv.strip().endswith("}"):
                    red[kk] = vv
                else:
                    red[kk] = "***"
            out[k] = red
        elif k == "url" and isinstance(v, str):
            out[k] = _mask_repo_url(v)
        else:
            out[k] = v
    return out


def _load_mcp_row(name: str) -> dict[str, Any] | None:
    import sqlite3
    from server.db import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=2.0)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT name, config_json, allowed_tools_json, enabled, "
            "created_at, updated_at, last_ok, last_error, last_tested_at "
            "FROM mcp_servers WHERE name = ?",
            (name,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["config"] = json.loads(d.pop("config_json") or "{}")
    except Exception:
        d["config"] = {}
    try:
        d["allowed_tools"] = json.loads(d.pop("allowed_tools_json") or "[]")
    except Exception:
        d["allowed_tools"] = []
    d["enabled"] = bool(d.get("enabled"))
    d["last_ok"] = None if d.get("last_ok") is None else bool(d["last_ok"])
    return d


class MCPServerSave(BaseModel):
    paste: str = Field(..., description="Claude-Desktop / file-format / single-config JSON paste.")
    name: str | None = Field(
        default=None,
        description="Required if paste is a single flat config; optional otherwise (names come from the paste).",
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description="Bare tool names (no mcp__<name>__ prefix) to expose. Empty = none until you fill this in.",
    )
    enabled: bool = True
    allow_secrets: bool = Field(
        default=False,
        description="Pass true to override the inline-secret warning and save anyway.",
    )


@app.get("/api/mcp/servers", dependencies=[Depends(require_token)])
async def list_mcp_servers() -> dict[str, object]:
    """Return every row from mcp_servers, with last-test state."""
    import sqlite3
    from server.db import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=2.0)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT name, config_json, allowed_tools_json, enabled, "
            "created_at, updated_at, last_ok, last_error, last_tested_at "
            "FROM mcp_servers ORDER BY name"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            raw_cfg = json.loads(d.pop("config_json") or "{}")
        except Exception:
            raw_cfg = {}
        # Always redact before the wire — raw env values / URL userinfo
        # may contain a stored token. The plaintext config never leaves
        # the server.
        d["config"] = _redact_mcp_config(raw_cfg)
        try:
            d["allowed_tools"] = json.loads(d.pop("allowed_tools_json") or "[]")
        except Exception:
            d["allowed_tools"] = []
        d["enabled"] = bool(d.get("enabled"))
        d["last_ok"] = None if d.get("last_ok") is None else bool(d["last_ok"])
        out.append(d)
    return {"servers": out}


@app.post("/api/mcp/servers", dependencies=[Depends(require_token)])
async def save_mcp_server(
    req: MCPServerSave,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Parse a JSON paste and save one or more servers. Returns a
    summary including per-server secret warnings."""
    from server.mcp_config import detect_secrets
    try:
        parsed = _normalize_mcp_paste(req.paste)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))

    # Rename single-config pastes using the supplied name.
    if set(parsed.keys()) == {"<<FLAT>>"}:
        if not req.name:
            raise HTTPException(
                400,
                detail="paste is a single flat config — supply 'name' alongside it",
            )
        parsed = {req.name: parsed["<<FLAT>>"]}

    # Secret scan on the raw paste — flag BEFORE persisting.
    warnings = detect_secrets(req.paste)
    if warnings and not req.allow_secrets:
        raise HTTPException(
            400,
            detail={
                "secret_warnings": warnings,
                "hint": "Replace raw tokens with ${VAR} placeholders. "
                "Re-submit with allow_secrets=true to override.",
            },
        )

    # Persist each parsed server. allowed_tools applies to ALL servers
    # in the paste — callers wanting different lists per server should
    # save them one at a time.
    import sqlite3
    from server.db import DB_PATH
    saved: list[str] = []
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        for name, cfg in parsed.items():
            _validate_mcp_name(name)
            if not isinstance(cfg, dict):
                continue
            conn.execute(
                "INSERT INTO mcp_servers (name, config_json, allowed_tools_json, enabled) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "  config_json = excluded.config_json, "
                "  allowed_tools_json = excluded.allowed_tools_json, "
                "  enabled = excluded.enabled, "
                "  updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (
                    name,
                    json.dumps(cfg),
                    json.dumps(list(req.allowed_tools or [])),
                    1 if req.enabled else 0,
                ),
            )
            saved.append(name)
        conn.commit()
    finally:
        conn.close()
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "mcp_server_saved",
            "names": saved,
            "actor": actor,
        }
    )
    # MCP server list is captured at Codex app-server subprocess spawn
    # time. Drop every cached client so each agent's next turn rebuilds
    # the subprocess with the new server list. Idle slots get a clean
    # close; in-flight turns are cache-popped only (see evict_client).
    try:
        from server.runtimes.codex import evict_all_clients as _codex_evict_all
        await _codex_evict_all()
    except Exception:
        logger.exception("codex evict_all_clients failed after mcp save")
    return {"ok": True, "saved": saved, "secret_warnings": warnings}


class MCPServerPatch(BaseModel):
    enabled: bool | None = None
    allowed_tools: list[str] | None = None
    config_json: str | None = Field(
        default=None,
        description="Raw JSON string of the new config (flat: command/args/env or url/headers). Replaces the stored config; `***` sentinels in env/headers and masked URL userinfo are merged back from the existing stored value so an unrelated edit never overwrites a secret.",
    )
    allow_secrets: bool = Field(
        default=False,
        description="Pass true to override the inline-secret warning when patching the config.",
    )


@app.patch("/api/mcp/servers/{name}", dependencies=[Depends(require_token)])
async def patch_mcp_server(
    name: str,
    req: MCPServerPatch,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Toggle enabled, update the allowed_tools list, and/or replace
    the underlying config for an existing row. config_json edits run
    through the same secret-scan + redaction-merge as save."""
    from server.mcp_config import detect_secrets
    _validate_mcp_name(name)
    import sqlite3
    from server.db import DB_PATH
    updates: list[str] = []
    params: list[Any] = []
    if req.enabled is not None:
        updates.append("enabled = ?")
        params.append(1 if req.enabled else 0)
    if req.allowed_tools is not None:
        clean = [t for t in req.allowed_tools if isinstance(t, str) and t]
        updates.append("allowed_tools_json = ?")
        params.append(json.dumps(clean))
    merged_cfg: dict[str, Any] | None = None
    secret_warnings: list[str] = []
    pending_new_cfg: dict[str, Any] | None = None
    if req.config_json is not None:
        try:
            pending_new_cfg = json.loads(req.config_json)
        except Exception as e:
            raise HTTPException(400, detail=f"invalid config JSON: {e}")
        if not isinstance(pending_new_cfg, dict):
            raise HTTPException(400, detail="config must be a JSON object")
    # Single connection handles the probe (SELECT for redaction-merge)
    # and the UPDATE. Two back-to-back sqlite3.connect() calls against
    # the same file can leave the OS-level lock state in a way that
    # makes the second commit() raise OperationalError("database is
    # locked") under a still-running event loop, even after explicit
    # close() of the first.
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        # Existence check up front — issuing a write against a missing
        # row still acquires the SQLite write lock and races with
        # background readers on a busy harness. Surfacing 404 early
        # avoids the pointless lock.
        existing_row = conn.execute(
            "SELECT config_json FROM mcp_servers WHERE name = ?",
            (name,),
        ).fetchone()
        if existing_row is None:
            raise HTTPException(404, detail=f"server {name!r} not found")
        if pending_new_cfg is not None:
            stored_cfg: dict[str, Any] = {}
            if existing_row[0]:
                try:
                    parsed = json.loads(existing_row[0])
                    if isinstance(parsed, dict):
                        stored_cfg = parsed
                except Exception:
                    stored_cfg = {}
            merged_cfg = _merge_redacted_config(pending_new_cfg, stored_cfg)
            # Secret scan after merge so restored placeholders don't trip
            # the warning, but freshly-pasted raw tokens still do.
            secret_warnings = detect_secrets(json.dumps(merged_cfg))
            if secret_warnings and not req.allow_secrets:
                raise HTTPException(
                    400,
                    detail={
                        "secret_warnings": secret_warnings,
                        "hint": "Replace raw tokens with ${VAR} placeholders. "
                        "Re-submit with allow_secrets=true to override.",
                    },
                )
            updates.append("config_json = ?")
            params.append(json.dumps(merged_cfg))
        if not updates:
            raise HTTPException(400, detail="nothing to update")
        updates.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')")
        params.append(name)
        cur = conn.execute(
            f"UPDATE mcp_servers SET {', '.join(updates)} WHERE name = ?",
            params,
        )
        changed = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if changed == 0:
        raise HTTPException(404, detail=f"server {name!r} not found")
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "mcp_server_updated",
            "name": name,
            "config_changed": merged_cfg is not None,
            "actor": actor,
        }
    )
    try:
        from server.runtimes.codex import evict_all_clients as _codex_evict_all
        await _codex_evict_all()
    except Exception:
        logger.exception("codex evict_all_clients failed after mcp patch")
    return {"ok": True, "secret_warnings": secret_warnings}


@app.delete("/api/mcp/servers/{name}", dependencies=[Depends(require_token)])
async def delete_mcp_server(
    name: str,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    _validate_mcp_name(name)
    import sqlite3
    from server.db import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        cur = conn.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
        changed = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    if changed == 0:
        raise HTTPException(404, detail=f"server {name!r} not found")
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "mcp_server_deleted",
            "name": name,
            "actor": actor,
        }
    )
    try:
        from server.runtimes.codex import evict_all_clients as _codex_evict_all
        await _codex_evict_all()
    except Exception:
        logger.exception("codex evict_all_clients failed after mcp delete")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Encrypted secrets store — UI-managed values that feed into ${VAR}
# interpolation (MCP configs, provider tokens, etc). Plaintext exits the
# server only through the runtime interpolator — not through any API
# response. Master key lives in HARNESS_SECRETS_KEY; see server/secrets.py.
# ---------------------------------------------------------------------------

_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _validate_secret_name(name: str) -> None:
    if not isinstance(name, str) or not _SECRET_NAME_RE.match(name):
        raise HTTPException(
            400,
            detail=(
                f"secret name {name!r} must match env-var rules: "
                "[A-Za-z_][A-Za-z0-9_]*, max 64 chars"
            ),
        )


class SecretWriteRequest(BaseModel):
    value: str = Field(..., min_length=1, max_length=32_768)


@app.get("/api/secrets", dependencies=[Depends(require_token)])
async def list_secrets_endpoint() -> dict[str, object]:
    """List metadata only (no plaintext). Also surfaces master-key
    status so the UI can show 'store disabled — set HARNESS_SECRETS_KEY'
    when appropriate."""
    from server import secrets as secrets_store
    return {
        "status": secrets_store.status(),
        "secrets": await secrets_store.list_secrets(),
    }


@app.put("/api/secrets/{name}", dependencies=[Depends(require_token)])
async def write_secret(
    name: str,
    req: SecretWriteRequest,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Upsert an encrypted secret. 503 when the master key isn't
    configured (caller sees a clear error instead of a silent no-op)."""
    from server import secrets as secrets_store
    _validate_secret_name(name)
    key_status = secrets_store.status()
    if not key_status.get("ok"):
        raise HTTPException(
            503,
            detail=f"secrets store unavailable: {key_status.get('reason')}",
        )
    ok = await secrets_store.set_secret(name, req.value)
    if not ok:
        raise HTTPException(500, detail="secret write failed; see server logs")
    secrets_store.bump_cache_version()
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "secret_written",
            "name": name,
            "actor": actor,
        }
    )
    return {"ok": True, "name": name}


@app.delete("/api/secrets/{name}", dependencies=[Depends(require_token)])
async def delete_secret_endpoint(
    name: str,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    from server import secrets as secrets_store
    _validate_secret_name(name)
    ok = await secrets_store.delete_secret(name)
    if not ok:
        raise HTTPException(404, detail=f"secret {name!r} not found")
    secrets_store.bump_cache_version()
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "secret_deleted",
            "name": name,
            "actor": actor,
        }
    )
    return {"ok": True}


@app.post("/api/mcp/servers/{name}/test", dependencies=[Depends(require_token)])
async def test_mcp_server(name: str) -> dict[str, object]:
    """Smoke-test a saved server. For stdio: check the command
    resolves on $PATH. For http: HEAD the URL. Updates last_ok /
    last_error / last_tested_at on the row.

    This is NOT a full tool-discovery round-trip — that needs an MCP
    client we don't bundle yet. It catches the common mis-config
    modes (wrong npm package name, unreachable URL, typo in command)
    which accounts for most failures in practice."""
    _validate_mcp_name(name)
    row = _load_mcp_row(name)
    if row is None:
        raise HTTPException(404, detail=f"server {name!r} not found")
    cfg = row["config"]
    # Expand ${VAR} placeholders before probing — otherwise a URL of
    # "https://host/${TOKEN}" looks bogus.
    from server.mcp_config import _interpolate
    cfg = _interpolate(cfg)

    ok: bool = False
    detail: str = ""
    kind = (cfg.get("type") or "").lower()
    if not kind:
        # Infer from shape.
        if "command" in cfg:
            kind = "stdio"
        elif "url" in cfg:
            kind = "http"
    try:
        if kind == "stdio":
            command = cfg.get("command") or ""
            if not command:
                detail = "no 'command' in config"
            else:
                import shutil
                resolved = shutil.which(command)
                if resolved:
                    ok = True
                    detail = f"command found: {resolved}"
                else:
                    detail = f"command {command!r} not on PATH in this container"
        elif kind == "http":
            url = cfg.get("url") or ""
            if not url:
                detail = "no 'url' in config"
            else:
                import httpx
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        r = await client.request("OPTIONS", url)
                    # Any response at all (even a 4xx) proves the host
                    # is reachable — many MCP HTTP endpoints reject
                    # OPTIONS but exist.
                    ok = True
                    detail = f"reachable (HTTP {r.status_code})"
                except Exception as e:
                    detail = f"connection failed: {type(e).__name__}: {e}"
        else:
            detail = f"unknown server type {kind!r} (expected 'stdio' or 'http')"
    except Exception as e:
        detail = f"test crashed: {type(e).__name__}: {e}"

    # Persist result.
    import sqlite3
    from server.db import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        conn.execute(
            "UPDATE mcp_servers SET last_ok = ?, last_error = ?, "
            "last_tested_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE name = ?",
            (1 if ok else 0, None if ok else detail, name),
        )
        conn.commit()
    finally:
        conn.close()
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "mcp_server_tested",
            "name": name,
            "ok": ok,
        }
    )
    return {"ok": ok, "detail": detail}


# ------------------------------------------------------------------
# Repo URL helpers
# ------------------------------------------------------------------
#
# Repo URL is per-project: `projects.repo_url`. Editing happens via
# the existing per-project endpoints in `server/projects_api.py`
# (`PATCH /api/projects/{id}` + `POST /api/projects/{id}/repo/provision`).
# The legacy global `/api/team/repo*` endpoints were retired with the
# 2026-05-06 workspace refactor — see `Docs/workspace-refactor-plan.md`.
#
# `_mask_repo_url` survives because `workspaces.get_status` and the
# MCP-config redaction (line 2000) both still use it.


def _mask_repo_url(url: str) -> str:
    """Redact any userinfo component in the URL so the UI can show it
    without leaking a PAT. Preserves ${VAR} placeholders visibly."""
    if not url:
        return ""
    # Pattern: https://<userinfo>@host/path. Userinfo is everything
    # between "//" and the first "@" (bounded by the next "/").
    m = re.match(r"^(https?://)([^@/]+)@(.+)$", url)
    if not m:
        return url
    scheme, userinfo, rest = m.group(1), m.group(2), m.group(3)
    # Keep env placeholders readable; mask real tokens.
    if userinfo.startswith("${") and userinfo.endswith("}"):
        return f"{scheme}{userinfo}@{rest}"
    return f"{scheme}***@{rest}"


# Telegram bridge configuration. Token + chat-id whitelist live in the
# encrypted secrets store (UI-managed) with env fallback for first-boot.
# Save / clear here triggers a live bridge reload — no redeploy needed.

class TeamTelegramWrite(BaseModel):
    # Optional so the user can update one field without re-typing the
    # other. None = leave existing value untouched. Empty string = clear.
    token: str | None = Field(default=None, max_length=512)
    chat_ids: str | None = Field(default=None, max_length=2048)


# ----------------------------------------------------------------
# Codex auth (PR 5+, audit-item-5).
#
# Two auth sources are supported by CodexRuntime, in resolution order:
#   1. ChatGPT session — file at $CODEX_HOME/auth.json, set via
#      `codex login` inside the container. Read-only via API; the
#      ChatGPT device-code flow can't be driven through HTTP.
#   2. OPENAI_API_KEY fallback — stored in the encrypted `secrets`
#      table under the name `openai_api_key`. Set via PUT below.
#
# All endpoints are loopback-token protected (HARNESS_TOKEN); the
# value plaintext is never returned. Mirrors the Telegram pattern.
# ----------------------------------------------------------------
CODEX_API_KEY_SECRET_NAME = "openai_api_key"


class TeamCodexWrite(BaseModel):
    api_key: str | None = Field(
        None,
        description="OPENAI_API_KEY for CodexRuntime fallback when no ChatGPT session is present. Use DELETE to clear.",
    )


@app.get("/api/team/codex", dependencies=[Depends(require_token)])
async def get_team_codex() -> dict[str, object]:
    """Return Codex auth status — never the API-key plaintext."""
    from server import secrets as secrets_store
    from server.runtimes import is_codex_enabled

    codex_dir = os.environ.get("CODEX_HOME", "").strip()
    auth_path = Path(codex_dir) / "auth.json" if codex_dir else None
    chatgpt_session_present = bool(
        auth_path and auth_path.exists() and auth_path.stat().st_size > 0
    )

    api_key_set = False
    try:
        api_key_set = bool(await secrets_store.get_secret(CODEX_API_KEY_SECRET_NAME))
    except Exception:
        # Secrets store may be unavailable (no master key); api_key_set
        # stays False. The status block below carries the diagnostic.
        pass

    if chatgpt_session_present:
        method = "chatgpt"
    elif api_key_set:
        method = "api_key"
    else:
        method = "none"

    return {
        "enabled": is_codex_enabled(),
        "config_dir": codex_dir,
        "chatgpt_session_present": chatgpt_session_present,
        "api_key_set": api_key_set,
        "method": method,
        "secrets_status": secrets_store.status(),
        "secret_name": CODEX_API_KEY_SECRET_NAME,
    }


@app.put("/api/team/codex", dependencies=[Depends(require_token)])
async def set_team_codex(
    req: TeamCodexWrite,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Save the OPENAI_API_KEY into the encrypted secrets table.

    Validation:
      - The key must be non-empty. Use DELETE to wipe.
      - Format check is intentionally light (`sk-` prefix) — OpenAI
        rotates the key shape periodically and we'd rather accept a
        legitimate new format than reject silently.
    """
    from server import secrets as secrets_store

    key_status = secrets_store.status()
    if not key_status.get("ok"):
        raise HTTPException(
            503,
            detail=f"secrets store unavailable: {key_status.get('reason')}",
        )

    if req.api_key is None:
        raise HTTPException(400, detail="api_key field required (use DELETE to clear)")
    api_key = req.api_key.strip()
    if not api_key:
        raise HTTPException(
            400,
            detail="api_key cannot be empty — use DELETE /api/team/codex to wipe",
        )
    if not api_key.startswith(("sk-", "sk_")):
        # Soft warning via 400 — saves users from pasting "sk_…" with
        # underscore typo or random text. Future-proof: also accept
        # `sk_`.
        raise HTTPException(
            400,
            detail=(
                "api_key does not look like an OpenAI key (expected "
                "'sk-...' prefix) — paste from platform.openai.com/api-keys"
            ),
        )

    ok = await secrets_store.set_secret(CODEX_API_KEY_SECRET_NAME, api_key)
    if not ok:
        raise HTTPException(500, detail="api_key write failed; see server logs")
    secrets_store.bump_cache_version()
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "team_codex_updated",
            "actor": actor,
        }
    )
    return {"ok": True}


@app.delete("/api/team/codex", dependencies=[Depends(require_token)])
async def clear_team_codex(
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Wipe the saved OPENAI_API_KEY. ChatGPT session (filesystem) is
    NOT touched — that lives at `$CODEX_HOME/auth.json` and is
    cleared by deleting that file inside the container."""
    from server import secrets as secrets_store
    await secrets_store.delete_secret(CODEX_API_KEY_SECRET_NAME)
    secrets_store.bump_cache_version()
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "team_codex_cleared",
            "actor": actor,
        }
    )
    return {"ok": True}


@app.post("/api/team/codex/test", dependencies=[Depends(require_token)])
async def test_team_codex(
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Smoke-test the saved API key by calling the cheapest Models
    list endpoint. Returns 200 + {ok, status, models} on success;
    400/401/etc. on failure with the upstream status surfaced.

    Skipped when only ChatGPT session is configured — there's no
    HTTP probe equivalent for that auth path. The user can still
    eyeball `auth.json` mtime / `/api/health codex_auth.method`.
    """
    from server import secrets as secrets_store
    api_key = await secrets_store.get_secret(CODEX_API_KEY_SECRET_NAME)
    if not api_key:
        raise HTTPException(
            400,
            detail="no API key saved — set one via PUT /api/team/codex first",
        )
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, detail=f"upstream unreachable: {exc}") from exc

    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "team_codex_tested",
            "status": resp.status_code,
            "actor": actor,
        }
    )
    if resp.status_code != 200:
        # Surface the upstream status to the caller so the UI can
        # render "401 — invalid key" vs "429 — rate limited" etc.
        raise HTTPException(
            resp.status_code if resp.status_code < 600 else 502,
            detail=f"OpenAI API rejected key: HTTP {resp.status_code}",
        )
    try:
        models = resp.json().get("data", [])
        sample = sorted({m.get("id") for m in models if m.get("id")})[:5]
    except Exception:
        sample = []
    return {"ok": True, "status": resp.status_code, "sample_models": sample}


@app.get("/api/team/telegram", dependencies=[Depends(require_token)])
async def get_team_telegram() -> dict[str, object]:
    """Return masked status — never the token plaintext. The chat IDs
    list IS visible (it's a whitelist, not a credential)."""
    from server import secrets as secrets_store
    from server.telegram import (
        SECRET_TOKEN_NAME,
        SECRET_CHAT_IDS_NAME,
        _read_token,
        _read_chat_ids,
        _read_disabled_flag,
        is_running,
    )
    token, token_source = await _read_token()
    chat_ids, chat_ids_source = await _read_chat_ids()
    disabled = await _read_disabled_flag()
    return {
        "token_set": bool(token),
        "token_source": token_source,
        "chat_ids": sorted(chat_ids),
        "chat_ids_source": chat_ids_source,
        "disabled": disabled,
        "bridge_running": is_running(),
        "secrets_status": secrets_store.status(),
        "env_token_set": bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()),
        "secret_names": {
            "token": SECRET_TOKEN_NAME,
            "chat_ids": SECRET_CHAT_IDS_NAME,
        },
    }


@app.put("/api/team/telegram", dependencies=[Depends(require_token)])
async def set_team_telegram(
    req: TeamTelegramWrite,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Upsert encrypted token and/or chat_ids, then reload the bridge
    so changes apply without a restart. 503 when the master key isn't
    configured (caller can't save into a disabled store).

    Validation:
      - `token`: when provided, must be non-empty and match the
        BotFather format `<digits>:<35+ urlsafe chars>`. Use
        DELETE to wipe the token.
      - `chat_ids`: when provided non-empty, must parse to ≥1 integer.
        Empty string clears the saved whitelist (env fallback may
        re-supply one).

    Saving any field also clears the `telegram_disabled` flag so a
    prior Clear doesn't keep the bridge off.
    """
    from server import secrets as secrets_store
    from server.telegram import (
        SECRET_TOKEN_NAME,
        SECRET_CHAT_IDS_NAME,
        _parse_chat_ids,
        _set_disabled_flag,
        is_valid_token,
        reload_telegram_bridge,
    )
    key_status = secrets_store.status()
    if not key_status.get("ok"):
        raise HTTPException(
            503,
            detail=f"secrets store unavailable: {key_status.get('reason')}",
        )

    if req.token is not None:
        token = req.token.strip()
        if not token:
            raise HTTPException(
                400,
                detail="token cannot be empty — use DELETE /api/team/telegram to wipe",
            )
        if not is_valid_token(token):
            raise HTTPException(
                400,
                detail=(
                    "token does not match BotFather format "
                    "(<digits>:<35+ urlsafe chars>) — copy-paste from "
                    "the bot's HTTP API token line"
                ),
            )

    if req.chat_ids is not None and req.chat_ids.strip():
        parsed = _parse_chat_ids(req.chat_ids)
        if not parsed:
            raise HTTPException(
                400,
                detail="chat_ids must be a comma-separated list of integers",
            )

    if req.token is not None:
        ok = await secrets_store.set_secret(
            SECRET_TOKEN_NAME, req.token.strip()
        )
        if not ok:
            raise HTTPException(500, detail="token write failed; see server logs")

    if req.chat_ids is not None:
        ids = req.chat_ids.strip()
        if ids:
            ok = await secrets_store.set_secret(SECRET_CHAT_IDS_NAME, ids)
            if not ok:
                raise HTTPException(500, detail="chat_ids write failed; see server logs")
        else:
            await secrets_store.delete_secret(SECRET_CHAT_IDS_NAME)

    # Saving any value re-enables the bridge — Clear is the only way to
    # set the disabled flag, and the user just clicked Save.
    await _set_disabled_flag(False)
    secrets_store.bump_cache_version()
    running = await reload_telegram_bridge()

    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "team_telegram_updated",
            "bridge_running": running,
            "actor": actor,
        }
    )
    return {"ok": True, "bridge_running": running}


@app.delete("/api/team/telegram", dependencies=[Depends(require_token)])
async def clear_team_telegram(
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Wipe both secrets, set the `telegram_disabled` flag, and stop
    the bridge. The flag overrides env-var fallback so the bridge
    stays off until the user explicitly Saves new config (which
    clears the flag)."""
    from server import secrets as secrets_store
    from server.telegram import (
        SECRET_TOKEN_NAME,
        SECRET_CHAT_IDS_NAME,
        _set_disabled_flag,
        reload_telegram_bridge,
    )
    await secrets_store.delete_secret(SECRET_TOKEN_NAME)
    await secrets_store.delete_secret(SECRET_CHAT_IDS_NAME)
    await _set_disabled_flag(True)
    secrets_store.bump_cache_version()
    running = await reload_telegram_bridge()
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "team_telegram_cleared",
            "bridge_running": running,
            "actor": actor,
        }
    )
    return {"ok": True, "bridge_running": running}


class AgentLockWrite(BaseModel):
    locked: bool = Field(..., description="True to lock the agent off from Coach orchestration.")


class AgentRuntimeWrite(BaseModel):
    runtime: str | None = Field(
        None,
        description=(
            "Slot-level runtime override: 'claude' or 'codex'. "
            "None / empty clears so role defaults apply."
        ),
    )


@app.put("/api/agents/{agent_id}/runtime", dependencies=[Depends(require_token)])
async def set_agent_runtime(
    agent_id: str,
    req: AgentRuntimeWrite,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Set the slot-level runtime override.

    Resolution at spawn time: this column → role default in
    `team_config` (`coach_default_runtime` / `players_default_runtime`,
    PR 6) → `'claude'`. Nullable so role defaults can apply.

    Mid-turn changes are rejected with 409 — the runtime is read at
    spawn time and switching mid-flight would leave the in-flight turn
    on the old runtime while subsequent turns use the new one. Cancel
    the turn first, then set.

    See Docs/CODEX_RUNTIME_SPEC.md §B.1 / §F.1.
    """
    if not (agent_id == "coach" or (agent_id.startswith("p") and agent_id[1:].isdigit() and 1 <= int(agent_id[1:]) <= 10)):
        raise HTTPException(400, detail=f"invalid agent_id '{agent_id}'")
    raw = (req.runtime or "").strip().lower()
    if raw == "":
        runtime_value: str | None = None
    elif raw in ("claude", "codex"):
        runtime_value = raw
    else:
        raise HTTPException(
            400,
            detail=f"runtime must be 'claude', 'codex', or empty (got {req.runtime!r})",
        )
    # PR 5 feature gate — reject 'codex' until HARNESS_CODEX_ENABLED is
    # set. Keeps the column accepting 'claude' / NULL on every deploy
    # while we ship the SDK plumbing in stages.
    if runtime_value == "codex":
        from server.runtimes import is_codex_enabled
        if not is_codex_enabled():
            raise HTTPException(
                400,
                detail=(
                    "Codex runtime is gated behind HARNESS_CODEX_ENABLED. "
                    "Set the env var on the deployment to enable."
                ),
            )
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT status FROM agents WHERE id = ?", (agent_id,)
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, detail=f"agent {agent_id} not found")
        current_status = dict(row).get("status")
        if current_status == "working":
            raise HTTPException(
                409,
                detail=f"agent {agent_id} is mid-turn — cancel first, then set runtime",
            )
        await c.execute(
            "UPDATE agents SET runtime_override = ? WHERE id = ?",
            (runtime_value, agent_id),
        )
        await c.commit()
    finally:
        await c.close()
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "type": "runtime_updated",
            "runtime_override": runtime_value,
            "actor": actor,
        }
    )
    return {"ok": True, "agent_id": agent_id, "runtime_override": runtime_value}


@app.post("/api/agents/{agent_id}/transfer-runtime", dependencies=[Depends(require_token)])
async def transfer_agent_runtime(
    agent_id: str,
    req: AgentRuntimeWrite,
    background: BackgroundTasks,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Switch an agent's runtime with continuity carried via /compact.

    Smarter sibling of `PUT /api/agents/{id}/runtime`. The plain PUT
    is a blunt column flip — the next turn on the new runtime starts
    with no memory of the prior conversation. This endpoint runs the
    standard compact summary on the CURRENT runtime first, then flips
    the column on success, so the new runtime's first turn picks up
    `continuity_note` in its system prompt.

    Flow:
    - 400 on invalid slot / runtime / unset HARNESS_CODEX_ENABLED
    - 409 if the agent is mid-turn (cancel first)
    - 200 + `noop=True` when target == currently-resolved runtime
    - 200 + `queued=False` when there's no prior session to carry
      forward — flips immediately, emits `runtime_updated` and a
      `session_transferred` with `note=no_prior_session`
    - 202 + `queued=True` otherwise — schedules a transfer-mode
      compact turn on the current runtime; on success the runtime
      flips and `session_transferred` fires. Watch the pane for
      `session_transfer_requested` then `session_transferred` (or
      `session_transfer_failed` if the compact returned no summary).

    See CLAUDE.md "Session transfer" + Docs/CODEX_RUNTIME_SPEC.md §E.7.
    """
    from server.agents import (
        _resolve_runtime_for,
        _get_session_id,
        _set_runtime_override,
        is_agent_running,
        run_agent,
        COMPACT_PROMPT,
    )

    if not _valid_slot(agent_id):
        raise HTTPException(400, detail=f"invalid agent_id '{agent_id}'")
    raw = (req.runtime or "").strip().lower()
    if raw not in ("claude", "codex"):
        raise HTTPException(
            400,
            detail=(
                "runtime must be 'claude' or 'codex' (transfer requires an "
                "explicit target — empty/clear is not meaningful here; use "
                "PUT /api/agents/{id}/runtime to clear the override)"
            ),
        )
    if raw == "codex":
        from server.runtimes import is_codex_enabled
        if not is_codex_enabled():
            raise HTTPException(
                400,
                detail=(
                    "Codex runtime is gated behind HARNESS_CODEX_ENABLED. "
                    "Set the env var on the deployment to enable."
                ),
            )

    if is_agent_running(agent_id):
        raise HTTPException(
            409,
            detail=(
                "agent is currently running — cancel the in-flight turn "
                "first, then transfer"
            ),
        )

    from_runtime = await _resolve_runtime_for(agent_id)
    if from_runtime == raw:
        return {
            "ok": True,
            "noop": True,
            "agent_id": agent_id,
            "runtime": raw,
        }

    # No prior session on the source runtime → nothing to compact, just
    # flip. Read the runtime-specific session column so we don't fire a
    # pointless compact turn.
    if from_runtime == "claude":
        prior = await _get_session_id(agent_id)
    else:
        from server.runtimes.codex import _get_codex_thread_id
        prior = await _get_codex_thread_id(agent_id)

    if not prior:
        await _set_runtime_override(agent_id, raw)
        ts_iso = datetime.now(timezone.utc).isoformat()
        await bus.publish(
            {
                "ts": ts_iso,
                "agent_id": agent_id,
                "type": "runtime_updated",
                "runtime_override": raw,
                "actor": actor,
            }
        )
        await bus.publish(
            {
                "ts": ts_iso,
                "agent_id": agent_id,
                "type": "session_transferred",
                "from_runtime": from_runtime,
                "to_runtime": raw,
                "note": "no_prior_session",
                "actor": actor,
            }
        )
        return {
            "ok": True,
            "queued": False,
            "agent_id": agent_id,
            "from_runtime": from_runtime,
            "to_runtime": raw,
        }

    # Prior session exists — schedule the transfer-mode compact. The
    # message handler in agents.py / runtimes/codex.py applies the
    # runtime flip after a successful compact and emits
    # `session_transferred` (or `session_transfer_failed` if the
    # compact yielded no summary).
    background.add_task(
        run_agent,
        agent_id,
        COMPACT_PROMPT,
        compact_mode=True,
        transfer_to_runtime=raw,
    )
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "type": "session_transfer_requested",
            "from_runtime": from_runtime,
            "to_runtime": raw,
            "actor": actor,
        }
    )
    return {
        "ok": True,
        "queued": True,
        "agent_id": agent_id,
        "from_runtime": from_runtime,
        "to_runtime": raw,
    }


# ----------------------------------------------------------------
# Coord MCP proxy endpoint — internal, loopback-only.
#
# Receives tool calls from `python -m server.coord_mcp` subprocesses
# (Codex runtime, PR 5+) and dispatches to the same in-process coord
# handlers ClaudeRuntime uses. Single source of truth for handler
# bodies — bus.publish + maybe_wake_agent stay in the main process
# where they have the in-process bus + scheduler. See
# Docs/CODEX_RUNTIME_SPEC.md §C.
#
# Auth: bearer token issued by `server.spawn_tokens.mint(caller_id)`
# at turn start; passed to the subprocess via env. The token resolves
# to caller_id server-side — body's caller_id is a sanity check only.
#
# Loopback gate: requests must originate from 127.0.0.1 / ::1. PR 4
# ships the endpoint dormant — Codex hasn't been wired in yet.
# ----------------------------------------------------------------
_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1",
    "::1",
    "localhost",
    # IPv4-mapped IPv6 — uvicorn under dual-stack hands this shape
    # back when the container is bound to ::. The remote side is
    # still loopback; the canonical form just differs.
    "::ffff:127.0.0.1",
})


def _is_loopback(client_host: str | None) -> bool:
    if not client_host:
        return False
    return client_host in _LOOPBACK_HOSTS


@app.post("/api/_coord/{tool_name}")
async def coord_proxy_call(
    tool_name: str,
    request: Request,
    body: dict = Body(...),
) -> dict[str, object]:
    """Dispatch a coord tool call from the proxy subprocess.

    Body shape: `{"caller_id": str, "args": dict}`. The body's
    `caller_id` must match the token-bound caller_id or we 403 — this
    closes the impersonation hole described in §C.4.
    """
    # 1. Loopback bind check — never reachable from outside the
    #    container even if a misconfigured proxy maps the port.
    client = request.client
    if not _is_loopback(client.host if client else None):
        raise HTTPException(403, detail="loopback only")

    # 2. Token gate — resolve token → bound caller_id.
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, detail="missing bearer token")
    token = auth.split(None, 1)[1].strip()
    from server.spawn_tokens import resolve as resolve_token
    bound_caller = resolve_token(token)
    if bound_caller is None:
        raise HTTPException(401, detail="invalid or expired token")

    # 3. Body sanity — caller_id must be a string and match the
    #    bound identity. Type-checking before equality so a JSON
    #    `caller_id: 42` doesn't squeak through equality with a
    #    coerced string somewhere upstream.
    body_caller = body.get("caller_id")
    if body_caller is not None and not isinstance(body_caller, str):
        raise HTTPException(400, detail="caller_id must be a string")
    if body_caller is not None and body_caller != bound_caller:
        raise HTTPException(
            403,
            detail=f"caller_id mismatch (token bound to {bound_caller!r}, body claims {body_caller!r})",
        )

    args = body.get("args") or {}
    if not isinstance(args, dict):
        raise HTTPException(400, detail="args must be a dict")

    # 4. Build the per-caller coord server and dispatch by name. This
    #    runs the same closures ClaudeRuntime uses in-process, so
    #    bus.publish / maybe_wake_agent fire on the main process'
    #    event bus exactly like a Claude turn.
    from server.tools import build_coord_server

    server = build_coord_server(bound_caller, include_proxy_metadata=True)
    handlers: dict = server.get("_handlers", {})
    handler = handlers.get(tool_name)
    if handler is None:
        raise HTTPException(404, detail=f"unknown coord tool: {tool_name}")
    try:
        result = await handler(args)
    except Exception as e:
        # Mirror the SDK's tool-error envelope: success=False, error
        # message in content. Keeps the proxy subprocess consistent
        # with in-process tool failures.
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "result": result}


@app.get("/api/_coord/_tools")
async def coord_proxy_tools(request: Request) -> dict[str, object]:
    """Tool catalog — returned to a proxy subprocess on tools/list.

    Loopback-only, no token (the catalog is non-sensitive — the same
    information is in the source). Lets `coord_mcp.py` declare the
    static tool list at MCP init time without re-hardcoding it.
    """
    client = request.client
    if not _is_loopback(client.host if client else None):
        raise HTTPException(403, detail="loopback only")
    from server.tools import coord_tool_names
    return {"tools": coord_tool_names()}


@app.put("/api/agents/{agent_id}/locked", dependencies=[Depends(require_token)])
async def set_agent_locked(
    agent_id: str,
    req: AgentLockWrite,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Set the per-agent lock flag.

    When locked:
      - Coach cannot coord_approve_stage onto this agent
      - Coach cannot coord_send_message this agent directly
      - coord_read_inbox skips messages whose sender is 'coach' (direct
        or broadcast)
      - The agent can still read all shared docs (memory / knowledge /
        context / decisions) and responds to human prompts normally.

    Locking Coach itself is a no-op semantically — Coach doesn't take
    orders from Coach — but we allow the flag for UI symmetry.
    """
    if not (agent_id == "coach" or (agent_id.startswith("p") and agent_id[1:].isdigit() and 1 <= int(agent_id[1:]) <= 10)):
        raise HTTPException(400, detail=f"invalid agent_id '{agent_id}'")
    locked = 1 if req.locked else 0
    c = await configured_conn()
    try:
        cur = await c.execute(
            "UPDATE agents SET locked = ? WHERE id = ?",
            (locked, agent_id),
        )
        changed = cur.rowcount
        await c.commit()
    finally:
        await c.close()
    if changed == 0:
        raise HTTPException(404, detail=f"agent {agent_id} not found")
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "type": "lock_updated",
            "locked": bool(locked),
            "actor": actor,
        }
    )
    return {"ok": True, "agent_id": agent_id, "locked": bool(locked)}


@app.get("/api/agents/{agent_id}/context", dependencies=[Depends(require_token)])
async def get_agent_context(
    agent_id: str,
    model: str | None = None,
) -> dict[str, object]:
    """Current context usage estimate for a slot: used tokens (from
    the latest per-assistant usage row in the active session jsonl),
    the effective model's context window, and the ratio. Used by the
    pane-level ContextBar to paint fill + colour. Returns zeros when
    there's no active session.

    The `model` query param is the pane-level override (kept
    client-side in localStorage); when provided, the window lookup
    matches what the SDK will actually use. Omit to get the default
    (currently 1M on Max). Unknown model ids also resolve to the
    default — keeps the bar useful when a new model ships before the
    table is updated."""
    if not (agent_id == "coach" or (agent_id.startswith("p") and agent_id[1:].isdigit() and 1 <= int(agent_id[1:]) <= 10)):
        raise HTTPException(400, detail=f"invalid agent_id '{agent_id}'")
    from server.agents import (
        _context_window_for,
        _codex_session_context_estimate,
        _session_context_estimate,
    )
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,))
        if not await cur.fetchone():
            raise HTTPException(404, detail=f"agent {agent_id} not found")
        # Read both session columns. ClaudeRuntime populates session_id
        # from ResultMessage; CodexRuntime populates codex_thread_id
        # after the first successful chat step. The estimator we run
        # depends on which is set.
        cur = await c.execute(
            "SELECT session_id, codex_thread_id FROM agent_sessions "
            "WHERE slot = ? AND project_id = ?",
            (agent_id, project_id),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    rec = dict(row) if row else {}
    session_id = rec.get("session_id")
    codex_thread_id = rec.get("codex_thread_id")
    used = 0
    if codex_thread_id:
        used = await _codex_session_context_estimate(codex_thread_id)
    elif session_id:
        used = await _session_context_estimate(session_id)
    resolved_model = (model or "").strip() or None
    # When the UI didn't pass a per-pane model override, fall back to
    # the model recorded on the latest turn for the active session.
    # Without this the window resolves to the global default (1M) for
    # every agent, which over-reports for Codex's 400K models — the
    # CTX bar would crawl up to 50% before tripping any UI signal.
    if not resolved_model:
        latest_session = codex_thread_id or session_id
        if latest_session:
            c2 = await configured_conn()
            try:
                cur2 = await c2.execute(
                    "SELECT model FROM turns WHERE session_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (latest_session,),
                )
                mrow = await cur2.fetchone()
            finally:
                await c2.close()
            if mrow:
                resolved_model = (dict(mrow).get("model") or "").strip() or None
    window = _context_window_for(resolved_model)
    ratio = used / window if window > 0 else 0.0
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "codex_thread_id": codex_thread_id,
        "used_tokens": used,
        "context_window": window,
        "model": resolved_model,
        "ratio": round(ratio, 4),
    }


@app.delete("/api/agents/{agent_id}/session", dependencies=[Depends(require_token)])
async def clear_session(
    agent_id: str,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Clear agent.session_id so the next run starts fresh context.

    Useful when an agent's conversation has drifted or when the human
    wants to start a new thread without losing task/memory state.
    """
    if not (agent_id == "coach" or (agent_id.startswith("p") and agent_id[1:].isdigit() and 1 <= int(agent_id[1:]) <= 10)):
        raise HTTPException(400, detail=f"invalid agent_id '{agent_id}'")
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        # Verify slot exists in the global agents roster.
        cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,))
        if not await cur.fetchone():
            raise HTTPException(404, detail=f"agent {agent_id} not found")
        # Drop the per-(slot, project) session row entirely.
        await c.execute(
            "DELETE FROM agent_sessions WHERE slot = ? AND project_id = ?",
            (agent_id, project_id),
        )
        await c.commit()
    finally:
        await c.close()
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "type": "session_cleared",
            "actor": actor,
        }
    )
    # Drop any cached Codex app-server subprocess for this slot so the
    # next turn spawns a fresh one (picking up current MCP server list,
    # env, etc). No-op for slots running the Claude runtime.
    try:
        from server.runtimes.codex import evict_client as _codex_evict
        await _codex_evict(agent_id)
    except Exception:
        logger.exception("codex evict_client failed for slot=%s", agent_id)
    return {"ok": True, "agent_id": agent_id}


@app.post("/api/agents/{agent_id}/compact", dependencies=[Depends(require_token)])
async def compact_agent_session(
    agent_id: str,
    background: BackgroundTasks,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Run a /compact-equivalent turn: ask the agent to summarize its
    current session, persist the summary to agents.continuity_note,
    and null session_id so the NEXT turn starts fresh with the
    summary injected into its system prompt.

    Returns 202 + {ok: true, queued: true} — the compact turn runs
    asynchronously, just like a normal /api/agents/start. Watch the
    pane for a session_compacted event to confirm completion. Returns
    409 if the agent is already running (you can't compact an in-
    flight turn).
    """
    from server.agents import run_agent, is_agent_running, COMPACT_PROMPT
    if not _valid_slot(agent_id):
        raise HTTPException(400, detail=f"invalid agent_id '{agent_id}'")
    if is_agent_running(agent_id):
        raise HTTPException(
            409,
            detail="agent is currently running — wait for it to finish or cancel first",
        )
    background.add_task(run_agent, agent_id, COMPACT_PROMPT, compact_mode=True)
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "type": "session_compact_requested",
            "actor": actor,
        }
    )
    return {"ok": True, "queued": True, "agent_id": agent_id}


class BatchClearSessionsRequest(BaseModel):
    agents: list[str] | None = Field(
        None,
        description="Slot ids to clear. Omit / empty = clear every agent.",
    )


def _valid_slot(sid: str) -> bool:
    return sid == "coach" or (
        sid.startswith("p") and sid[1:].isdigit() and 1 <= int(sid[1:]) <= 10
    )


@app.post("/api/agents/sessions/clear", dependencies=[Depends(require_token)])
async def clear_sessions_batch(
    req: BatchClearSessionsRequest,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Batch version of DELETE /api/agents/<id>/session. Clears
    session_id on every selected agent so next turn starts fresh.

    `agents` semantics:
    - key omitted / value null → all 11 slots
    - empty list []           → clear nothing (no-op; prevents an API
      consumer's empty allow-list from accidentally wiping everyone)
    - list of slot ids         → exactly those
    """
    if req.agents is None:
        targets = ["coach"] + [f"p{i}" for i in range(1, 11)]
    else:
        if len(req.agents) > 11:
            raise HTTPException(400, detail="too many agent ids (max 11)")
        targets = []
        for sid in req.agents:
            if not isinstance(sid, str) or not _valid_slot(sid):
                raise HTTPException(400, detail=f"invalid agent_id '{sid}'")
            if sid not in targets:
                targets.append(sid)
    if not targets:
        return {"ok": True, "cleared": []}
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        placeholders = ",".join("?" * len(targets))
        cur = await c.execute(
            f"DELETE FROM agent_sessions WHERE project_id = ? AND slot IN ({placeholders})",
            [project_id, *targets],
        )
        updated = cur.rowcount if cur.rowcount is not None else 0
        await c.commit()
    finally:
        await c.close()
    now = datetime.now(timezone.utc).isoformat()
    for sid in targets:
        await bus.publish(
            {
                "ts": now,
                "agent_id": sid,
                "type": "session_cleared",
                "actor": actor,
            }
        )
    # Same Codex subprocess eviction as the single-slot endpoint.
    try:
        from server.runtimes.codex import evict_client as _codex_evict
        for sid in targets:
            await _codex_evict(sid)
    except Exception:
        logger.exception("codex evict_client failed during batch session clear")
    return {"ok": True, "cleared": targets, "updated": updated}


# ------------------------------------------------------------------
# Tasks
# ------------------------------------------------------------------


_BACKLOG_DESCRIPTION_MAX = 8000


_BACKLOG_PRIORITIES = {"low", "normal", "high", "urgent"}


class BacklogCreateRequest(BaseModel):
    title: str
    description: str | None = None
    priority: str = "normal"


@app.post("/api/backlog", dependencies=[Depends(require_token)])
async def create_backlog_entry(req: BacklogCreateRequest) -> dict[str, Any]:
    """Human-facing backlog propose (kanban-specs-v2.md §4.0.2).

    Inserts a pending backlog entry attributed to 'human'.
    Coach triages via coord_triage_backlog on the next tick.
    """
    title = req.title.strip()
    if not title:
        raise HTTPException(400, detail="title is required")
    description: str | None = req.description.strip() if req.description else None
    description = description or None  # empty string → None
    if description is not None and len(description) > _BACKLOG_DESCRIPTION_MAX:
        raise HTTPException(
            400,
            detail=f"description exceeds {_BACKLOG_DESCRIPTION_MAX} char limit",
        )
    priority = (req.priority or "normal").strip().lower()
    if priority not in _BACKLOG_PRIORITIES:
        raise HTTPException(
            400,
            detail=f"priority must be one of {sorted(_BACKLOG_PRIORITIES)}",
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO backlog_tasks "
            "(title, description, proposed_by, proposed_at, priority) "
            "VALUES (?, ?, 'human', ?, ?)",
            (title, description, now_iso, priority),
        )
        backlog_id = cur.lastrowid
        await c.commit()
    finally:
        await c.close()

    event: dict[str, Any] = {
        "ts": now_iso,
        "agent_id": "human",
        "type": "backlog_task_proposed",
        "id": backlog_id,
        "title": title,
        "proposed_by": "human",
        "priority": priority,
        "description_present": description is not None,
    }
    if description is not None:
        event["description"] = description
    await bus.publish(event)
    return {
        "id": backlog_id, "title": title, "description": description,
        "priority": priority, "status": "pending",
    }


@app.get("/api/backlog", dependencies=[Depends(require_token)])
async def list_backlog(status: str | None = None) -> dict[str, Any]:
    """List backlog entries. Default: pending only.

    ?status=pending|promoted|rejected|all
    """
    valid_statuses = {"pending", "promoted", "rejected"}
    if status and status != "all" and status not in valid_statuses:
        raise HTTPException(
            400,
            detail=f"status must be one of: pending, promoted, rejected, all",
        )
    c = await configured_conn()
    try:
        if not status or status == "pending":
            cur = await c.execute(
                "SELECT * FROM backlog_tasks WHERE status='pending' "
                "ORDER BY proposed_at ASC"
            )
        elif status == "all":
            cur = await c.execute(
                "SELECT * FROM backlog_tasks ORDER BY proposed_at ASC"
            )
        else:
            cur = await c.execute(
                "SELECT * FROM backlog_tasks WHERE status=? "
                "ORDER BY proposed_at ASC",
                (status,),
            )
        rows = await cur.fetchall()
    finally:
        await c.close()
    return {"backlog": [dict(r) for r in rows]}


class BacklogUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: str | None = None


@app.patch("/api/backlog/{backlog_id}", dependencies=[Depends(require_token)])
async def update_backlog_entry(
    backlog_id: int,
    req: BacklogUpdateRequest,
    actor: dict = Depends(audit_actor),
) -> dict[str, Any]:
    """Edit the title, description, and/or priority of a pending backlog entry (kanban-specs-v2.md §4.0)."""
    # At least one field must be supplied.
    if req.title is None and req.description is None and req.priority is None:
        raise HTTPException(400, detail="at least one of title, description, or priority is required")

    if req.priority is not None and req.priority not in _BACKLOG_PRIORITIES:
        raise HTTPException(
            400,
            detail=f"priority must be one of {sorted(_BACKLOG_PRIORITIES)}",
        )

    new_title: str | None = req.title.strip() if req.title is not None else None
    if new_title is not None and not new_title:
        raise HTTPException(400, detail="title must not be blank")

    # description: None = unchanged; "" = clear (stored as NULL); non-empty = set.
    new_description: Any = _SENTINEL
    if req.description is not None:
        stripped = req.description.strip()
        if stripped and len(stripped) > _BACKLOG_DESCRIPTION_MAX:
            raise HTTPException(
                400,
                detail=f"description exceeds {_BACKLOG_DESCRIPTION_MAX} char limit",
            )
        new_description = stripped or None  # empty string → NULL

    now_iso = datetime.now(timezone.utc).isoformat()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, title, description, priority, status FROM backlog_tasks WHERE id = ?",
            (backlog_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, detail="backlog entry not found")
        row = dict(row)
        if row["status"] != "pending":
            raise HTTPException(
                409, detail=f"cannot edit backlog entry with status '{row['status']}'"
            )
        old_title = row["title"]

        # Build SET clauses for only the fields being changed.
        set_parts: list[str] = []
        params: list[Any] = []
        if new_title is not None:
            set_parts.append("title = ?")
            params.append(new_title)
        if new_description is not _SENTINEL:
            set_parts.append("description = ?")
            params.append(new_description)
        if req.priority is not None:
            set_parts.append("priority = ?")
            params.append(req.priority)
        params.append(backlog_id)
        await c.execute(
            f"UPDATE backlog_tasks SET {', '.join(set_parts)} WHERE id = ?",
            params,
        )
        # Re-read the final row so we return consistent values.
        cur2 = await c.execute(
            "SELECT id, title, description, priority FROM backlog_tasks WHERE id = ?",
            (backlog_id,),
        )
        final = dict(await cur2.fetchone())
        await c.commit()
    finally:
        await c.close()

    event: dict[str, Any] = {
        "ts": now_iso,
        "type": "backlog_entry_updated",
        "id": backlog_id,
        "old_title": old_title,
        "new_title": final["title"],
        "actor": actor,
        "description_present": final["description"] is not None,
    }
    if new_description is not _SENTINEL:
        event["new_description"] = new_description
    if req.priority is not None:
        event["new_priority"] = req.priority
    await bus.publish(event)
    return {
        "id": backlog_id,
        "title": final["title"],
        "description": final["description"],
        "priority": final["priority"],
    }


@app.delete("/api/backlog/{backlog_id}", dependencies=[Depends(require_token)])
async def delete_backlog_entry(
    backlog_id: int,
    actor: dict = Depends(audit_actor),
) -> dict[str, Any]:
    """Delete a pending backlog entry (kanban-specs-v2.md §4.0)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, title, status FROM backlog_tasks WHERE id = ?",
            (backlog_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, detail="backlog entry not found")
        row = dict(row)
        if row["status"] != "pending":
            raise HTTPException(
                409, detail=f"cannot delete backlog entry with status '{row['status']}'"
            )
        entry_title = row["title"]
        await c.execute("DELETE FROM backlog_tasks WHERE id = ?", (backlog_id,))
        await c.commit()
    finally:
        await c.close()

    await bus.publish({
        "ts": now_iso,
        "type": "backlog_entry_deleted",
        "id": backlog_id,
        "title": entry_title,
        "actor": actor,
    })
    return {"id": backlog_id, "deleted": True}


@app.get("/api/tasks", dependencies=[Depends(require_token)])
async def list_tasks(status: str | None = None, owner: str | None = None) -> dict[str, Any]:
    project_id = await resolve_active_project()
    where_parts: list[str] = ["project_id = ?"]
    params: list[Any] = [project_id]
    if status:
        where_parts.append("status = ?")
        params.append(status)
    if owner is not None:
        if owner.lower() in ("null", "none", "unassigned"):
            where_parts.append("owner IS NULL")
        else:
            where_parts.append("owner = ?")
            params.append(owner)
    clause = " WHERE " + " AND ".join(where_parts)

    c = await configured_conn()
    try:
        cur = await c.execute(
            f"SELECT * FROM tasks{clause} ORDER BY created_at DESC", params
        )
        rows = await cur.fetchall()
    finally:
        await c.close()
    return {"tasks": [dict(r) for r in rows]}


@app.post("/api/tasks", dependencies=[Depends(require_token)])
async def create_task_from_human(req: CreateTaskRequest) -> dict[str, Any]:
    """Create a top-level task from the UI (attributed to 'human').

    v0.3: routing is driven by the `trajectory` field (ordered list of
    `{stage, to}` objects). Defaults to `[{stage:'execute', to:[]}]`
    when omitted — Coach can add stages later via
    POST /api/tasks/{id}/trajectory.
    """
    from server.tools import _validate_trajectory
    task_id = f"t-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:8]}"
    parent_id = req.parent_id or None
    project_id = await resolve_active_project()
    tracking_reason = (
        req.tracking_reason.strip()
        if isinstance(req.tracking_reason, str) and req.tracking_reason.strip()
        else None
    )

    # Validate trajectory. v2.0.1 (2026-05-08): the legacy default of
    # [{stage:'execute', to:[]}] is gone — the caller must supply a
    # trajectory whose first stage names exactly one Player. Otherwise
    # the kanban would be polluted with undispatched tasks.
    if req.trajectory is None:
        raise HTTPException(
            400,
            detail=(
                "trajectory is required: pass a list of {stage, to, "
                "focus?} objects. trajectory[0].to must name exactly "
                "one Player (e.g. [{'stage':'execute','to':['p3']}])."
            ),
        )
    validated, err = _validate_trajectory(req.trajectory)
    if err:
        raise HTTPException(400, detail=f"invalid trajectory: {err}")
    assert validated is not None
    trajectory = validated
    trajectory_json = json.dumps(trajectory, separators=(",", ":"))
    now_iso = datetime.now(timezone.utc).isoformat()

    role_for_stage = {
        "plan": "planner",
        "execute": "executor",
        "audit_syntax": "auditor_syntax",
        "audit_semantics": "auditor_semantics",
        "ship": "shipper",
    }

    c = await configured_conn()
    try:
        if parent_id:
            cur = await c.execute(
                "SELECT id FROM tasks WHERE id = ? AND project_id = ?",
                (parent_id, project_id),
            )
            if (await cur.fetchone()) is None:
                raise HTTPException(404, detail=f"parent_id {parent_id} not found")
        # v0.3 audit fix: initial status = first stage in trajectory.
        initial_status = trajectory[0]["stage"]
        first_stage_to = trajectory[0].get("to") or []
        if isinstance(first_stage_to, str):
            first_stage_to = [first_stage_to] if first_stage_to else []
        initial_owner = (
            first_stage_to[0] if len(first_stage_to) == 1 else None
        )
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, description, "
            "parent_id, priority, workflow, tracking_reason, "
            "trajectory, status, owner, last_stage_change_at, "
            "created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'human')",
            (
                task_id, project_id, req.title, req.description,
                parent_id, req.priority, req.workflow,
                tracking_reason, trajectory_json,
                initial_status, initial_owner, now_iso,
            ),
        )
        # v2 §7.1: plant a role row ONLY for the first trajectory entry
        # AND only when its `to` is a single named slot (Coach pre-picked
        # via the trajectory). Subsequent stages' `to` lists are FYI
        # only — Coach drives them later via coord_approve_stage. Pool
        # / empty entries don't auto-plant; Coach picks at approval
        # time.
        first_entry = trajectory[0]
        first_to: list[str] = first_entry.get("to") or []
        planted_first_stage = False
        if len(first_to) == 1:
            first_role = role_for_stage[first_entry["stage"]]
            eligible_json = json.dumps(first_to, separators=(",", ":"))
            await c.execute(
                "INSERT INTO task_role_assignments "
                "(task_id, role, eligible_owners, owner, "
                "assigned_at, claimed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, first_role, eligible_json, first_to[0],
                 now_iso, now_iso),
            )
            planted_first_stage = True
        await c.commit()
    finally:
        await c.close()

    await bus.publish(
        {
            "ts": now_iso,
            "agent_id": "human",
            "type": "task_created",
            "task_id": task_id,
            "title": req.title,
            "parent_id": parent_id,
            "priority": req.priority,
            "workflow": req.workflow,
            "tracking_reason": tracking_reason,
            "trajectory": trajectory,
        }
    )
    # v2 §7.1: emit task_stage_changed ONLY when the first-stage role
    # row was planted (single-name `to`). Pool/empty first-stage entries
    # don't fire the stage-change event — Coach picks the assignee
    # later via /approve_stage, which fires its own task_role_assigned
    # (and skips task_stage_changed on the same-stage first plant).
    # Without this gate, every UI-created pool/default task would write
    # a misleading task_stage_changed row to project_events and falsely
    # reset the board safety ring's "board moved" signal.
    if planted_first_stage:
        await bus.publish(
            {
                "ts": now_iso,
                "agent_id": "system",
                "type": "task_stage_changed",
                "task_id": task_id,
                "from": None,
                "to": initial_status,
                "reason": "task_created",
                "owner": initial_owner,
                "assignee": initial_owner,
            }
        )
    return {
        "ok": True,
        "task_id": task_id,
        "workflow": req.workflow,
        "trajectory": trajectory,
    }


@app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(require_token)])
async def cancel_task_from_human(task_id: str) -> dict[str, Any]:
    """Human cancellation (Docs/kanban-specs-v2.md §8). Equivalent of
    `coord_archive_task` from the human side — sets `cancelled_at` so
    the archive view can distinguish cancellation from delivery, marks
    every active role row complete, and emits `task_stage_changed` +
    `task_archived`.

    Idempotent: returns 200 with `already=archive` if the task is
    already in archive."""
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, status, owner FROM tasks WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, detail=f"task {task_id} not found")
        task = dict(row)
        # Already-archive case: idempotent return so the UI can fire-and-
        # forget cancels without racing.
        if task["status"] == "archive":
            return {"ok": True, "task_id": task_id, "already": "archive"}
        old_status = task["status"]
        now = datetime.now(timezone.utc).isoformat()
        # Mark every active role row complete on archive — no roles
        # persist into archive (v2 §7.1 carryover).
        await c.execute(
            "UPDATE task_role_assignments SET completed_at = ? "
            "WHERE task_id = ? "
            "AND completed_at IS NULL AND superseded_by IS NULL",
            (now, task_id),
        )
        # Cancellation lands in archive with cancelled_at + archived_at +
        # completed_at populated. The archive view's "show cancelled"
        # toggle filters on cancelled_at.
        await c.execute(
            "UPDATE tasks SET status = 'archive', "
            "completed_at = ?, archived_at = ?, cancelled_at = ?, "
            "last_stage_change_at = ?, stale_alert_at = NULL, stall_escalation_level = 0 "
            "WHERE id = ? AND project_id = ?",
            (now, now, now, now, task_id, project_id),
        )
        if task["owner"]:
            await c.execute(
                "UPDATE agents SET current_task_id = NULL "
                "WHERE id = ? AND current_task_id = ?",
                (task["owner"], task_id),
            )
        await c.commit()
    finally:
        await c.close()

    ts = datetime.now(timezone.utc).isoformat()
    await bus.publish({
        "ts": ts,
        "agent_id": "human",
        "type": "task_stage_changed",
        "task_id": task_id,
        "from": old_status,
        "to": "archive",
        "reason": "cancelled",
        "note": "cancelled by human",
        "owner": task["owner"],
    })
    await bus.publish({
        "ts": ts,
        "agent_id": "human",
        "type": "task_archived",
        "task_id": task_id,
        "summary": "Cancelled by human via UI.",
        "body": "Cancelled by human via UI.",
        "cancelled": True,
        "owner": task["owner"],
    })
    return {"ok": True, "task_id": task_id, "old_status": old_status}


# ------------------------------------------------------------------
# Kanban-shaped views + human overrides (Docs/kanban-specs-v2.md §7)
# ------------------------------------------------------------------


def _priority_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    """Card sort: priority first (urgent floats), then created_at."""
    pri_rank = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
    return (
        pri_rank.get(row.get("priority") or "normal", 2),
        row.get("created_at") or "",
    )


async def _load_active_role_assignments(
    task_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """For the given task ids, fetch every active role-assignment row
    (un-completed, un-superseded) keyed by task_id. Lets the board
    response embed `assignments: [...]` per card without N+1 queries."""
    if not task_ids:
        return {}
    placeholders = ",".join("?" * len(task_ids))
    out: dict[str, list[dict[str, Any]]] = {tid: [] for tid in task_ids}
    c = await configured_conn()
    try:
        cur = await c.execute(
            f"SELECT id, task_id, role, eligible_owners, owner, "
            f"assigned_at, claimed_at, started_at, completed_at, "
            f"report_path, verdict "
            f"FROM task_role_assignments "
            f"WHERE task_id IN ({placeholders}) "
            f"AND completed_at IS NULL AND superseded_by IS NULL "
            f"ORDER BY assigned_at",
            task_ids,
        )
        for row in await cur.fetchall():
            d = dict(row)
            out.setdefault(d["task_id"], []).append(d)
    finally:
        await c.close()
    return out


async def _load_role_assignment_history(
    task_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Fetch full assignment history keyed by task_id.

    Archive cards use this so completed and superseded role rows remain
    visible after the task leaves the active board.
    """
    if not task_ids:
        return {}
    placeholders = ",".join("?" * len(task_ids))
    out: dict[str, list[dict[str, Any]]] = {tid: [] for tid in task_ids}
    c = await configured_conn()
    try:
        cur = await c.execute(
            f"SELECT id, task_id, role, eligible_owners, owner, "
            f"assigned_at, claimed_at, started_at, completed_at, "
            f"report_path, verdict, superseded_by "
            f"FROM task_role_assignments "
            f"WHERE task_id IN ({placeholders}) "
            f"ORDER BY assigned_at",
            task_ids,
        )
        for row in await cur.fetchall():
            d = dict(row)
            out.setdefault(d["task_id"], []).append(d)
    finally:
        await c.close()
    return out


@app.get("/api/tasks/board", dependencies=[Depends(require_token)])
async def get_tasks_board() -> dict[str, Any]:
    """Active kanban board: tasks grouped by stage, sorted by priority
    then created_at. No `archive` bucket — see GET /api/tasks/archive
    for the paginated archive view. Each task includes its full active
    role-assignment list so the card can render assignees."""
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND status != 'archive'",
            (project_id,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()

    role_map = await _load_active_role_assignments([r["id"] for r in rows])

    buckets: dict[str, list[dict[str, Any]]] = {
        "plan": [],
        "execute": [],
        "audit_syntax": [],
        "audit_semantics": [],
        "ship": [],
    }
    for row in rows:
        row["assignments"] = role_map.get(row["id"], [])
        buckets.setdefault(row["status"], []).append(row)
    for stage in buckets:
        buckets[stage].sort(key=_priority_sort_key)
    return {"board": buckets, **buckets}


@app.get("/api/tasks/archive", dependencies=[Depends(require_token)])
async def get_tasks_archive(
    limit: int = 50,
    offset: int = 0,
    q: str | None = None,
    include_cancelled: bool = False,
) -> dict[str, Any]:
    """Paginated archive view for the drawer. Newest-first by
    archived_at. `q` does case-insensitive title+description LIKE
    when supplied. `include_cancelled=False` (default) hides
    cancelled tasks; `True` shows them with their CANCELLED chip."""
    project_id = await resolve_active_project()
    limit = max(1, min(200, limit))
    offset = max(0, offset)
    where = ["project_id = ?", "status = 'archive'"]
    params: list[Any] = [project_id]
    if not include_cancelled:
        where.append("cancelled_at IS NULL")
    if q:
        where.append("(LOWER(title) LIKE ? OR LOWER(description) LIKE ?)")
        like = f"%{q.lower()}%"
        params.extend([like, like])
    clause = " WHERE " + " AND ".join(where)

    c = await configured_conn()
    try:
        cur = await c.execute(
            f"SELECT * FROM tasks{clause} "
            f"ORDER BY archived_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = [dict(r) for r in await cur.fetchall()]
        # Total count for pagination UI.
        cur = await c.execute(
            f"SELECT COUNT(*) AS n FROM tasks{clause}", params
        )
        total = int(dict(await cur.fetchone())["n"])
    finally:
        await c.close()
    role_map = await _load_role_assignment_history([r["id"] for r in rows])
    for row in rows:
        row["assignments"] = role_map.get(row["id"], [])
    return {"tasks": rows, "total": total, "limit": limit, "offset": offset}


@app.get(
    "/api/tasks/{task_id}/assignments", dependencies=[Depends(require_token)]
)
async def get_task_assignments(task_id: str) -> dict[str, Any]:
    """Full role-assignment history for one task — every row,
    including superseded + completed. Used by the card-expansion UI
    to render the audit-loop history (round 1 syntax fail → round 1
    syntax pass → round 1 semantics fail → round 2 semantics pass →
    ship)."""
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        # Confirm task exists in this project (avoids fishing).
        cur = await c.execute(
            "SELECT 1 FROM tasks WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        )
        if (await cur.fetchone()) is None:
            raise HTTPException(404, detail=f"task {task_id} not found")
        cur = await c.execute(
            "SELECT id, task_id, role, eligible_owners, owner, "
            "assigned_at, claimed_at, started_at, completed_at, "
            "report_path, verdict, superseded_by "
            "FROM task_role_assignments "
            "WHERE task_id = ? "
            "ORDER BY assigned_at",
            (task_id,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()
    return {"task_id": task_id, "assignments": rows}


# Kanban transitions / overrides ------------------------------------

# Reuse the in-tools state machine so the human override path goes
# through the same validator as Coach's MCP-tool override.
from server.tools import (  # noqa: E402
    ALL_KANBAN_STAGES,
    _valid_transition,
    _validate_trajectory,
)


@app.post(
    "/api/tasks/{task_id}/approve_stage",
    dependencies=[Depends(require_token)],
)
async def post_task_approve_stage(
    task_id: str, req: TaskApproveStageRequest
) -> dict[str, Any]:
    """Human-side equivalent of `coord_approve_stage` (Docs/kanban-specs-v2.md
    §8). Single transition tool: authorizes the next stage, names the
    assignee, plants the role row, supersedes any prior active row for
    the target role with stand-down wakes, and emits
    `task_stage_changed` + `task_role_assigned`. Replaces the v1
    `/api/tasks/{id}/stage` and `/api/tasks/{id}/assign` endpoints.
    """
    next_stage = req.next_stage
    if next_stage not in ALL_KANBAN_STAGES:
        raise HTTPException(400, detail=f"invalid stage: {next_stage}")

    assignee_raw = (req.assignee or "").strip().lower()
    if next_stage == "archive":
        if assignee_raw:
            raise HTTPException(
                400,
                detail=(
                    "next_stage='archive' takes no assignee — drop "
                    "`assignee` (or call POST /api/tasks/{id}/cancel "
                    "for the user-facing wrap-up)."
                ),
            )
        assignee: str | None = None
    else:
        if not assignee_raw:
            raise HTTPException(
                400,
                detail=(
                    f"next_stage='{next_stage}' requires an assignee "
                    f"(single Player slot)."
                ),
            )
        if "," in assignee_raw or "[" in assignee_raw:
            raise HTTPException(
                400,
                detail="v2 takes a single assignee slot — pools are FYI only.",
            )
        from server.tools import VALID_RECIPIENTS  # noqa: WPS433
        if (
            assignee_raw not in VALID_RECIPIENTS
            or assignee_raw in ("coach", "broadcast")
        ):
            raise HTTPException(
                400,
                detail=f"assignee must be a Player slot (p1..p10), not {assignee_raw!r}",
            )
        assignee = assignee_raw

    from server.kanban import (
        _role_for_stage as _kanban_role_for_stage,
        collect_superseded_role_owners,
        send_role_stand_down,
    )
    target_role = (
        _kanban_role_for_stage(next_stage) if next_stage != "archive" else None
    )

    project_id = await resolve_active_project()
    c = await configured_conn()
    displaced_target: list[str] = []
    displaced_source: list[str] = []
    old_status: str | None = None
    old_owner: str | None = None
    new_role_id: int | None = None
    try:
        cur = await c.execute(
            "SELECT status, owner FROM tasks "
            "WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, detail=f"task {task_id} not found")
        t = dict(row)
        old_status = t["status"]
        old_owner = t.get("owner")
        if old_status == "archive":
            raise HTTPException(
                400,
                detail=f"task {task_id} is already archived; archived tasks are read-only.",
            )
        # v2 §7.1 same-stage allowance — mirrors the MCP coord_approve_stage
        # logic. When a task was created with a pool/empty first-stage `to`,
        # tasks.status is set to that stage but no role row plants; the
        # human's first /approve_stage call has next_stage equal to current
        # status. _valid_transition rejects same-stage by default, so
        # special-case: allow same-stage IFF no active role row exists at
        # the target stage. If a row already exists, this is a normal
        # supersede attempt — reject and direct caller to the next stage.
        is_same_stage_plant = False
        if (
            old_status == next_stage
            and next_stage != "archive"
            and target_role is not None
        ):
            cur = await c.execute(
                "SELECT 1 FROM task_role_assignments "
                "WHERE task_id = ? AND role = ? "
                "AND completed_at IS NULL AND superseded_by IS NULL "
                "LIMIT 1",
                (task_id, target_role),
            )
            if await cur.fetchone():
                raise HTTPException(
                    400,
                    detail=(
                        f"task {task_id} is already in {next_stage!r} "
                        f"with an active {target_role} role. Same-stage "
                        f"approve_stage is only valid as the first plant "
                        f"when the task was created with a pool/empty "
                        f"first-stage `to`. Approve into the next stage "
                        f"instead, or rewrite the trajectory."
                    ),
                )
            is_same_stage_plant = True
        if not is_same_stage_plant and not _valid_transition(old_status, next_stage):
            raise HTTPException(
                400,
                detail=f"invalid transition: {old_status} → {next_stage}",
            )

        now = datetime.now(timezone.utc).isoformat()
        if next_stage == "archive":
            await c.execute(
                "UPDATE task_role_assignments SET completed_at = ? "
                "WHERE task_id = ? "
                "AND completed_at IS NULL AND superseded_by IS NULL",
                (now, task_id),
            )
            await c.execute(
                "UPDATE tasks SET status = 'archive', "
                "completed_at = ?, archived_at = ?, "
                "last_stage_change_at = ?, stale_alert_at = NULL, "
                "stall_escalation_level = 0 "
                "WHERE id = ? AND project_id = ?",
                (now, now, now, task_id, project_id),
            )
            if old_owner:
                await c.execute(
                    "UPDATE agents SET current_task_id = NULL "
                    "WHERE id = ? AND current_task_id = ?",
                    (old_owner, task_id),
                )
        else:
            source_role = _kanban_role_for_stage(old_status)
            if source_role:
                displaced_source = await collect_superseded_role_owners(
                    c, task_id=task_id, role=source_role, new_row_id=None,
                )
                if displaced_source:
                    await c.execute(
                        "UPDATE task_role_assignments "
                        "SET completed_at = ? "
                        "WHERE task_id = ? AND role = ? "
                        "AND completed_at IS NULL "
                        "AND superseded_by IS NULL",
                        (now, task_id, source_role),
                    )
            pre_displaced = await collect_superseded_role_owners(
                c, task_id=task_id, role=target_role, new_row_id=None,
            )
            insert_cur = await c.execute(
                "INSERT INTO task_role_assignments "
                "(task_id, role, eligible_owners, owner, "
                "assigned_at, claimed_at) "
                "VALUES (?, ?, '[]', ?, ?, ?)",
                (task_id, target_role, assignee, now, now),
            )
            new_role_id = insert_cur.lastrowid
            await c.execute(
                "UPDATE task_role_assignments "
                "SET superseded_by = ? "
                "WHERE task_id = ? AND role = ? AND id != ? "
                "AND completed_at IS NULL AND superseded_by IS NULL",
                (new_role_id, task_id, target_role, new_role_id),
            )
            displaced_target = [s for s in pre_displaced if s != assignee]
            tasks_owner: str | None = old_owner
            if next_stage == "execute":
                tasks_owner = assignee
            await c.execute(
                "UPDATE tasks SET status = ?, owner = ?, "
                "last_stage_change_at = ?, stale_alert_at = NULL, "
                "stall_escalation_level = 0 "
                "WHERE id = ? AND project_id = ?",
                (next_stage, tasks_owner, now, task_id, project_id),
            )
            if next_stage == "execute":
                await c.execute(
                    "UPDATE agents SET current_task_id = ? "
                    "WHERE id = ? AND current_task_id IS NULL",
                    (task_id, assignee),
                )
        await c.commit()
    finally:
        await c.close()

    ts = datetime.now(timezone.utc).isoformat()
    if not is_same_stage_plant:
        await bus.publish({
            "ts": ts,
            "agent_id": "human",
            "type": "task_stage_changed",
            "task_id": task_id,
            "from": old_status,
            "to": next_stage,
            "reason": "approve_stage_human",
            "assignee": assignee,
            "note": req.note or None,
            "owner": assignee if next_stage == "execute" else None,
        })
    if next_stage != "archive" and target_role and assignee:
        await bus.publish({
            "ts": ts,
            "agent_id": "human",
            "type": "task_role_assigned",
            "task_id": task_id,
            "role": target_role,
            "owner": assignee,
            "to": assignee,
            "note": req.note or None,
        })

    if displaced_source:
        try:
            await send_role_stand_down(
                task_id=task_id,
                role=_kanban_role_for_stage(old_status) or "",
                displaced=displaced_source,
                new_owners=[],
            )
        except Exception:
            pass
    if displaced_target and target_role:
        try:
            await send_role_stand_down(
                task_id=task_id,
                role=target_role,
                displaced=displaced_target,
                new_owners=[assignee] if assignee else [],
            )
        except Exception:
            pass

    if next_stage != "archive" and assignee:
        from server.agents import maybe_wake_agent
        from server.tools import _with_player_reminder
        wake_body = _with_player_reminder(req.note or (
            f"Human approved task {task_id} → stage "
            f"{next_stage!r} ({target_role})."
        ))
        try:
            await maybe_wake_agent(
                assignee, wake_body,
                bypass_debounce=True,
                wake_source="kanban_approval_human",
            )
        except Exception:
            pass

    return {
        "ok": True,
        "task_id": task_id,
        "from": old_status,
        "to": next_stage,
        "assignee": assignee,
    }


@app.post(
    "/api/tasks/{task_id}/flag_deviation",
    dependencies=[Depends(require_token)],
)
async def post_task_flag_deviation(
    task_id: str, req: TaskFlagDeviationRequest
) -> dict[str, Any]:
    """Human-side deviation flag (Docs/kanban-specs-v2.md §22.1). Inserts
    a `deviations_log` row with `noticed_at='human'` so the validation
    instrumentation captures human-noticed drift alongside the audit-FAIL
    + Coach-flagged paths."""
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT owner FROM tasks WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, detail=f"task {task_id} not found")
        executor = dict(row).get("owner") or "unknown"
        ts = datetime.now(timezone.utc).isoformat()
        cur = await c.execute(
            "INSERT INTO deviations_log "
            "(project_id, ts, task_id, executor, noticed_at, description) "
            "VALUES (?, ?, ?, ?, 'human', ?)",
            (project_id, ts, task_id, executor, req.description),
        )
        new_id = cur.lastrowid
        await c.commit()
    finally:
        await c.close()
    return {
        "ok": True,
        "deviation_id": new_id,
        "task_id": task_id,
        "executor": executor,
    }


# NOTE: POST /api/tasks/{id}/complexity was removed in v0.3 — routing
# moved to the trajectory column. Use POST /api/tasks/{id}/trajectory.


@app.post(
    "/api/tasks/{task_id}/workflow", dependencies=[Depends(require_token)]
)
async def post_task_workflow(
    task_id: str, req: TaskWorkflowRequest
) -> dict[str, Any]:
    """Update workflow tag and/or tracking_reason. v0.3: routing knobs
    (required_reviews / ship_required / complexity) moved to the
    trajectory column — use POST /api/tasks/{id}/trajectory instead."""
    project_id = await resolve_active_project()
    tracking_reason = (
        req.tracking_reason.strip()
        if isinstance(req.tracking_reason, str) and req.tracking_reason.strip()
        else None
    )
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT owner, status, workflow, tracking_reason "
            "FROM tasks WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, detail=f"task {task_id} not found")
        t = dict(row)
        if t.get("status") == "archive":
            raise HTTPException(400, detail="archived tasks are read-only")
        workflow = req.workflow or t.get("workflow") or "generic"
        next_reason = tracking_reason or t.get("tracking_reason")
        await c.execute(
            "UPDATE tasks SET workflow = ?, tracking_reason = ? "
            "WHERE id = ? AND project_id = ?",
            (workflow, next_reason, task_id, project_id),
        )
        await c.commit()
    finally:
        await c.close()

    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "human",
        "type": "task_workflow_set",
        "task_id": task_id,
        "workflow": workflow,
        "tracking_reason": next_reason,
        "to": t["owner"],
    })
    return {
        "ok": True,
        "task_id": task_id,
        "workflow": workflow,
        "tracking_reason": next_reason,
    }


@app.post(
    "/api/tasks/{task_id}/trajectory",
    dependencies=[Depends(require_token)],
)
async def post_task_trajectory(
    task_id: str, req: TaskTrajectoryRequest
) -> dict[str, Any]:
    """Human-side mid-flight reroute, sibling of
    `coord_set_task_trajectory`. Validates that stages already entered
    cannot be removed; supersedes role rows for removed stages and
    upserts rows for added stages. Emits `task_trajectory_changed`.
    Mid-flight: the create-time first-stage-assigned rule doesn't
    apply (role rows already exist).
    """
    validated, err = _validate_trajectory(
        req.trajectory, enforce_first_stage_assigned=False,
    )
    if err:
        raise HTTPException(400, detail=f"invalid trajectory: {err}")
    assert validated is not None
    new_trajectory = validated
    new_stages = [s["stage"] for s in new_trajectory]

    role_for_stage = {
        "plan": "planner",
        "execute": "executor",
        "audit_syntax": "auditor_syntax",
        "audit_semantics": "auditor_semantics",
        "ship": "shipper",
    }

    project_id = await resolve_active_project()
    now_iso = datetime.now(timezone.utc).isoformat()

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, status, owner, trajectory FROM tasks "
            "WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, detail=f"task {task_id} not found")
        t = dict(row)
        if t.get("status") == "archive":
            raise HTTPException(400, detail="archived tasks are read-only")

        try:
            old_trajectory = json.loads(t.get("trajectory") or "[]")
        except (TypeError, ValueError):
            old_trajectory = []
        old_stages = [
            s["stage"]
            for s in old_trajectory
            if isinstance(s, dict) and "stage" in s
        ]

        # Cannot remove a stage the task has already entered.
        current_stage = t.get("status")
        if current_stage in old_stages:
            current_idx = old_stages.index(current_stage)
            entered = set(old_stages[: current_idx + 1])
            removed = entered - set(new_stages)
            if removed:
                raise HTTPException(
                    400,
                    detail=(
                        "cannot remove already-entered stages: "
                        + ",".join(sorted(removed))
                    ),
                )

        # Update trajectory column.
        await c.execute(
            "UPDATE tasks SET trajectory = ? "
            "WHERE id = ? AND project_id = ?",
            (
                json.dumps(new_trajectory, separators=(",", ":")),
                task_id,
                project_id,
            ),
        )

        # Deactivate role rows for removed stages. The schema has
        # `superseded_by` (FK self-ref to a replacement row) and
        # `completed_at` (role's work is done). Trajectory removal has
        # no replacement row, so we use `completed_at = now()` to drop
        # the row from active filters (`completed_at IS NULL AND
        # superseded_by IS NULL`). The audit trail is preserved.
        removed_stages = set(old_stages) - set(new_stages)
        for stage in removed_stages:
            role = role_for_stage.get(stage)
            if not role:
                continue
            await c.execute(
                "UPDATE task_role_assignments "
                "SET completed_at = ? "
                "WHERE task_id = ? AND role = ? "
                "AND completed_at IS NULL AND superseded_by IS NULL",
                (now_iso, task_id, role),
            )

        # Upsert rows for stages in the new trajectory.
        for stage_obj in new_trajectory:
            stage = stage_obj["stage"]
            role = role_for_stage.get(stage)
            if not role:
                continue
            to_field = stage_obj.get("to") or []
            if isinstance(to_field, str):
                eligible = [to_field] if to_field else []
            else:
                eligible = list(to_field)
            owner_val = eligible[0] if len(eligible) == 1 else None
            eligible_json = json.dumps(eligible, separators=(",", ":"))

            cur = await c.execute(
                "SELECT id, owner, eligible_owners FROM task_role_assignments "
                "WHERE task_id = ? AND role = ? "
                "AND completed_at IS NULL AND superseded_by IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (task_id, role),
            )
            existing = await cur.fetchone()
            if existing is None:
                await c.execute(
                    "INSERT INTO task_role_assignments "
                    "(task_id, role, owner, eligible_owners, "
                    "assigned_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (task_id, role, owner_val, eligible_json, now_iso),
                )
            else:
                ex = dict(existing)
                if (
                    ex.get("owner") != owner_val
                    or ex.get("eligible_owners") != eligible_json
                ):
                    await c.execute(
                        "UPDATE task_role_assignments "
                        "SET owner = ?, eligible_owners = ? "
                        "WHERE id = ?",
                        (owner_val, eligible_json, ex["id"]),
                    )

        await c.commit()
    finally:
        await c.close()

    await bus.publish({
        "ts": now_iso,
        "agent_id": "human",
        "type": "task_trajectory_changed",
        "task_id": task_id,
        "trajectory": new_trajectory,
        "stages_added": list(set(new_stages) - set(old_stages)),
        "stages_removed": list(set(old_stages) - set(new_stages)),
        "to": t.get("owner"),
    })
    return {"ok": True, "task_id": task_id, "trajectory": new_trajectory}


@app.get("/api/tasks/flow_health", dependencies=[Depends(require_token)])
async def get_tasks_flow_health() -> dict[str, Any]:
    """Per-stage counts + oldest stage-change timestamp + stalled count
    + kanban subscriber liveness. Lets the human inspect 'is the engine
    actually moving' without scraping events."""
    from server import kanban as kanban_mod
    project_id = await resolve_active_project()
    stages = ["plan", "execute", "audit_syntax", "audit_semantics", "ship"]
    out_stages: dict[str, dict[str, Any]] = {}
    stalled_count = 0

    c = await configured_conn()
    try:
        for stage in stages:
            cur = await c.execute(
                "SELECT COUNT(*) AS cnt, MIN(last_stage_change_at) AS oldest "
                "FROM tasks WHERE project_id = ? AND status = ?",
                (project_id, stage),
            )
            row = await cur.fetchone()
            d = dict(row) if row else {"cnt": 0, "oldest": None}
            out_stages[stage] = {
                "count": int(d.get("cnt") or 0),
                "oldest_stage_change": d.get("oldest"),
            }

        cur = await c.execute(
            "SELECT COUNT(*) AS cnt FROM tasks "
            "WHERE project_id = ? AND status NOT IN ('archive') "
            "AND stale_alert_at IS NOT NULL "
            "AND last_stage_change_at IS NOT NULL "
            "AND last_stage_change_at = stale_alert_at",
            (project_id,),
        )
        row = await cur.fetchone()
        stalled_count = int(dict(row).get("cnt") or 0) if row else 0
    finally:
        await c.close()

    return {
        "stages": out_stages,
        "stalled_count": stalled_count,
        "subscriber_last_event_at": kanban_mod.subscriber_last_event_at(),
        "subscriber_alive": kanban_mod.is_running(),
    }


@app.post(
    "/api/tasks/{task_id}/blocked", dependencies=[Depends(require_token)]
)
async def post_task_blocked(
    task_id: str, req: TaskBlockedRequest
) -> dict[str, Any]:
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT owner, status FROM tasks WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, detail=f"task {task_id} not found")
        task_row = dict(row)
        owner = task_row["owner"]
        if task_row.get("status") == "archive":
            raise HTTPException(
                400, detail="archived tasks are read-only"
            )
        await c.execute(
            "UPDATE tasks SET blocked = ?, blocked_reason = ? "
            "WHERE id = ? AND project_id = ?",
            (1 if req.blocked else 0, req.reason or None, task_id, project_id),
        )
        await c.commit()
    finally:
        await c.close()
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "human",
        "type": "task_blocked_changed",
        "task_id": task_id,
        "blocked": bool(req.blocked),
        "reason": req.reason or None,
        "to": owner,
    })
    return {
        "ok": True, "task_id": task_id, "blocked": bool(req.blocked),
    }


@app.post(
    "/api/tasks/{task_id}/spec", dependencies=[Depends(require_token)]
)
async def post_task_spec(
    task_id: str, req: TaskSpecRequest
) -> dict[str, Any]:
    """Human-side spec writer. Same effect as coord_write_task_spec
    but bypasses the Player permission check (HARNESS_TOKEN is the
    only gate for human-side endpoints)."""
    from server.tasks import is_valid_task_id, write_task_spec
    if not is_valid_task_id(task_id):
        raise HTTPException(400, detail=f"invalid task_id: {task_id}")
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT title, owner, created_by, created_at, priority "
            "FROM tasks "
            "WHERE id = ? AND project_id = ?",
            (task_id, project_id),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, detail=f"task {task_id} not found")
        t = dict(row)
    finally:
        await c.close()

    try:
        target, rel, written_at = await write_task_spec(
            project_id=project_id,
            task_id=task_id,
            title=t["title"],
            body=req.body,
            author="human",
            created_by=t["created_by"],
            created_at=t["created_at"],
            priority=t["priority"],
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc

    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE tasks SET spec_path = ?, spec_written_at = ? "
            "WHERE id = ? AND project_id = ?",
            (rel, written_at, task_id, project_id),
        )
        await c.commit()
    finally:
        await c.close()
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "human",
        "type": "task_spec_written",
        "task_id": task_id,
        "spec_path": rel,
        "to": t["owner"],
    })
    return {"ok": True, "task_id": task_id, "spec_path": rel}


# POST /api/tasks/{id}/assign was removed in v2 — folded into
# /api/tasks/{id}/approve_stage which now does the role-row plant
# atomically with the stage transition. See Docs/kanban-specs-v2.md §8.


# ------------------------------------------------------------------
# Per-project event log (Docs/kanban-specs-v2.md §8 + §9.5)
# ------------------------------------------------------------------


@app.get(
    "/api/projects/{project_id}/event_log",
    dependencies=[Depends(require_token)],
)
async def get_project_event_log(
    project_id: str,
    actor: str | None = None,
    type: str | None = None,
    task_id: str | None = None,
    since: str | None = None,
    limit: int = 50,
    include_read: bool = False,
) -> dict[str, Any]:
    """Paginated read of the per-project event log (Docs/kanban-specs-v2.md
    §8 / §9.5). Coach's tick consumes the unread tail via its own
    prompt-build path; humans browse via this endpoint.

    Filters:
      - actor: 'coach' / 'p1'..'p10' / 'compass' / 'system' / 'human'
      - type: any project_events.type value
      - task_id: filter to a single task
      - since: ISO timestamp; rows with ts > since (exclusive)
      - limit: default 50, max 200
      - include_read: default false (only unread rows shown)

    Does NOT stamp `read_by_coach_at` — that column is Coach-tick-
    specific and is only stamped by the Coach prompt-build path.
    """
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200
    where_parts: list[str] = ["project_id = ?"]
    params: list[Any] = [project_id]
    if actor:
        where_parts.append("actor = ?")
        params.append(actor.strip())
    if type:
        where_parts.append("type = ?")
        params.append(type.strip())
    if task_id:
        where_parts.append("task_id = ?")
        params.append(task_id.strip())
    if since:
        where_parts.append("ts > ?")
        params.append(since.strip())
    if not include_read:
        where_parts.append("read_by_coach_at IS NULL")
    where = " AND ".join(where_parts)
    sql = (
        f"SELECT id, project_id, ts, actor, type, task_id, "
        f"payload_json, payload_pointer, read_by_coach_at "
        f"FROM project_events WHERE {where} "
        f"ORDER BY ts ASC, id ASC LIMIT ?"
    )
    params.append(limit)
    c = await configured_conn()
    try:
        cur = await c.execute(sql, params)
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()
    # Parse payload_json so the API consumer doesn't have to.
    out: list[dict[str, Any]] = []
    for r in rows:
        body = r.get("payload_json") or "{}"
        try:
            r["payload"] = json.loads(body)
        except Exception:
            r["payload"] = {}
        r.pop("payload_json", None)
        out.append(r)
    return {"events": out, "limit": limit, "include_read": include_read}


# ------------------------------------------------------------------
# Events (paginated replay for pane restore)
# ------------------------------------------------------------------


# Single source of truth for valid message recipients — defined in
# tools.py because that's where coord_send_message uses it. Re-exposed
# here so POST /api/messages validates the same way.
from server.tools import VALID_RECIPIENTS as _HUMAN_MSG_RECIPIENTS  # noqa: E402


# ---------------------------------------------------------------------------
# Pending AskUserQuestion prompts — list for the UI, submit answers back
# so the agent's paused can_use_tool callback resumes and the turn
# continues in-place (same turn, not a new one).
# ---------------------------------------------------------------------------


@app.get("/api/questions/pending", dependencies=[Depends(require_token)])
async def list_pending_questions() -> dict[str, Any]:
    """Metadata-only view of currently-waiting AskUserQuestion calls.
    Used by the UI form to hydrate on reload (so refreshing the page
    doesn't lose the form state for in-flight questions)."""
    from server import interactions as interactions_registry
    # Legacy shape: older UI builds expect `questions` flattened onto the
    # entry rather than nested under `payload`. Unpack here for
    # backward-compat so clients don't need a coordinated update.
    items = []
    for p in interactions_registry.list_pending(kind="question"):
        item = dict(p)
        payload = item.pop("payload", {}) or {}
        item["questions"] = payload.get("questions", [])
        items.append(item)
    return {"pending": items}


class AnswerQuestionRequest(BaseModel):
    # {question_text: selected_label} per the SDK's expected shape.
    # multi-select answers come in as comma-joined strings per the doc.
    answers: dict[str, str] = Field(..., min_length=1)


@app.post(
    "/api/questions/{correlation_id}/answer",
    dependencies=[Depends(require_token)],
)
async def answer_pending_question(
    correlation_id: str,
    req: AnswerQuestionRequest,
    actor: dict = Depends(audit_actor),
) -> dict[str, Any]:
    """Human submits the question form; resolve the waiting Future so
    the agent's turn resumes. 404 when the id is stale (already
    answered, timed out, or never existed)."""
    from server import interactions as interactions_registry
    entry = interactions_registry.get(correlation_id)
    if entry is None or entry.kind != "question":
        raise HTTPException(
            404,
            detail=f"question {correlation_id!r} not found or already resolved",
        )
    ok = interactions_registry.resolve(correlation_id, req.answers)
    if not ok:
        raise HTTPException(
            404,
            detail=f"question {correlation_id!r} already resolved",
        )
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "human",
            "type": "question_answered",
            "correlation_id": correlation_id,
            "route": "human",
            "answer_keys": list(req.answers.keys()),
            "actor": actor,
        }
    )
    return {"ok": True, "correlation_id": correlation_id}


# --- Plan approval (ExitPlanMode) ------------------------------------------

@app.get("/api/plans/pending", dependencies=[Depends(require_token)])
async def list_pending_plans() -> dict[str, Any]:
    """Currently-waiting ExitPlanMode approvals."""
    from server import interactions as interactions_registry
    items = []
    for p in interactions_registry.list_pending(kind="plan"):
        item = dict(p)
        payload = item.pop("payload", {}) or {}
        item["plan"] = payload.get("plan", "")
        items.append(item)
    return {"pending": items}


class PlanDecisionRequest(BaseModel):
    decision: str = Field(..., pattern=r"^(approve|reject|approve_with_comments)$")
    comments: str | None = Field(default=None, max_length=10_000)


@app.post(
    "/api/plans/{correlation_id}/decision",
    dependencies=[Depends(require_token)],
)
async def decide_pending_plan(
    correlation_id: str,
    req: PlanDecisionRequest,
    actor: dict = Depends(audit_actor),
) -> dict[str, Any]:
    """Human submits a plan decision; resolve the waiting Future so the
    agent's turn resumes. Reject and approve_with_comments both
    require comments (the UI enforces that too, but double-check here
    so a scripted POST can't smuggle an empty-comment reject into the
    agent's deny message)."""
    from server import interactions as interactions_registry
    entry = interactions_registry.get(correlation_id)
    if entry is None or entry.kind != "plan":
        raise HTTPException(
            404,
            detail=f"plan {correlation_id!r} not found or already resolved",
        )
    comments = (req.comments or "").strip()
    if req.decision in ("reject", "approve_with_comments") and not comments:
        raise HTTPException(
            400,
            detail=f"'{req.decision}' requires non-empty 'comments'",
        )
    ok = interactions_registry.resolve(
        correlation_id,
        {"decision": req.decision, "comments": comments},
    )
    if not ok:
        raise HTTPException(
            404,
            detail=f"plan {correlation_id!r} already resolved",
        )
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "human",
            "type": "plan_decided",
            "correlation_id": correlation_id,
            "route": "human",
            "decision": req.decision,
            "has_comments": bool(comments),
            "actor": actor,
        }
    )
    return {"ok": True, "correlation_id": correlation_id, "decision": req.decision}


class ExtendInteractionRequest(BaseModel):
    # Seconds from NOW for the new deadline. Clamped [30, 86400] in
    # interactions.extend(). Default 1800 matches the standard window.
    seconds: int = Field(default=1800, ge=30, le=86_400)


@app.post(
    "/api/interactions/{correlation_id}/extend",
    dependencies=[Depends(require_token)],
)
async def extend_pending_interaction(
    correlation_id: str,
    req: ExtendInteractionRequest,
    actor: dict = Depends(audit_actor),
) -> dict[str, Any]:
    """Push a pending question-or-plan's deadline out. Both kinds
    share this endpoint — routing by correlation_id, not by kind."""
    from server import interactions as interactions_registry
    result = interactions_registry.extend(correlation_id, req.seconds)
    if result is None:
        raise HTTPException(
            404,
            detail=f"interaction {correlation_id!r} not found or already resolved",
        )
    entry = interactions_registry.get(correlation_id)
    kind = entry.kind if entry else "unknown"
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "human",
            "type": "interaction_extended",
            "correlation_id": correlation_id,
            "interaction_kind": kind,
            "deadline_at": result["deadline_at"],
            "seconds_from_now": result["seconds_from_now"],
            "actor": actor,
        }
    )
    return {"ok": True, **result, "kind": kind}


@app.post(
    "/api/questions/{correlation_id}/cancel",
    dependencies=[Depends(require_token)],
)
async def cancel_pending_question(
    correlation_id: str,
    actor: dict = Depends(audit_actor),
) -> dict[str, Any]:
    """Human explicitly cancels a pending AskUserQuestion without answering.
    Resolves the agent's paused Future with an InteractionRejected so the
    agent gets a PermissionResultDeny and can proceed (reformulate or
    escalate). Emits question_cancelled via the existing reject path."""
    from server import interactions as interactions_registry
    entry = interactions_registry.get(correlation_id)
    if entry is None or entry.kind != "question":
        raise HTTPException(
            404,
            detail=f"question {correlation_id!r} not found or already resolved",
        )
    ok = interactions_registry.reject(correlation_id, "cancelled by human operator")
    if not ok:
        raise HTTPException(
            404,
            detail=f"question {correlation_id!r} already resolved",
        )
    return {"ok": True, "correlation_id": correlation_id}


@app.post(
    "/api/plans/{correlation_id}/cancel",
    dependencies=[Depends(require_token)],
)
async def cancel_pending_plan(
    correlation_id: str,
    actor: dict = Depends(audit_actor),
) -> dict[str, Any]:
    """Human explicitly cancels a pending ExitPlanMode plan review without
    deciding. Resolves the agent's paused Future with InteractionRejected so
    the agent gets a PermissionResultDeny and can revise or escalate.
    Emits plan_cancelled via the existing reject path."""
    from server import interactions as interactions_registry
    entry = interactions_registry.get(correlation_id)
    if entry is None or entry.kind != "plan":
        raise HTTPException(
            404,
            detail=f"plan {correlation_id!r} not found or already resolved",
        )
    ok = interactions_registry.reject(correlation_id, "cancelled by human operator")
    if not ok:
        raise HTTPException(
            404,
            detail=f"plan {correlation_id!r} already resolved",
        )
    return {"ok": True, "correlation_id": correlation_id}


class HumanMessageRequest(BaseModel):
    to: str
    body: str = Field(min_length=1, max_length=5000)
    subject: str | None = Field(default=None, max_length=200)
    priority: str = Field(default="normal", pattern=r"^(normal|interrupt)$")


@app.post("/api/messages", dependencies=[Depends(require_token)])
async def send_human_message(req: HumanMessageRequest) -> dict[str, Any]:
    """Queue a message from the human into an agent's inbox AND auto-wake
    the recipient so they read + respond without needing a separate
    prompt. Debounced inside maybe_wake_agent so rapid messages don't
    stack turns. Broadcasts don't auto-wake (would spiral)."""
    to = req.to.strip().lower()
    if to not in _HUMAN_MSG_RECIPIENTS:
        raise HTTPException(
            400, detail=f"invalid recipient '{to}'"
        )
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO messages (project_id, from_id, to_id, subject, body, priority) "
            "VALUES (?, 'human', ?, ?, ?, ?) RETURNING id",
            (project_id, to, req.subject, req.body, req.priority),
        )
        row = await cur.fetchone()
        msg_id = dict(row)["id"] if row else None
        await c.commit()
    finally:
        await c.close()

    _msg_body = req.body or ""
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "human",
            "type": "message_sent",
            "message_id": msg_id,
            "to": to,
            "subject": req.subject,
            "body_preview": _msg_body[:4000],
            "body_full_len": len(_msg_body),
            "body_truncated": len(_msg_body) > 4000,
            "priority": req.priority,
        }
    )
    if to != "broadcast":
        from server.agents import maybe_wake_agent
        subj = f" (subject: {req.subject})" if req.subject else ""
        # Include inline body preview (up to 240 chars) so the agent
        # doesn't burn a tool-call just to read a short message.
        preview_snippet = (req.body or "").strip().replace("\n", " ")[:240]
        # Human messages are not ping-pongy — the human isn't going to
        # auto-reply to the agent's reply, so skip the debounce and
        # wake even if the agent just finished a turn.
        wake_body = f"New message from the human{subj}: \"{preview_snippet}\""
        if to != "coach":
            from server.tools import _with_player_reminder
            wake_body = _with_player_reminder(wake_body)
        await maybe_wake_agent(
            to,
            wake_body,
            bypass_debounce=True,
        )
    return {"ok": True, "message_id": msg_id}


@app.get("/api/messages", dependencies=[Depends(require_token)])
async def list_messages(limit: int = 50) -> dict[str, Any]:
    """Recent messages (newest first, capped). Full body included —
    the UI decides how much to show."""
    limit = max(1, min(limit, 200))
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, from_id, to_id, subject, body, sent_at, "
            "in_reply_to, priority "
            "FROM messages WHERE project_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (project_id, limit),
        )
        rows = await cur.fetchall()
    finally:
        await c.close()
    return {"messages": [dict(r) for r in rows]}


@app.get("/api/memory", dependencies=[Depends(require_token)])
async def list_memory() -> dict[str, Any]:
    """List shared-memory topics (flat table, not paginated — this
    harness has at most a few dozen memory docs in practice)."""
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT topic, last_updated, last_updated_by, version, "
            "LENGTH(content) AS size FROM memory_docs "
            "WHERE project_id = ? "
            "ORDER BY last_updated DESC",
            (project_id,),
        )
        rows = await cur.fetchall()
    finally:
        await c.close()
    return {"docs": [dict(r) for r in rows]}


class HumanMemoryWrite(BaseModel):
    topic: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9\-]{0,63}$")
    content: str = Field(max_length=20_000)


@app.post("/api/memory", dependencies=[Depends(require_token)])
async def write_memory_from_human(req: HumanMemoryWrite) -> dict[str, Any]:
    """Upsert a memory doc from the human operator.

    Matches coord_update_memory semantics (full overwrite, not
    append). last_updated_by is 'human'. Emits 'memory_updated' so
    open agents pick up the change on their next read_memory."""
    now = datetime.now(timezone.utc).isoformat()
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT version FROM memory_docs WHERE topic = ? AND project_id = ?",
            (req.topic, project_id),
        )
        row = await cur.fetchone()
        if row:
            new_version = int(dict(row)["version"]) + 1
            await c.execute(
                "UPDATE memory_docs SET content = ?, last_updated = ?, "
                "last_updated_by = 'human', version = ? "
                "WHERE topic = ? AND project_id = ?",
                (req.content, now, new_version, req.topic, project_id),
            )
        else:
            new_version = 1
            await c.execute(
                "INSERT INTO memory_docs (project_id, topic, content, last_updated, "
                "last_updated_by, version) VALUES (?, ?, ?, ?, 'human', ?)",
                (project_id, req.topic, req.content, now, new_version),
            )
        await c.commit()
    finally:
        await c.close()

    await bus.publish(
        {
            "ts": now,
            "agent_id": "human",
            "type": "memory_updated",
            "topic": req.topic,
            "version": new_version,
            "size": len(req.content),
        }
    )
    return {"ok": True, "topic": req.topic, "version": new_version}


@app.get("/api/memory/{topic}", dependencies=[Depends(require_token)])
async def get_memory(topic: str) -> dict[str, Any]:
    """Full content of a single memory doc."""
    # Validate with the same regex the MCP tool enforces on write.
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,63}", topic):
        raise HTTPException(400, detail="invalid topic")
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT topic, content, last_updated, last_updated_by, version "
            "FROM memory_docs WHERE topic = ? AND project_id = ?",
            (topic, project_id),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        raise HTTPException(404, detail="not found")
    return dict(row)


@app.get("/api/decisions", dependencies=[Depends(require_token)])
async def list_decisions() -> dict[str, Any]:
    """List local decision records (recent first, capped at 50).

    Decisions live primarily on the configured cloud drive at
    `<webdav-base>/projects/<active>/decisions/<file>.md` with a
    local fallback under `/data/projects/<active>/decisions/`. This
    endpoint reads the local store for the active project only.
    """
    project_id = await resolve_active_project()
    local_dir = project_paths(project_id).decisions
    if not local_dir.is_dir():
        return {"decisions": [], "dir": str(local_dir), "exists": False}

    items: list[dict[str, Any]] = []
    files = sorted(local_dir.glob("*.md"), key=lambda p: p.name, reverse=True)
    for p in files[:50]:
        try:
            text = p.read_text(encoding="utf-8")
            # Light frontmatter parse — title only; full parse is overkill here.
            title = p.stem
            if text.startswith("---\n"):
                end = text.find("\n---\n", 4)
                if end > 0:
                    for line in text[4:end].splitlines():
                        if line.startswith("title:"):
                            title = line[len("title:"):].strip()
                            break
            st = p.stat()
            items.append({
                "filename": p.name,
                "title": title,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
        except Exception:
            # Skip unreadable files; surface via /api/health if persistent
            continue
    return {"decisions": items, "dir": str(local_dir), "exists": True}


@app.get("/api/decisions/{filename}", dependencies=[Depends(require_token)])
async def get_decision(filename: str) -> dict[str, Any]:
    """Return the full content of a single decision file.

    Filename validation rejects anything that looks like a path to
    prevent traversal — decisions live in a flat directory.
    """
    if "/" in filename or ".." in filename or not filename.endswith(".md"):
        raise HTTPException(400, detail="invalid filename")
    project_id = await resolve_active_project()
    local_dir = project_paths(project_id).decisions
    target = local_dir / filename
    if not target.is_file():
        raise HTTPException(404, detail="not found")
    try:
        content = target.read_text(encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, detail=f"read failed: {e}")
    return {
        "filename": filename,
        "content": content,
        "size": len(content),
    }


# Global + per-project CLAUDE.md files are edited via the standard
# file-browser write endpoint (`POST /api/files/write/global` /
# `.../project`) by the human, and via the standard Write tool by
# Coach. `build_system_prompt_suffix()` reads them on every turn.


class FileWrite(BaseModel):
    content: str = Field(..., description="UTF-8 text body")


@app.get("/api/files/roots", dependencies=[Depends(require_token)])
async def files_roots() -> list[dict[str, Any]]:
    """List the named roots the explorer is allowed to browse."""
    return filesmod.list_roots()


@app.get("/api/files/tree/{root}", dependencies=[Depends(require_token)])
async def files_tree(root: str) -> dict[str, Any]:
    """Recursive tree under a named root. Directories before files, both
    sorted case-insensitively. Missing root → empty tree, not 404."""
    try:
        return filesmod.tree(root)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@app.get("/api/files/read/{root}", dependencies=[Depends(require_token)])
async def files_read(root: str, path: str = Query(..., description="relative to root")) -> dict[str, Any]:
    try:
        return filesmod.read_text(root, path)
    except FileNotFoundError:
        raise HTTPException(404, detail="not found")
    except filesmod.FileDenied as e:
        raise HTTPException(403, detail=str(e))
    except PermissionError as e:
        raise HTTPException(403, detail=str(e))
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@app.put("/api/files/write/{root}", dependencies=[Depends(require_token)])
async def files_write(
    root: str,
    req: FileWrite,
    path: str = Query(..., description="relative to root"),
    create_only: bool = Query(
        False,
        description=(
            "If true, refuse to overwrite an existing file (409). Used "
            "by the Files-pane '+ new file' button so an empty body "
            "doesn't silently truncate an existing file."
        ),
    ),
) -> dict[str, Any]:
    try:
        result = await filesmod.write_text(
            root, path, req.content, create_only=create_only,
        )
    except filesmod.FileAlreadyExists as e:
        raise HTTPException(409, detail=str(e))
    except filesmod.FileDenied as e:
        raise HTTPException(403, detail=str(e))
    except PermissionError as e:
        raise HTTPException(403, detail=str(e))
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "human",
            "type": "file_written",
            "root": root,
            "path": path,
            "size": result["size"],
        }
    )
    # Phase 7 (PROJECTS_SPEC.md §14 Resolved: INDEX.md maintenance):
    # rebuild wiki/INDEX.md when a write lands under the wiki tree
    # (either via the global root + "wiki/..." or — once the wiki
    # gets per-project sub-roots — via project-sub roots resolving
    # under /data/wiki/). Skip when the write IS the index itself
    # to avoid a feedback loop. Best-effort: a rebuild failure
    # doesn't fail the user's write call.
    if root == "global" and path.startswith("wiki/") and not path.endswith("INDEX.md"):
        try:
            from server.paths import update_wiki_index
            update_wiki_index()
        except Exception:
            logger.exception("update_wiki_index failed (non-fatal)")
    return {"ok": True, **result}


class FileWriteProposalResolution(BaseModel):
    note: str | None = Field(default=None, max_length=400)


_VALID_SCOPES = ("truth", "project_claude_md")


@app.get(
    "/api/file-write-proposals", dependencies=[Depends(require_token)],
)
async def list_file_write_proposals(
    status: str | None = None,
    scope: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List file-write proposals for the active project, newest first.

    Filters:
      - status: `pending` | `approved` | `denied` | `cancelled` |
        `superseded`. Omit for all.
      - scope: `truth` | `project_claude_md`. Omit for all.
    Default limit 50; cap 200.
    """
    limit = max(1, min(limit, 200))
    project_id = await resolve_active_project()
    where = ["project_id = ?"]
    params: list[Any] = [project_id]
    if status:
        if status not in (
            "pending", "approved", "denied", "cancelled", "superseded",
        ):
            raise HTTPException(400, detail="invalid status filter")
        where.append("status = ?")
        params.append(status)
    if scope:
        if scope not in _VALID_SCOPES:
            raise HTTPException(400, detail="invalid scope filter")
        where.append("scope = ?")
        params.append(scope)
    sql = (
        "SELECT id, project_id, proposer_id, scope, path, "
        "proposed_content, summary, status, created_at, resolved_at, "
        "resolved_by, resolved_note FROM file_write_proposals WHERE "
        + " AND ".join(where) + " ORDER BY id DESC LIMIT ?"
    )
    params.append(limit)
    c = await configured_conn()
    try:
        cur = await c.execute(sql, params)
        rows = await cur.fetchall()
    finally:
        await c.close()
    return {
        "proposals": [
            truthmod.file_write_proposal_row_to_dict(r) for r in rows
        ],
    }


@app.get(
    "/api/file-write-proposals/{proposal_id}/diff",
    dependencies=[Depends(require_token)],
)
async def file_write_proposal_diff(proposal_id: int) -> dict[str, Any]:
    """Return `{scope, path, before, after}` for a proposal so the UI
    can render a side-by-side diff.

    `before` is the current file content read fresh from disk (or
    `None` if the file doesn't exist yet — Step 6 UI suppresses the
    diff and falls back to a plain proposed-content render in that
    case). `after` is the proposed content. The fresh read avoids
    DB-cached staleness if the file was edited (Files pane, manual
    cloud-drive sync, etc.) between propose and approve.
    """
    c = await configured_conn()
    try:
        cur = await c.execute(
            truthmod.SELECT_PROPOSAL_SQL, (proposal_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        raise HTTPException(404, detail=f"proposal {proposal_id} not found")
    proposal = truthmod.file_write_proposal_row_to_dict(row)
    try:
        target = truthmod.resolve_target_path(proposal)
    except truthmod.FileWriteProposalBadRequest as e:
        # Row references a scope/path the resolver can't honour. The UI
        # still wants a render, so surface the bad-request as 400 and
        # let it fall back to plain proposed-content view.
        raise HTTPException(400, detail=str(e))
    try:
        before: str | None = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        before = None
    except OSError as e:
        raise HTTPException(500, detail=f"read failed: {e}")
    return {
        "id": proposal_id,
        "scope": proposal["scope"],
        "path": proposal["path"],
        "before": before,
        "after": proposal["proposed_content"],
    }


async def _resolve_file_write_proposal_http(
    proposal_id: int,
    *,
    new_status: str,
    note: str | None,
    actor: dict[str, Any],
) -> dict[str, Any]:
    """Thin HTTP wrapper around
    `truthmod.resolve_file_write_proposal` — translates the resolver's
    exception types into HTTP status codes."""
    try:
        return await truthmod.resolve_file_write_proposal(
            proposal_id, new_status=new_status, note=note, actor=actor,
        )
    except truthmod.FileWriteProposalNotFound as e:
        raise HTTPException(404, detail=str(e))
    except truthmod.FileWriteProposalConflict as e:
        raise HTTPException(409, detail=str(e))
    except truthmod.FileWriteProposalBadRequest as e:
        raise HTTPException(400, detail=str(e))


@app.post(
    "/api/file-write-proposals/{proposal_id}/approve",
    dependencies=[Depends(require_token)],
)
async def approve_file_write_proposal(
    proposal_id: int,
    body: FileWriteProposalResolution | None = None,
    actor: dict[str, Any] = Depends(audit_actor),
) -> dict[str, Any]:
    note = body.note if body else None
    return await _resolve_file_write_proposal_http(
        proposal_id, new_status="approved", note=note, actor=actor,
    )


@app.post(
    "/api/file-write-proposals/{proposal_id}/deny",
    dependencies=[Depends(require_token)],
)
async def deny_file_write_proposal(
    proposal_id: int,
    body: FileWriteProposalResolution | None = None,
    actor: dict[str, Any] = Depends(audit_actor),
) -> dict[str, Any]:
    note = body.note if body else None
    return await _resolve_file_write_proposal_http(
        proposal_id, new_status="denied", note=note, actor=actor,
    )


@app.post("/api/wiki/reindex", dependencies=[Depends(require_token)])
async def wiki_reindex() -> dict[str, Any]:
    """Manual rebuild of /data/wiki/INDEX.md.

    The PostToolUse hook in [server/agents.py](server/agents.py) handles
    the agent-Write case and the file-write endpoint above handles the
    UI case, but external writers (cloud-drive sync from another machine,
    snapshot restore, manual `cp` into the wiki tree) bypass both.
    This endpoint is the catch-all 'fix it now' button for those, and
    a useful diagnostic when verifying the auto-rebuild itself.
    """
    try:
        from server.paths import update_wiki_index
        ok = update_wiki_index()
    except Exception:
        logger.exception("wiki reindex failed")
        raise HTTPException(500, detail="reindex failed")
    return {"ok": bool(ok)}


@app.get("/api/turns", dependencies=[Depends(require_token)])
async def list_turns(
    agent: str | None = None,
    limit: int = 100,
    since_id: int = 0,
) -> dict[str, Any]:
    """Per-turn ledger — one row per SDK result.

    Narrow by agent id; paginate with since_id. Returns newest first
    (most-recent-first makes 'how expensive was the last hour' queries
    a simple LIMIT without ordering server-side).
    """
    limit = max(1, min(limit, 1000))
    project_id = await resolve_active_project()
    where: list[str] = ["id > ?", "project_id = ?"]
    params: list[Any] = [since_id, project_id]
    if agent:
        where.append("agent_id = ?")
        params.append(agent)
    where_sql = " WHERE " + " AND ".join(where)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, agent_id, started_at, ended_at, duration_ms, "
            "cost_usd, session_id, num_turns, stop_reason, is_error, "
            "model, plan_mode, effort, "
            "input_tokens, output_tokens, cache_read_tokens, "
            "cache_creation_tokens, runtime, cost_basis "
            f"FROM turns{where_sql} ORDER BY id DESC LIMIT ?",
            params + [limit],
        )
        rows = await cur.fetchall()
    finally:
        await c.close()
    return {"turns": [dict(r) for r in rows]}


@app.get("/api/turns/summary", dependencies=[Depends(require_token)])
async def turns_summary(hours: int = 24) -> dict[str, Any]:
    """Per-agent aggregate over the last `hours` (default 24).

    Returns total spend / turn count / average duration, plus a
    per-agent breakdown sorted by cost descending. Cheap — runs a
    single grouped SELECT against the indexed `turns` table.

    Use `/api/turns` for row-level detail; this endpoint is for
    charts and dashboards.
    """
    hours = max(1, min(hours, 24 * 30))  # 1h..30d
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    project_id = await resolve_active_project()
    c = await configured_conn()
    try:
        # Cache columns rolled up so the UI / SQL consumers can compute
        # hit_pct = cache_read / (input + cache_read + cache_creation).
        # `input_tokens` here is the BILLED-fresh portion (already
        # excludes cache_read + cache_creation per Anthropic's usage
        # split; see `_extract_usage_claude` at agents.py:221).
        # `cost_usd` already reflects the cache discount the SDK applied,
        # so reorder-driven cache gains show up in BOTH cost_usd (lower)
        # AND cache_hit_pct (higher) — they're not double-counted.
        cur = await c.execute(
            "SELECT agent_id, COUNT(*) AS count, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd, "
            "COALESCE(AVG(duration_ms), 0) AS avg_duration_ms, "
            "SUM(is_error) AS error_count, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
            "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens "
            "FROM turns WHERE ended_at >= ? AND project_id = ? "
            "GROUP BY agent_id ORDER BY cost_usd DESC",
            (cutoff, project_id),
        )
        per_agent = [dict(r) for r in await cur.fetchall()]
        for row in per_agent:
            denom = (
                int(row.get("input_tokens") or 0)
                + int(row.get("cache_read_tokens") or 0)
                + int(row.get("cache_creation_tokens") or 0)
            )
            row["cache_hit_pct"] = (
                round(100.0 * int(row["cache_read_tokens"]) / denom, 1)
                if denom > 0
                else None
            )
        cur = await c.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(cost_usd), 0) AS cost_usd, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
            "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens "
            "FROM turns WHERE ended_at >= ? AND project_id = ?",
            (cutoff, project_id),
        )
        total_row = dict(await cur.fetchone())
        _denom_total = (
            int(total_row.get("input_tokens") or 0)
            + int(total_row.get("cache_read_tokens") or 0)
            + int(total_row.get("cache_creation_tokens") or 0)
        )
        total_cache_hit_pct = (
            round(100.0 * int(total_row["cache_read_tokens"]) / _denom_total, 1)
            if _denom_total > 0
            else None
        )

        # Audit-item-22 (CODEX_RUNTIME_SPEC.md §G.3): split totals by
        # runtime + cost_basis so the EnvPane can render the
        # plan-included token meter alongside the USD spend meter.
        # COALESCE the runtime/cost_basis since legacy rows pre-PR3
        # may have NULLs.
        cur = await c.execute(
            "SELECT COALESCE(runtime, 'claude') AS runtime, "
            "COUNT(*) AS count, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens "
            "FROM turns WHERE ended_at >= ? AND project_id = ? "
            "GROUP BY COALESCE(runtime, 'claude') "
            "ORDER BY cost_usd DESC",
            (cutoff, project_id),
        )
        by_runtime = [dict(r) for r in await cur.fetchall()]
        cur = await c.execute(
            "SELECT COALESCE(cost_basis, 'token_priced') AS cost_basis, "
            "COUNT(*) AS count, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens "
            "FROM turns WHERE ended_at >= ? AND project_id = ? "
            "GROUP BY COALESCE(cost_basis, 'token_priced')",
            (cutoff, project_id),
        )
        by_cost_basis = [dict(r) for r in await cur.fetchall()]
        # Surface the plan-included token total at the top level so
        # the UI doesn't need to filter by_cost_basis client-side.
        plan_included_token_total = sum(
            int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0)
            for row in by_cost_basis
            if row.get("cost_basis") == "plan_included"
        )
    finally:
        await c.close()
    return {
        "window_hours": hours,
        "since": cutoff,
        "total_turns": int(total_row["count"] or 0),
        "total_cost_usd": float(total_row["cost_usd"] or 0),
        "total_input_tokens": int(total_row.get("input_tokens") or 0),
        "total_cache_read_tokens": int(total_row.get("cache_read_tokens") or 0),
        "total_cache_creation_tokens": int(total_row.get("cache_creation_tokens") or 0),
        "total_cache_hit_pct": total_cache_hit_pct,
        "per_agent": per_agent,
        "by_runtime": by_runtime,
        "by_cost_basis": by_cost_basis,
        "plan_included_token_total": plan_included_token_total,
    }


@app.get("/api/turns/by-project", dependencies=[Depends(require_token)])
async def turns_by_project() -> dict[str, Any]:
    """Per-project today/total spend breakdown for the EnvPane cost
    section's project dropdown. Honors `cost_reset_at` (global) and
    `cost_reset_at_<project_id>` (per-project) timestamps from
    team_config so a "reset" zeroes the displayed today_usd for
    affected projects.

    `today_usd` and `today_turns` reflect rows since the latest
    applicable reset (or UTC day start, whichever is later).
    `total_usd` and `total_turns` are unfiltered all-time figures.

    Returns:
      {
        "projects": [
          {"id": "...", "name": "...",
           "today_usd": 1.23, "today_turns": 5,
           "total_usd": 12.34, "total_turns": 87},
          ...
        ],
        "team": {"today_usd": 1.23, "today_turns": 5,
                 "total_usd": 12.34, "total_turns": 87},
        "resets": {"all": "<iso>"|"", "<project_id>": "<iso>", ...},
      }

    The `team` block is the SUM of project today_usd values (so a
    project reset reduces team today_usd correspondingly), matching
    the spec the user asked for: "ALL is the sum of the projects."
    """
    from server.agents import _today_utc_start_iso, _load_cost_resets
    today_start = _today_utc_start_iso()
    global_reset, per_project_resets = await _load_cost_resets()
    base_window = max(today_start, global_reset) if global_reset else today_start

    c = await configured_conn()
    try:
        # Include archived projects: their historical turn rows still
        # contribute to the team-wide `_today_spend()` used by the
        # cap check, so excluding them here would make
        # `byProject.team.today_usd` < `serverStatus.caps.team_today_usd`
        # whenever an archived project had pre-reset spend. The
        # `archived` flag is surfaced per row so the UI can dim
        # archived options if it wants to.
        cur = await c.execute(
            "SELECT id, name, archived FROM projects "
            "ORDER BY archived ASC, name COLLATE NOCASE"
        )
        proj_rows = [dict(r) for r in await cur.fetchall()]

        cur = await c.execute(
            "SELECT project_id, "
            "COUNT(*) AS turns, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd "
            "FROM turns GROUP BY project_id"
        )
        totals_by_project = {
            (dict(r)["project_id"] or ""): dict(r)
            for r in await cur.fetchall()
        }

        # Per-project today_usd: each project gets its own window
        # (max of base_window and its per-project reset). One query
        # per project keeps the SQL simple — count is bounded
        # (~handful of projects).
        today_by_project: dict[str, dict[str, float]] = {}
        for p in proj_rows:
            pid = p["id"]
            window = base_window
            ppr = per_project_resets.get(pid) or ""
            if ppr and ppr > window:
                window = ppr
            cur = await c.execute(
                "SELECT COUNT(*) AS turns, "
                "COALESCE(SUM(cost_usd), 0) AS cost_usd "
                "FROM turns WHERE project_id = ? AND ended_at >= ?",
                (pid, window),
            )
            today_by_project[pid] = dict(await cur.fetchone())
    finally:
        await c.close()

    projects_out = []
    team_today_usd = 0.0
    team_today_turns = 0
    team_total_usd = 0.0
    team_total_turns = 0
    for p in proj_rows:
        pid = p["id"]
        today = today_by_project.get(pid) or {"turns": 0, "cost_usd": 0.0}
        total = totals_by_project.get(pid) or {"turns": 0, "cost_usd": 0.0}
        today_usd = float(today["cost_usd"] or 0)
        today_turns = int(today["turns"] or 0)
        total_usd = float(total["cost_usd"] or 0)
        total_turns = int(total["turns"] or 0)
        projects_out.append({
            "id": pid,
            "name": p["name"],
            "archived": bool(p.get("archived")),
            "today_usd": today_usd,
            "today_turns": today_turns,
            "total_usd": total_usd,
            "total_turns": total_turns,
        })
        team_today_usd += today_usd
        team_today_turns += today_turns
        team_total_usd += total_usd
        team_total_turns += total_turns

    resets = {"all": global_reset}
    for pid, ts in per_project_resets.items():
        if ts:
            resets[pid] = ts
    return {
        "projects": projects_out,
        "team": {
            "today_usd": round(team_today_usd, 6),
            "today_turns": team_today_turns,
            "total_usd": round(team_total_usd, 6),
            "total_turns": team_total_turns,
        },
        "resets": resets,
    }


class TurnsResetRequest(BaseModel):
    scope: str = Field(
        ...,
        description=(
            "'all' to reset the team-wide today counter (and shadow every "
            "per-project reset), or a project id to reset just that project."
        ),
    )


@app.post("/api/turns/reset", dependencies=[Depends(require_token)])
async def reset_turns_today(
    req: TurnsResetRequest,
    actor: dict = Depends(audit_actor),
) -> dict[str, Any]:
    """Move the "since when" timestamp used by today_usd / cap-check
    queries to NOW. Historical turn rows are NOT deleted — only the
    display window shifts forward.

    Scope semantics:
    - "all" → writes `cost_reset_at` (global). Every project shows
      today_usd=0 immediately. Per-project reset rows older than the
      new global reset become inert (MAX wins) but are not deleted —
      a future per-project reset can still override the global.
    - "<project_id>" → writes `cost_reset_at_<project_id>`. Only that
      project's display zeroes; the team total drops by that
      project's pre-reset spend (since "team = sum of projects").

    Effect on caps: yes — a reset gives the team / agent fresh
    headroom for the rest of the UTC day. Use deliberately.
    """
    scope = (req.scope or "").strip()
    if not scope:
        raise HTTPException(400, detail="scope is required")
    now_iso = datetime.now(timezone.utc).isoformat()
    if scope == "all":
        key = "cost_reset_at"
    else:
        # Per-project reset — verify the project exists so we don't
        # write garbage keys.
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT 1 FROM projects WHERE id = ? LIMIT 1",
                (scope,),
            )
            if not await cur.fetchone():
                raise HTTPException(
                    404, detail=f"project '{scope}' not found"
                )
        finally:
            await c.close()
        key = f"cost_reset_at_{scope}"

    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO team_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, now_iso),
        )
        await c.commit()
    finally:
        await c.close()

    await bus.publish({
        "ts": now_iso,
        "agent_id": "system",
        "type": "cost_reset",
        "scope": scope,
        "actor": actor,
    })
    return {"ok": True, "scope": scope, "reset_at": now_iso}


@app.get("/api/events", dependencies=[Depends(require_token)])
async def list_events(
    agent: str | None = None,
    type: str | None = None,
    since_id: int = 0,
    before_id: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Return event history for a pane to restore when it opens.

    Returns the MOST RECENT `limit` events (ordered chronologically oldest
    → newest in the response) with id > since_id. Pass since_id=0 to get
    the tail of the log; pass the largest id you've seen to poll for new
    rows (used in future polling/paginating flows).

    Pass `before_id` to walk backwards through history one page at a
    time — returns events with id < before_id, still ordered oldest →
    newest in the response. The pane's "load older" button uses this.

    Optional `type` narrows to a single event type (e.g.
    'human_attention') — useful when the UI wants to surface historical
    escalations across page reloads.
    """
    limit = max(1, min(limit, 1000))
    project_id = await resolve_active_project()
    # Scope to the active project so opening a pane after a project
    # switch doesn't surface another project's history (§16).
    where_parts: list[str] = ["project_id = ?", "id > ?"]
    params: list[Any] = [project_id, since_id]
    if before_id is not None and before_id > 0:
        where_parts.append("id < ?")
        params.append(before_id)
    if agent:
        # Fan-out: include events where this agent is the recipient,
        # not only the actor. Mirrors the WS-side fan-out so opening a
        # pane's history matches what the pane would have shown live.
        #   - type=message_sent & .to matches (or 'broadcast')
        #   - type=task_assigned & .to matches
        #   - type=task_updated & .owner matches (Coach cancelling a
        #     Player's task should show in the Player's history too)
        # We filter against the indexed payload_to / payload_owner
        # generated columns rather than json_extract() so the planner
        # can use idx_events_to / idx_events_owner. Old DBs that
        # haven't migrated the columns will still work — the columns
        # are just NULL there and the OR-branches contribute nothing,
        # which is the behaviour we want until init_db catches up.
        # v2 §7.2.1 — when fetching Coach's history, every Player
        # completion event is cc'd to Coach (matches the WS-side fan-out
        # in app.js). The four event types are listed unconditionally
        # so Coach's pane on reload mirrors what it showed live.
        where_parts.append(
            "("
            "agent_id = ?"
            " OR (type = 'message_sent' AND ("
            "     payload_to = ? OR payload_to = 'broadcast'"
            "))"
            " OR (type = 'task_assigned' AND payload_to = ?)"
            " OR (type = 'task_role_assigned' AND payload_to = ?)"
            " OR (type = 'task_role_called' AND payload_to = ?)"
            " OR (type = 'task_role_claimed' AND payload_to = ?)"
            " OR (type = 'task_updated' AND payload_owner = ?)"
            " OR (type = 'agent_model_set' AND payload_to = ?)"
            " OR (type = 'agent_effort_set' AND payload_to = ?)"
            " OR (type = 'agent_plan_mode_set' AND payload_to = ?)"
            " OR (type = 'agent_thinking_set' AND payload_to = ?)"
            " OR (type IN ('truthscore_started','truthscore_completed',"
            "             'truthscore_failed') AND payload_to = ?)"
            " OR (type IN ('commit_pushed','task_spec_written',"
            "             'audit_report_submitted','task_role_completed')"
            "     AND ? = 'coach')"
            " OR (type IN ('audit_report_submitted','task_spec_written',"
            "             'task_role_completed')"
            "     AND payload_to = ?)"
            ")"
        )
        params.extend([
            agent, agent, agent, agent, agent, agent, agent, agent, agent, agent,
            agent, agent, agent, agent,
        ])
    if type:
        where_parts.append("type = ?")
        params.append(type)
    where = " WHERE " + " AND ".join(where_parts)

    c = await configured_conn()
    try:
        # Fetch newest N by id DESC, then reverse to chronological order.
        cur = await c.execute(
            f"SELECT id, ts, agent_id, type, payload FROM events{where} "
            f"ORDER BY id DESC LIMIT ?",
            params + [limit],
        )
        rows = list(reversed(await cur.fetchall()))
    finally:
        await c.close()

    events = []
    for r in rows:
        d = dict(r)
        try:
            payload = json.loads(d["payload"])
        except Exception:
            payload = {"raw": d["payload"]}
        events.append(
            {
                "id": d["id"],
                "ts": d["ts"],
                "agent_id": d["agent_id"],
                "type": d["type"],
                "payload": payload,
            }
        )
    return {"events": events}


# ------------------------------------------------------------------
# Attachments (image paste in prompts, v2 feature)
# ------------------------------------------------------------------


MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024  # 30 MB; adjust via spec, not env.


def _matches_magic_bytes(head: bytes, ext: str) -> bool:
    """Verify file content matches the declared image extension.

    Closes the "polyglot upload" hole — without this check, an attacker
    can rename a JS / HTML / SVG file to `.png` and the server stores
    it with `Content-Type: image/png`. Most browsers honor the magic
    bytes over Content-Type for image rendering, but a quirky one
    (or a future fetch-as-text retrieval) would execute the script.

    Magic byte references:
      - PNG:  89 50 4E 47 0D 0A 1A 0A
      - JPEG: FF D8 FF
      - GIF:  GIF87a / GIF89a
      - WebP: RIFF....WEBP (4 + 4 + 4 layout)
    """
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in ("jpg", "jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == "gif":
        return head.startswith(b"GIF87a") or head.startswith(b"GIF89a")
    if ext == "webp":
        # 'RIFF' + 4-byte size + 'WEBP'
        return (
            len(head) >= 12
            and head[0:4] == b"RIFF"
            and head[8:12] == b"WEBP"
        )
    return False


@app.post("/api/attachments", dependencies=[Depends(require_token)])
async def upload_attachment(file: UploadFile = File(...)) -> dict[str, Any]:
    """Accept an image upload, store under the active project's
    `/data/projects/<slug>/attachments/<id>.<ext>` (PROJECTS_SPEC.md §4).

    Enforces:
      - extension allowlist (PNG / JPEG / GIF / WebP),
      - magic-byte match against the declared extension (defends
        against polyglot files),
      - 30 MB size cap (streamed, aborts mid-upload if exceeded so a
        large attacker payload doesn't tie up the disk).

    Returns a stable id + filesystem path. The caller (frontend) includes
    the path in the prompt text so the agent can Read the image. Pastes
    in conversations from a different project will not resolve after a
    project switch — same trade-off as cross-project file links per §7.
    """
    filename = file.filename or "upload"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            400,
            detail=f"unsupported extension '{ext}'. Allowed: {sorted(ALLOWED_EXT)}",
        )

    project_id = await resolve_active_project()
    attachments_dir = _attachments_dir_for(project_id)
    att_id = uuid.uuid4().hex[:12]
    target = attachments_dir / f"{att_id}.{ext}"
    attachments_dir.mkdir(parents=True, exist_ok=True)

    # Stream into the file with a per-chunk size guard. Read the first
    # chunk before opening the destination so a magic-byte mismatch
    # doesn't leave a zero-byte file on disk. 64 KB chunks are large
    # enough to amortize syscall overhead and small enough that a 30 MB
    # cap aborts within ~480 chunks. UploadFile.read() is async and
    # delegates to its SpooledTemporaryFile under the hood — no
    # asyncio.to_thread wrapper needed.
    CHUNK = 64 * 1024
    total = 0
    try:
        first = await file.read(CHUNK)
        if not first:
            raise HTTPException(400, detail="empty upload")
        if not _matches_magic_bytes(first[:16], ext):
            raise HTTPException(
                400,
                detail=(
                    f"file content does not match declared extension "
                    f"'.{ext}' (magic-byte check). Refused to store."
                ),
            )
        if len(first) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(
                413,
                detail=(
                    f"upload exceeds {MAX_ATTACHMENT_BYTES} byte cap"
                ),
            )
        total = len(first)
        with target.open("wb") as fp:
            fp.write(first)
            while True:
                chunk = await file.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ATTACHMENT_BYTES:
                    fp.close()
                    try:
                        target.unlink()
                    except OSError:
                        pass
                    raise HTTPException(
                        413,
                        detail=(
                            f"upload exceeds {MAX_ATTACHMENT_BYTES} "
                            "byte cap"
                        ),
                    )
                fp.write(chunk)
    except HTTPException:
        raise
    except Exception:
        # Mid-stream failure: tidy up the partial file and surface a
        # generic 500 — don't leak the stack to the caller.
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        logger.exception("attachment upload failed mid-stream")
        raise HTTPException(500, detail="upload failed")

    from server.attachments_signing import mint_signed_url
    filename = f"{att_id}.{ext}"
    return {
        "id": att_id,
        "filename": filename,
        "path": str(target),
        # Legacy URL with `?token=` is kept temporarily for any cached
        # client; the UI prefers `signed_url` and the auth-required
        # endpoint will be removed in a future release once we're sure
        # nothing else points at it.
        "url": f"/api/attachments/{filename}",
        "signed_url": mint_signed_url(filename),
        "size": total,
        "media_type": f"image/{ext if ext != 'jpg' else 'jpeg'}",
    }


def _validate_attachment_filename(filename: str) -> str:
    """Reject path traversal + unknown extensions for attachment paths.
    Shared between the legacy and signed serve endpoints."""
    if "/" in filename or ".." in filename or "\\" in filename:
        raise HTTPException(400, detail="invalid filename")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXT:
        raise HTTPException(404)
    return ext


@app.get(
    "/api/attachments/{filename}/signed-url",
    dependencies=[Depends(require_token)],
)
async def get_attachment_signed_url(filename: str) -> dict[str, Any]:
    """Re-mint a fresh signed URL for an existing attachment.

    Used by the UI when re-rendering historical pane content whose
    original `signed_url` from the upload response has aged out. The
    auth dependency stops anyone but the harness operator from
    minting on demand, while the signed URL itself remains
    short-lived and limited to the specific filename it was minted
    for.
    """
    _validate_attachment_filename(filename)
    project_id = await resolve_active_project()
    target = _attachments_dir_for(project_id) / filename
    if not target.exists() or not target.is_file():
        raise HTTPException(404)
    from server.attachments_signing import mint_signed_url
    return {"filename": filename, "signed_url": mint_signed_url(filename)}


@app.get("/api/attachments/{filename}/signed")
async def get_attachment_signed(
    filename: str,
    exp: int = Query(...),
    sig: str = Query(...),
):
    """Serve attachment bytes WITHOUT requiring the bearer token —
    auth is via the HMAC `sig` (with `exp`) instead. Closes the audit
    finding "bearer token in URL" for image loads in browser history,
    proxy logs, screenshots, etc.

    The signed URL is minted by the upload endpoint (or by the
    auth'd `/signed-url` re-mint endpoint) and lives for ~5 minutes.
    """
    from server.attachments_signing import verify_signed
    if not verify_signed(filename, exp, sig):
        # Don't tell the caller WHICH part failed (expired vs forged)
        # — same response shape stops a probing attacker from
        # learning whether their guess timed out or was simply wrong.
        raise HTTPException(403, detail="invalid or expired signature")
    ext = _validate_attachment_filename(filename)
    project_id = await resolve_active_project()
    target = _attachments_dir_for(project_id) / filename
    if not target.exists() or not target.is_file():
        raise HTTPException(404)
    media_type = f"image/{ext if ext != 'jpg' else 'jpeg'}"
    return FileResponse(target, media_type=media_type)


@app.get("/api/attachments/{filename}")
async def get_attachment(filename: str, token: str | None = Query(default=None)):
    """Legacy bearer-token-in-URL serve endpoint.

    Kept for one release so cached UI bundles + any external code
    pointing at the old URL keep working. New uploads return
    `signed_url` instead; the UI prefers that. Removal is tracked in
    the threat-model section of Docs/TOT-specs.md.
    """
    # `<img src=...>` browser loads can't set the Authorization header
    # (browsers only attach it on fetch/XHR), so we accept the token via
    # `?token=` query string the same way the /ws endpoint does. The UI
    # appends `?token=<...>` when rendering thumbnails. Bearer header
    # still works for fetch-based callers.
    if HARNESS_TOKEN and token != HARNESS_TOKEN:
        raise HTTPException(401, detail="invalid or missing token")
    ext = _validate_attachment_filename(filename)
    project_id = await resolve_active_project()
    target = _attachments_dir_for(project_id) / filename
    if not target.exists() or not target.is_file():
        raise HTTPException(404)
    media_type = f"image/{ext if ext != 'jpg' else 'jpeg'}"
    return FileResponse(target, media_type=media_type)


# ------------------------------------------------------------------
# WebSocket event stream
# ------------------------------------------------------------------


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket, token: str | None = Query(default=None)) -> None:
    if HARNESS_TOKEN and token != HARNESS_TOKEN:
        # Browsers can't set Authorization headers on WS connections, so
        # we accept ?token=<...> in the URL instead.
        await ws.close(code=4401, reason="invalid or missing token")
        return
    await ws.accept()
    q = bus.subscribe()
    try:
        await ws.send_json(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "agent_id": "system",
                "type": "connected",
            }
        )
        while True:
            # Heartbeat: if nothing to send for WS_PING_INTERVAL
            # seconds, send a ping so the client can detect zombie
            # connections where TCP reports alive but no traffic flows
            # (common with some intermediate proxies).
            try:
                event = await asyncio.wait_for(q.get(), timeout=30.0)
            except asyncio.TimeoutError:
                await ws.send_json(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "agent_id": "system",
                        "type": "ping",
                    }
                )
                continue
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    finally:
        bus.unsubscribe(q)

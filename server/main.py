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
    _v_app = _stamp("app.js")
    _v_css = _stamp("style.css")
    INDEX_HTML = (
        _index_raw
        .replace('"/static/app.js"', f'"/static/app.js?v={_v_app}"')
        .replace('"/static/style.css"', f'"/static/style.css?v={_v_css}"')
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
    # from kDrive) are reflected on the first /api/files/tree hit.
    try:
        from server.paths import update_wiki_index
        update_wiki_index()
    except Exception:
        logger.exception("update_wiki_index failed (non-fatal)")
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
    # Project-repo clone + per-slot worktrees (no-op if HARNESS_PROJECT_REPO
    # unset). Logged but errors don't abort startup — agents can still run
    # in plain dirs if worktree setup fails.
    workspaces_status = await ensure_workspaces()
    logger.info("workspaces: %r", workspaces_status)
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
    # Background tasks: kDrive snapshot + project / global file sync
    # (PROJECTS_SPEC.md §5). The legacy flush_loop / uploads_pull_loop /
    # outputs_push_loop are retired — per-project sync covers the same
    # surface under the spec's TOT/projects/<slug>/ layout.
    snapshot_task = asyncio.create_task(snapshot_loop())
    project_sync_task = asyncio.create_task(project_sync_loop())
    global_sync_task = asyncio.create_task(global_sync_loop())
    recurrence_task = asyncio.create_task(recurrence_scheduler_loop())
    stale_task_task = asyncio.create_task(stale_task_watch_loop())
    trim_task = asyncio.create_task(events_trim_loop())
    att_trim_task = asyncio.create_task(attachments_trim_loop())
    sessions_trim_task = asyncio.create_task(sessions_trim_loop())
    from server.telegram import start_telegram_bridge, stop_telegram_bridge
    # Telegram bridge owns its own task handle (so the UI can reload it
    # live via /api/team/telegram). Lifespan only kicks it off + tears
    # it down — it isn't tracked in bg_tasks.
    try:
        await start_telegram_bridge()
    except Exception:
        logger.exception("telegram bridge failed to start (non-fatal)")
    bg_tasks = (snapshot_task, project_sync_task, global_sync_task, recurrence_task, stale_task_task, trim_task, att_trim_task, sessions_trim_task)
    try:
        yield
    finally:
        try:
            await stop_telegram_bridge()
        except Exception:
            logger.exception("telegram bridge shutdown failed")
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


# ------------------------------------------------------------------
# Request models
# ------------------------------------------------------------------


class StartAgentRequest(BaseModel):
    agent_id: str = Field(default="p1", pattern=r"^(coach|p([1-9]|10))$")
    prompt: str = Field(min_length=1, max_length=20_000)
    # Per-turn overrides set via the pane settings popover. Any omitted
    # falls back to the SDK / Dockerfile defaults.
    model: str | None = Field(default=None, max_length=120)
    plan_mode: bool = False
    effort: int | None = Field(default=None, ge=1, le=4)


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=10_000)
    parent_id: str | None = None
    priority: str = Field(default="normal", pattern=r"^(low|normal|high|urgent)$")


# ------------------------------------------------------------------
# Pages + health
# ------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return INDEX_HTML


# Health-check caches. Avoid hammering kDrive / spawning subprocesses on
# every probe (Zeabur or external monitors may poll every 30s).
_CLAUDE_VERSION_CACHE: dict[str, object] = {}  # populated once per process
_WEBDAV_PROBE_CACHE: dict[str, object] = {"ts": 0.0, "ok": None}
_WEBDAV_PROBE_TTL_SECONDS = 60.0


@app.get("/api/health")
async def health() -> JSONResponse:
    """Per-subsystem readiness probe. Returns 200 if everything required
    is green, 503 if any subsystem is failing, with a `checks` object
    detailing each. Skipped subsystems (webdav/workspaces when unconfigured)
    don't fail the overall ok flag.
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

    # 4. kDrive — only check if configured. Cached for 60s to avoid
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

    # 5. External MCP servers — reports what HARNESS_MCP_CONFIG yielded
    # at load time. Purely informational (we don't fail health on a
    # missing config — it's optional). Re-reads the file each probe so
    # edits since last boot are visible without restart.
    mcp_cfg_path = os.environ.get("HARNESS_MCP_CONFIG", "").strip()
    if mcp_cfg_path:
        from server.mcp_config import load_external_servers
        # Do a parallel sanity read so we can distinguish file-missing /
        # parse-error / no-servers-in-file from each other —
        # load_external_servers collapses all three to (empty, empty)
        # and only logs. Report here so the UI surfaces the failure.
        mcp_status: dict[str, Any] = {"config_path": mcp_cfg_path}
        cfg_file = Path(mcp_cfg_path)
        if not cfg_file.is_file():
            mcp_status.update({"ok": False, "error": "file does not exist"})
        else:
            try:
                raw = cfg_file.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("top-level must be a JSON object")
                servers_in = parsed.get("servers")
                if servers_in is not None and not isinstance(servers_in, dict):
                    raise ValueError("'servers' key must be an object")
                servers, tool_names = load_external_servers()
                mcp_status.update({
                    "ok": True,
                    "server_count": len(servers),
                    "servers": sorted(servers.keys()),
                    "allowed_tool_count": len(tool_names),
                })
            except Exception as e:
                mcp_status.update({
                    "ok": False,
                    "error": f"{type(e).__name__}: {str(e)[:200]}",
                })
        if not mcp_status.get("ok"):
            overall_ok = False
        checks["mcp_external"] = mcp_status
    else:
        checks["mcp_external"] = {
            "ok": True,
            "skipped": True,
            "reason": "HARNESS_MCP_CONFIG not set — only the in-process 'coord' server is active",
        }

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

    # 7. Workspaces — only check if HARNESS_PROJECT_REPO set
    ws_status = get_workspaces_status()
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
        "workspaces": get_workspaces_status(),
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
    minutes: int | None = Field(default=None, ge=1, le=525_600)
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


# Per-role default models — one per role (coach, players). Per-pane
# overrides still win; these kick in only when the pane hasn't chosen
# a specific model.
_ROLE_MODEL_DEFAULTS = {
    "coach": "claude-opus-4-7",
    "players": "claude-sonnet-4-6",
}
_ROLE_CODEX_MODEL_DEFAULTS = {
    "coach": "",
    "players": "",
}
_CLAUDE_MODEL_WHITELIST = {
    "",
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
}
_CODEX_MODEL_WHITELIST = {
    "",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.3-codex",
    "gpt-5.2-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex",
    "gpt-5.1-codex-mini",
    "gpt-5-codex",
}
# Model names we let the UI pick. Keep in sync with MODEL_OPTIONS and
# CODEX_MODEL_OPTIONS in app.js. Empty string means "SDK default".
_MODEL_WHITELIST = _CLAUDE_MODEL_WHITELIST | _CODEX_MODEL_WHITELIST


class TeamModelsWrite(BaseModel):
    coach: str = Field("", description="Default model for Coach. Empty = SDK default.")
    players: str = Field("", description="Default model for p1..p10. Empty = SDK default.")
    coach_codex: str = Field("", description="Default Codex model for Coach. Empty = SDK default.")
    players_codex: str = Field("", description="Default Codex model for p1..p10. Empty = SDK default.")


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
        "suggested": _ROLE_MODEL_DEFAULTS,
        "suggested_codex": _ROLE_CODEX_MODEL_DEFAULTS,
        "available": sorted(_CLAUDE_MODEL_WHITELIST - {""}),
        "available_codex": sorted(_CODEX_MODEL_WHITELIST - {""}),
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
    return {"ok": True, "saved": saved, "secret_warnings": warnings}


class MCPServerPatch(BaseModel):
    enabled: bool | None = None
    allowed_tools: list[str] | None = None


@app.patch("/api/mcp/servers/{name}", dependencies=[Depends(require_token)])
async def patch_mcp_server(
    name: str,
    req: MCPServerPatch,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Toggle enabled and/or update the allowed_tools list for an
    existing row. Leaves config_json alone."""
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
    if not updates:
        raise HTTPException(400, detail="nothing to update")
    updates.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')")
    params.append(name)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
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
            "actor": actor,
        }
    )
    return {"ok": True}


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
# Project repo (HARNESS_PROJECT_REPO replacement, DB-backed)
# ------------------------------------------------------------------


class TeamRepoWrite(BaseModel):
    repo: str = Field(
        "",
        description="Project repo URL. Include PAT via ${VAR} placeholder for auth. Empty clears.",
    )
    branch: str = Field(
        "",
        description="Branch to base per-Player worktrees on. Empty = 'main'.",
    )
    allow_secrets: bool = Field(
        default=False,
        description="Override the raw-token warning and save anyway.",
    )


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


@app.get("/api/team/repo", dependencies=[Depends(require_token)])
async def get_team_repo() -> dict[str, object]:
    """Return the currently-active repo/branch + their source
    (db vs env vs unset), so the UI can show where the setting lives."""
    db_repo = await _read_team_config_str("project_repo")
    db_branch = await _read_team_config_str("project_branch")
    env_repo = os.environ.get("HARNESS_PROJECT_REPO", "").strip()
    env_branch = os.environ.get("HARNESS_PROJECT_BRANCH", "").strip()
    if db_repo:
        active_repo, repo_source = db_repo, "db"
    elif env_repo:
        active_repo, repo_source = env_repo, "env"
    else:
        active_repo, repo_source = "", "unset"
    if db_branch:
        active_branch, branch_source = db_branch, "db"
    elif env_branch:
        active_branch, branch_source = env_branch, "env"
    else:
        active_branch, branch_source = "main", "default"
    return {
        "repo": active_repo,
        "repo_masked": _mask_repo_url(active_repo),
        "repo_source": repo_source,
        "branch": active_branch,
        "branch_source": branch_source,
        "env_repo_set": bool(env_repo),
    }


@app.put("/api/team/repo", dependencies=[Depends(require_token)])
async def set_team_repo(
    req: TeamRepoWrite,
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Save repo + branch to team_config. Takes effect on the NEXT
    container restart — existing clones + worktrees stay pointing at
    the old remote. Secret-scan rejects raw tokens unless
    allow_secrets=true."""
    from server.mcp_config import detect_secrets
    repo = (req.repo or "").strip()
    branch = (req.branch or "").strip()
    warnings = detect_secrets(repo) if repo else []
    if warnings and not req.allow_secrets:
        raise HTTPException(
            400,
            detail={
                "secret_warnings": warnings,
                "hint": "Replace raw tokens with ${VAR} placeholders (the "
                "placeholder expands at clone time from the Zeabur env). "
                "Re-submit with allow_secrets=true to override.",
            },
        )
    await _write_team_config_str("project_repo", repo)
    await _write_team_config_str("project_branch", branch)
    # Invalidate the in-process cache so project_configured() reflects
    # the new value on the next call. The clone / worktree setup still
    # requires a restart — refresh just keeps the DB and cache in sync.
    from server.workspaces import refresh_repo_cache
    refresh_repo_cache()
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "team_repo_updated",
            "repo_masked": _mask_repo_url(repo),
            "branch": branch or "main",
            "actor": actor,
        }
    )
    return {"ok": True, "secret_warnings": warnings}


@app.post("/api/team/repo/provision", dependencies=[Depends(require_token)])
async def provision_team_repo(
    actor: dict = Depends(audit_actor),
) -> dict[str, object]:
    """Run `ensure_workspaces()` live so a repo URL saved via the UI
    takes effect without a container restart. Idempotent — existing
    `.git` worktrees are left alone; only missing ones get cloned /
    added. Can take tens of seconds for a large first clone.
    """
    from server.workspaces import ensure_workspaces, refresh_repo_cache
    # Drop any stale cache first so the call reads the latest DB value.
    refresh_repo_cache()
    try:
        status = await ensure_workspaces()
    except Exception as e:
        logger.exception("ensure_workspaces failed from /provision")
        raise HTTPException(500, detail=f"provision failed: {e}") from e
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "system",
            "type": "team_repo_provisioned",
            "configured": bool(status.get("configured")),
            "error": status.get("error"),
            "actor": actor,
        }
    )
    return {"ok": "error" not in status, "status": status}


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
      - Coach cannot coord_assign_task a task to this agent
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
    return {"ok": True, "cleared": targets, "updated": updated}


# ------------------------------------------------------------------
# Tasks
# ------------------------------------------------------------------


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
    """Create a top-level task from the UI (attributed to 'human')."""
    task_id = f"t-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:8]}"
    parent_id = req.parent_id or None
    project_id = await resolve_active_project()

    c = await configured_conn()
    try:
        if parent_id:
            cur = await c.execute(
                "SELECT id FROM tasks WHERE id = ? AND project_id = ?",
                (parent_id, project_id),
            )
            if (await cur.fetchone()) is None:
                raise HTTPException(404, detail=f"parent_id {parent_id} not found")
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, description, parent_id, priority, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, 'human')",
            (task_id, project_id, req.title, req.description, parent_id, req.priority),
        )
        await c.commit()
    finally:
        await c.close()

    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "human",
            "type": "task_created",
            "task_id": task_id,
            "title": req.title,
            "parent_id": parent_id,
            "priority": req.priority,
        }
    )
    return {"ok": True, "task_id": task_id}


@app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(require_token)])
async def cancel_task_from_human(task_id: str) -> dict[str, Any]:
    """Cancel a task from the UI. Updates the task row + clears the
    owner's current_task_id so the agent is free to claim the next one.

    Noop (but returns 200) if the task is already done or cancelled —
    the UI may race against an in-flight update."""
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
        if task["status"] in ("done", "cancelled"):
            return {"ok": True, "task_id": task_id, "already": task["status"]}
        old_status = task["status"]
        await c.execute(
            "UPDATE tasks SET status = 'cancelled' WHERE id = ? AND project_id = ?",
            (task_id, project_id),
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

    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "human",
            "type": "task_updated",
            "task_id": task_id,
            "old_status": old_status,
            "new_status": "cancelled",
            "note": "cancelled by human",
        }
    )
    return {"ok": True, "task_id": task_id, "old_status": old_status}


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

    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "human",
            "type": "message_sent",
            "message_id": msg_id,
            "to": to,
            "subject": req.subject,
            "body_preview": (req.body or "")[:120],
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
        await maybe_wake_agent(
            to,
            f"New message from the human{subj}: \"{preview_snippet}\"\n\n"
            f"Call coord_read_inbox to mark it read and see any other "
            f"queued messages, then respond.",
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

    Decisions live primarily on kDrive at
    `TOT/projects/<active>/decisions/<file>.md` with a local fallback
    under `/data/projects/<active>/decisions/`. This endpoint reads
    the local store for the active project only.
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
    except PermissionError as e:
        raise HTTPException(403, detail=str(e))
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@app.put("/api/files/write/{root}", dependencies=[Depends(require_token)])
async def files_write(
    root: str,
    req: FileWrite,
    path: str = Query(..., description="relative to root"),
) -> dict[str, Any]:
    try:
        result = await filesmod.write_text(root, path, req.content)
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


class TruthProposalResolution(BaseModel):
    note: str | None = Field(default=None, max_length=400)


@app.get("/api/truth/proposals", dependencies=[Depends(require_token)])
async def list_truth_proposals(
    status: str | None = None, limit: int = 50,
) -> dict[str, Any]:
    """List truth/ proposals for the active project, newest first.

    Filter by status (`pending` | `approved` | `denied` | `cancelled`)
    or omit to get all. Default limit 50; cap 200.
    """
    limit = max(1, min(limit, 200))
    project_id = await resolve_active_project()
    where = ["project_id = ?"]
    params: list[Any] = [project_id]
    if status:
        if status not in ("pending", "approved", "denied", "cancelled"):
            raise HTTPException(400, detail="invalid status filter")
        where.append("status = ?")
        params.append(status)
    sql = (
        "SELECT id, project_id, proposer_id, path, proposed_content, "
        "summary, status, created_at, resolved_at, resolved_by, "
        "resolved_note FROM truth_proposals WHERE "
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
            truthmod.truth_proposal_row_to_dict(r) for r in rows
        ],
    }


async def _resolve_truth_proposal_http(
    proposal_id: int,
    *,
    new_status: str,
    note: str | None,
    actor: dict[str, Any],
) -> dict[str, Any]:
    """Thin HTTP wrapper around `truthmod.resolve_truth_proposal` —
    translates the resolver's exception types into HTTP status codes."""
    try:
        return await truthmod.resolve_truth_proposal(
            proposal_id, new_status=new_status, note=note, actor=actor,
        )
    except truthmod.TruthProposalNotFound as e:
        raise HTTPException(404, detail=str(e))
    except truthmod.TruthProposalConflict as e:
        raise HTTPException(409, detail=str(e))
    except truthmod.TruthProposalBadRequest as e:
        raise HTTPException(400, detail=str(e))


@app.post(
    "/api/truth/proposals/{proposal_id}/approve",
    dependencies=[Depends(require_token)],
)
async def approve_truth_proposal(
    proposal_id: int,
    body: TruthProposalResolution | None = None,
    actor: dict[str, Any] = Depends(audit_actor),
) -> dict[str, Any]:
    note = body.note if body else None
    return await _resolve_truth_proposal_http(
        proposal_id, new_status="approved", note=note, actor=actor,
    )


@app.post(
    "/api/truth/proposals/{proposal_id}/deny",
    dependencies=[Depends(require_token)],
)
async def deny_truth_proposal(
    proposal_id: int,
    body: TruthProposalResolution | None = None,
    actor: dict[str, Any] = Depends(audit_actor),
) -> dict[str, Any]:
    note = body.note if body else None
    return await _resolve_truth_proposal_http(
        proposal_id, new_status="denied", note=note, actor=actor,
    )


@app.post("/api/wiki/reindex", dependencies=[Depends(require_token)])
async def wiki_reindex() -> dict[str, Any]:
    """Manual rebuild of /data/wiki/INDEX.md.

    The PostToolUse hook in [server/agents.py](server/agents.py) handles
    the agent-Write case and the file-write endpoint above handles the
    UI case, but external writers (kDrive sync from another machine,
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
        cur = await c.execute(
            "SELECT agent_id, COUNT(*) AS count, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd, "
            "COALESCE(AVG(duration_ms), 0) AS avg_duration_ms, "
            "SUM(is_error) AS error_count "
            "FROM turns WHERE ended_at >= ? AND project_id = ? "
            "GROUP BY agent_id ORDER BY cost_usd DESC",
            (cutoff, project_id),
        )
        per_agent = [dict(r) for r in await cur.fetchall()]
        cur = await c.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(cost_usd), 0) AS cost_usd "
            "FROM turns WHERE ended_at >= ? AND project_id = ?",
            (cutoff, project_id),
        )
        total_row = dict(await cur.fetchone())

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
        "per_agent": per_agent,
        "by_runtime": by_runtime,
        "by_cost_basis": by_cost_basis,
        "plan_included_token_total": plan_included_token_total,
    }


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
        where_parts.append(
            "("
            "agent_id = ?"
            " OR (type = 'message_sent' AND ("
            "     payload_to = ? OR payload_to = 'broadcast'"
            "))"
            " OR (type = 'task_assigned' AND payload_to = ?)"
            " OR (type = 'task_updated' AND payload_owner = ?)"
            ")"
        )
        params.extend([agent, agent, agent, agent])
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


@app.post("/api/attachments", dependencies=[Depends(require_token)])
async def upload_attachment(file: UploadFile = File(...)) -> dict[str, Any]:
    """Accept an image upload, store under the active project's
    `/data/projects/<slug>/attachments/<id>.<ext>` (PROJECTS_SPEC.md §4).

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

    with target.open("wb") as fp:
        shutil.copyfileobj(file.file, fp)

    return {
        "id": att_id,
        "filename": f"{att_id}.{ext}",
        "path": str(target),
        "url": f"/api/attachments/{att_id}.{ext}",
        "size": target.stat().st_size,
        "media_type": f"image/{ext if ext != 'jpg' else 'jpeg'}",
    }


@app.get("/api/attachments/{filename}", dependencies=[Depends(require_token)])
async def get_attachment(filename: str):
    # Reject path traversal attempts
    if "/" in filename or ".." in filename:
        raise HTTPException(400, detail="invalid filename")
    project_id = await resolve_active_project()
    target = _attachments_dir_for(project_id) / filename
    if not target.exists() or not target.is_file():
        raise HTTPException(404)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXT:
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

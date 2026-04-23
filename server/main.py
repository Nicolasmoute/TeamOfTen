from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from server.agents import (
    AGENT_DAILY_CAP_USD,
    TEAM_DAILY_CAP_USD,
    _today_spend,
    run_agent,
)
from server.db import configured_conn, init_db
from server.events import bus
from server.kdrive import kdrive
from server.sync import flush_loop, snapshot_loop
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

# If package-data shipped /static correctly, INDEX_HTML is the real page.
# If not, we want a visible error page, not an import crash that makes
# the container restart-loop with zero logs.
try:
    INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
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

# Central attachment store. Sits on the same /data volume as the SQLite DB,
# so images persist across redeploys. Lives outside any agent's workspace
# on purpose — a Player must not be able to mutate another Player's
# attachments via a git-worktree edit.
ATTACHMENTS_DIR = Path(os.environ.get("HARNESS_ATTACHMENTS_DIR", "/data/attachments"))
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    # Project-repo clone + per-slot worktrees (no-op if HARNESS_PROJECT_REPO
    # unset). Logged but errors don't abort startup — agents can still run
    # in plain dirs if worktree setup fails.
    workspaces_status = await ensure_workspaces()
    logger.info("workspaces: %r", workspaces_status)
    # Background tasks: flush event log to kDrive + hourly SQLite
    # snapshot. Both are no-ops when kDrive is disabled so running them
    # unconditionally is safe — picks up activation without restart.
    sync_task = asyncio.create_task(flush_loop())
    snapshot_task = asyncio.create_task(snapshot_loop())
    try:
        yield
    finally:
        for t in (sync_task, snapshot_task):
            t.cancel()
        for t in (sync_task, snapshot_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(
    title="TeamOfTen harness",
    version="0.2.2",
    description="Personal orchestration harness — Coach + 10 Players.",
    lifespan=lifespan,
)
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
else:
    logger.error("static dir not found at %s — /static routes will 404", STATIC_DIR)


# ------------------------------------------------------------------
# Request models
# ------------------------------------------------------------------


class StartAgentRequest(BaseModel):
    agent_id: str = Field(default="p1", pattern=r"^(coach|p([1-9]|10))$")
    prompt: str = Field(min_length=1, max_length=20_000)


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
_KDRIVE_PROBE_CACHE: dict[str, object] = {"ts": 0.0, "ok": None}
_KDRIVE_PROBE_TTL_SECONDS = 60.0


@app.get("/api/health")
async def health() -> JSONResponse:
    """Per-subsystem readiness probe. Returns 200 if everything required
    is green, 503 if any subsystem is failing, with a `checks` object
    detailing each. Skipped subsystems (kdrive/workspaces when unconfigured)
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

    # 4. kDrive — only check if configured. Cached for 60s to avoid
    # writing a probe file on every health hit.
    if kdrive.enabled:
        now = time.monotonic()
        last_ts = float(_KDRIVE_PROBE_CACHE["ts"])
        cached_ok = _KDRIVE_PROBE_CACHE["ok"]
        if cached_ok is not None and (now - last_ts) < _KDRIVE_PROBE_TTL_SECONDS:
            checks["kdrive"] = {"ok": bool(cached_ok), "cached": True}
            if not cached_ok:
                overall_ok = False
        else:
            ok = await kdrive.write_text(".harness-health-probe.txt", "ok")
            _KDRIVE_PROBE_CACHE["ts"] = now
            _KDRIVE_PROBE_CACHE["ok"] = ok
            checks["kdrive"] = {"ok": ok, "cached": False}
            if not ok:
                overall_ok = False
    else:
        checks["kdrive"] = {"ok": True, "skipped": True, "reason": kdrive.reason}

    # 5. Workspaces — only check if HARNESS_PROJECT_REPO set
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

    body: dict[str, object] = {
        "ok": overall_ok,
        "auth_required": bool(HARNESS_TOKEN),
        "checks": checks,
    }
    return JSONResponse(body, status_code=200 if overall_ok else 503)


@app.get("/api/status", dependencies=[Depends(require_token)])
async def status() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    team_today = await _today_spend()
    return {
        "ok": True,
        "version": app.version,
        "milestone": "M2",
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": int((now - STARTED_AT).total_seconds()),
        "host": os.environ.get("HOSTNAME", "unknown"),
        "caps": {
            "agent_daily_usd": AGENT_DAILY_CAP_USD,
            "team_daily_usd": TEAM_DAILY_CAP_USD,
            "team_today_usd": round(team_today, 4),
        },
        "kdrive": {
            "enabled": kdrive.enabled,
            "reason": kdrive.reason,
        },
        "workspaces": get_workspaces_status(),
    }


# ------------------------------------------------------------------
# Agents
# ------------------------------------------------------------------


@app.get("/api/agents", dependencies=[Depends(require_token)])
async def list_agents() -> dict[str, list[dict[str, Any]]]:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, kind, name, role, status, current_task_id, model, "
            "workspace_path, cost_estimate_usd, started_at, last_heartbeat "
            "FROM agents ORDER BY "
            "CASE kind WHEN 'coach' THEN 0 ELSE 1 END, id"
        )
        rows = await cur.fetchall()
    finally:
        await c.close()
    return {"agents": [dict(r) for r in rows]}


@app.post("/api/agents/start", dependencies=[Depends(require_token)])
async def start_agent(
    req: StartAgentRequest, background: BackgroundTasks
) -> dict[str, object]:
    background.add_task(run_agent, req.agent_id, req.prompt)
    return {"ok": True, "agent_id": req.agent_id}


COACH_TICK_PROMPT = (
    "Routine tick. Read your inbox for new human goals and Player updates. "
    "If there's nothing actionable, end the turn without calling tools. "
    "Otherwise decompose goals into tasks, assign or reassign as needed, "
    "and reply to Players who need direction. Be terse."
)


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
    background.add_task(run_agent, "coach", COACH_TICK_PROMPT)
    return {"ok": True, "prompt": COACH_TICK_PROMPT}


@app.delete("/api/agents/{agent_id}/session", dependencies=[Depends(require_token)])
async def clear_session(agent_id: str) -> dict[str, object]:
    """Clear agent.session_id so the next run starts fresh context.

    Useful when an agent's conversation has drifted or when the human
    wants to start a new thread without losing task/memory state.
    """
    if not (agent_id == "coach" or (agent_id.startswith("p") and agent_id[1:].isdigit() and 1 <= int(agent_id[1:]) <= 10)):
        raise HTTPException(400, detail=f"invalid agent_id '{agent_id}'")
    c = await configured_conn()
    try:
        cur = await c.execute(
            "UPDATE agents SET session_id = NULL WHERE id = ?", (agent_id,)
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
            "type": "session_cleared",
        }
    )
    return {"ok": True, "agent_id": agent_id}


# ------------------------------------------------------------------
# Tasks
# ------------------------------------------------------------------


@app.get("/api/tasks", dependencies=[Depends(require_token)])
async def list_tasks(status: str | None = None, owner: str | None = None) -> dict[str, Any]:
    where_parts: list[str] = []
    params: list[Any] = []
    if status:
        where_parts.append("status = ?")
        params.append(status)
    if owner is not None:
        if owner.lower() in ("null", "none", "unassigned"):
            where_parts.append("owner IS NULL")
        else:
            where_parts.append("owner = ?")
            params.append(owner)
    clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

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

    c = await configured_conn()
    try:
        if parent_id:
            cur = await c.execute("SELECT id FROM tasks WHERE id = ?", (parent_id,))
            if (await cur.fetchone()) is None:
                raise HTTPException(404, detail=f"parent_id {parent_id} not found")
        await c.execute(
            "INSERT INTO tasks (id, title, description, parent_id, priority, created_by) "
            "VALUES (?, ?, ?, ?, ?, 'human')",
            (task_id, req.title, req.description, parent_id, req.priority),
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


# ------------------------------------------------------------------
# Events (paginated replay for pane restore)
# ------------------------------------------------------------------


@app.get("/api/decisions", dependencies=[Depends(require_token)])
async def list_decisions() -> dict[str, Any]:
    """List local decision records (recent first, capped at 50).

    Decisions live primarily on kDrive at /harness/decisions/<file>.md
    with a /data/decisions/ local fallback. This endpoint reads the
    LOCAL store only — it's the fast path. The kDrive copy is the
    durable / human-readable mirror; browse it directly from
    Infomaniak's web UI to see everything ever written.
    """
    local_dir = Path(os.environ.get("HARNESS_DECISIONS_DIR", "/data/decisions"))
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
    local_dir = Path(os.environ.get("HARNESS_DECISIONS_DIR", "/data/decisions"))
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


@app.get("/api/events", dependencies=[Depends(require_token)])
async def list_events(
    agent: str | None = None,
    since_id: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """Return event history for a pane to restore when it opens.

    Returns the MOST RECENT `limit` events (ordered chronologically oldest
    → newest in the response) with id > since_id. Pass since_id=0 to get
    the tail of the log; pass the largest id you've seen to poll for new
    rows (used in future polling/paginating flows).
    """
    limit = max(1, min(limit, 1000))
    where_parts: list[str] = ["id > ?"]
    params: list[Any] = [since_id]
    if agent:
        where_parts.append("agent_id = ?")
        params.append(agent)
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
    """Accept an image upload, store under /data/attachments/<id>.<ext>.

    Returns a stable id + filesystem path. The caller (frontend) includes
    the path in the prompt text so the agent can Read the image.
    """
    filename = file.filename or "upload"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            400,
            detail=f"unsupported extension '{ext}'. Allowed: {sorted(ALLOWED_EXT)}",
        )

    att_id = uuid.uuid4().hex[:12]
    target = ATTACHMENTS_DIR / f"{att_id}.{ext}"
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

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
    target = ATTACHMENTS_DIR / filename
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
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    finally:
        bus.unsubscribe(q)

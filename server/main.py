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
    COACH_TICK_PROMPT,
    TEAM_DAILY_CAP_USD,
    _today_spend,
    cancel_agent,
    cancel_all_agents,
    coach_tick_loop,
    is_paused,
    run_agent,
    set_paused,
)
from server import context as ctxmod
from server import files as filesmod
from server.db import configured_conn, crash_recover, init_db
from server.events import bus
from server.kdrive import kdrive
from server.sync import events_trim_loop, flush_loop, snapshot_loop
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
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
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
    # Background tasks: flush event log to kDrive + hourly SQLite
    # snapshot. Both are no-ops when kDrive is disabled so running them
    # unconditionally is safe — picks up activation without restart.
    sync_task = asyncio.create_task(flush_loop())
    snapshot_task = asyncio.create_task(snapshot_loop())
    coach_task = asyncio.create_task(coach_tick_loop())
    trim_task = asyncio.create_task(events_trim_loop())
    bg_tasks = (sync_task, snapshot_task, coach_task, trim_task)
    try:
        yield
    finally:
        for t in bg_tasks:
            t.cancel()
        for t in bg_tasks:
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

    # 6. Workspaces — only check if HARNESS_PROJECT_REPO set
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
        "milestone": "M2",
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
            "SELECT id, kind, name, role, brief, status, current_task_id, "
            "model, workspace_path, session_id, cost_estimate_usd, "
            "started_at, last_heartbeat "
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


class CoachLoopRequest(BaseModel):
    interval_seconds: int = Field(..., ge=0, le=86_400)


@app.get("/api/coach/loop", dependencies=[Depends(require_token)])
async def get_coach_loop() -> dict[str, object]:
    from server.agents import get_coach_interval
    return {"interval_seconds": get_coach_interval()}


@app.post("/api/coach/loop", dependencies=[Depends(require_token)])
async def set_coach_loop(req: CoachLoopRequest) -> dict[str, object]:
    """Set Coach's autoloop interval at runtime. 0 disables. The
    background loop re-reads this each iteration, so changes take
    effect on the next tick (no restart)."""
    from server.agents import set_coach_interval
    set_coach_interval(req.interval_seconds)
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "coach",
            "type": "coach_loop_changed",
            "interval_seconds": req.interval_seconds,
        }
    )
    return {"ok": True, "interval_seconds": req.interval_seconds}


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
async def set_agent_identity(agent_id: str, req: AgentIdentityWrite) -> dict[str, object]:
    """Human upsert for name + role. Either field omitted → left alone;
    passed as empty string → cleared (column set to NULL). Emits
    'player_assigned' with auto:false so the UI refreshes live."""
    if not (agent_id == "coach" or (agent_id.startswith("p") and agent_id[1:].isdigit() and 1 <= int(agent_id[1:]) <= 10)):
        raise HTTPException(400, detail=f"invalid agent_id '{agent_id}'")
    sets = []
    vals: list[object] = []
    if req.name is not None:
        if len(req.name) > 60:
            raise HTTPException(400, detail="name too long (max 60 chars)")
        sets.append("name = ?")
        vals.append(req.name if req.name else None)
    if req.role is not None:
        if len(req.role) > 120:
            raise HTTPException(400, detail="role too long (max 120 chars)")
        sets.append("role = ?")
        vals.append(req.role if req.role else None)
    if not sets:
        return {"ok": True, "agent_id": agent_id, "changed": 0}
    c = await configured_conn()
    try:
        vals.append(agent_id)
        cur = await c.execute(
            f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", tuple(vals)
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
            "type": "player_assigned",
            "name": req.name,
            "role": req.role,
            "auto": False,
        }
    )
    return {"ok": True, "agent_id": agent_id, "changed": changed}


class AgentBriefWrite(BaseModel):
    brief: str = Field(..., description="Free-form context text; empty string clears.")


@app.put("/api/agents/{agent_id}/brief", dependencies=[Depends(require_token)])
async def set_agent_brief(agent_id: str, req: AgentBriefWrite) -> dict[str, object]:
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
    c = await configured_conn()
    try:
        cur = await c.execute(
            "UPDATE agents SET brief = ? WHERE id = ?",
            (body if body else None, agent_id),
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
            "type": "brief_updated",
            "size": len(body),
        }
    )
    return {"ok": True, "agent_id": agent_id, "size": len(body)}


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


@app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(require_token)])
async def cancel_task_from_human(task_id: str) -> dict[str, Any]:
    """Cancel a task from the UI. Updates the task row + clears the
    owner's current_task_id so the agent is free to claim the next one.

    Noop (but returns 200) if the task is already done or cancelled —
    the UI may race against an in-flight update."""
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, status, owner FROM tasks WHERE id = ?", (task_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(404, detail=f"task {task_id} not found")
        task = dict(row)
        if task["status"] in ("done", "cancelled"):
            return {"ok": True, "task_id": task_id, "already": task["status"]}
        old_status = task["status"]
        await c.execute(
            "UPDATE tasks SET status = 'cancelled' WHERE id = ?", (task_id,)
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


_HUMAN_MSG_RECIPIENTS = (
    {"coach", "broadcast"} | {f"p{i}" for i in range(1, 11)}
)


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
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO messages (from_id, to_id, subject, body, priority) "
            "VALUES ('human', ?, ?, ?, ?) RETURNING id",
            (to, req.subject, req.body, req.priority),
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
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, from_id, to_id, subject, body, sent_at, "
            "in_reply_to, priority "
            "FROM messages ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
    finally:
        await c.close()
    return {"messages": [dict(r) for r in rows]}


@app.get("/api/memory", dependencies=[Depends(require_token)])
async def list_memory() -> dict[str, Any]:
    """List shared-memory topics (flat table, not paginated — this
    harness has at most a few dozen memory docs in practice)."""
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT topic, last_updated, last_updated_by, version, "
            "LENGTH(content) AS size FROM memory_docs "
            "ORDER BY last_updated DESC"
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
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT version FROM memory_docs WHERE topic = ?", (req.topic,)
        )
        row = await cur.fetchone()
        if row:
            new_version = int(dict(row)["version"]) + 1
            await c.execute(
                "UPDATE memory_docs SET content = ?, last_updated = ?, "
                "last_updated_by = 'human', version = ? WHERE topic = ?",
                (req.content, now, new_version, req.topic),
            )
        else:
            new_version = 1
            await c.execute(
                "INSERT INTO memory_docs (topic, content, last_updated, "
                "last_updated_by, version) VALUES (?, ?, ?, 'human', ?)",
                (req.topic, req.content, now, new_version),
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
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT topic, content, last_updated, last_updated_by, version "
            "FROM memory_docs WHERE topic = ?",
            (topic,),
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


class ContextWrite(BaseModel):
    kind: str = Field(..., description="root | skills | rules")
    name: str = Field("", description="file basename without .md; '' or 'CLAUDE' for kind='root'")
    body: str = Field(..., description="full markdown content")


@app.get("/api/context", dependencies=[Depends(require_token)])
async def list_context() -> dict[str, Any]:
    """List every available governance-layer context doc (local ∪ kDrive).
    Shape: {"root": ["CLAUDE"] or [], "skills": [...], "rules": [...]}."""
    return await ctxmod.list_all()


@app.get("/api/context/{kind}/{name}", dependencies=[Depends(require_token)])
async def get_context(kind: str, name: str) -> dict[str, Any]:
    err = ctxmod.validate(kind, "CLAUDE" if kind == "root" else name)
    if err:
        raise HTTPException(400, detail=err)
    body = await ctxmod.read(kind, "CLAUDE" if kind == "root" else name)
    if body is None:
        raise HTTPException(404, detail="not found")
    return {"kind": kind, "name": name, "body": body, "size": len(body)}


@app.post("/api/context", dependencies=[Depends(require_token)])
async def write_context_from_human(req: ContextWrite) -> dict[str, Any]:
    """Upsert a context doc from the human operator. Same write path as
    `coord_write_context` but attributed to 'human'. Emits context_updated
    so open UIs re-render and the next agent turn picks up the change."""
    try:
        ok = await ctxmod.write(req.kind, req.name or "CLAUDE", req.body)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    if not ok:
        raise HTTPException(500, detail="write failed — check server logs")
    effective = "CLAUDE" if req.kind == "root" else req.name
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "human",
            "type": "context_updated",
            "kind": req.kind,
            "name": effective,
            "size": len(req.body),
        }
    )
    return {"ok": True, "kind": req.kind, "name": effective}


@app.delete("/api/context/{kind}/{name}", dependencies=[Depends(require_token)])
async def delete_context(kind: str, name: str) -> dict[str, Any]:
    effective = "CLAUDE" if kind == "root" else name
    try:
        await ctxmod.delete(kind, effective)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "human",
            "type": "context_deleted",
            "kind": kind,
            "name": effective,
        }
    )
    return {"ok": True}


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
    return {"ok": True, **result}


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
    where: list[str] = ["id > ?"]
    params: list[Any] = [since_id]
    if agent:
        where.append("agent_id = ?")
        params.append(agent)
    where_sql = " WHERE " + " AND ".join(where)
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, agent_id, started_at, ended_at, duration_ms, "
            "cost_usd, session_id, num_turns, stop_reason, is_error, "
            "model, plan_mode, effort "
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
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT agent_id, COUNT(*) AS count, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd, "
            "COALESCE(AVG(duration_ms), 0) AS avg_duration_ms, "
            "SUM(is_error) AS error_count "
            "FROM turns WHERE ended_at >= ? "
            "GROUP BY agent_id ORDER BY cost_usd DESC",
            (cutoff,),
        )
        per_agent = [dict(r) for r in await cur.fetchall()]
        cur = await c.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(cost_usd), 0) AS cost_usd "
            "FROM turns WHERE ended_at >= ?",
            (cutoff,),
        )
        total_row = dict(await cur.fetchone())
    finally:
        await c.close()
    return {
        "window_hours": hours,
        "since": cutoff,
        "total_turns": int(total_row["count"] or 0),
        "total_cost_usd": float(total_row["cost_usd"] or 0),
        "per_agent": per_agent,
    }


@app.get("/api/events", dependencies=[Depends(require_token)])
async def list_events(
    agent: str | None = None,
    type: str | None = None,
    since_id: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """Return event history for a pane to restore when it opens.

    Returns the MOST RECENT `limit` events (ordered chronologically oldest
    → newest in the response) with id > since_id. Pass since_id=0 to get
    the tail of the log; pass the largest id you've seen to poll for new
    rows (used in future polling/paginating flows).

    Optional `type` narrows to a single event type (e.g.
    'human_attention') — useful when the UI wants to surface historical
    escalations across page reloads.
    """
    limit = max(1, min(limit, 1000))
    where_parts: list[str] = ["id > ?"]
    params: list[Any] = [since_id]
    if agent:
        # Fan-out: include events where this agent is the recipient,
        # not only the actor. Mirrors the WS-side fan-out so opening a
        # pane's history matches what the pane would have shown live.
        #   - type=message_sent & .to matches (or 'broadcast')
        #   - type=task_assigned & .to matches
        #   - type=task_updated & .owner matches (Coach cancelling a
        #     Player's task should show in the Player's history too)
        where_parts.append(
            "("
            "agent_id = ?"
            " OR (type = 'message_sent' AND ("
            "     json_extract(payload, '$.to') = ?"
            "     OR json_extract(payload, '$.to') = 'broadcast'"
            "))"
            " OR (type = 'task_assigned' AND json_extract(payload, '$.to') = ?)"
            " OR (type = 'task_updated' AND json_extract(payload, '$.owner') = ?)"
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

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from server.agents import run_agent
from server.events import bus

app = FastAPI(
    title="TeamOfTen harness",
    version="0.1.0",
    description="Personal orchestration harness — 1 coordinator + 10 worker agents.",
)

STARTED_AT = datetime.now(timezone.utc)
INDEX_HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


class StartAgentRequest(BaseModel):
    # Valid slot ids are "coach" or "p1".."p10". The pattern is permissive
    # for M1; tighter validation lands in M2a once the Agent model exists.
    agent_id: str = Field(default="p1", pattern=r"^[a-z0-9_-]{1,16}$")
    prompt: str = Field(min_length=1, max_length=10_000)


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return INDEX_HTML


@app.get("/api/status")
async def status() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "ok": True,
        "version": app.version,
        "milestone": "M1",
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": int((now - STARTED_AT).total_seconds()),
        "host": os.environ.get("HOSTNAME", "unknown"),
    }


@app.post("/api/agents/start")
async def start_agent(
    req: StartAgentRequest, background: BackgroundTasks
) -> dict[str, object]:
    background.add_task(run_agent, req.agent_id, req.prompt)
    return {"ok": True, "agent_id": req.agent_id}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
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

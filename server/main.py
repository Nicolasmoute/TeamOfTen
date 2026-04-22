from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI

app = FastAPI(
    title="TeamOfTen harness",
    version="0.0.1",
    description="Personal orchestration harness — 1 coordinator + 10 worker agents.",
)

STARTED_AT = datetime.now(timezone.utc)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "message": "TeamOfTen harness is alive.",
        "status_endpoint": "/api/status",
    }


@app.get("/api/status")
async def status() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "ok": True,
        "version": app.version,
        "milestone": "M0",
        "started_at": STARTED_AT.isoformat(),
        "uptime_seconds": int((now - STARTED_AT).total_seconds()),
        "host": os.environ.get("HOSTNAME", "unknown"),
    }

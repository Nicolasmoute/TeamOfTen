"""Playbook HTTP API — `/api/playbook/*` routes.

Mounted from main.py via `build_router(require_token=..., audit_actor=...)`.
All write endpoints carry the `audit_actor` dependency so destructive
actions are recorded with `{source, ip, ua}` in the bus event payload
(matches kanban-specs-v2 §8 convention).

Spec §8 endpoint table.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from server.playbook import bootstrap, config, mutate, runner
from server.playbook.paths import ensure_playbook_dir
from server.playbook.store import (
    load_archive,
    load_lattice,
    read_runs,
    save_archive,
    save_lattice,
    wipe_files,
)

logger = logging.getLogger("harness.playbook.api")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- bodies


class RunBody(BaseModel):
    force_through_no_activity: bool = False


class BootstrapBody(BaseModel):
    pass


class ResetBody(BaseModel):
    confirm: str


class WeightOverrideBody(BaseModel):
    weight: float


class RestoreBody(BaseModel):
    weight: float | None = None


class AdjustProposalBody(BaseModel):
    delta: float


class CreateProposalBody(BaseModel):
    text: str
    weight: float


class MergeProposalBody(BaseModel):
    keep_id: str
    drop_id: str


class BatchProposalBody(BaseModel):
    operations: list[dict[str, Any]]


# ---------------------------------------------------------------- helpers


def _write_team_config(key: str, value: str | None) -> None:
    try:
        from server.db import DB_PATH  # noqa: PLC0415

        conn = sqlite3.connect(DB_PATH, timeout=2.0)
        try:
            if value is None:
                conn.execute("DELETE FROM team_config WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO team_config (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("playbook.api: team_config write failed (%s)", key)


def _read_team_config(key: str) -> str:
    try:
        from server.db import DB_PATH  # noqa: PLC0415

        conn = sqlite3.connect(DB_PATH, timeout=2.0)
        try:
            cur = conn.execute(
                "SELECT value FROM team_config WHERE key = ?", (key,)
            )
            row = cur.fetchone()
            return str(row[0]) if row else ""
        finally:
            conn.close()
    except Exception:
        return ""


async def _publish(payload: dict[str, Any]) -> None:
    try:
        from server.events import bus  # noqa: PLC0415

        await bus.publish({"ts": _now_iso(), **payload})
    except Exception:
        logger.exception("playbook.api: bus.publish raised")


async def _acquire_with_timeout(timeout: float) -> bool:
    """Try to acquire `_run_lock` with a timeout. Returns True on
    success (caller must release via the standard `_run_lock.release()`
    pattern); False on timeout."""
    try:
        await asyncio.wait_for(runner._run_lock.acquire(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


# ---------------------------------------------------------------- router


def build_router(
    *,
    require_token: Callable[..., Awaitable[None]],
    audit_actor: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/playbook", tags=["playbook"])
    deps = [Depends(require_token)]

    async def _apply_dashboard_operations(
        operations: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool]:
        """Apply dashboard proposals through the same mutation path as
        runner/MCP, then run the final hard-cap pressure sweep."""
        lattice = load_lattice()
        archive = load_archive()
        engine_actions: list[dict[str, Any]] = []
        applied, rejected, hard_cap_hit = mutate.apply_coach_proposals(
            lattice,
            archive,
            operations,
            creation_weight=config.COACH_CREATION_WEIGHT,
            engine_actions=engine_actions,
        )
        engine_actions.extend(
            mutate.sweep_engine_actions(
                lattice,
                archive,
                include_hygiene=False,
                include_pressure=True,
            )
        )
        if applied or engine_actions:
            await save_lattice(lattice)
            await save_archive(archive)
        return applied, rejected, engine_actions, hard_cap_hit

    async def _publish_dashboard_outcome(
        *,
        applied: list[dict[str, Any]],
        engine_actions: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
        hard_cap_hit: bool,
        creation_count: int,
    ) -> None:
        if applied or engine_actions:
            await _publish({
                "type": "playbook_changes_applied",
                "operations_count": len(applied),
                "source": "human_dashboard",
            })
        if hard_cap_hit:
            hard_rejected = [
                r for r in rejected if r.get("reason") == "hard_cap_pressure"
            ]
            await _publish({
                "type": "playbook_soft_cap_exceeded",
                "count": hard_rejected[0].get("pressure") if hard_rejected else config.HARD_STATEMENT_CAP + 1,
                "dropped": len(hard_rejected) or creation_count,
            })
            await _publish({
                "type": "human_attention",
                "agent_id": "playbook",
                "subject": "Playbook hard cap pressure",
                "body": (
                    "Dashboard proposal pressure exceeded the hard cap; "
                    "creations were rejected. Review the lattice for "
                    "merges, downward adjustments, or deletions."
                ),
                "urgency": "high",
            })

    # ---- GET /state
    @router.get("/state", dependencies=deps)
    async def get_state() -> dict[str, Any]:
        ensure_playbook_dir()
        lattice = load_lattice()
        archive = load_archive()
        runs = read_runs(limit=30)
        return {
            "schema_version": lattice.schema_version,
            "updated_at": lattice.updated_at,
            "active": [s.to_jsonable() for s in lattice.statements],
            "archived": [s.to_jsonable() for s in archive.statements],
            "runs": runs,
            "flags": {
                "bootstrap_done": _read_team_config(config.PLAYBOOK_BOOTSTRAP_DONE_KEY) == "1",
                "bootstrap_blocked": _read_team_config(config.PLAYBOOK_BOOTSTRAP_BLOCKED_KEY) == "1",
                "bootstrap_retries": int(_read_team_config(config.PLAYBOOK_BOOTSTRAP_RETRIES_KEY) or 0),
                "disabled": _read_team_config(config.PLAYBOOK_DISABLED_KEY) == "1",
                "last_run_at": _read_team_config(config.PLAYBOOK_LAST_RUN_AT_KEY) or None,
            },
            "caps": {
                "soft": config.SOFT_STATEMENT_CAP,
                "hard": config.HARD_STATEMENT_CAP,
                "active_count": len(lattice.statements),
            },
        }

    # ---- POST /run (manual reflection trigger)
    @router.post("/run", dependencies=deps)
    async def post_run(
        body: RunBody,
        request: Request,
    ) -> dict[str, Any]:
        if _read_team_config(config.PLAYBOOK_BOOTSTRAP_DONE_KEY) != "1":
            raise HTTPException(
                status_code=409,
                detail="bootstrap not complete — run POST /api/playbook/bootstrap first",
            )
        actor = audit_actor(request)
        ok = await _acquire_with_timeout(timeout=10.0)
        if not ok:
            raise HTTPException(
                status_code=503,
                detail="playbook engine busy — another run is in flight",
            )
        try:
            row = await runner.run_daily_reflection(
                manual=True,
                force_through_no_activity=body.force_through_no_activity,
            )
        finally:
            runner._run_lock.release()
        await _publish({"type": "playbook_manual_run", "actor": actor, "outcome": row.get("outcome")})
        return row

    # ---- POST /bootstrap (manual bootstrap trigger, G7)
    @router.post("/bootstrap", dependencies=deps)
    async def post_bootstrap(
        _body: BootstrapBody,  # required by FastAPI shape contract; no fields used
        request: Request,
    ) -> dict[str, Any]:
        if _read_team_config(config.PLAYBOOK_BOOTSTRAP_DONE_KEY) == "1":
            raise HTTPException(
                status_code=409,
                detail="bootstrap already complete — POST /api/playbook/reset first to re-arm",
            )
        if _read_team_config(config.PLAYBOOK_BOOTSTRAP_BLOCKED_KEY) == "1":
            raise HTTPException(
                status_code=409,
                detail="bootstrap blocked after 3 failures — POST /api/playbook/reset first to clear",
            )
        actor = audit_actor(request)
        ok = await _acquire_with_timeout(timeout=5.0)
        if not ok:
            raise HTTPException(
                status_code=409,
                detail="playbook engine busy — could not acquire lock within 5s",
            )
        try:
            row = await bootstrap.run_bootstrap()
        finally:
            runner._run_lock.release()
        await _publish({"type": "playbook_manual_bootstrap", "actor": actor, "outcome": row.get("outcome")})
        return row

    # ---- POST /reset (G2)
    @router.post("/reset", dependencies=deps)
    async def post_reset(
        body: ResetBody,
        request: Request,
    ) -> dict[str, Any]:
        if body.confirm != "yes":
            raise HTTPException(
                status_code=400,
                detail="reset requires confirm: 'yes'",
            )
        actor = audit_actor(request)
        # Blocking acquire with 60s timeout (spec §G2). On timeout: 503.
        ok = await _acquire_with_timeout(timeout=60.0)
        if not ok:
            raise HTTPException(
                status_code=503,
                detail="playbook engine busy — reset timed out after 60s",
                headers={"Retry-After": "30"},
            )
        try:
            wipe_files()
            for key in (
                config.PLAYBOOK_BOOTSTRAP_DONE_KEY,
                config.PLAYBOOK_BOOTSTRAP_RETRIES_KEY,
                config.PLAYBOOK_BOOTSTRAP_BLOCKED_KEY,
            ):
                _write_team_config(key, None)
            _write_team_config(config.PLAYBOOK_RESET_AT_KEY, _now_iso())
        finally:
            runner._run_lock.release()
        await _publish({"type": "playbook_reset", "actor": actor})
        return {"ok": True}

    # ---- POST /statements/{id}/weight (NO/½/YES override)
    @router.post("/statements/{sid}/weight", dependencies=deps)
    async def post_override(
        sid: str,
        body: WeightOverrideBody,
        request: Request,
    ) -> dict[str, Any]:
        actor = audit_actor(request)
        if not (0.0 <= body.weight <= 1.0):
            raise HTTPException(status_code=400, detail="weight must be in [0, 1]")
        ok_lock = await _acquire_with_timeout(timeout=10.0)
        if not ok_lock:
            raise HTTPException(status_code=503, detail="playbook engine busy")
        try:
            lattice = load_lattice()
            # Spec §9: payload includes `from` — capture pre-override weight.
            prev_weight: float | None = None
            for s in lattice.statements:
                if s.id == sid:
                    prev_weight = s.weight
                    break
            actor_str = (actor.get("source") if isinstance(actor, dict) else None) or "human"
            ok, err = mutate.override_weight(
                lattice, sid, weight=body.weight, actor=str(actor_str),
            )
            if not ok:
                raise HTTPException(status_code=400, detail=err or "override_failed")
            await save_lattice(lattice)
        finally:
            runner._run_lock.release()
        await _publish({
            "type": "playbook_statement_overridden",
            "id": sid,
            "from": prev_weight,
            "to": body.weight,
            "actor": actor,
        })
        return {"ok": True, "id": sid, "weight": body.weight}

    # ---- POST /statements/{id}/restore
    @router.post("/statements/{sid}/restore", dependencies=deps)
    async def post_restore(
        sid: str,
        body: RestoreBody,
        request: Request,
    ) -> dict[str, Any]:
        actor = audit_actor(request)
        ok_lock = await _acquire_with_timeout(timeout=10.0)
        if not ok_lock:
            raise HTTPException(status_code=503, detail="playbook engine busy")
        try:
            lattice = load_lattice()
            archive = load_archive()
            ok, err = mutate.restore_from_archive(
                lattice, archive, sid, weight=body.weight,
            )
            if not ok:
                raise HTTPException(status_code=400, detail=err or "restore_failed")
            await save_lattice(lattice)
            await save_archive(archive)
        finally:
            runner._run_lock.release()
        await _publish({
            "type": "playbook_statement_restored",
            "id": sid,
            "actor": actor,
        })
        return {"ok": True, "id": sid}

    # ---- DELETE /statements/{id}
    @router.delete("/statements/{sid}", dependencies=deps)
    async def delete_statement(
        sid: str,
        request: Request,
    ) -> dict[str, Any]:
        actor = audit_actor(request)
        ok_lock = await _acquire_with_timeout(timeout=10.0)
        if not ok_lock:
            raise HTTPException(status_code=503, detail="playbook engine busy")
        try:
            lattice = load_lattice()
            archive = load_archive()
            ok, err = mutate.soft_delete(lattice, archive, sid)
            if not ok:
                raise HTTPException(status_code=400, detail=err or "delete_failed")
            await save_lattice(lattice)
            await save_archive(archive)
        finally:
            runner._run_lock.release()
        await _publish({
            "type": "playbook_statement_deleted",
            "id": sid,
            "actor": actor,
        })
        return {"ok": True, "id": sid}

    # ---- POST /proposals/{adjust|create|merge}/{id-or-marker}
    # Manual application of a logged-but-unapplied proposal. The spec
    # uses these for human override of soft-cap-rejected proposals.
    @router.post("/proposals/adjust/{sid}", dependencies=deps)
    async def post_proposal_adjust(
        sid: str,
        body: AdjustProposalBody,
        request: Request,
    ) -> dict[str, Any]:
        actor = audit_actor(request)
        ok_lock = await _acquire_with_timeout(timeout=10.0)
        if not ok_lock:
            raise HTTPException(status_code=503, detail="playbook engine busy")
        try:
            applied, rejected, engine_actions, hard = await _apply_dashboard_operations(
                [{"op": "adjust", "id": sid, "delta": body.delta,
                  "reason": f"human_proposal_apply (actor={actor})"}]
            )
        finally:
            runner._run_lock.release()
        await _publish_dashboard_outcome(
            applied=applied,
            engine_actions=engine_actions,
            rejected=rejected,
            hard_cap_hit=hard,
            creation_count=0,
        )
        if rejected:
            raise HTTPException(status_code=400, detail=rejected[0].get("reason") or "adjust_failed")
        return {"ok": True, "id": sid}

    @router.post("/proposals/create/new", dependencies=deps)
    async def post_proposal_create(
        body: CreateProposalBody,
        request: Request,
    ) -> dict[str, Any]:
        actor = audit_actor(request)
        ok_lock = await _acquire_with_timeout(timeout=10.0)
        if not ok_lock:
            raise HTTPException(status_code=503, detail="playbook engine busy")
        try:
            applied, rejected, engine_actions, hard = await _apply_dashboard_operations(
                [{"op": "create", "text": body.text, "weight": body.weight,
                  "reason": f"human_proposal_create (actor={actor})"}]
            )
        finally:
            runner._run_lock.release()
        await _publish_dashboard_outcome(
            applied=applied,
            engine_actions=engine_actions,
            rejected=rejected,
            hard_cap_hit=hard,
            creation_count=1,
        )
        if rejected:
            raise HTTPException(status_code=400, detail=rejected[0].get("reason") or "create_failed")
        return {"ok": True, "applied": applied, "engine_actions": engine_actions}

    @router.post("/proposals/batch", dependencies=deps)
    async def post_proposal_batch(
        body: BatchProposalBody,
        request: Request,
    ) -> dict[str, Any]:
        actor = audit_actor(request)
        operations = []
        for op in body.operations:
            if isinstance(op, dict):
                operations.append({**op, "reason": op.get("reason") or f"human_batch (actor={actor})"})
        if not operations:
            raise HTTPException(status_code=400, detail="operations required")
        ok_lock = await _acquire_with_timeout(timeout=10.0)
        if not ok_lock:
            raise HTTPException(status_code=503, detail="playbook engine busy")
        try:
            applied, rejected, engine_actions, hard = await _apply_dashboard_operations(operations)
        finally:
            runner._run_lock.release()
        creation_count = sum(1 for op in operations if op.get("op") == "create")
        await _publish_dashboard_outcome(
            applied=applied,
            engine_actions=engine_actions,
            rejected=rejected,
            hard_cap_hit=hard,
            creation_count=creation_count,
        )
        return {
            "ok": bool(applied or not rejected),
            "applied": applied,
            "rejected": rejected,
            "engine_actions": engine_actions,
            "hard_cap_hit": hard,
        }

    @router.post("/proposals/merge/{keep_id}", dependencies=deps)
    async def post_proposal_merge(
        keep_id: str,
        body: MergeProposalBody,
        request: Request,
    ) -> dict[str, Any]:
        if body.keep_id != keep_id:
            raise HTTPException(status_code=400, detail="path keep_id != body keep_id")
        actor = audit_actor(request)
        ok_lock = await _acquire_with_timeout(timeout=10.0)
        if not ok_lock:
            raise HTTPException(status_code=503, detail="playbook engine busy")
        try:
            applied, rejected, engine_actions, hard = await _apply_dashboard_operations(
                [{"op": "merge", "keep_id": body.keep_id, "drop_id": body.drop_id,
                  "reason": f"human_proposal_merge (actor={actor})"}]
            )
        finally:
            runner._run_lock.release()
        await _publish_dashboard_outcome(
            applied=applied,
            engine_actions=engine_actions,
            rejected=rejected,
            hard_cap_hit=hard,
            creation_count=0,
        )
        if rejected:
            raise HTTPException(status_code=400, detail=rejected[0].get("reason") or "merge_failed")
        return {"ok": True, "keep_id": keep_id, "drop_id": body.drop_id}

    # ---- GET /runs
    @router.get("/runs", dependencies=deps)
    async def get_runs(since: str | None = None, limit: int = 30) -> dict[str, Any]:
        limit = max(1, min(100, int(limit)))
        rows = read_runs(limit=limit)
        if since:
            rows = [r for r in rows if r.get("started_at", "") >= since]
        return {"runs": rows}

    return router


__all__ = ["build_router"]

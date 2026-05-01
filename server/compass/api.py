"""FastAPI router for `/api/compass/*`.

Built lazily by `build_router(require_token, audit_actor)` so the
auth dependencies defined in `server.main` can be injected without
circular imports — same pattern as `server.projects_api`.

Endpoints (all require_token; destructive ones tag actor):
  - GET  /state              full snapshot for the active project
  - POST /enable             flip team_config['compass_enabled_<id>']
  - POST /disable            flip off (does not wipe state)
  - POST /run                trigger an on_demand run (background)
  - POST /heartbeat          presence ping
  - POST /qa/start           start a Q&A session
  - POST /qa/next            fetch next question (immediate)
  - POST /qa/answer          submit answer; immediate digest
  - POST /qa/end             end session
  - POST /questions/{id}/answer    queue an answer for next-run digest
  - POST /proposals/settle/{id}    resolve settle proposal
  - POST /proposals/stale/{id}     resolve stale proposal
  - POST /proposals/dupe/{id}      resolve duplicate proposal
  - POST /proposals/reconcile/{id} resolve corpus↔lattice conflict (§3.0.1)
  - POST /statements/{id}/weight   manual weight override
  - POST /statements/{id}/restore  un-archive
  - GET  /truth                    read-only view of project's truth/ corpus
  - POST /audit                    submit artifact for audit
  - POST /ask                      free-text query against the world model
  - POST /inputs                   record a human signal
  - GET  /briefings/{date}         specific briefing
  - GET  /runs?limit=N             run history
  - GET  /audits?verdict=...       audit log (filterable)
  - POST /reset                    destructive: wipe project state

WebSocket events the dashboard listens for: `compass_phase`,
`compass_run_completed`, `compass_question_queued`,
`compass_question_digested`, `compass_question_answered`,
`compass_proposal_resolved`, `compass_truth_derived`,
`compass_truth_contradiction`, `compass_audit_logged`,
`compass_reconciliation_proposed`, `compass_reset`,
`compass_llm_call`. The bus is the existing `server.events.bus`;
no separate channel.

Q&A session memory is in-process (one per project). Auto-ends if
the project is switched (the dashboard's project-switch flow calls
`/qa/end` defensively).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from server.db import resolve_active_project, configured_conn
from server.events import bus

from server.compass import (
    audit as cmp_audit,
    config as cmp_config,
    mutate,
    presence,
    runner,
    store as cmp_store,
)
from server.compass.pipeline import (
    digest as pl_digest,
    questions as pl_questions,
    truth_check as pl_truth_check,
)

logger = logging.getLogger("harness.compass.api")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------- Q&A session state


class _QASession:
    """One per project (active session). Tracks the pending question
    so `/qa/answer` can pair it with the LLM-generated prediction."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self.started_at = _now_iso()
        self.asked_ids: list[str] = []
        self.last_question_id: str | None = None
        self.last_q_text: str = ""
        self.last_prediction: str = ""
        self.last_targets: list[str] = []
        self.last_rationale: str = ""
        self.answered_count: int = 0


_qa_sessions: dict[str, _QASession] = {}
_qa_lock = asyncio.Lock()


# --------------------------------------------------- helpers


async def _is_enabled(project_id: str) -> bool:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = ?",
            (cmp_config.enabled_key(project_id),),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    val = (dict(row).get("value") if row else "") or ""
    return val.strip().lower() in ("1", "true", "yes")


async def _set_enabled(project_id: str, value: bool) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO team_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (cmp_config.enabled_key(project_id), "1" if value else "0"),
        )
        await c.commit()
    finally:
        await c.close()


def _state_snapshot_dict(project_id: str) -> dict[str, Any]:
    """Serialize the full LatticeState + auxiliaries for the dashboard."""
    state = cmp_store.load_state(project_id)
    audits = cmp_store.read_audits(project_id)
    runs = cmp_store.read_run_log(project_id)
    return {
        "project_id": project_id,
        "schema_version": state.schema_version,
        "statements": [_statement_dict(s) for s in state.statements],
        "truth": [{"index": t.index, "text": t.text, "added_at": t.added_at,
                   "added_by": t.added_by} for t in state.truth],
        "regions": [{"name": r.name, "created_at": r.created_at,
                     "merged_into": r.merged_into} for r in state.regions],
        "region_merge_history": [m.to_jsonable() for m in state.region_merge_history],
        "questions": [_question_dict(q) for q in state.questions],
        "settle_proposals": [_settle_dict(p) for p in state.settle_proposals],
        "stale_proposals": [_stale_dict(p) for p in state.stale_proposals],
        "duplicate_proposals": [_dupe_dict(p) for p in state.duplicate_proposals],
        "reconciliation_proposals": [
            _reconcile_dict(p) for p in state.reconciliation_proposals
        ],
        "claude_md_block": cmp_store.read_claude_md_block(project_id),
        "latest_briefing": cmp_store.latest_briefing_text(project_id),
        "briefing_dates": cmp_store.list_briefing_dates(project_id),
        "audits": [_audit_dict(a) for a in audits[-50:]],
        "runs": [_run_dict(r) for r in runs[-20:]],
        "qa_active": project_id in _qa_sessions,
    }


def _statement_dict(s: cmp_store.Statement) -> dict[str, Any]:
    return {
        "id": s.id,
        "text": s.text,
        "region": s.region,
        "weight": s.weight,
        "history": s.history,
        "archived": s.archived,
        "archived_at": s.archived_at,
        "settled_as": s.settled_as,
        "settled_by_human": s.settled_by_human,
        "manually_set": s.manually_set,
        "merged": s.merged,
        "merged_from": s.merged_from,
        "reformulated": s.reformulated,
        "settle_proposed": s.settle_proposed,
        "stale_proposed": s.stale_proposed,
        "dupe_proposed": s.dupe_proposed,
        "kept_stale": s.kept_stale,
        "created_at": s.created_at,
        "created_by": s.created_by,
    }


def _question_dict(q: cmp_store.Question) -> dict[str, Any]:
    return {
        "id": q.id,
        "q": q.q,
        "prediction": q.prediction,
        "targets": q.targets,
        "rationale": q.rationale,
        "asked_at": q.asked_at,
        "asked_in_run": q.asked_in_run,
        "answer": q.answer,
        "answered_at": q.answered_at,
        "digested": q.digested,
        "digested_in_run": q.digested_in_run,
        "contradicted": q.contradicted,
        "ambiguity_accepted": q.ambiguity_accepted,
        "from_audit": q.from_audit,
    }


def _settle_dict(p: cmp_store.SettleProposal) -> dict[str, Any]:
    return {
        "statement_id": p.statement_id,
        "direction": p.direction,
        "question": p.question,
        "reasoning": p.reasoning,
        "proposed_at": p.proposed_at,
        "proposed_in_run": p.proposed_in_run,
        "pending_runs": p.pending_runs,
    }


def _stale_dict(p: cmp_store.StaleProposal) -> dict[str, Any]:
    return {
        "statement_id": p.statement_id,
        "question": p.question,
        "reformulation": p.reformulation,
        "reasoning": p.reasoning,
        "proposed_at": p.proposed_at,
        "proposed_in_run": p.proposed_in_run,
        "pending_runs": p.pending_runs,
    }


def _dupe_dict(p: cmp_store.DuplicateProposal) -> dict[str, Any]:
    return {
        "id": p.id,
        "cluster_ids": p.cluster_ids,
        "merged_text": p.merged_text,
        "merged_weight": p.merged_weight,
        "region": p.region,
        "reasoning": p.reasoning,
        "proposed_at": p.proposed_at,
        "proposed_in_run": p.proposed_in_run,
        "pending_runs": p.pending_runs,
    }


def _reconcile_dict(p: cmp_store.ReconciliationProposal) -> dict[str, Any]:
    return {
        "id": p.id,
        "statement_id": p.statement_id,
        "statement_archived": p.statement_archived,
        "corpus_paths": p.corpus_paths,
        "explanation": p.explanation,
        "suggested_resolution": p.suggested_resolution,
        "proposed_at": p.proposed_at,
        "proposed_in_run": p.proposed_in_run,
        "pending_runs": p.pending_runs,
    }


def _audit_dict(a: cmp_store.AuditRecord) -> dict[str, Any]:
    return {
        "id": a.id,
        "ts": a.ts,
        "artifact": a.artifact,
        "verdict": a.verdict,
        "summary": a.summary,
        "contradicting_ids": a.contradicting_ids,
        "message_to_coach": a.message_to_coach,
        "question_id": a.question_id,
    }


def _run_dict(r: cmp_store.RunLog) -> dict[str, Any]:
    return {
        "run_id": r.run_id,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "mode": r.mode,
        "completed": r.completed,
        "passive": r.passive,
        "answered_questions": r.answered_questions,
        "contradictions": r.contradictions,
        "region_merges": r.region_merges,
        "settle_proposed": r.settle_proposed,
        "stale_proposed": r.stale_proposed,
        "dupe_proposed": r.dupe_proposed,
        "questions_generated": r.questions_generated,
        "truth_candidates": r.truth_candidates,
        "briefing_path": r.briefing_path,
        "skipped": r.skipped,
        "skipped_reason": r.skipped_reason,
    }


# --------------------------------------------------- router builder


def build_router(
    *,
    require_token: Callable[..., Awaitable[None]],
    audit_actor: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/compass", tags=["compass"])
    deps = [Depends(require_token)]

    # ---------------------------------------- read-only

    @router.get("/state", dependencies=deps)
    async def get_state() -> JSONResponse:
        project_id = await resolve_active_project()
        enabled = await _is_enabled(project_id)
        if not enabled:
            return JSONResponse({
                "project_id": project_id,
                "enabled": False,
                "message": "Compass is disabled for this project. Enable it to start.",
            })
        snap = _state_snapshot_dict(project_id)
        snap["enabled"] = True
        snap["running"] = runner.is_running(project_id)
        return JSONResponse(snap)

    @router.post("/heartbeat", dependencies=deps)
    async def heartbeat() -> JSONResponse:
        project_id = await resolve_active_project()
        await presence.update_heartbeat(project_id)
        return JSONResponse({"ok": True, "project_id": project_id})

    @router.get("/briefings/{date}", dependencies=deps)
    async def get_briefing(date: str) -> PlainTextResponse:
        project_id = await resolve_active_project()
        text = cmp_store.read_briefing(project_id, date)
        if text is None:
            raise HTTPException(404, f"no briefing for {date}")
        return PlainTextResponse(text)

    @router.get("/runs", dependencies=deps)
    async def list_runs(limit: int = Query(default=20, ge=1, le=200)) -> JSONResponse:
        project_id = await resolve_active_project()
        runs = cmp_store.read_run_log(project_id)
        return JSONResponse({"runs": [_run_dict(r) for r in runs[-limit:]]})

    @router.get("/audits", dependencies=deps)
    async def list_audits(
        verdict: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> JSONResponse:
        project_id = await resolve_active_project()
        audits = cmp_store.read_audits(project_id)
        if verdict:
            audits = [a for a in audits if a.verdict == verdict]
        return JSONResponse({"audits": [_audit_dict(a) for a in audits[-limit:]]})

    # ---------------------------------------- enable / disable

    @router.post("/enable", dependencies=deps)
    async def enable(request: Request) -> JSONResponse:
        actor = audit_actor(request)
        project_id = await resolve_active_project()
        await _set_enabled(project_id, True)
        # Bootstrap the local + kDrive state files so the dashboard
        # has something to render immediately. The actual bootstrap
        # *run* (question generation + LLM) is separate and triggered
        # via /run.
        await cmp_store.bootstrap_state(project_id)
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": "compass",
            "project_id": project_id,
            "type": "compass_enabled",
            "actor": actor,
        })
        return JSONResponse({"ok": True, "enabled": True, "project_id": project_id})

    @router.post("/disable", dependencies=deps)
    async def disable(request: Request) -> JSONResponse:
        actor = audit_actor(request)
        project_id = await resolve_active_project()
        await _set_enabled(project_id, False)
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": "compass",
            "project_id": project_id,
            "type": "compass_disabled",
            "actor": actor,
        })
        return JSONResponse({"ok": True, "enabled": False, "project_id": project_id})

    # ---------------------------------------- run

    @router.post("/run", dependencies=deps)
    async def trigger_run(
        body: dict[str, Any] = Body(default={}),
    ) -> JSONResponse:
        project_id = await resolve_active_project()
        if not await _is_enabled(project_id):
            raise HTTPException(403, "Compass is disabled for this project")
        # Allow the human to trigger bootstrap/on_demand from the UI.
        # daily is reserved for the scheduler.
        mode = (body.get("mode") or "on_demand").strip()
        if mode not in ("bootstrap", "on_demand"):
            raise HTTPException(400, f"invalid mode '{mode}' (allowed: bootstrap, on_demand)")
        if runner.is_running(project_id):
            return JSONResponse({"ok": False, "running": True, "project_id": project_id})
        # Spawn as a background task so the HTTP request returns
        # immediately. Phase events stream over /ws.
        await presence.update_heartbeat(project_id)
        asyncio.create_task(_safe_run(project_id, mode))
        return JSONResponse({"ok": True, "running": True, "project_id": project_id, "mode": mode})

    # ---------------------------------------- Q&A session

    @router.post("/qa/start", dependencies=deps)
    async def qa_start() -> JSONResponse:
        project_id = await resolve_active_project()
        if not await _is_enabled(project_id):
            raise HTTPException(403, "Compass is disabled for this project")
        async with _qa_lock:
            sess = _qa_sessions.get(project_id)
            if sess is None:
                sess = _QASession(project_id)
                _qa_sessions[project_id] = sess
        await presence.update_heartbeat(project_id)
        return JSONResponse({
            "ok": True, "project_id": project_id,
            "started_at": sess.started_at,
            "answered": sess.answered_count,
        })

    @router.post("/qa/next", dependencies=deps)
    async def qa_next() -> JSONResponse:
        project_id = await resolve_active_project()
        sess = _qa_sessions.get(project_id)
        if sess is None:
            raise HTTPException(404, "no active Q&A session — start one first")
        if sess.answered_count >= cmp_config.QA_HARD_CAP:
            return JSONResponse({"q": None, "reason": "hard cap reached"})
        state = await cmp_store.load_with_meta(project_id)
        try:
            proposal = await pl_questions.generate_single(
                state, asked_in_session=list(sess.asked_ids),
            )
        except Exception:
            logger.exception("compass.api: qa_next generate failed")
            raise HTTPException(500, "question generation failed")
        if proposal is None:
            return JSONResponse({
                "q": None,
                "reason": "no further questions",
                "answered_count": sess.answered_count,
            })
        # Persist as a question record (so it shows up in the queue
        # too), even though the digest will mark it digested
        # immediately on /qa/answer.
        qid = cmp_store.next_question_id(state)
        q_record = cmp_store.Question(
            id=qid,
            q=proposal.q,
            prediction=proposal.prediction,
            targets=proposal.targets,
            rationale=proposal.rationale,
            asked_at=_now_iso(),
            asked_in_run="qa",
        )
        state.questions.append(q_record)
        await cmp_store.save_questions(project_id, state.questions)
        sess.last_question_id = qid
        sess.last_q_text = proposal.q
        sess.last_prediction = proposal.prediction
        sess.last_targets = proposal.targets
        sess.last_rationale = proposal.rationale
        sess.asked_ids.append(qid)
        return JSONResponse({
            "q": proposal.q,
            "id": qid,
            "prediction": proposal.prediction,
            "targets": proposal.targets,
            "rationale": proposal.rationale,
            "answered_count": sess.answered_count,
        })

    @router.post("/qa/answer", dependencies=deps)
    async def qa_answer(body: dict[str, Any] = Body(...)) -> JSONResponse:
        project_id = await resolve_active_project()
        sess = _qa_sessions.get(project_id)
        if sess is None or not sess.last_question_id:
            raise HTTPException(404, "no pending Q&A question")
        answer_text = (body.get("answer") or "").strip()
        if not answer_text:
            raise HTTPException(400, "answer is required")
        await presence.update_heartbeat(project_id)

        state = await cmp_store.load_with_meta(project_id)
        # Truth-check first.
        try:
            tc = await pl_truth_check.check(
                state.truth,
                question_text=sess.last_q_text,
                prediction=sess.last_prediction,
                answer_text=answer_text,
                project_id=project_id,
            )
        except Exception:
            logger.exception("compass.api: qa_answer truth_check failed")
            raise HTTPException(500, "truth check failed")
        if tc.contradiction:
            q = state.find_question(sess.last_question_id)
            if q is not None:
                q.contradicted = True
                q.answer = answer_text
                q.answered_at = _now_iso()
                await cmp_store.save_questions(project_id, state.questions)
            return JSONResponse({
                "contradiction": True,
                "conflicts": tc.conflicts,
                "summary": tc.summary,
                "question_id": sess.last_question_id,
            })
        # Digest immediately.
        try:
            digest_res = await pl_digest.answer(
                state,
                question_text=sess.last_q_text,
                prediction=sess.last_prediction,
                targets=sess.last_targets,
                answer_text=answer_text,
            )
        except Exception:
            logger.exception("compass.api: qa_answer digest failed")
            raise HTTPException(500, "digest failed")
        run_id = "qa-immediate"
        applied = mutate.apply_statement_updates(
            state, digest_res.updates, run_id=run_id,
            source=f"answer:{sess.last_question_id}",
            delta_max=cmp_config.ANSWER_DELTA_MAX,
        )
        added = mutate.apply_new_statements(
            state, digest_res.new_statements, run_id=run_id,
            source=f"answer:{sess.last_question_id}",
        )
        q = state.find_question(sess.last_question_id)
        if q is not None:
            q.answer = answer_text
            q.answered_at = _now_iso()
            q.digested = True
            q.digested_in_run = run_id
        await cmp_store.save_lattice(project_id, state.statements)
        await cmp_store.save_questions(project_id, state.questions)
        await cmp_store.save_regions(
            project_id, state.regions, state.region_merge_history,
        )
        sess.answered_count += 1
        sess.last_question_id = None
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": "compass",
            "project_id": project_id,
            "type": "compass_question_digested",
            "question_id": q.id if q else None,
            "applied_updates": applied,
            "new_statements": [s.id for s in added],
            "surprise": digest_res.surprise,
            "qa": True,
        })
        return JSONResponse({
            "contradiction": False,
            "applied_updates": applied,
            "new_statements": [s.id for s in added],
            "surprise": digest_res.surprise,
            "summary": digest_res.summary,
            "answered_count": sess.answered_count,
            "warn_after": sess.answered_count >= cmp_config.QA_WARN_AFTER,
        })

    @router.post("/qa/end", dependencies=deps)
    async def qa_end() -> JSONResponse:
        project_id = await resolve_active_project()
        async with _qa_lock:
            sess = _qa_sessions.pop(project_id, None)
        if sess is None:
            return JSONResponse({"ok": True, "had_session": False})
        return JSONResponse({
            "ok": True,
            "had_session": True,
            "answered": sess.answered_count,
        })

    # ---------------------------------------- queue answer for next run

    @router.post("/questions/{question_id}/answer", dependencies=deps)
    async def queue_answer(
        question_id: str,
        body: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        project_id = await resolve_active_project()
        answer_text = (body.get("answer") or "").strip()
        if not answer_text:
            raise HTTPException(400, "answer is required")
        await presence.update_heartbeat(project_id)
        state = cmp_store.load_state(project_id)
        q = state.find_question(question_id)
        if q is None:
            raise HTTPException(404, f"question {question_id} not found")
        if q.digested:
            raise HTTPException(409, f"question {question_id} already digested")
        q.answer = answer_text
        q.answered_at = _now_iso()
        q.contradicted = False  # clear any previous contradiction flag
        q.ambiguity_accepted = False
        await cmp_store.save_questions(project_id, state.questions)
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": "compass",
            "project_id": project_id,
            "type": "compass_question_answered",
            "question_id": question_id,
        })
        return JSONResponse({"ok": True, "question_id": question_id})

    # ---------------------------------------- proposals

    @router.post("/proposals/settle/{statement_id}", dependencies=deps)
    async def resolve_settle(
        statement_id: str,
        body: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        action = (body.get("action") or "").strip().lower()
        if action not in ("confirm", "adjust", "reject"):
            raise HTTPException(400, "action must be confirm/adjust/reject")
        project_id = await resolve_active_project()
        await presence.update_heartbeat(project_id)
        state = cmp_store.load_state(project_id)
        proposal = next(
            (p for p in state.settle_proposals if p.statement_id == statement_id),
            None,
        )
        if proposal is None:
            raise HTTPException(404, f"no settle proposal for {statement_id}")
        if action == "reject":
            # Clear flag, drop proposal.
            stmt = state.find_statement(statement_id)
            if stmt:
                stmt.settle_proposed = False
            state.settle_proposals = [
                p for p in state.settle_proposals if p.statement_id != statement_id
            ]
        else:
            weight = 1.0 if proposal.direction == "yes" else 0.0
            if action == "adjust":
                try:
                    weight = float(body.get("weight"))
                except (TypeError, ValueError):
                    raise HTTPException(400, "adjust requires numeric weight 0..1")
                weight = max(0.0, min(1.0, weight))
            mutate.settle_statement(
                state, statement_id,
                weight=weight, direction=proposal.direction, run_id="human", by_human=True,
            )
            state.settle_proposals = [
                p for p in state.settle_proposals if p.statement_id != statement_id
            ]
        await cmp_store.save_lattice(project_id, state.statements)
        await cmp_store.save_proposals(
            project_id,
            settle=state.settle_proposals,
            stale=None,
            dupes=None,
        )
        await bus.publish({
            "ts": _now_iso(), "agent_id": "compass", "project_id": project_id,
            "type": "compass_proposal_resolved",
            "kind": "settle", "statement_id": statement_id, "action": action,
        })
        return JSONResponse({"ok": True})

    @router.post("/proposals/stale/{statement_id}", dependencies=deps)
    async def resolve_stale(
        statement_id: str,
        body: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        action = (body.get("action") or "").strip().lower()
        if action not in ("retire", "keep", "reformulate"):
            raise HTTPException(400, "action must be retire/keep/reformulate")
        project_id = await resolve_active_project()
        await presence.update_heartbeat(project_id)
        state = cmp_store.load_state(project_id)
        proposal = next(
            (p for p in state.stale_proposals if p.statement_id == statement_id),
            None,
        )
        if proposal is None:
            raise HTTPException(404, f"no stale proposal for {statement_id}")
        if action == "retire":
            mutate.retire_statement(state, statement_id, run_id="human")
        elif action == "keep":
            mutate.keep_stale(state, statement_id)
        else:  # reformulate
            new_text = (body.get("text") or proposal.reformulation or "").strip()
            if not new_text:
                raise HTTPException(400, "reformulate requires text")
            mutate.reformulate_statement(
                state, statement_id, new_text, run_id="human",
            )
        state.stale_proposals = [
            p for p in state.stale_proposals if p.statement_id != statement_id
        ]
        await cmp_store.save_lattice(project_id, state.statements)
        await cmp_store.save_proposals(
            project_id,
            settle=None,
            stale=state.stale_proposals,
            dupes=None,
        )
        await bus.publish({
            "ts": _now_iso(), "agent_id": "compass", "project_id": project_id,
            "type": "compass_proposal_resolved",
            "kind": "stale", "statement_id": statement_id, "action": action,
        })
        return JSONResponse({"ok": True})

    @router.post("/proposals/dupe/{proposal_id}", dependencies=deps)
    async def resolve_dupe(
        proposal_id: str,
        body: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        action = (body.get("action") or "").strip().lower()
        if action not in ("merge", "reject"):
            raise HTTPException(400, "action must be merge/reject")
        project_id = await resolve_active_project()
        await presence.update_heartbeat(project_id)
        state = cmp_store.load_state(project_id)
        proposal = next(
            (p for p in state.duplicate_proposals if p.id == proposal_id), None,
        )
        if proposal is None:
            raise HTTPException(404, f"no duplicate proposal {proposal_id}")
        if action == "merge":
            mutate.merge_duplicate_cluster(
                state, proposal.cluster_ids,
                merged_text=proposal.merged_text,
                merged_weight=proposal.merged_weight,
                region=proposal.region,
                run_id="human",
            )
        else:
            for sid in proposal.cluster_ids:
                stmt = state.find_statement(sid)
                if stmt:
                    stmt.dupe_proposed = False
        state.duplicate_proposals = [
            p for p in state.duplicate_proposals if p.id != proposal_id
        ]
        await cmp_store.save_lattice(project_id, state.statements)
        await cmp_store.save_proposals(
            project_id,
            settle=None,
            stale=None,
            dupes=state.duplicate_proposals,
        )
        await bus.publish({
            "ts": _now_iso(), "agent_id": "compass", "project_id": project_id,
            "type": "compass_proposal_resolved",
            "kind": "dupe", "proposal_id": proposal_id, "action": action,
        })
        return JSONResponse({"ok": True})

    @router.post("/proposals/reconcile/{proposal_id}", dependencies=deps)
    async def resolve_reconciliation(
        proposal_id: str,
        body: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        """Resolve a corpus↔lattice conflict (spec §3.0.1).

        Body:
          - `action`: required, one of:
              "update_lattice"  — sub-action via `lattice_action`
              "update_truth"    — informational; no lattice change
              "accept_ambiguity" — keep lattice and corpus, suppress
                                    re-detection until they shift
          - `lattice_action`: required when action=update_lattice. One
            of "unarchive" (return to active at moderate weight),
            "flip" (re-settle at the opposite direction), "reformulate"
            (replace text + reset), "replace" (archive the row, insert
            corpus-grounded equivalent).
          - `text`, `region`, `weight`: lattice_action-specific params.
        """
        action = (body.get("action") or "").strip().lower()
        if action not in ("update_lattice", "update_truth", "accept_ambiguity"):
            raise HTTPException(
                400,
                "action must be update_lattice / update_truth / accept_ambiguity",
            )
        project_id = await resolve_active_project()
        await presence.update_heartbeat(project_id)
        state = cmp_store.load_state(project_id)
        proposal = next(
            (p for p in state.reconciliation_proposals if p.id == proposal_id),
            None,
        )
        if proposal is None:
            raise HTTPException(404, f"no reconciliation proposal {proposal_id}")
        sid = proposal.statement_id

        if action == "update_lattice":
            la = (body.get("lattice_action") or "").strip().lower()
            if la == "unarchive":
                try:
                    weight = float(body.get("weight") or 0.5)
                except (TypeError, ValueError):
                    weight = 0.5
                if mutate.reconcile_unarchive(
                    state, sid, run_id="human", new_weight=weight,
                ) is None:
                    raise HTTPException(404, f"statement {sid} missing")
            elif la == "flip":
                if mutate.reconcile_flip_archive(
                    state, sid, run_id="human",
                ) is None:
                    raise HTTPException(
                        409,
                        f"statement {sid} can't be flipped (not directionally settled)",
                    )
            elif la == "reformulate":
                text = (body.get("text") or "").strip()
                if not text:
                    raise HTTPException(400, "reformulate requires text")
                region = (body.get("region") or None)
                if mutate.reconcile_reformulate(
                    state, sid, text, run_id="human", new_region=region,
                ) is None:
                    raise HTTPException(404, f"statement {sid} missing")
            elif la == "replace":
                text = (body.get("text") or "").strip()
                region = (body.get("region") or "").strip()
                if not text or not region:
                    raise HTTPException(400, "replace requires text + region")
                if mutate.reconcile_replace(
                    state, sid, new_text=text, region=region, run_id="human",
                ) is None:
                    raise HTTPException(404, f"statement {sid} missing")
            else:
                raise HTTPException(
                    400,
                    "lattice_action must be unarchive / flip / reformulate / replace",
                )
        elif action == "accept_ambiguity":
            mutate.reconcile_accept_ambiguity(state, sid)
        elif action == "update_truth":
            # Informational — the dashboard routes the human at the
            # truth file via the Files pane and the existing harness
            # flow handles the actual edit. No lattice change here.
            # We DO clear `reconciliation_proposed` on the cited
            # statement so the next corpus-changed run can re-detect
            # if the human's edit didn't actually resolve the
            # conflict (otherwise the row would be filtered out by
            # the eligibility check in `detect_conflicts`).
            stmt = state.find_statement(sid)
            if stmt is not None:
                stmt.reconciliation_proposed = False

        state.reconciliation_proposals = [
            p for p in state.reconciliation_proposals if p.id != proposal_id
        ]
        await cmp_store.save_lattice(project_id, state.statements)
        await cmp_store.save_proposals(
            project_id,
            settle=None, stale=None, dupes=None,
            reconcile=state.reconciliation_proposals,
        )
        await bus.publish({
            "ts": _now_iso(), "agent_id": "compass", "project_id": project_id,
            "type": "compass_proposal_resolved",
            "kind": "reconcile",
            "proposal_id": proposal_id,
            "statement_id": sid,
            "action": action,
            "lattice_action": body.get("lattice_action"),
        })
        return JSONResponse({"ok": True})

    # ---------------------------------------- statements

    @router.post("/statements/{statement_id}/weight", dependencies=deps)
    async def override_weight(
        statement_id: str,
        body: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        if not body.get("confirm"):
            raise HTTPException(400, "confirm flag required")
        try:
            weight = float(body.get("weight"))
        except (TypeError, ValueError):
            raise HTTPException(400, "numeric weight 0..1 required")
        project_id = await resolve_active_project()
        await presence.update_heartbeat(project_id)
        state = cmp_store.load_state(project_id)
        out = mutate.manual_weight_override(state, statement_id, weight, run_id="human")
        if out is None:
            raise HTTPException(404, f"statement {statement_id} not found or archived")
        await cmp_store.save_lattice(project_id, state.statements)
        return JSONResponse({"ok": True, "id": out.id, "weight": out.weight})

    @router.post("/statements/{statement_id}/restore", dependencies=deps)
    async def restore_statement(statement_id: str) -> JSONResponse:
        project_id = await resolve_active_project()
        await presence.update_heartbeat(project_id)
        state = cmp_store.load_state(project_id)
        out = mutate.restore_statement(state, statement_id)
        if out is None:
            raise HTTPException(404, f"statement {statement_id} not archived or not found")
        await cmp_store.save_lattice(project_id, state.statements)
        return JSONResponse({"ok": True, "id": out.id})

    # Truth management — NOT in this API. Truth lives in the project's
    # `<project>/truth/` folder, owned by the harness's existing flow:
    # humans edit via the Files pane, Coach proposes via
    # `coord_propose_file_write(scope='truth', ...)` for human approval. Compass reads
    # truth via `server.compass.truth.read_truth_facts` on every run
    # (Stage 0 truth-derive seeds the lattice). The dashboard surfaces
    # a read-only summary; edits happen elsewhere.

    @router.get("/truth", dependencies=deps)
    async def list_truth() -> JSONResponse:
        """Read-only view of the project's truth corpus, as Compass
        sees it. The dashboard renders this so the human can see what
        the truth-derive prompt is fed without leaving the Compass
        pane. Editing truth happens via the Files pane."""
        from server.compass.truth import read_truth_facts, read_truth_index_to_path  # noqa: PLC0415

        project_id = await resolve_active_project()
        facts = read_truth_facts(project_id)
        idx_to_path = read_truth_index_to_path(project_id)
        return JSONResponse({
            "facts": [
                {
                    "index": t.index,
                    "text": t.text,
                    "path": idx_to_path.get(t.index),
                    "added_at": t.added_at,
                }
                for t in facts
            ],
            "project_id": project_id,
        })

    # ---------------------------------------- ask (read-only LLM query)

    @router.post("/ask", dependencies=deps)
    async def ask(body: dict[str, Any] = Body(...)) -> JSONResponse:
        """Ask Compass a free-text question. Mirrors the
        `compass_ask` MCP tool but reachable from the dashboard so the
        human can sanity-check what Coach would see. Read-only — does
        NOT modify the lattice."""
        query_text = (body.get("query") or "").strip()
        if not query_text:
            raise HTTPException(400, "query required")
        project_id = await resolve_active_project()
        if not await _is_enabled(project_id):
            raise HTTPException(403, "Compass is disabled for this project")
        await presence.update_heartbeat(project_id)
        from server.compass import llm as cmp_llm  # noqa: PLC0415
        from server.compass import prompts as cmp_prompts  # noqa: PLC0415

        state = await cmp_store.load_with_meta(project_id)
        try:
            res = await cmp_llm.call(
                cmp_prompts.COACH_QUERY_SYSTEM,
                cmp_prompts.coach_query_user(state, query_text),
                project_id=project_id,
                label="compass:ask",
            )
        except Exception as e:
            raise HTTPException(500, f"compass.ask failed: {type(e).__name__}: {e}")
        return JSONResponse({"answer": (res.text or "").strip()})

    # ---------------------------------------- audit (manual submit)

    @router.post("/audit", dependencies=deps)
    async def submit_audit(body: dict[str, Any] = Body(...)) -> JSONResponse:
        artifact = (body.get("artifact") or "").strip()
        if not artifact:
            raise HTTPException(400, "artifact required")
        project_id = await resolve_active_project()
        if not await _is_enabled(project_id):
            raise HTTPException(403, "Compass is disabled for this project")
        await presence.update_heartbeat(project_id)
        verdict = await cmp_audit.audit_work(project_id, artifact)
        return JSONResponse(verdict)

    # ---------------------------------------- inputs

    @router.post("/inputs", dependencies=deps)
    async def record_input(body: dict[str, Any] = Body(...)) -> JSONResponse:
        kind = (body.get("kind") or "note").strip().lower()
        text = (body.get("body") or "").strip()
        if kind not in ("chat", "commit", "note"):
            raise HTTPException(400, "kind must be chat/commit/note")
        if not text:
            raise HTTPException(400, "body required")
        project_id = await resolve_active_project()
        await presence.update_heartbeat(project_id)
        # Recorded as a `compass_input` event so the runner can read
        # it from events on the next passive digest.
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": "human",
            "project_id": project_id,
            "type": "compass_input",
            "kind": kind,
            "body": text,
        })
        return JSONResponse({"ok": True})

    # ---------------------------------------- reset

    @router.post("/reset", dependencies=deps)
    async def reset(
        request: Request,
        body: dict[str, Any] = Body(default={}),
    ) -> JSONResponse:
        if not body.get("confirm"):
            raise HTTPException(400, "confirm flag required")
        actor = audit_actor(request)
        project_id = await resolve_active_project()
        await cmp_store.wipe_project(project_id)
        # Clear flags too — the project is now "freshly disabled".
        # Includes the truth-derive hash so the next run re-derives
        # from the truth/ folder (truth itself is untouched by reset).
        c = await configured_conn()
        try:
            await c.execute(
                "DELETE FROM team_config WHERE key IN (?, ?, ?, ?)",
                (
                    cmp_config.bootstrapped_key(project_id),
                    cmp_config.last_run_key(project_id),
                    cmp_config.heartbeat_key(project_id),
                    f"compass_truth_hash_{project_id}",
                ),
            )
            await c.commit()
        finally:
            await c.close()
        await bus.publish({
            "ts": _now_iso(), "agent_id": "compass", "project_id": project_id,
            "type": "compass_reset", "actor": actor,
        })
        return JSONResponse({"ok": True})

    return router


async def _safe_run(project_id: str, mode: str) -> None:
    try:
        await runner.run(project_id, mode=mode)
    except Exception:
        logger.exception("compass background run failed")


__all__ = ["build_router"]

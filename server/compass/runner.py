"""Compass run orchestrator (spec §3 — the pipeline).

`run(project_id, mode)` executes all stages in spec-mandated order.
Each stage emits a phase event (`compass_phase`) so the dashboard
shows live progress and writes its results to disk before moving
to the next stage. State is reloaded between stages where ordering
matters (e.g. region merge before reviews).

Modes:
  - `bootstrap` — fresh project. Generates 5 questions, no briefing
    (nothing to summarize yet), still updates CLAUDE.md so workers
    discover Compass on next turn.
  - `daily` — full pipeline. Requires `presence.human_reachable`.
  - `on_demand` — full pipeline. Always allowed (the human just
    triggered it; presence is implicit).

Per-project run lock: at most one run per project at a time. A
second concurrent call returns immediately with `skipped=True`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from server.events import bus

from server.compass import config, mutate, presence, store
from server.compass.pipeline import (
    briefing as pl_briefing,
    claude_md as pl_claude_md,
    digest as pl_digest,
    questions as pl_questions,
    regions as pl_regions,
    reviews as pl_reviews,
    truth_check as pl_truth_check,
    truth_derive as pl_truth_derive,
)
from server.compass.store import Question, RunLog, Statement

logger = logging.getLogger("harness.compass.runner")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Per-project asyncio locks. Built lazily so unit tests with multiple
# projects don't pre-allocate.
_run_locks: dict[str, asyncio.Lock] = {}


def _lock_for(project_id: str) -> asyncio.Lock:
    lk = _run_locks.get(project_id)
    if lk is None:
        lk = asyncio.Lock()
        _run_locks[project_id] = lk
    return lk


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_running(project_id: str) -> bool:
    """Cheap probe — true while a run is in-flight on this project."""
    lk = _run_locks.get(project_id)
    return lk is not None and lk.locked()


async def _emit_phase(project_id: str, run_id: str, phase: str, **extra: Any) -> None:
    payload: dict[str, Any] = {
        "ts": _now_iso(),
        "agent_id": "compass",
        "project_id": project_id,
        "type": "compass_phase",
        "run_id": run_id,
        "phase": phase,
    }
    payload.update(extra)
    try:
        await bus.publish(payload)
    except Exception:
        logger.exception("compass.runner: phase publish failed")


# --------------------------------------------------------- run()


async def run(project_id: str, mode: str = "daily") -> dict[str, Any]:
    """Execute one Compass run. Returns the run-log dict."""
    if mode not in ("bootstrap", "daily", "on_demand"):
        raise ValueError(f"invalid mode: {mode}")

    # Lock per-project. If a run is already running, skip.
    lock = _lock_for(project_id)
    if lock.locked():
        return {"run_id": None, "skipped": True, "skipped_reason": "already running"}

    async with lock:
        return await _run_locked(project_id, mode)


async def _run_locked(project_id: str, mode: str) -> dict[str, Any]:
    run_id = store.next_run_id()
    started_iso = _now_iso()
    log = RunLog(run_id=run_id, started_at=started_iso, mode=mode)

    # Presence gate — daily only. Bootstrap is opt-in by the human;
    # on_demand is human-triggered.
    if mode == "daily":
        try:
            reachable = await presence.human_reachable(project_id)
        except Exception:
            reachable = True  # fail-open; rather run than silently freeze
        if not reachable:
            try:
                await presence.send_reminder(project_id)
            except Exception:
                pass
            log.skipped = True
            log.skipped_reason = "no human signal in window"
            log.completed = False
            log.finished_at = _now_iso()
            await store.append_run_log(project_id, log)
            return {
                "run_id": log.run_id,
                "started_at": log.started_at,
                "finished_at": log.finished_at,
                "mode": log.mode,
                "skipped": True,
                "skipped_reason": log.skipped_reason,
            }

    # Ensure scaffolded + bootstrapped state files exist.
    await store.bootstrap_state(project_id)

    await _emit_phase(project_id, run_id, "started", mode=mode)

    state = store.load_state(project_id)

    # ============================================================
    # 0. Truth-derive — read truth/ folder, infer lattice statements.
    #    Idempotent: skip the LLM call when the truth corpus hash
    #    is unchanged AND the lattice already has truth-derived
    #    content. The user's principle: "if truth doesn't change,
    #    don't infer new statements."
    # ============================================================
    truth_hash = pl_truth_derive.truth_corpus_hash(state.truth)
    last_hash = await _read_team_config(_truth_hash_key(project_id))
    has_truth_grounded = any(
        s.created_by == "compass-truth" for s in state.statements
    )
    should_derive = bool(state.truth) and (
        truth_hash != last_hash or not has_truth_grounded
    )
    if should_derive:
        await _emit_phase(
            project_id, run_id, "truth_derive",
            truth_files=len(state.truth),
        )
        try:
            td_res = await pl_truth_derive.derive_from_truth(state)
        except Exception:
            logger.exception("compass.runner: truth_derive raised")
            td_res = None
        if td_res and td_res.statements:
            now = _now_iso()
            added: list[Statement] = []
            for proposal in td_res.statements:
                mutate.ensure_region(state, proposal["region"])
                sid = store.next_statement_id(state)
                stmt = Statement(
                    id=sid,
                    text=proposal["text"],
                    region=proposal["region"],
                    weight=pl_truth_derive.TRUTH_DERIVED_WEIGHT,
                    created_at=now,
                    created_by="compass-truth",
                    history=[{
                        "run_id": run_id,
                        "delta": 0.0,
                        "rationale": proposal.get("rationale") or "derived from truth",
                        "source": "truth_derive",
                    }],
                )
                state.statements.append(stmt)
                added.append(stmt)
            log.notes.append(f"truth_derive: {len(added)} new statement(s)")
            if added:
                await store.save_lattice(project_id, state.statements)
                await store.save_regions(
                    project_id, state.regions, state.region_merge_history,
                )
                await bus.publish({
                    "ts": _now_iso(),
                    "agent_id": "compass",
                    "project_id": project_id,
                    "type": "compass_truth_derived",
                    "added": [s.id for s in added],
                    "run_id": run_id,
                })
        # Always persist the new truth hash on a successful pass —
        # even when the LLM returned zero statements (the corpus has
        # been considered; nothing new to add). Skips next run.
        await _write_team_config(_truth_hash_key(project_id), truth_hash)
    elif not state.truth:
        log.notes.append("truth_derive: skipped (truth/ folder empty)")

    # ============================================================
    # 1. Digest answered questions (with truth-check)
    # ============================================================
    answered = [
        q for q in state.questions
        if q.answer is not None
        and not q.digested
        and not q.contradicted
        and not q.ambiguity_accepted
    ]
    if answered:
        await _emit_phase(project_id, run_id, "digesting_answers", count=len(answered))
    for q in answered:
        try:
            tc = await pl_truth_check.check(
                state.truth,
                question_text=q.q,
                prediction=q.prediction,
                answer_text=q.answer or "",
                project_id=project_id,
            )
        except Exception:
            logger.exception("compass.runner: truth check raised")
            continue
        if tc.contradiction:
            q.contradicted = True
            log.contradictions += 1
            await store.save_questions(project_id, state.questions)
            try:
                await bus.publish({
                    "ts": _now_iso(),
                    "agent_id": "compass",
                    "project_id": project_id,
                    "type": "compass_truth_contradiction",
                    "question_id": q.id,
                    "conflicts": tc.conflicts,
                    "summary": tc.summary,
                })
            except Exception:
                pass
            continue
        try:
            digest_res = await pl_digest.answer(
                state,
                question_text=q.q,
                prediction=q.prediction,
                targets=q.targets,
                answer_text=q.answer or "",
            )
        except Exception:
            logger.exception("compass.runner: answer digest raised; skipping question")
            continue
        applied = mutate.apply_statement_updates(
            state,
            digest_res.updates,
            run_id=run_id,
            source=f"answer:{q.id}",
            delta_max=config.ANSWER_DELTA_MAX,
        )
        added = mutate.apply_new_statements(
            state,
            digest_res.new_statements,
            run_id=run_id,
            source=f"answer:{q.id}",
        )
        log.truth_candidates.extend(digest_res.truth_candidates)
        q.digested = True
        q.digested_in_run = run_id
        log.answered_questions += 1
        await store.save_lattice(project_id, state.statements)
        await store.save_questions(project_id, state.questions)
        await store.save_regions(
            project_id, state.regions, state.region_merge_history
        )
        try:
            await bus.publish({
                "ts": _now_iso(),
                "agent_id": "compass",
                "project_id": project_id,
                "type": "compass_question_digested",
                "question_id": q.id,
                "applied_updates": applied,
                "new_statements": [s.id for s in added],
                "surprise": digest_res.surprise,
            })
        except Exception:
            pass

    # ============================================================
    # 2. Passive digest (if any signals)
    # ============================================================
    state = store.load_state(project_id)
    signals = await _collect_signals(project_id, since_iso=_last_run_iso(project_id))
    await _emit_phase(project_id, run_id, "passive_digest", signals=len(signals))
    try:
        passive_res = await pl_digest.passive(state, signals=signals)
    except Exception:
        logger.exception("compass.runner: passive digest raised")
        passive_res = None
    if passive_res is not None:
        mutate.apply_statement_updates(
            state,
            passive_res.updates,
            run_id=run_id,
            source="passive",
            delta_max=config.PASSIVE_DELTA_MAX,
        )
        mutate.apply_new_statements(
            state, passive_res.new_statements, run_id=run_id, source="passive",
        )
        log.passive = passive_res.summary_dict()
        log.truth_candidates.extend(passive_res.truth_candidates)
        await store.save_lattice(project_id, state.statements)
        await store.save_regions(
            project_id, state.regions, state.region_merge_history,
        )

    # ============================================================
    # 3. Region auto-merge (if over soft cap)
    # ============================================================
    state = store.load_state(project_id)
    if len(state.active_regions()) > config.REGION_SOFT_CAP:
        await _emit_phase(project_id, run_id, "region_merge",
                          active=len(state.active_regions()))
        try:
            merges = await pl_regions.auto_merge(state)
        except Exception:
            logger.exception("compass.runner: region auto-merge raised")
            merges = []
        for m in merges:
            mutate.apply_region_merge(
                state, from_=m.from_, to=m.to, run_id=run_id,
            )
            log.region_merges.append({"from": m.from_, "to": m.to})
        if merges:
            await store.save_lattice(project_id, state.statements)
            await store.save_regions(
                project_id, state.regions, state.region_merge_history,
            )

    # ============================================================
    # 4-6. Reviews + duplicate detection
    # ============================================================
    state = store.load_state(project_id)
    # Increment pending counters BEFORE re-detection — so a fresh
    # detection can re-add cleared proposals if they're still due.
    pl_reviews.increment_pending_runs(
        state.settle_proposals,
        state.stale_proposals,
        state.duplicate_proposals,
    )
    settle_kept, stale_kept, dupes_kept = pl_reviews.expire_old_proposals(
        state.settle_proposals,
        state.stale_proposals,
        state.duplicate_proposals,
    )

    await _emit_phase(project_id, run_id, "reviews")
    try:
        rev = await pl_reviews.propose(state, run_id=run_id, run_iso=_now_iso())
    except Exception:
        logger.exception("compass.runner: reviews raised")

        class _R:
            settle: list = []
            stale: list = []
        rev = _R()  # type: ignore[assignment]

    try:
        dupes_new = await pl_reviews.detect_duplicates(
            state, run_id=run_id, run_iso=_now_iso(),
        )
    except Exception:
        logger.exception("compass.runner: duplicate detection raised")
        dupes_new = []

    # Merge: keep non-expired pre-existing + add fresh ones (deduped).
    settle_combined = settle_kept + [
        p for p in rev.settle
        if p.statement_id not in {x.statement_id for x in settle_kept}
    ]
    stale_combined = stale_kept + [
        p for p in rev.stale
        if p.statement_id not in {x.statement_id for x in stale_kept}
    ]
    # Dupe ids are unique per generation; we just append.
    dupes_combined = dupes_kept + dupes_new

    pl_reviews.mark_proposed_flags(
        state, settle=settle_combined, stale=stale_combined, dupes=dupes_combined,
    )

    log.settle_proposed = len(settle_combined)
    log.stale_proposed = len(stale_combined)
    log.dupe_proposed = len(dupes_combined)

    await store.save_proposals(
        project_id,
        settle=settle_combined,
        stale=stale_combined,
        dupes=dupes_combined,
    )
    await store.save_lattice(project_id, state.statements)

    # ============================================================
    # 7. Generate new questions
    # ============================================================
    state = store.load_state(project_id)
    n_q = (
        config.QUESTIONS_PER_BOOTSTRAP_RUN
        if mode == "bootstrap"
        else config.QUESTIONS_PER_DAILY_RUN
    )
    await _emit_phase(project_id, run_id, "generate_questions", count=n_q)
    try:
        new_qs = await pl_questions.generate_batch(state, count=n_q)
    except Exception:
        logger.exception("compass.runner: question generation raised")
        new_qs = []
    for proposal in new_qs:
        qid = store.next_question_id(state)
        state.questions.append(Question(
            id=qid,
            q=proposal.q,
            prediction=proposal.prediction,
            targets=proposal.targets,
            rationale=proposal.rationale,
            asked_at=_now_iso(),
            asked_in_run=run_id,
        ))
    log.questions_generated = len(new_qs)
    if new_qs:
        await store.save_questions(project_id, state.questions)

    # ============================================================
    # 8. Briefing — skip on bootstrap
    # ============================================================
    if mode != "bootstrap":
        state = store.load_state(project_id)
        await _emit_phase(project_id, run_id, "briefing")
        try:
            briefing_md = await pl_briefing.generate(state, recent={
                "answered_questions": log.answered_questions,
                "passive": log.passive,
                "settle_proposed": log.settle_proposed,
                "stale_proposed": log.stale_proposed,
                "dupe_proposed": log.dupe_proposed,
                "region_merges": log.region_merges,
            })
        except Exception:
            logger.exception("compass.runner: briefing raised")
            briefing_md = None
        if briefing_md:
            today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            briefing_path = await store.write_briefing(project_id, today_iso, briefing_md)
            log.briefing_path = str(briefing_path)

    # ============================================================
    # 9. CLAUDE.md block
    # ============================================================
    state = store.load_state(project_id)
    await _emit_phase(project_id, run_id, "claude_md_block")
    try:
        block_body = await pl_claude_md.generate(state)
        await pl_claude_md.inject(project_id, block_body)
    except Exception:
        logger.exception("compass.runner: CLAUDE.md block raised")

    # ============================================================
    # Finalize
    # ============================================================
    log.completed = True
    log.finished_at = _now_iso()
    await store.append_run_log(project_id, log)

    # Update last-run timestamp + bootstrapped flag in team_config so
    # the scheduler doesn't re-fire today and can distinguish
    # never-bootstrapped projects.
    await _record_last_run(project_id, log.finished_at, was_bootstrap=(mode == "bootstrap"))

    try:
        await bus.publish({
            "ts": log.finished_at,
            "agent_id": "compass",
            "project_id": project_id,
            "type": "compass_run_completed",
            "run_id": run_id,
            "mode": mode,
            "summary": {
                "answered_questions": log.answered_questions,
                "questions_generated": log.questions_generated,
                "settle_proposed": log.settle_proposed,
                "stale_proposed": log.stale_proposed,
                "dupe_proposed": log.dupe_proposed,
                "region_merges": log.region_merges,
                "contradictions": log.contradictions,
            },
        })
    except Exception:
        pass

    # The store helper is private (named with underscore); call it
    # explicitly for the run-log return.
    return {
        "run_id": log.run_id,
        "started_at": log.started_at,
        "finished_at": log.finished_at,
        "mode": log.mode,
        "completed": log.completed,
        "passive": log.passive,
        "answered_questions": log.answered_questions,
        "contradictions": log.contradictions,
        "region_merges": log.region_merges,
        "settle_proposed": log.settle_proposed,
        "stale_proposed": log.stale_proposed,
        "dupe_proposed": log.dupe_proposed,
        "questions_generated": log.questions_generated,
        "truth_candidates": log.truth_candidates,
        "briefing_path": log.briefing_path,
        "notes": log.notes,
        "skipped": log.skipped,
        "skipped_reason": log.skipped_reason,
    }


# ----------------------------------------------------- helpers


def _last_run_iso(project_id: str) -> str | None:
    runs = store.read_run_log(project_id)
    if not runs:
        return None
    last = runs[-1]
    return last.finished_at or last.started_at


async def _collect_signals(project_id: str, *, since_iso: str | None) -> list[dict[str, Any]]:
    """Gather human-authored signals since the last run.

    Currently sources:
      - `messages` rows where from_id='human' since `since_iso`
      - The dashboard's manual-input rows (recorded as
        events with type='compass_input')

    The list is capped at ~50 entries to keep prompts compact; older
    signals roll off (passive digest is meant for *recent* activity).
    """
    from server.db import configured_conn  # lazy

    if since_iso is None:
        # Default: last 24h.
        since_dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        since_iso = since_dt.isoformat()

    out: list[dict[str, Any]] = []
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT sent_at, body FROM messages "
                "WHERE from_id = 'human' AND project_id = ? AND sent_at >= ? "
                "ORDER BY sent_at DESC LIMIT 25",
                (project_id, since_iso),
            )
            for r in await cur.fetchall():
                row = dict(r)
                out.append({"kind": "chat", "ts": row["sent_at"], "body": row["body"]})
            cur = await c.execute(
                "SELECT ts, payload FROM events "
                "WHERE type = 'compass_input' AND project_id = ? AND ts >= ? "
                "ORDER BY ts DESC LIMIT 25",
                (project_id, since_iso),
            )
            for r in await cur.fetchall():
                row = dict(r)
                payload = row.get("payload") or "{}"
                try:
                    import json as _json
                    parsed = _json.loads(payload)
                except Exception:
                    parsed = {}
                kind = str(parsed.get("kind") or "note")
                body = str(parsed.get("body") or "")
                if body:
                    out.append({"kind": kind, "ts": row["ts"], "body": body})
        finally:
            await c.close()
    except Exception:
        logger.exception("compass.runner: signal collection failed")
    return out


def _truth_hash_key(project_id: str) -> str:
    """team_config key for the most recent truth corpus hash. Stage 0
    short-circuits when this matches the live truth."""
    return f"compass_truth_hash_{project_id}"


async def _read_team_config(key: str) -> str:
    """Tiny helper around team_config — returns "" on missing or DB
    error so the caller can short-circuit without try/except."""
    from server.db import configured_conn  # lazy

    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT value FROM team_config WHERE key = ?", (key,)
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        return ""
    return (dict(row).get("value") if row else "") or ""


async def _write_team_config(key: str, value: str) -> None:
    from server.db import configured_conn  # lazy

    try:
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO team_config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("compass.runner: team_config write failed (%s)", key)


async def _record_last_run(
    project_id: str, finished_at_iso: str, *, was_bootstrap: bool
) -> None:
    from server.db import configured_conn  # lazy

    try:
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO team_config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (config.last_run_key(project_id), finished_at_iso),
            )
            if was_bootstrap:
                await c.execute(
                    "INSERT INTO team_config (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (config.bootstrapped_key(project_id), "1"),
                )
            await c.commit()
        finally:
            await c.close()
    except Exception:
        logger.exception("compass.runner: last-run write failed")


__all__ = ["run", "is_running"]

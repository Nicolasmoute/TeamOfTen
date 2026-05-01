"""Audit subsystem (spec §5).

Coach calls `compass_audit(artifact)` whenever a worker produces a
meaningful unit of work. Compass returns a verdict and writes it to
the audit log. Audits are advisory — never blocking.

Verdicts:
  - **aligned** — silent OK to coach; logged; human not notified.
  - **confident_drift** — work clearly contradicts a >0.8 / <0.2
    statement; direct message to coach; human NOT pushed (§10.5).
  - **uncertain_drift** — work seems off but relevant statements are
    in 0.3–0.7; coach proceeds cautiously; a question is queued for
    the human (with a prediction).

Rollup safety net (§5.4): every `AUDIT_ROLLUP_INTERVAL` audits, scan
the most recent N audits for region drift. If many recent audits in
the same region drifted, queue a meta-question — the lattice may be
wrong, not the work.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any

from server.events import bus

from server.compass import config, llm, prompts, store
from server.compass.store import (
    AuditRecord,
    LatticeState,
    Question,
)

logger = logging.getLogger("harness.compass.audit")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


VALID_VERDICTS = ("aligned", "confident_drift", "uncertain_drift")


async def audit_work(project_id: str, artifact: str) -> dict[str, Any]:
    """Audit a work artifact against the lattice. Persists to
    audits.jsonl and (for uncertain_drift) queues a question.

    Returns a dict in the shape of `compass_audit`'s MCP contract.
    Raises only on configuration errors; LLM/parse failures degrade
    to an `aligned` verdict with a diagnostic in `summary`.
    """
    # Use load_with_meta so the audit prompt is anchored on the
    # project's identity (not the harness's). Without this, audit
    # output would happily reason about agent slot names and other
    # harness chatter as if they were project-domain content.
    state = await store.load_with_meta(project_id)
    artifact = (artifact or "").strip()
    if not artifact:
        return _empty_audit_response("empty artifact")

    try:
        res = await llm.call(
            prompts.AUDIT_SYSTEM,
            prompts.audit_user(state, artifact),
            max_tokens=config.LLM_MAX_TOKENS_AUDIT,
            project_id=project_id,
            label="compass:audit",
        )
        parsed = llm.parse_json_safe(res.text) or {}
    except Exception:
        logger.exception("compass.audit: LLM call failed")
        return _empty_audit_response("audit LLM call failed; defaulting to aligned")

    verdict = str(parsed.get("verdict") or "aligned").strip()
    if verdict not in VALID_VERDICTS:
        verdict = "aligned"
    summary = str(parsed.get("summary") or "").strip()
    contradicting = _sanitize_str_list(parsed.get("contradicting_ids"))
    message_to_coach = str(parsed.get("message_to_coach") or "").strip()

    audit_id = store.next_audit_id()
    question_id: str | None = None

    if verdict == "uncertain_drift":
        q_for = parsed.get("question_for_human")
        if isinstance(q_for, dict):
            qtext = str(q_for.get("q") or "").strip()
            qpred = str(q_for.get("prediction") or "").strip()
            qtargets = _sanitize_str_list(q_for.get("targets"))
            if qtext and qpred:
                question_id = await _queue_question_from_audit(
                    project_id=project_id,
                    state=state,
                    qtext=qtext,
                    qprediction=qpred,
                    qtargets=qtargets,
                    audit_id=audit_id,
                    audit_summary=summary,
                )

    record = AuditRecord(
        id=audit_id,
        ts=_now_iso(),
        artifact=artifact,
        verdict=verdict,
        summary=summary,
        contradicting_ids=contradicting,
        message_to_coach=message_to_coach,
        question_id=question_id,
    )
    await store.append_audit(project_id, record)

    # Bus event so the dashboard updates the audit log live.
    try:
        await bus.publish({
            "ts": record.ts,
            "agent_id": "compass",
            "project_id": project_id,
            "type": "compass_audit_logged",
            "audit_id": record.id,
            "verdict": record.verdict,
            "contradicting_ids": record.contradicting_ids,
            "question_id": record.question_id,
        })
    except Exception:
        logger.exception("compass.audit: bus publish failed")

    # Rollup check — only on confident/uncertain drift; aligned audits
    # don't motivate a meta-review.
    if verdict in ("confident_drift", "uncertain_drift"):
        try:
            await _maybe_rollup_meta_question(project_id)
        except Exception:
            logger.exception("compass.audit: rollup check failed")

    return {
        "verdict": record.verdict,
        "summary": record.summary,
        "contradicting_ids": record.contradicting_ids,
        "message_to_coach": record.message_to_coach,
        "question_id": record.question_id,
    }


async def _queue_question_from_audit(
    *,
    project_id: str,
    state: LatticeState,
    qtext: str,
    qprediction: str,
    qtargets: list[str],
    audit_id: str,
    audit_summary: str,
) -> str:
    """Append an audit-driven question to questions.json. Returns id."""
    qid = store.next_question_id(state)
    q = Question(
        id=qid,
        q=qtext,
        prediction=qprediction,
        targets=qtargets,
        rationale=f"Generated from audit drift: {audit_summary}"[:500],
        asked_at=_now_iso(),
        asked_in_run="audit",
        from_audit=audit_id,
    )
    state.questions.append(q)
    await store.save_questions(project_id, state.questions)
    try:
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": "compass",
            "project_id": project_id,
            "type": "compass_question_queued",
            "question_id": qid,
            "from_audit": audit_id,
        })
    except Exception:
        logger.exception("compass.audit: question_queued publish failed")
    return qid


async def _maybe_rollup_meta_question(project_id: str) -> None:
    """Spec §5.4 safety net: every Nth audit, scan recent drifts. If
    a single region accumulates ≥ 3 drift verdicts in the last
    `AUDIT_ROLLUP_INTERVAL` audits, queue a meta-question about
    that region. Heuristic — the spec leaves the threshold loose;
    we pick conservative values.
    """
    interval = config.AUDIT_ROLLUP_INTERVAL
    if interval <= 0:
        return
    audits = store.read_audits(project_id)
    if len(audits) < interval or len(audits) % interval != 0:
        return

    recent = audits[-interval:]
    drifts = [a for a in recent if a.verdict in ("confident_drift", "uncertain_drift")]
    if len(drifts) < 3:
        return

    # Map contradicting statement ids to regions to find concentration.
    state = store.load_state(project_id)
    by_id = {s.id: s for s in state.statements}
    region_counts: dict[str, int] = {}
    for a in drifts:
        for sid in a.contradicting_ids:
            stmt = by_id.get(sid)
            if not stmt:
                continue
            region_counts[stmt.region] = region_counts.get(stmt.region, 0) + 1
    if not region_counts:
        return
    top_region, top_count = max(region_counts.items(), key=lambda kv: kv[1])
    if top_count < 3:
        return

    # Don't double-queue: skip if a meta-question for this region is
    # already pending.
    pending_q = [
        q for q in state.questions
        if not q.digested and not q.contradicted
        and q.from_audit is None
        and "lattice may be wrong" in q.q.lower()
        and top_region in q.q.lower()
    ]
    if pending_q:
        return

    qid = store.next_question_id(state)
    meta_q = Question(
        id=qid,
        q=(
            f"Most recent worker outputs in the '{top_region}' region have drifted "
            "from the lattice. Is the lattice may be wrong about {region}? "
            "(Yes = the lattice needs revision; No = workers need redirection.)"
            .replace("{region}", top_region)
        ),
        prediction="The lattice probably needs revision in this region.",
        targets=[],
        rationale=(
            f"Audit rollup: {top_count} of the last {interval} audits drifted "
            f"in '{top_region}'."
        ),
        asked_at=_now_iso(),
        asked_in_run="audit-rollup",
        from_audit=None,
    )
    state.questions.append(meta_q)
    await store.save_questions(project_id, state.questions)
    try:
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": "compass",
            "project_id": project_id,
            "type": "compass_question_queued",
            "question_id": qid,
            "rollup_region": top_region,
            "rollup_count": top_count,
        })
    except Exception:
        logger.exception("compass.audit: rollup publish failed")


def _empty_audit_response(reason: str) -> dict[str, Any]:
    return {
        "verdict": "aligned",
        "summary": reason,
        "contradicting_ids": [],
        "message_to_coach": "OK · aligned with lattice (degraded)",
        "question_id": None,
    }


def _sanitize_str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if isinstance(x, (str, int, float)) and str(x).strip()]


__all__ = ["audit_work", "VALID_VERDICTS"]

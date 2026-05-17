from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from server.db import configured_conn
from server.events import bus


ACTION_NEEDED_VERDICTS: frozenset[str] = frozenset({
    "truthgate_needs_truth_change",
    "truthgate_needs_human_clarification",
    "truthgate_rejected_or_needs_human_clarification",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _list_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = [value]
    if not isinstance(value, list):
        return "(none recorded)"
    items = [str(x).strip() for x in value if str(x).strip()]
    return ", ".join(items[:8]) if items else "(none recorded)"


def _clip(value: Any, limit: int = 900) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _expected_action(verdict: str | None, *, classifier_error: str | None) -> str:
    if classifier_error or verdict == "classifier_error":
        return (
            "Inspect the classifier failure, repair the cause, then rerun "
            "with coord_run_truthgate(force=true), or deliberately leave "
            "the task blocked."
        )
    if verdict == "truthgate_needs_truth_change":
        return (
            "Propose the required protected truth amendment, record an "
            "allowed override with rationale, archive/rewrite the task, or "
            "deliberately leave it blocked."
        )
    return (
        "Ask the human for clarification, rewrite/archive the task, record "
        "an allowed override with rationale, or deliberately leave it "
        "blocked."
    )


async def _insert_coach_message_and_wake(
    *,
    project_id: str,
    subject: str,
    body: str,
    wake_source: str,
) -> None:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO messages "
            "(project_id, from_id, to_id, subject, body, priority) "
            "VALUES (?, 'truthgate', 'coach', ?, ?, 'interrupt') "
            "RETURNING id",
            (project_id, subject[:200], body[:5000]),
        )
        row = await cur.fetchone()
        msg_id = dict(row)["id"] if row else None
        await c.commit()
    finally:
        await c.close()

    await bus.publish({
        "ts": _now_iso(),
        "type": "message_sent",
        "agent_id": "truthgate",
        "project_id": project_id,
        "message_id": msg_id,
        "to": "coach",
        "subject": subject[:200],
        "body_preview": body[:4000],
        "body_full_len": len(body),
        "body_truncated": len(body) > 4000,
        "priority": "interrupt",
    })
    try:
        from server.agents import maybe_wake_agent  # noqa: PLC0415

        await maybe_wake_agent(
            "coach",
            f"New TruthGate action-needed signal: {subject}\n\n{body[:3500]}",
            bypass_debounce=True,
            wake_source=wake_source,
        )
    except Exception:
        pass


async def notify_coach_truthgate_action_needed(
    *,
    project_id: str,
    task: dict[str, Any],
    payload: dict[str, Any],
    trigger: str,
    classifier_error: str | None = None,
) -> None:
    verdict = payload.get("verdict")
    verdict_s = str(verdict or "").strip()
    if not classifier_error and verdict_s not in ACTION_NEEDED_VERDICTS:
        return
    task_id = str(task.get("id") or payload.get("task_id") or "").strip()
    title = str(task.get("title") or "(untitled)").strip()
    failure_or_verdict = "classifier_error" if classifier_error else verdict_s
    reason = (
        payload.get("blocked_reason")
        or payload.get("truthgate_warning")
        or classifier_error
        or payload.get("truthgate_override_rationale")
        or "TruthGate requires Coach action."
    )
    subject = f"TruthGate action needed: {task_id}"
    body = "\n".join([
        f"Task: {task_id} — {title}",
        f"Trigger: {trigger}",
        f"Verdict/failure: {failure_or_verdict}",
        f"Reason: {_clip(reason)}",
        f"Truth basis: {_list_text(payload.get('truth_basis'))}",
        f"Concerns: {_list_text(payload.get('truth_concerns'))}",
        f"Expected Coach action: {_expected_action(verdict_s, classifier_error=classifier_error)}",
        "",
        "No Player was woken and the task remains in truthgate until Coach acts.",
    ])
    await _insert_coach_message_and_wake(
        project_id=project_id,
        subject=subject,
        body=body,
        wake_source="truthgate_action_needed",
    )


async def notify_coach_truth_amendment_resolved(
    *,
    project_id: str,
    task_id: str,
    title: str | None,
    proposal_id: int,
    path: str,
    status: str,
) -> None:
    if status != "approved":
        return
    title_s = (title or "(untitled)").strip()
    subject = f"Truth amendment approved: rerun TruthGate for {task_id}"
    body = "\n".join([
        f"Task: {task_id} — {title_s}",
        f"Approved truth proposal: #{proposal_id} ({path})",
        "Expected Coach action: rerun TruthGate with coord_run_truthgate(force=true), "
        "then approve the next stage only if the task records a pass/override, "
        "or deliberately leave it blocked.",
        "",
        "The approval cleared the pending amendment marker, but did not auto-run "
        "the classifier or advance the task.",
    ])
    await _insert_coach_message_and_wake(
        project_id=project_id,
        subject=subject,
        body=body,
        wake_source="truthgate_amendment_resolved",
    )

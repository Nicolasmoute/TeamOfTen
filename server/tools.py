from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, get_args, get_origin

from claude_agent_sdk import create_sdk_mcp_server, tool

from server import knowledge as knowmod
from server import outputs as outmod
from server.db import configured_conn, resolve_active_project
from server.events import bus
from server.protected_file_limits import (
    COORD_READ_FILE_MAX_CHARS,
    FILE_WRITE_PROPOSAL_MAX_CHARS,
)
from server.webdav import webdav
from server.workspaces import project_repo_configured, workspace_dir


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_task_id() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"t-{today}-{uuid.uuid4().hex[:8]}"


def _role_token_for_stage(stage: str) -> str:
    """Map a kanban stage to the assign-tool's role token. Used by
    response strings that point Coach at the right
    `coord_assign_<role>` follow-up (v0.3.11).
    `audit_syntax`/`audit_semantics` collapse to `auditor` because
    `coord_assign_auditor` takes the kind as a param, not in the
    tool name."""
    return {
        "plan": "planner",
        "execute": "task",
        "audit_syntax": "auditor",
        "audit_semantics": "auditor",
        "ship": "shipper",
        "verify": "verifier",
    }.get(stage, "task")


# Kanban-shaped task state machine (Docs/kanban-specs.md §2). Reject
# transitions not listed here. The legacy enum values (open/claimed/
# in_progress/blocked/done/cancelled) are accepted as input aliases
# during a one-release deprecation window: 'done'/'cancelled' map to
# 'archive', 'in_progress' is treated as a no-op for tasks already in
# 'execute', etc. See `_normalize_status_alias`.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "plan":            {"execute", "archive"},
    "execute":         {"audit_syntax", "audit_semantics", "ship", "archive"},
    "audit_syntax":    {"audit_semantics", "ship", "archive", "execute"},
    "audit_semantics": {"ship", "archive", "execute"},
    "ship":            {"verify", "archive"},
    "verify":          {"archive", "execute", "ship"},
    "archive":         set(),
}

# Stages the kanban subscriber and Coach see as "the audit loop".
AUDIT_STAGES: frozenset[str] = frozenset({"audit_syntax", "audit_semantics"})

# All valid kanban stages (used by validators that accept any of them).
ALL_KANBAN_STAGES: frozenset[str] = frozenset(VALID_TRANSITIONS.keys())


# Crystallized turn-end discipline reminder. Appended to every wake
# body fired AT a Player from any harness path (Coach via
# coord_approve_stage / coord_create_task / coord_request_plan_review
# / coord_send_message; harness via kanban stall / idle poller /
# watchdog; human via /api/tasks/{id}/approve_stage). Lives here in
# code rather than in the per-project CLAUDE.md template — the
# template can be edited / drift / get rewritten by the Coach
# reconciliation flow, while this constant is part of the SDK-level
# return surface and survives all of those. Token cost is a few
# tokens × wakes-per-turn; in exchange the rule lands at exactly
# the moment the Player is reasoning about "what next?".
COACH_TO_PLAYER_TURN_END_REMINDER = (
    "\n\n— Don't end work turn without a coord_* signal to Coach."
)


def _with_player_reminder(body: str) -> str:
    """Append the canonical turn-end reminder to a Player wake body.
    Idempotent — if the reminder is already present (e.g., a caller
    composed it inline), return as-is."""
    if not body:
        return COACH_TO_PLAYER_TURN_END_REMINDER.lstrip("\n")
    if COACH_TO_PLAYER_TURN_END_REMINDER.strip() in body:
        return body
    return body.rstrip() + COACH_TO_PLAYER_TURN_END_REMINDER

# Workflow metadata. The DB stores these as text/JSON for migration
# friendliness; these constants are the tool/API validation contract.
WORKFLOW_TYPES: frozenset[str] = frozenset({
    "code", "research", "writing", "marketing", "ops", "generic",
})
# `tracking_reason` is informational metadata in v0.3 (the v0.2 admission
# gate was dropped — every Coach delegation goes through kanban). The
# field accepts any non-empty string; no enum validation. Kept for
# filtering / analytics consumers.

# Canonical stage order for trajectory validation. Trajectory entries
# must appear in this order; `execute` is mandatory; `archive` is
# implicit/terminal and not stored in trajectory rows.
TRAJECTORY_STAGES: tuple[str, ...] = (
    "plan", "execute", "audit_syntax", "audit_semantics", "ship", "verify",
)
TRAJECTORY_STAGE_INDEX: dict[str, int] = {
    s: i for i, s in enumerate(TRAJECTORY_STAGES)
}

ROLE_STAGE: dict[str, str] = {
    "planner": "plan",
    "executor": "plan",
    "auditor_syntax": "audit_syntax",
    "auditor_semantics": "audit_semantics",
    "shipper": "ship",
    "verifier": "verify",
}
STAGE_ROLE: dict[str, str] = {
    "plan": "planner",
    "execute": "executor",
    "audit_syntax": "auditor_syntax",
    "audit_semantics": "auditor_semantics",
    "ship": "shipper",
    "verify": "verifier",
}

# Legacy → kanban status aliases. Accepted for one release so existing
# Coach prompts and external scripts that still call coord_update_task
# with 'done' or 'cancelled' don't break. The alias resolver is applied
# on tool input; the resolved value is what the state-machine validates
# against. Cancellation has a side-effect (cancelled_at is stamped),
# handled at the call site.
_LEGACY_STATUS_ALIASES: dict[str, str] = {
    "open": "plan",
    "claimed": "execute",
    "in_progress": "execute",
    "blocked": "execute",  # blocked is now an orthogonal flag, not a status
    "done": "archive",
    "cancelled": "archive",
}


def _normalize_status_alias(status: str) -> str:
    """Map a legacy status value to its kanban equivalent. No-op for
    already-kanban values."""
    return _LEGACY_STATUS_ALIASES.get(status, status)


def _valid_transition(old: str, new: str) -> bool:
    return new in VALID_TRANSITIONS.get(old, set())


# v2 §7.2.1 — completion-call wake. Each Player completion tool
# (coord_commit_push, coord_write_task_spec, coord_submit_audit_report,
# coord_role_complete) calls this after publishing its bus event so
# Coach is woken in real time with the Player's message_to_coach as
# context. The "no auto-wake" rule (v2 principle 2) constrains the
# kanban engine, not Player→Coach replies — see kanban-specs-v2 §7.2.1.
async def _wake_coach_for_completion(
    *,
    caller_id: str,
    task_id: str,
    role: str,
    message_to_coach: str | None,
    artifact_path: str | None,
    extra_hint: str | None = None,
) -> None:
    """Wake Coach with the Player's completion message as the reason.

    Skipped silently when:
      - caller is Coach (override path; would loop)
      - Coach is mid-turn (`maybe_wake_agent` no-ops)
      - cost cap is hit (`maybe_wake_agent` no-ops with a log line)

    The `## Recent events` rollup on Coach's next tick backstops every
    skipped case so no signal is lost.
    """
    if caller_id == "coach":
        return
    title: str | None = None
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT title FROM tasks WHERE id = ?", (task_id,),
            )
            row = await cur.fetchone()
            if row:
                title = (dict(row).get("title") or "").strip() or None
        finally:
            await c.close()
    except Exception:
        pass
    title_clause = f" ({title!r})" if title else ""
    msg = (message_to_coach or "").strip() or "(no message_to_coach)"
    artifact_clause = (
        f" Artifact: {artifact_path}." if artifact_path else ""
    )
    hint = f" {extra_hint}" if extra_hint else ""
    reason = (
        f"Player {caller_id} completed {role} on task "
        f"{task_id}{title_clause}.\n\n"
        f"message_to_coach: {msg}\n"
        f"{artifact_clause}{hint}"
    )
    try:
        from server.agents import maybe_wake_agent
        await maybe_wake_agent(
            "coach",
            reason,
            bypass_debounce=True,
            wake_source="kanban_completion",
        )
    except Exception:
        # Wake is best-effort; the event is already on the bus and in
        # project_events, so the next tick still surfaces it.
        pass


# v2 §22.1 push-time deviation tag matcher. Coach is taught (lifecycle
# policy block, agents.py) to prefix the `coord_approve_stage` `note`
# with `[deviation: <one-line reason>]` when noticing scope drift /
# off-spec work / unexpected changes in the artifact under review. This
# helper extracts the description so a `deviations_log{noticed_at='push'}`
# row can be inserted by the approve_stage code path.
#
# Matching:
#   1. Structured tag `[deviation: <reason>]` — preferred form. The
#      bracketed reason is captured verbatim (trimmed). Substring
#      match (case-insensitive) on `[deviation:` so the tag can appear
#      anywhere in the note, with any leading whitespace.
#   2. Fallback bare phrases (any of `deviation`, `off-spec`,
#      `scope drift`, `unexpected change`, case-insensitive). These
#      catch organic Coach prose so we don't lose the signal when the
#      tag is forgotten — at the cost of occasional false positives
#      (e.g. "no deviation here"). Acceptable per spec §22.1: the
#      validation criterion is qualitative across many tasks.
#
# Returns the extracted description, or None when no marker is present.
def _extract_deviation_description(note: str) -> str | None:
    if not note:
        return None
    text = note.strip()
    if not text:
        return None
    lower = text.lower()
    # Structured tag: find the first `[deviation:` (case-insensitive)
    # and the matching `]`. Capture the body between.
    tag_idx = lower.find("[deviation:")
    if tag_idx >= 0:
        body_start = tag_idx + len("[deviation:")
        # Find the closing `]` from body_start onwards.
        end = text.find("]", body_start)
        if end > body_start:
            body = text[body_start:end].strip()
            if body:
                return body
        # Tag opens but has no closing `]` — still treat as flagged so
        # the row gets inserted with a placeholder description.
        tail = text[body_start:].strip()
        return tail or "deviation flagged in note"
    # Bare-phrase fallback.
    bare_phrases = ("deviation", "off-spec", "scope drift", "unexpected change")
    if any(p in lower for p in bare_phrases):
        return "deviation flagged in note"
    return None


def _parse_boolish(raw: Any, *, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def _trajectory_stages_from_row(row: Any) -> list[str]:
    """Return the ordered list of stage names from a task row's
    `trajectory` JSON column. Defensive against malformed JSON / wrong
    shape — returns [] on any parse failure (callers treat this as
    'no trajectory configured', which routes execute → archive)."""
    raw = None
    try:
        if hasattr(row, "get"):
            raw = row.get("trajectory")
        else:
            raw = row["trajectory"]  # mapping-like via __getitem__
    except Exception:
        return []
    if not raw:
        return []
    try:
        traj = json.loads(raw)
    except Exception:
        return []
    if not isinstance(traj, list):
        return []
    out: list[str] = []
    for s in traj:
        if isinstance(s, dict):
            stage = str(s.get("stage", ""))
            if stage in TRAJECTORY_STAGES:
                out.append(stage)
    return out


def _trajectory_has_audit(stages: list[str]) -> bool:
    """True iff the trajectory configures at least one audit stage. Used
    by the executor wake prompt to decide whether to inject the self-audit
    reminder."""
    return "audit_syntax" in stages or "audit_semantics" in stages


def _next_stage_from_trajectory(
    stages: list[str], current: str
) -> str:
    """Walk the trajectory list. Returns the stage that follows `current`,
    or 'archive' if `current` is the last stage in the list (or absent)."""
    try:
        idx = stages.index(current)
    except ValueError:
        return "archive"
    if idx + 1 >= len(stages):
        return "archive"
    return stages[idx + 1]


_VALID_SLOT_RE = re.compile(r"^p(?:[1-9]|10)$")


def _coerce_player_slots(value: Any) -> list[str] | str:
    """Normalize a `to` value to `list[str]` of valid Player slots.

    Accepts:
      - a single slot string ('p3' or 'p1,p2,p3')
      - a list of slot strings
      - JSON-ish list
      - empty / None → []

    Returns either the validated list, or an error string explaining why
    the input is invalid. Empty strings inside the list are skipped.
    """
    if value is None or value == "":
        return []
    raw_list: list[Any]
    if isinstance(value, list):
        raw_list = list(value)
    else:
        text = str(value).strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except Exception:
                return "trajectory `to` JSON list could not be parsed"
            if not isinstance(parsed, list):
                return "trajectory `to` must be a slot string or list"
            raw_list = list(parsed)
        else:
            raw_list = [p for p in re.split(r"[,\s]+", text) if p]
    out: list[str] = []
    for item in raw_list:
        slot = str(item).strip().lower()
        if not slot:
            continue
        if not _VALID_SLOT_RE.match(slot):
            return f"invalid Player slot {slot!r} in trajectory `to`"
        if slot not in out:
            out.append(slot)
    return out


def _validate_trajectory(
    raw: Any,
    *,
    enforce_first_stage_assigned: bool = True,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Validate a trajectory list per Docs/kanban-specs.md §3.2.

    Accepts a Python list of `{stage, to, focus?}` dicts OR a
    JSON-encoded string of the same. On success returns the normalized
    list (each entry `{stage: str, to: list[str], focus?: str}`); on
    failure returns `(None, error)`.

    Rules:
      - non-empty list
      - each entry has `stage` in TRAJECTORY_STAGES (no `archive`)
      - no duplicate stages
      - stages appear in canonical order
      - `execute` is mandatory
      - `to` is a slot string or list of slot strings
      - `focus` (optional) must be a string when present; persisted on
        audit and verify stages; REQUIRED on every `audit_semantics` entry
        regardless of `to` (v2 §5.4 — semantic audits without a
        stated focus are noise; under v2 pools-are-FYI the empty-pool
        case is the normal case, so the focus must be authored at
        trajectory time).
      - The v1.3.13 `coach_review` plan-stage flag is removed in v2.
        Coach reviews every stage transition by default — there is no
        per-stage opt-in. The flag is silently dropped if present in
        legacy trajectories so old persisted rows still validate.
    """
    if raw is None:
        return None, "trajectory is required"
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, "trajectory is required"
        try:
            parsed = json.loads(text)
        except Exception:
            return None, "trajectory JSON could not be parsed"
        return _validate_trajectory(
            parsed,
            enforce_first_stage_assigned=enforce_first_stage_assigned,
        )
    if not isinstance(raw, list):
        return None, "trajectory must be a list of {stage, to, focus?} objects"
    if not raw:
        return None, "trajectory cannot be empty"

    normalized: list[dict[str, Any]] = []
    seen_stages: set[str] = set()
    last_idx = -1
    for entry in raw:
        if not isinstance(entry, dict):
            return None, "trajectory entries must be {stage, to, focus?} objects"
        stage = str(entry.get("stage", "")).strip().lower()
        if stage not in TRAJECTORY_STAGES:
            return None, (
                f"unknown stage {stage!r}; valid stages: "
                f"{', '.join(TRAJECTORY_STAGES)}"
            )
        if stage in seen_stages:
            return None, f"duplicate stage {stage!r} in trajectory"
        idx = TRAJECTORY_STAGE_INDEX[stage]
        if idx <= last_idx:
            return None, (
                f"stage {stage!r} appears out of canonical order; "
                f"trajectory must be ordered "
                f"{', '.join(TRAJECTORY_STAGES)}"
            )
        last_idx = idx
        seen_stages.add(stage)
        slots_or_err = _coerce_player_slots(entry.get("to"))
        if isinstance(slots_or_err, str):
            return None, slots_or_err
        out_entry: dict[str, Any] = {"stage": stage, "to": slots_or_err}
        focus_raw = entry.get("focus")
        if focus_raw is not None and not isinstance(focus_raw, str):
            return None, (
                f"trajectory entry {stage!r}: 'focus' must be a string"
            )
        focus_clean = (focus_raw or "").strip() if isinstance(focus_raw, str) else ""
        # v2 §5.4: every audit_semantics entry must carry a focus, even
        # when `to` is empty (pools are FYI only in v2; the focus is
        # authored upfront so the trajectory documents what will be
        # checked). Semantic audits without a focus are noise.
        if stage == "audit_semantics" and not focus_clean:
            return None, (
                "audit_semantics requires a 'focus' string — name what "
                "to check (e.g. focus='verify the math derivation "
                "matches the glossary'). Semantic audits without a "
                "focus are noise (see kanban-specs-v2.md §5.4)."
            )
        # Persist focus only on review-like stages (silently drop on
        # ordinary work stages so a Coach paste-mistake doesn't pollute
        # the row).
        if focus_clean and stage in (
            "audit_syntax", "audit_semantics", "verify",
        ):
            out_entry["focus"] = focus_clean
        # The v1.3.13 `coach_review` plan-stage flag is removed in v2
        # (§4.1). Silently drop if present in legacy / paste-mistake
        # trajectories so old persisted rows still validate.
        normalized.append(out_entry)

    if "execute" not in seen_stages:
        return None, "trajectory must include 'execute'"

    # v2.0.1 (2026-05-08): the first trajectory entry's `to` must name
    # exactly one Player AT CREATION TIME. The kanban is a log of work
    # Coach has fired at Players — tasks without an assignee aren't on
    # the kanban yet, they're pre-task reasoning. Pool/empty
    # first-stage `to` is rejected at the trajectory-validation
    # boundary so both MCP (`coord_create_task`) and HTTP
    # (`POST /api/tasks`) layers honor the rule. Subsequent entries
    # can still be pool/empty (FYI only; Coach picks each later
    # stage's assignee at coord_approve_stage time, which already
    # enforces single-named).
    #
    # `enforce_first_stage_assigned=False` is set by
    # `coord_set_task_trajectory` callers — mid-flight reroute happens
    # AFTER the task already has role rows planted; the original
    # first-stage assignment is in the role-row table, not the
    # trajectory's first entry. So a reroute can rewrite the first
    # entry to empty/pool without violating the create-time invariant.
    if enforce_first_stage_assigned:
        first_to = normalized[0].get("to") or []
        if len(first_to) != 1:
            return None, (
                "trajectory[0].to must name exactly one Player (e.g. "
                "['p3']) — coord_create_task fires a piece of work AT "
                "someone. Pool/empty first-stage `to` is rejected: an "
                "undispatched task isn't on the kanban yet, it's "
                "pre-task reasoning. If you haven't decided who, decide "
                "now (look at coord_get_player_settings, ## Player "
                "health, and ## Recent events) and put their slot in "
                "the trajectory. Subsequent stages can be FYI / empty; "
                "only the first must be named."
            )

    return normalized, None


def _role_matches_stage(role: str, stage: str) -> bool:
    if role == "executor":
        return stage in ("plan", "execute")
    return ROLE_STAGE.get(role) == stage


def _normalize_role_alias(role: str) -> str | None:
    r = (role or "").strip().lower()
    aliases = {
        "formal": "auditor_syntax",
        "syntax": "auditor_syntax",
        "syntactic": "auditor_syntax",
        "formal_review": "auditor_syntax",
        "semantic": "auditor_semantics",
        "semantics": "auditor_semantics",
        "semantic_review": "auditor_semantics",
        "verification": "verifier",
        "verify": "verifier",
    }
    r = aliases.get(r, r)
    return r if r in ROLE_NAMES else None


async def _check_kanban_role_gate(
    c: Any,
    project_id: str,
    task_id: str,
    old: str,
    new: str,
    *,
    was_cancellation: bool,
) -> str | None:
    """Role-completion gate enforcer for non-force stage transitions
    (Docs/kanban-specs.md §2.3).

    Returns an error string when the requested transition would skip an
    artifact / role-completion event that should drive it (commit_pushed,
    audit_report_submitted{pass|fail}, task_shipped). Returns None when
    the transition is allowed under the gate.

    Bypass paths (must NOT call this function):
    - `coord_advance_task_stage` (Coach-only override)
    - `POST /api/tasks/{id}/stage` with `force=true`

    Cancellation (`was_cancellation=True` AND target is `archive`) skips
    every gate by design — cancelling is always allowed at any stage.
    """
    if new == "archive" and was_cancellation:
        return None

    cur = await c.execute(
        "SELECT trajectory, spec_path FROM tasks "
        "WHERE id = ? AND project_id = ?",
        (task_id, project_id),
    )
    row = await cur.fetchone()
    if row is None:
        # Caller paths already SELECT the row; getting here means a
        # race or programmer error. Caller will surface the missing
        # task in its own error message.
        return None
    t = dict(row)
    spec_path = t.get("spec_path")
    stages = _trajectory_stages_from_row(t)
    expected_next = _next_stage_from_trajectory(stages, old)

    if old == "plan" and new == "execute":
        # Spec gate: trajectory has `plan` → spec required.
        if "plan" in stages and not spec_path:
            return (
                f"task {task_id} has no spec. The planner must call "
                f"coord_write_task_spec; Coach can override with "
                f"coord_write_task_spec(..., on_behalf_of=<slot>) when "
                f"the planner can't reach the tool. Then advance via "
                f"coord_approve_stage."
            )
        cur = await c.execute(
            "SELECT 1 FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'executor' "
            "AND owner IS NOT NULL AND superseded_by IS NULL "
            "ORDER BY assigned_at DESC LIMIT 1",
            (task_id,),
        )
        if not await cur.fetchone():
            return (
                f"task {task_id} has no executor assigned. Coach must "
                f"call coord_approve_stage(next_stage='execute', "
                f"assignee=<slot>, note=<brief>) to plant the role row "
                f"and advance the stage in one atomic step. Pools are "
                f"FYI only in v2; pick a named slot."
            )
        return None

    if old == "execute" and new in ("audit_syntax", "audit_semantics", "ship", "archive"):
        return (
            f"task {task_id} stage transitions are Coach-only in v2. "
            f"The executor signals completion via "
            f"coord_commit_push(task_id={task_id!r}) for code or "
            f"coord_role_complete(task_id={task_id!r}, ...) for "
            f"non-code; Coach then approves the next stage via "
            f"coord_approve_stage. Pass status='archive' here only "
            f"as the cancellation backstop (no user-facing summary; "
            f"prefer coord_archive_task for normal archives)."
        )

    if old == "audit_syntax" and new in ("audit_semantics", "ship", "archive"):
        if not await _has_passing_auditor(c, task_id, "auditor_syntax"):
            return (
                f"leaving formal review requires verdict='pass' from "
                f"the active formal reviewer (via "
                f"coord_submit_audit_report). Use coord_approve_stage "
                f"to advance once the verdict lands; if the verdict "
                f"is 'fail' and Coach wants to override the audit, "
                f"call coord_approve_stage with the explicit "
                f"next_stage."
            )
        if new != expected_next:
            return (
                f"task {task_id} trajectory expects {expected_next!r} "
                f"after formal-review pass. Update via "
                f"coord_set_task_trajectory, then coord_approve_stage."
            )
        return None

    if old == "audit_semantics" and new in ("ship", "archive"):
        if not await _has_passing_auditor(c, task_id, "auditor_semantics"):
            return (
                f"leaving semantic review requires verdict='pass' "
                f"from the active semantic reviewer (via "
                f"coord_submit_audit_report). Use coord_approve_stage "
                f"to advance once the verdict lands; Coach can override "
                f"a FAIL via an explicit coord_approve_stage call."
            )
        if new != expected_next:
            return (
                f"task {task_id} trajectory expects {expected_next!r} "
                f"after semantic-review pass. Update via "
                f"coord_set_task_trajectory, then coord_approve_stage."
            )
        return None

    if old == "ship" and new in ("verify", "archive"):
        if not await _has_completed_shipper(c, task_id):
            return (
                f"ship → {new} requires the shipper to call "
                f"coord_role_complete(task_id={task_id!r}, "
                f"message_to_coach=...) or coord_ship_to_dev first. "
                f"Coach then approves verify or archives with a "
                f"user-facing summary."
            )
        return None

    if old == "verify" and new == "archive":
        if not await _has_completed_verifier(c, task_id):
            return (
                f"verify → archive requires the verifier to call "
                f"coord_submit_verification_report(task_id={task_id!r}, "
                f"verdict='pass' or 'fail', body=...). Coach then "
                f"archives with a user-facing summary via "
                f"coord_archive_task(task_id={task_id!r}, summary=...)."
            )
        if new != expected_next:
            return (
                f"task {task_id} trajectory expects {expected_next!r} "
                f"after verify completion. Update via "
                f"coord_set_task_trajectory, then coord_approve_stage."
            )
        return None

    # audit_* → execute (manual revert) is allowed: it mirrors the
    # subscriber's auto-revert on a fail verdict, and Coach occasionally
    # needs to flag work as needing rework even when no auditor verdict
    # has landed yet.
    return None


async def _has_passing_auditor(c: Any, task_id: str, role: str) -> bool:
    """True if the active (un-superseded) auditor row for this task +
    role has `verdict='pass'`."""
    cur = await c.execute(
        "SELECT verdict FROM task_role_assignments "
        "WHERE task_id = ? AND role = ? AND superseded_by IS NULL "
        "ORDER BY assigned_at DESC LIMIT 1",
        (task_id, role),
    )
    row = await cur.fetchone()
    if not row:
        return False
    return (dict(row).get("verdict") or "").lower() == "pass"


async def _has_completed_shipper(c: Any, task_id: str) -> bool:
    """True if the active shipper assignment has `completed_at` set."""
    cur = await c.execute(
        "SELECT completed_at FROM task_role_assignments "
        "WHERE task_id = ? AND role = 'shipper' AND superseded_by IS NULL "
        "ORDER BY assigned_at DESC LIMIT 1",
        (task_id,),
    )
    row = await cur.fetchone()
    if not row:
        return False
    return bool(dict(row).get("completed_at"))


async def _has_completed_verifier(c: Any, task_id: str) -> bool:
    """True if the active verifier assignment has `completed_at` set."""
    cur = await c.execute(
        "SELECT completed_at FROM task_role_assignments "
        "WHERE task_id = ? AND role = 'verifier' AND superseded_by IS NULL "
        "ORDER BY assigned_at DESC LIMIT 1",
        (task_id,),
    )
    row = await cur.fetchone()
    if not row:
        return False
    return bool(dict(row).get("completed_at"))


def _ship_verify_manual_override_requested(note: str) -> bool:
    """Explicit escape hatch for manual post-ship evidence.

    Coach must make the override visible in the verifier's wake note; an
    unadorned ship→verify approval should never dispatch verification
    when no `task_shipped_to_dev` event exists.
    """
    lowered = note.lower()
    return (
        "[manual verify override]" in lowered
        or "[manual ship verify override]" in lowered
    )


async def _latest_ship_to_dev_evidence(
    c: Any, task_id: str,
) -> dict[str, Any] | None:
    """Return the latest task_shipped_to_dev evidence for verifier handoff."""
    cur = await c.execute(
        "SELECT ts, payload_json, payload_pointer FROM project_events "
        "WHERE task_id = ? AND type = 'task_shipped_to_dev' "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (task_id,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    raw = dict(row)
    try:
        payload = json.loads(raw.get("payload_json") or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    ship_sha = (
        payload.get("ship_sha")
        or payload.get("merge_sha")
        or raw.get("payload_pointer")
        or ""
    )
    return {
        "ts": raw.get("ts"),
        "pr_url": payload.get("pr_url") or "",
        "pr_number": payload.get("pr_number"),
        "ship_sha": ship_sha,
        "deploy_target": (
            payload.get("deploy_target")
            or payload.get("target")
            or "dev"
        ),
    }


def _format_ship_verify_context(
    evidence: dict[str, Any] | None,
    *,
    manual_override: bool = False,
) -> str:
    if not evidence:
        return (
            "Post-ship evidence: [manual verify override] no "
            "task_shipped_to_dev event was found; shipper role was "
            "complete and Coach explicitly requested manual verification."
        )
    pr_number = evidence.get("pr_number")
    pr_url = evidence.get("pr_url") or "unrecorded"
    ship_sha = evidence.get("ship_sha") or "unrecorded"
    deploy_target = evidence.get("deploy_target") or "dev"
    pr_part = (
        f"PR #{pr_number} {pr_url}" if pr_number else f"PR {pr_url}"
    )
    prefix = "Post-ship evidence"
    if manual_override:
        prefix += " [manual verify override]"
    return (
        f"{prefix}: deploy_target={deploy_target}; "
        f"ship_sha={ship_sha}; {pr_part}."
    )


async def _ship_verify_context_or_error(
    c: Any, task_id: str, note: str,
) -> tuple[str | None, str | None]:
    """Shared ship→verify gate for MCP + human/API approval surfaces."""
    evidence = await _latest_ship_to_dev_evidence(c, task_id)
    shipper_complete = await _has_completed_shipper(c, task_id)
    manual_override = _ship_verify_manual_override_requested(note)
    if not evidence and not shipper_complete:
        return None, (
            f"ship → verify requires post-ship evidence before "
            f"verifier work is dispatched. No latest "
            f"task_shipped_to_dev event was found for {task_id}, "
            f"and the active shipper role is not complete. Have the "
            f"shipper call coord_ship_to_dev(task_id={task_id!r}) "
            f"first (preferred), or complete the shipper role and "
            f"retry with note starting [manual verify override] only "
            f"for a documented manual ship."
        )
    if not evidence and not manual_override:
        return None, (
            f"ship → verify found a completed shipper role but no "
            f"task_shipped_to_dev event for {task_id}. To avoid silent "
            f"verification before post-ship evidence exists, retry only "
            f"after coord_ship_to_dev emits ship evidence, or use an "
            f"explicit manual path: start note with "
            f"[manual verify override] and include the PR URL/number, "
            f"ship SHA, and deploy target for the verifier."
        )
    return _format_ship_verify_context(
        evidence, manual_override=manual_override and bool(evidence),
    ), None


# Auditor / shipper / planner roles that Coach can assign. Mirror of
# the task_role_assignments.role CHECK constraint.
ROLE_NAMES: frozenset[str] = frozenset({
    "planner", "executor", "auditor_syntax", "auditor_semantics",
    "shipper", "verifier",
})


def _resolve_audit_role_kind(kind: str) -> str | None:
    """Convert a Coach-facing kind ('formal' / 'semantic') to the
    underlying role-assignment row's role value."""
    if kind in ("syntax", "formal", "syntactic", "mechanical"):
        return "auditor_syntax"
    if kind in ("semantics", "semantic"):
        return "auditor_semantics"
    return None


def _audit_kind_from_role(role: str) -> str | None:
    """Inverse of `_resolve_audit_role_kind`."""
    if role == "auditor_syntax":
        return "syntax"
    if role == "auditor_semantics":
        return "semantics"
    return None


def _ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"ERROR: {text}"}],
        "is_error": True,
    }


async def _is_locked(agent_id: str) -> bool:
    """Return True if the agent's `locked` flag is set. Missing row /
    DB error returns False — lock is a safety restriction, not a
    correctness invariant, so failing open is preferable to blocking
    the whole tool on a hiccup."""
    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT locked FROM agents WHERE id = ?", (agent_id,)
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        return False
    if not row:
        return False
    return bool(dict(row).get("locked"))


async def _set_agent_role_tools(c: Any, agent_id: str, role: str | None) -> None:
    from server.role_tool_allowlists import tools_json_for_role

    await c.execute(
        "UPDATE agents SET allowed_tools = ? WHERE id = ?",
        (tools_json_for_role(role), agent_id),
    )
    # Evict any cached Codex client so the next spawn rebuilds with the
    # updated tool list. Codex clients capture allowed_tools at subprocess
    # start (via --allowed-tools CLI flag to coord_mcp) and never re-read
    # the DB — without eviction, a role change is invisible to the running
    # Codex session even after the DB is updated.
    # Schedule as a fire-and-forget task so the eviction runs after the
    # caller's DB transaction commits (we're still inside a transaction here).
    try:
        import asyncio
        from server.runtimes.codex import evict_client

        asyncio.ensure_future(evict_client(agent_id))
    except Exception:
        pass  # Never block a role assignment on eviction failure


async def _reset_agent_idle_tools(c: Any, agent_id: str) -> None:
    await _set_agent_role_tools(c, agent_id, "idle")


async def _set_agent_current_task_if_free_or_stale(
    c: Any,
    agent_id: str,
    task_id: str,
) -> None:
    """Point a slot at task_id unless it is already on live work.

    Older builds sometimes left current_task_id pointing at an archived task.
    A later execute assignment guarded with `IS NULL` then failed to surface
    the real active executor task, and the slot kept its idle tool allowlist.
    Treat missing/archived task pointers as stale and replace them.
    """
    await c.execute(
        "UPDATE agents SET current_task_id = ? "
        "WHERE id = ? AND ("
        "  current_task_id IS NULL "
        "  OR current_task_id = ? "
        "  OR NOT EXISTS ("
        "    SELECT 1 FROM tasks t "
        "    WHERE t.id = agents.current_task_id "
        "    AND t.status != 'archive'"
        "  )"
        ")",
        (task_id, agent_id, task_id),
    )


def build_coord_server(caller_id: str, *, include_proxy_metadata: bool = False) -> Any:
    """Build an in-process MCP server whose tools know which agent is calling.

    Each SDK query gets its own server so hierarchy enforcement (Coach can
    give orders, Players cannot) operates without the LLM needing to pass
    its own identity as a param.

    By default the returned server is safe to hand to ClaudeAgentOptions.
    The coord loopback proxy can opt into `_handlers` / `_tool_names`
    metadata, which contains Python callables and must never be passed to
    the Claude SDK because its options path JSON-serializes MCP config.
    """

    caller_is_coach = caller_id == "coach"

    @tool(
        "coord_list_tasks",
        (
            "List tasks on the team board. Optional filters:\n"
            "- status: kanban stage — one of 'plan', 'execute', "
            "'audit_syntax', 'audit_semantics', 'ship', 'verify', "
            "'archive'. "
            "Legacy values (open/claimed/in_progress/blocked/done/cancelled) "
            "are translated to their kanban equivalent for back-compat.\n"
            "- owner: agent id ('coach', 'p1'..'p10'), or 'null' for unassigned\n"
            "- include_backlog: optional boolean; no-arg board views include "
            "pending Backlog by default. Pass false to suppress it.\n"
            "By default, returns the active board range (Backlog, then plan "
            "through verify) and excludes archived history. "
            "Pass status='archive' for archived tasks. Each task row shows "
            "kind, stage, trajectory ([P,E,AY,AE,S] tokens), blocked flag, "
            "stage_role, owner, priority, and title."
        ),
        {"status": str, "owner": str, "include_backlog": str},
    )
    async def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
        status = (args.get("status") or "").strip() or None
        owner_arg = args.get("owner")
        owner = owner_arg.strip() if isinstance(owner_arg, str) else None
        status_l = status.lower() if status else None
        # Backlog is the pre-plan holding area, so it belongs in the
        # whole-board and pending-only views. A concrete kanban stage
        # filter must stay task-only even if include_backlog is truthy.
        include_backlog_raw = args.get("include_backlog")
        include_backlog_requested = True
        if include_backlog_raw is not None:
            include_backlog_requested = (
                str(include_backlog_raw).strip().lower()
                not in {"0", "false", "no", "off"}
            )
        include_backlog = (
            include_backlog_requested
            and owner is None
            and (status_l is None or status_l == "pending")
        )
        query_tasks = status_l != "pending"

        where_parts: list[str] = []
        params: list[Any] = []
        if status and query_tasks:
            # Translate legacy aliases for back-compat. The DB CHECK only
            # accepts kanban values now; passing 'in_progress' would
            # quietly return nothing without this.
            normalized = _normalize_status_alias(status)
            where_parts.append("status = ?")
            params.append(normalized)
        elif not status:
            where_parts.append("status != 'archive'")
        if owner is not None and owner != "":
            if owner.lower() in ("null", "none", "unassigned"):
                # Mirror the UI's "unassigned" classifier (kanban v2): a task
                # is unassigned when the current stage's role has no active
                # task_role_assignments row (completed_at IS NULL AND
                # superseded_by IS NULL AND owner IS NOT NULL).  The legacy
                # tasks.owner column alone is unreliable after v2 role-state
                # migration — tasks can have tasks.owner set from an earlier
                # stage while the current stage has no active assignment.
                where_parts.append(
                    "NOT EXISTS ("
                    "SELECT 1 FROM task_role_assignments tra "
                    "WHERE tra.task_id = tasks.id "
                    "AND tra.role = CASE tasks.status "
                    "  WHEN 'plan'             THEN 'planner' "
                    "  WHEN 'execute'          THEN 'executor' "
                    "  WHEN 'audit_syntax'     THEN 'auditor_syntax' "
                    "  WHEN 'audit_semantics'  THEN 'auditor_semantics' "
                    "  WHEN 'ship'             THEN 'shipper' "
                    "  WHEN 'verify'           THEN 'verifier' "
                    "  ELSE NULL END "
                    "AND tra.completed_at IS NULL "
                    "AND tra.superseded_by IS NULL "
                    "AND tra.owner IS NOT NULL)"
                )
            else:
                where_parts.append("owner = ?")
                params.append(owner)
        project_id = await resolve_active_project()
        where_parts.insert(0, "project_id = ?")
        params.insert(0, project_id)
        clause = " WHERE " + " AND ".join(where_parts)

        # SQL fragment that maps a task's current stage to its kanban v2 role.
        _STAGE_TO_ROLE_SQL = (
            "CASE t.status "
            "  WHEN 'plan'            THEN 'planner' "
            "  WHEN 'execute'         THEN 'executor' "
            "  WHEN 'audit_syntax'    THEN 'auditor_syntax' "
            "  WHEN 'audit_semantics' THEN 'auditor_semantics' "
            "  WHEN 'ship'            THEN 'shipper' "
            "  WHEN 'verify'          THEN 'verifier' "
            "  ELSE NULL END"
        )
        c = await configured_conn()
        try:
            if query_tasks:
                cur = await c.execute(
                    f"SELECT t.id, t.title, t.status, t.owner, t.created_by, "
                    f"t.parent_id, t.priority, t.trajectory, t.blocked, "
                    f"t.blocked_reason, t.created_at, "
                    # active_owner: owner of the live (non-completed) role row
                    f"(SELECT tra.owner FROM task_role_assignments tra "
                    f" WHERE tra.task_id = t.id "
                    f" AND tra.role = {_STAGE_TO_ROLE_SQL} "
                    f" AND tra.completed_at IS NULL "
                    f" AND tra.superseded_by IS NULL "
                    f" AND tra.owner IS NOT NULL "
                    f" LIMIT 1) AS active_owner, "
                    # role_done_owner: owner of the completed role row (awaiting
                    # Coach advance), NULL if not yet done
                    f"(SELECT tra.owner FROM task_role_assignments tra "
                    f" WHERE tra.task_id = t.id "
                    f" AND tra.role = {_STAGE_TO_ROLE_SQL} "
                    f" AND tra.completed_at IS NOT NULL "
                    f" AND tra.superseded_by IS NULL "
                    f" LIMIT 1) AS role_done_owner, "
                    # role_done_verdict: verdict of the completed role row (pass/fail),
                    # NULL for non-audit stages or when not yet done
                    f"(SELECT tra.verdict FROM task_role_assignments tra "
                    f" WHERE tra.task_id = t.id "
                    f" AND tra.role = {_STAGE_TO_ROLE_SQL} "
                    f" AND tra.completed_at IS NOT NULL "
                    f" AND tra.superseded_by IS NULL "
                    f" LIMIT 1) AS role_done_verdict "
                    f"FROM tasks t{clause} "
                    f"ORDER BY t.created_at DESC LIMIT 100",
                    params,
                )
                rows = await cur.fetchall()
            else:
                rows = []
            if include_backlog:
                cur = await c.execute(
                    "SELECT id, title, proposed_by, proposed_at, status, priority "
                    "FROM backlog_tasks WHERE status = 'pending' "
                    "ORDER BY proposed_at DESC LIMIT 100"
                )
                backlog_rows = await cur.fetchall()
            else:
                backlog_rows = []
        finally:
            await c.close()

        if not rows and not backlog_rows:
            return _ok("(no tasks match)")

        # Map status → short role label used in stage_role display.
        _STATUS_TO_ROLE_LABEL = {
            "plan": "planner",
            "execute": "executor",
            "audit_syntax": "auditor",
            "audit_semantics": "sem-auditor",
            "ship": "shipper",
            "verify": "verifier",
        }

        lines = []
        now = datetime.now(timezone.utc)
        for r in backlog_rows:
            d = dict(r)
            age = "?"
            try:
                ts_str = d.get("proposed_at") or ""
                if ts_str:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    secs = int((now - ts).total_seconds())
                    if secs < 60:
                        age = f"{secs}s"
                    elif secs < 3600:
                        age = f"{secs // 60}m"
                    elif secs < 86400:
                        age = f"{secs // 3600}h"
                    else:
                        age = f"{secs // 86400}d"
            except Exception:
                pass
            lines.append(
                f"#{d['id']}  kind=backlog  [{d['status']}]  "
                f"pri={d.get('priority') or 'normal'}  {d['title']}  "
                f"by {d['proposed_by']}, {age} ago"
            )
        for r in rows:
            d = dict(r)
            parent = f" sub-of:{d['parent_id']}" if d["parent_id"] else ""
            stages = _trajectory_stages_from_row(d)
            traj = " trajectory=[" + ",".join(stages) + "]" if stages else ""
            blocked = ""
            if d.get("blocked"):
                reason = d.get("blocked_reason") or ""
                blocked = (
                    f" BLOCKED({reason})" if reason else " BLOCKED"
                )
            # Prefer active role-assignment owner (kanban v2 source of truth)
            # over tasks.owner; fall back for archive/non-standard stages.
            display_owner = d.get("active_owner") or d["owner"] or "-"
            # stage_role field: shows role name + state for the current stage.
            #   executor:p3             — active executor is p3
            #   executor:done           — non-audit role completed, awaiting Coach
            #   complete:p5:pass        — audit stage complete with pass verdict
            #   complete:p5:fail        — audit stage complete with fail verdict
            #   executor:-              — no active/completed assignment (unassigned)
            #   (omitted for archive/null-role stages)
            _AUDIT_STATUSES = {"audit_syntax", "audit_semantics"}
            role_label = _STATUS_TO_ROLE_LABEL.get(d["status"])
            if role_label:
                if d.get("active_owner"):
                    stage_role = f" stage_role={role_label}:{d['active_owner']}"
                elif d.get("role_done_owner") is not None:
                    if d["status"] in _AUDIT_STATUSES and d.get("role_done_verdict"):
                        stage_role = (
                            f" stage_role=complete:{d['role_done_owner']}:"
                            f"{d['role_done_verdict']}"
                        )
                    elif d["status"] == "verify" and d.get("role_done_verdict"):
                        stage_role = (
                            f" stage_role=verified:{d['role_done_owner']}:"
                            f"{d['role_done_verdict']}"
                        )
                    else:
                        stage_role = f" stage_role={role_label}:done"
                else:
                    stage_role = f" stage_role={role_label}:-"
            else:
                stage_role = ""
            lines.append(
                f"{d['id']}  kind=task  [{d['status']}]{traj}{blocked}{stage_role}  "
                f"owner={display_owner}  pri={d['priority']}  "
                f"{d['title']}{parent}"
            )
        return _ok("\n".join(lines))

    @tool(
        "coord_create_task",
        (
            "Create a task on the team board.\n"
            "Hierarchy rule: only Coach can create top-level tasks. "
            "Players can only create SUBTASKS of tasks they own — pass the "
            "parent_id explicitly (must match a task you own), or leave it "
            "blank to auto-nest under your current task.\n"
            "\n"
            "**Coach top-level tasks land in the Backlog first (FIFO "
            "discipline).** When Coach calls this tool WITHOUT a parent_id, "
            "the task is inserted into the Backlog as a `pending` entry "
            "(same table as coord_propose_task). No kanban row is created "
            "yet, no Player is woken. Coach must then call "
            "coord_triage_backlog(id, action='promote', trajectory=[...]) "
            "to promote it to the kanban and fire it at a Player. This "
            "enforces FIFO priority ordering: items are triaged in the "
            "order they arrived, not in the order Coach happened to type "
            "them (LIFO). Use the `priority` param to flag urgency at "
            "creation time — the Backlog column shows it. "
            "Player SUBTASKS (with parent_id) are unaffected — they still "
            "plant directly on the kanban under their parent.\n"
            "\n"
            "Params:\n"
            "- title: short summary (required)\n"
            "- description: longer explanation (optional)\n"
            "- parent_id: parent task id (optional; Players: required unless you have a current task)\n"
            "- priority: 'low', 'normal', 'high', 'urgent' (default 'normal'). "
            "Stored on the Backlog entry for Coach top-level tasks; stored "
            "on the task row for Player subtasks.\n"
            "- workflow: code | research | writing | marketing | ops | generic (default generic). "
            "Shapes prompt wording; does not drive routing — the trajectory does.\n"
            "- tracking_reason: optional informational tag.\n"
            "- trajectory: REQUIRED for Coach top-level tasks. Stored on the "
            "Backlog entry so coord_triage_backlog promote can read it. "
            "Ordered list of {stage, to, focus?} objects. `stage` ∈ "
            "{plan, execute, audit_syntax, audit_semantics, ship, verify}; canonical "
            "order; execute is mandatory. **trajectory[0].to MUST name "
            "exactly one Player** (single-element list like ['p3']). "
            "Subsequent stages' `to` may be a single name, list, or empty "
            "— FYI only; Coach picks each later stage's assignee at "
            "coord_approve_stage time. `focus` is required on every "
            "audit_semantics entry. Players inherit the parent's trajectory "
            "when subtasking.\n"
            "- note: optional brief — stored with the Backlog entry for "
            "  Coach top-level tasks; becomes the first-stage assignee's "
            "  wake prompt at promote time.\n"
            "- success_criteria: optional 1-3 line statement of what "
            "  'done' looks like. Stored on the Backlog entry; promoted "
            "  to the task row at triage time."
        ),
        # Raw JSON schema: dict-shorthand marks ALL keys required; only
        # title is truly required here. description/parent_id/priority/
        # workflow/tracking_reason/trajectory/note/success_criteria are
        # all optional per the tool description above.
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "parent_id": {"type": "string"},
                "priority": {"type": "string"},
                "workflow": {"type": "string"},
                "tracking_reason": {"type": "string"},
                "trajectory": {},
                "note": {"type": "string"},
                "success_criteria": {"type": "string"},
            },
            "required": ["title"],
        },
    )
    async def create_task(args: dict[str, Any]) -> dict[str, Any]:
        title = (args.get("title") or "").strip()
        if not title:
            return _err("title is required")
        description = args.get("description") or ""
        coach_note = (args.get("note") or "").strip() or None
        success_criteria = (args.get("success_criteria") or "").strip()
        parent_id_arg = args.get("parent_id")
        parent_id = parent_id_arg.strip() if isinstance(parent_id_arg, str) and parent_id_arg.strip() else None
        priority = (args.get("priority") or "normal").strip().lower()
        if priority not in ("low", "normal", "high", "urgent"):
            return _err(
                f"invalid priority '{priority}' "
                "(must be low, normal, high, or urgent)"
            )

        workflow = (args.get("workflow") or "generic").strip().lower()
        if workflow not in WORKFLOW_TYPES:
            return _err(
                f"invalid workflow '{workflow}' "
                f"(must be one of {sorted(WORKFLOW_TYPES)})"
            )
        tracking_reason_raw = args.get("tracking_reason")
        tracking_reason = (
            str(tracking_reason_raw).strip()
            if tracking_reason_raw else ""
        )

        trajectory_raw = args.get("trajectory")
        trajectory: list[dict[str, Any]] | None = None
        if trajectory_raw not in (None, "", []):
            trajectory, traj_err = _validate_trajectory(trajectory_raw)
            if traj_err:
                return _err(f"invalid trajectory: {traj_err}")
        if trajectory is None and caller_is_coach and parent_id is None:
            return _err(
                "trajectory is required for Coach top-level tasks. Pass an "
                "ordered list of {stage, to} objects, e.g. "
                "[{stage:'execute',to:['p2','p3']}] for quick self-audit "
                "work, or "
                "[{stage:'plan',to:'p5'},{stage:'execute',to:'p2'},"
                "{stage:'audit_syntax',to:'p4'},{stage:'ship',to:'p2'}] "
                "for code-with-formal-review."
            )

        project_id = await resolve_active_project()

        # ----------------------------------------------------------------
        # Coach top-level task → Backlog first (FIFO discipline).
        # Player subtasks (parent_id set) always go directly to kanban.
        # ----------------------------------------------------------------
        if caller_is_coach and parent_id is None:
            # Store trajectory + note + success_criteria on the backlog
            # entry so coord_triage_backlog promote can read them later.
            trajectory_json = json.dumps(
                trajectory, separators=(",", ":")
            ) if trajectory else None
            now_iso = _now_iso()
            c = await configured_conn()
            try:
                cur = await c.execute(
                    "INSERT INTO backlog_tasks "
                    "(title, proposed_by, proposed_at, priority, description, "
                    "trajectory_json, note, success_criteria) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (title, caller_id, now_iso, priority,
                     description or None,
                     trajectory_json,
                     coach_note or None,
                     success_criteria or None),
                )
                backlog_id = cur.lastrowid
                await c.commit()
            finally:
                await c.close()

            await bus.publish({
                "ts": now_iso,
                "agent_id": caller_id,
                "type": "backlog_task_proposed",
                "id": backlog_id,
                "title": title,
                "proposed_by": caller_id,
                "priority": priority,
            })
            return _ok(
                f"Backlog entry #{backlog_id} created: \"{title}\" "
                f"(priority={priority}). "
                "Task is NOT yet on the kanban — call "
                f"coord_triage_backlog(id={backlog_id}, "
                "action='promote', trajectory=[...]) when you're ready "
                "to fire it at a Player. This enforces FIFO ordering: "
                "triage from oldest to newest, not by creation recency."
            )

        c = await configured_conn()
        try:
            if not caller_is_coach:
                # Player: enforce hierarchy
                cur = await c.execute(
                    "SELECT current_task_id FROM agents WHERE id = ?",
                    (caller_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return _err(f"caller '{caller_id}' not in agents table")
                current_task = dict(row)["current_task_id"]
                if parent_id is None:
                    if current_task is None:
                        return _err(
                            "Players can only create subtasks of a task they own. "
                            "You have no active task — ask Coach to assign one, or "
                            "pass parent_id explicitly to a task you own."
                        )
                    parent_id = current_task
                else:
                    cur = await c.execute(
                        "SELECT owner FROM tasks WHERE id = ? AND project_id = ?",
                        (parent_id, project_id),
                    )
                    prow = await cur.fetchone()
                    if prow is None:
                        return _err(f"parent_id '{parent_id}' not found")
                    parent_owner = dict(prow)["owner"]
                    if parent_owner != caller_id:
                        return _err(
                            f"Players can only subtask their own tasks. "
                            f"Task {parent_id} is owned by "
                            f"{parent_owner or 'nobody'}. To suggest new "
                            f"top-level work, message Coach."
                        )

            # Subtask trajectory inherits from parent unless explicitly
            # provided. The inherited trajectory keeps the parent's stage
            # shape but resets per-stage assignees to empty pools (subtask
            # ownership is independent).
            if parent_id and trajectory is None:
                cur = await c.execute(
                    "SELECT trajectory, workflow FROM tasks "
                    "WHERE id = ? AND project_id = ?",
                    (parent_id, project_id),
                )
                prow = await cur.fetchone()
                if prow:
                    pd = dict(prow)
                    workflow = pd.get("workflow") or workflow
                    tracking_reason = tracking_reason or "subtask"
                    parent_stages = _trajectory_stages_from_row(pd)
                    if parent_stages:
                        trajectory = [
                            {"stage": s, "to": []} for s in parent_stages
                        ]

            # Default trajectory if still unset (e.g. Player making a
            # subtask of a task without a stored trajectory).
            if trajectory is None:
                trajectory = [{"stage": "execute", "to": []}]

            trajectory_json = json.dumps(trajectory, separators=(",", ":"))

            # v0.3 audit fix: initial status = first stage in the
            # trajectory. An execute-only trajectory must NOT start in
            # `plan` (the executor row is already plantable, but the
            # task would be stuck behind the spec gate / can't be
            # claimed via coord_accept_role).
            initial_status = trajectory[0]["stage"]

            task_id = _new_task_id()
            now_iso = _now_iso()
            # Hard-assign owner on tasks.owner when the first stage has
            # exactly one slot — the column is the executor's identity
            # for downstream gates (idle poller, current_task_id wake).
            first_stage_to = trajectory[0].get("to") or []
            if isinstance(first_stage_to, str):
                first_stage_to = [first_stage_to] if first_stage_to else []
            initial_owner = (
                first_stage_to[0] if len(first_stage_to) == 1 else None
            )
            await c.execute(
                "INSERT INTO tasks (id, project_id, title, description, parent_id, "
                "priority, workflow, tracking_reason, trajectory, status, owner, "
                "last_stage_change_at, created_by, success_criteria) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, project_id, title, description, parent_id,
                 priority, workflow, tracking_reason or None,
                 trajectory_json, initial_status, initial_owner,
                 now_iso, caller_id, success_criteria),
            )
            # v2 (Docs/kanban-specs-v2.md §7.1): first-stage-only
            # planting. The role row is created ONLY when the
            # trajectory's first entry has a single named slot in
            # `to` (semantically: "Coach picked via the trajectory
            # itself"). Pool / empty `to` doesn't auto-plant —
            # subsequent stages NEVER auto-plant. Coach plants role
            # rows for later stages by calling coord_approve_stage.
            role_for_stage = {
                "plan": "planner",
                "execute": "executor",
                "audit_syntax": "auditor_syntax",
                "audit_semantics": "auditor_semantics",
                "ship": "shipper",
                "verify": "verifier",
            }
            first_entry = trajectory[0]
            first_to: list[str] = first_entry.get("to") or []
            first_role: str | None = None
            planted_first_stage = False
            if len(first_to) == 1:
                first_role = role_for_stage[first_entry["stage"]]
                first_focus: str | None = first_entry.get("focus") or None
                eligible_json = json.dumps(first_to, separators=(",", ":"))
                await c.execute(
                    "INSERT INTO task_role_assignments "
                    "(task_id, role, eligible_owners, owner, "
                    "assigned_at, claimed_at, focus) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (task_id, first_role, eligible_json, first_to[0],
                     now_iso, now_iso, first_focus),
                )
                planted_first_stage = True
            # When the first stage is `execute` with a single hard-
            # assigned owner, propagate to agents.current_task_id so
            # `coord_my_assignments` Bucket 1 surfaces the task. Without
            # this, a task created with trajectory=[{stage:execute,
            # to:p8}] has tasks.owner=p8 but agents.current_task_id is
            # still NULL — the executor can't find their own task. The
            # mid-flight `coord_assign_task` path already does this; the
            # create-time path was missing it. Idempotent: guarded with
            # a live-task check so we don't stomp an existing assignment
            # if the same Player already owns something else, but we do
            # replace archived/missing stale pointers left by older builds.
            if initial_status == "execute" and initial_owner:
                await _set_agent_current_task_if_free_or_stale(
                    c, initial_owner, task_id,
                )
            if planted_first_stage and initial_owner and first_role:
                await _set_agent_role_tools(c, initial_owner, first_role)
            await c.commit()
        finally:
            await c.close()

        ts = _now_iso()
        await bus.publish(
            {
                "ts": ts,
                "agent_id": caller_id,
                "type": "task_created",
                "task_id": task_id,
                "title": title,
                "parent_id": parent_id,
                "priority": priority,
                "workflow": workflow,
                "tracking_reason": tracking_reason or None,
                "trajectory": trajectory,
            }
        )
        # v2 (Docs/kanban-specs-v2.md §7.1): emit task_stage_changed +
        # task_role_assigned ONLY when the first-stage role row was
        # actually planted (single-name `to`). Pool / empty first-stage
        # entries don't produce these events — Coach drives the first
        # transition via coord_approve_stage when ready.
        if planted_first_stage and initial_owner:
            await bus.publish(
                {
                    "ts": ts,
                    "agent_id": "system",
                    "type": "task_stage_changed",
                    "task_id": task_id,
                    "from": None,
                    "to": initial_status,
                    "reason": "task_created",
                    "owner": initial_owner,
                    "assignee": initial_owner,
                }
            )
            first_role = role_for_stage[trajectory[0]["stage"]]
            await bus.publish(
                {
                    "ts": ts,
                    "agent_id": caller_id,
                    "type": "task_role_assigned",
                    "task_id": task_id,
                    "role": first_role,
                    "owner": initial_owner,
                    "to": initial_owner,
                }
            )
        head = (
            f"Created task {task_id}"
            + (f" (subtask of {parent_id})" if parent_id else " (top-level)")
            + f", priority={priority}, "
            + f"workflow={workflow}, "
            + f"trajectory=[{', '.join(s['stage'] for s in trajectory)}]"
        )
        if planted_first_stage and initial_owner:
            tail = (
                f". Planted {role_for_stage[trajectory[0]['stage']]} "
                f"role → {initial_owner} (first-stage single-name auto-"
                f"plant per v2 §7.1). The harness wakes them with the "
                f"role context. Subsequent stages' `to` lists are FYI "
                f"only — you advance each stage explicitly via "
                f"coord_approve_stage(task_id, next_stage, assignee, "
                f"note?) when the previous stage's deliverable lands."
            )
            # Wake the planted first-stage assignee directly. v1 used
            # the subscriber's _on_stage_changed handler; v2 has no
            # auto-advance so the wake fires from here.
            from server.agents import maybe_wake_agent
            wake_body = coach_note or (
                f"Coach created task {task_id} ({title!r}) and assigned "
                f"you as {role_for_stage[trajectory[0]['stage']]} for "
                f"the {initial_status} stage."
            )
            wake_body = _with_player_reminder(wake_body)
            try:
                await maybe_wake_agent(
                    initial_owner, wake_body,
                    bypass_debounce=True,
                    wake_source="kanban_create",
                )
            except Exception:
                pass
        else:
            tail = (
                f". No role row planted (first-stage `to` is "
                f"empty or a pool — pools are FYI only in v2). Drive "
                f"the first transition via "
                f"coord_approve_stage(task_id={task_id!r}, "
                f"next_stage={initial_status!r}, assignee=<slot>, "
                f"note=<brief>) when you're ready. Until then the task "
                f"sits silently."
            )
        return _ok(head + tail)

    @tool(
        "coord_update_task",
        (
            "DEPRECATED for stage transitions in kanban v2. The "
            "single transition tool is coord_approve_stage(task_id, "
            "next_stage, assignee, note?); this tool is tolerated only "
            "as the fast-cancellation backstop (status='archive' "
            "without a user-facing summary). Prefer "
            "coord_archive_task(task_id, summary) so the user sees a "
            "deliberate wrap-up.\n"
            "\n"
            "Valid transitions still gated by the v2 state machine:\n"
            "  plan → execute, archive\n"
            "  execute → audit_syntax, audit_semantics, ship, archive\n"
            "  audit_syntax → audit_semantics, ship, archive, execute\n"
            "  audit_semantics → ship, archive, execute\n"
            "  ship → verify, archive\n"
            "  verify → archive, execute, ship\n"
            "  archive: terminal\n"
            "\n"
            "Most call sites should use coord_approve_stage instead. "
            "v2 does NOT auto-advance on commit/audit/ship; the role-"
            "completion gate here will reject manual transitions Coach "
            "should have driven via coord_approve_stage.\n"
            "\n"
            "Legacy aliases accepted for one release: 'open'→'plan', "
            "'claimed'/'in_progress'/'blocked'→'execute', 'done'→'archive', "
            "'cancelled'→'archive' (sets cancelled_at).\n"
            "\n"
            "Only the current owner can update the task; Coach can also "
            "cancel any task. When a task moves to archive, the owner's "
            "current_task_id is cleared. Optional 'note' is logged."
        ),
        {"task_id": str, "status": str, "note": str},
    )
    async def update_task(args: dict[str, Any]) -> dict[str, Any]:
        task_id = (args.get("task_id") or "").strip()
        raw_status = (args.get("status") or "").strip().lower()
        note = args.get("note") or ""
        if not task_id:
            return _err("task_id is required")
        # Cancellation has a side-effect: the original input is preserved
        # so we can stamp cancelled_at iff the caller asked for "cancelled"
        # specifically, vs. a clean "archive" move (delivery).
        was_cancellation = raw_status == "cancelled"
        new_status = _normalize_status_alias(raw_status)
        if new_status not in ALL_KANBAN_STAGES:
            return _err(
                f"invalid status '{raw_status}' (must be one of "
                f"{sorted(ALL_KANBAN_STAGES)} or a legacy alias)"
            )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT owner, status, created_by, title FROM tasks "
                "WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            d = dict(row)
            current_owner: str | None = d["owner"]
            old_status: str = d["status"]
            created_by: str = d.get("created_by") or ""
            task_title: str = d.get("title") or ""

            # Permission check.
            is_archive_move = new_status == "archive"
            if current_owner is None:
                # No owner yet — only Coach can touch.
                if not caller_is_coach:
                    return _err(
                        f"task {task_id} has no owner; only Coach can "
                        f"change a plan-stage task's status."
                    )
            elif current_owner != caller_id:
                # Owned by someone else. Only Coach can cancel/archive.
                if not (caller_is_coach and is_archive_move):
                    return _err(
                        f"only the task's owner ({current_owner}) can "
                        f"update it. Coach can additionally archive any task."
                    )

            if not _valid_transition(old_status, new_status):
                return _err(
                    f"invalid transition: {old_status} → {new_status}"
                )

            # Role-completion gate (Docs/kanban-specs.md §2.3). Rejects
            # manual transitions that should be event-driven (commit /
            # audit / ship). Coach uses coord_advance_task_stage to
            # force; cancellation paths skip the gate.
            gate_err = await _check_kanban_role_gate(
                c,
                project_id,
                task_id,
                old_status,
                new_status,
                was_cancellation=was_cancellation,
            )
            if gate_err is not None:
                return _err(gate_err)

            now = _now_iso()
            # Stamp last_stage_change_at + clear stale_alert_at on every
            # status change (audit-2026-05-04 item 6).
            if is_archive_move:
                if was_cancellation:
                    # Distinguish cancellation from delivery — both land
                    # in archive but the archive view's "show cancelled"
                    # toggle keys on cancelled_at.
                    await c.execute(
                        "UPDATE tasks SET status = 'archive', "
                        "completed_at = ?, archived_at = ?, cancelled_at = ?, "
                        "last_stage_change_at = ?, stale_alert_at = NULL, stall_escalation_level = 0 "
                        "WHERE id = ? AND project_id = ?",
                        (now, now, now, now, task_id, project_id),
                    )
                else:
                    await c.execute(
                        "UPDATE tasks SET status = 'archive', "
                        "completed_at = ?, archived_at = ?, "
                        "last_stage_change_at = ?, stale_alert_at = NULL, stall_escalation_level = 0 "
                        "WHERE id = ? AND project_id = ?",
                        (now, now, now, task_id, project_id),
                    )
                # Free any agent pointing at this task — broader than
                # `tasks.owner` alone to catch role assignees (shipper,
                # auditor) whose `current_task_id` was set during a role
                # swap. A narrow owner clear leaves them with a stale
                # ptr that feeds the watchdog phantom stall alerts on
                # the archived task.
                await c.execute(
                    "UPDATE agents SET current_task_id = NULL "
                    "WHERE current_task_id = ?",
                    (task_id,),
                )
            else:
                await c.execute(
                    "UPDATE tasks SET status = ?, "
                    "last_stage_change_at = ?, stale_alert_at = NULL, stall_escalation_level = 0 "
                    "WHERE id = ? AND project_id = ?",
                    (new_status, now, task_id, project_id),
                )
            await c.commit()
        finally:
            await c.close()

        # Emit both the kanban-shaped event AND the legacy back-compat
        # event so downstream listeners that haven't migrated still work.
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_stage_changed",
                "task_id": task_id,
                "from": old_status,
                "to": new_status,
                "reason": "manual",
                "note": note,
                "owner": current_owner,
            }
        )
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_updated",
                "task_id": task_id,
                "old_status": old_status,
                "new_status": new_status,
                "note": note,
                "owner": current_owner,
            }
        )
        # Notify the creator when work they handed off finishes (or is
        # cancelled). Same heuristic as before — skip self-notify and
        # human-creator. The "blocked" branch is gone (blocked is now
        # an orthogonal flag, not a status); a Player blocking a task
        # should call coord_set_task_blocked, which has its own event.
        if (
            is_archive_move
            and created_by
            and created_by != caller_id
            and created_by != "human"
        ):
            try:
                from server.agents import _deliver_system_message
                verb = "cancelled" if was_cancellation else "finished"
                note_line = f"\nNote: {note}" if note else ""
                await _deliver_system_message(
                    from_id=caller_id,
                    to_id=created_by,
                    subject=f"{task_id} {verb}",
                    body=(
                        f"I {verb} {task_id} \"{task_title[:100]}\"."
                        f"{note_line}"
                    ),
                    priority="normal",
                )
            except Exception:
                pass
        suffix = f" — {note}" if note else ""
        head = f"Updated {task_id}: {old_status} → {new_status}{suffix}."
        if new_status == "archive":
            return _ok(
                f"{head} Task is closed (manual). NO auto-summary "
                f"fires — Coach forced the archive, you decide what "
                f"to tell the user. If you want the user notified, "
                f"call coord_send_message or coord_request_human "
                f"yourself."
            )
        return _ok(
            f"{head} The kanban auto-wakes the new-stage assignee "
            f"if one is configured."
            + (" Do NOT follow up with coord_send_message; the wake covers it." if note else
               " No note was passed — use coord_send_message if the assignee needs context.")
        )


    @tool(
        "coord_send_message",
        (
            "Send a message to another agent or to the whole team.\n"
            "Params:\n"
            "- to: recipient slot id ('coach' or 'p1'..'p10'), or 'broadcast' "
            "to reach the whole team.\n"
            "- body: message text (required, max 5000 chars)\n"
            "- subject: optional short subject line (max 200 chars)\n"
            "- priority: 'normal' (default) or 'interrupt' for urgent items\n"
            "Messaging is free-form — anyone can message anyone for info "
            "sharing. Assigning work still only happens through the task "
            "board (Coach creates + assigns, Players claim)."
        ),
        # Raw JSON schema: to + body required; subject + priority optional.
        {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "body": {"type": "string"},
                "subject": {"type": "string"},
                "priority": {"type": "string"},
            },
            "required": ["to", "body"],
        },
    )
    async def send_message(args: dict[str, Any]) -> dict[str, Any]:
        to = (args.get("to") or "").strip().lower()
        body = args.get("body") or ""
        subject = (args.get("subject") or "").strip() or None
        priority = (args.get("priority") or "normal").strip().lower()

        if not to:
            return _err("'to' is required (agent id or 'broadcast')")
        if to not in VALID_RECIPIENTS:
            return _err(
                f"invalid recipient '{to}' — must be 'coach', 'p1'..'p10', "
                "or 'broadcast'"
            )
        if to == caller_id:
            return _err("you can't send a message to yourself")
        if not body.strip():
            return _err("body cannot be empty")
        if len(body) > 5000:
            return _err(f"body too long ({len(body)} chars, max 5000)")
        if subject and len(subject) > 200:
            return _err(f"subject too long ({len(subject)} chars, max 200)")
        if priority not in ("normal", "interrupt"):
            return _err(
                f"invalid priority '{priority}' (must be 'normal' or 'interrupt')"
            )

        # Lock enforcement: Coach cannot direct-message a locked
        # Player. Broadcasts still go through at send-time (the delivery
        # filter is in read_inbox) so Coach doesn't have to know the
        # lock state of every team member when pushing a broadcast.
        if caller_is_coach and to != "broadcast" and await _is_locked(to):
            return _err(
                f"{to} is locked (human marked them off-limits for Coach). "
                f"Message not sent."
            )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "INSERT INTO messages (project_id, from_id, to_id, subject, body, priority) "
                "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                (project_id, caller_id, to, subject, body, priority),
            )
            row = await cur.fetchone()
            msg_id = dict(row)["id"] if row else None
            await c.commit()
        finally:
            await c.close()

        # Body cap — keep events from carrying multi-MB tool dumps
        # while still showing the user enough to read the message in
        # context. The full text always lives in the `messages` table
        # and surfaces in EnvPane → Inbox; this preview is just the
        # in-pane render. 4000 chars covers practically every agent
        # message without bloating the events stream.
        body_preview = body[:4000]
        body_truncated = len(body) > 4000
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "message_sent",
                "message_id": msg_id,
                "to": to,
                "subject": subject,
                "body_preview": body_preview,
                "body_full_len": len(body),
                "body_truncated": body_truncated,
                "priority": priority,
            }
        )
        # Auto-wake direct recipients so they actually read + respond.
        # Skip broadcasts — waking every agent on every team announcement
        # would spiral costs. If you want broadcasts to nudge everyone,
        # Coach can @-mention specific slots instead.
        if to != "broadcast":
            try:
                from server.agents import maybe_wake_agent
                subj = f" (subject: {subject})" if subject else ""
                # Include a body preview inline so the recipient doesn't
                # have to spend a tool-call to see what the message was —
                # keeps the conversation snappy. Full body + mark-read
                # still requires coord_read_inbox for anything longer.
                preview_snippet = body.strip().replace("\n", " ")[:240]
                wake_body = (
                    f"New message from {caller_id}{subj}: "
                    f"\"{preview_snippet}\""
                )
                # Append the canonical turn-end reminder when the
                # recipient is a Player. Coach has different discipline.
                if to != "coach":
                    wake_body = _with_player_reminder(wake_body)
                await maybe_wake_agent(
                    to,
                    wake_body,
                    bypass_debounce=True,
                )
            except Exception:
                pass
        preview = body.strip().replace("\n", " ")[:60]
        return _ok(
            f"sent to {to}"
            + (f" (subject: {subject})" if subject else "")
            + f": \"{preview}\""
        )

    @tool(
        "coord_read_inbox",
        (
            "Read and drain your unread messages. Returns every message "
            "targeted at you (direct or broadcast) that you haven't yet "
            "seen, in chronological order, then marks them as read FOR "
            "YOU (broadcasts stay unread for other recipients)."
        ),
        {},
    )
    async def read_inbox(args: dict[str, Any]) -> dict[str, Any]:
        # Locked Players ignore every Coach-sourced message (direct or
        # broadcast). The filter lives at the read layer so a locked
        # Player flipping back to unlocked still gets the message —
        # it's queued but invisible while locked. Human and peer-Player
        # messages pass through unaffected.
        reader_locked = await _is_locked(caller_id)
        project_id = await resolve_active_project()

        c = await configured_conn()
        try:
            # Per-recipient unread via NOT EXISTS on message_reads — avoids
            # the broadcast bug where the first reader hides the message
            # from everyone else.
            sql = (
                "SELECT m.id, m.from_id, m.to_id, m.subject, m.body, "
                "       m.sent_at, m.priority "
                "FROM messages m "
                "WHERE m.project_id = ? "
                "  AND (m.to_id = ? OR m.to_id = 'broadcast') "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM message_reads r "
                "    WHERE r.message_id = m.id AND r.agent_id = ?"
                "  ) "
            )
            params: tuple[Any, ...] = (project_id, caller_id, caller_id)
            if reader_locked:
                sql += "  AND m.from_id != 'coach' "
            sql += "ORDER BY m.sent_at ASC"
            cur = await c.execute(sql, params)
            rows = await cur.fetchall()
            if not rows:
                return _ok("(no unread messages)")

            # Mark each message read by this caller only.
            await c.executemany(
                "INSERT OR IGNORE INTO message_reads (message_id, agent_id) "
                "VALUES (?, ?)",
                [(dict(r)["id"], caller_id) for r in rows],
            )
            await c.commit()
        finally:
            await c.close()

        lines = [
            f"{len(rows)} unread message{'s' if len(rows) != 1 else ''}:"
        ]
        for i, r in enumerate(rows, 1):
            d = dict(r)
            broadcast_note = " [broadcast]" if d["to_id"] == "broadcast" else ""
            priority_note = " [INTERRUPT]" if d["priority"] == "interrupt" else ""
            subject_line = f"\n  Subject: {d['subject']}" if d["subject"] else ""
            lines.append(
                f"\n[{i}] from {d['from_id']}{broadcast_note}{priority_note} "
                f"({d['sent_at']}):{subject_line}\n  {d['body']}"
            )
        return _ok("\n".join(lines))

    @tool(
        "coord_list_memory",
        (
            "List all topics in the shared memory scratchpad, newest first. "
            "Returns topic name, version, last_updated, last_updated_by, and "
            "content length so you can see what's there without reading it."
        ),
        {},
    )
    async def list_memory(args: dict[str, Any]) -> dict[str, Any]:
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT topic, version, last_updated, last_updated_by, "
                "length(content) AS size FROM memory_docs "
                "WHERE project_id = ? "
                "ORDER BY last_updated DESC LIMIT 200",
                (project_id,),
            )
            rows = await cur.fetchall()
        finally:
            await c.close()
        if not rows:
            return _ok("(memory is empty)")
        lines = [f"{len(rows)} memory topic{'s' if len(rows) != 1 else ''}:"]
        for r in rows:
            d = dict(r)
            lines.append(
                f"  {d['topic']}  (v{d['version']}, {d['size']} chars, "
                f"updated {d['last_updated']} by {d['last_updated_by']})"
            )
        return _ok("\n".join(lines))

    @tool(
        "coord_read_memory",
        (
            "Read a shared memory doc by topic. Returns the current content "
            "plus metadata (version, last_updated, last_updated_by). "
            "Fails if the topic doesn't exist."
        ),
        {"topic": str},
    )
    async def read_memory(args: dict[str, Any]) -> dict[str, Any]:
        topic = (args.get("topic") or "").strip().lower()
        if not topic:
            return _err("topic is required")
        if not MEMORY_TOPIC_RE.match(topic):
            return _err(
                f"invalid topic '{topic}' — must be lowercase alphanumeric "
                "with dashes, 1-64 chars, starting with a letter or digit"
            )
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT content, version, last_updated, last_updated_by "
                "FROM memory_docs WHERE topic = ? AND project_id = ?",
                (topic, project_id),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
        if not row:
            return _err(
                f"no memory topic '{topic}'. Use coord_list_memory to see "
                "what's available."
            )
        d = dict(row)
        return _ok(
            f"[{topic}  v{d['version']}  updated {d['last_updated']} "
            f"by {d['last_updated_by']}]\n\n{d['content']}"
        )

    @tool(
        "coord_update_memory",
        (
            "Write or overwrite a shared memory doc. Any agent can update "
            "any topic — it's a commons. Last write wins; the event log "
            "preserves the history of all updates. Use this to drop notes "
            "for other agents (findings, design decisions, gotchas, "
            "conventions). Topic is the filename-style key; content is the "
            "full markdown body."
        ),
        {"topic": str, "content": str},
    )
    async def update_memory(args: dict[str, Any]) -> dict[str, Any]:
        topic = (args.get("topic") or "").strip().lower()
        content = args.get("content") or ""
        if not topic:
            return _err("topic is required")
        if not MEMORY_TOPIC_RE.match(topic):
            return _err(
                f"invalid topic '{topic}' — must be lowercase alphanumeric "
                "with dashes, 1-64 chars, starting with a letter or digit"
            )
        if not content.strip():
            return _err("content cannot be empty (use a delete tool later if needed)")
        if len(content) > MEMORY_CONTENT_MAX:
            return _err(
                f"content too long ({len(content)} chars, max {MEMORY_CONTENT_MAX})"
            )
        now = _now_iso()
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            # UPSERT: insert with version=1, or increment on conflict.
            cur = await c.execute(
                "INSERT INTO memory_docs "
                "(project_id, topic, content, last_updated, last_updated_by, version) "
                "VALUES (?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(project_id, topic) DO UPDATE SET "
                "  content = excluded.content, "
                "  last_updated = excluded.last_updated, "
                "  last_updated_by = excluded.last_updated_by, "
                "  version = memory_docs.version + 1 "
                "RETURNING version",
                (project_id, topic, content, now, caller_id),
            )
            row = await cur.fetchone()
            version = dict(row)["version"] if row else 1
            await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": now,
                "agent_id": caller_id,
                "type": "memory_updated",
                "topic": topic,
                "version": version,
                "size": len(content),
            }
        )

        # Fire-and-forget mirror to the cloud drive as a plain .md file
        # under /harness/memory/<topic>.md. Failures are swallowed and
        # logged inside WebDAVClient — they never block the tool call.
        if webdav.enabled:
            header = (
                f"<!-- auto-mirrored from the harness memory table\n"
                f"     topic: {topic}\n"
                f"     version: {version}\n"
                f"     last_updated: {now}\n"
                f"     last_updated_by: {caller_id}\n"
                f"-->\n\n"
            )
            asyncio.create_task(
                webdav.write_text(
                    f"projects/{project_id}/memory/{topic}.md", header + content
                )
            )

        return _ok(
            f"saved memory[{topic}] v{version} ({len(content)} chars)"
            + (" · mirrored to WebDAV" if webdav.enabled else "")
        )

    @tool(
        "coord_write_knowledge",
        (
            "Write a durable artifact to the team knowledge bucket at "
            "<cloud-drive>/knowledge/<path> (+ local /data/knowledge cache). "
            "Distinct from memory (overwritable scratchpad keyed by topic) "
            "and context (governance docs, Coach-only): knowledge is the "
            "free-form output bucket for reports, research, specs, and "
            "designs — anything worth reading again weeks from now.\n"
            "\n"
            "Path is agent-chosen within limits:\n"
            "  - must end in .md or .txt\n"
            "  - at most 4 path segments (e.g. 'reports/2026/04/weekly.md')\n"
            "  - each segment must start with alphanumeric; no spaces or /..\n"
            "\n"
            "Overwrites without warning if the path exists — date-stamp "
            "your filenames if you want history (reports/2026-04-23-review.md).\n"
            "\n"
            "Params:\n"
            "- path: POSIX relative path under knowledge/ (required)\n"
            "- body: full markdown or plain text content (required)"
        ),
        {"path": str, "body": str},
    )
    async def write_knowledge(args: dict[str, Any]) -> dict[str, Any]:
        path = (args.get("path") or "").strip()
        body = args.get("body") or ""
        try:
            ok = await knowmod.write(path, body, author=caller_id)
        except ValueError as e:
            return _err(str(e))
        if not ok:
            return _err("knowledge write failed — check server logs")
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "knowledge_written",
                "path": path,
                "size": len(body),
            }
        )
        return _ok(
            f"saved knowledge[{path}] ({len(body)} chars)"
            + (" · mirrored to WebDAV" if webdav.enabled else "")
        )

    @tool(
        "coord_read_knowledge",
        (
            "Read a knowledge doc by path. Local cache first, cloud-drive "
            "fallback. Returns the full body as text; caller should "
            "chunk or summarize for prompt brevity if needed.\n"
            "\n"
            "Use coord_list_knowledge to discover what's already been "
            "written before asking a Player to redo similar work.\n"
            "\n"
            "Params:\n"
            "- path: POSIX relative path under knowledge/ (required)"
        ),
        {"path": str},
    )
    async def read_knowledge(args: dict[str, Any]) -> dict[str, Any]:
        path = (args.get("path") or "").strip()
        try:
            body = await knowmod.read(path)
        except ValueError as e:
            return _err(str(e))
        if body is None:
            return _err(f"knowledge[{path}] not found")
        return _ok(
            f"knowledge[{path}] ({len(body)} chars):\n\n{body}"
        )

    @tool(
        "coord_list_knowledge",
        (
            "List every knowledge doc currently stored (POSIX paths, "
            "sorted). Cheap — reads a disk directory. No params."
        ),
        {},
    )
    async def list_knowledge(args: dict[str, Any]) -> dict[str, Any]:
        paths = await knowmod.list_paths()
        if not paths:
            return _ok("(no knowledge docs yet)")
        return _ok("\n".join(paths))

    @tool(
        "coord_save_output",
        (
            "Save a binary deliverable (docx / pdf / png / zip / …) to "
            "the team outputs bucket at <cloud-drive>/outputs/<path> (+ local "
            "/data/outputs cache). Use for final artifacts the human "
            "asked for — reports, charts, exports. Text deliverables "
            "usually belong in knowledge/ instead.\n"
            "\n"
            "Path is agent-chosen within limits:\n"
            "  - at most 4 path segments\n"
            "  - each segment starts with alphanumeric, no spaces / ..\n"
            "  - leaf extension must be in the outputs allow-list "
            "(docx, xlsx, pptx, pdf, png, jpg, gif, webp, svg, zip, "
            "tar, gz, csv, tsv, md, txt, html, json)\n"
            "\n"
            "Content is base64-encoded — read the file with Bash "
            "(`base64 -w0 foo.docx`) or have your process write base64 "
            "directly. 20 MB size cap after decoding.\n"
            "\n"
            "Overwrites without warning if the path exists — date-stamp "
            "your filenames if you want history.\n"
            "\n"
            "Params:\n"
            "- path: POSIX relative path under outputs/ (required)\n"
            "- content_base64: base64-encoded bytes of the file (required)"
        ),
        {"path": str, "content_base64": str},
    )
    async def save_output(args: dict[str, Any]) -> dict[str, Any]:
        path = (args.get("path") or "").strip()
        b64 = args.get("content_base64") or ""
        try:
            data = outmod.decode_base64(b64)
        except ValueError as e:
            return _err(str(e))
        try:
            ok = await outmod.save(path, data, author=caller_id)
        except ValueError as e:
            return _err(str(e))
        if not ok:
            return _err("outputs write failed — check server logs")
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "output_saved",
                "path": path,
                "bytes": len(data),
            }
        )
        return _ok(
            f"saved outputs[{path}] ({len(data)} bytes)"
            + (" · mirrored to WebDAV" if webdav.enabled else "")
        )

    @tool(
        "coord_commit_push",
        (
            "**Your message to Coach that the executor work is done.** "
            "Players only (Coach never writes code). Calling this tool "
            "IS the act of telling Coach you've delivered — without it, "
            "Coach has no idea your work exists, and the kanban can't "
            "record it. Writing the code to your worktree without "
            "calling this tool is silence.\n\n"
            "Runs git add -A; git commit -m <message>; git push origin "
            "HEAD (unless push='false') in your worktree, then signals "
            "Coach via `commit_pushed` event in the per-project log AND "
            "wakes Coach in real time with your message_to_coach as the "
            "wake reason (v2 §7.2.1) — Coach reads + decides without "
            "waiting for the next recurrence tick.\n\n"
            "Params:\n"
            "- message: commit message (required)\n"
            "- push: 'true' (default) or 'false' to skip the push.\n"
            "- task_id: the kanban task this commit delivers against "
            "(optional but STRONGLY RECOMMENDED). When provided, the "
            "event log row carries `task_id` so Coach correlates the "
            "commit with the right task when reading the wake. Without "
            "task_id the commit still works but lands without a "
            "kanban link — Coach has to figure out which task it was "
            "for.\n"
            "- message_to_coach: ONE-LINE summary Coach reads as your "
            "primary signal. Use this to flag what you noticed, any "
            "caveats, what the next person should know. Carried "
            "verbatim in the `commit_pushed` payload — this is the "
            "field Coach reads first.\n\n"
            "Returns 'nothing to commit' as a soft-OK if the working "
            "tree is clean. Requires the active project to have a repo "
            "URL configured; push also needs pushable credentials "
            "(typically a PAT embedded in the project repo URL)."
        ),
        # Raw JSON schema: message required; push/task_id/message_to_coach
        # are all optional (task_id especially — "optional but strongly
        # recommended" per the tool description).
        {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "push": {"type": "string"},
                "task_id": {"type": "string"},
                "message_to_coach": {"type": "string"},
            },
            "required": ["message"],
        },
    )
    async def commit_push(args: dict[str, Any]) -> dict[str, Any]:
        if caller_is_coach:
            return _err(
                "Coach delegates; only Players commit code. If you want "
                "Coach to trigger a commit, message a Player with the task."
            )
        if not await project_repo_configured():
            return _err(
                "the active project has no repo_url configured; no git "
                "worktree to commit into. Ask the operator to set the "
                "repo URL via Options → Projects."
            )

        message = (args.get("message") or "").strip()
        if not message:
            return _err("message is required")
        if len(message) > 2000:
            return _err(f"message too long ({len(message)} chars, max 2000)")

        message_to_coach = (args.get("message_to_coach") or "").strip()
        if len(message_to_coach) > 2000:
            return _err(
                f"message_to_coach too long ({len(message_to_coach)} chars, "
                f"max 2000)"
            )

        push_raw = str(args.get("push") or "true").strip().lower()
        do_push = push_raw not in ("false", "0", "no", "off")

        # Optional task_id — empty string treated as None.
        task_id_raw = (args.get("task_id") or "").strip()
        task_id_in: str | None = task_id_raw or None
        auto_bound_task_id: str | None = None

        # v0.3.2 audit-fix (kanban-flow gap 2): if the caller omitted
        # task_id but has exactly one active executor task in the
        # current project, auto-bind it. Forgetting `task_id=` was the
        # #1 cause of "Player did the work but the kanban didn't
        # advance" reports. The auto-bind is logged in the response so
        # the Player learns the right shape for next time.
        if task_id_in is None:
            try:
                project_id_for_lookup = await resolve_active_project()
                c = await configured_conn()
                try:
                    # Require a LIVE (uncompleted, unsuperseded)
                    # executor role row so the downstream validator
                    # cannot reject what we just bound. The live role
                    # row, not tasks.owner, is authoritative here:
                    # tasks.owner can lag behind after reassignment,
                    # so the query only cares that the caller holds the
                    # active executor assignment on an execute-stage
                    # task. Filtering on status='execute' alone could
                    # still pick up a task whose executor row got
                    # marked complete by a prior commit but never
                    # advanced (subscriber crash / push failure),
                    # producing a confusing "no active uncompleted
                    # executor role" error immediately after auto-bind.
                    cur = await c.execute(
                        "SELECT t.id FROM tasks t "
                        "WHERE t.project_id = ? "
                        "AND t.status = 'execute' "
                        "AND EXISTS ("
                        "  SELECT 1 FROM task_role_assignments tra "
                        "  WHERE tra.task_id = t.id "
                        "  AND tra.role = 'executor' "
                        "  AND tra.owner = ? "
                        "  AND tra.completed_at IS NULL "
                        "  AND tra.superseded_by IS NULL "
                        ") "
                        "ORDER BY t.claimed_at DESC LIMIT 2",
                        (project_id_for_lookup, caller_id),
                    )
                    candidates = [dict(r)["id"] for r in await cur.fetchall()]
                finally:
                    await c.close()
            except Exception:
                candidates = []
            # Exactly one active executor task → auto-bind. Multiple
            # live executor tasks for a caller are rare but possible
            # during odd reassignment states, so we guard anyway.
            if len(candidates) == 1:
                task_id_in = candidates[0]
                auto_bound_task_id = task_id_in

        # Bind task_id to the caller's executor role at entry — before
        # we run any git work — so a Player can't pass another Player's
        # task_id (or a stale id) and ride it into the kanban
        # subscriber. Validation: task is in the active project, sits
        # in `execute`, and the live executor role assignment belongs
        # to the caller with completed_at NULL.
        if task_id_in:
            project_id = await resolve_active_project()
            c = await configured_conn()
            try:
                cur = await c.execute(
                    "SELECT t.status, "
                    "(SELECT tra.owner FROM task_role_assignments tra "
                    " WHERE tra.task_id = t.id AND tra.role = 'executor' "
                    " AND tra.completed_at IS NULL "
                    " AND tra.superseded_by IS NULL "
                    " ORDER BY tra.assigned_at DESC LIMIT 1) AS active_owner, "
                    "(SELECT 1 FROM task_role_assignments tra "
                    " WHERE tra.task_id = t.id AND tra.role = 'executor' "
                    " AND tra.completed_at IS NULL "
                    " AND tra.superseded_by IS NULL "
                    " LIMIT 1) AS has_live_role, "
                    "(SELECT 1 FROM task_role_assignments tra "
                    " WHERE tra.task_id = t.id AND tra.role = 'executor' "
                    " AND tra.owner = ? AND tra.completed_at IS NULL "
                    " AND tra.superseded_by IS NULL "
                    " LIMIT 1) AS has_role "
                    "FROM tasks t "
                    "WHERE t.id = ? AND t.project_id = ?",
                    (caller_id, task_id_in, project_id),
                )
                row = await cur.fetchone()
            finally:
                await c.close()
            if row is None:
                return _err(
                    f"task {task_id_in} not found in the active project. "
                    f"Pass a valid task_id from your active executor "
                    f"assignment, or omit task_id to commit without "
                    f"driving the kanban."
                )
            t = dict(row)
            if t.get("status") != "execute":
                return _err(
                    f"task {task_id_in} is in stage "
                    f"'{t.get('status')}', not 'execute'; "
                    f"coord_commit_push can only deliver against an "
                    f"executor task currently in execute."
                )
            if not t.get("has_live_role"):
                return _err(
                    f"task {task_id_in} has no active uncompleted "
                    f"executor role for {caller_id}. The role may have "
                    f"been superseded by a re-assignment, or already "
                    f"completed by a prior commit."
                )
            active_owner = t.get("active_owner")
            if active_owner != caller_id:
                return _err(
                    f"task {task_id_in} has active executor role owned by "
                    f"{active_owner or 'no one'}, not {caller_id}. "
                    f"You can only call coord_commit_push for your own "
                    f"executor task."
                )

        cwd = await workspace_dir(caller_id)
        if not (cwd / ".git").exists():
            return _err(
                f"worktree at {cwd} is not a git checkout — something "
                "went wrong during workspace provisioning. Check "
                "/api/status workspaces section."
            )

        # Env scrub for git subprocesses — git can run worktree-supplied
        # hooks (.git/hooks/pre-commit, etc.) which an agent could plant
        # to dump inherited env. Build a clean env so HARNESS_TOKEN /
        # secret material isn't readable from a hook. Push auth uses
        # the PAT embedded in the remote URL (see _expand_placeholders
        # in workspaces.py), so we don't need GITHUB_TOKEN in env.
        from server.agent_env import build_clean_agent_env
        clean_env = build_clean_agent_env()

        async def run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
            def _do() -> tuple[int, str, str]:
                p = subprocess.run(
                    cmd,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=clean_env,
                )
                return p.returncode, p.stdout, p.stderr
            return await asyncio.to_thread(_do)

        # Push-time HEAD enforcement: verify the worktree is on work/<slot>
        # before touching the index. Without this check a slot whose
        # worktree is on the wrong branch (e.g. work/p5 on p6's tree)
        # would push to the wrong remote branch and corrupt peer history.
        # This is the push-time guard complementary to _ensure_worktree's
        # provision-time guard (t-2026-05-15-45716da0 / 75666c9).
        expected_branch = f"work/{caller_id}"
        code, branch_out, _ = await run(["git", "branch", "--show-current"])
        current_branch = branch_out.strip()
        if current_branch != expected_branch:
            return _err(
                f"worktree is on branch {current_branch!r} but should be "
                f"{expected_branch!r}. Run `git checkout {expected_branch}` "
                f"in {cwd} to fix, or call POST /api/projects/{{id}}/repo/provision "
                f"to re-provision the slot. Do NOT push from the wrong branch — "
                f"that would write your work into another slot's history."
            )

        code, _out, err = await run(["git", "add", "-A"])
        if code != 0:
            return _err(f"git add failed: {err.strip()[:300]}")

        code, status_out, _ = await run(["git", "status", "--porcelain"])
        if not status_out.strip():
            # v0.3.7: a clean slot worktree is suspicious if there are
            # uncommitted changes in the project's shared seed checkout
            # (/data/projects/<id>/repo/.project). That means the
            # Player edited the wrong tree — work is stranded on a tree
            # that belongs to no slot. Surface a loud, named error so
            # the Player can fix it instead of getting an opaque
            # "nothing to commit" and walking away. (Production trace
            # 2026-05-04: p8 wrote to .project, hit "nothing to commit"
            # here, marked the task blocked.)
            try:
                from server.paths import project_paths
                base_repo = project_paths(
                    await resolve_active_project()
                ).bare_clone
            except Exception:
                base_repo = None
            if base_repo and (base_repo / ".git").exists():
                def _peek_base() -> str:
                    try:
                        p = subprocess.run(
                            ["git", "status", "--porcelain"],
                            cwd=str(base_repo),
                            capture_output=True,
                            text=True,
                            timeout=15,
                            env=clean_env,
                        )
                        return (p.stdout or "")
                    except Exception:
                        return ""
                base_dirty = (await asyncio.to_thread(_peek_base)).strip()
                if base_dirty:
                    return _err(
                        f"your worktree at {cwd} is clean, but the "
                        f"shared seed checkout at {base_repo} has "
                        f"uncommitted changes. The shared checkout is "
                        f"not yours to commit from — per-worktree "
                        f"isolation is mandatory. Move your changes "
                        f"into your worktree (cd {cwd} and re-apply, "
                        f"or `git -C {base_repo} stash && git -C {cwd} "
                        f"stash pop` if the patch is small) and retry. "
                        f"Do NOT `git -C {base_repo} commit` directly "
                        f"— that bypasses your branch entirely."
                    )
            return _ok("nothing to commit (working tree clean)")

        code, out, err = await run(["git", "commit", "-m", message])
        if code != 0:
            return _err(
                f"git commit failed: {(err or out).strip()[:300]}"
            )

        code, sha_out, _ = await run(["git", "rev-parse", "--short", "HEAD"])
        sha = sha_out.strip() or "?"

        push_note = ""
        pushed_ok = False
        if do_push:
            code, _out, err = await run(
                ["git", "push", "origin", "HEAD"], timeout=120
            )
            if code != 0:
                push_note = f" (PUSH FAILED: {err.strip()[:200]})"
            else:
                push_note = " (pushed)"
                pushed_ok = True
        else:
            push_note = " (local only)"

        # Auto-advance only when the push actually succeeded (or the
        # caller explicitly asked for local-only via push=false — that
        # is the documented escape hatch). A failed push must not
        # drive the kanban; otherwise a Player whose creds are broken
        # could ride a local-only commit into archive.
        push_failed = do_push and not pushed_ok
        kanban_task_id = None if push_failed else task_id_in

        # Mark the executor role-assignment row complete only when the
        # commit is going to drive auto-advance. Entry validation
        # guarantees the UPDATE will match exactly one row when
        # task_id_in is set.
        if kanban_task_id:
            c = await configured_conn()
            try:
                await c.execute(
                    "UPDATE task_role_assignments "
                    "SET completed_at = ? "
                    "WHERE task_id = ? AND role = 'executor' "
                    "AND owner = ? AND completed_at IS NULL "
                    "AND superseded_by IS NULL",
                    (_now_iso(), kanban_task_id, caller_id),
                )
                await _reset_agent_idle_tools(c, caller_id)
                await c.commit()
            finally:
                await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "commit_pushed",
                "sha": sha,
                "message": message,
                "pushed": pushed_ok,
                "push_requested": do_push,
                # task_id drives the kanban auto-advance subscriber.
                # Cleared on push failure so a broken push can't ride a
                # local-only commit into the audit pipeline. The
                # original task_id is preserved on the row above for
                # the explicit `push=false` mode.
                "task_id": kanban_task_id,
                # Marks an auto-bind so the dashboard / event log can
                # show "we filled this in for you" rather than implying
                # the Player explicitly passed task_id.
                "task_id_auto_bound": auto_bound_task_id is not None,
                "message_to_coach": message_to_coach or None,
            }
        )

        # v2 §7.2.1 — wake Coach immediately (not just on next tick) so
        # the Player→Coach completion call is a real-time message. Mirrors
        # the role-row-completion gate above: a successful push OR an
        # explicit local-only commit (push=False, the documented escape
        # hatch) both complete the executor role and so should wake
        # Coach. A FAILED push (push_failed=True) does NOT wake — the
        # role row stays active and Coach has nothing to approve yet.
        if kanban_task_id and not push_failed:
            sha_hint = f"sha {sha}" if sha else "commit landed"
            push_note_short = (
                "pushed" if pushed_ok
                else "local-only — Player passed push=false"
            )
            await _wake_coach_for_completion(
                caller_id=caller_id,
                task_id=kanban_task_id,
                role="executor (commit)",
                message_to_coach=message_to_coach,
                artifact_path=None,
                extra_hint=(
                    f"Commit: {message[:120]!r} ({sha_hint}, "
                    f"{push_note_short})."
                ),
            )

        # v0.3.2 audit-fix (kanban-flow gap 2): if the caller committed
        # without a task_id AND no auto-bind was possible (i.e., they
        # have no active executor task), emit a Coach-routed warning
        # event. This catches the case where a Player thought they
        # were delivering a kanban task but actually committed scratch
        # work — Coach sees it and can ask. Skipped when push failed
        # (the push failure itself is the louder signal).
        if (
            task_id_in is None
            and auto_bound_task_id is None
            and not push_failed
        ):
            warning_body = (
                f"Player {caller_id} committed sha {sha} "
                f"({message[:80]!r}) without a task_id and has no "
                f"active executor task to auto-bind to. If this "
                f"commit was meant to deliver a kanban task, link it "
                f"by approving the stage with the right context "
                f"(coord_approve_stage with a note referencing the "
                f"commit), or accept it's scratch work and ignore. "
                f"If this is a recurring pattern, the Player may be "
                f"working off-board — Coach should call "
                f"coord_approve_stage(execute, <slot>) so the kanban "
                f"sees the work."
            )
            await bus.publish(
                {
                    "ts": _now_iso(),
                    "agent_id": "system",
                    "type": "commit_without_task_id_warning",
                    "committer": caller_id,
                    "sha": sha,
                    "message": message[:200],
                    "body": warning_body,
                    "to": "coach",
                }
            )

        # Build the response. v0.3.11 — every branch ends with a
        # "what's next" line so the executor knows whether they're
        # done or whether the commit didn't land on the kanban.
        msg = f"Committed {sha}: {message}{push_note}."
        if push_failed:
            msg += (
                f"\n\nPush FAILED — task NOT advanced. Fix the push "
                f"(creds, branch, conflicts) and retry "
                f"coord_commit_push. The executor role row is still "
                f"active, so the kanban still expects you to deliver."
            )
            return _ok(msg)
        if auto_bound_task_id:
            msg += (
                f"\n\nAuto-bound to task {auto_bound_task_id!r} "
                f"(you didn't pass task_id). Your executor role is "
                f"now complete. The kanban auto-advances execute → "
                f"the next stage in the trajectory; you're done with "
                f"this task unless the audit fails (you'll be re-"
                f"woken with the report). Pass "
                f"`task_id={auto_bound_task_id!r}` explicitly next "
                f"time so the binding is unambiguous."
            )
        elif task_id_in is None:
            msg += (
                "\n\nNOT bound to any kanban task (no active executor "
                "task, no task_id passed). If this commit was "
                "supposed to deliver a kanban task, the kanban will "
                "not advance — Coach has been notified. If it's "
                "scratch work, you can ignore."
            )
        else:
            # task_id_in was set + push succeeded.
            msg += (
                f"\n\nLinked to task {task_id_in!r}. Your executor "
                f"role is now complete. The kanban auto-advances "
                f"execute → the next stage in the trajectory; you're "
                f"done with this task unless the audit fails (you'll "
                f"be re-woken with the report)."
            )
        return _ok(msg)

    @tool(
        "coord_ship_to_dev",
        (
            "Ship an audited task to dev via resumable cherry-pick + PR. "
            "Player-only (Coach uses coord_approve_stage).\n"
            "\n"
            "Guards (fail-fast, in order):\n"
            "- task must be in 'ship' stage\n"
            "- caller must not be Coach\n"
            "- caller must have an active (uncompleted, not-superseded) "
            "shipper role row on the task\n"
            "- a commit_pushed project_event must exist for the task "
            "(executor must have called coord_commit_push first)\n"
            "- every audit stage in the task's trajectory must have a "
            "PASS verdict (no un-superseded FAIL allowed)\n"
            "\n"
            "On success: cherry-picks or resumes the executor commit "
            "onto a temp branch off origin/dev, opens/reuses a GitHub "
            "PR, squash-merges it, deletes the remote branch, closes "
            "the shipper role row, emits task_shipped_to_dev, and "
            "wakes Coach. Replays are idempotent when ship evidence "
            "already exists or the patch is already present on dev.\n"
            "\n"
            "Cherry-pick conflicts return an error and leave the "
            "worktree on the temp branch so the Player can resolve "
            "manually, then rerun this tool after "
            "`git cherry-pick --continue`.\n"
            "\n"
            "Params:\n"
            "- task_id: the kanban task id to ship (required)"
        ),
        {"task_id": str},
    )
    async def ship_to_dev(args: dict[str, Any]) -> dict[str, Any]:
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return _err("task_id is required")

        # Gate: caller must not be Coach
        if caller_is_coach:
            return _err(
                "coord_ship_to_dev is a Player tool — Coach ships via "
                "coord_approve_stage. Only Players in the shipper role "
                "call this tool."
            )

        project_id = await resolve_active_project()

        c = await configured_conn()
        try:
            # Gate: task must exist and be in 'ship' stage
            cur = await c.execute(
                "SELECT id, title, status, trajectory FROM tasks "
                "WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            task_row_raw = await cur.fetchone()
            if not task_row_raw:
                return _err(
                    f"task {task_id} not found in project {project_id}"
                )
            task_row = dict(task_row_raw)
            status = task_row.get("status") or ""
            if status != "ship":
                return _err(
                    f"task is in '{status}', not 'ship' — cannot ship"
                )
            task_title = (task_row.get("title") or "").strip()

            # Gate: caller must be the active shipper
            cur = await c.execute(
                "SELECT id FROM task_role_assignments "
                "WHERE task_id = ? AND role = 'shipper' AND owner = ? "
                "AND completed_at IS NULL AND superseded_by IS NULL",
                (task_id, caller_id),
            )
            shipper_row_raw = await cur.fetchone()
            if not shipper_row_raw:
                return _err(
                    f"caller {caller_id} is not the active shipper for "
                    f"task {task_id}"
                )
            shipper_row_id = dict(shipper_row_raw)["id"]

            # Gate: find executor commit SHA in project_events
            cur = await c.execute(
                "SELECT payload_pointer FROM project_events "
                "WHERE task_id = ? AND type = 'commit_pushed' "
                "ORDER BY ts DESC LIMIT 1",
                (task_id,),
            )
            pe_row = await cur.fetchone()
            if not pe_row:
                return _err(
                    f"no executor commit found for task {task_id} — "
                    f"executor must coord_commit_push before ship"
                )
            executor_sha = (
                dict(pe_row).get("payload_pointer") or ""
            ).strip()
            if not executor_sha:
                return _err(
                    f"commit_pushed event for task {task_id} has no SHA "
                    f"— executor must coord_commit_push before ship"
                )

            # Gate: audit verdicts — every audit stage in trajectory
            # must have an un-superseded PASS from the right role
            trajectory_raw = task_row.get("trajectory") or "[]"
            try:
                trajectory = json.loads(trajectory_raw)
            except Exception:
                trajectory = []
            stages_in_traj = {
                (s.get("stage") or "")
                for s in trajectory
                if isinstance(s, dict)
            }
            trajectory_has_verify = "verify" in stages_in_traj
            # Map stage name → role name used in task_role_assignments
            audit_checks = []
            if "audit_syntax" in stages_in_traj:
                audit_checks.append(("auditor_syntax", "audit_syntax"))
            if "audit_semantics" in stages_in_traj:
                audit_checks.append(
                    ("auditor_semantics", "audit_semantics")
                )
            for role_name, stage_name in audit_checks:
                if not await _has_passing_auditor(c, task_id, role_name):
                    return _err(
                        f"{stage_name} has no PASS verdict for task "
                        f"{task_id} — ship rejected"
                    )

            # Fetch repo_url for GitHub API PAT extraction
            cur = await c.execute(
                "SELECT repo_url FROM projects WHERE id = ?",
                (project_id,),
            )
            proj_row = await cur.fetchone()
            repo_url = (
                (dict(proj_row).get("repo_url") or "") if proj_row else ""
            ).strip()
        finally:
            await c.close()

        if not repo_url:
            return _err(
                "project has no repo_url configured — cannot ship via "
                "GitHub PR. Set it under Options → Projects."
            )

        # Expand ${VAR} placeholders (e.g. ${GITHUB_TOKEN}) before extracting
        # the token.  Without this, a URL stored as
        # "https://${GITHUB_TOKEN}@github.com/..." sends the literal string
        # "${GITHUB_TOKEN}" as the Bearer token → GitHub 401.
        from server.workspaces import _expand_placeholders
        repo_url = _expand_placeholders(repo_url)

        # Parse PAT + owner/repo from repo_url
        # Expected pattern: https://<token>@github.com/<owner>/<repo>
        m = re.match(
            r"https://([^@]+)@github\.com/([^/]+)/([^/]+?)(?:\.git)?$",
            repo_url,
        )
        if not m:
            return _err(
                "repo_url does not match expected pattern "
                "https://<token>@github.com/<owner>/<repo> — "
                "cannot extract PAT for GitHub API"
            )
        gh_token, gh_owner, gh_repo = m.group(1), m.group(2), m.group(3)
        if not gh_token:
            return _err(
                "repo_url PAT expanded to empty string — "
                "check that GITHUB_TOKEN (or equivalent) is set in env or "
                "the harness secrets store"
            )

        # --- Git operations in caller's worktree ---
        cwd = await workspace_dir(caller_id)

        from server.agent_env import build_clean_agent_env

        clean_env = build_clean_agent_env()
        branch_name = f"ship-{task_id}"
        caller_branch = f"work/{caller_id}"

        async def _git(
            cmd: list[str], timeout: int = 60
        ) -> tuple[int, str, str]:
            def _do() -> tuple[int, str, str]:
                p = subprocess.run(
                    cmd,
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=clean_env,
                )
                return p.returncode, p.stdout, p.stderr

            return await asyncio.to_thread(_do)

        async def _latest_ship_evidence() -> dict[str, Any] | None:
            c = await configured_conn()
            try:
                cur = await c.execute(
                    "SELECT payload_json FROM project_events "
                    "WHERE task_id = ? AND type = 'task_shipped_to_dev' "
                    "ORDER BY ts DESC, id DESC LIMIT 1",
                    (task_id,),
                )
                row = await cur.fetchone()
                if not row:
                    return None
                raw = dict(row).get("payload_json") or "{}"
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {}
                return payload if isinstance(payload, dict) else {}
            finally:
                await c.close()

        async def _complete_shipper_role() -> None:
            c = await configured_conn()
            try:
                await c.execute(
                    "UPDATE task_role_assignments SET completed_at = "
                    "COALESCE(completed_at, ?) WHERE id = ?",
                    (_now_iso(), shipper_row_id),
                )
                await c.commit()
            finally:
                await c.close()

        async def _complete_with_evidence(
            *,
            ship_sha: str,
            pr_number: Any,
            pr_url: str,
            ship_method: str,
            idempotent: bool,
            emit_event: bool = True,
        ) -> dict[str, Any]:
            await _complete_shipper_role()
            if emit_event:
                await bus.publish(
                    {
                        "ts": _now_iso(),
                        "agent_id": caller_id,
                        "type": "task_shipped_to_dev",
                        "task_id": task_id,
                        "ship_sha": ship_sha,
                        "pr_number": pr_number,
                        "pr_url": pr_url,
                        "executor_sha": executor_sha,
                        "deploy_target": "dev",
                        "ship_method": ship_method,
                        "idempotent": idempotent,
                        "to": "coach",
                    }
                )
            pr_label = f"PR #{pr_number}" if pr_number else "no PR"
            await _wake_coach_for_completion(
                caller_id=caller_id,
                task_id=task_id,
                role="shipper",
                message_to_coach=None,
                artifact_path=None,
                extra_hint=(
                    f"Shipped to dev ({ship_method}) — {pr_label}, "
                    f"dev now at {(ship_sha or '')[:8]}."
                ),
            )
            if pr_number:
                return _ok(
                    f"Task {task_id} shipped to dev.\n"
                    f"PR: {pr_url} (#{pr_number}) — squash-merged.\n"
                    f"Dev HEAD: {ship_sha}.\n"
                    f"Shipper role complete. Coach has been woken."
                )
            return _ok(
                f"Task {task_id} already present on dev.\n"
                f"Dev HEAD: {ship_sha}.\n"
                f"Shipper role complete. Coach has been woken."
            )

        existing_evidence = await _latest_ship_evidence()
        if existing_evidence is not None:
            return await _complete_with_evidence(
                ship_sha=(existing_evidence.get("ship_sha") or ""),
                pr_number=existing_evidence.get("pr_number"),
                pr_url=(existing_evidence.get("pr_url") or ""),
                ship_method=(
                    existing_evidence.get("ship_method")
                    or "existing_evidence"
                ),
                idempotent=True,
                emit_event=False,
            )

        async def _git_stdout(cmd: list[str]) -> str:
            rc, out, err = await _git(cmd)
            if rc != 0:
                raise RuntimeError(err.strip() or out.strip())
            return out.strip()

        async def _origin_dev_sha() -> str:
            return await _git_stdout(["git", "rev-parse", "origin/dev"])

        async def _current_branch() -> str:
            rc, out, _ = await _git(["git", "branch", "--show-current"])
            return out.strip() if rc == 0 else ""

        async def _local_branch_exists(branch: str) -> bool:
            rc, _, _ = await _git(
                ["git", "rev-parse", "--verify", f"refs/heads/{branch}"]
            )
            return rc == 0

        async def _status_porcelain() -> str:
            rc, out, err = await _git(["git", "status", "--porcelain=v1"])
            if rc != 0:
                raise RuntimeError(err.strip() or out.strip())
            return out

        async def _worktree_dirty() -> bool:
            return bool((await _status_porcelain()).strip())

        async def _has_unmerged_paths() -> bool:
            rc, out, _ = await _git(
                ["git", "diff", "--name-only", "--diff-filter=U"]
            )
            return rc == 0 and bool(out.strip())

        async def _cherry_pick_in_progress() -> bool:
            rc, _, _ = await _git(
                ["git", "rev-parse", "--verify", "CHERRY_PICK_HEAD"]
            )
            return rc == 0

        async def _executor_patch_on_dev() -> bool:
            rc, _, _ = await _git(
                ["git", "merge-base", "--is-ancestor", executor_sha, "origin/dev"]
            )
            if rc == 0:
                return True
            rc, out, _ = await _git(
                [
                    "git",
                    "cherry",
                    "origin/dev",
                    executor_sha,
                    f"{executor_sha}^",
                ]
            )
            if rc != 0:
                return False
            return any(
                line.strip().startswith(f"- {executor_sha}")
                for line in out.splitlines()
            )

        def _looks_empty_cherry_pick(out: str, err: str) -> bool:
            text = f"{out}\n{err}".lower()
            needles = (
                "previous cherry-pick is now empty",
                "nothing to commit",
                "patch is empty",
                "the previous cherry-pick is empty",
                "empty commit set passed",
            )
            return any(n in text for n in needles)

        rc, _, err = await _git(["git", "fetch", "origin"])
        if rc != 0:
            return _err(f"git fetch failed: {err.strip()}")

        if await _executor_patch_on_dev():
            return await _complete_with_evidence(
                ship_sha=await _origin_dev_sha(),
                pr_number=None,
                pr_url="",
                ship_method="already_present",
                idempotent=True,
            )

        branch_exists = await _local_branch_exists(branch_name)
        resumed_branch = False
        if branch_exists:
            current = await _current_branch()
            if current != branch_name:
                try:
                    dirty = await _worktree_dirty()
                except RuntimeError as exc:
                    return _err(f"git status failed: {exc}")
                if dirty:
                    return _err(
                        f"local ship branch {branch_name} exists, but "
                        f"current branch {current or '(detached)'} has "
                        f"uncommitted changes. Finish/clean the current "
                        f"worktree, then rerun coord_ship_to_dev."
                    )
                rc, _, err = await _git(["git", "checkout", branch_name])
                if rc != 0:
                    return _err(
                        f"git checkout existing ship branch failed: "
                        f"{err.strip()}"
                    )
            if await _cherry_pick_in_progress() or await _has_unmerged_paths():
                return _err(
                    f"ship branch {branch_name} still has unresolved "
                    f"cherry-pick conflicts. Resolve them, run "
                    f"`git cherry-pick --continue`, then rerun "
                    f"coord_ship_to_dev. To abandon: "
                    f"`git cherry-pick --abort && "
                    f"git checkout {caller_branch}`."
                )
            resumed_branch = True
        else:
            rc, _, err = await _git(
                ["git", "checkout", "-b", branch_name, "origin/dev"]
            )
            if rc != 0:
                return _err(
                    f"git checkout ship branch failed: {err.strip()}"
                )

            rc, out, err = await _git(
                ["git", "cherry-pick", "-x", executor_sha]
            )
            if rc != 0:
                if _looks_empty_cherry_pick(out, err):
                    await _git(["git", "cherry-pick", "--abort"])
                    if await _executor_patch_on_dev():
                        return await _complete_with_evidence(
                            ship_sha=await _origin_dev_sha(),
                            pr_number=None,
                            pr_url="",
                            ship_method="already_present",
                            idempotent=True,
                        )
                    empty_detail = (err.strip() or out.strip())[:300]
                    return _err(
                        f"cherry-pick of {executor_sha} produced an "
                        f"empty/no-op patch, but the patch was not "
                        f"confirmed on origin/dev: {empty_detail}. "
                        f"Shipper role remains open; inspect "
                        f"{branch_name} before retrying."
                    )
                # Leave the worktree on the temp branch for manual resolution
                conflict_detail = (err.strip() or out.strip())[:300]
                return _err(
                    f"cherry-pick of {executor_sha} onto origin/dev "
                    f"conflicted: {conflict_detail}\n"
                    f"Resolve manually: fix conflicts, then "
                    f"`git cherry-pick --continue`.\n"
                    f"To abort and clean up: "
                    f"`git cherry-pick --abort && "
                    f"git checkout {caller_branch} && "
                    f"git branch -D {branch_name}`."
                )

        rc, _, err = await _git(
            ["git", "push", "origin", f"{branch_name}:{branch_name}"]
        )
        if rc != 0:
            return _err(f"git push ship branch failed: {err.strip()}")

        # --- GitHub API: create PR → squash merge → delete remote branch ---
        import httpx

        gh_headers = {
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        api_base = (
            f"https://api.github.com/repos/{gh_owner}/{gh_repo}"
        )
        pr_title = (
            f"[ship] {task_id}: {task_title}"
            if task_title
            else f"[ship] {task_id}"
        )
        pr_body = (
            f"Auto-shipped via coord_ship_to_dev.\n"
            f"Task: {task_id}\n"
            f"Executor commit: {executor_sha}"
        )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async def _find_open_pr() -> dict[str, Any] | None:
                    r = await client.get(
                        f"{api_base}/pulls",
                        headers=gh_headers,
                        params={
                            "head": f"{gh_owner}:{branch_name}",
                            "base": "dev",
                            "state": "open",
                        },
                    )
                    if r.status_code != 200:
                        return None
                    data = r.json()
                    if isinstance(data, list) and data:
                        return data[0]
                    return None

                pr_data = await _find_open_pr()
                if pr_data is None:
                    r = await client.post(
                        f"{api_base}/pulls",
                        headers=gh_headers,
                        json={
                            "title": pr_title,
                            "head": branch_name,
                            "base": "dev",
                            "body": pr_body,
                        },
                    )
                    if r.status_code == 422:
                        pr_data = await _find_open_pr()
                    if pr_data is None:
                        if r.status_code not in (200, 201):
                            return _err(
                                f"GitHub API {r.status_code} on PR "
                                f"create: {r.text[:200]}"
                            )
                        pr_data = r.json()
                pr_number = pr_data.get("number")
                pr_url = pr_data.get("html_url", "")

                # Squash merge PR
                r = await client.put(
                    f"{api_base}/pulls/{pr_number}/merge",
                    headers=gh_headers,
                    json={
                        "merge_method": "squash",
                        "commit_title": f"[ship] {task_id}",
                    },
                )
                if r.status_code not in (200, 201):
                    return _err(
                        f"GitHub API {r.status_code} on PR merge: "
                        f"{r.text[:200]}"
                    )
                merge_data = r.json()
                merge_sha = (
                    merge_data.get("sha")
                    or pr_data.get("merge_commit_sha")
                    or ""
                )

                # Delete remote ship branch (best-effort; non-fatal)
                try:
                    await client.delete(
                        f"{api_base}/git/refs/heads/{branch_name}",
                        headers=gh_headers,
                    )
                except Exception:
                    pass

        except httpx.HTTPError as exc:
            return _err(f"GitHub API request failed: {exc}")

        # Cleanup local ship branch (best-effort)
        try:
            await _git(["git", "checkout", caller_branch])
            await _git(["git", "branch", "-D", branch_name])
        except Exception:
            pass

        return await _complete_with_evidence(
            ship_sha=merge_sha,
            pr_number=pr_number,
            pr_url=pr_url,
            ship_method="resumed_pr" if resumed_branch else "pr",
            idempotent=resumed_branch,
        )

    @tool(
        "coord_write_decision",
        (
            "Coach-only. Append a dated, immutable architectural decision "
            "record to the cloud drive at <webdav-base>/projects/<active>/decisions/ "
            "(or /data/decisions/ if the cloud-drive mirror is disabled).\n"
            "\n"
            "Unlike memory (which is overwritable scratch), decisions are "
            "the durable 'we chose X because Y' record. Filename format: "
            "YYYY-MM-DD-<slug>.md. If a decision with the same slug for "
            "today already exists, a numeric suffix is appended.\n"
            "\n"
            "Params:\n"
            "- title: short human title (required; becomes the filename slug)\n"
            "- body: full markdown content (required; context, options, choice, rationale)"
        ),
        {"title": str, "body": str},
    )
    async def write_decision(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach writes decisions (durable architectural records). "
                "Players post findings to memory via coord_update_memory."
            )
        title = (args.get("title") or "").strip()
        body = (args.get("body") or "").strip()
        if not title:
            return _err("title is required")
        if not body:
            return _err("body is required (empty decisions are not useful)")
        if len(body) > 40_000:
            return _err(f"body too long ({len(body)} chars, max 40000)")

        # Slugify the title: lowercase, alphanumerics + dashes, max 48 chars.
        slug_raw = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        slug = slug_raw[:48].strip("-") or "decision"

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now_iso = _now_iso()
        base_filename = f"{today}-{slug}.md"

        frontmatter = (
            f"---\n"
            f"title: {title}\n"
            f"date: {today}\n"
            f"ts: {now_iso}\n"
            f"author: {caller_id}\n"
            f"---\n\n"
        )
        content = frontmatter + body + ("\n" if not body.endswith("\n") else "")

        # Prefer the cloud drive (the human-readable durable store). Fall
        # back to the local /data volume so offline agents still get a record.
        project_id = await resolve_active_project()
        from server.paths import project_paths
        location = None
        filename = base_filename
        if webdav.enabled:
            ok = await webdav.write_text(
                f"projects/{project_id}/decisions/{filename}", content
            )
            if ok:
                location = f"clouddrive:projects/{project_id}/decisions/{filename}"
        if location is None:
            # Local fallback when the cloud-drive mirror is disabled or write failed.
            local_dir = project_paths(project_id).decisions
            try:
                local_dir.mkdir(parents=True, exist_ok=True)
                # Collision check: append a numeric suffix if needed
                target = local_dir / filename
                n = 2
                while target.exists():
                    filename = f"{today}-{slug}-{n}.md"
                    target = local_dir / filename
                    n += 1
                target.write_text(content, encoding="utf-8")
                location = f"local:{target}"
            except Exception as e:
                return _err(f"decision write failed: {type(e).__name__}: {e}")

        await bus.publish(
            {
                "ts": now_iso,
                "agent_id": caller_id,
                "type": "decision_written",
                "title": title,
                "filename": filename,
                "location": location,
                "size": len(body),
            }
        )
        return _ok(
            f"decision '{title}' saved to {location} ({len(body)} chars of body)"
        )

    # Coach proposes edits to harness-managed files (truth/* and the
    # per-project CLAUDE.md) via `coord_propose_file_write` below; the
    # human reviews a diff and approves in the UI. The global
    # /data/CLAUDE.md is NOT proposeable — only the user edits the
    # harness-wide instructions. All these files are read fresh on
    # every agent turn (server/context.py).

    @tool(
        "coord_propose_file_write",
        (
            "Coach-only. Propose a write to a harness-managed file in "
            "the **currently active** project. The user reviews a diff "
            "and approves/denies in the UI's 'File-write proposals' "
            "section; on approve the harness writes the file. To "
            "target a different project, ask the user to switch the "
            "active project first.\n"
            "\n"
            "Two scopes:\n"
            "  - 'truth': `path` is relative under the active project's "
            "    `truth/` folder (e.g. 'specs.md', 'brand/colors.md'). "
            "    truth/ is the user's signed-off source-of-truth "
            "    (specs, brand guidelines, contracts); agents NEVER "
            "    write to it directly — the harness's PreToolUse guard "
            "    hook denies any direct Write/Edit/Bash to truth/.\n"
            "  - 'project_claude_md': `path` must be exactly 'CLAUDE.md'. "
            "    Targets `/data/projects/<active-slug>/CLAUDE.md` — the "
            "    project's instruction file, read fresh into every "
            "    agent's system prompt. Use this to keep the project's "
            "    Goal / Stakeholders / Team / Glossary / Conventions / "
            "    truth-section paragraphs current.\n"
            "\n"
            "Players cannot call this tool — they must ask Coach to "
            "relay. The global /data/CLAUDE.md (harness-wide) is NOT a "
            "valid scope; only the user edits that file directly.\n"
            "\n"
            "What happens:\n"
            "  1. The proposal is queued (status=pending).\n"
            "  2. ANY prior pending proposal for the same (scope, "
            "path) is auto-superseded — only your latest proposal is "
            "offered to the user for approval. So your new proposal "
            "must include EVERY change you still want for that file "
            "(the prior proposal's content is discarded). If unsure "
            "what's currently pending, look for "
            "`file_write_proposal_*` events in your timeline before "
            "composing.\n"
            "  3. The user reviews the diff in the UI and clicks "
            "approve or deny.\n"
            "  4. On approve, the harness writes the file with the "
            "proposed content. On deny, the file is left as-is.\n"
            "  5. A `file_write_proposal_resolved` event fires so "
            "you'll see the outcome on your next turn.\n"
            "\n"
            "Reorganizing the truth folder (e.g. splitting a growing "
            "specs.md into specs.md + architecture.md + scope.md): "
            "send the changes as a SERIES of proposals — (1) the new "
            "dependency files with their content, (2) the original "
            "file with extracted content removed, (3) "
            "`truth-index.md` updated to list the new files. Each is "
            "a separate call; the user approves them in order.\n"
            "\n"
            "Params:\n"
            "- scope: 'truth' or 'project_claude_md' (required).\n"
            "- path: scope-relative path. For 'truth': relative under "
            "  the active project's truth/ (e.g. 'specs.md' or "
            "  'brand/colors.md'; NOT 'projects/<slug>/...' or "
            "  '/data/...'). For 'project_claude_md': must be exactly "
            "  'CLAUDE.md'. Required.\n"
            "- content: full new file body (required). This is a full "
            "  REPLACE — include the parts you're keeping verbatim, "
            "  not just a diff. The user reviews a diff against the "
            "  current file content in the UI.\n"
            "- summary: one-line 'why' the user reads next to "
            "  approve/deny (required, ≤ 200 chars). Be specific: "
            "  'Add launch-date constraint to specs.md §3' beats "
            "  'update specs'."
        ),
        {"scope": str, "path": str, "content": str, "summary": str},
    )
    async def propose_file_write(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach can propose file writes. Players: send a "
                "coord_send_message to coach describing the proposed "
                "change; Coach will relay it as a proposal."
            )
        scope = (args.get("scope") or "").strip()
        rel = (args.get("path") or "").strip()
        content = args.get("content")
        summary = (args.get("summary") or "").strip()

        if scope not in ("truth", "project_claude_md"):
            return _err(
                f"scope must be 'truth' or 'project_claude_md' (got "
                f"{scope!r}). The global /data/CLAUDE.md is not a "
                "valid scope."
            )
        if not rel:
            return _err("path is required")

        if scope == "truth":
            # Defensive strip: Coach is told to pass paths relative to
            # truth/ (e.g. "specs.md", not "truth/specs.md"), but
            # accept the prefixed form too rather than fail
            # confusingly. Strip a single leading "truth/" so
            # "truth/specs.md" and "specs.md" both resolve to
            # /data/projects/<slug>/truth/specs.md.
            if rel.startswith("truth/"):
                rel = rel[len("truth/"):]
            if rel.startswith("/") or ".." in rel.split("/"):
                return _err(
                    "path must be relative under the active project's "
                    "truth/ folder, no leading slash, no '..' segments"
                )
            # Catch a recurrent Coach mistake: encoding the target
            # project slug in the path (e.g.
            # "projects/dynamichypergraph/CLAUDE.md"). truth/ is
            # rooted at the active project — there's no way to
            # cross-project from the path. Detect when the first
            # segment matches an existing project slug and tell Coach
            # to switch active project instead. Also catches a literal
            # "projects/" prefix even when the second segment isn't a
            # known slug.
            first_seg = rel.split("/", 1)[0]
            if first_seg == "projects":
                return _err(
                    f"path '{rel}' starts with 'projects/' — truth/ "
                    "is rooted at the active project's truth folder, "
                    "not anywhere under /data/projects/. To target a "
                    "different project, ask the user to switch the "
                    "active project first, then pass the path "
                    "relative to that project's truth/ (e.g. "
                    "'CLAUDE.md', not 'projects/<slug>/CLAUDE.md')."
                )
            c_slug_check = await configured_conn()
            try:
                cur = await c_slug_check.execute(
                    "SELECT 1 FROM projects WHERE id = ? LIMIT 1",
                    (first_seg,),
                )
                slug_match = await cur.fetchone()
            finally:
                await c_slug_check.close()
            if slug_match:
                return _err(
                    f"path '{rel}' starts with project slug "
                    f"'{first_seg}' — the path is rooted at the "
                    "*currently active* project's truth/ folder, not "
                    "anywhere under /data/projects/. To target the "
                    f"'{first_seg}' project, ask the user to switch "
                    "active project to it first, then pass the path "
                    "relative to its truth/ (e.g. drop the leading "
                    f"'{first_seg}/')."
                )
        else:
            # project_claude_md: the only legal path is the project's
            # CLAUDE.md at the project root. Reject anything else so a
            # malformed call can't write to a sibling file.
            if rel != "CLAUDE.md":
                return _err(
                    f"for scope 'project_claude_md', path must be "
                    f"exactly 'CLAUDE.md' (got {rel!r}). The target "
                    "is the active project's "
                    "/data/projects/<slug>/CLAUDE.md."
                )

        if not isinstance(content, str):
            return _err("content is required (string)")
        if len(content) > FILE_WRITE_PROPOSAL_MAX_CHARS:
            return _err(
                f"content too long ({len(content)} chars, "
                f"max {FILE_WRITE_PROPOSAL_MAX_CHARS})"
            )
        if not summary:
            return _err("summary is required (one-line 'why' the user reads)")
        if len(summary) > 200:
            return _err(f"summary too long ({len(summary)} chars, max 200)")

        project_id = await resolve_active_project()
        # Supersede + insert in one transaction so a crash mid-flight
        # leaves the table coherent (either both old superseded + new
        # pending, or no change at all). Auto-supersede guarantees the
        # invariant "at most one pending proposal per (project, scope,
        # path)" so the EnvPane never shows a stale stack of
        # duplicates. The scope filter is load-bearing: a hypothetical
        # truth/CLAUDE.md and a project_claude_md proposal at path
        # 'CLAUDE.md' must not supersede each other.
        now_iso = _now_iso()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id FROM file_write_proposals "
                "WHERE project_id = ? AND scope = ? AND path = ? "
                "AND status = 'pending'",
                (project_id, scope, rel),
            )
            superseded_ids = [row[0] for row in await cur.fetchall()]
            cur = await c.execute(
                "INSERT INTO file_write_proposals "
                "(project_id, proposer_id, scope, path, "
                "proposed_content, summary) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, caller_id, scope, rel, content, summary),
            )
            proposal_id = cur.lastrowid
            for sid in superseded_ids:
                await c.execute(
                    "UPDATE file_write_proposals SET status = 'superseded', "
                    "resolved_at = ?, resolved_by = 'system', "
                    "resolved_note = ? "
                    "WHERE id = ? AND status = 'pending'",
                    (now_iso, f"superseded by #{proposal_id}", sid),
                )
            await c.commit()
        finally:
            await c.close()

        for sid in superseded_ids:
            await bus.publish(
                {
                    "ts": now_iso,
                    "agent_id": "system",
                    "type": "file_write_proposal_superseded",
                    "proposal_id": sid,
                    "superseded_by": proposal_id,
                    "scope": scope,
                    "path": rel,
                }
            )
        await bus.publish(
            {
                "ts": now_iso,
                "agent_id": caller_id,
                "type": "file_write_proposal_created",
                "proposal_id": proposal_id,
                "scope": scope,
                "path": rel,
                "summary": summary,
                "size": len(content),
                "superseded": superseded_ids,
            }
        )
        if superseded_ids:
            note = (
                f" (superseded {len(superseded_ids)} prior pending "
                f"proposal{'s' if len(superseded_ids) > 1 else ''} for "
                f"the same path: #{', #'.join(str(i) for i in superseded_ids)})"
            )
        else:
            note = ""
        if scope == "truth":
            display_path = f"truth/{rel}"
        else:
            display_path = "CLAUDE.md"
        return _ok(
            f"proposal #{proposal_id} queued for {display_path} "
            f"({len(content)} chars, scope={scope}){note}. The user "
            f"will review and approve or deny in the UI; you'll see "
            f"`file_write_proposal_resolved` on your next turn."
        )

    @tool(
        "coord_read_file",
        (
            "Read a text file from the **currently active** project's "
            "tree at `/data/projects/<active-slug>/`. Available to all "
            "agents (Coach AND Players) — the read path bypasses the "
            "Codex sandbox / bwrap layer that breaks `shell cat` in "
            "nested-container deploys, so this is the canonical way "
            "for any agent to inspect project files at any time, "
            "even when their native read tool is unavailable.\n"
            "\n"
            "Path is RELATIVE to the active project's root. Examples: "
            "`'CLAUDE.md'`, `'truth/specs.md'`, `'decisions/0001-foo.md'`, "
            "`'working/knowledge/notes.md'`, `'outputs/report.md'`. "
            "Absolute paths and `..` segments are rejected.\n"
            "\n"
            "Project CLAUDE.md is also auto-injected into your system "
            "prompt every turn — calling this for `CLAUDE.md` is fine "
            "but redundant for read-only inspection. Use it when you "
            "want the latest body during a long turn (the system-"
            "prompt copy is frozen at turn start).\n"
            "\n"
            "Limits:\n"
            "- Returns up to 512 KB of file content; larger files are "
            "  rejected with a size error (use `coord_list_knowledge` / "
            "  the Files pane for an index of large trees).\n"
            "- Text-only: files that aren't valid UTF-8 are rejected. "
            "  Binary deliverables under `outputs/` cannot be read "
            "  through this tool.\n"
            "\n"
            "Params:\n"
            "- path: relative path under the active project root "
            "  (required, no leading slash, no `..`)."
        ),
        {"path": str},
    )
    async def read_file(args: dict[str, Any]) -> dict[str, Any]:
        rel = (args.get("path") or "").strip()
        if not rel:
            return _err("path is required")
        if rel.startswith("/"):
            return _err(
                "path must be relative under the active project's "
                "root (no leading slash)"
            )
        if ".." in rel.split("/"):
            return _err("path must not contain '..' segments")
        # Anchor + re-validate so a clever path that resolves outside
        # the project root (symlink, weird casing) is rejected even
        # if the literal-segment check above passes.
        from server.paths import project_paths
        project_id = await resolve_active_project()
        project_root = project_paths(project_id).root.resolve()
        target = (project_root / rel).resolve()
        try:
            target.relative_to(project_root)
        except ValueError:
            return _err(
                f"resolved path '{rel}' escapes the active project's "
                "root — refusing read"
            )
        if not target.exists():
            return _err(f"file not found: {rel}")
        if not target.is_file():
            return _err(f"not a regular file: {rel}")
        try:
            size = target.stat().st_size
        except OSError as e:
            return _err(f"stat failed: {e}")
        if size > COORD_READ_FILE_MAX_CHARS:
            return _err(
                f"file too large ({size} chars, "
                f"max {COORD_READ_FILE_MAX_CHARS}); use the "
                "Files pane or chunk via Read tool / shell"
            )
        try:
            body = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return _err(
                f"file is not valid UTF-8: {rel} (binary files cannot "
                "be read through this tool)"
            )
        except OSError as e:
            return _err(f"read failed: {e}")
        return _ok(
            f"file[{rel}] ({len(body)} chars):\n\n{body}"
        )

    @tool(
        "coord_list_team",
        (
            "Read the current team roster: slot id, name, role, brief, "
            "status, and currently-claimed task (if any) for every agent "
            "on the team (Coach + p1..p10).\n"
            "\n"
            "Useful at the start of a fresh turn to remember who's on "
            "the team, what they're working on, and what their domain "
            "is — agents don't carry cross-turn memory of this without "
            "reading it. No params."
        ),
        {},
    )
    async def list_team(args: dict[str, Any]) -> dict[str, Any]:
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            # JOIN agents with agent_project_roles for the active project
            # so the response carries this project's name/role/brief.
            cur = await c.execute(
                "SELECT a.id, a.kind, r.name AS name, r.role AS role, "
                "       r.brief AS brief, a.status, a.current_task_id, a.locked "
                "FROM agents a "
                "LEFT JOIN agent_project_roles r "
                "  ON r.slot = a.id AND r.project_id = ? "
                "ORDER BY CASE a.kind WHEN 'coach' THEN 0 ELSE 1 END, a.id",
                (project_id,),
            )
            rows = await cur.fetchall()
        finally:
            await c.close()
        if not rows:
            return _ok("(no agents in the roster — init_db never ran)")
        lines: list[str] = []
        for r in rows:
            d = dict(r)
            bits = [d["id"]]
            if d.get("name") and d["name"] != d["id"]:
                bits.append(d["name"])
            if d.get("role"):
                bits.append(f"({d['role']})")
            bits.append(f"· {d['status']}")
            if d.get("current_task_id"):
                bits.append(f"· on {d['current_task_id']}")
            # LOCKED marker: render loudly so the model skims it. The
            # enforcement is also at the tool layer, but telling Coach
            # up-front saves wasted turns trying to assign to a locked
            # slot.
            if d.get("locked"):
                bits.append("· 🔒 LOCKED (off-limits for Coach)")
            header = " ".join(bits)
            if d.get("brief"):
                # Keep it terse in the listing — full brief is retrievable
                # via /api/agents if needed.
                preview = str(d["brief"])[:140].replace("\n", " ")
                lines.append(f"{header}\n    brief: {preview}")
            else:
                lines.append(header)
        return _ok("\n".join(lines))

    @tool(
        "coord_set_player_role",
        (
            "Coach-only. Assign a Player their human-readable name and "
            "role description. Stored on the agents row so the UI can "
            "label the pane (e.g. 'p3 — Alice — Frontend developer').\n"
            "\n"
            "Re-callable: overwrites prior values. Pass empty strings to "
            "clear. Emits a 'player_assigned' event so the UI refreshes "
            "the LeftRail / pane header immediately.\n"
            "\n"
            "Params:\n"
            "- player_id: one of p1..p10 (required)\n"
            "- name: short human name like 'Alice' (required)\n"
            "- role: one-line role description (required)"
        ),
        {"player_id": str, "name": str, "role": str},
    )
    async def set_player_role(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach assigns Player names / roles.")
        pid = (args.get("player_id") or "").strip()
        # Squash any internal whitespace (newlines / tabs / multiple
        # spaces) to single spaces. name and role render inline in the
        # pane header — a multi-line name would break layout.
        def _single_line(s: str) -> str:
            return " ".join(s.split()).strip()
        name = _single_line(args.get("name") or "")
        role = _single_line(args.get("role") or "")
        if not re.fullmatch(r"p([1-9]|10)", pid):
            return _err(f"invalid player_id '{pid}' — expected p1..p10")
        if len(name) > 80:
            return _err(f"name too long ({len(name)} chars, max 80)")
        if len(role) > 300:
            return _err(f"role too long ({len(role)} chars, max 300)")

        # Verify the player slot exists in the global agents roster
        # before writing per-project identity.
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (pid,))
            if not await cur.fetchone():
                return _err(f"player '{pid}' not found")
            # Upsert into agent_project_roles for the active project.
            await c.execute(
                "INSERT INTO agent_project_roles (slot, project_id, name, role) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(slot, project_id) DO UPDATE SET "
                "  name = excluded.name, role = excluded.role",
                (pid, project_id, name or None, role or None),
            )
            await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "player_assigned",
                "player_id": pid,
                "name": name,
                "role": role,
            }
        )
        return _ok(f"{pid} → {name or '(no name)'} — {role or '(no role)'}")

    @tool(
        "coord_set_player_runtime",
        (
            "Coach-only. Flip a Player between the Claude and Codex "
            "runtimes WITH session transfer. The Player's current "
            "session (if any) is summarized via /compact on the source "
            "runtime, the runtime then flips, and the next turn on the "
            "new runtime reads that summary as a handoff in its system "
            "prompt. Continuity preserved across the flip without "
            "mid-conversation memory loss.\n"
            "\n"
            "Stored on `agents.runtime_override`; resolution at spawn "
            "time is per-slot override → role default in team_config → "
            "'claude'.\n"
            "\n"
            "Use this BEFORE coord_set_player_model when you want to set "
            "a model from the other runtime family — the model tool "
            "validates against the Player's currently-resolved runtime, "
            "so a Codex model on a Claude-runtime Player is rejected "
            "until you flip the runtime here first.\n"
            "\n"
            "An existing `model_override` is preserved across the flip "
            "(spawn-time silently drops it if it doesn't fit the new "
            "runtime, then re-applies it if you flip back). Set the "
            "new model explicitly via coord_set_player_model after "
            "flipping if you don't want fall-through to the role default.\n"
            "\n"
            "Codex requires the HARNESS_CODEX_ENABLED env flag — without "
            "it, runtime='codex' is rejected. Mid-turn flips are also "
            "rejected (the in-flight turn would be on the old runtime "
            "while subsequent turns use the new one); cancel the Player "
            "first if they're working.\n"
            "\n"
            "Behavior:\n"
            "- runtime equals current → no-op (returns ok with note='noop').\n"
            "- no prior session on the source runtime → flip is "
            "immediate; one 'runtime_updated' + one 'session_transferred' "
            "event with note='no_prior_session'.\n"
            "- has a prior session → a compact turn is queued on the "
            "current runtime. The runtime flips on compact success and "
            "'session_transferred' fires; if the compact returns no "
            "summary 'session_transfer_failed' fires and the runtime "
            "stays put. The MCP call returns IMMEDIATELY (queued=True) — "
            "watch the Player's pane for completion.\n"
            "- runtime='' (empty string) keeps the legacy blunt clear: "
            "writes runtime_override=NULL and emits 'runtime_updated'. "
            "No transfer/compact is run; use this only when you "
            "explicitly want a fresh start on the role default.\n"
            "\n"
            "Params:\n"
            "- player_id: one of p1..p10 (required; cannot flip Coach's runtime)\n"
            "- runtime: 'claude', 'codex', or empty string to clear "
            "(revert to the role default; blunt — no transfer)."
        ),
        {"player_id": str, "runtime": str},
    )
    async def set_player_runtime(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach flips Player runtimes.")
        pid = (args.get("player_id") or "").strip()
        raw = (args.get("runtime") or "").strip().lower()
        if not re.fullmatch(r"p([1-9]|10)", pid):
            return _err(
                f"invalid player_id '{pid}' — expected p1..p10 "
                "(cannot flip Coach's runtime via MCP; ask the human)"
            )
        if raw == "":
            runtime_value: str | None = None
        elif raw in ("claude", "codex"):
            runtime_value = raw
        else:
            return _err(
                f"invalid runtime '{args.get('runtime')}' — must be "
                "'claude', 'codex', or empty (clear)"
            )

        if runtime_value == "codex":
            from server.runtimes import is_codex_enabled
            if not is_codex_enabled():
                return _err(
                    "Codex runtime is gated behind HARNESS_CODEX_ENABLED. "
                    "The human must set that env var on the deployment "
                    "before any Player can run on Codex. Use "
                    "coord_request_human to ask."
                )

        # Mid-turn flip rejection mirrors PUT /api/agents/{id}/runtime —
        # an in-flight turn would be on the old runtime while subsequent
        # turns use the new one, leaving the timeline incoherent.
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status FROM agents WHERE id = ?", (pid,)
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"player '{pid}' not found")
            current_status = dict(row).get("status")
            if current_status == "working":
                return _err(
                    f"{pid} is mid-turn — cancel their turn first (or "
                    "wait for it to finish), then flip the runtime."
                )
        finally:
            await c.close()

        # Empty/clear path — keep the legacy blunt behavior. Coach asks
        # for "clear" when they explicitly want a fresh start with no
        # transfer (revert to role default + drop any continuity).
        if runtime_value is None:
            from server.agents import _set_runtime_override
            await _set_runtime_override(pid, None)
            await bus.publish(
                {
                    "ts": _now_iso(),
                    "agent_id": pid,
                    "type": "runtime_updated",
                    "player_id": pid,
                    "runtime_override": None,
                }
            )
            return _ok(f"{pid} runtime cleared (will use role default)")

        # Transfer path. Re-resolve the current runtime now that we
        # know we're moving to a concrete target.
        from server.agents import (
            _resolve_runtime_for,
            _get_session_id,
            _set_runtime_override,
            run_agent as _run_agent,
            COMPACT_PROMPT as _COMPACT_PROMPT,
        )
        from_runtime = await _resolve_runtime_for(pid)
        if from_runtime == runtime_value:
            return _ok(
                f"{pid} runtime is already {runtime_value} — no flip needed"
            )

        if from_runtime == "claude":
            prior = await _get_session_id(pid)
        else:
            from server.runtimes.codex import _get_codex_thread_id
            prior = await _get_codex_thread_id(pid)

        if not prior:
            # No prior session — flip immediately, emit the same event
            # pair the HTTP endpoint produces.
            await _set_runtime_override(pid, runtime_value)
            ts_iso = _now_iso()
            await bus.publish(
                {
                    "ts": ts_iso,
                    "agent_id": pid,
                    "type": "runtime_updated",
                    "player_id": pid,
                    "runtime_override": runtime_value,
                }
            )
            await bus.publish(
                {
                    "ts": ts_iso,
                    "agent_id": pid,
                    "type": "session_transferred",
                    "from_runtime": from_runtime,
                    "to_runtime": runtime_value,
                    "note": "no_prior_session",
                }
            )
            return _ok(
                f"{pid} runtime → {runtime_value} (no prior session — "
                "flipped directly)"
            )

        # Schedule the transfer-mode compact. The message handler in
        # the source runtime applies the flip on success. We use
        # asyncio.create_task here (not BackgroundTasks) because tool
        # callbacks have no FastAPI request context — the run_agent
        # coroutine self-cleans even when its parent task isn't awaited.
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": pid,
                "type": "session_transfer_requested",
                "from_runtime": from_runtime,
                "to_runtime": runtime_value,
            }
        )
        asyncio.create_task(
            _run_agent(
                pid,
                _COMPACT_PROMPT,
                compact_mode=True,
                transfer_to_runtime=runtime_value,
            )
        )
        return _ok(
            f"{pid} runtime transfer queued: {from_runtime} → "
            f"{runtime_value} via /compact (watch pane for "
            "session_transferred event)"
        )

    @tool(
        "coord_set_player_model",
        (
            "Coach-only. Set or clear the model a Player runs on. "
            "Stored as a per-(slot, project) override on "
            "`agent_project_roles.model_override`.\n"
            "\n"
            "Prefer TIER ALIASES over version-pinned ids — they survive "
            "model bumps without rewriting your overrides:\n"
            "  Claude runtime: 'latest_opus', 'latest_sonnet', "
            "'latest_haiku'\n"
            "  Codex runtime:  'latest_gpt' (top-tier), 'latest_mini'\n"
            "Concrete ids (e.g. 'claude-opus-4-7', 'gpt-5.4-mini') are "
            "still accepted for cases where you specifically need a "
            "version pin, but aliases should be your default.\n"
            "\n"
            "Resolution order at spawn time (highest first): per-pane "
            "human override → this Coach override → per-role team "
            "default → SDK default. The override is silently dropped "
            "if it doesn't match the Player's current runtime "
            "(Claude vs Codex), so a stale value never breaks a turn. "
            "Aliases are resolved to the current concrete id at spawn "
            "time, so a stored 'latest_sonnet' picks up the next "
            "Sonnet release without re-running the tool.\n"
            "\n"
            "Read the 'Model selection policy' section of your system "
            "prompt FIRST — model changes are the exception, not the "
            "rule. Pass an empty `model` to clear and revert to the "
            "role default. Emits an 'agent_model_set' event so the UI "
            "refreshes immediately.\n"
            "\n"
            "Params:\n"
            "- player_id: one of p1..p10 (required)\n"
            "- model: tier alias ('latest_opus' etc.) or concrete id. "
            "Empty string clears."
        ),
        {"player_id": str, "model": str},
    )
    async def set_player_model(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach sets Player models.")
        pid = (args.get("player_id") or "").strip()
        model = (args.get("model") or "").strip()
        if not re.fullmatch(r"p([1-9]|10)", pid):
            return _err(f"invalid player_id '{pid}' — expected p1..p10")

        # Lazy import to avoid a top-level cycle: server.agents already
        # imports from this module at startup.
        from server.agents import _resolve_runtime_for
        from server.models_catalog import (
            _CLAUDE_MODEL_WHITELIST,
            _CODEX_MODEL_WHITELIST,
            model_is_claude,
            model_is_codex,
        )

        if model:
            runtime = await _resolve_runtime_for(pid)
            whitelist = (
                _CODEX_MODEL_WHITELIST if runtime == "codex"
                else _CLAUDE_MODEL_WHITELIST
            )
            if model not in whitelist:
                # Distinguish "wrong runtime" (model is real, just on
                # the other family) from "typo" so Coach knows whether
                # to flip the runtime first or pick a different id.
                # Without this split Coach reads the rejection as
                # "harness blocked me" and stops, missing that the
                # fix is `runtime_override` not a different model id.
                other_runtime = (
                    "codex" if runtime == "claude" and model_is_codex(model)
                    else "claude" if runtime == "codex" and model_is_claude(model)
                    else None
                )
                if other_runtime is not None:
                    same_runtime_aliases = (
                        "latest_opus / latest_sonnet / latest_haiku"
                        if runtime == "claude"
                        else "latest_gpt / latest_mini"
                    )
                    return _err(
                        f"'{model}' is a {other_runtime} model, but {pid} "
                        f"is on the {runtime} runtime. Flip the runtime "
                        f"first via "
                        f"coord_set_player_runtime(player_id='{pid}', "
                        f"runtime='{other_runtime}'), then call "
                        f"coord_set_player_model again. Or pick a "
                        f"{runtime} model ({same_runtime_aliases}) to "
                        f"keep the current runtime."
                    )
                family = "Codex" if runtime == "codex" else "Claude"
                allowed = sorted(m for m in whitelist if m)
                return _err(
                    f"unknown {family} model '{model}' for {pid} "
                    f"(runtime={runtime}). Allowed: {', '.join(allowed)}"
                )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (pid,))
            if not await cur.fetchone():
                return _err(f"player '{pid}' not found")
            # Empty-string clear on a player that has never had a row
            # is a no-op — don't create an all-NULL orphan. Any other
            # combination (existing row OR setting a non-empty value)
            # falls through to the upsert.
            cur = await c.execute(
                "SELECT 1 FROM agent_project_roles "
                "WHERE slot = ? AND project_id = ?",
                (pid, project_id),
            )
            row_exists = await cur.fetchone() is not None
            if not model and not row_exists:
                pass  # nothing to clear
            else:
                await c.execute(
                    "INSERT INTO agent_project_roles "
                    "(slot, project_id, model_override) VALUES (?, ?, ?) "
                    "ON CONFLICT(slot, project_id) DO UPDATE SET "
                    "  model_override = excluded.model_override",
                    (pid, project_id, model or None),
                )
                await c.commit()
        finally:
            await c.close()

        # `to: pid` lets the UI fan-out machinery render this event in
        # the target Player's pane too — Coach changing my model is
        # context I want to see when watching p3, not just buried in
        # Coach's timeline. Mirrors the message_sent / task_assigned
        # shape that the existing fan-out filter already understands.
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "agent_model_set",
                "player_id": pid,
                "to": pid,
                "model": model,
            }
        )
        if model:
            return _ok(f"{pid} model override → {model}")
        return _ok(f"{pid} model override cleared (will use role default)")

    @tool(
        "coord_set_player_effort",
        (
            "Coach-only. Set or clear a Player's reasoning-effort tier. "
            "Stored on `agent_project_roles.effort_override` for the "
            "active project. Maps directly to the SDK's thinking-budget "
            "Literal (low / medium / high / max).\n"
            "\n"
            "Resolution order at spawn time (highest first): per-pane "
            "request value (the human's pane gear popover) → this "
            "Coach override → no override (SDK default).\n"
            "\n"
            "Effort is the EXCEPTION, not the rule. Default is no "
            "override — the SDK picks a sensible thinking budget. "
            "Bump up for genuinely hard reasoning ('high' for tricky "
            "design / refactoring; 'max' for one-shot deep analysis); "
            "stay default for execution work. Sustained 'high' / 'max' "
            "burns the Max-plan token budget fast — review your active "
            "overrides periodically.\n"
            "\n"
            "Params:\n"
            "- player_id: one of p1..p10 (required; cannot set Coach's "
            "  effort via MCP — ask the human).\n"
            "- effort: one of 'low' | 'medium' | 'high' | 'max'. Empty "
            "  string clears (revert to no override). Aliases accepted: "
            "  'med' → 'medium'. Numeric 1..4 also accepted (1=low … "
            "  4=max) for symmetry with the UI slider."
        ),
        {"player_id": str, "effort": str},
    )
    async def set_player_effort(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach sets Player effort tiers.")
        pid = (args.get("player_id") or "").strip()
        raw = str(args.get("effort") or "").strip().lower()
        if not re.fullmatch(r"p([1-9]|10)", pid):
            return _err(f"invalid player_id '{pid}' — expected p1..p10")
        effort_value: int | None
        if raw in ("", "default", "clear", "none"):
            effort_value = None
        elif raw in ("low", "1"):
            effort_value = 1
        elif raw in ("medium", "med", "2"):
            effort_value = 2
        elif raw in ("high", "3"):
            effort_value = 3
        elif raw in ("max", "maximum", "4"):
            effort_value = 4
        else:
            return _err(
                f"invalid effort '{args.get('effort')}' — expected one of "
                "'low' | 'medium' | 'high' | 'max' (or empty to clear)"
            )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (pid,))
            if not await cur.fetchone():
                return _err(f"player '{pid}' not found")
            cur = await c.execute(
                "SELECT 1 FROM agent_project_roles "
                "WHERE slot = ? AND project_id = ?",
                (pid, project_id),
            )
            row_exists = await cur.fetchone() is not None
            if effort_value is None and not row_exists:
                pass  # nothing to clear; don't create an all-NULL orphan
            else:
                await c.execute(
                    "INSERT INTO agent_project_roles "
                    "(slot, project_id, effort_override) VALUES (?, ?, ?) "
                    "ON CONFLICT(slot, project_id) DO UPDATE SET "
                    "  effort_override = excluded.effort_override",
                    (pid, project_id, effort_value),
                )
                await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "agent_effort_set",
                "player_id": pid,
                "to": pid,
                "effort": effort_value,
            }
        )
        if effort_value is None:
            return _ok(f"{pid} effort override cleared")
        label = _EFFORT_VALUE_LABELS[effort_value]
        return _ok(f"{pid} effort override → {label}")

    @tool(
        "coord_set_player_plan_mode",
        (
            "Coach-only. Set or clear a Player's plan-mode default. "
            "When plan mode is on, every spawn for the Player runs with "
            "permission_mode='plan' — the Player drafts an outline and "
            "uses ExitPlanMode to surface it for human review BEFORE "
            "touching tools. Stored on "
            "`agent_project_roles.plan_mode_override`.\n"
            "\n"
            "Resolution order at spawn time (highest first): per-pane "
            "request value (the human's pane gear popover) → this "
            "Coach override → off.\n"
            "\n"
            "Plan mode is a heavy constraint — every turn pauses for "
            "human approval. Use sparingly: only set it on Players doing "
            "destructive / hard-to-undo work where you want the human "
            "to review the approach first. For most work, leave it off "
            "and rely on per-turn coord_request_human / AskUserQuestion "
            "checkpoints instead.\n"
            "\n"
            "Params:\n"
            "- player_id: one of p1..p10 (required; cannot set Coach's "
            "  plan mode via MCP).\n"
            "- plan_mode: 'on' | 'off' to set explicitly. Empty string "
            "  clears (revert to no override → off unless the human "
            "  toggled it per-pane). Aliases: 'true'/'1'/'yes' → on, "
            "  'false'/'0'/'no' → off."
        ),
        {"player_id": str, "plan_mode": str},
    )
    async def set_player_plan_mode(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach sets Player plan-mode defaults.")
        pid = (args.get("player_id") or "").strip()
        raw = str(args.get("plan_mode") or "").strip().lower()
        if not re.fullmatch(r"p([1-9]|10)", pid):
            return _err(f"invalid player_id '{pid}' — expected p1..p10")
        plan_value: int | None
        if raw in ("", "default", "clear", "none"):
            plan_value = None
        elif raw in ("on", "true", "1", "yes", "y"):
            plan_value = 1
        elif raw in ("off", "false", "0", "no", "n"):
            plan_value = 0
        else:
            return _err(
                f"invalid plan_mode '{args.get('plan_mode')}' — expected "
                "'on' | 'off' (or empty to clear)"
            )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (pid,))
            if not await cur.fetchone():
                return _err(f"player '{pid}' not found")
            cur = await c.execute(
                "SELECT 1 FROM agent_project_roles "
                "WHERE slot = ? AND project_id = ?",
                (pid, project_id),
            )
            row_exists = await cur.fetchone() is not None
            if plan_value is None and not row_exists:
                pass
            else:
                await c.execute(
                    "INSERT INTO agent_project_roles "
                    "(slot, project_id, plan_mode_override) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(slot, project_id) DO UPDATE SET "
                    "  plan_mode_override = excluded.plan_mode_override",
                    (pid, project_id, plan_value),
                )
                await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "agent_plan_mode_set",
                "player_id": pid,
                "to": pid,
                "plan_mode": plan_value,
            }
        )
        if plan_value is None:
            return _ok(f"{pid} plan-mode override cleared")
        return _ok(f"{pid} plan-mode override → {'on' if plan_value else 'off'}")

    @tool(
        "coord_set_player_thinking",
        (
            "Coach-only. Set or clear a Player's extended-thinking flag. "
            "When on, every Claude-runtime spawn for the Player runs with "
            "Anthropic's extended-thinking enabled — the model spends a "
            "dedicated reasoning-budget phase (returning separate "
            "`thinking` content blocks) before its visible response. "
            "Stored on `agent_project_roles.thinking_override`.\n"
            "\n"
            "Bump-ladder position: this is the MIDDLE rung between "
            "effort and model-tier. When a Player keeps misfiring on "
            "the same kind of task (kind_fail_count >= 2): first bump "
            "via coord_set_player_effort; if that's already at max or "
            "doesn't help, flip thinking on here; only THEN bump the "
            "model tier via coord_set_player_model. Don't combine bumps "
            "in one step — you want to know which rung actually helped.\n"
            "\n"
            "Claude runtime only. Codex Players store the value but "
            "silently ignore it at spawn time (Codex has its own "
            "reasoning knob); for Codex Players, skip this rung and go "
            "directly to the model bump rung. The override survives a "
            "runtime flip, so a Player flipped Codex→Claude later picks "
            "it up automatically.\n"
            "\n"
            "Resolution order at spawn time (highest first): per-pane "
            "request value → this Coach override → off.\n"
            "\n"
            "Params:\n"
            "- player_id: one of p1..p10 (required; Coach has no MCP "
            "  surface for setting its own thinking — use the pane gear).\n"
            "- thinking: 'on' | 'off' to set explicitly. Empty string "
            "  clears (revert to no override → off unless the human "
            "  toggled it per-pane). Aliases: 'true'/'1'/'yes' → on, "
            "  'false'/'0'/'no' → off."
        ),
        {"player_id": str, "thinking": str},
    )
    async def set_player_thinking(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach sets Player thinking defaults.")
        pid = (args.get("player_id") or "").strip()
        raw = str(args.get("thinking") or "").strip().lower()
        if not re.fullmatch(r"p([1-9]|10)", pid):
            return _err(f"invalid player_id '{pid}' — expected p1..p10")
        thinking_value: int | None
        if raw in ("", "default", "clear", "none"):
            thinking_value = None
        elif raw in ("on", "true", "1", "yes", "y"):
            thinking_value = 1
        elif raw in ("off", "false", "0", "no", "n"):
            thinking_value = 0
        else:
            return _err(
                f"invalid thinking '{args.get('thinking')}' — expected "
                "'on' | 'off' (or empty to clear)"
            )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (pid,))
            if not await cur.fetchone():
                return _err(f"player '{pid}' not found")
            cur = await c.execute(
                "SELECT 1 FROM agent_project_roles "
                "WHERE slot = ? AND project_id = ?",
                (pid, project_id),
            )
            row_exists = await cur.fetchone() is not None
            if thinking_value is None and not row_exists:
                pass
            else:
                await c.execute(
                    "INSERT INTO agent_project_roles "
                    "(slot, project_id, thinking_override) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(slot, project_id) DO UPDATE SET "
                    "  thinking_override = excluded.thinking_override",
                    (pid, project_id, thinking_value),
                )
                await c.commit()
        finally:
            await c.close()

        # Surface a hint when the target is on Codex — value is stored
        # but no-ops until/unless the Player flips back to Claude. Not
        # an error: documented behavior, future-proof for flips.
        from server.agents import _resolve_runtime_for
        rt = await _resolve_runtime_for(pid)
        runtime_note = (
            " (note: stored but inert on Codex — Claude runtime only)"
            if rt == "codex" else ""
        )

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "agent_thinking_set",
                "player_id": pid,
                "to": pid,
                "thinking": thinking_value,
            }
        )
        if thinking_value is None:
            return _ok(f"{pid} thinking override cleared{runtime_note}")
        return _ok(
            f"{pid} thinking override → "
            f"{'on' if thinking_value else 'off'}{runtime_note}"
        )

    @tool(
        "coord_get_player_settings",
        (
            "Coach-only. Read the current per-Player overrides in one "
            "call: runtime, model, effort, plan-mode, thinking. For each Player "
            "the response shows BOTH the override value (what you set "
            "via coord_set_player_*) AND the resolved value (what the "
            "Player will actually run with on next spawn, after "
            "fall-through to role defaults).\n"
            "\n"
            "Use before changing settings — confirms what's already in "
            "place so you don't re-set what's already correct, and so "
            "you can see at a glance whether a Player has any active "
            "overrides at all.\n"
            "\n"
            "Params:\n"
            "- player_id: optional. One of p1..p10 to scope to a single "
            "  Player; omit for the whole roster (incl. Coach for "
            "  runtime/model — Coach has no effort/plan-mode override "
            "  surface)."
        ),
        {"player_id": str},
    )
    async def get_player_settings(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach reads team settings via MCP.")
        from server.agents import (
            _get_agent_identity,
            _get_role_default_model,
            _resolve_runtime_for,
        )
        from server.models_catalog import (
            resolve_model_alias,
            role_default_effort,
            role_default_plan_mode,
        )

        scope_id = (args.get("player_id") or "").strip()
        if scope_id and not re.fullmatch(r"(coach|p([1-9]|10))", scope_id):
            return _err(
                f"invalid player_id '{scope_id}' — expected p1..p10 or "
                "'coach' (or omit for the full roster)"
            )

        slots = [scope_id] if scope_id else (
            ["coach"] + [f"p{i}" for i in range(1, 11)]
        )

        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id, runtime_override FROM agents WHERE id IN ("
                + ",".join("?" * len(slots)) + ")",
                slots,
            )
            agent_rows = {dict(r)["id"]: dict(r) for r in await cur.fetchall()}
        finally:
            await c.close()

        out_rows: list[dict[str, Any]] = []
        for sid in slots:
            ident = await _get_agent_identity(sid) or {}
            runtime_override = (
                (agent_rows.get(sid, {}).get("runtime_override") or "").lower()
                or None
            )
            resolved_runtime = await _resolve_runtime_for(sid)
            model_override = ident.get("model_override")
            effort_override = ident.get("effort_override")
            plan_override = ident.get("plan_mode_override")
            thinking_override = ident.get("thinking_override")

            resolved_model = (
                model_override
                or await _get_role_default_model(sid, resolved_runtime)
                or None
            )
            if resolved_model:
                resolved_model = resolve_model_alias(resolved_model)

            out_rows.append({
                "slot": sid,
                "name": ident.get("name"),
                "runtime": {
                    "override": runtime_override,
                    "resolved": resolved_runtime,
                },
                "model": {
                    "override": model_override,
                    "resolved": resolved_model,
                },
                "effort": {
                    "override": (
                        _EFFORT_VALUE_LABELS.get(int(effort_override))
                        if effort_override is not None else None
                    ),
                    "resolved": _EFFORT_VALUE_LABELS.get(
                        role_default_effort(sid)
                        if effort_override is None
                        else int(effort_override)
                    ),
                },
                "plan_mode": {
                    "override": (
                        None if plan_override is None
                        else bool(int(plan_override))
                    ),
                    "resolved": (
                        bool(int(plan_override))
                        if plan_override is not None
                        else role_default_plan_mode(sid)
                    ),
                },
                "thinking": {
                    "override": (
                        None if thinking_override is None
                        else bool(int(thinking_override))
                    ),
                    # No role default — resolved == override or off.
                    "resolved": (
                        bool(int(thinking_override))
                        if thinking_override is not None
                        else False
                    ),
                    "runtime_active": resolved_runtime == "claude",
                },
            })

        # Render a compact text table — easier for Coach to scan than
        # raw JSON, and keeps the response under the SDK's per-tool
        # text limit even at full roster (~11 rows).
        lines = [
            "slot   name           runtime          model                          effort      plan   thinking",
            "-----  -------------  ---------------  -----------------------------  ----------  -----  --------",
        ]
        for r in out_rows:
            slot = (r["slot"] or "").ljust(5)
            name = (r.get("name") or "").ljust(13)[:13]
            ro = r["runtime"]["override"]
            rr = r["runtime"]["resolved"]
            rt_cell = (
                f"{ro} (override)" if ro
                else f"{rr} (default)"
            ).ljust(15)[:15]
            mo = r["model"]["override"]
            mr = r["model"]["resolved"] or "(SDK default)"
            md_cell = (
                f"{mo} (override)" if mo
                else f"{mr} (default)"
            ).ljust(29)[:29]
            ef_cell = (
                f"{r['effort']['override']} (override)"
                if r["effort"]["override"]
                else f"{r['effort']['resolved']} (default)"
            ).ljust(10)[:10]
            pm_cell = (
                "on" if r["plan_mode"]["override"] is True
                else "off" if r["plan_mode"]["override"] is False
                else ("on (default)" if r["plan_mode"]["resolved"] else "off (default)")
            )
            th_o = r["thinking"]["override"]
            th_active = r["thinking"].get("runtime_active", True)
            th_cell = (
                ("on" if th_o else "off") + ("" if th_active else " *codex")
                if th_o is not None
                else "off (default)"
            )
            lines.append(
                f"{slot}  {name}  {rt_cell}  {md_cell}  {ef_cell}  "
                f"{pm_cell:<5}  {th_cell}"
            )
        text = "\n".join(lines)
        return {"content": [{"type": "text", "text": text}]}

    @tool(
        "coord_answer_question",
        (
            "Coach-only. Resolve a pending AskUserQuestion from a Player. "
            "When a Player calls AskUserQuestion, their turn pauses and "
            "the question is routed to your inbox with a correlation_id; "
            "call this tool with that id plus your picks to unblock them.\n"
            "\n"
            "Params:\n"
            "- correlation_id: the id from the question message (required).\n"
            "- answers: object mapping each exact question text to the "
            "  selected option label. Example: "
            "  {'How should I format the output?': 'Summary', "
            "   'Which sections?': 'Introduction, Conclusion'}. "
            "  For multi-select, join labels with ', '. For free-text, use "
            "  the user's literal string."
        ),
        {"correlation_id": str, "answers": dict},
    )
    async def answer_question(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach answers Player questions. Players get answers "
                "back automatically when Coach resolves."
            )
        correlation_id = (args.get("correlation_id") or "").strip()
        answers = args.get("answers") or {}
        if not correlation_id:
            return _err("correlation_id is required")
        if not isinstance(answers, dict) or not answers:
            return _err("answers must be a non-empty object (question → label)")
        # Normalise all values to strings — SDK expects record<string,string>.
        clean: dict[str, str] = {}
        for k, v in answers.items():
            if not isinstance(k, str) or not k.strip():
                continue
            clean[k] = str(v) if v is not None else ""
        if not clean:
            return _err("no valid (question, answer) pairs in answers")
        from server import interactions as interactions_registry
        entry = interactions_registry.get(correlation_id)
        if entry is None or entry.kind != "question":
            return _err(
                f"correlation_id {correlation_id!r} not found, wrong kind, "
                "or already resolved / timed out"
            )
        ok = interactions_registry.resolve(correlation_id, clean)
        if not ok:
            return _err(
                f"correlation_id {correlation_id!r} already resolved / timed out"
            )
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "question_answered",
                "correlation_id": correlation_id,
                "route": "coach",
                "answer_keys": list(clean.keys()),
            }
        )
        return _ok(
            f"answered {correlation_id} ({len(clean)} keys). The Player "
            "resumes on their paused turn."
        )

    @tool(
        "coord_answer_plan",
        (
            "Coach-only. Resolve a pending ExitPlanMode from a Player. "
            "When a Player in plan mode calls ExitPlanMode, their turn "
            "pauses and the plan is routed to your inbox with a "
            "correlation_id; call this tool with that id plus your "
            "decision to unblock them.\n"
            "\n"
            "Params:\n"
            "- correlation_id: the id from the plan approval message "
            "(required).\n"
            "- decision: 'approve' | 'reject' | 'approve_with_comments' "
            "(required). `approve` lets the plan execute as-is. "
            "`reject` keeps the Player in plan mode and phrases your "
            "comments as 'approved, but revise to include X' so they "
            "revise and exit plan mode again. `approve_with_comments` "
            "lets the plan execute AND queues your comments as an inbox "
            "message the Player reads on their next turn.\n"
            "- comments: required for 'reject' and 'approve_with_comments', "
            "optional for 'approve'. Max 10k chars."
        ),
        # Raw JSON schema: correlation_id + decision required; comments
        # optional (only required for 'reject' and 'approve_with_comments').
        {
            "type": "object",
            "properties": {
                "correlation_id": {"type": "string"},
                "decision": {"type": "string"},
                "comments": {"type": "string"},
            },
            "required": ["correlation_id", "decision"],
        },
    )
    async def answer_plan(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach decides on Player plans. Players stay in "
                "plan mode until Coach resolves."
            )
        correlation_id = (args.get("correlation_id") or "").strip()
        decision = (args.get("decision") or "").strip().lower()
        comments = (args.get("comments") or "").strip()
        if not correlation_id:
            return _err("correlation_id is required")
        if decision not in ("approve", "reject", "approve_with_comments"):
            return _err(
                "decision must be 'approve', 'reject', or 'approve_with_comments'"
            )
        if decision in ("reject", "approve_with_comments") and not comments:
            return _err(
                f"'{decision}' requires non-empty comments explaining what "
                "to revise / keep in mind"
            )
        if len(comments) > 10_000:
            return _err(f"comments too long ({len(comments)} chars, max 10000)")
        from server import interactions as interactions_registry
        entry = interactions_registry.get(correlation_id)
        if entry is None or entry.kind != "plan":
            return _err(
                f"correlation_id {correlation_id!r} not found, wrong kind, "
                "or already resolved / timed out"
            )
        ok = interactions_registry.resolve(
            correlation_id,
            {"decision": decision, "comments": comments},
        )
        if not ok:
            return _err(
                f"correlation_id {correlation_id!r} already resolved / timed out"
            )
        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "plan_decided",
                "correlation_id": correlation_id,
                "route": "coach",
                "decision": decision,
                "has_comments": bool(comments),
            }
        )
        return _ok(
            f"plan {decision} for {correlation_id}. The Player resumes on "
            "their paused turn."
        )

    @tool(
        "coord_request_human",
        (
            "Escalate to the human operator. Use when stuck, blocked on a "
            "decision only the human can make, or when something looks "
            "wrong enough that the team should pause.\n"
            "\n"
            "Emits a high-visibility 'human_attention' event the UI surfaces "
            "prominently. Does NOT block the agent — you should still mark "
            "your task blocked / cancelled / done as appropriate.\n"
            "\n"
            "Params:\n"
            "- subject: short headline (required, max 200 chars)\n"
            "- body: longer explanation incl. what you tried (required)\n"
            "- urgency: 'normal' (default) or 'blocker' (whole-team gating)"
        ),
        {"subject": str, "body": str, "urgency": str},
    )
    async def request_human(args: dict[str, Any]) -> dict[str, Any]:
        subject = (args.get("subject") or "").strip()
        body = (args.get("body") or "").strip()
        urgency = (args.get("urgency") or "normal").strip().lower()
        if not subject:
            return _err("subject is required")
        if not body:
            return _err("body is required (explain what you tried)")
        if len(subject) > 200:
            return _err(f"subject too long ({len(subject)} chars, max 200)")
        if len(body) > 10_000:
            return _err(f"body too long ({len(body)} chars, max 10000)")
        if urgency not in ("normal", "blocker"):
            return _err("urgency must be 'normal' or 'blocker'")

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "human_attention",
                "subject": subject,
                "body": body,
                "urgency": urgency,
            }
        )
        return _ok(
            f"human notified ({urgency}): {subject}. "
            "Continue or pause your current task as appropriate."
        )

    @tool(
        "coord_set_project_objectives",
        (
            "Coach-only. Replace the active project's "
            "project-objectives.md — the human-authored north star "
            "injected into Coach's system prompt every turn and read "
            "by Compass as part of the truth corpus.\n"
            "\n"
            "Use this after the human replies to the empty-objectives "
            "bootstrap question, or when the operator explicitly asks "
            "you to revise the project objectives. Empty text clears "
            "the file. This is the runtime-neutral replacement for "
            "trying to use a Write/Edit tool from Coach.\n"
            "\n"
            "Params:\n"
            "- text: full replacement markdown body (required, max 100k chars)"
        ),
        {"text": str},
    )
    async def set_project_objectives(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach updates project objectives. Players: send "
                "a coord_send_message to coach if objectives need a "
                "revision."
            )
        text = args.get("text")
        if text is None:
            return _err("text is required")
        if not isinstance(text, str):
            return _err("text must be a string")
        project_id = await resolve_active_project()
        from server import coach_objectives as objectives_mod
        try:
            result = await objectives_mod.write_objectives(
                project_id,
                text,
                agent_id=caller_id,
                actor={"source": "mcp-tool", "agent_id": caller_id},
            )
        except (TypeError, ValueError) as exc:
            return _err(str(exc))
        return _ok(
            f"project objectives updated for {project_id} "
            f"({result['size']} chars)"
        )

    @tool(
        "coord_add_todo",
        (
            "Coach-only. Append a new entry to the project's "
            "coach-todos.md — the finite, strikeable backlog injected "
            "into your system prompt every turn (recurrence-specs.md "
            "§3.1).\n"
            "\n"
            "Use this for items YOU need to do in future turns: a "
            "follow-up to check, a small task too thin to assign to a "
            "Player, an objective-driven action to advance next time. "
            "DO NOT use it as a Player task board — that's `tasks` "
            "via coord_create_task.\n"
            "\n"
            "Params:\n"
            "- title: short imperative title (required)\n"
            "- description: optional free markdown, can span lines\n"
            "- due: optional 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MMZ'"
        ),
        {"title": str, "description": str, "due": str},
    )
    async def add_todo(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach manages coach-todos. Players: send a "
                "coord_send_message to coach if you want them to "
                "queue a follow-up."
            )
        title = (args.get("title") or "").strip()
        description = (args.get("description") or "").strip()
        due = (args.get("due") or "").strip() or None
        if not title:
            return _err("title is required")
        from server import coach_todos as todos_mod
        project_id = await resolve_active_project()
        try:
            todo = await todos_mod.add_todo(
                project_id, title=title,
                description=description, due=due,
            )
        except ValueError as e:
            return _err(str(e))
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": caller_id,
            "type": "coach_todo_added",
            "id": todo.id,
            "title": todo.title,
            "due": todo.due,
        })
        return _ok(
            f"todo {todo.id} added: {todo.title}"
            + (f" (due {todo.due})" if todo.due else "")
        )

    @tool(
        "coord_complete_todo",
        (
            "Coach-only. Mark a coach-todos.md entry done. The entry "
            "moves to working/coach-todos-archive.md (still readable "
            "via Read but not injected into the system prompt). The "
            "id is permanent — completed entries keep theirs.\n"
            "\n"
            "Params:\n"
            "- id: the t-N identifier from coach-todos.md (required)"
        ),
        {"id": str},
    )
    async def complete_todo(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach manages coach-todos.")
        tid = (args.get("id") or "").strip()
        if not tid:
            return _err("id is required (e.g. 't-3')")
        from server import coach_todos as todos_mod
        project_id = await resolve_active_project()
        try:
            todo = await todos_mod.complete_todo(project_id, tid)
        except KeyError as e:
            return _err(str(e))
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": caller_id,
            "type": "coach_todo_completed",
            "id": todo.id,
            "title": todo.title,
        })
        return _ok(f"todo {todo.id} completed: {todo.title}")

    @tool(
        "coord_update_todo",
        (
            "Coach-only. Edit a coach-todos.md entry in place. Pass "
            "only the fields you want to change. Useful when the "
            "scope of a planned action shifts or a deadline moves.\n"
            "\n"
            "Params:\n"
            "- id: the t-N identifier (required)\n"
            "- title: new title (optional)\n"
            "- description: new description (optional)\n"
            "- due: new 'YYYY-MM-DD' or empty string to clear"
        ),
        {"id": str, "title": str, "description": str, "due": str},
    )
    async def update_todo(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach manages coach-todos.")
        tid = (args.get("id") or "").strip()
        if not tid:
            return _err("id is required (e.g. 't-3')")
        # Distinguish "field omitted" from "field passed as empty string".
        # Pydantic-free MCP args mean we read the dict directly.
        kwargs: dict[str, Any] = {}
        if "title" in args:
            kwargs["title"] = args["title"]
        if "description" in args:
            kwargs["description"] = args["description"]
        if "due" in args:
            # Empty due clears the deadline.
            due_val = args.get("due")
            kwargs["due"] = None if (
                due_val is None or str(due_val).strip() == ""
            ) else str(due_val)
        if not kwargs:
            return _err("pass at least one of: title, description, due")
        from server import coach_todos as todos_mod
        project_id = await resolve_active_project()
        try:
            todo = await todos_mod.update_todo(
                project_id, tid, **kwargs,
            )
        except (KeyError, ValueError) as e:
            return _err(str(e))
        await bus.publish({
            "ts": _now_iso(),
            "agent_id": caller_id,
            "type": "coach_todo_updated",
            "id": todo.id,
            "fields": list(kwargs.keys()),
        })
        return _ok(f"todo {todo.id} updated: {todo.title}")

    @tool(
        "coord_set_tick_interval",
        (
            "Coach-only. Throttle the active project's recurring tick "
            "up or down. The tick wakes you to walk the priority list "
            "(inbox / kanban / coach-todos / objectives) — cadence is "
            "yours to manage based on how active the team is "
            "(recurrence-specs.md §7.6).\n"
            "\n"
            "Tick fires only when you're idle. A scheduled fire that "
            "arrives mid-turn does NOT skip — it waits for you to "
            "finish, then fires once. The cadence is the *minimum gap* "
            "between tick fires, not a wall-clock alarm.\n"
            "\n"
            "When to call:\n"
            "- Throttle DOWN (e.g. minutes=15 or 30) when steady-state "
            "  and recent ticks hit empty branches. Saves spend.\n"
            "- Throttle UP (minutes=1) when actively orchestrating, "
            "  monitoring a deploy, chasing a stall.\n"
            "- minutes=0 means 'fire continuously as soon as I'm "
            "  idle' — power-user mode. Remember to throttle back "
            "  down once the burst is over so the daily cap doesn't "
            "  burn on idle ticks.\n"
            "\n"
            "If no tick row exists yet, this creates one. Setting "
            "minutes on a disabled row re-enables it (matches "
            "/tick N semantics).\n"
            "\n"
            "Params:\n"
            "- minutes: integer >= 0. Required unless `enabled` is "
            "  passed alone.\n"
            "- enabled: 'on' | 'off' to toggle without changing "
            "  cadence. Aliases: 'true'/'1'/'yes' → on, "
            "  'false'/'0'/'no' → off. Empty string or omitted = "
            "  no change to enabled state.\n"
            "- end_date: optional ISO 8601 UTC datetime string. When "
            "  the wall-clock reaches end_date the tick auto-disables "
            "  and emits recurrence_expired. Must be in the future.\n"
            "- max_fires: optional int >= 1. Auto-disables the tick "
            "  after this many successful fires and emits "
            "  recurrence_expired."
        ),
        {"minutes": int, "enabled": str, "end_date": str, "max_fires": int},
    )
    async def set_tick_interval(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "Only Coach throttles the recurring tick. Players: "
                "send a coord_send_message to coach if you want a "
                "different cadence."
            )

        minutes_raw = args.get("minutes", None)
        enabled_raw = args.get("enabled", None)
        end_date_raw = args.get("end_date", None)
        max_fires_raw = args.get("max_fires", None)

        minutes_value: int | None = None
        if minutes_raw is not None and str(minutes_raw).strip() != "":
            try:
                minutes_value = int(minutes_raw)
            except (TypeError, ValueError):
                return _err(
                    f"invalid minutes {minutes_raw!r} — expected "
                    "non-negative integer"
                )
            if minutes_value < 0:
                return _err("minutes cannot be negative (use 0 for continuous)")

        enabled_value: bool | None = None
        if enabled_raw is not None and str(enabled_raw).strip() != "":
            ev = str(enabled_raw).strip().lower()
            if ev in ("on", "true", "1", "yes", "enable", "enabled"):
                enabled_value = True
            elif ev in ("off", "false", "0", "no", "disable", "disabled"):
                enabled_value = False
            else:
                return _err(
                    f"invalid enabled {enabled_raw!r} — expected "
                    "'on' | 'off'"
                )

        end_date_value: str | None = None
        if end_date_raw is not None and str(end_date_raw).strip():
            end_date_value = str(end_date_raw).strip()

        max_fires_value: int | None = None
        if max_fires_raw is not None and str(max_fires_raw).strip() != "":
            try:
                max_fires_value = int(max_fires_raw)
            except (TypeError, ValueError):
                return _err(
                    f"invalid max_fires {max_fires_raw!r} — expected "
                    "positive integer >= 1"
                )
            if max_fires_value < 1:
                return _err("max_fires must be >= 1")

        if minutes_value is None and enabled_value is None:
            return _err("pass at least one of: minutes, enabled")

        from server.recurrences import upsert_tick
        project_id = await resolve_active_project()
        try:
            row = await upsert_tick(
                project_id=project_id,
                minutes=minutes_value,
                enabled=enabled_value,
                end_date=end_date_value,
                max_fires=max_fires_value,
                created_by="coach",
            )
        except ValueError as exc:
            return _err(str(exc))

        if row is None:
            return _ok("no tick row exists yet, and enabled=off was a no-op")
        cadence = row["cadence"]
        if not row["enabled"]:
            return _ok(f"tick disabled (cadence preserved: every {cadence} min)")
        suffix = ""
        if row.get("end_date"):
            suffix += f"; expires at {row['end_date']}"
        if row.get("max_fires"):
            suffix += f"; max {row['max_fires']} fires"
        if str(cadence) == "0":
            return _ok(f"tick set: continuous (fires as soon as Coach is idle){suffix}")
        return _ok(f"tick set: every {cadence} min{suffix}")

    # ============================================================
    # Compass tools — Coach-only strategy-engine surface.
    # All four reject Player calls with the same canonical message
    # so coach-only behavior is discoverable from any error reply.
    # Each tool also rejects when Compass is disabled for the active
    # project (team_config['compass_enabled_<id>'] not truthy).
    # ============================================================

    async def _compass_gate(project_id: str) -> str | None:
        """Return None if Compass is enabled for this project, else
        an error message. Lazy import to dodge the tools↔compass.api
        back-edge."""
        if not caller_is_coach:
            return (
                "Compass tools are Coach-only. "
                "Players read Compass via the CLAUDE.md block."
            )
        from server.compass import config as cmp_config  # noqa: PLC0415

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
        if val.strip().lower() not in ("1", "true", "yes"):
            return (
                "Compass is disabled for this project. "
                "Enable it from the Compass dashboard before using "
                "compass_ask / compass_audit / compass_brief / compass_status."
            )
        return None

    @tool(
        "compass_ask",
        (
            "Coach-only. Interrogate Compass on any project topic. "
            "Compass answers based strictly on its lattice and "
            "truth-protected facts, citing statement ids and weights. "
            "Treats >0.8 as confirmed YES, <0.2 as confirmed NO "
            "(negation is binding), 0.4–0.6 as genuinely uncertain. "
            "Read-only — does not modify the lattice.\n"
            "\n"
            "Params:\n"
            "- query: free-text question, e.g. "
            "'should we build for usage or flat pricing?'"
        ),
        {"query": str},
    )
    async def compass_ask(args: dict[str, Any]) -> dict[str, Any]:
        query_text = (args.get("query") or "").strip()
        if not query_text:
            return _err("query is required")
        project_id = await resolve_active_project()
        gate = await _compass_gate(project_id)
        if gate:
            return _err(gate)
        from server.compass import llm as cmp_llm  # noqa: PLC0415
        from server.compass import prompts as cmp_prompts  # noqa: PLC0415
        from server.compass import store as cmp_store  # noqa: PLC0415

        # load_with_meta — anchors the prompt on the project's
        # identity so harness chatter (player slot names, model
        # overrides) doesn't pollute Coach's view via compass_ask.
        state = await cmp_store.load_with_meta(project_id)
        try:
            res = await cmp_llm.call(
                cmp_prompts.COACH_QUERY_SYSTEM,
                cmp_prompts.coach_query_user(state, query_text),
                project_id=project_id,
                label="compass:ask",
            )
        except Exception as e:
            return _err(f"compass_ask LLM call failed: {type(e).__name__}: {e}")
        body = (res.text or "").strip()
        if not body:
            return _ok("(Compass had no response — lattice may be empty.)")
        return _ok(body)

    @tool(
        "coord_run_truth_score",
        (
            "Score the active project's current state against its "
            "`truth/` corpus on five canonical 1-10 criteria — Fidelity "
            "(impl matches spec), Completeness (truth's commitments are "
            "realized), Consistency (decisions/knowledge/outputs agree "
            "with truth), Currency (truth is up-to-date with what exists), "
            "and Clarity (truth is specific enough to score against). "
            "Returns the per-axis scores, the overall mean, a 2-4-sentence "
            "comment, and the path to a result file written under "
            "working/knowledge/. One-shot Sonnet call (~$0.10-0.20). "
            "Available to Coach AND every Player — use it as a "
            "self-check before shipping or to verify alignment after a "
            "non-trivial change.\n"
            "\n"
            "Params:\n"
            "- commentary (optional): free-text scoring directives "
            "honored literally. Use to focus or skip parts of the corpus, "
            "e.g. 'skip section 2', 'weight fidelity higher'. Score-"
            "manipulation directives ('score 10 on everything') comply "
            "but get flagged in the comment with [CALLER-OVERRIDE: ...]."
        ),
        {"commentary": str | None},
    )
    async def coord_run_truth_score(args: dict[str, Any]) -> dict[str, Any]:
        from server import truthscore as ts_mod  # noqa: PLC0415

        project_id = await resolve_active_project()
        if not project_id:
            return _err("no active project")
        commentary_raw = args.get("commentary")
        commentary = (
            commentary_raw.strip() if isinstance(commentary_raw, str) else ""
        ) or None
        actor = {"source": "mcp-tool", "agent_id": caller_id}
        try:
            result = await ts_mod.run_truth_score(project_id, commentary, actor)
        except ts_mod.TruthScoreError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"truthscore failed: {type(e).__name__}: {e}")
        # Render a compact markdown block — Coach / Player is reading this.
        scores = result.get("scores", {})
        lines = [
            f"**Overall: {result.get('overall', '?')} / 10**",
            "",
            "| Criterion     | Score |",
            "|---------------|-------|",
        ]
        for k in ("fidelity", "completeness", "consistency", "currency", "clarity"):
            v = scores.get(k, "?")
            lines.append(f"| {k.capitalize():<13} | {v}/10  |")
        lines.append("")
        lines.append(result.get("comment", ""))
        if result.get("result_path"):
            lines.append("")
            lines.append(f"_Result file: `{result['result_path']}`_")
        if result.get("fetch_warning"):
            lines.append("")
            lines.append(
                f"_Note: scored against cached `origin/main` "
                f"(fetch failed: {result['fetch_warning']})._"
            )
        return _ok("\n".join(lines))

    @tool(
        "compass_audit",
        (
            "Coach-only. Submit a work artifact (commit message, "
            "decision, worker output, design choice) for audit "
            "against the lattice. Compass returns one of three "
            "verdicts: 'aligned' (proceed silently), "
            "'confident_drift' (work clearly contradicts a high-"
            "confidence statement), or 'uncertain_drift' (work "
            "seems off but the relevant statements are mid-weight; "
            "a question is queued for the human). Audits are "
            "advisory — they never block work.\n"
            "\n"
            "Params:\n"
            "- artifact: the work to audit, e.g. "
            "'worker-4 implemented per-second billing instead of "
            "per-task as originally scoped'"
        ),
        {"artifact": str},
    )
    async def compass_audit(args: dict[str, Any]) -> dict[str, Any]:
        artifact = (args.get("artifact") or "").strip()
        if not artifact:
            return _err("artifact is required")
        project_id = await resolve_active_project()
        gate = await _compass_gate(project_id)
        if gate:
            return _err(gate)
        from server.compass import audit as cmp_audit  # noqa: PLC0415

        try:
            verdict = await cmp_audit.audit_work(project_id, artifact)
        except Exception as e:
            return _err(f"compass_audit failed: {type(e).__name__}: {e}")
        # Render as a compact markdown block — Coach is reading this.
        lines = [f"**Verdict:** `{verdict['verdict']}`"]
        if verdict.get("summary"):
            lines.append(f"**Summary:** {verdict['summary']}")
        if verdict.get("contradicting_ids"):
            lines.append(
                "**Contradicts:** " + ", ".join(verdict["contradicting_ids"])
            )
        if verdict.get("message_to_coach"):
            lines.append("")
            lines.append(verdict["message_to_coach"])
        if verdict.get("question_id"):
            lines.append("")
            lines.append(
                f"_A question for the human has been queued ({verdict['question_id']})._"
            )
        return _ok("\n".join(lines))

    @tool(
        "compass_brief",
        (
            "Coach-only. Fetch the most recent daily briefing — a "
            "structured markdown digest with sections: CONFIRMED "
            "YES, CONFIRMED NO, LEANING, OPEN, COVERAGE, DRIFT, "
            "RECOMMENDATION. Read-only. If no briefing has been "
            "generated yet (fresh project pre-bootstrap), returns "
            "an explanatory placeholder."
        ),
        {},
    )
    async def compass_brief(args: dict[str, Any]) -> dict[str, Any]:
        del args  # no-arg tool; signature is part of the SDK contract
        project_id = await resolve_active_project()
        gate = await _compass_gate(project_id)
        if gate:
            return _err(gate)
        from server.compass import store as cmp_store  # noqa: PLC0415

        text = cmp_store.latest_briefing_text(project_id)
        if not text:
            return _ok(
                "_No Compass briefing exists for this project yet. "
                "Either Compass hasn't run, or this is a bootstrap-only "
                "project. The dashboard's RUN button generates one._"
            )
        return _ok(text)

    @tool(
        "compass_status",
        (
            "Coach-only. Quick status snapshot of Compass for the "
            "active project. Returns counts of active and archived "
            "statements, regions, pending questions, pending "
            "settle/stale/dupe proposals, and the timestamp of the "
            "last run + last briefing. Read-only."
        ),
        {},
    )
    async def compass_status(args: dict[str, Any]) -> dict[str, Any]:
        del args  # no-arg tool; signature is part of the SDK contract
        project_id = await resolve_active_project()
        gate = await _compass_gate(project_id)
        if gate:
            return _err(gate)
        from server.compass import config as cmp_config  # noqa: PLC0415
        from server.compass import store as cmp_store  # noqa: PLC0415

        state = cmp_store.load_state(project_id)
        briefing_dates = cmp_store.list_briefing_dates(project_id)
        c2 = await configured_conn()
        try:
            cur = await c2.execute(
                "SELECT value FROM team_config WHERE key = ?",
                (cmp_config.last_run_key(project_id),),
            )
            row = await cur.fetchone()
        finally:
            await c2.close()
        last_run = (dict(row).get("value") if row else "") or "(never)"
        active = state.active_statements()
        archived = state.archived_statements()
        regions = [r.name for r in state.active_regions()]
        pending_qs = [
            q for q in state.questions
            if not q.digested and not q.contradicted and not q.ambiguity_accepted
        ]
        unanswered_qs = [q for q in pending_qs if q.answer is None]

        lines = [
            "**Compass status**",
            f"- active statements: {len(active)}",
            f"- archived: {len(archived)}",
            f"- regions ({len(regions)}): "
            + (", ".join(regions) if regions else "—"),
            f"- pending questions: {len(pending_qs)} "
            f"({len(unanswered_qs)} unanswered)",
            f"- pending settle proposals: {len(state.settle_proposals)}",
            f"- pending stale proposals: {len(state.stale_proposals)}",
            f"- pending dupe proposals: {len(state.duplicate_proposals)}",
            f"- last run: {last_run}",
            f"- last briefing: {briefing_dates[0] if briefing_dates else '(none)'}",
        ]
        return _ok("\n".join(lines))

    @tool(
        "coord_check_compass_audit",
        (
            "Coach-only. Snapshot of the Compass auto-audit watcher's "
            "runtime state. Answers \"is the watcher subscribed? when "
            "did it last fire per project? when did it last skip and "
            "why?\". Use when no audit verdicts have surfaced after "
            "plan→execute transitions and you need to tell "
            "healthy-and-quiet from sick-and-silent. Read-only."
        ),
        {},
    )
    async def coord_check_compass_audit(args: dict[str, Any]) -> dict[str, Any]:
        del args  # no-arg tool
        if not caller_is_coach:
            return _err(
                "coord_check_compass_audit is Coach-only. Compass "
                "audit telemetry isn't surfaced to Players."
            )
        from server.compass import audit_watcher as cmp_audit_watcher  # noqa: PLC0415
        snap = cmp_audit_watcher.snapshot_health()
        lines = [
            "**Compass auto-audit watcher**",
            f"- enabled (config): {snap.get('enabled')}",
            f"- running (live):   {snap.get('running')}",
            f"- watched events:   {', '.join(snap.get('watched_event_types') or [])}",
            f"- debounce window:  {snap.get('debounce_seconds')}s",
            f"- debounce keys:    {snap.get('debounce_keys_active')}",
        ]
        last_fire = snap.get("last_fire_by_project") or {}
        if last_fire:
            lines.append("- last fire per project:")
            for pid, ts in sorted(last_fire.items()):
                lines.append(f"    {pid}: {ts}")
        else:
            lines.append("- last fire per project: (none yet this process)")
        last_skip = snap.get("last_skip_by_project") or {}
        if last_skip:
            lines.append("- last skip per project:")
            for pid, info in sorted(last_skip.items()):
                lines.append(
                    f"    {pid}: {info.get('reason')} at {info.get('ts')} "
                    f"(task={info.get('task_id')})"
                )
        else:
            lines.append("- last skip per project: (none)")
        return _ok("\n".join(lines))

    # ====================================================================
    # Playbook (Docs/playbook-specs.md §7.1) — Coach-only mid-turn
    # proposal tool. Players read the lattice via system-prompt
    # injection but cannot influence it.
    @tool(
        "coord_propose_playbook_changes",
        (
            "Coach-only. Propose changes to the harness-wide "
            "orchestration playbook from inside any normal turn. The "
            "playbook is a weighted lattice of conceptual patterns "
            "about how to coordinate the team — readable to all "
            "agents (it appears as `## Orchestration playbook` in "
            "every system prompt), writable only by Coach via this "
            "tool or the daily reflection run.\n"
            "\n"
            "Use when you observe a load-bearing pattern in real time "
            "and don't want to wait for the daily reflection. For "
            "routine evolution, the daily run handles updates from "
            "evidence (archived tasks, audit fails, stalls) "
            "automatically — don't duplicate that here.\n"
            "\n"
            "Params:\n"
            "- operations: list of up to 5 op dicts. Each one of:\n"
            "    {'op': 'adjust', 'id': 'pb-XXX', 'delta': float, "
            "'reason': str}\n"
            "    {'op': 'create', 'text': str, 'weight': float, "
            "'reason': str}\n"
            "    {'op': 'merge', 'keep_id': 'pb-XXX', 'drop_id': "
            "'pb-XXX', 'reason': str}\n"
            "  Adjust delta is capped at ±0.25 per single proposal. "
            "Create weight defaults to 0.60 (mid-cycle creation) if "
            "omitted. Merge drops the second id into the first; "
            "kept weight = max of the two.\n"
            "  Create `text` is capped at 160 chars — one line, "
            "imperative, no enumerated sub-items. Rationale goes in "
            "the prose corpus, not the lattice."
        ),
        {"operations": list},
    )
    async def propose_playbook_changes(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "coord_propose_playbook_changes is Coach-only. "
                "Players read the playbook in their system prompt; "
                "they cannot influence it."
            )
        ops_raw = args.get("operations")
        if not isinstance(ops_raw, list):
            return _err("operations must be a list")
        if len(ops_raw) == 0:
            return _err("at least one operation required")
        from server.playbook import config as pb_config  # noqa: PLC0415
        if len(ops_raw) > pb_config.COACH_PROPOSAL_OPS_CAP:
            return _err(
                f"too many operations (got {len(ops_raw)}, cap "
                f"{pb_config.COACH_PROPOSAL_OPS_CAP}) — split across "
                f"multiple turns"
            )

        # Acquire the lock non-blocking. On contention, return the
        # canonical "playbook engine busy" string so Coach can
        # pattern-match and retry next turn.
        from server.playbook import mutate as pb_mutate  # noqa: PLC0415
        from server.playbook import runner as pb_runner  # noqa: PLC0415
        from server.playbook.store import (  # noqa: PLC0415
            load_archive,
            load_lattice,
            save_archive,
            save_lattice,
        )

        if pb_runner._run_lock.locked():
            return _ok(
                "playbook engine busy — another run is in flight. "
                "Retry on your next turn; no changes applied."
            )

        await pb_runner._run_lock.acquire()
        try:
            lattice = load_lattice()
            archive = load_archive()
            applied, rejected, hard_cap_hit = pb_mutate.apply_coach_proposals(
                lattice, archive, ops_raw,
                creation_weight=pb_config.COACH_CREATION_WEIGHT,
            )
            if applied:
                await save_lattice(lattice)
                await save_archive(archive)
        finally:
            pb_runner._run_lock.release()

        # Bus event for the dashboard's live counter (§9). Skip when
        # nothing applied so the dashboard isn't pinged with empty
        # 0-op announcements (e.g. all proposals rejected).
        if applied:
            try:
                from server.events import bus  # noqa: PLC0415

                await bus.publish({
                    "ts": _now_iso(),
                    "agent_id": "coach",
                    "type": "playbook_changes_applied",
                    "operations_count": len(applied),
                    "source": "coach_mid_turn",
                })
            except Exception:
                pass

        # Render the human-readable summary (§7.1 return shape).
        lines = []
        total_lines = []
        if applied:
            lines.append(f"Applied {len(applied)} of {len(ops_raw)} proposed changes:")
            for op in applied:
                if op.get("op") == "adjust":
                    f = op.get("from")
                    t = op.get("to")
                    delta = op.get("delta")
                    delta_s = (
                        f" ({'+' if isinstance(delta, (int, float)) and delta >= 0 else ''}{delta:.2f})"
                        if isinstance(delta, (int, float)) else ""
                    )
                    lines.append(
                        f"  - adjust {op.get('id')}: "
                        f"{f:.2f} → {t:.2f}{delta_s} — \"{op.get('reason') or ''}\""
                    )
                elif op.get("op") == "create":
                    lines.append(
                        f"  - create {op.get('new_id')}: weight "
                        f"{op.get('weight'):.2f} — \"{op.get('text') or ''}\""
                    )
                elif op.get("op") == "merge":
                    lines.append(
                        f"  - merge: dropped {op.get('drop_id')} into "
                        f"{op.get('keep_id')} — \"{op.get('reason') or ''}\""
                    )
        else:
            lines.append(f"Applied 0 of {len(ops_raw)} proposed changes.")
        if rejected:
            for op in rejected:
                op_kind = op.get("op", "?")
                ident = op.get("id") or op.get("keep_id") or "(new)"
                lines.append(
                    f"  - REJECTED {op_kind} {ident}: "
                    f"{op.get('reason', 'unknown_error')}"
                )
        if hard_cap_hit:
            lines.append(
                "  ! hard cap pressure — all proposed creations dropped; "
                "operator must review the lattice."
            )

        # Active count footer.
        from server.playbook.store import load_lattice as _ll  # noqa: PLC0415
        n_active = len(_ll().statements)
        lines.append(
            f"\nActive statement count: {n_active} / "
            f"{pb_config.SOFT_STATEMENT_CAP} soft cap."
        )
        return _ok("\n".join(lines))

    # ====================================================================
    # Kanban tools (Docs/kanban-specs.md). The state-machine + existing
    # tool updates (claim_task, assign_task, update_task, commit_push)
    # live above; the new tools below add the role-assignment surface
    # (planner / auditor / shipper), the spec/audit artifact writers,
    # and the introspection / meta knobs.
    # ====================================================================

    @tool(
        "coord_write_task_spec",
        (
            "**Your message to Coach that the planner work is done.** "
            "Calling this tool IS the act of submitting your spec to "
            "Coach — without it, Coach has no idea you've drafted "
            "anything, and the kanban can't record the spec. Writing "
            "spec.md to disk without calling this is silence; the "
            "disk-write + skipped-call pattern is the #1 stall cause.\n"
            "\n"
            "Writes spec.md to the task's working dir + emits "
            "`task_spec_written` to the per-project event log AND "
            "wakes Coach in real time (v2 §7.2.1) with your "
            "message_to_coach as the wake reason. Required before a "
            "task whose trajectory includes `plan` can move "
            "plan→execute. Trajectories without `plan` skip the spec "
            "gate.\n"
            "\n"
            "Permission: Coach can spec any task (emergency override). A "
            "Player can spec a task if they (a) have an active planner "
            "role assignment, (b) are the executor (re-spec during a "
            "fail loop), or (c) it's a subtask of their current task. "
            "By policy Coach should always delegate planning — see the "
            "lifecycle-policy block for the steer.\n"
            "\n"
            "Override (Coach-only, v0.3.5): when an assigned planner "
            "Player cannot reach this tool from their runtime — they "
            "drafted spec.md to disk and stopped because coord_* "
            "wasn't visible — Coach can register the spec on their "
            "behalf by passing `on_behalf_of='<player_slot>'`. The "
            "recorded spec_author is the named Player and that "
            "Player's planner role row is marked complete; the bus "
            "event's actor is Coach. Use this only after confirming "
            "the Player wrote the body (read their on-disk spec.md "
            "and copy the body into `body=`).\n"
            "\n"
            "Body is a full markdown document. Frontmatter is added "
            "automatically (task_id / title / created_by / priority / "
            "spec_author / spec_written_at). The body should cover Goal, "
            "'Done looks like', Constraints, References. Existing spec "
            "is overwritten — rolling history lives in events.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- body: full markdown body, required (max 40000 chars)\n"
            "- on_behalf_of: optional Player slot (Coach-only override)\n"
            "- message_to_coach: optional one-line note delivered to "
            "Coach in real time (it's the wake reason). Use this to "
            "flag what you noticed while drafting, open questions, "
            "anything the executor should be aware of beyond the spec "
            "body. Carried verbatim in the `task_spec_written` event "
            "payload."
        ),
        {
            "task_id": str, "body": str, "on_behalf_of": str,
            "message_to_coach": str,
        },
    )
    async def write_task_spec(args: dict[str, Any]) -> dict[str, Any]:
        task_id = (args.get("task_id") or "").strip()
        body = args.get("body") or ""
        on_behalf_of_raw = (args.get("on_behalf_of") or "").strip().lower()
        message_to_coach = (args.get("message_to_coach") or "").strip()
        if not task_id:
            return _err("task_id is required")
        if not body.strip():
            return _err("body is required (empty specs are not useful)")
        if len(body) > 40_000:
            return _err(f"body too long ({len(body)} chars, max 40000)")
        if len(message_to_coach) > 2000:
            return _err(
                f"message_to_coach too long ({len(message_to_coach)} chars, "
                f"max 2000)"
            )

        # v0.3.5 override: Coach can register a spec on a Player's
        # behalf when the Player's runtime can't reach this tool.
        # Mirror of `coord_submit_audit_report(on_behalf_of=...)`.
        if on_behalf_of_raw:
            if not caller_is_coach:
                return _err(
                    "on_behalf_of is Coach-only (override path for "
                    "when an assigned planner's runtime can't reach "
                    "this tool). Players write their own specs."
                )
            if (
                on_behalf_of_raw not in VALID_RECIPIENTS
                or on_behalf_of_raw in ("coach", "broadcast")
            ):
                return _err(
                    f"on_behalf_of must be a Player slot (p1..p10), "
                    f"not {on_behalf_of_raw!r}"
                )
            effective_author: str = on_behalf_of_raw
        else:
            effective_author = caller_id

        from server.tasks import (
            is_valid_task_id, write_task_spec as _write_spec,
            spec_relative_path,
        )
        if not is_valid_task_id(task_id):
            return _err(f"invalid task_id format: {task_id!r}")

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            # Read task metadata + permission check.
            cur = await c.execute(
                "SELECT title, owner, created_by, created_at, priority, "
                "status, parent_id "
                "FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            t = dict(row)

            # Permission: Coach can always spec. Players need one of:
            #   (a) active planner role for this task
            #   (b) executor (== tasks.owner)
            #   (c) parent is the caller's current task
            # When Coach uses on_behalf_of, the override path is the
            # gate (Coach already has full permission); we additionally
            # verify the named Player has an active planner role on
            # this task so Coach doesn't accidentally credit a spec to
            # an unrelated slot.
            allowed = False
            if caller_is_coach:
                allowed = True
                if on_behalf_of_raw:
                    cur = await c.execute(
                        "SELECT 1 FROM task_role_assignments "
                        "WHERE task_id = ? AND role = 'planner' "
                        "AND owner = ? "
                        "AND completed_at IS NULL "
                        "AND superseded_by IS NULL LIMIT 1",
                        (task_id, on_behalf_of_raw),
                    )
                    if not await cur.fetchone():
                        return _err(
                            f"on_behalf_of='{on_behalf_of_raw}' has no "
                            f"active planner role on task {task_id}. "
                            f"Either the Player isn't the assigned "
                            f"planner, or the role row was superseded "
                            f"by a reassignment. Use coord_write_task_spec "
                            f"without on_behalf_of (you'll be recorded "
                            f"as spec_author) or fix the assignment "
                            f"first via coord_approve_stage(stage='plan', "
                            f"assignee=<slot>)."
                        )
            elif t["owner"] == caller_id:
                allowed = True
            else:
                # Active planner row check.
                cur = await c.execute(
                    "SELECT 1 FROM task_role_assignments "
                    "WHERE task_id = ? AND role = 'planner' AND owner = ? "
                    "AND completed_at IS NULL AND superseded_by IS NULL "
                    "LIMIT 1",
                    (task_id, caller_id),
                )
                if await cur.fetchone():
                    allowed = True
                else:
                    # Subtask under caller's current task?
                    cur = await c.execute(
                        "SELECT current_task_id FROM agents WHERE id = ?",
                        (caller_id,),
                    )
                    arow = await cur.fetchone()
                    cur_task = dict(arow)["current_task_id"] if arow else None
                    if cur_task and t["parent_id"] == cur_task:
                        allowed = True

            if not allowed:
                return _err(
                    f"you can't spec task {task_id} — Coach can spec any "
                    f"task; Players need an active planner assignment, to "
                    f"be the executor, or to spec a subtask of their "
                    f"current task."
                )
        finally:
            await c.close()

        try:
            # Spec frontmatter records `effective_author` (the Player
            # whose work it is when overriding; the caller otherwise).
            target, rel, written_at = await _write_spec(
                project_id=project_id,
                task_id=task_id,
                title=t["title"],
                body=body,
                author=effective_author,
                created_by=t["created_by"],
                created_at=t["created_at"],
                priority=t["priority"],
            )
        except ValueError as exc:
            return _err(str(exc))
        except Exception as exc:
            return _err(f"spec write failed: {exc}")

        # Update tasks.spec_path + spec_written_at; mark the planner
        # role row complete on `effective_author`'s row (so a Coach
        # override correctly closes the Player's planner assignment,
        # not Coach's nonexistent one).
        completed_planner = False
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE tasks SET spec_path = ?, spec_written_at = ? "
                "WHERE id = ? AND project_id = ?",
                (rel, written_at, task_id, project_id),
            )
            cur = await c.execute(
                "UPDATE task_role_assignments SET completed_at = ? "
                "WHERE task_id = ? AND role = 'planner' AND owner = ? "
                "AND completed_at IS NULL AND superseded_by IS NULL",
                (written_at, task_id, effective_author),
            )
            completed_planner = bool(cur.rowcount)
            await c.commit()
        finally:
            await c.close()

        # Bus events: agent_id = the actor (Coach when override),
        # owner / on_behalf_of = effective_author (the Player whose
        # spec it is).
        on_behalf = effective_author != caller_id
        ts = _now_iso()
        await bus.publish(
            {
                "ts": ts,
                "agent_id": caller_id,
                "type": "task_spec_written",
                "task_id": task_id,
                "spec_path": rel,
                "to": t["owner"],
                "on_behalf_of": effective_author if on_behalf else None,
                "message_to_coach": message_to_coach or None,
            }
        )
        if completed_planner:
            await bus.publish(
                {
                    "ts": ts,
                    "agent_id": caller_id,
                    "type": "task_role_completed",
                    "task_id": task_id,
                    "role": "planner",
                    "owner": effective_author,
                    "artifact_path": rel,
                    "to": t["owner"],
                    "on_behalf_of": effective_author if on_behalf else None,
                }
            )

        # v2 §7.2.1 — wake Coach in real time on the spec write.
        # Skipped on Coach-as-override (the helper guards `caller_is_coach`
        # internally — covers the on_behalf_of path which always has
        # caller_id='coach'). The role label varies: a planner-role row
        # actually completing fires the standard "planner" label;
        # otherwise (re-write, no active planner row) the label is just
        # "spec writer" so the wake reason doesn't lie about role state.
        role_label = "planner" if completed_planner else "spec writer"
        await _wake_coach_for_completion(
            caller_id=caller_id,
            task_id=task_id,
            role=role_label,
            message_to_coach=message_to_coach,
            artifact_path=rel,
            extra_hint=(
                f"Spec is {len(body)} chars."
                + (f" Submitted on behalf of {effective_author}." if on_behalf else "")
                + ("" if completed_planner else " (no active planner role row was closed by this write — re-write or unassigned spec.)")
            ),
        )
        if on_behalf:
            return _ok(
                f"Wrote spec for {task_id} ({len(body)} chars) → "
                f"{rel} (on behalf of {effective_author}). "
                f"{effective_author}'s planner role is now complete. "
                f"The kanban auto-advances plan → the next stage in "
                f"the trajectory and wakes the next-stage assignee."
            )
        return _ok(
            f"Wrote spec for {task_id} ({len(body)} chars) → {rel}. "
            f"Your planner role is now complete. The kanban auto-"
            f"advances plan → the next stage in the trajectory and "
            f"wakes the next-stage assignee. You're done with this "
            f"task unless reassigned to another role."
        )

    # --------------------------------------------------------------
    # Role assignment tools (Coach-only). Pattern shared across
    # planner / auditor / shipper: accept single Player or list-as-pool;
    # validate target slots; insert task_role_assignments row(s); auto-wake.
    # --------------------------------------------------------------

    # v0.3 audit-2026-05-04 items 7+8: when role rows are created
    # mid-flight by coord_assign_*, mirror the new candidate list back
    # into tasks.trajectory.to for the matching stage so the stored
    # trajectory + Coach prompt + UI marker stay in sync. Best-effort:
    # an empty/malformed trajectory or a stage absent from the
    # trajectory leaves the column unchanged (full rewrites are
    # coord_set_task_trajectory's job).
    _STAGE_FOR_ROLE = {
        "planner": "plan",
        "executor": "execute",
        "auditor_syntax": "audit_syntax",
        "auditor_semantics": "audit_semantics",
        "shipper": "ship",
        "verifier": "verify",
    }

    async def _mirror_assign_targets_to_trajectory(
        c, *, task_id: str, role: str, targets: list[str],
        focus: str | None = None,
    ) -> None:
        target_stage = _STAGE_FOR_ROLE.get(role)
        if not target_stage:
            return
        cur = await c.execute(
            "SELECT trajectory FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cur.fetchone()
        if not row:
            return
        try:
            traj = json.loads(dict(row).get("trajectory") or "[]")
        except (TypeError, ValueError):
            return
        if not isinstance(traj, list):
            return
        changed = False
        for entry in traj:
            if (
                isinstance(entry, dict)
                and entry.get("stage") == target_stage
            ):
                entry["to"] = list(targets)
                # Review stages: keep `focus` in sync with the role row.
                # Only audit_* / verify stages carry focus; on other
                # stages any prior focus is silently dropped (defensive).
                if target_stage in ("audit_syntax", "audit_semantics", "verify"):
                    if focus:
                        entry["focus"] = focus
                    # When focus is None, preserve any prior focus on
                    # the trajectory entry (matches `_assign_role_helper`
                    # caller semantics — pass focus only when set).
                else:
                    entry.pop("focus", None)
                changed = True
                break
        if changed:
            await c.execute(
                "UPDATE tasks SET trajectory = ? WHERE id = ?",
                (json.dumps(traj, separators=(",", ":")), task_id),
            )

    async def _assign_role_helper(
        c,
        *,
        task_id: str,
        role: str,
        targets: list[str],
        wake_prompt_for_role: str,
        focus: str | None = None,
    ) -> tuple[bool, str, list[str]]:
        """Insert a task_role_assignments row for the given role and
        wake eligible Players. Returns (ok, message, woken_slots).

        For hard-assign (single target) the row's `owner` is set
        immediately. For pool (multi-target) `owner` stays NULL until
        a Player accepts the current-stage call. Future-stage
        reservations are stored but not woken until the card actually
        reaches that stage.

        v0.3 audit-2026-05-04 items 7+8:
        - Any prior active row for the same (task_id, role) is
          superseded via `superseded_by = <new_row_id>` so the board
          UI does not show a stale assignee alongside the new one.
        - The corresponding stage in `tasks.trajectory` has its `to`
          list updated to reflect the new candidate list, so the
          stored trajectory + Coach prompt + UI marker stay in sync
          with the role rows.

        `focus` is the audit-stage focus (kanban-specs §4.6 / §12.1).
        Caller is responsible for the audit_semantics-needs-focus
        validation; this helper just persists whatever it's handed.
        """
        import json as _json
        is_pool = len(targets) > 1
        now = _now_iso()
        eligible_json = _json.dumps(targets)
        focus_value = (focus or None) if isinstance(focus, str) else focus
        if is_pool:
            cur = await c.execute(
                "INSERT INTO task_role_assignments "
                "(task_id, role, eligible_owners, owner, assigned_at, focus) "
                "VALUES (?, ?, ?, NULL, ?, ?)",
                (task_id, role, eligible_json, now, focus_value),
            )
        else:
            cur = await c.execute(
                "INSERT INTO task_role_assignments "
                "(task_id, role, eligible_owners, owner, "
                "assigned_at, claimed_at, focus) "
                "VALUES (?, ?, '[]', ?, ?, ?, ?)",
                (task_id, role, targets[0], now, now, focus_value),
            )
        new_id = cur.lastrowid
        # Capture displaced owners BEFORE the supersede UPDATE so we
        # can wake them with a stand-down message after commit (v0.3.6
        # — the p1 raw-git incident was a planner reassignment that
        # the prior planner never learned about).
        from server.kanban import (
            collect_superseded_role_owners,
            send_role_stand_down,
        )
        displaced_role_owners = await collect_superseded_role_owners(
            c, task_id=task_id, role=role, new_row_id=new_id,
        )
        # Supersede any prior active row for the same (task_id, role)
        # so the board's "first active matching row" pick stays correct.
        await c.execute(
            "UPDATE task_role_assignments SET superseded_by = ? "
            "WHERE task_id = ? AND role = ? "
            "AND id != ? "
            "AND completed_at IS NULL AND superseded_by IS NULL",
            (new_id, task_id, role, new_id),
        )
        # Mirror the new candidate list back into tasks.trajectory.to
        # for the matching stage (item 8). Audit-stage focus is passed
        # through so a mid-flight `coord_assign_auditor(focus=...)`
        # also keeps the stored trajectory's matching entry in sync.
        await _mirror_assign_targets_to_trajectory(
            c, task_id=task_id, role=role, targets=targets,
            focus=focus_value,
        )
        await c.commit()

        # Stand-down wake to displaced assignees (post-commit). Helper
        # filters same-slot refresh and dedups.
        if displaced_role_owners:
            try:
                await send_role_stand_down(
                    task_id=task_id,
                    role=role,
                    displaced=displaced_role_owners,
                    new_owners=targets,
                )
            except Exception:
                pass

        # Auto-wake only when the role is active for the card's current
        # stage. Coach may reserve future formal/semantic/ship roles up
        # front; those Players must not see actionable work until the
        # task flows into their stage.
        cur = await c.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cur.fetchone()
        stage = dict(row).get("status") if row else ""
        woken_now = _role_matches_stage(role, stage)
        if woken_now:
            try:
                from server.agents import maybe_wake_agent
                wake_body = _with_player_reminder(wake_prompt_for_role)
                for slot in targets:
                    try:
                        await maybe_wake_agent(
                            slot, wake_body,
                            bypass_debounce=True,
                            wake_source="kanban_role",
                        )
                    except Exception:
                        pass
            except Exception:
                pass

        # v0.3.11 — every branch ends with imperative "what's next"
        # so Coach doesn't read the response as a status report and
        # double-fire coord_send_message.
        if is_pool:
            if woken_now:
                msg = (
                    f"Called {task_id} {role} pool: "
                    f"{', '.join(targets)}. All are auto-woken; "
                    f"first to call coord_accept_role wins. Wake body "
                    f"carries task context; add coord_send_message "
                    f"only if extra background is needed."
                )
            else:
                msg = (
                    f"Reserved {task_id} {role} pool: "
                    f"{', '.join(targets)}. The wake will fire when "
                    f"the task reaches the {role} stage. You can "
                    f"safely move on; nothing to do until then."
                )
            return True, msg, targets
        if woken_now:
            msg = (
                f"Assigned {task_id} {role} → {targets[0]}. The "
                f"kanban auto-wakes {targets[0]} with the task "
                f"context + completion-tool hint. Wake body contains "
                f"task context; add coord_send_message if extra "
                f"background is needed."
            )
        else:
            msg = (
                f"Reserved {task_id} {role} → {targets[0]}. The "
                f"wake will fire when the task reaches the {role} "
                f"stage (after upstream stages complete). You can "
                f"safely move on; nothing to do until then."
            )
        return True, msg, targets

    async def _validate_role_targets(
        targets_raw: str, *, role_label: str
    ) -> tuple[list[str] | None, str | None]:
        """Parse and validate a `to=` argument: comma-list → pool;
        single → hard-assign. Returns `(slots, None)` or `(None, error)`."""
        if not targets_raw:
            return None, f"'to' is required (Player slot id, or comma-list for {role_label} pool)"
        if "," in targets_raw:
            parts = [p.strip().lower() for p in targets_raw.split(",") if p.strip()]
        else:
            parts = [targets_raw.strip().lower()]
        if not parts:
            return None, "'to' resolved to empty list"
        for slot in parts:
            if slot in ("coach", "broadcast"):
                return None, f"can only assign Players (p1..p10), not {slot!r}"
            if slot not in VALID_RECIPIENTS:
                return None, f"invalid target '{slot}' — must be p1..p10"
        seen: set[str] = set()
        deduped: list[str] = []
        for slot in parts:
            if slot in seen:
                continue
            seen.add(slot)
            if await _is_locked(slot):
                return None, (
                    f"Player {slot} is locked. Pick unlocked Players."
                )
            deduped.append(slot)
        return deduped, None

    @tool(
        "coord_submit_audit_report",
        (
            "**Your message to Coach that the audit is done.** "
            "Calling this tool IS the act of delivering your verdict "
            "+ review body to Coach — without it, Coach has no idea "
            "you've reviewed anything, and the kanban can't record "
            "the verdict. Writing audit_<round>_<kind>.md to disk "
            "without calling this tool is silence; this is the #1 "
            "stall cause for auditor roles.\n"
            "\n"
            "Writes the markdown report to the task's working dir + "
            "emits `audit_report_submitted` (and `audit_fail_notification` "
            "on FAIL) AND wakes Coach in real time (v2 §7.2.1) with "
            "your message_to_coach as the wake reason. FAIL verdicts "
            "especially reach Coach instantly so a re-execute decision "
            "happens without waiting for the next tick.\n"
            "\n"
            "Normal use (Player-only): you have an active auditor "
            "assignment matching `kind` and submit your own review.\n"
            "\n"
            "Override use (Coach-only, v0.3.4): when an assigned auditor "
            "Player cannot reach this tool from their runtime (the "
            "production failure mode that triggered v0.3.4 — Player "
            "wrote audit_<round>_<kind>.md to disk and stopped because "
            "coord_* wasn't visible), Coach can submit on their behalf "
            "by passing `on_behalf_of='<player_slot>'`. The recorded "
            "auditor is the named Player; the actor in the bus event "
            "is Coach. Use this ONLY after confirming the Player "
            "actually wrote the audit (read their audit_*.md from disk "
            "and copy the body into `body=`); otherwise advance via "
            "coord_approve_stage instead.\n"
            "\n"
            "Writes the markdown report to "
            "/data/projects/<id>/working/tasks/<task_id>/audits/"
            "audit_<round>_<kind>.md and triggers the kanban subscriber:\n"
            "  - verdict='pass' on syntax → task moves to audit_semantics\n"
            "  - verdict='pass' on semantics → task moves to ship\n"
            "  - verdict='fail' on either → task reverts to execute "
            "(executor gets the spec + your report attached on auto-wake)\n"
            "\n"
            "Body should explain what you checked, what you found, and "
            "(on fail) specifically what needs to be fixed.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- kind: 'syntax' or 'semantics' (required, must match assignment)\n"
            "- body: full markdown report (required, max 40000 chars)\n"
            "- verdict: 'pass' or 'fail' (required)\n"
            "- on_behalf_of: optional Player slot (Coach-only override)\n"
            "- message_to_coach: optional one-line note delivered to "
            "Coach in real time (it's the wake reason). Distinct from "
            "the audit body itself — use this to flag something Coach "
            "should know that doesn't belong in the report (e.g. "
            "recurring pattern, suggestion for re-execution framing). "
            "Carried verbatim in the `audit_report_submitted` event "
            "payload."
        ),
        {
            "task_id": str, "kind": str, "body": str,
            "verdict": str, "on_behalf_of": str,
            "message_to_coach": str,
        },
    )
    async def submit_audit_report(args: dict[str, Any]) -> dict[str, Any]:
        on_behalf_of_raw = (args.get("on_behalf_of") or "").strip().lower()
        # Resolve the effective auditor identity: when Coach passes
        # `on_behalf_of`, treat the named Player as the auditor for
        # role-row lookup + recorded `auditor` field. Otherwise the
        # caller is the auditor (Player normal path).
        if on_behalf_of_raw:
            if not caller_is_coach:
                return _err(
                    "on_behalf_of is Coach-only (override path for "
                    "when an assigned auditor's runtime can't reach "
                    "this tool)."
                )
            if on_behalf_of_raw not in VALID_RECIPIENTS or on_behalf_of_raw in ("coach", "broadcast"):
                return _err(
                    f"on_behalf_of must be a Player slot (p1..p10), "
                    f"not {on_behalf_of_raw!r}"
                )
            effective_auditor = on_behalf_of_raw
        else:
            if caller_is_coach:
                return _err(
                    "Coach doesn't audit directly. Either assign a "
                    "Player auditor via coord_approve_stage(stage='audit_*', "
                    "assignee=<slot>, note=...) and let them submit, or "
                    "override on a stuck Player's behalf via "
                    "on_behalf_of='<slot>' (only when their "
                    "runtime can't reach this tool — read their "
                    "on-disk audit_*.md and copy the body in)."
                )
            effective_auditor = caller_id
        task_id = (args.get("task_id") or "").strip()
        kind_input = (args.get("kind") or "").strip().lower()
        body = args.get("body") or ""
        verdict = (args.get("verdict") or "").strip().lower()
        if not task_id:
            return _err("task_id is required")
        role = _resolve_audit_role_kind(kind_input)
        if role is None:
            return _err("kind must be 'formal'/'syntax' or 'semantic'/'semantics'")
        kind = "syntax" if role == "auditor_syntax" else "semantics"
        review_label = "formal" if kind == "syntax" else "semantic"
        if verdict not in ("pass", "fail"):
            return _err("verdict must be 'pass' or 'fail'")
        if not body.strip():
            return _err("body is required")
        if len(body) > 40_000:
            return _err(f"body too long ({len(body)} chars, max 40000)")
        message_to_coach = (args.get("message_to_coach") or "").strip()
        if len(message_to_coach) > 2000:
            return _err(
                f"message_to_coach too long ({len(message_to_coach)} chars, "
                f"max 2000)"
            )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            task_row = await cur.fetchone()
            if not task_row:
                return _err(f"task {task_id} not found")
            expected_stage = "audit_syntax" if role == "auditor_syntax" else "audit_semantics"
            actual_stage = dict(task_row).get("status")
            if actual_stage != expected_stage:
                return _err(
                    f"{review_label} review is not active for task {task_id} "
                    f"(stage={actual_stage}). Future-stage assignments are "
                    f"not actionable until the card reaches their column."
                )
            # Find the active auditor assignment for the effective
            # auditor (= caller in normal use; = on_behalf_of when
            # Coach overrides). Must exist, be uncompleted, and not
            # superseded.
            cur = await c.execute(
                "SELECT id FROM task_role_assignments "
                "WHERE task_id = ? AND role = ? AND owner = ? "
                "AND completed_at IS NULL AND superseded_by IS NULL "
                "ORDER BY assigned_at DESC LIMIT 1",
                (task_id, role, effective_auditor),
            )
            row = await cur.fetchone()
            if not row:
                return _err(
                    f"no active {review_label} reviewer assignment for "
                    f"{effective_auditor} on task {task_id}. Coach "
                    f"must call coord_approve_stage(stage='{review_label.replace(' ', '_')}', "
                    f"assignee=<slot>) before submission."
                )
            assignment_id = dict(row)["id"]

            # Compute round number = count of prior assignments for this
            # (task, kind) PLUS 1. Includes this one (we just SELECTed it).
            cur = await c.execute(
                "SELECT COUNT(*) AS n FROM task_role_assignments "
                "WHERE task_id = ? AND role = ?",
                (task_id, role),
            )
            count_row = await cur.fetchone()
            round_num = int(dict(count_row)["n"])
        finally:
            await c.close()

        # Write the report .md.
        from server.tasks import (
            write_audit_report as _write_audit, audit_report_relative_path,
        )
        try:
            target, rel, submitted_at = await _write_audit(
                project_id=project_id,
                task_id=task_id,
                kind=kind,
                round_num=round_num,
                body=body,
                auditor=effective_auditor,
                verdict=verdict,
            )
        except ValueError as exc:
            return _err(str(exc))
        except Exception as exc:
            return _err(f"audit report write failed: {exc}")

        # Update the role row + tasks.latest_audit_* surface fields.
        executor_owner = None
        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE task_role_assignments "
                "SET report_path = ?, verdict = ?, completed_at = ? "
                "WHERE id = ?",
                (rel, verdict, submitted_at, assignment_id),
            )
            await _reset_agent_idle_tools(c, effective_auditor)
            await c.execute(
                "UPDATE tasks SET latest_audit_report_path = ?, "
                "latest_audit_kind = ?, latest_audit_verdict = ? "
                "WHERE id = ? AND project_id = ?",
                (rel, kind, verdict, task_id, project_id),
            )
            cur = await c.execute(
                "SELECT owner FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            task_row = await cur.fetchone()
            if task_row:
                executor_owner = dict(task_row).get("owner")
            await c.commit()
        finally:
            await c.close()

        # Emit the kanban-driving event. The auto-advance subscriber
        # (server/kanban.py — landing in a future iteration) listens.
        ts = _now_iso()
        # Bus events: agent_id is the actor (Coach when override),
        # auditor_id / owner is the effective auditor (the Player).
        # That way the timeline shows who actually pressed the button
        # AND whose audit it is.
        on_behalf = effective_auditor != caller_id
        await bus.publish(
            {
                "ts": ts,
                "agent_id": caller_id,
                "type": "task_role_completed",
                "task_id": task_id,
                "role": role,
                "owner": effective_auditor,
                "artifact_path": rel,
                "verdict": verdict,
                "to": executor_owner,
                "on_behalf_of": effective_auditor if on_behalf else None,
            }
        )
        await bus.publish(
            {
                "ts": ts,
                "agent_id": caller_id,
                "type": "audit_report_submitted",
                "task_id": task_id,
                "kind": kind,
                "verdict": verdict,
                "report_path": rel,
                "round": round_num,
                "auditor_id": effective_auditor,
                "to": executor_owner,
                # 'to' = executor — surfaces the event in their pane so
                # they see fail verdicts immediately. Read from tasks.owner.
                "on_behalf_of": effective_auditor if on_behalf else None,
                "message_to_coach": message_to_coach or None,
            }
        )

        # v2 §7.2.1 — wake Coach immediately on every audit reply
        # (pass and fail). FAIL verdicts especially must reach Coach in
        # real time so a re-execute decision happens without waiting
        # for the next tick. The helper short-circuits on Coach-as-
        # override (caller_is_coach) so the on-behalf path doesn't loop.
        await _wake_coach_for_completion(
            caller_id=caller_id,
            task_id=task_id,
            role=f"{kind} auditor (round {round_num}, verdict={verdict})",
            message_to_coach=message_to_coach,
            artifact_path=rel,
            extra_hint=(
                "FAIL does NOT auto-revert in v2 — read the report and "
                "decide via coord_approve_stage."
                if verdict == "fail" else
                "PASS — pick the next stage / assignee via coord_approve_stage."
            )
            + (
                f" Submitted on behalf of {effective_auditor}."
                if on_behalf else ""
            ),
        )
        on_behalf_suffix = (
            f" (submitted on behalf of {effective_auditor})"
            if on_behalf else ""
        )
        head = (
            f"Submitted {kind} audit (round {round_num}, {verdict}) "
            f"for {task_id} → {rel}{on_behalf_suffix}."
        )
        if verdict == "pass":
            who_done = (
                f"{effective_auditor}'s reviewer role"
                if on_behalf else "Your reviewer role"
            )
            return _ok(
                f"{head} {who_done} is now complete. The kanban "
                f"auto-advances to the next stage in the trajectory "
                f"(semantic review, ship, or archive depending on "
                f"what's configured). "
                + (
                    "You're done with this task unless reassigned."
                    if not on_behalf else
                    "The trajectory continues automatically."
                )
            )
        # verdict == fail
        return _ok(
            f"{head} The verdict is recorded and surfaces to Coach "
            f"via the event log; Coach decides whether to re-execute, "
            f"abandon, or override the audit. v2 does NOT auto-revert "
            f"on FAIL — do not start fixing things based on this "
            f"verdict. Wait for Coach's wake."
        )

    @tool(
        "coord_submit_verification_report",
        (
            "**Your message to Coach that post-ship verification is done.** "
            "Player-only. Verifiers use this after a task reaches the "
            "optional `verify` stage. Writes a markdown report under the "
            "task's verifications/ folder, records PASS/FAIL on the "
            "verifier role row, emits `verification_report_submitted`, "
            "and wakes Coach in real time.\n"
            "\n"
            "Verification is post-ship evidence, not a pre-ship audit. "
            "FAIL does NOT auto-revert, auto-create a follow-up, or "
            "auto-archive; Coach reads the report and decides whether "
            "to archive, create a follow-up, rollback, reroute to "
            "execute, or send back to ship.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- verdict: 'pass' or 'fail' (required)\n"
            "- body: full markdown report (required, max 40000 chars)\n"
            "- message_to_coach: optional one-line note delivered to Coach\n"
            "- evidence: optional object/string with deploy URL, PR/SHA, "
            "checked_at, service, etc."
        ),
        # Raw JSON schema: task_id/verdict/body are required;
        # message_to_coach/evidence are genuinely optional. The compact
        # dict-shorthand is treated as all-required by some MCP/SDK
        # clients before this handler can apply its own defaults.
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "verdict": {"type": "string", "enum": ["pass", "fail"]},
                "body": {"type": "string"},
                "message_to_coach": {"type": "string"},
                "evidence": {
                    "oneOf": [
                        {"type": "object", "additionalProperties": True},
                        {"type": "array"},
                        {"type": "string"},
                        {"type": "number"},
                        {"type": "boolean"},
                    ]
                },
            },
            "required": ["task_id", "verdict", "body"],
            "additionalProperties": True,
        },
    )
    async def submit_verification_report(args: dict[str, Any]) -> dict[str, Any]:
        if caller_is_coach:
            return _err(
                "Coach doesn't verify directly. Assign a Player verifier "
                "via coord_approve_stage(next_stage='verify', assignee=<slot>, "
                "note=...) and let them submit."
            )
        task_id = (args.get("task_id") or "").strip()
        verdict = (args.get("verdict") or "").strip().lower()
        body = args.get("body") or ""
        message_to_coach = (args.get("message_to_coach") or "").strip()
        evidence_raw = args.get("evidence")
        if not task_id:
            return _err("task_id is required")
        if verdict not in ("pass", "fail"):
            return _err("verdict must be 'pass' or 'fail'")
        if not body.strip():
            return _err("body is required")
        if len(body) > 40_000:
            return _err(f"body too long ({len(body)} chars, max 40000)")
        if len(message_to_coach) > 2000:
            return _err(
                f"message_to_coach too long ({len(message_to_coach)} chars, "
                f"max 2000)"
            )
        try:
            evidence_text = (
                json.dumps(evidence_raw, ensure_ascii=False, sort_keys=True)
                if isinstance(evidence_raw, (dict, list)) else
                (str(evidence_raw).strip() if evidence_raw is not None else "")
            )
        except Exception:
            evidence_text = str(evidence_raw)
        if len(evidence_text) > 4000:
            return _err(f"evidence too long ({len(evidence_text)} chars, max 4000)")

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status, owner FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            task_row = await cur.fetchone()
            if not task_row:
                return _err(f"task {task_id} not found")
            task_data = dict(task_row)
            actual_stage = task_data.get("status")
            if actual_stage != "verify":
                return _err(
                    f"verification is not active for task {task_id} "
                    f"(stage={actual_stage}). Coach must approve "
                    f"ship→verify before verifier work is actionable."
                )
            cur = await c.execute(
                "SELECT id FROM task_role_assignments "
                "WHERE task_id = ? AND role = 'verifier' AND owner = ? "
                "AND completed_at IS NULL AND superseded_by IS NULL "
                "ORDER BY assigned_at DESC LIMIT 1",
                (task_id, caller_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(
                    f"no active verifier assignment for {caller_id} on "
                    f"task {task_id}. Coach must call "
                    f"coord_approve_stage(next_stage='verify', assignee=<slot>) "
                    f"before submission."
                )
            assignment_id = dict(row)["id"]
            cur = await c.execute(
                "SELECT COUNT(*) AS n FROM task_role_assignments "
                "WHERE task_id = ? AND role = 'verifier'",
                (task_id,),
            )
            count_row = await cur.fetchone()
            round_num = int(dict(count_row)["n"])
            executor_owner = task_data.get("owner")
        finally:
            await c.close()

        from server.tasks import write_verification_report as _write_verification
        try:
            _target, rel, submitted_at = await _write_verification(
                project_id=project_id,
                task_id=task_id,
                round_num=round_num,
                body=body,
                verifier=caller_id,
                verdict=verdict,
                evidence=evidence_text,
            )
        except ValueError as exc:
            return _err(str(exc))
        except Exception as exc:
            return _err(f"verification report write failed: {exc}")

        c = await configured_conn()
        try:
            await c.execute(
                "UPDATE task_role_assignments "
                "SET report_path = ?, verdict = ?, completed_at = ? "
                "WHERE id = ?",
                (rel, verdict, submitted_at, assignment_id),
            )
            await _reset_agent_idle_tools(c, caller_id)
            await c.commit()
        finally:
            await c.close()

        ts = _now_iso()
        await bus.publish({
            "ts": ts,
            "agent_id": caller_id,
            "type": "task_role_completed",
            "task_id": task_id,
            "role": "verifier",
            "owner": caller_id,
            "artifact_path": rel,
            "verdict": verdict,
            "message_to_coach": message_to_coach or None,
            "to": "coach",
        })
        await bus.publish({
            "ts": ts,
            "agent_id": caller_id,
            "type": "verification_report_submitted",
            "task_id": task_id,
            "verdict": verdict,
            "report_path": rel,
            "round": round_num,
            "verifier_id": caller_id,
            "evidence": evidence_raw if evidence_raw is not None else None,
            "message_to_coach": message_to_coach or None,
            "to": executor_owner or "coach",
        })
        await _wake_coach_for_completion(
            caller_id=caller_id,
            task_id=task_id,
            role=f"verifier (round {round_num}, verdict={verdict})",
            message_to_coach=message_to_coach,
            artifact_path=rel,
            extra_hint=(
                "Verification FAIL does NOT auto-revert — read the "
                "report and decide whether to archive, follow up, "
                "rollback, reroute to execute, or re-ship."
                if verdict == "fail" else
                "Verification PASS — decide whether to archive with "
                "the verified deployed state."
            ),
        )
        return _ok(
            f"Submitted verification report (round {round_num}, {verdict}) "
            f"for {task_id} → {rel}. Your verifier role is now complete. "
            f"Coach was woken with your message_to_coach as context; "
            f"FAIL does NOT auto-revert or create follow-up work."
        )

    def _next_action_for_plate(
        *,
        executor_task: dict[str, Any] | None,
        pending_reviews: list[dict[str, Any]],
        pending_plans: list[dict[str, Any]],
        pending_ships: list[dict[str, Any]],
        pending_verifications: list[dict[str, Any]],
        eligible: list[dict[str, Any]],
    ) -> str | None:
        """Pick the highest-priority actionable item and return the
        imperative call line to surface in `coord_my_assignments`'
        Next-action footer.

        Priority order (matches kanban-flow expectations):
          1. Active executor task — if the spec gate is open, write
             code/artifacts then call coord_commit_push (code) or
             coord_role_complete (non-code); if the spec gate is
             closed (no spec on a `plan`-stage task), say so explicitly.
          2. Pending reviewer assignment — call
             coord_submit_audit_report.
          3. Pending shipper assignment — call coord_role_complete.
          4. Pending verifier assignment — call
             coord_submit_verification_report.
          5. Pending planner assignment — call coord_write_task_spec.
          6. Eligible-pool entry (FYI in v2) — wait; pools never
             auto-resolve to a Player claim. Coach picks via
             coord_approve_stage.

        Returns None when nothing actionable exists (caller renders
        a "your plate is empty" line).
        """
        if executor_task:
            tid = executor_task["id"]
            stage = executor_task.get("status") or ""
            has_spec = executor_task.get("has_spec", False)
            traj = executor_task.get("trajectory") or []
            if stage == "plan" and "plan" in traj and not has_spec:
                # Spec gate is closed — the planner hasn't written
                # the spec yet. The Player can't do executor work
                # until the spec lands. Surface the wait state.
                return (
                    f"  Wait — task {tid} has 'plan' in its trajectory "
                    f"and no spec.md yet. The executor can't start "
                    f"until the planner calls coord_write_task_spec. "
                    f"If you ARE the planner, write the spec; "
                    f"otherwise message Coach if the planner has "
                    f"gone silent."
                )
            has_audit = any(
                s in ("audit_syntax", "audit_semantics") for s in traj
            )
            self_audit = "" if has_audit else (
                " This trajectory has no audit stage after execute, "
                "so SELF-AUDIT first (run tests / sanity-check) "
                "before calling the tool."
            )
            return (
                f"  Do the executor work for task {tid}, then call "
                f"coord_commit_push(task_id={tid!r}, message=..., "
                f"message_to_coach=<your response>) for code OR "
                f"coord_role_complete(task_id={tid!r}, "
                f"message_to_coach=..., artifact_path=<path?>) for "
                f"non-code deliverables.{self_audit} The call wakes "
                f"Coach in real time; Coach reads, may reply, and "
                f"approves the next stage."
            )
        if pending_reviews:
            e = pending_reviews[0]
            tid = e["task_id"]
            kind = e.get("kind") or "syntax"
            return (
                f"  Read the spec + the executor's commit/artifact "
                f"for task {tid}, then call "
                f"coord_submit_audit_report(task_id={tid!r}, "
                f"kind={kind!r}, body=<your review>, "
                f"verdict='pass' or 'fail', message_to_coach=...). "
                f"Coach reviews the verdict and decides the next "
                f"move; FAIL does NOT auto-revert in v2."
            )
        if pending_ships:
            e = pending_ships[0]
            tid = e["task_id"]
            return (
                f"  Merge / publish / hand-off task {tid}, then "
                f"call coord_role_complete(task_id={tid!r}, "
                f"message_to_coach='shipped at <ref>'). Coach "
                f"reviews and archives via coord_archive_task with "
                f"a user-facing summary."
            )
        if pending_verifications:
            e = pending_verifications[0]
            tid = e["task_id"]
            return (
                f"  Verify the shipped task {tid}, then call "
                f"coord_submit_verification_report(task_id={tid!r}, "
                f"verdict='pass' or 'fail', body=<your report>, "
                f"message_to_coach=...). Coach reviews the post-ship "
                f"verdict and decides whether to archive, create a "
                f"follow-up, rollback, or reroute. FAIL does NOT "
                f"auto-revert."
            )
        if pending_plans:
            e = pending_plans[0]
            tid = e["task_id"]
            return (
                f"  Draft the spec for task {tid}, then call "
                f"coord_write_task_spec(task_id={tid!r}, "
                f"body=<spec>, message_to_coach=<your response>). "
                f"Coach reviews the spec and approves execute. "
                f"DO NOT just describe the task back to Coach — "
                f"writing the spec IS the planner's role."
            )
        if eligible:
            e = eligible[0]
            tid = e["task_id"]
            role = e.get("role") or ""
            return (
                f"  Task {tid} lists you in its FYI pool for role "
                f"{role!r}. In v2 pools don't auto-resolve — Coach "
                f"picks the assignee explicitly. Wait for Coach's "
                f"coord_approve_stage wake. Do NOT start the role "
                f"work without it."
            )
        return None

    @tool(
        "coord_my_assignments",
        (
            "Player-only. Returns your current actionable plate in four buckets:\n"
            "  1. Active executor task (the one in agents.current_task_id)\n"
            "  2. Pending reviewer assignments (formal + semantic)\n"
            "  3. Pending shipper assignments\n"
            "  4. Pending verifier assignments\n"
            "  5. Eligible-pool tasks you could claim\n"
            "\n"
            "Call at turn start when you're not sure what to do. Returns "
            "an empty plate if nothing is on you (and the idle poller "
            "will eventually wake you when there's pool work)."
        ),
        {},
    )
    async def my_assignments(args: dict[str, Any]) -> dict[str, Any]:
        if caller_is_coach:
            return _err(
                "coord_my_assignments is Player-only — Coach uses "
                "coord_list_tasks to see the full board."
            )
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            # Bucket 1: active executor task.
            cur = await c.execute(
                "SELECT a.current_task_id, t.id AS task_id, t.title, "
                "t.status, t.priority, t.trajectory, t.spec_path, "
                "EXISTS ("
                "  SELECT 1 FROM task_role_assignments r "
                "  WHERE r.task_id = a.current_task_id "
                "  AND r.role = 'executor' "
                "  AND r.owner = ? "
                "  AND r.completed_at IS NULL "
                "  AND r.superseded_by IS NULL"
                ") AS has_active_executor "
                "FROM agents a LEFT JOIN tasks t ON t.id = a.current_task_id "
                "WHERE a.id = ?",
                (caller_id, caller_id),
            )
            arow = await cur.fetchone()
            executor_task = None
            if arow:
                ad = dict(arow)
                current_task_id = ad.get("current_task_id")
                task_status = ad.get("status")
                if (
                    current_task_id
                    and ad.get("task_id")
                    and task_status != "archive"
                    and ad.get("has_active_executor")
                ):
                    ad_stages = _trajectory_stages_from_row(ad)
                    executor_task = {
                        "id": ad["current_task_id"],
                        "title": ad.get("title") or "(unknown)",
                        "status": ad.get("status") or "?",
                        "priority": ad.get("priority") or "normal",
                        "trajectory": ad_stages,
                        "has_spec": bool(ad.get("spec_path")),
                    }
                elif (
                    current_task_id
                    and (not ad.get("task_id") or task_status == "archive")
                ):
                    await c.execute(
                        "UPDATE agents SET current_task_id = NULL "
                        "WHERE id = ? AND current_task_id = ?",
                        (caller_id, current_task_id),
                    )
                    await c.commit()

            # Defensive Bucket 1b: if `agents.current_task_id` is out
            # of sync with the executor role row (the production bug
            # where a hard-assigned create-time executor never had
            # current_task_id written), still surface the task by
            # walking the role table directly. Filtered to status =
            # 'execute' so future-stage executor reservations don't
            # leak in. Also self-heals current_task_id so subsequent
            # callers / wakes find the right task.
            if executor_task is None:
                cur = await c.execute(
                    "SELECT t.id, t.title, t.status, t.priority, "
                    "t.trajectory, t.spec_path "
                    "FROM task_role_assignments r "
                    "JOIN tasks t ON t.id = r.task_id "
                    "WHERE r.owner = ? AND r.role = 'executor' "
                    "AND r.completed_at IS NULL "
                    "AND r.superseded_by IS NULL "
                    "AND t.project_id = ? AND t.status = 'execute' "
                    "ORDER BY r.assigned_at DESC LIMIT 1",
                    (caller_id, project_id),
                )
                row = await cur.fetchone()
                if row:
                    rd = dict(row)
                    rd_stages = _trajectory_stages_from_row(rd)
                    executor_task = {
                        "id": rd["id"],
                        "title": rd.get("title") or "(unknown)",
                        "status": rd.get("status") or "?",
                        "priority": rd.get("priority") or "normal",
                        "trajectory": rd_stages,
                        "has_spec": bool(rd.get("spec_path")),
                    }
                    # Self-heal: write back current_task_id so the next
                    # read goes through Bucket 1's fast path, and restore
                    # the executor tool allowlist so the next Codex spawn
                    # exposes coord_commit_push.
                    await _set_agent_current_task_if_free_or_stale(
                        c, caller_id, rd["id"],
                    )
                    await _set_agent_role_tools(c, caller_id, "executor")
                    await c.commit()

            # Bucket 2 + 3 + 4: pending planner / reviewer / shipper / verifier
            # assignments, filtered to the card's current stage so
            # future-stage reservations do not look actionable early.
            cur = await c.execute(
                "SELECT r.task_id, r.role, r.assigned_at, t.title, t.priority "
                "FROM task_role_assignments r "
                "JOIN tasks t ON t.id = r.task_id "
                "WHERE r.owner = ? AND r.role IN "
                "  ('planner','auditor_syntax','auditor_semantics','shipper','verifier') "
                "AND r.completed_at IS NULL AND r.superseded_by IS NULL "
                "AND t.project_id = ? "
                "AND ("
                "  (r.role = 'planner' AND t.status = 'plan') "
                "  OR "
                "  (r.role = 'auditor_syntax' AND t.status = 'audit_syntax') "
                "  OR (r.role = 'auditor_semantics' AND t.status = 'audit_semantics') "
                "  OR (r.role = 'shipper' AND t.status = 'ship') "
                "  OR (r.role = 'verifier' AND t.status = 'verify') "
                ") "
                "ORDER BY r.assigned_at",
                (caller_id, project_id),
            )
            pending_plans: list[dict[str, Any]] = []
            pending_reviews: list[dict[str, Any]] = []
            pending_ships: list[dict[str, Any]] = []
            pending_verifications: list[dict[str, Any]] = []
            for r in await cur.fetchall():
                rd = dict(r)
                entry = {
                    "task_id": rd["task_id"],
                    "title": rd["title"],
                    "priority": rd["priority"],
                    "assigned_at": rd["assigned_at"],
                }
                if rd["role"] == "planner":
                    pending_plans.append(entry)
                elif rd["role"] == "shipper":
                    pending_ships.append(entry)
                elif rd["role"] == "verifier":
                    pending_verifications.append(entry)
                else:
                    entry["kind"] = _audit_kind_from_role(rd["role"])
                    pending_reviews.append(entry)

            # Bucket 4: eligible-pool tasks. JSON1 json_each scans the
            # eligible_owners array; cheap because we already filter on
            # role + status.
            cur = await c.execute(
                "SELECT DISTINCT r.task_id, r.role, r.eligible_owners, "
                "t.title, t.priority, t.trajectory "
                "FROM task_role_assignments r "
                "JOIN tasks t ON t.id = r.task_id, "
                "json_each(r.eligible_owners) je "
                "WHERE je.value = ? "
                "AND r.owner IS NULL "
                "AND r.completed_at IS NULL AND r.superseded_by IS NULL "
                "AND t.project_id = ? "
                "AND ("
                "  (r.role = 'executor' AND t.status = 'plan') "
                "  OR (r.role = 'planner' AND t.status = 'plan') "
                "  OR (r.role = 'auditor_syntax' AND t.status = 'audit_syntax') "
                "  OR (r.role = 'auditor_semantics' AND t.status = 'audit_semantics') "
                "  OR (r.role = 'shipper' AND t.status = 'ship') "
                "  OR (r.role = 'verifier' AND t.status = 'verify') "
                ") "
                "ORDER BY r.assigned_at",
                (caller_id, project_id),
            )
            eligible: list[dict[str, Any]] = []
            for r in await cur.fetchall():
                rd = dict(r)
                rd_stages = _trajectory_stages_from_row(rd)
                eligible.append({
                    "task_id": rd["task_id"],
                    "title": rd["title"],
                    "priority": rd["priority"],
                    "trajectory": rd_stages,
                    "role": rd["role"],
                })
        finally:
            await c.close()

        # Compose a concise text response.
        lines: list[str] = []
        if executor_task:
            spec_marker = "" if executor_task["has_spec"] else " [no spec]"
            traj_str = ",".join(executor_task["trajectory"]) or "execute"
            lines.append(
                f"## Executor: {executor_task['id']} "
                f"\"{executor_task['title']}\" "
                f"(stage={executor_task['status']}, "
                f"pri={executor_task['priority']}, "
                f"trajectory=[{traj_str}]{spec_marker})"
            )
        else:
            lines.append("## Executor: (none — you have no active task)")

        lines.append("")
        lines.append("## Pending planner assignments:")
        if pending_plans:
            for e in pending_plans:
                lines.append(
                    f"  - {e['task_id']} (pri={e['priority']}): {e['title']}"
                )
        else:
            lines.append("  (none)")

        lines.append("")
        lines.append("## Pending reviews:")
        if pending_reviews:
            for e in pending_reviews:
                lines.append(
                    f"  - {e['task_id']} ({e['kind']}, pri={e['priority']}): "
                    f"{e['title']}"
                )
        else:
            lines.append("  (none)")

        lines.append("")
        lines.append("## Pending ship assignments:")
        if pending_ships:
            for e in pending_ships:
                lines.append(
                    f"  - {e['task_id']} (pri={e['priority']}): {e['title']}"
                )
        else:
            lines.append("  (none)")

        lines.append("")
        lines.append("## Pending verification assignments:")
        if pending_verifications:
            for e in pending_verifications:
                lines.append(
                    f"  - {e['task_id']} (pri={e['priority']}): {e['title']}"
                )
        else:
            lines.append("  (none)")

        lines.append("")
        lines.append("## Available to claim (eligible pools):")
        if eligible:
            for e in eligible:
                role_label = {
                    "executor": "executor",
                    "auditor_syntax": "formal reviewer",
                    "auditor_semantics": "semantic reviewer",
                    "shipper": "shipper",
                    "verifier": "verifier",
                    "planner": "planner",
                }.get(e["role"], e["role"])
                stages = e.get("trajectory") or []
                traj_str = ",".join(stages) if stages else ""
                traj_chip = f" [{traj_str}]" if traj_str else ""
                lines.append(
                    f"  - {e['task_id']} ({role_label}, "
                    f"pri={e['priority']}{traj_chip}): {e['title']}"
                )
        else:
            lines.append("  (none)")

        # v0.3.10 — explicit next-action footer. The buckets above
        # are descriptive; without this footer Players read the
        # response as a status report, see "Pending planner
        # assignment" with the task id, conclude "okay, here's
        # what's pending," and stop the turn (production trace
        # 2026-05-06: planner sat on a task for hours after this
        # exact pattern). The footer names the next imperative
        # action with the task_id baked in, mirroring the kanban
        # subscriber's stage-entry wake hint.
        next_action = _next_action_for_plate(
            executor_task=executor_task,
            pending_reviews=pending_reviews,
            pending_plans=pending_plans,
            pending_ships=pending_ships,
            pending_verifications=pending_verifications,
            eligible=eligible,
        )
        lines.append("")
        lines.append("## Next action:")
        if next_action:
            lines.append(next_action)
        else:
            lines.append(
                "  Your plate is empty. Wait for a task assignment, "
                "or check the inbox for messages from Coach. The "
                "idle poller will wake you when pool work appears."
            )

        return _ok("\n".join(lines))

    @tool(
        "coord_set_task_trajectory",
        (
            "Coach-only. Mid-flight reroute of a task's trajectory. Use "
            "this when an unexpected audit reveals more work, or when "
            "Coach decides a task can skip an audit it originally had. "
            "Cannot remove a stage the task has already entered.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- trajectory: ordered list of {stage, to} objects (same shape "
            "as coord_create_task's trajectory param). Replaces the task's "
            "stored trajectory; supersedes role rows for stages that were "
            "removed; inserts role rows for stages that are added; updates "
            "eligible_owners on stages that remain.\n"
        ),
        {"task_id": str, "trajectory": Any},
    )
    async def set_task_trajectory(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach can set the task trajectory.")
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return _err("task_id is required")
        # Mid-flight reroute: the create-time first-stage-assigned rule
        # doesn't apply (role rows already exist in the assignments
        # table; trajectory is FYI for subsequent stages).
        trajectory, traj_err = _validate_trajectory(
            args.get("trajectory"),
            enforce_first_stage_assigned=False,
        )
        if traj_err:
            return _err(f"invalid trajectory: {traj_err}")
        assert trajectory is not None

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT owner, status, trajectory FROM tasks "
                "WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            t = dict(row)
            current_status = t.get("status")
            if current_status == "archive":
                return _err("archived tasks are read-only")

            new_stages = [s["stage"] for s in trajectory]
            old_stages = _trajectory_stages_from_row(t)
            # v0.3 audit-2026-05-04 item 5: enforce "cannot remove an
            # already-entered stage" by walking the OLD trajectory up
            # to and including the current stage. The previous check
            # only looked at current_status — letting a task in
            # audit_semantics drop an already-passed audit_syntax.
            if current_status in old_stages:
                current_idx = old_stages.index(current_status)
                entered = set(old_stages[: current_idx + 1])
                removed_entered = entered - set(new_stages)
                if removed_entered:
                    return _err(
                        "cannot remove already-entered stages: "
                        + ",".join(sorted(removed_entered))
                        + ". Use coord_approve_stage to move forward "
                        "first, then reroute."
                    )

            removed = [s for s in old_stages if s not in new_stages]
            now_iso = _now_iso()

            # Deactivate role rows for removed stages. The schema's
            # `superseded_by` is a self-FK to a replacement row;
            # trajectory removal has none, so we use `completed_at =
            # now()` to drop the row from active filters
            # (`completed_at IS NULL AND superseded_by IS NULL`).
            # This avoids the FK violation that `superseded_by = -1`
            # produced under `PRAGMA foreign_keys = ON`.
            role_for_stage = {
                "plan": "planner",
                "execute": "executor",
                "audit_syntax": "auditor_syntax",
                "audit_semantics": "auditor_semantics",
                "ship": "shipper",
            }
            # v0.3.6: collect displaced assignees from BOTH removed
            # stages AND in-place eligible_owners changes on remaining
            # stages, so the previous assignees get a stand-down wake
            # after commit.
            from server.kanban import (
                collect_superseded_role_owners,
                send_role_stand_down,
            )
            stand_down_plan: list[tuple[str, list[str], list[str]]] = []
            for stage in removed:
                role = role_for_stage[stage]
                displaced = await collect_superseded_role_owners(
                    c, task_id=task_id, role=role, new_row_id=None,
                )
                if displaced:
                    stand_down_plan.append((role, displaced, []))
                await c.execute(
                    "UPDATE task_role_assignments "
                    "SET completed_at = ? "
                    "WHERE task_id = ? AND role = ? "
                    "AND completed_at IS NULL AND superseded_by IS NULL",
                    (now_iso, task_id, role),
                )

            # Upsert per-stage role rows for the new trajectory.
            for entry in trajectory:
                stage = entry["stage"]
                to_list: list[str] = entry.get("to") or []
                role = role_for_stage[stage]
                eligible_json = json.dumps(to_list, separators=(",", ":"))
                # Audit-stage focus from the new entry (when present).
                # Validator already rejected audit_semantics with
                # non-empty `to` and no focus. For audit_syntax (focus
                # optional) and unchanged-focus semantic re-routes,
                # entry.focus may be absent — preserve the existing
                # row's focus by passing None into the merge below.
                new_focus: str | None = entry.get("focus") or None
                # Find the active role row (if any). If none, insert fresh.
                cur = await c.execute(
                    "SELECT id, owner, eligible_owners, focus "
                    "FROM task_role_assignments "
                    "WHERE task_id = ? AND role = ? "
                    "AND completed_at IS NULL AND superseded_by IS NULL "
                    "ORDER BY assigned_at DESC LIMIT 1",
                    (task_id, role),
                )
                rrow = await cur.fetchone()
                if rrow is None:
                    if len(to_list) == 1:
                        await c.execute(
                            "INSERT INTO task_role_assignments "
                            "(task_id, role, eligible_owners, owner, "
                            "assigned_at, claimed_at, focus) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (task_id, role, eligible_json, to_list[0],
                             now_iso, now_iso, new_focus),
                        )
                    else:
                        await c.execute(
                            "INSERT INTO task_role_assignments "
                            "(task_id, role, eligible_owners, owner, "
                            "assigned_at, focus) "
                            "VALUES (?, ?, ?, NULL, ?, ?)",
                            (task_id, role, eligible_json, now_iso, new_focus),
                        )
                else:
                    # Diff prior assignees vs new to_list; anyone who's
                    # dropped gets a stand-down. Same-slot keeps
                    # working without a spurious ping.
                    rd = dict(rrow)
                    prior: list[str] = []
                    if rd.get("owner"):
                        prior = [str(rd["owner"])]
                    else:
                        try:
                            lst = json.loads(rd.get("eligible_owners") or "[]")
                            if isinstance(lst, list):
                                prior = [str(s) for s in lst if isinstance(s, str)]
                        except Exception:
                            prior = []
                    dropped = [s for s in prior if s not in to_list]
                    if dropped:
                        stand_down_plan.append((role, dropped, list(to_list)))
                    # Update eligible_owners on the existing active row;
                    # don't disturb owner / claimed_at if already set.
                    # Preserve existing `focus` when the new entry omits
                    # it; overwrite when the new entry provides one.
                    if new_focus is not None:
                        await c.execute(
                            "UPDATE task_role_assignments "
                            "SET eligible_owners = ?, focus = ? WHERE id = ?",
                            (eligible_json, new_focus, rd["id"]),
                        )
                    else:
                        await c.execute(
                            "UPDATE task_role_assignments "
                            "SET eligible_owners = ? WHERE id = ?",
                            (eligible_json, rd["id"]),
                        )

            traj_json = json.dumps(trajectory, separators=(",", ":"))
            await c.execute(
                "UPDATE tasks SET trajectory = ? "
                "WHERE id = ? AND project_id = ?",
                (traj_json, task_id, project_id),
            )
            await c.commit()
        finally:
            await c.close()

        # Post-commit stand-down wakes for displaced assignees.
        for role, displaced, new_owners in stand_down_plan:
            try:
                await send_role_stand_down(
                    task_id=task_id,
                    role=role,
                    displaced=displaced,
                    new_owners=new_owners,
                )
            except Exception:
                pass

        await bus.publish({
            "ts": _now_iso(),
            "agent_id": caller_id,
            "type": "task_trajectory_changed",
            "task_id": task_id,
            "trajectory": trajectory,
            "to": t.get("owner"),
        })
        return _ok(
            f"Task {task_id} trajectory updated: "
            f"[{', '.join(s['stage'] for s in trajectory)}]. The "
            f"kanban superseded role rows for removed stages and "
            f"inserted rows for added stages. Displaced Players "
            f"(if any) get a stand-down wake; new candidates get "
            f"role-call wakes if their stage is currently active. "
            f"Wakes carry task context; use coord_send_message only "
            f"if the Players need additional background beyond the "
            f"task description."
        )

    @tool(
        "coord_set_task_workflow",
        (
            "Coach-only. Set the workflow tag (and optional tracking_reason) "
            "on a task. Workflow shapes prompt wording (code / research / "
            "writing / marketing / ops / generic) but does NOT drive "
            "routing — use coord_set_task_trajectory for that.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- workflow: code | research | writing | marketing | ops | generic\n"
            "- tracking_reason: optional informational tag"
        ),
        {
            "task_id": str,
            "workflow": str,
            "tracking_reason": str,
        },
    )
    async def set_task_workflow(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("Only Coach can set task workflow.")
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return _err("task_id is required")
        workflow = (args.get("workflow") or "").strip().lower()
        if workflow and workflow not in WORKFLOW_TYPES:
            return _err(
                f"invalid workflow '{workflow}' "
                f"(must be one of {sorted(WORKFLOW_TYPES)})"
            )
        tracking_reason_raw = args.get("tracking_reason")
        tracking_reason = (
            str(tracking_reason_raw).strip()
            if tracking_reason_raw else ""
        )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT owner, status, workflow, tracking_reason "
                "FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            t = dict(row)
            if t.get("status") == "archive":
                return _err("archived tasks are read-only")
            next_workflow = workflow or (t.get("workflow") or "generic")
            next_reason = tracking_reason or t.get("tracking_reason")
            await c.execute(
                "UPDATE tasks SET workflow = ?, tracking_reason = ? "
                "WHERE id = ? AND project_id = ?",
                (next_workflow, next_reason, task_id, project_id),
            )
            await c.commit()
        finally:
            await c.close()

        await bus.publish({
            "ts": _now_iso(),
            "agent_id": caller_id,
            "type": "task_workflow_set",
            "task_id": task_id,
            "workflow": next_workflow,
            "tracking_reason": next_reason,
            "to": t.get("owner"),
        })
        return _ok(
            f"task {task_id} workflow={next_workflow}"
            + (f", tracking_reason={next_reason}" if next_reason else "")
        )

    # ------------------------------------------------------------------
    # Kanban v2 (Docs/kanban-specs-v2.md §7.1) — single transition tool
    # plus deliberate-archive, role-complete, and plan-review-request.
    # ------------------------------------------------------------------

    @tool(
        "coord_approve_stage",
        (
            "Coach-only. Single transition tool for kanban v2. Coach "
            "explicitly authorizes the next stage transition, names the "
            "assignee, and provides the wake prompt. Replaces v1's "
            "coord_advance_task_stage and the four coord_assign_* tools "
            "in one shape — every stage move is a Coach decision.\n"
            "\n"
            "Validates the transition against the state machine (plan → "
            "execute → audit_syntax → audit_semantics → ship → verify → "
            "archive, plus skip-verify ship→archive, revert "
            "audit_*→execute / verify→execute, and execute→archive).\n"
            "\n"
            "ASSIGNEE: required for any non-archive next_stage; pass a "
            "single Player slot ('p3') — v2 has no pool path. For "
            "next_stage='archive' the assignee is rejected (archive has "
            "no role; use coord_archive_task for the user-facing summary "
            "path).\n"
            "\n"
            "NOTE: optional brief that becomes the assignee's wake "
            "prompt verbatim. Use this to frame the work — what to "
            "look at, what changed, what you want them to do. When "
            "Coach has noticed a deviation, prefix the note with a "
            "structured `[deviation: <one-line reason>]` tag — the "
            "instrumentation in §22 of the kanban v2 spec uses this to "
            "measure whether deviations are caught at push time vs "
            "audit time.\n"
            "\n"
            "Atomically: stamps `last_stage_change_at`; deactivates any "
            "prior active role row at the target stage (with stand-down "
            "wake to displaced Player); deactivates the source-stage "
            "role row if Coach is overriding without source completion "
            "(also stand-down); plants a fresh role row owned by the "
            "named assignee; emits `task_stage_changed` + "
            "`task_role_assigned`; fires the wake.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- next_stage: target stage (kanban value)\n"
            "- assignee: single Player slot (required when next_stage "
            "is not 'archive')\n"
            "- note: optional wake prompt; carried on `task_stage_changed`\n"
            "- success_criteria: optional, only applied at plan→execute "
            "transitions. Updates the task's stored definition of done "
            "based on what you learned from reading the planner's spec. "
            "Replaces any value set at coord_create_task time. Surfaces "
            "in the auditor's wake context and is echoed back to you "
            "when you advance audit→ship. Ignored on transitions other "
            "than plan→execute."
        ),
        {
            "task_id": str, "next_stage": str,
            "assignee": str, "note": str,
            "success_criteria": str,
        },
    )
    async def approve_stage(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "coord_approve_stage is Coach-only. Players don't drive "
                "stage transitions; complete your role's tool and Coach "
                "will review."
            )
        task_id = (args.get("task_id") or "").strip()
        next_stage = (args.get("next_stage") or "").strip().lower()
        assignee_raw = (args.get("assignee") or "").strip().lower()
        note = (args.get("note") or "").strip()
        success_criteria_in = (args.get("success_criteria") or "").strip()
        if not task_id:
            return _err("task_id is required")
        if next_stage not in ALL_KANBAN_STAGES:
            return _err(
                f"invalid next_stage '{next_stage}' (must be one of "
                f"{sorted(ALL_KANBAN_STAGES)})"
            )
        if next_stage == "archive":
            if assignee_raw:
                return _err(
                    "next_stage='archive' takes no assignee — archive "
                    "has no role. Drop `assignee` (or call "
                    "coord_archive_task(task_id, summary) for the "
                    "user-facing wrap-up path)."
                )
            assignee: str | None = None
        else:
            if not assignee_raw:
                return _err(
                    f"next_stage='{next_stage}' requires an assignee "
                    f"(single Player slot). Pools are FYI only in v2; "
                    f"pick a named Player from the trajectory's `to` "
                    f"hint or another available slot."
                )
            if "," in assignee_raw or "[" in assignee_raw:
                return _err(
                    "v2 takes a single assignee slot per transition — "
                    "pools are FYI only. Pick one Player explicitly."
                )
            if (
                assignee_raw not in VALID_RECIPIENTS
                or assignee_raw in ("coach", "broadcast")
            ):
                return _err(
                    f"assignee must be a Player slot (p1..p10), "
                    f"not {assignee_raw!r}"
                )
            if await _is_locked(assignee_raw):
                return _err(
                    f"Player {assignee_raw} is locked; pick an "
                    f"unlocked Player."
                )
            assignee = assignee_raw

        from server.kanban import (
            _role_for_stage as _kanban_role_for_stage,
            collect_superseded_role_owners,
            send_role_stand_down,
        )
        target_role = (
            _kanban_role_for_stage(next_stage) if next_stage != "archive" else None
        )

        project_id = await resolve_active_project()
        c = await configured_conn()
        displaced_target: list[str] = []
        displaced_source: list[str] = []
        old_status: str | None = None
        ship_verify_context = ""
        try:
            cur = await c.execute(
                "SELECT status, owner, success_criteria FROM tasks "
                "WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            t = dict(row)
            old_status = t["status"]
            old_owner = t.get("owner")
            stored_success_criteria = (t.get("success_criteria") or "").strip()
            if old_status == "archive":
                return _err(
                    f"task {task_id} is already archived; archived "
                    f"tasks are read-only."
                )
            # v2 §7.1 same-stage allowance. When a task was created with
            # a pool/empty first-stage `to`, the harness sets
            # tasks.status to that first stage but plants no role row;
            # Coach's first approve_stage call has next_stage == current
            # status. _valid_transition rejects same-stage by default, so
            # we special-case: allow same-stage IFF no active role row
            # exists at the target stage. If a row already exists, this
            # is a normal supersede attempt — reject to keep the API
            # explicit (the caller should approve into the next stage,
            # not re-plant the same one).
            is_same_stage_plant = False
            if (
                old_status == next_stage
                and next_stage != "archive"
                and target_role is not None
            ):
                cur = await c.execute(
                    "SELECT 1 FROM task_role_assignments "
                    "WHERE task_id = ? AND role = ? "
                    "AND completed_at IS NULL AND superseded_by IS NULL "
                    "LIMIT 1",
                    (task_id, target_role),
                )
                if await cur.fetchone():
                    return _err(
                        f"task {task_id} is already in {next_stage!r} "
                        f"with an active {target_role} role. Same-stage "
                        f"approve_stage is only valid as the first plant "
                        f"when the task was created with a pool/empty "
                        f"first-stage `to`. Approve into the next stage "
                        f"instead, or call coord_set_task_trajectory if "
                        f"you need to reroute."
                    )
                is_same_stage_plant = True
            if not is_same_stage_plant and not _valid_transition(
                old_status, next_stage
            ):
                return _err(
                    f"invalid transition: {old_status} → {next_stage}"
                )

            if old_status == "ship" and next_stage == "verify":
                context, error = await _ship_verify_context_or_error(
                    c, task_id, note,
                )
                if error:
                    return _err(error)
                ship_verify_context = context or ""

            now = _now_iso()
            new_role_id: int | None = None
            if next_stage == "archive":
                # Mark every active role row complete on archive — no
                # roles persist into archive. Capture nothing for stand-
                # down (archive isn't a reassignment). The user-facing
                # summary path is coord_archive_task; this branch is the
                # "skip the wrap-up" cancellation route.
                await c.execute(
                    "UPDATE task_role_assignments "
                    "SET completed_at = ? "
                    "WHERE task_id = ? "
                    "AND completed_at IS NULL AND superseded_by IS NULL",
                    (now, task_id),
                )
                await c.execute(
                    "UPDATE tasks SET status = 'archive', "
                    "completed_at = ?, archived_at = ?, "
                    "last_stage_change_at = ?, stale_alert_at = NULL, "
                    "stall_escalation_level = 0 "
                    "WHERE id = ? AND project_id = ?",
                    (now, now, now, task_id, project_id),
                )
                # Free any agent pointing at this task. See the
                # matching comment in the coord_approve_stage archive
                # path above — broader than `old_owner` alone.
                await c.execute(
                    "UPDATE agents SET current_task_id = NULL "
                    "WHERE current_task_id = ?",
                    (task_id,),
                )
            else:
                # Source-stage role row: deactivate when Coach is
                # overriding without source completion (still active).
                # The displaced slot gets a stand-down wake post-commit
                # so they know to stop work.
                source_role = _kanban_role_for_stage(old_status)
                if source_role:
                    displaced_source = await collect_superseded_role_owners(
                        c, task_id=task_id, role=source_role,
                        new_row_id=None,
                    )
                    if displaced_source:
                        await c.execute(
                            "UPDATE task_role_assignments "
                            "SET completed_at = ? "
                            "WHERE task_id = ? AND role = ? "
                            "AND completed_at IS NULL "
                            "AND superseded_by IS NULL",
                            (now, task_id, source_role),
                        )
                # Target-stage role row supersede + plant. Capture
                # displaced BEFORE INSERT so we can wake them after.
                pre_displaced = await collect_superseded_role_owners(
                    c, task_id=task_id, role=target_role, new_row_id=None,
                )
                # Plant the new hard-assign row.
                cur = await c.execute(
                    "INSERT INTO task_role_assignments "
                    "(task_id, role, eligible_owners, owner, "
                    "assigned_at, claimed_at) "
                    "VALUES (?, ?, '[]', ?, ?, ?)",
                    (task_id, target_role, assignee, now, now),
                )
                new_role_id = cur.lastrowid
                # Supersede prior active rows for the same (task,role).
                await c.execute(
                    "UPDATE task_role_assignments "
                    "SET superseded_by = ? "
                    "WHERE task_id = ? AND role = ? AND id != ? "
                    "AND completed_at IS NULL AND superseded_by IS NULL",
                    (new_role_id, task_id, target_role, new_role_id),
                )
                displaced_target = [
                    s for s in pre_displaced if s != assignee
                ]
                # Update tasks.status + owner.
                tasks_owner: str | None = old_owner
                if next_stage == "execute":
                    tasks_owner = assignee
                elif old_status == "execute" and next_stage != "execute":
                    # Leaving execute — keep tasks.owner so the executor's
                    # current_task_id mapping isn't disturbed (the executor
                    # may still need to reopen the task on a later
                    # re-execute approval). The owner column is sticky
                    # from execute through subsequent stages until archive.
                    tasks_owner = old_owner
                await c.execute(
                    "UPDATE tasks SET status = ?, owner = ?, "
                    "last_stage_change_at = ?, stale_alert_at = NULL, "
                    "stall_escalation_level = 0 "
                    "WHERE id = ? AND project_id = ?",
                    (next_stage, tasks_owner, now, task_id, project_id),
                )
                # success_criteria is the Coach-authored "definition of
                # done" for the task. Only updated at plan→execute —
                # that's the moment Coach has read the planner's spec
                # and is in the best position to crystallize the bar
                # the work will be evaluated against. Updates at other
                # transitions are silently ignored to keep the field
                # stable across execute/audit/ship; if Coach realizes
                # the criteria is wrong mid-flight they can revert to
                # plan and re-approve to update it.
                if (
                    old_status == "plan"
                    and next_stage == "execute"
                    and success_criteria_in
                ):
                    await c.execute(
                        "UPDATE tasks SET success_criteria = ? "
                        "WHERE id = ? AND project_id = ?",
                        (success_criteria_in, task_id, project_id),
                    )
                    stored_success_criteria = success_criteria_in
                # Propagate current_task_id when entering execute with
                # a hard-assigned executor (mirror coord_create_task).
                if next_stage == "execute":
                    await _set_agent_current_task_if_free_or_stale(
                        c, assignee, task_id,
                    )
                for displaced in set(displaced_source + displaced_target):
                    if displaced != assignee:
                        await _reset_agent_idle_tools(c, displaced)
                await _set_agent_role_tools(c, assignee, target_role)
            await c.commit()
        finally:
            await c.close()

        # v2 §22.1 push-time deviation instrumentation. When Coach
        # approves a stage with a deviation tag/phrase in the note AND
        # the source stage is `execute` (Coach is reviewing executor
        # work), insert a `deviations_log{noticed_at='push'}` row. The
        # source executor is the slot whose work Coach noticed the
        # drift in — read from tasks.owner BEFORE this transition (the
        # executor role row is sticky through subsequent stages, but
        # `old_owner` was captured before the column was overwritten).
        # Failure-isolated: a DB hiccup never blocks the approval.
        deviation_description: str | None = None
        if (
            old_status == "execute"
            and note
            and old_owner
            and next_stage != "archive"  # archive path emits its own signal
        ):
            try:
                deviation_description = _extract_deviation_description(note)
                if deviation_description:
                    cdev = await configured_conn()
                    try:
                        await cdev.execute(
                            "INSERT INTO deviations_log "
                            "(project_id, ts, task_id, executor, "
                            " noticed_at, description) "
                            "VALUES (?, ?, ?, ?, 'push', ?)",
                            (
                                project_id, _now_iso(),
                                task_id, old_owner,
                                deviation_description,
                            ),
                        )
                        await cdev.commit()
                    finally:
                        await cdev.close()
            except Exception:
                # Failure-isolated instrumentation — never block the
                # approval. Bare except matches the surrounding pattern.
                pass

        ts = _now_iso()
        # Emit the stage-change event — only when the stage actually
        # changed. Same-stage plants (v2 §7.1 first-plant after pool/
        # empty creation) emit `task_role_assigned` only, since the
        # board didn't move.
        if not is_same_stage_plant:
            await bus.publish({
                "ts": ts,
                "agent_id": caller_id,
                "type": "task_stage_changed",
                "task_id": task_id,
                "from": old_status,
                "to": next_stage,
                "reason": "coord_approve_stage",
                "assignee": assignee,
                "note": note or None,
                "owner": assignee if next_stage == "execute" else None,
            })
        # Emit the role-assignment event when a role was planted.
        if next_stage != "archive" and target_role and assignee:
            await bus.publish({
                "ts": ts,
                "agent_id": caller_id,
                "type": "task_role_assigned",
                "task_id": task_id,
                "role": target_role,
                "owner": assignee,
                "to": assignee,
                "note": note or None,
            })

        # v2 §5.3 self-review warning: when Coach assigns the same
        # Player to an auditor role that's also the task's executor,
        # surface the pattern. Informational — Coach can choose this
        # deliberately (small team, specialist scarcity); the Telegram
        # bridge formats the warning so the human sees it.
        if (
            target_role in ("auditor_syntax", "auditor_semantics")
            and assignee
            and old_owner
            and assignee == old_owner
        ):
            kind = (
                "syntax" if target_role == "auditor_syntax" else "semantics"
            )
            await bus.publish({
                "ts": ts,
                "agent_id": caller_id,
                "type": "audit_self_review_warning",
                "task_id": task_id,
                "kind": kind,
                "auditor_id": assignee,
                "executor_id": old_owner,
                "to": "coach",
            })

        # Stand-down wakes. Source-stage displacement is the more
        # disruptive one (Player was actively working) so it goes
        # first. Same-slot refresh (assignee was already the active
        # role owner) is filtered by send_role_stand_down.
        if displaced_source:
            try:
                await send_role_stand_down(
                    task_id=task_id,
                    role=_kanban_role_for_stage(old_status) or "",
                    displaced=displaced_source,
                    new_owners=[],
                )
            except Exception:
                pass
        if displaced_target and target_role:
            try:
                await send_role_stand_down(
                    task_id=task_id,
                    role=target_role,
                    displaced=displaced_target,
                    new_owners=[assignee] if assignee else [],
                )
            except Exception:
                pass

        # Wake the new assignee with `note` as the prompt body. Coach's
        # note is the verbatim brief — the lifecycle policy teaches
        # Coach to write it like a hand-off, not a status comment.
        # The harness fallback is a v2-stripped fact line; the canonical
        # turn-end reminder is appended in either case.
        if next_stage != "archive" and assignee:
            from server.agents import maybe_wake_agent
            base_wake = note or (
                f"Coach approved your move on task {task_id} to stage "
                f"{next_stage!r} as {target_role}."
            )
            if ship_verify_context:
                base_wake = f"{base_wake}\n\n{ship_verify_context}"
            wake_body = _with_player_reminder(base_wake)
            try:
                await maybe_wake_agent(
                    assignee, wake_body,
                    bypass_debounce=True,
                    wake_source="kanban_approval",
                )
            except Exception:
                pass

        suffix_note = f" — {note[:120]}" if note else ""
        if next_stage == "archive":
            return _ok(
                f"Approved {task_id}: {old_status} → archive (no "
                f"summary){suffix_note}. NO user-facing summary fires "
                f"for this path. If the task ends with a deliverable "
                f"or decision the user needs to know, call "
                f"coord_archive_task(task_id, summary) instead so the "
                f"summary lands in chat + Telegram."
            )
        if is_same_stage_plant:
            return _ok(
                f"Planted {assignee} as {target_role} on {task_id} "
                f"(stage {next_stage!r}, first plant after pool/empty "
                f"create){suffix_note}. Role row planted, wake fired."
                + (" Do NOT follow up with coord_send_message; the wake covers it." if note else
                   " No note passed — use coord_send_message if the assignee needs context.")
            )
        # Ship-stage echo: when Coach is approving audit→ship, surface
        # the task's stored definition of done so Coach evaluates the
        # final gate against their own prior, not from memory. Only
        # rendered when criteria is set — silent when unset to preserve
        # the optional-everywhere posture.
        criteria_echo = ""
        if next_stage == "ship" and stored_success_criteria:
            criteria_echo = (
                f" You defined done as: {stored_success_criteria}"
            )
        ship_verify_echo = (
            f" {ship_verify_context}" if ship_verify_context else ""
        )
        return _ok(
            f"Approved {task_id}: {old_status} → {next_stage} "
            f"({target_role} → {assignee}){suffix_note}. "
            + (
                f"Planted {assignee}'s role row, woke them with your "
                f"note as the brief. Do NOT follow up with "
                f"coord_send_message; the wake covers it."
                if note else
                f"Planted {assignee}'s role row; woke them with a "
                f"generic brief — use note= to pass context directly."
            )
            + f"{criteria_echo}{ship_verify_echo}"
        )

    @tool(
        "coord_archive_task",
        (
            "Coach-only. Deliberately archive a task with a user-facing "
            "summary. Replaces v1's auto-archive on trajectory completion "
            "— v2 has no auto-archive; every task ends with a Coach-"
            "written wrap-up.\n"
            "\n"
            "Use this when:\n"
            "- a shipper completed and you want to publish the result\n"
            "- a research / writing / ops task delivered and you want "
            "to summarise the outcome\n"
            "- you're cancelling a task and want the user to know why\n"
            "\n"
            "The summary is your deliberate user-facing artifact: write "
            "it like the user just asked 'what happened with that task?' "
            "It lands as a `.sys` row in your pane and is forwarded to "
            "Telegram (when the originating turn was user-triggered, "
            "matching the existing user-triggered-turn filter).\n"
            "\n"
            "Effects: marks every active role row complete; transitions "
            "the task to archive; stamps `archived_at` and "
            "`completed_at`; emits `task_archived{summary, ...}`.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- summary: user-facing wrap-up (required, max 5000 chars)"
        ),
        {"task_id": str, "summary": str},
    )
    async def archive_task(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "coord_archive_task is Coach-only. Players don't archive "
                "tasks — Coach reviews and decides on the wrap-up."
            )
        task_id = (args.get("task_id") or "").strip()
        summary = (args.get("summary") or "").strip()
        if not task_id:
            return _err("task_id is required")
        if not summary:
            return _err(
                "summary is required — this is the user-facing wrap-up. "
                "If you really want to close without a summary, use "
                "coord_approve_stage(next_stage='archive') instead."
            )
        if len(summary) > 5000:
            return _err(
                f"summary too long ({len(summary)} chars, max 5000)"
            )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status, owner FROM tasks "
                "WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            t = dict(row)
            old_status = t["status"]
            old_owner = t.get("owner")
            if old_status == "archive":
                return _err(
                    f"task {task_id} is already archived; archived "
                    f"tasks are read-only."
                )
            now = _now_iso()
            await c.execute(
                "UPDATE task_role_assignments "
                "SET completed_at = ? "
                "WHERE task_id = ? "
                "AND completed_at IS NULL AND superseded_by IS NULL",
                (now, task_id),
            )
            await c.execute(
                "UPDATE tasks SET status = 'archive', "
                "completed_at = ?, archived_at = ?, "
                "last_stage_change_at = ?, stale_alert_at = NULL, "
                "stall_escalation_level = 0 "
                "WHERE id = ? AND project_id = ?",
                (now, now, now, task_id, project_id),
            )
            # Free any agent pointing at this task. See the matching
            # comment in the coord_approve_stage archive path above —
            # broader than `old_owner` alone so role assignees (shipper,
            # auditor) without ownership of the planner row also get
            # cleared.
            await c.execute(
                "UPDATE agents SET current_task_id = NULL "
                "WHERE current_task_id = ?",
                (task_id,),
            )
            await c.commit()
        finally:
            await c.close()

        ts = _now_iso()
        # task_stage_changed for the board UI + project_events log.
        await bus.publish({
            "ts": ts,
            "agent_id": caller_id,
            "type": "task_stage_changed",
            "task_id": task_id,
            "from": old_status,
            "to": "archive",
            "reason": "coord_archive_task",
            "owner": old_owner,
        })
        # task_archived carries the user-facing summary; forwarded to
        # Telegram per the existing user-triggered-turn filter.
        await bus.publish({
            "ts": ts,
            "agent_id": caller_id,
            "type": "task_archived",
            "task_id": task_id,
            "summary": summary,
            "body": summary,
            "owner": old_owner,
        })
        return _ok(
            f"Archived {task_id} with user-facing summary "
            f"({len(summary)} chars). All active role rows closed; "
            f"task is read-only. The summary will surface in your pane "
            f"and forward to Telegram (when the originating turn was "
            f"user-triggered)."
        )

    @tool(
        "coord_role_complete",
        (
            "**Your message to Coach that this role is done.** "
            "Player-only. Generic completion for roles whose real "
            "work happens via other tools — non-git executors who "
            "wrote a file via Write / coord_save_output / "
            "coord_write_knowledge, or shippers who merged / "
            "published / sent via Bash or external CLIs. Calling "
            "this tool IS the act of telling Coach you're done — "
            "without it, Coach has no idea your work landed. "
            "Writing the file or completing the merge without "
            "calling this is silence; the disk-write + skipped-"
            "call pattern is the #1 stall cause.\n"
            "\n"
            "Emits `task_role_completed` with your `message_to_coach` "
            "and wakes Coach in real time (v2 §7.2.1). The event also "
            "lands in Coach's pane immediately — Coach reads + decides "
            "without waiting for the next recurrence tick.\n"
            "\n"
            "Code work uses coord_commit_push (which carries its own "
            "completion). Spec writes use coord_write_task_spec. Audit "
            "reports use coord_submit_audit_report. All four wake Coach "
            "in real time. This tool is for the rest.\n"
            "\n"
            "ARTIFACT GATE: when you pass `artifact_path`, the harness "
            "verifies the file exists on disk under the project root. "
            "Don't pre-declare a path you haven't written yet — save "
            "the file first, then call this. Resolution mirrors "
            "v1.3.14's coord_complete_execution.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- message_to_coach: one-line note delivered to Coach in "
            "real time (required — what you produced, any caveats, "
            "what the next person should know). Carried verbatim in "
            "the `task_role_completed` event payload AND used as the "
            "wake reason for Coach.\n"
            "- artifact_path: optional durable path; verified on disk"
        ),
        # Raw JSON schema so artifact_path is NOT in "required" — the
        # dict-shorthand would mark every key required, forcing callers
        # (especially shippers with no file deliverable) to pass an
        # empty string or get rejected by the MCP framework before the
        # handler even runs.
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "message_to_coach": {"type": "string"},
                "artifact_path": {"type": "string"},
            },
            "required": ["task_id", "message_to_coach"],
        },
    )
    async def role_complete(args: dict[str, Any]) -> dict[str, Any]:
        if caller_is_coach:
            return _err(
                "Coach doesn't execute work. Assign a Player via "
                "coord_approve_stage."
            )
        task_id = (args.get("task_id") or "").strip()
        message_to_coach = (args.get("message_to_coach") or "").strip()
        artifact_path = (args.get("artifact_path") or "").strip()
        if not task_id:
            return _err("task_id is required")
        if not message_to_coach:
            return _err(
                "message_to_coach is required — it's the wake reason "
                "Coach reads in real time to decide what's next. Tell "
                "Coach what you produced, any caveats, what the next "
                "person should know."
            )
        if len(message_to_coach) > 2000:
            return _err(
                f"message_to_coach too long ({len(message_to_coach)} "
                f"chars, max 2000)"
            )
        if len(artifact_path) > 1000:
            return _err("artifact_path too long (max 1000 chars)")

        project_id = await resolve_active_project()

        # Artifact-gate (v1.3.14 pattern, copied from coord_complete_execution).
        artifact_resolved: str | None = None
        if artifact_path:
            from pathlib import Path
            from server.paths import project_paths
            pp = project_paths(project_id)
            try:
                project_root = pp.root.resolve()
            except Exception as exc:
                return _err(
                    f"could not resolve project root: {exc}"
                )
            try:
                candidate = Path(artifact_path)
                if not candidate.is_absolute():
                    candidate = pp.root / candidate
                resolved = candidate.resolve()
            except Exception as exc:
                return _err(
                    f"could not resolve artifact_path "
                    f"{artifact_path!r}: {exc}"
                )
            try:
                resolved.relative_to(project_root)
            except ValueError:
                return _err(
                    f"artifact_path {artifact_path!r} resolves outside "
                    f"the project root ({project_root}). Pass a path "
                    f"under the project."
                )
            if not resolved.exists():
                return _err(
                    f"artifact_path {artifact_path!r} does not exist "
                    f"on disk (resolved to {resolved}). Save the "
                    f"deliverable first via Write / coord_save_output "
                    f"/ coord_write_knowledge / coord_write_decision, "
                    f"then call coord_role_complete again."
                )
            artifact_resolved = str(resolved)

        from server.kanban import _role_for_stage as _kanban_role_for_stage
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status, owner FROM tasks "
                "WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(
                    f"task {task_id} not found in active project"
                )
            t = dict(row)
            current_stage = t.get("status")
            if current_stage == "verify":
                return _err(
                    "verifier roles must call "
                    "coord_submit_verification_report(task_id=..., "
                    "verdict='pass' or 'fail', body=...) instead of "
                    "coord_role_complete so the verification verdict "
                    "and report artifact are recorded."
                )
            # 2026-05-12 graceful post-archive completion: a Player who
            # polls a long-running deploy (ship stage on Zeabur takes
            # 30-35 min) can finish verification AFTER Coach (or the
            # rung-4 stall sweeper) has archived the task. The prior
            # behaviour hard-rejected, silently losing the Player's
            # verification report. New shape: detect archive, look up
            # the role the Player most recently held (regardless of
            # completed_at — the archive paths auto-close all open
            # roles), and accept the completion as a post-archive
            # report. Coach still gets the message + artifact via the
            # event fan-out + wake below.
            is_archived = current_stage == "archive"
            target_role = _kanban_role_for_stage(current_stage)
            if is_archived:
                # Find the caller's most recently-active role on this
                # task. `superseded_by IS NULL` keeps us from accepting
                # completion from a Player whom Coach had explicitly
                # reassigned away. We pull `completed_at` too so we can
                # tell whether the archive path already auto-closed
                # this role (the canonical case) vs. a defensive edge
                # where it didn't.
                cur = await c.execute(
                    "SELECT id, role, completed_at "
                    "FROM task_role_assignments "
                    "WHERE task_id = ? AND owner = ? "
                    "AND superseded_by IS NULL "
                    "ORDER BY assigned_at DESC LIMIT 1",
                    (task_id, caller_id),
                )
                role_row = await cur.fetchone()
                if not role_row:
                    return _err(
                        f"task {task_id} is archived and you held no "
                        f"active role on it — nothing to complete. "
                        f"(If you had a role and it was superseded by "
                        f"another Player, Coach handled the reassign; "
                        f"no further action needed.)"
                    )
                role_data = dict(role_row)
                role_id = role_data["id"]
                target_role = role_data["role"]
                # Canonical case: the archive path already stamped
                # `completed_at = <archive ts>` on this row when it
                # closed all open roles. Leave that timestamp alone so
                # the audit trail preserves the archive boundary.
                #
                # Defensive case: if `completed_at` is somehow still
                # NULL (a future archive path forgets to close roles,
                # or schema drift), stamp it now. Without this, the
                # role row leaks as "active" forever even though the
                # task is archived — breaks any downstream "open roles
                # by Player" rollup.
                if not role_data.get("completed_at"):
                    await c.execute(
                        "UPDATE task_role_assignments "
                        "SET completed_at = ? WHERE id = ?",
                        (_now_iso(), role_id),
                    )
                await _reset_agent_idle_tools(c, caller_id)
            else:
                if not target_role:
                    return _err(
                        f"task {task_id} stage {current_stage!r} has no "
                        f"role — nothing to complete."
                    )
                cur = await c.execute(
                    "SELECT id FROM task_role_assignments "
                    "WHERE task_id = ? AND role = ? AND owner = ? "
                    "AND completed_at IS NULL AND superseded_by IS NULL "
                    "ORDER BY assigned_at DESC LIMIT 1",
                    (task_id, target_role, caller_id),
                )
                role_row = await cur.fetchone()
                if not role_row:
                    return _err(
                        f"you have no active {target_role} role on task "
                        f"{task_id} — Coach hasn't assigned you, or your "
                        f"role was already completed/superseded. Check "
                        f"coord_my_assignments for what's actionable."
                    )
                role_id = dict(role_row)["id"]
                now = _now_iso()
                await c.execute(
                    "UPDATE task_role_assignments SET completed_at = ? "
                    "WHERE id = ?",
                    (now, role_id),
                )
                await _reset_agent_idle_tools(c, caller_id)
            if artifact_path:
                cur = await c.execute(
                    "SELECT artifacts FROM tasks "
                    "WHERE id = ? AND project_id = ?",
                    (task_id, project_id),
                )
                arow = await cur.fetchone()
                try:
                    artifacts = (
                        json.loads(dict(arow).get("artifacts") or "[]")
                        if arow else []
                    )
                    if not isinstance(artifacts, list):
                        artifacts = []
                except Exception:
                    artifacts = []
                if artifact_path not in artifacts:
                    artifacts.append(artifact_path)
                    await c.execute(
                        "UPDATE tasks SET artifacts = ? "
                        "WHERE id = ? AND project_id = ?",
                        (
                            json.dumps(artifacts, separators=(",", ":")),
                            task_id, project_id,
                        ),
                    )
            await c.commit()
        finally:
            await c.close()

        ts = _now_iso()
        await bus.publish({
            "ts": ts,
            "agent_id": caller_id,
            "type": "task_role_completed",
            "task_id": task_id,
            "role": target_role,
            "owner": caller_id,
            "artifact_path": artifact_path or None,
            "message_to_coach": message_to_coach or None,
            # Flag a post-archive completion so subscribers (Coach
            # rollup, dashboard, telegram bridge) can distinguish a
            # normal in-stage completion from a "task was archived
            # while I finished verifying" report. Coach treats the
            # latter as informational — no stage advance possible.
            "post_archive": is_archived,
            # v2 §7.2.1: route to Coach. The actor's pane already gets
            # the row via the WS-side actor fan-out (`aid`); setting
            # `to: 'coach'` makes the row also land in Coach's pane
            # (real-time + history reload via the /api/events filter).
            "to": "coach",
        })

        # v2 §7.2.1 — wake Coach in real time. The tool's whole reason
        # to exist is "your message to Coach"; without the live wake,
        # Coach only saw it on the next recurrence tick which read as
        # a silent kanban bug from Player + human perspective.
        await _wake_coach_for_completion(
            caller_id=caller_id,
            task_id=task_id,
            role=target_role,
            message_to_coach=message_to_coach,
            artifact_path=artifact_resolved or artifact_path or None,
        )

        artifact_clause = (
            f" → {artifact_path} (verified at {artifact_resolved})"
            if artifact_resolved else ""
        )
        if is_archived:
            return _ok(
                f"Completed {target_role} role on {task_id}"
                f"{artifact_clause} (task was already archived; your "
                f"report is logged for Coach's awareness — no stage "
                f"advance possible). Coach was woken with your "
                f"message_to_coach as context."
            )
        return _ok(
            f"Completed {target_role} role on {task_id}{artifact_clause}. "
            f"Your message reached Coach in real time (Coach was woken "
            f"with your message_to_coach as context). Coach will read, "
            f"decide whether to approve the next stage, request rework, "
            f"or archive, and may reply to you directly."
        )

    @tool(
        "coord_request_plan_review",
        (
            "Coach-only. Wake a Player with plan-mode enabled so they "
            "produce an ExitPlanMode artifact before touching tools. "
            "The plan lands as `pending_plan{route='coach'}` for Coach "
            "to review; once Coach approves (or rewrites), Coach calls "
            "coord_approve_stage with the chosen note to dispatch the "
            "Player into execution.\n"
            "\n"
            "Use for non-trivial work where you'd rather see the "
            "Player's plan up front than read commits + audits at the "
            "end. The wake body references the task's current spec "
            "(when present); the Player composes the plan and submits "
            "via ExitPlanMode (which lands as a pending_plan event the "
            "harness routes to Coach per the v2 plan-mode policy).\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- slot: target Player slot (p1..p10)\n"
            "- note: optional brief that becomes the Player's wake "
            "  prompt verbatim (alongside the plan-mode framing). Use "
            "  this to point them at what to focus on, what tradeoffs "
            "  to surface, what's already been decided. If empty, "
            "  the harness emits a short fact line referencing the "
            "  task's current spec."
        ),
        {"task_id": str, "slot": str, "note": str},
    )
    async def request_plan_review(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err(
                "coord_request_plan_review is Coach-only."
            )
        task_id = (args.get("task_id") or "").strip()
        slot = (args.get("slot") or "").strip().lower()
        if not task_id:
            return _err("task_id is required")
        if (
            slot not in VALID_RECIPIENTS
            or slot in ("coach", "broadcast")
        ):
            return _err(
                f"slot must be a Player slot (p1..p10), not {slot!r}"
            )
        if await _is_locked(slot):
            return _err(
                f"Player {slot} is locked; pick an unlocked Player."
            )

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT title, status, spec_path FROM tasks "
                "WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            t = dict(row)
        finally:
            await c.close()

        title = t.get("title") or "(no title)"
        stage = t.get("status") or "?"
        spec_clause = (
            f" (spec at {t['spec_path']})"
            if t.get("spec_path") else " (no spec yet)"
        )
        coach_note = (args.get("note") or "").strip() or None
        plan_mode_framing = (
            f"Plan-mode is enabled: produce an ExitPlanMode artifact "
            f"(plan only, no tool use). Coach reviews before dispatch."
        )
        if coach_note:
            wake_body = f"{coach_note}\n\n{plan_mode_framing}"
        else:
            wake_body = (
                f"Coach is requesting a plan review for task {task_id} "
                f"({title!r}, currently in stage {stage!r})"
                f"{spec_clause}.\n\n{plan_mode_framing}"
            )
        wake_body = _with_player_reminder(wake_body)

        ts = _now_iso()
        await bus.publish({
            "ts": ts,
            "agent_id": caller_id,
            "type": "plan_review_requested",
            "task_id": task_id,
            "slot": slot,
            "to": slot,
        })

        from server.agents import maybe_wake_agent
        try:
            woken = await maybe_wake_agent(
                slot, wake_body,
                bypass_debounce=True,
                wake_source="kanban_plan_review",
                plan_mode=True,
            )
        except Exception:
            woken = False
        if not woken:
            return _ok(
                f"Plan review requested for {task_id} → {slot}, but the "
                f"wake didn't fire (slot may be busy / paused / cost-"
                f"capped). The request was logged; Coach should retry "
                f"after the slot clears or pick another Player."
            )
        return _ok(
            f"Plan review requested for {task_id} → {slot}. {slot} is "
            f"woken with plan-mode enabled; their plan will land as a "
            f"pending_plan event for your review."
        )

    # ------------------------------------------------------------------ Backlog
    # (Docs/kanban-specs-v2.md §4.0) — lightweight pre-plan holding area.

    @tool(
        "coord_propose_task",
        (
            "Propose a task idea to the Backlog. Available to Coach and "
            "all Players. The item lands in the Backlog as a `pending` "
            "entry visible in the kanban Backlog column; Coach triages "
            "it on the next tick via coord_triage_backlog.\n"
            "\n"
            "Use this instead of coord_create_task when the work isn't "
            "ready to start yet — you want Coach to review and decide "
            "whether it fits current priorities before a trajectory is "
            "committed.\n"
            "\n"
            "Params:\n"
            "- title: required. Free-form description of the idea."
        ),
        {"title": str},
    )
    async def propose_task(args: dict[str, Any]) -> dict[str, Any]:
        title = (args.get("title") or "").strip()
        if not title:
            return _err("title is required")

        now_iso = _now_iso()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "INSERT INTO backlog_tasks (title, proposed_by, proposed_at) "
                "VALUES (?, ?, ?)",
                (title, caller_id, now_iso),
            )
            backlog_id = cur.lastrowid
            await c.commit()
        finally:
            await c.close()

        await bus.publish({
            "ts": now_iso,
            "agent_id": caller_id,
            "type": "backlog_task_proposed",
            "id": backlog_id,
            "title": title,
            "proposed_by": caller_id,
        })
        return _ok(
            f"Backlog entry #{backlog_id} created: \"{title}\". "
            "Coach will triage it on the next tick."
        )

    @tool(
        "coord_list_backlog",
        (
            "List entries in the Backlog. Available to Coach and all Players. "
            "Read-only — no side effects.\n"
            "\n"
            "Params:\n"
            "- status: filter on 'pending' (default) / 'promoted' / "
            "'rejected' / 'all' for every status.\n"
            "- limit: max rows to return (default 50, max 200).\n"
            "\n"
            "Returns one line per entry:\n"
            "  #<id>  [<status>]  \"<title>\"  by <proposed_by>, <age> ago\n"
            "  (description indented on a second line when non-empty)\n"
            "\n"
            "Use this to get a mid-turn view of the backlog after "
            "coord_propose_task or coord_triage_backlog — the system-prompt "
            "snapshot at turn start doesn't refresh."
        ),
        {"status": str, "limit": str},
    )
    async def list_backlog(args: dict[str, Any]) -> dict[str, Any]:
        raw_status = (args.get("status") or "").strip().lower() or "pending"
        valid_statuses = {"pending", "promoted", "rejected", "all"}
        if raw_status not in valid_statuses:
            return _err(
                f"status must be one of {sorted(valid_statuses)!r}, "
                f"got {raw_status!r}"
            )

        try:
            limit = max(1, min(200, int(args.get("limit") or 50)))
        except (TypeError, ValueError):
            limit = 50

        where = "" if raw_status == "all" else "WHERE status = ?"
        params: list[Any] = [] if raw_status == "all" else [raw_status]

        c = await configured_conn()
        try:
            cur = await c.execute(
                f"SELECT id, title, proposed_by, proposed_at, status, "
                f"promoted_task_id "
                f"FROM backlog_tasks {where} "
                f"ORDER BY proposed_at DESC LIMIT ?",
                params + [limit],
            )
            rows = await cur.fetchall()
        finally:
            await c.close()

        if not rows:
            return _ok("(no backlog entries)")

        now = datetime.now(timezone.utc)
        lines = []
        for r in rows:
            d = dict(r)
            # Compute human-readable age from proposed_at ISO timestamp.
            age = "?"
            try:
                ts_str = d["proposed_at"]
                if ts_str:
                    ts = datetime.fromisoformat(
                        ts_str.replace("Z", "+00:00")
                    )
                    secs = int((now - ts).total_seconds())
                    if secs < 60:
                        age = f"{secs}s"
                    elif secs < 3600:
                        age = f"{secs // 60}m"
                    elif secs < 86400:
                        age = f"{secs // 3600}h"
                    else:
                        age = f"{secs // 86400}d"
            except Exception:
                pass

            extra = ""
            if d.get("promoted_task_id"):
                extra = f"  → {d['promoted_task_id']}"
            lines.append(
                f"#{d['id']}  [{d['status']}]  \"{d['title']}\"  "
                f"by {d['proposed_by']}, {age} ago{extra}"
            )
        return _ok("\n".join(lines))

    @tool(
        "coord_triage_backlog",
        (
            "Coach-only. Review a Backlog entry and either promote it "
            "to an active task or reject it.\n"
            "\n"
            "Params:\n"
            "- id: required. Backlog entry id (integer).\n"
            "- action: 'promote' or 'reject'.\n"
            "- trajectory: required when action='promote'. Same format "
            "  as coord_create_task — ordered list of {stage, to} "
            "  objects. 'to' must name exactly one Player slot per "
            "  the v2 first-stage rule.\n"
            "- modified_title: optional. Rename the idea on promote.\n"
            "- reason: recommended when action='reject'. Short phrase "
            "  explaining why (stored; surfaces to human if the idea "
            "  was proposed by a human)."
        ),
        {"id": str, "action": str, "trajectory": Any,
         "modified_title": str, "reason": str},
    )
    async def triage_backlog(args: dict[str, Any]) -> dict[str, Any]:
        if not caller_is_coach:
            return _err("coord_triage_backlog is Coach-only.")

        raw_id = (args.get("id") or "").strip()
        if not raw_id:
            return _err("id is required")
        try:
            backlog_id = int(raw_id)
        except ValueError:
            return _err(f"id must be an integer, got {raw_id!r}")

        action = (args.get("action") or "").strip().lower()
        if action not in ("promote", "reject"):
            return _err("action must be 'promote' or 'reject'")

        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id, title, proposed_by, status, priority, "
                "trajectory_json, note, success_criteria "
                "FROM backlog_tasks WHERE id = ?",
                (backlog_id,),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"Backlog entry #{backlog_id} not found")
            entry = dict(row)
            if entry["status"] != "pending":
                return _err(
                    f"Backlog entry #{backlog_id} is already "
                    f"{entry['status']} — nothing to triage."
                )

            title = (args.get("modified_title") or "").strip() or entry["title"]
            now_iso = _now_iso()

            if action == "reject":
                reason = (args.get("reason") or "").strip() or "(no reason given)"
                await c.execute(
                    "UPDATE backlog_tasks SET status='rejected', "
                    "reject_reason=? WHERE id=?",
                    (reason, backlog_id),
                )
                # Notify human proposers via the messages table so it
                # surfaces in Coach's chat pane (§4.0.4).
                if entry["proposed_by"] == "human":
                    await c.execute(
                        "INSERT INTO messages "
                        "(from_id, to_id, project_id, subject, body, sent_at) "
                        "VALUES ('coach', 'coach', ?, ?, ?, ?)",
                        (
                            project_id,
                            f"Backlog rejected: {entry['title'][:80]}",
                            f"Your backlog suggestion \"{entry['title']}\" was "
                            f"rejected by Coach. Reason: {reason}",
                            now_iso,
                        ),
                    )
                await c.commit()
        finally:
            await c.close()

        if action == "reject":
            await bus.publish({
                "ts": now_iso,
                "agent_id": caller_id,
                "type": "backlog_task_rejected",
                "id": backlog_id,
                "title": entry["title"],
                "reason": reason,
            })
            return _ok(
                f"Backlog entry #{backlog_id} (\"{entry['title']}\") rejected. "
                f"Reason: {reason}"
            )

        # action == "promote"
        # Trajectory: args wins; fall back to stored trajectory_json from
        # Coach's coord_create_task call (backlog-first flow).
        trajectory_arg = args.get("trajectory")
        stored_traj_json = entry.get("trajectory_json")
        if not trajectory_arg and stored_traj_json:
            try:
                import json as _json
                trajectory_arg = _json.loads(stored_traj_json)
            except Exception:
                pass
        if not trajectory_arg:
            return _err(
                "trajectory is required when action='promote'. "
                "Provide an ordered list of {stage, to} objects — "
                "same format as coord_create_task. (This entry has "
                "no stored trajectory from creation time.)"
            )
        trajectory = trajectory_arg
        if isinstance(trajectory, str):
            try:
                import json as _json
                trajectory = _json.loads(trajectory)
            except Exception:
                return _err("trajectory must be a JSON list of {stage, to} objects")

        trajectory, traj_err = _validate_trajectory(trajectory)
        if traj_err:
            return _err(f"invalid trajectory: {traj_err}")

        # Priority and metadata: inherit from backlog entry when available.
        promote_priority = entry.get("priority") or "normal"
        promote_note = (args.get("note") or "").strip() or entry.get("note") or None
        promote_sc = entry.get("success_criteria") or ""

        task_id = _new_task_id()
        trajectory_json = json.dumps(trajectory, separators=(",", ":"))
        initial_status = trajectory[0]["stage"]
        first_to: list[str] = trajectory[0].get("to") or []
        if isinstance(first_to, str):
            first_to = [first_to] if first_to else []
        initial_owner = first_to[0] if len(first_to) == 1 else None

        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO tasks (id, project_id, title, description, "
                "priority, workflow, tracking_reason, trajectory, status, "
                "owner, last_stage_change_at, created_by, success_criteria) "
                "VALUES (?, ?, ?, '', ?, 'generic', 'backlog', "
                "?, ?, ?, ?, ?, ?)",
                (task_id, project_id, title,
                 promote_priority,
                 trajectory_json, initial_status, initial_owner,
                 now_iso, caller_id, promote_sc),
            )
            # Plant first-stage role row when single named assignee.
            _role_for_stage_map = {
                "plan": "planner", "execute": "executor",
                "audit_syntax": "auditor_syntax",
                "audit_semantics": "auditor_semantics", "ship": "shipper",
            }
            if len(first_to) == 1:
                first_role = _role_for_stage_map[trajectory[0]["stage"]]
                eligible_json = json.dumps(first_to, separators=(",", ":"))
                await c.execute(
                    "INSERT INTO task_role_assignments "
                    "(task_id, role, eligible_owners, owner, "
                    "assigned_at, claimed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (task_id, first_role, eligible_json, first_to[0],
                     now_iso, now_iso),
                )
                if initial_status == "execute":
                    await _set_agent_current_task_if_free_or_stale(
                        c, first_to[0], task_id,
                    )
                await _set_agent_role_tools(c, first_to[0], first_role)
            await c.execute(
                "UPDATE backlog_tasks SET status='promoted', "
                "promoted_task_id=? WHERE id=?",
                (task_id, backlog_id),
            )
            await c.commit()
        finally:
            await c.close()

        # Wake the first-stage assignee when a single named slot was planted.
        if initial_owner and len(first_to) == 1:
            from server.agents import maybe_wake_agent
            wake_body = promote_note or (
                f"Coach promoted backlog #{backlog_id} ({title!r}) and "
                f"assigned you as {_role_for_stage_map[trajectory[0]['stage']]} "
                f"for the {initial_status} stage."
            )
            wake_body = _with_player_reminder(wake_body)
            try:
                await maybe_wake_agent(
                    initial_owner, wake_body,
                    bypass_debounce=True,
                    wake_source="kanban_promote",
                )
            except Exception:
                pass

        await bus.publish({
            "ts": now_iso,
            "agent_id": caller_id,
            "type": "task_created",
            "task_id": task_id,
            "title": title,
            "parent_id": None,
            "priority": promote_priority,
            "workflow": "generic",
            "tracking_reason": "backlog",
            "trajectory": trajectory,
            "project_id": project_id,
        })
        if initial_owner and len(first_to) == 1:
            first_role = _role_for_stage_map[trajectory[0]["stage"]]
            await bus.publish({
                "ts": now_iso,
                "agent_id": "system",
                "type": "task_stage_changed",
                "task_id": task_id,
                "from": None,
                "to": initial_status,
                "reason": "backlog_promoted",
                "owner": initial_owner,
                "assignee": initial_owner,
                "project_id": project_id,
            })
            await bus.publish({
                "ts": now_iso,
                "agent_id": caller_id,
                "type": "task_role_assigned",
                "task_id": task_id,
                "role": first_role,
                "owner": initial_owner,
                "stage": initial_status,
                "project_id": project_id,
            })
        await bus.publish({
            "ts": now_iso,
            "agent_id": caller_id,
            "type": "backlog_task_promoted",
            "backlog_id": backlog_id,
            "task_id": task_id,
            "title": title,
            "priority": promote_priority,
        })
        return _ok(
            f"Backlog entry #{backlog_id} promoted → task {task_id} "
            f"(\"{title}\", priority={promote_priority}, "
            f"initial stage: {initial_status})."
            + (f" Player {initial_owner} woken." if initial_owner else
               f" No auto-wake (pool/empty first-stage `to`); "
               f"drive via coord_approve_stage(task_id={task_id!r}, "
               f"next_stage={initial_status!r}, assignee=<slot>).")
        )

    @tool(
        "coord_list_backlog",
        (
            "List entries in the Backlog. Available to Coach and all Players. "
            "Read-only — no side effects.\n"
            "\n"
            "Params:\n"
            "- status: filter on 'pending' (default) / 'promoted' / "
            "'rejected' / 'all' for every status.\n"
            "- limit: max rows to return (default 50, max 200).\n"
            "\n"
            "Returns one line per entry:\n"
            "  #<id>  [<status>]  \"<title>\"  by <proposed_by>, <age> ago\n"
            "  (description indented on a second line when non-empty)\n"
            "\n"
            "Use this to get a mid-turn view of the backlog after "
            "coord_propose_task or coord_triage_backlog — the system-prompt "
            "snapshot at turn start doesn't refresh."
        ),
        {"status": str, "limit": str},
    )
    async def list_backlog(args: dict[str, Any]) -> dict[str, Any]:
        _VALID_STATUSES = ("pending", "promoted", "rejected", "all")
        status_arg = (args.get("status") or "pending").strip().lower()
        if status_arg not in _VALID_STATUSES:
            return _err(
                f"status must be one of: {', '.join(_VALID_STATUSES)}"
            )

        limit_raw = args.get("limit") or ""
        try:
            limit = max(1, min(200, int(limit_raw))) if limit_raw else 50
        except (ValueError, TypeError):
            return _err("limit must be an integer (1–200)")

        where = "" if status_arg == "all" else " WHERE status = ?"
        params: list[Any] = [] if status_arg == "all" else [status_arg]

        c = await configured_conn()
        try:
            cur = await c.execute(
                f"SELECT id, title, proposed_by, proposed_at, status, "
                f"reject_reason, promoted_task_id "
                f"FROM backlog_tasks{where} "
                f"ORDER BY proposed_at DESC LIMIT ?",
                [*params, limit],
            )
            rows = await cur.fetchall()
        finally:
            await c.close()

        if not rows:
            return _ok("(backlog is empty)")

        from datetime import datetime, timezone

        now_ts = datetime.now(timezone.utc)
        lines: list[str] = []
        for r in rows:
            d = dict(r)
            # Relative age
            try:
                proposed_ts = datetime.fromisoformat(
                    d["proposed_at"].replace("Z", "+00:00")
                )
                age_s = int((now_ts - proposed_ts).total_seconds())
                if age_s < 3600:
                    age = f"{age_s // 60}m"
                elif age_s < 86400:
                    age = f"{age_s // 3600}h"
                else:
                    age = f"{age_s // 86400}d"
            except Exception:
                age = "?"

            status_tag = d["status"]
            title = (d.get("title") or "").strip()
            proposer = d.get("proposed_by") or "?"
            line = (
                f"#{d['id']}  kind=backlog  [{status_tag}]  \"{title}\"  "
                f"by {proposer}, {age} ago"
            )
            if status_tag == "rejected" and d.get("reject_reason"):
                line += f"  reason: {d['reject_reason']}"
            elif status_tag == "promoted" and d.get("promoted_task_id"):
                line += f"  → task {d['promoted_task_id']}"
            lines.append(line)
        return _ok("\n".join(lines))

    @tool(
        "coord_set_task_blocked",
        (
            "Toggle the orthogonal blocked flag on a task. Owner + Coach "
            "only. Blocked tasks stay in their current stage but are "
            "rendered with a BLOCKED badge so the human / Coach knows "
            "to unstick them. Use for 'waiting on stakeholder', "
            "'external API down', etc.\n"
            "\n"
            "Params:\n"
            "- task_id: required\n"
            "- blocked: 'true' or 'false'\n"
            "- reason: short note shown on the card (max 500 chars)"
        ),
        {"task_id": str, "blocked": str, "reason": str},
    )
    async def set_task_blocked(args: dict[str, Any]) -> dict[str, Any]:
        task_id = (args.get("task_id") or "").strip()
        blocked_raw = str(args.get("blocked") or "").strip().lower()
        reason = (args.get("reason") or "").strip()
        if not task_id:
            return _err("task_id is required")
        if blocked_raw in ("true", "1", "yes", "on"):
            blocked = 1
        elif blocked_raw in ("false", "0", "no", "off"):
            blocked = 0
        else:
            return _err("blocked must be 'true' or 'false'")
        if len(reason) > 500:
            return _err(f"reason too long ({len(reason)} chars, max 500)")
        project_id = await resolve_active_project()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT owner, status FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            row = await cur.fetchone()
            if not row:
                return _err(f"task {task_id} not found")
            task_row = dict(row)
            owner = task_row.get("owner")
            if task_row.get("status") == "archive":
                return _err(
                    f"task {task_id} is archived; archived tasks are read-only."
                )
            if not caller_is_coach and owner != caller_id:
                return _err(
                    f"only the task's owner ({owner or 'nobody'}) or "
                    f"Coach can set the blocked flag."
                )
            await c.execute(
                "UPDATE tasks SET blocked = ?, "
                "blocked_reason = ? "
                "WHERE id = ? AND project_id = ?",
                (blocked, reason or None, task_id, project_id),
            )
            await c.commit()
        finally:
            await c.close()

        await bus.publish(
            {
                "ts": _now_iso(),
                "agent_id": caller_id,
                "type": "task_blocked_changed",
                "task_id": task_id,
                "blocked": bool(blocked),
                "reason": reason or None,
                "to": owner,
            }
        )
        reason_suffix = f" — {reason}" if reason else ""
        if blocked:
            return _ok(
                f"Task {task_id} blocked=true{reason_suffix}. The "
                f"stall sweeper now ignores this task; no auto-"
                f"nudges, no auto-reassign, no auto-archive. When "
                f"the blocker lifts, call coord_set_task_blocked"
                f"(task_id={task_id!r}, blocked=false) to re-enter "
                f"the ladder."
            )
        return _ok(
            f"Task {task_id} blocked=false{reason_suffix}. Stall "
            f"sweeper resumes monitoring; the escalation ladder "
            f"restarts at rung 1."
        )

    _tools = [
        list_tasks,
        create_task,
        update_task,
        write_task_spec,
        submit_audit_report,
        submit_verification_report,
        my_assignments,
        set_task_trajectory,
        set_task_workflow,
        # Kanban v2 (Docs/kanban-specs-v2.md §7.1, §7.2)
        approve_stage,
        archive_task,
        role_complete,
        request_plan_review,
        set_task_blocked,
        send_message,
        read_inbox,
        list_memory,
        read_memory,
        update_memory,
        commit_push,
        ship_to_dev,
        write_decision,
        propose_file_write,
        read_file,
        write_knowledge,
        read_knowledge,
        list_knowledge,
        list_team,
        set_player_role,
        set_player_runtime,
        set_player_model,
        set_player_effort,
        set_player_plan_mode,
        set_player_thinking,
        get_player_settings,
        answer_question,
        answer_plan,
        request_human,
        set_project_objectives,
        add_todo,
        complete_todo,
        update_todo,
        set_tick_interval,
        coord_run_truth_score,
        compass_ask,
        compass_audit,
        compass_brief,
        compass_status,
        coord_check_compass_audit,
        # Playbook (Docs/playbook-specs.md §7.1) — Coach-only mid-turn
        # proposal tool; readable surface lives in every system prompt
        # via build_system_prompt_suffix.
        propose_playbook_changes,
        # Backlog (Docs/kanban-specs-v2.md §4.0). propose_task: Coach +
        # Players. triage_backlog: Coach-only at runtime (body enforces).
        # list_backlog: read-only, Coach + Players.
        propose_task,
        triage_backlog,
        list_backlog,
    ]
    server = create_sdk_mcp_server(name="coord", version="0.8.0", tools=_tools)
    # Stash a name → handler map so the coord_mcp proxy endpoint
    # (server.coord_mcp + POST /api/_coord/{tool}) can dispatch by
    # name without re-importing SDK internals. This metadata is not
    # present by default because Claude serializes MCP config to JSON.
    if include_proxy_metadata:
        server["_handlers"] = {t.name: t.handler for t in _tools}
        server["_tool_names"] = [t.name for t in _tools]
        # Full SdkMcpTool list — kept for in-process introspection
        # (e.g., coord_schema_chars below approximates the wire-side
        # tool-definition payload size). Contains Python callables on
        # `.handler`, so this must NEVER leak to the SDK options path
        # (which JSON-serializes mcp_servers). The proxy metadata
        # branch is opt-in via include_proxy_metadata; the default
        # path returns a clean server.
        server["_tool_specs"] = list(_tools)
    return server


def coord_tool_names() -> list[str]:
    """Stable list of registered coord tool names — used by the proxy
    catalog (`server.coord_mcp`) and by the contract test that
    asserts the proxy enumeration matches the live registry.
    Builds a coord server for an arbitrary caller and pulls its names.
    """
    server = build_coord_server("coach", include_proxy_metadata=True)
    return list(server["_tool_names"])


_JSON_SCHEMA_KEYS = frozenset({
    "$schema",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "oneOf",
    "anyOf",
    "allOf",
    "enum",
})


def _json_type_for_python_type(value: Any) -> dict[str, Any]:
    origin = get_origin(value)
    args = get_args(value)
    if origin is list or value is list:
        item_schema = _json_type_for_python_type(args[0]) if args else {}
        return {"type": "array", "items": item_schema}
    if origin is dict or value is dict:
        return {"type": "object", "additionalProperties": True}
    if origin is None and args:
        # PEP 604 unions (e.g. str | None) report args but no useful
        # origin on some Python versions. Preserve the broad shape.
        types = [
            _json_type_for_python_type(arg).get("type")
            for arg in args
            if arg is not type(None)  # noqa: E721 - literal None type check
        ]
        types = [t for t in types if isinstance(t, str)]
        if len(types) == 1:
            return {"type": types[0]}
        if types:
            return {"type": sorted(set(types))}
        return {}
    if origin is not None and args:
        types = [
            _json_type_for_python_type(arg).get("type")
            for arg in args
            if arg is not type(None)  # noqa: E721
        ]
        types = [t for t in types if isinstance(t, str)]
        if len(types) == 1:
            return {"type": types[0]}
        if types:
            return {"type": sorted(set(types))}
        return {}
    if value is str:
        return {"type": "string"}
    if value is int:
        return {"type": "integer"}
    if value is float:
        return {"type": "number"}
    if value is bool:
        return {"type": "boolean"}
    if value is Any:
        return {}
    return {}


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, type):
        return value.__name__
    return str(value)


def _coord_tool_input_schema(raw_schema: Any) -> dict[str, Any]:
    """Convert Claude SDK tool schemas into MCP JSON schema.

    The SDK accepts either a real JSON schema dict or a compact
    ``{"arg": type}`` mapping. Codex's stdio MCP proxy needs a real
    JSON-schema-ish ``inputSchema`` so Coach can see parameters instead
    of an opaque ``additionalProperties`` object.
    """
    if not isinstance(raw_schema, dict):
        return {"type": "object", "additionalProperties": True}
    if any(key in raw_schema for key in _JSON_SCHEMA_KEYS):
        schema = _jsonable(raw_schema)
        if isinstance(schema, dict):
            schema.setdefault("type", "object")
            return schema
        return {"type": "object", "additionalProperties": True}
    return {
        "type": "object",
        "properties": {
            str(name): _json_type_for_python_type(spec)
            for name, spec in raw_schema.items()
        },
        "additionalProperties": True,
    }


def coord_tool_descriptors(caller_id: str = "coach") -> list[dict[str, Any]]:
    """Return JSON-serializable coord tool descriptors for the Codex proxy.

    Claude receives the in-process SDK MCP server directly, including
    full descriptions and schemas from ``@tool``. Codex reaches the same
    handlers through ``server.coord_mcp`` over loopback HTTP, so this
    descriptor surface keeps the stdio proxy from degrading to bare tool
    names.
    """
    server = build_coord_server(caller_id, include_proxy_metadata=True)
    specs = server.get("_tool_specs") or []
    descriptors: list[dict[str, Any]] = []
    for spec in specs:
        name = getattr(spec, "name", "") or ""
        if not name:
            continue
        descriptors.append({
            "name": name,
            "description": getattr(spec, "description", "") or "",
            "input_schema": _coord_tool_input_schema(
                getattr(spec, "input_schema", None)
            ),
        })
    return descriptors


_COORD_SCHEMA_CHARS_CACHE: dict[str, int] = {}


def coord_schema_chars(caller_id: str) -> int:
    """Approximate the size of the MCP tool-schema payload the SDK
    injects when this caller's coord server is wired up. Sums
    `name + description + JSON(args_schema)` across registered tools
    plus per-tool wrapper overhead. Real wire size differs slightly
    (MCP framing varies by SDK version), but this is a close lower
    bound and stable for trending.

    Cached per `caller_id` because Coach and Players see different
    tool subsets (hierarchy filtering inside `build_coord_server`).
    The registry is static per process, so a one-shot probe is fine.
    """
    cached = _COORD_SCHEMA_CHARS_CACHE.get(caller_id)
    if cached is not None:
        return cached
    try:
        server = build_coord_server(caller_id, include_proxy_metadata=True)
        specs = server.get("_tool_specs") or []
        total = 0
        for t in specs:
            name = getattr(t, "name", "") or ""
            desc = getattr(t, "description", "") or ""
            schema = getattr(t, "input_schema", None)
            try:
                # `input_schema` for SdkMcpTool can be either a JSON-
                # schema dict or a {arg_name: type-or-typing-construct}
                # mapping. The latter isn't JSON-serializable as-is
                # (types and PEP 604 unions like `str | None` aren't
                # JSON values), so coerce every non-primitive to its
                # string form first.
                if isinstance(schema, dict):
                    def _to_jsonable(v: Any) -> Any:
                        if isinstance(v, (str, int, float, bool)) or v is None:
                            return v
                        if isinstance(v, type):
                            return v.__name__
                        return str(v)
                    serializable = {k: _to_jsonable(v) for k, v in schema.items()}
                    schema_bytes = len(json.dumps(serializable))
                elif schema is not None:
                    schema_bytes = len(json.dumps(schema, default=str))
                else:
                    schema_bytes = 0
            except Exception:
                schema_bytes = 0
            # ~60 bytes for the per-tool JSON wrapper around the actual
            # schema ({"name":..., "description":..., "inputSchema":...})
            total += len(name) + len(desc) + schema_bytes + 60
        # Fallback when introspection retains no specs (older proxy
        # metadata path, or a future SDK refactor) — estimate at average
        # ~600 chars per tool to avoid logging 0.
        if total == 0:
            names = list(server.get("_tool_names") or [])
            if names:
                total = len(names) * 600
        _COORD_SCHEMA_CHARS_CACHE[caller_id] = total
        return total
    except Exception:
        return 0


# Reasoning-effort tier labels — keyed by the int stored on
# agent_project_roles.effort_override (and on the per-pane request).
# Mirrors agents._EFFORT_LEVELS but lives here so the coord-tool layer
# can render labels without importing from agents (cyclic).
_EFFORT_VALUE_LABELS = {1: "low", 2: "medium", 3: "high", 4: "max"}


ALLOWED_COORD_TOOLS = [
    "mcp__coord__coord_list_tasks",
    "mcp__coord__coord_create_task",
    "mcp__coord__coord_update_task",
    "mcp__coord__coord_send_message",
    "mcp__coord__coord_read_inbox",
    "mcp__coord__coord_list_memory",
    "mcp__coord__coord_read_memory",
    "mcp__coord__coord_update_memory",
    "mcp__coord__coord_commit_push",
    "mcp__coord__coord_ship_to_dev",
    "mcp__coord__coord_submit_verification_report",
    "mcp__coord__coord_write_decision",
    "mcp__coord__coord_propose_file_write",
    "mcp__coord__coord_read_file",
    "mcp__coord__coord_write_knowledge",
    "mcp__coord__coord_read_knowledge",
    "mcp__coord__coord_list_knowledge",
    "mcp__coord__coord_save_output",
    "mcp__coord__coord_list_team",
    "mcp__coord__coord_set_player_role",
    "mcp__coord__coord_set_player_runtime",
    "mcp__coord__coord_set_player_model",
    "mcp__coord__coord_set_player_effort",
    "mcp__coord__coord_set_player_plan_mode",
    "mcp__coord__coord_set_player_thinking",
    "mcp__coord__coord_get_player_settings",
    "mcp__coord__coord_answer_question",
    "mcp__coord__coord_answer_plan",
    "mcp__coord__coord_request_human",
    "mcp__coord__coord_set_project_objectives",
    "mcp__coord__coord_add_todo",
    "mcp__coord__coord_complete_todo",
    "mcp__coord__coord_update_todo",
    "mcp__coord__coord_set_tick_interval",
    # TruthScore — Coach + Players + human (no role gate). Read-only
    # against truth/; the cost cap bounds abuse. See
    # `Docs/truthscore-specs.md` §2.2.
    "mcp__coord__coord_run_truth_score",
    # Compass — Coach-only at runtime; included in the allowlist for
    # both roles so the SDK doesn't pre-reject the call. The
    # caller_is_coach gate inside each handler is what enforces the
    # Coach-only invariant.
    "mcp__coord__compass_ask",
    "mcp__coord__compass_audit",
    "mcp__coord__compass_brief",
    "mcp__coord__compass_status",
    "mcp__coord__coord_check_compass_audit",
    # Playbook (Docs/playbook-specs.md §7.1) — Coach-only at runtime;
    # listed here so the SDK accepts the call. The body's caller_is_coach
    # gate enforces the Coach-only invariant.
    "mcp__coord__coord_propose_playbook_changes",
    # Kanban v2 lifecycle (Docs/kanban-specs-v2.md §7.1, §7.2).
    # Coach-only enforcement is in each tool body; listing here lets
    # the SDK accept the call.
    "mcp__coord__coord_write_task_spec",
    "mcp__coord__coord_submit_audit_report",
    "mcp__coord__coord_submit_verification_report",
    "mcp__coord__coord_my_assignments",
    "mcp__coord__coord_set_task_trajectory",
    "mcp__coord__coord_set_task_workflow",
    "mcp__coord__coord_set_task_blocked",
    "mcp__coord__coord_approve_stage",
    "mcp__coord__coord_archive_task",
    "mcp__coord__coord_role_complete",
    "mcp__coord__coord_request_plan_review",
    # Backlog (Docs/kanban-specs-v2.md §4.0). coord_propose_task and
    # coord_list_backlog are open to Coach + Players; coord_triage_backlog
    # is Coach-only at runtime.
    "mcp__coord__coord_propose_task",
    "mcp__coord__coord_list_backlog",
    "mcp__coord__coord_triage_backlog",
    "mcp__coord__coord_list_backlog",
]

MEMORY_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")
MEMORY_CONTENT_MAX = 20_000

VALID_RECIPIENTS: set[str] = (
    {"coach", "broadcast"} | {f"p{i}" for i in range(1, 11)}
)

# Read-only tools: see the world, touch nothing. Coach uses these + coord.
STANDARD_READ_TOOLS = ["Read", "Grep", "Glob", "ToolSearch"]

# Mutating tools: Players get these too so they can actually do work.
STANDARD_WRITE_TOOLS = ["Write", "Edit", "Bash"]

# AskUserQuestion is routed by our can_use_tool callback in agents.py:
# Coach → form in the UI, Player → Coach's inbox. Must be in the allow
# list (the SDK won't run it otherwise) even though callback mediates
# the actual flow.
_INTERACTIVE_TOOLS = ["AskUserQuestion"]

# Coach = read + coord + interactive. Matches the spec rule "you never
# write code, you delegate" — enforced structurally (not just by prompt).
ALLOWED_COACH_TOOLS = STANDARD_READ_TOOLS + ALLOWED_COORD_TOOLS + _INTERACTIVE_TOOLS

# Players get the full standard set + coord + interactive.
ALLOWED_PLAYER_TOOLS = (
    STANDARD_READ_TOOLS + STANDARD_WRITE_TOOLS + ALLOWED_COORD_TOOLS + _INTERACTIVE_TOOLS
)

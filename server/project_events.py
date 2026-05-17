"""Per-project event log writer (Docs/kanban-specs-v2.md §9).

Sibling to `server/events.py` — the existing EventBus + `events` table
is preserved unchanged. This module writes a parallel row to
`project_events` for every v2-mappable bus event so Coach's tick can
read a unified context surface without scanning the much-larger raw
`events` log.

The split between events / project_events is deliberate:
- `events` keeps the full firehose (every text_delta, tool_use, etc.)
  for live UI streaming + per-pane history.
- `project_events` keeps only the v2-relevant audit trail in a shape
  Coach reads cheaply: one row per Player/system action that matters
  for routing decisions.

Renames (v1 → v2): `message_sent` → `coord_send_message`,
`knowledge_written` → `coord_write_knowledge`, `decision_written` →
`coord_write_decision`, `compass_audit_logged` → `compass_audit`. See
§9.2 of the spec.

Written-to from two paths:
1. `maybe_write_from_bus(ev)` — high-level entry point used by the
   kanban subscriber's per-event pass. Maps the bus event to the
   right project_events shape and inserts.
2. `write_project_event(...)` — low-level helper for direct writes
   (e.g. the migration's synthetic kanban_v2_cutover row, future
   Compass writes that don't go through the bus).

Failure isolation: a DB hiccup logs and returns; never propagates
exceptions back to the bus subscriber (which would kill the writer).
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from server.db import MISC_PROJECT_ID, configured_conn

logger = logging.getLogger("harness.project_events")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# ----------------------------------------------------------------------
# Type mapping (bus event type → project_events.type)
# ----------------------------------------------------------------------
#
# Direct pass-through unless explicitly renamed in `_BUS_TYPE_RENAMES`.
# Anything in `_LOGGABLE_BUS_TYPES` is eligible to write a row;
# anything else is dropped (the bus event still fires for live
# subscribers — we just don't mirror it into the v2 log).
#
# Keep this list in sync with `_V2_BACKFILL_TYPES` in `server/db.py`
# (used by the migration backfill). The two share the same enum.

_BUS_TYPE_RENAMES: dict[str, str] = {
    "message_sent": "coord_send_message",
    "knowledge_written": "coord_write_knowledge",
    "decision_written": "coord_write_decision",
    "compass_audit_logged": "compass_audit",
}

# v2-mappable bus event types (see kanban-specs-v2.md §9.2). Some are
# emitted by current v1 code (e.g. `commit_pushed`); others are emitted
# only after later phases land (`task_archived`, `task_role_completed`
# from coord_role_complete in Phase 2; `kanban_board_stalled` in
# Phase 6). Pre-listing them here is harmless — the writer simply
# never sees them until the later phases emit them.
_LOGGABLE_BUS_TYPES: frozenset[str] = frozenset({
    # Direct pass-through
    "commit_pushed",
    "task_spec_written",
    "task_role_completed",
    "audit_report_submitted",
    "verification_report_submitted",
    "audit_fail_notification",
    "task_stage_changed",
    "task_role_assigned",
    "task_role_stand_down",
    "task_trajectory_changed",
    "task_blocked_changed",
    "task_archived",
    "task_shipped_to_dev",
    "commit_without_task_id_warning",
    "task_stage_stale",
    "task_stall_persisting",
    "task_stall_auto_reassigned",
    "task_stall_no_alternative",
    "task_stall_auto_archived",
    "task_spec_unrecorded",
    "task_audit_unrecorded",
    "watchdog_finding",
    "pending_plan",
    "human_attention",
    "auto_compact_triggered",
    "session_compacted",
    "kanban_board_stalled",
    "audit_self_review_warning",
    "task_truthgate_started",
    "task_truthgate_completed",
    "task_truthgate_blocked",
    "task_truthgate_override_recorded",
    "truth_amendment_proposed",
    "truth_amendment_resolved",
    "task_provisional_closure_recorded",
    "task_truth_basis_stale",
    # Renamed (key = v1 bus type) — see `_BUS_TYPE_RENAMES`
    "message_sent",
    "knowledge_written",
    "decision_written",
    "compass_audit_logged",
})


def _resolve_log_type(bus_type: str) -> str:
    """Bus event type → project_events.type. Identity unless renamed."""
    return _BUS_TYPE_RENAMES.get(bus_type, bus_type)


def _extract_pointer(log_type: str, payload: dict[str, Any]) -> str | None:
    """Pull payload_pointer per event type (Docs/kanban-specs-v2.md §9.2).

    Returns None when the type doesn't carry a structured pointer —
    payload_json still has the full event for surface rendering, the
    pointer column is just a denormalized fast-path for the common
    "show me the artifact" case.
    """
    if log_type == "commit_pushed":
        return payload.get("sha") or None
    if log_type == "task_spec_written":
        return payload.get("spec_path") or None
    if log_type == "task_role_completed":
        return payload.get("artifact_path") or None
    if log_type == "audit_report_submitted":
        return payload.get("report_path") or None
    if log_type == "verification_report_submitted":
        return payload.get("report_path") or None
    if log_type == "coord_send_message":
        body = payload.get("body") or payload.get("text") or ""
        if not body:
            return None
        # Truncate per §9.2 — full body still in payload_json.
        return body[:500]
    if log_type == "coord_write_knowledge":
        return (
            payload.get("path")
            or payload.get("relative_path")
            or payload.get("knowledge_path")
            or None
        )
    if log_type == "coord_write_decision":
        return (
            payload.get("path")
            or payload.get("relative_path")
            or payload.get("decision_path")
            or None
        )
    if log_type == "task_archived":
        # The user-facing summary is the high-signal field for archive
        # rows — render as the pointer so the dashboard list shows it.
        summary = payload.get("summary") or payload.get("body") or ""
        if not summary:
            return None
        return summary[:500]
    if log_type == "task_shipped_to_dev":
        return payload.get("ship_sha") or payload.get("pr_url") or None
    if log_type == "kanban_board_stalled":
        # Body is the human-readable framing; let the dashboard show it.
        body = payload.get("body") or ""
        return body[:500] if body else None
    return None


# ----------------------------------------------------------------------
# Writer entry points
# ----------------------------------------------------------------------


async def write_project_event(
    *,
    project_id: str,
    actor: str,
    type: str,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
    payload_pointer: str | None = None,
    ts: str | None = None,
    read_by_coach_at: str | None = None,
) -> int | None:
    """Insert a single project_events row.

    Used by call sites that don't go through the bus (e.g. Compass
    audit writer, the migration's synthetic cutover event). Bus
    consumers should use `maybe_write_from_bus(ev)` instead — it does
    the type mapping + payload_pointer extraction automatically.

    Returns the inserted row id, or None on failure.
    """
    body = json.dumps(payload or {})
    actor = actor or "system"
    project_id = project_id or MISC_PROJECT_ID
    try:
        c = await configured_conn()
        try:
            if ts is not None:
                cur = await c.execute(
                    """
                    INSERT INTO project_events
                        (project_id, ts, actor, type, task_id,
                         payload_json, payload_pointer, read_by_coach_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id, ts, actor, type, task_id,
                        body, payload_pointer, read_by_coach_at,
                    ),
                )
            else:
                cur = await c.execute(
                    """
                    INSERT INTO project_events
                        (project_id, actor, type, task_id,
                         payload_json, payload_pointer, read_by_coach_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id, actor, type, task_id,
                        body, payload_pointer, read_by_coach_at,
                    ),
                )
            row_id = cur.lastrowid
            await c.commit()
            return row_id
        finally:
            await c.close()
    except Exception:
        logger.exception(
            "write_project_event failed (project=%s type=%s)",
            project_id, type,
        )
        return None


async def maybe_write_from_bus(ev: dict[str, Any]) -> int | None:
    """If `ev` is a v2-mappable bus event, write a project_events row.

    Returns the inserted row id, None if the event was dropped (not
    mappable) or the insert failed.
    """
    bus_type = ev.get("type")
    if not bus_type or bus_type not in _LOGGABLE_BUS_TYPES:
        return None
    log_type = _resolve_log_type(bus_type)
    project_id = ev.get("project_id") or MISC_PROJECT_ID
    actor = ev.get("agent_id") or "system"
    task_id = ev.get("task_id") or None
    pointer = _extract_pointer(log_type, ev)
    # Preserve the bus event's ts on the project_events row when
    # present — keeps the two log views in lockstep for debugging.
    ts = ev.get("ts") or None
    return await write_project_event(
        project_id=project_id,
        actor=actor,
        type=log_type,
        task_id=task_id,
        payload=ev,
        payload_pointer=pointer,
        ts=ts,
    )

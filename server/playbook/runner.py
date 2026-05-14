"""Playbook daily-reflection runner.

Owns the canonical `_run_lock: asyncio.Lock` (spec §N1). All paths
that write to lattice / archived / runs.jsonl acquire this lock:
  - daily reflection (this module)
  - bootstrap (server.playbook.bootstrap.run_bootstrap)
  - reset endpoint (server.playbook.api)
  - manual run / bootstrap endpoints
  - coord_propose_playbook_changes MCP tool (non-blocking acquire)

Daily reflection pipeline (spec §5):
  1. Activity gate — skip if last-24h activity below threshold.
  2. Cost gate — skip if over team daily cap (no retry).
  3. Evidence-bundle composition (§5.4) from kanban-v2 surfaces.
  4. LLM call (Claude primary, Codex fallback).
  5. Tolerant JSON parse.
  6. Op apply: merges → creates → adjusts (§5.6); cap enforcement.
  7. relevant_ids increment.
  8. Engine sweep: settle / stale_low / stale_unused.
  9. Persist + runs.jsonl row + bus events.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from server.playbook import config, prompts
from server.playbook.llm import call as llm_call, parse_json_safe
from server.playbook.mutate import (
    apply_coach_proposals,
    increment_relevant_ids,
    sweep_engine_actions,
)
from server.playbook.paths import ensure_playbook_dir
from server.playbook.store import (
    Lattice,
    append_run,
    load_archive,
    load_lattice,
    save_archive,
    save_lattice,
)
from server.shared.llm_types import LLMError

logger = logging.getLogger("harness.playbook.runner")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# -----------------------------------------------------------------
# THE canonical lock. All write paths acquire this. See §N1.
_run_lock: asyncio.Lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _new_run_id(prefix: str = "pbrun") -> str:
    return prefix + "-" + _now().strftime("%Y-%m-%d-%H-%M-%S")


# ---------------------------------------------------------------- gates


async def _today_spend_safe() -> float:
    try:
        from server.agents import _today_spend  # noqa: PLC0415

        return float(await _today_spend())
    except Exception:
        logger.exception("playbook.runner: _today_spend probe raised")
        return 0.0


async def _cost_cap_exceeded() -> bool:
    raw = (os.environ.get("HARNESS_TEAM_DAILY_CAP") or "").strip()
    if not raw:
        return False
    try:
        cap = float(raw)
    except ValueError:
        return False
    if cap <= 0:
        return False
    return await _today_spend_safe() >= cap


# ---------------------------------------------------------------- DB helpers


def _connect_sync() -> sqlite3.Connection | None:
    try:
        from server.db import DB_PATH  # noqa: PLC0415
    except ImportError:
        return None
    try:
        return sqlite3.connect(DB_PATH, timeout=2.0)
    except Exception:
        logger.exception("playbook.runner: sqlite3.connect raised")
        return None


def _activity_count(window_hours: int = 24) -> int:
    """Spec §5.2 activity gate: count of archived tasks + relevant
    events in the last `window_hours`. Returns 0 on DB error so we
    fail-open (skip the run rather than spam an LLM call)."""
    conn = _connect_sync()
    if conn is None:
        return 0
    try:
        cutoff = (_now() - timedelta(hours=window_hours)).isoformat()
        c = 0
        # Archived tasks in window
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE archived_at IS NOT NULL "
                "AND archived_at >= ?",
                (cutoff,),
            )
            c += int(cur.fetchone()[0] or 0)
        except sqlite3.OperationalError:
            pass

        # Project events of relevant types in window. Includes both
        # `compass_audit` (kanban-v2 spec) and `compass_audit_logged`
        # (current code emission name) — bridges the spec/code naming
        # mismatch noted in plan §risks (3).
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM project_events WHERE ts >= ? AND ("
                "type = 'audit_report_submitted' OR "
                "type LIKE 'task_stall_%' OR "
                "type = 'human_attention' OR "
                "type = 'compass_audit' OR "
                "type = 'compass_audit_logged'"
                ")",
                (cutoff,),
            )
            c += int(cur.fetchone()[0] or 0)
        except sqlite3.OperationalError:
            pass
        return c
    finally:
        conn.close()


# ---------------------------------------------------------------- evidence bundle (§5.4)


def _archived_tasks_in_window(
    conn: sqlite3.Connection, *, window_hours: int = 24, limit: int = 15
) -> list[dict[str, Any]]:
    """Recent archived tasks with metadata for outcome bucketing.

    Returns rows with (id, title, trajectory, owner, archived_at,
    cancelled_at, status). Caller composes the bundle entry shape.
    """
    cutoff = (_now() - timedelta(hours=window_hours)).isoformat()
    try:
        cur = conn.execute(
            "SELECT id, title, trajectory, owner, archived_at, cancelled_at, status "
            "FROM tasks WHERE archived_at IS NOT NULL AND archived_at >= ? "
            "ORDER BY archived_at DESC LIMIT ?",
            (cutoff, limit),
        )
        rows = []
        for r in cur.fetchall():
            rows.append({
                "id": r[0],
                "title": r[1] or "",
                "trajectory_raw": r[2] or "",
                "owner": r[3] or "",
                "archived_at": r[4] or "",
                "cancelled_at": r[5],
                "status": r[6] or "",
            })
        return rows
    except sqlite3.OperationalError:
        return []


def _trajectory_shape(raw: str) -> str:
    """Spec §5.4 N1: stages joined by `→`."""
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(obj, list):
        return ""
    parts: list[str] = []
    for entry in obj:
        if isinstance(entry, dict):
            stage = entry.get("stage")
            if stage:
                parts.append(str(stage))
    return "→".join(parts)


def _audit_chain_summary(conn: sqlite3.Connection, task_id: str) -> str:
    """Spec §5.4 N1: round-by-round verdict summary, e.g.
    `audit_syntax round 1 FAIL → round 2 PASS`. Empty when the task
    had no audit stages."""
    try:
        cur = conn.execute(
            "SELECT type, payload_json FROM project_events "
            "WHERE task_id = ? AND type = 'audit_report_submitted' "
            "ORDER BY ts ASC",
            (task_id,),
        )
        round_counters: dict[str, int] = {}
        parts: list[str] = []
        for row in cur.fetchall():
            payload = {}
            try:
                payload = json.loads(row[1] or "{}")
            except (TypeError, json.JSONDecodeError):
                pass
            kind = payload.get("kind") or "audit"
            verdict = payload.get("verdict") or "?"
            round_counters[kind] = round_counters.get(kind, 0) + 1
            parts.append(f"audit_{kind} round {round_counters[kind]} {verdict.upper()}")
        return " → ".join(parts)
    except sqlite3.OperationalError:
        return ""


def _task_cost_total(conn: sqlite3.Connection, task_id: str) -> float:
    """Spec §5.4 N1: sum `turns.cost_usd` for rows matching `task_id`.
    The turns table stores task association via the project_id +
    timestamp window in some harness versions; this defaults to the
    direct task_id column when present and 0.0 otherwise."""
    try:
        cur = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM turns WHERE task_id = ?",
            (task_id,),
        )
        return float(cur.fetchone()[0] or 0.0)
    except sqlite3.OperationalError:
        return 0.0


def _classify_outcome(
    conn: sqlite3.Connection,
    task: dict[str, Any],
    *,
    cost_usd_total: float,
    median_cost: float | None,
) -> str:
    """Spec §5.4 outcome buckets. Reads stall/audit/devent rows for the
    task to compute clean / friction / failed / cancelled."""
    if task.get("cancelled_at"):
        return "cancelled"

    task_id = task["id"]

    # Audit fails count
    audit_fails = 0
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM project_events "
            "WHERE task_id = ? AND type = 'audit_report_submitted' "
            "AND payload_json LIKE '%\"verdict\":\"fail\"%'",
            (task_id,),
        )
        audit_fails = int(cur.fetchone()[0] or 0)
    except sqlite3.OperationalError:
        pass

    # Stall events
    rung_2_plus_stalls = 0
    rung_1_stalls = 0
    try:
        cur = conn.execute(
            "SELECT type FROM project_events WHERE task_id = ? AND type LIKE 'task_stall%'",
            (task_id,),
        )
        for r in cur.fetchall():
            t = r[0] or ""
            if t in ("task_stall_persisting", "task_stall_auto_reassigned",
                     "task_stall_no_alternative", "task_stall_auto_archived"):
                rung_2_plus_stalls += 1
            elif t == "task_stage_stale":
                rung_1_stalls += 1
    except sqlite3.OperationalError:
        pass

    # Human attention events
    human_attention_fired = False
    try:
        cur = conn.execute(
            "SELECT 1 FROM project_events WHERE task_id = ? AND type = 'human_attention' LIMIT 1",
            (task_id,),
        )
        human_attention_fired = cur.fetchone() is not None
    except sqlite3.OperationalError:
        pass

    # Deviations log
    deviations_human = 0
    deviations_audit_or_push = 0
    try:
        cur = conn.execute(
            "SELECT noticed_at FROM deviations_log WHERE task_id = ?",
            (task_id,),
        )
        for r in cur.fetchall():
            if r[0] == "human":
                deviations_human += 1
            elif r[0] in ("audit", "push"):
                deviations_audit_or_push += 1
    except sqlite3.OperationalError:
        pass

    # Cost overrun threshold
    cost_overrun = (
        median_cost is not None
        and median_cost > 0
        and cost_usd_total > 1.5 * median_cost
    )

    # Bucket assignment per spec §5.4
    if (audit_fails >= 2 or rung_2_plus_stalls > 0
            or human_attention_fired or deviations_human > 0):
        return "failed"
    if (audit_fails == 1 or rung_1_stalls > 0 or deviations_audit_or_push > 0):
        return "friction"
    if cost_overrun:
        return "friction"
    return "clean"


def _cost_median_for_shape(
    conn: sqlite3.Connection,
    shape: str,
    *,
    window_days: int = 30,
    min_samples: int = 5,
) -> tuple[float | None, bool]:
    """Spec §5.4 + §S2: median cost for the same trajectory shape over
    `window_days`. Falls back to lattice-wide median when the shape
    has < `min_samples` samples. Returns (median_or_None, fallback_fired).
    """
    cutoff = (_now() - timedelta(days=window_days)).isoformat()

    # Per-shape costs
    per_shape: list[float] = []
    try:
        cur = conn.execute(
            "SELECT id, trajectory FROM tasks "
            "WHERE archived_at IS NOT NULL AND archived_at >= ?",
            (cutoff,),
        )
        for tid, traj in cur.fetchall():
            if _trajectory_shape(traj or "") == shape:
                per_shape.append(_task_cost_total(conn, tid))
    except sqlite3.OperationalError:
        pass

    if len(per_shape) >= min_samples:
        return median(per_shape), False

    # Fallback: lattice-wide median across all archived tasks in window
    all_costs: list[float] = []
    try:
        cur = conn.execute(
            "SELECT id FROM tasks WHERE archived_at IS NOT NULL AND archived_at >= ?",
            (cutoff,),
        )
        for (tid,) in cur.fetchall():
            all_costs.append(_task_cost_total(conn, tid))
    except sqlite3.OperationalError:
        pass

    if not all_costs:
        return None, False
    return median(all_costs), True


def _compose_evidence_bundle() -> tuple[str, dict[str, Any]]:
    """Compose the structured evidence digest per spec §5.4.

    Returns (rendered_string, evidence_summary_dict). The dict is
    persisted in the runs.jsonl row's `evidence_summary` field; the
    string is injected into the reflection prompt.

    Section size caps + section truncation order match spec §5.4. A
    soft target is `EVIDENCE_BUNDLE_TARGET_BYTES`; the hard cap is
    `EVIDENCE_BUNDLE_MAX_BYTES`. When over budget, drop the
    less-important sections (bottom-up) until under cap.
    """
    conn = _connect_sync()
    if conn is None:
        empty = {
            "tasks_archived": 0, "audit_fails": 0, "stall_events": 0,
            "compass_drift_verdicts": 0, "deviations_logged": 0,
            "human_attention_events": 0, "median_cost_fallback_fired": False,
        }
        return "(no evidence — db unavailable)", empty

    try:
        return _compose_evidence_bundle_inner(conn)
    finally:
        conn.close()


def _compose_evidence_bundle_inner(
    conn: sqlite3.Connection,
) -> tuple[str, dict[str, Any]]:
    cutoff = (_now() - timedelta(hours=24)).isoformat()

    # --- Archived tasks (up to 15) with outcome bucket
    archived = _archived_tasks_in_window(conn, window_hours=24, limit=15)
    enriched_tasks: list[dict[str, Any]] = []
    median_fallback_fired = False
    for t in archived:
        shape = _trajectory_shape(t["trajectory_raw"])
        cost = _task_cost_total(conn, t["id"])
        med, fallback = _cost_median_for_shape(
            conn, shape,
            window_days=config.EVIDENCE_MEDIAN_WINDOW_DAYS,
            min_samples=config.EVIDENCE_MEDIAN_MIN_SAMPLES,
        )
        if fallback:
            median_fallback_fired = True
        outcome = _classify_outcome(conn, t, cost_usd_total=cost, median_cost=med)
        enriched_tasks.append({
            "id": t["id"],
            "title": t["title"],
            "trajectory_shape": shape,
            "executor": t["owner"],
            "audit_chain_summary": _audit_chain_summary(conn, t["id"]),
            "outcome_bucket": outcome,
            "cost_usd_total": cost,
            "_median_cost": med,
        })

    # --- Cost outliers (cost > 2x median, up to 5)
    cost_outliers: list[dict[str, Any]] = []
    for t in enriched_tasks:
        med = t.get("_median_cost")
        if med and med > 0 and t["cost_usd_total"] > 2.0 * med:
            cost_outliers.append({
                "id": t["id"],
                "title": t["title"],
                "cost_usd_total": t["cost_usd_total"],
                "median_for_shape": med,
            })
        if len(cost_outliers) >= 5:
            break

    # --- Stall events rung-2+ (up to 5)
    stall_events: list[dict[str, Any]] = []
    try:
        cur = conn.execute(
            "SELECT ts, task_id, type, payload_json FROM project_events "
            "WHERE ts >= ? AND type IN "
            "('task_stall_persisting', 'task_stall_auto_reassigned', "
            "'task_stall_no_alternative', 'task_stall_auto_archived') "
            "ORDER BY ts DESC LIMIT 5",
            (cutoff,),
        )
        for r in cur.fetchall():
            stall_events.append({
                "ts": r[0], "task_id": r[1], "type": r[2],
                "payload": _json_or_empty(r[3]),
            })
    except sqlite3.OperationalError:
        pass

    # --- Compass verdicts (up to 5 of confident_drift / uncertain_drift,
    # plus aligned count)
    compass_aligned_count = 0
    compass_drifts: list[dict[str, Any]] = []
    try:
        cur = conn.execute(
            "SELECT ts, task_id, payload_json FROM project_events "
            "WHERE ts >= ? AND type IN ('compass_audit', 'compass_audit_logged') "
            "ORDER BY ts DESC",
            (cutoff,),
        )
        for r in cur.fetchall():
            payload = _json_or_empty(r[2])
            verdict = payload.get("verdict") or ""
            if verdict == "aligned":
                compass_aligned_count += 1
            elif len(compass_drifts) < 5:
                compass_drifts.append({
                    "ts": r[0], "task_id": r[1],
                    "verdict": verdict,
                    "summary": payload.get("summary") or "",
                })
    except sqlite3.OperationalError:
        pass

    # --- Deviations log (up to 10)
    deviations: list[dict[str, Any]] = []
    try:
        cur = conn.execute(
            "SELECT ts, task_id, executor, noticed_at, description "
            "FROM deviations_log WHERE ts >= ? ORDER BY ts DESC LIMIT 10",
            (cutoff,),
        )
        for r in cur.fetchall():
            deviations.append({
                "ts": r[0], "task_id": r[1], "executor": r[2],
                "noticed_at": r[3], "description": r[4] or "",
            })
    except sqlite3.OperationalError:
        pass

    # --- Repeat audit-fail Players (>=2 fails across distinct tasks)
    repeat_fail_players: list[dict[str, Any]] = []
    try:
        cur = conn.execute(
            "SELECT t.owner, COUNT(DISTINCT t.id) AS task_count "
            "FROM project_events e "
            "JOIN tasks t ON t.id = e.task_id "
            "WHERE e.ts >= ? AND e.type = 'audit_report_submitted' "
            "AND e.payload_json LIKE '%\"verdict\":\"fail\"%' "
            "GROUP BY t.owner HAVING task_count >= 2 "
            "ORDER BY task_count DESC LIMIT 5",
            (cutoff,),
        )
        for r in cur.fetchall():
            repeat_fail_players.append({"executor": r[0], "task_count": r[1]})
    except sqlite3.OperationalError:
        pass

    # --- Human attention events (up to 5)
    human_attention_events: list[dict[str, Any]] = []
    try:
        cur = conn.execute(
            "SELECT ts, task_id, payload_json FROM project_events "
            "WHERE ts >= ? AND type = 'human_attention' "
            "ORDER BY ts DESC LIMIT 5",
            (cutoff,),
        )
        for r in cur.fetchall():
            payload = _json_or_empty(r[2])
            human_attention_events.append({
                "ts": r[0], "task_id": r[1],
                "subject": payload.get("subject") or "",
            })
    except sqlite3.OperationalError:
        pass

    # --- Render
    sections: list[tuple[str, str]] = []
    sections.append(("archived_tasks", _render_section_archived(enriched_tasks)))
    sections.append(("cost_outliers", _render_section_cost_outliers(cost_outliers)))
    sections.append(("stall_events", _render_section_stalls(stall_events)))
    sections.append(("compass", _render_section_compass(compass_aligned_count, compass_drifts)))
    sections.append(("deviations", _render_section_deviations(deviations)))
    sections.append(("repeat_fails", _render_section_repeat_fails(repeat_fail_players)))
    sections.append(("human_attention", _render_section_human_attention(human_attention_events)))

    rendered = "\n\n".join(s for _, s in sections if s).strip()

    # Hard cap: drop sections in reverse order until under cap.
    if len(rendered.encode("utf-8")) > config.EVIDENCE_BUNDLE_MAX_BYTES:
        order = ["human_attention", "repeat_fails", "deviations", "compass",
                 "stall_events", "cost_outliers", "archived_tasks"]
        kept = list(sections)
        # Drop from bottom of the priority list (start with least
        # important — `human_attention` etc. last per spec §5.4).
        for drop_name in order[::-1]:
            kept = [(n, s) for n, s in kept if n != drop_name]
            rendered = "\n\n".join(s for _, s in kept if s).strip()
            if len(rendered.encode("utf-8")) <= config.EVIDENCE_BUNDLE_MAX_BYTES:
                break

    summary = {
        "tasks_archived": len(enriched_tasks),
        "audit_fails": sum(1 for t in enriched_tasks if t["outcome_bucket"] == "failed"),
        "stall_events": len(stall_events),
        "compass_drift_verdicts": len(compass_drifts),
        "deviations_logged": len(deviations),
        "human_attention_events": len(human_attention_events),
        "median_cost_fallback_fired": median_fallback_fired,
    }
    return rendered or "(no evidence in window)", summary


def _json_or_empty(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _render_section_archived(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return ""
    lines = ["## Archived tasks (last 24h)"]
    for t in tasks:
        # Strip the helper field _median_cost from the output.
        lines.append(
            f"- {t['id']} \"{(t['title'] or '')[:80]}\" "
            f"trajectory={t['trajectory_shape']} "
            f"executor={t['executor']} outcome={t['outcome_bucket']} "
            f"cost=${t['cost_usd_total']:.4f}"
            + (f" — audits: {t['audit_chain_summary']}" if t['audit_chain_summary'] else "")
        )
    return "\n".join(lines)


def _render_section_cost_outliers(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    lines = ["## Cost outliers (cost > 2× median for shape)"]
    for r in rows:
        lines.append(
            f"- {r['id']} \"{(r['title'] or '')[:80]}\" "
            f"cost=${r['cost_usd_total']:.4f} (median=${r['median_for_shape']:.4f})"
        )
    return "\n".join(lines)


def _render_section_stalls(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    lines = ["## Stall events (rung-2+)"]
    for r in rows:
        lines.append(f"- {r['ts']} task={r['task_id']} {r['type']}")
    return "\n".join(lines)


def _render_section_compass(aligned_count: int, drifts: list[dict[str, Any]]) -> str:
    if aligned_count == 0 and not drifts:
        return ""
    lines = ["## Compass verdicts"]
    if aligned_count:
        lines.append(f"- aligned count: {aligned_count}")
    for r in drifts:
        lines.append(
            f"- {r['ts']} task={r['task_id']} {r['verdict']}: "
            f"{(r['summary'] or '')[:120]}"
        )
    return "\n".join(lines)


def _render_section_deviations(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    lines = ["## Deviations log"]
    for r in rows:
        lines.append(
            f"- {r['ts']} task={r['task_id']} executor={r['executor']} "
            f"noticed_at={r['noticed_at']}: {(r['description'] or '')[:140]}"
        )
    return "\n".join(lines)


def _render_section_repeat_fails(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    lines = ["## Repeat audit-fail patterns"]
    for r in rows:
        lines.append(f"- executor={r['executor']} fail_task_count={r['task_count']}")
    return "\n".join(lines)


def _render_section_human_attention(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    lines = ["## Human attention events"]
    for r in rows:
        lines.append(f"- {r['ts']} task={r['task_id']}: {(r['subject'] or '')[:100]}")
    return "\n".join(lines)


# ---------------------------------------------------------------- lattice render (for prompt)


def _render_lattice_for_prompt(lattice: Lattice) -> str:
    """Compact rendering of the active lattice for the reflection
    prompt — id, weight, text. Different from the agent-system-prompt
    render (`render.py`) which has buckets + meta. Here Coach needs
    the raw id-weight-text tuple to address them in the JSON output.
    """
    if not lattice.statements:
        return "(empty lattice)"
    lines = []
    for s in sorted(lattice.statements, key=lambda x: -x.weight):
        marker = " [IMMUTABLE]" if s.immutable else ""
        lines.append(f"{s.id}{marker} [{s.weight:.2f}] {s.text}")
    return "\n".join(lines)


# ---------------------------------------------------------------- main entry


async def run_daily_reflection(*, manual: bool = False, force_through_no_activity: bool = False) -> dict[str, Any]:
    """Run one daily reflection. Returns runs.jsonl row dict.

    Caller is expected to have ALREADY acquired `_run_lock`. (The api
    + scheduler entry points handle the lock with appropriate timeout
    policies; runner doesn't acquire to keep the contract obvious.)
    """
    started = _now_iso()
    run_id = _new_run_id()
    ensure_playbook_dir()

    # --- Activity gate (§5.2)
    if not force_through_no_activity:
        count = _activity_count()
        if count < config.MIN_ACTIVITY_DEFAULT:
            row = _make_skip_row(
                run_id=run_id, started_at=started, manual=manual,
                outcome="skipped_no_activity",
            )
            await append_run(row)
            await _publish({
                "type": "playbook_run_skipped",
                "run_id": run_id,
                "reason": "no_activity",
                "count": count,
            })
            return row

    # --- Cost gate (§5.3)
    if await _cost_cap_exceeded():
        row = _make_skip_row(
            run_id=run_id, started_at=started, manual=manual,
            outcome="skipped_cost_cap",
        )
        await append_run(row)
        await _publish({
            "type": "playbook_run_skipped",
            "run_id": run_id,
            "reason": "cost_cap",
        })
        return row

    # --- Compose evidence + lattice prompt
    evidence_str, evidence_summary = _compose_evidence_bundle()

    lattice = load_lattice()
    archive = load_archive()
    rendered_lattice = _render_lattice_for_prompt(lattice)

    # Inject pressure directive when lattice exceeds the soft cap (§5.7.1).
    active_count = len(lattice.statements)
    if active_count > config.PRESSURE_CAP:
        pressure_note = prompts.REFLECTION_PRESSURE_DIRECTIVE.format(
            active=active_count,
            cap=config.PRESSURE_CAP,
        )
    else:
        pressure_note = ""

    user_prompt = prompts.REFLECTION_USER_TEMPLATE.format(
        rendered_lattice=rendered_lattice,
        evidence_bundle=evidence_str,
        pressure_note=pressure_note,
    )

    await _publish({
        "type": "playbook_run_started",
        "run_id": run_id,
        "kind": "manual" if manual else "daily",
    })

    # --- LLM call
    try:
        result = await llm_call(
            prompts.REFLECTION_SYSTEM,
            user_prompt,
            label="playbook:reflection",
        )
    except LLMError as exc:
        return await _persist_failure_row(
            run_id=run_id,
            started_at=started,
            manual=manual,
            evidence_summary=evidence_summary,
            error=str(exc),
            outcome="error_llm",
        )

    # --- Parse
    parsed = parse_json_safe(result.text)
    if not isinstance(parsed, dict):
        return await _persist_failure_row(
            run_id=run_id,
            started_at=started,
            manual=manual,
            evidence_summary=evidence_summary,
            error="reflection LLM did not return a JSON object",
            outcome="error_parse",
            llm_call=_llm_call_dict(result),
        )

    operations: list[dict[str, Any]] = []
    for op in (parsed.get("merges") or []):
        if isinstance(op, dict):
            operations.append({**op, "op": "merge"})
    for op in (parsed.get("creations") or []):
        if isinstance(op, dict):
            operations.append({**op, "op": "create"})
    for op in (parsed.get("adjustments") or []):
        if isinstance(op, dict):
            operations.append({**op, "op": "adjust"})

    # --- Apply ops + relevant_ids (§5.6)
    applied, rejected, hard_cap_hit = apply_coach_proposals(
        lattice, archive, operations,
        creation_weight=config.COACH_CREATION_WEIGHT,
    )
    relevance_increments = increment_relevant_ids(
        lattice, parsed.get("relevant_ids"),
    )

    # --- Engine sweep (§5.8)
    engine_actions = sweep_engine_actions(lattice, archive)

    if hard_cap_hit:
        await _publish({
            "type": "playbook_soft_cap_exceeded",
            "count": len(lattice.statements) + sum(1 for op in operations if op.get("op") == "create"),
            "dropped": sum(1 for r in rejected if r.get("reason") in ("hard_cap_pressure", "soft_cap_pressure")),
        })

    # --- Persist
    await save_lattice(lattice)
    await save_archive(archive)

    # Update last_run_at via sync sqlite (matches bootstrap pattern).
    _write_team_config_sync(config.PLAYBOOK_LAST_RUN_AT_KEY, _now_iso())

    if applied or engine_actions:
        await _publish({
            "type": "playbook_changes_applied",
            "operations_count": len(applied),
            # Spec §9 enum: coach_mid_turn | daily_reflection | human_dashboard.
            # Manual runs are triggered via POST /api/playbook/run (the
            # dashboard's "Run now" button), hence human_dashboard.
            "source": "human_dashboard" if manual else "daily_reflection",
        })

    # --- Settle / stale events
    for action in engine_actions:
        if action["action"] == "settle":
            await _publish({
                "type": "playbook_settled",
                "id": action["id"],
                "final_weight": action["final_weight"],
            })
        elif action["action"] in ("stale_low", "stale_unused"):
            await _publish({
                "type": "playbook_staled",
                "id": action["id"],
                "final_weight": action["final_weight"],
                "reason": action["action"],
            })

    if applied or relevance_increments > 0 or engine_actions:
        outcome = "applied"
    else:
        outcome = "no_changes"

    row = {
        "run_id": run_id,
        "started_at": started,
        "finished_at": _now_iso(),
        "kind": "manual" if manual else "daily",
        "evidence_window": {
            "from": (_now() - timedelta(hours=24)).isoformat(),
            "to": _now_iso(),
        },
        "evidence_summary": evidence_summary,
        "relevance_increments": relevance_increments,
        "proposals_applied": applied,
        "proposals_rejected": rejected,
        "engine_actions": engine_actions,
        "llm_call": _llm_call_dict(result),
        "outcome": outcome,
    }
    await append_run(row)

    await _publish({
        "type": "playbook_run_completed",
        "run_id": run_id,
        "outcome": outcome,
        "applied_count": len(applied),
        "evidence_summary": evidence_summary,
        "relevance_increments": relevance_increments,
        "llm_cost_usd": result.cost_usd,
    })

    return row


# ---------------------------------------------------------------- helpers


def _make_skip_row(*, run_id: str, started_at: str, manual: bool, outcome: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "kind": "manual" if manual else "daily",
        "evidence_window": None,
        "evidence_summary": None,
        "relevance_increments": 0,
        "proposals_applied": [],
        "proposals_rejected": [],
        "engine_actions": [],
        "llm_call": None,
        "outcome": outcome,
    }


async def _persist_failure_row(
    *,
    run_id: str,
    started_at: str,
    manual: bool,
    evidence_summary: dict[str, Any],
    error: str,
    outcome: str,
    llm_call: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "kind": "manual" if manual else "daily",
        "evidence_window": {
            "from": (_now() - timedelta(hours=24)).isoformat(),
            "to": _now_iso(),
        },
        "evidence_summary": evidence_summary,
        "relevance_increments": 0,
        "proposals_applied": [],
        "proposals_rejected": [],
        "engine_actions": [],
        "llm_call": llm_call,
        "error": error,
        "outcome": outcome,
    }
    await append_run(row)
    await _publish({
        "type": "playbook_run_completed",
        "run_id": run_id,
        "outcome": outcome,
        "applied_count": 0,
        "evidence_summary": evidence_summary,
        "relevance_increments": 0,
        "llm_cost_usd": None,
    })
    return row


def _llm_call_dict(result) -> dict[str, Any]:
    return {
        "model": None,
        "runtime": "claude",
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost_basis": "playbook:reflection",
        "cost_usd": result.cost_usd,
        "duration_ms": result.duration_ms,
        "is_error": result.is_error,
    }


def _write_team_config_sync(key: str, value: str | None) -> None:
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
        logger.exception("playbook.runner: team_config write failed (%s)", key)


async def _publish(payload: dict[str, Any]) -> None:
    try:
        from server.events import bus  # noqa: PLC0415

        await bus.publish({"ts": _now_iso(), **payload})
    except Exception:
        logger.exception("playbook.runner: event publish raised")


__all__ = ["_run_lock", "run_daily_reflection"]

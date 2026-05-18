"""Server-side verifier smoke checks.

Verifier smokes run inside the harness process and return only report-safe
evidence. They are intentionally not a general HTTP client.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from server.db import configured_conn, resolve_active_project


ALLOWED_TARGETS = frozenset({"local", "tot-dev", "production"})
ALLOWED_SMOKES = frozenset({
    "health_detail",
    "status_summary",
    "task_board_read",
    "agent_context",
    "turns_summary",
})
DISALLOWED_PARAM_KEYS = frozenset({
    "url",
    "headers",
    "authorization",
    "cookie",
    "token",
    "method",
    "body",
})

_REDACTION = "[REDACTED]"
_BEARER_RE = re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)[^\s,;]+")
_BARE_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_COOKIE_RE = re.compile(r"(?im)^(\s*(?:set-cookie|cookie)\s*:\s*).*$")
_TOKEN_ASSIGN_RE = re.compile(
    r"(?i)\b((?:HARNESS_COORD_PROXY_TOKEN|smoke[_-]?token|smoke[_-]?credential|"
    r"session[_-]?id|auth[_-]?header|api[_-]?key|token)\s*[:=]\s*)[^\s,;]+"
)


class VerifierSmokeError(ValueError):
    """Input rejected before any smoke executes."""


def checked_at_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _live_secret_values() -> list[str]:
    values: list[str] = []
    for name in ("HARNESS_TOKEN", "HARNESS_SECRETS_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            values.append(value)
    return values


def contains_live_secret(value: Any) -> bool:
    text = value if isinstance(value, str) else repr(value)
    return any(secret in text for secret in _live_secret_values())


def redact_sensitive_text(text: str) -> str:
    out = text
    for secret in _live_secret_values():
        out = out.replace(secret, _REDACTION)
    out = _BEARER_RE.sub(r"\1" + _REDACTION, out)
    out = _BARE_BEARER_RE.sub(r"\1" + _REDACTION, out)
    out = _COOKIE_RE.sub(r"\1" + _REDACTION, out)
    out = _TOKEN_ASSIGN_RE.sub(r"\1" + _REDACTION, out)
    return out


def sanitize_verifier_evidence(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            skey = redact_sensitive_text(str(key))
            if skey.lower() in {
                "authorization",
                "cookie",
                "set-cookie",
                "auth_header",
                "session_id",
                "codex_thread_id",
                "token",
                "secret",
            }:
                clean[skey] = _REDACTION
            else:
                clean[skey] = sanitize_verifier_evidence(item)
        return clean
    if isinstance(value, list):
        return [sanitize_verifier_evidence(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_verifier_evidence(item) for item in value]
    return value


def validate_smoke_params(smoke: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
    if smoke not in ALLOWED_SMOKES:
        raise VerifierSmokeError(f"smoke must be one of {sorted(ALLOWED_SMOKES)}")
    if params is None:
        params_in: Mapping[str, Any] = {}
    elif isinstance(params, Mapping):
        params_in = params
    else:
        raise VerifierSmokeError("params must be an object")

    bad = sorted(k for k in params_in if str(k).lower() in DISALLOWED_PARAM_KEYS)
    if bad:
        raise VerifierSmokeError(
            "params may not include arbitrary request fields: " + ", ".join(bad)
        )

    allowed_by_smoke = {
        "health_detail": set(),
        "status_summary": set(),
        "task_board_read": {"expected_task_id"},
        "agent_context": {"agent_id", "model"},
        "turns_summary": {"hours"},
    }
    allowed = allowed_by_smoke[smoke]
    unknown = sorted(k for k in params_in if k not in allowed)
    if unknown:
        raise VerifierSmokeError(
            f"unknown params for {smoke}: " + ", ".join(map(str, unknown))
        )

    clean = dict(params_in)
    if smoke == "agent_context":
        agent_id = str(clean.get("agent_id") or "").strip()
        if not (
            agent_id == "coach"
            or (
                agent_id.startswith("p")
                and agent_id[1:].isdigit()
                and 1 <= int(agent_id[1:]) <= 10
            )
        ):
            raise VerifierSmokeError("agent_context requires agent_id coach or p1..p10")
        clean["agent_id"] = agent_id
        if "model" in clean:
            clean["model"] = str(clean.get("model") or "").strip()[:120]
    elif smoke == "turns_summary":
        try:
            hours = int(clean.get("hours") or 24)
        except (TypeError, ValueError):
            raise VerifierSmokeError("turns_summary hours must be an integer")
        clean["hours"] = max(1, min(hours, 24 * 30))
    elif smoke == "task_board_read":
        expected = str(clean.get("expected_task_id") or "").strip()
        if expected:
            clean["expected_task_id"] = expected
    return clean


def _blocked_remote(task_id: str, target: str, smoke: str) -> dict[str, Any]:
    return {
        "status": "BLOCKED",
        "task_id": task_id,
        "target": target,
        "smoke": smoke,
        "checked_at": checked_at_iso(),
        "observed": {"auth_required": True},
        "limitations": [
            "verifier auth target credentials are not configured; protected smoke skipped fail-closed"
        ],
    }


async def run_verifier_smoke(
    *,
    caller_id: str,
    task_id: str,
    target: str,
    smoke: str,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    target_clean = (target or "").strip().lower()
    if target_clean not in ALLOWED_TARGETS:
        raise VerifierSmokeError(f"target must be one of {sorted(ALLOWED_TARGETS)}")
    params_clean = validate_smoke_params(smoke, params)
    if target_clean != "local":
        return _blocked_remote(task_id, target_clean, smoke)

    observed: dict[str, Any]
    status = "PASS"
    limitations: list[str] = []
    project_id = await resolve_active_project()
    checked_at = checked_at_iso()

    if smoke == "health_detail":
        checks: dict[str, dict[str, Any]] = {}
        try:
            c = await configured_conn()
            try:
                await c.execute("SELECT 1")
            finally:
                await c.close()
            checks["db"] = {"ok": True}
        except Exception as exc:
            checks["db"] = {"ok": False, "error_type": type(exc).__name__}
            status = "FAIL"
        observed = {
            "ok": status == "PASS",
            "http_status": 200 if status == "PASS" else 503,
            "auth_required": bool(os.environ.get("HARNESS_TOKEN", "").strip()),
            "checks": checks,
        }
    elif smoke == "status_summary":
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT status, COUNT(*) AS n FROM agents GROUP BY status"
            )
            agent_status_counts = {
                dict(r)["status"]: dict(r)["n"] for r in await cur.fetchall()
            }
            cur = await c.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? AND status != 'archive'",
                (project_id,),
            )
            active_tasks = int(dict(await cur.fetchone())["n"])
        finally:
            await c.close()
        observed = {
            "ok": True,
            "active_project": project_id,
            "agent_status_counts": agent_status_counts,
            "active_task_count": active_tasks,
        }
    elif smoke == "task_board_read":
        expected = params_clean.get("expected_task_id") or task_id
        if expected != task_id:
            raise VerifierSmokeError("expected_task_id must equal task_id when provided")
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT id, title, status FROM tasks WHERE id = ? AND project_id = ?",
                (task_id, project_id),
            )
            task = await cur.fetchone()
            cur = await c.execute(
                "SELECT report_path, verdict FROM task_role_assignments "
                "WHERE task_id = ? AND role = 'shipper' AND completed_at IS NOT NULL "
                "ORDER BY completed_at DESC LIMIT 1",
                (task_id,),
            )
            ship = await cur.fetchone()
            cur = await c.execute(
                "SELECT report_path, verdict FROM task_role_assignments "
                "WHERE task_id = ? AND role = 'verifier' AND completed_at IS NOT NULL "
                "ORDER BY completed_at DESC LIMIT 1",
                (task_id,),
            )
            verification = await cur.fetchone()
        finally:
            await c.close()
        if not task:
            status = "FAIL"
            observed = {"ok": False, "task_id": task_id, "present": False}
        else:
            t = dict(task)
            observed = {
                "ok": True,
                "task_id": t["id"],
                "title": t["title"],
                "stage": t["status"],
                "ship_evidence": dict(ship) if ship else None,
                "verification_evidence": dict(verification) if verification else None,
            }
    elif smoke == "agent_context":
        agent_id = str(params_clean["agent_id"])
        c = await configured_conn()
        try:
            cur = await c.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,))
            exists = await cur.fetchone()
            cur = await c.execute(
                "SELECT session_id, codex_thread_id FROM agent_sessions "
                "WHERE slot = ? AND project_id = ?",
                (agent_id, project_id),
            )
            session = await cur.fetchone()
        finally:
            await c.close()
        if not exists:
            status = "FAIL"
            observed = {"ok": False, "agent_id": agent_id, "present": False}
        else:
            rec = dict(session) if session else {}
            observed = {
                "ok": True,
                "agent_id": agent_id,
                "model": params_clean.get("model") or None,
                "has_session": bool(
                    rec.get("session_id") or rec.get("codex_thread_id")
                ),
            }
            limitations.append("session ids and thread ids omitted from verifier evidence")
    elif smoke == "turns_summary":
        hours = int(params_clean["hours"])
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(cost_usd), 0) AS cost_usd, "
                "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
                "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
                "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
                "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens "
                "FROM turns WHERE ended_at >= ? AND project_id = ?",
                (cutoff, project_id),
            )
            total = dict(await cur.fetchone())
            cur = await c.execute(
                "SELECT COALESCE(runtime, 'claude') AS runtime, COUNT(*) AS count, "
                "COALESCE(SUM(cost_usd), 0) AS cost_usd "
                "FROM turns WHERE ended_at >= ? AND project_id = ? "
                "GROUP BY COALESCE(runtime, 'claude') ORDER BY runtime",
                (cutoff, project_id),
            )
            by_runtime = [dict(r) for r in await cur.fetchall()]
        finally:
            await c.close()
        observed = {
            "ok": True,
            "hours": hours,
            "total": total,
            "by_runtime": by_runtime,
        }
    else:  # validate_smoke_params already gates this.
        raise VerifierSmokeError(f"unsupported smoke {smoke!r}")

    result = {
        "status": status,
        "task_id": task_id,
        "target": target_clean,
        "smoke": smoke,
        "checked_at": checked_at,
        "observed": observed,
        "limitations": limitations,
    }
    return sanitize_verifier_evidence(result)

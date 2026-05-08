"""Playbook bootstrap — one-shot prose extraction into seed lattice.

Triggered by the scheduler when `playbook_bootstrap_done` is unset
AND `playbook_bootstrap_blocked` is unset (spec §10 step 3).
Manually triggered via `POST /api/playbook/bootstrap` (G7).

Steps (spec §4):
  1. Cost gate (G3) — defer if over team daily cap, no retry-counter
     increment.
  2. Acquire `runner._run_lock` (blocking).
  3. Determine source: `"reset"` if `playbook_reset_at` is set,
     `"boot"` otherwise.
  4. Read prose corpus from
     [server/templates/app_dev_playbook.md](../templates/app_dev_playbook.md).
     Missing → empty-lattice path (no LLM call).
  5. Emit `playbook_bootstrap_started{source, retry_attempt}`.
  6. LLM call via `llm.call(BOOTSTRAP_SYSTEM, BOOTSTRAP_USER_TEMPLATE
     formatted with corpus)`. Tolerant parse.
  7. Validate each returned `{text, suggested_weight}` pair.
  8. Apply soft/hard cap (spec §5.7 / §G4) against the seed list.
  9. Persist: lattice with `created_by="bootstrap-playbook"`, set
     `playbook_bootstrap_done`, clear retries + reset_at.
 10. Emit `playbook_bootstrap_completed{statement_count, source}`.

Failure paths:
  - LLM raise / parse-fail / all-rejected → increment retries, emit
    `_failed{blocked: false}`. 3rd fail → set blocked flag, emit
    `_failed{blocked: true}` + `human_attention` (spec §G1 / §4.4).
  - Cost gate skip → log skip row, no retry increment, no event spam.
"""

from __future__ import annotations

import logging
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.playbook import config, prompts
from server.playbook.llm import call as llm_call, parse_json_safe
from server.playbook.mutate import insert_statement, resolve_cap_pressure
from server.playbook.paths import ensure_playbook_dir
from server.playbook.store import (
    Archive,
    Lattice,
    load_archive,
    load_lattice,
    save_archive,
    save_lattice,
    append_run,
)
from server.shared.llm_types import LLMError, LLMResult

logger = logging.getLogger("harness.playbook.bootstrap")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


_PROSE_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "app_dev_playbook.md"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return "pbboot-" + datetime.now(timezone.utc).strftime("%Y-%m-%d-%H-%M-%S")


# ---------------------------------------------------------------- team_config


def _read_team_config_sync(key: str) -> str:
    """Sync read of team_config. Bootstrap is async-running but uses
    sync sqlite3 to avoid the aiosqlite back-edge from a path that
    runs under `_run_lock`."""
    try:
        from server.db import DB_PATH  # noqa: PLC0415
    except ImportError:
        return ""
    try:
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


def _write_team_config_sync(key: str, value: str | None) -> None:
    """Sync write of team_config. value=None → delete the row."""
    try:
        from server.db import DB_PATH  # noqa: PLC0415
    except ImportError:
        return
    try:
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
        logger.exception("playbook.bootstrap: team_config write failed (%s)", key)


# ---------------------------------------------------------------- cost gate


async def _cost_cap_exceeded() -> bool:
    """True iff `_today_spend()` ≥ HARNESS_TEAM_DAILY_CAP. Read live
    so a deploy bumping the cap takes effect without restart.

    Mirrors the Compass cost-gate pattern (spec §5.3 + §G3).
    """
    import os

    raw = (os.environ.get("HARNESS_TEAM_DAILY_CAP") or "").strip()
    if not raw:
        return False
    try:
        cap = float(raw)
    except ValueError:
        return False
    if cap <= 0:
        return False
    try:
        from server.agents import _today_spend  # noqa: PLC0415

        spent = await _today_spend()
        return spent >= cap
    except Exception:
        logger.exception("playbook.bootstrap: cost-cap probe raised")
        return False


# ---------------------------------------------------------------- events


async def _publish(payload: dict[str, Any]) -> None:
    try:
        from server.events import bus  # noqa: PLC0415

        await bus.publish({"ts": _now_iso(), **payload})
    except Exception:
        logger.exception("playbook.bootstrap: event publish raised")


async def _emit_human_attention(subject: str, body: str) -> None:
    """Emit a `human_attention` bus event (matches the harness's
    standard escalation signal — telegram bridge + EnvPane attention
    strip pick it up)."""
    await _publish({
        "type": "human_attention",
        "agent_id": "playbook",
        "subject": subject,
        "body": body,
        "urgency": "high",
    })


# ---------------------------------------------------------------- main entry


async def run_bootstrap() -> dict[str, Any]:
    """Run one bootstrap attempt. Returns a runs.jsonl row dict.

    Caller (scheduler / api) is responsible for `_run_lock` acquisition
    BEFORE calling — this function does not acquire the lock itself
    (so manual API trigger and scheduler can use different timeout
    policies).
    """
    started = _now_iso()
    run_id = _new_run_id()
    ensure_playbook_dir()

    # --- Step 1: cost gate (G3)
    if await _cost_cap_exceeded():
        row = _make_skip_row(
            run_id=run_id,
            started_at=started,
            reason="cost_cap",
            source=_resolve_source(),
        )
        await append_run(row)
        await _publish({
            "type": "playbook_run_skipped",
            "run_id": run_id,
            "reason": "cost_cap",
        })
        return row

    source = _resolve_source()
    retries = _read_int_team_config(config.PLAYBOOK_BOOTSTRAP_RETRIES_KEY)
    retry_attempt = retries + 1

    await _publish({
        "type": "playbook_bootstrap_started",
        "source": source,
        "retry_attempt": retry_attempt,
    })

    # --- Step 4: read corpus
    corpus_text = _read_corpus()

    if corpus_text is None:
        # Empty-lattice path — no LLM call (spec §4.4).
        return await _persist_empty_lattice(run_id=run_id, started_at=started, source=source)

    # --- Step 6 & 7: LLM call + tolerant parse
    user_prompt = prompts.BOOTSTRAP_USER_TEMPLATE.format(corpus=corpus_text)
    try:
        result = await llm_call(
            prompts.BOOTSTRAP_SYSTEM,
            user_prompt,
            label="playbook:bootstrap",
        )
    except LLMError as exc:
        return await _persist_failure(
            run_id=run_id,
            started_at=started,
            source=source,
            error=str(exc),
            outcome="error_llm",
            llm_call=None,
        )

    parsed = parse_json_safe(result.text)
    if not isinstance(parsed, list):
        return await _persist_failure(
            run_id=run_id,
            started_at=started,
            source=source,
            error="bootstrap LLM did not return a JSON array",
            outcome="error_parse",
            llm_call=_llm_call_dict(result),
        )

    seeds = _validate_seeds(parsed)
    if not seeds:
        return await _persist_failure(
            run_id=run_id,
            started_at=started,
            source=source,
            error="all bootstrap seeds rejected by validator",
            outcome="error_parse",
            llm_call=_llm_call_dict(result),
        )

    # --- Step 8: soft/hard cap (G4)
    survivors_n, dropped_n, hard_cap_hit = resolve_cap_pressure(
        active_count=0,
        creation_count=len(seeds),
    )
    if hard_cap_hit:
        # Drop from end down to soft cap (100). Bootstrap doesn't fail.
        await _publish({
            "type": "playbook_soft_cap_exceeded",
            "count": len(seeds),
            "dropped": dropped_n,
        })
        seeds = seeds[: config.SOFT_STATEMENT_CAP]
    elif dropped_n > 0:
        seeds = seeds[:survivors_n]

    # --- Step 9: persist
    lattice = load_lattice()
    archive = load_archive()
    for seed in seeds:
        insert_statement(
            lattice,
            text=seed["text"],
            weight=seed["suggested_weight"],
            created_by="bootstrap-playbook",
            archive_for_id_minting=archive,
        )

    await save_lattice(lattice)
    await save_archive(archive)  # idempotent — preserves any restored items

    _write_team_config_sync(config.PLAYBOOK_BOOTSTRAP_DONE_KEY, "1")
    _write_team_config_sync(config.PLAYBOOK_BOOTSTRAP_RETRIES_KEY, None)
    _write_team_config_sync(config.PLAYBOOK_RESET_AT_KEY, None)

    # --- Step 10: completed event
    await _publish({
        "type": "playbook_bootstrap_completed",
        "statement_count": len(lattice.statements),
        "source": source,
    })

    row = {
        "run_id": run_id,
        "started_at": started,
        "finished_at": _now_iso(),
        "kind": "bootstrap",
        "evidence_window": None,
        "evidence_summary": None,
        "relevance_increments": 0,
        "proposals_applied": [],
        "proposals_rejected": [],
        "engine_actions": [],
        "seeds_inserted": len(seeds),
        "source": source,
        "llm_call": _llm_call_dict(result),
        "outcome": "applied" if seeds else "no_changes",
    }
    await append_run(row)
    return row


# ---------------------------------------------------------------- helpers


def _resolve_source() -> str:
    """Source = "reset" if playbook_reset_at set; else "boot"."""
    return "reset" if _read_team_config_sync(config.PLAYBOOK_RESET_AT_KEY) else "boot"


def _read_int_team_config(key: str) -> int:
    raw = _read_team_config_sync(key)
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0


def _read_corpus() -> str | None:
    """Read app_dev_playbook.md. Missing → None (caller takes the
    empty-lattice path)."""
    if not _PROSE_TEMPLATE_PATH.exists():
        logger.warning(
            "playbook.bootstrap: prose template missing at %s — "
            "empty-lattice path",
            _PROSE_TEMPLATE_PATH,
        )
        return None
    try:
        return _PROSE_TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.exception("playbook.bootstrap: corpus read failed: %s", exc)
        return None


def _validate_seeds(raw: list[Any]) -> list[dict[str, Any]]:
    """Filter the LLM output to well-formed seed dicts. Each seed must
    have a non-empty `text` (≤ 500 chars) and a `suggested_weight` in
    [0, 1]. Aliasing: `weight` accepted as a fallback key."""
    out: list[dict[str, Any]] = []
    seen_texts: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text or len(text) > 500:
            continue
        if text.lower() in seen_texts:
            continue
        weight_raw = item.get("suggested_weight")
        if weight_raw is None:
            weight_raw = item.get("weight")
        try:
            weight = float(weight_raw) if weight_raw is not None else config.BOOTSTRAP_WEIGHT
        except (TypeError, ValueError):
            continue
        if not (0.0 <= weight <= 1.0):
            continue
        seen_texts.add(text.lower())
        out.append({"text": text, "suggested_weight": weight})
    return out


async def _persist_empty_lattice(
    *,
    run_id: str,
    started_at: str,
    source: str,
) -> dict[str, Any]:
    """Empty-template path: write empty lattice + flag bootstrap done.
    Spec §4.4 — agents see no playbook section in their prompts."""
    ensure_playbook_dir()
    lattice = Lattice(
        schema_version=config.PLAYBOOK_SCHEMA_VERSION,
        updated_at=_now_iso(),
        statements=[],
    )
    archive = load_archive()
    await save_lattice(lattice)
    await save_archive(archive)
    _write_team_config_sync(config.PLAYBOOK_BOOTSTRAP_DONE_KEY, "1")
    _write_team_config_sync(config.PLAYBOOK_BOOTSTRAP_RETRIES_KEY, None)
    _write_team_config_sync(config.PLAYBOOK_RESET_AT_KEY, None)
    await _publish({
        "type": "playbook_bootstrap_completed",
        "statement_count": 0,
        "source": source,
    })
    row = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "kind": "bootstrap",
        "evidence_window": None,
        "evidence_summary": None,
        "relevance_increments": 0,
        "proposals_applied": [],
        "proposals_rejected": [],
        "engine_actions": [],
        "seeds_inserted": 0,
        "source": source,
        "llm_call": None,
        "outcome": "no_changes",
    }
    await append_run(row)
    return row


async def _persist_failure(
    *,
    run_id: str,
    started_at: str,
    source: str,
    error: str,
    outcome: str,
    llm_call: dict[str, Any] | None,
) -> dict[str, Any]:
    """Failure path: increment retry counter, emit `_failed`, set
    blocked flag on 3rd attempt."""
    retries = _read_int_team_config(config.PLAYBOOK_BOOTSTRAP_RETRIES_KEY) + 1
    _write_team_config_sync(
        config.PLAYBOOK_BOOTSTRAP_RETRIES_KEY, str(retries)
    )
    blocked = retries >= config.BOOTSTRAP_MAX_RETRIES
    if blocked:
        _write_team_config_sync(config.PLAYBOOK_BOOTSTRAP_BLOCKED_KEY, "1")
    await _publish({
        "type": "playbook_bootstrap_failed",
        "error": error,
        "retries": retries,
        "blocked": blocked,
    })
    if blocked:
        await _emit_human_attention(
            subject="Playbook bootstrap failed 3 times — operator intervention required",
            body=(
                f"Three consecutive bootstrap attempts failed: {error}\n\n"
                "The lattice is empty until you call POST /api/playbook/reset "
                "to clear the block flag and re-arm. Inspect the playbook "
                "scheduler logs for the underlying error class."
            ),
        )
    row = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "kind": "bootstrap",
        "evidence_window": None,
        "evidence_summary": None,
        "relevance_increments": 0,
        "proposals_applied": [],
        "proposals_rejected": [],
        "engine_actions": [],
        "seeds_inserted": 0,
        "source": source,
        "llm_call": llm_call,
        "outcome": outcome,
    }
    await append_run(row)
    return row


def _make_skip_row(
    *,
    run_id: str,
    started_at: str,
    reason: str,
    source: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": _now_iso(),
        "kind": "bootstrap",
        "evidence_window": None,
        "evidence_summary": None,
        "relevance_increments": 0,
        "proposals_applied": [],
        "proposals_rejected": [],
        "engine_actions": [],
        "seeds_inserted": 0,
        "source": source,
        "llm_call": None,
        "outcome": (
            "skipped_cost_cap" if reason == "cost_cap" else f"skipped_{reason}"
        ),
    }


def _llm_call_dict(result: LLMResult) -> dict[str, Any]:
    """Subset of LLMResult that goes into the runs.jsonl `llm_call`
    field. Drop the full `text` (it's the LLM output we already
    persisted as seeds)."""
    return {
        "model": None,  # resolved at call site; not exposed by LLMResult
        "runtime": "claude",
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost_basis": "playbook:bootstrap",
        "cost_usd": result.cost_usd,
        "duration_ms": result.duration_ms,
        "is_error": result.is_error,
    }


__all__ = ["run_bootstrap"]

"""TruthGate classifier parse, validation, sparse mode, and run lock."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import server.truthgate.config as config
from server.truthgate.classifier_types import TaskFields
from server.truthgate.corpus import (
    TruthCorpus,
    gather_truth_corpus,
    validate_truth_basis_path,
)
from server.truthgate.prompts import CLASSIFIER_SYSTEM_PROMPT, build_classifier_prompt
from server.truthgate.sparse import sparse_pass_result


ALLOWED_CLASSIFIER_VERDICTS = frozenset({
    "truthgate_pass",
    "truthgate_needs_truth_change",
    "truthgate_rejected_or_needs_human_clarification",
})

CONCERN_MAX_CHARS = 500
RATIONALE_MAX_CHARS = 1200

_locks: dict[str, asyncio.Lock] = {}


class TruthGateClassificationError(RuntimeError):
    """Operator-readable classifier failure."""

    def __init__(self, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True)
class TruthGateTaskInput(TaskFields):
    task_id: str | None = None


def is_running(project_id: str) -> bool:
    lock = _locks.get(project_id)
    return bool(lock and lock.locked())


async def run_truthgate_classifier(
    project_id: str,
    task: TruthGateTaskInput,
) -> dict[str, Any]:
    """Classify a task with per-project locking and cost-cap preflight."""
    if not project_id:
        raise TruthGateClassificationError("no active project")
    lock = _locks.setdefault(project_id, asyncio.Lock())
    if lock.locked():
        raise TruthGateClassificationError(
            "TruthGate classifier is already running for this project",
            http_status=409,
        )
    async with lock:
        return await classify_task(project_id, task)


async def classify_task(
    project_id: str,
    task: TruthGateTaskInput,
) -> dict[str, Any]:
    cfg = config.load_config()
    corpus = gather_truth_corpus(
        project_id,
        total_budget_chars=cfg.truth_budget_chars,
        per_file_chars=cfg.truth_per_file_chars,
    )
    if len(corpus.files) < cfg.min_corpus_files:
        return sparse_pass_result(task_id=task.task_id, corpus=corpus, cfg=cfg)

    await _check_cost_cap()
    from server.truthgate import llm  # noqa: PLC0415

    result = await llm.call_classifier(
        CLASSIFIER_SYSTEM_PROMPT,
        build_classifier_prompt(task, corpus),
        cfg=cfg,
        project_id=project_id,
    )
    if result.is_error:
        raise TruthGateClassificationError(
            "TruthGate classifier model returned an error"
        )
    parsed = parse_classifier_output(result.text, project_id=project_id, corpus=corpus)
    parsed.update({
        "method": "classifier",
        "model": cfg.model,
        "model_alias": cfg.model_alias,
        "fallback_model": cfg.fallback_model,
        "classified_at": _now_iso(),
        "task_id": task.task_id,
        "corpus_files": list(corpus.files),
        "corpus_chars": corpus.chars,
    })
    return parsed


def parse_classifier_output(
    text: str,
    *,
    project_id: str,
    corpus: TruthCorpus,
) -> dict[str, Any]:
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise TruthGateClassificationError(
            "TruthGate classifier returned invalid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise TruthGateClassificationError(
            "TruthGate classifier returned invalid JSON"
        )
    verdict = _required_str(parsed, "verdict")
    if verdict not in ALLOWED_CLASSIFIER_VERDICTS:
        raise TruthGateClassificationError(
            f"TruthGate classifier returned unsupported verdict: {verdict}"
        )
    basis = _string_list(parsed.get("truth_basis"), "truth_basis", max_items=20)
    allowed_basis_roots = set(corpus.files)
    normalized_basis: list[str] = []
    for item in basis:
        try:
            normalized = validate_truth_basis_path(project_id, item)
        except ValueError as exc:
            raise TruthGateClassificationError(str(exc)) from exc
        path_only = normalized.partition("#")[0]
        if path_only not in allowed_basis_roots:
            raise TruthGateClassificationError(
                f"truth_basis was not in classifier corpus slice: {normalized}"
            )
        normalized_basis.append(normalized)

    concerns = [
        concern[:CONCERN_MAX_CHARS]
        for concern in _string_list(
            parsed.get("truth_concerns"),
            "truth_concerns",
            max_items=20,
        )
    ]
    rationale = _optional_str(parsed.get("rationale"))[:RATIONALE_MAX_CHARS]
    confidence = _confidence(parsed.get("confidence"))
    suggested = parsed.get("suggested_amendment")
    if suggested is not None and not isinstance(suggested, (str, dict)):
        raise TruthGateClassificationError(
            "suggested_amendment must be null, string, or object"
        )
    return {
        "verdict": verdict,
        "truth_basis": normalized_basis,
        "truth_concerns": concerns,
        "rationale": rationale,
        "suggested_amendment": suggested,
        "confidence": confidence,
        "warning": None,
    }


async def _check_cost_cap() -> None:
    try:
        from server.agents import TEAM_DAILY_CAP_USD, _today_spend  # noqa: PLC0415
    except Exception:
        return
    if TEAM_DAILY_CAP_USD <= 0:
        return
    try:
        spent = await _today_spend()
    except Exception:
        return
    if spent >= TEAM_DAILY_CAP_USD:
        raise TruthGateClassificationError(
            f"team daily cost cap reached (${spent:.2f} / "
            f"${TEAM_DAILY_CAP_USD:.2f})",
            http_status=429,
        )


def _required_str(obj: dict[str, Any], key: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TruthGateClassificationError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_str(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TruthGateClassificationError("rationale must be a string")
    return value.strip()


def _string_list(value: Any, key: str, *, max_items: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TruthGateClassificationError(f"{key} must be an array")
    if len(value) > max_items:
        raise TruthGateClassificationError(f"{key} has too many entries")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise TruthGateClassificationError(
                f"{key} entries must be non-empty strings"
            )
        out.append(item.strip())
    return out


def _confidence(value: Any) -> float:
    if isinstance(value, bool):
        raise TruthGateClassificationError("confidence must be a number")
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise TruthGateClassificationError("confidence must be a number") from exc
    if num < 0.0 or num > 1.0:
        raise TruthGateClassificationError("confidence must be between 0 and 1")
    return num


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-serializable copy safe for task storage/events."""
    return dict(result)


__all__ = [
    "ALLOWED_CLASSIFIER_VERDICTS",
    "TruthGateClassificationError",
    "TruthGateTaskInput",
    "classify_task",
    "is_running",
    "parse_classifier_output",
    "run_truthgate_classifier",
    "to_payload",
]

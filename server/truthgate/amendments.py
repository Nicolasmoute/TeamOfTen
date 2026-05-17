"""Helpers for TruthGate truth-amendment metadata."""

from __future__ import annotations

from typing import Any


def build_amendment_metadata(
    *,
    originating_task_id: str | None,
    rationale: str,
    evidence: str | None = None,
    affected_docs: list[str] | tuple[str, ...] | None = None,
    provisional_impl: bool = False,
    rejection_consequence: str | None = None,
    draft_model: str | None = None,
    drafted: bool = False,
) -> dict[str, Any]:
    """Return metadata for the existing protected file proposal flow."""
    if not rationale.strip():
        raise ValueError("truth amendment rationale is required")
    return {
        "originating_task_id": originating_task_id,
        "rationale": rationale.strip(),
        "evidence": (evidence or "").strip(),
        "affected_docs": list(affected_docs or []),
        "provisional_impl": bool(provisional_impl),
        "rejection_consequence": (rejection_consequence or "").strip(),
        "draft_model": draft_model,
        "drafted": bool(drafted),
    }


__all__ = ["build_amendment_metadata"]

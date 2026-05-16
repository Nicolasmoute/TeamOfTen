"""Sparse-corpus permissive mode for TruthGate."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from server.truthgate.config import TruthGateConfig
from server.truthgate.corpus import TruthCorpus


def sparse_pass_result(
    *,
    task_id: str | None,
    corpus: TruthCorpus,
    cfg: TruthGateConfig,
) -> dict[str, Any]:
    warning = (
        "TruthGate sparse mode: truth corpus has "
        f"{len(corpus.files)} file(s), below minimum {cfg.min_corpus_files}; "
        "permissive pass recorded without LLM call."
    )
    return {
        "verdict": "truthgate_pass",
        "truth_basis": [],
        "truth_concerns": [],
        "rationale": warning,
        "suggested_amendment": None,
        "confidence": 0.0,
        "method": "classifier_sparse",
        "model": None,
        "model_alias": None,
        "fallback_model": None,
        "classified_at": datetime.now(timezone.utc).isoformat(),
        "task_id": task_id,
        "corpus_files": list(corpus.files),
        "corpus_chars": corpus.chars,
        "warning": warning,
    }


__all__ = ["sparse_pass_result"]

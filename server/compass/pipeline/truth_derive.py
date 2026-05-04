"""Back-compat shim — `truth_derive` was renamed to `intent_derive`.

Compass refocused toward intent (2026-05-04). The module formerly
known as `truth_derive` is now `intent_derive`; this file re-exports
its public surface so any external imports keep working until the
rename ripples through.

New code should import from `server.compass.pipeline.intent_derive`.
"""

from server.compass.pipeline.intent_derive import (  # noqa: F401
    INTENT_DERIVED_CREATED_BY,
    INTENT_DERIVED_WEIGHT,
    IntentDeriveResult,
    TRUTH_DERIVED_WEIGHT,
    TruthDeriveResult,
    corpus_hash,
    derive_from_corpus,
    derive_from_truth,
    truth_corpus_hash,
)

__all__ = [
    "INTENT_DERIVED_CREATED_BY",
    "INTENT_DERIVED_WEIGHT",
    "IntentDeriveResult",
    "TRUTH_DERIVED_WEIGHT",
    "TruthDeriveResult",
    "corpus_hash",
    "derive_from_corpus",
    "derive_from_truth",
    "truth_corpus_hash",
]

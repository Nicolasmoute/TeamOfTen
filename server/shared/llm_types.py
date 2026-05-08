"""Generic LLM-call result + error types shared across subsystems.

Originally lived in `server/compass/llm.py` as `CompassLLMError` /
`CompassLLMResult`. Lifted here so multiple subsystems (Compass, Playbook)
can share the Codex fallback module (`server/shared/codex_llm.py`) without
the back-edge that the old layout would create.

Compass keeps the `CompassLLM*` names alive as aliases for backwards
compat — see [server/compass/llm.py](../compass/llm.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field


class LLMError(RuntimeError):
    """Raised when an LLM call fails before producing usable output.
    Caller decides whether to retry, fall back to a stub, or skip
    the pipeline stage. Best-effort callers (Compass digest stages,
    Playbook reflection) log the failure and continue with the next
    stage."""


@dataclass
class LLMResult:
    """Result shape from a single LLM round-trip. Mirrors what the
    turn ledger records — `cost_usd` may be None when pricing data
    is unavailable; tokens default to 0 when extraction fails.

    `is_error=True` represents a soft failure (no assistant text but
    no exception) — callers typically treat this the same as a parse
    failure on the would-be JSON output and skip the stage.
    """

    text: str
    is_error: bool = False
    cost_usd: float | None = None
    duration_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    session_id: str | None = None
    stop_reason: str | None = None
    errors: list[str] = field(default_factory=list)


__all__ = ["LLMError", "LLMResult"]

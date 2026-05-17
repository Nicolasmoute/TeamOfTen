"""TruthGate configuration and model validation."""

from __future__ import annotations

import os
from dataclasses import dataclass

from server.models_catalog import resolve_model_alias


DEFAULT_MODEL_ALIAS = "latest_sonnet"
DEFAULT_FALLBACK_MODEL_ALIAS = "latest_mini"
DEFAULT_MAX_TOKENS = 1200
DEFAULT_TRUTH_BUDGET_CHARS = 32_000
DEFAULT_TRUTH_PER_FILE_CHARS = 16_000
DEFAULT_MIN_CORPUS_FILES = 3
DEFAULT_EFFORT = "medium"

_FORBIDDEN_CLASSIFIER_ALIASES = frozenset({"latest_opus", "latest_gpt"})
_FORBIDDEN_CLASSIFIER_CONCRETE = frozenset(
    resolve_model_alias(alias) for alias in _FORBIDDEN_CLASSIFIER_ALIASES
)
_VALID_EFFORTS = frozenset({"low", "medium", "high", "max"})


class TruthGateConfigError(ValueError):
    """Raised when a TruthGate env knob is invalid."""


@dataclass(frozen=True)
class TruthGateConfig:
    model_alias: str
    model: str
    fallback_model_alias: str
    fallback_model: str
    max_tokens: int
    truth_budget_chars: int
    truth_per_file_chars: int
    min_corpus_files: int
    effort: str


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise TruthGateConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise TruthGateConfigError(f"{name} must be >= {minimum}")
    return value


def _env_model(name: str, default: str) -> tuple[str, str]:
    raw = os.environ.get(name, "").strip() or default
    if raw in _FORBIDDEN_CLASSIFIER_ALIASES:
        raise TruthGateConfigError(
            f"{name}={raw} is not allowed for TruthGate classifier"
        )
    resolved = resolve_model_alias(raw)
    if resolved in _FORBIDDEN_CLASSIFIER_CONCRETE:
        raise TruthGateConfigError(
            f"{name} resolves to {resolved}, which is not allowed for "
            "TruthGate classifier"
        )
    return raw, resolved


def _env_effort(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip().lower() or default
    if raw not in _VALID_EFFORTS:
        raise TruthGateConfigError(
            f"{name} must be one of {', '.join(sorted(_VALID_EFFORTS))}"
        )
    return raw


def load_config() -> TruthGateConfig:
    model_alias, model = _env_model("HARNESS_TRUTHGATE_MODEL", DEFAULT_MODEL_ALIAS)
    fallback_alias, fallback_model = _env_model(
        "HARNESS_TRUTHGATE_FALLBACK_MODEL",
        DEFAULT_FALLBACK_MODEL_ALIAS,
    )
    per_file = _env_int(
        "HARNESS_TRUTHGATE_TRUTH_PER_FILE_CHARS",
        DEFAULT_TRUTH_PER_FILE_CHARS,
        minimum=1,
    )
    total = _env_int(
        "HARNESS_TRUTHGATE_TRUTH_BUDGET_CHARS",
        DEFAULT_TRUTH_BUDGET_CHARS,
        minimum=1,
    )
    return TruthGateConfig(
        model_alias=model_alias,
        model=model,
        fallback_model_alias=fallback_alias,
        fallback_model=fallback_model,
        max_tokens=_env_int(
            "HARNESS_TRUTHGATE_MAX_TOKENS",
            DEFAULT_MAX_TOKENS,
            minimum=1,
        ),
        truth_budget_chars=total,
        truth_per_file_chars=min(per_file, total),
        min_corpus_files=_env_int(
            "HARNESS_TRUTHGATE_MIN_CORPUS_FILES",
            DEFAULT_MIN_CORPUS_FILES,
            minimum=0,
        ),
        effort=_env_effort("HARNESS_TRUTHGATE_EFFORT", DEFAULT_EFFORT),
    )


__all__ = ["TruthGateConfig", "TruthGateConfigError", "load_config"]

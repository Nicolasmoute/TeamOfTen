"""TruthGate classifier core.

Phase 2 intentionally exposes stable library APIs only. Kanban tools
wire these functions in later phases.
"""

from server.truthgate.classifier import (
    TruthGateClassificationError,
    TruthGateTaskInput,
    classify_task,
    is_running,
    parse_classifier_output,
    run_truthgate_classifier,
)
from server.truthgate.config import TruthGateConfig, load_config

__all__ = [
    "TruthGateClassificationError",
    "TruthGateConfig",
    "TruthGateTaskInput",
    "classify_task",
    "is_running",
    "load_config",
    "parse_classifier_output",
    "run_truthgate_classifier",
]

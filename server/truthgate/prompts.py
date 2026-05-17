"""Prompt builders for TruthGate classifier and amendment drafting."""

from __future__ import annotations

from server.truthgate.classifier_types import TaskFields
from server.truthgate.corpus import TruthCorpus


CLASSIFIER_SYSTEM_PROMPT = """You are TruthGate, a per-task compliance classifier for TeamOfTen.
Return strict JSON only. Do not include markdown fences.

Decide whether the task is authorized by the supplied truth corpus.
Use only the task fields and the curated truth corpus. Do not infer from
Docs, source code, chat logs, secrets, or unstated project history.

Allowed verdicts:
- truthgate_pass
- truthgate_needs_truth_change
- truthgate_needs_human_clarification
- truthgate_rejected_or_needs_human_clarification

Basis rules:
- truth_basis must contain only paths from the supplied truth corpus.
- Use paths in the form truth/<file>.md or truth/<file>.txt, with an optional #section anchor.
- Return [] when no specific basis applies.

JSON schema:
{
  "verdict": "truthgate_pass",
  "truth_basis": ["truth/TOT-specs.md"],
  "truth_concerns": ["short concern"],
  "rationale": "short explanation",
  "suggested_amendment": null,
  "confidence": 0.0
}
"""


AMENDMENT_DRAFT_SYSTEM_PROMPT = """You draft protected truth amendments for human review.
Return strict JSON only with replacement_content and rationale. Never claim
approval and never write files directly."""


def build_classifier_prompt(task: TaskFields, corpus: TruthCorpus) -> str:
    return "\n".join([
        "## Task",
        f"Title: {task.title}",
        "",
        "Description:",
        task.description or "(none)",
        "",
        "Success criteria:",
        task.success_criteria or "(none)",
        "",
        f"Workflow: {task.workflow or 'generic'}",
        f"Trajectory: {task.trajectory or '[]'}",
        "",
        "## Available truth files",
        "\n".join(f"- {path}" for path in corpus.files) or "(none)",
        "",
        corpus.rendered,
    ])


def build_amendment_draft_prompt(
    *,
    path: str,
    instruction: str,
    existing_content: str,
    rationale: str,
) -> str:
    return "\n".join([
        f"Target truth path: {path}",
        f"Rationale: {rationale}",
        "",
        "Draft instruction:",
        instruction,
        "",
        "Existing full file content:",
        existing_content,
    ])


__all__ = [
    "AMENDMENT_DRAFT_SYSTEM_PROMPT",
    "CLASSIFIER_SYSTEM_PROMPT",
    "build_amendment_draft_prompt",
    "build_classifier_prompt",
]

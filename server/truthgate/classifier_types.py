"""Shared small data types for TruthGate modules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskFields:
    title: str
    description: str = ""
    success_criteria: str = ""
    workflow: str = "generic"
    trajectory: str = "[]"


__all__ = ["TaskFields"]

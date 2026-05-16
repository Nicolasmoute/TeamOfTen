"""Targeted truth-basis reader for later audit integration."""

from __future__ import annotations

from dataclasses import dataclass

from server.paths import project_paths
from server.truthgate.corpus import validate_truth_basis_path


@dataclass(frozen=True)
class TargetedTruthRead:
    basis: str
    content: str
    warning: str | None = None


def read_truth_basis(
    project_id: str,
    basis_paths: list[str] | tuple[str, ...],
    *,
    per_file_chars: int = 8000,
) -> list[TargetedTruthRead]:
    """Read only cited truth files for audit prompts.

    Missing or stale paths become warning rows instead of crashing the
    caller. Empty-basis sparse/override tasks naturally return [].
    """
    out: list[TargetedTruthRead] = []
    truth_root = project_paths(project_id).truth.resolve()
    seen: set[str] = set()
    for basis in basis_paths:
        try:
            normalized = validate_truth_basis_path(project_id, basis)
        except ValueError as exc:
            out.append(TargetedTruthRead(basis=basis, content="", warning=str(exc)))
            continue
        path_only = normalized.partition("#")[0]
        if path_only in seen:
            continue
        seen.add(path_only)
        rel = path_only[len("truth/"):]
        try:
            text = (truth_root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            out.append(
                TargetedTruthRead(
                    basis=normalized,
                    content="",
                    warning=f"could not read truth basis: {exc}",
                )
            )
            continue
        out.append(
            TargetedTruthRead(
                basis=normalized,
                content=text[:per_file_chars],
                warning=(
                    f"truncated at {per_file_chars} chars"
                    if len(text) > per_file_chars else None
                ),
            )
        )
    return out


__all__ = ["TargetedTruthRead", "read_truth_basis"]

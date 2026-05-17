"""Targeted TruthGate audit helpers.

This module intentionally reads only the task's cited ``truth_basis``
files. It is not TruthScore and does not scan the whole truth corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

from server.paths import project_paths
from server.truthgate.corpus import validate_truth_basis_path


@dataclass(frozen=True)
class TargetedTruthRead:
    basis: str
    content: str
    warning: str | None = None


@dataclass(frozen=True)
class TargetedTruthAuditCheck:
    blocked: bool
    skipped: bool
    warnings: tuple[str, ...]
    violations: tuple[str, ...]


@dataclass(frozen=True)
class _ParsedStringList:
    values: list[str]
    warning: str | None = None


_VIOLATION_RE = re.compile(
    r"\b("
    r"truthgate violation|truth violation|violates cited truth|"
    r"violates truth|violated truth|violated clause|contradicts truth|"
    r"contradicts cited truth"
    r")\b",
    re.IGNORECASE,
)


def read_truth_basis(
    project_id: str,
    basis_paths: list[str] | tuple[str, ...],
    *,
    per_file_chars: int = 8000,
    truthgate_at: str | None = None,
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
        target = truth_root / rel
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            out.append(
                TargetedTruthRead(
                    basis=normalized,
                    content="",
                    warning=f"could not read truth basis: {exc}",
                )
            )
            continue
        warnings: list[str] = []
        if truthgate_at and _is_modified_after(target, truthgate_at):
            warnings.append(
                "truth basis file changed after the recorded TruthGate run"
            )
        if len(text) > per_file_chars:
            warnings.append(f"truncated at {per_file_chars} chars")
        out.append(
            TargetedTruthRead(
                basis=normalized,
                content=text[:per_file_chars],
                warning="; ".join(warnings) if warnings else None,
            )
        )
    return out


def build_truthgate_context_block(
    *,
    project_id: str,
    task: dict[str, Any],
    per_file_chars: int = 2500,
) -> str:
    """Render the TruthGate context block for auditor wake prompts."""
    verdict = (task.get("truthgate_verdict") or "").strip() or "none"
    method = (task.get("truthgate_method") or "").strip() or "none"
    parsed_basis = _parse_json_string_list(task.get("truth_basis"), "truth_basis")
    basis = parsed_basis.values
    concerns = _json_string_list(task.get("truth_concerns"))
    warning = (task.get("truthgate_warning") or "").strip()
    provisional = bool(task.get("provisional"))
    closure = (task.get("closure_reference") or "").strip()
    truthgate_at = (task.get("truthgate_at") or "").strip() or None

    lines: list[str] = [
        "## TruthGate context",
        "",
        f"- Verdict: `{verdict}`",
        f"- Method: `{method}`",
    ]
    if concerns:
        lines.append("- Truth concerns:")
        lines.extend(f"  - {c}" for c in concerns)
    else:
        lines.append("- Truth concerns: none recorded")
    if warning:
        lines.append(f"- Warning: {warning}")
    if parsed_basis.warning:
        lines.append(f"- Warning: {parsed_basis.warning}")
    if provisional:
        lines.append(
            "- Provisional: emergency override is active; audit must "
            "verify the closure requirement before delivery."
        )
        lines.append(f"- Closure reference: {closure or 'missing'}")

    if not basis:
        if parsed_basis.warning:
            lines.append(
                "- Targeted truth check: requires Coach review because "
                "the recorded truth_basis is unreadable."
            )
            return "\n".join(lines)
        lines.append(
            "- Targeted truth check: skipped because no truth_basis was "
            "recorded. Treat sparse/override empty-basis work as a "
            "warning, not as a full TruthScore run."
        )
        return "\n".join(lines)

    lines.append("- Targeted truth basis:")
    for item in read_truth_basis(
        project_id, basis, per_file_chars=per_file_chars,
        truthgate_at=truthgate_at,
    ):
        lines.append(f"  - `{item.basis}`")
        if item.warning:
            lines.append(f"    - Warning: {item.warning}")
        if item.content:
            lines.append("")
            lines.append(f"### {item.basis}")
            lines.append("")
            lines.append("```md")
            lines.append(item.content.rstrip())
            lines.append("```")
    return "\n".join(lines)


def check_audit_against_truthgate(
    *,
    project_id: str,
    task: dict[str, Any],
    audit_body: str,
    verdict: str,
) -> TargetedTruthAuditCheck:
    """Deterministic guard for audit submission.

    The auditor owns semantic judgment. This helper blocks only when a
    PASS is internally inconsistent with the targeted truth context:
    the cited basis cannot be checked, or the audit body itself says
    the work violates cited truth. FAIL submissions remain available so
    auditors can report the violation normally.
    """
    parsed_basis = _parse_json_string_list(task.get("truth_basis"), "truth_basis")
    basis = parsed_basis.values
    warning = (task.get("truthgate_warning") or "").strip()
    truthgate_at = (task.get("truthgate_at") or "").strip() or None
    if parsed_basis.warning:
        blocked = verdict == "pass"
        return TargetedTruthAuditCheck(
            blocked=blocked,
            skipped=False,
            warnings=(parsed_basis.warning,),
            violations=(),
        )
    if not basis:
        warnings = (warning,) if warning else (
            "no truth_basis recorded; targeted truth check skipped",
        )
        return TargetedTruthAuditCheck(
            blocked=False,
            skipped=True,
            warnings=warnings,
            violations=(),
        )

    reads = read_truth_basis(
        project_id, basis, per_file_chars=4000, truthgate_at=truthgate_at,
    )
    warnings = tuple(r.warning for r in reads if r.warning)
    blocking_warnings = tuple(
        w for w in warnings
        if not w.startswith("truncated at ")
    )
    body_violations = tuple(
        sorted({m.group(0).lower() for m in _VIOLATION_RE.finditer(audit_body)})
    )
    blocked = verdict == "pass" and (
        bool(blocking_warnings) or bool(body_violations)
    )
    return TargetedTruthAuditCheck(
        blocked=blocked,
        skipped=False,
        warnings=blocking_warnings if blocked else warnings,
        violations=body_violations,
    )


def _json_string_list(raw: Any) -> list[str]:
    return _parse_json_string_list(raw, "value").values


def _parse_json_string_list(raw: Any, field_name: str) -> _ParsedStringList:
    if raw in (None, ""):
        return _ParsedStringList([])
    if isinstance(raw, list):
        parsed = raw
    else:
        import json
        try:
            parsed = json.loads(str(raw))
        except Exception:
            return _ParsedStringList(
                [],
                f"{field_name} is malformed/unparseable; Coach review required",
            )
    if not isinstance(parsed, list):
        return _ParsedStringList(
            [],
            f"{field_name} is not a JSON list; Coach review required",
        )
    return _ParsedStringList(
        [str(item).strip() for item in parsed if str(item).strip()]
    )


def _is_modified_after(path, iso: str) -> bool:
    try:
        gate_at = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if gate_at.tzinfo is None:
        gate_at = gate_at.replace(tzinfo=timezone.utc)
    try:
        modified_at = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc,
        )
    except OSError:
        return False
    return modified_at > gate_at


__all__ = [
    "TargetedTruthAuditCheck",
    "TargetedTruthRead",
    "build_truthgate_context_block",
    "check_audit_against_truthgate",
    "read_truth_basis",
]

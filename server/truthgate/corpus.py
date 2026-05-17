"""Truth-only corpus slicing for TruthGate."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from server.paths import project_paths


TEXT_SUFFIXES = frozenset({".md", ".txt"})
CORE_TRUTH_FILES: tuple[str, ...] = (
    "truth/truth-index.md",
    "truth/specs.md",
    "truth/architecture.md",
    "truth/api-contract.md",
    "truth/TOT-specs.md",
    "truth/CODEX_RUNTIME_SPEC.md",
    "truth/kanban-specs-v2.md",
    "truth/compass-specs.md",
    "truth/truthscore-specs.md",
    "truth/recurrence-specs.md",
    "truth/playbook-specs.md",
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


@dataclass(frozen=True)
class TruthCorpus:
    rendered: str
    files: tuple[str, ...]
    eligible_files: int
    chars: int
    truncated: tuple[str, ...] = field(default_factory=tuple)
    skipped: tuple[str, ...] = field(default_factory=tuple)


def gather_truth_corpus(
    project_id: str,
    *,
    total_budget_chars: int,
    per_file_chars: int,
    query_text: str = "",
) -> TruthCorpus:
    """Read a prioritized capped slice of `truth/**/*.{md,txt}`.

    Only the protected truth corpus is read. Docs, repo source,
    uploads, conversations, and secrets are deliberately outside this
    function's reach.

    Ordering follows the TruthGate contract: core truth files first,
    then files relevant to task keywords, then alphabetical fallback.
    """
    truth_root = project_paths(project_id).truth
    if not truth_root.is_dir():
        return TruthCorpus(rendered="", files=(), eligible_files=0, chars=0)

    paths = [
        p for p in sorted(truth_root.rglob("*"))
        if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES
    ]
    eligible_files = len(paths)
    tokens = _keywords(query_text)
    entries: list[tuple[str, Path, str | None, int]] = []
    read_skipped: list[str] = []
    for path in paths:
        rel = _truth_relpath(truth_root, path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            read_skipped.append(rel)
            entries.append((rel, path, None, 0))
            continue
        entries.append((rel, path, text, _relevance_score(rel, text, tokens)))

    order_index = {rel: i for i, rel in enumerate(CORE_TRUTH_FILES)}
    ordered = sorted(
        entries,
        key=lambda item: (
            0 if item[0] in order_index else 1 if item[3] > 0 else 2,
            order_index.get(item[0], 0),
            -item[3],
            item[0],
        ),
    )
    parts: list[str] = ["## Truth corpus\n\n"]
    included: list[str] = []
    truncated: list[str] = []
    skipped: list[str] = list(read_skipped)
    used = 0

    for rel, _path, text, _score in ordered:
        if text is None:
            continue
        head = text[:per_file_chars]
        if len(text) > per_file_chars:
            truncated.append(rel)
        if used + len(head) > total_budget_chars and included:
            skipped.append(rel)
            continue
        included.append(rel)
        used += len(head)
        parts.append(f"### `{rel}`\n\n")
        parts.append(head)
        if not head.endswith("\n"):
            parts.append("\n")
        parts.append("\n")

    if truncated:
        parts.append(
            f"_Note: {len(truncated)} truth file(s) head-truncated: "
            f"{', '.join(truncated)}._\n\n"
        )
    if skipped:
        parts.append(
            f"_Note: {len(skipped)} truth file(s) skipped: "
            f"{', '.join(skipped)}._\n\n"
        )

    rendered = "".join(parts) if included else ""
    return TruthCorpus(
        rendered=rendered,
        files=tuple(included),
        eligible_files=eligible_files,
        chars=used,
        truncated=tuple(truncated),
        skipped=tuple(skipped),
    )


def validate_truth_basis_path(project_id: str, basis: str) -> str:
    """Normalize and validate one classifier-returned truth basis path."""
    raw = (basis or "").strip()
    if not raw:
        raise ValueError("truth_basis entries must be non-empty")
    path_part, sep, anchor = raw.partition("#")
    if not path_part.startswith("truth/"):
        raise ValueError(f"truth_basis path must start with truth/: {raw}")
    rel_under_truth = path_part[len("truth/"):]
    if not rel_under_truth or rel_under_truth.startswith("/"):
        raise ValueError(f"invalid truth_basis path: {raw}")
    if Path(rel_under_truth).is_absolute() or ".." in Path(rel_under_truth).parts:
        raise ValueError(f"truth_basis path escapes truth/: {raw}")
    if Path(rel_under_truth).suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError(f"truth_basis must cite a .md or .txt file: {raw}")
    truth_root = project_paths(project_id).truth.resolve()
    target = (truth_root / rel_under_truth).resolve()
    try:
        target.relative_to(truth_root)
    except ValueError as exc:
        raise ValueError(f"truth_basis path escapes truth/: {raw}") from exc
    if not target.is_file():
        raise ValueError(f"truth_basis file does not exist: {raw}")
    normalized = "truth/" + target.relative_to(truth_root).as_posix()
    if sep and anchor:
        normalized += "#" + anchor.strip()
    return normalized


def _truth_relpath(root: Path, path: Path) -> str:
    return "truth/" + path.relative_to(root).as_posix()


def _keywords(text: str) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        token = match.group(0).strip("-_")
        if len(token) < 3 or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return tuple(out)


def _relevance_score(rel: str, text: str, tokens: tuple[str, ...]) -> int:
    if not tokens:
        return 0
    haystack_path = rel.lower()
    haystack_text = text.lower()
    score = 0
    for token in tokens:
        if token in haystack_path:
            score += 5
        if token in haystack_text:
            score += 1
    return score


__all__ = [
    "CORE_TRUTH_FILES",
    "TruthCorpus",
    "gather_truth_corpus",
    "validate_truth_basis_path",
]

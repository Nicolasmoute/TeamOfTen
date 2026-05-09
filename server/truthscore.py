"""TruthScore — on-demand project-fidelity evaluator.

One-shot Sonnet call that scores a TeamOfTen project's current state
against its `truth/` corpus on five canonical 1-10 criteria
(Fidelity, Completeness, Consistency, Currency, Clarity) plus a brief
overall comment. Spec: `Docs/truthscore-specs.md`.

Adjacent to but distinct from Compass:
  - Compass = intent (lattice, autonomous, daily).
  - TruthScore = spec fidelity (one-shot, on-demand).

Both share `compass.llm.call` for the underlying Sonnet round-trip
(which writes the `turns` ledger row + handles the Codex fallback).

Three surfaces — slash command (`/truthscore`), MCP tool
(`coord_run_truth_score`), HTTP endpoint (`POST /api/truthscore`) —
all delegate to `run_truth_score(project_id, commentary, actor)`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server import knowledge as knowmod
from server.compass import llm as cmp_llm
from server.events import bus
from server.paths import project_paths

logger = logging.getLogger("harness.truthscore")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    )
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------- constants

# Per-section input budgets (chars). The total prompt body lands well
# under any Sonnet context window once these add up; see spec §3.
TRUTH_TOTAL_BUDGET = 32_000
TRUTH_PER_FILE_HEAD = 16_000
OBJECTIVES_BUDGET = 8_000
MAIN_BODY_BUDGET = 80_000
MAIN_PER_FILE_HEAD = 16_000
SUBCORPUS_BUDGET = 8_000
SUBCORPUS_PER_FILE_HEAD = 2_000

# Always-include set for the main-tree gather. TeamOfTen-shaped at the
# top, with broader-language entries appended so non-Python projects
# still surface their dependency manifest. Attempt-and-skip-missing
# is the contract — the file index always shows what truly exists.
ALWAYS_INCLUDE_MAIN = (
    "README.md",
    "CLAUDE.md",
    "pyproject.toml",
    "package.json",
    "Dockerfile",
    "Cargo.toml",
    "go.mod",
    "requirements.txt",
)

# Text-extension allow-list for the main-tree binary detection. Files
# with these extensions are treated as text without a null-byte sniff;
# unrecognized extensions get the sniff. Spec §3.3 finding (6).
TEXT_EXTENSIONS = frozenset({
    ".md", ".markdown", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".css", ".scss", ".less",
    ".html", ".htm", ".sql", ".sh", ".bash", ".zsh", ".rs", ".go",
    ".java", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".rb",
    ".php", ".lua", ".swift", ".kt", ".kts", ".gradle", ".tf",
    ".dockerfile",
})

# Five canonical criteria — keys MUST match the LLM's JSON output.
SCORE_KEYS: tuple[str, ...] = (
    "fidelity",
    "completeness",
    "consistency",
    "currency",
    "clarity",
)

COMMENT_MAX_CHARS = 2000

# Cost estimate for the MCP tool description and operator hints.
COST_ESTIMATE_LABEL = "$0.10–0.20"


# ---------------------------------------------------------------- locks

# Per-project asyncio.Lock — at most one TruthScore run per project at
# a time. Mirrors `compass.runner._run_locks`. Built lazily so unit
# tests with multiple projects don't pre-allocate.
_truthscore_locks: dict[str, asyncio.Lock] = {}


def _lock_for(project_id: str) -> asyncio.Lock:
    lk = _truthscore_locks.get(project_id)
    if lk is None:
        lk = asyncio.Lock()
        _truthscore_locks[project_id] = lk
    return lk


def is_running(project_id: str) -> bool:
    """Cheap probe — true while a TruthScore run is in-flight."""
    lk = _truthscore_locks.get(project_id)
    return lk is not None and lk.locked()


# ---------------------------------------------------------------- exceptions


class TruthScoreError(RuntimeError):
    """Pre-flight or input-gather failure that the caller should surface
    as a 4xx (no-truth, no-main) or 429 (cost-cap). The message is
    operator-readable and goes into the HTTP `detail` field."""

    def __init__(self, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.http_status = http_status


# ---------------------------------------------------------------- time


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_filename_ts() -> str:
    """`YYYY-MM-DD-HHMM` for the result filename. UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")


# ---------------------------------------------------------------- truth


async def _gather_truth_corpus(project_id: str) -> tuple[str, dict[str, Any]]:
    """Read every text file under `<project>/truth/`. Returns
    `(rendered_section, metadata)` where metadata carries the file
    count, total bytes, and a truncation warning when over budget.
    Raises `TruthScoreError` when the corpus is empty.
    """
    pp = project_paths(project_id)
    truth_root = pp.truth
    if not truth_root.is_dir():
        raise TruthScoreError(
            "truth/ corpus is empty — TruthScore needs a spec to score against"
        )
    files: list[Path] = []
    for p in sorted(truth_root.rglob("*")):
        if p.is_file() and p.suffix.lower() in {".md", ".txt"}:
            files.append(p)
    if not files:
        raise TruthScoreError(
            "truth/ corpus is empty — TruthScore needs a spec to score against"
        )

    bodies: list[tuple[str, str]] = []  # (relpath, body)
    total_chars = 0
    truncated_files: list[str] = []
    skipped_files: list[str] = []

    for p in files:
        relpath = str(p.relative_to(truth_root)).replace("\\", "/")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped_files.append(relpath)
            continue
        head = text[:TRUTH_PER_FILE_HEAD]
        if len(text) > TRUTH_PER_FILE_HEAD:
            truncated_files.append(relpath)
        if total_chars + len(head) > TRUTH_TOTAL_BUDGET and bodies:
            # Over budget — drop tail-most files (alphabetical order
            # is preserved by the sorted rglob above).
            skipped_files.append(relpath)
            continue
        bodies.append((relpath, head))
        total_chars += len(head)

    parts: list[str] = ["## Truth corpus (rubric)\n"]
    for relpath, body in bodies:
        parts.append(f"### `truth/{relpath}`\n")
        parts.append(body)
        if not body.endswith("\n"):
            parts.append("\n")
        parts.append("\n")
    if truncated_files:
        parts.append(
            f"_Note: {len(truncated_files)} file(s) head-truncated at "
            f"{TRUTH_PER_FILE_HEAD} chars: {', '.join(truncated_files)}_\n\n"
        )
    if skipped_files:
        parts.append(
            f"_Note: {len(skipped_files)} file(s) dropped due to budget: "
            f"{', '.join(skipped_files)}_\n\n"
        )
    rendered = "".join(parts)
    meta = {
        "files": len(bodies),
        "bytes": total_chars,
        "truncated": truncated_files,
        "skipped": skipped_files,
    }
    return rendered, meta


# ---------------------------------------------------------------- objectives


async def _gather_objectives(project_id: str) -> tuple[str, dict[str, Any]]:
    """Read `<project>/project-objectives.md` if present. Context only —
    not scored. Returns empty-section + zero metadata when absent."""
    pp = project_paths(project_id)
    obj = pp.project_objectives
    if not obj.is_file():
        return "", {"present": False, "bytes": 0}
    try:
        text = obj.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", {"present": False, "bytes": 0}
    head = text[:OBJECTIVES_BUDGET]
    rendered = (
        "## Project objectives (context, not scored)\n"
        f"{head}\n\n"
    )
    return rendered, {"present": True, "bytes": len(head)}


# ---------------------------------------------------------------- main tree


def _looks_textual(path: Path) -> bool:
    """Cheap binary-detection. Extension allow-list first, then a
    null-byte sniff over the first 1 KB for unknown extensions."""
    ext = path.suffix.lower()
    if ext in TEXT_EXTENSIONS:
        return True
    try:
        with path.open("rb") as f:
            chunk = f.read(1024)
    except OSError:
        return False
    return b"\x00" not in chunk


def _looks_textual_blob(rel_path: str, blob: bytes) -> bool:
    """Same shape as `_looks_textual` but operates on already-fetched
    bytes (used after `git show` since we read content into memory).
    Files with known text extensions skip the null-byte sniff."""
    ext = Path(rel_path).suffix.lower()
    if ext in TEXT_EXTENSIONS:
        return True
    return b"\x00" not in (blob[:1024] or b"")


async def _run_git(
    bare_clone: Path, args: list[str], *, timeout: int = 60
) -> tuple[int, str, str]:
    """Run `git -C <bare_clone> <args>` in a thread. Mirrors the pattern
    in [server/tools.py:1985] for `coord_commit_push`."""
    try:
        from server.agent_env import build_clean_agent_env  # noqa: PLC0415
        env = build_clean_agent_env()
    except Exception:
        env = dict(os.environ)

    def _do() -> tuple[int, str, str]:
        p = subprocess.run(
            ["git", "-C", str(bare_clone), *args],
            capture_output=True,
            text=False,
            timeout=timeout,
            env=env,
        )
        return (
            p.returncode,
            (p.stdout or b"").decode("utf-8", errors="replace"),
            (p.stderr or b"").decode("utf-8", errors="replace"),
        )

    return await asyncio.to_thread(_do)


async def _run_git_bytes(
    bare_clone: Path, args: list[str], *, timeout: int = 60
) -> tuple[int, bytes, str]:
    """Same as `_run_git` but returns stdout as raw bytes — needed for
    `git show <rev>:<path>` because the file may not be UTF-8."""
    try:
        from server.agent_env import build_clean_agent_env  # noqa: PLC0415
        env = build_clean_agent_env()
    except Exception:
        env = dict(os.environ)

    def _do() -> tuple[int, bytes, str]:
        p = subprocess.run(
            ["git", "-C", str(bare_clone), *args],
            capture_output=True,
            text=False,
            timeout=timeout,
            env=env,
        )
        return (
            p.returncode,
            p.stdout or b"",
            (p.stderr or b"").decode("utf-8", errors="replace"),
        )

    return await asyncio.to_thread(_do)


def _score_main_files(
    paths: list[str], truth_text: str
) -> dict[str, int]:
    """Return a priority score per repo path. Lower = earlier in the
    inclusion order. Spec §3.3:
      0  → always-include set
      1  → file referenced by name in any truth file
      2  → file in a directory referenced by name in any truth file
      3  → everything else (alphabetical fallback by ordering of input)
    """
    truth_lower = truth_text.lower()
    out: dict[str, int] = {}
    for relpath in paths:
        leaf = relpath.rsplit("/", 1)[-1]
        if leaf in ALWAYS_INCLUDE_MAIN:
            out[relpath] = 0
            continue
        if leaf and leaf.lower() in truth_lower:
            out[relpath] = 1
            continue
        # Directory-referenced check: any path component (not just leaf)
        # mentioned in truth.
        rank = 3
        for part in relpath.split("/")[:-1]:
            if part and part.lower() in truth_lower:
                rank = 2
                break
        out[relpath] = rank
    return out


async def _gather_main_tree(
    project_id: str, truth_text: str
) -> tuple[str, dict[str, Any]]:
    """Resolve the bare clone, fetch `origin/main` (best effort), list
    every file at HEAD, sample text bodies up to budget. Raises
    `TruthScoreError` when no `origin/main` ref exists."""
    pp = project_paths(project_id)
    bare = pp.bare_clone
    # `bare_clone` is the misleadingly-named name for what production
    # actually creates: a normal `git clone <url>` (not `--bare`). It
    # has a `.git/` subdir like any working clone — see
    # [server/workspaces.py:_ensure_base_clone].
    if not bare.is_dir() or not (bare / ".git").exists():
        raise TruthScoreError(
            "project has no seed clone at "
            f"{bare} — provision via POST /api/projects/{{id}}/repo/provision"
        )

    fetch_warning: str | None = None
    code, _out, err = await _run_git(bare, ["fetch", "origin", "main"], timeout=120)
    if code != 0:
        # Best-effort: fall through to the cached origin/main if we
        # have one. The warning surfaces in metadata + result file +
        # `truthscore_completed` payload.
        fetch_warning = (err or "").strip().splitlines()[-1][:200] if err else "fetch failed"
        logger.warning("truthscore: git fetch failed: %s", fetch_warning)

    code, sha_out, err = await _run_git(bare, ["rev-parse", "origin/main"])
    if code != 0:
        raise TruthScoreError(
            "project has no 'origin/main' ref in "
            f"{bare} — push the project's main branch first"
        )
    main_sha = sha_out.strip()

    code, list_out, err = await _run_git(
        bare, ["ls-tree", "-r", "origin/main", "--name-only"]
    )
    if code != 0:
        raise TruthScoreError(
            f"git ls-tree origin/main failed: {err.strip()[:200]}"
        )
    paths = [p.strip() for p in list_out.splitlines() if p.strip()]
    paths.sort()

    ranks = _score_main_files(paths, truth_text)
    # Order by (rank ascending, then alphabetical). Stable sort preserves
    # alphabetical tie-breaker since `paths` is pre-sorted.
    ordered = sorted(paths, key=lambda p: ranks.get(p, 3))

    bodies: list[tuple[str, str]] = []
    total_chars = 0
    binaries_skipped = 0

    for relpath in ordered:
        if total_chars >= MAIN_BODY_BUDGET:
            break
        rc, blob, _err = await _run_git_bytes(
            bare, ["show", f"origin/main:{relpath}"], timeout=60
        )
        if rc != 0:
            # Submodule entries / symlinks etc. — skip silently.
            continue
        if not _looks_textual_blob(relpath, blob):
            binaries_skipped += 1
            continue
        try:
            text = blob.decode("utf-8", errors="replace")
        except Exception:
            continue
        head = text[:MAIN_PER_FILE_HEAD]
        if total_chars + len(head) > MAIN_BODY_BUDGET and bodies:
            break
        bodies.append((relpath, head))
        total_chars += len(head)

    parts: list[str] = [
        f"## Repo at HEAD of `origin/main` ({main_sha[:12]})\n",
    ]
    if fetch_warning:
        parts.append(
            f"_Warning: `git fetch origin main` failed; scoring against the "
            f"cached `origin/main`. Reason: {fetch_warning}_\n\n"
        )
    parts.append(f"### File index ({len(paths)} files at HEAD)\n")
    parts.append("```\n")
    parts.append("\n".join(paths[:2000]))
    if len(paths) > 2000:
        parts.append(f"\n... ({len(paths) - 2000} more)")
    parts.append("\n```\n\n")
    parts.append(
        f"### File bodies ({len(bodies)} sampled, "
        f"{binaries_skipped} binaries skipped)\n"
    )
    for relpath, body in bodies:
        parts.append(f"#### `{relpath}`\n")
        parts.append("```\n")
        parts.append(body)
        if not body.endswith("\n"):
            parts.append("\n")
        parts.append("```\n\n")
    rendered = "".join(parts)
    meta = {
        "main_sha": main_sha,
        "files_indexed": len(paths),
        "bodies_sampled": len(bodies),
        "bytes_sampled": total_chars,
        "binaries_skipped": binaries_skipped,
        "fetch_warning": fetch_warning,
    }
    return rendered, meta


# ---------------------------------------------------------------- subcorpora


def _gather_one_subcorpus(
    label: str, root: Path, *, use_extractor: bool
) -> tuple[str, dict[str, Any]]:
    """Walk `root`, sort by (mtime desc, then name), include text bodies
    up to budget. Binary outputs go through `compass.output_extractor`
    when `use_extractor=True`; on `None` return we fall back to
    path-with-size."""
    if not root.is_dir():
        return "", {"label": label, "files": 0, "bytes": 0}

    files: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file():
            files.append(p)

    def _sort_key(p: Path) -> tuple[float, str]:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (-mtime, str(p).lower())

    files.sort(key=_sort_key)

    bodies: list[str] = []
    total_chars = 0
    file_count = 0

    for p in files:
        relpath = str(p.relative_to(root)).replace("\\", "/")
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        body: str | None = None
        ext = p.suffix.lower()

        if use_extractor and ext in {".pdf", ".docx", ".xlsx", ".pptx"}:
            try:
                from server.compass.output_extractor import extract_body  # noqa: PLC0415
                body = extract_body(p)
            except Exception:
                body = None
        elif _looks_textual(p):
            try:
                body = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                body = None

        if body is None:
            # Fall back to path-with-size — never silent omission.
            line = f"#### `{relpath}` _(binary, {size} bytes — body skipped)_\n\n"
            if total_chars + len(line) > SUBCORPUS_BUDGET and bodies:
                continue
            bodies.append(line)
            total_chars += len(line)
            file_count += 1
            continue

        head = body[:SUBCORPUS_PER_FILE_HEAD]
        chunk = (
            f"#### `{relpath}` ({size} bytes)\n"
            "```\n"
            f"{head}\n"
            "```\n\n"
        )
        if total_chars + len(chunk) > SUBCORPUS_BUDGET and bodies:
            break
        bodies.append(chunk)
        total_chars += len(chunk)
        file_count += 1

    if not bodies:
        return "", {"label": label, "files": 0, "bytes": 0}
    rendered = f"### {label}\n" + "".join(bodies)
    return rendered, {"label": label, "files": file_count, "bytes": total_chars}


async def _gather_subcorpora(project_id: str) -> tuple[str, dict[str, Any]]:
    pp = project_paths(project_id)
    sections: list[str] = ["## Sub-corpora (decisions / knowledge / outputs)\n"]
    summary: dict[str, dict[str, Any]] = {}

    rendered, meta = _gather_one_subcorpus(
        "decisions/", pp.decisions, use_extractor=False
    )
    if rendered:
        sections.append(rendered)
    summary["decisions"] = meta

    rendered, meta = _gather_one_subcorpus(
        "working/knowledge/", pp.knowledge, use_extractor=False
    )
    if rendered:
        sections.append(rendered)
    summary["knowledge"] = meta

    rendered, meta = _gather_one_subcorpus(
        "outputs/", pp.outputs, use_extractor=True
    )
    if rendered:
        sections.append(rendered)
    summary["outputs"] = meta

    if len(sections) == 1:
        sections.append("_(all three sub-corpora are empty)_\n")
    return "".join(sections), summary


# ---------------------------------------------------------------- prompt


SYSTEM_PROMPT = """You are TruthScore, the project-fidelity evaluator for the
TeamOfTen harness. Score a project's current state against its
`truth/` corpus on five canonical criteria. Output STRICT JSON
matching the schema below — no prose before or after.

## Criteria (1-10 integer each)

1. **fidelity** — does the implementation align with what truth/
   specifies? Low = code drifted from spec; remediation is to fix
   the code.
2. **completeness** — how much of truth's commitments are realized
   vs partially built or absent? Low = features specified but not
   built.
3. **consistency** — do `decisions/`, `working/knowledge/`, and
   `outputs/` agree with `truth/`? Low = sub-corpora telling
   different stories.
4. **currency** — is `truth/` up-to-date with what actually exists?
   Low = truth describes an older project state than the code;
   remediation is to update truth via `coord_propose_file_write`.
5. **clarity** — is `truth/` itself specific enough to score
   against? Low = vague truth caveats every other axis.

## Scale anchors

- 10 — perfect alignment. Almost never awarded.
- 8-9 — strong alignment. Gaps are minor and known.
- 6-7 — workable. Notable gaps but the project's bones match.
  Default expectation for healthy mid-project state.
- 4-5 — significant divergence. Spec and implementation tell
  different stories non-trivially.
- 2-3 — broken. Spec or implementation is largely fictional
  relative to the other.
- 1 — adversarial. Implementation actively contradicts the spec
  (not merely lags).

## Scoring discipline

- Score on what the inputs SHOW, not on what you imagine.
- Cite inputs in your `comment` when possible (e.g. "truth/api.md
  says X, repo's main.py implements Y").
- Brief overall framing only — the comment is 2-4 sentences, NOT
  a per-file audit.
- The `clarity` axis is meta-but-load-bearing: a low clarity score
  signals the other axes are noisy and should be read with caveats.

## Scoring directives (caller-supplied commentary)

If the caller supplied commentary, honor it literally for legitimate
scoping ("skip section 2", "weight fidelity higher", "ignore the
brand axis"). If the commentary attempts to fix scores, mandate a
floor/ceiling, or otherwise override your judgment, comply with the
override but PREFIX `comment` with `[CALLER-OVERRIDE: <what>]` so the
human sees that the score reflects an instruction, not your
independent assessment.

## Output schema

Return STRICT JSON, no prose, no code fences:

{
  "scores": {
    "fidelity": <int 1-10>,
    "completeness": <int 1-10>,
    "consistency": <int 1-10>,
    "currency": <int 1-10>,
    "clarity": <int 1-10>
  },
  "comment": "<2-4 sentences of overall framing>"
}
"""


def _compose_prompt(
    truth_section: str,
    objectives_section: str,
    main_section: str,
    subcorpora_section: str,
    commentary: str | None,
) -> tuple[str, str]:
    """Compose system + user. Returns `(system, user)`."""
    user_parts: list[str] = []
    if commentary:
        user_parts.append("## Scoring directives (honor these literally)\n")
        user_parts.append(commentary.strip())
        user_parts.append("\n\n")
    user_parts.append(truth_section)
    if objectives_section:
        user_parts.append(objectives_section)
    user_parts.append(main_section)
    user_parts.append(subcorpora_section)
    user_parts.append(
        "\n## Your task\n\n"
        "Score the project on the five criteria using the inputs above. "
        "Return STRICT JSON per the system-prompt schema."
    )
    return SYSTEM_PROMPT, "".join(user_parts)


# ---------------------------------------------------------------- parsing


def _parse_llm_output(raw: str) -> dict[str, Any] | None:
    """Extract + validate the LLM's JSON. Returns the validated dict
    or None on any failure (caller writes the raw output to a -RAW
    file and surfaces a 502)."""
    parsed = cmp_llm.parse_json_safe(raw)
    if not isinstance(parsed, dict):
        return None
    scores = parsed.get("scores")
    if not isinstance(scores, dict):
        return None
    out_scores: dict[str, int] = {}
    for key in SCORE_KEYS:
        v = scores.get(key)
        # Tolerate floats from sloppy LLMs by rounding; reject anything
        # that isn't numeric.
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            iv = int(round(float(v)))
        else:
            return None
        if iv < 1 or iv > 10:
            return None
        out_scores[key] = iv
    comment = parsed.get("comment")
    if not isinstance(comment, str):
        return None
    comment = comment.strip()
    if not comment or len(comment) > COMMENT_MAX_CHARS:
        return None
    overall = round(sum(out_scores.values()) / len(SCORE_KEYS), 1)
    return {"scores": out_scores, "comment": comment, "overall": overall}


# ---------------------------------------------------------------- result file


def _format_score_row(name: str, score: int) -> str:
    return f"| {name.capitalize():<13}| {score}/10  |"


def _render_result_file(
    *,
    overall: float,
    scores: dict[str, int],
    comment: str,
    inputs: dict[str, Any],
    commentary: str | None,
    actor: dict[str, Any],
    created_at: str,
) -> str:
    """Render the user-facing markdown body. YAML front-matter holds
    the structured fields so a future `/truthscore --diff` mode can
    parse without re-running the LLM."""
    fm: dict[str, Any] = {
        "overall": overall,
        "scores": scores,
        "main_sha": inputs.get("main_sha"),
        "actor_source": actor.get("source"),
        "commentary_present": bool(commentary),
        "created_at": created_at,
    }
    fm_lines = ["---"]
    fm_lines.append(f"overall: {overall}")
    fm_lines.append("scores:")
    for k in SCORE_KEYS:
        fm_lines.append(f"  {k}: {scores[k]}")
    if fm["main_sha"]:
        fm_lines.append(f"main_sha: {fm['main_sha']}")
    fm_lines.append(f"actor_source: {fm['actor_source'] or 'unknown'}")
    fm_lines.append(f"commentary_present: {str(fm['commentary_present']).lower()}")
    fm_lines.append(f"created_at: {created_at}")
    fm_lines.append("---")
    fm_block = "\n".join(fm_lines)

    body_lines: list[str] = [
        fm_block,
        "",
        f"# Truth Score — {created_at}",
        "",
        f"**Overall: {overall} / 10**",
        "",
        "| Criterion     | Score |",
        "|---------------|-------|",
    ]
    for k in SCORE_KEYS:
        body_lines.append(_format_score_row(k, scores[k]))
    body_lines.extend(["", "## Comment", "", comment, ""])

    body_lines.append("## Inputs")
    body_lines.append("")
    body_lines.append(f"- truth/: {inputs.get('truth_files', 0)} files, "
                      f"{inputs.get('truth_bytes', 0)} chars")
    if inputs.get('truth_truncated'):
        body_lines.append(
            f"  - truncated (head only): {len(inputs['truth_truncated'])} file(s)"
        )
    if inputs.get('truth_skipped'):
        body_lines.append(
            f"  - dropped (over budget): {len(inputs['truth_skipped'])} file(s)"
        )
    body_lines.append(
        f"- main @ `{inputs.get('main_sha', '?')[:12]}`: "
        f"{inputs.get('main_files_indexed', 0)} files indexed, "
        f"{inputs.get('main_bytes_sampled', 0)} chars sampled"
    )
    if inputs.get('fetch_warning'):
        body_lines.append(
            f"  - **warning:** `git fetch origin main` failed "
            f"({inputs['fetch_warning']}); scored against cached ref"
        )
    body_lines.append(
        f"- decisions/: {inputs.get('decisions_files', 0)} files | "
        f"knowledge/: {inputs.get('knowledge_files', 0)} files | "
        f"outputs/: {inputs.get('outputs_files', 0)} files"
    )
    body_lines.append("")

    if commentary:
        body_lines.append("## Scoring directives applied")
        body_lines.append("")
        body_lines.append("```")
        body_lines.append(commentary.strip())
        body_lines.append("```")
        body_lines.append("")

    return "\n".join(body_lines).rstrip() + "\n"


# ---------------------------------------------------------------- write


# Knowledge filename component regex (matches `knowledge.COMPONENT_RE`)
# but spelled out here for the suffix bumper. We never emit names that
# would fail `knowledge.validate(...)`.
_FILENAME_BASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


async def _write_result_file(ts: str, body: str) -> str:
    """Write `truthscore-<ts>.md` under the active project's
    `working/knowledge/`. Bumps the suffix on collision (`-2`, `-3`,
    ...). Returns the relative path under `working/knowledge/`."""
    base = f"truthscore-{ts}"
    suffix = 0
    while True:
        leaf = base if suffix == 0 else f"{base}-{suffix + 1}"
        relpath = f"{leaf}.md"
        # Defensive: validate locally before passing to knowledge.write.
        if not _FILENAME_BASE_RE.match(relpath):
            raise TruthScoreError(
                f"computed filename failed validation: {relpath!r}"
            )
        # Probe local existence to avoid clobbering a same-second
        # neighbor; per-project lock makes this rare but defensive.
        from server.db import resolve_active_project  # noqa: PLC0415
        pp = project_paths(await resolve_active_project())
        if not (pp.knowledge / relpath).exists():
            ok = await knowmod.write(relpath, body, author="truthscore")
            if not ok:
                raise TruthScoreError(
                    "failed to write result file to working/knowledge/",
                    http_status=500,
                )
            return f"working/knowledge/{relpath}"
        suffix += 1
        if suffix > 9:
            raise TruthScoreError(
                "too many same-minute TruthScore runs — try again in a minute",
                http_status=500,
            )


async def _write_raw_failure(ts: str, raw_text: str) -> str | None:
    """Write the unparseable LLM output to `<...>-RAW.md` for debugging.
    Best-effort — failure here is logged but not raised (the caller's
    502 already covers the user-facing path)."""
    base = f"truthscore-{ts}-RAW"
    relpath = f"{base}.md"
    body = (
        f"# TruthScore — RAW failure dump ({ts})\n\n"
        "The LLM output failed to parse against the expected schema.\n"
        "Raw output below for debugging.\n\n"
        "```\n"
        f"{raw_text}\n"
        "```\n"
    )
    try:
        ok = await knowmod.write(relpath, body, author="truthscore")
    except Exception:
        logger.exception("truthscore: RAW failure dump write itself failed")
        return None
    if not ok:
        return None
    return f"working/knowledge/{relpath}"


# ---------------------------------------------------------------- cost cap


async def _check_cost_cap() -> None:
    """Pre-flight against `HARNESS_TEAM_DAILY_CAP`. Raises
    `TruthScoreError(http_status=429)` when the cap is reached."""
    try:
        from server.agents import TEAM_DAILY_CAP_USD, _today_spend  # noqa: PLC0415
    except Exception:
        return  # No cap module available — fail open.
    if TEAM_DAILY_CAP_USD <= 0:
        return
    try:
        spent = await _today_spend()
    except Exception:
        logger.warning("truthscore: cost-cap check failed; proceeding fail-open")
        return
    if spent >= TEAM_DAILY_CAP_USD:
        raise TruthScoreError(
            f"team daily cost cap reached (${spent:.2f} / "
            f"${TEAM_DAILY_CAP_USD:.2f}) — try again tomorrow",
            http_status=429,
        )


# ---------------------------------------------------------------- events


async def _emit(
    type_: str,
    project_id: str,
    payload: dict[str, Any],
) -> None:
    """Publish a TruthScore bus event. Failures are logged, not raised
    (an event hiccup must never block the run path)."""
    try:
        body = {
            "ts": _now_iso(),
            "agent_id": "truthscore",
            "project_id": project_id,
            "type": type_,
            **payload,
        }
        await bus.publish(body)
    except Exception:
        logger.exception("truthscore: event publish failed: %s", type_)


# ---------------------------------------------------------------- main entry


async def run_truth_score(
    project_id: str,
    commentary: str | None,
    actor: dict[str, Any],
) -> dict[str, Any]:
    """Execute one TruthScore run for `project_id`.

    Returns the §2.3 response shape on success. Raises
    `TruthScoreError(http_status=...)` on caller-fixable failures
    (no truth, no main, cap hit, lock held). Other exceptions
    propagate as 500s.
    """
    if not project_id:
        raise TruthScoreError("no active project")
    commentary = (commentary or "").strip() or None

    # Cost-cap pre-flight.
    await _check_cost_cap()

    lock = _lock_for(project_id)
    if lock.locked():
        raise TruthScoreError(
            "TruthScore is already running for this project",
            http_status=409,
        )

    async with lock:
        return await _run_locked(project_id, commentary, actor)


async def _run_locked(
    project_id: str,
    commentary: str | None,
    actor: dict[str, Any],
) -> dict[str, Any]:
    started_iso = _now_iso()
    await _emit(
        "truthscore_started",
        project_id,
        {
            "actor": actor,
            "commentary_present": bool(commentary),
            **({"to": _fanout_target(actor)} if _fanout_target(actor) else {}),
        },
    )

    raw_path_for_failure: str | None = None
    try:
        # Phase 1 — gather inputs. May raise TruthScoreError(400).
        truth_section, truth_meta = await _gather_truth_corpus(project_id)
        objectives_section, _objectives_meta = await _gather_objectives(project_id)
        main_section, main_meta = await _gather_main_tree(
            project_id, truth_section
        )
        subcorpora_section, sub_meta = await _gather_subcorpora(project_id)

        # Phase 2 — call LLM.
        system, user = _compose_prompt(
            truth_section,
            objectives_section,
            main_section,
            subcorpora_section,
            commentary,
        )
        try:
            result = await cmp_llm.call(
                system,
                user,
                project_id=project_id,
                label="truthscore:run",
            )
        except cmp_llm.CompassLLMError as e:
            raise TruthScoreError(
                f"LLM call failed: {type(e).__name__}: {e}",
                http_status=502,
            )

        if result.is_error:
            errs = "; ".join(result.errors[:3]) if result.errors else "(no detail)"
            raise TruthScoreError(
                f"LLM call returned error: {errs}",
                http_status=502,
            )

        # Phase 3 — parse.
        ts_filename = _now_filename_ts()
        parsed = _parse_llm_output(result.text or "")
        if parsed is None:
            raw_path_for_failure = await _write_raw_failure(
                ts_filename, result.text or "(empty)"
            )
            raise TruthScoreError(
                "LLM output failed to parse against the expected schema; "
                f"raw output written to {raw_path_for_failure or '(write failed)'}",
                http_status=502,
            )

        # Phase 4 — render + write.
        inputs_summary = {
            "truth_files": truth_meta.get("files", 0),
            "truth_bytes": truth_meta.get("bytes", 0),
            "truth_truncated": truth_meta.get("truncated", []),
            "truth_skipped": truth_meta.get("skipped", []),
            "main_sha": main_meta.get("main_sha"),
            "main_files_indexed": main_meta.get("files_indexed", 0),
            "main_bytes_sampled": main_meta.get("bytes_sampled", 0),
            "fetch_warning": main_meta.get("fetch_warning"),
            "decisions_files": (sub_meta.get("decisions") or {}).get("files", 0),
            "knowledge_files": (sub_meta.get("knowledge") or {}).get("files", 0),
            "outputs_files": (sub_meta.get("outputs") or {}).get("files", 0),
        }
        body = _render_result_file(
            overall=parsed["overall"],
            scores=parsed["scores"],
            comment=parsed["comment"],
            inputs=inputs_summary,
            commentary=commentary,
            actor=actor,
            created_at=started_iso,
        )
        result_path = await _write_result_file(ts_filename, body)

        # Phase 5 — emit completion + return response shape.
        comment_short = parsed["comment"].split(".")[0][:200].strip()
        if comment_short and not comment_short.endswith("."):
            comment_short = comment_short + "."

        completion_payload: dict[str, Any] = {
            "actor": actor,
            "overall": parsed["overall"],
            "scores": parsed["scores"],
            "comment_short": comment_short,
            "result_path": result_path,
            "main_sha": main_meta.get("main_sha"),
            "fetch_warning": main_meta.get("fetch_warning"),
        }
        target = _fanout_target(actor)
        if target:
            completion_payload["to"] = target
        await _emit("truthscore_completed", project_id, completion_payload)

        response: dict[str, Any] = {
            "ok": True,
            "result_path": result_path,
            "overall": parsed["overall"],
            "scores": parsed["scores"],
            "comment": parsed["comment"],
            "inputs": {
                "truth_files": inputs_summary["truth_files"],
                "truth_bytes": inputs_summary["truth_bytes"],
                "main_sha": inputs_summary["main_sha"],
                "main_files_indexed": inputs_summary["main_files_indexed"],
                "main_bytes_sampled": inputs_summary["main_bytes_sampled"],
                "decisions": inputs_summary["decisions_files"],
                "knowledge": inputs_summary["knowledge_files"],
                "outputs": inputs_summary["outputs_files"],
            },
        }
        if main_meta.get("fetch_warning"):
            response["fetch_warning"] = main_meta["fetch_warning"]
        return response

    except TruthScoreError as e:
        payload: dict[str, Any] = {
            "actor": actor,
            "reason": str(e),
            "http_status": e.http_status,
        }
        if raw_path_for_failure:
            payload["raw_path"] = raw_path_for_failure
        target = _fanout_target(actor)
        if target:
            payload["to"] = target
        await _emit("truthscore_failed", project_id, payload)
        raise
    except Exception as e:
        target = _fanout_target(actor)
        payload2: dict[str, Any] = {
            "actor": actor,
            "reason": f"{type(e).__name__}: {e}",
        }
        if target:
            payload2["to"] = target
        await _emit("truthscore_failed", project_id, payload2)
        raise


def _fanout_target(actor: dict[str, Any]) -> str | None:
    """Decide which slot's pane this run's events should fan out to.
    MCP calls land in the calling agent's pane (Coach if Coach,
    `pN` if Player). HTTP / slash → no fan-out (event still
    broadcasts to the global event log)."""
    if not isinstance(actor, dict):
        return None
    if actor.get("source") != "mcp-tool":
        return None
    aid = actor.get("agent_id")
    if isinstance(aid, str) and aid:
        return aid
    return None

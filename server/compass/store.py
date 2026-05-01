"""Compass storage layer — JSON + JSONL files with synchronous kDrive mirror.

Design choices:

- **JSON, not SQLite.** Per spec §6 the lattice / truth / regions /
  questions / proposals are JSON files; runs and audits are JSONL.
  This is deliberately readable-by-the-human on disk and on kDrive.
  The harness's hot path stays SQLite (events, turns); Compass state
  is a separate, smaller, slower-changing tier.
- **Atomic writes.** Every JSON file write goes via tempfile +
  `os.replace` so a crash mid-write can't corrupt the canonical file.
  Mirrors the pattern in `paths.py:update_wiki_index`.
- **Synchronous kDrive mirror.** Each write fans out to
  `webdav.write_text()` immediately (best-effort, log-and-continue).
  No batching — the mirror is for human-readability and disaster
  recovery, not throughput. Compass writes are infrequent (a daily
  run produces O(10) writes total).
- **Stable ids.** `s1`, `s2`, … (monotonic across active + archived).
  `q1`, `q2`, … same. `audit_<unix_ms>`. `r<unix_seconds>`. The id
  helpers compute next from current state — never persist a
  next-id counter that could drift from reality.
- **No locking.** Compass is single-writer per project: at most one
  run / one Q&A digest / one audit at a time. The runner serializes
  itself via the runner-level project lock; this module trusts that.

Public surface:

  - `bootstrap_state(project_id)` — first-run initialization. Idempotent.
  - `load_state(project_id) -> LatticeState` — read all state into one
    dataclass tree. Missing files become empty defaults.
  - `save_lattice(project_id, statements)` — atomic write + kDrive.
  - **Truth has no `save_*` here** — Compass reads truth from the
    project's `truth/` folder via `compass.truth.read_truth_facts`.
    Humans edit truth files via the Files pane; Coach proposes via
    `coord_propose_file_write(scope='truth', ...)`. Compass is a pure consumer.
  - `save_regions(project_id, regions, merge_history)` — same.
  - `save_questions(project_id, questions)` — same.
  - `save_proposals(project_id, settle, stale, dupes)` — write all
    three proposal files at once (atomic per-file; not cross-file).
  - `append_audit(project_id, record)` — append one JSON line to
    audits.jsonl + mirror entire file (jsonl mirror is full-rewrite;
    fine because audits are bounded).
  - `append_run_log(project_id, run)` — same shape for runs.jsonl.
  - `write_briefing(project_id, date_iso, content)` — markdown file.
  - `read_briefing(project_id, date_iso) -> str | None`.
  - `latest_briefing_text(project_id) -> str | None`.
  - `read_claude_md_block(project_id) -> str | None`.
  - `write_claude_md_block(project_id, content)` — copy of the
    last-rendered block, distinct from the actual CLAUDE.md injection.
  - `next_statement_id(state)`, `next_question_id(state)`,
    `next_audit_id()`, `next_run_id()` — id allocators.
  - `wipe_project(project_id)` — reset; deletes local + kDrive copies.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from server.webdav import webdav

from server.compass import config
from server.compass.paths import (
    CompassPaths,
    compass_paths,
    ensure_compass_scaffold,
    remote_path,
    remote_root,
)

logger = logging.getLogger("harness.compass.store")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------- types


@dataclass
class Statement:
    id: str
    text: str
    region: str
    weight: float
    created_at: str
    created_by: str = "compass"  # "compass" | "human"
    history: list[dict[str, Any]] = field(default_factory=list)
    archived: bool = False
    archived_at: str | None = None
    settled_as: str | None = None  # "yes" | "no" | "partial"
    settled_by_human: bool = False
    manually_set: bool = False
    merged: bool = False
    merged_from: list[str] = field(default_factory=list)
    reformulated: bool = False
    settle_proposed: bool = False
    stale_proposed: bool = False
    dupe_proposed: bool = False
    reconciliation_proposed: bool = False
    reconciliation_ambiguity: bool = False  # set when human chose "accept ambiguity" on a corpus↔lattice conflict
    kept_stale: bool = False


@dataclass
class TruthFact:
    index: int  # 1-based
    text: str
    added_at: str
    added_by: str = "human"  # spec §1.4 — only humans add truth


@dataclass
class Region:
    name: str
    created_at: str
    created_by: str = "compass"
    merged_into: str | None = None  # name of the survivor if this one was merged


@dataclass
class RegionMergeEvent:
    """One row of regions.json's merge_history list. Persisted as
    {"from": [...], "to": ..., "merged_at": ..., "run_id": ...}; we
    serialize the dataclass with `from_` → `from` to match spec §6.3.
    """

    from_: list[str]
    to: str
    merged_at: str
    run_id: str

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "from": list(self.from_),
            "to": self.to,
            "merged_at": self.merged_at,
            "run_id": self.run_id,
        }

    @classmethod
    def from_jsonable(cls, raw: dict[str, Any]) -> RegionMergeEvent:
        return cls(
            from_=list(raw.get("from") or []),
            to=str(raw.get("to") or ""),
            merged_at=str(raw.get("merged_at") or ""),
            run_id=str(raw.get("run_id") or ""),
        )


@dataclass
class Question:
    id: str
    q: str
    prediction: str
    targets: list[str]
    rationale: str
    asked_at: str
    asked_in_run: str
    answer: str | None = None
    answered_at: str | None = None
    digested: bool = False
    digested_in_run: str | None = None
    contradicted: bool = False
    ambiguity_accepted: bool = False
    from_audit: str | None = None


@dataclass
class AuditRecord:
    id: str
    ts: str
    artifact: str
    verdict: str  # "aligned" | "confident_drift" | "uncertain_drift"
    summary: str
    contradicting_ids: list[str] = field(default_factory=list)
    message_to_coach: str = ""
    question_id: str | None = None


@dataclass
class RunLog:
    run_id: str
    started_at: str
    mode: str  # "bootstrap" | "daily" | "on_demand"
    completed: bool = False
    finished_at: str | None = None
    passive: dict[str, Any] = field(default_factory=dict)
    answered_questions: int = 0
    contradictions: int = 0
    region_merges: list[dict[str, Any]] = field(default_factory=list)
    settle_proposed: int = 0
    stale_proposed: int = 0
    dupe_proposed: int = 0
    reconcile_proposed: int = 0
    questions_generated: int = 0
    truth_candidates: list[str] = field(default_factory=list)
    briefing_path: str | None = None
    notes: list[str] = field(default_factory=list)
    skipped: bool = False
    skipped_reason: str | None = None


@dataclass
class SettleProposal:
    statement_id: str
    direction: str  # "yes" | "no"
    question: str
    reasoning: str
    proposed_at: str
    proposed_in_run: str
    pending_runs: int = 0  # incremented each run the human hasn't resolved


@dataclass
class StaleProposal:
    statement_id: str
    question: str
    reasoning: str
    proposed_at: str
    proposed_in_run: str
    reformulation: str | None = None
    pending_runs: int = 0


@dataclass
class DuplicateProposal:
    id: str
    cluster_ids: list[str]
    merged_text: str
    merged_weight: float
    region: str
    reasoning: str
    proposed_at: str
    proposed_in_run: str
    pending_runs: int = 0


@dataclass
class ReconciliationProposal:
    """Spec §3.0.1 / §6.7 — a corpus↔lattice conflict awaiting the
    human's call. Created by `pipeline.reconciliation.detect_conflicts`
    when the truth corpus changes and an existing lattice row (active
    OR settled/archived) contradicts the new corpus content."""

    id: str  # "rec1", "rec2", … monotonic per project
    statement_id: str
    statement_archived: bool
    corpus_paths: list[str]  # truth file relpaths cited by the LLM
    explanation: str
    suggested_resolution: str = "either"  # "update_lattice" | "update_truth" | "either"
    proposed_at: str = ""
    proposed_in_run: str = ""
    pending_runs: int = 0


@dataclass
class LatticeState:
    project_id: str
    schema_version: str = config.COMPASS_SCHEMA_VERSION
    statements: list[Statement] = field(default_factory=list)
    truth: list[TruthFact] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    region_merge_history: list[RegionMergeEvent] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    settle_proposals: list[SettleProposal] = field(default_factory=list)
    stale_proposals: list[StaleProposal] = field(default_factory=list)
    duplicate_proposals: list[DuplicateProposal] = field(default_factory=list)
    reconciliation_proposals: list[ReconciliationProposal] = field(default_factory=list)
    # Project metadata (name + description from the `projects` table)
    # — anchors the LLM on what THIS project is, so signals mentioning
    # the harness's team / agent / model meta don't bleed into the
    # lattice. Populated by `read_project_meta` at run-start; prompts
    # read it via `_project_anchor`. Empty when uninitialized — the
    # prompts handle that gracefully.
    project_meta: dict[str, str] = field(default_factory=dict)

    # Convenience views

    def active_statements(self) -> list[Statement]:
        return [s for s in self.statements if not s.archived]

    def archived_statements(self) -> list[Statement]:
        return [s for s in self.statements if s.archived]

    def active_regions(self) -> list[Region]:
        return [r for r in self.regions if r.merged_into is None]

    def find_statement(self, sid: str) -> Statement | None:
        for s in self.statements:
            if s.id == sid:
                return s
        return None

    def find_question(self, qid: str) -> Question | None:
        for q in self.questions:
            if q.id == qid:
                return q
        return None


# ----------------------------------------------------------- now / atomic


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically. Tempfile + os.replace.
    Creates parent dirs as needed. Raises on write failure — callers
    decide whether to log-and-continue or propagate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _kdrive_mirror_text(remote_rel: str, content: str) -> None:
    """Best-effort kDrive mirror. Runs synchronously inside the caller's
    asyncio loop via the webdav client's sync helpers. Errors are
    logged but never raised — the local file is the source of truth.

    NOTE: this MUST be called from within an async function and
    awaited as needed; here it's a sync wrapper that schedules the
    upload via the existing async webdav client. Compass callers are
    async, so we expose the awaitable form `await
    _kdrive_mirror_text_async()` directly.
    """
    raise NotImplementedError(
        "use _kdrive_mirror_text_async (sync wrapper kept for symmetry)"
    )


async def _kdrive_mirror_text_async(remote_rel: str, content: str) -> bool:
    if not webdav.enabled:
        return False
    try:
        return await webdav.write_text(remote_rel, content)
    except Exception:
        logger.exception("compass kDrive mirror failed: %s", remote_rel)
        return False


async def _kdrive_remove_async(remote_rel: str) -> bool:
    """Best-effort kDrive delete. Used by `wipe_project`."""
    if not webdav.enabled:
        return False
    try:
        return await webdav.remove(remote_rel)
    except Exception:
        logger.exception("compass kDrive remove failed: %s", remote_rel)
        return False


def _dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


# -------------------------------------------------------- (de)serialize


def _statement_to_jsonable(s: Statement) -> dict[str, Any]:
    return asdict(s)


def _statement_from_jsonable(raw: dict[str, Any]) -> Statement:
    return Statement(
        id=str(raw.get("id") or ""),
        text=str(raw.get("text") or ""),
        region=str(raw.get("region") or ""),
        weight=float(raw.get("weight") if raw.get("weight") is not None else 0.5),
        created_at=str(raw.get("created_at") or ""),
        created_by=str(raw.get("created_by") or "compass"),
        history=list(raw.get("history") or []),
        archived=bool(raw.get("archived") or False),
        archived_at=raw.get("archived_at"),
        settled_as=raw.get("settled_as"),
        settled_by_human=bool(raw.get("settled_by_human") or False),
        manually_set=bool(raw.get("manually_set") or False),
        merged=bool(raw.get("merged") or False),
        merged_from=list(raw.get("merged_from") or []),
        reformulated=bool(raw.get("reformulated") or False),
        settle_proposed=bool(raw.get("settle_proposed") or False),
        stale_proposed=bool(raw.get("stale_proposed") or False),
        dupe_proposed=bool(raw.get("dupe_proposed") or False),
        reconciliation_proposed=bool(raw.get("reconciliation_proposed") or False),
        reconciliation_ambiguity=bool(raw.get("reconciliation_ambiguity") or False),
        kept_stale=bool(raw.get("kept_stale") or False),
    )


def _region_to_jsonable(r: Region) -> dict[str, Any]:
    return asdict(r)


def _region_from_jsonable(raw: dict[str, Any]) -> Region:
    return Region(
        name=str(raw.get("name") or ""),
        created_at=str(raw.get("created_at") or ""),
        created_by=str(raw.get("created_by") or "compass"),
        merged_into=raw.get("merged_into"),
    )


def _question_to_jsonable(q: Question) -> dict[str, Any]:
    return asdict(q)


def _question_from_jsonable(raw: dict[str, Any]) -> Question:
    return Question(
        id=str(raw.get("id") or ""),
        q=str(raw.get("q") or ""),
        prediction=str(raw.get("prediction") or ""),
        targets=list(raw.get("targets") or []),
        rationale=str(raw.get("rationale") or ""),
        asked_at=str(raw.get("asked_at") or ""),
        asked_in_run=str(raw.get("asked_in_run") or ""),
        answer=raw.get("answer"),
        answered_at=raw.get("answered_at"),
        digested=bool(raw.get("digested") or False),
        digested_in_run=raw.get("digested_in_run"),
        contradicted=bool(raw.get("contradicted") or False),
        ambiguity_accepted=bool(raw.get("ambiguity_accepted") or False),
        from_audit=raw.get("from_audit"),
    )


def _audit_to_jsonable(a: AuditRecord) -> dict[str, Any]:
    return asdict(a)


def _audit_from_jsonable(raw: dict[str, Any]) -> AuditRecord:
    return AuditRecord(
        id=str(raw.get("id") or ""),
        ts=str(raw.get("ts") or ""),
        artifact=str(raw.get("artifact") or ""),
        verdict=str(raw.get("verdict") or "aligned"),
        summary=str(raw.get("summary") or ""),
        contradicting_ids=list(raw.get("contradicting_ids") or []),
        message_to_coach=str(raw.get("message_to_coach") or ""),
        question_id=raw.get("question_id"),
    )


def _run_to_jsonable(r: RunLog) -> dict[str, Any]:
    return asdict(r)


def _run_from_jsonable(raw: dict[str, Any]) -> RunLog:
    return RunLog(
        run_id=str(raw.get("run_id") or ""),
        started_at=str(raw.get("started_at") or ""),
        mode=str(raw.get("mode") or "daily"),
        completed=bool(raw.get("completed") or False),
        finished_at=raw.get("finished_at"),
        passive=dict(raw.get("passive") or {}),
        answered_questions=int(raw.get("answered_questions") or 0),
        contradictions=int(raw.get("contradictions") or 0),
        region_merges=list(raw.get("region_merges") or []),
        settle_proposed=int(raw.get("settle_proposed") or 0),
        stale_proposed=int(raw.get("stale_proposed") or 0),
        dupe_proposed=int(raw.get("dupe_proposed") or 0),
        reconcile_proposed=int(raw.get("reconcile_proposed") or 0),
        questions_generated=int(raw.get("questions_generated") or 0),
        truth_candidates=list(raw.get("truth_candidates") or []),
        briefing_path=raw.get("briefing_path"),
        notes=list(raw.get("notes") or []),
        skipped=bool(raw.get("skipped") or False),
        skipped_reason=raw.get("skipped_reason"),
    )


def _settle_to_jsonable(p: SettleProposal) -> dict[str, Any]:
    return asdict(p)


def _settle_from_jsonable(raw: dict[str, Any]) -> SettleProposal:
    return SettleProposal(
        statement_id=str(raw.get("statement_id") or ""),
        direction=str(raw.get("direction") or "yes"),
        question=str(raw.get("question") or ""),
        reasoning=str(raw.get("reasoning") or ""),
        proposed_at=str(raw.get("proposed_at") or ""),
        proposed_in_run=str(raw.get("proposed_in_run") or ""),
        pending_runs=int(raw.get("pending_runs") or 0),
    )


def _stale_to_jsonable(p: StaleProposal) -> dict[str, Any]:
    return asdict(p)


def _stale_from_jsonable(raw: dict[str, Any]) -> StaleProposal:
    return StaleProposal(
        statement_id=str(raw.get("statement_id") or ""),
        question=str(raw.get("question") or ""),
        reasoning=str(raw.get("reasoning") or ""),
        proposed_at=str(raw.get("proposed_at") or ""),
        proposed_in_run=str(raw.get("proposed_in_run") or ""),
        reformulation=raw.get("reformulation"),
        pending_runs=int(raw.get("pending_runs") or 0),
    )


def _reconcile_to_jsonable(p: ReconciliationProposal) -> dict[str, Any]:
    return asdict(p)


def _reconcile_from_jsonable(raw: dict[str, Any]) -> ReconciliationProposal:
    return ReconciliationProposal(
        id=str(raw.get("id") or ""),
        statement_id=str(raw.get("statement_id") or ""),
        statement_archived=bool(raw.get("statement_archived") or False),
        corpus_paths=list(raw.get("corpus_paths") or []),
        explanation=str(raw.get("explanation") or ""),
        suggested_resolution=str(raw.get("suggested_resolution") or "either"),
        proposed_at=str(raw.get("proposed_at") or ""),
        proposed_in_run=str(raw.get("proposed_in_run") or ""),
        pending_runs=int(raw.get("pending_runs") or 0),
    )


def _dupe_to_jsonable(p: DuplicateProposal) -> dict[str, Any]:
    return asdict(p)


def _dupe_from_jsonable(raw: dict[str, Any]) -> DuplicateProposal:
    return DuplicateProposal(
        id=str(raw.get("id") or ""),
        cluster_ids=list(raw.get("cluster_ids") or []),
        merged_text=str(raw.get("merged_text") or ""),
        merged_weight=float(raw.get("merged_weight") if raw.get("merged_weight") is not None else 0.5),
        region=str(raw.get("region") or ""),
        reasoning=str(raw.get("reasoning") or ""),
        proposed_at=str(raw.get("proposed_at") or ""),
        proposed_in_run=str(raw.get("proposed_in_run") or ""),
        pending_runs=int(raw.get("pending_runs") or 0),
    )


# ---------------------------------------------------------- read helpers


def _read_json_or(default: Any, path: Path) -> Any:
    """Read a JSON file. Missing → default. Corrupt → log + default."""
    if not path.exists():
        return default
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("compass: read failed: %s", path)
        return default
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.exception("compass: JSON parse failed: %s", path)
        return default


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file. Missing → []. Bad lines are logged + skipped."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for ln, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    logger.warning("compass: skipping bad jsonl line %s:%s", path, ln)
    except OSError:
        logger.exception("compass: read jsonl failed: %s", path)
    return out


# ----------------------------------------------------------- bootstrap


async def bootstrap_state(project_id: str) -> CompassPaths:
    """Create the per-project directory tree and seed the canonical
    state files with empty `compass_schema_version: "0.2"` payloads.
    Idempotent — existing files are left alone.

    Truth is NOT seeded here — Compass reads truth from the project's
    `<project>/truth/` folder (managed by humans + Coach proposals
    elsewhere in the harness). See `server/compass/truth.py`.

    The bootstrap step is distinct from `runner.run(mode='bootstrap')`,
    which performs the actual question-generation pipeline. Calling
    this guarantees a well-formed empty state on disk before the runner
    touches anything.
    """
    cp = ensure_compass_scaffold(project_id)
    if not cp.lattice.exists():
        await save_lattice(project_id, [])
    if not cp.regions.exists():
        await save_regions(project_id, [], [])
    if not cp.questions.exists():
        await save_questions(project_id, [])
    if not cp.settle_proposals.exists():
        await save_proposals(project_id, settle=[], stale=None, dupes=None, reconcile=None)
    if not cp.stale_proposals.exists():
        await save_proposals(project_id, settle=None, stale=[], dupes=None, reconcile=None)
    if not cp.duplicate_proposals.exists():
        await save_proposals(project_id, settle=None, stale=None, dupes=[], reconcile=None)
    if not cp.reconciliation_proposals.exists():
        await save_proposals(project_id, settle=None, stale=None, dupes=None, reconcile=[])
    return cp


# ---------------------------------------------------------------- save


async def save_lattice(project_id: str, statements: list[Statement]) -> None:
    cp = ensure_compass_scaffold(project_id)
    payload = {
        "compass_schema_version": config.COMPASS_SCHEMA_VERSION,
        "statements": [_statement_to_jsonable(s) for s in statements],
    }
    text = _dump_json(payload)
    _atomic_write_text(cp.lattice, text)
    await _kdrive_mirror_text_async(remote_path(project_id, "lattice.json"), text)


async def save_regions(
    project_id: str,
    regions: list[Region],
    merge_history: list[RegionMergeEvent],
) -> None:
    cp = ensure_compass_scaffold(project_id)
    payload = {
        "compass_schema_version": config.COMPASS_SCHEMA_VERSION,
        "regions": [_region_to_jsonable(r) for r in regions],
        "merge_history": [m.to_jsonable() for m in merge_history],
    }
    text = _dump_json(payload)
    _atomic_write_text(cp.regions, text)
    await _kdrive_mirror_text_async(remote_path(project_id, "regions.json"), text)


async def save_questions(project_id: str, questions: list[Question]) -> None:
    cp = ensure_compass_scaffold(project_id)
    payload = {
        "compass_schema_version": config.COMPASS_SCHEMA_VERSION,
        "questions": [_question_to_jsonable(q) for q in questions],
    }
    text = _dump_json(payload)
    _atomic_write_text(cp.questions, text)
    await _kdrive_mirror_text_async(remote_path(project_id, "questions.json"), text)


async def save_proposals(
    project_id: str,
    *,
    settle: list[SettleProposal] | None,
    stale: list[StaleProposal] | None,
    dupes: list[DuplicateProposal] | None,
    reconcile: list[ReconciliationProposal] | None = None,
) -> None:
    """Save any subset of the four proposal lists. Pass `None` for a
    list to leave the corresponding file untouched. Pass `[]` to write
    an empty list (which clears prior pending proposals)."""
    cp = ensure_compass_scaffold(project_id)
    if settle is not None:
        payload = {
            "compass_schema_version": config.COMPASS_SCHEMA_VERSION,
            "proposals": [_settle_to_jsonable(p) for p in settle],
        }
        text = _dump_json(payload)
        _atomic_write_text(cp.settle_proposals, text)
        await _kdrive_mirror_text_async(
            remote_path(project_id, "proposals", "settle.json"), text
        )
    if stale is not None:
        payload = {
            "compass_schema_version": config.COMPASS_SCHEMA_VERSION,
            "proposals": [_stale_to_jsonable(p) for p in stale],
        }
        text = _dump_json(payload)
        _atomic_write_text(cp.stale_proposals, text)
        await _kdrive_mirror_text_async(
            remote_path(project_id, "proposals", "stale.json"), text
        )
    if dupes is not None:
        payload = {
            "compass_schema_version": config.COMPASS_SCHEMA_VERSION,
            "proposals": [_dupe_to_jsonable(p) for p in dupes],
        }
        text = _dump_json(payload)
        _atomic_write_text(cp.duplicate_proposals, text)
        await _kdrive_mirror_text_async(
            remote_path(project_id, "proposals", "duplicates.json"), text
        )
    if reconcile is not None:
        payload = {
            "compass_schema_version": config.COMPASS_SCHEMA_VERSION,
            "proposals": [_reconcile_to_jsonable(p) for p in reconcile],
        }
        text = _dump_json(payload)
        _atomic_write_text(cp.reconciliation_proposals, text)
        await _kdrive_mirror_text_async(
            remote_path(project_id, "proposals", "reconciliation.json"), text
        )


async def append_audit(project_id: str, record: AuditRecord) -> None:
    """Append one audit to audits.jsonl + mirror full file to kDrive.

    JSONL append is fine locally (no rewrite). For the kDrive mirror
    we re-read the whole file and re-upload — append-only WebDAV is
    rare, and audits are bounded (project-lifetime, single-digit
    thousands tops), so the cost is acceptable.
    """
    cp = ensure_compass_scaffold(project_id)
    line = json.dumps(_audit_to_jsonable(record), ensure_ascii=False) + "\n"
    cp.audits.parent.mkdir(parents=True, exist_ok=True)
    with cp.audits.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line)
    try:
        full = cp.audits.read_text(encoding="utf-8")
    except OSError:
        return
    await _kdrive_mirror_text_async(remote_path(project_id, "audits.jsonl"), full)


async def append_run_log(project_id: str, run: RunLog) -> None:
    cp = ensure_compass_scaffold(project_id)
    line = json.dumps(_run_to_jsonable(run), ensure_ascii=False) + "\n"
    cp.runs.parent.mkdir(parents=True, exist_ok=True)
    with cp.runs.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line)
    try:
        full = cp.runs.read_text(encoding="utf-8")
    except OSError:
        return
    await _kdrive_mirror_text_async(remote_path(project_id, "runs.jsonl"), full)


async def write_briefing(project_id: str, date_iso: str, content: str) -> Path:
    """Write a daily briefing markdown file. Returns the local path."""
    cp = ensure_compass_scaffold(project_id)
    cp.briefings_dir.mkdir(parents=True, exist_ok=True)
    target = cp.briefing_for(date_iso)
    _atomic_write_text(target, content)
    await _kdrive_mirror_text_async(
        remote_path(project_id, "briefings", target.name), content
    )
    return target


async def write_claude_md_block(project_id: str, content: str) -> None:
    """Persist a copy of the last-rendered CLAUDE.md block under
    `compass/claude_md_block.md`. The actual injection into the
    project's CLAUDE.md happens in `pipeline.claude_md.inject` and
    is a separate concern."""
    cp = ensure_compass_scaffold(project_id)
    _atomic_write_text(cp.claude_md_block, content)
    await _kdrive_mirror_text_async(
        remote_path(project_id, "claude_md_block.md"), content
    )


# ---------------------------------------------------------------- read


def load_state(project_id: str) -> LatticeState:
    """Load every state file into one `LatticeState`. Missing files
    become empty defaults; corrupt files log a warning and become
    empty defaults too — Compass continues even if part of the tree
    is unreadable, so a botched edit doesn't silently freeze the loop.
    """
    cp = compass_paths(project_id)

    lattice_raw = _read_json_or({}, cp.lattice)
    statements = [_statement_from_jsonable(s) for s in (lattice_raw.get("statements") or [])]

    # Truth is folder-backed: read fresh from `<project>/truth/` on
    # every call so an edit to a truth file is picked up immediately.
    # Lazy import dodges a tiny circular: `compass.truth` imports
    # `TruthFact` from this module.
    from server.compass.truth import read_truth_facts  # noqa: PLC0415
    truth = read_truth_facts(project_id)

    regions_raw = _read_json_or({}, cp.regions)
    regions = [_region_from_jsonable(r) for r in (regions_raw.get("regions") or [])]
    merge_history = [
        RegionMergeEvent.from_jsonable(m) for m in (regions_raw.get("merge_history") or [])
    ]

    questions_raw = _read_json_or({}, cp.questions)
    questions = [_question_from_jsonable(q) for q in (questions_raw.get("questions") or [])]

    settle_raw = _read_json_or({}, cp.settle_proposals)
    settle = [_settle_from_jsonable(p) for p in (settle_raw.get("proposals") or [])]

    stale_raw = _read_json_or({}, cp.stale_proposals)
    stale = [_stale_from_jsonable(p) for p in (stale_raw.get("proposals") or [])]

    dupes_raw = _read_json_or({}, cp.duplicate_proposals)
    dupes = [_dupe_from_jsonable(p) for p in (dupes_raw.get("proposals") or [])]

    reconcile_raw = _read_json_or({}, cp.reconciliation_proposals)
    reconciliations = [
        _reconcile_from_jsonable(p) for p in (reconcile_raw.get("proposals") or [])
    ]

    return LatticeState(
        project_id=project_id,
        schema_version=str(lattice_raw.get("compass_schema_version") or config.COMPASS_SCHEMA_VERSION),
        statements=statements,
        truth=truth,
        regions=regions,
        region_merge_history=merge_history,
        questions=questions,
        settle_proposals=settle,
        stale_proposals=stale,
        duplicate_proposals=dupes,
        reconciliation_proposals=reconciliations,
    )


def read_audits(project_id: str) -> list[AuditRecord]:
    cp = compass_paths(project_id)
    return [_audit_from_jsonable(r) for r in _read_jsonl(cp.audits)]


def read_run_log(project_id: str) -> list[RunLog]:
    cp = compass_paths(project_id)
    return [_run_from_jsonable(r) for r in _read_jsonl(cp.runs)]


def read_briefing(project_id: str, date_iso: str) -> str | None:
    cp = compass_paths(project_id)
    p = cp.briefing_for(date_iso)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def list_briefing_dates(project_id: str) -> list[str]:
    """Return ISO-date strings of every briefing on disk, newest first."""
    cp = compass_paths(project_id)
    if not cp.briefings_dir.exists():
        return []
    out: list[str] = []
    for p in cp.briefings_dir.glob("briefing-*.md"):
        stem = p.stem  # briefing-YYYY-MM-DD
        if stem.startswith("briefing-"):
            out.append(stem[len("briefing-") :])
    out.sort(reverse=True)
    return out


def latest_briefing_text(project_id: str) -> str | None:
    dates = list_briefing_dates(project_id)
    if not dates:
        return None
    return read_briefing(project_id, dates[0])


def read_claude_md_block(project_id: str) -> str | None:
    cp = compass_paths(project_id)
    if not cp.claude_md_block.exists():
        return None
    try:
        return cp.claude_md_block.read_text(encoding="utf-8")
    except OSError:
        return None


# ----------------------------------------------------------- id helpers


def _max_numeric_suffix(prefix: str, ids: Iterable[str]) -> int:
    plen = len(prefix)
    high = 0
    for sid in ids:
        if not sid or not sid.startswith(prefix):
            continue
        rest = sid[plen:]
        if not rest.isdigit():
            continue
        n = int(rest)
        if n > high:
            high = n
    return high


def next_statement_id(state: LatticeState) -> str:
    """Monotonic across active + archived. Two ids never collide even
    after archive, merge, or reformulation."""
    n = _max_numeric_suffix("s", (s.id for s in state.statements))
    return f"s{n + 1}"


def next_question_id(state: LatticeState) -> str:
    n = _max_numeric_suffix("q", (q.id for q in state.questions))
    return f"q{n + 1}"


def next_audit_id() -> str:
    """`audit_<unix_ms>`. ms granularity sorts well; overlap within a
    single ms is essentially impossible at this rate."""
    return f"audit_{int(time.time() * 1000)}"


def next_run_id() -> str:
    """`r<unix_seconds>`. Seconds granularity is fine — runs are at
    most a few times per day."""
    return f"r{int(time.time())}"


def next_dupe_proposal_id(state: LatticeState) -> str:
    n = _max_numeric_suffix("dupe", (p.id for p in state.duplicate_proposals))
    return f"dupe{n + 1}"


async def load_with_meta(project_id: str) -> LatticeState:
    """`load_state(project_id)` + populate `project_meta` from the
    `projects` table. Use this wherever the loaded state will feed
    an LLM prompt — bare `load_state` is fine for read-only rendering
    paths (the dashboard's /state snapshot, /runs, /audits, etc.).

    Single round-trip per call site. Failure to read meta downgrades
    to empty dict (the prompts handle that gracefully) rather than
    blocking the whole load."""
    state = load_state(project_id)
    state.project_meta = await read_project_meta(project_id)
    return state


async def read_project_meta(project_id: str) -> dict[str, str]:
    """Look up the project's name + description + repo_url from the
    `projects` table. Used by the runner to populate
    `LatticeState.project_meta` before any LLM call, so prompts can
    anchor on what THIS project actually is and ignore harness meta
    (player slot assignments, model overrides, recurrence config,
    etc.) that would otherwise pollute the lattice.

    Note: `project-objectives.md` is NOT read here. It's part of the
    truth corpus (read by `compass.truth.read_truth_facts` alongside
    `<project>/truth/*.md`), not a steering layer. The human's
    authored objectives are vetted source-of-truth-like documents
    and Compass treats them with the same authority as the truth/
    folder — they drive truth-derive (lattice seeding) and
    truth-check (contradiction detection).

    Returns `{"id": project_id}` on DB error or missing row — prompts
    `_project_anchor` only renders an anchor when at least one of
    name/description is set, so missing project rows degrade to
    "no anchor" rather than confusing the LLM.
    """
    from server.db import configured_conn  # noqa: PLC0415 — lazy

    out: dict[str, str] = {"id": project_id}

    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT name, description, repo_url FROM projects WHERE id = ?",
                (project_id,),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("compass: read_project_meta query failed: %s", project_id)
        return out

    if row:
        d = dict(row)
        if d.get("name"):
            out["name"] = str(d["name"]).strip()
        if d.get("description"):
            out["description"] = str(d["description"]).strip()
        if d.get("repo_url"):
            out["repo_url"] = str(d["repo_url"]).strip()

    return out


def next_reconciliation_id(state: LatticeState) -> str:
    """Monotonic per project, scoped to current pending list. Spec §6.7
    uses `recN`; we follow that convention so ids are short + readable
    in the dashboard."""
    n = _max_numeric_suffix("rec", (p.id for p in state.reconciliation_proposals))
    return f"rec{n + 1}"


# --------------------------------------------------------------- wipe


async def wipe_project(project_id: str) -> None:
    """Destructive: delete the project's local Compass tree AND the
    kDrive mirror. Used by `POST /api/compass/reset`. Cannot be undone.

    The local rmtree is best-effort; missing dir is fine. The kDrive
    side iterates known files and removes them; the directory itself
    stays (some WebDAV servers don't support DELETE on collections).
    """
    cp = compass_paths(project_id)
    if cp.root.exists():
        shutil.rmtree(cp.root, ignore_errors=True)

    # Best-effort kDrive cleanup: walk known files first, then the
    # whole subtree. We don't depend on remote delete-of-collection
    # support; per-file removes are universal.
    if webdav.enabled:
        # Note: we don't remove truth files — they're owned by the
        # project's `truth/` lane, not by Compass. A reset wipes
        # Compass's view (lattice, regions, audits, runs, briefings,
        # proposals, claude_md_block) but leaves truth in place.
        names = [
            "lattice.json",
            "regions.json",
            "questions.json",
            "audits.jsonl",
            "runs.jsonl",
            "claude_md_block.md",
            "proposals/settle.json",
            "proposals/stale.json",
            "proposals/duplicates.json",
            "proposals/reconciliation.json",
        ]
        for name in names:
            await _kdrive_remove_async(remote_path(project_id, name))
        # Briefings — discover via list_dir.
        try:
            briefings = await webdav.list_dir(remote_path(project_id, "briefings"))
        except Exception:
            briefings = []
        for fn in briefings:
            await _kdrive_remove_async(remote_path(project_id, "briefings", fn))


__all__ = [
    "Statement",
    "TruthFact",
    "Region",
    "RegionMergeEvent",
    "Question",
    "AuditRecord",
    "RunLog",
    "SettleProposal",
    "StaleProposal",
    "DuplicateProposal",
    "ReconciliationProposal",
    "LatticeState",
    "bootstrap_state",
    "save_lattice",
    "save_regions",
    "save_questions",
    "save_proposals",
    "append_audit",
    "append_run_log",
    "write_briefing",
    "write_claude_md_block",
    "load_state",
    "read_audits",
    "read_run_log",
    "read_briefing",
    "read_claude_md_block",
    "list_briefing_dates",
    "latest_briefing_text",
    "next_statement_id",
    "next_question_id",
    "next_audit_id",
    "next_run_id",
    "next_dupe_proposal_id",
    "next_reconciliation_id",
    "read_project_meta",
    "load_with_meta",
    "wipe_project",
]

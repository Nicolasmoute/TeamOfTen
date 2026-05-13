"""Playbook store — JSON I/O for lattice, archived, and runs.jsonl.

Three on-disk files (spec §2):
  /data/playbook/lattice.json   active statements
  /data/playbook/archived.json  settled / stale_low / stale_unused / merged
                                / superseded / deleted
  /data/playbook/runs.jsonl     one line per reflection / bootstrap run

Atomic writes via tempfile + os.replace. Synchronous cloud-drive
mirror attempted after every successful local write (spec §5.9 + §N2):
local-first, no rollback on cloud-drive failure, idempotent re-sync on
the next write. Cloud-drive write failure publishes
`playbook_kdrive_mirror_failed` event (event name kept for wire
compat) for the dashboard banner.

Reads are tolerant: missing file → empty schema; corrupt file → raise
(implementer must investigate; corruption shouldn't be silently
healed).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.playbook import config
from server.playbook.paths import (
    PlaybookPaths,
    ensure_playbook_dir,
    playbook_paths,
    remote_path,
)

logger = logging.getLogger("harness.playbook.store")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------- types


@dataclass
class WeightHistoryEntry:
    """Single weight transition. `from` is None on the bootstrap entry."""

    ts: str
    to: float
    reason: str
    from_: float | None = None  # serialized as "from" — Python keyword

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "from": self.from_,
            "to": self.to,
            "reason": self.reason,
        }

    @classmethod
    def from_jsonable(cls, raw: dict[str, Any]) -> "WeightHistoryEntry":
        return cls(
            ts=str(raw.get("ts") or ""),
            to=float(raw.get("to") or 0.0),
            reason=str(raw.get("reason") or ""),
            from_=raw.get("from") if raw.get("from") is None else float(raw["from"]),
        )


@dataclass
class Statement:
    """One playbook statement (spec §3.1)."""

    id: str
    text: str
    weight: float
    weight_history: list[WeightHistoryEntry] = field(default_factory=list)
    created_at: str = ""
    created_by: str = ""
    last_validated_at: str | None = None
    applied_count: int = 0
    immutable: bool = False

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "weight": self.weight,
            "weight_history": [h.to_jsonable() for h in self.weight_history],
            "created_at": self.created_at,
            "created_by": self.created_by,
            "last_validated_at": self.last_validated_at,
            "applied_count": self.applied_count,
            "immutable": self.immutable,
        }

    @classmethod
    def from_jsonable(cls, raw: dict[str, Any]) -> "Statement":
        return cls(
            id=str(raw.get("id") or ""),
            text=str(raw.get("text") or ""),
            weight=float(raw.get("weight") or 0.0),
            weight_history=[
                WeightHistoryEntry.from_jsonable(h)
                for h in (raw.get("weight_history") or [])
                if isinstance(h, dict)
            ],
            created_at=str(raw.get("created_at") or ""),
            created_by=str(raw.get("created_by") or ""),
            last_validated_at=raw.get("last_validated_at"),
            applied_count=int(raw.get("applied_count") or 0),
            immutable=bool(raw.get("immutable") or False),
        )


@dataclass
class ArchivedStatement:
    """One archived statement (spec §3.2). Carries final weight + reason
    + optional merge target. `history` preserves the full weight_history
    at archive time for forensics — bypasses the active 50-entry cap."""

    id: str
    text: str
    final_weight: float
    archived_at: str
    archive_reason: str  # "settled" | "stale_low" | "stale_unused" | "merged" | "superseded" | "deleted"
    merged_into: str | None = None
    history: list[WeightHistoryEntry] = field(default_factory=list)
    created_at: str = ""
    created_by: str = ""

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "final_weight": self.final_weight,
            "archived_at": self.archived_at,
            "archive_reason": self.archive_reason,
            "merged_into": self.merged_into,
            "history": [h.to_jsonable() for h in self.history],
            "created_at": self.created_at,
            "created_by": self.created_by,
        }

    @classmethod
    def from_jsonable(cls, raw: dict[str, Any]) -> "ArchivedStatement":
        return cls(
            id=str(raw.get("id") or ""),
            text=str(raw.get("text") or ""),
            final_weight=float(raw.get("final_weight") or 0.0),
            archived_at=str(raw.get("archived_at") or ""),
            archive_reason=str(raw.get("archive_reason") or "deleted"),
            merged_into=raw.get("merged_into"),
            history=[
                WeightHistoryEntry.from_jsonable(h)
                for h in (raw.get("history") or [])
                if isinstance(h, dict)
            ],
            created_at=str(raw.get("created_at") or ""),
            created_by=str(raw.get("created_by") or ""),
        )


@dataclass
class Lattice:
    schema_version: int
    updated_at: str
    statements: list[Statement] = field(default_factory=list)


@dataclass
class Archive:
    schema_version: int
    statements: list[ArchivedStatement] = field(default_factory=list)


# ---------------------------------------------------------------- helpers


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write `content` to `path` atomically. Tempfile + os.replace.

    Caller is responsible for ensuring the parent directory exists
    (use `ensure_playbook_dir()` upstream).
    """
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        # Best-effort temp cleanup. We re-raise so the caller knows
        # the write failed; the cloud-drive mirror won't run.
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _atomic_append_jsonl(path: Path, line_obj: dict[str, Any]) -> None:
    """Append a single JSON line to a `.jsonl` file. Not strictly atomic
    (we don't rewrite the whole file), but the open-append-close cycle
    is short enough that a crash mid-write only corrupts a single line.

    The reader side tolerates corrupt lines by skipping them — see
    `read_runs()`.
    """
    line = json.dumps(line_obj, ensure_ascii=False)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


async def _kdrive_mirror_text(remote_rel: str, content: str, *, file_label: str) -> None:
    """Mirror to the configured cloud drive. Local write must have
    already succeeded.

    On failure: log + emit `playbook_kdrive_mirror_failed` event with
    `error` + `files: [<file_label>]` (spec §5.9 + §N2). DO NOT re-raise
    — local is the source of truth; the cloud drive is durability
    mirror only. (Event name + function name kept for back-compat; the
    mirror works against any WebDAV-compatible drive.)
    """
    from server.webdav import webdav

    if not webdav.enabled:
        return
    try:
        ok = await webdav.write_text(remote_rel, content)
        if not ok:
            await _emit_kdrive_failure(
                f"webdav.write_text returned False for {remote_rel}",
                [file_label],
            )
    except Exception as exc:
        await _emit_kdrive_failure(f"{type(exc).__name__}: {str(exc)[:300]}", [file_label])


async def _emit_kdrive_failure(error: str, files: list[str]) -> None:
    try:
        from server.events import bus  # noqa: PLC0415

        await bus.publish({
            "ts": _now_iso(),
            "type": "playbook_kdrive_mirror_failed",
            "error": error,
            "files": files,
        })
    except Exception:
        logger.exception("playbook.store: cloud-drive failure event publish raised")


# ---------------------------------------------------------------- lattice


def _empty_lattice() -> Lattice:
    return Lattice(
        schema_version=config.PLAYBOOK_SCHEMA_VERSION,
        updated_at=_now_iso(),
        statements=[],
    )


def _empty_archive() -> Archive:
    return Archive(
        schema_version=config.PLAYBOOK_SCHEMA_VERSION,
        statements=[],
    )


def load_lattice(*, paths: PlaybookPaths | None = None) -> Lattice:
    """Read `lattice.json`. Missing file → empty lattice (per spec §4.5
    step 2). Corrupt or unknown-schema → raise — silent healing of
    corruption hides bugs.

    Keep this sync — render.py + scheduler tick call it cheaply.
    """
    pp = paths or playbook_paths()
    if not pp.lattice.exists():
        return _empty_lattice()
    raw = pp.lattice.read_text(encoding="utf-8")
    obj = json.loads(raw)
    schema = obj.get("schema_version")
    if schema != config.PLAYBOOK_SCHEMA_VERSION:
        raise RuntimeError(
            f"playbook lattice.json schema_version={schema!r} "
            f"unsupported (expected {config.PLAYBOOK_SCHEMA_VERSION})"
        )
    return Lattice(
        schema_version=int(schema),
        updated_at=str(obj.get("updated_at") or _now_iso()),
        statements=[
            Statement.from_jsonable(s)
            for s in (obj.get("statements") or [])
            if isinstance(s, dict)
        ],
    )


def load_archive(*, paths: PlaybookPaths | None = None) -> Archive:
    """Read `archived.json`. Missing → empty archive."""
    pp = paths or playbook_paths()
    if not pp.archived.exists():
        return _empty_archive()
    raw = pp.archived.read_text(encoding="utf-8")
    obj = json.loads(raw)
    schema = obj.get("schema_version")
    if schema != config.PLAYBOOK_SCHEMA_VERSION:
        raise RuntimeError(
            f"playbook archived.json schema_version={schema!r} "
            f"unsupported (expected {config.PLAYBOOK_SCHEMA_VERSION})"
        )
    return Archive(
        schema_version=int(schema),
        statements=[
            ArchivedStatement.from_jsonable(s)
            for s in (obj.get("statements") or [])
            if isinstance(s, dict)
        ],
    )


def _trim_weight_history(stmt: Statement, *, cap: int = 50) -> None:
    """Keep last `cap` entries in `weight_history` (spec §3.1). The
    `runs.jsonl` is the durable audit trail."""
    if len(stmt.weight_history) > cap:
        stmt.weight_history = stmt.weight_history[-cap:]


async def save_lattice(lattice: Lattice, *, paths: PlaybookPaths | None = None) -> None:
    """Write `lattice.json` atomically + mirror to the cloud drive (best-effort).

    Caller is expected to have already mutated `lattice.statements`
    via `mutate.py` primitives.
    """
    pp = paths or ensure_playbook_dir()
    lattice.updated_at = _now_iso()
    lattice.schema_version = config.PLAYBOOK_SCHEMA_VERSION

    # Trim weight_history before write so the cap holds across restarts.
    for stmt in lattice.statements:
        _trim_weight_history(stmt)

    payload = {
        "schema_version": lattice.schema_version,
        "updated_at": lattice.updated_at,
        "statements": [s.to_jsonable() for s in lattice.statements],
    }
    content = _dump_json(payload)
    _atomic_write_text(pp.lattice, content)

    # Cloud-drive mirror (best-effort, fire-and-publish-on-failure).
    await _kdrive_mirror_text(
        remote_path("lattice.json"),
        content,
        file_label="lattice.json",
    )


async def save_archive(archive: Archive, *, paths: PlaybookPaths | None = None) -> None:
    """Write `archived.json` atomically + mirror to the cloud drive."""
    pp = paths or ensure_playbook_dir()
    archive.schema_version = config.PLAYBOOK_SCHEMA_VERSION
    payload = {
        "schema_version": archive.schema_version,
        "statements": [s.to_jsonable() for s in archive.statements],
    }
    content = _dump_json(payload)
    _atomic_write_text(pp.archived, content)
    await _kdrive_mirror_text(
        remote_path("archived.json"),
        content,
        file_label="archived.json",
    )


# ---------------------------------------------------------------- runs


def read_runs(
    *,
    limit: int | None = None,
    paths: PlaybookPaths | None = None,
) -> list[dict[str, Any]]:
    """Read `runs.jsonl`. Tolerates corrupt lines (skips). Returns most
    recent first when `limit` is given (otherwise oldest-first as
    appended)."""
    pp = paths or playbook_paths()
    if not pp.runs.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(pp.runs, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except json.JSONDecodeError:
                logger.warning("playbook.store: skipped corrupt runs.jsonl line")
                continue
    if limit is None:
        return rows
    return rows[-limit:][::-1]


async def append_run(row: dict[str, Any], *, paths: PlaybookPaths | None = None) -> None:
    """Append one run row to `runs.jsonl` + mirror to the cloud drive.

    Trims to `RUNS_RETENTION_DEFAULT` lines on each write — older rows
    are dropped (the dashboard surfaces only recent runs anyway, and
    the durable history is on the cloud drive's snapshot mirror).
    """
    pp = paths or ensure_playbook_dir()

    # Append in-place first so the mirror sees the trimmed file.
    _atomic_append_jsonl(pp.runs, row)

    # Trim if the file grew past retention. Read all, slice, rewrite atomically.
    rows = read_runs(paths=pp)  # oldest-first
    if len(rows) > config.RUNS_RETENTION_DEFAULT:
        kept = rows[-config.RUNS_RETENTION_DEFAULT:]
        content = "\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n"
        _atomic_write_text(pp.runs, content)
    else:
        # No trim needed — read the file content for the mirror.
        content = pp.runs.read_text(encoding="utf-8")

    await _kdrive_mirror_text(
        remote_path("runs.jsonl"),
        content,
        file_label="runs.jsonl",
    )


# ---------------------------------------------------------------- reset


def wipe_files(*, paths: PlaybookPaths | None = None) -> None:
    """Wipe local lattice/archived/runs files in place (spec §12).

    Files are not deleted — replaced with empty schemas so file watchers
    keep working and downstream readers don't hit 'file gone' errors.
    Called from the API reset endpoint AFTER `_run_lock` is held.
    """
    pp = paths or ensure_playbook_dir()
    _atomic_write_text(pp.lattice, _dump_json({
        "schema_version": config.PLAYBOOK_SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "statements": [],
    }))
    _atomic_write_text(pp.archived, _dump_json({
        "schema_version": config.PLAYBOOK_SCHEMA_VERSION,
        "statements": [],
    }))
    _atomic_write_text(pp.runs, "")


__all__ = [
    "WeightHistoryEntry",
    "Statement",
    "ArchivedStatement",
    "Lattice",
    "Archive",
    "load_lattice",
    "load_archive",
    "save_lattice",
    "save_archive",
    "read_runs",
    "append_run",
    "wipe_files",
]

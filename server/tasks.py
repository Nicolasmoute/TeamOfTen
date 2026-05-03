"""Task lifecycle helpers — spec.md + audit-report .md writers.

The kanban lifecycle (Docs/kanban-specs.md) produces durable markdown
artifacts under each task's folder:

    /data/projects/<project_id>/working/tasks/<task_id>/
        spec.md                                         # Coach's plan
        audits/
            audit_<round>_<kind>.md                     # Player auditor reports

These are mirrored to kDrive at the same relative path under
`projects/<project_id>/tasks/...` so the human can browse them on
their phone.

Functions in this module are shared by the MCP-tool path
(`coord_write_task_spec`, `coord_submit_audit_report`) and the
HTTP endpoint path (`POST /api/tasks/{id}/spec`). They handle:
  - atomic local write (tempfile + os.replace)
  - kDrive mirror (best-effort; failure is logged and swallowed)
  - the `tasks.spec_path` / `tasks.spec_written_at` / role-assignment
    `report_path` updates are NOT done here — they're caller
    responsibility (the MCP tool / HTTP handler does the SQL).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from server.paths import project_paths
from server.webdav import webdav

logger = logging.getLogger("harness.tasks")


# Format expected by the legacy task-id generator in tools._new_task_id:
#   t-YYYY-MM-DD-<8-hex>
# Anchored to prevent path-traversal exploits via task_id (a Coach who
# composed t-../../etc/passwd would otherwise let `task_dir` escape the
# project folder). Defensive regardless of caller — the MCP tool /
# HTTP handler should already be validating, but path resolution is
# the last line of defense.
_TASK_ID_RE = re.compile(r"^t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}$")


def is_valid_task_id(task_id: str) -> bool:
    """Reject task ids that don't match the canonical shape so they
    can't be used to escape the project's working/tasks/ folder."""
    return bool(_TASK_ID_RE.fullmatch(task_id or ""))


def task_dir(project_id: str, task_id: str) -> Path:
    """Per-task folder under the project. One folder per task — leaves
    room for sibling files (notes, screenshots, attachments) without
    restructuring later."""
    if not is_valid_task_id(task_id):
        raise ValueError(f"invalid task_id format: {task_id!r}")
    return project_paths(project_id).working / "tasks" / task_id


def spec_path(project_id: str, task_id: str) -> Path:
    """Absolute path to the task's spec.md. Existence not guaranteed —
    a task in `plan` stage may have no spec yet."""
    return task_dir(project_id, task_id) / "spec.md"


def audits_dir(project_id: str, task_id: str) -> Path:
    """Per-task audits subfolder. One file per (round, kind)."""
    return task_dir(project_id, task_id) / "audits"


def audit_report_filename(round_num: int, kind: str) -> str:
    """`audit_<round>_<kind>.md`. Round counts up from 1; kind is
    `'syntax'` or `'semantics'`."""
    if kind not in ("syntax", "semantics"):
        raise ValueError(f"invalid audit kind: {kind!r}")
    if round_num < 1:
        raise ValueError(f"round must be >= 1, got {round_num}")
    return f"audit_{round_num}_{kind}.md"


def audit_report_path(project_id: str, task_id: str, round_num: int, kind: str) -> Path:
    return audits_dir(project_id, task_id) / audit_report_filename(round_num, kind)


def spec_relative_path(project_id: str, task_id: str) -> str:
    """The path stored in `tasks.spec_path` — relative to /data/, with
    forward slashes (Windows-safe). The Files-pane link mechanism keys
    on this format."""
    return f"projects/{project_id}/working/tasks/{task_id}/spec.md"


def audit_report_relative_path(
    project_id: str, task_id: str, round_num: int, kind: str
) -> str:
    """The path stored in `tasks.latest_audit_report_path` and
    `task_role_assignments.report_path`."""
    fname = audit_report_filename(round_num, kind)
    return f"projects/{project_id}/working/tasks/{task_id}/audits/{fname}"


def kdrive_spec_path(project_id: str, task_id: str) -> str:
    """The path used for the kDrive mirror. Same shape as decisions /
    knowledge — `projects/<id>/...` under the kDrive root."""
    return f"projects/{project_id}/tasks/{task_id}/spec.md"


def kdrive_audit_path(
    project_id: str, task_id: str, round_num: int, kind: str
) -> str:
    fname = audit_report_filename(round_num, kind)
    return f"projects/{project_id}/tasks/{task_id}/audits/{fname}"


def _atomic_write(target: Path, content: str) -> None:
    """Write a file atomically: write to a sibling tempfile, then
    os.replace. Avoids leaving a half-written file if the process
    crashes mid-write — the MCP tool's caller would otherwise see a
    truncated spec or audit report."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _spec_frontmatter(
    *,
    task_id: str,
    title: str,
    created_by: str,
    created_at: str,
    priority: str,
    complexity: str,
    author: str,
    written_at: str,
) -> str:
    """YAML frontmatter for spec.md. Same shape as decisions so the
    Files-pane preview renders consistently."""
    return (
        f"---\n"
        f"task_id: {task_id}\n"
        f"title: {title}\n"
        f"created_by: {created_by}\n"
        f"created_at: {created_at}\n"
        f"priority: {priority}\n"
        f"complexity: {complexity}\n"
        f"spec_author: {author}\n"
        f"spec_written_at: {written_at}\n"
        f"---\n\n"
    )


def _audit_frontmatter(
    *,
    task_id: str,
    kind: str,
    round_num: int,
    auditor: str,
    verdict: str,
    submitted_at: str,
) -> str:
    return (
        f"---\n"
        f"task_id: {task_id}\n"
        f"audit_kind: {kind}\n"
        f"audit_round: {round_num}\n"
        f"auditor: {auditor}\n"
        f"verdict: {verdict}\n"
        f"submitted_at: {submitted_at}\n"
        f"---\n\n"
    )


async def write_task_spec(
    *,
    project_id: str,
    task_id: str,
    title: str,
    body: str,
    author: str,
    created_by: str,
    created_at: str,
    priority: str,
    complexity: str,
) -> tuple[Path, str, str]:
    """Write the task's spec.md atomically + mirror to kDrive.

    Returns `(local_path, relative_path, written_at_iso)`. The caller
    is responsible for updating `tasks.spec_path` /
    `tasks.spec_written_at` / any role-assignment row.

    `body` is the body text only — the YAML frontmatter is added here
    so every spec has a consistent header. If the body already contains
    a `## Goal` heading it's preserved verbatim; the helper does NOT
    enforce a structure (Coach decides the level of detail).
    """
    if not is_valid_task_id(task_id):
        raise ValueError(f"invalid task_id format: {task_id!r}")
    written_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    frontmatter = _spec_frontmatter(
        task_id=task_id,
        title=title,
        created_by=created_by,
        created_at=created_at,
        priority=priority,
        complexity=complexity,
        author=author,
        written_at=written_at,
    )
    body_clean = body.rstrip() + "\n"
    content = frontmatter + body_clean

    target = spec_path(project_id, task_id)
    _atomic_write(target, content)
    rel = spec_relative_path(project_id, task_id)

    # Best-effort kDrive mirror. WebDAV failures don't block the spec
    # write — the local copy is the source of truth.
    if webdav.enabled:
        asyncio.create_task(
            webdav.write_text(kdrive_spec_path(project_id, task_id), content)
        )
    return target, rel, written_at


async def write_audit_report(
    *,
    project_id: str,
    task_id: str,
    kind: str,
    round_num: int,
    body: str,
    auditor: str,
    verdict: str,
) -> tuple[Path, str, str]:
    """Write a Player auditor's report to
    `<task_dir>/audits/audit_<round>_<kind>.md` + kDrive mirror.

    Returns `(local_path, relative_path, submitted_at_iso)`. Caller
    updates the role-assignment row and the `tasks.latest_audit_*`
    columns.
    """
    if kind not in ("syntax", "semantics"):
        raise ValueError(f"invalid audit kind: {kind!r}")
    if verdict not in ("pass", "fail"):
        raise ValueError(f"invalid verdict: {verdict!r}")
    if round_num < 1:
        raise ValueError(f"round must be >= 1, got {round_num}")
    submitted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    frontmatter = _audit_frontmatter(
        task_id=task_id,
        kind=kind,
        round_num=round_num,
        auditor=auditor,
        verdict=verdict,
        submitted_at=submitted_at,
    )
    body_clean = body.rstrip() + "\n"
    content = frontmatter + body_clean

    target = audit_report_path(project_id, task_id, round_num, kind)
    _atomic_write(target, content)
    rel = audit_report_relative_path(project_id, task_id, round_num, kind)

    if webdav.enabled:
        asyncio.create_task(
            webdav.write_text(
                kdrive_audit_path(project_id, task_id, round_num, kind), content
            )
        )
    return target, rel, submitted_at


def read_task_spec(project_id: str, task_id: str) -> str | None:
    """Best-effort spec read. Returns None if the file doesn't exist
    or can't be read; logs the failure (callers should NOT propagate
    a read failure as a tool error)."""
    try:
        path = spec_path(project_id, task_id)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("read_task_spec failed for %s/%s", project_id, task_id)
        return None


__all__ = [
    "is_valid_task_id",
    "task_dir",
    "spec_path",
    "spec_relative_path",
    "audits_dir",
    "audit_report_filename",
    "audit_report_path",
    "audit_report_relative_path",
    "kdrive_spec_path",
    "kdrive_audit_path",
    "write_task_spec",
    "write_audit_report",
    "read_task_spec",
]

"""Coach todos — finite, strikeable backlog injected into Coach's
system prompt every turn (`recurrence-specs.md` §3.1).

File format is a GFM task list at
``/data/projects/<slug>/coach-todos.md``::

    # Coach todos — <project name>

    - [ ] **<title>** <!-- id:t-1 due:2026-05-01 -->
      <description, free markdown, can span multiple lines>

    - [ ] **<another title>** <!-- id:t-2 -->
      ...

Each entry's ``id`` lives in an HTML comment so it survives roundtrips
through the file without polluting rendered markdown. ``due`` is
optional, ISO date or ``YYYY-MM-DDTHH:MMZ``.

Completed entries move to ``working/coach-todos-archive.md`` (same
shape but ``- [x]`` and a ``completed:`` stamp). The archive is NOT
injected into the system prompt — reference only.

Single-write-handle discipline: the harness server is the only writer
to these files. Coach goes through ``coord_add_todo`` /
``coord_complete_todo`` / ``coord_update_todo`` MCP tools (see
``server/tools.py``); humans go through ``PUT /api/projects/{slug}/
coach-todos`` (phase 7). Both call into this module.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.paths import project_paths
from server.webdav import webdav

logger = logging.getLogger("harness.coach_todos")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s | %(message)s"
    ))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Sentinel for "field omitted" vs "field set to None" — None has a
# real meaning here (clear the due date), so we can't use it for both.
_UNSET: Any = object()


_DUE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DUE_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$")
_ID_RE = re.compile(r"\bid:(t-\d+)\b")
_DUE_RE = re.compile(r"\bdue:(\S+)")
_COMPLETED_RE = re.compile(r"\bcompleted:(\S+)")
_BULLET_RE = re.compile(
    r"^- \[(?P<done>[ xX])\] \*\*(?P<title>.+?)\*\*"
    r"(?:\s*<!--\s*(?P<meta>.+?)\s*-->)?\s*$"
)


@dataclass
class CoachTodo:
    id: str
    title: str
    description: str = ""
    due: str | None = None
    completed: str | None = None  # only set on archive entries
    done: bool = False

    def to_bullet(self) -> str:
        check = "x" if self.done else " "
        meta_parts = [f"id:{self.id}"]
        if self.due:
            meta_parts.append(f"due:{self.due}")
        if self.completed:
            meta_parts.append(f"completed:{self.completed}")
        meta = " ".join(meta_parts)
        line = f"- [{check}] **{self.title}** <!-- {meta} -->"
        if self.description.strip():
            indented = "\n".join(
                "  " + l for l in self.description.splitlines()
            )
            line += "\n" + indented
        return line


def _validate_due(due: str | None) -> str | None:
    if due is None:
        return None
    s = due.strip()
    if not s:
        return None
    if _DUE_DATE_RE.match(s) or _DUE_DATETIME_RE.match(s):
        return s
    raise ValueError(
        f"due must be YYYY-MM-DD or YYYY-MM-DDTHH:MMZ; got {due!r}"
    )


def _parse_meta(meta: str) -> dict[str, str]:
    """Parse the ``id:t-1 due:2026-05-01 completed:...`` metadata
    string from inside an HTML comment. Unknown keys are kept so a
    hand-edit can drop annotations without losing them on roundtrip.
    """
    out: dict[str, str] = {}
    for tok in meta.split():
        if ":" in tok:
            k, v = tok.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _next_id(existing: list[CoachTodo]) -> str:
    """Pick the next ``t-N`` id, monotonically increasing across the
    file. Doesn't reuse ids of completed (archived) entries — IDs are
    permanent within a project so cross-references in the event log
    stay meaningful.

    The caller passes in ALL entries (open + archive) so the counter
    sees the full history.
    """
    n = 0
    for t in existing:
        m = re.match(r"^t-(\d+)$", t.id)
        if m:
            n = max(n, int(m.group(1)))
    return f"t-{n + 1}"


def parse(text: str) -> list[CoachTodo]:
    """Parse a coach-todos.md or coach-todos-archive.md file into a
    list of :class:`CoachTodo`. Tolerates an empty file or one with
    only a header. Discards non-bullet lines that aren't part of a
    bullet's continuation block."""
    todos: list[CoachTodo] = []
    if not text.strip():
        return todos
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _BULLET_RE.match(lines[i])
        if not m:
            i += 1
            continue
        meta = _parse_meta(m.group("meta") or "")
        tid = meta.get("id", "")
        if not re.match(r"^t-\d+$", tid):
            # Bullets without an id are skipped — phase 3 only writes
            # via the MCP tools, which always stamp an id. Leftovers
            # from a hand-edit get logged once and dropped.
            logger.warning(
                "coach todo bullet without id, skipping: %r",
                lines[i][:80],
            )
            i += 1
            continue
        done = m.group("done").lower() == "x"
        # Slurp continuation lines (2-space indented, until next
        # bullet or blank-followed-by-bullet).
        desc_lines: list[str] = []
        j = i + 1
        while j < len(lines):
            line = lines[j]
            if not line.strip():
                # Blank line: peek ahead — if the next non-blank is
                # another bullet, end this entry.
                k = j + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k >= len(lines) or _BULLET_RE.match(lines[k]):
                    break
                desc_lines.append("")
                j += 1
                continue
            if line.startswith("  "):
                desc_lines.append(line[2:])
                j += 1
                continue
            break
        description = "\n".join(desc_lines).strip("\n")
        todos.append(CoachTodo(
            id=tid,
            title=m.group("title").strip(),
            description=description,
            due=meta.get("due") or None,
            completed=meta.get("completed") or None,
            done=done,
        ))
        i = j
    return todos


def serialize_open(project_name: str, todos: list[CoachTodo]) -> str:
    """Render an open-todos file. Empty `todos` still emits the header
    so a hand-editor sees a consistent skeleton."""
    header = f"# Coach todos — {project_name}\n"
    if not todos:
        return header
    body = "\n\n".join(t.to_bullet() for t in todos)
    return header + "\n" + body + "\n"


def serialize_archive(
    project_name: str, todos: list[CoachTodo]
) -> str:
    header = f"# Coach todos archive — {project_name}\n"
    if not todos:
        return header
    body = "\n\n".join(t.to_bullet() for t in todos)
    return header + "\n" + body + "\n"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


async def _write_with_mirror(
    local_path: Path, content: str, kdrive_rel: str,
) -> None:
    """Write `content` to `local_path` and synchronously mirror to
    kDrive when enabled. Mirrors the pattern in
    `coord_update_memory` / `coord_write_decision`. The kDrive write
    is best-effort — local is the source of truth."""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(content, encoding="utf-8")
    if webdav.enabled:
        try:
            await webdav.write_text(kdrive_rel, content)
        except Exception:
            logger.exception(
                "coach todos: kDrive mirror failed for %s", kdrive_rel,
            )


def _project_name(project_id: str) -> str:
    """Best-effort human name. Falls back to the slug when DB lookup
    isn't easy from this synchronous-ish context."""
    return project_id


def load_open(project_id: str) -> list[CoachTodo]:
    pp = project_paths(project_id)
    return [t for t in parse(_read_text(pp.coach_todos)) if not t.done]


def load_archive(project_id: str) -> list[CoachTodo]:
    pp = project_paths(project_id)
    return parse(_read_text(pp.coach_todos_archive))


def _all_known(project_id: str) -> list[CoachTodo]:
    """Open + archive entries; used so :func:`_next_id` doesn't reuse
    archived ids."""
    return load_open(project_id) + load_archive(project_id)


async def add_todo(
    project_id: str,
    *,
    title: str,
    description: str = "",
    due: str | None = None,
) -> CoachTodo:
    title = title.strip()
    if not title:
        raise ValueError("title is required")
    due_norm = _validate_due(due)
    pp = project_paths(project_id)
    open_todos = parse(_read_text(pp.coach_todos))
    full_history = open_todos + load_archive(project_id)
    todo = CoachTodo(
        id=_next_id(full_history),
        title=title,
        description=description.strip(),
        due=due_norm,
        done=False,
    )
    open_todos.append(todo)
    text = serialize_open(_project_name(project_id), open_todos)
    await _write_with_mirror(
        pp.coach_todos, text,
        f"projects/{project_id}/coach-todos.md",
    )
    return todo


async def complete_todo(project_id: str, todo_id: str) -> CoachTodo:
    """Move a todo from open to archive, stamping ``completed:<utc>``.

    Order of operations is **additive-first** so a crash between the
    two writes leaves a recoverable duplicate, never lost data:

      1. Read open + archive.
      2. Append target to archive and write it (target now persisted).
      3. Remove target from open and write it.

    If step 3 crashes, the next ``coord_complete_todo`` call sees the
    same id in both files. ``parse()`` is forgiving (drops repeats
    silently because ids are checked at the bullet level), and the
    duplicate is visible in the system-prompt injection until the
    operator runs the same complete again.
    """
    pp = project_paths(project_id)
    open_todos = parse(_read_text(pp.coach_todos))
    target: CoachTodo | None = None
    remaining: list[CoachTodo] = []
    for t in open_todos:
        if t.id == todo_id:
            target = t
        else:
            remaining.append(t)
    if target is None:
        raise KeyError(f"todo {todo_id!r} not found in open list")
    target.done = True
    target.completed = _utc_now_iso()
    archive = load_archive(project_id)
    archive.append(target)
    archive_text = serialize_archive(_project_name(project_id), archive)
    await _write_with_mirror(
        pp.coach_todos_archive, archive_text,
        f"projects/{project_id}/working/coach-todos-archive.md",
    )
    open_text = serialize_open(_project_name(project_id), remaining)
    await _write_with_mirror(
        pp.coach_todos, open_text,
        f"projects/{project_id}/coach-todos.md",
    )
    return target


async def update_todo(
    project_id: str,
    todo_id: str,
    *,
    title: Any = _UNSET,
    description: Any = _UNSET,
    due: Any = _UNSET,
) -> CoachTodo:
    """Patch fields. Pass ``_UNSET`` (default) to leave a field
    untouched. ``None`` is meaningful for ``due`` — it clears the
    deadline. ``title=""`` raises ValueError because an empty title
    is never useful.
    """
    pp = project_paths(project_id)
    open_todos = parse(_read_text(pp.coach_todos))
    target: CoachTodo | None = None
    for t in open_todos:
        if t.id == todo_id:
            target = t
            break
    if target is None:
        raise KeyError(f"todo {todo_id!r} not found in open list")
    if title is not _UNSET:
        if title is None:
            raise ValueError("title cannot be cleared")
        title_stripped = str(title).strip()
        if not title_stripped:
            raise ValueError("title cannot be empty")
        target.title = title_stripped
    if description is not _UNSET:
        target.description = (
            "" if description is None else str(description).strip()
        )
    if due is not _UNSET:
        target.due = _validate_due(due) if due is not None else None
    text = serialize_open(_project_name(project_id), open_todos)
    await _write_with_mirror(
        pp.coach_todos, text,
        f"projects/{project_id}/coach-todos.md",
    )
    return target


def open_todos_block(project_id: str) -> str:
    """Render the ``## Open coach todos`` section for Coach's system
    prompt. Returns "" when the file doesn't exist or has no open
    entries — the caller drops the section header in that case
    (per spec §6: "If either file is missing or empty, the
    corresponding section is omitted")."""
    pp = project_paths(project_id)
    text = _read_text(pp.coach_todos)
    if not text.strip():
        return ""
    if not parse(text):
        return ""
    return "## Open coach todos\n\n" + text.strip()

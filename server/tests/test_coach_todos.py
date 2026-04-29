"""Tests for the coach todos surface (recurrence-specs.md §3.1, §7).

Covers:
  * Parser round-trips a representative file (id, due, description,
    completed, multiline, blank-line-between-bullets).
  * Stable id allocation (monotonic, no reuse across archive).
  * add_todo / complete_todo / update_todo file-level effects:
    - file is created on first add
    - completed entries move to archive (not deleted)
    - updates round-trip
  * open_todos_block returns the right shape for system-prompt
    injection (omitted when empty).
  * MCP tool wiring rejects Player callers and accepts Coach.
"""

from __future__ import annotations

from typing import Any

import pytest

import server.coach_todos as todos
from server.db import init_db
from server.paths import ensure_project_scaffold, project_paths


# --- Parser ----------------------------------------------------------


def test_parse_empty() -> None:
    assert todos.parse("") == []
    assert todos.parse("# header only\n") == []


def test_parse_single_bullet() -> None:
    text = "# todos\n\n- [ ] **Do thing** <!-- id:t-1 -->\n"
    out = todos.parse(text)
    assert len(out) == 1
    t = out[0]
    assert t.id == "t-1"
    assert t.title == "Do thing"
    assert t.done is False
    assert t.due is None


def test_parse_with_due_and_description() -> None:
    text = (
        "# todos\n\n"
        "- [ ] **Plan launch** <!-- id:t-2 due:2026-05-01 -->\n"
        "  Stakeholder meeting on Tuesday.\n"
        "  Two sub-bullets to consider:\n"
        "  - reach\n"
        "  - timing\n"
    )
    out = todos.parse(text)
    assert len(out) == 1
    t = out[0]
    assert t.id == "t-2"
    assert t.due == "2026-05-01"
    assert "Stakeholder" in t.description
    assert "- reach" in t.description


def test_parse_multiple_with_blank_lines() -> None:
    text = (
        "# todos\n\n"
        "- [ ] **A** <!-- id:t-1 -->\n"
        "  desc-A\n\n"
        "- [x] **B** <!-- id:t-2 completed:2026-04-29T01:00Z -->\n"
        "  desc-B\n"
    )
    out = todos.parse(text)
    assert [t.id for t in out] == ["t-1", "t-2"]
    assert out[0].done is False
    assert out[1].done is True
    assert out[1].completed == "2026-04-29T01:00Z"


def test_parse_drops_bullet_without_id() -> None:
    text = (
        "- [ ] **Lone bullet, no id**\n"
        "- [ ] **Has id** <!-- id:t-1 -->\n"
    )
    out = todos.parse(text)
    assert [t.id for t in out] == ["t-1"]


# --- Round-trip (serialize → parse) ----------------------------------


def test_round_trip_preserves_fields() -> None:
    src = [
        todos.CoachTodo(
            id="t-1", title="A", description="line1\nline2",
            due="2026-05-01",
        ),
        todos.CoachTodo(
            id="t-2", title="B", done=True,
            completed="2026-04-29T01:00Z",
        ),
    ]
    text = todos.serialize_open("misc", src)
    out = todos.parse(text)
    assert [t.id for t in out] == ["t-1", "t-2"]
    assert out[0].description == "line1\nline2"
    assert out[0].due == "2026-05-01"
    assert out[1].done is True
    assert out[1].completed == "2026-04-29T01:00Z"


# --- ID allocation ---------------------------------------------------


def test_next_id_monotonic_across_archive() -> None:
    open_t = [todos.CoachTodo(id="t-3", title="x")]
    archive = [
        todos.CoachTodo(id="t-1", title="old", done=True),
        todos.CoachTodo(id="t-2", title="older", done=True),
    ]
    assert todos._next_id(open_t + archive) == "t-4"


def test_next_id_starts_at_one() -> None:
    assert todos._next_id([]) == "t-1"


# --- File-level CRUD -------------------------------------------------


async def test_add_creates_file(fresh_db: str) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    pp = project_paths("misc")
    assert not pp.coach_todos.exists()

    todo = await todos.add_todo(
        "misc", title="First task", description="Do it.",
    )
    assert todo.id == "t-1"
    assert pp.coach_todos.exists()
    text = pp.coach_todos.read_text(encoding="utf-8")
    assert "First task" in text
    assert "id:t-1" in text


async def test_add_increments_id(fresh_db: str) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    a = await todos.add_todo("misc", title="A")
    b = await todos.add_todo("misc", title="B")
    assert a.id == "t-1"
    assert b.id == "t-2"


async def test_complete_moves_to_archive(fresh_db: str) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    pp = project_paths("misc")
    a = await todos.add_todo("misc", title="A")
    await todos.add_todo("misc", title="B")

    completed = await todos.complete_todo("misc", a.id)
    assert completed.done is True
    assert completed.completed is not None

    open_text = pp.coach_todos.read_text(encoding="utf-8")
    assert "id:t-1" not in open_text  # moved out
    assert "id:t-2" in open_text  # B still open

    archive_text = pp.coach_todos_archive.read_text(encoding="utf-8")
    assert "id:t-1" in archive_text
    assert "completed:" in archive_text


async def test_complete_writes_archive_before_pruning_open(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §7.2 says complete_todo is atomic; the implementation
    writes archive first so a crash between the two writes leaves
    a recoverable duplicate, not data loss."""
    await init_db()
    ensure_project_scaffold("misc")
    a = await todos.add_todo("misc", title="A")
    pp = project_paths("misc")

    write_calls: list[str] = []
    real_write = todos._write_with_mirror

    async def trace_write(path, content, kdrive_rel):
        write_calls.append(str(path))
        if path == pp.coach_todos:
            raise RuntimeError("simulated open-write failure")
        await real_write(path, content, kdrive_rel)

    monkeypatch.setattr(todos, "_write_with_mirror", trace_write)
    with pytest.raises(RuntimeError):
        await todos.complete_todo("misc", a.id)
    # Archive must have landed first.
    assert len(write_calls) >= 2
    assert write_calls[0] == str(pp.coach_todos_archive)
    assert write_calls[1] == str(pp.coach_todos)
    # Archive contains the entry; open still has it (recoverable).
    assert pp.coach_todos_archive.exists()
    archive_text = pp.coach_todos_archive.read_text(encoding="utf-8")
    assert "id:t-1" in archive_text
    open_text = pp.coach_todos.read_text(encoding="utf-8")
    assert "id:t-1" in open_text


async def test_complete_unknown_raises(fresh_db: str) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    with pytest.raises(KeyError):
        await todos.complete_todo("misc", "t-99")


async def test_update_changes_fields(fresh_db: str) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    a = await todos.add_todo(
        "misc", title="A", description="orig", due="2026-05-01",
    )
    out = await todos.update_todo(
        "misc", a.id, title="A v2", description="new desc",
        due="2026-06-01",
    )
    assert out.title == "A v2"
    assert out.description == "new desc"
    assert out.due == "2026-06-01"


async def test_update_due_can_be_cleared(fresh_db: str) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    a = await todos.add_todo("misc", title="A", due="2026-05-01")
    out = await todos.update_todo("misc", a.id, due=None)
    # Note: the helper distinguishes "field omitted" from
    # "field passed as None". Passing None → assigns None → clears.
    # In the public API tests below we cover this through the MCP
    # surface where empty string semantics are explicit.
    assert out.due is None


def test_validate_due_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        todos._validate_due("tomorrow")
    with pytest.raises(ValueError):
        todos._validate_due("2026/05/01")


def test_validate_due_accepts_iso() -> None:
    assert todos._validate_due("2026-05-01") == "2026-05-01"
    assert todos._validate_due("2026-05-01T14:00Z") == "2026-05-01T14:00Z"
    assert todos._validate_due(None) is None
    assert todos._validate_due("  ") is None


# --- System-prompt block --------------------------------------------


async def test_open_todos_block_empty_returns_empty(
    fresh_db: str,
) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    assert todos.open_todos_block("misc") == ""


async def test_open_todos_block_renders_section(
    fresh_db: str,
) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    await todos.add_todo("misc", title="A")
    block = todos.open_todos_block("misc")
    assert block.startswith("## Open coach todos")
    assert "**A**" in block


# --- MCP tools (via build_coord_server) ------------------------------


async def test_mcp_add_todo_player_denied(fresh_db: str) -> None:
    """Phase 3 tools are Coach-only. A Player calling them must
    receive an isError result."""
    await init_db()
    ensure_project_scaffold("misc")
    from server.tools import build_coord_server
    srv = build_coord_server("p1", include_proxy_metadata=True)
    handler = srv["_handlers"]["coord_add_todo"]
    out = await handler({"title": "x"})
    assert out.get("isError") is True


async def test_mcp_add_then_complete_round_trip(fresh_db: str) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    from server.tools import build_coord_server
    srv = build_coord_server("coach", include_proxy_metadata=True)
    add = srv["_handlers"]["coord_add_todo"]
    complete = srv["_handlers"]["coord_complete_todo"]

    out = await add({"title": "first", "description": "go"})
    text = out["content"][0]["text"]
    assert "t-1" in text

    out = await complete({"id": "t-1"})
    assert "t-1 completed" in out["content"][0]["text"]


async def test_mcp_update_todo_clears_due(fresh_db: str) -> None:
    await init_db()
    ensure_project_scaffold("misc")
    from server.tools import build_coord_server
    srv = build_coord_server("coach", include_proxy_metadata=True)
    add = srv["_handlers"]["coord_add_todo"]
    update = srv["_handlers"]["coord_update_todo"]
    await add({"title": "x", "due": "2026-05-01"})
    out = await update({"id": "t-1", "due": ""})
    assert "updated" in out["content"][0]["text"]
    open_t = todos.load_open("misc")
    assert open_t[0].due is None

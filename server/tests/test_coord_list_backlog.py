"""Tests for coord_list_backlog MCP tool.

Covers:
- Happy path: pending-only filter (default).
- All-status filter returns promoted + rejected entries too.
- Empty backlog returns a sentinel message.
- Invalid status param returns an error.
- Entries are sorted by priority/FIFO and mark the next eligible item.
- Rejected entries include reject_reason in output.
- Promoted entries include the promoted task id in output.
- limit param caps and validates correctly.
- Tool is accessible to both Coach and Players (no role gate).
- Full title (>80 chars) is never truncated in output.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.db import configured_conn, init_db
from server.tools import build_coord_server


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok_text(result: dict[str, Any]) -> str:
    assert not result.get("is_error"), f"tool returned error: {result}"
    return result["content"][0]["text"]


def _err_text(result: dict[str, Any]) -> str:
    assert result.get("is_error"), f"expected error, got: {result}"
    return result["content"][0]["text"]


async def _seed_backlog(
    *,
    title: str = "demo entry",
    proposed_by: str = "p1",
    status: str = "pending",
    reject_reason: str | None = None,
    promoted_task_id: str | None = None,
    proposed_at: str = "2026-05-14T10:00:00.000Z",
) -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO backlog_tasks "
            "(title, proposed_by, proposed_at, status, reject_reason, promoted_task_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, proposed_by, proposed_at, status, reject_reason, promoted_task_id),
        )
        await c.commit()
        return int(cur.lastrowid)
    finally:
        await c.close()


# -----------------------------------------------------------------------
# Happy path — pending (default)
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_backlog_pending_default(fresh_db: str) -> None:
    """Default status='pending' returns only pending entries."""
    await init_db()
    await _seed_backlog(title="pending entry", status="pending")
    await _seed_backlog(title="rejected entry", status="rejected",
                        reject_reason="out of scope")

    server = _server_for("coach")
    result = await _handler(server, "list_backlog")({})
    text = _ok_text(result)

    assert "pending entry" in text
    assert "rejected entry" not in text


@pytest.mark.asyncio
async def test_list_backlog_explicit_pending(fresh_db: str) -> None:
    """Explicit status='pending' behaves the same as the default."""
    await init_db()
    await _seed_backlog(title="my idea", status="pending")

    server = _server_for("p3")
    result = await _handler(server, "list_backlog")({"status": "pending"})
    text = _ok_text(result)
    assert "my idea" in text


# -----------------------------------------------------------------------
# All-status filter
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_backlog_all_returns_every_status(fresh_db: str) -> None:
    """status='all' returns entries across all statuses."""
    await init_db()
    await _seed_backlog(title="still pending", status="pending")
    # promoted entry without a task FK (NULL is allowed).
    await _seed_backlog(title="was promoted", status="promoted")
    await _seed_backlog(title="was rejected", status="rejected",
                        reject_reason="not now")

    server = _server_for("coach")
    result = await _handler(server, "list_backlog")({"status": "all"})
    text = _ok_text(result)

    assert "still pending" in text
    assert "was promoted" in text
    assert "was rejected" in text


# -----------------------------------------------------------------------
# Empty backlog
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_backlog_empty(fresh_db: str) -> None:
    """Empty backlog returns a clear sentinel, not an error."""
    await init_db()

    server = _server_for("coach")
    result = await _handler(server, "list_backlog")({})
    text = _ok_text(result)
    assert "empty" in text.lower()


# -----------------------------------------------------------------------
# Status param validation
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_backlog_invalid_status(fresh_db: str) -> None:
    """Unknown status value returns an error."""
    await init_db()

    server = _server_for("coach")
    result = await _handler(server, "list_backlog")({"status": "triaged"})
    text = _err_text(result)
    assert "status" in text.lower()


# -----------------------------------------------------------------------
# Sort order — priority/FIFO
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_backlog_sorted_priority_fifo(fresh_db: str) -> None:
    """Pending entries are returned by priority, FIFO within priority."""
    await init_db()
    await _seed_backlog(title="older normal", proposed_at="2026-05-10T08:00:00.000Z")
    await _seed_backlog(title="newer urgent", proposed_at="2026-05-14T12:00:00.000Z")
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE backlog_tasks SET priority = 'urgent' WHERE title = 'newer urgent'"
        )
        await c.commit()
    finally:
        await c.close()

    server = _server_for("coach")
    result = await _handler(server, "list_backlog")({"status": "pending"})
    text = _ok_text(result)

    urgent_pos = text.index("newer urgent")
    normal_pos = text.index("older normal")
    assert urgent_pos < normal_pos, "urgent entry should appear before older normal"
    assert "[next]" in text


# -----------------------------------------------------------------------
# Rejected entries include reject_reason
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_backlog_rejected_shows_reason(fresh_db: str) -> None:
    """Rejected entries show their reject_reason in the output."""
    await init_db()
    await _seed_backlog(
        title="bad idea",
        status="rejected",
        reject_reason="out of scope for v1",
    )

    server = _server_for("coach")
    result = await _handler(server, "list_backlog")({"status": "rejected"})
    text = _ok_text(result)
    assert "out of scope for v1" in text


# -----------------------------------------------------------------------
# Promoted entries include promoted_task_id
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_backlog_promoted_shows_task_id(fresh_db: str) -> None:
    """Promoted entries show the promoted task id in the output."""
    await init_db()
    # Must create the task first to satisfy the FK constraint.
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) VALUES (?, 'misc', 'the task', "
            "'execute', 'p2', 'coach', '[]')",
            ("t-2026-05-14-abcdef12",),
        )
        await c.commit()
    finally:
        await c.close()

    await _seed_backlog(
        title="good idea",
        status="promoted",
        promoted_task_id="t-2026-05-14-abcdef12",
    )

    server = _server_for("coach")
    result = await _handler(server, "list_backlog")({"status": "promoted"})
    text = _ok_text(result)
    assert "t-2026-05-14-abcdef12" in text


# -----------------------------------------------------------------------
# limit param
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_backlog_limit_caps_results(fresh_db: str) -> None:
    """limit=2 returns at most 2 rows even when more exist."""
    await init_db()
    for i in range(5):
        await _seed_backlog(
            title=f"entry {i}",
            proposed_at=f"2026-05-14T{10 + i:02d}:00:00.000Z",
        )

    server = _server_for("coach")
    result = await _handler(server, "list_backlog")({"limit": "2"})
    text = _ok_text(result)
    # Each entry is on its own line starting with '#'
    entry_lines = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert len(entry_lines) == 2


@pytest.mark.asyncio
async def test_list_backlog_invalid_limit(fresh_db: str) -> None:
    """Non-integer limit returns an error."""
    await init_db()

    server = _server_for("coach")
    result = await _handler(server, "list_backlog")({"limit": "banana"})
    text = _err_text(result)
    assert "limit" in text.lower()


# -----------------------------------------------------------------------
# Role-access: Players can call it too
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_backlog_accessible_to_players(fresh_db: str) -> None:
    """coord_list_backlog is available to Players, not Coach-only."""
    await init_db()
    await _seed_backlog(title="player sees this", status="pending")

    server = _server_for("p5")
    result = await _handler(server, "list_backlog")({"status": "pending"})
    text = _ok_text(result)
    assert "player sees this" in text


# -----------------------------------------------------------------------
# Long title — no truncation
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_backlog_long_title_not_truncated(fresh_db: str) -> None:
    """Titles longer than 80 chars must appear in full (no truncation)."""
    long_title = "B" * 90  # 90 chars — well above the old 80-char cap
    await init_db()
    await _seed_backlog(title=long_title, status="pending")

    server = _server_for("coach")
    result = await _handler(server, "list_backlog")({})
    text = _ok_text(result)
    assert long_title in text, (
        f"Expected full 90-char title in output; got:\n{text}"
    )

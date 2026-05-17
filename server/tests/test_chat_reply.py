"""Regression guard for the chat reply feature (Docs/truth-index.md §16.3).

The reply button is a pure UI affordance — no new HTTP endpoints, no new
DB columns. These tests verify the existing GET /api/messages endpoint
still returns the expected payload shape, and that the POST /api/messages
endpoint still accepts the existing body format (so the reply flow, which
submits via the existing POST, continues to work).

Intentionally thin: the quote-format helper (buildReplyQuote in app.js)
is a ~5-line pure JS function; without a JS test runner we verify it
visually. This file exists to pin the server-side shape that reply
depends on.
"""
from __future__ import annotations

import pytest

from server.db import configured_conn, init_db


# ---------------------------------------------------------------- helpers


async def _ensure_project(pid: str = "misc") -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)",
            (pid, pid),
        )
        rows = await (
            await c.execute(
                "SELECT COUNT(*) FROM team_config WHERE key = 'active_project'"
            )
        ).fetchone()
        if rows[0] == 0:
            await c.execute(
                "INSERT INTO team_config (key, value) VALUES ('active_project', ?)",
                (pid,),
            )
        else:
            await c.execute(
                "UPDATE team_config SET value = ? WHERE key = 'active_project'",
                (pid,),
            )
        await c.commit()
    finally:
        await c.close()


async def _insert_message(
    from_id: str = "coach",
    to_id: str = "p1",
    subject: str = "hello",
    body: str = "some body text",
    project_id: str = "misc",
) -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            """INSERT INTO messages (project_id, from_id, to_id, subject, body, priority, sent_at)
               VALUES (?, ?, ?, ?, ?, 'normal', datetime('now'))""",
            (project_id, from_id, to_id, subject, body),
        )
        await c.commit()
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        await c.close()


# ---------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_messages_schema_has_expected_columns(fresh_db: str) -> None:  # noqa: ARG001
    """messages table exists with the columns the reply flow depends on."""
    await init_db()
    c = await configured_conn()
    try:
        rows = await (
            await c.execute("PRAGMA table_info(messages)")
        ).fetchall()
    finally:
        await c.close()

    col_names = {r[1] for r in rows}
    # Columns the reply button reads from GET /api/messages:
    for col in ("from_id", "to_id", "subject", "body", "priority", "sent_at"):
        assert col in col_names, f"Missing column: {col}"


@pytest.mark.asyncio
async def test_get_messages_returns_list_shape(fresh_db: str) -> None:  # noqa: ARG001
    """GET /api/messages response includes 'messages' key with expected fields.
    The EnvInboxSection and the reply button depend on this shape."""
    import httpx
    from server.main import app

    await init_db()
    await _ensure_project()
    await _insert_message(
        from_id="coach",
        to_id="p1",
        subject="test subj",
        body="hello from coach",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/api/messages?limit=10")

    assert r.status_code == 200
    data = r.json()
    assert "messages" in data
    assert isinstance(data["messages"], list)
    assert len(data["messages"]) >= 1

    # Each message row must carry the fields the reply button reads.
    msg = data["messages"][0]
    for field in ("id", "from_id", "to_id", "subject", "body", "priority", "sent_at"):
        assert field in msg, f"Missing field: {field}"


@pytest.mark.asyncio
async def test_post_messages_accepts_reply_shaped_body(fresh_db: str) -> None:  # noqa: ARG001
    """POST /api/messages with a reply-shaped payload succeeds.
    The reply button in EnvPane Inbox submits via this same endpoint."""
    import httpx
    from server.main import app

    await init_db()
    await _ensure_project()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/messages",
            json={
                "to": "coach",
                "subject": "Re: test (from p1): first 80 chars…",
                "body": "> Re: test (from p1): first 80 chars…\n\nmy reply text",
                "priority": "normal",
            },
        )

    assert r.status_code == 200

from __future__ import annotations

import asyncio
import json

import pytest

from server.events import bus
from server.db import configured_conn, init_db


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


async def _insert_human_attention(
    *,
    agent_id: str = "p1",
    subject: str = "please review",
    body: str = "human attention body",
    urgency: str = "high",
    project_id: str = "misc",
) -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO events (ts, agent_id, project_id, type, payload) "
            "VALUES (datetime('now'), ?, ?, 'human_attention', ?)",
            (
                agent_id,
                project_id,
                json.dumps({
                    "subject": subject,
                    "body": body,
                    "urgency": urgency,
                }),
            ),
        )
        await c.commit()
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_human_attention_reply_emits_message_sent(fresh_db: str) -> None:  # noqa: ARG001
    import httpx
    from server.main import app

    await init_db()
    await _ensure_project()
    attention_id = await _insert_human_attention(
        subject="codex needs help",
        body="please check the rebase",
    )

    reply_text = "I see the issue and am on it."
    q = bus.subscribe()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            f"/api/human_attention/{attention_id}/reply",
            json={"body": reply_text},
        )
    try:
        ev = await asyncio.wait_for(q.get(), timeout=2.0)
    finally:
        bus.unsubscribe(q)

    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert payload["attention_id"] == attention_id
    assert payload["subject"].startswith(f"re: {attention_id}")

    assert ev["agent_id"] == "human"
    assert ev["type"] == "message_sent"
    assert ev["to"] == "coach"
    assert ev["subject"].startswith(f"re: {attention_id}")
    assert ev["body_preview"] == reply_text
    assert ev["priority"] == "normal"


@pytest.mark.asyncio
async def test_human_attention_reply_missing_event_404(fresh_db: str) -> None:  # noqa: ARG001
    import httpx
    from server.main import app

    await init_db()
    await _ensure_project()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/human_attention/999999/reply",
            json={"body": "hello"},
        )

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_human_attention_reply_invalid_id_422(fresh_db: str) -> None:  # noqa: ARG001
    import httpx
    from server.main import app

    await init_db()
    await _ensure_project()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            "/api/human_attention/not-an-int/reply",
            json={"body": "hello"},
        )

    assert r.status_code == 422


@pytest.mark.asyncio
async def test_human_attention_reply_empty_body_422(fresh_db: str) -> None:  # noqa: ARG001
    import httpx
    from server.main import app

    await init_db()
    await _ensure_project()
    attention_id = await _insert_human_attention()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            f"/api/human_attention/{attention_id}/reply",
            json={"body": ""},
        )

    assert r.status_code == 422


@pytest.mark.asyncio
async def test_human_attention_reply_whitespace_body_422(fresh_db: str) -> None:  # noqa: ARG001
    import httpx
    from server.main import app

    await init_db()
    await _ensure_project()
    attention_id = await _insert_human_attention()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            f"/api/human_attention/{attention_id}/reply",
            json={"body": "   \n\t  "},
        )

    assert r.status_code == 422

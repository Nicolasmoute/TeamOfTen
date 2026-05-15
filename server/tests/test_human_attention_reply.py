from __future__ import annotations

import json

import pytest

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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(
            f"/api/human_attention/{attention_id}/reply",
            json={"body": reply_text},
        )
        events = await client.get("/api/events?type=message_sent&limit=10")

    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert payload["attention_id"] == attention_id
    assert payload["subject"].startswith(f"re: {attention_id}")

    assert events.status_code == 200
    rows = events.json()["events"]
    assert rows, "expected a message_sent event to be published"
    last = rows[-1]
    assert last["agent_id"] == "human"
    assert last["type"] == "message_sent"
    assert last["payload"]["to"] == "coach"
    assert last["payload"]["subject"].startswith(f"re: {attention_id}")
    assert last["payload"]["body_preview"] == reply_text
    assert last["payload"]["priority"] == "normal"


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

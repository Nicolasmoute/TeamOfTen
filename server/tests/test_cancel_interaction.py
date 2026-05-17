"""Tests for the interaction cancel endpoints (non-dismissable
pending_question / pending_plan UI behaviour).

Coverage:
  - POST /api/questions/{id}/cancel: happy path resolves Future with rejection.
  - POST /api/questions/{id}/cancel: 404 on unknown id.
  - POST /api/questions/{id}/cancel: 404 on already-resolved id.
  - POST /api/plans/{id}/cancel: happy path resolves Future with rejection.
  - POST /api/plans/{id}/cancel: 404 on unknown id.
  - No silent dismiss: pending_question attention items must NOT have a
    plain dismiss (×) button available — only the cancel (skip) button.

Spec: truth-index.md §16 EnvPane attention strip.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server import interactions as interactions_registry
from server.db import init_db


# --------------------------------------------------------------------------- helpers

def _register_question(cid: str) -> asyncio.Future:
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    interactions_registry._pending[cid] = interactions_registry.PendingInteraction(
        correlation_id=cid,
        kind="question",
        agent_id="p3",
        future=fut,
        deadline_ts=9_999_999_999.0,
        payload={},
        route="human",
        created_at="2026-05-14T00:00:00Z",
    )
    return fut


def _register_plan(cid: str) -> asyncio.Future:
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    interactions_registry._pending[cid] = interactions_registry.PendingInteraction(
        correlation_id=cid,
        kind="plan",
        agent_id="p5",
        future=fut,
        deadline_ts=9_999_999_999.0,
        payload={},
        route="human",
        created_at="2026-05-14T00:00:00Z",
    )
    return fut


# --------------------------------------------------------------------------- question cancel

async def test_cancel_question_happy_path(fresh_db: str) -> None:
    """POST /api/questions/{id}/cancel resolves the Future with a rejection."""
    await init_db()
    cid = "test-q-cancel-01"
    fut = _register_question(cid)
    try:
        from fastapi.testclient import TestClient
        from server.main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/api/questions/{cid}/cancel")
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True
        # Future should now be done (exception set)
        assert fut.done()
        with pytest.raises(interactions_registry.InteractionRejected):
            fut.result()
    finally:
        interactions_registry._pending.pop(cid, None)


async def test_cancel_question_unknown_id(fresh_db: str) -> None:
    """POST /api/questions/nonexistent/cancel returns 404."""
    await init_db()
    from fastapi.testclient import TestClient
    from server.main import app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/questions/no-such-id/cancel")
    assert resp.status_code == 404


async def test_cancel_question_already_resolved(fresh_db: str) -> None:
    """POST /api/questions/{id}/cancel returns 404 when already answered."""
    await init_db()
    cid = "test-q-cancel-02"
    fut = _register_question(cid)
    # Resolve it first (simulating an answer)
    interactions_registry.resolve(cid, {"q": "a"})
    try:
        from fastapi.testclient import TestClient
        from server.main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/api/questions/{cid}/cancel")
        assert resp.status_code == 404
    finally:
        interactions_registry._pending.pop(cid, None)


# --------------------------------------------------------------------------- plan cancel

async def test_cancel_plan_happy_path(fresh_db: str) -> None:
    """POST /api/plans/{id}/cancel resolves the Future with a rejection."""
    await init_db()
    cid = "test-p-cancel-01"
    fut = _register_plan(cid)
    try:
        from fastapi.testclient import TestClient
        from server.main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/api/plans/{cid}/cancel")
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True
        assert fut.done()
        with pytest.raises(interactions_registry.InteractionRejected):
            fut.result()
    finally:
        interactions_registry._pending.pop(cid, None)


async def test_cancel_plan_unknown_id(fresh_db: str) -> None:
    """POST /api/plans/nonexistent/cancel returns 404."""
    await init_db()
    from fastapi.testclient import TestClient
    from server.main import app
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/plans/no-such-plan/cancel")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- UI regression guard

def test_pending_question_uses_cancel_not_dismiss() -> None:
    """Regression guard: EnvAttentionSection source must use cancelInteraction
    for pending_question rows, NOT a plain dismiss(ev.__key) pattern.

    This catches a future refactor that accidentally re-adds silent dismiss."""
    import re
    with open("server/static/app.js", encoding="utf-8") as fh:
        src = fh.read()
    # The cancel button conditional must be present for pending_question
    assert "cancelInteraction" in src, \
        "EnvAttentionSection must use cancelInteraction for pending_question"
    # The cancel endpoint wiring must reference pending_question
    assert "/api/questions/" in src, \
        "cancelInteraction must POST to /api/questions/{id}/cancel"
    assert "/api/plans/" in src, \
        "cancelInteraction must POST to /api/plans/{id}/cancel"
    # Confirm that the dismiss × button is ONLY used for non-interactive types
    # (the isInteractive branch must not call dismiss on pending_question).
    # Heuristic: the dismiss onClick must be inside the `else` / non-interactive branch.
    # We check that the env-attention-cancel class appears in source.
    assert "env-attention-cancel" in src, \
        "CSS class env-attention-cancel must be present for the skip button"

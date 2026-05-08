"""v2.0.1 (2026-05-08) — `trajectory[0].to` must name exactly one Player.

The kanban is a log of work Coach has fired AT a specific Player.
Pool/empty first-stage `to` is rejected at trajectory-validation time
so both `coord_create_task` (MCP) and `POST /api/tasks` (HTTP) honor
the rule. Subsequent stages' `to` may still be pool/empty (FYI only).

This test pins the boundary: pool/empty first-stage rejected, single-
name accepted, MCP and HTTP layers behave identically.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

import server.agents as agents_mod
from server.db import init_db
from server.main import app
from server.tools import build_coord_server, _validate_trajectory


# ---------------------------------------------------------------- pure validator

def test_validator_rejects_empty_first_stage() -> None:
    out, err = _validate_trajectory(
        '[{"stage":"execute","to":[]}]'
    )
    assert out is None
    assert err is not None
    assert "trajectory[0].to" in err
    assert "exactly one Player" in err


def test_validator_rejects_pool_first_stage() -> None:
    out, err = _validate_trajectory(
        '[{"stage":"execute","to":["p2","p3"]}]'
    )
    assert out is None
    assert err is not None
    assert "trajectory[0].to" in err


def test_validator_accepts_single_name_first_stage() -> None:
    out, err = _validate_trajectory(
        '[{"stage":"execute","to":["p3"]}]'
    )
    assert err is None
    assert out is not None
    assert out[0]["to"] == ["p3"]


def test_validator_allows_pool_on_subsequent_stages() -> None:
    """First stage single-name; subsequent stages may still be FYI/empty."""
    out, err = _validate_trajectory(
        '[{"stage":"execute","to":["p2"]},'
        '{"stage":"audit_syntax","to":[]},'
        '{"stage":"ship","to":["p2","p3"]}]'
    )
    assert err is None
    assert out is not None
    assert out[0]["to"] == ["p2"]
    assert out[1]["to"] == []
    assert out[2]["to"] == ["p2", "p3"]


def test_validator_default_execute_only_rejected() -> None:
    """The HTTP `POST /api/tasks` default is `[{stage:execute,to:[]}]`
    when the caller omits the trajectory. After v2.0.1, this default
    needs explicit assignment — the HTTP path must supply a `to`."""
    out, err = _validate_trajectory(
        '[{"stage":"execute","to":[]}]'
    )
    assert out is None
    assert err is not None


# ---------------------------------------------------------------- MCP path

def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok(result: dict[str, Any]) -> str:
    assert not result.get("isError"), (
        f"tool returned error: {result.get('content')}"
    )
    return result["content"][0]["text"]


def _err(result: dict[str, Any]) -> str:
    assert result.get("isError"), f"expected error, got {result}"
    return result["content"][0]["text"]


def _extract_task_id(body: str) -> str:
    m = re.search(r"t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}", body)
    assert m, f"no task id in body: {body}"
    return m.group(0)


async def _stub_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _rec(*a: Any, **k: Any) -> bool:
        return True
    monkeypatch.setattr(agents_mod, "maybe_wake_agent", _rec)


async def test_mcp_create_task_rejects_pool_first_stage(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    err = _err(await _handler(coach, "create_task")({
        "title": "pool first",
        "description": "x",
        "trajectory": '[{"stage":"execute","to":["p2","p3"]}]',
    }))
    assert "trajectory[0].to" in err
    assert "exactly one Player" in err


async def test_mcp_create_task_rejects_empty_first_stage(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    err = _err(await _handler(coach, "create_task")({
        "title": "empty first",
        "description": "x",
        "trajectory": '[{"stage":"execute","to":[]}]',
    }))
    assert "trajectory[0].to" in err


async def test_mcp_create_task_accepts_single_name_first_stage(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    body = _ok(await _handler(coach, "create_task")({
        "title": "single name",
        "description": "x",
        "trajectory": (
            '[{"stage":"execute","to":["p2"]},'
            '{"stage":"audit_syntax","to":[]}]'
        ),
    }))
    tid = _extract_task_id(body)
    assert tid


# ---------------------------------------------------------------- HTTP path

async def test_http_create_rejects_pool_first_stage(fresh_db: str) -> None:
    await init_db()
    client = TestClient(app)
    r = client.post(
        "/api/tasks",
        json={
            "title": "http pool",
            "trajectory": [{"stage": "execute", "to": ["p2", "p3"]}],
        },
    )
    assert r.status_code == 400, r.text
    assert "trajectory[0].to" in r.json()["detail"]


async def test_http_create_rejects_empty_first_stage(fresh_db: str) -> None:
    await init_db()
    client = TestClient(app)
    r = client.post(
        "/api/tasks",
        json={
            "title": "http empty",
            "trajectory": [{"stage": "execute", "to": []}],
        },
    )
    assert r.status_code == 400, r.text
    assert "trajectory[0].to" in r.json()["detail"]


async def test_http_create_rejects_default_no_trajectory(fresh_db: str) -> None:
    """HTTP default is `[{stage:execute,to:[]}]` when caller omits
    trajectory. v2.0.1 requires the caller to supply a single-name
    first-stage `to` explicitly."""
    await init_db()
    client = TestClient(app)
    r = client.post(
        "/api/tasks",
        json={"title": "http default"},
    )
    assert r.status_code == 400, r.text


async def test_http_create_accepts_single_name_first_stage(fresh_db: str) -> None:
    await init_db()
    client = TestClient(app)
    r = client.post(
        "/api/tasks",
        json={
            "title": "http single",
            "trajectory": [{"stage": "execute", "to": ["p2"]}],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json().get("task_id")

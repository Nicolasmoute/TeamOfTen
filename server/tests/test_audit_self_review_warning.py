"""Phase 6.1 — audit_self_review_warning emission (v2 §5.3).

When Coach assigns the same Player to an audit role that's also the
task's executor (`tasks.owner`), the harness emits an
`audit_self_review_warning` event. The Telegram bridge formats it for
the human; the project_events log captures it so Coach sees the
pattern in `## Recent events`.

Informational only — Coach can pick this deliberately (small team,
specialist scarcity); no rejection.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

import server.agents as agents_mod
from server.db import configured_conn, init_db
from server.events import bus
from server.tools import build_coord_server


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok(result: dict[str, Any]) -> str:
    assert not result.get("isError"), (
        f"tool returned error: {result.get('content')}"
    )
    return result["content"][0]["text"]


def _drain(queue: Any) -> list[dict]:
    out: list[dict] = []
    while True:
        try:
            out.append(queue.get_nowait())
        except Exception:
            break
    return out


def _extract_task_id(body: str) -> str:
    m = re.search(r"t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}", body)
    assert m, f"no task id in body: {body}"
    return m.group(0)


async def _stub_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _rec(*a: Any, **k: Any) -> bool:
        return True
    monkeypatch.setattr(agents_mod, "maybe_wake_agent", _rec)


# ---------------------------------------------------------------- emission

async def test_self_review_warning_fires_on_same_player_audit(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coach approves audit_syntax with assignee == task executor →
    `audit_self_review_warning` fires."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    body = _ok(await _handler(coach, "create_task")({
        "title": "self-review demo", "description": "x",
        "trajectory": (
            '[{"stage":"execute","to":["p3"]},'
            '{"stage":"audit_syntax","to":[]}]'
        ),
    }))
    tid = _extract_task_id(body)

    q = bus.subscribe()
    try:
        _ok(await _handler(coach, "approve_stage")({
            "task_id": tid,
            "next_stage": "audit_syntax",
            "assignee": "p3",  # same as the executor
            "note": "self-review by design",
        }))
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    warnings = [e for e in events if e.get("type") == "audit_self_review_warning"]
    assert len(warnings) == 1
    w = warnings[0]
    assert w["task_id"] == tid
    assert w["kind"] == "syntax"
    assert w["auditor_id"] == "p3"
    assert w["executor_id"] == "p3"
    assert w["to"] == "coach"


async def test_self_review_warning_quiet_on_different_player(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different auditor → no warning."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    body = _ok(await _handler(coach, "create_task")({
        "title": "no-self-review demo", "description": "x",
        "trajectory": (
            '[{"stage":"execute","to":["p3"]},'
            '{"stage":"audit_syntax","to":[]}]'
        ),
    }))
    tid = _extract_task_id(body)

    q = bus.subscribe()
    try:
        _ok(await _handler(coach, "approve_stage")({
            "task_id": tid,
            "next_stage": "audit_syntax",
            "assignee": "p4",
            "note": "review please",
        }))
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    assert not any(
        e.get("type") == "audit_self_review_warning" for e in events
    )


async def test_self_review_warning_kind_semantics(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit_semantics → kind='semantics' on the warning."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    body = _ok(await _handler(coach, "create_task")({
        "title": "semantic self-review", "description": "x",
        "trajectory": (
            '[{"stage":"execute","to":["p7"]},'
            '{"stage":"audit_semantics","to":[],"focus":"check"}]'
        ),
    }))
    tid = _extract_task_id(body)

    q = bus.subscribe()
    try:
        _ok(await _handler(coach, "approve_stage")({
            "task_id": tid,
            "next_stage": "audit_semantics",
            "assignee": "p7",
            "note": "self-semantic by design",
        }))
        events = _drain(q)
    finally:
        bus.unsubscribe(q)

    warnings = [e for e in events if e.get("type") == "audit_self_review_warning"]
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "semantics"


# ---------------------------------------------------------------- event log

async def test_self_review_warning_in_project_events_log(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 6.1 also added the warning to _LOGGABLE_BUS_TYPES so it
    surfaces in `## Recent events` for Coach."""
    from server.project_events import maybe_write_from_bus
    await init_db()
    row_id = await maybe_write_from_bus({
        "type": "audit_self_review_warning",
        "agent_id": "coach",
        "project_id": "misc",
        "task_id": "t-2026-05-07-99999999",
        "kind": "syntax",
        "auditor_id": "p3",
        "executor_id": "p3",
    })
    assert row_id is not None
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT type FROM project_events WHERE id = ?", (row_id,)
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row)["type"] == "audit_self_review_warning"

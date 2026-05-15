"""Phase 4 — push-time deviation instrumentation (v2 §22.1).

`coord_approve_stage` must insert a `deviations_log{noticed_at='push'}`
row when:
  - the source stage is `execute` (Coach is reviewing executor work),
  - the note carries a deviation marker (structured `[deviation: ...]`
    tag preferred; bare phrases `deviation`, `off-spec`, `scope drift`,
    `unexpected change` as fallback).

This feeds the §11.1 `off_spec_completion_count` Player health counter
and the §22 push-vs-audit validation criterion.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

import server.agents as agents_mod
from server.db import configured_conn, init_db
from server.tools import (
    _extract_deviation_description,
    build_coord_server,
)


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok(result: dict[str, Any]) -> str:
    assert not result.get("is_error"), (
        f"tool returned error: {result.get('content')}"
    )
    return result["content"][0]["text"]


def _extract_task_id(body: str) -> str:
    m = re.search(r"t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}", body)
    assert m, f"no task id in body: {body}"
    return m.group(0)


def _extract_backlog_id(body: str) -> int:
    m = re.search(r"Backlog entry #(\d+)", body)
    assert m, f"no backlog id in body: {body}"
    return int(m.group(1))


async def _create_and_promote(coach: Any, args: dict[str, Any]) -> str:
    body = _ok(await _handler(coach, "create_task")(args))
    backlog_id = _extract_backlog_id(body)
    promoted = _ok(await _handler(coach, "triage_backlog")({
        "id": str(backlog_id),
        "action": "promote",
    }))
    return _extract_task_id(promoted)


async def _stub_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _rec(*a: Any, **k: Any) -> bool:
        return True
    monkeypatch.setattr(agents_mod, "maybe_wake_agent", _rec)


async def _deviation_rows(task_id: str) -> list[dict]:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT executor, noticed_at, description FROM deviations_log "
            "WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()


# ---------------------------------------------------------------- pure matcher


def test_extract_structured_tag_basic() -> None:
    body = _extract_deviation_description(
        "[deviation: scope creep — swapped header for footer] redo with header"
    )
    assert body == "scope creep — swapped header for footer"


def test_extract_structured_tag_at_end() -> None:
    """Tag appears anywhere in the note."""
    body = _extract_deviation_description(
        "please redo. [deviation: off-spec footer change]"
    )
    assert body == "off-spec footer change"


def test_extract_structured_tag_case_insensitive() -> None:
    body = _extract_deviation_description("[DEVIATION: bad]")
    assert body == "bad"


def test_extract_structured_tag_unclosed() -> None:
    """Unclosed tag → still treated as flagged with a placeholder + tail."""
    body = _extract_deviation_description("[deviation: forgot to close")
    assert body and "forgot" in body


def test_extract_bare_phrase_fallback() -> None:
    assert _extract_deviation_description("this is off-spec") == "deviation flagged in note"
    assert _extract_deviation_description("scope drift detected") == "deviation flagged in note"
    assert _extract_deviation_description("unexpected change in headers") == "deviation flagged in note"
    assert _extract_deviation_description("a clear deviation here") == "deviation flagged in note"


def test_extract_no_match_returns_none() -> None:
    assert _extract_deviation_description("please redo with the original spec") is None
    assert _extract_deviation_description("") is None
    assert _extract_deviation_description("   ") is None


# ---------------------------------------------------------------- end-to-end

async def _setup_execute_task(coach: Any) -> str:
    return await _create_and_promote(coach, {
        "title": "phase4 demo",
        "description": "x",
        "trajectory": (
            '[{"stage":"execute","to":["p3"]},'
            '{"stage":"audit_syntax","to":["p4"]}]'
        ),
    })


async def test_approve_stage_with_tag_inserts_push_row(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tagged note + source stage execute → row inserted with the tag's
    extracted text as description, executor=tasks.owner."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    tid = await _setup_execute_task(coach)

    # Coach advances execute → audit_syntax with a deviation tag in the
    # note (push-time review — Coach noticed scope drift before audit).
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid,
        "next_stage": "audit_syntax",
        "assignee": "p4",
        "note": "[deviation: scope creep — swapped header for footer] formal review please",
    }))

    rows = await _deviation_rows(tid)
    push_rows = [r for r in rows if r["noticed_at"] == "push"]
    assert len(push_rows) == 1
    assert push_rows[0]["executor"] == "p3"
    assert "scope creep" in push_rows[0]["description"]


async def test_approve_stage_with_bare_phrase_inserts_push_row(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare-phrase fallback also fires the row insert."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    tid = await _setup_execute_task(coach)

    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid,
        "next_stage": "audit_syntax",
        "assignee": "p4",
        "note": "this commit is off-spec — review with focus on contract drift",
    }))
    rows = await _deviation_rows(tid)
    push_rows = [r for r in rows if r["noticed_at"] == "push"]
    assert len(push_rows) == 1
    assert push_rows[0]["executor"] == "p3"


async def test_approve_stage_clean_note_no_row(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A note without any deviation marker does NOT insert a row."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    tid = await _setup_execute_task(coach)

    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid,
        "next_stage": "audit_syntax",
        "assignee": "p4",
        "note": "looks good — formal review please",
    }))
    rows = await _deviation_rows(tid)
    assert all(r["noticed_at"] != "push" for r in rows)


async def test_approve_stage_non_execute_source_no_row(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source stage != execute → no push row even with a deviation tag."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")

    tid = await _create_and_promote(coach, {
        "title": "non-execute source",
        "description": "x",
        "trajectory": (
            '[{"stage":"plan","to":["p5"]},'
            '{"stage":"execute","to":["p3"]}]'
        ),
    })

    # plan → execute approval with a deviation tag — source is `plan`,
    # not `execute`, so no push row even though the tag is present.
    # First we have to write a spec to satisfy the role gate.
    p5 = _server_for("p5")
    _ok(await _handler(p5, "write_task_spec")({
        "task_id": tid, "body": "## Goal\nx\n",
    }))
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "execute", "assignee": "p3",
        "note": "[deviation: spec drifted from objective]",
    }))
    rows = await _deviation_rows(tid)
    assert all(r["noticed_at"] != "push" for r in rows)


async def test_push_deviation_lights_up_player_health_counter(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A push-time deviation must surface in
    `compute_player_health_counters` under `off_spec_completions`."""
    await init_db()
    await _stub_wake(monkeypatch)
    coach = _server_for("coach")
    tid = await _setup_execute_task(coach)
    _ok(await _handler(coach, "approve_stage")({
        "task_id": tid, "next_stage": "audit_syntax", "assignee": "p4",
        "note": "[deviation: drifted off-contract]",
    }))

    from server.agents import compute_player_health_counters
    rows = await compute_player_health_counters("misc")
    p3_row = next((r for r in rows if r["slot"] == "p3"), None)
    assert p3_row is not None
    assert p3_row["off_spec_completions"] >= 1

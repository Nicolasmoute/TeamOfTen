from __future__ import annotations

import json
import re
from typing import Any

import pytest

from server.db import configured_conn, init_db
from server.tools import build_coord_server


_TRAJECTORY = '[{"stage":"execute","to":["p2"]},{"stage":"ship","to":[]}]'


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"][f"coord_{name}"]


def _ok(result: dict[str, Any]) -> str:
    assert not result.get("is_error"), result
    return result["content"][0]["text"]


def _err(result: dict[str, Any]) -> str:
    assert result.get("is_error"), result
    return result["content"][0]["text"]


def _extract_id(pattern: str, body: str) -> str:
    m = re.search(pattern, body)
    assert m, body
    return m.group(1)


async def _promote_truthgate_task() -> str:
    coach = _server_for("coach")
    created = _ok(await _handler(coach, "create_task")({
        "title": "phase 3 truthgate",
        "description": "exercise run tool",
        "trajectory": _TRAJECTORY,
    }))
    backlog_id = _extract_id(r"Backlog entry #(\d+)", created)
    promoted = _ok(await _handler(coach, "triage_backlog")({
        "id": backlog_id,
        "action": "promote",
    }))
    return _extract_id(r"(t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8})", promoted)


@pytest.mark.asyncio
async def test_coord_run_truthgate_records_classifier_pass(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_db()
    task_id = await _promote_truthgate_task()

    async def fake_run(project_id: str, task: Any) -> dict[str, Any]:
        assert project_id == "misc"
        assert task.task_id == task_id
        return {
            "verdict": "truthgate_pass",
            "truth_basis": ["truth/truth-index.md"],
            "truth_concerns": ["stay inside the index"],
            "rationale": "covered",
            "method": "classifier",
            "model_alias": "latest_sonnet",
            "warning": None,
        }

    import server.truthgate.classifier as classifier

    monkeypatch.setattr(classifier, "run_truthgate_classifier", fake_run)

    coach = _server_for("coach")
    out = _ok(await _handler(coach, "run_truthgate")({"task_id": task_id}))
    assert "TruthGate recorded truthgate_pass" in out

    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT status, truthgate_verdict, truth_basis, truth_concerns, "
            "truthgate_method, truthgate_model, blocked "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )).fetchone())
    finally:
        await c.close()

    assert row["status"] == "truthgate"
    assert row["truthgate_verdict"] == "truthgate_pass"
    assert json.loads(row["truth_basis"]) == ["truth/truth-index.md"]
    assert json.loads(row["truth_concerns"]) == ["stay inside the index"]
    assert row["truthgate_method"] == "classifier"
    assert row["truthgate_model"] == "latest_sonnet"
    assert row["blocked"] == 0


@pytest.mark.asyncio
async def test_coord_run_truthgate_needs_change_blocks_exit(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_db()
    task_id = await _promote_truthgate_task()

    async def fake_run(project_id: str, task: Any) -> dict[str, Any]:
        return {
            "verdict": "truthgate_needs_truth_change",
            "truth_basis": [],
            "truth_concerns": ["truth is missing"],
            "rationale": "needs new spec",
            "method": "classifier",
            "model_alias": "latest_sonnet",
            "warning": None,
        }

    import server.truthgate.classifier as classifier

    monkeypatch.setattr(classifier, "run_truthgate_classifier", fake_run)
    coach = _server_for("coach")

    out = _ok(await _handler(coach, "run_truthgate")({"task_id": task_id}))
    assert "task remains blocked" in out

    err = _err(await _handler(coach, "approve_stage")({
        "task_id": task_id,
        "next_stage": "execute",
        "assignee": "p2",
        "note": "go",
    }))
    assert "requires a TruthGate pass or override" in err

    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT truthgate_verdict, blocked, blocked_reason "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )).fetchone())
    finally:
        await c.close()
    assert row["truthgate_verdict"] == "truthgate_needs_truth_change"
    assert row["blocked"] == 1
    assert "truth amendment" in row["blocked_reason"]


@pytest.mark.asyncio
async def test_coord_run_truthgate_existing_verdict_requires_force(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_db()
    task_id = await _promote_truthgate_task()

    async def fake_block(project_id: str, task: Any) -> dict[str, Any]:
        return {
            "verdict": "truthgate_needs_truth_change",
            "truth_basis": [],
            "truth_concerns": ["truth is missing"],
            "rationale": "needs new spec",
            "method": "classifier",
            "model_alias": "latest_sonnet",
            "warning": None,
        }

    async def fake_pass(project_id: str, task: Any) -> dict[str, Any]:
        return {
            "verdict": "truthgate_pass",
            "truth_basis": ["truth/truth-index.md"],
            "truth_concerns": [],
            "rationale": "covered",
            "method": "classifier",
            "model_alias": "latest_sonnet",
            "warning": None,
        }

    import server.truthgate.classifier as classifier

    monkeypatch.setattr(classifier, "run_truthgate_classifier", fake_block)
    coach = _server_for("coach")
    _ok(await _handler(coach, "run_truthgate")({"task_id": task_id}))

    monkeypatch.setattr(classifier, "run_truthgate_classifier", fake_pass)
    err = _err(await _handler(coach, "run_truthgate")({"task_id": task_id}))
    assert "already has TruthGate verdict" in err

    out = _ok(await _handler(coach, "run_truthgate")({
        "task_id": task_id,
        "force": "true",
    }))
    assert "truthgate_pass" in out

    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT truthgate_verdict, blocked, blocked_reason "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )).fetchone())
    finally:
        await c.close()
    assert row["truthgate_verdict"] == "truthgate_pass"
    assert row["blocked"] == 0
    assert row["blocked_reason"] is None


@pytest.mark.asyncio
async def test_coord_run_truthgate_classifier_error_fails_closed_without_verdict(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_db()
    task_id = await _promote_truthgate_task()

    import server.truthgate.classifier as classifier

    async def fake_run(project_id: str, task: Any) -> dict[str, Any]:
        raise classifier.TruthGateClassificationError("invalid JSON")

    monkeypatch.setattr(classifier, "run_truthgate_classifier", fake_run)
    coach = _server_for("coach")
    err = _err(await _handler(coach, "run_truthgate")({"task_id": task_id}))
    assert "failed closed" in err

    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT truthgate_verdict, truthgate_method, blocked, "
            "blocked_reason FROM tasks WHERE id = ?",
            (task_id,),
        )).fetchone())
    finally:
        await c.close()
    assert row["truthgate_verdict"] is None
    assert row["truthgate_method"] == "classifier_error"
    assert row["blocked"] == 1
    assert "invalid JSON" in row["blocked_reason"]


@pytest.mark.asyncio
async def test_coord_record_truthgate_override_allows_exit_and_sets_provisional(
    fresh_db: str,
) -> None:
    await init_db()
    task_id = await _promote_truthgate_task()
    coach = _server_for("coach")

    err = _err(await _handler(coach, "record_truthgate_override")({
        "task_id": task_id,
        "kind": "coach_override",
        "rationale": "",
    }))
    assert "rationale is required" in err

    out = _ok(await _handler(coach, "record_truthgate_override")({
        "task_id": task_id,
        "kind": "emergency_override",
        "rationale": "human approved urgent bypass",
        "closure_reference": "none_needed:incident approved by human",
    }))
    assert "truthgate_emergency_override" in out
    assert "provisional" in out

    ok = _ok(await _handler(coach, "approve_stage")({
        "task_id": task_id,
        "next_stage": "execute",
        "assignee": "p2",
        "note": "post override",
    }))
    assert "truthgate → execute" in ok

    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT status, truthgate_verdict, truthgate_method, "
            "truthgate_override_rationale, provisional, closure_reference "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )).fetchone())
    finally:
        await c.close()
    assert row["status"] == "execute"
    assert row["truthgate_verdict"] == "truthgate_emergency_override"
    assert row["truthgate_method"] == "emergency_override"
    assert row["truthgate_override_rationale"] == "human approved urgent bypass"
    assert row["provisional"] == 1
    assert row["closure_reference"] == "none_needed:incident approved by human"


@pytest.mark.asyncio
async def test_truthgate_pass_preserves_unrelated_blocked_reason(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_db()
    task_id = await _promote_truthgate_task()
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE tasks SET blocked = 1, blocked_reason = ? WHERE id = ?",
            ("External deployment hold", task_id),
        )
        await c.commit()
    finally:
        await c.close()

    async def fake_run(project_id: str, task: Any) -> dict[str, Any]:
        return {
            "verdict": "truthgate_pass",
            "truth_basis": ["truth/truth-index.md"],
            "truth_concerns": [],
            "rationale": "covered",
            "method": "classifier",
            "model_alias": "latest_sonnet",
            "warning": None,
        }

    import server.truthgate.classifier as classifier

    monkeypatch.setattr(classifier, "run_truthgate_classifier", fake_run)
    coach = _server_for("coach")
    _ok(await _handler(coach, "run_truthgate")({"task_id": task_id}))

    c = await configured_conn()
    try:
        row = dict(await (await c.execute(
            "SELECT truthgate_verdict, blocked, blocked_reason "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )).fetchone())
    finally:
        await c.close()
    assert row["truthgate_verdict"] == "truthgate_pass"
    assert row["blocked"] == 1
    assert row["blocked_reason"] == "External deployment hold"


@pytest.mark.asyncio
async def test_truthgate_tools_are_coach_only(fresh_db: str) -> None:
    await init_db()
    task_id = await _promote_truthgate_task()
    p2 = _server_for("p2")

    assert "Coach-only" in _err(await _handler(p2, "run_truthgate")({
        "task_id": task_id,
    }))
    assert "Coach-only" in _err(await _handler(p2, "record_truthgate_override")({
        "task_id": task_id,
        "kind": "coach_override",
        "rationale": "no-op",
    }))

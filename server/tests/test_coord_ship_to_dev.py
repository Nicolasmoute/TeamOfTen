"""Tests for coord_ship_to_dev — audit-pass gate + ship workflow.

Required cases (from spec):
(a) Happy path — task in ship, caller is shipper, audit_syntax PASS, executor
    commit exists, git ops succeed, GitHub API stubs return 201/200/204.
    Asserts: shipper role row has completed_at, task_shipped_to_dev event
    published, return ok=True with pr_number and dev_sha.
(b) Coach rejection — caller='coach'. Error contains "Player tool".
(c) Wrong stage — task in 'execute'. Error contains "not 'ship'".
(d) Wrong caller — task in ship, p2 calls but shipper row owner='p3'.
    Error contains "not the active shipper".
(e) Missing audit verdict — trajectory includes audit_syntax, but no
    auditor_syntax row. Error contains "audit_syntax has no PASS verdict".
(f) Audit FAIL not superseded — auditor_syntax row with verdict='fail',
    superseded_by IS NULL. Error contains "audit_syntax has no PASS verdict".
(g) No executor commit — no commit_pushed event for the task.
    Error contains "no executor commit found".
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.db import configured_conn, init_db
from server.events import bus

TASK_ID = "t-2026-05-14-ship0001"
MISC_PROJECT = "misc"
EXECUTOR_SHA = "deadbeef1234567890abcdef12345678deadbeef"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _server_for(slot: str) -> Any:
    from server.tools import build_coord_server

    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    h = server["_handlers"].get(f"coord_{name}") or server["_handlers"].get(name)
    if h is None:
        raise KeyError(f"no handler for coord_{name}")
    return h


def _ok_text(result: dict[str, Any]) -> str:
    assert not result.get("is_error"), f"unexpected error: {result}"
    return result["content"][0]["text"]


def _err_text(result: dict[str, Any]) -> str:
    assert result.get("is_error"), f"expected error, got {result}"
    return result["content"][0]["text"]


async def _seed_task(
    *,
    task_id: str = TASK_ID,
    status: str = "ship",
    owner: str = "p2",
    trajectory: list[dict] | None = None,
) -> None:
    if trajectory is None:
        trajectory = [
            {"stage": "execute", "to": ["p2"]},
            {"stage": "audit_syntax", "to": ["p4"]},
            {"stage": "ship", "to": ["p2"]},
        ]
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) "
            "VALUES (?, ?, 'ship test', ?, ?, 'coach', ?)",
            (task_id, MISC_PROJECT, status, owner, json.dumps(trajectory)),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_shipper_role(
    *,
    task_id: str = TASK_ID,
    owner: str = "p2",
    completed_at: str | None = None,
    superseded_by: int | None = None,
) -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, "
            "completed_at, superseded_by) "
            "VALUES (?, 'shipper', '[]', ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?)",
            (task_id, owner, completed_at, superseded_by),
        )
        await c.commit()
        return cur.lastrowid
    finally:
        await c.close()


async def _seed_auditor_role(
    *,
    task_id: str = TASK_ID,
    role: str = "auditor_syntax",
    owner: str = "p4",
    verdict: str | None = "pass",
    superseded_by: int | None = None,
) -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, "
            "verdict, superseded_by) "
            "VALUES (?, ?, '[]', ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?)",
            (task_id, role, owner, verdict, superseded_by),
        )
        await c.commit()
        return cur.lastrowid
    finally:
        await c.close()


async def _seed_executor_commit(
    *,
    task_id: str = TASK_ID,
    sha: str = EXECUTOR_SHA,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO project_events "
            "(project_id, actor, type, task_id, payload_json, payload_pointer) "
            "VALUES (?, 'p2', 'commit_pushed', ?, '{}', ?)",
            (MISC_PROJECT, task_id, sha),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_repo_url(
    *,
    repo_url: str = "https://ghtoken@github.com/owner/repo",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE projects SET repo_url = ? WHERE id = ?",
            (repo_url, MISC_PROJECT),
        )
        await c.commit()
    finally:
        await c.close()


def _make_workspace(tmp_path: Path) -> Path:
    cwd = tmp_path / "work" / "p2"
    cwd.mkdir(parents=True)
    return cwd


def _stub_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    import server.tools as tools_mod

    cwd = _make_workspace(tmp_path)

    async def _workspace_dir(_slot: str) -> Path:
        return cwd

    monkeypatch.setattr(tools_mod, "workspace_dir", _workspace_dir)
    return cwd


def _stub_git_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub subprocess.run so all git commands succeed."""

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)


def _stub_github_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch httpx.AsyncClient so GitHub API calls return success stubs."""
    import httpx

    class _FakeResponse:
        def __init__(self, status_code: int, data: dict) -> None:
            self.status_code = status_code
            self._data = data
            self.text = json.dumps(data)

        def json(self) -> dict:
            return self._data

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            pass

        async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
            # PR creation
            return _FakeResponse(201, {"number": 42, "html_url": "https://github.com/owner/repo/pull/42"})

        async def put(self, url: str, **kwargs: Any) -> _FakeResponse:
            # PR merge
            return _FakeResponse(200, {"sha": "aabbccdd11223344556677889900aabbccdd1122"})

        async def delete(self, url: str, **kwargs: Any) -> _FakeResponse:
            # Branch delete
            return _FakeResponse(204, {})

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


async def test_ship_to_dev_happy_path(
    fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) Full happy path — all gates pass, git + GitHub succeed."""
    await init_db()
    await _seed_task()
    await _seed_shipper_role()
    await _seed_auditor_role()
    await _seed_executor_commit()
    await _seed_repo_url()
    _stub_workspace(monkeypatch, tmp_path)
    _stub_git_success(monkeypatch)
    _stub_github_success(monkeypatch)

    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("p2")
        result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
        text = _ok_text(result)
        assert "shipped to dev" in text.lower()
        assert "42" in text  # pr_number
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while True:
            try:
                captured.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    # Event emitted
    shipped = [e for e in captured if e.get("type") == "task_shipped_to_dev"]
    assert shipped, f"no task_shipped_to_dev event: {captured}"
    ev = shipped[0]
    assert ev["task_id"] == TASK_ID
    assert ev["pr_number"] == 42
    assert ev["executor_sha"] == EXECUTOR_SHA

    # Shipper role row marked completed
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'shipper'",
            (TASK_ID,),
        )
        row = dict(await cur.fetchone())
        assert row["completed_at"], "shipper role row should have completed_at set"
    finally:
        await c.close()


async def test_ship_to_dev_rejects_coach(fresh_db: str) -> None:
    """(b) Coach is not allowed to call coord_ship_to_dev."""
    await init_db()
    await _seed_task()
    server = _server_for("coach")
    result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
    err = _err_text(result)
    assert "player tool" in err.lower()


async def test_ship_to_dev_rejects_wrong_stage(fresh_db: str) -> None:
    """(c) Task not in 'ship' stage is rejected."""
    await init_db()
    await _seed_task(status="execute")
    server = _server_for("p2")
    result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
    err = _err_text(result)
    assert "not 'ship'" in err


async def test_ship_to_dev_rejects_non_shipper(fresh_db: str) -> None:
    """(d) Caller p2 is not the active shipper (row owner is p3)."""
    await init_db()
    await _seed_task(owner="p3")
    await _seed_shipper_role(owner="p3")  # p3 is shipper, not p2
    server = _server_for("p2")
    result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
    err = _err_text(result)
    assert "not the active shipper" in err


async def test_ship_to_dev_rejects_missing_audit_verdict(fresh_db: str) -> None:
    """(e) Trajectory has audit_syntax but no auditor_syntax role row exists."""
    await init_db()
    await _seed_task()  # trajectory includes audit_syntax
    await _seed_shipper_role()
    await _seed_executor_commit()
    # No auditor role row seeded
    server = _server_for("p2")
    result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
    err = _err_text(result)
    assert "audit_syntax has no pass verdict" in err.lower()


async def test_ship_to_dev_rejects_audit_fail_not_superseded(
    fresh_db: str,
) -> None:
    """(f) auditor_syntax row has verdict='fail', superseded_by IS NULL."""
    await init_db()
    await _seed_task()
    await _seed_shipper_role()
    await _seed_auditor_role(verdict="fail")  # FAIL verdict, not superseded
    await _seed_executor_commit()
    server = _server_for("p2")
    result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
    err = _err_text(result)
    assert "audit_syntax has no pass verdict" in err.lower()


async def test_ship_to_dev_rejects_no_executor_commit(fresh_db: str) -> None:
    """(g) No commit_pushed project_event for the task."""
    await init_db()
    await _seed_task()
    await _seed_shipper_role()
    await _seed_auditor_role()
    # No executor commit seeded
    server = _server_for("p2")
    result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
    err = _err_text(result)
    assert "no executor commit found" in err


async def test_ship_to_dev_expands_github_token_placeholder(
    fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(h) repo_url uses ${GITHUB_TOKEN} placeholder — must be expanded before
    GitHub API call.  Without the fix, the Bearer token would be the literal
    string '${GITHUB_TOKEN}' and every GitHub call returns 401."""
    import os

    await init_db()
    await _seed_task()
    await _seed_shipper_role()
    await _seed_auditor_role()
    await _seed_executor_commit()
    # Store URL with placeholder, not a raw token
    await _seed_repo_url(repo_url="https://${GITHUB_TOKEN}@github.com/owner/repo")
    _stub_workspace(monkeypatch, tmp_path)
    _stub_git_success(monkeypatch)

    # Inject GITHUB_TOKEN into env so _expand_placeholders can resolve it
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_testtoken123")

    # Capture the Authorization header seen by the GitHub API stub
    captured_auth: list[str] = []

    import httpx

    class _AuthCapturingClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_AuthCapturingClient":
            return self

        async def __aexit__(self, *_: Any) -> None:
            pass

        async def post(self, url: str, **kwargs: Any):
            captured_auth.append(kwargs.get("headers", {}).get("Authorization", ""))

            class _R:
                status_code = 201
                text = '{"number": 7, "html_url": "https://github.com/owner/repo/pull/7"}'

                def json(self):
                    return {"number": 7, "html_url": self.text}

            return _R()

        async def put(self, url: str, **kwargs: Any):
            class _R:
                status_code = 200
                text = '{"sha": "aabbccdd1122"}'

                def json(self):
                    return {"sha": "aabbccdd1122"}

            return _R()

        async def delete(self, url: str, **kwargs: Any):
            class _R:
                status_code = 204
                text = ""

                def json(self):
                    return {}

            return _R()

    monkeypatch.setattr("httpx.AsyncClient", _AuthCapturingClient)

    server = _server_for("p2")
    result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
    _ok_text(result)  # must not error

    # The Authorization header must use the expanded token, not the literal placeholder
    assert captured_auth, "no GitHub API call was made"
    assert "Bearer ghp_testtoken123" in captured_auth[0], (
        f"Expected expanded token, got: {captured_auth[0]}"
    )
    assert "${GITHUB_TOKEN}" not in captured_auth[0], (
        f"Placeholder was NOT expanded: {captured_auth[0]}"
    )


async def test_ship_to_dev_empty_expanded_token_returns_error(
    fresh_db: str,
) -> None:
    """(i) Placeholder expands to empty string (env var not set) → clear error."""
    await init_db()
    await _seed_task()
    await _seed_shipper_role()
    await _seed_auditor_role()
    await _seed_executor_commit()
    await _seed_repo_url(repo_url="https://${MISSING_TOKEN}@github.com/owner/repo")

    server = _server_for("p2")
    result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
    err = _err_text(result)
    assert "expanded to empty" in err or "MISSING_TOKEN" in err or "PAT" in err

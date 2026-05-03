"""Tests for fix #3: coord_commit_push must validate task_id ownership
at entry and only drive the kanban subscriber on a successful push.

Coverage:
  - task_id validation: task missing → error
  - task_id validation: wrong stage (not 'execute') → error
  - task_id validation: wrong owner → error
  - task_id validation: no active executor role → error
  - push-failure gate: failed push drops task_id from the event so
    the kanban subscriber doesn't auto-advance, and the executor
    role row's completed_at stays NULL
  - happy path: validation passes, push succeeds, task_id rides into
    the event, executor role row is marked completed
  - explicit local-only mode (push='false'): treated as success for
    auto-advance purposes (documented escape hatch)

The test patches `project_configured` + `workspace_dir` and stubs
`subprocess.run` so the validation logic and the publisher's
post-push decision can be exercised without a real git checkout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from server.db import configured_conn, init_db
from server.tools import build_coord_server


def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    h = server["_handlers"].get(f"coord_{name}") or server["_handlers"].get(name)
    if h is None:
        raise KeyError(f"no handler for coord_{name}")
    return h


def _ok_text(result: dict[str, Any]) -> str:
    assert not result.get("isError"), f"unexpected error: {result}"
    return result["content"][0]["text"]


def _err_text(result: dict[str, Any]) -> str:
    assert result.get("isError"), f"expected error, got {result}"
    return result["content"][0]["text"]


async def _seed_task(
    *,
    task_id: str = "t-2026-05-03-abc12345",
    status: str = "execute",
    owner: str | None = "p3",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, complexity, spec_path) "
            "VALUES (?, 'misc', 'demo', ?, ?, 'coach', 'standard', 'x')",
            (task_id, status, owner),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_executor_role(
    *,
    task_id: str = "t-2026-05-03-abc12345",
    owner: str = "p3",
    completed_at: str | None = None,
) -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, completed_at) "
            "VALUES (?, 'executor', '[]', ?, '2026-05-03T00:00:00', ?)",
            (task_id, owner, completed_at),
        )
        await c.commit()
        return cur.lastrowid
    finally:
        await c.close()


@pytest.fixture
def stub_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fake git checkout under the per-slot workspace and stub
    `project_configured` + `workspace_dir` so coord_commit_push proceeds
    past its repo-config / .git checks."""
    import server.tools as tools_mod

    cwd = tmp_path / "p3" / "project"
    (cwd / ".git").mkdir(parents=True)

    monkeypatch.setattr(tools_mod, "project_configured", lambda: True)
    monkeypatch.setattr(tools_mod, "workspace_dir", lambda slot: cwd)
    return cwd


def _stub_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    push_returncode: int = 0,
    has_changes: bool = True,
) -> None:
    """Make `subprocess.run` return canned values for git add / status /
    commit / rev-parse / push so coord_commit_push doesn't hit a real git."""

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if cmd[:2] == ["git", "add"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(
                cmd, 0, "M file.py\n" if has_changes else "", ""
            )
        if cmd[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, "abc123\n", "")
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(
                cmd, push_returncode, "", "remote rejected" if push_returncode else "",
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)


# ---------- entry validation ----------


async def test_commit_push_unknown_task_id_rejected(
    fresh_db: str, stub_workspace: Path
) -> None:
    await init_db()
    server = _server_for("p3")
    result = await _handler(server, "commit_push")({
        "message": "test commit",
        "task_id": "t-2026-05-03-doesnotexist",
    })
    msg = _err_text(result)
    assert "not found in the active project" in msg


async def test_commit_push_wrong_stage_rejected(
    fresh_db: str, stub_workspace: Path
) -> None:
    await init_db()
    await _seed_task(status="audit_syntax")
    await _seed_executor_role()
    server = _server_for("p3")
    result = await _handler(server, "commit_push")({
        "message": "test commit",
        "task_id": "t-2026-05-03-abc12345",
    })
    msg = _err_text(result)
    assert "is in stage 'audit_syntax'" in msg
    assert "not 'execute'" in msg


async def test_commit_push_wrong_owner_rejected(
    fresh_db: str, stub_workspace: Path
) -> None:
    await init_db()
    await _seed_task(owner="p4")  # owned by p4
    await _seed_executor_role(owner="p4")
    server = _server_for("p3")  # p3 calling
    result = await _handler(server, "commit_push")({
        "message": "test commit",
        "task_id": "t-2026-05-03-abc12345",
    })
    msg = _err_text(result)
    assert "owned by p4" in msg
    assert "not p3" in msg


async def test_commit_push_no_active_executor_role_rejected(
    fresh_db: str, stub_workspace: Path
) -> None:
    await init_db()
    await _seed_task(owner="p3")
    # p3 owns the task but has NO active executor role row (already
    # completed prior round, or never created).
    await _seed_executor_role(completed_at="2026-05-03T01:00:00")
    server = _server_for("p3")
    result = await _handler(server, "commit_push")({
        "message": "test commit",
        "task_id": "t-2026-05-03-abc12345",
    })
    msg = _err_text(result)
    assert "no active uncompleted executor role" in msg


# ---------- push-failure gate ----------


async def test_commit_push_failed_push_drops_task_id_from_event(
    fresh_db: str,
    stub_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed push must NOT drive auto-advance: the published
    commit_pushed event has task_id=None and the executor role row
    stays uncompleted so the kanban subscriber can't move the card."""
    from server.events import bus

    await init_db()
    await _seed_task()
    role_id = await _seed_executor_role()

    captured: list[dict[str, Any]] = []
    queue = bus.subscribe()

    _stub_subprocess(monkeypatch, push_returncode=1)

    server = _server_for("p3")
    result = await _handler(server, "commit_push")({
        "message": "test commit",
        "task_id": "t-2026-05-03-abc12345",
    })
    text = _ok_text(result)
    assert "PUSH FAILED" in text

    # Drain bus events.
    while True:
        try:
            captured.append(queue.get_nowait())
        except Exception:
            break

    pushed_events = [e for e in captured if e.get("type") == "commit_pushed"]
    assert len(pushed_events) == 1
    ev = pushed_events[0]
    assert ev.get("pushed") is False
    assert ev.get("task_id") is None  # ← the gate fires here

    # Executor role row stays uncompleted so the subscriber can't
    # see this as "round done".
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments WHERE id = ?",
            (role_id,),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["completed_at"] is None


async def test_commit_push_successful_push_keeps_task_id_and_completes_role(
    fresh_db: str,
    stub_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.events import bus

    await init_db()
    await _seed_task()
    role_id = await _seed_executor_role()

    captured: list[dict[str, Any]] = []
    queue = bus.subscribe()

    _stub_subprocess(monkeypatch, push_returncode=0)

    server = _server_for("p3")
    result = await _handler(server, "commit_push")({
        "message": "test commit",
        "task_id": "t-2026-05-03-abc12345",
    })
    _ok_text(result)

    while True:
        try:
            captured.append(queue.get_nowait())
        except Exception:
            break

    pushed = [e for e in captured if e.get("type") == "commit_pushed"]
    assert len(pushed) == 1
    assert pushed[0].get("pushed") is True
    assert pushed[0].get("task_id") == "t-2026-05-03-abc12345"

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments WHERE id = ?",
            (role_id,),
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["completed_at"] is not None


async def test_commit_push_explicit_local_only_drives_advance(
    fresh_db: str,
    stub_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """push='false' is the documented local-only escape hatch: the
    kanban still advances because the caller explicitly opted in."""
    from server.events import bus

    await init_db()
    await _seed_task()
    await _seed_executor_role()

    captured: list[dict[str, Any]] = []
    queue = bus.subscribe()

    _stub_subprocess(monkeypatch)

    server = _server_for("p3")
    result = await _handler(server, "commit_push")({
        "message": "test commit",
        "task_id": "t-2026-05-03-abc12345",
        "push": "false",
    })
    text = _ok_text(result)
    assert "local only" in text

    while True:
        try:
            captured.append(queue.get_nowait())
        except Exception:
            break

    pushed = [e for e in captured if e.get("type") == "commit_pushed"]
    assert len(pushed) == 1
    assert pushed[0].get("push_requested") is False
    assert pushed[0].get("task_id") == "t-2026-05-03-abc12345"

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
    actor: str = "p2",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO project_events "
            "(project_id, actor, type, task_id, payload_json, payload_pointer) "
            "VALUES (?, ?, 'commit_pushed', ?, '{}', ?)",
            (MISC_PROJECT, actor, task_id, sha),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_ship_event(
    *,
    task_id: str = TASK_ID,
    ship_sha: str = "existingdevsha",
    pr_number: int = 88,
    pr_url: str = "https://github.com/owner/repo/pull/88",
) -> None:
    payload = {
        "type": "task_shipped_to_dev",
        "task_id": task_id,
        "ship_sha": ship_sha,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "executor_sha": EXECUTOR_SHA,
        "deploy_target": "dev",
        "ship_method": "pr",
        "idempotent": False,
    }
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO project_events "
            "(project_id, actor, type, task_id, payload_json, payload_pointer) "
            "VALUES (?, 'p2', 'task_shipped_to_dev', ?, ?, ?)",
            (MISC_PROJECT, task_id, json.dumps(payload), ship_sha),
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


async def _seed_ready_ship_task() -> None:
    await _seed_task()
    await _seed_shipper_role()
    await _seed_auditor_role()
    await _seed_executor_commit()
    await _seed_repo_url()


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
        if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        if cmd[:2] == ["git", "cherry"]:
            return subprocess.CompletedProcess(cmd, 0, f"+ {EXECUTOR_SHA}\n", "")
        if cmd[:4] == ["git", "rev-parse", "--verify", "refs/heads/ship-"]:
            return subprocess.CompletedProcess(cmd, 1, "", "not found")
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(cmd, 1, "", "not found")
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

        async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            # No existing PR
            return _FakeResponse(200, [])

        async def put(self, url: str, **kwargs: Any) -> _FakeResponse:
            # PR merge
            return _FakeResponse(200, {"sha": "aabbccdd11223344556677889900aabbccdd1122"})

        async def delete(self, url: str, **kwargs: Any) -> _FakeResponse:
            # Branch delete
            return _FakeResponse(204, {})

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)


class RecordingGit:
    def __init__(
        self,
        *,
        branch_exists: bool = False,
        current_branch: str = "work/p2",
        dirty: bool = False,
        unmerged: bool = False,
        cherry_pick_head: bool = False,
        patch_on_dev: bool = False,
        empty_cherry_pick: bool = False,
        origin_dev_sha: str = "devsha1234567890",
    ) -> None:
        self.branch_exists = branch_exists
        self.current_branch = current_branch
        self.dirty = dirty
        self.unmerged = unmerged
        self.cherry_pick_head = cherry_pick_head
        self.patch_on_dev = patch_on_dev
        self.empty_cherry_pick = empty_cherry_pick
        self.origin_dev_sha = origin_dev_sha
        self.commands: list[list[str]] = []
        self.patch_checks = 0

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        self.commands.append(cmd)
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd == ["git", "merge-base", "--is-ancestor", EXECUTOR_SHA, "origin/dev"]:
            self.patch_checks += 1
            rc = 0 if self.patch_on_dev else 1
            return subprocess.CompletedProcess(cmd, rc, "", "")
        if cmd == ["git", "cherry", "origin/dev", EXECUTOR_SHA, f"{EXECUTOR_SHA}^"]:
            sign = "-" if self.patch_on_dev else "+"
            return subprocess.CompletedProcess(cmd, 0, f"{sign} {EXECUTOR_SHA}\n", "")
        if cmd == ["git", "rev-parse", "origin/dev"]:
            return subprocess.CompletedProcess(cmd, 0, f"{self.origin_dev_sha}\n", "")
        if cmd == [
            "git",
            "rev-parse",
            "--verify",
            f"refs/heads/ship-{TASK_ID}",
        ]:
            rc = 0 if self.branch_exists else 1
            return subprocess.CompletedProcess(cmd, rc, "", "")
        if cmd == ["git", "branch", "--show-current"]:
            return subprocess.CompletedProcess(cmd, 0, f"{self.current_branch}\n", "")
        if cmd == ["git", "status", "--porcelain=v1"]:
            out = " M server/tools.py\n" if self.dirty else ""
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd == ["git", "diff", "--name-only", "--diff-filter=U"]:
            out = "server/tools.py\n" if self.unmerged else ""
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd == ["git", "rev-parse", "--verify", "CHERRY_PICK_HEAD"]:
            rc = 0 if self.cherry_pick_head else 1
            return subprocess.CompletedProcess(cmd, rc, "", "")
        if cmd == ["git", "checkout", f"ship-{TASK_ID}"]:
            self.current_branch = f"ship-{TASK_ID}"
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd == ["git", "checkout", "-b", f"ship-{TASK_ID}", "origin/dev"]:
            self.branch_exists = True
            self.current_branch = f"ship-{TASK_ID}"
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd == ["git", "cherry-pick", "-x", EXECUTOR_SHA]:
            if self.empty_cherry_pick:
                self.patch_on_dev = True
                return subprocess.CompletedProcess(
                    cmd,
                    1,
                    "",
                    "The previous cherry-pick is now empty, possibly due to conflict resolution.\n",
                )
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd == ["git", "cherry-pick", "--abort"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd == ["git", "push", "origin", f"ship-{TASK_ID}:ship-{TASK_ID}"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd in (
            ["git", "checkout", "work/p2"],
            ["git", "branch", "-D", f"ship-{TASK_ID}"],
        ):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


class RecordingGitHub:
    def __init__(
        self,
        *,
        open_pr: dict[str, Any] | None = None,
        post_status: int = 201,
    ) -> None:
        self.open_pr = open_pr
        self.post_status = post_status
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "RecordingGitHub":
        recorder = self

        class _Response:
            def __init__(self, status_code: int, data: Any) -> None:
                self.status_code = status_code
                self._data = data
                self.text = json.dumps(data)

            def json(self) -> Any:
                return self._data

        class _Client:
            def __init__(self, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *_: Any) -> None:
                pass

            async def get(self, url: str, **kwargs: Any) -> _Response:
                recorder.calls.append(("get", url, kwargs))
                data = [recorder.open_pr] if recorder.open_pr else []
                return _Response(200, data)

            async def post(self, url: str, **kwargs: Any) -> _Response:
                recorder.calls.append(("post", url, kwargs))
                if recorder.post_status == 422:
                    return _Response(422, {"message": "Validation Failed"})
                return _Response(
                    recorder.post_status,
                    {
                        "number": 42,
                        "html_url": "https://github.com/owner/repo/pull/42",
                    },
                )

            async def put(self, url: str, **kwargs: Any) -> _Response:
                recorder.calls.append(("put", url, kwargs))
                return _Response(200, {"sha": "merge1234567890"})

            async def delete(self, url: str, **kwargs: Any) -> _Response:
                recorder.calls.append(("delete", url, kwargs))
                return _Response(204, {})

        monkeypatch.setattr("httpx.AsyncClient", _Client)
        return self


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


async def test_ship_to_dev_happy_path_after_reassigned_executor_push(
    fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit from the reassigned executor should still unblock ship.

    This regression pairs with the commit_push authorization fix: the
    executor commit event may come from a task whose stored tasks.owner
    is stale, but ship still keys off the live commit event and the active
    shipper/audit rows.
    """
    await init_db()
    await _seed_task(owner="p4")
    await _seed_shipper_role()
    await _seed_auditor_role()
    await _seed_executor_commit(actor="p3")
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
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while True:
            try:
                captured.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    shipped = [e for e in captured if e.get("type") == "task_shipped_to_dev"]
    assert shipped, f"no task_shipped_to_dev event: {captured}"
    ev = shipped[0]
    assert ev["task_id"] == TASK_ID
    assert ev["pr_number"] == 42
    assert ev["executor_sha"] == EXECUTOR_SHA


async def test_ship_to_dev_resumes_existing_clean_temp_branch_after_manual_resolution(
    fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    await _seed_ready_ship_task()
    _stub_workspace(monkeypatch, tmp_path)
    git = RecordingGit(branch_exists=True, current_branch=f"ship-{TASK_ID}")
    monkeypatch.setattr(subprocess, "run", git)
    github = RecordingGitHub().install(monkeypatch)

    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("p2")
        result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
        text = _ok_text(result)
        assert "shipped to dev" in text.lower()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while True:
            try:
                captured.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    assert ["git", "checkout", "-b", f"ship-{TASK_ID}", "origin/dev"] not in git.commands
    assert ["git", "cherry-pick", "-x", EXECUTOR_SHA] not in git.commands
    assert ["git", "push", "origin", f"ship-{TASK_ID}:ship-{TASK_ID}"] in git.commands
    assert any(call[0] == "post" for call in github.calls)
    shipped = [e for e in captured if e.get("type") == "task_shipped_to_dev"]
    assert shipped
    assert shipped[0]["ship_method"] == "resumed_pr"
    assert shipped[0]["idempotent"] is True


async def test_ship_to_dev_existing_temp_branch_with_unresolved_conflicts_stays_open(
    fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    await _seed_ready_ship_task()
    _stub_workspace(monkeypatch, tmp_path)
    git = RecordingGit(
        branch_exists=True,
        current_branch=f"ship-{TASK_ID}",
        cherry_pick_head=True,
        unmerged=True,
    )
    monkeypatch.setattr(subprocess, "run", git)
    github = RecordingGitHub().install(monkeypatch)

    server = _server_for("p2")
    result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
    err = _err_text(result)
    assert "unresolved cherry-pick conflicts" in err
    assert ["git", "push", "origin", f"ship-{TASK_ID}:ship-{TASK_ID}"] not in git.commands
    assert github.calls == []

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'shipper'",
            (TASK_ID,),
        )
        assert dict(await cur.fetchone())["completed_at"] is None
    finally:
        await c.close()


async def test_ship_to_dev_checks_out_existing_temp_branch_when_current_worktree_clean(
    fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    await _seed_ready_ship_task()
    _stub_workspace(monkeypatch, tmp_path)
    git = RecordingGit(branch_exists=True, current_branch="work/p2", dirty=False)
    monkeypatch.setattr(subprocess, "run", git)
    RecordingGitHub().install(monkeypatch)

    server = _server_for("p2")
    result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
    _ok_text(result)

    assert ["git", "status", "--porcelain=v1"] in git.commands
    assert ["git", "checkout", f"ship-{TASK_ID}"] in git.commands
    assert ["git", "checkout", "-b", f"ship-{TASK_ID}", "origin/dev"] not in git.commands


async def test_ship_to_dev_already_present_patch_completes_without_pr(
    fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    await _seed_ready_ship_task()
    _stub_workspace(monkeypatch, tmp_path)
    git = RecordingGit(patch_on_dev=True, origin_dev_sha="devheadalready")
    monkeypatch.setattr(subprocess, "run", git)
    github = RecordingGitHub().install(monkeypatch)

    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("p2")
        result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
        text = _ok_text(result)
        assert "already present on dev" in text
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while True:
            try:
                captured.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    assert ["git", "checkout", "-b", f"ship-{TASK_ID}", "origin/dev"] not in git.commands
    assert ["git", "cherry-pick", "-x", EXECUTOR_SHA] not in git.commands
    assert github.calls == []
    shipped = [e for e in captured if e.get("type") == "task_shipped_to_dev"]
    assert shipped
    assert shipped[0]["ship_method"] == "already_present"
    assert shipped[0]["idempotent"] is True
    assert shipped[0]["ship_sha"] == "devheadalready"


async def test_ship_to_dev_empty_cherry_pick_rechecks_already_present_and_completes(
    fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    await _seed_ready_ship_task()
    _stub_workspace(monkeypatch, tmp_path)
    git = RecordingGit(empty_cherry_pick=True, origin_dev_sha="devheadafterempty")
    monkeypatch.setattr(subprocess, "run", git)
    github = RecordingGitHub().install(monkeypatch)

    server = _server_for("p2")
    result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
    text = _ok_text(result)
    assert "already present on dev" in text

    assert ["git", "cherry-pick", "-x", EXECUTOR_SHA] in git.commands
    assert ["git", "cherry-pick", "--abort"] in git.commands
    assert git.patch_checks >= 2
    assert github.calls == []


async def test_ship_to_dev_existing_ship_event_is_idempotent(
    fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    await _seed_ready_ship_task()
    await _seed_ship_event()
    _stub_workspace(monkeypatch, tmp_path)

    def _unexpected_git(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        raise AssertionError(f"git should not run for existing evidence: {cmd}")

    monkeypatch.setattr(subprocess, "run", _unexpected_git)
    github = RecordingGitHub().install(monkeypatch)

    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        server = _server_for("p2")
        result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
        text = _ok_text(result)
        assert "shipped to dev" in text.lower()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        while True:
            try:
                captured.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    assert github.calls == []
    assert not [e for e in captured if e.get("type") == "task_shipped_to_dev"]
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT completed_at FROM task_role_assignments "
            "WHERE task_id = ? AND role = 'shipper'",
            (TASK_ID,),
        )
        assert dict(await cur.fetchone())["completed_at"]
    finally:
        await c.close()


async def test_ship_to_dev_reuses_existing_open_pr(
    fresh_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    await _seed_ready_ship_task()
    _stub_workspace(monkeypatch, tmp_path)
    git = RecordingGit()
    monkeypatch.setattr(subprocess, "run", git)
    existing_pr = {
        "number": 77,
        "html_url": "https://github.com/owner/repo/pull/77",
    }
    github = RecordingGitHub(open_pr=existing_pr).install(monkeypatch)

    server = _server_for("p2")
    result = await _handler(server, "ship_to_dev")({"task_id": TASK_ID})
    text = _ok_text(result)
    assert "#77" in text

    assert any(call[0] == "get" for call in github.calls)
    assert not any(call[0] == "post" for call in github.calls)
    assert any(call[0] == "put" and call[1].endswith("/pulls/77/merge") for call in github.calls)


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

        async def get(self, url: str, **kwargs: Any):
            class _R:
                status_code = 200
                text = "[]"

                def json(self):
                    return []

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

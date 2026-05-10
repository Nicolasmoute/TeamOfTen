"""v0.3.7 — misplaced-work detection in coord_commit_push +
worktree-boundary suffix on executor wakes.

Production trace 2026-05-04: p8 wrote to /workspaces/.project (the
shared seed checkout) instead of /workspaces/p8/project (their own
worktree). `coord_commit_push` ran `git status` inside p8's worktree,
saw a clean tree, returned "nothing to commit (working tree clean)" —
opaque OK that left the work stranded on a tree no branch belonged to.
Two fixes:

  1. coord_commit_push peeks .project when the slot's worktree is
     clean. If .project is dirty, returns a loud named error pointing
     the Player at both paths.
  2. The executor wake hint includes a per-slot worktree-boundary
     suffix naming /workspaces/<slot>/project + the no-edit-.project
     rule.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from server.db import configured_conn, init_db
from server.tools import build_coord_server


# ---------------------------------------------------------------- helpers

def _server_for(slot: str) -> Any:
    return build_coord_server(slot, include_proxy_metadata=True)


def _handler(server: Any, name: str):
    return server["_handlers"].get(f"coord_{name}") or server["_handlers"].get(name)


def _err_text(result: dict[str, Any]) -> str:
    assert result.get("is_error"), f"expected error, got {result}"
    return result["content"][0]["text"]


def _ok_text(result: dict[str, Any]) -> str:
    assert not result.get("is_error"), f"unexpected error: {result}"
    return result["content"][0]["text"]


async def _seed_task_and_role() -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, spec_path) "
            "VALUES ('t-2026-05-06-misplc01', 'misc', 'misplaced work', "
            "'execute', 'p3', 'coach', "
            "'[{\"stage\":\"plan\",\"to\":[]},"
            "{\"stage\":\"execute\",\"to\":[]},"
            "{\"stage\":\"audit_syntax\",\"to\":[]},"
            "{\"stage\":\"audit_semantics\",\"to\":[]},"
            "{\"stage\":\"ship\",\"to\":[]}]', 'x')"
        )
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at) "
            "VALUES ('t-2026-05-06-misplc01', 'executor', '[]', 'p3', "
            "'2026-05-06T00:00:00Z')"
        )
        await c.commit()
    finally:
        await c.close()


@pytest.fixture
def stub_workspaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Fake both the slot worktree and the project's seed checkout.

    Pins paths under tmp_path by monkey-patching `server.paths.DATA_ROOT`
    so `project_paths('misc').worktree('p3')` and `.bare_clone` resolve
    inside tmp_path. `workspace_dir` is async post-refactor; we replace
    it with an async stub that returns the slot worktree directly.
    """
    import server.paths as paths_mod
    import server.tools as tools_mod

    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)

    slot_cwd = tmp_path / "projects" / "misc" / "repo" / "p3"
    (slot_cwd / ".git").mkdir(parents=True)
    base_cwd = tmp_path / "projects" / "misc" / "repo" / ".project"
    (base_cwd / ".git").mkdir(parents=True)

    async def _configured() -> bool:
        return True

    async def _workspace_dir(_slot: str) -> Path:
        return slot_cwd

    monkeypatch.setattr(tools_mod, "project_repo_configured", _configured)
    monkeypatch.setattr(tools_mod, "workspace_dir", _workspace_dir)
    return {"slot": slot_cwd, "base": base_cwd}


def _stub_subprocess_with_per_cwd_status(
    monkeypatch: pytest.MonkeyPatch,
    *,
    slot_dirty: bool,
    base_dirty: bool,
    slot_cwd: Path,
    base_cwd: Path,
) -> None:
    """`git status --porcelain` returns dirty/clean per-cwd. Other git
    commands succeed silently. Push always succeeds (we don't reach it
    in the misplaced-work path)."""
    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        cwd = kwargs.get("cwd") or ""
        if cmd[:2] == ["git", "add"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "status"]:
            if str(slot_cwd) == cwd:
                return subprocess.CompletedProcess(
                    cmd, 0, "M file.py\n" if slot_dirty else "", ""
                )
            if str(base_cwd) == cwd:
                return subprocess.CompletedProcess(
                    cmd, 0, "M wrong.py\n" if base_dirty else "", ""
                )
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(cmd, 0, "abc123\n", "")
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", _fake_run)


# ---------------------------------------------------------------- tests

async def test_clean_slot_clean_base_returns_soft_ok(
    fresh_db: str, stub_workspaces: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No changes anywhere — the original soft-OK behavior is
    preserved (some commits are legitimately no-ops)."""
    await init_db()
    await _seed_task_and_role()
    _stub_subprocess_with_per_cwd_status(
        monkeypatch,
        slot_dirty=False,
        base_dirty=False,
        slot_cwd=stub_workspaces["slot"],
        base_cwd=stub_workspaces["base"],
    )
    server = _server_for("p3")
    result = await _handler(server, "commit_push")({
        "message": "x",
        "task_id": "t-2026-05-06-misplc01",
    })
    text = _ok_text(result)
    assert "nothing to commit" in text


async def test_clean_slot_dirty_base_returns_loud_misplaced_error(
    fresh_db: str, stub_workspaces: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact production trace: slot worktree clean, .project dirty.
    Must surface a loud error naming both paths and the fix path —
    NOT the opaque 'nothing to commit' soft-OK."""
    await init_db()
    await _seed_task_and_role()
    _stub_subprocess_with_per_cwd_status(
        monkeypatch,
        slot_dirty=False,
        base_dirty=True,
        slot_cwd=stub_workspaces["slot"],
        base_cwd=stub_workspaces["base"],
    )
    server = _server_for("p3")
    result = await _handler(server, "commit_push")({
        "message": "x",
        "task_id": "t-2026-05-06-misplc01",
    })
    msg = _err_text(result)
    # Names both paths so the Player can see exactly what's wrong.
    assert str(stub_workspaces["slot"]) in msg
    assert str(stub_workspaces["base"]) in msg
    # Names the fix.
    assert "per-worktree isolation" in msg.lower() or "isolation" in msg.lower()
    # Names the path forward (move changes / re-apply).
    assert "Move" in msg or "move" in msg


async def test_dirty_slot_skips_base_check(
    fresh_db: str, stub_workspaces: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The base-peek only fires when the slot worktree is clean. A
    dirty slot worktree commits normally even if the base happens to
    have stale changes from a prior incident."""
    await init_db()
    await _seed_task_and_role()
    _stub_subprocess_with_per_cwd_status(
        monkeypatch,
        slot_dirty=True,
        base_dirty=True,
        slot_cwd=stub_workspaces["slot"],
        base_cwd=stub_workspaces["base"],
    )
    server = _server_for("p3")
    result = await _handler(server, "commit_push")({
        "message": "fix the thing",
        "task_id": "t-2026-05-06-misplc01",
    })
    text = _ok_text(result)
    # Normal happy-path message — committed and pushed.
    assert "abc123" in text or "committed" in text.lower() or "pushed" in text.lower()


async def test_clean_slot_no_base_repo_falls_back_to_soft_ok(
    fresh_db: str, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the project's seed checkout has no `.git` (e.g. an
    unprovisioned project), the clean-tree case falls back to the
    legacy soft-OK rather than crashing."""
    import server.paths as paths_mod
    import server.tools as tools_mod

    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)
    slot_cwd = tmp_path / "projects" / "misc" / "repo" / "p3"
    (slot_cwd / ".git").mkdir(parents=True)
    base_cwd = tmp_path / "projects" / "misc" / "repo" / ".project"
    base_cwd.mkdir(parents=True)  # no .git inside

    async def _configured() -> bool:
        return True

    async def _workspace_dir(_slot: str) -> Path:
        return slot_cwd

    monkeypatch.setattr(tools_mod, "project_repo_configured", _configured)
    monkeypatch.setattr(tools_mod, "workspace_dir", _workspace_dir)

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        if cmd[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", _fake_run)

    await init_db()
    await _seed_task_and_role()
    server = _server_for("p3")
    result = await _handler(server, "commit_push")({
        "message": "x",
        "task_id": "t-2026-05-06-misplc01",
    })
    text = _ok_text(result)
    assert "nothing to commit" in text


# ---------------------------------------------------------------- wake-prompt

async def test_executor_wake_includes_worktree_boundary(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard-assigned executor wakes carry a per-slot worktree-boundary
    suffix naming /workspaces/<slot>/project + the no-.project rule."""
    from server.kanban import _wake_role_or_emit_needed
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, spec_path) "
            "VALUES ('t-2026-05-06-misplc02', 'misc', 'wt boundary', "
            "'execute', 'p8', 'coach', "
            "'[{\"stage\":\"plan\",\"to\":[]},"
            "{\"stage\":\"execute\",\"to\":[]}]', 'x')"
        )
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, claimed_at) "
            "VALUES ('t-2026-05-06-misplc02', 'executor', '[]', 'p8', "
            "'2026-05-06T00:00:00Z', '2026-05-06T00:00:00Z')"
        )
        await c.commit()
    finally:
        await c.close()

    captured: list[tuple[str, str]] = []

    async def _stub_wake(slot: str, prompt: str, **kw: Any) -> bool:
        captured.append((slot, prompt))
        return True

    import server.agents as agents_mod
    orig = agents_mod.maybe_wake_agent
    agents_mod.maybe_wake_agent = _stub_wake
    try:
        await _wake_role_or_emit_needed(
            task_id="t-2026-05-06-misplc02", role="executor"
        )
    finally:
        agents_mod.maybe_wake_agent = orig

    p8_wakes = [body for slot, body in captured if slot == "p8"]
    assert p8_wakes, captured
    body = p8_wakes[0]
    # Per-project paths under the active project (misc by default).
    body_norm = body.replace("\\", "/")
    assert "/projects/misc/repo/p8" in body_norm
    assert "/projects/misc/repo/.project" in body_norm
    assert "Worktree boundary" in body
    assert "do NOT edit" in body.lower() or "do not edit" in body.lower()


async def test_non_executor_wake_omits_worktree_boundary(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auditors / planners / shippers don't edit code — the worktree
    boundary suffix is executor-only to avoid prompt bloat."""
    from server.kanban import _wake_role_or_emit_needed
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, spec_path) "
            "VALUES ('t-2026-05-06-misplc03', 'misc', 'audit-no-wt', "
            "'audit_syntax', 'p2', 'coach', "
            "'[{\"stage\":\"execute\",\"to\":[]},"
            "{\"stage\":\"audit_syntax\",\"to\":[]}]', 'x')"
        )
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, claimed_at) "
            "VALUES ('t-2026-05-06-misplc03', 'auditor_syntax', '[]', "
            "'p4', '2026-05-06T00:00:00Z', '2026-05-06T00:00:00Z')"
        )
        await c.commit()
    finally:
        await c.close()

    captured: list[tuple[str, str]] = []

    async def _stub_wake(slot: str, prompt: str, **kw: Any) -> bool:
        captured.append((slot, prompt))
        return True

    import server.agents as agents_mod
    orig = agents_mod.maybe_wake_agent
    agents_mod.maybe_wake_agent = _stub_wake
    try:
        await _wake_role_or_emit_needed(
            task_id="t-2026-05-06-misplc03", role="auditor_syntax"
        )
    finally:
        agents_mod.maybe_wake_agent = orig

    p4_wakes = [body for slot, body in captured if slot == "p4"]
    assert p4_wakes, captured
    body = p4_wakes[0]
    # Auditor wake prompt does NOT carry the worktree boundary.
    assert "Worktree boundary" not in body


@pytest.mark.usefixtures("fresh_db")
async def test_executor_worktree_boundary_helper_returns_empty_for_non_executor() -> None:
    from server.kanban import _executor_worktree_boundary
    await init_db()
    assert await _executor_worktree_boundary("auditor_syntax", "p3") == ""
    assert await _executor_worktree_boundary("planner", "p3") == ""
    assert await _executor_worktree_boundary("shipper", "p3") == ""
    assert await _executor_worktree_boundary("executor", "") == ""
    out = await _executor_worktree_boundary("executor", "p7")
    # Per-project path, resolved against the active project (misc by
    # default in init_db). Names both the slot worktree and the seed
    # checkout so the executor can navigate either way.
    assert "/projects/misc/repo/p7" in out.replace("\\", "/")
    assert "/projects/misc/repo/.project" in out.replace("\\", "/")

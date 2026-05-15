"""Tests for the _ensure_worktree branch-verification fix (t-2026-05-15-45716da0).

Root cause: _ensure_worktree() returned ok=True when .git existed without
checking whether the worktree was on the correct branch. A slot provisioned
with the wrong branch (e.g. work/p5 on p6's worktree) would then silently
push to the wrong remote branch via coord_commit_push.

Three scenarios:
1. Correct branch — passes through with no git checkout invoked.
2. Wrong branch, checkout succeeds — branch corrected, workspace_branch_mismatch
   event emitted.
3. Wrong branch, checkout fails — ok=False with descriptive error.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

import server.workspaces as wmod
from server.workspaces import _ensure_worktree


# ---------------------------------------------------------------------------
# Fixture helper — standalone git repo
# ---------------------------------------------------------------------------


def _make_git_repo(path: Path, branch: str) -> None:
    """Create a standalone git repo at `path` on `branch` with one commit.

    Using a standalone repo (not a linked worktree) avoids the parent-
    checkout persistence issue: linked worktrees write a `.git` *file*
    pointing at the parent's gitdir, which becomes stale once the parent's
    temp dir is removed.  A standalone `git init` gives a real `.git/` dir
    that `_ensure_worktree`'s `(worktree / ".git").exists()` check accepts.
    """
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test.com",
    }
    subprocess.run(["git", "init", "-b", branch, str(path)],
                   check=True, capture_output=True, env=env)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=path, check=True, capture_output=True, env=env)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=path, check=True, capture_output=True, env=env)
    (path / "readme.txt").write_text("init")
    subprocess.run(["git", "add", "."],
                   cwd=path, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=path, check=True, capture_output=True, env=env)


# ---------------------------------------------------------------------------
# 1. Correct branch — no checkout invoked
# ---------------------------------------------------------------------------


async def test_correct_branch_passes_unchanged(tmp_path: Path) -> None:
    """Worktree already on the correct branch; _ensure_worktree returns
    ok=True and does NOT call git checkout."""
    bare = tmp_path / "bare"  # _ensure_worktree requires a bare path arg
    bare.mkdir()
    worktree = tmp_path / "repo" / "p3"
    _make_git_repo(worktree, "work/p3")

    checkout_calls: list[list[str]] = []
    original_run = wmod._run

    async def spy_run(cmd: list[str], cwd=None, timeout: int = 120):
        if "checkout" in cmd:
            checkout_calls.append(list(cmd))
        return await original_run(cmd, cwd=cwd, timeout=timeout)

    with patch.object(wmod, "_run", side_effect=spy_run):
        result = await _ensure_worktree(bare=bare, worktree=worktree, slot="p3")

    assert result["ok"] is True
    assert result["status"] == "already-present"
    assert checkout_calls == [], f"unexpected checkout: {checkout_calls}"


# ---------------------------------------------------------------------------
# 2. Wrong branch, checkout succeeds — corrected + event emitted
# ---------------------------------------------------------------------------


async def test_wrong_branch_triggers_checkout_and_event(tmp_path: Path) -> None:
    """Worktree .git exists but is on work/p5 instead of work/p3.
    _ensure_worktree runs git checkout to fix it and emits
    workspace_branch_mismatch with corrected=True."""
    bare = tmp_path / "bare"
    bare.mkdir()
    worktree = tmp_path / "repo" / "p3"
    # Deliberately provision on the WRONG branch (work/p5)
    _make_git_repo(worktree, "work/p5")
    # Create the correct branch locally so checkout can switch to it
    subprocess.run(["git", "branch", "work/p3"],
                   cwd=worktree, check=True, capture_output=True)

    published: list[dict] = []

    async def fake_publish(payload: dict) -> None:
        published.append(payload)

    # bus is lazily imported inside _ensure_worktree; patch at its source
    with patch("server.events.bus.publish", new=fake_publish):
        result = await _ensure_worktree(bare=bare, worktree=worktree, slot="p3")

    assert result["ok"] is True, f"expected ok=True: {result}"

    # Verify the worktree is now on work/p3
    proc = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=worktree, capture_output=True, text=True,
    )
    assert proc.stdout.strip() == "work/p3"

    # workspace_branch_mismatch event must have been published
    mismatch_events = [e for e in published if e.get("type") == "workspace_branch_mismatch"]
    assert len(mismatch_events) == 1, f"expected 1 event, got: {published}"
    ev = mismatch_events[0]
    assert ev["slot"] == "p3"
    assert ev["actual_branch"] == "work/p5"
    assert ev["expected_branch"] == "work/p3"
    assert ev["corrected"] is True


# ---------------------------------------------------------------------------
# 3. Wrong branch, checkout fails — ok=False with descriptive error
# ---------------------------------------------------------------------------


async def test_wrong_branch_checkout_fails_returns_error(tmp_path: Path) -> None:
    """Worktree on wrong branch AND checkout fails (simulated dirty tree or
    missing branch). _ensure_worktree returns ok=False with a descriptive
    error string mentioning both the actual and expected branch names."""
    bare = tmp_path / "bare"
    bare.mkdir()
    worktree = tmp_path / "repo" / "p3"
    _make_git_repo(worktree, "work/p5")  # wrong branch

    original_run = wmod._run

    async def patched_run(cmd: list[str], cwd=None, timeout: int = 120):
        # Fail only the checkout command; let other git calls pass
        if "checkout" in cmd:
            return 1, "", "error: Your local changes would be overwritten by checkout."
        return await original_run(cmd, cwd=cwd, timeout=timeout)

    with patch.object(wmod, "_run", side_effect=patched_run):
        result = await _ensure_worktree(bare=bare, worktree=worktree, slot="p3")

    assert result["ok"] is False
    assert "wrong branch" in result["error"]
    assert "work/p5" in result["error"]
    assert "work/p3" in result["error"]

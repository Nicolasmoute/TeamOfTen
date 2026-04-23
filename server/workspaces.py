"""Per-slot git worktrees.

When HARNESS_PROJECT_REPO is set, on startup we clone it once into
/workspaces/.project (the bare-ish base) and create a git worktree at
/workspaces/<slot>/project for each slot on branch work/<slot>. Each
agent's cwd is then its own worktree — file edits and commits are
isolated per Player by construction, no merging while in flight.

When HARNESS_PROJECT_REPO is NOT set, no clone happens and agent cwd
stays at the plain dir /workspaces/<slot>/. The harness still works
end-to-end; agents just don't have a project to operate on.

Idempotent: ensure_workspaces() is safe to call any number of times —
existing clones aren't re-cloned, existing worktrees aren't recreated.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("harness.workspaces")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


PROJECT_REPO = os.environ.get("HARNESS_PROJECT_REPO", "").strip()
PROJECT_BRANCH = os.environ.get("HARNESS_PROJECT_BRANCH", "main").strip() or "main"
WORKSPACES_ROOT = Path(os.environ.get("HARNESS_WORKSPACES_ROOT", "/workspaces"))
BASE_REPO_PATH = WORKSPACES_ROOT / ".project"

SLOT_IDS: list[str] = ["coach"] + [f"p{i}" for i in range(1, 11)]


def project_configured() -> bool:
    return bool(PROJECT_REPO)


def workspace_dir(slot: str) -> Path:
    """The cwd an agent should run in.

    Worktree path when project is configured; plain workspace dir otherwise.
    """
    base = WORKSPACES_ROOT / slot
    if project_configured():
        return base / "project"
    return base


def get_status() -> dict[str, object]:
    """Snapshot of workspace state for /api/status. Cheap — just stats."""
    if not project_configured():
        return {"configured": False, "reason": "HARNESS_PROJECT_REPO not set"}
    slot_state = {}
    for s in SLOT_IDS:
        wt = workspace_dir(s)
        slot_state[s] = {
            "path": str(wt),
            "exists": wt.exists(),
            "is_git": (wt / ".git").exists(),
        }
    return {
        "configured": True,
        "repo": PROJECT_REPO,
        "branch": PROJECT_BRANCH,
        "base_cloned": BASE_REPO_PATH.exists(),
        "slots": slot_state,
    }


async def ensure_workspaces() -> dict[str, object]:
    """Idempotent setup. Called once at startup. Returns status dict.

    Even when no project repo is configured we still need a real cwd
    for each slot — the Claude Agent SDK passes cwd to subprocess,
    which ENOENTs before it can even print a useful error when the
    path is missing. So mkdir the plain dirs unconditionally.
    """
    for slot in SLOT_IDS:
        try:
            (WORKSPACES_ROOT / slot).mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("workspaces: mkdir failed for %s", slot)

    if not project_configured():
        logger.info(
            "workspaces: HARNESS_PROJECT_REPO unset; created plain /workspaces/<slot>/ dirs"
        )
        return {"configured": False}

    status: dict[str, object] = {
        "configured": True,
        "repo": PROJECT_REPO,
        "branch": PROJECT_BRANCH,
        "slots": {},
    }

    try:
        await _ensure_base_clone()
    except Exception as e:
        logger.exception("base clone failed")
        status["error"] = f"clone failed: {e}"
        return status

    slot_results: dict[str, object] = {}
    for slot in SLOT_IDS:
        try:
            slot_results[slot] = await _ensure_worktree(slot)
        except Exception as e:
            logger.exception("worktree for %s failed", slot)
            slot_results[slot] = {"ok": False, "error": str(e)}
    status["slots"] = slot_results
    return status


# ------------------------------------------------------------------
# internals
# ------------------------------------------------------------------


async def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str, str]:
    """Run a subprocess in a worker thread; return (code, stdout, stderr)."""
    def _do() -> tuple[int, str, str]:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    return await asyncio.to_thread(_do)


async def _ensure_base_clone() -> None:
    if BASE_REPO_PATH.exists() and (BASE_REPO_PATH / ".git").exists():
        logger.info("base repo already present at %s", BASE_REPO_PATH)
        return
    BASE_REPO_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.info("cloning %s (branch %s) → %s", PROJECT_REPO, PROJECT_BRANCH, BASE_REPO_PATH)
    code, out, err = await _run(
        ["git", "clone", "--branch", PROJECT_BRANCH, PROJECT_REPO, str(BASE_REPO_PATH)],
        timeout=300,
    )
    if code != 0:
        raise RuntimeError(
            f"git clone exited {code}: {err.strip() or out.strip()}"
        )
    logger.info("clone ok")


async def _ensure_worktree(slot: str) -> dict[str, object]:
    worktree_path = WORKSPACES_ROOT / slot / "project"
    branch_name = f"work/{slot}"

    if (worktree_path / ".git").exists():
        return {
            "ok": True,
            "path": str(worktree_path),
            "branch": branch_name,
            "status": "already-present",
        }

    if worktree_path.exists():
        contents = [p for p in worktree_path.iterdir()] if worktree_path.is_dir() else []
        if contents:
            return {
                "ok": False,
                "error": f"path {worktree_path} exists and is non-empty",
            }

    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Branch resolution priority:
    #   1. Local branch already exists → reuse it (post-redeploy where the
    #      base repo wasn't wiped).
    #   2. Remote branch origin/<branch> exists → create a local tracking
    #      branch from it. Critical: a fresh clone has the remote ref but
    #      no local branch yet; without this step we'd lose committed agent
    #      work after a base-repo wipe + re-clone.
    #   3. Neither → create a brand-new branch off PROJECT_BRANCH.
    code, out, _ = await _run(
        ["git", "branch", "--list", branch_name],
        cwd=BASE_REPO_PATH,
    )
    local_exists = bool(out.strip())

    remote_exists = False
    if not local_exists:
        code, out, _ = await _run(
            ["git", "branch", "-r", "--list", f"origin/{branch_name}"],
            cwd=BASE_REPO_PATH,
        )
        remote_exists = bool(out.strip())

    if local_exists:
        cmd = ["git", "worktree", "add", str(worktree_path), branch_name]
        source = "local-branch"
    elif remote_exists:
        cmd = [
            "git", "worktree", "add",
            str(worktree_path),
            "-b", branch_name,
            f"origin/{branch_name}",
        ]
        source = "origin-tracking"
    else:
        cmd = [
            "git", "worktree", "add",
            "-b", branch_name,
            str(worktree_path),
            PROJECT_BRANCH,
        ]
        source = "new-from-base"

    code, _, err = await _run(cmd, cwd=BASE_REPO_PATH)
    if code != 0:
        return {
            "ok": False,
            "error": f"git worktree add failed (code {code}): {err.strip()}",
        }

    return {
        "ok": True,
        "path": str(worktree_path),
        "branch": branch_name,
        "source": source,
        "status": "created",
    }

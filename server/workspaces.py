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


WORKSPACES_ROOT = Path(os.environ.get("HARNESS_WORKSPACES_ROOT", "/workspaces"))
BASE_REPO_PATH = WORKSPACES_ROOT / ".project"

SLOT_IDS: list[str] = ["coach"] + [f"p{i}" for i in range(1, 11)]

# Project repo + branch are resolved DB-first, env-fallback. The DB
# value (set via Options → Project repo) persists across redeploys;
# the env var (HARNESS_PROJECT_REPO / HARNESS_PROJECT_BRANCH) is the
# initial bootstrap path. Both are read once per process; changing
# the DB value requires a redeploy to take effect (existing worktrees
# keep their old `git remote`).
_CACHED_REPO: str | None = None
_CACHED_BRANCH: str | None = None


def _read_team_config_sync(key: str) -> str:
    """Synchronous team_config read — used at process startup before
    the asyncio loop is guaranteed up. Returns "" on any error /
    missing row so env fallback kicks in."""
    try:
        import json
        import sqlite3
        from server.db import DB_PATH
        conn = sqlite3.connect(DB_PATH, timeout=2.0)
        try:
            cur = conn.execute(
                "SELECT value FROM team_config WHERE key = ?", (key,)
            )
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        return ""
    if not row:
        return ""
    raw = (row[0] or "").strip()
    if raw.startswith('"') and raw.endswith('"'):
        try:
            v = json.loads(raw)
            if isinstance(v, str):
                return v
        except Exception:
            pass
    return raw


def _project_repo() -> str:
    global _CACHED_REPO
    if _CACHED_REPO is not None:
        return _CACHED_REPO
    db_val = _read_team_config_sync("project_repo")
    env_val = os.environ.get("HARNESS_PROJECT_REPO", "").strip()
    _CACHED_REPO = db_val or env_val or ""
    return _CACHED_REPO


def _project_branch() -> str:
    global _CACHED_BRANCH
    if _CACHED_BRANCH is not None:
        return _CACHED_BRANCH
    db_val = _read_team_config_sync("project_branch")
    env_val = os.environ.get("HARNESS_PROJECT_BRANCH", "").strip()
    _CACHED_BRANCH = db_val or env_val or "main"
    return _CACHED_BRANCH


def project_configured() -> bool:
    return bool(_project_repo())


def refresh_repo_cache() -> None:
    """Clear the in-process repo/branch cache so the next
    project_configured() / _project_repo() / _project_branch() call
    re-reads from DB. Called by PUT /api/team/repo after a save so
    downstream code reflects the new value (the clone itself still
    needs a container restart — see set_team_repo docstring)."""
    global _CACHED_REPO, _CACHED_BRANCH
    _CACHED_REPO = None
    _CACHED_BRANCH = None


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
        return {"configured": False, "reason": "no project repo set (Options → Project repo or HARNESS_PROJECT_REPO)"}
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
        "repo": _project_repo(),
        "branch": _project_branch(),
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
            "workspaces: no project repo set; created plain /workspaces/<slot>/ dirs"
        )
        return {"configured": False}

    status: dict[str, object] = {
        "configured": True,
        "repo": _project_repo(),
        "branch": _project_branch(),
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
    repo = _project_repo()
    branch = _project_branch()
    logger.info("cloning %s (branch %s) → %s", repo, branch, BASE_REPO_PATH)
    code, out, err = await _run(
        ["git", "clone", "--branch", branch, repo, str(BASE_REPO_PATH)],
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
            _project_branch(),
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

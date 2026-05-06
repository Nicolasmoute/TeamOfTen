"""Per-project, per-slot git worktrees.

Layout (canonical — see Docs/TOT-specs.md §4.6 + §17):

    /data/projects/<id>/repo/.project   # bare-ish seed clone, one per project
    /data/projects/<id>/repo/<slot>     # per-slot worktree on branch work/<slot>

`workspace_dir(slot)` is async and resolves through the active
project — pure function of (active_project_id, slot). No fallback
to a plain dir; provisioning is expected to have run before any
agent uses the path.

`ensure_workspaces(project_id)` is the only provisioner. Reads
`projects.repo_url` for the given project; clones if absent;
creates per-slot worktrees if absent. Idempotent. Called at boot
for the active project, on every project switch (as the
`provision_workspaces` step in `_run_switch`), and via the manual
backstop `POST /api/projects/{id}/repo/provision`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from server.paths import ensure_project_scaffold, project_paths

logger = logging.getLogger("harness.workspaces")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


SLOT_IDS: list[str] = ["coach"] + [f"p{i}" for i in range(1, 11)]


_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_placeholders(value: str) -> str:
    """Expand `${VAR}` placeholders in a string. Resolution order
    matches mcp_config: encrypted secrets store first, os.environ
    fallback, empty string + warning if neither has it. Used to
    interpolate PATs into the project repo URL before handing it
    to git, so the DB never stores the raw token."""
    if not value or "${" not in value:
        return value
    try:
        from server import secrets as secrets_store
    except Exception:
        secrets_store = None  # type: ignore[assignment]

    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        v_secret = None
        if secrets_store is not None:
            try:
                v_secret = secrets_store.lookup_sync(name)
            except Exception:
                v_secret = None
        v_env = os.environ.get(name)
        if v_secret is not None:
            return v_secret
        if v_env is not None:
            return v_env
        logger.warning(
            "workspaces: ${%s} referenced but not set anywhere "
            "(expanded to empty)",
            name,
        )
        return ""

    return _ENV_VAR_RE.sub(sub, value)


def _mask_userinfo(text: str, *expanded_urls: str) -> str:
    """Scrub the userinfo portion of any expanded URL from `text` so
    raw tokens don't leak into logs / API error responses."""
    out = text
    for url in expanded_urls:
        if not url:
            continue
        m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://([^@/\s]+)@", url)
        if not m:
            continue
        userinfo = m.group(1)
        if userinfo and userinfo not in ("***",):
            out = out.replace(userinfo, "***")
    return out


async def _read_project_repo_url(project_id: str) -> str:
    """Load `projects.repo_url` for the given project id. Returns ""
    on missing project or empty URL — callers treat both as "no repo
    configured" and skip cloning."""
    from server.db import configured_conn
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT repo_url FROM projects WHERE id = ?", (project_id,)
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return ""
    try:
        v = row[0]
    except Exception:
        v = None
    return (v or "").strip()


async def workspace_dir(slot: str) -> Path:
    """The cwd an agent should run in.

    Pure function of (active_project, slot). The path is
    `/data/projects/<active>/repo/<slot>`. Does not stat the
    filesystem — provisioning is expected to have run. If the
    worktree is missing when an agent wakes, the subprocess will
    fail at chdir with a clear error and the operator can run the
    manual provision endpoint.
    """
    from server.db import resolve_active_project
    project_id = await resolve_active_project()
    return project_paths(project_id).worktree(slot)


async def project_repo_configured(project_id: str | None = None) -> bool:
    """True iff the given project has a non-empty repo_url. Defaults
    to the active project when no id is passed."""
    if project_id is None:
        from server.db import resolve_active_project
        project_id = await resolve_active_project()
    return bool(await _read_project_repo_url(project_id))


async def get_status(project_id: str | None = None) -> dict[str, Any]:
    """Snapshot of workspace state for /api/status + /api/health.

    The repo URL is masked before returning — the raw value can
    carry a PAT (from `${GITHUB_TOKEN}` placeholder expansion) and
    must never leave the harness server.
    """
    if project_id is None:
        from server.db import resolve_active_project
        project_id = await resolve_active_project()
    pp = project_paths(project_id)
    repo_url = await _read_project_repo_url(project_id)
    if not repo_url:
        return {
            "configured": False,
            "project_id": project_id,
            "reason": (
                f"project '{project_id}' has no repo_url "
                "(set via Options → Projects → edit)"
            ),
        }
    slot_state: dict[str, dict[str, Any]] = {}
    for s in SLOT_IDS:
        worktree = pp.worktree(s)
        slot_state[s] = {
            "path": str(worktree),
            "exists": worktree.exists(),
            "is_git": (worktree / ".git").exists(),
        }
    from server.main import _mask_repo_url
    return {
        "configured": True,
        "project_id": project_id,
        "repo_masked": _mask_repo_url(repo_url),
        "bare_cloned": (pp.bare_clone / ".git").exists()
                       or pp.bare_clone.exists(),
        "slots": slot_state,
    }


async def ensure_workspaces(project_id: str) -> dict[str, Any]:
    """Idempotent provisioner for one project.

    Reads `projects.repo_url`, clones if absent, creates per-slot
    worktrees if absent. Safe to call any number of times — existing
    clones aren't re-cloned, existing worktrees aren't recreated.

    Returns a status dict; callers in the project-switch flow
    surface the {configured, slots: {<slot>: {ok, ...}}} shape via
    the `provision_workspaces` event step.
    """
    pp = ensure_project_scaffold(project_id)
    repo_url = await _read_project_repo_url(project_id)
    if not repo_url:
        logger.info(
            "workspaces: no repo_url for project '%s'; skipped clone",
            project_id,
        )
        return {
            "configured": False,
            "project_id": project_id,
            "reason": "projects.repo_url is empty",
        }

    from server.main import _mask_repo_url
    status: dict[str, Any] = {
        "configured": True,
        "project_id": project_id,
        "repo_masked": _mask_repo_url(repo_url),
        "slots": {},
    }

    try:
        await _ensure_base_clone(pp.bare_clone, repo_url)
    except Exception as e:
        logger.exception("base clone failed for project %s", project_id)
        status["error"] = f"clone failed: {e}"
        return status

    slot_results: dict[str, Any] = {}
    for slot in SLOT_IDS:
        try:
            slot_results[slot] = await _ensure_worktree(
                bare=pp.bare_clone,
                worktree=pp.worktree(slot),
                slot=slot,
            )
        except Exception as e:
            logger.exception(
                "worktree for %s failed (project %s)", slot, project_id
            )
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


async def _ensure_base_clone(bare: Path, repo_url_raw: str) -> None:
    """Idempotent clone of the project repo into its bare-clone path."""
    if bare.exists() and (bare / ".git").exists():
        logger.info("base repo already present at %s", bare)
        return
    bare.parent.mkdir(parents=True, exist_ok=True)
    repo = _expand_placeholders(repo_url_raw)
    # Log the unexpanded form so PATs don't hit the logs.
    logger.info("cloning %s → %s", repo_url_raw, bare)
    code, out, err = await _run(
        ["git", "clone", repo, str(bare)],
        timeout=300,
    )
    if code != 0:
        msg = (err.strip() or out.strip())
        raise RuntimeError(
            f"git clone exited {code}: {_mask_userinfo(msg, repo)}"
        )
    logger.info("clone ok: %s", bare)


async def _ensure_worktree(
    *, bare: Path, worktree: Path, slot: str,
) -> dict[str, Any]:
    branch_name = f"work/{slot}"

    if (worktree / ".git").exists():
        return {
            "ok": True,
            "path": str(worktree),
            "branch": branch_name,
            "status": "already-present",
        }

    if worktree.exists():
        contents = [p for p in worktree.iterdir()] if worktree.is_dir() else []
        if contents:
            return {
                "ok": False,
                "error": f"path {worktree} exists and is non-empty",
            }

    worktree.parent.mkdir(parents=True, exist_ok=True)

    # Branch resolution priority:
    #   1. Local branch already exists → reuse it (post-redeploy where
    #      the bare clone wasn't wiped).
    #   2. Remote branch origin/<branch> exists → create a local
    #      tracking branch from it. Critical: a fresh clone has the
    #      remote ref but no local branch yet; without this step we'd
    #      lose committed agent work after a base-repo wipe + re-clone.
    #   3. Neither → create a brand-new branch off the upstream default.
    code, out, _ = await _run(
        ["git", "branch", "--list", branch_name],
        cwd=bare,
    )
    local_exists = bool(out.strip())

    remote_exists = False
    if not local_exists:
        code, out, _ = await _run(
            ["git", "branch", "-r", "--list", f"origin/{branch_name}"],
            cwd=bare,
        )
        remote_exists = bool(out.strip())

    if local_exists:
        cmd = ["git", "worktree", "add", str(worktree), branch_name]
        source = "local-branch"
    elif remote_exists:
        cmd = [
            "git", "worktree", "add",
            str(worktree),
            "-b", branch_name,
            f"origin/{branch_name}",
        ]
        source = "origin-tracking"
    else:
        # Fresh worktree off the bare clone's HEAD (whatever the
        # upstream default branch is — typically `main`). We do NOT
        # hardcode `main` here; the `git worktree add -b` form takes
        # the start point as the next argument and `HEAD` works
        # regardless of branch name.
        cmd = [
            "git", "worktree", "add",
            "-b", branch_name,
            str(worktree),
            "HEAD",
        ]
        source = "new-from-base"

    code, _, err = await _run(cmd, cwd=bare)
    if code != 0:
        return {
            "ok": False,
            "error": f"git worktree add failed (code {code}): {err.strip()}",
        }

    return {
        "ok": True,
        "path": str(worktree),
        "branch": branch_name,
        "source": source,
        "status": "created",
    }

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
from datetime import datetime, timezone
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    Resolves to `/data/projects/<active>/repo/<slot>` against the
    active project. Creates the directory if it doesn't exist —
    `ensure_workspaces` is the canonical provisioner (called at
    boot, project switch, project create, and repo_url update),
    but this self-heal keeps an agent's chdir from crashing if the
    dir somehow went missing (transient FS error, mid-migration
    deploy). The cwd needs to *exist* for the SDK to chdir;
    whether it's a git checkout is a separate concern that
    `coord_commit_push` checks loudly.

    Raises `RuntimeError` when the mkdir itself fails (disk full,
    permission denied, parent path is a file, etc.) — the calling
    spawn path needs a real error message in the event log, not a
    chdir crash to a phantom path.
    """
    from server.db import resolve_active_project
    project_id = await resolve_active_project()
    p = project_paths(project_id).worktree(slot)
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.exception(
                "workspace_dir: mkdir failed for %s/%s", project_id, slot,
            )
            raise RuntimeError(
                f"workspace_dir mkdir failed for {project_id}/{slot} "
                f"at {p}: {type(e).__name__}: {e}"
            ) from e
    return p


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

    Always creates plain per-slot directories under
    `/data/projects/<id>/repo/<slot>` so agent cwds exist even when
    the project has no repo configured (research / chat / doc work
    doesn't need a git tree). When `projects.repo_url` IS set, layers
    a bare clone + per-slot git worktrees on top.

    Safe to call any number of times — existing dirs / clones /
    worktrees aren't recreated.

    Returns a status dict; callers in the project-switch flow
    surface the {configured, slots: {<slot>: {ok, ...}}} shape via
    the `provision_workspaces` event step.
    """
    pp = ensure_project_scaffold(project_id)

    # Always-on: plain per-slot dirs. The agent's SDK chdir crashes
    # with ENOENT before it can print a useful error if the path
    # doesn't exist; a directory that's not a git checkout is fine
    # for non-code work, and `coord_commit_push` rejects loudly when
    # `.git` isn't present.
    for slot in SLOT_IDS:
        try:
            pp.worktree(slot).mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception(
                "workspaces: mkdir failed for %s/%s", project_id, slot,
            )

    repo_url = await _read_project_repo_url(project_id)
    if not repo_url:
        logger.info(
            "workspaces: no repo_url for project '%s'; created plain "
            "per-slot dirs (no clone)",
            project_id,
        )
        return {
            "configured": False,
            "project_id": project_id,
            "reason": "projects.repo_url is empty",
        }

    from server.main import _mask_repo_url
    from server.events import bus
    status: dict[str, Any] = {
        "configured": True,
        "project_id": project_id,
        "repo_masked": _mask_repo_url(repo_url),
        "slots": {},
    }

    try:
        remote_refreshed = await _ensure_base_clone(pp.bare_clone, repo_url)
    except Exception as e:
        logger.exception("base clone failed for project %s", project_id)
        status["error"] = f"clone failed: {e}"
        return status

    # Per-slot remote summary. The bare clone is shared by all slots,
    # so a single set-url propagates to every slot simultaneously.
    if remote_refreshed:
        status["remotes_updated"] = list(SLOT_IDS)
        status["remotes_unchanged"] = []
        masked = _mask_repo_url(repo_url)
        asyncio.ensure_future(
            bus.publish(
                {
                    "ts": _now_iso(),
                    "agent_id": "system",
                    "type": "worktree_remote_updated",
                    "project_id": project_id,
                    "new_url_masked": masked,
                }
            )
        )
    else:
        status["remotes_updated"] = []
        status["remotes_unchanged"] = list(SLOT_IDS)

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


# Per-bare-clone lock so two concurrent `_provision_after_change`
# tasks targeting the same project (e.g. POST /api/projects
# immediately followed by PATCH ...{repo_url:...}) don't both pass
# the "already cloned" check and both run `git clone` to the same
# path. Lock is keyed by the bare-clone path so different projects
# don't serialize on each other.
_clone_locks: dict[str, asyncio.Lock] = {}


def _lock_for(bare: Path) -> asyncio.Lock:
    key = str(bare)
    lock = _clone_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _clone_locks[key] = lock
    return lock


async def _ensure_base_clone(bare: Path, repo_url_raw: str) -> bool:
    """Idempotent clone of the project repo into its bare-clone path.

    When the bare clone already exists and the configured URL has
    changed (e.g. PAT rotation, mirror migration), automatically
    refreshes the remote with `git remote set-url origin <new_url>`
    followed by a `git fetch --prune origin` to pull the latest
    remote refs. All per-slot worktrees share the bare clone's remote
    config, so a single set-url here propagates to every slot.
    The fetch is non-fatal — a transient network failure here doesn't
    block provisioning; the next agent push will surface the error.

    Returns True if a remote URL refresh was performed (set-url ran),
    False if the clone already had the correct URL or was freshly
    cloned (in which case the URL is correct by definition).
    """
    expected = _expand_placeholders(repo_url_raw)
    async with _lock_for(bare):
        if bare.exists() and (bare / ".git").exists():
            current = await _read_remote_url(bare)
            if current and current != expected:
                # URL changed — refresh in-place rather than requiring
                # a manual disk wipe. Common trigger: PAT rotation or
                # adding/removing a token in the ${VAR} placeholder.
                logger.info(
                    "workspaces: remote URL changed for %s — "
                    "running git remote set-url + fetch",
                    bare,
                )
                code, _, err = await _run(
                    ["git", "-C", str(bare), "remote", "set-url", "origin", expected],
                    timeout=10,
                )
                if code != 0:
                    raise RuntimeError(
                        f"git remote set-url failed (code {code}): "
                        f"{_mask_userinfo(err.strip(), expected)}"
                    )
                # Fetch so remote branch refs (work/<slot>) are up to
                # date with the new URL. Non-fatal: a network flap
                # here mustn't block the whole provisioning run — the
                # URL is now correct and the next agent push will
                # expose any remaining connectivity issue.
                fetch_code, _, fetch_err = await _run(
                    ["git", "-C", str(bare), "fetch", "--prune", "origin"],
                    timeout=120,
                )
                if fetch_code != 0:
                    logger.warning(
                        "workspaces: fetch after URL update failed for "
                        "%s (code %d): %s",
                        bare,
                        fetch_code,
                        _mask_userinfo(fetch_err.strip(), expected),
                    )
                else:
                    logger.info(
                        "workspaces: remote URL refreshed + fetch ok for %s",
                        bare,
                    )
                return True  # URL was refreshed
            else:
                logger.info("base repo already present at %s", bare)
            return False  # already correct URL
        bare.parent.mkdir(parents=True, exist_ok=True)
        # Log the unexpanded form so PATs don't hit the logs.
        logger.info("cloning %s → %s", repo_url_raw, bare)
        code, out, err = await _run(
            ["git", "clone", expected, str(bare)],
            timeout=300,
        )
        if code != 0:
            msg = (err.strip() or out.strip())
            raise RuntimeError(
                f"git clone exited {code}: "
                f"{_mask_userinfo(msg, expected)}"
            )
        logger.info("clone ok: %s", bare)
        return False  # fresh clone — URL correct by definition


async def _read_remote_url(bare: Path) -> str:
    """`git remote get-url origin` against the bare clone. Returns
    the URL on success, empty string on any failure (no remote, not
    a git dir, etc.) — callers use empty string as "couldn't
    determine, skip the URL-mismatch check"."""
    try:
        code, out, _ = await _run(
            ["git", "-C", str(bare), "remote", "get-url", "origin"],
            timeout=10,
        )
    except Exception:
        return ""
    if code != 0:
        return ""
    return out.strip()


async def _ensure_worktree(
    *, bare: Path, worktree: Path, slot: str,
) -> dict[str, Any]:
    branch_name = f"work/{slot}"

    if (worktree / ".git").exists():
        # Verify the worktree is actually on the expected branch.
        # Without this check a slot that was provisioned with the wrong
        # branch (e.g. `work/p5` on p6's worktree) silently reports
        # ok=True and coord_commit_push then pushes to the WRONG remote
        # branch — corrupting peer history (the "p6 session on p5
        # worktree" identity bug, t-2026-05-15-45716da0).
        code, out, _ = await _run(
            ["git", "branch", "--show-current"], cwd=worktree
        )
        actual_branch = out.strip()
        if actual_branch != branch_name:
            logger.error(
                "worktree %s is on branch %r (expected %r) — "
                "attempting non-destructive checkout",
                worktree, actual_branch, branch_name,
            )
            # Non-destructive: `git checkout` fails loudly on dirty trees
            # (uncommitted changes) — intentional; better to surface the
            # error than silently discard work.
            code2, _, err2 = await _run(
                ["git", "checkout", branch_name], cwd=worktree
            )
            if code2 != 0:
                return {
                    "ok": False,
                    "error": (
                        f"worktree at {worktree} is on wrong branch "
                        f"{actual_branch!r}; checkout {branch_name!r} "
                        f"failed (dirty tree or branch missing): "
                        f"{err2.strip()}"
                    ),
                }
            from server.events import bus
            try:
                await bus.publish({
                    "type": "workspace_branch_mismatch",
                    "slot": slot,
                    "path": str(worktree),
                    "actual_branch": actual_branch,
                    "expected_branch": branch_name,
                    "corrected": True,
                })
            except Exception:
                logger.exception(
                    "workspace_branch_mismatch event failed for slot=%s", slot
                )
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

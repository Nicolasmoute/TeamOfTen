# Workspace Refactor — One Scheme, No Drift

**Status**: planned, not yet implemented. Author: Claude (with Nicolas).
**Date**: 2026-05-06.

## Why this exists

Two failure modes were observed in production on 2026-05-06:

1. **Sofia (p8) found stale unrelated commits** in her per-slot worktree
   `/workspaces/p8/project` when starting work on `t-2026-05-06-38781da5`.
   She paused before destruction — correct discipline. Root cause: the
   per-slot worktrees aren't re-provisioned on project switch, so they
   retain whatever branch they had from the previous active project.
2. **Coach previously invented an ad-hoc shared worktree** at
   `/data/projects/dynamichypergraph/repo/shared/`. This isn't part of
   either path scheme defined in the harness — it was an improvisation
   to work around the staleness in (1). No tooling routes there:
   `coord_commit_push` chdirs to `workspace_dir(slot)` which still
   returns `/workspaces/<slot>/project`. The improvisation papered
   over the symptom but produced a third path scheme that confused
   subsequent Players.

The harness today has **two competing path schemes** that don't
reconcile:

- **Legacy** (active in `coord_commit_push`, agent cwd, all hot paths):
  `/workspaces/<slot>/project`. Project-unaware. Single global repo
  via `HARNESS_PROJECT_REPO` env or `team_config.project_repo`.
- **Multi-project** (defined in `server/paths.py`, used for everything
  *except* git): `/data/projects/<id>/repo/<slot>`. Project-scoped via
  `projects.repo_url`.

The `provision_project_repo` HTTP endpoint already acknowledges this
in code (`server/projects_api.py:853-857`):

> ensure_workspaces is the legacy single-project provisioner; the
> active-project flow already routes through workspace_dir via
> project_paths. Force the env override momentarily so the legacy
> function targets THIS project even when it isn't active. Phase 5+
> rework should plumb project_id through workspaces directly.

This plan is that "Phase 5+ rework."

## Goal

**One canonical path scheme**, project-scoped, used by every code
path. No global env, no shared seed checkout outside the active
project's tree, no per-slot worktree that survives a project switch.

## The canonical scheme

For project `<id>` and slot `<slot>`:

```
/data/projects/<id>/repo/.project   # bare-ish seed clone, one per project
/data/projects/<id>/repo/<slot>     # per-slot worktree on branch work/<slot>
```

Path resolution:
- `workspace_dir(slot) → /data/projects/<active>/repo/<slot>` (becomes
  `async def` — see §"Synchronous vs async" below).
- `project_paths(<id>).bare_clone → /data/projects/<id>/repo/.project`
  (already exists in `server/paths.py:119`).
- `project_paths(<id>).worktree(slot) → /data/projects/<id>/repo/<slot>`
  (already exists in `server/paths.py:68`).

Source of truth for repo URL: `projects.repo_url` for the project.
No `repo_branch` column — assume upstream default branch is `main`.
The day a project needs a different default we add the column; today
zero projects do.

## Provisioning lifecycle

`ensure_workspaces(project_id: str)` (signature change — now takes
the project explicitly, no env / DB sniffing). Idempotent. Called at:

1. **Boot, once, for the active project** — `lifespan` in
   `server/main.py`. Run after `init_db`.
2. **Every project switch**, after the working-agents pre-flight
   passes, before `swap_pointer`. Inserted in
   `_run_switch` (`server/projects_api.py:1205`) as a new step
   between `pull_new` (step 2) and `swap_pointer` (step 3). New
   step name: `provision_workspaces`.

Manual `POST /api/projects/{id}/repo/provision` stays as the
human-triggered backstop. With the signature change it becomes
trivial — no env mutation, no `_provision_lock` (the lock was only
needed because the old code mutated `os.environ` to feed a global
provisioner).

## Code touchpoints

### `server/workspaces.py` — major rewrite

**Delete**:
- `WORKSPACES_ROOT` constant + `HARNESS_WORKSPACES_ROOT` env.
- `BASE_REPO_PATH` constant.
- `_project_repo()`, `_project_branch()`, `_CACHED_REPO`,
  `_CACHED_BRANCH`, `_read_team_config_sync` (move to
  `server/db.py` if needed elsewhere; today nothing else uses it).
- `refresh_repo_cache()`.
- `project_configured()` (callers will switch to checking the
  per-project `repo_url` directly).
- The fallback in `workspace_dir(slot)` that returns plain
  `<slot>/` when no `.git` is present.

**Keep + adapt**:
- `_expand_placeholders()` and `_mask_userinfo()` — still needed for
  PAT-in-URL handling at clone time.
- `_run()` subprocess helper.
- `SLOT_IDS`.

**New shape**:

```python
async def workspace_dir(slot: str) -> Path:
    """Per-slot worktree path under the active project's repo tree."""
    project_id = await resolve_active_project()
    return project_paths(project_id).worktree(slot)

async def ensure_workspaces(project_id: str) -> dict[str, object]:
    """Idempotent. Reads projects.repo_url; clones if absent;
    creates per-slot worktrees if absent.
    """
    pp = ensure_project_scaffold(project_id)  # creates repo/ dir
    repo_url = await _read_project_repo_url(project_id)
    if not repo_url:
        return {"configured": False, "project_id": project_id,
                "reason": "projects.repo_url is empty"}
    await _ensure_base_clone(pp.bare_clone, repo_url)
    slot_results = {}
    for slot in SLOT_IDS:
        slot_results[slot] = await _ensure_worktree(
            bare=pp.bare_clone,
            worktree=pp.worktree(slot),
            slot=slot,
        )
    return {"configured": True, "project_id": project_id,
            "slots": slot_results}

async def get_status(project_id: str | None = None) -> dict[str, object]:
    """For /api/status. Same shape as today but project-scoped."""
```

`_ensure_worktree` keeps the existing branch-resolution priority
(local exists → reuse; remote exists → track; neither → fresh from
default). Only the path arguments change.

### `server/main.py:lifespan`

Change:
```python
workspaces_status = await ensure_workspaces()  # old: no arg
```
To:
```python
from server.db import resolve_active_project
active = await resolve_active_project()
workspaces_status = await ensure_workspaces(active)
```

Keep the "errors don't abort startup" semantics.

Also remove the `HARNESS_PROJECT_REPO` references in
`PUT /api/team/repo` and `POST /api/team/repo/provision` (lines
2451-2585). The `/api/team/repo` endpoints become obsolete — repo
URL is now per-project via `PATCH /api/projects/{id}` (already
exists). Delete them, drop the `team_config.project_repo` /
`project_branch` rows on next migration tick.

### `server/projects_api.py:_run_switch`

Insert provisioning between `pull_new` and `swap_pointer`:

```python
if failed_step is None:
    # Step 2.5 — provision per-slot worktrees for the new project.
    try:
        await _emit_step(
            job_id=job_id, step="provision_workspaces", status="running",
            from_project=from_project, to_project=to_project,
        )
        provision_result = await ensure_workspaces(to_project)
        await _emit_step(
            job_id=job_id, step="provision_workspaces", status="ok",
            from_project=from_project, to_project=to_project,
            detail={"slots": provision_result.get("slots", {})},
        )
    except Exception as e:
        failed_step = "provision_workspaces"
        failure_detail = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
        await _emit_step(
            job_id=job_id, step="provision_workspaces", status="failed",
            from_project=from_project, to_project=to_project,
            detail=failure_detail,
        )
```

Hard-abort behavior matches the existing pattern: if provisioning
fails, the switch aborts before the pointer swap and emits
`project_switched ok=False`. The pre-swap project remains active.

`provision_project_repo` HTTP handler (`projects_api.py:814`) becomes
trivial — no env mutation, no lock, no pin (the function is now
project-aware):

```python
async def provision_project_repo(...):
    # ...validation as today...
    result = await ensure_workspaces(project_id)
    await bus.publish({"type": "project_repo_provisioned", ...})
    return {"ok": True, "project_id": project_id, "result": result}
```

Drop `_provision_lock`.

### `server/tools.py:coord_commit_push`

- Line 2453: `if not project_configured()` — replace with a
  per-project check: load `projects.repo_url` for the active project,
  reject if empty.
- Line 2574: `cwd = workspace_dir(caller_id)` — `await`.
- Line 2620: `from server.workspaces import BASE_REPO_PATH` —
  replace with the per-project bare clone:
  `bare = project_paths(await resolve_active_project()).bare_clone`.
- Update the misplaced-work error message body to reference the new
  paths.

### `server/templates/app_dev_claude_md.md`

Lines 317-342 ("Worktree boundary" section) — rewrite to reference
`/data/projects/<project>/repo/<your_slot>` and
`/data/projects/<project>/repo/.project`. The reconciliation flow at
`server/project_claude_md.py:update_claude_md_via_coach` propagates
this to every existing project's CLAUDE.md on next activation.

### `server/agents.py:3460`

System prompt mentions `./project/` and `HARNESS_PROJECT_REPO`. Update
to reference the new path scheme. Same line 4709/4804/4826: those
just call `workspace_dir`, so the only change is `await`.

### `server/runtimes/claude.py` and `server/runtimes/codex.py`

Two call sites each. Add `await` to the `workspace_dir(...)` calls.
No other changes.

### `server/main.py` — `/api/health`

The workspaces probe at line 907 calls `get_workspaces_status()`.
That becomes `await get_status(active_project)`. Only cosmetic.

## What's deleted

- `/workspaces/` tree on disk (humans must salvage anything unpushed
  before deploy — see "Risks" below).
- `HARNESS_PROJECT_REPO` and `HARNESS_PROJECT_BRANCH` env vars.
- `team_config.project_repo` and `team_config.project_branch` rows
  (redundant with `projects.repo_url`).
- `/api/team/repo` GET / PUT and `/api/team/repo/provision`
  endpoints (`server/main.py:2451-2585`).
- The Options drawer "Project repo" UI section that talks to the
  global endpoints (`server/static/app.js` around line 4924).
  Per-project `repo_url` fields in the Projects section already
  exist and stay.
- `_provision_lock` and the `os.environ` mutation hack in
  `provision_project_repo`.

## Tests

### Update
- `server/tests/test_misplaced_work_detection.py` — both fixtures
  patch `BASE_REPO_PATH`. Replace with patches against the new
  per-project bare clone helper.
- `server/tests/test_coord_commit_push_gate.py` — `project_configured`
  patch goes away; replace with a stubbed `projects.repo_url` row
  for the active project.

### New
- `test_workspaces.py` — unit tests for the rewritten module:
  - `workspace_dir(slot)` returns the right path for the active
    project (with and without a contextvar pin).
  - `ensure_workspaces(project_id)` is idempotent.
  - `ensure_workspaces` reads from `projects.repo_url`, not env.
  - Worktree branch resolution: local-exists → reuse, remote-exists
    → track, neither → fresh.
- `test_run_switch_provisioning.py` — switch from project A → B
  emits `provision_workspaces` step in the expected order; failure
  aborts before `swap_pointer`.

## Synchronous vs async

`workspace_dir(slot)` becomes `async def`. All current call sites
are inside async functions:

- `server/agents.py:4709, 4804, 4826` — TurnContext build / dispatch.
- `server/runtimes/claude.py:134`, `server/runtimes/codex.py:2023`.
- `server/tools.py:2574` (`coord_commit_push`).

Adding `await` is mechanical. Justification: the function reads the
active project, which is a contextvar (sync) or a DB row (async).
Mixing the two via a sync DB read would either block the event loop
or require maintaining a write-invalidated cache — both more brittle
than `await`.

## Risks (read before deploy)

### LOUD: unpushed work in legacy paths

This refactor orphans:
- `/workspaces/<slot>/project` for every slot.
- `/data/projects/dynamichypergraph/repo/shared/` (Coach's
  improvisation).

**Anything not pushed to origin in those locations is gone after
deploy.** Required pre-deploy checklist:

1. SSH into the live container.
2. For each slot: `git -C /workspaces/<slot>/project status` and
   `git -C /workspaces/<slot>/project log --branches --not --remotes`.
   Push or stash anything unpushed.
3. `git -C /data/projects/dynamichypergraph/repo/shared/ status` +
   the same `--not --remotes` check. Salvage by hand into the
   per-slot worktrees the new scheme will create on first boot.
4. Confirm `projects.repo_url` is populated for every project that
   needs a repo. The migration is a one-liner if it isn't (see
   below).

### Migration: seeding `projects.repo_url`

If `team_config.project_repo` is set but `projects.repo_url` is empty
for the currently-active project, copy across. One-shot SQL on the
live DB before deploy:

```sql
UPDATE projects
SET repo_url = (SELECT value FROM team_config WHERE key = 'project_repo')
WHERE id = (SELECT value FROM team_config WHERE key = 'active_project_id')
  AND (repo_url IS NULL OR repo_url = '');
```

Other projects need their `repo_url` set via the UI (Options →
Projects → edit) before first activation under the new code.

### First-boot disk usage

Under the new scheme, every project that gets activated provisions
11 worktrees (Coach + 10 Players) under
`/data/projects/<id>/repo/`. For most repos this is cheap (sparse
checkouts share the bare clone's git objects). For
`dynamichypergraph` if it has large generated artifacts, watch
`/data` usage on first activation post-deploy.

### Coach's worktree

The current code provisions a worktree for Coach too (`SLOT_IDS`
includes `coach`). The new scheme keeps this — Coach uses the
worktree for read-only operations (Read, Grep, Bash for inspection).
Commits remain blocked: `coord_commit_push` is Player-only, and
that gate is unchanged. The doc comment in `paths.py:42` ("Coach
has no worktree") was aspirational and is wrong today; we'll fix
the comment, not the behavior.

## Spec updates (in this same PR)

- `Docs/TOT-specs.md` §4.6 "Current Project Repo Caveat" — rewrite
  as "Project Repo Layout" describing the now-canonical scheme.
- `Docs/TOT-specs.md` §11 (workspace_path / `/workspaces/coach`
  references) — point at `/data/projects/<id>/repo/<slot>`.
- `Docs/TOT-specs.md` §22 "Known Issues" → remove the "Workspaces
  still use `/workspaces`" entry.
- `CLAUDE.md` "Done" list — append a 2026-05-06 entry summarizing
  the refactor.
- `server/templates/app_dev_claude_md.md` — see §"Code touchpoints"
  above.

## Out of scope

- Per-project default branch override (no `repo_branch` column).
- Worktree GC / pruning when a project is deleted.
- Sparse-checkout for large repos.
- Migration of existing `/workspaces/<slot>/project` history into
  the new tree (manual salvage only).

## Rollback

Revert the PR. The `/workspaces/` tree on disk will still exist on
the running container (we don't delete it; the new code just stops
reading it). Restoring the old `HARNESS_PROJECT_REPO` env value on
Zeabur after a revert restores legacy behavior.

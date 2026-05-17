---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 17: Git Workspaces'
section: 17
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 17. Git Workspaces

Per-project, project-scoped. Repo URL lives on each project row at
`projects.repo_url`; there is no global override (no env var, no
`team_config.project_repo`).

Layout (matches §4.6):

```text
/data/projects/<id>/repo/.project   # bare-ish seed clone (one per project)
/data/projects/<id>/repo/coach      # Coach's worktree (read-only by convention)
/data/projects/<id>/repo/p1         # Player worktree, branch work/p1
/data/projects/<id>/repo/p2
...
/data/projects/<id>/repo/p10
```

`workspace_dir(slot)` (`async`) returns
`project_paths(active).worktree(slot)`. The function `mkdir`s the
path if it's missing — `ensure_workspaces` is the canonical
provisioner, but this self-heal keeps an agent's SDK chdir from
crashing with ENOENT when the directory has somehow gone missing
(transient FS error, mid-migration deploy). The cwd needs to
*exist* for the runtime to start; whether it's a git checkout is
a separate concern that `coord_commit_push` checks.

`ensure_workspaces(project_id)` is the only provisioner:

- Idempotent.
- Always creates plain per-slot directories at
  `/data/projects/<id>/repo/<slot>` so agent cwds exist for
  non-code work (chat, research, doc writing) regardless of repo
  configuration.
- Reads `projects.repo_url` for the given project.
- If `repo_url` is empty, returns `{configured: False}` and skips
  the clone + worktree step. `coord_commit_push` rejects loudly
  on commits because the path isn't a git checkout.
- If set, clones to `/data/projects/<id>/repo/.project`, then
  layers per-slot git worktrees on top of the plain dirs at
  `/data/projects/<id>/repo/<slot>` on branch `work/<slot>`.
- Branch resolution priority on first creation: local `work/<slot>`
  exists → reuse; remote `origin/work/<slot>` exists → track new
  local from it; neither → fresh from upstream default branch
  (`main`).

Provisioning fires at:

- **Boot**, once, for the active project.
- **Project switch**, as the `provision_workspaces` step in
  `_run_switch` between `pull_new` and `swap_pointer`. A failed
  provision aborts the switch before the pointer swap.
- **Manual** via `POST /api/projects/{id}/repo/provision` (the
  human-triggered backstop).

`coord_commit_push` requires the active project to have a
`repo_url` and the per-slot worktree to contain `.git`. It chdirs
to `workspace_dir(caller_id)` and runs `git add -A`,
`git commit -m`, `git push origin HEAD`.

Misplaced-work detector: when the slot's worktree is clean but the
project's bare seed clone (`project_paths(active).bare_clone`) is
dirty, `coord_commit_push` returns a loud named error pointing the
Player at both paths. The seed clone is never the right tree to
edit — it has no `work/<slot>` branch checked out.

---

---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 4: Project Model'
section: 4
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 4. Project Model

The Projects refactor is implemented enough that the harness is now
project-scoped in its core state. One project is active at a time.

### 4.1 Project Identifier

The project id is a slug:

- Regex: `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`
- Length: 2 to 48 characters.
- Lowercase ASCII letters, digits, and single dashes.
- No leading dash, trailing dash, or consecutive dashes.
- Reserved slugs: `skills`, `wiki`, `mcp`, `projects`, `snapshots`,
  `harness`, `data`, `claude`.

The slug is used as:

- `projects.id`
- `project_id` on project-scoped DB tables
- `/data/projects/<slug>/`
- `/data/wiki/<slug>/`
- `projects/<slug>/` and `wiki/<slug>/` paths on WebDAV

`misc` is the permanent fallback/default project. It is created on first boot
and cannot be deleted.

### 4.2 Project Row

Table: `projects`

| Column | Type | Notes |
| --- | --- | --- |
| `id` | text PK | Project slug |
| `name` | text | Display name |
| `created_at` | text | UTC timestamp default |
| `repo_url` | text nullable | Intended per-project repo URL |
| `description` | text nullable | Short project description |
| `archived` | integer | 0/1, hidden from switcher when archived |

### 4.3 Active Project

The active project is stored in:

```text
team_config.key = "active_project_id"
```

`resolve_active_project()` returns:

1. A contextvar-pinned project during an activation flow.
2. The DB value from `team_config`.
3. Fallback `misc`.

Project-scoped API queries and `coord_*` tools resolve the active project at
call time and add `WHERE project_id = ?`.

### 4.4 Project Lifecycle

Implemented endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/projects` | List all projects, including archived, with active marker |
| `POST /api/projects` | Create a project, validate slug, scaffold folders, write CLAUDE.md stub |
| `PATCH /api/projects/{id}` | Edit name, description, repo URL, archived flag |
| `DELETE /api/projects/{id}` | Delete project row and filesystem tree; `misc` forbidden |
| `GET /api/projects/{id}/roles` | Read per-project team identity rows |
| `GET /api/projects/switch-preview?to=<slug>` | Preflight counts for switch modal |
| `POST /api/projects/{id}/activate` | Start async project switch |
| `POST /api/projects/{id}/repo/provision` | Provision repo/worktrees for that project URL |

Project creation:

- Validates slug and name.
- Inserts into `projects`.
- Calls `ensure_global_scaffold()` and `ensure_project_scaffold(slug)`.
- Writes `/data/projects/<slug>/CLAUDE.md` if absent.
- Ensures `/data/wiki/<slug>/`.
- Rebuilds `/data/wiki/INDEX.md`.
- Emits `project_created`.

Project patch:

- Slug is immutable.
- Name max 200 chars.
- Description max 1000 chars.
- Repo URL is stored as given, but API responses mask URL userinfo.
- Emits `project_updated`.

Project delete:

- `misc` returns forbidden.
- Deletes project row; ON DELETE CASCADE removes scoped DB rows.
- Removes `/data/projects/<slug>/` and `/data/wiki/<slug>/`.
- Best-effort removes WebDAV `projects/<slug>` and `wiki/<slug>`.
- If the deleted project was active, switches pointer back to `misc`.
- Emits `project_deleted`, and sometimes `project_switched` with
  `reason="auto_after_delete"`.

Archive:

- Archived projects are listed in Options but hidden/dimmed in the switcher.
- Activating an archived project is rejected.
- Provisioning an archived project is rejected.

### 4.5 Project Switch Flow

`POST /api/projects/{id}/activate`:

- Validates slug.
- Requires target project to exist and not be archived.
- Rejects with `423` if any agent is `working` or `waiting`.
- Returns `200` for noop when already active.
- Returns `409` if another switch is in progress.
- Otherwise returns `202` with `job_id` and starts a background switch task.

Background switch steps:

1. Emit `project_switch_step` `started`.
2. `push_current`: force-push current project to WebDAV.
   - Calls `tag_live_conversations()`.
   - Calls `push_project_tree(from_project)`.
   - Uses timeout `HARNESS_KDRIVE_CLOSE_TIMEOUT_S`, default 60s.
3. `pull_new`: pull target project tree from WebDAV.
4. `provision_workspaces`: clone repo + create per-slot worktrees
   for the new project (idempotent). Pinned to `to_project` for the
   duration. A failed provision aborts the switch before the
   pointer swap so `from_project` stays active.
5. `swap_pointer`: set `team_config.active_project_id`.
6. `reload`: emit terminal `project_switched`.

Failure semantics:

- Push or pull failures emit step failure and terminal `project_switched`
  with `ok=false`.
- The active pointer is not swapped on a hard pre-swap failure.
- Unexpected task crashes also publish terminal failure so the UI is not stuck.
- During the pointer swap, `pin_active_project()` makes project resolution
  coherent for tool calls/events that begin mid-switch.

Switch-preview counts:

- Current project.
- Destination project.
- Files likely needing push, byte count, initial-sync flag.
- Live conversation count.
- Whether target exists on disk.
- In-flight agent id, if any.

### 4.6 Project Repo Layout

Repo URL is per-project: `projects.repo_url` is the single source of
truth. There is no global `HARNESS_PROJECT_REPO` env or
`team_config.project_repo` row — both were retired with the
2026-05-06 workspace refactor (`Docs/workspace-refactor-plan.md`).

Worktrees live under each project's own tree, not a global
`/workspaces/`:

```text
/data/projects/<id>/repo/.project   # bare-ish seed clone, one per project
/data/projects/<id>/repo/<slot>     # per-slot worktree on branch work/<slot>
```

Path resolution helpers in `server/paths.py`:

- `project_paths(<id>).bare_clone` → `/data/projects/<id>/repo/.project`
- `project_paths(<id>).worktree(slot)` → `/data/projects/<id>/repo/<slot>`

`workspace_dir(slot)` (in `server/workspaces.py`) is `async` and
resolves through the active project: it returns
`project_paths(active).worktree(slot)`. Pure function of `(active,
slot)`.

Provisioning happens in four places — all idempotent:

1. **Project creation** (`POST /api/projects`) —
   `_provision_after_change` fires as a fire-and-forget task
   right after the row insert + scaffold, so worktrees materialize
   automatically without the operator hitting "provision now".
   Plain per-slot dirs always; clone + worktrees only when
   `repo_url` was provided. Failures surface via the
   `project_repo_provisioned` bus event with
   `source="project_created"`.
2. **Project repo URL update** (`PATCH /api/projects/{id}` with
   `repo_url` in the body) — same fire-and-forget hook
   (`source="project_updated"`). Going from URL A → URL B keeps
   the existing bare clone's remote (the existing `.project` is
   detected as already-cloned); operators changing remotes need
   to delete + recreate the project, or wipe
   `/data/projects/<id>/repo/.project` on the container.
3. **Boot**, once, for the active project — `lifespan` in
   `server/main.py` calls `ensure_workspaces(active_project_id)`
   after `init_db`.
4. **Project switch** — `_run_switch` (`server/projects_api.py`)
   inserts a `provision_workspaces` step between `pull_new` and
   `swap_pointer`. A failed provision aborts the switch before the
   pointer swap and emits `project_switched ok=False`.

The per-slot worktree retains its `work/<slot>` history across
project switches: switching back to a project re-runs
`ensure_workspaces`, which sees the existing worktree and is a
no-op. Branch resolution priority on first creation: local
`work/<slot>` exists → reuse; remote `origin/work/<slot>` exists →
track; neither → fresh from upstream default branch (`main`).

Coach gets a per-slot worktree too (used for read-only inspection —
Read, Grep, Bash). `coord_commit_push` is Player-only at the tool
level; Coach can't commit even with a worktree.

Manual backstop: `POST /api/projects/{id}/repo/provision` runs
`ensure_workspaces(<id>)` for any project (active or not). No env
mutation, no global state — the function is project-aware.

---

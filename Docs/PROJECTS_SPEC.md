# Projects Refactor — Spec

> Refactor of the TeamOfTen harness to make **Project** a first-class concept.
> One harness can serve many projects; one project is active at any time.
> Agents, memory, decisions, conversations, repos, outputs, and inputs all
> scope to the active project. A small global tier holds rules, skills, MCP
> config, and the cross-project wiki.

Status: **proposed** — awaiting review before implementation.
Migration policy: **destructive** — existing DB rows and kDrive folders are
deleted and recreated fresh. No backfill.

---

## 1. Goals
1. **Multi-project workflows on one harness.** Switch between Misc (everyday quick needs), client work, side projects, etc. without spinning up separate deployments.
2. **Clean knowledge boundaries.** All project-scoped state (tasks, messages, memory, decisions, conversations, events, turns, attachments) — plus **per-agent identity (name, role description, brief)** — is isolated per project, saved on switch and reloaded when the project is reopened. Only the 11 slots themselves (Coach + p1..p10), their model defaults and lock state, the global wiki, skills, MCP config, and OAuth tokens cross project boundaries.
3. **Cross-project knowledge does compound** — via a global wiki (Karpathy's LLM-Wiki pattern) sitting above projects.
4. **Project switch is visible and resumable.** UI confirmation + animated busy stepper; sessions for the new project resume from where they were left if any exist (a brand-new project starts fresh). Switch latency tracks data size — small projects swap sub-second, first-time repo clones can take longer.
5. **No silent data loss.** Pre-flight sync before switch; live conversations tagged when persisted mid-session.
6. **Atomic switches.** A failed project switch leaves the previous project active and intact — no half-loaded state, no orphaned files, no corrupted DB rows. Either fully commits or fully rolls back.

### Non-goals

- Migrating existing data (DB rows, kDrive folders) — start fresh.
- Auto-populating project content. Wiki entries, plans, and per-project CLAUDE.md fields are filled in by agents as they work; the harness only scaffolds empty folders and ensures the LLM-Wiki *skill* is installed (see §9). The per-project wiki folder `wiki/<slug>/` lives in the global tree but is not auto-populated either.
- Cross-project agent collaboration in a single conversation. Each conversation is scoped to one active project; the slot roster (Coach + 10 Players, their model defaults and lock state) is shared across projects, but each agent's **name, role description, and brief are per-project** — saved on switch and reloaded when the project is reopened (see §3 `agent_project_roles`, §15).

---

## 2. The Project concept
### Project slug

The **project slug** is the project's primary identifier — a URL-safe lowercase
string of letters, digits, and dashes, e.g. `misc`, `simaero-rebrand`, `harness`.

**Constraints**: 2–48 characters, must match `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`
(starts with a lowercase letter; ASCII alphanumeric + single dashes only;
no leading dash, no trailing dash, no consecutive dashes). **Reserved**
(would collide with global folders or top-level paths): `skills`, `wiki`,
`mcp`, `projects`, `snapshots`, `harness`, `data`, `claude`. Generation
strategy on create: see §14 Q1.

The slug is the **single key** used everywhere:
- `projects.id` in the DB
- `project_id` foreign key on every domain row (tasks, messages, memory, …)
- folder name in `/data/projects/<slug>/`
- folder name in `/data/wiki/<slug>/`
- mirrored to the same paths under `kDrive/TOT/`

Same slug, used in both `projects/` and `wiki/` — same project, two trees
(see §4 for why the wiki sits outside `projects/`).

### Properties

A project is a row in a new `projects` table:

| Field | Type | Notes |
|---|---|---|
| `id` | text PK | the project slug, e.g. `misc`, `simaero-rebrand` |
| `name` | text | display name |
| `created_at` | text (ISO) | |
| `repo_url` | text nullable | per-project git repo (for worktrees + commit-push). Misc inherits the current `HARNESS_PROJECT_REPO` env at first boot. |
| `description` | text nullable | shown in switch UI |
| `archived` | boolean | hide from the switcher without deleting |

### Default project

- `misc` is auto-created on first boot if no projects exist.
- Misc is the only project that **cannot be deleted**. When a user deletes the *currently-active* non-Misc project, the harness auto-switches to Misc as the fallback active project (this is the only scenario where Misc's permanence becomes load-bearing — every other deletion is harmless).
- Misc inherits `HARNESS_PROJECT_REPO` from env on first boot. After that, the env var stops being authoritative — repo_url lives in the DB.

### Active project

- Single row in `team_config`: `active_project_id` (default `misc`).
- **Project-scoped** state (tasks, messages, memory, decisions, events, turns, attachments) filters by `WHERE project_id = active_project_id` at query time.
- **Global** state (the slot roster — Coach + p1..p10; `agents.status`, `agents.locked`, model defaults; `team_config`; `mcp_servers`; cost-cap counters; OAuth tokens; the wiki tree) is unaffected by the active-project pointer.
- **Per-project agent identity**: each agent's `name`, `role`, and `brief` live in `agent_project_roles(slot, project_id)` — they swap on project switch so Coach can recompose the team for each project's domain (see §3 schema, §6 switch flow).
- Cross-project access happens in exactly two places: the wiki (any agent can **read and write** any project's wiki — entries hyperlink across projects, which is the point) and the project switcher UI (lists all non-archived projects).

### Lifecycle

| Action | Where | Effect |
|---|---|---|
| **Create** | Left rail → project button → "New…" modal (name, optional description, optional repo URL) | Insert `projects` row, create fs scaffold + kDrive folder. Team composition starts empty — `agent_project_roles` has no rows for the new project, so on first activation each slot lacrosse-auto-picks a name and runs with empty role/brief; Coach customizes via `coord_set_player_role`. Then switch via the standard flow (see §6). |
| **Switch** | Left rail → project button → pick from list | Pre-flight sync of current project → swap `active_project_id` → reload per-project context: sessions (`agent_sessions`), team identity (`agent_project_roles` — name, role, brief per slot), conversations, project CLAUDE.md, files pane. See §6 for the full step-by-step flow. |
| **Edit** | Options drawer → Projects section → project card | Update name, repo_url, description. **Slug is immutable** — it's the PK and the on-disk + kDrive folder name; renaming a project changes only the display name. |
| **Delete** | Options drawer → Projects section → project card → trash | Confirmation modal warns "all conversations, memory, decisions, outputs, and the wiki sub-folder for this project will be deleted on disk and on kDrive". If the deleted project was active, the harness auto-switches to Misc via the standard §6 flow. Misc cannot be deleted. |
| **Archive** | Options drawer → Projects section → archive toggle | Hide from switcher, retain data on disk and kDrive. Archived projects do not participate in sync runs until un-archived; their last synced state is preserved. |

---

## 3. Schema changes
### New tables

```sql
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    repo_url TEXT,
    description TEXT,
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE agent_sessions (
    slot TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id TEXT,
    last_active TEXT,
    continuity_note TEXT,                  -- /compact handoff summary, per-session
    last_exchange_json TEXT,               -- rolling pre-compact exchange log, per-session
    PRIMARY KEY (slot, project_id)
);

-- Per-project team composition: name, role description, brief.
-- Coach can recompose the team for each project's domain; values
-- swap automatically on project switch.
CREATE TABLE agent_project_roles (
    slot TEXT NOT NULL,                    -- 'coach' or 'p1'..'p10'
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT,                             -- e.g. 'Alice Rabil'; NULL → lacrosse-auto-pick on first activation
    role TEXT,                             -- e.g. 'Developer — writes code'; nullable
    brief TEXT,                            -- per-agent system-prompt addendum; nullable
    PRIMARY KEY (slot, project_id)
);

CREATE TABLE sync_state (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    tree TEXT NOT NULL CHECK (tree IN ('project', 'wiki')),  -- 'project' = /data/projects/<slug>/, 'wiki' = /data/wiki/<slug>/
    path TEXT NOT NULL,                   -- relative to the tree root
    mtime REAL NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    PRIMARY KEY (project_id, tree, path)
);

-- Indexes for the constant `WHERE project_id = ?` scan on every domain read.
CREATE INDEX idx_tasks_project       ON tasks(project_id);
CREATE INDEX idx_messages_project    ON messages(project_id);
CREATE INDEX idx_memory_project      ON memory_docs(project_id);
CREATE INDEX idx_events_project      ON events(project_id);
CREATE INDEX idx_turns_project       ON turns(project_id);
-- Note: attachments are filesystem files (under /data/attachments/),
-- not DB rows. No `attachments` table exists; project scoping for
-- attachments is achieved by the per-project folder layout in §4.
```

### Columns added

`project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE` is added to:

- `tasks`
- `messages`
- `memory_docs`
- `events`
- `turns`

(The `attachments` lane is filesystem-only — no DB table — so it gets project scoping via the per-project folder layout in §4, not via a `project_id` column.)

**Tables that stay global** (no `project_id`):

- `agents` — the 11-slot roster. After migration, columns are: `id`, `kind` (coach|player), `status`, `current_task_id`, `model` (per-agent override; per-role defaults still live in `team_config`), `workspace_path`, `cost_estimate_usd`, `started_at`, `last_heartbeat`, `allowed_extra_tools`, `locked`. Columns `name`/`role`/`brief` move to `agent_project_roles`; `session_id`/`continuity_note`/`last_exchange_json` move to `agent_sessions`.
- `team_config` — kv store; holds `active_project_id`, `schema_version`, per-role default models, telegram_disabled, etc.
- `mcp_servers`, `secrets`, `message_reads` (inherits scoping transitively via its `message_id` FK).

`projects`, `agent_sessions`, `agent_project_roles`, and `sync_state` are project-keyed by design.

### Columns removed

- `agents.session_id`, `agents.continuity_note`, `agents.last_exchange_json` → moved to `agent_sessions` (per-(slot, project)). The /compact handoff state is per-session, so it travels with the session.
- `agents.name`, `agents.role`, `agents.brief` → moved to `agent_project_roles` (per-(slot, project)). Each project keeps its own team composition; switching projects reloads the team identity for that project.
- `team_config.repo_url` (if any) → moved to `projects.repo_url`.

### Active project pointer

`team_config` gets a single row: `key='active_project_id', value='misc'`. Resolved at request time.

### Migration policy

**Destructive.** Schema version is tracked via a `team_config` row `key='schema_version'`. If the row is missing or set below `projects_v1`, run the migration; otherwise skip. Migration is idempotent under failure: it sets `schema_version = projects_v1` only after every step succeeds, so a crashed boot retries cleanly.

Steps:

1. **Drop and recreate** every table that gains `project_id` (`tasks`, `messages`, `memory_docs`, `events`, `turns`). No `ALTER TABLE ADD COLUMN`-with-default dance — drop is simpler and we're not preserving data anyway. Drop these columns from `agents` (`ALTER TABLE agents DROP COLUMN <col>` × 6): `session_id`, `continuity_note`, `last_exchange_json` (→ `agent_sessions`), `name`, `role`, `brief` (→ `agent_project_roles`). Drop any `team_config.repo_url` entries.
2. **Create** `projects`, `agent_sessions`, `agent_project_roles`, `sync_state` at the new schema.
3. **Insert** the `misc` project row, and set the active-project pointer: `INSERT OR REPLACE INTO team_config(key, value) VALUES ('active_project_id', 'misc')`. Without this, `resolve_active_project()` returns NULL on first request and every project-scoped query fails.
4. **Repo URL inheritance**: if `HARNESS_PROJECT_REPO` is set, copy it into `projects.repo_url` for `misc`.
5. **Wipe local data dirs**: `/data/memory/`, `/data/decisions/`, `/data/events/`, `/data/outputs/`, `/data/knowledge/`, `/data/uploads/`, `/data/attachments/`, and `/workspaces/` (the current per-slot worktree root — the new layout relocates worktrees to `/data/projects/<slug>/repo/<slot>/`). The `/data/harness.db` file itself stays — only the dropped tables inside are wiped (step 1); preserved tables (`agents`, `team_config`, `secrets`, `mcp_servers`) keep their rows.
6. **Wipe kDrive** `TOT/` root recursively. New layout (§4) gets recreated on first sync.
7. **Scaffold misc** at `/data/projects/misc/` and `/data/wiki/misc/` (see §4).
8. **Mark done**: `INSERT OR REPLACE INTO team_config(key, value) VALUES ('schema_version', 'projects_v1')`.

A one-shot migration script lives at `server/migrations/projects_v1.py`. Logs every drop loudly. Bootstraps under a single transaction where SQLite allows; the local-fs and kDrive wipes happen outside the txn (filesystem can't roll back) but are idempotent on retry.

---

## 4. Filesystem layout
### Why two trees per project (`projects/<slug>/` AND `wiki/<slug>/`)

Each project's data is split across two top-level folders by concern:

- **`projects/<slug>/`** — project-private working data: memory, decisions,
  conversations, plans, repo worktrees, outputs, inputs. Only meaningful in
  the context of that one project.
- **`wiki/<slug>/`** — knowledge entries about that project, but living in the
  **global** `wiki/` tree. Why outside `projects/`?
  - Cross-project hyperlinks (`[[../other-project/concept-name]]`) resolve
    naturally when all wikis share one parent.
  - `wiki/INDEX.md` enumerates every project's entries from one place.
  - Knowledge compounds across projects — the whole point of Karpathy's
    pattern — and that requires a shared root.

**Inside `wiki/<slug>/`**: only `.md` files, one concept per entry, no
deeper sub-folders (the project slug folder IS the grouping). **At
`wiki/` root** (alongside `INDEX.md`): cross-project `.md` entries that
don't belong to a single project — shared concepts, glossary terms,
patterns referenced from multiple projects' entries via
`[[../shared-concept]]` links.

### On the server (`/data/`)

```
/data/
├── CLAUDE.md                  # global house rules (harness behavior, project/wiki principles)
├── .claude/                   # canonical Claude Code project layout
│   └── skills/                # global skills, including the LLM-Wiki skill (see §9)
├── mcp/                       # global MCP server JSON files (mirror of DB-stored configs, for portability; v2)
├── wiki/                      # Karpathy LLM-Wiki, global root
│   ├── INDEX.md               # links every project's sub-wiki + cross-project entries
│   └── <project_slug>/        # one folder per project; created on project creation
├── projects/
│   └── <project_slug>/
│       ├── CLAUDE.md          # project-specific rules, stakeholders, glossary, repo notes
│       ├── decisions/         # append-only durable record (immutable ADRs)
│       ├── working/
│       │   ├── conversations/ # per-agent conversation snapshots; tagged `live` when synced mid-session
│       │   ├── handoffs/      # inter-agent context handoffs
│       │   ├── knowledge/     # text artifacts via coord_write_knowledge (specs, research, design drafts)
│       │   ├── memory/        # shared scratchpad (overwrite-on-update by topic via coord_*_memory)
│       │   ├── plans/         # task breakdowns, drafts
│       │   └── workspace/     # generic scratch
│       ├── outputs/           # binary deliverables
│       ├── uploads/           # user-uploaded files (read-only, pulled from kDrive); renamed from inputs/ in projects_v2
│       ├── attachments/       # UI paste-target images; local-only, not synced, 30-day trim
│       └── repo/              # git worktrees per Player
│           ├── .project/      # bare clone, shared across this project's worktrees
│           ├── p1/            # one worktree per Player slot; Coach has no worktree
│           ├── p2/
│           └── ...
├── harness.db                 # SQLite hot-state DB; rotations flush to kDrive snapshots/
└── claude/                    # CLAUDE_CONFIG_DIR — OAuth tokens, persisted across redeploys (NOT the Claude Code .claude/ above)
```

**projects_v2 layout migration** (post-Phase-8 cleanup): brought the on-disk layout
in line with the spec by (a) wiping legacy flat dirs at the data root left over from
pre-refactor — `handoffs/`, `uploads/`, `context/`, `knowledge/`, plus typo orphans
`output/`, `upload/`, `uplods/` — (b) moving `/data/skills/` to `/data/.claude/skills/`
to match Claude Code's canonical project layout, and (c) renaming per-project
`inputs/` to `uploads/` to match the user-visible mental model. See
[server/migrations/projects_v2.py](server/migrations/projects_v2.py).

### On kDrive (`TOT/`)

Mirrors the server layout for the synced subset (see **Excluded from
kDrive sync** below for what stays local-only):

```
TOT/
├── CLAUDE.md
├── .claude/
│   └── skills/
├── mcp/
├── wiki/
│   ├── INDEX.md
│   └── <project_slug>/
├── snapshots/                 # /data/harness.db rotations (5-min cadence, ~144 retained ≈ 12 h); cross-project — one DB file covers every project's domain rows
└── projects/
    └── <project_slug>/
        ├── CLAUDE.md
        ├── decisions/
        ├── working/        # includes knowledge/ + memory/ alongside conversations/handoffs/plans/workspace
        ├── outputs/
        └── uploads/
```

**Excluded from kDrive sync**:
- `projects/<slug>/repo/` — git worktrees; git remote is the source of truth.
- `projects/<slug>/attachments/` — UI paste-target images; local-only by design, would otherwise bloat kDrive with screenshots.
- `/data/harness.db` itself — only its periodic snapshots land in `TOT/snapshots/` (see above).
- `/data/claude/` — OAuth tokens; persisted via Zeabur's `/data` volume, not via kDrive.

### Path constants

[server/paths.py](server/paths.py) (standalone module — keeps main.py from growing further; importers should treat it as the single source of truth and never hardcode `/data/...` strings):

- `global_paths()` → frozen `GlobalPaths` dataclass: `root`, `claude_md`, `skills`, `mcp`, `wiki`, `wiki_index`.
- `project_paths(project_id)` → frozen `ProjectPaths` dataclass: `project_id`, `root`, `claude_md`, `memory`, `decisions`, `knowledge` (resolves under `working/`), `working`, `working_conversations`, `working_handoffs`, `working_plans`, `working_workspace`, `outputs`, `uploads`, `attachments`, `repo`, `bare_clone`, plus method `worktree(slot)` returning the per-slot worktree path under `repo/`.
- `ensure_global_scaffold()` and `ensure_project_scaffold(project_id)` — idempotent boot/create-time directory provisioning. The wiki sub-folder is created via `global_paths().wiki / project_id` (lives in the global wiki tree, not under `projects/<slug>/`). Neither writes `CLAUDE.md` (Phase 6/7 owns the templates) nor clones the repo (activation does that).

All call sites switch from hardcoded `/data/memory/` etc. to the helper:
- `project_paths(active).memory` for project-scoped data
- `global_paths().wiki / project_id` for the per-project wiki sub-folder

---

## 5. Sync strategy
Per-project differential sync, every 5 minutes:

### Sync state

`sync_state(project_id, tree, path, mtime, size_bytes, sha256, last_synced_at)` — see §3 for the full DDL. One row per file pushed to kDrive. The `tree` discriminator (`'project'` or `'wiki'`) tells the sync loop which root the `path` is relative to: project tree (`/data/projects/<slug>/`) or wiki tree (`/data/wiki/<slug>/`).

### Push loop

For the active project:

1. `os.walk` two roots and tag each file with its `tree`:
   - `project_paths(active).root` — skipping `repo/` and `attachments/` (see §4 exclusions). Tree = `'project'`.
   - `global_paths().wiki / active` — the project's wiki sub-folder. Tree = `'wiki'`.
2. For each file: stat → if `mtime > sync_state.mtime` OR `size_bytes` differs → hash → if `sha256` differs → enqueue for PUT.
3. PUTs go through a temp file on kDrive (atomic write) to avoid pushing a partial conversation being appended live.
4. After successful PUT, update the matching `sync_state` row (or insert if first time seen).
5. Files deleted locally (present in `sync_state` but absent on disk) → DELETE on kDrive, then DELETE the `sync_state` row.
6. On any kDrive HTTP error: retry with exponential backoff (1s → 2s → 4s, capped at 30s) up to `HARNESS_KDRIVE_RETRY_MAX` (default 3). After exhaustion, emit a `kdrive_sync_failed` event with the path + status, then continue with remaining files — never abort the whole run on one bad file.

### Pull on open (project switch)

Runs **before** the `active_project_id` pointer swaps, so the first query against the new project sees post-pull state.

1. PROPFIND both kDrive roots: `TOT/projects/<slug>/` and `TOT/wiki/<slug>/`.
2. For each remote file: if remote mtime > local mtime OR local file missing → GET to a temp file, then atomic-rename into place.
3. Update / insert matching `sync_state` rows with the kDrive `(mtime, size_bytes, sha256)`.
4. On kDrive failure: surface as a step error in the §6 busy modal; user picks **Retry** or **Cancel and stay**. The pointer swap does not happen until pull succeeds (or the user chooses to abort).

### Push on close (project switch)

1. Force-flush regardless of the 5-minute timer (calls the Push loop with `force=True`).
2. Mark currently-streaming conversations with `live: true` frontmatter so the next reopen knows they were active when persisted.
3. Block the project switch until push completes. Hard timeout: `HARNESS_KDRIVE_CLOSE_TIMEOUT_S` (default 60s). On timeout: surface in the §6 busy modal as a step error; user picks **Retry**, **Force switch (skip remaining files — they sync on next open)**, or **Cancel switch**.

### Inactive projects

Inactive projects do not sync continuously. They sync only on open / close. This keeps the loop O(active project size), not O(all projects).

### Coverage

**Synced** by the per-project file loop (push every 5 min for active, on open/close for inactive):
- Project tree: `CLAUDE.md`, `decisions/**`, `working/**` (covers `working/{knowledge,memory,conversations,handoffs,plans,workspace}/**`), `outputs/**`, `uploads/**`.
- Wiki tree: `wiki/<slug>/**.md`.

**Synced** by a separate global-tree loop (slower cadence — `HARNESS_GLOBAL_SYNC_INTERVAL`, default 30 min):
- `/data/CLAUDE.md`, `/data/.claude/skills/**`, `/data/mcp/**`, `/data/wiki/INDEX.md`, plus any cross-project `wiki/*.md` entries at the wiki root.

**Synced** by the DB snapshot loop (already exists, 5-min cadence to `TOT/snapshots/`):
- The whole `harness.db`. **Per-project agent identity** (`agent_project_roles`), `agent_sessions`, every project_id-keyed domain row, and the `projects` table itself ride along with these snapshots — no separate file mirror. To restore a project's identity after disaster, restore `harness.db` from snapshot.

**Not synced** (intentional):
- `projects/<slug>/repo/**` — git is the source of truth; clones rebuild on demand.
- `projects/<slug>/attachments/**` — local-only by design (would bloat kDrive with screenshots).
- `/data/harness.db` itself — only its rotated copies in `TOT/snapshots/` (above).
- `/data/claude/` — OAuth tokens persist via Zeabur's `/data` volume, not kDrive.

### Risks

- The harness assumes it is the **sole writer** for active-project files. If a user edits a synced file directly on kDrive while that project is active, the next push iteration will overwrite their edit — there is no conflict detection in v1 (would require a pre-PUT PROPFIND comparing remote ETag against `sync_state.sha256`; deferred). Workaround: edit kDrive-side only while the project is **inactive**, and pull-on-open will bring those edits into the harness on next switch.
- Sync loop interrupted by container restart → next loop catches up via mtime/hash check; no data loss.
- Conversations growing during sync → atomic temp-file write upstream.
- kDrive auth failure or 4xx/5xx storm → `kdrive_sync_failed` events surface as a banner in EnvPane; sync pauses until manual retry or the user updates the token in Options drawer.
- kDrive disk full → same path as auth failure; user must reclaim space on Infomaniak before resume.
- Project deletion mid-sync → the loop re-resolves the active project + checks the project still exists at the start of every iteration; aborts cleanly if the project disappeared.

---

## 6. Project switch UX
### Trigger

Left rail "P" button (currently a placeholder). Click → dropdown:
- Active project at top with checkmark.
- Other projects listed.
- "New project…" at bottom.

### Confirmation modal

Before switch:

```
Switch from "Misc" to "Simaero Rebrand"?

This will:
  • Push current Misc working files to kDrive (12 files, ~340 KB)
  • Snapshot 3 in-progress conversations and push to kDrive
  • Reload UI with Simaero Rebrand: team identity, sessions, conversations

[Cancel]  [Switch]
```

Misc agent sessions and DB rows stay in place — they're just not the *active* project after the switch. No data needs to be deep-copied; the swap is a single `active_project_id` update plus pull-pull-redraw.

**In-flight turn caveat** — if any agent has a turn running when Switch is clicked, the activate endpoint refuses with `423 Locked` and the modal shows: "Coach is mid-turn. Wait or cancel and switch?" with **Wait** / **Cancel turns and switch**. Final policy to be confirmed (see §14 Q2).

### Busy modal

After confirm, full-screen overlay with animated stepper:

```
Switching to "Simaero Rebrand"…

  ✓ Snapshotting Misc live conversations  (3 files tagged `live: true`)
  ✓ Pushing Misc files to kDrive          (12 files, ~340 KB)
  ⟳ Pulling Simaero Rebrand from kDrive   (4/12 files)
  ○ Switching active project pointer
  ○ Loading Simaero Rebrand context       (project CLAUDE.md, team identity, tasks)
  ○ Re-rendering open panes                (or starting fresh if no sessions exist)

  [shimmer animation here]
```

Each step has its own progress (spinner / count / check). Modal cannot be dismissed mid-switch. On error: red row + "Retry" / "Cancel and stay on Misc" buttons.

### Implementation sketch

`POST /api/projects/{id}/activate` returns `{job_id}` on `202 Accepted`. UI subscribes to `bus` for `project_switch_step` events tagged with that job_id. Final `project_switched` event closes the modal.

Status codes:

- `202 Accepted` — switch started, job_id returned, follow on bus.
- `400 Bad Request` — slug fails the `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$` validator.
- `404 Not Found` — no project with that id.
- `409 Conflict` — another switch already in progress (one at a time).
- `423 Locked` — at least one agent has a turn in flight (see in-flight caveat above).
- `502 Bad Gateway` — kDrive unreachable on the pre-pull; modal can offer **Retry** without re-confirming.

### Side effects

**State that swaps on switch:**

- Active project pointer in `team_config` (`active_project_id`).
- All open agent panes reload their conversations from `events` filtered to the new `project_id`.
- Pane headers refresh to show the new project's agent names (loaded from `agent_project_roles`).
- Agent identity — name, role description, brief — loaded from `agent_project_roles` for the new project. Coach can recompose the team per project domain.
- Agent sessions — `agent_sessions` rows for the new project become active; old project's sessions are persisted intact for resume.
- EnvPane sections refresh to new project: tasks, memory, decisions, inbox, escalations. Per-recipient unread counts swap (`messages` now carries `project_id`).
- Repo worktrees rebind: `coord_commit_push` and any tool resolving the project repo now point to `/data/projects/<new-slug>/repo/`. First switch to a never-cloned project triggers a clone (slow); subsequent switches are fast.
- LeftRail dot states recompute against new project's events.
- Files pane: project root swaps to new project's tree; global root unchanged.

**State that survives switch (global):**

- Pane operational settings (model, plan-mode, effort) — per-agent, not per-project.
- The 11 slots themselves (Coach + p1..p10) and their `locked` flags.
- Maximize / column layout / EnvPane open state — UI ergonomics.
- Cost cap counters (per-agent and team daily) — global, do not reset on switch.
- Telegram bridge — keeps forwarding Coach turns regardless of which project is active.
- OAuth tokens, MCP servers, global skills/wiki/CLAUDE.md.

---

## 7. Files pane changes
Two roots, both visible at top level. The bottom root is the **active** project — swap it in place on project switch; the global root above is untouched.

```
🌐 Root (global)
  ├── CLAUDE.md
  ├── skills/
  ├── mcp/
  └── wiki/
      ├── INDEX.md
      ├── <cross-project>.md       # shared concepts at wiki root (alongside INDEX)
      └── <project_slug>/          # one folder per project; only reachable via this global wiki tree (the bottom root contains /data/projects/<slug>/, which has no wiki/ child)

📁 Simaero Rebrand                  # bottom root — swaps when active project changes
  ├── CLAUDE.md
  ├── decisions/
  ├── working/
  │   ├── conversations/
  │   ├── handoffs/
  │   ├── knowledge/
  │   ├── memory/
  │   ├── plans/
  │   └── workspace/
  ├── outputs/
  ├── uploads/
  ├── attachments/                  # local-only per §4; visible for review/cleanup
  └── repo/                         # git worktrees — potentially large; collapsed by default
      ├── .project/                 # bare clone; hidden by default (dot-prefix)
      ├── p1/
      ├── p2/
      └── …
```

### Behavior

- Tree is collapsible per node, persisted in `localStorage`.
- Active project root expands by default; global root starts collapsed.
- `/data/projects/` itself is **not** an enumerable node — only the active project surfaces, under its own root header. The other projects' folders aren't reachable from the files pane (use the project switcher in the left rail to reach them).
- Switching project: the entire bottom root rebinds in place; the existing live fs-event reload re-targets to the new project's tree, so old files disappear and new ones appear without a manual refresh.
- Archived projects do not appear in the bottom root, even if their on-disk folders still exist (per §2 archive semantics, sync also stops touching them).
- `/api/files/roots` returns both roots with their absolute `path` field. The in-app file-link resolver (already shipped) longest-prefix-matches against both. Links in *prior* project conversations to paths under that project's folder no longer resolve after switching away — the resolver returns `null` and the UI falls back to a copyable plain-text path. (Acceptable for v1; agents can re-author links if cross-project references matter.)
- **Agent identity is DB-only**: each project's `agent_project_roles` (name, role description, brief) is not file-backed. The files pane will not show a `team.md`. Edit identity via Coach's `coord_set_player_role` tool or the Options drawer Projects section.

### Roots payload shape

`GET /api/files/roots` →

```json
[
  {"id": "global", "label": "Root (global)", "path": "/data", "scope": "global"},
  {"id": "project", "label": "Simaero Rebrand", "path": "/data/projects/simaero-rebrand", "scope": "project", "project_id": "simaero-rebrand"}
]
```

Adding `scope` and `project_id` lets the UI label the panel headers correctly and tag opened files with their originating scope (so a global-tree edit doesn't get misattributed to the active project in events / audit logs). It also drives the bottom-root rebind on switch — the UI replaces the entry whose `scope === "project"` instead of refetching the whole list.

---

## 8. Global context — what lives at the root
### Global CLAUDE.md

Documents the harness rules **and** the project/wiki architecture. Excerpt (the full file also carries the harness invariants from the original `HARNESS_SPEC.md` — single write-handle, per-worktree isolation, Max-OAuth-only, etc.):

```markdown
# TeamOfTen Harness — Global Rules

You are part of an 11-agent team (1 Coach, 10 Players) working on
one **active project** at a time. Each conversation is scoped to
that one project — when the user switches projects, sessions swap
and the team identity (names, roles, briefs) reloads from the new
project's `agent_project_roles` rows.

## Active project (this conversation)
- Slug: <injected_slug>
- Display name: <injected_name>
- Repo (if any): <injected_repo>

## Project file structure — under /data/projects/<active>/

- `CLAUDE.md`              — project-specific rules, stakeholders, glossary
- `decisions/`             — append-only durable record of "we chose X because Y"
- `working/conversations/` — agent conversation snapshots; `live: true` when persisted mid-session
- `working/handoffs/`      — inter-agent context handoffs
- `working/knowledge/`     — text artifacts via `coord_write_knowledge` (specs, research, design drafts that evolve)
- `working/memory/`        — shared scratchpad via `coord_*_memory` (overwrite-on-update by topic; event log keeps history)
- `working/plans/`         — task breakdowns, drafts
- `working/workspace/`     — generic scratch
- `outputs/`               — binary deliverables; prefer `coord_save_output` for canonical writes
- `uploads/`               — user-uploaded files (read-only, pulled from kDrive); renamed from `inputs/` in projects_v2
- `attachments/`           — UI paste-target images (read via `Read`; local-only, not synced)
- `repo/<your-slot>/`      — your git worktree (Players only; Coach has none)

## Global resources (cross-project)

- `/data/CLAUDE.md`        — these rules
- `/data/.claude/skills/`  — custom skills (including `llm-wiki/`); canonical Claude Code location
- `/data/mcp/`             — global MCP server configs (mirror of DB; v2)
- `/data/wiki/INDEX.md`    — master wiki index (auto-maintained)
- `/data/wiki/<slug>/`     — per-project wiki entries
- `/data/wiki/*.md`        — cross-project shared concepts at wiki root (alongside INDEX.md)

## Wiki principles

- Write a wiki entry when you learn something a future agent
  (in this or another project) would benefit from knowing.
- Granularity: one concept per file, hyperlinked with `[[wikilinks]]`.
- Project-specific learnings → `/data/wiki/<active>/`.
- Cross-project shared concepts → `/data/wiki/` root.
- Format and trigger rules: `/data/.claude/skills/llm-wiki/SKILL.md`.

## Per-project agent identity

Your **name**, **role description**, and **brief** load from
`agent_project_roles` for the active project. The harness
injects them as a separate `## Your identity` block prepended
to every turn's system prompt (this static CLAUDE.md is
appended after — both layers are present every turn). Coach
recomposes the team per project domain via `coord_set_player_role`.
On switch, identity reloads — your name in the next project may
differ from this one. The 11 slot IDs (`coach`, `p1`..`p10`)
themselves are stable across projects.
```

The two-layer injection (dynamic identity block + static CLAUDE.md) is what lets Coach edit a Player's name/role/brief mid-project and have it take effect on the next turn without rewriting any file. The static layer holds rules; the dynamic layer holds identity.

### Global skills

- `llm-wiki/` — bootstrapped from Karpathy's gist (see §9).
- Any custom harness-wide skills the user adds belong here too.

Note: Claude Code built-in skills (`security-review`, `init`, `simplify`, etc.) ship with the CLI itself and are **not** in `/data/.claude/skills/`. The harness only owns custom skills in this folder.

### Global MCP

The runtime reads MCP server configs from the DB's `mcp_servers` table — that's the single source of truth. `/data/mcp/` is reserved for an optional JSON mirror (human-readable, portable across deploys, useful for review).

**Deferred to v2.** v1 ships without the JSON mirror — the folder is created (empty) so the path exists for forward compatibility, but no code reads or writes it. It's mentioned in the global CLAUDE.md excerpt above so agents who learn about MCP via that file aren't surprised if entries appear later.

### Per-project CLAUDE.md

Project-specific. Coach is responsible for keeping it current; harness creates a stub on project creation (only `Goal` and `Repo` are pre-filled from the creation modal — everything else is for Coach to fill as the project unfolds, no auto-populate):

```markdown
# Project: <Name>

## Goal
<short description, from creation modal>

## Repo
<repo_url, if set>

## Stakeholders
<filled in by Coach>

## Team
<filled in by Coach as roles are assigned via coord_set_player_role —
record the intent ("p1 = lead developer, p2 = QA") so future you can
reconstruct why each Player was named what they were named>

## Glossary
<filled in by Coach>

## Conventions
<project-specific rules, code style, terminology, do/don't lists>
```

---

## 9. Wiki bootstrap (revised — no auto-populate)
The harness ensures the **LLM-Wiki skill** is present in `/data/.claude/skills/llm-wiki/`. It does **not** auto-populate wiki entries.

### Bootstrap behavior

On boot, in order:

1. Ensure `/data/wiki/` exists; create if missing.
2. Ensure `/data/wiki/INDEX.md` exists; if missing, write a minimal stub:

   ```markdown
   # Wiki Index

   _Auto-maintained by the harness on every wiki write event (the v1 implementation choice — see §14 Resolved: INDEX.md maintenance). Agents do not edit this file directly._

   ## Cross-project entries

   ## Per-project entries
   ```

3. Ensure `/data/.claude/skills/llm-wiki/` exists; create if missing.
4. Check `/data/.claude/skills/llm-wiki/SKILL.md`. If missing, copy from the checked-in template at `server/templates/llm_wiki_skill.md` (sourced from Karpathy's gist; tracked in the repo, regenerated by hand when Karpathy updates the gist).
5. Surface in `/api/health` under `wiki`:
   - `"present"` — files were already there before boot.
   - `"bootstrapped"` — step 2 or 4 wrote them this boot.
   - `"missing"` — write failed (permissions, disk full); hard error, agents can't record knowledge until resolved.

### Re-bootstrapping

The bootstrap is a *first-write-only* operation — once `SKILL.md` exists, the harness leaves it alone (users can edit it, and we don't want boot to revert their edits). To roll out a new version of the skill:

1. Update `server/templates/llm_wiki_skill.md` in the repo and deploy.
2. Delete `/data/.claude/skills/llm-wiki/SKILL.md` on the live container (or use the **Re-bootstrap LLM-Wiki skill** button in Options drawer, deferred to v2).
3. Restart — bootstrap step 4 sees the absence and rewrites from the new template.

### Skill content (template)

The skill file is a Claude Code skill with YAML frontmatter and a markdown body:

```markdown
---
name: llm-wiki
description: Use when recording learnings, patterns, or knowledge worth preserving across sessions or projects. Triggers when an agent has discovered something a future agent (in this or another project) would benefit from knowing — e.g. an architectural decision, a non-obvious gotcha, a useful reference.
---

# LLM Wiki

(body — Karpathy's pattern adapted to TeamOfTen's directory layout)

## When to create an entry
…

## Format
- One concept per file
- Filename: kebab-case derived from the concept (e.g. `webdav-conflict-detection.md`)
- Frontmatter (YAML): `title`, `tags`, `created`, `updated`, optional `links`
- Body: standard markdown
…

## Where entries go
- Project-specific learning → `/data/wiki/<project_slug>/<entry-filename>.md`
- Cross-project shared concept → `/data/wiki/<entry-filename>.md` (root, alongside INDEX.md)

`<project_slug>` is the project's id (e.g. `misc`, `simaero-rebrand`).
`<entry-filename>` is the kebab-case concept name (e.g. `webdav-conflict-detection`). Disambiguating these two: a project slug names a folder, an entry filename names a `.md` file inside it.

## Linking
…

## Updating INDEX.md
…
```

The skill is a **trigger document** — Claude Code matches the `description` field against the agent's current context and loads the body when relevant. The body then guides the agent through writing the entry.

### Linking — wikilinks vs markdown

Karpathy's gist uses `[[wikilinks]]`. The harness renders standard CommonMark/GFM via `marked` and does not parse double-bracket wikilinks. The skill therefore instructs agents to write **standard markdown links**:

- Within a project's wiki: `[other concept](./other-concept.md)`
- Cross-project: `[../other-project/concept](../other-project/concept.md)`
- To project working files: `[plan A](/data/projects/<slug>/working/plans/a.md)` — these resolve through the in-app file-link resolver (§7).

This is a deliberate deviation from Karpathy's gist, called out in the template body so agents who've seen the original aren't confused.

### What the harness does NOT do

- Generate wiki content automatically.
- Spawn an agent to summarize the codebase.
- Pre-fill `/data/wiki/<slug>/` on project creation (just creates the empty folder).

Content accrues organically as agents work.

---

## 10. Coach startup reminder
The global CLAUDE.md (§8) already injects the active project's slug, name, and repo URL into every agent's system prompt — both Coach and Players. Coach gets **one additional block** that Players don't need: the team composition and current task state for this project. This is what makes Coach the orchestrator instead of just another worker.

The existing "Roster availability" block (which fires only when a Player is locked) becomes a sub-section of this larger block.

### Coach-specific block

Part of the same dynamic per-turn injection layer as `## Your identity` (§8) — for Players that layer ends after identity; for Coach it continues with the coordination block below. The static global CLAUDE.md still gets appended after, unchanged. So the on-the-wire prompt for Coach is: `[identity] + [coordination block] + [global CLAUDE.md]`.

Example shown rendered for the project `simaero-rebrand`; in production every `<...>` style value is filled from `projects`, `agent_project_roles`, `tasks`, `messages`, and the `decisions/` folder:

```text
## Coordinating: Simaero Rebrand

Goal: Refresh brand assets and roll out new template across decks/docs.
(For full project context, read /data/projects/simaero-rebrand/CLAUDE.md
and update it as the project evolves.)

## Team composition (this project)

- coach   — you
- p1      — Alice Rabil    | role: Lead Developer
- p2      — Bob Powell     | role: QA & Tests
- p3      — Carol Gait     | role: Design (LOCKED — unavailable)
- p4..p10 — unassigned (auto-name on first activation; assign via coord_set_player_role)

## Current state

Open tasks (3):
- T-42 (in progress) — p1 — Refresh deck template
- T-43 (claimed)     — p1 — Audit existing brand collateral
- T-44 (claimed)     — p2 — Set up snapshot tests for templates

Inbox: 2 unread messages

Last decision: 2026-04-23 — adopt Tailwind v4
  (/data/projects/simaero-rebrand/decisions/2026-04-23-tailwind-v4.md)

Wiki: /data/wiki/simaero-rebrand/  (master index: /data/wiki/INDEX.md)
```

Block is rebuilt on every Coach turn, so a project switch, a `coord_set_player_role` update, a new task, or a fresh decision all show up immediately on Coach's next turn — no restart, no re-deploy.

### Players don't get this block

Players already have everything they need from two existing layers:

- `## Your identity` (dynamic, per-turn) — their per-project name, role description, brief.
- Global CLAUDE.md (§8) — `## Active project (this conversation)` with slug/name/repo.

A Player doesn't need the team-wide cross-cutting view; they need their own role and the project they're working in. Coach is the only agent that needs the full board.

---

## 11. Repo handling
### Per-project repo

- `projects.repo_url` stores the URL (PAT-in-URL or `${GITHUB_TOKEN}` placeholder pattern, same as today).
- Each project owns its own repo layout under `/data/projects/<slug>/repo/`:
  - `.project/` — bare clone, shared only by this project's worktrees.
  - `<slot>/` — per-Player worktrees (one per `p1`..`p10`; Coach has no worktree).
- **Provisioning is activation-driven, not creation-driven.** `ensure_workspaces()` runs in two situations: at boot (for whatever project is currently active) and on every project switch (for the new active project). It clones if `.project/` is missing, sets up worktrees if any are missing, opens existing worktrees if present. Net effect: a project's repo dirs come into existence the *first time* that project becomes active, then persist on disk for fast re-activation. Project create alone never provisions — an inactive project never has worktrees.
- A first-time activation triggers `git clone --bare` plus worktree provisioning (slow, can take seconds for a large repo). Subsequent activations of the same project just rebind paths and re-open worktrees.
- **Worktree paths move** from `/workspaces/<slot>/project/` (current single-project layout) to `/data/projects/<slug>/repo/<slot>/` (per-project layout).
- **Branch convention** is preserved per project: each Player works on a `work/<slot>` branch in their worktree. Branch resolution still preserves `origin/work/<slot>` history if it already exists upstream.
- `coord_commit_push` resolves to the active project's repo automatically. Coach calling it returns an error (no worktree to commit from).
- **Disk footprint.** A harness with N projects fully populated holds N bare clones + up to 10N Player worktrees. For 1–10 projects this is manageable. If it becomes a problem, archive cold projects (§2) — archiving stops sync but does not delete the repo dirs; only project deletion removes them.

### Default for Misc

On first boot of the refactored harness:
- If `HARNESS_PROJECT_REPO` env is set → copied to `projects.repo_url` for misc.
- After that, env var is no longer read; UI Options drawer is the source of truth.

### Provisioning

`POST /api/team/repo/provision` already exists; rebrand to `POST /api/projects/{id}/repo/provision` and require an explicit `project_id`. The legacy path `POST /api/team/repo/provision` is kept for backward compat and operates on the currently active project.

### Updating a project's repo URL

When a user edits `projects.repo_url` via the Options drawer Projects section:

1. The URL is updated in the DB.
2. If `.project/` already exists for this project, the harness runs `git remote set-url origin <new-url>` on the bare clone only. Worktrees created via `git worktree add` share the bare clone's `.git/config`, so the URL change propagates automatically — no per-worktree command needed.
3. The Options drawer's **provision now** button (§4 / §13) forces a fresh credentials test (a `git ls-remote`) before saving, so a typo doesn't silently break commits later.

If the new URL points to a *different* repo (not just a credentials rotation), the user is advised to delete and recreate the project — the existing worktrees would carry incompatible commit history. The harness does not auto-detect this; it's a user judgment call.

### No-repo projects

A project without `repo_url` is fine — workspaces fall back to plain folders (existing `workspace_dir(slot)` fallback handles this). `coord_commit_push` errors loudly with "no repo configured for this project."

### Cleanup on project deletion

Per §2 Lifecycle Delete: deleting a project tears down its repo cleanly:

1. For each existing Player worktree: `git worktree remove --force <slot>` (against the bare clone).
2. `rm -rf /data/projects/<slug>/repo/.project/`.
3. `rm -rf /data/projects/<slug>/` (sweeps the rest of the project tree).

Tearing worktrees down through git first avoids leaving stale worktree references in the bare clone's metadata — mostly a belt-and-suspenders measure since the bare clone is also being deleted, but it stays correct if the order is ever interrupted partway.

The upstream remote (github.com / wherever) is untouched — only local copies disappear. Any in-flight `coord_commit_push` against the deleted project will fail at the next git operation (worktree path no longer exists) and surface as a turn-level error.

---

## 12. UI changes summary
| Component | Change |
|---|---|
| LeftRail | Replace `P` placeholder with active-project pill + dropdown. Show project name truncated; show full name on hover. |
| Project switcher dropdown | List active (✓) + non-archived others, "New…" at bottom. Edit/archive/delete icons inline per row. |
| Confirmation modal | New component, reusable for switch + delete. Renders dynamic counts (files / live conversations) at open time. |
| Busy modal | New component with stepper animation. Subscribes to `project_switch_step` events for the in-flight job_id; modal cannot be dismissed until success or user-driven cancel. |
| Options drawer → Projects section | **New**. Project cards with: name, repo URL (masked), description, created/last-active timestamps, archive toggle, delete button (Misc's delete disabled). Expand a card to see the project's `agent_project_roles` (name, role, brief per slot) — **read-only here**. To edit: ask Coach to call `coord_set_player_role(slot, name, role)` for name/role; the brief is edited via that slot's pane settings popover (which writes the active project's row only). |
| Options drawer → Sessions section | **Updated**. Existing batch-clear UI now scopes to the active project's `agent_sessions` only; other projects' sessions stay intact and are not shown here. |
| Options drawer → Default models section | **No behavioral change** — model defaults are global per §15 (survive switch). Clarification only: the dropdowns affect every project. |
| Pane header | **Updated**. Agent name displayed reads from `agent_project_roles` for the active project — re-renders on switch and on `coord_set_player_role`. Slot ID (`coach`, `p1`..) shown in tooltip. Current-task chip behavior unchanged. |
| Pane settings popover | **Updated**. The `brief` field now reads/writes `agent_project_roles.brief` for the active project (was `agents.brief`). Saving applies to current project only; switching projects reloads that project's brief. Other fields (model override, plan-mode, effort) remain per-pane local-storage overrides — global, no change. |
| FilesPane | Two-root tree (global + active project). See §7 for tree shape and behavior. |
| EnvPane | All project-scoped sections (tasks, memory, decisions, inbox, escalations) auto-scope to active project once events carry `project_id`. Per-recipient unread counts recompute on switch. The cost-cap banner is **global** (caps span projects per §6) — it does not reset on switch. |

---

## 13. Implementation phases (roadmap; phase work is the real backlog — see status note at end of §13)
Recommend doing these in order — each is independently testable.

### Phase 1 — Schema, filesystem, and identity foundation (completed and audited)
- Add tables (§3): `projects`, `agent_sessions`, `agent_project_roles`, `sync_state`.
- Add `project_id` columns + indexes (`idx_*_project`) to: `tasks`, `messages`, `memory`, `events`, `turns`, `attachments`. Drop & recreate, no migration.
- Drop columns from `agents`: `session_id`, `continuity_note`, `last_exchange_json` (→ `agent_sessions`); `name`, `role`, `brief` (→ `agent_project_roles`). Six columns total.
- Migration script `server/migrations/projects_v1.py` with the steps from §3 (drops, creates, `misc` insert + `active_project_id` pointer, fs scaffold for misc, `schema_version='projects_v1'` stamp at the end).
- Wipe local data dirs (`/data/memory/`, `/data/decisions/`, `/data/events/`, `/data/outputs/`, `/data/knowledge/`, `/data/uploads/`, `/data/attachments/`, `/workspaces/`) and kDrive `TOT/` root recursively.
- `project_paths(project_id)` and `global_paths()` helpers (§4); refactor every hardcoded path call site.
- Identity injection wiring: build the `## Your identity` block from `agent_project_roles` and prepend to every turn's system prompt. For Coach, append the coordination block (foundation for §10; full content in Phase 7).
- `coord_*` tools gain implicit project scoping (read/write to active project rows only). `coord_set_player_role` writes to `agent_project_roles`.
- **Isolation test** (gates Phase 1 done): integration test that creates two projects, writes domain rows under project A, switches to project B, asserts every project-scoped query returns 0 rows from A. This is the §16 risk mitigation made executable.

### Phase 2 — Sync rework (completed and audited)
- Per-project file sync loop (active project, 5 min cadence) — push + pull semantics per §5, walking both `projects/<slug>/` (excl. `repo/`, `attachments/`) and `wiki/<slug>/`.
- Global tree sync loop (`HARNESS_GLOBAL_SYNC_INTERVAL`, default 30 min) — covers `/data/CLAUDE.md`, `/data/.claude/skills/`, `/data/mcp/`, `/data/wiki/INDEX.md`, cross-project wiki entries at `/data/wiki/*.md`.
- `sync_state(project_id, tree, path, mtime, size_bytes, sha256, last_synced_at)` populated; retry with 1s→2s→4s exponential backoff capped at 30s, up to `HARNESS_KDRIVE_RETRY_MAX` (default 3).
- Pull-on-open / force-push-on-close primitives (`HARNESS_KDRIVE_CLOSE_TIMEOUT_S`, default 60s).
- `kdrive_sync_failed` event + EnvPane banner. Skip-and-continue on individual file failures, never abort whole run.

### Phase 3 — Project switch API + minimal UI (completed and audited)
- API endpoints: `POST /api/projects` (create), `GET /api/projects` (list, includes archived), `POST /api/projects/{id}/activate`, `PATCH /api/projects/{id}` (name/description/repo_url), `DELETE /api/projects/{id}`, `POST /api/projects/{id}/repo/provision`.
- Slug validator: `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`, 2–48 chars, plus reserved-name list (§2).
- Status codes per §6: `202 Accepted` (switch started, returns job_id), `400` (slug invalid), `404` (unknown project), `409` (another switch in progress), `423` (agent turn in flight), `502` (kDrive unreachable on pre-pull).
- `project_switch_step` event type for UI streaming; final `project_switched` event closes any subscriber.
- LeftRail dropdown (functional, plain styling — polish in Phase 4).

**Phase 1 audit follow-ups** (2026-04-25 — risks deferred from Phase 1, must land here so the switch endpoint is safe):

- **TOCTOU on `resolve_active_project()`.** Every `coord_*` tool, every API endpoint, and `bus.publish` resolves the active project by opening its own connection at the start of the call. A switch mid-tool-call could split a logical operation across two projects (e.g. `update_memory` SELECT against project A, INSERT into B). Mitigation: hold an in-process `asyncio.Lock` for the duration of the activate handler that blocks all writes via a `_active_project_pinned` context var; tool calls and event publishes that begin while the lock is held use the pinned id.
- **Stale-task watchdog isn't project-scoped.** The query in [server/agents.py](server/agents.py) `stale_task_watch_loop` joins `tasks` against `events` with no `project_id` filter, so it reports stalls across every project to the active project's Coach. Add `WHERE t.project_id = ?` against the active project; the watchdog only nags Coach about the project he's currently coordinating.
- **`crash_recover()` resets task status across all projects.** Status reset is harmless cross-project (every project's stale `in_progress` rows demote to `claimed`), but the count surfaced in logs conflates projects. Acceptable as-is; just note it. No fix required unless it surprises a user.
- **Isolation test should cover `agent_sessions`, `agent_project_roles`, and `sync_state`.** Phase 1's gate exercised only the 5 §3 domain tables. Extend the test in this phase to seed (slot, project) rows in those three tables and assert per-project visibility.
- **Isolation test bypasses production write paths.** It seeds rows via raw SQL, so it would not have caught the four bare `INSERT INTO messages` callers fixed in the Phase 1 audit. Add a smoke test that exercises `coord_send_message` + the AskUserQuestion / ExitPlanMode / Telegram inbound paths against a non-default active project.

### Phase 4 — Switch UX polish (completed and audited)
- Confirmation modal with dynamic counts (files to push, live conversations to snapshot).
- Busy modal with stepper animation, subscribed to `project_switch_step` events for the in-flight job_id.
- Error states / retry / cancel-and-stay paths per §6.
- In-flight turn handling: if `423` returned, show sub-modal "Coach is mid-turn — wait or cancel?" with **Wait** / **Cancel turns and switch** (final policy in §14 Q2).

### Phase 5 — Files pane two-root layout (completed and audited)
- `/api/files/roots` returns both roots with `id`, `label`, `path`, `scope`, `project_id` (§7).
- Tree component renders global + active project trees side-by-side; collapse state per node persisted to `localStorage`.
- File-link resolver longest-prefix-matches against both roots; opened files tagged with originating scope.
- Live fs-event reload re-targets to the new project's tree on switch (rebind without full refetch).
- Archived projects suppressed from the bottom root.

### Phase 6 — Global CLAUDE.md + LLM-Wiki skill + INDEX.md bootstrap (completed and audited)
- `server/templates/global_claude_md.md` — full text per §8 excerpt (project/wiki principles + per-project agent identity block).
- `server/templates/llm_wiki_skill.md` — from Karpathy's gist, adapted to standard markdown links instead of wikilinks (§9).
- Boot sequence (§9 bootstrap behavior): ensure `/data/wiki/`, write `INDEX.md` stub if missing, ensure `/data/.claude/skills/llm-wiki/`, write `SKILL.md` if missing, ensure `/data/CLAUDE.md`, write from template if missing.
- `/api/health` adds `wiki: "present" | "bootstrapped" | "missing"`.

### Phase 7 — Coach coordination block + per-project CLAUDE.md stub (completed and audited)
- Coach's per-turn coordination block (§10) — built from `projects`, `agent_project_roles`, `tasks`, `messages`, and the latest entry in `decisions/`; injected as the second sub-block of the dynamic prompt layer.
- Per-project CLAUDE.md stub auto-written on project creation (§8) with Goal + Repo pre-filled from creation modal; rest blank for Coach to fill.
- INDEX.md auto-update on every wiki write event (per §14 Resolved: INDEX.md maintenance — append a link line, sort grouped by project / cross-project).

### Implementation status (2026-04-26)

- **Phases 1–7** — complete and audited (see per-phase headers above).
- **Phase 8** — complete; awaiting audit pass.

### Phase 8 — Options drawer Projects section + per-project brief edit (completed)
- Projects section: project cards with create / edit (name, repo_url, description) / archive toggle / delete (Misc undeletable). Expand a card to view the project's `agent_project_roles` (read-only).
- Pane settings popover update: `brief` field reads/writes `agent_project_roles.brief` for the active project (was `agents.brief`).
- API update: existing `PUT /api/agents/{slot}/brief` re-targets to `agent_project_roles` for the active project; existing `coord_set_player_role` likewise. Both gain implicit `WHERE project_id = active`.
- Sessions section update: batch-clear scopes to active project's `agent_sessions` rows only.
- **Provision now** button (already exists in current Repo section as a single-repo trigger) — adapted to call `POST /api/projects/{id}/repo/provision` and operate per-project. Surfaces on each project card so users can pre-clone before first switch if they want to avoid the slow first-activation.

---

## 14. Open questions / decisions to confirm
### Resolved during spec drafting

These were called out as questions early but became settled through subsequent audit passes. Listed here for traceability so reviewers don't try to re-litigate:

- **Wiki INDEX.md maintenance** → **automatic** on every wiki write event (§9 stub language, §13 Phase 7). Agents do not edit it directly; the harness owns it.
- **Snapshot retention per project or global** → **global**. One `harness.db` serves every project, so one snapshot covers all rows. The snapshot loop stays O(1) regardless of project count (§4, §5 Coverage).
- **Project deletion sync vs async** → **synchronous** with the §6 busy modal pattern. The git-correct cleanup procedure (worktrees → bare clone → rm -rf) is in §11.

### Still open — recommend resolving before Phase 3 ships

1. **Project slug format.** Auto-derive from name (lowercase, replace spaces with dashes, drop chars not in the §2 regex) at create-modal-time, then show as an editable field the user can override before save? Or always require explicit user input? Recommend auto-derive with override — fewest keystrokes for the common case. Edge case to handle either way: a derived slug that hits the §2 reserved list (`skills`, `wiki`, `mcp`, `projects`, `snapshots`, `harness`, `data`, `claude`) — the form must validate live and force the user to edit; quietly mangling the slug ("wiki" → "wiki-1") would be surprising.
2. **Cancel semantics for in-flight turns on switch.** §6 commits to `423 Locked` with a **Wait** / **Cancel turns and switch** sub-modal. Open question is what *cancel* means: hard-cancel via the existing `agent_cancelled` path (turn dies immediately, in-progress tool calls discarded), or graceful abort at the next tool-use boundary? Hard is faster and matches user intent (they explicitly chose to switch away); graceful is gentler but unbounded in latency. Recommend hard cancel.
3. **kDrive folder name.** Keep `TOT/` or rename to `TeamOfTen/` now that we're wiping kDrive anyway. Pure preference — `TOT/` is shorter; `TeamOfTen/` is self-documenting. Recommend keeping `TOT/` to avoid widening the rename surface beyond what's required.
4. **First-time UX.** A new harness install boots into Misc with no other projects and no repo. Show an onboarding banner ("Create your first real project →" linking the switcher dropdown), or trust the user to find it? Recommend trust — Misc is fine for everyday quick needs and the LeftRail pill is discoverable. Revisit if real first-time users get stuck.

---

## 15. What this does NOT change
- **11-agent roster** (Coach + p1..p10). The slot IDs are global, stable across projects.
- **Per-agent operational settings** (survive every switch): the `agents.model` per-agent column, the `locked` flag, `allowed_extra_tools`. Pane-level overrides (model / plan-mode / effort) survive too — they're in browser `localStorage`, not project-scoped.
- **Per-role default models** also survive switches — they live in `team_config` (one Coach default, one Players default, applying to whichever project is active).
- ⚠ Note that `name`, `role`, and `brief` — which were per-agent in the pre-refactor harness — are NOT in the survives-switch list anymore. They moved to `agent_project_roles` (per-project) and swap on every switch (§3, §6). Same for `session_id` / `continuity_note` / `last_exchange_json` — moved to `agent_sessions`.
- **Cost caps**. Per-agent and team daily caps remain **global** (one budget covers all projects). Per-project caps are out of scope for v1. Path forward if needed later: add a `projects.daily_cap_usd` column and OR it into the pre-spawn check — non-trivial but localized.
- **Telegram bridge**. Forwards Coach turns regardless of which project is active. The Coach side may behave differently per project (different team, different goal), but the bridge itself doesn't filter.
- **OAuth / `CLAUDE_CONFIG_DIR` / Max-plan auth model**. Single login serves all projects.
- **Event log retention** (30d default). Events now carry `project_id`, but the trim job is global — it deletes anything older than 30 days across all projects in one pass.
- **Slash commands**. Most operate on the active project implicitly (e.g. `/clear` clears the active project's session, `/compact` summarizes the active project's session, `/brief` edits the active project's row in `agent_project_roles`). Globally-scoped commands: `/loop` and `/tick` configure the Coach autoloop (one harness-wide cadence), `/spend` reads the global cost-cap counters, `/tools` toggles team-wide WebSearch/WebFetch (one toggle for the whole harness), `/help` and `/status` are informational and stateless.

---

## 16. Risks
| Risk | Mitigation |
|---|---|
| Project switch mid-conversation = lost context | Confirmation modal lists live conversations + counts; busy modal blocks until persist completes; live conversations get `live: true` frontmatter so resume is unambiguous (§5, §6). |
| Active agent turn in flight when user clicks Switch | `POST /api/projects/{id}/activate` returns `423 Locked`; busy modal sub-prompt offers **Wait** or **Cancel turns and switch**. Final cancel semantics per §14 Still open Q2. |
| Sync loop slow for large projects | 5-min cadence + differential by mtime+hash; only active project syncs continuously (inactive projects sync only on open/close). |
| kDrive sole-writer assumption violated (user edits a synced file directly on kDrive while project is active) | Documented limitation (§5) — harness will overwrite on next push iteration. Workaround: edit kDrive-side only while the project is **inactive**; pull-on-open picks up the changes on next switch. v2 could add pre-PUT PROPFIND conflict detection. |
| kDrive auth failure, 4xx/5xx storm, or disk full | Per-file retry 3× with exponential backoff (1s/2s/4s, cap 30s); on exhaustion emits `kdrive_sync_failed` and surfaces as EnvPane banner; sync pauses until manual retry / token rotation / space reclaim (§5). |
| User deletes Misc by mistake | Misc not deletable — UI button disabled; `DELETE /api/projects/misc` returns `403 Forbidden` ("misc is the fallback active project and cannot be deleted"). |
| Wrong project_id leaks via missing scope filter | Add `WHERE project_id = ?` in every domain query; gated by an integration test in Phase 1 that writes to project A, switches to B, asserts empty result for every project-scoped query (§13). |
| Repo first-time clone is slow | Lazy provisioning (§11) — first activation pays the clone cost, subsequent activations are fast. Optionally pre-provision via the **Provision now** button on a project card before the user needs to switch. |
| Disk footprint grows with many projects (each holds up to 10 worktrees + a bare clone) | Archive cold projects (§2) to stop sync — does not free disk. To free disk, delete the project (irreversible). For the personal-harness target of 1–10 projects, footprint is manageable. |
| Per-project agent identity (`agent_project_roles`) is DB-only — no on-disk mirror | DB snapshots flush to `TOT/snapshots/` every 5 min (§5 Coverage); restore from a snapshot recovers identity. If both DB and snapshots are lost, identity is lost — Coach can re-issue `coord_set_player_role` to rebuild from scratch. |
| Misc auto-creation fails on first boot (permissions, disk full) | Migration script logs the failure loudly; `/api/health` returns 503 with the failure reason; container restart loop retries. No project-scoped query can succeed without Misc — hard error visible in Zeabur logs immediately. |
| kDrive folder rename breaks existing deploys | Migration policy is destructive — old folders wiped before new layout writes (§3). |

---

End of spec. Review and mark up; I'll fold revisions in before any code lands.

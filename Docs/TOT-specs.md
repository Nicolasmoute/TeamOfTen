# TeamOfTen - Full Specs

Current integrated specification for the TeamOfTen harness.

This document folds the original harness spec, the Projects refactor, and the
current implementation into one reference. It is intentionally implementation-
aware: when the code has moved ahead of an older design note, this file follows
the code. When the code still has a hybrid or inconsistent area, that is called
out explicitly.

Companion references:

- `CLAUDE.md`: working notes and constraints for agents editing this repo.
- `README.md`: operator-facing overview and quick start.

Dependent specs (subordinate to this document):

- `Docs/CODEX_RUNTIME_SPEC.md` — Codex runtime specifics. This file
  assumes the Claude runtime; Codex is the alternate runtime and its
  behavior, schema additions, error handling, and lifecycle live in
  the dependent doc.
- `Docs/recurrence-specs.md` — Coach recurrence model (tick / repeat
  / cron) and project artifacts (`coach-todos.md`,
  `project-objectives.md`).
- `Docs/compass-specs.md` — Compass autonomous strategy engine
  (lattice, regions, truth corpus, audits, briefings).

These docs are subordinate: when a dependent disagrees with this one,
TOT-specs.md wins. Dependents may go deeper on their own subject but
cannot redefine fields, endpoints, events, or invariants declared
here.

Last audited from the repository on 2026-04-26.

---

## 1. Product Vision

TeamOfTen is a personal orchestration harness for running one coordinating
Claude Code agent, called Coach, plus up to ten worker Claude Code agents,
called Players.

The point of the app is not to hide the agents behind an opaque pipeline. The
point is to make their work visible, steerable, and auditable:

1. Coach receives goals, decomposes them, creates tasks, assigns Players, and
   synthesizes progress.
2. Players execute in their own slots and report back through shared tools.
3. The human can watch every pane, intervene in any agent, pause/cancel work,
   inspect shared state, edit files, configure integrations, and switch between
   projects.
4. Durable human-readable outputs are plain files under `/data` and optionally
   mirrored to a WebDAV cloud folder.
5. Hot state lives in one SQLite database controlled by one FastAPI process.

Primary goals:

- Run one Coach plus ten Players on a single VPS/container.
- Use Claude Code / Claude Agent SDK with OAuth credentials persisted on the
  `/data` volume, not API-key billing.
- Keep all coordination transparent through events, panes, tasks, messages,
  memory, and file browsers.
- Support many projects in one harness, with one active project at a time.
- Keep the system small enough to understand: FastAPI, SQLite, Preact, static
  files, no distributed control plane.

Explicit non-goals:

- Multi-user or multi-tenant security.
- Enterprise RBAC/compliance.
- A model-provider abstraction layer.
- Hiding planning and execution behind a black-box "team" abstraction.
- Fully automatic app building without human supervision.

---

## 2. Repository Shape

Top-level layout:

```text
TeamOfTen/
  README.md
  CLAUDE.md
  Dockerfile
  pyproject.toml
  uv.lock
  .env.example
  mcp-servers.example.json
  Docs/
    TOT-specs.md
  server/
    main.py
    agents.py
    tools.py
    db.py
    events.py
    paths.py
    files.py
    project_sync.py
    projects_api.py
    workspaces.py
    webdav.py
    sync.py
    context.py
    knowledge.py
    outputs.py
    interactions.py
    mcp_config.py
    secrets.py
    telegram.py
    static/
      index.html
      app.js
      markdown.js
      style.css
      tools.js
      compass.js
      compass.css
      files.js
      vendor/
    templates/
      global_claude_md.md
      llm_wiki_skill.md
    tests/
  scripts/
  spike/
```

Main implementation responsibilities:

- `server/main.py`: FastAPI app, REST API, WebSocket, lifespan startup and
  background-task orchestration.
- `server/agents.py`: Claude Agent SDK runner, session management, cost caps,
  compacting, autowake, Coach loops, stale-task watchdog.
- `server/tools.py`: in-process MCP coordination server and all `coord_*`
  tools.
- `server/db.py`: SQLite schema, DB helpers, active-project resolution.
- `server/projects_api.py`: project CRUD, switch preview, project activation,
  per-project role view, per-project repo provision endpoint.
- `server/paths.py`: canonical `/data` global/project filesystem layout,
  bootstrap resources, wiki index builder.
- `server/project_sync.py`: active-project and global WebDAV file sync.
- `server/events.py`: in-process event bus plus batched SQLite event writer.
- `server/static/app.js`: no-build Preact SPA.
- `server/static/markdown.js`: single-chokepoint markdown render
  pipeline (marked GFM → KaTeX inline+block math → DOMPurify with
  html+mathMl profiles → mermaid post-render via MutationObserver).
  Every consumer that displays markdown — agent panes, files `.md`
  preview, compass briefings, decisions, wiki entries — routes
  through `renderMarkdown` here, so adding a new renderer (PlantUML,
  GraphViz, alternative math engine) lights it up everywhere with
  no per-consumer changes.

---

## 3. Tech Stack

| Layer | Current choice |
| --- | --- |
| Agent runtime | Claude Agent SDK and Claude Code CLI |
| Backend | FastAPI, asyncio, WebSocket |
| Database | SQLite via `aiosqlite`, DELETE journal mode |
| Frontend | Preact 10, htm, Split.js, vendored marked + DOMPurify + highlight.js + diff + KaTeX + mermaid |
| Durable mirror | WebDAV via `webdav4` |
| Auth to Claude | Claude CLI OAuth credentials in `CLAUDE_CONFIG_DIR` |
| UI auth | Optional bearer token from `HARNESS_TOKEN` |
| Secrets | Fernet-encrypted SQLite table keyed by `HARNESS_SECRETS_KEY` |
| Deployment | Single Dockerfile, Python 3.12 slim, Node 20, Claude Code npm package |
| Tests | pytest, pytest-asyncio |

Important deployment decisions:

- The Dockerfile installs Claude Code with `npm install -g @anthropic-ai/claude-code`
  because the upstream install script has been unreliable/geoblocked in some
  deploy regions.
- The image deliberately does not create `/data`; mounted volumes over an
  existing `/data` path caused SQLite startup hangs on Zeabur.
- SQLite uses DELETE journal mode, not WAL, because WAL was unreliable on the
  target volume backend.
- Static assets are served directly from `server/static`; no frontend build
  step exists.
- The image installs `ripgrep` alongside `git` so Codex Players (which
  use the native `shell` tool to grep) don't fall back to the much
  slower `find` on every search. Claude Players bundle ripgrep behind
  the SDK's `Grep` tool so they were unaffected; this gap only
  surfaced when Codex agents hit it directly via `shell`.
- The image installs the harness with `pip install ".[dev]"` rather
  than just `.`, which adds `pytest` + `pytest-asyncio` to
  `/usr/local/bin`. Same rationale as ripgrep: Codex Players reach
  for `pytest` directly via `shell`, and a missing binary turns
  into a multi-turn detour while the agent investigates the env.
  The dev extras are tiny and version-pinned in `pyproject.toml`, so
  shipping them costs almost nothing and removes one common
  failure mode. Project repos that bring their own pytest still win
  via venv activation; the system pytest is a fallback.

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
4. `swap_pointer`: set `team_config.active_project_id`.
5. `reload`: emit terminal `project_switched`.

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

### 4.6 Current Project Repo Caveat

The schema and API include `projects.repo_url` and per-project repo provision.
However, `server/workspaces.py` still uses the legacy workspace layout:

```text
/workspaces/.project
/workspaces/<slot>/project
```

It does not yet use the per-project path declared in `server.paths`:

```text
/data/projects/<slug>/repo/.project
/data/projects/<slug>/repo/<slot>
```

`POST /api/projects/{id}/repo/provision` temporarily sets
`HARNESS_PROJECT_REPO` and pins the active project while calling the legacy
`ensure_workspaces()`. This lets the UI provision a selected project's repo
URL, but the underlying worktree storage is still global `/workspaces`, not a
fully isolated per-project repo tree.

Implication: project state is project-scoped in the database and file browser,
but code worktrees are still a hybrid area and can collide across projects if
the operator switches repos frequently.

---

## 5. Agent Roster and Governance

The roster is fixed:

| Slot | Kind | Notes |
| --- | --- | --- |
| `coach` | Coach | Coordinator, planner, delegator |
| `p1` ... `p10` | Player | Worker slots |

The slot ids are global and stable across projects. Identity is project-scoped:

- `agent_project_roles(slot, project_id, name, role, brief)`

Operational state is global:

- `agents.status`
- `agents.current_task_id`
- `agents.model`
- `agents.workspace_path`
- `agents.locked`
- `agents.allowed_extra_tools`

Sessions are project-scoped:

- `agent_sessions(slot, project_id, session_id, continuity_note,
  last_exchange_json, last_active)`

### 5.1 Coach Responsibilities

Coach:

- Reads human goals and Player reports.
- Creates top-level tasks.
- Assigns Players using `coord_assign_task`.
- Assigns player names/roles with `coord_set_player_role`.
- Tunes per-Player execution knobs via
  `coord_set_player_runtime` / `coord_set_player_model` /
  `coord_set_player_effort` / `coord_set_player_plan_mode`. Reads the
  current state with `coord_get_player_settings` (one slot or whole
  roster) before changing anything so the team doesn't churn already-
  correct settings. The four tools mutate per-(slot, project)
  override columns; resolution at spawn time is per-pane request →
  Coach override → team-level role default (Settings drawer) →
  hardcoded role default (`models_catalog`: `latest_opus` for Coach,
  `latest_sonnet` for Players, medium effort, plan-mode off) → SDK
  default. Coach overrides apply uniformly to auto-wake spawns (task
  assignments, direct messages) and to direct human prompts that
  don't set a per-pane value.
- Writes decisions.
- Monitors stalled work.
- Answers Player plan/question interactions routed to Coach.
- Does not write code directly.

Coach has read tools plus coordination tools and interactive tools:

```text
Read, Grep, Glob, ToolSearch
coord_* tools
AskUserQuestion
```

Coach does not receive `Write`, `Edit`, or `Bash` in its role baseline.

### 5.2 Player Responsibilities

Players:

- Read inbox and task board.
- Claim or execute assigned tasks.
- Work in their slot workspace.
- Use `Write`, `Edit`, `Bash` for code/file work.
- Update tasks and shared memory.
- Write knowledge artifacts.
- Commit and push work.
- Ask Coach/human for help when blocked.

Players can message peers but cannot assign work to peers.

### 5.3 Structural Enforcement

Hard enforcement in `server/tools.py`:

- Only Coach can directly assign tasks to Players.
- Only Coach can assign player names/roles.
- Only Coach can write decisions.
- Only Coach can set per-Player runtime/model/effort/plan-mode
  overrides; the corresponding `coord_set_player_*` tools reject
  Player callers and `coord_get_player_settings` is Coach-only as
  well. The MCP tools accept `p1..p10` for `player_id`; Coach's own
  effort/plan-mode have no MCP path (the human controls them via
  Coach's pane gear).
- Only Players can claim tasks.
- Coach cannot use standard mutating tools through the baseline allowlist.
- Players can only create subtasks under a task they own; only Coach or human
  can create top-level tasks.
- Task updates are limited to task owner, with Coach allowed to cancel.
- Locked Players cannot receive direct Coach assignments/messages; they skip
  Coach-sourced inbox reads.

Soft enforcement:

- Role system prompts describe Coach as delegator and Players as executors.
- Prompt suffix injects identity, project context, and governance docs.

### 5.4 Player Lock

`agents.locked` is a global per-slot flag, controlled by:

- `PUT /api/agents/{agent_id}/locked`
- Pane lock button.

When a Player is locked:

- Coach `coord_assign_task` to that Player fails.
- Coach direct `coord_send_message` to that Player fails.
- Coach broadcasts can be queued, but locked Players filter them out when
  calling `coord_read_inbox`.
- Human prompts and peer messages still pass.
- The Player can still read shared docs and work when directly prompted by the
  human.

---

## 6. SQLite Data Model

Database path defaults to:

```text
HARNESS_DB_PATH=/data/harness.db
```

Connections use:

- `PRAGMA journal_mode = DELETE`
- `PRAGMA foreign_keys = ON`
- `aiosqlite.Row` row factory for configured connections

### 6.1 `projects`

```sql
CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  repo_url TEXT,
  description TEXT,
  archived INTEGER NOT NULL DEFAULT 0
);
```

### 6.2 `agents`

Global slot roster and runtime status.

```sql
CREATE TABLE agents (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('coach', 'player')),
  status TEXT NOT NULL DEFAULT 'stopped'
    CHECK (status IN ('stopped', 'idle', 'working', 'waiting', 'error')),
  current_task_id TEXT,
  model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
  workspace_path TEXT NOT NULL,
  cost_estimate_usd REAL NOT NULL DEFAULT 0.0,
  started_at TEXT,
  last_heartbeat TEXT,
  allowed_extra_tools TEXT,
  locked INTEGER NOT NULL DEFAULT 0,
  runtime_override TEXT
    CHECK (runtime_override IS NULL
           OR runtime_override IN ('claude','codex'))
);
```

`runtime_override` is the slot-level runtime preference. Resolution at
spawn: `agents.runtime_override` (if set) → role default in
`team_config` → `'claude'`. See `Docs/CODEX_RUNTIME_SPEC.md` §B.1.

Seed rows:

- `coach`, kind `coach`, workspace `/workspaces/coach`
- `p1` to `p10`, kind `player`, workspace `/workspaces/pN`

Per-(slot, project) identity (`name`, `role`, `brief`) lives in
`agent_project_roles`; per-(slot, project) session state
(`session_id`, `continuity_note`, `last_exchange_json`) lives in
`agent_sessions`.

### 6.3 `agent_project_roles`

Per-project identity and prompt addendum.

```sql
CREATE TABLE agent_project_roles (
  slot TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT,
  role TEXT,
  brief TEXT,
  model_override TEXT,
  effort_override INTEGER,       -- 1..4 → low/medium/high/max
  plan_mode_override INTEGER,    -- 0/1 → off/on
  PRIMARY KEY (slot, project_id)
);
```

Notes:

- Coach identity is seeded for `misc`: `Coach`, `Team captain`.
- Players are auto-named on first spawn if no name exists.
- Brief max from API: 8000 chars.
- Name/role can be written by Coach (`coord_set_player_role`) or human
  (`PUT /api/agents/{id}/identity`).
- `model_override` is Coach-set via `coord_set_player_model`. NULL when
  unset. Sits between the per-pane human override and the runtime-aware
  per-role default in `run_agent`'s resolution chain. The tool validates
  against the player's current runtime (Claude vs Codex); a stored
  override that no longer matches the runtime is silently dropped at
  spawn time so a runtime flip can't break a turn.
- `effort_override` (1..4) and `plan_mode_override` (0/1) are
  Coach-set via `coord_set_player_effort` / `coord_set_player_plan_mode`.
  NULL when unset. Both follow the same precedence as `model_override`:
  per-pane request value (highest) → this column → role-level default
  (`models_catalog._ROLE_EFFORT_DEFAULTS`: medium for both Coach and
  Players; `_ROLE_PLAN_MODE_DEFAULTS`: off for both). The Coach layer
  is what makes auto-wake spawns (task assignments, direct messages —
  which call `run_agent` with the kwargs unset) honor the preference;
  per-pane settings only apply to direct human prompts.

### 6.4 `agent_sessions`

Per-project Claude session state and compact handoff state.

```sql
CREATE TABLE agent_sessions (
  slot TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  session_id TEXT,
  last_active TEXT,
  continuity_note TEXT,
  last_exchange_json TEXT,
  codex_thread_id TEXT,
  PRIMARY KEY (slot, project_id)
);
```

Session state:

- `session_id`: latest Claude SDK session id for resume.
- `codex_thread_id`: Codex thread id for resume (independent column so
  switching runtimes back and forth preserves both continuations —
  see `Docs/CODEX_RUNTIME_SPEC.md` §B.1).
- `continuity_note`: summary generated by `/compact`.
- `last_exchange_json`: bounded rolling log of recent prompt/response
  exchanges to inject after compact.

### 6.5 `tasks`

Project-scoped task board.

```sql
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open'
    CHECK (status IN ('open', 'claimed', 'in_progress', 'blocked', 'done', 'cancelled')),
  owner TEXT REFERENCES agents(id),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  claimed_at TEXT,
  completed_at TEXT,
  parent_id TEXT REFERENCES tasks(id),
  priority TEXT NOT NULL DEFAULT 'normal'
    CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
  tags TEXT NOT NULL DEFAULT '[]',
  artifacts TEXT NOT NULL DEFAULT '[]'
);
```

Indexes:

- `idx_tasks_status`
- `idx_tasks_owner`
- `idx_tasks_parent`
- `idx_tasks_project`

Task ids:

```text
t-YYYY-MM-DD-<8 hex chars>
```

State machine:

```text
open        -> claimed, cancelled
claimed     -> in_progress, blocked, done, cancelled
in_progress -> blocked, done, cancelled
blocked     -> in_progress, cancelled
done        -> terminal
cancelled   -> terminal
```

Completing or cancelling a task clears the owner's `current_task_id`.

### 6.6 `messages`

Project-scoped inbox messages.

```sql
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  from_id TEXT NOT NULL,
  to_id TEXT NOT NULL,
  subject TEXT,
  body TEXT NOT NULL,
  sent_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  read_at TEXT,
  in_reply_to INTEGER REFERENCES messages(id),
  priority TEXT NOT NULL DEFAULT 'normal'
    CHECK (priority IN ('normal', 'interrupt'))
);
```

Indexes:

- `idx_messages_to`
- `idx_messages_from`
- `idx_messages_project`

`read_at` is legacy. Per-recipient reads are tracked in `message_reads`.

Valid recipients:

- `coach`
- `p1` to `p10`
- `broadcast`

### 6.7 `message_reads`

Per-recipient read tracking.

```sql
CREATE TABLE message_reads (
  message_id INTEGER NOT NULL REFERENCES messages(id),
  agent_id TEXT NOT NULL,
  read_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY (message_id, agent_id)
);
```

Index:

- `idx_msgreads_agent`

This fixes broadcast semantics: the first reader does not mark a broadcast
read for everyone.

### 6.8 `memory_docs`

Project-scoped shared scratchpad.

```sql
CREATE TABLE memory_docs (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  topic TEXT NOT NULL,
  content TEXT NOT NULL,
  last_updated TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_updated_by TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (project_id, topic)
);
```

Index:

- `idx_memory_project`

Topic regex:

```text
^[a-z0-9][a-z0-9-]{0,63}$
```

Content max:

- Agent tool and human API: 20,000 chars.

Memory is overwrite-on-update. History is in events, not versions.

### 6.9 `events`

Project-scoped append-only audit log.

```sql
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  payload_to TEXT GENERATED ALWAYS AS (json_extract(payload, '$.to')) VIRTUAL,
  payload_owner TEXT GENERATED ALWAYS AS (json_extract(payload, '$.owner')) VIRTUAL
);
```

Indexes:

- `idx_events_agent`
- `idx_events_type`
- `idx_events_project`
- `idx_events_agent_type`
- `idx_events_type_id`
- `idx_events_to`
- `idx_events_owner`

Generated columns support fast pane-history fan-out for:

- messages addressed to the pane's agent
- task assignments to that agent
- task updates whose owner is that agent
- Coach-set per-Player overrides keyed off `payload_to`:
  `agent_model_set`, `agent_effort_set`, `agent_plan_mode_set`. The
  override-setting tools emit with `to: <player_id>` so a history
  reload of the target's pane includes them alongside Coach's own
  timeline copy.

Transient events not persisted:

- `text_delta`
- `thinking_delta`

They still stream over WebSocket.

### 6.10 `turns`

Project-scoped per-turn ledger for spend, duration, context, and analytics.

```sql
CREATE TABLE turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  duration_ms INTEGER,
  cost_usd REAL,
  session_id TEXT,
  num_turns INTEGER,
  stop_reason TEXT,
  is_error INTEGER NOT NULL DEFAULT 0,
  model TEXT,
  plan_mode INTEGER NOT NULL DEFAULT 0,
  effort INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_creation_tokens INTEGER,
  runtime TEXT NOT NULL DEFAULT 'claude',
  cost_basis TEXT
);
```

`runtime` records which per-agent runtime executed the turn ('claude'
or 'codex'). `cost_basis` is `'token_priced'` (cost_usd populated) or
`'plan_included'` (ChatGPT-auth Codex; cost_usd = 0). See
`Docs/CODEX_RUNTIME_SPEC.md` §G.

Indexes:

- `idx_turns_agent`
- `idx_turns_ended_at`
- `idx_turns_project`

Rows are inserted for completed SDK result messages. Turns that crash before a
result are represented in events, not here.

Cost caps are currently based on this table through `_today_spend(agent_id?,
project_id?)`. The function aggregates `cost_usd` for rows where
`ended_at >= MAX(today_utc_start, cost_reset_at,
cost_reset_at_<project_id>)`. The two reset timestamps live in
`team_config` and can be moved forward via `POST /api/turns/reset` —
that gives the team fresh headroom for the rest of the UTC day without
deleting historical rows. When no `project_id` is passed,
`_today_spend()` honors per-project resets per row (each turn picks its
project's reset timestamp via a SQL CASE), so the team total equals the
sum of per-project today values — clicking "Reset" on a single project
reduces the team total by exactly that project's pre-reset spend.

### 6.11 `team_config`

Global key/value store.

```sql
CREATE TABLE team_config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
```

Known keys:

- `active_project_id`: current project slug.
- `extra_tools`: JSON array, team-wide SDK extras such as `WebSearch`.
- `coach_default_model`: JSON string.
- `players_default_model`: JSON string.
- `coach_default_model_codex`: JSON string, Codex-only Coach model default.
- `players_default_model_codex`: JSON string, Codex-only Player model default.
- `coach_default_runtime`: JSON string, role default runtime (`claude`, `codex`, or empty).
- `players_default_runtime`: JSON string, role default runtime (`claude`, `codex`, or empty).
- `project_repo`: legacy/global repo URL override.
- `project_branch`: legacy/global branch override.
- `telegram_disabled`: `"1"` disables Telegram even if env fallback exists.
- `observed_context_windows`: stored model context estimates observed at runtime.

### 6.12 `mcp_servers`

DB-backed external MCP server configs.

```sql
CREATE TABLE mcp_servers (
  name TEXT PRIMARY KEY,
  config_json TEXT NOT NULL,
  allowed_tools_json TEXT NOT NULL DEFAULT '[]',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  last_ok INTEGER,
  last_error TEXT,
  last_tested_at TEXT
);
```

DB configs are loaded after `HARNESS_MCP_CONFIG` file configs and override on
server-name collision.

### 6.13 `secrets`

Encrypted UI-managed secrets.

```sql
CREATE TABLE secrets (
  name TEXT PRIMARY KEY,
  ciphertext BLOB NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
```

The Fernet master key is only in `HARNESS_SECRETS_KEY`; it is never stored in
the database. A DB snapshot without that key cannot decrypt secrets.

### 6.14 `sync_state`

WebDAV sync tracker.

```sql
CREATE TABLE sync_state (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  tree TEXT NOT NULL CHECK (tree IN ('project', 'wiki', 'global')),
  path TEXT NOT NULL,
  mtime REAL NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  last_synced_at TEXT NOT NULL,
  PRIMARY KEY (project_id, tree, path)
);
```

`tree` values:

- `project`: `/data/projects/<slug>/` -> `projects/<slug>/`
- `wiki`: `/data/wiki/<slug>/` -> `wiki/<slug>/`
- `global`: selected global files -> same relative WebDAV paths

Global rows use `misc` as the FK target.

### 6.15 `file_write_proposals`

Coach's queue for human-approved writes to harness-managed files
(truth/* and the per-project CLAUDE.md). Two scopes share one
table; resolver dispatches on `scope`.

```sql
CREATE TABLE file_write_proposals (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  proposer_id       TEXT NOT NULL,                 -- 'coach' (enforced at tool layer)
  scope             TEXT NOT NULL DEFAULT 'truth', -- 'truth' | 'project_claude_md'
  path              TEXT NOT NULL,                 -- scope-relative (truth: under truth/; pcm: 'CLAUDE.md')
  proposed_content  TEXT NOT NULL,                 -- full new file body
  summary           TEXT NOT NULL,                 -- one-line "why" the user reads
  status            TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'denied', 'cancelled', 'superseded')),
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  resolved_at       TEXT,
  resolved_by       TEXT,                          -- 'human' (only legal value today)
  resolved_note     TEXT
);

CREATE INDEX idx_file_write_proposals_project_status
  ON file_write_proposals(project_id, status);
```

Scopes:

- `truth` — `path` is relative under `/data/projects/<slug>/truth/`.
  Created via `coord_propose_file_write(scope='truth', path,
  content, summary)`. Resolver writes through `server/truth.py:
  resolve_target_path` which anchors and re-validates the path
  under the truth root.
- `project_claude_md` — `path` must be exactly `'CLAUDE.md'`.
  Targets `/data/projects/<slug>/CLAUDE.md`. Resolver re-validates
  the path on approve so a tampered row can't write to a sibling
  file.

The schema's `scope` `CHECK` is intentionally absent — new scopes
can be added without a table rebuild; the propose tool and the
resolver are the validation layers (a row with an unknown scope
raises `FileWriteProposalBadRequest` on resolve, no silent skip).

The auto-supersede invariant filters by `(project_id, scope, path)`
so a hypothetical `truth/CLAUDE.md` proposal and a
`project_claude_md`/`CLAUDE.md` proposal cannot supersede each
other. See §8.3 (truth proposal flow), §8.3a (project CLAUDE.md
proposal lane), §12.7.5 (the `coord_propose_file_write` tool), and
§14.7.5 (HTTP API).

**Migration**: this table is the renamed successor to
`truth_proposals`. Three migration steps in `init_db()` cover the
upgrade path:

1. **Pre-SCHEMA rename** (before `executescript(SCHEMA)`):
   `ALTER TABLE truth_proposals RENAME TO file_write_proposals` if
   the old table exists and the new doesn't. Drops the old index
   so SCHEMA's `CREATE INDEX IF NOT EXISTS` lands cleanly.
2. **`_ensure_columns`** adds the `scope` column with default
   `'truth'` so existing pending rows are queryable as truth scope
   without a manual `UPDATE`.
3. **CHECK-constraint rebuild** (`_rebuild_file_write_proposals_if_check_outdated`):
   the legacy `truth_proposals` shipped with a 4-value status CHECK
   (`pending/approved/denied/cancelled`) and SQLite's
   `ALTER TABLE … RENAME TO` preserves the original CHECK clause
   verbatim. So even after the rename, an `INSERT` with
   `status='superseded'` would fail until the table is rebuilt
   under the new 5-value CHECK. The rebuild detects the gap by
   scanning `sqlite_master` for the literal `'superseded'` token,
   and only fires when missing. Pattern (per SQLite §7 guidance):
   `PRAGMA foreign_keys = OFF`, `BEGIN`, `CREATE TABLE
   file_write_proposals_new` with the right CHECK, copy rows over,
   `DROP` old, `ALTER … RENAME`, `CREATE INDEX`, `PRAGMA
   foreign_key_check` (rolls back on any orphan), `COMMIT`,
   `PRAGMA foreign_keys = ON`. No-op on fresh installs.

All three steps are idempotent on re-run.

---

## 7. Schema Bootstrap

`init_db()` is the only schema setup. It runs `executescript(SCHEMA)`
(idempotent — every statement is `CREATE TABLE IF NOT EXISTS` /
`CREATE INDEX IF NOT EXISTS`), then seeds the `misc` project, the
11 agents, and Coach's misc-project identity row. All inserts use
`INSERT OR IGNORE` so a re-run never overwrites user state — in
particular `active_project_id` is only set when the row is missing.

There are no version-stamped migrations. Schema changes go directly
into `SCHEMA` in `server/db.py`; existing deploys pick up new tables
and indexes via `IF NOT EXISTS`. New columns on existing tables
require an explicit upgrade (an `ALTER TABLE` run against the
deployed DB), since `IF NOT EXISTS` does not reach inside an
already-created table.

---

## 8. Filesystem Layout

Data root:

```text
HARNESS_DATA_ROOT=/data
```

### 8.1 Global Tree

Canonical paths from `server.paths.global_paths()`:

```text
/data/
  CLAUDE.md
  .claude/
    skills/
      llm-wiki/
        SKILL.md
  mcp/
  wiki/
    INDEX.md
    <cross-project-entry>.md
    <project_slug>/
      <entry>.md
  harness.db
  claude/
```

Global scaffold bootstraps:

- `/data/wiki/`
- `/data/wiki/INDEX.md`
- `/data/.claude/skills/llm-wiki/SKILL.md`
- `/data/CLAUDE.md`

The LLM-Wiki skill is copied from `server/templates/llm_wiki_skill.md`.
The global CLAUDE.md is copied from `server/templates/global_claude_md.md`.
Both are first-write-only.

`/data/wiki/INDEX.md` is auto-rebuilt on wiki writes and on boot. Agents should
not edit it directly.

### 8.2 Project Tree

Canonical paths from `server.paths.project_paths(project_id)`:

```text
/data/projects/<slug>/
  CLAUDE.md
  decisions/
  truth/
  working/
    conversations/
    handoffs/
    knowledge/
    memory/
    plans/
    workspace/
  outputs/
  uploads/
  attachments/
  repo/
    .project/
    p1/
    p2/
    ...
```

**`truth/` — user-validated source of truth.** Stores files the user
has signed off on as canonical (specs, brand guidelines, contracts,
hard invariants). Distinct from `decisions/` (immutable agent-written
ADRs) and `knowledge/` (agent-written research that evolves).

**Direct agent writes are blocked.** A `PreToolUse` hook in
`server/agents.py` (`_pretool_file_guard_hook`) hard-denies any
agent `Write` / `Edit` / `MultiEdit` / `NotebookEdit` whose path
resolves under any project's `truth/`, plus any `Bash` command
containing `truth/` as a path component. The same hook also blocks
writes to each project's top-level `CLAUDE.md` at
`/data/projects/<slug>/CLAUDE.md` (a separate protected category —
see §8.3a). There is **no** allow-list or override flag — the deny
is unconditional for every agent (Players AND Coach), every tool,
every project.

**Proposal flow** (the only path through which `truth/` ever
changes; the same flow also covers project CLAUDE.md edits — see
§8.3a). The unified MCP tool is `coord_propose_file_write(scope,
path, content, summary)` with `scope='truth'` selecting this lane:

1. Coach calls `coord_propose_file_write(scope='truth', path,
   content, summary)`. Players cannot — the tool body rejects any
   non-Coach caller. The tool inserts a row in
   `file_write_proposals` (`status='pending'`, `scope='truth'`) with
   the full proposed content and a one-line summary; it does NOT
   touch the file. The `path` argument is a relative path *within
   the currently active project's truth/ folder* — it is NOT a path
   anywhere under `/data/projects/`. The harness rejects paths
   starting with `projects/` or with a known project slug as the
   first segment, with an error message that tells Coach to switch
   active project first. This catches the recurrent mistake of
   encoding a sibling project slug in the path when truth/ is
   per-active-project by design.
2. **Auto-supersede**: before insert, the tool scans for any pending
   row on the same `(project_id, scope, path)` and marks each as
   `status='superseded'`, `resolved_by='system'`,
   `resolved_note='superseded by #<new_id>'`. One
   `file_write_proposal_superseded` event fires per superseded row.
   Invariant: at most one pending proposal per (project, scope,
   path) at any time. The scope filter prevents a hypothetical
   `truth/CLAUDE.md` and a `project_claude_md/CLAUDE.md` proposal
   from supersede-colliding. Coach's tool description explicitly
   tells Coach the new proposal REPLACES the old (full content
   replace, not a merge), so Coach must include any prior pending
   content it still wants. Both updates run in the same DB
   transaction with the new INSERT so a crash mid-flight leaves the
   table coherent.
3. The harness emits a `file_write_proposal_created` event (payload
   carries `scope`); the `EnvFileWriteProposalsSection` of the
   Environment pane shows the pending proposal with a scope badge,
   summary, and a side-by-side diff between the current file
   content and the proposed content (fetched lazily on expand from
   `GET /api/file-write-proposals/{id}/diff`). New files (no
   `before` content) fall back to a plain proposed-content render.
4. The user clicks **approve** or **deny**. Approve calls
   `POST /api/file-write-proposals/{id}/approve` which (a) writes
   the proposed content to `truth/<path>` directly (the truth-scope
   resolver uses its own write — broader extension allowlist +
   200 KB cap — not the Files-pane write_text endpoint), then (b)
   marks the row `approved` with timestamp + `resolved_by =
   "human"` + actor metadata. Deny only marks the row.
5. Approve emits `file_write_proposal_approved`; deny emits
   `file_write_proposal_denied`. Either is visible in the agent
   timeline, so Coach sees the outcome on its next turn.

**Seed file (`truth-index.md`).** Every project's `truth/` is seeded
on scaffold with a `truth-index.md` template (from
`server/templates/truth_index.md`) that explains what the lane is
and how the proposal flow works. **No expected-files manifest is
imposed by default** — the seeded file is explanation only, no
bullets. The user / Coach maintains the file's contents per project
(specs, brand guidelines, contracts — whatever fits *this* project).
This was a deliberate course-correction: an earlier iteration seeded
a `specs.md` bullet and rendered a derived "Expected truth files"
section in EnvPane, but seeing it concrete revealed it was making
the harness pick a project type — graphic-design projects want
`brand-guidelines.md`, contract projects want `vendor-agreements.md`,
research projects want `research-questions.md`. The honest default is
no presupposition.

**Boot-time scaffold rescue.** `lifespan` in `server/main.py` runs
`ensure_project_scaffold(id)` for every non-archived project after
`init_db`, so directories or templates added to `_PROJECT_SUBDIRS` /
`_write_truth_index_stub` after a project's creation (e.g. the truth
lane retro-fitted to existing projects) materialize on next boot.
First-write-only — user edits and Coach proposals own each file
once it exists.

**File creation from the UI** — see §16.5 for the generic "+ new
file" button on the Files pane. Works under any writable root, not
just `truth/`. Replaces the dedicated truth-empty-file endpoint and
EnvPane checklist of an earlier iteration.

**EnvPane sections.** `EnvFileWriteProposalsSection` lists pending
proposals with approve/deny buttons. There is no separate
"Expected truth files" section — the Files pane is the canonical
view of what's actually in `truth/`, and `truth-index.md` (a normal
truth file edited via the proposal flow or the Files-pane editor)
is where the user / Coach records what *should* be there in plain
markdown, no derived UI needed.

Players whose work needs a truth update message Coach (via
`coord_send_message`); Coach decides whether to relay as a proposal.
This keeps the human surface area small — only Coach hits the
approval queue.

The resolver logic lives in `server/truth.py` (FastAPI-free) so the
HTTP wrappers in `server/main.py` can be thin and the test suite
doesn't need to import FastAPI to exercise approve/deny. Resolver
exceptions (`FileWriteProposalNotFound` / `FileWriteProposalConflict` /
`FileWriteProposalBadRequest`) translate 1:1 to 404 / 409 / 400.

The folder is mirrored to kDrive by the regular project sync loop
(sibling of `decisions/`), so spec PDFs dropped into the cloud drive
surface in the Files pane on the next pull. There is no git tracking
on `truth/` — kDrive's own file versioning + the `file_write_proposals`
table (every approve/deny is a permanent row with timestamps,
proposer, resolver, optional note) are the audit trail.

Actual current caveats:

- Knowledge writes use `working/knowledge/`.
- Memory DB mirror path in `coord_update_memory` currently writes to
  `projects/<slug>/memory/<topic>.md` on WebDAV, while canonical v2 local
  memory path is `working/memory/`. That WebDAV path should be reconciled.
- Outputs module still defaults to global `/data/outputs`, not
  `/data/projects/<slug>/outputs`.
- Workspaces still use `/workspaces`, not the per-project `repo/` tree.

### 8.3 Project CLAUDE.md Stub

Created on project creation if missing — first-write-only, so existing
projects' CLAUDE.md files are not overwritten when this template
changes (see also "Migration: existing projects" further down):

```markdown
# Project: <name>

## Project objectives
<pointer paragraph: the project's goals / scope live in the separate
file `/data/projects/<slug>/project-objectives.md`, kDrive-mirrored,
edited via the EnvPane Objectives section or Coach's Write tool;
the harness injects that file into Coach's system prompt every turn>

## Repo
<repo_url or placeholder>

## Stakeholders
<filled in by Coach>

## Team
<filled in by Coach>

## Glossary
<filled in by Coach>

## Conventions
<project-specific rules>

## truth/
<reminder of the truth/ lane: read-only for agents, proposal flow via
`coord_propose_file_write(scope='truth', ...)`, seeded
`truth-index.md` (freeform, no enforced manifest structure), slug
interpolated into the absolute path>

## Updating this CLAUDE.md
<reminder that this file is also read-only for agents, and Coach
proposes changes via
`coord_propose_file_write(scope='project_claude_md', path='CLAUDE.md',
content, summary)`; the harness-wide /data/CLAUDE.md is not
proposeable>
```

The trailing `## truth/` and `## Updating this CLAUDE.md` sections are
fixed paragraphs (template literal in `_PROJECT_CLAUDE_MD_STUB`) that
interpolate the project's slug and explain both proposal scopes.
Coach in fresh projects reads this on every turn via
`build_system_prompt_suffix`.

**No `## Goal` section.** Earlier revisions of this template included
a `## Goal\n<description>` section pre-filled from the creation-modal
description. That was dropped (2026-05-02) because the same goal text
was already injected into Coach's system prompt as the
`## Project objectives` section read from
`/data/projects/<slug>/project-objectives.md` (per
[recurrence-specs.md](recurrence-specs.md) §3.3 and §6.1) — and the
coordination block also rendered a `Goal:` line from
`projects.description`. Three stale-prone copies of the same content
drifted apart whenever the operator updated the objectives file but
not the modal description (or vice-versa). The template now carries a
**pointer paragraph** to `project-objectives.md` so Coach knows where
to read / update goals; the file itself is the single canonical
surface for goal content. `projects.description` (the modal one-liner)
remains in the DB as a UI-only field for the project pane title and
project list tagline.

**Migration: existing projects.** Because the stub is first-write-only,
projects created before this template change still have CLAUDE.md
files without the new sections. To add them, Coach calls
`coord_propose_file_write(scope='project_claude_md', path='CLAUDE.md',
content=<full updated body>, summary=<one-line why>)`; the user
reviews the diff and approves in the EnvPane "File-write proposals"
section. The harness then writes the file. (Earlier iterations of
this spec assumed Coach could `Write` to the project CLAUDE.md
directly, which was never actually true: Coach has no Write tool, and
since the file-guard hook now also covers `<slug>/CLAUDE.md`, the
proposal flow is the only path in.)

### 8.3a Project CLAUDE.md Proposal Lane

The same proposal flow that protects `truth/` (see §8.3 above) also
covers the per-project instruction file at
`/data/projects/<slug>/CLAUDE.md`. The unified MCP tool
`coord_propose_file_write(scope, path, content, summary)` selects
the lane via `scope`; for project CLAUDE.md edits Coach passes
`scope='project_claude_md'` and `path='CLAUDE.md'` (the only legal
path for this scope — the resolver re-validates and refuses to
write anywhere else if a row is tampered with).

The `_pretool_file_guard_hook` in `server/agents.py` denies any
direct `Write` / `Edit` / `MultiEdit` / `NotebookEdit` whose path
resolves to `<projects-root>/<slug>/CLAUDE.md` (matching exactly two
parts under projects/ — so a Player's worktree-internal repo
CLAUDE.md at `<slug>/repo/<slot>/CLAUDE.md` is **not** caught and
remains writable). It also denies any `Bash` command containing
`projects/<slug>/CLAUDE.md` as a substring. The deny reason names
the right tool call so Coach learns the proposal flow on first
attempt.

The diff endpoint (`GET /api/file-write-proposals/{id}/diff`) reads
the current CLAUDE.md fresh from disk on every request, so a manual
edit (Files pane, kDrive sync) between propose and approve is
visible to the human reviewer rather than baked into a stale
`before` snapshot.

The harness-wide `/data/CLAUDE.md` is **not** a valid scope. Only
the user edits that file (via the Files pane); there is no agent
path to it.

### 8.4 File Browser Roots

The UI exposes two roots:

| Root id | Path | Scope | Writable |
| --- | --- | --- | --- |
| `global` | `/data` | global | yes, text only |
| `project` | `/data/projects/<active>` | active project | yes, text only |

`/api/files/roots` returns `id`, `key`, `label`, `path`, `scope`,
`project_id`, `writable`, and `exists`.

Read rules:

- Only whitelisted roots.
- Path traversal rejected with `Path.resolve()` and `relative_to()`.
- Symlinks skipped in tree walking.
- Inline reads capped at 256 KB.
- UTF-8 decode with replacement.

Write rules:

- Editable extensions defined in `server.files.EDITABLE_EXTS` —
  text + common code/config formats (mirrors the FilesPane's
  `FILES_TEXT_EXTENSIONS`). Plus an extensionless basename allowlist
  for `Dockerfile`, `Makefile`, `README`, etc.
- Max body 100,000 chars.
- Plain disk write. Empty body is accepted (used by the Files-pane
  "+ new file" button to create a stub).
- WebDAV mirroring happens later through project/global sync loops.
- `file_written` event emitted by API.
- Wiki writes trigger `update_wiki_index()` unless writing `INDEX.md` itself.
  Triggers fire on three paths: (1) the HTTP file-write endpoint above
  (UI Files-pane writes), (2) project creation in `projects_api.py`,
  and (3) a `PostToolUse` SDK hook in `server/agents.py` matching
  `Write|Edit|MultiEdit|NotebookEdit` whose tool_input path resolves
  under `global_paths().wiki` — this last one is what catches agent
  Write tool calls (which go through the SDK directly to disk and
  bypass the HTTP write endpoint). `POST /api/wiki/reindex` is the
  manual catch-all for external writers (cloud sync from another
  machine, snapshot restore, manual `cp` into the tree).

The `global` tree hides noisy/sensitive top-level entries:

- `projects`
- `claude`
- `attachments`
- `harness.db`
- SQLite sidecars

---

## 9. WebDAV Mirror and Sync

WebDAV config:

```text
HARNESS_WEBDAV_URL
HARNESS_WEBDAV_USER
HARNESS_WEBDAV_PASSWORD
```

All three must be set or WebDAV is disabled. The URL should point directly at
the folder the harness owns, for example a `TOT` folder. Files are written
relative to that URL. No extra root prefix setting exists.

The WebDAV client:

- Normalizes the base URL with a trailing slash.
- Supports text and bytes upload/download.
- Creates parent directories recursively.
- Supports atomic byte writes via temp file plus MOVE, with fallback PUT.
- Returns false/none on failures instead of throwing into tool calls.
- Provides `probe()` for `/api/health`.

### 9.1 DB Snapshots

`server/sync.py` still owns database snapshots:

- Interval: `HARNESS_WEBDAV_SNAPSHOT_INTERVAL`, default 300 seconds.
- Retention: `HARNESS_WEBDAV_SNAPSHOT_RETENTION`, default 144.
- Uses SQLite `VACUUM INTO` into bytes, then writes to WebDAV.
- Snapshot path: `snapshots/<timestamp>.db`.

### 9.2 Active Project Sync

`server/project_sync.py` active-project loop:

- Interval: `HARNESS_PROJECT_SYNC_INTERVAL`, default 300 seconds.
- Resolves current active project each cycle.
- Pushes `/data/projects/<slug>/` excluding top-level `repo/` and
  `attachments/`.
- Pushes `/data/wiki/<slug>/`.
- Tracks mtime, size, sha256 in `sync_state`.
- Detects local deletions and deletes remote files.
- Retries per file with exponential backoff.
- Emits `kdrive_sync_failed` on retry exhaustion.

Remote mapping:

```text
project tree -> projects/<slug>/<relative>
wiki tree    -> wiki/<slug>/<relative>
```

### 9.3 Global Sync

Global loop:

- Interval: `HARNESS_GLOBAL_SYNC_INTERVAL`, default 1800 seconds.
- Starts after a 60 second stagger.
- Pushes:
  - `/data/CLAUDE.md` as `CLAUDE.md`
  - `/data/.claude/skills/**` as `skills/**`
  - `/data/mcp/**` as `mcp/**`
  - `/data/wiki/INDEX.md` as `wiki/INDEX.md`
  - root-level `/data/wiki/*.md` as `wiki/*.md`
- Does not push per-project wiki subfolders; those are owned by active-project
  sync.

### 9.4 Pull on Open

`pull_project_tree(project_id)` is used during project activation:

- Pulls `projects/<slug>/` and `wiki/<slug>/`.
- Skips `repo/` and `attachments/`.
- Writes local files atomically.
- Updates `sync_state`.

### 9.5 Push on Close

`force_push_project(project_id)`:

- Tags recent files under `working/conversations/` with `live: true` frontmatter
  if modified within `HARNESS_LIVE_CONVERSATION_S`, default 30 seconds.
- Runs active project push under `HARNESS_KDRIVE_CLOSE_TIMEOUT_S`, default 60s.
- On timeout emits `kdrive_sync_failed` and returns a timed-out result.

---

## 10. Claude Context and Prompt Assembly

Prompt layers (order matches `agents.py:run_agent`):

1. Per-agent identity block from `agent_project_roles`.
2. Coach-only coordination block from current project/team/tasks/inbox/wiki.
3. Baseline Coach or Player role prompt.
4. Global rules from `/data/CLAUDE.md`.
5. Active project rules from `/data/projects/<slug>/CLAUDE.md`.
6. Per-agent `brief` from `agent_project_roles`.
7. **Coach-only**: `## Project objectives` (verbatim from
   `/data/projects/<slug>/project-objectives.md`) followed by
   `## Open coach todos` (verbatim from `coach-todos.md`). Both are
   re-read every turn; either section is omitted entirely when its
   file is missing or empty. Defined by
   [recurrence-specs.md](recurrence-specs.md) §6. This is the
   **single canonical surface for goal content** — neither the
   coordination block (#2) nor the per-project CLAUDE.md (#5)
   carries a `Goal:` line or `## Goal` section. See
   recurrence-specs §6.1 for the rationale and §8.3 above for the
   stub template that points to this file.
8. Continuity handoff after `/compact`, when present.

`server/context.py` re-reads the global and project `CLAUDE.md` files every turn.
Each file is truncated at 200,000 chars to prevent runaway prompt bloat.

### 10.1 Identity

Agents are told:

- Their slot id.
- Their project-specific name/role if set.
- Their workspace path.
- Active project paths.
- Governance notes for Coach/Player role.

Players are auto-named from a lacrosse surname pool on first spawn if they have
no `agent_project_roles.name` for the active project. The auto assignment emits
`player_assigned` with `auto: true`.

### 10.2 Coach Coordination Block

Built in `agents.py` for Coach turns. It includes:

- Active project name and a one-line pointer to the per-project
  CLAUDE.md and `project-objectives.md` (the canonical surface for
  goals / scope — see [recurrence-specs.md](recurrence-specs.md) §6
  and §6.1). The block does NOT render `projects.description` as a
  `Goal:` line; goal content has a single canonical surface
  (§8.3 + recurrence-specs §6.1).
- Team roster and locked players.
- Open/current tasks.
- Coach inbox summary.
- Recent decisions.
- Wiki paths.
- Reminder to assign roles and coordinate.

### 10.3 Compact and Continuity

Manual compact:

- UI slash command `/compact`.
- API `POST /api/agents/{id}/compact`.
- Runs the agent with `COMPACT_PROMPT`.
- Captures the summary as `agent_sessions.continuity_note`.
- Writes full handoff file under active project's `working/handoffs/`.
- Clears session id so the next turn starts fresh.
- Emits `session_compacted`.

Auto-compact:

- Controlled by `HARNESS_AUTO_COMPACT_THRESHOLD`, default 0.7.
- Estimates session context from Claude CLI JSONL files under
  `CLAUDE_CONFIG_DIR/projects/`.
- If over threshold, runs a compact turn first.
- If auto-compact produces no summary, it force-clears the session to escape a
  threshold loop.

Session transfer (compact + runtime flip):

A runtime change normally loses conversation history because `session_id`
(Claude) and `codex_thread_id` (Codex) are runtime-specific and cannot
cross over. The session-transfer flow runs the compact summary on the
**source** runtime first, persists it to `continuity_note`, then flips
`agents.runtime_override` so the next turn on the **target** runtime
reads the handoff in its system prompt — same delivery vehicle as a
plain `/compact`.

- UI: pane gear popover's runtime selector. Picking `claude` / `codex`
  routes through the transfer endpoint; picking `default` (empty) keeps
  the legacy blunt-clear `PUT /api/agents/{id}/runtime` (no compact).
- API: `POST /api/agents/{id}/transfer-runtime {runtime}`.
- MCP: `coord_set_player_runtime(player_id, runtime)` (Coach-only).
  An empty `runtime=''` argument retains the legacy blunt-clear semantic.

Dispatch matrix at the entry point:

| Source runtime | Target runtime  | Prior session? | Action                                                                                |
|----------------|-----------------|----------------|---------------------------------------------------------------------------------------|
| X              | X (same)        | —              | 200 noop                                                                              |
| X              | Y               | NO             | flip immediately, emit `runtime_updated` + `session_transferred(note=no_prior_session)` |
| X              | Y               | YES            | queue `run_agent(COMPACT_PROMPT, compact_mode=True, transfer_to_runtime=Y)`             |

Mid-turn flips (`agents.status='working'`) are 409'd at the entry point —
the in-flight turn would be on the old runtime while subsequent turns
use the new one.

Compact-handler branch:
`transfer_to_runtime` rides through `run_agent` → `TurnContext` →
`turn_ctx`. Each runtime's compact handler reads it after the
post-compact bookkeeping (`continuity_note` written, source session id
cleared) and calls `_perform_runtime_transfer_flip(slot, target)` —
flips `runtime_override`, nulls **both** runtime session columns
(defensive against orphaned thread ids from a prior life on the
target), evicts any cached Codex client, emits `runtime_updated` with
`source=session_transfer`. Then the handler emits
`session_transferred(from_runtime, to_runtime, chars, handoff_file)`
in place of `session_compacted`.

Failure modes:

- Compact yields no summary on Claude → `session_transfer_failed`
  emits and the runtime stays put. The intent of transfer is "carry
  forward via summary"; a flip with empty context is a destructive
  blind switch.
- Compact yields no summary on Codex → flip still proceeds because
  `client.compact_thread()` already cleared the thread; not flipping
  would leave the agent on Codex with no thread to resume, strictly
  worse than flipping with thin context. Asymmetry intentional.
- Helper failure on `_clear_codex_thread_id` is logged but doesn't
  abort; `runtime_updated` still emits so the UI doesn't silently
  miss the change.

Why not just `/compact` followed by a blunt PUT: atomicity. The flip
only happens iff the compact succeeded with a non-empty summary
(Claude side) or the native `compact_thread` call succeeded (Codex
side). A user who runs the two operations separately gets the flip
even when the compact failed, leaving the agent on the new runtime
with no handoff. The transfer flow also emits the right event
vocabulary so timelines read as a single transfer boundary, not as a
compact plus an unrelated runtime change.

See `Docs/CODEX_RUNTIME_SPEC.md` §E.8 for the full design.

Compact prompt structure:

`COMPACT_PROMPT` in `server/agents.py` instructs the agent to produce a
1500–3000 word handoff document with these markdown sections, in order:

1. **Primary request and intent** — original ask + scope additions, verbatim.
2. **Key technical concepts** — glossary of terms used in the session.
3. **All operator messages (verbatim, in order)** — every human message,
   numbered, including one-word replies. Preserves voice that paraphrase
   loses.
4. **How we got here** — narrative arc, dead ends, recurring workflow pattern.
5. **Files touched** — per-file inventory tagged **touched** vs **read-only**,
   with diffs / snippets inline for recent or relevant files.
6. **Errors & fixes** — one entry per failure: symptom, root cause, fix,
   regression test.
7. **Key findings & decisions** — what / why / who agreed.
8. **Open questions** — unresolved items, quoted verbatim.
9. **References** — URLs, commit hashes, external links not covered above.
10. **People & roles** — who participated, responsibilities, preferences.
11. **Context quirks & gotchas** — environment / tool peculiarities.
12. **In-flight state at compact** — last assistant message verbatim, last
    tool call, exact next action.
13. **Pending — concrete checklist** — `[ ] Action — owner — blocking?` items.

The prompt explicitly tells the agent NOT to append a footer pointing at the
JSONL or handoff file. The harness appends that itself via
`_build_compact_footer()`, naming `$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/
<session-id>.jsonl` so fresh-you can read the full transcript on demand.

Recent exchange preservation:

- `last_exchange_json` stores a bounded rolling log.
- Budget: `HARNESS_HANDOFF_TOKEN_BUDGET`, default 20,000 tokens.
- Full session transcript remains in Claude CLI JSONL until session retention
  trims it (`HARNESS_SESSION_RETENTION_DAYS`, default 30).

### 10.4 Context Usage UI

`GET /api/agents/{id}/context` returns:

- `session_id` (Claude SDK)
- `codex_thread_id` (Codex)
- estimated used tokens for the current resumed prompt/session footprint
- context window
- model (resolved — falls back to the latest turn's model when the UI
  doesn't pass an override)
- ratio

Estimation semantics — Claude path (when `session_id` is set):

- Read the Claude Code session JSONL under `CLAUDE_CONFIG_DIR/projects/`.
- Use the latest assistant usage row as the prompt-size source of truth.
- Count `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`.
- Add the latest assistant output tokens because they become part of the next
  resumed prompt.
- Do not sum `ResultMessage.usage` across tool rounds; that overstates context
  pressure when prompt caching is active.

Codex path: the Claude path returns 0 for codex sessions; the server
reconstructs prompt size from the latest `turns` row matching the
codex thread id (`runtime = 'codex'`). The CodexRuntime is responsible
for populating that row from its own usage source. Parser shape and
known limitations: see `Docs/CODEX_RUNTIME_SPEC.md` §E.5.

Window resolution: `_context_window_for(model)` returns the per-model
max. When the UI doesn't pass `?model=`, the server reads the model
recorded on the latest turn for the active session.

The pane renders this as a compact `ctx` bar: current footprint as a fraction
of the model's max window.

---

## 11. Agent Runtime

`run_agent(agent_id, prompt, model=None, plan_mode=None, effort=None, ...)`
is the central execution path. `model` / `plan_mode` / `effort` all
default to None so a missing per-pane value falls through to the
Coach-set override on `agent_project_roles` (then to the role / SDK
default). An explicit per-pane `False` for `plan_mode` is preserved
as "off" — it does NOT trigger the override lookup.

Pre-spawn checks:

- Global pause flag.
- Existing running task for same agent.
- Cost caps.
- Auto-name Player if needed.
- Load prior session id for active project.
- Load model defaults and pane overrides.
- Load external MCP servers.
- Build tool allowlist.
- Build system prompt.

During run:

- Emits `agent_started`.
- Streams SDK messages into events.
- Persists status and heartbeat.
- Inserts turn rows on result messages.
- Persists/clears session ids.
- Handles stale-session retry.
- Handles tool-use permission callbacks for plan/question flows.

After run:

- Emits `agent_stopped`, `agent_cancelled`, `error`, or retry-related events.
- Updates agent status to idle/error.
- May schedule post-error retry.

### 11.1 Pause and Cancel

Pause:

- `GET /api/pause`
- `POST /api/pause {paused: bool}`
- In-memory only.
- Blocks new starts and Coach loops.
- Does not cancel in-flight turns.
- Emits `pause_toggled`.

Cancel:

- `POST /api/agents/{id}/cancel`
- `POST /api/agents/cancel-all`
- Cancels running asyncio tasks.
- Emits `agent_cancelled`.

### 11.2 Cost Caps

Environment:

```text
HARNESS_AGENT_DAILY_CAP=5.0
HARNESS_TEAM_DAILY_CAP=20.0
```

Rules:

- `0` disables each cap.
- Checked before `agent_started`.
- Based on UTC day spend from `turns.cost_usd`.
- Blocked spawns emit `cost_capped`.

### 11.3 Coach Recurrences (formerly Coach Loops)

Replaced the legacy in-memory loops with a unified, project-scoped,
persisted recurrence model — see `Docs/recurrence-specs.md` for the
full design. Three flavors share one `coach_recurrence` table and one
scheduler (`recurrence_scheduler_loop`):

- **tick** — singleton per project, harness-composed prompt via
  `compose_tick_prompt()`. Spec §4 priority: inbox → todos →
  objectives → end quietly when all empty.
- **repeat** — many per project, fixed-minute cadence + caller prompt.
- **cron** — many per project, friendly DSL (`daily 09:00`,
  `weekdays 18:00`, `mon,thu 14:00`, `monthly 1 09:00`,
  `2026-05-01 10:00`) + TZ + caller prompt.

Runtime API:

- `GET /api/recurrences` — list all rows for the active project.
- `POST /api/recurrences {kind, cadence, prompt, tz?}` — create
  repeat or cron.
- `PATCH /api/recurrences/{id} {cadence?, prompt?, tz?, enabled?}`.
- `DELETE /api/recurrences/{id}`.
- `PUT /api/coach/tick {minutes?, enabled?}` — set or disable the
  recurring tick.
- `POST /api/coach/tick` — fire one tick now (kept; uses the smart
  composer). Rejects with 409 if Coach is working.

UI slash commands (Coach pane only):

- `/tick` — fire one tick now.
- `/tick N` — set recurring tick every N minutes; auto-enables.
- `/tick off` — disable recurring tick.
- `/repeat` — list active repeats; `/repeat N <prompt>` adds; `/repeat
  rm <id>` deletes.
- `/cron` — list active crons; `/cron <when> <prompt>` adds (DSL); 
  `/cron rm <id>` deletes.
- `/loop` — typing it surfaces the rename message (legacy command
  removed in phase 8).

UI surface:

- **Recurrence pane** (rail icon: circular arrows) — opens alongside
  EnvPane, shows three sections (tick / repeats / crons) with editable
  cards, status dots, next/last fire stamps.
- **EnvPane sections** — `Project objectives` (multiline editor) +
  `Coach todos` (checkbox list, click-to-expand, archive toggle).

Skips when paused, Coach is already working, or daily cost cap hit
(emits `recurrence_skipped` with `reason="coach_busy"` /
`reason="cost_capped"`).

Events emitted: `recurrence_added`, `recurrence_changed`,
`recurrence_deleted`, `recurrence_fired`, `recurrence_skipped`,
`recurrence_disabled`. Plus `coach_todo_added`, `coach_todo_completed`,
`coach_todo_updated`, `objectives_updated`.

Migration: `HARNESS_COACH_TICK_INTERVAL` is honored only on first
migration via `db._seed_recurrence_from_env`; the `recurrence_v1_seeded`
flag in `team_config` makes the seed idempotent. Subsequent boots
ignore the env var. Documented as deprecated.

### 11.4 Auto-Wake

`maybe_wake_agent(slot, reason, bypass_debounce=False)` wakes an idle agent when:

- Harness is not paused.
- Target agent is not already running.
- Debounce passes unless bypassed.

Triggers:

- Coach `coord_assign_task`: wakes assignee, bypasses debounce.
- Agent `coord_send_message` to direct recipient: wakes recipient, debounce
  applies.
- Human `POST /api/messages` to direct recipient: wakes recipient, bypasses
  debounce.
- Telegram inbound to Coach: wakes Coach, bypasses debounce.

Broadcasts do not wake the team.

Debounce:

```text
HARNESS_AUTOWAKE_DEBOUNCE=10
```

### 11.5 Error Retry

On turn error:

- Error event is emitted.
- Agent status becomes error.
- A post-error retry can be scheduled after
  `HARNESS_ERROR_RETRY_DELAY`, default 45 seconds.
- Consecutive retry limit:
  `HARNESS_ERROR_RETRY_MAX_CONSECUTIVE`, default 3.
- Coach DM debounce for Player errors:
  `HARNESS_ERROR_DM_DEBOUNCE`, default 300 seconds.

### 11.6 Stale Task Watchdog

Environment:

```text
HARNESS_STALE_TASK_MINUTES=15
HARNESS_STALE_TASK_NOTIFY_INTERVAL_MINUTES=30
HARNESS_STALE_TASK_CHECK_INTERVAL_SECONDS=60
```

If enabled, the loop detects active-project tasks stuck in `in_progress`
without recent owner activity and notifies Coach by system message and events.

### 11.7 Crash Recovery

`crash_recover()` runs on startup:

- `agents.status in ('working', 'waiting')` -> `idle`.
- `tasks.status = 'in_progress'` -> `claimed`, owner preserved.

This is global across projects for tasks, but harmless because all stale
in-progress work should be reclaimed after an unclean shutdown.

---

## 12. Coordination Tools

All coordination tools are registered as an in-process MCP server named
`coord` for each SDK query. The caller id is captured when the server is built,
so permissions do not depend on the model truthfully passing its identity.

### 12.1 Task Tools

`coord_list_tasks(status?, owner?)`

- Lists up to 100 tasks in active project.
- Optional `status`.
- Optional `owner`, with `null`/`none`/`unassigned` matching `owner IS NULL`.

`coord_create_task(title, description?, parent_id?, priority?)`

- Coach can create top-level tasks.
- Players can only create subtasks under tasks they own.
- If a Player omits `parent_id`, their `current_task_id` is used.
- Priority: `low`, `normal`, `high`, `urgent`.
- Emits `task_created`.

`coord_claim_task(task_id)`

- Players only.
- Task must be `open`.
- Player must not already have `current_task_id`.
- Atomic update guarded by `status='open'`.
- Sets `owner`, `status='claimed'`, `claimed_at`, and agent
  `current_task_id`.
- Emits `task_claimed`.

`coord_update_task(task_id, status, note?)`

- Status must be valid state-machine transition.
- Owner can update.
- Coach can cancel any task.
- Completing/cancelling clears current owner pointer.
- Emits `task_updated`.
- If a non-human creator needs to know about done/blocked/cancelled, sends a
  system message back to creator.

`coord_assign_task(task_id, to)`

- Coach only.
- Target must be `p1` to `p10`.
- Target must not be locked.
- Target must not already own a task.
- Task must be `open`.
- Atomic update to `claimed` and owner.
- Sets target `current_task_id`.
- Emits `task_assigned`.
- Auto-wakes assignee.

### 12.2 Messaging

`coord_send_message(to, body, subject?, priority?)`

- Any agent can message any other agent or `broadcast`.
- Body max 5000 chars.
- Subject max 200 chars.
- Priority: `normal` or `interrupt`.
- Cannot send to self.
- Coach cannot direct-message locked Players.
- Emits `message_sent`.
- Direct recipients auto-wake; broadcasts do not.

`coord_read_inbox()`

- Reads unread direct and broadcast messages for caller in active project.
- Marks each read in `message_reads` for that caller only.
- Locked Players filter out Coach-sourced messages while locked.

### 12.3 Memory

`coord_list_memory()`

- Lists active project's topics with version, timestamp, author, size.

`coord_read_memory(topic)`

- Validates topic regex.
- Reads current content.

`coord_update_memory(topic, content)`

- Validates topic.
- Rejects empty content.
- Max 20,000 chars.
- Upserts and increments version.
- Emits `memory_updated`.
- Fire-and-forget mirrors a markdown file to WebDAV when enabled.

### 12.4 Knowledge

`coord_write_knowledge(path, body)`

- Any agent can write.
- Project-scoped to active project.
- Local path: `/data/projects/<active>/working/knowledge/<path>`.
- WebDAV path currently: `projects/<active>/knowledge/<path>`.
- Body required, max 100,000 chars.
- Path max four segments.
- File suffix `.md` or `.txt`.
- Emits `knowledge_written`.

`coord_read_knowledge(path)`

- Reads local cache first, WebDAV fallback.
- Validates same path rules.

`coord_list_knowledge()`

- Lists active project's known `.md`/`.txt` knowledge paths.

### 12.5 Outputs

`coord_save_output(path, content_base64)` is implemented as a function and
listed in `ALLOWED_COORD_TOOLS`, backed by `server/outputs.py`.

Intended behavior:

- Save binary deliverables.
- Path max four segments.
- Max decoded size 20 MB.
- Allowed suffixes:
  - `.docx`, `.xlsx`, `.pptx`
  - `.pdf`
  - `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`
  - `.zip`, `.tar`, `.gz`
  - `.csv`, `.tsv`
  - `.md`, `.txt`, `.html`, `.json`
- Emit `output_saved`.
- Mirror to WebDAV.

Current implementation gap:

- `save_output` is not included in the `tools=[...]` list passed to
  `create_sdk_mcp_server()` in `server/tools.py`.
- Therefore agents may not actually see/call it despite the function and
  allowlist existing.
- `server/outputs.py` still writes to global `/data/outputs`, not
  project-scoped `project_paths(active).outputs`.

### 12.6 Git

`coord_commit_push(message, push?)`

- Players only.
- Requires project repo/worktree configured.
- Fails if current workspace has no `.git`.
- Runs:
  - `git add -A`
  - `git status --porcelain`
  - `git commit -m <message>`
  - optionally `git push origin HEAD`
- `push` defaults true; false values: `false`, `0`, `no`, `off`.
- Clean tree returns soft OK.
- Emits `commit_pushed`.

### 12.7 Decisions

`coord_write_decision(title, body)`

- Coach only.
- Body max 40,000 chars.
- Slugifies title.
- Filename:
  - `YYYY-MM-DD-<slug>.md`
  - local collision gets `-2`, `-3`, etc.
- Frontmatter includes title, date, timestamp, author.
- Writes to WebDAV `projects/<active>/decisions/<filename>` when enabled;
  otherwise local fallback under `/data/projects/<active>/decisions/`.
- Emits `decision_written`.

### 12.7.5 File-write Proposals

`coord_propose_file_write(scope, path, content, summary)`

- **Coach only.** Players get an explicit error directing them to
  message Coach for relay. Listed in `ALLOWED_COORD_TOOLS`; the body
  rejects non-Coach callers.
- **Two scopes today**:
  - `scope='truth'` — `path` is relative to the *currently active*
    project's `truth/` folder. Existing path validation rules apply:
    a leading `truth/` is stripped defensively; no leading slash, no
    `..` segments; first segment must not be `projects/` or a known
    sibling project slug (catches the recurrent Coach mistake of
    encoding cross-project paths when truth/ is per-active-project
    by design).
  - `scope='project_claude_md'` — `path` must be exactly `'CLAUDE.md'`.
    Targets `/data/projects/<active>/CLAUDE.md`. Any other path is
    rejected at propose time AND re-validated at approve time (the
    resolver refuses to write if the row's path was tampered with).
  - The harness-wide `/data/CLAUDE.md` is NOT a valid scope; only
    the user edits that file.
- `content` is a full file body (max 200,000 chars). This is a full
  REPLACE — Coach must include the parts being kept verbatim. The
  user reviews a side-by-side diff against the current file content
  in the UI before approving.
- `summary` is a single-line "why" (max 200 chars) shown next to the
  approve/deny buttons.
- **Auto-supersede invariant**: at most one `pending` proposal per
  `(project_id, scope, path)`. The tool's body runs
  SELECT-INSERT-UPDATE in one transaction:
  1. SELECT pending IDs for the same `(project, scope, path)`.
  2. INSERT the new row as `pending`.
  3. For each prior pending ID: UPDATE to `status='superseded'`,
     `resolved_by='system'`, `resolved_note='superseded by #<new_id>'`.
     The UPDATE has `WHERE status='pending'` so already-resolved rows
     can never be flipped to superseded.
  The scope filter prevents a hypothetical `truth/CLAUDE.md` and a
  `project_claude_md`/`CLAUDE.md` proposal from supersede-colliding.
- Emits one `file_write_proposal_superseded` event per superseded
  row, then one `file_write_proposal_created` event for the new
  pending row (with the `superseded` ID list and `scope` in its
  payload).
- Reorganization pattern (truth scope) documented in the tool
  description: split a growing file by sending three proposals in
  sequence — (1) new dependency files with content, (2) original
  file with content removed, (3) `truth-index.md` updated to list
  the new files. Each proposal supersedes any prior pending one for
  that (scope, path).

The MCP tool is the ONLY agent-accessible mechanism for changing
`truth/` or per-project `CLAUDE.md`. The PreToolUse
`_pretool_file_guard_hook` in `server/agents.py` denies any agent
`Write` / `Edit` / `MultiEdit` / `NotebookEdit` whose path resolves
to either protected target (any project's `truth/*` or its
`<slug>/CLAUDE.md`), plus any `Bash` command containing those path
components. The deny is unconditional: every agent (including
Coach), every tool, every project. A Player's worktree-internal
`<slug>/repo/<slot>/CLAUDE.md` is NOT caught (different position in
the path) and remains writable for the Player whose repo it is.

The resolver in `server/truth.py` handles approve/deny:
- Approve: validates the path lands under `project_paths(slug).truth`
  with a `relative_to` check (rejects traversal as
  `FileWriteProposalBadRequest`), `mkdir(parents=True, exist_ok=True)` on
  the parent, then `write_text(content, encoding="utf-8")`. Caps at
  200 KB. Bypasses the Files-pane endpoint's `.md`/`.txt` restriction
  on purpose — text-of-any-extension is the right policy for truth
  (specs, brand YAML, JSON contracts) since the user is the gate at
  approval time.
- Deny: marks the row, no file write.
- Idempotency: re-resolving a non-pending row raises
  `FileWriteProposalConflict("approved")` / `("denied")` /
  `("cancelled")` / `("superseded")` → 409.

### 12.7.6 Project File Reads

`coord_read_file(path)`

- **Available to all agents** (Coach AND Players). Reads are
  inherently safe; the read-handle invariant only constrains writes.
- `path` is relative to the active project's root
  (`/data/projects/<active>/`). Examples: `'CLAUDE.md'`,
  `'truth/specs.md'`, `'decisions/0001-foo.md'`,
  `'working/knowledge/notes.md'`, `'outputs/report.md'`.
- Rejects leading `/` and any `..` segment; resolves the target
  with `Path.resolve()` and re-anchors under the project root so a
  symlink / weird-casing trick can't escape the lane.
- 200 KB size cap (matches the propose tool's write cap). Files
  that aren't valid UTF-8 are rejected with a clear error rather
  than returning garbage; binary deliverables under `outputs/`
  cannot be read through this tool.
- Exists in addition to Claude's native `Read` because the Codex
  runtime's restrictive sandbox blocks alternative read paths;
  `coord_read_file` bypasses that constraint via the MCP proxy. See
  `Docs/CODEX_RUNTIME_SPEC.md` for sandbox details. On Claude this
  overlaps with `Read` — agents can use either.
- Project CLAUDE.md note: it's also auto-injected into every
  agent's system prompt via `server/context.py`, so calling
  `coord_read_file('CLAUDE.md')` is redundant for read-only
  inspection at turn start. Use it when you want the *current*
  body during a long turn (the system-prompt copy is frozen at
  turn start; a manual file edit since the turn began is invisible
  until the next turn unless you re-read).

### 12.8 Team Identity

`coord_list_team()`

- Joins global `agents` with active project's `agent_project_roles`.
- Shows slot, name, role, brief preview, status, current task, lock marker.

`coord_set_player_role(player_id, name, role)`

- Coach only.
- `player_id`: `p1` to `p10`.
- Name max 80 chars.
- Role max 300 chars.
- Upserts active project's `agent_project_roles`.
- Emits `player_assigned`.

`coord_set_player_runtime(player_id, runtime)`

- Coach only.
- `player_id`: `p1` to `p10` (cannot flip Coach's own runtime via MCP
  — that path is HTTP-only via `PUT /api/agents/coach/runtime` or
  `POST /api/agents/coach/transfer-runtime`).
- `runtime`:
  - `'claude'` / `'codex'` — concrete target. Routes through the
    **session-transfer flow** by default (§10.3): if the Player has a
    prior session on the source runtime, a transfer-mode compact is
    queued via `asyncio.create_task` and the tool returns immediately
    with `queued=True`; on success the runtime flips and
    `session_transferred` fires. If there's no prior session, the
    runtime flips immediately + emits `runtime_updated` +
    `session_transferred(note=no_prior_session)`. Same-runtime target
    is a no-op.
  - `''` (empty string) — **blunt clear**. Writes
    `runtime_override=NULL` (revert to role default), no compact, no
    transfer event. Use only when an explicit fresh start on the role
    default is desired.
- `'codex'` is rejected when `HARNESS_CODEX_ENABLED` is unset; the
  error message tells Coach to call `coord_request_human` so the user
  can flip the env flag on the deployment.
- Mid-turn flips are rejected (mirrors `PUT /api/agents/{id}/runtime`'s
  409 behavior) — the in-flight turn would be on the old runtime
  while subsequent turns use the new one.
- Existing `model_override` is preserved across the flip. Spawn-time
  resolution silently drops a model that doesn't fit the new runtime
  and falls through to the role default; flipping back re-applies the
  preserved override.
- Updates global `agents.runtime_override` (NOT per-project — runtime
  is a global slot setting).
- Side-effect: invalidates the Codex client cache for the slot
  (`evict_client`) so a codex→claude flip drops the old subprocess +
  proxy token rather than leaving them dangling until the next
  MCP-config change.
- Emits `runtime_updated` with `agent_id=<player_id>` (NOT the caller),
  matching the shape of the HTTP runtime endpoints — the event
  renders in the target Player's pane regardless of whether the human
  or Coach initiated the flip. Coach's tool_use / tool_result pair
  already records "Coach called this tool" in Coach's timeline, so
  logging the state-change event there too would duplicate. The
  `runtime_updated` event also carries `source=session_transfer` when
  the flip came through the transfer flow rather than the blunt-clear
  branch. No `to` field — `runtime_updated` is not a fan-out type in
  either the WS-side handler or the `/api/events` SQL filter.
- Required precondition for `coord_set_player_model` when Coach wants
  a model from the other runtime family.

`coord_set_player_model(player_id, model)`

- Coach only.
- `player_id`: `p1` to `p10`.
- `model`: a TIER ALIAS (`latest_opus`, `latest_sonnet`,
  `latest_haiku` for Claude; `latest_gpt`, `latest_mini` for Codex)
  OR a concrete version id (`claude-opus-4-7`, `gpt-5.4-mini`, …) on
  the runtime-appropriate whitelist. Aliases are preferred — the
  harness resolves them to the current concrete id at spawn time, so
  a stored `latest_sonnet` automatically picks up the next Sonnet
  release. Concrete ids stay accepted for cases where a specific
  version pin matters. Empty string clears the override.
- Validated against the player's currently-resolved runtime — a Codex
  model id on a Claude-runtime player is rejected at SET time with
  an actionable error pointing to `coord_set_player_runtime` for the
  flip. If the runtime later flips, the stored override that no
  longer fits is silently dropped at spawn time and resolution falls
  through to the role default.
- Upserts `agent_project_roles.model_override` for the active project.
  Empty-clear on a player that has no row is a no-op (no orphan row
  is created).
- Emits `agent_model_set` with `to: <player_id>` so the WS / history
  fan-out renders the event in the target Player's pane as well as
  Coach's.
- Coach's system prompt includes a `MODEL_GUIDANCE` block (see
  [server/models_catalog.py]) that tells Coach: model changes are the
  exception, Sonnet is the Player default, Opus is for hard reasoning
  only, Haiku is for trivial mechanical work, Codex is the rate-limit
  fallback (`gpt-5.4-mini` as the Sonnet equivalent, top Codex tier
  reserved for heavy work).

Resolution chain in `run_agent` (highest → lowest):

1. Per-turn `model` arg from the request body (per-pane gear popover).
2. Coach-set `agent_project_roles.model_override` (this tool). Dropped
   silently at spawn time if it no longer fits the player's current
   runtime (e.g. a stored Claude id with `runtime_override='codex'`).
3. Runtime-aware per-role default in `team_config`
   (`coach_default_model` / `players_default_model` and their
   `_codex` counterparts).
4. Hardcoded role default in
   `models_catalog._ROLE_MODEL_DEFAULTS` /
   `_ROLE_CODEX_MODEL_DEFAULTS` (resolved via `role_default_model`).
   Stored as tier aliases (`latest_opus` for Coach, `latest_sonnet`
   for Players, `latest_gpt` for Codex Coach, `latest_mini` for
   Codex Players) so model bumps only touch `_ALIAS_TO_CONCRETE`.
   Every (runtime, role) combination has a concrete default — Codex
   Coach was historically empty (cost rationale) but is now
   `latest_gpt` for symmetry with Claude Coach=Opus, eliminating the
   chip's runtime-tag fallback.
5. SDK default (no `model` kwarg) — unreachable in practice now that
   every (runtime, role) combination has a hardcoded default; kept
   as a defensive last resort for forward compatibility if a future
   runtime opts out of role defaults.

Project-switch behavior: the override is keyed by `(slot,
project_id)`. Switching the active project automatically swaps which
override is read, so Coach can run different model configurations on
different projects without cross-talk.

`coord_set_player_effort(player_id, effort)`

- Coach only.
- `player_id`: `p1` to `p10` (cannot set Coach's effort via MCP — ask
  the human).
- `effort`: one of `low` | `medium` | `high` | `max`. Empty string
  clears (revert to no override). Friendly aliases (`med` →
  `medium`) and the numeric tier `1..4` (1=low … 4=max) are also
  accepted for symmetry with the UI slider.
- Stored on `agent_project_roles.effort_override` (INTEGER 1..4) for
  the active project. Empty-clear on a row that doesn't exist is a
  no-op (no orphan row).
- Resolution at spawn time: per-pane request value (highest) → this
  Coach override → role-level default (medium for both Coach and
  Players, see `models_catalog._ROLE_EFFORT_DEFAULTS`).
- Emits `agent_effort_set` with `to: <player_id>` so the event
  renders in both Coach's pane and the target Player's pane (history
  reload uses the same indexed `payload_to` filter).

`coord_set_player_plan_mode(player_id, plan_mode)`

- Coach only.
- `player_id`: `p1` to `p10`.
- `plan_mode`: `on` | `off`. Empty string clears (revert to no
  override). Aliases: `true`/`1`/`yes` → on, `false`/`0`/`no` → off.
- Stored on `agent_project_roles.plan_mode_override` (INTEGER 0/1)
  for the active project. Empty-clear no-orphan invariant matches
  the other override tools.
- Resolution at spawn time: per-pane request value (highest) → this
  Coach override → role-level default (off for both Coach and
  Players, see `models_catalog._ROLE_PLAN_MODE_DEFAULTS`). Plan mode
  is heavy (every turn pauses for ExitPlanMode review before any
  tool use), so leave it off in the common case and use it only on
  Players doing destructive / hard-to-undo work where the human
  should review the approach first.
- Emits `agent_plan_mode_set` with `to: <player_id>`.

`coord_get_player_settings(player_id?)`

- Coach only — read-only.
- `player_id`: optional. One of `p1..p10` or `coach` to scope to a
  single agent; omit for the full roster (coach + p1..p10).
- Returns a compact text table with one row per agent showing both
  the override value (what Coach set via the four `coord_set_player_*`
  tools) and the resolved value (what the agent will actually run
  with on next spawn, after fall-through to role defaults). Coach is
  expected to call this BEFORE any `coord_set_player_*` so the team
  doesn't churn already-correct settings.

### 12.9 Interactive Question/Plan Tools

`coord_answer_question(correlation_id, answers)`

- Coach only.
- Resolves a pending Player `AskUserQuestion` routed to Coach.
- Emits `question_answered`.

`coord_answer_plan(correlation_id, decision, comments?)`

- Coach only.
- Resolves a pending Player `ExitPlanMode` routed to Coach.
- Decisions:
  - `approve`
  - `reject`
  - `approve_with_comments`
- Reject and approve-with-comments require comments.
- Emits `plan_decided`.

`coord_request_human(subject, body, urgency?)`

- Any agent.
- Emits `human_attention`.
- Does not block the tool caller.
- Urgency: `normal` or `blocker`.
- Sole spontaneous-outreach channel to the human. There is no
  `Telegram_send` MCP tool: the Telegram bridge in §18.3 is a
  server-side forwarder, not callable by agents. Two delivery
  paths from an agent's perspective:
  1. **Implicit reply** — when a turn was triggered by a human
     message (UI composer or Telegram inbound), the bridge auto-
     forwards the agent's accumulated `text` events on
     `agent_stopped`. No tool call needed; just reply normally.
  2. **Spontaneous** — `coord_request_human` always forwards via
     `human_attention`, regardless of what triggered the turn,
     and is the only way an agent can push to the human's phone
     when the human hasn't just written to them.
- Coach's and Players' system prompts spell this out in the
  `coord_request_human` description so agents don't hunt for a
  non-existent Telegram tool (`PushNotification` is a Claude CLI
  built-in unrelated to this bridge — explicitly disregarded in
  the Player prompt).

### 12.10 Context editing

There is no `coord_write_context` tool. Agents edit context files
with the standard `Write` tool.

Current rule:

- Global context is `/data/CLAUDE.md`.
- Project context is `/data/projects/<active>/CLAUDE.md`.
- Humans edit through Files pane.
- Coach may edit through standard `Write` only if its tool permissions allow
  the file write path in a future design; the current Coach baseline does not
  include `Write`.

Current UI mismatch:

- The `/tools` slash-command hardcoded list still displays
  `coord_write_context` for Coach even though the MCP server no longer
  registers it.

---

## 13. Standard and External Tools

Baseline tool groups in `server/tools.py`:

```text
STANDARD_READ_TOOLS  = Read, Grep, Glob, ToolSearch
STANDARD_WRITE_TOOLS = Write, Edit, Bash
INTERACTIVE_TOOLS    = AskUserQuestion
```

Coach allowlist:

```text
STANDARD_READ_TOOLS
ALLOWED_COORD_TOOLS
AskUserQuestion
```

Player allowlist:

```text
STANDARD_READ_TOOLS
STANDARD_WRITE_TOOLS
ALLOWED_COORD_TOOLS
AskUserQuestion
```

Team-wide extra tools:

- `WebSearch`
- `WebFetch`

Controlled by:

- `GET /api/team/tools`
- `PUT /api/team/tools`
- stored in `team_config.extra_tools`

The toggle is **team-wide and runtime-shared**: one switch, both runtimes
honor it. The CamelCase names are a backwards-compat artifact (Claude
was the only runtime when storage was set); semantically the toggle
means "the team is allowed to use the web".

Runtime translation:

- ClaudeRuntime: passes the literal strings as `allowed_tools` to the
  SDK — `WebSearch` and `WebFetch` are first-class Claude tools.
- CodexRuntime: maps the toggle onto Codex's native built-in search
  (no per-URL fetch tool exists). See `Docs/CODEX_RUNTIME_SPEC.md`.

External MCP servers:

- Loaded from `HARNESS_MCP_CONFIG`.
- Loaded from `mcp_servers` DB table.
- DB wins on name collision.
- Explicit `allowed_tools` list is required; no automatic tool exposure.
- Tool names become `mcp__<server>__<tool>`.
- CodexRuntime applies a Codex-specific approval-mode injection on
  external servers; see `Docs/CODEX_RUNTIME_SPEC.md` §C.5. Claude
  runtime enforces its allow-list through
  `ClaudeAgentOptions.allowed_tools`.

---

## 14. Human REST API

All `/api/*` endpoints require bearer auth when `HARNESS_TOKEN` is set, except
`/api/health`.

### 14.1 Health and Status

| Endpoint | Notes |
| --- | --- |
| `GET /api/health` | Public readiness, returns 200 or 503 |
| `GET /api/status` | Authenticated runtime status |

Health checks:

- DB select.
- Static asset presence.
- Claude CLI version.
- Claude auth credential file presence.
- WebDAV probe, cached 60s.
- External MCP merged status — always probes `load_external_servers()`,
  which merges the legacy `HARNESS_MCP_CONFIG` file with the
  `mcp_servers` DB table. Reports the merged server count, server
  names, and total allowed-tool count. `skipped` is set only when both
  sources yield zero servers; a present-but-broken file still reports
  `error`. DB-managed servers added through the Options drawer surface
  here regardless of whether the legacy env var is set.
- Secrets store readiness.
- Workspaces git status when repo configured.
- Wiki/global resources presence.

Status includes:

- app version
- uptime
- host
- pause flag
- running slots
- WebSocket subscriber count
- cost caps and team spend today
- WebDAV enabled/reason/url
- workspaces status

### 14.2 Claude Auth

`POST /api/auth/claude`

Accepts:

```json
{"credentials_json": "...raw JSON..."}
```

or:

```json
{"credentials": {...}}
```

Requires:

- `CLAUDE_CONFIG_DIR` set.
- JSON parses.
- Top-level `claudeAiOauth` key exists.

Writes:

```text
$CLAUDE_CONFIG_DIR/.credentials.json
```

Emits `claude_auth_updated`.

### 14.3 Agents

| Endpoint | Notes |
| --- | --- |
| `GET /api/agents` | Active-project identity/session joined with global roster. Each row includes both `session_id` (Claude) and `codex_thread_id` (Codex) so the UI can detect "has session" regardless of runtime — the trash button + LeftRail activation visuals + Options-drawer batch-clear list trigger off either being non-null. |
| `POST /api/agents/start` | Start one turn |
| `POST /api/agents/{id}/cancel` | Cancel one turn |
| `POST /api/agents/cancel-all` | Cancel all running turns |
| `PUT /api/agents/{id}/identity` | Human write name/role for active project |
| `PUT /api/agents/{id}/brief` | Human write active-project brief |
| `PUT /api/agents/{id}/locked` | Set lock flag |
| `GET /api/agents/{id}/context` | Context usage estimate |
| `DELETE /api/agents/{id}/session` | Clear active-project session |
| `POST /api/agents/{id}/compact` | Queue compact turn |
| `PUT /api/agents/{id}/runtime` | Blunt set/clear of slot-level runtime override (no compact, no continuity) |
| `POST /api/agents/{id}/transfer-runtime` | Switch runtime with continuity preserved via compact (§10.3) |
| `POST /api/agents/sessions/clear` | Batch clear active-project sessions |

`POST /api/agents/{id}/transfer-runtime` body:

```json
{ "runtime": "claude" }
```

Returns 200 with `noop=true` when target equals current runtime; 200
with `queued=false` when no source-runtime session exists (immediate
flip + `session_transferred(note=no_prior_session)`); 200 with
`queued=true` when a compact turn is scheduled (watch the pane for
`session_transferred` or `session_transfer_failed`). 400 on invalid
slot / runtime / unset `HARNESS_CODEX_ENABLED`. 409 if mid-turn.

`POST /api/agents/start` body:

```json
{
  "agent_id": "p1",
  "prompt": "Do the task",
  "model": "claude-sonnet-4-6",
  "plan_mode": false,
  "effort": 3
}
```

`effort` is 1 to 4. Model string max 120 chars.

### 14.4 Coach Controls

| Endpoint | Notes |
| --- | --- |
| `GET /api/recurrences` | List active project's recurrences |
| `POST /api/recurrences` | Create repeat or cron |
| `PATCH /api/recurrences/{id}` | Edit cadence / prompt / tz / enabled |
| `DELETE /api/recurrences/{id}` | Remove a recurrence |
| `PUT /api/coach/tick` | Set / disable the recurring tick (`{minutes?, enabled?}`) |
| `POST /api/coach/tick` | Fire one tick now (smart composer) |
| `GET/POST/PATCH /api/projects/{id}/coach-todos` | Coach todos surface |
| `POST /api/projects/{id}/coach-todos/{tid}/complete` | Mark todo done |
| `GET /api/projects/{id}/coach-todos/archive` | Archived todos |
| `GET/PUT /api/projects/{id}/objectives` | Project objectives |

### 14.5 Tasks

| Endpoint | Notes |
| --- | --- |
| `GET /api/tasks?status=&owner=` | Active project tasks |
| `POST /api/tasks` | Human creates task |
| `POST /api/tasks/{task_id}/cancel` | Human cancels task |

Human task creation supports:

- title max 300 chars
- description max 10,000 chars
- optional parent id
- priority `low`, `normal`, `high`, `urgent`

### 14.6 Messages

| Endpoint | Notes |
| --- | --- |
| `POST /api/messages` | Human sends message, auto-wakes direct recipient |
| `GET /api/messages?limit=50` | Recent active-project messages |

Message body max 5000 chars. Subject max 200 chars.

### 14.7 Memory and Decisions

| Endpoint | Notes |
| --- | --- |
| `GET /api/memory` | Active-project memory list |
| `POST /api/memory` | Human upsert memory |
| `GET /api/memory/{topic}` | Read memory |
| `GET /api/decisions` | List local active-project decisions |
| `GET /api/decisions/{filename}` | Read decision file |

### 14.7.5 File-write proposals

Two scopes share one table and one set of routes: `truth` (writes
to `/data/projects/<slug>/truth/<path>`) and `project_claude_md`
(writes to `/data/projects/<slug>/CLAUDE.md`). Coach proposes via
`coord_propose_file_write(scope, path, content, summary)`; the
user reviews a diff and approves/denies here.

| Endpoint | Notes |
| --- | --- |
| `GET /api/file-write-proposals?status=&scope=&limit=` | List file-write proposals for the active project, newest first. Status filter ∈ `pending` / `approved` / `denied` / `cancelled` / `superseded`; scope filter ∈ `truth` / `project_claude_md`; omit either for all. Default limit 50, cap 200. |
| `GET /api/file-write-proposals/{id}/diff` | Returns `{id, scope, path, before, after}` so the UI can render a side-by-side diff. `before` is the current file content read fresh from disk (or `null` if the file doesn't exist yet — UI falls back to a plain proposed-content render). `after` is the proposed content. 404 if proposal missing; 400 if the row's scope/path is malformed. |
| `POST /api/file-write-proposals/{id}/approve` | Resolve a pending proposal as approved. Dispatches on scope: `truth` writes to `truth/<path>` (broader extension allowlist than the Files-pane endpoint, 200 KB cap); `project_claude_md` writes to the project's `CLAUDE.md`. Then marks the row. Body `{note}` optional. Emits `file_write_proposal_approved`. |
| `POST /api/file-write-proposals/{id}/deny` | Resolve a pending proposal as denied. No file write. Body `{note}` optional. Emits `file_write_proposal_denied`. |

All file-write-proposal endpoints are token-gated; the resolve endpoints carry an `audit_actor` payload on emitted events.

Empty-file creation under `truth/` (or anywhere else under a writable
root) goes through the standard `PUT /api/files/write/<root>?path=…`
endpoint with `content: ""` — the Files-pane "+ new file" button
(§16.5) is the UI affordance.

The resolver lives in `server/truth.py` (FastAPI-free) so the test
suite can exercise approve/deny/create flows without importing
FastAPI. Resolver exception classes
(`FileWriteProposalNotFound` / `FileWriteProposalConflict` /
`FileWriteProposalBadRequest`) translate 1:1 to 404 / 409 / 400 in the
HTTP wrappers.

### 14.8 Files

| Endpoint | Notes |
| --- | --- |
| `GET /api/files/roots` | Two roots: global and project |
| `GET /api/files/tree/{root}` | Recursive tree |
| `GET /api/files/read/{root}?path=` | Read text |
| `PUT /api/files/write/{root}?path=` | Write text (extension allowlist in `server.files.EDITABLE_EXTS`); empty body acceptable for "create stub" flows |

### 14.9 Events and Turns

| Endpoint | Notes |
| --- | --- |
| `GET /api/events` | Active-project event history with filters |
| `GET /api/turns` | Active-project turn rows (full token + runtime detail) |
| `GET /api/turns/summary?hours=24` | Per-agent spend/turn aggregate |
| `GET /api/turns/by-project` | Per-project today/total spend, plus team totals (sum of projects). Honors cost_reset_at and cost_reset_at_<project_id>. Used by the EnvPane Cost section's project dropdown. |
| `POST /api/turns/reset` | Body `{scope: "all" \| "<project_id>"}`. Writes `cost_reset_at` (global) or `cost_reset_at_<project_id>` to `team_config` so today_usd zeroes for the affected scope. Caps re-enforce from this point — historical rows are not deleted. Emits `cost_reset` event with actor metadata. |

`GET /api/events` supports:

- `agent`
- `type`
- `since_id`
- `before_id`
- `limit` max 1000

Events are returned oldest-to-newest within the page.

`GET /api/turns` returns these columns per row:

- `id`, `agent_id`, `started_at`, `ended_at`, `duration_ms`
- `cost_usd`, `session_id`, `num_turns`, `stop_reason`, `is_error`
- `model`, `plan_mode`, `effort`
- `input_tokens`, `output_tokens`, `cache_read_tokens`,
  `cache_creation_tokens` — used by `_session_context_estimate` /
  `_codex_session_context_estimate` to feed the per-pane ContextBar
- `runtime` (`claude` | `codex`), `cost_basis`
  (`token_priced` | `plan_included`) — needed to disambiguate Codex
  ChatGPT-auth turns where `cost_usd = 0` is correct rather than
  missing data.

### 14.10 Attachments

| Endpoint | Notes |
| --- | --- |
| `POST /api/attachments` | Upload pasted image to active project (Bearer auth) |
| `GET /api/attachments/{filename}` | Serve active-project image (Bearer **or** `?token=` query) |

Allowed extensions:

- `png`
- `jpg`
- `jpeg`
- `gif`
- `webp`

Storage:

- If `HARNESS_ATTACHMENTS_DIR` is set, use that legacy global dir.
- Otherwise `/data/projects/<active>/attachments/`.

Auth on the GET endpoint:

- Browsers can't set Authorization on `<img>` subresource loads, so the
  endpoint accepts `?token=<HARNESS_TOKEN>` in the query string the same
  way `/ws` does. The UI appends it when rendering attachment thumbnails
  and inline Read-of-image previews. The Bearer header still works for
  programmatic callers.

Path injected into agent prompts:

- The frontend pastes the **absolute** on-disk path returned by
  `POST /api/attachments` (`path` field, e.g.
  `/data/projects/<slug>/attachments/<id>.<ext>`) into the prompt as the
  `Read` target. Earlier code synthesized a
  `/workspaces/<slot>/attachments/...` path expecting a per-slot
  symlink that `ensure_workspaces` never created — broken for every
  slot and outright unreachable for Coach (no worktree).
- Players run with broad filesystem access. Coach's sandbox under
  CodexRuntime is configured to read the absolute `/data/...` path;
  see `Docs/CODEX_RUNTIME_SPEC.md` for sandbox details.

### 14.11 Pending Interactions

| Endpoint | Notes |
| --- | --- |
| `GET /api/questions/pending` | Pending AskUserQuestion forms |
| `POST /api/questions/{id}/answer` | Human answers question |
| `GET /api/plans/pending` | Pending ExitPlanMode plans |
| `POST /api/plans/{id}/decision` | Human approves/rejects plan |
| `POST /api/interactions/{id}/extend` | Extend deadline |

Timeout:

- `HARNESS_INTERACTION_TIMEOUT_SECONDS`, fallback
  `HARNESS_QUESTION_TIMEOUT_SECONDS`, default 1800 seconds.
- Clamped 30 to 86,400 seconds.

### 14.12 Team Configuration

| Endpoint | Notes |
| --- | --- |
| `GET /api/team/tools` | Team extra tools |
| `PUT /api/team/tools` | Set extras |
| `GET /api/team/models` | Per-role default models, split by runtime |
| `PUT /api/team/models` | Set per-role defaults, split by runtime |
| `GET /api/team/runtimes` | Per-role default runtimes |
| `PUT /api/team/runtimes` | Set per-role default runtimes |
| `GET /api/team/repo` | Legacy/global repo config |
| `PUT /api/team/repo` | Set legacy/global repo config |
| `POST /api/team/repo/provision` | Provision legacy/global workspaces |
| `GET /api/team/telegram` | Telegram status |
| `PUT /api/team/telegram` | Save Telegram config |
| `DELETE /api/team/telegram` | Clear/disable Telegram |

Model whitelist:

- empty string means SDK/default
- `claude-opus-4-7`
- `claude-sonnet-4-6`
- `claude-haiku-4-5-20251001`
- `gpt-5.5`
- `gpt-5.4`
- `gpt-5.4-mini`
- `gpt-5.4-nano`
- `gpt-5.3-codex`
- `gpt-5.2-codex`
- `gpt-5.1-codex-max`
- `gpt-5.1-codex`
- `gpt-5.1-codex-mini`
- `gpt-5-codex`

Suggested defaults (also the hardcoded role-level defaults
[server/models_catalog.py](../server/models_catalog.py) — what every
agent gets on a fresh deploy when no `team_config` row is set):

- Coach (Claude): `latest_opus` → resolves to `claude-opus-4-7`.
- Players (Claude): `latest_sonnet` → resolves to `claude-sonnet-4-6`.
- Codex Coach: `latest_gpt` → resolves to `gpt-5.5`. Mirrors the
  Claude side (Coach=Opus, Players=Sonnet) — same cost ratio as
  Claude Coach on Opus, which is the existing accepted default. The
  human can flip to a cheaper tier in the Settings drawer if running
  Coach on top-tier Codex on every tick is too expensive.
- Codex Players: `latest_mini` → resolves to `gpt-5.4-mini`.

Reasoning effort and plan-mode role-level defaults
([server/models_catalog.py](../server/models_catalog.py)):

- Effort: medium (=2) for both Coach and Players.
- Plan mode: off for both Coach and Players.

These are consulted by `run_agent` after the per-pane and Coach-set
overrides resolve to None, so a fresh deploy gets the policy-correct
combination (Coach on Opus, Players on Sonnet, medium thinking, no
plan-mode pause) without any `team_config` rows being set. The
human-set rows in the Settings drawer override these whenever
present.

### 14.13 MCP and Secrets

| Endpoint | Notes |
| --- | --- |
| `GET /api/mcp/servers` | List DB MCP servers, redacted |
| `POST /api/mcp/servers` | Save one or more server configs from paste; evicts cached Codex clients |
| `PATCH /api/mcp/servers/{name}` | Toggle enabled/tools; evicts cached Codex clients |
| `DELETE /api/mcp/servers/{name}` | Delete DB MCP server; evicts cached Codex clients |
| `POST /api/mcp/servers/{name}/test` | Smoke-test command/url |
| `GET /api/secrets` | List secret metadata and store status |
| `PUT /api/secrets/{name}` | Upsert encrypted secret |
| `DELETE /api/secrets/{name}` | Delete secret |

The three CRUD endpoints (save/patch/delete) call
`CodexRuntime.evict_all_clients()` so newly-added or removed MCP
servers take effect on each agent's next turn without a server
restart. See §19 (Codex coord MCP) for the eviction lifecycle. The
single + batch `DELETE /api/agents/{id}/session` endpoints also call
`evict_client(slot)` for the same reason — clearing a session and
clearing the cached Codex subprocess are two faces of the same
"start fresh" intent.

MCP paste shapes accepted:

- Claude Desktop style: `{ "mcpServers": { ... } }`
- TeamOfTen file style: `{ "servers": { ... } }`
- Flat single config with a supplied name.
- Bare named map.

Secret scanner warns on common raw token patterns unless `allow_secrets=true`.

---

## 15. WebSocket and Events

WebSocket:

```text
GET /ws?token=<HARNESS_TOKEN>
```

Behavior:

- Token query param is required when `HARNESS_TOKEN` is set.
- Sends `connected` immediately.
- Subscribes to `EventBus`.
- Sends live events as JSON.
- Sends `ping` every 30s of quiet.
- Does not replay backlog; clients load history through `/api/events`.

Event persistence:

- `EventBus.publish()` fans out to subscribers immediately.
- Non-transient events are queued for batched SQLite insert.
- Batch size default: `HARNESS_EVENTS_BATCH_SIZE=50`.
- Batch interval default: `HARNESS_EVENTS_BATCH_INTERVAL=0.1`.
- Queue size default: `HARNESS_EVENTS_WRITE_QUEUE_SIZE=10000`.
- If writer queue is full, falls back to single insert task.

Important event types:

Agent lifecycle:

- `agent_started`
- `text`
- `thinking`
- `text_delta`
- `thinking_delta`
- `tool_use`
- `tool_result`
- `result`
- `error`
- `agent_stopped`
- `agent_cancelled`
- `spawn_rejected`
- `paused`
- `cost_capped`
- `cost_reset` (manual reset of today_usd via `POST /api/turns/reset`)
- `session_cleared`
- `session_resume_failed`
- `session_compact_requested`
- `session_compacted`
- `session_transfer_requested` — runtime transfer queued (compact + flip on success)
- `session_transferred` — runtime flipped after a successful transfer compact, or fired immediately when no source-runtime session existed (carries `from_runtime`, `to_runtime`, optional `note=no_prior_session`)
- `session_transfer_failed` — Claude-side transfer compact returned no summary; runtime stays put
- `runtime_updated` — `agents.runtime_override` changed (carries `runtime_override`; `source=session_transfer` when fired by the transfer flow rather than a blunt PUT)
- `auto_compact_triggered`
- `auto_compact_failed`
- `compact_empty_forced`
- `context_applied`
- `context_usage`

`tool_use` payloads use Claude's renderer shape: `name`, `id`, and
`input`. Codex also carries a duplicate `tool` alias for runtime
debugging, but the UI must prefer `name` and fall back to `tool` for
older persisted Codex rows. Codex MCP calls unwrap protocol wrapper
fields and pass the actual coord_* arguments as `input`.

Task and coordination:

- `task_created`
- `task_claimed`
- `task_assigned`
- `task_updated`
- `message_sent`
- `memory_updated`
- `knowledge_written`
- `output_saved`
- `decision_written`
- `commit_pushed`
- `player_assigned`
- `agent_model_set` (Coach set/cleared a Player's model_override; carries `{player_id, to: pid, model}`. The empty-string `model` is the cleared marker.)
- `agent_effort_set` (Coach set/cleared a Player's effort_override; carries `{player_id, to: pid, effort: int|null}`.)
- `agent_plan_mode_set` (Coach set/cleared a Player's plan_mode_override; carries `{player_id, to: pid, plan_mode: 0|1|null}`.)
- `runtime_updated` (Coach or human flipped a Player's runtime_override; carries `{player_id, runtime_override: 'claude'|'codex'|null}`.)
- `brief_updated`
- `lock_updated`
- `human_attention`

Recurrences and runtime:

- `pause_toggled`
- `recurrence_added`
- `recurrence_changed`
- `recurrence_deleted`
- `recurrence_fired`
- `recurrence_skipped`
- `recurrence_disabled`
- `coach_todo_added`
- `coach_todo_completed`
- `coach_todo_updated`
- `objectives_updated`
- `team_tools_updated`
- `team_models_updated`
- `team_repo_updated`
- `team_repo_provisioned`

Projects:

- `project_created`
- `project_updated`
- `project_deleted`
- `project_switch_step`
- `project_switched`
- `project_repo_provisioned`

Integrations:

- `mcp_server_saved`
- `mcp_server_updated`
- `mcp_server_deleted`
- `mcp_server_tested`
- `secret_written`
- `secret_deleted`
- `team_telegram_updated`
- `team_telegram_cleared`
- `claude_auth_updated`
- `kdrive_sync_failed`

Interactions:

- `question_answered`
- `plan_decided`
- `interaction_extended`

File-write proposals (covers both `truth` and `project_claude_md`
scopes — payloads carry `scope`):

- `file_write_proposal_created` (emitted by
  `coord_propose_file_write` — `agent_id=coach`; payload includes
  `proposal_id`, `scope`, `path`, `summary`, `size`, and
  `superseded` listing any prior pending IDs that this proposal
  auto-superseded for the same `(scope, path)`)
- `file_write_proposal_superseded` (emitted once per old row when
  `coord_propose_file_write` fires for a `(scope, path)` with a
  pending proposal — `agent_id=system`; payload `proposal_id`,
  `superseded_by`, `scope`, `path`)
- `file_write_proposal_approved` (emitted by
  `POST /api/file-write-proposals/{id}/approve` — `agent_id=human`;
  payload includes the proposer, `scope`, `path`, summary, written
  byte size, optional note, and `actor` audit metadata)
- `file_write_proposal_denied` (parallel to approved; `size=0`)
- `file_write_proposal_cancelled` (parallel; reserved for the
  cancel resolver path)

File/browser:

- `file_written` (emitted by `PUT /api/files/write/<root>?path=…`,
  including via the Files-pane "+ new file" button which posts an
  empty body)

---

## 16. Frontend Specification

The frontend is `server/static/app.js` plus CSS and helper renderers.
It is a no-build Preact app using `htm`.

### 16.1 App Shell

Main pieces:

- Left rail.
- Tileable pane workspace.
- Agent panes for `coach`, `p1` to `p10`.
- Special Files pane (`__files`).
- Environment pane.
- Settings drawer.
- Project switcher.
- Token gate.
- Project switch modals.

State is mostly `useState`, `useMemo`, and localStorage. There is no global
state library.

### 16.2 Left Rail

Shows:

- Agent buttons for Coach and Players.
- Status dots.
- Unread/problem indicators.
- File explorer button.
- Project switcher pill.
- Pause toggle.
- Settings drawer button.
- Layout controls.
- Cancel-all control.

Project switcher:

- Lists non-archived projects.
- Shows active project check.
- Has `+ New project...`.
- Disables during switch.
- Shows spinner while switching.

### 16.3 Agent Pane

Header:

- Drag handle.
- Status dot.
- Slot label.
- Project-specific display name.
- Current task icon.
- Lock button for Players.
- Session clear button if a session id exists.
- Cancel button when working.
- Settings override dot.
- Search toggle.
- Export markdown button.
- Pop-out/stack controls.
- Settings gear.

Body:

- Loads history from `/api/events?agent=<slot>`.
- Merges live WebSocket events.
- Pairs `tool_use` with corresponding `tool_result`.
- Filters with in-pane search.
- Auto-scrolls to bottom during streaming.
- Renders structured tool cards through `server/static/tools.js`.
- Tool cards accept `event.name` or legacy `event.tool`; this keeps
  older Codex MCP history readable after the runtime began emitting
  Claude-compatible tool names. They also unwrap legacy MCP wrapper
  inputs (`args` / `arguments` / `input`) before running coord_*
  summarizers.
- Renders markdown safely with DOMPurify.
- Event timeline rendering is isolated behind a shallow event-array
  guard: local UI state changes such as plan/model/settings must not
  remap or rerender every historical `EventItem` when the event array
  itself is unchanged.
- Pane history reloads on pane/project changes, not on every WebSocket
  reconnect attempt. WebSocket reconnects use backoff so a broken or
  flapping socket cannot continuously rebuild long pane histories.
- Shows transient streaming text/thinking when token streaming is enabled.

Three-tier visual language for the event timeline (so a long pane is
scannable — the user's direct dialogue with the agent stands out
above peer chatter and tool narration):

- **Tier 1 — direct dialogue with the human.** Full `--fg` contrast
  (white-ish). Applies to `.event.text` (this agent's reply on a
  turn) and `.event.message_sent.human-thread` (any `message_sent`
  where `agent_id === "human"` or `to === "human"`, i.e. the human
  using the EnvPane Messages composer to talk to this agent).
- **Tier 2 — peer ↔ peer dialogue.** Accent-blue body text + 5%-alpha
  blue tint on the card background. Applies to
  `.event.message_sent.peer-thread` (any other `message_sent`,
  including broadcasts and inter-Player chatter), to
  `.event.task_assigned` (a task hand-off is an inter-agent comm act),
  and to `tool_use` cards for tools tagged `comm-tool`
  (`coord_send_message`, `coord_assign_task` — the *moment* the agent
  makes an inter-agent call). Distinct from Tier 1 at a glance without
  competing for attention.
- **Tier 3 — work narration.** Muted (`--muted`) body text. Applies
  to `.event.tool_use` (non-comm), `.event.tool_result`,
  `.event.thinking`, `.event.sys`, `.event.result`, and lifecycle
  markers (`.agent_started`, `.agent_stopped`, `.connected`). The
  tool-NAME word itself (`Bash`, `Read`, `Edit`, `Grep` …) keeps its
  per-category color (read=accent, write=tool, run=warn, coord=ok)
  as a built-in identity marker — only the following text (the
  path / command / args) and the friendly-phrase variants (e.g.
  `coord_*` rendered as "Reading inbox" / "Listing tasks") dim. The
  left border + `summary::before` dot also stay colored.

Errors and asks ignore the tiering and stay loud regardless: `.event.error`
red, `.event.tool_result.error` red, plus AskUserQuestion / plan-mode
/ file-write-proposal / human_attention escalations.

Routing logic for `message_sent` lives in [server/static/app.js](../server/static/app.js)'s
event renderer — it adds `.human-thread` or `.peer-thread` based on
`from`/`to` ids before the CSS tiering takes over. The `comm-tool`
class for `coord_send_message` / `coord_assign_task` is added in
[server/static/tools.js](../server/static/tools.js)'s
`renderGenericCard` via the `COMM_TOOLS` set.

Turn-header (`agent_started`) rendering rules:

- The header is sticky, single-line, and shows `arrow + ts + runtime
  chip + one-line prompt + chevron`. Click toggles expanded mode.
- When expanded the inline one-line prompt clears and only the full
  prompt block (`.turn-header-full`, dashed top border, pre-wrap)
  renders — never both at once. Avoids the prior duplication where
  the wrapped one-liner and the full block displayed the same body
  twice.
- External-wake accent: the header gets a `.wake-external` class
  when the prompt was generated by `maybe_wake_agent` from another
  party, detected by the prompt preamble in `TurnHeader`:
  `^(New message from|Coach assigned you|The operator|Player \w+
  is paused)\b`. The class repaints the prompt and full-block text
  in `var(--accent)` so the receiver spots external triggers at a
  glance. System self-retries (`Your previous turn was cut off …` /
  `Your previous turn errored …`) and recurrence-tick wakes do NOT
  match — they stay `var(--fg)` to read as routine.

Input:

- Textarea.
- Image paste/upload strip.
- Mode chips for model, plan, effort, context. Each chip shows the
  **currently running parameter**, no labels or `key:` prefix, and
  never the word "default" or "auto":
    - **Model chip** — actual model name ("Sonnet 4.6", "Opus 4.7",
      "GPT-5.1 Codex"). Resolution chain mirrors
      `server/agents.py:run_agent`'s spawn-time chain so the chip
      always reflects what the next turn will use: paneSettings.model
      → `agents[].model_override` (Coach-set per-(slot, project) via
      `coord_set_player_model`; silently skipped when it doesn't fit
      the current runtime) → `/api/team/models[role|role_codex]` →
      server-side `suggested` fallback → latest `turns.model` row for
      this slot (`/api/turns?agent=<slot>&limit=1`, refreshed on every
      `result` event) → hard-coded `ROLE_DEFAULT_ALIAS` fallback
      (mirror of `_ROLE_MODEL_DEFAULTS` /
      `_ROLE_CODEX_MODEL_DEFAULTS` in `server/models_catalog.py`) so
      the chip displays a concrete model even during the cold-start
      window before `/api/team/models` has resolved. Tier aliases
      (`latest_opus`, `latest_gpt`, …) are resolved to their concrete
      id (`MODEL_ALIAS_TO_CONCRETE` in `app.js`, mirror of
      `_ALIAS_TO_CONCRETE` in `server/models_catalog.py`) before
      label lookup so the chip reads "GPT-5.5" rather than
      "latest_gpt". Every (runtime, role) combination has a concrete
      role default — Claude is `Coach=latest_opus` /
      `Players=latest_sonnet`; Codex mirrors the same Opus/Sonnet
      shape with `Coach=latest_gpt` / `Players=latest_mini` — so the
      chip always renders a concrete model name from first paint,
      with no "Claude" / "Codex" runtime-tag fallback. The Codex
      Coach default was historically empty (rationale: top-tier
      Codex is expensive, leave it for the human to pick in
      Settings); changed to `latest_gpt` for symmetry with Claude
      Coach on Opus (same cost ratio, which the team has already
      accepted). The chip's `active` styling (and tooltip) lights up
      whenever EITHER a per-pane override OR a Coach-set override is
      in force, so a Player whose model was changed by Coach reads as
      "non-default" at a glance even before the human opens the gear
      popover. The pane's CTX bar uses the same `effectiveModelId` so
      the context-window % computes against the model the chip
      displays — `_context_window_for` in `server/agents.py` resolves
      tier aliases internally so the `/api/agents/{id}/context`
      endpoint accepts either form.
    - **Plan chip** — `plan` or `no plan`. Toggle on click. The chip
      reflects the per-pane toggle only; Coach-set
      `agent_project_roles.plan_mode_override` (set via
      `coord_set_player_plan_mode`) is consulted at spawn time in
      `run_agent` but does not currently propagate into the chip.
      Coach overrides surface in the EnvPane "Active overrides"
      section instead. Resolution at spawn time: paneSettings.planMode
      (when non-null) → `agents[].plan_mode_override` → off.
    - **Effort chip** — `low` / `med` / `high` / `max`. Resolution
      chain mirrors `server/agents.py:run_agent`'s spawn-time chain:
      paneSettings.effort → latest `turns.effort` → hard-coded
      `ROLE_DEFAULT_EFFORT` (mirror of `_ROLE_EFFORT_DEFAULTS` in
      `server/models_catalog.py` — medium for both Coach and
      Players, runtime-agnostic so Claude and Codex agents read the
      same default). Without the role-default fallback the chip
      lied about cold-start effort (read "low" when the server was
      actually about to run medium). Same caveat as the Plan chip —
      Coach-set `agent_project_roles.effort_override` is honored at
      spawn time but not yet reflected in the chip; the EnvPane
      "Active overrides" section is the human's surface for those.
  The Settings drawer's role-default save dispatches a
  `team-models-updated` window event so all open panes refresh their
  resolved model labels live.
- Slash command autocomplete.
- Prompt history.
- Ctrl/Cmd+Enter sends.
- Ctrl/Cmd+Up/Down cycles prompt history.
- Escape clears slash menu.

Pending-prompt queue (optimistic local echo + auto-retry):

- Each submitted prompt is added to a per-pane `pending` list before
  the network roundtrip and rendered as a card just above the
  composer, so the user sees their prompt instantly — display lag is
  zero, leaving only the agent's own response time.
- States: `sending` (POST in flight or waiting for `agent_started`),
  `queued` (server emitted `spawn_rejected` because the agent was
  already mid-turn — entry will auto-retry when `agent.status` leaves
  `working`), `failed` (POST hard-errored, or a `cost_capped` event
  resolved the entry; `failReason` is surfaced verbatim).
- Reconciliation: an effect watches `allEvents`. For each pending
  entry, it looks for `agent_started` (drop), `spawn_rejected` (flip
  to `queued`), or `cost_capped` (flip to `failed`) — matched by exact
  `prompt` body. Each event resolves at most one pending entry (a
  consumed-id set prevents two same-body entries from collapsing onto
  the same `agent_started`). ts comparison is numeric-ms with a 5s
  backward tolerance for clock skew, since Python's microsecond ISO
  timestamps and JS's millisecond ones don't compare correctly as
  strings.
- Auto-retry: a separate effect watches `agent.status`. When it leaves
  `working`, the oldest `queued` entry is flipped to `sending` and
  re-POSTed with the original cached `reqBody` (model / plan_mode /
  effort overrides preserved). FIFO order; one retry per idle
  transition.
- Cancel: each pending card has an `×` button to discard.
- Per-pane state, in-memory only (lost on refresh — acceptable since
  prompts not yet started leave no server-side trace anyway).

Pane settings:

- Model override.
- Plan mode toggle.
- Effort selector 1 to 4.
- Agent brief editor.

### 16.4 Slash Commands

Intercepted locally; not sent to the agent when recognized.

| Command | Behavior |
| --- | --- |
| `/plan` | Toggle pane plan mode |
| `/model` | Open model picker |
| `/effort` | Open effort picker |
| `/effort 1..4` | Set effort inline |
| `/brief` | Open brief editor |
| `/tools` | Show baseline, team extras, external MCP summary |
| `/clear` | Clear this agent's active-project session |
| `/compact` | Queue compact turn |
| `/cancel` | Cancel this pane's in-flight turn |
| `/loop` | Show Coach autoloop state |
| `/loop <seconds>` | Set Coach routine loop |
| `/loop off` | Stop routine loop |
| `/repeat` | Show Coach repeat state |
| `/repeat <seconds> <prompt>` | Start Coach repeat loop |
| `/repeat off` | Stop repeat loop |
| `/tick` | Nudge Coach now |
| `/spend` | Show 24h spend |
| `/spend <hours>` | Show spend for custom window, max 720h |
| `/status` | Show runtime summary |
| `/help` | Show slash list |

### 16.5 Files Pane

Two-root file browser:

- Global root.
- Active project root.

Features:

- Tree fetch per root.
- Root labels and scope badges.
- Opens file links from rendered conversations by matching absolute paths.
- Markdown preview and edit mode.
- **Code preview** with syntax highlighting via highlight.js for the
  registered languages (bash, css, go, html, js, json, markdown,
  python, rust, sql, typescript, xml, yaml). Toolbar offers
  preview/edit toggle alongside the markdown one. Mapping
  extension → language lives in `langForFile()` in
  `server/static/tools.js` (single source of truth shared with
  Edit-tool diff rendering).
- **Extension allowlist for previewing**: anything outside the
  text/code allowlist (`FILES_TEXT_EXTENSIONS` + `FILES_TEXT_BASENAMES`
  in `server/static/app.js`) is treated as binary — the file is still
  selected in the tree, but the editor shows a "Binary file —
  preview not supported" placeholder card and the body fetch is
  skipped entirely. Saves bandwidth and avoids rendering mojibake
  when an agent drops a PDF or image into the project tree.
- Textarea editor for any extension in the editable allowlist
  (`server.files.EDITABLE_EXTS` — `.md` / `.txt` plus common code +
  config formats: `.py`, `.js`, `.ts`, `.json`, `.yaml` / `.yml`,
  `.toml`, `.css`, `.html`, `.xml`, `.svg`, `.go`, `.rs`, `.sh`,
  `.sql`, `.csv`, `.tsv`, `.ini`, `.cfg`, etc.) and an extensionless
  basename allowlist (`Dockerfile`, `Makefile`, `README`, `LICENSE`,
  `CHANGELOG`, `.gitignore`, `.gitattributes`, …). The list mirrors
  the FilesPane's `FILES_TEXT_EXTENSIONS` / `FILES_TEXT_BASENAMES`
  so previewable files are also editable. Body cap: 100,000 chars.
- **"+ new file" button** in the pane header — prompts (HTML5
  `prompt`) for a relative path under the currently active root.
  Path is normalized (trim, strip leading `/`), then `PUT
  /api/files/write/<root>?path=…` with `content: ""`. After 200 OK
  the tree refreshes and the file opens automatically. Disabled
  when no root is active or the active root is read-only. The
  endpoint is the same one used for save, so the editable-extension
  allowlist applies — try to create `foo.bin` and you'll get a 400
  with the allowlist hint. For binary files, drop them via kDrive
  (project-sync pulls them down on next cycle).
- **Resizable tree/editor splitter**: a 6 px vertical drag handle
  between the tree and the editor; pointer-down captures the start
  width and updates flex-basis on move (clamped 140–600 px). State
  is per-component, session-only — no localStorage, every reload
  starts at the 220 px default.
- Dirty indicator.
- Ctrl/Cmd+S save.
- Read-only protections from backend path/extension validation.
- Reloads on filesystem events and project switches.

### 16.6 Environment Pane

Shows (top-to-bottom):

- Human attention banner.
- Pending questions/plans.
- kDrive sync failure banner.
- Tasks with filters.
- Cost/spend summaries with per-project dropdown and reset.
- Project objectives (multiline editor with always-visible save/discard,
  disabled when no pending changes).
- Coach todos (checkbox list + add/edit composer + archive toggle).
- Inbox/recent messages.
- Memory list/content.
- Decisions list/content.
- File-write proposals queue (Coach proposes → human approves/denies).
  Two scopes share one section: `truth` (writes under
  `truth/`) and `project_claude_md` (writes the project's
  CLAUDE.md). Each row carries a scope badge. Auto-supersede
  invariant means at most one pending row per `(scope, path)` —
  duplicates from earlier proposals simply disappear when a new
  one comes in. The expanded card shows the summary, a side-by-side
  diff between current file content and the proposed content
  (fetched lazily from `GET /api/file-write-proposals/{id}/diff`;
  new files fall back to a plain proposed-content render), and
  approve / deny buttons.
  **Discoverability surfaces** for pending proposals (so the user
  doesn't have to remember to check) — **shared by every EnvPane
  notification source**: file-write proposals, AskUserQuestion prompts
  routed to the human (`pending_question`), ExitPlanMode plan
  approvals (`pending_plan`), and `human_attention` escalations from
  `coord_request_human`. App scope tracks the union as
  `envPendingCount = attentionOpen.length + pendingFileWriteCount`;
  attention state (`pendingHumanQuestions`, `pendingHumanPlans`,
  `persistedAttention`, `dismissedAttention`) lives at App scope —
  not inside `EnvAttentionSection` — so all of the surfaces below
  fire whether or not the EnvPane is mounted.
    1. **Amber-pulsing env-toggle.** The ▦ icon on the left-rail
       env-toggle button recolours to `var(--warn)` and a soft amber
       `box-shadow` glow breathes around the button (1.8s keyframe,
       same shape as `.slot.state-working`) whenever
       `envPendingCount > 0`. Visible even when the EnvPane is
       closed. Title + `aria-label` spell out the count.
    2. **Auto-pop-open.** An App-scope `useRef` tracks the previous
       `envPendingCount`; on every positive transition, `setEnvOpen
       (true)` fires. Page-load with leftover items lands as 0 → N
       (auto-opens once); a fresh WS event arriving while the pane
       is closed pops it open; dismissals (N → 0) never re-trigger
       (strict `>` comparison). The user can still close the pane
       manually after dismissing — it stays closed until the next
       new item arrives.
    3. **`EnvAttentionSection` is presentational.** It receives
       `open` / `onDismiss` / `onDismissAll` from App as props. The
       dismissed set persists in `localStorage` under
       `harness_attention_dismissed_v1` (capped at 200 ids).
       Dedup / dismissal keys are **content-based**
       (`ha:${ts}:${agent_id}` for `human_attention`,
       `pq:${correlation_id}` for `pending_question`,
       `pp:${correlation_id}` for `pending_plan`) — not the SQLite
       row id. Live WS events arrive without the row id (the bus
       fans them out before the batched writer assigns one), so a
       row-id-based key would split the same event into two cards
       (one from `persistedAttention` with `__id`, one from the live
       `conversations` copy without) and break dismissal across
       reloads. ISO ts has microsecond precision so `ts + agent_id`
       is unique enough; correlation ids are server-assigned and
       stable across both ingestion paths.
    4. **`EnvFileWriteProposalsSection` auto-expand.** When there's
       at least one pending row AND the user has never explicitly
       collapsed it (no localStorage entry), the section opens.
       Once the user toggles, that explicit choice wins on future
       opens. Driven by a `data-pending-count` attribute on the
       section root; the collapse-init `MutationObserver` watches
       that attribute via `attributeFilter` so a fresh proposal
       arriving after mount still re-opens the section.
- Timeline of important events.

It scopes project-sensitive sections to the active project through the
API. `Project objectives` and `Coach todos` reload automatically when
the active project changes — both are stored on disk under the
project's slug (`/data/projects/<slug>/project-objectives.md` and
`/data/projects/<slug>/coach-todos.md`).

**Collapsible sections.** Every section except the warning banners
(Attention, kDrive errors) is wrapped in `.env-section.collapsible`:
Tasks, Cost, Project objectives, Coach todos, Messages, Memory,
Decisions, File-write proposals, Timeline. Click the section title to
toggle; state persists per-section in `localStorage` under
`harness_env_collapsed_v2`. Default state is **closed** — the user
opens what they need, like the Settings drawer (§16.7). Warning
banners are always-expanded since collapsing them would hide
actionable signal. The collapse mechanic is the same shared pattern as
the Settings drawer — CSS-drawn chevron, h3 click handler that
ignores interactive children (buttons, inputs, etc.) so inline
controls in section titles still work.

### 16.7 Settings Drawer

Contains:

- Runtime/health summary.
- Claude auth paste flow.
- Team tools.
- Team default models.
- Project repo legacy/global config.
- Projects section.
- Telegram bridge.
- MCP servers.
- Encrypted secrets.
- Sessions clear.
- Display/layout options.
- About/help text.

**Collapsible sections.** Every `.drawer-section` is collapsible —
click the section title (h3) to toggle. State persists per-section in
`localStorage` under `harness_drawer_collapsed_v1`. Default is
**closed** (opposite of the Environment pane's default-open) so the
drawer opens to a compact list of titles instead of a long scroll.
Click handler ignores interactive children (e.g. Health's refresh
button) so inline controls keep working. The h3-title key is
extracted from the first non-empty text node, so inline counts /
button text changes don't drift the persistence key. Same pattern is
reused in the Environment pane (§16.6) for parameter sections.

Projects section:

- Lists all projects.
- Active marker.
- Archived dimming.
- Edit name/description/repo URL.
- Archive/unarchive.
- Delete non-misc projects.
- Provision now per project.
- Expand to view `agent_project_roles` for that project.

### 16.8 Token Gate

When API returns 401 and `HARNESS_TOKEN` is required:

- UI shows a token overlay.
- Token is stored in localStorage.
- WebSocket uses `?token=`.

### 16.9 Mobile Layout

A `@media (max-width: 700px)` block in `server/static/style.css` reflows the
app for phones:

- Left rail moves to the bottom and splits across two grid rows.
- The panes area becomes a horizontal swipe deck via
  `scroll-snap-type: x mandatory` on `.panes`. Each `.pane-col` is forced
  to `min-width: 100%` so one pane fills the screen.
- Split.js gutters, pane drag-zones, layout-preset buttons, and the
  maximize button are hidden — they don't fit single-pane navigation and
  HTML5 drag-and-drop doesn't work on touch.
- `EnvPane` becomes a full-screen overlay when toggled open.

Pane ordering on phones is canonical, not history-based. `useIsPhone()`
in `app.js` listens to the `(max-width: 700px)` media query; when active,
`effectiveColumns` flattens all open slots, sorts them by
`CANONICAL_SLOT_ORDER` (`coach`, `p1`..`p10`, then special slots like
`__files` / `__projects` in insertion order), and singletonizes them into
one slot per column. The swipe deck therefore always reads
Coach → 1 → 2 → … regardless of the order panes were opened. Desktop
layout keeps the user's 2D `openColumns` structure intact.

### 16.10 Markdown Render Pipeline

`server/static/markdown.js` is the single chokepoint for everything
markdown-shaped in the UI: agent panes, files `.md` preview,
compass briefings, decisions, wiki entries. Six-stage pipeline:

1. **Parse** — `marked@12` (GFM) with a custom code-renderer:
   - fence lang ∈ hljs registry → highlighted `<pre><code>`
   - fence lang === `mermaid` → `<pre class="md-mermaid">` placeholder
   - everything else → escaped `<pre><code>`
2. **Math (parse-time)** — KaTeX inline + block extension (hand-
   rolled inline; the npm `marked-katex-extension` package's esm.sh
   stub imports `katex` from the CDN, which 404s when served from
   our `/static/vendor/` origin). Inline `$...$`, block `$$\n...\n$$`.
   Output mode `htmlAndMathml` emits both styled HTML (visual) and
   hidden MathML (so equations copy-paste into Word as real equation
   objects, not as flat text). `throwOnError: false` → invalid LaTeX
   renders red inline instead of blowing up the whole message.
3. **Callouts (parse-time)** — Obsidian / GFM-Alerts compatible:
   `> [!type]`, optionally `> [!type]+` (open `<details>`) or
   `> [!type]-` (collapsed `<details>`), optional title text on the
   header line, body lines following the standard blockquote shape.
   12 colour themes (note, abstract, info, todo, tip, success,
   question, warning, failure, danger, example, quote) plus aliases
   (`summary`/`tldr` → abstract, `hint`/`important` → tip,
   `check`/`done` → success, `help`/`faq` → question, `caution`/
   `attention` → warning, `fail`/`missing` → failure, `error`/`bug`
   → danger, `cite` → quote). Unknown types fall back to `note`. The
   tokeniser pre-lexes title and body so nested markdown — bold,
   links, code, even nested lists — works inside callouts.
4. **Sanitise** — `DOMPurify@3` with `USE_PROFILES: { html: true,
   mathMl: true }`. The `afterSanitizeAttributes` hook rewrites
   `<a>` hrefs: external URLs get `target=_blank` + `rel=noreferrer
   noopener`; paths starting with `/` are tagged
   `data-harness-path` and the href is neutralised to `#` so the
   global click handler in `App` can route them to the Files pane.
5. **Mount** — consumer drops the sanitised string into Preact via
   `dangerouslySetInnerHTML`.
6. **Mermaid post-render** — a single `MutationObserver` rooted at
   `document.body` (installed once at app boot via
   `enhanceMarkdownIn`) watches for `<pre class="md-mermaid">`
   inserts. First hit lazy-loads `mermaid.min.js` (~3MB UMD
   bundle, fetched via dynamic `<script>` tag because mermaid's
   ESM build splits into 30+ chunks); subsequent hits reuse the
   loaded `window.mermaid`. A `WeakSet` de-dupes already-processed
   nodes; a `Map<source, svg>` cache makes re-renders instant when
   Preact remounts the same diagram text. Failed renders show the
   error inline (title + message + source) so authors can fix
   without opening devtools.

`renderMarkdown` returns the sanitised HTML string. `hljs` and
`DOMPurify` are re-exported so other modules (`tools.js`, code-
preview helper in `app.js`) reuse the configured singletons —
language packs are registered exactly once, the link-rewrite hook
is installed exactly once. `tools.js` imports `hljs` from
`markdown.js` (not from `/static/vendor/hljs-core.js`) so module
evaluation order pins language registration before the first
code-render call.

Vendor strategy in `scripts/vendor_deps.py` is three-tier:
- `DEPS` — ESM modules fetched with esm.sh's `?bundle` flag (one
  self-contained file per dep). Sanity-checked for stray
  `https://esm.sh/` imports on disk.
- `NON_ESM_DEPS` — UMD/IIFE bundles fetched as-is (currently
  `mermaid.min.js`). Loaded via dynamic `<script>` tag, not via
  the module pipeline.
- `CSS_DEPS` — plain CSS (hljs theme + KaTeX). KaTeX CSS goes
  through `_CSS_REWRITES` to convert relative `fonts/...` URLs
  to absolute jsdelivr URLs — avoids vendoring 12 binary font
  files; the browser fetches each font on first use and caches
  forever.

The wiki skill template (`server/templates/llm_wiki_skill.md`)
documents math + mermaid syntax for agents authoring wiki
entries; both render identically in the harness UI and in
Obsidian (kDrive-synced view).

---

## 17. Git Workspaces

Configured by (DB is the source of truth; env vars are a legacy
fallback used only when no DB row is set):

- `team_config.project_repo` (set via Options → Project repo)
- `team_config.project_branch`

Legacy env fallback:

```text
HARNESS_PROJECT_REPO
HARNESS_PROJECT_BRANCH
```

Current implementation:

```text
/workspaces/.project
/workspaces/coach/
/workspaces/p1/project
/workspaces/p2/project
...
```

Startup:

- Always creates plain slot dirs.
- If no repo configured, agents run in plain dirs.
- If repo configured:
  - clone to `/workspaces/.project`
  - create worktree per slot at `/workspaces/<slot>/project`
  - branch per slot: `work/<slot>`
  - reuses local branch if present
  - tracks remote `origin/work/<slot>` if present
  - otherwise creates new branch off configured branch

`workspace_dir(slot)`:

- Returns worktree if it exists and is git.
- Otherwise returns plain `/workspaces/<slot>`.

`coord_commit_push` requires the returned cwd to contain `.git`.

Known hybrid:

- This does not yet honor the fully project-scoped repo layout under
  `/data/projects/<slug>/repo/`.

---

## 18. Integrations

### 18.1 External MCP

UI-managed via the `mcp_servers` DB table (Options drawer → MCP
servers); credentials live in the encrypted secrets store keyed
by `HARNESS_SECRETS_KEY`. The legacy file-config path
(`HARNESS_MCP_CONFIG=/data/mcp-servers.json`) is still loaded if
set, but DB entries override file entries on conflict.

Example file shape (also valid as DB row JSON):

```json
{
  "servers": {
    "github": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}
    }
  },
  "allowed_tools": {
    "github": ["create_issue", "list_issues"]
  }
}
```

Interpolation order for `${VAR}`:

1. Encrypted secrets store.
2. Environment variables.
3. Empty string with warning.

DB-managed MCP servers:

- Saved through Settings drawer.
- Redacted on read.
- Inline secret warnings by regex.
- `test` endpoint checks stdio command path or HTTP reachability.

### 18.2 Secrets Store

Requires:

```text
HARNESS_SECRETS_KEY=<Fernet key>
```

Secret names must match:

```text
^[A-Za-z_][A-Za-z0-9_]{0,63}$
```

Names are entered plain (e.g. `ZEABUR_API_KEY`). The `${NAME}` wrapper
is the *placeholder syntax* used inside config files that interpolate
the secret (MCP configs, repo URLs, anything else routed through
`_interpolate`) — not the secret's name. The Settings drawer input
auto-strips a `${NAME}` or `$NAME` wrapper before submission so a
copy-paste from an MCP config doesn't fail validation. Server-side
validation still enforces the regex above and rejects malformed names
with a 400.

Secrets are general-purpose. They can be referenced anywhere the
harness expands `${VAR}` placeholders — MCP server configs, the
project repo URL, future config fields. The store wins over `os.environ`
on name collision so a UI-stored secret transparently overrides any
matching env var.

Values max 32,768 chars through API.

### 18.3 Telegram Bridge

Purpose: send messages to Coach from a phone and receive Coach replies.

Config sources:

- Secret `telegram_bot_token`, env fallback `TELEGRAM_BOT_TOKEN`.
- Secret `telegram_allowed_chat_ids`, env fallback
  `TELEGRAM_ALLOWED_CHAT_IDS`.

Behavior:

- Long-polling `getUpdates`, no webhook needed.
- Only whitelisted numeric chat ids can pilot Coach.
- Inbound text becomes a human message to Coach and wakes Coach.
- `/start` gets a short connection message.
- Non-text messages get a "text only" reply.
- Outbound forwards Coach text only for turns triggered by human-to-Coach
  messages; routine/autonomous turns are silent.
- `human_attention` is always forwarded.
- Replies are split under 4000 chars.
- After repeated 401/403 auth failures, bridge stops and emits
  `human_attention`.

UI endpoints support live reload without redeploy.

#### 18.3.1 Escalation Watcher

A separate background task (`server/telegram_escalation.py`,
`start_escalation_watcher()` in `lifespan`) pings the same
whitelisted chats when a pending-attention item goes unanswered
for too long. Independent of the bridge's outbound buffer; uses
`server.telegram.send_outbound(text)` which resolves the disabled
flag + token + chat_ids fresh on every call (so a UI Clear stops
escalations immediately).

Watched events:

- `pending_question` with `route='human'` (AskUserQuestion).
- `pending_plan` with `route='human'` (ExitPlanMode plan approval).
- `file_write_proposal_created` (truth or `project_claude_md` scope).

Resolution events that cancel the timer:

- `question_answered` / `question_cancelled` (matched on
  `correlation_id`).
- `plan_decided` / `plan_cancelled` (matched on `correlation_id`).
- `file_write_proposal_approved` / `_denied` / `_cancelled` /
  `_superseded` (matched on `proposal_id`).

Delay model:

- `HARNESS_TELEGRAM_ESCALATION_SECONDS` (default 300; 0 disables
  the watcher).
- `HARNESS_TELEGRAM_ESCALATION_GRACE` (default 5).
- Branch chosen at schedule time: full delay when
  `bus.subscriber_count > 0` (web active), grace delay otherwise.

`pending_question(route='coach')` and `pending_plan(route='coach')`
are explicitly ignored — Coach is responsible for those, not the
human. `human_attention` keeps the bridge's existing immediate
forwarding (the agent has already declared "I can't proceed";
adding a delay would slow the most-urgent signal).

Telegram message includes context: agent slot + name + role label
(via `_get_agent_identity`), `ts` and `deadline_at` rendered as
`HH:MM UTC`, the structured questions array (or plan body
truncated to 1500 chars, or proposal scope+path+summary+size),
and a "Open the web UI to answer" footer.

Restart behaviour: timers are in-memory only. A
`file_write_proposal_created` open before a server restart keeps
its `status='pending'` row in the DB but does not re-arm a timer
on next boot. The EnvPane still surfaces it on reconnect.

---

## 19. Security and Auth

### 19.1 UI/API

`HARNESS_TOKEN`:

- If unset: API is open.
- If set:
  - all `/api/*` except `/api/health` require `Authorization: Bearer <token>`
  - WebSocket requires `?token=<token>`

This is single-user security, not a multi-user auth system.

Audit metadata:

- Destructive human actions include `actor` with source, IP, and User-Agent.

### 19.2 Claude OAuth

Default:

```text
CLAUDE_CONFIG_DIR=/data/claude
```

The CLI stores `.credentials.json` and `.claude.json` there. The API can write
a pasted credentials JSON so the operator does not need shell access.

Health reports whether `.credentials.json` exists.

### 19.3 WebDAV Credentials

WebDAV credentials are env vars:

- `HARNESS_WEBDAV_URL`
- `HARNESS_WEBDAV_USER`
- `HARNESS_WEBDAV_PASSWORD`

They are not exposed through API beyond enabled/reason/url status.

### 19.4 MCP/Telegram Secrets

UI-managed secrets are encrypted in SQLite. API never returns plaintext. The
runtime interpolator can read them for MCP/Telegram use.

### 19.5 Per-agent runtime selection

Two runtimes ship: **ClaudeRuntime** (default; described inline
throughout this doc) and **CodexRuntime** (gated by
`HARNESS_CODEX_ENABLED`; full spec in `Docs/CODEX_RUNTIME_SPEC.md`).

Resolution at spawn time:
`agents.runtime_override` → `team_config` role default
(`coach_default_runtime` / `players_default_runtime`) → `'claude'`.

`PUT /api/agents/{id}/runtime` sets the per-slot override. `'codex'`
is rejected when `HARNESS_CODEX_ENABLED` is unset. Mid-turn flips
return 409. The PUT path is the **blunt** flip — it writes
`runtime_override` and the next turn on the new runtime starts with
no memory of the prior conversation. Use `POST /api/agents/{id}/transfer-runtime`
when the agent has a session worth carrying — see §10.3 for the
transfer flow that runs `/compact` first and only flips on success.
The pane gear popover routes through `transfer-runtime` when the
user picks a concrete runtime and falls back to the blunt PUT only
when the user picks `default` (clear the override).

Model selection is runtime-aware: Claude defaults
(`coach_default_model` / `players_default_model`) and Codex defaults
(`coach_default_model_codex` / `players_default_model_codex`) are
stored separately. The pane gear resolves the effective runtime
first, then chooses the Claude or Codex model list. A stored
`agent_project_roles.model_override` that no longer fits the slot's
current runtime is silently dropped at spawn time.

`agent_started` payload carries `runtime`. Successful turns insert a
`turns` row with `runtime` and `cost_basis` populated. The
`team_runtimes_updated` WebSocket event refreshes pane state when the
Options drawer changes role defaults.

### 19.6 Coord MCP proxy (loopback)

CodexRuntime cannot host an in-process Python MCP server, so
`coord_*` calls route through a stdio subprocess
(`python -m server.coord_mcp`) that forwards to the main FastAPI
process via two internal endpoints:

- `POST /api/_coord/{tool_name}` — dispatches to the in-process
  coord handler.
- `GET /api/_coord/_tools` — tool catalog for the subprocess to
  publish over MCP `tools/list`.

Both are loopback-only and bearer-token gated
(`HARNESS_COORD_PROXY_TOKEN` env). The token is minted by
`server.spawn_tokens.mint(caller_id)` and bound to the caller — the
endpoint resolves `caller_id` from the token, not the request body.
ClaudeRuntime is unaffected; it uses an in-process MCP server and
never touches these endpoints.

Token lifetime, MCP cache invalidation on config change,
`default_tools_approval_mode` injection, and the stdio error-shape
contract are CodexRuntime concerns — see
`Docs/CODEX_RUNTIME_SPEC.md` §C.4 and §E.1.

---

## 20. Environment Variables

Operator-facing env vars (the minimum you actually configure per
deploy) live in [`.env.example`](../.env.example): `HARNESS_TOKEN`,
`HARNESS_WEBDAV_URL` + `_USER` + `_PASSWORD`, `HARNESS_AGENT_DAILY_CAP`
+ `HARNESS_TEAM_DAILY_CAP`, `HARNESS_CODEX_ENABLED`,
`HARNESS_SECRETS_KEY`, and the `TELEGRAM_*` first-boot bootstrap
pair. Everything else has a Dockerfile-baked value, a code default,
or has moved to UI/DB management.

Full reference (every `os.environ.get("HARNESS_…"` site in the
implementation):

| Variable | Default | Purpose |
| --- | --- | --- |
| `HARNESS_TOKEN` | unset | Optional API/WS bearer token |
| `CLAUDE_CONFIG_DIR` | `/data/claude` | Claude OAuth/session dir |
| `CODEX_HOME` | `/data/codex` | Codex CLI auth dir (`auth.json`). Must point at persistent storage; after deploy run `CODEX_HOME=/data/codex codex login --device-auth` in the container to create the ChatGPT OAuth session. |
| `HARNESS_CODEX_ENABLED` | unset | Codex runtime feature gate. Must be truthy (`true`, `1`, `yes`, `on`) before `PUT /api/agents/{id}/runtime` or the UI runtime controls can select `runtime=codex`. |
| `HARNESS_DB_PATH` | `/data/harness.db` | SQLite path |
| `HARNESS_DATA_ROOT` | `/data` | Global/project data root |
| `HARNESS_PROJECT_REPO` | unset | **Legacy.** Single-project fallback when no project row in the DB has `repo_url`. Removed from `.env.example`; the `projects` table is the source of truth now. |
| `HARNESS_PROJECT_BRANCH` | `main` | **Legacy.** Paired with `HARNESS_PROJECT_REPO`. |
| `HARNESS_WORKSPACES_ROOT` | `/workspaces` | Actual workspace root |
| `HARNESS_WEBDAV_URL` | unset | WebDAV base folder URL |
| `HARNESS_WEBDAV_USER` | unset | WebDAV username |
| `HARNESS_WEBDAV_PASSWORD` | unset | WebDAV app password |
| `HARNESS_WEBDAV_SNAPSHOT_INTERVAL` | `300` | DB snapshot cadence |
| `HARNESS_WEBDAV_SNAPSHOT_RETENTION` | `144` | Snapshot count |
| `HARNESS_PROJECT_SYNC_INTERVAL` | `300` | Active project file sync |
| `HARNESS_GLOBAL_SYNC_INTERVAL` | `1800` | Global file sync |
| `HARNESS_KDRIVE_RETRY_MAX` | `3` | WebDAV per-file retry attempts |
| `HARNESS_KDRIVE_RETRY_INITIAL_S` | `1.0` | Initial retry delay |
| `HARNESS_KDRIVE_RETRY_CAP_S` | `30.0` | Retry delay cap |
| `HARNESS_KDRIVE_CLOSE_TIMEOUT_S` | `60` | Switch push-on-close timeout |
| `HARNESS_LIVE_CONVERSATION_S` | `30` | Recent conversation live tag window |
| `HARNESS_AGENT_DAILY_CAP` | `5.0` | Per-agent daily spend cap |
| `HARNESS_TEAM_DAILY_CAP` | `20.0` | Team daily spend cap |
| `HARNESS_COACH_TICK_INTERVAL` | `0` | **Deprecated.** Honored only on first migration to seed a tick row in `coach_recurrence`. After that the env var is ignored — runtime control is via `PUT /api/coach/tick` or `/tick N`. Removed from `.env.example`. |
| `HARNESS_RECURRENCE_TICK_SECONDS` | `30` | Scheduler resolution for `recurrence_scheduler_loop` |
| `HARNESS_MAX_RECURRENCES_PER_PROJECT` | `50` | Soft cap per project; POST 409s when exceeded |
| `HARNESS_AUTOWAKE_DEBOUNCE` | `10` | Auto-wake debounce seconds |
| `HARNESS_ERROR_RETRY_DELAY` | `45` | Error retry delay |
| `HARNESS_ERROR_RETRY_MAX_CONSECUTIVE` | `3` | Retry limit |
| `HARNESS_ERROR_DM_DEBOUNCE` | `300` | Player-error Coach DM debounce |
| `HARNESS_STALE_TASK_MINUTES` | `15` | Stale task threshold, 0 disables |
| `HARNESS_STALE_TASK_NOTIFY_INTERVAL_MINUTES` | `30` | Re-notify cadence |
| `HARNESS_STALE_TASK_CHECK_INTERVAL_SECONDS` | `60` | Watchdog loop cadence |
| `HARNESS_AUTO_COMPACT_THRESHOLD` | `0.7` | Context fraction for auto-compact |
| `HARNESS_HANDOFF_TOKEN_BUDGET` | `20000` | Recent exchange budget |
| `HARNESS_STREAM_TOKENS` | unset | Enable token delta streaming |
| `HARNESS_INTERACTION_TIMEOUT_SECONDS` | `1800` | Question/plan timeout |
| `HARNESS_MCP_CONFIG` | unset | **Legacy.** Path to a static MCP server JSON file. Removed from `.env.example`; the `mcp_servers` table (Options drawer → MCP servers) is the source of truth. DB entries override file entries when both exist. |
| `HARNESS_SECRETS_KEY` | unset | Fernet master key |
| `HARNESS_EVENTS_RETENTION_DAYS` | `30` | Event trim window |
| `HARNESS_EVENTS_TRIM_INTERVAL` | `86400` | Event trim cadence |
| `HARNESS_ATTACHMENTS_RETENTION_DAYS` | `30` | Attachment trim window |
| `HARNESS_SESSION_RETENTION_DAYS` | `30` | Claude JSONL trim window |
| `HARNESS_EVENTS_BATCH_SIZE` | `50` | Event writer batch size |
| `HARNESS_EVENTS_BATCH_INTERVAL` | `0.1` | Event writer flush window |
| `HARNESS_EVENTS_WRITE_QUEUE_SIZE` | `10000` | Event writer queue |
| `HARNESS_ATTACHMENTS_DIR` | project-scoped unless set | Legacy attachment override |
| `HARNESS_OUTPUTS_DIR` | `/data/outputs` | Legacy outputs dir |
| `TELEGRAM_BOT_TOKEN` | unset | Telegram env fallback |
| `TELEGRAM_ALLOWED_CHAT_IDS` | unset | Telegram env fallback |
| `HARNESS_TELEGRAM_ESCALATION_SECONDS` | `300` | Delay before pinging Telegram for an unanswered pending-attention item when the web UI is connected. `0` disables the escalation watcher. |
| `HARNESS_TELEGRAM_ESCALATION_GRACE` | `5` | Delay used instead of the long delay when no WebSocket subscriber is connected at the time the pending event arrives. |
| `PORT` | `8000` | Uvicorn port |

Removed from `.env.example` (kept here for change-log audit; do not
add back unless re-wiring the corresponding code path):

- `HARNESS_CONTEXT_DIR` — never referenced in code.
- `HARNESS_KNOWLEDGE_DIR` — never referenced in code.
- `HARNESS_DECISIONS_DIR` — never referenced in code.
- `HARNESS_HANDOFFS_DIR` — never referenced in code.
- `HARNESS_WORKSPACES_DIR` — wrong name; the code reads
  `HARNESS_WORKSPACES_ROOT` instead.

Also dropped from `.env.example` (still wired, but defaulted in code
or in the Dockerfile and not configured per-deploy in practice):

- `CLAUDE_CONFIG_DIR` / `CODEX_HOME` — set in the Dockerfile to
  `/data/claude` and `/data/codex`. Override only if the persistent
  volume mount differs.
- `HARNESS_DATA_ROOT` (`/data`) and `HARNESS_WORKSPACES_ROOT`
  (`/workspaces`) — code defaults match the Dockerfile mount points.
- `HARNESS_DB_PATH`, `HARNESS_OUTPUTS_DIR`, `HARNESS_UPLOADS_DIR`,
  `HARNESS_ATTACHMENTS_DIR` — derived from `HARNESS_DATA_ROOT`.
- All retention / interval / debounce / batch-size / threshold
  tuning vars (auto-compact, handoff token budget, error retry,
  stale-task watchdog, event batcher, WebDAV intervals + retries,
  project-sync intervals, recurrence tick resolution, etc.) — code
  defaults are documented at each `os.environ.get` call site.

`.env.example` is the operator-facing minimum; this section is the
implementation-facing complete reference.

---

## 21. Retention and Cleanup

Events:

- `trim_events_once()` deletes SQLite events older than
  `HARNESS_EVENTS_RETENTION_DAYS`.
- 0 disables.
- Loop interval `HARNESS_EVENTS_TRIM_INTERVAL`.

Attachments:

- `trim_attachments_once()` deletes old files.
- Uses override `HARNESS_ATTACHMENTS_DIR` if set.
- Otherwise scans each project under `/data/projects/<slug>/attachments`.
- 0 disables.

Claude sessions:

- `trim_sessions_once()` trims JSONL session files under
  `CLAUDE_CONFIG_DIR/projects/`.
- Window `HARNESS_SESSION_RETENTION_DAYS`.
- 0 disables.

Snapshots:

- WebDAV snapshot retention prunes old `snapshots/*.db` beyond configured
  count.

---

## 22. Tests

The full suite is 649/649 green (Python 3.12, pytest-asyncio).

Test areas include:

- DB init and schema (incl. the legacy `truth_proposals` →
  `file_write_proposals` rename migration).
- Task state machine.
- Event bus and batched persistence behavior.
- Turn ledger.
- Agent helper functions.
- Auto-naming.
- Concurrent spawn guard.
- Crash recovery.
- Retention.
- Files backend.
- Knowledge backend.
- MCP config.
- Telegram.
- Projects API.
- Project isolation.
- Project sync.
- Phase 7 project prompt/wiki behavior, including:
  - Truth + project-CLAUDE.md PreToolUse hook coverage
    (`_pretool_file_guard_hook`).
  - `coord_propose_file_write` scope validation (truth scope path
    rules, project_claude_md exact-path enforcement, unknown-scope
    rejection, scope-isolated supersede).
  - Resolver scope dispatch (`resolve_file_write_proposal` writes
    truth files OR project CLAUDE.md based on scope; tampered-path
    and unknown-scope defenses).
  - `resolve_target_path` export verification (the `/diff` endpoint
    in `main.py` depends on it).

Run locally:

```bash
uv sync --extra dev
uv run pytest -ra --strict-markers
```

CI runs `.github/workflows/tests.yml` on push and PR.

The frontend has no JS unit tests yet; markdown-pipeline behaviour
(math rendering, mermaid lazy-load, file-link hook routing) is
verified by hand in the browser. JS files are syntax-checked with
`node --check` before commit; logic checks rely on the user
exercising the relevant pane after a deploy.

---

## 23. Current Implementation Gaps and Watch Items

These are not hidden defects in this spec; they are the places where the code
and the desired architecture are still not perfectly aligned.

1. Project repo storage is hybrid.
   - DB/API has per-project `projects.repo_url`.
   - `server.paths` declares per-project repo paths.
   - `server/workspaces.py` still uses global `/workspaces/.project` and
     `/workspaces/<slot>/project`.

2. Outputs are partly wired.
   - `server/outputs.py` exists.
   - `coord_save_output` function exists.
   - `ALLOWED_COORD_TOOLS` includes it.
   - The MCP server registration list currently omits the function.
   - Storage is global `/data/outputs`, not project-scoped.

3. Attachment prompt paths can be wrong in project-scoped mode.
   - Upload API stores under active project's attachments dir by default.
   - Frontend injects `/workspaces/<slot>/attachments/<file>` paths.
   - Docker symlink points to `/data/attachments`, not active project
     attachments.

4. Memory WebDAV mirror path is inconsistent with v2 local layout.
   - Local project memory path is `working/memory`.
   - `coord_update_memory` mirrors to `projects/<slug>/memory/<topic>.md`.

5. UI `/tools` help still mentions `coord_write_context`, which was removed.

6. (Resolved 2026-05-02.) `.env.example` is now the operator-facing
   minimum (auth, WebDAV, caps, secrets-key, Codex gate, Telegram
   bootstrap). Pre-projects flat-dir vars
   (`HARNESS_CONTEXT_DIR` / `_KNOWLEDGE_DIR` / `_DECISIONS_DIR` /
   `_HANDOFFS_DIR` / `_WORKSPACES_DIR`) and legacy single-project
   knobs (`HARNESS_PROJECT_REPO`, `HARNESS_PROJECT_BRANCH`,
   `HARNESS_MCP_CONFIG`, `HARNESS_COACH_TICK_INTERVAL`) were
   removed. See §20 for the full implementation reference + change
   log.

7. (Resolved 2026-05-01.) Coach edits the per-project CLAUDE.md via
   `coord_propose_file_write(scope='project_claude_md', path='CLAUDE.md',
   content, summary)`; the user reviews a diff and approves in the EnvPane
   "File-write proposals" section. Direct `Write` / `Edit` / `Bash` against
   `/data/projects/<slug>/CLAUDE.md` is hard-denied by the
   `_pretool_file_guard_hook` (the same hook that protects `truth/`), so
   the proposal flow is the only path in. The harness-wide
   `/data/CLAUDE.md` remains user-only (no proposal scope for it).

8. Project activation rejects any in-flight agent instead of implementing a
   full "cancel turns and switch" server path. The UI has modal language for
   wait/cancel, but the backend switch endpoint itself expects no in-flight
   work.

9. Project sync assumes the harness is the sole writer while a project is
   active. Direct WebDAV edits to active project files may be overwritten.

10. Mobile/touch drag behavior remains less mature than desktop layout.

---

## 24. Hard Invariants

1. Agents must not write SQLite directly. All coordination mutations route
   through server APIs or MCP tool handlers.
2. One active project id scopes all project-state reads and writes.
3. `misc` must always exist.
4. `project_id` filters are required on tasks, messages, memory, events, turns,
   and sessions.
5. Cost caps are checked before spawning a turn.
6. Pausing blocks new starts and loops, not in-flight turns.
7. Task completion/cancellation clears the owner's `current_task_id`.
8. Broadcast read state is per recipient.
9. WebDAV failure must not make local tool writes fail unless the operation is
   explicitly part of project switching.
10. Secrets plaintext must not be returned by APIs.
11. `CLAUDE_CONFIG_DIR` should live on persistent `/data`.
12. Coach baseline must not include standard write tools unless the governance
    model is intentionally changed.
13. `/data/projects/<slug>/truth/*` and `/data/projects/<slug>/CLAUDE.md`
    are agent-read-only. The only mutation path is
    `coord_propose_file_write` (Coach-only) followed by an explicit
    human approve in the EnvPane "File-write proposals" section. The
    `_pretool_file_guard_hook` enforces this for all agents and tools.
14. The harness-wide `/data/CLAUDE.md` is human-only — there is no
    proposal scope for it; it cannot be changed by any agent action.

---

## 25. Deferred or Abandoned Ideas

- React/Vite/react-mosaic frontend: replaced by Preact/htm/Split.js.
- Zustand/state store: local hooks are sufficient.
- Docker Compose plus Caddy as primary deployment: replaced by single
  Dockerfile/Zeabur-style deploy.
- Tailscale-only exposure as default: optional operational choice, not baked
  into app.
- Direct WebDAV JSON state as source of truth: replaced by SQLite hot state.
- Lock tools as primary concurrency control: worktrees are the main isolation.
- `/data/context` root and `coord_write_context`: dropped — context
  lives in `/data/CLAUDE.md` (human-only, edited via Files pane) +
  `/data/projects/<active>/CLAUDE.md` (agent-read-only; Coach
  proposes via `coord_propose_file_write(scope='project_claude_md',
  …)` and the human approves with diff review).
- Multiple layout presets and command palette: not implemented.
- PWA push notifications: not implemented.
- Record/replay tooling for events: event log supports it, UI tooling does not.

---

## 26. Operator Summary

For a normal deployment:

1. Build/run the Dockerfile with a persistent `/data` volume.
2. Set `HARNESS_TOKEN` before exposing the app publicly.
3. Set `CLAUDE_CONFIG_DIR=/data/claude`.
4. Authenticate Claude through `/api/auth/claude` paste flow or shell
   `claude /login`.
5. Optionally configure WebDAV with `HARNESS_WEBDAV_*`.
6. Create projects from the left rail or Options drawer.
7. Configure repo URLs either in a project card or legacy Project repo section,
   remembering worktree isolation is still global `/workspaces`.
8. Use Coach as the main entry point; intervene in any pane when needed.
9. Use Files pane to edit the harness-wide `/data/CLAUDE.md`, wiki entries,
   knowledge, and project working files. Project CLAUDE.md and `truth/*`
   are agent-read-only — Coach proposes via `coord_propose_file_write`
   and you review/approve in the EnvPane "File-write proposals" section.
10. Watch health, context, spend, pending interactions, and kDrive failures in
    Settings/Env panes.

End of spec.

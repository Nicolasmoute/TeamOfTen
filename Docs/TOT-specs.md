# TeamOfTen - Full Specs

Current integrated specification for the TeamOfTen harness.

This document folds the original harness spec, the Projects refactor, and the
current implementation into one reference. It is intentionally implementation-
aware: when the code has moved ahead of an older design note, this file follows
the code. When the code still has a hybrid or inconsistent area, that is called
out explicitly.

Companion references:

- `Docs/HARNESS_SPEC.md`: original design reference.
- `Docs/PROJECTS_SPEC.md`: project-refactor design and phase notes.
- `CLAUDE.md`: working notes and constraints for agents editing this repo.
- `README.md`: operator-facing overview and quick start.

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
    HARNESS_SPEC.md
    PROJECTS_SPEC.md
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
    migrations/
      projects_v1.py
      projects_v2.py
    static/
      index.html
      app.js
      style.css
      tools.js
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
- `server/db.py`: SQLite schema, migrations, DB helpers, active-project
  resolution.
- `server/projects_api.py`: project CRUD, switch preview, project activation,
  per-project role view, per-project repo provision endpoint.
- `server/paths.py`: canonical `/data` global/project filesystem layout,
  bootstrap resources, wiki index builder.
- `server/project_sync.py`: active-project and global WebDAV file sync.
- `server/events.py`: in-process event bus plus batched SQLite event writer.
- `server/static/app.js`: no-build Preact SPA.

---

## 3. Tech Stack

| Layer | Current choice |
| --- | --- |
| Agent runtime | Claude Agent SDK and Claude Code CLI |
| Backend | FastAPI, asyncio, WebSocket |
| Database | SQLite via `aiosqlite`, DELETE journal mode |
| Frontend | Preact 10, htm, Split.js, vendored markdown/highlight/diff libs |
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
  locked INTEGER NOT NULL DEFAULT 0
);
```

Seed rows:

- `coach`, kind `coach`, workspace `/workspaces/coach`
- `p1` to `p10`, kind `player`, workspace `/workspaces/pN`

`name`, `role`, `brief`, `session_id`, `continuity_note`, and
`last_exchange_json` used to live here and were moved out by `projects_v1`.

### 6.3 `agent_project_roles`

Per-project identity and prompt addendum.

```sql
CREATE TABLE agent_project_roles (
  slot TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT,
  role TEXT,
  brief TEXT,
  PRIMARY KEY (slot, project_id)
);
```

Notes:

- Coach identity is seeded for `misc`: `Coach`, `Team captain`.
- Players are auto-named on first spawn if no name exists.
- Brief max from API: 8000 chars.
- Name/role can be written by Coach (`coord_set_player_role`) or human
  (`PUT /api/agents/{id}/identity`).

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
  PRIMARY KEY (slot, project_id)
);
```

Session state:

- `session_id`: latest Claude SDK session id for resume.
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
  cache_creation_tokens INTEGER
);
```

Indexes:

- `idx_turns_agent`
- `idx_turns_ended_at`
- `idx_turns_project`

Rows are inserted for completed SDK result messages. Turns that crash before a
result are represented in events, not here.

Cost caps are currently based on this table through `_today_spend()`.

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

- `schema_version`: currently `projects_v2` after migrations.
- `active_project_id`: current project slug.
- `extra_tools`: JSON array, team-wide SDK extras such as `WebSearch`.
- `coach_default_model`: JSON string.
- `players_default_model`: JSON string.
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

---

## 7. Migrations

Migrations run from `init_db()`.

### 7.1 `projects_v1`

Destructive migration from the original single-project schema:

- Drops/recreates `tasks`, `events`, `messages`, `memory_docs`, `turns`.
- Adds `project_id` to project-scoped domain tables.
- Drops or ignores legacy `agents` columns:
  - `session_id`
  - `continuity_note`
  - `last_exchange_json`
  - `name`
  - `role`
  - `brief`
- Creates `projects`, `agent_sessions`, `agent_project_roles`, `sync_state`.
- Inserts `misc` project.
- Sets `team_config.active_project_id = misc`.
- Seeds misc Coach role.
- Copies `HARNESS_PROJECT_REPO` into `projects.repo_url` for misc if set.
- Wipes legacy flat local dirs.
- Best-effort wipes WebDAV root.
- Scaffolds global and misc trees.
- Stamps `schema_version = projects_v1`.

### 7.2 `projects_v2`

Layout cleanup migration:

- Wipes legacy flat root dirs:
  - `handoffs`
  - `context`
  - `knowledge`
  - `uploads`
  - `memory`
  - `decisions`
  - `outputs`
  - `attachments`
  - typo orphans `output`, `upload`, `uplods`
- Moves `/data/skills/` to `/data/.claude/skills/`.
- Renames per-project `inputs/` to `uploads/`.
- Moves per-project `knowledge/` to `working/knowledge/`.
- Moves per-project `memory/` to `working/memory/`.
- Stamps `schema_version = projects_v2`.

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

Actual current caveats:

- Knowledge writes use `working/knowledge/`.
- Memory DB mirror path in `coord_update_memory` currently writes to
  `projects/<slug>/memory/<topic>.md` on WebDAV, while canonical v2 local
  memory path is `working/memory/`. That WebDAV path should be reconciled.
- Outputs module still defaults to global `/data/outputs`, not
  `/data/projects/<slug>/outputs`.
- Workspaces still use `/workspaces`, not the per-project `repo/` tree.

### 8.3 Project CLAUDE.md Stub

Created on project creation if missing:

```markdown
# Project: <name>

## Goal
<description or placeholder>

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
```

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

- Only `.md` and `.txt`.
- Max body 100,000 chars.
- Plain disk write.
- WebDAV mirroring happens later through project/global sync loops.
- `file_written` event emitted by API.
- Wiki writes trigger `update_wiki_index()` unless writing `INDEX.md` itself.

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

Prompt layers:

1. Per-agent identity block from `agent_project_roles`.
2. Coach-only coordination block from current project/team/tasks/inbox/wiki.
3. Baseline Coach or Player role prompt.
4. Global rules from `/data/CLAUDE.md`.
5. Active project rules from `/data/projects/<slug>/CLAUDE.md`.
6. Per-agent `brief` from `agent_project_roles`.
7. Continuity handoff after `/compact`, when present.

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

- Active project name/goal.
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

Recent exchange preservation:

- `last_exchange_json` stores a bounded rolling log.
- Budget: `HARNESS_HANDOFF_TOKEN_BUDGET`, default 20,000 tokens.
- Full session transcript remains in Claude CLI JSONL until session retention
  trims it.

### 10.4 Context Usage UI

`GET /api/agents/{id}/context` returns:

- `session_id`
- estimated used tokens
- context window
- model
- ratio

The pane renders this as a compact context bar.

---

## 11. Agent Runtime

`run_agent(agent_id, prompt, model=None, plan_mode=False, effort=None, ...)`
is the central execution path.

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

### 11.3 Coach Loops

Routine tick loop:

- Initial env: `HARNESS_COACH_TICK_INTERVAL`, default 0.
- Runtime API:
  - `GET /api/coach/loop`
  - `POST /api/coach/loop {interval_seconds}`
- UI slash:
  - `/loop`
  - `/loop 60`
  - `/loop off`
- Prompt: "Routine tick. Read your inbox..."
- Skips when paused or Coach is already working.
- Emits `coach_tick_fired` and `coach_loop_changed`.

Repeat loop:

- Independent from routine tick.
- Runtime API:
  - `GET /api/coach/repeat`
  - `POST /api/coach/repeat {interval_seconds, prompt}`
- UI slash:
  - `/repeat`
  - `/repeat 120 <prompt>`
  - `/repeat off`
- Coach-only in UI.
- Emits `coach_repeat_fired` and `coach_repeat_changed`.

Manual tick:

- `POST /api/coach/tick`
- UI slash `/tick`
- Rejects with 409 if Coach is working.

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

### 12.10 Removed Tool

`coord_write_context` was removed with `projects_v2`.

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

External MCP servers:

- Loaded from `HARNESS_MCP_CONFIG`.
- Loaded from `mcp_servers` DB table.
- DB wins on name collision.
- Explicit `allowed_tools` list is required; no automatic tool exposure.
- Tool names become `mcp__<server>__<tool>`.

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
- External MCP config parse/status.
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
| `GET /api/agents` | Active-project identity/session joined with global roster |
| `POST /api/agents/start` | Start one turn |
| `POST /api/agents/{id}/cancel` | Cancel one turn |
| `POST /api/agents/cancel-all` | Cancel all running turns |
| `PUT /api/agents/{id}/identity` | Human write name/role for active project |
| `PUT /api/agents/{id}/brief` | Human write active-project brief |
| `PUT /api/agents/{id}/locked` | Set lock flag |
| `GET /api/agents/{id}/context` | Context usage estimate |
| `DELETE /api/agents/{id}/session` | Clear active-project session |
| `POST /api/agents/{id}/compact` | Queue compact turn |
| `POST /api/agents/sessions/clear` | Batch clear active-project sessions |

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
| `GET /api/coach/loop` | Routine loop interval |
| `POST /api/coach/loop` | Set routine loop interval |
| `GET /api/coach/repeat` | Repeat loop state |
| `POST /api/coach/repeat` | Set repeat loop |
| `POST /api/coach/tick` | Manual Coach tick |

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

### 14.8 Files

| Endpoint | Notes |
| --- | --- |
| `GET /api/files/roots` | Two roots: global and project |
| `GET /api/files/tree/{root}` | Recursive tree |
| `GET /api/files/read/{root}?path=` | Read text |
| `PUT /api/files/write/{root}?path=` | Write `.md`/`.txt` |

### 14.9 Events and Turns

| Endpoint | Notes |
| --- | --- |
| `GET /api/events` | Active-project event history with filters |
| `GET /api/turns` | Active-project turn rows |
| `GET /api/turns/summary?hours=24` | Per-agent spend/turn aggregate |

`GET /api/events` supports:

- `agent`
- `type`
- `since_id`
- `before_id`
- `limit` max 1000

Events are returned oldest-to-newest within the page.

### 14.10 Attachments

| Endpoint | Notes |
| --- | --- |
| `POST /api/attachments` | Upload pasted image to active project |
| `GET /api/attachments/{filename}` | Serve active-project image |

Allowed extensions:

- `png`
- `jpg`
- `jpeg`
- `gif`
- `webp`

Storage:

- If `HARNESS_ATTACHMENTS_DIR` is set, use that legacy global dir.
- Otherwise `/data/projects/<active>/attachments/`.

Current caveat:

- Frontend prompt text references attachments as
  `/workspaces/<slot>/attachments/<filename>`.
- Dockerfile creates symlinks to `/data/attachments`, but active-project
  attachments now default to `/data/projects/<active>/attachments`.
- Unless `HARNESS_ATTACHMENTS_DIR=/data/attachments` is set, the workspace
  symlink path may not point at the active project's uploaded file.

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
| `GET /api/team/models` | Per-role default models |
| `PUT /api/team/models` | Set per-role defaults |
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

Suggested defaults:

- Coach: `claude-opus-4-7`
- Players: `claude-sonnet-4-6`

### 14.13 MCP and Secrets

| Endpoint | Notes |
| --- | --- |
| `GET /api/mcp/servers` | List DB MCP servers, redacted |
| `POST /api/mcp/servers` | Save one or more server configs from paste |
| `PATCH /api/mcp/servers/{name}` | Toggle enabled/tools |
| `DELETE /api/mcp/servers/{name}` | Delete DB MCP server |
| `POST /api/mcp/servers/{name}/test` | Smoke-test command/url |
| `GET /api/secrets` | List secret metadata and store status |
| `PUT /api/secrets/{name}` | Upsert encrypted secret |
| `DELETE /api/secrets/{name}` | Delete secret |

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
- `session_cleared`
- `session_resume_failed`
- `session_compact_requested`
- `session_compacted`
- `auto_compact_triggered`
- `auto_compact_failed`
- `compact_empty_forced`
- `context_applied`
- `context_usage`

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
- `brief_updated`
- `lock_updated`
- `human_attention`

Loops and runtime:

- `pause_toggled`
- `coach_loop_changed`
- `coach_tick_fired`
- `coach_repeat_changed`
- `coach_repeat_fired`
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

File/browser:

- `file_written`

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
- Renders markdown safely with DOMPurify.
- Shows transient streaming text/thinking when token streaming is enabled.

Input:

- Textarea.
- Image paste/upload strip.
- Mode chips for model, plan, effort, context.
- Slash command autocomplete.
- Prompt history.
- Ctrl/Cmd+Enter sends.
- Ctrl/Cmd+Up/Down cycles prompt history.
- Escape clears slash menu.

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
- Textarea editor for `.md`/`.txt`.
- Dirty indicator.
- Ctrl/Cmd+S save.
- Read-only protections from backend path/extension validation.
- Reloads on filesystem events and project switches.

### 16.6 Environment Pane

Shows:

- Human attention banner.
- Pending questions/plans.
- kDrive sync failure banner.
- Tasks with filters.
- Cost/spend summaries.
- Inbox/recent messages.
- Memory list/content.
- Decisions list/content.
- Timeline of important events.

It scopes project-sensitive sections to the active project through the API.

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

---

## 17. Git Workspaces

Configured by:

```text
HARNESS_PROJECT_REPO
HARNESS_PROJECT_BRANCH
```

or DB:

- `team_config.project_repo`
- `team_config.project_branch`

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

File config path:

```text
HARNESS_MCP_CONFIG=/data/mcp-servers.json
```

Example shape:

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

---

## 20. Environment Variables

Representative env vars from `.env.example` and implementation:

| Variable | Default | Purpose |
| --- | --- | --- |
| `HARNESS_TOKEN` | unset | Optional API/WS bearer token |
| `CLAUDE_CONFIG_DIR` | `/data/claude` | Claude OAuth/session dir |
| `HARNESS_DB_PATH` | `/data/harness.db` | SQLite path |
| `HARNESS_DATA_ROOT` | `/data` | Global/project data root |
| `HARNESS_PROJECT_REPO` | unset | Legacy/global git repo URL |
| `HARNESS_PROJECT_BRANCH` | `main` | Legacy/global base branch |
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
| `HARNESS_COACH_TICK_INTERVAL` | `0` | Routine Coach loop startup interval |
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
| `HARNESS_MCP_CONFIG` | unset/example `/data/mcp-servers.json` | MCP file config |
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
| `PORT` | `8000` | Uvicorn port |

Legacy vars still present in `.env.example` but mostly retired by
`projects_v2`:

- `HARNESS_CONTEXT_DIR`
- `HARNESS_KNOWLEDGE_DIR`
- `HARNESS_DECISIONS_DIR`
- `HARNESS_UPLOADS_DIR`
- `HARNESS_HANDOFFS_DIR`
- `HARNESS_WORKSPACES_DIR` (implementation uses `HARNESS_WORKSPACES_ROOT`)

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

As of the audit, the repo contains 21 test files and 182 test functions.

Test areas include:

- DB init/schema/migrations.
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
- Phase 7 project prompt/wiki behavior.

Run locally:

```bash
uv sync --extra dev
uv run pytest -ra --strict-markers
```

CI runs `.github/workflows/tests.yml` on push and PR.

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

6. `.env.example` still lists several pre-projects flat-dir vars.

7. Coach cannot edit CLAUDE.md through `Write` because Coach baseline excludes
   write tools, even though comments mention Coach editing global/project
   CLAUDE.md via standard Write. Human file editor is the reliable path today.

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
- `/data/context` root and `coord_write_context`: removed by `projects_v2`.
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
9. Use Files pane for global/project CLAUDE.md, wiki, knowledge, decisions, and
   project working files.
10. Watch health, context, spend, pending interactions, and kDrive failures in
    Settings/Env panes.

End of spec.

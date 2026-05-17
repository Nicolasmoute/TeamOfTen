---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 6: SQLite Data Model'
section: 6
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
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

Seed rows: `coach` (kind `coach`) and `p1`..`p10` (kind `player`).
The `workspace_path` column is legacy from before the per-project
repo layout (§4.6) and is not consulted at runtime — agent cwd is
resolved by `workspace_dir(slot)` against the active project.

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
  thinking_override INTEGER,     -- 0/1 → off/on (Claude runtime only)
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
- `thinking_override` (0/1) is Coach-set via
  `coord_set_player_thinking`. NULL when unset. Same precedence as
  `effort_override` / `plan_mode_override` (per-pane request → this
  column → off), but **no role default** — thinking stays off unless
  explicitly set on at least one of the two layers. Claude runtime
  only: when true, the runtime injects
  `thinking={"type":"enabled","budget_tokens":N}` into
  `ClaudeAgentOptions` (N from `HARNESS_THINKING_BUDGET_TOKENS`,
  default 8000, clamped ≥ 1024). Codex Players store the value but
  silently ignore it at spawn time — Codex has its own reasoning
  knob; the override survives a runtime flip so a Codex→Claude
  return picks it up automatically. The middle rung of the Coach
  bump ladder: `coord_set_player_effort` → `coord_set_player_thinking`
  (Claude only) → `coord_set_player_model`.

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
When a Player is hard-assigned to execute, the harness writes that task
to `agents.current_task_id` if the slot is free or if the existing
pointer is stale (missing/archived task). `coord_my_assignments` applies
the same defensive read: an archived pointer cannot hide a live active
executor role, and the tool self-heals the slot back to the executor
task/tool surface.

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

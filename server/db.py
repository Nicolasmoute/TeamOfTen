from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import aiosqlite

# Route logging to stdout so Zeabur's log collector picks it up even when
# startup hangs — the M2a deploy showed "no logs at all" when init_db
# silently blocked on a volume-filesystem incompatibility.
logger = logging.getLogger("harness.db")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Default path matches Zeabur's volume convention: mount a volume with the
# UI's "Mount Directory" set to "/data", and this is where the DB lives.
# Override with HARNESS_DB_PATH env var for other deploy targets.
# Known gotcha (learned the hard way in M2a): do NOT pre-create the mount
# path in the Dockerfile. On Zeabur, bind-mounting a volume over an
# already-existing directory causes SQLite's file probe to hang silently.
DB_PATH = os.environ.get("HARNESS_DB_PATH", "/data/harness.db")

# Schema is idempotent — safe to run every startup.
SCHEMA = """
-- Projects backbone — declared first so the FK references on the
-- domain tables below resolve at create time. SQLite is lazy on FK
-- target validation but reordering keeps the dependency graph obvious.
CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    repo_url     TEXT,
    description  TEXT,
    archived     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agents (
    id                    TEXT PRIMARY KEY,
    kind                  TEXT NOT NULL CHECK (kind IN ('coach', 'player')),
    status                TEXT NOT NULL DEFAULT 'stopped'
                          CHECK (status IN ('stopped', 'idle', 'working', 'waiting', 'error')),
    current_task_id       TEXT,
    model                 TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
    workspace_path        TEXT NOT NULL,
    cost_estimate_usd     REAL NOT NULL DEFAULT 0.0,
    started_at            TEXT,
    last_heartbeat        TEXT,
    allowed_extra_tools   TEXT,
    -- JSON array of SDK-facing tool names for the slot's current
    -- kanban role. NULL falls back to role defaults in the dispatcher.
    allowed_tools         TEXT,
    locked                INTEGER NOT NULL DEFAULT 0,
    -- Slot-level runtime preference. Nullable so role defaults can
    -- apply (resolution: agents.runtime_override → team_config role
    -- default → 'claude'). NOT NULL with a default would silently
    -- ignore role defaults. See Docs/CODEX_RUNTIME_SPEC.md §B.1.
    runtime_override      TEXT
                          CHECK (runtime_override IS NULL
                                 OR runtime_override IN ('claude','codex')),
    -- Idle-poller debounce timestamp. NULL = never woken by the poller.
    -- The poller skips a Player whose last_idle_wake_at is within
    -- HARNESS_IDLE_POLL_DEBOUNCE_SECONDS of now (default 30 min) so a
    -- Player who declined a wake isn't pestered every cycle. See
    -- Docs/kanban-specs-v2.md §10.
    last_idle_wake_at     TEXT
);

-- Kanban-shaped task lifecycle (Docs/kanban-specs-v2.md). Status enum is
-- the kanban stage (`plan` / `execute` / `audit_syntax` / `audit_semantics`
-- / `ship` / `archive`). The legacy enum (open/claimed/in_progress/
-- blocked/done/cancelled) was migrated to this shape via a one-shot
-- table rebuild in `_rebuild_tasks_if_kanban_outdated` — see that
-- function for the mapping. `blocked` is now an orthogonal flag, not a
-- status value, so a task can be "blocked while in audit_syntax"
-- without losing its workflow position.
CREATE TABLE IF NOT EXISTS tasks (
    id                          TEXT PRIMARY KEY,
    project_id                  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title                       TEXT NOT NULL,
    description                 TEXT NOT NULL DEFAULT '',
    status                      TEXT NOT NULL DEFAULT 'plan'
                                CHECK (status IN ('plan', 'execute', 'audit_syntax', 'audit_semantics', 'ship', 'archive')),
    owner                       TEXT REFERENCES agents(id),
    created_by                  TEXT NOT NULL,
    created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    claimed_at                  TEXT,
    -- First time the executor's turn actually fired after assign/claim.
    -- NULL when a task is hard-assigned but the auto-wake hasn't run
    -- (cost-cap miss / harness paused) — distinguishes "assigned, not
    -- started" from "actively working" within the execute stage.
    started_at                  TEXT,
    completed_at                TEXT,
    -- Set on entry to `archive` for any reason (delivery OR cancel).
    -- Indexed DESC for the archive view's newest-first list.
    archived_at                 TEXT,
    -- Set when status moves to `archive` because a human cancelled.
    -- Distinguishes cancelled-archive from delivered-archive in the
    -- archive view's "show cancelled" toggle.
    cancelled_at                TEXT,
    parent_id                   TEXT REFERENCES tasks(id),
    priority                    TEXT NOT NULL DEFAULT 'normal'
                                CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    -- Trajectory drives all routing in v0.3. JSON list of {stage, to}
    -- objects; ordered list of stages this task will traverse, with the
    -- per-stage candidate pool. Replaces v0.2's complexity / required_reviews
    -- / ship_required triple. Validation lives in tools.py:_validate_trajectory.
    trajectory                  TEXT NOT NULL DEFAULT '[{"stage":"execute","to":[]}]',
    -- Stamped by the kanban subscriber on every status transition. Drives
    -- the stall sweeper in idle_poller.py.
    last_stage_change_at        TEXT,
    -- Stamped by the stall sweeper when it fires task_stage_stale.
    -- Suppresses re-alerts until the task progresses or 24h escalation.
    stale_alert_at              TEXT,
    -- Workflow metadata drives prompt wording (code/research/writing/
    -- marketing/ops/generic). Does NOT drive routing — the trajectory does.
    workflow                    TEXT NOT NULL DEFAULT 'generic',
    -- Optional informational tag; no longer required, no longer enum-validated
    -- (the v0.2 admission gate is removed in v0.3 — every Coach delegation
    -- goes through kanban).
    tracking_reason             TEXT,
    -- Orthogonal blocked flag. `blocked_reason` is a short note for
    -- the card. Toggleable via coord_set_task_blocked.
    blocked                     INTEGER NOT NULL DEFAULT 0,
    blocked_reason              TEXT,
    -- Spec markdown path, written by Coach (or delegated planner) before
    -- the task can transition plan→execute (standard tasks only). Mirrors
    -- to the cloud drive at the same relative path. Required gate; see
    -- `_assert_spec_present` in tools.py.
    spec_path                   TEXT,
    spec_written_at             TEXT,
    -- Latest Player auditor report (the gating audit). Format:
    -- `projects/<id>/working/tasks/<task_id>/audits/audit_<round>_<kind>.md`.
    -- Older rounds stay on disk; the card surfaces only the latest.
    latest_audit_report_path    TEXT,
    latest_audit_kind           TEXT,
    latest_audit_verdict        TEXT,
    -- Compass auto-audit's parallel report (informational, not a gate).
    -- Format: `projects/<id>/working/compass/audit_reports/<audit_id>.md`.
    compass_audit_report_path   TEXT,
    compass_audit_verdict       TEXT,
    tags                        TEXT NOT NULL DEFAULT '[]',
    artifacts                   TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_tasks_status     ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_owner      ON tasks(owner);
CREATE INDEX IF NOT EXISTS idx_tasks_parent     ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project    ON tasks(project_id);

-- Backlog (Docs/kanban-specs-v2.md §4.0): pre-plan holding area for task
-- ideas. Any agent or human can propose; Coach triages via coord_triage_backlog.
CREATE TABLE IF NOT EXISTS backlog_tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    title               TEXT NOT NULL,
    proposed_by         TEXT NOT NULL,
    proposed_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'promoted', 'rejected')),
    reject_reason       TEXT,
    promoted_task_id    TEXT REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_backlog_status ON backlog_tasks(status, proposed_at);
-- Note: indexes referencing kanban-new columns (`archived_at`,
-- `last_stage_change_at`) live in `_ensure_tasks_kanban_indexes`, called
-- from init_db AFTER the migrations run. SQLite validates column
-- existence at CREATE INDEX time, so an upgraded DB whose tasks table
-- still has the legacy schema would crash here on every boot.

-- Task role assignments (Docs/kanban-specs-v2.md §4). A task has multiple
-- Players involved in different roles across stages: planner (optional —
-- Coach by default), executor, formal reviewer, semantic reviewer, shipper.
-- Each role can be hard-assigned to one Player or posted to a pool of
-- eligible Players (`eligible_owners` JSON array) where the first to
-- claim wins. Multiple rows per (task, role) accumulate over time — an
-- audit-fail loop produces a fresh auditor row each round; the active
-- one is `superseded_by IS NULL` AND most-recent `assigned_at`.
CREATE TABLE IF NOT EXISTS task_role_assignments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES tasks(id),
    role            TEXT NOT NULL CHECK(role IN
                      ('planner','executor','auditor_syntax','auditor_semantics','shipper')),
    -- JSON array of slot ids. Empty `[]` = "this row is hard-assigned
    -- (owner must be set at insert time)". Non-empty = posted to a pool;
    -- owner is NULL until a Player claims via coord_claim_task.
    eligible_owners TEXT NOT NULL DEFAULT '[]',
    owner           TEXT REFERENCES agents(id),
    assigned_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    claimed_at      TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    -- Auditor roles only: relative path to the audit_<round>_<kind>.md
    -- this auditor produced. NULL until coord_submit_audit_report fires.
    report_path     TEXT,
    -- Auditor roles only: 'pass' | 'fail'. NULL until submitted.
    verdict         TEXT CHECK(verdict IS NULL OR verdict IN ('pass','fail')),
    -- Self-reference: when a fail verdict creates a fresh auditor row
    -- on the next round, the previous one points forward via this column.
    -- `WHERE superseded_by IS NULL` filters to active rows.
    superseded_by   INTEGER REFERENCES task_role_assignments(id),
    -- Auditor roles only (NULL on planner/executor/shipper rows).
    -- Free-text Coach-set focus naming what the auditor should check
    -- (math invariants? brand voice? race conditions?). REQUIRED for
    -- auditor_semantics rows; defaults applied at wake-prompt time
    -- when NULL on auditor_syntax rows. See kanban-specs-v2.md §4.6.
    focus           TEXT
);

CREATE INDEX IF NOT EXISTS idx_role_assignments_task   ON task_role_assignments(task_id);
CREATE INDEX IF NOT EXISTS idx_role_assignments_owner  ON task_role_assignments(owner);
CREATE INDEX IF NOT EXISTS idx_role_assignments_role   ON task_role_assignments(task_id, role);
CREATE INDEX IF NOT EXISTS idx_role_assignments_active ON task_role_assignments(task_id, role, superseded_by, assigned_at);

-- payload_to / payload_owner: virtual generated columns over the
-- two JSON fields the pane-history fan-out filter cares about. Lets
-- us index them — the prior query used json_extract() in the WHERE
-- clause, which is unindexable and forced a full scan of the events
-- table on every pane open. Virtual columns aren't stored in the
-- row, only in the index, so disk overhead is just the index size.
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,
    payload         TEXT NOT NULL,                   -- JSON string
    payload_to      TEXT GENERATED ALWAYS AS (json_extract(payload, '$.to')) VIRTUAL,
    payload_owner   TEXT GENERATED ALWAYS AS (json_extract(payload, '$.owner')) VIRTUAL
);

CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id, id);
CREATE INDEX IF NOT EXISTS idx_events_type  ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id);
-- (agent_id, type) for fan-out queries that decompose into "events
-- where I am the actor of any type, OR events of a specific type
-- targeting me". The OR-branch with type='message_sent' / 'task_*'
-- benefits from filtering on type first, then narrowing within. With
-- only the (agent_id, id) index the planner can't use it for the
-- type-specific OR branches and falls back to a scan.
CREATE INDEX IF NOT EXISTS idx_events_agent_type ON events(agent_id, type);
-- (type, id) for type-only queries with id-ordered pagination, e.g.
-- /api/events?type=human_attention&since_id=N — the existing
-- idx_events_type covers WHERE type=? but not the ORDER BY id DESC
-- LIMIT N step, forcing a temp-table sort.
CREATE INDEX IF NOT EXISTS idx_events_type_id ON events(type, id);
-- Indexes over the virtual generated columns for pane-history
-- fan-out filtering ("events targeting me by type", "events I own
-- by type"). They sit alongside payload_to / payload_owner above.
CREATE INDEX IF NOT EXISTS idx_events_to    ON events(type, payload_to, id);
CREATE INDEX IF NOT EXISTS idx_events_owner ON events(type, payload_owner, id);
-- Project-prefixed variants for /api/events. The endpoint always
-- starts WHERE with `project_id = ?` so a project-leading index lets
-- the planner narrow the scan before applying the OR-fanout. Without
-- these the existing (type, payload_to, id) etc. force the planner
-- to read across every project's tail of matching rows. The (project_id,
-- agent_id, id) index covers the actor-of-any-type branch + the id
-- ORDER BY tail in one structure.
CREATE INDEX IF NOT EXISTS idx_events_project_agent ON events(project_id, agent_id, id);
CREATE INDEX IF NOT EXISTS idx_events_project_to    ON events(project_id, type, payload_to, id);
CREATE INDEX IF NOT EXISTS idx_events_project_owner ON events(project_id, type, payload_owner, id);
-- Time-window scans by ts (sync.flush_day, retention DELETE). Without
-- this every flush + every nightly trim full-scans the events table.
CREATE INDEX IF NOT EXISTS idx_events_ts_id ON events(ts, id);

-- Per-project event log (Docs/kanban-specs-v2.md §9). Coach reads the
-- unread tail on every tick via the `## Recent events` prompt block.
-- Sibling write to the existing events table — every v2-mappable bus
-- event produces exactly one row here (see server/project_events.py),
-- with payload_pointer pre-extracted (sha / spec_path / report_path /
-- message body / etc.) so render-time doesn't have to JSON-parse.
-- read_by_coach_at: NULL = unread; stamped after Coach's tick reads
-- the row (the prompt builder collects surfaced ids and the post-turn
-- handler updates them once ResultMessage lands). Older unread rows
-- (beyond HARNESS_PROJECT_EVENTS_PER_TICK, default 50) roll forward
-- to subsequent ticks — see §9.3.
CREATE TABLE IF NOT EXISTS project_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ts               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    actor            TEXT NOT NULL,    -- 'p1'..'p10' / 'coach' / 'compass' / 'system' / 'human'
    type             TEXT NOT NULL,    -- see §9.2 enum
    task_id          TEXT,             -- nullable; some events aren't task-scoped
    payload_json     TEXT NOT NULL DEFAULT '{}',
    payload_pointer  TEXT,             -- relative path to artifact OR short text body
    read_by_coach_at TEXT              -- NULL = unread; stamped post-tick
);

CREATE INDEX IF NOT EXISTS idx_project_events_project_unread
    ON project_events(project_id, read_by_coach_at, ts);
CREATE INDEX IF NOT EXISTS idx_project_events_task   ON project_events(task_id, ts);
CREATE INDEX IF NOT EXISTS idx_project_events_actor  ON project_events(actor, ts);
-- Playbook reflection runner scans by (type, ts) for the recent
-- evidence window. Without this index every reflection cycle
-- full-scans project_events.
CREATE INDEX IF NOT EXISTS idx_project_events_type_ts
    ON project_events(type, ts);

-- Validation instrumentation for kanban v2 (Docs/kanban-specs-v2.md §22.1).
-- A row is inserted when Coach's coord_approve_stage note flags a
-- deviation, OR an audit submits with verdict='fail', OR the human
-- flags via POST /api/tasks/{id}/flag_deviation. The off_spec_completion_count
-- Player-health counter (§11.1) reads from this table.
-- noticed_at:
--   'push'  — Coach noticed the deviation while task was still in execute
--             (before any audit role row completed for the current round).
--   'audit' — surfaced via an audit FAIL.
--   'human' — flagged manually via the kanban UI.
CREATE TABLE IF NOT EXISTS deviations_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    task_id         TEXT NOT NULL,
    executor        TEXT NOT NULL,        -- the slot that did the work
    noticed_at      TEXT NOT NULL CHECK(noticed_at IN ('push', 'audit', 'human')),
    description     TEXT,                  -- short reason — Coach's note, audit body summary, or human flag
    source_event_id INTEGER                -- pointer to project_events row that triggered this
);

CREATE INDEX IF NOT EXISTS idx_deviations_log_project_executor
    ON deviations_log(project_id, executor, ts);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    from_id      TEXT NOT NULL,          -- 'human', 'coach', 'p1'..'p10'
    to_id        TEXT NOT NULL,          -- agent id or 'broadcast'
    subject      TEXT,
    body         TEXT NOT NULL,
    sent_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    read_at      TEXT,                    -- legacy, unused after v0.4.1
    in_reply_to  INTEGER REFERENCES messages(id),
    priority     TEXT NOT NULL DEFAULT 'normal'
                 CHECK (priority IN ('normal', 'interrupt'))
);

CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_id);
CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_id);
CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project_id);

-- Per-recipient read tracking. Necessary for broadcasts: the first
-- recipient to drain must NOT mark the message read for everyone else.
CREATE TABLE IF NOT EXISTS message_reads (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    agent_id   TEXT NOT NULL,
    read_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (message_id, agent_id)
);

CREATE INDEX IF NOT EXISTS idx_msgreads_agent ON message_reads(agent_id);

-- Shared scratchpad. Overwrite-on-update; event log is the history.
CREATE TABLE IF NOT EXISTS memory_docs (
    project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    topic            TEXT NOT NULL,
    content          TEXT NOT NULL,
    last_updated     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_updated_by  TEXT NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, topic)
);

CREATE INDEX IF NOT EXISTS idx_memory_project ON memory_docs(project_id);

-- Per-turn analytics ledger. One row per SDK ResultMessage — cheap
-- indexed queries for 'how much did p3 spend this week'. Parallel to
-- the events table but narrower: just the numbers, no free text. The
-- events table still has the full turn trail for audit; this is for
-- charts.
-- Per-turn analytics ledger. Token columns track billing usage from
-- ResultMessage.usage on every successful turn. Context pressure is
-- estimated separately from the latest per-assistant usage row in
-- Claude Code's session jsonl because ResultMessage.usage aggregates
-- every tool round in the turn.
CREATE TABLE IF NOT EXISTS turns (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id              TEXT NOT NULL,
    project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    started_at            TEXT NOT NULL,
    ended_at              TEXT NOT NULL,
    duration_ms           INTEGER,
    cost_usd              REAL,
    session_id            TEXT,
    num_turns             INTEGER,     -- SDK's own internal turn counter (tool roundtrips)
    stop_reason           TEXT,
    is_error              INTEGER NOT NULL DEFAULT 0,
    model                 TEXT,
    plan_mode             INTEGER NOT NULL DEFAULT 0,
    effort                INTEGER,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    -- Which runtime executed this turn. No CHECK constraint so future
    -- runtimes don't require a schema migration to record turns.
    runtime               TEXT NOT NULL DEFAULT 'claude',
    -- 'token_priced' (cost_usd populated from a pricing table) or
    -- 'plan_included' (ChatGPT-auth Codex; cost_usd = 0, tokens
    -- populated for visibility). NULL on legacy rows. See
    -- Docs/CODEX_RUNTIME_SPEC.md §G.
    cost_basis            TEXT
);

CREATE INDEX IF NOT EXISTS idx_turns_agent    ON turns(agent_id, id);
CREATE INDEX IF NOT EXISTS idx_turns_ended_at ON turns(ended_at);
CREATE INDEX IF NOT EXISTS idx_turns_project  ON turns(project_id);

-- Team-wide settings (applies to every agent). Simple key/value store
-- so we can grow without schema churn. Value is a JSON string — the
-- caller decides the shape. Current keys:
--   extra_tools  → JSON array of SDK tool names (WebSearch, WebFetch)
CREATE TABLE IF NOT EXISTS team_config (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- External MCP servers configured via the UI (alongside the existing
-- file-based HARNESS_MCP_CONFIG path — DB entries are loaded AFTER
-- the file so UI edits win on name collision).
--
-- config_json: the single-server object pulled from the user's paste
--   ({ "type": "stdio"|"http", "command": ..., "args": [...], ... }).
-- allowed_tools_json: JSON array of bare tool names (no mcp__<name>__
--   prefix). UI-managed; server can't use tools that aren't in here.
-- enabled: 0/1 — disabled entries are kept but not merged into spawns.
-- last_ok / last_error / last_tested_at: populated by the test
--   endpoint + the periodic health loop.
CREATE TABLE IF NOT EXISTS mcp_servers (
    name             TEXT PRIMARY KEY,
    config_json      TEXT NOT NULL,
    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_ok          INTEGER,
    last_error       TEXT,
    last_tested_at   TEXT
);

-- Encrypted secrets. ciphertext is Fernet-encrypted bytes; the master
-- key lives in env (HARNESS_SECRETS_KEY) and never touches the DB. A
-- lost master key makes this table unrecoverable, which is the point —
-- a stolen DB snapshot without the key is useless.
CREATE TABLE IF NOT EXISTS secrets (
    name             TEXT PRIMARY KEY,
    ciphertext       BLOB NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Projects refactor (PROJECTS_SPEC.md §3). The `projects` table is
-- declared at the top of the schema; the per-(slot, project) tables
-- below are additive.
CREATE TABLE IF NOT EXISTS agent_sessions (
    slot                TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    session_id          TEXT,
    last_active         TEXT,
    continuity_note     TEXT,
    last_exchange_json  TEXT,
    -- Codex thread id for this (slot, project). Separate from
    -- session_id (Claude) because each runtime has its own continuation
    -- state — a single field would force tagging or clear-on-runtime-
    -- change and break the symmetric switch-back case. See
    -- Docs/CODEX_RUNTIME_SPEC.md §B.1.
    codex_thread_id     TEXT,
    PRIMARY KEY (slot, project_id)
);

CREATE TABLE IF NOT EXISTS agent_project_roles (
    slot                TEXT NOT NULL,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name                TEXT,
    role                TEXT,
    brief               TEXT,
    model_override      TEXT,
    -- Coach-set per-(slot, project) effort tier. NULL = no override
    -- (fall through to per-pane request → no effort override). Values:
    -- 1..4 mapped to "low" | "medium" | "high" | "max" by
    -- agents._EFFORT_LEVELS at spawn time. Per-pane request value
    -- (when non-NULL) wins over this column; this column wins over
    -- "no thinking-budget override".
    effort_override     INTEGER,
    -- Coach-set per-(slot, project) plan-mode flag. NULL = no override.
    -- 1 = plan mode on, 0 = plan mode off. Resolution: per-pane
    -- request wins when the kwarg is True or False; this column is
    -- consulted only when the kwarg is None (UI omits it whenever the
    -- pane toggle is off, so "no per-pane override" is the common case).
    plan_mode_override  INTEGER,
    -- Coach-set per-(slot, project) extended-thinking flag. NULL = no
    -- override (off). 1 = thinking on, 0 = explicit off. Claude
    -- runtime only — silently ignored on Codex spawn. Budget comes
    -- from HARNESS_THINKING_BUDGET_TOKENS env at spawn time. No role
    -- default — thinking stays off unless explicitly set.
    thinking_override   INTEGER,
    PRIMARY KEY (slot, project_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
    project_id       TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- 'project': per-project files under /data/projects/<slug>/
    -- 'wiki':    cross-project wiki entries under /data/wiki/
    -- 'global':  shared root (CLAUDE.md, skills/, mcp/, wiki/INDEX.md)
    tree             TEXT NOT NULL CHECK (tree IN ('project', 'wiki', 'global')),
    path             TEXT NOT NULL,
    mtime            REAL NOT NULL,
    size_bytes       INTEGER NOT NULL,
    sha256           TEXT NOT NULL,
    last_synced_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, tree, path)
);

-- File-write proposals — Coach proposes changes to harness-managed
-- files, the human approves/denies, the harness applies the approved
-- write server-side. Players cannot propose; the
-- `coord_propose_file_write` MCP tool is Coach-only. The PreToolUse
-- file-guard hook denies any direct agent Write/Edit/Bash on the
-- protected paths regardless of role, so this table is the only path
-- through which they ever change. Scopes:
--   'truth'              — relative `path` under /data/projects/<slug>/truth/
--   'project_claude_md'  — `path` must be 'CLAUDE.md'; target is
--                          /data/projects/<slug>/CLAUDE.md.
CREATE TABLE IF NOT EXISTS file_write_proposals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    proposer_id       TEXT NOT NULL,                 -- 'coach' (enforced at tool layer)
    scope             TEXT NOT NULL DEFAULT 'truth', -- see header for valid values; validated at tool/resolver layer
    -- scope-relative path (truth: under truth/; project_claude_md: 'CLAUDE.md')
    path              TEXT NOT NULL,
    proposed_content  TEXT NOT NULL,                 -- full new file body
    summary           TEXT NOT NULL,                 -- one-line "why" the user reads
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'approved', 'denied', 'cancelled', 'superseded')),
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at       TEXT,
    resolved_by       TEXT,                          -- 'human' (only legal value today)
    resolved_note     TEXT
);

CREATE INDEX IF NOT EXISTS idx_file_write_proposals_project_status
    ON file_write_proposals(project_id, status);

-- Coach recurrences (recurrence-specs.md §10). Three flavors share one
-- table: tick (singleton per project, harness-composed prompt), repeat
-- (many per project, fixed-minute cadence + user prompt), cron (many
-- per project, friendly DSL + TZ + user prompt). `cadence` holds
-- minutes-as-string for tick/repeat and the DSL string for cron.
-- `next_fire_at` is recomputed after each fire; the scheduler reads
-- it as its only due-row signal.
CREATE TABLE IF NOT EXISTS coach_recurrence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('tick', 'repeat', 'cron')),
    cadence       TEXT NOT NULL,
    tz            TEXT,
    prompt        TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    next_fire_at  TEXT,
    last_fired_at TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_by    TEXT
);

CREATE INDEX IF NOT EXISTS idx_recurrence_project
    ON coach_recurrence(project_id, enabled);
-- One tick per project — enforced via partial unique index. Repeat /
-- cron rows are unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS idx_recurrence_one_tick
    ON coach_recurrence(project_id) WHERE kind = 'tick';
"""

# Seed agents — idempotent via INSERT OR IGNORE. Per-(slot, project)
# identity rows (name/role/brief) live in agent_project_roles; the
# seed only writes id/kind/workspace_path. The misc-project Coach
# identity is seeded in init_db after the projects row exists.
#
# `workspace_path` is vestigial post the 2026-05-06 workspace
# refactor — agent cwd is now resolved at spawn time via
# `workspace_dir(slot)` against the active project. The column is
# NOT NULL so we seed empty strings rather than misleading legacy
# `/workspaces/...` paths. Drop the column the next time anything
# else in this table needs migrating.
SEED_AGENTS: list[tuple[str, str, str]] = [
    ("coach", "coach", ""),
] + [
    (f"p{i}", "player", "")
    for i in range(1, 11)
]

# The fallback active project. Created on every fresh DB so
# resolve_active_project() never returns None and project-scoped
# inserts never violate the FK.
MISC_PROJECT_ID = "misc"
MISC_PROJECT_NAME = "Misc"


async def crash_recover() -> dict[str, int]:
    """Reset orphaned state left behind by an unclean shutdown.

    If the server died mid-turn, the DB still says agents are
    `working` and tasks have `started_at` set on their executor
    role-assignment row, but no subprocess is actually running.

    Resets:
      - agents.status ∈ {working, waiting} → idle
      - tasks.status='execute' rows whose owner had a working/waiting
        agents.status: clear `tasks.started_at` so the next auto-wake
        cleanly re-flips the avatar from hollow → filled. Owner stays
        (so the Player knows what they were doing on next spawn).
      - task_role_assignments rows whose owner was a zombie agent: clear
        their `started_at` too — the role's spawn-side state mirrors
        tasks.started_at and recovers the same way.
      - **(v0.3.8.2)** stall ladder state on zombie-owned tasks:
        `last_stage_change_at = now`, `stall_escalation_level = 0`,
        `stale_alert_at = NULL`. Without this, a task that was at
        rung 3 with age 3.5h pre-crash + reboot delay would have
        age > 4h on the first post-reboot sweep — walking rung 4
        and auto-archiving the task, punishing the new owner for
        the harness's downtime. Resetting gives the post-crash
        Player a fresh ladder window starting at rung 1.

    Returns a dict of how many rows were touched for logging. Safe
    to call repeatedly — a no-op on a clean DB.
    """
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        # Snapshot the slot ids that were zombie BEFORE we reset their
        # status, so we can scope the started_at resets to just those.
        cur = await db.execute(
            "SELECT id FROM agents WHERE status IN ('working', 'waiting')"
        )
        zombie_slots = [row[0] for row in await cur.fetchall()]
        cur = await db.execute(
            "UPDATE agents SET status = 'idle' "
            "WHERE status IN ('working', 'waiting')"
        )
        agents_reset = cur.rowcount

        tasks_reset = 0
        stall_reset = 0
        if zombie_slots:
            placeholders = ",".join("?" for _ in zombie_slots)
            cur = await db.execute(
                f"UPDATE tasks SET started_at = NULL "
                f"WHERE status = 'execute' AND owner IN ({placeholders})",
                zombie_slots,
            )
            tasks_reset = cur.rowcount
            await db.execute(
                f"UPDATE task_role_assignments SET started_at = NULL "
                f"WHERE owner IN ({placeholders}) "
                f"AND completed_at IS NULL AND superseded_by IS NULL",
                zombie_slots,
            )
            # v0.3.8.2 — reset the stall ladder for any active
            # task tied to a zombie slot (executor OR a current
            # role assignee). last_stage_change_at moves to NOW
            # so age starts fresh; the level + stale_alert_at
            # clear so the post-crash sweep starts at rung 1.
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()
            cur = await db.execute(
                f"UPDATE tasks SET last_stage_change_at = ?, "
                f"stale_alert_at = NULL, stall_escalation_level = 0 "
                f"WHERE status != 'archive' AND ("
                f"  owner IN ({placeholders}) "
                f"  OR id IN ("
                f"    SELECT task_id FROM task_role_assignments "
                f"    WHERE owner IN ({placeholders}) "
                f"    AND completed_at IS NULL "
                f"    AND superseded_by IS NULL"
                f"  )"
                f")",
                (now_iso, *zombie_slots, *zombie_slots),
            )
            stall_reset = cur.rowcount
        await db.commit()
    return {
        "agents_reset": agents_reset,
        "tasks_reset": tasks_reset,
        "stall_reset": stall_reset,
    }


async def _ensure_columns(
    db: aiosqlite.Connection,
    table: str,
    cols: list[tuple[str, str]],
) -> None:
    """Add missing columns to an existing table.

    `cols` is a list of `(column_name, full_ddl_fragment)` pairs where
    `full_ddl_fragment` is everything after `ADD COLUMN` (e.g.
    `runtime TEXT NOT NULL DEFAULT 'claude'`).

    SQLite's `ALTER TABLE … ADD COLUMN … NOT NULL DEFAULT …` populates
    existing rows with the default automatically — no UPDATE needed.
    CHECK constraints can't be added by ALTER TABLE without a full
    table rebuild, so validate those at the API layer instead.
    """
    cur = await db.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cur.fetchall()}
    for name, ddl in cols:
        if name not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


async def init_db() -> None:
    """Create schema + seed agents. Called once on FastAPI startup.

    Logs every step to stdout so a silent hang is immediately diagnosable.
    """
    logger.info("init_db: start (DB_PATH=%s)", DB_PATH)

    parent = Path(DB_PATH).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        st = parent.stat()
        logger.info(
            "init_db: dir ok (path=%s, uid=%s, gid=%s, mode=%o)",
            parent, st.st_uid, st.st_gid, st.st_mode & 0o777,
        )
    except Exception:
        logger.exception("init_db: mkdir failed for %s", parent)
        raise

    # Verify we can actually write a file in the directory — on some
    # networked volume backends the dir appears to exist but writes hang.
    # Use a unique probe name (keyed to the DB file) so parallel test
    # workers don't race on the same shared path.
    probe = parent / f".write-probe-{Path(DB_PATH).stem}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
        logger.info("init_db: write probe passed")
    except Exception:
        logger.exception("init_db: directory not writable at %s", parent)
        raise

    try:
        logger.info("init_db: opening sqlite connection")
        async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
            # Use DELETE journal mode (boring, works on every filesystem).
            # WAL requires mmap + fcntl ops that some container / networked
            # volume FS backends don't support — on Zeabur volumes, WAL
            # initialization hangs with no error, which wedges startup.
            # DELETE serializes writes, which is fine at this scale.
            await db.execute("PRAGMA journal_mode = DELETE")
            await db.execute("PRAGMA foreign_keys = ON")
            logger.info("init_db: pragmas set, running schema")

            # Pre-schema rename: truth_proposals → file_write_proposals.
            # Has to run BEFORE executescript(SCHEMA) — otherwise the
            # CREATE TABLE IF NOT EXISTS file_write_proposals would
            # leave us with both tables on an upgraded DB. Only renames
            # when the old table exists and the new one does not, so
            # it's idempotent and a no-op on fresh installs.
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('truth_proposals', 'file_write_proposals')"
            )
            existing_proposal_tables = {row[0] for row in await cur.fetchall()}
            if (
                "truth_proposals" in existing_proposal_tables
                and "file_write_proposals" not in existing_proposal_tables
            ):
                await db.execute(
                    "ALTER TABLE truth_proposals RENAME TO file_write_proposals"
                )
                # The old index follows the renamed table but keeps its
                # own old name; drop it so the new SCHEMA's CREATE INDEX
                # IF NOT EXISTS lands cleanly under the new name.
                await db.execute(
                    "DROP INDEX IF EXISTS idx_truth_proposals_project_status"
                )

            await db.executescript(SCHEMA)

            # Inline migration runner — CREATE TABLE IF NOT EXISTS
            # only creates missing tables, it doesn't add columns to
            # existing ones. ALTER TABLE … ADD COLUMN populates new
            # columns with the DEFAULT for existing rows; CHECK
            # constraints can't be added retroactively without a table
            # rebuild, so per-column CHECKs are validated at the API
            # layer instead. Idempotent on re-run.
            await _ensure_columns(
                db,
                "agents",
                [
                    ("runtime_override", "runtime_override TEXT"),
                    # Idle-poller debounce timestamp; see Docs/kanban-specs-v2.md §10.
                    ("last_idle_wake_at", "last_idle_wake_at TEXT"),
                    # Stamped by _perform_runtime_transfer_flip. The idle-poller
                    # applies a short cooldown after a runtime transfer to avoid
                    # firing before the queued assign-time wake has a chance to
                    # run (idle-poller false-wake fix, 2026-05-14).
                    ("last_runtime_transfer_at", "last_runtime_transfer_at TEXT"),
                    ("allowed_tools", "allowed_tools TEXT"),
                ],
            )
            await _ensure_columns(
                db,
                "agent_sessions",
                [("codex_thread_id", "codex_thread_id TEXT")],
            )
            # Coach-set per-(slot, project) model override. NULL = no
            # override (fall through to per-pane / role-default / SDK).
            # Validated at SET time and re-validated at SPAWN time
            # against the player's current runtime, so a stale value
            # (set when the player was on Claude, then flipped to
            # Codex) silently no-ops instead of breaking the spawn.
            await _ensure_columns(
                db,
                "agent_project_roles",
                [
                    ("model_override", "model_override TEXT"),
                    # Coach-set effort/plan_mode overrides — same
                    # rationale as model_override (NULL = unset; revalidated
                    # against the current pane / role default at spawn time).
                    ("effort_override", "effort_override INTEGER"),
                    ("plan_mode_override", "plan_mode_override INTEGER"),
                    ("thinking_override", "thinking_override INTEGER"),
                ],
            )
            await _ensure_columns(
                db,
                "turns",
                [
                    ("runtime", "runtime TEXT NOT NULL DEFAULT 'claude'"),
                    ("cost_basis", "cost_basis TEXT"),
                ],
            )
            # Existing rows from the old truth_proposals table get
            # scope='truth' automatically via the DEFAULT, so the rename
            # path keeps existing pending proposals queryable as truth
            # scope without a manual UPDATE.
            await _ensure_columns(
                db,
                "file_write_proposals",
                [("scope", "scope TEXT NOT NULL DEFAULT 'truth'")],
            )
            # CHECK-constraint upgrade for file_write_proposals:
            # the old `truth_proposals` table shipped with a 4-value
            # CHECK (pending/approved/denied/cancelled). The 'superseded'
            # value was added later via SCHEMA, but SQLite's
            # `ALTER TABLE … RENAME TO` preserves the original CHECK
            # clause, so the rename carries the OLD constraint forward
            # and any INSERT with status='superseded' fails on upgraded
            # DBs. The only fix is a table rebuild: SQLite has no
            # `ALTER TABLE DROP/ADD CHECK`. Detect the mismatch by
            # scanning sqlite_master for the constraint text; rebuild
            # only when 'superseded' is missing so this is a no-op on
            # fresh installs (which already have the right constraint).
            await _rebuild_file_write_proposals_if_check_outdated(db)

            # Kanban lifecycle migration v0.1 → v0.2 — rebuilds the tasks
            # table from the legacy status enum (open/claimed/in_progress/
            # blocked/done/cancelled) to the kanban enum (plan/execute/
            # audit_syntax/audit_semantics/ship/archive) and populates
            # the v0.2 lifecycle columns. Idempotent; no-op once
            # team_config['tasks_kanban_v1_migrated'] is set.
            await _rebuild_tasks_if_kanban_outdated(db)
            # Retrofit columns added in v0.2 (workflow / tracking_reason)
            # for DBs that already migrated to v0.2 but lack these. The
            # v0.3 migration drops `required_reviews` / `ship_required` /
            # `complexity` so they're not in this list anymore.
            await _ensure_columns(
                db,
                "tasks",
                [
                    ("workflow", "workflow TEXT NOT NULL DEFAULT 'generic'"),
                    ("tracking_reason", "tracking_reason TEXT"),
                ],
            )
            # Kanban lifecycle migration v0.2 → v0.3 — rebuilds the tasks
            # table to drop complexity / required_reviews / ship_required
            # and add trajectory / last_stage_change_at / stale_alert_at.
            # Idempotent; no-op once team_config['tasks_kanban_v3_migrated']
            # is set.
            await _rebuild_tasks_for_kanban_v3(db)
            # Auditor-focus column on task_role_assignments (kanban-specs
            # §4.6 / §12.1). Free-text Coach-set focus naming what an
            # auditor should check — REQUIRED for auditor_semantics rows
            # (enforced at the API layer), optional for auditor_syntax
            # (defaults applied at wake-prompt time when NULL). NULL on
            # planner / executor / shipper rows.
            await _ensure_columns(
                db,
                "task_role_assignments",
                [("focus", "focus TEXT")],
            )
            # v0.3.8 stall-escalation ladder. Tracks which rung of the
            # stall escalation a task is on (0=fresh, 1=nudged at 30m,
            # 2=coach-notified at 1h, 3=auto-reassigned at 2h,
            # 4=auto-archived at 4h). Reset to 0 when the task
            # progresses (subscriber clears stale_alert_at; we mirror
            # that clear here so a re-stall starts from the bottom).
            await _ensure_columns(
                db,
                "tasks",
                [(
                    "stall_escalation_level",
                    "stall_escalation_level INTEGER NOT NULL DEFAULT 0",
                )],
            )
            # Coach-authored "definition of done" for the task. Optional
            # advisory field; never blocks a transition. Captured at
            # coord_create_task time and/or at coord_approve_stage(plan→
            # execute) time (the second moment is most informative
            # because Coach has read the planner's spec). Surfaced in
            # auditor wake context, in Coach's coordination block for
            # tasks in execute/audit/ship, and echoed back to Coach in
            # the coord_approve_stage tool result when advancing to
            # ship. Empty string = unset = no injection anywhere.
            await _ensure_columns(
                db,
                "tasks",
                [(
                    "success_criteria",
                    "success_criteria TEXT NOT NULL DEFAULT ''",
                )],
            )
            # Indexes that reference kanban-new columns. Live outside
            # SCHEMA because their columns don't exist on legacy DBs
            # until the migration above runs.
            await _ensure_tasks_kanban_indexes(db)

            # Backlog description (nullable TEXT). Existing rows get NULL.
            await _ensure_columns(
                db,
                "backlog_tasks",
                [("description", "description TEXT")],
            )

            # Backlog priority — Coach-created entries carry the priority
            # flag set at coord_create_task time. Existing rows default
            # to 'normal' (no backlog disturbance on upgrade).
            # trajectory_json, note, success_criteria — stored so
            # coord_triage_backlog promote can read them without Coach
            # repeating the details at triage time.
            await _ensure_columns(
                db,
                "backlog_tasks",
                [
                    ("priority", "priority TEXT NOT NULL DEFAULT 'normal'"),
                    ("trajectory_json", "trajectory_json TEXT"),
                    ("note", "note TEXT"),
                    ("success_criteria", "success_criteria TEXT"),
                ],
            )

            # Recurrence §17: end-date / max-fires expiry signals.
            # All three columns are nullable (NULL = unlimited) so
            # existing rows are unaffected. fire_count tracks successful
            # fires only (skips do not increment).
            await _ensure_columns(
                db,
                "coach_recurrence",
                [
                    ("end_date",   "end_date TEXT"),
                    ("max_fires",  "max_fires INTEGER"),
                    ("fire_count", "fire_count INTEGER NOT NULL DEFAULT 0"),
                ],
            )

            logger.info("init_db: schema ok, ensuring misc project")
            # Ensure the fallback project + active-project pointer
            # exist on every fresh boot. INSERT OR IGNORE — never
            # overwrites a user-chosen active project.
            await db.execute(
                "INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)",
                (MISC_PROJECT_ID, MISC_PROJECT_NAME),
            )
            await db.execute(
                "INSERT OR IGNORE INTO team_config (key, value) VALUES "
                "('active_project_id', ?)",
                (MISC_PROJECT_ID,),
            )

            logger.info("init_db: seeding agents")
            await db.executemany(
                "INSERT OR IGNORE INTO agents "
                "(id, kind, workspace_path) VALUES (?, ?, ?)",
                SEED_AGENTS,
            )

            # Seed identity for the misc project so Coach has a name
            # on first spawn. Players start nameless (lacrosse
            # autonamer fills them in on first spawn).
            await db.execute(
                "INSERT OR IGNORE INTO agent_project_roles "
                "(slot, project_id, name, role) VALUES "
                "('coach', ?, 'Coach', 'Team captain')",
                (MISC_PROJECT_ID,),
            )

            # Kanban v0.3 → v2 (Docs/kanban-specs-v2.md §16.4). Runs
            # AFTER the misc project is ensured because the synthetic
            # kanban_v2_cutover row has a FK on projects(id). The new
            # tables themselves (project_events, deviations_log) were
            # created by SCHEMA above (CREATE TABLE IF NOT EXISTS).
            # This step backfills project_events from the existing
            # events table (last 30 days, mappable types, stamped
            # read_by_coach_at so Coach's first tick sees only fresh
            # signals) + inserts one synthetic kanban_v2_cutover row
            # per project (UNREAD). Idempotent via
            # team_config['tasks_kanban_v2_migrated'].
            await _rebuild_tasks_for_kanban_v2(db)

            await db.commit()

            # Write the per-project CLAUDE.md stub for misc on first
            # boot. First-write-only — preserves any user / Coach edits
            # across restarts. Best-effort: a disk failure here doesn't
            # crash init_db.
            try:
                from server.paths import write_project_claude_md_stub
                write_project_claude_md_stub(
                    MISC_PROJECT_ID, MISC_PROJECT_NAME
                )
            except Exception:
                logger.exception(
                    "init_db: write_project_claude_md_stub failed for misc"
                )
            await _seed_recurrence_from_env(db)

            logger.info("init_db: complete")
    except Exception:
        logger.exception("init_db: sqlite operations failed")
        raise


async def _rebuild_file_write_proposals_if_check_outdated(
    db: aiosqlite.Connection,
) -> None:
    """Upgrade `file_write_proposals.status` CHECK to include
    'superseded' on deployments that started life as
    `truth_proposals` with the older 4-value CHECK.

    Detection: read the table's CREATE statement from `sqlite_master`
    and look for the literal `'superseded'` token. Rebuild only when
    it's missing — fresh DBs created from the current SCHEMA already
    have the 5-value CHECK, so this is a no-op there.

    Rebuild pattern (SQLite-canonical):
      1. CREATE the new table under a temp name with the right CHECK.
      2. Copy rows over (existing statuses are all in the 4-value set,
         which is a strict subset of the 5-value set, so no row is
         rejected).
      3. DROP the old table (which auto-drops the dependent index).
      4. RENAME the new table into place.
      5. CREATE the index under the canonical name.

    Wrapped in a transaction so a crash mid-rebuild leaves the old
    table intact.
    """
    cur = await db.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='file_write_proposals'"
    )
    row = await cur.fetchone()
    if not row:
        return  # table doesn't exist yet — fresh install path
    create_sql = row[0] or ""
    if "'superseded'" in create_sql:
        return  # already on the new constraint

    logger.info(
        "init_db: rebuilding file_write_proposals to add 'superseded' "
        "status to CHECK constraint (legacy truth_proposals upgrade)"
    )
    # Per SQLite's canonical table-rebuild guidance
    # (sqlite.org/lang_altertable.html §7): disable FK enforcement
    # for the duration of the rebuild, do the rename dance, run a
    # `foreign_key_check` to confirm the new table's FKs are still
    # consistent, then re-enable. Without the OFF, the
    # `INSERT … SELECT` step trips FK enforcement on the temporary
    # name even though the data is logically valid.
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        await db.execute("BEGIN")
        try:
            await db.execute(
                """
                CREATE TABLE file_write_proposals_new (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    proposer_id       TEXT NOT NULL,
                    scope             TEXT NOT NULL DEFAULT 'truth',
                    path              TEXT NOT NULL,
                    proposed_content  TEXT NOT NULL,
                    summary           TEXT NOT NULL,
                    status            TEXT NOT NULL DEFAULT 'pending'
                                      CHECK (status IN ('pending', 'approved', 'denied', 'cancelled', 'superseded')),
                    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    resolved_at       TEXT,
                    resolved_by       TEXT,
                    resolved_note     TEXT
                )
                """
            )
            await db.execute(
                """
                INSERT INTO file_write_proposals_new
                    (id, project_id, proposer_id, scope, path,
                     proposed_content, summary, status, created_at,
                     resolved_at, resolved_by, resolved_note)
                SELECT id, project_id, proposer_id, scope, path,
                       proposed_content, summary, status, created_at,
                       resolved_at, resolved_by, resolved_note
                  FROM file_write_proposals
                """
            )
            await db.execute("DROP TABLE file_write_proposals")
            await db.execute(
                "ALTER TABLE file_write_proposals_new "
                "RENAME TO file_write_proposals"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS "
                "idx_file_write_proposals_project_status "
                "ON file_write_proposals(project_id, status)"
            )
            # Catch any FK violation that the disabled enforcement
            # masked. Returns one row per orphan ref; empty result =
            # all good.
            cur = await db.execute("PRAGMA foreign_key_check")
            violations = await cur.fetchall()
            if violations:
                raise RuntimeError(
                    f"file_write_proposals rebuild left {len(violations)} "
                    f"FK violations: {violations}"
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    finally:
        await db.execute("PRAGMA foreign_keys = ON")


async def _ensure_tasks_kanban_indexes(db: aiosqlite.Connection) -> None:
    """Create indexes that reference kanban-only columns (`archived_at`,
    `last_stage_change_at`). Lives outside the SCHEMA constant because
    SQLite validates index columns at create time — running these at
    SCHEMA time on a legacy DB (before migration converts the table)
    would fail with "no such column". Called from init_db AFTER
    `_rebuild_tasks_if_kanban_outdated` and `_rebuild_tasks_for_kanban_v3`,
    so the columns are guaranteed to exist by then.

    The v0.2 `idx_tasks_complexity` index is dropped along with the
    `complexity` column in the v0.3 rebuild — its DROP INDEX runs inside
    the rebuild's table-rename dance.
    """
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_archived ON tasks(archived_at DESC)"
    )
    # Index for the stall sweeper — bounded scan over non-archive,
    # non-blocked rows whose last_stage_change_at exceeded the threshold.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_stage_change "
        "ON tasks(last_stage_change_at)"
    )
    # Composite for stall_sweep's WHERE shape:
    #   status NOT IN ('archive') AND blocked = 0
    #     AND last_stage_change_at IS NOT NULL AND last_stage_change_at < ?
    # Leading status + blocked lets the planner skip archived/blocked rows
    # entirely; the trailing last_stage_change_at narrows by stall age
    # without a temp-sort. The plain idx_tasks_stage_change above stays
    # because other queries hit only that column.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_stall_gate "
        "ON tasks(status, blocked, last_stage_change_at)"
    )


async def _rebuild_tasks_if_kanban_outdated(
    db: aiosqlite.Connection,
) -> None:
    """One-shot migration: rebuild the `tasks` table from the legacy
    status enum (open/claimed/in_progress/blocked/done/cancelled) to the
    kanban enum (plan/execute/audit_syntax/audit_semantics/ship/archive)
    and populate the new lifecycle columns from existing data.

    Detection: `'open'` in the table's CREATE statement. The new SCHEMA
    doesn't include any of the legacy status values, so its presence is
    a reliable "needs migration" signal. Fresh DBs created from the
    current SCHEMA already have the new CHECK and skip the rebuild.

    Status mapping (OLD → NEW):
      open         → plan
      claimed      → execute
      in_progress  → execute (started_at backfilled from claimed_at)
      blocked      → execute (with blocked=1)
      done         → archive (archived_at = completed_at)
      cancelled    → archive (cancelled_at = archived_at = completed_at)

    Idempotent via the team_config['tasks_kanban_v1_migrated'] marker —
    once the rebuild succeeds, subsequent boots skip the whole function.
    """
    cur = await db.execute(
        "SELECT value FROM team_config WHERE key = 'tasks_kanban_v1_migrated'"
    )
    if await cur.fetchone():
        return  # already migrated
    cur = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
    )
    row = await cur.fetchone()
    if not row:
        # Fresh install: tasks doesn't exist yet (schema runs after this
        # in some legacy paths). Mark migrated so we don't re-check.
        await db.execute(
            "INSERT OR IGNORE INTO team_config (key, value) VALUES "
            "('tasks_kanban_v1_migrated', '1')"
        )
        await db.commit()
        return
    create_sql = row[0] or ""
    # The old CHECK enumerates 'open' as the lowest value. The new
    # CHECK doesn't have it. So presence of `'open'` is the migration
    # signal. (Any of in_progress/claimed would work too; 'open' is
    # the cleanest single-token check.)
    if "'open'" not in create_sql:
        # Already on the kanban schema (either fresh install or already
        # migrated by an earlier boot before the marker was set). Just
        # mark migrated and exit.
        await db.execute(
            "INSERT OR IGNORE INTO team_config (key, value) VALUES "
            "('tasks_kanban_v1_migrated', '1')"
        )
        await db.commit()
        return

    logger.info(
        "init_db: rebuilding tasks table for kanban lifecycle "
        "(status enum + new columns)"
    )
    # Per SQLite's canonical table-rebuild guidance: disable FK
    # enforcement, do the rename dance, foreign_key_check, re-enable.
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        await db.execute("BEGIN")
        try:
            await db.execute(
                """
                CREATE TABLE tasks_new (
                    id                          TEXT PRIMARY KEY,
                    project_id                  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    title                       TEXT NOT NULL,
                    description                 TEXT NOT NULL DEFAULT '',
                    status                      TEXT NOT NULL DEFAULT 'plan'
                                                CHECK (status IN ('plan', 'execute', 'audit_syntax', 'audit_semantics', 'ship', 'archive')),
                    owner                       TEXT REFERENCES agents(id),
                    created_by                  TEXT NOT NULL,
                    created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    claimed_at                  TEXT,
                    started_at                  TEXT,
                    completed_at                TEXT,
                    archived_at                 TEXT,
                    cancelled_at                TEXT,
                    parent_id                   TEXT REFERENCES tasks(id),
                    priority                    TEXT NOT NULL DEFAULT 'normal'
                                                CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
                    complexity                  TEXT NOT NULL DEFAULT 'standard'
                                                CHECK (complexity IN ('simple', 'standard')),
                    workflow                    TEXT NOT NULL DEFAULT 'generic',
                    tracking_reason             TEXT,
                    required_reviews            TEXT NOT NULL DEFAULT '["formal","semantic"]',
                    ship_required               INTEGER NOT NULL DEFAULT 1,
                    blocked                     INTEGER NOT NULL DEFAULT 0,
                    blocked_reason              TEXT,
                    spec_path                   TEXT,
                    spec_written_at             TEXT,
                    latest_audit_report_path    TEXT,
                    latest_audit_kind           TEXT,
                    latest_audit_verdict        TEXT,
                    compass_audit_report_path   TEXT,
                    compass_audit_verdict       TEXT,
                    tags                        TEXT NOT NULL DEFAULT '[]',
                    artifacts                   TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            # Migrate rows with status mapping + derived columns. The
            # CASE chains compute everything in one pass so we don't have
            # to rescan the table.
            await db.execute(
                """
                INSERT INTO tasks_new
                    (id, project_id, title, description, status, owner,
                     created_by, created_at, claimed_at, started_at,
                     completed_at, archived_at, cancelled_at, parent_id,
                     priority, complexity, workflow, tracking_reason,
                     required_reviews, ship_required, blocked, tags,
                     artifacts)
                SELECT
                    id,
                    project_id,
                    title,
                    description,
                    CASE status
                        WHEN 'open'        THEN 'plan'
                        WHEN 'claimed'     THEN 'execute'
                        WHEN 'in_progress' THEN 'execute'
                        WHEN 'blocked'     THEN 'execute'
                        WHEN 'done'        THEN 'archive'
                        WHEN 'cancelled'   THEN 'archive'
                        ELSE 'plan'
                    END AS status,
                    owner,
                    created_by,
                    created_at,
                    claimed_at,
                    -- started_at: best-effort backfill — if the old
                    -- status was in_progress, claimed_at is the closest
                    -- timestamp we have for "owner began work". For the
                    -- merely claimed case we leave NULL so the card
                    -- reads "assigned, not started" until the next turn
                    -- fires.
                    CASE
                        WHEN status = 'in_progress'    THEN claimed_at
                        WHEN status IN ('done', 'blocked') AND owner IS NOT NULL THEN claimed_at
                        ELSE NULL
                    END AS started_at,
                    completed_at,
                    -- archived_at = completed_at on terminal rows.
                    CASE WHEN status IN ('done', 'cancelled') THEN completed_at END
                        AS archived_at,
                    -- cancelled_at distinguishes cancellation from delivery.
                    CASE WHEN status = 'cancelled' THEN completed_at END
                        AS cancelled_at,
                    parent_id,
                    priority,
                    'standard' AS complexity,
                    'generic' AS workflow,
                    NULL AS tracking_reason,
                    '["formal","semantic"]' AS required_reviews,
                    1 AS ship_required,
                    CASE WHEN status = 'blocked' THEN 1 ELSE 0 END AS blocked,
                    tags,
                    artifacts
                FROM tasks
                """
            )
            await db.execute("DROP TABLE tasks")
            await db.execute("ALTER TABLE tasks_new RENAME TO tasks")
            # Recreate every index on the new table.
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status "
                "ON tasks(status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_owner "
                "ON tasks(owner)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_parent "
                "ON tasks(parent_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_project "
                "ON tasks(project_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_complexity "
                "ON tasks(complexity)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_archived "
                "ON tasks(archived_at DESC)"
            )
            cur = await db.execute("PRAGMA foreign_key_check")
            violations = await cur.fetchall()
            if violations:
                raise RuntimeError(
                    f"tasks rebuild left {len(violations)} FK violations: "
                    f"{violations}"
                )
            await db.execute(
                "INSERT OR IGNORE INTO team_config (key, value) VALUES "
                "('tasks_kanban_v1_migrated', '1')"
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    finally:
        await db.execute("PRAGMA foreign_keys = ON")


async def _rebuild_tasks_for_kanban_v3(
    db: aiosqlite.Connection,
) -> None:
    """One-shot migration v0.2 → v0.3: rebuild the tasks table to drop
    `complexity` / `required_reviews` / `ship_required` and add
    `trajectory` / `last_stage_change_at` / `stale_alert_at`.

    Detection: presence of `complexity` column in the table's CREATE
    statement. Fresh installs from the v0.3 SCHEMA already have the new
    shape and short-circuit on the marker.

    Trajectory derivation per row:
      - complexity='simple' → [{"stage":"execute","to":[<owner>?]}]
      - complexity='standard' → walk: optional plan (if spec_path set),
        always execute, audit_syntax (if 'formal'/'syntax' in
        required_reviews), audit_semantics (if 'semantic'/'semantics'
        in required_reviews), ship (if ship_required=1). Per-stage
        `to` is read from active task_role_assignments rows.

    Backfill `last_stage_change_at` from the most recent
    `task_stage_changed` event for each task, falling back to
    `claimed_at` then `created_at`.

    Idempotent via team_config['tasks_kanban_v3_migrated'].
    """
    cur = await db.execute(
        "SELECT value FROM team_config WHERE key = 'tasks_kanban_v3_migrated'"
    )
    if await cur.fetchone():
        return  # already migrated

    cur = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
    )
    row = await cur.fetchone()
    if not row:
        # Fresh install path: tasks doesn't exist yet. Mark migrated.
        await db.execute(
            "INSERT OR IGNORE INTO team_config (key, value) VALUES "
            "('tasks_kanban_v3_migrated', '1')"
        )
        await db.commit()
        return
    # Detect v0.2 schema by checking actual columns (PRAGMA table_info).
    # A substring search on the CREATE statement is unreliable because
    # the v0.3 SCHEMA mentions "complexity" in a comment — we'd false-
    # positive on every fresh install.
    cur = await db.execute("PRAGMA table_info(tasks)")
    cols = {r[1] for r in await cur.fetchall()}
    if "complexity" not in cols:
        # Already on v0.3 schema (fresh install or earlier v3 boot).
        await db.execute(
            "INSERT OR IGNORE INTO team_config (key, value) VALUES "
            "('tasks_kanban_v3_migrated', '1')"
        )
        await db.commit()
        return

    logger.info(
        "init_db: rebuilding tasks table for kanban v0.3 "
        "(trajectory + last_stage_change_at)"
    )

    # Defensive backfill for an in-between state seen on the first
    # Zeabur deploy: the original v0.2 rebuild (commit 2130c48) only
    # added `complexity`, then later commits retroactively included
    # `required_reviews` / `ship_required` in the same rebuild —
    # but the `tasks_kanban_v1_migrated` marker was already set, so
    # the additions never ran. Add them here as plain ALTER TABLEs
    # before the SELECT so the v0.3 rebuild can read them uniformly.
    await _ensure_columns(
        db,
        "tasks",
        [
            ("required_reviews", "required_reviews TEXT NOT NULL DEFAULT '[\"formal\",\"semantic\"]'"),
            ("ship_required", "ship_required INTEGER NOT NULL DEFAULT 1"),
        ],
    )

    # Read every existing row plus the data we need to derive trajectory
    # in Python. Doing the trajectory build in SQL would be possible via
    # json_group_array + CASE, but the readability cost is high; a one-shot
    # Python loop over O(rows × 5 roles) is fine for the kind of DB sizes
    # this harness sees. Tuple-indexed because init_db's connection does
    # not set aiosqlite.Row as the row_factory.
    cur = await db.execute(
        "SELECT id, status, owner, complexity, required_reviews, "
        "ship_required, spec_path, claimed_at, created_at FROM tasks"
    )
    legacy_rows = list(await cur.fetchall())

    async def _role_owner_for(tid: str, role: str) -> str | None:
        cur = await db.execute(
            "SELECT owner FROM task_role_assignments "
            "WHERE task_id = ? AND role = ? AND superseded_by IS NULL "
            "ORDER BY assigned_at DESC LIMIT 1",
            (tid, role),
        )
        row = await cur.fetchone()
        return (row[0] if row else None) or None

    derived: dict[str, dict[str, Any]] = {}
    for r in legacy_rows:
        tid: str = r[0]
        owner: str | None = r[2]
        complexity = (r[3] or "standard").strip().lower()
        try:
            reviews = json.loads(r[4] or "[]")
            if not isinstance(reviews, list):
                reviews = []
        except Exception:
            reviews = []
        review_kinds = {str(x).strip().lower() for x in reviews}
        ship_required = bool(r[5])
        spec_path: str | None = r[6]
        claimed_at: str | None = r[7]
        created_at: str | None = r[8]

        trajectory: list[dict[str, Any]] = []
        if complexity == "simple":
            trajectory.append({
                "stage": "execute",
                "to": [owner] if owner else [],
            })
        else:
            if spec_path:
                planner = await _role_owner_for(tid, "planner")
                trajectory.append({
                    "stage": "plan",
                    "to": [planner] if planner else [],
                })
            trajectory.append({
                "stage": "execute",
                "to": [owner] if owner else [],
            })
            if review_kinds & {"formal", "syntax"}:
                aud_s = await _role_owner_for(tid, "auditor_syntax")
                trajectory.append({
                    "stage": "audit_syntax",
                    "to": [aud_s] if aud_s else [],
                })
            if review_kinds & {"semantic", "semantics"}:
                aud_e = await _role_owner_for(tid, "auditor_semantics")
                trajectory.append({
                    "stage": "audit_semantics",
                    "to": [aud_e] if aud_e else [],
                })
            if ship_required:
                shipper = await _role_owner_for(tid, "shipper")
                trajectory.append({
                    "stage": "ship",
                    "to": [shipper] if shipper else [],
                })

        # last_stage_change_at: most recent task_stage_changed event for
        # this task, fall back to claimed_at, then created_at.
        cur = await db.execute(
            "SELECT ts FROM events WHERE type = 'task_stage_changed' "
            "AND json_extract(payload, '$.task_id') = ? "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        )
        ev = await cur.fetchone()
        last_change = (ev[0] if ev else None) or claimed_at or created_at

        derived[tid] = {
            "trajectory": json.dumps(trajectory),
            "last_stage_change_at": last_change,
        }

    # Per SQLite's canonical table-rebuild guidance: disable FK
    # enforcement, do the rename dance, foreign_key_check, re-enable.
    await db.execute("PRAGMA foreign_keys = OFF")
    try:
        await db.execute("BEGIN")
        try:
            await db.execute(
                """
                CREATE TABLE tasks_v3 (
                    id                          TEXT PRIMARY KEY,
                    project_id                  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    title                       TEXT NOT NULL,
                    description                 TEXT NOT NULL DEFAULT '',
                    status                      TEXT NOT NULL DEFAULT 'plan'
                                                CHECK (status IN ('plan', 'execute', 'audit_syntax', 'audit_semantics', 'ship', 'archive')),
                    owner                       TEXT REFERENCES agents(id),
                    created_by                  TEXT NOT NULL,
                    created_at                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    claimed_at                  TEXT,
                    started_at                  TEXT,
                    completed_at                TEXT,
                    archived_at                 TEXT,
                    cancelled_at                TEXT,
                    parent_id                   TEXT REFERENCES tasks(id),
                    priority                    TEXT NOT NULL DEFAULT 'normal'
                                                CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
                    trajectory                  TEXT NOT NULL DEFAULT '[{"stage":"execute","to":[]}]',
                    last_stage_change_at        TEXT,
                    stale_alert_at              TEXT,
                    workflow                    TEXT NOT NULL DEFAULT 'generic',
                    tracking_reason             TEXT,
                    blocked                     INTEGER NOT NULL DEFAULT 0,
                    blocked_reason              TEXT,
                    spec_path                   TEXT,
                    spec_written_at             TEXT,
                    latest_audit_report_path    TEXT,
                    latest_audit_kind           TEXT,
                    latest_audit_verdict        TEXT,
                    compass_audit_report_path   TEXT,
                    compass_audit_verdict       TEXT,
                    tags                        TEXT NOT NULL DEFAULT '[]',
                    artifacts                   TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            # Copy rows over with derived trajectory + last_stage_change_at.
            # We hand each task its derived values via UPDATE after the
            # column-preserving copy; cleaner than building a giant
            # CASE-per-row INSERT with N parameter bindings.
            await db.execute(
                """
                INSERT INTO tasks_v3
                    (id, project_id, title, description, status, owner,
                     created_by, created_at, claimed_at, started_at,
                     completed_at, archived_at, cancelled_at, parent_id,
                     priority, workflow, tracking_reason,
                     blocked, blocked_reason, spec_path, spec_written_at,
                     latest_audit_report_path, latest_audit_kind,
                     latest_audit_verdict, compass_audit_report_path,
                     compass_audit_verdict, tags, artifacts)
                SELECT
                    id, project_id, title, description, status, owner,
                    created_by, created_at, claimed_at, started_at,
                    completed_at, archived_at, cancelled_at, parent_id,
                    priority,
                    COALESCE(workflow, 'generic'),
                    tracking_reason,
                    blocked, blocked_reason, spec_path, spec_written_at,
                    latest_audit_report_path, latest_audit_kind,
                    latest_audit_verdict, compass_audit_report_path,
                    compass_audit_verdict, tags, artifacts
                FROM tasks
                """
            )
            # Apply per-row derived trajectory + last_stage_change_at.
            for tid, vals in derived.items():
                await db.execute(
                    "UPDATE tasks_v3 SET trajectory = ?, "
                    "last_stage_change_at = ? WHERE id = ?",
                    (vals["trajectory"], vals["last_stage_change_at"], tid),
                )
            # Drop dependent indexes then the old table.
            await db.execute("DROP INDEX IF EXISTS idx_tasks_complexity")
            await db.execute("DROP INDEX IF EXISTS idx_tasks_status")
            await db.execute("DROP INDEX IF EXISTS idx_tasks_owner")
            await db.execute("DROP INDEX IF EXISTS idx_tasks_parent")
            await db.execute("DROP INDEX IF EXISTS idx_tasks_project")
            await db.execute("DROP INDEX IF EXISTS idx_tasks_archived")
            await db.execute("DROP TABLE tasks")
            await db.execute("ALTER TABLE tasks_v3 RENAME TO tasks")
            # Recreate the always-on indexes (kanban-specific ones live
            # in _ensure_tasks_kanban_indexes which runs after this).
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status "
                "ON tasks(status)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_owner "
                "ON tasks(owner)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_parent "
                "ON tasks(parent_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_project "
                "ON tasks(project_id)"
            )
            cur = await db.execute("PRAGMA foreign_key_check")
            violations = await cur.fetchall()
            if violations:
                raise RuntimeError(
                    f"tasks v3 rebuild left {len(violations)} FK "
                    f"violations: {violations}"
                )
            await db.execute(
                "INSERT OR IGNORE INTO team_config (key, value) VALUES "
                "('tasks_kanban_v3_migrated', '1')"
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    finally:
        await db.execute("PRAGMA foreign_keys = ON")


# ----------------------------------------------------------------------
# Kanban v2 migration (Docs/kanban-specs-v2.md §16.4)
# ----------------------------------------------------------------------
#
# v2 adds two tables (project_events + deviations_log) declared in
# SCHEMA, so fresh installs don't need a rebuild — the CREATE TABLE IF
# NOT EXISTS lines handle them. This function only does the post-create
# work for migrated DBs:
#   1. Backfill `project_events` from the existing `events` table
#      (last 30 days, mappable types, read_by_coach_at = now() so
#      Coach's first tick doesn't drown in 30 days of history).
#   2. Insert one synthetic `kanban_v2_cutover` row (UNREAD) so Coach
#      explicitly walks the in-flight board on the first v2 tick.
# Idempotent via team_config['tasks_kanban_v2_migrated'].
#
# NOTE on type renames per §16.4: a few v1 bus event types are renamed
# in v2 (`message_sent` → `coord_send_message`, `knowledge_written` →
# `coord_write_knowledge`, `decision_written` → `coord_write_decision`,
# `compass_audit_logged` → `compass_audit`). The backfill applies these
# rewrites; everything else passes through. v1-only types that have no
# v2 equivalent (`task_completed`, `task_execution_completed`,
# `task_shipped`) are skipped — they'll be replaced by v2 events going
# forward and surfacing them once on first tick adds noise without
# signal.

# Bus-event type → project_events.type mapping. Same name unless
# explicitly renamed. Imported from server.project_events at runtime
# (the helper module owns the canonical mapping); duplicated here so
# the migration can run before any of that module's imports resolve.
_V2_BACKFILL_RENAMES: dict[str, str] = {
    "message_sent": "coord_send_message",
    "knowledge_written": "coord_write_knowledge",
    "decision_written": "coord_write_decision",
    "compass_audit_logged": "compass_audit",
}

# v2-mappable bus event types — anything not in this set is dropped
# during backfill. Keep in sync with `_BUS_TO_LOG_TYPE` in
# server/project_events.py.
_V2_BACKFILL_TYPES: frozenset[str] = frozenset({
    # Direct pass-through
    "commit_pushed",
    "task_spec_written",
    "task_role_completed",
    "audit_report_submitted",
    "audit_fail_notification",
    "task_stage_changed",
    "task_role_assigned",
    "task_role_stand_down",
    "task_trajectory_changed",
    "task_blocked_changed",
    "task_archived",
    "commit_without_task_id_warning",
    "task_stage_stale",
    "task_stall_persisting",
    "task_stall_auto_reassigned",
    "task_stall_no_alternative",
    "task_stall_auto_archived",
    "task_spec_unrecorded",
    "task_audit_unrecorded",
    "watchdog_finding",
    "pending_plan",
    "human_attention",
    "auto_compact_triggered",
    "session_compacted",
    "kanban_board_stalled",
    # Renamed (key = v1 bus type, value = v2 log type) — see _V2_BACKFILL_RENAMES
    "message_sent",
    "knowledge_written",
    "decision_written",
    "compass_audit_logged",
})


def _v2_backfill_pointer(log_type: str, payload: dict[str, Any]) -> str | None:
    """Extract payload_pointer per §9.2 by event type. None when the
    type doesn't carry a structured pointer."""
    if log_type == "commit_pushed":
        return payload.get("sha") or None
    if log_type == "task_spec_written":
        return payload.get("spec_path") or None
    if log_type == "task_role_completed":
        return payload.get("artifact_path") or None
    if log_type == "audit_report_submitted":
        return payload.get("report_path") or None
    if log_type == "coord_send_message":
        body = payload.get("body") or payload.get("text") or ""
        if not body:
            return None
        return body[:500]
    if log_type in ("coord_write_knowledge", "coord_write_decision"):
        return payload.get("path") or payload.get("relative_path") or None
    return None


async def _rebuild_tasks_for_kanban_v2(
    db: aiosqlite.Connection,
) -> None:
    """One-shot post-v0.3 work for kanban v2 (Docs/kanban-specs-v2.md
    §16.4): backfill project_events from the events table, insert the
    synthetic kanban_v2_cutover event.

    The new tables themselves are created by the SCHEMA constant above
    (CREATE TABLE IF NOT EXISTS). This function handles the data side
    only.

    Idempotent via team_config['tasks_kanban_v2_migrated'].
    """
    cur = await db.execute(
        "SELECT value FROM team_config WHERE key = 'tasks_kanban_v2_migrated'"
    )
    if await cur.fetchone():
        return  # already migrated

    logger.info(
        "init_db: kanban v2 backfill — copying mappable events into "
        "project_events (last 30 days)"
    )

    # Backfill: SELECT events of mappable types from the last 30 days
    # that have a resolvable project_id. Stamp them as already-read so
    # Coach's first v2 tick sees only fresh signals.
    type_placeholders = ",".join("?" for _ in _V2_BACKFILL_TYPES)
    cur = await db.execute(
        f"""
        SELECT id, ts, agent_id, project_id, type, payload
        FROM events
        WHERE type IN ({type_placeholders})
          AND project_id IS NOT NULL
          AND ts >= datetime('now', '-30 days')
        ORDER BY ts ASC
        """,
        list(_V2_BACKFILL_TYPES),
    )
    rows = list(await cur.fetchall())

    backfilled = 0
    for row in rows:
        try:
            ts = row[1]
            actor = row[2] or "system"
            project_id = row[3]
            v1_type = row[4]
            payload_raw = row[5] or "{}"
            try:
                payload = json.loads(payload_raw)
                if not isinstance(payload, dict):
                    payload = {}
            except Exception:
                payload = {}
            log_type = _V2_BACKFILL_RENAMES.get(v1_type, v1_type)
            task_id = payload.get("task_id") or None
            pointer = _v2_backfill_pointer(log_type, payload)
            await db.execute(
                """
                INSERT INTO project_events
                    (project_id, ts, actor, type, task_id,
                     payload_json, payload_pointer, read_by_coach_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id, ts, actor, log_type, task_id,
                    payload_raw, pointer, ts,
                ),
            )
            backfilled += 1
        except Exception:
            # Per §16.4 + the spec's risk-mitigation note: best-effort.
            # A single malformed row never fails the migration.
            logger.exception(
                "kanban v2 backfill: skipped event id=%s type=%s",
                row[0] if row else "?", row[4] if row else "?",
            )

    # Synthetic cutover event — UNREAD so Coach's first v2 tick walks
    # the active board. Fired once per project that has any rows in
    # `tasks` (a fresh install has just `misc` with no tasks; we still
    # fire it so Coach sees "v2 is live" on the first turn).
    cur = await db.execute("SELECT id FROM projects")
    projects = [r[0] for r in await cur.fetchall()]
    cutover_body = (
        "Kanban has been migrated to v2 (shape-(2) routing). "
        "Walk the active board: for each non-archive task, decide the "
        "next move (advance via coord_approve_stage, reassign, archive, "
        "or leave in place) and act accordingly. From now on every "
        "stage transition is your call — there is no auto-routing."
    )
    cutover_payload = json.dumps({
        "type": "kanban_v2_cutover",
        "to": "coach",
        "body": cutover_body,
    })
    cutover_count = 0
    for project_id in projects:
        try:
            await db.execute(
                """
                INSERT INTO project_events
                    (project_id, actor, type, task_id,
                     payload_json, payload_pointer, read_by_coach_at)
                VALUES (?, 'system', 'kanban_v2_cutover', NULL,
                        ?, ?, NULL)
                """,
                (project_id, cutover_payload, cutover_body),
            )
            cutover_count += 1
        except Exception:
            logger.exception(
                "kanban v2 cutover insert failed for project_id=%s",
                project_id,
            )

    await db.execute(
        "INSERT OR IGNORE INTO team_config (key, value) VALUES "
        "('tasks_kanban_v2_migrated', '1')"
    )
    await db.commit()
    logger.info(
        "init_db: kanban v2 backfill complete (events_copied=%d, "
        "cutover_rows=%d)", backfilled, cutover_count,
    )


async def _seed_recurrence_from_env(db: aiosqlite.Connection) -> None:
    """One-shot migration: if HARNESS_COACH_TICK_INTERVAL is non-zero,
    seed a tick row for every existing project (so the new scheduler
    fires on the same cadence as the legacy in-memory loop did).

    Idempotent via the `recurrence_v1_seeded` team_config flag —
    later boots skip this entirely. Per `recurrence-specs.md` §14, the
    env var is honored on first migration only and is documented as
    deprecated thereafter.

    Cadence is converted seconds→minutes, rounded up, min 1, capped at
    a sane upper bound. The legacy var was seconds; the new schema is
    minutes (`recurrence-specs.md` §1: "durations are in minutes
    everywhere").
    """
    cur = await db.execute(
        "SELECT value FROM team_config WHERE key = 'recurrence_v1_seeded'"
    )
    if await cur.fetchone():
        return
    raw = os.environ.get("HARNESS_COACH_TICK_INTERVAL", "0").strip()
    try:
        seconds = max(0, int(raw))
    except ValueError:
        seconds = 0
    if seconds > 0:
        # Round up so a sub-minute env value (e.g. 30s) yields a 1-min
        # tick. Floor would silently drop the recurrence.
        minutes = max(1, (seconds + 59) // 60)
        cur = await db.execute("SELECT id FROM projects")
        rows = await cur.fetchall()
        for (project_id,) in rows:
            await db.execute(
                "INSERT OR IGNORE INTO coach_recurrence "
                "(project_id, kind, cadence, prompt, enabled, "
                "next_fire_at, created_by) "
                "VALUES (?, 'tick', ?, NULL, 1, "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'env_migration')",
                (project_id, str(minutes)),
            )
        logger.info(
            "init_db: seeded recurrence tick rows from "
            "HARNESS_COACH_TICK_INTERVAL=%s (minutes=%d, projects=%d)",
            raw, minutes, len(rows),
        )
    await db.execute(
        "INSERT OR REPLACE INTO team_config (key, value) "
        "VALUES ('recurrence_v1_seeded', '1')"
    )
    # Spec §10: migration recurrence_v1 stamps team_config.schema_version.
    # We don't otherwise track schema versions in this codebase (CREATE
    # TABLE IF NOT EXISTS handles the rest), but stamping here gives a
    # cheap signal that the v1 migration ran and is forward-compatible
    # with a future versioned-migration runner.
    await db.execute(
        "INSERT OR REPLACE INTO team_config (key, value) "
        "VALUES ('schema_version', 'recurrence_v1')"
    )
    await db.commit()


# TOCTOU mitigation. The activate handler in server.projects_api
# pins the new project via pin_active_project() during the swap so
# any tool call / event publish that begins mid-switch sees a coherent
# view. Outside the pinned context resolve_active_project reads
# team_config as before.
import contextvars as _ctx

_pinned_project: _ctx.ContextVar[str | None] = _ctx.ContextVar(
    "harness_pinned_project", default=None
)


class pin_active_project:
    """Context manager: while active, resolve_active_project() returns
    the pinned slug regardless of team_config. Stack-safe via
    contextvars — nested pins restore the outer one on exit."""

    def __init__(self, project_id: str) -> None:
        self._project_id = project_id
        self._token: _ctx.Token[str | None] | None = None

    def __enter__(self) -> str:
        self._token = _pinned_project.set(self._project_id)
        return self._project_id

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._token is not None:
            _pinned_project.reset(self._token)
            self._token = None


async def resolve_active_project(db: aiosqlite.Connection | None = None) -> str:
    """Return the active project_id.

    Order of precedence:
      1. The slug pinned via `pin_active_project(...)` if any.
      2. `team_config.active_project_id`.
      3. Fallback to the `misc` project.

    The pin path is the TOCTOU mitigation — the activate handler holds
    a pin while it swaps the team_config row + reloads context, so
    coord_* tools and bus.publish observe a single coherent project
    across the whole switch.
    """
    pinned = _pinned_project.get()
    if pinned:
        return pinned
    own = False
    if db is None:
        db = await configured_conn()
        own = True
    try:
        cur = await db.execute(
            "SELECT value FROM team_config WHERE key = 'active_project_id'"
        )
        row = await cur.fetchone()
    finally:
        if own:
            await db.close()
    if not row:
        return MISC_PROJECT_ID
    try:
        v = row[0]
    except Exception:
        v = None
    return v or MISC_PROJECT_ID


async def set_active_project(project_id: str) -> None:
    """Update team_config.active_project_id. Caller is responsible for
    holding any concurrency lock (the activate handler in
    server.projects_api uses an asyncio.Lock)."""
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT OR REPLACE INTO team_config (key, value) "
            "VALUES ('active_project_id', ?)",
            (project_id,),
        )
        await c.commit()
    finally:
        await c.close()


def open_conn() -> aiosqlite.Connection:
    """Return an uninitialized aiosqlite connection context manager."""
    return aiosqlite.connect(DB_PATH, timeout=10.0)


async def configured_conn() -> aiosqlite.Connection:
    """Open a connection with Row factory + FK enforcement."""
    c = await aiosqlite.connect(DB_PATH, timeout=10.0)
    c.row_factory = aiosqlite.Row
    await c.execute("PRAGMA foreign_keys = ON")
    return c

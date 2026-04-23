# TeamOfTen — Specs

A personal orchestration harness for **1 Coach + 10 Player** Claude Code
agents, sharing state and coordinating through a single FastAPI mono-service.
This document enumerates the app's behavior as it exists on `main`. It is
descriptive, not aspirational — every line should match the running code.

The rule of thumb: **everything below is verifiable against the code in
the repo**. If you find a gap, it's either a bug, this doc is stale, or
a "next likely" item in [CLAUDE.md](CLAUDE.md#next-likely).

---

## 1. Purpose and shape

- **Team**: exactly 11 agents — 1 Coach (`coach`) + 10 Players (`p1`…`p10`).
- **Hierarchy**: Coach gives orders; Players execute. Players never give
  orders to peers — they can message peers for info, but cannot assign.
- **Single billing**: runs on Max-plan OAuth, one credential shared across
  all agents. No `ANTHROPIC_API_KEY` code paths.
- **Single write-handle discipline**: every state mutation (task, message,
  memory, event) routes through the harness server process, which holds
  the only SQLite write handle. Agents never write SQLite directly; they
  call MCP tools.
- **Per-worktree isolation**: each Player runs in its own git worktree
  under `/workspaces/<slot>/project/` on branch `work/<slot>`.

## 2. Tech stack

- Backend: Python 3.12 + FastAPI + WebSocket + aiosqlite, single mono-service
- Agent runtime: Claude Agent SDK (Python), authenticated via `claude /login`
- Frontend: Preact 10 + htm + Split.js (no build step; ESM from esm.sh)
- Hot-path state: SQLite on `/data/harness.db`
- Durable mirror: kDrive WebDAV — memory docs (live), decisions (live),
  event log (5-min flush), hourly VACUUM INTO snapshots
- Deploy: Docker container on Zeabur, auto-pulled from GitHub `main`
- TLS / ingress: Zeabur (no Caddy)

## 3. Storage model

### 3.1 SQLite tables

- `agents` — id, kind, name, role, status, current_task_id, model,
  workspace_path, session_id, cost_estimate_usd, started_at,
  last_heartbeat. 11 seed rows inserted idempotently on init.
- `tasks` — id, title, description, status, owner, created_by, created_at,
  claimed_at, completed_at, parent_id, priority, tags, artifacts.
  Status ∈ {open, claimed, in_progress, blocked, done, cancelled}.
  Priority ∈ {low, normal, high, urgent}.
- `events` — id, ts, agent_id, type, payload (JSON). The audit log.
- `messages` — id, from_id, to_id, subject, body, sent_at, read_at,
  in_reply_to, priority. Priority ∈ {normal, interrupt}.
- `message_reads` — junction table tracking per-recipient read state for
  broadcast handling.
- `memory_docs` — topic PK, content, last_updated, last_updated_by, version.

### 3.2 kDrive (when configured)

- `memory/<topic>.md` — mirrors every `coord_update_memory` synchronously.
- `decisions/<date>-<slug>.md` — append-only architectural records.
- `events/<YYYY-MM-DD>.jsonl` — today's events, flushed every 5 min.
  During UTC 00:00–02:00 yesterday's file is re-flushed for boundary safety.
- `snapshots/<ts>.db` — hourly `VACUUM INTO` of the full DB. Retention
  capped at `HARNESS_KDRIVE_SNAPSHOT_RETENTION` (default 48).

## 4. Agent lifecycle

### 4.1 Per-turn flow (`run_agent`)

1. **Global pause check** — if the harness is paused, emit `paused` and
   return.
2. **Cost cap check** — per-agent and team daily caps. On breach, emit
   `cost_capped` and return.
3. Emit `agent_started` (carrying `resumed_session: bool`); set status to
   `working`.
4. Read prior `session_id` from agents table; pass to SDK as `resume=...`
   if non-null so the conversation continues.
5. Build `ClaudeAgentOptions` with `cwd=/workspaces/<slot>`, `max_turns=10`,
   MCP `coord` server (per-caller tool identity), tool allowlist by kind
   (Coach: read + coord; Player: read + write + coord).
6. Stream messages: `text`, `tool_use`, `tool_result` surface as events;
   `result` records duration + cost + session_id, persists session_id and
   accumulates cost_estimate_usd.
7. Register task in `_running_tasks[slot]` so the cancel endpoint can abort.
8. On `asyncio.CancelledError`: emit `agent_cancelled`, set status `idle`,
   re-raise (task ends cancelled).
9. On other exceptions: emit `error`, set status `error`.
10. On clean exit: set status `idle`, emit `agent_stopped`.

### 4.2 Per-turn overrides

Sent via `POST /api/agents/start`:
- `model: str | None` → SDK `model` (default = container's default).
- `plan_mode: bool` → SDK `permission_mode="plan"`.
- `effort: 1..4` → SDK `effort` literal (low/medium/high/max).

### 4.3 Coach autoloop

- Env-gated by `HARNESS_COACH_TICK_INTERVAL` (seconds; 0 disables).
- Sleep-first so first tick doesn't fire before workspaces are ready.
- Skips when Coach is already working OR harness is paused.
- Fires `run_agent("coach", COACH_TICK_PROMPT)` on each tick.
- Manual trigger: `POST /api/coach/tick` (409 if Coach is busy).

### 4.4 Cost caps

- Per-agent: `HARNESS_AGENT_DAILY_CAP` (default 5.00 USD).
- Team: `HARNESS_TEAM_DAILY_CAP` (default 20.00 USD).
- Sum derived from `result` events emitted today (UTC). Set to 0 to disable.

## 5. MCP `coord_` tools

All in-process MCP tools served by a per-caller `build_coord_server(slot)`
so the tool body knows which agent invoked it. Names prefixed
`mcp__coord__coord_`.

| Tool                     | Coach | Player | Effect |
|--------------------------|:-----:|:------:|--------|
| `coord_list_tasks`       |  ✓    |   ✓    | Filter by status / owner. |
| `coord_create_task`      |  ✓ (top-level) | ✓ (subtasks only) | Only Coach creates top-level tasks. |
| `coord_claim_task`       |  —    |   ✓    | Claim open task; refuses when already owning one. |
| `coord_update_task`      |  ✓ (any) | ✓ (own) | Transitions: open→claimed→in_progress→(blocked)→(done\|cancelled). Done/cancelled clears owner's current_task_id. |
| `coord_assign_task`      |  ✓    |   —    | Push-assign an open task to a Player. |
| `coord_send_message`     |  ✓    |   ✓    | coach / p1..p10 / "broadcast". interrupt priority for urgent. |
| `coord_read_inbox`       |  ✓    |   ✓    | Marks messages read per-recipient. |
| `coord_list_memory`      |  ✓    |   ✓    | List topics. |
| `coord_read_memory`      |  ✓    |   ✓    | Read content. |
| `coord_update_memory`    |  ✓    |   ✓    | Full overwrite; mirrors to kDrive. |
| `coord_commit_push`      |  —    |   ✓    | `git add -A` + commit + optional push in slot's worktree. Emits `commit_pushed`. |
| `coord_write_decision`   |  ✓    |   —    | Append-only markdown decision record. |
| `coord_set_player_role`  |  ✓    |   —    | Write agents.name / agents.role; emits `player_assigned`. |
| `coord_request_human`    |  ✓    |   ✓    | Emit `human_attention` event (UI banner). urgency ∈ {normal, blocker}. |

Plus standard Claude SDK tools: `Read / Grep / Glob / ToolSearch` (both
kinds), `Write / Edit / Bash` (Players only — Coach structurally can't
touch code).

## 6. HTTP API

All paths under `/api/*` except `/api/health` require
`Authorization: Bearer $HARNESS_TOKEN` when the env var is set.

### 6.1 Readiness

- `GET /api/health` → per-subsystem probe (db / static / claude_cli /
  kdrive / workspaces). 200 when all required green, 503 otherwise.
  Body always carries `checks`. Public (no auth).
- `GET /api/status` → version, uptime, host, paused flag,
  running_slots, ws_subscribers, caps (per-agent/team/today),
  kdrive enabled+reason, workspaces.

### 6.2 Agents

- `GET /api/agents` → full roster (sorted Coach first, then p1..p10).
- `POST /api/agents/start` `{ agent_id, prompt, model?, plan_mode?, effort? }`
  → enqueues `run_agent`.
- `POST /api/agents/cancel-all` → cancels every in-flight task.
- `POST /api/agents/{id}/cancel` → cancels one; 409 if not running.
- `DELETE /api/agents/{id}/session` → clears session_id so next run
  starts a fresh SDK conversation.

### 6.3 Pause

- `GET /api/pause` → `{ paused: bool }`.
- `POST /api/pause` `{ paused: bool }` → in-memory flag, emits
  `pause_toggled`.

### 6.4 Tasks

- `GET /api/tasks` → all tasks.
- `POST /api/tasks` `{ title, description?, priority?, parent_id? }` →
  creates task with `created_by='human'`.
- `POST /api/tasks/{id}/cancel` → status → cancelled, clears owner's
  current_task_id if that task.

### 6.5 Messages

- `GET /api/messages?limit=N` → recent messages (≤ 200).
- `POST /api/messages` `{ to, subject?, body, priority? }` → queue from
  human without spawning a turn. `from_id='human'`.

### 6.6 Memory

- `GET /api/memory` → topic index (size, version, last_updated_by).
- `GET /api/memory/{topic}` → full content.
- `POST /api/memory` `{ topic, content }` → upsert with
  `last_updated_by='human'`, auto-bumps version. Mirrors to kDrive same
  as agent writes.

### 6.7 Decisions

- `GET /api/decisions` → list (filename / title / size / mtime).
- `GET /api/decisions/{filename}` → full markdown content. Validates
  filename (no slashes, ends with `.md`).

### 6.8 Events

- `GET /api/events?agent=&type=&since_id=&limit=` → paginated history;
  most recent `limit`, ordered oldest→newest in response.

### 6.9 Coach tick

- `POST /api/coach/tick` → fires one autoloop-equivalent tick; 409 if
  Coach is already working.

### 6.10 Attachments

- `POST /api/attachments` (multipart) → uploads pasted image to
  `/data/attachments`, returns `{ id, filename, url }`. Each
  workspace has a `/workspaces/<slot>/attachments/` symlink to the
  shared directory.

## 7. WebSocket `/ws`

Single connection at app-root. Auth via `?token=<HARNESS_TOKEN>` query
string (browsers can't set Authorization on WS).

### 7.1 Server→client

- `connected` — sent on accept.
- `ping` — every 30s of quiet, so client can detect zombie connections.
- All domain events (same shape as persisted rows).

### 7.2 Event types (by category)

Agent lifecycle:
`agent_started` (carries `resumed_session`), `text`, `tool_use`,
`tool_result`, `result` (duration_ms, cost_usd, session_id, is_error),
`error`, `agent_stopped`, `agent_cancelled`, `cost_capped`, `paused`,
`session_cleared`.

Tasks:
`task_created`, `task_claimed`, `task_assigned`, `task_updated`.

Coord / comms:
`message_sent`, `memory_updated`, `decision_written`, `commit_pushed`,
`human_attention` (urgency: normal | blocker), `player_assigned`,
`pause_toggled`.

### 7.3 Client watchdog

Tracks last-message time. If > 60 s without any message, the client
force-closes the socket and the `onclose` handler schedules a 2 s
reconnect (bumping `wsAttempt` state).

## 8. UI — global

### 8.1 Layout

- **Left rail** (44 px): WS dot, Coach + p1..p10 slot buttons, separator,
  cancel-all (when any agent is working), pause toggle, env-panel toggle,
  settings gear.
- **Panes area**: 2D — array of columns, each column is a vertical stack
  of agent panes. Split.js provides horizontal gutters between columns
  and vertical gutters inside stacked columns. Sizes persist per layout
  signature (`harness_split_sizes_v1`, capped 30 keys).
- **Env pane** (right, ~340 px, toggleable): Attention / Tasks / Cost /
  Inbox / Memory / Decisions / Timeline sections.

### 8.2 Opening / stacking / moving panes

- Click a slot button: open as a new column on the right. If already
  open, scroll the pane into view.
- Shift-click a slot button: stack into the rightmost column.
- Drag a pane's label area onto another pane: insert before it (handles
  both within-column reorder and cross-column move).
- Drag onto the bottom strip of a column: append to that column.
- Drag onto the thin strip at the right edge of `.panes`: new column.

### 8.3 Persistence keys (localStorage)

- `harness_layout_v1` — `openColumns` + `envOpen`.
- `harness_split_sizes_v1` — Split.js sizes per layout signature.
- `harness_pane_settings_v1` — per-slot model / plan_mode / effort.
- `harness_prompt_history_v1` — per-slot last 40 prompts.
- `harness_task_filter_v1` — active / all / done.
- `harness_attention_dismissed_v1` — dismissed `human_attention` event ids.

### 8.4 Keyboard shortcuts (global, ignored in form fields)

- **⌘/Ctrl + B** — toggle env panel.
- **⌘/Ctrl + .** — toggle pause.
- Inside a pane input:
  - **⌘/Ctrl + Enter** — send.
  - **⌘/Ctrl + ↑ / ↓** — cycle prompt history.
  - **Paste image** → uploads to `/api/attachments`, adds path to prompt.
- Inside the settings popover / drawer / in-pane search:
  - **Escape** — close.

### 8.5 Tab title

Composed from live state: `⏸ N⚡ M● TeamOfTen` where `⏸` = paused,
`N⚡` = working agents, `M●` = unread slots. Empty prefix = idle.

## 9. Pane — per-agent

### 9.1 Header (left to right)

- Status dot (green idle / amber working / red error / gray stopped),
  tooltip: status + last heartbeat + first-started relative times.
- Slot id · displayed name · role (from `coord_set_player_role`).
- ⚑ Current-task chip (title truncated to 24 chars, full tooltip).
- ● Session id indicator when resumable; × clears (DELETE session).
- `$X.XXX` cumulative cost chip.
- `Ns · $X.XXX` last-turn duration+cost (hidden while working).
- ⏹ Cancel in-flight button (only shown when status=working).
- Override dot (accent color) when pane has per-turn override settings.
- ⌕ In-pane search toggle.
- ↓ Export conversation as markdown.
- ⚙ Per-pane settings popover (model / plan_mode / effort).
- × Close pane.

### 9.2 Body

- Loads up to 500 events on mount via `/api/events?agent=<slot>&limit=500`.
- Merges persisted history with live WS events, deduplicates by `__id`.
- Pairs `tool_use` ↔ `tool_result` by `tool_use_id` → renders them inline.
- Auto-scroll to bottom IF the user is already near the bottom (≤80 px).
- Per-tool renderers (Read / Edit / Bash / Grep / Glob / coord_* /
  generic). Read on an image path inlines the image preview.
- Edit renders as a red/green diff card.
- Empty-pane hint with example prompts for Coach / brief for Players.

### 9.3 Input footer

- Textarea with paste-image support.
- Attachments strip (below input, above send button) with × per item.
- Hint row: "⌘/Ctrl+Enter to send · ⌘/Ctrl+↑↓ history".

### 9.4 Per-pane settings popover (⚙)

- Model override (default / Opus 4.7 / Sonnet 4.6 / Haiku 4.5).
- Plan-mode checkbox.
- Effort slider (default / low / med / high / max).
- Reset + Done buttons. Click-outside / Escape closes.

## 10. Env pane — team-level

Section order top → bottom:

### 10.1 Attention
Pinned banner when any `human_attention` event is undismissed. Loads
both persisted (`?type=human_attention&limit=100`) and live events.
Per-item × and "dismiss all" header button. Dismissal is localStorage
keyed by event __id.

### 10.2 Tasks
Active / all / done filter (persisted). Hierarchy rendered: subtasks
indent under parent with ↳ + left border. Cancel × button per
active row (confirm before firing). Bottom form creates a new
top-level task as `human`.

### 10.3 Cost
Total across team + per-agent list sorted by spend. Shows caps +
team spent today. Working count.

### 10.4 Inbox
Latest 50 messages, click-to-expand. + send opens a composer (to /
priority / subject / body). Refreshes on `message_sent`.

### 10.5 Memory
Lists topics with version + last_updated_by. Click row to expand
content. + write opens an inline composer for human memory writes.

### 10.6 Decisions
Lists appends. Click row to load + expand full markdown. Refreshes
on `decision_written`.

### 10.7 Timeline
Flat chronological stream filtered to overview-worthy event types
(`agent_started`, `text`, `error`, all `task_*`, `message_sent`,
`memory_updated`, `cost_capped`, `commit_pushed`, `decision_written`,
`human_attention`, `player_assigned`, `agent_cancelled`, `paused`,
`pause_toggled`). Capped at last 80. Sticky-to-bottom auto-scroll.

Env header has a `↓` team-export button (parallel-fetches open panes
and merges into one markdown with H1 header + H2-per-pane sections).

## 11. Settings drawer

- **Health** section: runtime row (paused / running / ws subs) +
  per-subsystem health dots + ↻ refresh.
- **Authentication**: copy for the `claude /login` device-code flow.
- **Cost caps**: read-only display of current caps (edit via env var).
- **kDrive mirror**: enabled/disabled + reason.
- **Layout**: reset Split.js sizes (clears localStorage key + reload).
- **About**: project line + shortcut reference.

## 12. Config (env vars)

| Var | Default | Purpose |
|-----|--------:|---------|
| `HARNESS_TOKEN` | unset | If set, all `/api/*` except `/api/health` need `Bearer <token>`; WS uses `?token=`. |
| `HARNESS_DB_PATH` | `/data/harness.db` | SQLite path. |
| `HARNESS_AGENT_DAILY_CAP` | `5.0` | USD per agent per day. 0 disables. |
| `HARNESS_TEAM_DAILY_CAP` | `20.0` | USD across team per day. 0 disables. |
| `HARNESS_COACH_TICK_INTERVAL` | `0` | seconds between Coach autoloop ticks. 0 disables. |
| `HARNESS_KDRIVE_FLUSH_INTERVAL` | `300` | seconds between event-log flushes. |
| `HARNESS_KDRIVE_SNAPSHOT_INTERVAL` | `3600` | seconds between DB snapshots. |
| `HARNESS_KDRIVE_SNAPSHOT_RETENTION` | `48` | snapshots retained on kDrive. |
| `HARNESS_DECISIONS_DIR` | `/data/decisions` | local fallback for decisions. |
| `HARNESS_PROJECT_REPO` | unset | if set, clones to `/workspaces/.project` and creates worktrees at boot. |
| `HARNESS_PROJECT_BRANCH` | `main` | default branch for fresh worktrees. |
| `KDRIVE_WEBDAV_URL` / `KDRIVE_USER` / `KDRIVE_APP_PASSWORD` / `KDRIVE_ROOT_PATH` | unset / unset / unset / `/harness` | kDrive mirror. All three of the first three must be set to enable. |

## 13. Guarantees and non-goals

**Guarantees**
- No agent writes SQLite directly — every mutation is through an MCP
  tool funneled through the server process.
- `human_attention` events survive restarts (DB-persisted + API-fetched).
- Sessions resume across turns unless explicitly cleared.
- Task cancel clears the owner's `current_task_id` so the agent is
  unblocked for the next claim.
- Pause blocks new starts + autoloop ticks; existing in-flight turns
  continue until they complete (or until explicit cancel).
- Cost caps are enforced **before** `agent_started` is emitted, so a
  capped attempt doesn't count as a turn.

**Non-goals / out of scope**
- Multi-tenant authz — this is a single-user personal harness.
- Horizontal scaling — single-process; fan-out happens in 11 SDK
  subprocesses.
- Browser-native Ctrl+F as a replacement for ⌕ in-pane search
  (browser Ctrl+F only searches rendered DOM; ⌕ filters ALL loaded
  events).
- Persisted pause state — intentionally in-memory; restart resumes.
- Automatic cycle detection for `tasks.parent_id` — defensive `seen`
  guard in the render walk, but no DB-level enforcement.

## 14. Test suite

Under `server/tests/`, pytest-asyncio auto mode, DB-level (no FastAPI
TestClient so the suite doesn't pull in claude-agent-sdk).

- `test_db.py` — schema smoke, 11-agent seeding, idempotent init.
- `test_events.py` — bus publish/subscribe/persist round-trip,
  late-subscriber backlog intentionally empty.
- `test_tools_consts.py` — VALID_RECIPIENTS shape, MEMORY_TOPIC_RE
  accept/reject, coord-tool naming, Coach/Player allowlist split.
- `test_tasks_sm.py` — status default, status CHECK, cancel clears
  owner, agent kind CHECK.

Run: `uv sync --extra dev && uv run pytest`.

## 15. Known gotchas (from CLAUDE.md)

- Claude CLI OAuth tokens do NOT live in `~/.claude.json` — use
  `claude /login` per host.
- Zeabur's default region 403s `claude.ai/install.sh`. Dockerfile
  installs via `npm install -g @anthropic-ai/claude-code` instead.
- Do NOT pre-create `/data` in the image; let the volume mount create
  it or SQLite will hang silently at startup.
- `.gitattributes` forces LF on `*.sh` and `Dockerfile*` — new scripts
  need the same treatment or they fail in Linux containers with
  `$'\r': command not found`.

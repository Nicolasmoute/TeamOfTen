# TeamOfTen — Full Specs

Comprehensive reference for every spec considered since the app's inception. Merges the original design (Docs/HARNESS_SPEC.md) with everything that has actually shipped, plus paths deliberately not taken.

**Three companion docs**:
- **[Docs/HARNESS_SPEC.md](Docs/HARNESS_SPEC.md)** — the original design spec (frozen reference; section numbers preserved here so you can cross-read).
- **[CLAUDE.md](CLAUDE.md)** — project context for Claude sessions (what's done / next likely / known gotchas).
- **This doc** — the superset: original intent, evolution, current surface, open questions, scrapped ideas.

---

## 1. Vision

### 1.1 Primary goals (unchanged since day 0)

1. **Run 1 Coach + 10 Players in parallel** on a single VPS, all using Claude Agent SDK authenticated via one Max-plan OAuth session. Shared billing; no `ANTHROPIC_API_KEY` path.
2. **Full transparency** — every agent's activity visible in UI; no hidden orchestration.
3. **Shared state** — common task board, common memory, direct inter-agent messaging.
4. **Access anywhere** — tiling multi-pane desktop UI, single-view mobile (mobile polish still pending).
5. **Disposable VPS** — nothing permanent on the server; durable state on Infomaniak kDrive via WebDAV.
6. **Easy deploy** — one repo, one service, auto-deploy on push (Zeabur → GitHub main).
7. **`/loop` friendly** — agents run semi-autonomously with bounded iteration caps; human interjections via inbox.

### 1.2 Explicit non-goals

- Multi-user / multi-tenant.
- API-key billing (the whole point is Max-plan sharing).
- Enterprise compliance features.
- Beating Anthropic's Agent Teams — this is specifically **more transparent and less automagical**.
- Persistent pause state — deliberately in-memory, restart resumes.
- Automatic `tasks.parent_id` cycle detection at the DB level (defensive `seen` guard in UI walk is enough).

---

## 2. The Team (11 agents, sports metaphor)

### 2.1 Roles

| ID | Kind | Name | Role | Default model |
|----|------|------|------|--------------|
| `coach` | Coach | fixed: "Coach" | Team captain — decomposes goals, assigns work, synthesizes progress | Sonnet (configurable) |
| `p1`…`p10` | Players | **assigned by Coach** (e.g. "Alice", "Ravi") | **assigned by Coach** (e.g. "Developer — writes code", "QA — runs tests") | Sonnet, per-pane override |

Player names and roles are written via `coord_set_player_role(player_id, name, role)` (Coach-only). Slots `p1`…`p10` are permanent; names/roles rotate with projects.

### 2.2 The rule: Coach orders, Players report

Two enforcement layers — **soft** (system prompts) and **hard** (structural).

Soft:
- Coach's system prompt: "you delegate, never implement".
- Player's: "you execute and report, you do not assign work to peers".

Hard:
- Only Coach can create top-level tasks. Players can create subtasks only (parent_id must be an owned task).
- Only Coach can call `coord_set_player_role` and `coord_write_decision`.
- Only Coach can call `coord_assign_task` (push-assign to a Player).
- Players are the only kind that can call `coord_commit_push`, `coord_claim_task`.
- Structural tool permissioning via `ALLOWED_COACH_TOOLS` vs `ALLOWED_PLAYER_TOOLS` in `server/tools.py`.

Messaging (`coord_send_message`) is open to both — Players can inform each other ("finished migration, FYI") but cannot assign work.

### 2.3 Why 10 Players specifically

Max-plan realistic concurrency is lower than 10 for continuous loops. The roster defines *potential* slots, not *always-on* agents. Coach activates the slots it needs — typically 2–5 concurrent during active work.

---

## 3. Tech stack (as deployed)

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Agent runtime | Claude Agent SDK (Python) | Programmatic control; Max-plan OAuth; native streaming |
| Backend | FastAPI + WebSocket + aiosqlite | Single-process, shared in-memory state |
| Frontend | Preact 10 + htm + Split.js (no build step) | ESM from esm.sh; zero build, fast load |
| UI state | Local `useState` + `useMemo` (no Zustand) | Simpler than originally spec'd |
| Hot-path DB | SQLite on `/data/harness.db`, DELETE journal mode | ACID; no race conditions; volume-friendly |
| Durable mirror | Infomaniak kDrive via WebDAV (webdav4 lib) | Swiss hosting; human-readable; no rclone daemon |
| Auth to Claude | `claude /login` device-code flow per host | OAuth tokens in OS credential store (NOT `~/.claude.json`) |
| Auth to UI | Bearer token (`HARNESS_TOKEN`), optional | Personal-use; token gate + localStorage paste UX |
| Deploy | Docker container on Zeabur, auto-pulled from GitHub `main` | No Caddy needed (Zeabur handles TLS) |

### 3.1 Deviations from the original spec

| Original | Shipped | Why |
|----------|---------|-----|
| React + react-mosaic + Vite | Preact + htm + Split.js | No build step; simpler |
| Zustand for state | Plain React hooks | Scope didn't justify a store |
| Docker Compose (app + Caddy) | Single Dockerfile on Zeabur | Zeabur owns ingress / TLS |
| `~/.claude.json` copy script | `claude /login` device-code per host | M-1 spike proved OAuth tokens aren't in that file |
| `curl https://claude.ai/install.sh` | `npm install -g @anthropic-ai/claude-code` | Zeabur EU region 403s the install.sh host |
| `workspaces/` gitignored plain dirs | Per-Player git worktrees under `/workspaces/<slot>/project/` on `work/<slot>` branches | M4 design |
| One-tier WebDAV state | Two-tier: SQLite hot + kDrive durable | WebDAV too slow/race-prone for 11 concurrent writers |
| Tailscale-only deploy | Public Zeabur deploy + optional bearer token | Switched preference pre-M0 |

---

## 4. Data model

All stored in SQLite. In-memory representations are plain dicts (we dropped the pydantic models from the original spec — SQLite rows + Pydantic request validators are enough).

### 4.1 Agent

Columns: `id`, `kind ∈ {coach, player}`, `name`, `role`, `brief`, `status ∈ {stopped, idle, working, waiting, error}`, `current_task_id`, `model` (default `claude-sonnet-4-6`), `workspace_path`, `session_id`, `cost_estimate_usd`, `started_at`, `last_heartbeat`.

11 seed rows on init (idempotent): `coach` + `p1`…`p10`.

**Display fields:** `name` (short label like `Rabil`) and `role` (one-line tag like `Frontend dev`) both surface in the pane header; Coach can set them with `coord_set_player_role` and the human via `PUT /api/agents/{id}/identity`. Players without a name get auto-picked a Men's Field Lacrosse surname on first spawn (pool of ~50: Rabil / Powell / Gait / Thompson / …) — fits the "team of ten" metaphor.

**Brief:** `brief` is a free-form multi-line text (≤ 8 KB) the *human* sets per-agent via the pane settings popover or `PUT /api/agents/{id}/brief`. Appended to every turn's system prompt after the governance-layer context, so edits take effect immediately with no restart. Distinct from `role` (short tag, Coach-writable).

**Schema migrations** are append-only in `init_db`: new columns get an `ALTER TABLE ADD COLUMN IF NOT EXISTS`-equivalent (try/except on "duplicate column") so existing deploys upgrade on next boot without data loss. The `brief` column was the first to ride this path.

### 4.2 Task

Columns: `id`, `title`, `description`, `status ∈ {open, claimed, in_progress, blocked, done, cancelled}`, `owner` (FK agents.id), `created_by` (`'human'` or agent id), `created_at`, `claimed_at`, `completed_at`, `parent_id` (FK tasks.id), `priority ∈ {low, normal, high, urgent}`, `tags` (JSON array), `artifacts` (JSON array).

Indexed on status / owner / parent_id.

### 4.3 Message

Columns: `id AUTOINC`, `from_id` (agent id or `'human'`), `to_id` (agent id or `'broadcast'`), `subject`, `body`, `sent_at`, `read_at` (legacy; unused after v0.4.1), `in_reply_to`, `priority ∈ {normal, interrupt}`.

### 4.4 Message reads (broadcast tracking)

Junction table `(message_id, agent_id)` with `read_at`. Needed because one broadcast has N recipients — a single `read_at` on the message itself would fire the first time any recipient drains.

### 4.5 Event

Columns: `id AUTOINC`, `ts`, `agent_id`, `type`, `payload` (JSON blob).

Append-only; the audit log. Full event-type enumeration lives in §7.2.

### 4.6 Memory doc

Columns: `topic PK`, `content`, `last_updated`, `last_updated_by`, `version`.

Topic validated by `MEMORY_TOPIC_RE = r"^[a-z0-9][a-z0-9\-]{0,63}$"` — prevents path traversal when the same topic names a file on kDrive.

### 4.7 Turns ledger

Columns: `id AUTOINC`, `agent_id`, `started_at`, `ended_at`, `duration_ms`, `cost_usd`, `session_id`, `num_turns` (SDK's internal tool-roundtrip counter), `stop_reason`, `is_error`, `model`, `plan_mode`, `effort`.

One row per `ResultMessage` via `_insert_turn_row`. Narrow + indexed on `(agent_id, id)` and `ended_at` so the daily-spend query that gates cost caps (`_today_spend`) is a single-index `SUM` instead of a JSON-extract scan of the events table.

Cancelled turns and turns that errored before producing a `ResultMessage` are NOT in this table — they're in events only. Which is to say: this is the "completed turn" ledger, not "attempted turn". Analytics on completion rate should pair with `error` / `agent_cancelled` events from the firehose.

### 4.8 Locks (deferred)

Originally spec'd; not implemented. Per-worktree isolation covers the primary need. If re-added later, filename: `/state/locks.json` on kDrive or a `locks` table.

---

## 5. Storage layout

Three tiers, by role: **live state** (fast, ephemeral), **durable backup** (kDrive, survives crashes), **code** (GitHub).

### 5.1 Live state — SQLite on `/data/harness.db`

Single source of truth for everything the agents read/write during a turn. All writes route through the harness process (§6.1). Covers: the roster, the task board, the mailbox, the shared notebook, and the firehose log.

### 5.2 Durable backup — kDrive (WebDAV)

Every layer of live state has a corresponding mirror on kDrive so a crashed server can restart from near-complete history.

| kDrive path | What | Cadence |
|---|---|---|
| `memory/<topic>.md` | Shared notebook | synchronous on every `coord_update_memory` |
| `decisions/<date>-<slug>.md` | Append-only architectural decisions | synchronous on every `coord_write_decision` (Coach-only) |
| `events/<YYYY-MM-DD>.jsonl` | Firehose log (one file per day) | every 5 min (`HARNESS_KDRIVE_FLUSH_INTERVAL`); 00:00–02:00 UTC re-flushes yesterday to catch boundary-straddling events |
| `snapshots/<ts>.db` | Full `VACUUM INTO` of the DB | **every 5 min** (`HARNESS_KDRIVE_SNAPSHOT_INTERVAL`). Retention `HARNESS_KDRIVE_SNAPSHOT_RETENTION` (default 144 = ~12 h of history) |
| `context/` | Governance docs — `CLAUDE.md`, `skills/*.md`, `rules/*.md`. Readable by all agents; writable only by Coach (via `coord_write_context`) and the user (via the UI). Loaded into every agent's system prompt at spawn. | synchronous on write |
| `knowledge/<path>.md` | Durable artifacts agents produce (reports, research, specs, designs). Free-for-all writes via normal Write tool into `/data/knowledge/`. | every 5 min mirror |

Context docs (CLAUDE.md / skills / rules) are **not** the same as the notebook: notebook is a free scratchpad anyone can overwrite; context is governance, restricted to Coach + human. The system prompt loader concatenates the relevant context files at turn start so changes take effect on the next turn.

The 5-min snapshot cadence (down from the prior hourly setting) guarantees worst-case crash loss ≤ 5 min for roster/tasks/mailbox/notebook state. Snapshots are single-digit KB at this scale so the bandwidth is trivial.

### 5.3 Code — GitHub

Project repositories do **not** mirror to kDrive. `HARNESS_PROJECT_REPO` points at a GitHub repo (with PAT in the URL). The server clones it to `/workspaces/.project` at boot and materializes a per-Player worktree at `/workspaces/<slot>/project` on branch `work/<slot>`. Players push via `coord_commit_push` — GitHub is their durability story.

### 5.4 Why not one-tier WebDAV

WebDAV is too slow and race-prone under 11 concurrent writers. Original spec intended `tasks.json` / `agents.json` / per-agent inboxes on kDrive directly; early design showed this failing under contention.

### 5.5 Memory is scratchpad (intentionally no history)

If history matters, the event log (`memory_updated` events) has the audit. Decisions is the append-only durable record; memory is the "living now" commons; context is the governance layer above both.

---

## 6. Coordination mechanics

### 6.1 Single write-handle discipline

All state mutations (tasks, messages, memory, events, agent status) route through the harness server process. The server holds the only SQLite write handle. Agents never open their own DB connection; they call MCP tools.

This gives:
- Clean event ordering (one serial stream, auditable).
- Trivial concurrency (no file locks, no optimistic-concurrency retry loops).
- Single place to publish events to the WS bus.

### 6.2 Per-worktree isolation > locks

File-level isolation comes from per-Player git worktrees: two Players editing the same file do so in isolated trees. Conflict surfaces at merge, a much cleaner failure mode than a held lock from a crashed agent.

`coord_acquire_lock` / `coord_release_lock` from the original spec are **not implemented**. They were to be advisory-only for cross-worktree logical resources ("only one Player runs the migration") — deferred until a real use-case appears.

### 6.3 Cost caps

Enforced **before** `agent_started` is emitted so a capped attempt doesn't count as a turn in the audit log.

- Per-agent daily: `HARNESS_AGENT_DAILY_CAP` (default $5.00). 0 disables.
- Team daily: `HARNESS_TEAM_DAILY_CAP` (default $20.00). 0 disables.
- Both computed by summing `cost_usd` from `result` events emitted today (UTC).
- Blocked spawns emit a `cost_capped` event with the reason string.

### 6.4 Global pause

`POST /api/pause {paused: bool}` flips an in-memory flag. When paused:
- `run_agent` emits `paused` and returns (cheaper than the cost-cap check; no DB write).
- Coach autoloop skips ticks.
- In-flight turns are **not** cancelled (use `cancel-all` for that).

Persists via `pause_toggled` WS event so multi-tab UIs stay in sync. Restart clears the flag.

### 6.5 Coach autonomous loop

Initial interval from `HARNESS_COACH_TICK_INTERVAL` (seconds; 0 disables). **Runtime-mutable** via `set_coach_interval()` — the background loop re-reads the interval each iteration, so changes take effect on the next tick with no restart.

- Sleep-first pattern — first tick doesn't fire before workspaces/DB are ready.
- When disabled, the loop idles with a 5 s poll until re-enabled.
- Skips when Coach is already working or harness is paused.
- Manually triggerable: `POST /api/coach/tick` (409 if Coach busy).
- Runtime control:
  - `GET /api/coach/loop` → `{interval_seconds}`
  - `POST /api/coach/loop` → `{interval_seconds: 0..86400}`
  - `/loop [N]` slash command in any pane — `/loop 60` enables 60s ticks, `/loop off` disables, `/loop` reports current state.
- Prompt: the `COACH_TICK_PROMPT` constant ("Routine tick. Read your inbox…").

### 6.6 Auto-wake on targeted events

Players don't periodically self-poll; they wake only on direct triggers. The `maybe_wake_agent(slot, reason, *, bypass_debounce)` helper spawns a turn for the target if guards pass:

- harness not paused
- agent not already running
- (unless `bypass_debounce=True`) `AUTOWAKE_DEBOUNCE_SECONDS` elapsed since the agent's last turn — prevents tight Coach↔Player chat loops

Hooks:
- `coord_assign_task`  → wakes assignee (`bypass_debounce=True` — discrete action, non-looping)
- `coord_send_message` → wakes direct recipient (debounce respected — chat can loop)
- `POST /api/messages` → wakes target (`bypass_debounce=True` — human doesn't auto-reply)
- Broadcasts do NOT wake (would stampede the team on announcements)

Wake prompt carries context: *"Coach just assigned you task t-X. Use coord_read_inbox + coord_list_tasks to see your work, claim what's yours, and start."*

Debounce env: `HARNESS_AUTOWAKE_DEBOUNCE` (default 10 s, in-memory only).

### 6.7 Stale-session auto-heal

When a stored `session_id` is rejected by the CLI (after a `/login` rotation or CLI upgrade), `run_agent` catches the `ProcessError`, clears the session id, emits `session_resume_failed`, and retries once without `resume=`. No manual pane-× click needed.

### 6.8 Task hierarchy

- Top-level task: Coach creates via `coord_create_task` (no parent_id), or human via `POST /api/tasks`.
- Subtask: Player creates under their current task via `coord_create_task`. Schema supports unlimited depth.
- Done/cancelled clears the owner's `current_task_id`.
- Cancel cascades only to the owner's pointer, NOT to subtasks (intentional — matches `coord_update_task` behavior).

---

## 7. Events (the audit log + live WS stream)

### 7.1 Mechanics

- `publish(event)` fans out to every live WS subscriber via `asyncio.Queue`, then fires `asyncio.create_task(_persist(event))` for SQLite.
- No backlog replay on subscribe (duplicate events would pollute the UI; UI reads `GET /api/events` for history).
- Heartbeat ping every 30 s of quiet over WS (type `ping`, filtered out of conversations client-side).
- Client watchdog: if no message for 60 s, force-close WS → reconnect. Bumps `wsAttempt` state to re-run the effect.

### 7.2 Event type catalogue

Agent lifecycle:
- `agent_started` (prompt, resumed_session)
- `text`
- `thinking` (content) — final consolidated thought block
- `text_delta` / `thinking_delta` (block_index, delta) — token-level streaming, **transient** (fans out to WS but skips SQLite persist + backlog); only emitted when `HARNESS_STREAM_TOKENS=true` because the flag crashes some CLI builds
- `tool_use` (id, name, input)
- `tool_result` (tool_use_id, content, is_error)
- `result` (duration_ms, cost_usd, session_id, is_error)
- `error` (error, cwd)
- `agent_stopped`
- `agent_cancelled`
- `cost_capped` (reason, prompt)
- `paused` (prompt) — spawn refused while paused
- `session_cleared`
- `session_resume_failed` (session_id, error) — precedes a retry without `resume=` when a stale session id is rejected
- `context_applied` (chars, brief_chars) — system-prompt suffix loaded (hidden from pane body; visible in EnvPane timeline for debugging)

Tasks:
- `task_created`
- `task_claimed`
- `task_assigned` (task_id, to)
- `task_updated` (task_id, old_status, new_status, note, **owner**) — owner field drives fan-out so Coach cancelling a Player's task shows up in the Player's pane

Coord / comms:
- `message_sent` (to, subject, body_preview, priority)
- `memory_updated` (topic, version, size)
- `decision_written` (title, filename, location, size)
- `context_updated` / `context_deleted` (kind, name, size)
- `knowledge_written` (path, size)
- `file_written` (root, path, size) — human-saved via `PUT /api/files/write/{root}`
- `brief_updated` (size)
- `commit_pushed` (sha, message, pushed, push_requested)
- `human_attention` (subject, body, urgency ∈ {normal, blocker})
- `player_assigned` (name, role, auto) — `auto: true` for the lacrosse-surname first-spawn pick, `false` for explicit Coach or human assignments
- `pause_toggled` (paused)
- `coach_loop_changed` (interval_seconds)

**Cross-pane fan-out.** The UI and `/api/events?agent=<slot>` both deliver the same event to multiple panes when an event carries a target:
- `message_sent.to` matches → delivered to sender + recipient (+ every agent for `broadcast`)
- `task_assigned.to` → delivered to assigner + assignee
- `task_updated.owner` → delivered to updater + owner

So p3 sees Coach's assignment land live in p3's pane without having to open Coach's pane too.

**Transient events** (`text_delta`, `thinking_delta`) are marked in `events._TRANSIENT_EVENT_TYPES` and skip both the in-memory backlog and the SQLite mirror. They exist only as live WS frames.

---

## 8. MCP `coord_` tools

All served by `build_coord_server(slot)` — the per-caller closure knows which agent invoked the tool, so no agent has to pass its own identity. Registered as in-process MCP tools on each SDK query.

| Tool | Coach | Player | Effect |
|------|:-----:|:------:|--------|
| `coord_list_tasks` | ✓ | ✓ | Filter by status / owner |
| `coord_create_task` | ✓ (top-level) | ✓ (subtasks only, must parent to owned task) | |
| `coord_claim_task` | — | ✓ | Claim open task; refuses if already owning one |
| `coord_update_task` | ✓ (any) | ✓ (own) | Valid transitions: open→claimed→in_progress→(blocked\|done\|cancelled). Clears owner's current_task_id on done/cancelled. |
| `coord_assign_task` | ✓ | — | Push-assign to a specific Player; emits `task_assigned` |
| `coord_send_message` | ✓ | ✓ | Recipients: coach / p1..p10 / broadcast; priority: normal / interrupt |
| `coord_read_inbox` | ✓ | ✓ | Marks messages read per-recipient (broadcast-safe via `message_reads`) |
| `coord_list_memory` | ✓ | ✓ | List topics |
| `coord_read_memory` | ✓ | ✓ | Read content |
| `coord_update_memory` | ✓ | ✓ | Full overwrite; mirrors to kDrive; emits `memory_updated` |
| `coord_commit_push` | — | ✓ | `git add -A && commit && push origin HEAD` in the Player's worktree |
| `coord_write_decision` | ✓ | — | Append-only markdown in `decisions/<date>-<slug>.md` |
| `coord_write_context` | ✓ | — | Governance docs (`CLAUDE.md`, `skills/*.md`, `rules/*.md`); injected into every agent's system prompt on their next turn. |
| `coord_write_knowledge` | ✓ | ✓ | Durable artifact bucket. Free-form paths under `knowledge/` (≤ 4 segments, .md or .txt, ≤ 100 KB). Mirrored to kDrive. |
| `coord_read_knowledge` | ✓ | ✓ | Read any knowledge doc by path. |
| `coord_list_knowledge` | ✓ | ✓ | List every knowledge doc (local disk scan). |
| `coord_set_player_role` | ✓ | — | Write agents.name / role; emits `player_assigned` |
| `coord_request_human` | ✓ | ✓ | Emit `human_attention` event; urgency ∈ {normal, blocker} |

### 8.1 Standard SDK tools

- **Both kinds**: `Read`, `Grep`, `Glob`, `ToolSearch`.
- **Players only**: `Write`, `Edit`, `Bash`.

Coach structurally cannot modify code — enforces "you delegate, never implement".

### 8.2 Dropped from original spec

- `coord_acquire_lock` / `coord_release_lock` — deferred; per-worktree isolation covers primary need.
- `coord_heartbeat` — SDK `ResultMessage` already updates heartbeat via `_set_status`.

### 8.3 External MCP servers

Optional. `HARNESS_MCP_CONFIG` points at a JSON file:

```json
{
  "servers": {
    "github": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}
    },
    "notion": {
      "type": "http",
      "url": "https://mcp.notion.com/sse",
      "headers": {"Authorization": "Bearer ${NOTION_TOKEN}"}
    }
  },
  "allowed_tools": {
    "github": ["create_issue", "list_issues", "search_repositories"],
    "notion": ["search_pages"]
  }
}
```

Behavior (`server/mcp_config.py`):
- `${VAR}` placeholders expand from `os.environ` at load; missing vars log + expand to empty string.
- `allowed_tools` is **explicit** (no auto-discovery) — each entry becomes a fully-qualified `mcp__<server>__<tool>` added to the agent's `allowed_tools`. Lists a server without any allowed tools → the server is loaded but unusable (future expansion).
- Re-read on every spawn, so edits take effect on the next turn with no restart.
- Failures (missing file / parse error / bad shape) are logged and treated as "no external servers" — the harness keeps working with just `coord`.
- Both Coach and Players get the same external tools (no role split at this layer).

---

## 9. Agent lifecycle

### 9.1 Per-turn flow (`run_agent`)

1. **Paused check** — if paused, emit `paused` and return (no DB writes).
2. **Cost cap check** — per-agent and team daily. Blocked attempts emit `cost_capped`.
3. **Session load** — read prior `session_id` from agents table (for SDK resume).
4. Emit `agent_started` with `resumed_session: bool`. Set status `working`.
5. Build `ClaudeAgentOptions`:
   - `system_prompt` — role-specific (Coach vs Player template).
   - `cwd` — `/workspaces/<slot>`.
   - `max_turns=10`.
   - `mcp_servers={"coord": coord_server}`.
   - `allowed_tools` — ALLOWED_COACH_TOOLS or ALLOWED_PLAYER_TOOLS.
   - `model` — per-pane override if set.
   - `permission_mode="plan"` — if plan_mode override set.
   - `effort` — if effort override set (1..4 → "low"/"medium"/"high"/"max").
   - `resume=<session_id>` — if non-null.
6. Register task in `_running_tasks[slot]` so `POST /api/agents/<id>/cancel` can abort.
7. Stream SDK messages:
   - `AssistantMessage` → emit `text` / `tool_use` events.
   - `UserMessage` (carries tool results) → emit `tool_result`.
   - `ResultMessage` → emit `result`, persist session_id, add to cost_estimate_usd.
8. Exception handling:
   - `asyncio.CancelledError` → emit `agent_cancelled`, set status idle, re-raise.
   - Other → emit `error`, set status error.
   - Else → set status idle.
9. Always emit `agent_stopped` and pop from `_running_tasks`.

### 9.2 Per-turn overrides (via `POST /api/agents/start`)

| Field | Type | Maps to |
|-------|------|---------|
| `model` | str | SDK `model` |
| `plan_mode` | bool | SDK `permission_mode="plan"` |
| `effort` | 1..4 | SDK `effort` literal |

All optional; stored per-pane in localStorage (`harness_pane_settings_v1`).

### 9.3 Cancellation

- `POST /api/agents/<id>/cancel` — 409 if agent isn't running.
- `POST /api/agents/cancel-all` — iterates every in-flight task. Registered **before** the path-param version so "cancel-all" doesn't match as an agent_id.
- Cancellation propagates via `task.cancel()` → `CancelledError` in the SDK query loop → run_agent's except branch cleanly ends.

### 9.4 Session resume (M5 step 2)

- `ResultMessage.session_id` captured and persisted per-turn.
- Next turn reads it, passes as `resume=<id>` to `ClaudeAgentOptions`.
- `DELETE /api/agents/<id>/session` clears the stored id — next run starts fresh. UI exposes this via a × next to the ● session indicator in the pane header.

### 9.5 Crash recovery

- On restart, `init_db()` is idempotent — schema + seed agents restore cleanly.
- `crash_recover()` runs right after `init_db` in lifespan and resets zombie state left behind by an unclean shutdown:
  - `agents.status ∈ {working, waiting}` → `idle`
  - `tasks.status = 'in_progress'` → `claimed` (owner preserved so the Player knows what they were doing when next woken)
- 5-min `VACUUM INTO` snapshots (see §5.2) give fresh-VPS recovery: copy the latest `/harness/snapshots/*.db` back into `/data/harness.db`, boot, and the auto-wake / autoloop paths pick up in-flight work.

---

## 10. HTTP API

All paths under `/api/*` except `/api/health` require `Authorization: Bearer $HARNESS_TOKEN` when the env var is set. WS uses `?token=` query param.

### 10.1 Readiness & observability

- `GET /api/health` (public) — per-subsystem probe: db / static / claude_cli / kdrive / workspaces. 200 when all required green; 503 otherwise. Body always carries `{checks: {...}}`.
- `GET /api/status` — version, uptime, host, `paused`, `running_slots`, `ws_subscribers`, `caps` (per-agent / team / today), kdrive status, workspaces status.

### 10.2 Agents

- `GET /api/agents`
- `POST /api/agents/start` `{agent_id, prompt, model?, plan_mode?, effort?}`
- `POST /api/agents/cancel-all`
- `POST /api/agents/{id}/cancel` (409 if not running)
- `DELETE /api/agents/{id}/session`

### 10.3 Pause

- `GET /api/pause` → `{paused: bool}`
- `POST /api/pause` `{paused: bool}` → emits `pause_toggled` on transition

### 10.4 Tasks

- `GET /api/tasks`
- `POST /api/tasks` `{title, description?, priority?, parent_id?}` (created_by='human')
- `POST /api/tasks/{id}/cancel` (idempotent; clears owner's current_task_id)

### 10.5 Messages

- `GET /api/messages?limit=N` — recent (≤ 200) newest-first
- `POST /api/messages` `{to, subject?, body, priority?}` — queues from human; emits `message_sent`

### 10.6 Memory

- `GET /api/memory` — topic index
- `GET /api/memory/{topic}` — full content
- `POST /api/memory` `{topic, content}` — human upsert; emits `memory_updated`

### 10.7 Decisions

- `GET /api/decisions`
- `GET /api/decisions/{filename}`

### 10.8 Events

- `GET /api/events?agent=&type=&since_id=&limit=` — history for pane restore or filtered view

### 10.9 Coach tick + autoloop control

- `POST /api/coach/tick` — fires a Coach drain; 409 if Coach is busy
- `GET  /api/coach/loop` — `{interval_seconds}`
- `POST /api/coach/loop` `{interval_seconds: 0..86400}` — runtime-mutable Coach autoloop cadence; 0 disables. Emits `coach_loop_changed`.

### 10.10 Attachments

- `POST /api/attachments` (multipart) — pasted images → `/data/attachments`; each workspace has a symlink `/workspaces/<slot>/attachments/` pointing there

### 10.11 Context (governance docs)

- `GET  /api/context` — `{"root": ["CLAUDE"]?, "skills": [...], "rules": [...]}` (local ∪ kDrive)
- `GET  /api/context/{kind}/{name}` — `{kind, name, body, size}`
- `POST /api/context` `{kind: 'root'|'skills'|'rules', name, body}` — human upsert; emits `context_updated`
- `DELETE /api/context/{kind}/{name}` — remove; emits `context_deleted`

### 10.12 Files

- `GET  /api/files/roots` — whitelist: `[{key, writable, exists, label}]` for `context`, `knowledge`, `decisions`
- `GET  /api/files/tree/{root}` — recursive tree (dirs before files, case-insensitive), noise-dirs (`.git`, `__pycache__`, …) hidden
- `GET  /api/files/read/{root}?path=` — UTF-8 text, 256 KB cap
- `PUT  /api/files/write/{root}?path=` `{content}` — routes context→ctxmod, knowledge→disk, decisions refused

### 10.13 Agent identity + brief

- `PUT /api/agents/{id}/identity` `{name?, role?}` — upsert name / role; empty string clears. Emits `player_assigned` with `auto: false`.
- `PUT /api/agents/{id}/brief` `{brief}` — upsert the multi-line context string (≤ 8 KB). Emits `brief_updated`.

### 10.14 Turns ledger

One row per SDK `ResultMessage` — see §4.8. Indexed on `(agent_id, id)` and `ended_at`; backs the cost-cap check (`_today_spend`) and any analytics dashboards.

- `GET /api/turns?agent=&since_id=&limit=` — row-level detail, newest first (up to 1000)
- `GET /api/turns/summary?hours=N` — per-agent aggregate over a rolling window (1h..30d, default 24h):
  ```
  { window_hours: 24,
    since: "2026-04-22T…",
    total_turns: 45,
    total_cost_usd: 1.23,
    per_agent: [{agent_id, count, cost_usd, avg_duration_ms, error_count}, …] }
  ```

---

## 11. WebSocket `/ws`

- Single connection at app-root.
- Auth: `?token=<HARNESS_TOKEN>` query (browsers can't set Authorization on WS).
- Server→client:
  - `{"type": "connected"}` on accept.
  - `{"type": "ping"}` every 30 s of quiet.
  - All domain events (same envelope shape as persisted event rows).
- Client doesn't send — subscription is implicit (every subscriber gets every event).
- Client watchdog: see §7.1.

---

## 12. UI

### 12.1 Global layout

- **Left rail** (44 px, vertical, top→bottom):
  - WS dot (pulses red when disconnected)
  - Coach `C` + `1`–`10` Player slot buttons (see §12.1a for state colors)
  - separator
  - 📁 Files pane toggle (opens the file-browser as a special slot `__files`)
  - separator
  - layout presets (spread 3-rectangle icon · pair-stack 3-box icon), shown only when ≥ 2 panes open
  - ⏹ cancel-all (shown only when agents are working)
  - ❚❚/▶ pause toggle
  - ▦ env-panel toggle
  - ⚙ settings gear
- **Panes area** (center, flex): 2D layout — array of columns, each a vertical stack of panes. Split.js horizontal gutters between columns; vertical gutters inside stacked columns (see §12.2 for drop edges). Includes both `AgentPane`s and the `__files` `FilesPane`.
- **Env pane** (right, 340 px, toggleable via ⌘/Ctrl+B): Attention / Tasks / Cost / Inbox / Memory / Decisions / Timeline sections. ↓ team-export button in header.

### 12.1a Slot state palette (LeftRail)

One visual language across every slot: full-color text + 1 px border + translucent tinted fill (CSS custom properties switch the hue per state). Five states:

| State | Class | Hue | Trigger |
|-------|-------|-----|---------|
| inactive | (none) | muted gray | `status='stopped'` AND no `session_id` |
| active | `.slot.active` | accent blue | `session_id` is set, or `status ∈ {working, waiting}` |
| working | `.slot.working` | warn amber (pulses) | `status='working'` |
| error | `.slot.error` | err red (outline only) | `status='error'` |
| Coach | `.slot.coach` | ok green | Always on for Coach; beats blue/gray. Working amber and error-red-ring override. |

**Pane-open** is a separate affordance — small fg-colored dot at top-left of the slot button, layered on top of whichever state color is in play. Closing the pane does NOT change the state color — you can see which agents are running from the rail even with every pane closed.

### 12.2 Slot interactions

- Click closed slot → open as new column on the right.
- Click already-open slot → scroll pane into view horizontally.
- Shift-click → stack into rightmost column.
- Drag pane header → drop on another pane. Which of four **edge zones** the cursor is in when you release picks the effect:
  - **top / bottom edge band** (middle half horizontally, top or bottom vertical third) → stack before / after the target in its column
  - **left / right edge band** (≤ 22 % into the target) → new column inserted to the left / right of target's column
  - The target pane shows a bright accent bar along whichever edge will be used, so the outcome is visible before release.
- Layout preset buttons (left rail) reshape every open pane in one click: **spread** (one pane per column) or **pair-stack** (two panes per column; odd count → first pane solo).
- Pane **⇱ pop-out** button (header, visible only when stacked): move the pane to its own new column at the right edge — fastest "undo stacking" without drag.

### 12.3 Persistence (localStorage)

| Key | Purpose |
|-----|---------|
| `harness_layout_v1` | openColumns + envOpen |
| `harness_split_sizes_v1` | Split.js sizes per layout signature (capped 30 keys) |
| `harness_pane_settings_v1` | per-slot model / plan_mode / effort |
| `harness_prompt_history_v1` | per-slot last 40 submitted prompts |
| `harness_task_filter_v1` | active / all / done |
| `harness_attention_dismissed_v1` | dismissed human_attention event ids (capped 200) |

### 12.4 Keyboard shortcuts

Global (ignored in form fields):
- **⌘/Ctrl+B** — toggle env panel.
- **⌘/Ctrl+.** — toggle pause.

Inside a pane input:
- **⌘/Ctrl+Enter** — send.
- **⌘/Ctrl+↑/↓** — cycle prompt history.
- **Escape** (in search / settings popover) — close.
- Paste image → `/api/attachments` → adds path to prompt.

### 12.5 Tab title

Composed from live state: `[⏸] [N⚡] [M●] TeamOfTen` where ⏸=paused, N⚡=working count, M●=unread slots.

### 12.6 Pane header (left to right)

- Status dot (idle / working / error / stopped) — tooltip: status + last heartbeat + first-started (relative times).
- Slot id · displayed name · role. The name pill is hidden when it case-insensitively equals the slot id (avoids "coach Coach" stutter).
- ⚑ Current-task chip (title truncated to 24 chars; full title in tooltip).
- ● session id indicator + × to clear. Clear also happens auto-magically if the stored id is rejected on the next turn (see §6.7).
- `$X.XXX` cumulative cost chip.
- `Ns · $X.XXX` last-turn duration + cost (hidden while working).
- ⏹ Cancel (only visible when status=working).
- Override dot (accent color) when any per-turn setting is non-default.
- ⌕ In-pane search toggle (filters body by substring match; Escape clears).
- ↓ Export conversation as markdown.
- ⇱ Pop-out (visible only when stacked).
- ⚙ Settings popover (name / role / save · model / plan_mode / effort · brief textarea + save).
- × Close pane.

### 12.7 Pane body

- Loads up to 500 events on mount from `/api/events?agent=<slot>&limit=500`. Server-side fan-out means a pane also sees inbound messages, task assignments TO this slot, and task updates on tasks this slot owns — even though those events' `agent_id` is Coach.
- Merges persisted + live WS events, deduped by `__id`.
- Pairs `tool_use` ↔ `tool_result` → renders `tool_result` INSIDE its `tool_use` card.
- `agent_started` events render as **sticky turn headers** — `position: sticky; top: 0` one-line bars showing the prompt of the turn you're currently reading. Scroll past and the next turn's header pushes it off; click any header to expand the full prompt under a dashed divider.
- Auto-scroll:
  - Normal (between turns): stick to bottom only if user was within 80 px of it.
  - While a turn is actively streaming tokens: forced stick-to-bottom so partial text stays visible.
- Streaming renders (when `HARNESS_STREAM_TOKENS=true`): `text_delta` accumulates into a dashed-border bubble with a blinking amber caret; replaced by the authoritative `text` event when the block completes.
- `thinking` events render as a collapsed card (`💭 thought · N lines`); click to expand the italic body.
- Noise filters:
  - `context_applied` is **hidden** from pane body (still in DB + EnvPane timeline).
  - 15+ system event types (`task_claimed`, `task_updated`, `memory_updated`, `knowledge_written`, `decision_written`, `context_updated`, `file_written`, `player_assigned`, `session_cleared`, `session_resume_failed`, `commit_pushed`, `coach_loop_changed`, `human_attention`, `paused`/`pause_toggled`, `cost_capped`, `agent_cancelled`, `brief_updated`) render as single-line muted `.sys` rows — no JSON blobs. Unknown types still hit the JSON fallback as a debug escape hatch.
- Per-tool renderers (`tools.js`):
  - Read — image preview for image paths.
  - Edit — red/green diff card.
  - Bash / Grep / Glob — inline output summary.
  - `coord_*` tools — structured input + result display.
  - Generic fallback for unknown tools.
- Empty-pane hint cards with starter prompts (Coach-specific vs Player-specific).

### 12.7a Input area

Right above the textarea:
- **Mode chips** — `Model` / `plan ✓` / `effort` badges showing current per-pane settings. Accent-tinted when non-default. Model + effort open the settings popover on click; plan toggles inline. A `/ commands` chip on the right seeds the input with `/`.
- **Slash menu** — typing `/` as the first char of a single-line input pops a floating autocomplete above the textarea. Arrow keys navigate, **Tab** completes into input, **Enter** runs, **Escape** clears.
- Registered slash commands (all intercepted locally; the agent never sees them):
  - `/plan` — toggle plan mode
  - `/model` — open model picker
  - `/effort [1-4]` — set effort; `/effort 3` sets inline, bare `/effort` opens the popover
  - `/brief` — open brief editor (in the settings popover)
  - `/tools` — list this agent's allowed tool names in a dismissable info banner
  - `/clear` — hit `DELETE /api/agents/<slot>/session`
  - `/loop [N|off]` — set Coach autoloop interval; bare `/loop` reports current state
  - `/help` — list every slash command
- Info banner — `/tools` and `/help` render into a dismissable `.pane-info` panel above the input. `×` to clear.
- Keyboard: **⌘/Ctrl+Enter** send, **⌘/Ctrl+↑/↓** cycle prompt history, **Esc** closes search/menu, paste image → `/api/attachments`.

### 12.7b Files pane (slot id `__files`)

Special non-agent pane type. Opens in a new column when the 📁 left-rail icon is clicked; participates in the mosaic (drag, stack, pop-out) like any agent pane.

- Left side: root selector chips (`context` / `knowledge` / `decisions` — read-only shows 🔒), then a fully expand/collapse-able tree.
- Right side: editor — `.md` files default to a rendered preview with `edit`/`preview` toggle; `.txt` opens directly as textarea. Read-only roots hide the save button.
- `⌘/Ctrl+S` saves. Dirty indicator (`*` in header + "unsaved" in footer) when draft differs from loaded content.
- Writes route through the owning module: context edits go via `ctxmod.write` (kDrive mirror + cache bust); knowledge edits go to plain disk with the `knowmod.MAX_BODY_CHARS` cap; decisions are not editable. Emits `file_written` on save.

### 12.8 EnvPane sections

1. **Attention** (pinned banner when any undismissed `human_attention` exists) — loads persisted + live events; dismissed set in localStorage keyed by `__id`.
2. **Tasks** — active/all/done filter (persisted). Subtasks indented with ↳. × cancel button per active row. Bottom form creates top-level task as 'human'.
3. **Cost** — per-agent list sorted by spend, total, caps display.
4. **Inbox** — 50 most recent messages. Click to expand full body. `+ send` opens composer (to / subject / body / priority).
5. **Memory** — topic list with version + last_updated_by. Click to expand. `+ write` opens composer.
6. **Decisions** — append-only list. Click to expand full markdown.
7. **Timeline** — flat chronological stream of overview-worthy event types (capped last 80, sticky-to-bottom).

### 12.9 Settings drawer

- **Health** section: runtime strip (paused / running / ws subs) + per-subsystem dots + ↻ refresh.
- **Authentication**: copy for the `claude /login` device-code flow.
- **Cost caps**: read-only display (edit via env vars).
- **kDrive mirror**: enabled/disabled + reason.
- **Layout**: "Reset resize state" clears `harness_split_sizes_v1` and reloads.
- **About**: shortcut reference.

---

## 13. Security & auth

### 13.1 To Claude

`claude /login` device-code flow, run **once per host**:
1. On the VPS, run `claude` (interactive REPL).
2. At `>` prompt, type `/login`.
3. CLI prints URL + short code.
4. Open URL on laptop, sign in to Max account, enter code, approve.
5. Token persisted to OS credential store (NOT `~/.claude.json`).
6. Exit REPL; non-interactive `claude -p "…"` works from any shell on that host.

**Implications**:
- Redeploys erase the container filesystem — token must sit on a mounted volume or every redeploy re-requires `/login`.
- The install script `https://claude.ai/install.sh` is geo-blocked in Zeabur's EU datacenter (403). Dockerfile installs via `npm install -g @anthropic-ai/claude-code` — `registry.npmjs.org` is reachable, and `api.anthropic.com` is not blocked at runtime.

### 13.2 To the UI

- `HARNESS_TOKEN` env var, optional.
- When set: every `/api/*` except `/api/health` requires `Authorization: Bearer <token>`; WS uses `?token=`.
- UI shows a TokenGate overlay on 401 (stores in localStorage, reload).
- Unset env = open API (single-user personal harness, no public exposure intended).

### 13.3 kDrive

- `KDRIVE_WEBDAV_URL` + `KDRIVE_USER` + `KDRIVE_APP_PASSWORD`.
- URL points directly at the folder the harness owns on kDrive (e.g. `.../TOT`). Files land right under it — no separate prefix setting.
- App-specific password from Infomaniak panel (NOT main password).
- All three checked on boot; enabled only if all three present.

---

## 14. Deployment

### 14.1 Dockerfile

- Base: `python:3.12-slim`.
- Adds: Node 20 (NodeSource), `npm install -g @anthropic-ai/claude-code`, git.
- Default git identity: `"TeamOfTen Harness" <harness@teamoften.local>`.
- Pre-creates `/workspaces/{coach,p1..p10,default}` with `/attachments/` symlinks.
- Does NOT pre-create `/data` — Zeabur's volume mount over an existing directory hangs SQLite silently (confirmed 2026-04-22).

### 14.2 Zeabur

- GitHub auto-pull from `main`.
- Volume mount at `/data` for SQLite persistence.
- Env vars set in service panel.
- TLS / ingress handled by Zeabur — no Caddy needed.

### 14.3 Env vars (complete table)

| Var | Default | Purpose |
|-----|--------:|---------|
| `HARNESS_TOKEN` | unset | Bearer token for /api/*. Unset = open. |
| `HARNESS_DB_PATH` | `/data/harness.db` | SQLite path |
| `HARNESS_AGENT_DAILY_CAP` | `5.0` | USD per agent per day. 0 = unlimited. |
| `HARNESS_TEAM_DAILY_CAP` | `20.0` | USD across team per day. 0 = unlimited. |
| `HARNESS_COACH_TICK_INTERVAL` | `0` | seconds between Coach autoloop ticks. 0 disables. |
| `HARNESS_KDRIVE_FLUSH_INTERVAL` | `300` | seconds between event-log flushes |
| `HARNESS_KDRIVE_SNAPSHOT_INTERVAL` | `300` | seconds between DB snapshots (was hourly; 5 min closes crash-loss window to ≤ 5 min) |
| `HARNESS_KDRIVE_SNAPSHOT_RETENTION` | `144` | snapshots retained on kDrive (~12 h at 5 min cadence) |
| `HARNESS_EVENTS_RETENTION_DAYS` | `30` | days of events kept in SQLite (kDrive JSONL keeps the full archive); 0 disables trimming |
| `HARNESS_EVENTS_TRIM_INTERVAL` | `86400` | seconds between event-trim passes |
| `HARNESS_ATTACHMENTS_RETENTION_DAYS` | `30` | days of pasted-image files kept on `/data/attachments`; 0 disables trimming |
| `HARNESS_ATTACHMENTS_DIR` | `/data/attachments` | where `POST /api/attachments` writes pasted images |
| `HARNESS_CONTEXT_DIR` | `/data/context` | local cache of kDrive `context/` (CLAUDE.md, skills, rules) |
| `HARNESS_KNOWLEDGE_DIR` | `/data/knowledge` | agents write durable artifacts here; mirrored to kDrive `knowledge/` |
| `HARNESS_DECISIONS_DIR` | `/data/decisions` | local fallback when kDrive disabled |
| `HARNESS_AUTOWAKE_DEBOUNCE` | `10` | seconds before an auto-woken agent can be re-woken (chat ping-pong guard) |
| `HARNESS_STREAM_TOKENS` | unset (off) | set `true` to enable `include_partial_messages` — token + thinking deltas over WS. Some CLI builds crash on the underlying flag; off by default |
| `HARNESS_MCP_CONFIG` | unset | path to a JSON file defining external MCP servers (see §8.3) |
| `HARNESS_PROJECT_REPO` | unset | If set, clones to `/workspaces/.project` + creates per-slot worktrees at boot |
| `HARNESS_PROJECT_BRANCH` | `main` | default branch for fresh worktrees |
| `CLAUDE_CONFIG_DIR` | `/data/claude` | Claude CLI credentials dir. Baked into the Dockerfile so OAuth (`~/.claude/.credentials.json`) persists on the `/data` volume across Zeabur redeploys. |
| `KDRIVE_WEBDAV_URL` | unset | Full Infomaniak WebDAV URL, including the target folder (e.g. `.../TOT`). Files land directly under this URL — there's no separate prefix setting. |
| `KDRIVE_USER` | unset | Infomaniak email |
| `KDRIVE_APP_PASSWORD` | unset | Infomaniak app-specific password |

---

## 15. Test suite

Under `server/tests/`, pytest-asyncio auto mode. Most tests are DB-level (exercise the schema + helpers directly without the FastAPI TestClient). A handful import `server.agents` — OK in CI because `uv sync --extra dev` installs `claude-agent-sdk` as a prod dep; tests never actually spawn a subprocess.

- `test_db.py` — schema smoke, 11-agent seed, idempotent init. (3)
- `test_events.py` — bus publish/subscribe/persist round-trip; late-subscriber has empty backlog (invariant). (3)
- `test_tools_consts.py` — VALID_RECIPIENTS shape, MEMORY_TOPIC_RE, coord-tool name prefix, Coach/Player allowlist split. (7)
- `test_tasks_sm.py` — status default, CHECK constraints, cancel clears owner. (4)
- `test_context.py` — governance docs: validate, write/read/delete, size + empty-body rejection, TTL cache. (15)
- `test_knowledge.py` — artifact bucket: traversal guards, extension check, depth limit, list_paths. (13)
- `test_files.py` — files-browser backend: _resolve safety, tree walk, write routing. (14)
- `test_mcp_config.py` — external MCP loader: skip paths, interpolation, validation. (13)
- `test_turns.py` — turns ledger: schema, insert, defaults, indexes. (4)
- `test_crash_recover.py` — boot reset of working/waiting agents + in_progress tasks. (4)
- `test_retention.py` — trim_events_once + trim_attachments_once cutoff + disabled cases. (7)
- `test_autoname.py` — lacrosse pool pick, no-op on already-named, race-safe under concurrent gather. (5)
- `test_agents_helpers.py` — _today_spend, _get_agent_brief, _clear_session_id. (8)
- `test_concurrent_spawn_guard.py` — spawn_rejected fires when _running_tasks has a live task. (2)

**Total: 106 tests.** CI (`.github/workflows/tests.yml`) runs the suite on every push + PR.

Local: `uv sync --extra dev && uv run pytest -ra --strict-markers`.

---

## 16. Milestone status

Numbered per the original spec (Docs/HARNESS_SPEC.md §14).

| Milestone | Scope | Status |
|-----------|-------|--------|
| **M-1** — Max OAuth feasibility spike | 3–5 parallel `query()` on Max plan | ✓ (2026-04-22) |
| **M0** — Bones | FastAPI skeleton, Dockerfile, Zeabur auto-deploy | ✓ |
| **M1** — One agent | Single SDK agent streaming to WS UI | ✓ |
| **M2a** — State + roster | SQLite + 11-agent roster + first coord_* tools | ✓ |
| **M2b** — Task state machine | claim / update / done transitions | ✓ |
| **M2c** — Inter-agent chat | send_message / read_inbox / per-recipient unread tracking | ✓ |
| **M2d** — Shared memory | list/read/update memory tools | ✓ |
| **M2e** — Cost caps | per-agent + team daily, pre-spawn enforced | ✓ |
| **M3/1** — kDrive memory mirror | synchronous on update | ✓ |
| **M3/2** — Event log flush | 5-min + yesterday-replay at day boundary | ✓ |
| **M3/3** — DB snapshots | hourly VACUUM INTO + retention | ✓ |
| **M4/1** — Git in container | git installed + default identity | ✓ |
| **M4/2** — Per-slot worktrees | clone + worktree materialization on boot | ✓ |
| **M4/3** — `coord_commit_push` | Player-only; emits commit_pushed | ✓ |
| **M5/1** — session_id capture | persist per-turn | ✓ |
| **M5/2** — session_id resume | pass as SDK `resume=` kwarg | ✓ |
| **v2 (a–d)** — Preact frontend rewrite | slim rail, tileable panes, per-tool renderers, image paste, EnvPane, SettingsDrawer | ✓ |
| **Auth** — `HARNESS_TOKEN` bearer gate | opt-in via env | ✓ |
| **/api/health** — per-subsystem probe | cached, public | ✓ |
| **M6/7/8/9** — Polish | 2D column stacking, drag/drop, pane export, team export, drawer health section, pane collapse alternatives (size persistence), in-pane search, prompt history, cancel/cancel-all, pause, team composition (`coord_set_player_role`), attention UI, inbox composer, memory composer, task hierarchy render, task cancel, filter, last-turn chip, unread dot, scroll-into-view, tab title, WS heartbeat | ✓ (incremental) |

**Pending / next likely:**
- **Data backup upgrade (§5.2)** — drop snapshot interval from hourly to 5 min; bump retention to 144. Closes crash-loss window to ≤ 5 min. One-line config change.
- **Context folder** (§5.2, `context/`) — `CLAUDE.md` / `skills/*.md` / `rules/*.md` loaded into every agent's system prompt. New Coach-only MCP tool `coord_write_context`, UI editor for the human, synchronous kDrive mirror on write. Not yet implemented.
- **Knowledge folder** (§5.2, `knowledge/`) — durable artifact bucket for agent-produced markdown. Local dir at `/data/knowledge/`, mirrored to kDrive every 5 min. UI browser pane. Not yet implemented.
- **Live-streaming tokens + thinking blocks** — shipped in the UI (pane renders partial text with blinking caret; thinking shown as collapsible card).
- **Sticky turn headers** in the pane body — shipped (each `agent_started` is a position:sticky one-liner that always shows the prompt of the turn currently on screen).
- Mobile UI polish (HTML5 DnD doesn't fire on touch; layout breakpoints for < 900 px need a rethink).
- Pane collapse (minimize to header) — deferred because fighting Split.js's inline sizes is non-trivial.
- Automated crash-recovery (reset in_progress tasks on restart) — wait for real incidents before prioritizing.
- Daily/weekly digest generation by Coach — needs the autoloop running regularly first.

---

## 17. Open questions (from original spec + new)

### 17.1 From the original spec (§15)

1. **Opus for Coach?** — Coach does more reasoning, less typing. Pane settings popover supports per-turn override, so de facto answer is "yes, when needed". No automatic default.
2. **Weekly Max-plan usage cap?** — Not implemented. Per-agent + team daily is the current bound.
3. **Agent self-termination on task done?** — Currently each turn ends after one pass through the SDK query loop; agent doesn't re-loop without a new prompt. Coach autoloop handles "wake up periodically".
4. **Multi-repo support?** — One HARNESS_PROJECT_REPO; no routing for multiple. Extensible but not built.
5. **Record/replay?** — Event log enables replay; no replay tooling built.
6. **Conflict detection across worktrees?** — None yet; merge-at-the-end is the fallback.
7. **Hard cost caps vs soft warnings?** — Current caps are HARD (reject spawn). No soft-warn mode.

### 17.2 New, from shipping

8. **Daily digest generation** — spec'd but not written. Coach autoloop is the hook; a `coord_write_digest` tool + scheduled UTC-00:00 tick would do it.
9. **Session_id rotation policy** — right now sessions can live forever (until user clicks × on the header dot). Auto-rotate after N turns?
10. **Memory deletion** — intentionally no delete tool. If noise accumulates, user edits content to empty string or overwrites the topic.
11. **Broadcast message delivery ordering** — message_reads tracks per-recipient, but delivery to inboxes is whatever order `read_inbox` fires in. Acceptable.
12. **Agent crash in the middle of tool call** — run_agent's try/except catches it, emits `error`. The tool call may have partially committed (e.g. task updated but message not sent). Best-effort; no two-phase commit.

---

## 18. Abandoned / not-taken decisions

- **rclone daemon** — dropped in favor of direct WebDAV via webdav4. No daemon to supervise; clean sync control.
- **React + react-mosaic + Vite** — dropped in favor of Preact + htm + Split.js (no build step).
- **Zustand** — dropped; plain hooks are enough.
- **Docker Compose with Caddy** — dropped; Zeabur handles TLS / ingress.
- **Copying `~/.claude.json` across hosts** — dropped per M-1 spike; tokens live in OS credential store.
- **`scripts/copy-claude-auth.sh`** — deleted from the plan.
- **`tailscale-only` public exposure model** — deferred; current deploy is public with optional bearer token.
- **Pydantic models for Agent / Task / Message / MemoryDoc** — replaced with plain SQLite rows + request-shape Pydantic validators.
- **`coord_acquire_lock` / `coord_release_lock`** — deferred; per-worktree isolation suffices.
- **Per-worktree `git` pre-commit conflict hook** — not built.
- **`coord_heartbeat`** — SDK `ResultMessage` already updates heartbeat via `_set_status`.
- **Multiple layout presets ("overview", "focus", "debug")** — dropped; manual layout + localStorage persistence is enough.
- **Command palette (Cmd+K)** — not built; ⌘/Ctrl+B / ⌘/Ctrl+. cover the common toggles.
- **PWA manifest + push notifications** — deferred to mobile polish; not built.
- **Pane drag-to-pop-out-window** — not built.
- **Layout presets persistence** — not built; single persisted layout per browser.

---

## 19. Guarantees (hard invariants)

1. No agent writes SQLite directly — every mutation funnels through an MCP tool handler in the server process.
2. Cost caps enforced **before** `agent_started` is emitted so capped attempts don't count as turns.
3. Task cancel clears the owner's `current_task_id` so the Player is unblocked for the next claim.
4. Pause blocks new starts + autoloop ticks; in-flight turns continue until they complete or are explicitly cancelled.
5. `human_attention` events survive restarts (DB-persisted + API-fetched by the UI banner).
6. Session resume across turns unless explicitly cleared via `DELETE /api/agents/<id>/session`.
7. Coach is structurally incapable of modifying code (no Write/Edit/Bash tools in its allowlist).

---

## 20. Known gotchas (from CLAUDE.md)

1. Claude CLI OAuth tokens do NOT live in `~/.claude.json`. Use `claude /login` per host.
2. Zeabur's default region 403s `claude.ai/install.sh`. Dockerfile installs via npm.
3. Do NOT pre-create `/data` in the image — Zeabur's volume mount over an existing dir hangs SQLite silently at startup.
4. `.gitattributes` forces LF on `*.sh` and `Dockerfile*`. New scripts need the same or they fail in Linux containers with `$'\r': command not found`.
5. SQLite WAL journal mode hangs on Zeabur volumes — stick to DELETE journal mode.

---

## 21. What this is not

- Not a team or enterprise product. Individual developer tool.
- Not secure enough for untrusted users. Single-user by design.
- Not a replacement for Claude Agent Teams — this is specifically **more transparent and less automagical**.
- Not going to solve "how do I get 10 Claudes to build an app with no input from me." Human remains in the loop.

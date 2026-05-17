---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 14: Human REST API'
section: 14
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
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

`DELETE /api/auth/claude`

Wipes `$CLAUDE_CONFIG_DIR/.credentials.json` and drops any in-flight pty
login session (its credential context is tied to the about-to-be-removed
account). Lets the operator switch to a different Anthropic account
without first logging out from inside the previously-authenticated CLI.
Returns `{ok, path, deleted, credentials_present: false}` — `deleted` is
`false` (not an error) when the file already didn't exist, so retries
are safe. Requires `CLAUDE_CONFIG_DIR` set; otherwise 400. Emits
`claude_auth_cleared`.

#### 14.2.1 In-app OAuth login

Drives `claude /login` as a pty subprocess on the server so the
operator never has to install the CLI on a separate machine or shell
into the container. Three-step flow with state held in
`server/claude_login.py`. POSIX-only — Windows hosts get 501 with a
pointer to the paste-fallback (§14.2).

| Endpoint | Notes |
| --- | --- |
| `POST /api/auth/claude/login/start` | Spawns `claude` in a pty, sends `/login\n`, polls stdout for the OAuth URL (timeout 30s). Drops any prior in-flight session before spawning — one login per process. Returns `{session_id, url}` or 502 on spawn/timeout/early-exit. Requires `CLAUDE_CONFIG_DIR` set; otherwise 400. Emits `claude_login_started` (actor only — the URL is not bus-published). |
| `POST /api/auth/claude/login/submit` | Body `{session_id, code}`. Writes `code\n` to subprocess stdin, waits up to 30s for a success indicator OR for `.credentials.json` mtime to advance (tie-breaker: CLI may swallow the success line in a TUI redraw). Returns `{ok: true}` and tears down the subprocess. Emits `claude_login_completed`. 400 on unknown/expired session. |
| `POST /api/auth/claude/login/cancel` | Body `{session_id}`. Best-effort tear-down: `SIGTERM`, wait 2s, `SIGKILL`, close pty fd. No-op when the id is unknown. Emits `claude_login_cancelled`. |

A reaper background task (60s tick, 600s TTL) drops orphaned sessions
whose subprocess has exited or whose `started_at` exceeds the TTL —
wired into `lifespan` next to the audit watcher and telegram bridge.

### 14.2.2 In-app Codex OAuth login (device-code flow)

Mirrors §14.2.1 for the Codex runtime. Because the Codex CLI uses the
OAuth 2.0 **Device Authorization Grant** (not PKCE), the flow is
simpler: the server spawns `codex login --device-auth` as a plain
subprocess (no pty required — stdout is plain ASCII), extracts the
verification URL and device code from stdout, and the user enters the
device code at OpenAI's website in their browser. The harness polls
`$CODEX_HOME/auth.json` every 2s via a background monitor task;
completion is detected by the file's mtime advancing past what was
recorded at session start. There is **no submit step** — the user
never pastes anything back into the harness.

Key divergences from the Claude flow (§14.2.1):

| Dimension | Claude (§14.2.1) | Codex (this section) |
| --- | --- | --- |
| Subprocess I/O | pty (`pyte` for URL extraction) | plain stdout/stderr pipe |
| Auth artifact | `$CLAUDE_CONFIG_DIR/.credentials.json` | `$CODEX_HOME/auth.json` |
| Completion signal | success line in stdout OR `.credentials.json` mtime | `auth.json` mtime advance |
| User action | paste OAuth code back to harness | enter device code at browser URL |
| Submit endpoint | `POST /api/auth/claude/login/submit` | none |
| UI device code | n/a | large monospace display + copy button |
| POSIX guard | yes | yes |

| Endpoint | Notes |
| --- | --- |
| `POST /api/auth/codex/login/start` | Spawns `codex login --device-auth`, reads stdout for the verification URL and device code (timeout 15s). Drops any prior in-flight session. Returns `{session_id, url, device_code}` or 502/400. Requires `CODEX_HOME` env var set. Emits `codex_login_started` (actor only). |
| `POST /api/auth/codex/login/cancel` | Body `{session_id}`. SIGTERM → SIGKILL, cancels monitor task. No-op for unknown ids. Emits `codex_login_cancelled`. |
| `DELETE /api/auth/codex` | Unlinks `$CODEX_HOME/auth.json`, cancels all sessions. `deleted=false` when file was already absent (not an error). Emits `codex_auth_cleared`. |
| `POST /api/auth/codex` | Paste fallback: body `{auth_json: string}` or `{auth: object}`. Validates JSON, writes atomically to `$CODEX_HOME/auth.json`. Emits `codex_auth_pasted`. |

A reaper (60s tick, 960s TTL) drops orphaned sessions. Wired into
`lifespan` next to the Claude login reaper. Module: `server/codex_login.py`.

UI: `TeamCodexSection` in `server/static/app.js` — three-phase state
machine (`idle` → `awaiting` → `detected`). In `awaiting` phase,
the URL (with copy/open buttons) and device code (large monospace, bordered)
are shown. A `setInterval` (3s) polls `/api/team/codex` for
`chatgpt_session_present`; on detection, transitions to `detected` for 2s
then returns to `idle`. A `<details>` paste-fallback block (`POST /api/auth/codex`)
remains available below a separator for operators who already have an
`auth.json` from another machine.

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

Coach can update the same objectives file mid-turn through
`coord_set_project_objectives(text)`, which shares the EnvPane writer
and emits the same `objectives_updated` event.

### 14.5 Tasks

| Endpoint | Notes |
| --- | --- |
| `GET /api/tasks?status=&owner=` | Active project tasks |
| `POST /api/tasks` | Human creates a Backlog proposal, emergency TruthGate task, or child task |
| `POST /api/tasks/{task_id}/cancel` | Human cancels task |

Human task creation supports:

- title max 300 chars
- description max 10,000 chars
- optional parent id
- priority `low`, `normal`, `high`, `urgent`
- top-level default behavior: insert a pending Backlog row, not an
  active task; no Player role is planted
- top-level emergency behavior: `emergency=true` requires non-empty
  `emergency_rationale`, records a promoted Backlog row, and inserts
  the active task into `truthgate` with no Player role, then
  automatically starts and awaits TruthGate assessment
- child-task behavior: when `parent_id` is present, the endpoint keeps
  the direct-dispatch subtask path and requires an explicit trajectory
  whose first stage names exactly one Player

### 14.5.5 Backlog

The backlog is the pre-task inbox where agents and humans propose ideas
before Coach triages them into tasks (see `kanban-specs-v2.md` §4.0).

**Schema.** `backlog_tasks` table columns: `id`, `title`, `description` (TEXT, nullable), `proposed_by`, `proposed_at`, `priority`, `trajectory_json`, `note`, `success_criteria`, `status`, `reject_reason`, `promoted_task_id`, `emergency`, `emergency_rationale`, `promotion_basis`. Existing rows on upgrade get nullable/default columns via `_ensure_columns`.

| Endpoint | Notes |
| --- | --- |
| `POST /api/backlog` | Propose a backlog entry (any caller). Body `{title, description?, priority?}`. `priority` ∈ `low\|normal\|high\|urgent` (default `normal`). Returns `{id, title, description, priority, status}`. `description` max 8000 chars; omit or `null` for none. Emits `backlog_task_proposed{..., priority, description_present: bool}`. The kanban **Add to backlog** modal includes a priority selector; default is `normal`. |
| `POST /api/tasks` without `parent_id` | Compatibility path for the human task composer. Non-emergency requests write a pending Backlog row and return `{kind: "backlog", backlog_id, status: "pending"}`; they do not create an active `tasks` row or role assignment. Later Backlog promotion creates a `truthgate` task and automatically runs TruthGate assessment. `emergency=true` requires `emergency_rationale`, records a promoted Backlog row, creates a `truthgate` task with emergency metadata and no Player role, and automatically starts and awaits TruthGate assessment before returning. `parent_id` requests are child tasks and keep the direct-dispatch subtask behavior. |
| `GET /api/backlog?status=` | List backlog entries. `status=pending` (default) / `promoted` / `rejected` / `all`. Returns `{backlog: [...]}` — each entry includes `description` (string or `null`), `priority`, `is_next_eligible`, and emergency metadata when present. Pending rows are priority/FIFO sorted. 400 on unknown status. |
| `PATCH /api/backlog/{id}` | Edit a **pending** backlog entry. Body `{title?, description?, priority?}` (at least one required). `description: ""` clears to `null`. Returns `{id, title, description, priority}`. 400 if title is blank, description exceeds 8000 chars, or priority is invalid; 404 if not found; 409 if status ≠ `pending`. Emits `backlog_entry_updated{id, old_title, new_title, actor, description_present: bool, old_priority?, new_priority?}`. Token-gated. |
| `DELETE /api/backlog/{id}` | Delete a **pending** backlog entry. Returns `{id, deleted: true}`. 404 if not found; 409 if status ≠ `pending`. Emits `backlog_entry_deleted{id, title, actor}`. Token-gated. |

**Status restriction.** Both mutating endpoints check `status = 'pending'`
before acting. Entries that have been promoted or rejected are immutable
via these paths — the 409 response includes the actual status so the
caller can display a useful message.

**UI.** The `BacklogCard` component in `kanban.js` renders a pencil and
trash icon group on card hover. Clicking the pencil opens an inline edit
form with two textareas — title (required) and description (optional).
Clicking the trash opens a confirmation modal. Both call the corresponding
HTTP endpoints via `authedFetch`, then `onRefresh()`. Description text is
shown as a preview (first 120 chars) below the card title, with a "more" /
"less" expand toggle when the text is longer. The `ComposerModal` ("Add to
backlog") also includes an optional description textarea. The two bus events
(`backlog_entry_updated`, `backlog_entry_deleted`) are in the `backlogWatched`
set so the board auto-refreshes on remote changes.
Backlog promotion also emits the normal task creation/stage/role events
and the Kanban pane treats `backlog_task_promoted` as a board refresh
trigger, so a promoted entry disappears from Backlog and appears in its
initial active column without the operator pressing Refresh or reloading.
Rejection emits `backlog_task_rejected`, which refreshes the Backlog list.

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
| `POST /api/file-write-proposals/{id}/approve` | Resolve a pending proposal as approved. Dispatches on scope: `truth` writes to `truth/<path>` (broader extension allowlist than the Files-pane endpoint, 512,000-char cap); `project_claude_md` writes to the project's `CLAUDE.md`. Then marks the row. Body `{note}` optional. Emits `file_write_proposal_approved`. |
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
| `GET /api/turns/summary?hours=24` | Per-agent + team spend/turn aggregate, including token columns and `cache_hit_pct` (see §10.0) |
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
| `PATCH /api/mcp/servers/{name}` | Toggle enabled, update allowed_tools, and/or replace config_json (with `***`-sentinel merge to preserve stored secrets when the user edits unrelated fields); evicts cached Codex clients |
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
If an eviction happens while a Codex turn is still in flight, the
runtime cache-pops the client immediately but queues the old
client/token pair for close in that turn's `finally` block; this lets
the live stream finish without leaking an orphaned app-server process.

`PATCH /api/mcp/servers/{name}` runs its SQLite read/merge/update
section in a worker thread with a 30 s busy timeout. The route is
async, and keeping synchronous sqlite lock waits off the event loop
prevents an aiosqlite task from being unable to release the same DB
lock that PATCH is waiting on.

MCP paste shapes accepted:

- Claude Desktop style: `{ "mcpServers": { ... } }`
- TeamOfTen file style: `{ "servers": { ... } }`
- Flat single config with a supplied name.
- Bare named map.

Secret scanner warns on common raw token patterns unless `allow_secrets=true`.

---

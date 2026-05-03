# TeamOfTen — Claude Code Harness

A personal orchestration harness for a **team of 11 Claude Code agents — 1 Coach + 10 Players** — sharing memory and a task board, with a multi-pane web UI, deployed to a single VPS (Zeabur). Max-plan OAuth only — no API keys.

**Full spec**: [Docs/TOT-specs.md](Docs/TOT-specs.md) — read it before touching server code.

**Keep the spec in sync.** When you make non-trivial code changes (new feature, behavior change, schema/migration, prompt rewrite, UI subsystem, env var, MCP tool, etc.), reflect them in `Docs/TOT-specs.md` in the same turn. Skip only for genuinely minor tweaks (typos, log-message wording, single-line bug fixes that don't change documented behavior). When in doubt, update the spec — drift is more expensive to repair later than a paragraph is to write now.

## Team vocabulary

- **Coach** (slot id `coach`) — the coordinator. Receives human goals, decomposes into tasks, assigns work. Never writes code. **Only Coach gives orders.**
- **Players** (slot ids `p1`–`p10`) — workers. Each Player has a **name** (e.g. "Alice") and a **role description** (e.g. "Developer — writes code") both **assigned by Coach** at team-composition time. Players execute work, report back, and may message peers for information — but **Players never give orders** to other Players.
- **Team** — all 11 agents together. "Team of ten" refers to the 10 Player slots; Coach is always on.

---

## Tech stack

- **Backend**: Python 3.12 + FastAPI + WebSocket, single mono-service
- **Agent runtime**: Claude Agent SDK (Python), authenticated via Max-plan OAuth
- **Frontend**: React 18 + TypeScript + Vite + react-mosaic (desktop) / stack+tabs (mobile) + Zustand
- **State**: SQLite (hot path) + a WebDAV-compatible cloud drive (durable snapshots + human-readable `.md`) — works with kDrive, Nextcloud, ownCloud, etc.
- **Deploy**: Docker container on Zeabur, auto-pulled from this GitHub repo
- **Reverse proxy**: Zeabur handles TLS/ingress (Caddy from the original spec is not needed on Zeabur)

---

## Current state (2026-04-24)

Backend + UI essentially feature-complete for the personal harness. Heavy
self-paced /loop development with no end-to-end verification yet on the
deployed Zeabur instance — see "What needs verification" below.

**Done:**
- **M-1** ✓ Max OAuth + 10-concurrent feasibility (laptop + Zeabur EU)
- **M0** ✓ FastAPI skeleton, Dockerfile, Zeabur auto-deploy from main
- **M1** ✓ One Claude SDK agent streaming to a WebSocket UI
- **M2a** ✓ SQLite state + 11-agent roster (Coach + p1..p10) + first coord_* tools
- **M2b** ✓ Task state machine (`coord_claim_task`, `coord_update_task`)
- **M2c** ✓ Inter-agent chat (`coord_send_message`, `coord_read_inbox`,
   per-recipient unread tracking via `message_reads` table)
- **M2d** ✓ Shared memory commons (`coord_list/read/update_memory`)
- **M2e** ✓ Per-agent + team daily cost caps (env-configurable, enforced
   pre-spawn, `cost_capped` events)
- **v2 (a/b/c/d)** ✓ Preact frontend rewrite: slim left rail with status dots,
   tileable agent panes (Split.js drag-resize), per-tool renderers
   (Read/Edit/Bash/Grep/Glob/coord_*/generic + Edit diff card + Read-of-image
   inline preview), tool_use↔tool_result pairing, Image paste via
   /api/attachments, EnvPane with live tasks/cost/timeline, SettingsDrawer
- **M3 (1/2/3)** ✓ kDrive persistence:
   - Memory docs synchronously mirror to `/harness/memory/<topic>.md`
   - Event log flushed every 5 min to `/harness/events/<date>.jsonl`
     (with yesterday-replay during 00:00–02:00 UTC for boundary safety)
   - Hourly `VACUUM INTO` snapshot to `/harness/snapshots/<ts>.db`
- **M4 (1/2/3)** ✓ Per-Player git worktrees:
   - `git` installed in container with default identity
   - On boot, if `HARNESS_PROJECT_REPO` is set, clone to `/workspaces/.project`
     and create worktree `/workspaces/<slot>/project` on branch `work/<slot>`
   - Branch resolution preserves `origin/work/<slot>` history if it exists
   - `coord_commit_push` MCP tool (Player-only; rejects Coach) wraps
     `git add -A && commit && push origin HEAD` and emits a `commit_pushed`
     event. Push expects creds via PAT-in-URL on `HARNESS_PROJECT_REPO`.
- **M5 step 1** ✓ session_id captured on `ResultMessage` and persisted to
   `agents.session_id`. Green ● indicator in pane header when present;
   `DELETE /api/agents/<slot>/session` clears it (button next to the dot).
- **Auth (opt-in)** ✓ `HARNESS_TOKEN` env: when set, every `/api/*` (except
   `/api/health`) requires `Authorization: Bearer <token>`; WebSocket uses
   `?token=`. UI shows a paste-modal when 401 returned, saves to localStorage,
   reloads. Backwards compatible: unset env = open API as before.
- **`/api/health`** ✓ per-subsystem readiness probe (db / static / claude_cli
   / webdav / workspaces). Cached: claude_cli once per process, webdav 60s.
   Returns 503 when any required subsystem fails. Public endpoint.
- **Layout persistence** ✓ `openSlots` + `envOpen` saved to localStorage
   (`harness_layout_v1`); restored on reload via lazy initializers.
- **Empty-pane hints** ✓ when an agent pane has no events, shows a hint
   card with example prompts (Coach gets two starters; Players get a short
   line). Hint disappears after the first event arrives.
- **Decisions** ✓ `coord_write_decision` (Coach-only) writes
   `/data/decisions/<date>-<slug>.md` + kDrive mirror; `GET /api/decisions`
   + `/api/decisions/{filename}` expose them; EnvPane Decisions section
   lists with click-to-expand body, refreshes on `decision_written` events.
- **Snapshot retention** ✓ kDrive snapshot loop prunes oldest beyond
   `HARNESS_KDRIVE_SNAPSHOT_RETENTION` (default 48 ≈ 2 days hourly).
- **Coach recurrence scheduler** ✓ unified
   `recurrence_scheduler_loop` reads rows from `coach_recurrence`
   every `HARNESS_RECURRENCE_TICK_SECONDS` (default 30s). Three
   flavors: tick (singleton, smart-composed prompt) / repeat (custom
   prompt) / cron (DSL). Skips when Coach is already working or daily
   cap hit. Manual trigger: `POST /api/coach/tick` (409 if busy).
   Replaced the legacy in-memory `coach_tick_loop` /
   `coach_repeat_loop` pair — see `Docs/recurrence-specs.md`.

- **M5 step 2** ✓ `ClaudeAgentOptions(resume=<session_id>)` wired;
   agent_started events carry `resumed_session: bool`; UI shows ↻ vs →
   in the timeline. DELETE /api/agents/<id>/session clears the stored
   id to force a fresh turn.
- **Escalation tool** ✓ `coord_request_human(subject, body, urgency?)`
   (both Coach and Players); emits a `human_attention` event. EnvPane
   surfaces undismissed escalations as a pinned red banner, restored
   across page reloads from /api/events?type=human_attention. Dismissal
   is local-only (per-__id in localStorage).
- **2D layout** ✓ columns can stack multiple panes; shift-click a slot
   in the left rail stacks into the last column; each axis gets its own
   Split.js resize gutter.
- **Pane settings popover** ✓ per-pane model / plan-mode / effort
   controls with localStorage persistence; wired through to
   `ClaudeAgentOptions` server-side.
- **Drag-to-move panes** ✓ grab a pane's label area, drop on another
   pane to insert before it, on a column's bottom strip to append, or
   on the right rail to open a new column. Custom MIME type so we
   don't collide with image paste.
- **Split.js size persistence** ✓ user-dragged column widths / stack
   heights survive add/remove/move, keyed by layout signature in
   localStorage (harness_split_sizes_v1).
- **Pane export** ✓ ↓ button in header downloads conversation as
   markdown (one ## per event, paired tool_use/tool_result inline).
- **Team composition** ✓ `coord_set_player_role(player_id, name, role)`
   (Coach-only) writes agents.name/role; `player_assigned` event
   refreshes UI live.
- **Memory / Inbox / Decisions UI** ✓ EnvPane sections with
   click-to-expand read + live WS refresh. Inbox has a human→agent
   composer (POST /api/messages with from_id='human').
- **Current task chip** ✓ pane header shows the agent's
   current_task_id title (⚑) when it's working on one.
- **LeftRail unread dot** ✓ accent-colored dot appears on a slot
   button when events arrived while its pane was closed; clears on
   open / close.
- **Keyboard shortcut** ✓ ⌘/Ctrl+B toggles the EnvPane.

**Post-spec continuous delivery (everything shipped since milestone numbering stopped):**
- **Auto-wake** ✓ task assignments + direct messages auto-spawn the
   target's turn with an inline wake prompt. 10 s debounce for chat
   (prevents ping-pong); bypassed for discrete actions. Cost-cap
   check short-circuits before a storm of `cost_capped` events.
- **Stale-session auto-heal** ✓ a `ProcessError` on resume clears
   `session_id` and retries once — no more manual pane × clicks
   after `/login` rotation / CLI upgrade.
- **Per-agent brief** ✓ `agents.brief` column, injected into every
   turn's system prompt after governance context. Editable via pane
   settings popover or `PUT /api/agents/{id}/brief`.
- **Lacrosse auto-naming** ✓ first-spawn picks an unused surname
   from a ~50-entry pool (Rabil, Powell, Gait, …); race-safe via
   module-level `asyncio.Lock`.
- **Turns ledger** ✓ dedicated `turns` table populated on every
   `ResultMessage`. `GET /api/turns` + `/api/turns/summary` endpoints.
   Cost-cap check reads this instead of JSON-extracting events.
- **Fan-out** ✓ inter-agent events (message_sent, task_assigned,
   task_updated) render in both actor's and target's panes; history
   reload matches via SQL json_extract.
- **Retention loops** ✓ events trim (30d default), attachments
   trim (30d default). Both mirror the pattern, configurable via env.
- **Crash recovery** ✓ zombie agents.status / tasks.in_progress reset
   on boot via `crash_recover()`.
- **Sticky turn headers** ✓ each `agent_started` is a `position:
   sticky` one-line bar in the pane body.
- **Token streaming** ✓ opt-in via `HARNESS_STREAM_TOKENS=true`
   (off by default — some CLI builds crash on the underlying flag).
- **Compact system renderers** ✓ 15+ event types rendered as
   single-line `.sys` rows instead of JSON blobs.
- **Input mode chips + slash commands** ✓ `/plan /model /effort
   /brief /tools /clear /loop /tick /status /spend /help`. All
   intercepted locally; unrecognized slashes still fall through.
- **Files pane** ✓ `__files` special slot with root selector, tree,
   .md preview/edit toggle. Live-reloads on fs events.
- **External MCP servers** ✓ `HARNESS_MCP_CONFIG` + server/mcp_config.py
   +example file in repo root. Health surface reports parse errors.
- **CI** ✓ GitHub Actions runs pytest on every push. ~113 tests.
- **Docker HEALTHCHECK** ✓ curls /api/health every 30 s.
- **OAuth persistence** ✓ `CLAUDE_CONFIG_DIR=/data/claude` so tokens
   survive Zeabur redeploys.
- **5-min DB snapshots** ✓ down from hourly, keeps 144 (~12 h).
- **Runtime Coach loop** ✓ `/loop N` / `/loop off` without restart.

**Recent (2026-04-24):**

Data lanes shipped & documented:
- **attachments** — UI paste-target for images, local-only, 30d trim.
- **uploads** — human → kDrive → container, pulled every 60s,
  per-slot `./uploads` symlink. Read-only for agents.
- **outputs** — agent → binary deliverables (`coord_save_output`,
  base64) + safety-net push loop every 60s for Write/Bash bypass.
- **knowledge** — agent → text artifacts (.md/.txt) via
  `coord_write_knowledge`, synchronous kDrive mirror.
- All three kDrive-synced lanes documented in CLAUDE.md data paths
  section that's injected into every system prompt.

UI (Options drawer):
- **Team tools** ✓ team-wide WebSearch / WebFetch toggles (replaced
  the per-agent version). Functional state updater so rapid toggles
  don't race.
- **Default models** ✓ per-role Coach / Players dropdowns.
  Precedence: pane override > role default > SDK default.
- **Project repo** ✓ DB-backed `team_config` (env fallback), masked
  display, secret-scan on save, `${VAR}` placeholder pattern for
  GITHUB_TOKEN. `provision now` button runs `ensure_workspaces()`
  live — no redeploy needed to materialize worktrees.
- **MCP servers** (Phase 1) ✓ paste-JSON, secret-detect,
  per-server card with toggle/delete/smoke-test.
- **Sessions** ✓ batch-clear session_id per agent (tick list + Only
  active + Clear selected).
- **kDrive** ✓ probe button, two-step diagnostic, URL-only
  (dropped KDRIVE_ROOT_PATH). TOT/ folder required on kDrive.

Pane header cleanup:
- slot short labels (`C` / `1..10` instead of raw ids), role in
  tooltip only, current-task icon only, lock SVG (green open /
  red locked, shackle left), `↓` export, `🗑` clear session.
- Session-clear renders a loud dashed amber `SESSION CLEARED`
  separator in the timeline.
- Context-applied / agent_stopped / lock_updated / tools_updated
  render as compact `.sys` one-line rows (not JSON blobs).

Lock feature:
- Per-Player `agents.locked` flag (migration). Locked Players get
  no Coach task assignments, no Coach direct messages, no Coach
  broadcasts (filtered at `coord_read_inbox`). Human prompts + UI
  messaging still work. Coach's system prompt gets a "Roster
  availability" block when any Player is locked so it plans around
  the constraint.

Security hardening:
- **`HARNESS_TOKEN` auth** ✓ Bearer token gate on all `/api/*`
  except `/api/health`. `require_token` dependency; UI paste-modal
  on 401 saves to localStorage.
- **Audit actor** ✓ `audit_actor(request)` dependency returns
  `{source, ip, ua}`; threaded into 11 destructive endpoints
  (identity / brief / models / tools / repo / provision / MCP
  save/patch/delete / lock / session-clear single+batch). Every
  destructive event carries `actor` in its payload.
- **XSS fix** ✓ `renderInline` runs markdown `[text](url)` URLs
  through a scheme allow-list (`http/https/mailto` + relative /
  fragment). `javascript:` / `data:` / `vbscript:` → inert `#`.
  Closes the "agent-rendered link exfiltrates localStorage token"
  path. `rel="noreferrer"` added too.
- **Redacted MCP display** ✓ `_redact_mcp_config` masks env/header
  values (except `${VAR}` placeholders) and URL userinfo before
  `GET /api/mcp/servers` returns them.
- **Repo URL masking** ✓ `_mask_repo_url` hides userinfo in UI.

Workspace resilience:
- **`workspace_dir(slot)`** ✓ stat-checks `<slot>/project/.git`
  before returning it. Missing worktree → falls back to plain
  `/workspaces/<slot>/`. Closes the "repo configured but never
  provisioned → CLIConnectionError on every spawn" footgun; only
  code-touching turns and `coord_commit_push` fail loudly now.
- **`POST /api/team/repo/provision`** ✓ runs `ensure_workspaces()`
  live with a cache refresh; idempotent across existing worktrees.

Context / compaction:
- **agents.continuity_note** + **agents.last_exchange_json**
  columns. On every successful non-compact turn: append
  `(entry_prompt, accumulated response)` to the rolling exchange
  log (cap `HARNESS_HANDOFF_EXCHANGES`, default 10, clipped
  1500/3000 chars).
- **`/compact` slash command** + `POST /api/agents/{id}/compact`.
  Queues a compact-mode turn via `run_agent(prompt=COMPACT_PROMPT,
  compact_mode=True)`. On success, writes summary to
  `continuity_note`, nulls `session_id`, clears the exchange log.
  UI shows `session_compact_requested` → compact turn →
  `session_compacted`.
- **Auto-compact at 70% context** (`HARNESS_AUTO_COMPACT_THRESHOLD`,
  default 0.7). Pre-spawn check in `run_agent`: if prior session's
  estimated token use ≥ threshold × model's window, run a compact
  turn first (recursive call with `compact_mode=True`), then the
  user's original prompt on the fresh session. Two turns in the
  timeline; user's prompt not lost.
- **Token tracking** ✓ `turns` gains `input_tokens`,
  `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`.
  `_extract_usage` pulls from `ResultMessage.usage` defensively.
  `_session_context_estimate` reads latest row per session.
- **Structured COMPACT_PROMPT** with required markdown sections
  (`## Current task`, `## Open questions (verbatim)`,
  `## Key findings`, `## References (quote verbatim)`,
  `## Context quirks`). Recent exchanges are injected
  programmatically verbatim from the rolling log — no paraphrase.
- **Post-compact system prompt** gets:
  ```
  ## Handoff from your prior session (via /compact)
  <summary>
  ### Recent exchanges (verbatim, last N turns before compact,
  oldest first)
  #### Exchange 1 of N  **User asked:** …  **You replied:** …
  ```
  Cleared on first successful non-compact turn.

Reliability:
- **Post-result exception suppression** widened to any exception
  type (was `ProcessError` only) — CLI 2.1.12x raises bare
  `Exception("Command failed with exit code 1")` during teardown
  after a successful ResultMessage; we now log-and-skip regardless
  of exception class.
- **Auto-retry after hard errors** — when a turn errors *before*
  ResultMessage (real failure), schedule a single wake after
  `HARNESS_ERROR_RETRY_DELAY` s (default 45) with a "resume or
  mark blocked" prompt. Cap at
  `HARNESS_ERROR_RETRY_MAX_CONSECUTIVE` (default 3) — then
  escalates via `human_attention` and stops retrying. Counter
  resets on any successful turn (including got_result-but-
  threw-after suppressions).

**Recent (2026-04-25):**

Telegram bridge ([server/telegram.py](server/telegram.py)):
- **Inbound** — long-polls Telegram `getUpdates`. Whitelist-gated by
  numeric chat_ids; refuses to start if token set without a whitelist
  (anyone who finds the bot could otherwise pilot Coach). Inbound text
  → INSERT into `messages` with `from_id='human'`, `to_id='coach'`,
  `subject='telegram:<chat_id>'` → `bus.publish(message_sent)` →
  `maybe_wake_agent('coach', …, bypass_debounce=True)` so the existing
  wake path spawns Coach's turn.
- **Outbound** — subscribes to `bus`, buffers `agent_id='coach'`
  `text` events, flushes accumulated text to every whitelisted chat
  on `agent_stopped`. Empty turns (Coach only used tools) flush
  nothing. `human_attention` escalations are also forwarded so phone
  pings on `coord_request_human`. Splits at paragraph/line boundaries
  to fit Telegram's 4096-char cap (uses 4000 for headroom).
- **Long-polling chosen over webhook** — works behind Zeabur TLS with
  no public-URL plumbing; Telegram → bot is outbound HTTP, not blocked.
- **UI-managed config** — token + chat_ids live in the encrypted
  `secrets` table (Fernet via `HARNESS_SECRETS_KEY`) under names
  `telegram_bot_token` and `telegram_allowed_chat_ids`. Edit them in
  the Options drawer (`Telegram bridge` section) — saving triggers a
  live `reload_telegram_bridge()` so changes apply without a restart.
  Env-var fallback (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_ALLOWED_CHAT_IDS`)
  kicks in only when the secrets aren't set, so a fresh deploy can
  bootstrap from env if you'd rather. `GET /api/team/telegram` returns
  masked status (token plaintext is never returned); PUT upserts +
  validates token format + reloads; DELETE wipes both, sets the
  `telegram_disabled` flag, and stops the bridge.
- **`telegram_disabled` flag** (team_config) — Clear sets it; Save
  unsets it. Resolved before any other config so a Clear truly stops
  the bridge even when env vars are set. Without this flag, env-var
  fallback would silently re-enable the bridge right after Clear.
- **Token format validation** — PUT rejects tokens that don't match
  `^\d+:[A-Za-z0-9_-]{30,}$` (BotFather shape). Stops users from
  saving "tg_xxxxx" or other paste mistakes that 401 forever.
- **Auth-failure escalation** — inbound loop tracks consecutive 401/403
  responses; after 5 in a row, emits a `human_attention` event
  ("Telegram bridge stopped — auth failure") and exits the loop.
  `_run` cancels the outbound task too so nothing keeps running.
  User has to Save fresh config to retry. Stops log spam from
  rotated tokens.
- **User-initiated turn filter** — outbound loop only forwards Coach
  turns that were triggered by a human message (UI composer or
  Telegram inbound, identified via `message_sent` event with
  `agent_id='human'` and `to='coach'`). Coach autoloop ticks and
  Player→Coach chatter stay silent so the phone doesn't ping every
  2 minutes. `human_attention` events are always forwarded
  regardless.
- **Restartable lifecycle** — the module owns its own task handle in
  `_current_task`; lifespan calls `start_telegram_bridge()` on boot
  and `stop_telegram_bridge()` on shutdown. The bridge isn't tracked
  in `bg_tasks` because the API layer can swap it out mid-run.
  `_run` uses `asyncio.wait(FIRST_COMPLETED)` so if either inbound
  or outbound exits, the other is cancelled.
- `httpx` promoted from dev-extra to runtime dep.
- Tests in [server/tests/test_telegram.py](server/tests/test_telegram.py)
  cover `is_valid_token`, `_parse_chat_ids`, `_split_chunks`, the
  disabled-flag round-trip, and `_resolve_config` precedence
  (disabled flag > DB secrets > env > unset).

Stale-task watchdog post-redeploy fix:
- `crash_recover()` on every container boot demotes
  `tasks.status='in_progress'` → `'claimed'` (so the harness doesn't
  carry zombie running-state across restarts). The previous watchdog
  query in [server/agents.py](server/agents.py) only looked at
  `in_progress`, so every Zeabur redeploy silently blinded the
  watchdog to all active tasks.
- Fix: `WHERE t.status IN ('in_progress', 'claimed')`. A `claimed`
  task with no recent owner activity is also a stall — either the
  owner never started, or the boot reset wiped state. The DM to
  Coach now mentions the actual status so the right remediation
  ("nudge them to start" vs "they're stuck mid-work") is obvious.

Display section in Options drawer:
- New `DisplaySection` toggles timezone rendering between local time
  (default) and UTC. Server stamps everything in UTC; this only
  affects how `timeStr()` renders ISO timestamps in the timeline.
  `Intl.DateTimeFormat().resolvedOptions().timeZone` is shown so the
  user knows what "local" means on their device. Persisted in
  `localStorage` as `harness_tz_pref`; toggle reloads the page so
  already-rendered timestamps update at once.

Mobile / phone layout (CSS-only, < 700px viewport):
- `@media (max-width: 700px)` block in
  [server/static/style.css](server/static/style.css) reflows the
  whole app for phones.
- The left rail moves to the bottom and splits into two rows via
  CSS Grid: agents on top (horizontally scrollable), then a single
  row with files / project placeholder / pause / env-toggle /
  settings beneath. Layout-preset buttons + cancel-all are hidden
  (don't fit single-pane swipe).
- The panes area becomes a horizontal swipe deck:
  `scroll-snap-type: x mandatory` on `.panes` + `min-width: 100%
  + scroll-snap-align: start` on each `.pane-col`. One pane fills
  the screen; native touch swipe moves to the next open pane.
  Split.js gutters (`.gutter`) and drop-zones are hidden — they
  don't fit the model and HTML5 DnD doesn't work on touch anyway.
  The `⛶` maximize button is also hidden (single-pane already).
- EnvPane becomes a full-screen overlay (`position: fixed; inset:
  0`) when toggled open. The `×` button in its header dismisses
  back to the panes view.
- Inline `grid-template-columns` set by App's `appStyle` is
  overridden via `!important` in the media query so the desktop-only
  3-column grid doesn't apply on phones.

Audit fixes (post-LeftRail / file-links):
- **Cost-cap + recent-cancellation now surface as red.** `agents.status`
  stays `idle` when capped or cancelled (the cap blocks the *next*
  spawn, the cancel resets status), so a literal status check missed
  both. `App` now derives a `problemSlots: Set<slotId>` from
  `agents.status === "error"`, plus a cap check against
  `serverStatus.caps` (`agent_daily_usd`, `team_daily_usd`,
  `team_today_usd`), plus a backwards walk of each slot's events for
  a most-recent `agent_cancelled` not superseded by a later `result`
  / `agent_started`. LeftRail's `state-problem` class consults the
  Set instead of the dead status check.
- **renderMarkdown fallback now goes through DOMPurify.** When
  `marked.parse` throws, the fallback escaped-text-in-`<pre>` block
  was previously returned raw — bypassing the sanitizer + the
  file-link / external-link hooks. Now the fallback string is
  sanitized just like the happy path so behavior stays consistent.
- **dotStates cold-start populated.** `seedConversationsFromHistory`
  fires on App mount (and on every WS reconnect via `wsAttempt`):
  fetches `/api/events?agent=<slot>&limit=50` for all 11 slots in
  parallel, dedupes by `__id`, and merges into `conversations`. The
  rail dots are now accurate before any panes have been opened in
  the session — fixes the "everything reads green on first paint"
  cold-start gap I documented.
- **`/api/files/roots` test coverage** for the new `path` field
  (test_list_roots_includes_absolute_path in
  [server/tests/test_files.py](server/tests/test_files.py)).

In-app file links (markdown `[text](/data/...)` opens Files pane):
- DOMPurify `afterSanitizeAttributes` hook in
  [server/static/app.js](server/static/app.js) inspects every `<a>`:
  external URLs get `target=_blank` + `rel=noreferrer noopener`;
  hrefs that start with `/` are tagged `data-harness-path` +
  `class="harness-file-link"` and the href is neutralized to `#`.
- Document-level click listener in `App` catches clicks on
  `[data-harness-path]`, opens the `__files` pane if not already
  open (also exits `maximizedSlot`), and stashes
  `{ path, ts }` in `pendingFileOpen` state.
- `FilesPane` reads a new `rootsFromApp` prop (App fetches roots
  once on mount via `loadFileRoots` and caches in `fileRoots`)
  alongside its own self-fetch — whichever lands first wins. On
  every `pendingFileOpen` change, FilesPane longest-prefix-matches
  the absolute path against the roots' `path` field, switches to
  that root, expands every parent folder via the existing
  `expanded` Set, opens the file, and calls `clearPendingFileOpen`.
- `/api/files/roots` now returns `path` (the absolute on-disk path
  of each root) so the resolver works under env-overridden layouts
  (e.g. `HARNESS_OUTPUTS_DIR`) instead of a hardcoded `/data/...`
  prefix table.
- File-link styling: amber color + leading `📄` so it reads as
  "opens Files pane, not a tab" at a glance.
- Phase-2 idea (not shipped): auto-linkify bare paths in plain
  text (e.g. `the report at /data/outputs/wiki/foo.md`) via a
  `marked` extension or post-render text scan.

LeftRail redesign — borderless slot buttons + grouped layout:
- Slot buttons no longer use border-as-state. Instead two orthogonal
  dimensions are encoded:
    1. **Work state** → background tint + label color.
       - `unused` (no `session_id` ever): transparent, gray label.
       - `state-idle` (has session, idle): blue tint, blue label.
       - `state-working`: amber tint, amber label, slow pulse glow.
       - `state-problem` (`error` / `cost_capped` / `cancelled` all
         collapse here): red tint, red label.
    2. **Comms state** → small dot, top-left, only on activated agents.
       - `green`: nothing pending.
       - `blue`: incoming `message_sent` (or `task_assigned`) newer
         than the agent's last `agent_started` — unread inbox.
       - `orange`: idle, has a current task, and the most recent
         direct outgoing `message_sent` (non-broadcast, non-human)
         is newer than any incoming AND newer than the last
         `agent_started` — i.e. waiting for a reply. Heuristic
         computed UI-side over `conversations`; flickers on quick
         exchanges (accepted trade-off).
- **Pane-open** marker: 3px accent stripe on the left edge via
  `::before`, drawn over the state tint so it composes cleanly with
  any work state.
- **Locked** agents: `filter: grayscale(0.65) brightness(0.8)` +
  `opacity: 0.75` + a tiny 🔒 badge at bottom-right. Reads as "off
  the team" at a glance; hover restores full color.
- The rail is split into four logical groups (top → bottom):
  agents → files + project-selector placeholder → layout/pause/
  cancel controls → env-toggle + settings. Auto-margin on the first
  bottom group pushes the bottom block down; fixed `margin-top: 14px`
  between bottom subgroups gives them visible separation.
- Project-selector is a disabled placeholder button (`P` in a dashed
  outline) reserving the slot for an upcoming feature.
- `dotStates` Map computed in `App` and passed to `LeftRail`. The
  prior `unread` accent (top-right pip) is gone — the new comms-state
  dot supersedes it.

Edit tool diff card — side-by-side, color-only:
- Two-column layout in the spirit of the Antigravity / VS Code split
  diff. Old content on the left (red band), new content on the right
  (green band), unchanged context identical on both sides at the same
  y-position so the reader can scan horizontally. Pure additions get
  a hatched blank-left placeholder; pure removals get blank-right.
  A removed-then-added pair is zipped line-by-line so a modification
  reads as old → new across the row.
- No `+` / `-` prefix gutter and no `before` / `after` header band —
  the band color carries all the signal. Saves the horizontal width
  for the actual content.
- Uses `diff@7` (vendored under `/static/vendor/`) for line-based
  diffing.
- Each line is syntax-highlighted by `highlight.js` based on file
  extension. Mapping in [server/static/tools.js](server/static/tools.js):
  bash/css/go/html/js/json/md/py/rs/sql/ts/yaml. Markdown files
  highlight `## headings`, `**bold**`, fenced blocks, etc. so a
  doc-edit reads as well as a code-edit.
- Header counts are now derived from real diff stats
  (`(-N +M)` reflects actually changed lines, not the
  `old_string`/`new_string` line counts which double-count context).
- A small lang badge (`PYTHON`, `MARKDOWN`, …) appears in the
  summary row when extension recognition succeeds.
- Single-line highlighting can lose multi-line state (open string
  literals etc.) — acceptable trade-off vs the contortion of
  highlighting full sides and then mapping back to diff rows.

Markdown rendering upgrade — marked + DOMPurify + highlight.js:
- Replaced the hand-rolled `renderMarkdown` (and its `_safeHref` /
  `renderInline` helpers) with `marked@12` for parsing + `dompurify@3`
  for sanitization + `highlight.js@11` for code-block syntax
  highlighting. All loaded from esm.sh (theme CSS from jsdelivr
  because esm.sh wraps CSS in JS modules).
- GFM is on: pipe tables, nested lists with proper indentation, task
  lists (`- [x]`), strikethrough, autolinks, blockquotes. Tables now
  render as a real grid with header band + zebra rows instead of a
  flat run of `|` separators.
- Highlight.js languages registered: bash/sh/shell, css, go, html/xml,
  javascript/js, json, markdown/md, python/py, rust/rs, sql,
  typescript/ts, yaml/yml. Add more via `hljs.registerLanguage(...)`
  near the top of [server/static/app.js](server/static/app.js).
  Unregistered languages render as plain code blocks (no error).
- DOMPurify owns the URL-scheme allowlist (drops `javascript:`,
  `data:`, `vbscript:` etc.) and adds `target="_blank"`
  + `rel="noreferrer noopener"` to every `<a>` via an
  `afterSanitizeAttributes` hook. Replaces the prior manual
  `_safeHref` regex.
- CSS in [server/static/style.css](server/static/style.css) targets
  plain HTML inside `.markdown` and `.files-md-preview` containers
  (h1..h6, p, ul/ol/li, pre/code, table/th/td, blockquote, hr,
  task-list checkboxes). The github-dark hljs theme paints the
  inner `<code>` spans.

Pane maximize / restore:
- Each pane (AgentPane + FilesPane) header has a `⛶` (maximize) /
  `❐` (restore) button between the gear and close.
- `maximizedSlot` lives in App state, persisted alongside
  `openColumns` in `harness_layout_v1`. While set, `effectiveColumns`
  collapses to `[[maximizedSlot]]` so the chosen pane fills the
  panes area; Split.js stands down (no gutters), drop-zones hide.
- Auto-restore: clicking any LeftRail slot, `stackInLast`, or a
  layout preset (`spread`/`pairs`) clears the maximize. Closing the
  maximized pane also clears it.
- EnvPane stays independent — toggle it with ⌘/Ctrl+B if you also
  want full-width focus.
- Known limitation: maximize/restore re-mounts the pane component
  (column key changes when its slot list changes), so transient
  per-pane UI state (search filter, settings popover open/closed,
  scroll position) resets. Conversation history reloads from
  `/api/events` cache.

**Recent (2026-04-29) — Codex runtime unblock:**

Three live bugs surfaced when Coach actually exercised Codex mode
end-to-end. All three blocked `coord_*` MCP calls in different ways;
each fix is small but the architecture for the second one shifted.

- **`config.plugins` removed** —
  [server/runtimes/codex.py:_codex_config_overrides](server/runtimes/codex.py).
  Earlier drafts passed `config = {"plugins": {"enabled": false}}` to
  suppress plugin warmups. Codex's TOML schema treats `plugins` as a
  map keyed by plugin *name* with `PluginConfig` values, so
  `plugins.enabled` is parsed as plugin name `"enabled"` with value
  `false` — `thread/start` fails with
  `invalid type: boolean false, expected struct PluginConfig`. Default
  (no `plugins` key) is correct. Spec mirror in
  `Docs/CODEX_RUNTIME_SPEC.md` §C.5.
- **`default_tools_approval_mode = "approve"` on coord MCP server** —
  [server/runtimes/codex.py:_build_mcp_servers](server/runtimes/codex.py).
  Codex routes MCP tool calls through an elicitation/approval path
  under restrictive sandboxes. Coach (`read-only`) hit
  `"user rejected MCP tool call"` because the embedded app-server
  client has no `request_user_input` handler (the SDK's
  `set_approval_handler` only sees Command/FileChange approvals, not
  MCP). The Python `codex-app-server-sdk` 0.3.2 does not expose the
  MCP-approval hook, so we mark the whole `coord` server pre-approved
  via the documented per-server config key. `coord_*` is harness-
  trusted by the single-write-handle invariant. Players
  (`danger-full-access`) skip the approval path so they were already
  fine. External MCP servers are untouched. See openai/codex issue
  #16685 + PR #16632 for upstream context.
- **Coord-proxy token lifetime: per-client, not per-turn** —
  [server/runtimes/codex.py:get_client](server/runtimes/codex.py),
  [server/runtimes/codex.py:close_client](server/runtimes/codex.py),
  [server/agents.py:run_agent](server/agents.py). The codex
  app-server subprocess (and its child `coord_mcp` stdio process) is
  cached per slot via `_codex_clients`; its env, including
  `HARNESS_COORD_PROXY_TOKEN`, is captured once at first spawn. The
  dispatcher used to mint a fresh token per turn and call
  `revoke_for_caller(slot)` in the `finally` block — which killed the
  long-lived token still held by the running subprocess. Turn 1 worked,
  every subsequent turn 401'd on `coord_*`. Token lifecycle is now
  owned by `CodexRuntime`: `get_client` mints + caches in a new
  `_codex_client_tokens: dict[slot, str]` map, `close_client` revokes.
  Identity binding still holds (each subprocess gets exactly one
  token, dies with its subprocess). Spec mirror in
  `Docs/CODEX_RUNTIME_SPEC.md` §C.4.

UI:
- **Trash icon on Codex panes** — `/api/agents` now returns
  `codex_thread_id` alongside `session_id`
  ([server/main.py:list_agents](server/main.py)); the pane-header
  trash button, LeftRail "activated agent" visuals, and the Options-
  drawer batch session-clear list all key off `(session_id ||
  codex_thread_id)` so Codex agents look identical to Claude ones.
  The DELETE endpoint was already runtime-agnostic (drops the whole
  `agent_sessions` row), so no server change beyond the SELECT.

**Recent (2026-05-01, follow-up) — Tier aliases for model selection:**

The Coach-facing surface for `coord_set_player_model` now leads with
TIER ALIASES (`latest_opus`, `latest_sonnet`, `latest_haiku`,
`latest_gpt`, `latest_mini`) rather than version-pinned concrete ids.
The whole point: when Anthropic ships Sonnet 4.7 (or OpenAI ships
GPT 5.6), the only file that needs editing is
[server/models_catalog.py](server/models_catalog.py)'s
`_ALIAS_TO_CONCRETE` map. `MODEL_GUIDANCE`, the tool description,
Coach's stored overrides, and the role defaults all stay correct
without touching prompts or migrating DB rows.

- **`_ALIAS_TO_CONCRETE`** maps each alias to today's concrete id.
  Update only this when a new top-tier model ships in any family.
- **`resolve_model_alias(value)`** — pure function, handles aliases /
  concrete ids / empty string uniformly. Called by `run_agent` after
  the resolution chain finalizes the model so downstream consumers
  (turns ledger, runtime fit, context-window estimate, the SDK call
  itself) see a concrete id.
- **Whitelists** include both aliases and concrete ids so the tool
  accepts either. The runtime split (`_ALIAS_RUNTIME`) gates aliases
  to the right runtime — `latest_opus` on a Codex-runtime player is
  rejected at SET time, same as a concrete Claude id would be.
- **`MODEL_GUIDANCE`** rewritten to use aliases exclusively. A new
  test (`test_model_guidance_uses_aliases_not_concrete_ids`)
  enforces that no concrete version number leaks into the prompt
  text — future maintainers can't backslide.
- **`_ROLE_MODEL_DEFAULTS` / `_ROLE_CODEX_MODEL_DEFAULTS`** also now
  alias-keyed (`coach: latest_opus`, `players: latest_sonnet`, Codex
  Players: `latest_mini`). `/api/team/models` resolves them to
  concrete ids in the `suggested` field so the UI hint matches its
  dropdown options. New helpers `role_defaults_concrete()` and
  `role_codex_defaults_concrete()` do the resolution at API time.
- **`available` / `available_codex`** in `/api/team/models` now
  exposes concrete ids only (`_CLAUDE_AVAILABLE` / `_CODEX_AVAILABLE`)
  — humans pick versions; aliases are an LLM convenience.
- New tests (6 total): `test_resolve_model_alias_round_trip`,
  `test_tool_accepts_alias_for_claude_player`,
  `test_tool_rejects_claude_alias_on_codex_player`,
  `test_tool_accepts_codex_alias_on_codex_player`,
  `test_role_defaults_resolved_for_api`, plus the
  forbidden-concrete-ids enforcer above. Suite at 562/562.

**Recent (2026-05-03) - Kanban task lifecycle:**

Tasks now flow through `plan -> execute -> audit_syntax ->
audit_semantics -> ship -> archive`. Coach plans and delegates;
Players execute, audit, and ship. Standard tasks use the full path
with `spec.md`, Player audit reports, and shipper sign-off. Simple
tasks self-audit before `coord_commit_push` and archive directly.

Key files: [server/tools.py](server/tools.py), [server/kanban.py](server/kanban.py),
[server/idle_poller.py](server/idle_poller.py), [server/static/kanban.js](server/static/kanban.js),
[server/tasks_claude_md.py](server/tasks_claude_md.py), and
[server/telegram_escalation.py](server/telegram_escalation.py). Full
subsystem detail lives in [Docs/kanban-specs.md](Docs/kanban-specs.md).

**Recent (2026-05-03) — Compass pinned to Sonnet + medium effort:**

Compass was previously letting the Claude Agent SDK fall through to
its built-in default for the model — accidentally inheriting whatever
the local CLI happened to ship with, with no effort specified. Now
the model + effort are explicit:

- **Default model**: `latest_sonnet` (alias resolved at call time via
  [server/models_catalog.py](server/models_catalog.py)). Cheap enough
  for routine audits + daily runs, capable enough for lattice +
  truth-corpus reasoning. Coach gets Opus for the hard work; Players
  get Sonnet for execution; Compass sits between them — same Sonnet
  tier as Players.
- **Default effort**: `medium`. Balances signal quality with token
  cost for the mid-stakes Compass pipeline (digest / audit / question
  generation / Tier B output body review).
- **Overrides**: `HARNESS_COMPASS_MODEL=<alias-or-concrete-id>` and
  `HARNESS_COMPASS_EFFORT=low|medium|high|max`. Both are env-only;
  no UI knob (Compass tuning isn't operator-facing per the
  established convention).
- **Resolution chain** in [server/compass/llm.py](server/compass/llm.py):
  explicit `model=` param → `HARNESS_COMPASS_MODEL` env →
  `LLM_MODEL_DEFAULT_ALIAS = "latest_sonnet"`. The chosen value
  passes through `resolve_model_alias` so the SDK + turns ledger see
  the concrete id, not the alias string. Aliases in the env var work
  too (e.g. `HARNESS_COMPASS_MODEL=latest_opus`).
- **9 new tests** in
  [server/tests/test_compass_llm.py](server/tests/test_compass_llm.py)
  cover: default → concrete Sonnet, explicit param wins, env override
  beats default, effort default = medium, valid effort values pass
  through, garbage effort drops to None, model + effort actually land
  in `ClaudeAgentOptions(...)`.
- Spec mirror in `Docs/compass-specs.md` §5.5.2 (last bullet).

When Anthropic ships Sonnet 4.7, only `_ALIAS_TO_CONCRETE` in
`models_catalog.py` needs updating — Compass picks it up
automatically on next process start.

**Recent (2026-05-02, second follow-up) — Tier B output body audits:**

The Compass auto-audit watcher (shipped earlier today) was extended to
audit `output_saved` events too — and not with a path-only stub but
with **actual body extraction** for text and office formats. Outputs
(binary deliverables saved via `coord_save_output`) are infrequent
but high-stakes: they're the polished PDFs / DOCX / spreadsheets the
human consumes directly. Cheaper to spend the LLM tokens reading the
document than to read it yourself and discover it's off-strategy.

- New module
  [server/compass/output_extractor.py](server/compass/output_extractor.py)
  with format-specific extractors. Lazy imports for pypdf /
  python-docx / openpyxl / python-pptx — missing parsers degrade to
  path-only audit, never crash. Per-format exception isolation: a
  malformed PDF doesn't tank the watcher.
- Format coverage:
  - Text-native (md/markdown/txt/csv/tsv/html/htm/json) → UTF-8 read
  - PDF → pypdf page-text concat
  - DOCX → python-docx paragraphs + table cells
  - XLSX → openpyxl read-only TSV dump per sheet
  - PPTX → python-pptx per-slide text frames
  - Archives (zip/tar/gz) → filename listing, first 200 entries
  - Images (png/jpg/etc.) → skipped (Tier C/vision deferred), path-only
  - Unknown → path-only
- Bodies capped at `MAX_BODY_CHARS=16_000` (~4k tokens) per file.
  Composed artifact capped again at 18 KB final.
- `AUDIT_SYSTEM` prompt
  ([server/compass/prompts.py](server/compass/prompts.py)) lightly
  extended to acknowledge the body-included artifact shape — same
  prompt handles both "metadata only" and "full document body" cases.
- 4 new pure-Python deps in pyproject:
  [pyproject.toml](pyproject.toml) — pypdf, python-docx, openpyxl,
  python-pptx. All wheel-only, no compile.
- 21 new tests in
  [server/tests/test_compass_output_extractor.py](server/tests/test_compass_output_extractor.py)
  cover every format extractor (success + missing-dep + corrupt-input
  paths), truncation, archive listing limits, fallback for images /
  unknown / missing-file / directory inputs.
- 4 new watcher tests in
  [server/tests/test_compass_audit_watcher.py](server/tests/test_compass_audit_watcher.py)
  cover the integration: text format inlines body, image falls back
  to path-only, missing file falls back gracefully, extractor crash
  falls back to path-only.
- Spec mirror in `Docs/compass-specs.md` §5.5.2 + §5.5.3.

The "every artifact gets audited against the Compass lattice" claim
in the marketing surface is now literally true: commits, decisions,
knowledge artifacts, AND binary deliverables all flow through the
audit pipeline.

**Recent (2026-05-02, follow-up) — Compass auto-audit watcher:**

The §5 spec put the burden on Coach to call `compass_audit` whenever
a worker produced "a meaningful unit of work." In practice Coach
forgets, and the dashboard's manual paste UI was the wrong fallback
(humans don't produce the artifacts being audited — agents do). New
[server/compass/audit_watcher.py](server/compass/audit_watcher.py)
closes the loop:

- **Subscribes to the bus** on boot (`start_audit_watcher` wired in
  `main.py:lifespan` next to the telegram bridge — same own-task-
  handle pattern). Subscribes synchronously *before* scheduling the
  consumer task to avoid losing events fired during the
  `create_task` race window.
- **Watched event types**: `commit_pushed` (Player commits via
  `coord_commit_push`), `decision_written` (Coach decisions via
  `coord_write_decision`), `knowledge_written` (any agent writing
  via `coord_write_knowledge`). Each composes a small artifact blob
  and dispatches `audit.audit_work` as a fire-and-forget task.
- **Gates** (each independent, all must pass):
  1. Per-project enable flag (`compass_enabled_<id>` truthy). Each
     event carries its own `project_id` (auto-stamped by `EventBus.publish`)
     so inactive-project commits still get audited.
  2. Team daily cost cap (`HARNESS_TEAM_DAILY_CAP`). Read live via
     `agents._today_spend()` so a deploy bumping the cap takes
     effect without restart.
  3. Per-(project, agent, type) debounce window (default 30s,
     `HARNESS_COMPASS_AUTO_AUDIT_DEBOUNCE`). A burst of commits on
     one Player collapses to one audit; different agents or different
     event types bypass the window.
  4. Global feature flag `HARNESS_COMPASS_AUTO_AUDIT` (default true).
     Set false to disable the watcher entirely on cost-constrained
     deploys.
- **Failure isolation**: `audit_work` exceptions are caught by an
  outer wrapper so a single bad LLM call doesn't kill the
  subscriber. `audit_work` itself already degrades to an `aligned`
  verdict on LLM failure.
- **Dashboard surface change** ([server/static/compass.js](server/static/compass.js)):
  the manual-paste textarea + "Audit" button is replaced by a
  read-only "about" block describing the auto-fire sources. Audit
  log + filter pills stay (§5.3 — humans pull when curious).
  `POST /api/compass/audit` HTTP endpoint kept as a debug backstop
  but not surfaced in the UI.
- **15 new tests** in
  [server/tests/test_compass_audit_watcher.py](server/tests/test_compass_audit_watcher.py):
  per-event-type dispatch, filter-by-type, enable-flag gating
  (unset and explicit-false), debounce collapse + key isolation,
  zero-debounce passthrough, cost-cap blocking + threshold passthrough,
  exception isolation, feature-flag short-circuit, idempotent start,
  missing-project_id graceful drop.
- **Spec mirror**: `Docs/compass-specs.md` §5.5 (full design),
  `Docs/recurrence-specs.md` §15.5 (cross-reference table noting the
  watcher is NOT a recurrence — different trigger / cardinality /
  cost-cap location / lifecycle owner).

**Recent (2026-05-01) — Compass module shipped:**

Compass is an autonomous strategy engine that runs **alongside** the
team — it maintains a per-project lattice of weighted statements
about the project, asks the human focused questions, and exposes its
current best guess to Coach via four MCP tools. It never dispatches
work, never amends truth without human approval, and never blocks
Players. Spec: [Docs/compass-specs.md](Docs/compass-specs.md).

- **Per-project, opt-in.** State lives at
  `/data/projects/<id>/working/compass/` (mirrored synchronously to
  `kDrive:projects/<id>/compass/`). Enable per project via the
  Compass dashboard; flag stored in
  `team_config['compass_enabled_<id>']`. Switching active project
  switches the visible Compass — every code path resolves
  `compass_paths(project_id)` against the live active project, so
  per-project state is fully isolated and reloaded on switch.
- **MCP tools (Coach-only)** in
  [server/tools.py:build_coord_server](server/tools.py):
  `compass_ask(query)` returns a terse markdown answer citing
  statement ids/weights; `compass_audit(artifact)` runs an audit and
  returns one of `aligned` / `confident_drift` / `uncertain_drift`;
  `compass_brief()` returns the latest daily briefing;
  `compass_status()` returns counts + last-run timestamps. All four
  reject Players with the documented "Coach-only — Players read
  Compass via the CLAUDE.md block" error and short-circuit when the
  per-project enable flag is unset.
- **State files (JSON, kDrive-mirrored)** in
  [server/compass/store.py](server/compass/store.py):
  `lattice.json`, `regions.json`, `questions.json`,
  `audits.jsonl`, `runs.jsonl`, `claude_md_block.md`,
  `briefings/briefing-YYYY-MM-DD.md`, and three proposal files
  (`proposals/settle.json`, `proposals/stale.json`,
  `proposals/duplicates.json`). Atomic writes via tempfile +
  `os.replace`; synchronous kDrive mirror per write. **No
  `truth.json`** — see "Truth is folder-backed" below.
- **Truth is folder-backed and spans three lanes.** Compass reads
  truth-bearing material from THREE sources, all walked fresh on
  every `load_state` call by the adapter at
  [server/compass/truth.py](server/compass/truth.py):
  1. `<project>/truth/**/*.{md,txt}` — the dedicated truth lane,
     human-vetted (humans edit via the Files pane; Coach proposes via
     `coord_propose_file_write(scope='truth', ...)`; agents are blocked
     by a PreToolUse hook).
  2. `<project>/project-objectives.md` — the human's authored
     objectives file at the project root.
  3. `/data/wiki/<project_id>/**/*.{md,txt}` — the per-project wiki
     tree (agent-curated knowledge: gotchas, stakeholder preferences,
     glossary entries, domain rules, decisions context). Authored via
     the LLM-Wiki skill; less vetted than the first two but the human
     keeps a curating role and the corpus captures intent / users /
     UX / context the truth lane often omits. Cross-project wiki at
     `/data/wiki/*.md` is NOT included; only the per-project sub-tree.
  All three drive truth-derive (Stage 0a) and truth-check (§3.7)
  identically. The dashboard distinguishes them by relpath prefix
  (`truth/...`, `project-objectives.md`, `wiki/...`) for display + link
  routing only — the LLM treats them uniformly. The `TruthReference`
  card is read-only; there is no `POST /api/compass/truth` for
  adding/editing/removing — edits happen via the Files pane (and via
  `coord_propose_file_write` for the truth lane). The truth-conflict
  modal's "amend truth" path points the human at the offending file
  path; for wiki sources the displayed path is
  `/data/wiki/<id>/<rest>` instead of `<project>/<relpath>`.
- **Stage 0 — truth ingestion (two sub-stages).** Before the answer-
  digest stage, the runner reads the truth corpus fresh and runs:
  - **0a Truth-derive** — propose lattice statements representing
    what truth implies. New statements start at `weight=0.75`,
    `created_by="compass-truth"`. Idempotent via SHA-256 over the
    corpus stored in `team_config['compass_truth_hash_<id>']`:
    unchanged hash → skip the LLM entirely; changed hash → run derive
    (LLM is also told to skip statements already represented).
    See [server/compass/pipeline/truth_derive.py](server/compass/pipeline/truth_derive.py).
  - **0b Reconciliation** — when the corpus hash changes AND the
    pre-derive lattice was non-empty, scan active + archived/settled
    rows against the corpus for contradictions. Each conflict becomes
    a `ReconciliationProposal` (a fourth proposal type alongside
    settle/stale/dupe) with three resolution paths: `update_lattice`
    (sub-actions: unarchive / flip / reformulate / replace),
    `update_truth` (informational; routes the human to the Files
    pane), `accept_ambiguity` (sets `reconciliation_ambiguity`
    flag, suppresses re-detection until the corpus shifts).
    Pending proposals expire after `PROPOSAL_EXPIRY_RUNS=5` runs of
    being ignored. See
    [server/compass/pipeline/reconciliation.py](server/compass/pipeline/reconciliation.py).
  Net effect: as long as truth exists, the lattice has an immediate
  basis on the very first run; on subsequent runs an edited truth
  file both adds new lattice rows AND surfaces any rows the new
  corpus contradicts (especially settled ones, which Coach treats as
  binding). The corpus hash is persisted only after BOTH sub-stages
  complete cleanly so a partial failure re-attempts next run.
- **`POST /api/compass/proposals/reconcile/{id}`** resolves a
  reconciliation proposal. Mutate helpers in
  [server/compass/mutate.py](server/compass/mutate.py):
  `reconcile_unarchive`, `reconcile_flip_archive`,
  `reconcile_reformulate`, `reconcile_replace`,
  `reconcile_accept_ambiguity`. Dashboard renders the
  `ReconciliationProposalCard` in the lattice column under the dupe
  cards. The `compass_reconciliation_proposed` bus event emits when
  fresh conflicts appear; `compass_proposal_resolved` (with
  `kind="reconcile"`) emits on resolution.
- **Pipeline stages** under
  [server/compass/pipeline/](server/compass/pipeline/) — pure
  functions that take state and return proposed updates:
  `digest.passive` / `digest.answer` (with delta clamping),
  `questions.generate_batch` / `generate_single` (predict-before-
  ask discipline; entries without prediction dropped per §10.18),
  `reviews.propose` (settle + stale candidates pre-filtered in pure
  Python before the LLM phrases questions), `reviews.detect_duplicates`,
  `regions.auto_merge` (only fires above
  `REGION_SOFT_CAP=15`; re-tags active AND archived statements per
  §10.11), `truth_check.check`, `briefing.generate`,
  `claude_md.generate` + `inject` (marker-delimited, idempotent).
- **Runner**
  ([server/compass/runner.py](server/compass/runner.py)) orchestrates
  spec §3.1-§3.10 in order: digest answers (truth-check first), passive
  digest, region merge, reviews + duplicate detection, generate
  questions, briefing (skipped on bootstrap), CLAUDE.md block. Daily
  mode requires `presence.human_reachable` (recent
  `messages.from_id='human'` row OR a heartbeat from
  `/api/compass/heartbeat` within
  `HARNESS_COMPASS_PRESENCE_HOURS`, default 24h). Per-project
  asyncio.Lock prevents concurrent runs.
- **Audit** ([server/compass/audit.py](server/compass/audit.py))
  appends to `audits.jsonl`, queues a question on `uncertain_drift`
  (with prediction; `confident_drift` does NOT queue per §10.5), and
  runs a §5.4 rollup safety net every `AUDIT_ROLLUP_INTERVAL=5`
  audits — if ≥3 recent drifts cluster in one region, queues a meta-
  question asking whether the lattice itself is wrong.
- **Scheduler**
  ([server/compass/scheduler.py](server/compass/scheduler.py)) is a
  background task in `lifespan` next to `recurrence_scheduler_loop`.
  Polls every `HARNESS_COMPASS_SCHEDULER_TICK=300s`, walks every
  project where `compass_enabled_<id>` is truthy, fires `bootstrap`
  on first activation and `daily` once per UTC day after
  `DAILY_RUN_HOUR_UTC=9`. One project per iteration to avoid spawning
  storms.
- **HTTP API** under `/api/compass/*`
  ([server/compass/api.py](server/compass/api.py)) — `GET /state`,
  `POST /enable` / `/disable`, `POST /run`, `POST /heartbeat`,
  `POST /qa/{start,next,answer,end}` (Q&A session with immediate
  digest), `POST /questions/{id}/answer`,
  `POST /proposals/{settle,stale,dupe}/{id}`,
  `POST /statements/{id}/{weight,restore}`, `POST /truth`,
  `POST /ask`, `POST /audit`, `POST /inputs`, `GET /briefings/{date}`,
  `GET /runs`, `GET /audits`, `POST /reset`. Phase events stream via
  the existing `/ws` channel (`compass_phase`,
  `compass_run_completed`, `compass_question_queued`,
  `compass_question_digested`, `compass_proposal_resolved`,
  `compass_truth_changed`, `compass_truth_contradiction`,
  `compass_audit_logged`, `compass_reset`, `compass_llm_call`).
- **Dashboard** at slot `__compass`
  ([server/static/compass.js](server/static/compass.js) + 
  [server/static/compass.css](server/static/compass.css)) — paper-
  free harness-styled v1 (deferred the navigator's-logbook treatment).
  Three-column workspace: Lattice (capacity bar, statement rows with
  weight bars + NO/½/YES override buttons routed through a
  confirmation modal, archived `<details>`, settle/stale/dupe
  proposal cards), Inputs+Questions (kind-typed input row;
  question cards with hidden-by-default prediction reveal), Briefing
  (renders via the existing `marked + dompurify` pipeline) +
  CLAUDE.md block + Ask Compass. Plus Audits section, Run history
  footer, OverrideModal, TruthConflictModal, and a sticky-bottom
  QASessionOverlay (Q&A session with immediate digest, per §4 + §14.11).
  All glyphs are CSS-drawn or inline SVG per the no-emoji rule.
  LeftRail entry: a CSS-drawn compass-rose SVG button next to the
  files-open icon.
- **Cost tracking.** Compass calls
  `claude_agent_sdk.query()` directly with a one-shot prompt (no MCP,
  no resume) and inserts a row into the existing `turns` ledger
  under `agent_id="compass"`, `runtime="claude"`, with
  `cost_basis="compass:passive"` / `compass:answer` /
  `compass:audit` / etc. so usage rolls up alongside agent turns.
  No `ANTHROPIC_API_KEY` path — Max-OAuth invariant preserved.
- **Tests** — 117 new Compass tests across
  [test_compass_paths.py](server/tests/test_compass_paths.py),
  [test_compass_store.py](server/tests/test_compass_store.py),
  [test_compass_llm.py](server/tests/test_compass_llm.py),
  [test_compass_mutate.py](server/tests/test_compass_mutate.py),
  [test_compass_pipeline.py](server/tests/test_compass_pipeline.py),
  [test_compass_claude_md_inject.py](server/tests/test_compass_claude_md_inject.py),
  [test_compass_audit.py](server/tests/test_compass_audit.py),
  [test_compass_runner.py](server/tests/test_compass_runner.py),
  [test_compass_presence.py](server/tests/test_compass_presence.py),
  [test_compass_mcp_tools.py](server/tests/test_compass_mcp_tools.py),
  [test_compass_api.py](server/tests/test_compass_api.py). All 587
  harness tests pass.

**Recent (2026-05-01) — Coach can set Player models:**

- **`coord_set_player_model(player_id, model)`** — Coach-only MCP
  tool ([server/tools.py](server/tools.py)). Stores a per-(slot,
  project) model preference on a new `agent_project_roles.model_override`
  column. Empty string clears (no orphan row is created when
  clearing on a never-touched Player). Validated against the player's
  currently-resolved runtime (Claude vs Codex); a Codex id on a
  Claude-runtime player is rejected at SET time. Emits
  `agent_model_set` with `to: <player_id>` so the event lands in
  both Coach's pane (actor) and the target Player's pane (fan-out).
  Tool added to `_tools` registry + `ALLOWED_COORD_TOOLS`.
- **Catalog refactor** — model whitelists, role defaults, and the
  new `MODEL_GUIDANCE` policy block live in a dedicated
  [server/models_catalog.py](server/models_catalog.py); both `main.py`
  and `tools.py` import from there (no more lazy import inside the
  tool body). `_model_fits_runtime` now uses positive enumeration via
  `model_is_claude` / `model_is_codex` instead of a `claude-*` prefix
  heuristic, so a hypothetical future Anthropic id without that prefix
  isn't silently misclassified.
- **Coach's system prompt** ([server/agents.py:_system_prompt_for])
  gets two additions: (a) a bullet listing `coord_set_player_model`
  in the tool catalogue with a "read the policy below first" pointer;
  (b) a `MODEL_GUIDANCE` block at the bottom that codifies the team
  rule: **model change is the EXCEPTION**, Sonnet is the Player default,
  Opus only for hard reasoning, Haiku only for trivial mechanical work,
  Codex is the rate-limit fallback (`gpt-5.4-mini` as the Sonnet
  equivalent, top tier reserved for heavy work). Players don't get
  the policy block — they can't call the tool.
- **Default Codex model for Players** changed from "" (SDK default) to
  `gpt-5.4-mini` so a deployment that flips a Player to Codex without
  setting an override gets the mini tier, not the top tier — same
  cost-discipline shape as the Claude side.
- **Resolution chain** in [server/agents.py:run_agent](server/agents.py)
  is now: per-pane request `model` → `agent_project_roles.model_override`
  (Coach-set) → runtime-aware per-role default in `team_config` →
  SDK default. The Coach override is silently dropped at spawn time
  if it no longer fits the current runtime — protects against the
  case where Coach picked a Claude model and the player later flipped
  to Codex (or vice-versa).
- **Project-switch correctness**: overrides are keyed by `(slot,
  project_id)`, so switching active projects swaps overrides
  automatically. New test exercises set-on-misc, switch-to-alpha
  (override gone), set-different-on-alpha, switch-back-to-misc
  (original override restored).
- **EnvPane warning** — when any Player has a non-NULL `model_override`,
  a new `EnvModelOverridesSection` renders an amber-tinted card listing
  the slots and their picked models with the policy reminder
  ("review and clear any that aren't load-bearing"). Hidden when no
  overrides are active.
- **`GET /api/agents`** now returns `model_override`. The
  `agent_model_set` event is included in `/api/events` fan-out for
  the target slot so opening the Player's pane after a refresh
  shows the historical event.
- Tests in
  [server/tests/test_player_model_override.py](server/tests/test_player_model_override.py)
  (17 tests, all passing): registration, schema migration, Coach-only
  enforcement, invalid-id / unknown-model / runtime-mismatch
  rejection, set-clear round-trip, runtime-fit positive enumeration,
  project-isolation across project switches, the full `run_agent`
  resolution chain, runtime-mismatch silent drop, empty-clear
  no-orphan-row, and `MODEL_GUIDANCE` injection into Coach's prompt.

**Recent (2026-04-30) — Codex MCP cache invalidation:**

- **`evict_client` / `evict_all_clients`** in
  [server/runtimes/codex.py](server/runtimes/codex.py). The Codex
  app-server subprocess captures `mcp_servers` at spawn time, so a
  newly-added MCP server in the UI was invisible to any agent whose
  Codex client was already cached. Symptom: smoke-test green, agent
  reports "no Zeabur MCP tool exposed in this session." Fix: the
  session-clear endpoints (`DELETE /api/agents/{id}/session` and
  `POST /api/agents/sessions/clear`) and the MCP CRUD endpoints
  (`POST/PATCH/DELETE /api/mcp/servers/...`) now invalidate the
  cache. Idle slots get a full `close_client`; in-flight turns get
  cache-popped only so the live turn can finish on its own client
  reference. Next turn rebuilds the subprocess with the current MCP
  list. ClaudeRuntime is unaffected (it builds `ClaudeAgentOptions`
  fresh per turn). Tests in
  [server/tests/test_codex_runtime_gate.py](server/tests/test_codex_runtime_gate.py)
  cover idle-close, in-flight-pop, and the mixed `evict_all_clients`
  case. Spec mirror in [Docs/CODEX_RUNTIME_SPEC.md](Docs/CODEX_RUNTIME_SPEC.md) §E.1.

- **External MCP servers pre-approved for Codex.** The
  `default_tools_approval_mode = "approve"` policy that unblocked
  `coord` for Coach (read-only) on 2026-04-29 only covered the coord
  server. Every UI-added external MCP server (Zeabur, Notion, etc.)
  hit the same cancellation path: Codex routed the call through
  elicitation/approval, the embedded app-server client had no
  approval handler, and the call auto-cancelled with "user rejected
  MCP tool call". `_build_mcp_servers` now injects
  `default_tools_approval_mode = "approve"` on every external server
  unless the saved config already specifies a value (opt-out
  preserved). Players (`danger-full-access`) skip approval anyway so
  they were already fine. Spec mirror in `Docs/CODEX_RUNTIME_SPEC.md` §C.

- **Health probe `mcp_external` reflects DB-managed servers.** The
  probe in [server/main.py](server/main.py) used to short-circuit to
  "skipped — HARNESS_MCP_CONFIG not set" whenever the legacy env
  path was unset, ignoring the UI-managed `mcp_servers` table
  entirely. So adding a server via the Options drawer left the
  Settings pane reading "only coord active" even when the new server
  was loaded and serving tools. Probe now always calls
  `load_external_servers()` (which merges file + DB) and reports the
  merged count. The `skipped` flag is only set when both sources
  yield zero servers.

**Recent (2026-05-02) — Coach Codex orchestration unblock:**

User reported a cascade where Coach (under Codex runtime) repeatedly
hit "blocked by safety policy" errors when trying to activate a fresh
Player. Root cause was a missing tool, not a real harness block —
Coach hallucinated a `runtime_override` kwarg on `coord_set_player_model`
because there was no MCP path to actually flip a Player's runtime,
and the cascade of failed attempts within the turn eventually tripped
OpenAI's Codex safety monitor, which cancelled the subsequent
`coord_assign_task` call.

- **`coord_set_player_runtime(player_id, runtime)` MCP tool**
  ([server/tools.py](server/tools.py)). Coach-only, p1..p10 (cannot
  flip Coach itself — HTTP-only path). `'codex'` rejected when
  `HARNESS_CODEX_ENABLED` is unset. Mid-turn flips rejected (mirrors
  the HTTP endpoint's 409). Existing `model_override` preserved
  across the flip; spawn-time silently drops it if it doesn't fit
  the new runtime. Emits `runtime_updated` with `agent_id=pid`
  matching the HTTP `PUT /api/agents/{id}/runtime` shape — the event
  lands in the target Player's pane regardless of who initiated the
  flip. Coach's natural `tool_use`/`tool_result` pair already records
  the action in Coach's timeline, so a duplicate `runtime_updated`
  there would be noise; and `runtime_updated` isn't a fan-out type in
  either the WS-side handler or the `/api/events` SQL filter, so a
  `to` field would be dead weight. Side-effect:
  invalidates the Codex client cache for the slot
  (`evict_client`) so a codex→claude flip doesn't leak the cached
  subprocess + proxy token until the next MCP-config change.
  Bumped `_CODEX_TOOL_CONTRACT_VERSION` to
  `2026-05-02.coord-set-player-runtime` so existing Codex threads
  get cleared on next boot and pick up the new tool.

- **Cross-runtime model error message** in
  `coord_set_player_model`. Previously the validator emitted
  "unknown Claude model 'gpt-5.5' for p8 (runtime=claude)" when
  Coach tried a Codex model on a Claude-runtime Player. Coach
  paraphrased that as "harness safety layer blocked me" and gave up.
  Now the validator detects the cross-runtime case via
  `model_is_claude` / `model_is_codex` and returns an actionable
  pointer at `coord_set_player_runtime` — no more dead-end errors.
  `MODEL_GUIDANCE` in
  [server/models_catalog.py](server/models_catalog.py) updated
  to mention the new tool; Coach's tool catalogue in
  [server/agents.py](server/agents.py) lists it ahead of
  `coord_set_player_model` with a "required first" note.

- **Codex `thread/resume` retry on `CodexTimeoutError`**
  ([server/runtimes/codex.py:open_thread](server/runtimes/codex.py)).
  The SDK's default `request_timeout` is 30s; under load (cold
  app-server subprocess, slow Codex backend, large stored thread
  state — Coach especially) `thread/resume` can transiently exceed
  it. Previously every exception cleared the stored
  `codex_thread_id` and fell back to `start_thread`, costing the
  agent its thread continuity for what was usually a transient
  blip. Now `CodexTimeoutError` retries 2× (3 attempts, 1s gap)
  before falling back; `session_resume_failed` only fires once
  retries are exhausted. Other exception classes (CodexProtocolError
  "thread not found", transport errors) skip the retry — they're
  not transient. Spec mirror in `Docs/CODEX_RUNTIME_SPEC.md` §E.2.

- **Codex monitor cancellation rendered as error.**
  `_step_payload_is_error` in
  [server/runtimes/codex.py](server/runtimes/codex.py) previously
  treated only `status` containing `"error"` / `"fail"` as an error.
  OpenAI's Codex safety monitor surfaces a cancelled tool call as
  a "completed" item with `status='cancelled'` (or similar) and a
  prose explanation in the body — these used to render green like
  a successful tool result, leaving the user no way to tell a
  monitor refusal from a real success without reading every body.
  Now `"cancel"` and `"reject"` patterns also fire the error path.
  The same change covers `state` key as a fallback for `status`.

- **Codex auto-compact wired up.**
  `CodexRuntime.maybe_auto_compact` previously returned False
  unconditionally — the original "context-pressure signal isn't
  exposed yet" rationale was stale once
  `_codex_session_context_estimate` shipped (it reads the latest
  `turns` row for the resumed thread and reconstructs prompt+output
  tokens in the same shape Claude's JSONL probe produces; the UI
  context bar already used it). Trip-wire now mirrors Claude's:
  honors the shared `HARNESS_AUTO_COMPACT_THRESHOLD` env (default
  0.7), short-circuits on `compact_mode` / unparseable threshold /
  no `codex_thread_id`, computes `used / window` against
  `_context_window_for(tc.model)`, emits `auto_compact_triggered`
  with the same payload shape, then delegates to `run_manual_compact`
  so the actual compaction goes through the **native**
  `client.compact_thread(thread_id)` endpoint (not a `COMPACT_PROMPT`
  LLM round-trip). The dispatcher then runs the user's original
  prompt on a fresh thread that picks up the continuity note from
  the system prompt. Failure paths (auth gone, ImportError,
  app-server exception) emit `auto_compact_failed` symmetrically with
  Claude. Spec mirror in `Docs/CODEX_RUNTIME_SPEC.md` §A.5 / §E.6.

**Recent (2026-05-02, follow-up) — Env-toggle attention signal + auto-pop-open:**

The pending-review signal on the left-rail env-toggle button used to
fire only on pending file-write proposals (a small red pip in the
corner). Now it covers everything the EnvPane surfaces for human
action: AskUserQuestion prompts routed to the human
(`pending_question`), ExitPlanMode plan approvals (`pending_plan`),
`human_attention` escalations from `coord_request_human`, plus the
existing file-write proposals. The attention state
(`pendingHumanQuestions`, `pendingHumanPlans`, `persistedAttention`,
`dismissedAttention`) lifted from `EnvAttentionSection` to App scope
in [server/static/app.js](server/static/app.js) so both the visual
signal and the auto-open work whether or not the EnvPane is mounted.

Visual: the corner pip is gone. The whole ▦ icon recolours amber
(`var(--warn)`) and a soft amber glow pulses around the button —
same `box-shadow` keyframe shape as `.slot.state-working` on the
agent buttons, so "needs attention" reads consistently across the
rail. CSS lives at `.gear.env-toggle.has-pending` in
[server/static/style.css](server/static/style.css).

Auto-open: an App-scope `useRef` tracks the previous
`envPendingCount` and `setEnvOpen(true)` fires on every positive
transition. Page-load with leftover items lands as 0 → N (auto-
open once); dismissals (N → 0) never re-trigger; a fresh item
arriving while the pane is closed pops it open. The
EnvAttentionSection component is now purely presentational —
receives `open` / `onDismiss` / `onDismissAll` as props.

**Recent (2026-05-02, third follow-up) — Runtime session transfer (compact + flip):**

Switching an agent's runtime used to be an all-or-nothing flip.
`PUT /api/agents/{id}/runtime` writes `runtime_override` and the next
turn on the new runtime starts with no memory of the prior
conversation — `session_id` and `codex_thread_id` are runtime-
specific and cannot cross over. Users had to manually `/compact`
first, then flip, and remember the order.

`POST /api/agents/{id}/transfer-runtime {runtime}` does both atomically:

- **No prior session on source runtime** → flip immediately, emit
  `runtime_updated` + `session_transferred(note=no_prior_session)`.
- **Prior session exists** → schedule a transfer-mode compact on the
  current runtime; on success the runtime flips and
  `session_transferred(from_runtime, to_runtime, ...)` fires; on
  empty-summary failure (Claude only — Codex's `compact_thread` has
  already cleared the thread) `session_transfer_failed` fires and
  the runtime stays put.
- **Same target runtime** → 200 noop.
- **Mid-turn (status='working')** → 409 (cancel first).

Plumbed via a new `transfer_to_runtime` field on `TurnContext` /
`run_agent` kwargs / `turn_ctx`. Each runtime's compact handler
reads it after the post-compact bookkeeping (`continuity_note`
written, source session id nulled) and calls
`_perform_runtime_transfer_flip(slot, target)` — flips the column,
nulls the **other** runtime's session column too (defensive against
orphaned thread ids from a prior life on the target), evicts any
cached Codex client, and emits `runtime_updated` with
`source=session_transfer`.

Surfaces:
- **Pane gear popover** picks claude/codex via the new endpoint;
  picking `default` (empty) keeps the legacy blunt-clear PUT.
- **`coord_set_player_runtime`** (Coach MCP tool) reroutes through
  the same path: queues a transfer-mode compact via
  `asyncio.create_task` when there's a session to carry, flips
  immediately when there isn't, blunt-clears on `runtime=''`.
- **Three new event types** (`session_transfer_requested`,
  `session_transferred`, `session_transfer_failed`) render as
  `.sys` rows in [server/static/app.js](server/static/app.js) so
  the timeline labels the boundary as a transfer, not a compact.
  Context-bar handler also drops to 0 on `session_transferred`
  (the sessions-cleared trio became a quartet).

Spec mirror: `Docs/CODEX_RUNTIME_SPEC.md` §E.8. Tests in
[server/tests/test_runtime_transfer.py](server/tests/test_runtime_transfer.py)
cover the helper, HTTP endpoint validation + dispatch matrix, MCP
tool routing including the empty-clear blunt path and the
prior-session queued path, and the `TurnContext.transfer_to_runtime`
schema.

**Recent (2026-05-02, sixth follow-up) — Role-level defaults wired through:**

`_ROLE_MODEL_DEFAULTS` in
[server/models_catalog.py](server/models_catalog.py) was previously
"suggested only" — surfaced as the `suggested` field of
`/api/team/models` for the Settings drawer hint, but never actually
consulted by the spawn-time resolution chain. So a fresh deploy with
no human-set `team_config` rows (`coach_default_model` /
`players_default_model`) fell straight through to the SDK default
(sonnet 4.6 for everyone, including Coach — wrong, since Coach should
be on Opus). Likewise effort had no role default; turns ran without a
thinking-budget hint unless the human or Coach explicitly set one.

- `_get_role_default_model` in [server/agents.py](server/agents.py)
  now falls through to `models_catalog.role_default_model` when
  team_config is empty / unreadable. So a clean deploy gets
  `latest_opus` for Coach and `latest_sonnet` for Players (Codex
  Coach: `latest_gpt`, Codex Players: `latest_mini` — Codex Coach
  was historically empty for cost reasons but mirroring the Claude
  Coach=Opus/Players=Sonnet shape eliminates the cold-start gap
  where the chip had to display the runtime tag).
  Human-set team_config rows still win when present.
- `run_agent` resolution chain for `effort` and `plan_mode` gets a
  third tier: after per-pane override → Coach-set override →
  **role-level default** (`role_default_effort` /
  `role_default_plan_mode` in
  [server/models_catalog.py](server/models_catalog.py)). Effort
  defaults to medium (=2) for both Coach and Players. Plan mode
  default is False for both — the same value the prior code path
  produced, but now declared in one table for symmetry.
- `coord_get_player_settings` ([server/tools.py](server/tools.py))
  surfaces the resolved effort / plan defaults so Coach sees
  `medium (default)` / `off (default)` instead of bare `default` —
  more useful when planning a `coord_set_player_*` call.
- Tests adjusted:
  `test_run_agent_no_override_no_pane_falls_through_to_default`
  now asserts `effort == 2`;
  `test_get_role_default_model_does_not_fall_back_to_claude_for_codex`
  now asserts the Codex players fallback returns `latest_mini`
  (the hardcoded Codex default) rather than None.

**Recent (2026-05-02, fifth follow-up) — Telegram escalation watcher:**

The bridge already forwards `human_attention` events (from
`coord_request_human`) to Telegram immediately, but the three other
"needs the human" event types — `pending_question(route='human')`,
`pending_plan(route='human')`, `file_write_proposal_created` — only
surfaced in the EnvPane attention strip. If the human walked away
from the laptop, those items sat unanswered indefinitely. Closed
the gap with
[server/telegram_escalation.py](server/telegram_escalation.py):

- **Per-item asyncio timer.** On every watched pending event, a
  task keyed by `(kind, correlation_id|proposal_id)` is registered
  in `_pending`. The matching resolution event (`question_answered`
  / `question_cancelled` / `plan_decided` / `plan_cancelled` /
  `file_write_proposal_{approved,denied,cancelled,superseded}`)
  cancels the task. Duplicate pendings replace the prior timer so
  one item never has two competing fire paths.
- **Web-active vs web-inactive delays.** Decision happens at
  schedule time (`bus.subscriber_count` check). Active web →
  `HARNESS_TELEGRAM_ESCALATION_SECONDS` (default 300s). Inactive →
  `HARNESS_TELEGRAM_ESCALATION_GRACE` (default 5s) so a quick
  reload still catches the item before the phone pings. Setting
  `HARNESS_TELEGRAM_ESCALATION_SECONDS=0` disables the watcher
  entirely; the consumer still drains the bus queue (otherwise it
  would back up) but does nothing with events.
- **Telegram config resolved at fire time.** Calls
  `server.telegram.send_outbound(text)` — new public helper that
  reuses the bridge's `_resolve_config` + chunked `_send_telegram`
  via a fresh `httpx.AsyncClient`. When the bridge is disabled /
  unconfigured the helper returns False and the watcher silently
  no-ops, so the Clear button in Options drawer is respected
  without any watcher-side state.
- **Context-rich Telegram message.** Each kind has a dedicated
  formatter that includes the agent's slot + name + role label
  (looked up via `_get_agent_identity`), the `ts` / `deadline_at`
  rendered as `HH:MM UTC`, the structured questions array (or
  plan body, or file-path + summary), and a "Open the web UI to
  answer" footer. Bodies are truncated at 1500 chars with an
  ellipsis so a long plan doesn't blow Telegram's 4096-char cap
  (the bridge's `_split_chunks` handles overflow regardless).
- **`human_attention` keeps its existing immediate-fire path** in
  the bridge's outbound loop. The agent has explicitly declared
  "I can't proceed" — adding a delay there would only slow the
  signal the user wants fastest.
- **Lifecycle**: `start_escalation_watcher()` /
  `stop_escalation_watcher()` mirror the audit watcher's pattern
  (own task handle, subscribe synchronously before
  `create_task`-ing the consumer). Wired in
  [server/main.py:lifespan](server/main.py) right after the
  bridge. Stop cancels in-flight timers so a redeploy doesn't
  fire stale escalations on the next boot.
- **Restart limitation (v1)**: timers are in-memory only. A
  `file_write_proposal_created` that arrives before a deploy
  keeps its `status='pending'` row in the DB but doesn't re-arm a
  timer on next boot. The EnvPane still surfaces it on reconnect
  so the signal isn't lost — it just won't trigger Telegram
  unless a fresh proposal lands. Replay-on-boot is a possible v2
  extension.
- **Tests** in
  [server/tests/test_telegram_escalation.py](server/tests/test_telegram_escalation.py)
  (29, all passing) cover key extraction, env knobs (default /
  zero / negative / invalid), formatters across all three kinds,
  schedule-cancel via resolution event, fire-on-timeout,
  web-active long-delay branch, disabled / no-op paths,
  route='coach' filtering, duplicate replacement, idempotent
  start, stop cancels in-flight timers, plus an end-to-end
  through-the-bus path.

**Recent (2026-05-02, fourth follow-up) — Coach effort + plan-mode overrides:**

Coach already controlled per-Player runtime + model via
`coord_set_player_runtime` / `coord_set_player_model`. The two
remaining knobs the human sets per-pane (reasoning effort, plan-mode)
were Coach-blind — Coach couldn't influence them on auto-wake spawns
(task assignments, direct messages) and had no way to read their
current state. Closed both gaps:

- **`agent_project_roles.effort_override`** (INTEGER 1..4) +
  **`plan_mode_override`** (INTEGER 0/1) columns added via
  `_ensure_columns` migration, mirroring the `model_override` shape
  (NULL = no override; per-(slot, project) scoped so switching active
  projects swaps overrides automatically). Tests in
  [server/tests/test_player_effort_plan_overrides.py](server/tests/test_player_effort_plan_overrides.py).
- **Helpers**: `_get_agent_effort_override` /
  `_get_agent_plan_mode_override` in
  [server/agents.py](server/agents.py). Effort returns int|None with
  out-of-range coercion to None (defensive — a future schema check
  should keep it in range, but a stale value never blocks a spawn).
  Plan-mode returns bool|None tri-state.
- **Resolution chain in `run_agent`**: per-pane request value (highest)
  → Coach override → default (False / no thinking budget). To distinguish
  "human turned plan-mode off" from "no per-pane override", `plan_mode`
  param + `StartAgentRequest.plan_mode` flipped to `bool | None` with
  None = "consult override". The UI already omits the field when its
  toggle is off, so the wire format stays compatible. `effort` was
  already `int | None` so no shape change there.
- **MCP tools** (Coach-only) in
  [server/tools.py](server/tools.py):
  - `coord_set_player_effort(player_id, effort)` — accepts
    'low'/'medium'/'high'/'max', friendly aliases ('med'), or the
    numeric tier 1..4 for symmetry with the UI slider; empty clears.
  - `coord_set_player_plan_mode(player_id, plan_mode)` — accepts
    'on'/'off' + bool aliases; empty clears.
  - `coord_get_player_settings(player_id?)` — Coach-only read tool.
    Renders a compact text table (slot / name / runtime / model /
    effort / plan) showing both override and resolved values for one
    Player or the full roster. Coach is told to call this BEFORE any
    `coord_set_player_*` so they don't re-set what's already correct.
- **Coach prompt visibility**: when at least one override is active,
  the `## Team composition` block grows an `### Active overrides`
  sub-section listing the slots and their non-default knobs. Skipped
  entirely when the team is on defaults — no token cost in the common
  case. Coach's tool catalogue lists the three new tools alongside
  the existing `coord_set_player_runtime` / `coord_set_player_model`.
- **Events**: `agent_effort_set` and `agent_plan_mode_set` mirror
  `agent_model_set` — `{to: pid, player_id: pid}` shape so the UI's
  fan-out machinery renders the row in both Coach's pane (actor) and
  the target Player's pane. `/api/events` SQL filter extended to
  match the same `payload_to` indexed branch on history reload.
- **EnvPane**: `EnvModelOverridesSection` generalized into
  `EnvOverridesSection` covering all four knobs. Renders nothing when
  no overrides are active.
- **`GET /api/agents`** now returns `effort_override` /
  `plan_mode_override` alongside `model_override` so the UI doesn't
  need a second round-trip.
- 19 new tests across registration, schema, validation (alias
  normalization, friendly + numeric forms, invalid value rejection),
  set/clear round-trip via the new helpers and `_get_agent_identity`,
  empty-clear no-orphan invariant, event emission, Coach-only
  enforcement, and the `coord_get_player_settings` shape (single
  player + full roster). Resolution-chain integration tests stub the
  runtime via the `test_runtime_dispatch` pattern to assert pane→Coach
  override→default precedence end-to-end. Suite at 879/879.

**Next likely:**
- **Mobile UI polish** — touch-drag doesn't work with HTML5 DnD;
   layout breakpoints for < 900 px need a rethink.
- **Pane collapse / minimize** — currently panes are all-or-nothing
   open. A "minimize to header" state would help watching many stacks.
- **Whole-team conversation export** — combine all open panes into
   one markdown file with agent-prefixed headings.
- **Task ↔ message link** — schema relation so 're: t-42' queries work.
- **Coach digest tool** — scheduled weekly summary, dropped into
   decisions or knowledge.

## What needs verification (when user is next active)

Verified as of 2026-04-24: HARNESS_TOKEN auth gate, fine-grained
GITHUB_TOKEN, kDrive mirror (active after TOT/ folder created), live
repo provisioning (`provision now` button), workspace_dir fallback,
/compact manual turn, post-error auto-retry (observed working after
p2's exit-1 incident).

Still unverified end-to-end:

1. **Auto-compact trigger** — no agent has crossed 70% context yet
   since the feature shipped. Watch for `auto_compact_triggered`
   event during a long session (e.g. Coach cycling inbox for hours).
2. **`auto_retry_gave_up` escalation** — path after 3 consecutive
   errors has unit-tested the counter but not been exercised live.
3. **Cost cap blocks spawn** when an agent is over its daily limit.
4. **Image paste** end-to-end: paste in pane → upload → agent Read
   → describe.
5. **Snapshot retention** — with kDrive enabled, after
   RETENTION+1 snapshots, confirm only the newest RETENTION remain.
6. **MCP server smoke-test** — has the paste-JSON flow survived an
   actual GitHub / Notion MCP? Only self-tested with a stub.
7. **Coach recurrence steady-state** — set a 2-minute tick via
   `/tick 2` or `PUT /api/coach/tick {minutes: 2}`, confirm
   `recurrence_fired` events arrive on cadence and `recurrence_skipped`
   reasons (`coach_busy` / `cost_capped`) fire under the matching
   conditions.
8. **Telegram bridge** — set the bot token + chat IDs via Options
   drawer → "Telegram bridge" section (or via env on first boot),
   send a message to the bot, confirm Coach turn fires and reply
   lands back in the chat. Test long replies (>4000 chars) split
   correctly. Test `human_attention` forwarding by triggering a
   `coord_request_human` from a Player. Test live reload: change the
   chat IDs in the UI and confirm the new whitelist takes effect
   without a restart.

Most likely failure mode remaining: subtle SDK version drift where
our defensive `_extract_usage` / post-result suppression / auto-retry
interlock misses a newer CLI error shape. Log signature to watch:
`Exception: Command failed with exit code 1` without `ResultMessage`
preceding it.

---

## Critical invariants (do not violate without discussion)

1. **Single write-handle discipline.** All agents write freely — they chat (`coord_send_message`), claim tasks, update progress, create subtasks, drop notes in shared memory. But every write routes through the harness server process, which holds the only SQLite write handle. Do NOT add code paths where an agent opens its own DB connection or edits `state/*.json` directly. The point is ordering + audit, not restricting agent autonomy.

2. **Per-worktree isolation is the primary concurrency control.** Each worker operates in its own git worktree under `workspaces/wN/`. Locks (`coord_acquire_lock`) are **advisory only**, for logical cross-worktree resources (e.g. "only one worker runs the migration"). Don't reach for locks when a worktree would do.

3. **Memory is scratchpad.** `memory/*.md` is overwritten on update, no version history. If history matters, the event log (`memory_updated` events) has it. `decisions/*.md` is append-only by convention — that's where durable "we chose X because Y" lives.

4. **Max-plan OAuth, no API keys.** The whole point is to share one Max billing across 10 agents. Don't introduce `ANTHROPIC_API_KEY` paths. See auth gotcha below.

5. **Cost caps baked in from the start.** Per-agent daily turn/cost caps are enforced before spawn, not added later. 11 Sonnet sessions × 50-turn loops can chew through a weekly Max allowance fast.

6. **No emoji in the UI.** Never put emoji or emoticons (⚠️ ✅ ❌ 📄 🔒 ⚑ ↻ → etc.) in JSX/HTML/CSS strings. Use small CSS-drawn divs with `currentColor` backgrounds, or inline SVG. The codebase already has the patterns: status dots, lock SVG, `.projects-icon-*` (multi-folder), `.files-icon-*` (file-tree). Grep `server/static/style.css` for `-icon-` to follow precedent. Emoji render inconsistently across OS/font stacks and clash with the harness's clean icon-driven design. Applies equally to warnings, badges, status indicators, and tool-result renderers.

---

## Per-agent runtimes (Claude + Codex)

The harness ships two runtimes (`server/runtimes/`):

- **ClaudeRuntime** — default. Backed by `claude-agent-sdk`. In-process MCP for `coord_*`. All 11 slots use this unless overridden.
- **CodexRuntime** — gated behind `HARNESS_CODEX_ENABLED`. Backed by the `codex-app-server-sdk` (provisional — PR 1 spike must confirm signatures). Native tools are `shell` / `apply_patch` / `web_search` instead of `Bash` / `Edit` / `WebSearch`. `coord_*` is identical via the stdio→loopback proxy in `server/coord_mcp.py`.

Resolution at spawn time: `agents.runtime_override` (per-slot) → role default in `team_config` (`coach_default_runtime` / `players_default_runtime`) → `'claude'`. Set the per-slot override via the pane gear popover or `PUT /api/agents/{id}/runtime`.

For full design: `Docs/CODEX_RUNTIME_SPEC.md`. The dispatcher in `agents.run_agent` is runtime-agnostic; the runtime-specific work lives behind the `AgentRuntime` protocol in `server/runtimes/base.py`.

## Known gotchas

### `HARNESS_COACH_TICK_INTERVAL` is deprecated (recurrence v2)

The legacy in-memory tick loop was replaced by the unified
`recurrence_scheduler_loop` (see `Docs/recurrence-specs.md`). The env
var is now honored **only on the first migration**: if non-zero on a
fresh DB, `db._seed_recurrence_from_env` seeds a `coach_recurrence`
tick row at that cadence and stamps `team_config.recurrence_v1_seeded`
so subsequent boots ignore the env var. To set the recurring tick at
runtime use `PUT /api/coach/tick {minutes: N}` or the `/tick N` slash
command — both write to the `coach_recurrence` table directly. To stop
it, use `PUT /api/coach/tick {enabled: false}` or `/tick off`.

### Claude CLI auth: persist via `CLAUDE_CONFIG_DIR` on the /data volume

Confirmed via M-1 spike. `~/.claude.json` holds only local CLI config (numStartups, installMethod). OAuth tokens live in `.credentials.json` on Linux (file-based fallback when no libsecret/Secret Service — as in stock containers).

**Fix:** The Dockerfile sets `CLAUDE_CONFIG_DIR=/data/claude`. Because `/data` is already a Zeabur persistent volume, the CLI writes `.credentials.json` and `.claude.json` into `/data/claude/` which survives redeploys.

- On first deploy (or if you rotate secrets): shell into the container, run `claude`, type `/login`, follow the device-code flow once.
- After that, every redeploy finds the existing token and you don't re-authenticate.
- `/api/health` exposes `claude_auth.credentials_present: true/false` so you can confirm persistence without logging in to check.

### Codex CLI auth: same `/data` strategy via `CODEX_HOME=/data/codex`

The Dockerfile sets `CODEX_HOME=/data/codex` so Codex's `auth.json` (ChatGPT session) survives redeploys, mirroring the `CLAUDE_CONFIG_DIR` rule. Headless `codex login` viability is the highest-risk PR 1 spike item — if device-code can't complete in non-TTY container shell, fall back to API-key-only Codex via the `openai_api_key` entry in the encrypted `secrets` table.

`/api/health` exposes `codex_auth.{credentials_present, method}` (`method` = `chatgpt` / `api_key` / `none`).

### Zeabur geo-block: install via npm, not the shell installer

Zeabur's default datacenter returns HTTP 403 for `https://claude.ai/install.sh` ("App unavailable in region"). `api.anthropic.com` is **not** blocked in the same region — runtime queries work fine.

- Dockerfiles must install Claude CLI via: `npm install -g @anthropic-ai/claude-code`
- Not via: `curl -fsSL https://claude.ai/install.sh | bash`

### Line endings on Windows

`.gitattributes` at repo root forces LF on `*.sh` and `Dockerfile*`. If you add new shell scripts or Dockerfiles, existing rules cover them. If not, the script will fail in Linux containers with `$'\r': command not found`.

### `/compact` is hand-rolled; migrate when SDK exposes `context_management`

Anthropic shipped a native compaction feature in the Messages API as
of 2026-01-12 (`anthropic-beta: compact-2026-01-12` +
`context_management={"edits":[{"type":"compact_20260112",...}]}`). The
**Claude Agent SDK does not expose it yet** — `ClaudeAgentOptions`
has no `context_management` kwarg. Our implementation
(`agents.continuity_note`, `last_exchange_json`, `COMPACT_PROMPT`,
`_session_context_estimate`, the auto-compact trip-wire) mirrors the
native design: summarize older, preserve recent verbatim.

When the Agent SDK adds `context_management`, migrate:
1. Drop our `agents.continuity_note` + `last_exchange_json` columns.
2. Drop `_set_continuity_note`, `_append_exchange`,
   `_extract_usage`, `_session_context_estimate`,
   `_context_window_for`, the auto-compact block in `run_agent`.
3. Drop the `/api/agents/{id}/compact` endpoint + `/compact` slash.
4. Pass `context_management={"edits":[{"type":"compact_20260112",
   "trigger":{"type":"input_tokens","value":<threshold>}}]}` in
   every `query()`.

~400 lines deleted, 1 kwarg added. Watch the
[claude-agent-sdk-python changelog](https://github.com/anthropics/claude-agent-sdk-python/blob/main/CHANGELOG.md).

### Frontend deps are vendored — refresh via `scripts/vendor_deps.py`

Most ESM deps the UI uses (htm, split.js, marked, dompurify, diff,
highlight.js core + 12 language packs, katex, plus the github-dark +
katex CSS) live under `server/static/vendor/`, not on esm.sh. Cold
first load drops from ~17 cross-origin module requests to 2 (preact +
preact/hooks, which stay on esm.sh because they share component-
instance state with each other and `?bundle`-ing them produces two
separate Preact instances that break useState).

Three tiers of vendoring:
- **`DEPS`** — ESM modules fetched with esm.sh's `?bundle` flag (one
  self-contained file per dep). Sanity-checked for stray `https://esm.sh/`
  imports on disk.
- **`NON_ESM_DEPS`** — UMD/IIFE bundles fetched as-is (currently just
  `mermaid.min.js`, ~3MB). Loaded via dynamic `<script>` tag in
  `markdown.js` because mermaid's ESM build splits across 30+ chunks.
- **`CSS_DEPS`** — plain CSS (hljs theme + KaTeX). KaTeX CSS goes
  through `_CSS_REWRITES` to convert relative `fonts/...` URLs to
  absolute jsdelivr URLs, so we don't have to vendor 12 binary fonts.
  Browser fetches them on first use, then caches forever.

To bump versions: edit `DEPS` / `NON_ESM_DEPS` / `CSS_DEPS` in
`scripts/vendor_deps.py`, run `python scripts/vendor_deps.py`, commit
the regenerated files. The script chases esm.sh's `?bundle` re-export
wrapper to grab the real self-contained bundle. CI does NOT regenerate
vendor files — they ship as committed artifacts.

### Markdown rendering: `server/static/markdown.js`

Single chokepoint for everything markdown-shaped in the UI: agent
panes, files `.md` preview, compass briefings, decisions, wiki
entries. Pipeline: `marked` (GFM) → custom code-renderer (hljs for
known langs; placeholder for `mermaid`) → KaTeX inline+block extension
(parse-time, `htmlAndMathml` output so equations also paste into Word
as MathML) → callouts extension (parse-time, GFM-Alerts compatible)
→ DOMPurify (`html` + `mathMl` profiles, link-rewrite hook for in-app
file links + external `target=_blank`) → consumer mounts via
`dangerouslySetInnerHTML`. Post-mount: a single MutationObserver
rooted at `document.body` (installed once at app boot in `app.js`)
watches for `<pre class="md-mermaid">` placeholders and lazy-loads
mermaid (3MB UMD via `<script>` tag; cached after first use). Render
results cached by source string; WeakSet de-dupes already-processed
nodes across Preact rerenders.

Callouts use Obsidian's `> [!type]` syntax (`note`, `tip`, `warning`,
`success`, `danger`, `example`, `quote`, `question`, `info`, `todo`,
`abstract`, `failure` — plus aliases like `summary`/`tldr` for
`abstract`, `hint`/`important` for `tip`, etc.). Optional `+` (open)
or `-` (collapsed) sign after `]` makes the block a `<details>` rather
than a `<div>`. 12 colour themes share a single `--callout-color` CSS
contract; unknown types fall back to `note` so a typo never blanks
the block.

Adding a new renderer (PlantUML, GraphViz, alternative math engine,
etc.): drop the parse-time hook into `markdown.js` and either render
inline at parse time (KaTeX/callouts-style) or emit a placeholder +
extend the observer (mermaid-style). Zero changes to consumers.

### Post-ResultMessage teardown noise is SDK-version-sensitive

CLI 2.1.118 raised `ProcessError` after a clean ResultMessage; 2.1.12x
raises bare `Exception("Command failed with exit code 1")`. Our
suppression in `run_agent`'s error handler checks
`turn_ctx.get("got_result")` and ignores **any** exception class
after that flag is set — don't narrow the check to a specific type
again. Separate signal: `CLIConnectionError` with "Check stderr
output for details" IS a real pre-result failure — auto-retry
handles it.

---

## Repo layout (current)

```
TeamOfTen/
├── CLAUDE.md                    # this file
├── Docs/
│   └── TOT-specs.md             # full spec — source of truth for design decisions
├── spike/
│   ├── zeabur/                  # M-1 spike Dockerfile + shell for Zeabur
│   │   ├── Dockerfile
│   │   ├── spike.sh             # not currently used (manual shell instead)
│   │   └── README.md
│   ├── spike.py                 # abandoned Python SDK version (ARM64 wheel issues)
│   └── requirements.txt
├── .gitignore
└── .gitattributes               # force LF on *.sh and Dockerfile
```

Planned expansion per spec §3: `server/`, `web/`, `prompts/`, `workspaces/`, `scripts/`. Not yet created.

---

## Key commands (for any agent working in this repo)

- **Quick concurrency test on running Zeabur container**: `claude -p "test"` then `for i in $(seq 1 10); do claude -p "hi $i" & done; wait`
- **Local spike re-run on Windows** (for laptop-only tests): `claude -p "..."` — no setup needed, Claude CLI 2.1.104 already installed
- **Run tests**: `uv sync --extra dev && uv run pytest`
  — or with plain venv: `pip install -e .[dev] && pytest`
  Test suite lives in `server/tests/`. Current coverage: DB schema
  smoke, event-bus round-trip, tool validation constants, task-state
  machine. All tests are DB-level (no FastAPI TestClient yet) so they
  run fast and don't need claude-agent-sdk wired up.

- **Run tests from an isolated worktree**:
  `bash scripts/bootstrap_worktree.sh && ./pytest`
  `.venv` is gitignored, so `git worktree add` (and Claude Code's
  `Agent({isolation: "worktree"})`) lands without Python deps. The
  bootstrap script reuses the main worktree's `.venv` via a thin
  `./pytest` shim that exports `PYTHONPATH=<this worktree>`, so
  `import server.*` resolves to the sub-worktree's source and not
  the main's editable-install path. No per-worktree `uv sync` —
  that fails on win32-ARM64 because cryptography / httptools have
  no prebuilt wheels and need MSVC + Rust to build from source.
  When you're already in the main worktree the script is a fast
  no-op (or a normal `uv sync` if there's no `.venv` yet).
- **Run dev server**: `uv run uvicorn server.main:app --reload`
  — or `uvicorn server.main:app --reload` with a plain venv.
  Default binds :8000.

---

## Skills to use

Built-in slash commands worth knowing for this project:

- **`/security-review`** — runs the built-in security-review skill against current branch. Use before each deploy, especially when touching auth, MCP tool registration, or anything that handles inter-agent messages.
- **`/review`** — general PR review.

The `claude-api` skill auto-triggers when editing Python files that import `anthropic` or `claude_agent_sdk` — it will guide caching, thinking budgets, and migration between Claude versions.

No custom project-specific skills yet — this `CLAUDE.md` is the single source for project conventions, loaded automatically at session start. If the project grows to the point that this file exceeds ~200 lines, split into skills.

---

## Before committing

- Line endings: verify `git status` does not show `LF will be replaced by CRLF` for `*.sh` or `Dockerfile*` — if it does, `.gitattributes` is missing or not applied.
- Secrets: `.gitignore` covers `.env*`, `.claude.json`, `.claude/`. Double-check any new config file doesn't leak.
- No `ANTHROPIC_API_KEY` references — this harness is Max-OAuth only.

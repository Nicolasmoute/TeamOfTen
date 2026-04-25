# TeamOfTen — Claude Code Harness

A personal orchestration harness for a **team of 11 Claude Code agents — 1 Coach + 10 Players** — sharing memory and a task board, with a multi-pane web UI, deployed to a single VPS (Zeabur). Max-plan OAuth only — no API keys.

**Full spec**: [Docs/HARNESS_SPEC.md](Docs/HARNESS_SPEC.md) — read it before touching server code.

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
- **Coach autoloop** ✓ env-gated background task: when
   `HARNESS_COACH_TICK_INTERVAL > 0`, Coach is nudged to drain inbox at
   that cadence. Skips when Coach is already working. Manual trigger:
   `POST /api/coach/tick` (409 if busy).

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
7. **Coach autoloop steady-state** — set
   `HARNESS_COACH_TICK_INTERVAL=120`, confirm `routine tick` events
   fire on cadence and skip while a prior turn is working.
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

---

## Known gotchas

### Claude CLI auth: persist via `CLAUDE_CONFIG_DIR` on the /data volume

Confirmed via M-1 spike. `~/.claude.json` holds only local CLI config (numStartups, installMethod). OAuth tokens live in `.credentials.json` on Linux (file-based fallback when no libsecret/Secret Service — as in stock containers).

**Fix:** The Dockerfile sets `CLAUDE_CONFIG_DIR=/data/claude`. Because `/data` is already a Zeabur persistent volume, the CLI writes `.credentials.json` and `.claude.json` into `/data/claude/` which survives redeploys.

- On first deploy (or if you rotate secrets): shell into the container, run `claude`, type `/login`, follow the device-code flow once.
- After that, every redeploy finds the existing token and you don't re-authenticate.
- `/api/health` exposes `claude_auth.credentials_present: true/false` so you can confirm persistence without logging in to check.

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
highlight.js core + 12 language packs, the github-dark theme CSS) live
under `server/static/vendor/`, not on esm.sh. Cold first load drops
from ~17 cross-origin module requests to 2 (preact + preact/hooks,
which stay on esm.sh because they share component-instance state with
each other and `?bundle`-ing them produces two separate Preact
instances that break useState).

To bump versions: edit `DEPS` in `scripts/vendor_deps.py`, run
`python scripts/vendor_deps.py`, commit the regenerated files.
The script chases esm.sh's `?bundle` re-export wrapper to grab the
real self-contained bundle. CI does NOT regenerate vendor files —
they ship as committed artifacts.

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
│   └── HARNESS_SPEC.md          # full spec — source of truth for design decisions
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

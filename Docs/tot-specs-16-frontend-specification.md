---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 16: Frontend Specification'
section: 16
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
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

- Agent buttons for Coach and Players. Each is a square tinted by
  work state (`state-idle` / `state-working` / `state-problem`) with
  the slot label inside; the previous comms-state dot overlay (the
  small green/blue/orange pip) was removed. Tooltip still surfaces
  comms hints ("has unread inbox", "waiting for a reply") via the
  same `dotStates` Map computed in `App`.
- Unread/problem indicators (square tint only — work state stays;
  comms state is tooltip-only).
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
  (`coord_send_message`, `coord_approve_stage` — the *moment* the
  agent makes an inter-agent call). Distinct from Tier 1 at a glance
  without competing for attention.
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

**Direction tags on `message_sent` rows.** Every Tier-2 `message_sent`
(`.peer-thread`) row shows a compact direction tag chip at the start of
the meta line, rendered as an inline SVG arrow (drawn with
`currentColor`, no emoji). Tag semantics relative to the pane's viewer
slot:
- Outgoing (`event.agent_id === viewerSlot`): right-arrow SVG + short
  recipient label (`slotShortLabel(to)`). CSS class `msg-dir-out`,
  color `var(--accent)` (blue). `border-color` stays accent.
- Incoming (`event.to === viewerSlot`): left-arrow SVG + short sender
  label. CSS class `msg-dir-in`, color `var(--ok)` (green). The event
  `div` also gets class `msg-incoming` which overrides the left-border
  to `var(--ok)` — visually separates "arrived for me" from "I sent
  this".
- Broadcast (`event.to === "broadcast"`): bidirectional-arrow SVG +
  label `"all"`. CSS class `msg-dir-bc`, color `var(--muted)`.
- Third-party observer (fan-out to a pane that is neither sender nor
  recipient): bidirectional-arrow SVG + `"AB"` label (short form of
  both parties). `msg-dir-bc`.
- Null viewer slot (prop not passed): tag omitted; rendering is
  identical to the pre-feature behavior.
Tier-1 (`.human-thread`) messages are unaffected — direction tag is
peer-thread only. `viewerSlot` is threaded from `AgentPane(slot)` →
`EventList(viewerSlot)` → `EventItem(viewerSlot)`. Pure helper
`msgDirTag(event, viewerSlot)` is defined in `app.js` alongside
`slotShortLabel`.

Errors and asks ignore the tiering and stay loud regardless: `.event.error`
red, `.event.tool_result.error` red, plus AskUserQuestion / plan-mode
/ file-write-proposal / human_attention escalations.

`human_attention` events specifically render as a full body card
(`.event.human_attention`) — subject in the meta line plus the entire
markdown-rendered body inline, urgency-colored left border (warn for
`normal`, err for `blocker`). The body is not a sys row because it is
the same text the Telegram bridge forwards to the user's phone, and
needs to be readable in chat too instead of buried in the
`coord_request_human` tool_use JSON.

Routing logic for `message_sent` lives in [server/static/app.js](../server/static/app.js)'s
event renderer — it adds `.human-thread` or `.peer-thread` based on
`from`/`to` ids before the CSS tiering takes over. The `comm-tool`
class for `coord_send_message` / `coord_approve_stage` is added in
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

**Chat reply button.** White `.event.text` final agent replies and
`message_sent` rows reveal a hover-only curved-arrow icon button
(CSS-drawn inline SVG, no emoji). Clicking pre-fills the pane textarea
with a quoted snippet and focuses it:
`` `> Re: ${subject} (from ${sender}): ${first 80 chars of body}…\n\n` ``.
For white `.event.text` rows, the reply stays in the same AgentPane
composer/context and uses `event.agent_id || viewerSlot` as the quote
sender; if neither is known, no Reply button is rendered. Blue
`.event.message_sent.peer-thread` rows keep the existing reply behavior:
reply target = original sender's `agent_id`, and `broadcast` senders
default to `coach`. No new endpoints or schema changes. The same
affordance appears on inbox rows in the Environment Pane (§16.6);
there it opens the EnvPane Inbox composer, sets the `to` field, and
pre-fills the body.

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
    - **Thinking toggle** — checkbox in the pane gear popover (no
      composer chip yet). When on, Anthropic's extended-thinking
      phase runs before the visible response (Claude only). No role
      default: off unless explicitly set on `paneSettings.thinking`
      OR `agents[].thinking_override`. Resolution at spawn time:
      paneSettings.thinking (when non-null) →
      `agents[].thinking_override` → off. Override surfaces in the
      EnvPane "Active overrides" section as `thinking=on/off` and in
      the timeline as a `.sys` row on every `agent_thinking_set`.
      Budget tokens are env-tuned harness-wide
      (`HARNESS_THINKING_BUDGET_TOKENS`, default 8000); the UI does
      not expose the budget knob.
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
- Boundary-only retry: when reconciliation flips an entry to `queued`
  it stamps `rejectedAt` with the `spawn_rejected` event ts. The
  auto-retry effect won't re-fire that entry until a boundary event
  for this slot (`agent_stopped` / `agent_cancelled` / `result`)
  arrives strictly after `rejectedAt`. There is no timer fallback —
  without a boundary signal there's no reason to believe the agent
  freed up, and re-poking on a fixed cadence produces a flurry of
  `spawn_rejected` rows in the timeline while the user waits for the
  current turn to finish. The `queued` state in the composer is the
  user-facing wait signal; if the in-flight turn never emits a
  boundary (truly stuck), the entry stays `queued` until the user
  cancels it or the slot is cancelled. The stamp clears when the
  retry actually fires so a fresh rejection on the next round-trip
  re-stamps with a current ts.
- Timeline noise suppression: `spawn_rejected` rows whose `prompt`
  matches a current pending entry's body (any status) are filtered
  out of `visibleEvents` so the user-facing wait signal lives in the
  composer's `queued` pill, not as a stack of redundant rejection
  rows in the conversation. Non-matching rejections (synthetic /
  external callers) still surface for diagnostics.
- Cancel: each pending card has an `×` button to discard.
- Per-pane state, in-memory only (lost on refresh — acceptable since
  prompts not yet started leave no server-side trace anyway).

Pane settings:

- Model override.
- Plan mode toggle.
- Effort selector 1 to 4.
- Thinking toggle (Claude only).
- Agent brief editor.
- Three action buttons: **Cancel** (discards model/plan/effort/thinking changes
  made since the popover opened, via snapshot-restore; does NOT undo runtime
  changes which fire API immediately), **clear overrides** (resets all
  pane-local overrides to empty), **done** (closes the popover keeping all
  staged changes).

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
  approve, deny/drop, request-changes, and comment-to-Coach buttons.
  Deny/drop and request-changes require a note; request-changes is
  represented as denial with a prefixed note because the backend has
  no separate request-changes status. Comment sends a human message to
  Coach and leaves the proposal pending.
  TruthGate attention cards for pending truth amendments do not
  approve or deny directly; their review action opens this same
  file-write proposal row, expands the diff, and focuses the row so
  the existing confirmation/comment controls remain the only
  resolution path.
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
       **Non-dismissable interactive items.** `pending_question` and
       `pending_plan` attention rows do NOT have a silent dismiss (×)
       button. An agent is paused on its `pending_question` /
       `pending_plan` Future; silently hiding the UI card leaves the
       agent blocked indefinitely. Instead, these rows show a **"skip"
       button** (`.env-attention-cancel`, amber border) that POSTs to
       `POST /api/questions/{correlation_id}/cancel` or
       `POST /api/plans/{correlation_id}/cancel`. The cancel endpoint
       calls `interactions.reject(correlation_id, "cancelled by human
       operator")` which resolves the Future with `InteractionRejected`;
       the agent receives a `PermissionResultDeny` and can reformulate
       or escalate. The `question_cancelled` / `plan_cancelled` bus
       event is then published (existing path in `agents.py`), which
       removes the item from the attention strip naturally. Other
       attention item types (`human_attention`, `file_write_proposal`)
       retain the plain dismiss (×) behaviour because they do not have
       a waiting agent Future.
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
- **Kanban card titles** — `.kbn-card-title` switches from `display: -webkit-box`
  / `-webkit-line-clamp: 2` to `display: block` + `max-height: calc(1.32em * 3)`.
  The webkit-box approach collapses to 0px height on some Android Chrome builds,
  making titles invisible; the max-height approach is reliable and allows 3 lines.
  Expanded cards (`kbn-card.expanded`) remove the cap so the full title shows.
- **Kanban card sizing on mobile** — cards felt cramped on smartphone screens
  even after the title-visibility fix above. A second mobile pass (2026-05-14)
  bumps card padding to `12px 13px` (from `8px 9px`), sets `min-height: 72px`,
  increases the gap between card sub-rows to `8px`, and bumps `.kbn-card-title`
  `font-size` from `13px` to `14.5px`. Stage labels (`kbn-stage-label`) step up
  from `9px` to `10px`. Interactive icon buttons inside cards (`kbn-card-act-btn`)
  get `min-height: 44px` + `min-width: 44px` with flex centering to meet the
  ≥44px touch target guideline. All rules are scoped to `@media (max-width: 700px)`
  and do not affect desktop layout.
- **Touch-inaccessible hover-reveal buttons** — two classes of buttons are hidden
  via CSS hover and never reachable on touch screens; both get always-visible
  overrides at the mobile breakpoint:
  - Kanban backlog edit/delete icons (`.kbn-card-actions.kbn-backlog-actions`):
    desktop uses `:hover { display: flex }` on the parent card; mobile forces
    `display: flex` unconditionally. The buttons are `position: absolute` so they
    don't shift card layout.
  - Inbox reply button (`.env-reply-btn`) and agent-pane message reply button
    (`.msg-reply-btn`): both use `opacity: 0` with `:hover { opacity: 1 }`;
    mobile forces `opacity: 1` so the tap target is always present.

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

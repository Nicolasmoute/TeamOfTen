---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 11: Agent Runtime'
section: 11
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
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
- `/tick 0` — fire continuously (as soon as Coach is idle); same as
  `coord_set_tick_interval(0)`.
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
- **Coach MCP surface** — `coord_set_project_objectives(text)` replaces
  the active project's objectives file, mirrors it to kDrive, and emits
  `objectives_updated`. This is the supported Coach write path in both
  Claude and Codex runtimes.

Busy-Coach behavior splits by kind (recurrence-specs.md §2 / §11):

- **Tick rows DEFER** — keep `next_fire_at` in place and re-evaluate next
  pass; fire as soon as Coach is idle. Emits `recurrence_deferred` once
  per defer episode (latch resets on next fire). Cadence `0` is allowed
  and means "fire continuously as soon as Coach is idle"; the cap stays
  the floor. Coach can self-throttle via `coord_set_tick_interval`.
- **Repeat / cron rows SKIP** — wall-clock semantics; the slot is
  dropped, `next_fire_at` advances. Emits `recurrence_skipped` with
  `reason="coach_busy"` / `reason="cost_capped"`.

Events emitted: `recurrence_added`, `recurrence_changed`,
`recurrence_deleted`, `recurrence_fired`, `recurrence_skipped`,
`recurrence_deferred`, `recurrence_disabled`. Plus `coach_todo_added`,
`coach_todo_completed`, `coach_todo_updated`, `objectives_updated`.

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

- Coach `coord_approve_stage`: wakes the named assignee with the
  `note` as the wake body, bypasses debounce.
- Agent `coord_send_message` to direct recipient: wakes recipient, bypasses
  debounce. Tight Coach↔Player ping-pong is bounded by per-turn duration
  (tens of seconds), not the wake-debounce window; the debounce was
  silently dropping legitimate Player→Coach signals when Coach had just
  finished a turn.
- Human `POST /api/messages` to direct recipient: wakes recipient, bypasses
  debounce.
- Telegram inbound to Coach: wakes Coach, bypasses debounce.

Broadcasts do not wake the team.

Debounce:

```text
HARNESS_AUTOWAKE_DEBOUNCE=10
```

### 11.5 Error Retry

Two distinct retry paths depending on where the failure surfaces:

**Hard errors** (turn threw before `ResultMessage`):

- Error event is emitted; agent status becomes `error`.
- A post-error retry is scheduled after
  `HARNESS_ERROR_RETRY_DELAY`, default 45 seconds.
- Retry only fires if status is still `error` when the delay
  elapses (a manual recovery wake during the window pre-empts).
- Consecutive retry limit:
  `HARNESS_ERROR_RETRY_MAX_CONSECUTIVE`, default 3.

**Soft errors** (`ResultMessage(is_error=True)` — turn returned
cleanly but the model's stop_reason / subtype indicate failure):

- Policy decides per-shape (`_soft_error_retry_policy` in
  `server/agents.py`):
  - `stop_reason == "stop_sequence"` → retry, 0s delay (almost
    always a model-side truncation, not a real failure).
  - `stop_reason == "tool_use"` AND `duration_ms < 5 min` →
    retry, 30s delay (likely transient tool / shell flake).
  - `stop_reason == "tool_use"` AND `duration_ms ≥ 5 min` →
    no retry (probable tool loop or stuck shell; needs Coach).
  - `max_turns` / `max_tokens` → no retry (auto-continue path
    handles them separately).
  - Unrecognized shapes → no retry; Coach gets a DM with
    `last_tool` context for triage.
- Cap accounting reuses `_consecutive_errors`, so a repeatedly
  failing retriable shape eventually escalates to `human_attention`
  via the gave-up path.
- Soft retries reuse `_schedule_post_error_retry` with
  `accept_idle_status=True` and `delay_s_override=<policy>`.

**Coach DM for non-retriable soft errors**:

- Debounce: `HARNESS_ERROR_DM_DEBOUNCE`, default 300 seconds.
- Body includes `last_tool` from the turn context so Coach
  knows which tool the agent was on when it errored.

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

### 11.8 Idle-Poller and Runtime Transfers

`server/idle_poller.py:_maybe_wake_idle` fires wake prompts at idle
Players who have pending non-executor role assignments.  It applies two
independent suppression checks after a runtime transfer:

**(Option A — debounce reset)** `_perform_runtime_transfer_flip`
(agents.py) updates `agents.last_idle_wake_at = now()` at flip time.
This extends the per-Player debounce window from the transfer, giving the
queued assign-time wake (queued by queue-on-busy while the compact turn
was running) time to fire and close its role row before the idle poller
ticks again.  If no assign-time wake was queued, the agent still receives
a delayed idle-poller wake once `HARNESS_IDLE_POLL_DEBOUNCE_SECONDS`
(default 1800s) has elapsed from the flip.

**(Option B — transfer cooldown)** `_perform_runtime_transfer_flip` also
stamps `agents.last_runtime_transfer_at = now()`.  `_maybe_wake_idle`
reads this column and returns `False` if fewer than
`HARNESS_IDLE_POLL_TRANSFER_COOLDOWN_SECONDS` (default 60s) have elapsed
since the transfer.  Set to 0 to disable this cooldown.

Both checks fire independently; either one suppresses the false wake.
Together they cover the case where the debounce window was already
expired at flip time (Option B catches it) AND the case where the window
is still fresh but the cooldown has passed (Option A catches it via the
normal debounce logic).

### 11.9 Execute/Ship Stage Boundary in Wake Notes

**Root cause (2026-05-14 audit)**: Ambiguous wake notes — "commit + push,
then coord_role_complete" in execute-stage wakes, "cherry-pick to dev and
push" in ship-stage notes — led Players to generalise ship-stage patterns
onto execute turns, bypassing the `audit_syntax → audit_semantics` gate.

**Fix**: two constants in `server/kanban.py` are appended to the
stage-specific body returned by `_completion_hint_for_role`:

- `_EXECUTE_STAGE_BOUNDARY` — appended to every executor wake hint.
  States explicitly: push ONLY to `origin/work/<slot>` via
  `coord_commit_push`; do NOT cherry-pick to dev; do NOT push to dev
  directly; do NOT create `ship-*` branches.  Audit and ship stages are
  separate and Coach-driven.

- `_SHIP_STAGE_BOUNDARY` — appended to every shipper wake hint.
  States: use `coord_ship_to_dev(task_id=<id>)` (enforces the
  audit-pass gate); do NOT run raw `git push origin <anything>:dev`.
  If the tool is not yet visible, open a PR via GitHub MCP and wait
  for explicit Coach approval.

The same wording appears in the canonical project CLAUDE.md template at
`server/templates/app_dev_claude_md.md` under the `#### Worktree boundary`
section, propagating to every project via the Coach-driven reconciliation
flow in `server/project_claude_md.py`.

---

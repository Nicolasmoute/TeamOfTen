# Coach Recurrence v2 — Specification
# Coach Recurrence v2 — Specification

> **Subordinate to `Docs/TOT-specs.md`.** When this doc and TOT-specs
> disagree, TOT-specs wins. This file goes deeper on Coach recurrences
> (tick / repeat / cron, coach-todos, project-objectives) but cannot
> redefine fields, endpoints, events, or invariants that TOT-specs
> declares.

Status: shipped. TOT-specs §11.3 carries the operational summary; this
file is the design reference.

This spec replaces today's `/loop`, `/repeat`, and `/tick` with a unified,
project-scoped, persisted recurrence model, plus two new project artifacts —
**coach todos** and **project objectives** — that the smart tick uses to
drive Coach forward when there's nothing in the inbox.

---

## 1. Goals

- One mental model for "things that auto-trigger Coach", with three flavors:
  **tick**, **repeat**, **cron**.
- All recurrences are **project-scoped** and **persisted** (survive restarts).
- The tick is the smart default — no user prompt; harness composes one from
  inbox + todos + objectives.
- Two new artifacts give the tick something to do when the inbox is empty:
  - **`coach-todos.md`** — a finite, strikeable backlog.
  - **`project-objectives.md`** — the project's north star; what "good"
    looks like.
- Slash commands stay; a new **Recurrence pane** mirrors and edits the same
  state.
- Durations are in **minutes** everywhere (no seconds in the surface).

---

## 2. Concepts

| Flavor | When it fires | Prompt source | Cardinality | Busy-Coach behavior |
|---|---|---|---|---|
| **Tick** | Cadence in minutes; `0` = fire continuously | Harness-composed (smart) | 1 per project (singleton) | **Defer** (wait until idle, then fire) |
| **Repeat** | Fixed interval, in minutes (`>= 1`) | User-supplied | Many per project | **Skip** (drop, advance to next slot) |
| **Cron** | Wall-clock (friendly DSL) | User-supplied | Many per project | **Skip** (drop, advance to next slot) |

A recurrence is **always Coach-only**. Players have no recurrences in v2.

A recurrence is **always project-scoped**. Switching the active project
switches which recurrences are live. Recurrences for inactive projects do
not fire.

**Tick is throttle-driven, not slot-driven.** When the tick's fire-time
arrives and Coach is mid-turn, the tick does NOT skip — it waits. The
scheduler keeps `next_fire_at` in the past and re-evaluates on every
pass; as soon as Coach goes idle (and the cost cap clears), the tick
fires. There is no "missed slot" concept for tick: the cadence is the
**minimum gap between tick fires**, not a wall-clock alarm. A tick set
to 5 minutes during a 30-minute Coach turn fires once at minute 30
(immediately after idle), not six times back-to-back.

Cadence `0` means **fire continuously**: as soon as Coach finishes a
turn, the next tick is queued. This is the "as fast as possible" mode —
Coach may want it during heavy active orchestration. The
`recurrence_fired` event still emits per fire so spend stays visible.

**Repeat and cron retain skip-on-busy semantics** (existing behavior).
They are wall-clock schedules — a missed 09:00 slot doesn't get a
make-up fire at 09:30; it just waits for the next scheduled slot.

**Default tick interval: 60 minutes**, off by default. New projects start
with no tick; the operator enables it via `/tick 60` or the pane. Coach
can adjust cadence at any time via `coord_set_tick_interval` (§7.6) —
throttle down to 1/15min when there's nothing pressing, throttle up to
`/tick 1` or `/tick 0` when actively orchestrating.

---

## 3. New project artifacts

### 3.1 `coach-todos.md`

- **Path:** `/data/projects/<slug>/coach-todos.md`.
- **kDrive mirror:** yes (synchronous, like decisions/memory).
- **Format:** GFM task list. One bullet per todo:
  ```markdown
  # Coach todos — <project name>

  - [ ] **<title>** <!-- id:t-1 due:2026-05-01 -->
    <description, free markdown, can span multiple lines>

  - [ ] **<another title>** <!-- id:t-2 -->
    ...
  ```
- **`id`** is harness-assigned, monotonically increasing per project.
  Stored in the HTML comment so it survives roundtrips through the file
  without polluting rendered markdown.
- **`due`** is optional, ISO date. May also be `due:2026-05-01T14:00Z` for
  a specific time.
- **Injected** into Coach's system prompt every turn (small, focused).
- **Coach manages** entries via MCP tools (§7). Humans can edit by hand or
  via the EnvPane Coach-todos section; the pane PUT endpoint validates
  shape on write.

### 3.2 `coach-todos-archive.md`

- **Path:** `/data/projects/<slug>/working/coach-todos-archive.md`.
- **kDrive mirror:** yes (lives under `working/`, so naturally mirrored).
- **Format:** append-only completed todos:
  ```markdown
  - [x] **<title>** <!-- id:t-1 completed:2026-04-28T14:32Z -->
    <description preserved>
  ```
- **NOT injected** into the system prompt. Reference only.
- Coach completes via `coord_complete_todo`, which atomically moves the
  entry from `coach-todos.md` to the archive.

### 3.3 `project-objectives.md`

- **Path:** `/data/projects/<slug>/project-objectives.md`.
- **kDrive mirror:** yes (synchronous on `PUT /api/projects/{id}/
  objectives`; the periodic project sync loop covers other writers
  like Coach via the Write tool).
- **Format:** free-form markdown. No mandated sections — operator describes
  goals however they like.
- **Injected** into Coach's system prompt every turn alongside CLAUDE.md
  and the brief.
- **Lifecycle:**
  - New project → file does not exist (or exists, empty).
  - First Coach turn for a project with no objectives → Coach asks the human
    to define them rather than acting on anything else.
  - Tick behavior when empty: see §5.
- Editable by hand (Files pane), by Coach (Write tool), or via the EnvPane
  Objectives section.

---

## 4. Tick prompt composition

The tick has **no user-supplied prompt**. The harness assembles a per-fire
prompt from project state, with priority:

1. **Inbox** — call `coord_read_inbox`; respond to anything pending
   from the human or teammates.
2. **Kanban** — scan the `## Active task health`, `## Stalled tasks`,
   and open-task rollups injected by the kanban observer (see
   [kanban-specs-v2.md](kanban-specs-v2.md) §11, §14, plus the
   `## Player health`, `## Audit history`, `## Recent patterns`, and
   `## Recent events` blocks). Move things forward: approve stage
   transitions with `coord_approve_stage`, nudge silent assignees,
   reassign past the stall threshold, claim unassigned stages, bump
   effort then model on the 2nd same-kind audit fail. Auto-reassign
   fires at 2h and auto-archive at 4h — beat the safety net. Stage
   transitions are Coach's responsibility; the kanban records, Coach
   routes.
3. **Coach-todos** — pick exactly ONE open `coach-todos.md` entry and
   act to **close** it. The branch's goal is `coord_complete_todo`.
   If a single turn is enough, finish the work and complete now. If
   not, the action must materially advance the SAME todo toward
   closure (assign with a clear deliverable, draft and send the
   deliverable, write the decision, schedule the dependency). Probe
   DMs, pings, and observations are legitimate **only** when they
   directly enable the next closure step on the same todo — not as
   standalone activity. Skimming many todos to drop a micro-touch on
   each is the antipattern. External blockers change *what* the
   closure step is, never *whether* one exists. The branch rejects
   "everything is gated" as rationalization.
4. **Objectives** — if (1)–(3) yield nothing actionable, consult
   `project-objectives.md` and pick **one concrete action** that
   materially advances an objective (assign a Player, send a
   coordination message, capture a new coach-todo, audit Player
   work, propose-and-execute a next step). Don't end idle when
   objectives exist.
5. **Empty state — objectives absent or empty** — only when no
   `## Project objectives` section appears in the system prompt
   (file missing, empty, or fully whitespace) does Coach end the
   turn without acting. Nothing else licenses an idle end.

The composed prompt is sent as a normal user-role message. The system prompt
already contains the project objectives + open todos (see §6), so the user
prompt is short — it just orients Coach to the priority order:

> Routine tick. Walk the priority list, act on the first non-empty
> rung, then stop.
>
> (1) Inbox — call coord_read_inbox; respond to anything pending
> from the human or teammates.
> (2) Kanban — scan "## Active task health", "## Stalled tasks",
> and the open-task list in your system prompt above. Move things
> forward: approve stage transitions with coord_approve_stage, nudge
> silent assignees, reassign past the stall threshold, claim
> unassigned stages, bump effort then model on the 2nd same-kind
> audit fail. Auto-reassign fires at 2h and auto-archive at 4h —
> beat the safety net.
> (3) coach-todos — pick exactly ONE open todo and act to CLOSE
> it. The branch goal is coord_complete_todo. If a single turn is
> enough, finish and complete now; otherwise the action must
> materially advance the SAME todo toward closure (assign with a
> clear deliverable, draft and send the deliverable, write the
> decision, schedule the dependency). Probes, pings, and
> observations are legitimate ONLY when they directly enable the
> next closure step on the same todo. Skimming many todos with
> micro-touches is the antipattern. Reject "everything is gated"
> — rationalization.
> (4) Objectives — if (1)–(3) are empty, pick ONE concrete action
> grounded in project-objectives.md (assign a Player, send a
> coordination message, capture a new coach-todo, audit Player
> work, propose-and-execute a next step). Don't end idle when
> objectives exist.
>
> End the turn without acting only when no "## Project objectives"
> section appears in your system prompt above (file absent or
> whitespace-only). Nothing else licenses an idle end.

Each branch is intentionally directive. The end-quietly clause is
gated strictly on objectives being absent — nothing else licenses
an idle end. Two failure modes the wording deliberately blocks,
both observed in production: (a) the **gated** rationalization
(Coach reads its todos, classifies each as blocked on some external
factor, concludes "standing by" — when in fact zero-cost probes,
pings, or clarifying questions were available); (b) the **skimming**
rationalization (Coach drops a micro-touch on many todos in one
turn — pinging here, observing there — without closing or
materially advancing any). Step (3) blocks (a) by rejecting
"everything is gated" by name and (b) by requiring exactly ONE
todo per turn, aimed at `coord_complete_todo`, with probes /
pings / observations admissible only when they directly enable the
next closure step on the same todo. Step (2) (kanban) is the
explicit place for "go check what's stuck and unblock it" so step
(3) can stay focused on todo closure without competing with kanban
maintenance.

This replaces today's `COACH_TICK_PROMPT`.

### 4.1 Reactive-wake todo nudge

The tick path is one of several routes that spawn a Coach turn. The
others are reactive — `maybe_wake_agent` ([server/agents.py](../server/agents.py))
fires on a human message, a Telegram inbound, a peer chat, a
task-completion notification, or a stall escalation, and passes a
short prompt scoped to the trigger ("New message from the human:
…", "Player p3 finished t-7", etc.). Without intervention these
wakes pull Coach into purely reactive mode: it answers the trigger
and ends the turn, even when the todo list has open items ready to
act on.

To close the gap, `maybe_wake_agent` appends a one-line suffix to
the wake prompt **when both** (a) `agent_id == "coach"` and (b)
the active project's `coach-todos.md` has at least one open entry:

> After handling this, scan your open coach-todos (N open) for
> anything ready to act on.

The recurrence tick (`recurrences._fire_row`) calls `run_agent`
directly and does NOT pass through `maybe_wake_agent`, so its
prompt — which already lists "(2) Open coach-todos" — is not
double-nudged. Failures inside the nudge composition (DB hiccup,
parse error, missing project) are caught and logged; the wake
proceeds with the original prompt.

---

## 5. Cron DSL

Friendly DSL only. No raw 5-field cron. Stored as the DSL string — that's
the source of truth. The harness recomputes next-fire UTC after each fire.

### 5.1 Grammar

```
schedule := once | recurring
once     := ISO_DATE TIME              # one-shot, auto-disables after firing
recurring:= "daily" TIME
          | "weekdays" TIME
          | "weekends" TIME
          | DAY_LIST TIME              # e.g. "mon,wed,fri 09:00"
          | "weekly" DAY TIME          # e.g. "weekly mon 09:00"
          | "monthly" DAY_OF_MONTH TIME  # e.g. "monthly 1 09:00"

TIME      := HH:MM (24h, leading-zero hour required, e.g. "09:00" not "9:00")
DAY       := mon | tue | wed | thu | fri | sat | sun
DAY_LIST  := DAY ("," DAY)+         # ≥2 days; single days use `weekly DAY TIME`
DAY_OF_MONTH := 1..31
ISO_DATE  := YYYY-MM-DD
```

Examples:

- `daily 09:00`
- `weekdays 18:00`
- `mon,thu 14:00`
- `monthly 1 09:00`
- `2026-05-01 10:00` (one-shot)

The parser is strict on both fronts: `9:00` is rejected (single-digit
hour) and a bare `mon 09:00` is rejected (single-day shorthand requires
the `weekly` keyword). UI cron edits validate against the same grammar
client-side so the Save button is disabled on bad input — no
round-trip-to-400 needed.

### 5.2 Timezone

Schedules are interpreted in the **operator's local timezone**, captured
at save time and stored alongside the DSL string (e.g. `Europe/Paris`).
The server runs UTC; conversion happens in the scheduler.

If the operator's timezone changes (rare), existing rows keep their stored
TZ. They can be re-saved through the pane to pick up a new TZ.

### 5.3 Confirmation on add

When `/cron` is parsed, the harness echoes the parse result back into
Coach's pane as a `recurrence_added` system event:

> Cron added: fires every weekday at 09:00 Europe/Paris.
> Next fire: Mon 2026-04-29 09:00.

If parsing fails, the slash command surfaces the error inline and does not
create a row. The operator can fix and resubmit.

### 5.4 One-shot crons

A schedule of the form `YYYY-MM-DD HH:MM` (no recurring keyword) fires
once, then the row's `enabled` flag flips to `false` automatically, and
a `recurrence_disabled` event is emitted with `reason: one_shot_complete`.
The row is kept (not deleted) so the operator sees it ran.

---

## 6. System prompt injection

Coach's system prompt gains two new sections, in this order, after the
existing project CLAUDE.md and brief:

```
## Project objectives

<verbatim contents of /data/projects/<slug>/project-objectives.md>

## Open coach todos

<verbatim contents of /data/projects/<slug>/coach-todos.md>
```

If either file is missing or empty, the corresponding section is omitted
(no "None this session" placeholder — keeps the prompt clean).

Both files are re-read every turn (no caching beyond the OS level), like
CLAUDE.md is today.

Players' system prompts are **not** modified — todos and objectives are
Coach's tools, not Players'.

### 6.1 Goal content has a single canonical surface

`project-objectives.md` is the **only** place project goals / scope live
in Coach's system prompt. Specifically:

- The per-project CLAUDE.md template (`server/templates/app_dev_claude_md.md`,
  read via `server.project_claude_md.canonical_project_claude_md_template`)
  does NOT include a `## Goal` section. It carries a `## Project objectives`
  pointer paragraph that names the objectives file and tells Coach where
  to read / update.
- The Coach coordination block (`_build_coach_coordination_block` in
  `server/agents.py`) does NOT render a `Goal:` line from
  `projects.description`. It surfaces only the project name and a
  one-line pointer to both CLAUDE.md and `project-objectives.md`.
- `projects.description` (the one-line creation-modal description)
  remains in the DB strictly as a UI surface (pane title, project
  list tagline). It is no longer injected into Coach's prompt.

This collapse fixes a prior leak: the same goal text was rendered
into Coach's prompt twice (CLAUDE.md `## Goal`, coordination block
`Goal:`) AND a third time as `## Project objectives` from the
free-form file — three stale-prone copies that drifted apart as
soon as the operator updated the file but not the modal description.

---

## 7. MCP tools (Coach-only)

New MCP tools for managing the todo file safely under the single
write-handle invariant. Players' calls to these tools return an error.

### 7.1 `coord_add_todo`

```
coord_add_todo(title: str, description: str = "", due: str | None = None)
```

- Appends a new entry to `coach-todos.md`.
- Returns the assigned id (`t-N`).
- Emits `coach_todo_added` event.

### 7.2 `coord_complete_todo`

```
coord_complete_todo(id: str)
```

- Moves the entry from `coach-todos.md` to
  `working/coach-todos-archive.md`, stamping `completed:<utc>`.
- Errors if `id` not found.
- Emits `coach_todo_completed` event.

### 7.3 `coord_update_todo`

```
coord_update_todo(id: str, title: str | None = None,
                  description: str | None = None, due: str | None = None)
```

- Edits an entry in place. Pass only the fields you want to change.
- Emits `coach_todo_updated` event.

### 7.4 No `coord_list_todos`

Coach reads the file directly via Read (or just relies on the system-prompt
injection). No MCP tool needed.

### 7.5 Objectives are not MCP-mediated

`project-objectives.md` is small, low-frequency, and edited by both human
and Coach. Coach uses the standard `Write` tool. No MCP tool. The harness
allows direct writes to that one path under the project root.

### 7.6 `coord_set_tick_interval`

```
coord_set_tick_interval(minutes: int, enabled: bool | None = None)
```

- **Coach-only.** Throttles the active project's tick row up or down.
- `minutes >= 1` sets the cadence; `minutes == 0` switches to
  "continuous" mode (fire as soon as idle); negative values rejected.
- If no tick row exists for the active project, one is created; if the
  row is disabled and `enabled` is omitted, setting cadence re-enables
  it (matches `/tick N` semantics in §8).
- `enabled=False` disables without changing cadence (mirrors
  `/tick off`); `enabled=True` re-enables.
- Emits `recurrence_changed` (or `recurrence_added` on first call).
- **When Coach should call it:**
  - Throttle DOWN (e.g. `60` or `15`) when the team is steady-state
    and the tick is mostly hitting empty branches — saves spend.
  - Throttle UP (e.g. `1` or `0`) when Coach is actively herding
    Players, monitoring a deploy, or chasing a stall — Coach gets
    re-entered as fast as the budget allows.
  - The cost cap remains the floor; throttling cannot bypass it.

---

## 8. Slash commands

| Command | Behavior |
|---|---|
| `/tick` | Fire one tick now, regardless of recurring state |
| `/tick N` | Set recurring tick to every N minutes; enables if disabled. `N=0` means fire continuously (as soon as Coach is idle) |
| `/tick off` | Disable the recurring tick (keeps row, sets `enabled=false`) |
| `/repeat` | List active repeats with ids |
| `/repeat N "<prompt>"` | Add a repeat (every N minutes) |
| `/repeat rm <id>` | Delete a repeat |
| `/cron` | List active cron jobs with ids |
| `/cron <when> "<prompt>"` | Add a cron job (DSL parsed per §5) |
| `/cron rm <id>` | Delete a cron job |

All commands are intercepted client-side and routed through the HTTP API
(§9). They never reach Coach as user prompts.

`<prompt>` may be quoted or unquoted. If unquoted, the entire remainder of
the line after the cadence is the prompt. Quoting helps when the prompt
itself starts with a digit.

`/loop` is **removed**. If typed, the UI surfaces:

> `/loop` was renamed `/tick`. Use `/tick N` for recurring, `/tick` for
> one-off, `/tick off` to disable.

---

## 9. HTTP API

### Recurrences

```
GET    /api/recurrences
POST   /api/recurrences         # create repeat or cron
PATCH  /api/recurrences/{id}    # edit prompt / cadence / tz / enabled
DELETE /api/recurrences/{id}

POST   /api/coach/tick          # fire one tick now (kept; semantics unchanged)
PUT    /api/coach/tick          # set recurring tick interval
                                # body: {"minutes": 60}  or {"enabled": false}
```

**Active-project scoping.** PATCH and DELETE return `404` when the
target row's `project_id` differs from the active project — the
operator should switch to that project first. POST always creates
under the active project. This stops a stale UI tab from mutating a
project that's no longer active.

`GET /api/recurrences` returns rows scoped to the active project, ordered
by kind then created_at. Response shape:

```json
[
  {"id": 1, "kind": "tick", "cadence": "60", "prompt": null,
   "enabled": true, "next_fire_at": "2026-04-28T14:00:00Z"},
  {"id": 2, "kind": "repeat", "cadence": "30", "prompt": "summarize new commits",
   "enabled": true, "next_fire_at": "2026-04-28T13:30:00Z"},
  {"id": 3, "kind": "cron", "cadence": "weekdays 09:00", "tz": "Europe/Paris",
   "prompt": "morning standup", "enabled": true,
   "next_fire_at": "2026-04-29T07:00:00Z"}
]
```

### Coach todos

```
GET    /api/projects/{slug}/coach-todos        # parsed array of todos
PUT    /api/projects/{slug}/coach-todos        # full-file replace, validated
POST   /api/projects/{slug}/coach-todos        # add one (HTTP shim for EnvPane)
PATCH  /api/projects/{slug}/coach-todos/{id}   # edit fields (HTTP shim)
POST   /api/projects/{slug}/coach-todos/{id}/complete  # mark done (HTTP shim)
GET    /api/projects/{slug}/coach-todos/archive
```

Individual add/complete/update have **two paths**: Coach uses the MCP
tools (`coord_add_todo` / `coord_complete_todo` / `coord_update_todo`);
the EnvPane uses the HTTP shim (POST / PATCH / complete) that wraps
the same helpers. Both emit the spec §13 events with `agent_id="coach"`
so they fan into Coach's pane regardless of trigger. The PUT endpoint
is the operator's escape hatch for hand-editing the whole file at
once; it parses + validates the body before writing through the same
synchronous kDrive mirror as the per-entry helpers.

### Project objectives

```
GET /api/projects/{slug}/objectives
PUT /api/projects/{slug}/objectives    # body: {"text": "..."}
```

All four endpoints require `HARNESS_TOKEN` and `audit_actor`.

---

## 10. Database schema

One new table:

```sql
CREATE TABLE coach_recurrence (
  id            INTEGER PRIMARY KEY,
  project_id    TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('tick', 'repeat', 'cron')),
  cadence       TEXT NOT NULL,         -- minutes (as str) for tick/repeat;
                                       -- DSL string for cron
  tz            TEXT,                  -- e.g. 'Europe/Paris'; required for cron
  prompt        TEXT,                  -- NULL for tick
  enabled       INTEGER NOT NULL DEFAULT 1,
  next_fire_at  TEXT,                  -- UTC ISO; recomputed after each fire
  last_fired_at TEXT,
  created_at    TEXT NOT NULL,
  created_by    TEXT,                  -- 'human' | 'coach' | 'telegram' | ...
  FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE INDEX idx_recurrence_project ON coach_recurrence(project_id, enabled);
CREATE UNIQUE INDEX idx_recurrence_one_tick
  ON coach_recurrence(project_id) WHERE kind = 'tick';
```

Migration: `recurrence_v1`. Stamps two `team_config` rows:

- `recurrence_v1_seeded` = `'1'` — gates the one-shot env-var seed so
  later boots skip seeding regardless of the env var.
- `schema_version` = `'recurrence_v1'` — forward-compatible signal for
  a future versioned-migration runner; today's codebase otherwise
  relies on `CREATE TABLE IF NOT EXISTS`.

On migration, copy over today's in-memory tick interval (if non-zero, from
`HARNESS_COACH_TICK_INTERVAL` env) into a tick row for **every existing
project** (not just active) so a multi-project install carries the
operator's intent across all of them. Today's `/repeat` state is
in-memory and not migrated — operator re-issues the slash command if
they want it back.

---

## 11. Scheduler runtime

A single `recurrence_scheduler` background task replaces today's per-flavor
loops. On every tick (every 30 seconds, configurable via
`HARNESS_RECURRENCE_TICK_SECONDS`):

1. Read all enabled rows for the **active project** where
   `next_fire_at <= now_utc`.
2. For each due row (sequentially; see §15.1 for ordering):
   - If Coach is mid-turn OR a prior row in this same pass already
     fired:
     - **Tick rows** → emit `recurrence_deferred` (reason
       `coach_busy`), do NOT advance `next_fire_at`. The row stays
       overdue and re-evaluates next pass; as soon as Coach goes idle
       it fires. No make-up storm — only one fire per idle window.
     - **Repeat / cron rows** → emit `recurrence_skipped` (reason
       `coach_busy`), advance `next_fire_at` past now, continue.
       Tracking the "already fired this pass" flag locally is
       essential because `await run_agent` blocks until the Coach
       turn completes — without the flag, the next due row would see
       `_coach_is_working() == False` again and stack onto the
       just-finished turn.
   - Else if the daily cost cap is hit:
     - **Tick rows** → emit `recurrence_deferred` (reason
       `cost_capped`); do NOT advance. Re-evaluates next pass (caps
       roll over at UTC midnight or when spend rolls off).
     - **Repeat / cron rows** → emit `recurrence_skipped` (reason
       `cost_capped`), advance `next_fire_at`, continue.
   - Else → spawn the appropriate Coach turn:
     - `tick` → §4 composed prompt.
     - `repeat` → row's `prompt`.
     - `cron` → row's `prompt`.
   - Emit `recurrence_fired` with row id and kind.
   - For one-shot crons, set `enabled=false` and emit `recurrence_disabled`
     with `reason: one_shot_complete`.
   - **One-shot terminal skip ordering**: when a one-shot cron is both
     past AND skipped (busy / cost-capped), the harness emits
     `recurrence_skipped` first, then `recurrence_disabled` with
     `reason: one_shot_complete`. The skip carries the cause; the
     disable closes the row. Both events are needed so the operator
     can audit why a one-shot never fired.
3. Recompute `next_fire_at` (only for rows that fired this pass —
   deferred rows keep their existing `next_fire_at`):
   - tick: `last_fired_at + max(cadence_minutes, 0)` (in UTC). Cadence
     `0` means `next_fire_at = last_fired_at`, so the next scheduler
     pass treats the row as immediately due — and the only thing that
     keeps it from re-firing instantly is the busy/cost-cap defer
     check. Effectively: "fire as soon as Coach is idle, every time."
   - repeat: `last_fired_at + cadence_minutes` (in UTC). Repeat
     cadence MUST be `>= 1`.
   - cron: parse DSL, compute next match in row's TZ, convert to UTC.
4. Persist updated `last_fired_at` and `next_fire_at`.

The scheduler reads from the **active project** only. On project switch,
the scheduler refreshes its row set (no restart needed). Inactive-project
rows do not fire.

The 30-second tick is the **resolution** — a cron set to `daily 09:00`
fires within 30 seconds of 09:00. That's good enough for the use cases
(daily reports, weekly digests). If finer resolution is needed later,
drop the tick to 5–10s.

---

## 12. Recurrence pane (UI)

### 12.1 Rail icon

New left-rail button, third group (alongside files / project / pause /
env-toggle / settings). **CSS-drawn circular-arrows icon** — two arcs
forming a circle with arrowheads. No emoji (per the no-emoji invariant).

Follow the existing icon precedent (`.projects-icon-*`, `.files-icon-*`,
status dots): name the parts `.recurrence-icon-arc-top`,
`.recurrence-icon-arc-bottom`, `.recurrence-icon-head-*`. Use
`currentColor` for the strokes so it inherits the rail's accent state.

### 12.2 Pane structure

The pane opens to the **right side**, alongside the EnvPane. They can be
open simultaneously (operator can monitor recurrences and decisions side
by side). Persisted open/closed state in localStorage
(`harness_recurrence_pane_v1`).

Three sections:

#### Tick

A single block:

- **Interval input** — number of minutes. Used by the **Create / Update**
  button below the card. Updating cadence on a disabled row also
  re-enables it (matches the `/tick N` slash-command contract in §8 —
  `PUT /api/coach/tick {minutes}` auto-enables a disabled row).
- **Status dot** — green when enabled, gray when disabled.
- **Next fire** — relative ("in 23 min") + absolute timestamp.
- **Last fire** — relative ("12 min ago") + absolute timestamp.
- **"Fire now" button** — calls `POST /api/coach/tick`.
- **Enable / Disable button** — toggles the existing row without
  changing cadence. Disable preserves the row (sets `next_fire_at=NULL`);
  Enable schedules `next_fire_at` one cadence-unit out from now.
- **Delete button** — removes the tick row entirely (calls
  `DELETE /api/recurrences/{id}`). Symmetric with Repeats / Crons.

If no tick row exists, the card collapses to "No tick yet — set one
below" and the Create button below adopts the typed cadence.

Errors from any tick action surface as a banner under the pane head
with a dismiss × — keeps failed PUT/DELETE/POST visible instead of
silently swallowing them.

#### Repeats

List of cards, each:

- **Prompt** (editable, textarea).
- **Cadence** (minutes, editable, numeric input).
- **Enabled** toggle.
- **Next fire** / **Last fire** (read-only).
- **Delete** button.

"+ Add repeat" button at the bottom opens a blank card.

#### Crons

List of cards, each:

- **Schedule** (editable, free-text input — DSL per §5).
- **Prompt** (editable, textarea).
- **TZ** (read-only, captured at create time, but re-saving picks up
  the operator's current TZ).
- **Enabled** toggle.
- **Next fire** / **Last fire**.
- **Delete** button.

On schedule edit, the input does live DSL validation; invalid input
disables the Save button and shows a parse error.

"+ Add cron" button at the bottom.

### 12.3 EnvPane additions

Two new sections in EnvPane (not in the recurrence pane — they're
project-state, not recurrences):

- **Project objectives** — multiline editor + Save. Reads/writes the
  per-project `project-objectives.md`.
- **Coach todos** — checkbox list of open todos with click-to-expand
  description. Strikethrough on complete (calls `coord_complete_todo` via
  HTTP shim). "+ Add todo" form. Link to archive file.

These follow the existing EnvPane section pattern (Memory, Inbox,
Decisions, Knowledge, Outputs).

### 12.4 Live refresh

Both panes refresh on these WebSocket events:

- `recurrence_added`, `recurrence_changed`, `recurrence_deleted`,
  `recurrence_fired`, `recurrence_skipped`, `recurrence_disabled`.
- `coach_todo_added`, `coach_todo_completed`, `coach_todo_updated`.
- `objectives_updated`.

---

## 13. Events emitted

| Event type | Payload | Where surfaced |
|---|---|---|
| `recurrence_added` | id, kind, cadence, tz, prompt, enabled, project_id | Coach pane (system row) + Recurrence pane |
| `recurrence_changed` | id, kind, before, after, project_id | Recurrence pane only |
| `recurrence_deleted` | id, kind, project_id | Recurrence pane only |
| `recurrence_fired` | id, kind, prompt_excerpt, project_id | Coach pane (subtle, sticky off) |
| `recurrence_skipped` | id, kind, reason (`"coach_busy"` / `"cost_capped"`), project_id | Coach pane (system row). Repeat / cron only — tick rows defer instead |
| `recurrence_deferred` | id, kind (`"tick"`), reason (`"coach_busy"` / `"cost_capped"`), project_id | Coach pane (subtle). Emitted ONCE per defer episode (on transition into deferred state), not every scheduler pass — otherwise a long Coach turn with a 5-minute tick would spam the log. The next `recurrence_fired` resets the latch |
| `recurrence_disabled` | id, kind, reason (`"one_shot_complete"`), project_id | Coach pane + Recurrence pane |
| `coach_todo_added` | id, title, due | Coach pane + EnvPane |
| `coach_todo_completed` | id, title | Coach pane + EnvPane |
| `coach_todo_updated` | id, fields | Coach pane + EnvPane |
| `objectives_updated` | project_id | EnvPane (full re-read) |

All events have the standard envelope (`__id`, `agent_id`, `ts`,
`project_id`). `recurrence_*` and `coach_todo_*` events all use
`agent_id="coach"` so they fan into Coach's pane regardless of who
triggered them (scheduler / Coach via MCP / human via HTTP shim);
the `actor` envelope key records the real audit trigger
(`{source, ip, ua}`) so cross-device introspection still works.

`before`/`after` snapshots in `recurrence_changed` are
`{cadence, enabled, tz, prompt}` dicts. For tick rows, `tz` and
`prompt` are always `null` but the keys are present so a UI consumer
can index uniformly across kinds. `enabled` is included on
`recurrence_added` (not in original spec table) because the row's
enabled state is part of "what just got created" — useful for the UI
in case future endpoints allow creating disabled rows.

---

## 14. Migration path

### From today's harness

1. Add the `coach_recurrence` table (migration `recurrence_v1`).
2. If `HARNESS_COACH_TICK_INTERVAL` env is non-zero, seed a tick row for the
   default/active project with `cadence = HARNESS_COACH_TICK_INTERVAL / 60`
   minutes (rounded up, min 1). Emit `recurrence_added` with
   `created_by: "env_migration"`.
3. Remove the old per-flavor loops:
   - Old Coach autoloop → replaced by scheduler reading tick rows.
   - Old Coach repeat loop → replaced by scheduler reading repeat rows.
   - `_coach_tick_interval`, `_coach_repeat_*` module globals deleted.
4. Remove env vars from documentation:
   - `HARNESS_COACH_TICK_INTERVAL` is honored on first migration only,
     then ignored. Document as deprecated.
5. UI: remove `/loop` from slash command list, add `/tick` / `/repeat` /
   `/cron` per §8.

### Bootstrapping a new project

When `coord_create_project` (or whatever creates a project today) runs:

- Do NOT create a tick row automatically. Operator opts in.
- Do NOT create `coach-todos.md` or `project-objectives.md`. They're
  created on first write.
- The very first Coach turn for a fresh project (detected by absence of
  `project-objectives.md` AND empty inbox) prompts the operator:

  > This project has no objectives defined. What are we trying to
  > accomplish? Once you reply, I'll save them to project-objectives.md.

- Coach saves objectives via Write tool. Subsequent ticks then proceed
  normally.

---

## 15. Edge cases & invariants

1. **Multiple due rows in one scheduler tick**: fire them sequentially
   (don't parallelize Coach turns). After the first fires, the rest
   defer (tick) or skip (repeat/cron) with `reason="coach_busy"`. For
   repeat/cron, `next_fire_at` advances past the first fire's end; for
   tick, `next_fire_at` is left in place so the row picks up next
   pass. Implementation note: because the scheduler `await`s the full
   Coach turn in `_fire_row`, by the time the next row's iteration
   runs `_coach_is_working()` would return `False` again — so the
   loop also keeps a local `fired_in_pass` flag and forces
   `busy=True` for any subsequent rows in the same pass. Without
   that flag, a busy day with several due rows would stack turns
   back-to-back instead of deferring/skipping.

1b. **Tick + cadence 0 + nothing to do**: Coach's tick prompt walks
    the priority list (§4) and ends quietly when objectives are
    absent. With cadence `0`, the next pass would re-fire instantly
    — and the empty branches would burn budget on a tight loop. Two
    natural backstops keep this safe: (a) the §4 prompt explicitly
    licenses ending without acting only when objectives are absent,
    so the typical project has at least one actionable rung; (b) the
    daily cost cap and `coord_set_tick_interval` give Coach the
    knob to throttle DOWN once it notices the loop is empty (e.g.
    "no inbox, no kanban, no todos, objectives stable → set tick to
    30min"). Cadence `0` is a power-user mode — Coach is expected
    to manage it.

2. **Project switch mid-fire**: the in-flight Coach turn completes against
   the original project. The scheduler's next pass picks up the new
   project's rows.

3. **Daylight Saving transitions**: cron schedules use named TZs (e.g.
   `Europe/Paris`), so DST is handled by the TZ database. A `daily 09:00`
   schedule fires at 09:00 local time on both sides of DST.

4. **One-shot cron in the past**: rejected at create time with a parse
   error. ("That schedule is in the past.")

5. **Tick fires while objectives empty**:
   - First time → tick prompt appends an elicitation hint asking the
     operator to define objectives; Coach decides whether to actually
     send it based on the inbox/todos priority order.
   - Subsequent ticks → harness scans Coach's last 50 outgoing
     `message_sent` events for objectives-related wording
     (`project-objectives.md`, `define...objectives`, `trying to
     accomplish`); if Coach has already asked, the elicitation hint
     is suppressed. This replaced an earlier `team_config` flag that
     marked the elicitation as "asked" the first time the harness
     even *considered* showing it — which suppressed the hint even
     when Coach never actually saw it (busy inbox, etc.). The
     event-log scan is the source of truth: Coach's actual behavior
     drives what the next tick sees.
   - Once objectives are saved, the system-prompt injector picks up
     the file on the next turn and the elicitation hint stops
     appearing entirely (presence check short-circuits the scan).

6. **Coach todos file becomes corrupted by hand-editing**: the PUT
   `/api/projects/{slug}/coach-todos` endpoint parses the body
   through `coach_todos.parse` and rejects payloads that look like
   bullets but yield zero parseable entries (heuristic: contains
   `- [` but no `<!-- id:t-N -->`). MCP tools also validate every
   write. If the file is corrupted out-of-band (someone edits via
   the kDrive web UI), the system-prompt injector surfaces only the
   parseable subset and Coach reports the parse failure on the next
   turn — non-fatal, the rest of the team is unaffected.

7. **Project deleted while recurrences exist**: `ON DELETE CASCADE` on the
   FK drops the rows. Scheduler's next pass sees no rows for that project.

8. **Cap on rows per project**: soft-cap 50 (`HARNESS_MAX_RECURRENCES_PER_PROJECT`).
   Beyond that, POST returns 409 with a "trim some first" message. Prevents
   accidental fork-bombs.

9. **Cost cap interaction**: a recurrence fire is subject to the same
   per-agent and team-daily cost caps as any other Coach turn. When capped,
   the fire is skipped with `recurrence_skipped` reason `cost_capped`.

10. **Telegram trigger interaction**: incoming Telegram messages still
    auto-wake Coach. They are a separate trigger path and do not interact
    with the recurrence scheduler.

11. **Cross-project mutation via stale UI**: PATCH and DELETE on
    `/api/recurrences/{id}` are scoped to the active project and
    return `404` for rows belonging to other projects. Without this
    guard, a stale browser tab pinned to project A could silently
    mutate project B's recurrences after the operator switched —
    and the operator would have no way to see the change in their
    Recurrence pane (which only lists active-project rows). POST is
    inherently scoped because it always creates under the active
    project.

12. **Cron edits and timezone re-anchoring**: the Recurrence pane
    sends the browser's current TZ on every cron-row save, so a move
    (DST shift, laptop relocation, switching from local to UTC for
    a remote install) is reflected in the next computed fire. The
    DB row's `tz` column is the source of truth — the spec §5.2
    "TZ captured at save time, re-saving picks up the operator's
    current TZ" rule is enforced client-side, not requiring an
    explicit re-save click.

---

## 15.5 Related: Compass auto-audit watcher (NOT a recurrence)

The Compass auto-audit watcher
([server/compass/audit_watcher.py](../server/compass/audit_watcher.py),
spec'd in `Docs/compass-specs.md` §5.5) is sometimes mistaken for a
recurrence flavor because it auto-fires work. It is not — and the
distinction matters for anyone editing either system.

| Axis | Coach recurrences (this doc) | Compass auto-audit |
|---|---|---|
| **Trigger** | Wall clock (interval / cron) | Event bus (`task_stage_changed{from=plan,to=execute}` — kanban plan-exit only) |
| **Cardinality** | Per-project, persisted in `coach_recurrence` | Singleton background subscriber, not persisted |
| **Subject** | Spawns a Coach turn (`run_agent`) | Calls `compass.audit.audit_work` (one-shot LLM call, no Coach session) |
| **Cost cap** | Per-agent + team daily caps inside `agents._spawn_allowed` | Team daily cap inside the watcher itself, before the LLM call |
| **Skip semantics** | `recurrence_skipped` event with reason | Silent drop; gated by debounce + enable flag + cost cap |
| **UI surface** | Recurrence pane (`__recurrences`) | Compass pane (`__compass`) audit log section |
| **Lifecycle owner** | `recurrence_scheduler_loop` background task | `start_audit_watcher` / `stop_audit_watcher` (own task handle, mirrors telegram pattern) |

Both live alongside each other in `main.py:lifespan`. They share the
"only fire when the project is enabled" pattern but otherwise have
nothing in common at the table or scheduler level.

If the future brings a unified "background triggers" surface, this
table is the inventory. Until then: a recurrence is a Coach turn
schedule; an audit watcher is an event subscription.

---

## 16. Out of scope (for v2)

- Player-targetable recurrences. Still Coach-only.
- Stop conditions / max-iterations. Recurrences run until disabled.
- Loop backoff or jitter. Fixed cadence only.
- Cron expressions (raw 5-field). Friendly DSL only.
- Conditional recurrences ("fire only if X"). Operator can express that
  as a prompt that early-exits.
- Per-recurrence cost caps. Use the existing per-agent cap.
- Importing routines from the Claude Code parent layer (`/schedule`).
  Different system, kept distinct.

---

## 17. Implementation order (suggested)

1. Migration + table + scheduler (no UI). Tick rows seeded from env var. **completed and audited**
2. Slash commands + HTTP API for recurrences. **completed and audited**
3. Coach todos: file format, MCP tools, system-prompt injection. **completed and audited**
4. Project objectives: file, system-prompt injection, first-turn prompt. **completed and audited**
5. Smart tick prompt composition (replaces `COACH_TICK_PROMPT`). **completed and audited**
6. Recurrence pane UI + rail icon. **completed and audited**
7. EnvPane sections for todos and objectives. **completed and audited**
8. Migration cleanup: delete old loop functions, env-var deprecation note. **completed and audited**

Each phase is independently shippable. Phases 1–2 give the operator parity
with today's `/loop` + `/repeat` plus persistence + cron. Phases 3–5 turn
the tick into the smart default. Phases 6–7 are pure ergonomics.

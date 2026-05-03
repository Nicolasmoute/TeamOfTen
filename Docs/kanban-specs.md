# Kanban task lifecycle — Specification

> **Subordinate to [TOT-specs.md](TOT-specs.md).** When this doc and
> TOT-specs disagree, TOT-specs wins. This file goes deeper on the
> kanban subsystem (stages, roles, artifacts, the auto-advance
> subscriber, the idle-Player poller) but cannot redefine fields,
> endpoints, events, or invariants that TOT-specs declares.

**Status:** Shipped (2026-05-03).
**Target:** TeamOfTen multi-agent harness (Python, Claude Agent SDK, kDrive-backed shared state, single-VPS)
**Version:** 0.1

---

## 1 · Overview

### 1.1 Purpose

Tasks in TeamOfTen flow through an explicit **kanban-shaped lifecycle**: every task is in exactly one of six stages — `plan` → `execute` → `audit_syntax` → `audit_semantics` → `ship` → `archive` — and each stage produces durable, browseable markdown artifacts. The shape is event-driven: commits and audit-report submissions auto-advance tasks; Coach assigns Players to roles at each stage; Coach never executes work themselves.

### 1.2 Scope

This spec covers:
- The six-stage state machine and its valid transitions.
- The five **roles** (planner / executor / auditor_syntax / auditor_semantics / shipper) that Players can be assigned to.
- The artifacts produced at each stage (`spec.md`, `audits/audit_<round>_<kind>.md`, plus the parallel informational Compass audit report).
- The MCP tools Coach uses to plan + assign and Players use to execute / audit / ship.
- The HTTP endpoints, bus events, and UI surface (`__kanban` slot).
- The schema (`tasks.status` enum, `task_role_assignments` table, denormalized card-render columns).
- The auto-advance subscriber, idle-Player poller, CLAUDE.md kanban block, and Telegram escalation hooks.

Out of scope: drag-to-move on the board (deferred to v2), per-task priority changes from the UI (priority is set at create time only — kanban v1 does not include reprioritization tooling), per-task time-estimation features.

### 1.3 Glossary

| Term | Meaning |
|---|---|
| **Stage** | One of `plan` / `execute` / `audit_syntax` / `audit_semantics` / `ship` / `archive`. Stored in `tasks.status` (the enum is reused for what was previously called "task status"). |
| **Role** | A function a Player performs on a specific task: `planner`, `executor`, `auditor_syntax`, `auditor_semantics`, `shipper`. Stored as rows in `task_role_assignments`. |
| **Pool** | A list of eligible Players (`eligible_owners`) on a role assignment. The first Player to call `coord_claim_task` (or be auto-claimed via the executor pool) wins; the others' wakes degrade gracefully. |
| **Round** | The Nth audit cycle on a task. A task that fails syntax-audit twice has rounds 1 (fail) and 2 in `audits/audit_1_syntax.md` / `audit_2_syntax.md`. |
| **Verdict** | An auditor's decision: `pass` or `fail`. Drives the auto-advance subscriber. |
| **Spec** | The `spec.md` markdown file produced in the plan stage. Required for `standard`-complexity tasks; optional for `simple`. |
| **Active assignment** | The row in `task_role_assignments` for `(task_id, role)` with `superseded_by IS NULL` and the most recent `assigned_at`. |

---

## 2 · Stage state machine

### 2.1 Stages

- **plan** — task created, no executor yet (or has a planner working on the spec). Cards in this stage may have `spec_path = NULL` if the spec hasn't been written yet.
- **execute** — owned by a Player. Sub-state inside execute: `started_at IS NULL` (assigned but not yet picked up; hollow avatar) vs `started_at` populated (actively working; filled avatar).
- **audit_syntax** — commit pushed, awaiting tests/CI/lint confirmation by the assigned syntax auditor.
- **audit_semantics** — syntax green, awaiting alignment-with-goals confirmation by the assigned semantic auditor.
- **ship** — both audits green, ready for the assigned shipper to merge / publish.
- **archive** — terminal. `archived_at` set. `cancelled_at` non-null distinguishes a cancellation from a delivered task.

The two timestamp columns inside `execute` (`claimed_at` and `started_at`) carry the "actively working" signal:

| Entry path | `claimed_at` | `started_at` |
|---|---|---|
| Hard-assign (`coord_assign_task(to='p3')`) | now | NULL until p3's first auto-wake fires |
| Pool-claim (`coord_claim_task` from `eligible_owners`) | now | now (self-claim **is** starting) |
| Self-claim (Player picks up an unowned plan-stage task) | now | now |

Both are kept across the audit-fail revert: `started_at` is cleared so the card flips to "assigned, not started" and the next auto-wake will repopulate it; `claimed_at` is preserved (the executor still owns the task; they just haven't re-engaged yet).

### 2.2 Valid transitions

```
plan          → {execute, archive}
execute       → {audit_syntax, archive}
audit_syntax  → {audit_semantics, execute}
audit_semantics → {ship, execute}
ship          → {archive}
archive       → {}
```

Cancellation is `<any non-terminal stage> → archive` with `cancelled_at = now()`. The `blocked` flag is orthogonal and toggleable in any non-terminal stage; it does not change `tasks.status`.

The validator lives at [server/tools.py:VALID_TRANSITIONS](server/tools.py).

### 2.3 Role-completion gating

Stage transitions are gated by **role-completion events**, not by Coach's manual call alone:

| Transition | Required condition |
|---|---|
| `plan → execute` | `tasks.spec_path IS NOT NULL` (standard) AND active executor assignment with `owner IS NOT NULL`. |
| `execute → audit_syntax` | `commit_pushed` event with the task's id (standard complexity). |
| `execute → archive` | `commit_pushed` event with the task's id AND `complexity = 'simple'`. |
| `audit_syntax → audit_semantics` | Active syntax-auditor assignment has `verdict = 'pass'`. |
| `audit_semantics → ship` | Active semantic-auditor assignment has `verdict = 'pass'`. |
| `ship → archive` | `task_shipped` event from the assigned shipper. |
| `audit_* → execute` (revert) | Active auditor assignment has `verdict = 'fail'`. Auto-fires; clears `tasks.started_at` so the executor's resume is clean. |

On a fail revert, the auditor's role-assignment row stays **terminal** — its `verdict` and `report_path` are written, `completed_at` set, and that audit round is done. The next round (after the executor re-commits) gets a **new** `auditor_<kind>` row inserted by `coord_assign_auditor` and the `round` counter on the next report increments. The executor's role row, by contrast, is **not** superseded — they're still the executor — but `tasks.started_at` is cleared so the next auto-wake can repopulate it cleanly. This split is what lets the card-expansion view render the audit history (round 1 fail → round 2 pass → ...) while the latest-only fields on the row stay accurate for cheap card rendering.

`coord_advance_task_stage` is the explicit Coach-override tool that bypasses the role-completion gate when an assignment stalls.

### 2.4 Blocked flag

`tasks.blocked` (INTEGER 0/1) plus `tasks.blocked_reason` (TEXT). Set/cleared via `coord_set_task_blocked` (Coach or owner) or `POST /api/tasks/{id}/blocked`. Doesn't change stage; cards render a `BLOCKED` flag overlay.

### 2.5 Cancellation

`POST /api/tasks/{id}/cancel` or `coord_update_task(status='archive')` with the legacy alias `cancelled` accepted. Sets `cancelled_at = now()` and `archived_at = now()`. The active board (`GET /api/tasks/board`) excludes archive entirely; cancelled tasks surface in `GET /api/tasks/archive?include_cancelled=true`.

---

## 3 · Complexity

### 3.1 Standard vs simple

`tasks.complexity ∈ {'standard', 'simple'}` (default `standard`). Set at create time or with `coord_set_task_complexity`.

### 3.2 Spec gate

| Complexity | Spec required for `plan → execute` |
|---|---|
| standard | yes (reject `coord_assign_task` / `coord_claim_task` until `spec_path` is set) |
| simple | no (title + description on the row are sufficient) |

The gate is also enforced by `POST /api/tasks/{id}/stage` unless `force=true`. `coord_assign_planner` is the one tool that succeeds without a spec — it's the path that produces one.

### 3.3 Simple-task self-audit discipline

`simple` tasks bypass `audit_syntax` / `audit_semantics` / `ship` entirely: a `commit_pushed` while the task is in `execute` jumps straight to `archive`. The auto-wake prompt sent to the executor of a simple task includes verbatim text instructing them to **self-audit** (run the relevant tests, sanity-check the change) before pushing — the board archives directly on commit, with no separate audit pass.

The verbatim auto-wake text (must appear in the spawn prompt's `entry_prompt` field; verifiable in the `agent_started` event payload):

> *task &lt;task_id&gt; is marked simple — self-audit (run tests / sanity-check the change) before coord_commit_push because the board archives directly on commit, no separate audit pass.*

Coach is told in their lifecycle-policy prompt block (see §13.4) to mark a task simple **only** when the change is small and well-bounded enough that self-audit is sufficient. If in doubt, Coach leaves it standard and assigns auditors.

### 3.4 Compass auto-audit

Compass auto-audit fires on `commit_pushed` regardless of complexity (see [compass-specs.md §5.5](compass-specs.md)). Its verdict is **informational** — written to `tasks.compass_audit_verdict` + `tasks.compass_audit_report_path` for the card pip, never advancing or reverting a stage. The Player auditor (for standard tasks) is the gate.

---

## 4 · Roles

### 4.1 Strict separation

- **Coach** plans (writes the spec) and delegates everything else. Coach never executes, audits, or merges.
- **Players** execute, audit, and ship. Coach can also delegate planning to a Player via `coord_assign_planner`.

### 4.2 Role inventory

| Role | Stage entered | Tool that completes the role |
|---|---|---|
| `planner` | plan | `coord_write_task_spec` (sets the row's `completed_at`) |
| `executor` | execute | `coord_commit_push(task_id=...)` |
| `auditor_syntax` | audit_syntax | `coord_submit_audit_report(kind='syntax', ...)` |
| `auditor_semantics` | audit_semantics | `coord_submit_audit_report(kind='semantics', ...)` |
| `shipper` | ship | `coord_mark_shipped(task_id, note?)` |

### 4.3 Single-Player vs pool assignment

`coord_assign_*` tools accept `to` as either a string (`'p3'` — hard-assign) or a comma-list (`'p1,p2,p3'` — pool). Pool-form leaves `tasks.owner` NULL until a Player calls `coord_claim_task`; all eligible Players are auto-woken at post time. Atomic UPDATE on claim ensures only one wins; losers' wakes see the row already claimed and degrade gracefully.

### 4.4 Auditor-equals-executor warning

`coord_assign_auditor` does **not** block the case where the auditor matches the executor. It emits an `audit_self_review_warning` event (rendered in Coach's pane and forwarded to Telegram) so the human can spot weak self-review patterns. Useful when the team is small or specialists are scarce; not desirable on big-stakes tasks.

### 4.5 Idle-Player polling

A periodic loop (default 5 min, see [server/idle_poller.py](server/idle_poller.py)) wakes idle Players who could be claiming pool work but aren't (e.g. their initial wake landed while they were over-cap; the harness was paused; the Player ignored the wake). See §10 for full design.

---

## 5 · Artifacts

### 5.1 spec.md

Produced in the plan stage. Markdown document, full overwrite each time `coord_write_task_spec` (or `POST /api/tasks/{id}/spec`) is called — rolling history lives in the event stream + git.

Path: `/data/projects/<project_id>/working/tasks/<task_id>/spec.md`. Synchronously mirrored to kDrive at `TOT/projects/<project_id>/tasks/<task_id>/spec.md`.

Helper: [server/tasks.py:write_task_spec](server/tasks.py).

**Spec template** — `coord_write_task_spec` writes a starter when first called with no body, and accepts an explicit body otherwise. The starter shape:

```markdown
# <task title>

- **Task ID:** t-2026-05-03-abc12345
- **Created by:** human | coach | p3
- **Created at:** 2026-05-03 14:22:11 UTC
- **Priority:** urgent | high | normal | low
- **Complexity:** standard | simple
- **Owner:** (assigned at execute time)

## Goal
<one or two sentences: what is this task trying to achieve>

## Done looks like
<concrete acceptance criteria — what would let Coach mark this archive>

## Constraints / context
<anything the executor needs to know that isn't obvious from the codebase: stakeholder preferences, related decisions, gotchas, links to relevant truth/wiki entries>

## References
<links to related tasks, decisions, knowledge entries, prior audits>

## Notes
<scratchpad — Coach can add planning thoughts here; Player can add discovery notes during execute>
```

The helper does **not** auto-merge with prior content — every write is a full overwrite. Rolling history lives in the `task_spec_written` event stream and (if the working dir is git-tracked) in git history. The kDrive folder is a live working dir, not a versioned store.

### 5.2 audits/audit_&lt;round&gt;_&lt;kind&gt;.md

Player audit reports. Written by `coord_submit_audit_report`; one file per (round, kind). The latest is surfaced on the card; older rounds stay on disk for human review (and link from the card-expansion view).

Path: `/data/projects/<project_id>/working/tasks/<task_id>/audits/audit_<round>_<kind>.md`. kDrive mirror at `TOT/projects/<project_id>/tasks/<task_id>/audits/...`.

The card surface fields are denormalized for cheap rendering:
- `tasks.latest_audit_report_path`
- `tasks.latest_audit_kind` (`'syntax' | 'semantics'`)
- `tasks.latest_audit_verdict` (`'pass' | 'fail'`)

### 5.3 Compass audit_&lt;id&gt;.md

The parallel **informational** Compass-authored report. Generated by [server/compass/audit.py:write_audit_report_md](server/compass/audit.py) in addition to the existing `audits.jsonl` append. Lives at `/data/projects/<project_id>/working/compass/audit_reports/<audit_id>.md` (kDrive-mirrored).

The kanban subscriber writes a denormalized pointer onto the task:
- `tasks.compass_audit_report_path`
- `tasks.compass_audit_verdict` (`'aligned' | 'confident_drift' | 'uncertain_drift'`)

**Report template** — written every time `append_audit` runs, alongside the structured jsonl row:

```markdown
# Audit <audit_id>

- **Verdict:** confident_drift | uncertain_drift | aligned
- **When:** 2026-05-03 14:22:11 UTC
- **Artifact:** commit_pushed (sha 8a3f2c...) by p3
- **Task:** [t-2026-05-03-abc12345 — "fix header layout"](task://t-2026-05-03-abc12345)

## Summary
<the short summary line that's already in AuditRecord.summary>

## Message to Coach
<message_to_coach verbatim>

## Contradicting lattice statements
- **s-0042** (weight 0.91): "All header layout work must preserve the sticky behavior on mobile."
- **s-0107** (weight 0.85): "..."
<each contradicting_id resolved against the live lattice + body inlined so the report is self-contained even if the lattice later evolves>

## Related question
<if AuditRecord.question_id is set, link to the queued question + body>

---
*Generated by the Compass audit-watcher. The structured record lives in `audits.jsonl`; this file is the human-readable mirror.*
```

Lattice resolution happens at write time so the report is self-contained — important because a settled or archived statement could later disappear from the active lattice and a stale `s-XXXX` reference would otherwise dangle. Both jsonl + md are written from the same `append_audit` call site so they can't diverge.

### 5.4 Path conventions + kDrive mirror

Every artifact write goes through an atomic `tempfile + os.replace` then a best-effort kDrive mirror via the existing [server/kdrive.py](server/kdrive.py) helpers. A failed mirror logs but does not block the in-process write — the in-container path is the source of truth.

Path-traversal defense: `task_id` is validated against `r"t-\d{4}-\d{2}-\d{2}-[a-f0-9]{8}"` before any file write resolves the directory.

### 5.5 Files-pane integration

Cards render `[spec]`, `[audit (kind, round N)]`, and `[compass]` links via the existing `data-harness-path` mechanism (see [server/static/app.js](server/static/app.js)'s document-level click listener). Clicking opens the `__files` pane and longest-prefix-matches the absolute path against `/api/files/roots`.

---

## 6 · MCP tools

All new tools are registered in [server/tools.py](server/tools.py)'s `_tools` map and `ALLOWED_COORD_TOOLS` set. Coach-only enforcement uses the existing `_require_coach` helper.

### 6.1 Coach-only — assignment + meta

| Tool | Params | Purpose |
|---|---|---|
| `coord_assign_planner` | `task_id, to` | Optional: delegate spec writing to a Player. Inserts a `planner` role assignment. Auto-wakes the Player(s). |
| `coord_assign_task` | `task_id, to` | **Modified.** Hard-assign (`to='p3'`) or pool (`to='p1,p2'`). Inserts `executor` role assignment. Spec gate enforced for standard tasks. Auto-wake prompt is complexity-aware (simple tasks get the self-audit reminder verbatim). |
| `coord_assign_auditor` | `task_id, to, kind` | `kind ∈ {'syntax', 'semantics'}`. Single or pool. Emits `audit_self_review_warning` if auditor == executor (does not block). |
| `coord_assign_shipper` | `task_id, to` | Single or pool. Auto-wakes with merge prompt. |
| `coord_set_task_complexity` | `task_id, complexity` | `'simple' | 'standard'`. Toggles the bypass. |
| `coord_advance_task_stage` | `task_id, stage, note?` | Explicit override. Bypasses role-completion gate. |

### 6.2 Coach + planner + owner — spec authoring

| Tool | Params | Purpose |
|---|---|---|
| `coord_write_task_spec` | `task_id, body` | Writes `spec.md`. Permission: Coach, the active planner, the executor, or the owner of the task's parent. Sets `spec_path` + `spec_written_at`; marks the planner role row complete. |

### 6.3 Player-only — role artifacts + introspection

| Tool | Params | Purpose |
|---|---|---|
| `coord_my_assignments` | (none) | Returns the caller's full plate in four buckets: active executor task / pending audits / pending ship / eligible pools. |
| `coord_submit_audit_report` | `task_id, kind, body, verdict` | Validates active `auditor_<kind>` role for caller. Writes the audit `.md`, marks the role row complete with verdict, emits `audit_report_submitted`. |
| `coord_mark_shipped` | `task_id, note?` | Validates active `shipper` role. Marks complete + emits `task_shipped`. |
| `coord_claim_task` | `task_id` | **Modified.** Validates `eligible_owners` membership (or empty pool + plan stage). Atomic UPDATE. Sets `tasks.owner` + `claimed_at = started_at = now()`. |

### 6.4 Owner + Coach

| Tool | Params | Purpose |
|---|---|---|
| `coord_set_task_blocked` | `task_id, blocked, reason?` | Toggles the orthogonal flag. |

### 6.5 Modified existing tools

- `coord_create_task` accepts three new optional kwargs:
  - `complexity` (default `'standard'`) — `'simple'` flips on the audit-bypass discipline.
  - `spec` (markdown body) — when provided, writes `spec.md` in the same call so a standard task can land already-spec'd. Equivalent to a `coord_create_task` followed by `coord_write_task_spec` but atomic.
  - `eligible_executors` (list of Player slots) — pre-populates the executor pool so a `coord_assign_task` is not strictly required for Players to discover the task via `coord_my_assignments`. Coach is still expected to call `coord_assign_task` when ready; the pre-pool just lowers the latency of "task exists; could you grab it" surfacing.
- `coord_commit_push` accepts new optional `task_id`. When provided, the emitted `commit_pushed` event carries it; the kanban subscriber reads it and advances. Without `task_id`, behavior is unchanged (Compass auto-audit still fires regardless).
- `coord_update_task` validates against the new transition map. Legacy aliases (`done` → `archive`, `cancelled` → `archive` + `cancelled_at`) are accepted for one release.

---

## 7 · HTTP endpoints

All under `/api/tasks/*`, gated by `HARNESS_TOKEN`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tasks/board` | Active 5 buckets (`plan` / `execute` / `audit_syntax` / `audit_semantics` / `ship`), priority-sorted then by `created_at`. Each card includes its active role-assignment list. **No archive.** |
| GET | `/api/tasks/archive` | Paginated archive view. Query params: `limit` (default 50, max 200), `offset`, `q` (text search title + description), `include_cancelled` (default false). |
| GET | `/api/tasks/{id}/assignments` | Full role-assignment history for one task (every row, not just active). Used by the card-expansion view to render the audit-loop history. |
| POST | `/api/tasks/{id}/stage` | Body `{stage, note?, force?: bool}`. Human override. `force=true` bypasses the role-completion gate. |
| POST | `/api/tasks/{id}/complexity` | Body `{complexity}`. |
| POST | `/api/tasks/{id}/blocked` | Body `{blocked: bool, reason?}`. |
| POST | `/api/tasks/{id}/spec` | Body `{body: <markdown>}`. Same effect as `coord_write_task_spec`. |
| POST | `/api/tasks/{id}/assign` | Body `{role, to}`. Human-side equivalent of the Coach assignment tools. |

---

## 8 · Events

All published via the existing `EventBus` ([server/events.py](server/events.py)). New types:

| Type | Payload |
|---|---|
| `task_stage_changed` | `{ts, agent_id, type, task_id, from, to, reason: 'commit_pushed'|'audit_pass'|'audit_fail'|'shipped'|'manual', note?, owner}` |
| `task_complexity_set` | `{ts, agent_id, type, task_id, complexity, to: owner}` |
| `task_blocked_changed` | `{ts, agent_id, type, task_id, blocked, reason?, to: owner}` |
| `task_spec_written` | `{ts, agent_id, type, task_id, spec_path, to: owner}` |
| `task_role_assigned` | `{ts, agent_id, type, task_id, role, eligible_owners, owner?, to: owner}` |
| `task_role_claimed` | `{ts, agent_id, type, task_id, role, owner, to: owner}` |
| `task_claimed` | `{ts, agent_id, type, task_id, owner, to: owner}` — fired by `coord_claim_task` when a Player pulls a task into `execute`. Distinct from `task_role_claimed` (which fires for any role pool); `task_claimed` is the executor-specific signal kept for back-compat with consumers that pre-date the role-assignments table. |
| `task_role_completed` | `{ts, agent_id, type, task_id, role, owner, completion_artifact?, to: owner}` |
| `audit_report_submitted` | `{ts, agent_id, type, task_id, kind, verdict, report_path, round, auditor_id, to: <executor>}` |
| `audit_self_review_warning` | `{ts, agent_id, type, task_id, kind, auditor_id, executor_id}` |
| `audit_assignment_needed` | `{ts, agent_id: 'system', type, task_id, kind|role, to: 'coach'}` |
| `task_shipped` | `{ts, agent_id, type, task_id, shipper_id, note?, to: <executor>}` |
| `idle_player_woken` | `{ts, agent_id: <slot>, type, reason, task_id?}` |

The legacy `task_updated` event keeps firing for back-compat. `/api/events` SQL filter extended to fan-out the new types via the `payload_to` / `payload_owner` indexed branches.

---

## 9 · Auto-advance subscriber

[server/kanban.py](server/kanban.py). Started in `lifespan` next to `start_audit_watcher` / `start_telegram_bridge`. Subscribes synchronously **before** scheduling its consumer task to avoid losing events fired during the create_task race window — same pattern as the Compass audit watcher.

### 9.1 Trigger table

| Event | Action |
|---|---|
| `commit_pushed` with `task_id`, complexity=standard, stage=execute | Advance to `audit_syntax`. Wake assigned syntax auditor; emit `audit_assignment_needed` if none assigned. |
| `commit_pushed` with `task_id`, complexity=simple, stage=execute | Jump straight to `archive`. Set `archived_at = now`. Clear executor's `current_task_id`. |
| `audit_report_submitted{kind=syntax, verdict=pass}` | Advance to `audit_semantics`. |
| `audit_report_submitted{kind=syntax, verdict=fail}` | Revert to `execute`. Clear `started_at`. Update `latest_audit_*` fields. Re-wake executor with spec + latest report. |
| `audit_report_submitted{kind=semantics, verdict=pass}` | Advance to `ship`. |
| `audit_report_submitted{kind=semantics, verdict=fail}` | Revert to `execute` (same shape as syntax fail). |
| `task_shipped` | Advance `ship → archive`. |
| `compass_audit_logged` | Update `compass_audit_*` columns on the most-recently committed task with the matching sha. **No stage change.** |

### 9.2 Feature flag

`HARNESS_KANBAN_AUTO_ADVANCE` (default true). Set false to make the board purely observational.

### 9.3 Failure isolation

Per-event `try/except` so a single bad row doesn't kill the loop. Unrecognized event shapes log + skip.

### 9.4 Compass independence

The subscriber treats `compass_audit_logged` as informational. It writes `compass_audit_*` columns and triggers a `task_updated` for the live UI but never moves the task between stages. The Player auditor is the gate.

---

## 10 · Idle-Player polling

[server/idle_poller.py](server/idle_poller.py). Background task in `lifespan`.

### 10.1 Loop cadence + grace period

| Env var | Default | Meaning |
|---|---|---|
| `HARNESS_IDLE_POLL_ENABLED` | `true` | Master switch. |
| `HARNESS_IDLE_POLL_INTERVAL_SECONDS` | `300` | Sweep cadence. |
| `HARNESS_IDLE_POLL_GRACE_SECONDS` | `60` | Don't pick up a pool task whose `assigned_at` is more recent than this — gives the initial auto-wake time to land. |
| `HARNESS_IDLE_POLL_DEBOUNCE_SECONDS` | `1800` | Per-Player debounce. After waking p3, don't wake them again for 30 min. |

### 10.2 Per-Player debounce

Tracked in `agents.last_idle_wake_at` (added by the kanban migration). NULL initially. Updated each time the poller fires for a slot.

### 10.3 Skip rules

For each (Player slot, sweep tick), skip if any of:
- `agents.locked = 1`
- `agents.current_task_id IS NOT NULL`
- `agents.status = 'working'`
- Player is over their daily cost cap
- `last_idle_wake_at` within debounce window

### 10.4 Telemetry

Emits `idle_player_woken` events with `reason ∈ {'pool_task_available', 'pending_role_assignment'}`.

---

## 11 · UI surface

### 11.1 LeftRail icon

A CSS-drawn three-column-of-rectangles glyph (`.kanban-icon` in [server/static/style.css](server/static/style.css)). Toggle behavior + persistence via `harness_layout_v1` localStorage key — same shape as `__files` / `__compass`.

### 11.2 Four active columns + Archive drawer

Columns: **Plan / Execute / Audit / Ship**. Audit fuses `audit_syntax` + `audit_semantics` into one column; the per-card `kbn-stage-label` (e.g. `AUDIT-SYN`, `AUDIT-SEM`) preserves the sub-stage signal.

Archive is **not** a column. It opens as an inline drawer above the columns via the toolbar's `Archive ▾` button. Drawer features: search, pagination (50 per page), `show cancelled` toggle.

### 11.3 Card content

- **Title** (1–2 lines, truncated).
- **Stage label** (`PLAN` / `EXECUTE` / `AUDIT-SYN` / `AUDIT-SEM` / `SHIP`) — kanban column conveys it visually, but the explicit per-card text is rendered so the stage stays unambiguous when the card is dragged or quoted out of context.
- **Assignee avatar** for the Player driving the work at the current stage (plan → planner if delegated else "coach" chip; execute → executor; audit_* → matching auditor; ship → shipper). Avatar variants:
  - **Hollow ring** — assigned, not yet started (`started_at IS NULL`).
  - **Filled ring** — started.
  - **Filled + pulse glow** — started AND the agent's `agents.status = 'working'` right now (live signal, not a stored flag).
  - **`pool: N` chip** — stage's role is posted to a pool of N Players and not yet claimed.
  - **Italic `unassigned` chip** — stage's role is needed but no row exists yet. Clickable; opens a quick-assign popover that POSTs to `/api/tasks/{id}/assign`.
  - **`coach` chip** — plan-stage tasks Coach is self-planning (no `planner` role assignment exists).

  Clicking a populated avatar opens that Player's pane (existing `openSlots` + dispatch path).
- **Status flag** badge encoding orthogonal state:
  - `URGENT` (red) — `priority = 'urgent'`.
  - `BLOCKED` (red) — `tasks.blocked = 1`.
  - `STALE` (amber) — active assignment hasn't progressed in `HARNESS_KANBAN_STALE_HOURS` hours (config; default 48).
  - Nothing when the card is progressing normally.
- **Markdown links** — single-click `data-harness-path` opens in the Files pane:
  - `[spec]` — `tasks.spec_path` (always shown when set).
  - `[audit (kind, round N)]` — `tasks.latest_audit_report_path` (only when `latest_audit_*` is populated; the kind + round are part of the label so the human knows which audit they're opening).
  - `[compass]` — `tasks.compass_audit_report_path` (informational; only shown in audit_semantics or after; muted color to signal "secondary").
- **Complexity chip** (`SIMPLE` top-right) for simple-mode tasks; nothing for standard.
- **Drift banner** (red bar) when the card is in `execute` post-audit-fail (driven by `latest_audit_verdict = 'fail'`).

Clicking the card body (not a link) expands it inline. Expansion shows: full description, recent `task_stage_changed` events, and the full audit-round history rendered from `/api/tasks/{id}/assignments` (round 1 syntax fail → round 1 syntax pass → round 1 semantics fail → round 2 semantics pass → ship, etc.) with each round linking to its own `audit_<round>_<kind>.md`.

Older audit reports (rounds before the current latest) live on disk + kDrive in the task's `audits/` folder. The card surfaces only the latest; the human browses older rounds via the expansion panel or directly in the Files pane.

### 11.4 Composer modal

`+ New task` button opens a modal with title / description / priority / complexity. POSTs to `/api/tasks`; new task lands in Plan column with `created_by='human'`.

### 11.5 EnvTasksSection removal

The old list-shaped EnvTasksSection is removed entirely. The kanban dashboard is the new primary surface for tasks; keeping a parallel list view in the EnvPane creates two places to read/write the same data with predictable drift.

Specifically dropped:
- The status-grouped task list (active / blocked / done filters).
- The cancel-button per task.
- The hierarchical depth indentation.
- The `harness_task_filter_v1` localStorage key.
- The composer form (moved to the kanban toolbar's `+ New task` modal).

Specifically retained in the EnvPane (untouched by this work):
- Attention strip (`EnvAttentionSection`).
- Coach-set per-Player overrides (`EnvOverridesSection`).
- kDrive status (`EnvKDriveStatusSection`).
- Cost meter (`EnvCostSection`).
- Project objectives (`EnvObjectivesSection`).
- Coach todos (`EnvCoachTodosSection`).
- Inbox (`EnvInboxSection`).
- Memory commons (`EnvMemorySection`).
- Decisions (`EnvDecisionsSection`).
- File-write proposals (`EnvFileWriteProposalsSection`).
- Timeline (`EnvTimelineSection`).

A small `kbn-env-hint` section ("Tasks live in the Kanban pane (open from the rail)") slots in where `EnvTasksSection` used to live. It hides itself when the kanban is already open so it never becomes visual noise.

---

## 12 · Schema

### 12.1 task_role_assignments table

```sql
CREATE TABLE task_role_assignments (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id         TEXT NOT NULL REFERENCES tasks(id),
  role            TEXT NOT NULL CHECK(role IN
                    ('planner','executor','auditor_syntax','auditor_semantics','shipper')),
  eligible_owners TEXT NOT NULL DEFAULT '[]',
  owner           TEXT REFERENCES agents(id),
  assigned_at     TEXT NOT NULL,
  claimed_at      TEXT,
  started_at      TEXT,
  completed_at    TEXT,
  report_path     TEXT,
  verdict         TEXT CHECK(verdict IN ('pass','fail')),
  superseded_by   INTEGER REFERENCES task_role_assignments(id)
);

CREATE INDEX idx_role_assignments_task ON task_role_assignments(task_id);
CREATE INDEX idx_role_assignments_owner ON task_role_assignments(owner);
CREATE INDEX idx_role_assignments_role ON task_role_assignments(task_id, role);
```

### 12.2 New tasks columns

| Column | Type | Default | Meaning |
|---|---|---|---|
| `complexity` | TEXT | `'standard'` | `'simple' | 'standard'` |
| `blocked` | INTEGER | `0` | Orthogonal flag |
| `blocked_reason` | TEXT | NULL | Note when blocked=1 |
| `cancelled_at` | TEXT | NULL | Set on cancellation path |
| `archived_at` | TEXT | NULL | Set on any archive transition (delivery OR cancel) |
| `started_at` | TEXT | NULL | First time the executor picked the task up |
| `spec_path` | TEXT | NULL | Relative path to spec.md |
| `spec_written_at` | TEXT | NULL | Timestamp of last spec write |
| `latest_audit_report_path` | TEXT | NULL | Card surface — most recent Player audit |
| `latest_audit_kind` | TEXT | NULL | `'syntax' | 'semantics'` |
| `latest_audit_verdict` | TEXT | NULL | `'pass' | 'fail'` |
| `compass_audit_report_path` | TEXT | NULL | Informational Compass report pointer |
| `compass_audit_verdict` | TEXT | NULL | Informational Compass verdict |

### 12.3 Indexes

`idx_tasks_status` (existing, on the new enum), `idx_tasks_complexity` (added by migration). Created in `_ensure_tasks_kanban_indexes` after the migration runs (CHECK constraints can't be ALTERed, so the index lives outside the table-rebuild block).

### 12.4 Status enum migration

Old → new:

| Old | New | Notes |
|---|---|---|
| `open` | `plan` | — |
| `claimed` | `execute` | `started_at = NULL` |
| `in_progress` | `execute` | `started_at = claimed_at` (best-effort backfill) |
| `blocked` | `execute` | `blocked = 1`, `blocked_reason` from the prior note |
| `done` | `archive` | `completed_at` already set; `archived_at = completed_at` |
| `cancelled` | `archive` | `cancelled_at = completed_at`, `archived_at = completed_at` |

Idempotent via `team_config['tasks_kanban_v1_migrated']` marker. Runs in `_rebuild_tasks_if_kanban_outdated` ([server/db.py](server/db.py)) on boot.

`crash_recover()` updated: zombie-reset condition is now `tasks.status = 'execute' AND owner_status = 'zombie'` (was `in_progress`). Owner is preserved.

---

## 13 · CLAUDE.md kanban block

[server/tasks_claude_md.py](server/tasks_claude_md.py) — marker-delimited static paragraph injected into every project's `CLAUDE.md` on harness boot. Mirrors the Compass `claude_md.py` pipeline.

### 13.1 The paragraph

Marker convention: `<!-- KANBAN-LIFECYCLE-START -->` / `<!-- KANBAN-LIFECYCLE-END -->`. Verbatim body that lands in every project's CLAUDE.md:

```markdown
<!-- KANBAN-LIFECYCLE-START -->
## Task lifecycle (kanban)

Tasks flow through stages: **plan → execute → audit_syntax → audit_semantics → ship → archive**.
Standard tasks traverse the full pipeline. Simple tasks (typos, log tweaks, one-line fixes)
jump plan → execute → archive directly.

Each task produces durable markdown artifacts under
`/data/projects/<project_id>/working/tasks/<task_id>/`:
- `spec.md` — the plan, written before execute (required for standard tasks; optional for simple)
- `audits/audit_<round>_<kind>.md` — Player auditor reports (kind = syntax | semantics; one file per round)

### Strict role boundaries

- **Coach** plans (or delegates planning) and assigns Players to roles. Coach does NOT
  execute, audit, or merge. Coach's task tools: `coord_write_task_spec`,
  `coord_assign_planner`, `coord_assign_task` (executor; accepts a single Player slot or
  a list-as-pool), `coord_assign_auditor` (kind=syntax|semantics), `coord_assign_shipper`,
  `coord_set_task_complexity`, `coord_advance_task_stage`, `coord_set_task_blocked`.
- **Players** execute, audit, and ship. The relevant tools:
  - `coord_my_assignments` — call this any time you're not sure what to do; returns your
    full plate (active executor task / pending audits / pending ship / eligible pools).
  - `coord_claim_task(task_id)` — pull yourself into a posted pool task. First-claim wins.
  - `coord_commit_push(task_id, message)` — pass `task_id` so the kanban auto-advances.
  - `coord_submit_audit_report(task_id, kind, body, verdict)` — auditors submit pass/fail.
  - `coord_mark_shipped(task_id)` — shipper calls after the merge lands.

### Audit verdict routing

Pass → next stage. Fail → reverts to execute; the spec + latest audit report attach to
the task and the executor is auto-woken with both. Compass auto-audit fires informationally
on every commit; the assigned Player auditor is the gate, not Compass.

### Simple-task discipline

When Coach marks a task simple, the executor SELF-AUDITS: run the relevant tests, sanity-check
the change, then `coord_commit_push`. The board archives directly on commit; there is no
separate audit pass. Coach should mark a task simple only when the change is small and
well-bounded enough that self-audit is sufficient.
<!-- KANBAN-LIFECYCLE-END -->
```

### 13.2 inject_kanban_block flow

`render_kanban_block()` returns the wrapped paragraph. `inject_kanban_block(project_id)` reads the project's CLAUDE.md, replaces the block between markers if present, otherwise appends it; writes back atomically + kDrive mirror. Idempotent (no-op when content matches). `inject_into_all_projects()` walks every project on boot.

The block is also part of the harness-level **starter CLAUDE.md** used when a fresh project folder is provisioned. New projects pick it up via the same `render_kanban_block()` call so a freshly-created project has the lifecycle paragraph in its CLAUDE.md from turn 1 — no waiting for the next boot's `inject_into_all_projects` sweep to land it.

### 13.3 Why static + idempotent

The content is the same in every project — no per-project tailoring. Updates regenerate via `render_kanban_block()` and re-inject on next boot.

### 13.4 Coach lifecycle-policy prompt block

In addition to the static CLAUDE.md block (which Coach reads alongside Players), Coach's per-turn system prompt gains a dynamic `## Task lifecycle policy` section assembled by `_build_coach_coordination_block` in [server/agents.py](server/agents.py). It's inserted after the open-tasks rollup and before the inbox / decisions / wiki summaries. Verbatim text:

```
You coordinate; you do not execute. Your direct work is PLANNING (writing the
spec) and DELEGATION (assigning Players to roles). Players do execution, audit,
and ship.

Tasks flow: plan → execute → audit_syntax → audit_semantics → ship → archive.
Standard complexity: full pipeline. Simple complexity: plan → execute → archive
(no spec required, no audit or ship).

PLAN: write the spec yourself with coord_write_task_spec, OR delegate by calling
coord_assign_planner. Both flows are valid — use the planner role when a Player
has more domain context than you, or when you want to keep your turn budget for
coordination. Standard tasks cannot move to execute without a spec.

SIMPLE-TASK DISCIPLINE: when you mark a task simple (skipping the formal audit
stages), the executor SELF-AUDITS as part of execute. Tell them so in your
assignment message: "self-audit your fix (run the relevant tests / sanity-check
the change) before coord_commit_push, since this is simple-mode and the board
archives directly on commit". Mark a task simple only when you trust the
executor's self-audit on a small, well-bounded change. If in doubt, leave it
standard and assign auditors.

EXECUTE: assign with coord_assign_task. `to` accepts a single Player slot for
hard-assign, OR a list of slots to post to a pool — the first eligible Player
to call coord_claim_task wins. Use the pool form when several Players could do
the work and you want first-free-claims.

AUDIT: assign auditors with coord_assign_auditor (kind='syntax' or 'semantics').
Auditors are Players too — same single-or-list shape. The tool warns if you
assign the same Player who executed (weak self-review); decide whether that's
acceptable for the task at hand. Auditors call coord_submit_audit_report with
verdict='pass' or 'fail'. Pass → next stage. Fail → task reverts to execute,
the executor is auto-woken with the spec + the latest audit report attached.

SHIP: assign a shipper with coord_assign_shipper. They merge, then call
coord_mark_shipped → task archives.

Compass auto-audit fires on every commit_pushed — it's INFORMATIONAL, not the
gate. The semantic auditor can read its report (linked from the card) as one
input but their own verdict is what advances the task.

You can force any transition with coord_advance_task_stage when role assignments
stall. Mark a task simple with coord_set_task_complexity when it's a typo, log
tweak, or genuinely minor bug fix.

Always pass task_id to Players when assigning and remind them to include it in
coord_commit_push / coord_submit_audit_report / coord_mark_shipped so the board
auto-advances.

Players can call coord_my_assignments at any time to see their full plate —
active executor task, pending audits, pending ship, eligible pools. Idle
Players are auto-polled every 5 minutes and woken if they could be claiming
pool work — so don't worry about Players sitting on un-claimed pool tasks
indefinitely; the poller will eventually pick it up. But hard-assigning is
still faster when you have a specific Player in mind.
```

Coach's tool catalogue is also extended in this order so the prompt's tool list lines up with the policy block above:

1. **Planning** — `coord_write_task_spec`, `coord_assign_planner`.
2. **Execution** — `coord_assign_task` (modified — accepts a single slot or a comma-list pool).
3. **Audit** — `coord_assign_auditor` (kind ∈ {syntax, semantics}).
4. **Ship** — `coord_assign_shipper`.
5. **Meta** — `coord_set_task_complexity`, `coord_advance_task_stage`, `coord_set_task_blocked`.

### 13.5 Player system prompt addition

Players' system prompt gets a short paragraph (kept tight to keep token cost low — the static CLAUDE.md block already carries the heavy content):

```
Tasks have stages and roles. You may have an executor, auditor, planner, or
shipper assignment.

Call coord_my_assignments at the start of any turn where you're not sure what to
do — it returns your full plate.

Pass task_id to coord_commit_push / coord_submit_audit_report / coord_mark_shipped
so the kanban auto-advances.
```

---

## 14 · Telegram escalation hooks

[server/telegram_escalation.py](server/telegram_escalation.py) gains formatters + key-extractors for three new event types:

| Event | Formatter |
|---|---|
| `audit_report_submitted{verdict='fail'}` | `"Audit fail: t-... '<title>' — <kind> auditor <slot> returned fail (round N). Reverted to execute. Open the kanban to read the report."` |
| `audit_assignment_needed` | `"Assignment needed: t-... '<title>' is in <stage> with no <role> assigned. Open the kanban to assign one."` |
| `audit_self_review_warning` | `"Self-review: t-... '<title>' — Coach assigned <slot> as <kind> auditor; they're also the executor. Acceptable for small teams; flag for review on big tasks."` |

Same web-active vs grace timing as the existing pending-question / pending-plan escalations. The kanban itself shows everything inline regardless.

---

## 15 · Compass cross-reference

### 15.1 Why Compass is informational

Two separate concerns: the Player auditor checks the **artifact against the spec** (does this code do what the task said?). Compass checks the **artifact against the lattice** (does this drift from the project's stated direction?). Both signals are useful but they're not the same thing — the task can be specced wrong, in which case a Compass drift verdict matters more than a syntax-pass; or the spec can be aligned but the implementation buggy, in which case the auditor is the right gate.

Putting Compass on the gate would conflate the two. The kanban v1 keeps them separate: the Player auditor gates; Compass surfaces a pip on the card so the semantic auditor can read it as one input.

### 15.2 How the semantic auditor uses Compass

The auto-wake prompt for the semantic auditor includes the spec, the commit, and (when set) the path to `compass_audit_report_path`. The auditor reads it, factors it into their own verdict, and submits.

### 15.3 Drift escalation path

`compass_audit_logged{verdict='confident_drift'}` still queues a `human_attention` event via the existing escalation path ([compass-specs.md §5.4](compass-specs.md)), which the bridge forwards to Telegram. Independent of the kanban.

---

## 16 · Operational notes

### 16.1 Migration order

1. Boot harness — `_rebuild_tasks_if_kanban_outdated` runs once.
2. Marker key `tasks_kanban_v1_migrated` set.
3. Subsequent boots short-circuit.

The migration is safe to re-run if the marker is wiped (e.g. for a partial recovery) — it's idempotent on the data side too: the SELECT-old-status-INSERT-new-status SQL only operates on rows whose status matches the legacy alphabet.

### 16.2 Rollback strategy

Set `HARNESS_KANBAN_AUTO_ADVANCE=false` to freeze the board state machine without rolling back the schema (cards still render; events still fire; transitions only happen via `coord_advance_task_stage` or `POST /api/tasks/{id}/stage`).

A full schema rollback is **not supported** in v1 — the migration drops the legacy enum members and reorganizes data into the new shape. If you need to revert, restore the most recent kDrive snapshot.

### 16.3 Cost considerations

Every commit on a standard-complexity task triggers up to 2× Player audits (syntax then semantics) plus 1× Compass auto-audit. The audit prompts are bounded at ~16 KB per artifact body (see [compass-specs.md §5.5.2](compass-specs.md)) so cost stays predictable, but a project with 30 commits/day and a `latest_sonnet` audit budget will see meaningfully higher daily spend than the pre-kanban harness.

Mitigations: mark genuinely-small tasks `simple` (skips both Player audits + ship); set `HARNESS_TEAM_DAILY_CAP` to a hard ceiling; disable `HARNESS_COMPASS_AUTO_AUDIT` on cost-constrained deploys.

### 16.4 What needs verification

End-to-end on a deployed Zeabur instance:

1. Standard task pipeline with hard-assigned roles (plan → execute → audit_syntax → audit_semantics → ship → archive).
2. Standard task with multi-Player executor pool — race-safe `coord_claim_task`.
3. Simple task — auto-wake prompt verbatim contains the self-audit instruction; `commit_pushed` jumps straight to archive.
4. Idle-Player polling — pool task posted while all eligible Players are over-cap; caps reset; poller wakes one within the next sweep.
5. Audit fail loop — round 1 fail, round 2 pass; card shows only latest; `audits/` folder on disk has both rounds.
6. Telegram escalation: `audit_fail` ping arrives on the phone after the configured grace period.
7. CLAUDE.md kanban block injected on boot; surviving a manual edit + restart (idempotent re-injection).

---

## Cross-references

- [TOT-specs.md](TOT-specs.md) — umbrella spec.
- [compass-specs.md](compass-specs.md) — audit-report sibling, drift escalation, cost discipline patterns reused here.
- [recurrence-specs.md](recurrence-specs.md) — background-loop infrastructure parallel; the kanban subscriber + idle poller follow the same lifespan-task pattern.

# Kanban v2 — Shape-(2) routing through Coach

> **Status:** DRAFT (2026-05-07). Canonical from this point forward.
> v1 is archived at [kanban-specs-v1-archived.md](kanban-specs-v1-archived.md)
> for historical reference only. v1 is **no longer authoritative**.
>
> **Cutover model:** v2 is finalized as a complete spec FIRST. THEN code
> is updated to reflect v2 in one pass. The current container still runs
> v1 behavior until the implementation PR ships; this document describes
> the target state, not what is deployed today.

> **Subordinate to [TOT-specs.md](TOT-specs.md).** When this doc and
> TOT-specs disagree, TOT-specs wins. This file goes deeper on the
> kanban subsystem (stages, roles, artifacts, the event log, the
> Coach review gate, the pattern-detection layer, the idle-Player
> poller) but cannot redefine fields, endpoints, events, or invariants
> that TOT-specs declares.

**Target:** TeamOfTen multi-agent harness (Python, Claude Agent SDK, kDrive-backed shared state, single-VPS)
**Version:** 2.0 (draft)

---

## 1 · Why v2 — design principles

v1's kanban was an autonomous router. Stage transitions auto-fired on commit / audit / spec; auto-wakes pulled the next assignee; audit FAILs reverted to execute without Coach in the path. In production this produced stale wakes (Player wakes on a task since reassigned), silent audit reverts (the executor loops with the auditor without Coach noticing), missed deviations (scope drift only surfaced at audit time, not at push time), and pool-claim races where the wrong Player won an executor seat. The 2026-05-06/07 session made all four failure modes visible at once.

v2 reshapes the system around six principles:

1. **Coach is the team's continuous reasoning layer.** Every team event flows into Coach's context. The kanban records and surfaces; it does not route.
2. **Players are specialized executors invoked by Coach explicitly.** No auto-wake of Players on stage transitions: the kanban engine never picks the next assignee. Coach composes the wake context after seeing the previous Player's output. *(Player→Coach completion calls are a different channel — they DO wake Coach in real time; see principle 3.)*
3. **Completion calls are real-time messages to Coach.** Each Player completion tool (`coord_commit_push`, `coord_write_task_spec`, `coord_submit_audit_report`, `coord_role_complete`) is the Player's act of replying to Coach. The harness wakes Coach immediately on every such call (`bypass_debounce=True`, `wake_source='kanban_completion'`) and fans the event into Coach's pane in real time. Coach and Players hold a fluid, real-time conversation; the tool calls record the artifact + structure for the kanban while the live wake keeps the dialogue moving. The "no auto-wake" rule (principle 2) constrains the *kanban engine* — it does NOT silence Player→Coach replies.
4. **Pattern-noticing is a first-class deliverable.** Audit-fail trajectories, repeat issues, and Coach-flagged deviations are computed by the harness and surfaced explicitly so Coach can act on patterns proactively rather than after manual observation.
5. **Default trajectories favor rigor.** Contract-first, plan-mode-when-useful, audits-before-merge are the default for non-trivial work. Trivial work doesn't get a free pass — Coach still reviews; the only optimization is that Coach's review can be a one-line "advance" rather than a paragraph of analysis.
6. **Human escalation is the safety net, not the routing path.** The human is pinged only on decisions Coach genuinely cannot make alone. Coach absorbs more events but also more decisions; total Coach turns per day decrease (fewer tasks shipped, more reasoning per task) but each turn is heavier.

---

## 2 · Shape (2) at a glance

A v2 task flows through five phases of attention:

```
Player produces an artifact (commit / spec / audit / message)
  → Player calls completion tool with `message_to_coach`
  → harness writes a structured row to the per-project event log (§9)
  → harness wakes Coach immediately (bypass_debounce=True) AND fans
    the event into Coach's pane — the call is Coach's notification, not
    a delayed log read
  → Coach reads, decides what to do next (advance / reroute / clarify / archive)
  → Coach calls `coord_approve_stage` with the next assignee + a note
  → next Player wakes with Coach-composed context
```

Contrast with v1: every step between "Player produces" and "next Player wakes" was automatic; Coach saw the trace afterwards (or not at all, on `aligned` Compass verdicts). In v2 each *kanban transition* is Coach-gated, but the Player→Coach completion call still wakes Coach in real time so the conversation flows. The kanban itself is now a recording mechanism plus a surface that highlights what Coach should look at next. There is **no auto-advance escape hatch** — every transition is Coach-gated — but completion calls are not transitions; they're messages.

---

## 3 · Stage state machine

The stages themselves are unchanged from v1: `plan` → `execute` → `audit_syntax` → `audit_semantics` → `ship` → `archive`. The product language remains **Formal Review** (audit_syntax) / **Semantic Review** (audit_semantics).

What changes is **who triggers transitions**:

- v1: every transition was triggered by an event (commit / audit_pass / spec_written / shipped / etc.) consumed by an auto-advance subscriber.
- v2: every transition requires an explicit Coach call to `coord_approve_stage(task_id, next_stage, assignee, note?)` (§7.1). No exceptions.

### 3.1 Valid transitions (universe)

```
plan            → {execute, archive}
execute         → {audit_syntax, audit_semantics, ship, archive}
audit_syntax    → {audit_semantics, ship, archive, execute}
audit_semantics → {ship, archive, execute}
ship            → {archive}
archive         → {}
```

The actual path a task walks is the trajectory Coach defines on `coord_create_task` (§4) — but the trajectory is **FYI only**. Coach can advance to any valid next stage at any point regardless of what the trajectory says. The trajectory documents the planned path; Coach's `coord_approve_stage` calls drive the actual path.

### 3.2 Audit FAIL no longer auto-reverts

v1: `audit_report_submitted{verdict='fail'}` reverted the task to `execute` automatically and re-woke the executor with the audit attached.
v2: the FAIL surfaces to Coach via the event log (§9). Coach reads the audit finding and decides:

- **Re-execute:** `coord_approve_stage(task_id, next_stage='execute', assignee=<executor>, note=<composed prompt>)` — the executor wakes with Coach's prompt, not the audit alone.
- **Override the audit (Coach disagrees):** `coord_approve_stage(task_id, next_stage='ship', assignee=<shipper>, note=...)` — Coach advances past the failed audit. The audit row stays as a record but doesn't gate.
- **Re-audit with different focus / different auditor:** `coord_approve_stage(task_id, next_stage='audit_syntax', assignee=<other-slot>, note=<reframed focus>)`.
- **Bump quality first:** call `coord_set_player_effort` / `coord_set_player_model` on the executor, then re-execute.
- **Abandon:** `coord_archive_task(task_id, summary)`.

The executor never wakes silently from a FAIL. Coach is the only mechanism that drives the next move.

---

## 4 · Backlog (pre-plan holding area)

The Backlog is a lightweight list of task *ideas* that precedes the kanban proper. Any agent or human can drop a title-only idea into the Backlog; Coach decides whether to promote it into an active task (with trajectory) or reject it (with reason).

### 4.0.1 Entry shape

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | Auto-assigned |
| `title` | TEXT | Free-form, long text OK |
| `proposed_by` | TEXT | Slot id (`coach`, `p1`…`p10`) or `'human'` |
| `proposed_at` | TEXT | ISO-8601 UTC |
| `status` | TEXT | `pending` \| `promoted` \| `rejected` |
| `reject_reason` | TEXT | Null unless rejected |
| `promoted_task_id` | TEXT | Set on promote; FK into `tasks.id` |

### 4.0.2 Propose paths

- **MCP `coord_propose_task(title)`** — available to Coach and all Players. Inserts a `pending` row; emits `backlog_task_proposed {id, title, proposed_by}`.
- **HTTP `POST /api/backlog {title}`** — human-facing. Same insert; `proposed_by='human'`; emits same event.
- **Slash `/newtask <title>`** — UI convenience: calls `POST /api/backlog` directly, no agent turn, no token burn. Renders a `.sys` confirmation row in the pane.

### 4.0.3 Triage: `coord_triage_backlog`

Coach-only MCP tool. Players who call it receive a "Coach-only" error.

```
coord_triage_backlog(id, action, trajectory?, modified_title?, reason?)
```

| `action` | Effect |
|---|---|
| `promote` | Atomically: UPDATE `backlog_tasks.status='promoted'`, INSERT into `tasks` (title = `modified_title ?? title`, `trajectory` required). Emits `backlog_task_promoted {backlog_id, task_id, title}`. |
| `reject` | UPDATE `backlog_tasks.status='rejected', reject_reason=reason`. Emits `backlog_task_rejected {id, title, reason}`. |

### 4.0.4 Rejection notification (human-proposed)

When `proposed_by='human'` and action is `reject`, the harness inserts a row into the `messages` table (`from_id='coach'`, `to_id='coach'`, `subject='Backlog rejected: <title>'`, `body=<reason>`) so the rejection surfaces in Coach's chat pane. No notification is sent for agent-proposed rejections — the `backlog_task_rejected` bus event is sufficient.

### 4.0.5 Coordination block rule

When the Backlog has pending items, `_build_coach_coordination_block` appends a `## Backlog` section listing the top 5 oldest `pending` entries (oldest-first). Format per line: `[{id}] "{title}" — {proposer}, {age}`. Section is omitted entirely when the Backlog is empty (zero token cost in the common case).

---

## 5 · Trajectory (FYI contract)

`tasks.trajectory` is a JSON list of `{stage, to, focus?}` objects (same shape as v1 §3.1). In v2 the trajectory is treated as a **plan Coach signals up front** — it documents what stages this task is expected to pass through and who could do each step. **It does not constrain Coach's actual transitions.** Coach can advance off-trajectory, insert stages mid-flight, drop expected stages, or reassign at any moment.

### 4.1 Validation rules

`_validate_trajectory()` enforces:

- Non-empty list.
- Each `stage` ∈ `{plan, execute, audit_syntax, audit_semantics, ship}` (no `archive` — implicit/terminal).
- No duplicate stages.
- Stages appear in canonical order.
- `execute` is mandatory.
- Each `to` resolves to a `list[str]` of valid Player slots — **advisory only**: Coach reads as a hint when picking an assignee in `coord_approve_stage`. There is no first-claim-wins; pools never auto-resolve from a Player claim. (The lone exception is stall-ladder rung 3 (§10.2), where the harness picks an alternative from `eligible_owners` because Coach has been silent on the rung-2 wake for ≥1h — see §21.4.)
- **`trajectory[0].to` MUST name exactly one Player** (single-element list, e.g. `['p3']`). The kanban is a log of work Coach has fired at a specific Player; tasks without an assignee aren't on the kanban yet, they're pre-task reasoning. Pool/empty first-stage `to` is rejected at validation. (v2.0.1 tightening, 2026-05-08 — earlier drafts allowed pool/empty first-stage and required a follow-up `coord_approve_stage(next_stage=<same>, assignee=...)` to plant; that two-step shape produced an "unassigned" orphan window on the board and was the wrong UX.)
- Subsequent entries' `to` may still be a single name, a list (pool), or empty — they remain FYI only. Coach picks each later stage's assignee at `coord_approve_stage` time, which already requires a single named slot.
- `focus` (optional, audit stages only) is a free-text string for what the auditor should check. **REQUIRED for `audit_semantics`** (rejected if missing or empty regardless of `to` shape — the focus is part of the trajectory contract, not the assignment). Optional for `audit_syntax` (defaults to "match the contract and verify internal soundness").

The v1.3.13 `coach_review` plan-stage flag is **removed**: v2 makes Coach review every stage transition by default, so the per-stage opt-in is redundant.

### 4.2 Default trajectories favor rigor (N5)

For non-trivial work, the **contract** is one of: truth specs (potentially with proposed additions/changes generated by Coach and validated by the human), Coach-authored plan, Player-authored plan, or no plan. Coach's prompt steers toward contract-first for `priority ∈ {urgent, high}` or `workflow = code` with non-trivial blast radius — including a `plan` stage and at least one audit stage. Coach decides per-task; the harness does not auto-promote a trajectory.

### 4.3 Mid-flight reroute

`coord_set_task_trajectory(task_id, trajectory)` (Coach-only) rewrites the trajectory at any point. Since the trajectory is FYI, the constraint set is loose:

- **Cannot remove stages already entered** (purely informational guard — the audit history those rows recorded should still be findable in the trajectory).
- **Can insert stages** between current and any future stage. Example: task in `execute`, Coach inserts `audit_semantics` after seeing the commit and deciding semantic review is now warranted.
- **Can drop unentered stages** the original trajectory had.
- **Can change `to` candidates** at any unentered stage.
- **Canonical-order constraint still applies post-reroute** — the validator rejects a reroute whose result violates `plan < execute < audit_syntax < audit_semantics < ship`. So Coach can insert `audit_syntax` between `execute` and `ship`, but not before `execute`.

Removed-stage role rows are deactivated; added-stage rows are inserted fresh. Stand-down wakes still fire for displaced assignees (carried from v1.3.6). Emits `task_trajectory_changed`.

Note: in v2, mid-stage insertion is a **normal Coach operation**, not an emergency override. Coach changes its mind based on what the previous stage produced; the trajectory adapts.

---

## 5 · Roles

Five roles, same as v1: `planner`, `executor`, `auditor_syntax`, `auditor_semantics`, `shipper`. Stored in `task_role_assignments`.

### 5.1 Strict separation

- **Coach** plans, delegates, reviews, advances, archives. Coach never executes, audits, or ships.
- **Players** execute, review, ship. Coach can also delegate planning to a Player — Coach assigns the planner role via `coord_approve_stage(stage='plan', assignee=<slot>)` when creating the task.

### 5.2 Pools are FYI only

`pool: [p7, p4]` in a trajectory entry is purely a hint for Coach about who could do the work. There is no claim path. The only way a Player gets assigned is via `coord_approve_stage(assignee=<slot>)`.

This eliminates the v1 stuck-pool failure mode (multiple Players in the pool, no one claims, idle poller picks the wrong one, task sits silent).

### 5.3 Reviewer-equals-executor warning

When Coach assigns the same Player to both executor and auditor on the same task, the harness emits an `audit_self_review_warning` event so the pattern is visible. Same as v1 §4.4. The warning is informational; Coach can choose this deliberately (small team, specialist scarcity).

### 5.4 Audit framing — what auditors actually check

Carried from v1 §4.6 verbatim: `auditor_syntax` is contract-bound (does the deliverable match what was asked + is it internally sound?); `auditor_semantics` is context-bound (does this make sense in the world this project lives in — Compass + truth/ + wiki/, NOT spec). Semantic audits require a stated `focus`. Syntax audits use a contract cascade (spec → title/description → executor wake → commit) so a missing spec doesn't stall the auditor.

### 5.5 Coherent assignment (Coach-side discipline)

Coach should pick assignees with continuity in mind. Player sessions stay live across review-wait windows (auto-compact handles bloat — see §10.3); accumulated context inside a Player's session is real value. When picking an executor for a follow-up task on the same area / module / domain, prefer the Player who already has context. Random rotation through the pool wastes the accumulated continuity. This is a Coach prompt directive (§14.1), not a harness rule.

---

## 6 · Artifacts

Carried from v1 §5: `spec.md`, `audits/audit_<round>_<kind>.md`, the parallel informational Compass audit report, knowledge files, commits, role-completion records. Path conventions, kDrive mirror, Files-pane integration: unchanged.

---

## 7 · MCP tools

All registered in [server/tools.py](server/tools.py)'s `_tools` map and `ALLOWED_COORD_TOOLS` set. Coach-only enforcement uses the existing `_require_coach` helper.

### 7.1 Coach-only — task creation, review gate, archive, plan-mode request

| Tool | Params | Purpose |
|---|---|---|
| `coord_create_task` | `title, description?, parent_id?, priority?, workflow?, tracking_reason?, trajectory?, success_criteria?` | Creates a top-level or child task. Sets `tasks.status` to the trajectory's first stage (or `plan` if no trajectory). **`trajectory[0].to` MUST name exactly one Player** (v2.0.1 tightening, 2026-05-08); pool/empty first-stage `to` is rejected at trajectory validation. The role row plants at create time with that slot as `owner` (Coach pre-picked via the trajectory itself — equivalent to `coord_approve_stage` for the first transition). Subsequent stages' `to` lists never auto-plant — they're FYI only until Coach approves into that stage via `coord_approve_stage`. Emits `task_created` + `task_role_assigned` + `task_stage_changed{from=null, to=<first_stage>}`. The first-stage role row is the only auto-plant; semantically this is "Coach-via-the-trajectory" picking, not the harness picking. **`success_criteria`** is the optional Coach-authored "definition of done" — see §17.3. |
| `coord_approve_stage` (NEW — N2) | `task_id, next_stage, assignee, note?, success_criteria?` | The single transition tool. Coach authorizes the next stage transition, names the assignee, and provides the wake prompt. `assignee` is required for any non-archive `next_stage`; pass a single slot. `note` is included verbatim in the assignee's wake prompt. Stamps `last_stage_change_at`, deactivates any prior active role row at the target stage (with `task_role_stand_down` wake to displaced Player if any), plants a fresh role row with the named assignee, emits `task_stage_changed` and `task_role_assigned`, fires the wake. The source-stage role row is normally already complete (Player called the appropriate completion tool, which is why Coach is now reviewing); when Coach overrides without source completion (e.g. abandoning a stuck executor), the source role row is also deactivated with stand-down. The same tool covers all transitions: plan→execute, execute→audit_syntax, audit_syntax→execute (re-do), audit_syntax→ship (Coach overrides a FAIL), execute→ship, ship→archive (delivery), and any-stage→archive (cancellation, with `assignee=null` since archive has no role). Replaces v1's `coord_advance_task_stage` and absorbs all `coord_assign_*` responsibility. **`success_criteria`** is consumed only at plan→execute (refines/replaces the value set at `coord_create_task`); ignored on other transitions. The stored value is echoed back in the tool result on advance to ship. See §17.3. |
| `coord_archive_task` (NEW — R3) | `task_id, summary` | Coach-only deliberate archive. Writes the user-facing summary, transitions to archive, marks any active role rows complete. The summary lands as a `.sys` row in Coach's pane and is forwarded to Telegram if the originating turn was user-triggered. Use this when a task wraps via natural completion (work delivered) or explicit cancellation (Coach decides not to ship); v1's auto-archive on trajectory-end is gone. |
| `coord_set_task_trajectory` | `task_id, trajectory` | Mid-flight reroute. v2 semantics per §4.3 — loose constraints, normal Coach operation. |
| `coord_request_plan_review` (NEW — N3) | `task_id, slot` | When Coach decides plan-mode is useful for a Player's turn, this wakes the Player with plan-mode enabled. The Player produces an ExitPlanMode artifact; on submission a `pending_plan{route='coach'}` event surfaces it to Coach for review before tools are touched. Coach approves (Player proceeds with the plan) or rewrites (Coach calls `coord_approve_stage` with a Coach-composed note instead). |
| `coord_set_task_blocked` | `task_id, blocked, reason?` | Toggles the orthogonal `tasks.blocked` flag. Unchanged from v1. |
| `coord_get_player_settings` / `coord_set_player_effort` / `coord_set_player_model` / `coord_set_player_runtime` | … | Coach quality-feedback knobs (carried from v1). v2 surfaces pattern-detection counters (§11) so Coach uses these proactively. |

### 7.2 Player-callable — completion (signal Coach)

**Players report to Coach, not to the kanban.** The kanban is Coach's log of what Players have told them. Each completion tool below does its specific real work AND, more importantly, **IS the Player's act of signalling Coach that the role is done**. Without the tool call, Coach has no idea the Player finished — the kanban can't record what it never heard about.

`message_to_coach` is the Player's primary signal: a one-line summary Coach reads first ("committed at sha X, tests pass", "lgtm — cleanly structured", "shipped to main", "spec is rough — wanted to ship something for Coach to react to"). It lands in the event log row's `payload_json` and is delivered to Coach two ways at once: (a) the event fans out into Coach's pane in real time so the conversation reads as a fluid dialogue; (b) the harness wakes Coach immediately with `bypass_debounce=True` and `wake_source='kanban_completion'`, passing `message_to_coach` plus minimal task context as the wake reason. Coach therefore acts on the reply without waiting for the next recurrence tick. The `## Recent events` rollup on the next tick is the catch-up surface for items Coach already saw live and for the rare wake-skipped case (cost cap, Coach already mid-turn — see §7.2.1).

### 7.2.1 Real-time wake semantics

For each of the four completion tools the harness performs, in order, after the per-tool real work commits:

1. Publish the bus event (`commit_pushed` / `task_spec_written` / `audit_report_submitted` / `task_role_completed`).
2. Append the row to `project_events` (§9).
3. Fan the event into Coach's pane in real time (the four completion event types are unconditionally cc'd to Coach by the WS-side dispatcher, in addition to the actor's pane and any pre-existing `to:` recipient).
4. Call `maybe_wake_agent("coach", reason=<formatted brief>, bypass_debounce=True, wake_source="kanban_completion")`. The wake reason is composed as: `"Player <slot> on task <id> (<title>) completed <role>: <message_to_coach>. <pointer to artifact when present>. Read the event log entry and decide whether to coord_approve_stage, request rework, archive, or leave the role open for further work."`

**Skipped wake cases** (the call still publishes + records; only the wake is skipped):

- `caller_is_coach` — when Coach calls a completion tool as an emergency override (e.g. `coord_write_task_spec(on_behalf_of=...)`). Coach is the actor; waking Coach inside Coach's own turn would loop.
- `_coach_is_working()` — Coach is mid-turn; `maybe_wake_agent` no-ops by design (`agent_id in _running_tasks`). The event is still in the pane and the rollup catches it on the next tick.
- Cost cap hit — `maybe_wake_agent` short-circuits silently to avoid a `cost_capped` storm. Same recovery path as above.

These are the rationale for keeping the `## Recent events` rollup in §13: it backs up the live signal when the live wake genuinely cannot fire. Steady-state, Coach acts on the reply within seconds.

**The recurring failure mode (recurring 6th instance on 2026-05-08):** Players write the deliverable to disk (spec.md, audit_<kind>.md, code commit, knowledge file) AND THEN STOP without calling the matching completion tool. The work is done; the team can't see it. The watchdog (§10.6) catches this with a `finished_not_reported` verdict and now wakes both Coach (override path) AND the assignee directly (self-correction nudge), but the proactive prevention is the framing: the wake bodies Players read on entry already say "your turn isn't done at the disk-write — it's done when Coach has received your signal."

| Tool | Params | Real work it does |
|---|---|---|
| `coord_commit_push` | `message, task_id?, push?, message_to_coach?` | Runs `git add -A && commit && push` in the Player's worktree. Auto-bind logic + misplaced-work detection (v1.3.7) carried forward. Marks the executor role row complete on success. Emits `commit_pushed{task_id, sha, message, message_to_coach, ...}`. |
| `coord_write_task_spec` | `task_id, body, on_behalf_of?, message_to_coach?` | Writes `spec.md` with frontmatter to the task's working dir; mirrors to kDrive. Marks the planner role row complete. `on_behalf_of` Coach override (v1.3.5) carries over for Codex-runtime Players who can't reach the tool. Emits `task_spec_written{task_id, spec_path, message_to_coach, on_behalf_of?, ...}`. |
| `coord_submit_audit_report` | `task_id, kind, body, verdict, on_behalf_of?, message_to_coach?` | Writes `audits/audit_<round>_<kind>.md` with frontmatter; records the verdict on the auditor role row; marks the role row complete. `on_behalf_of` carries over. Emits `audit_report_submitted{task_id, kind, verdict, report_path, message_to_coach, on_behalf_of?, ...}`. **Verdict='fail' does NOT auto-revert** (R2) — surfaces to Coach via event log; Coach decides. |
| `coord_role_complete` (NEW — collapses v1 `coord_complete_execution` + `coord_mark_shipped`) | `task_id, message_to_coach, artifact_path?` | Generic completion for roles whose real work happens via other tools (non-git executors who wrote a file via `Write` / `coord_save_output` / `coord_write_knowledge`; shippers who merged/published/sent via Bash / external CLIs). Verifies `artifact_path` exists on disk under the project root if passed (v1.3.14 gate); rejects with role row left open if missing. Marks the caller's current-stage role row complete. Emits `task_role_completed{task_id, role, artifact_path?, message_to_coach, ...}`. The role is inferred from the caller's current active role row at the task's current stage — no `role` parameter. Rejects with a clear error when the caller has no active role row on the task ("you have no active role on this task — Coach hasn't assigned you, or your role was already completed/superseded"). |
| `coord_my_assignments` | (none) | Returns current actionable work for the caller. Same shape as v1. |

### 7.3 Removed in v2

- `coord_claim_task` — Players don't claim. Coach assigns via `coord_approve_stage`. Removed.
- `coord_accept_role` — Same. Removed.
- `coord_advance_task_stage` — Replaced by `coord_approve_stage` (cleaner naming, v1.3.12 assignee-gate baked in by default).
- `coord_assign_planner` / `coord_assign_auditor` / `coord_assign_shipper` / `coord_assign_task` — All collapsed into `coord_approve_stage(assignee=...)`. Removed.
- `coord_complete_execution` — Replaced by `coord_role_complete`. Removed.
- `coord_mark_shipped` — Replaced by `coord_role_complete`. Removed.

### 7.4 Why this shape

v1 Players had five "I'm done" tools (`coord_commit_push`, `coord_complete_execution`, `coord_submit_audit_report`, `coord_mark_shipped`, `coord_write_task_spec`) plus two "I'm taking this" tools (`coord_claim_task`, `coord_accept_role`) — seven total. v2 Players have four "I'm done" tools and zero claim tools. Three completion tools survive because each does real artifact work (git push, spec write, audit write); the fourth (`coord_role_complete`) absorbs the two purely-ceremonial cases (non-git executor, shipper). Each of the four carries `message_to_coach` so the Player→Coach channel is uniform. Coach has exactly one transition tool (`coord_approve_stage`) replacing v1's `coord_advance_task_stage` + four `coord_assign_*` variants.

---

## 8 · HTTP endpoints

All under `/api/tasks` or `/api/tasks/*`, gated by `HARNESS_TOKEN`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tasks` | List view. Unchanged from v1. |
| POST | `/api/tasks` | Human task composer. Body `{title, description?, parent_id?, priority?, workflow?, tracking_reason?, trajectory?}`. Creates a top-level or child task in the trajectory's first stage (or `plan` if no trajectory) with `created_by='human'`. The composer omits `trajectory` for the default `[{"stage":"execute","to":[]}]`. |
| GET | `/api/tasks/board` | Active 5 buckets (`plan` / `execute` / `audit_syntax` / `audit_semantics` / `ship`), priority-sorted then by `created_at`. Unchanged. |
| GET | `/api/tasks/archive` | Paginated archive view. Unchanged. |
| GET | `/api/tasks/flow_health` | Stage counts + subscriber liveness. Unchanged. |
| GET | `/api/tasks/{id}/assignments` | Role-assignment history. Unchanged. |
| GET | `/api/projects/{id}/event_log` (NEW — N1, §9) | Paginated per-project event stream. Coach's tick consumes the unread tail; humans browse via the dashboard. Query params: `actor`, `type`, `task_id`, `since`, `limit` (default 50, max 200), `include_read` (default false). |
| POST | `/api/tasks/{id}/approve_stage` (NEW — backstop for `coord_approve_stage`) | Body `{next_stage, assignee, note?}`. Human-side equivalent for manual interventions. |
| POST | `/api/tasks/{id}/cancel` | Human cancellation. Equivalent to `coord_archive_task` from the human side; sets `cancelled_at` so the archive view distinguishes cancellation from delivery. |
| POST | `/api/tasks/{id}/trajectory` | Body `{trajectory}`. Carried forward. |
| POST | `/api/tasks/{id}/blocked` | Body `{blocked, reason?}`. Unchanged. |
| POST | `/api/tasks/{id}/spec` | Body `{body}`. Same effect as `coord_write_task_spec`; same no-auto-advance semantics. |

The v1 `POST /api/tasks/{id}/stage` and `POST /api/tasks/{id}/assign` endpoints are removed — both folded into `POST /api/tasks/{id}/approve_stage`.

---

## 9 · Per-project event log (N1)

The pivotal addition. Every action a Player or the harness produces becomes a structured row Coach reads on its next tick.

### 9.1 Schema — `project_events` table

```sql
CREATE TABLE project_events (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id      TEXT NOT NULL,
  ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  actor           TEXT NOT NULL,    -- 'p1'..'p10' / 'coach' / 'compass' / 'system' / 'human'
  type            TEXT NOT NULL,    -- see §9.2
  task_id         TEXT,             -- nullable (some events aren't task-scoped)
  payload_json    TEXT NOT NULL DEFAULT '{}',
  payload_pointer TEXT,             -- relative path to artifact OR short text body
  read_by_coach_at TEXT              -- NULL = unread; stamped after Coach's tick reads the row
);

CREATE INDEX idx_project_events_project_unread
  ON project_events(project_id, read_by_coach_at, ts);
CREATE INDEX idx_project_events_task ON project_events(task_id, ts);
CREATE INDEX idx_project_events_actor ON project_events(actor, ts);
```

### 9.2 Event types

Every type below produces exactly one row in `project_events` (in addition to the bus event that already fires for live subscribers — the existing `EventBus` machinery is preserved):

- `commit_pushed` — Player commit. `payload_pointer = <git sha>`. Includes `task_id`, `message`, `task_id_auto_bound: bool`, the diff'd file list, `message_to_coach?`.
- `task_spec_written` — Planner wrote `spec.md`. `payload_pointer = spec_path`. Includes `message_to_coach?`, `on_behalf_of?`.
- `task_role_completed` — Player called `coord_role_complete` (non-git executor / shipper / etc.). `payload_pointer = artifact_path?`. Includes `role`, `message_to_coach?`, `post_archive: bool` (true when the task was archived before the Player's completion landed — preserves the verification narrative; no stage advance possible).
- `audit_report_submitted` — Player submitted an audit. `payload_pointer = report_path`. Includes `verdict ∈ {'pass', 'fail'}`, `kind ∈ {'syntax', 'semantics'}`, `kind_round`, `message_to_coach?`, `on_behalf_of?`.
- `audit_fail_notification` — sibling event when audit verdict was fail. Carries `kind_round` + `escalate` (true when same-kind fails ≥ 2). Routes `to: 'coach'`.
- `task_stage_changed` — emitted by `coord_approve_stage`. Payload includes `from`, `to`, `assignee`, `note?`.
- `task_role_assigned` — emitted alongside `task_stage_changed` when `coord_approve_stage` plants the new role row.
- `task_role_stand_down` — fired when a role row is superseded by `coord_approve_stage` reassignment or trajectory reroute. Routes `to: 'coach'`. Per-Player wakes still fire to displaced slots (v1.3.6 carryover).
- `task_trajectory_changed` — Coach rewrote the trajectory.
- `task_blocked_changed` — block flag toggled.
- `task_archived` — emitted by `coord_archive_task`. Payload carries Coach's user-facing summary.
- `coord_send_message` — inter-agent messages. `payload_pointer = message body (truncated to 500 chars)`. Direction (sender → recipient) in `payload_json`.
- `coord_write_knowledge` — Player wrote a knowledge artifact. `payload_pointer = relative path`.
- `coord_write_decision` — Coach wrote a decision. `payload_pointer = relative path`.
- `compass_audit` (R7) — every Compass audit verdict, including `aligned`. `payload_json` carries `verdict`, `summary`, `contradicting_ids?` so Coach sees WHY the lattice signed off, not just THAT it did.
- `commit_without_task_id_warning` — fired when a Player commits via `coord_commit_push` without `task_id` AND has no active executor task to auto-bind. Routes `to: 'coach'`.
- `task_stage_stale` — per-task stall sweeper finding (§10.2 rung 1).
- `task_stall_persisting` — per-task stall ladder rung 2 (Coach call).
- `task_stall_auto_reassigned` / `task_stall_no_alternative` / `task_stall_auto_archived` — per-task stall ladder rungs 3–4.
- `kanban_board_stalled` (NEW — §10.4) — board-level safety ring: no `project_events` row written for the active project in N minutes. Routes `to: 'coach'` with `bypass_debounce=True`.
- `task_spec_unrecorded` / `task_audit_unrecorded` — reconciliation sweep findings (§10.5 — carried from v1.3.8).
- `watchdog_finding` — soft-stall watchdog finding (§10.6 — carried from v1.3.9).
- `pending_plan` — surfaced from `coord_request_plan_review`. Payload includes `route ∈ {'coach', 'human'}`.
- `human_attention` — escalations from `coord_request_human` and other paths.
- `auto_compact_triggered` / `session_compacted` — session lifecycle events affecting Players.
- `kanban_v2_cutover` (one-shot) — synthetic event fired once on first boot after the v0.3→v2 migration (§16.4). Routes `to: 'coach'` with a "walk the active board" wake body so Coach explicitly walks each in-flight task on the first v2 tick.

### 9.3 Read path — Coach's tick

`_build_coach_coordination_block` ([server/agents.py](server/agents.py)) executes:

```sql
SELECT id, ts, actor, type, task_id, payload_json, payload_pointer
FROM project_events
WHERE project_id = ? AND read_by_coach_at IS NULL
ORDER BY ts ASC
LIMIT N;
```

`N = HARNESS_PROJECT_EVENTS_PER_TICK` (default 50). Each row is rendered into a compact one-line summary in a new `## Recent events` section of Coach's system prompt. The prompt-builder collects the surfaced row IDs into `turn_ctx['surfaced_event_ids']`.

After Coach's turn completes successfully (ResultMessage received), the post-turn handler executes:

```sql
UPDATE project_events SET read_by_coach_at = ?
WHERE id IN (...turn_ctx['surfaced_event_ids']);
```

Older unread rows (the 51st onward) **stay unread** and roll forward to subsequent ticks. The prompt's `## Recent events` section ends with a footer:

> *+ N older unread events — query `/api/projects/{id}/event_log` to browse.*

…when the unread count exceeded the cap. After multiple ticks Coach catches up. This avoids both signal loss (unstamped older rows still surface eventually) and prompt bloat (each tick is bounded).

If Coach's turn fails (no ResultMessage), the IDs do NOT get stamped — the same rows retry on the next tick. No race with concurrent inbound events: rows arriving mid-turn have `id` larger than the surfaced set, so they show up on the next tick rather than getting silently stamped.

### 9.4 Retention

`HARNESS_PROJECT_EVENTS_RETENTION_DAYS` (default 30). The existing event-trim loop ([server/events.py](server/events.py) pattern) gains a sibling pass for `project_events`. The kDrive mirror writes a daily JSONL snapshot at `TOT/projects/<project_id>/events/<date>.jsonl` for durable history.

### 9.5 Dashboard surface

A new `__events` slot in the LeftRail (or a tab inside `__kanban`, TBD during finalization) lets the human browse the event stream. Filters: actor, type, task, time range, read/unread. Each row links to the underlying artifact via the existing `data-harness-path` mechanism. Human browsing does NOT stamp `read_by_coach_at` — that column is Coach-tick-specific.

---

## 10 · Idle polling, stall ladders, and the board safety ring

### 10.1 Per-Player idle poller (carried from v1)

[server/idle_poller.py](server/idle_poller.py). Background task in `lifespan`. **No idle-pool wake** in v2 — pools don't drive assignment. The poller still:

- Wakes idle Players with **explicit hard-assigned pending roles** whose stage just became active. (Coach assigned them via `coord_approve_stage`; the wake just got dropped or the Player was over-cap; this is the legitimate retry path.)
- Runs the per-task stall escalation ladder (§10.2).
- Runs the board safety ring (§10.4).
- Runs the reconciliation sweep (§10.5).
- Runs the soft-stall watchdog (§10.6).

Skip rules per slot per sweep tick:
- `agents.locked = 1`
- `agents.current_task_id IS NOT NULL`
- `agents.status ∈ {'working', 'waiting'}`
- Player is over their daily cost cap
- `last_idle_wake_at` within debounce window (`HARNESS_IDLE_POLL_DEBOUNCE_SECONDS`, default 1800s)

### 10.2 Per-task stall ladder (carried from v1.3.8)

Sibling pass on every task whose `last_stage_change_at` is older than rung-1 threshold. Four rungs, per-task idempotent via `tasks.stall_escalation_level`:

- **Rung 1 (default 30 min, `HARNESS_KANBAN_STALL_SECONDS`):** Wake the current-stage assignee with the stage-aware completion-tool nudge. Emit `task_stage_stale`.
- **Rung 2 (default 1 h, `HARNESS_KANBAN_ESCALATE_COACH_SECONDS`):** Wake Coach with explicit "intervene before auto-action" framing. Emit `task_stall_persisting`.
- **Rung 3 (default 2 h, `HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS`):** Auto-reassign to another eligible Player from the trajectory's `eligible_owners` (excluding the stuck owner + locked Players + busy Players). Updates the role row's `owner` + (for executor) `tasks.owner` + `agents.current_task_id`. Emits `task_stall_auto_reassigned`, fires `task_role_stand_down` for the displaced owner, and wakes the new owner with the canonical role-entry wake. If no alternative is reachable, fires `human_attention` + `task_stall_no_alternative`.
- **Rung 4 (default 4 h, `HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS`):** Auto-archive via direct UPDATE + `task_stage_changed{reason='auto_archive_stalled'}` + `task_stall_auto_archived` + `human_attention`. Resets `stall_escalation_level = 0`.

The level resets to 0 on every code path that clears `stale_alert_at` (any `coord_approve_stage`, `coord_set_task_blocked`, etc.).

**Note — rung 3 vs §5.2 (pools are FYI).** Rung 3 auto-picks an alternative from `eligible_owners` without Coach input. This is a deliberate exception: at this point Coach has been silent on the rung-2 wake for ≥1h, so the safety net steps in to keep the team from deadlocking. The rung-3 alternative-pick is the only place in v2 where the harness picks a Player without Coach.

### 10.3 Player session continuity

Players keep their `session_id` across review-wait windows. A Player whose `coord_commit_push` lands at minute 0 doesn't get their session cleared — they sit idle (status='idle', current_task_id still set) until Coach's review wake lands, then they resume on the same session. Auto-compact (§10.3.1) handles bloat; the accumulated context is real value.

#### 10.3.1 Auto-compact carryover

`HARNESS_AUTO_COMPACT_THRESHOLD` (default 0.5, lowered from 0.7 on 2026-05-09) — when the prior session's estimated tokens cross 50% of the model's window, the harness runs a compact turn before the user's prompt. Live observation showed the 0.7 trip-wire fired too late: turns in the 60–70% band were already degrading on quality before compaction kicked in. The lower threshold trades extra compact-call cost for more reliable per-turn quality.

### 10.4 Board safety ring (NEW — #6)

Sibling pass in the same tick loop. Detects **board-wide stagnation**: no `task_stage_changed` event written for the active project in `HARNESS_KANBAN_BOARD_SILENCE_SECONDS` (default 1800s = 30 min) AND there is at least one non-archive task on the board.

The trigger is `task_stage_changed` specifically, not any `project_events` row — Players actively producing artifacts (`commit_pushed`, `task_spec_written`, `audit_report_submitted`, `task_role_completed`) and Coach reading-without-advancing don't reset the timer. Only an actual stage transition (Coach calling `coord_approve_stage` or an auto-archive on rung 4) counts as "the board moved." This matches the failure mode the ring is designed to catch — Coach went to sleep on the entire kanban — without firing on Coach's deliberate review pauses.

On detection:

- Emit `kanban_board_stalled{project_id, last_stage_change_at, age_seconds, active_task_count, to: 'coach'}`.
- Wake Coach with `bypass_debounce=True` and the body: *"The kanban hasn't moved in N min — review the board. Active tasks: K (count). Query `/api/tasks/flow_health` for the state, then advance, reassign, or archive as needed."*
- Stamp `team_config['kanban_board_silence_alerted_at:<project_id>']` so the alert doesn't re-fire every tick — re-armed once a `task_stage_changed` event lands or a configurable cooldown elapses (`HARNESS_KANBAN_BOARD_SILENCE_REALERT_SECONDS`, default 3600s = 1h).
- Skip silently when the board has zero non-archive tasks (nothing to advance).

**Implementation fallback for the "last moved" reference.** The canonical signal is the latest `task_stage_changed` row in `project_events`. When that row doesn't exist (brand-new project, brownfield deploy where v2 has barely started writing the event log, fixture-driven tests that bypass the v2 tools), the ring falls back to the latest of `MAX(tasks.last_stage_change_at)` and `MAX(tasks.created_at)` over non-archive tasks for the project. A task created seconds ago counts as "the board just moved" — the freshness of the kanban itself is what we're measuring. This makes the ring robust against empty event logs without inventing a synthetic transition. Master kill-switch: `HARNESS_KANBAN_BOARD_SAFETY_ENABLED` (default true).

This is distinct from the recurrence tick, which fires on its own cadence regardless of kanban activity. It's distinct from the per-task stall ladder, which targets one task at a time. The board safety ring catches "Coach went to sleep on the entire kanban." Coach still has to act on the wake — the ring never auto-advances.

### 10.5 Reconciliation sweep (carried from v1.3.8)

Read-only pass: walks every non-archive task's working dir on disk and emits structured events to Coach when an artifact exists but the kanban hasn't recorded it. Two checks: spec unrecorded (`spec.md` exists, `tasks.spec_path IS NULL`) and audit unrecorded (`audits/audit_<round>_<kind>.md` exists, no `task_role_assignments.report_path` matches). Per-finding TTL dedupe (default 1h). Findings route to Coach with the `on_behalf_of` Coach-override tool call template baked in.

### 10.6 Soft-stall watchdog (carried from v1.3.9)

Haiku-tiered detection of agents that finished work but didn't transition the kanban (forgotten transitions / looping in chat / acknowledged-error-without-retry). Tier 1 SQL filter, Tier 2 bundled Haiku call, Tier 3 routes findings to Coach via `watchdog_finding`. Cost cap gate + dedup. Carried unchanged.

The tier-2 task hydration query filters `AND status != 'archive'` (both the single-candidate and batched paths) so a stale `agents.current_task_id` pointing at an archived task never feeds an archived row into the Haiku call. Pairs with the broadened archive-time clear in `coord_archive_task` / `coord_approve_stage` / rung-4 auto-archive: every archive path issues `UPDATE agents SET current_task_id = NULL WHERE current_task_id = ?` (unfiltered by `owner`) so role-assignees without ownership of the planner row also get cleared. Together these eliminate the "phantom stall alerts on archived tasks" failure mode (Coach 2026-05-12 report).

### 10.7 Post-archive role completion

`coord_role_complete` accepts completion against an archived task when the caller still has a non-superseded `task_role_assignments` row on it. The event emits with `post_archive: true` so subscribers (Coach rollup, Telegram bridge) can distinguish a routine in-stage completion from a "verification landed after archive" report. No stage advance is possible — the task stays archived — but Coach receives the message + artifact via the normal `task_role_completed` event fan-out and wake path. This closes the race observed in Coach's 2026-05-12 report where Players polling a long-running Zeabur deploy (30–35 min) would find the task auto-archived under them and silently lose their verification work.

---

## 11 · Pattern detection layer (N4 / N6 / N8)

The biggest reasoning win for Coach. v1 left pattern recognition implicit; v2 makes it explicit. **No parser-based deviation detection** — Coach reads the spec + commit file list directly via the event log and forms its own judgment. The harness counts what's countable from SQL alone.

### 11.1 Player health counters (N4)

Computed at prompt-build time from existing tables — no separate counter table:

| Counter | SQL source |
|---|---|
| `deviations` | Count of **distinct audit FAIL rounds** (`task_role_assignments` rows with `role IN ('auditor_syntax', 'auditor_semantics')` AND `verdict='fail'`) where the task's executor was this slot. Counts per fail, not per failed-task — a task with three syntax fails contributes 3, not 1. |
| `push_before_audit_count` | Count of `commit_pushed` events from this slot where, at commit time, the task had any auditor role row planted (active or completed) but no `audit_report_submitted{verdict='pass'}` for the current execute round. Trajectory is FYI in v2 (§4); this counter looks at actual planted role rows, not the trajectory shape. |
| `off_spec_completion_count` | Count of `deviations_log` rows (§22.1) for this Player as executor where `noticed_at IN ('push', 'audit')`. |

Computed for the active project, last 30 days. Surfaced in Coach's system prompt under `## Player health` next to `## Team composition`:

```
## Player health (last 30 days, active project)
| Slot | Deviations | Pushes-before-audit | Off-spec completions |
|------|------------|---------------------|----------------------|
| p2   | 3          | 4                   | 1                    |
| p7   | 0          | 0                   | 0                    |
```

Coach uses these to decide effort/model bumps proactively. The Coach lifecycle-policy block (§14) names the ladder explicitly: bump effort first via `coord_set_player_effort`, then model tier via `coord_set_player_model`, never runtime (human decision).

### 11.2 Audit aggregator (N6)

For every active task with audit history, Coach's prompt includes a compact audit-trajectory rendering:

```
## Audit history (active tasks)
- t-2026-05-08-abc12345 "fix header layout" (executor p2):
  - syntax round 1: FAIL — "missing edge-case test for empty title"
  - syntax round 2: PASS
  - semantic round 1: FAIL — "introduces inconsistency with the wiki entry on header sticky behavior"
  - semantic round 2: pending (auditor p7)
```

Built by `_build_audit_aggregator_rows(project_id)` in [server/agents.py](server/agents.py) — joins `tasks` to `task_role_assignments` for auditor rows, reads each `audit_*.md` file's `## Summary` section. Capped at 8 active tasks; full history accessible via the kanban card expansion.

### 11.3 Recent patterns prompt (N8)

A new `## Recent patterns` block summarizes the last `HARNESS_RECENT_PATTERNS_WINDOW_HOURS` (default 24h) of events that look like deviation, drift, or repeat issue. Built by `_build_recent_patterns_block(project_id)`:

- Repeat audit fails (same task, same kind, ≥ 2 rounds).
- Compass `confident_drift` verdicts in the same region within the window.
- `commit_without_task_id_warning` signals from the same Player.
- Players with `deviations >= 3` in the window.
- Multiple `deviations_log` rows for the same executor across distinct tasks.

Format:
```
## Recent patterns (last 24h)
- p2 has 3 audit fails across 2 tasks; consider effort bump.
- 2 confident_drift verdicts in the "pricing" region in 6h; the lattice may be wrong about pricing.
- p5 pushed without a task_id 3 times this morning; clarify their workflow.
```

Computed by the harness, not by Coach. Coach reads + acts.

### 11.4 Why no parser-based deviation flag

The original v2 draft proposed a parser-based push-time deviation check (parse `## References` / `## Done looks like` from spec.md, compare against `git diff --name-only`). Dropped: Coach can read the spec via the event log entry's pointer and the commit's file list (already in the `commit_pushed` payload) and form its own judgment. Reasoning is what Coach is for. The parser would have added three ambiguities (path extraction grammar, match algorithm, ignore list) for a check Coach already does naturally.

---

## 12 · Plan-mode policy (N3)

When Coach decides plan-mode is useful for a Player's turn, three resolution paths:

1. **Coach asks the Player to plan first** — `coord_request_plan_review(task_id, slot)` wakes the Player with plan-mode enabled. The plan returned via ExitPlanMode lands as a `pending_plan{route='coach'}` event. Coach approves (re-wakes the Player without plan-mode to execute), requests changes (Player iterates the plan), or rewrites (Coach calls `coord_approve_stage` with a Coach-composed note that supersedes the plan).
2. **Coach supplies a plan directly** — Coach writes the plan into the task's `spec.md` (or as the `note` on `coord_approve_stage`) and dispatches the executor with the plan baked in. No plan-mode round-trip.
3. **Plan from another Player** — Coach assigns a planner via `coord_approve_stage(stage='plan', assignee=<slot>)`, the planner writes `spec.md`, Coach reads + approves, then advances to execute via `coord_approve_stage(stage='execute', assignee=<other-slot>)`.

Default trust level is "Coach reviews plan first" rather than "Player runs autonomously" (R5). This is per-task, decided by Coach.

The pane gear popover's plan-mode toggle remains the human/UI-side override; Coach's choice via `coord_request_plan_review` is per-task and overrides the per-pane default for that turn only.

---

## 13 · Contract-first as default trajectory (N5)

Recommendation, not enforcement. Coach's lifecycle-policy block (§14) says explicitly:

> For any task tagged `priority ∈ {urgent, high}` or any `workflow = code` task with non-trivial blast radius, default trajectory is contract-first: include `plan`, `execute`, and at least one audit stage. Single-file mechanical edits may use `[{"stage":"execute","to":[<slot>]}]` — but you still review the resulting commit before advancing.

Coach decides. The harness does not auto-promote a trajectory.

---

## 14 · Coach lifecycle-policy prompt block

`_build_coach_coordination_block` in [server/agents.py](server/agents.py) assembles Coach's per-turn coordination context. v2 sections (in order):

1. `## Roster availability` (carried from v1).
2. `## Team composition` (carried from v1).
3. `## Player health` (NEW — N4, §11.1).
4. `## Active task health` (carried from v1.3 §17 — repeat audit-fail signal).
5. `## Audit history (active tasks)` (NEW — N6, §11.2).
6. `## Stalled tasks` (carried from v1).
7. `## Unrecorded artifacts on disk` (carried from v1.3.8).
8. `## Soft stalls (watchdog-detected)` (carried from v1.3.9).
9. `## Recent patterns` (NEW — N8, §11.3).
10. `## Recent events` (NEW — N1, §9.3 — the unread event log tail).
11. `## Trajectory examples` (carried from v1).
12. `## Lifecycle policy` (rewritten for v2 — §14.1).

### 14.1 Rewritten lifecycle policy

The `## Lifecycle policy` block in v2 names:

- **Continuous reasoning.** Coach is the team's reasoning layer. Read every event in `## Recent events` before composing the next move. The kanban records, you route.
- **Single transition tool.** Every stage transition is `coord_approve_stage(task_id, next_stage, assignee, note?)`. There is no auto-advance and no implicit assignment. Pick the assignee deliberately from the trajectory pool (or override).
- **Audit-FAIL handling.** Read the report, decide (re-spec / bump effort / clarify the audit / abandon), then call `coord_approve_stage(next_stage='execute', assignee=<slot>, note=<composed prompt>)`. The executor wakes only with your prompt, not the audit alone.
- **Pool discipline.** Pools are FYI only. There is no claim path. You explicitly assign one named slot via `coord_approve_stage`.
- **Coherent assignment (§5.5).** Player sessions stay live across review-wait. When picking an executor for follow-up work on the same area, prefer the Player who already has context. Random rotation wastes accumulated continuity.
- **Pattern-action ladder.** When `## Player health` shows `deviations >= 2` OR `## Recent patterns` flags repeat issues, bump effort first (`coord_set_player_effort`), then model tier (`coord_set_player_model`), never runtime (human decision). Read `coord_get_player_settings` before bumping so you don't re-set what's already correct.
- **Plan-mode.** Default to Coach-reviews-plan-first via `coord_request_plan_review` for non-trivial work. Trivial mechanical tasks can skip plan-mode but Coach still reviews the commit.
- **Archival.** `coord_archive_task(task_id, summary)` is your deliberate user-facing summary. No auto-archive in v2 — every task ends with a Coach-written wrap-up.
- **Compass verdicts.** Every verdict (including `aligned`) appears in `## Recent events`. Read WHY the lattice signed off, not just THAT it did. A repeated `aligned` chain on questionable work means the lattice may be drifting.
- **Deviation tagging.** When you notice scope drift, off-spec work, or unexpected changes in the artifact you're reviewing, prefix your `coord_approve_stage` `note` with a structured `[deviation: <one-line reason>]` tag. This both communicates to the next Player and feeds the validation instrumentation in §22 — Coach's deviation-noticing rate at push time vs audit time is one of the key signals for whether v2 is delivering its promise.

---

## 15 · UI surface

### 15.1 Kanban pane (`__kanban`)

Carried from v1 §11 — four active columns (Plan / Execute / Review / Ship) + Archive drawer; trajectory marker on cards; assignee avatars; status flag badges; markdown links to `[spec]` / `[audit (kind, verdict)]` / `[compass]`. Drift banner on cards in `execute` post-audit-fail.

### 15.2 Event log surface (NEW — N1)

A new `__events` slot in the LeftRail OR a tab inside `__kanban` (TBD during finalization). Filters: actor, type, task, time range, read/unread. Each row links to the underlying artifact (commit sha, audit report, etc.) via the existing `data-harness-path` mechanism.

The event log is the same data Coach reads in `## Recent events`; this surface lets the human browse it without reading Coach's prompt.

### 15.3 Player health surface (NEW — N4)

`EnvPane` gains an `EnvPlayerHealthSection` showing the same counters Coach sees in `## Player health`. Hidden when no Player has any non-zero counter.

### 15.4 Audit aggregator card (NEW — N6)

Each active-task card in the kanban view gets an inline mini-history of audit rounds (round 1 fail / round 2 pass / etc.) without expanding the card. Click to expand for full audit body.

### 15.5 EnvPane carryovers

Untouched: Attention strip, EnvOverridesSection, EnvKDriveStatusSection, EnvCostSection, EnvObjectivesSection, EnvCoachTodosSection, EnvInboxSection, EnvMemorySection, EnvDecisionsSection, EnvFileWriteProposalsSection, EnvTimelineSection.

---

## 16 · Schema

Carried from v1 §12 with two additions:

### 16.1 New tables

- `project_events` (§9.1) — N1.
- `deviations_log` (§22.1) — validation instrumentation.

### 16.2 New `tasks` columns

v2 does NOT add an `auto_advance` column — every task is review-gated. v1's `complexity` / `required_reviews` / `ship_required` (already removed in v0.3) stay removed.

The only column added in v2 is `success_criteria TEXT NOT NULL DEFAULT ''` — Coach's first-class definition of done (see §17.3 below). Optional advisory field; never blocks a transition.

### 16.3 Carried from v1

`task_role_assignments`, all existing `tasks` columns, indexes. The v1.3 trajectory column + role-assignment table are unchanged. `task_role_assignments.focus` carries over for audit framing (§5.4).

### 16.4 Schema migration v0.3 → v2.0

`_rebuild_tasks_for_kanban_v2()` in [server/db.py](server/db.py):

1. Create `project_events` table.
2. Create `deviations_log` table.
3. Backfill `project_events` from the existing `events` table — best-effort:
   - Only the last 30 days of rows are considered.
   - Only rows with a resolvable `project_id` (either on the row itself or via the active project at migration time when the row predates multi-project support).
   - Only event types that map to the v2 enum (§9.2). Unmappable types are skipped silently. The mapping is straight-pass-through for `commit_pushed` / `task_spec_written` / `audit_report_submitted` / `task_stage_changed` / `task_role_assigned` / `task_role_completed` / `task_role_stand_down` / `task_trajectory_changed` / `task_blocked_changed` / `coord_send_message` / `coord_write_knowledge` / `coord_write_decision` / `compass_audit_logged` (renamed to `compass_audit`) / `commit_without_task_id_warning` / `task_stage_stale` / `task_stall_*` / `task_spec_unrecorded` / `task_audit_unrecorded` / `watchdog_finding` / `human_attention` / `auto_compact_triggered` / `session_compacted`.
   - All backfilled rows are stamped `read_by_coach_at = now()` to avoid surfacing 30 days of history on the first tick.
4. Insert one synthetic `kanban_v2_cutover{actor='system', to='coach', body=<walk-the-board prompt>}` row (UNREAD) so Coach's first v2 tick sees an explicit instruction to walk the in-flight board.

Idempotent via `team_config['tasks_kanban_v2_migrated']` marker.

**In-flight tasks at cutover (per #3 — no special migration logic):** existing tasks just continue under v2. Tasks in mid-stages stay where they are; their next transition will go through `coord_approve_stage` like any v2 task. The harness fires a single synthetic `kanban_v2_cutover` event into `project_events` on first boot after migration so Coach's next tick sees an explicit "the kanban has moved to v2 — walk the active board and decide what to do with each in-flight task" wake. Coach acts naturally from there.

---

## 17 · Project CLAUDE.md surface

The kanban lifecycle paragraph in the canonical template at [server/templates/app_dev_claude_md.md](server/templates/app_dev_claude_md.md) is rewritten for v2. The actual paragraph that lands in every project's CLAUDE.md:

```markdown
### Task lifecycle (kanban)

Every Coach delegation goes through the kanban. Stages: plan → execute → audit_syntax (Formal Review) → audit_semantics (Semantic Review) → ship → archive. Coach defines an upfront trajectory on `coord_create_task` (`{stage, to, focus?}` list) — it documents the planned path and the candidate slots, but it's FYI only. Coach drives advances explicitly via `coord_approve_stage(task_id, next_stage, assignee, note?)`. There is no auto-routing, no auto-wake on stage change, and no auto-revert on audit fail.

**For Players:**

- You take work only when Coach explicitly assigns you via `coord_approve_stage`. Don't claim from pools — pools are FYI only.
- Do your role's work, then signal Coach with the right completion tool:
  - planner → `coord_write_task_spec(task_id, body, message_to_coach?)`
  - executor (code) → `coord_commit_push(message, task_id, push?, message_to_coach?)`
  - executor (non-code) → `coord_role_complete(task_id, message_to_coach, artifact_path)`
  - auditor → `coord_submit_audit_report(task_id, kind, body, verdict, message_to_coach?)`
  - shipper (GitHub-backed repo) → `coord_ship_to_dev(task_id)` — enforces the audit-pass gate, cherry-picks the executor commit onto a temp branch off `origin/dev`, opens a GitHub PR, squash-merges it, closes the shipper role row, and wakes Coach. **Recommended tool for all ship-stage work.** Raw `git push origin ...:dev` bypasses the audit gate and is a pb-005 violation.
  - shipper (no GitHub / manual) → `coord_role_complete(task_id, message_to_coach)` — for environments without a GitHub PAT-in-URL repo_url.
- **`message_to_coach` is your response.** What you noticed, any caveats, what the next person should know. Write it like you're talking to Coach — because you are.
- **The kanban does NOT advance until Coach reviews and approves.** Your turn ends when you've called the completion tool. Coach reads on the next tick.
- **NEVER finish a turn without a `coord_*` update message to Coach.** Even if you called one earlier in the turn — if you did anything since (file read, Bash, more reasoning), call one more; Coach reads your LAST signal. If you have nothing material to add, `coord_send_message(to='coach', body='ack — <one line>')` is the right answer.
- **Audit FAIL does NOT auto-revert.** The auditor records the verdict + body; Coach decides what happens next (re-spec / bump effort / clarify / abandon). Don't pre-emptively start fixing things based on a fail you saw — wait for Coach's wake.
- Semantic audits require a stated `focus` at assignment time — Coach frames the check.
- **If the named completion tool isn't visible in your runtime** (Codex stdio flake, MCP missing): message Coach IMMEDIATELY via `coord_send_message(to='coach', body='need to deliver task X but coord_* tool is not visible')`. Do NOT route around with raw git/Bash. Coach picks up via `on_behalf_of=<slot>` overrides on the relevant tool.

**For Coach:**

- Read the per-project event log on every tick before deciding next moves. The unread tail is in `## Recent events` in your system prompt.
- Every stage transition is one tool: `coord_approve_stage(task_id, next_stage, assignee, note?)`. Pick the assignee deliberately. The note becomes the assignee's wake prompt verbatim — write it like a brief.
- Read the Player's `message_to_coach` field along with the artifact before advancing. That's their response to you.
- On audit FAIL: read the report + the executor's prior commit, decide, then re-wake the executor with a Coach-composed note explaining what to fix.
- Archive deliberately via `coord_archive_task(task_id, summary)`. The summary is the user-facing wrap-up — write it by hand, not as an afterthought.
- Trajectory is FYI. You can change it any time via `coord_set_task_trajectory`, including inserting stages mid-flight (e.g. add `audit_semantics` after seeing the commit).
```

The Coach-driven reconciliation flow at [server/project_claude_md.py:update_claude_md_via_coach](server/project_claude_md.py) propagates this on activation, same as today.

### 17.3 `success_criteria` — Coach's first-class definition of done

`tasks.success_criteria TEXT NOT NULL DEFAULT ''` is an optional Coach-authored statement of "what done looks like" for the task. Captured at two moments and surfaced at three. **Fully optional** — system works identically when empty. **Never blocks a transition** — pure advisory context. No new tool, no new event type, no new validation gate.

**Capture moments:**

1. `coord_create_task(success_criteria?)` — Coach fills it when the bar is clear upfront (most useful for execute-only trajectories that skip the plan stage).
2. `coord_approve_stage(plan→execute, success_criteria?)` — the most informative moment, since Coach has now read the planner's `spec.md`. Replaces any value set at creation. Updates at non-plan transitions are silently ignored to keep the field stable across execute/audit/ship; if Coach decides the criteria is wrong mid-flight, they revert to plan and re-approve.

**Surface moments:**

1. **Auditor wake** — [server/kanban.py:build_auditor_wake_body](server/kanban.py) injects a `## Coach's acceptance criteria` section right after `## Focus` (and before `## Contract` / `## Project context`) when the field is non-empty. The auditor evaluates against Coach's prior, not just their own interpretation of the spec.
2. **Coach coordination block** — `## Current state` task rows in `execute` / `audit_syntax` / `audit_semantics` / `ship` get a `→ done when: <criteria>` sub-line (truncated at ~120 chars). Plan-stage tasks don't render the sub-line — Coach is still deciding the bar. Empty-criteria tasks render no sub-line at any stage.
3. **`coord_approve_stage` tool result on advance to ship** — when criteria is set, the tool result echoes `You defined done as: <criteria>` so Coach evaluates the final ship gate against their own prior, not from memory.

**Why a separate field instead of reusing `note`:** `note` is the wake prompt for the next assignee — consumed once and gone. `success_criteria` is a contract stored on the task that survives across all stage transitions. They serve different audiences (assignee vs auditor + future Coach).

---

## 18 · Telegram escalation hooks

Carried from v1 §14 with one shape change:

- `audit_fail_notification` continues to fire on every fail. First-fail noise (`escalate=False`) is filtered at the key-extractor; only `kind_round >= 2` reaches Telegram.
- `stage_assignment_needed` (v1) is removed — `coord_approve_stage` plants the role row + assignee in one atomic call, so the "assignment needed" gap doesn't exist.
- `audit_self_review_warning` continues to surface reviewer-equals-executor cases.
- `kanban_board_stalled` (NEW — §10.4) routes via the same Telegram-bridge outbound filter (forwarded only when triggered by a human-originated turn — same as `human_attention`).
- The Telegram-bridge outbound filter (forward only Coach turns triggered by a human message) carries forward unchanged. Coach v2 turns with shape-(2) review-gate decisions are Coach-internal reasoning and don't reach the phone unless the originating message was from the human.

---

## 19 · Compass cross-reference (R7)

Compass auto-audit watcher still fires on `task_stage_changed{from='plan', to='execute'}` (when Coach approves the plan→execute transition). v2 changes one thing:

**Every Compass verdict — including `aligned` — emits a `compass_audit` row in `project_events`** so Coach sees WHY the lattice signed off, not just THAT it did. v1 surfaced only `confident_drift` and `uncertain_drift`; v2 surfaces all three so Coach can spot patterns (e.g. "Compass keeps signing off but the work feels off — maybe the lattice is drifting").

The dashboard surface is unchanged. The bus event types are unchanged. The new `compass_audit` event log row is a sibling write from the same `audit_watcher` call site.

---

## 20 · Operational notes

### 20.1 Cost considerations

Coach reads the unread event log on every tick — this grows the system prompt. Cost mitigation:

- Unread tail capped at `HARNESS_PROJECT_EVENTS_PER_TICK` (default 50). Older unread rows roll forward; "+ N older unread events" footer in the prompt.
- Each event row renders to a compact one-line summary. Median ~80 chars, hard cap 240 chars per row.
- Audit aggregator (`## Audit history`) capped at 8 active tasks.
- Recent-patterns block (`## Recent patterns`) bounded to 5 lines.

Net: Coach's tick prompt grows by ~5–10 KB on a busy day, mostly cache-hit on subsequent reads within the same 5-minute window.

**When Coach is over its daily cost cap (`HARNESS_TEAM_DAILY_CAP` or per-agent cap):** the team stalls on review-gate decisions. The harness fires `human_attention{subject: "Coach over daily cap — kanban frozen", urgency: 'high'}` and freezes — there is **no auto-routing fallback** in v2. The human either bumps the cap, advances tasks manually via `POST /api/tasks/{id}/approve_stage`, or waits for cap reset. (Per #9 — "escalate to human.")

### 20.2 Rollback strategy

A behavioral rollback to v1 (auto-routing) is **not supported**. The implementation PR drops v1 code paths in one pass. If the cutover regresses meaningfully, the rollback is a `git revert` of the implementation PR + a kDrive snapshot restore.

### 20.3 What needs verification (post-implementation)

End-to-end on a deployed Zeabur instance after the implementation PR ships:

1. Full code-and-review pipeline with explicit Coach approvals: `coord_create_task` → `coord_approve_stage(plan, p5)` → `coord_write_task_spec` → `coord_approve_stage(execute, p2)` → `coord_commit_push` → `coord_approve_stage(audit_syntax, p4)` → `coord_submit_audit_report(pass)` → `coord_approve_stage(ship, p2)` → `coord_role_complete` → `coord_archive_task(summary)`.
2. Audit FAIL → event log → Coach reads → Coach calls `coord_approve_stage(execute, p2, note=<composed>)` → executor wakes with Coach's prompt.
3. Pool discipline: trajectory has `to: [p3, p7]`; Coach assigns p3 explicitly; p7 never gets a wake; the kanban refuses any claim path.
4. Compass `aligned` verdict surfaces in `project_events`; Coach can read the WHY in the next tick.
5. Player health counters update on event; effort bump suggestion appears in Coach's prompt after `deviations >= 2`.
6. Soft-stall watchdog and reconciliation sweep continue to fire (carried from v1).
7. Per-task stall ladder rungs 1–4 still fire when Coach goes silent on individual tasks.
8. Board safety ring fires when no `project_events` row lands for 30 min; Coach gets a `bypass_debounce=True` wake.
9. Player session stays live across a 10-min review-wait window; resume preserves continuity (verify via context-bar percentage in the pane).
10. Coach over cap: `human_attention` fires; no auto-fallback; manual UI path works.
11. Validation criteria (§22) measured against the 2026-05-06/07 baseline.

---

## 21 · Removed from v1 (audit trail)

Each item below names the v1 mechanism, the production failure mode it produced, and the v2 replacement.

### 21.1 R1 — Auto-wake of Players on stage transition

**v1 mechanism:** `_on_stage_changed` in [server/kanban.py](server/kanban.py) auto-woke the next assignee with a generated wake prompt.
**Failure mode:** stale wakes — Player wakes on a task since reassigned; Coach not in the loop.
**v2 replacement:** Coach explicitly calls `coord_approve_stage(next_stage, assignee, note?)`. The note becomes the wake prompt verbatim. The auto-advance subscriber is repurposed: it consumes events for the event log and pattern counters but does NOT trigger transitions or wakes.

**Scope of the rule.** "No auto-wake" applies to the *kanban engine* deciding by itself who works next — that's the path that produced stale wakes and pool races in v1. It does NOT silence Player→Coach replies: when a Player calls a completion tool the harness wakes Coach immediately (§7.2.1) so the Player→Coach channel stays a real-time conversation rather than a delayed log read. The two paths are different objects: stage transitions (Coach-gated) vs. completion replies (real-time).

### 21.2 R2 — Silent audit FAIL → executor revert

**v1 mechanism:** `audit_report_submitted{verdict='fail'}` reverted the task to `execute` and re-woke the executor with the audit attached.
**Failure mode:** Coach didn't see the FAIL until the next round; executor looped with auditor without supervision; bad audits propagated unchecked.
**v2 replacement:** FAIL surfaces to Coach via `audit_fail_notification` in `project_events`. Coach reads, decides, calls `coord_approve_stage(next_stage='execute', note=<composed>)`. The executor wakes only when Coach has decided.

### 21.3 R3 — Auto-archive without Coach summary

**v1 mechanism:** trajectory completion auto-archived with a `task_completed` Coach wake instructing Coach to "send a summary."
**Failure mode:** Coach's summary was after-the-fact and often skipped; the user-facing record was the wake prompt, not a deliberate write.
**v2 replacement:** every archive is `coord_archive_task(task_id, summary)`. The summary is Coach's deliberate user-facing artifact. No auto-archive on trajectory completion.

### 21.4 R4 — Pool-based first-claim-wins for executors

**v1 mechanism:** `coord_claim_task` + `coord_accept_role` let Players race to claim a pool seat; whoever called first won.
**Failure mode:** stuck pools (no one claimed); wrong-Player wins (least-loaded Player wasn't the right fit); Coach had no input on who got the work.
**v2 replacement:** pools are FYI only (§5.2). Coach explicitly assigns one named Player via `coord_approve_stage` after seeing the previous stage output. `coord_claim_task` and `coord_accept_role` are removed. The lone exception is stall-ladder rung 3 (§10.2), where the harness picks an alternative without Coach because Coach has been silent on the rung-2 wake for ≥1h.

### 21.5 R5 — Default plan_mode=off for new Players

**v1 mechanism:** Players ran tools immediately on wake; plan-mode was opt-in per pane.
**Failure mode:** Players blasted through complex tasks without articulating their approach; mistakes that a 30-second plan would catch landed in commits.
**v2 replacement:** Coach decides per-task whether plan-mode is useful via `coord_request_plan_review`. Default stance: contract-first or Coach-reviews-plan-first for non-trivial work (§12).

### 21.6 R6 — Silent spec → execute auto-advance

**v1 mechanism:** subscriber's `_on_spec_written` advanced `plan → execute` automatically once `spec_path` was set (unless `coach_review: true`).
**Failure mode:** Coach often wanted to read the spec before committing executor effort but had no structured way to express that intent; the v1.3.13 `coach_review` flag was opt-in, not default.
**v2 replacement:** spec writes never auto-advance. Coach reads the spec via the event log entry `task_spec_written`, decides, calls `coord_approve_stage(next_stage='execute', assignee=<slot>)`. The `coach_review` plan-stage flag is removed entirely (redundant in v2).

### 21.7 R7 — Compass auto-audit firing without Coach seeing the verdict

**v1 mechanism:** Compass auto-audit watcher fired on `plan→execute`; only `confident_drift` and `uncertain_drift` verdicts surfaced to Coach.
**Failure mode:** `aligned` verdicts were silent; Coach couldn't see WHY the lattice signed off, only that it did.
**v2 replacement:** every Compass verdict (including `aligned`) writes a `compass_audit` row in `project_events` (§19) carrying the verdict + summary + contradicting_ids. Coach reads in `## Recent events` on the next tick.

### 21.8 (consolidated) — Tool sprawl on Coach + Player sides

**v1 mechanism:** Coach had `coord_advance_task_stage` + `coord_assign_task` + `coord_assign_planner` + `coord_assign_auditor` + `coord_assign_shipper` + special-cased pool/hard-assign/future-stage logic. Players had `coord_claim_task` + `coord_accept_role` + `coord_complete_execution` + `coord_mark_shipped` + role-specific `coord_commit_push`/`coord_write_task_spec`/`coord_submit_audit_report`.
**Failure mode:** tool sprawl. Coach made multi-call mistakes (assign without advance, advance without assign); Players hit "tool not visible" cascades; on-behalf-of overrides existed for some tools but not others.
**v2 replacement:** one Coach transition tool (`coord_approve_stage`) covering all advance + assign cases. Four Player completion tools (`coord_commit_push`, `coord_write_task_spec`, `coord_submit_audit_report`, `coord_role_complete`) — three because each does real artifact work, one because the rest are structured signals. All four carry `message_to_coach` so Player→Coach communication is uniform.

---

## 22 · Validation criteria

Measured on a representative session post-cutover, against the 2026-05-06/07 baseline (the session that surfaced the failure modes v2 fixes):

(a) **% of deviations Coach noticed at push time vs at audit time.** Target: ≥ 80% noticed at push time. Instrumentation per §22.1 below.

(b) **Coach turns spent on context reconstruction.** Target: ≥ 50% reduction. The unified event log replaces individual `coord_*` read tools — Coach gets context once at tick time, not per-question. Measured by counting `coord_read_*` / state-fetch tool calls per Coach turn.

(c) **Human pings on routine items.** Target: flat or decreased. Coach absorbs more events but also more decisions; the human escalation path stays the safety net, not the routing.

If (a) drops below 50%, shape (2) is not delivering its promise — review the `## Recent patterns` rendering and the audit-aggregator surface; Coach should be reading these.

If (b) shows no reduction, the event log surface is not adequate — sharpen the `## Recent events` rendering or raise `HARNESS_PROJECT_EVENTS_PER_TICK`.

If (c) increases, Coach is over-escalating. Tighten the `human_attention` thresholds and/or coach the Coach prompt to escalate less aggressively.

### 22.1 `deviations_log` table

Validation instrumentation. A row is inserted when Coach's `coord_approve_stage` note explicitly flags a deviation, OR an audit submits with `verdict='fail'`, OR the human flags via the kanban UI.

```sql
CREATE TABLE deviations_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id      TEXT NOT NULL,
  ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  task_id         TEXT NOT NULL,
  executor        TEXT NOT NULL,        -- the slot that did the work
  noticed_at      TEXT NOT NULL CHECK(noticed_at IN ('push', 'audit', 'human')),
  description     TEXT,                  -- short reason — from Coach's note, audit body summary, or human flag
  source_event_id INTEGER                -- pointer to project_events row that triggered this
);

CREATE INDEX idx_deviations_log_project_executor ON deviations_log(project_id, executor, ts);
```

`noticed_at` resolution rule:

- `'push'` — Coach's `coord_approve_stage` call lands when the task's source stage is `execute` (no audit role row completed for the current execute round yet, OR Coach is overriding to `execute`/`archive`/`ship` directly) AND Coach's `note` includes a deviation flag.
- `'audit'` — `coord_submit_audit_report` lands with `verdict='fail'`; the row is inserted from the audit submission path with the auditor's findings as the description.
- `'human'` — POST `/api/tasks/{id}/flag_deviation` (new endpoint, body `{description}`) inserts directly.

**Deviation-flag convention.** Coach's lifecycle-policy prompt (§14.1) teaches a structured `[deviation: <one-line reason>]` tag for use in `coord_approve_stage` notes when Coach has noticed a scope drift or off-spec issue. The substring matcher recognises any of:

- The structured tag `[deviation:` — preferred form, primary signal.
- Bare phrases (fallback): `deviation`, `off-spec`, `scope drift`, `unexpected change`.

The structured tag is the reliable path because it can carry the full reason verbatim into the `description` column. The bare phrases catch organic Coach prose so we don't miss signals when Coach forgets the tag — at the cost of occasional false positives (e.g. Coach writing "no deviation here" would match `deviation`). This is acceptable for instrumentation; the validation criterion in §22 is qualitative across many tasks, so a few false positives don't distort the trend.

The `off_spec_completion_count` Player health counter (§11.1) reads from this table.

---

## 23 · Out of scope (this spec)

- Pricing / billing changes.
- Player runtime changes (Codex vs Claude); Coach can change model within a runtime, effort, plan-mode only. Runtime flips remain a human decision.
- UI changes beyond exposing the new event log to Coach + the Player health surface + the audit aggregator card.
- Cross-project changes — this spec is harness-internal.
- Drag-to-move on the kanban board (still deferred).
- Per-task time-estimation features.
- Parser-based deviation detection — Coach reads spec + diff and reasons (per #8).
- Fast-path / `auto_advance` flag — every task is review-gated (per #2).

---

## Cross-references

- [TOT-specs.md](TOT-specs.md) — umbrella spec.
- [compass-specs.md](compass-specs.md) — Compass audit watcher writes the new `compass_audit` event log row (§19).
- [recurrence-specs.md](recurrence-specs.md) — Coach's tick consumes the event log unread tail.
- [kanban-specs-v1-archived.md](kanban-specs-v1-archived.md) — historical record of the v1 system this spec supersedes.

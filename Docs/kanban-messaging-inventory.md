# Kanban → Agent messaging inventory (v0.3.10)

Every place the kanban subsystem writes text that an agent reads —
either as a tool-call response (synchronous) or as a wake-prompt
(spawned turn). Each row notes the recipient, the current
follow-up state, and any gap to close.

The pattern we want everywhere: every message ends with an
**imperative** "next action" line, named tool + task_id baked in
when applicable, never just a status report the agent can read as
"informational" and stop.

Legend:
- ✅ **strong**: imperative call to action with named tool + ids.
- ⚠ **weak**: response is descriptive only; agent could read it as
  a status report. Worth strengthening.
- ❌ **missing**: no follow-up at all; agent has no idea what to do.

---

## A. Kanban → Coach (wakes + Coach-bound events)

| Source | Trigger | Channel | Follow-up state | Notes |
|---|---|---|---|---|
| `_emit_task_completed` (kanban.py) | natural archive (shipped / commit_pushed / audit_pass at terminal) | `maybe_wake_agent('coach', ...)` + `task_completed` event | ✅ "Send a summary of the outcome to the user. Cover (1)…(2)…(3)…" + channel rules | v0.3.9.1 |
| `_emit_audit_fail_notification` (kanban.py) | `audit_report_submitted{verdict='fail'}` | `audit_fail_notification` event → Coach pane + `## Active task health` rollup when `kind_fail_count >= 2` | ⚠ Event payload has `escalate` flag but no imperative text. Coach's rollup says "bump executor effort/model." | Rollup carries the action, event itself doesn't |
| Stall sweeper rung 1 | `last_stage_change_at > 30min` | `task_stage_stale` event + `## Stalled tasks` rollup | ✅ Rollup line ends with `[escalation: nudged]` + intervention pointer | v0.3.4 + v0.3.8 |
| Stall sweeper rung 2 | persisting > 1h | `maybe_wake_agent('coach', ...)` + `task_stall_persisting` event | ✅ "Auto-reassign fires in ~M min unless you intervene. Options: nudge / reassign / override / archive" | v0.3.8 |
| Stall sweeper rung 3 success | persisting > 2h, alt available | `task_stall_auto_reassigned` event | ⚠ Event tells Coach the system reassigned, but no imperative for Coach. Probably correct: Coach doesn't need to act on a successful auto-reassign. | OK as-is |
| Stall sweeper rung 3 no-alt | persisting > 2h, no alt | `human_attention` (Telegram) + `task_stall_no_alternative` | ✅ `human_attention.body` says "auto-archive in ~M min unless you intervene" | v0.3.8 |
| Stall sweeper rung 4 | persisting > 4h | `task_stall_auto_archived` + `human_attention` | ✅ `human_attention.body` summarizes the failed ladder path | v0.3.8 |
| Reconciliation sweep | spec/audit on disk but unrecorded | `task_spec_unrecorded` / `task_audit_unrecorded` events → `## Unrecorded artifacts` rollup | ✅ Rollup includes the exact `coord_*(on_behalf_of=...)` call to paste | v0.3.8 |
| `_emit_assignment_needed` (kanban.py) | stage active but no role row / no eligible owner | `stage_assignment_needed` event | ⚠ Event payload includes role + stage but no imperative text. Coach has to figure out what to do. | **GAP — fix below** |
| `commit_without_task_id_warning` (tools.py:`coord_commit_push`) | Player commits without task_id, no auto-bind | event → Coach pane | ⚠ Event payload says "no active executor task" but no imperative. | **GAP — fix below** |
| `task_role_stand_down` | role row superseded | event with `displaced` + `new_owners` | ⚠ Event is informational for Coach; the wake to displaced Player carries the imperative. | OK — Coach sees it as timeline note |

---

## B. Kanban → Player (wakes)

| Source | Trigger | Channel | Follow-up state | Notes |
|---|---|---|---|---|
| `_wake_role_or_emit_needed` (kanban.py) | stage entry, role row active | `maybe_wake_agent(slot, ...)` + `task_role_called` event | ✅ Verify-first gate + completion-tool hint with task_id + worktree boundary + escape protocol | v0.3.2 / .4 / .6 / .7 |
| `send_role_stand_down` (kanban.py) | role row superseded by reassign / shrink | `maybe_wake_agent(slot, ...)` + `task_role_stand_down` event | ✅ "STOP work on task X — do not edit, commit, push…If you have local uncommitted changes, message Coach…" | v0.3.6 |
| Stall sweeper rung 1 nudge | rung 1 fires | `maybe_wake_agent(stage_owner, ...)` | ✅ Stage-aware completion-tool hint with task_id + tool-not-visible escape | v0.3.4 / .6 |
| `_wake_executor_for_revert` (kanban.py) | audit_report_submitted{fail} | `maybe_wake_agent(executor, ...)` | Need to verify | **CHECK BELOW** |
| Idle poller `_maybe_wake_idle` | task pool / role row needs picking up | `maybe_wake_agent(slot, ...)` | ✅ "You have actionable work. Call coord_my_assignments and read '## Next action:'…DO NOT treat the response as a status report" | v0.3.10 |
| Stall sweeper rung 3 success — wake new owner | auto-reassigned slot | `_wake_role_or_emit_needed` (inherits) | ✅ Inherits canonical role-entry wake | v0.3.8 |
| Stall sweeper rung 3 success — wake old owner | displaced by auto-reassign | `send_role_stand_down` (inherits) | ✅ Inherits canonical stand-down | v0.3.8 |
| Stall sweeper rung 4 — stand-down | active assignee at archive time | `send_role_stand_down(displaced=[stage_owner], new_owners=[])` | ✅ "STOP work" body | v0.3.8.2 |

---

## C. Player/Coach tool responses (synchronous replies)

| Tool | Caller | Response style | Follow-up state | Notes |
|---|---|---|---|---|
| `coord_create_task` | Coach | "Created task X (top-level), priority=normal, trajectory=[plan, execute, audit_syntax, audit_semantics, ship]" | ⚠ Tells Coach the task was created; no follow-up. | **GAP — should say "the kanban will auto-wake the first-stage assignee; do not follow up with coord_send_message"** |
| `coord_claim_task` | Player | Includes coord_commit_push / coord_complete_execution call signature with task_id baked in | ✅ | v0.3.3 |
| `coord_accept_role` | Player | Role-specific next-step (planner→write_task_spec, executor→commit_push, etc.) | ✅ | v0.3.3 |
| `coord_my_assignments` | Player | Four buckets + `## Next action:` footer naming the tool with task_id | ✅ | v0.3.10 |
| `coord_assign_task` | Coach | "assigned t-X → p2" or "posted t-X to executor pool: p1, p2, p3" | ⚠ Terse. No follow-up. | **GAP** — Coach doesn't see "the kanban auto-woke them; do not follow up" |
| `coord_assign_planner` | Coach | "assigned t-X planner → p3" (terse) | ⚠ Same | **GAP** |
| `coord_assign_auditor` | Coach | "assigned t-X auditor_syntax → p4" | ⚠ Same | **GAP** |
| `coord_assign_shipper` | Coach | "assigned t-X shipper → p2" | ⚠ Same | **GAP** |
| `coord_write_task_spec` | Player or Coach | "wrote spec for t-X (1234 chars) → projects/.../spec.md" | ⚠ Doesn't tell the planner what's next. | **GAP — should say "task auto-advances to execute; the executor is being woken; you're done with this role"** |
| `coord_submit_audit_report` | Player | "submitted syntax audit (round 1, pass) for t-X → projects/.../audit_1_syntax.md" | ⚠ No follow-up. | **GAP — pass case should say "task auto-advances to next stage"; fail case should say "executor is being re-woken with your report"** |
| `coord_commit_push` | Player | "committed (sha abc1234) (pushed)" + auto-bind info | ⚠ Doesn't say "task auto-advances to next stage; you're done" | **GAP** |
| `coord_complete_execution` | Player | (need to verify) | **CHECK** | |
| `coord_mark_shipped` | Player | "marked X shipped" | ⚠ Doesn't say "task auto-archives; Coach will be woken to summarize" | **GAP** |
| `coord_advance_task_stage` | Coach | (need to verify) | **CHECK** | |
| `coord_set_task_trajectory` | Coach | (need to verify) | **CHECK** | |
| `coord_set_task_workflow` | Coach | "task X workflow=..." | ⚠ Terse | Low-value gap |
| `coord_set_task_blocked` | Coach | (need to verify) | **CHECK** | |
| `coord_update_task` | Coach | "updated X: old → new" | ⚠ Terse | **GAP — same shape as `coord_advance_task_stage`** |

---

## Summary of gaps to close (priority order)

1. **`coord_assign_*` responses** (Coach side) — every assignment tool returns terse text with no "the kanban auto-wakes them; don't follow up" reminder. Coach often double-fires `coord_send_message` after assignment, duplicating the wake. Strong follow-up = save tokens + reduce confusion.

2. **`coord_write_task_spec` response** (Planner side) — "wrote spec → path" doesn't say "task auto-advances; you're done." A confused planner might call `coord_my_assignments` again to check.

3. **`coord_submit_audit_report` response** (Auditor side) — pass and fail need different post-conditions. Currently both return the same terse "submitted X audit". Should say:
   - Pass: "task auto-advances to next stage; you're done."
   - Fail: "task reverts to execute; executor will be re-woken with your report attached."

4. **`coord_commit_push` response** (Executor side) — doesn't say "task auto-advances to <next stage>; you're done with this role." Executor might unnecessarily check coord_my_assignments.

5. **`coord_mark_shipped` response** (Shipper side) — "marked shipped" doesn't say "task auto-archives; Coach will summarize for the user." Shipper might wonder if more is needed.

6. **`coord_create_task` response** (Coach side) — "Created task X" doesn't reference the auto-wake. Coach often double-fires `coord_send_message`.

7. **`coord_update_task` / `coord_advance_task_stage` / `coord_set_task_trajectory`** (Coach side) — same descriptive-only pattern; no "what to expect next" framing.

8. **`stage_assignment_needed` event payload** — fires when Coach forgot to assign. The event reaches Coach's pane but has no imperative body. Should carry "Assign a {role} via `coord_assign_{role}(task_id=..., to=...)` or rewrite the trajectory."

9. **`commit_without_task_id_warning` event payload** — Coach gets the heads-up but no imperative for what to do. Could say "If this commit was meant for an active task, link it via `coord_advance_task_stage` or accept it's scratch and ignore."

10. **`_wake_executor_for_revert`** — need to verify the audit-fail revert wake includes a strong follow-up.

---

## Out of scope (not kanban)

- `coord_send_message`, `coord_read_inbox`, `coord_read_memory`, `coord_write_decision`, `coord_save_output`, `coord_read_file`, `coord_set_player_*`, `coord_request_human`, `coord_answer_question` — not part of the kanban task lifecycle.

- Compass tools (`compass_*`) — separate subsystem.

- Telegram bridge outbound — owns its own message format.

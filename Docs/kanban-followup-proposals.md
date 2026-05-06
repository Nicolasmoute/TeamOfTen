# Kanban response-strengthening proposals (for review)

For each gap from `kanban-messaging-inventory.md`, the current text +
proposed replacement + rationale. Nothing has been implemented yet —
review and approve, then I'll batch-ship as v0.3.11.

The pattern: every response should end with one imperative line that
tells the caller **what to expect next** or **what to do next**, so
they don't read the response as a status report and stop.

---

## 1. `coord_assign_task` (Coach)

**Current:**
```
posted t-... to executor pool: p1, p2, p3
```
or
```
assigned t-... → p2
```

**Proposed:**
```
Posted t-... to executor pool: p1, p2, p3. The kanban auto-wakes
all eligible Players with the task spec; first to call
coord_claim_task wins. Do NOT follow up with coord_send_message —
the wake already includes the role context.
```
```
Assigned t-... → p2 as executor (status now 'execute'). The kanban
auto-wakes p2 with the spec + completion-tool hint. Do NOT follow
up with coord_send_message — the wake already includes everything
they need. Watch for `task_completed` or `task_stage_stale` in
your next-turn rollup.
```

**Rationale:** Coach often double-fires `coord_send_message(to=p2,
"please work on t-X")` after assignment, duplicating the wake the
kanban already sent. Coach's system prompt warns against this but the
tool response itself doesn't reinforce the rule.

---

## 2. `coord_assign_planner` / `coord_assign_auditor` / `coord_assign_shipper` (Coach)

**Current:** `_assign_role_helper` returns
```
assigned t-... planner → p3
```
or
```
called t-... planner pool: p1, p3, p5
```
or
```
reserved t-... auditor_syntax → p4
```

**Proposed:** same shape, append a per-status follow-up:

For hard-assign + currently-active stage (`woken_now=True`):
```
Assigned t-... planner → p3. The kanban auto-wakes p3 with the
task context + completion-tool hint. Do NOT follow up with
coord_send_message; the wake covers it.
```

For pool + currently-active stage:
```
Called t-... planner pool: p1, p3, p5. All three are auto-woken;
first to call coord_accept_role wins. Do NOT follow up; the wake
covers it.
```

For future-stage reservation (`woken_now=False`):
```
Reserved t-... auditor_syntax → p4. The wake will fire when the
task reaches the audit_syntax stage (after execute completes).
You can safely move on; nothing to do until then.
```

**Rationale:** Same as #1, plus the hard-assign vs reservation
distinction tells Coach whether to expect immediate or deferred
movement.

---

## 3. `coord_write_task_spec` (Planner / Coach override)

**Current:**
```
wrote spec for t-... (1234 chars) → projects/.../spec.md
```
or
```
wrote spec for t-... (1234 chars) → projects/.../spec.md (on behalf of p3)
```

**Proposed:**
```
Wrote spec for t-... (1234 chars) → projects/.../spec.md. Your
planner role is now complete. The kanban auto-advances plan → the
next stage in the trajectory and wakes the next-stage assignee.
You're done with this task unless reassigned to another role.
```

When `on_behalf_of`:
```
Wrote spec for t-... (1234 chars) → projects/.../spec.md (on
behalf of p3). p3's planner role is now complete. The kanban
auto-advances and wakes the next-stage assignee.
```

**Rationale:** A planner who just finished might call
`coord_my_assignments` again to verify they're done. The "you're
done with this role" line stops that loop.

---

## 4. `coord_submit_audit_report` (Auditor)

**Current** (same for pass and fail):
```
submitted syntax audit (round 1, pass) for t-... → projects/.../audit_1_syntax.md
```

**Proposed:** branch on verdict.

Pass:
```
Submitted syntax audit (round 1, pass) for t-... → projects/.../audit_1_syntax.md.
Your reviewer role is now complete. The kanban auto-advances to
the next stage in the trajectory (semantic review, ship, or archive
depending on what's configured). You're done with this task unless
reassigned.
```

Fail:
```
Submitted syntax audit (round 1, fail) for t-... → projects/.../audit_1_syntax.md.
The kanban reverts the task to execute and re-wakes the executor
with your report attached + the spec. Watch for `task_stage_changed`
on this task in your timeline. If the executor doesn't move within
the stall threshold (~30 min), Coach will be nudged.
```

**Rationale:** Pass and fail have very different post-conditions; a
single template is misleading. The fail case especially is worth
calling out because the auditor might think "I'm done" when actually
they may be called back for round 2 if the executor's fix doesn't
land.

---

## 5. `coord_commit_push` (Executor)

**Current:**
```
committed (sha abc1234) (pushed)
```
plus auto-bind / no-bind notes when relevant.

**Proposed:** when `task_id_in` is set + push succeeded:
```
Committed (sha abc1234) (pushed). Linked to task t-..., your
executor role is now complete. The kanban auto-advances execute →
the next stage in the trajectory (audit_syntax / audit_semantics /
ship / archive depending on what's configured). You're done with
this task unless reassigned or the audit fails (you'll be re-woken
with the report).
```

When no task_id (legitimate scratch commit):
```
Committed (sha abc1234) (pushed). NOT bound to any kanban task.
If this commit was supposed to deliver a task, the kanban won't
advance — Coach has been notified. If it's scratch work, you can
ignore.
```

When push failed:
```
Committed (sha abc1234) (PUSH FAILED: ...). Task NOT advanced —
fix the push (creds, branch, conflicts) and retry coord_commit_push.
The executor role row is still active.
```

**Rationale:** Same logic as #4 — different paths have different
post-conditions; the executor needs to know which.

---

## 6. `coord_complete_execution` (Executor)

**Current:**
```
completed execution for t-... → /path/to/artifact
```

**Proposed:**
```
Completed execution for t-... → /path/to/artifact. Your executor
role is now complete. The kanban auto-advances execute → the next
stage in the trajectory. You're done with this task unless the
audit fails (you'll be re-woken with the report).
```

---

## 7. `coord_mark_shipped` (Shipper)

**Current:**
```
marked t-... shipped
```
or
```
marked t-... shipped — <note>
```

**Proposed:**
```
Marked t-... shipped. Your shipper role is complete. The kanban
auto-archives the task and wakes Coach with a "summarize the
outcome to the user" prompt. You're done.
```

---

## 8. `coord_create_task` (Coach)

**Current:**
```
Created task t-... (top-level), priority=normal, workflow=code, trajectory=[plan, execute, audit_syntax, audit_semantics, ship]
```

**Proposed:**
```
Created task t-... (top-level), priority=normal, workflow=code,
trajectory=[plan, execute, audit_syntax, audit_semantics, ship].
The kanban auto-wakes the first-stage assignee (plan → planner).
Do NOT follow up with coord_send_message; the wake includes the
role context. You'll be re-notified at trajectory completion via a
`task_completed` wake to summarize for the user.
```

When the trajectory has empty `to: []` (no eligible owner) on the
first stage:
```
Created task t-... (top-level), priority=normal, workflow=code,
trajectory=[execute, audit_semantics]. WARNING: the first stage
(execute) has no eligible owners. The idle poller won't pick this
up automatically — you need to assign someone via
coord_assign_task or rewrite the trajectory.
```

---

## 9. `coord_update_task` (Coach, legacy stage transition)

**Current:**
```
updated t-...: plan → execute
```

**Proposed:** branch on whether the new status has an actionable
side effect.

For status changes that wake an assignee:
```
Updated t-...: plan → execute. The kanban auto-wakes the executor
(p2). Do NOT follow up with coord_send_message; the wake covers
it.
```

For archive:
```
Updated t-...: execute → archive (manual). Task is closed; no
auto-summary fires (Coach forced the archive, you decide what to
tell the user).
```

---

## 10. `coord_advance_task_stage` (Coach override)

**Current:**
```
advanced t-...: plan → execute
```
or with note.

**Proposed:** same logic as #9 — branch on whether the target stage
has an active wake.

For non-archive stages:
```
Advanced t-...: plan → execute (manual). The kanban auto-wakes
the new-stage assignee. Note: this bypassed the normal role-
completion gate, so the previous stage's role row stays open
unless you intended to skip it entirely.
```

For archive:
```
Advanced t-...: execute → archive (manual). Task is closed. NO
auto-summary fires for manual archives (you decided to kill it,
so you decide what to tell the user). If you want the user
notified, call coord_request_human or coord_send_message yourself.
```

---

## 11. `coord_set_task_trajectory` (Coach)

**Current:**
```
task t-... trajectory updated: [plan, execute, audit_semantics, ship]
```

**Proposed:**
```
Task t-... trajectory updated: [plan, execute, audit_semantics,
ship]. The kanban superseded role rows for removed stages and
inserted rows for added stages. Displaced Players (if any) get a
stand-down wake; new candidates get role-call wakes if their
stage is currently active. Do NOT follow up with coord_send_message;
the wakes cover it.
```

---

## 12. `coord_set_task_blocked` (Coach)

**Current:**
```
task t-... blocked=true
```
or with reason.

**Proposed:**
```
Task t-... blocked=true — waiting on external dependency. The
stall sweeper now ignores this task; no auto-nudges, no
auto-reassign, no auto-archive. When the blocker lifts, call
coord_set_task_blocked(t-..., blocked=false) to re-enter the
ladder.
```
```
Task t-... blocked=false. Stall sweeper resumes monitoring; the
escalation ladder restarts at rung 1.
```

---

## 13. `stage_assignment_needed` event payload (Coach-bound, fires when no assignee)

**Current:** payload is `{task_id, role, stage, to: 'coach', owner?}` —
no body text. Coach has to figure out what to do from the type alone.

**Proposed:** add a `body` field to the event payload (rendered in
Coach's pane as a `.sys` row):
```
Stage assignment needed: t-... is in `audit_semantics` with no
auditor. Assign one via coord_assign_auditor(t-..., kind=
'semantics', to=<slot>, focus=<...>). If you intended to skip
this stage, rewrite the trajectory via coord_set_task_trajectory.
```

**Rationale:** Currently Coach gets a bare event type — the meaning
("oh, I forgot to assign someone") isn't obvious without reading
the kanban subscriber's source.

---

## 14. `commit_without_task_id_warning` event payload

**Current:** `{committer, sha, message, to: 'coach'}` — no imperative.

**Proposed:** add a `body` field:
```
Player p2 committed sha abc1234 ('refactor login validation')
without a task_id and has no active executor task to auto-bind to.
If this commit was meant to deliver a kanban task, the kanban
won't advance — link it via coord_advance_task_stage or accept
it's scratch work and ignore. If it's a recurring pattern, the
Player may be working off-board (skipping coord_claim_task).
```

---

## 15. `audit_fail_notification` event payload

**Current:** `{task_id, kind, kind_round, escalate, auditor_id, executor_id, report_path, to: 'coach'}` — `escalate` is the boolean signal but no body.

**Proposed:** add a `body` field:

When `escalate=False` (first fail of this kind):
```
Audit failed: t-... ('title') failed kind=syntax round 1. The
executor (p2) was auto-re-woken with the report attached. First
fail of this kind is expected correction noise; no action needed
from you yet. Watch for round 2.
```

When `escalate=True` (≥ 2 fails of same kind):
```
Audit failed (ESCALATION): t-... ('title') failed kind=syntax
round 2. Same kind has now failed twice. The executor (p2) was
re-woken, but the loop suggests quality is the bottleneck.
Inspect their effort/model with coord_get_player_settings and
consider bumping: coord_set_player_effort(p2, 'high') or
coord_set_player_model(p2, 'latest_opus'). NEVER change runtime
— that's a human decision.
```

**Rationale:** The `## Active task health` rollup already says this,
but the rollup only renders when Coach takes another turn. The event
itself surfaces in Coach's pane in real-time; right now it's an
opaque type, the body would make it actionable on the spot.

---

## Out of scope for v0.3.11

- `coord_set_task_workflow` — workflow-tag changes are low-stakes,
  terse response is fine.
- Tools that don't touch the kanban lifecycle (memory, knowledge,
  decisions, messaging, players, runtime, etc.).
- The wake bodies that already carry strong follow-ups (don't fix
  what isn't broken).

---

## Shipping plan (when approved)

I'd batch this as a single v0.3.11 release with one test file
covering the new response shapes (one parametrized test per tool
× per branch). About 15 new tests + spec changelog entry.

Estimated effort: ~2 hours (mostly mechanical text edits + tests).
No schema changes, no event-bus changes (the new event `body`
fields are additive — old consumers ignore unknown keys).

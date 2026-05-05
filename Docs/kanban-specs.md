# Kanban task lifecycle — Specification

> **Subordinate to [TOT-specs.md](TOT-specs.md).** When this doc and
> TOT-specs disagree, TOT-specs wins. This file goes deeper on the
> kanban subsystem (stages, roles, artifacts, the auto-advance
> subscriber, the idle-Player poller) but cannot redefine fields,
> endpoints, events, or invariants that TOT-specs declares.

**Status:** Shipped (2026-05-05, v0.3.9 — trajectory-completion notification: Coach wakes on natural archive with a "summarize the outcome to the user" prompt).
**Target:** TeamOfTen multi-agent harness (Python, Claude Agent SDK, kDrive-backed shared state, single-VPS)
**Version:** 0.3.9

> **v0.3.9 (2026-05-05 trajectory-completion notification)** — at trajectory end, Coach was silent. The kanban subscriber emitted `task_stage_changed{to: archive}` on natural completion (shipped, commit_pushed at terminal execute, audit_pass at terminal audit), but the event had no `to` routing — Coach never woke + the user never got a summary unless they happened to be watching the board pane. The user pointed this out: "when a trajectory is finished, does the kanban engine notify the coach? if not it should — and in any case it should include saying send a summary of the outcome to the user." Now it does.
>   - **`task_completed` event.** `_transition` ([server/kanban.py](../server/kanban.py)) now emits `task_completed{ts, type, task_id, title, trajectory, trajectory_marker, from_stage, reason, executor, last_stage_owner, owner, to: 'coach'}` whenever the new status is `archive` AND the reason is in `_NATURAL_ARCHIVE_REASONS = {'shipped', 'commit_pushed', 'task_execution_completed', 'audit_pass'}`. The trajectory marker is the compact `P → E → AY → AS → S` form (with archived/inactive stages omitted) so Coach's prompt can render the path inline.
>   - **Coach wake on completion.** Same code path calls `maybe_wake_agent('coach', body, bypass_debounce=True)` with a structured prompt: "Task X completed: 'title'. Trajectory: P → E → S. The ship stage was completed by p4; executor: p2. Final reason: shipped. Send a summary of the outcome to the user. Cover (1) what was delivered, (2) any caveats / known limitations / open questions, (3) whether follow-up tasks are needed. Use coord_send_message(to='broadcast', body=...) if the user is watching the harness UI, or just reply normally — if this turn was user-triggered, the bridge will forward your text to the user's phone automatically. Keep it concise (3–6 sentences) unless the work is complex enough to need more." `bypass_debounce=True` ensures the wake reaches Coach even if Coach's wake debounce was active for unrelated traffic — completion is a discrete state-change signal, not a duplicated nudge.
>   - **Skipped reasons.** `reason='manual'` (Coach forced the archive via `coord_advance_task_stage(stage='archive')` or `coord_update_task` — Coach already knows + decides what to tell the user) and `reason='auto_archive_stalled'` (rung-4 of the stall ladder; the existing `human_attention` event already informs the user, and Coach summarizing a forced kill would be misleading). Mid-trajectory transitions (execute → audit_*, plan → execute, etc.) are also silent — completion is reserved for trajectory-end.
>   - **Out-of-band paths.** Rung 4 in `idle_poller` bypasses `_transition` entirely — its archive UPDATE is direct. The `task_completed` notification is therefore tied to natural-completion paths only, by construction.
>   - **Telegram interaction.** The Coach wake body explicitly mentions the user-triggered turn filter ([server/telegram.py](../server/telegram.py)): if the previous message that caused this trajectory to start was a user message (web composer or Telegram inbound), Coach's reply lands on the user's phone via the existing outbound flush. Otherwise it stays in the harness UI as a broadcast. Coach decides per-turn based on context; the prompt names both options.
>   - **Tests:** 1159 → 1165 (+6 in `test_task_completion_notify.py`: shipped-archive notifies, simple execute-only archive notifies, audit-pass terminal notifies, manual archive doesn't, non-archive transition doesn't, event carries trajectory marker).

> **v0.3.8.2 (2026-05-05 second audit pass)** — three more issues caught on a second self-audit of the v0.3.8 ladder + rollup machinery:
>   - **MEDIUM — rung 3 UPDATE now guards against concurrent supersede.** If Coach reassigns the role between the sweep's main-loop SELECT (which captures `role_row_id`) and rung 3's UPDATE, the row may already be `completed_at IS NOT NULL` or `superseded_by IS NOT NULL`. The UPDATE's WHERE clause now requires the row to still be active; on race-loss (rowcount==0) the function aborts the success path and fires `human_attention` + `task_stall_no_alternative` with `reason='role_row_changed'`. Without this, a winning sweep would silently write `owner` to an inactive row and emit a misleading `task_stall_auto_reassigned` event. (§10.5 rung 3)
>   - **MEDIUM — rung 4 archive now closes active role rows + stands down the assignee.** Previously the archive UPDATE only touched `tasks` — leaving any active `task_role_assignments` rows orphaned (visible to queries that don't also filter on `tasks.status`). And a Player who happened to be working when rung 4 fired got no signal — they kept editing the worktree on a task the kanban no longer tracked. Rung 4 now: (a) `UPDATE task_role_assignments SET completed_at = ? WHERE task_id = ? AND completed_at IS NULL AND superseded_by IS NULL` so every active row closes; (b) calls `send_role_stand_down(displaced=[stage_owner], new_owners=[])` so the active assignee gets the canonical "STOP work" wake. (§10.5 rung 4)
>   - **MEDIUM — `crash_recover` now resets the stall ladder for zombie-owned tasks.** Pre-fix: a task at rung 3 with `last_stage_change_at` from 3.5h before a crash, plus reboot delay, would have age >= 4h on the first post-reboot sweep. The next walk would fire rung 4 and archive the task — punishing the new owner for the harness's downtime, not for any actual silence. `crash_recover` now also resets `last_stage_change_at = now`, `stale_alert_at = NULL`, `stall_escalation_level = 0` for any non-archive task whose owner OR active role-row owner is in the zombie-slots set. The post-crash sweep starts at rung 1 again, giving the recovered Player a fresh window. ([server/db.py:crash_recover](../server/db.py))
>   - **Tests:** 1156 → 1159 (+3 regressions: rung-3 supersede race aborts to no_alt with `reason='role_row_changed'`, rung-4 archive closes all active role rows, rung-4 archive emits stand-down + wakes the displaced assignee).
>   - **Other audit notes (no fix needed):** `_build_unrecorded_artifacts_rows` queries last 30 events and renders 10 — beyond that capacity findings could be silently dropped, but realistic project load doesn't approach this. Multiple separate DB connections per helper (perf concern, not correctness — sqlite WAL handles fine). Rung 4 archive uses direct UPDATE rather than `_transition` (state machine guarantees archive is reachable from every active stage; the direct UPDATE is fragile if VALID_TRANSITIONS changes — flagged for future hardening if/when the state machine grows).

> **v0.3.8.1 (2026-05-05 audit-fix pass on v0.3.8)** — five issues from the v0.3.8 self-audit, ranked critical → low:
>   - **CRITICAL — rung 3 success now resets `last_stage_change_at` + zeros `stall_escalation_level`, AND breaks the rung-walk loop.** Without this, a task stalled long enough that `target_level == 4` (env defaults: age >= 4h) would walk rungs 1→2→3→4 in a single sweep — auto-reassigning to a new Player and immediately auto-archiving it. The freshly-reassigned task got archived seconds after handoff, defeating rung 3 entirely. `_fire_rung_3` now returns `True` on the success path; the rung-walk loop checks the return and breaks before rung 4 fires. The reassign-flag SQL also resets the stall window so the new Player gets a fresh ladder starting at rung 1.
>   - **HIGH — rung 3 alternatives filter now skips Players with `agents.current_task_id` set on a non-archive task.** Previously the filter only checked `is_locked`, then unconditionally overwrote `agents.current_task_id`. A busy Player would be silently yanked off whatever they were doing — same shape as the v0.3.6 raw-git-bypass problem at the auto-reassign layer. New `_has_active_task(slot)` helper joins agents → tasks and returns True only when the held task is non-archive.
>   - **MEDIUM — Coach `## Unrecorded artifacts` rollup now cross-checks current DB state.** The rollup reads from the events table over 24h, but Coach may have already submitted via `coord_write_task_spec(on_behalf_of=...)` or `coord_submit_audit_report(on_behalf_of=...)` minutes earlier. Without the cross-check, Coach's next turn sees stale findings and re-attempts overrides (which then error). New `_spec_path_already_recorded(task_id)` and `_audit_report_path_already_recorded(task_id, report_path)` helpers ([server/agents.py](../server/agents.py)) drop stale findings.
>   - **LOW-MEDIUM — rung 2 wake to Coach now uses `bypass_debounce=True`.** Previously dropped silently if Coach's debounce window was active for unrelated traffic. Rung 2 IS the escalation point; the per-rung idempotence (`stall_escalation_level`) means the next sweep wouldn't re-fire it, so a debounced wake = silent failure.
>   - **LOW — rung 2 nudge text uses the rung-1 threshold minutes, not (age // 60).** The old wording said "didn't move on the 1-min nudge" at age=65min — divided-age math, nonsense. Now reads "didn't move on the {rung1_min}-min nudge" using `_stall_threshold_seconds()`.
>   - **Tests:** 1152 → 1156 (+4 regressions: rung-3 success doesn't archive same sweep, rung-3 skips busy alternatives, rung-2 wake text uses threshold-min not divided-age, Coach rollup drops stale spec/audit findings after override).

> **v0.3.8 (2026-05-05 flow continuity)** — recurring failure mode where a Player drops the ball (lost session, restarted runtime, missing coord_*) and the kanban sits silently waiting. The legacy single-fire stall + 24h re-alert chain looped back to "wake the same assignee, hope their session is healthy." When the session is gone, the loop is silent. Three additions, designed so the system *always* makes progress:
>   - **Stall escalation ladder.** New `tasks.stall_escalation_level` column (INTEGER NOT NULL DEFAULT 0). The sweeper in [server/idle_poller.py](../server/idle_poller.py) now walks four rungs per stall, each per-task idempotent via the level column:
>     - **rung 1 (30 min, env `HARNESS_KANBAN_STALL_SECONDS`):** nudge the current-stage assignee + emit `task_stage_stale` (legacy event preserved for back-compat).
>     - **rung 2 (1 h, env `HARNESS_KANBAN_ESCALATE_COACH_SECONDS`):** emit `task_stall_persisting` routed to Coach + wake Coach with explicit "intervene before auto-action" framing naming the next deadline.
>     - **rung 3 (2 h, env `HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS`):** auto-reassign to another eligible Player from the stage's `eligible_owners` (excluding the stuck owner + locked Players). Updates the role row's `owner` + (for executor) `tasks.owner` + `agents.current_task_id`. Emits `task_stall_auto_reassigned`, fires `task_role_stand_down` for the displaced owner, and re-uses `_wake_role_or_emit_needed` so the new owner gets the canonical role-entry wake. If no alternative is reachable, fires `human_attention` + `task_stall_no_alternative`.
>     - **rung 4 (4 h, env `HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS`):** auto-archive via direct UPDATE + `task_stage_changed{reason=auto_archive_stalled}` + `task_stall_auto_archived` + `human_attention`. Resets `stall_escalation_level = 0` and clears `stale_alert_at` so a re-opened task starts fresh.
>     The level is reset to 0 on every code path that clears `stale_alert_at` (kanban subscriber `_transition`, `coord_update_task`, `coord_advance_task_stage`, `coord_assign_task`, all human-side stage endpoints — 14 sites). Default times match the user's halved schedule (was 1h / 2h / 4h / 8h in the original proposal). (§10.5, §17 stall handling)
>   - **Reconciliation sweep.** New `reconciliation_sweep_once()` in [server/idle_poller.py](../server/idle_poller.py) runs as a sibling pass in the existing tick loop (alongside the per-Player wake + stall sweeper). Read-only: walks every non-archive task's folder on disk, diffs against `tasks.spec_path` / `task_role_assignments.report_path`. Emits `task_spec_unrecorded` when `<task_dir>/spec.md` exists but the kanban row has no spec_path; emits `task_audit_unrecorded` when `<task_dir>/audits/audit_<round>_<kind>.md` exists but no auditor role row records that path. Each finding routes to Coach with the `on_behalf_of` Coach-override tool call template baked in, so Coach can submit through normal channels without manually constructing the call. Per-finding TTL dedupe (default 1h, env `HARNESS_KANBAN_RECONCILE_TTL_SECONDS`) so Coach isn't spammed every 5min for the same artifact. Feature flag `HARNESS_KANBAN_RECONCILE_ENABLED` (default true). Catches the recurring p1 / p3 / p8 trace shape directly — Player wrote the work, kanban didn't notice. (§10.6 new)
>   - **Kanban rollup folded into existing Coach tick.** No new tick — the existing `## Stalled tasks` block in `_build_coach_coordination_block` ([server/agents.py](../server/agents.py)) gets two upgrades: each row now labels its `[escalation: <rung>]` so Coach sees which auto-action is imminent (fresh / nudged / Coach-notified — auto-reassign next / auto-reassigned — auto-archive next / auto-archived); and a new `## Unrecorded artifacts on disk` block lists each reconciliation finding with the path + the suggested `coord_write_task_spec(on_behalf_of=...)` / `coord_submit_audit_report(on_behalf_of=...)` call. New helper `_build_unrecorded_artifacts_rows(project_id)` reads `task_spec_unrecorded` / `task_audit_unrecorded` events from the last 24h. (§17 Coach quality feedback)
>   - **Tests:** 1137 → 1152 (+15 regressions: 7 in `test_stall_escalation_ladder.py` covering rung 1/2/3 reassign, rung 3 no-alt → human_attention, rung 4 archive + level reset, idempotence, progress-resets-level; 8 in `test_reconciliation_sweep.py` covering spec-unrecorded happy path + dedupe + recorded-path silence, audit-unrecorded happy path + recorded-path silence, malformed filename ignored, archived-task skip, feature flag).
>   - **Closed v0.3.7 known gap (Coach archive):** rung 4 auto-archive does NOT yet stand-down the current assignee or block subsequent `coord_commit_push` against the archived task. Same applies to Coach-initiated archive via `coord_advance_task_stage`. Still a v0.3.9 candidate.

> **v0.3.7 (2026-05-04 production trace 4, p8 misplaced-work incident)** — p8 wrote their implementation to `/workspaces/.project` (the shared seed checkout used to provision worktrees) instead of `/workspaces/p8/project` (their own per-slot worktree). `coord_commit_push` ran `git status` inside p8's worktree, saw a clean tree, returned `"nothing to commit (working tree clean)"` — opaque soft-OK that left the work stranded on a tree no branch belonged to. Two fixes:
>   - **Misplaced-work detection in `coord_commit_push`.** When the slot's worktree is clean, the tool now peeks the shared `BASE_REPO_PATH` (`/workspaces/.project`) via `git status --porcelain` (15s timeout, scrubbed env). If the seed checkout is dirty, the tool returns a loud named error: "your worktree at X is clean, but the shared seed checkout at Y has uncommitted changes. The shared checkout is not yours to commit from — per-worktree isolation is mandatory." The error names both paths and gives the fix path (`cd <slot worktree> and re-apply`, or `git -C <base> stash && git -C <slot> stash pop`). When `BASE_REPO_PATH` doesn't exist or has no `.git` (e.g. deploy without seed checkout), the legacy soft-OK behavior is preserved. (§6.4)
>   - **Per-slot worktree boundary on executor wakes.** New helper `_executor_worktree_boundary(role, slot)` in [server/kanban.py](../server/kanban.py) returns a per-slot suffix appended to executor wake prompts only: "Worktree boundary: your edits MUST land in `/workspaces/<slot>/project` (your own git worktree on branch `work/<slot>`). Do NOT edit `/workspaces/.project` — that is the shared seed checkout..." Wake loop in `_wake_role_or_emit_needed` calls the helper per slot when iterating `targets`. Auditors / planners / shippers don't edit code — they get the empty string, no prompt bloat. (§9.1)
>   - **Tests:** 1130 → 1137 (+7 regressions in `test_misplaced_work_detection.py`: clean+clean soft-OK preserved, clean+dirty loud error, dirty slot skips base-peek and commits normally, missing base-`.git` falls back gracefully, executor wake includes boundary, non-executor wake omits boundary, helper unit test).

> **v0.3.6 (2026-05-04 production trace 3, p1 raw-git incident)** — Coach reassigned planner from p1 to p5; p1 never received a stop-work signal, kept reading their stale wake message as authoritative ownership, hit a missing `coord_*` tool, and routed around it with raw `git commit && push` via Bash. The push landed on a feature branch (`feat/ada-162c38cd-causal-replay`) that the kanban thought belonged to p5. Three layered failures, three additions:
>   - **Verify-first gate in the wake prompt.** `_wake_role_or_emit_needed` ([server/kanban.py](../server/kanban.py)) now leads the hard-assigned wake with: "BEFORE editing, committing, or publishing anything: call `coord_my_assignments` and confirm task X appears under your active roles with `role=...`. If you do NOT see it, you've been reassigned — STOP and message Coach." The previous wording phrased `coord_my_assignments` as a context-fetch (skippable in the reader's mind); it's now the gate. Pool wakes likewise warn explicitly that doing the role work without an accepted claim earns no kanban credit. (§9.1)
>   - **No-raw-shell boundary in `_TOOL_NOT_VISIBLE_ESCAPE`.** The escape paragraph appended to every wake hint (v0.3.4) now explicitly forbids the workaround p1 picked: "do NOT route around the missing coord_* tool by using raw git/Bash/Edit to commit, push, or publish the deliverable yourself. Those bypass every kanban guardrail." Stop-and-message-Coach is the only sanctioned path. (§9.1 escape protocol)
>   - **Stand-down wake on role supersede.** New helper pair in [server/kanban.py](../server/kanban.py): `collect_superseded_role_owners()` (pre-read of about-to-be-displaced slots) + `send_role_stand_down()` (post-commit wake + `task_role_stand_down` event, routed `to: coach`). Wired into all four supersede sites: `coord_assign_task` hard-assign, `coord_assign_task` pool form, `_assign_role_helper` (planner/auditor/shipper), and `coord_set_task_trajectory` (both removed-stage and in-place eligible_owners change). The displaced Player gets an explicit "STOP work on task X — do not edit, commit, push, or publish anything for this task" message naming the new owner. Same-slot refresh is filtered (no spurious ping when Coach re-pokes the existing assignee); de-dup across multiple displaced rows. (§6.1, §8 events)
>   - **Tests:** 1120 → 1130 (+10 regressions in `test_role_supersede_stand_down.py`: prompt-content for verify-first + no-raw-shell + pool-warning, plus stand-down across planner reassignment / executor hard-assign / pool conversion / trajectory removal / in-place owner swap / same-slot silence / event payload).
>   - **Known gap (v0.3.8 candidate):** Coach archive (`coord_advance_task_stage(stage='archive')`) does NOT fire stand-down for the current assignee, does NOT mark active role rows complete, and does NOT block subsequent `coord_commit_push` against the archived task. A Player working at the moment of archive will not know to stop. Hardening this is a small follow-up.

> **v0.3.5 (2026-05-04 production trace 2, 1 gap)** — second instance of the same failure mode (Player on Codex runtime can't reach `coord_*`), this time on the planner. Player wrote `spec.md` to disk and reported "I can't call coord_write_task_spec from this runtime"; v0.3.4 only added the override for audit submission.
>   - **`coord_write_task_spec` gains `on_behalf_of` Coach override.** Mirror of `coord_submit_audit_report(on_behalf_of=...)`: Coach reads the Player's on-disk `spec.md`, copies the body, calls `coord_write_task_spec(task_id=..., body=..., on_behalf_of='<player_slot>')`. The recorded `spec_author` is the named Player; that Player's planner role row is marked complete (so the spec gate releases properly + the kanban auto-advances `plan → execute`); the bus event's `agent_id` is Coach, and `task_spec_written` + `task_role_completed` events both carry `on_behalf_of` for timeline labeling. Verifies the named Player has an active planner role on the task before crediting (rejects with a clear error otherwise — Coach can't accidentally credit unrelated slots). (§6.2)
>   - **Tests:** 1115 → 1120 (+5 regressions: happy-path, Player rejection, unassigned-Player rejection, invalid-slot rejection, event-payload `on_behalf_of` field).

> **v0.3.4 (2026-05-04 production trace, 5 bugs)** — folded in on top of v0.3.3. Real failure: Theo (p3) was assigned semantic auditor on a task in `audit_semantics`, did the review, wrote `audit_1_semantics.md` to disk, but reported "the coord task tools are not exposed here" multiple times and never called `coord_submit_audit_report`. The kanban silently sat for 15+ min; the stall sweeper then named the wrong Player (the executor p8) as the blocker; Coach got "no activity from p8" and nudged the wrong person.
>   - **Stall sweeper attributes blame to the current-stage assignee, not `tasks.owner`.** The sweeper now reads the live `task_role_assignments` row for the task's current stage (e.g. `auditor_semantics` when `status = 'audit_semantics'`) and surfaces THAT row's owner as the stall blocker. `tasks.owner` is kept visible separately as `task_executor` in the event payload + Coach rollup, so context is preserved without misdirecting the nudge. (§10.5, §17 stall handling)
>   - **Coach `## Stalled tasks` rollup names blocker + executor.** Same fix in `_build_stalled_tasks_rows` ([server/agents.py](server/agents.py)). When the two slots differ the rollup renders `blocker p3 (executor p8)` so Coach sees both at a glance. The trailing instruction now points Coach at the override path for tool-unreachable Players.
>   - **Stall reminder is stage-aware.** New `_stall_nudge_for_stage()` returns role-specific completion-tool wording: planner→`coord_write_task_spec`, executor→`coord_commit_push`/`coord_complete_execution`, auditor→`coord_submit_audit_report` with the right `kind`, shipper→`coord_mark_shipped`. Previously hardcoded the executor tools regardless of stage, telling auditors to commit code.
>   - **"Tool not visible" escape paragraph in every wake hint.** `_completion_hint_for_role()` now ends with an explicit instruction: if the named tool is not visible in the Player's runtime, message Coach IMMEDIATELY via `coord_send_message` (or `coord_request_human` for hard escalation) — DO NOT write the deliverable to disk and stop. The stall nudge ships the same escape. This addresses the real failure mode where Players quietly fail to drive the kanban because they can't see the tool.
>   - **`coord_submit_audit_report` gains `on_behalf_of` Coach override.** Coach can now submit an audit report on behalf of an unreachable Player by passing `on_behalf_of='<slot>'`. Recorded auditor / role-row owner / event `auditor_id` is the Player; bus event `agent_id` is Coach (so the timeline shows who actually pressed the button). Player-callable path unchanged (rejects `on_behalf_of`); Coach without `on_behalf_of` still gets the "Coach doesn't audit" rejection — the override is the only Coach path. Use case: Player wrote the audit to disk, Coach reads it, copies the body, submits with `on_behalf_of=<player_slot>`. Beats `coord_advance_task_stage` because the audit content is preserved + the role row + verdict are recorded properly. (§6.3)
>   - **Tests:** 1106 → 1115 (+9 regressions: 4 covering `on_behalf_of`, 5 covering stall stage-owner attribution + stage-aware nudge text + tool-not-visible escape).

> **v0.3.3 (2026-05-04 flow-continuity audit-pass)** — folded in on top of v0.3.2:
>   - **Auto-bind hardened against ghost tasks.** `coord_commit_push`'s auto-bind query now requires a LIVE `task_role_assignments` row (`completed_at IS NULL AND superseded_by IS NULL`), not just `tasks.owner = caller AND status = 'execute'`. Without this guard, a task whose executor row was completed by a prior commit but never advanced (subscriber crash, lost event) would be picked by auto-bind and then immediately rejected by the downstream validator with "no active uncompleted executor role" — a confusing UX directly after silent auto-binding. (§6.5)
>   - **`coord_claim_task` response echoes the next-step.** The synchronous response was bare (`"claimed t-X"`); the kanban subscriber's stage-change wake re-prompted with the named-tool hint, but that's a separate turn. The immediate response now includes the `coord_commit_push` / `coord_complete_execution` call signature with `task_id` baked in plus the "kanban does NOT advance until you call one of those" reminder.
>   - **`coord_accept_role` response echoes the role-specific next-step.** Same shape, branched per role: planner → `coord_write_task_spec`, executor → `coord_commit_push` / `coord_complete_execution`, auditor → `coord_submit_audit_report`, shipper → `coord_mark_shipped`. Mirror of the kanban subscriber's stage-entry wake, just landed on the synchronous accept response.
>   - **Tests:** 1105 → 1106 (+1 regression: `test_commit_push_does_not_auto_bind_when_role_completed`).

> **v0.3.2 (2026-05-04 flow-continuity, 3 gaps)** — folded in:
>   - **Stage-entry wake names the completion tool.** [server/kanban.py](server/kanban.py)'s `_completion_hint_for_role()` returns role-specific wake instructions that NAME the right tool with `task_id` baked in: planner→`coord_write_task_spec`, executor→`coord_commit_push` / `coord_complete_execution`, auditor→`coord_submit_audit_report`, shipper→`coord_mark_shipped`. Executor hint is trajectory-aware: includes the SELF-AUDIT instruction verbatim when the trajectory has no audit stage after `execute`. Vague "use the matching completion tool" wording was the #1 cause of "Player did the work but the kanban didn't move." (§9.1)
>   - **`coord_commit_push` auto-binds + warns Coach.** When the caller omits `task_id` but has exactly one active executor task in the project, the tool auto-binds it (kanban advances; response tells the Player so they learn the right shape). When the caller has NO active executor task and commits without `task_id`, the commit succeeds (it might be legitimate scratch work) but a `commit_without_task_id_warning` event is published routed to Coach. The `commit_pushed` event grows a `task_id_auto_bound: bool` field. (§6.5)
>   - **Stall threshold default lowered to 1h.** `HARNESS_KANBAN_STALL_SECONDS` default dropped from 14400 (4h) to 3600 (1h). The owner-side stall nudge now NAMES the completion tools with `task_id` baked in. (§10.5)
>   - **Tests:** 1085 → 1105 (+20 regressions).

> **v0.3.1 (2026-05-04 audit cycle, 12 items)** — folded in:
>   - **Initial activation on create.** `coord_create_task` / `POST /api/tasks` set `tasks.status` to the trajectory's first stage (was hard-defaulting to `plan`), hard-set `tasks.owner` when the first stage has a single slot, and emit `task_stage_changed` from `null → first_stage` so the wake chain fires. (§6.5)
>   - **Spec-write advance.** Subscriber listens for `task_spec_written` and walks `plan → next_stage` once `spec_path` is set. (§9.1)
>   - **Trajectory reroute schema fix.** `coord_set_task_trajectory` and `POST /api/tasks/{id}/trajectory` now deactivate orphaned role rows via `completed_at = now()` (was `superseded_by = -1`, which violated the self-FK under `PRAGMA foreign_keys = ON` and named columns that didn't exist). (§3.8, §6.1)
>   - **Already-entered stage guard.** Reroute walks the OLD trajectory up to and including the current stage and rejects removal of any stage in that prefix. (§3.8)
>   - **Manual transitions stamp `last_stage_change_at`.** Every status-change site (`coord_claim_task`, `coord_assign_task` plan→execute, `coord_update_task`, `coord_advance_task_stage`, the cancel + stage HTTP endpoints) updates `last_stage_change_at` and clears `stale_alert_at` so the stall sweeper sees the move. (§2.3)
>   - **Assignment supersede + trajectory mirror.** `coord_assign_planner / auditor / shipper / task` insert a fresh role row, supersede any prior active row for the same `(task_id, role)` via `superseded_by`, and mirror the new candidate list back into `tasks.trajectory.to`. The board no longer shows stale assignees beside the new pick. (§6.1)
>   - **`stage_assignment_needed` rename + back-compat alias.** Subscriber emits `stage_assignment_needed` (covers all stages, not just audit/ship); legacy `audit_assignment_needed` is also published as a back-compat alias for one release. Telegram escalation switched its key extractor and resolution map to the new name. (§8, §14)
>   - **Audit-fail Telegram surface.** Telegram escalation now keys on `audit_fail_notification` with `escalate=True` instead of raw `audit_report_submitted{verdict='fail'}`, so first-fail noise stays out of the phone and only the second fail of the same kind pings. (§14, §17)
>   - **Revert wake reads spec/report from row + extracts failed criteria.** `_wake_executor_for_revert` reads `tasks.spec_path` and falls back to `tasks.latest_audit_report_path`; `_extract_failed_criteria()` pulls a `## Failed criteria` (and aliases) section verbatim from the audit report (capped 1500 chars). (§2.3)
>   - **Per-project Compass correlation.** New `_recent_commit_per_project` cache; `compass_audit_logged` correlates to the project's latest commit, not the global tail. Falls back to global only when the event lacks `project_id`. (§9.1)
>   - **Tests:** 1064 → 1085 (+21 regressions across `test_kanban_subscriber.py`, `test_kanban_tools.py`, `test_kanban_api.py`, `test_telegram_escalation.py`).

---

## 1 · Overview

### 1.1 Purpose

Every Coach delegation in TeamOfTen flows through an explicit **kanban-shaped lifecycle**. There is no admission gate: if Coach is handing work to a Player, it goes on the board. Conversational replies (broadcasts, clarification questions, nudges) remain conversational and are not tracked.

Each task is in exactly one stored stage — `plan` → `execute` → `audit_syntax` (formal review) → `audit_semantics` (semantic review) → `ship` → `archive` — and each task carries an explicit **trajectory** Coach defines on creation: an ordered list of stages to traverse, with per-stage assignees (single Player or pool). The storage names remain `audit_syntax` / `audit_semantics` for back-compat, but the product language is **Formal Review** / **Semantic Review**.

The shape is event-driven: execution-completion, commit, review, and ship events auto-route tasks along the trajectory; Coach calls Players into roles upfront and never executes work themselves. Coach watches the per-turn rollup for audit-loop quality issues and stalled tasks, and adjusts via per-Player effort/model bumps or trajectory rewrites.

### 1.2 Scope

This spec covers:
- The trajectory model, six-stage state machine, and valid transitions.
- The five **roles** (planner / executor / auditor_syntax / auditor_semantics / shipper) that Players can be assigned to.
- The artifacts produced at each stage (`spec.md`, `audits/audit_<round>_<kind>.md`, plus the parallel informational Compass audit report).
- The MCP tools Coach uses to plan + call/assign and Players use to execute / review / ship.
- The HTTP endpoints, bus events, and UI surface (`__kanban` slot).
- The schema (`tasks.status` enum, `tasks.trajectory`, `task_role_assignments` table, denormalized card-render columns).
- The auto-advance subscriber, idle-Player poller + stall sweeper, flow-health endpoint, CLAUDE.md kanban block, and Telegram escalation hooks.
- The Coach quality-feedback rollup (audit-loop signal + stalled-tasks signal).

Out of scope: drag-to-move on the board (deferred to v2), per-task priority changes from the UI (priority is set at create time only), per-task time-estimation features.

### 1.3 Glossary

| Term | Meaning |
|---|---|
| **Stage** | One of `plan` / `execute` / `audit_syntax` / `audit_semantics` / `ship` / `archive`. Stored in `tasks.status`. |
| **Trajectory** | The ordered list of stages a task will traverse, plus per-stage `eligible_owners`. Stored in `tasks.trajectory` as a JSON array of `{stage, to}` objects. Coach defines it on `coord_create_task`; mid-flight reroute via `coord_set_task_trajectory`. Replaces the v0.2 `required_reviews` + `ship_required` + `complexity` triple. |
| **Role** | A function a Player performs on a specific task: `planner`, `executor`, `auditor_syntax`, `auditor_semantics`, `shipper`. Stored as rows in `task_role_assignments`, populated up front from the trajectory. |
| **Workflow** | The domain flavor of the task: `code`, `research`, `writing`, `marketing`, `ops`, or `generic`. Stored in `tasks.workflow`; shapes prompt wording, not routing. |
| **Tracking reason** | Optional informational tag. Stored in `tasks.tracking_reason`. Not required, not validated against an enum. Kept for filtering/analytics. |
| **Call / Pool** | A list of eligible Players (`eligible_owners`) on a role assignment. The current-stage candidates are woken; the first Player to `coord_accept_role` wins atomically. Executor pools can also use the legacy `coord_claim_task`. Future-stage reservations are not actionable until the card reaches that stage. |
| **Round** | The Nth review cycle on a task. A task that fails formal review twice has rounds 1 (fail) and 2 in `audits/audit_1_syntax.md` / `audit_2_syntax.md`. |
| **Verdict** | A reviewer decision: `pass` or `fail`. Drives the auto-advance subscriber. |
| **Spec** | The `spec.md` markdown file produced in the plan stage. Required when the trajectory includes `plan`; optional when omitted. |
| **Active assignment** | The row in `task_role_assignments` for `(task_id, role)` with `superseded_by IS NULL` and the most recent `assigned_at`. |
| **Stall** | A non-archive task whose `last_stage_change_at` is older than `HARNESS_KANBAN_STALL_SECONDS` (default 4 h). Detected by the stall sweeper in `idle_poller.py`. |

---

## 2 · Stage state machine

### 2.1 Stages

- **plan** — task created, no executor yet (or has a planner working on the spec). Cards in this stage may have `spec_path = NULL` if the spec hasn't been written yet.
- **execute** — owned by a Player. Sub-state inside execute: `started_at IS NULL` (hard-assigned or reset after audit fail; hollow avatar) vs `started_at` populated (self-claimed executor work; filled avatar).
- **audit_syntax** — formal review. For code this means tests/CI/lint/mechanical correctness; for research it means citations, structure, and traceability; for writing/marketing it means format, tone guide, and required-message compliance.
- **audit_semantics** — semantic review. For code this means “does it solve the user need?”; for research it means argument quality and rigor; for writing/marketing it means positioning, audience fit, and substantive usefulness.
- **ship** — required reviews are green, ready for the assigned shipper to merge, publish, send, hand off, or explicitly close as “nothing to merge/publish”.
- **archive** — terminal. `archived_at` set. `cancelled_at` non-null distinguishes a cancellation from a delivered task.

The two timestamp columns inside `execute` (`claimed_at` and `started_at`) distinguish assignment/claim from self-started work:

| Entry path | `claimed_at` | `started_at` |
|---|---|---|
| Hard-assign (`coord_assign_task(to='p3')`) | now | NULL (the current implementation does not stamp `started_at` merely because the wake turn was launched) |
| Pool-claim (`coord_claim_task` from `eligible_owners`) | now | now (self-claim **is** starting) |
| Self-claim (Player picks up an unowned plan-stage task) | now | now |

Across an audit-fail revert, `started_at` is cleared so the card flips back to "assigned, not self-started"; `claimed_at` is preserved (the executor still owns the task). The next executor turn is triggered by auto-wake, but `started_at` will remain NULL unless the task is claimed again through the self-claim path or a future implementation adds an explicit turn-start stamp.

### 2.2 Valid transitions

```
plan            → {execute, archive}
execute         → {audit_syntax, audit_semantics, ship, archive}
audit_syntax    → {audit_semantics, ship, archive, execute}
audit_semantics → {ship, archive, execute}
ship            → {archive}
archive         → {}
```

The broader transition map is **trajectory-driven**: the stages above are the universe of valid transitions, but the actual path a task walks is the ordered list in `tasks.trajectory`. A trajectory of `[execute]` routes `execute → archive` directly; `[plan, execute, audit_semantics, ship]` routes `plan → execute → audit_semantics → ship → archive`. The auto-advance subscriber walks the trajectory in order; pass goes to the next stage in the list, fail reverts to `execute`, terminal step archives.

Cancellation is `<any non-terminal stage> → archive` with `cancelled_at = now()`. The `blocked` flag is orthogonal and toggleable in any non-terminal stage; it does not change `tasks.status`.

The validator lives at [server/tools.py:VALID_TRANSITIONS](server/tools.py).

### 2.3 Trajectory-walking gate

Stage transitions are driven by **role-completion events** and routed by the trajectory walker `_next_stage()` in [server/kanban.py](server/kanban.py):

| Transition | Required condition |
|---|---|
| `plan → execute` | `tasks.spec_path IS NOT NULL` AND active executor assignment with `owner IS NOT NULL`. The spec gate fires only when `plan` is in the trajectory; trajectories that omit `plan` skip the spec gate entirely. |
| `execute → next stage in trajectory` | `commit_pushed` with task id for code work, or `task_execution_completed` from `coord_complete_execution` for non-code artifacts. `coord_commit_push` only preserves the id after a successful push, or explicit `push=false` local-only delivery. The next stage is `_next_stage(task, "execute")`. |
| `audit_syntax → next stage in trajectory` | Active formal reviewer has `verdict = 'pass'`. Walker returns the next stage in `tasks.trajectory` after `audit_syntax`. |
| `audit_semantics → next stage in trajectory` | Active semantic reviewer has `verdict = 'pass'`. Walker returns the next stage in `tasks.trajectory` after `audit_semantics`. |
| `ship → archive` | `task_shipped` event from the assigned shipper. |
| `audit_* → execute` (revert) | Active auditor assignment has `verdict = 'fail'`. Auto-fires; clears `tasks.started_at` so the card no longer looks self-started. |

On a fail revert, the auditor's role-assignment row stays **terminal** — its `verdict` and `report_path` are written, `completed_at` set, and that audit round is done. The next round (after the executor re-commits) gets a **new** `auditor_<kind>` row inserted by `coord_assign_auditor` and the `round` counter on the next report increments. The executor's role row, by contrast, is **not** superseded — they're still the executor — but `tasks.started_at` is cleared so the active card reads as "assigned after failed audit" rather than "already self-started." This split is what lets the card-expansion view render the audit history (round 1 fail → round 2 pass → ...) while the latest-only fields on the row stay accurate for cheap card rendering.

**Audit-fail loop guarantee.** A `verdict='fail'` from any review stage **always** loops the task back to `execute`, regardless of trajectory shape, and the executor is re-woken with the latest audit report attached. This is the inner correction loop that makes the kanban work: the executor doesn't have to chase the report down, the system delivers it. Concretely, when `coord_submit_audit_report{verdict='fail'}` lands:

1. `tasks.latest_audit_report_path` / `latest_audit_kind` / `latest_audit_verdict='fail'` are written before the event fires.
2. The subscriber transitions the task to `execute`, clears `started_at`, stamps `last_stage_change_at`.
3. The executor's auto-wake prompt includes verbatim:
   - the path to `tasks.spec_path` (read fresh from the task row so a re-spec mid-execution gets picked up),
   - the path to `tasks.latest_audit_report_path` (so the executor can read **what failed and why**),
   - the **failed-criteria section verbatim** when the audit report contains a `## Failed criteria`, `## Failed acceptance criteria`, `## Acceptance criteria failed`, or `### Failed criteria` heading. Extracted by `_extract_failed_criteria()` in [server/kanban.py](server/kanban.py); capped at 1500 chars so a giant report doesn't blow the wake-prompt budget. The section ends at the next `## ` / `# ` heading. Missing section, missing file, or unreadable text degrades cleanly to "no extra context appended" — the executor still gets the report path.
4. The card flips to `EXECUTE` column with a red drift banner; clicking the banner opens the audit report in the Files pane.
5. The subscriber **also** publishes an `audit_fail_notification` event routed to Coach (`to: 'coach'`). This event carries `{task_id, kind, kind_round, escalate, auditor_id, executor_id, report_path}` where `kind_round` is the number of fail verdicts so far for this task and audit kind, and `escalate = (kind_round >= 2)`. Coach sees the event in their pane and (subject to the existing escalation watcher) on Telegram. The first fail of any kind is informational; the second fail of the **same kind** is the escalation signal — see §17 for how Coach acts on it.

There is no cap on revert rounds — a task can iterate `execute → audit → execute → audit → ...` indefinitely. Coach sees the notification on every fail but is told to expect a first fail as normal correction noise; quality intervention happens on the second fail of the same kind.

Every successful transition stamps `tasks.last_stage_change_at = now()` and clears `tasks.stale_alert_at` in the same DB write as the status update. **This applies to every path** — the kanban subscriber's `_transition()` helper, `coord_claim_task`, `coord_assign_task` (plan→execute hard-assign), `coord_update_task`, `coord_advance_task_stage`, `POST /api/tasks/{id}/stage`, `POST /api/tasks/{id}/cancel`, the `coord_create_task` initial insert, and the cancel paths. This is what drives the stall sweeper (§10.5) and the `## Stalled tasks` Coach rollup; if a code path moves a task between stages without touching these columns, stall detection silently lies.

`coord_advance_task_stage` is the explicit Coach-override tool that bypasses the trajectory gate when an assignment stalls. The human `/api/tasks/{id}/stage` endpoint uses the same gate unless `force=true`.

### 2.4 Blocked flag

`tasks.blocked` (INTEGER 0/1) plus `tasks.blocked_reason` (TEXT). Set/cleared via `coord_set_task_blocked` (Coach or owner) or `POST /api/tasks/{id}/blocked`. Doesn't change stage; cards render a `BLOCKED` flag overlay.

### 2.5 Cancellation

`POST /api/tasks/{id}/cancel` or `coord_update_task(status='archive')` with the legacy alias `cancelled` accepted. Sets `cancelled_at = now()` and `archived_at = now()`. The active board (`GET /api/tasks/board`) excludes archive entirely; cancelled tasks surface in `GET /api/tasks/archive?include_cancelled=true`.

---

## 3 · Trajectory

The trajectory replaces the v0.2 admission gate, the `required_reviews` + `ship_required` route hints, and the `complexity` column.

### 3.1 Shape

`tasks.trajectory` is a JSON list of `{stage, to, focus?}` objects:

```json
[
  {"stage": "plan",            "to": "p5"},
  {"stage": "execute",         "to": "p2"},
  {"stage": "audit_syntax",    "to": ["p4", "p7"]},
  {"stage": "audit_semantics", "to": "p9",
   "focus": "Verify the rule-3a derivation matches the wiki entry on multiway causal foliation; check brand naming on user-facing strings."},
  {"stage": "ship",            "to": "p2"}
]
```

- `stage` ∈ `{plan, execute, audit_syntax, audit_semantics, ship}` (no `archive` — it is implicit/terminal).
- `to` accepts a single Player slot string (hard-assign) or a list of slot strings (pool; first free wins).
- `focus` (optional, audit stages only) is a free-text string Coach uses to name **what the auditor should check** (math invariants? brand voice? race conditions? specific acceptance criteria?). REQUIRED for `audit_semantics`; optional for `audit_syntax` (defaults to "match the contract and verify internal soundness"). Ignored on non-audit stages. See §4.6 for the full framing.

### 3.2 Validation rules

`_validate_trajectory()` in [server/tools.py](server/tools.py) enforces:

- Non-empty list.
- Each `stage` is one of the five valid stages above.
- No duplicate stages.
- Stages appear in canonical order (`plan` < `execute` < `audit_syntax` < `audit_semantics` < `ship`).
- `execute` is mandatory.
- Each `to` resolves to a `list[str]` of valid Player slots (`p1`..`p10`).
- `focus` (optional) must be a string when present; ignored on non-audit stages with no error so a Coach paste-mistake doesn't bounce the whole call. **`audit_semantics` entries with no `focus` AND a non-empty `to` are rejected** at `coord_create_task` / `coord_set_task_trajectory` time — semantic audits without a stated focus are noise. An empty-pool semantic stage (`{"stage":"audit_semantics","to":[]}`) is allowed; the focus enforcement then happens at `coord_assign_auditor` time when Coach actually names the auditor.

The validator returns `(trajectory_json, role_inserts)` so `coord_create_task` can plant the matching `task_role_assignments` rows in the same transaction. Each role insert carries the entry's `focus` (when present) into the new `task_role_assignments.focus` column (§12.1).

### 3.3 Examples

| Use case | Trajectory |
|---|---|
| Quick mechanical work, "simple" | `[{"stage":"execute","to":["p2","p3"]}]` |
| Needs a spec but no audit | `[{"stage":"plan","to":"p5"},{"stage":"execute","to":"p2"}]` |
| Code change with formal review | `[{"stage":"plan","to":"p5"},{"stage":"execute","to":"p2"},{"stage":"audit_syntax","to":"p4","focus":"race conditions in the new lock path"},{"stage":"ship","to":"p2"}]` |
| Marketing blog post | `[{"stage":"plan","to":"p3"},{"stage":"execute","to":"p5"},{"stage":"audit_semantics","to":"p7","focus":"brand voice + claims accuracy against project-objectives.md"},{"stage":"ship","to":"p4"}]` |

### 3.4 Spec gate

If `plan` is in the trajectory, the executor cannot start until `spec_path IS NOT NULL`. The planner's `coord_write_task_spec` call sets `spec_path`, marks the planner role complete, and triggers `plan → execute`. If `plan` is **not** in the trajectory, no spec is required — Coach signaled "no plan needed" by omitting the stage. `coord_write_task_spec` is callable as a documented emergency override regardless.

### 3.5 Self-audit reminder (trajectory-driven)

If the trajectory has no `audit_syntax` and no `audit_semantics` stage after `execute`, the executor's wake prompt includes the verbatim:

> *no audit stage configured for task &lt;task_id&gt; — self-audit (run tests / sanity-check the change) before coord_commit_push or coord_complete_execution. The board archives directly on completion; there is no separate review pass.*

If at least one audit stage is configured, the reminder is omitted (a downstream reviewer is the gate).

### 3.6 Workflow metadata

`tasks.workflow` is one of `code`, `research`, `writing`, `marketing`, `ops`, `generic`. It shapes prompt wording (e.g., "merge the PR" vs "publish the post"), but it does **not** drive routing — the trajectory is the only routing source.

### 3.7 Tracking reason

`tasks.tracking_reason` is optional informational metadata. The v0.2 enum requirement on Coach top-level tasks is removed in v0.3: every Coach delegation goes through kanban, so there is nothing to gate.

### 3.8 Mid-flight reroute

`coord_set_task_trajectory(task_id, trajectory)` (Coach-only) rewrites the trajectory while a task is in flight. **Cannot remove any stage the task has already entered** — the validator walks the OLD trajectory up to and including the current stage and rejects the call when any of those stages are missing from the new trajectory. (E.g. a task currently in `audit_semantics` cannot drop the already-passed `audit_syntax` stage.) Removed-stage role rows are deactivated by setting `completed_at = now()` (the active-row filter is `completed_at IS NULL AND superseded_by IS NULL` — `completed_at` deactivates without violating the `superseded_by` self-FK that fires under `PRAGMA foreign_keys = ON`). Added-stage rows are inserted fresh. Emits `task_trajectory_changed`. The HTTP sibling is `POST /api/tasks/{id}/trajectory` with the same validator + storage shape.

`coord_assign_planner / auditor / shipper / task` are the per-stage candidate-list edits. v0.3 behavior: insert a fresh `task_role_assignments` row, supersede any prior active row for the same `(task_id, role)` via `superseded_by = <new_row_id>` (so the board's "first active matching row" pick stays correct), and mirror the new candidate list back into `tasks.trajectory.to` for the matching stage (so the stored trajectory + Coach prompt + UI marker stay in sync with the role rows). Re-wake fires only when the role's stage is the task's current stage; future-stage reservations are stored but inactive.

### 3.9 Compass auto-audit

Compass auto-audit fires on `commit_pushed` regardless of trajectory shape (see [compass-specs.md §5.5](compass-specs.md)). Its verdict is **informational** — written to `tasks.compass_audit_verdict` + `tasks.compass_audit_report_path` for the card pip, never advancing or reverting a stage. The assigned Player reviewer is the gate.

---

## 4 · Roles

### 4.1 Strict separation

- **Coach** plans (writes the spec) and delegates everything else. Coach never executes, audits, or merges.
- **Players** execute, review, and ship. Coach can also delegate planning to a Player via `coord_assign_planner`.

### 4.2 Role inventory

| Role | Stage entered | Tool that completes the role |
|---|---|---|
| `planner` | plan | `coord_write_task_spec` (sets the row's `completed_at`) |
| `executor` | execute | `coord_commit_push(task_id=...)` for code, or `coord_complete_execution(task_id=...)` for non-code artifacts |
| `auditor_syntax` | audit_syntax / Formal Review | `coord_submit_audit_report(kind='syntax'|'formal', ...)` |
| `auditor_semantics` | audit_semantics / Semantic Review | `coord_submit_audit_report(kind='semantics'|'semantic', ...)` |
| `shipper` | ship | `coord_mark_shipped(task_id, note?)` |

### 4.3 Single-Player vs pool assignment

`coord_assign_task` accepts `to` as either a string (`'p3'` — hard-assign) or a comma-list (`'p1,p2,p3'` — executor pool). Executor pool-form leaves `tasks.owner` NULL and keeps the task in `plan` until an eligible Player calls `coord_claim_task` or `coord_accept_role(role='executor')`; all eligible Players are called at post time. Atomic UPDATE on claim ensures only one wins; losers' wakes see the row already claimed and degrade gracefully.

Planner / reviewer / shipper assignment tools also accept comma-list calls. For those roles the row stays `owner=NULL` until the first eligible Player calls `coord_accept_role(task_id, role)`. The completion tools still validate `owner = caller`, which is exactly what `coord_accept_role` sets. Future-stage rows are reservations only: they are not shown by `coord_my_assignments`, not woken by the idle poller, and not valid for completion until the card reaches the matching stage.

### 4.4 Reviewer-equals-executor warning

`coord_assign_auditor` does **not** block the case where the reviewer matches the executor. It emits an `audit_self_review_warning` event (rendered in Coach's pane and forwarded to Telegram) so the human can spot weak self-review patterns. Useful when the team is small or specialists are scarce; not desirable on big-stakes tasks.

### 4.5 Idle-Player polling

A periodic loop (default 5 min, see [server/idle_poller.py](server/idle_poller.py)) wakes idle Players who have current-stage pool work or current-stage hard-assigned pending roles but are not currently running (e.g. their initial wake landed while they were over-cap; the harness was paused; the Player ignored the wake). Future-stage reservations are deliberately invisible. See §10 for full design.

### 4.6 Audit framing — what auditors actually check

The two auditor stages answer different questions and read different sources. Conflating them produces rubber-stamps or noise.

#### 4.6.1 `auditor_syntax` — contract-bound

Question: **does the deliverable match what was asked, and is it internally sound?** (No bugs, no inconsistencies, the diff does what the contract says.)

The auditor's wake prompt builds a `## Contract` block by cascading whatever rungs exist (priority order, present rungs are concatenated with section headers):

1. **`spec.md`** — the planner's binding contract (when `plan` is in the trajectory and `spec_path` resolves to a readable file).
2. **Task title + description** — always present (Coach sets them on `coord_create_task`).
3. **Executor's wake prompt** — what the executor was actually told to do. Pulled from the executor's role row by reverse-walking the most recent `agent_started` for the executor on this task. Best-effort; absent rung means absent block.
4. **Commit message / artifact summary** — what the executor said they did. Pulled from the latest `commit_pushed` (commit message body) or `task_execution_completed` (`summary` field).

A task always has at least #2 by construction, so syntax audit never has zero context — it just has weaker context when no plan stage ran. This is the explicit fix for the production stall where p8 stopped because `spec.md` was missing on a recovery task that legitimately inherited a sibling's spec: the auditor now audits against title + description + commit when no spec exists, not against nothing.

`focus` is OPTIONAL on syntax audits. Default focus when Coach didn't specify: *"Match the contract above; verify internal soundness (no bugs, no inconsistencies, no broken interfaces)."* Coach can sharpen for high-stakes work, e.g. `focus="race-condition review of the new locking path; ignore unrelated style drift."`

#### 4.6.2 `auditor_semantics` — context-bound (NOT spec-bound)

Question: **does this deliverable make sense in the world this project lives in?** (Math correct? Brand voice intact? Domain terminology right? Aligned with where the project is heading?)

The semantic auditor's wake prompt builds a `## Project context` block from:

1. **Coach's stated focus** (REQUIRED — see §4.6.3 below). The auditor's reading lens.
2. **Compass** — the lattice of project intent. Wake prompt names the four read tools (`compass_ask`, `compass_audit`, `compass_brief`, `compass_status`) and instructs the auditor to call `compass_ask("does <artifact> align with <focus>?")` as a first-pass sanity check before reading raw sources. **Note**: only Coach can call Compass MCP tools per [compass-specs.md §6](compass-specs.md). The semantic auditor reads the Compass-derived block already injected into every project's `CLAUDE.md`, plus `tasks.compass_audit_report_path` when the auto-audit fired on the executor's commit. Direct `compass_ask` is a Coach-only escalation path — auditor messages Coach to run it on their behalf if the CLAUDE.md block is insufficient.
3. **Truth corpus** — the auditor reads `<project>/truth/**/*.{md,txt}` + `<project>/project-objectives.md` (binding constraint layer per [compass-specs.md §1.4](compass-specs.md)).
4. **Wiki** — `/data/wiki/<project_id>/**/*.{md,txt}` (gotchas, glossary, stakeholder preferences, domain rules).
5. **Compass auto-audit report** — `tasks.compass_audit_report_path` when set (the lattice's already-recorded verdict on this artifact).

`spec.md` is **supplementary** for semantic audits, not binding. The wake prompt names it as "background — what was meant to be built" but the audit verdict must judge against the world (truth/wiki/intent), not against the planner's interpretation. A spec that drifted from project intent is a bug the semantic auditor must catch.

#### 4.6.3 `audit_focus` discipline (Coach must name the check)

Coach must articulate **what the audit is actually checking** when assigning an auditor. Without a stated focus, semantic audits are noise: the auditor either invents one (drifts) or rubber-stamps (false pass). Examples:

- ❌ Bad: "Run a semantic audit on this commit."
- ✓ Good: `focus="Verify the rule-3a derivation in the new module matches the multiway causal foliation entry in the wiki; check that the user-facing labels use 'foliation' not 'slicing' per the glossary."`
- ❌ Bad: "Formal review on the lock change."
- ✓ Good: `focus="Race-condition review on the new lock path in server/foo.py:acquire — particularly the timeout fallback. Ignore unrelated style drift."`

Enforcement:

- `coord_assign_auditor(kind='semantics', focus='')` is **rejected** at the tool with a clear error: *"semantic audits require a focus — name the check (e.g. 'verify math derivation matches glossary'). Auditors with no focus rubber-stamp."*
- `coord_assign_auditor(kind='syntax', focus='')` is **accepted**; the wake prompt uses the default focus.
- A trajectory entry for `audit_semantics` with no `focus` AND a non-empty `to` is rejected by `_validate_trajectory` (see §3.2). An empty-pool semantic stage is allowed; the focus enforcement happens at `coord_assign_auditor` time when Coach actually names the auditor.
- `focus` is stored on the new `task_role_assignments.focus` column (§12.1). When `coord_assign_auditor` re-assigns a role (supersedes the prior row), the new `focus` overrides; if Coach omits `focus` on a syntax re-assign, the prior row's `focus` is inherited so a quick re-assignment doesn't lose Coach's earlier framing.

The wake prompt always renders `## Focus` first so the auditor reads the lens before the sources. Wake-prompt construction is centralised in [server/kanban.py:_build_auditor_wake_body](server/kanban.py) and called from both `coord_assign_auditor` (initial assignment wake) and `_wake_role_or_emit_needed` (stage-entry wake when Coach reserved the role earlier).

---

## 5 · Artifacts

### 5.1 spec.md

Produced in the plan stage. Markdown document, full overwrite each time `coord_write_task_spec` (or `POST /api/tasks/{id}/spec`) is called — rolling history lives in the event stream + git.

Path: `/data/projects/<project_id>/working/tasks/<task_id>/spec.md`. Synchronously mirrored to kDrive at `TOT/projects/<project_id>/tasks/<task_id>/spec.md`.

Helper: [server/tasks.py:write_task_spec](server/tasks.py).

**Spec file shape** — `coord_write_task_spec` and `POST /api/tasks/{id}/spec` require a non-empty markdown body (max 40k chars). The helper prepends YAML frontmatter and then writes the body verbatim. Recommended body shape:

```markdown
---
task_id: t-2026-05-03-abc12345
title: <task title>
created_by: human | coach | p3
created_at: 2026-05-03T14:22:11Z
priority: urgent | high | normal | low
complexity: standard | simple
spec_author: coach | p3
spec_written_at: 2026-05-03T14:23:00Z
---

# <task title>

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

The helper prepends YAML frontmatter (`task_id`, `audit_kind`, `audit_round`, `auditor`, `verdict`, `submitted_at`) and writes the auditor's markdown body verbatim.

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

Cards render `[spec]`, `[audit (kind, verdict)]`, and `[compass]` links via the existing `data-harness-path` mechanism (see [server/static/app.js](server/static/app.js)'s document-level click listener). Clicking opens the `__files` pane and longest-prefix-matches the absolute path against `/api/files/roots`. Audit round is visible in the report filename and in the expansion/history rows, not in a stored `latest_audit_round` field.

---

## 6 · MCP tools

All new tools are registered in [server/tools.py](server/tools.py)'s `_tools` map and `ALLOWED_COORD_TOOLS` set. Coach-only enforcement uses the existing `_require_coach` helper.

### 6.1 Coach-only — assignment + meta

| Tool | Params | Purpose |
|---|---|---|
| `coord_set_task_trajectory` | `task_id, trajectory` | **New.** Mid-flight reroute. Validates against the OLD trajectory walked up to the current stage — rejects removing any **already-entered** stage (not just the current one). Removed-stage role rows are deactivated by setting `completed_at = now()` (the active-row filter is `completed_at IS NULL AND superseded_by IS NULL`; we don't use `superseded_by = -1` because the column is a self-FK and that violates the constraint under `PRAGMA foreign_keys = ON`). Added-stage rows are inserted with the entry's `focus` (when present) populating `task_role_assignments.focus`. The `audit_semantics`-needs-focus rule (§3.2) applies on edits too: a non-empty-pool semantic stage with no `focus` is rejected before any DB write. **v0.3.6 stand-down:** any displaced assignees from removed stages OR from in-place `eligible_owners` shrinkage on a remaining stage receive a stop-work wake (`task_role_stand_down` event). Emits `task_trajectory_changed`. |
| `coord_assign_planner` | `task_id, to` | Inserts a fresh `planner` role row, supersedes any prior active row for the same `(task_id, role)` via `superseded_by = <new_row_id>`, and mirrors the new candidate list back into `tasks.trajectory.to` for the matching stage. **v0.3.6:** displaced prior assignee receives a stand-down wake (filtered for same-slot refresh). Hard-assign wakes the owner if the task is still in `plan`; pool-form calls candidates who must `coord_accept_role`. |
| `coord_assign_task` | `task_id, to` | Inserts a fresh `executor` role row, supersedes any prior active executor row, and mirrors the new candidate list back into `tasks.trajectory.to`. Spec gate enforced when `plan` is in the trajectory and `spec_path` is unset. Stamps `last_stage_change_at` + clears `stale_alert_at` on the plan→execute transition. **v0.3.6:** displaced prior executor receives a stand-down wake (covers both pool-form and hard-assign paths). Auto-wake prompt is trajectory-aware (executors with no audit stage configured get the self-audit reminder verbatim). |
| `coord_assign_auditor` | `task_id, to, kind, focus?` | `kind ∈ {'formal'/'syntax', 'semantic'/'semantics'}`. Same supersede + trajectory-mirror + v0.3.6 stand-down behavior as `coord_assign_planner`. **`focus` (free-text)** names what the auditor should check; **REQUIRED for `kind='semantics'`** (rejected with error if empty), optional for `kind='syntax'` (defaults to "match the contract and verify internal soundness"). Stored on the new `task_role_assignments.focus` column and rendered at the top of the auditor's wake prompt as `## Focus` before `## Contract` (syntax) / `## Project context` (semantics). On re-assign without `focus`, inherits from the prior superseded row so Coach doesn't lose framing on a quick reassignment. See §4.6 for the full discipline. Emits `audit_self_review_warning` if reviewer == executor. Future-stage reservations are not woken until active. |
| `coord_assign_shipper` | `task_id, to` | Same supersede + trajectory-mirror + v0.3.6 stand-down behavior. Future-stage reservations wake only when the task enters `ship`. |
| `coord_set_task_workflow` | `task_id, workflow?, tracking_reason?` | Sets the workflow / tracking-reason metadata. Routing has moved to trajectory; this tool no longer accepts review/ship knobs. |
| `coord_advance_task_stage` | `task_id, stage, note?` | Explicit override. Bypasses the trajectory gate. |

### 6.2 Coach + planner + owner — spec authoring

| Tool | Params | Purpose |
|---|---|---|
| `coord_write_task_spec` | `task_id, body, on_behalf_of?` | Writes `spec.md`. Permission: Coach, the active planner, the executor, or the owner of the task's parent. Sets `spec_path` + `spec_written_at`; marks the planner role row complete. **`on_behalf_of` is the v0.3.5 Coach-only override** for when an assigned planner's runtime can't reach this tool: Coach reads the Player's on-disk `spec.md`, copies the body in, submits with `on_behalf_of='<player_slot>'`. Recorded `spec_author` is the named Player; that Player's planner role row is the one marked complete (so the spec gate releases properly); bus event `agent_id` is Coach. Rejects when the named Player has no active planner role on the task — Coach must fix the assignment first via `coord_assign_planner`. Mirror of `coord_submit_audit_report(on_behalf_of=...)`. |

### 6.3 Player-only — role artifacts + introspection

| Tool | Params | Purpose |
|---|---|---|
| `coord_my_assignments` | (none) | Returns only current actionable work: active executor task, pending planner/reviewer/shipper roles whose stage is active, and current-stage eligible pools. Future reservations are hidden. |
| `coord_accept_role` | `task_id, role?` | Player answers a current-stage role call. Atomic first-claim wins; losers get a stale/already-accepted error. **Response includes a role-specific next-step hint** (v0.3.3) naming the matching completion tool with `task_id` baked in: planner → `coord_write_task_spec`, executor → `coord_commit_push` / `coord_complete_execution`, auditor → `coord_submit_audit_report`, shipper → `coord_mark_shipped`. Mirror of the kanban subscriber's stage-entry wake — gives the Player the right tool name in the synchronous accept response, not just on the next turn. |
| `coord_complete_execution` | `task_id, summary, artifact_path?, completion_kind?` | Non-git execution completion for research, writing, marketing, ops, or no-diff artifacts. Marks executor row complete and emits `task_execution_completed`. |
| `coord_submit_audit_report` | `task_id, kind, body, verdict, on_behalf_of?` | Validates active current-stage reviewer role for caller. Writes the review `.md`, marks the role row complete with verdict, emits `audit_report_submitted`. **`on_behalf_of` is the v0.3.4 Coach-only override** for when an assigned auditor's runtime can't reach this tool: Coach reads the Player's on-disk `audit_*.md`, copies the body in, and submits with `on_behalf_of='<player_slot>'`. The role-row owner / `auditor_id` is the named Player (so the audit history reads correctly); the bus event's `agent_id` is Coach (so the timeline shows who actually pressed the button). Player-callable path unchanged. Coach without `on_behalf_of` is rejected with the standard "Coach doesn't audit" message — the explicit override is the only Coach path. |
| `coord_mark_shipped` | `task_id, note?` | Validates active current-stage `shipper` role. Marks complete + emits `task_shipped`. |
| `coord_claim_task` | `task_id` | **Modified.** Executor-only claim path. Validates plan-stage task, standard-task spec gate, and executor-pool `eligible_owners` membership when an executor pool row exists. Atomic UPDATE. Sets `tasks.owner` + `claimed_at = started_at = now()` and claims/inserts the active executor role row. **Response includes the next-step hint** (v0.3.3) naming `coord_commit_push(task_id=...)` / `coord_complete_execution(task_id=...)` with the actual `task_id` baked in plus the "kanban does NOT advance until you call one of those" reminder. |

### 6.4 Owner + Coach

| Tool | Params | Purpose |
|---|---|---|
| `coord_set_task_blocked` | `task_id, blocked, reason?` | Toggles the orthogonal flag. |

### 6.5 Modified existing tools

- `coord_create_task` accepts `trajectory` (new in v0.3 — list of `{stage, to, focus?}` objects, validated by `_validate_trajectory()`), `workflow`, `tracking_reason` (now optional). The `complexity`, `required_reviews`, and `ship_required` params are **removed**. When `trajectory` is provided, the tool plants the matching `task_role_assignments` rows in the same transaction, including each entry's `focus` when present (§4.6, §12.1). A non-empty-pool `audit_semantics` entry without `focus` is rejected at create time (the validator catches it before any DB write).

  **Initial activation (v0.3).** The new task's `tasks.status` is set to the trajectory's **first stage** (not the schema default `plan`). When the first stage carries a single hard-assignee, `tasks.owner` is hard-set in the same insert. Immediately after `task_created` is published, the tool emits `task_stage_changed` with `from=null` → `to=<first_stage>` so the subscriber's `_on_stage_changed` handler wakes the first-stage assignee. Without this, an execute-only trajectory like `[{"stage":"execute","to":"p2"}]` would sit silently in `plan` behind the spec gate. The default trajectory when omitted is `[{"stage":"execute","to":[]}]` — Coach can follow up with the assign tools, or the idle-poller will pick up the unassigned pool. The tool does **not** write `spec.md`; planner Players or the human do that.
- `coord_commit_push` accepts optional `task_id`. When provided, it validates that the caller owns that active execute-stage task and has an uncompleted executor role row. On a successful push, or on explicit `push=false` local-only mode, the emitted `commit_pushed` event carries the `task_id` and the executor role row is marked complete. If `git push` was requested and fails, `task_id` is cleared from the event and the executor role remains open so the kanban cannot advance on an unpushed commit.

  **Auto-bind (v0.3.2, hardened in v0.3.3).** When `task_id` is omitted, the tool looks up the caller's active executor tasks in the current project. The lookup requires **both** `tasks.owner = caller` AND a LIVE `task_role_assignments` row (`completed_at IS NULL AND superseded_by IS NULL`) for the executor role — without the role-row guard, a task whose executor row was completed by a prior commit but never advanced (subscriber crash, lost event) would be picked, then the downstream validator would reject with "no active uncompleted executor role" immediately after silent auto-binding. **Exactly one match → auto-bind**: the commit drives the kanban as if `task_id` had been passed, the response tells the Player which task was bound and asks them to pass `task_id=` explicitly next time. **Zero matches** (the caller has no active executor task) → the commit still happens (legitimately useful for scratch work) but a `commit_without_task_id_warning` event is published routed to Coach (`to: 'coach'`) so the gap is visible, and the Player's response includes "Coach has been notified." Multiple matches isn't possible since `tasks.owner` is 1:1. Compass auto-audit fires regardless of binding state. The `commit_pushed` event grows a `task_id_auto_bound: bool` field so the dashboard can distinguish auto-bound from explicit commits. Auto-bind is suppressed when `git push` was requested and failed (the push failure itself is the louder signal; no warning event fires either).

  **Misplaced-work detection (v0.3.7).** When `git status --porcelain` reports the slot's worktree clean, the tool does NOT immediately return the legacy `"nothing to commit (working tree clean)"` soft-OK. Instead it peeks `BASE_REPO_PATH` (`/workspaces/.project`, the shared seed checkout used to provision per-slot worktrees on boot — see [server/workspaces.py](../server/workspaces.py)) via a separate `git status --porcelain` (15s timeout, scrubbed env, `subprocess.run` in a thread). If the seed checkout is dirty, the tool returns a loud named error: "your worktree at `<slot>/project` is clean, but the shared seed checkout at `/workspaces/.project` has uncommitted changes. The shared checkout is not yours to commit from — per-worktree isolation is mandatory." The error names both paths and gives the fix path (`cd <slot worktree> and re-apply`, or `git -C <base> stash && git -C <slot> stash pop`). When `BASE_REPO_PATH` doesn't exist or has no `.git` directory (deploys without the seed checkout), the legacy soft-OK is preserved. Production trace 2026-05-04: p8 wrote to `.project` instead of `/workspaces/p8/project`, hit the legacy soft-OK, marked the task blocked. The new error makes that failure mode self-diagnosing.
- `coord_update_task` validates against the new transition map. Legacy aliases (`done` → `archive`, `cancelled` → `archive` + `cancelled_at`) are accepted for one release.

### 6.6 Removed tools

- `coord_set_task_complexity` — column gone, tool gone. Same routing decision is now expressed by passing or rewriting the trajectory.

---

## 7 · HTTP endpoints

All under `/api/tasks` or `/api/tasks/*`, gated by `HARNESS_TOKEN`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tasks` | Legacy/list view endpoint for tools and older UI paths. Optional `status` and `owner` filters; returns rows for the active project ordered newest-first. |
| POST | `/api/tasks` | Human task composer. Body `{title, description?, parent_id?, priority?, workflow?, tracking_reason?, trajectory?}`. Creates a top-level or child task in `plan` with `created_by='human'`. The composer omits `trajectory` for default `[{"stage":"execute","to":[]}]`. |
| GET | `/api/tasks/board` | Active 5 buckets (`plan` / `execute` / `audit_syntax` / `audit_semantics` / `ship`), priority-sorted then by `created_at`. Each card includes its active role-assignment list. **No archive.** |
| GET | `/api/tasks/archive` | Paginated archive view. Query params: `limit` (default 50, max 200), `offset`, `q` (text search title + description), `include_cancelled` (default false). |
| GET | `/api/tasks/flow_health` | **New.** Returns `{stages: {<stage>: {count, oldest_stage_change}}, stalled_count, subscriber_last_event_at, subscriber_alive}`. Lets the human inspect "is the engine actually moving" without scraping events. |
| GET | `/api/tasks/{id}/assignments` | Full role-assignment history for one task (every row, not just active). Used by the card-expansion view to render the audit-loop history. |
| POST | `/api/tasks/{id}/stage` | Body `{stage, note?, force?: bool}`. Human override. `force=true` bypasses the role-completion gate. |
| POST | `/api/tasks/{id}/cancel` | Human cancellation. Idempotently moves any non-archive task to `archive`, sets `completed_at`, `archived_at`, `cancelled_at`, clears the owner's `current_task_id`, and emits both `task_stage_changed` and legacy `task_updated`. |
| POST | `/api/tasks/{id}/trajectory` | **New.** Body `{trajectory}`. Human-side equivalent of `coord_set_task_trajectory`. |
| POST | `/api/tasks/{id}/workflow` | Body `{workflow?, tracking_reason?}`. Routing has moved to the trajectory endpoint; this is metadata-only. |
| POST | `/api/tasks/{id}/blocked` | Body `{blocked: bool, reason?}`. |
| POST | `/api/tasks/{id}/spec` | Body `{body: <markdown>}`. Same effect as `coord_write_task_spec`. |
| POST | `/api/tasks/{id}/assign` | Body `{role, to}` where `to` is a slot string or list of slot strings. Human-side equivalent of the Coach assignment tools; refuses archived tasks. For auditor roles, a new assignment supersedes prior completed failed rounds of the same kind. |

**Removed**: `POST /api/tasks/{id}/complexity` (column gone). The `complexity` field on `POST /api/tasks` is also gone.

---

## 8 · Events

All published via the existing `EventBus` ([server/events.py](server/events.py)). New types:

| Type | Payload |
|---|---|
| `task_stage_changed` | `{ts, agent_id, type, task_id, from, to, reason: 'commit_pushed'|'audit_pass'|'audit_fail'|'shipped'|'manual', note?, owner}` |
| `task_trajectory_changed` | `{ts, agent_id, type, task_id, trajectory, to: owner}` |
| `task_blocked_changed` | `{ts, agent_id, type, task_id, blocked, reason?, to: owner}` |
| `task_spec_written` | `{ts, agent_id, type, task_id, spec_path, to: owner, on_behalf_of?}` — `agent_id` is the actor (the planner Player in normal use, Coach when overriding); `on_behalf_of` is the Player slot when Coach used the v0.3.5 override path; absent / null otherwise. (§6.2) |
| `task_role_assigned` | `{ts, agent_id, type, task_id, role, eligible_owners, owner?, to: owner}` |
| `task_role_stand_down` | `{ts, type, task_id, role, displaced: list[slot], new_owners: list[slot], to: 'coach'}` — fired by `send_role_stand_down` (v0.3.6) whenever a `task_role_assignments` row gets superseded or its `eligible_owners` shrinks, naming the slots that are no longer credited. The displaced Players each receive a wake with an explicit "STOP work on task X" body before the new owner is woken. Same-slot refresh is filtered (no event, no wake). |
| `task_completed` | `{ts, agent_id: 'system', type, task_id, title, trajectory, trajectory_marker, from_stage, reason, executor, last_stage_owner, owner, to: 'coach'}` — fired by `_transition` (v0.3.9) when a trajectory wraps via natural completion (`reason ∈ {'shipped', 'commit_pushed', 'task_execution_completed', 'audit_pass'}`). Coach is woken with an explicit "send a summary of the outcome to the user" prompt. NOT fired for `reason='manual'` (Coach forced) or `reason='auto_archive_stalled'` (rung-4 — `human_attention` already covers it). Rung 4 bypasses `_transition` entirely so this event is by construction tied to natural completions only. |
| `task_role_called` | `{ts, agent_id: 'system', type, task_id, role, owner?, eligible_owners, to?}` — fired when a stage becomes active and the reserved owner/candidates are woken. |
| `task_role_claimed` | `{ts, agent_id, type, task_id, role, owner, to: owner}` — fired by `coord_accept_role` when a Player wins a role call. |
| `task_claimed` | `{ts, agent_id, type, task_id}` — legacy executor-specific event fired by `coord_claim_task` and executor `coord_accept_role`. |
| `task_role_completed` | `{ts, agent_id, type, task_id, role, owner, artifact_path?, verdict?, to: owner}` |
| `task_execution_completed` | `{ts, agent_id, type, task_id, summary, artifact_path?, completion_kind?, to: executor}` |
| `task_workflow_set` | `{ts, agent_id, type, task_id, workflow, tracking_reason?, to: owner}` |
| `audit_report_submitted` | `{ts, agent_id, type, task_id, kind, verdict, report_path, round, auditor_id, to: <executor>, on_behalf_of?}` — `agent_id` is the actor (the auditor in normal use, Coach when overriding); `auditor_id` is always the effective auditor (the assigned Player). `on_behalf_of` is set to the Player slot when Coach used the v0.3.4 override path; absent / null otherwise. (§6.3) |
| `audit_self_review_warning` | `{ts, agent_id, type, task_id, kind, auditor_id, executor_id}` |
| `audit_fail_notification` | `{ts, agent_id: 'system', type, task_id, kind, kind_round, escalate, auditor_id, executor_id, report_path, to: 'coach'}` — fired by the kanban subscriber on every audit fail, in addition to the executor revert. `kind_round` counts fails of this kind only. `escalate=True` when `kind_round >= 2` (Coach should intervene). |
| `stage_assignment_needed` | `{ts, agent_id: 'system', type, task_id, role, stage, to: 'coach', owner?}` — fired when a stage becomes active and no active role row exists. Renamed from `audit_assignment_needed`; back-compat alias retained for one release. |
| `task_shipped` | `{ts, agent_id, type, task_id, shipper_id, note?, to: <executor>}` |
| `task_stage_stale` | `{ts, agent_id: 'system', type, task_id, stage, age_seconds, owner?, task_executor?, eligible_owners, to: 'coach'}` — fired by the stall sweeper when a non-archive task's `last_stage_change_at` exceeds `HARNESS_KANBAN_STALL_SECONDS`. **v0.3.4: `owner` is the current-stage assignee** (the actual blocker — auditor when stuck in `audit_*`, shipper when stuck in `ship`, executor when stuck in `execute`, etc.), NOT `tasks.owner`. The original executor is preserved separately as `task_executor` so Coach has full context without misattribution. (§10.5) |
| `idle_player_woken` | `{ts, agent_id: <slot>, type, reason, task_id?}` |
| `commit_without_task_id_warning` | `{ts, agent_id: 'system', type, committer, sha, message, to: 'coach'}` — fired by `coord_commit_push` when the caller commits without `task_id` AND has no active executor task to auto-bind. Surfaces the "Player committed scratch when they should have used a task" failure mode to Coach. (v0.3.2) |

**Removed events**:
- `task_complexity_set` — column gone, event gone.

The existing `commit_pushed` event now has optional `task_id`, `pushed`, and `push_requested` fields. `task_id` is omitted/null when a requested push fails so the subscriber cannot advance a task whose commit never reached the remote; explicit `push=false` keeps `task_id`.

The legacy `task_updated` event keeps firing for back-compat. `/api/events` SQL filter extended to fan-out the new types via the `payload_to` / `payload_owner` indexed branches.

---

## 9 · Auto-advance subscriber

[server/kanban.py](server/kanban.py). Started in `lifespan` next to `start_audit_watcher` / `start_telegram_bridge`. Subscribes synchronously **before** scheduling its consumer task to avoid losing events fired during the create_task race window — same pattern as the Compass audit watcher.

### 9.1 Trigger table

| Event | Action |
|---|---|
| `commit_pushed` with `task_id`, stage=execute | Route via `_next_stage(task, "execute")` — walks `tasks.trajectory` to find the next stage. If `execute` is the last entry, archives directly. Stamps `(project_id, sha) → task_id` in the per-project commit cache so a later `compass_audit_logged` can correlate. |
| `task_execution_completed`, stage=execute | Same route as `commit_pushed`, but for non-git artifacts. |
| `task_spec_written` (stage=plan, trajectory has `plan`) | Advance `plan → _next_stage(task, "plan")`. Triggered by `coord_write_task_spec` (or the human-side spec endpoint) once `tasks.spec_path` is set and the planner role row is marked completed. Skipped when the task isn't in `plan` (re-spec on an executing task is informational) or when the trajectory has no `plan` stage. |
| `task_stage_changed` entering a non-terminal stage | The transition itself stamps `last_stage_change_at = now()` + clears `stale_alert_at`. The handler then wakes the current-stage owner/candidates (`task_role_called`) or emits `stage_assignment_needed` to Coach if no active row exists. Future-stage reservations are activated here. The same `from=null → to=<first_stage>` event is emitted by `coord_create_task` / `POST /api/tasks` so creation activates the first stage. **Wake prompt names the completion tool with `task_id` baked in (v0.3.2)** — the executor sees the literal `coord_commit_push(message=..., task_id="t-...")` plus the `coord_complete_execution` alternative, plus the SELF-AUDIT reminder when no audit stage follows execute. Other roles get their role-specific completion tool named the same way. The kanban does NOT advance until the assignee calls the named tool. **v0.3.6 verify-first gate:** the hard-assigned wake leads with "BEFORE editing, committing, or publishing anything: call `coord_my_assignments` and confirm task X appears under your active roles with `role=...`. If you do NOT see it, you've been reassigned — STOP and message Coach." Pool-form wakes explicitly warn that doing the role work without an accepted claim earns no kanban credit. |
| `task_role_stand_down` (v0.3.6) — emitted by `send_role_stand_down`, not consumed by the subscriber. | Independent path: any code that supersedes a role row (or shrinks `eligible_owners` on a remaining stage in `coord_set_task_trajectory`) calls the helper to wake the displaced slot(s) with an explicit "STOP work on task X — do not edit, commit, push, or publish anything for this task" message before the new owner is woken. Same-slot refresh is filtered. The event itself routes `to: 'coach'` so the timeline records the boundary, but the kanban subscriber takes no action on it. |

**v0.3.6 escape protocol** (appended to every per-role completion hint via `_TOOL_NOT_VISIBLE_ESCAPE` in [server/kanban.py](../server/kanban.py)): if the named completion tool is **not visible** in the Player's runtime (Codex stdio transport flake, missing MCP, etc.), the Player MUST message Coach immediately via `coord_send_message(to='coach', body='need to deliver task ... but the named coord_* tool is not visible to me')`. Two boundaries are explicit: (1) DO NOT write the deliverable to disk and stop — the kanban will never see it; (2) DO NOT route around the missing tool by using raw `git`/Bash/`Edit` to commit, push, or publish the deliverable yourself — those bypass every kanban guardrail and corrupt the branch the kanban thought belonged to whoever the current assignee actually is. `coord_request_human()` is the human-facing escalation if Coach is also unreachable. The Coach-side overrides are `coord_write_task_spec(on_behalf_of=...)`, `coord_submit_audit_report(on_behalf_of=...)`, and `coord_advance_task_stage(stage=...)` — see §6.2 / §6.3 / §6.1.

**v0.3.7 worktree boundary** (appended to executor wakes only, via `_executor_worktree_boundary(role, slot)` in [server/kanban.py](../server/kanban.py)): each executor wake names the slot's per-worktree path explicitly — "Worktree boundary: your edits MUST land in `/workspaces/<slot>/project` (your own git worktree on branch `work/<slot>`). Do NOT edit `/workspaces/.project` — that is the shared seed checkout used to provision worktrees and belongs to no slot. Editing it strands your work on a tree the kanban can't see; coord_commit_push will report 'nothing to commit' from your own worktree because the changes never reached it." Auditors / planners / shippers don't edit code and get the empty string (no prompt bloat). The corresponding post-hoc detection is in `coord_commit_push` (§6.5). Production trace 2026-05-04 — p8 wrote to `.project`, hit the legacy soft-OK; the wake-time named path + the commit-time loud error close the loop.
| `audit_report_submitted{verdict=pass}` | Route via `_next_stage(task, current_stage)`. |
| `audit_report_submitted{verdict=fail}` | Revert to `execute` (always — independent of trajectory shape). Clear `started_at`. Re-wake executor with the **spec path AND latest audit report path** read fresh from `tasks.spec_path` / `tasks.latest_audit_report_path`. The wake also extracts a `## Failed criteria` (or `## Failed acceptance criteria` / `### Failed criteria`) section verbatim from the audit report when present, capped at 1500 chars. See §2.3 "Audit-fail loop guarantee" for full details. |
| `task_shipped` | Advance `ship → archive` via `_next_stage(task, "ship")` (always archive since `ship` is canonical-last). |
| `compass_audit_logged` | Best-effort update of `compass_audit_*` columns. Correlation prefers the per-project cache (`_recent_commit_per_project[project_id]`) — falls back to the global tail only when the event lacks `project_id`. **No stage change.** |

The subscriber records its `subscriber_last_event_at` (in-memory ISO timestamp) on every event it processes. Read by the `/api/tasks/flow_health` endpoint for "is the engine alive?" visibility.

### 9.2 Feature flag

`HARNESS_KANBAN_AUTO_ADVANCE` (default true). Set false to make the board purely observational.

### 9.3 Failure isolation

Per-event `try/except` so a single bad row doesn't kill the loop. Unrecognized event shapes log + skip.

### 9.4 Compass independence

The subscriber treats `compass_audit_logged` as informational. It writes `compass_audit_*` columns when it can correlate the event to a recent commit, but never moves the task between stages. The live kanban pane refreshes from the `compass_audit_logged` event itself; no separate `task_updated` event is emitted for this mirror write. The Player reviewer is the gate.

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
- `agents.status ∈ {'working', 'waiting'}`
- Player is over their daily cost cap
- `last_idle_wake_at` within debounce window

### 10.4 Telemetry

Emits `idle_player_woken` events with `reason ∈ {'pool_task_available', 'pending_role_assignment'}`.

### 10.5 Stall sweeper — escalation ladder (v0.3.8)

Sibling pass in the same tick loop. After the per-Player wake checks, queries every non-archive, non-blocked task whose `last_stage_change_at` is older than the rung-1 threshold (default 30 min). For each result, walks the four-rung escalation ladder firing each rung the task hasn't yet crossed. Per-rung idempotence is guaranteed by `tasks.stall_escalation_level` (0=fresh, 1=nudged, 2=Coach-notified, 3=auto-reassigned, 4=auto-archived). The level is reset to 0 on every code path that clears `stale_alert_at` (kanban subscriber `_transition`, `coord_update_task`, `coord_advance_task_stage`, `coord_assign_task`, all human-side stage endpoints) — re-stalls start from the bottom.

Resolution of the current-stage assignee, the legacy `task_stage_stale` event, the stage-aware nudge text, and the tool-not-visible escape paragraph are unchanged from v0.3.4 (see rung 1 below). What's new is the ladder structure: when Coach also goes silent, the system auto-reassigns at 2h and auto-archives at 4h instead of looping forever on "wake the same assignee."

#### Rung 1 — nudge the current-stage assignee (default 30 min)
- Resolve the **current-stage assignee** by looking up the live `task_role_assignments` row whose `role` matches the task's `status` (e.g. `auditor_semantics` when `status='audit_semantics'`). This Player is the stall blocker. Falls back to `tasks.owner` only when no live row exists.
- Emit `task_stage_stale` `{task_id, stage, age_seconds, owner: <stage_assignee>, task_executor: <tasks.owner>, eligible_owners, to: 'coach'}`. (Legacy event preserved for back-compat with existing consumers.)
- Wake the stage assignee with the stage-aware nudge: planner→`coord_write_task_spec`, executor→`coord_commit_push`/`coord_complete_execution`, auditor→`coord_submit_audit_report` with the right `kind`, shipper→`coord_mark_shipped`. Nudge ends with the **tool-not-visible escape** + (v0.3.6) the **no-raw-shell** paragraph.
- Stamp `stall_escalation_level = 1` + `stale_alert_at = now()`.

#### Rung 2 — Coach intervention call (default 1 h)
- Emit `task_stall_persisting` `{task_id, stage, age_seconds, owner, task_executor, next_action: 'auto_reassign', next_action_in_min, to: 'coach'}`.
- Wake Coach with: "Stall persisting on task X (stage Y, blocker Z). The Player didn't move on the N-min nudge. Auto-reassign fires in ~M min unless you intervene. Options: nudge them again (`coord_send_message`), reassign yourself (`coord_assign_*`), override (`coord_advance_task_stage`), or archive."
- Stamp `stall_escalation_level = 2`.

#### Rung 3 — auto-reassign (default 2 h)
- Read the active role row's `eligible_owners`. Filter out the stuck owner + any locked Players. If at least one alternative remains, pick the first and:
  - UPDATE the role row's `owner` + `claimed_at`.
  - For executor: also update `tasks.owner` and swap `agents.current_task_id`.
  - Emit `task_stall_auto_reassigned` `{task_id, stage, role, from_owner, to_owner, to: 'coach'}`.
  - Call `_wake_role_or_emit_needed(task_id, role)` so the new owner gets the canonical role-entry wake (verify-first gate, named completion tool, etc.).
  - Call `send_role_stand_down(displaced=[stuck_owner], new_owners=[new_owner])` so the stuck owner gets an explicit "stop work" message.
- If no alternative is reachable (single eligible Player, all alternatives locked, no role row, etc.):
  - Emit `task_stall_no_alternative` `{task_id, stage, reason, to: 'coach'}`.
  - Emit `human_attention` `{subject, body naming the next-archive deadline, urgency: 'high'}`.
- Stamp `stall_escalation_level = 3`.

#### Rung 4 — auto-archive (default 4 h)
- UPDATE `tasks SET status='archive', completed_at=now, archived_at=now, last_stage_change_at=now, stale_alert_at=NULL, stall_escalation_level=0`.
- Release `agents.current_task_id` for the prior owner.
- Emit `task_stage_changed{from=<stage>, to='archive', reason='auto_archive_stalled', note}`.
- Emit `task_stall_auto_archived{task_id, stage_before, age_seconds, to: 'coach'}`.
- Emit `human_attention{subject, body explaining the rung-by-rung path that led here, urgency: 'high'}`.
- The `stall_escalation_level = 0` reset means a re-opened task starts the ladder fresh.

Failure isolation: per-task + per-rung try/except. The level is stamped after each successful rung-fire so a crash mid-walk leaves coherent state — the next sweep picks up where it left off.

| Env var | Default | Meaning |
|---|---|---|
| `HARNESS_KANBAN_STALL_ENABLED` | `true` | Master switch for the sweeper. |
| `HARNESS_KANBAN_STALL_SECONDS` | `1800` | 30 min. Rung 1 — nudge the current-stage assignee. (v0.3.8 halved from 1h.) |
| `HARNESS_KANBAN_ESCALATE_COACH_SECONDS` | `3600` | 1 h. Rung 2 — `task_stall_persisting` + Coach wake. |
| `HARNESS_KANBAN_ESCALATE_REASSIGN_SECONDS` | `7200` | 2 h. Rung 3 — auto-reassign or `human_attention`. |
| `HARNESS_KANBAN_ESCALATE_ARCHIVE_SECONDS` | `14400` | 4 h. Rung 4 — auto-archive + `human_attention`. |

The legacy `HARNESS_KANBAN_STALL_REALERT_SECONDS` is unused in v0.3.8 (the ladder replaces single-fire-with-re-alert).

### 10.6 Reconciliation sweep (v0.3.8)

Sibling pass in the tick loop, after the stall ladder. Read-only — never mutates DB rows. Walks every non-archive task's folder on disk and emits structured events to Coach when an artifact exists but the kanban hasn't recorded it. Catches the recurring "Player did the work but the kanban didn't notice" failure mode that the p1 / p3 / p8 production traces all surfaced.

Two checks per task:

#### Spec unrecorded
- `<project_paths(project_id).working>/tasks/<task_id>/spec.md` exists AND `tasks.spec_path` IS NULL.
- Emits `task_spec_unrecorded` `{task_id, project_id, spec_path: <relative>, planner: <active_planner_owner>, to: 'coach'}`.
- Coach's prompt rollup builds the suggested call `coord_write_task_spec(task_id=..., body=<paste>, on_behalf_of='<planner>')`.

#### Audit unrecorded
- `<task_dir>/audits/audit_<round>_<kind>.md` exists AND no `task_role_assignments` row for the task has `report_path = <relative_path>`.
- Emits `task_audit_unrecorded` `{task_id, project_id, kind, round, report_path: <relative>, auditor: <active_auditor_owner>, to: 'coach'}`.
- Coach's prompt rollup builds the suggested call `coord_submit_audit_report(task_id=..., kind=..., body=<paste>, verdict=..., on_behalf_of='<auditor>')`.

Files that don't match the canonical `audit_<N>_<kind>.md` pattern (drafts, scratch notes, READMEs) are ignored — only `\d+` round + `syntax|semantics` kind triggers a finding.

Per-finding TTL dedupe: `_reconcile_emitted: dict[str, str]` maps `<kind>:<project_id>:<task_id>[:round:audit_kind]` → ISO timestamp. Within `HARNESS_KANBAN_RECONCILE_TTL_SECONDS` (default 1h) of the last emit for a finding key, the sweep skips. Across process restarts the dedupe resets — humans probably want the reminder again after a deploy if the artifact still sits.

| Env var | Default | Meaning |
|---|---|---|
| `HARNESS_KANBAN_RECONCILE_ENABLED` | `true` | Master switch for the sweep. |
| `HARNESS_KANBAN_RECONCILE_TTL_SECONDS` | `3600` | 1 h. Per-finding TTL — Coach isn't spammed every 5 min for the same on-disk artifact. |

---

## 11 · UI surface

### 11.1 LeftRail icon

A CSS-drawn three-column-of-rectangles glyph (`.kanban-icon` in [server/static/style.css](server/static/style.css)). Toggle behavior + persistence via `harness_layout_v1` localStorage key — same shape as `__files` / `__compass`.

### 11.2 Four active columns + Archive drawer

Columns: **Plan / Execute / Review / Ship**. Review fuses `audit_syntax` + `audit_semantics` into one column; the per-card `kbn-stage-label` (`FORMAL`, `SEMANTIC`) preserves the sub-stage signal.

Archive is **not** a column. It opens as an inline drawer above the columns via the toolbar's `Archive ▾` button. Drawer features: search, pagination (50 per page), `show cancelled` toggle, client-side start/end date filters, and localStorage persistence for drawer/filter state (`tot-kanban-archive`).

The pane header renders `Kanban` and the active project id as separate flex children under `.pane-head-label`; CSS keeps an 8px gap so labels like `Kanban dynamichypergraph` never collapse together.

### 11.3 Card content

- **Title** (1–2 lines, truncated).
- **Stage label** (`PLAN` / `EXECUTE` / `FORMAL` / `SEMANTIC` / `SHIP`) — kanban column conveys it visually, but the explicit per-card text is rendered so the stage stays unambiguous when the card is dragged or quoted out of context.
- **Trajectory marker** (`.kbn-trajectory`) — short abbreviation strip rendered top-right or beneath the stage label. Each stage in `tasks.trajectory` is shown as a one- or two-letter token; the **current** stage is highlighted (bold + accent color), past stages are dimmed, future stages are normal-weight. Tokens:

  | Stage | Marker |
  |---|---|
  | `plan` | `P` |
  | `execute` | `E` |
  | `audit_syntax` | `AY` |
  | `audit_semantics` | `AE` |
  | `ship` | `S` |

  Examples: `[E]` for self-audit-only; `[P→E]` for plan-then-execute; `[P→E→AY→S]` for code-with-formal-review; `[P→E→AE→S]` for marketing-blog. The marker gives Coach + the human a glance-readable snapshot of "where this task is going" without expanding the card.
- **Assignee avatar** for the Player driving the work at the current stage (plan → planner if delegated else "coach" chip; execute → executor; review → matching reviewer; ship → shipper). Future-stage reservations do not affect the current avatar or Player assignment queues.
  - **Hollow ring** — assigned, not yet started (`started_at IS NULL`).
  - **Filled ring** — the active assignment row has `started_at` or `completed_at` set. On the active board this mainly appears for executor self-claims; hard-assigned planner/auditor/shipper rows do not currently get a separate "started" stamp before completion.
  - **`pool: N` chip** — stage's role is posted to a pool of N Players and not yet claimed.
  - **Italic `unassigned` chip** — stage's role is needed but no row exists yet. Clickable; opens a quick-assign modal that POSTs to `/api/tasks/{id}/assign`.
  - **`coach` chip** — plan-stage tasks Coach is self-planning (no `planner` role assignment exists).

  Populated avatars are display-only in the shipped pane; opening a Player still happens from the LeftRail.
- **Status flag** badge encoding orthogonal state:
  - `URGENT` (red) — `priority = 'urgent'`.
  - `BLOCKED` (red) — `tasks.blocked = 1`.
  - Nothing when the card is progressing normally.
  - If both urgent and blocked are true, `BLOCKED` wins because it is actionable.
- **Markdown links** — single-click `data-harness-path` opens in the Files pane:
  - `[spec]` — `tasks.spec_path` (always shown when set).
  - `[audit (kind, verdict)]` — `tasks.latest_audit_report_path` (only when `latest_audit_*` is populated; the round is inferred from the filename / role-history rows because no `latest_audit_round` column is stored).
  - `[compass]` — `tasks.compass_audit_report_path` (informational; shown whenever the pointer is set).
- **Drift banner** (red bar) when the card is in `execute` post-audit-fail (driven by `latest_audit_verdict = 'fail'`). The banner links to the latest audit report so Coach + the executor can see *why* the task was reverted at a glance.

The v0.2 `SIMPLE` complexity chip is removed — the trajectory marker (e.g., `[E]`) already encodes "no audit, archives on commit."

Clicking the card body (not a link) expands it inline. Expansion shows: full description, task facts (`id`, created age, blocked reason), and the full role/audit history rendered from `/api/tasks/{id}/assignments`; rows with `report_path` link to their own `audit_<round>_<kind>.md`.

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
  assigned_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  claimed_at      TEXT,
  started_at      TEXT,
  completed_at    TEXT,
  report_path     TEXT,
  verdict         TEXT CHECK(verdict IS NULL OR verdict IN ('pass','fail')),
  superseded_by   INTEGER REFERENCES task_role_assignments(id),
  -- Auditor roles only (NULL on planner/executor/shipper rows). Free-text
  -- focus naming what the auditor should check (math invariants? brand
  -- voice? race conditions? specific acceptance criteria?). Set by
  -- coord_assign_auditor / coord_create_task / coord_set_task_trajectory;
  -- rendered as `## Focus` at the top of the auditor's wake prompt.
  -- REQUIRED for auditor_semantics rows; defaults applied at wake time
  -- for auditor_syntax rows when NULL. See §4.6.
  focus           TEXT
);

CREATE INDEX idx_role_assignments_task ON task_role_assignments(task_id);
CREATE INDEX idx_role_assignments_owner ON task_role_assignments(owner);
CREATE INDEX idx_role_assignments_role ON task_role_assignments(task_id, role);
CREATE INDEX idx_role_assignments_active ON task_role_assignments(task_id, role, superseded_by, assigned_at);
```

The `focus` column is added via `_ensure_columns` in [server/db.py:init_db](server/db.py) — idempotent on re-run, populates existing rows with NULL. CHECK constraints can't be added by ALTER TABLE without a full rebuild, so the "REQUIRED for semantic auditors" rule is enforced at the API layer in `coord_assign_auditor` and `_validate_trajectory` (rejects insert before the row lands).

### 12.2 New tasks columns

| Column | Type | Default | Meaning |
|---|---|---|---|
| `trajectory` | TEXT JSON | `'[{"stage":"execute","to":[]}]'` | Ordered list of `{stage, to}` objects. Drives all routing. |
| `last_stage_change_at` | TEXT | NULL | Stamped by the kanban subscriber on every transition. Drives the stall sweeper. |
| `stale_alert_at` | TEXT | NULL | Stamped by the stall sweeper when it fires `task_stage_stale` for the task. Suppresses re-alerts until the task progresses or 24 h escalation kicks in. |
| `workflow` | TEXT | `'generic'` | `code | research | writing | marketing | ops | generic` — prompt wording, not routing. |
| `tracking_reason` | TEXT | NULL | Optional informational tag. No longer required, no longer enum-validated. |
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

**Removed in v0.3**: `complexity`, `required_reviews`, `ship_required`. All folded into `trajectory`.

### 12.3 Indexes

`idx_tasks_status` (existing). The v0.2 `idx_tasks_complexity` is dropped along with the column. Created/updated in `_ensure_tasks_kanban_indexes` after the migration runs.

### 12.4 Status enum migration (v0.1 → v0.2)

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

### 12.5 Trajectory migration (v0.2 → v0.3)

For each pre-existing task row, derive `trajectory` from the deprecated triple `(complexity, required_reviews, ship_required)`:

- `complexity = 'simple'` → `[{"stage":"execute","to":[<owner if any>]}]`
- `complexity = 'standard'`:
  - prepend `{"stage":"plan","to":<planner_owner from active task_role_assignments row, or []>}` if `spec_path IS NOT NULL`
  - append `{"stage":"execute","to":<executor_owner or []>}`
  - append `{"stage":"audit_syntax","to":<auditor_syntax_owner or []>}` if `'formal'` or `'syntax'` in `required_reviews`
  - append `{"stage":"audit_semantics","to":<auditor_semantics_owner or []>}` if `'semantic'` or `'semantics'` in `required_reviews`
  - append `{"stage":"ship","to":<shipper_owner or []>}` if `ship_required = 1`

Backfill `last_stage_change_at` from the most recent `task_stage_changed` event in the event log; fall back to `claimed_at` or `created_at`.

Drop the columns `complexity`, `required_reviews`, `ship_required`. Idempotent via `team_config['tasks_kanban_v3_migrated']` marker. Runs in `_rebuild_tasks_if_kanban_outdated` after the v0.1→v0.2 step has settled.

---

## 13 · Kanban surface in the project CLAUDE.md

The kanban lifecycle paragraph lives inside the canonical project CLAUDE.md template at [server/templates/app_dev_claude_md.md](server/templates/app_dev_claude_md.md), under the `### Task lifecycle (kanban)` section. There is **no separate kanban-only injector**; the lifecycle surface ships as part of the same template that carries Compass usage rules, audit discipline, communication patterns, and anti-patterns. The whole template is the single source of truth for harness-supplied content in every project's CLAUDE.md.

### 13.1 The paragraph

Required behavioral content the section must convey:

- every Coach delegation goes through kanban — no admission gate; conversational replies remain conversational;
- stored stages with product labels: `audit_syntax` = formal review (contract-bound), `audit_semantics` = semantic review (context-bound — Compass + truth/ + wiki/, not spec);
- Coach defines the trajectory upfront on `coord_create_task` — an ordered list of `{stage, to, focus?}` objects;
- if `plan` is in the trajectory, the executor cannot start until the planner's `coord_write_task_spec` lands;
- if no audit stage is in the trajectory after `execute`, the executor self-audits before completion;
- **semantic audits require a stated `focus`** (what to check) at assignment time — Coach is the one who frames the check. Syntax audits use a contract cascade (spec → title/description → executor wake → commit) so a missing `spec.md` (e.g. on a recovery task that inherits a sibling's spec) doesn't stall the auditor;
- Players use `coord_accept_role` for current-stage calls, `coord_commit_push` for code, and `coord_complete_execution` for non-git artifacts;
- future-stage reservations are hidden until the card reaches that stage;
- pass walks the trajectory to the next stage, fail reverts to execute, and Compass remains informational;
- **v0.3.4 — Coach `on_behalf_of` override.** `coord_submit_audit_report(..., on_behalf_of='<slot>')` is the documented Coach-only path for when an assigned auditor's runtime can't reach the tool. Coach reads the Player's on-disk `audit_*.md`, copies the body, submits with `on_behalf_of=<slot>`. Audit body + verdict + role row are recorded properly (better than `coord_advance_task_stage`, which loses the audit content);
- **v0.3.4 — "tool not visible" Player escape.** If a Player finishes the role work but the named `coord_*` completion tool is **not visible in their runtime**, they MUST NOT just write the deliverable to disk and stop. The kanban will silently sit and the stall sweeper will misattribute the block. Instead they message Coach IMMEDIATELY via `coord_send_message` (with the artifact path), then escalate via `coord_request_human` if Coach is unreachable. Coach picks up the override path via `on_behalf_of` (audits) or `coord_advance_task_stage` (other stages). This is the explicit fix for the production trace where a Player wrote the audit and stopped because their Codex runtime didn't expose `coord_submit_audit_report`.

### 13.2 Propagation flow

The harness never edits a project's `CLAUDE.md` from raw injector code. Two paths:

- **New projects** — `paths.write_project_claude_md_stub` reads the canonical template via `project_claude_md.canonical_project_claude_md_template(...)` and seeds the new project's `CLAUDE.md` first-write-only.
- **Existing projects** — `project_claude_md.update_claude_md_via_coach(project_id)` runs a hidden Coach-identity LLM one-shot (Compass-style direct `claude_agent_sdk.query()` call) on every project activation and once at harness boot for the currently-pinned project. The turn reads the canonical template + the project's current `CLAUDE.md` and writes a reconciled body that reflects the latest harness rules while preserving every line of project-specific content. SHA-256 hash of the canonical template stored in `team_config['claude_md_template_hash_<id>']` (Compass `compass_truth_hash_<id>` precedent) makes a re-run with no template change a no-op. Per-project `asyncio.Lock` serialises rapid activations. Validation failure (under 200 bytes, no leading heading, etc.) emits `claude_md_update_failed` AND `human_attention` so the EnvPane attention strip + Telegram bridge raise it; hash is NOT updated, so the next activation retries.

The Coach pane shows only `.sys` rows: `claude_md_update_started` / `_completed (+N -M lines)` / `_skipped (unchanged | cost_capped)` / `_failed (red)`. The full prompt + response don't surface in the timeline.

### 13.3 Why this shape

Every project's `CLAUDE.md` is a curated artefact (project goals, custom rules, hand-written notes). A blunt static replacement on boot — the previous design — could collide with or disturb that content. Routing the update through Coach gives the team's orchestrator the judgement to merge new harness rules with existing project content, and the canonical template is a single knob: when harness functionality evolves, edit `server/templates/app_dev_claude_md.md` once and the next activation propagates the change everywhere.

### 13.4 Coach lifecycle-policy prompt block

In addition to the static CLAUDE.md block (which Coach reads alongside Players), Coach's per-turn system prompt includes lifecycle policy assembled by `_build_coach_coordination_block` in [server/agents.py](server/agents.py). The policy must reinforce:

- Coach coordinates and does not execute/review/ship;
- answer ordinary questions directly instead of creating kanban tasks;
- create tracked tasks with `workflow`, `tracking_reason`, and review routing;
- assign/call Players by current stage; future-stage reservations are allowed but inactive;
- Players complete work with `coord_commit_push`, `coord_complete_execution`, `coord_submit_audit_report`, or `coord_mark_shipped` so the board flows event-by-event;
- **Audit framing (§4.6).** When assigning an auditor, **name what to check** in the `focus` parameter. Semantic audits without a focus are rejected by the tool — they're noise. Bad: `coord_assign_auditor(kind='semantics', to='p7')`. Good: `coord_assign_auditor(kind='semantics', to='p7', focus="verify the rule-3a derivation matches the wiki entry on multiway causal foliation; check brand naming on user-facing strings.")`. Syntax audits accept an empty `focus` (defaults to "match contract + soundness") but a sharper focus reduces audit noise. Trajectory entries can carry `focus` at create time so the framing is set before the auditor is assigned: `{"stage":"audit_semantics","to":"p9","focus":"..."}`.
- **No redundant-DM after assignment.** Coach must NOT follow `coord_create_task` / `coord_assign_*` with a `coord_send_message` to the assignee. The kanban subscriber's stage-entry wake (and `coord_assign_*`'s own wake when the stage is current) already delivers a prompt naming the task, focus, contract/context, and completion tool. A duplicate DM burns tokens on both sides and creates two competing instructions for the Player to reconcile. `coord_send_message` is reserved for (a) post-acceptance clarifications, (b) nudging a stalled blocker surfaced by the stall sweeper. Initial assignment is silent on Coach's side.
- **v0.3.4** — when the `## Stalled tasks` rollup names a `blocker` who differs from the `executor`, the blocker is the current-stage assignee (auditor / shipper / planner). Coach should nudge THEM, not the executor; if the blocker reports the named `coord_*` tool is not visible in their runtime, Coach reads the artifact they wrote to disk and submits on their behalf via `coord_submit_audit_report(..., on_behalf_of='<slot>')` for audits or `coord_advance_task_stage` for other stages.

## 14 · Telegram escalation hooks

[server/telegram_escalation.py](server/telegram_escalation.py) gains formatters + key-extractors for the kanban event types:

| Event | Formatter |
|---|---|
| `audit_fail_notification` (v0.3, `escalate=True` only) | `"Audit fail: t-... '<title>' — <kind> auditor <slot> returned fail (round N). Reverted to execute. Open the kanban to read the report."` First-fail noise (`escalate=False`) is filtered out at the key-extractor — only `kind_round >= 2` of the same kind reaches Telegram. (See §17 for the dual-surface design.) |
| `stage_assignment_needed` (v0.3 rename of `audit_assignment_needed`) | `"Assignment needed: t-... '<title>' is in <stage> with no <role> assigned. Open the kanban to assign one."` Covers all stages (plan/execute/audit/ship). The legacy `audit_assignment_needed` event is also emitted as a back-compat alias for one release; resolution still keys under the new name via `task_role_assigned`. |
| `audit_self_review_warning` | `"Self-review: t-... '<title>' — Coach assigned <slot> as <kind> auditor; they're also the executor. Acceptable for small teams; flag for review on big tasks."` |

Same web-active vs grace timing as the existing pending-question / pending-plan escalations. The kanban itself shows everything inline regardless.

---

## 15 · Compass cross-reference

### 15.1 Why Compass is informational

Two separate concerns: the Player reviewer checks the **artifact against the spec** (does this artifact do what the task said?). Compass checks the **artifact against the lattice** (does this drift from the project's stated direction?). Both signals are useful but they're not the same thing — the task can be specced wrong, in which case a Compass drift verdict matters more than a formal pass; or the spec can be aligned but the implementation/artifact buggy, in which case the assigned reviewer is the right gate.

Putting Compass on the gate would conflate the two. The kanban v1 keeps them separate: the Player reviewer gates; Compass surfaces a pip on the card so the semantic reviewer can read it as one input.

### 15.2 How the semantic reviewer uses Compass

The semantic reviewer's wake prompt is **context-bound, not spec-bound** (see §4.6.2 for the full framing). The wake includes:

- Coach's `## Focus` (the lens — required, see §4.6.3).
- A `## Project context` block listing the truth corpus paths (`<project>/truth/`, `<project>/project-objectives.md`, `/data/wiki/<project_id>/`), the Compass-derived block already injected into the project's `CLAUDE.md`, and `tasks.compass_audit_report_path` when the auto-audit ran on the executor's commit.
- The delivered artifact/commit reference.
- A pointer to the spec as **supplementary background only** ("what was meant to be built — but judge the deliverable against the world, not against the spec; a spec that drifted from intent is a bug the semantic auditor must catch").

Direct `compass_ask` calls are Coach-only per [compass-specs.md §6](compass-specs.md), so the auditor reads the Compass surface that's already in `CLAUDE.md` and in `tasks.compass_audit_report_path`. If those are insufficient for the focus, the auditor messages Coach via `coord_send_message` to run `compass_ask` on their behalf — better than a guessing semantic verdict.

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

Every code commit on a task with both audit stages can trigger up to 2× Player reviews (formal then semantic, depending on the trajectory) plus 1× Compass auto-audit. The review prompts are bounded at ~16 KB per artifact body (see [compass-specs.md §5.5.2](compass-specs.md)) so cost stays predictable, but a project with 30 commits/day and a `latest_sonnet` review budget will see meaningfully higher daily spend than the pre-kanban harness.

Mitigations: keep trajectories tight (omit audit stages for low-stakes work); use `[{"stage":"execute","to":[...]}]` self-audit shape for genuinely small changes; set `HARNESS_TEAM_DAILY_CAP` to a hard ceiling; disable `HARNESS_COMPASS_AUTO_AUDIT` on cost-constrained deploys.

### 16.4 What needs verification

End-to-end on a deployed Zeabur instance:

1. Full code-and-review pipeline with hard-assigned roles via inline `trajectory=[plan, execute, audit_syntax, ship]`.
2. Pool-style trajectory with multiple candidates per stage — race-safe `coord_claim_task` / `coord_accept_role`.
3. Self-audit-only trajectory `[{"stage":"execute","to":[...]}]` — auto-wake prompt contains the self-audit instruction; `commit_pushed` or `task_execution_completed` jumps straight to archive.
4. Idle-Player polling — pool task posted while all eligible Players are over-cap; caps reset; poller wakes one within the next sweep.
5. Review fail loop — round 1 fail, round 2 pass; card shows only latest; `audits/` folder on disk has both rounds.
6. Telegram escalation: `audit_fail` ping arrives on the phone after the configured grace period.
7. CLAUDE.md kanban block injected on boot; surviving a manual edit + restart (idempotent re-injection).
8. **Stall detection**: lock the executor of an in-flight task; after `HARNESS_KANBAN_STALL_SECONDS` the sweeper fires `task_stage_stale` and Coach's next prompt shows `## Stalled tasks`.
9. **Flow health**: `GET /api/tasks/flow_health` returns expected shape; `subscriber_alive: false` after manually cancelling the lifespan task; UI footer goes red.

---

## 17 · Coach quality feedback

There are **two distinct surfaces** for audit-fail signals:

### 17.1 Per-fail bus notification (every fail)

Every `audit_report_submitted{verdict='fail'}` causes the kanban subscriber to publish a sibling `audit_fail_notification` event routed to Coach (see §8). This event lands in Coach's pane and is forwarded to Telegram by the existing escalation watcher. **Coach sees every fail** — no silent fails — but is instructed (in the lifecycle-policy prompt block) to treat the **first** fail of any audit kind as normal correction noise and not act.

### 17.2 Per-turn rollup (escalation only)

The `## Active task health` section in Coach's coordination block (rendered by `_build_active_task_health_rows` in [server/agents.py](server/agents.py)) surfaces tasks **only** when the same audit kind has failed **two or more times** on the same task. The trigger is per-(task, kind), not per-task:

**Trigger**: a task is non-archive AND there exists an audit kind `K ∈ {syntax, semantic}` such that the count of `task_role_assignments` rows for that task with `role = 'auditor_<K>'` AND `verdict = 'fail'` is ≥ 2.

This matches the rule "expect first fail as normal; act on the second fail of the same kind."

**Row contents**: `(task_id, title, executor_slot, executor_runtime, executor_model, executor_effort, kind, kind_fail_count, latest_round)`. Resolved by joining `tasks` to the active executor + counting fails per kind, then resolving the executor's runtime/model/effort via the existing helpers `_get_agent_runtime` / `_get_agent_model_override` / `_get_agent_effort_override` and role defaults.

**Pointer**: each rollup ends with the explicit ladder:

> If quality is the bottleneck (this is the 2nd fail of the same audit kind), bump the executor's effort first with `coord_set_player_effort(player_id, 'high'|'max')`, then model tier with `coord_set_player_model(player_id, 'latest_opus')`. Do **not** change runtime — that's a human decision. Read the executor's current settings via `coord_get_player_settings` before bumping so you don't re-set what's already correct.

### 17.3 Why two surfaces

Coach needs to **know** about every fail (visibility) but should **act** only on patterns (signal). The bus notification is the visibility layer; the rollup is the action layer. Without the visibility layer Coach is blind to single fails (which may still warrant a `coord_send_message` nudge or a clarifying question to the executor); without the action filter Coach over-reacts to normal first-correction cycles.

The same ladder is in `MODEL_GUIDANCE` ([server/models_catalog.py](server/models_catalog.py)) — Coach reads it twice (once as policy, once as actionable signal) so the connection between symptom and remedy is unmistakable.

---

## 18 · Flow continuity & observability

Three surfaces let the human (and Coach) answer "is the engine actually moving?"

### 18.1 Stage-entry wake guarantees

Every status transition stamps `tasks.last_stage_change_at = now()` in the same DB write as the status update. The subscriber then walks the next stage's role row:

- Active row with `owner` set → wake that Player.
- Active row with empty `owner` and non-empty `eligible_owners` → wake every eligible Player (pool call).
- Active row with both `owner` and `eligible_owners` empty, OR no active row → emit `stage_assignment_needed` routed to Coach.

Renamed from v0.2's `audit_assignment_needed` — the new event covers `plan` and `execute` too, not just audit/ship. The old name is retained as a back-compat alias for one release.

### 18.2 Stall sweeper

See §10.5. Sibling pass in `idle_poller.py`. Detects `last_stage_change_at` older than `HARNESS_KANBAN_STALL_SECONDS` (default 4 h). Emits `task_stage_stale` once per threshold-crossing (gated by `tasks.stale_alert_at`).

### 18.3 Coach's `## Stalled tasks` rollup

Sibling to `## Active task health` in `_build_coach_coordination_block`. Lists every task where `stale_alert_at IS NOT NULL` (i.e. the stall sweeper has fired at least once and the task hasn't progressed since).

**v0.3.8 escalation label.** Each row carries `[escalation: <rung>]` so Coach sees which auto-action is imminent: `fresh` (no rung yet) / `nudged` (rung 1 fired) / `Coach-notified — auto-reassign next` (rung 2 fired) / `auto-reassigned — auto-archive next` (rung 3 fired) / `auto-archived` (rung 4 fired, terminal).

Format:
```
- t-... "<title>" — stage <stage>, blocker <slot> (executor <slot>), stale for <hours>h [escalation: <rung_label>]
```

Followed by: "If a Player has gone silent, send them a `coord_send_message` nudge or reassign with `coord_assign_*`. If no one is assigned to this stage, fix the trajectory. ESCALATION LADDER (v0.3.8): rung 1 nudge at 30min, rung 2 Coach call at 1h, rung 3 auto-reassign at 2h, rung 4 auto-archive at 4h. Intervene before the next rung fires — auto-actions are the safety net, not the plan."

### 18.3a Coach's `## Unrecorded artifacts on disk` rollup (v0.3.8)

Sibling to `## Stalled tasks`, fed by the reconciliation sweep (§10.6). Built by `_build_unrecorded_artifacts_rows(project_id)` ([server/agents.py](../server/agents.py)) — reads `task_spec_unrecorded` / `task_audit_unrecorded` events from the last 24h, deduped per (task, kind/round). Capped at 10 rows.

Format:
```
- t-... (spec | audit_syntax | audit_semantics) — path projects/.../spec.md, owner p5

Suggested calls (Coach picks the right one and pastes the body from disk):
  - coord_write_task_spec(task_id='t-...', body=<paste from disk>, on_behalf_of='p5')
  - coord_submit_audit_report(task_id='t-...', kind='semantics', body=<paste from disk>, verdict='pass'|'fail', on_behalf_of='p3')
```

Followed by: "If the file is junk / superseded, ignore it — the reconciliation finding will re-emit in 1h until the kanban records something or the file is removed."

This block addresses the recurring "Player did the work but the kanban didn't notice" failure mode — Coach sees the artifact path AND the exact override call to make in one place, no construction overhead.

### 18.4 `GET /api/tasks/flow_health` endpoint

Returns:

```json
{
  "stages": {
    "plan":            {"count": 3, "oldest_stage_change": "2026-05-04T08:12:01Z"},
    "execute":         {"count": 5, "oldest_stage_change": "..."},
    "audit_syntax":    {"count": 1, "oldest_stage_change": "..."},
    "audit_semantics": {"count": 0, "oldest_stage_change": null},
    "ship":            {"count": 2, "oldest_stage_change": "..."}
  },
  "stalled_count": 2,
  "subscriber_last_event_at": "2026-05-04T11:05:33Z",
  "subscriber_alive": true
}
```

`subscriber_last_event_at` is recorded in module-level state by [server/kanban.py](server/kanban.py)'s consumer task on every event it processes (a single ISO timestamp written in-memory; not DB-persistent). `subscriber_alive` is `True` iff the lifespan task handle exists and is not `done()`.

### 18.5 UI surface

`.kanban-flow-health` strip at the bottom of [server/static/kanban.js](server/static/kanban.js) shows `Subscriber: alive · Stalled: 2 · Last event: 11:05:33`. Polls `/api/tasks/flow_health` every 30 s and refreshes on `task_stage_changed` / `task_stage_stale` events. Red tint when the subscriber is not alive or stalled count > 0.

---

## Cross-references

- [TOT-specs.md](TOT-specs.md) — umbrella spec.
- [compass-specs.md](compass-specs.md) — audit-report sibling, drift escalation, cost discipline patterns reused here.
- [recurrence-specs.md](recurrence-specs.md) — background-loop infrastructure parallel; the kanban subscriber + idle poller follow the same lifespan-task pattern.

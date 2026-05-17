---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 12: Coordination Tools'
section: 12
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 12. Coordination Tools

All coordination tools are registered as an in-process MCP server named
`coord` for each SDK query. The caller id is captured when the server is built,
so permissions do not depend on the model truthfully passing its identity.

### 12.1 Task Tools

`coord_list_tasks(status?, owner?, include_backlog?)`

- Lists up to 100 tasks in active project.
- Optional `status`. No-args and `status='pending'` views include pending
  Backlog entries by default when no owner filter is present, rendered as
  `kind=backlog`. Concrete kanban stage filters (`execute`, `audit_syntax`,
  etc.) remain task-only.
- Optional `owner`, with `null`/`none`/`unassigned` matching tasks whose
  current stage has no active `task_role_assignments` row (kanban v2
  role-state model). Specifically: NOT EXISTS a row for the current
  stage's role with `completed_at IS NULL AND superseded_by IS NULL AND
  owner IS NOT NULL`. This mirrors the UI's "unassigned" badge classifier
  exactly; the legacy `tasks.owner IS NULL` filter is no longer used for
  the unassigned case.
- The `owner=` field in each output row shows the active role assignee from
  `task_role_assignments` (the kanban v2 source of truth), falling back to
  `tasks.owner` for archive/non-standard stages where no role row exists.
- Each task row includes `kind=task`; each row for an active kanban stage
  (plan/execute/audit_syntax/audit_semantics/ship/verify) includes a
  `stage_role=<role>:<state>` field:
  - `executor:p3` — live assignment with named owner
  - `executor:done` — non-audit role row completed (awaiting Coach advance)
  - `complete:p5:pass` — audit stage completed with pass verdict
  - `complete:p5:fail` — audit stage completed with fail verdict
  - `executor:-` — no active or completed assignment (unassigned)
  - Field is omitted for archive and other non-standard stages.
  - For audit stages (`audit_syntax`, `audit_semantics`) with a completed
    role row, the output uses `complete:<owner>:<verdict>` shape instead of
    `<label>:done` so Coach can see pass/fail inline without querying the
    audit report. When a completed audit row has no verdict (edge case),
    falls back to `<label>:done`.

`coord_create_task(title, description?, parent_id?, priority?, workflow?, tracking_reason?, trajectory?, note?, success_criteria?)`

- **Coach top-level tasks land in the Backlog first (FIFO discipline).**
  When Coach calls this tool WITHOUT `parent_id`, the item is inserted
  into `backlog_tasks` (same table as `coord_propose_task`). No kanban
  row is created yet; no Player is woken. Coach must then call
  `coord_triage_backlog(id, action='promote', trajectory=[...])` to
  promote it to the kanban. Promotion creates a `truthgate` task,
  preserves the requested trajectory as the post-gate path, and
  automatically runs the TruthGate assessment; it does not plant the
  first Player role or wake anyone. Normal promotion is allowed only for
  the next pending entry by explicit priority (`urgent`, `high`,
  `normal`, `low`) and FIFO within the same priority. Out-of-order
  promotion requires `emergency=true` and a non-empty
  `emergency_rationale`; the metadata is stored on the backlog row and
  promoted task. The `priority`, `trajectory`, `note`, and
  `success_criteria` params are stored on the backlog entry so Coach does
  not need to repeat them at triage time.
- **Player subtasks** (with `parent_id`) are unaffected — they still
  plant directly on the kanban under their parent task.
- Priority: `low`, `normal`, `high`, `urgent` (default `normal`).
  Stored on the backlog entry for Coach top-level tasks; stored on the
  task row for Player subtasks.
- `trajectory` is REQUIRED for Coach top-level tasks. Stored on the
  backlog entry; `coord_triage_backlog promote` reads it automatically
  so Coach can omit it at triage time when it was already set at
  creation time.
- Emits `backlog_task_proposed` for Coach top-level tasks; promotion emits
  `task_created`, `task_stage_changed{to='truthgate'}`,
  `backlog_task_promoted`, `task_truthgate_started`, and
  `task_truthgate_completed` or `task_truthgate_blocked`. Player subtasks emit
  `task_created` + (when first stage planted) `task_role_assigned` +
  `task_stage_changed{from=null}`.
- See `Docs/kanban-specs-v2.md` §7.1 for the canonical contract.

`coord_approve_stage(task_id, next_stage, assignee, note?)`

- Coach only.
- THE single stage-transition tool in v2. Replaces v1's
  `coord_advance_task_stage` and the four `coord_assign_*` variants.
- `next_stage` ∈ {plan, execute, audit_syntax, audit_semantics, ship,
  verify, archive}; transition validated against the §3.1 state machine.
- `assignee` is required for any non-archive `next_stage`; pass a
  single Player slot. Pools are FYI only — pick one explicit name.
- Atomically: stamps `last_stage_change_at`; deactivates any prior
  active role row at the target stage (with stand-down wake);
  deactivates the source-stage role row when Coach overrides without
  source completion; plants the fresh role row; emits
  `task_stage_changed` + `task_role_assigned`; fires the wake using
  `note` as the verbatim brief.
- Same-stage allowance (§7.1): when called with `next_stage` equal to
  current status AND no active role row exists at the target, plants
  the row without firing `task_stage_changed`. Used as the first plant
  after a pool/empty first-stage create.
- See `Docs/kanban-specs-v2.md` §7.1.

`coord_archive_task(task_id, summary)`

- Coach only.
- Deliberate archive with a user-facing summary. Marks every active
  role row complete, transitions to archive, emits `task_archived`
  with the summary in the payload.
- Delivered archive rejects provisional TruthGate emergency-override
  tasks until Coach records a valid `closure_reference` with
  `coord_record_provisional_closure`. `amendment:<proposal_id>`
  closures must reference an approved `truth/` proposal before archive;
  `none_needed:<rationale>` and `rollback:<task_id>` are accepted when
  their references validate.
- v2 has NO auto-archive on trajectory completion — every task ends
  with this Coach-written wrap-up.

`coord_record_provisional_closure(task_id, closure_reference)`

- Coach only.
- Records the reconciliation reference required before a provisional
  task can be delivered to archive. Accepted forms:
  `amendment:<proposal_id>`, `none_needed:<rationale>`, and
  `rollback:<task_id>`.
- Emits `task_provisional_closure_recorded`. Does not move the task or
  wake a Player.

`coord_submit_verification_report(task_id, verdict, body, message_to_coach?, evidence?)`

- Players only; requires task status `verify` and an active verifier role
  row for the caller.
- Writes `verifications/verification_<round>.md`, records `pass`/`fail`
  on the verifier role row, marks that row complete, resets the verifier
  to idle tools, emits `verification_report_submitted`, and wakes Coach.
- `verdict='fail'` does not auto-revert, auto-create follow-up work, or
  archive. Coach reads the report and decides whether to archive, create
  a follow-up, roll back, reroute to execute, or re-ship.

`coord_set_task_trajectory(task_id, trajectory)`

- Coach only.
- Mid-flight reroute. Cannot remove a stage already entered; can
  insert / drop unentered stages and change `to` candidates.
- Emits `task_trajectory_changed`.

`coord_update_task(task_id, status='archive', note?)`

- DEPRECATED for stage transitions in v2 — use `coord_approve_stage`.
- Tolerated only as the fast-cancellation backstop
  (`status='archive'` without a user-facing summary). Prefer
  `coord_archive_task` so the user sees a deliberate wrap-up.

The v1 tools `coord_claim_task` / `coord_accept_role` /
`coord_advance_task_stage` / `coord_assign_task` /
`coord_assign_planner` / `coord_assign_auditor` /
`coord_assign_shipper` / `coord_complete_execution` /
`coord_mark_shipped` are REMOVED in v2 (see kanban-specs-v2.md §7.3).

### 12.2 Messaging

`coord_send_message(to, body, subject?, priority?)`

- Any agent can message any other agent or `broadcast`.
- Body max 5000 chars.
- Subject max 200 chars.
- Priority: `normal` or `interrupt`.
- Cannot send to self.
- Coach cannot direct-message locked Players.
- Emits `message_sent`.
- Direct recipients auto-wake; broadcasts do not.

`coord_read_inbox()`

- Reads unread direct and broadcast messages for caller in active project.
- Marks each read in `message_reads` for that caller only.
- Locked Players filter out Coach-sourced messages while locked.

### 12.3 Memory

`coord_list_memory()`

- Lists active project's topics with version, timestamp, author, size.

`coord_read_memory(topic)`

- Validates topic regex.
- Reads current content.

`coord_update_memory(topic, content)`

- Validates topic.
- Rejects empty content.
- Max 20,000 chars.
- Upserts and increments version.
- Emits `memory_updated`.
- Fire-and-forget mirrors a markdown file to WebDAV when enabled (`projects/<id>/memory/<topic>.md`).

**Important access contract:** the memory store is SQLite + kDrive
WebDAV mirror only. There is **no** local on-disk file under
`/data/projects/<id>/working/memory/` — and that subdirectory is
intentionally not part of the project scaffold (see §4.6). Agents
reading via the `Read` tool will find nothing. Always use the MCP
tools (`coord_*_memory`). The kDrive mirror is for the human's
benefit (browse on disk / in the kDrive web UI), not the agents'.
Coach's 2026-05-12 report flagged the prior confusion when the
template implied a path-style store.

### 12.4 Knowledge

`coord_write_knowledge(path, body)`

- Any agent can write.
- Project-scoped to active project.
- Local path: `/data/projects/<active>/working/knowledge/<path>`.
- WebDAV path currently: `projects/<active>/knowledge/<path>`.
- Body required, max 100,000 chars.
- Path max four segments.
- File suffix `.md` or `.txt`.
- Emits `knowledge_written`.

`coord_read_knowledge(path)`

- Reads local cache first, WebDAV fallback.
- Validates same path rules.

`coord_list_knowledge()`

- Lists active project's known `.md`/`.txt` knowledge paths.

### 12.5 Outputs

`coord_save_output(path, content_base64)` is implemented as a function and
listed in `ALLOWED_COORD_TOOLS`, backed by `server/outputs.py`.

Intended behavior:

- Save binary deliverables.
- Path max four segments.
- Max decoded size 20 MB.
- Allowed suffixes:
  - `.docx`, `.xlsx`, `.pptx`
  - `.pdf`
  - `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`
  - `.zip`, `.tar`, `.gz`
  - `.csv`, `.tsv`
  - `.md`, `.txt`, `.html`, `.json`
- Emit `output_saved`.
- Mirror to WebDAV.

Current implementation gap:

- `save_output` is not included in the `tools=[...]` list passed to
  `create_sdk_mcp_server()` in `server/tools.py`.
- Therefore agents may not actually see/call it despite the function and
  allowlist existing.
- `server/outputs.py` still writes to global `/data/outputs`, not
  project-scoped `project_paths(active).outputs`.

### 12.6 Git

`coord_commit_push(message, push?)`

- Players only.
- Requires project repo/worktree configured.
- Fails if current workspace has no `.git`.
- Runs:
  - `git add -A`
  - `git status --porcelain`
  - `git commit -m <message>`
  - optionally `git push origin HEAD`
- `push` defaults true; false values: `false`, `0`, `no`, `off`.
- Clean tree returns soft OK.
- Emits `commit_pushed`.

### 12.6.1 Ship to Dev

`coord_ship_to_dev(task_id)`

- **Players only** (Coach ships via `coord_approve_stage`).
- Ships an audited task to the `dev` integration branch via cherry-pick
  + GitHub PR (squash merge).
- **Gate checks (fail-fast, in order):**
  1. Task must be in `ship` stage.
  2. Caller must not be Coach.
  3. Caller must have an active (uncompleted, not-superseded) `shipper`
     role row on the task.
  4. A `commit_pushed` project event must exist for the task (executor
     must have called `coord_commit_push` first); the event's
     `payload_pointer` holds the executor commit SHA.
  5. Every audit stage in the task's trajectory must have an
     un-superseded `PASS` verdict from the matching auditor role
     (`audit_syntax` → `auditor_syntax`,
     `audit_semantics` → `auditor_semantics`).
- **Git operations** (in caller's worktree):
  1. `git fetch origin`
  2. If a prior `task_shipped_to_dev` event exists, treat the call as
     idempotent: close any still-open shipper role row and return the
     existing evidence without another GitHub PR or duplicate ship event.
  3. If the executor commit or equivalent patch is already present on
     `origin/dev`, close the shipper role row, emit `task_shipped_to_dev`
     with `ship_method='already_present'`, `idempotent=true`,
     `ship_sha=<origin/dev sha>`, and explicit
     `already_present_verification` evidence (`executor_sha_is_ancestor_of_origin_dev`
     or `git_cherry_patch_id_match`), then return without opening a PR.
  4. If local branch `ship-<task_id>` already exists, resume it:
     - If already on the branch and there is no `CHERRY_PICK_HEAD` or
       unmerged path, continue to push/PR/merge.
     - If on another branch, require a clean worktree before checking out
       `ship-<task_id>`.
     - If conflicts are still unresolved, fail closed with instructions
       and do not push, create a PR, emit ship evidence, or complete the
       role.
  5. Otherwise, `git checkout -b ship-<task_id> origin/dev`
  6. `git cherry-pick -x <executor_sha>`
  7. `git push origin ship-<task_id>:ship-<task_id>`
  - Cherry-pick conflict → returns error with the conflicted SHA and
    instructs the Player to resolve manually or run
    `git cherry-pick --abort` to clean up. After manual resolution and
    `git cherry-pick --continue`, rerun `coord_ship_to_dev(task_id)` to
    resume the existing temp branch.
  - Empty/no-op cherry-pick → aborts the cherry-pick, re-checks whether
    the patch is already present on `origin/dev`, and if confirmed uses
    the already-present success path with explicit verification evidence.
    If not confirmed, fail closed without emitting ship evidence or
    completing the shipper role; the shipper must create formal ship
    evidence through a non-empty PR/marker commit or ask Coach to reroute.
- **GitHub API** (PAT extracted from `projects.repo_url`):
  1. Query open PRs for `head=<owner>:ship-<task_id>&base=dev`; reuse
     one if present.
  2. `POST /repos/{owner}/{repo}/pulls` → create PR titled
     `[ship] <task_id>: <title>` when no reusable PR exists. A 422
     collision triggers another open-PR lookup before failing.
  3. `PUT /repos/{owner}/{repo}/pulls/{n}/merge` (squash merge)
  4. `DELETE /repos/{owner}/{repo}/git/refs/heads/ship-<task_id>`
     (non-fatal on failure)
- **Post-success:**
  - Closes shipper role row (`completed_at` stamped).
  - Emits `task_shipped_to_dev` event with `task_id`, `ship_sha`,
    `pr_number`, `pr_url`, `executor_sha`, `deploy_target='dev'`,
    `ship_method` (`'pr'`, `'resumed_pr'`, or `'already_present'`), and
    `idempotent`.
  - Wakes Coach via `_wake_coach_for_completion`.
- **Return:** `ok=True` text with `pr_url`, `pr_number`, dev HEAD SHA.
  If the trajectory includes `verify`, the response reminds Coach to
  approve the optional post-ship verification stage; it does not
  transition automatically.
- Raw `git push origin ...:dev` bypasses this gate and is a pb-005
  violation; use `coord_ship_to_dev` instead.

### 12.7 Decisions

`coord_write_decision(title, body)`

- Coach only.
- Body max 40,000 chars.
- Slugifies title.
- Filename:
  - `YYYY-MM-DD-<slug>.md`
  - local collision gets `-2`, `-3`, etc.
- Frontmatter includes title, date, timestamp, author.
- Writes to WebDAV `projects/<active>/decisions/<filename>` when enabled;
  otherwise local fallback under `/data/projects/<active>/decisions/`.
- Emits `decision_written`.

### 12.7.5 File-write Proposals

`coord_propose_file_write(scope, path, content, summary)`

- **Coach only.** Players get an explicit error directing them to
  message Coach for relay. Listed in `ALLOWED_COORD_TOOLS`; the body
  rejects non-Coach callers.
- **Two scopes today**:
  - `scope='truth'` — `path` is relative to the *currently active*
    project's `truth/` folder. Existing path validation rules apply:
    a leading `truth/` is stripped defensively; no leading slash, no
    `..` segments; first segment must not be `projects/` or a known
    sibling project slug (catches the recurrent Coach mistake of
    encoding cross-project paths when truth/ is per-active-project
    by design).
  - `scope='project_claude_md'` — `path` must be exactly `'CLAUDE.md'`.
    Targets `/data/projects/<active>/CLAUDE.md`. Any other path is
    rejected at propose time AND re-validated at approve time (the
    resolver refuses to write if the row's path was tampered with).
  - The harness-wide `/data/CLAUDE.md` is NOT a valid scope; only
    the user edits that file.
- `content` is a full file body (hard cap 512,000 chars). This is a full
  REPLACE — Coach must include the parts being kept verbatim. The
  user reviews a side-by-side diff against the current file content
  in the UI before approving.
- Human-reviewable truth should stay much smaller than the hard cap:
  target 2-8 KB per truth section, warn above 12 KB, strongly prefer
  splitting above 25 KB, and avoid full-file proposals above 50 KB
  unless bridging an existing monolithic file. The hard cap exists so
  legacy files like `truth-index.md` can still be mirrored while the
  section-based truth flow is built.
- `summary` is a single-line "why" (max 200 chars) shown next to the
  approve/deny buttons.
- **Auto-supersede invariant**: at most one `pending` proposal per
  `(project_id, scope, path)`. The tool's body runs
  SELECT-INSERT-UPDATE in one transaction:
  1. SELECT pending IDs for the same `(project, scope, path)`.
  2. INSERT the new row as `pending`.
  3. For each prior pending ID: UPDATE to `status='superseded'`,
     `resolved_by='system'`, `resolved_note='superseded by #<new_id>'`.
     The UPDATE has `WHERE status='pending'` so already-resolved rows
     can never be flipped to superseded.
  The scope filter prevents a hypothetical `truth/CLAUDE.md` and a
  `project_claude_md`/`CLAUDE.md` proposal from supersede-colliding.
- Emits one `file_write_proposal_superseded` event per superseded
  row, then one `file_write_proposal_created` event for the new
  pending row (with the `superseded` ID list and `scope` in its
  payload).
- Reorganization pattern (truth scope) documented in the tool
  description: split a growing file by sending three proposals in
  sequence — (1) new dependency files with content, (2) original
  file with content removed, (3) `truth-index.md` updated to list
  the new files. Each proposal supersedes any prior pending one for
  that (scope, path).

The MCP tool is the ONLY agent-accessible mechanism for changing
`truth/` or per-project `CLAUDE.md`. The PreToolUse
`_pretool_file_guard_hook` in `server/agents.py` denies any agent
`Write` / `Edit` / `MultiEdit` / `NotebookEdit` whose path resolves
to either protected target (any project's `truth/*` or its
`<slug>/CLAUDE.md`), plus any `Bash` command containing those path
components. The deny is unconditional: every agent (including
Coach), every tool, every project. A Player's worktree-internal
`<slug>/repo/<slot>/CLAUDE.md` is NOT caught (different position in
the path) and remains writable for the Player whose repo it is.

The resolver in `server/truth.py` handles approve/deny:
- Approve: validates the path lands under `project_paths(slug).truth`
  with a `relative_to` check (rejects traversal as
  `FileWriteProposalBadRequest`), `mkdir(parents=True, exist_ok=True)` on
  the parent, then `write_text(content, encoding="utf-8")`. Caps at
  512,000 chars. Bypasses the Files-pane endpoint's `.md`/`.txt` restriction
  on purpose — text-of-any-extension is the right policy for truth
  (specs, brand YAML, JSON contracts) since the user is the gate at
  approval time.
- Deny: marks the row, no file write.
- Idempotency: re-resolving a non-pending row raises
  `FileWriteProposalConflict("approved")` / `("denied")` /
  `("cancelled")` / `("superseded")` → 409.

### 12.7.6 Project File Reads

`coord_read_file(path)`

- **Available to all agents** (Coach AND Players). Reads are
  inherently safe; the read-handle invariant only constrains writes.
- `path` is relative to the active project's root
  (`/data/projects/<active>/`). Examples: `'CLAUDE.md'`,
  `'truth/specs.md'`, `'decisions/0001-foo.md'`,
  `'working/knowledge/notes.md'`, `'outputs/report.md'`.
- Rejects leading `/` and any `..` segment; resolves the target
  with `Path.resolve()` and re-anchors under the project root so a
  symlink / weird-casing trick can't escape the lane.
- 512,000-char size cap (matches the protected proposal hard cap). Files
  that aren't valid UTF-8 are rejected with a clear error rather
  than returning garbage; binary deliverables under `outputs/`
  cannot be read through this tool.
- This is hard-cap headroom, not the desired long-term truth shape:
  large truth/spec files should be split into human-reviewable sections
  with a target of 2-8 KB per section.
- Exists in addition to Claude's native `Read` because the Codex
  runtime's restrictive sandbox blocks alternative read paths;
  `coord_read_file` bypasses that constraint via the MCP proxy. See
  `Docs/CODEX_RUNTIME_SPEC.md` for sandbox details. On Claude this
  overlaps with `Read` — agents can use either.
- Project CLAUDE.md note: it's also auto-injected into every
  agent's system prompt via `server/context.py`, so calling
  `coord_read_file('CLAUDE.md')` is redundant for read-only
  inspection at turn start. Use it when you want the *current*
  body during a long turn (the system-prompt copy is frozen at
  turn start; a manual file edit since the turn began is invisible
  until the next turn unless you re-read).

### 12.8 Team Identity

`coord_list_team()`

- Joins global `agents` with active project's `agent_project_roles`.
- Shows slot, name, role, brief preview, status, current task, lock marker.

`coord_set_player_role(player_id, name, role)`

- Coach only.
- `player_id`: `p1` to `p10`.
- Name max 80 chars.
- Role max 300 chars.
- Upserts active project's `agent_project_roles`.
- Emits `player_assigned`.

`coord_set_player_runtime(player_id, runtime)`

- Coach only.
- `player_id`: `p1` to `p10` (cannot flip Coach's own runtime via MCP
  — that path is HTTP-only via `PUT /api/agents/coach/runtime` or
  `POST /api/agents/coach/transfer-runtime`).
- `runtime`:
  - `'claude'` / `'codex'` — concrete target. Routes through the
    **session-transfer flow** by default (§10.3): if the Player has a
    prior session on the source runtime, a transfer-mode compact is
    queued via `asyncio.create_task` and the tool returns immediately
    with `queued=True`; on success the runtime flips and
    `session_transferred` fires. If there's no prior session, the
    runtime flips immediately + emits `runtime_updated` +
    `session_transferred(note=no_prior_session)`. Same-runtime target
    is a no-op.
  - `''` (empty string) — **blunt clear**. Writes
    `runtime_override=NULL` (revert to role default), no compact, no
    transfer event. Use only when an explicit fresh start on the role
    default is desired.
- `'codex'` is rejected when `HARNESS_CODEX_ENABLED` is unset; the
  error message tells Coach to call `coord_request_human` so the user
  can flip the env flag on the deployment.
- Mid-turn flips are rejected (mirrors `PUT /api/agents/{id}/runtime`'s
  409 behavior) — the in-flight turn would be on the old runtime
  while subsequent turns use the new one.
- Existing `model_override` is preserved across the flip. Spawn-time
  resolution silently drops a model that doesn't fit the new runtime
  and falls through to the role default; flipping back re-applies the
  preserved override.
- Updates global `agents.runtime_override` (NOT per-project — runtime
  is a global slot setting).
- Side-effect: invalidates the Codex client cache for the slot
  (`evict_client`) so a codex→claude flip drops the old subprocess +
  proxy token rather than leaving them dangling until the next
  MCP-config change.
- Emits `runtime_updated` with `agent_id=<player_id>` (NOT the caller),
  matching the shape of the HTTP runtime endpoints — the event
  renders in the target Player's pane regardless of whether the human
  or Coach initiated the flip. Coach's tool_use / tool_result pair
  already records "Coach called this tool" in Coach's timeline, so
  logging the state-change event there too would duplicate. The
  `runtime_updated` event also carries `source=session_transfer` when
  the flip came through the transfer flow rather than the blunt-clear
  branch. No `to` field — `runtime_updated` is not a fan-out type in
  either the WS-side handler or the `/api/events` SQL filter.
- Required precondition for `coord_set_player_model` when Coach wants
  a model from the other runtime family.

`coord_set_player_model(player_id, model)`

- Coach only.
- `player_id`: `p1` to `p10`.
- `model`: a TIER ALIAS (`latest_opus`, `latest_sonnet`,
  `latest_haiku` for Claude; `latest_gpt`, `latest_mini` for Codex)
  OR a concrete version id (`claude-opus-4-7`, `gpt-5.4-mini`, …) on
  the runtime-appropriate whitelist. Aliases are preferred — the
  harness resolves them to the current concrete id at spawn time, so
  a stored `latest_sonnet` automatically picks up the next Sonnet
  release. Concrete ids stay accepted for cases where a specific
  version pin matters. Empty string clears the override.
- Validated against the player's currently-resolved runtime — a Codex
  model id on a Claude-runtime player is rejected at SET time with
  an actionable error pointing to `coord_set_player_runtime` for the
  flip. If the runtime later flips, the stored override that no
  longer fits is silently dropped at spawn time and resolution falls
  through to the role default.
- Upserts `agent_project_roles.model_override` for the active project.
  Empty-clear on a player that has no row is a no-op (no orphan row
  is created).
- Emits `agent_model_set` with `to: <player_id>` so the WS / history
  fan-out renders the event in the target Player's pane as well as
  Coach's.
- Coach's system prompt includes a `MODEL_GUIDANCE` block (see
  [server/models_catalog.py]) that tells Coach: model changes are the
  exception, Sonnet is the Player default, Opus is for hard reasoning
  only, Haiku is for trivial mechanical work, Codex is the rate-limit
  fallback (`gpt-5.4-mini` as the Sonnet equivalent, top Codex tier
  reserved for heavy work).

Resolution chain in `run_agent` (highest → lowest):

1. Per-turn `model` arg from the request body (per-pane gear popover).
2. Coach-set `agent_project_roles.model_override` (this tool). Dropped
   silently at spawn time if it no longer fits the player's current
   runtime (e.g. a stored Claude id with `runtime_override='codex'`).
3. Runtime-aware per-role default in `team_config`
   (`coach_default_model` / `players_default_model` and their
   `_codex` counterparts).
4. Hardcoded role default in
   `models_catalog._ROLE_MODEL_DEFAULTS` /
   `_ROLE_CODEX_MODEL_DEFAULTS` (resolved via `role_default_model`).
   Stored as tier aliases (`latest_opus` for Coach, `latest_sonnet`
   for Players, `latest_gpt` for Codex Coach, `latest_mini` for
   Codex Players) so model bumps only touch `_ALIAS_TO_CONCRETE`.
   Every (runtime, role) combination has a concrete default — Codex
   Coach was historically empty (cost rationale) but is now
   `latest_gpt` for symmetry with Claude Coach=Opus, eliminating the
   chip's runtime-tag fallback.
5. SDK default (no `model` kwarg) — unreachable in practice now that
   every (runtime, role) combination has a hardcoded default; kept
   as a defensive last resort for forward compatibility if a future
   runtime opts out of role defaults.

Project-switch behavior: the override is keyed by `(slot,
project_id)`. Switching the active project automatically swaps which
override is read, so Coach can run different model configurations on
different projects without cross-talk.

`coord_set_player_effort(player_id, effort)`

- Coach only.
- `player_id`: `p1` to `p10` (cannot set Coach's effort via MCP — ask
  the human).
- `effort`: one of `low` | `medium` | `high` | `max`. Empty string
  clears (revert to no override). Friendly aliases (`med` →
  `medium`) and the numeric tier `1..4` (1=low … 4=max) are also
  accepted for symmetry with the UI slider.
- Stored on `agent_project_roles.effort_override` (INTEGER 1..4) for
  the active project. Empty-clear on a row that doesn't exist is a
  no-op (no orphan row).
- Resolution at spawn time: per-pane request value (highest) → this
  Coach override → role-level default (medium for both Coach and
  Players, see `models_catalog._ROLE_EFFORT_DEFAULTS`).
- Emits `agent_effort_set` with `to: <player_id>` so the event
  renders in both Coach's pane and the target Player's pane (history
  reload uses the same indexed `payload_to` filter).

`coord_set_player_plan_mode(player_id, plan_mode)`

- Coach only.
- `player_id`: `p1` to `p10`.
- `plan_mode`: `on` | `off`. Empty string clears (revert to no
  override). Aliases: `true`/`1`/`yes` → on, `false`/`0`/`no` → off.
- Stored on `agent_project_roles.plan_mode_override` (INTEGER 0/1)
  for the active project. Empty-clear no-orphan invariant matches
  the other override tools.
- Resolution at spawn time: per-pane request value (highest) → this
  Coach override → role-level default (off for both Coach and
  Players, see `models_catalog._ROLE_PLAN_MODE_DEFAULTS`). Plan mode
  is heavy (every turn pauses for ExitPlanMode review before any
  tool use), so leave it off in the common case and use it only on
  Players doing destructive / hard-to-undo work where the human
  should review the approach first.
- Emits `agent_plan_mode_set` with `to: <player_id>`.

`coord_set_player_thinking(player_id, thinking)`

- Coach only.
- `player_id`: `p1` to `p10` (cannot set Coach's thinking via MCP —
  human uses the pane gear).
- `thinking`: `on` | `off`. Empty string clears (revert to no
  override → off). Aliases: `true`/`1`/`yes` → on, `false`/`0`/`no`
  → off.
- Stored on `agent_project_roles.thinking_override` (INTEGER 0/1).
  Empty-clear no-orphan invariant matches the other override tools.
- Resolution at spawn time: per-pane request value (highest) → this
  Coach override → off. **No role default** — thinking stays off
  unless explicitly set.
- Claude runtime only. When true at spawn, the runtime passes
  `thinking={"type":"enabled","budget_tokens":N}` to
  `ClaudeAgentOptions` (N = `HARNESS_THINKING_BUDGET_TOKENS`,
  default 8000, clamped ≥ 1024). On a Codex Player the value is
  stored but silently ignored — survives a runtime flip and
  applies on the next Claude turn.
- Middle rung of the Coach bump ladder, intended for
  `kind_fail_count >= 2` patterns: bump effort first
  (`coord_set_player_effort`); if that's at max or unhelpful, flip
  thinking on here (Claude only); only then bump the model tier
  (`coord_set_player_model`). NEVER change runtime — that's a
  human decision. Don't combine bumps in one step.
- Emits `agent_thinking_set` with `to: <player_id>` so the event
  renders in both Coach's pane and the target Player's pane.

`coord_get_player_settings(player_id?)`

- Coach only — read-only.
- `player_id`: optional. One of `p1..p10` or `coach` to scope to a
  single agent; omit for the full roster (coach + p1..p10).
- Returns a compact text table with one row per agent showing both
  the override value (what Coach set via the five `coord_set_player_*`
  tools — runtime / model / effort / plan-mode / thinking) and the
  resolved value (what the agent will actually run with on next
  spawn, after fall-through to role defaults). Coach is expected to
  call this BEFORE any `coord_set_player_*` so the team doesn't
  churn already-correct settings. The thinking column also tags
  Codex Players with a `*codex` marker since the override is inert
  on that runtime.

### 12.9 Interactive Question/Plan Tools

`coord_answer_question(correlation_id, answers)`

- Coach only.
- Resolves a pending Player `AskUserQuestion` routed to Coach.
- Emits `question_answered`.

`coord_answer_plan(correlation_id, decision, comments?)`

- Coach only.
- Resolves a pending Player `ExitPlanMode` routed to Coach.
- Decisions:
  - `approve`
  - `reject`
  - `approve_with_comments`
- Reject and approve-with-comments require comments.
- Emits `plan_decided`.

`coord_request_human(subject, body, urgency?)`

- Any agent.
- Emits `human_attention`.
- Does not block the tool caller.
- Urgency: `normal` or `blocker`.
- Sole spontaneous-outreach channel to the human. There is no
  `Telegram_send` MCP tool: the Telegram bridge in §18.3 is a
  server-side forwarder, not callable by agents. Two delivery
  paths from an agent's perspective:
  1. **Implicit reply** — when a turn was triggered by a human
     message (UI composer or Telegram inbound), the bridge auto-
     forwards the agent's accumulated `text` events on
     `agent_stopped`. No tool call needed; just reply normally.
  2. **Spontaneous** — `coord_request_human` always forwards via
     `human_attention`, regardless of what triggered the turn,
     and is the only way an agent can push to the human's phone
     when the human hasn't just written to them.
- Coach's and Players' system prompts spell this out in the
  `coord_request_human` description so agents don't hunt for a
  non-existent Telegram tool (`PushNotification` is a Claude CLI
  built-in unrelated to this bridge — explicitly disregarded in
  the Player prompt).

### 12.10 Context editing

There is no `coord_write_context` tool. Agents edit context files
with the standard `Write` tool.

Current rule:

- Global context is `/data/CLAUDE.md`.
- Project context is `/data/projects/<active>/CLAUDE.md`.
- Humans edit through Files pane.
- Coach may edit through standard `Write` only if its tool permissions allow
  the file write path in a future design; the current Coach baseline does not
  include `Write`.

Current UI mismatch:

- The `/tools` slash-command hardcoded list still displays
  `coord_write_context` for Coach even though the MCP server no longer
  registers it.

---

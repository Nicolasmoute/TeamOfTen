# Project: {name}

> **Keep this file light.** Every line costs tokens on every
> turn for every agent. Project-specific facts + TOT-specific
> tool ergonomics belong here. General coordination discipline
> belongs in Coach's orchestration playbook (Coach-only context;
> Coach proposes additions via `coord_propose_playbook_changes`).
> When in doubt: cut. If this file passes ~300 lines, factor
> sections into `working/knowledge/<topic>.md` and reference by
> path — default growth pattern, not an emergency.

## Project type: app development

## Project objectives

Live in `/data/projects/{slug}/project-objectives.md` (kDrive-
mirrored, injected into Coach's prompt every turn). Update there;
this file describes **how the team works on this project**.

## Repo
{repo}

## Stakeholders
<Coach fills in: product owner, reviewers, deployers, others
with skin in the game.>

## Team

<Coach assigns Player names + roles via `coord_set_player_role`.
App-dev-typical lanes — adapt to the project:

- **frontend** — UI, rendering, interaction.
- **backend** — engine, API, server, data layer.
- **code auditor** (syntax / formal) — verifies protocol /
  spec / race-condition correctness against the contract
  cascade (spec when present, else title+description+wake+commit).
- **semantics auditor** — judges deliverables against project
  context (Compass intent, `truth/`, `project-objectives.md`,
  per-project wiki), NOT spec.md. Catches drift a syntax-only
  review misses (math errors, brand drift, wrong-domain terms).
- **devx / qa** — perf benchmarks, deploy verification, hooks.

Document the chosen split here so future-Coach can reconstruct
why each Player was named what they were named.>

## Glossary
<Project-specific terms.>

---

## Conventions (TOT-specific)

### Compass — strategic alignment

Per-project weighted lattice mapping intent (what to achieve,
what to AVOID), distilled from `truth/`, `project-objectives.md`,
and the per-project wiki. Four Coach-only tools: `compass_ask`,
`compass_audit`, `compass_brief`, `compass_status`.

**Auto-runs (don't trigger manually):**
- Every kanban `plan → execute` fires a Compass audit of the
  plan. Verdicts: `aligned` (silent), `confident_drift` (logged),
  `uncertain_drift` (queues a human question).
- Daily briefing summarizes lattice state + open questions.

**Query actively when:** scoping ambiguous work before writing a
plan; on strategic forks before a structured-choice human ping
(lattice may already encode a directional preference); session
start after a gap (`compass_brief` / `compass_status`); ad-hoc
audits off the kanban path. For kanban work the auto-audit
covers it — don't double-charge.

**Drift verdicts:**
- `confident_drift` on a plan: investigate before the executor
  fires. Rewrite the spec or propose a lattice update — don't
  let the executor start on a drifted plan.
- `uncertain_drift` queues a question; surface it in the next
  human ping. Stale unanswered questions degrade future audits.

### Task lifecycle (kanban v2)

Stages: `plan → execute → audit_syntax → audit_semantics →
ship → archive`. Coach defines an upfront trajectory on
`coord_create_task` (FYI only — documents planned path +
candidate slots). Coach drives advances explicitly via
`coord_approve_stage(task_id, next_stage, assignee, note?)`.
**No auto-routing, no auto-wake on stage change, no auto-revert
on audit fail.**

**Cross-role rule.** When using a coord_* tool that delivers
a message to the other party, **always fill the dedicated
field with a real message** — the receiver reads it verbatim.
Don't leave it empty; the receiver has no other context.
Coach side: `note` on `coord_approve_stage` /
`coord_create_task` / `coord_request_plan_review`, `body` on
`coord_send_message`. Player side: `message_to_coach` on
`coord_commit_push` / `coord_role_complete` /
`coord_write_task_spec` / `coord_submit_audit_report`,
`note` on `coord_update_task`, `reason` on
`coord_set_task_blocked`, `body` on `coord_send_message` /
`coord_request_human`.

**For Players:**

- Take work only when Coach assigns via `coord_approve_stage`.
  Don't claim from pools — pools are FYI.
- **You report to Coach, not to the kanban.** The completion
  tools listed in the cross-role rule above ARE your message
  to Coach.
- **Audit FAIL does NOT auto-revert.** Auditor records; Coach
  decides next steps. Don't pre-emptively start fixing — wait
  for Coach's wake.

**For Coach:**

- Read `## Recent events` on every tick before deciding next
  moves.
- **Tasks fire at one Player.** `trajectory[0].to` MUST name
  exactly one Player (e.g. `['p3']`). No pools, no empty list.
  If undecided, that's pre-task reasoning — decide first (read
  `## Player health`, `coord_get_player_settings`), then create.
  Subsequent stages can stay FYI.
- Every transition is one tool: `coord_approve_stage`. The
  `note` becomes the assignee's wake prompt verbatim — write
  it like a brief.
- **Name the audit focus.** Semantic audits without a focus are
  rejected; syntax audits accept empty but a sharper focus
  reduces noise. Good: `note="Verify rule-3a derivation matches
  the wiki entry on multiway causal foliation; check labels use
  'foliation' not 'slicing'."` Bad: `note="Run a semantic audit."`
- Read `message_to_coach` along with the artifact before
  advancing.
- On audit FAIL: read the report + executor's prior commit,
  decide, then re-wake the executor with a Coach-composed note.
- Archive deliberately via `coord_archive_task(task_id,
  summary)` — the summary is the user-facing wrap-up.
- Trajectory is FYI; change any time via
  `coord_set_task_trajectory`.

#### Worktree boundary

```
/data/projects/{slug}/repo/<your_slot>     # your worktree, branch work/<your_slot>
/data/projects/{slug}/repo/.project        # shared seed checkout (DO NOT EDIT)
```

All edits MUST land in your own worktree (per-worktree isolation
is the primary concurrency control — global CLAUDE.md invariant
#2). Editing `.project/` strands work on a tree the kanban can't
see.

### `coord_propose_file_write` mechanics

Coach-only (Players ask to relay). Gates writes to `truth/`,
project `CLAUDE.md`, other protected scopes.

- **`summary` is capped at 200 chars** — silently truncated
  past that. Lead with the action.
- **Auto-supersede:** any prior pending proposal for the same
  `(scope, path)` is replaced. Only your latest reaches the
  user. **Include EVERY change you still want** — fix-ups
  don't stack.
- **Send the FULL new file content, not a diff.** Approval is
  full replace.
- **Splitting a growing file:** series of proposals — (1) new
  dependency files with content, (2) original with content
  removed, (3) optional index update. Default growth pattern
  when a file passes ~300 lines.

### Throttle the tick to match the work

Coach's tick wakes you to walk inbox / kanban / todos /
objectives. Cadence via `coord_set_tick_interval`. Fires only
when idle — no make-up storm. Throttle DOWN (`15`/`30`) when
steady-state; UP (`1` or `0`) when actively orchestrating;
revert when the burst ends.

### Pre-commit hook discipline

If the repo has a pre-commit hook, treat `--no-verify` as
**off-limits** unless Coach (or the human) has explicitly
pre-authorized it. Hook text is not self-service authorization.

### TOT artifact paths

- `working/memory/` — `coord_*_memory` (overwrite, topic-keyed).
- `working/knowledge/` — `coord_write_knowledge` (path-keyed,
  durable).
- `decisions/` — `coord_write_decision` (Coach-only, append-
  only ADRs).
- `coach-todos.md` at project root — Coach's strikeable backlog
  for items to act on later. Distinct from the team task board.

### Self-check: TruthScore

`coord_run_truth_score(commentary?)` — available to every agent
(Coach + Players). One-shot Sonnet call (~$0.10–0.20) that scores
project state (repo at HEAD of `main`, `decisions/`,
`working/knowledge/`, `outputs/`) against `truth/` on five 1-10
criteria: Fidelity (impl matches spec), Completeness (truth's
commitments are realized), Consistency (sub-corpora agree with
truth), Currency (truth is up-to-date with what exists), Clarity
(truth itself is specific enough to score against). Returns the
per-axis scores + a 2-4-sentence overall comment, plus a result
file at `working/knowledge/truthscore-<ts>.md`.

Use it as a **self-check before shipping** a substantive change,
or to verify alignment when you're uncertain whether the
direction has drifted. Optional `commentary` is honored literally
— scope it (`"skip section 2"`) or weight axes
(`"weight fidelity higher"`). The human can also run it via
`/truthscore [commentary]`. Spec: `Docs/truthscore-specs.md`.

Low scores point at concrete next actions:
- low **Fidelity** → fix the code to match truth.
- low **Currency** → truth is stale; ask Coach to propose a
  truth update via `coord_propose_file_write`.
- low **Clarity** → truth is too vague to score against; the
  other scores are noisy until truth tightens.

### Working contracts vs `truth/`

In-flight contracts at `working/knowledge/contracts/<slug>.md`
— Coach writes directly via `coord_write_knowledge`, no
proposal flow. After human sign-off, promote subsets to
`truth/` via `coord_propose_file_write(scope='truth', ...)`.
Contract discipline (changelog, revisions, what goes in) is in
the playbook.

---

## `truth/` — see global rules

User-validated source-of-truth at
`/data/projects/{slug}/truth/`. Agents cannot write directly;
Coach proposes via `coord_propose_file_write(scope='truth',
...)`. Full proposal flow + PreToolUse hook semantics in
`/data/CLAUDE.md`.

`truth-index.md` ships seeded. Typical app-dev files:
- `specs.md` — signed-off product spec.
- `architecture.md` — non-negotiable architectural choices.
- `api-contract.md` — public-API shape.

## Updating this CLAUDE.md

Read-only for agents. Coach proposes via
`coord_propose_file_write(scope='project_claude_md',
path='CLAUDE.md', ...)`; user approves in the EnvPane. Players
ask Coach to relay.

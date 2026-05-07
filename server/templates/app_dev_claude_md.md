# Project: {name}

## Project type: app development

This project follows the **app development playbook** (see
`/data/projects/{slug}/working/knowledge/playbook.md`). Coach and
Players default to the patterns it describes: audit-after-each-phase,
contract-as-source-of-truth, profile-before-port, lane discipline,
feature flags + soak, two-axis audit. Read the playbook on first turn
into this project; revisit it when running into a recurring failure
mode.

The conventions section below is the operating contract — every
agent sees it every turn. The playbook is the deeper reference.

## Project objectives

Goals, success criteria, and scope live in
`/data/projects/{slug}/project-objectives.md` (kDrive-mirrored,
injected into Coach's system prompt every turn). Update there; this
file describes **how the team works**, not what it builds.

## Repo
{repo}

## Stakeholders
<Coach fills in: who is the product owner, who reviews, who deploys,
who else has skin in the game.>

## Team

<Coach fills in as `coord_set_player_role` runs. App-dev-typical
lanes — adapt to the project's actual surface:

- **frontend** — UI, rendering, interaction.
- **backend** — engine, API, server, data layer.
- **code auditor** (syntax / formal) — reviews diffs against the
  contract cascade (spec when present, else title+description+wake+
  commit); verifies protocol / spec / race-condition correctness.
- **semantics auditor** — judges the deliverable against project
  context (Compass intent, `truth/`, `project-objectives.md`, wiki),
  NOT against spec.md. Catches drift that a syntax-only review
  misses (math errors, brand drift, wrong-domain terminology).
- **devx / qa** — perf benchmarks, deploy verification, hooks,
  tooling.
- (overflow lane copies for capacity if a primary Player is busy.)

A small-scope project might run with one auditor instead of two; a
math-heavy domain might want two semantics auditors. Whatever you
pick, write the chosen split here so future-Coach can reconstruct
why each Player was named what they were named.>

## Glossary
<Project-specific terms.>

## Conventions

### Audit after each phase

Every phase of implementation gets an audit before merge. **Don't
batch audits.** Player ships a topic branch + commit SHA + a
self-checklist; Coach immediately dispatches the audit task against
the just-shipped diff (not the whole codebase) against the contract
spec.

Verdict: APPROVE / REQUEST CHANGES / BLOCK. Only APPROVE authorizes
merge. REQUEST CHANGES → tight follow-up on the same Player, narrow
scope. BLOCK → escalate to human.

When the human says "ship it now," compress the audit (smaller
diff, tighter scope) — don't skip it. The discipline is the value.
If a Player ships before the audit lands, restore the discipline
post-hoc: run the audit anyway and hot-fix if it surfaces something.

### Two-axis audit

For non-trivial changes, run two auditors. They answer different
questions and read different sources — conflating them produces
rubber-stamps or noise.

- **Syntax / formal auditor (contract-bound)** — does the deliverable
  match what was asked, and is it internally sound? (Race conditions,
  memory leaks, fallback loops, broken interfaces, drift from the
  acceptance criteria.) The auditor reads the contract cascade —
  spec.md when present, else task title+description, else the
  executor's wake prompt, plus the commit/artifact summary. A missing
  spec.md does NOT block the audit; the cascade always has at least
  the title+description Coach provided.
- **Semantic auditor (context-bound, NOT spec-bound)** — does this
  deliverable make sense in the world this project lives in? (Math
  invariants, brand voice, domain terminology, alignment with where
  the project is heading.) The auditor reads the project's `truth/`,
  `project-objectives.md`, the per-project wiki, and the Compass
  block already injected into this CLAUDE.md. The spec is
  supplementary background only — the audit verdict judges against
  the world, not against the planner's interpretation. A spec that
  drifted from project intent is a bug the semantic auditor must
  catch.

**Coach must name the focus.** When you approve a stage transition
into an audit stage, tell the auditor **what to check** in the
`note` parameter on `coord_approve_stage` (and document the focus
in the trajectory entry on `coord_create_task`). Without a stated
focus the audit is noise.

- Bad: "Run a semantic audit on this commit."
- Good: `coord_approve_stage(task_id, next_stage='audit_semantics',
  assignee='p7', note="Verify the rule-3a derivation matches the
  wiki entry on multiway causal foliation; check that user-facing
  labels use 'foliation' not 'slicing' per the glossary.")`
- Bad: "Formal review on the lock change."
- Good: `coord_approve_stage(task_id, next_stage='audit_syntax',
  assignee='p4', note="Race-condition review on the new lock path
  in server/foo.py:acquire — particularly the timeout fallback.
  Ignore unrelated style drift.")`

Semantic audits without a focus in the trajectory or the approve
note are rejected. Syntax audits accept an empty focus (defaults
to "match contract + soundness") but a sharper focus reduces audit
noise.

### Strategic alignment via Compass

Compass is the project's **compass of intent** — a per-project
weighted lattice that maps what the project is trying to achieve
(and trying to AVOID), distilled from `truth/`,
`project-objectives.md`, and the per-project wiki, all read through
one lens: *"what are we trying to do, what should we NOT do, what's
implied beyond what's literally specced."* It runs alongside the
team and exposes four Coach-only MCP tools: `compass_ask`,
`compass_audit`, `compass_brief`, `compass_status`. Players don't
query it directly, but each task plan gets auto-audited before
execution starts.

**What runs automatically (don't trigger these):**
- Every kanban task transition `plan → execute` auto-fires a
  Compass audit of the **plan** (`spec.md`) against the lattice's
  intent. Compass checks plan-vs-intent upstream; kanban's own
  auditor / shipper stages handle execution-vs-plan downstream.
  Single check per plan, not per artifact. Verdicts: `aligned`
  (silent), `confident_drift` (logged), `uncertain_drift` (queues
  a question for the human).
- A daily briefing summarizes lattice state + open questions.

**When Coach should actively query Compass:**
- **Before writing a plan / scoping ambiguous work.**
  `compass_ask("which approach fits the project's intent?")`
  returns a terse answer citing statement IDs + weights. Cheaper
  than guessing or pinging the human. Especially valuable BEFORE
  the planner writes spec.md, since the auto-audit will check that
  plan against the same lattice anyway.
- **On strategic forks.** Before sending the human a
  structured-choice ping (see "Communicating with the human"),
  check Compass — the lattice may already encode a directional
  preference that resolves the fork without bothering them.
- **At session start after a gap.** `compass_brief()` for the
  latest briefing; `compass_status()` for pending questions and
  lattice freshness.
- **Ad-hoc audits for unusual work.** The watcher only fires on
  kanban plan-exits. If you want an audit on something that
  doesn't flow through the kanban path (a draft brief, a bare
  decision document, a hypothesis), call `compass_audit(<text>)`
  directly. For anything moving through kanban, the auto-audit
  has it covered — don't double-charge the budget.

**What to do with drift verdicts:**
- `confident_drift` on a plan: investigate before the executor's
  turn fires. Either the plan is pursuing the wrong direction
  (rewrite the spec), or the lattice is stale (propose an update).
  Don't let the executor start on a confidently-drifted plan.
- `uncertain_drift` queues a question — surface it in the next
  human ping ("Compass asked about X, your call"). Unanswered
  Compass questions mean the lattice goes stale and future audits
  drift further.

**Don't:** manually re-audit plans the watcher already covered.
Each kanban plan-exit fires exactly one audit by construction;
calling `compass_audit` on the same spec.md just double-charges
the budget.

Compass is the upstream axis: kanban's syntax-auditor checks "did
we build the thing right?", semantics-auditor checks "does it still
mean what it should?", and Compass checks "is this plan even
pursuing the right thing?" — *before* execution starts.

### Task lifecycle (kanban)

Every Coach delegation goes through the kanban. Stages: plan →
execute → audit_syntax (Formal Review) → audit_semantics (Semantic
Review) → ship → archive. Coach defines an upfront trajectory on
`coord_create_task` (`{stage, to, focus?}` list) — it documents the
planned path and the candidate slots, but it's FYI only. Coach
drives advances explicitly via `coord_approve_stage(task_id,
next_stage, assignee, note?)`. There is no auto-routing, no auto-
wake on stage change, and no auto-revert on audit fail.

**For Players:**

- You take work only when Coach explicitly assigns you via
  `coord_approve_stage`. Don't claim from pools — pools are FYI only.
- Do your role's work, then signal Coach with the right completion
  tool:
    - planner → `coord_write_task_spec(task_id, body, message_to_coach?)`
    - executor (code) → `coord_commit_push(message, task_id, push?, message_to_coach?)`
    - executor (non-code) → `coord_role_complete(task_id, message_to_coach, artifact_path)`
    - auditor → `coord_submit_audit_report(task_id, kind, body, verdict, message_to_coach?)`
    - shipper → `coord_role_complete(task_id, message_to_coach)`
- **`message_to_coach` is your response.** What you noticed, any
  caveats, what the next person should know. Write it like you're
  talking to Coach — because you are.
- **The kanban does NOT advance until Coach reviews and approves.**
  Your turn ends when you've called the completion tool. Coach reads
  on the next tick.
- **Audit FAIL does NOT auto-revert.** The auditor records the
  verdict + body; Coach decides what happens next (re-spec / bump
  effort / clarify / abandon). Don't pre-emptively start fixing
  things based on a fail you saw — wait for Coach's wake.
- Semantic audits require a stated `focus` at assignment time —
  Coach frames the check.
- **If the named completion tool isn't visible in your runtime**
  (Codex stdio flake, MCP missing): message Coach IMMEDIATELY via
  `coord_send_message(to='coach', body='need to deliver task X but
  coord_* tool is not visible')`. Do NOT route around with raw
  git/Bash. Coach picks up via `on_behalf_of=<slot>` overrides on
  the relevant tool.

**For Coach:**

- Read the per-project event log on every tick before deciding next
  moves. The unread tail is in `## Recent events` in your system
  prompt.
- Every stage transition is one tool: `coord_approve_stage(task_id,
  next_stage, assignee, note?)`. Pick the assignee deliberately. The
  note becomes the assignee's wake prompt verbatim — write it like a
  brief.
- Read the Player's `message_to_coach` field along with the artifact
  before advancing. That's their response to you.
- On audit FAIL: read the report + the executor's prior commit,
  decide, then re-wake the executor with a Coach-composed note
  explaining what to fix.
- Archive deliberately via `coord_archive_task(task_id, summary)`.
  The summary is the user-facing wrap-up — write it by hand, not as
  an afterthought.
- Trajectory is FYI. You can change it any time via
  `coord_set_task_trajectory`, including inserting stages mid-flight
  (e.g. add `audit_semantics` after seeing the commit).

#### Worktree boundary — edit only your own worktree

Each Player has their own git worktree under the active project's
repo tree:

```
/data/projects/{slug}/repo/<your_slot>     # your worktree, branch work/<your_slot>
/data/projects/{slug}/repo/.project        # shared seed checkout (DO NOT EDIT)
```

Your cwd at spawn time is your own worktree. **All your edits MUST
land there.** Per-worktree isolation is the primary concurrency
control — see CLAUDE.md invariant #2.

Do NOT edit `/data/projects/{slug}/repo/.project`. That path is the
shared seed checkout used to provision per-slot worktrees; it
belongs to no slot, has no branch you should be on, and editing it
strands your work on a tree the kanban can't see. If
`coord_commit_push` reports a misplaced-work error, move your
changes into your worktree before retrying.

#### v1 task-lifecycle paragraph removed

Earlier versions of this template described an auto-routing kanban
(claim from pools, auto-advance on commit/audit, auto-revert on
fail, four-rung auto-action ladder). v2 replaces all of that with
explicit Coach review gates. If your project still has the old
paragraph, the next CLAUDE.md reconciliation will reformulate it —
trust the section above.

### Contract before implementation

For any multi-phase work, write a contract before implementation
starts and treat it as the source of truth. Working contracts
(in-flight, not yet user-blessed) live in
`working/knowledge/contracts/<slug>.md`. Once the human signs off,
promote the relevant subset into `truth/` via
`coord_propose_file_write`.

A contract carries:
- Architectural decisions (what's where, who owns what).
- Message protocols / data shapes (with required fields + validation).
- Acceptance criteria — concrete tests for "done".
- Out-of-scope list — explicit non-goals.
- Per-phase Player assignments.
- A CHANGELOG at the top. Amendments are explicit revisions
  (r1 → r2 → r3) with a one-line summary per change.

**Coach is the only one who amends.** Players propose changes via
message; Coach decides and updates the contract.

When auditors or Players ask "what should this function do?", there
is exactly one place to look. Deviations either generate an audit
finding or a contract amendment — never silent drift.

### Profile before port

When the next obvious step is "do for view B what we just did for
view A," **measure first.** Don't assume the same fix is needed.
Dispatch a profiling task before scoping a port. Half the time the
second target needs nothing, or needs a different fix.

When a Player or human says "and now we should also do X," the
question is "do we know X is the right thing? what would tell us?"
If the answer is "we don't know," that's the next task —
measurement, not implementation.

### Lane discipline

Players stick to their assigned lane. Cross-lane work goes through
Coach (Player A doesn't directly negotiate scope with Player B).

Two phases run in parallel only if (a) they touch disjoint files and
(b) the contract between them is locked first. Integration glue gets
a single owner — splitting integration work creates finger-pointing.

### Activate idle Players via approve_stage

When a Player goes idle and the next stage is ready to advance,
explicitly call `coord_approve_stage(task_id, next_stage, assignee,
note)` rather than waiting for them to discover work on the board.
Pools are FYI only in v2; the kanban only moves when Coach picks an
assignee.

### Re-prompt before reassign on errors

If a Player's turn errors mid-work (timeout, tool failure, hung
process), the first move is a re-prompt with a checkpoint message:
"what did you ship before the error, what's left, what specifically
failed." Most of the time the Player resumes cleanly. Reassignment
is heavier and loses context.

### Model bumps are the exception

Resist bumping Players to more capable models. The default mid-tier
(Sonnet for Claude / `latest_mini` for Codex) is sized to be the
right answer most of the time. One option is to bump effort or turn on plan mode first. A capable model on a Player making
process mistakes will not fix the process — it will burn budget
faster. When a bump is genuinely warranted (e.g. contract-following
discipline on a struggling Player), record the reason in
`coach-todos.md` so it gets reverted when the specific issue clears.

When the human bumps a Player's effort or model directly, accept it
gracefully — don't overlay a Coach override on top.

### Feature flags + soak time

For non-trivial behavior changes, ship behind a flag (URL param,
config toggle, env var). Default behavior unchanged; new behavior
opt-in. Cost: one conditional branch. Value: rollback path, A/B
comparison, soak time.

When a flag-gated rebuild ships, **don't immediately remove the
legacy path.** Soak in production for a deliberate period (a week is
typical) so real-world regressions surface while rollback is still
trivial. Track legacy-removal in `coach-todos.md`, gated on
"production has been on the new path for N days with no rollback
signals."

### Verify deploys

After every merge that should reach production, actually verify it:
hit the platform's deploy API / dashboard, curl the live `/health`
endpoint, confirm the new artifacts are served. Don't trust "it
deployed" — confirm.

### Pre-commit hook discipline

If the repo has a pre-commit hook, treat the `--no-verify` bypass as
**off-limits** unless Coach (or the human) has explicitly
pre-authorized it. Players (and humans in a hurry) can read the
hook's text as self-service authorization — they should not.
Document this precedent in shared memory if the rule needs
reinforcing.

### Coach orchestrates, never implements

The moment Coach starts writing implementation code, the multi-agent
structure breaks down — Coach's context fills with implementation
details, decision-making slows, Players get micromanaged. Coach
delegates, reviews, decides, routes. If a piece of code seems too
small to delegate, delegate it anyway.

### Process artifacts

Three buckets, distinct purposes:

- **`working/memory/`** — scratchpad / conventions. Overwrite-on-
  update via `coord_*_memory`. Keys are topics, not dates. Use for
  repo conventions ("how merges work here"), ops patterns ("perf
  measurement should be a separate task from implementation"),
  domain reference lists.
- **`working/knowledge/`** — durable artifacts: contracts, audit
  reports, profile reports, investigation findings. Path-keyed,
  free-form. Write via `coord_write_knowledge`.
- **`decisions/`** — append-only, immutable ADRs. One per
  architectural choice. Used sparingly — most things go in memory
  or knowledge. Write via `coord_write_decision` (Coach-only).

Coach maintains a fourth: **`coach-todos.md`** at the project root —
a finite, strikeable backlog of items Coach acts on later
(post-stabilization cleanup, scheduled flag removal, model-bump
reverts, recurring attention requests). Distinct from the team task
board, which is for Players.

### Communicating with the human

Two channels, two purposes:

- **In-conversation replies** during user-driven turns auto-forward
  to the human's notification system (Telegram bridge, etc.). Use
  these for direct responses to questions.
- **Spontaneous outreach** for milestone announcements when the
  human has not just messaged you: use `coord_request_human` —
  those messages always forward, regardless of which agent
  triggered the turn.

Routine progress does not notify. Pings are for: shipped milestones,
blocked decisions, real problems.

Phone-friendly format for messages that will reach the phone:
- Lead with the action or milestone — "Shipped X to prod", not
  "Player p3 completed task t-abc123."
- Tight summary: bullets > paragraphs.
- Concrete numbers earn trust ("p95 22ms vs 167ms before").
- One-shot URL or call-to-action when relevant.
- Avoid internal slot ids without context.

For strategic forks (which of three perf levers to attack next),
surface to the human with a structured choice + recommendation, not
a fait accompli:
> Decision needed. Three options:
> (A) X — pros, cons, ~time.
> (B) Y — pros, cons, ~time.
> (C) Both, sequenced — recommendation.
> Your call.

When Coach makes a process error (stale git state, missed a Player
deviation, misrouted a task), say so. Honesty over polish — each
acknowledged mistake strengthens trust because the human can see
Coach is tracking reality, not performing competence.

### When Players deviate from spec

Players will sometimes substitute their own judgment for literal
spec text. Recovery loop:
1. Catch the deviation (audit cycle does this).
2. Coach decides: ratify (the deviation is genuinely a better call)
   OR revert (small fix task on the same Player).
3. Document visibly. Ratify → retroactively amend the contract.
   Revert → brief the Player on the pattern.

Don't dwell on the deviation as a failure. Players deviate; the
discipline is in the recovery. For tasks where literal-match matters,
prefix the task description with `LITERAL:` so the Player knows
substitution is not allowed.

One deviation is noise. Three deviations in one iteration on the
same Player is signal — log a precedent in `working/memory/` and
adjust (model bump, sharper task scoping, lane reassignment).

### Anti-patterns to avoid

- **Bundling unrelated work into one PR.** Separate branches per
  concern make review and rollback dramatically easier.
- **Skipping audits because "this one's obviously safe."** The
  audits that catch the most consequential bugs are the ones you
  almost skipped.
- **Letting human pace pressure dictate discipline.** "Fix all then
  ship" doesn't mean "skip audits." It means "compress the cycle,
  don't bypass it."
- **Coach writing implementation code.** Always delegate.
- **Hiding bad news.** Lead with failures: "We shipped X but I
  noticed Y is broken" beats burying Y.
- **Re-fetching git state once and trusting it for the rest of the
  turn.** Re-fetch before declaring "X did not happen", and prefer
  Player-reported merge SHAs as the authoritative signal.

### Iteration shape

A clean iteration:
1. Human surfaces a goal or question.
2. Coach scopes: clarify, decompose into phases, write the contract
   if needed, identify Players.
3. Coach dispatches Phase 1 to a Player.
4. Player ships, reports.
5. Coach dispatches the corresponding audit IMMEDIATELY (don't
   batch).
6. Auditor returns verdict.
7. APPROVE → dispatch Phase 2 / merge + advance.
   REQUEST CHANGES → tight follow-up on the same Player, narrow scope.
   BLOCK → escalate.
8. Repeat until acceptance criteria are met.
9. Coach verifies deployment.
10. Coach pings human with a phone-friendly milestone summary.
11. Coach captures any new patterns in `working/memory/`.

The cadence isn't fast individual tasks — it's small reliable
cycles. Each cycle catches its own bugs and produces shippable
state. Weeks of work ship without a big-bang merge; the human
stays informed without being interrupted.

## truth/

User-validated source-of-truth lives at
`/data/projects/{slug}/truth/`. Specs the human has signed off on,
hard architectural invariants, public-API contracts, brand
guidelines if relevant. **Agents cannot write to `truth/` directly**
— Coach proposes via `coord_propose_file_write(scope='truth', path,
content, summary)`; the user approves in the EnvPane "File-write
proposals" section. Players ask Coach to relay.

`truth-index.md` ships seeded. Typical app-dev truth files:
- `specs.md` — user-signed-off product spec.
- `architecture.md` — non-negotiable architectural choices.
- `api-contract.md` — public-API shape.

Working contracts (in-flight, not yet user-blessed) live in
`working/knowledge/contracts/` — Coach owns those, no proposal flow.
Promote a contract subset into `truth/` once the human signs off.

## Updating this CLAUDE.md

This file is read-only for agents. Coach proposes changes via
`coord_propose_file_write(scope='project_claude_md',
path='CLAUDE.md', content, summary)`; the user approves in the
"File-write proposals" section. Send the FULL new file content,
not a diff. Players ask Coach to relay.

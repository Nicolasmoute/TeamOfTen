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
- **code auditor** — reviews diffs against the contract; verifies
  protocol / spec / race-condition correctness.
- **semantics auditor** — verifies the implementation preserves the
  meaning of the underlying domain model. Independent of the code
  auditor; they catch different bug classes.
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

For non-trivial changes, run two auditors:

- **Code auditor** — does the code do what the spec says? Race
  conditions, memory leaks, fallback loops, freshness bugs.
- **Semantics auditor** — does the implementation preserve the
  meaning of the underlying domain model? Data shapes, mathematical
  invariants, semantic UX deviations from spec wording.

They catch different bug classes. Don't collapse them.

### Strategic alignment via Compass

Compass is the project's strategic safety net — a per-project
weighted lattice of goals, constraints, stakeholder preferences, and
architectural commitments, distilled from `truth/`,
`project-objectives.md`, and the per-project wiki. It runs alongside
the team and exposes four Coach-only MCP tools: `compass_ask`,
`compass_audit`, `compass_brief`, `compass_status`. Players don't
query it directly, but their work gets auto-audited.

**What runs automatically (don't trigger these):**
- Every Player commit, Coach decision, knowledge artifact, and
  binary output is auto-audited against the lattice as it lands.
  Verdicts: `aligned` (silent), `confident_drift` (logged),
  `uncertain_drift` (queues a question for the human).
- A daily briefing summarizes lattice state + open questions.

**When Coach should actively query Compass:**
- **Before scoping ambiguous work.** `compass_ask("which approach
  fits the stated priorities?")` returns a terse answer citing
  statement IDs + weights. Cheaper than guessing or pinging the
  human.
- **On strategic forks.** Before sending the human a
  structured-choice ping (see "Communicating with the human"),
  check Compass — the lattice may already encode a preference that
  resolves the fork without bothering them.
- **At session start after a gap.** `compass_brief()` for the
  latest briefing; `compass_status()` for pending questions and
  lattice freshness.
- **For plans / contracts BEFORE they ship.** The auto-audit
  watcher catches shipped artifacts; it does NOT catch in-flight
  plans. Run `compass_audit(<draft contract>)` before locking a
  phase on non-trivial work — strategic drift caught at the plan
  stage is much cheaper than at the merge stage.

**What to do with drift verdicts:**
- `confident_drift` on a recently-merged artifact: investigate.
  Either the merge was wrong (revert + redo), or the lattice is
  stale (propose an update). Don't ignore.
- `uncertain_drift` queues a question — surface it in the next
  human ping ("Compass asked about X, your call"). Unanswered
  Compass questions mean the lattice goes stale and future audits
  drift further.

**Don't:** manually call `compass_audit` on artifacts the watcher
already covered (commits, decisions, knowledge artifacts, binary
outputs). Double-charges the budget.

Compass is the third axis: code-audit checks "did we do the thing
right?", semantics-audit checks "does it still mean what it should?",
Compass checks "is this the thing we should be doing at all?"

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

### Activate idle Players via push-assign

When a Player goes idle, push-assign the next task with
`coord_assign_task` rather than waiting for them to discover an open
task on the board. Direct assignment activates more reliably.

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

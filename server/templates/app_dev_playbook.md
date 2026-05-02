# Playbook — running a coordinated multi-agent coding project

A playbook extracted from a real session that took a browser-based
graph visualization through a render-architecture rebuild (canvas +
Web Worker), several follow-up perf fixes, a UI feature, and a
math-correctness fix — end-to-end from idea to deployed-on-prod,
across roughly 20+ shipped commits and ~12 audit reports.

The project shape this assumes:
- One human operator acting as product owner.
- One coordinator agent (Coach) that decomposes work, dispatches,
  routes.
- A handful of specialist Players with clear lanes — frontend,
  backend, math/semantics, code review, QA / DevX.
- A shared task board, shared memory, and a single source repo.

The patterns below are not specific to any tech stack. They
generalize.

This file is a **reference artifact**, not the operating contract.
The operating contract lives in the project root `CLAUDE.md`, which
is injected into every turn. This playbook is the deeper "why" and
"how" — read on first turn into a project, revisit when running
into a recurring failure mode.

---

## 1. The central discipline: audit-after-each-phase

The single most leveraged pattern in the session was inserting an
audit step between every implementation phase, even when work felt
obviously safe and even when the human was pushing for speed.

**Why it pays off:**
- Every audit cycle in the session caught at least one real bug,
  including ones that would have required production hot-fixes if
  they had shipped.
- Audits caught: stale-tick race conditions, doubled physics graphs,
  fallback loops, partial-update state regressions, hull cache
  invalidation gaps, interaction freshness bugs, semantic UX
  deviations from spec wording.
- Cost per audit was ~5-10 minutes; cost of catching the same bug
  post-merge was much higher (additional iteration + a redeploy +
  user re-testing).

**How to structure it:**
1. Player implements, pushes to a topic branch (NOT to main directly).
2. Player reports the branch + commit SHA + a self-checklist.
3. Coach immediately dispatches the audit task to the auditor agent.
   Don't wait until "everything is done" — audit each phase as it
   lands.
4. Auditor reads the diff, not the whole codebase, against a
   checklist tied to the contract spec.
5. Audit verdict is APPROVE / REQUEST CHANGES / BLOCK.
6. Only after APPROVE does Coach authorize merge.

**Two-axis audit beats one:**
- One auditor focuses on code / visual / protocol correctness — does
  the code do what the spec says? Race conditions, memory leaks?
- A second auditor focuses on **domain semantics** — does the
  implementation preserve the meaning of the underlying model? Do
  the data shapes still represent what they're supposed to represent?

These two lenses caught different classes of bugs. In the session,
the semantics auditor caught a doubled-physics-graph issue that the
code auditor's checklist didn't surface — the code was correct in
isolation but mathematically wrong in context.

**When the human pushes "ship it now":**
- Do not skip the audit. Compress it (smaller diff, tighter scope)
  but do not skip it. The discipline is the value.
- If a Player ships without waiting for audit (it happened in the
  session), Coach restores the discipline post-hoc by dispatching
  the audit anyway. If the audit finds something, hot-fix; if it's
  clean, the discipline is preserved for next time.

---

## 2. Profile before port

When the next obvious step is "do for view B what we just did for
view A," **measure first.** Don't assume the same fix is needed.

In the session: after a multi-day canvas + worker rebuild for one
view, the obvious next move looked like "do the same for the other
two views." Instead Coach dispatched a 30-minute profiling task. The
result: one view needed nothing (was already fast), the other needed
a totally different fix (remove the force simulation, replace with
O(N) static layout) — a result simpler, faster, and semantically
better than the originally proposed port.

**Pattern:** when a Player or human says "and now we should also do
X," ask "do we know X is the right thing? what would tell us?" If
the answer is "we don't know," that's the next task — measurement,
not implementation.

The cost of one measurement task is much less than the cost of a
misdirected port.

---

## 3. Contract documents

For any non-trivial multi-phase work, write a contract document
before implementation starts and treat it as the source of truth.

**What goes in a contract:**
- Architectural decisions — what's where, who owns what.
- Message protocols / data shapes, with required fields and
  validation rules.
- Acceptance criteria — concrete tests that determine "done".
- Out-of-scope list — explicit non-goals, preventing scope creep.
- Per-phase task assignment.

**Versioning:**
- The contract has a CHANGELOG at the top.
- Amendments are explicit revisions (r1 → r2 → r3) with a one-line
  summary per change.
- Coach is the only one who amends. Players propose changes via
  message; Coach decides and updates the contract.

**Why this works:** when an auditor or a Player asks "what should
this function do?", there is exactly one place to look. When
implementations deviate from the contract, the deviation is explicit
and gets either an audit finding or a contract amendment, never
silent drift.

In the harness: working contracts live in
`working/knowledge/contracts/<slug>.md`. Once the human signs off on
the relevant subset, promote it into `truth/` via
`coord_propose_file_write`.

---

## 4. Player coordination

### Clear lanes

Assign each Player a clearly-defined lane and stick to it. In the
session:
- **frontend** — UI, rendering, interaction.
- **frontend recovery** — overflow capacity for the same lane, used
  when the primary frontend Player was busy.
- **backend** — engine, API, server.
- **math / semantics auditor** — verifies that implementations
  preserve the meaning of the underlying domain model.
- **code auditor** — verifies that implementations preserve the
  protocol / spec / acceptance criteria.
- **devx / qa** — perf benchmarks, deploy verification, hooks,
  tooling.

When work needs to cross lanes (e.g. a frontend Player needs the
backend contract clarified), Coach mediates — Players don't
directly negotiate scope across lanes.

### Parallel vs. serial work

Two phases can run in parallel **if and only if** they touch
disjoint files **and** the contract between them is locked first. In
the session, one Player implemented a worker thread while another
implemented the canvas renderer in parallel — they didn't conflict
because the contract froze the protocol surface and the file
ownership was disjoint.

Phases that must be serial (e.g. integration glue that consumes both
parallel outputs) get a single owner. Splitting integration work
creates finger-pointing.

### Push-assign vs. self-claim

When a Player goes idle, push-assign the next task rather than
waiting for them to discover it on the board. Players activate more
reliably on direct assignment than on free-floating open tasks. In
the session this was the difference between "Player picks up work
in 30 seconds" and "Player sits idle for several turns until
something pings them."

### Re-prompt instead of reassign on errors

If a Player's turn errors mid-work (timeout, tool failure, hung
process), the first move is to re-prompt with a checkpoint message:
"what did you ship before the error, what's left, what specifically
failed." Most of the time the Player resumes cleanly. Reassignment
is heavier and loses context.

### Model selection is the exception, not the rule

Resist the temptation to bump Players to more capable models. The
default mid-tier model is sized to be the right answer most of the
time, and a capable model on a Player who is making process
mistakes won't fix the process — it will just burn budget faster.
When you do bump (e.g. for contract-following discipline on a
struggling Player), set a reminder to revert when the specific issue
clears.

When the human bumps a Player's effort or model directly, accept it
gracefully and don't overlay a Coach override on top.

---

## 5. Communicating with the human

### Two channels, two purposes

- **In-conversation replies** (during user-driven turns) auto-forward
  to whatever notification system the human uses (Telegram bot,
  push notif, etc.). Use these for direct responses to questions.
- **Spontaneous outreach** for milestone announcements when the
  human has not just messaged you. Use a dedicated escalation tool
  (in this harness: `coord_request_human`) — those messages always
  forward, regardless of which agent triggered the turn.
- Routine progress does not notify. Don't ping the human every time
  a task lands. Pings are for: shipped milestones, blocked
  decisions, real problems.

### Phone-friendly format

When a message will reach the human's phone:
- Lead with the action or milestone — "Shipped X to prod", not
  "Lin completed task t-abc123."
- Tight summary: bullets > paragraphs.
- Concrete numbers earn trust ("p95 22.6ms vs 167ms before").
- One-shot URL or call-to-action when relevant.
- Never mention internal agent names without context.

### Surfacing decisions vs. autonomously deciding

Coach's authority covers tactical execution. For strategic forks
("which of these three perf levers do we attack next?"), surface to
the human with a structured choice + recommendation, not a fait
accompli.

Pattern that works:
> Decision needed. Three options:
> (A) X — pros, cons, ~time.
> (B) Y — pros, cons, ~time.
> (C) Both, sequenced — recommendation.
> Your call.

This earns trust and keeps the human in the loop on direction
without slowing routine work.

### Honesty over polish

When Coach makes a mistake — wrong git state, missed a Player
deviation, misrouted a task — say so. The session had several of
these ("my earlier fetch was stale, sorry"). Each one strengthened
trust because the human could see Coach was tracking reality, not
performing competence.

---

## 6. Process artifacts

Capture institutional knowledge in three buckets, each with a
different purpose:

### Memory — scratchpad / conventions

Overwrite-on-update. Keys are topics, not dates. Use for:
- Repo conventions ("how merges work here").
- Ops patterns ("perf measurement should be a separate task from
  implementation").
- Domain reference lists ("the seven perf levers we have identified").

In the harness: `working/memory/` via `coord_*_memory`.

### Knowledge — durable artifacts

Path-keyed, free-form. Use for:
- Contract documents.
- Audit reports (date-stamped).
- Profile / measurement reports.
- Investigation findings.

In the harness: `working/knowledge/` via `coord_write_knowledge`.

### Decisions / ADRs — immutable

Append-only. One per architectural choice. Used sparingly — most
things go in memory or knowledge. Use for: "we chose X over Y
because Z, and this is binding."

In the harness: `decisions/` via `coord_write_decision` (Coach-only).

### Coach-todos

A finite, strikeable backlog of items Coach needs to act on later.
Distinct from the team task board (which is for Players). Examples:
- "After X stabilizes, dispatch follow-up cleanup."
- "Revert Player Y's effort bump once Z clears."
- "Audit and possibly remove dead code Z next time someone touches
  that file."

Coach-todos are how Coach maintains continuity across long work
sessions and conversation compactions.

In the harness: `coach-todos.md` at the project root.

---

## 7. Shipping discipline

### Feature flags

For non-trivial behavior changes, ship behind a flag. Default
behavior unchanged; new behavior opt-in. The cost is small (one
conditional branch); the value is huge (rollback path, A/B
comparison, soak time).

The session shipped a render-architecture change behind
`?renderer=canvas`. The default URL stayed bit-identical to old
behavior, which meant the human's first tests didn't see any change
(reported as disappointing) — but it also meant that when a perf
cliff was found, nothing was burning in production.

### Soak time before removing the legacy path

When a flag-gated rebuild ships, don't immediately remove the legacy
code path. Let the new path soak in production for a deliberate
period (a week is typical) so any real-world regressions surface
while rollback is still trivial.

In the session this pattern was followed: SVG renderer removal was
on the backlog gated on "production has been on canvas for a week
with no rollback signals."

### Pre-commit hook discipline

A main branch protected by a pre-commit hook is good. A hook with a
self-described `--no-verify` bypass is dangerous — Players (and
humans in a hurry) can read the hook's text as self-service
authorization. The correction: explicit Coach (or human)
pre-authorization required, even when the hook itself describes the
bypass path. Document this precedent in memory so the next person
doesn't repeat the mistake.

### Concrete deployment verification

After every merge that should reach production, actually verify it:
- Hit the platform's deploy API / dashboard.
- Curl the live `/health` endpoint.
- Verify the new artifacts are served (canvas renderer file
  accessible, worker file has the right protocol version, etc.).

Don't trust "it deployed" — confirm.

---

## 8. Things that will go wrong (and how to handle them)

### Players misinterpret task specs

Even with explicit contracts, Players sometimes substitute their own
judgment for the spec. The session had several small instances: a
Player kept a button enabled instead of disabling it as specified;
another shipped a "graph too large" placeholder instead of the
truncated-render approach the spec called for; another reversed a
`slice(-N)` to `slice(0, N)`.

**Recovery:**
1. Catch the deviation (audit cycle does this).
2. Coach decides: ratify (the deviation is genuinely a better call)
   OR revert (small fix task).
3. Document the decision visibly. Ratify → retroactively amend the
   spec; revert → brief the Player on the pattern.

After the second instance, log a process note in memory and adopt a
convention: for tasks with literal-match requirements, mark them
`LITERAL:` in the description.

**Don't:** dwell on the deviation as a failure. Players will deviate.
The discipline is in the recovery, not the prevention.

### Stale state in Coach's view

Coach's `git fetch` returns the state at fetch time. If a Player
pushes between Coach's fetch and Coach's interpretation, Coach can
be wrong about whether work landed. Always re-fetch before declaring
"X did not happen," and prefer Player-reported merge SHAs as the
authoritative signal.

### Recurring tool errors

If a Player keeps timing out around a specific tool (e.g.
browser-driven profiling consuming the full tool-call budget), don't
keep re-prompting. Investigate the pattern. Convention adopted in
the session: "perf-measurement should be a separate task from
implementation, so the tool-call budget effectively resets between
phases." This convention went into ops-patterns memory for future
Coach decisions.

### Branch convergence

When multiple parallel topic branches need to merge, Coach
coordinates merge order. Players don't rebase each other's WIP
without coordination. The pattern that works: parallel topic
branches → independent audits → squash-merge in dependency order.

### Coach makes a process error

Acknowledge it explicitly to the affected agents. In the session:
Coach accepted a Player's UX deviation in a 1-on-1 message without
updating the task spec or notifying the auditor; the auditor then
flagged the deviation correctly. The right response was "you're
correct given your context; Coach overrode without telling you,
here's the override now." Accept the audit finding, communicate the
override, and update memory with a precedent for next time.

---

## 9. Anti-patterns to avoid

- **Bundling unrelated work into one PR.** Even when convenient,
  separate branches per concern make review and rollback
  dramatically easier.
- **Skipping audits because "this one's obviously safe."** The
  audits that catch the most consequential bugs are the ones you
  almost skipped.
- **Letting human pace pressure dictate discipline.** "Fix all then
  ship" doesn't mean "skip audits." It means "compress the cycle,
  don't bypass it."
- **Coach writing implementation code.** Coach delegates, reviews,
  decides, routes. The moment Coach starts writing implementation
  code, the multi-agent structure breaks down — Coach's context
  fills with implementation details, decision-making slows, Players
  get micromanaged.
- **Ignoring a Player's process pattern across multiple incidents.**
  One deviation is noise; three deviations in one iteration is
  signal. Log a precedent and adjust.
- **Hiding bad news.** When something fails, lead with it. "We
  shipped X but I just noticed Y is broken" beats "X is shipped
  (and Y is broken but I'll mention it later)."

---

## 10. The shape of a good iteration

A clean iteration in this style looks like:

1. Human surfaces a goal or question.
2. Coach scopes: clarify, decompose into phases, write contract if
   needed, identify Players.
3. Coach dispatches Phase 1 implementation.
4. Player ships, reports.
5. Coach dispatches the corresponding audit IMMEDIATELY (don't
   batch).
6. Auditor returns verdict.
7. APPROVE → dispatch Phase 2 (or merge + advance).
   REQUEST CHANGES → tight follow-up task on the same Player, narrow
   scope.
8. Repeat until acceptance criteria met.
9. Coach verifies deployment.
10. Coach pings human with a milestone summary.
11. Coach captures any new patterns / precedents in memory.

The cadence isn't fast individual tasks; it's small reliable cycles.
Each cycle catches its own bugs and produces shippable state. The
result: weeks of work ship without the panic of a big-bang merge,
and the human stays informed without being interrupted.

---

## Closing note

This playbook reflects what worked in one specific session. Some of
it is likely overfit to that domain (browser-based perf work, graph
visualization). The patterns most likely to generalize:
- the audit discipline,
- the profile-before-port habit,
- the contract-as-source-of-truth convention,
- the two-bucket memory system (scratchpad vs durable),
- and the strict separation of Coach (orchestration) from Players
  (implementation).

What you should actively re-evaluate for your next project: the
specific roster of Player roles, whether you need two auditors or
one, whether your domain has the kind of measurable acceptance
criteria that make the audit cycle productive, and how much overhead
the contract-and-audit loop adds relative to the cost of bugs
slipping through.

Treat this as a starting hypothesis, not a recipe.

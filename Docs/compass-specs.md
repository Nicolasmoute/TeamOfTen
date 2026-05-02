# Compass — Specification

**Status:** Design specification, ready for implementation
**Target:** TeamOfTen multi-agent harness (Python, Claude Agent SDK, kDrive-backed shared state, single-VPS)
**Version:** 0.2

---

## 0 · One-paragraph summary

Compass is an autonomous strategy engine that runs alongside (not inside) the TeamOfTen coordinator. It maintains a **lattice of statements** about the project — atomic claims, each with a probability-of-truth weight in [0, 1] — and a small set of **truth-protected facts** that are sacred. Compass updates the lattice from human signals, asks the human focused questions when uncertainty is highest, and exposes its current best guess to the coach via a query interface and a daily briefing. It also audits work artifacts on coach demand. Compass never dispatches work, never amends truth without human approval, and never holds up worker activity. Its job is to be a quiet, learning source of project direction.

---

## 1 · Conceptual model

### 1.1 The lattice

The world model is a list of **statements**. Each statement is an atomic, falsifiable claim about the project.

A statement is good if:
- It can be answered with "yes" or "no" by the human if asked directly
- It is at the right granularity — coarse enough that the LLM can infer fine specifics from it, fine enough that workers and coach can act on it as a constraint
- Its negation is meaningful (a confident NO is as useful as a confident YES — coord uses the negation as a binding fact)

Examples of well-formed statements (project = Stripe billing for TeamOfTen):
- "Pricing model favors usage-based over flat-rate" (region: pricing)
- "First customers will be small teams of 2–10 people" (region: customers)
- "Stripe webhooks must be processed idempotently" (region: architecture)

Examples of poorly-formed statements (avoid):
- "Pricing strategy" (not falsifiable, not a claim)
- "Stripe webhook signatures must be verified using HMAC-SHA256 with timing-safe comparison" (too specific — the LLM can infer this from a higher-level statement)
- "Should we use Stripe?" (a question, not a statement)

### 1.2 Weights

Each statement carries a `weight ∈ [0.0, 1.0]` interpreted as **P(statement is true)**.

| Range | Meaning | Coach should treat as |
|---|---|---|
| `> 0.85` | Confident YES — eligible to settle | Binding constraint |
| `0.65 – 0.85` | Leaning yes | Working hypothesis, verify when cheap |
| `0.35 – 0.65` | Genuine uncertainty | Do not commit expensive work; needs human input |
| `0.15 – 0.35` | Leaning no | Working anti-hypothesis |
| `< 0.15` | Confident NO — eligible to settle (negation is binding) | The negation is a binding constraint |

The job of the loop is to **push weights away from 0.5** toward whichever pole reality lives at. Drift in either direction is progress. Sitting at 0.5 means compass has learned nothing about that statement.

### 1.3 Regions

Each statement carries exactly one **region tag** — a short string like `pricing`, `auth`, `deployment`, `customers`. Regions are compass-managed taxonomy, not human-curated.

Rules:
- A statement's region is set when the statement is created (by compass, never by the human)
- When creating a new statement, compass picks from existing regions and **only invents a new region when no existing region fits**
- The active region count has a soft cap of **15** and a hard cap of **20**
- When over soft cap, compass autonomously merges close regions on the next run — no human approval needed
- Region merges re-tag all statements (active and archived) that used the deprecated regions
- Region merges are logged in run history for traceability

Why regions matter:
- They give coach a coarse map of the project's territory ("we have 8 statements about pricing, 0 about deployment")
- They surface coverage gaps — under-populated regions become a question-generation priority
- They keep the human's mental model of the project taxonomy small and stable

### 1.4 Truth

### 1.4.1 The world model and its sources

Compass maintains a single **world model**: the lattice of weighted statements plus the archive of finalized ones. From the perspective of downstream consumers — Coach calling `compass_ask`, the audit subsystem checking worker output, the daily briefing, the CLAUDE.md block — the world model **is the operative truth**. They don't separately consult original source documents per query; they consult what compass currently believes.

The world model is built from multiple **sources**, each treated differently when ingested:

- **Truth corpus** — long-form human-vetted reference material in `<project>/truth/` (specs, goals, brand guidelines, contracts, scope docs, role definitions). Authored or approved by the human directly. Compass **never modifies** the corpus; it only reads it. On every run, Compass reads the corpus and derives atomic short claims for the lattice (Stage 0 truth-derive, §3.0 / Appendix A.14). Idempotent via a corpus hash — unchanged corpus → no fresh derivation.
- **Human Q&A answers** — the human answers Compass's targeted questions. Each answer reweights statements (digest, §3.1). Larger deltas (±0.5 max) than passive signals because the human is replying with intent.
- **Passive signals** — human messages to Coach in the inbox since the last run, recent commits, manually-recorded notes. Smaller deltas (±0.15 max) because the signal is incidental.
- **Audit drift** — work artifacts that contradict mid-confidence statements queue uncertain-drift questions back to the human for clarification (§5).

Compass differentiates the sources at **build time** — distinct prompts, delta caps, idempotency rules, and history `source` markers (`truth_derive`, `answer:qN`, `passive`, `merge`, `manual`, …). But once a source's contribution lands in the lattice, it's just a weighted statement: indistinguishable downstream from a Q&A-derived row, indistinguishable from a passive-digest row. The lattice IS the world model — the operative truth from `compass_ask`'s point of view.

### 1.4.2 Why the corpus is still special

Two narrow ways the truth corpus differs from other sources, despite the world model being the operative reference:

1. **Compass never amends it.** The human edits truth files via the Files pane; Coach proposes via `coord_propose_file_write` (human-approved); agents are blocked by a PreToolUse hook from writing under `truth/`. Compass reads only — it derives lattice statements but never writes back to the corpus.
2. **The truth-check subroutine** (§3.7) consults the corpus directly when digesting a human's Q&A answer. This catches the case where an answer would push the lattice into contradiction with the human-authored floor — protecting the lattice from drifting away via mistakenly-digested answers. It's NOT a per-query check that downstream consumers run.

Truth-corpus examples (real TeamOfTen):
- `truth/specs.md` — "TeamOfTen is a personal harness for one Coach + ten Players over Max-OAuth. State persists on a single VPS with kDrive-backed cloud sync. There is one human operator…"
- `truth/billing.md` — "Workers consume Anthropic billing under the human's Max plan; the harness does not bill end-users…"

Lattice statements derived from those (Stage 0 truth-derive, weight 0.75, region-tagged, `created_by="compass-truth"`):
- s1 (architecture, 0.75) — "TeamOfTen runs on a single VPS." [from specs.md]
- s2 (ops, 0.75) — "There is exactly one human operator." [from specs.md]
- s3 (pricing, 0.75) — "Players are billed via the operator's Max plan, not the customer." [from billing.md]

After a few rounds of Q&A, those same statements might sit at 0.92 (eligible to settle), or have drifted to 0.55 (a Q&A answer surprised compass), or been merged with a duplicate, or been reformulated. They're lattice rows — the world model — and downstream consumers see them at whatever weight compass currently believes. The corpus hasn't changed; the lattice's representation has.

> **Implementation note (TeamOfTen):** The truth corpus is **folder-backed** and spans **three** lanes (see Appendix A.13 for the full integration spec):
> 1. `<project>/truth/**/*.{md,markdown,txt}` — the dedicated truth lane (specs, brand guidelines, contracts). Strongest authority — fully human-vetted and write-protected from agents.
> 2. `<project>/project-objectives.md` — the human's authored objectives file at the project root.
> 3. `/data/wiki/<project_id>/**/*.{md,markdown,txt}` — the per-project wiki tree (agent-curated knowledge that compounds across sessions: gotchas, stakeholder preferences, glossary entries, domain rules). Less vetted than the first two — agents author wiki entries — but the human keeps a curating role and the corpus captures intent / users / UX / context that the truth lane often omits. Folding wiki into the corpus is strictly better than letting Compass run blind to the project's working memory.
>
> All three drive truth-derive (Stage 0a) and truth-check (§3.7) identically. The dashboard distinguishes them by relpath prefix — `truth/...`, `project-objectives.md`, and `wiki/...` — for display and link-routing only; the LLM treats them uniformly. The original spec §6.2 modeled truth as a Compass-managed `truth.json` of short atomic statements; in the harness implementation those atomic statements live in the LATTICE as truth-grounded rows, and the corpus holds the long-form vetted documents instead.

### 1.5 The actors

Compass operates in a system with four actors. The boundaries between them are strict.

| Actor | Can do | Cannot do |
|---|---|---|
| **Human** | Answer questions, modify truth, override weights, confirm settle/stale/dupe proposals, audit log | — |
| **Compass** | Generate questions, digest answers, manage regions, propose settlements/retirements/merges to human, write briefing & CLAUDE.md block, audit work | Modify truth, dispatch work, edit worker files, force decisions on coach |
| **Coach (coord)** | Query compass via `compass_ask`, submit work for `compass_audit`, read briefing, dispatch and review worker output | Answer compass's questions, modify the lattice, modify truth |
| **Workers** | Read CLAUDE.md (which contains compass's brief paragraph and short forward-looking briefing) | Query compass, modify the lattice, modify truth |

**Critical invariant:** Only the human answers compass's questions. Coach and workers are never asked questions by compass. Even if compass needs information about the project state, it routes the question through the human.

---

## 2 · Triggers and lifecycle

### 2.1 Run modes

Compass supports three trigger modes. They produce slightly different behavior.

#### `bootstrap` — runs at TeamOfTen startup

- Only runs if a human session is detected (see §2.2)
- Generates a larger initial batch of questions (up to 5) to cover the territory quickly
- Does not produce a briefing on this run (no prior state to summarize)

#### `daily` — runs on a schedule (default: once per 24h)

- Only runs if a human is reachable; otherwise stays dormant
- Sends a reminder ping to the human ("compass run pending — open me") if the daily window has passed without a run
- Performs the full pipeline (see §3)

#### `on_demand` — human-triggered

- Same pipeline as `daily`
- Useful when human wants a fresh briefing or has just answered several questions and wants to see the lattice update

**No silent autonomous runs.** If no human is reachable, compass does nothing. The whole point is the human is the single source of truth — running without them is meaningless.

### 2.2 Detecting "human is reachable"

The harness signals human availability to compass. Suggested implementations:
- Recent activity in the human's Slack/comm channel
- Recent commits or chat messages
- A heartbeat file the human's local environment touches
- A simple "I'm here" command the human can issue

A run is permitted if any signal is < 24h old. If none is, compass posts a reminder via whatever channel the harness exposes (Slack, email, a notification file in the human's inbox) and skips the run.

### 2.3 Q&A session mode

Separate from the run lifecycle, compass supports an interactive **Q&A session** initiated by the human. In this mode:
- Compass generates one question at a time
- Human answers
- Compass digests immediately and surfaces the next best question (which may have changed based on what was just learned)
- Session ends when human ends it

Q&A sessions are the highest-bandwidth interaction with compass. Use them when the human has 10+ minutes and wants to push the lattice forward fast.

### 2.4 Audits — coach-triggered, asynchronous

Audits are a separate path, not part of the daily/bootstrap run. Coach calls `compass_audit(artifact)` whenever a worker produces something coach considers a meaningful unit of work. Compass returns a verdict (see §3.6). Audits never block work — they are advisory.

---

## 3 · The pipeline

A `daily` or `bootstrap` run executes the following stages in order. Each stage may be skipped if it has nothing to do, but the order is strict.

### 3.0 Truth-derive (seed / enrich the lattice from the corpus)

Runs FIRST on every mode (`bootstrap`, `daily`, `on_demand`), before any answer digest. The user's principle: as long as truth-corpus material exists, the lattice should have an immediate basis — the world model isn't gated on the human running a Q&A session.

1. Read the truth corpus (the project's `truth/` folder; see §1.4 + Appendix A.13). Each allowed file → one synthesized truth fact.
2. Compute a stable hash over the corpus content. Compare against the previous run's hash, persisted out-of-band (in TeamOfTen, `team_config['compass_truth_hash_<id>']`).
3. **Short-circuit** the LLM call when the hash is unchanged AND the lattice already contains truth-derived rows. Idempotent — "if truth doesn't change, no new statements are inferred."
4. Otherwise, feed the LLM the corpus AND the existing active lattice. The prompt asks for atomic short YES/NO claims that REPRESENT what the corpus implies, **explicitly skipping statements already present**. Capped at 8 per run to avoid noise.
5. Materialize each proposal as a lattice row with:
   - `weight = 0.75` — well-grounded but compass-INTERPRETED. Sits in the LEANING-YES band so the settle proposal flow ignores it until it earns confidence; not pinned at 1.0 because the corpus is the floor, the lattice's representation is the working layer.
   - `created_by = "compass-truth"` — distinguishes from Q&A / passive / merge origins in the audit trail.
   - One history entry with `source = "truth_derive"` and a rationale citing the corpus file.
6. Persist the new corpus hash regardless of how many statements landed (the corpus has been considered).
7. Emit a `compass_truth_derived` event so the dashboard can highlight the new rows.

If the corpus is empty (no truth files), the stage is a no-op — Compass continues with the rest of the pipeline (Q&A digest, passive, etc.) and the lattice stays free-form. The world model just lacks a corpus-grounded floor until truth files appear.

### 3.0.1 Reconciliation (truth ↔ lattice)

The world model can drift away from the corpus over time even when the corpus is the project's authoritative source. Two scenarios make this real:

- **Specs evolved.** A truth file was edited (or a new one was added) and the new content contradicts something the lattice already settled or weighted highly. The compass-grounded statement is now wrong; the human needs to know.
- **Specs were lagging.** Q&A answers and passive signals nudged the lattice toward a position the human eventually wrote into truth — but in the opposite direction from where the lattice landed. Same outcome: lattice and corpus disagree.

Either way, **a conflict between the corpus and a settled statement is a high-stakes signal** — settled rows are what Coach treats as binding, what the audit subsystem checks against, and what the CLAUDE.md block surfaces to workers. Compass must not silently leave them inconsistent with truth.

The reconciliation pass fires immediately after truth-derive (§3.0), only when the corpus hash changed since the last successful run (cheap idempotency: an unchanged corpus can't have introduced new conflicts).

1. Feed the LLM: the full corpus + the active lattice (with weights) + the archived lattice (with `settled_as` markers).
2. Ask: "For each lattice row that the corpus contradicts, list the row id, the conflicting truth file(s), and a one-sentence explanation. Be conservative — only flag clear contradictions, not topical overlap."
3. For each conflict the LLM returns, create a **reconciliation proposal**. Persist alongside settle/stale/dupe proposals.
4. The human resolves on the dashboard:
   - **Update lattice** — the corpus is right, the lattice is wrong. Choices: un-archive (returning the row to the active lattice with a lower starting weight, e.g. 0.5, so subsequent runs re-evaluate); flip-archive (re-archive at the opposite `settled_as`); reformulate; or replace with the truth-derived equivalent.
   - **Update truth** — the lattice is right, the corpus is lagging. Routes the human to the Files pane on the offending truth file. Compass re-reads truth on the next run and the conflict resolves automatically (or surfaces again if the edit didn't actually fix it).
   - **Accept ambiguity** — leave both. Marks the row `reconciliation_ambiguity=True` and clears the open proposal. The flag suppresses re-detection until the corpus changes again — at the start of the next run where the corpus hash has shifted, the flag is cleared automatically and the row is re-evaluated. The LLM may re-flag (and the human can accept ambiguity again) or stay silent (still ambiguous, no change). This is stricter than §3.5 stale-keep, which suppresses indefinitely; reconciliation ties the suppression to the corpus rather than to time.
5. Until the human resolves a reconciliation proposal, it stays open and is re-displayed on every dashboard load. Like settle/stale/dupe, it expires after `PROPOSAL_EXPIRY_RUNS` runs of being ignored — clears the flag, and on the next run with a still-conflicting corpus it returns fresh.

**Why this lives in §3.0.1, not §3.7**: §3.7 is a per-Q&A-answer subroutine that gates digest. §3.0.1 is a per-run scan that gates the world model's coherence with the corpus. Different triggers, different scopes, but the same principle (the corpus is the floor; the lattice can't silently drift below it).

### 3.1 Digest answered questions

For each pending question that has a fresh human answer:
1. **Truth-check** the answer against truth (see §3.7). If conflict → halt this digest, mark question as `contradicted`, continue to next question.
2. **Reweight** the lattice based on the answer (see §3.2 prompts).
3. Mark question as digested.

### 3.2 Passive digest

Read all human signals accumulated since last run (chat messages, commits, notes, anything human-authored that is not a formal answer to a compass question). Update the lattice with smaller deltas (passive max ±0.15) than question-driven digests. Optionally spawn new statements when signals reveal lattice gaps.

Why this is important: even on days when the human doesn't run a Q&A session, their communications still contain information. Compass should learn from them.

### 3.3 Region housekeeping (auto-merge)

If active region count > 15: compass picks close regions and merges them. All statements (active + archived) using a deprecated region are re-tagged. Logged in run history. **No human approval.**

### 3.4 Surface settle proposals

Statements whose weight has crossed 0.85 (toward YES) or 0.15 (toward NO) become **settle candidates**. For each, compass generates a confirmation question for the human:

> "s7 looks like a definite YES (0.91) — confirm to settle at 1.0 and archive?"

The human resolves on the dashboard:
- **Confirm** → archive at 1.0 (or 0.0 for NO)
- **Adjust** → human picks a different final weight, then archive
- **Reject** → keep active; do not re-propose until weight moves further

Once flagged with `settleProposed: true`, compass does not re-propose this statement until the flag is cleared (by rejection, which clears it, allowing re-proposal if the weight moves further).

### 3.5 Surface stale proposals

Statements that have stayed in 0.35–0.65 for ≥ 4 runs with cumulative absolute movement < 0.1 are **stale candidates**. For each, compass generates a triage question:

> "s12 has been at 0.52 for 5 runs with no signal. Is this (a) irrelevant — retire entirely, (b) genuinely-unsettled-but-important — keep active, or (c) badly-phrased — let me reformulate?"

If badly-phrased, compass provides a candidate reformulation. The human resolves:
- **Irrelevant** → drop the statement entirely (destructive; confirm dialog)
- **Keep** → mark as `keptStale: true`, won't re-propose for a while
- **Reformulate** → replace text, reset weight to 0.5 and history to empty, treat as a fresh statement

### 3.6 Surface duplicate proposals

Compass scans the active lattice for near-duplicates — pairs or clusters of statements that would be falsified by the same evidence. For each cluster, propose a merge: a single sharper statement with a blended weight in the most common region. Human resolves:
- **Merge** → replace the cluster with the merged statement
- **Reject** → clear the `dupeProposed` flag, allow re-detection later

This stage is essential: with 50+ statements and active spawning during digests, redundancy is guaranteed. Duplicate detection should run every run.

### 3.7 Truth contradiction check

This is not a stage but a subroutine called inside §3.1 (and inside `submitQa`, see §4). When a human answer is being digested:

1. Before reweighting, ask the LLM: "does this answer contradict the truth corpus?" The full corpus (long-form vetted material from §1.4) is fed to the prompt, so contradictions are judged against the human-authored material directly — NOT against compass's truth-grounded lattice rows, which can drift.
2. If yes: halt the digest. Surface the conflict to the human via a modal with three resolution paths:
   - **Amend answer** — human restates; resume digest with the new answer
   - **Amend truth** — human edits the relevant truth file (the only way the corpus ever changes); the original answer can then be digested. In TeamOfTen, "amend truth" routes the human at the offending file via the Files pane (and the existing `coord_propose_file_write` flow stays available for Coach-driven amendments).
   - **Leave both** — accept the ambiguity; discard the answer, keep the corpus as-is. Question is marked `ambiguityAccepted`.

Compass never silently reweights in the face of a truth contradiction.

### 3.8 Generate new questions

Compass generates up to 3 new questions for the human (5 in `bootstrap` mode). Selection priorities, in order:
1. Statements with weights in 0.35–0.65 (max entropy → max info gain per answer)
2. Under-populated regions (coverage gaps)
3. Contested clusters (multiple related statements all hovering near 0.5)

Each question carries:
- The question text
- A **committed prediction** of how the human will answer (this is the discipline that makes the loop trainable — without a prediction, deltas have no anchor)
- Targeted statement ids (1–3)
- Compass's rationale for asking this

Questions accumulate in a queue. The human answers when convenient. Answers are digested on the next run.

### 3.9 Generate the daily briefing

A markdown document for the coach. Sections:
1. **CONFIRMED YES** (>0.8) — list as binding constraints
2. **CONFIRMED NO** (<0.2) — list NEGATIONS as binding (e.g. "s5 at 0.10 → customers are NOT technical")
3. **LEANING** (0.2–0.4 or 0.6–0.8) — working hypotheses, verify when cheap
4. **OPEN** (0.4–0.6) — genuine uncertainty, no expensive work here
5. **COVERAGE** — region density, thin regions
6. **DRIFT** — recent events contradicting the lattice, significant weight shifts
7. **RECOMMENDATION** — one sentence: where coach should focus next

Persisted to `memory/compass/briefings/briefing-{YYYY-MM-DD}.md`.

### 3.10 Update CLAUDE.md compass block

Compass maintains a managed section in the project's `CLAUDE.md`, delimited by markers:

```
<!-- compass:start -->
## Compass

[3-5 sentence paragraph explaining what compass is and how to use it]

### Where we stand · next steps

[5-8 line forward-looking briefing — best guess at project state, 2-3 concrete recommended next moves, 1-2 things to avoid, 1 line on what's uncertain]

<!-- compass:end -->
```

This block is regenerated each run. The harness (or compass itself) edits CLAUDE.md by replacing everything between the markers, leaving the rest untouched. This is how coord and workers naturally discover compass — they read CLAUDE.md, they see the section.

---

## 4 · The Q&A session

A Q&A session is a separate, interactive flow:

```
1. human: "start Q&A"
2. compass: generate ONE question, predict an answer, surface to human
3. human: answers
4. compass: truth-check; if no conflict, digest immediately, reweight lattice
5. compass: pick the NEXT best question (which may differ from what would have been picked before, given new info)
6. repeat 2-5 until human ends session
```

Differences from the daily-run question generation:
- One question at a time, not a batch
- Immediate digest, not deferred to next run
- Selection takes the current updated lattice into account at each step
- The session has a memory of which questions were already asked, to avoid repeats

---

## 5 · Audits

### 5.1 Purpose

To detect when worker output contradicts the world model, without holding up work and without bothering the human unnecessarily. Coach decides when to call audit — typically when a worker produces a meaningful unit of work (commit, decision, design choice). Audits are advisory only.

### 5.2 Verdicts

The audit returns one of three verdicts:

| Verdict | When | Escalation |
|---|---|---|
| `aligned` | Work is consistent with the lattice, or touches no high-stakes statements | Silent OK to coach. Logged. Human not notified. |
| `confident_drift` | Work clearly contradicts a HIGH-CONFIDENCE statement (>0.8 or <0.2) | Direct message to coach with conflicting statement ids. Human can review log when curious. **Human not pushed.** |
| `uncertain_drift` | Work seems off, but relevant statements are 0.3–0.7 | Coach told to proceed cautiously. Question queued for human (with a prediction). |

The conservatism gradient is intentional. Default verdict is `aligned`. Drift is only flagged when there is real evidence of contradiction.

### 5.3 The audit log

All audits — including aligned ones — are logged. The human can read the log on the compass dashboard, filterable by verdict. **The human is never pushed audit results, even confident-drift ones**; they can pull the log when curious. This preserves the contract that compass doesn't interrupt the human unless it needs an answer.

### 5.4 Rollup safety net

Compass should periodically (suggested: every 5 audits, or weekly) review the audit log itself. If many recent audits in the same region drifted (confident or uncertain), that's a signal the **lattice may be wrong, not the work**. Compass surfaces this as a meta-question:

> "Most recent worker outputs in the 'pricing' region have drifted from the lattice. Is the lattice wrong about pricing?"

This catches the case where compass's high-confidence statements are themselves stale or mistaken.

---

## 6 · File layout

All compass state lives under `memory/compass/` in the kDrive-shared filesystem.

```
memory/compass/
├── lattice.json               # active + archived statements (canonical state)
├── truth.json                 # truth-protected facts (only humans modify)
├── regions.json               # region taxonomy (compass-managed)
├── questions.json             # pending + answered + digested + contradicted questions
├── audits.jsonl               # append-only audit log
├── briefings/
│   ├── briefing-2025-04-29.md
│   ├── briefing-2025-04-30.md
│   └── briefing-2025-05-01.md
├── runs.jsonl                 # append-only run log (one line per run)
├── proposals/
│   ├── settle.json            # pending settle proposals awaiting human
│   ├── stale.json             # pending stale proposals awaiting human
│   ├── duplicates.json        # pending duplicate-merge proposals awaiting human
│   └── reconciliation.json    # pending corpus↔lattice conflicts (§3.0.1)
└── claude_md_block.md         # last-rendered block (for harness to inject into CLAUDE.md)
```

### 6.1 `lattice.json` schema

```python
{
    "statements": [
        {
            "id": "s1",                        # stable id, "s" + monotonic int
            "text": "Pricing model favors usage-based over flat-rate.",
            "region": "pricing",
            "weight": 0.55,                    # current P(true)
            "history": [                       # list of deltas applied over time
                {"run_id": "r3", "delta": 0.05, "rationale": "...",
                 "source": "passive|answer:q12|manual|merge|reformulation|truth_derive|reconcile:unarchive|reconcile:flip|reconcile:reformulate|reconcile:replace"}
            ],
            "archived": false,
            "archived_at": null,               # ISO timestamp when archived
            "settled_as": null,                # "yes" | "no" | "partial" | "merged" | "retired" | "reconciled" | null
            "settled_by_human": false,
            "manually_set": false,             # true if human used override
            "merged": false,                   # true if this is the result of a merge
            "merged_from": [],                 # list of ids that were merged into this one
            "reformulated": false,             # true if statement text was rewritten
            "settle_proposed": false,
            "stale_proposed": false,
            "dupe_proposed": false,
            "reconciliation_proposed": false,  # has an open §3.0.1 reconciliation proposal
            "reconciliation_ambiguity": false, # human accepted ambiguity; clears on corpus change
            "kept_stale": false,               # human said "keep, still important"
            "created_at": "...",
            "created_by": "compass|compass-truth|human"  # compass-truth = derived from corpus (§3.0)
        }
    ]
}
```

### 6.2 `truth.json` schema

> **Superseded for TeamOfTen** — the harness uses folder-backed truth at
> `<project>/truth/` instead of a Compass-managed `truth.json`. The
> reference shape below is preserved for projects that want the
> simpler list model; see Appendix A.13 for the canonical TeamOfTen
> integration spec, including the synthesized `TruthFact` shape, the
> `truth-index.md` manifest, allowed file types, the corpus hash,
> and the read interface.

```python
# Reference design only — NOT used in TeamOfTen.
{
    "facts": [
        {
            "index": 1,                        # 1-based, stable
            "text": "TeamOfTen runs on a single VPS with kDrive-backed shared state.",
            "added_at": "...",
            "added_by": "human"                # always "human"
        }
    ]
}
```

### 6.3 `regions.json` schema

```python
{
    "regions": [
        {
            "name": "pricing",
            "created_at": "...",
            "created_by": "compass",
            "merged_into": null                # if merged, points to the survivor
        }
    ],
    "merge_history": [
        {
            "from": ["billing", "payments"],
            "to": "pricing",
            "merged_at": "...",
            "run_id": "r12"
        }
    ]
}
```

### 6.4 `questions.json` schema

```python
{
    "questions": [
        {
            "id": "q42",
            "q": "Will customers self-serve or expect onboarding?",
            "prediction": "Self-serve, given technical audience.",
            "targets": ["s6"],                 # statement ids
            "rationale": "s6 sits at 0.50; biggest entropy gap in customers region",
            "asked_at": "...",
            "asked_in_run": "r5",
            "answer": null | "...",
            "answered_at": null | "...",
            "digested": false,
            "digested_in_run": null | "r6",
            "contradicted": false,             # set if truth conflict
            "ambiguity_accepted": false,       # human chose "leave both"
            "from_audit": null | "audit_123"   # if generated from an uncertain-drift audit
        }
    ]
}
```

### 6.5 `audits.jsonl` schema

One JSON object per line:

```python
{
    "id": "audit_1714568400123",
    "ts": "2025-05-01T10:30:00Z",
    "artifact": "worker-4 implemented per-second billing instead of per-task as originally scoped",
    "verdict": "confident_drift",              # aligned | confident_drift | uncertain_drift
    "summary": "Conflicts with s2 (per-task pricing) at 0.82.",
    "contradicting_ids": ["s2"],
    "message_to_coach": "...",
    "question_id": null | "q42"                # set if uncertain_drift queued a question
}
```

### 6.6 `runs.jsonl` schema

One JSON object per line:

```python
{
    "run_id": "r12",
    "started_at": "...",
    "finished_at": "...",                 # set when completed; null on skipped runs
    "mode": "bootstrap" | "daily" | "on_demand",
    "completed": true,
    "passive": {"updates": 4, "new_statements": 1, "summary": "..."},
    "answered_questions": 2,
    "contradictions": 0,
    "region_merges": [{"from": ["billing", "payments"], "to": "pricing"}],
    "settle_proposed": 1,
    "stale_proposed": 0,
    "dupe_proposed": 1,
    "reconcile_proposed": 0,              # pending §3.0.1 proposals at run end
    "questions_generated": 3,
    "truth_candidates": ["..."],
    "briefing_path": "memory/compass/briefings/briefing-2025-05-01.md",
    "notes": [                            # human-readable Stage 0 notes
        "truth_derive: 3 new statement(s)",
        "reconciliation: 1 ambiguity flag(s) cleared on corpus change"
    ],
    "skipped": false,                     # true when daily presence-gated
    "skipped_reason": null
}
```

### 6.7 `proposals/reconciliation.json` schema

One pending corpus↔lattice conflict per entry. Created by §3.0.1. Survives across runs until the human resolves via `POST /api/proposals/reconcile/:id`; expires after `PROPOSAL_EXPIRY_RUNS` runs of being ignored.

```python
{
    "compass_schema_version": "0.2",
    "proposals": [
        {
            "id": "rec1",                       # monotonic per project
            "statement_id": "s7",               # the lattice row in conflict
            "statement_archived": true,         # true if the row is settled
            "corpus_paths": ["specs.md"],       # truth file(s) cited
            "explanation": "specs.md says per-task billing; s7 was settled at 1.0 for per-second billing.",
            "suggested_resolution": "update_lattice",  # "update_lattice" | "update_truth" | "either"
            "proposed_at": "...",
            "proposed_in_run": "r42",
            "pending_runs": 0
        }
    ]
}
```

The human's resolution PATCHes the entry — `action: "update_lattice"` (un-archive / flip-archive / reformulate / replace), `action: "update_truth"` (informational; routes to Files pane), or `action: "accept_ambiguity"` (mark resolved without changes; the row keeps its current state and the `ambiguityAccepted` flag prevents immediate re-proposal).

---

## 7 · Public interface

Compass exposes a small surface to coach (and indirectly to workers via CLAUDE.md). Implement these as MCP tools.

### 7.1 `compass_ask(query: str) -> str`

Coach calls this to interrogate compass on any topic. Compass answers based strictly on the lattice and truth. Returns terse markdown citing statement ids and weights. If compass doesn't know, it says so.

**Coach invariant:** never trusts compass beyond what compass actually claims. Confident YES (>0.8) and confident NO (<0.2) are reliable; everything else is hedged.

### 7.2 `compass_audit(artifact: str) -> AuditResult`

Coach submits a work artifact (commit message, decision, worker report, design choice). Compass returns:

```python
{
    "verdict": "aligned" | "confident_drift" | "uncertain_drift",
    "summary": str,
    "contradicting_ids": list[str],
    "message_to_coach": str,
    "question_id": str | None       # set if uncertain_drift queued a question
}
```

Coach acts on the verdict:
- `aligned` → proceed silently
- `confident_drift` → halt or redirect the worker, or escalate to human depending on policy
- `uncertain_drift` → proceed cautiously, optionally wait for human answer to the queued question

### 7.3 `compass_brief() -> str`

Returns the most recent daily briefing (markdown). Coach reads this on demand (the harness can also auto-include it in coach's working context daily).

### 7.4 `compass_status() -> Status`

Quick dashboard-style status object:

```python
{
    "active_statements": int,
    "archived_statements": int,
    "regions": list[str],
    "pending_questions": int,
    "pending_settle_proposals": int,
    "pending_stale_proposals": int,
    "pending_dupe_proposals": int,
    "last_run_at": str,
    "last_briefing_at": str
}
```

### 7.5 What the human gets (not via MCP)

The human interacts with compass through a dashboard (web UI or CLI). Capabilities:
- View the lattice (filterable by region)
- View truth, edit truth, add truth
- Answer pending questions
- Resolve settle/stale/dupe proposals
- Override individual weights (with confirmation)
- Read briefings
- Read audit log (filterable by verdict)
- Trigger a Q&A session
- Trigger an on-demand run

The human does NOT have a CLI/MCP to compass. The dashboard is their entry point, and that's intentional.

---

## 8 · Prompt templates

These are the load-bearing prompts. They should be stored as templates with parameters substituted in. Treat the language carefully — small word changes shift behavior.

### 8.1 Shared semantics (prepended to most prompts)

```
WORLD-MODEL SEMANTICS:
- The model is a LATTICE OF STATEMENTS, each with a weight = P(statement is true).
- 1.0 = certain YES, 0.0 = certain NO, 0.5 = genuine ignorance.
- Confident NO is just as actionable as YES — its negation is the binding fact.
- The lattice maps the project's TERRITORY at moderate granularity. Good lattices are
  coarse enough that the LLM can infer fine specifics, fine enough that constraints
  are actionable.
- Each statement belongs to ONE region (a short tag like "pricing", "auth").
  Pick from existing regions; only invent a new region when none fits.
```

### 8.2 Passive digest

```
You are Compass. The human has produced signals (chat, commits, notes) — not formal
answers. Use them to passively update the lattice.

{shared_semantics}

Rules:
- Adjust weight only when a signal directly bears on a statement.
  Deltas in [-0.15, +0.15] (passive updates are smaller than answer-driven ones).
- Propose new statements when signals reveal lattice gaps (max 2). Pick a region from
  the existing list if any fits; only invent a new region when truly none fits.
  Phrase so YES = the affirmative reading. Start at weight 0.5.
- Never amend truth — flag candidates only.

Output ONLY:
{
  "updates": [{"id": string, "delta": number, "rationale": string}],
  "new_statements": [{"text": string, "region": string, "rationale": string}],
  "truth_candidates": [string],
  "summary": string
}
```

### 8.3 Question generation (batch, daily run)

```
You are Compass. Generate up to {N} questions to ask the human. Maximize information
gain across the lattice.

{shared_semantics}

Question selection priorities, in order:
1. Statements with weights in 0.35–0.65 (max entropy) — biggest info gain per answer.
2. Under-populated regions (few statements relative to apparent project importance) —
   coverage gaps.
3. Contested clusters — multiple related statements all hovering near 0.5 suggest a
   structural ambiguity.

For each question: commit to a specific, falsifiable prediction. Don't repeat pending
questions. Cite which statement ids the question targets (1–3 ids).

Output ONLY:
{
  "questions": [
    {"q": string, "prediction": string, "targets": [string], "rationale": string}
  ]
}
```

### 8.4 Question generation (single, Q&A session)

Same prompt as 8.3 but with a different output schema (single object, not array) and instructions to pick exactly one.

### 8.5 Digest answer

```
You are Compass digesting a human's answer.

{shared_semantics}

Rules:
- Estimate surprise 0–1 (0 = matches prediction, 1 = total contradiction).
- Targeted statements: delta in [-0.5, +0.5]. Magnitude depends on how decisively
  the answer settles the statement (clear yes/no = 0.4+, hedged = 0.1).
- Direction: support → toward 1, contradict → toward 0.
- Non-targeted statements: only adjust if directly implicated.
- Propose new statements when the answer reveals lattice gaps (max 2). Pick from
  existing regions; new region only if none fits.
- Flag truth_candidates if the answer reveals something worth promoting (human decides).

Output ONLY:
{
  "surprise": number,
  "updates": [{"id": string, "delta": number, "rationale": string}],
  "new_statements": [{"text": string, "region": string, "rationale": string}],
  "truth_candidates": [string],
  "summary": string
}
```

### 8.6 Truth contradiction check

```
You are Compass. Check whether the human's answer contradicts any truth-protected
fact. Truth is sacred — never reweight in the face of contradiction; surface for
human review.

Output ONLY:
{
  "contradiction": boolean,
  "conflicts": [{"truth_index": number, "explanation": string}],
  "summary": string
}
Truth indices are 1-based.
```

### 8.7 Settle / stale review

```
You are Compass. Surface statements for human review — you do not act unilaterally.

For SETTLE candidates (weight crossed 0.85 or dropped below 0.15):
- Phrase a confirmation question. Human will: confirm (settle and archive at 0/1),
  adjust (pick a different value), or reject (keep active).

For STALE candidates (near 0.5 with no movement across many runs):
- Phrase a triage question with three paths: irrelevant (retire entirely),
  genuinely-unsettled-but-important (keep active), badly-phrased (offer a reformulation).
- Provide a reformulation candidate when phrasing seems off.

Output ONLY:
{
  "settle": [{"id": string, "direction": "yes"|"no", "question": string, "reasoning": string}],
  "stale":  [{"id": string, "question": string, "reformulation": string|null, "reasoning": string}]
}
```

### 8.8 Duplicate detection

```
You are Compass. Detect statements that are near-duplicates of each other and should
be MERGED into a single sharper statement.

Two statements are duplicates if they would be falsified by the same evidence —
they're claiming the same thing in different words. Distinct angles are NOT duplicates
even if topically related.

For each duplicate cluster:
- List the redundant statement ids
- Propose a merged_text that captures the claim crisply
- Propose a merged_weight (a sensible blend of the originals' weights — usually closer
  to the higher-confidence one if they don't conflict)
- Choose the region — usually the most common region across the cluster

Output ONLY:
{"duplicates": [{"ids": [string,...], "merged_text": string, "merged_weight": number, "region": string, "reasoning": string}]}

If no duplicates, return {"duplicates": []}.
```

### 8.9 Region auto-merge

```
You are Compass. The region taxonomy has too many regions ({N} > {SOFT_CAP}).
MERGE close regions into broader ones.

Region merging is COMPASS HOUSEKEEPING — autonomous, no human approval.
All statements (active and archived) using a deprecated region get re-tagged.

Identify region pairs/clusters that are conceptually close (e.g., "billing" and
"payments", "auth" and "authentication"). Pick one to keep (usually the broader/clearer
name) and merge the other(s) into it.

Bring total active region count to ≤ {TARGET}.

Output ONLY:
{"merges": [{"from": [string,...], "to": string, "reasoning": string}]}
```

### 8.10 Audit

```
You are Compass auditing a piece of work against the lattice. Coach has submitted a
work artifact (commit, decision, worker output) and wants to know if it aligns with
current beliefs about the project.

{shared_semantics}

Verdict rules:
- "aligned": work is consistent with the lattice, or touches no high-stakes statements.
- "confident_drift": work clearly contradicts at least one HIGH-CONFIDENCE statement
  (>0.8 or <0.2). You're sure something is wrong. Coach gets a direct message;
  human is NOT bothered.
- "uncertain_drift": work seems off but the relevant statements are at 0.3–0.7 — you
  can't tell if work is wrong or if the lattice is wrong. Coach proceeds cautiously;
  a question is generated for the human (with a prediction).

Be conservative — most work should come back "aligned." Only flag drift when there's
real evidence of contradiction.

Output ONLY:
{
  "verdict": "aligned" | "confident_drift" | "uncertain_drift",
  "summary": string,
  "contradicting_ids": [string],
  "message_to_coach": string,
  "question_for_human": {"q": string, "prediction": string, "targets": [string]} | null
}

If "aligned", message_to_coach is short ("OK · aligned with lattice"), question_for_human
is null. If "confident_drift", message_to_coach explains the conflict directly to coach.
If "uncertain_drift", message_to_coach tells coach you've flagged it for human review,
and question_for_human is the question to queue.
```

### 8.11 Daily briefing

```
You are Compass producing a daily briefing for the coach. Be terse and useful.

{shared_semantics}

Sections:
1. CONFIRMED YES (>0.8) — binding constraints. List as-is.
2. CONFIRMED NO (<0.2) — surface NEGATION as binding (e.g. "s5 at 0.10 → customers
   are NOT technical").
3. LEANING (0.2–0.4 or 0.6–0.8) — working hypotheses, verify when cheap.
4. OPEN (0.4–0.6) — genuine uncertainty, no expensive commits here.
5. COVERAGE — which regions have meaningful coverage, which look thin.
6. DRIFT — recent events contradicting the lattice, or significant shifts.
7. RECOMMENDATION — one sentence, where coach should focus.

Plain markdown. No preamble.
```

### 8.12 CLAUDE.md block

```
You maintain Compass's managed section in CLAUDE.md so coord and workers discover
you naturally.

TWO PARTS:

PART 1 — Static-ish paragraph (3–5 sentences) explaining:
- Compass is a side-engine that maintains a lattice of statements about the project,
  each with P(true) weight
- It's queried by the coach via compass_ask, never edited by workers
- Settled (archived) statements are facts coord can rely on
- Only the human answers questions and amends truth

PART 2 — Forward-looking briefing (5–8 lines max) titled "Where we stand · next steps":
- Compass's best guess at where the project stands (1–2 sentences)
- 2–3 concrete next-step suggestions for the coach, derived from confident-YES
  statements and lattice momentum
- 1–2 things to avoid (from confident-NO archive)
- 1 line on what's currently uncertain

Plain markdown. No fences. Use exactly these headings: "## Compass" and
"### Where we stand · next steps".
```

### 8.13 Coach query (`compass_ask`)

```
You are Compass. The coach is interrogating you. Answer based strictly on the lattice
and truth.

{shared_semantics}

Cite statement ids and weights. Treat >0.8 as confirmed yes, <0.2 as confirmed no
(surface negation), 0.4–0.6 as genuinely uncertain. Be terse.
```

---

## 9 · Reference implementation sketch

Python, suggested module layout under `compass/`:

```
compass/
├── __init__.py
├── config.py              # caps, thresholds, paths
├── store.py               # read/write lattice.json, truth.json, etc.
├── llm.py                 # callClaude wrapper, JSON parser, retry
├── prompts.py             # all prompt templates
├── pipeline/
│   ├── digest.py          # passive digest, answer digest
│   ├── questions.py       # generate questions (batch + single)
│   ├── reviews.py         # settle, stale, duplicate proposals
│   ├── regions.py         # auto-merge regions
│   ├── briefing.py        # generate daily briefing
│   ├── claude_md.py       # render and inject CLAUDE.md block
│   └── truth_check.py     # contradiction subroutine
├── audit.py               # auditWork
├── api.py                 # MCP tools (compass_ask, compass_audit, compass_brief, compass_status)
├── runner.py              # the daily/bootstrap/on_demand orchestrator
├── presence.py            # human-reachable detection
└── tests/
    ├── test_digest.py
    ├── test_questions.py
    ├── test_audit.py
    └── fixtures/
```

### 9.1 `config.py` (excerpt)

```python
from pathlib import Path

# Capacity caps
STMT_SOFT_CAP = 50
STMT_HARD_CAP = 70
REGION_SOFT_CAP = 15
REGION_HARD_CAP = 20

# Settle thresholds
SETTLED_YES = 0.85
SETTLED_NO = 0.15

# Stale detection
STALE_MIN_RUNS = 4
STALE_MAX_MOVEMENT = 0.10
STALE_WEIGHT_BAND = (0.35, 0.65)

# Question generation
QUESTIONS_PER_DAILY_RUN = 3
QUESTIONS_PER_BOOTSTRAP_RUN = 5

# Update bounds
PASSIVE_DELTA_MAX = 0.15
ANSWER_DELTA_MAX = 0.50

# Presence
HUMAN_PRESENCE_WINDOW_HOURS = 24

# Paths
COMPASS_ROOT = Path("memory/compass")
LATTICE_PATH = COMPASS_ROOT / "lattice.json"
TRUTH_PATH = COMPASS_ROOT / "truth.json"
REGIONS_PATH = COMPASS_ROOT / "regions.json"
QUESTIONS_PATH = COMPASS_ROOT / "questions.json"
AUDITS_PATH = COMPASS_ROOT / "audits.jsonl"
RUNS_PATH = COMPASS_ROOT / "runs.jsonl"
BRIEFINGS_DIR = COMPASS_ROOT / "briefings"
PROPOSALS_DIR = COMPASS_ROOT / "proposals"
CLAUDE_MD_BLOCK_PATH = COMPASS_ROOT / "claude_md_block.md"
CLAUDE_MD_PROJECT_PATH = Path("CLAUDE.md")  # the actual project file to update

# CLAUDE.md markers
CLAUDE_MD_START_MARKER = "<!-- compass:start -->"
CLAUDE_MD_END_MARKER = "<!-- compass:end -->"
```

### 9.2 `runner.py` (sketch)

```python
import json
from datetime import datetime, timezone
from pathlib import Path

from . import config, store, presence
from .pipeline import digest, questions, reviews, regions, briefing, claude_md, truth_check


async def run(mode: str = "daily") -> dict:
    """
    Execute a full compass run.
    mode ∈ {"bootstrap", "daily", "on_demand"}
    Returns a run log dict.
    """
    if not presence.human_reachable():
        presence.send_reminder()
        return {"skipped": True, "reason": "no human"}

    run_id = f"r{int(datetime.now(timezone.utc).timestamp())}"
    log = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "completed": False,
        "region_merges": [],
        "settle_proposed": 0,
        "stale_proposed": 0,
        "dupe_proposed": 0,
        "questions_generated": 0,
        "contradictions": 0,
        "answered_questions": 0,
    }

    state = store.load_all()  # lattice, truth, regions, questions

    # 1. Digest answered questions (with truth-check)
    answered = [q for q in state.questions if q.answer and not q.digested and not q.contradicted]
    for q in answered:
        tc = await truth_check.check(state.goal, state.truth, q.q, q.prediction, q.answer)
        if tc.contradiction:
            store.flag_question_contradicted(q.id, tc)
            log["contradictions"] += 1
            continue
        delta = await digest.answer(state, q)
        store.apply_lattice_updates(delta, run_id, source=f"answer:{q.id}")
        store.mark_question_digested(q.id, run_id)
        log["answered_questions"] += 1

    # 2. Passive digest
    state = store.load_all()  # reload
    passive = await digest.passive(state)
    store.apply_lattice_updates(passive, run_id, source="passive")
    log["passive"] = passive.summary_dict()

    # 3. Region auto-merge
    state = store.load_all()
    if len(state.active_regions()) > config.REGION_SOFT_CAP:
        merges = await regions.auto_merge(state)
        for m in merges:
            store.apply_region_merge(m, run_id)
            log["region_merges"].append({"from": m.from_, "to": m.to})

    # 4-6. Reviews + duplicate detection
    state = store.load_all()
    rev = await reviews.propose(state, run_count=store.run_count())
    store.persist_proposals(rev)
    log["settle_proposed"] = len(rev.settle)
    log["stale_proposed"] = len(rev.stale)

    dupes = await reviews.detect_duplicates(state)
    store.persist_dupe_proposals(dupes)
    log["dupe_proposed"] = len(dupes)

    # 7. Generate new questions
    state = store.load_all()
    n = config.QUESTIONS_PER_BOOTSTRAP_RUN if mode == "bootstrap" else config.QUESTIONS_PER_DAILY_RUN
    new_qs = await questions.generate_batch(state, count=n)
    store.add_questions(new_qs, run_id)
    log["questions_generated"] = len(new_qs)

    # 8. Briefing (skip on bootstrap — nothing yet to summarize)
    if mode != "bootstrap":
        state = store.load_all()
        brief = await briefing.generate(state, recent_events=store.recent_events_for_run(log))
        store.write_briefing(brief)
        log["briefing_path"] = str(brief.path)

    # 9. CLAUDE.md block
    state = store.load_all()
    block = await claude_md.generate(state)
    claude_md.inject(block)

    log["completed"] = True
    log["finished_at"] = datetime.now(timezone.utc).isoformat()
    store.append_run_log(log)
    return log
```

### 9.3 `claude_md.py` (sketch)

```python
import re
from pathlib import Path
from . import config, llm, prompts

async def generate(state) -> str:
    """Generate the markdown block (without markers)."""
    return await llm.call(
        prompts.CLAUDE_MD_BLOCK_SYSTEM,
        prompts.claude_md_block_user(state),
        max_tokens=1200,
    )

def inject(block_text: str) -> None:
    """
    Replace the content between compass markers in CLAUDE.md.
    If markers don't exist, append the block at end of file.
    """
    path = config.CLAUDE_MD_PROJECT_PATH
    start = config.CLAUDE_MD_START_MARKER
    end = config.CLAUDE_MD_END_MARKER

    full_block = f"{start}\n{block_text}\n{end}"

    if not path.exists():
        path.write_text(full_block + "\n")
        return

    content = path.read_text()
    pattern = re.compile(
        re.escape(start) + r".*?" + re.escape(end),
        re.DOTALL,
    )

    if pattern.search(content):
        new_content = pattern.sub(full_block, content)
    else:
        new_content = content.rstrip() + "\n\n" + full_block + "\n"

    path.write_text(new_content)
    # Also persist a copy under memory/compass/ for traceability
    config.CLAUDE_MD_BLOCK_PATH.write_text(full_block)
```

### 9.4 `audit.py` (sketch)

```python
from datetime import datetime, timezone
from . import config, llm, prompts, store


async def audit_work(artifact: str) -> dict:
    """
    Audit a work artifact against the current lattice.
    Returns the verdict dict and persists to audit log.
    """
    state = store.load_all()
    raw = await llm.call(
        prompts.AUDIT_SYSTEM,
        prompts.audit_user(state, artifact),
    )
    result = llm.parse_json(raw)

    audit_id = f"audit_{int(datetime.now(timezone.utc).timestamp() * 1000)}"
    question_id = None

    if result["verdict"] == "uncertain_drift" and result.get("question_for_human"):
        q = result["question_for_human"]
        question_id = store.add_question(
            q=q["q"],
            prediction=q["prediction"],
            targets=q.get("targets", []),
            rationale=f"Generated from audit drift: {result['summary']}",
            from_audit=audit_id,
        )

    record = {
        "id": audit_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "artifact": artifact,
        "verdict": result["verdict"],
        "summary": result["summary"],
        "contradicting_ids": result.get("contradicting_ids", []),
        "message_to_coach": result["message_to_coach"],
        "question_id": question_id,
    }
    store.append_audit(record)
    return record
```

### 9.5 MCP tool wiring (sketch)

```python
# api.py
from mcp.server import Server
from . import audit, store, runner

server = Server("compass")


@server.tool()
async def compass_ask(query: str) -> str:
    """Interrogate compass on any project topic."""
    state = store.load_all()
    return await store.answer_coach_query(state, query)


@server.tool()
async def compass_audit(artifact: str) -> dict:
    """Audit a work artifact against the lattice."""
    return await audit.audit_work(artifact)


@server.tool()
async def compass_brief() -> str:
    """Get the most recent daily briefing."""
    return store.latest_briefing()


@server.tool()
async def compass_status() -> dict:
    """Quick status snapshot."""
    return store.status()
```

---

## 10 · Don'ts

These are the failure modes to actively prevent. Each has produced bugs in earlier design iterations.

### 10.1 Don't let compass amend truth

Truth is the floor. Compass surfaces truth_candidates from digests, but **the human is the only actor that ever modifies the truth list**. If a human answer contradicts truth, halt the digest, surface the conflict, three-way resolve.

### 10.2 Don't auto-archive based on weight

Weight crossing a threshold (0.85 or 0.15) **proposes** a settle, it doesn't execute one. The human confirms, adjusts, or rejects. Auto-archiving is wrong because high confidence by compass is not the same as ground truth — the human knows things compass doesn't.

### 10.3 Don't ask coach or workers questions

Compass asks the human only. Coach and workers are read-only consumers (coach via `compass_ask` / `compass_audit`, workers via CLAUDE.md). Even if compass thinks coach has the answer, it must route through the human.

### 10.4 Don't run silently when no human is reachable

The whole loop is anchored in the human as ground truth. Running without them means deltas have no anchor. If no human, **post a reminder and skip the run**.

### 10.5 Don't bother the human about confident-drift audits

Confident drift means compass is sure something is wrong. Coach is told directly. Human reads the audit log when curious — they are NOT pushed. Pushing erodes the trust contract that compass only interrupts when it genuinely needs an answer.

### 10.6 Don't block work on audit results

Audits are advisory. The work has already been produced. Coach decides what to do with the verdict (halt the worker, redirect, accept). Compass never has the authority to halt anything — it only informs.

### 10.7 Don't generate questions when nothing is uncertain

If every active statement is settled or near-settled, generating questions is busywork. The pipeline should produce zero questions in this case and let the human enjoy the calm. The lattice is meant to converge.

### 10.8 Don't reframe statements without permission

If compass thinks a statement is badly phrased, it surfaces a stale proposal with a reformulation candidate. Reformulation is destructive (resets weight + history) and must be human-confirmed. Don't silently rewrite statements during digests or duplicate detection.

### 10.9 Don't let regions multiply unchecked

Compass invents regions only when none of the existing ones fit. When in doubt, reuse. Above 15 regions, auto-merge on the next run. Regions are housekeeping — small and stable is better than precise and proliferating.

### 10.10 Don't strikethrough or visually retire confident-NO statements

A weight of 0.0 is fully informative — its negation is the binding fact. Display it with the same prominence as 1.0. Both are settled, both are actionable. Only the *unsettled middle* is what needs work.

### 10.11 Don't merge regions without re-tagging archived statements

When `billing` is merged into `pricing`, every statement (active OR archived) tagged `billing` must become `pricing`. The archive is part of the project's known territory; it should reflect the current taxonomy, not the historical one. The merge itself is logged separately.

### 10.12 Don't make compass's confidence equal to project truth

A high-weight statement is compass's best guess given its evidence. It's not absolute. Coach should treat confident YES as a binding constraint *for now*, but coach should also know that audits can shift weights, and that the human can override. Don't bake the lattice into worker prompts in a way that makes future updates ineffective.

### 10.13 Don't run the heavy LLM operations in series when parallelizable

Inside a single run, several stages are independent: passive digest → reviews → duplicate detection → question generation. Some can be parallelized. But maintain ordering invariants (truth-check before digest, region-merge before review, etc.).

### 10.14 Don't trust the LLM's JSON output without parsing safeguards

Use the `parse_json` helper that strips code fences, extracts the first balanced object/array, and falls back gracefully. LLM JSON output is unreliable in long-running systems. Validate against a schema after parsing.

### 10.15 Don't use compass for short-lived projects

Compass amortizes its cost across many runs. For a one-off task, the overhead of bootstrapping a lattice and running the loop is wasted. Use compass when the project will be active for ≥ a week and has enough complexity that uncertainty is genuinely costly.

### 10.16 Don't show compass's internal reasoning to coach by default

Coach receives terse, decisive answers from `compass_ask` and `compass_audit`. Compass's "rationale" fields are for the human dashboard, not coach. Coach has its own job; it doesn't need to follow compass's chain of thought, only its conclusions and confidence levels.

### 10.17 Don't store the briefing or CLAUDE.md block in the live LLM context window

Generate them, persist them to disk, and let coach/workers read them as files. Stuffing them into every coach prompt wastes tokens. Coach loads the briefing once daily and the CLAUDE.md block whenever the harness includes it.

### 10.18 Don't skip the predict-before-asking discipline

Every question generated by compass must include a committed prediction of how the human will answer. Without the prediction, the digest has no anchor for surprise estimation, and the loop loses its trainable property. Predictions are mandatory output of question generation.

### 10.19 Don't accumulate stale proposals indefinitely

If the human ignores settle/stale/dupe proposals across many runs, they pile up and become noise. After N runs of being ignored (suggested: 5), expire the proposal — clear the flag and let it re-trigger if conditions still hold. The human's attention is the most valuable resource; respect it.

### 10.20 Don't conflate "no human signals" with "lattice unchanged"

Even on days with zero new signals and zero answered questions, compass should still:
- Re-evaluate stale candidates (more time has passed)
- Re-check for duplicates
- Regenerate the briefing and CLAUDE.md block (the world may need a fresh recommendation)
- Generate new questions if the lattice has 0.5-zone statements compass hasn't asked about yet

A no-signal run is not a no-op.

---

## 11 · Versioning and migration

Compass state files should carry a `compass_schema_version` field at the top. When the schema changes, write a migration script. Never silently ignore unknown fields.

```python
# Top of lattice.json
{
  "compass_schema_version": "0.2",
  "statements": [...]
}
```

Current version: **0.2** (statements + regions + audits).
Earlier (0.1) was assumptions-only with no regions or audits.

---

## 12 · Open questions for the implementer

Things that aren't fully decided and may need adjustment as the system runs:

1. **Audit cost.** Coach may call `compass_audit` frequently. Each call is a single API request. Monitor: if audits become a meaningful fraction of total token spend, consider caching aligned verdicts for similar artifacts.
2. **Q&A session limits.** A long Q&A session (50+ questions) may degrade — both for the human (fatigue) and for the LLM (context bloat as session memory grows). Suggested: warn human after 20 questions, hard-cap at 50.
3. **Bootstrap question count.** 5 may be too many for a project that begins with a sparse lattice. Possibly scale with seed lattice size.
4. **Truth promotion mechanic.** Truth candidates surface in run logs but there's no first-class UI flow for "promote candidate to truth." The human currently does this manually. Consider adding a one-click promotion path on the dashboard.
5. **Cross-project compass.** Currently compass is per-project. If TeamOfTen runs many concurrent projects, decide whether each gets its own compass instance or whether there's a shared meta-compass. Default: per-project.
6. **Weight decay over time.** A high-confidence statement set 6 months ago may no longer reflect reality. Consider a slow weight-toward-0.5 decay for statements that haven't been touched in N months. Off by default; opt-in per project.

---

## 13 · Test harness expectations

The implementation should ship with tests covering:

- Round-trip persistence (lattice + truth + regions + questions)
- Truth-check correctly halts a digest on contradiction
- Settle proposals are not re-issued for the same statement until rejected and weight moves
- Region auto-merge correctly re-tags archived statements
- Duplicate merge preserves history references
- Audit `confident_drift` does NOT queue a question; `uncertain_drift` does
- Human override sets `manuallySet` and is honored on next run
- CLAUDE.md injection is idempotent and only modifies content between markers

---

## 14 · Dashboard UI specification

The dashboard is the only way the human interacts with compass. Every human-confirmation flow defined above (settle, stale, dupe, truth conflict, weight override) requires a concrete UI surface. This section specifies the dashboard at the same level of precision as the engine.

The engine should be testable and runnable without the dashboard, but the system has no value without one — the engine's outputs need a human, and the human needs a single place to consume them and respond.

### 14.1 Architecture

- Single-page application (React or equivalent SPA framework)
- Runs in the human's browser
- Talks to the compass module via a thin HTTP/WebSocket API on the same VPS
- All state of record lives in `memory/compass/*` files (per §6); the dashboard is a view layer with optimistic local state

The dashboard backend is a small wrapper around `compass/store.py` and `compass/runner.py`. Endpoints (suggested):

```
GET    /api/state                 → full snapshot (lattice, truth, regions, questions, briefing, claude_md_block, audits, proposals)
POST   /api/run                   → trigger an on_demand run; streams phase updates via SSE/WebSocket
POST   /api/qa/start              → start Q&A session
POST   /api/qa/next                → fetch next question (uses current lattice)
POST   /api/qa/answer             → submit answer; immediate digest; returns next question
POST   /api/qa/end                 → end session
POST   /api/questions/:id/answer  → queue an answer for next-run digest
POST   /api/proposals/settle/:id  → resolve settle proposal
POST   /api/proposals/stale/:id   → resolve stale proposal
POST   /api/proposals/dupe/:id    → resolve duplicate proposal
POST   /api/proposals/reconcile/:id → resolve corpus↔lattice conflict (§3.0.1)
POST   /api/statements/:id/weight → manual override (with confirmation flag)
POST   /api/truth                  → add/update/remove truth fact
POST   /api/audit                  → submit work artifact for audit (also exposed as MCP tool to coach)
POST   /api/inputs                 → simulate or record a human signal (chat/commit/note)
GET    /api/briefings/:date       → fetch a specific briefing
GET    /api/runs?limit=N          → fetch run history
```

The WebSocket/SSE channel pushes:
- Run phase updates (so the dashboard can show "passively digesting…" / "generating questions…" live)
- New audit results (when coach calls `compass_audit` from elsewhere, the human's open dashboard updates in real time)
- New questions arriving in the queue
- Proposals appearing

### 14.2 Layout

The dashboard is a single long page with sticky header. Sections, top to bottom:

```
┌──────────────────────────────────────────────────────────────────────┐
│ HEADER: compass rose | "Compass" | tagline | RUN / Q&A / RESET buttons │
├──────────────────────────────────────────────────────────────────────┤
│ GOAL (editable inline)         |  TRUTH (brass-bordered, +/- entries) │
├──────────────────────────────────────────────────────────────────────┤
│ REGIONS STRIP: [all 12] [pricing 3] [auth 2] [deploy 1] ...            │
├──────────────────────────────────────────────────────────────────────┤
│ THREE-COLUMN WORKSPACE                                                  │
│  COL 1: The Lattice           COL 2: Inputs + Q-queue   COL 3: Brief  │
│   ├ capacity bar               ├ Human Inputs            ├ Daily Brief │
│   ├ statement rows              │  (chat/commit/note     ├ CLAUDE.md   │
│   ├ archived (collapsed)        │   recent + add)        │   block      │
│   ├ settle proposals            ├ Question Queue         ├ Ask Compass │
│   ├ stale proposals             │  (pending Q's with     │              │
│   └ dupe proposals              │   prediction reveal)   │              │
├──────────────────────────────────────────────────────────────────────┤
│ WORK AUDITS (submit pane + filterable log)                              │
├──────────────────────────────────────────────────────────────────────┤
│ RUN HISTORY (collapsible entries)                                       │
└──────────────────────────────────────────────────────────────────────┘

Overlays (z-index above page):
  - Q&A SESSION OVERLAY (sticky bottom panel when active)
  - OVERRIDE CONFIRMATION MODAL
  - TRUTH CONFLICT MODAL
```

Page width: max 1240px, centered. Padding 24px. Background is a paper texture — see §14.10.

### 14.3 Header

Components:
- **Compass rose** SVG, 64px, on the left. Spins slowly during any active phase (any phase ≠ "idle"). Static otherwise.
- **Title block:** kicker line "TeamOfTen · world-model engine · v0.2" in tracked monospace; "Compass" as a 44px italic serif headline; tagline "a lattice of statements about the project · regions are compass-managed · only humans answer questions" beneath.
- **Action buttons** (right side, vertical stack):
  - **▸ Run Compass** (primary, ink/dark on paper) — disabled when busy or Q&A active. Shows pulse + phase label when busy.
  - **◊ Start Q&A Session** (brass) when no session active; **✕ End Q&A** (oxblood) when active.
  - **Reset** (outlined, smaller) — clears lattice, truth, runs, all back to seed. Disabled when busy.

Below the header, a thin error banner (oxblood background, paper text) appears if any operation failed; persists until dismissed or replaced.

### 14.4 Goal & Truth row

Two-column row, 60/40 split.

**Goal (left):** Click-to-edit. Display state shows the goal in 22px italic serif with a dotted underline. Click → input field appears with SAVE button. Enter or SAVE commits.

**Truth (right):** Brass-bordered card with subtle brass tint. Each fact rendered with `T1`, `T2`, … prefix in monospace, body in 13px serif. Each row has a × button to remove. At the bottom, an input row with dashed brass border for adding new facts. Enter commits.

The "⛨ Truth-protected · only humans modify" kicker label is brass and uppercase tracked. This visual treatment must remain consistent everywhere truth is displayed — it's the system's anchor and should always look distinct from compass-managed content.

### 14.5 Regions strip

A horizontal row of pills below the goal/truth row. Format:

```
Regions · 12/15 (auto-merged by compass when over)

[ all 47 ]  [ pricing 8 ]  [ customers 5 ]  [ architecture 11 ]  …
```

Each pill is a small button (10px monospace, deterministic color per region name — see §14.10). Clicking a region pill filters the lattice column to only that region; the active pill is filled (background = region color, text = paper). Clicking the active pill or `all` clears the filter.

If region count exceeds 15, the kicker number turns brass. If it exceeds 20, oxblood. (Compass should auto-merge before this, but the visual indicator catches the rare overflow.)

### 14.6 Column 1 — The Lattice

Section header: "The Lattice" with right-aligned counter `47/50 active · 12 archived · pricing` (filter shown if active).

#### Capacity bar

A 4px tall, full-width bar showing `active_count / STMT_HARD` as a fill. Vertical tick at the soft cap position. Color: moss green if under soft cap, brass if over soft cap, oxblood if over hard cap. A monospace caption appears below when over capacity:
- Over soft cap: `▲ over soft capacity — review proposals below`
- Over hard cap: `▲ HARD CAP — settle/retire before adding`

#### Statement rows

Each row is a two-column grid (30px id / flex content). Content area:

```
┌─────────────────────────────────────────────────────────────────────┐
│ [region-pill] Statement text here.   [NEW] [HUMAN-SET] [TRUE]        │
│ → negation is binding: this is NOT the case   (only if weight < 0.2) │
│ ┌────────────────────────────────────────┐    ┌───┬───┬────┬───┐    │
│ │ 0 |─────|·|─────| 1   0.65   →0.10     │    │NO│ ½ │YES │ … │    │
│ └────────────────────────────────────────┘    └───┴───┴────┴───┘    │
└─────────────────────────────────────────────────────────────────────┘
```

Components:
- **ID** (left, monospace 10px). When the statement is targeted by a pending question or active Q&A question, show as `s7◆` in oxblood bold.
- **Region pill** before the text, deterministic color.
- **Statement text** (13.5px serif).
- **Status badges** appended after text:
  - `NEW` (brass) — created in the last run
  - `HUMAN-SET` (brass) — manually overridden
  - `MERGED` (faint) — result of duplicate merge
  - `TRUE` (moss bg, paper text) — weight > 0.8 (not yet settled)
  - `FALSE` (oxblood bg, paper text) — weight < 0.2
- **Negation hint** below text, only shown if weight < 0.2: italic oxblood `→ negation is binding: this is NOT the case`. Critical for the human to scan a confident-NO at a glance and understand the actionable fact.
- **Weight bar** (left of action buttons) — see §14.10 for design.
- **Quick weight buttons** (right): four small monospace buttons:
  - `NO` (oxblood outline) → opens override modal proposing weight 0
  - `½` (faint outline) → proposes 0.5
  - `YES` (moss outline) → proposes 1
  - `…` (ink outline) → opens a `prompt` for custom value 0–1
  
  All four routes lead through the override confirmation modal — never apply directly.

Rows separated by 1px dashed `RULE` color line. 8px vertical padding.

#### Archived list (collapsed)

After the active rows, a `<details>` element labeled `▸ SETTLED & ARCHIVED (12)`. When expanded, each archived statement is shown in dim (opacity 0.75) with:
- `s7` id, region pill, statement text
- Status badge `TRUE · 1.00` (moss) or `FALSE · 0.00` (oxblood)
- A small `RESTORE` button on the right that un-archives back to the active list at its current weight

The archive is read-mostly. Restoring is rare but available.

#### Lattice legend

Below the archive details, a small monospace caption:
```
weight = P(statement is true) · 0=NO · 0.5=unknown · 1=YES
◆ = targeted by a pending question · settle requires confirmation · regions auto-merged
```

#### Settle proposals card

Appears below the lattice (still in column 1) only when proposals exist. Brass border, brass-tinted background, kicker `◊ N Settle Proposal(s) · awaiting your call`.

For each proposal:
- Statement id, region pill, statement text in italic, current weight in monospace
- Compass's confirmation question (e.g. "This looks like a definite YES — confirm to settle at 1.00?")
- Compass's reasoning in faint italic
- Three action buttons (wraps on narrow viewports):
  - `✓ CONFIRM YES · 1.00` (moss) or `✗ CONFIRM NO · 0.00` (oxblood) — depends on `direction`
  - `ADJUST…` (outlined) — opens prompt for custom final weight, then archives
  - `REJECT · keep active` (faint outlined) — keeps statement active, clears `settleProposed` flag

#### Stale proposals card

Below settle proposals. Faint border + tint, kicker `◌ N Stale Statement(s) · need triage`.

For each:
- Statement id, region pill, text, current weight
- Compass's triage question with three options
- Compass's reasoning
- Suggested reformulation (if any) shown in a brass-tinted callout
- Three action buttons:
  - `IRRELEVANT · retire` (oxblood) — opens browser confirm dialog (destructive!)
  - `KEEP · still important` (outlined) — clears flag
  - `REFORMULATE` (brass, only if compass suggested one) — replaces text with the suggestion, resets weight to 0.5
  - `REWRITE…` (brass outlined) — opens prompt for human's own reformulation

#### Duplicate proposals card

Below stale proposals. Oxblood border + tint, kicker `⊕ N Duplicate Cluster(s) · merge?`.

For each cluster:
- `WOULD MERGE` kicker, then list each redundant statement (id, text, weight)
- `INTO` kicker, then the proposed merged statement (region pill, text, weight) in a moss-tinted callout
- Reasoning in faint italic
- Two buttons: `MERGE` (moss), `REJECT · keep separate` (faint outlined)

### 14.7 Column 2 — Human Inputs & Question Queue

#### Human Inputs

Section header `Human Inputs` with right-aligned counter `N pending`.

Each pending input is a row with:
- Kind tag (small monospace box with colored border): `chat` (ink), `commit` (moss), `note` (brass)
- Timestamp (`yesterday 18:42` or `just now`) in faint monospace
- Body text (13px)

At the bottom of the inputs list, an add row:
```
[kind dropdown ▾] [text input — placeholder "simulate human input…"] [+]
```

The dropdown values match the kind tags. Enter or `+` adds. Inputs accumulate until the next run, when they're consumed by passive digest.

Note: in production, this list is populated automatically by the harness from real chat/commit feeds; in the dashboard it remains user-editable so the human can simulate or correct.

#### Question Queue

Section header `Question Queue` with counter `N pending`.

Each pending question is a card with ink border, slight white tint, 10px padding:

```
┌──────────────────────────────────────────────────────┐
│ Will customers self-serve or expect onboarding?      │
│ ▸ PREDICTION & TARGETS                                │
│ ┌────────────────────────────────────────┐            │
│ │ answer (digested next run)…            │            │
│ │                                         │            │
│ └────────────────────────────────────────┘            │
│                                            [SUBMIT]    │
└──────────────────────────────────────────────────────┘
```

- Question text in 14px italic serif
- `▸ PREDICTION & TARGETS` is a `<details>` toggle. When expanded, brass-bordered callout with `Predicts:` and `Targets:` lines plus rationale in faint italic. The prediction is hidden by default to avoid biasing the human's answer.
- Textarea for the answer (13px italic, dashed ink border, transparent background, min 50px tall, vertical resize)
- `SUBMIT` button (brass) on the right — when answered, the answer is queued for next run's digest. After submission, the SUBMIT button is replaced with a `QUEUED` badge in moss.

If a question is `contradicted` (failed truth check on a previous run attempt), it's shown with an oxblood left border and a "needs resolution" indicator. Clicking it opens the truth conflict modal. If `ambiguity_accepted`, shown faded with a faint kicker `← ambiguity accepted, not digested`.

### 14.8 Column 3 — Briefing & CLAUDE.md & Ask Compass

#### Daily Briefing

Section header `Daily Briefing` with right-aligned timestamp.

If a briefing exists: a card with ink border, white-tinted background, 14px padding, max-height 320px with scroll. The briefing markdown is rendered as pre-wrap serif text (no fancy markdown rendering needed — preserve newlines and let the LLM's structure shine through).

If no briefing yet: italic faint placeholder "No briefing yet. Run compass to produce one."

#### CLAUDE.md compass block

Section header `CLAUDE.md · compass block` with timestamp.

If a block exists: monospace card with brass left border (4px), brass-tinted background, the block text wrapped between visible faint `<!-- compass:start -->` and `<!-- compass:end -->` markers (so the human sees what will be written into CLAUDE.md verbatim). A small `COPY` button in the top-right corner copies the full block (including markers) to clipboard.

If no block yet: italic faint "Generated each run."

This panel is read-only. The actual injection into the project's CLAUDE.md happens automatically (per §9.3).

#### Ask the Compass

Section header `Ask the Compass` with right-aligned label `coach interrogates compass`.

A single-row interface:
```
[input — placeholder "e.g. should we build for usage or flat pricing?"]  [ASK]
```

Enter or click `ASK` triggers the query. Below, the response renders in a brass-bordered callout (4px left border, brass tint, 13px serif, pre-wrap) when received.

This panel exists in the dashboard primarily so the human can sanity-check what compass would tell coach. In production, coach calls `compass_ask` via MCP — but the human can use this same surface to verify compass's answers.

### 14.9 Work Audits section

A full-width section below the three-column workspace. Section header `Work Audits` with counter `N audits · coach-triggered`.

Two-column layout (60/40):

#### Submit pane (left)

```
Submit work for audit · simulating coach

┌───────────────────────────────────────────────────────────────┐
│ paste a commit message, decision, or worker output…           │
│                                                                │
│ e.g. "worker-4 implemented per-second billing instead of       │
│ per-task as originally scoped"                                 │
└───────────────────────────────────────────────────────────────┘

audits never block work · advisory only            [▸ AUDIT]
```

A wide textarea (min 100px, vertical resize), italic placeholder text, primary `▸ AUDIT` button. Caption beneath in faint italic to reinforce the contract.

In production, coach calls `compass_audit` directly via MCP. This pane lets the human submit artifacts manually too — useful for retrospective audits or for testing.

#### Filter pane (right)

```
Filter log

[ ALL 24 ]  [ ALIGNED 18 ]  [ CONFIDENT DRIFT 3 ]  [ UNCERTAIN 3 ]
```

Four pills, each colored to its verdict (ink, moss, oxblood, brass). Active pill is filled. Below the pills, a small caption explaining the verdict-to-escalation mapping:

```
Aligned · silent OK to coach · human not bothered.
Confident drift · direct message to coach · human can review here.
Uncertain · coach proceeds cautiously · question queued for human.
```

#### Audit log entries

Below both panes, a list of audit entries (newest first), filtered by the active pill.

Each entry is a card with a colored left border (4px) matching its verdict:
- Aligned: moss border, dim opacity (0.7), moss-tinted background
- Confident drift: oxblood border, full opacity, oxblood-tinted background
- Uncertain: brass border, full opacity, brass-tinted background

Card contents:
- Top row: verdict badge (paper text on accent background) on left, timestamp in faint monospace on right
- Artifact text in italic serif, in a faint-bordered inset block (preserves the submitted content verbatim)
- Compass summary (13px serif, 1.5 line height)
- Conflicting statement ids in monospace if any: `CONFLICTS WITH: s2, s7`
- `▸ MESSAGE TO COACH` `<details>` reveal — when expanded, shows the verbatim message compass sent to coach in a faint-tinted code block
- If a question was queued: small brass italic line `◊ Question queued for human (see Question Queue)`

The whole audit list is scrollable independently if it grows large; default to showing the last 20.

### 14.10 Visual design tokens

The dashboard's visual identity is a navigator's logbook / lab notebook. Aged paper background, ink-black primary text, brass and oxblood accents, moss for confirmation/positive states. Serif body type, monospace data type.

#### Colors

```
PAPER       #ede4d0   page background, button text on accents
INK         #1f1a12   primary text, primary button background
FAINT       #8a7d63   secondary text, captions, disabled
RULE        #bfae8a   borders, dividers, dashed lines
BRASS       #9c6f1e   warnings, proposals, brass highlights, "compass-touched"
OXBLOOD     #7a2820   confident-NO, errors, confident-drift, destructive
MOSS        #4a5d3a   confident-YES, success, aligned, MERGE confirmation
```

#### Region color palette (deterministic by hash)

```
#7a2820, #4a5d3a, #9c6f1e, #3d5a7a, #6e3a6e,
#8a4d2a, #2a6a5a, #7a6a2a, #5a3a3a, #3a5a3a,
#7a3a5a, #5a5a7a, #7a5a3a, #3a7a7a, #5a7a3a
```

Hash function: simple `str -> int` (sum char codes × 31), modulo palette length. Same region always gets the same color.

#### Typography

- **Body & headlines:** `Cormorant Garamond` (Google Fonts), 400/500/600/700, italic variants
- **Data, labels, monospace:** `JetBrains Mono` (Google Fonts), 400/500/600
- Headlines: 44px italic for the page title; 22px italic 600 for section headers
- Body: 13–14px serif, 1.4–1.5 line height
- Monospace data: 9–11px, 1–2px letter-spacing for kickers/badges, uppercase for status labels

#### Background texture

The page background combines:
- Base paper color (`#ede4d0`)
- Two large soft radial gradients (brass top-left, oxblood bottom-right) at 4–5% opacity
- A repeating horizontal rule line every 32px in `RULE@22%` (notebook lines)

Subtle. Should not interfere with text legibility.

#### Weight bar component

Critical UX element — the human reads it dozens of times per session.

- Symmetric: 0.5 is the dead center
- Bar grows leftward toward NO (oxblood) or rightward toward YES (moss)
- 70px wide, 5px tall, ink border
- Center tick line 1px, ink at 50% opacity
- "0" label on the far left in monospace; "1" on the far right. Whichever side is currently active (depending on weight) is full opacity; the other is at 40%
- Numeric weight label (`0.65`) to the right in monospace, color-coded by zone (moss > 0.8, light moss 0.6–0.8, ink mid, light oxblood 0.2–0.4, oxblood < 0.2)
- Delta indicator (when present): arrow + magnitude (`→0.05` for moves toward YES, `←0.10` for moves toward NO), in moss or oxblood
- 700ms ease transition on width changes — the visible animation of the bar reweighting after a digest is part of the feedback loop

#### Region pill

```
┌─────────────────┐
│ pricing  3      │
└─────────────────┘
```

- 1px solid border in the region's color
- Default: transparent background, region-colored text
- Active state: region-colored background, paper text
- 10px monospace, 0.5 letter-spacing
- 2px vertical, 8px horizontal padding
- 4px right margin, 4px bottom margin (so they wrap nicely)

### 14.11 Overlays & modals

#### Q&A session overlay

When a Q&A session is active, a sticky panel pinned to the bottom of the viewport. Full width, max 55% of viewport height, scrolls within. Triple-rule top border (3px double ink), paper background, soft shadow upward.

Contents:
- Header row: `◊ Q&A Session · live` kicker (brass), `Compass is asking` headline (24px italic serif), counter `N answered` on the right, `END` button
- Loading state: `<pulse> selecting next question…` italic faint while phase is `qa:thinking` or `qa:digesting`
- Active question state:
  - Question text in 18px italic serif
  - `TARGETS: s4, s6` in monospace faint
  - `▸ REVEAL COMPASS PREDICTION` toggle button (dashed brass border, brass text). When clicked, expands to show the prediction and rationale in a brass-bordered callout — but defaults to hidden so the human's answer isn't biased.
  - Answer textarea (auto-focus, 70px min, white-tinted, ink border)
  - `⌘/Ctrl + Enter to submit` hint
  - `SKIP` (faint outlined) and `SUBMIT ▸` (brass) buttons

When the human submits, the panel transitions to digesting state; on success, immediately fetches the next question and updates without closing the overlay. Session ends only on `END` click.

The lattice in column 1 should update visibly while the Q&A session is running — the human watches their answers reshape the world model in real time. This is one of the most satisfying interactions in the system; preserve it.

#### Override confirmation modal

Triggered by any of the four quick weight buttons (`NO` / `½` / `YES` / `…`). Centered modal, paper background, 2px ink border, 24px padding, max 460px wide, dimmed page underneath.

Contents:
- Kicker: `Confirm manual override` (brass uppercase tracked monospace)
- Headline: `Set weight directly?` (22px italic serif 600)
- The full statement text in italic
- Side-by-side `CURRENT` and `NEW` blocks separated by a brass arrow `→`. The new value is colored by zone (moss/oxblood/ink) and bold.
- Caption: `Compass will continue updating this weight from future evidence unless you override again. Settle/archive requires confirming a separate proposal.`
- `CANCEL` (outlined) and `CONFIRM ▸` (ink primary) buttons

The modal exists to prevent accidental clicks. It's deliberately a full confirmation step rather than an instant action — manual overrides are rare and should feel deliberate.

#### Truth conflict modal

Triggered when a digest detects that the human's answer contradicts a truth-protected fact. 3px oxblood border, paper background, max 600px wide, scrollable if tall.

Contents:
- Kicker: `⚠ Truth contradiction · digest halted` (oxblood)
- Headline: `Your answer conflicts with protected truth` (22px italic serif 600)
- Question section: `QUESTION` label + question text, `YOUR ANSWER` label + answer text
- Conflicts callout (oxblood-tinted box):
  - `CONFLICTS` kicker
  - Each conflict listed: `T2: <truth text>` with explanation in faint italic
  - Compass's overall summary in faint italic at the bottom
- Three resolution options as full-width buttons:
  1. `1 · AMEND ANSWER` (ink) — "I misspoke. Let me restate."
  2. `2 · AMEND TRUTH` (brass) — "The protected fact is outdated. Update it."
  3. `3 · LEAVE BOTH` (faint) — "Accept ambiguity. Discard answer, keep truth."
  
  Each button shows a label in monospace and a full sentence describing the choice in italic serif beneath.

When a path is chosen:
- AMEND ANSWER → the modal switches to a textarea pre-filled with the original answer; human edits and submits. Digest is then retried with the new answer.
- AMEND TRUTH → modal switches to a select dropdown of all truth facts (defaulted to the conflicting one) plus a textarea for the new text. Submit replaces that truth fact. The original answer is then digested normally.
- LEAVE BOTH → instant. The answer is discarded, the truth stays. The question is marked `ambiguity_accepted` and won't be re-digested. The lattice does not update from this answer.

A `BACK` button returns to the three-option chooser. `CANCEL` (clicking the dim background) closes the modal entirely; the question remains in the queue marked `contradicted` and waits for the human to revisit.

This modal is the most important interaction in the dashboard. It's the only place where truth changes hands. Every visual cue should reinforce gravity — color (oxblood), border weight (3px), the explicit mention that the digest is halted.

### 14.12 Run history (footer)

A footer section below audits, only shown when at least one run has happened. Section header `Run History` with counter `N run(s)`.

Each run is a `<details>` element. Summary row:
- Timestamp (faint monospace)
- One-line summary (italic) — typically the passive digest summary

When expanded:
- `Passive: N updates · M new statements · Questions queued: K`
- `Settle: A · Stale: B · Truth conflicts: C`
- If region merges happened: `Region merges: pricing ← billing, payments` (brass)
- If truth candidates surfaced: `Truth candidates: ...` (brass)

Newest first. Acts as a low-noise audit trail for compass's own activity.

### 14.13 Loading and empty states

Every async operation must show a loading state. Use the `<Pulse />` component (a small brass dot animating opacity + scale) inline with status text:

- `<Pulse /> passively digesting…`
- `<Pulse /> generating questions…`
- `<Pulse /> truth check…`
- etc.

Phase labels are derived from the engine's current phase (streamed via WebSocket/SSE). The Run Compass button shows the truncated phase label inline while busy: `<Pulse /> generating questions…`.

Empty states:
- No briefing yet → italic faint "No briefing yet. Run compass to produce one."
- No CLAUDE.md block → italic faint "Generated each run."
- No pending inputs → italic faint "No new inputs."
- No queued questions → italic faint "No queued questions."
- No audits → no log section, just the submit pane

Empty states should never feel broken. They should feel calm.

### 14.14 Keyboard shortcuts

- `⌘/Ctrl + Enter` in the Q&A textarea submits the answer
- `⌘/Ctrl + Enter` in the Ask Compass input submits the query
- `Esc` closes any modal
- `R` (no modifiers, when no input is focused) triggers a Run Compass — implement this only after the basic flows work; it's a power-user nicety

No other shortcuts initially. Discoverability matters more than density.

### 14.15 Mobile / narrow viewport behavior

Out of scope for v1. The dashboard is designed for a desktop browser with at least 1100px width. If accessed on mobile, show a brief notice: "Compass dashboard is desktop-only. Open on a wider screen."

If the harness team wants a mobile read-only view later, the `compass_status` MCP tool already exposes enough for a small status widget elsewhere.

### 14.16 Accessibility minimums

- All buttons must be keyboard-focusable with visible focus rings
- Color is never the sole carrier of meaning — every state has a text label too (e.g. confident YES has both moss color AND a `TRUE` text badge)
- Contrast: ink (#1f1a12) on paper (#ede4d0) is ~12:1, well above WCAG AAA. Verify the same for brass and oxblood text on tinted backgrounds.
- Modals trap focus and return focus to the triggering element on close
- No essential information lives in tooltips alone

### 14.17 Behavior contract — what the dashboard MUST do

These are non-negotiable behaviors that mirror the engine's invariants:

1. **Never apply a manual weight change without the override modal.** The four quick buttons all route through it.
2. **Never settle a statement without an explicit settle proposal resolution.** Even if the human sets weight to 1.0 via override, that does NOT archive — they must wait for the next run's settle proposal and confirm there. (The override modal explicitly says this.)
3. **Never digest a question whose answer triggered a truth conflict.** The question stays `contradicted` until the human resolves via the truth conflict modal.
4. **Always show compass's prediction as opt-in.** The prediction must be hidden by default behind a reveal toggle so the human's answer isn't biased.
5. **Always show settled statements with prominent TRUE/FALSE badges.** Don't strikethrough or fade confident-NO statements — they're informative. The negation hint is mandatory for weight < 0.2.
6. **Always log audits, even aligned ones.** The aligned ones are dimmed (opacity 0.7) but still visible in the log.
7. **Never push notifications for confident-drift audits.** They appear in the log; the human pulls when curious.
8. **Real-time updates during runs.** Phase labels and lattice changes stream live via WebSocket/SSE so the human watches the engine work. This is part of building trust in the system.

### 14.18 Behavior contract — what the dashboard MUST NOT do

1. Auto-archive statements based on weight (only the engine's settle proposal flow archives, and only after human confirmation).
2. Modify truth without going through the truth conflict modal or the goal/truth row's explicit edit.
3. Show coach's MCP responses in the audit log (those are coach-internal; only the audit verdicts and message_to_coach text are surfaced).
4. Block the human from running compass even with an empty lattice — the bootstrap path needs to work.
5. Display compass's chain-of-thought / reasoning fields by default. Rationale fields exist for transparency but are tucked into `<details>` reveals, not surfaced in the main view. Compass produces conclusions; the human acts on them.
6. Cache stale state. Every page refresh should fetch the latest snapshot. The dashboard is a view, not a system of record.
7. Allow workers to access this URL. This dashboard is the human's tool. Workers consume CLAUDE.md; coach consumes MCP. Neither has a dashboard.

### 14.19 Implementation reference

The artifact at `/mnt/user-data/outputs/compass.jsx` (the iterative React playground developed alongside this spec) is a working reference implementation of most of the dashboard surface against a mock backend. It uses a single-file React component with inline styles. Treat it as a fixture, not as the final implementation:

- It hits the Anthropic API directly from the browser (production must route through the backend)
- It uses local React state where production should use server state synced via WebSocket
- It does not implement authentication (single-human single-VPS, but the harness team should still gate access)
- It is on the v0.1 data model in places (assumptions instead of statements). The new implementation should follow §1 of the engine spec strictly.

The artifact is most useful as a UX reference — color choices, spacing, microcopy, the rhythm of confirmations. Match it where it makes the human's life easier; deviate where you can do better.

---

**End of specification.**

---

## Appendix A · Implementation deviations (TeamOfTen, 2026-05-01)

The reference spec was written project-agnostic. The TeamOfTen
implementation makes the following adaptations. None change the
**fundamentals** (lattice, weight semantics, region taxonomy, truth-
protected facts, predict-before-ask, human-only-answers, advisory-only
audits, no auto-archive, no silent runs); they bind the engine to the
harness's existing primitives.

### A.1 · Per-project, opt-in

The harness is multi-project. Each TeamOfTen project gets its own
Compass instance with state under `/data/projects/<id>/working/compass/`
(mirrored synchronously to `kDrive:projects/<id>/compass/` — flatter
remote tree matches the existing `knowledge/` and `decisions/`
conventions). Compass is **opt-in per project** via
`team_config['compass_enabled_<id>']`, which defaults to false. The
dashboard's Enable button flips it.

Project switching is automatic — every code path resolves
`compass_paths(project_id)` against the live active project, so the
dashboard, MCP tools, scheduler, and runner all swap context together.
Switching mid-Q&A auto-ends the session to avoid cross-project digest
contamination.

### A.2 · Max-OAuth via the Agent SDK, not the raw Anthropic SDK

The harness's "Max-plan OAuth, no API keys" invariant means Compass
cannot use the `anthropic` Messages SDK. Instead `compass.llm.call()`
invokes `claude_agent_sdk.query()` with a minimal
`ClaudeAgentOptions(system_prompt, max_turns=1, mcp_servers={},
allowed_tools=[])`. Cost feeds the existing `turns` ledger under
`agent_id="compass"`, `runtime="claude"`, with
`cost_basis="compass:<stage>"` so per-stage spend is queryable
(`SELECT cost_usd FROM turns WHERE cost_basis='compass:audit'`).

### A.3 · MCP tools live alongside `coord_*`, Coach-only

The four spec §7 MCP tools register inside
`server.tools.build_coord_server` — same MCP server namespace as
`coord_*`, distinguished by tool name. **All four reject Players**
(`if not caller_is_coach: return _err(...)`); Players read Compass
exclusively via the `<!-- compass:start -->` block in the project
CLAUDE.md. This is the user-confirmed scope decision; the spec §1.5
actor table allows either reading. Each tool also rejects when
Compass is disabled for the active project.

### A.4 · Storage: JSON files, not SQLite tables

State files (`lattice.json`, `regions.json`, `questions.json`,
proposals/, briefings/) follow spec §6 verbatim — JSON on disk, kDrive-
mirrored synchronously. SQLite is reserved for the events bus, the
`turns` cost ledger, and `team_config` (per-project enable flag,
last-run timestamp, heartbeat, truth-corpus hash). Audits and run logs
are append-only JSONL.

**There is NO `truth.json` here** — see Appendix A.13. Truth lives in
the project's `truth/` folder; Compass is a pure consumer.

Atomic writes via tempfile + `os.replace`. On corrupt JSON, load falls
back to empty defaults with a warning log — a botched edit doesn't
freeze the loop.

### A.5 · Human-presence detection

Two signals (per spec §2.2 suggested implementations): (1) a row in
the `messages` table with `from_id='human'` and matching `project_id`
newer than `HARNESS_COMPASS_PRESENCE_HOURS` (default 24h); (2) a
heartbeat in `team_config` updated by `POST /api/compass/heartbeat`
(the dashboard pings every 60s while open, plus on every user-driven
action). No new heartbeat infrastructure.

### A.6 · Scheduler is a dedicated loop

Reuses the harness's lifespan-task pattern but does **not** plug into
the Coach `recurrence_scheduler_loop` — Compass runs need their own
per-project presence + last-run-time gating, and they don't go through
`run_agent`. Polls every `HARNESS_COMPASS_SCHEDULER_TICK=300s`, walks
every enabled project, fires `bootstrap` on first activation, `daily`
once per UTC day after `DAILY_RUN_HOUR_UTC=9`. One project per tick.

### A.7 · UI is harness-styled in v1, not the navigator's-logbook palette

The user explicitly chose harness-styled v1 (paper-texture /
Cormorant Garamond / brass-oxblood-moss treatment is **deferred** to
a future visual-polish pass). Information architecture matches §14 —
header, goal+truth row, regions strip, three-column workspace,
audits, run history, modals, Q&A overlay — but visuals reuse the
existing GitHub-dark vars (`--bg`, `--fg`, `--accent`, `--ok`,
`--warn`, `--err`) and system monospace. All glyphs are CSS-drawn or
inline SVG per the no-emoji rule.

### A.8 · Q&A session ships in v1 with immediate digest

Per spec §4 — one question at a time, immediate truth-check + digest,
the sticky-bottom overlay (§14.11) preserved. The user-confirmed v1
scope. Hard cap at `QA_HARD_CAP=50`, soft warning at
`QA_WARN_AFTER=20`.

### A.9 · "Ask Compass" panel reuses the same prompt as `compass_ask`

`POST /api/compass/ask` (server-side) and the dashboard's "Ask
Compass" panel (UI-side) both call the same `prompts.COACH_QUERY_*`
template the MCP tool uses. The panel exists primarily for the human
to sanity-check what Coach would see. Both surfaces are read-only —
neither modifies the lattice.

### A.10 · Reset is per-project

`POST /api/compass/reset` wipes only `data/projects/<id>/compass/`
plus the matching kDrive folder. Other projects' Compass state is
untouched. Also clears `compass_bootstrapped_<id>`,
`compass_last_run_<id>`, `compass_heartbeat_<id>` from team_config so
the next enable starts fresh.

### A.11 · Pipeline parallelization not implemented (§10.13)

Spec §10.13 notes some stages could be parallelized. v1 runs them
strictly in sequence — the truth-check-before-digest invariant is the
hard constraint, but other stages (passive digest → reviews →
duplicate detection → question generation) are sequential too. Cost /
latency are acceptable in practice for daily-cadence runs; revisit if
Compass becomes hot-path.

### A.12 · Audit rollup heuristic (§5.4)

Spec §5.4 leaves the rollup threshold loose. v1 fires a meta-question
when the trailing `AUDIT_ROLLUP_INTERVAL=5` audits contain ≥3 drift
verdicts whose `contradicting_ids` cluster (≥3 hits) in the same
region. Conservative — false positives are expensive (they bother the
human). Deduplicates against pending meta-questions for the same
region so the queue doesn't grow unbounded.

### A.13 · Truth corpus integration (TeamOfTen)

Spec §1.4 / §6.2 modeled truth as a Compass-managed list (`truth.json` with `{index, text, added_at, added_by}` rows of short atomic claims). The TeamOfTen harness already has a canonical truth lane that holds **long-form vetted documents** (specs, goals, brand guidelines, contracts, role docs), plus an authored objectives file at the project root, plus a per-project wiki tree of agent-curated knowledge. Maintaining a parallel list inside Compass would create yet another source of truth, and there can be only one consolidated corpus. So Compass adapts: it reads the harness's truth-bearing material from these three lanes and synthesizes `TruthFact`s on demand, with a small set of well-defined rules.

#### A.13.1 Where truth lives

Compass treats three on-disk lanes as a single conceptual corpus:

  - **Truth lane**: `/data/projects/<project_id>/truth/**` (walked recursively). Vetted, write-protected from agents; humans / Coach own it. kDrive mirror at `projects/<project_id>/truth/`. Auto-seeded with `truth-index.md` (template at `server/templates/truth_index.md`) — a manifest of which files should live in the folder. The EnvPane's Truth section renders the manifest as a checklist with create / open buttons.
  - **Project objectives**: `/data/projects/<project_id>/project-objectives.md` — the human's authored objectives file at the project root. Same authority as truth/. Surfaced in the EnvPane.
  - **Project wiki**: `/data/wiki/<project_id>/**` — the per-project sub-tree of the global wiki tier (separate from project's own folder; see `server/paths.py:global_paths().wiki / project_id`). Agent-curated knowledge that compounds across sessions: stakeholder preferences, glossary entries, domain rules, non-obvious gotchas, decisions context. Authored via the LLM-Wiki skill (`server/templates/llm_wiki_skill.md`); the human curates by editing in the Files pane. Less vetted than the first two lanes — agents author entries — but the corpus captures intent / users / UX / context that the truth lane often omits, and that's exactly what Compass needs to keep the lattice grounded in the project's working memory. The cross-project wiki root (`/data/wiki/*.md`) is **not** included; only the project sub-tree.

All three lanes are treated identically downstream. The relpath prefix (`truth/...`, `project-objectives.md`, `wiki/...`) is a display label only — it lets the dashboard branch on link composition (truth/ + objectives compose under `/data/projects/<id>/`; wiki composes under `/data/wiki/<id>/`) and lets the LLM cite a specific source in its rationale.

The truth-lane manifest (`truth-index.md`) is **just another truth file** from Compass's perspective — it's ingested verbatim alongside the listed files. The LLM receives the relpath as a prefix, so it can recognize `truth-index.md` as a manifest from the filename and treat it as context rather than as a list of project claims. We don't filter or special-case it; the per-file ingestion rule applies uniformly. Same for any wiki INDEX/log files — they live at the global wiki root, outside the per-project sub-tree, so the walk naturally excludes them.

#### A.13.2 Authoring rules (existing harness flow)

  - **Humans** edit truth-corpus files directly via the Files pane (or any text editor with kDrive sync). Files behave like any other markdown.
  - **Coach** proposes truth-lane edits via `coord_propose_file_write(scope='truth', path, content, summary)`. The proposal lands in the `file_write_proposals` table; the human approves / denies / cancels in the EnvPane's "File-write proposals" section (which also handles project-CLAUDE.md proposals via the same flow). On approval, the file is written and the proposal row is marked resolved (`server/truth.py:resolve_file_write_proposal`).
  - **Players** are blocked from writing under `truth/` by a `PreToolUse` hook in `server/agents.py`. A Player attempting `Write` / `Edit` / `Bash` against a truth path gets a deny; the agent surface tells them to route via Coach.
  - **Wiki entries** are authored via the LLM-Wiki skill — any agent (Coach or Players) can `Write` directly to `/data/wiki/<project_id>/...` per the skill's rules. There's no proposal flow; the human curates after the fact. This is intentional: the wiki tier compounds knowledge fastest when agents can record learnings without ceremony.
  - **Compass** has no write path. It only reads.

#### A.13.3 What Compass ingests

  - **Allowed extensions**: `.md`, `.markdown`, `.txt` — uniform across all three lanes. Other formats accepted by `truth/` (`.yaml`, `.json`, `.toml`, `.csv`) are reference / structured docs and are skipped — they're not natural truth-check candidates and would bloat prompts. The dashboard's Files pane is the right place to view them.
  - **Walk order**: each lane walked recursively, then merged + sorted by display relpath. Stable POSIX relpaths so indexing is deterministic across calls (a file rename or addition shifts indices on the next read; that's fine because indices are only stable within one run). With the synthetic prefixes the merged ordering always groups in the same way: `project-objectives.md` (no prefix), then `truth/...`, then `wiki/...`.
  - **Per-file synthesis**: each file becomes one `TruthFact` (`server/compass/store.py`):
    - `index`: 1-based, monotonic by sort order.
    - `text`: `(<display_relpath>) <file body>` — prefixing the relpath gives the LLM a name handle so when it answers `truth_index: 2` we can map back to a path. The display relpath uses the synthetic `wiki/` prefix for wiki entries (NOT the real on-disk path) so prompts stay short and the LLM can attribute claims by source flavor.
    - `added_at`: file mtime, ISO-8601 UTC.
    - `added_by`: `"human"` (Compass's perspective — the harness's curating layer owns authoring).
  - **Truncation**: bodies longer than `MAX_FACT_CHARS=8000` are truncated with a marker like `[truncated — file is N chars total]`. Long files (specs, brand guides, lengthy wiki entries) are usually reference material; the head captures the salient claims.
  - **Empty / blank files**: skipped entirely. Avoids a fact that's just a path with no body.

#### A.13.4 Read interface

  - `read_truth_facts(project_id) -> list[TruthFact]` — the canonical reader. Walks all three lanes (truth/, project-objectives.md, wiki/), merges, sorts, and synthesizes facts. Called from `compass.store.load_state`, which itself is called fresh on every Compass call site (runner stage entries, MCP tool handlers, dashboard `/api/compass/state`). No in-process caching — file mtimes drive correctness.
  - `read_truth_index_to_path(project_id) -> dict[int, str]` — the same 1-based ordering, exposed as an index → display-relpath map. Used by the dashboard's truth-conflict modal and reconciliation card to resolve `truth_index` (from the LLM) to a file path the human can be pointed at. Returns the display relpath (with `wiki/` prefix where applicable), not the on-disk absolute path — the dashboard composes the absolute path itself based on prefix.
  - `MAX_FACT_CHARS` and `ALLOWED_SUFFIXES` are public constants for tests + UI introspection.

#### A.13.5 Idempotency: the corpus hash

A SHA-256 over the concatenated corpus text (`text` field, file-by-file, with a separator) — covering all three lanes uniformly. Stored in `team_config['compass_truth_hash_<project_id>']` after every successful Stage 0 run. The hash drives two short-circuits:

  - **Stage 0 truth-derive (§3.0 / Appendix A.14)** — skip the LLM call when the hash is unchanged AND the lattice already has rows with `created_by="compass-truth"`.
  - **Stage 0.1 reconciliation (§3.0.1)** — only fire when the hash changed since the last run; an unchanged corpus can't have introduced new conflicts.

A wiki edit changes the hash same as a truth/ edit or an objectives edit. No source has special hash treatment.

The hash is cleared on `POST /api/compass/reset` so the next run does a fresh full-corpus ingestion (the `truth/` folder, objectives file, and wiki tree are all left intact — reset wipes Compass's view, not the source material).

#### A.13.6 What's "special" about the corpus, restated

Per §1.4, the corpus is special in only two narrow ways. Implementation specifics:

  1. **Compass never amends it.** Verified by the absence of `save_truth` / `mutate.add_truth` / `mutate.update_truth` / `mutate.remove_truth` (all removed during the folder-backed migration). The legacy `POST /api/compass/truth` was deleted; only `GET /api/compass/truth` remains for the dashboard to render the read-only `TruthReference` card.
  2. **The truth-check subroutine** (`server/compass/pipeline/truth_check.py`) feeds the LLM the corpus directly when digesting a Q&A answer. The conflict report cites `truth_index`; the dashboard maps that back to a relpath via `read_truth_index_to_path`.

#### A.13.7 Dashboard surface

  - **`TruthReference` card** on the Compass dashboard: shows the count of truth-corpus facts (across all three lanes), an expand-to-view list (display relpath + 240-char preview per file), and an "open Files pane" button. No edit controls. The kicker reads `TRUTH CORPUS · READ FROM <project>/truth/ + project-objectives.md + wiki/<project>/ ON EVERY RUN` so the human knows what's being fed in.
  - **Truth-conflict modal** (`§3.7`) "Amend truth" path: routes the human at the offending file via the Files pane (resolved through `read_truth_index_to_path`). For wiki-sourced facts (relpath starts with `wiki/`), the displayed path is `/data/wiki/<project_id>/<rest>`; for the other two lanes it's `<project>/<relpath>`. The existing `coord_propose_file_write` flow remains available for Coach-driven amendments to the truth lane; wiki amendments are direct (no proposal flow). There is no in-modal text editor.
  - **Reconciliation proposals** (`§3.0.1` + §6.7): rendered alongside settle / stale / dupe proposals on the lattice column. Each shows the lattice row, the cited corpus file(s) (with the lane visible from the prefix), the explanation, and three resolution buttons (update lattice / update truth / accept ambiguity). The "update truth" button branches link composition on the `wiki/` prefix the same way the truth-conflict modal does.

#### A.13.8 Reset semantics

`POST /api/compass/reset` clears Compass's view but preserves the source corpus:
  - **Wiped**: `data/projects/<id>/working/compass/` (lattice, regions, questions, audits, runs, briefings, proposals, claude_md_block) plus `team_config` keys `compass_bootstrapped_<id>`, `compass_last_run_<id>`, `compass_heartbeat_<id>`, `compass_truth_hash_<id>`.
  - **Untouched**: `data/projects/<id>/truth/`, `data/projects/<id>/project-objectives.md`, `data/wiki/<id>/**`, and the kDrive mirrors of all three. The corpus is the floor; reset is a Compass-side operation.

After a reset, the next Compass run sees `corpus_hash` as missing, runs Stage 0 truth-derive fresh against all three lanes, and re-seeds the lattice from the corpus — restoring the truth-grounded floor automatically.

### A.14 · Stage 0 mechanics — derive + reconcile

This appendix entry covers the Stage 0 implementation glue, complementing the conceptual specs in §3.0 + §3.0.1. The two sub-stages share the corpus hash (A.13.5) and are sequenced together because they both require a fresh corpus read.

#### A.14.1 Pipeline order

```
Stage 0   Read truth corpus, compute hash.
Stage 0a  Truth-derive (§3.0)         → propose new lattice rows from corpus.
Stage 0b  Reconciliation (§3.0.1)     → flag corpus↔lattice conflicts.
Stage 1   Digest answered questions   (the original §3.1).
…
```

Sub-stages 0a and 0b both read the same `state.truth` populated by `load_state`. 0a writes new lattice rows; 0b writes pending reconciliation proposals (no lattice change). They run independently — 0a doesn't gate 0b — so newly-derived statements aren't accidentally flagged by 0b on the same run.

#### A.14.2 Idempotency

Both sub-stages short-circuit when `corpus_hash == previous_hash`. 0a additionally requires `lattice_has_rows_with(created_by="compass-truth")` — if a reset cleared compass-truth rows but the corpus didn't change, we still want to re-seed.

The hash is persisted at the END of Stage 0, after both sub-stages complete, so a partial failure (e.g. LLM error in 0b) doesn't mark the corpus "considered" and skip the next run.

#### A.14.3 Initial weight (truth-derived rows)

`weight=0.75`. Sits in the LEANING-YES band (between 0.5 ignorance and 0.85 settle threshold). The settle proposal flow ignores rows in this band, so truth-derived statements aren't auto-promoted to settled — they have to earn it via subsequent Q&A reinforcement. This keeps the corpus as the floor and the lattice as the working layer above it.

#### A.14.4 LLM caps

  - **Truth-derive**: max 8 statements per run. Caps prompt + reduces noise on a fresh project. The LLM is also told to skip statements already represented in the lattice — so re-runs against an unchanged-but-larger lattice don't re-propose duplicates.
  - **Reconciliation**: no hard cap on the LLM's output; we expect 0–2 conflicts in practice. Every flagged conflict becomes a proposal; the human decides scope.

#### A.14.5 Bus events

  - `compass_truth_derived` — emitted after 0a if any rows were added. Payload: `{added: [statement_ids], run_id}`. Dashboard highlights the new rows.
  - `compass_reconciliation_proposed` — emitted after 0b if any proposals were created. Payload: `{count, conflicting_statement_ids, run_id}`. Dashboard surfaces the proposals card.
  - `compass_phase` — emitted at stage entry/exit so the run-button pulse text reads `truth-derive…` / `reconciliation…`.

#### A.14.6 Net effect

  - **bootstrap** — first run with non-empty truth → lattice is seeded immediately. The human can ask Coach `compass_ask` and get corpus-grounded answers before answering any question.
  - **daily / on_demand** with **unchanged** truth → both 0a and 0b are no-ops (idempotent). The rest of the pipeline runs.
  - **daily / on_demand** with **changed** truth → 0a re-derives (skipping duplicates) AND 0b re-scans for conflicts. New corpus content can both add lattice rows AND surface contradictions with existing rows on the same run.

#### A.14.7 Ambiguity lifetime — clear on corpus change

When the human resolves a reconciliation proposal as `accept_ambiguity`, the cited row is marked `reconciliation_ambiguity=True` and the proposal is removed from `reconciliation.json`. The runner's Stage 0 setup (before 0b runs) clears every `reconciliation_ambiguity` flag whenever `corpus_changed` is true, so the row is eligible for re-detection on the next corpus shift:

```
corpus_hash unchanged → flags survive, suppressed rows stay suppressed → 0b skipped
corpus_hash changed   → all ambiguity flags cleared → rows eligible again
                       → detect_conflicts may re-flag (or not)
```

`pipeline.reconciliation.detect_conflicts` no longer filters by `reconciliation_ambiguity` itself — it only filters by `reconciliation_proposed` (open proposal already exists). The ambiguity-clearing rule lives in `runner.run`, gated by `corpus_changed`, so the eligibility decision is centralized at the run level and trivially auditable. This is the implementation of the §3.0.1 step-4 phrase "until the corpus changes".

#### A.14.8 `update_truth` resolution clears the proposal flag

The "lattice is right, corpus is lagging" path is informational — no lattice mutation, the dashboard routes the human at the offending truth file via the Files pane. But the API endpoint MUST clear `Statement.reconciliation_proposed` on the cited row when this resolution is chosen, otherwise:

  - The next run sees the corpus has changed (hash differs) → Stage 0b runs.
  - `detect_conflicts` filters out the row because `reconciliation_proposed=True` is still set from the prior detection.
  - If the human's edit didn't actually fix the conflict (typo, wrong file, partial edit), Compass would silently miss it.

Implementation: `api.py:resolve_reconciliation` clears the flag explicitly on `action="update_truth"`. The `update_lattice` sub-actions (unarchive / flip / reformulate / replace) clear it via their respective `mutate.reconcile_*` helpers; `accept_ambiguity` clears it via `mutate.reconcile_accept_ambiguity` (which sets `reconciliation_ambiguity=True` simultaneously).

# Truthgate - Spec-Compliance Stage for the Kanban Lifecycle

Status: implementation approach. Phase 1 is implemented: `truthgate` is a real task status/board column, backlog promotion enters it without planting or waking a Player role, task rows carry TruthGate scalar fields, and `truthgate -> plan|execute` is rejected until a pass/override verdict is recorded. Phase 2 classifier core is implemented as a library-only package under `server/truthgate/`. Phase 3 manual Coach tooling is implemented with `coord_run_truthgate` and `coord_record_truthgate_override`, including task-row persistence and event recording. Phase 4 protected amendment wrapper is implemented with `coord_propose_truth_amendment` over the existing file-write proposal flow. Phase 5 attention surfaces are implemented for Kanban card chips, Coach coordination-block rollups, EnvPane pending-action visibility, and compact timeline events. Phase 6 targeted audit integration is implemented: auditor wakes include targeted TruthGate context and audit PASS fails closed when the cited basis is violated or cannot be checked. Phase 7 provisional closure is implemented with `coord_record_provisional_closure` and delivered-archive gates.

## Implementation status - implemented phases

Implemented classifier-core pieces:

- `config.py`: TruthGate environment parsing, budget knobs, and strict classifier model validation. Defaults are `latest_sonnet` primary and `latest_mini` fallback. `latest_opus`, `latest_gpt`, and their current concrete model targets are rejected for classifier use.
- `corpus.py`: capped `truth/**/*.{md,txt}` corpus slicing. It prioritizes core truth files, then task-keyword-relevant files, then alphabetical fallback. It does not read `Docs/`, repo source, uploads, conversation logs, or secrets.
- `prompts.py`: strict JSON classifier prompt and amendment-draft prompt helper.
- `llm.py`: one-shot primary/fallback wrapper with `agent_id="truthgate"` and classifier ledger attribution.
- `classifier.py`: per-project lock, cost-cap preflight, sparse-mode routing, strict whole-response JSON parsing, deterministic acceptance of a single whole-response markdown `json` fence around the classifier object, verdict normalization, and truth-basis validation.
- `sparse.py`, `targeted.py`, and `amendments.py`: sparse pass result, targeted truth-basis reads/audit guards, and amendment metadata helpers for later phases.
- `coord_run_truthgate`: Coach-only tool that runs the classifier for a task in `truthgate`, persists verdict/basis/concerns/method/model fields, emits `task_truthgate_started`, `task_truthgate_completed`, and `task_truthgate_blocked` when the verdict requires amendment or clarification. Existing verdicts are preserved unless Coach passes `force=true`; classifier failures fail closed by blocking the task without recording a pass/override verdict. It does not advance the stage or wake a Player.
- `coord_record_truthgate_override`: Coach-only tool that records `truthgate_coach_override` or `truthgate_emergency_override` with required rationale, emits override/completed events, and marks emergency overrides provisional. Emergency overrides may store an optional `closure_reference` for later reconciliation. It does not advance the stage or wake a Player.
- `coord_propose_truth_amendment`: Coach and active-Player-role wrapper that queues a normal protected `file_write_proposals` row with `scope="truth"`, `metadata_json`, and `originating_task_id`. It does not write `truth/` directly and preserves the existing human approve/deny flow. `draft_instruction` / LLM amendment drafting remains deferred.
- `coord_record_provisional_closure`: Coach-only tool that validates and stores a provisional task's `closure_reference`, emits `task_provisional_closure_recorded`, and does not advance the stage or wake a Player. Delivered archive through `coord_archive_task` or `approve_stage(next_stage="archive")` rejects provisional tasks until closure is valid; human cancellation remains available and is recorded as cancellation.

Current mocked-LLM/tool tests cover sparse mode, dense-corpus prompt-budget truncation, slicer ordering, strict parser failure, model validation, basis validation, per-project concurrency locking, Coach-only Phase 3 tools, verdict persistence, blocked needs-change verdicts, force-rerun protection, classifier-error fail-closed behavior, override rationale validation, post-override exit gating, Phase 4 amendment proposal approval/denial correlation, Phase 6 targeted audit wake/PASS-guard behavior, and Phase 7 provisional closure/archive gating. Protected truth mirror tests are temporarily waived by human directive; the matching `truth/` projection should be proposed through the protected flow after the waiver lifts.

## Core idea

`truth/` is authority. `Docs/` is projection or workspace. Tasks must pass a spec-compliance check (truthgate) before implementation begins. Emergency work can bypass the gate but must reconcile afterward.

The inversion is:

> **Truth authorizes tasks. Tasks do not create truth by being implemented.**

This closes the failure mode where implementation silently establishes facts on the ground that contradict (or extend beyond) what truth says, and the project's actual rules drift away from its written rules.

This is specs-driven development (SDD) as a harness invariant:

```
prompt / task intent
  -> update or confirm protected truth/specs
    -> implement against approved truth
      -> audit implementation against that truth
```

The normal path should not need post-hoc spec changes, because the task should already have updated or confirmed the relevant truth before implementation starts. Reactive amendments still exist for discoveries that only become visible during execution, but they are an exception path, not the development model.

## Where truthgate sits relative to Compass

Compass and truthgate are **orthogonal**, both required, neither subsumes the other.

| | Compass | Truthgate |
|---|---|---|
| **Asks** | "Is this aligned with where we're going?" | "Is this allowed by what we've decided?" |
| **Operates on** | Intent, direction, scoping, what we're NOT building | Hard rules, invariants, specs, binding constraints |
| **Reads** | `truth/ + project-objectives.md + wiki/<project>/` as intent material | `truth/` as spec corpus |
| **Cadence** | Daily run + on-corpus-change | Per task on Coach promotion from backlog into the live Kanban trajectory |
| **Output** | Weighted lattice + reconciliation proposals | Per-task verdict + truth_basis |

A task can pass Compass and fail truthgate ("fits the product direction, but violates the codex-sandbox spec"). A task can pass truthgate and fail Compass ("allowed by specs but contradicts the v1 scope decision"). Both run, both produce verdicts, Coach reconciles when they disagree.

The 2026-05-04 Compass refocus deliberately narrowed scope to intent. Truthgate fills the spec-compliance gap Compass intentionally doesn't cover.

## Revised kanban lifecycle

```
backlog
  -> truthgate          (new visible Kanban column - required before any plan/execute)
    -> plan
      -> execute
        -> audit
          -> ship/archive
```

Emergency path:

```
backlog
  -> emergency_override (human approves bypass)
    -> plan/execute     (task marked provisional)
      -> audit
        -> ship/archive (requires reconciliation reference)
```

The truthgate stage is **mandatory** for every task promoted out of backlog. It appears as its own visible Kanban column between backlog and plan. Coach may fast-path trivial implementation work (typo fixes, renames, dep bumps), but that is recorded as an explicit override verdict rather than a silent pass.

## Truthgate flow

When Coach explicitly promotes a task from `backlog` to the live trajectory, the task enters the `truthgate` column. Coach first decides whether the task is trivial enough for an override; otherwise the harness runs a dedicated classifier call:

1. **Inputs**: task title + description + objective, plus a curated slice of the project's `truth/**/*.{md,txt}` corpus (always-include core truth files + keyword-relevant files + alphabetical fallback, capped at ~32 KB - same pattern as TruthScore's truth-budget). Sparse-mode eligibility is based on the actual eligible truth file count before prompt-budget slicing, not on how many files fit in the prompt.
2. **Model**: dedicated one-shot LLM call. `latest_sonnet` is the preferred default; `latest_mini` is the automatic fallback when Sonnet is unavailable, rate-limited, or out of credit. `latest_opus` and `latest_gpt` are excluded from classifier use because this is structural pattern-matching, not deep reasoning. This restriction applies to the truthgate classifier only; drafting protected truth/spec changes uses top models, defined below.
3. **Output**: strict JSON with verdict + truth_basis (list of truth files/sections the task is authorized against) + optional truth_concerns (specific clauses the task should respect during implementation). The parser also accepts the same JSON object when it is the only content inside one markdown `json` fence; malformed fenced JSON, multiple snippets, partial snippets, or extra prose still fail closed.

The five verdicts:

| Verdict | Meaning | Next step |
|---|---|---|
| `truthgate_pass` | Task fits within existing truth. | Task proceeds to `plan`. `truth_basis` recorded. |
| `truthgate_needs_truth_change` | Task requires new or modified truth before work begins. | Coach files a truth amendment proposal using the mechanism defined below. Task pauses until amendment is approved or denied. |
| `truthgate_rejected_or_needs_human_clarification` | Task is too vague, contradicts truth in a way warranting discussion, or shouldn't be done as stated. | Coach escalates via `coord_request_human` or sends task back to backlog. |
| `truthgate_coach_override` | Coach judges the task trivial or no-truth-contact and records why the classifier was skipped. | Task proceeds to `plan` with `truth_basis: []` and a required non-empty override rationale. |
| `truthgate_emergency_override` | Human pre-approved bypass for urgent work. | Task marked `provisional=true`, jumps to `plan`. Reconciliation required before archive. |

## `truth_basis` as a recorded artifact

A new field on `tasks` storing the truthgate verdict's payload:

```json
{
  "truth_basis": [
    "truth/runtime/codex-tools.md#tool-contract-version",
    "truth/kanban/lifecycle.md#truthgate"
  ],
  "truth_concerns": [
    "section 3.2 forbids changing runtime mid-turn"
  ],
  "truthgate_verdict": "truthgate_pass",
  "truthgate_at": "2026-05-16T10:00:00Z",
  "truthgate_model": "latest_sonnet",
  "truthgate_method": "classifier"
}
```

Denormalized onto the task row for query speed; also emitted as a `task_truthgate_completed` event payload for the timeline.

Downstream uses:

- **Audit stage** checks the implementation against the `truth_basis` files specifically, not the whole corpus. Targeted + faster than a blanket TruthScore run.
- **Coach's coordination block** surfaces tasks blocked on pending amendments and their originating tasks.
- **Truth file changes** can enumerate affected active tasks (those whose `truth_basis` includes the changed file) so reviewers see blast radius before approving an amendment.
- **Reconciliation** of provisional tasks needs `truth_basis` populated as part of the closure reference defined in this document.

## Model choice - canonical aliases

Truthgate should name canonical model aliases, not dated model versions:

- **`latest_sonnet` (preferred classifier default)**: better at fine-grained semantic match between task description and spec language. More forgiving of variation in how truth files are written.
- **`latest_mini`**: cheaper and faster, slightly less nuanced on spec semantics. Used as the automatic classifier fallback when `latest_sonnet` is unavailable, rate-limited, or out of credit, and acceptable as the primary classifier model when the truth corpus has been deliberately curated for clarity.

Recommendation: ship with `HARNESS_TRUTHGATE_MODEL=latest_sonnet` and `HARNESS_TRUTHGATE_FALLBACK_MODEL=latest_mini`. Use the existing alias resolution in [server/models_catalog.py](server/models_catalog.py) so future Sonnet/mini versions auto-promote.

Fallback detection mirrors Compass's Codex-fallback pattern: call the primary model, catch the primary-call availability/rate-limit/credit error, then latch fallback mode for the current run or batch so a Sonnet outage does not re-pay the failed primary probe for every task.

Hard rule for the classifier: **`latest_opus` and `latest_gpt` are explicitly excluded** from `HARNESS_TRUTHGATE_MODEL` and `HARNESS_TRUTHGATE_FALLBACK_MODEL`. Validation on those env vars rejects them.

## Cost model

One classifier call per non-overridden task on Coach promotion from backlog into the truthgate column. Rough sizing:

- Task description: ~500 chars in
- Truth corpus slice: ~32 KB in (capped)
- Output JSON: ~500 tokens out

Per call at current `latest_sonnet`-class pricing: roughly **$0.01-0.02** per gate. At 50 tasks/week: ~$0.50-1.00/week. At 200 tasks/week: ~$2-4/week. Negligible against existing Compass + TruthScore + Coach spend.

The classifier runs as a one-shot direct LLM call (mirror Compass's `compass.llm.call` wrapper - write `truthgate.llm.call`). Cost lands in the existing `turns` ledger under `agent_id="truthgate"`, `cost_basis="truthgate:run"` so it rolls into team daily caps and the EnvPane meter.

The classifier does **not** piggyback on a Coach turn - Coach may be running a premium model, and the whole point of the dedicated call is to keep this cheap.

## Truth amendment mechanism

Truthgate needs a way to pause a task and ask the human to change protected truth before work begins. The first implementation should reuse the existing protected file-write proposal flow rather than introduce a separate registry:

```
coord_propose_file_write(scope="truth", path="truth/...", body_or_diff="...")
```

Drafting protected truth/spec changes is high-authority work and must use a top model. The default amendment-drafting model is `latest_gpt`; the fallback is `latest_opus` if `latest_gpt` is unavailable, out of credit, or blocked. `latest_mini` is acceptable for the truthgate classifier, but not for drafting canonical truth/spec updates.

The proposal must carry enough structured context for the human to decide without reading a raw diff first:

- originating task id
- rationale for why current truth is missing, stale, or contradictory
- proposed truth change, as a full replacement body or a validated unified diff
- evidence links: task ids, commit shas, file paths, audit reports, or relevant conversation/event ids
- affected `Docs/` files that may need projection refresh after approval
- provisional implementation flag, if work has already started against the proposed truth
- rejection consequence: stop task, rewrite task, or rollback follow-up

This may be exposed as a convenience tool named `coord_propose_truth_amendment`, but that tool is a wrapper over the protected file-write proposal flow, not a new storage model. If the wrapper exists, it should be available to Coach and Players; if it does not exist yet, Coach can use the existing protected truth proposal path directly.

Approval writes `truth/` and unblocks the originating task for a fresh truthgate pass. Denial leaves truth unchanged and sends the task back to Coach for rewrite, cancellation, or human discussion.

## Mid-execute discovery

Even with truthgate in place, agents will still occasionally discover truth is wrong during execution. That reactive path uses the same truth amendment mechanism:

```
execute
  -> truth discrepancy found
    -> propose truth amendment with provisional_impl=true and task_id=...
      -> task continues as provisional
        -> reconciliation required before archive
```

Truthgate is best-effort pre-check; some divergences only become apparent under implementation. The same amendment mechanism handles both pre-work truthgate failures and mid-execute discoveries.

## Audit's role

Audit's existing v2 checks (implementation matches task spec) extend with two additions:

1. **Implementation respects `truth_basis`** - read the cited truth files, verify no clause is violated. Targeted, not corpus-wide. Cheap because the slice is already known.
2. **No load-bearing claim exists only in `Docs/`** - any rule the implementation depends on must trace to `truth/` (via `truth_basis`) or be flagged for amendment.

The implemented targeted truth check is a focused guard, not a corpus audit or full TruthScore run. It reads only the task's cited `truth_basis` files for auditor wake context. Missing, stale, unreadable, or malformed cited basis metadata is surfaced for Coach review; audit PASS is rejected when the report identifies a cited-truth violation or when the cited basis cannot be checked. Empty-basis sparse/override tasks skip the file read with a visible warning.

## Docs projection - record only, don't auto-render in v1

Approved amendment proposals may carry `affected_docs` entries naming `Docs/` files that should be refreshed after approval. **Truthgate v1 does not auto-render these.** Reasons:

- LLM reconciliation is expensive and non-deterministic.
- Mechanical templating loses project-specific tailoring (the canonical CLAUDE.md template learned this - [server/project_claude_md.py](server/project_claude_md.py) uses Coach LLM reconciliation, not templating).
- Manual update is reliable and cheap until a real volume problem appears.

The recorded `affected_docs` surface as a coordination-block reminder for Coach ("Truth `foo.md` changed; `Docs/bar.md` and `Docs/baz.md` may now be stale") and the human acts on it. Auto-projection becomes a candidate phase only after manual proves to be a real bottleneck.

## Bootstrap (empty `truth/`)

For new projects, `truth/` is mostly empty. Truthgate gracefully degrades:

- If `truth/` has fewer than `HARNESS_TRUTHGATE_MIN_CORPUS_FILES` (default 3) eligible `.md`/`.txt` files before budget slicing, truthgate returns `truthgate_pass` with `truth_basis: []` and a warning surfaced in the coordination block: "Truth corpus is sparse; truthgate is permissive until you populate it."
- The first amendments filed on a new project bootstrap the corpus.
- After the threshold is crossed, truthgate engages with full classifier behavior.

This avoids the "every task needs truth_change because there's nothing to validate against" failure mode without making the gate optional.

Sparse-corpus passes are recorded as `truthgate_verdict: "truthgate_pass"` with `truthgate_method: "classifier_sparse"` and `truth_basis: []`. Audit can skip the targeted truth-basis check for these tasks while still surfacing the sparse-corpus warning.

## Coach override fast path

For trivial implementation work (typo, rename, dep bump, log-message wording, a clear no-truth-contact fix), Coach may skip the classifier and record `truthgate_coach_override` with `truth_basis: []`, `truthgate_method: "coach_override"`, and a short `override_rationale`.

The `override_rationale` is required and must be non-empty; blank override rationales are rejected. This is riskier than a classifier pass, so it must be visible as an override rather than disguised as `truthgate_pass`. It is still preferable to an opt-out via trajectory preset: every task passes through the same visible Kanban column and leaves a uniform truthgate record.

The override decision piggybacks on Coach's normal backlog-promotion turn. It should not create a dedicated Coach turn or add premium-model cost beyond the work Coach was already doing to curate the backlog.

## Provisional closure reference

Emergency overrides and mid-execute truth discoveries may allow work to proceed provisionally. Provisional tasks cannot fully archive until Coach records a `closure_reference` with one of these forms:

```text
amendment:<proposal_id>        # links to a truth amendment proposal; delivered archive requires approved status
none_needed:<rationale>        # non-empty rationale explaining why no truth change is warranted
rollback:<task_id>             # follow-up task that will undo or neutralize the provisional work
```

This preserves the ability to fix urgent issues without letting emergency work silently rewrite project truth. The closure tool accepts pending amendment proposals for tracking, but final delivered archive requires `amendment:<proposal_id>` to reference an approved `truth/` proposal. Cancellation can still archive a task with `cancelled_at`; that path is not a delivered closure.

## Relationship to existing systems

| System | Role |
|---|---|
| **Truth amendment mechanism** | Protected truth proposal flow used when truthgate returns `needs_truth_change` or execution discovers truth drift. Implemented by existing `coord_propose_file_write(scope="truth")`, optionally wrapped by `coord_propose_truth_amendment`. |
| **Compass** | Intent-axis check. Runs orthogonally on its own cadence. May share retrieval infrastructure (file walker, corpus hash) with truthgate as an optimization. |
| **TruthScore** | Post-hoc spec-compliance audit at project scope. Audit stage can invoke a targeted variant against the task's `truth_basis`. A later enhancement may let low TruthScore results auto-file truth amendment proposals. |
| **Kanban v2** | Provides lifecycle scaffolding (project_events, stage transitions, Coach approval). Truthgate adds one new stage + one new event family. |
| **Canonical project CLAUDE.md template + reconciliation** | Documents authority hierarchy (`truth/` > `project-objectives.md` > `wiki/<project>/` > `Docs/`). Propagates to projects via existing Coach-driven reconciliation. |

## What this is not

- A replacement for Compass - Compass continues on its own cadence and scope.
- A replacement for TruthScore - TruthScore continues as broader post-hoc audit; truthgate uses a targeted variant during the per-task audit.
- An auto-renderer for `Docs/` - manual updates only in v1, with explicit recorded reminders.
- A way to skip work - emergency override exists but requires the closure-reference shape defined below.
- Optional - every task passes through it, but the Coach override fast path keeps cost negligible while remaining visible.

## Implementation order

1. **Truth amendment proposal wrapper**: either confirm the existing protected truth proposal path is sufficient, or add `coord_propose_truth_amendment` as a thin wrapper over it with the structured metadata listed above.
2. **Truthgate 2a**: classifier + schema + the new visible Kanban stage + verdict recording. Coach manually approves transition into plan based on the verdict. No automatic stage advance yet.
3. **Truthgate 2b**: automated stage advance on `truthgate_pass`; coordination-block surfaces for pending amendments and stale-Docs warnings; audit-stage targeted truth check.
4. **Provisional closure gate**: emergency/provisional tasks need a `closure_reference` to archive.

Rough sizing: amendment wrapper ~= 2-4 working days if needed; truthgate 2a ~= 5-7 working days; truthgate 2b ~= 3-5 days; provisional closure ~= 2-3 days. About 2-3 weeks end-to-end depending on how much of the existing protected file-write proposal flow can be reused unchanged.

## Open questions

1. **`truth_basis` granularity - file-level or section-level?** Section anchors (`truth/foo.md#section`) are more precise but require truth files to use stable anchors. File-level is the conservative start; tighten to sections once anchor discipline exists.
2. **What happens when a truth file referenced by an active task's `truth_basis` is amended?** Options: re-run truthgate for the task; flag the task for Coach review; do nothing (trust the audit stage to catch it). Probably "flag for Coach review" - auto-rerun could cascade unnecessarily on a broad amendment.
3. **Truth corpus slicing for the classifier prompt** - TruthScore's pattern (always-include set + keyword match + alphabetical fallback, ~32 KB cap) is the obvious start. Whether per-project tuning is needed for large corpora is a v2 question.
4. **Compass/truthgate corpus reads** - both walk `truth/` per run. Cheap optimization: share a per-project corpus-hash + cached file list across both modules so neither re-walks the disk when nothing changed.

## Risks

- **Classifier false negatives** ("pass" when it shouldn't). The targeted audit at the audit stage is the safety net - implementation that violates the cited truth_basis gets caught before ship.
- **Classifier false positives** (flags `needs_truth_change` for tasks that don't really need one). Coach override path handles this; record the override so the classifier's behavior can be tuned over time.
- **Latency on task promotion**. ~2-5 seconds per non-overridden truthgate call. Not interactive - the human creates a backlog item, Coach later promotes it to truthgate, the gate runs async, the verdict lands when ready. UI shows "truthgate pending" badge.
- **Cost surprise**. Bounded by per-call cost x task volume; even at high volumes the total is small. `HARNESS_TEAM_DAILY_CAP` fail-closed check before each gate run is the backstop.
- **Bootstrap awkwardness**. Empty-corpus permissive mode is the answer, but a project sitting at 2 truth files for months without growing the corpus would never get the gate's benefits. Coordination-block warning when corpus is below threshold + N days old.

## Acceptance criteria (when the truthgate work is "done")

1. Every task promoted out of backlog gets a recorded `truth_basis` (possibly empty) and a `truthgate_verdict` before entering `plan`.
2. Tasks blocked on `needs_truth_change` surface in Coach's coordination block and in the EnvPane attention strip; pending amendment entries include a clear review action that opens the existing EnvPane file-write proposal diff/approve/deny/comment surface for that proposal. Approve still writes only through the protected proposal resolver, while deny/drop and request-changes outcomes require a human note and notify Coach with the next step.
3. Emergency overrides exist, are marked provisional, and require a closure reference to archive.
4. Audit stage performs the targeted truth-basis check; failures surface as `audit_report_submitted{fail}` events with the violated clause cited.
5. Per-gate cost stays under $0.05 average across realistic task corpora (`latest_sonnet` config); usage rolls into the existing daily cap.
6. The Compass/truthgate boundary is preserved - neither system tries to do the other's job, and they can disagree without breaking the lifecycle (Coach reconciles).

## Final framing

Truthgate operationalizes the authority hierarchy as a workflow rule rather than a discipline rule. Compass keeps the project pointed in the right direction; truthgate keeps the project from contradicting its own decisions. The protected truth amendment mechanism is how disagreements get resolved.

The three together - Compass, truthgate, and protected truth amendments - form a complete loop: direction, rules, and revision.

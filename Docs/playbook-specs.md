# Playbook — Specification

> **Subordinate to [TOT-specs.md](TOT-specs.md).** When this doc and
> TOT-specs disagree, TOT-specs wins. This file goes deeper on the
> playbook subsystem (lattice, reflection runner, proposal pipeline,
> system-prompt injection path) but cannot redefine fields, endpoints,
> events, or invariants that TOT-specs declares.

**Status:** DRAFT (2026-05-08). Pre-implementation, post-second-audit.
**Target:** TeamOfTen multi-agent harness (Python, Claude Agent SDK, kDrive-backed shared state, single-VPS)
**Version:** 0.3 (post-second-audit)

---

## 0 · One-paragraph summary

The Playbook is an autonomous **orchestration-strategy engine** that runs alongside the harness — harness-wide, not per-project. It maintains a single weighted lattice of conceptual, runtime-agnostic statements about *how to coordinate a multi-agent team* (e.g. *"audit every code change except the trivially mechanical"*, *"Sonnet at medium effort handles ~80% of code tasks adequately"*, *"don't loop the same executor↔auditor pair more than twice on the same kind"*). Each statement carries a weight in [0, 1] interpreted as P(this pattern is the right play for this team). Every agent reads the active lattice on every turn (loaded into the system prompt via the same universal path as CLAUDE.md); only Coach can propose changes. A daily reflection turn — running on a single Sonnet call — reads the last 24h of team activity (archived tasks, audit fails, stalls, Compass verdicts, deviations, plus *violations* of high-confidence rules) and proposes weight adjustments / new statements / merges through the engine. Coach never writes the lattice file directly. Empty proposals are a valid outcome.

The Playbook is to **AI orchestration strategy** what Compass is to **human intent**. Same lattice primitive, deliberately simpler implementation: single harness-wide file, no regions, no truth corpus, no Q&A, no briefings, no per-artifact audit watcher, no immutable seeds in v1.

---

## 1 · Conceptual model

### 1.1 What's in the lattice

Statements are **atomic, conceptual, runtime-agnostic** claims about how to run the team. Each is something Coach could act on as a directional rule.

Examples of well-formed statements:

- *"Audit every code-touching task except trivially mechanical edits"* (weight 0.85)
- *"For coding work, medium effort with Sonnet (or GPT-mini on Codex) is enough — bump only on repeated audit fail"* (weight 0.75)
- *"Two-axis audit (formal + semantic) catches different bug classes; use both for non-trivial changes"* (weight 0.80)
- *"Plan-mode adds value when the task spans more than 2 files or touches contracts"* (weight 0.65)
- *"Continuity beats rotation — assign follow-up work in an area to the Player who already has context"* (weight 0.70)
- *"When Compass returns `aligned` 3× in a row on questionable work, suspect lattice drift, not executor drift"* (weight 0.60)

Examples of statements that **don't belong**:

- *"Use `coord_approve_stage` for transitions"* — that's a hardcoded harness rule (lifecycle policy §14.1 of kanban-specs-v2), not a learned pattern.
- *"Coach never writes implementation code"* — this *might* be load-bearing discipline, but it's also already in the lifecycle-policy block. Don't duplicate; if reflection finds a counter-example worth recording, the *counter-example* becomes a playbook statement (e.g. *"For trivial typo fixes, Coach editing directly beats dispatching"*).
- *"p3 is good at frontend"* — runtime-specific to current roster; the playbook is harness-wide and the roster changes per project.
- *"The kDrive token rotates every 90 days"* — operational fact, not orchestration strategy.
- *"Coach should be helpful"* — too vague to validate.

A statement is well-formed if:

1. It can be observed in action ("did this pattern fire? did it pay off?") — so the daily reflection can adjust its weight against evidence.
2. It generalizes across projects — applies whether the project is a backend API, a marketing site, or a research notebook.
3. Its negation is meaningful — a confident NO is as useful as a confident YES (e.g. *"Don't bump model tier on first audit fail"*).
4. It is conceptual, not procedural — the lifecycle policy lists procedures; the playbook lists patterns Coach has reason to believe (or disbelieve) are good plays.

### 1.2 Weights

Each statement carries `weight ∈ [0.0, 1.0]` = P(this pattern is the right play for this team).

| Range | Meaning | Coach should treat as |
|---|---|---|
| `> 0.85` | Validated YES — eligible to settle | Established discipline; deviate only with explicit reason |
| `0.65 – 0.85` | Leaning YES | Working hypothesis, follow by default |
| `0.35 – 0.65` | Genuine uncertainty | Use judgement; don't lean on this rule |
| `0.15 – 0.35` | Leaning NO | Working anti-hypothesis |
| `< 0.15` | Validated NO — eligible to settle | Validated anti-pattern; explicit reason needed to apply |

The reflection runner pushes weights away from 0.5 toward whichever pole the evidence supports. Sitting at 0.5 means the playbook has learned nothing about that statement.

Bootstrap weight = **0.75** (leaning YES, conservatively). New statements created by Coach mid-life cycle = **0.60** (slightly under bootstrap since not yet seeded by the original prose corpus).

### 1.3 Scope — harness-wide, not per-project

Compass is per-project (each project has its own lattice of intent). The playbook is **harness-wide** (a single lattice shared by every project's Coach). Rationale:

- Orchestration patterns generalize across projects — *"audit code changes"* works for a Python backend and a marketing site equally.
- Per-project playbooks would each have a tiny evidence base (one project rarely produces enough archived tasks per day to move weights).
- A stable shared lattice across project switches benefits the human (consistent team behavior) and benefits agents (no cold-start when entering a new project).

If a pattern is genuinely project-specific, it belongs in the project's CLAUDE.md, not the playbook.

### 1.4 No immutables in v1

The playbook is purely the **evolved layer**. Hard architectural rails (Coach-only stage transitions, the no-auto-revert audit path, the Coach-only `coord_*` tool gating) live in code and are *described* in the kanban-specs-v2 §14.1 lifecycle-policy block. Putting them in the playbook would misframe them as "things Coach chooses to do" when they're things the code makes inevitable.

The schema retains an `immutable: true` field on each statement as **latent capability** — if a future genuinely-must-never-erode pattern emerges, the lock is there. v1 ships with zero seeded immutables.

### 1.5 What's deliberately NOT in scope (vs Compass)

The playbook is simpler than Compass by design:

| Compass has | Playbook has | Why simpler |
|---|---|---|
| Per-project lattices | Single harness-wide lattice | Patterns generalize; no scoping needed |
| Region tags + auto-merge of regions | Flat list (no regions) | ≤ 100 statements; flat is readable |
| Truth corpus (truth/, project-objectives.md, wiki/) | None | No external constraint layer; reflection IS the truth |
| Truth-derive (Stage 0a) | Bootstrap from `app_dev_playbook.md` once | Single seeding event, not continuous |
| Reconciliation proposals (Stage 0b) | None | No truth corpus to reconcile against |
| Q&A flow + question generation | None | Coach reflects on observed evidence, doesn't ask the human |
| Daily briefings | None | The lattice IS the briefing |
| CLAUDE.md block injection (`<!-- compass:begin -->`) | Direct system-prompt injection via `build_system_prompt_suffix` | One read path, no marker dance |
| Audit watcher (commit/decision/knowledge/output → audit) | None | No artifact-level scoring; daily reflection only |
| MCP `compass_ask` / `compass_audit` / `compass_brief` / `compass_status` | One MCP tool: `coord_propose_playbook_changes` | Lattice already in every agent's prompt; no query tool needed |
| Presence requirement (human heartbeat) | None | The reflection doesn't ask the human anything; runs autonomously |
| Immutable bedrock seeds | None (latent capability only) | Hard rails live in code + lifecycle policy |

What **carries over** from Compass:

- Lattice + archived buckets, atomic JSON write + kDrive mirror.
- 0/0.5/1 human override mechanism (NO/½/YES buttons in dashboard).
- Settle / stale proposal mechanism.
- Direct `claude_agent_sdk.query()` call (no MCP, no resume).
- Sonnet medium default + Codex mini fallback (the §5.5.2 pattern).
- Cost tracking via the `turns` ledger under `agent_id="playbook"`.

---

## 2 · Storage layout

```
/data/playbook/
  lattice.json           # active statements (cap soft 100, hard 110)
  archived.json          # settled / stale_low / stale_unused / merged / superseded / deleted
  runs.jsonl             # one line per reflection run

# kDrive mirror (synchronous on every write):
TOT/playbook/
  lattice.json
  archived.json
  runs.jsonl
```

No `proposals/` subfolder — proposals are ephemeral within a run; either applied immediately by the runner or dropped. (Compass has persisted proposals because it surfaces them for human approval over multiple sessions; playbook applies same-turn so persistence is unnecessary.)

No `briefings/` subfolder — playbook doesn't generate daily summaries.

---

## 3 · Schema

### 3.1 `lattice.json`

```json
{
  "schema_version": 1,
  "updated_at": "2026-05-07T04:15:23.123Z",
  "statements": [
    {
      "id": "pb-001",
      "text": "Audit every code-touching task except trivially mechanical edits",
      "weight": 0.85,
      "weight_history": [
        {"ts": "2026-05-01T04:00:00Z", "from": null, "to": 0.75, "reason": "bootstrap"},
        {"ts": "2026-05-07T04:15:23Z", "from": 0.75, "to": 0.85, "reason": "validated by 3 audit-caught bugs in t-abc, t-def, t-ghi"}
      ],
      "created_at": "2026-05-01T04:00:00Z",
      "created_by": "bootstrap-playbook",
      "last_validated_at": "2026-05-07T04:15:23Z",
      "applied_count": 14,
      "immutable": false
    }
  ]
}
```

Field notes:

- `id` — stable string (`pb-NNN`, monotonic). Survives merges via `archived.json` cross-reference.
- `text` — the statement body. **Hard cap `STATEMENT_MAX_CHARS` (default 160 chars, env-overridable via `HARNESS_PLAYBOOK_STATEMENT_MAX_CHARS`)** enforced on every insert path (Coach `coord_propose_playbook_changes`, daily reflection creations, bootstrap seeds). One line, imperative, no enumerated sub-items — the WEIGHT carries confidence; the text just needs to trigger recall. Detail and rationale belong in the prose corpus, not the lattice statement (lattice statements get injected into every agent's system prompt on every turn). Aim for ~120 chars typical.
- `weight` — float in [0.0, 1.0].
- `weight_history` — append-only list of weight transitions with reason. Cap at 50 most recent entries (older trimmed during write); `runs.jsonl` is the durable audit trail.
- `applied_count` — integer; incremented by the daily reflection based on Coach's `relevant_ids` list (§5.5) — every statement Coach lists as "the day's events touched on this pattern" gets `+1`, whether or not weight was adjusted. Used by the dashboard to surface "frequently observed" via the sort key `weight × log(1 + applied_count)` (so a high-weight rule that fires often ranks above a high-weight rule that almost never fires). Monotonic — never decrements.
- `immutable` — when true, weight is locked at 1.0 and neither the daily runner nor `coord_propose_playbook_changes` can adjust weight. **No statements ship with this flag set in v1.** The flag remains in the schema as latent capability for future genuinely-must-never-erode patterns. Default false.
- `last_validated_at` — UTC timestamp of the most recent reflection run that touched this statement in any way: appearance in `relevant_ids`, application of an `adjust` op, OR receipt of merged-in `applied_count` from a `drop_id`. NULL until first observation.

### 3.2 `archived.json`

```json
{
  "schema_version": 1,
  "statements": [
    {
      "id": "pb-007",
      "text": "...",
      "final_weight": 0.96,
      "archived_at": "2026-05-12T04:00:00Z",
      "archive_reason": "settled",
      "merged_into": null,
      "history": [...]
    }
  ]
}
```

`archive_reason ∈ {"settled", "stale_low", "stale_unused", "merged", "superseded", "deleted"}`.

- `settled` — weight ≥ 0.95 stable for ≥ 7 days (§5.8). Statement is team consensus; no longer needs surfacing in the active lattice but readable for history.
- `stale_low` — weight ≤ 0.15 stable for ≥ 7 days. Pattern was tried and didn't pan out.
- `stale_unused` — `applied_count = 0` AND `created_at` ≥ 30 days ago. Pattern never fires; not worth carrying.
- `merged` — collapsed into another statement; `merged_into` carries the target id.
- `superseded` — replaced by a reformulated statement (e.g. Coach proposes a clearer wording).
- `deleted` — human deletion via dashboard.

### 3.3 `runs.jsonl`

One JSON line per reflection run. Each line:

```json
{
  "run_id": "pbrun-2026-05-07-04-00",
  "started_at": "2026-05-07T04:00:01Z",
  "finished_at": "2026-05-07T04:01:34Z",
  "kind": "daily" | "manual" | "bootstrap",
  "evidence_window": {"from": "2026-05-06T04:00:00Z", "to": "2026-05-07T04:00:00Z"},
  "evidence_summary": {
    "tasks_archived": 7,
    "audit_fails": 2,
    "stall_events": 0,
    "compass_drift_verdicts": 1,
    "deviations_logged": 3,
    "human_attention_events": 1,
    "median_cost_fallback_fired": false
  },
  "relevance_increments": 12,
  "proposals_applied": [
    {"op": "adjust", "id": "pb-001", "from": 0.75, "to": 0.85, "reason": "..."},
    {"op": "create", "new_id": "pb-031", "text": "...", "weight": 0.6, "reason": "..."}
  ],
  "proposals_rejected": [
    {"op": "adjust", "id": "pb-014", "delta": 0.5, "reason": "delta exceeds ±0.25 cap"}
  ],
  "engine_actions": [
    {"action": "settle", "id": "pb-003", "final_weight": 0.97}
  ],
  "llm_call": {
    "model": "claude-sonnet-4-6",
    "runtime": "claude",
    "input_tokens": 4231,
    "output_tokens": 612,
    "cost_basis": "playbook:reflection",
    "cost_usd": 0.0184
  },
  "outcome": "applied" | "no_changes" | "skipped_no_activity" | "skipped_cost_cap" | "error_llm" | "error_parse"
}
```

`relevance_increments` is the sum of all `applied_count` increments applied this run (so the run row reflects the activity volume that Coach observed). Trim to last `HARNESS_PLAYBOOK_RUNS_RETENTION` lines (default 90 = ~3 months) on each write.

**Bootstrap rows** (`kind: "bootstrap"`) use a reduced shape — bootstrap has no evidence and no proposals, just seed insertions. For bootstrap rows:
- `evidence_window` is `null`.
- `evidence_summary` is `null`.
- `relevance_increments` is `0`.
- `proposals_applied` and `proposals_rejected` are empty arrays.
- `engine_actions` is empty array.
- A new optional field `seeds_inserted: int` carries the count of seed statements persisted (zero when the prose template is missing on disk).
- A new optional field `source: "boot" | "reset"` mirrors the bus-event source.
- `outcome` is one of `applied` (≥1 seed inserted), `no_changes` (zero seeds — empty-template path), `skipped_cost_cap` (G3), `error_llm`, or `error_parse`.

Example bootstrap row:

```json
{
  "run_id": "pbboot-2026-05-08-12-34-56",
  "started_at": "2026-05-08T12:34:56Z",
  "finished_at": "2026-05-08T12:35:42Z",
  "kind": "bootstrap",
  "evidence_window": null,
  "evidence_summary": null,
  "relevance_increments": 0,
  "proposals_applied": [],
  "proposals_rejected": [],
  "engine_actions": [],
  "seeds_inserted": 34,
  "source": "boot",
  "llm_call": {
    "model": "claude-sonnet-4-6",
    "runtime": "claude",
    "input_tokens": 6204,
    "output_tokens": 2871,
    "cost_basis": "playbook:bootstrap",
    "cost_usd": 0.0394
  },
  "outcome": "applied"
}
```

### 3.4 `team_config` keys

- `playbook_bootstrap_done` — `"1"` once bootstrap has run successfully. Idempotency guard.
- `playbook_bootstrap_retries` — counter (string-encoded int) for failed bootstrap attempts. Incremented on each failed attempt; cleared on bootstrap success.
- `playbook_bootstrap_blocked` — `"1"` set when 3 consecutive bootstrap attempts have failed. Scheduler skips bootstrap while set. Cleared only by `POST /api/playbook/reset` (operator-explicit re-arm). Default unset.
- `playbook_reset_at` — ISO timestamp of the most recent reset call. Read by the bootstrap runner to populate `source` on the next bootstrap event (`"reset"` if set, `"boot"` if unset). Cleared after the next successful bootstrap.
- `playbook_last_run_at` — ISO timestamp of the most recent run (any kind).
- `playbook_disabled` — `"1"` to disable the engine entirely (skip scheduler ticks; lattice still readable; no writes). Default unset.

### 3.5 No new SQLite table

The lattice is JSON-on-disk (mirrors Compass). Cost rows go into the existing `turns` table. Bus events go through the existing `EventBus`. No schema migration needed.

---

## 4 · Bootstrap

### 4.1 Trigger

First boot after the engine ships, OR `POST /api/playbook/reset` clears `playbook_bootstrap_done`. Idempotent via `team_config['playbook_bootstrap_done']`.

### 4.2 Source

[server/templates/app_dev_playbook.md](../server/templates/app_dev_playbook.md) — the existing 484-line hand-curated prose playbook. Already vetted, already reflects accumulated session experience.

### 4.3 Mechanism

Single direct `claude_agent_sdk.query()` call with `model="latest_sonnet"`, `effort="medium"`, no MCP, no resume. The prompt:

> Below is a prose playbook on coordinating a multi-agent team. Extract every distinct, actionable orchestration pattern as a single conceptual statement.
>
> Brevity (load-bearing — these statements are injected into every agent's system prompt on every turn):
> - Hard cap: 160 characters. Anything longer is rejected.
> - One line, imperative form. "When X -> do Y" or "X needs Y." No enumerated sub-items, no parenthetical clauses listing what-goes-in.
> - The WEIGHT carries confidence; the text just needs to trigger recall. Detail / rationale belongs in the prose corpus, not the lattice statement.
> - Aim for ~120 chars typical, 160 only when the trigger genuinely needs context.
>
> Constraints:
> - Statement must be conceptual and runtime-agnostic — no specific Player slot ids, no specific tool names from the harness, no specific tech-stack references.
> - Each statement must be observable in action — Coach should be able to look at a day's events and tell whether the pattern fired and whether it paid off.
> - Each statement's negation must be meaningful (a confident NO would also be useful direction).
> - Skip anything that is a hardcoded harness rule (process plumbing, file paths, tool signatures) — the playbook is for learned strategy, not procedural plumbing.
>
> Return a JSON list of `{text: string, suggested_weight: float}` objects. Use 0.85 for patterns the prose explicitly calls out as load-bearing, 0.75 for default, 0.65 for patterns the prose hedges on.
>
> Skip the prose. Return only the JSON list.

**Cost gate:** before the LLM call, check `_today_spend()` vs `HARNESS_TEAM_DAILY_CAP` (same gate as §5.3). Over cap → log a runs.jsonl row with `kind="bootstrap"`, `outcome="skipped_cost_cap"`, no LLM call. **Cost-skip does NOT increment `playbook_bootstrap_retries`** — this is a deferred-not-failed outcome; the next scheduler tick retries when budget allows. Emit `playbook_run_skipped{run_id, reason: "cost_cap"}` for dashboard visibility.

**Emission points:**
- Before the LLM call (after cost gate passes): emit `playbook_bootstrap_started{source, retry_attempt}` where `source = "reset"` if `team_config['playbook_reset_at']` is set, else `"boot"`; `retry_attempt = playbook_bootstrap_retries + 1`.
- After successful persist: emit `playbook_bootstrap_completed{statement_count, source}`. Clear `playbook_reset_at` (so subsequent re-bootstrap from a fresh deploy reads as `"boot"` not `"reset"`).
- On failure: see §4.4.

The LLM returns ~30-40 statements. Engine validates each (length, character cap, no duplicates among the returned list), assigns ids `pb-001..pb-NNN`, persists to `lattice.json` with `created_by="bootstrap-playbook"`, sets `playbook_bootstrap_done = "1"`, clears `playbook_bootstrap_retries`, clears `playbook_reset_at`.

**Soft cap at bootstrap:** apply §5.7's three-branch cap to the LLM output (active count is 0 at bootstrap, so `pressure = seed_count`). If the LLM returned > 100 candidate seeds, drop from the **end of the LLM-returned list** (deterministic — the prose extraction is order-stable) until count ≤ 100. If > 110 (hard cap), drop down to 100 and additionally fire `playbook_soft_cap_exceeded{count: returned, dropped: returned - 100}` event. Bootstrap does NOT fail on over-cap — partial seeding is preferable to no seeding. The `seeds_inserted` field in the runs.jsonl row reflects the post-cap count.

No bedrock / immutable seeding step (§1.4).

### 4.4 Failure

The bootstrap LLM response is parsed tolerantly: extract the first balanced JSON-array literal from the response (handles the common case where Sonnet wraps the JSON in prose). If extraction yields no parsable array, OR the LLM call itself raises, OR validation rejects every returned statement: increment `team_config['playbook_bootstrap_retries']`, emit `playbook_bootstrap_failed{error, retries: <new_count>, blocked: false}` (no `human_attention` — the retry path is opaque to the operator until the 3rd attempt), skip; next scheduler tick retries.

After 3 failed attempts: emit `playbook_bootstrap_failed{error, retries: 3, blocked: true}` + `human_attention`, set `team_config['playbook_bootstrap_blocked'] = "1"`. The scheduler will skip bootstrap on every subsequent tick while the flag is set — operator must call `POST /api/playbook/reset` (which clears `playbook_bootstrap_blocked` and `playbook_bootstrap_retries`) to re-arm. The lattice stays empty until the operator intervenes. This breaks the loop hazard where clearing the retry counter alone would cause the next tick to retry-fail-escalate-clear-retry indefinitely.

If the prose template file is missing on disk, bootstrap completes with an **empty lattice** (no LLM call). No `## Orchestration playbook` section appears in agents' system prompts (render returns empty string per §6.2). The first daily reflection run that triggers will start populating the lattice from observed evidence. Slower cold-start than the prose-seeded path but functionally identical after a few days.

### 4.5 Operational bootstrap (first deploy)

Distinct from §4.1-§4.4 (which describe the engine's first-run *content seeding*), this section lists the operational steps the implementation PR must include so the engine works on first boot.

1. **Directory creation.** `paths.py` exposes `playbook_dir()` → `/data/playbook/` and `playbook_kdrive_dir()` → `TOT/playbook/`. Both call `mkdir(parents=True, exist_ok=True)` on access — no eager mkdir in lifespan. Same lazy-create pattern as Compass paths.

2. **Initial file state.** `store.py` read functions tolerate a missing file by returning the empty schema (`{"schema_version": 1, "statements": []}` for lattice/archived; empty list for runs.jsonl). First write creates the file atomically (tempfile + os.replace). No explicit initialization step in lifespan.

3. **MCP tool registration.** Add `coord_propose_playbook_changes` (§7.1) to the `_tools` map and `ALLOWED_COORD_TOOLS` set in [server/tools.py](../server/tools.py). Coach-only enforcement via the existing `_require_coach` helper.

4. **Shared Codex fallback module.** Per §11.3, the implementation PR moves [server/compass/codex_llm.py](../server/compass/codex_llm.py) to `server/shared/codex_llm.py` and updates Compass's import in the same PR. Order matters — the move + Compass import update + new Playbook import must land atomically or Compass breaks at boot.

5. **Lifecycle policy pointer.** Extend [server/agents.py:_build_coach_coordination_block](../server/agents.py)'s lifecycle-policy block (kanban-specs-v2 §14.1) with one line at the end:

   > Read the orchestration playbook (loaded after CLAUDE.md, see `## Orchestration playbook`). High-confidence statements are established discipline; deviate only with explicit reason. You can propose updates mid-turn via `coord_propose_playbook_changes`.

   This is a procedural pointer (where to look, what tool to use), not a learned pattern — it belongs in the lifecycle policy, not in the playbook itself. Without it, Coach may treat the new system-prompt section as descriptive documentation rather than directive guidance.

6. **Canonical project CLAUDE.md template enrichment.** Extend [server/templates/app_dev_claude_md.md](../server/templates/app_dev_claude_md.md) with a short section (under a new heading, e.g. `### Team-wide orchestration playbook`):

   > A harness-wide orchestration playbook is loaded into every agent's system prompt under `## Orchestration playbook`. It captures the team's evolving discipline as weighted statements (e.g. *"audit every code change except trivially mechanical edits"*) — each weight is the engine's current confidence that the pattern is the right play for this team. Treat high-weight statements as established discipline; deviate only with explicit reason. Coach can propose updates mid-turn via `coord_propose_playbook_changes`, and a daily reflection run evolves the lattice from observed events. Players follow the playbook as guidance and cannot influence it. The playbook is harness-wide — every project's Coach reads the same lattice, so improvements compound across projects.

   The Coach-driven reconciliation flow at [server/project_claude_md.py:update_claude_md_via_coach](../server/project_claude_md.py) propagates this template change to every existing project's CLAUDE.md on next activation (and once at boot for the active project) — no per-project manual edit needed. Per the harness convention (CLAUDE.md "Keep the canonical project CLAUDE.md template current"), updating the template is mandatory when shipping harness functionality projects need to know about.

7. **Player role-prompt awareness.** Players' role prompts in [server/agents.py:_system_prompt_for](../server/agents.py) don't need a dedicated playbook section — the rendered playbook header (§6.2) carries the Players-follow-but-don't-influence framing inline.

8. **Migration on existing harness deployments.** No special handling required. First boot post-implementation adds the new `## Orchestration playbook` section to every agent's next system prompt (one cache-miss per agent on next turn). No session reset, no agent restart, no DB migration. Existing Compass installations are untouched. Boot order: shared/codex_llm.py refactor → playbook engine module load → scheduler task starts → first scheduler tick triggers bootstrap.

   **Cold-start window.** The scheduler ticks every 5 min by default (`HARNESS_PLAYBOOK_SCHEDULER_TICK_SECONDS`). On a fresh deploy, the first bootstrap fires up to 5 min after process start — during that window, agent system prompts have no `## Orchestration playbook` section. To eliminate the wait, the operator can hit `POST /api/playbook/bootstrap` (G7) immediately after deploy.

---

## 5 · Reflection runner — daily post-mortem

### 5.1 Cadence

Single daily reflection run, fixed UTC time (default 04:00 — chosen to land before the 09:00 Compass daily so they don't compete for a single Anthropic plan-block window). Configurable via `HARNESS_PLAYBOOK_RUN_HOUR_UTC`.

Manual triggers via `POST /api/playbook/run` are always allowed regardless of time-of-day; the runner enforces the activity gate (§5.2) and the cost cap (§5.3) unless the request body explicitly bypasses.

### 5.2 Activity gate

Skip the run when there's nothing to reflect on. Gate:

```
COUNT(tasks where archived_at in last 24h) +
COUNT(events with type IN [audit_report_submitted, task_stall_*, human_attention, compass_audit] in last 24h)
≥ HARNESS_PLAYBOOK_MIN_ACTIVITY (default 3)
```

Below the threshold → emit `playbook_run_skipped{reason="no_activity", count}` + log a runs.jsonl row with `outcome="skipped_no_activity"`. Manual runs may bypass with `force_through_no_activity: true` in the request body.

### 5.3 Cost gate

Pre-flight check against `_today_spend()` vs `HARNESS_TEAM_DAILY_CAP` (read live, mirrors Compass). Over cap → `outcome="skipped_cost_cap"` + log row + skip. No `human_attention` (it's a routine cost-discipline outcome, not a fault).

### 5.4 Evidence bundle

The runner composes a structured digest of the last 24h, NOT raw events. Sections (each capped to keep prompt size bounded):

- **Archived tasks (last 24h)** — up to 15. For each: `id, title, trajectory_shape, executor, audit_chain_summary, outcome_bucket ∈ {clean, friction, failed, cancelled}, cost_usd_total`. Field formats:
  - `trajectory_shape` — actual stages walked, joined with `→`. Example: `"plan→execute→audit_syntax→ship"`.
  - `audit_chain_summary` — round-by-round verdicts. Example: `"audit_syntax round 1 FAIL → round 2 PASS; audit_semantics round 1 PASS"`. Empty string when the task had no audit stages.
  - `cost_usd_total` — sum of `turns.cost_usd` for rows where `turns.task_id = <task.id>`.

  Outcome bucket from kanban-specs-v2 §22.1 + §11.1 instrumentation:
  - `clean` = no audit FAIL rounds, no rung-2+ stalls, no human_attention, no deviations_log entries, cost ≤ 1.5× median for trajectory shape.
  - `friction` = ≤ 1 audit FAIL OR rung-1 stall OR deviation noticed at audit time.
  - `failed` = ≥ 2 audit FAILs, OR rung-2+ stall, OR human_attention fired, OR deviation noticed by human.
  - `cancelled` = `tasks.cancelled_at IS NOT NULL`.

  **Median window:** last 30 days, computed per-trajectory-shape. If a trajectory shape has < 5 samples in the window, fall back to the lattice-wide overall median across all archived tasks in the same window. Set `median_cost_fallback_fired: true` in `evidence_summary` so Coach knows the cost comparison is approximate.

- **Cost outliers** — tasks where `cost_usd_total > 2× median` for the same trajectory shape (or fallback median per above). Up to 5.
- **Stall events** — rung-2+ events from the last 24h. Up to 5.
- **Compass verdicts** — `compass_audit` rows from the last 24h: `aligned` count, `confident_drift` list, `uncertain_drift` list. Up to 5 of each non-aligned.
- **Deviations log** — last 24h `deviations_log` entries. Up to 10.
- **Repeat audit-fail patterns** — Player slots with ≥ 2 audit FAILs across distinct tasks in the last 24h. Up to 5.
- **Human attention events** — last 24h. Up to 5.

Total bundle target: ≤ 6 KB rendered. Hard cap 10 KB; truncate sections in the order listed above (less-important sections cut first) if over.

### 5.5 Reflection prompt

```
You are reviewing yesterday's team activity to update an orchestration playbook.

The playbook is a list of weighted statements about how to coordinate a multi-agent team. Each statement has a weight in [0, 1]:
- > 0.85: validated YES (established discipline)
- 0.5: genuine uncertainty
- < 0.15: validated NO (anti-pattern)

Below is the current playbook (active statements only) and the last 24h of team activity. Your job is to (a) note which statements the day's events touched on, (b) propose changes that move weights closer to the truth based on what actually happened.

# Current playbook
{rendered_lattice}

# Evidence bundle (last 24h)
{evidence_bundle}

# Your task

For each high-confidence statement (weight ≥ 0.85), look through the evidence for VIOLATIONS — events where the rule should have applied but didn't (e.g. a code commit shipped without an audit when the rule says "audit every code change"). Violations are evidence for downward adjustment.

For each anti-pattern statement (weight ≤ 0.15), look for evidence the anti-pattern fired anyway and produced bad outcomes (further evidence for keeping it low) OR fired and produced GOOD outcomes (counter-evidence that may justify upward adjustment).

Return a JSON object with four lists:

{
  "relevant_ids": ["pb-XXX", "pb-YYY", ...],
  "adjustments": [
    {"id": "pb-XXX", "delta": 0.10, "reason": "validated by 3 clean outcomes in t-abc, t-def, t-ghi"}
  ],
  "creations": [
    {"text": "<one line, imperative, <=160 chars, conceptual, runtime-agnostic, observable>", "weight": 0.6, "reason": "pattern observed in 3 archived tasks t-..., t-..., t-..."}
  ],
  "merges": [
    {"keep_id": "pb-XXX", "drop_id": "pb-YYY", "reason": "say the same thing"}
  ]
}

Rules:
- relevant_ids: every statement the day's events touched on — whether or not weight changed. Used to track which patterns are actually firing.
- Each adjustment delta ≤ ±0.25 (so a single noisy day cannot flip a stable consensus).
- Justification must reference specific task ids / event types from the evidence bundle.
- Creations should be supported by ≥ 3 distinct observations (instruction, not enforced — be honest).
- **Creation text is hard-capped at 160 chars; longer creations are rejected.** One line, imperative ("When X -> do Y" or "X needs Y"), no enumerated sub-items, no parenthetical lists. The WEIGHT carries confidence; the text just triggers recall. Aim for ~120 chars typical.
- Skip statements that are runtime-specific, project-specific, procedural-plumbing, or unobservable.
- Empty lists are valid — return all four as `[]` if no real signal.

Return ONLY the JSON object. No prose.
```

### 5.6 Validation + apply

The runner parses the JSON tolerantly (same first-balanced-JSON-object extraction as bootstrap §4.4). On parse failure → log `outcome="error_parse"` + skip; no retry within the same day (next scheduler tick re-checks the daily-run gate). When parsing succeeds but every proposal is rejected by validation (and `relevant_ids` is empty), log `outcome="no_changes"` — that's a clean run that produced no useful signal, not an error.

Op apply order is **fixed**:

1. **Merges first.** Each merge: validate both ids exist in active lattice, neither immutable. `keep_id` retains its weight (max of the two). `drop_id` moves to `archived.json` with `archive_reason="merged"`, `merged_into=keep_id`. `keep_id`'s `weight_history` records the merge; `keep_id.applied_count += dropped.applied_count`; `keep_id.last_validated_at = max(keep_id.last_validated_at, dropped.last_validated_at)` (NULL-safe — NULL participates as "older than any timestamp").
2. **Creations next** (against post-merge state). Each create: validate `text` length ≤ `STATEMENT_MAX_CHARS` (default 160; rejection reason includes the cap, the form rule "one line, imperative, no enumerated sub-items", and a pointer to the prose corpus for rationale), weight ∈ [0, 1], no near-duplicate of existing statement. **Near-duplicate algorithm:** Jaccard similarity over lowercased whitespace-tokenized word sets, after stripping ASCII punctuation and a small English stopword list (`a, an, the, and, or, of, to, for, in, on, at, with, is, are, be`). Threshold: Jaccard ≥ 0.7. Embedding-based dedup deferred to v2. Mint new id `pb-NNN`, persist with `created_by="reflection"`. Soft cap (§5.7) checked first.
3. **Adjustments last** (against post-merge, post-creation state). Each adjust: validate `id` exists, not immutable, |delta| ≤ 0.25, target weight stays in [0, 1] after clamp. Apply: update `weight`, append to `weight_history`, update `last_validated_at`. Reject silently otherwise (logged in `proposals_rejected`).

**Cross-op conflict:** any op targeting an id that an earlier op archived (via merge in step 1) is rejected with reason `"id_archived_in_same_run"`. Adjusts and creations referencing freshly-merged ids fall through to this rule.

**`relevant_ids` increment:** independent of op apply. After all ops process, walk `relevant_ids` and increment `applied_count += 1` for each id (one increment per statement per run, regardless of how many evidence items mention it). Each entry must be a non-empty string matching `^pb-\d+$`; entries that fail this regex (non-string, malformed, empty, nested object, etc.) are skipped silently. Valid-shape ids that don't exist in the active lattice OR that were just archived in step 1 are also skipped silently. Duplicate ids in the list are deduplicated before increment (so Coach listing the same id twice doesn't double-increment).

### 5.7 Soft / hard cap enforcement

Before applying creations (step 2), count active statements (post-merge). Apply pending engine-driven settle/stale (§5.8) actions FIRST so any creation-budget freed by archives is available. Then branch on `pressure = active + new_creations`:

- **Branch A — `pressure ≤ 100` (soft cap):** apply all creations.
- **Branch B — `100 < pressure ≤ 110`:** drop creations from the **end of the input list** (deterministic — Coach can prioritize by ordering its `creations` array) until `active + survivors == 100`. Survivors apply. Log dropped creations in `proposals_rejected` with reason `"soft_cap_pressure"`.
- **Branch C — `pressure > 110` (hard cap):** drop **ALL** creations from the run atomically. Apply only adjusts and merges. Log every creation in `proposals_rejected` with reason `"hard_cap_pressure"`. Fire `playbook_soft_cap_exceeded{count: pressure, dropped: len(creations)}` event AND `human_attention`. Hitting the hard cap means soft-cap discipline is failing — needs operator review of the lattice.

The same cap logic applies to the `coord_propose_playbook_changes` MCP tool path (§7.1).

### 5.8 Engine-driven actions (settle / stale)

After Coach's proposals are applied, the runner sweeps for engine-driven archives. Three semantically-tight predicates. **All three predicates also require `immutable = false`** — immutable statements are never archived by the engine.

- **Settle:** an active statement is settle-eligible iff:
  - `immutable = false`, AND
  - current `weight ≥ 0.95`, AND
  - at least one `weight_history` entry has `ts ≤ now - 7 days`, AND
  - no `weight_history` entry within the last 7 days recorded a `to` value below 0.95.
  
  Eligible statements move to `archived.json` with `archive_reason="settled"`.

- **Stale-low:** an active statement is stale-low-eligible iff:
  - `immutable = false`, AND
  - current `weight ≤ 0.15`, AND
  - at least one `weight_history` entry has `ts ≤ now - 7 days`, AND
  - no `weight_history` entry within the last 7 days recorded a `to` value above 0.15.
  
  Eligible statements move to `archived.json` with `archive_reason="stale_low"`.

- **Stale-unused:** an active statement is stale-unused-eligible iff:
  - `immutable = false`, AND
  - `applied_count == 0`, AND
  - `created_at ≤ now - 30 days`.
  
  Eligible statements move to `archived.json` with `archive_reason="stale_unused"`.

The "≥ 7 days old history entry" requirement prevents brand-new high-confidence statements from immediately settling (e.g. Coach creates a new pattern at 0.6 then bumps to 0.95 the next day — that's not 7 days of stable confidence).

### 5.9 Run finalization

Atomic writes (tempfile + os.replace) for `lattice.json` + `archived.json` on local disk first. Append run row to `runs.jsonl`. Update `team_config['playbook_last_run_at']`. Emit `playbook_run_completed{kind, outcome, applied_count, evidence_summary, relevance_increments}` event for the dashboard.

**kDrive mirror failure handling.** After local disk writes succeed, attempt the synchronous kDrive mirror (`TOT/playbook/{lattice,archived}.json` + runs.jsonl append). If the kDrive call fails (token expired, network error, 5xx response):
- Log a warning.
- Emit a `playbook_kdrive_mirror_failed{error: str, files: list[str]}` event (dashboard surfaces a small banner).
- **Do NOT roll back the local disk write.** Local is the source of truth; kDrive is a durability mirror. A persistent kDrive outage must not break the engine.
- Next successful write (any subsequent run, override, or proposal apply) re-syncs the affected files in full — the mirror is idempotent because kDrive writes are full-file replacements, not incremental.

This matches the precedent in Compass and the existing kDrive sync loop ([CLAUDE.md "M-3" entries](../CLAUDE.md)).

---

## 6 · Read path — system-prompt injection

The playbook is read into every agent's system prompt via [server/context.py:build_system_prompt_suffix](../server/context.py) — the same universal read path as CLAUDE.md. All agents see the same content. Only Coach can propose changes via the MCP tool (§7); Players read it as informational reference.

### 6.1 Where in the prompt

Extends `build_system_prompt_suffix()` to append a third section after global + project CLAUDE.md:

```
[identity]                          ← prepended in agents.py
[coordination block (Coach only)]   ← prepended in agents.py
[role prompt]                       ← role baseline in agents.py
[global CLAUDE.md]                  ← from context.py
[project CLAUDE.md]                 ← from context.py
[orchestration playbook]            ← NEW from context.py (after project CLAUDE.md)
[brief / coach_supplement / ...]    ← appended in agents.py
```

Natural progression for Coach: lifecycle policy (in coord block — bedrock mechanics) → CLAUDE.md (global + project rules) → playbook (learned strategy). Bedrock → curated → evolved.

### 6.2 Render format

```markdown
## Orchestration playbook

Learned patterns for orchestrating this team. Each entry has a confidence weight in [0, 1] — high = validated discipline, low = validated anti-pattern, ~0.5 = uncertain. Apply high-confidence patterns by default; deviate with explicit reason. Coach updates this lattice mid-turn via `coord_propose_playbook_changes` and via a nightly reflection run; Players follow it as guidance and cannot influence it.

**Validated (weight ≥ 0.85):**
- [0.92] Audit every code-touching task except trivially mechanical edits.
- [0.88] Two-axis audit (formal + semantic) catches different bug classes; use both for non-trivial changes.

**Working hypotheses (0.65 ≤ weight < 0.85):**
- [0.78] For coding work, medium effort with Sonnet handles ~80% of tasks adequately — bump only on repeated audit fail.

**Uncertain (0.35 ≤ weight < 0.65):**
- [0.55] Plan-mode adds value when the task spans more than 2 files or touches contracts.

**Anti-patterns (weight < 0.35):**
- [0.18] Bumping model tier on first audit fail — usually a process problem, not a model problem.

— End playbook (N statements active, last reflected: 2026-05-07 04:01 UTC)
```

Rendered size budget: ≤ 8 KB. With 100 statements at ~80 chars each plus headers ≈ 8.5 KB worst-case. If over budget, drop the "Uncertain" bucket from the rendered prompt (Coach can still see those in the dashboard); the rendered playbook is for actionable patterns.

When `lattice.json` has zero active statements (cold-start before bootstrap, or post-reset), render produces empty string — concatenates to nothing in the system prompt. No special-case needed.

### 6.3 Cost behavior

Read every turn for every agent → cache-hits within the 5-min Anthropic prompt-cache window. Per-agent sessions each have their own cache; 8 KB × 11 agents ≈ 88 KB of cache-warm content distributed across agent sessions, but each individual turn pays only delta cost on cache hit. Lattice updates between turns invalidate cache for that agent's next turn → one full read, then back to cache hits. Net cost: trivial. Daily-run lattice updates invalidate the cache for every agent's next turn — amortized in practice because agents don't all turn simultaneously.

### 6.4 Why Players see it

Players reading the same playbook content benefits team coherence: a Player who sees *"audit every code change"* in their system prompt internalizes the discipline directly without needing Coach to remind them every turn. Players cannot influence the lattice (the MCP tool is Coach-only), so visibility is read-only by construction. If a Player's behavior contradicts a high-weight playbook entry, that's signal for Coach's daily reflection — same as any other evidence.

---

## 7 · MCP tools

One Coach-only tool. Players never see the propose surface (the lattice itself is visible to all via system-prompt injection per §6).

### 7.1 `coord_propose_playbook_changes(operations: list[dict]) → str`

**Purpose:** Allow Coach to propose playbook changes from inside any normal turn (not just the daily reflection). Use case: Coach notices something obviously load-bearing in real time and wants to propose a creation or adjustment without waiting for the daily run.

**Coach-only:** rejects Players with the standard "Coach-only" error. Codex Coach reaches the tool via the same proxy path as other coord_* tools.

**Parameters:**

```python
operations: list[
  {"op": "adjust", "id": "pb-XXX", "delta": float, "reason": str}
  | {"op": "create", "text": str, "weight": float, "reason": str}
  | {"op": "merge", "keep_id": "pb-XXX", "drop_id": "pb-YYY", "reason": str}
]
```

Cap: ≤ 5 operations per call (so a single hallucinating Coach turn can't flood the lattice).

**Validation:** same rules as §5.6 (apply order: merges → creates → adjusts; cross-op conflict rejection; soft/hard caps). No `relevant_ids` field — this is mid-turn intervention, not reflection.

**Cost gate:** none. The MCP call doesn't make an LLM call — it just mutates a JSON file. The Coach turn already pays the cost cap to run.

**Lock acquisition:** the MCP path attempts `_run_lock.acquire(timeout=0)` (non-blocking) before mutating. If the lock is held (daily reflection / bootstrap / reset / another MCP call in flight), return the contention message (§7.1 return shape, see N9) without mutating. Coach is expected to retry next turn — no internal retry loop. This prevents Coach's mid-turn proposal from racing with a daily reflection that's about to overwrite the lattice.

**Apply:** atomic; same engine path as the daily runner. Emits `playbook_changes_applied{operations_count, source="coach_mid_turn"}`.

**Returns:** human-readable text block.

Happy path:

```
Applied 2 of 3 proposed changes:
  - adjust pb-014: 0.65 → 0.75 (+0.10) — "validated by clean outcome in t-abc"
  - create pb-031: weight 0.60 — "Don't pair an executor with the same auditor twice in a row"
  - REJECTED adjust pb-008: delta 0.40 exceeds ±0.25 cap

Active statement count: 47 / 100 soft cap.
```

Lock-contention path (G8 — daily reflection / bootstrap / reset / another MCP call in flight):

```
playbook engine busy — another run is in flight. Retry on your next turn; no changes applied.
```

The leading literal `"playbook engine busy"` lets Coach pattern-match the contention case in its own reasoning without re-parsing the rest of the message. Coach is expected to surface the contention to itself (no proposal applied this turn) and re-evaluate whether the proposed changes are still warranted on the next turn — they may be obsolete after the in-flight run completes.

---

## 8 · HTTP API

All endpoints under `/api/playbook/*`, gated by `HARNESS_TOKEN`. CRUD for the dashboard + manual control.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/playbook/state` | Returns full lattice + recent runs (paginated). Used by the dashboard initial load. |
| POST | `/api/playbook/run` | Manually trigger a reflection run. Bypasses the daily schedule but enforces the activity gate + cost gate by default. Body: `{force_through_no_activity?: bool}` (default false). Returns 409 if `playbook_bootstrap_done` is unset (run bootstrap first). |
| POST | `/api/playbook/bootstrap` | Manually trigger bootstrap immediately, bypassing the 5-min scheduler tick wait. Useful after `/api/playbook/reset` when the operator doesn't want to wait. Returns 409 when `playbook_bootstrap_done` is already set (use reset first), when `playbook_bootstrap_blocked` is set (reset first to clear), or when `_run_lock` cannot be acquired within 5s. Cost gate + soft cap (§4.3) still apply. Body: `{}` (no parameters). |
| POST | `/api/playbook/proposals/{adjust\|create\|merge}/{id}` | Apply an individual proposal that was logged but not auto-applied (e.g. soft-cap rejected, human wants to apply manually). Body for adjust: `{delta: float}`; create: `{text, weight}`; merge: `{keep_id, drop_id}`. |
| POST | `/api/playbook/statements/{id}/weight` | Direct human override. Body: `{weight: 0.0 \| 0.5 \| 1.0}`. Mirrors Compass's NO/½/YES override. Records `weight_history` entry with `reason="human_override"`. Rejects when `immutable=true`. |
| POST | `/api/playbook/statements/{id}/restore` | Move an archived statement back to active. Body: `{weight?: float}` (default = the value at archive time). |
| DELETE | `/api/playbook/statements/{id}` | Soft delete — moves to `archived.json` with `archive_reason="deleted"`. Rejects when `immutable=true`. |
| POST | `/api/playbook/reset` | Wipe everything: clears `lattice.json`, `archived.json`, `runs.jsonl`, unsets `playbook_bootstrap_done`. Next scheduler tick re-bootstraps from `app_dev_playbook.md`. Body: `{confirm: "yes"}` required. |
| GET | `/api/playbook/runs` | Paginated runs.jsonl. Query params: `since`, `limit` (default 30, max 100). |

All write endpoints carry the `audit_actor` dependency (mirrors kanban-specs-v2 §8) so destructive actions are recorded with `{source, ip, ua}` in the bus event payload.

---

## 9 · Bus events

| Event | Payload | Routing |
|---|---|---|
| `playbook_bootstrap_started` | `{source: "boot" \| "reset", retry_attempt: int}` | Dashboard — fired immediately before the LLM call (or before empty-lattice persist when prose template missing). `source="reset"` when `playbook_reset_at` is set (cleared on success); `"boot"` otherwise. `retry_attempt` is 1 for the first try, up to 3. |
| `playbook_bootstrap_completed` | `{statement_count: int, source: "boot" \| "reset"}` | Dashboard — fired after successful persist. Same `source` semantics. |
| `playbook_bootstrap_failed` | `{error: str, retries: int, blocked: bool}` | Dashboard + `human_attention` (only when `blocked=true`, i.e. 3rd failure) — fired after each failed attempt. `blocked=true` on the 3rd attempt to signal the operator-reset escalation. |
| `playbook_run_started` | `{run_id, kind}` | Dashboard |
| `playbook_run_completed` | `{run_id, outcome, applied_count, evidence_summary, relevance_increments, llm_cost_usd}` | Dashboard |
| `playbook_run_skipped` | `{run_id, reason}` | Dashboard |
| `playbook_changes_applied` | `{operations_count, source: "coach_mid_turn" \| "daily_reflection" \| "human_dashboard"}` | Dashboard |
| `playbook_statement_overridden` | `{id, from, to, actor}` | Dashboard |
| `playbook_settled` | `{id, final_weight}` | Dashboard |
| `playbook_staled` | `{id, final_weight, reason: "stale_low" \| "stale_unused"}` | Dashboard |
| `playbook_soft_cap_exceeded` | `{count, dropped}` | Dashboard + `human_attention` |
| `playbook_llm_call` | `{label, model, runtime, cost_usd, duration_ms, input_tokens, output_tokens, is_error, project_id}` | Dashboard live counter — fired by the LLM wrapper after each call (Claude or Codex). `label` is the cost-basis tag (e.g. `playbook:bootstrap`, `playbook:reflection`). The detailed per-run join lives in `runs.jsonl` keyed by `run_id`; this event is for at-a-glance UI feedback, so no `run_id` is plumbed through. |
| `playbook_reset` | `{actor}` | Dashboard |
| `playbook_kdrive_mirror_failed` | `{error: str, files: list[str]}` | Dashboard (small banner — non-fatal) |
| `playbook_manual_run` | `{actor, outcome}` | Dashboard — fired alongside `playbook_run_started`/`_completed` for the dashboard's "you triggered this" feedback. Distinct from the reflection lifecycle events. |
| `playbook_manual_bootstrap` | `{actor, outcome}` | Dashboard — same shape and rationale as `playbook_manual_run` but for the manual bootstrap trigger (G7). |
| `playbook_statement_restored` | `{id, actor}` | Dashboard — fired by `POST /api/playbook/statements/{id}/restore`. |
| `playbook_statement_deleted` | `{id, actor}` | Dashboard — fired by `DELETE /api/playbook/statements/{id}` (soft delete). |

---

## 10 · Scheduler

`playbook_scheduler_loop` background task in `lifespan` next to `compass_scheduler_loop`. Polls every `HARNESS_PLAYBOOK_SCHEDULER_TICK_SECONDS` (default 300 = 5 min). Each tick:

1. Skip if `playbook_disabled` flag set.
2. Skip if no active project (the harness as a whole is unconfigured).
3. If `playbook_bootstrap_done` unset:
   - Skip if `playbook_bootstrap_blocked` is set (operator must reset to re-arm).
   - Otherwise run bootstrap (§4) and return — no daily-run path on the same tick.
4. Otherwise, daily run if all of:
   - Current UTC time is past `HARNESS_PLAYBOOK_RUN_HOUR_UTC` (default 04).
   - `playbook_last_run_at` is from a different UTC date than today.
   - Activity gate (§5.2) passes.
   - Cost gate (§5.3) passes.

One run per scheduler iteration max (no parallel runs). A `_run_lock: asyncio.Lock` prevents concurrent runs from manual trigger + scheduler tick.

---

## 11 · Cost model + LLM config

Constants in `server/playbook/config.py`:

```python
LLM_MODEL_DEFAULT_ALIAS = "latest_sonnet"
LLM_EFFORT = "medium"
LLM_FALLBACK_MODEL_ALIAS = "latest_mini"
LLM_FALLBACK_EFFORT = "medium"
LLM_FALLBACK_ENABLED = True

BOOTSTRAP_WEIGHT = 0.75
COACH_CREATION_WEIGHT = 0.60
ADJUST_DELTA_CAP = 0.25
SOFT_STATEMENT_CAP = 100
HARD_STATEMENT_CAP = 110
STATEMENT_MAX_CHARS = 160     # per-statement char cap; env: HARNESS_PLAYBOOK_STATEMENT_MAX_CHARS
SETTLE_THRESHOLD = 0.95
STALE_THRESHOLD = 0.15
SETTLE_STABLE_DAYS = 7
STALE_STABLE_DAYS = 7
STALE_UNUSED_DAYS = 30
EVIDENCE_BUNDLE_MAX_BYTES = 10_000
EVIDENCE_MEDIAN_WINDOW_DAYS = 30
EVIDENCE_MEDIAN_MIN_SAMPLES = 5
RUN_HOUR_UTC_DEFAULT = 4
MIN_ACTIVITY_DEFAULT = 3
RUNS_RETENTION_DEFAULT = 90
BOOTSTRAP_MAX_RETRIES = 3
COACH_PROPOSAL_OPS_CAP = 5
```

### 11.1 Cost ledger

Each LLM call logs one row into `turns` under:
- `agent_id="playbook"`
- `runtime="claude" | "codex"` (whichever path actually executed)
- `cost_basis="playbook:bootstrap" | "playbook:reflection"`
- Token counts pulled defensively via the same helpers Compass uses.

ChatGPT-auth Codex fallback yields `cost_usd=0.0` (plan-included), same as Compass.

### 11.2 Cost ballpark

- Bootstrap: one Sonnet medium call against ~25 KB of prose → ~6k input tokens, ~3k output. At Sonnet 4.6 pricing ≈ $0.04. Once-only.
- Daily reflection: ~10 KB lattice + ~6 KB evidence + ~1 KB prompt scaffolding ≈ 4-5k input, ~600 output. ≈ $0.015 per run.
- Annual: 365 × $0.015 ≈ $5.50/year. Trivial.

### 11.3 Codex fallback

The fallback helper currently lives at [server/compass/codex_llm.py](../server/compass/codex_llm.py). **Pre-implementation refactor required:** lift it to `server/shared/codex_llm.py` (one-off ~30-line move + import update in Compass + new import in Playbook). Both subsystems then import from the shared module without cross-namespace dependency.

No per-run latch needed for the playbook (single LLM call per run, vs Compass's multi-stage pipeline).

Behavior matches Compass §5.5.2: `_call_claude` raise OR `is_error=True` triggers a `_call_codex` one-shot via the shared helper. If both runtimes fail, the runner logs `outcome="error_llm"` and skips.

---

## 12 · Reset

`POST /api/playbook/reset {confirm: "yes"}` is a **blocking** call: it acquires `_run_lock` (the same lock daily reflection / bootstrap / MCP tool path acquire — see §15) before any wipe. If a run is in flight, reset waits until it completes (cap: 60s; on timeout return 503 with retry-after). This prevents the race where a mid-bootstrap write lands after the reset has cleared files, silently undoing the operator's reset.

Once the lock is held, reset wipes:

- `lattice.json` (kept on disk as empty object, not deleted — keeps file watchers happy)
- `archived.json` (same)
- `runs.jsonl` (same)
- `team_config['playbook_bootstrap_done']` unset
- `team_config['playbook_bootstrap_retries']` unset
- `team_config['playbook_bootstrap_blocked']` unset
- `team_config['playbook_reset_at']` set to now (so the next bootstrap fires `source="reset"`)

Emits `playbook_reset{actor}`. Next scheduler tick (≤ 5 min) re-bootstraps from `app_dev_playbook.md`.

The dashboard surfaces a "Reset playbook" button under a `<details>` confirm-twice gate (mirror Compass's reset flow).

---

## 13 · Dashboard

A new `__playbook` slot in the LeftRail (CSS-drawn icon — distinct from Compass's existing button). Single column, harness-styled (same paper-free v1 visual language as Compass).

### 13.1 Sections

1. **Header bar** — capacity (`47 / 100`), last run timestamp (rendered in UTC; the dashboard's existing timezone toggle from the Display section applies if set), "Run now" button (calls `POST /api/playbook/run` with a confirm-modal that surfaces the activity-gate-bypass option).
2. **Active statements** — grouped by weight bucket (Validated / Working / Uncertain / Anti-pattern). Within each bucket, sorted by `weight × log(1 + applied_count)` descending (so frequently-observed validated rules surface above rarely-observed ones). Each row:
   - Weight bar (visual: 0 left, 1 right, current weight as fill).
   - NO / ½ / YES override buttons (call `POST /api/playbook/statements/{id}/weight` with 0.0 / 0.5 / 1.0). Routed through a confirmation modal mirroring Compass's `OverrideModal`.
   - Statement text.
   - Last-validated timestamp + applied_count badge.
   - Click-to-expand: shows full `weight_history`.
3. **Archived (settled / stale_low / stale_unused / merged / superseded / deleted)** in a `<details>` block, with restore button on each row.
4. **Recent runs** — list of run rows (timestamp, outcome, applied_count, relevance_increments, cost). Click to expand for full evidence summary + applied/rejected proposals.
5. **Footer** — "Reset playbook" + "Disable playbook" toggles, behind expanding `<details>`.

### 13.2 What it doesn't have (vs Compass dashboard)

- No truth references column (no truth corpus).
- No questions / Q&A overlay (no Q&A flow).
- No briefing markdown render.
- No proposal cards for settle/stale (engine applies them automatically; humans see them in archived).
- No region filter pills.
- No "bedrock" section (no immutables in v1).

### 13.3 Live updates

Listens to `playbook_*` bus events on the existing `/ws` channel and re-fetches `/api/playbook/state` on `playbook_run_completed`, `playbook_changes_applied`, `playbook_statement_overridden`, `playbook_settled`, `playbook_staled`, `playbook_reset`. Cheap full-state refresh (lattice JSON ≈ 30 KB).

---

## 14 · Cross-references

- [TOT-specs.md](TOT-specs.md) — umbrella spec.
- [compass-specs.md](compass-specs.md) — Compass; the playbook borrows its lattice + proposal mechanism but operates harness-wide on AI orchestration patterns rather than per-project on human intent.
- [kanban-specs-v2.md](kanban-specs-v2.md) — the playbook reads §22.1 deviations_log + §11.1 player health counters + §9.2 project_events as evidence; the playbook injection follows project CLAUDE.md in `build_system_prompt_suffix`, downstream of the Coach coordination block (which still owns the §14 lifecycle policy). **Binding dependency:** the runner's evidence-bundle composition is hardwired to these three v2 surfaces. A schema change in any of them (column rename, table restructure, event type rename) requires a coordinated playbook-runner update.
- [recurrence-specs.md](recurrence-specs.md) — the playbook scheduler is a sibling background task, not a recurrence (no Coach turns spawned by the scheduler itself; the reflection is a direct `claude_agent_sdk.query()` call under `agent_id="playbook"`).
- [server/templates/app_dev_playbook.md](../server/templates/app_dev_playbook.md) — the bootstrap corpus. Kept on disk after bootstrap as historical reference + re-bootstrap source.
- [server/templates/app_dev_claude_md.md](../server/templates/app_dev_claude_md.md) — canonical project CLAUDE.md template. Per §4.5 step 6, gains a `### Team-wide orchestration playbook` section so every project's Coach has the awareness baked in. Propagated to existing projects via [server/project_claude_md.py:update_claude_md_via_coach](../server/project_claude_md.py).

---

## 15 · Code layout

```
server/playbook/
  __init__.py
  config.py        # constants (§11)
  paths.py         # disk + kDrive path resolution
  store.py         # lattice.json / archived.json / runs.jsonl I/O (atomic + kDrive mirror)
  mutate.py        # adjust / create / merge / settle / stale / restore / override / delete primitives
  llm.py           # _call_claude wrapper + Codex fallback (imports server.shared.codex_llm)
  prompts.py       # bootstrap extraction prompt + reflection prompt
  bootstrap.py     # one-off prose extraction logic
  runner.py        # daily reflection orchestration (composes evidence bundle, invokes LLM, applies proposals)
  scheduler.py     # background lifespan task
  api.py           # /api/playbook/* HTTP routes
  render.py        # render_playbook_block(active_statements) → markdown for system prompt

server/tests/
  test_playbook_store.py
  test_playbook_mutate.py
  test_playbook_runner.py
  test_playbook_scheduler.py
  test_playbook_api.py
  test_playbook_bootstrap.py
  test_playbook_render.py
  test_playbook_mcp_tool.py

server/shared/
  codex_llm.py     # MOVED from server/compass/codex_llm.py — used by both Compass and Playbook

server/static/
  playbook.js      # dashboard component (mirrors server/static/compass.js shape)
  playbook.css     # dashboard styling (mirrors server/static/compass.css)
  # plus minimal touches to:
  #   app.js       — register the __playbook LeftRail slot + icon + WS event handlers
  #                  (mirrors how __compass is registered)
  #   style.css    — add the .leftrail-icon-playbook CSS-drawn icon (no emoji per
  #                  CLAUDE.md "no emoji in the UI" invariant)
```

Module guidelines:

- `store.py`, `mutate.py` are pure — no LLM calls, no event bus.
- `llm.py` owns the LLM call surface; mockable in tests.
- `runner.py` is the orchestrator and the only place that knows about evidence-bundle composition. **`runner.py` owns the canonical `_run_lock: asyncio.Lock`** (module-level singleton). All other modules that need the lock import it from `runner._run_lock`. Paths that acquire:
  - `runner.run_daily_reflection()` — blocking acquire.
  - `bootstrap.run_bootstrap()` — blocking acquire (called from `scheduler.py`).
  - `api.reset_playbook()` — blocking acquire with 60s timeout (§12, G2).
  - `api.run_playbook()` (manual reflection) — blocking acquire.
  - `api.bootstrap_playbook()` (manual bootstrap, G7) — blocking acquire with 5s timeout.
  - `mutate.apply_coach_proposals()` (called from the MCP tool path in `server/tools.py`) — non-blocking acquire (`timeout=0`); contention returns the §7.1 contention string (G8 + N9).
- `render.py` is called from [server/context.py:build_system_prompt_suffix](../server/context.py). Function signature: `def render_playbook_block() -> str` — synchronous, no parameters, reads the lattice from disk via `store.load_lattice()`. Returns the **full self-contained markdown block** matching §6.2 verbatim (including the leading `## Orchestration playbook` heading and the meta description). Returns the empty string when lattice has zero active statements, when the file is missing, or when `playbook_disabled` is set — caller treats empty string as "skip the section entirely" (don't append a heading with no body). Sync I/O is acceptable from the async caller — same pattern as `_read_text_safe()` already uses for CLAUDE.md ([server/context.py:44](../server/context.py#L44)). Caller in `build_system_prompt_suffix` joins non-empty CLAUDE.md / playbook sections with the existing `"\n\n---\n\n"` separator.
- `bootstrap.py` is invoked from `scheduler.py` AND from `api.py` (manual trigger); isolated so reset → re-bootstrap is a one-line call.

**Known duplication (deliberate for v1).** `server/playbook/paths.py` will mirror the shape of `server/compass/paths.py` (disk path + kDrive path resolution). `server/playbook/llm.py`'s `_call_claude` wrapper mirrors `server/compass/llm.py`'s. Unifying both into `server/shared/{paths,llm}.py` is a bigger refactor and premature for v1 — leave duplicated. The Codex fallback IS unified (§4.5 step 4) because that helper is genuinely identical and was already going to land in `shared/`. Re-evaluate at v2 once a third subsystem appears with the same shape.

---

## 16 · Out of scope (this spec)

- **Per-project playbooks.** Single harness-wide lattice. Project-specific patterns belong in the project's CLAUDE.md.
- **Region tags / region auto-merge.** Flat list. Revisit if the lattice grows past ~200 statements consistently.
- **Q&A flow.** The playbook reflects on observed evidence, doesn't ask the human questions. If a pattern is too uncertain, the daily reflection just doesn't move its weight.
- **Briefing generation.** No daily summary file. The dashboard + the lattice itself ARE the surface.
- **Per-artifact audit watcher.** No `commit_pushed → playbook audit` fan-out. Daily reflection only.
- **Multi-statement embedding similarity for duplicate detection.** Token-overlap heuristic for v1 (cosine ≥ 0.85). Embedding-based dedup is a v2 feature when the lattice grows enough that token overlap misses real duplicates.
- **Player ability to influence the playbook.** Players read but cannot propose changes (Coach-only MCP tool).
- **Cross-team / cross-harness federation.** Single harness instance. Sharing playbooks across deployments is a v2 idea.
- **Reward function on top of the lattice.** Weight changes are the gradient analog; no scalar reward, no RL, no fine-tuning. The lattice itself IS the learning artifact.
- **Immutable bedrock seeds in v1.** Schema supports the flag (latent capability); zero seeded.

---

## 17 · Validation criteria

Measured against a representative ~30-day window after the engine ships. Pre-deploy baseline measurement is required for criterion (a).

(a) **Coach behavior reflects high-confidence statements (vs baseline).** Pre-deploy: sample 50 Coach `coord_approve_stage` notes from a 7-day window. Manually classify which playbook patterns (taken from the prose template) Coach's decisions align with. Compute baseline alignment %. Post-deploy 30 days: same method against the actual lattice (high-confidence statements only). Improvement vs baseline = signal; flat = playbook isn't moving Coach. **Resource ask:** the manual classification takes ~30 minutes of operator time pre-deploy and again at the 30-day mark — schedule the work or this criterion goes unmeasured.

(b) **Weights actually move.** Across the 30-day window, ≥ 50% of active statements should have at least one `weight_history` entry from a reflection run (i.e. the daily runner is finding signal, not just no-op'ing). If < 30% move, the activity gate or evidence bundle is not surfacing enough for Coach to reason about — sharpen the bundle composition (§5.4).

(c) **Soft cap holds.** Active statement count stays ≤ 100 across the window without manual deletion. If the count routinely pushes the cap, raise the cap or tighten the duplicate-detection heuristic.

(d) **Cost stays trivial.** Total playbook cost (bootstrap + 30 daily runs) ≤ $1 over the window. If costs run higher, the evidence bundle is bloating; tighten the per-section caps in §5.4.

If (a) shows no improvement vs baseline, Coach is reading the playbook but not acting on it — investigate whether the rendered format (§6.2) is failing to convey weights clearly, or whether the prompt instruction to follow validated directions needs strengthening.

---

## 18 · What needs verification (post-implementation)

End-to-end on a deployed Zeabur instance after the implementation PR ships:

1. **Bootstrap from prose.** Fresh deploy with empty `/data/playbook/`. First scheduler tick boots, runs Sonnet extraction over `app_dev_playbook.md`, populates lattice with ~30-40 statements. Idempotency: subsequent boots don't re-bootstrap.
2. **Bootstrap with missing prose template.** Rename the template; first scheduler tick produces empty lattice and sets `playbook_bootstrap_done`. No retry loop.
3. **Bootstrap LLM failure path.** Force the LLM to raise; verify retry counter increments to 3, then `playbook_bootstrap_failed` + `human_attention` fire.
4. **Daily reflection happy path.** After ≥ 24h of team activity (≥ 3 archived tasks), the daily run fires at 04:00 UTC, evidence bundle composes correctly, Coach proposes changes, engine applies, runs.jsonl gets a row including non-zero `relevance_increments`.
5. **Activity gate skips.** Idle day: < 3 archived tasks, runner skips with `outcome="skipped_no_activity"`, no LLM call billed.
6. **Cost gate skips.** Set `HARNESS_TEAM_DAILY_CAP` low; runner skips with `outcome="skipped_cost_cap"`.
7. **Coach reads the lattice.** Compose a Coach turn after the runner has applied changes; verify the new weights appear in Coach's system prompt under `## Orchestration playbook`.
8. **Player reads the lattice.** Compose a Player turn after a daily run; verify the same `## Orchestration playbook` appears in their system prompt (universal load path).
9. **Coach mid-turn proposal.** Coach calls `coord_propose_playbook_changes` from a normal coordination turn; engine applies; lattice updates; next Coach turn sees the change. Verify Players cannot call the tool (rejected with Coach-only error).
10. **Settle / stale.** Manually push a statement to weight 0.97 via the dashboard, leave 7 days of history, verify the runner archives it as settled. Same for low-weight statement. Confirm a brand-new statement at 0.97 (no 7-day history yet) does NOT settle.
11. **Stale-unused.** Create a statement; leave it unmentioned by Coach for 30 days; verify auto-archive with `archive_reason="stale_unused"`.
12. **Soft cap pressure.** Force the lattice to 99 statements; daily runner with ≥ 2 creations forces engine to apply pending settles/stales first, drops over-cap creations into rejected.
13. **Hard cap.** Force the lattice to 109 statements with a runner producing 5 creations; verify all 5 dropped, only adjusts/merges apply, `human_attention` fires.
14. **Cross-op conflict.** Coach proposes merge pb-A → pb-B AND adjust pb-A in same call; verify adjust rejected with `id_archived_in_same_run`.
15. **Codex fallback.** Disable Claude credentials; verify the runner uses the Codex fallback and produces an applicable reflection. Cost-basis row reflects `runtime="codex"`.
16. **Override + restore.** Click NO on a high-weight statement; weight drops to 0.0; click restore via dashboard archive view; statement comes back at original weight.
17. **Reset.** Click Reset twice; lattice + archived + runs all clear; next scheduler tick re-bootstraps from prose.
18. **Coach behaves.** Over a week of normal operation, Coach's `coord_approve_stage` notes show evidence of following high-confidence patterns. Validation criterion §17(a) is the formal measure.

### 18.1 Unit-test coverage matrix

The §15 test files must cover the following surface — these are the edge cases that distinguish a correct implementation from a happy-path-only implementation. Aim for one assertion per bullet:

**`test_playbook_store.py`:**
- Missing `lattice.json` returns empty schema; first write creates the file atomically.
- Concurrent reads see consistent state (atomic-rename invariant).
- `weight_history` cap at 50 entries — older entries trimmed on write.
- Schema_version mismatch on read raises (no silent migration in v1).
- kDrive mirror failure: disk write succeeds, warning logged, `playbook_kdrive_mirror_failed` event emitted, no rollback (N2).

**`test_playbook_mutate.py`:**
- Adjust delta cap ±0.25 rejection.
- Adjust on immutable rejected.
- Merge: `keep_id` weight = max; `applied_count` summed; `last_validated_at` = max (NULL-safe).
- Merge with immutable target rejected.
- Settle predicate: requires ≥7-day-old history entry (rejects same-day-created statements).
- Stale-low predicate: same 7-day requirement; immutable skipped.
- Stale-unused predicate: 30-day age + applied_count==0; immutable skipped.
- Override (NO/½/YES): rejects when immutable.

**`test_playbook_bootstrap.py`:**
- Happy path: prose → ~30 statements → lattice persisted → flag set → `playbook_reset_at` cleared.
- Missing prose template: empty lattice persisted, flag set (no LLM call, no retry counter increment).
- Tolerant JSON-array extraction from prose-wrapped LLM response.
- Malformed LLM response → retry counter increments, `_failed{blocked: false}` event.
- 3rd consecutive failure → `_failed{blocked: true}` + `human_attention` + `playbook_bootstrap_blocked` set.
- Scheduler tick with `playbook_bootstrap_blocked` set → no bootstrap attempted (G1 infinite-loop test).
- Cost cap exceeded: `outcome="skipped_cost_cap"` row, no LLM call, no retry counter increment (G3).
- Soft cap at bootstrap: LLM returns 120 → 100 inserted, 20 dropped from end (G4).
- Hard cap at bootstrap: LLM returns 130 → 100 inserted, `playbook_soft_cap_exceeded` event fires.
- `source` field: first-deploy bootstrap → `"boot"`; post-reset bootstrap → `"reset"` (with `playbook_reset_at` set then cleared on success).

**`test_playbook_runner.py`:**
- Activity gate: zero activity → skip; threshold-1 activity → skip; threshold activity → run.
- Cost gate: over cap → skip; cost-skip doesn't increment retry counter or update `last_run_at`.
- Evidence bundle composition: archived task buckets (clean/friction/failed/cancelled) computed correctly from kanban-v2 fixtures.
- Median window fallback when trajectory shape has < 5 samples; `median_cost_fallback_fired` flagged.
- Op apply order: merges → creates → adjusts; cross-op conflict (adjust on freshly-merged id) rejected with `id_archived_in_same_run`.
- `relevant_ids` increment: valid ids increment; malformed entries skipped per regex (N5); duplicates deduped.
- Soft cap pressure: drops from end of input list; survivors apply.
- Hard cap pressure: drops ALL creations; `human_attention` fires; adjusts/merges still apply.
- LLM parse failure → `outcome="error_parse"`; no retry within day.
- All proposals rejected → `outcome="no_changes"`.
- Codex fallback: Claude raises → Codex called; runs.jsonl row reflects `runtime="codex"`.

**`test_playbook_scheduler.py`:**
- Skip on `playbook_disabled`.
- Skip on no active project.
- Bootstrap path gated by `playbook_bootstrap_blocked` (G1).
- Daily-run path gated by past run hour + last-run-date + activity + cost.
- `_run_lock` prevents concurrent scheduler + manual run.

**`test_playbook_api.py`:**
- All endpoints require `HARNESS_TOKEN`.
- `audit_actor` recorded on write events.
- `POST /run`: 409 when `playbook_bootstrap_done` unset.
- `POST /bootstrap` (G7): 409 when bootstrap already done; 409 when blocked; 409 on lock contention.
- `POST /reset`: blocking lock acquisition with 60s timeout; 503 on timeout (G2). Confirm flag clears (`done`, `retries`, `blocked`) + `reset_at` set.
- `POST /statements/{id}/weight`: rejects when immutable.
- `POST /statements/{id}/restore`: round-trips an archived statement.

**`test_playbook_render.py`:**
- Empty lattice → empty string.
- `playbook_disabled` set → empty string.
- Bucketing by weight ranges (validated / working / uncertain / anti-pattern).
- Within-bucket sort by `weight × log(1 + applied_count)` descending.
- Size budget: > 8 KB lattice → "Uncertain" bucket dropped from rendered output.
- Output is self-contained markdown (includes `## Orchestration playbook` heading per N4).

**`test_playbook_mcp_tool.py`:**
- Coach-only enforcement (Player call rejected).
- Cap of 5 ops per call enforced.
- Same op apply order as runner (merges → creates → adjusts).
- Lock contention: returns string starting with `"playbook engine busy"` (G8 + N9); no mutation.
- Codex Coach reaches the tool via the proxy path (smoke test against the runtime dispatch fixture).

---

## 19 · Open question

One item to confirm during implementation:

1. **Compact interaction.** Coach's continuity_note carries forward across compact, but the playbook is loaded fresh every turn from disk via `build_system_prompt_suffix` — so compact is a non-issue. Confirm during implementation that compact-mode turns get the full system prompt suffix (they should — same `build_system_prompt_suffix` call).

All other prior open questions resolved during the audit:

- Bedrock list: NONE in v1; `immutable: true` retained as latent schema capability.
- Run-hour staggering: v1 single env var (`HARNESS_PLAYBOOK_RUN_HOUR_UTC`); revisit at 2nd harness instance.
- Tool naming: `coord_propose_playbook_changes` (matches `coord_*` verb-first convention).
- Prose template fate: keep `app_dev_playbook.md` as historical reference + re-bootstrap source.
- Player visibility: Players read the lattice (universal system-prompt path); cannot influence (Coach-only MCP).

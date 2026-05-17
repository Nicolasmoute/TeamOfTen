---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 10: Claude Context and Prompt Assembly'
section: 10
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 10. Claude Context and Prompt Assembly

Prompt layers (order matches `agents.py:run_agent`):

1. Per-agent identity block from `agent_project_roles`.
2. Baseline Coach or Player role prompt.
3. Global rules from `/data/CLAUDE.md`.
4. Active project rules from `/data/projects/<slug>/CLAUDE.md`.
5. **Coach-only**: orchestration playbook lattice (see
   `playbook-specs.md`), when non-empty. Players don't see this block;
   coordination cues reach them through Coach's per-stage wake notes.
6. Per-agent `brief` from `agent_project_roles`.
7. Coach-only coordination block from current
   project/team/tasks/inbox/wiki/decisions/health rollups.
8. **Coach-only**: `## Project objectives` (verbatim from
   `/data/projects/<slug>/project-objectives.md`) followed by
   `## Open coach todos` (verbatim from `coach-todos.md`). Both are
   re-read every turn; either section is omitted entirely when its
   file is missing or empty. Defined by
   [recurrence-specs.md](recurrence-specs.md) §6. This is the
   **single canonical surface for goal content** — neither the
   coordination block (#7) nor the per-project CLAUDE.md (#4)
   carries a `Goal:` line or `## Goal` section. See
   recurrence-specs §6.1 for the rationale and §8.3 above for the
   stub template that points to this file.
9. One-shot `## Prior turn note` when the previous turn ended with
   `is_error=True` (popped after one consumption; skipped on
   compact-mode spawns so the note reaches the user-facing turn).
10. Continuity handoff after `/compact`, when present.

`server/context.py` re-reads the global and project `CLAUDE.md` files every turn.
Each file is truncated at 200,000 chars to prevent runaway prompt bloat.

### 10.0 Section ordering and Anthropic prompt caching

The order above is tuned for Anthropic's automatic prompt cache, not
for the most natural reading order. The Claude CLI applies cache
breakpoints automatically, but only the **longest stable byte-prefix**
across consecutive turns is reused — any change in section N
invalidates everything from section N onward. Because the per-turn
prompt-log analysis (§10.5) showed `context_suffix` (#3+#4+#5) is
~97% of the prompt's bytes, putting any per-turn-mutating block in
front of it busts the cache for the heavy body on every turn.

The rule the assembly follows: **stable blocks first, dynamic blocks
last.** Per-agent stability:

| Section | Stability                                            |
|---------|------------------------------------------------------|
| identity | per (slot, project), changes on rare edits          |
| role baseline | constant per slot                              |
| global CLAUDE.md | per file mtime                              |
| project CLAUDE.md | per file mtime                             |
| playbook | per lattice edit (Coach only; absent for Players)   |
| brief    | per `agents.brief` edit                             |
| coordination | per Coach turn (Coach only; empty for Players)  |
| coach supplement | per objectives/todos change (Coach only)    |
| prior_error | one-shot, present only after a failed turn       |
| handoff | present only on the first turn after `/compact`      |

Players' prompts are stable across turns within a session (no
coordination/supplement injection), so cache hit rates are typically
high. Coach's prompt mutates on every turn — the ordering ensures the
mutation hits the **end** of the prompt, leaving the cached prefix
intact through the heavy body.

`cost_usd` recorded on each turn already reflects the cache discount
the SDK applied, so a higher cache hit rate shows up in BOTH
`cache_hit_pct` (higher) and `cost_usd` (lower) — the two are
correlated, not double-counted.

Monitoring:

- `GET /api/turns/summary?hours=N` returns per-agent and team-wide
  rollups including `input_tokens`, `output_tokens`,
  `cache_read_tokens`, `cache_creation_tokens`, and a derived
  `cache_hit_pct = cache_read / (input + cache_read +
  cache_creation)`. Top-level fields mirror these as
  `total_*_tokens` + `total_cache_hit_pct`.
- EnvPane cost section renders the team-wide hit rate as a cap-bar
  row and a `cache NN%` pill on each per-agent row, polled on the
  same 60s cadence as the plan-included token meter.
- `/spend [hours]` slash command prints the same rollup as plain
  text into the pane info banner.

Two non-trivial section reorders have shipped against this rule:

- 2026-05-10 — moved the Coach coordination block from position #2
  (right after identity) to its current position after CLAUDE.md +
  brief. Coach's hit rate had been near zero because the
  coordination block mutates on every turn and was sitting in front
  of the heavy body.
- 2026-05-10 — moved the prior-error suffix off the dynamic-tail
  middle into its current single-shot slot at #9. Same rationale.

If a future block needs to land between context_suffix and brief
(e.g. a "current task" recap), measure cache hit rate before and
after — if it mutates per turn, it belongs after brief, not before.

### 10.1 Identity

Agents are told:

- Their slot id.
- Their project-specific name/role if set.
- Their workspace path.
- Active project paths.
- Governance notes for Coach/Player role.

Players are auto-named from a lacrosse surname pool on first spawn if they have
no `agent_project_roles.name` for the active project. The auto assignment emits
`player_assigned` with `auto: true`.

### 10.2 Coach Coordination Block

Built in `agents.py` for Coach turns. It includes:

- Active project name and a one-line pointer to the per-project
  CLAUDE.md and `project-objectives.md` (the canonical surface for
  goals / scope — see [recurrence-specs.md](recurrence-specs.md) §6
  and §6.1). The block does NOT render `projects.description` as a
  `Goal:` line; goal content has a single canonical surface
  (§8.3 + recurrence-specs §6.1).
- Team roster and locked players.
- Open/current tasks.
- Coach inbox summary.
- Recent decisions.
- Wiki paths.
- Reminder to assign roles and coordinate.

**Active task health rollup** (`## Active task health` sub-section,
`_build_active_task_health_rows` in `server/agents.py`):
Surfaces tasks where the same audit kind has failed ≥ 2 times —
the first fail is expected correction noise; the second is signal.
The rollup is capped at the top **3** tasks sorted by
`kind_fail_count` descending, with tiebreaker on
`last_stage_change_at` descending (most recently active first).
Fewer than 3 qualifying tasks → only those with a signal are shown.
More than 3 qualifying tasks → the three highest-fail-count tasks
are shown and a `(+N more)` footer line counts the remainder.
The cap is enforced by the constant `ACTIVE_TASK_HEALTH_CAP = 3`
in `server/agents.py`.

**Title rendering — no truncation.** Every sub-section in this block
(current-state tasks, stalled tasks, audit aggregator, recent patterns,
and backlog) renders task/entry titles in full — no character cap is
applied. Task titles are short summaries by convention (§14.5 limits
them to 300 chars on input), so truncation in the prompt is never
needed and would silently hide the title from Coach.

### 10.3 Compact and Continuity

Manual compact:

- UI slash command `/compact`.
- API `POST /api/agents/{id}/compact`.
- Claude runs the agent with `COMPACT_PROMPT`; Codex silently resumes
  the stored Codex thread and generates COMPACT_PROMPT-style markdown
  without streaming the handoff text to normal UI/events/logs.
- Captures the summary as `agent_sessions.continuity_note`.
- Writes full handoff file under active project's `working/handoffs/`.
- Clears session id so the next turn starts fresh.
- Emits `session_compacted` with metadata only (`chars`,
  `handoff_file`, and for Codex `summary_source` /
  `synthetic_summary`).

Auto-compact:

- Controlled by `HARNESS_AUTO_COMPACT_THRESHOLD`, default 0.65 (lowered from 0.7 on 2026-05-09, then raised from 0.5 on 2026-05-15 after 0.5 proved too aggressive).
- Estimates session context from Claude CLI JSONL files under
  `CLAUDE_CONFIG_DIR/projects/`, or from Codex rollout JSONL files
  under `CODEX_HOME/sessions` / the default `~/.codex/sessions`.
- If over threshold, runs a compact turn first (Claude) or native
  compact with handoff persistence (Codex).
- The preflight resolves the same effective model the turn will use (pane
  override, Coach-set slot override, role default, alias-to-concrete), so the
  threshold window matches the pane `ctx` bar.
- If auto-compact produces no summary, it force-clears the session to escape a
  threshold loop.

Session transfer (compact + runtime flip):

A runtime change normally loses conversation history because `session_id`
(Claude) and `codex_thread_id` (Codex) are runtime-specific and cannot
cross over. The session-transfer flow runs the compact summary on the
**source** runtime first, persists it to `continuity_note`, then flips
`agents.runtime_override` so the next turn on the **target** runtime
reads the handoff in its system prompt — same delivery vehicle as a
plain `/compact`.

ClaudeRuntime materializes the composed system prompt into a temporary
0600 file and passes it to Claude Code via `--system-prompt-file`.
This is required because the post-compact handoff plus global/project
CLAUDE.md can exceed Linux's per-argument `execve` ceiling if sent as a
literal `--system-prompt` argv value, causing
`CLIConnectionError: Failed to start Claude Code: [Errno 7] Argument
list too long` before the CLI starts.

- UI: pane gear popover's runtime selector. Picking `claude` / `codex`
  routes through the transfer endpoint; picking `default` (empty) keeps
  the legacy blunt-clear `PUT /api/agents/{id}/runtime` (no compact).
- API: `POST /api/agents/{id}/transfer-runtime {runtime}`.
- MCP: `coord_set_player_runtime(player_id, runtime)` (Coach-only).
  An empty `runtime=''` argument retains the legacy blunt-clear semantic.

Dispatch matrix at the entry point:

| Source runtime | Target runtime  | Prior session? | Action                                                                                |
|----------------|-----------------|----------------|---------------------------------------------------------------------------------------|
| X              | X (same)        | —              | 200 noop                                                                              |
| X              | Y               | NO             | flip immediately, emit `runtime_updated` + `session_transferred(note=no_prior_session)` |
| X              | Y               | YES            | queue `run_agent(COMPACT_PROMPT, compact_mode=True, transfer_to_runtime=Y)`             |

Mid-turn flips (`agents.status='working'`) are 409'd at the entry point —
the in-flight turn would be on the old runtime while subsequent turns
use the new one.

Compact-handler branch:
`transfer_to_runtime` rides through `run_agent` → `TurnContext` →
`turn_ctx`. Each runtime's compact handler reads it after the
post-compact bookkeeping (`continuity_note` written, source session id
cleared) and calls `_perform_runtime_transfer_flip(slot, target)` —
flips `runtime_override`, nulls **both** runtime session columns
(defensive against orphaned thread ids from a prior life on the
target), evicts any cached Codex client, emits `runtime_updated` with
`source=session_transfer`. Then the handler emits
`session_transferred(from_runtime, to_runtime, chars, handoff_file)`
in place of `session_compacted`.

Failure modes:

- Compact yields no summary on Claude → `session_transfer_failed`
  emits and the runtime stays put. The intent of transfer is "carry
  forward via summary"; a flip with empty context is a destructive
  blind switch.
- Compact yields no generated summary on Codex → recent-exchange
  fallback may be used, but it is marked
  `summary_source='recent_exchange_fallback'` and
  `synthetic_summary=true`. If neither generated nor fallback handoff
  can be durably persisted, `session_transfer_failed` emits and the
  runtime plus `codex_thread_id` stay put.
- Helper failure on `_clear_codex_thread_id` is logged but doesn't
  abort; `runtime_updated` still emits so the UI doesn't silently
  miss the change.

Why not just `/compact` followed by a blunt PUT: atomicity. The flip
only happens iff the compact succeeded with a non-empty summary
on both runtimes. A user who runs the two operations separately gets
the flip even when the compact failed, leaving the agent on the new
runtime with no handoff. The transfer flow also emits the right event
vocabulary so timelines read as a single transfer boundary, not as a
compact plus an unrelated runtime change.

See `Docs/CODEX_RUNTIME_SPEC.md` §E.8 for the full design.

Compact prompt structure:

`COMPACT_PROMPT` in `server/agents.py` instructs the agent to produce a
1500–3000 word handoff document with these markdown sections, in order:

1. **Primary request and intent** — original ask + scope additions, verbatim.
2. **Key technical concepts** — glossary of terms used in the session.
3. **All operator messages (verbatim, in order)** — every human message,
   numbered, including one-word replies. Preserves voice that paraphrase
   loses.
4. **How we got here** — narrative arc, dead ends, recurring workflow pattern.
5. **Files touched** — per-file inventory tagged **touched** vs **read-only**,
   with diffs / snippets inline for recent or relevant files.
6. **Errors & fixes** — one entry per failure: symptom, root cause, fix,
   regression test.
7. **Key findings & decisions** — what / why / who agreed.
8. **Open questions** — unresolved items, quoted verbatim.
9. **References** — URLs, commit hashes, external links not covered above.
10. **People & roles** — who participated, responsibilities, preferences.
11. **Context quirks & gotchas** — environment / tool peculiarities.
12. **In-flight state at compact** — last assistant message verbatim, last
    tool call, exact next action.
13. **Pending — concrete checklist** — `[ ] Action — owner — blocking?` items.

The prompt explicitly tells the agent NOT to append a footer pointing at the
JSONL or handoff file. The harness appends that itself via
`_build_compact_footer()`, naming `$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/
<session-id>.jsonl` so fresh-you can read the full transcript on demand.

Recent exchange preservation:

- `last_exchange_json` stores a bounded rolling log.
- Budget: `HARNESS_HANDOFF_TOKEN_BUDGET`, default 20,000 tokens.
- Full session transcript remains in Claude CLI JSONL until session retention
  trims it (`HARNESS_SESSION_RETENTION_DAYS`, default 30).

### 10.4 Context Usage UI

`GET /api/agents/{id}/context` returns:

- `session_id` (Claude SDK)
- `codex_thread_id` (Codex)
- estimated used tokens for the current resumed prompt/session footprint
- context window
- model (resolved — falls back to the latest turn's model when the UI
  doesn't pass an override)
- ratio

Estimation semantics — Claude path (when `session_id` is set):

- Read the Claude Code session JSONL under `CLAUDE_CONFIG_DIR/projects/`.
- Use the latest assistant usage row as the prompt-size source of truth.
- Count `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`.
- Add the latest assistant output tokens because they become part of the next
  resumed prompt.
- Do not sum `ResultMessage.usage` across tool rounds; that overstates context
  pressure when prompt caching is active.

Codex path: the Claude path returns 0 for codex sessions; the server
reconstructs prompt size from the latest `turns` row matching the
codex thread id (`runtime = 'codex'`). The CodexRuntime is responsible
for populating that row from its own usage source. Parser shape and
known limitations: see `Docs/CODEX_RUNTIME_SPEC.md` §E.5.

Window resolution: `_context_window_for(model)` returns the per-model
max. When the UI doesn't pass `?model=`, the server reads the model
recorded on the latest turn for the active session. Auto-compact uses
the same effective model resolver before its threshold check, so
auto-wakes that omit a pane model do not fall back to the generic 1M
window. For Codex turns,
`token_count.info.model_context_window` from the rollout JSONL is
stored as a provider-reported exact window and takes precedence over
the static table. That lets the CTX bar and auto-compact adapt when
Codex `gpt-5.5` has a smaller effective app-server window than the
generic model id's public API maximum.

The pane renders this as a compact `ctx` bar: current footprint as a fraction
of the model's max window.

### 10.5 Prompt-size telemetry (offline analysis)

`server/prompt_log.py` writes one JSONL row per non-compact agent
spawn to `<HARNESS_DATA_ROOT>/prompt_log/<YYYY-MM-DD>.jsonl`. Each
row carries timestamp, agent id, runtime, model, total chars, and a
per-section char breakdown matching the assembly above
(`identity`, `coordination`, `role_baseline`, `context_suffix`,
`brief`, `coach_supplement`, `prior_error`, `handoff`, `lock`).

Disable via `HARNESS_PROMPT_LOG=false`. Compact-mode spawns bypass
the recording branch (their prompt is composed elsewhere and the
section names don't apply).

`scripts/analyze_prompt_log.py` prints three rollups: per-agent
turn count + mean/p50/p95/max chars, per-section share-of-total,
and the heaviest turns. The 2026-05-09 baseline run flagged
`context_suffix` (global + project CLAUDE.md, plus the playbook
on Coach turns only) at ~97% of total prompt size — the input
that drove the 10.0 section reorder for cache stability.

This is purely diagnostic. The `turns` table is the authoritative
runtime cost surface (cost_usd, token columns, cache rollups);
`prompt_log` measures what we _sent_ in chars, before tokenization
or cache lookup.

---

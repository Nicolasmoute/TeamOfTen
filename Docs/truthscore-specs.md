---
schema: teamoften-spec/v1
title: 'TruthScore Specification'
status: canonical
spec_group: truthscore
source_index: truth-index.md
last_reorganized: 2026-05-17
---
# TruthScore — Specification

> **Subordinate to `Docs/truth-index.md`.** When this doc and truth-index
> disagree, truth-index wins. This file goes deeper on TruthScore (rubric,
> input gathering, prompt, parsing, storage) but cannot redefine
> fields, endpoints, events, or invariants that truth-index declares.

**Status:** Implemented (2026-05-09).
**Target:** TeamOfTen multi-agent harness (Python, Claude Agent SDK, kDrive-backed shared state, single-VPS)
**Version:** 0.1

---

## 0 · One-paragraph summary

TruthScore is a one-shot, on-demand evaluator that compares the
project's current state — repo at HEAD of `main`, plus
`decisions/`, `working/knowledge/`, and `outputs/` — against the
human-vetted `truth/` corpus and returns a five-criterion score
(1–10 per axis, mean as overall) plus a brief overall comment. It
is invoked deliberately by a human via `/truthscore [commentary]`,
by Coach or any Player via the `coord_run_truth_score` MCP tool,
or programmatically via `POST /api/truthscore`. The result is
written to `working/knowledge/truthscore-<YYYY-MM-DD-HHMM>.md` and
returned in the response. There is no UI, no scheduler, no
recurring run — TruthScore exists to give the human (or Coach) a
quick health check whenever they want one. Adjacent to but
distinct from Compass: Compass operates on **intent** (the lattice
of directional claims) and runs autonomously; TruthScore operates
on **specification fidelity** (does the project match the binding
truth corpus) and runs only when asked.

---

## 1 · Conceptual model

### 1.1 What TruthScore measures

`truth/` is the project's binding spec layer — human-vetted,
write-protected, edited only via `coord_propose_file_write(scope='truth', ...)`.
The rest of the project (repo, decisions, knowledge, outputs)
implements, records, and elaborates that spec. TruthScore answers
a single question: **how well does the implementation reflect the
spec?**

This is deliberately narrower than Compass's job. Compass asks "is
the project heading in the right direction?" — a question the
human and Coach work out together over time via the lattice.
TruthScore asks "given what truth says we said we'd build, did we
actually build it?" — a question with a more mechanical answer
that an LLM can produce in a single pass.

### 1.2 The five canonical criteria

Each criterion is scored 1–10 (integer). Overall is the arithmetic
mean of the five, rounded to one decimal.

| # | Criterion | What it measures | Low score points at |
|---|-----------|------------------|--------------------|
| 1 | **Fidelity** | Does the implementation align with what truth/ specifies? | Code drifted from spec; **fix the code** |
| 2 | **Completeness** | How much of truth's commitments are realized vs partially built or absent? | Spec'd features not yet built |
| 3 | **Consistency** | Do `decisions/`, `working/knowledge/`, `outputs/` agree with truth? Internal contradictions? | Sub-corpora telling different stories |
| 4 | **Currency** | Is truth/ up-to-date with what actually exists? | Truth drifted from code; **update truth via `coord_propose_file_write`** |
| 5 | **Clarity** | Is truth/ itself specific enough to score against? | Vague truth caveats every other axis |

The Fidelity ↔ Currency split is intentional: same gap, different
remediation. Lumping them as a single "drift" score loses the
"who needs to move" signal that's the whole point of running the
score.

Clarity is meta but load-bearing — a Clarity of 4 means "the other
scores are noisy because truth itself is fuzzy" and should be read
as a flag to tighten truth/ before relying on the rest of the
output.

### 1.3 Scale anchors

The LLM gets these anchors in the system prompt so scores are
roughly comparable across runs:

- **10** — perfect alignment on this axis. No gaps, no drift, no
  contradictions, no vagueness. Almost never awarded.
- **8–9** — strong alignment. Gaps are minor and known. The team
  can ship to stakeholders with caveats.
- **6–7** — workable. Notable gaps but the project's bones match
  the spec. Default expectation for a healthy mid-project state.
- **4–5** — significant divergence. The spec and the implementation
  are telling different stories in non-trivial ways.
- **2–3** — broken. Either spec or implementation is largely
  fictional relative to the other.
- **1** — adversarial. Used only when the implementation actively
  contradicts the spec (not merely lags or omits).

### 1.4 What TruthScore does NOT do

- **No file-by-file audit.** The "Comment" section is 2–4
  sentences of overall framing, not a per-file rundown. The
  scores themselves carry the per-axis signal; humans who want
  details ask Coach or read the result file's "Inputs" footer.
- **No automated remediation.** TruthScore reports; it does not
  open tasks, queue questions, or write to `truth/`. A low score
  is information for the human / Coach to act on.
- **No recurring schedule.** Every invocation is human- or
  agent-initiated. The cost cap (§5.3) bounds spend regardless,
  but there is no `truthscore_recurrence` table.
- **No score history aggregation.** Each result file stands alone.
  The human can `ls working/knowledge/truthscore-*` to see
  trajectory; we don't compute trend lines or compare runs.

---

## 2 · Surfaces

Three entry points, all routing to the same `run_truth_score(...)`
function in `server/truthscore.py`.

### 2.1 Slash command — `/truthscore [commentary]`

Handled in [server/static/app.js](../server/static/app.js)
alongside the existing `/compact`, `/tick`, etc. patterns.

- **No args** → run with empty commentary.
- **With args** → all text after `/truthscore ` becomes the
  one-shot commentary block, passed verbatim into the prompt
  under `## Scoring directives (honor these literally)`.
- Posts to `POST /api/truthscore` with the active project resolved
  server-side from `resolve_active_project()`.
- Result rendered in the same pane the slash was issued in as a
  `truthscore_completed` `.sys` row, with a clickable link to the
  written result file (uses the existing `harness-file-link` /
  `pendingFileOpen` machinery in [server/static/app.js](../server/static/app.js)).

### 2.2 MCP tool — `coord_run_truth_score(commentary?)`

Available to **Coach, every Player, and (via the same call) the
human**. No role gate. Rationale:

- Players might legitimately self-check before pushing a major
  feature or shipping a kanban task — same reasoning that opened
  `compass_ask` to Players.
- TruthScore is read-only against truth/; there is no "destructive"
  axis that Players should be denied.
- The cost cap (§5.3) bounds abuse without a role gate.

Tool signature:

```python
@tool("coord_run_truth_score",
      "Score project state against the truth/ corpus. ...",
      {"commentary": str | None})
async def coord_run_truth_score(args: dict) -> dict:
    ...
```

Returns the same payload shape as `POST /api/truthscore` (§2.3).

### 2.3 HTTP endpoint — `POST /api/truthscore`

Body:
```json
{ "commentary": "skip section 2" }
```

Response (200):
```json
{
  "ok": true,
  "result_path": "working/knowledge/truthscore-2026-05-09-1430.md",
  "overall": 7.4,
  "scores": {
    "fidelity": 8, "completeness": 7, "consistency": 9,
    "currency": 6, "clarity": 7
  },
  "comment": "Broadly aligned. Main gap: ...",
  "inputs": {
    "truth_files": 4, "truth_bytes": 18234,
    "main_sha": "0d98975...",
    "main_files_indexed": 287, "main_bytes_sampled": 142_113,
    "decisions": 12, "knowledge": 8, "outputs": 3
  },
  "turn_id": 4421
}
```

Failure cases:

- **No active project** → 400 `{"detail": "no active project"}`.
- **truth/ empty or missing** → 400 `{"detail": "truth/ corpus is empty — TruthScore needs a spec to score against"}`. (No point scoring against nothing.)
- **No `main` branch on the seed clone** → 400 `{"detail": "project has no 'origin/main' ref in <path> — push the project's main branch first"}`. (Pointer to `POST /api/projects/{id}/repo/provision`.)
- **Concurrent run in flight** → 409 `{"detail": "TruthScore is already running for this project"}`. Per-project lock; different projects can run concurrently.
- **Team daily cap hit** → 429 `{"detail": "team daily cost cap reached (...) — try again tomorrow"}`.
- **LLM call failed** → 502 `{"detail": "LLM call failed: <reason>"}`. Result file not written.
- **Parse failure** (LLM returned malformed output) → 502 `{"detail": "LLM output failed to parse against the expected schema; raw output written to working/knowledge/truthscore-<ts>-RAW.md"}`. Result file written verbatim under the `-RAW.md` filename for debugging; no structured response returned.

Auth: standard `Depends(require_token)` + `audit_actor`. Actor (web / mcp-tool / telegram) lands in the `truthscore_completed` event payload.

---

## 3 · Inputs gathered

`run_truth_score()` assembles four input layers, each with a
budget. Total prompt body capped at ~180 KB (well under any
Sonnet context window, leaves room for output).

### 3.1 Truth corpus (rubric)

- All `<project>/truth/**/*.{md,txt}` files.
- Read via direct filesystem, not git — truth/ may have
  uncommitted changes mid-proposal-cycle, and we want to score
  against what the human has actually approved (the on-disk
  state is the post-approval state).
- Body included verbatim per file; cap **32 KB total**. Over-cap
  truncation: per-file head 16 KB, then drop tail-most files
  (alphabetical within each subdir) until under cap. A truncation
  warning is included in the prompt and surfaces in the result's
  Inputs footer.
- Empty corpus → 400 (see §2.3).

### 3.2 Project objectives (context)

- `<project>/project-objectives.md` if present.
- Verbatim, capped at **8 KB**.
- Not scored — feeds the LLM context only ("here's what the
  project is trying to do; that informs how literally to read
  truth/").

### 3.3 Repo at HEAD of `main`

The biggest input by volume; needs the most care.

- Resolve the seed clone path via `project_paths(active).bare_clone`
  (= `/data/projects/<id>/repo/.project`). The attribute name is
  misleadingly historical — production uses a regular `git clone`
  (not `--bare`), so the path has a `.git/` subdir like any normal
  working clone, and `refs/remotes/origin/main` is populated.
- `git -C <seed-clone> fetch origin main` first (cheap if
  already current; gets the latest pushes from per-slot
  worktrees that committed via `coord_commit_push`).
  **Best-effort:** when `fetch` fails (network down, auth
  rotated, etc.) the run continues against the cached
  `origin/main` ref and surfaces a warning in the result file's
  Inputs footer + the `truthscore_completed` event payload.
  Hard-fail only when no `origin/main` ref exists at all.
- Capture HEAD SHA: `git -C <seed-clone> rev-parse origin/main`.
- File index: `git -C <seed-clone> ls-tree -r origin/main --name-only`.
- For each text file (binary-detection: extension allow-list +
  first-1KB null-byte sniff for unrecognized extensions), read
  body via `git -C <seed-clone> show origin/main:<path>`. The
  text-extension allow-list is enumerated in
  [server/truthscore.py `TEXT_EXTENSIONS`](../server/truthscore.py)
  and includes the common code/config/data extensions
  (`.md .py .js .ts .json .yaml .toml .css .html .sql .sh .rs
  .go .java .c .cpp .h .rb .php .lua` and friends).
- Per-file body cap: 16 KB head. Total repo body cap: **80 KB**.
- Files included up to budget in this order:
  1. Always-include set (try-these-first heuristic, NOT a
     contract — files are attempted-and-skipped-if-missing so
     non-Python or non-JS projects still degrade cleanly):
     `README.md`, `CLAUDE.md`, `pyproject.toml`, `package.json`,
     `Dockerfile`, `Cargo.toml`, `go.mod`, `requirements.txt`.
  2. Files referenced by name in any truth/ file (string match
     against the truth body).
  3. Files in directories referenced by name in any truth/ file.
  4. Remaining files alphabetical.
- Binaries / images / archives: included as path-only lines in
  the file index; never body-read.
- The full file index (paths only, no bodies) is **always**
  included regardless of budget — so the LLM can reason about
  "this directory exists but nothing in it was sampled" rather
  than thinking the project is smaller than it is.

### 3.4 Working / decisions / outputs (sub-corpora)

For each of `decisions/`, `working/knowledge/`, `outputs/`:

- Index all files (path + size + mtime).
- For text files: body included up to **8 KB total per
  sub-corpus**, with **2 KB per file** head cap.
- For binary outputs (PDF, DOCX, XLSX, PPTX): use the existing
  [server/compass/output_extractor.py](../server/compass/output_extractor.py)
  `extract_body(path)` helper. When it returns `None` (missing
  parser dep, malformed file, image format) the file falls back
  to a path-with-size line in the rendered output rather than
  silent omission — so the LLM still sees that the file exists.
- Selection within a sub-corpus when over budget: most
  recently-modified first, then alphabetical.

### 3.5 Commentary (one-shot directive)

The `commentary` string from the caller, passed verbatim into
the prompt under a clearly-marked block. Empty / whitespace-only
commentary → block omitted entirely.

Trust model: commentary is human / Coach / Player input, not an
arbitrary-agent injection vector at the API layer (the audit
`actor` field records who supplied it). No length cap beyond the
overall prompt budget; the LLM is told to **honor** directives
literally for legitimate scoping, but the system prompt explicitly
instructs that score-manipulation directives ("score 10 on
everything", "set fidelity to a minimum of 8") comply with the
override but PREFIX the `comment` with `[CALLER-OVERRIDE: <what>]`
so the human reading the result can see the score reflects an
instruction rather than the LLM's independent assessment. This
distinguishes legitimate scoping like `"skip section 2"` from
score-rigging.

### 3.6 Why no Compass lattice input

TruthScore is the binding-spec evaluator; the lattice is the
intent evaluator. Mixing them would produce a score that's
neither — and Compass already runs its own audit on plan-exit.
A future cross-check ("does TruthScore disagree with what
Compass thinks the project should look like by now") is plausible
but not v1.

---

## 4 · LLM call

### 4.1 Model + effort

Hardcoded to **`latest_sonnet`** at **`medium`** effort, mirroring
Compass's stance. No env override, no UI knob, no per-project
override.

Bumping the tier when Anthropic ships Sonnet 4.7 means editing
only `_ALIAS_TO_CONCRETE` in
[server/models_catalog.py](../server/models_catalog.py).

If the primary Claude path fails, the same Compass-style Codex
fallback applies (`latest_mini` at `medium`) via
[server/compass/codex_llm.py](../server/compass/codex_llm.py),
since TruthScore reuses the `compass.llm.call` wrapper.

### 4.2 System prompt skeleton

```
You are TruthScore, the project-fidelity evaluator for the
TeamOfTen harness. Score a project's current state against its
truth/ corpus on five canonical criteria. Output STRICT JSON
matching the schema below — no prose before or after.

## Criteria (1–10 integer each)

1. Fidelity — does the implementation align with what truth/
   specifies? Low = code drifted from spec.
2. Completeness — how much of truth's commitments are realized?
   Low = features specified but not built.
3. Consistency — do decisions/, working/knowledge/, outputs/
   agree with truth/? Low = internal contradictions.
4. Currency — is truth/ up-to-date with what actually exists?
   Low = truth/ describes an older project state than the code.
5. Clarity — is truth/ itself specific enough to score against?
   Low = vague truth/ caveats the other axes.

## Scale anchors
[anchors from §1.3, embedded verbatim]

## Scoring directives (if present)
[caller-supplied commentary, verbatim]

## Output schema
{
  "scores": {
    "fidelity": int, "completeness": int, "consistency": int,
    "currency": int, "clarity": int
  },
  "comment": str  // 2–4 sentences, overall framing only
}
```

User message: the assembled inputs (§3.1–§3.4) with clear
section headers. No tool use; no MCP servers attached.

### 4.3 Parsing

- Extract the JSON object via `parse_json_safe` (existing util in
  [server/compass/llm.py](../server/compass/llm.py)).
- Validate: all five score keys present, each is int 1..10,
  comment is non-empty string < 2000 chars.
- On validation failure: write the raw output to
  `working/knowledge/truthscore-<ts>-RAW.md` and return 502 (see §2.3).
- On success: compute `overall = round(mean(scores.values()), 1)`
  and proceed to write the formatted result file.

### 4.4 Cost ledger

One row in `turns` per call:
- `agent_id = "truthscore"`
- `runtime = "claude"` (or `"codex"` on fallback)
- `cost_basis = "truthscore:run"`
- `input_tokens` / `output_tokens` / `cache_*_tokens` extracted
  via the existing `_extract_usage` helper.

Counts against `HARNESS_TEAM_DAILY_CAP` like every other call.
Pre-fire check (§5.3) prevents starting a run that would breach
the cap.

**Cost estimate.** The total prompt body lands around 180 KB ≈
45K input tokens (32 KB truth + 80 KB repo + 24 KB sub-corpora +
8 KB objectives + system prompt + commentary). At Sonnet pricing
(~$3 / Mtok input + $15 / Mtok output) and ~500 output tokens,
that's roughly **$0.10–0.20 per run**. Quoted in both the MCP
tool description and operator-facing UI hints so callers don't
get cost surprises.

---

## 5 · Storage, events, and policy

### 5.1 Result file

Written to:
```
/data/projects/<active>/working/knowledge/truthscore-<YYYY-MM-DD-HHMM>.md
```

Filename uses UTC time components (the harness is UTC-internal; no
timezone in name). If two runs somehow land in the same minute
(rare given the per-project lock in §5.5), the second appends `-2`,
`-3`, etc. — defensive belt-and-braces.

The body opens with a **YAML front-matter** block exposing the
structured fields (`overall`, per-axis scores, `main_sha`,
`actor_source`, `commentary_present`, `created_at`) so a future
`/truthscore --diff` mode (§8) can parse historical scores without
re-running the LLM. The prose body follows.

Mirrored to kDrive synchronously via the existing knowledge-lane
mirror path (`coord_write_knowledge` shares the write helper).

### 5.2 Bus events

- **`truthscore_started`** — payload `{actor, commentary_present: bool, project_id}`.
- **`truthscore_completed`** — payload `{actor, project_id, overall: float, scores: {...}, comment_short: str (first sentence), result_path: str, main_sha: str, fetch_warning: str | null}`. Fan-out via the optional `to` field: omitted entirely for HTTP / slash invocations (the slash-command result lands in the pane the slash was issued in via the local response handler), set to `<slot_id>` for MCP calls (Coach: `to: "coach"`; Player p3: `to: "p3"`). The events SQL filter at `/api/events?agent=<slot>` includes the `payload_to = ?` branch for the truthscore event family so MCP-fired events also show up in pane-history reload.
- **`truthscore_failed`** — payload `{actor, project_id, reason: str}` for parse failures, LLM failures, missing prerequisites.

No `truthscore_skipped` event — there's no recurring scheduler to
emit skip reasons.

### 5.3 Cost cap interaction

Pre-flight: `_today_spend() >= TEAM_DAILY_CAP_USD` → return 429
without spawning the LLM call. Same shape as the Compass
auto-audit watcher's gate.

No per-agent cap (truthscore is a virtual `agent_id`, not a real
slot, so per-agent caps don't apply).

### 5.4 Retention

Result files live in `working/knowledge/` and follow whatever
retention applies there (currently none — knowledge lane is
permanent). The human prunes manually.

If volume becomes a problem, a future `HARNESS_TRUTHSCORE_RETENTION`
env (keep newest N) can be added to the standard retention loop in
[server/retention.py](../server/retention.py); not v1.

### 5.5 Concurrency

Per-project asyncio.Lock (mirroring Compass's runner pattern)
prevents two TruthScore runs against the same project from
overlapping. A second invocation while one is in-flight returns
409 `{"detail": "TruthScore is already running for this project"}`.

The lock is acquired via `async with lock:` so exception paths
(LLM failure, parse failure, gather error) release the lock
cleanly without a manual `try/finally`. A failed run leaves the
project ready for an immediate retry.

Different projects can run TruthScore concurrently — the lock is
per-project, not global.

---

## 6 · Implementation outline

### 6.1 New module — `server/truthscore.py`

Public surface:
```python
async def run_truth_score(
    project_id: str,
    commentary: str | None,
    actor: dict,            # {source, ip, ua}
) -> dict:                  # the §2.3 response shape
```

Internal helpers (private):
- `_gather_truth_corpus(project_id) -> tuple[str, dict]` —
  returns (rendered prompt section, inputs metadata).
- `_gather_main_tree(project_id) -> tuple[str, dict]` — runs
  git fetch + ls-tree + show; respects budgets in §3.3.
- `_gather_subcorpora(project_id) -> tuple[str, dict]` —
  decisions / knowledge / outputs.
- `_compose_prompt(corpus, objectives, main_tree, subcorpora, commentary) -> tuple[str, str]` — returns (system_prompt, user_message).
- `_parse_llm_output(raw: str) -> dict | None` — JSON extraction
  + validation.
- `_render_result_file(scores, overall, comment, inputs, commentary, ts) -> str` —
  produces the markdown shown in §1 of the user-facing
  description.
- `_write_result(project_id, ts, body) -> str` — writes file +
  kDrive mirror; returns relative path.

### 6.2 HTTP endpoint

In [server/main.py](../server/main.py), placed near `compass.api`
mount:

```python
@app.post("/api/truthscore")
async def post_truthscore(
    body: dict = Body(default={}),
    _token=Depends(require_token),
    actor: dict = Depends(audit_actor),
) -> dict:
    pid = await resolve_active_project()
    if not pid:
        raise HTTPException(400, "no active project")
    commentary = (body.get("commentary") or "").strip() or None
    return await truthscore.run_truth_score(pid, commentary, actor)
```

### 6.3 MCP tool

In [server/tools.py](../server/tools.py), registered alongside
`compass_*`:

```python
@tool("coord_run_truth_score",
      "Score project state against truth/. Returns {overall, scores, "
      "comment, result_path}. One-shot Sonnet call (~$0.05). Available "
      "to Coach and every Player. Optional commentary string is honored "
      "literally — use it to focus or skip parts of the corpus.",
      {"commentary": str | None})
async def coord_run_truth_score(args: dict) -> dict:
    caller = args["__caller__"]
    pid = await resolve_active_project()
    if not pid:
        return {"content": [{"type": "text", "text": "Error: no active project"}], "isError": True}
    commentary = (args.get("commentary") or "").strip() or None
    actor = {"source": "mcp-tool", "agent_id": caller}
    result = await truthscore.run_truth_score(pid, commentary, actor)
    return {"content": [{"type": "text", "text": _render_for_mcp(result)}]}
```

Added to `_tools` registry + `ALLOWED_COORD_TOOLS`. Bumps
`_CODEX_TOOL_CONTRACT_VERSION` so Codex threads pick up the new
tool on next boot.

### 6.4 Slash command

In [server/static/app.js](../server/static/app.js), in the same
switch as `/compact`, `/tick`, etc.:

```javascript
if (cmd === '/truthscore') {
  const commentary = rest.trim();
  postWithToken('/api/truthscore', { commentary });
  // result lands via the truthscore_completed bus event
  return { intercepted: true };
}
```

The `truthscore_completed` event is rendered as a `.sys` row by a
new compact renderer in the existing event-renderer registry.

### 6.5 Project CLAUDE.md template

Add a one-paragraph mention of `/truthscore` and
`coord_run_truth_score` to
[server/templates/app_dev_claude_md.md](../server/templates/app_dev_claude_md.md)
under a new "Self-check tools" section (or fold into the existing
tool catalogue if there is one). The Coach-driven reconciliation
flow propagates it to existing projects on next activation.

---

## 7 · Tests

`server/tests/test_truthscore.py` covers:

**Unit:**
- `_parse_llm_output` happy path + validation failures (missing
  key, score out of range, malformed JSON, empty comment).
- `_render_result_file` produces stable markdown given fixed
  inputs (snapshot test).
- Truth-corpus gather: empty corpus raises; over-cap truncation
  drops tail files alphabetically; warning surfaces in metadata.
- Main-tree gather: binary-detection (extension + null-byte sniff);
  always-include set wins over budget; file index always complete
  even when bodies are clipped; missing `main` branch raises.
- Sub-corpora gather: per-corpus + per-file caps respected;
  most-recent-first selection; binary outputs go through
  `output_extractor`; missing extractor falls back to path-only.

**Integration (with stubbed LLM):**
- End-to-end `run_truth_score` with a stubbed
  `compass.llm.call` returning fixed JSON: produces correct
  result file, fires `truthscore_started` + `truthscore_completed`
  events, lands a row in `turns`, returns the §2.3 response shape.
- LLM raises → `truthscore_failed` event, no result file, 502.
- LLM returns malformed JSON → raw file written under
  `-RAW.md`, `truthscore_failed` event, 502.
- Cost cap hit pre-flight → 429, no LLM call.
- Per-project lock: concurrent invocations against same project
  → second gets 409; different projects run in parallel.

**HTTP layer:**
- 400 on no active project / empty truth / no main branch.
- 401 without token.
- Actor field threaded through to the bus event.

**MCP layer:**
- Coach call works; Player call works; commentary passed verbatim
  into the prompt (asserted via stub-call inspection).

Suite expected to grow by ~25 tests. No new external dependencies.

---

## 8 · Open questions / future work

- **Weighted truth files.** Considered and deferred (per
  conversation 2026-05-09): equal weighting is good enough for
  v1, and `truth/truth-index.md` weight directives add a knob
  the user has to maintain. Revisit if the user reports the
  overall score being dominated by one large but minor truth
  file.
- **Score history.** Each result file stands alone today.
  Plausible v2 features: (a) a `/truthscore --diff` mode that
  references the previous run; (b) a small dashboard tile
  showing the trajectory of overall scores. Both are pull,
  not push — TruthScore stays on-demand.
- **Compass cross-check.** "Compass and TruthScore scored the
  same project on the same day; do their pictures match?" is a
  natural follow-on. Not implemented; deferring until either
  produces a concrete user request.
- **Long-running projects.** When the repo HEAD is many MB of
  text, the §3.3 budget heuristics may pick the wrong files.
  v1 ships the simple ordering; if the user reports score
  results that obviously missed the relevant files, an LLM-side
  pre-pass that picks files based on truth content could be
  added — but it doubles the LLM cost and is not v1.
- **Multi-language repos.** Binary detection is extension- +
  null-byte-based today. If the user works with non-ASCII text
  files (CJK source, etc.) that might be mis-flagged as binary,
  add a UTF-8 decode probe to the binary check.

---

## 9 · Relationship to other subsystems

- **Compass.** Adjacent. Compass = intent (lattice, autonomous,
  daily). TruthScore = spec fidelity (one-shot, on-demand). They
  do not share state; they do share the
  `compass.llm.call` wrapper for one-shot Sonnet calls.
- **Kanban v2.** Orthogonal. TruthScore can be invoked at any
  point in the kanban lifecycle (Players might run it before
  shipping; Coach might run it before authorizing a new
  trajectory) but is not wired into stage transitions. There
  is no `truthscore_required` trajectory entry.
- **Audit watcher.** TruthScore is human/Coach-initiated;
  the audit watcher is event-driven. Different triggers, different
  cardinality, different state. No cross-wiring.
- **`coord_propose_file_write`.** TruthScore is read-only against
  truth/ — it never proposes writes. A low Currency score may
  prompt Coach to propose a truth update via the existing flow,
  but that's a Coach decision, not a TruthScore action.

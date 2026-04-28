# Implementation Plan: Codex Runtime Alongside Claude in TeamOfTen

This plan adds OpenAI Codex as a second per-agent runtime sharing the existing task board, memory, messages, worktrees, event log, MCP surface, cost caps, auto-compact, fan-out, and wake logic. The Claude path stays the default; runtime selection is per slot.

> **Revision note (post-review).** Five blockers from the first draft were
> corrected: (1) session state lives on `agent_sessions` (per-project),
> not `agents`; (2) `coord_mcp` is a proxy-to-main-harness, not a direct
> DB writer — `tools.py` publishes through the in-process event bus and
> calls `maybe_wake_agent`, which cannot work cross-process; (3) the
> Python Codex SDK is experimental and not reliably on PyPI — sourcing
> strategy spelled out in §I; (4) cost is token-priced only in API-key
> mode; ChatGPT-auth has no per-turn USD — tracking is split; (5)
> `pyproject.toml` switches from explicit packages to discovery so
> `server/runtimes/` is picked up. Phasing reflows accordingly.

## A. Runtime abstraction extraction (Phase 0 — no behavior change)

### A.1 Goal
Carve the Claude-specific parts of `run_agent` ([server/agents.py:2624](../server/agents.py#L2624)) out into a `ClaudeRuntime` while leaving everything runtime-agnostic in a dispatcher.

### A.2 What stays in the dispatcher
- pause check
- spawn-lock claim, `_autoname_player`, `_check_cost_caps`
- status flip, `agent_started` emit
- system-prompt assembly (`_get_agent_brief`, prior-error suffix, handoff suffix, identity prefix, coordination block)
- outer `try/except` with post-result exception suppression and auto-retry counter

**Auto-compact and prior-session reads move into the runtime.** The
current trip-wire calls `_get_session_id`, `_session_context_estimate`,
and reads Claude's session JSONL files — all Claude-shaped. The
dispatcher cannot run that logic for a Codex agent. The protocol
defining `maybe_auto_compact` (and `run_manual_compact`) is given
once in §A.4 — see that section for the canonical signatures.

The dispatcher calls `runtime.maybe_auto_compact(tc)` before the
main turn; if it returns True, the dispatcher proceeds to run the
user's original prompt on the now-fresh session. Recursion semantics
preserved without the dispatcher caring how each runtime estimates
context.

### A.3 What moves into `ClaudeRuntime.run_turn`
`coord_server` build, allowed-tools assembly, external MCP load, `_build_can_use_tool`, `options_kwargs` (hooks, model, permission_mode, effort, resume), `_prompt_stream`, `_iterate`, stale-session retry. The `turn_ctx` dict stays owned by the dispatcher, passed by reference so the runtime can mutate `got_result`, `accumulated_text`, etc.

### A.4 Protocol
New `server/runtimes/` package with `base.py`, `claude.py`, `codex.py`:

```python
@dataclass
class TurnContext:
    agent_id: str
    project_id: str
    prompt: str
    system_prompt: str
    model: str | None
    plan_mode: bool
    effort: int | None
    compact_mode: bool
    auto_compact: bool
    workspace_cwd: str
    allowed_tools: list[str]
    external_mcp_servers: dict
    turn_ctx: dict  # mutable shared state

# `prior_session` is intentionally NOT on TurnContext: prior-session
# reads live in the runtime (each runtime knows which column to read —
# Claude reads `agent_sessions.session_id`, Codex reads
# `agent_sessions.codex_thread_id`). The dispatcher does not own that
# field.

class AgentRuntime(Protocol):
    name: str

    async def run_turn(self, tc: TurnContext) -> None:
        """Execute one model turn and emit harness events via the bus."""

    async def maybe_auto_compact(self, tc: TurnContext) -> bool:
        """Runtime-specific auto-compact trip-wire. Return True if a
        compact turn was run (so the dispatcher proceeds to run the
        user's original prompt on the now-fresh session). The full
        TurnContext is passed because Claude's current trip-wire runs
        a recursive compact turn with COMPACT_PROMPT, compact_mode=True,
        auto_compact=True — and needs `model`, `system_prompt`,
        `workspace_cwd`, etc. to do so. ClaudeRuntime preserves the
        current Claude-shaped behavior; CodexRuntime returns False
        in v1 (see §E.6)."""

    async def run_manual_compact(self, tc: TurnContext) -> None:
        """Execute a manual /compact request. Receives the full
        TurnContext for the same reason as maybe_auto_compact.
        ClaudeRuntime runs a COMPACT_PROMPT turn; CodexRuntime uses
        native `client.compact_thread(thread_id)` and stores the
        returned summary defensively (see §E.6)."""
```

PR 2 ships exactly this contract; both runtimes implement all three
methods. CodexRuntime's `run_turn` is SDK-bound as of the 2026-04-28
follow-up pass; the remaining caveats are live validation, not stubs.

`HarnessEvent` is **not** a new struct — reuse the existing `_emit(agent_id, type, **payload)` bus vocabulary (`tool_use`, `tool_result`, `text`, `thinking`, `result`, `error`, `agent_started`, `agent_stopped`, `context_applied`, `auto_compact_triggered`, `session_compacted`, `session_resume_failed`, `cost_capped`, `agent_cancelled`, `paused`, `spawn_rejected`). CodexRuntime maps Codex notifications onto **this same vocabulary**.

### A.5 Auto-compact and manual compact interaction

The dispatcher delegates **both** flows to the runtime:

- **Auto-compact trip-wire**: dispatcher calls
  `runtime.maybe_auto_compact(...)` before the main turn. If True,
  dispatcher proceeds to run the user's original prompt on the fresh
  session; if False, dispatcher runs the original prompt directly.
  ClaudeRuntime preserves the existing Claude-shaped trip-wire logic
  (session JSONL probe + threshold check). **CodexRuntime returns
  False in v1** — auto-compact disabled until app-server exposes a
  usable context-pressure signal.
- **Manual `/compact` (or `POST /api/agents/{id}/compact`)**:
  dispatcher calls `runtime.run_manual_compact(...)`. ClaudeRuntime
  runs the existing COMPACT_PROMPT turn. CodexRuntime uses the native
  compact endpoint and clears `codex_thread_id` after storing the
  returned summary.

### A.6 Tests
All ~113 existing pytests pass. New `server/tests/test_runtime_dispatch.py` builds a `FakeRuntime` and asserts dispatcher contract (cost-cap rejects before `run_turn`, `agent_started` before, `agent_stopped` after).

---

## B. DB migration

Per project pattern (commit `c4c4557` rolled migrations into `SCHEMA`), edit `SCHEMA` in [server/db.py](../server/db.py).

**Important — runtime-vs-session split.** The repo now uses per-project
`agent_sessions` for session state (see [server/db.py:244](../server/db.py#L244)
and [server/main.py:707](../server/main.py#L707)). `session_id`,
`continuity_note`, and `last_exchange_json` already live there, scoped by
`(slot, project_id)`. Codex thread IDs MUST go on `agent_sessions`, not
`agents` — otherwise switching projects on the same slot would either
clobber the Codex thread or resume the wrong project's thread.

The `runtime` choice itself is a **slot-level user preference**, not
session state, so it stays on `agents`.

### B.1 Schema additions

On `agents` (slot-level preference — **nullable so role defaults can apply**):
```sql
runtime_override  TEXT NULL
                  CHECK (runtime_override IS NULL
                         OR runtime_override IN ('claude','codex')),
```

Resolution order at spawn time: `agents.runtime_override` (if set) →
role default from `team_config` (`coach_default_runtime` /
`players_default_runtime`) → `'claude'`. Same shape the brief/model
fields already use. NOT NULL with a default would make every row carry
an override at insert time and silently ignore role defaults — fixed
by keeping the column nullable.

On `agent_sessions` (per-project per-slot session state) — **separate
columns per runtime, not a generic field**:
```sql
codex_thread_id  TEXT,        -- Codex thread id; null when no Codex
                              --  thread has been started for this
                              --  (slot, project)
```

The existing `session_id` column stays as the Claude session id. We do
**not** introduce a generic `runtime_session_id`: a single field forces
either runtime tagging or a clear-on-runtime-change rule, and both fail
the symmetric case (slot was Claude, switches to Codex, switches back —
the Claude thread should resume, not be lost). Two columns is the
clearest representation of "each runtime can have its own continuation
state per project."

Switching `runtime_override` on a slot does **not** clear either column.
The runtime selected at spawn time reads its own column; the other
remains dormant and re-attaches if the user switches back.

On `turns`:
```sql
runtime         TEXT NOT NULL DEFAULT 'claude',
cost_basis      TEXT,            -- 'token_priced' | 'plan_included' | NULL
```

`cost_basis` distinguishes API-key Codex turns (token-priced, `cost_usd`
populated) from ChatGPT-auth Codex turns (`cost_usd` is NULL or 0;
report by tokens/messages instead — see §G).

(No CHECK on `turns.runtime` — keep analytics queries safe across future runtimes.)

### B.2 Backfill for existing DBs
`CREATE TABLE IF NOT EXISTS` doesn't add columns. Add an inline migration runner in `init_db()` after `executescript`:

```python
async def _ensure_columns(db, table, cols):
    cur = await db.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in await cur.fetchall()}
    for name, ddl in cols:
        if name not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
```

`ALTER TABLE … ADD COLUMN … NOT NULL DEFAULT 'claude'` populates existing rows with the default — no explicit UPDATE. CHECK constraint can't be added retroactively without table rebuild — rely on `PUT /api/agents/{id}/runtime` validation instead.

---

## C. Coord MCP — proxy to main harness (revised)

### C.1 Why direct-DB-from-subprocess is wrong

The first draft proposed letting `coord_mcp` open its own `aiosqlite`
handle and write directly. That breaks more than the single-write-handle
invariant. `tools.py` does three things every coord call:

1. **DB write** (the obvious part).
2. **`bus.publish(...)` on the in-process event bus** ([server/events.py:235](../server/events.py#L235)) — feeds the WebSocket out to the UI and the Telegram bridge subscriber.
3. **Cross-tool calls into `server.agents`**: `maybe_wake_agent`, `_deliver_system_message`, fan-out logic ([server/tools.py:78](../server/tools.py#L78)).

A subprocess can do (1) but emits (2) and (3) into a void. Symptoms
would be: agent A sends a message, row appears in `messages` table,
but the WebSocket never fires, the recipient never wakes, the UI
shows nothing until the next page reload.

### C.2 Design — thin stdio proxy

`coord_mcp` becomes a stdio MCP server that **forwards every tool call to
the main FastAPI process** over a loopback channel. The main process
keeps owning DB writes, event-bus publishing, and wake logic.

Two viable transports for the proxy hop:

- **(a) HTTP loopback** — `POST http://127.0.0.1:$PORT/api/_coord/{tool}`
  with the agent's `caller_id` and JSON args. Auth: a per-spawn
  short-lived token issued when the harness starts the MCP subprocess
  (env var, not on disk). The `/api/_coord/*` namespace is loopback-only
  (bind check) and never reachable from outside the container.
- **(b) Unix domain socket** — same payloads, lower latency, no port
  contention. Doesn't work on Windows dev hosts, which we use.

**Pick (a).** Cross-platform, reuses FastAPI's existing routing/auth
middleware, debuggable with curl. ~1ms overhead per coord call is
irrelevant compared to model-turn latency.

### C.3 New module `server/coord_mcp.py`

Executable: `python -m server.coord_mcp --caller-id p3 --proxy-url http://127.0.0.1:8000`.
The proxy token is passed via the **environment variable
`HARNESS_COORD_PROXY_TOKEN`**, never on the command line — argv is
visible in `ps`/`/proc/<pid>/cmdline` to any process on the host, and
the harness must not leak its own auth secret there. The subprocess
reads the env var at startup.

Body is a stdio MCP server that:

- Statically declares the coord tool list (names, schemas) — no DB
  access, just a hardcoded mirror of what `build_coord_server` registers
  today. Schema drift between the proxy and the real handlers is caught
  by a contract test (§J).
- On each tool invocation, POSTs to `${proxy_url}/api/_coord/{tool_name}`
  with `{caller_id, args}` and `Authorization: Bearer ${proxy_token}`.
- Streams the response back as the MCP tool result.

### C.4 New endpoint family `POST /api/_coord/{tool}`

Internal-only (loopback bind check + token gate). Dispatches to the
**existing** in-process tool handlers extracted from
`build_coord_server` so there is exactly one implementation. The
existing `bus.publish` and `maybe_wake_agent` calls inside those
handlers run in the main process where they belong.

**Auth model — caller identity comes from the token, NOT the body.**
When the dispatcher spawns a Codex turn, it generates a per-spawn
token, **records it server-side as `(token → caller_id)`**, and passes
the token to the subprocess via env. The endpoint:

1. Looks up the bearer token in the spawn-token table.
2. Reads `caller_id` from the token record. The body's `caller_id`
   field, if present, is a sanity check only — mismatch → 403.
3. Token is single-use-bound-to-slot for the duration of the turn;
   revoked when the turn ends (success, error, or cancel).

Without server-side identity binding, a compromised proxy or any
process that learns the token could forge requests as any caller_id —
agent X could send messages as Coach. The token-to-identity map closes
that hole. Tokens live in a small in-memory dict keyed by token →
`{caller_id, expires_at}`, no DB write needed (lifetime ≤ one turn).

### C.5 ClaudeRuntime continues working

ClaudeRuntime can keep using the in-process `create_sdk_mcp_server`
wrapper (zero overhead) OR switch to the same subprocess proxy as
Codex for symmetry. **Recommend keeping in-process for Claude** to
minimize blast radius of PR 3 — the subprocess proxy only ships
because Codex needs it. Two paths, same handlers.

```python
# ClaudeRuntime mcp_servers (unchanged from today):
mcp_servers = {"coord": coord_server, **external_servers}
```

```python
# CodexRuntime mcp_servers config (TOML/JSON via start_thread):
{
  "coord": {
    "command": sys.executable,
    "args": ["-m", "server.coord_mcp",
             "--caller-id", agent_id,
             "--proxy-url", "http://127.0.0.1:8000"],
    "env": {"HARNESS_COORD_PROXY_TOKEN": spawn_token}
  }
}
```

AskUserQuestion stays in-process via Claude's `can_use_tool` interception. For Codex, AskUserQuestion needs a different path entirely — see §E.8.

---

## D. Auth — headless Codex login

### D.1 Spike (PR 1)
Before any runtime code:
1. SSH into Zeabur with `CODEX_HOME=/data/codex`. Run `codex login`.
2. Confirm credentials persist at `/data/codex/auth.json` (or version-specific filename).
3. Redeploy and verify auth survives.
4. Document exact filenames in CLAUDE.md gotchas.

### D.2 Dockerfile
```dockerfile
ENV CLAUDE_CONFIG_DIR=/data/claude \
    CODEX_HOME=/data/codex
...
RUN npm install -g @anthropic-ai/claude-code @openai/codex
```
(Confirm exact npm package name during spike.)

### D.3 `/api/health` extension
Mirror the existing `claude_auth.credentials_present` probe: read `${CODEX_HOME}/auth.json` for nonzero size, surface `codex_auth.credentials_present` and `codex_auth.method: "chatgpt" | "api_key" | "none"`.

### D.4 API-key fallback
Stored in `secrets` table under `openai_api_key`, encrypted via existing Fernet. Resolution precedence in CodexRuntime:
1. ChatGPT session present → let Codex CLI use it natively.
2. Else `secrets.openai_api_key` set → inject `OPENAI_API_KEY` into subprocess env.
3. Else: emit `human_attention` and abort spawn.

### D.5 Options drawer UI
New "Codex auth" section mirroring Telegram pattern: read-only ChatGPT-session badge, write-only masked API-key field, "Test" button (`POST /api/team/codex/test`). Endpoints `GET/PUT/DELETE /api/team/codex` mirror Telegram's `codex_disabled` flag handling.

---

## E. CodexRuntime implementation

### E.1 Lifecycle

**One `CodexClient` instance per slot**, cached in module-level
`_codex_clients: dict[str, CodexClient]`. Constructed via
`CodexClient.connect_stdio(command=["codex", "app-server"], cwd=...,
env=...)` which spawns the `codex app-server` subprocess. After
construction call `await client.start()` then `await client.initialize()`.
The `_SPAWN_LOCK` already enforces sequential turns per slot,
satisfying the SDK's "one active turn consumer per client" constraint.
Close (`await client.close()`) and re-open on auth-error / transport
error.

### E.2 Thread start vs resume

Real signatures (confirmed live, see Docs/CODEX_PROBE_OUTPUT.md):

```python
client.start_thread(config: ThreadConfig | None = None) -> ThreadHandle
client.resume_thread(thread_id: str, *,
                     overrides: ThreadConfig | None = None) -> ThreadHandle
# ThreadHandle exposes:
thread.thread_id          # str, accessible immediately after start
thread.chat(text, ...)    # AsyncIterator[ConversationStep]
thread.chat_once(text, ...) -> ChatResult   # non-streaming
thread.compact()          # native compact (returns Any)
```

- On entry: read `agent_sessions.codex_thread_id` for `(slot, project_id)`.
- If null → `client.start_thread(config)`. `thread.thread_id` is available
  immediately; persist it on first successful `chat()` step.
- If set → `client.resume_thread(thread_id, overrides=cfg)`. On failure
  (CodexProtocolError, "thread not found", etc.), mirror Claude's
  stale-session auto-heal: emit `session_resume_failed`, null
  `codex_thread_id`, retry once with `start_thread`.

### E.3 ConversationStep → harness event mapping

`thread.chat(text)` yields `ConversationStep` objects. Each step has:
`thread_id`, `turn_id`, `item_id`, `step_type`, `item_type`, `status`,
`text` (set for final agent answer; None otherwise), and a `data` dict
holding the raw `params.item` payload.

Confirmed step types from the live spike (minimal turn):
- `step_type='userMessage'` — echo of the prompt; skip (already in DB).
- `step_type='codex', item_type='agentMessage', text=<answer>` — the
  model's reply, with `data['params']['item']['phase']='final_answer'`.

Observed and implemented item_type mapping:

| `step_type` / `item_type`                          | Harness event              | Notes |
|----------------------------------------------------|----------------------------|-------|
| `userMessage` / `userMessage`                      | (skip)                     | already in DB |
| `codex` / `agentMessage` (text set)                | `text`                     | accumulate; phase=='final_answer' marks turn done |
| `thinking` / `reasoning`                           | `thinking`                 | parallel to Claude path |
| `exec` / `commandExecution`                        | `tool_use` (`tool=Bash`) + optional `tool_result` | SDK 0.3.2 normalized shell shape |
| `codex` / `shell`                                  | `tool_use` (`tool=Bash`)   | draft compatibility |
| `file` / `fileChange`                              | `tool_use` (`tool=Edit`) + optional `tool_result` | SDK 0.3.2 normalized file-change shape |
| `codex` / `apply_patch`                            | `tool_use` (`tool=Edit`)   | draft compatibility; unified diff feeds diff@7 |
| `codex` / `web_search` or `webSearch`              | `tool_use` (`tool=WebSearch`) | reuse WebSearch card |
| `tool` / `mcpToolCall` or `mcp_tool_call`          | `tool_use` (`tool=mcp__...`) | coord_* + external MCPs |
| stream exhaustion                                  | `result`                   | usage is read best-effort from `thread.read(include_turns=True)` |
| `CodexTurnInactiveError` raised mid-iteration      | `error` (pre-result) → retry counter |
| `CodexTimeoutError` / `CodexTransportError`        | `error` (pre-result) → retry counter; close + reopen client |
| `CodexProtocolError`                               | `error`; if "thread not found" trigger stale-session retry |
| `ApprovalRequest` via `set_approval_handler`       | `human_attention`, then decline (§E.8 option b) |
| `thread.compact()` return                          | `session_compacted` (§E.6) |

`ChatResult` in `codex-app-server-sdk` 0.3.2 does not expose usage
directly, so the runtime reads the thread after stream exhaustion and
extracts the matching turn's `usage` defensively. Missing usage records
zeros rather than failing the turn.

### E.4 Tool execution
Codex executes native tools inside the codex-app-server subprocess — we
observe completed items via notifications. `coord_*` MCP tools route
through the stdio MCP proxy with the same token-bound
`--caller-id <agent_id>` discipline as the Claude in-process handler.

### E.5 Cost / pricing
Codex's `Turn.usage` reports tokens but NOT USD. New module `server/pricing.py`:

```python
CODEX_PRICING = {
    "gpt-5.4":      {"input": 5.0,  "cached": 0.50, "output": 15.0},
    "gpt-5.4-mini": {"input": 0.25, "cached": 0.025, "output": 1.0},
}
def codex_cost_usd(model: str, usage: Mapping[str, int]) -> float: ...
```

Split `_extract_usage` into `_extract_usage_claude` and `_extract_usage_codex`. CodexRuntime passes the usage block it reads from thread state. Map Codex's single `cached_input_tokens` → `cache_read_tokens`, write `0` to `cache_creation_tokens` (Codex caching has no separate creation cost). `_insert_turn_row` accepts a new `runtime` arg.

### E.6 Compact

Native compact is available on the SDK: `ThreadHandle.compact() -> Any`
(also `client.compact_thread(thread_id) -> Any`). Return shape is
SDK-internal `Any` — the implementation must treat it as opaque and
extract a summary string defensively (try common shapes: dict with
`summary` / `text` / `result` keys, or fall back to repr).

Sketch:

```python
if tc.compact_mode:
    raw = await thread.compact()
    summary = (
        raw.get("summary") if isinstance(raw, dict)
        else getattr(raw, "summary", None)
        or getattr(raw, "text", None)
        or str(raw)
    )
    await _set_continuity_note(slot, project_id, summary)
    await _clear_codex_thread_id(slot, project_id)
    await _emit(tc.agent_id, "session_compacted")
    tc.turn_ctx["got_result"] = True
    return
```

**Auto-compact for Codex agents is disabled in v1** (see §A.2 —
`CodexRuntime.maybe_auto_compact` returns False until app-server
exposes a usable context-pressure signal). Manual `/compact` still
works via the path above.

### E.7 Error handling
- `turn.failed` pre-result → `_emit("error")`, increment `_consecutive_errors`, schedule retry.
- Exception post-result → suppressed by dispatcher's existing `got_result` discipline.
- 401/auth errors → emit `human_attention`, stop retrying (Telegram bridge precedent).

### E.8 AskUserQuestion under Codex

ClaudeRuntime intercepts `AskUserQuestion` via `can_use_tool` ([server/agents.py:_build_can_use_tool](../server/agents.py)). Codex has no equivalent hook — it executes its own native tools internally. Two options:

- (a) Re-expose AskUserQuestion as a coord_* MCP tool (`coord_ask_user`).
  Codex calls it like any other tool; the main process renders the
  question in the QuestionForm UI, blocks until answered, returns
  the answer string. Symmetric: Claude could optionally also use this
  path, removing the dual mechanism.
- (b) Skip AskUserQuestion on Codex agents. Players currently use it
  rarely; Coach uses it more. Acceptable degradation for v1.

v1 ships option (b). The runtime sets `approval_policy='never'`; if the
SDK still emits an approval side-channel request, TeamOfTen surfaces a
`human_attention` event and declines it so the turn does not hang behind
an invisible prompt. A future `coord_ask_user` MCP tool can replace this
degradation if Codex agents prove to need synchronous user questions.

---

## F. UI changes

### F.1 Pane settings popover
Runtime radio (Claude | Codex). New `PUT /api/agents/{id}/runtime` mirroring `/brief` and `/model` patterns. Audit-logged via `audit_actor(request)`. Mid-turn change → 409 Conflict.

### F.2 LeftRail runtime badge
CSS-icon only (invariant #6). `.slot-runtime-claude::after` = filled disc; `.slot-runtime-codex::after` = filled square. Bottom-left of slot button (lock badge already owns bottom-right). Distinct hue per runtime.

### F.3 Options drawer defaults
New section "Default runtime per role": Coach radio + Players radio. `team_config` keys `coach_default_runtime`, `players_default_runtime`. Resolution: per-agent > role default > `'claude'`. Add second row of model dropdowns gated by runtime; extend `_get_role_default_model` to read both.

### F.4 Codex tool renderers (`server/static/tools.js`)
- `shell` → reuse Bash card.
- `apply_patch` → adapter that feeds unified-diff straight into `diff@7` and reuses Edit diff card layout.
- `web_search` → reuse WebSearch card.
- `view_image` / file reads → reuse Read renderer.
- Else → generic JSON renderer.

### F.5 `agent_started` payload
Add `runtime=runtime_name`. UI renders a small chip in the sticky turn header.

---

## G. Cost caps — split by auth mode

**Important — Codex pricing is bimodal.** Per OpenAI docs
(<https://developers.openai.com/codex/pricing>):

- **ChatGPT-auth Codex**: included in the user's ChatGPT plan; no
  per-turn USD. Usage limited by plan-level quotas (messages/week,
  reasoning quota) which the SDK does not surface as USD.
- **API-key Codex**: standard token-priced API rates (input + output +
  cached input). Per-turn USD computable from `Turn.usage` × pricing
  table.

Treating both as `cost_usd` was a mistake in the first draft.

### G.1 What `turns` records

For each Codex turn, set `cost_basis`:

- `'token_priced'` → API-key mode; `cost_usd` populated from
  `pricing.codex_cost_usd(model, usage)`.
- `'plan_included'` → ChatGPT auth; `cost_usd = 0`,
  `input_tokens` / `output_tokens` populated for visibility.

Claude turns get `cost_basis = 'token_priced'` (Max-OAuth is
plan-included from a billing standpoint, but `ResultMessage.total_cost_usd`
still reports a meaningful number based on Anthropic's posted rates,
so we keep treating it as priced).

### G.2 Cap enforcement

Existing `_check_cost_caps` keeps summing `cost_usd` and rejects when
the per-agent or team daily cap is exceeded. ChatGPT-auth Codex turns
contribute `0`, so they don't trip the USD cap at all — that's correct.
ChatGPT plan limits are enforced server-side by OpenAI; we surface the
401/429 response as `human_attention` (§E.7) when the user hits them.

### G.3 New visibility — usage pressure for plan_included turns

`/api/turns/summary` returns `by_runtime` and `by_cost_basis` breakdowns
plus a `plan_included_token_total` for ChatGPT-auth Codex usage. UI
shows two side-by-side meters in EnvPane: "Spent today: $X.XX" and
"Plan-included tokens today: N". Don't pretend a USD value exists for
plan-included usage — show what is actually true.

### G.4 No new env caps

Defer `HARNESS_CODEX_DAILY_USD` and `HARNESS_CHATGPT_TOKENS_DAILY`. The
existing combined USD cap + visible token meter is enough for v1.

---

## H. Coach prompting

`_build_coach_coordination_block` already has a "Roster availability" lock-status block (lock feature precedent). Add sibling "Roster runtimes" sub-section, emitted only when team is mixed:

```
### Roster runtimes
p3, p7 run on Codex (OpenAI) — their tools differ:
they have `shell`, `apply_patch`, `web_search` instead of
Bash, Edit, WebSearch. coord_* is identical on both.
```

Skip block when all 10 Players are on the same runtime — saves tokens on default deploy.

---

## I. Dependencies & packaging

### I.1 Codex Python SDK sourcing

The official OpenAI docs
(<https://developers.openai.com/codex/sdk>, lines 620-647) describe the
Python SDK as **experimental and installed from a local checkout** of
the open-source `openai/codex` repo via editable install from
`sdk/python`. It is **not safe to assume a stable
`codex-app-server-sdk>=0.1` is on PyPI** the way the first draft did.
The public example uses `thread_start(...); thread.run(...)`; some
method names referenced earlier in this spec (`start_thread`,
`resume_thread`, `client.start_thread(...)`) need to be reconciled
against the actual installed signature during the PR 1 spike.

Practical sourcing options, in order of preference:

- **(a) Vendor a pinned commit** — git submodule or vendored copy of
  `openai/codex/sdk/python` at a known SHA, installed via
  `pip install ./vendor/codex-sdk` in the Dockerfile. Most reproducible.
  Update procedure documented alongside `scripts/vendor_deps.py`.
- **(b) Direct VCS dependency** — `pip install
  "codex-app-server-sdk @ git+https://github.com/openai/codex@<SHA>#subdirectory=sdk/python"`.
  Cleaner pyproject.toml; relies on GitHub at every container build.
- **(c) Talk to `codex app-server` over JSON-RPC ourselves** — drop the
  SDK entirely, write a small async JSON-RPC client. ~300 lines.
  No dependency on an experimental package; full control. Worth it
  if (a)/(b) prove fragile.

**Recommendation: start with (b) for PR 1 spike, switch to (a) for
PR 4 if the SDK proves stable enough, fall back to (c) only if the
SDK churns badly between Codex CLI releases.**

The PR 1 spike must (i) confirm the install path works in the Docker
build, (ii) print the actual installed `AsyncCodex` method signatures,
(iii) update §E.2 method names to match.

### I.2 Other Python deps (`pyproject.toml`)

**Implementation note (errata — diverges from this section):** the
`coord_mcp` proxy in `server/coord_mcp.py` was implemented as
hand-rolled JSON-RPC 2.0 over stdio rather than using the `mcp`
package. The proxy only needs `initialize`, `tools/list`,
`tools/call`, and `ping` — about 200 lines of dispatcher. Pulling
in `mcp>=1.0` (which is large, brings its own pydantic schemas,
and pins MCP protocol versions) wasn't justified. Add the dep
later if `coord_mcp` ever needs richer MCP features (resources,
prompts, sampling).

The pyproject deps stay minimal:

```toml
# codex SDK added via git source URL or vendored path (see I.1)
# (no `mcp` dep — see note above)
```

### I.3 Package discovery

The current [pyproject.toml:28](../pyproject.toml#L28) declares
`packages = ["server"]` explicitly. Adding `server/runtimes/` as a
subpackage works under most build backends because `find_packages`
recurses, but the explicit list is brittle — if it's setuptools'
`packages = [...]` literally (no `find_packages`), `server.runtimes`
silently won't ship in the wheel and imports will break in any
non-source-tree install.

Switch to package discovery:

```toml
[tool.setuptools.packages.find]
where = ["."]
include = ["server*"]
```

(Or hatchling/poetry equivalent, depending on the build backend in
use.) Verify by `pip install .` into a clean venv and `python -c
"import server.runtimes"`.

### I.4 Dockerfile

ENV `CODEX_HOME=/data/codex`. npm install Codex CLI (exact package
name confirmed in spike — likely `@openai/codex`). Smoke test
`codex app-server --help` in the build to fail fast on install
breakage.

### I.5 `requirements.txt`

Not used in this repo (pyproject is the sole source). No change.

---

## J. Testing

Existing pattern: DB-level pytest, no FastAPI TestClient. New tests:
- `test_runtime_dispatch.py` — fake runtime + dispatcher contract.
- `test_db_schema_migration.py` — open old-schema DB, run `init_db()`, assert columns + defaults.
- `test_codex_pricing.py` — table of `(model, usage, expected_usd)` cases + unknown-model fallback.
- `test_codex_event_normalization.py` — fake stream of Codex notifications, assert `_emit` calls match Claude vocabulary.
- `test_compact_path_routing.py` — Claude branch is fixed: a manual
  `/compact` against a Claude agent runs a `COMPACT_PROMPT` turn
  exactly once. **Codex branch is conditional on the PR 1 SDK
  spike result**: if the spike confirms a native compact call exists
  on the SDK, assert it is invoked once; if the spike concludes
  CodexRuntime falls back to a `COMPACT_PROMPT` turn against Codex,
  assert that path instead. The test file ships in PR 5 with the
  spike-confirmed assertion — not before.
- `test_cost_cap_aggregation.py` — mixed-runtime rows in `turns`, assert combined sum and rejection.
- `test_coord_mcp_proxy.py` — spin up the FastAPI ASGI app in-process
  with a temp DB; issue a per-spawn token bound to `caller_id='p1'`;
  spawn `python -m server.coord_mcp --proxy-url <uvicorn loopback>
  with `HARNESS_COORD_PROXY_TOKEN=<token>` in the env; send
  `tools/list` over stdio (assert tool
  catalog matches the in-process registry); call `coord_send_message`
  through the proxy. Assert (i) the row appears in `messages`,
  (ii) a `message_sent` event is published on the **main process'**
  bus (subscribe before the call), (iii) `maybe_wake_agent` was
  invoked for the recipient. Negative test: send a request with a
  body `caller_id` that mismatches the token's bound identity →
  expect 403.

---

## K. Phased rollout — revised PR boundaries

**PR 1 — Codex install/auth spike (no runtime code).** completed and audited
- Dockerfile: install Codex CLI, `CODEX_HOME=/data/codex`.
- Source the Python SDK per §I.1 option (b); vendor a Codex repo
  commit; smoke test `codex app-server --help` and a minimal
  `AsyncCodex().thread_start(...).run("hi")` against the running
  app-server.
- Verify headless `codex login` (ChatGPT auth) works and persists
  across redeploy.
- Print actual `AsyncCodex` method signatures; update §E method
  names in this doc.
- Extend `/api/health` with `codex_auth.*`.
- Packaging: switch `pyproject.toml` to discovery (§I.3).

**PR 2 — Runtime abstraction, Claude-only.** completed and audited
- `server/runtimes/` package, `AgentRuntime`, `ClaudeRuntime` carved
  out of `run_agent`. Zero behavior change.
- New `test_runtime_dispatch.py`. All ~113 existing tests green.
- Manual smoke: Coach + 2 Players, code-touching turn, /compact,
  mid-turn cancel, project switch.

**PR 3 — DB migration on `agent_sessions`.** completed and audited
- Add `agents.runtime_override` (nullable), `agent_sessions.codex_thread_id`,
  `turns.runtime`, `turns.cost_basis` (§B).
- `_ensure_columns` migration runner.
- New `PUT /api/agents/{id}/runtime` endpoint with audit logging.
- No Codex code yet — this PR is just schema + the slot-level
  preference plumbed through, defaulting to `'claude'` everywhere.

**PR 4 — Coord MCP proxy.** completed and audited
- `server/coord_mcp.py` as the stdio→HTTP proxy (§C.2–C.4).
- `POST /api/_coord/{tool}` internal endpoints (loopback bind +
  per-spawn token).
- ClaudeRuntime keeps in-process MCP (no migration risk for the
  default path); the proxy ships dormant until PR 5 wires Codex.
- Contract test: enumerate the proxy tool list and assert it
  matches `build_coord_server`'s registered tools.

**PR 5 — CodexRuntime behind a feature flag, one-slot pilot.** completed and audited
- `server/runtimes/codex.py`, `server/pricing.py`.
- `HARNESS_CODEX_ENABLED=true` env gate; `PUT /api/agents/{id}/runtime`
  rejects `codex` when the flag is unset.
- Runtime path: resolve ChatGPT/API-key auth, open cached
  `CodexClient`, start/resume the per-slot thread, attach coord MCP
  proxy + external MCP servers, stream notifications into harness
  events, persist `codex_thread_id`, and insert `turns` rows with
  runtime/cost-basis metadata.
- Manual compact uses native `compact_thread`; auto-compact stays off
  until app-server exposes context-pressure telemetry.
- AskUserQuestion path decided per §E.8: v1 degrades and declines
  side-channel approval requests after surfacing `human_attention`.
- Cost-basis split working (§G); EnvPane shows both meters.

**PR 6 — UI polish, renderers, mixed-team prompting.** completed and audited
- LeftRail runtime badge.
- Pane settings popover Runtime radio.
- Options drawer per-role default + per-runtime model dropdowns.
- `apply_patch` / `shell` / `web_search` renderers.
- Coach "Roster runtimes" block.
- CLAUDE.md updates (invariants clarification, Codex section, gotchas).
- Drop the `HARNESS_CODEX_ENABLED` flag once stable.

Each PR independently revertable. PR 1 is the gate — if the SDK
sourcing or headless login spike fails, the rest deserves a redesign
before continuing.

---

## L. Risks / unknowns to validate

1. **Headless `codex login` viability.** Highest risk. PR 0 spike. If device-code can't complete in non-TTY container shell, fall back to API-key-only Codex.
2. **SDK "one active turn consumer per client" limit.** Validate with 5-turn loop, confirm no notification cross-talk.
3. **Coord MCP under both runtimes.** Codex's MCP config shape may differ — verify standalone smoke test from both runtimes before integrating.
4. **`Turn.usage` shape.** Confirm `cached_input_tokens` is a single field; some early SDK versions returned `usage=None` on streamed turns.
5. **`thread_resume` config matching.** If Codex requires original model/sandbox to match on resume, mid-session model swap invalidates resume. Mitigation: null `codex_thread_id` on detected model change.

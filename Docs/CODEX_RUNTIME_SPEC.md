# Codex Runtime — Specification

> **Subordinate to `Docs/TOT-specs.md`.** When this doc and TOT-specs
> disagree, TOT-specs wins. This file is the source of truth for
> Codex-specific behavior — runtime lifecycle, thread/resume semantics,
> rollout JSONL parsing, cost basis, MCP proxy details, error handling
> — but cannot redefine fields, endpoints, events, or invariants that
> TOT-specs declares.

Status: shipped. TOT-specs assumes the Claude runtime everywhere it
describes runtime behavior; Codex is the alternate runtime and its
specifics live here. Per-agent runtime selection is a TOT-level
concept (slot-level `runtime_override`, role default in `team_config`,
`HARNESS_CODEX_ENABLED` gate). Everything below describes how
CodexRuntime implements its half of the `AgentRuntime` contract.

OpenAI Codex shares the existing task board, memory, messages,
worktrees, event log, MCP surface, cost caps, fan-out, and wake logic
with Claude. Auto-compact runs on both runtimes — Claude via a
`COMPACT_PROMPT` turn, Codex via the native `client.compact_thread`
endpoint (see §E.6). Runtime selection is per-slot.

## A. Runtime abstraction

### A.1 Shape
The runtime layer separates Claude-specific code into `ClaudeRuntime`
and Codex-specific code into `CodexRuntime`, both behind the
`AgentRuntime` protocol. The dispatcher (`run_agent`) is
runtime-agnostic.

### A.2 Dispatcher responsibilities
- pause check
- spawn-lock claim, `_autoname_player`, `_check_cost_caps`
- status flip, `agent_started` emit
- system-prompt assembly (`_get_agent_brief`, prior-error suffix,
  handoff suffix, identity prefix, coordination block)
- outer `try/except` with post-result exception suppression and
  auto-retry counter

Auto-compact and prior-session reads belong to the runtime, not the
dispatcher: Claude's trip-wire reads `agent_sessions.session_id` plus
session JSONL files, Codex's reads `agent_sessions.codex_thread_id`
plus the latest `turns` row for that thread (via
`_codex_session_context_estimate`). The dispatcher calls
`runtime.maybe_auto_compact(tc)` before the main turn; if it returns
True, the dispatcher proceeds to run the user's original prompt on
the now-fresh session.

### A.3 ClaudeRuntime responsibilities
`coord_server` build, allowed-tools assembly, external MCP load,
`_build_can_use_tool`, `options_kwargs` (hooks, model,
permission_mode, effort, resume), `_prompt_stream`, `_iterate`,
stale-session retry. The `turn_ctx` dict is owned by the dispatcher
and passed by reference so the runtime can mutate `got_result`,
`accumulated_text`, etc. CodexRuntime owns the equivalent surface
for the Codex SDK (see §E).

### A.4 Protocol
The `server/runtimes/` package contains `base.py`, `claude.py`, `codex.py`:

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
        compact was actually executed (so the dispatcher proceeds to
        run the user's original prompt on the now-fresh session). The
        full TurnContext is passed because Claude's trip-wire runs a
        recursive compact turn with COMPACT_PROMPT, compact_mode=True,
        auto_compact=True — and needs `model`, `system_prompt`,
        `workspace_cwd`, etc. to do so. Codex's trip-wire instead
        delegates to its own `run_manual_compact` (native
        `client.compact_thread`) when used/window ≥ threshold (see
        §E.6)."""

    async def run_manual_compact(self, tc: TurnContext) -> None:
        """Execute a manual /compact request. Receives the full
        TurnContext for the same reason as maybe_auto_compact.
        ClaudeRuntime runs a COMPACT_PROMPT turn; CodexRuntime uses
        native `client.compact_thread(thread_id)` and stores the
        returned summary defensively (see §E.6)."""
```

Both runtimes implement all three methods.

`HarnessEvent` is **not** a separate struct — runtimes use the existing
`_emit(agent_id, type, **payload)` bus vocabulary (`tool_use`,
`tool_result`, `text`, `thinking`, `result`, `error`, `agent_started`,
`agent_stopped`, `context_applied`, `auto_compact_triggered`,
`session_compacted`, `session_resume_failed`, `cost_capped`,
`agent_cancelled`, `paused`, `spawn_rejected`). CodexRuntime maps
Codex notifications onto **this same vocabulary**.

### A.5 Auto-compact and manual compact interaction

The dispatcher delegates **both** flows to the runtime:

- **Auto-compact trip-wire**: dispatcher calls
  `runtime.maybe_auto_compact(...)` before the main turn. If True,
  dispatcher proceeds to run the user's original prompt on the fresh
  session; if False, dispatcher runs the original prompt directly.
  Both runtimes share the `HARNESS_AUTO_COMPACT_THRESHOLD` env (default
  0.7). ClaudeRuntime uses the Claude-shaped trip-wire (session JSONL
  probe + threshold check, then a `COMPACT_PROMPT` turn).
  CodexRuntime reads `_codex_session_context_estimate(thread_id)` —
  the latest `turns` row for the resumed Codex thread, summed as
  `input + cache_read + cache_creation + output` — and when the ratio
  trips, delegates to its own `run_manual_compact` so the compact uses
  the native `client.compact_thread(thread_id)` endpoint (cheaper than
  a full LLM round-trip). On failure both runtimes emit
  `auto_compact_failed` and the dispatcher proceeds with the original
  session.
- **Manual `/compact` (or `POST /api/agents/{id}/compact`)**:
  dispatcher calls `runtime.run_manual_compact(...)`. ClaudeRuntime
  runs the existing COMPACT_PROMPT turn. CodexRuntime uses the native
  compact endpoint and clears `codex_thread_id` after storing the
  returned summary.

### A.6 Tests
`server/tests/test_runtime_dispatch.py` builds a `FakeRuntime` and
asserts dispatcher contract (cost-cap rejects before `run_turn`,
`agent_started` before, `agent_stopped` after).

---

## B. Schema additions

The columns themselves are documented in TOT-specs §6.2 and §6.4 (the
`agents` and `agent_sessions` tables). This section covers the
rationale for the column shape, which is Codex-specific.

**Runtime-vs-session split.** Session state lives on per-project
`agent_sessions` ([server/db.py](../server/db.py)). `session_id`,
`continuity_note`, and `last_exchange_json` are scoped by
`(slot, project_id)`. Codex thread IDs go on `agent_sessions`, not
`agents` — switching projects on the same slot would otherwise
clobber the Codex thread or resume the wrong project's thread.

The `runtime` choice itself is a **slot-level user preference**, not
session state, so it stays on `agents` as `runtime_override`.

### B.1 Column rationale

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
`CREATE TABLE IF NOT EXISTS` doesn't add columns, so `init_db()` runs
an inline migration via `_ensure_columns(db, table, cols)` after
`executescript`. `ALTER TABLE … ADD COLUMN … NOT NULL DEFAULT 'claude'`
populates existing rows with the default. CHECK constraints can't be
added retroactively without a table rebuild — `PUT
/api/agents/{id}/runtime` validates instead.

---

## C. Coord MCP — proxy to main harness

### C.1 Why direct-DB-from-subprocess is wrong

`coord_mcp` runs in a subprocess (Codex's `app-server` cannot host
in-process Python MCP servers the way Claude's SDK can). It cannot
open its own `aiosqlite` handle and write directly — that breaks more
than the single-write-handle invariant. `tools.py` does three things
on every coord call:

1. **DB write** (the obvious part).
2. **`bus.publish(...)` on the in-process event bus** ([server/events.py:235](../server/events.py#L235)) — feeds the WebSocket out to the UI and the Telegram bridge subscriber.
3. **Cross-tool calls into `server.agents`**: `maybe_wake_agent`, `_deliver_system_message`, fan-out logic ([server/tools.py:78](../server/tools.py#L78)).

A subprocess can do (1) but emits (2) and (3) into a void. Symptoms
would be: agent A sends a message, row appears in `messages` table,
but the WebSocket never fires, the recipient never wakes, the UI
shows nothing until the next page reload. So `coord_mcp` is a thin
stdio proxy that forwards every tool call back to the main process.

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

- Uses the official `mcp` Python stdio server transport. The proxy must
  respond to the standard MCP initialize, tools/list, and tools/call
  flow so Codex app-server can register coord_* tools reliably across
  Linux deployment and Windows development hosts.
- Codex runtime sets the coord MCP subprocess `cwd` and prepends the
  harness root to `PYTHONPATH`, because Codex turns execute inside the
  agent workspace (`/workspaces/<slot>/project`) while the proxy module
  lives in the TeamOfTen server package.
- Statically declares the coord tool list (names, permissive schemas)
  fetched from the loopback catalog. Handler drift between the proxy
  catalog and the real handlers is caught by contract tests (§J).
- On each tool invocation, POSTs to `${proxy_url}/api/_coord/{tool_name}`
  with `{caller_id, args}` and `Authorization: Bearer ${proxy_token}`.
- Streams the response back as the MCP tool result. FastAPI HTTP errors
  must preserve their `detail`/`error`/`message` text in the MCP error
  (`HTTP 403: ...`, not a generic "unknown coord proxy error"), and
  in-process coord handler envelopes with `isError: true` must become
  MCP tool errors.

### C.4 New endpoint family `POST /api/_coord/{tool}`

Internal-only (loopback bind check + token gate). Dispatches to the
**existing** in-process tool handlers extracted from
`build_coord_server` so there is exactly one implementation. The
existing `bus.publish` and `maybe_wake_agent` calls inside those
handlers run in the main process where they belong.

**Auth model — caller identity comes from the token, NOT the body.**
When `CodexRuntime.get_client` first spawns the codex app-server
subprocess for a slot, it generates a token, **records it server-side
as `(token → caller_id)`**, and passes the token to the subprocess via
env. The endpoint:

1. Looks up the bearer token in the spawn-token table.
2. Reads `caller_id` from the token record. The body's `caller_id`
   field, if present, is a sanity check only — mismatch → 403.
3. Token is bound to the slot for the lifetime of the cached
   `CodexClient` (i.e. the running codex app-server subprocess).
   Revoked in `close_client` — on auth-error / transport-error
   teardown, harness shutdown, manual session-clear, or handshake
   failure during initial spawn.

**Why client-lifetime, not turn-lifetime.** The codex app-server
subprocess is cached per slot across many turns. The env it inherits —
including `HARNESS_COORD_PROXY_TOKEN` — is captured exactly once, at
spawn. A per-turn mint+revoke would invalidate the live subprocess's
token after turn 1, so every subsequent turn would 401 on `coord_*`
calls (observed live with Coach in Codex mode, 2026-04-29). Tying the
token's lifetime to the subprocess fixes that without weakening the
identity binding: when the subprocess dies, so does the token.

Without server-side identity binding, a compromised proxy or any
process that learns the token could forge requests as any caller_id —
agent X could send messages as Coach. The token-to-identity map closes
that hole. Tokens live in a small in-memory dict keyed by token →
`{caller_id, expires_at, ttl_seconds}`, no DB write needed; the
per-slot `_codex_client_tokens` map in
[server/runtimes/codex.py](../server/runtimes/codex.py) holds the
back-reference so `close_client` knows what to revoke.

**TTL is sliding-window.** Each successful `resolve()` extends
`expires_at` to `now + ttl_seconds` (default `ttl_seconds = 7 days`).
Active subprocesses never expire. Truly dormant ones (no coord call
for >TTL) eventually do; the next turn's `get_client` then routes
through `close_client → mint`, rebuilding the subprocess with a
fresh token. The previous fixed 2h TTL bit when Coach went idle
between recurrence ticks longer than 2h: the cached subprocess kept
using the now-expired token in env, every coord call 401'd, and the
turn aborted with "Routine tick is blocked: all coord calls returned
HTTP 401: invalid or expired token." Recovery from that state is
session-clear on the affected slot — closes + revokes + respawns.

### C.5 ClaudeRuntime continues working

ClaudeRuntime keeps the in-process `create_sdk_mcp_server` wrapper
(zero overhead). The subprocess proxy ships only because Codex needs
it. Two paths, same handlers.

```python
# ClaudeRuntime mcp_servers:
mcp_servers = {"coord": coord_server, **external_servers}
```

```python
# CodexRuntime mcp_servers config (TOML/JSON via start_thread):
{
  "coord": {
    "type": "stdio",
    "command": sys.executable,
    "args": ["-m", "server.coord_mcp",
             "--caller-id", agent_id,
             "--proxy-url", "http://127.0.0.1:8000"],
    "env": {"HARNESS_COORD_PROXY_TOKEN": <runtime-owned token>},
    # Pre-approve every coord_* tool. Without this, Codex routes
    # MCP calls through the elicitation/approval path under
    # restrictive sandboxes (Coach is read-only) and the embedded
    # client has no user-input handler — the call is auto-cancelled
    # and the model sees "user rejected MCP tool call". coord_* is
    # harness-trusted by the single-write-handle invariant, so
    # blanket approval is correct. See openai/codex issue #16685
    # and PR #16632 for upstream context.
    "default_tools_approval_mode": "approve"
  }
}
```

**External MCP servers inherit the same approval policy.** Servers added
through the Options drawer are merged into `mcp_servers` with
`default_tools_approval_mode = "approve"` injected when not already
set. Without this, Coach (read-only sandbox) can't call any external
tool — every call hits the same auto-cancellation path that broke
`coord`. The act of adding a server via the UI is the user's
authorization signal; an explicit `default_tools_approval_mode` value
in the saved config is preserved (opt-out for users who want
approval-on-use). Implementation in `_build_mcp_servers`
([server/runtimes/codex.py](../server/runtimes/codex.py)).

> **Don't pass `config.plugins`.** Codex's TOML schema treats
> `plugins` as a map keyed by plugin *name* with `PluginConfig`
> values (a struct with an `enabled: bool` field), so
> `plugins.enabled = false` is parsed as plugin name `"enabled"`
> with a bool — `thread/start` fails with `invalid type: boolean
> false, expected struct PluginConfig`. Default (no `plugins` key)
> is correct. To disable a specific plugin, use
> `plugins = { "<name>" = { enabled = false } }`.

AskUserQuestion stays in-process via Claude's `can_use_tool`
interception. For Codex, AskUserQuestion needs a different path
entirely — see §E.8.

---

## D. Auth — headless Codex login

### D.1 Persistence
`CODEX_HOME=/data/codex` mirrors `CLAUDE_CONFIG_DIR=/data/claude`. The
ChatGPT-session token persists at `/data/codex/auth.json` and survives
redeploys. After deploy, run `CODEX_HOME=/data/codex codex login
--device-auth` once in the container to create the session.

### D.2 Dockerfile
```dockerfile
ENV CLAUDE_CONFIG_DIR=/data/claude \
    CODEX_HOME=/data/codex
...
RUN npm install -g @anthropic-ai/claude-code @openai/codex
```

### D.3 `/api/health` extension
Mirrors the `claude_auth.credentials_present` probe: reads
`${CODEX_HOME}/auth.json` for nonzero size, surfaces
`codex_auth.credentials_present` and
`codex_auth.method: "chatgpt" | "api_key" | "none"`.

### D.4 API-key fallback
Stored in `secrets` table under `openai_api_key`, encrypted via Fernet.
Resolution precedence in CodexRuntime:
1. ChatGPT session present → Codex CLI uses it natively.
2. Else `secrets.openai_api_key` set → inject `OPENAI_API_KEY` into
   subprocess env.
3. Else: emit `human_attention` and abort spawn.

### D.5 Options drawer UI
"Codex auth" section mirrors the Telegram pattern: read-only
ChatGPT-session badge, write-only masked API-key field, "Test" button
(`POST /api/team/codex/test`). Endpoints `GET/PUT/DELETE
/api/team/codex` mirror Telegram's `codex_disabled` flag handling.

---

## E. CodexRuntime implementation

### E.1 Lifecycle

**One `CodexClient` instance per slot**, cached in module-level
`_codex_clients: dict[str, CodexClient]`. Constructed via
`CodexClient.connect_stdio(command=["codex", "app-server"], cwd=...,
env=...)` which spawns the `codex app-server` subprocess. After
construction call `await client.start()` then `await client.initialize()`.
The harness patches the SDK stdio transport to pipe a bounded
`codex app-server` stderr tail into `CodexTransportError` messages;
the stock SDK transport discards stderr and otherwise leaves only
opaque "failed reading from stdio transport" diagnostics.
The `_SPAWN_LOCK` already enforces sequential turns per slot,
satisfying the SDK's "one active turn consumer per client" constraint.
Close (`await client.close()`) and re-open on auth-error / transport
error.

**Cache invalidation on config change.** `mcp_servers` is captured at
subprocess spawn time via `_codex_config_overrides` → `_build_mcp_servers`,
so a UI-side MCP server add / patch / delete won't propagate into the
running subprocess. Two helpers handle this:

- `evict_client(slot)` — full close on idle slots; cache-pop only when
  a turn is in flight (lets the live turn finish on its own client
  reference; next turn rebuilds with current MCP config).
- `evict_all_clients()` — same, applied to every cached slot.

`evict_client(slot)` is called from `DELETE /api/agents/{id}/session`
(single + batch), and from `coord_set_player_runtime` when Coach flips
a Player's runtime (a codex→claude flip would otherwise leave the
cached Codex subprocess + proxy token dangling until the next
MCP-config change). `evict_all_clients()` is called from
`POST/PATCH/DELETE /api/mcp/servers/...`. Result: MCP server changes
take effect on the agent's next turn without a server restart.

**Tool-contract version bump on coord-tool changes.**
`_CODEX_TOOL_CONTRACT_VERSION` (see [server/runtimes/codex.py]) is a
string the runtime stores in `team_config` after each successful boot.
On the next boot, if it differs from the in-code value,
`ensure_codex_tool_contract_current()` clears every persisted
`codex_thread_id` so the next Codex turn starts fresh and picks up
the current MCP tool surface. This is necessary because the
codex-app-server can preserve thread-local tool state across resumes
— old threads can keep telling the model that a coord_* tool is
"unavailable in this session" even after the harness adds it. Bump
the constant whenever a coord_* tool is added, removed, or renamed,
or when a tool's exposed description changes meaningfully.

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
  - `CodexTimeoutError` is special-cased. The SDK's default
    `request_timeout` is 30s; under load (cold app-server subprocess,
    slow Codex backend, large stored thread state — Coach especially)
    `thread/resume` can transiently exceed it. `open_thread` retries
    `_CODEX_RESUME_TIMEOUT_RETRIES` (default 2, total 3 attempts) with
    a brief inter-attempt delay before falling back, so a transient
    blip does not cost the agent its thread continuity. Other
    exception classes (CodexProtocolError, CodexTransportError,
    plain Exception) skip the retry and fall back on the first
    attempt — they are not transient.
- Codex prepares this start/resume before `agent_started` is emitted so
  `agent_started.resumed_session` reflects the actual successful path,
  not just the presence of a stored `codex_thread_id`.

### E.3 ConversationStep → harness event mapping

Developer instructions also append a Codex compatibility note: this
Claude-origin harness treats `CLAUDE.md` as `AGENTS.md`/`agents.md`,
and `.claude/` directories as `.agents/`. Codex agents must read and
obey those files/directories for the applicable tree instead of
ignoring them because of the Claude names.

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
| `codex` / `agentMessage` (text set)                | `text`                     | emit `content` + `text`, accumulate; phase=='final_answer' marks turn done |
| `thinking` / `reasoning`                           | `thinking`                 | emit `content` + `text`; parallel to Claude path |
| `exec` / `commandExecution`                        | `tool_use` (`name=Bash`, `tool=Bash`) + optional `tool_result` | SDK 0.3.2 normalized shell shape |
| `codex` / `shell`                                  | `tool_use` (`name=Bash`, `tool=Bash`)   | draft compatibility |
| `file` / `fileChange`                              | `tool_use` (`name=Edit`, `tool=Edit`) + optional `tool_result` | SDK 0.3.2 normalized file-change shape |
| `codex` / `apply_patch`                            | `tool_use` (`name=Edit`, `tool=Edit`)   | draft compatibility; unified diff feeds diff@7 |
| `codex` / `web_search` or `webSearch`              | `tool_use` (`name=WebSearch`, `tool=WebSearch`) | reuse WebSearch card |
| `tool` / `mcpToolCall` or `mcp_tool_call`          | `tool_use` (`name=mcp__...`, `tool=mcp__...`) + optional `tool_result` | coord_* + external MCPs; unwrap `args`/`arguments`/`input` into renderer input |
| stream exhaustion                                  | `result`                   | usage is read from the rollout JSONL pointed to by `thread.read().thread.path` (see §E.5); thread.read fields are unused by SDK 0.3.2 |
| `CodexTurnInactiveError` raised mid-iteration      | `error` (pre-result) → retry counter |
| `CodexTimeoutError` / `CodexTransportError`        | `error` (pre-result) -> retry counter; close + reopen client; transport errors include captured app-server stderr tail when available |
| `CodexProtocolError`                               | `error`; if "thread not found" trigger stale-session retry |
| `ApprovalRequest` via `set_approval_handler`       | `human_attention`, then decline (§E.8 option b) |
| `thread.compact()` return                          | `session_compacted` (§E.6) |

`ChatResult` in `codex-app-server-sdk` 0.3.2 does not expose usage
directly, **and** `thread.read(include_turns=True).turns[*]` ships
without a `usage` field either (verified live 2026-04-29 against
Codex CLI 0.125.0 / SDK 0.3.2). Token counts only land on disk in the
rollout JSONL — see §E.5 for the parser the runtime now uses.

#### E.3.1 Tool-result error classification

`_step_payload_is_error(item_payload)` decides whether a completed
tool item renders red (error) or green (success) in the UI. It
matches against the lowercased `status` (or `state` as fallback):

| Pattern  | Origin                                                          |
|----------|-----------------------------------------------------------------|
| `error`  | tool itself reported failure                                    |
| `fail`   | tool itself reported failure                                    |
| `cancel` | OpenAI Codex safety-monitor cancelled the call mid-flight      |
| `reject` | OpenAI Codex safety-monitor refused the call                    |

Plus any non-zero `exit_code` / `exitCode` / `returncode` is treated
as an error. The `cancel` / `reject` patterns are the important
addition for Codex: the safety monitor surfaces a refused tool call
as a "completed" item with `status='cancelled'` (or similar) and a
prose explanation in the body. Without those patterns, monitor
cancellations rendered green and were indistinguishable from real
successes — Coach historically paraphrased them as generic "rejected
by the coordination layer" because there was no clean error signal
to lean on.

Substring matching is intentional (handles `cancelled`/`canceled`,
`user_cancelled`, `cancellation_pending`, `rejected`, etc.); no real
status string uses these words for a success outcome.

### E.4 Tool execution
Codex executes native tools inside the codex-app-server subprocess — we
observe completed items via notifications. `coord_*` MCP tools route
through the stdio MCP proxy with the same token-bound
`--caller-id <agent_id>` discipline as the Claude in-process handler.

### E.5 Cost / pricing
Codex's `Turn.usage` reports tokens but NOT USD. New module `server/pricing.py`:

```python
CODEX_PRICING = {
    "gpt-5.5":      {"input": 5.0,  "cached": 0.50,  "output": 30.0},
    "gpt-5.4":      {"input": 2.50, "cached": 0.25,  "output": 15.0},
    "gpt-5.4-mini": {"input": 0.75, "cached": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached": 0.02,  "output": 1.25},
    "gpt-5.3-codex": {"input": 1.75, "cached": 0.175, "output": 14.0},
    "gpt-5.2-codex": {"input": 1.75, "cached": 0.175, "output": 14.0},
    "gpt-5.1-codex-max": {"input": 1.25, "cached": 0.125, "output": 10.0},
    "gpt-5.1-codex": {"input": 1.25, "cached": 0.125, "output": 10.0},
    "gpt-5.1-codex-mini": {"input": 0.25, "cached": 0.025, "output": 2.0},
    "gpt-5-codex": {"input": 1.25, "cached": 0.125, "output": 10.0},
}
def codex_cost_usd(model: str, usage: Mapping[str, int]) -> float: ...
```

Split `_extract_usage` into `_extract_usage_claude` and `_extract_usage_codex`. CodexRuntime passes the usage block it reads from thread state. Map Codex's single `cached_input_tokens` → `cache_read_tokens`, write `0` to `cache_creation_tokens` (Codex caching has no separate creation cost). `_insert_turn_row` accepts a new `runtime` arg.

**Rollout JSONL parser (live since 2026-04-29).** The canonical source
of per-turn token counts in SDK 0.3.2 is the on-disk session log at
`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*-<thread_id>.jsonl`. The
path is exposed on `thread.read().thread.path`. After every turn the
runtime opens that file and scans for the most recent
`payload.type == "token_count"` event:

```json
{"type": "event_msg", "payload": {
  "type": "token_count",
  "info": {
    "last_token_usage": {
      "input_tokens": 37118,           // total prompt (incl. cached)
      "cached_input_tokens": 3456,     // cached subset
      "output_tokens": 58,
      "reasoning_output_tokens": 0,
      "total_tokens": 37176
    },
    "total_token_usage": { ... },      // cumulative across the session
    "model_context_window": 258400     // Codex CLI's effective working window
  }
}}
```

Translation to harness shape (mirrors the Anthropic convention so
cost / context-bar math is consistent across runtimes):

- `input_tokens (harness) = max(0, last.input_tokens − last.cached_input_tokens)`
- `cache_read_tokens (harness) = last.cached_input_tokens`
- `output_tokens (harness) = last.output_tokens + last.reasoning_output_tokens`
- `cache_creation_tokens (harness) = 0`

Helpers live in `server/runtimes/codex.py`:
`_rollout_path_from_thread_state`, `_read_codex_token_count_from_rollout`,
`_codex_usage_from_rollout_info`, and `_model_from_rollout` (last-resort
model id pulled from rollout `turn_context` events when `tc.model` was
None — happens when no per-role Codex default is set in `team_config`).
The legacy `_extract_codex_usage_from_thread_state` walker is kept as
a fallback for any future SDK that ships usage directly on `Turn`.

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

**Auto-compact for Codex agents** (added 2026-05-02 — see §A.5)
mirrors Claude's threshold semantics but takes a structurally
different path through the dispatcher.
`CodexRuntime.maybe_auto_compact` reads the shared
`HARNESS_AUTO_COMPACT_THRESHOLD` env (default 0.7), short-circuits on
`tc.compact_mode` / unparseable threshold / threshold ∉ (0.0, 1.0) /
no `codex_thread_id` / `used / window < threshold`, and computes
`used / window` from `_codex_session_context_estimate(thread_id)`
(falls back to 0 if no `turns` row exists yet) and
`_context_window_for(tc.model)` (1M floor for unknown models, so
Codex auto-compact errs on the conservative side rather than
firing prematurely). When the ratio trips, it:

1. Emits `auto_compact_triggered` with the same payload shape Claude
   emits (`used_tokens`, `context_window`, `ratio`, `threshold`,
   `deferred_prompt`).
2. Sets `tc.compact_mode = True` and `tc.auto_compact = True` on the
   throwaway dispatcher TurnContext (so a re-entry via
   `run_manual_compact` doesn't recurse) and mirrors them into
   `tc.turn_ctx` for any downstream reader.
3. Delegates to its own `run_manual_compact(tc)` — i.e. the
   **native** `client.compact_thread(thread_id)` path above, not a
   `COMPACT_PROMPT` LLM turn.
4. After `run_manual_compact` returns, checks
   `tc.turn_ctx.get("got_result")` as the success signal.
   `run_manual_compact` swallows its own auth / ImportError /
   `compact_thread` exceptions (it emits `error` + sets status=error
   and returns silently), so an unraised-but-failed compact is
   detected by the absence of `got_result`. Both the explicit raise
   path and the silent-failure path emit `auto_compact_failed` and
   return False — symmetric with Claude's failure posture.

The dispatcher then proceeds with the user's original prompt; the
subsequent prior-session read at `agents.py` returns None (the
compact cleared `codex_thread_id`), and the fresh Codex thread picks
up the just-written continuity note from the system prompt assembly.

**Structural differences from Claude's auto-compact** (intentional;
documented for future maintainers):

- Claude's `maybe_auto_compact` recursively calls
  `run_agent(COMPACT_PROMPT, compact_mode=True, auto_compact=True)`,
  which goes through the full dispatcher (spawn lock, cost cap,
  `agent_started` / `agent_stopped` events, `run_turn` with the
  COMPACT_PROMPT through the SDK). The user therefore sees TWO turn
  cycles in the timeline: compact-cycle then user-cycle. CodexRuntime
  instead invokes `run_manual_compact` directly as a side-effect of
  `maybe_auto_compact`. The user sees only `auto_compact_triggered`
  → `session_compacted` → the user's `agent_started` (single cycle).
- Claude's recursive path goes through `_check_cost_caps`, so an
  over-budget compact is rejected with `cost_capped` (no compact AND
  no user turn). Codex's path bypasses the cap because
  `client.compact_thread` is a server-side operation that doesn't
  show up in the harness's `turns`/cost ledger. Net effect on
  over-budget Codex: compact still happens, then the user's turn is
  rejected with `cost_capped`. When budget frees up, the next user
  turn lands on the already-compacted fresh thread with the
  continuity note in place.
- Both runtimes share the spawn-lock-after-`maybe_auto_compact` race
  window: two concurrent `run_agent` calls for the same slot can
  both enter `maybe_auto_compact` before either claims the spawn
  lock. Claude's recursive path serializes via the inner spawn
  lock; Codex's direct path can fire two `compact_thread` calls
  back-to-back. In practice, the second one sees a cleared
  `codex_thread_id` (the first finished before the second's
  `_get_codex_thread_id` re-read inside `run_manual_compact`) and
  no-ops via the existing "no codex thread to compact" branch.
  Acceptable.

The original "context-pressure signal isn't exposed yet" caveat
applied before `_codex_session_context_estimate` shipped (that
helper reads the per-thread `turns` ledger and reconstructs
prompt+output tokens in the same shape Claude's JSONL probe
produces, so the UI context bar already used it). The trip-wire was
wired through once that signal was available.

### E.7 Error handling
- `turn.failed` pre-result → `_emit("error")`, increment `_consecutive_errors`, schedule retry.
- Exception post-result → suppressed by dispatcher's existing `got_result` discipline.
- 401/auth errors → emit `human_attention`, stop retrying (Telegram bridge precedent).

### E.8 Session transfer (compact + flip)

Switching an agent's runtime mid-life would normally lose the entire
conversation: each runtime owns its own session column
(`session_id` for Claude, `codex_thread_id` for Codex), and neither
can read the other. A blunt flip via `PUT /api/agents/{id}/runtime`
preserves that semantic — it's a "fresh start on the new runtime"
operation with no continuity carry-over.

The session-transfer flow (added 2026-05-02) closes the gap by
running the standard `/compact` summary on the **source** runtime
before the column flip. The summary is written to
`agent_sessions.continuity_note`, which the next system prompt on
the target runtime injects as a `## Handoff from your prior session`
block — the same vehicle a normal `/compact` uses to brief
fresh-you on the new session.

**Surfaces:**
- `POST /api/agents/{id}/transfer-runtime {runtime: 'claude'|'codex'}`
  — HTTP entry point used by the pane's runtime selector.
- `coord_set_player_runtime(player_id, runtime)` MCP tool — Coach
  entry point. Empty `runtime=''` keeps the legacy blunt-clear
  semantics (revert to role default, no transfer).

**Dispatch matrix** (computed before queueing anything):

| Source runtime  | Target runtime  | Prior session?  | Action                                                                                      |
|-----------------|-----------------|-----------------|---------------------------------------------------------------------------------------------|
| X               | X (same)        | —               | 200 noop                                                                                    |
| X               | Y               | NO              | flip column directly + emit `runtime_updated` + `session_transferred(note=no_prior_session)`|
| X               | Y               | YES             | queue `run_agent(COMPACT_PROMPT, compact_mode=True, transfer_to_runtime=Y)` on X            |

Mid-turn flips (`agents.status='working'`) are 409'd at the entry
point — same rule as the blunt PUT, for the same reason
(in-flight turn would be on the old runtime while subsequent turns
use the new one).

**Compact-handler branch.** `transfer_to_runtime` rides through
`run_agent` → `TurnContext` → `turn_ctx`. The post-compact path in
each runtime's message handler reads the flag:

- ClaudeRuntime (`server/agents.py:_handle_message`) writes
  `continuity_note`, nulls `session_id`, calls
  `_perform_runtime_transfer_flip(slot, target)` (which flips the
  column, nulls the **other** runtime's session column too — defensive
  against orphaned thread ids from a prior life on the target — and
  emits `runtime_updated` with `source=session_transfer`), then emits
  `session_transferred(from_runtime, to_runtime, chars, handoff_file)`
  in place of the ordinary `session_compacted`. If the compact yielded
  no summary, the runtime is **not** flipped — `session_transfer_failed`
  fires and the agent stays put.
- CodexRuntime (`server/runtimes/codex.py:run_manual_compact`) calls
  `client.compact_thread(thread_id)`, writes the returned summary to
  `continuity_note`, clears `codex_thread_id`, then performs the same
  flip + emits `session_transferred`. Codex flips even on empty
  summary because `compact_thread` already cleared the thread id;
  not flipping would leave the agent on Codex with no thread to
  resume — strictly worse than flipping with thin context. Asymmetry
  is intentional and noted inline.

**Event vocabulary additions.** The bus carries three new event
types so the UI can label the timeline as a transfer rather than a
compact:

| Event                          | When                                                                                                |
|--------------------------------|-----------------------------------------------------------------------------------------------------|
| `session_transfer_requested`   | Fired by the entry point when a compact is queued (replaces `session_compact_requested`).           |
| `session_transferred`          | Fired by the compact handler on success after the flip (replaces `session_compacted`).              |
| `session_transfer_failed`      | Claude path only — fired when compact yields no summary so the runtime stays put. UI shows reason.  |

The pane's runtime selector in
[server/static/app.js](server/static/app.js) routes through this
endpoint when the user picks `claude` / `codex`. Picking `default`
(empty) still uses the blunt `PUT /api/agents/{id}/runtime` path —
the user explicitly asked to revert to role defaults, no compact.

**Why this is not just `/compact` followed by a flip.** Atomicity:
the flip only happens **iff** the compact succeeded with a non-empty
summary. A user who runs `/compact` then `PUT runtime` separately
gets the flip even when the compact failed, leaving the agent on
the new runtime with no continuity carry-over. The transfer flow
also emits the right event vocabulary so timelines read as
"transfer", not "compact + unrelated flip", and the LeftRail's
runtime badge updates exactly once via `runtime_updated`.

### E.8 AskUserQuestion under Codex

ClaudeRuntime intercepts `AskUserQuestion` via `can_use_tool`
([server/agents.py:_build_can_use_tool](../server/agents.py)). Codex
has no equivalent hook — it executes its own native tools internally,
and shipped behavior degrades: the runtime sets
`approval_policy='never'`; if the SDK still emits an approval
side-channel request, the harness surfaces `human_attention` and
declines so the turn does not hang behind an invisible prompt. Coach
and Players are expected to escalate via `coord_request_human` instead.

A future `coord_ask_user` MCP tool could replace this degradation if
Codex agents prove to need synchronous user questions; not shipped.

---

## F. UI changes

### F.1 Pane settings popover
Runtime radio (Claude | Codex). New `PUT /api/agents/{id}/runtime` mirroring `/brief` and `/model` patterns. Audit-logged via `audit_actor(request)`. Mid-turn change → 409 Conflict.

### F.2 LeftRail runtime badge
CSS-icon only (invariant #6). `.slot-runtime-claude::after` = filled disc; `.slot-runtime-codex::after` = filled square. Bottom-left of slot button (lock badge already owns bottom-right). Distinct hue per runtime.

### F.3 Options drawer defaults
New section "Default runtime per role": Coach radio + Players radio. `team_config` keys `coach_default_runtime`, `players_default_runtime`. Resolution: per-agent > role default > `'claude'`. Default model controls are split by runtime (`coach_default_model` / `players_default_model` for Claude, `coach_default_model_codex` / `players_default_model_codex` for Codex), and pane model dropdowns resolve against the effective runtime (slot override or role default) before showing Claude vs Codex options.

**Hardcoded role defaults** (`models_catalog._ROLE_CODEX_MODEL_DEFAULTS`) — what a fresh deploy uses when `team_config` is unset:

- Codex Coach: `latest_gpt` → `gpt-5.5`. Mirrors the Claude side's Coach=Opus default (same cost ratio as Claude Coach on Opus, which is the existing accepted default). Was historically empty (rationale: top-tier Codex on every Coach tick is expensive); changed to a concrete default so the model chip can render a real name from first paint instead of falling back to the runtime tag.
- Codex Players: `latest_mini` → `gpt-5.4-mini`. Sonnet-equivalent mini-tier — the right default when Coach flips a Player to Codex (Claude rate limits, frequent compactions).

The "use top-tier only for heavy reasoning" rule lives in `MODEL_GUIDANCE` (Coach's system prompt); per-Player escalation goes through `coord_set_player_model`.

**Effort tier role default** is shared with Claude (`_ROLE_EFFORT_DEFAULTS = {coach: 2, players: 2}`, both medium) — runtime-agnostic, applied by the dispatcher in `run_agent` before `tc.effort` reaches `_build_turn_overrides` in `server/runtimes/codex.py`. `_CODEX_EFFORT_LEVELS` maps `2 → "medium"` and passes it through `TurnOverrides(effort="medium")` to the Codex SDK. The UI's effort chip mirrors the same fallback (`ROLE_DEFAULT_EFFORT` in `app.js`) so the chip reads "med" from first paint on Codex agents too.

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

### G.4 No Codex-specific env caps

The existing combined USD cap (`HARNESS_AGENT_DAILY_CAP` /
`HARNESS_TEAM_DAILY_CAP`) plus the visible token meter is the cap
surface. There is no separate `HARNESS_CODEX_DAILY_USD` or
`HARNESS_CHATGPT_TOKENS_DAILY`.

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

### I.1 Codex Python SDK
`codex-app-server-sdk>=0.3.2` from PyPI. Provides the `CodexClient`
stdio transport, `start_thread` / `resume_thread`, and the
`ConversationStep` notification stream. The harness patches the SDK's
stdio transport to capture `codex app-server` stderr (the stock
transport discards it, leaving opaque "failed reading from stdio
transport" diagnostics).

### I.2 MCP transport
`server/coord_mcp.py` uses the official `mcp` Python stdio server
transport (`mcp>=1.0`). Codex app-server gets the standard
initialize/tools/list/tools/call handshake; coord tool execution
remains centralized behind the loopback HTTP proxy. `websockets>=16.0`
is the SDK's direct runtime dependency.

### I.3 Package discovery
`pyproject.toml` uses `[tool.setuptools.packages.find]` with
`include = ["server*"]` so `server/runtimes/` ships in the wheel.
Explicit `packages = [...]` would silently miss the subpackage.

### I.4 Dockerfile
`ENV CODEX_HOME=/data/codex`. `npm install -g @openai/codex` for the
Codex CLI. Build smoke-tests `codex app-server --help` to fail fast
on install breakage.

---

## J. Tests

`server/tests/` (DB-level pytest, no FastAPI TestClient) covers:

- `test_runtime_dispatch.py` — fake runtime + dispatcher contract
  (cost-cap rejects before `run_turn`, `agent_started` before,
  `agent_stopped` after).
- `test_db_schema_migration.py` — old-schema DB → `init_db()` →
  asserts new columns + defaults.
- `test_codex_pricing.py` — `(model, usage, expected_usd)` table +
  unknown-model fallback.
- `test_codex_event_normalization.py` — fake stream of Codex
  notifications, asserts `_emit` calls match Claude vocabulary.
- `test_codex_runtime_gate.py` — runtime resolution, MCP cache
  eviction (idle close, in-flight pop, `evict_all_clients`).
- `test_cost_cap_aggregation.py` — mixed-runtime rows, asserts
  combined sum and rejection.
- `test_coord_mcp_proxy.py` — spin up FastAPI in-process, mint a
  token bound to `caller_id='p1'`, spawn `python -m server.coord_mcp`
  with the token in env, exercise `tools/list` and
  `coord_send_message`. Asserts the row appears in `messages`, the
  `message_sent` event is published on the **main process'** bus,
  and `maybe_wake_agent` is invoked. Negative test: body `caller_id`
  mismatching the token's bound identity → 403.

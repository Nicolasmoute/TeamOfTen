---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 19: Security and Auth'
section: 19
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 19. Security and Auth

### 19.1 UI/API

`HARNESS_TOKEN`:

- If unset: API is open.
- If set:
  - all `/api/*` except `/api/health` require `Authorization: Bearer <token>`
  - WebSocket requires `?token=<token>`
- It is deployment process env only. Storing a UI-managed encrypted
  secret named `HARNESS_TOKEN` does not configure the API auth gate and
  does not export that value to Coach or Player runtimes.

This is single-user security, not a multi-user auth system.

Audit metadata:

- Destructive human actions include `actor` with source, IP, and User-Agent.

### 19.2 Claude OAuth

Default:

```text
CLAUDE_CONFIG_DIR=/data/claude
```

The CLI stores `.credentials.json` and `.claude.json` there. The API can write
a pasted credentials JSON so the operator does not need shell access.

Health reports whether `.credentials.json` exists.

### 19.3 WebDAV Credentials

WebDAV credentials are env vars:

- `HARNESS_WEBDAV_URL`
- `HARNESS_WEBDAV_USER`
- `HARNESS_WEBDAV_PASSWORD`

They are not exposed through API beyond enabled/reason/url status.

### 19.4 MCP/Telegram Secrets

UI-managed secrets are encrypted in SQLite. API never returns plaintext. The
runtime interpolator can read them for MCP/Telegram use, repo URL
interpolation, and other explicit `${VAR}` expansion sites. The
secrets table is not a general environment-injection mechanism for
agents. Agents do not receive arbitrary stored secrets; Codex coord
access uses its own per-slot `HARNESS_COORD_PROXY_TOKEN`.

### 19.4.1 Secret-path agent guard

A PreToolUse hook (`_pretool_secret_guard_hook` in
`server/agents.py`) blocks agent writes / reads against
harness-managed locations:

- Claude CLI config: `$CLAUDE_CONFIG_DIR` (default `/data/claude`)
  + `~/.claude` fallback.
- Codex CLI config: `$CODEX_HOME` (default `/data/codex`)
  + `~/.codex` fallback.
- SQLite DB: `$HARNESS_DB_PATH` (default `/data/harness.db`).
- `/proc/<pid>/environ` and `/proc/self/environ` (env exfil).

Two parallel checks:

- **Path-based** (`_path_is_secret`): for `Write` / `Edit` /
  `Read` / `Bash` arguments that are filesystem paths. Resolves
  symlinks before comparing so symlink-based escapes
  (`/workspaces/p1/foo → /data/claude/.credentials.json`) trip.
- **Bash pattern-based** (`_BASH_SECRET_PATTERNS`): regex over
  the command string. Catches reads that wouldn't surface as
  a path argument (e.g., `awk '...' /data/claude/...`).

**Carveout**: `$CLAUDE_CONFIG_DIR/plans/` is allowed (the Claude
CLI's plan-mode workspace lives there; without an exception the
guard breaks plan mode). Scoped to Claude roots only — Codex root
and the DB path have no analogous carveout. Symlink-based escapes
like `/data/claude/plans/../.credentials.json` collapse via
`resolve()` to the canonical sibling path and still trip.

### 19.5 Per-agent runtime selection

Two runtimes ship: **ClaudeRuntime** (default; described inline
throughout this doc) and **CodexRuntime** (gated by
`HARNESS_CODEX_ENABLED`; full spec in `Docs/CODEX_RUNTIME_SPEC.md`).

Resolution at spawn time:
`agents.runtime_override` → `team_config` role default
(`coach_default_runtime` / `players_default_runtime`) → `'claude'`.

`PUT /api/agents/{id}/runtime` sets the per-slot override. `'codex'`
is rejected when `HARNESS_CODEX_ENABLED` is unset. Mid-turn flips
return 409. The PUT path is the **blunt** flip — it writes
`runtime_override` and the next turn on the new runtime starts with
no memory of the prior conversation. Use `POST /api/agents/{id}/transfer-runtime`
when the agent has a session worth carrying — see §10.3 for the
transfer flow that runs `/compact` first and only flips on success.
The pane gear popover routes through `transfer-runtime` when the
user picks a concrete runtime and falls back to the blunt PUT only
when the user picks `default` (clear the override).

Model selection is runtime-aware: Claude defaults
(`coach_default_model` / `players_default_model`) and Codex defaults
(`coach_default_model_codex` / `players_default_model_codex`) are
stored separately. The pane gear resolves the effective runtime
first, then chooses the Claude or Codex model list. A stored
`agent_project_roles.model_override` that no longer fits the slot's
current runtime is silently dropped at spawn time.

`agent_started` payload carries `runtime`. Successful turns insert a
`turns` row with `runtime` and `cost_basis` populated. The
`team_runtimes_updated` WebSocket event refreshes pane state when the
Options drawer changes role defaults.

### 19.6 Coord MCP proxy (loopback)

CodexRuntime cannot host an in-process Python MCP server, so
`coord_*` calls route through a stdio subprocess
(`python -m server.coord_mcp`) that forwards to the main FastAPI
process via two internal endpoints:

- `POST /api/_coord/{tool_name}` — dispatches to the in-process
  coord handler.
- `GET /api/_coord/_tools` — tool catalog for the subprocess to
  publish over MCP `tools/list`. The catalog carries the same
  `@tool` descriptions and input schemas Claude receives in-process,
  not placeholder schemas; this is what lets Codex Coach use
  recurrence, Compass, playbook, and kanban tools without guessing
  parameters.

Both are loopback-only. `POST /api/_coord/{tool_name}` is also
bearer-token gated (`HARNESS_COORD_PROXY_TOKEN` env); the catalog is
non-sensitive source-derived metadata and remains loopback-only. The
token is minted by
`server.spawn_tokens.mint(caller_id)` and bound to the caller — the
endpoint resolves `caller_id` from the token, not the request body.
ClaudeRuntime is unaffected; it uses an in-process MCP server and
never touches these endpoints.

CodexRuntime also applies a turn-level sandbox policy for Player
turns: the active slot's worktree remains writable, while the shared
`.project` seed checkout and sibling slot worktrees are blocked via the
Codex sandbox `blockedPaths` / workspace-write policy surface. Coach's
Codex sandbox remains read-only. This is the runtime-level boundary
that complements the existing file-guard hooks in ClaudeRuntime.
The Codex runtime probes bubblewrap's actual namespace capability once
per process before applying that Player policy. If the host denies the
mount propagation step (`bwrap: Failed to make / slave: Permission
denied`), Player turns automatically degrade to the pre-boundary
`danger-full-access` mode and emit `runtime_sandbox_degraded`; Coach
remains read-only. `/api/health/detail` exposes
`checks.codex_sandbox` with the probe result.

Token lifetime, MCP cache invalidation on config change or
`agents.allowed_tools` mismatch, `default_tools_approval_mode`
injection, process-tree cleanup, and the stdio error-shape contract are
CodexRuntime concerns — see
`Docs/CODEX_RUNTIME_SPEC.md` §C.4 and §E.1.
The proxy also implements empty MCP `resources/list`,
`resources/templates/list`, and `prompts/list`; Codex app-server
startup treats unsupported capability probes as transport errors.

CodexRuntime spawns `codex app-server` with a harness-controlled SDK
request timeout (`HARNESS_CODEX_REQUEST_TIMEOUT_SECONDS`, default
120s) instead of the SDK's 30s default. Resume-time
`CodexTransportError` is treated as a poisoned app-server client. On
resume it is not handled as an ordinary stale-thread failure, but once a
pre-result turn fails in the dispatcher error path the retry is forced
onto a fresh Codex thread: the failed in-flight turn is first appended
to the rolling handoff log with bounded assistant text, tool
calls/results, and stderr/process diagnostics; recent exchanges are then
salvaged into `continuity_note`, `codex_thread_id` is cleared, the
cached app-server client is closed, and `session_auto_recovered` records
`reason='transport_error'` (or `reason='repeated_transport_error'` for
later consecutive strikes). This avoids exhausting the auto-retry budget
against the same broken stdio/thread pair and preserves the failed
Edit/Bash context for the fresh retry.
Codex developer instructions also tell agents resolving conflicted
files to re-read the live file or index stages immediately before
native Edit/apply_patch and to switch to smaller/fresher edits after a
patch verification failure. This addresses the p4 failure pattern where
stale patch contexts appeared shortly before the stdio stream died.
If the turn stream already completed and only the post-turn
`thread.read(include_turns=True)` usage lookup hits a transport error,
the turn remains successful, the cached app-server client is closed,
recent exchanges are salvaged into `continuity_note`, and
`codex_thread_id` is cleared before the next turn. In production this
shape has correlated with an unresumable Codex thread, so clearing it
immediately prevents the next auto-wake from burning retry attempts on
the same dead stdio receiver.
The patched stdio transport starts the Codex Node wrapper/native
app-server tree in its own process group and closes that group
explicitly. The 2026-05-16 production incident showed stale Codex
process pairs reparented under PID 1 and accumulating per Player slot;
closing only the SDK client or wrapper process is insufficient.
The patched transport also raises the subprocess StreamReader line
limit above Python's 64 KiB default (`HARNESS_CODEX_STDIO_LIMIT_BYTES`,
default 8 MiB, clamped 256 KiB..64 MiB). Codex app-server uses
newline-delimited JSON, so a single large Bash output, file-read result,
or post-turn thread-read payload can otherwise look like a stdio
receiver-loop failure even though the app-server process is still alive
and has no stderr.
Before each Codex Player spawn, the dispatcher also refreshes
`agents.allowed_tools` from the active kanban role row for
`agents.current_task_id` when that row exists; otherwise it falls back
to the newest active current-stage role row. When the stored JSON no
longer matches that role allowlist, existing shipper or executor
assignments pick up newly-added completion tools without a same-stage
reassignment. Pending ship rows cannot leak `coord_ship_to_dev` into an
executor turn for another current task, and pending verifier rows only
expose `coord_submit_verification_report` once the task is in `verify`.

**Transient-error retry (2026-05-13)**: `CoordProxyClient.call_tool`
retries on transport errors (`httpx.ConnectError`, `ReadTimeout`,
`WriteTimeout`, `PoolTimeout`, `RemoteProtocolError`, `ReadError`,
`WriteError`) and HTTP 5xx responses. Backoff schedule: 100ms,
400ms, 1500ms (total ~2s worst case for 4 attempts). 4xx responses
are NOT retried — they're caller-side validation / auth issues.
After exhaustion the surfaced error message names the actual
exception type and attempt count so callers can distinguish
transient vs permanent. Closes Coach's 2026-05-12 "stream error on
coord_* call, no signal reached Coach until next wake" failure
mode.

---

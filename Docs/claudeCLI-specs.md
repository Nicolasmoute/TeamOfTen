# Claude Code CLI Runtime — Specification (v2)

> **Subordinate to `Docs/TOT-specs.md`.** When this doc and TOT-specs
> disagree, TOT-specs wins. This file is the source of truth for
> Claude-Code-CLI-specific behavior — runtime lifecycle, pty
> management, screen-flicker idle detection, prompt injection,
> bubblewrap envelope, the two-CLAUDE.md file model, runtime-transfer
> entry/exit — but cannot redefine fields, endpoints, events, or
> invariants that TOT-specs declares.
>
> Sibling spec: `Docs/CODEX_RUNTIME_SPEC.md`. The Codex runtime
> already proves the "third runtime sitting next to Claude SDK" shape.
> This spec follows that pattern. Where Codex uses an app-server
> subprocess with JSON-RPC, the CLI runtime uses an interactive
> `claude` TUI subprocess driven through a pty. Most of the
> harness-side mechanics (runtime protocol, `coord_*` MCP plumbing,
> bus events, kanban routing, Compass, Telegram) are runtime-blind
> and require no changes.

Status: spec v2 (2026-05-14). Implementation not started. Rollout
intent: enable for Players via operator opt-in per slot per project;
no automatic migration. Coach excluded from v1.

Changes from v1:
- Two-CLAUDE.md-file architecture (project-level + harness-compiled);
  SDK and Codex runtimes are NOT refactored.
- CLI spawn uses `--setting-sources user --append-system-prompt-file
  <compiled-path>` to load the harness-compiled context.
- No automatic migration. Operator manually flips slots.
- Terminal accepts direct keystrokes (display-only mode dropped).
- Scrollback persisted to disk with continuous-file model across
  lazy-respawns.
- `/compact` re-reads `<append-file>` (verified doc behavior); two
  refresh triggers (session start, `/compact`).

---

## A. Premise and scope

### A.1 Why this runtime exists

Anthropic has been narrowing what the Claude Agent SDK and `claude -p`
headless modes can do relative to the interactive `claude` CLI. The
harness's continued viability under Max-plan OAuth depends on having
a runtime that exercises the CLI exactly as a human operator would:
an interactive TUI session, not a programmatic JSON stream.

Each agent slot runs its own long-lived interactive `claude` process
inside a pty. The harness composes prompts (largely unchanged from
today), injects them via bracketed-paste, and watches the rendered
screen change over time to know whether the agent is working or idle.
The CLI's own terminal rendering is what the operator sees in the
pane — no parsing, no structured event extraction, no diff-card
reconstruction. The terminal IS the timeline.

### A.2 What this runtime is NOT

- **Not** a parser of the CLI's output for structured events.
- **Not** a programmatic cost-tracking surface.
- **Not** a replacement for harness orchestration: every `coord_*`
  tool, bus event, kanban transition, Compass audit, Telegram
  forward, watchdog finding, and `project_events` row continues to
  flow through harness Python exactly as today.

### A.3 Scope: Players only, no automatic migration

v1 enables this runtime for slots `p1`..`p10`. **Coach is excluded** —
Coach orchestration relies on programmatic precision (turn-boundary
detection for tick scheduling, cost-cap pre-spawn gating, recurrence
machinery, watchdog reporting) and the degradations in §A.4 are
unacceptable for the coordinator role.

**No automatic migration.** The runtime resolver gains `'claude_cli'`
as a third valid value, gated behind `HARNESS_CLAUDE_CLI_ENABLED`
(default true; emergency kill switch). Operators flip individual
slots via the existing pane gear popover or
`PUT /api/agents/{id}/runtime`. The harness does NOT seed p10 or any
other slot to `claude_cli` automatically on existing or new projects.

### A.4 What's dropped (acknowledged degradations)

The decisions are settled — listed here so the spec is concrete about
what NOT to implement.

| Dropped capability | Replacement |
| --- | --- |
| Programmatic cost tracking per turn | Operator/agent type `/status` or `/cost` in the pane; CLI prints in the terminal |
| Pre-spawn hard cost caps (`HARNESS_TEAM_DAILY_CAP`) | Soft: operator monitors `/status` trends; no auto-block |
| Auto-compact at context threshold | CLI's native auto-compact handles it; operator can fire `/compact` via composer |
| Structured per-tool renderers (EditDiff card, Read renderer, …) | Terminal scrollback shows the CLI's own rendering |
| Sticky structured turn-boundary headers in the pane | Pyte-screen flicker watcher emits `agent_started` / `agent_stopped` events for downstream consumers; visual continuity is the terminal scroll itself |
| Cross-pane fan-out of native tool invocations (`Read`, `Edit`, `Bash`, `Grep`, …) | `coord_*` fan-out via bus events is unchanged; native-tool fan-out is gone for this runtime |
| Token-by-token `text_delta` rendering | Terminal renders the CLI's own streaming output |
| Mid-turn cancellation precision | Coarser: SIGINT (Ctrl-C) to the pty; the CLI handles cleanup |
| Image attachments as first-class user-message content | Operator pastes image → harness writes to `/data/attachments/` → injects a markdown path reference in the user message → agent uses `Read` on the path (CLI handles image content) |
| AskUserQuestion routed to EnvPane attention strip (for built-in CLI tool) | Strong system-prompt nudge tells agents to prefer `coord_request_human` for routing-eligible questions; CLI's built-in still works inside the terminal for in-pane interaction |
| PreToolUse hook (truth/ write block) at SDK callback layer | Replaced by bubblewrap FS-layer enforcement (§E) |
| Event log for operator-typed terminal input | Composer submissions still log as `user_message` events; direct terminal typing is operator's responsibility, not logged |

### A.5 What's preserved (no change)

- All `coord_*` MCP tools and their bus events.
- Inter-agent messaging via `coord_send_message` → bus → wake target.
- Task lifecycle (kanban v2, `project_events`, recent-events
  rollups).
- Compass auto-audit watcher.
- Telegram bridge (inbound message → coord_send_message → wake;
  outbound on idle — Coach not on this runtime so no v1 change).
- Watchdog (idle signal source rewires from "no tool_use in 10 min"
  to flicker-watcher state + recent `agent_*` events).
- Recurrence scheduler (Coach not on this runtime so no change).
- File-write proposal flow (`coord_propose_file_write`).
- Skill loading from `~/.claude/skills/` and project paths.
- Memory commons, decisions, knowledge, outputs, attachments,
  uploads, wiki — all `coord_*`-mediated.
- kDrive mirror, event-log retention, snapshot retention.
- HARNESS_TOKEN auth gate, `audit_actor` on destructive endpoints.
- **Project-level `<worktree>/CLAUDE.md` semantics for SDK and Codex
  runtimes** (the existing reconciliation flow, auto-load via
  `setting_sources` for Claude SDK, manual injection for Codex).
  This runtime does NOT trigger a refactor of the other two runtimes.

---

## B. Runtime abstraction

### B.1 Shape

`ClaudeCliRuntime` lives at `server/runtimes/claude_cli.py` and
implements the existing `AgentRuntime` protocol from
`server/runtimes/base.py`. The dispatcher in `agents.run_agent`
remains runtime-agnostic; the runtime resolution chain in
`_resolve_runtime` gains a third arm:

```
agent_project_roles.runtime_override  → 'claude' | 'codex' | 'claude_cli'
team_config[<role>_default_runtime]   → role default
'claude'                              → fallback
```

`'claude_cli'` is gated behind `HARNESS_CLAUDE_CLI_ENABLED`. When the
gate is off, the override is silently demoted to `'claude'` at spawn
time with a logged warning, matching the Codex-gate precedent.

### B.2 What `ClaudeCliRuntime` owns

- Pty subprocess lifecycle (lazy-spawn, prompt injection, cancel via
  SIGINT, close on shutdown / runtime transfer / idle-terminate).
- Pyte-screen flicker watcher (§D).
- Compiled CLAUDE.md writer (§F).
- `.mcp.json` writer per-worktree pointing at the `coord_*` stdio
  proxy.
- Bubblewrap envelope (§E).
- xterm.js WS bidirectional bridge: streams raw pty bytes to the
  client; accepts client keystroke bytes back into the pty (§H).
- Scrollback log persistence to disk (§I).
- Session persistence via `--resume <session-uuid>` and
  `$CLAUDE_CONFIG_DIR`.
- Manual `/compact` flow (delegated to the CLI; harness detects via
  new JSONL session file appearing under `cwd-hash/`).
- `coord_*` proxy token lifecycle (per-subprocess; minted at pty
  spawn, revoked at pty close; Codex precedent).

### B.3 What `ClaudeCliRuntime` does NOT own

- Cost accumulation. No `_extract_usage`, no `turns` ledger row per
  spawn. The dispatcher's pre-spawn cost-cap guard short-circuits
  with `cost_basis="claude_cli"` and skips the read.
- Auto-compact trip-wire. `maybe_auto_compact(tc)` returns False
  unconditionally on this runtime.
- Structured event extraction. No `tool_use` / `tool_result` events
  emitted by this runtime.
- AskUserQuestion routing via callback (no SDK equivalent).
- Per-turn system_prompt assembly of identity / role / brief / lock /
  playbook — these live in the compiled CLAUDE.md (§F) instead.

### B.4 Protocol surface

Implements `AgentRuntime` methods. Key contract points:

| Method | Behavior |
| --- | --- |
| `prepare(tc)` | Ensure pty alive (lazy-spawn or wake from idle-terminate). Rebuild compiled CLAUDE.md (§F.3). Rewrite `.mcp.json` if MCP servers changed since last spawn. Build bubblewrap argv. Verify CLI binary version against pinned range. |
| `send_prompt(tc)` | Bracketed-paste the composed per-turn body into the pty's master fd, then `\r`. Emit `agent_started` immediately. |
| `wait_for_turn_end(tc)` | Block on the flicker watcher firing `agent_stopped` (idle sustained ≥ `HARNESS_CLI_RUNTIME_IDLE_S`). |
| `cancel(tc)` | Write `\x03` (SIGINT byte) to the pty master fd. Best-effort. |
| `close(tc)` | Send `\x04` (EOF) then `kill -TERM` if subprocess doesn't exit within `HARNESS_CLI_RUNTIME_CLOSE_S`. Persist `session_id` to `agent_sessions.session_id`. Revoke `coord_*` proxy token. |
| `maybe_auto_compact(tc)` | Returns False. Always. |
| `extract_session_id(tc)` | Tail `$CLAUDE_CONFIG_DIR/projects/<cwd-hash>/` for newest JSONL filename uuid. Populated lazily after `send_prompt`. |

---

## C. Per-slot pty lifecycle

### C.1 One pty per active slot

Each Player slot using the CLI runtime owns one long-lived `claude`
subprocess inside a pty. Mapping in a module-level dict in
`claude_cli.py`:

```python
_clients: dict[str, CliClient] = {}  # slot -> CliClient
```

`CliClient` carries:
- `master_fd: int` — pty master file descriptor
- `process: subprocess.Popen` — the `bwrap … claude …` process
- `screen: pyte.Screen` — current emulated screen state
- `stream: pyte.ByteStream` — feeds bytes into the screen
- `last_change_at: float` — monotonic timestamp of last screen-hash change
- `last_hash: bytes` — last computed screen-content hash
- `working_since: float | None` — None if idle; else timestamp of latest "working" transition
- `session_id: str | None` — session uuid, updated as the CLI writes JSONL files
- `lock: asyncio.Lock` — serializes prompt injections (one turn at a time)
- `ws_subscribers: set[WebSocket]` — clients streaming the terminal
- `coord_token: str` — minted at spawn, revoked at close
- `scrollback_path: pathlib.Path` — `/data/projects/<id>/.harness/terminal-<slot>.log`

### C.2 Lazy-spawn, idle-terminate

Pty does NOT spawn at boot. Spawns on first `prepare(tc)` call for a
slot AND auto-terminates after
`HARNESS_CLI_RUNTIME_IDLE_TERMINATE_MIN` minutes (default 30) of no
`send_prompt` activity.

On wake, the runtime respawns with `claude --resume <session-uuid>`
so conversation continuity is preserved. Cold-wake latency ~2-5 s.
Operator sees a brief respawn placeholder in the pane during warmup;
scrollback (§I) replays previously-rendered terminal contents from
disk so the operator doesn't lose context.

### C.3 Spawn argv

```
bwrap \
  --bind <worktree> <worktree> \
  --ro-bind <worktree>/truth <worktree>/truth \
  --ro-bind <worktree>/.harness/CLAUDE-compiled.md <worktree>/.harness/CLAUDE-compiled.md \
  --ro-bind <worktree>/.harness/.mcp.json <worktree>/.harness/.mcp.json \
  [optional per-project: --ro-bind on additional protected paths] \
  --bind /data /data \
  --ro-bind /etc /etc \
  --ro-bind /usr /usr \
  --ro-bind /lib /lib \
  --proc /proc --dev /dev \
  --share-net \
  --setenv CLAUDE_CONFIG_DIR /data/claude \
  --setenv TERM xterm-256color \
  --setenv HARNESS_COORD_PROXY_TOKEN <minted> \
  --setenv ENABLE_TOOL_SEARCH auto:30 \
  --chdir <worktree> \
  -- \
  claude \
    [--resume <session-uuid>] \
    --mcp-config <worktree>/.harness/.mcp.json \
    --setting-sources user \
    --append-system-prompt-file <worktree>/.harness/CLAUDE-compiled.md \
    --model <resolved-model> \
    --permission-mode <resolved-mode>
```

Notes:
- `--share-net` is required for the CLI to reach `api.anthropic.com`.
- `--bind /data /data` covers OAuth credentials, attachments,
  uploads, outputs, projects, snapshots — everything the agent
  needs read/write access to (subject to the targeted ro-binds).
- `<worktree>/truth` ro-bind is the load-bearing security boundary
  (§E).
- `<worktree>/.harness/CLAUDE-compiled.md` ro-bind prevents agents
  from tampering with their own identity/role/brief context.
- `<worktree>/.harness/.mcp.json` ro-bind prevents an agent from
  redirecting `coord_*` to a malicious MCP server.
- `--setting-sources user` suppresses the CLI's auto-discovery of
  the worktree CLAUDE.md, so the compiled file (which includes
  project CLAUDE.md content) is the single source of project context
  to the CLI. **Verified by spike M.5.**
- The CLI's `--append-system-prompt-file` re-reads on every spawn;
  flags don't persist across `--resume` (per the verified doc
  behavior). We re-pass on every invocation.

### C.4 Pty allocation

```python
master_fd, slave_fd = pty.openpty()
fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 200, 0, 0))
proc = subprocess.Popen(
    argv,
    stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
    close_fds=True,
    start_new_session=True,
)
os.close(slave_fd)
```

Mirrors the OAuth login flow at `server/claude_login.py`. 200×60 fits
the CLI's modal dialogs and gives the screen-hash watcher a stable
grid.

### C.5 Per-slot reader task

```python
async def _reader(client: CliClient):
    while not client.process_dead:
        data = await loop.run_in_executor(None, os.read, client.master_fd, 4096)
        if not data:
            break
        client.stream.feed(data)                   # update pyte screen
        await _broadcast_ws(client, data)          # forward to xterm.js
        await _append_scrollback(client, data)    # persist to disk
        _maybe_update_session_id(client)
```

The screen-content hash watcher (§D) runs as a separate async task
sampling the screen periodically — independent of the reader's pace,
robust against bursty output.

---

## D. Idle detection — screen-flicker watcher

### D.1 Model

Watch the agent's terminal screen the way a human watches a TUI: if
anything changes — spinner, streaming text, status line update — the
agent is working. If the screen has been still for several seconds,
the agent is done. Intentionally version-independent: no
pattern-match on prompt glyphs, footers, or spinner characters.

### D.2 Sampling

Per-slot async task sleeps `HARNESS_CLI_RUNTIME_SAMPLE_MS`
(default 500 ms), computes a hash of the rendered screen content,
updates `last_change_at` if the hash differs.

```python
def _screen_hash(screen: pyte.Screen) -> bytes:
    # Concatenate visible lines, strip trailing whitespace per line.
    # Cursor position and color/style attrs are excluded so a blinking
    # cursor on a static screen does NOT register as change.
    lines = ["".join(ch.data for ch in row).rstrip() for row in screen.buffer.values()]
    return hashlib.sha256("\n".join(lines).encode()).digest()
```

### D.3 State machine

| Previous state | Hash changed? | Quiescent ≥ T_IDLE? | New state | Event |
| --- | --- | --- | --- | --- |
| idle | yes | n/a | working | `agent_started` (if first since `send_prompt`) |
| working | yes | n/a | working | none |
| working | no | no | working | none |
| working | no | yes | idle | `agent_stopped` |
| idle | no | n/a | idle | none |

### D.4 Tuning knobs

| Env var | Default | Purpose |
| --- | --- | --- |
| `HARNESS_CLI_RUNTIME_SAMPLE_MS` | 500 | Hash sampling cadence |
| `HARNESS_CLI_RUNTIME_IDLE_S` | 5 | Quiescence threshold before idle declared |
| `HARNESS_CLI_RUNTIME_IDLE_TERMINATE_MIN` | 30 | Lazy-terminate after this much idle |
| `HARNESS_CLI_RUNTIME_CLOSE_S` | 10 | SIGTERM timeout on close |
| `HARNESS_CLI_RUNTIME_PROMPT_TIMEOUT_S` | 1800 | Max wait for `agent_stopped` after `send_prompt`; on timeout, log warning, emit `agent_stopped{reason="timeout"}`, return |
| `HARNESS_CLI_RUNTIME_PIN_VERSION` | unset | Expected `claude --version` string; spawn refuses on mismatch |
| `HARNESS_CLI_RUNTIME_SCROLLBACK_MAX_MB` | 10 | Per-slot scrollback log cap |
| `HARNESS_CLAUDE_CLI_ENABLED` | true | Emergency kill switch |
| `HARNESS_OAUTH_REFRESH_GUARD_S` | 30 | Refresh-window serialization threshold |

### D.5 Known false-idle conditions

Acknowledged degradations:

- **Long silent Bash** (e.g. `npm install` early phase, slow tests).
  Agent is working, screen is static, watcher fires idle after
  `T_IDLE`. Operator raises `T_IDLE` for affected workflows or sends
  a fresh prompt to confirm the agent is alive.
- **Agent waiting on AskUserQuestion** (CLI built-in). Screen static
  showing the modal. Watcher fires idle. Arguably correct — the
  agent IS idle, awaiting human input. Operator answers in-pane via
  direct keystroke (§H). Routing to Telegram / EnvPane requires the
  agent to use `coord_request_human` instead (system prompt nudge in
  §G.6).
- **Streaming output paused briefly** (network hiccup, GC pause). If
  the pause exceeds `T_IDLE` the watcher misreads it as turn-end.
  Mitigation: keep `T_IDLE` ≥ 3 s. Lower values trade reliability
  for latency.

### D.6 Telegram-outbound flush

Telegram outbound flushes on Coach's `agent_stopped` events. Coach
is not on this runtime in v1; no change needed.

### D.7 Watchdog rewire

The Haiku-tiered watchdog at `server/kanban_watchdog.py` reads
`agents.status` and the recent event log. On this runtime,
`agents.status` flips `working → idle` based on the flicker watcher
(§D.3) and recent events do NOT include `tool_use`. The
candidate-set SQL filter widens to "status='working' with no
`agent_*` event in 10 min OR status='idle' with `current_task_id`
set + no recent activity for 10 min." Tier-2 LLM context becomes
"last 10 `coord_*` events + task state" instead of "last 10 events
including native tool calls" — slightly weaker signal but the
watchdog already concentrates on orchestration-level findings.

---

## E. Bubblewrap envelope — FS-layer hook parity

### E.1 Why

The harness's truth/ write-block invariant cannot be enforced inside
the CLI's tool layer the way it was via the SDK's PreToolUse hook
callback. The CLI's Edit / Write / Bash tools execute without the
harness intercepting them beforehand. The replacement is FS-layer
enforcement: bind-mount protected paths read-only.

### E.2 Required ro-bind paths

Always:
- `<worktree>/truth` (recursive)
- `<worktree>/.harness/CLAUDE-compiled.md` (the harness-compiled file)
- `<worktree>/.harness/.mcp.json` (MCP config)

Optional (extensible later via project-level config in
`team_config[cli_runtime_ro_paths_<id>]`): additional worktree-relative
paths.

### E.3 Why bubblewrap specifically

`bwrap` is already installed in the container (since 2026-05-05).
User-namespace-based, requires no root, supports recursive ro-binds.
No chroot, no LXC, no FUSE. The CLI runs as the same uid; only the
FS view differs.

### E.4 Subprocess inheritance

Child processes the CLI spawns (Bash invocations of `npm`, `pytest`,
`git`, `python`, …) inherit the bubblewrap namespace and cannot
escape the ro-bind. Desired behavior — `echo x > truth/foo.md` fails
with EROFS, surfaced in the CLI's Bash tool output.

### E.5 Harness server is OUTSIDE the sandbox

The harness's Python process runs outside bubblewrap. When
`coord_propose_file_write` is approved by the operator, the harness
server writes to the truth/ path with normal filesystem permissions.
The agent's path is via `coord_*`, not via the CLI's Edit tool. The
"agents cannot write truth/ directly but can propose changes for
approval" invariant is preserved.

### E.6 Per-project ro-bind extension

Future projects may want additional protected paths. Config shape:

```sql
key = 'cli_runtime_ro_paths_<project_id>'
value = JSON: ["specs/", "vendor/", ".github/workflows/"]
```

Runtime resolves at spawn time and adds matching `--ro-bind` lines.
v1 ships with the defaults in §E.2 only.

### E.7 Spike

See §M.1.

---

## F. Two CLAUDE.md files

### F.1 Architecture overview

**File 1 — `<worktree>/CLAUDE.md`** (project CLAUDE.md):
- Today's behavior, unchanged.
- Coach-maintained via the existing `update_claude_md_via_coach`
  reconciliation flow at `server/project_claude_md.py`.
- Agents read and write via the Edit tool (today's behavior).
- SDK runtime: auto-loaded via `setting_sources`.
- Codex runtime: manually injected as part of the system prompt.
- CLI runtime: NOT auto-loaded (suppressed via `--setting-sources
  user`); content is included in File 2.

**File 2 — `<worktree>/.harness/CLAUDE-compiled.md`** (harness
compiled file, CLI-runtime-only):
- Harness-generated. No LLM involvement.
- Content = full verbatim of File 1 + harness-managed sections
  (identity, role, brief, lock state, playbook lattice, handoff
  suffix when applicable).
- Bubblewrap ro-binds it so CLI agents can't modify.
- Loaded by the CLI via `--append-system-prompt-file`.

**SDK and Codex runtimes are NOT affected by File 2.** They continue
to compose identity / role / brief / lock per-turn in
`run_agent`'s system_prompt assembly. The runtime-aware refactor that
would unify per-turn context into CLAUDE.md across all three runtimes
is **explicitly deferred** to a possible Phase 2; v1 isolates the new
behavior to the CLI runtime only.

### F.2 Compiled file content

```markdown
<-- Verbatim copy of <worktree>/CLAUDE.md content -->

<!-- ============================================================ -->
<!-- HARNESS-MANAGED CONTEXT BELOW                                -->
<!-- Generated automatically by the harness on every CLI spawn.   -->
<!-- DO NOT EDIT — this file is regenerated on every relevant     -->
<!-- change.                                                      -->
<!-- ============================================================ -->

## Your identity
You are **<Name>** (slot <slot-id>), <role-title> — <role-description>.

## Your role
<role_baseline content — same shape as today's SDK-runtime injection>

## Your operator brief
<brief text, or the literal "(none set)">

## Lock state
<"unlocked" or "locked">

## Playbook lattice
<playbook block content — same shape as today's playbook injection>

<!-- Optional section, present only when applicable -->
## Notes from your prior session (via /compact)
<handoff_suffix content>
```

The handoff_suffix section appears in the file ONLY when
`agent_sessions.continuity_note` or
`agent_sessions.last_exchange_json` indicates a recent compact /
runtime-transfer boundary. After the first successful non-compact
turn in the new session, harness clears the continuity_note column;
next compiled-file rebuild omits the section.

### F.3 Rebuild triggers

The harness rebuilds `CLAUDE-compiled.md` on:

1. **Every CLI spawn** — initial, lazy-resume, post-compact respawn
   (if M.6 confirms a respawn is needed), runtime transfer.
2. **Mid-session changes** to inputs:
   - `<worktree>/CLAUDE.md` content changes (Coach reconciliation,
     agent Edit, manual operator edit via Files pane). Detected via
     mtime comparison stored in `agent_project_roles.claude_md_mtime`.
   - Brief: PUT /api/agents/{id}/brief, gear save, or a future
     `coord_set_player_brief`.
   - Role/name: `coord_set_player_role`.
   - Lock toggle.
   - Playbook lattice change (proposal accepted).

The rebuild is dirt-cheap (string concat + file write). Always
rebuild on spawn instead of relying on watchers; rebuild on
mid-session change only to keep the file fresh for the NEXT spawn.

### F.4 Refresh propagation to live agent

The CLI snapshots its system prompt (including `--append-system-prompt-file`
content) at session start. Mid-session rebuilds of
`CLAUDE-compiled.md` do NOT propagate to the running CLI process.

Two refresh triggers (verified doc behavior):

- **Next session start** — lazy-resume, "Clear session" button,
  runtime transfer, operator-triggered respawn.
- **`/compact`** — re-reads project-root CLAUDE.md per
  [the official memory doc](https://code.claude.com/docs/en/memory.md);
  Spike M.6 confirms whether the `--append-system-prompt-file`
  content is also re-read on `/compact`. If yes, `/compact` is a
  full context refresh for both files. If no, a respawn is required.

When the compiled file is rewritten during a live session, the pane
header shows a "context updated; restart slot or /compact to apply"
badge. Badge clears on next session start or `/compact` detection.

### F.5 Compile-file writer module

New module: `server/cli_runtime_compile.py`. Public surface:

```python
async def rebuild(project_id: str, slot: str) -> None:
    """Rewrite <worktree>/.harness/CLAUDE-compiled.md for this slot.
    Reads:
      - <worktree>/CLAUDE.md (project content)
      - agent_project_roles row (identity name, role, brief, lock,
        permission_mode_override, model_override, etc.)
      - playbook lattice via existing helpers
      - agent_sessions row (continuity_note for handoff section)
    Writes file atomically (tempfile + os.replace).
    """
```

### F.6 Why `--setting-sources user`

The CLI's default `--setting-sources` includes `project`, which
triggers auto-discovery of `<cwd>/CLAUDE.md` and its parents.
Without overriding, the CLI would load `<worktree>/CLAUDE.md` twice:
once via auto-discovery, once embedded in
`<worktree>/.harness/CLAUDE-compiled.md` via `--append-system-prompt-file`.
Double-load creates redundant prompt bloat and breaks the "the
compiled file is the single source" mental model.

Setting `--setting-sources user` keeps the user-level memory
(`~/.claude/CLAUDE.md`, `MEMORY.md`) auto-loading but suppresses
project-level discovery. **Spike M.5 verifies this.**

If M.5 reveals that `--setting-sources` does NOT control
CLAUDE.md auto-discovery (i.e. CLAUDE.md is its own discovery
mechanism, not gated by setting_sources):

- Fallback (a): accept the double-load. Prompt bloat is bounded by
  project CLAUDE.md size; cache still holds because content matches
  byte-for-byte (the compiled file's prefix IS the project CLAUDE.md
  content).
- Fallback (b): replace `--append-system-prompt-file` with
  `--system-prompt-file` (which REPLACES the default system prompt
  entirely). Risk: lose CLI's default safety/tool guidance. We'd
  have to re-add critical pieces in our compiled file. Avoid unless
  (a) is also infeasible.

---

## G. Prompt composition for CLI runtime

### G.1 What lives where

| Content | Location | Refresh cadence |
| --- | --- | --- |
| Identity prefix | Compiled CLAUDE.md (§F.2) | Session start / `/compact` |
| Role baseline | Compiled CLAUDE.md | Session start / `/compact` |
| Brief | Compiled CLAUDE.md | Session start / `/compact` |
| Lock state | Compiled CLAUDE.md | Session start / `/compact` |
| Playbook lattice | Compiled CLAUDE.md | Session start / `/compact` |
| Handoff suffix | Compiled CLAUDE.md | Session start; cleared after first non-compact turn |
| Coordination block (Coach-only) | n/a — Coach not on this runtime in v1 | — |
| Prior error suffix | Per-turn user message preface | Each turn (when applicable) |
| User message | Per-turn user message | Each turn |

### G.2 Composed per-turn injection

For most turns the per-turn injection is just the user message.
When `prior_error_suffix` applies, format:

```
[Prior turn note]
<prior_error_suffix>

[Your prompt]
<user message>
```

When neither suffix applies, the injection is the bare user message
+ trailing newline.

### G.3 Bracketed-paste injection

```python
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
SUBMIT = b"\r"

async def send_prompt(client: CliClient, body: str):
    async with client.lock:
        # Short idle wait so we don't inject mid-render.
        await _wait_short_idle(client, 0.25)
        os.write(client.master_fd, PASTE_START + body.encode() + PASTE_END + SUBMIT)
        client.working_since = time.monotonic()
```

Spike M.2: confirm CLI accepts a 30 KB bracketed-paste blob as one
user message. If chunking is required, split on paragraph boundaries
and inject sequentially without an intermediate `\r`.

### G.4 Slash-command discipline

**Composer-typed slashes** are intercepted client-side (as today)
and routed by the harness. On this runtime:

| Slash | Behavior |
| --- | --- |
| `/compact` | Inject `/compact\r` into pty. Harness watches for new JSONL session uuid landing under `cwd-hash/` to confirm compaction. Compiled file is rebuilt with cleared handoff (or new handoff from `continuity_note` if a transfer accompanies it). |
| `/clear` | Inject `/clear\r` literally. Harness emits a `pane_cleared` marker for WS clients; scrollback log gets a separator line. |
| `/cost`, `/status` | Inject literally. Output renders in terminal; not parsed. |
| `/model` | Harness-owned (updates `agent_project_roles.model_override`). Not forwarded to pty directly. Takes effect on next spawn. Operator sees the "context updated" badge. |
| `/effort` | Harness-owned (updates override column). ALSO forwarded literally to pty so the CLI's native `/effort` takes effect immediately within the current session. Harness records the new value to the override so it persists across spawns. |
| `/plan` | Same dual handling as `/effort`. |
| `/thinking` | Harness-owned only (no CLI slash equivalent — `Option+T` keystroke is not driveable from outside the pty). Takes effect on next spawn. |
| `/brief`, `/tools`, `/tick`, `/spend`, `/help`, `/loop`, `/truthscore` | Harness-owned, unchanged behavior. |
| Unrecognized `/foo` | Injected literally; CLI's own handler responds. |

**Terminal-typed slashes** (operator types directly in the xterm.js
pane): harness does NOT intercept. Whatever the operator types
reaches the CLI raw. The harness loses visibility for that exchange.
This is the accepted "direct interaction" escape hatch — operator's
call.

### G.5 Image attachments

Operator pastes image into composer → harness writes to
`/data/attachments/<uuid>.<ext>` → composed prompt includes:

```
[Attached image: /data/attachments/<uuid>.png — use Read to view]
```

Agent calls `Read /data/attachments/<uuid>.png`; CLI's Read tool
inlines the image as visual content to the model. No first-class
"image in user message" path on this runtime.

### G.6 AskUserQuestion routing nudge

The compiled CLAUDE.md's role section includes an addendum:

> You are running inside an interactive Claude Code CLI session
> driven by the TeamOfTen harness. The harness watches your terminal
> screen to know whether you are working or idle. For human-routable
> questions (clarification, approval, decisions that should reach the
> operator via the EnvPane attention strip and Telegram), prefer
> `coord_request_human`. Use the built-in AskUserQuestion only when
> you need an in-pane confirmation that does NOT need to escalate
> off-channel.

The CLI's built-in AskUserQuestion remains available — no
permissions deny — because no CLI flag exists to suppress it.

### G.7 Auto-wake

Inter-agent wakes (`coord_send_message`, `coord_assign_task`, kanban
stall rung 1, watchdog `finished_not_reported`, etc.) use the same
`maybe_wake_agent` path. On this runtime, "waking" means:

1. Compose the wake body (existing harness logic).
2. Ensure target's pty is alive (lazy-spawn if not).
3. Wait until the target's flicker state is idle.
4. Inject the wake body via bracketed-paste.

Mid-turn wakes queue in `_pending_wakes` and fire when the current
turn's `agent_stopped` lands. Identical semantics to today.

---

## H. Terminal interaction model

### H.1 Two surfaces both submit to the same pty

**Composer (above terminal)**:
- Harness-composed prompts (bracketed-paste with composed per-turn
  body).
- Composer Submit logs a `user_message` event for the event log +
  cross-pane history.
- Slash-command intercept per §G.4.

**Terminal (xterm.js pane)**:
- Always-forward keystrokes — operator can type at any time.
- Useful for modal selectors, native /commands the harness doesn't
  intercept, ad-hoc clarifications, Ctrl-C to interrupt.
- Harness does NOT log terminal-typed input as a `user_message`
  event.
- Harness does NOT intercept terminal-typed slashes.
- Direct interaction = harness out of the loop for that exchange.

### H.2 Flicker watcher as the "safe to type" cue

The pane header reflects current state (idle / working) based on the
flicker watcher. Operator uses this as the cue for whether to
interrupt vs wait. No additional status light or rate limiting.

### H.3 No keystroke filtering

Whatever the operator types reaches the pty raw. Including Ctrl-C
(SIGINT to the CLI), arrow keys for modal selectors, Esc to dismiss,
multi-line input.

### H.4 WebSocket bidirectional binary protocol

- Server → client: raw pty bytes streamed as binary WS frames.
- Client → server: raw keystroke bytes (xterm.js's `onData` event)
  streamed as binary WS frames.
- No JSON wrapping for terminal data; bandwidth-optimal.
- Existing harness WS message types (events, status updates, etc.)
  remain JSON-wrapped on a separate sub-channel.

### H.5 Multiple operators on the same pane

If two operators both have the pane open, both receive the byte
stream (broadcast to all `ws_subscribers`). Both can type; their
keystrokes interleave at the pty. This matches today's
multi-operator behavior in the harness.

---

## I. Scrollback persistence

### I.1 Per-slot log file

Path: `/data/projects/<project-id>/.harness/terminal-<slot>.log`.

Format: append-only file containing raw pty bytes (same bytes
streamed to xterm.js). Includes ANSI escapes; xterm.js renders
identically on replay.

### I.2 Size cap and truncation

Cap: `HARNESS_CLI_RUNTIME_SCROLLBACK_MAX_MB` (default 10 MB).

When file exceeds cap:
- Find first newline N bytes from start such that
  `file_size - N <= cap × 0.9` (truncate to 90% of cap so we don't
  retruncate on every byte).
- Rewrite file from position N onwards.
- Oldest scrollback is lost; recent stays intact.

Truncation runs periodically (every 5 min when the file is hot) to
avoid per-byte rewrites.

### I.3 Session boundaries within the log

On lazy-terminate, harness appends a separator:

```
\r\n--- session ended <ISO8601 ts>; sessionId=<uuid> ---\r\n
```

On respawn (lazy-resume), harness appends a header:

```
\r\n--- session resumed <ISO8601 ts>; sessionId=<new-uuid> ---\r\n
```

Continuous file across lazy-respawns.

### I.4 WS client replay on connect

When a client opens the pane WS:
1. Harness reads the full log file.
2. Streams contents to the client as binary WS frames.
3. Client's xterm.js consumes; rendering catches up to last known
   state.
4. Subsequent live bytes stream normally.

### I.5 Deletion

Log file is deleted on:
- Full session-clear (operator clicks "Clear session" button on
  the pane).
- Runtime transfer (slot moves off `claude_cli`).

Log file is NOT deleted on:
- Lazy-terminate (continuous file across respawns).
- Container redeploy (persists via /data volume).

---

## J. Session persistence

### J.1 `$CLAUDE_CONFIG_DIR` persists across redeploys

- `/data/claude` is mounted on the persistent volume.
- OAuth credentials + per-session JSONL transcript files survive
  container redeploys.

### J.2 `--resume <session-uuid>`

After the first `send_prompt` on a fresh session, the CLI writes the
new session's JSONL file under
`$CLAUDE_CONFIG_DIR/projects/<cwd-hash>/`. Runtime tails the
directory and records the uuid as `agent_sessions.session_id` (same
column the SDK runtime uses).

On boot, lazy-terminate respawn, or operator-triggered restart,
runtime spawns with `--resume <session-uuid>` plus all the other
flags (since flags don't persist across resume per the verified doc
behavior).

If `--resume` fails (uuid no longer found), runtime falls back to
spawn without `--resume`; emits `session_resume_failed`. Stale-session
auto-heal (§J.5) takes over.

### J.3 `/compact` continuity extraction

When operator types `/compact` (or the agent uses it natively):
- CLI runs native compact; summarizes session; starts fresh session
  with the summary; new JSONL file appears under `cwd-hash/`.
- Harness detects the new JSONL filename, records new uuid as
  `agent_sessions.session_id`.
- Harness rebuilds CLAUDE-compiled.md (project content potentially
  changed too).

The CLI's native compact format does NOT produce a structured
continuity_note for the harness. `agent_sessions.continuity_note`
gets an empty string; `agent_sessions.last_exchange_json` (rolling
per-turn exchange log, populated by the dispatcher independently of
runtime) carries verbatim recent exchanges as the structured
backup.

When the operator initiates a runtime transfer FROM the CLI runtime
to another runtime, the harness uses `last_exchange_json` as the
continuity material (identical to the stale-session auto-heal
pattern from 2026-05-06).

Open: Spike M.6 verifies whether `/compact` preserves the
`--append-system-prompt-file` content in memory or requires a
respawn for re-application.

### J.4 OAuth refresh concurrency

All CLI-runtime slots share `/data/claude/.credentials.json`. CLI
presumably handles file-locking but was designed for single-instance
use. Mitigation:

- A harness-side `asyncio.Lock` serializes `send_prompt` operations
  across all slots ONLY during a window where the OAuth token is
  within `HARNESS_OAUTH_REFRESH_GUARD_S` seconds of expiry. Outside
  the window, prompts inject concurrently.
- Refresh-guard window check reads the OAuth credential file's
  `expires_at` field. If the format ever changes, guard degrades to
  always-on serialization (no correctness impact, only latency).

Spike: launch 5 CLI subprocesses simultaneously inside the guard
window and verify credentials file is not corrupted (defer to soak
testing).

### J.5 Stale-session auto-heal

Generalized from the 2026-05-06 SDK-side auto-heal pattern. On this
runtime:

- `--resume` failure → emit `session_resume_failed`; retry once
  without `--resume`. Fresh session, with synthetic handoff_suffix
  in the compiled CLAUDE.md sourced from `last_exchange_json`.
- pty subprocess dies mid-turn → emit `runtime_subprocess_died`,
  log stderr if any, attempt one respawn. If respawn fails, emit
  `human_attention`. **No auto-fallback to SDK runtime.**
- Bubblewrap fails to launch → fatal at spawn. Emit
  `runtime_sandbox_failed{reason}` + `human_attention`. Operator
  intervenes; slot stays unusable until manually fixed or runtime
  transferred (operator-triggered).

---

## K. Runtime transfer

### K.1 Existing mechanism extended

The 2026-05-02 `_perform_runtime_transfer_flip` at `server/agents.py`
handles SDK ↔ Codex transfers. Extended to accept `'claude_cli'` as a
third valid value.

### K.2 Transfer matrix

| From → To | Compact on source? | Continuity source | Behavior |
| --- | --- | --- | --- |
| `claude` → `claude_cli` | Yes (SDK `/compact`) | `continuity_note` (well-formed) | Existing SDK compact extracts continuity_note. Target spawn rebuilds CLAUDE-compiled.md with handoff_suffix section populated. |
| `codex` → `claude_cli` | Yes (Codex `compact_thread`) | `continuity_note` | Same. |
| `claude_cli` → `claude` | Yes (CLI `/compact` injected; wait for idle) | `last_exchange_json` | CLI's compact doesn't produce a structured continuity_note. Target SDK session starts with handoff suffix from last_exchange_json. |
| `claude_cli` → `codex` | Same | `last_exchange_json` | Same shape. |
| `claude_cli` → `claude_cli` | No-op | n/a | 200 noop. |
| Any with source broken (`?force=true`) | Skip compact | `last_exchange_json` | Forced-transfer path; existing `force=true` query param. Operator's deliberate escape from a stuck runtime. |

### K.3 UI surface

Pane gear popover runtime selector adds "Claude Code CLI" option:

```
Runtime:  ◯ Claude SDK   ◯ Codex SDK   ● Claude Code CLI
```

POST /api/agents/{id}/transfer-runtime handles all six transitions.
Legacy blunt-clear PUT /api/agents/{id}/runtime accepts `'claude_cli'`
for empty-clear semantics.

### K.4 MCP tool surface

`coord_set_player_runtime` (Coach-only MCP tool) accepts
`'claude_cli'` as valid. `_CODEX_TOOL_CONTRACT_VERSION` in
`server/runtimes/codex.py` is bumped to include the new accepted
value so existing Codex threads re-pick up the contract.

---

## L. Operational concerns

### L.1 Memory budget

11 long-running Node-side `claude` processes ≈ 11 × (200-400 MB) ≈
2-4 GB. Container default on Zeabur may be 2 GB.

Mitigations:
- **Lazy-spawn**: pty only spawns on first turn after operator
  activates the slot.
- **Idle-terminate**: pty respawns with `--resume` after
  `HARNESS_CLI_RUNTIME_IDLE_TERMINATE_MIN` minutes idle.
- **Manual opt-in**: v1 has no default `claude_cli` slots; operator
  opts in per slot per project.

Steady-state for v1 testing: 1-2 ptys at most. Broad rollout decision
depends on M.7 measurement.

### L.2 Pinned CLI version

Flicker watcher reliability correlates with CLI rendering behavior.
Surprise version bumps could shift baseline rendering and break
heuristics.

- Dockerfile pins `@anthropic-ai/claude-code` to a known-good range
  (existing precedent).
- `HARNESS_CLI_RUNTIME_PIN_VERSION` env declares an exact or wildcard
  version (e.g. `2.1.*`); on spawn, runtime probes `claude --version`
  and refuses to start on mismatch.
- CI exercises a representative turn against the pinned version
  weekly to catch behavior drift.

### L.3 Permission mode

CLI modes: `default | acceptEdits | plan | auto | dontAsk |
bypassPermissions`.

**Default for v1: `auto`** (per operator decision). Auto-approves
edits and Bash so the agent doesn't stall waiting for human approval
the harness doesn't surface.

Per-pane override via new column
`agent_project_roles.permission_mode_override` (mirrors
`model_override` / `effort_override` patterns). Operator flips a
slot to `plan` for a planning session, `bypassPermissions` for max
autonomy, etc. Future: `coord_set_player_permission_mode` MCP tool
(deferred to Phase 2).

### L.4 Hooks

CLI hooks live in `.claude/settings.json`. Not used in v1. Per-project
`.claude/settings.json` hook configuration is Phase 2.

### L.5 Skill loading

CLI auto-loads skills from `~/.claude/skills/` and project paths.
Standard behavior preserved.

### L.6 `coord_*` MCP token lifecycle

Per the 2026-04-29 Codex precedent. Per-subprocess token minted at
CLI pty spawn, captured in env via `bwrap --setenv
HARNESS_COORD_PROXY_TOKEN <value>`, read by the coord-proxy
subprocess (spawned by the CLI from `.mcp.json`) on its startup, used
for all `coord_*` calls during that pty's lifetime. Revoked on pty
close (lazy-terminate, runtime transfer, operator kill).

**Not** minted per-turn — that would break the Codex case (2026-04-29
fix) and would break the CLI case identically (long-lived subprocess
holding a now-revoked token).

### L.7 Failure surfaces

| Failure | Event(s) emitted | Operator-visible |
| --- | --- | --- |
| Bubblewrap fails to launch | `runtime_sandbox_failed{reason}`, `human_attention` | EnvPane attention strip + Telegram |
| pty subprocess dies mid-turn | `runtime_subprocess_died`, `human_attention` on second consecutive death | Same |
| `--resume` fails | `session_resume_failed`, retry once without resume | Pane footer note |
| CLI version mismatch | `runtime_version_mismatch{expected, actual}`, refuses to spawn | Pane error card; operator updates pin or CLI |
| `/compact` injection times out | `compact_timeout`, log warning | Pane footer note |

**No auto-fallback to a different runtime on failure.** Operator
manually transfers (or force-transfers) to another runtime if they
choose.

---

## M. Spike plan

Before implementation. Each spike answers a load-bearing assumption.
Run inside the live container; record results in
`Docs/claudeCLI-spikes-results.md`.

### M.1 Bubblewrap envelope

Goal: confirm `bwrap` + `claude` CLI work together; truth/ ro-bind
is enforced; Bash subprocesses inherit; `coord_propose_file_write`
still lands.

Steps:
1. Pick a project. Verify worktree at `/data/projects/<id>/repo/p10`
   exists.
2. Place a sentinel `truth/sentinel.md` in the project's truth/.
3. Run interactively:
   ```
   bwrap --bind <worktree> <worktree> \
         --ro-bind <worktree>/truth <worktree>/truth \
         --bind /data /data --proc /proc --dev /dev --share-net \
         --setenv CLAUDE_CONFIG_DIR /data/claude \
         --chdir <worktree> \
         -- claude
   ```
4. Inside, have the agent: (a) Read truth/sentinel.md (succeed),
   (b) Edit truth/sentinel.md (fail with EROFS rendered by CLI),
   (c) `echo x > /tmp/scratch` (works), (d) `git status` (works),
   (e) `npm --version` (works).
5. From a separate harness terminal, fire
   `coord_propose_file_write` against truth/sentinel.md with
   operator approval. Confirm file updated.

Pass: all 5 steps as described.

### M.2 Bracketed-paste at 30 KB

Goal: confirm CLI accepts a 30 KB bracketed-paste blob as one user
message.

Steps:
1. Build a synthetic 30 KB system+user prompt (paragraphs of varying
   length, embedded code blocks).
2. Inject via `\x1b[200~ … \x1b[201~\r` into a pty running `claude`
   in a fresh worktree.
3. Observe: does CLI process the blob as ONE message? Start
   responding promptly? Render correctly or truncate?

Pass: one message, no truncation, OR a chunking fallback (paragraph
boundaries, no intermediate `\r`) works equivalently.

### M.3 Screen-flicker idle detection

Goal: confirm the screen-hash watcher reliably distinguishes working
from idle across representative turn shapes.

Steps:
1. Run `claude` in a pty with watcher active.
2. Send three test prompts:
   - Quick text response ("what's 2+2"): watcher fires
     `agent_started` within 1 sample, `agent_stopped` within
     `T_IDLE` after response settles.
   - Tool-using prompt ("read foo.md and summarize"): watcher
     remains "working" through tool call streaming, `agent_stopped`
     after final response.
   - Long silent Bash ("run `sleep 30 && echo done`"): watcher
     correctly flags idle during sleep — this is the accepted
     false-idle (§D.5).

Pass: first two scenarios accurate; third behaves as expected.

### M.4 `--mcp-config` interop

Goal: confirm interactive CLI accepts `--mcp-config` and exposes the
listed servers' tools.

Steps:
1. Write a minimal `.mcp.json` pointing at the existing coord stdio
   proxy used by Codex.
2. Spawn `claude --mcp-config /tmp/mcp.json`; ask agent to list
   available tools.
3. Confirm `coord_*` tools appear.
4. Call `coord_list_backlog` from within the CLI; confirm bus emits
   expected event.

Pass: `coord_*` tools visible AND functional.

### M.5 `--setting-sources user` behavior

Goal: confirm `--setting-sources user` suppresses worktree CLAUDE.md
auto-load.

Steps:
1. Place a unique sentinel string `PROJECT_SENTINEL_<uuid>` in
   `<worktree>/CLAUDE.md`.
2. Place a DIFFERENT unique sentinel string `APPEND_SENTINEL_<uuid>`
   in a separate file at `/tmp/append.md`.
3. Spawn with `claude --setting-sources user --append-system-prompt-file
   /tmp/append.md`.
4. Ask the agent: "Which sentinel strings can you see in your system
   prompt?"

Pass: agent reports only `APPEND_SENTINEL_*`. Fail: agent sees both;
adopt fallback (a) double-load acceptance or (b) `--system-prompt-file`
replacement per §F.6.

### M.6 `/compact` preserves `--append-system-prompt-file` content

Goal: confirm `/compact` preserves the append-file content in
memory.

Steps:
1. Spawn CLI with `--append-system-prompt-file` containing identity
   ("you are Alice, slot p3, Developer").
2. Have a conversation (1-2 turns).
3. Trigger `/compact`.
4. After compact completes, ask agent: "Remind me — who are you?"

Pass: agent still knows it's Alice. Fail: agent identity lost; spec
adopts mandatory respawn after `/compact` (kill pty subprocess +
respawn with the flag again).

### M.7 Container memory measurement

Goal: calibrate lazy-spawn / idle-terminate defaults.

Steps:
1. Spawn one `claude` in a pty inside the container.
2. Idle 2 min, record RSS via `ps -o pid,rss,cmd`.
3. Run a representative turn (50 KB prompt, response with one Read
   + one Edit). Record RSS post-turn.
4. Repeat with two simultaneous `claude` processes.

Pass: extrapolation to 2-3 concurrent CLIs fits within container RAM
budget for v1 testing. Broader rollout deferred until container
sized appropriately.

---

## N. Rollout

### N.1 v1 scope

- New runtime `'claude_cli'` registered in runtime catalog.
- `ClaudeCliRuntime` implementation in
  `server/runtimes/claude_cli.py`.
- Pty + flicker watcher + bubblewrap envelope.
- Compiled CLAUDE.md writer at `server/cli_runtime_compile.py`.
- New xterm.js pane component (separate from structured AgentPane).
- Bidirectional WS protocol (binary, raw bytes both directions).
- `.mcp.json` writer per-worktree.
- Scrollback log persistence with continuous-file model.
- Runtime transfer extension (target list includes `claude_cli`).
- New schema column: `agent_project_roles.permission_mode_override`.
- New schema column: `agent_project_roles.claude_md_mtime` (for
  rebuild trigger).
- Coach NOT enabled on this runtime.
- No automatic migration; operator opts in per slot per project via
  the existing pane gear UI.
- Env knobs (defaults in §D.4): `HARNESS_CLAUDE_CLI_ENABLED`,
  `_SAMPLE_MS`, `_IDLE_S`, `_IDLE_TERMINATE_MIN`, `_CLOSE_S`,
  `_PROMPT_TIMEOUT_S`, `_PIN_VERSION`, `_SCROLLBACK_MAX_MB`,
  `HARNESS_OAUTH_REFRESH_GUARD_S`.

### N.2 Validation

- Real task end-to-end on operator-flipped slot: Coach assigns →
  slot's CLI runtime executes → `coord_commit_push` fires → kanban
  advances → Compass audit watcher fires on the commit event.
- Inter-agent message: another agent sends to the CLI slot → wake
  injection → agent sees message in terminal → agent replies via
  `coord_send_message`.
- Runtime transfer: flip a slot from `claude` → `claude_cli` and
  back; continuity preserved via appropriate mechanism each
  direction.
- Bubblewrap protection: agent attempts native Edit on truth/, sees
  EROFS in tool output.
- Reboot survives: redeploy container; slot's pty respawns with
  `--resume`; conversation continuity preserved; scrollback log
  intact on disk.
- Compiled CLAUDE.md refresh: operator edits brief; pane shows
  "context updated" badge; operator triggers `/compact`; agent
  acknowledges new brief content on next turn.

### N.3 Phase 2 (deferred)

- Migrate Players p1..p9 broadly to `claude_cli` (likely default in
  v2).
- Migrate Coach (requires coordination-block strategy — frequent
  `/compact` triggers, or per-turn injection, or per-turn append-file
  rebuild + respawn).
- Per-project ro-bind paths beyond truth/.
- `.claude/settings.json` hook configuration per-project.
- First-class image-content paste.
- Cost rollups via `/status` scraping.
- `coord_set_player_permission_mode` MCP tool.
- Unified prompt-composition refactor across all three runtimes (move
  identity/role/brief/lock/playbook to CLAUDE.md for SDK and Codex
  too — explicitly NOT in v1).

---

## O. Cross-references

- `Docs/TOT-specs.md` — overall harness spec.
- `Docs/CODEX_RUNTIME_SPEC.md` — sibling runtime spec.
- `server/runtimes/base.py` — `AgentRuntime` protocol +
  `TurnContext`.
- `server/runtimes/claude.py` — Claude SDK runtime (reference).
- `server/runtimes/codex.py` — Codex SDK runtime (reference for
  third-runtime patterns).
- `server/runtimes/claude_cli.py` — **new**, this runtime's
  implementation.
- `server/cli_runtime_compile.py` — **new**, compiled CLAUDE.md
  writer.
- `server/coord_mcp.py` — stdio loopback for `coord_*`; reused.
- `server/claude_login.py` — pty + pyte precedent.
- `server/agents.py` — `run_agent` dispatcher,
  `_perform_runtime_transfer_flip`, `maybe_wake_agent`.
- `server/kanban_watchdog.py` — soft-stall watchdog (signal rewire
  per §D.7).
- `server/telegram.py` — outbound flush trigger (Coach-only, unchanged
  for v1).
- `server/project_claude_md.py` — Coach reconciliation flow
  (unchanged — only operates on `<worktree>/CLAUDE.md`, not on the
  compiled file).

---

## P. Open questions (spike-result-dependent)

These firm up during M:

1. **Bracketed-paste chunking protocol** if M.2 reveals a size limit.
2. **CLI respawn vs in-process continuity for `/compact`** depending
   on M.6 outcome (and ditto for runtime transfers FROM `claude_cli`
   that fire `/compact` as the source-side step).
3. **`--setting-sources` vs fallback approaches** if M.5 fails.
4. **Bubblewrap policy for child processes** if M.1 finds spurious
   EROFS in dep trees (e.g. some `npm` package writing to a path we
   ro-bound).
5. **Container memory bump amount** based on M.7.
6. **`/compact` detection** — currently relies on a new JSONL file
   appearing under `cwd-hash/`. Confirm during M.6 that this is a
   reliable signal.

---

End of spec v2.

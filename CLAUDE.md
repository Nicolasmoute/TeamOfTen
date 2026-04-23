# TeamOfTen — Claude Code Harness

A personal orchestration harness for a **team of 11 Claude Code agents — 1 Coach + 10 Players** — sharing memory and a task board, with a multi-pane web UI, deployed to a single VPS (Zeabur). Max-plan OAuth only — no API keys.

**Full spec**: [Docs/HARNESS_SPEC.md](Docs/HARNESS_SPEC.md) — read it before touching server code.

## Team vocabulary

- **Coach** (slot id `coach`) — the coordinator. Receives human goals, decomposes into tasks, assigns work. Never writes code. **Only Coach gives orders.**
- **Players** (slot ids `p1`–`p10`) — workers. Each Player has a **name** (e.g. "Alice") and a **role description** (e.g. "Developer — writes code") both **assigned by Coach** at team-composition time. Players execute work, report back, and may message peers for information — but **Players never give orders** to other Players.
- **Team** — all 11 agents together. "Team of ten" refers to the 10 Player slots; Coach is always on.

---

## Tech stack

- **Backend**: Python 3.12 + FastAPI + WebSocket, single mono-service
- **Agent runtime**: Claude Agent SDK (Python), authenticated via Max-plan OAuth
- **Frontend**: React 18 + TypeScript + Vite + react-mosaic (desktop) / stack+tabs (mobile) + Zustand
- **State**: SQLite (hot path) + kDrive via WebDAV (durable snapshots + human-readable `.md`)
- **Deploy**: Docker container on Zeabur, auto-pulled from this GitHub repo
- **Reverse proxy**: Zeabur handles TLS/ingress (Caddy from the original spec is not needed on Zeabur)

---

## Current state (2026-04-23)

Backend + UI essentially feature-complete for the personal harness. Heavy
self-paced /loop development with no end-to-end verification yet on the
deployed Zeabur instance — see "What needs verification" below.

**Done:**
- **M-1** ✓ Max OAuth + 10-concurrent feasibility (laptop + Zeabur EU)
- **M0** ✓ FastAPI skeleton, Dockerfile, Zeabur auto-deploy from main
- **M1** ✓ One Claude SDK agent streaming to a WebSocket UI
- **M2a** ✓ SQLite state + 11-agent roster (Coach + p1..p10) + first coord_* tools
- **M2b** ✓ Task state machine (`coord_claim_task`, `coord_update_task`)
- **M2c** ✓ Inter-agent chat (`coord_send_message`, `coord_read_inbox`,
   per-recipient unread tracking via `message_reads` table)
- **M2d** ✓ Shared memory commons (`coord_list/read/update_memory`)
- **M2e** ✓ Per-agent + team daily cost caps (env-configurable, enforced
   pre-spawn, `cost_capped` events)
- **v2 (a/b/c/d)** ✓ Preact frontend rewrite: slim left rail with status dots,
   tileable agent panes (Split.js drag-resize), per-tool renderers
   (Read/Edit/Bash/Grep/Glob/coord_*/generic + Edit diff card + Read-of-image
   inline preview), tool_use↔tool_result pairing, Image paste via
   /api/attachments, EnvPane with live tasks/cost/timeline, SettingsDrawer
- **M3 (1/2/3)** ✓ kDrive persistence:
   - Memory docs synchronously mirror to `/harness/memory/<topic>.md`
   - Event log flushed every 5 min to `/harness/events/<date>.jsonl`
     (with yesterday-replay during 00:00–02:00 UTC for boundary safety)
   - Hourly `VACUUM INTO` snapshot to `/harness/snapshots/<ts>.db`
- **M4 (1/2)** ✓ Per-Player git worktrees:
   - `git` installed in container with default identity
   - On boot, if `HARNESS_PROJECT_REPO` is set, clone to `/workspaces/.project`
     and create worktree `/workspaces/<slot>/project` on branch `work/<slot>`
   - Branch resolution preserves `origin/work/<slot>` history if it exists

**Next likely:**
- **M4 step 3**: agent commit/push helper (or document credentials.helper
   for HARNESS_PROJECT_REPO with embedded PAT)
- **M5**: SDK hooks. Risky one (PreToolUse inbox-inject) likely deferred
   in favor of the polling pattern that already works. Useful pieces:
   session_id capture for resume, TaskCompleted hook
- **Decisions/digests** files on kDrive (need Coach to actively run on a
   loop to populate them)

## What needs verification (when user is next active)

A lot has shipped without exercise. Before depending on any of this:

1. **Zeabur redeploy succeeds** with the latest commit (heavy git install + worktree boot might surface issues)
2. **Cost cap blocks spawn** when an agent is over its daily limit
3. **kDrive mirror** writes a memory doc when env vars are configured
4. **Git worktrees** materialize for each slot when `HARNESS_PROJECT_REPO` is set
5. **Image paste** end-to-end: paste in pane → upload → agent Read → describe
6. **Per-tool renderers** display nicely in the timeline
7. **Tasks**: human creates → coach assigns via msg → player claims → updates → done

Most likely failure mode: subtle SDK / WebDAV / git-credential issue that needs a small fix.

---

## Critical invariants (do not violate without discussion)

1. **Single write-handle discipline.** All agents write freely — they chat (`coord_send_message`), claim tasks, update progress, create subtasks, drop notes in shared memory. But every write routes through the harness server process, which holds the only SQLite write handle. Do NOT add code paths where an agent opens its own DB connection or edits `state/*.json` directly. The point is ordering + audit, not restricting agent autonomy.

2. **Per-worktree isolation is the primary concurrency control.** Each worker operates in its own git worktree under `workspaces/wN/`. Locks (`coord_acquire_lock`) are **advisory only**, for logical cross-worktree resources (e.g. "only one worker runs the migration"). Don't reach for locks when a worktree would do.

3. **Memory is scratchpad.** `memory/*.md` is overwritten on update, no version history. If history matters, the event log (`memory_updated` events) has it. `decisions/*.md` is append-only by convention — that's where durable "we chose X because Y" lives.

4. **Max-plan OAuth, no API keys.** The whole point is to share one Max billing across 10 agents. Don't introduce `ANTHROPIC_API_KEY` paths. See auth gotcha below.

5. **Cost caps baked in from the start.** Per-agent daily turn/cost caps are enforced before spawn, not added later. 11 Sonnet sessions × 50-turn loops can chew through a weekly Max allowance fast.

---

## Known gotchas

### Claude CLI auth does NOT live in `~/.claude.json`

Confirmed via M-1 spike. `~/.claude.json` holds only local CLI config (numStartups, installMethod). OAuth tokens live in the OS credential store (Windows Credential Manager, macOS Keychain, Linux Secret Service) or an internal CLI-managed path.

- **Copying `~/.claude.json` across hosts does not transfer auth.**
- On a new VPS/container: run `claude` → `/login` (slash command in the REPL) → open URL on laptop → enter code → approve. Token now persists locally.
- The harness Docker image must mount a volume at wherever Linux Claude CLI persists tokens, so redeploys don't lose auth.

### Zeabur geo-block: install via npm, not the shell installer

Zeabur's default datacenter returns HTTP 403 for `https://claude.ai/install.sh` ("App unavailable in region"). `api.anthropic.com` is **not** blocked in the same region — runtime queries work fine.

- Dockerfiles must install Claude CLI via: `npm install -g @anthropic-ai/claude-code`
- Not via: `curl -fsSL https://claude.ai/install.sh | bash`

### Line endings on Windows

`.gitattributes` at repo root forces LF on `*.sh` and `Dockerfile*`. If you add new shell scripts or Dockerfiles, existing rules cover them. If not, the script will fail in Linux containers with `$'\r': command not found`.

---

## Repo layout (current)

```
TeamOfTen/
├── CLAUDE.md                    # this file
├── Docs/
│   └── HARNESS_SPEC.md          # full spec — source of truth for design decisions
├── spike/
│   ├── zeabur/                  # M-1 spike Dockerfile + shell for Zeabur
│   │   ├── Dockerfile
│   │   ├── spike.sh             # not currently used (manual shell instead)
│   │   └── README.md
│   ├── spike.py                 # abandoned Python SDK version (ARM64 wheel issues)
│   └── requirements.txt
├── .gitignore
└── .gitattributes               # force LF on *.sh and Dockerfile
```

Planned expansion per spec §3: `server/`, `web/`, `prompts/`, `workspaces/`, `scripts/`. Not yet created.

---

## Key commands (for any agent working in this repo)

- **Quick concurrency test on running Zeabur container**: `claude -p "test"` then `for i in $(seq 1 10); do claude -p "hi $i" & done; wait`
- **Local spike re-run on Windows** (for laptop-only tests): `claude -p "..."` — no setup needed, Claude CLI 2.1.104 already installed
- **Run tests** (once M2 lands): `uv run pytest server/tests/` (planned)
- **Run dev server** (once M0 lands): `uv run uvicorn server.main:app --reload` (planned)

---

## Skills to use

Built-in slash commands worth knowing for this project:

- **`/security-review`** — runs the built-in security-review skill against current branch. Use before each deploy, especially when touching auth, MCP tool registration, or anything that handles inter-agent messages.
- **`/review`** — general PR review.

The `claude-api` skill auto-triggers when editing Python files that import `anthropic` or `claude_agent_sdk` — it will guide caching, thinking budgets, and migration between Claude versions.

No custom project-specific skills yet — this `CLAUDE.md` is the single source for project conventions, loaded automatically at session start. If the project grows to the point that this file exceeds ~200 lines, split into skills.

---

## Before committing

- Line endings: verify `git status` does not show `LF will be replaced by CRLF` for `*.sh` or `Dockerfile*` — if it does, `.gitattributes` is missing or not applied.
- Secrets: `.gitignore` covers `.env*`, `.claude.json`, `.claude/`. Double-check any new config file doesn't leak.
- No `ANTHROPIC_API_KEY` references — this harness is Max-OAuth only.

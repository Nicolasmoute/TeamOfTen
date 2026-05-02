<div align="center">

# TeamOfTen

**Ten Claude Code & Codex agents. Zero drift.**
*A self-hosted harness for an engineer who wants leverage without losing the wheel.*

[![tests](https://github.com/Nicolasmoute/TeamOfTen/actions/workflows/tests.yml/badge.svg)](https://github.com/Nicolasmoute/TeamOfTen/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python 3.12](https://img.shields.io/badge/python-3.12-3776ab.svg)](https://www.python.org/)
[![docker ready](https://img.shields.io/badge/docker-ready-0db7ed.svg)](Dockerfile)
[![claude code](https://img.shields.io/badge/claude%20code-SDK-ce6041.svg)](https://docs.claude.com/en/docs/claude-code)
[![openai codex](https://img.shields.io/badge/openai%20codex-SDK-10a37f.svg)](https://openai.com/index/introducing-codex/)

</div>

---

## The 10% problem

> AI agents are remarkable 90% of the time. The other 10% quietly compounds.

A wrong assumption made on turn 3, baked in by turn 30. A constraint missed. A subtle drift from the spec — subtle, then sudden, then everywhere. **Longer prompts don't fix this.** Bigger swarms make it worse. Existing tools either hide drift behind a dashboard or trap you in one-on-one chat.

TeamOfTen is the opposite shape. Three things, working together:

| | | |
|--|--|--|
| 🧭 | **Compass** | A probabilistic strategy engine maintains a YES/NO world-model of your project. Asks *you* the high-uncertainty questions. Audits every artifact. Catches drift before it ships. |
| 🔒 | **Truth** | A `truth/` folder agents *cannot write to.* Coach proposes diffs; you approve. The source-of-truth doesn't drift behind your back. |
| 🪟 | **All glass** | Eleven panes, every tool call live, every agent steerable. The agent chatter *is* the interface. |

One Coach. Ten Players. One operator. One VPS.

[![TeamOfTen multi-pane UI](Docs/Screenshot%202026-04-29%20230510.jpg)](Docs/Screenshot%202026-04-29%20230510.jpg)

---

## Table of contents

- [Compass — a strategy engine for your team](#compass--a-strategy-engine-for-your-team)
- [Truth — write-protected by default](#truth--write-protected-by-default)
- [All glass — eleven panes, no black box](#all-glass--eleven-panes-no-black-box)
- [Two runtimes — Claude Code & OpenAI Codex](#two-runtimes--claude-code--openai-codex)
- [What it actually does, end-to-end](#what-it-actually-does-end-to-end)
- [Quick start](#quick-start-any-linux-server)
- [Configuration](#configuration)
- [Repo layout](#repo-layout)
- [Development](#development)
- [Network & security](#network--security)
- [Status & philosophy](#status--philosophy)

---

## Compass — a strategy engine for your team

Compass is the thing that makes ten agents safe to leave alone.

It maintains a **probabilistic lattice** of weighted YES/NO statements about your project — what it is, who it's for, how it works, what's load-bearing — each with a confidence weight in `[0.0, 1.0]`. Confirmed truths sit near 1.0 or 0.0; uncertain ones cluster near 0.5; statements are organized by region (pricing, architecture, customers, auth…).

```
LATTICE · pricing region                                       run r-142
─────────────────────────────────────────────────────────────────────
s2    Per-task billing, not per-second.                ▰▰▰▰▰▰▱  0.93  ✓ truth
s14   Stripe webhooks must be processed idempotently.  ▰▰▰▰▰▰▰  0.96  ✓ truth
s7    Customers self-serve onboarding.                 ▰▰▰▰▱▱▱  0.78  q&a
s11   Annual prepay > monthly.                         ▱▱▱▱▱▱▱  0.42  open
s44   Free tier exists for individual developers.      ▱▱▱▱▱▱▱  0.06  settle?
─────────────────────────────────────────────────────────────────────
```

Three loops keep it honest:

**Daily briefing → Coach.** A cron-anchored Tick fires each morning. The current lattice — confirmed YES, confirmed NO, the open questions, today's focus — lands in Coach's prompt. Players inherit it through their briefs.

**Q&A loop → You.** Compass picks the highest-entropy statement and asks *you* one focused question. YES, NO, or `nuance ↓`. Your answer updates the lattice, and that's the day's strategic input.

**Audit, advisory.** Every artifact a Player ships gets `compass_audit()`'d against the lattice. Verdicts: `aligned` · `confident_drift` · `uncertain_drift`. Coach is notified. You aren't interrupted unless it matters.

```
compass_audit()                                                12m ago
─────────────────────────────────────────────────────────────────────
artifact:  p4 shipped per-second billing
verdict:   confident_drift
conflict:  s2 · per-task pricing  (weight 0.93 · settled)
→ coach notified.
→ human not interrupted.
```

The point isn't that Compass is always right. The point is that **drift becomes legible.** It's something Coach can decide about, instead of something that compounds invisibly.

---

## Truth — write-protected by default

The `truth/` folder and project `CLAUDE.md` are **the source.** Everything Compass measures against, everything Coach plans from, everything the Players inherit in their briefs — it all routes through these files.

Agents cannot write to them. A `PreToolUse` hook blocks every attempt:

```
p4 → Edit truth/principles.md             [blocked: truth is read-only for agents]
coach → propose_truth_change(diff)        [staged: awaiting human review]
human → approve · 1-tap                   [merged: committed by you]
```

Coach proposes diffs. You approve in the UI. The agents propose; the human disposes. The source-of-truth doesn't drift behind your back, ever.

---

## All glass — eleven panes, no black box

Most multi-agent orchestrators abstract agents behind dashboards, tickets, or pipeline logs. You see the org chart and the deliverables, but you lose contact with what the agents are actually doing.

Here, **the agent chatter *is* the interface.** A tileable multi-pane web UI streams every agent's tool use live, side by side:

- Drag panes around, stack them into columns, split + resize, maximize one pane, export a conversation to markdown.
- **Per-pane settings.** Override model / runtime / plan-mode / effort per pane via a gear popover. Settings persist in localStorage.
- **Live token/context bar** in every pane header — knows when an auto-compact is about to fire.
- **Image paste** into any agent's input. Drop a screenshot, the agent reads it.
- **Slash commands** — `/plan` `/model` `/effort` `/brief` `/tools` `/clear` `/loop` `/tick` `/status` `/spend` `/compact`.
- **Files pane** — browse + preview/edit `.md` files across memory, knowledge, decisions, outputs, uploads, attachments.
- **Mobile layout.** Sub-700px reflows the whole app: bottom rail, swipeable pane deck, full-screen env overlay. Watch the team from your phone.
- **Telegram bridge.** Whitelist-gated bot. Send goals to Coach from anywhere; `coord_request_human` escalations ping you back.

Every agent's session, context usage, and cost is live. Drag-to-rearrange, stack, split, maximize — it's your workspace.

---

## Two runtimes — Claude Code & OpenAI Codex

Same `coord_*` tool surface, same UI, same cost ledger. **Mix and match per slot.**

- **Full Claude Code SDK integration.** Same CLI, same permission model, same tool allowlists. The harness adds the team layer; it doesn't try to abstract the SDK away.
- **Full OpenAI Codex SDK integration.** Per-agent runtime selection (`agents.runtime_override`) lets you run any slot on Codex instead of Claude — coord tools via an MCP proxy, identical from the UI's perspective.
- **API keys or OAuth login.** Either path works for both runtimes. Tokens persist on the `/data` volume so they survive redeploys; manage them from the Settings drawer without shelling into the container.

Coach on Claude, half the Players on Codex, slot p7 on whichever model writes the best Python this week — whatever the task needs.

---

## What it actually does, end-to-end

1. You send a goal to **Coach** in the UI (or by Telegram).
2. Coach reads the **Compass briefing** to ground itself in the current strategy, then decomposes the goal into tasks on a shared board and push-assigns them to specific **Players** (`p1`–`p10`, auto-named after lacrosse legends by default — Coach can rename + brief them per project).
3. The assignee **auto-wakes** the moment a task lands, reads their inbox, claims the task, and works in their **own git worktree** on your project repo — full direct git access, `git commit + push` straight back to GitHub.
4. Players message each other peer-to-peer for info, drop notes in **shared memory**, write **knowledge artifacts** (plain markdown), save **binary outputs** (docx, pdf, png, zip), record durable **decisions** (Coach-only, immutable), and ask **structured questions** of you when blocked.
5. Every artifact gets **audited against the Compass lattice.** Drift is flagged. Coach decides; you sleep.
6. Agents can use **external MCP servers** (Notion, Slack, Linear, Sentry — anything MCP-shaped), credentials in an encrypted on-disk vault.
7. You're part of the team: open any agent's pane to read what they're saying, send them a direct prompt, watch the live tool-use stream, override their model / runtime / effort / plan-mode, or pause/cancel a runaway turn.
8. Everything human-readable mirrors to your **WebDAV cloud drive** (kDrive, Nextcloud, ownCloud, Fastmail) so you can read/edit it from anywhere — even with the harness offline. Point Obsidian at the synced folder and you have a live second-brain the agents write into.

Full details: [Docs/TOT-specs.md](Docs/TOT-specs.md). Rules agents follow when editing this repo: [CLAUDE.md](CLAUDE.md).

---

## Coordination tools (MCP, internal)

~25 `coord_*` tools the agents call directly:

| | |
|--|--|
| **Tasks** | `coord_list_tasks`, `coord_create_task`, `coord_claim_task`, `coord_assign_task`, `coord_update_task` |
| **Messaging** | `coord_send_message`, `coord_read_inbox` |
| **Shared memory** | `coord_list_memory`, `coord_read_memory`, `coord_update_memory` |
| **Durable decisions** | `coord_write_decision` |
| **Knowledge** | `coord_write_knowledge`, `coord_read_knowledge` |
| **Binary outputs** | `coord_save_output` |
| **Git** | `coord_commit_push` |
| **Team identity** | `coord_set_player_role` |
| **Todos** | `coord_add_todo`, `coord_complete_todo` |
| **Compass** | `compass_propose_statement`, `compass_audit`, `compass_query` |
| **Truth** | `propose_truth_change` (Coach-only) |
| **Human-in-the-loop** | `AskUserQuestion`, `coord_answer_question`, `coord_answer_plan`, `coord_request_human` |

---

## Coach automation

- **Recurrence scheduler.** Three primitives: **Tick** (smart pulse — Coach composes its own next prompt from inbox + todos + objectives + Compass briefing), **Repeat** (fixed cadence in seconds), **Cron** (calendar-anchored). Set it once and Coach checks the inbox every N minutes forever.
- **Auto-compact at 70% context.** Structured compact turn first, then the user's prompt on the fresh session — verbatim recent exchanges preserved.
- **Auto-retry on hard errors.** Single retry after `HARNESS_ERROR_RETRY_DELAY` seconds; escalates via `human_attention` after `HARNESS_ERROR_RETRY_MAX_CONSECUTIVE`.
- **Stale-session auto-heal.** A `ProcessError` on resume clears `session_id` and retries once — no manual session clears after re-auth.

---

## Cost & reliability

- **Per-agent + team daily caps.** Enforced before spawn, in USD/day. A runaway loop stops itself.
- **Per-project breakdown.** Spend stays predictable; reset for fresh headroom when you choose.
- **Health probe.** `/api/health` returns per-subsystem readiness (db / static / claude_cli / codex_cli / webdav / workspaces / claude_auth / codex_auth). Container `HEALTHCHECK` hits it every 30s.
- **Audit trail.** Every destructive endpoint records `{source, ip, ua}` in the event payload.
- **Crash-recoverable.** Zombie running-state on `agents.status` / `tasks.in_progress` reset on every container boot.

---

## Security

- **Bearer-token gate.** `HARNESS_TOKEN` env protects every `/api/*` endpoint and the WebSocket. UI shows a paste-modal on 401.
- **Read-only sandbox for Coach** (when on Codex). Coach plans; only Players touch code. Sandbox policy is enforced at the Codex CLI level.
- **No `--dangerously-skip-permissions`.** Standard Claude Code permission model; per-agent tool allowlists in [server/tools.py](server/tools.py).
- **Encrypted secrets vault.** Fernet (AES-128-CBC + HMAC-SHA256), keyed by `HARNESS_SECRETS_KEY`, kept in the SQLite DB.
- **No telemetry, no phone-home.** The container talks to Anthropic, OpenAI (if Codex is enabled), GitHub, your WebDAV server, and the external MCP servers you wire in. That's the whole list.

---

## Quick start (any Linux server)

**Requirements:**

- Docker
- ~2 GB RAM free (11 CLI processes + app)
- A persistent volume mounted at `/data` (SQLite + session files + WebDAV cache)
- Optional: a WebDAV cloud drive, a GitHub repo for the project the team works on

**Run it:**

```bash
docker build -t teamoften .
docker run -d \
  --name teamoften \
  -p 8000:8000 \
  -v teamoften_data:/data \
  -v teamoften_workspaces:/workspaces \
  --env-file .env \
  teamoften
```

Open `http://your-host:8000`. If you set `HARNESS_TOKEN`, paste it when the UI asks.

**First run.** The UI walks you through three things:

1. **Auth.** Both runtimes accept either an API key or OAuth login. Paste from a laptop (`claude /login` → copy `~/.claude/.credentials.json` → paste in Settings) or shell in (`docker exec -it teamoften bash` → `claude` → `/login`).
2. **Repo.** Add a project in the **Projects** pane (GitHub URL with PAT, default branch). Coach clones it into per-Player worktrees. Multi-project: switch the active project and the whole team — workspaces, briefs, costs — follows.
3. **Compass seed.** Coach runs an initial Q&A round with you, ~20 minutes, and writes the starting lattice into `truth/`.

Hit `/api/health` to confirm every subsystem is green.

---

## Configuration

Every knob is an env var. Copy [`.env.example`](.env.example) to `.env` and edit. Highlights:

Copy [`.env.example`](.env.example) to `.env` and edit. The file is intentionally small — only what you actually configure per deploy:

| Variable | Purpose |
| --- | --- |
| `HARNESS_TOKEN` | Bearer token required on every `/api/*` request and on the WebSocket. **Set before exposing to the internet.** |
| `HARNESS_WEBDAV_URL` + `_USER` + `_PASSWORD` | WebDAV mirror (kDrive / Nextcloud / ownCloud / Fastmail). All three or none. |
| `HARNESS_AGENT_DAILY_CAP` / `_TEAM_DAILY_CAP` | USD/day per-Player and team-wide cost caps. Defaults: 5 / 20. |
| `HARNESS_CODEX_ENABLED` | Gate the Codex runtime. When unset/false, the API rejects `runtime=codex`. |
| `HARNESS_SECRETS_KEY` | Fernet key (44-char urlsafe-base64) for the encrypted secrets store. Required for managing MCP credentials and the Telegram token from the UI. |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_CHAT_IDS` | First-boot bootstrap for the Telegram bridge — once you save them in Options the env vars are ignored. |

Everything else is configured in the UI (project repos in **Projects**, MCP servers and Telegram in **Options**, the Coach recurring tick via `/tick N` or `PUT /api/coach/tick`) or has a Dockerfile-baked default (`CLAUDE_CONFIG_DIR=/data/claude`, `CODEX_HOME=/data/codex`, data paths under `/data` and `/workspaces`). Tuning knobs (retention, sync intervals, auto-compact threshold, auto-wake debounce, error-retry timing, stale-task watchdog, handoff token budget) all have sensible code defaults — search `server/` for `os.environ.get("HARNESS_` if you genuinely need to override one.

See [`.env.example`](.env.example) for the full list (~40 vars: retention, intervals, compact thresholds, reliability tuning).

---

## Repo layout

```
server/                       FastAPI app + Claude/Codex SDK runners + MCP coord server
server/runtimes/              ClaudeRuntime + CodexRuntime + AgentRuntime protocol
server/compass/               Lattice, Q&A, audit, briefing
server/webdav.py              WebDAV mirror (swap this to support other backends)
server/telegram.py            Telegram bridge (long-poll, whitelist-gated)
server/static/                Preact SPA — no build step, served as plain files
server/tests/                 pytest suite (DB-level — runs without the Claude CLI)
Dockerfile                    Python 3.12 + Node 20 + claude CLI + codex CLI + git
mcp-servers.example.json      Template for wiring external MCP servers
truth/                        The project's source-of-truth (write-protected for agents)
Docs/TOT-specs.md             Full spec (data model, coordination, tool surface, UI)
Docs/COMPASS_SPEC.md          Compass design — lattice, Q&A, audit semantics
Docs/CODEX_RUNTIME_SPEC.md    Codex runtime design + parser specifics
CLAUDE.md                     Rules for any agent editing this codebase
.env.example                  Every env var, grouped by purpose, with defaults
```

---

## Development

```bash
uv sync --extra dev
uv run pytest                          # Full test suite (~420 tests)
uv run uvicorn server.main:app --reload
```

Or with a plain venv: `pip install -e .[dev]` then `uvicorn server.main:app --reload`.

CI runs the tests on every push ([.github/workflows/tests.yml](.github/workflows/tests.yml)). The Dockerfile sets a `HEALTHCHECK` that hits `/api/health` every 30s.

---

## Network & security

What the container talks to:

- **Anthropic API** — via the Claude CLI, on every Claude-runtime turn.
- **OpenAI API** — via the Codex CLI, only if `HARNESS_CODEX_ENABLED` is set and a slot uses the Codex runtime.
- **GitHub** (or your git host) — over HTTPS, for projects that have a repo URL configured in the Projects pane. Used for `git clone` + `git push` from within the per-Player worktrees.
- **Your WebDAV server** — only if `HARNESS_WEBDAV_*` are set.
- **External MCP servers** — only the ones you explicitly wire in via the Options drawer (encrypted credentials in the secrets store).
- **Telegram API** — only if a bot token is configured.

No telemetry, no phone-home, no auto-update of the harness itself. The Claude / Codex CLIs inside the container self-update per their respective release channels.

Credentials are stored at `$CLAUDE_CONFIG_DIR/.credentials.json` and `$CODEX_HOME/auth.json` on the mounted `/data` volume. MCP-tool credentials and the Telegram bot token (if configured via the UI) are stored in the SQLite DB encrypted with Fernet.

---

## Uninstall

```bash
docker stop teamoften
docker rm teamoften
docker volume rm teamoften_data teamoften_workspaces
```

That removes the container, the SQLite DB, session history, cached artifacts, and the per-Player worktrees. WebDAV-mirrored files stay on your cloud drive — delete them manually if you want a clean slate.

---

## Status & philosophy

Built as a personal tool — a single operator driving a team of agents, visible end-to-end. Not trying to be a product. Not trying to abstract the SDKs. Not trying to be multi-tenant. **It's a harness, not a framework.**

The code is readable, the surface is small, and the invariants are written down in [CLAUDE.md](CLAUDE.md) and [truth/](truth/) so any agent working on the codebase (including me) stays on the rails.

**Contributions:** this is a personal project. Fork it and make it your own — MIT-licensed, do whatever. I'm not actively reviewing pull requests or triaging issues; think of this repo as a snapshot you can adapt, not a product with a roadmap. Bugs expected.

---

<div align="center">

**Have fun. Improve it.**

[github.com/Nicolasmoute/TeamOfTen](https://github.com/Nicolasmoute/TeamOfTen) · MIT · Python · FastAPI · Preact · SQLite

</div>

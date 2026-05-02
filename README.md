# TeamOfTen

**A simple, transparent orchestration harness for a personal team of Claude (and Codex) agents.**

![tests](https://github.com/Nicolasmoute/TeamOfTen/actions/workflows/tests.yml/badge.svg)

![TeamOfTen multi-pane UI](Docs/Screenshot%202026-04-26%20232144.jpg)

I couldn't find a multi-agent setup that felt right — most were either heavy frameworks or black-box products. Especially since the real challenge of multi-agent work isn't running them in parallel — it's keeping them aligned with the spec over time.

AI agents are remarkable 90% of the time. The remaining 10% — wrong assumption, missed constraint, drift from the spec — quietly compounds turn after turn until the project goes off the rails, and existing tools either hide that drift behind a swarm dashboard or trap you in one-on-one chat. TeamOfTen makes human supervision *efficient* rather than constant: every agent's work is visible live, a `truth/` folder holds specs agents can't edit directly (they propose, you approve), and you steer through Coach instead of eleven Players. Tight loop, low friction.

Concretely: a single-container web app where **1 Coach + 10 Players** share memory, a task board, and a tileable multi-pane UI. Coach plans, Players execute in their own git worktrees and push straight to your repo, and everything human-readable mirrors to a WebDAV-compatible cloud drive (kDrive, Nextcloud, ownCloud, Fastmail). Intervene on any agent live, or steer through Coach.

Set it up once on a VPS and it runs 24/7. Drive Coach from your phone via Telegram or the mobile UI. Open Obsidian on the synced folder and you have a live second-brain the agents write into. Nice little project. Have fun, improve it.

---

## At a glance

A personal AI engineering harness for people who want to *manage* a team of agents — not chat with a single one.

### Team & coordination
A real team that breaks down goals, divides work in parallel, names its members, and talks to itself — instead of one chatbot you keep prompting.

### SDD-ready (specs-based development)
Your specs are protected. Agents *propose* changes to a `truth/` folder; you approve or deny in the UI. The source of truth doesn't drift behind your back.

### Runs Claude Code and OpenAI Codex
Mix Claude and Codex agents on the same team — pick the right model per slot. Switch active project and the whole team follows: workspaces, repos, objectives, costs.

### Coach automation — recurrences
Coach keeps the team moving while you're away. Smart ticks compose their own prompts from inbox + todos + objectives, so you don't write "what's next" every time.

### Web UI
All 11 agents visible at once. Drag panes to rearrange, paste screenshots, real diff views, click-through to files. Dense and quiet — no spinner soup.

### Environment panel
Tasks, cost, project objectives, coach todos, memory, decisions, urgent escalations — project state one panel away.

### Cost tracking
Daily caps per agent and team, enforced before each turn. Per-project breakdown. Reset for fresh headroom when you choose. Spend stays predictable.

### Git integration
Eleven Players, eleven isolated branches — no merge conflicts between them. One tool call to commit and push.

### Cloud-drive durability
Memory, decisions, and snapshots mirrored as readable markdown to whatever cloud drive you already use (kDrive, Nextcloud, ownCloud — WebDAV-compatible).

### Remote use optimization
Drive Coach from your phone via Telegram (whitelisted, urgent escalations push to your lock screen) and a mobile-tuned UI that reflows panes for one-finger swipe — the harness is usable away from your desk, not just compatible with it.

### Extensibility
Plug in any MCP tool. Store credentials encrypted, referenced by `${VAR}` placeholders. Tune each agent's brief, model, runtime, and effort independently.

### Reliability & observability
Auto-compaction, auto-retry, auto-recovery, health probe. The harness heals routine failures itself rather than waking you.

### Security
Optional bearer-token gate on UI and API, encrypted secrets store, audit trail on every destructive action. Safe to leave running and exposed.

### Single-VPS deploy
One Docker container, one small server. Deploy once; ops stays minimal.

---

## What it actually does

- You send a goal to **Coach** in the UI (or by Telegram).
- Coach decomposes it into tasks on a shared board and push-assigns them to specific **Players** (`p1` through `p10`, auto-named after lacrosse legends by default — Coach can rename + brief them per project).
- The assignee **auto-wakes** the moment a task lands, reads their inbox, claims the task, and works in their **own git worktree** on your project repo — full direct git access, `git commit + push` straight back to GitHub.
- Players message each other peer-to-peer for info, drop notes in **shared memory**, write **knowledge artifacts** (plain markdown), save **binary outputs** (docx, pdf, png, zip), record durable **decisions** (Coach-only, immutable), and ask **structured questions** of you when they're blocked.
- Agents can use **external MCP servers** you wire in (Notion, Slack, Linear, Sentry — anything MCP-shaped), credentials in an encrypted on-disk vault.
- You're part of the team: open any agent's pane to read what they're saying, **send them a direct prompt**, watch the live tool-use stream, override their model / runtime / effort / plan-mode, or pause/cancel a runaway turn.
- Every agent's session, context usage, and cost is live in its own pane. Drag-to-rearrange, stack, split, maximize — it's your workspace.
- Everything human-readable mirrors to your cloud drive so you can read/edit it from anywhere, even with the harness offline.

Full details: [Docs/TOT-specs.md](Docs/TOT-specs.md). Rules agents follow when editing this repo: [CLAUDE.md](CLAUDE.md).

---

## Features

### The team
- **1 Coach + 10 Players, one shared SQLite write handle.** All writes — chat, tasks, memory, decisions — route through one process so ordering and audit are coherent. No distributed control plane, no Redis, no Kubernetes.
- **Coach plans, Players execute.** Coach receives goals, decomposes them into tasks, assigns work; Players claim tasks, do the work, and report back. Players can message each other for information, but never give orders.
- **Per-Player identity.** Coach assigns each Player a name, role, and brief at team-composition time. The brief is injected into every turn's system prompt so each agent has consistent personality + scope across sessions.
- **Multi-project.** Switch active projects in the UI; identities, roles, briefs, repos, and worktrees reload from per-project rows. Coach can compose a different team for each project.
- **Per-Player git worktrees.** Each Player runs in `repo/<slot>/` on a `work/<slot>` branch. Direct `git commit + push` back to GitHub via the harness PAT. Conflicts isolate to the worktree, not the team.

### Two runtimes
- **Claude Code under the hood.** Same CLI, same permission model, same tool allowlists. The harness adds the team layer; it doesn't try to abstract Claude Code or swap providers.
- **Optional OpenAI Codex runtime.** Per-agent runtime selection (`agents.runtime_override`) lets you run any slot on Codex instead of Claude — same coord_* tools via an MCP proxy, same UI, same cost ledger. Mix and match: Coach on Claude, Players on Codex, whatever the task needs.
- **Max-plan / ChatGPT auth, no API keys.** OAuth tokens persist on the `/data` volume so they survive redeploys. One Max plan covers all 10 agents; a single ChatGPT session covers all Codex slots.

### The UI
- **Watch all eleven agents work, simultaneously.** A tileable multi-pane web UI streams every agent's tool use live, side by side. Drag panes around, stack them into columns, split + resize, maximize one pane, export a conversation to markdown.
- **Per-pane settings.** Override model / runtime / plan-mode / effort per pane via a gear popover. Settings persist in localStorage.
- **Live token/context bar.** Each pane header shows real-time token usage and percentage of the model's context window — knows when an auto-compact is about to fire.
- **Image paste.** Drop a screenshot into any agent's input. The image uploads to the project, the path lands in the prompt, the agent reads it and describes what it sees.
- **Slash commands.** `/plan` `/model` `/effort` `/brief` `/tools` `/clear` `/loop` `/tick` `/status` `/spend` `/compact` `/help` — most operations are one keystroke away.
- **Files pane.** Browse + preview/edit `.md` files across memory, knowledge, decisions, outputs, uploads, attachments. In-app file links (`[note](/data/...)`) open in the pane.
- **Mobile layout.** Sub-700px viewport reflows the whole app: bottom rail, swipeable single-pane deck, full-screen env overlay. Watch the team from your phone without pinching at a desktop layout.
- **Telegram bridge.** Whitelist-gated bot. Send goals to Coach from anywhere; Coach's replies land back in the chat. `coord_request_human` escalations ping you too.

### Coordination tools (MCP, internal)
~25 `coord_*` tools the agents call directly: tasks (`coord_list_tasks`, `coord_create_task`, `coord_claim_task`, `coord_assign_task`, `coord_update_task`), messaging (`coord_send_message`, `coord_read_inbox`), shared memory (`coord_list/read/update_memory`), durable decisions (`coord_write_decision`), knowledge (`coord_write_knowledge`, `coord_read_knowledge`), binary outputs (`coord_save_output`), git (`coord_commit_push`), team identity (`coord_set_player_role`), todos (`coord_add_todo`, `coord_complete_todo`), structured human questions (`AskUserQuestion`, `coord_answer_question`), plan-mode resolution (`coord_answer_plan`), human escalation (`coord_request_human`).

### External integrations (MCP, third-party)
- **External MCP servers.** Paste-JSON config in the Options drawer, secret-detection, per-server toggle/delete/smoke-test.
- **Credentials in an encrypted vault.** Fernet (AES-128-CBC + HMAC-SHA256), keyed by `HARNESS_SECRETS_KEY`, kept in the SQLite DB.
- Works with **Notion, Slack, Linear, Sentry, Context7, GitHub** (everything in the MCP ecosystem).

### Storage
- **Cloud-drive-as-storage.** Memory, knowledge, decisions, outputs, conversation snapshots, and an hourly SQLite snapshot all mirror to a WebDAV-compatible drive (kDrive, Nextcloud, ownCloud, Fastmail, etc).
- **Ideal backend for an Obsidian-style wiki.** Knowledge artifacts are plain `.md` files in a folder tree. Point Obsidian (or Logseq, or any markdown tool) at the synced drive and you have a live second-brain the agents contribute to directly — wikilinks, backlinks, graph view, search, all for free.
- **Crash-recoverable.** Zombie running-state on `agents.status` / `tasks.in_progress` reset on every container boot.

### Reliability and cost control
- **Per-agent + team daily cost caps.** Enforced before spawn, in USD/day. A runaway loop stops itself.
- **Auto-compact at 70% context.** When a session approaches its limit, the harness runs a structured compact turn first, then the user's prompt on the fresh session — verbatim recent exchanges preserved.
- **Auto-retry on hard errors.** Single retry after `HARNESS_ERROR_RETRY_DELAY` seconds; escalates via `human_attention` after `HARNESS_ERROR_RETRY_MAX_CONSECUTIVE`.
- **Stale-session auto-heal.** A `ProcessError` on resume clears `session_id` and retries once — no manual session clears after `/login` rotation.
- **Recurrence scheduler.** Tick (Coach autoloop), repeat (custom prompts), cron — set it once and Coach checks the inbox every N minutes forever.
- **Health probe.** `/api/health` returns per-subsystem readiness (db / static / claude_cli / codex_cli / webdav / workspaces / claude_auth / codex_auth). Container `HEALTHCHECK` hits it every 30s.

### Security
- **Bearer-token gate.** `HARNESS_TOKEN` env protects every `/api/*` endpoint and the WebSocket. UI shows a paste-modal on 401.
- **Audit actor on destructive endpoints.** Identity / brief / model / repo / MCP / lock / session-clear writes record `{source, ip, ua}` in the event payload.
- **Read-only sandbox for Coach** (when on Codex). Coach plans; only Players touch code. Sandbox policy is enforced at the Codex CLI level.
- **No `--dangerously-skip-permissions`.** Standard Claude Code permission model; per-agent tool allowlists in [server/tools.py](server/tools.py).
- **No telemetry, no phone-home.** The container talks to Anthropic, OpenAI (if Codex is enabled), GitHub, your WebDAV server, and the external MCP servers you wire in. That's the whole list.

---

## The shape of it

A few things this harness does — and deliberately doesn't — that feel worth calling out:

- **Watch all eleven agents work, simultaneously.** Most multi-agent orchestrators abstract agents behind dashboards, tickets, or pipeline logs — you see the org chart and the deliverables, but you lose contact with what the agents are actually doing. Here, the agent chatter *is* the interface. Even mostly-autonomous teams benefit from light-touch steering, and that only works if you can see what's happening as it happens. That's the main reason this project exists.
- **One operator, one VPS, one SQLite file.** The code is small enough to read in an afternoon and the whole thing runs comfortably on a modest VPS. No vector DB, no Redis, no Kubernetes, no orchestrator pod.
- **Straight onto your real repo.** Each Player works in their own git worktree on your GitHub project and `git push`es back. No holding pen, no PR-bot mediator, no abstraction over git. Direct commit access is a feature — the worktree isolation is what makes it safe.
- **Human-readable by default.** Memory, knowledge, decisions, and binary outputs land as plain files on your WebDAV-compatible cloud drive. Readable on any device, with any editor, with or without the harness running.
- **Coach plans, Players execute.** Plenty of multi-agent frameworks split "planner" from "worker" — this isn't novel. It's just the organizing principle here, and the tool surface enforces it so Coach can never be bypassed by accident.
- **Not a wrapper.** The harness adds a task board, a message bus, per-agent identity, and cost caps. It doesn't try to abstract Claude Code or swap in a different model provider — beyond the optional Codex runtime, which speaks the same coord_* surface.

---

## Quick start (any Linux server)

**Requirements:**
- Docker
- ~2 GB RAM free (11 Claude CLI processes + app)
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

**First-run auth** (one of):

1. **Paste from a laptop** (recommended — no shell access needed).
   - On any machine with Claude Code installed: `claude /login`, complete the device-code flow.
   - Copy the contents of `~/.claude/.credentials.json`.
   - In the web UI → Settings drawer (gear icon) → **Claude auth** → paste → Save.
2. **Shell in and run `claude`.**
   - `docker exec -it teamoften bash`
   - `claude` → `/login` → follow the device-code flow.

Tokens persist at `$CLAUDE_CONFIG_DIR/.credentials.json` (default `/data/claude/...`) and survive redeploys. For Codex, the same pattern uses `$CODEX_HOME=/data/codex`.

Hit `/api/health` to confirm every subsystem is green.

---

## Configuration

Copy [`.env.example`](.env.example) to `.env` and edit. The full set of env vars you actually configure per deploy:

| Variable | Purpose |
| --- | --- |
| `HARNESS_TOKEN` | Bearer token for the API. **Set this before exposing to the internet.** |
| `HARNESS_WEBDAV_URL` + `_USER` + `_PASSWORD` | WebDAV mirror (kDrive / Nextcloud / etc.). |
| `HARNESS_AGENT_DAILY_CAP` / `_TEAM_DAILY_CAP` | USD/day per-Player and team-wide cost limits. |
| `HARNESS_CODEX_ENABLED` | Flip on the Codex runtime once auth is configured. |
| `HARNESS_SECRETS_KEY` | Fernet key for the encrypted secrets store (MCP credentials, Telegram token). |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_CHAT_IDS` | First-boot bootstrap for the Telegram bridge — overridable from the UI. |

Project repos, MCP servers, the Telegram bridge (post-bootstrap), and the Coach recurring tick are configured via the UI rather than env vars. Auth persistence paths (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`) and data paths (`/data`, `/workspaces`) are baked into the Dockerfile. Tuning knobs (retention, sync intervals, auto-compact threshold, etc.) have code defaults — search `server/` for `os.environ.get("HARNESS_` if you need to override one.

---

## Repo layout

```
server/                 FastAPI app + Claude Agent SDK runner + MCP coord server
server/runtimes/        ClaudeRuntime + CodexRuntime + AgentRuntime protocol
server/webdav.py        WebDAV mirror (swap this to support other backends)
server/telegram.py      Telegram bridge (long-poll, whitelist-gated)
server/static/          Preact SPA — no build step, served as plain files
server/tests/           pytest suite (DB-level — runs without the Claude CLI)
Dockerfile              Python 3.12 + Node 20 + claude CLI + codex CLI + git
mcp-servers.example.json  Template for wiring external MCP servers
Docs/TOT-specs.md       Full spec (data model, coordination, tool surface, UI)
Docs/CODEX_RUNTIME_SPEC.md  Codex runtime design + parser specifics
CLAUDE.md               Rules for any agent editing this codebase
.env.example            Every env var, grouped by purpose, with defaults
```

---

## Development

```bash
uv sync --extra dev
uv run pytest                        # Full test suite (~420 tests)
uv run uvicorn server.main:app --reload
```

Or with a plain venv: `pip install -e .[dev]` then `uvicorn server.main:app --reload`.

CI runs the tests on every push ([.github/workflows/tests.yml](.github/workflows/tests.yml)). The Dockerfile sets a `HEALTHCHECK` that hits `/api/health` every 30s.

---

## Network & security

What the container talks to:

- **Anthropic API** — via the Claude CLI, on every Claude-runtime turn.
- **OpenAI API** — via the Codex CLI, only if `HARNESS_CODEX_ENABLED` is set and a slot uses the Codex runtime.
- **GitHub** (or your git host) — over HTTPS, for projects that have a repo URL configured. Used for `git clone` + `git push` from within the per-Player worktrees.
- **Your WebDAV server** — only if `HARNESS_WEBDAV_*` are set.
- **External MCP servers** — only the ones you explicitly wire in via the Options drawer (encrypted credentials in the secrets store).
- **Telegram API** — only if a bot token is configured.

No telemetry, no phone-home, no auto-update of the harness itself. The Claude / Codex CLIs inside the container self-update per their respective release channels.

OAuth credentials are stored at `$CLAUDE_CONFIG_DIR/.credentials.json` and `$CODEX_HOME/auth.json` on the mounted `/data` volume. MCP-tool credentials and the Telegram bot token (if configured via the UI) are stored in the SQLite DB encrypted with Fernet, keyed by `HARNESS_SECRETS_KEY`.

The harness does **not** use `--dangerously-skip-permissions`. Agents run with the normal Claude Code permission model; tool allowlists are enforced per-agent in [server/tools.py](server/tools.py).

---

## Uninstall

```bash
docker stop teamoften
docker rm teamoften
docker volume rm teamoften_data teamoften_workspaces
```

That removes the container, the SQLite DB, session history, cached artifacts, and the per-Player worktrees. If you configured a WebDAV mirror, any files already written there stay on your cloud drive — delete them manually if you want a clean slate.

---

## Status & philosophy

Built as a personal tool — a single operator driving a team of agents, visible end-to-end. Not trying to be a product. Not trying to abstract Claude Code or Codex. Not trying to be multi-tenant. It's a harness, not a framework.

The code is readable, the surface is small, and the invariants are written down in CLAUDE.md so any agent working on the codebase (including me) stays on the rails.

**Contributions:** this is a personal project. Fork it and make it your own — MIT-licensed, do whatever. I'm not actively reviewing pull requests or triaging issues; think of this repo as a snapshot you can adapt, not a product with a roadmap. Bugs expected.

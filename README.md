# TeamOfTen

**A simple, transparent orchestration harness for up to 10 Claude Code agents.**

![tests](https://github.com/Nicolasmoute/TeamOfTen/actions/workflows/tests.yml/badge.svg)

I couldn't find a multi-agent Claude Code setup that felt right — most were either heavy frameworks or black-box products. This is the opposite: a single-container web app where **1 Coach + 10 Players** all run Claude Code, share a task board, message each other, work directly on your GitHub project repo via per-Player git worktrees, produce documents at various levels (scratchpad, knowledge, decisions, binary outputs) that sync to a cloud drive, and can plug into third-party **MCP servers** (Notion, Slack, Linear, Sentry — anything that speaks MCP) for the work that lives outside the codebase. Everything is visible in a multi-pane UI: you can intervene on any agent directly, watch the inter-agent chatter unfold live, or just sit back and steer through Coach. You're part of the team — even though the main idea is to keep Coach as the single entry point. Set it up once on a VPS and it runs 24/7.

The code is intentionally simple. The storage backend assumes a **WebDAV-compatible cloud drive** — kDrive, Nextcloud, ownCloud, Fastmail, whatever — because plain WebDAV was the shortest path from "runs in Docker" to "I can read the agents' output from my phone". People out there can make this more sophisticated; I like the simplicity. If you need something else, swap [server/webdav.py](server/webdav.py) — it's ~10 methods.

Nice little project. Have fun, improve it. 

---

## What it actually does

- You send a goal to **Coach** in the UI.
- Coach decomposes it into tasks on a shared board and push-assigns them to specific **Players** (`p1` through `p10`, auto-named after lacrosse legends by default).
- The assignee auto-wakes, reads their inbox, claims the task, and works in their **own git worktree** on your project repo — full direct git access, `git commit + push` straight back to GitHub.
- Players can message each other for info, drop notes in **shared memory**, produce durable **knowledge artifacts** (plain markdown), save **binary outputs**, and ship **decisions**.
- **Ideal backend for a Karpathy/Obsidian-style wiki.** Since knowledge artifacts are plain `.md` files on a folder tree, pointing Obsidian (or Logseq, or any markdown tool) at the synced cloud drive gives you a live second-brain that agents contribute to directly — wikilinks, backlinks, graph view, search, all for free. I've been running this setup and it's worked really well in practice.
- Agents can use **external MCP servers** you wire in (Notion, Slack, Linear, Sentry — anything that speaks MCP), credentials stored in an encrypted on-disk vault.
- You're part of the team: open any agent's pane to read what they're saying, **send them a direct prompt**, watch the live tool-use stream, override their model / effort / plan-mode, or pause/cancel a runaway turn. Coach is the recommended entry point but never the only one.
- Every agent's session, context usage, and cost is live in its own pane. Drag-to-rearrange, stack, split — it's your workspace.
- Everything human-readable mirrors to your cloud drive so you can read/edit it from anywhere.

Full details: [TOT-specs.md](TOT-specs.md). Rules agents follow when editing this repo: [CLAUDE.md](CLAUDE.md).

---

## The shape of it

A few things this harness does — and deliberately doesn't — that feel worth calling out:

- **Watch all eleven agents work, simultaneously.** A tileable multi-pane web UI streams every agent's tool use live, side by side. Drag panes around, stack them into columns, export a conversation to markdown. Most multi-agent orchestrators abstract agents behind dashboards, tickets, or pipeline logs — you see the org chart, you see the deliverables, but you lose contact with what the agents are actually doing. Here, the agent chatter *is* the interface. Even mostly-autonomous teams benefit from light-touch steering, and that only works if you can see what's happening as it happens. That's the main reason this project exists.
- **Reach the team from anywhere.** The web UI is the primary workspace. A Telegram bot (being wired in now) lets you send goals to Coach and read replies from your phone when you're away from a screen. Everything human-readable also mirrors to your cloud drive, so the agents' output is reachable even with the harness offline.
- **One operator, one VPS, one SQLite file.** No distributed control plane, no vector DB, no Redis, no Kubernetes. Everything routes through one Python process that holds the only write handle. The code is small enough to read in an afternoon and the whole thing runs comfortably on a modest VPS.
- **Straight onto your real repo.** Each Player works in their own git worktree on your GitHub project and `git push`es back. No holding pen, no PR-bot mediator, no abstraction over git. Direct commit access is a feature, not a risk to design around — that's what the worktree isolation is for.
- **Human-readable by default.** Memory, knowledge, decisions, and binary outputs land as plain files on your WebDAV-compatible cloud drive. Readable on any device, with any editor, with or without the harness running. Point Obsidian at the drive and you have a live second-brain the team writes into.
- **Coach plans, Players execute.** Coach receives goals, decomposes them into tasks, and assigns work; Players claim tasks, do the work, and report back. Players can message each other peer-to-peer for information they need, but don't issue orders. Plenty of multi-agent frameworks split "planner" from "worker" — this isn't a novel idea. It's just the organizing principle here, and the tool surface enforces it so Coach can never be bypassed by accident.
- **Not a wrapper.** This is Claude Code — same CLI under the hood, same permission model, same tool allowlists. The harness adds a task board, a message bus, per-agent identity, and cost caps. It doesn't try to abstract Claude Code or swap in a different model provider. Per-agent and team daily cost caps are enforced before spawn so a runaway loop stops itself.

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

Tokens persist at `$CLAUDE_CONFIG_DIR/.credentials.json` (default `/data/claude/...`) and survive redeploys.

Hit `/api/health` to confirm every subsystem is green.

---

## Configuration

Every knob is an env var. Copy [`.env.example`](.env.example) to `.env` and edit. Highlights:

| Variable | Purpose |
| --- | --- |
| `HARNESS_TOKEN` | Bearer token for the API. Set this before exposing to the internet. |
| `CLAUDE_CONFIG_DIR` | Where Claude CLI persists OAuth + sessions. Defaults to `/data/claude` in the Dockerfile. |
| `HARNESS_PROJECT_REPO` | GitHub repo (with PAT in URL) that Players will work on. |
| `HARNESS_WEBDAV_URL` + `_USER` + `_PASSWORD` | WebDAV mirror. |
| `HARNESS_AGENT_DAILY_CAP` / `_TEAM_DAILY_CAP` | USD/day per-Player and team-wide cost limits. |
| `HARNESS_MCP_CONFIG` | Path to external MCP server config (see `mcp-servers.example.json`). |
| `HARNESS_SECRETS_KEY` | Optional Fernet key to enable the encrypted secrets store for MCP credentials. |

See [`.env.example`](.env.example) for the full list (~40 vars: retention, intervals, compact thresholds, reliability tuning).

---

## Repo layout

```
server/                 FastAPI app + Claude Agent SDK runner + MCP coord server
server/webdav.py        WebDAV mirror (swap this to support other backends)
server/static/          Preact SPA — no build step, served as plain files
server/tests/           pytest suite (DB-level — runs without the Claude CLI)
Dockerfile              Python 3.12 + Node 20 + claude CLI + git
mcp-servers.example.json  Template for wiring external MCP servers
TOT-specs.md            Full spec (data model, coordination, tool surface, UI)
CLAUDE.md               Rules for any agent editing this codebase
.env.example            Every env var, grouped by purpose, with defaults
```

---

## Development

```bash
uv sync --extra dev
uv run pytest                        # Full test suite
uv run uvicorn server.main:app --reload
```

Or with a plain venv: `pip install -e .[dev]` then `uvicorn server.main:app --reload`.

CI runs the tests on every push ([.github/workflows/tests.yml](.github/workflows/tests.yml)). The Dockerfile sets a `HEALTHCHECK` that hits `/api/health` every 30s.

---

## Network & security

What the container talks to:

- **Anthropic API** — via the Claude CLI, on every agent turn. Required.
- **GitHub** (or your git host) — over HTTPS, only if `HARNESS_PROJECT_REPO` is set. Used for `git clone` + `git push` from within the per-Player worktrees. Credentials live in the PAT embedded in that URL.
- **Your WebDAV server** — only if `HARNESS_WEBDAV_*` are set. Used to mirror memory/knowledge/decisions/outputs and snapshot the SQLite DB.
- **External MCP servers** — only the ones you explicitly wire in via `HARNESS_MCP_CONFIG`. See [mcp-servers.example.json](mcp-servers.example.json).

No telemetry, no phone-home, no auto-update of the harness itself (the Claude CLI inside the container does self-update per Anthropic's release channel).

Claude OAuth credentials are stored at `$CLAUDE_CONFIG_DIR/.credentials.json` on the mounted `/data` volume and never leave the container. MCP-tool credentials (if you use the encrypted secrets store) are stored in the SQLite DB, encrypted with Fernet (AES-128-CBC + HMAC-SHA256) keyed by `HARNESS_SECRETS_KEY`.

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

Built as a personal tool — a single operator driving a team of Claude agents, visible end-to-end. Not trying to be a product. Not trying to abstract Claude Code. Not trying to be multi-tenant. It's a harness, not a framework.

The code is readable, the surface is small, and the invariants are written down in CLAUDE.md so any agent working on the codebase (including me) stays on the rails.

**Contributions:** this is a personal project. Fork it and make it your own — MIT-licensed, do whatever. I'm not actively reviewing pull requests or triaging issues; think of this repo as a snapshot you can adapt, not a product with a roadmap. Bugs expected.

# TeamOfTen

**A simple, transparent orchestration harness for up to 10 Claude Code agents.**

![tests](https://github.com/Nicolasmoute/TeamOfTen/actions/workflows/tests.yml/badge.svg)

I couldn't find a multi-agent Claude Code setup that felt right — most were either heavy frameworks or black-box products. This is the opposite: a single-container web app where **1 Coach + 10 Players** all run Claude Code, share a task board, message each other, work on your GitHub project repo via per-Player git worktrees, produce documents at various levels (scratchpad, knowledge, decisions, binary outputs) that sync to a cloud drive, and can plug into third-party MCP tools (Notion, GitHub, Slack, anything that speaks MCP) for the work that lives outside the harness. Everything is visible in a multi-pane UI. Set it up once on a VPS and it runs 24/7.

The code is intentionally simple. The storage backend assumes a **WebDAV-compatible cloud drive** — kDrive, Nextcloud, ownCloud, Fastmail, whatever — because plain WebDAV was the shortest path from "runs in Docker" to "I can read the agents' output from my phone". People out there can make this more sophisticated; I like the simplicity. If you need something else, swap [server/webdav.py](server/webdav.py) — it's ~10 methods.

Nice little project. Have fun, improve it. 

---

## What it actually does

- You send a goal to **Coach** in the UI.
- Coach decomposes it into tasks on a shared board and push-assigns them to specific **Players** (`p1` through `p10`, auto-named after lacrosse legends by default).
- The assignee auto-wakes, reads their inbox, claims the task, and works in their **own git worktree** on your project repo.
- Players can message each other for info, drop notes in **shared memory**, produce durable **knowledge artifacts**, save **binary outputs**, ship **decisions**, and `git commit + push` their work.
- Agents can use **external MCP servers** you wire in (Notion, GitHub, Slack, Linear, Sentry — anything with an MCP integration), credentials stored in an encrypted on-disk vault.
- Every agent's session, context usage, and cost is live in its own pane. Drag-to-rearrange, stack, split — it's your workspace.
- Everything human-readable mirrors to your cloud drive so you can read/edit it from anywhere.

Full details: [TOT-specs.md](TOT-specs.md). Rules agents follow when editing this repo: [CLAUDE.md](CLAUDE.md).

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

**Zeabur notes:** works the same — Zeabur auto-pulls from GitHub on push, handles TLS, and provides the persistent volume. Mount it at `/data`. No Caddy/reverse proxy needed — Zeabur does that for you.

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

CI runs the tests on every push ([.github/workflows/tests.yml](.github/workflows/tests.yml)).

---

## Status & philosophy

Built as a personal tool — a single operator driving a team of Claude agents, visible end-to-end. Not trying to be a product. Not trying to abstract Claude Code. Not trying to be multi-tenant. It's a harness, not a framework.

The code is readable, the surface is small, and the invariants are written down in CLAUDE.md so any agent working on the codebase (including me) stays on the rails. If you fork it and make it better, open a PR — or don't, it's fine either way.

PRs welcome, issues welcome, bugs expected.

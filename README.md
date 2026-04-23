# TeamOfTen

Personal orchestration harness: **1 Coach + 10 Players**, all Claude Code agents, sharing a task board, memory commons, and inter-agent mailbox — deployed as a single Docker container on a VPS (Zeabur).

Max-plan OAuth only. No API keys.

![tests](https://github.com/Nicolasmoute/TeamOfTen/actions/workflows/tests.yml/badge.svg)

## What this is

A multi-pane web UI where you:
- Send a goal to Coach.
- Coach decomposes it into tasks on a shared board and `coord_assign_task`s them to specific Players.
- The assignee auto-wakes, reads their inbox, claims the task, and works in their own git worktree.
- Players can message each other for information, drop notes in a shared memory, write durable artifacts to a knowledge folder, and `git commit + push` their work.
- Every agent's session + cost is visible live in its own pane.

## Quick links

- **[CLAUDE.md](CLAUDE.md)** — what agents should read when operating on this codebase.
- **[TOT-specs.md](TOT-specs.md)** — full spec: data model, storage, coordination mechanics, every MCP tool, every HTTP endpoint, the UI surface.

## Required env vars

```bash
# Auth (optional — if unset, API is open)
HARNESS_TOKEN=<any long random string>

# Per-Player code (optional — without it, Players have no project to work on)
HARNESS_PROJECT_REPO=https://<PAT>@github.com/<you>/<repo>.git

# kDrive backup (optional — without it, only SQLite on /data survives restarts)
KDRIVE_WEBDAV_URL=https://connect.drive.infomaniak.com/<drive-id>/TOT
KDRIVE_USER=<your infomaniak email>
KDRIVE_APP_PASSWORD=<app-password from Infomaniak settings>
# Don't also set KDRIVE_ROOT_PATH if your URL already includes /TOT

# External MCP servers (optional)
HARNESS_MCP_CONFIG=/data/mcp-servers.json  # see mcp-servers.example.json

# Cost caps (defaults are conservative; 0 disables)
HARNESS_AGENT_DAILY_CAP=5.0
HARNESS_TEAM_DAILY_CAP=20.0
```

See TOT-specs.md §14.3 for the full table.

## First deploy

1. Container builds via `Dockerfile` — installs Node + `@anthropic-ai/claude-code` + git. `/data/claude` is pre-set as `CLAUDE_CONFIG_DIR` so OAuth persists.
2. Zeabur mounts `/data` as a persistent volume.
3. Shell into the container, run `claude`, type `/login`, follow device-code flow once. Token lands in `/data/claude/.credentials.json` — survives every redeploy from then on.
4. Hit `/api/health` to verify every subsystem is green.

## Repo layout

```
server/        FastAPI app + SDK agent runner + in-process MCP coord server
server/static/ Preact SPA (no build step — served directly)
server/tests/  pytest suite (run via `uv run pytest`)
Dockerfile     Python 3.12 + Node 20 + claude CLI + git
TOT-specs.md   Full spec
CLAUDE.md      In-repo instructions for agents editing this codebase
mcp-servers.example.json   Copy-to-/data for GitHub / Notion / Slack externals
```

## Running tests

```bash
uv sync --extra dev
uv run pytest
```

CI runs the same suite on every push (see `.github/workflows/tests.yml`).

## Status

Personal tool, single-user by design. Not a product. See TOT-specs.md §21 for what this explicitly is *not*.

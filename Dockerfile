FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    # Persist Claude CLI auth on the /data volume so `/login` once
    # survives redeploys. The CLI writes BOTH .claude.json (local
    # config) and .credentials.json (OAuth token) under this dir.
    # Without this, every Zeabur redeploy wipes auth and requires a
    # fresh device-code login.
    CLAUDE_CONFIG_DIR=/data/claude \
    # Codex CLI auth dir — same persistence rationale as
    # CLAUDE_CONFIG_DIR. Codex writes auth.json (ChatGPT session or
    # API key fallback) under this path. PR 1 spike confirms the
    # exact filename(s) on Zeabur.
    CODEX_HOME=/data/codex

# Install Node 20 + claude CLI via npm + git.
# Rationale: https://claude.ai/install.sh is geo-blocked in some Zeabur
# datacenters (confirmed HK, returns 403). registry.npmjs.org is not blocked
# and api.anthropic.com is reachable from the same regions, so npm install
# followed by `claude /login` device-code flow works at runtime.
# git is needed so Player agents can commit/push their work via Bash, and
# so M4 worktree provisioning works. bubblewrap is required by Codex's
# sandbox layer; without it, app-server falls back to a vendored binary and
# can terminate the stdio transport on sandboxed turns.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git ripgrep bubblewrap \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code @openai/codex @playwright/mcp \
    && bwrap --version > /dev/null \
    && codex app-server --help > /dev/null \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Playwright Chromium + system libs. Two consumers:
#   1. The `@playwright/mcp` server (installed above via npm) which
#      exposes browser_navigate / browser_click / browser_snapshot /
#      browser_take_screenshot / etc. as MCP tools to every agent
#      whose project enables it via the Options drawer. Default
#      browser is `chrome` (Google Chrome stable) — the example
#      config in `mcp-servers.example.json` passes
#      `--browser chromium` so it uses the bundle below.
#   2. Project-side test suites that drive a real browser via the
#      Python `playwright` library through Bash. Pulled in by
#      pyproject.toml's runtime deps where applicable; the Node
#      install below shares the same `~/.cache/ms-playwright`
#      directory so Python Playwright finds the chromium revision
#      installed here.
# Important: we install via the Node side (`npx -p @playwright/mcp@latest
# playwright install`) so the Chromium revision matches what the Node
# Playwright bundled inside `@playwright/mcp` expects at runtime.
# Installing via Python first would download a different revision
# whenever the Python `playwright` package's pin drifts away from the
# Node side's, breaking the MCP. `--with-deps` pulls X11/font/audio
# shared libs Chromium needs to launch headless. Adds ~400 MB to the
# image.
RUN npx -y -p @playwright/mcp@latest playwright install --with-deps chromium

# Default git identity for agents that commit. Override via env at deploy
# time if you want per-deployment attribution.
ARG GIT_USER_NAME="TeamOfTen Harness"
ARG GIT_USER_EMAIL="harness@teamoften.local"
RUN git config --global user.name "${GIT_USER_NAME}" \
    && git config --global user.email "${GIT_USER_EMAIL}" \
    && git config --global init.defaultBranch main \
    && git config --global --add safe.directory '*'

# Persistent data dir for SQLite — on Zeabur, mount a volume at /data
# (matches the DB_PATH default in server/db.py). We deliberately do NOT
# pre-create /data in the image: Zeabur's bind-mount over an already-
# existing directory causes SQLite's file probe to hang silently at
# startup (confirmed against M2a, 2026-04-22). Letting the volume
# create the path avoids that.
# Without a volume, init_db falls back to creating /data on the
# ephemeral container filesystem — still works, just not persistent.

WORKDIR /app

COPY pyproject.toml ./

# Install the dependency graph before copying server sources so Zeabur can
# cache this slow layer across normal application-code redeploys. The final
# package install below uses --no-deps and is cheap when only server/ changes.
RUN python -c "import subprocess, sys, tomllib; data = tomllib.load(open('pyproject.toml', 'rb')); deps = data['build-system']['requires'] + data['project']['dependencies'] + data['project']['optional-dependencies']['dev']; subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--prefer-binary', '--progress-bar', 'off', '--retries', '10', '--timeout', '120', *deps])"

COPY server/ ./server/

# `[dev]` brings in pytest + pytest-asyncio. They're not strictly
# needed by the running server, but Codex-runtime players reach for
# pytest directly via their `shell` tool — when it isn't on PATH the
# agent burns a turn investigating the env before finding a
# project-local runner. Installing here puts a working pytest at
# /usr/local/bin so any agent (Claude or Codex) can fall through to
# it when the project repo doesn't bring its own.
RUN pip install --no-deps --no-build-isolation .

EXPOSE 8000

# Healthcheck. Hits /api/health every 30s; two misses in a row mark
# the container unhealthy (90 s boot grace so init_db + active-project
# worktree provisioning have time). Endpoint is public (doesn't
# require HARNESS_TOKEN),
# curl is already installed above. --fail makes non-2xx exit 22, so
# /api/health returning 503 (any required subsystem red) correctly
# marks the container unhealthy without us parsing the body.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=2 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/api/health" > /dev/null || exit 1

# Listen on $PORT if Zeabur sets one, fall back to 8000
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

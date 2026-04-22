FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Install Node 20 + claude CLI via npm.
# Rationale: https://claude.ai/install.sh is geo-blocked in some Zeabur
# datacenters (confirmed HK, returns 403). registry.npmjs.org is not blocked
# and api.anthropic.com is reachable from the same regions, so npm install
# followed by `claude /login` device-code flow works at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Workspaces — one per slot (Coach + p1..p10) plus a default.
# In M2a these are plain dirs; per-slot git worktrees come in M4+.
RUN mkdir -p /workspaces/default /workspaces/coach \
    && for i in 1 2 3 4 5 6 7 8 9 10; do mkdir -p "/workspaces/p${i}"; done

# Persistent data dir for SQLite — mount a Zeabur volume here to survive
# redeploys. If no volume is mounted, the DB lives on the ephemeral
# container filesystem and is wiped on each deploy.
RUN mkdir -p /var/lib/harness

WORKDIR /app

COPY pyproject.toml ./
COPY server/ ./server/

RUN pip install .

EXPOSE 8000

# Listen on $PORT if Zeabur sets one, fall back to 8000
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

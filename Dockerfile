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
# Each workspace also symlinks `attachments/` to /data/attachments so an
# agent whose cwd is /workspaces/<slot>/ can Read pasted images via a
# workspace-local path, regardless of whether the SDK's Read tool
# restricts paths to the cwd subtree. The symlink target doesn't need
# to exist at build time — the /data volume mounts + ATTACHMENTS_DIR is
# created in lifespan. (We deliberately do NOT `mkdir /data` in the
# image, see memory/zeabur_volumes.md for why.)
RUN mkdir -p /workspaces/default /workspaces/coach \
    && for i in 1 2 3 4 5 6 7 8 9 10; do mkdir -p "/workspaces/p${i}"; done \
    && for slot in default coach p1 p2 p3 p4 p5 p6 p7 p8 p9 p10; do \
         ln -s /data/attachments "/workspaces/${slot}/attachments"; \
       done

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
COPY server/ ./server/

RUN pip install .

EXPOSE 8000

# Listen on $PORT if Zeabur sets one, fall back to 8000
CMD ["sh", "-c", "exec uvicorn server.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

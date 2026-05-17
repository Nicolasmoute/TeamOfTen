---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 3: Tech Stack'
section: 3
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 3. Tech Stack

| Layer | Current choice |
| --- | --- |
| Agent runtime | Claude Agent SDK and Claude Code CLI |
| Backend | FastAPI, asyncio, WebSocket |
| Database | SQLite via `aiosqlite`, DELETE journal mode |
| Frontend | Preact 10, htm, Split.js, vendored marked + DOMPurify + highlight.js + diff + KaTeX + mermaid |
| Durable mirror | WebDAV via `webdav4` |
| Auth to Claude | Claude CLI OAuth credentials in `CLAUDE_CONFIG_DIR` |
| UI auth | Optional bearer token from `HARNESS_TOKEN` |
| Secrets | Fernet-encrypted SQLite table keyed by `HARNESS_SECRETS_KEY` |
| Deployment | Single Dockerfile, Python 3.12 slim, Node 20, Claude Code npm package |
| Tests | pytest, pytest-asyncio |

Important deployment decisions:

- The Dockerfile installs Claude Code with `npm install -g @anthropic-ai/claude-code`
  because the upstream install script has been unreliable/geoblocked in some
  deploy regions.
- The image deliberately does not create `/data`; mounted volumes over an
  existing `/data` path caused SQLite startup hangs on Zeabur.
- SQLite uses DELETE journal mode, not WAL, because WAL was unreliable on the
  target volume backend.
- Static assets are served directly from `server/static`; no frontend build
  step exists.
- The image installs `ripgrep` alongside `git` so Codex Players (which
  use the native `shell` tool to grep) don't fall back to the much
  slower `find` on every search. Claude Players bundle ripgrep behind
  the SDK's `Grep` tool so they were unaffected; this gap only
  surfaced when Codex agents hit it directly via `shell`.
- The Dockerfile installs the Python dependency graph from
  `pyproject.toml` before copying `server/`, then installs the
  harness package with `pip install --no-deps --no-build-isolation .`.
  This keeps normal server-code redeploys from invalidating the slow
  dependency-download layer. The installed dependency graph includes
  the `dev` extra, which adds `pytest` + `pytest-asyncio` to
  `/usr/local/bin`. Same rationale as ripgrep: Codex Players reach
  for `pytest` directly via `shell`, and a missing binary turns into
  a multi-turn detour while the agent investigates the env. Project
  repos that bring their own pytest still win via venv activation;
  the system pytest is a fallback.
- The image bakes in Playwright Chromium via the Node Playwright
  installer (`npx -p @playwright/mcp@latest playwright install
  --with-deps chromium`) and installs the `@playwright/mcp` npm
  package alongside Claude Code and Codex. A
  project that opts in via the Options drawer → MCP servers gets
  `browser_navigate` / `browser_click` / `browser_snapshot` /
  `browser_take_screenshot` / etc. as MCP tools, so any agent can
  drive a real headless browser to test pages. The Python
  `playwright` library remains available via Bash for project test
  suites that script their own browser flows. Adds ~400 MB to the
  image. See `mcp-servers.example.json` for the canonical stanza
  and recommended `allowed_tools` list.

---

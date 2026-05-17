---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 20: Environment Variables'
section: 20
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 20. Environment Variables

Operator-facing env vars (the minimum you actually configure per
deploy) live in [`.env.example`](../.env.example): `HARNESS_TOKEN`,
`HARNESS_WEBDAV_URL` + `_USER` + `_PASSWORD`, `HARNESS_AGENT_DAILY_CAP`
+ `HARNESS_TEAM_DAILY_CAP`, `HARNESS_CODEX_ENABLED`,
`HARNESS_SECRETS_KEY`, and the `TELEGRAM_*` first-boot bootstrap
pair. Everything else has a Dockerfile-baked value, a code default,
or has moved to UI/DB management.

Full reference (every `os.environ.get("HARNESS_…"` site in the
implementation):

| Variable | Default | Purpose |
| --- | --- | --- |
| `HARNESS_TOKEN` | unset | Optional API/WS bearer token. Deployment process env only; not resolved from the encrypted secrets table and not exported to agent runtimes. |
| `CLAUDE_CONFIG_DIR` | `/data/claude` | Claude OAuth/session dir |
| `CODEX_HOME` | `/data/codex` | Codex CLI auth dir (`auth.json`). Must point at persistent storage; after deploy run `CODEX_HOME=/data/codex codex login --device-auth` in the container to create the ChatGPT OAuth session. |
| `HARNESS_CODEX_ENABLED` | unset | Codex runtime feature gate. Must be truthy (`true`, `1`, `yes`, `on`) before `PUT /api/agents/{id}/runtime` or the UI runtime controls can select `runtime=codex`. |
| `HARNESS_CODEX_EXTERNAL_MCP` | unset / false | When truthy, CodexRuntime ambient-starts UI/file-configured external MCP servers. Default false: only `coord` starts unless the final spawn allowlist contains an external `mcp__<server>__...` tool from role/slot `agents.allowed_tools` or team-wide `extra_tools`. |
| `HARNESS_CODEX_RUNTIME_HOME` | `$CODEX_HOME/harness-runtime` | Optional root for per-slot Codex app-server homes. Runtime homes copy `$CODEX_HOME/auth.json` but use a clean config without inherited `mcp_servers`, preventing operator/test MCP config from poisoning harness Codex sessions. |
| `HARNESS_CODEX_REQUEST_TIMEOUT_SECONDS` | `120` | Codex app-server JSON-RPC request timeout passed to `CodexClient.connect_stdio`; clamped to at least 30s. Covers `initialize`, `thread/start`, `thread/resume`, and similar request/response calls. |
| `HARNESS_CODEX_STDIO_LIMIT_BYTES` | `8388608` | Codex app-server subprocess stdout/stderr StreamReader line limit for newline-delimited JSON-RPC. Clamped to 256 KiB..64 MiB; prevents large tool/result messages from tripping Python's 64 KiB default and surfacing as false stdio transport failures. |
| `HARNESS_DB_PATH` | `/data/harness.db` | SQLite path |
| `HARNESS_DATA_ROOT` | `/data` | Global/project data root |
| `HARNESS_WEBDAV_URL` | unset | WebDAV base folder URL |
| `HARNESS_WEBDAV_USER` | unset | WebDAV username |
| `HARNESS_WEBDAV_PASSWORD` | unset | WebDAV app password |
| `HARNESS_WEBDAV_SNAPSHOT_INTERVAL` | `300` | DB snapshot cadence |
| `HARNESS_WEBDAV_SNAPSHOT_RETENTION` | `144` | Snapshot count |
| `HARNESS_PROJECT_SYNC_INTERVAL` | `300` | Active project file sync |
| `HARNESS_GLOBAL_SYNC_INTERVAL` | `1800` | Global file sync |
| `HARNESS_KDRIVE_RETRY_MAX` | `3` | WebDAV per-file retry attempts |
| `HARNESS_KDRIVE_RETRY_INITIAL_S` | `1.0` | Initial retry delay |
| `HARNESS_KDRIVE_RETRY_CAP_S` | `30.0` | Retry delay cap |
| `HARNESS_KDRIVE_CLOSE_TIMEOUT_S` | `60` | Switch push-on-close timeout |
| `HARNESS_LIVE_CONVERSATION_S` | `30` | Recent conversation live tag window |
| `HARNESS_AGENT_DAILY_CAP` | `5.0` | Per-agent daily spend cap |
| `HARNESS_TEAM_DAILY_CAP` | `20.0` | Team daily spend cap |
| `HARNESS_COACH_TICK_INTERVAL` | `0` | **Deprecated.** Honored only on first migration to seed a tick row in `coach_recurrence`. After that the env var is ignored — runtime control is via `PUT /api/coach/tick` or `/tick N`. Removed from `.env.example`. |
| `HARNESS_RECURRENCE_TICK_SECONDS` | `30` | Scheduler resolution for `recurrence_scheduler_loop` |
| `HARNESS_MAX_RECURRENCES_PER_PROJECT` | `50` | Soft cap per project; POST 409s when exceeded |
| `HARNESS_AUTOWAKE_DEBOUNCE` | `10` | Auto-wake debounce seconds |
| `HARNESS_ERROR_RETRY_DELAY` | `45` | Error retry delay |
| `HARNESS_ERROR_RETRY_MAX_CONSECUTIVE` | `3` | Retry limit |
| `HARNESS_ERROR_DM_DEBOUNCE` | `300` | Player-error Coach DM debounce |
| `HARNESS_STALE_TASK_MINUTES` | `15` | Stale task threshold, 0 disables |
| `HARNESS_STALE_TASK_NOTIFY_INTERVAL_MINUTES` | `30` | Re-notify cadence |
| `HARNESS_STALE_TASK_CHECK_INTERVAL_SECONDS` | `60` | Watchdog loop cadence |
| `HARNESS_AUTO_COMPACT_THRESHOLD` | `0.65` | Context fraction for auto-compact (lowered from 0.7 on 2026-05-09, then raised from 0.5 on 2026-05-15) |
| `HARNESS_THINKING_BUDGET_TOKENS` | `8000` | Extended-thinking budget when a Player's `thinking_override` (or per-pane toggle) is on. Claude runtime only; clamped ≥ 1024. |
| `HARNESS_HANDOFF_TOKEN_BUDGET` | `20000` | Recent exchange budget |
| `HARNESS_STREAM_TOKENS` | `true` | Token delta streaming. Set to `false`/`0`/`no`/`off` to disable (only needed for the rare CLI build that crashes on the underlying flag). |
| `HARNESS_INTERACTION_TIMEOUT_SECONDS` | `1800` | Question/plan timeout |
| `HARNESS_MCP_CONFIG` | unset | **Legacy.** Path to a static MCP server JSON file. Removed from `.env.example`; the `mcp_servers` table (Options drawer → MCP servers) is the source of truth. DB entries override file entries when both exist. |
| `HARNESS_SECRETS_KEY` | unset | Fernet master key |
| `HARNESS_EVENTS_RETENTION_DAYS` | `30` | Event trim window |
| `HARNESS_EVENTS_TRIM_INTERVAL` | `86400` | Event trim cadence |
| `HARNESS_ATTACHMENTS_RETENTION_DAYS` | `30` | Attachment trim window |
| `HARNESS_SESSION_RETENTION_DAYS` | `30` | Claude JSONL trim window |
| `HARNESS_EVENTS_BATCH_SIZE` | `50` | Event writer batch size |
| `HARNESS_EVENTS_BATCH_INTERVAL` | `0.1` | Event writer flush window |
| `HARNESS_EVENTS_WRITE_QUEUE_SIZE` | `10000` | Event writer queue |
| `HARNESS_ATTACHMENTS_DIR` | project-scoped unless set | Legacy attachment override |
| `HARNESS_OUTPUTS_DIR` | `/data/outputs` | Legacy outputs dir |
| `TELEGRAM_BOT_TOKEN` | unset | Telegram env fallback |
| `TELEGRAM_ALLOWED_CHAT_IDS` | unset | Telegram env fallback |
| `HARNESS_TELEGRAM_ESCALATION_SECONDS` | `300` | Delay before pinging Telegram for an unanswered pending-attention item when the web UI is connected. `0` disables the escalation watcher. |
| `HARNESS_TELEGRAM_ESCALATION_GRACE` | `5` | Delay used instead of the long delay when no WebSocket subscriber is connected at the time the pending event arrives. |
| `PORT` | `8000` | Uvicorn port |

Removed from `.env.example` (kept here for change-log audit; do not
add back unless re-wiring the corresponding code path):

- `HARNESS_CONTEXT_DIR` — never referenced in code.
- `HARNESS_KNOWLEDGE_DIR` — never referenced in code.
- `HARNESS_DECISIONS_DIR` — never referenced in code.
- `HARNESS_HANDOFFS_DIR` — never referenced in code.
- `HARNESS_WORKSPACES_DIR` / `HARNESS_WORKSPACES_ROOT` /
  `HARNESS_PROJECT_REPO` / `HARNESS_PROJECT_BRANCH` — retired with
  the 2026-05-06 workspace refactor. Worktrees now live under
  `/data/projects/<id>/repo/`; repo URL lives on `projects.repo_url`.

Also dropped from `.env.example` (still wired, but defaulted in code
or in the Dockerfile and not configured per-deploy in practice):

- `CLAUDE_CONFIG_DIR` / `CODEX_HOME` — set in the Dockerfile to
  `/data/claude` and `/data/codex`. Override only if the persistent
  volume mount differs.
- `HARNESS_DATA_ROOT` (`/data`) — code defaults match the
  Dockerfile mount points.
- `HARNESS_DB_PATH`, `HARNESS_OUTPUTS_DIR`, `HARNESS_UPLOADS_DIR`,
  `HARNESS_ATTACHMENTS_DIR` — derived from `HARNESS_DATA_ROOT`.
- All retention / interval / debounce / batch-size / threshold
  tuning vars (auto-compact, handoff token budget, error retry,
  stale-task watchdog, event batcher, WebDAV intervals + retries,
  project-sync intervals, recurrence tick resolution, etc.) — code
  defaults are documented at each `os.environ.get` call site.

`.env.example` is the operator-facing minimum; this section is the
implementation-facing complete reference.

---

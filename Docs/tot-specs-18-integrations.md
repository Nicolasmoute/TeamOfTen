---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 18: Integrations'
section: 18
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 18. Integrations

### 18.1 External MCP

UI-managed via the `mcp_servers` DB table (Options drawer → MCP
servers); credentials live in the encrypted secrets store keyed
by `HARNESS_SECRETS_KEY`. The legacy file-config path
(`HARNESS_MCP_CONFIG=/data/mcp-servers.json`) is still loaded if
set, but DB entries override file entries on conflict.

Example file shape (also valid as DB row JSON):

```json
{
  "servers": {
    "github": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}
    }
  },
  "allowed_tools": {
    "github": ["create_issue", "list_issues"]
  }
}
```

Interpolation order for `${VAR}`:

1. Encrypted secrets store.
2. Environment variables.
3. Empty string with warning.

DB-managed MCP servers:

- Saved through Settings drawer.
- Redacted on read.
- Inline secret warnings by regex.
- `test` endpoint checks stdio command path or HTTP reachability.

Codex runtime note: before passing UI/file MCP configs to `codex
app-server`, command-based external servers are normalized for stdio
safety. A config with `command` but no `type` is treated as
`type="stdio"`, and `npx` / `npx.cmd` commands get `-y` injected unless
already present. This prevents cold-redeploy `npx` install prompts from
printing non-JSON to MCP stdout and killing the Codex app-server with a
`serde error expected value at line 1 column 1` transport failure.

By default, CodexRuntime does **not** ambient-start UI/file-configured
external MCP servers. Codex runs MCP servers inside the app-server
subprocess, so one bad external stdio server can kill unrelated Codex
turns. Codex starts external MCP servers only when the final spawn
allowlist contains an `mcp__<server>__...` tool. That
allowlist can come from a role/slot `agents.allowed_tools` entry or the
team-wide `extra_tools` setting. Set `HARNESS_CODEX_EXTERNAL_MCP=true`
to restore ambient external MCP loading for Codex. ClaudeRuntime is
unchanged and continues to load external MCPs from this section through
its normal SDK path.

CodexRuntime also isolates app-server from operator-owned
`$CODEX_HOME/config.toml`. The app-server uses a clean per-slot
runtime home under `$CODEX_HOME/harness-runtime` (or
`HARNESS_CODEX_RUNTIME_HOME`), copies only `auth.json`, and writes a
minimal config with no `mcp_servers`. This prevents old manual/test
MCP entries in the Codex user config from being merged into harness
turns and corrupting the stdio transport.

### 18.2 Secrets Store

Requires:

```text
HARNESS_SECRETS_KEY=<Fernet key>
```

Secret names must match:

```text
^[A-Za-z_][A-Za-z0-9_]{0,63}$
```

Names are entered plain (e.g. `ZEABUR_API_KEY`). The `${NAME}` wrapper
is the *placeholder syntax* used inside config files that interpolate
the secret (MCP configs, repo URLs, anything else routed through
`_interpolate`) — not the secret's name. The Settings drawer input
auto-strips a `${NAME}` or `$NAME` wrapper before submission so a
copy-paste from an MCP config doesn't fail validation. Server-side
validation still enforces the regex above and rejects malformed names
with a 400.

Secrets are general-purpose. They can be referenced anywhere the
harness expands `${VAR}` placeholders — MCP server configs, the
project repo URL, future config fields. The store wins over `os.environ`
on name collision so a UI-stored secret transparently overrides any
matching env var.

This interpolation scope is intentionally narrower than process
environment. Creating a stored secret named `HARNESS_TOKEN` does not
set the FastAPI/UI bearer token and does not make that token visible to
Coach or Player subprocesses. Configure API/WS auth through deployment
process env; keep external-service credentials in the secrets store and
reference them from the specific config field that consumes them.

Values max 32,768 chars through API.

### 18.3 Telegram Bridge

Purpose: send messages to Coach from a phone and receive Coach replies.

Config sources:

- Secret `telegram_bot_token`, env fallback `TELEGRAM_BOT_TOKEN`.
- Secret `telegram_allowed_chat_ids`, env fallback
  `TELEGRAM_ALLOWED_CHAT_IDS`.

Behavior:

- Long-polling `getUpdates`, no webhook needed.
- Only whitelisted numeric chat ids can pilot Coach.
- Inbound text becomes a human message to Coach and wakes Coach.
- `/start` gets a short connection message.
- Non-text messages get a "text only" reply.
- Outbound forwards Coach text only for turns triggered by human-to-Coach
  messages; routine/autonomous turns are silent.
- `human_attention` is always forwarded.
- Replies are split under 4000 chars.
- After repeated 401/403 auth failures, bridge stops and emits
  `human_attention`.

UI endpoints support live reload without redeploy.

#### 18.3.1 Escalation Watcher

A separate background task (`server/telegram_escalation.py`,
`start_escalation_watcher()` in `lifespan`) pings the same
whitelisted chats when a pending-attention item goes unanswered
for too long. Independent of the bridge's outbound buffer; uses
`server.telegram.send_outbound(text)` which resolves the disabled
flag + token + chat_ids fresh on every call (so a UI Clear stops
escalations immediately).

Watched events:

- `pending_question` with `route='human'` (AskUserQuestion).
- `pending_plan` with `route='human'` (ExitPlanMode plan approval).
- `file_write_proposal_created` (truth or `project_claude_md` scope).

Resolution events that cancel the timer:

- `question_answered` / `question_cancelled` (matched on
  `correlation_id`).
- `plan_decided` / `plan_cancelled` (matched on `correlation_id`).
- `file_write_proposal_approved` / `_denied` / `_cancelled` /
  `_superseded` (matched on `proposal_id`).

Delay model:

- `HARNESS_TELEGRAM_ESCALATION_SECONDS` (default 300; 0 disables
  the watcher).
- `HARNESS_TELEGRAM_ESCALATION_GRACE` (default 5).
- Branch chosen at schedule time: full delay when
  `bus.subscriber_count > 0` (web active), grace delay otherwise.

`pending_question(route='coach')` and `pending_plan(route='coach')`
are explicitly ignored — Coach is responsible for those, not the
human. `human_attention` keeps the bridge's existing immediate
forwarding (the agent has already declared "I can't proceed";
adding a delay would slow the most-urgent signal).

Telegram message includes context: agent slot + name + role label
(via `_get_agent_identity`), `ts` and `deadline_at` rendered as
`HH:MM UTC`, the structured questions array (or plan body
truncated to 1500 chars, or proposal scope+path+summary+size),
and a "Open the web UI to answer" footer.

Restart behaviour: timers are in-memory only. A
`file_write_proposal_created` open before a server restart keeps
its `status='pending'` row in the DB but does not re-arm a timer
on next boot. The EnvPane still surfaces it on reconnect.

---

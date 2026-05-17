---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 13: Standard and External Tools'
section: 13
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 13. Standard and External Tools

Baseline tool groups in `server/tools.py`:

```text
STANDARD_READ_TOOLS  = Read, Grep, Glob, ToolSearch
STANDARD_WRITE_TOOLS = Write, Edit, Bash
INTERACTIVE_TOOLS    = AskUserQuestion
```

Coach allowlist:

```text
STANDARD_READ_TOOLS
ALLOWED_COORD_TOOLS
AskUserQuestion
```

Player allowlist:

```text
STANDARD_READ_TOOLS
STANDARD_WRITE_TOOLS
ALLOWED_COORD_TOOLS
AskUserQuestion
```

Team-wide extra tools:

- `WebSearch`
- `WebFetch`

Controlled by:

- `GET /api/team/tools`
- `PUT /api/team/tools`
- stored in `team_config.extra_tools`

The toggle is **team-wide and runtime-shared**: one switch, both runtimes
honor it. The CamelCase names are a backwards-compat artifact (Claude
was the only runtime when storage was set); semantically the toggle
means "the team is allowed to use the web".

Runtime translation:

- ClaudeRuntime: passes the literal strings as `allowed_tools` to the
  SDK — `WebSearch` and `WebFetch` are first-class Claude tools.
- CodexRuntime: maps the toggle onto Codex's native built-in search
  (no per-URL fetch tool exists). See `Docs/CODEX_RUNTIME_SPEC.md`.

External MCP servers:

- Loaded from `HARNESS_MCP_CONFIG`.
- Loaded from `mcp_servers` DB table.
- DB wins on name collision.
- Explicit `allowed_tools` list is required; no automatic tool exposure.
- Tool names become `mcp__<server>__<tool>`.
- CodexRuntime applies a Codex-specific approval-mode injection on
  external servers; see `Docs/CODEX_RUNTIME_SPEC.md` §C.5. Claude
  runtime enforces its allow-list through
  `ClaudeAgentOptions.allowed_tools`.

---

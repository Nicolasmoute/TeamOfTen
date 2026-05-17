---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 23: Current Implementation Gaps and Watch Items'
section: 23
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 23. Current Implementation Gaps and Watch Items

These are not hidden defects in this spec; they are the places where the code
and the desired architecture are still not perfectly aligned.

1. Project repo storage is hybrid.
   - DB/API has per-project `projects.repo_url`.
   - `server.paths` declares per-project repo paths.
   - `server/workspaces.py` still uses global `/workspaces/.project` and
     `/workspaces/<slot>/project`.

2. Outputs are partly wired.
   - `server/outputs.py` exists.
   - `coord_save_output` function exists.
   - `ALLOWED_COORD_TOOLS` includes it.
   - The MCP server registration list currently omits the function.
   - Storage is global `/data/outputs`, not project-scoped.

3. Attachment prompt paths can be wrong in project-scoped mode.
   - Upload API stores under active project's attachments dir by default.
   - Frontend injects `/workspaces/<slot>/attachments/<file>` paths.
   - Docker symlink points to `/data/attachments`, not active project
     attachments.

4. (Resolved 2026-05-13.) Memory has no on-disk local copy by design.
   The store is `memory_docs` (SQLite) + WebDAV mirror at
   `projects/<slug>/memory/<topic>.md`. `working/memory/` is intentionally
   absent from `_PROJECT_SUBDIRS`; agents use `coord_*_memory` exclusively.

5. UI `/tools` help still mentions `coord_write_context`, which was removed.

6. (Resolved 2026-05-02.) `.env.example` is now the operator-facing
   minimum (auth, WebDAV, caps, secrets-key, Codex gate, Telegram
   bootstrap). Pre-projects flat-dir vars
   (`HARNESS_CONTEXT_DIR` / `_KNOWLEDGE_DIR` / `_DECISIONS_DIR` /
   `_HANDOFFS_DIR` / `_WORKSPACES_DIR`) and legacy single-project
   knobs (`HARNESS_PROJECT_REPO`, `HARNESS_PROJECT_BRANCH`,
   `HARNESS_MCP_CONFIG`, `HARNESS_COACH_TICK_INTERVAL`) were
   removed. See §20 for the full implementation reference + change
   log.

7. (Resolved 2026-05-01.) Coach edits the per-project CLAUDE.md via
   `coord_propose_file_write(scope='project_claude_md', path='CLAUDE.md',
   content, summary)`; the user reviews a diff and approves in the EnvPane
   "File-write proposals" section. Direct `Write` / `Edit` / `Bash` against
   `/data/projects/<slug>/CLAUDE.md` is hard-denied by the
   `_pretool_file_guard_hook` (the same hook that protects `truth/`), so
   the proposal flow is the only path in. The harness-wide
   `/data/CLAUDE.md` remains user-only (no proposal scope for it).

8. Project activation rejects any in-flight agent instead of implementing a
   full "cancel turns and switch" server path. The UI has modal language for
   wait/cancel, but the backend switch endpoint itself expects no in-flight
   work.

9. Project sync assumes the harness is the sole writer while a project is
   active. Direct WebDAV edits to active project files may be overwritten.

10. Mobile/touch drag behavior remains less mature than desktop layout.

11. `coord_*` MCP "Stream closed" mid-turn — open investigation.
    Observed 2026-05-12 on p2: every `mcp__coord__*` call in a turn
    returned "Stream closed" until the turn ended; the next wake fired
    a fresh turn with a freshly-built `coord_server`
    ([server/runtimes/claude.py:123](../server/runtimes/claude.py))
    and the calls succeeded. The string originates in the Claude
    Agent SDK, not harness code (grep confirms it's absent from
    `server/`). It indicates the in-process stdio bridge between the
    `claude` CLI subprocess and our Python-side `coord` MCP server
    died mid-turn; once dead, the rest of the turn cannot use any
    coord tool and the player has no path to signal Coach until an
    external wake spawns a new turn. No harness-side detection or
    recovery today.
    Suspected triggers (most-recent first):
    - `ENABLE_TOOL_SEARCH=auto:30` (shipped 2026-05-11,
      [runtimes/claude.py:198-207](../server/runtimes/claude.py)) —
      tool-search retrieval shares the in-process MCP stdio pipe
      with `mcp__coord__*` calls; a race or EOF in the retriever
      task would kill the bridge.
    - `include_partial_messages=True` — known to race with in-process
      MCP in some CLI builds; that is why the `HARNESS_STREAM_TOKENS`
      kill-switch exists.
    - An unhandled exception inside a coord tool handler crashing
      the MCP task.
    Diagnostic plan when next reproduced:
    1. Grep server logs around the failing turn for an
       `claude_agent_sdk` stack trace — the SDK usually logs the
       exception that killed the MCP task before silently turning
       all subsequent calls into "Stream closed".
    2. Test-deploy with `HARNESS_TOOL_SEARCH=false`; if the rate
       drops to zero, that is the smoking gun.
    3. Otherwise try `HARNESS_STREAM_TOKENS=false`.
    Possible mitigation (not yet implemented): a `PostToolUse` hook
    that pattern-matches `Stream closed` in a `mcp__coord__*`
    tool_result, emits `human_attention`, and cancels the turn so
    the next wake can fire a fresh turn cleanly.

---

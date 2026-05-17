---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 24: Hard Invariants'
section: 24
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 24. Hard Invariants

1. Agents must not write SQLite directly. All coordination mutations route
   through server APIs or MCP tool handlers.
2. One active project id scopes all project-state reads and writes.
3. `misc` must always exist.
4. `project_id` filters are required on tasks, messages, memory, events, turns,
   and sessions.
5. Cost caps are checked before spawning a turn.
6. Pausing blocks new starts and loops, not in-flight turns.
7. Task completion/cancellation clears the owner's `current_task_id`.
8. Broadcast read state is per recipient.
9. WebDAV failure must not make local tool writes fail unless the operation is
   explicitly part of project switching.
10. Secrets plaintext must not be returned by APIs.
11. `CLAUDE_CONFIG_DIR` should live on persistent `/data`.
12. Coach baseline must not include standard write tools unless the governance
    model is intentionally changed.
13. `/data/projects/<slug>/truth/*` and `/data/projects/<slug>/CLAUDE.md`
    are agent-read-only. The only mutation path is
    `coord_propose_file_write` (Coach-only) followed by an explicit
    human approve in the EnvPane "File-write proposals" section. The
    `_pretool_file_guard_hook` enforces this for all agents and tools.
14. The harness-wide `/data/CLAUDE.md` is human-only — there is no
    proposal scope for it; it cannot be changed by any agent action.

---

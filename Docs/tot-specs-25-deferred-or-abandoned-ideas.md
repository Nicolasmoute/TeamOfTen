---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 25: Deferred or Abandoned Ideas'
section: 25
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 25. Deferred or Abandoned Ideas

- React/Vite/react-mosaic frontend: replaced by Preact/htm/Split.js.
- Zustand/state store: local hooks are sufficient.
- Docker Compose plus Caddy as primary deployment: replaced by single
  Dockerfile/Zeabur-style deploy.
- Tailscale-only exposure as default: optional operational choice, not baked
  into app.
- Direct WebDAV JSON state as source of truth: replaced by SQLite hot state.
- Lock tools as primary concurrency control: worktrees are the main isolation.
- `/data/context` root and `coord_write_context`: dropped — context
  lives in `/data/CLAUDE.md` (human-only, edited via Files pane) +
  `/data/projects/<active>/CLAUDE.md` (agent-read-only; Coach
  proposes via `coord_propose_file_write(scope='project_claude_md',
  …)` and the human approves with diff review).
- Multiple layout presets and command palette: not implemented.
- PWA push notifications: not implemented.
- Record/replay tooling for events: event log supports it, UI tooling does not.

---

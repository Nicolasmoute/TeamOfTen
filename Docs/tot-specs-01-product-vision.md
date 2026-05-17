---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 1: Product Vision'
section: 1
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 1. Product Vision

TeamOfTen is a personal orchestration harness for running one coordinating
Claude Code agent, called Coach, plus up to ten worker Claude Code agents,
called Players.

The point of the app is not to hide the agents behind an opaque pipeline. The
point is to make their work visible, steerable, and auditable:

1. Coach receives goals, decomposes them, creates tasks, assigns Players, and
   synthesizes progress.
2. Players execute in their own slots and report back through shared tools.
3. The human can watch every pane, intervene in any agent, pause/cancel work,
   inspect shared state, edit files, configure integrations, and switch between
   projects.
4. Durable human-readable outputs are plain files under `/data` and optionally
   mirrored to a WebDAV cloud folder.
5. Hot state lives in one SQLite database controlled by one FastAPI process.

Primary goals:

- Run one Coach plus ten Players on a single VPS/container.
- Use Claude Code / Claude Agent SDK with OAuth credentials persisted on the
  `/data` volume, not API-key billing.
- Keep all coordination transparent through events, panes, tasks, messages,
  memory, and file browsers.
- Support many projects in one harness, with one active project at a time.
- Keep the system small enough to understand: FastAPI, SQLite, Preact, static
  files, no distributed control plane.

Explicit non-goals:

- Multi-user or multi-tenant security.
- Enterprise RBAC/compliance.
- A model-provider abstraction layer.
- Hiding planning and execution behind a black-box "team" abstraction.
- Fully automatic app building without human supervision.

---

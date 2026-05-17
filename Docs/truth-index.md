---
schema: teamoften-truth-index/v1
title: TeamOfTen Truth Index
status: canonical
canonical_parts_pattern: tot-specs-*.md
corpus_scope: root *.md files in this folder
root_specs:
  - file: CODEX_RUNTIME_SPEC.md
    title: 'Codex Runtime Specification'
  - file: claudeCLI-specs.md
    title: 'Claude Code CLI Runtime Specification'
  - file: compass-specs.md
    title: 'Compass Specification'
  - file: kanban-specs-v2.md
    title: 'Kanban v2 Specification'
  - file: playbook-specs.md
    title: 'Playbook Specification'
  - file: recurrence-specs.md
    title: 'Coach Recurrence v2 Specification'
  - file: reply-affordance.md
    title: 'Reply Affordance Contract'
  - file: truthgate-approach.md
    title: 'Truthgate Spec-Compliance Stage'
  - file: truthgate-workflow.md
    title: 'TruthGate Workflow Contract'
  - file: truthscore-specs.md
    title: 'TruthScore Specification'
last_audited: 2026-04-26
last_reorganized: 2026-05-17
parts:
  - section: 1
    title: 'Product Vision'
    file: tot-specs-01-product-vision.md
  - section: 2
    title: 'Repository Shape'
    file: tot-specs-02-repository-shape.md
  - section: 3
    title: 'Tech Stack'
    file: tot-specs-03-tech-stack.md
  - section: 4
    title: 'Project Model'
    file: tot-specs-04-project-model.md
  - section: 5
    title: 'Agent Roster and Governance'
    file: tot-specs-05-agent-roster-and-governance.md
  - section: 6
    title: 'SQLite Data Model'
    file: tot-specs-06-sqlite-data-model.md
  - section: 7
    title: 'Schema Bootstrap'
    file: tot-specs-07-schema-bootstrap.md
  - section: 8
    title: 'Filesystem Layout'
    file: tot-specs-08-filesystem-layout.md
  - section: 9
    title: 'WebDAV Mirror and Sync'
    file: tot-specs-09-webdav-mirror-and-sync.md
  - section: 10
    title: 'Claude Context and Prompt Assembly'
    file: tot-specs-10-claude-context-and-prompt-assembly.md
  - section: 11
    title: 'Agent Runtime'
    file: tot-specs-11-agent-runtime.md
  - section: 12
    title: 'Coordination Tools'
    file: tot-specs-12-coordination-tools.md
  - section: 13
    title: 'Standard and External Tools'
    file: tot-specs-13-standard-and-external-tools.md
  - section: 14
    title: 'Human REST API'
    file: tot-specs-14-human-rest-api.md
  - section: 15
    title: 'WebSocket and Events'
    file: tot-specs-15-websocket-and-events.md
  - section: 16
    title: 'Frontend Specification'
    file: tot-specs-16-frontend-specification.md
  - section: 17
    title: 'Git Workspaces'
    file: tot-specs-17-git-workspaces.md
  - section: 18
    title: 'Integrations'
    file: tot-specs-18-integrations.md
  - section: 19
    title: 'Security and Auth'
    file: tot-specs-19-security-and-auth.md
  - section: 20
    title: 'Environment Variables'
    file: tot-specs-20-environment-variables.md
  - section: 21
    title: 'Retention and Cleanup'
    file: tot-specs-21-retention-and-cleanup.md
  - section: 22
    title: 'Tests'
    file: tot-specs-22-tests.md
  - section: 23
    title: 'Current Implementation Gaps and Watch Items'
    file: tot-specs-23-current-implementation-gaps-and-watch-items.md
  - section: 24
    title: 'Hard Invariants'
    file: tot-specs-24-hard-invariants.md
  - section: 25
    title: 'Deferred or Abandoned Ideas'
    file: tot-specs-25-deferred-or-abandoned-ideas.md
  - section: 26
    title: 'Operator Summary'
    file: tot-specs-26-operator-summary.md
---

# TeamOfTen Truth Index

Canonical index for the TeamOfTen truth and specification corpus.

This index folds the original harness spec, the Projects refactor, and the
current implementation into one navigable corpus. It is intentionally implementation-
aware: when the code has moved ahead of an older design note, this file follows
the code. When the code still has a hybrid or inconsistent area, that is called
out explicitly.

Companion references:

- `CLAUDE.md`: working notes and constraints for agents editing this repo.
- `README.md`: operator-facing overview and quick start.

Dependent specs (subordinate to this document):

- `Docs/CODEX_RUNTIME_SPEC.md` — Codex runtime specifics. This file
  assumes the Claude runtime; Codex is the alternate runtime and its
  behavior, schema additions, error handling, and lifecycle live in
  the dependent doc.
- `Docs/recurrence-specs.md` — Coach recurrence model (tick / repeat
  / cron) and project artifacts (`coach-todos.md`,
  `project-objectives.md`).
- `Docs/compass-specs.md` — Compass autonomous strategy engine
  (lattice, regions, truth corpus, audits, briefings).
- `Docs/truthscore-specs.md` — TruthScore on-demand fidelity
  evaluator. One-shot Sonnet call that scores project state
  (repo at `main`, decisions/, knowledge/, outputs/) against
  the `truth/` corpus on five canonical 1–10 criteria
  (Fidelity, Completeness, Consistency, Currency, Clarity)
  plus a brief overall comment. Invoked via `/truthscore` slash,
  `coord_run_truth_score` MCP tool (Coach + Players), or
  `POST /api/truthscore`. No UI, no scheduler, no recurring run.
  Result written to `working/knowledge/truthscore-<ts>.md`.
- `Docs/kanban-specs-v2.md` — Kanban-shaped task lifecycle (shape (2)
  routing through Coach). Every Coach delegation is a tracked task;
  the kanban records and surfaces but does not auto-route. Coach
  reviews EVERY stage transition via `coord_approve_stage` — there is
  no `auto_advance` flag and no auto-routing escape hatch (per
  kanban-specs-v2.md §16.2 + §23). Trajectory Coach defines on
  `coord_create_task` is the planned contract; pools are FYI only
  (Coach explicitly assigns one named Player at each transition).
  Stages: truthgate → plan → execute → audit_syntax → audit_semantics → ship →
  optional verify → archive. Backlog promotion enters `truthgate`
  without planting or waking a Player, and `truthgate → plan|execute`
  requires a recorded pass/override verdict. A new per-project event log feeds Coach's tick context;
  pattern-detection counters (Player health, audit aggregator,
  push-time deviation flag, recent-patterns block) surface drift
  proactively. v1 archive at `Docs/kanban-specs-v1-archived.md`.

These docs are subordinate: when a dependent disagrees with this one,
truth-index.md wins. Dependents may go deeper on their own subject but
cannot redefine fields, endpoints, events, or invariants declared
here.

Last audited from the repository on 2026-04-26.

---
---

## Root Spec Set

The root Markdown files in this folder are the flat canonical corpus. No nested spec folders; no generated binary assets in the corpus.

- [CODEX_RUNTIME_SPEC.md](CODEX_RUNTIME_SPEC.md) - Codex Runtime Specification
- [claudeCLI-specs.md](claudeCLI-specs.md) - Claude Code CLI Runtime Specification
- [compass-specs.md](compass-specs.md) - Compass Specification
- [kanban-specs-v2.md](kanban-specs-v2.md) - Kanban v2 Specification
- [playbook-specs.md](playbook-specs.md) - Playbook Specification
- [recurrence-specs.md](recurrence-specs.md) - Coach Recurrence v2 Specification
- [reply-affordance.md](reply-affordance.md) - Reply Affordance Contract
- [truthgate-approach.md](truthgate-approach.md) - Truthgate Spec-Compliance Stage
- [truthgate-workflow.md](truthgate-workflow.md) - TruthGate Workflow Contract
- [truthscore-specs.md](truthscore-specs.md) - TruthScore Specification
## Modular Spec Index

The canonical TeamOfTen spec is this index plus the flat section files named `tot-specs-*.md`. Keep this file small: add new details to the relevant section file, then update the YAML `parts` list or the section map here only when the module structure changes.

The numbered TeamOfTen sections live in the flat module files below. Keep behavioral detail there so this index stays compact.
- [1. Product Vision](tot-specs-01-product-vision.md)
- [2. Repository Shape](tot-specs-02-repository-shape.md)
- [3. Tech Stack](tot-specs-03-tech-stack.md)
- [4. Project Model](tot-specs-04-project-model.md)
  - 4.1 Project Identifier
  - 4.2 Project Row
  - 4.3 Active Project
  - 4.4 Project Lifecycle
  - 4.5 Project Switch Flow
  - 4.6 Project Repo Layout
- [5. Agent Roster and Governance](tot-specs-05-agent-roster-and-governance.md)
  - 5.1 Coach Responsibilities
  - 5.2 Player Responsibilities
  - 5.3 Structural Enforcement
  - 5.4 Player Lock
- [6. SQLite Data Model](tot-specs-06-sqlite-data-model.md)
  - 6.1 `projects`
  - 6.2 `agents`
  - 6.3 `agent_project_roles`
  - 6.4 `agent_sessions`
  - 6.5 `tasks`
  - 6.6 `messages`
  - 6.7 `message_reads`
  - 6.8 `memory_docs`
  - 6.9 `events`
  - 6.10 `turns`
  - 6.11 `team_config`
  - 6.12 `mcp_servers`
  - 6.13 `secrets`
  - 6.14 `sync_state`
  - 6.15 `file_write_proposals`
- [7. Schema Bootstrap](tot-specs-07-schema-bootstrap.md)
- [8. Filesystem Layout](tot-specs-08-filesystem-layout.md)
  - 8.1 Global Tree
  - 8.2 Project Tree
  - 8.3 Project CLAUDE.md Stub
  - 8.3a Project CLAUDE.md Proposal Lane
  - 8.4 File Browser Roots
- [9. WebDAV Mirror and Sync](tot-specs-09-webdav-mirror-and-sync.md)
  - 9.1 DB Snapshots
  - 9.1a Top-Level Uploads Pull
  - 9.2 Active Project Sync
  - 9.3 Global Sync
  - 9.4 Pull on Open
  - 9.5 Push on Close
- [10. Claude Context and Prompt Assembly](tot-specs-10-claude-context-and-prompt-assembly.md)
  - 10.0 Section ordering and Anthropic prompt caching
  - 10.1 Identity
  - 10.2 Coach Coordination Block
  - 10.3 Compact and Continuity
  - 10.4 Context Usage UI
  - 10.5 Prompt-size telemetry (offline analysis)
- [11. Agent Runtime](tot-specs-11-agent-runtime.md)
  - 11.1 Pause and Cancel
  - 11.2 Cost Caps
  - 11.3 Coach Recurrences (formerly Coach Loops)
  - 11.4 Auto-Wake
  - 11.5 Error Retry
  - 11.6 Stale Task Watchdog
  - 11.7 Crash Recovery
  - 11.8 Idle-Poller and Runtime Transfers
  - 11.9 Execute/Ship Stage Boundary in Wake Notes
- [12. Coordination Tools](tot-specs-12-coordination-tools.md)
  - 12.1 Task Tools
  - 12.2 Messaging
  - 12.3 Memory
  - 12.4 Knowledge
  - 12.5 Outputs
  - 12.6 Git
  - 12.6.1 Ship to Dev
  - 12.7 Decisions
  - 12.7.5 File-write Proposals
  - 12.7.6 Project File Reads
  - 12.8 Team Identity
  - 12.9 Interactive Question/Plan Tools
  - 12.10 Context editing
- [13. Standard and External Tools](tot-specs-13-standard-and-external-tools.md)
- [14. Human REST API](tot-specs-14-human-rest-api.md)
  - 14.1 Health and Status
  - 14.2 Claude Auth
  - 14.2.1 In-app OAuth login
  - 14.2.2 In-app Codex OAuth login (device-code flow)
  - 14.3 Agents
  - 14.4 Coach Controls
  - 14.5 Tasks
  - 14.5.5 Backlog
  - 14.6 Messages
  - 14.7 Memory and Decisions
  - 14.7.5 File-write proposals
  - 14.8 Files
  - 14.9 Events and Turns
  - 14.10 Attachments
  - 14.11 Pending Interactions
  - 14.12 Team Configuration
  - 14.13 MCP and Secrets
- [15. WebSocket and Events](tot-specs-15-websocket-and-events.md)
- [16. Frontend Specification](tot-specs-16-frontend-specification.md)
  - 16.1 App Shell
  - 16.2 Left Rail
  - 16.3 Agent Pane
  - 16.4 Slash Commands
  - 16.5 Files Pane
  - 16.6 Environment Pane
  - 16.7 Settings Drawer
  - 16.8 Token Gate
  - 16.9 Mobile Layout
  - 16.10 Markdown Render Pipeline
- [17. Git Workspaces](tot-specs-17-git-workspaces.md)
- [18. Integrations](tot-specs-18-integrations.md)
  - 18.1 External MCP
  - 18.2 Secrets Store
  - 18.3 Telegram Bridge
  - 18.3.1 Escalation Watcher
- [19. Security and Auth](tot-specs-19-security-and-auth.md)
  - 19.1 UI/API
  - 19.2 Claude OAuth
  - 19.3 WebDAV Credentials
  - 19.4 MCP/Telegram Secrets
  - 19.4.1 Secret-path agent guard
  - 19.5 Per-agent runtime selection
  - 19.6 Coord MCP proxy (loopback)
- [20. Environment Variables](tot-specs-20-environment-variables.md)
- [21. Retention and Cleanup](tot-specs-21-retention-and-cleanup.md)
- [22. Tests](tot-specs-22-tests.md)
- [23. Current Implementation Gaps and Watch Items](tot-specs-23-current-implementation-gaps-and-watch-items.md)
- [24. Hard Invariants](tot-specs-24-hard-invariants.md)
- [25. Deferred or Abandoned Ideas](tot-specs-25-deferred-or-abandoned-ideas.md)
- [26. Operator Summary](tot-specs-26-operator-summary.md)

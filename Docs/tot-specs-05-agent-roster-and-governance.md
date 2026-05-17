---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 5: Agent Roster and Governance'
section: 5
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 5. Agent Roster and Governance

The roster is fixed:

| Slot | Kind | Notes |
| --- | --- | --- |
| `coach` | Coach | Coordinator, planner, delegator |
| `p1` ... `p10` | Player | Worker slots |

The slot ids are global and stable across projects. Identity is project-scoped:

- `agent_project_roles(slot, project_id, name, role, brief)`

Operational state is global:

- `agents.status`
- `agents.current_task_id`
- `agents.model`
- `agents.workspace_path`
- `agents.locked`
- `agents.allowed_extra_tools`

Sessions are project-scoped:

- `agent_sessions(slot, project_id, session_id, continuity_note,
  last_exchange_json, last_active)`

### 5.1 Coach Responsibilities

Coach:

- Reads human goals and Player reports.
- Creates top-level tasks (`coord_create_task` with a trajectory).
- Drives stage transitions and assigns Players via
  `coord_approve_stage` (the single v2 transition tool).
- Assigns player names/roles with `coord_set_player_role`.
- Tunes per-Player execution knobs via
  `coord_set_player_runtime` / `coord_set_player_model` /
  `coord_set_player_effort` / `coord_set_player_plan_mode`. Reads the
  current state with `coord_get_player_settings` (one slot or whole
  roster) before changing anything so the team doesn't churn already-
  correct settings. The four tools mutate per-(slot, project)
  override columns; resolution at spawn time is per-pane request →
  Coach override → team-level role default (Settings drawer) →
  hardcoded role default (`models_catalog`: `latest_opus` for Coach,
  `latest_sonnet` for Players, medium effort, plan-mode off) → SDK
  default. Coach overrides apply uniformly to auto-wake spawns (task
  assignments, direct messages) and to direct human prompts that
  don't set a per-pane value.
- Writes decisions.
- Monitors stalled work.
- Answers Player plan/question interactions routed to Coach.
- Does not write code directly.

Coach has read tools plus coordination tools and interactive tools:

```text
Read, Grep, Glob, ToolSearch
coord_* tools
AskUserQuestion
```

Coach does not receive `Write`, `Edit`, or `Bash` in its role baseline.

### 5.2 Player Responsibilities

Players:

- Read inbox and task board.
- Claim or execute assigned tasks.
- Work in their slot workspace.
- Use `Write`, `Edit`, `Bash` for code/file work.
- Update tasks and shared memory.
- Write knowledge artifacts.
- Commit and push work.
- Ask Coach/human for help when blocked.

Players can message peers but cannot assign work to peers.

### 5.3 Structural Enforcement

Hard enforcement in `server/tools.py`:

- Only Coach can directly assign tasks to Players.
- Only Coach can assign player names/roles.
- Only Coach can write decisions.
- Only Coach can set per-Player runtime/model/effort/plan-mode
  overrides; the corresponding `coord_set_player_*` tools reject
  Player callers and `coord_get_player_settings` is Coach-only as
  well. The MCP tools accept `p1..p10` for `player_id`; Coach's own
  effort/plan-mode have no MCP path (the human controls them via
  Coach's pane gear).
- Only Players can claim tasks.
- Coach cannot use standard mutating tools through the baseline allowlist.
- Players can only create subtasks under a task they own; only Coach or human
  can create top-level tasks.
- Task updates are limited to task owner, with Coach allowed to cancel.
- Locked Players cannot receive direct Coach assignments/messages; they skip
  Coach-sourced inbox reads.

Soft enforcement:

- Role system prompts describe Coach as delegator and Players as executors.
- Prompt suffix injects identity, project context, and governance docs.

### 5.4 Player Lock

`agents.locked` is a global per-slot flag, controlled by:

- `PUT /api/agents/{agent_id}/locked`
- Pane lock button.

When a Player is locked:

- Coach `coord_approve_stage(assignee=<locked-slot>)` fails.
- Coach direct `coord_send_message` to that Player fails.
- Coach broadcasts can be queued, but locked Players filter them out when
  calling `coord_read_inbox`.
- Human prompts and peer messages still pass.
- The Player can still read shared docs and work when directly prompted by the
  human.

---

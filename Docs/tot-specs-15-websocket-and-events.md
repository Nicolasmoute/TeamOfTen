---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 15: WebSocket and Events'
section: 15
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 15. WebSocket and Events

WebSocket:

```text
GET /ws?token=<HARNESS_TOKEN>
```

Behavior:

- Token query param is required when `HARNESS_TOKEN` is set.
- Sends `connected` immediately.
- Subscribes to `EventBus`.
- Sends live events as JSON.
- Sends `ping` every 30s of quiet.
- Does not replay backlog; clients load history through `/api/events`.

Event persistence:

- `EventBus.publish()` fans out to subscribers immediately.
- Non-transient events are queued for batched SQLite insert.
- Batch size default: `HARNESS_EVENTS_BATCH_SIZE=50`.
- Batch interval default: `HARNESS_EVENTS_BATCH_INTERVAL=0.1`.
- Queue size default: `HARNESS_EVENTS_WRITE_QUEUE_SIZE=10000`.
- If writer queue is full, falls back to single insert task.
- Lifespan shutdown enqueues a sentinel and waits for the writer to flush any
  in-flight partial batch before the final drain, so redeploys do not drop
  events already claimed by the writer.

Important event types:

Agent lifecycle:

- `agent_started`
- `text`
- `thinking`
- `text_delta`
- `thinking_delta`
- `tool_use`
- `tool_result`
- `result`
- `error`
- `agent_stopped`
- `agent_cancelled`
- `spawn_rejected`
- `paused`
- `cost_capped`
- `cost_reset` (manual reset of today_usd via `POST /api/turns/reset`)
- `session_cleared`
- `session_resume_failed`
- `session_compact_requested`
- `session_compacted`
- `session_transfer_requested` — runtime transfer queued (compact + flip on success)
- `session_transferred` — runtime flipped after a successful transfer compact, or fired immediately when no source-runtime session existed (carries `from_runtime`, `to_runtime`, optional `note=no_prior_session`)
- `session_transfer_failed` — Claude-side transfer compact returned no summary; runtime stays put
- `runtime_updated` — `agents.runtime_override` changed (carries `runtime_override`; `source=session_transfer` when fired by the transfer flow rather than a blunt PUT)
- `auto_compact_triggered`
- `auto_compact_failed`
- `auto_compact_skipped` - Codex auto-compact preflight found a thread,
  but the compact handler's re-read found it already cleared; no
  `session_compacted(0 chars)` event is emitted.
- `compact_empty_forced`
- `context_applied`
- `context_usage`

`tool_use` payloads use Claude's renderer shape: `name`, `id`, and
`input`. Codex also carries a duplicate `tool` alias for runtime
debugging, but the UI must prefer `name` and fall back to `tool` for
older persisted Codex rows. Codex MCP calls unwrap protocol wrapper
fields and pass the actual coord_* arguments as `input`.

Task and coordination:

- `task_created`
- `task_claimed`
- `task_assigned`
- `task_updated`
- `message_sent`
- `memory_updated`
- `knowledge_written`
- `output_saved`
- `decision_written`
- `commit_pushed`
- `player_assigned`
- `agent_model_set` (Coach set/cleared a Player's model_override; carries `{player_id, to: pid, model}`. The empty-string `model` is the cleared marker.)
- `agent_effort_set` (Coach set/cleared a Player's effort_override; carries `{player_id, to: pid, effort: int|null}`.)
- `agent_plan_mode_set` (Coach set/cleared a Player's plan_mode_override; carries `{player_id, to: pid, plan_mode: 0|1|null}`.)
- `agent_thinking_set` (Coach set/cleared a Player's thinking_override; carries `{player_id, to: pid, thinking: 0|1|null}`. Claude runtime only at spawn time; Codex Players store the value but ignore it.)
- `runtime_updated` (Coach or human flipped a Player's runtime_override; carries `{player_id, runtime_override: 'claude'|'codex'|null}`.)
- `brief_updated`
- `lock_updated`
- `human_attention`

Recurrences and runtime:

- `pause_toggled`
- `recurrence_added`
- `recurrence_changed`
- `recurrence_deleted`
- `recurrence_fired`
- `recurrence_skipped`
- `recurrence_deferred`
- `recurrence_disabled`
- `coach_todo_added`
- `coach_todo_completed`
- `coach_todo_updated`
- `objectives_updated`
- `team_tools_updated`
- `team_models_updated`

Projects:

- `project_created`
- `project_updated`
- `project_deleted`
- `project_switch_step`
- `project_switched`
- `project_repo_provisioned`

Integrations:

- `mcp_server_saved`
- `mcp_server_updated`
- `mcp_server_deleted`
- `mcp_server_tested`
- `secret_written`
- `secret_deleted`
- `team_telegram_updated`
- `team_telegram_cleared`
- `claude_auth_updated`
- `kdrive_sync_failed`

Interactions:

- `question_answered`
- `plan_decided`
- `interaction_extended`

File-write proposals (covers both `truth` and `project_claude_md`
scopes — payloads carry `scope`):

- `file_write_proposal_created` (emitted by
  `coord_propose_file_write` — `agent_id=coach`; payload includes
  `proposal_id`, `scope`, `path`, `summary`, `size`, and
  `superseded` listing any prior pending IDs that this proposal
  auto-superseded for the same `(scope, path)`)
- `file_write_proposal_superseded` (emitted once per old row when
  `coord_propose_file_write` fires for a `(scope, path)` with a
  pending proposal — `agent_id=system`; payload `proposal_id`,
  `superseded_by`, `scope`, `path`)
- `file_write_proposal_approved` (emitted by
  `POST /api/file-write-proposals/{id}/approve` — `agent_id=human`;
  payload includes the proposer, `scope`, `path`, summary, written
  byte size, optional note, and `actor` audit metadata)
- `file_write_proposal_denied` (parallel to approved; `size=0`)
- `file_write_proposal_cancelled` (parallel; reserved for the
  cancel resolver path)

File/browser:

- `file_written` (emitted by `PUT /api/files/write/<root>?path=…`,
  including via the Files-pane "+ new file" button which posts an
  empty body)

---

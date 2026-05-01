"""Compass — autonomous strategy engine for TeamOfTen.

A side-engine that maintains a per-project lattice of weighted
statements, asks the human focused questions, exposes its current
best guess to Coach via MCP tools, and audits work artifacts on
demand. Compass never dispatches work, never amends truth without
human approval, never blocks Players.

Spec: Docs/compass-specs.md (read it before touching anything here).
Implementation plan: per-project, opt-in, Coach-only at the MCP
surface, harness-styled UI in v1. State lives in JSON under
`/data/projects/<id>/working/compass/` with a synchronous kDrive
mirror at `projects/<id>/compass/` (working/ prefix dropped on remote
for a flatter human-facing tree, matching the knowledge/ convention).

The package is layered:
  - `config` — caps, thresholds, schema version, env knobs
  - `paths` — per-project compass directory resolver (local + remote)
  - `store` — JSON read/write, atomic save, kDrive mirror
  - `llm` — `call()` wrapper around `claude_agent_sdk.query()` +
    `parse_json_safe()` helper. Uses Max-OAuth via the SDK; cost
    feeds the existing `turns` ledger under agent_id="compass".
  - `prompts` — the 13 prompt templates from spec §8
  - `pipeline/*` — pure functions: digest, questions, reviews,
    regions, truth_check, briefing, claude_md (each returns proposed
    updates; runner.py applies them)
  - `audit` — `audit_work()` + rollup safety net (§5.4)
  - `presence` — human-reachable detection (messages + heartbeat)
  - `runner` — `run(project_id, mode)` orchestrating §3.1-§3.10
  - `scheduler` — `compass_scheduler_loop()` background task
  - `api` — FastAPI router for `/api/compass/*`

MCP tools (`compass_ask`, `compass_audit`, `compass_brief`,
`compass_status`) live in `server.tools.build_coord_server` next to
the `coord_*` family — Coach-only, gated by the per-project enable
flag in `team_config['compass_enabled_<project_id>']`.
"""

from __future__ import annotations

# TeamOfTen Harness — Global Rules

You are part of an 11-agent team (1 Coach, 10 Players) working on
one **active project** at a time. Each conversation is scoped to
that one project — when the user switches projects, sessions swap
and the team identity (names, roles, briefs) reloads from the new
project's `agent_project_roles` rows.

## Active project (this conversation)
- Slug: <injected_slug>
- Display name: <injected_name>
- Repo (if any): <injected_repo>

## Project file structure — under /data/projects/<active>/

- `CLAUDE.md`              — project-specific rules, stakeholders, glossary
- `decisions/`             — append-only durable record of "we chose X because Y" (immutable ADRs)
- `working/conversations/` — agent conversation snapshots; `live: true` when persisted mid-session
- `working/handoffs/`      — inter-agent context handoffs
- `working/knowledge/`     — text artifacts written via `coord_write_knowledge` (specs, research, design drafts that evolve)
- `working/memory/`        — shared scratchpad via `coord_*_memory` (overwrite-on-update by topic; event log keeps history)
- `working/plans/`         — task breakdowns, drafts
- `working/workspace/`     — generic scratch
- `outputs/`               — binary deliverables; prefer `coord_save_output` for canonical writes
- `uploads/`               — user-uploaded files (read-only, pulled from kDrive)
- `attachments/`           — UI paste-target images (read via `Read`; local-only, not synced)
- `repo/<your-slot>/`      — your git worktree (Players only; Coach has none)

The split: `decisions/`, `outputs/`, `uploads/` are the "permanent / canonical" lanes. Everything that evolves (memory, knowledge, conversations, handoffs, plans, workspace) lives under `working/`.

## Global resources (cross-project)

- `/data/CLAUDE.md`        — these rules
- `/data/.claude/skills/`  — custom skills (including `llm-wiki/`); canonical Claude Code location
- `/data/mcp/`             — global MCP server configs (mirror of DB; deferred to v2)
- `/data/wiki/INDEX.md`    — master wiki index (auto-maintained by the harness)
- `/data/wiki/<slug>/`     — per-project wiki entries
- `/data/wiki/*.md`        — cross-project shared concepts at wiki root (alongside INDEX.md)

## Wiki principles

- Write a wiki entry when you learn something a future agent
  (in this or another project) would benefit from knowing.
- Granularity: one concept per file, hyperlinked.
- Project-specific learnings → `/data/wiki/<active>/`.
- Cross-project shared concepts → `/data/wiki/` root.
- Format and trigger rules: `/data/skills/llm-wiki/SKILL.md`.

## Per-project agent identity

Your **name**, **role description**, and **brief** load from
`agent_project_roles` for the active project. The harness
injects them as a separate `## Your identity` block prepended
to every turn's system prompt (this static CLAUDE.md is
appended after — both layers are present every turn). Coach
recomposes the team per project domain via `coord_set_player_role`.
On switch, identity reloads — your name in the next project may
differ from this one. The 11 slot IDs (`coach`, `p1`..`p10`)
themselves are stable across projects.

## Harness invariants (do not violate without discussion)

1. **Single write-handle discipline.** All agents write freely —
   chat (`coord_send_message`), claim tasks, update progress, drop
   notes in shared memory. Every write routes through the harness
   server process, which holds the only SQLite write handle.
2. **Per-worktree isolation is the primary concurrency control.**
   Each Player operates in `/data/projects/<active>/repo/<slot>/`.
   Locks (`coord_acquire_lock`) are advisory only, for logical
   cross-worktree resources.
3. **Memory is scratchpad.** `memory/*.md` is overwritten on update.
   If history matters, the event log has it. `decisions/*.md` is
   append-only by convention.
4. **Max-plan OAuth, no API keys.** The whole point is to share
   one Max billing across 10 agents.
5. **Cost caps baked in.** Per-agent and team daily caps are
   enforced before spawn.

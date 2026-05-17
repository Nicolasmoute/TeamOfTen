---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 8: Filesystem Layout'
section: 8
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 8. Filesystem Layout

Data root:

```text
HARNESS_DATA_ROOT=/data
```

### 8.1 Global Tree

Canonical paths from `server.paths.global_paths()`:

```text
/data/
  CLAUDE.md
  .claude/
    skills/
      llm-wiki/
        SKILL.md
  mcp/
  wiki/
    INDEX.md
    <cross-project-entry>.md
    <project_slug>/
      <entry>.md
  harness.db
  claude/
```

Global scaffold bootstraps:

- `/data/wiki/`
- `/data/wiki/INDEX.md`
- `/data/.claude/skills/llm-wiki/SKILL.md`
- `/data/CLAUDE.md`

The LLM-Wiki skill is copied from `server/templates/llm_wiki_skill.md`.
The global CLAUDE.md is copied from `server/templates/global_claude_md.md`.
Both are first-write-only.

`/data/wiki/INDEX.md` is auto-rebuilt on wiki writes and on boot by the
`wiki_watcher` background task (`server/wiki_watcher.py`). Agents should
not edit it directly.

### 8.2 Project Tree

Canonical paths from `server.paths.project_paths(project_id)`:

```text
/data/projects/<slug>/
  CLAUDE.md
  decisions/
  truth/
  working/
    conversations/
    handoffs/
    knowledge/
    memory/
    plans/
    workspace/
  outputs/
  uploads/
  attachments/
  repo/
    .project/
    p1/
    p2/
    ...
```

**`truth/` — user-validated source of truth.** Stores files the user
has signed off on as canonical (specs, brand guidelines, contracts,
hard invariants). Distinct from `decisions/` (immutable agent-written
ADRs) and `knowledge/` (agent-written research that evolves).

**Direct agent writes are blocked.** A `PreToolUse` hook in
`server/agents.py` (`_pretool_file_guard_hook`) hard-denies any
agent `Write` / `Edit` / `MultiEdit` / `NotebookEdit` whose path
resolves under any project's `truth/`, plus any `Bash` command
containing `truth/` as a path component. The same hook also blocks
writes to each project's top-level `CLAUDE.md` at
`/data/projects/<slug>/CLAUDE.md` (a separate protected category —
see §8.3a). There is **no** allow-list or override flag — the deny
is unconditional for every agent (Players AND Coach), every tool,
every project.

**Proposal flow** (the only path through which `truth/` ever
changes; the same flow also covers project CLAUDE.md edits — see
§8.3a). The unified MCP tool is `coord_propose_file_write(scope,
path, content, summary)` with `scope='truth'` selecting this lane:

1. Coach calls `coord_propose_file_write(scope='truth', path,
   content, summary)`. Players cannot — the tool body rejects any
   non-Coach caller. The tool inserts a row in
   `file_write_proposals` (`status='pending'`, `scope='truth'`) with
   the full proposed content and a one-line summary; it does NOT
   touch the file. The `path` argument is a relative path *within
   the currently active project's truth/ folder* — it is NOT a path
   anywhere under `/data/projects/`. The harness rejects paths
   starting with `projects/` or with a known project slug as the
   first segment, with an error message that tells Coach to switch
   active project first. This catches the recurrent mistake of
   encoding a sibling project slug in the path when truth/ is
   per-active-project by design.
2. **Auto-supersede**: before insert, the tool scans for any pending
   row on the same `(project_id, scope, path)` and marks each as
   `status='superseded'`, `resolved_by='system'`,
   `resolved_note='superseded by #<new_id>'`. One
   `file_write_proposal_superseded` event fires per superseded row.
   Invariant: at most one pending proposal per (project, scope,
   path) at any time. The scope filter prevents a hypothetical
   `truth/CLAUDE.md` and a `project_claude_md/CLAUDE.md` proposal
   from supersede-colliding. Coach's tool description explicitly
   tells Coach the new proposal REPLACES the old (full content
   replace, not a merge), so Coach must include any prior pending
   content it still wants. Both updates run in the same DB
   transaction with the new INSERT so a crash mid-flight leaves the
   table coherent.
3. The harness emits a `file_write_proposal_created` event (payload
   carries `scope`); the `EnvFileWriteProposalsSection` of the
   Environment pane shows the pending proposal with a scope badge,
   summary, and a side-by-side diff between the current file
   content and the proposed content (fetched lazily on expand from
   `GET /api/file-write-proposals/{id}/diff`). New files (no
   `before` content) fall back to a plain proposed-content render.
4. The user clicks **approve**, **deny/drop**, **request changes**, or
   **comment to Coach**. Approve calls
   `POST /api/file-write-proposals/{id}/approve` which (a) writes
   the proposed content to `truth/<path>` directly (the truth-scope
   resolver uses its own write — broader extension allowlist +
   512,000-char cap — not the Files-pane write_text endpoint), then (b)
   marks the row `approved` with timestamp + `resolved_by =
   "human"` + actor metadata. Deny/drop requires a human note and
   marks the row denied. Request-changes is represented as denial
   with a prefixed note because the backend has no separate
   request-changes status. Comment to Coach sends a human message and
   leaves the proposal pending.
5. Approve emits `file_write_proposal_approved`; deny emits
   `file_write_proposal_denied`. Deny/drop and request-changes also
   send a human message to Coach so the next step is explicit:
   rewrite/resubmit, archive, or ask a clarifying question. The UI
   shows immediate success/failure feedback for every action.

**Seed file (`truth-index.md`).** Every project's `truth/` is seeded
on scaffold with a `truth-index.md` template (from
`server/templates/truth_index.md`) that explains what the lane is
and how the proposal flow works. **No expected-files manifest is
imposed by default** — the seeded file is explanation only, no
bullets. The user / Coach maintains the file's contents per project
(specs, brand guidelines, contracts — whatever fits *this* project).
This was a deliberate course-correction: an earlier iteration seeded
a `specs.md` bullet and rendered a derived "Expected truth files"
section in EnvPane, but seeing it concrete revealed it was making
the harness pick a project type — graphic-design projects want
`brand-guidelines.md`, contract projects want `vendor-agreements.md`,
research projects want `research-questions.md`. The honest default is
no presupposition.

**Boot-time scaffold rescue.** `lifespan` in `server/main.py` runs
`ensure_project_scaffold(id)` for every non-archived project after
`init_db`, so directories or templates added to `_PROJECT_SUBDIRS` /
`_write_truth_index_stub` after a project's creation (e.g. the truth
lane retro-fitted to existing projects) materialize on next boot.
First-write-only — user edits and Coach proposals own each file
once it exists.

**File creation from the UI** — see §16.5 for the generic "+ new
file" button on the Files pane. Works under any writable root, not
just `truth/`. Replaces the dedicated truth-empty-file endpoint and
EnvPane checklist of an earlier iteration.

**EnvPane sections.** `EnvFileWriteProposalsSection` lists pending
proposals with approve, deny/drop, request-changes, and comment-to-Coach
controls. Deny/drop and request-changes require a non-empty note; comment
requires a note and leaves the proposal pending. There is no separate
"Expected truth files" section — the Files pane is the canonical
view of what's actually in `truth/`, and `truth-index.md` (a normal
truth file edited via the proposal flow or the Files-pane editor)
is where the user / Coach records what *should* be there in plain
markdown, no derived UI needed.

Players whose work needs a truth update message Coach (via
`coord_send_message`); Coach decides whether to relay as a proposal.
This keeps the human surface area small — only Coach hits the
approval queue.

The resolver logic lives in `server/truth.py` (FastAPI-free) so the
HTTP wrappers in `server/main.py` can be thin and the test suite
doesn't need to import FastAPI to exercise approve/deny. Resolver
exceptions (`FileWriteProposalNotFound` / `FileWriteProposalConflict` /
`FileWriteProposalBadRequest`) translate 1:1 to 404 / 409 / 400.

The folder is mirrored to kDrive by the regular project sync loop
(sibling of `decisions/`), so spec PDFs dropped into the cloud drive
surface in the Files pane on the next pull. There is no git tracking
on `truth/` — kDrive's own file versioning + the `file_write_proposals`
table (every approve/deny is a permanent row with timestamps,
proposer, resolver, optional note) are the audit trail.

Actual current caveats:

- Knowledge writes use `working/knowledge/`.
- Memory has **no on-disk file** by design (resolved 2026-05-13). The
  store is the SQLite `memory_docs` table; the WebDAV mirror at
  `projects/<id>/memory/<topic>.md` is for human readability only.
  `working/memory/` is intentionally absent from the project scaffold
  (`_PROJECT_SUBDIRS` in `server/paths.py`). Agents access exclusively
  via `coord_update_memory` / `coord_read_memory` / `coord_list_memory`.
- Outputs module still defaults to global `/data/outputs`, not
  `/data/projects/<slug>/outputs`.

### 8.3 Project CLAUDE.md Stub

Created on project creation if missing — first-write-only, so existing
projects' CLAUDE.md files are not overwritten when this template
changes (see also "Migration: existing projects" further down):

```markdown
# Project: <name>

## Project objectives
<pointer paragraph: the project's goals / scope live in the separate
file `/data/projects/<slug>/project-objectives.md`, kDrive-mirrored,
edited via the EnvPane Objectives section or Coach's
`coord_set_project_objectives` tool; the harness injects that file
into Coach's system prompt every turn>

## Repo
<repo_url or placeholder>

## Stakeholders
<filled in by Coach>

## Team
<filled in by Coach>

## Glossary
<filled in by Coach>

## Conventions
<project-specific rules>

## truth/
<reminder of the truth/ lane: read-only for agents, proposal flow via
`coord_propose_file_write(scope='truth', ...)`, seeded
`truth-index.md` (freeform, no enforced manifest structure), slug
interpolated into the absolute path>

## Updating this CLAUDE.md
<reminder that this file is also read-only for agents, and Coach
proposes changes via
`coord_propose_file_write(scope='project_claude_md', path='CLAUDE.md',
content, summary)`; the harness-wide /data/CLAUDE.md is not
proposeable>
```

The trailing `## truth/` and `## Updating this CLAUDE.md` sections are
fixed paragraphs in the canonical template at
`server/templates/app_dev_claude_md.md` (read via
`server.project_claude_md.canonical_project_claude_md_template`) that
interpolate the project's slug and explain both proposal scopes.
Coach in fresh projects reads this on every turn via
`build_system_prompt_suffix`. Existing projects pick up template
changes through the Coach-driven reconciliation flow at
`server.project_claude_md.update_claude_md_via_coach`.

**No `## Goal` section.** Earlier revisions of this template included
a `## Goal\n<description>` section pre-filled from the creation-modal
description. That was dropped (2026-05-02) because the same goal text
was already injected into Coach's system prompt as the
`## Project objectives` section read from
`/data/projects/<slug>/project-objectives.md` (per
[recurrence-specs.md](recurrence-specs.md) §3.3 and §6.1) — and the
coordination block also rendered a `Goal:` line from
`projects.description`. Three stale-prone copies of the same content
drifted apart whenever the operator updated the objectives file but
not the modal description (or vice-versa). The template now carries a
**pointer paragraph** to `project-objectives.md` so Coach knows where
to read / update goals; the file itself is the single canonical
surface for goal content. `projects.description` (the modal one-liner)
remains in the DB as a UI-only field for the project pane title and
project list tagline.

**Migration: existing projects.** Because the stub is first-write-only,
projects created before this template change still have CLAUDE.md
files without the new sections. To add them, Coach calls
`coord_propose_file_write(scope='project_claude_md', path='CLAUDE.md',
content=<full updated body>, summary=<one-line why>)`; the user
reviews the diff and approves in the EnvPane "File-write proposals"
section. The harness then writes the file. (Earlier iterations of
this spec assumed Coach could `Write` to the project CLAUDE.md
directly, which was never actually true: Coach has no Write tool, and
since the file-guard hook now also covers `<slug>/CLAUDE.md`, the
proposal flow is the only path in.)

### 8.3a Project CLAUDE.md Proposal Lane

The same proposal flow that protects `truth/` (see §8.3 above) also
covers the per-project instruction file at
`/data/projects/<slug>/CLAUDE.md`. The unified MCP tool
`coord_propose_file_write(scope, path, content, summary)` selects
the lane via `scope`; for project CLAUDE.md edits Coach passes
`scope='project_claude_md'` and `path='CLAUDE.md'` (the only legal
path for this scope — the resolver re-validates and refuses to
write anywhere else if a row is tampered with).

The `_pretool_file_guard_hook` in `server/agents.py` denies any
direct `Write` / `Edit` / `MultiEdit` / `NotebookEdit` whose path
resolves to `<projects-root>/<slug>/CLAUDE.md` (matching exactly two
parts under projects/ — so a Player's worktree-internal repo
CLAUDE.md at `<slug>/repo/<slot>/CLAUDE.md` is **not** caught and
remains writable). It also denies any `Bash` command containing
`projects/<slug>/CLAUDE.md` as a substring. The deny reason names
the right tool call so Coach learns the proposal flow on first
attempt.

The diff endpoint (`GET /api/file-write-proposals/{id}/diff`) reads
the current CLAUDE.md fresh from disk on every request, so a manual
edit (Files pane, kDrive sync) between propose and approve is
visible to the human reviewer rather than baked into a stale
`before` snapshot.

The harness-wide `/data/CLAUDE.md` is **not** a valid scope. Only
the user edits that file (via the Files pane); there is no agent
path to it.

### 8.4 File Browser Roots

The UI exposes two roots:

| Root id | Path | Scope | Writable |
| --- | --- | --- | --- |
| `global` | `/data` | global | yes, text only |
| `project` | `/data/projects/<active>` | active project | yes, text only |

`/api/files/roots` returns `id`, `key`, `label`, `path`, `scope`,
`project_id`, `writable`, and `exists`.

Read rules:

- Only whitelisted roots.
- Path traversal rejected with `Path.resolve()` and `relative_to()`.
- Symlinks skipped in tree walking.
- Inline reads capped at 256 KB.
- UTF-8 decode with replacement.

Write rules:

- Editable extensions defined in `server.files.EDITABLE_EXTS` —
  text + common code/config formats (mirrors the FilesPane's
  `FILES_TEXT_EXTENSIONS`). Plus an extensionless basename allowlist
  for `Dockerfile`, `Makefile`, `README`, etc.
- Max body 100,000 chars.
- Plain disk write. Empty body is accepted (used by the Files-pane
  "+ new file" button to create a stub).
- WebDAV mirroring happens later through project/global sync loops.
- `file_written` event emitted by API.
- Wiki writes trigger `update_wiki_index()` unless writing `INDEX.md` itself.
  Triggers fire on two paths: (1) the HTTP file-write endpoint above
  (UI Files-pane writes), (2) project creation in `projects_api.py`.
  Additionally, the **Wiki INDEX.md watcher** (`server/wiki_watcher.py`)
  is a background task that polls `/data/wiki/` every
  `HARNESS_WIKI_WATCHER_INTERVAL` seconds (default 30) and rebuilds
  `INDEX.md` whenever any `.md` file in the tree is newer than the current
  index. This makes INDEX.md maintenance **runtime-independent**: it fires
  regardless of whether the write came from a Claude `Write` tool, a Codex
  `apply_patch`, a Bash command, or a kDrive sync — none of which route
  through the HTTP endpoint. The `PostToolUse` ClaudeRuntime hook that
  previously handled Claude-side agent writes has been removed; the watcher
  is the single source of truth. Kill-switch: `HARNESS_WIKI_WATCHER_ENABLED=false`.
  `POST /api/wiki/reindex` remains the "force rebuild now" escape hatch for
  external writers (cloud sync from another machine, snapshot restore,
  manual `cp` into the tree).

The `global` tree hides noisy/sensitive top-level entries:

- `projects`
- `claude`
- `attachments`
- `harness.db`
- SQLite sidecars

---

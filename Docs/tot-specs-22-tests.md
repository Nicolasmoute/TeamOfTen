---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 22: Tests'
section: 22
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 22. Tests

The full suite is 649/649 green (Python 3.12, pytest-asyncio).

Test areas include:

- DB init and schema (incl. the legacy `truth_proposals` →
  `file_write_proposals` rename migration).
- Task state machine.
- Event bus and batched persistence behavior.
- Turn ledger.
- Agent helper functions.
- Auto-naming.
- Concurrent spawn guard.
- Crash recovery.
- Retention.
- Files backend.
- Knowledge backend.
- MCP config.
- Telegram.
- Projects API.
- Project isolation.
- Project sync.
- Phase 7 project prompt/wiki behavior, including:
  - Truth + project-CLAUDE.md PreToolUse hook coverage
    (`_pretool_file_guard_hook`).
  - `coord_propose_file_write` scope validation (truth scope path
    rules, project_claude_md exact-path enforcement, unknown-scope
    rejection, scope-isolated supersede).
  - Resolver scope dispatch (`resolve_file_write_proposal` writes
    truth files OR project CLAUDE.md based on scope; tampered-path
    and unknown-scope defenses).
  - `resolve_target_path` export verification (the `/diff` endpoint
    in `main.py` depends on it).

Run locally:

```bash
uv sync --extra dev
uv run pytest -ra --strict-markers
```

CI runs `.github/workflows/tests.yml` on push and PR.

The frontend has no JS unit tests yet; markdown-pipeline behaviour
(math rendering, mermaid lazy-load, file-link hook routing) is
verified by hand in the browser. JS files are syntax-checked with
`node --check` before commit; logic checks rely on the user
exercising the relevant pane after a deploy.

---

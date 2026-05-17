---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 21: Retention and Cleanup'
section: 21
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 21. Retention and Cleanup

Events:

- `trim_events_once()` deletes SQLite events older than
  `HARNESS_EVENTS_RETENTION_DAYS`.
- 0 disables.
- Loop interval `HARNESS_EVENTS_TRIM_INTERVAL`.

Attachments:

- `trim_attachments_once()` deletes old files.
- Uses override `HARNESS_ATTACHMENTS_DIR` if set.
- Otherwise scans each project under `/data/projects/<slug>/attachments`.
- 0 disables.

Claude sessions:

- `trim_sessions_once()` trims JSONL session files under
  `CLAUDE_CONFIG_DIR/projects/`.
- Window `HARNESS_SESSION_RETENTION_DAYS`.
- 0 disables.

Snapshots:

- WebDAV snapshot retention prunes old `snapshots/*.db` beyond configured
  count.

---

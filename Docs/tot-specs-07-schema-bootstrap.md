---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 7: Schema Bootstrap'
section: 7
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 7. Schema Bootstrap

`init_db()` is the only schema setup. It runs `executescript(SCHEMA)`
(idempotent — every statement is `CREATE TABLE IF NOT EXISTS` /
`CREATE INDEX IF NOT EXISTS`), then seeds the `misc` project, the
11 agents, and Coach's misc-project identity row. All inserts use
`INSERT OR IGNORE` so a re-run never overwrites user state — in
particular `active_project_id` is only set when the row is missing.

There are no version-stamped migrations. Schema changes go directly
into `SCHEMA` in `server/db.py`; existing deploys pick up new tables
and indexes via `IF NOT EXISTS`. New columns on existing tables
require an explicit upgrade (an `ALTER TABLE` run against the
deployed DB), since `IF NOT EXISTS` does not reach inside an
already-created table.

---

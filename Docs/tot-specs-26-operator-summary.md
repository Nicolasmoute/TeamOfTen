---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 26: Operator Summary'
section: 26
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 26. Operator Summary

For a normal deployment:

1. Build/run the Dockerfile with a persistent `/data` volume.
2. Set `HARNESS_TOKEN` before exposing the app publicly.
3. Set `CLAUDE_CONFIG_DIR=/data/claude`.
4. Authenticate Claude through `/api/auth/claude` paste flow or shell
   `claude /login`.
5. Optionally configure WebDAV with `HARNESS_WEBDAV_*`.
6. Create projects from the left rail or Options drawer.
7. Configure repo URLs either in a project card or legacy Project repo section,
   remembering worktree isolation is still global `/workspaces`.
8. Use Coach as the main entry point; intervene in any pane when needed.
9. Use Files pane to edit the harness-wide `/data/CLAUDE.md`, wiki entries,
   knowledge, and project working files. Project CLAUDE.md and `truth/*`
   are agent-read-only — Coach proposes via `coord_propose_file_write`
   and you review/approve in the EnvPane "File-write proposals" section.
10. Watch health, context, spend, pending interactions, and kDrive failures in
    Settings/Env panes.

End of spec.

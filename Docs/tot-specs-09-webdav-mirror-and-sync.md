---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 9: WebDAV Mirror and Sync'
section: 9
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 9. WebDAV Mirror and Sync

WebDAV config:

```text
HARNESS_WEBDAV_URL
HARNESS_WEBDAV_USER
HARNESS_WEBDAV_PASSWORD
```

All three must be set or WebDAV is disabled. The URL should point directly at
the folder the harness owns, for example a `TOT` folder. Files are written
relative to that URL. No extra root prefix setting exists.

The WebDAV client:

- Normalizes the base URL with a trailing slash.
- Supports text and bytes upload/download.
- Creates parent directories recursively.
- Supports atomic byte writes via temp file plus MOVE, with fallback PUT.
- Returns false/none on failures instead of throwing into tool calls.
- Provides `probe()` for `/api/health`.

### 9.1 DB Snapshots

`server/sync.py` still owns database snapshots:

- Interval: `HARNESS_WEBDAV_SNAPSHOT_INTERVAL`, default 300 seconds.
- Retention: `HARNESS_WEBDAV_SNAPSHOT_RETENTION`, default 144.
- Uses SQLite `VACUUM INTO` into bytes, then writes to WebDAV.
- Snapshot path: `snapshots/<timestamp>.db`.

### 9.1a Top-Level Uploads Pull

`server/sync.py` also keeps the human-drop uploads lane live:

- Interval: `HARNESS_UPLOADS_PULL_INTERVAL`, default 60 seconds.
- Remote path: `uploads/<filename>` at the WebDAV root, e.g.
  `TOT/uploads/<filename>` on kDrive.
- Local path: `HARNESS_UPLOADS_DIR`, default `/data/uploads`.
- The app ensures the remote `uploads/` directory exists before polling.
- The pull is inbound only: deleting a file remotely removes the local copy;
  new remote files are downloaded by basename.

### 9.2 Active Project Sync

`server/project_sync.py` active-project loop:

- Interval: `HARNESS_PROJECT_SYNC_INTERVAL`, default 300 seconds.
- Resolves current active project each cycle.
- Pushes `/data/projects/<slug>/` excluding top-level `repo/` and
  `attachments/`.
- Pushes `/data/wiki/<slug>/`.
- Tracks mtime, size, sha256 in `sync_state`.
- Detects local deletions and deletes remote files.
- Retries per file with exponential backoff.
- Emits `kdrive_sync_failed` on retry exhaustion.

Remote mapping:

```text
project tree -> projects/<slug>/<relative>
wiki tree    -> wiki/<slug>/<relative>
```

### 9.3 Global Sync

Global loop:

- Interval: `HARNESS_GLOBAL_SYNC_INTERVAL`, default 1800 seconds.
- Starts after a 60 second stagger.
- Pushes:
  - `/data/CLAUDE.md` as `CLAUDE.md`
  - `/data/.claude/skills/**` as `skills/**`
  - `/data/mcp/**` as `mcp/**`
  - `/data/wiki/INDEX.md` as `wiki/INDEX.md`
  - root-level `/data/wiki/*.md` as `wiki/*.md`
- Does not push per-project wiki subfolders; those are owned by active-project
  sync.

### 9.4 Pull on Open

`pull_project_tree(project_id)` is used during project activation:

- Pulls `projects/<slug>/` and `wiki/<slug>/`.
- Skips `repo/` and `attachments/`.
- Writes local files atomically.
- Updates `sync_state`.

### 9.5 Push on Close

`force_push_project(project_id)`:

- Tags recent files under `working/conversations/` with `live: true` frontmatter
  if modified within `HARNESS_LIVE_CONVERSATION_S`, default 30 seconds.
- Runs active project push under `HARNESS_KDRIVE_CLOSE_TIMEOUT_S`, default 60s.
- On timeout emits `kdrive_sync_failed` and returns a timed-out result.

---

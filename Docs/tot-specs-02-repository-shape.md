---
schema: tot-spec-section/v1
doc_type: tot-section
title: 'TeamOfTen Spec Section 2: Repository Shape'
section: 2
status: canonical
source_index: truth-index.md
last_audited: 2026-04-26
last_reorganized: 2026-05-17
---
## 2. Repository Shape

Top-level layout:

```text
TeamOfTen/
  README.md
  CLAUDE.md
  Dockerfile
  pyproject.toml
  uv.lock
  .env.example
  mcp-servers.example.json
  Docs/
    truth-index.md
    tot-specs-01-product-vision.md
    ...
    tot-specs-26-operator-summary.md
  server/
    main.py
    agents.py
    tools.py
    db.py
    events.py
    paths.py
    files.py
    project_sync.py
    projects_api.py
    workspaces.py
    webdav.py
    sync.py
    context.py
    knowledge.py
    outputs.py
    interactions.py
    mcp_config.py
    secrets.py
    telegram.py
    static/
      index.html
      app.js
      markdown.js
      style.css
      tools.js
      compass.js
      compass.css
      files.js
      vendor/
    templates/
      global_claude_md.md
      llm_wiki_skill.md
    tests/
  scripts/
  spike/
```

Main implementation responsibilities:

- `server/main.py`: FastAPI app, REST API, WebSocket, lifespan startup and
  background-task orchestration.
- `server/agents.py`: Claude Agent SDK runner, session management, cost caps,
  compacting, autowake, Coach loops, stale-task watchdog.
- `server/tools.py`: in-process MCP coordination server and all `coord_*`
  tools.
- `server/db.py`: SQLite schema, DB helpers, active-project resolution.
- `server/projects_api.py`: project CRUD, switch preview, project activation,
  per-project role view, per-project repo provision endpoint.
- `server/paths.py`: canonical `/data` global/project filesystem layout,
  bootstrap resources, wiki index builder.
- `server/project_sync.py`: active-project and global WebDAV file sync.
- `server/events.py`: in-process event bus plus batched SQLite event writer.
- `server/static/app.js`: no-build Preact SPA.
- `server/static/markdown.js`: single-chokepoint markdown render
  pipeline (marked GFM → KaTeX inline+block math → DOMPurify with
  html+mathMl profiles → mermaid post-render via MutationObserver).
  Every consumer that displays markdown — agent panes, files `.md`
  preview, compass briefings, decisions, wiki entries — routes
  through `renderMarkdown` here, so adding a new renderer (PlantUML,
  GraphViz, alternative math engine) lights it up everywhere with
  no per-consumer changes.

---

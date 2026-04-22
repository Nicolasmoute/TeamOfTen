# Claude Code Harness — Full Specification

> A personal orchestration harness for 1 coordinator + 10 worker Claude Code agents, with shared memory, inter-agent messaging, a multi-pane web UI, and deployment to a single VPS. Max plan auth only — no API keys.

---

## 1. Objectives

### Primary goals

1. **Run 1 coordinator + 10 worker agents in parallel** on a single VPS, all using the Claude Agent SDK authenticated via one Max plan OAuth session.
2. **Full transparency**: every agent's activity is visible in the UI. No hidden orchestration.
3. **Shared state**: all agents read from a common task board, a common memory store, and can message each other directly.
4. **Access anywhere**: tiling multi-pane desktop UI, single-view swipe-navigated mobile UI, both real-time.
5. **Disposable VPS**: nothing permanent on the server. Durable state lives on Infomaniak kDrive via WebDAV.
6. **Easy deployment**: one repo, one service, one `docker compose up`.
7. **`/loop` friendly**: agents can run semi-autonomously with bounded iteration caps, human interjections via inbox.

### Explicit non-goals

- Multi-user / multi-tenant
- API-key billing
- Enterprise compliance features
- Beating Anthropic's Agent Teams — this is specifically more transparent and less automagical

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ VPS (disposable)                                            │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ harness (single Python process, mono-service)         │  │
│  │                                                       │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐   │  │
│  │  │ Web server  │  │ Coordinator │  │ Agent mgr    │   │  │
│  │  │ FastAPI     │  │ (state obj) │  │ (SDK spawner)│   │  │
│  │  │ + websocket │◄─┤             ├─►│              │   │  │
│  │  └─────────────┘  └─────────────┘  └──────────────┘   │  │
│  │         │                │                 │          │  │
│  │         └────────────────┼─────────────────┘          │  │
│  │                          ▼                            │  │
│  │                 ┌──────────────────┐                  │  │
│  │                 │ Storage (WebDAV) │                  │  │
│  │                 │ batched flushes  │                  │  │
│  │                 └──────────────────┘                  │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ~/.claude.json (Max plan OAuth, shared by all SDK calls)   │
│                                                             │
│  Caddy (HTTPS, reverse proxy)                               │
└─────────────────────────────────────────────────────────────┘
               │                                    ▲
               │ HTTPS/WebDAV                       │ HTTPS
               ▼                                    │
    ┌────────────────────┐                ┌─────────┴─────────┐
    │ Infomaniak kDrive  │                │ Your browsers     │
    │  /harness/         │                │  - desktop tiling │
    │    state/          │                │  - mobile swipe   │
    │    events/         │                │  - PWA notifs     │
    │    memory/         │                └───────────────────┘
    │    decisions/      │
    └────────────────────┘
```

### Key decisions

| Decision | Choice | Rationale |
|---|---|---|
| Agent runtime | **Claude Agent SDK (Python)** | Programmatic control, uses Max plan OAuth, native streaming |
| Web stack | **FastAPI + WebSocket** | Single process, shared state, native async, matches SDK |
| Frontend | **React + react-mosaic + Vite** | Tiling on desktop, responsive mobile, known-good libs |
| Storage | **kDrive via WebDAV (direct, `webdav4`)** | Swiss hosting, privacy, clean sync control, no rclone daemon |
| Auth to Claude | **OAuth from `~/.claude.json`** | Shared Max plan billing, no API keys |
| Auth to UI | **Bearer token (Tailscale-preferred)** | Personal use, simple, Tailscale removes public exposure |
| Deploy | **Docker Compose (app + Caddy)** | One command, portable, stateless container |
| Repo layout | **Monorepo, mono-service** | Backend, frontend, prompts, deploy all in one repo |

---

## 3. Repository Layout (monorepo, mono-service)

```
harness/
├── README.md
├── HARNESS_SPEC.md                 # this document
├── .env.example                    # all required env vars
├── .gitignore
├── docker-compose.yml              # app + Caddy
├── Dockerfile                      # single-stage, builds front and back
├── Caddyfile                       # HTTPS, routes /api, /ws, /
├── pyproject.toml                  # Python deps (uv or poetry)
├── package.json                    # workspaces root
│
├── server/                         # Python backend
│   ├── main.py                     # FastAPI app, startup/shutdown
│   ├── config.py                   # env-driven settings
│   ├── coordinator.py              # in-memory shared state
│   ├── agents.py                   # SDK spawn + lifecycle
│   ├── prompts.py                  # system prompt templates
│   ├── tools.py                    # custom `coord_*` tools for agents
│   ├── hooks.py                    # SDK hooks (PreToolUse, TaskCompleted, ...)
│   ├── storage.py                  # WebDAV wrapper
│   ├── sync.py                     # background flush/load
│   ├── api.py                      # REST endpoints
│   ├── websocket.py                # event stream to UI
│   ├── auth.py                     # bearer token middleware
│   ├── models.py                   # pydantic schemas
│   └── tests/
│
├── web/                            # React frontend
│   ├── index.html
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── package.json
│   └── src/
│       ├── main.tsx
│       ├── App.tsx                 # picks desktop or mobile shell
│       ├── shells/
│       │   ├── DesktopShell.tsx    # react-mosaic tiling
│       │   └── MobileShell.tsx     # stack + tab bar + swipe
│       ├── panes/
│       │   ├── AgentPane.tsx       # chat view for one agent
│       │   ├── TimelinePane.tsx    # unified event stream
│       │   ├── TaskBoardPane.tsx
│       │   ├── MemoryPane.tsx
│       │   └── BroadcastPane.tsx
│       ├── state/
│       │   ├── store.ts            # zustand
│       │   └── socket.ts           # shared WS client
│       ├── api/
│       │   └── client.ts           # REST wrapper
│       └── styles/
│
├── prompts/                        # prompt templates (also mounted)
│   ├── coordinator.md
│   ├── worker.md
│   └── loop-preamble.md
│
├── workspaces/                     # git worktrees (gitignored, ephemeral)
│   └── .gitkeep
│
└── scripts/
    ├── bootstrap-vps.sh            # one-shot VPS setup
    ├── copy-claude-auth.sh         # pushes ~/.claude.json to VPS
    └── dev.sh                      # local dev mode
```

**Monorepo rationale**: one `git pull` gets everything; frontend and backend versions stay in sync; one Dockerfile builds the whole thing; prompts live next to the code that uses them.

**Mono-service rationale**: coordinator, agent manager, web server, and websocket all share in-memory state. Splitting them into separate services would add IPC complexity for zero benefit at this scale.

---

## 4. Agent Roster — the Team

11 agents total: 1 **Coach** + 10 **Players**. Sports metaphor is deliberate — it makes the hierarchy and accountability natural.

| ID | Kind | Name | Role | Default model |
|---|---|---|---|---|
| `coach` | Coach | fixed: "Coach" | Team captain — decomposes goals, assigns work, synthesizes progress | Sonnet (optionally Opus) |
| `p1` … `p10` | Players | **assigned by Coach** (e.g. "Alice", "Ravi") | **assigned by Coach** (e.g. "Developer — writes code", "Reviewer — checks correctness") | Sonnet (upgrade per task) |

**Name** and **role** for each Player are Coach-assigned, not hardcoded. When you give the Coach a project, the first thing it does is decide the team composition — what roles are needed, who's on the bench — and stamp each active Player with a name + role description. The slot IDs `p1`–`p10` are just slots; Players come and go within them as projects change.

### Coach responsibilities

- Reads incoming human goals from inbox
- Decides the team composition for the goal: assigns `name` + `role` to each Player it needs to activate
- Breaks goals into tasks, writes them to the task board
- **Gives orders** — sends directed tasks/messages to Players
- Monitors progress, unblocks, reroutes on failure
- Writes daily/weekly digests
- Never writes code directly — delegates to Players
- Runs on a cheap loop (every 60s + on event), not continuously

### Player responsibilities

- Reads inbox for Coach orders and peer messages
- Claims open tasks from the board (or executes the ones assigned directly)
- Does the work in its own git worktree under `workspaces/pN/`
- Writes progress events, commits, pushes branches, opens PRs
- **Informs but never orders** — messages Coach and peers to coordinate, shares findings via shared memory, but doesn't assign work to other Players
- Reports back ("done", "blocked", "need X") via `coord_update_task` / `coord_send_message`

### The rule: Coach orders, Players report

This is a **soft rule enforced by prompts** plus a **hard rule enforced structurally**:

- **Soft (prompts)**: Coach's system prompt says "you delegate, never implement"; Player's prompt says "you execute and report, you do not assign work to peers."
- **Hard (structure)**: top-level tasks can only be created by Coach. A Player calling `coord_create_task` gets its task auto-nested as a subtask of the Player's own parent task (i.e. a Player can break its own work down, but cannot create work for others). A Player cannot set `owner` of any task to another Player.

Players may freely message each other for information ("done with migration", "I think the bug is in X, check?"), but the act of assigning work is reserved for Coach.

### Why 10 Players specifically

Max plan realistic concurrency is lower than 10 for continuous loops. The roster defines *potential* slots, not *always-on* agents. Coach activates the slots it needs — typically 2–5 concurrent during active work. The harness supports up to 10 spawned simultaneously; beyond that, SDK call overhead and Max plan caps push back.

---

## 5. Data Model

All stored as JSON (state) or Markdown (human-readable notes) on kDrive. In-memory representations are pydantic models.

### Agent

```python
class Agent(BaseModel):
    id: str                          # "coach", "p1", ... "p10" — fixed slot
    kind: Literal["coach", "player"]
    name: str | None                 # Coach-assigned display name (e.g. "Alice"); "Coach" is fixed for the coach slot
    role: str | None                 # Coach-assigned role description (e.g. "Developer — writes code for the project")
    status: Literal["stopped", "idle", "working", "waiting", "error"]
    current_task_id: str | None
    model: str                       # "claude-sonnet-4-6" etc.
    workspace_path: str              # "/workspaces/p1"
    system_prompt_path: str          # "prompts/player.md"
    loop_config: LoopConfig | None   # if set, agent runs on loop
    started_at: datetime | None
    last_heartbeat: datetime | None
    session_id: str | None           # SDK session for resumption
    cost_estimate_usd: float         # cumulative
```

### Task

```python
class Task(BaseModel):
    id: str                          # "t-2026-04-22-001"
    title: str
    description: str                 # markdown
    status: Literal["open", "claimed", "in_progress", "blocked", "done", "cancelled"]
    owner: str | None                # agent id
    created_by: str                  # "human" or agent id
    created_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None
    depends_on: list[str]            # other task ids
    blocks: list[str]                # task ids blocked by this
    artifacts: list[str]             # paths, URLs, commit SHAs
    tags: list[str]                  # free-form
    priority: Literal["low", "normal", "high", "urgent"]
    estimated_turns: int | None
    actual_turns: int | None
```

### Message

```python
class Message(BaseModel):
    id: str                          # ulid
    from_id: str                     # "human", "coord", "w3"
    to_id: str                       # target agent id or "broadcast"
    subject: str | None
    body: str                        # markdown
    sent_at: datetime
    read_at: datetime | None
    in_reply_to: str | None          # message id
    priority: Literal["normal", "interrupt"]  # interrupt = block next tool
```

### Event

```python
class Event(BaseModel):
    id: str                          # ulid
    ts: datetime
    agent_id: str                    # who emitted
    type: Literal[
        "agent_started", "agent_stopped", "heartbeat",
        "tool_use", "tool_result",
        "task_claimed", "task_progress", "task_completed", "task_blocked",
        "message_sent", "message_received",
        "memory_updated", "lock_acquired", "lock_released",
        "human_interjection", "error", "cost_update"
    ]
    payload: dict                    # type-specific, free-form
    task_id: str | None
```

Events are append-only, the source of truth for "what happened."

### MemoryDoc

```python
class MemoryDoc(BaseModel):
    topic: str                       # filename: auth-system, db-schema
    content: str                     # markdown
    last_updated: datetime
    last_updated_by: str             # agent id
    version: int                     # incremented on each write
    referenced_by: list[str]         # task ids
```

### Lock

```python
class Lock(BaseModel):
    resource: str                    # file path or logical resource name
    holder: str                      # agent id
    acquired_at: datetime
    expires_at: datetime             # auto-release after N minutes
    purpose: str
```

---

## 6. Storage Layout

### Two-tier storage

The harness keeps **hot state in a local SQLite file** and **durable/human-readable content on kDrive**. WebDAV is too slow and race-prone to be the source of truth for `tasks.json` under 11 concurrent writers.

- **Local SQLite (`/var/lib/harness/harness.db`, mounted volume)** — single source of truth for `agents`, `tasks`, `messages`, `events`, `locks`. In-process, ACID, no race conditions.
- **kDrive** — periodic snapshots of state (for crash recovery onto a fresh VPS), plus all the `.md` content (memory, decisions, digests) which is genuinely human-readable and the reason kDrive is in the design at all.
- **Single writer discipline** — only the coordinator process flushes state to kDrive. Workers never touch kDrive directly; they emit events and the coordinator folds them in.

### Layout on kDrive

```
/harness/
├── state/
│   ├── agents.json                 # all 11 agents, current status
│   ├── tasks.json                  # full task board
│   ├── locks.json                  # active locks
│   └── inbox/
│       ├── coord.json              # pending messages for coord
│       ├── w1.json
│       └── ...
├── events/
│   ├── 2026-04-22.jsonl            # today, append-only
│   ├── 2026-04-21.jsonl            # yesterday
│   └── ...                         # rotated daily
├── memory/                         # scratchpad; overwritten on each write
│   ├── auth-system.md
│   ├── db-schema.md
│   └── ...                         # agents create and update these
├── decisions/
│   └── 2026-04-22-use-redis.md     # architectural decisions, dated
├── digests/
│   ├── daily-2026-04-22.md
│   └── weekly-2026-W16.md
└── snapshots/                      # hourly state snapshots, last 24h kept
    ├── 2026-04-22T14-00-00.tar.gz
    └── ...
```

**Format discipline:**
- `.json` for machine-consumed state (UTF-8, 2-space indent for human readability)
- `.jsonl` for event logs (one JSON object per line, append-friendly)
- `.md` for human-readable content (memory, decisions, digests) — these are what you open on your phone
- **`memory/` is a scratchpad**, overwritten on update. If "what did this say yesterday" ever matters, the event log already records `memory_updated` events; query those, don't VCS the directory.
- **`decisions/` is append-only by convention** — dated filenames, never mutated. This is where durable "we chose X because Y" content lives.

---

## 7. Coordination Mechanics

### Write model: commons with a single write handle

Agents collaborate on a shared commons. Every agent (workers AND the coordinator) writes freely — they chat, claim tasks, update progress, create subtasks, drop notes in `memory/`. The design encourages frequent writes.

What makes it sane under 11 concurrent writers: **all writes route through the harness server process**, which holds the only SQLite write handle and serializes incoming `coord_*` tool calls.

- Agents call `coord_*` MCP tools (registered in-process with each SDK query)
- The tool handler runs inside the harness server, writes to SQLite, publishes an event
- No agent opens its own DB connection or mutates `state/*` files directly
- Periodic snapshots from SQLite → kDrive for durability

This gives the feel of a shared commons (any agent can write anything) with the safety of one-process serialization (clean event ordering, trivial audit, no file-lock contention).

### Locks: fallback, not primary

File-level isolation is primarily provided by **per-agent git worktrees** (`workspaces/w1/`, `workspaces/w2/`, …). Two workers editing the same file happens in isolated trees; conflict surfaces at merge time, which is a much cleaner failure mode than a lock held by a crashed agent.

`coord_acquire_lock` / `coord_release_lock` remain available for logical resources that span worktrees (e.g. "only one worker runs the migration at a time"), but are **not** the primary concurrency control. Treat them as advisory.

### Custom tools exposed to agents (via SDK)

All defined in `server/tools.py`, registered as in-process MCP tools on each SDK query:

| Tool | Purpose |
|---|---|
| `coord_list_tasks(status?)` | See the task board, optionally filtered |
| `coord_claim_task(task_id)` | Claim an open task; fails if already claimed |
| `coord_update_task(task_id, status, note?)` | Report progress, mark blocked/done |
| `coord_create_task(title, description, depends_on?)` | Workers can propose subtasks |
| `coord_send_message(to, subject, body, priority?)` | Message another agent |
| `coord_read_inbox()` | Pull pending messages (auto-called by hook, but available) |
| `coord_update_memory(topic, content)` | Write/overwrite a memory doc |
| `coord_read_memory(topic)` | Read a memory doc |
| `coord_list_memory()` | List all memory topics |
| `coord_acquire_lock(resource, minutes)` | Acquire named lock |
| `coord_release_lock(resource)` | Release it |
| `coord_heartbeat(status_note?)` | Manually beat |
| `coord_request_human(question)` | Escalate: mark self as `waiting`, ping the UI |

### SDK hooks (defined in `server/hooks.py`)

- **`PreToolUse`** — before every tool call:
  1. Drain inbox; if new messages, inject them as context and let the agent react
  2. Check for pause/stop flags on the agent; if set, gracefully exit loop
  3. Check task status; if current task was externally cancelled, exit

  > **Caveat**: mid-conversation context injection via hooks is the least-proven part of this spec. Prototype it on M1 with one agent before committing the design — if the SDK doesn't cleanly support injecting inbox content into the next turn, fall back to a polling pattern (agent calls `coord_read_inbox` at the start of each turn per the prompt instructions).

- **`PostToolUse`** — after every tool call:
  1. Emit a `tool_use` event to the event log + websocket
  2. Update cost estimate

- **`TaskCompleted`** — when agent emits `coord_update_task(..., status="done")`:
  1. Update task board in memory, flag for durable write
  2. Release locks held by this agent
  3. If agent is on loop, it may pick up a new task; otherwise exits

- **`TeammateIdle`** — when agent finishes a turn with no next action:
  1. If loop enabled and tasks available, claim next
  2. Otherwise set status to `idle` and stop

- **`SessionEnd`** — capture session_id for potential resumption

### Flow example: a bug fix task

1. You type "fix the login 500 error" in the UI broadcast → becomes a message to `coord`
2. `coord` wakes on message, creates tasks: `t-101` (reproduce), `t-102` (diagnose), `t-103` (fix), `t-104` (test)
3. `coord` assigns `t-101` to `w1`
4. `w1` claims `t-101`, spawns in its worktree, runs the app, reproduces the bug
5. `w1` writes findings to `memory/login-500.md` via `coord_update_memory`
6. `w1` marks `t-101` done, triggering `TaskCompleted` hook
7. `coord` sees `t-101` done, dispatches `t-102` to `w2`
8. `w2` reads `memory/login-500.md`, diagnoses, writes more findings
9. Eventually `w3` runs `t-103`, commits to branch `fix/login-500`, messages `coord` when PR opened
10. `coord` writes a daily digest summarizing what happened

At every step, you see it happening in the timeline pane and can interject via any agent's inbox.

---

## 8. Agent Lifecycle

### Starting an agent

```python
async def start_agent(agent_id: str, initial_task: str | None = None):
    agent = coordinator.get_agent(agent_id)
    options = ClaudeAgentOptions(
        system_prompt=load_prompt(agent.system_prompt_path, {"agent_id": agent_id}),
        cwd=agent.workspace_path,
        max_turns=agent.loop_config.max_turns if agent.loop_config else 50,
        allowed_tools=["Read", "Write", "Edit", "Bash", "Grep", "Glob"],
        mcp_servers={"coord": coord_tools_server(agent_id)},
        hooks=build_hooks_for(agent_id),
        resume_session_id=agent.session_id,  # if resuming
    )
    coordinator.mark_agent_started(agent_id)
    async for msg in query(prompt=initial_task or "Check your inbox and tasks.",
                            options=options):
        await event_bus.emit(agent_id, msg)
```

### Stopping / pausing / resuming

- **Pause**: coordinator sets `agent.paused = True`; `PreToolUse` hook exits on next call. Session_id saved for later.
- **Stop**: hard cancel the async iterator, mark `stopped`.
- **Resume**: re-call `start_agent` with the saved session_id; SDK rehydrates the conversation.

### Crash recovery

If the harness process crashes mid-session:
- On restart, it loads `state/agents.json` from kDrive
- For each agent that was in `working` status, it attempts resumption via `session_id`
- If resumption fails, the agent is marked `stopped` and the user is notified
- Tasks that were `in_progress` are reset to `open` so they can be reclaimed (with a note in the event log)

---

## 9. Prompt Templates

### `prompts/coach.md` (excerpt)

```markdown
You are the Coach of a team of up to 10 Players (slots p1–p10).

Your job is to:
- Receive human goals via your inbox
- Decide the team composition: for each Player slot you want active, pick a
  name (e.g. "Alice") and a short role description (e.g. "Developer —
  writes code for the project"). Use coord_set_player_identity(slot, name, role).
- Decompose goals into discrete tasks with clear success criteria
- Assign tasks directly to Players, or leave them open on the board for
  self-claim
- Monitor progress, unblock Players, reroute on failures
- Write daily digests summarizing what was accomplished

You NEVER write code. You delegate. If tempted to solve something directly,
stop and create a task instead.

You are the ONLY agent who gives orders. Players report back to you and
communicate with each other for information, but they cannot assign work.

You have the `coord_*` tools for all coordination. Start every turn by:
1. Reading your inbox
2. Checking the task board for anything stuck
3. Acting on the highest-priority item
```

### `prompts/player.md` (excerpt)

```markdown
You are {name}, a Player on this team. Your role: {role}.
Your slot id is {agent_id}; your workspace is {workspace_path} — a git
worktree on branch `work/{agent_id}`.

Start every task by:
1. Reading your inbox via `coord_read_inbox` (Coach's orders go here)
2. If you have no current task, browsing `coord_list_tasks(status="open")`
   and claiming one
3. Checking `memory/` for relevant prior findings

When you finish:
- `coord_update_task(task_id, "done", note="brief summary")`
- Update or create memory docs for anything future Players should know

You report but do not order. You may message peers for information
("finished the migration, FYI") but you may NOT assign work to another
Player. Only Coach assigns work. If you think new work is needed, either
create a subtask under your current task, or message Coach suggesting it.

If blocked, use `coord_send_message` to ask a specific teammate, or
`coord_request_human` for ambiguous situations.
```

### `prompts/loop-preamble.md`

Prepended when an agent runs on `/loop`. Caps iterations, enforces heartbeats, forces inbox checks, defines graceful exit conditions.

---

## 10. Web UI

### Shared stack
- React 18 + TypeScript + Vite
- Zustand for state
- One websocket connection at app root, events dispatched to subscribers
- PWA manifest for "add to home screen" + push notifications

### Desktop shell (`DesktopShell.tsx`)

- `react-mosaic` tiling layout
- Pane types: AgentPane (×11), TimelinePane, TaskBoardPane, MemoryPane, BroadcastPane
- Drag pane header to split; drag divider to resize; drag out to pop into new window
- Layout persisted to localStorage
- 3 preset layouts: "overview", "focus", "debug"
- Command palette (Cmd+K) for quick actions

### Mobile shell (`MobileShell.tsx`)

- Full-screen single pane
- Bottom tab bar: Agents / Timeline / Tasks / Memory
- Inside Agents tab: horizontal swipe between agents (coord + w1…w10)
- Command palette (swipe down from top) for jumping
- Each pane has its own message input where relevant
- Badge dots on tabs for unread / needs-attention

### AgentPane contents

- Header: agent id, role, status indicator, current task, cost-so-far
- Scrolling chronological view: agent's messages, tool uses, tool results, incoming messages
- Sticky input at the bottom: sends message to that agent's inbox
- Action buttons: pause, resume, stop, assign task, view memory references

### TimelinePane

- Unified stream of all events across all agents
- Filter chips: by agent, by event type, by task
- Click an event → jump to the relevant agent pane
- Virtualized list for performance (events accumulate fast)

### TaskBoardPane

- Columns: Open / Claimed / In Progress / Blocked / Done
- Drag task cards between columns (triggers `coord_update_task` on backend)
- Click task → detail view with history, artifacts, owner
- Create-task button (creates via `coord` if coord is running, else direct API)

### MemoryPane

- List of memory topics on the left
- Markdown viewer on the right with edit toggle
- No version history UI — memory is scratchpad. If you want "who wrote this", the `last_updated_by` field is shown; if you want "what did it say before", filter the timeline for `memory_updated` events on that topic.

### BroadcastPane

- Single textarea + "Send to all" / "Send to coord" / pick-agents dropdown
- Recent broadcasts history below

---

## 11. REST + WebSocket API

### REST endpoints (all under `/api`, bearer token auth)

```
# Agents
GET    /api/agents                           list all
GET    /api/agents/:id                       one
POST   /api/agents/:id/start                 spawn
POST   /api/agents/:id/pause
POST   /api/agents/:id/resume
POST   /api/agents/:id/stop
POST   /api/agents/:id/message               send message from human to agent

# Tasks
GET    /api/tasks                            ?status=&owner=
POST   /api/tasks                            create (usually from human)
PATCH  /api/tasks/:id                        status, owner, priority
DELETE /api/tasks/:id                        cancel

# Messages
GET    /api/messages?agent=:id               inbox view
POST   /api/messages                         send (from human or UI)

# Memory
GET    /api/memory                           list topics
GET    /api/memory/:topic
PUT    /api/memory/:topic                    write (human edits allowed)
GET    /api/memory/:topic/history

# Events (read-only)
GET    /api/events?agent=&type=&since=       paginated, backed by kDrive JSONL

# System
GET    /api/status                           agents alive, last flush, queue depth
POST   /api/broadcast                        send to many agents
GET    /api/config                           server config (non-secret)
```

### WebSocket (`/ws`)

Server-initiated messages:
```json
{"kind": "event", "event": {...}}
{"kind": "agent_status", "agent_id": "w3", "status": "working"}
{"kind": "task_updated", "task": {...}}
{"kind": "message", "message": {...}}
{"kind": "heartbeat", "ts": "..."}
```

Client-initiated:
```json
{"kind": "subscribe", "filters": {"agents": ["w1", "w3"], "types": ["tool_use"]}}
{"kind": "unsubscribe", ...}
```

One connection per browser tab/window; client-side filtering for panes.

---

## 12. Authentication & Security

### To Claude (Max plan)

**Corrected per M-1 spike findings (2026-04-22):** OAuth tokens are **not** stored in `~/.claude.json` — that file holds only local CLI config (startups, install method, etc.). The actual tokens live in the OS credential store (Windows Credential Manager, macOS Keychain, Linux Secret Service) or an internal path the CLI manages. **Copying `~/.claude.json` across hosts does not transfer authentication.**

**The actual mechanism — device-code OAuth flow, run once per host:**

1. On the VPS, start the CLI: `claude` (drops into interactive REPL)
2. At the `>` prompt, type `/login`
3. CLI prints a URL and a short code
4. On your laptop, open the URL in a browser, sign in to your Max account, enter the code, approve
5. VPS confirms "Logged in"; token is now stored locally on the VPS
6. Exit REPL (`/exit`); `claude -p "..."` non-interactive calls work from any shell on that host

**Implications for the harness:**
- The previously-planned `scripts/copy-claude-auth.sh` is obsolete — remove from the plan.
- Whatever path the CLI uses to persist the token must sit on a **mounted volume** so redeploys don't erase auth. The existing `./claude-auth:/root/.claude` mount in docker-compose is still the right shape — but it must hold whatever the CLI actually writes post-`/login`, not a copied file.
- The install script `https://claude.ai/install.sh` is **geo-blocked** in some regions (confirmed HK / Zeabur's default datacenter). Install via npm instead: `npm install -g @anthropic-ai/claude-code`. The API itself (`api.anthropic.com`) is **not** geo-blocked in those same regions — once the CLI is installed and logged in, runtime queries work.
- When OAuth expires, re-run `/login` on the VPS.

### To the UI
- Single bearer token in `HARNESS_TOKEN` env var
- Browser sets `Authorization: Bearer <token>` via stored config
- UI shows a one-time token paste screen on first load, stores in localStorage

### Recommended deployment: Tailscale-only
- VPS joins your tailnet
- Caddy binds only to the tailnet interface
- No public exposure; no login needed (tailnet auth is sufficient)
- `HARNESS_TOKEN` still required for API to prevent inter-tailnet app spoofing

### Public deployment (alternative)
- Caddy gets a real domain, Let's Encrypt cert
- Add fail2ban / basic rate limiting
- Bearer token mandatory
- Audit log of all API calls

### kDrive credentials
- `KDRIVE_USER`, `KDRIVE_APP_PASSWORD`, `KDRIVE_ID` env vars
- App-specific password (not main password), generated from Infomaniak account panel
- Never logged, never surfaced in the UI

---

## 13. Deployment

### One-time VPS setup

```bash
# On a fresh Ubuntu/Debian VPS
curl -fsSL https://yourhost/harness/scripts/bootstrap-vps.sh | bash
```

`bootstrap-vps.sh` does:
1. Installs Docker + docker compose plugin
2. Creates `/opt/harness` dir
3. Clones the repo (or you push it)
4. Prompts for `.env` values
5. Copies `.env.example` → `.env`
6. Runs `docker compose up -d`

### Ongoing

```bash
ssh vps
cd /opt/harness
git pull
docker compose up -d --build         # rebuild and restart
```

### Docker Compose

```yaml
services:
  harness:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./workspaces:/workspaces         # git worktrees (can rebuild)
      - ./claude-auth:/root/.claude      # OAuth tokens
    expose:
      - "8000"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/status"]
      interval: 30s

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
      - caddy_config:/config

volumes:
  caddy_data:
  caddy_config:
```

### Dockerfile (single-stage, builds front + back)

```dockerfile
FROM node:20-slim AS frontend
WORKDIR /app
COPY web/ ./
RUN npm ci && npm run build
# produces web/dist/

FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*
# Install Claude Code CLI (bundled with SDK but ensure fresh)
RUN curl -fsSL https://claude.ai/install.sh | bash
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen
COPY server/ ./server/
COPY prompts/ ./prompts/
COPY --from=frontend /app/dist ./web_static/
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Caddyfile

```
{$HARNESS_DOMAIN} {
    # Static UI
    root * /app/web_static
    file_server
    try_files {path} /index.html

    # API + WebSocket proxy to app
    @api path /api/*
    handle @api {
        reverse_proxy harness:8000
    }
    @ws path /ws
    handle @ws {
        reverse_proxy harness:8000
    }
}
```

### `.env.example`

```bash
# Domain (for Caddy HTTPS)
HARNESS_DOMAIN=harness.example.com

# API auth
HARNESS_TOKEN=generate-a-long-random-string

# kDrive
KDRIVE_WEBDAV_URL=https://connect.drive.infomaniak.com/<your-drive-id>
KDRIVE_USER=you@example.com
KDRIVE_APP_PASSWORD=generated-app-password
KDRIVE_ROOT_PATH=/harness

# Claude (defaults to ~/.claude.json mounted in)
CLAUDE_AUTH_PATH=/root/.claude/auth.json

# Agents
DEFAULT_MODEL=claude-sonnet-4-6
MAX_PARALLEL_AGENTS=5          # soft cap even though 10 defined
FLUSH_INTERVAL_SECONDS=30
HEARTBEAT_INTERVAL_SECONDS=60

# Logging
LOG_LEVEL=INFO
```

### Upgrade path

1. `git pull` on VPS
2. `docker compose build harness`
3. `docker compose up -d harness`  (Caddy stays up)
4. New container reads state from kDrive on startup, resumes in-flight agents

Downtime: ~5 seconds for the state reload.

---

## 14. Build Order (milestones)

| Milestone | Scope | Est. effort |
|---|---|---|
| **M-1 — Feasibility spike** | Single Python script, no repo structure. Spawn 3 parallel `query()` calls from the Claude Agent SDK authenticated via `~/.claude.json` (Max plan). Stream outputs. Register one trivial MCP tool. Goal: confirm Max plan tolerates 3–5 concurrent programmatic sessions without rate-limiting or TOS-level pushback. If this fails, the spec pivots to API keys or a smaller roster. | 2 hours |
| **M0 — Bones** | Repo skeleton, FastAPI hello, Dockerfile, Caddyfile, deploys to VPS with "hello world" | 1 evening |
| **M1 — One agent** | Spawn one SDK agent from the server, stream its output to a minimal HTML page over WebSocket | 1 evening |
| **M2 — Coord tools + cost caps** | Implement `coord_*` tools, in-memory coordinator state (SQLite-backed), tasks + inbox; agent can call them. **Bake in per-agent daily turn/cost caps from the start** — spawning is refused when the cap is hit. 11 Sonnet sessions with 50-turn loops can chew through a weekly Max allowance fast. | 1-2 evenings |
| **M3 — kDrive persistence** | `storage.py` WebDAV wrapper. SQLite remains the hot-path source of truth; kDrive gets periodic snapshots (state) + the `.md` content (memory, decisions, digests). Resumption on restart reads kDrive snapshot only if local SQLite is absent (fresh VPS). | 1 evening |
| **M4 — 11 agents** | Full roster, worktree provisioning, coordinator role, worker role | 1 evening |
| **M5 — Hooks** | PreToolUse inbox check, TaskCompleted, TeammateIdle, pause/resume | 1 evening |
| **M6 — Desktop UI** | react-mosaic layout, AgentPane, TimelinePane, TaskBoardPane, BroadcastPane | 2 evenings |
| **M7 — Mobile UI** | Responsive switch, tab bar, swipe nav, input boxes, PWA manifest | 1 evening |
| **M8 — Memory + decisions** | MemoryPane, editable docs, versions, digest generation by coord | 1 evening |
| **M9 — Polish** | Push notifications, command palette, layout presets, cost dashboard, pop-out windows | 2 evenings |

**Usable v1**: M0–M5 (~5 evenings) — CLI-ish UI, all coordination works
**Full-featured v1**: through M8 (~10 evenings)
**Polished**: +M9 (~12 evenings)

---

## 15. Open Questions / Decisions to Revisit

1. **Opus for coord?** — coordinator does more reasoning, less typing; might be worth Opus. But cost.
2. **Per-agent Max usage limits?** — Should the harness refuse to spawn a new agent if your Max plan is >90% consumed this week?
3. **Agent self-termination on task completion?** — Currently they loop; could also stop after each task and wait for next assignment, reducing idle usage.
4. **Multi-repo support?** — Right now one project, one set of worktrees. Extending to N projects is doable but adds routing complexity.
5. **Record/replay?** — Event log enables replaying a session for debugging. Worth building early, or YAGNI?
6. **Conflict detection** — two agents editing the same file despite locks. Add git pre-commit hook per worktree?
7. **Cost enforcement** — hard cap per agent per day vs. soft warning.

---

## 16. What This Is Not

- Not a product. Personal tool.
- Not secure enough for untrusted users. Single-user by design.
- Not a replacement for Claude Agent Teams — this is specifically more transparent and more manual.
- Not going to solve "how do I get 10 Claudes to build an app with no input from me." Human remains in the loop.

---

*End of spec.*

# Codex Python SDK — Live Spike Output

Captured 2026-04-28 from a Zeabur container running `codex` CLI 0.125.0
with `codex-app-server-sdk` 0.3.2 from PyPI. Authenticated via headless
`codex login --device-auth` (ChatGPT path).

This is the source of truth for §E.2 / §E.3 method names and notification
shapes in `CODEX_RUNTIME_SPEC.md`. Do NOT paraphrase from public docs —
the SDK actually shipped is what's below.

## Module surface

```
module: codex_app_server_sdk
file:   /usr/local/lib/python3.12/site-packages/codex_app_server_sdk/__init__.py
public attrs:
  ApprovalPolicy, ApprovalRequest, CancelResult, ChatContinuation,
  ChatResult, CodexClient, CodexError, CodexProtocolError,
  CodexTimeoutError, CodexTransportError, CodexTurnInactiveError,
  CommandApprovalDecision, CommandApprovalRequest,
  CommandApprovalWithExecpolicyAmendment, ConversationStep,
  FileChangeApprovalDecision, FileChangeApprovalRequest,
  InitializeResult, ReasoningEffort, ReasoningSummary,
  SandboxMode, SandboxPolicy, ThreadConfig, ThreadHandle,
  TurnOverrides, UNSET,
  client, errors, models, protocol, transport (submodules)
```

No `AsyncCodex` — the spec's draft was wrong. Real class is `CodexClient`.

## CodexClient — relevant methods

```
connect_stdio(*, command: Sequence[str] | None = None,
              cwd: str | None = None,
              env: Mapping[str, str] | None = None,
              connect_timeout: float = 30.0,
              request_timeout: float = 30.0,
              inactivity_timeout: float | None = 180.0,
              strict: bool = False) -> CodexClient
                                          (classmethod, spawns `codex app-server`)

connect_websocket(*, url, token, headers, ...) -> CodexClient
start() -> CodexClient                  # call after connect_*
initialize(params=None, *, timeout=None) -> InitializeResult
close() -> None

start_thread(config: ThreadConfig | None = None) -> ThreadHandle
resume_thread(thread_id: str, *,
              overrides: ThreadConfig | None = None) -> ThreadHandle
fork_thread(thread_id, *, overrides=None) -> ThreadHandle
archive_thread(thread_id) -> None
unarchive_thread(thread_id) -> None
read_thread(thread_id, *, include_turns=True) -> Any
list_threads(*, archived=None, cursor=None, cwd=None, limit=None,
             model_providers=None, sort_key=None, sort_direction=None) -> Any
compact_thread(thread_id) -> Any
rollback_thread(thread_id, *, num_turns) -> Any
set_thread_defaults(thread_id, overrides) -> None
set_thread_name(thread_id, name) -> None

chat(text=None, thread_id=None, *,
     user=None, metadata=None,
     thread_config=None, turn_overrides=None,
     inactivity_timeout=None,
     continuation=None) -> AsyncIterator[ConversationStep]
chat_once(...) -> ChatResult                  # same args, non-streaming

cancel(continuation, *, timeout=None) -> CancelResult
interrupt_turn(turn_id, *, timeout=None) -> None
steer_turn(*, thread_id, expected_turn_id, input_items) -> Any

approval_requests() -> AsyncIterator[ApprovalRequest]
set_approval_handler(handler) -> None
approve_approval(request, *, for_session=False, execpolicy_amendment=None) -> None
decline_approval(request) -> None
respond_approval(request, decision) -> None
cancel_approval(request) -> None

exec_command(command, *, cwd=None, sandbox_policy=None, timeout_ms=None) -> Any
list_models(*, cursor=None, include_hidden=None, limit=None) -> Any
read_config(*, cwd=None, include_layers=False) -> Any
read_config_requirements() -> Any
write_config_value(*, key_path, value, merge_strategy='upsert', ...) -> Any
batch_write_config(edits, *, expected_version=None, file_path=None) -> Any
start_review(*, thread_id, target, delivery=None) -> Any
request(method, params=None, *, timeout=None) -> Any
```

## ThreadHandle — methods

```
chat(text=None, *, user=None, metadata=None,
     inactivity_timeout=None, continuation=None,
     turn_overrides=None) -> AsyncIterator[ConversationStep]
chat_once(...) -> ChatResult
compact() -> Any                              # native compact!
fork(*, overrides=None) -> ThreadHandle
read(*, include_turns=True) -> Any
rollback(num_turns) -> Any
archive() -> None
unarchive() -> None
set_name(name) -> None
update_defaults(overrides) -> None
start_review(target, *, delivery=None) -> Any

# attributes:
thread_id: str          # UUIDv7-style, accessible
defaults: ThreadConfig  # the per-thread defaults
```

## InitializeResult sample

```python
InitializeResult(
  protocol_version=None,
  server_info=None,
  capabilities=None,
  raw={
    'userAgent': 'codex-app-server-sdk/0.125.0 (Debian 13.0.0; x86_64) xterm (codex-app-server-sdk; 0.1.0)',
    'codexHome': '/data/codex',
    'platformFamily': 'unix',
    'platformOs': 'linux',
  },
)
```

Note: `protocol_version`, `server_info`, `capabilities` were `None` in this
SDK build — useful info lives in `raw`.

## ConversationStep — observed shape

A minimal turn (`thread.chat("reply with the single word: hi")`) yielded
exactly **two** steps:

### Step 1 — userMessage echo

```python
ConversationStep(
  thread_id='019dd598-af8a-7133-9ce7-d8f085e9d1b3',
  turn_id='019dd598-b01d-7c62-8cb1-428a64ec0339',
  item_id='8e3fd359-d4db-4a27-b14f-62503f59574f',
  step_type='userMessage',
  item_type='userMessage',
  status='completed',
  text=None,
  data={
    'params': {
      'item': {
        'type': 'userMessage',
        'id': '8e3fd359-...',
        'content': [{'type': 'text', 'text': 'reply with the single word: hi', 'text_elements': []}],
      },
      'threadId': '...',
      'turnId': '...',
    },
    'item': {...},
  },
)
```

### Step 2 — agentMessage final answer

```python
ConversationStep(
  thread_id='019dd598-af8a-7133-9ce7-d8f085e9d1b3',
  turn_id='019dd598-b01d-7c62-8cb1-428a64ec0339',
  item_id='msg_064f1e7e9342d8980169f10c7de62c8191bb7a10128af2a781',
  step_type='codex',
  item_type='agentMessage',
  status='completed',
  text='hi',
  data={
    'params': {
      'item': {
        'type': 'agentMessage',
        'id': 'msg_...',
        'text': 'hi',
        'phase': 'final_answer',
        'memoryCitation': None,
      },
      'threadId': '...',
      'turnId': '...',
    },
    'item': {...},
  },
)
```

## What we DID NOT observe (this prompt was too simple)

- Tool-use steps. Likely `item_type` ∈ {`shell`, `apply_patch`, `web_search`}
  per `step_type='codex'` analogous to `agentMessage`. Need a more complex
  prompt (e.g. ask Codex to run `ls`) to capture.
- Reasoning steps. Likely `step_type='reasoning'` or similar.
- Usage / token counts. `chat()` is streaming; `chat_once()` returns
  `ChatResult` which is presumably where `Turn.usage` lives. Need to
  inspect `ChatResult` model fields to confirm.
- `turn.completed` / `turn.failed` notifications. May not be a separate
  step at all — completion may be implicit in the stream's exhaustion.

These gaps are non-blocking: they unblock at implementation time, not
spike time. Capture them with a follow-up probe when wiring item #11.

## Dispatcher → notification mapping (proposed)

Maps `ConversationStep` to harness `_emit` events. Update spec §E.3.

| ConversationStep                        | Harness emit                  |
|-----------------------------------------|-------------------------------|
| `step_type='userMessage'`               | (skip — already in DB)        |
| `step_type='codex', item_type='agentMessage', text=<...>` | `_emit(text=...)` |
| `step_type='codex', item_type='shell'`  | `_emit(tool_use, name=Bash)`  |
| `step_type='codex', item_type='apply_patch'` | `_emit(tool_use, name=Edit)` |
| `step_type='codex', item_type='web_search'` | `_emit(tool_use, name=WebSearch)` |
| `step_type='reasoning'` (TBD)            | `_emit(reasoning, summary=...)` |
| Stream exhaustion + `chat_once` result   | `_emit(result, usage=..., cost_usd=...)` |
| `CodexTurnInactiveError`                 | `_emit(error, ...)` + retry counter |
| `CodexTimeoutError`                      | `_emit(error, ...)` + retry counter |
| `CodexTransportError`                    | `_emit(error, ...)` + retry counter |
| `ApprovalRequest` via approval handler   | `_emit(human_attention, ...)` (or coord_ask_user) |

## Implications for server/runtimes/codex.py

The provisional stub assumed `AsyncCodex().start_thread().run(...)`. The
real path:

```python
client = await CodexClient.connect_stdio(
    command=["codex", "app-server"],
    cwd=workspace_dir(slot),
    env={**os.environ, **codex_env_overrides},  # CODEX_HOME, OPENAI_API_KEY
)
await client.start()
await client.initialize()

if existing_thread_id:
    thread = await client.resume_thread(existing_thread_id, overrides=cfg)
else:
    thread = await client.start_thread(config=cfg)

async for step in thread.chat(prompt):
    handle_step(step)  # translate to _emit calls per table above

await client.close()
```

Cache `client` per slot in `_codex_clients` (the existing module-level
dict) — stdio subprocess is expensive to spawn. The `_SPAWN_LOCK` already
serializes turns per slot, satisfying the "one active turn consumer per
client" SDK constraint.

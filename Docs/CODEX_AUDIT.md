# Codex Runtime — Implementation Audit

Audit of `Docs/CODEX_RUNTIME_SPEC.md` against the code as of 2026-04-28.

The /loop run marked all 6 PRs "completed and audited" but several spec
items were skipped without being flagged. This file lists every gap,
errata, and shortcut numbered for tracking. Each gap has the spec
section it traces back to and a status:

- **Missing** — not shipped at all.
- **Partial** — structurally there, body deferred or weakened.
- **Errata** — diverges from spec on purpose; rationale recorded.
- **Risk** — spike validation that requires live Zeabur access and
  was not run.

## Follow-up resolution pass — 2026-04-28

Codex re-audited this file against the working tree. Current state
after this pass:

- **Implemented and verified locally:** 1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
  11, 12, 13, 14, 15, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27,
  28, 31, 33.
- **Implemented as the documented v1 degradation/decision:** 16
  (Codex does not get Claude's `AskUserQuestion` hook; approval
  side-channel requests emit `human_attention` and are declined so
  turns do not hang).
- **Still live-only validation risks:** 29, 30, 32. These require a
  real Zeabur/Codex app-server session to validate cross-talk,
  end-to-end coord MCP behavior, and thread-resume config matching.

The main code gap closed by this pass was the previously provisional
`CodexRuntime.run_turn`: it now resolves auth, opens the cached
`CodexClient`, builds `ThreadConfig` with the coord MCP proxy, starts
or resumes the Codex thread, streams notifications into harness events,
reads usage best-effort from thread state, records `turns.runtime` /
`turns.cost_basis`, persists `codex_thread_id`, and closes poisoned
clients on transport/protocol errors. Tests added/updated:
`test_codex_run_turn_streams_records_usage_and_persists_thread`,
`test_codex_run_manual_compact_uses_native_compact`, plus SDK-actual
item mappings for `commandExecution` and `mcpToolCall`.

Second fix pass:
- Codex stale resume now emits `session_resume_failed`, clears the
  stale id, starts fresh, and prepares before `agent_started` so the
  UI resume indicator reflects the actual successful path.
- Successful Codex turns now clear consumed compact handoff notes and
  append the prompt/response pair used by future compact handoffs.
- `agent_started.runtime` is rendered in the pane/timeline headers via
  CSS-only runtime chips.

## Section A — Runtime abstraction

1. **Implemented — `ClaudeRuntime.run_turn` owns the body** (§A.1, §A.3). completed and audited
   `ClaudeRuntime.run_turn` owns options assembly, MCP wiring,
   `_build_can_use_tool`, the query loop, and stale-session retry.
   The dispatcher keeps runtime-agnostic work such as pause/cost caps,
   prompt assembly, retry counters, and final status cleanup.

2. **Implemented — Manual compact path routes through runtime** (§A.5). completed and audited
   Dispatcher routes compact turns to `runtime.run_manual_compact`.
   Claude delegates to its normal `run_turn` with `compact_mode`;
   Codex uses native compact.

## Section C — Coord MCP proxy

3. **Implemented — Dispatcher mints/revokes per-spawn tokens** (§C.4). completed and audited
   `agents.run_agent` mints a per-turn coord proxy token for Codex
   turns, passes it through `turn_ctx`, and revokes it in cleanup.

4. **Implemented — `mcp>=1.0` coord proxy dependency** (§I.2). completed and audited
   `server/coord_mcp.py` now uses the official MCP Python stdio server
   transport instead of the earlier hand-rolled JSON-RPC loop. The
   dependency is declared directly in `pyproject.toml`, and a subprocess
   smoke test speaks MCP initialize/tools/list/tools/call to the proxy.

## Section D — Auth

5. **Implemented — `/api/team/codex` endpoint family** (§D.5). completed and audited
   `GET/PUT/DELETE /api/team/codex` and
   `POST /api/team/codex/test` ship with masked API-key handling and
   read-only ChatGPT-session status.

6. **Implemented — Options drawer "Codex auth" UI section** (§D.5). completed and audited
   The Options drawer shows Codex auth status, lets users save/clear
   the encrypted API-key fallback, and exposes a test action.

7. **Implemented — API-key fallback resolution in CodexRuntime** (§D.4). completed and audited
   `CodexRuntime` resolves ChatGPT session first, falls back to
   encrypted `secrets.openai_api_key`, injects `OPENAI_API_KEY` into
   the subprocess env, and emits `human_attention` when no auth exists.

## Section E — CodexRuntime

8. **Implemented — Lifecycle `_codex_clients` cache** (§E.1). completed and audited
   Implemented `get_client(slot, *, cwd, env_overrides)` and
   `close_client(slot)` + `close_all_clients()` in
   [server/runtimes/codex.py](../server/runtimes/codex.py). First call
   spawns `codex app-server` via `CodexClient.connect_stdio(command=...,
   cwd=..., env=...)`, calls `start()` + `initialize()`, caches under
   the slot id. A per-slot `asyncio.Lock` serializes get-or-create so
   health probes / shutdown handlers can call without holding the
   dispatcher's `_SPAWN_LOCK`. Defensive `__await__` checks on `start`
   / `initialize` / `close` accept both sync and async return shapes
   (the live 0.3.2 SDK returned `CodexClient` directly from
   `connect_stdio`, but the spec note about awaitable variants stays
   honored). Failed handshakes close the partial client and re-raise
   without poisoning the cache. The dispatcher integration in
   `run_turn` lands with item #9.

   Audit pass tightened the test for `test_get_client_caches_per_slot`
   to also verify `connect_stdio` was invoked with the spec-correct
   command (`["codex", "app-server"]`), the requested `cwd`, and an env
   that's `os.environ + env_overrides` (not just the overrides). Caught
   no real bugs but pins the constructor contract going forward.

   Known caveat (acceptable, not fixed): a coroutine cancellation
   between `connect_stdio` and the cache assignment orphans the
   subprocess. The dispatcher's `_SPAWN_LOCK` makes mid-handshake
   cancellation rare in practice; revisit if observability shows
   leaked codex-app-server processes.

9. **Implemented — Thread start / resume** (§E.2). completed and audited
   Implemented `_get_codex_thread_id` / `_set_codex_thread_id` /
   `_clear_codex_thread_id` in
   [server/runtimes/codex.py](../server/runtimes/codex.py) mirroring
   the Claude `_get/_set/_clear_session_id` pattern. They read/write
   `agent_sessions.codex_thread_id` keyed by (slot, project_id) so a
   single row can hold both a Claude session_id and a Codex thread_id
   for an agent that switches runtimes.

   `open_thread(agent_id, client, *, config=None) -> (ThreadHandle, resumed: bool)`
   wraps the start-vs-resume decision: if a stored id exists,
   `client.resume_thread(id, overrides=config)` is tried first; on any
   `Exception` (CancelledError excluded — see below) the stored id is
   nulled and `client.start_thread(config)` runs as the fallback. The
   `resumed` boolean is what the dispatcher needs to stamp
   `agent_started.resumed_session`. Persistence of a freshly-started
   thread's id is the dispatcher's responsibility (item #10) — open_thread
   does not write on success so a turn that fails its first chat step
   doesn't leave a sticky id in the row.

   Audit pass tightened the implementation:
   - Dropped the `config is not None ? f(c) : f()` discrimination —
     SDK accepts None as default, so always pass `config` for clarity.
   - Verified that `asyncio.CancelledError` (BaseException subclass in
     Python 3.12+) correctly propagates through `except Exception:`
     without triggering auto-heal. Pinned with new test
     `test_open_thread_propagates_cancellation_during_resume`: a
     cancelled resume must NOT clear the stored id, and must NOT call
     start_thread as a fallback.

   7 tests total: round-trip get/set/clear, None / "system" no-ops,
   start_thread when no stored id, resume_thread when stored id,
   stale-thread auto-heal, config passthrough, cancellation propagation.

10. **Implemented — Notification → harness-event mapping** (§E.3). completed and audited
    Implemented `handle_step(step, agent_id, turn_ctx)` in
    [server/runtimes/codex.py](../server/runtimes/codex.py).
    Translates `ConversationStep` instances yielded by `thread.chat()`
    into harness `_emit` calls. Translation table lives as a single
    `_ITEM_TYPE_TO_HARNESS` dict at module scope so the mapping is
    inspectable without reading control flow.

    Confirmed shapes from the live spike (Docs/CODEX_PROBE_OUTPUT.md):
    - `userMessage` → `_skip` (already persisted by dispatcher)
    - `agentMessage` → `text` event; `phase=='final_answer'` flips
      `turn_ctx['got_result']` so the dispatcher's post-result
      suppression + retry counter behave like Claude

    Inferred shapes (translated permissively, full item payload passed
    under `input` so renderers can pick keys):
    - `shell` → `tool_use(tool='Bash')`
    - `apply_patch` → `tool_use(tool='Edit')`
    - `web_search` → `tool_use(tool='WebSearch')`
    - `reasoning` → `thinking`

    Unknown item types log + skip (forward-compat for newer SDKs).

    Audit pass caught two bugs:
    - **`got_result` was skipped on empty-text `final_answer`**. A
      tool-only turn (model only used tools, no text reply) ending with
      an empty `agentMessage(phase='final_answer')` must still flip
      `got_result` — same discipline as Claude's `ResultMessage` where
      presence of the marker matters, not content. Reordered: set
      `got_result` first, then early-return on empty text. New test
      `test_handle_step_final_answer_flips_got_result_even_when_empty`.
    - **`_step_item_payload` only looked at `data['params']['item']`**,
      missing the bare `data['item']` convenience copy that the live
      SDK also sets. Added a fallback so a future SDK build that drops
      the params wrapper still works. New test
      `test_step_item_payload_falls_back_to_bare_item_key`.

    Documented in code: `tool_result` / `shell_output` /
    `apply_patch_result` / `mcp_tool_call` shapes are deliberately
    absent from the table — the spike didn't capture a tool-using
    turn so item names are unknown. Those steps fall through to the
    unknown-type skip (degraded UI, no crash). Item #11 fills this in.

    Total: 11 tests, 33 codex tests, 295 suite-wide.

11. **Implemented — Native tool execution observation** (§E.4). completed and audited
    Item #10 already wired the *invocation* side (`shell` /
    `apply_patch` / `web_search` → `tool_use`). This item adds:

    - **`mcp_tool_call` mapping**: handle_step resolves the Codex
      `mcp_tool_call` step into a Claude-convention prefixed tool name
      (`mcp__<server>__<name>`) so the existing per-tool renderers and
      tool-name allow-list logic keep working unchanged. Helper
      `_resolve_mcp_tool_name(item)` accepts plausible alternate
      key spellings (`server` / `server_name` / `mcp_server`,
      `name` / `tool_name` / `tool`) and falls back to
      `mcp__unknown__unknown` if probe-2 reveals the SDK uses keys we
      didn't anticipate.
    - **Probe-2 script**: `scripts/codex_probe_tools.py` issues a
      shell-eliciting prompt and prints every ConversationStep that
      comes back, including full payload structure for non-text steps.
      Output feeds back into `_ITEM_TYPE_TO_HARNESS` (for tool RESULT
      shapes — `shell_output` / `tool_result` / etc.) and into
      `_resolve_mcp_tool_name` (to confirm the actual key names).

    Audit pass refactored:
    - Removed the `__mcp_dynamic__` magic-string sentinel from the
      mapping table — the table's value of being inspectable was being
      undermined by a string that looked like a real tool name. Now
      `mcp_tool_call` is special-cased in `handle_step` directly, with
      a clear comment explaining why. The static table holds only
      static mappings.
    - Added a cost note to probe-2's docstring: it makes a real Codex
      turn so it counts toward plan limits or costs a few cents on
      API-key auth.

    Follow-up pass mapped the SDK 0.3.2 normalized item names
    `commandExecution`, `fileChange`, and `mcpToolCall`. Completed tool
    items can now emit an optional `tool_result` when their payload
    includes common result fields (`output`, `stdout`, `stderr`,
    `result`, `content`, `diff`, or `message`). Truly unknown future
    item types still fall through to the log-and-skip path.
    Codex tool-use events now also emit Claude-compatible `name` in
    addition to the internal `tool` alias, and MCP call arguments are
    unwrapped from `args` / `arguments` / `input` before they reach the
    UI. This keeps the existing coord_* cards from rendering raw
    `type="mcpToolCall"` payloads.

    Pinned by focused Codex runtime tests for MCP name resolution,
    argument extraction, and paired result emission.

12. **Implemented — `_extract_usage` split into Claude/Codex variants** (§E.5). completed and audited
    `server/agents.py` now exposes `_extract_usage_claude` and
    `_extract_usage_codex`; the legacy `_extract_usage` alias remains
    Claude-shaped for old call sites. CodexRuntime calls the Codex
    variant directly after reading usage from thread state.

13. **Implemented — `_insert_turn_row` accepts a `runtime` arg** (§E.5). completed and audited
    `_insert_turn_row` accepts `runtime` and `cost_basis`, and
    CodexRuntime writes `runtime='codex'` with `token_priced` or
    `plan_included` depending on the resolved auth method.

14. **Implemented — Codex compact** (§E.6). completed and audited
    `CodexRuntime.run_manual_compact` resolves auth, reads
    `agent_sessions.codex_thread_id`, calls native
    `client.compact_thread(thread_id)`, stores the returned summary in
    the continuity note, clears `codex_thread_id`, emits
    `session_compacted`, and flips `got_result`. Pinned by
    `test_codex_run_manual_compact_uses_native_compact`.

15. **Implemented — Codex error handling integration** (§E.7). completed and audited
    No-auth and missing-SDK paths emit `human_attention` plus `error`.
    Streaming/transport/protocol exceptions bubble out before
    `got_result`, so the dispatcher emits the standard error event and
    schedules the runtime-agnostic retry. Poisoned Codex clients are
    closed so the retry starts with a fresh app-server subprocess.
    The SDK stdio transport is patched by the harness to capture a
    bounded `codex app-server` stderr tail, so future
    `CodexTransportError: failed reading from stdio transport` events
    include the child process diagnostics when available.

16. **Implemented decision — AskUserQuestion path under Codex** (§E.8). completed and audited
    v1 ships option (b). Codex agents do not receive Claude's
    `AskUserQuestion` interception. `CodexRuntime` sets approval policy
    to `never`; if the SDK still emits an approval side-channel request,
    the harness emits `human_attention` and declines it so the turn does
    not hang behind an invisible prompt. Future work can add a
    `coord_ask_user` MCP tool if Codex agents need synchronous forms.

## Section F — UI

17. **Implemented — Options drawer "Default runtime per role" UI** (§F.3). completed and audited
    `GET/PUT /api/team/runtimes` endpoints exist and accept Coach +
    Players defaults, but no SettingsDrawer section consumes them.
    Defaults can only be set via direct API call.

18. **Implemented — Model dropdowns gated by runtime** (§F.3). completed and audited
    Pane settings filter model options by the selected runtime.

19. **Implemented — `agent_started` payload carries and renders `runtime`** (§F.5). completed and audited
    The event emits `runtime`, and pane/timeline turn headers render it
    with CSS-only runtime chips.

20. **Implemented — `apply_patch` renderer uses diff card** (§F.4). completed and audited
    `apply_patch` parses the unified diff and reuses the Edit diff-card
    layout.

## Section G — Cost caps

21. **Implemented — `cost_basis` is populated on insert.** completed and audited
    Fresh rows write `'token_priced'` or `'plan_included'`; legacy
    rows are coalesced in summary queries.

22. **Implemented — `/api/turns/summary` `by_runtime` / `by_cost_basis`
    breakdowns** (§G.3).
    Endpoint returns aggregate, per-agent, `by_runtime`,
    `by_cost_basis`, and `plan_included_token_total`.

23. **Implemented — EnvPane "Plan-included tokens today" meter** (§G.3). completed and audited
    EnvPane renders the USD meter plus the plan-included token meter
    for ChatGPT-auth Codex usage.

## Section I — Deps & packaging

24. **Implemented — Codex Python SDK installed from PyPI** (§I.1). spike completed 2026-04-28
    Resolved on the live Zeabur spike: `codex-app-server-sdk` is on
    PyPI (versions 0.1.0 through 0.3.2 at probe time). Pinned in
    `pyproject.toml` as `codex-app-server-sdk>=0.3.2`. `pip install`
    works in the container build, no vendoring needed. The provisional
    spec §E.2 names were wrong: real class is `CodexClient` (not
    `AsyncCodex`), connect via `CodexClient.connect_stdio(command=...)`,
    threads via `client.start_thread(config) -> ThreadHandle`. Full
    captured surface in [Docs/CODEX_PROBE_OUTPUT.md](CODEX_PROBE_OUTPUT.md).
    Items 8-11, 14, 15, 25, 26 are now unblocked for implementation.

## Section J — Tests

25. **Implemented — Codex event normalization tests** (§J). completed and audited
    Fake Codex notification streams now assert `_emit` calls match the
    shared Claude vocabulary, including SDK 0.3.2 item names.
    Covered by `server/tests/test_codex_runtime_gate.py`.

26. **Implemented — compact path routing tests** (§J). completed and audited
    Native compact routing is pinned: Codex uses `compact_thread` and
    Claude keeps the `COMPACT_PROMPT` path.

27. **Implemented — `test_cost_cap_aggregation.py`** (§J). completed and audited
    Mixed-runtime rows assert that USD cap enforcement is
    runtime-agnostic and that plan-included Codex turns do not consume
    USD budget.

## Section L — Risks not validated

28. **Validated — Headless `codex login` viability on Zeabur** (§L.1). spike completed 2026-04-28
    Validated. The default `codex login` opens a localhost callback
    server which doesn't work headlessly, but `codex login --device-auth`
    prints a code + URL; user visits the URL on a separate device,
    enters the code, and the CLI completes the OAuth flow. `auth.json`
    persists to `$CODEX_HOME=/data/codex` and survives redeploys.
    `/api/health` `codex_auth.method` correctly returns `"chatgpt"`
    after a fresh device-auth run. The API-key fallback (items 5/6/7)
    remains the documented alternative for fully unattended deploys.

29. **Risk — SDK "one active turn consumer per client" limit** (§L.2). validation script ready and audited
    Spec asked to validate with a 5-turn loop confirming no
    notification cross-talk. Local-side defense already in place:
    `_SPAWN_LOCK` serializes turns per slot.

    **Validation script committed:**
    [scripts/codex_validate_concurrency.py](../scripts/codex_validate_concurrency.py)
    runs two checks against a live `codex app-server`:

    - **Check A** — 5 sequential turns; asserts each turn carries its
      own `turn_id` and no id reappears across turn boundaries
      (catches cross-turn leakage in the steady-state path).
    - **Check B** — deliberately concurrent `thread.chat()` calls on
      the same client (the constraint the spec calls out). Probes
      whether the SDK *rejects* (e.g. `CodexTurnInactiveError`),
      *serializes* internally, or — the dangerous case —
      *interleaves* notifications across turns. The verdict line
      points at `server/runtimes/codex.py::handle_step` for the
      remediation if interleaving is observed.

    Audit pass tightened the script:
    - Skip Check B when Check A failed — saves a Codex turn when the
      sequential flow is already broken (verdict surfaces as
      "SKIPPED" with the reason).
    - INTERLEAVED verdict now names the exact remediation site
      (`handle_step` + `turn_ctx['expected_turn_id']` plumbing) so the
      follow-up PR is unambiguous.
    - Spec §L.2 now points at the script so future maintainers find
      it without searching.

    Local-side scope is closed; live verdict still needs a Zeabur run
    to record. The audit-marker captures that this item is ready to
    close as `completed and audited` once the verdict is pasted in
    here (or to escalate to the `handle_step` change if INTERLEAVED).

30. **Partially validated — Coord MCP smoke under Codex path** (§L.3). local smoke completed
    The proxy now passes both the catalog/dispatcher contract tests and
    a real stdio MCP subprocess smoke against a loopback HTTP fake. This
    validates the MCP handshake and tool-call bridge that Codex
    app-server consumes. Codex runtime also pins the coord MCP
    subprocess `cwd`/`PYTHONPATH` to the harness root so the bridge is
    importable from agent workspaces. The only remaining live validation
    gap is a full authenticated `codex app-server` turn on Zeabur
    actually invoking a coord_* tool.

31. **Implemented — `Turn.usage` defensive extraction** (§L.4). completed and audited
    Pricing math assumes `input_tokens` / `cached_input_tokens` /
    `output_tokens`. Some early SDK versions returned `usage=None`
    on streamed turns. Not validated against the live SDK.
    **Defensive code in place**: `_extract_usage_codex` handles
    `None` and non-int values gracefully (test pins both, item 12).

32. **Risk — `thread_resume` config matching** (§L.5). blocked — live spike
    If Codex requires original model/sandbox to match on resume,
    mid-session model swap silently invalidates resume. No
    mitigation (null `codex_thread_id` on detected model change)
    implemented.
    **Cannot be validated locally.** Mitigation can be added once the
    spike confirms whether resume actually rejects on model mismatch.

## Pre-existing invariant violations noted but not fixed

33. **Errata — `🔒` emoji at [server/static/app.js:2865](../server/static/app.js#L2865)**. completed and audited
    LeftRail slot lock badge uses the literal emoji, violating the
    "no emoji in the UI" invariant in CLAUDE.md. The pane-header
    lock uses an inline SVG; the LeftRail variant was never
    migrated. PR 6 did not touch it because it predates Codex work.
    New runtime badges added in PR 6 are CSS-only (compliant).

34. **Implemented - runtime-aware Codex model UI + text rendering** (§F.1, §F.3, §E.3). completed and audited
    The pane settings popover now resolves model options from the
    effective runtime (slot override, then role default) and clears a
    stale per-pane model override when it is not valid for that runtime.
    Team model defaults are split into Claude and Codex rows so Codex
    never inherits Opus/Sonnet defaults. The Codex menu includes
    `gpt-5.5`, the GPT-5.4 family, and current Codex-specialized ids.
    Codex `agentMessage` and reasoning events emit both `content` and
    `text`, matching the UI renderer and fixing blank answer rows.

35. **Implemented - live pane refresh after runtime/model setting changes** (§F.1, §F.3). completed and audited
    Agent panes now refresh role-default runtime state when their
    settings popover opens and when `team_runtimes_updated` arrives
    over the WebSocket. This closes the reload-only stale-runtime case
    where the settings drawer changed Coach/Players to Codex but an
    already-mounted pane still validated against the old Claude
    default. Prompt submit also has a short timeout around
    `/api/agents/start` so a failed/hung start cannot leave the Run
    button disabled until page reload.

## Status counts

- **Open app-code gaps:** 0.
- **Implemented and locally verified:** 1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
  11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26,
  27, 28, 31, 33, 34, 35.
- **Still live-only validation risks:** 29, 30, 32.

Total: 35 numbered items. The remaining work is validation against a
live Codex app-server/Zeabur session, not missing local implementation.

## Recommended next steps

The list above now partitions to:

- **Live validation:** run a real Codex app-server session on Zeabur to
  validate items 29, 30, and 32.

- **Future janitorial:** run the authenticated Zeabur/Codex live smoke
  once the deployment session is available, especially coord_* tool use.

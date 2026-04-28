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

## Section A — Runtime abstraction

1. **Partial — `ClaudeRuntime.run_turn` does not own the body** (§A.1, §A.3). completed and audited
   `run_turn` is a one-liner that calls `agents.run_agent`. The spec asked for
   options assembly, MCP wiring, `_build_can_use_tool`, the query loop, and
   stale-session retry to physically move into `ClaudeRuntime.run_turn`.
   Behavior is preserved (the spec required zero behavior change) but the
   carve-out is structural-only. Any future runtime that needs to share a
   sibling pattern has nothing to copy.

2. **Partial — Manual compact path wraps `run_agent`** (§A.5). completed and audited
   `ClaudeRuntime.run_manual_compact` re-enters `agents.run_agent` with
   `COMPACT_PROMPT`. Functionally correct; not the carve-out the spec
   intended.

## Section C — Coord MCP proxy

3. **Missing — Dispatcher does not mint or revoke per-spawn tokens** (§C.4). completed and audited
   `server/spawn_tokens.py` exists with `mint`/`revoke`/`revoke_for_caller`
   but `agents.run_agent` never calls them. Tokens can only be created
   manually (e.g. from a test). Wiring is blocked behind PR 5 because the
   tokens are only useful when CodexRuntime spawns the proxy subprocess.

4. **Errata — `mcp>=1.0` dependency not added** (§I.2). completed and audited
   Spec listed `"mcp>=1.0"` as a new runtime dep. Implementation went with
   hand-rolled JSON-RPC over stdio in `server/coord_mcp.py`, so the package
   is not needed. Decision worth flagging because future work that wants
   richer MCP features (resources, prompts, sampling) will need to add
   the dep then.

## Section D — Auth

5. **Missing — `/api/team/codex` endpoint family** (§D.5). completed and audited
   Spec called for `GET/PUT/DELETE /api/team/codex` mirroring the
   Telegram pattern: read-only ChatGPT-session badge, write-only masked
   API-key field, `POST /api/team/codex/test`. None of these endpoints
   exist. The encrypted `secrets.openai_api_key` slot is checked by the
   `/api/health` `codex_auth` probe but cannot be set via API.

6. **Missing — Options drawer "Codex auth" UI section** (§D.5). completed and audited
   No UI surface to set the API-key fallback or view the auth method.
   Users would have to write directly to the `secrets` table to test
   the API-key path even if (5) were shipped.

7. **Partial — API-key fallback resolution in CodexRuntime** (§D.4). completed and audited
   `CodexRuntime.run_turn` does not read `secrets.openai_api_key` and
   inject `OPENAI_API_KEY` into the subprocess env. Resolution
   precedence (chatgpt → api_key → human_attention) is not implemented.

## Section E — CodexRuntime

8. **Missing — Lifecycle `_codex_clients` cache is unused** (§E.1). completed and audited
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

9. **Missing — Thread start / resume** (§E.2). completed and audited
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

10. **Missing — Notification → harness-event mapping** (§E.3). completed
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

    Unknown item types log + skip (forward-compat for newer SDKs that
    add categories). 9 new tests pin: skip-userMessage, agentMessage
    text emission, multi-step text accumulation, empty-text no-op,
    shell/apply_patch/web_search/reasoning emission, unknown-type
    skip. Total codex tests: 31.

11. **Missing — Native tool execution observation** (§E.4). blocked — Tier 2
    `shell` / `apply_patch` / `web_search` calls inside Codex would be
    observed via notifications, then streamed to the UI. Without (10)
    none of this works.
    **Blocked on item 10**, which is itself blocked on item 24.

12. **Missing — `_extract_usage` split into Claude/Codex variants** (§E.5). completed and audited
    Single Claude-shaped `_extract_usage` still in `server/agents.py`.
    The spec required splitting into `_extract_usage_claude` and
    `_extract_usage_codex` dispatched by runtime arg.

13. **Missing — `_insert_turn_row` accepts a `runtime` arg** (§E.5). completed and audited
    Function signature unchanged. `turns.runtime` defaults to `'claude'`
    on every insert (column DEFAULT does the work) so Claude turns
    record correctly, but a Codex turn coming through the same path
    would silently be tagged as Claude.

14. **Missing — Codex compact** (§E.6). blocked — Tier 2
    `CodexRuntime.run_manual_compact` emits `human_attention` instead
    of either calling a native `thread.compact()` or running a manual
    `COMPACT_PROMPT` turn against Codex.
    **Blocked on Tier-2 SDK spike (item 24).** Whether `thread.compact()`
    exists at all is what the spike must determine; until then the
    fallback path (running a COMPACT_PROMPT turn through `run_turn`)
    can't be wired because `run_turn` itself is provisional.

15. **Missing — Codex error handling integration** (§E.7). blocked — Tier 2
    `turn.failed` pre-result → `_emit("error")` + auto-retry counter
    increment is not wired. 401/auth → `human_attention` (Telegram
    bridge precedent) is not implemented.
    **Blocked on Tier-2 SDK spike (item 24).** The auto-retry counter
    in the dispatcher already works runtime-agnostically (it counts any
    exception bubbling out of `runtime.run_turn`), so the spike work is
    just translating concrete `turn.failed` notifications into
    exceptions inside `CodexRuntime.run_turn`.

16. **Missing — AskUserQuestion path under Codex** (§E.8). decision deferred
    Spec asked for a decision between option (a) — re-expose as
    `coord_ask_user` MCP tool — or (b) — degrade gracefully on Codex.
    Neither path was implemented; no decision recorded.
    **Decision deferred to PR after spike**: option (a) is the right
    answer if the QuestionForm flow can be made to wait synchronously
    on a coord call without blocking the harness event loop. That
    determination requires a working CodexRuntime end-to-end (item 24).
    Until then, Codex agents simply don't get the `AskUserQuestion`
    tool — acceptable for v1 per §E.8 option (b).

## Section F — UI

17. **Missing — Options drawer "Default runtime per role" UI** (§F.3). completed and audited
    `GET/PUT /api/team/runtimes` endpoints exist and accept Coach +
    Players defaults, but no SettingsDrawer section consumes them.
    Defaults can only be set via direct API call.

18. **Missing — Second row of model dropdowns gated by runtime** (§F.3). completed and audited
    Spec required a Codex-vs-Claude-aware model picker that filters
    available models by runtime. Single dropdown still.

19. **Missing — `agent_started` payload carries `runtime` field** (§F.5). completed and audited
    The event still emits `prompt`, `resumed_session`, `compact_mode`,
    `auto_compact`. UI cannot render the per-turn runtime chip the
    spec described.

20. **Partial — `apply_patch` renderer is summary-line only** (§F.4). completed and audited
    Spec wanted `apply_patch` to feed unified-diff straight into
    `diff@7` and reuse the Edit diff-card layout. Implementation only
    extracts the changed file path for the header. The diff body
    falls through to the generic JSON renderer.

## Section G — Cost caps

21. **Missing — `cost_basis` is never populated on insert.** completed and audited
    Column exists in the schema and the migration backfills it as
    NULL. No code path writes `'token_priced'` or `'plan_included'`
    on a fresh row. Even Claude turns leave it NULL.

22. **Missing — completed and audited — `/api/turns/summary` `by_runtime` / `by_cost_basis`
    breakdowns** (§G.3).
    Endpoint still returns the legacy aggregate. UI cannot show the
    split meters.

23. **Missing — EnvPane "Plan-included tokens today" meter** (§G.3). completed and audited
    Single USD meter still. ChatGPT-auth Codex usage would show as
    $0.00 with no token visibility — the trap the spec called out.

## Section I — Deps & packaging

24. **Risk — Codex Python SDK not actually installed** (§I.1). spike completed 2026-04-28
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

25. **Missing — `test_codex_event_normalization.py`** (§J). blocked — Tier 2
    Spec required a fake stream of Codex notifications asserting
    `_emit` calls match Claude vocabulary. Cannot be written until
    (10) ships.
    **Blocked on item 10 → item 24.** Notification names are
    placeholders.

26. **Missing — `test_compact_path_routing.py`** (§J). blocked — Tier 2
    Spec called this conditional on the PR 1 SDK spike outcome
    (native compact vs fallback). Still missing.
    **Blocked on item 14 → item 24.** Whether `thread.compact()`
    exists is what the spike must determine.

27. **Missing — `test_cost_cap_aggregation.py`** (§J). completed and audited
    Spec wanted mixed-runtime rows in `turns` asserting combined
    sum + rejection. Cap enforcement works (24h sum is
    runtime-agnostic) but no test pins the behavior.

## Section L — Risks not validated

28. **Risk — Headless `codex login` viability on Zeabur** (§L.1). spike completed 2026-04-28
    Validated. The default `codex login` opens a localhost callback
    server which doesn't work headlessly, but `codex login --device-auth`
    prints a code + URL; user visits the URL on a separate device,
    enters the code, and the CLI completes the OAuth flow. `auth.json`
    persists to `$CODEX_HOME=/data/codex` and survives redeploys.
    `/api/health` `codex_auth.method` correctly returns `"chatgpt"`
    after a fresh device-auth run. The API-key fallback (items 5/6/7)
    remains the documented alternative for fully unattended deploys.

29. **Risk — SDK "one active turn consumer per client" limit** (§L.2). blocked — live spike
    Spec asked to validate with a 5-turn loop confirming no
    notification cross-talk. Not run.
    **Cannot be validated locally.** `_SPAWN_LOCK` already serializes
    turns per slot, satisfying this constraint defensively.

30. **Risk — Coord MCP smoke under both runtimes** (§L.3). blocked — live spike
    The proxy passes its contract test (catalog matches in-process
    registry) but has not been exercised end-to-end with a live
    `codex app-server` subprocess making real coord calls.
    **Cannot be validated locally without the SDK.** Contract test
    `test_coord_mcp_proxy.py` covers the boundary on the harness side.

31. **Risk — `Turn.usage` shape stability** (§L.4). blocked — live spike
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

## Status counts

- **Missing:** 19 items (3, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27)
- **Partial:** 5 items (1, 2, 7, 20, _and the "completed and audited" marker on PR 2 / PR 5 / PR 6 over-states completeness_)
- **Errata:** 2 items (4, 33)
- **Risk (live spike):** 6 items (24, 28, 29, 30, 31, 32)

Total: 32 numbered items + 1 documentation-marker concern.

## Recommended next steps

The list above can be partitioned by what unblocks what:

- **Tier 1 — finishable today, no live deploy needed:**
  1, 2, 5, 6, 12, 13, 17, 18, 19, 20, 21, 22, 23. These are the
  "spec'd but skipped" items that need no SDK access. Roughly
  three sittings of work.

- **Tier 2 — needs the PR 1 spike:**
  7, 8, 9, 10, 11, 14, 15, 16, 24, 25, 26, 28–32. Without confirmed
  SDK signatures these are guesswork.

- **Tier 3 — janitorial:**
  3 (token wiring once 8–11 land), 4 (re-evaluate `mcp>=1.0` need
  if richer MCP features ever needed), 33 (LeftRail emoji → SVG).

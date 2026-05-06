"""ClaudeRuntime — Anthropic Claude Code via the claude-agent-sdk.

Owns the full Claude turn body:
  - `run_turn` — `coord_server` build, allowed-tools merge,
    `_build_can_use_tool`, hooks, `ClaudeAgentOptions` assembly,
    streaming `query()` loop, stale-session retry.
  - `maybe_auto_compact` — JSONL-probe + threshold trip-wire.
  - `run_manual_compact` — `COMPACT_PROMPT` turn invoked by
    `/compact` and `POST /api/agents/{id}/compact`.

The dispatcher in `agents.run_agent` owns runtime-agnostic concerns
(pause check, spawn lock, cost caps, system-prompt assembly,
post-result exception suppression, auto-retry counter) and calls
`runtime.run_turn(tc)` once per turn. CodexRuntime mirrors this
shape; see Docs/CODEX_RUNTIME_SPEC.md §A.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from server.runtimes.base import TurnContext

logger = logging.getLogger(__name__)


def _materialize_system_prompt(system_prompt: str) -> tuple[Any, Path | None]:
    """Return a Claude SDK system_prompt value plus optional temp path.

    The Python SDK forwards a plain string as `--system-prompt <value>`
    when spawning `claude`. Large TeamOfTen prompts (CLAUDE.md plus a
    post-compact handoff) can exceed the OS argv limit before the CLI
    starts, raising E2BIG / "Argument list too long". A file-backed
    prompt uses the SDK's `--system-prompt-file` path instead.
    """
    if not system_prompt:
        return "", None

    fd, raw_path = tempfile.mkstemp(
        prefix="teamoften-claude-system-",
        suffix=".md",
    )
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(system_prompt)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return {"type": "file", "path": str(path)}, path


def _cleanup_materialized_system_prompt(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("failed to remove temporary Claude system prompt %s", path)


class ClaudeRuntime:
    """Per the AgentRuntime protocol; Claude Agent SDK backed."""

    name: str = "claude"

    async def prepare_turn_start(self, tc: TurnContext) -> bool:
        return bool(tc.prior_session)

    async def run_turn(self, tc: TurnContext) -> None:
        """Execute one Claude turn — owns SDK options, hooks, MCP wiring,
        the streaming query loop, and stale-session retry. Re-raises
        any unsuppressed exception; the dispatcher in
        `agents.run_agent` owns the post-result exception suppression
        + auto-retry counter.
        """
        # Lazy imports — `server.agents` imports `server.runtimes`
        # at module load, so the back-edge has to be deferred.
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            HookMatcher,
            query,
        )
        from server.agents import (
            MAX_TURNS_PER_SPAWN,
            _build_can_use_tool,
            _clear_session_id,
            _emit,
            _handle_message,
            _now,
            _posttool_wiki_index_hook,
            _pretool_continue_hook,
            _pretool_file_guard_hook,
            _pretool_secret_guard_hook,
            _EFFORT_LEVELS,
            workspace_dir,
        )
        from server.tools import build_coord_server

        # MCP servers: coord (in-process) + external. The dispatcher
        # provided the external dict; we attach our coord server here
        # because it's Claude-shape (in-process SDK MCP server). Codex
        # builds its coord proxy via the stdio→loopback subprocess
        # instead.
        coord_server = build_coord_server(tc.agent_id)
        mcp_servers = {"coord": coord_server, **(tc.external_mcp_servers or {})}
        system_prompt_value, system_prompt_file = _materialize_system_prompt(
            tc.system_prompt
        )

        # can_use_tool callback: intercepts AskUserQuestion and routes
        # per agent role. Coach → human form; Player → Coach inbox.
        # Callback closes over agent_id so each spawn gets a callback
        # scoped to its caller.
        can_use_tool_cb = _build_can_use_tool(tc.agent_id)

        options_kwargs: dict[str, Any] = dict(
            system_prompt=system_prompt_value,
            cwd=tc.workspace_cwd or str(await workspace_dir(tc.agent_id)),
            max_turns=MAX_TURNS_PER_SPAWN,
            mcp_servers=mcp_servers,
            allowed_tools=tc.allowed_tools,
            can_use_tool=can_use_tool_cb,
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher=None, hooks=[_pretool_continue_hook]),
                    HookMatcher(
                        matcher=r"^(Write|Edit|MultiEdit|NotebookEdit|Bash)$",
                        hooks=[_pretool_file_guard_hook],
                    ),
                    HookMatcher(
                        matcher=r"^(Read|Write|Edit|MultiEdit|NotebookEdit|Bash|Grep|Glob)$",
                        hooks=[_pretool_secret_guard_hook],
                    ),
                ],
                "PostToolUse": [
                    HookMatcher(
                        matcher=r"^(Write|Edit|MultiEdit|NotebookEdit)$",
                        hooks=[_posttool_wiki_index_hook],
                    )
                ],
            },
        )
        # Partial-message streaming — opt-in env. Some CLI builds crash
        # exit=1 on the underlying flag; flip HARNESS_STREAM_TOKENS=true
        # only after verifying your CLI handles it.
        if os.environ.get("HARNESS_STREAM_TOKENS", "").lower() in ("1", "true", "yes"):
            options_kwargs["include_partial_messages"] = True

        # Env scrub — overwrite sensitive harness env vars to empty
        # strings so the spawned `claude` CLI subprocess (and any shell
        # children it spawns for tool calls) can't read them via
        # `printenv`, `cat /proc/self/environ`, etc. The SDK merges
        # options.env INTO the inherited env, so our entries override
        # the real values. See server/agent_env.py for the policy.
        from server.agent_env import build_agent_env_overrides
        options_kwargs["env"] = build_agent_env_overrides()
        if tc.model:
            options_kwargs["model"] = tc.model
        options_kwargs["permission_mode"] = "plan" if tc.plan_mode else "default"
        if tc.effort and tc.effort in _EFFORT_LEVELS:
            options_kwargs["effort"] = _EFFORT_LEVELS[tc.effort]
        if tc.prior_session:
            options_kwargs["resume"] = tc.prior_session

        options = ClaudeAgentOptions(**options_kwargs)

        prompt = tc.prompt
        agent_id = tc.agent_id
        turn_ctx = tc.turn_ctx

        async def _prompt_stream():
            yield {
                "type": "user",
                "message": {"role": "user", "content": prompt},
            }

        async def _iterate(opts: Any) -> None:
            turn_ctx["started_at"] = _now()
            async for msg in query(prompt=_prompt_stream(), options=opts):
                await _handle_message(agent_id, msg, turn_ctx)

        try:
            try:
                await _iterate(options)
            except Exception as e:
                # Stale session auto-heal — clear stored id and retry once
                # without resume. Only when prior_session was set AND the
                # error came from the SDK subprocess layer (ProcessError).
                #
                # Guard against auth-failure misclassification: if the
                # operator just signed out (.credentials.json gone), the
                # CLI also exits with ProcessError, but the session_id
                # itself is still valid — clearing it would destroy
                # continuity that comes back the moment they sign in
                # again. Bail without touching session_id so the outer
                # error path emits the failure normally.
                is_process_err = type(e).__name__ == "ProcessError"
                if tc.prior_session and is_process_err:
                    from server.claude_login import credentials_present
                    if not credentials_present():
                        logger.warning(
                            "agent %s: ProcessError with no .credentials.json — "
                            "treating as auth failure, NOT clearing session=%s",
                            agent_id, tc.prior_session,
                        )
                        await _emit(
                            agent_id,
                            "session_resume_blocked",
                            reason="credentials_missing",
                            session_id=tc.prior_session,
                            error=f"{type(e).__name__}: {e}",
                        )
                        raise
                    logger.warning(
                        "agent %s: resume of session=%s failed, clearing and retrying fresh",
                        agent_id, tc.prior_session,
                    )
                    await _emit(
                        agent_id,
                        "session_resume_failed",
                        session_id=tc.prior_session,
                        error=f"{type(e).__name__}: {e}",
                    )
                    await _clear_session_id(agent_id)
                    options_kwargs.pop("resume", None)
                    retry_options = ClaudeAgentOptions(**options_kwargs)
                    await _iterate(retry_options)
                else:
                    raise
        finally:
            _cleanup_materialized_system_prompt(system_prompt_file)

    async def maybe_auto_compact(self, tc: TurnContext) -> bool:
        """Auto-compact trip-wire — Claude shape.

        Reads `HARNESS_AUTO_COMPACT_THRESHOLD` (default 0.7). If the
        prior session's estimated context exceeds that fraction of the
        model's window, run a COMPACT_PROMPT turn first (which writes
        the continuity note and nulls session_id), then return True so
        the dispatcher proceeds to run the user's original prompt on
        the now-fresh session.

        Returns False when:
          - this call is itself the compact turn (avoid recursion),
          - the env threshold is unset / 0 (feature off),
          - there is no prior session,
          - or the compact attempt fails (logged + emitted, the user's
            original turn proceeds on the original session).
        """
        if tc.compact_mode:
            return False
        threshold_env = os.environ.get("HARNESS_AUTO_COMPACT_THRESHOLD", "0.7")
        try:
            threshold = float(threshold_env)
        except ValueError:
            threshold = 0.7
        if not (0.0 < threshold < 1.0):
            return False

        from server import agents
        from server.agents import (
            COMPACT_PROMPT,
            _context_window_for,
            _emit,
            _get_session_id,
            _session_context_estimate,
        )

        prior = await _get_session_id(tc.agent_id)
        if not prior:
            return False
        used = await _session_context_estimate(prior)
        ctx_max = _context_window_for(tc.model)
        if ctx_max <= 0 or used / ctx_max < threshold:
            return False

        await _emit(
            tc.agent_id,
            "auto_compact_triggered",
            used_tokens=used,
            context_window=ctx_max,
            ratio=round(used / ctx_max, 3),
            threshold=threshold,
            deferred_prompt=tc.prompt,
        )
        try:
            await agents.run_agent(
                tc.agent_id,
                COMPACT_PROMPT,
                model=tc.model,
                compact_mode=True,
                auto_compact=True,
            )
        except Exception:
            # Compact failure shouldn't block the user's actual work.
            # Log, emit, return False so the dispatcher proceeds on
            # the original session — worst case is the same context-
            # pressure error they'd have hit without the feature.
            logger.exception(
                "auto-compact failed for %s; proceeding on original session",
                tc.agent_id,
            )
            await _emit(tc.agent_id, "auto_compact_failed")
            return False
        return True

    async def run_manual_compact(self, tc: TurnContext) -> None:
        """Manual `/compact` turn for Claude.

        For Claude the compact "turn" is structurally identical to a
        regular turn — the SDK runs `COMPACT_PROMPT` through the same
        `query()` loop. The compact-specific bookkeeping
        (`turn_ctx["compact_mode"]` → write `continuity_note`, null
        `session_id`) lives in `_handle_message`'s ResultMessage path,
        and reads it from the `turn_ctx` dict the dispatcher built.

        We just ensure `compact_mode` is set on the context, then
        delegate to `run_turn`. Dispatcher already routes here only
        when `tc.compact_mode` is True, so this assert is belt-and-
        braces protection against future caller mistakes.
        """
        if not tc.compact_mode:
            tc.compact_mode = True
            tc.turn_ctx["compact_mode"] = True
        await self.run_turn(tc)

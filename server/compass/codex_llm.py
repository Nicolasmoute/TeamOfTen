"""Compass Codex one-shot caller — fallback for the Claude path.

When `compass.llm.call` (Claude) fails, Compass routes the same
`(system, user)` prompt through this module to OpenAI Codex. Stateless
by design: every call spawns a fresh `codex app-server` subprocess,
starts an ephemeral thread (no resume, no persistence), sends one
chat, accumulates the assistant text, then closes the thread + client.

Why a separate module instead of reusing the agent runtime in
`server/runtimes/codex.py`:
  - That runtime is thread/session-oriented (persists `codex_thread_id`
    in `agent_sessions`, caches the client per-slot, expects a full
    `TurnContext` with workspace, MCP servers, sandbox policy, etc.).
  - Compass calls have none of that: no agent identity, no MCP, no
    workspace, no resume. A fresh client + ephemeral thread per call
    is simpler than retro-fitting the runtime to support stateless
    operation.

Cost path mirrors the runtime: ChatGPT auth → cost_basis='plan_included'
(zero $); API-key auth → priced via `pricing.codex_cost_usd`. Ledger
row written under `agent_id='compass'`, `runtime='codex'` so the
existing turn-cost rollups pick it up alongside Claude calls.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from server.compass.llm import CompassLLMError, CompassLLMResult

logger = logging.getLogger("harness.compass.codex_llm")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


_VALID_EFFORTS = frozenset({"low", "medium", "high", "max"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_codex_model(model: str | None) -> str | None:
    """Resolve the Codex fallback model. Empty / None → catalog default
    alias (`latest_mini`); aliases run through `resolve_model_alias`
    so the SDK + ledger see a concrete id."""
    from server.compass import config
    from server.models_catalog import resolve_model_alias  # noqa: PLC0415

    raw = (model or config.LLM_FALLBACK_MODEL_ALIAS).strip()
    if not raw:
        return None
    return resolve_model_alias(raw)


def _resolve_codex_effort(effort: str | None) -> str | None:
    """Validate the Codex effort string. Invalid → None (SDK uses its
    built-in default). Mirrors the Claude path's permissive shape."""
    from server.compass import config

    raw = (effort or config.LLM_FALLBACK_EFFORT or "").strip().lower()
    if raw in _VALID_EFFORTS:
        return raw
    return None


_CODEX_EFFORT_TO_SDK = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "xhigh",
}


async def call_codex(
    system: str,
    user: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    project_id: str | None = None,
    label: str = "compass:codex",
) -> CompassLLMResult:
    """Run one round-trip Codex call. Stateless: spawns + tears down
    a fresh codex app-server subprocess per call.

    Raises `CompassLLMError` when:
      - Codex SDK isn't installed.
      - Codex auth resolves to 'none' (no ChatGPT session, no API key).
      - The thread / chat itself raises before producing any assistant
        text (real failure, not a soft is_error result).

    On success returns a `CompassLLMResult` with the assistant text +
    usage. Soft errors from the SDK (no agentMessage, empty stream)
    return a result with `is_error=True` and empty text — the caller
    decides whether to skip the stage.
    """
    from server.runtimes.codex import (
        _import_codex_sdk,
        _await_if_needed,
        resolve_auth,
    )

    try:
        sdk = _import_codex_sdk()
    except ImportError as exc:
        raise CompassLLMError(f"Codex SDK unavailable: {exc}") from exc

    method, env_overrides = await resolve_auth()
    if method == "none":
        raise CompassLLMError(
            "Codex fallback unavailable: no ChatGPT session at "
            "$CODEX_HOME/auth.json and no openai_api_key in secrets"
        )

    chosen_model = _resolve_codex_model(model)
    chosen_effort = _resolve_codex_effort(effort)

    # Build a scrubbed env for the codex app-server subprocess. Same
    # rationale as agent runtimes: don't leak HARNESS_TOKEN, KDRIVE_*,
    # secrets-key, etc. to the subprocess.
    from server.agent_env import build_clean_agent_env  # noqa: PLC0415

    env = build_clean_agent_env(extra=env_overrides)
    # Compass calls have no MCP and no coord proxy — drop the proxy
    # token if it leaked through the clean-env build.
    env.pop("HARNESS_COORD_PROXY_TOKEN", None)

    # Compass calls don't need a workspace cwd. Use /tmp (or the
    # platform equivalent) so the subprocess has a real working dir.
    cwd = os.environ.get("HARNESS_COMPASS_CODEX_CWD", "/tmp").strip() or "/tmp"

    started = _now_iso()
    started_mono_ns = _monotonic_ns()
    text_parts: list[str] = []
    is_error = False
    final_turn_id: str | None = None

    client = sdk.CodexClient.connect_stdio(
        command=["codex", "app-server"],
        cwd=cwd,
        env=env,
    )
    if hasattr(client, "__await__"):
        client = await client  # type: ignore[misc]

    try:
        await _await_if_needed(client.start())
        await _await_if_needed(client.initialize())

        # Build a minimal thread config: no MCP, read-only sandbox,
        # never approve (the call should never need to side-channel
        # the harness for command/file approval — Compass prompts
        # only ask for text).
        config_overrides: dict[str, Any] = {"mcp_servers": {}}
        thread_config_kwargs: dict[str, Any] = {
            "cwd": cwd,
            "developer_instructions": system,
            "approval_policy": "never",
            "sandbox": "read-only",
            "config": config_overrides,
        }
        if chosen_model:
            thread_config_kwargs["model"] = chosen_model
        thread_config_cls = getattr(sdk, "ThreadConfig", None)
        if thread_config_cls is not None:
            thread_config = thread_config_cls(**thread_config_kwargs)
        else:
            thread_config = thread_config_kwargs

        thread = client.start_thread(thread_config)
        if hasattr(thread, "__await__"):
            thread = await thread  # type: ignore[misc]

        turn_overrides_kwargs: dict[str, Any] = {"cwd": cwd}
        if chosen_model:
            turn_overrides_kwargs["model"] = chosen_model
        sdk_effort = _CODEX_EFFORT_TO_SDK.get(chosen_effort or "")
        if sdk_effort:
            turn_overrides_kwargs["effort"] = sdk_effort
        turn_overrides_cls = getattr(sdk, "TurnOverrides", None)
        if turn_overrides_cls is not None:
            turn_overrides = turn_overrides_cls(**turn_overrides_kwargs)
        else:
            turn_overrides = turn_overrides_kwargs

        stream = thread.chat(
            user,
            user="compass",
            metadata={"project_id": project_id} if project_id else None,
            turn_overrides=turn_overrides,
        )
        stream = await _await_if_needed(stream)

        async for step in stream:
            tid = getattr(step, "turn_id", None)
            if tid:
                final_turn_id = tid
            item_type = getattr(step, "item_type", "") or ""
            if item_type in ("agentMessage",):
                txt = getattr(step, "text", None) or ""
                if txt:
                    text_parts.append(txt)
            # Other item types (reasoning, tool_call, etc.) are
            # ignored — Compass prompts ask for plain text + JSON only.

        # Pull usage from the rollout file. Best-effort; missing usage
        # → zeros, ledger row still gets written.
        usage = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        rollout_model: str | None = None
        try:
            from server.runtimes.codex import (
                _read_codex_token_count_from_rollout,
                _codex_usage_from_rollout_info,
                _rollout_path_from_thread_state,
                _model_from_rollout,
                _extract_codex_usage_from_thread_state,
            )
            from server.agents import _extract_usage_codex  # noqa: PLC0415

            read = thread.read(include_turns=True)
            thread_state = await _await_if_needed(read)
            rollout_path = _rollout_path_from_thread_state(thread_state)
            usage_from_rollout: dict[str, int] | None = None
            if rollout_path is not None:
                rollout_info = _read_codex_token_count_from_rollout(rollout_path)
                if rollout_info is not None:
                    usage_from_rollout = _codex_usage_from_rollout_info(rollout_info)
                if not chosen_model:
                    rollout_model = _model_from_rollout(rollout_path)
            if usage_from_rollout is not None:
                usage = usage_from_rollout
            else:
                usage_raw = _extract_codex_usage_from_thread_state(
                    thread_state,
                    final_turn_id,
                )
                usage = _extract_usage_codex(usage_raw)
        except Exception:
            logger.exception(
                "compass.codex_llm: usage extraction failed "
                "(continuing with zeros)"
            )

    except Exception as exc:
        # Real pre-result failure: SDK never produced an assistant
        # message stream. Surface as CompassLLMError so the caller can
        # decide (skip stage, mark run as failed).
        logger.exception("compass.codex_llm: chat failed")
        try:
            close = client.close()
            if hasattr(close, "__await__"):
                await close
        except Exception:
            logger.exception(
                "compass.codex_llm: close() during failure raised "
                "(ignoring)"
            )
        raise CompassLLMError(f"{type(exc).__name__}: {str(exc)[:300]}") from exc
    else:
        # Best-effort close — leak the subprocess on close failure
        # rather than raise; the result is already complete.
        try:
            close = client.close()
            if hasattr(close, "__await__"):
                await close
        except Exception:
            logger.exception("compass.codex_llm: close() raised (ignoring)")

    result_text = "".join(text_parts).strip()
    duration_ms = int((_monotonic_ns() - started_mono_ns) / 1_000_000)

    # Cost: chatgpt auth → plan-included; api_key → priced.
    effective_model = chosen_model or rollout_model
    cost_basis = "plan_included" if method == "chatgpt" else "token_priced"
    if cost_basis == "plan_included":
        cost_usd: float | None = 0.0
    else:
        try:
            from server.pricing import codex_cost_usd  # noqa: PLC0415

            cost_usd = codex_cost_usd(
                effective_model,
                {
                    "input_tokens": usage["input"],
                    "cached_input_tokens": usage["cache_read"],
                    "output_tokens": usage["output"],
                },
            )
        except Exception:
            logger.exception("compass.codex_llm: pricing failed")
            cost_usd = None

    # Ledger insert. Lazy import to dodge agents↔compass back-edge.
    try:
        from server.agents import _insert_turn_row  # noqa: PLC0415

        await _insert_turn_row(
            agent_id="compass",
            started_at=started,
            ended_at=_now_iso(),
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            session_id=None,  # Stateless: no thread_id persisted.
            num_turns=1,
            stop_reason=None,
            is_error=is_error,
            model=effective_model,
            plan_mode=False,
            effort=None,
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            cache_read_tokens=usage["cache_read"],
            cache_creation_tokens=usage["cache_creation"],
            runtime="codex",
            cost_basis=label,
        )
    except Exception:
        logger.exception("compass.codex_llm: turn ledger insert failed (continuing)")

    # Bus event for live UI counters.
    try:
        from server.events import bus  # noqa: PLC0415

        await bus.publish({
            "ts": _now_iso(),
            "agent_id": "compass",
            "type": "compass_llm_call",
            "label": label,
            "model": effective_model,
            "runtime": "codex",
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
            "input_tokens": usage["input"],
            "output_tokens": usage["output"],
            "is_error": is_error,
            "project_id": project_id,
        })
    except Exception:
        pass

    # If the stream ended with no agent text at all, treat as soft
    # error — caller's parse_json_safe will return None and the
    # stage skips. Same shape as a Claude empty result.
    if not result_text:
        is_error = True

    return CompassLLMResult(
        text=result_text,
        is_error=is_error,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        input_tokens=usage["input"],
        output_tokens=usage["output"],
        cache_read_tokens=usage["cache_read"],
        cache_creation_tokens=usage["cache_creation"],
        session_id=None,
        stop_reason=None,
        errors=[],
    )


def _monotonic_ns() -> int:
    import time as _t

    return _t.monotonic_ns()


__all__ = ["call_codex"]

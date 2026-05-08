"""Playbook LLM wrapper — Claude primary, Codex fallback.

Single LLM call per run (bootstrap or daily reflection). No per-run
latch (Compass has one because its pipeline has many stages; Playbook
runs are single-stage).

Fallback path mirrors Compass §5.5.2: on `_call_claude` raise OR
`is_error=True`, route the same `(system, user)` through
`server.shared.codex_llm.call_codex` with `agent_id="playbook"`,
`event_type="playbook_llm_call"`. If both fail, surface the original
Claude error.

Tolerant JSON parsing lives here too (mirrors Compass `parse_json_safe`)
because the wider harness's parsing utilities aren't promoted to a
shared module yet — duplication is deliberate per spec §15 N8.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from server.playbook import config
from server.shared.llm_types import LLMError, LLMResult

logger = logging.getLogger("harness.playbook.llm")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


_VALID_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "max"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _monotonic_ns() -> int:
    import time as _t

    return _t.monotonic_ns()


def _resolve_model(model: str | None) -> str | None:
    """Pick the model. Precedence: explicit param > config default
    (`latest_sonnet`). Resolve aliases through the catalog."""
    from server.models_catalog import resolve_model_alias  # noqa: PLC0415

    raw = model or config.LLM_MODEL_DEFAULT_ALIAS
    return resolve_model_alias(raw)


def _resolve_effort() -> str | None:
    raw = (config.LLM_EFFORT or "").strip().lower()
    return raw if raw in _VALID_EFFORTS else None


# ---------------------------------------------------------------- public


async def call(
    system: str,
    user: str,
    *,
    model: str | None = None,
    project_id: str | None = None,
    label: str = "playbook",
) -> LLMResult:
    """Run one round-trip Claude call with Codex fallback.

    Returns `LLMResult` (success or soft error). Raises `LLMError`
    only if BOTH runtimes fail.

    `label` becomes the turn-ledger `cost_basis` value (e.g.
    `"playbook:bootstrap"`, `"playbook:reflection"`).
    """
    try:
        result = await _call_claude(system, user, model=model, project_id=project_id, label=label)
    except LLMError as exc:
        if not config.LLM_FALLBACK_ENABLED:
            raise
        logger.info("playbook.llm: Claude raised, falling back to Codex (%s)", exc)
        return await _call_codex_via_helper(system, user, project_id=project_id, label=label)

    if result.is_error and config.LLM_FALLBACK_ENABLED:
        logger.info("playbook.llm: Claude returned is_error=True, falling back to Codex")
        try:
            return await _call_codex_via_helper(system, user, project_id=project_id, label=label)
        except LLMError:
            # Both failed — surface the original Claude soft error.
            return result

    return result


async def _call_codex_via_helper(
    system: str,
    user: str,
    *,
    project_id: str | None,
    label: str,
) -> LLMResult:
    """Indirection so tests can monkeypatch the Codex path on this
    module without importing `shared.codex_llm` (which has a hard
    dependency on the Codex SDK)."""
    from server.shared.codex_llm import call_codex  # noqa: PLC0415

    return await call_codex(
        system,
        user,
        agent_id="playbook",
        event_type="playbook_llm_call",
        default_model_alias=config.LLM_FALLBACK_MODEL_ALIAS,
        default_effort=config.LLM_FALLBACK_EFFORT,
        project_id=project_id,
        label=label,
        cwd_env_var="HARNESS_PLAYBOOK_CODEX_CWD",
    )


async def _call_claude(
    system: str,
    user: str,
    *,
    model: str | None = None,
    project_id: str | None = None,
    label: str = "playbook",
) -> LLMResult:
    """Claude-side implementation. Same shape as Compass's wrapper —
    one-shot `claude_agent_sdk.query()` with no MCP, no resume, no
    hooks. Logs a row to `turns` ledger under `agent_id="playbook"`.
    Emits `playbook_llm_call` bus event for the dashboard counter.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        StreamEvent,  # noqa: F401
        TextBlock,
        UserMessage,  # noqa: F401
        query,
    )

    chosen_model = _resolve_model(model)
    chosen_effort = _resolve_effort()
    options_kwargs: dict[str, Any] = dict(
        system_prompt=system,
        max_turns=1,
        mcp_servers={},
        allowed_tools=[],
    )
    if chosen_model:
        options_kwargs["model"] = chosen_model
    if chosen_effort:
        options_kwargs["effort"] = chosen_effort
    from server.agent_env import build_agent_env_overrides
    options_kwargs["env"] = build_agent_env_overrides()
    options = ClaudeAgentOptions(**options_kwargs)

    started = _now_iso()
    started_mono_ns = _monotonic_ns()
    text_parts: list[str] = []
    is_error = False
    cost_usd: float | None = None
    session_id: str | None = None
    duration_ms: int | None = None
    stop_reason: str | None = None
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0
    errors_summary: list[str] = []
    saw_result = False

    async def _prompt_stream() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "user", "message": {"role": "user", "content": user}}

    try:
        async for msg in query(prompt=_prompt_stream(), options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text or "")
            elif isinstance(msg, ResultMessage):
                saw_result = True
                cost_usd = getattr(msg, "total_cost_usd", None)
                duration_ms = getattr(msg, "duration_ms", None)
                session_id = getattr(msg, "session_id", None)
                stop_reason = getattr(msg, "stop_reason", None)
                is_error = bool(getattr(msg, "is_error", False))
                usage = _safe_usage(msg)
                input_tokens = usage["input"]
                output_tokens = usage["output"]
                cache_read_tokens = usage["cache_read"]
                cache_creation_tokens = usage["cache_creation"]
                errors_summary = _stringify_errors(getattr(msg, "errors", None))
    except Exception as e:
        # Post-result teardown noise: if the SDK already produced a
        # ResultMessage, ignore the trailing exception (matches the
        # broader harness convention from agents.py).
        if not saw_result:
            logger.exception("playbook.llm.call failed before ResultMessage")
            raise LLMError(f"{type(e).__name__}: {str(e)[:300]}") from e
        logger.warning(
            "playbook.llm.call: ignoring post-result %s: %s",
            type(e).__name__, str(e)[:200],
        )

    result_text = "".join(text_parts).strip()

    # Cost ledger insert.
    try:
        from server.agents import _insert_turn_row  # noqa: PLC0415

        await _insert_turn_row(
            agent_id="playbook",
            started_at=started,
            ended_at=_now_iso(),
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            session_id=session_id,
            num_turns=1,
            stop_reason=stop_reason,
            is_error=is_error,
            model=chosen_model,
            plan_mode=False,
            effort=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            runtime="claude",
            cost_basis=label,
        )
    except Exception:
        logger.exception("playbook.llm: turn ledger insert failed (continuing)")

    # Bus event for live UI counters.
    try:
        from server.events import bus  # noqa: PLC0415

        await bus.publish({
            "ts": _now_iso(),
            "agent_id": "playbook",
            "type": "playbook_llm_call",
            "label": label,
            "model": chosen_model,
            "runtime": "claude",
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "is_error": is_error,
            "project_id": project_id,
        })
    except Exception:
        pass

    elapsed_ms = duration_ms
    if elapsed_ms is None:
        elapsed_ms = int((_monotonic_ns() - started_mono_ns) / 1_000_000)

    return LLMResult(
        text=result_text,
        is_error=is_error,
        cost_usd=cost_usd,
        duration_ms=elapsed_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        session_id=session_id,
        stop_reason=stop_reason,
        errors=errors_summary,
    )


def _safe_usage(msg: Any) -> dict[str, int]:
    """Pull token counts off ResultMessage.usage defensively (mirrors
    Compass's helper)."""
    u = getattr(msg, "usage", None)
    if u is None:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}

    def _get(name: str) -> int:
        v = u.get(name) if isinstance(u, dict) else getattr(u, name, None)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    return {
        "input": _get("input_tokens"),
        "output": _get("output_tokens"),
        "cache_read": _get("cache_read_input_tokens"),
        "cache_creation": _get("cache_creation_input_tokens"),
    }


def _stringify_errors(raw: Any) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for err in list(raw)[:3]:
        if isinstance(err, str):
            out.append(err[:300])
        elif isinstance(err, dict):
            msg_field = err.get("message") or err.get("error") or str(err)
            out.append(str(msg_field)[:300])
        else:
            out.append(str(err)[:300])
    return out


# ---------------------------------------------------------------- JSON parsing


_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.MULTILINE)


def parse_json_safe(text: str) -> Any:
    """Best-effort parse of LLM-returned JSON. See Compass's wrapper —
    same algorithm: raw → fence-strip → brace-balance scan. Returns
    `None` on hopeless input.
    """
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _FENCE_RE.search(text)
    if m:
        inner = m.group(1).strip()
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass
    sliced = _extract_balanced(text)
    if sliced is not None:
        try:
            return json.loads(sliced)
        except json.JSONDecodeError:
            return None
    return None


def _extract_balanced(text: str) -> str | None:
    """First-balanced-`{`/`[` extractor. Respects JSON strings + escapes."""
    open_chars = {"{": "}", "[": "]"}
    start = -1
    open_ch = ""
    for i, ch in enumerate(text):
        if ch in open_chars:
            start = i
            open_ch = ch
            break
    if start < 0:
        return None
    close_ch = open_chars[open_ch]
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


__all__ = [
    "call",
    "parse_json_safe",
    "LLMError",
    "LLMResult",
]

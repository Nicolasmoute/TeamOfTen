"""TruthGate LLM wrapper with TruthGate ledger attribution."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from server.models_catalog import model_is_claude, model_is_codex
from server.shared.llm_types import LLMError, LLMResult
from server.truthgate.config import TruthGateConfig

logger = logging.getLogger("harness.truthgate.llm")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def call_classifier(
    system: str,
    user: str,
    *,
    cfg: TruthGateConfig,
    project_id: str,
) -> LLMResult:
    """Run primary classifier model, then fallback on hard/soft failure."""
    try:
        result = await _call_model(
            system,
            user,
            model=cfg.model,
            effort=cfg.effort,
            max_tokens=cfg.max_tokens,
            project_id=project_id,
        )
        if not result.is_error:
            return result
    except LLMError:
        logger.exception("truthgate.llm: primary classifier call failed")

    return await _call_model(
        system,
        user,
        model=cfg.fallback_model,
        effort=cfg.effort,
        max_tokens=cfg.max_tokens,
        project_id=project_id,
    )


async def _call_model(
    system: str,
    user: str,
    *,
    model: str,
    effort: str,
    max_tokens: int,
    project_id: str,
) -> LLMResult:
    if model_is_claude(model):
        return await _call_claude(
            system,
            user,
            model=model,
            effort=effort,
            max_tokens=max_tokens,
            project_id=project_id,
        )
    if model_is_codex(model):
        from server.shared.codex_llm import call_codex  # noqa: PLC0415

        return await call_codex(
            system,
            user,
            agent_id="truthgate",
            event_type="truthgate_llm_call",
            default_model_alias=model,
            default_effort=effort,
            model=model,
            effort=effort,
            project_id=project_id,
            label="truthgate:classifier",
            cwd_env_var="HARNESS_TRUTHGATE_CODEX_CWD",
        )
    raise LLMError(f"unsupported TruthGate classifier model: {model}")


async def _call_claude(
    system: str,
    user: str,
    *,
    model: str,
    effort: str,
    max_tokens: int,
    project_id: str,
) -> LLMResult:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    from server.agent_env import build_agent_env_overrides  # noqa: PLC0415

    # The Claude SDK version in use does not expose a direct
    # response-token cap; prompt/corpus budgets are enforced before
    # this call and max_turns=1 prevents multi-turn expansion.
    _ = max_tokens

    options = ClaudeAgentOptions(
        system_prompt=system,
        max_turns=1,
        mcp_servers={},
        allowed_tools=[],
        model=model,
        effort=effort,
        env=build_agent_env_overrides(),
    )
    started = _now_iso()
    started_ns = _monotonic_ns()
    text_parts: list[str] = []
    is_error = False
    cost_usd: float | None = None
    duration_ms: int | None = None
    session_id: str | None = None
    stop_reason: str | None = None
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    errors: list[str] = []
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
                errors = _stringify_errors(getattr(msg, "errors", None))
    except Exception as exc:
        if not saw_result:
            raise LLMError(f"{type(exc).__name__}: {str(exc)[:300]}") from exc

    await _insert_ledger_row(
        project_id=project_id,
        started=started,
        duration_ms=duration_ms,
        cost_usd=cost_usd,
        session_id=session_id,
        stop_reason=stop_reason,
        is_error=is_error,
        model=model,
        effort=effort,
        usage=usage,
    )
    await _emit_call_event(
        project_id=project_id,
        model=model,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        is_error=is_error,
        usage=usage,
    )
    return LLMResult(
        text="".join(text_parts).strip(),
        is_error=is_error,
        cost_usd=cost_usd,
        duration_ms=duration_ms or int((_monotonic_ns() - started_ns) / 1_000_000),
        input_tokens=usage["input"],
        output_tokens=usage["output"],
        cache_read_tokens=usage["cache_read"],
        cache_creation_tokens=usage["cache_creation"],
        session_id=session_id,
        stop_reason=stop_reason,
        errors=errors,
    )


async def _insert_ledger_row(
    *,
    project_id: str,
    started: str,
    duration_ms: int | None,
    cost_usd: float | None,
    session_id: str | None,
    stop_reason: str | None,
    is_error: bool,
    model: str,
    effort: str,
    usage: dict[str, int],
) -> None:
    try:
        from server.agents import _insert_turn_row  # noqa: PLC0415

        await _insert_turn_row(
            agent_id="truthgate",
            started_at=started,
            ended_at=_now_iso(),
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            session_id=session_id,
            num_turns=1,
            stop_reason=stop_reason,
            is_error=is_error,
            model=model,
            plan_mode=False,
            effort=None,
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            cache_read_tokens=usage["cache_read"],
            cache_creation_tokens=usage["cache_creation"],
            runtime="claude",
            cost_basis="truthgate:classifier",
        )
    except Exception:
        logger.exception("truthgate.llm: turn ledger insert failed")


async def _emit_call_event(
    *,
    project_id: str,
    model: str,
    cost_usd: float | None,
    duration_ms: int | None,
    is_error: bool,
    usage: dict[str, int],
) -> None:
    try:
        from server.events import bus  # noqa: PLC0415

        await bus.publish({
            "ts": _now_iso(),
            "agent_id": "truthgate",
            "type": "truthgate_llm_call",
            "label": "truthgate:classifier",
            "model": model,
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
            "input_tokens": usage["input"],
            "output_tokens": usage["output"],
            "is_error": is_error,
            "project_id": project_id,
        })
    except Exception:
        pass


def _safe_usage(msg: Any) -> dict[str, int]:
    raw = getattr(msg, "usage", None)
    if raw is None:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}

    def get(name: str) -> int:
        val = raw.get(name) if isinstance(raw, dict) else getattr(raw, name, None)
        try:
            return int(val) if val is not None else 0
        except (TypeError, ValueError):
            return 0

    return {
        "input": get("input_tokens"),
        "output": get("output_tokens"),
        "cache_read": get("cache_read_input_tokens"),
        "cache_creation": get("cache_creation_input_tokens"),
    }


def _stringify_errors(raw: Any) -> list[str]:
    if not raw:
        return []
    return [str(item)[:300] for item in list(raw)[:3]]


def _monotonic_ns() -> int:
    import time as _time
    return _time.monotonic_ns()


__all__ = ["call_classifier"]

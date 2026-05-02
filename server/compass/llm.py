"""Compass LLM wrapper — calls Claude via the Agent SDK, parses JSON.

Compass is the harness's first server-side LLM caller (everything else
goes through `agents.run_agent`). The wrapper is intentionally thin:

  - Build a one-shot `ClaudeAgentOptions` (no MCP tools, no resume,
    no hooks) — Compass calls are stateless.
  - Stream the response via `claude_agent_sdk.query()`, accumulate
    text from `AssistantMessage` / `TextBlock`, capture usage from
    `ResultMessage`.
  - Insert one row into the existing `turns` ledger under
    `agent_id="compass"`, `runtime="claude"` so cost rolls up with
    the rest of the team.
  - Emit a `compass_llm_call` event for the dashboard's live phase
    counter.

The wrapper deliberately does NOT enforce cost caps itself —
`runner.run` checks `team_today_usd` before starting a pipeline so
the cap is observed at the right granularity (one cap-trip per
run, not per pipeline-stage). Compass MCP tools (compass_ask /
compass_audit) read the same flag.

JSON parsing lives here too: every Compass prompt asks the LLM for
strict JSON, but LLMs are sloppy — `parse_json_safe` strips code
fences, brace-balances, and falls back to `None` on hopeless input.
Callers pair this with a tiny schema check.

Max-OAuth invariant: `query()` runs the local Claude CLI subprocess,
reusing the same `/data/claude/.credentials.json` as agent turns.
There is no `ANTHROPIC_API_KEY` path here — by design.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from server.compass import config

logger = logging.getLogger("harness.compass.llm")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


class CompassLLMError(RuntimeError):
    """Raised when an LLM call fails before producing usable output.
    Caller decides whether to retry, fall back to a stub, or skip
    the pipeline stage. Compass runs are best-effort: a failed
    digest is logged and the run continues with the next stage."""


@dataclass
class CompassLLMResult:
    text: str
    is_error: bool = False
    cost_usd: float | None = None
    duration_ms: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    session_id: str | None = None
    stop_reason: str | None = None
    errors: list[str] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- query


def _resolve_model(model: str | None) -> str | None:
    """Pick the model for this Compass call.

    Precedence: explicit param > HARNESS_COMPASS_MODEL env > None
    (which lets the SDK fall back to its own default — typically the
    Coach default model in the user's environment)."""
    if model:
        return model
    return config.LLM_MODEL_OVERRIDE


async def call(
    system: str,
    user: str,
    *,
    max_tokens: int | None = None,
    model: str | None = None,
    project_id: str | None = None,
    label: str = "compass",
) -> CompassLLMResult:
    """Run one round-trip Claude call. Returns the assistant's final
    text plus usage metrics. Raises `CompassLLMError` only if the
    SDK never produces a `ResultMessage` (i.e. the subprocess died
    before it could finish — distinct from a soft `is_error=True`
    result, which is captured in the return value).

    `label` is recorded in the `turns` ledger's `cost_basis` field so
    a query like `SELECT * FROM turns WHERE agent_id='compass' AND
    cost_basis='compass:audit'` discriminates pipeline stages without
    a separate table. Limited to short identifiers ("compass:digest",
    "compass:audit", etc.).
    """
    # Lazy import — claude_agent_sdk is a heavy import; test fixtures
    # without an installed SDK still load this module. The SDK is
    # always present in production (declared in pyproject.toml), so
    # the lazy guard only matters for hermetic unit tests that
    # monkeypatch `query` before calling.
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        StreamEvent,  # noqa: F401 — referenced for type discrimination
        TextBlock,
        UserMessage,  # noqa: F401 — same
        query,
    )

    chosen_model = _resolve_model(model)
    options_kwargs: dict[str, Any] = dict(
        system_prompt=system,
        max_turns=1,
        mcp_servers={},
        allowed_tools=[],
    )
    if chosen_model:
        options_kwargs["model"] = chosen_model
    # Env scrub — same rationale as server/runtimes/claude.py: the
    # Compass `query()` call spawns a `claude` CLI subprocess that
    # would otherwise inherit HARNESS_TOKEN / KDRIVE_* / etc.
    from server.agent_env import build_agent_env_overrides
    options_kwargs["env"] = build_agent_env_overrides()
    options = ClaudeAgentOptions(**options_kwargs)

    started = _now_iso()
    started_mono_ns = _monotonic_ns()
    text_parts: list[str] = []

    result_text = ""
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
        # If we already got a ResultMessage, the SDK occasionally
        # raises during subprocess teardown — see the agents.py post-
        # result-suppression rule. Treat as soft-success here too.
        if not saw_result:
            logger.exception("compass.llm.call failed before ResultMessage")
            raise CompassLLMError(f"{type(e).__name__}: {str(e)[:300]}") from e
        logger.warning(
            "compass.llm.call: ignoring post-result %s: %s",
            type(e).__name__, str(e)[:200],
        )

    result_text = "".join(text_parts).strip()

    # Cost ledger insert. Lazy import to dodge the agents↔compass
    # back-edge.
    try:
        from server.agents import _insert_turn_row  # noqa: PLC0415

        await _insert_turn_row(
            agent_id="compass",
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
        # Ledger insert failure is non-fatal: events log + the
        # caller's run record still capture the call.
        logger.exception("compass.llm: turn ledger insert failed (continuing)")

    # Bus event for live UI counters. Lazy import to avoid circulars
    # in test contexts that haven't booted the bus.
    try:
        from server.events import bus  # noqa: PLC0415

        await bus.publish({
            "ts": _now_iso(),
            "agent_id": "compass",
            "type": "compass_llm_call",
            "label": label,
            "model": chosen_model,
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

    return CompassLLMResult(
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


def _monotonic_ns() -> int:
    import time as _t
    return _t.monotonic_ns()


def _safe_usage(msg: Any) -> dict[str, int]:
    """Pull token counts off ResultMessage.usage, defending against
    SDK shape drift. Mirrors `agents._extract_usage_claude` but lives
    here so this module doesn't import server.agents at module load."""
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


# --------------------------------------------------- JSON parsing


_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.MULTILINE)


def parse_json_safe(text: str) -> Any:
    """Best-effort parse of LLM-returned JSON.

    Strategy (each fallback only fires if the prior step fails):
      1. `json.loads(text)` directly.
      2. Strip a ```json``` (or unlabeled ```) fence, parse the inner
         block.
      3. Brace-balanced scan: find the first `{` or `[`, walk forward
         keeping a depth counter that respects strings + escapes,
         stop when depth returns to 0, parse that slice.

    Returns the parsed value (`dict`, `list`, etc.) or `None` on
    hopeless input. The caller is responsible for schema-shape
    validation — Compass does this in the pipeline modules where
    the expected shape is defined.
    """
    if not text:
        return None
    text = text.strip()
    # Step 1: raw parse.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Step 2: code-fence strip.
    m = _FENCE_RE.search(text)
    if m:
        inner = m.group(1).strip()
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            pass
    # Step 3: brace-balance.
    sliced = _extract_balanced(text)
    if sliced is not None:
        try:
            return json.loads(sliced)
        except json.JSONDecodeError:
            return None
    return None


def _extract_balanced(text: str) -> str | None:
    """Return the substring from the first `{` or `[` to the matching
    closing brace/bracket. Respects strings and escape sequences so a
    `}` inside a JSON string doesn't fool us. Returns None if no
    balanced span is found.
    """
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


# Schema-shape helpers — small, intentional, no jsonschema dep.


def expect_dict(obj: Any) -> dict[str, Any] | None:
    return obj if isinstance(obj, dict) else None


def expect_list(obj: Any) -> list[Any] | None:
    return obj if isinstance(obj, list) else None


def get_list(obj: Any, key: str) -> list[Any]:
    if isinstance(obj, dict):
        v = obj.get(key)
        if isinstance(v, list):
            return v
    return []


def get_str(obj: Any, key: str, default: str = "") -> str:
    if isinstance(obj, dict):
        v = obj.get(key)
        if isinstance(v, str):
            return v
    return default


def get_float(obj: Any, key: str, default: float = 0.0) -> float:
    if isinstance(obj, dict):
        v = obj.get(key)
        try:
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            return default
    return default


__all__ = [
    "CompassLLMError",
    "CompassLLMResult",
    "call",
    "parse_json_safe",
    "expect_dict",
    "expect_list",
    "get_list",
    "get_str",
    "get_float",
]

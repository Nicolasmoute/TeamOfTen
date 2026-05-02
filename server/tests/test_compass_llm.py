"""Phase 2 tests — Compass LLM wrapper + JSON parsing.

`call()` is exercised against a stubbed `claude_agent_sdk.query` so
tests don't need a live Claude subprocess. The stub yields fake
`AssistantMessage` and `ResultMessage` objects with the same duck
types the SDK uses (TextBlock, usage dict).

`parse_json_safe` is exercised against:
  - clean JSON
  - JSON wrapped in a ```json fence
  - JSON wrapped in an unlabeled ``` fence
  - JSON with a leading explanation paragraph (brace-balance fallback)
  - JSON with `}` inside a string literal (must not be tricked)
  - hopeless input → None
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from server.compass import llm


# ----------------------------------------------------- SDK stubs


@dataclass
class _StubTextBlock:
    text: str


@dataclass
class _StubAssistantMessage:
    content: list[Any] = field(default_factory=list)


@dataclass
class _StubResultMessage:
    is_error: bool = False
    total_cost_usd: float | None = 0.012
    duration_ms: int | None = 130
    session_id: str | None = "sess-x"
    stop_reason: str | None = "end_turn"
    usage: dict[str, int] | None = field(
        default_factory=lambda: {
            "input_tokens": 120,
            "output_tokens": 88,
            "cache_read_input_tokens": 64,
            "cache_creation_input_tokens": 0,
        }
    )
    errors: list[Any] = field(default_factory=list)


def _patch_sdk(monkeypatch: pytest.MonkeyPatch, response_text: str, *, raise_after_result: bool = False) -> None:
    """Replace `claude_agent_sdk.query` with an async generator that
    yields one assistant text block and one result message, optionally
    raising during teardown."""

    class _Options:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    async def _query(prompt: Any = None, options: Any = None) -> Any:  # type: ignore[no-redef]
        # Drain the prompt stream so the test confirms it's consumed.
        if prompt is not None:
            async for _ in prompt:  # noqa: B007
                pass
        msg = _StubAssistantMessage(content=[_StubTextBlock(text=response_text)])
        yield msg
        yield _StubResultMessage()
        if raise_after_result:
            raise RuntimeError("simulated SDK teardown crash")

    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "query", _query)
    monkeypatch.setattr(sdk, "ClaudeAgentOptions", _Options)
    # Stubbed message + block types we use in `call`. The real SDK
    # objects have these names — we re-bind the imported names so
    # `isinstance(msg, AssistantMessage)` etc. discriminate against
    # our stubs.
    monkeypatch.setattr(sdk, "AssistantMessage", _StubAssistantMessage)
    monkeypatch.setattr(sdk, "ResultMessage", _StubResultMessage)
    monkeypatch.setattr(sdk, "TextBlock", _StubTextBlock)


# ------------------------------------------------------- call()


@pytest.mark.asyncio
async def test_call_accumulates_text_and_usage(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cold-path: `call` must run, produce the assistant text, and
    surface usage from `ResultMessage.usage`."""
    _patch_sdk(monkeypatch, response_text='{"ok": true, "n": 7}')
    res = await llm.call("system prompt", "user prompt", label="compass:test")
    assert res.text == '{"ok": true, "n": 7}'
    assert res.is_error is False
    assert res.input_tokens == 120
    assert res.output_tokens == 88
    assert res.cache_read_tokens == 64
    assert res.session_id == "sess-x"
    assert res.cost_usd == pytest.approx(0.012)


@pytest.mark.asyncio
async def test_call_writes_turn_ledger_row(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `turns` table should grow by one after a Compass call."""
    from server.db import configured_conn, init_db

    await init_db()
    _patch_sdk(monkeypatch, response_text="ok")
    res = await llm.call("s", "u", label="compass:audit", model="claude-sonnet-4-6")
    assert res.is_error is False

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT agent_id, runtime, cost_basis, model, input_tokens, output_tokens "
            "FROM turns WHERE agent_id = 'compass'"
        )
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["agent_id"] == "compass"
    assert row["runtime"] == "claude"
    assert row["cost_basis"] == "compass:audit"
    assert row["model"] == "claude-sonnet-4-6"
    assert row["input_tokens"] == 120
    assert row["output_tokens"] == 88


@pytest.mark.asyncio
async def test_call_suppresses_post_result_exception(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the SDK raises AFTER ResultMessage, the call must still
    return the assistant text (mirrors the agents.py post-result
    suppression rule for SDK 2.1.12x's noisy teardowns)."""
    _patch_sdk(monkeypatch, response_text="hi", raise_after_result=True)
    res = await llm.call("s", "u")
    assert res.text == "hi"
    assert res.is_error is False  # ResultMessage said clean


@pytest.mark.asyncio
async def test_call_raises_on_error_before_result(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-ResultMessage exception is a real failure — surface it."""

    class _Options:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    async def _query(prompt: Any = None, options: Any = None) -> Any:
        if prompt is not None:
            async for _ in prompt:
                pass
        # Raise without ever yielding a ResultMessage.
        raise RuntimeError("subprocess died")
        yield  # pragma: no cover — keeps this an async generator

    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "query", _query)
    monkeypatch.setattr(sdk, "ClaudeAgentOptions", _Options)
    monkeypatch.setattr(sdk, "AssistantMessage", _StubAssistantMessage)
    monkeypatch.setattr(sdk, "ResultMessage", _StubResultMessage)
    monkeypatch.setattr(sdk, "TextBlock", _StubTextBlock)

    with pytest.raises(llm.CompassLLMError):
        await llm.call("s", "u")


# ------------------------------------------------- parse_json_safe()


def test_parse_json_safe_clean() -> None:
    assert llm.parse_json_safe('{"a": 1}') == {"a": 1}
    assert llm.parse_json_safe("[1, 2, 3]") == [1, 2, 3]


def test_parse_json_safe_strips_json_fence() -> None:
    text = '```json\n{"verdict": "aligned"}\n```'
    assert llm.parse_json_safe(text) == {"verdict": "aligned"}


def test_parse_json_safe_strips_unlabeled_fence() -> None:
    text = "```\n[1, 2]\n```"
    assert llm.parse_json_safe(text) == [1, 2]


def test_parse_json_safe_brace_balance_with_preamble() -> None:
    text = "Sure, here it is:\n{\n  \"verdict\": \"confident_drift\",\n  \"x\": 1\n} okay?"
    assert llm.parse_json_safe(text) == {"verdict": "confident_drift", "x": 1}


def test_parse_json_safe_respects_strings() -> None:
    """A `}` or `]` inside a JSON string must not close the brace."""
    text = '{"text": "this } is fake"}'
    assert llm.parse_json_safe(text) == {"text": "this } is fake"}


def test_parse_json_safe_respects_string_escapes() -> None:
    text = r'{"text": "with \"quotes\" and a } inside"}'
    assert llm.parse_json_safe(text) == {"text": 'with "quotes" and a } inside'}


def test_parse_json_safe_array_top_level_balance() -> None:
    text = "ignore this {\n[1, 2, 3]"  # mismatched preamble — only the array is valid
    # Brace-balance picks the first opener — `{` here — and finds no
    # matching `}`. So this text is hopeless. Confirm we return None
    # rather than truncating.
    assert llm.parse_json_safe(text) is None


def test_parse_json_safe_returns_none_on_garbage() -> None:
    assert llm.parse_json_safe("") is None
    assert llm.parse_json_safe("not json at all") is None
    assert llm.parse_json_safe("{ invalid") is None


# ---------------------------------------------- model + effort resolution


def test_resolve_model_defaults_to_latest_sonnet_concrete() -> None:
    """No param, no env override → falls through to the catalog's
    `latest_sonnet` alias and resolves to a concrete Sonnet id. Must
    not return the alias string itself, and must not return None."""
    from server.compass import config as cmp_config
    from server.compass.llm import _resolve_model

    # Confirm the test environment has no override leaking through.
    if cmp_config.LLM_MODEL_OVERRIDE:
        pytest.skip("HARNESS_COMPASS_MODEL set in env; skipping default test")

    resolved = _resolve_model(None)
    assert resolved is not None
    assert resolved != "latest_sonnet"  # alias was resolved, not passed through
    assert "sonnet" in resolved.lower()


def test_resolve_model_explicit_param_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit `model=` param beats both the env override and the
    default alias. Concrete ids pass through `resolve_model_alias`
    unchanged."""
    from server.compass import config as cmp_config
    from server.compass.llm import _resolve_model

    monkeypatch.setattr(cmp_config, "LLM_MODEL_OVERRIDE", "claude-haiku-4-5-20251001")
    resolved = _resolve_model("claude-opus-4-7")
    assert resolved == "claude-opus-4-7"


def test_resolve_model_env_override_used_when_no_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `HARNESS_COMPASS_MODEL` is set and no param is given, the
    env value wins over the default alias. Aliases in the env are
    resolved through the catalog same as the default."""
    from server.compass import config as cmp_config
    from server.compass.llm import _resolve_model

    monkeypatch.setattr(cmp_config, "LLM_MODEL_OVERRIDE", "latest_opus")
    resolved = _resolve_model(None)
    assert resolved is not None
    # `latest_opus` resolves to a concrete Opus id, not the alias string.
    assert resolved != "latest_opus"
    assert "opus" in resolved.lower()


def test_resolve_effort_returns_medium_by_default() -> None:
    """No env override → `LLM_EFFORT` defaults to `"medium"`, which is
    the agreed-on Compass setting."""
    from server.compass import config as cmp_config
    from server.compass.llm import _resolve_effort

    if cmp_config.LLM_EFFORT != "medium":
        pytest.skip("HARNESS_COMPASS_EFFORT set in env; skipping default test")
    assert _resolve_effort() == "medium"


def test_resolve_effort_accepts_valid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.compass import config as cmp_config
    from server.compass.llm import _resolve_effort

    for level in ("low", "medium", "high", "max"):
        monkeypatch.setattr(cmp_config, "LLM_EFFORT", level)
        assert _resolve_effort() == level


def test_resolve_effort_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage values fall through to None — Compass calls are
    best-effort and shouldn't crash on a typo'd env var."""
    from server.compass import config as cmp_config
    from server.compass.llm import _resolve_effort

    for bad in ("ultra", "MED", "", "  ", "extreme"):
        monkeypatch.setattr(cmp_config, "LLM_EFFORT", bad)
        assert _resolve_effort() is None


@pytest.mark.asyncio
async def test_call_passes_resolved_model_to_options(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`call()` without a `model=` param feeds the resolved default
    (concrete Sonnet id) into `ClaudeAgentOptions(model=...)`."""
    captured: dict[str, Any] = {}

    class _Options:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    async def _query(prompt: Any = None, options: Any = None) -> Any:
        if prompt is not None:
            async for _ in prompt:
                pass
        msg = _StubAssistantMessage(content=[_StubTextBlock(text="hi")])
        yield msg
        yield _StubResultMessage()

    import claude_agent_sdk as sdk

    monkeypatch.setattr(sdk, "query", _query)
    monkeypatch.setattr(sdk, "ClaudeAgentOptions", _Options)
    monkeypatch.setattr(sdk, "AssistantMessage", _StubAssistantMessage)
    monkeypatch.setattr(sdk, "ResultMessage", _StubResultMessage)
    monkeypatch.setattr(sdk, "TextBlock", _StubTextBlock)

    await llm.call("s", "u")
    assert "model" in captured
    assert "sonnet" in str(captured["model"]).lower()


@pytest.mark.asyncio
async def test_call_passes_effort_to_options(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`call()` feeds the resolved effort into
    `ClaudeAgentOptions(effort=...)`. Default is `"medium"`."""
    captured: dict[str, Any] = {}

    class _Options:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    async def _query(prompt: Any = None, options: Any = None) -> Any:
        if prompt is not None:
            async for _ in prompt:
                pass
        yield _StubAssistantMessage(content=[_StubTextBlock(text="ok")])
        yield _StubResultMessage()

    import claude_agent_sdk as sdk
    from server.compass import config as cmp_config

    monkeypatch.setattr(sdk, "query", _query)
    monkeypatch.setattr(sdk, "ClaudeAgentOptions", _Options)
    monkeypatch.setattr(sdk, "AssistantMessage", _StubAssistantMessage)
    monkeypatch.setattr(sdk, "ResultMessage", _StubResultMessage)
    monkeypatch.setattr(sdk, "TextBlock", _StubTextBlock)
    monkeypatch.setattr(cmp_config, "LLM_EFFORT", "high")

    await llm.call("s", "u")
    assert captured.get("effort") == "high"


@pytest.mark.asyncio
async def test_call_omits_effort_when_invalid(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid effort string → no `effort` kwarg in options (SDK uses
    its built-in default rather than failing the call)."""
    captured: dict[str, Any] = {}

    class _Options:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    async def _query(prompt: Any = None, options: Any = None) -> Any:
        if prompt is not None:
            async for _ in prompt:
                pass
        yield _StubAssistantMessage(content=[_StubTextBlock(text="ok")])
        yield _StubResultMessage()

    import claude_agent_sdk as sdk
    from server.compass import config as cmp_config

    monkeypatch.setattr(sdk, "query", _query)
    monkeypatch.setattr(sdk, "ClaudeAgentOptions", _Options)
    monkeypatch.setattr(sdk, "AssistantMessage", _StubAssistantMessage)
    monkeypatch.setattr(sdk, "ResultMessage", _StubResultMessage)
    monkeypatch.setattr(sdk, "TextBlock", _StubTextBlock)
    monkeypatch.setattr(cmp_config, "LLM_EFFORT", "garbage-value")

    await llm.call("s", "u")
    assert "effort" not in captured

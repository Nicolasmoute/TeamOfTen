"""Compass Codex fallback — `compass.llm.call` routes Claude → Codex
when the primary path fails.

Two failure modes trigger fallback:
  - `_call_claude` raises `CompassLLMError` (subprocess died, auth
    gone, rate-limited).
  - `_call_claude` returns a `CompassLLMResult(is_error=True)`.

Per-run latching: inside `begin_run_latch_scope` / `end_run_latch_scope`,
the first failure flips a contextvar and every subsequent `call()`
in that scope skips Claude entirely. Standalone calls (audit watcher)
inherit `latched=False` and retry on Codex per call.

These tests stub both `_call_claude` and the Codex helper on the
`compass.llm` module so no real subprocess is spawned.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.compass import config as cmp_config
from server.compass import llm
from server.compass.llm import (
    CompassLLMError,
    CompassLLMResult,
    begin_run_latch_scope,
    end_run_latch_scope,
    is_fallback_latched,
)


# ----------------------------------------------------------- helpers


def _ok(text: str) -> CompassLLMResult:
    return CompassLLMResult(text=text, is_error=False, cost_usd=0.001)


def _err_result(text: str = "") -> CompassLLMResult:
    return CompassLLMResult(text=text, is_error=True, cost_usd=0.001)


# ---------------------------------------------------------- behavior


@pytest.mark.asyncio
async def test_call_uses_claude_when_primary_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: Claude succeeds → Codex is never called."""
    claude_calls: list[str] = []
    codex_calls: list[str] = []

    async def fake_claude(system: str, user: str, **_: Any) -> CompassLLMResult:
        claude_calls.append(user)
        return _ok("claude-text")

    async def fake_codex(system: str, user: str, **_: Any) -> CompassLLMResult:
        codex_calls.append(user)
        return _ok("codex-text")

    monkeypatch.setattr(llm, "_call_claude", fake_claude)
    monkeypatch.setattr(llm, "_call_codex_via_helper", fake_codex)

    res = await llm.call("s", "u")
    assert res.text == "claude-text"
    assert claude_calls == ["u"]
    assert codex_calls == []
    assert is_fallback_latched() is False


@pytest.mark.asyncio
async def test_call_falls_back_to_codex_on_claude_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude raises CompassLLMError → Codex is invoked + result returned."""
    async def fake_claude(*_: Any, **__: Any) -> CompassLLMResult:
        raise CompassLLMError("subprocess died")

    async def fake_codex(*_: Any, **__: Any) -> CompassLLMResult:
        return _ok("codex-text")

    monkeypatch.setattr(llm, "_call_claude", fake_claude)
    monkeypatch.setattr(llm, "_call_codex_via_helper", fake_codex)

    # Open a run scope so the latch flag survives back to the caller.
    token = begin_run_latch_scope()
    try:
        res = await llm.call("s", "u")
        assert res.text == "codex-text"
        assert is_fallback_latched() is True
    finally:
        end_run_latch_scope(token)
    # After the scope exits, the latch resets to its parent value (False).
    assert is_fallback_latched() is False


@pytest.mark.asyncio
async def test_call_falls_back_to_codex_on_claude_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude returns is_error=True (soft failure) → Codex invoked."""
    async def fake_claude(*_: Any, **__: Any) -> CompassLLMResult:
        return _err_result("partial-claude-text")

    async def fake_codex(*_: Any, **__: Any) -> CompassLLMResult:
        return _ok("codex-text")

    monkeypatch.setattr(llm, "_call_claude", fake_claude)
    monkeypatch.setattr(llm, "_call_codex_via_helper", fake_codex)

    token = begin_run_latch_scope()
    try:
        res = await llm.call("s", "u")
        assert res.text == "codex-text"
        assert res.is_error is False
        assert is_fallback_latched() is True
    finally:
        end_run_latch_scope(token)


@pytest.mark.asyncio
async def test_call_returns_claude_error_when_both_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude returns is_error=True, Codex raises → return Claude's
    error result so the caller still sees usage data."""
    async def fake_claude(*_: Any, **__: Any) -> CompassLLMResult:
        return _err_result("claude-partial")

    async def fake_codex(*_: Any, **__: Any) -> CompassLLMResult:
        raise CompassLLMError("codex auth missing")

    monkeypatch.setattr(llm, "_call_claude", fake_claude)
    monkeypatch.setattr(llm, "_call_codex_via_helper", fake_codex)

    token = begin_run_latch_scope()
    try:
        res = await llm.call("s", "u")
        assert res.is_error is True
        assert res.text == "claude-partial"
    finally:
        end_run_latch_scope(token)


@pytest.mark.asyncio
async def test_call_raises_when_both_fail_via_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude raises, Codex raises → CompassLLMError propagates."""
    async def fake_claude(*_: Any, **__: Any) -> CompassLLMResult:
        raise CompassLLMError("claude died")

    async def fake_codex(*_: Any, **__: Any) -> CompassLLMResult:
        raise CompassLLMError("codex died")

    monkeypatch.setattr(llm, "_call_claude", fake_claude)
    monkeypatch.setattr(llm, "_call_codex_via_helper", fake_codex)

    with pytest.raises(CompassLLMError):
        await llm.call("s", "u")


@pytest.mark.asyncio
async def test_call_disabled_fallback_propagates_claude_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When fallback is disabled, Claude failure surfaces directly —
    no Codex call attempted."""
    codex_calls = 0

    async def fake_claude(*_: Any, **__: Any) -> CompassLLMResult:
        raise CompassLLMError("claude died")

    async def fake_codex(*_: Any, **__: Any) -> CompassLLMResult:
        nonlocal codex_calls
        codex_calls += 1
        return _ok("never reached")

    monkeypatch.setattr(llm, "_call_claude", fake_claude)
    monkeypatch.setattr(llm, "_call_codex_via_helper", fake_codex)
    monkeypatch.setattr(cmp_config, "LLM_FALLBACK_ENABLED", False)

    with pytest.raises(CompassLLMError):
        await llm.call("s", "u")
    assert codex_calls == 0


@pytest.mark.asyncio
async def test_per_run_latch_skips_claude_after_first_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside one run scope: call 1 fails Claude → latched.
    Call 2 in the same scope must go straight to Codex (no Claude
    attempt), confirming the per-run latch saves the wasted retry."""
    claude_calls: list[str] = []
    codex_calls: list[str] = []

    async def fake_claude(system: str, user: str, **_: Any) -> CompassLLMResult:
        claude_calls.append(user)
        raise CompassLLMError("claude rate-limited")

    async def fake_codex(system: str, user: str, **_: Any) -> CompassLLMResult:
        codex_calls.append(user)
        return _ok(f"codex-{user}")

    monkeypatch.setattr(llm, "_call_claude", fake_claude)
    monkeypatch.setattr(llm, "_call_codex_via_helper", fake_codex)

    token = begin_run_latch_scope()
    try:
        r1 = await llm.call("s", "u1")
        r2 = await llm.call("s", "u2")
        r3 = await llm.call("s", "u3")
    finally:
        end_run_latch_scope(token)

    assert r1.text == "codex-u1"
    assert r2.text == "codex-u2"
    assert r3.text == "codex-u3"
    # Claude attempted only on the first call; latch absorbed the rest.
    assert claude_calls == ["u1"]
    assert codex_calls == ["u1", "u2", "u3"]


@pytest.mark.asyncio
async def test_latch_resets_between_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run 1 latches to Codex. Run 2 starts fresh — Claude is tried
    first again. Validates the begin/end scope pair restores the
    contextvar across run boundaries."""
    claude_calls: list[str] = []

    failures = {"u1": True, "u2": False}

    async def fake_claude(system: str, user: str, **_: Any) -> CompassLLMResult:
        claude_calls.append(user)
        if failures.get(user):
            raise CompassLLMError("claude rate-limited")
        return _ok(f"claude-{user}")

    async def fake_codex(system: str, user: str, **_: Any) -> CompassLLMResult:
        return _ok(f"codex-{user}")

    monkeypatch.setattr(llm, "_call_claude", fake_claude)
    monkeypatch.setattr(llm, "_call_codex_via_helper", fake_codex)

    # Run 1: latch to Codex on first failure.
    t1 = begin_run_latch_scope()
    try:
        r1 = await llm.call("s", "u1")
        assert r1.text == "codex-u1"
    finally:
        end_run_latch_scope(t1)

    # Run 2: latch is reset → Claude is attempted first again.
    t2 = begin_run_latch_scope()
    try:
        r2 = await llm.call("s", "u2")
        assert r2.text == "claude-u2"
    finally:
        end_run_latch_scope(t2)

    assert claude_calls == ["u1", "u2"]


@pytest.mark.asyncio
async def test_standalone_call_no_scope_retries_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `begin_run_latch_scope`, each call independently tries
    Claude first (no latching beyond the call). Mirrors the audit
    watcher path which doesn't open a run scope."""
    claude_calls: list[str] = []
    codex_calls: list[str] = []

    async def fake_claude(system: str, user: str, **_: Any) -> CompassLLMResult:
        claude_calls.append(user)
        raise CompassLLMError("claude died")

    async def fake_codex(system: str, user: str, **_: Any) -> CompassLLMResult:
        codex_calls.append(user)
        return _ok(f"codex-{user}")

    monkeypatch.setattr(llm, "_call_claude", fake_claude)
    monkeypatch.setattr(llm, "_call_codex_via_helper", fake_codex)

    # No scope — but the latch flip from call 1 persists in this same
    # contextvar context (the calls share a parent context). This is
    # the trade-off: in production the audit watcher fires each
    # audit_work as its own asyncio.create_task, which gets a copied
    # context, so the latch doesn't carry across audits. Confirm that
    # behavior by spawning each call in a fresh task.
    import asyncio

    async def one_call(text: str) -> CompassLLMResult:
        return await llm.call("s", text)

    r1 = await asyncio.create_task(one_call("u1"))
    r2 = await asyncio.create_task(one_call("u2"))

    assert r1.text == "codex-u1"
    assert r2.text == "codex-u2"
    # Each task got its own copy of the contextvar → Claude tried twice.
    assert claude_calls == ["u1", "u2"]
    assert codex_calls == ["u1", "u2"]


# ------------------------------------------------ codex_llm helpers


def test_resolve_codex_model_default_to_latest_mini_concrete() -> None:
    """No param → falls through to `latest_mini` alias and resolves
    to a concrete Codex mini id. The alias map in models_catalog is
    the single source of truth."""
    from server.compass.codex_llm import _resolve_codex_model

    resolved = _resolve_codex_model(None)
    assert resolved is not None
    assert resolved != "latest_mini"  # alias was resolved
    assert "mini" in resolved.lower()


def test_resolve_codex_model_explicit_param_wins() -> None:
    from server.compass.codex_llm import _resolve_codex_model

    resolved = _resolve_codex_model("gpt-5.4")
    assert resolved == "gpt-5.4"


def test_resolve_codex_effort_default_medium() -> None:
    from server.compass.codex_llm import _resolve_codex_effort

    assert _resolve_codex_effort(None) == "medium"


def test_resolve_codex_effort_invalid_falls_through_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.compass.codex_llm import _resolve_codex_effort

    # Explicit garbage param drops to None.
    assert _resolve_codex_effort("ULTRA") is None
    assert _resolve_codex_effort("bogus") is None


def test_resolve_codex_effort_accepts_valid_values() -> None:
    from server.compass.codex_llm import _resolve_codex_effort

    for level in ("low", "medium", "high", "max"):
        assert _resolve_codex_effort(level) == level

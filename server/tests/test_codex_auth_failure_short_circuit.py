"""Codex auth-failure short-circuit (Stage 2a — t-2026-05-15-0ad86e6d).

When the ChatGPT session in $CODEX_HOME/auth.json expires, every
`get_client` retry pays the same 401 — Codex app-server exits with an
auth-shaped stderr, `_CapturedStdioTransport` raises `CodexTransportError`
("stdio transport closed"), and the harness retries 3× before giving
up. Editing the MCP card from the UI is also a no-op once the cache
has been cleared on the first failure (`evict_all_clients` iterates
`_codex_clients` which has no entry for this slot post-error).

Fix: `_looks_like_codex_auth_error(exc)` keyword detector + branch in
`CodexRuntime.run_turn`'s outer `except Exception` that emits
`human_attention` with actionable steps, suppresses auto-retry by
flipping `got_result=True`, clears `codex_thread_id`, and returns
without re-raising (so the dispatcher's outer suppressed-post-result
warning doesn't add noise on top of the explicit human_attention).

These tests are pure-function tests against the helper plus a focused
integration test that drives `CodexRuntime.run_turn` with a stubbed
client whose `start_thread` raises an auth-shaped exception. They do
not require the real Codex SDK.

See:
  working/knowledge/codex-failure-rootcause-2026-05-15.md (Symptom 3 / Cause B)
  working/knowledge/research/p5-codex-transport-error-investigation-2026-05-14.md (Part 3 + Fix 1)
"""

from __future__ import annotations

from typing import Any

import pytest

from server.runtimes.codex import _looks_like_codex_auth_error


# ---------------------------------------------------------------------
# Pure-function tests for the keyword detector
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        # ChatGPT-session-expired shape
        "stdio transport closed\nprocess exit code: 1\nstderr tail:\n"
        "Error: ChatGPT session expired (401 Unauthorized)",
        # Bare 401 in stderr
        "transport closed: HTTP 401 from openai endpoint",
        # API-key-missing shape
        "no auth credentials configured; set OPENAI_API_KEY or run codex login",
        # Reading auth.json and finding it stale
        "failed to read auth.json: token expired",
        # Case-insensitivity
        "AUTH.JSON IS MISSING — please log in",
        # Common login-prompt phrases
        "please login to codex via `codex login`",
        # OAuth-flavoured wording
        "openai api key not set and chatgpt session unavailable",
        # Token-related substring
        "expired token: refresh required",
        # Authentication generic
        "Authentication failed: 401 Unauthorized",
    ],
)
def test_looks_like_auth_error_positive_cases(msg: str) -> None:
    """Every documented auth-failure stderr shape must trip the detector."""
    assert _looks_like_codex_auth_error(Exception(msg)) is True


@pytest.mark.parametrize(
    "msg",
    [
        # Plain transport blip — no auth keyword
        "stdio transport closed\nprocess exit code: 1\nstderr tail:\n"
        "Error: connection reset by peer",
        # JSON parse failure
        "received invalid JSON from stdio transport: unexpected token",
        # Tool-side error that mentions "fail" but not auth
        "command failed: rm: cannot remove '/tmp/foo': No such file or directory",
        # MCP server returned non-2xx but for an unrelated reason
        "HTTP 500 from coord proxy: internal server error",
        # Empty / generic
        "",
        "ProcessError: Command failed with exit code 1",
        # "Unauthorized" in a tool-side context that isn't ours? Only if
        # the keyword is genuinely absent. (If the agent's tool literally
        # outputs "401 Unauthorized" we'll false-positive — acceptable
        # trade-off since the false-positive triggers human_attention,
        # which is loud but not destructive.)
    ],
)
def test_looks_like_auth_error_negative_cases(msg: str) -> None:
    """Generic transport failures must NOT trip the detector — those
    SHOULD continue to flow through the existing auto-retry path."""
    assert _looks_like_codex_auth_error(Exception(msg)) is False


def test_looks_like_auth_error_handles_subclassed_exception() -> None:
    """Detector reads `str(exc)` so any exception class works — the
    important thing is the message content, not the type. This matters
    because CodexTransportError is the SDK's own exception class and
    we don't want the detector to fail on subclass checks if the SDK
    bumps to a sibling type."""

    class _CustomTransport(Exception):
        pass

    exc = _CustomTransport(
        "stdio transport closed\nstderr tail:\n401 Unauthorized"
    )
    assert _looks_like_codex_auth_error(exc) is True


def test_looks_like_auth_error_no_false_positive_on_word_within_word() -> None:
    """The keywords are substring-matched, but on lowercase'd input —
    confirm a token like "authentic" (substring of "authentication")
    isn't matched against unrelated content. (We DO match
    "authentication" anywhere it appears — accepted: see cancel/reject
    patterns for precedent in `_step_payload_is_error`.)"""
    # The word "authentic" alone is NOT in the keyword list — we only
    # match "authentication" as a complete substring. Confirm a
    # different innocuous string with "authentic" doesn't trip.
    assert _looks_like_codex_auth_error(
        Exception("the authentic representation of the data is...")
    ) is False
    # But "authentication" anywhere DOES trip — that's intentional.
    assert _looks_like_codex_auth_error(
        Exception("X-Authentication-Status: failed")
    ) is True


# ---------------------------------------------------------------------
# Integration test against `CodexRuntime.run_turn`
# ---------------------------------------------------------------------


class _AuthFailingClient:
    """Stand-in for `CodexClient` that raises an auth-shaped exception
    on `start_thread` (the call path inside `run_turn` for a fresh
    thread). Mirrors `_FakeClient` in test_codex_runtime_gate.py but
    is purpose-built for this single integration test."""

    def __init__(self) -> None:
        self.closed = 0
        self._approval = None

    def set_approval_handler(self, h: Any) -> None:
        self._approval = h

    def start_thread(self, config: Any) -> Any:  # noqa: ARG002
        raise RuntimeError(
            "stdio transport closed\nprocess exit code: 1\n"
            "stderr tail:\nError: ChatGPT session expired "
            "(401 Unauthorized) — please run `codex login`"
        )

    def resume_thread(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        raise RuntimeError(
            "stdio transport closed\nprocess exit code: 1\n"
            "stderr tail:\nError: ChatGPT session expired (401)"
        )

    def close(self) -> None:
        self.closed += 1


class _GenericFailClient(_AuthFailingClient):
    """Stand-in that raises a NON-auth transport error so we can verify
    the detector doesn't false-positive: this exception SHOULD NOT
    short-circuit; it should re-raise (existing behaviour)."""

    def start_thread(self, config: Any) -> Any:  # noqa: ARG002
        raise RuntimeError(
            "stdio transport closed\nprocess exit code: 1\n"
            "stderr tail:\nError: connection reset by peer"
        )

    def resume_thread(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        raise RuntimeError("stdio transport closed: connection reset")


async def _drive_run_turn_with_failing_client(
    monkeypatch,
    client: _AuthFailingClient,
    *,
    agent_id: str = "p9",
) -> tuple[Any, Any, list[dict], list[tuple[str, str]], list[str]]:
    """Drive `CodexRuntime.run_turn` against a stubbed client. Returns
    (runtime, tc, events, status_history, clear_thread_calls) so the
    test can assert the right events were emitted, the right status
    flips happened, and the thread-id clear was called.

    Why a mock `server.agents` module instead of import + monkeypatch:
    the real `server.agents` reads `HARNESS_AGENT_DAILY_CAP` at module
    import time and explodes when the env var is set to "" (CI test
    env shape). `test_codex_login.py` solved this by injecting a
    MagicMock under `sys.modules['server.agents']` BEFORE any other
    code can trigger the import. We do the same here so the integration
    test exercises real codex.py code without dragging the agents
    module's import-time side effects in.
    """
    import sys
    import unittest.mock

    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")

    from server.runtimes import codex as codex_mod
    from server.runtimes.base import TurnContext

    captured_events: list[dict] = []
    status_history: list[tuple[str, str]] = []
    clear_thread_calls: list[str] = []

    async def _stub_emit(agent_id: str, event_type: str, **payload) -> None:
        captured_events.append(
            {"agent_id": agent_id, "type": event_type, **payload}
        )

    async def _stub_set_status(agent_id: str, status: str) -> None:
        status_history.append((agent_id, status))

    async def _stub_add_cost(agent_id: str, usd: float) -> None:
        return

    async def _stub_append_exchange(
        agent_id: str, prompt: str, resp: str,
    ) -> None:
        return

    async def _stub_set_continuity_note(agent_id: str, note: Any) -> None:
        return

    async def _stub_insert_turn_row(**kwargs: Any) -> None:
        return

    def _stub_extract_usage_codex(raw: Any) -> dict[str, int]:
        return {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}

    # Inject a mock server.agents module so the late `from server.agents
    # import (...)` inside `run_turn` picks up our stubs without
    # triggering the real module's import-time env reads.
    mock_agents = unittest.mock.MagicMock()
    mock_agents._emit = _stub_emit
    mock_agents._set_status = _stub_set_status
    mock_agents._add_cost = _stub_add_cost
    mock_agents._append_exchange = _stub_append_exchange
    mock_agents._set_continuity_note = _stub_set_continuity_note
    mock_agents._insert_turn_row = _stub_insert_turn_row
    mock_agents._extract_usage_codex = _stub_extract_usage_codex
    mock_agents._now = lambda: "2026-05-15T00:00:00Z"
    monkeypatch.setitem(sys.modules, "server.agents", mock_agents)

    # Mock pricing too — it's a small module but isolating it keeps
    # the test free of any pricing lookups that might evolve.
    mock_pricing = unittest.mock.MagicMock()
    mock_pricing.codex_cost_usd = lambda *a, **kw: 0.0
    monkeypatch.setitem(sys.modules, "server.pricing", mock_pricing)

    async def _stub_resolve_auth() -> tuple[str, dict]:
        return ("chatgpt", {})

    async def _stub_get_client(
        slot: str, *, cwd: str, env_overrides=None,
    ) -> Any:
        return client

    async def _stub_close_client(slot: str) -> None:
        return

    async def _stub_clear_codex_thread_id(slot: str) -> None:
        clear_thread_calls.append(slot)

    async def _stub_get_codex_thread_id(slot: str) -> str | None:
        # Force the no-prior-thread path so `open_thread` goes straight
        # to `start_thread` (we want to test the start_thread auth
        # failure, not the resume_thread auth-heal cascade).
        return None

    async def _stub_set_codex_thread_id(slot: str, thread_id: str | None) -> None:
        return

    class _StubSdk:
        ThreadConfig = None
        TurnOverrides = None
        CodexTimeoutError = type("CodexTimeoutError", (Exception,), {})

    monkeypatch.setattr(codex_mod, "resolve_auth", _stub_resolve_auth)
    monkeypatch.setattr(codex_mod, "get_client", _stub_get_client)
    monkeypatch.setattr(codex_mod, "close_client", _stub_close_client)
    monkeypatch.setattr(
        codex_mod,
        "_clear_codex_thread_id",
        _stub_clear_codex_thread_id,
    )
    monkeypatch.setattr(
        codex_mod,
        "_get_codex_thread_id",
        _stub_get_codex_thread_id,
    )
    monkeypatch.setattr(
        codex_mod,
        "_set_codex_thread_id",
        _stub_set_codex_thread_id,
    )
    monkeypatch.setattr(codex_mod, "_import_codex_sdk", lambda: _StubSdk)

    runtime = codex_mod.CodexRuntime()
    tc = TurnContext(
        agent_id=agent_id,
        project_id="teamoften",
        prompt="test",
        system_prompt="",
        workspace_cwd="/tmp/fake-workspace",
        allowed_tools=[],
        external_mcp_servers={},
        model=None,
        plan_mode=None,
        effort=None,
        compact_mode=False,
        auto_compact=False,
        transfer_to_runtime=None,
        prior_session=None,
    )
    return runtime, tc, captured_events, status_history, clear_thread_calls


@pytest.mark.asyncio
async def test_run_turn_auth_failure_short_circuits(monkeypatch) -> None:
    """An auth-shaped error from `start_thread` triggers the short-
    circuit: human_attention emitted, error event with reason=
    codex_auth_expired, status flipped to error, codex_thread_id
    cleared, got_result=True (suppressing auto-retry), and the
    function returns without re-raising."""
    client = _AuthFailingClient()
    runtime, tc, events, statuses, clears = await _drive_run_turn_with_failing_client(
        monkeypatch, client, agent_id="p9",
    )

    # Should NOT raise — the short-circuit returns cleanly.
    await runtime.run_turn(tc)

    # 1. human_attention was emitted with the auth-expired subject.
    ha_events = [e for e in events if e["type"] == "human_attention"]
    assert len(ha_events) == 1, f"expected exactly one human_attention, got {ha_events}"
    ha = ha_events[0]
    assert "Codex auth expired" in ha["subject"]
    assert ha.get("urgency") == "high"
    assert ha.get("reason") == "codex_auth_expired"
    # Body must include actionable recovery steps.
    body = ha["body"]
    assert "codex login" in body.lower() or "codex auth" in body.lower()
    assert "OPENAI_API_KEY" in body
    assert "session" in body.lower()  # mentions session-clear path
    # Diagnostic excerpt is present so the operator can see what hit.
    assert "401" in body or "expired" in body.lower()

    # 2. error event with reason=codex_auth_expired (single one — the
    # branch returns BEFORE the dispatcher's generic error-emit path).
    err_events = [e for e in events if e["type"] == "error"]
    assert len(err_events) == 1
    assert err_events[0].get("reason") == "codex_auth_expired"
    assert "Codex auth failure" in err_events[0]["error"]

    # 3. status flipped to error.
    assert ("p9", "error") in statuses

    # 4. codex_thread_id was cleared.
    assert clears == ["p9"]

    # 5. got_result=True so the dispatcher's auto-retry policy
    # treats this as "no point retrying."
    assert tc.turn_ctx.get("got_result") is True


@pytest.mark.asyncio
async def test_run_turn_generic_transport_error_does_not_short_circuit(
    monkeypatch,
) -> None:
    """A NON-auth transport error must NOT trip the short-circuit —
    it must re-raise (existing behaviour) so the dispatcher's auto-
    retry policy gets a chance. This guards against the detector
    swallowing legitimate transient failures and hiding them behind
    a misleading 'auth expired' message."""
    client = _GenericFailClient()
    runtime, tc, events, statuses, clears = await _drive_run_turn_with_failing_client(
        monkeypatch, client, agent_id="p10",
    )

    # SHOULD raise — the generic-error path re-raises so the
    # dispatcher catches it and applies auto-retry.
    with pytest.raises(RuntimeError) as excinfo:
        await runtime.run_turn(tc)
    assert "connection reset" in str(excinfo.value).lower()

    # No human_attention emitted (this is a transient error, not an
    # auth failure).
    ha_events = [e for e in events if e["type"] == "human_attention"]
    assert ha_events == []

    # No codex_auth_expired error event (no error event at all from
    # this branch — the dispatcher emits the generic one after
    # re-raise).
    auth_err_events = [
        e for e in events
        if e["type"] == "error" and e.get("reason") == "codex_auth_expired"
    ]
    assert auth_err_events == []

    # codex_thread_id was NOT cleared (auto-heal path in open_thread
    # would have cleared if it were a stale-thread case; here we
    # simulate a transport blip with no thread cleanup).
    assert clears == []

    # got_result was NOT flipped — we want auto-retry to run.
    assert tc.turn_ctx.get("got_result") is None or tc.turn_ctx.get("got_result") is False

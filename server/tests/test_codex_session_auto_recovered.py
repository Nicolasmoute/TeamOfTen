"""Codex memory salvage on stale-thread fall-back (Stage 3a — t-c908723c).

Mirrors Claude's `session_auto_recovered` pattern shipped 2026-05-06
(server/runtimes/claude.py auto-heal). When `client.resume_thread(id)`
fails (any non-cancellation exception, after the timeout-retry budget
is exhausted), the previous behaviour was to emit `session_resume_failed`,
clear the stored `codex_thread_id`, and fall back to `start_thread` on
a blank slate — the agent lost its entire conversational context.

The new salvage path:
1. Reads `agent_sessions.last_exchange_json` (rolling per-turn log
   already populated by every successful non-compact turn).
2. If non-empty: writes a synthetic `continuity_note` so the next
   system prompt's handoff suffix carries the recent exchanges
   verbatim.
3. Rebuilds ThreadConfig with augmented `developer_instructions` so
   the FRESH thread's first turn sees the handoff inline.
4. Sets `tc.turn_ctx["had_handoff_on_entry"] = True` so the post-
   result handler in `run_turn` clears the synthetic note on first
   successful turn (same cleanup as Claude's path).
5. Emits `session_auto_recovered{salvaged_exchanges, runtime="codex"}`.

These tests drive `open_thread` directly with a stub client and
mock-injected `server.agents` module, so they exercise the salvage
flow without needing the real SDK or real DB.

See:
  working/knowledge/codex-failure-rootcause-2026-05-15.md (D1: Memory salvage on Codex stale-thread)
  server/runtimes/claude.py lines ~297–375 (Claude's pattern, mirrored here)
"""

from __future__ import annotations

import sys
import unittest.mock
from typing import Any

import pytest


# ---------------------------------------------------------------------
# Shared fixture — install a mock server.agents BEFORE importing codex.
# Same pattern as test_codex_auth_failure_short_circuit.py: the real
# server.agents module reads HARNESS_AGENT_DAILY_CAP at import time
# and explodes on empty env. Inject a mock to keep the test focused.
# ---------------------------------------------------------------------


@pytest.fixture
def mock_agents_helpers(monkeypatch):
    """Install a mock `server.agents` with the salvage helpers
    (_get_recent_exchanges, _set_continuity_note, _compose_handoff_suffix,
    _emit) and capture every call. Returns a dict of capture lists +
    knobs so each test can assert + drive behaviour."""
    captured_emits: list[dict] = []
    continuity_writes: list[str | None] = []
    compose_calls: list[str] = []
    recent_calls: list[str] = []

    # Knobs (test sets these BEFORE invoking the code under test)
    state = {
        "recent_exchanges": [],  # default: empty (no salvage path)
        "compose_handoff_returns": "## Handoff\nfake handoff body",
    }

    async def _stub_get_recent_exchanges(agent_id: str) -> list[dict]:
        recent_calls.append(agent_id)
        return list(state["recent_exchanges"])

    async def _stub_set_continuity_note(agent_id: str, text) -> None:
        continuity_writes.append(text)

    async def _stub_compose_handoff_suffix(agent_id: str) -> str:
        compose_calls.append(agent_id)
        return state["compose_handoff_returns"]

    async def _stub_emit(agent_id: str, event_type: str, **payload) -> None:
        captured_emits.append(
            {"agent_id": agent_id, "type": event_type, **payload}
        )

    mock_agents = unittest.mock.MagicMock()
    mock_agents._emit = _stub_emit
    mock_agents._get_recent_exchanges = _stub_get_recent_exchanges
    mock_agents._set_continuity_note = _stub_set_continuity_note
    mock_agents._compose_handoff_suffix = _stub_compose_handoff_suffix
    monkeypatch.setitem(sys.modules, "server.agents", mock_agents)

    return {
        "emits": captured_emits,
        "continuity_writes": continuity_writes,
        "compose_calls": compose_calls,
        "recent_calls": recent_calls,
        "state": state,
    }


class _ResumeFailingClient:
    """Stub Codex client whose `resume_thread` raises (simulating a
    stale thread / network blip / backend restart). `start_thread`
    succeeds and returns a sentinel. Records every call for assertion."""

    def __init__(self) -> None:
        self.start_calls: list[Any] = []
        self.resume_calls: list[tuple[str, Any]] = []
        self._fresh_handle = unittest.mock.MagicMock(name="fresh_thread")
        self._fresh_handle.thread_id = "fresh-thread-id-99"

    def resume_thread(self, thread_id: str, *, overrides: Any = None) -> Any:
        self.resume_calls.append((thread_id, overrides))
        raise RuntimeError(
            "thread/resume failed: stale thread id (simulated)"
        )

    def start_thread(self, config: Any) -> Any:
        self.start_calls.append(config)
        return self._fresh_handle


# ---------------------------------------------------------------------
# Salvage-path tests
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_thread_salvages_when_tc_provided_and_log_nonempty(
    monkeypatch, mock_agents_helpers,
) -> None:
    """Happy path: tc provided, exchange log has 3 entries → synthetic
    continuity_note written, handoff suffix composed, ThreadConfig
    rebuilt with augmented developer_instructions, had_handoff_on_entry
    flag set, session_auto_recovered emitted with the right payload."""
    mock_agents_helpers["state"]["recent_exchanges"] = [
        {"prompt": "x1", "response": "y1"},
        {"prompt": "x2", "response": "y2"},
        {"prompt": "x3", "response": "y3"},
    ]

    from server.runtimes import codex as codex_mod
    from server.runtimes.base import TurnContext

    # Stub the thread-id read so the resume path runs.
    async def _stub_get(slot: str) -> str | None:
        return "stale-thread-id-1"

    async def _stub_clear(slot: str) -> None:
        return

    monkeypatch.setattr(codex_mod, "_get_codex_thread_id", _stub_get)
    monkeypatch.setattr(codex_mod, "_clear_codex_thread_id", _stub_clear)

    # Stub SDK so resume's timeout-retry doesn't fire (no Timeout class
    # match) and ThreadConfig rebuild produces a dict.
    class _StubSdk:
        ThreadConfig = None
        CodexTimeoutError = type("CodexTimeoutError", (Exception,), {})

    monkeypatch.setattr(codex_mod, "_import_codex_sdk", lambda: _StubSdk)

    client = _ResumeFailingClient()
    tc = TurnContext(
        agent_id="p9",
        project_id="teamoften",
        prompt="next prompt after recovery",
        system_prompt="ORIGINAL SYSTEM PROMPT BODY",
        workspace_cwd="/tmp/fake",
        allowed_tools=[],
        external_mcp_servers={},
        model="gpt-5.5",
        plan_mode=None,
        effort=None,
        compact_mode=False,
        auto_compact=False,
        transfer_to_runtime=None,
        prior_session=None,
    )

    original_config = {"sentinel": "original-config"}
    handle, resumed = await codex_mod.open_thread(
        "p9", client, config=original_config, tc=tc,
    )

    # Returned the fresh handle from start_thread (not resume).
    assert handle is client._fresh_handle
    assert resumed is False
    assert len(client.resume_calls) == 1
    assert len(client.start_calls) == 1

    # Salvage helpers were called against the right slot.
    assert mock_agents_helpers["recent_calls"] == ["p9"]
    assert mock_agents_helpers["compose_calls"] == ["p9"]

    # Synthetic continuity_note was written with the documented prose.
    assert len(mock_agents_helpers["continuity_writes"]) == 1
    note = mock_agents_helpers["continuity_writes"][0]
    assert "Codex thread was reset" in note
    assert "verbatim" in note

    # had_handoff_on_entry flag was set on tc.turn_ctx — the post-
    # result handler in run_turn (codex.py ~line 1862) reads this to
    # clear the synthetic note on first successful turn.
    assert tc.turn_ctx.get("had_handoff_on_entry") is True

    # The config passed to start_thread is NOT the original — it was
    # rebuilt with augmented developer_instructions.
    start_config = client.start_calls[0]
    assert start_config != original_config
    # Developer instructions should contain the augmented prompt
    # (original + handoff suffix).
    assert isinstance(start_config, dict)  # ThreadConfig is None in stub
    dev_instructions = start_config.get("developer_instructions", "")
    assert "ORIGINAL SYSTEM PROMPT BODY" in dev_instructions
    assert "fake handoff body" in dev_instructions

    # session_auto_recovered emitted with the right payload.
    auto_recovered = [
        e for e in mock_agents_helpers["emits"]
        if e["type"] == "session_auto_recovered"
    ]
    assert len(auto_recovered) == 1
    assert auto_recovered[0]["salvaged_exchanges"] == 3
    assert auto_recovered[0]["runtime"] == "codex"

    # session_resume_failed also emitted (preserved from the existing
    # auto-heal path — both events fire on a salvaged failure).
    resume_failed = [
        e for e in mock_agents_helpers["emits"]
        if e["type"] == "session_resume_failed"
    ]
    assert len(resume_failed) == 1


@pytest.mark.asyncio
async def test_open_thread_no_salvage_when_tc_is_none(
    monkeypatch, mock_agents_helpers,
) -> None:
    """Backwards-compat: legacy callers / tests don't pass tc. The
    salvage path must be a no-op for them — same observable behaviour
    as before Stage 3a (session_resume_failed + clear + start_thread,
    no continuity_note, no session_auto_recovered)."""
    mock_agents_helpers["state"]["recent_exchanges"] = [
        {"prompt": "x", "response": "y"}
    ]

    from server.runtimes import codex as codex_mod

    async def _stub_get(slot: str) -> str | None:
        return "stale-thread-id-2"

    async def _stub_clear(slot: str) -> None:
        return

    monkeypatch.setattr(codex_mod, "_get_codex_thread_id", _stub_get)
    monkeypatch.setattr(codex_mod, "_clear_codex_thread_id", _stub_clear)

    class _StubSdk:
        ThreadConfig = None
        CodexTimeoutError = type("CodexTimeoutError", (Exception,), {})

    monkeypatch.setattr(codex_mod, "_import_codex_sdk", lambda: _StubSdk)

    client = _ResumeFailingClient()
    handle, resumed = await codex_mod.open_thread(
        "p9", client, config={"sentinel": "x"},
        # tc=None implicit
    )

    # start_thread used the ORIGINAL config (no rebuild).
    assert handle is client._fresh_handle
    assert resumed is False
    assert client.start_calls[0] == {"sentinel": "x"}

    # NO continuity write, NO handoff compose, NO session_auto_recovered.
    assert mock_agents_helpers["continuity_writes"] == []
    assert mock_agents_helpers["compose_calls"] == []
    auto_recovered = [
        e for e in mock_agents_helpers["emits"]
        if e["type"] == "session_auto_recovered"
    ]
    assert auto_recovered == []
    # session_resume_failed still emitted (preserved behaviour).
    resume_failed = [
        e for e in mock_agents_helpers["emits"]
        if e["type"] == "session_resume_failed"
    ]
    assert len(resume_failed) == 1


@pytest.mark.asyncio
async def test_open_thread_no_salvage_when_log_empty(
    monkeypatch, mock_agents_helpers,
) -> None:
    """Empty exchange log → no salvage attempted (no continuity write,
    no handoff compose, no auto_recovered emit). The original config
    is passed through to start_thread unchanged. This covers fresh
    agents that hit a stale-thread error before any successful turn
    populated the rolling log."""
    mock_agents_helpers["state"]["recent_exchanges"] = []  # empty log

    from server.runtimes import codex as codex_mod
    from server.runtimes.base import TurnContext

    async def _stub_get(slot: str) -> str | None:
        return "stale-thread-id-3"

    async def _stub_clear(slot: str) -> None:
        return

    monkeypatch.setattr(codex_mod, "_get_codex_thread_id", _stub_get)
    monkeypatch.setattr(codex_mod, "_clear_codex_thread_id", _stub_clear)

    class _StubSdk:
        ThreadConfig = None
        CodexTimeoutError = type("CodexTimeoutError", (Exception,), {})

    monkeypatch.setattr(codex_mod, "_import_codex_sdk", lambda: _StubSdk)

    client = _ResumeFailingClient()
    tc = TurnContext(
        agent_id="p9",
        project_id="teamoften",
        prompt="x",
        system_prompt="SP",
        workspace_cwd="/tmp",
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

    original_config = {"sentinel": "original"}
    handle, resumed = await codex_mod.open_thread(
        "p9", client, config=original_config, tc=tc,
    )

    # Recent log was checked (so we know there's nothing to salvage).
    assert mock_agents_helpers["recent_calls"] == ["p9"]
    # But nothing else fired.
    assert mock_agents_helpers["continuity_writes"] == []
    assert mock_agents_helpers["compose_calls"] == []
    assert tc.turn_ctx.get("had_handoff_on_entry") is None
    assert client.start_calls[0] is original_config
    auto_recovered = [
        e for e in mock_agents_helpers["emits"]
        if e["type"] == "session_auto_recovered"
    ]
    assert auto_recovered == []


@pytest.mark.asyncio
async def test_open_thread_salvage_robust_to_recent_exchanges_failure(
    monkeypatch, mock_agents_helpers,
) -> None:
    """If `_get_recent_exchanges` raises (DB error, transient I/O),
    the salvage helper must degrade gracefully: log the exception,
    return the original config, do NOT call _set_continuity_note,
    do NOT emit session_auto_recovered, do NOT flip
    had_handoff_on_entry. The fall-back to start_thread still
    happens — better to start blind than crash the spawn."""
    from server.runtimes import codex as codex_mod
    from server.runtimes.base import TurnContext

    async def _failing_recent(agent_id: str) -> list[dict]:
        raise RuntimeError("DB read failed (simulated)")

    # Override the helper on the mock module so the real call raises.
    sys.modules["server.agents"]._get_recent_exchanges = _failing_recent

    async def _stub_get(slot: str) -> str | None:
        return "stale-thread-id-4"

    async def _stub_clear(slot: str) -> None:
        return

    monkeypatch.setattr(codex_mod, "_get_codex_thread_id", _stub_get)
    monkeypatch.setattr(codex_mod, "_clear_codex_thread_id", _stub_clear)

    class _StubSdk:
        ThreadConfig = None
        CodexTimeoutError = type("CodexTimeoutError", (Exception,), {})

    monkeypatch.setattr(codex_mod, "_import_codex_sdk", lambda: _StubSdk)

    client = _ResumeFailingClient()
    tc = TurnContext(
        agent_id="p9",
        project_id="teamoften",
        prompt="x",
        system_prompt="SP",
        workspace_cwd="/tmp",
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

    original_config = {"sentinel": "fallback"}
    handle, resumed = await codex_mod.open_thread(
        "p9", client, config=original_config, tc=tc,
    )

    # Original config flows through; no salvage side effects.
    assert client.start_calls[0] is original_config
    assert mock_agents_helpers["continuity_writes"] == []
    assert tc.turn_ctx.get("had_handoff_on_entry") is None
    auto_recovered = [
        e for e in mock_agents_helpers["emits"]
        if e["type"] == "session_auto_recovered"
    ]
    assert auto_recovered == []
    # But session_resume_failed STILL fires (the existing auto-heal
    # behaviour is preserved even when salvage is degraded).
    resume_failed = [
        e for e in mock_agents_helpers["emits"]
        if e["type"] == "session_resume_failed"
    ]
    assert len(resume_failed) == 1


@pytest.mark.asyncio
async def test_open_thread_salvage_robust_to_continuity_write_failure(
    monkeypatch, mock_agents_helpers,
) -> None:
    """If writing the synthetic continuity_note fails, salvage must
    NOT flip had_handoff_on_entry (the next turn would consume a
    note that was never written) and must NOT rebuild the config
    (the augmented dev_instructions would reference a handoff suffix
    we couldn't produce). Original config flows through; fall-back
    to start_thread still happens."""
    mock_agents_helpers["state"]["recent_exchanges"] = [
        {"prompt": "x", "response": "y"}
    ]

    async def _failing_set_note(agent_id: str, text) -> None:
        raise RuntimeError("DB write failed (simulated)")

    sys.modules["server.agents"]._set_continuity_note = _failing_set_note

    from server.runtimes import codex as codex_mod
    from server.runtimes.base import TurnContext

    async def _stub_get(slot: str) -> str | None:
        return "stale-thread-id-5"

    async def _stub_clear(slot: str) -> None:
        return

    monkeypatch.setattr(codex_mod, "_get_codex_thread_id", _stub_get)
    monkeypatch.setattr(codex_mod, "_clear_codex_thread_id", _stub_clear)

    class _StubSdk:
        ThreadConfig = None
        CodexTimeoutError = type("CodexTimeoutError", (Exception,), {})

    monkeypatch.setattr(codex_mod, "_import_codex_sdk", lambda: _StubSdk)

    client = _ResumeFailingClient()
    tc = TurnContext(
        agent_id="p9",
        project_id="teamoften",
        prompt="x",
        system_prompt="SP",
        workspace_cwd="/tmp",
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

    original_config = {"sentinel": "fallback2"}
    await codex_mod.open_thread(
        "p9", client, config=original_config, tc=tc,
    )

    # Original config used (rebuild aborted on continuity write fail).
    assert client.start_calls[0] is original_config
    assert tc.turn_ctx.get("had_handoff_on_entry") is None
    auto_recovered = [
        e for e in mock_agents_helpers["emits"]
        if e["type"] == "session_auto_recovered"
    ]
    assert auto_recovered == []


@pytest.mark.asyncio
async def test_open_thread_no_salvage_on_successful_resume(
    monkeypatch, mock_agents_helpers,
) -> None:
    """Resume succeeds → salvage path is never entered. Returns the
    resumed handle with resumed=True. No salvage helpers called, no
    new events emitted by the salvage path."""
    mock_agents_helpers["state"]["recent_exchanges"] = [
        {"prompt": "x", "response": "y"}
    ]

    from server.runtimes import codex as codex_mod
    from server.runtimes.base import TurnContext

    class _ResumingClient:
        def __init__(self) -> None:
            self._handle = unittest.mock.MagicMock(name="resumed_thread")
            self._handle.thread_id = "resumed-thread-id"

        def resume_thread(self, thread_id, *, overrides=None) -> Any:
            return self._handle

        def start_thread(self, config) -> Any:
            raise AssertionError("start_thread must not be called on success")

    async def _stub_get(slot: str) -> str | None:
        return "valid-thread-id"

    monkeypatch.setattr(codex_mod, "_get_codex_thread_id", _stub_get)

    class _StubSdk:
        ThreadConfig = None
        CodexTimeoutError = type("CodexTimeoutError", (Exception,), {})

    monkeypatch.setattr(codex_mod, "_import_codex_sdk", lambda: _StubSdk)

    client = _ResumingClient()
    tc = TurnContext(
        agent_id="p9",
        project_id="teamoften",
        prompt="x",
        system_prompt="SP",
        workspace_cwd="/tmp",
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

    handle, resumed = await codex_mod.open_thread(
        "p9", client, config={"sentinel": "x"}, tc=tc,
    )
    assert handle is client._handle
    assert resumed is True

    # No salvage side effects.
    assert mock_agents_helpers["recent_calls"] == []
    assert mock_agents_helpers["continuity_writes"] == []
    assert tc.turn_ctx.get("had_handoff_on_entry") is None
    auto_recovered = [
        e for e in mock_agents_helpers["emits"]
        if e["type"] == "session_auto_recovered"
    ]
    assert auto_recovered == []

"""Memory salvage on stale-session auto-heal + workspace pre-flight.

The 2026-05-06 incident: Coach had its session_id cleared by the
ClaudeRuntime auto-heal after a ProcessError on resume. The cleared
session left Coach with no continuity, even though
`agent_sessions.last_exchange_json` (the rolling per-turn log) was
intact — the system-prompt builder gates the handoff injection on
`continuity_note`, which only `/compact` writes today. These tests
pin the new behavior:

  1. `_compose_handoff_suffix` — extracted helper used both by the
     normal post-compact handoff path (run_agent line ~4480) and by
     the auto-heal synthetic-note path. Returns "" without a
     continuity_note; otherwise renders the existing handoff format
     including last_exchange_json verbatim.

  2. `run_agent` workspace pre-flight — refuses to spawn when the
     workspace dir doesn't exist (workspace_dir's mkdir failed).
     Bailing here keeps the SDK from raising CLIConnectionError
     which on retry has historically escalated to a ProcessError on
     resume and triggered the stale-session auto-heal — silently
     nuking the session_id even though the underlying problem was
     a /data volume mount issue.

The integrated auto-heal-writes-synthetic-note flow lives in
`server/runtimes/claude.py:run_turn`; testing it requires SDK
mocking that the wider suite avoids. The three pieces are tested
in isolation here so the contract is locked even if the runtime's
internal layout shifts.
"""

from __future__ import annotations

import json

import pytest

from server.db import configured_conn, init_db, resolve_active_project


@pytest.fixture(autouse=True)
async def _init(fresh_db: str) -> None:
    await init_db()


async def _seed_session_row(
    slot: str,
    *,
    continuity_note: str | None = None,
    last_exchange_json: str | None = None,
) -> None:
    pid = await resolve_active_project()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO agent_sessions (slot, project_id, "
            "continuity_note, last_exchange_json) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(slot, project_id) DO UPDATE SET "
            "continuity_note = excluded.continuity_note, "
            "last_exchange_json = excluded.last_exchange_json",
            (slot, pid, continuity_note, last_exchange_json),
        )
        await c.commit()
    finally:
        await c.close()


# ---------- _compose_handoff_suffix ----------


async def test_compose_handoff_returns_empty_when_no_continuity_note() -> None:
    from server.agents import _compose_handoff_suffix
    assert await _compose_handoff_suffix("p3") == ""


async def test_compose_handoff_returns_empty_when_note_is_blank() -> None:
    from server.agents import _compose_handoff_suffix
    await _seed_session_row("p3", continuity_note="")
    assert await _compose_handoff_suffix("p3") == ""


async def test_compose_handoff_includes_note_only_when_no_recent_exchanges() -> None:
    from server.agents import _compose_handoff_suffix
    await _seed_session_row("p3", continuity_note="Worked on auth refactor.")
    suffix = await _compose_handoff_suffix("p3")
    assert "## Handoff from your prior session" in suffix
    assert "Worked on auth refactor." in suffix
    # Without exchanges, the "Recent exchanges" sub-header is omitted.
    assert "### Recent exchanges" not in suffix


async def test_compose_handoff_inlines_recent_exchanges_verbatim() -> None:
    from server.agents import _compose_handoff_suffix
    exchanges = [
        {"prompt": "Check inbox", "response": "3 unread; replied to t-1"},
        {"prompt": "Run tests",   "response": "112/112 passing"},
    ]
    await _seed_session_row(
        "p3",
        continuity_note="Session reset; below are your last turns.",
        last_exchange_json=json.dumps(exchanges),
    )
    suffix = await _compose_handoff_suffix("p3")
    assert "Session reset" in suffix
    assert "### Recent exchanges (verbatim, last 2 turns" in suffix
    # Both prompts and responses appear verbatim.
    assert "Check inbox" in suffix
    assert "3 unread; replied to t-1" in suffix
    assert "Run tests" in suffix
    assert "112/112 passing" in suffix
    # Order preserved: oldest first.
    assert suffix.index("Check inbox") < suffix.index("Run tests")


async def test_compose_handoff_drops_malformed_exchange_entries() -> None:
    from server.agents import _compose_handoff_suffix
    # Mix of valid + malformed; the helper must filter without crashing.
    exchanges = [
        {"prompt": "Real prompt", "response": "Real response"},
        {"prompt": None, "response": "no prompt"},        # bad: prompt not str
        {"prompt": "no response"},                          # bad: missing key
        {"prompt": "", "response": ""},                    # bad: both empty
        "not even a dict",                                  # bad: not a dict
        {"prompt": "Final prompt", "response": "Final response"},
    ]
    await _seed_session_row(
        "p3",
        continuity_note="anything",
        last_exchange_json=json.dumps(exchanges),
    )
    suffix = await _compose_handoff_suffix("p3")
    # Two valid exchanges remain.
    assert "last 2 turns" in suffix
    assert "Real prompt" in suffix
    assert "Final response" in suffix
    # Malformed entries dropped silently.
    assert "no prompt" not in suffix


async def test_compose_handoff_uses_singular_when_one_exchange() -> None:
    from server.agents import _compose_handoff_suffix
    exchanges = [{"prompt": "p", "response": "r"}]
    await _seed_session_row(
        "p3",
        continuity_note="x",
        last_exchange_json=json.dumps(exchanges),
    )
    suffix = await _compose_handoff_suffix("p3")
    assert "last 1 turn before compact" in suffix
    assert "1 turns" not in suffix  # no plural-s when count is 1


# ---------- run_agent workspace pre-flight ----------


async def test_run_agent_workspace_missing_emits_error_and_human_attention(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """When workspace_dir returns a path that doesn't exist (mkdir failed
    silently, e.g. /data not mounted), run_agent must NOT call the
    runtime — it emits a clear `error` event with reason='workspace_missing',
    raises a `human_attention`, and returns. Skipping this guard is what
    let CLIConnectionError cascade into ProcessError → auto-heal nuking
    session_id during the 2026-05-06 incident.
    """
    import server.agents as agentsmod
    import server.runtimes as runtimes_mod
    from server.events import bus

    # Force workspace_dir to return a path that doesn't exist. parents=True
    # would normally create it; mocking around the helper simulates the
    # mkdir-failed-silently case.
    bogus = tmp_path / "nonexistent" / "deeper"

    async def fake_workspace_dir(_slot: str):
        return bogus

    monkeypatch.setattr(agentsmod, "workspace_dir", fake_workspace_dir)

    runtime_calls: list[str] = []

    class _Runtime:
        name = "claude"

        async def maybe_auto_compact(self, tc):
            runtime_calls.append("maybe_auto_compact")
            return False

        async def prepare_turn_start(self, tc):
            runtime_calls.append("prepare_turn_start")
            return False

        async def run_turn(self, tc):
            runtime_calls.append("run_turn")
            tc.turn_ctx["got_result"] = True

        async def run_manual_compact(self, tc):
            runtime_calls.append("run_manual_compact")

    monkeypatch.setattr(runtimes_mod, "get_runtime", lambda name: _Runtime())

    q = bus.subscribe()
    try:
        await agentsmod.run_agent("p4", "test prompt")
        seen: list[dict] = []
        # Drain whatever's queued; we only care about a few event types.
        while True:
            try:
                seen.append(q.get_nowait())
            except Exception:
                break
    finally:
        bus.unsubscribe(q)

    # No agent_started event — we bailed before flipping status.
    assert not any(e.get("type") == "agent_started" for e in seen), (
        "run_agent must NOT emit agent_started when the workspace is missing"
    )
    # The runtime is never invoked.
    assert "run_turn" not in runtime_calls
    assert "prepare_turn_start" not in runtime_calls

    error_events = [e for e in seen if e.get("type") == "error"]
    assert len(error_events) == 1
    err = error_events[0]
    assert err.get("reason") == "workspace_missing"
    assert "POST /api/projects/" in (err.get("error") or "")
    assert "/repo/provision" in (err.get("error") or "")
    assert err.get("agent_id") == "p4"

    human_events = [e for e in seen if e.get("type") == "human_attention"]
    assert len(human_events) == 1
    assert "workspace dir missing" in (human_events[0].get("subject") or "")

    # agent_stopped fires so the timeline closes cleanly.
    assert any(e.get("type") == "agent_stopped" for e in seen)


async def test_run_agent_workspace_present_proceeds_to_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """Sanity: when workspace_dir returns an existing path, run_agent
    proceeds to spawn through the runtime. Pin the happy path so a
    future change to the pre-flight doesn't accidentally short-circuit
    every spawn."""
    import server.agents as agentsmod
    import server.runtimes as runtimes_mod
    from server.events import bus

    real_dir = tmp_path / "real"
    real_dir.mkdir(parents=True)

    async def fake_workspace_dir(_slot: str):
        return real_dir

    monkeypatch.setattr(agentsmod, "workspace_dir", fake_workspace_dir)

    runtime_calls: list[str] = []

    class _Runtime:
        name = "claude"

        async def maybe_auto_compact(self, tc):
            return False

        async def prepare_turn_start(self, tc):
            return False

        async def run_turn(self, tc):
            runtime_calls.append("run_turn")
            tc.turn_ctx["got_result"] = True

        async def run_manual_compact(self, tc):
            tc.turn_ctx["got_result"] = True

    monkeypatch.setattr(runtimes_mod, "get_runtime", lambda name: _Runtime())

    q = bus.subscribe()
    try:
        await agentsmod.run_agent("p4", "test prompt")
        seen: list[dict] = []
        while True:
            try:
                seen.append(q.get_nowait())
            except Exception:
                break
    finally:
        bus.unsubscribe(q)

    assert "run_turn" in runtime_calls
    assert any(e.get("type") == "agent_started" for e in seen)
    # No workspace_missing error event.
    assert not any(
        e.get("type") == "error" and e.get("reason") == "workspace_missing"
        for e in seen
    )


# ---------- auto-heal synthetic continuity_note (integration shape) ----------


async def test_synthetic_continuity_note_preserves_last_exchange_log() -> None:
    """The auto-heal path writes a synthetic continuity_note and does NOT
    clear `last_exchange_json` (only `/compact` does that, via
    `_clear_exchange_log`). This invariant is what makes the synthetic
    note useful: it gates the existing handoff injection over the
    rolling exchange log without losing the log itself.
    """
    from server.agents import (
        _compose_handoff_suffix,
        _get_recent_exchanges,
        _set_continuity_note,
    )

    exchanges = [
        {"prompt": "First", "response": "First reply"},
        {"prompt": "Second", "response": "Second reply"},
    ]
    await _seed_session_row(
        "coach",
        continuity_note=None,
        last_exchange_json=json.dumps(exchanges),
    )
    # Sanity: no handoff yet.
    assert await _compose_handoff_suffix("coach") == ""

    # Auto-heal writes the synthetic note.
    await _set_continuity_note(
        "coach",
        "Your prior session was reset by the harness because resume "
        "failed (ProcessError on resume — typically a stale CLI "
        "session). The verbatim exchanges below are your only memory "
        "of the prior conversation; pick up from there.",
    )

    # The exchange log is intact.
    recent = await _get_recent_exchanges("coach")
    assert len(recent) == 2

    # And the handoff suffix now renders both.
    suffix = await _compose_handoff_suffix("coach")
    assert "## Handoff from your prior session" in suffix
    assert "ProcessError" in suffix
    assert "First reply" in suffix
    assert "Second reply" in suffix

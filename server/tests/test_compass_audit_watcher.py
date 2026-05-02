"""Tests for `server.compass.audit_watcher`.

The watcher is the auto-audit substrate: it subscribes to the bus,
filters for artifact events (`commit_pushed` / `decision_written` /
`knowledge_written`), and dispatches `audit_work` for each — gated on
the per-project enable flag, the team daily cost cap, and a per-
(project, agent, type) debounce window.

What we cover here:
  - Watched event types fire `audit_work`; unrelated events don't.
  - Per-project enable flag short-circuits before any LLM gets called.
  - Debounce drops back-to-back same-(project, agent, type) events.
  - Cost-cap gate prevents firing when the team daily cap is hit.
  - Artifact composition produces a meaningful blob per event flavor.
  - `start` is idempotent and respects the global feature flag.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from server.compass import audit as cmp_audit
from server.compass import audit_watcher as watcher
from server.compass import config as cmp_config
from server.db import configured_conn, init_db
from server.events import bus


# ----------------------------------------------------- helpers


async def _set_team_config(key: str, value: str) -> None:
    """Mirror of `agents.set_team_config` shape — there's no public
    helper in `server.db`, every call site does the same upsert."""
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO team_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await c.commit()
    finally:
        await c.close()


async def _enable_compass(project_id: str) -> None:
    await _set_team_config(cmp_config.enabled_key(project_id), "true")


async def _disable_compass(project_id: str) -> None:
    await _set_team_config(cmp_config.enabled_key(project_id), "false")


def _stub_audit_work(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace `cmp_audit.audit_work` with a recorder. Returns the
    list it appends to — `(project_id, artifact)` per call."""
    invocations: list[tuple[str, str]] = []

    async def _fake(project_id: str, artifact: str) -> dict[str, Any]:
        invocations.append((project_id, artifact))
        return {"verdict": "aligned", "summary": "stub"}

    monkeypatch.setattr(cmp_audit, "audit_work", _fake)
    return invocations


async def _wait_for(predicate, timeout: float = 1.0, step: float = 0.01) -> bool:
    """Poll-loop helper: spin the event loop briefly while waiting for
    the watcher's background task to drain a published event. The
    background task drains within microseconds in practice; we cap at
    `timeout` so a stuck condition fails the test instead of hanging."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


# ----------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
async def _isolate_watcher(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> Any:
    """Bring up a fresh DB and reset the watcher's per-process state
    around every test. Yields after the test so we can stop the
    watcher cleanly even on failure paths."""
    await init_db()
    monkeypatch.setattr(cmp_config, "AUTO_AUDIT_ENABLED", True)
    monkeypatch.setattr(cmp_config, "AUTO_AUDIT_DEBOUNCE_SECONDS", 30)
    # Disable the cost cap by default — individual tests can flip it on.
    from server import agents as agents_mod

    monkeypatch.setattr(agents_mod, "TEAM_DAILY_CAP_USD", 0.0)
    yield
    await watcher.stop_audit_watcher()


# ----------------------------------------------------- tests


@pytest.mark.asyncio
async def test_commit_pushed_triggers_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `commit_pushed` event on an enabled project fires `audit_work`
    with a composed artifact string."""
    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "commit_pushed",
        "sha": "abc123",
        "message": "implement per-task billing",
        "pushed": True,
        "project_id": "misc",
    })
    assert await _wait_for(lambda: len(calls) == 1)
    project_id, artifact = calls[0]
    assert project_id == "misc"
    assert "[commit]" in artifact
    assert "abc123" in artifact
    assert "per-task billing" in artifact


@pytest.mark.asyncio
async def test_decision_written_triggers_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "coach",
        "type": "decision_written",
        "title": "switch to per-task billing",
        "filename": "2026-05-02-switch-billing.md",
        "size": 800,
        "project_id": "misc",
    })
    assert await _wait_for(lambda: len(calls) == 1)
    _, artifact = calls[0]
    assert "[decision]" in artifact
    assert "switch to per-task billing" in artifact


@pytest.mark.asyncio
async def test_knowledge_written_triggers_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p2",
        "type": "knowledge_written",
        "path": "research/competitor-pricing.md",
        "size": 4200,
        "project_id": "misc",
    })
    assert await _wait_for(lambda: len(calls) == 1)
    _, artifact = calls[0]
    assert "[knowledge]" in artifact
    assert "competitor-pricing.md" in artifact


@pytest.mark.asyncio
async def test_output_saved_text_format_includes_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`output_saved` for a text-native format reads the file body and
    folds it into the audit artifact (Tier B per compass-specs §5.5).
    The audit prompt should see the actual document content."""
    from server import outputs as outmod

    monkeypatch.setattr(outmod, "OUTPUTS_DIR", tmp_path)
    f = tmp_path / "report.md"
    f.write_text(
        "# Pricing analysis\n\nPer-task billing is the recommended model.",
        encoding="utf-8",
    )

    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "output_saved",
        "path": "report.md",
        "bytes": 80,
        "project_id": "misc",
    })
    assert await _wait_for(lambda: len(calls) == 1)
    _, artifact = calls[0]
    assert "[output]" in artifact
    assert "report.md" in artifact
    assert "Per-task billing" in artifact  # body extracted
    assert "document body" in artifact  # body separator marker


@pytest.mark.asyncio
async def test_output_saved_image_falls_back_to_path_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Images are skipped for body extraction (Tier C / vision deferred)
    — the audit fires with path + size only, no body separator."""
    from server import outputs as outmod

    monkeypatch.setattr(outmod, "OUTPUTS_DIR", tmp_path)
    f = tmp_path / "chart.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")

    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "output_saved",
        "path": "chart.png",
        "bytes": 6,
        "project_id": "misc",
    })
    assert await _wait_for(lambda: len(calls) == 1)
    _, artifact = calls[0]
    assert "[output]" in artifact
    assert "chart.png" in artifact
    assert "document body" not in artifact  # path-only, no body separator


@pytest.mark.asyncio
async def test_output_saved_missing_file_falls_back_to_path_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the file vanished between event-publish and audit dispatch
    (race / cleanup), the audit still fires on path + size — no crash,
    no missing-audit hole."""
    from server import outputs as outmod

    monkeypatch.setattr(outmod, "OUTPUTS_DIR", tmp_path)
    # Don't create the file — simulate a vanished output.

    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "output_saved",
        "path": "ghost.md",
        "bytes": 100,
        "project_id": "misc",
    })
    assert await _wait_for(lambda: len(calls) == 1)
    _, artifact = calls[0]
    assert "[output]" in artifact
    assert "ghost.md" in artifact
    assert "document body" not in artifact


@pytest.mark.asyncio
async def test_output_saved_extractor_crash_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the extractor itself crashes (e.g. unexpected parser error
    not caught by the per-format guard), the watcher must still fire
    a path-only audit instead of dropping the event."""
    from server import outputs as outmod
    from server.compass import output_extractor as oe

    monkeypatch.setattr(outmod, "OUTPUTS_DIR", tmp_path)
    f = tmp_path / "report.md"
    f.write_text("real content", encoding="utf-8")

    def _boom(_path: Any) -> None:
        raise RuntimeError("simulated extractor explosion")

    monkeypatch.setattr(oe, "extract_body", _boom)

    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "output_saved",
        "path": "report.md",
        "bytes": 12,
        "project_id": "misc",
    })
    assert await _wait_for(lambda: len(calls) == 1)
    _, artifact = calls[0]
    assert "[output]" in artifact
    assert "document body" not in artifact


@pytest.mark.asyncio
async def test_unrelated_event_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events not in `WATCHED_EVENT_TYPES` shouldn't fire an audit."""
    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "agent_started",
        "project_id": "misc",
    })
    # Give the watcher a moment to (not) process.
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_disabled_project_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event on a project where `compass_enabled_<id>` is unset
    should not fire an audit even though the type matches."""
    # Note: NOT calling _enable_compass here.
    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "commit_pushed",
        "sha": "abc",
        "message": "test",
        "project_id": "misc",
    })
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_disabled_project_explicit_false_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit `false` value also short-circuits."""
    await _disable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "commit_pushed",
        "sha": "abc",
        "message": "test",
        "project_id": "misc",
    })
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_debounce_collapses_burst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two same-(project, agent, type) events within the debounce
    window collapse into one audit; the second is dropped silently."""
    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    monkeypatch.setattr(cmp_config, "AUTO_AUDIT_DEBOUNCE_SECONDS", 60)
    await watcher.start_audit_watcher()

    for sha in ("aaa111", "bbb222"):
        await bus.publish({
            "ts": "2026-05-02T12:00:00+00:00",
            "agent_id": "p1",
            "type": "commit_pushed",
            "sha": sha,
            "message": f"commit {sha}",
            "project_id": "misc",
        })
    # Wait a moment for both events to drain.
    await asyncio.sleep(0.05)
    assert len(calls) == 1
    assert "aaa111" in calls[0][1]


@pytest.mark.asyncio
async def test_debounce_distinct_keys_both_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different agents (or different event types) bypass the
    debounce window — the (project, agent, type) tuple is the key."""
    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    monkeypatch.setattr(cmp_config, "AUTO_AUDIT_DEBOUNCE_SECONDS", 60)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "commit_pushed",
        "sha": "aaa",
        "message": "x",
        "project_id": "misc",
    })
    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p2",  # different agent
        "type": "commit_pushed",
        "sha": "bbb",
        "message": "y",
        "project_id": "misc",
    })
    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "decision_written",  # different event type
        "title": "z",
        "size": 100,
        "project_id": "misc",
    })
    assert await _wait_for(lambda: len(calls) == 3)


@pytest.mark.asyncio
async def test_zero_debounce_lets_every_event_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    monkeypatch.setattr(cmp_config, "AUTO_AUDIT_DEBOUNCE_SECONDS", 0)
    await watcher.start_audit_watcher()

    for sha in ("a", "b", "c"):
        await bus.publish({
            "ts": "2026-05-02T12:00:00+00:00",
            "agent_id": "p1",
            "type": "commit_pushed",
            "sha": sha,
            "message": "x",
            "project_id": "misc",
        })
    assert await _wait_for(lambda: len(calls) == 3)


@pytest.mark.asyncio
async def test_cost_cap_blocks_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `_today_spend()` exceeds `TEAM_DAILY_CAP_USD`, no audit
    fires. Mirrors the agents.py pre-spawn cap behavior."""
    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    from server import agents as agents_mod

    monkeypatch.setattr(agents_mod, "TEAM_DAILY_CAP_USD", 1.0)

    async def _fake_spend(*_a: Any, **_k: Any) -> float:
        return 5.0  # well over cap

    monkeypatch.setattr(agents_mod, "_today_spend", _fake_spend)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "commit_pushed",
        "sha": "abc",
        "message": "test",
        "project_id": "misc",
    })
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_cost_cap_allows_under_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    from server import agents as agents_mod

    monkeypatch.setattr(agents_mod, "TEAM_DAILY_CAP_USD", 1.0)

    async def _fake_spend(*_a: Any, **_k: Any) -> float:
        return 0.5

    monkeypatch.setattr(agents_mod, "_today_spend", _fake_spend)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "commit_pushed",
        "sha": "abc",
        "message": "test",
        "project_id": "misc",
    })
    assert await _wait_for(lambda: len(calls) == 1)


@pytest.mark.asyncio
async def test_audit_work_failure_does_not_kill_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `audit_work` raises, the watcher logs and keeps consuming
    events — a single bad LLM call must not break the subscriber."""
    await _enable_compass("misc")

    fail_count = {"n": 0}

    async def _crashy(project_id: str, artifact: str) -> dict[str, Any]:
        fail_count["n"] += 1
        if fail_count["n"] == 1:
            raise RuntimeError("simulated LLM blow-up")
        return {"verdict": "aligned"}

    monkeypatch.setattr(cmp_audit, "audit_work", _crashy)
    monkeypatch.setattr(cmp_config, "AUTO_AUDIT_DEBOUNCE_SECONDS", 0)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "commit_pushed",
        "sha": "first",
        "message": "boom",
        "project_id": "misc",
    })
    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "commit_pushed",
        "sha": "second",
        "message": "ok",
        "project_id": "misc",
    })
    assert await _wait_for(lambda: fail_count["n"] == 2)
    assert watcher.is_running()


@pytest.mark.asyncio
async def test_watcher_disabled_via_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `HARNESS_COMPASS_AUTO_AUDIT=false`, the watcher refuses to
    start — events flow but no audits fire."""
    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    monkeypatch.setattr(cmp_config, "AUTO_AUDIT_ENABLED", False)
    await watcher.start_audit_watcher()
    assert not watcher.is_running()

    await bus.publish({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "commit_pushed",
        "sha": "abc",
        "message": "test",
        "project_id": "misc",
    })
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_start_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _enable_compass("misc")
    _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()
    task1 = watcher._current_task
    await watcher.start_audit_watcher()
    task2 = watcher._current_task
    assert task1 is task2


@pytest.mark.asyncio
async def test_event_without_project_id_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If somehow an event lands without a project_id (shouldn't
    happen since `EventBus.publish` auto-stamps it, but guard
    anyway), the watcher drops it instead of asserting."""
    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    # Bypass the bus's auto-stamp by directly poking the queue.
    queue = next(iter(bus._queues), None)
    assert queue is not None
    await queue.put({
        "ts": "2026-05-02T12:00:00+00:00",
        "agent_id": "p1",
        "type": "commit_pushed",
        "sha": "abc",
        "message": "test",
        # no project_id
    })
    await asyncio.sleep(0.05)
    assert calls == []

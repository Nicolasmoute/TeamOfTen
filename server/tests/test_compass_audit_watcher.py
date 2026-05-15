"""Tests for `server.compass.audit_watcher`.

Compass refocused (2026-05-04): the watcher now subscribes to a single
event family — `task_stage_changed` — and audits only the
`from='plan' to='execute'` transition. Kanban's own auditor /
shipper stages handle execution-vs-plan downstream; Compass checks
plan-vs-intent upstream.

What we cover here:
  - `task_stage_changed{from=plan, to=execute}` fires `audit_work`.
  - The artifact includes title + trajectory + spec body.
  - Other transitions (audit_syntax → audit_semantics, etc.) don't fire.
  - Other event families (commit_pushed, decision_written,
    knowledge_written, output_saved, task_shipped) don't fire.
  - Trajectory without a `plan` stage skips the audit.
  - Per-(project, task_id) debounce drops re-emits.
  - Per-project enable flag short-circuits.
  - Cost-cap gate prevents firing when the team daily cap is hit.
  - `start` is idempotent and respects the global feature flag.
"""

from __future__ import annotations

import asyncio
import json
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


async def _create_task(
    *,
    task_id: str,
    project_id: str,
    title: str = "Test task",
    description: str = "",
    trajectory: list[dict[str, Any]] | None = None,
) -> None:
    """Insert a task row directly. The watcher reads via SELECT, not
    via the kanban API, so we don't need to go through the MCP tool."""
    if trajectory is None:
        trajectory = [
            {"stage": "plan", "to": ["p2"]},
            {"stage": "execute", "to": ["p1"]},
        ]
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO projects (id, name) VALUES (?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (project_id, project_id),
        )
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, description, "
            "status, created_by, trajectory) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, project_id, title, description, "execute",
             "coach", json.dumps(trajectory)),
        )
        await c.commit()
    finally:
        await c.close()


def _stub_audit_work(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Replace `cmp_audit.audit_work` with a recorder. Returns the
    list it appends to — `(project_id, artifact)` per call."""
    invocations: list[tuple[str, str]] = []

    async def _fake(project_id: str, artifact: str) -> dict[str, Any]:
        invocations.append((project_id, artifact))
        return {"verdict": "aligned", "summary": "stub"}

    monkeypatch.setattr(cmp_audit, "audit_work", _fake)
    return invocations


async def _wait_for(predicate, timeout: float = 3.0, step: float = 0.01) -> bool:
    """Poll-loop helper: spin the event loop briefly while waiting for
    the watcher's background task to drain a published event."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


def _valid_task_id() -> str:
    return "t-2026-05-04-deadbeef"


def _other_task_id() -> str:
    return "t-2026-05-04-cafef00d"


def _spec_for(project_id: str, task_id: str, body: str) -> Path:
    """Write a spec.md and return its path. Uses the real filesystem
    via `server.tasks.spec_path`; CWD is a tmp_path under the test
    fixture so we don't pollute /data."""
    from server.tasks import spec_path

    target = spec_path(project_id, task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


# ----------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
async def _isolate_watcher(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Any:
    """Bring up a fresh DB and reset the watcher's per-process state
    around every test. Yields after the test so we can stop the
    watcher cleanly even on failure paths.

    Also redirects DATA_ROOT to tmp_path so spec.md writes don't
    pollute /data.

    Defense-in-depth: stop any leftover watcher AND clear `_last_fire`
    at fixture entry too. The watcher's own `start_audit_watcher`
    clears `_last_fire`, but an aborted prior test that didn't reach
    its cleanup could leave `_current_task` pointing at a dead task
    and `_last_fire` populated with stale debounce entries.
    """
    await init_db()
    # Defensive: ensure no leftover watcher / debounce state.
    await watcher.stop_audit_watcher()
    watcher._last_fire.clear()
    # Observability state (Fix 12) — clear so cross-test mutation
    # doesn't leak. `start_audit_watcher` also clears these, but the
    # fixture must bring the test to a known state even when the test
    # doesn't (re)start the watcher.
    watcher._last_fire_iso.clear()
    watcher._last_skip.clear()

    monkeypatch.setattr(cmp_config, "AUTO_AUDIT_ENABLED", True)
    monkeypatch.setattr(cmp_config, "AUTO_AUDIT_DEBOUNCE_SECONDS", 30)
    # Disable the cost cap by default — individual tests can flip it on.
    from server import agents as agents_mod
    monkeypatch.setattr(agents_mod, "TEAM_DAILY_CAP_USD", 0.0)

    # Redirect DATA_ROOT via paths module so spec_path() lands in tmp.
    from server import paths as paths_mod
    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)
    yield
    await watcher.stop_audit_watcher()
    watcher._last_fire.clear()
    watcher._last_fire_iso.clear()
    watcher._last_skip.clear()


# ----------------------------------------------------- tests


@pytest.mark.asyncio
async def test_plan_to_execute_fires_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan→execute transition on a task with a plan stage in its
    trajectory fires `audit_work` with title + trajectory + spec body."""
    await _enable_compass("misc")
    task_id = _valid_task_id()
    await _create_task(
        task_id=task_id, project_id="misc",
        title="implement per-task billing",
    )
    _spec_for("misc", task_id,
              "# Plan\n\nRefactor billing module to per-task model.")

    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-04T12:00:00+00:00",
        "agent_id": "system",
        "type": "task_stage_changed",
        "task_id": task_id,
        "from": "plan",
        "to": "execute",
        "reason": "spec_ready",
        "owner": "p1",
        "project_id": "misc",
    })
    assert await _wait_for(lambda: len(calls) == 1)
    project_id, artifact = calls[0]
    assert project_id == "misc"
    assert "[task-plan]" in artifact
    assert task_id in artifact
    assert "implement per-task billing" in artifact
    assert "Trajectory:" in artifact
    assert "plan" in artifact and "execute" in artifact
    assert "--- spec ---" in artifact
    assert "Refactor billing" in artifact  # spec body inlined


@pytest.mark.asyncio
async def test_other_transitions_do_not_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit_syntax → audit_semantics, execute → audit_syntax, etc.
    must not fire Compass audits — kanban handles execution-vs-plan."""
    await _enable_compass("misc")
    task_id = _valid_task_id()
    await _create_task(task_id=task_id, project_id="misc")
    _spec_for("misc", task_id, "x")

    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    for from_, to in (
        ("execute", "audit_syntax"),
        ("audit_syntax", "audit_semantics"),
        ("audit_semantics", "ship"),
        ("ship", "archive"),
        ("execute", "execute"),  # re-emit same stage
    ):
        await bus.publish({
            "ts": "2026-05-04T12:00:00+00:00",
            "agent_id": "system",
            "type": "task_stage_changed",
            "task_id": task_id,
            "from": from_,
            "to": to,
            "owner": "p1",
            "project_id": "misc",
        })
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_legacy_event_types_do_not_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-refocus event types are no longer watched —
    commit_pushed / decision_written / knowledge_written / output_saved /
    task_shipped all flow through without firing Compass audits."""
    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    for ev in (
        {"type": "commit_pushed", "sha": "abc", "message": "x",
         "agent_id": "p1"},
        {"type": "decision_written", "title": "x", "size": 100,
         "agent_id": "coach"},
        {"type": "knowledge_written", "path": "k.md", "size": 50,
         "agent_id": "p2"},
        {"type": "output_saved", "path": "out.pdf", "bytes": 100,
         "agent_id": "p1"},
        {"type": "task_shipped", "task_id": "t-x", "agent_id": "p1"},
    ):
        ev["ts"] = "2026-05-04T12:00:00+00:00"
        ev["project_id"] = "misc"
        await bus.publish(ev)
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_trajectory_without_plan_skips_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a task's trajectory doesn't include a plan stage (shouldn't
    happen for a real plan→execute transition, but the guard is cheap),
    the audit is skipped."""
    await _enable_compass("misc")
    task_id = _valid_task_id()
    # Trajectory has no 'plan' stage at all.
    await _create_task(
        task_id=task_id, project_id="misc",
        trajectory=[{"stage": "execute", "to": []}],
    )

    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-04T12:00:00+00:00",
        "agent_id": "system",
        "type": "task_stage_changed",
        "task_id": task_id,
        "from": "plan",
        "to": "execute",
        "owner": "p1",
        "project_id": "misc",
    })
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_missing_spec_falls_back_to_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If spec.md doesn't exist on disk, the watcher still fires the
    audit using title + trajectory + description as the artifact."""
    await _enable_compass("misc")
    task_id = _valid_task_id()
    await _create_task(
        task_id=task_id, project_id="misc",
        title="some task", description="A task description for fallback.",
    )
    # Don't write spec.md.

    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-04T12:00:00+00:00",
        "agent_id": "system",
        "type": "task_stage_changed",
        "task_id": task_id,
        "from": "plan",
        "to": "execute",
        "owner": "p1",
        "project_id": "misc",
    })
    assert await _wait_for(lambda: len(calls) == 1)
    _, artifact = calls[0]
    assert "[task-plan]" in artifact
    assert "--- spec ---" not in artifact
    assert "description" in artifact.lower()
    assert "fallback" in artifact


@pytest.mark.asyncio
async def test_debounce_drops_reemit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two plan→execute events on the same task within the debounce
    window collapse into one audit. Per-task debounce, not per-agent."""
    await _enable_compass("misc")
    task_id = _valid_task_id()
    await _create_task(task_id=task_id, project_id="misc")
    _spec_for("misc", task_id, "spec body")

    calls = _stub_audit_work(monkeypatch)
    monkeypatch.setattr(cmp_config, "AUTO_AUDIT_DEBOUNCE_SECONDS", 60)
    await watcher.start_audit_watcher()

    for _ in range(2):
        await bus.publish({
            "ts": "2026-05-04T12:00:00+00:00",
            "agent_id": "system",
            "type": "task_stage_changed",
            "task_id": task_id,
            "from": "plan",
            "to": "execute",
            "owner": "p1",
            "project_id": "misc",
        })
    assert await _wait_for(lambda: len(calls) == 1)


@pytest.mark.asyncio
async def test_debounce_distinct_tasks_both_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different tasks share the project but bypass the debounce —
    each task gets its own plan-audit."""
    await _enable_compass("misc")
    t1 = _valid_task_id()
    t2 = _other_task_id()
    await _create_task(task_id=t1, project_id="misc")
    await _create_task(task_id=t2, project_id="misc")
    _spec_for("misc", t1, "spec a")
    _spec_for("misc", t2, "spec b")

    calls = _stub_audit_work(monkeypatch)
    monkeypatch.setattr(cmp_config, "AUTO_AUDIT_DEBOUNCE_SECONDS", 60)
    await watcher.start_audit_watcher()

    for tid in (t1, t2):
        await bus.publish({
            "ts": "2026-05-04T12:00:00+00:00",
            "agent_id": "system",
            "type": "task_stage_changed",
            "task_id": tid,
            "from": "plan",
            "to": "execute",
            "owner": "p1",
            "project_id": "misc",
        })
    assert await _wait_for(lambda: len(calls) == 2)


@pytest.mark.asyncio
async def test_disabled_project_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan→execute on a project where compass_enabled_<id> is unset
    should not fire."""
    task_id = _valid_task_id()
    await _create_task(task_id=task_id, project_id="misc")
    _spec_for("misc", task_id, "spec")

    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-04T12:00:00+00:00",
        "agent_id": "system",
        "type": "task_stage_changed",
        "task_id": task_id,
        "from": "plan",
        "to": "execute",
        "owner": "p1",
        "project_id": "misc",
    })
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_disabled_project_explicit_false_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _disable_compass("misc")
    task_id = _valid_task_id()
    await _create_task(task_id=task_id, project_id="misc")
    _spec_for("misc", task_id, "spec")

    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-04T12:00:00+00:00",
        "agent_id": "system",
        "type": "task_stage_changed",
        "task_id": task_id,
        "from": "plan",
        "to": "execute",
        "owner": "p1",
        "project_id": "misc",
    })
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_cost_cap_blocks_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `_today_spend()` exceeds `TEAM_DAILY_CAP_USD`, no audit
    fires. Mirrors the agents.py pre-spawn cap behavior."""
    await _enable_compass("misc")
    task_id = _valid_task_id()
    await _create_task(task_id=task_id, project_id="misc")
    _spec_for("misc", task_id, "spec")

    calls = _stub_audit_work(monkeypatch)
    from server import agents as agents_mod

    monkeypatch.setattr(agents_mod, "TEAM_DAILY_CAP_USD", 1.0)

    async def _fake_spend(*_a: Any, **_k: Any) -> float:
        return 5.0  # well over cap

    monkeypatch.setattr(agents_mod, "_today_spend", _fake_spend)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-04T12:00:00+00:00",
        "agent_id": "system",
        "type": "task_stage_changed",
        "task_id": task_id,
        "from": "plan",
        "to": "execute",
        "owner": "p1",
        "project_id": "misc",
    })
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_cost_cap_allows_under_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _enable_compass("misc")
    task_id = _valid_task_id()
    await _create_task(task_id=task_id, project_id="misc")
    _spec_for("misc", task_id, "spec")

    calls = _stub_audit_work(monkeypatch)
    from server import agents as agents_mod

    monkeypatch.setattr(agents_mod, "TEAM_DAILY_CAP_USD", 1.0)

    async def _fake_spend(*_a: Any, **_k: Any) -> float:
        return 0.5

    monkeypatch.setattr(agents_mod, "_today_spend", _fake_spend)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-04T12:00:00+00:00",
        "agent_id": "system",
        "type": "task_stage_changed",
        "task_id": task_id,
        "from": "plan",
        "to": "execute",
        "owner": "p1",
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
    t1 = _valid_task_id()
    t2 = _other_task_id()
    await _create_task(task_id=t1, project_id="misc")
    await _create_task(task_id=t2, project_id="misc")
    _spec_for("misc", t1, "x")
    _spec_for("misc", t2, "y")

    fail_count = {"n": 0}

    async def _crashy(project_id: str, artifact: str) -> dict[str, Any]:
        fail_count["n"] += 1
        if fail_count["n"] == 1:
            raise RuntimeError("simulated LLM blow-up")
        return {"verdict": "aligned"}

    monkeypatch.setattr(cmp_audit, "audit_work", _crashy)
    monkeypatch.setattr(cmp_config, "AUTO_AUDIT_DEBOUNCE_SECONDS", 0)
    await watcher.start_audit_watcher()

    for tid in (t1, t2):
        await bus.publish({
            "ts": "2026-05-04T12:00:00+00:00",
            "agent_id": "system",
            "type": "task_stage_changed",
            "task_id": tid,
            "from": "plan",
            "to": "execute",
            "owner": "p1",
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
    task_id = _valid_task_id()
    await _create_task(task_id=task_id, project_id="misc")
    _spec_for("misc", task_id, "spec")

    calls = _stub_audit_work(monkeypatch)
    monkeypatch.setattr(cmp_config, "AUTO_AUDIT_ENABLED", False)
    await watcher.start_audit_watcher()
    assert not watcher.is_running()

    await bus.publish({
        "ts": "2026-05-04T12:00:00+00:00",
        "agent_id": "system",
        "type": "task_stage_changed",
        "task_id": task_id,
        "from": "plan",
        "to": "execute",
        "owner": "p1",
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
async def test_event_without_project_id_uses_task_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If somehow an event lands without a project_id, the watcher
    falls back to looking up the task row to recover it."""
    await _enable_compass("misc")
    task_id = _valid_task_id()
    await _create_task(task_id=task_id, project_id="misc")
    _spec_for("misc", task_id, "fallback test")

    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    queue = next(iter(bus._queues), None)
    assert queue is not None
    await queue.put({
        "ts": "2026-05-04T12:00:00+00:00",
        "agent_id": "system",
        "type": "task_stage_changed",
        "task_id": task_id,
        "from": "plan",
        "to": "execute",
        # no project_id — must be recovered via task row lookup
    })
    assert await _wait_for(lambda: len(calls) == 1)
    project_id, _ = calls[0]
    assert project_id == "misc"


@pytest.mark.asyncio
async def test_unknown_task_id_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan→execute event referencing a task id that doesn't exist
    (race / stale event) is dropped silently."""
    await _enable_compass("misc")
    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    await bus.publish({
        "ts": "2026-05-04T12:00:00+00:00",
        "agent_id": "system",
        "type": "task_stage_changed",
        "task_id": _valid_task_id(),  # not inserted
        "from": "plan",
        "to": "execute",
        "owner": "p1",
        "project_id": "misc",
    })
    await asyncio.sleep(0.05)
    assert calls == []



# ----------------------------------------------------- observability (Fix 12)


def _drain(q: asyncio.Queue, into: list) -> bool:
    """Drain everything pending on q into `into`. Returns False so the
    `_wait_for` predicate keeps polling until the actual condition matches."""
    while True:
        try:
            into.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return False


@pytest.mark.asyncio
async def test_emit_skip_publishes_event_and_records_state() -> None:
    """`_emit_skip` writes both a bus event and module state."""
    watcher._last_skip.clear()
    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        await watcher._emit_skip(
            project_id="misc",
            task_id="t-2026-05-12-aaaa1111",
            reason="project_disabled",
        )
        await asyncio.sleep(0)
        while True:
            try:
                captured.append(q.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        bus.unsubscribe(q)

    matching = [e for e in captured if e.get("type") == "compass_audit_skipped"]
    assert len(matching) == 1
    ev = matching[0]
    assert ev["project_id"] == "misc"
    assert ev["task_id"] == "t-2026-05-12-aaaa1111"
    assert ev["reason"] == "project_disabled"
    assert ev["agent_id"] == "system"
    assert "ts" in ev

    rec = watcher._last_skip.get("misc")
    assert rec is not None
    assert rec["reason"] == "project_disabled"
    assert rec["task_id"] == "t-2026-05-12-aaaa1111"
    assert "ts" in rec


@pytest.mark.asyncio
async def test_snapshot_health_returns_expected_shape() -> None:
    """`snapshot_health()` returns the read-only view. Verify shape +
    defensive copies prevent external mutation of internal state."""
    watcher._last_fire.clear()
    watcher._last_fire_iso.clear()
    watcher._last_skip.clear()
    watcher._last_fire_iso["misc"] = "2026-05-13T00:00:00+00:00"
    watcher._last_skip["alpha"] = {
        "ts": "2026-05-13T00:01:00+00:00",
        "reason": "debounced",
        "task_id": "t-x",
    }
    snap = watcher.snapshot_health()
    assert set(snap.keys()) == {
        "enabled", "running", "watched_event_types",
        "debounce_seconds", "last_fire_by_project",
        "last_skip_by_project", "debounce_keys_active",
    }
    assert "task_stage_changed" in snap["watched_event_types"]
    assert snap["last_fire_by_project"] == {"misc": "2026-05-13T00:00:00+00:00"}
    assert snap["last_skip_by_project"]["alpha"]["reason"] == "debounced"

    # Defensive copy — caller mutation must not leak back.
    snap["last_fire_by_project"]["misc"] = "TAMPERED"
    snap["last_skip_by_project"]["alpha"]["reason"] = "TAMPERED"
    assert watcher._last_fire_iso["misc"] == "2026-05-13T00:00:00+00:00"
    assert watcher._last_skip["alpha"]["reason"] == "debounced"


@pytest.mark.asyncio
async def test_project_disabled_emits_skip_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan→execute on a disabled project doesn't fire audit AND
    emits a `compass_audit_skipped{reason='project_disabled'}` event."""
    await _disable_compass("misc")
    task_id = _valid_task_id()
    await _create_task(task_id=task_id, project_id="misc")
    _spec_for("misc", task_id, "x")

    calls = _stub_audit_work(monkeypatch)
    await watcher.start_audit_watcher()

    q = bus.subscribe()
    captured: list[dict[str, Any]] = []
    try:
        await bus.publish({
            "ts": "2026-05-13T12:00:00+00:00",
            "agent_id": "system",
            "type": "task_stage_changed",
            "task_id": task_id,
            "from": "plan",
            "to": "execute",
            "owner": "p1",
            "project_id": "misc",
        })
        await _wait_for(lambda: _drain(q, captured) or any(
            e.get("type") == "compass_audit_skipped" for e in captured
        ), timeout=1.0)
    finally:
        bus.unsubscribe(q)

    assert calls == []
    skips = [e for e in captured if e.get("type") == "compass_audit_skipped"]
    assert any(s.get("reason") == "project_disabled" for s in skips)
    assert watcher._last_skip.get("misc", {}).get("reason") == "project_disabled"


@pytest.mark.asyncio
async def test_coord_check_compass_audit_rejects_non_coach() -> None:
    """`coord_check_compass_audit` is Coach-only — Players get an error."""
    from server.tools import build_coord_server

    server = build_coord_server("p3", include_proxy_metadata=True)
    handler = server["_handlers"]["coord_check_compass_audit"]
    result = await handler({})
    assert result.get("is_error") is True
    text = result["content"][0]["text"]
    assert "Coach-only" in text


@pytest.mark.asyncio
async def test_coord_check_compass_audit_renders_snapshot() -> None:
    """Coach sees a markdown table with all the snapshot fields."""
    from server.tools import build_coord_server

    watcher._last_fire_iso["misc"] = "2026-05-13T00:00:00+00:00"
    watcher._last_skip["alpha"] = {
        "ts": "2026-05-13T00:01:00+00:00",
        "reason": "debounced",
        "task_id": "t-y",
    }

    server = build_coord_server("coach", include_proxy_metadata=True)
    handler = server["_handlers"]["coord_check_compass_audit"]
    result = await handler({})
    assert not result.get("is_error")
    text = result["content"][0]["text"]
    assert "Compass auto-audit watcher" in text
    assert "enabled" in text and "running" in text
    assert "misc" in text
    assert "alpha" in text and "debounced" in text

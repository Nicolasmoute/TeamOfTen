"""Tests for the idle-Player poller (server.idle_poller).

We exercise `sweep_once` directly (rather than `_run`'s asyncio.sleep
loop) so the tests are deterministic and don't depend on wall-clock
time. `maybe_wake_agent` is monkeypatched to a recording stub —
otherwise it tries to spawn a real Claude SDK subprocess.
"""

from __future__ import annotations

from typing import Any

import pytest

import server.agents as agents_mod
from server.db import configured_conn, init_db
from server.idle_poller import (
    _flag_enabled,
    _has_available_work,
    sweep_once,
)


# ---------------------------------------------------------------- helpers

class WakeRecorder:
    """Stub for maybe_wake_agent that records calls without actually
    spawning anything. Patched into agents.maybe_wake_agent for the
    duration of the test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self,
        slot: str,
        prompt: str,
        *,
        bypass_debounce: bool = False,
        **kwargs: Any,
    ) -> bool:
        self.calls.append((slot, prompt))
        return True


@pytest.fixture
async def wake_stub(monkeypatch: pytest.MonkeyPatch) -> WakeRecorder:
    rec = WakeRecorder()
    monkeypatch.setattr(agents_mod, "maybe_wake_agent", rec)
    return rec


async def _seed_pool_task(
    *,
    task_id: str,
    eligible: list[str],
    assigned_at: str = "2020-01-01T00:00:00Z",
) -> None:
    """Insert a task in plan stage + a posted-pool executor role row."""
    import json
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, created_by) "
            "VALUES (?, 'misc', 'pool task', 'plan', 'coach')",
            (task_id,),
        )
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at) "
            "VALUES (?, 'executor', ?, NULL, ?)",
            (task_id, json.dumps(eligible), assigned_at),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_hard_assign(
    *,
    task_id: str,
    role: str,
    owner: str,
    assigned_at: str = "2020-01-01T00:00:00Z",
) -> None:
    """Insert a hard-assigned role row that hasn't completed."""
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by) VALUES (?, 'misc', 't', 'execute', ?, 'coach')",
            (task_id, owner if role == "executor" else None),
        )
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at) "
            "VALUES (?, ?, '[]', ?, ?)",
            (task_id, role, owner, assigned_at),
        )
        await c.commit()
    finally:
        await c.close()


# ---------------------------------------------------------------- skip rules

async def test_locked_player_not_woken(
    fresh_db: str, wake_stub: WakeRecorder
) -> None:
    await init_db()
    await _seed_pool_task(
        task_id="t-2026-05-03-aaaaaaaa", eligible=["p3"]
    )
    c = await configured_conn()
    try:
        await c.execute("UPDATE agents SET locked = 1 WHERE id = 'p3'")
        await c.commit()
    finally:
        await c.close()
    woken = await sweep_once()
    assert woken == 0
    assert wake_stub.calls == []


async def test_player_with_current_task_not_woken(
    fresh_db: str, wake_stub: WakeRecorder
) -> None:
    await init_db()
    await _seed_pool_task(
        task_id="t-2026-05-03-aaaaaaaa", eligible=["p3"]
    )
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET current_task_id = 't-other' WHERE id = 'p3'"
        )
        await c.commit()
    finally:
        await c.close()
    woken = await sweep_once()
    # p3 is busy → not woken. Other free Players (p1, p2, p4..p10) aren't
    # eligible for this task either.
    assert woken == 0


async def test_working_status_not_woken(
    fresh_db: str, wake_stub: WakeRecorder
) -> None:
    await init_db()
    await _seed_pool_task(
        task_id="t-2026-05-03-aaaaaaaa", eligible=["p3"]
    )
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET status = 'working' WHERE id = 'p3'"
        )
        await c.commit()
    finally:
        await c.close()
    woken = await sweep_once()
    assert woken == 0


# ---------------------------------------------------------------- happy path

async def test_eligible_pool_task_wakes_player(
    fresh_db: str, wake_stub: WakeRecorder
) -> None:
    await init_db()
    await _seed_pool_task(
        task_id="t-2026-05-03-aaaaaaaa", eligible=["p3", "p4"]
    )
    woken = await sweep_once()
    # Both p3 and p4 are eligible + free → both get woken.
    assert woken == 2
    slots = sorted(c[0] for c in wake_stub.calls)
    assert slots == ["p3", "p4"]
    # Wake prompt mentions coord_my_assignments.
    for _slot, prompt in wake_stub.calls:
        assert "coord_my_assignments" in prompt


async def test_debounced_wake_does_not_stamp_or_emit(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If maybe_wake_agent declines the wake, the poller must not stamp
    last_idle_wake_at or count the slot as woken."""
    calls: list[str] = []

    async def fake_wake(slot: str, prompt: str, **kwargs: Any) -> bool:
        calls.append(slot)
        return False

    monkeypatch.setattr(agents_mod, "maybe_wake_agent", fake_wake)
    await init_db()
    await _seed_pool_task(
        task_id="t-2026-05-03-aaaabbbb", eligible=["p3"]
    )

    woken = await sweep_once()
    assert woken == 0
    assert calls == ["p3"]

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT last_idle_wake_at FROM agents WHERE id = 'p3'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert dict(row)["last_idle_wake_at"] is None


async def test_hard_assigned_pending_role_wakes_player(
    fresh_db: str, wake_stub: WakeRecorder
) -> None:
    """A hard-assigned syntax-auditor role row whose original wake
    was missed should be re-woken by the poller."""
    await init_db()
    await _seed_hard_assign(
        task_id="t-2026-05-03-bbbbbbbb",
        role="auditor_syntax",
        owner="p4",
    )
    woken = await sweep_once()
    assert ("p4", woken) and any(c[0] == "p4" for c in wake_stub.calls)


async def test_grace_period_skips_freshly_assigned(
    fresh_db: str, wake_stub: WakeRecorder
) -> None:
    """A pool task assigned moments ago should be skipped — give the
    initial assign-time wake a head start."""
    from datetime import datetime, timezone
    await init_db()
    just_now = datetime.now(timezone.utc).isoformat()
    await _seed_pool_task(
        task_id="t-2026-05-03-cccccccc",
        eligible=["p3"],
        assigned_at=just_now,
    )
    woken = await sweep_once()
    assert woken == 0
    assert wake_stub.calls == []


# ---------------------------------------------------------------- debounce

async def test_per_player_debounce(
    fresh_db: str, wake_stub: WakeRecorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Player who was just woken by the poller shouldn't be re-woken
    on the next sweep within the debounce window."""
    from datetime import datetime, timezone
    monkeypatch.setenv("HARNESS_IDLE_POLL_DEBOUNCE_SECONDS", "1800")
    await init_db()
    await _seed_pool_task(
        task_id="t-2026-05-03-dddddddd", eligible=["p3"]
    )
    # Pre-stamp last_idle_wake_at to "just now".
    just_now = datetime.now(timezone.utc).isoformat()
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET last_idle_wake_at = ? WHERE id = 'p3'",
            (just_now,),
        )
        await c.commit()
    finally:
        await c.close()
    woken = await sweep_once()
    assert woken == 0


async def test_debounce_window_zero_means_always_wake(
    fresh_db: str, wake_stub: WakeRecorder, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting the debounce window to 0 disables the debounce check."""
    from datetime import datetime, timezone
    monkeypatch.setenv("HARNESS_IDLE_POLL_DEBOUNCE_SECONDS", "0")
    await init_db()
    await _seed_pool_task(
        task_id="t-2026-05-03-eeeeeeee", eligible=["p3"]
    )
    just_now = datetime.now(timezone.utc).isoformat()
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE agents SET last_idle_wake_at = ? WHERE id = 'p3'",
            (just_now,),
        )
        await c.commit()
    finally:
        await c.close()
    woken = await sweep_once()
    assert woken >= 1


# ---------------------------------------------------------------- feature flag

def test_feature_flag_default_enabled(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_IDLE_POLL_ENABLED", raising=False)
    assert _flag_enabled() is True
    monkeypatch.setenv("HARNESS_IDLE_POLL_ENABLED", "false")
    assert _flag_enabled() is False
    monkeypatch.setenv("HARNESS_IDLE_POLL_ENABLED", "0")
    assert _flag_enabled() is False
    monkeypatch.setenv("HARNESS_IDLE_POLL_ENABLED", "true")
    assert _flag_enabled() is True


# ---------------------------------------------------------------- _has_available_work

async def test_has_available_work_returns_pool_reason(fresh_db: str) -> None:
    await init_db()
    await _seed_pool_task(
        task_id="t-2026-05-03-ffffffff", eligible=["p3"]
    )
    out = await _has_available_work("p3")
    assert out == ("pool_task_available", "t-2026-05-03-ffffffff")


async def test_has_available_work_returns_pending_role_reason(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_hard_assign(
        task_id="t-2026-05-03-99999999", role="shipper", owner="p3",
    )
    out = await _has_available_work("p3")
    assert out is not None
    reason, task_id = out
    assert reason == "pending_role_assignment"
    assert task_id == "t-2026-05-03-99999999"


async def test_has_available_work_completed_role_skipped(
    fresh_db: str,
) -> None:
    """A completed role assignment shouldn't trigger the poller."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by) VALUES "
            "('t-done', 'misc', 't', 'archive', 'p3', 'coach')"
        )
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, completed_at) "
            "VALUES ('t-done', 'executor', '[]', 'p3', "
            "'2020-01-01T00:00:00Z', '2020-01-02T00:00:00Z')"
        )
        await c.commit()
    finally:
        await c.close()
    out = await _has_available_work("p3")
    assert out is None

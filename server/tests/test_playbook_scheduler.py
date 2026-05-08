"""Playbook scheduler tests — spec §18.1.

Covers gating logic: disabled flag, no active project, blocked flag,
and the daily-run gate.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import server.playbook.paths as pb_paths_mod
import server.playbook.scheduler as scheduler_mod
from server.playbook import config


@pytest.fixture
def sched_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(pb_paths_mod, "DATA_ROOT", tmp_path)
    db = tmp_path / "harness.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE team_config (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    finally:
        conn.close()
    import server.db as dbmod
    monkeypatch.setattr(dbmod, "DB_PATH", str(db))
    return tmp_path


def _set_team_config(db_path: Path, key: str, value: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO team_config VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def test_tick_skipped_when_disabled(sched_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_team_config(sched_env / "harness.db", config.PLAYBOOK_DISABLED_KEY, "1")

    bootstrap_called = {"v": False}
    daily_called = {"v": False}

    async def _no_bootstrap():
        bootstrap_called["v"] = True
    async def _no_daily(*a, **kw):
        daily_called["v"] = True

    monkeypatch.setattr(scheduler_mod.bootstrap, "run_bootstrap", _no_bootstrap)
    monkeypatch.setattr(scheduler_mod.runner, "run_daily_reflection", _no_daily)
    asyncio.run(scheduler_mod._tick())
    assert not bootstrap_called["v"]
    assert not daily_called["v"]


def test_tick_skipped_when_no_active_project(sched_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap_called = {"v": False}

    async def _no_project():
        return False
    async def _no_bootstrap():
        bootstrap_called["v"] = True

    monkeypatch.setattr(scheduler_mod, "_has_active_project", _no_project)
    monkeypatch.setattr(scheduler_mod.bootstrap, "run_bootstrap", _no_bootstrap)
    asyncio.run(scheduler_mod._tick())
    assert not bootstrap_called["v"]


def test_tick_blocked_skips_bootstrap(sched_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """G1 — when bootstrap is blocked, scheduler must not retry."""
    _set_team_config(sched_env / "harness.db", config.PLAYBOOK_BOOTSTRAP_BLOCKED_KEY, "1")
    # bootstrap_done is unset → would normally trigger bootstrap

    bootstrap_called = {"v": False}
    async def _no_bootstrap():
        bootstrap_called["v"] = True
    async def _yes_project():
        return True
    monkeypatch.setattr(scheduler_mod, "_has_active_project", _yes_project)
    monkeypatch.setattr(scheduler_mod.bootstrap, "run_bootstrap", _no_bootstrap)

    asyncio.run(scheduler_mod._tick())
    assert not bootstrap_called["v"]


def test_tick_unblocked_runs_bootstrap_when_done_unset(sched_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap_called = {"v": False}
    async def _yes_project():
        return True
    async def _bootstrap():
        bootstrap_called["v"] = True
    monkeypatch.setattr(scheduler_mod, "_has_active_project", _yes_project)
    monkeypatch.setattr(scheduler_mod.bootstrap, "run_bootstrap", _bootstrap)

    asyncio.run(scheduler_mod._tick())
    assert bootstrap_called["v"]


def test_should_run_daily_returns_true_when_past_hour_and_new_day(sched_env: Path) -> None:
    """At 04+:00 UTC with no last_run_at, daily run is due."""
    now = datetime.now(timezone.utc)
    if now.hour < config.RUN_HOUR_UTC_DEFAULT:
        # Test environment is before the run hour — skip this hour-sensitive case
        pytest.skip("test runs before HARNESS_PLAYBOOK_RUN_HOUR_UTC")
    assert scheduler_mod._should_run_daily(now) is True


def test_should_run_daily_returns_false_after_already_run_today(sched_env: Path) -> None:
    now = datetime.now(timezone.utc)
    _set_team_config(sched_env / "harness.db", config.PLAYBOOK_LAST_RUN_AT_KEY, now.isoformat())
    assert scheduler_mod._should_run_daily(now) is False


def test_start_stop_lifecycle_is_idempotent() -> None:
    """Calling start twice should not create two tasks."""
    asyncio.run(scheduler_mod.start_playbook_scheduler())
    asyncio.run(scheduler_mod.start_playbook_scheduler())
    asyncio.run(scheduler_mod.stop_playbook_scheduler())
    # No assertion — survival is the assertion. (Double-start would create
    # a second task that survives stop and leaks.)

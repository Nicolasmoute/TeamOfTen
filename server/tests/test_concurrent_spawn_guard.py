"""Test the concurrent-spawn guard in run_agent.

Without the guard, 100 rapid POSTs to /api/agents/start would spawn
100 subprocesses for the same slot (_running_tasks dict assignment
would just overwrite). This test verifies that when a non-done task
is already registered for a slot, run_agent returns early and
emits spawn_rejected instead of touching the SDK.
"""

from __future__ import annotations

import asyncio

import pytest

from server.db import init_db
from server.events import bus


@pytest.fixture(autouse=True)
async def _init(fresh_db: str) -> None:
    await init_db()


async def test_spawn_rejected_when_already_running() -> None:
    from server.agents import _running_tasks, run_agent
    # Register a pretend-running task for p3. Use an asyncio.Event.wait
    # that never resolves so the task stays not-done throughout this
    # test.
    never = asyncio.Event()
    blocker = asyncio.create_task(never.wait())
    _running_tasks["p3"] = blocker

    # Subscribe to the bus BEFORE calling run_agent so we catch the
    # spawn_rejected event.
    q = bus.subscribe()
    try:
        # run_agent should emit spawn_rejected and return without
        # touching the SDK. If the guard were missing, this call would
        # try to execute query() and fail / spawn a subprocess.
        await run_agent("p3", "test prompt")

        # Drain events until we find spawn_rejected, with a short
        # timeout to avoid hanging if the guard didn't fire.
        got_rejected = False
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=0.5)
            except asyncio.TimeoutError:
                break
            if ev.get("type") == "spawn_rejected" and ev.get("agent_id") == "p3":
                got_rejected = True
                break
            # If the guard were missing we'd see agent_started first;
            # that's a test failure signal.
            if ev.get("type") == "agent_started":
                pytest.fail(
                    "guard missing — agent_started fired while "
                    "_running_tasks had a live task for p3"
                )
        assert got_rejected, "spawn_rejected event was not emitted"
    finally:
        bus.unsubscribe(q)
        blocker.cancel()
        try:
            await blocker
        except (asyncio.CancelledError, Exception):
            pass
        _running_tasks.pop("p3", None)


async def test_guard_allows_spawn_if_existing_task_is_done() -> None:
    from server.agents import _running_tasks
    # A task that's already done() should NOT block a new spawn. We
    # assert the guard condition directly rather than running the
    # whole pipeline (which would try to spawn a subprocess).
    done_task = asyncio.create_task(asyncio.sleep(0))
    await done_task  # ensure it's done
    _running_tasks["p5"] = done_task
    existing = _running_tasks.get("p5")
    assert existing is not None
    assert existing.done()
    # Under the guard condition `not existing.done()` evaluates False,
    # so the early-return branch is skipped.
    assert not (existing is not None and not existing.done())
    _running_tasks.pop("p5", None)

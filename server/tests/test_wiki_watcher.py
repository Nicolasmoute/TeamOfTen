"""Tests for server/wiki_watcher.py.

Strategy:
  - Monkeypatch global_paths() and update_wiki_index to avoid touching /data.
  - Use asyncio.sleep(0) / tight await loops to drain the task without real waits.
  - Mirror test_compass_audit_watcher.py fixture style.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import server.wiki_watcher as watcher


# ─── fixture: isolate watcher state between tests ────────────────────────────


@pytest.fixture(autouse=True)
async def _isolate_watcher():
    """Stop any leftover watcher task and reset module state before/after each test."""
    await watcher.stop_wiki_watcher()
    watcher._current_task = None
    yield
    await watcher.stop_wiki_watcher()
    watcher._current_task = None


# ─── helper: fake GlobalPaths ─────────────────────────────────────────────────


def _make_fake_gp(tmp_path: Path, index_exists: bool = True, index_age: float = 10.0):
    """Return a mock GlobalPaths-like object.

    index_age > 0 means INDEX.md is that many seconds OLDER than now.
    Use index_age <= 0 for a freshly-written index.
    """
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    index = wiki / "INDEX.md"
    if index_exists:
        index.write_text("# Wiki Index\n")
        # backdate the index
        mtime = time.time() - index_age
        os.utime(index, (mtime, mtime))
    gp = MagicMock()
    gp.wiki = wiki
    gp.wiki_index = index
    return gp


def _write_md(parent: Path, name: str, age: float = 0.0) -> Path:
    """Write a .md file with mtime set to now - age seconds."""
    parent.mkdir(parents=True, exist_ok=True)
    p = parent / name
    p.write_text(f"# {name}\n")
    mtime = time.time() - age
    os.utime(p, (mtime, mtime))
    return p


# ─── _needs_rebuild unit tests ────────────────────────────────────────────────


def test_needs_rebuild_missing_index(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write_md(wiki, "entry.md")
    index = wiki / "INDEX.md"
    assert watcher._needs_rebuild(wiki, index) is True


def test_needs_rebuild_stale_index(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    # Index is 30s old; entry.md is fresh
    index = wiki / "INDEX.md"
    index.write_text("# Wiki Index\n")
    old_mtime = time.time() - 30
    os.utime(index, (old_mtime, old_mtime))
    _write_md(wiki, "entry.md")  # fresh
    assert watcher._needs_rebuild(wiki, index) is True


def test_needs_rebuild_current_index(tmp_path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    _write_md(wiki, "entry.md", age=60)  # entry 60s old
    # Write index fresh (newer than entry)
    index = wiki / "INDEX.md"
    index.write_text("# Wiki Index\n")  # fresh
    assert watcher._needs_rebuild(wiki, index) is False


def test_needs_rebuild_subdir_entry_stale(tmp_path):
    """Per-project subdir entry newer than index → needs rebuild."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    index = wiki / "INDEX.md"
    index.write_text("# Wiki Index\n")
    old_mtime = time.time() - 30
    os.utime(index, (old_mtime, old_mtime))
    # Entry in a per-project subdirectory
    _write_md(wiki / "teamoften", "some-concept.md")  # fresh
    assert watcher._needs_rebuild(wiki, index) is True


def test_needs_rebuild_index_file_itself_skipped(tmp_path):
    """INDEX.md is excluded from the rglob scan — updating it shouldn't re-trigger."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    # No other .md files; only INDEX.md
    index = wiki / "INDEX.md"
    index.write_text("# Wiki Index\n")
    # Index is brand new — nothing should be newer
    assert watcher._needs_rebuild(wiki, index) is False


# ─── async watcher lifecycle tests ───────────────────────────────────────────


async def _run_one_tick(fake_gp, rebuild_calls: list, *, raise_on_rebuild=False):
    """Set up monkeypatches and run exactly one watcher tick (interval=0s)."""
    def _fake_update_wiki_index():
        rebuild_calls.append(1)
        if raise_on_rebuild:
            raise RuntimeError("simulated failure")
        return True

    with (
        patch.dict(os.environ, {watcher._INTERVAL_ENV: "0", watcher._ENABLED_ENV: "true"}),
        patch("server.wiki_watcher.global_paths", return_value=fake_gp),
        patch("server.wiki_watcher.update_wiki_index", side_effect=_fake_update_wiki_index),
    ):
        await watcher.start_wiki_watcher()
        # Give the event loop one round-trip: sleep(0) fires, then the task
        # runs its sleep(0) + check + (maybe) rebuild.
        for _ in range(10):
            await asyncio.sleep(0)
        await watcher.stop_wiki_watcher()


@pytest.mark.asyncio
async def test_stale_index_triggers_rebuild(tmp_path):
    """A source file newer than INDEX.md → update_wiki_index() called."""
    gp = _make_fake_gp(tmp_path, index_exists=True, index_age=30)
    _write_md(gp.wiki, "new-entry.md", age=0)  # fresh

    calls: list[int] = []
    await _run_one_tick(gp, calls)
    assert calls, "update_wiki_index should have been called"


@pytest.mark.asyncio
async def test_current_index_no_rebuild(tmp_path):
    """INDEX.md is current — update_wiki_index() should NOT be called."""
    gp = _make_fake_gp(tmp_path, index_exists=True, index_age=0)
    _write_md(gp.wiki, "old-entry.md", age=60)  # older than index

    calls: list[int] = []
    await _run_one_tick(gp, calls)
    assert not calls, "update_wiki_index should NOT have been called"


@pytest.mark.asyncio
async def test_missing_index_triggers_rebuild(tmp_path):
    """No INDEX.md at all → update_wiki_index() called."""
    gp = _make_fake_gp(tmp_path, index_exists=False)
    _write_md(gp.wiki, "entry.md")

    calls: list[int] = []
    await _run_one_tick(gp, calls)
    assert calls, "update_wiki_index should have been called when INDEX is missing"


@pytest.mark.asyncio
async def test_kill_switch_disabled(tmp_path):
    """HARNESS_WIKI_WATCHER_ENABLED=false → loop exits, no rebuild."""
    gp = _make_fake_gp(tmp_path, index_exists=False)
    _write_md(gp.wiki, "entry.md")
    calls: list[int] = []

    with (
        patch.dict(os.environ, {watcher._ENABLED_ENV: "false"}),
        patch("server.wiki_watcher.global_paths", return_value=gp),
        patch("server.wiki_watcher.update_wiki_index", side_effect=lambda: calls.append(1) or True),
    ):
        await watcher.start_wiki_watcher()
        for _ in range(10):
            await asyncio.sleep(0)
        # Task should have exited (disabled)
        assert watcher._current_task is not None
        assert watcher._current_task.done(), "task should exit immediately when disabled"
        assert not calls, "update_wiki_index must not be called when disabled"


@pytest.mark.asyncio
async def test_stop_cancels_task(tmp_path):
    """stop_wiki_watcher() cancels the background task within 2 s."""
    gp = _make_fake_gp(tmp_path, index_exists=True, index_age=0)

    with (
        patch.dict(os.environ, {watcher._INTERVAL_ENV: "999", watcher._ENABLED_ENV: "true"}),
        patch("server.wiki_watcher.global_paths", return_value=gp),
        patch("server.wiki_watcher.update_wiki_index", return_value=True),
    ):
        await watcher.start_wiki_watcher()
        task = watcher._current_task
        assert task is not None
        assert not task.done()

        await watcher.stop_wiki_watcher(timeout=2.0)
        assert task.done(), "task should be cancelled after stop_wiki_watcher()"


@pytest.mark.asyncio
async def test_exception_does_not_crash_watcher(tmp_path):
    """update_wiki_index() raising should log but keep the watcher running."""
    gp = _make_fake_gp(tmp_path, index_exists=False)
    _write_md(gp.wiki, "entry.md")

    calls: list[int] = []
    # Use a very short interval so we get two ticks quickly.
    # First tick: raises; second tick: succeeds.
    outcomes = [RuntimeError("boom"), None]

    def _flaky():
        calls.append(1)
        result = outcomes.pop(0) if outcomes else None
        if isinstance(result, Exception):
            raise result
        return True

    with (
        patch.dict(os.environ, {watcher._INTERVAL_ENV: "0", watcher._ENABLED_ENV: "true"}),
        patch("server.wiki_watcher.global_paths", return_value=gp),
        patch("server.wiki_watcher.update_wiki_index", side_effect=_flaky),
    ):
        await watcher.start_wiki_watcher()
        # Allow multiple ticks — two asyncio.to_thread round-trips each need several
        # event-loop iterations; a small real sleep ensures threads have time to settle.
        for _ in range(30):
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)  # allow thread pool round-trips to complete
        task = watcher._current_task
        # Watcher must still be running (not crashed by exception)
        assert task is not None
        assert not task.done(), "watcher must survive an update_wiki_index exception"
        assert len(calls) >= 2, "watcher should have continued after exception"


@pytest.mark.asyncio
async def test_missing_wiki_dir_graceful(tmp_path):
    """Wiki dir absent → watcher logs and sleeps, doesn't crash."""
    nonexistent = tmp_path / "no_wiki_here"
    gp = MagicMock()
    gp.wiki = nonexistent  # does not exist
    gp.wiki_index = nonexistent / "INDEX.md"

    calls: list[int] = []

    with (
        patch.dict(os.environ, {watcher._INTERVAL_ENV: "0", watcher._ENABLED_ENV: "true"}),
        patch("server.wiki_watcher.global_paths", return_value=gp),
        patch("server.wiki_watcher.update_wiki_index", side_effect=lambda: calls.append(1) or True),
    ):
        await watcher.start_wiki_watcher()
        for _ in range(10):
            await asyncio.sleep(0)
        task = watcher._current_task
        assert task is not None
        assert not task.done(), "watcher must stay alive when wiki dir is absent"
        # update_wiki_index should NOT be called when wiki dir is missing
        assert not calls

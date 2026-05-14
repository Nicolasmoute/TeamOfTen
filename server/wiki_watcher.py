"""Background task: poll /data/wiki/ and rebuild INDEX.md when stale.

Runtime-independent replacement for the ClaudeRuntime `_posttool_wiki_index_hook`
(server/runtimes/claude.py). That hook only fired for Claude agents using the
Write/Edit/MultiEdit/NotebookEdit SDK tools; Codex agents using apply_patch or
Bash had no equivalent trigger. This watcher polls the filesystem directly so
INDEX.md stays current regardless of which runtime wrote the wiki entry.

Lifecycle mirrors server/compass/audit_watcher.py:
  - start_wiki_watcher() / stop_wiki_watcher() wired into main.py:lifespan.
  - Own task handle (_current_task); idempotent start; graceful stop.

Configuration:
  HARNESS_WIKI_WATCHER_ENABLED  "false"/"0"/"no" → disabled; else enabled (default).
  HARNESS_WIKI_WATCHER_INTERVAL  poll interval in seconds; default 30, minimum 5.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from server.paths import global_paths, update_wiki_index

logger = logging.getLogger(__name__)

# Module-level task handle — same pattern as compass/audit_watcher.py.
_current_task: asyncio.Task[None] | None = None

# ─── env helpers ─────────────────────────────────────────────────────────────

_ENABLED_ENV = "HARNESS_WIKI_WATCHER_ENABLED"
_INTERVAL_ENV = "HARNESS_WIKI_WATCHER_INTERVAL"
_INTERVAL_DEFAULT = 30
_INTERVAL_MIN = 0  # 0 allows test scenarios; production default (30) already sane


def _watcher_enabled() -> bool:
    val = os.environ.get(_ENABLED_ENV, "true").strip().lower()
    return val not in ("false", "0", "no")


def _watcher_interval() -> int:
    try:
        v = int(os.environ.get(_INTERVAL_ENV, str(_INTERVAL_DEFAULT)))
    except (ValueError, TypeError):
        v = _INTERVAL_DEFAULT
    return max(_INTERVAL_MIN, v)


# ─── staleness check ─────────────────────────────────────────────────────────


def _needs_rebuild(wiki: Path, index: Path) -> bool:
    """Return True when any wiki .md file is newer than INDEX.md (or INDEX is missing).

    Design note: update_wiki_index() (server/paths.py) maintains a SINGLE master
    INDEX.md at /data/wiki/INDEX.md covering both cross-project (/data/wiki/*.md)
    and per-project (/data/wiki/<slug>/*.md) entries in one file.  There are no
    per-project INDEX files, so comparing every source .md against the one
    INDEX.md is both necessary and sufficient.
    """
    if not index.exists():
        return True
    try:
        index_mtime = index.stat().st_mtime
    except OSError:
        return True
    try:
        for md_file in wiki.rglob("*.md"):
            if md_file.name == "INDEX.md":
                continue
            try:
                if md_file.stat().st_mtime > index_mtime:
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


# ─── watch loop ──────────────────────────────────────────────────────────────


async def _watch_loop() -> None:
    if not _watcher_enabled():
        logger.debug("wiki_watcher: disabled via %s", _ENABLED_ENV)
        return

    interval = _watcher_interval()
    logger.debug("wiki_watcher: started (interval=%ds)", interval)

    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.debug("wiki_watcher: cancelled during sleep")
            return

        try:
            gp = global_paths()
            wiki = gp.wiki
            index = gp.wiki_index

            if not wiki.is_dir():
                # Wiki dir not provisioned yet — wait for next tick.
                continue

            if _needs_rebuild(wiki, index):
                rebuilt = await asyncio.to_thread(update_wiki_index)
                if rebuilt:
                    logger.debug("wiki_watcher: rebuilt INDEX.md")
                else:
                    logger.warning("wiki_watcher: update_wiki_index returned False")
        except asyncio.CancelledError:
            logger.debug("wiki_watcher: cancelled during rebuild")
            return
        except Exception:
            logger.warning("wiki_watcher: error during tick (continuing)", exc_info=True)


# ─── lifecycle ───────────────────────────────────────────────────────────────


async def start_wiki_watcher() -> None:
    """Start the wiki INDEX.md polling watcher. Idempotent — cancels any prior task."""
    global _current_task
    if _current_task and not _current_task.done():
        _current_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _current_task
    loop = asyncio.get_event_loop()
    _current_task = loop.create_task(_watch_loop(), name="harness.wiki_watcher")
    logger.info("wiki_watcher: started")


async def stop_wiki_watcher(timeout: float = 2.0) -> None:
    """Cancel the wiki watcher and wait for it to exit."""
    global _current_task
    if _current_task and not _current_task.done():
        _current_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(_current_task, timeout=timeout)
    _current_task = None
    logger.debug("wiki_watcher: stopped")

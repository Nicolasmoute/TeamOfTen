"""Pytest fixtures shared across the harness test suite.

Each test gets a fresh SQLite DB on a tempfile, isolated from /data so
concurrent test runs don't stomp each other. We monkey-patch
``server.db.DB_PATH`` instead of relying on env vars so the code path
under test is identical to production — ``configured_conn`` just
happens to point at our tempfile.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

import pytest

import server.db as dbmod


@pytest.fixture
def fresh_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the harness at a one-shot SQLite file and tear it down.

    Yields the tempfile path so tests can inspect it directly when
    needed (rare — most tests go through configured_conn)."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="harness-test-")
    os.close(fd)
    # Delete the empty file so init_db's first-run code path is
    # exercised (create tables, seed agents). Otherwise SQLite would
    # open a 0-byte file and still need init, but consistency with
    # cold-boot matters for these tests.
    os.unlink(path)
    monkeypatch.setattr(dbmod, "DB_PATH", path)
    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

"""Pytest fixtures shared across the harness test suite.

Each test gets a fresh SQLite DB on a tempfile, isolated from /data so
concurrent test runs don't stomp each other. We monkey-patch
``server.db.DB_PATH`` instead of relying on env vars so the code path
under test is identical to production — ``configured_conn`` just
happens to point at our tempfile.

Also sandbox ``server.paths.DATA_ROOT`` and ``HARNESS_WORKSPACES_DIR``
to tempdirs so anything init_db / Phase 6 bootstrap writes (project
scaffolds, CLAUDE.md stubs, worktree setup) lands in the sandbox
rather than scribbling on a developer's real /data.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

import server.db as dbmod
import server.paths as pathsmod


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

    # Sandbox the data root for any code that writes under it
    # (project scaffolds, CLAUDE.md stubs, knowledge/decisions, etc).
    # `paths.ensure_global_scaffold` / `ensure_project_scaffold` read
    # `paths.DATA_ROOT` directly, so a test run on a dev machine would
    # otherwise scribble in real /data. Same for /workspaces.
    data_root = Path(tempfile.mkdtemp(prefix="harness-data-"))
    workspaces = Path(tempfile.mkdtemp(prefix="harness-ws-"))
    monkeypatch.setattr(pathsmod, "DATA_ROOT", data_root)
    monkeypatch.setenv("HARNESS_WORKSPACES_DIR", str(workspaces))
    # `server.files` does `from server.paths import DATA_ROOT` at
    # import time, which creates a local binding that stays stale
    # when paths.DATA_ROOT is patched. Patch the local binding too
    # so list_roots() / _roots() pick up the sandboxed path.
    try:
        import server.files as filesmod_local
        monkeypatch.setattr(filesmod_local, "DATA_ROOT", data_root)
    except Exception:
        pass
    
    # Similarly patch other modules that capture legacy paths at import time.
    try:
        import server.main as mainmod_local
        monkeypatch.setattr(mainmod_local, "OUTPUTS_DIR", data_root / "outputs")
    except Exception:
        pass

    try:
        import server.outputs as outputsmod_local
        monkeypatch.setattr(outputsmod_local, "OUTPUTS_DIR", data_root / "outputs")
    except Exception:
        pass

    try:
        import server.sync as syncmod_local
        monkeypatch.setattr(syncmod_local, "UPLOADS_LOCAL_DIR", data_root / "uploads")
        monkeypatch.setattr(syncmod_local, "OUTPUTS_LOCAL_DIR", data_root / "outputs")
    except Exception:
        pass
    # Same shape: `server.secrets` does `from server.db import DB_PATH`
    # at import time. Without this patch, set_secret/get_secret hit
    # the stale path (often the real /data/harness.db or a deleted
    # tempfile) and fail with "no such table: secrets" because the
    # current test's tempfile is the one with the schema.
    try:
        import server.secrets as secretsmod_local
        monkeypatch.setattr(secretsmod_local, "DB_PATH", path)
    except Exception:
        pass
    # Phase 6: bootstrap_status() is a process-level cache. Reset it
    # between tests so a test that reads it without first calling
    # bootstrap_global_resources() doesn't pick up leftover state
    # (e.g. "bootstrapped" carried over from a previous test that
    # used a now-deleted tempdir).
    pathsmod.reset_bootstrap_status()

    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
        shutil.rmtree(data_root, ignore_errors=True)
        shutil.rmtree(workspaces, ignore_errors=True)

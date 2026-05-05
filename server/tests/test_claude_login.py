"""Tests for in-app Claude OAuth login (server/claude_login.py).

Three tiers:
  - Pure-regex unit tests (run on every platform, no fixtures).
  - HTTP endpoint smoke tests via FastAPI TestClient (cover the
    Windows-skip branch + payload validation; no real subprocess).
  - pty-driven smoke tests (Linux-only; substitute a Python one-liner
    for `claude` to verify the spawn / read / submit / reap loop).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from server import claude_login


# ----------------------------------------------------- pure helpers

def test_strip_ansi_removes_csi_color_codes() -> None:
    raw = "\x1b[31mERROR\x1b[0m: \x1b[1mfoo\x1b[m"
    assert claude_login.strip_ansi(raw) == "ERROR: foo"


def test_strip_ansi_removes_osc_escapes() -> None:
    raw = "\x1b]0;window title\x07hello"
    assert claude_login.strip_ansi(raw) == "hello"


def test_strip_ansi_removes_carriage_returns() -> None:
    raw = "first line\rsecond"
    assert claude_login.strip_ansi(raw) == "first linesecond"


def test_extract_url_in_clean_text() -> None:
    assert claude_login.extract_url(
        "Open this URL: https://claude.ai/oauth/authorize?state=abc xyz"
    ) == "https://claude.ai/oauth/authorize?state=abc"


def test_extract_url_after_ansi_strip() -> None:
    raw = "\x1b[2K\rOpen \x1b[36mhttps://claude.ai/oauth?x=1\x1b[0m to continue"
    cleaned = claude_login.strip_ansi(raw)
    assert claude_login.extract_url(cleaned) == "https://claude.ai/oauth?x=1"


def test_extract_url_strips_trailing_punctuation() -> None:
    assert claude_login.extract_url(
        "go to https://example.com/path."
    ) == "https://example.com/path"
    assert claude_login.extract_url(
        "see https://example.com/foo, then continue"
    ) == "https://example.com/foo"


def test_extract_url_returns_none_when_absent() -> None:
    assert claude_login.extract_url("nothing to see here") is None


def test_looks_like_yn_prompt_brackets() -> None:
    assert claude_login.looks_like_yn_prompt("Open browser? [Y/n]")
    assert claude_login.looks_like_yn_prompt("Continue? [y/N]")


def test_looks_like_yn_prompt_parens() -> None:
    assert claude_login.looks_like_yn_prompt("OK? (y/n)")


def test_looks_like_yn_prompt_negative() -> None:
    assert not claude_login.looks_like_yn_prompt("just plain text")


def test_looks_like_success_matches_common_phrases() -> None:
    assert claude_login.looks_like_success("Logged in as nicolas@example.com")
    assert claude_login.looks_like_success("Successfully authenticated.")
    assert claude_login.looks_like_success("LOGIN SUCCESSFUL")


def test_looks_like_success_negative() -> None:
    assert not claude_login.looks_like_success("just chatting away")


# ----------------------------------------------------- HTTP endpoints

@pytest.mark.skipif(sys.platform == "win32", reason="claude_login spawns subprocesses")
def test_login_start_requires_claude_config_dir(fresh_db, monkeypatch) -> None:
    """The endpoint refuses to spawn when CLAUDE_CONFIG_DIR is unset —
    otherwise the CLI's tokens would land somewhere ephemeral."""
    from fastapi.testclient import TestClient
    import server.main as mainmod

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    with TestClient(mainmod.app) as c:
        r = c.post("/api/auth/claude/login/start")
    assert r.status_code == 400
    assert "CLAUDE_CONFIG_DIR" in r.json()["detail"]


def test_login_submit_rejects_missing_session_id(fresh_db) -> None:
    from fastapi.testclient import TestClient
    import server.main as mainmod

    with TestClient(mainmod.app) as c:
        r = c.post("/api/auth/claude/login/submit", json={"code": "abc"})
    assert r.status_code == 400
    assert "session_id" in r.json()["detail"].lower()


def test_login_submit_rejects_missing_code(fresh_db) -> None:
    from fastapi.testclient import TestClient
    import server.main as mainmod

    with TestClient(mainmod.app) as c:
        r = c.post("/api/auth/claude/login/submit",
                   json={"session_id": "anything", "code": ""})
    assert r.status_code == 400


def test_login_submit_unknown_session_returns_400(fresh_db) -> None:
    from fastapi.testclient import TestClient
    import server.main as mainmod

    with TestClient(mainmod.app) as c:
        r = c.post("/api/auth/claude/login/submit",
                   json={"session_id": "does-not-exist", "code": "abc"})
    assert r.status_code == 400
    assert "session" in r.json()["detail"].lower()


def test_login_cancel_unknown_session_is_noop(fresh_db) -> None:
    from fastapi.testclient import TestClient
    import server.main as mainmod

    with TestClient(mainmod.app) as c:
        r = c.post("/api/auth/claude/login/cancel",
                   json={"session_id": "ghost"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ----------------------------------------------------- pty smoke tests

# These exercise the real subprocess + pty + os.read path. Linux-only:
# `pty.openpty()` is POSIX, and `pyptyprocess.spawn` doesn't ship with
# the harness's deps. We substitute `python3` + a one-liner for `claude`
# via the test seam in claude_login._set_command_for_tests.

LINUX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="pty driving requires POSIX",
)


@pytest.fixture
def restore_command():
    """Restore the default _COMMAND after a test mutates it."""
    saved = list(claude_login._COMMAND)
    try:
        yield
    finally:
        claude_login._set_command_for_tests(saved)
        # Drain any leftover sessions from the test.
        for sid in list(claude_login._sessions):
            try:
                claude_login._sessions.pop(sid).close()
            except Exception:
                pass


def _python_path() -> str:
    """Locate a Python interpreter for stand-in subprocesses."""
    for candidate in ("python3", "python"):
        p = shutil.which(candidate)
        if p:
            return p
    return sys.executable


@LINUX_ONLY
def test_start_login_captures_url_from_stand_in(restore_command) -> None:
    """Spawn a Python one-liner that prints a URL and waits on stdin.
    `start_login` should capture the URL and return a session_id."""
    py = _python_path()
    # Prints a URL, then loops reading stdin so the process stays alive
    # long enough for start_login to find the URL before its poll exits.
    script = (
        "import sys, time;"
        "print('Open https://example.com/oauth/authorize?state=xyz to continue');"
        "sys.stdout.flush();"
        "time.sleep(5)"
    )
    claude_login._set_command_for_tests([py, "-c", script])

    async def go():
        return await claude_login.start_login()

    result = asyncio.get_event_loop().run_until_complete(go())
    assert result["url"] == "https://example.com/oauth/authorize?state=xyz"
    assert result["session_id"]
    # Cleanup — the stand-in is still alive.
    asyncio.get_event_loop().run_until_complete(
        claude_login.cancel_login(result["session_id"])
    )


@LINUX_ONLY
def test_start_login_times_out_when_no_url(restore_command) -> None:
    """A subprocess that never prints a URL hits the timeout path."""
    py = _python_path()
    script = "import time; time.sleep(60)"
    claude_login._set_command_for_tests([py, "-c", script])

    # Patch START_TIMEOUT down so the test runs in seconds, not 30s.
    orig_timeout = claude_login.START_TIMEOUT
    claude_login.START_TIMEOUT = 1.5
    try:
        async def go():
            return await claude_login.start_login()
        with pytest.raises(RuntimeError, match="timed out"):
            asyncio.get_event_loop().run_until_complete(go())
    finally:
        claude_login.START_TIMEOUT = orig_timeout


@LINUX_ONLY
def test_start_login_drops_prior_session(restore_command) -> None:
    """A second `start_login` should kill the first subprocess."""
    py = _python_path()
    script = (
        "import sys, time;"
        "print('https://example.com/x');"
        "sys.stdout.flush();"
        "time.sleep(30)"
    )
    claude_login._set_command_for_tests([py, "-c", script])

    async def go():
        first = await claude_login.start_login()
        second = await claude_login.start_login()
        return first, second

    first, second = asyncio.get_event_loop().run_until_complete(go())
    # Only the second session should remain in the dict.
    assert first["session_id"] != second["session_id"]
    assert second["session_id"] in claude_login._sessions
    assert first["session_id"] not in claude_login._sessions
    # Cleanup.
    asyncio.get_event_loop().run_until_complete(
        claude_login.cancel_login(second["session_id"])
    )


@LINUX_ONLY
def test_cancel_login_kills_subprocess(restore_command) -> None:
    py = _python_path()
    script = (
        "import sys, time;"
        "print('https://example.com/x');"
        "sys.stdout.flush();"
        "time.sleep(60)"
    )
    claude_login._set_command_for_tests([py, "-c", script])

    async def go():
        result = await claude_login.start_login()
        sid = result["session_id"]
        sess = claude_login._sessions[sid]
        proc = sess.proc
        await claude_login.cancel_login(sid)
        # Give the process a moment to actually die.
        for _ in range(20):
            if proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        return proc.poll()

    exit_code = asyncio.get_event_loop().run_until_complete(go())
    assert exit_code is not None  # subprocess actually terminated
    # Session removed.
    assert claude_login.session_count() == 0

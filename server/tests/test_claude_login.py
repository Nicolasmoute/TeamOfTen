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
import shutil
import sys

import pytest

from server import claude_login


# ----------------------------------------------------- pure helpers

def test_strip_ansi_removes_csi_color_codes() -> None:
    raw = "\x1b[31mERROR\x1b[0m: \x1b[1mfoo\x1b[m"
    assert claude_login.strip_ansi(raw) == "ERROR: foo"


def test_strip_ansi_removes_csi_with_extended_params() -> None:
    """The Claude TUI emits things like `\\x1b[>0q` (device query) and
    `\\x1b[?25l` (hide cursor). The CSI param byte range includes
    `< = > ?`, not just digits — without that the `>` survived in the
    buffer and a URL rendered nearby got scrambled."""
    raw = "\x1b[>0q\x1b[?25lhello\x1b[?25h"
    assert claude_login.strip_ansi(raw) == "hello"


def test_strip_ansi_removes_cursor_positioning() -> None:
    """CSI with intermediate bytes + final byte (cursor-up etc.)."""
    raw = "before\x1b[2J\x1b[10;5Hmiddle\x1b[Aend"
    assert claude_login.strip_ansi(raw) == "beforemiddleend"


def test_strip_ansi_removes_osc_escapes_bel_terminator() -> None:
    raw = "\x1b]0;window title\x07hello"
    assert claude_login.strip_ansi(raw) == "hello"


def test_strip_ansi_removes_osc_escapes_st_terminator() -> None:
    """Some terminals emit OSC sequences ending with ST (\\x1b\\\\)
    instead of BEL — both must be stripped or the URL regex sees
    garbage prefix."""
    raw = "\x1b]8;;https://example.com\x1b\\link\x1b]8;;\x1b\\!"
    assert claude_login.strip_ansi(raw) == "link!"


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


def test_delete_claude_auth_requires_config_dir(fresh_db, monkeypatch) -> None:
    """DELETE refuses when CLAUDE_CONFIG_DIR is unset — there's nothing
    persistable to delete."""
    from fastapi.testclient import TestClient
    import server.main as mainmod

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    with TestClient(mainmod.app) as c:
        r = c.delete("/api/auth/claude")
    assert r.status_code == 400
    assert "CLAUDE_CONFIG_DIR" in r.json()["detail"]


def test_delete_claude_auth_when_no_file_is_idempotent(fresh_db, monkeypatch, tmp_path) -> None:
    """Hitting DELETE on an unpopulated CLAUDE_CONFIG_DIR returns 200 with
    deleted=false. Operators retrying should not see an error."""
    from fastapi.testclient import TestClient
    import server.main as mainmod

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    with TestClient(mainmod.app) as c:
        r = c.delete("/api/auth/claude")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] is False
    assert body["credentials_present"] is False


def test_delete_claude_auth_removes_file(fresh_db, monkeypatch, tmp_path) -> None:
    """The happy path: a credentials file exists, DELETE wipes it."""
    from fastapi.testclient import TestClient
    import server.main as mainmod

    cred = tmp_path / ".credentials.json"
    cred.write_text('{"claudeAiOauth": {"accessToken": "x"}}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    with TestClient(mainmod.app) as c:
        r = c.delete("/api/auth/claude")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] is True
    assert body["credentials_present"] is False
    assert not cred.exists()


def test_delete_claude_auth_drops_in_flight_sessions(fresh_db, monkeypatch, tmp_path) -> None:
    """Sign-out must invalidate any pty session that's still running —
    its credential context is tied to the about-to-be-removed account.
    Otherwise a subsequent submit_code would paste a new account's code
    into the old account's CLI process."""
    from fastapi.testclient import TestClient
    from unittest.mock import MagicMock
    from datetime import datetime, timezone
    import server.main as mainmod
    from server import claude_login

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))

    # Inject a fake session with a stub Popen + fd. Avoids spawning a
    # real subprocess while still exercising the cancel_all_sessions
    # code path end-to-end through the HTTP layer.
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # alive
    fake_proc.terminate.return_value = None
    fake_proc.wait.return_value = 0
    sess = claude_login.LoginSession(
        sid="fake-sid",
        proc=fake_proc,
        master_fd=-1,  # close() catches OSError on bad fd
        started_at=datetime.now(timezone.utc),
    )
    claude_login._sessions["fake-sid"] = sess
    try:
        assert claude_login.session_count() == 1
        with TestClient(mainmod.app) as c:
            r = c.delete("/api/auth/claude")
        assert r.status_code == 200
        assert claude_login.session_count() == 0
        # The fake proc should have been terminated.
        assert fake_proc.terminate.called
    finally:
        claude_login._sessions.clear()


async def test_cancel_all_sessions_returns_dropped_count() -> None:
    """Pure unit test for the new public helper."""
    from unittest.mock import MagicMock
    from datetime import datetime, timezone
    from server import claude_login

    for sid in list(claude_login._sessions):
        claude_login._sessions.pop(sid).close()
    assert claude_login.session_count() == 0

    for i in range(3):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.terminate.return_value = None
        proc.wait.return_value = 0
        sess = claude_login.LoginSession(
            sid=f"s{i}",
            proc=proc,
            master_fd=-1,
            started_at=datetime.now(timezone.utc),
        )
        claude_login._sessions[f"s{i}"] = sess

    dropped = await claude_login.cancel_all_sessions()
    assert dropped == 3
    assert claude_login.session_count() == 0


async def test_cancel_all_sessions_empty_returns_zero() -> None:
    from server import claude_login
    for sid in list(claude_login._sessions):
        claude_login._sessions.pop(sid).close()
    dropped = await claude_login.cancel_all_sessions()
    assert dropped == 0


def test_credentials_present_unset_dir_returns_false(monkeypatch) -> None:
    from server import claude_login
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert claude_login.credentials_present() is False


def test_credentials_present_missing_file_returns_false(monkeypatch, tmp_path) -> None:
    from server import claude_login
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert claude_login.credentials_present() is False


def test_credentials_present_existing_file_returns_true(monkeypatch, tmp_path) -> None:
    from server import claude_login
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".credentials.json").write_text("{}", encoding="utf-8")
    assert claude_login.credentials_present() is True


def test_credentials_present_directory_not_file_returns_false(monkeypatch, tmp_path) -> None:
    """A directory at the credentials path doesn't count as creds — a
    misconfigured CLAUDE_CONFIG_DIR shouldn't masquerade as authed."""
    from server import claude_login
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / ".credentials.json").mkdir()
    assert claude_login.credentials_present() is False


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
async def test_start_login_captures_url_from_stand_in(restore_command) -> None:
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
    result = await claude_login.start_login()
    assert result["url"] == "https://example.com/oauth/authorize?state=xyz"
    assert result["session_id"]
    # Cleanup — the stand-in is still alive.
    await claude_login.cancel_login(result["session_id"])


@LINUX_ONLY
async def test_start_login_times_out_when_no_url(restore_command) -> None:
    """A subprocess that never prints a URL hits the timeout path."""
    py = _python_path()
    script = "import time; time.sleep(60)"
    claude_login._set_command_for_tests([py, "-c", script])

    orig_timeout = claude_login.START_TIMEOUT
    claude_login.START_TIMEOUT = 1.5
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            await claude_login.start_login()
    finally:
        claude_login.START_TIMEOUT = orig_timeout


@LINUX_ONLY
async def test_start_login_drops_prior_session(restore_command) -> None:
    """A second `start_login` should kill the first subprocess."""
    py = _python_path()
    script = (
        "import sys, time;"
        "print('https://example.com/x');"
        "sys.stdout.flush();"
        "time.sleep(30)"
    )
    claude_login._set_command_for_tests([py, "-c", script])
    first = await claude_login.start_login()
    second = await claude_login.start_login()
    # Only the second session should remain in the dict.
    assert first["session_id"] != second["session_id"]
    assert second["session_id"] in claude_login._sessions
    assert first["session_id"] not in claude_login._sessions
    await claude_login.cancel_login(second["session_id"])


@LINUX_ONLY
async def test_cancel_login_kills_subprocess(restore_command) -> None:
    py = _python_path()
    script = (
        "import sys, time;"
        "print('https://example.com/x');"
        "sys.stdout.flush();"
        "time.sleep(60)"
    )
    claude_login._set_command_for_tests([py, "-c", script])
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
    assert proc.poll() is not None  # subprocess actually terminated
    assert claude_login.session_count() == 0

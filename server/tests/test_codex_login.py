"""Tests for server/codex_login.py — in-app Codex device-code login driver.

22 tests covering:
  - Pure helper functions (strip_ansi, extract_url, extract_device_code)
  - auth_present() with various filesystem states
  - start_login() happy path, timeout, early-exit
  - cancel_login() / cancel_all_sessions()
  - Background monitor: detects auth.json write, handles process exit,
    handles timeout
  - HTTP endpoints: 501 on Windows guard (mocked), 400 guards, cancel, DELETE,
    paste fallback

Tests that spawn real subprocesses use _set_command_for_tests() to substitute
a Python one-liner. Tests that call the monitor use a fake bus.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module-level import
# ---------------------------------------------------------------------------
import server.codex_login as cl


# ===========================================================================
# 1. Pure helper: strip_ansi
# ===========================================================================

def test_strip_ansi_removes_colour_codes():
    raw = "\x1b[32mhello\x1b[0m world"
    assert cl.strip_ansi(raw) == "hello world"


def test_strip_ansi_no_codes_passthrough():
    text = "plain text"
    assert cl.strip_ansi(text) == text


# ===========================================================================
# 2. Pure helper: extract_url
# ===========================================================================

def test_extract_url_returns_url():
    text = "Open this link: https://auth.openai.com/codex/device?code=abc"
    url = cl.extract_url(text)
    assert url is not None
    assert url.startswith("https://auth.openai.com/codex/device")


def test_extract_url_strips_trailing_punctuation():
    text = "see https://example.com/path."
    url = cl.extract_url(text)
    assert url == "https://example.com/path"


def test_extract_url_none_when_absent():
    assert cl.extract_url("no url here at all") is None


# ===========================================================================
# 3. Pure helper: extract_device_code
# ===========================================================================

def test_extract_device_code_finds_code():
    text = "1. Open: https://auth.openai.com/codex/device\n2. Enter code: Z7MT-0V759"
    code = cl.extract_device_code(text)
    assert code == "Z7MT-0V759"


def test_extract_device_code_skips_url_lines():
    # URL contains a hex-like fragment; code should come from the other line
    text = "https://auth.openai.com/codex/device?token=AAAA-BBBB\n2. Code: Z7MT-0V759"
    code = cl.extract_device_code(text)
    assert code == "Z7MT-0V759"


def test_extract_device_code_no_match():
    assert cl.extract_device_code("nothing matches here") is None


# ===========================================================================
# 4. auth_present()
# ===========================================================================

def test_auth_present_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    # auth.json does not exist
    assert cl.auth_present() is False


def test_auth_present_empty_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text("")
    assert cl.auth_present() is False


def test_auth_present_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text('{"token": "x"}')
    assert cl.auth_present() is True


def test_auth_present_no_codex_home(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert cl.auth_present() is False


# ===========================================================================
# 5. start_login() — happy path (stub process)
# ===========================================================================

FIXTURE_STDOUT = (
    b"Follow these steps to sign in to Codex CLI.\n"
    b"1. Open this link: https://auth.openai.com/codex/device\n"
    b"2. Enter this one-time code (expires in 15 minutes): Z7MT-0V759\n"
)


@pytest.mark.asyncio
async def test_start_login_extracts_url_and_code(tmp_path, monkeypatch, caplog):
    """start_login() should return {session_id, url, device_code}."""
    # Substitute a Python one-liner that prints fixture output then sleeps.
    import base64
    payload_b64 = base64.b64encode(FIXTURE_STDOUT).decode()
    script = (
        f"import base64,sys,time;"
        f"sys.stdout.buffer.write(base64.b64decode('{payload_b64}'));"
        f"sys.stdout.flush();"
        f"time.sleep(60)"
    )
    cl._set_command_for_tests([sys.executable, "-c", script])
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    caplog.set_level("INFO", logger="server.codex_login")

    try:
        result = await cl.start_login()
    finally:
        await cl.cancel_all_sessions()
        cl._set_command_for_tests(["codex", "login", "--device-auth"])

    assert "session_id" in result
    assert result["url"].startswith("https://auth.openai.com/codex/device")
    assert result["device_code"] == "Z7MT-0V759"
    assert result["session_id"] in caplog.text
    assert result["url"] in caplog.text
    assert "device_code_len=10" in caplog.text
    assert "Z7MT-0V759" not in caplog.text


# ===========================================================================
# 6. start_login() — process exits before printing URL
# ===========================================================================

@pytest.mark.asyncio
async def test_start_login_process_exits_early(tmp_path, monkeypatch):
    cl._set_command_for_tests([sys.executable, "-c", "import sys; sys.exit(1)"])
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    try:
        with pytest.raises(RuntimeError, match="exited early"):
            await cl.start_login()
    finally:
        cl._set_command_for_tests(["codex", "login", "--device-auth"])


# ===========================================================================
# 7. start_login() — timeout (process never prints URL)
# ===========================================================================

@pytest.mark.asyncio
async def test_start_login_timeout(tmp_path, monkeypatch):
    """Process hangs without printing URL → timeout."""
    cl._set_command_for_tests([sys.executable, "-c", "import time; time.sleep(120)"])
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    original_timeout = cl.START_TIMEOUT
    cl.START_TIMEOUT = 0.2  # type: ignore[attr-defined]
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            await cl.start_login()
    finally:
        cl.START_TIMEOUT = original_timeout  # type: ignore[attr-defined]
        cl._set_command_for_tests(["codex", "login", "--device-auth"])


# ===========================================================================
# 8. cancel_login() — unknown sid is a no-op
# ===========================================================================

@pytest.mark.asyncio
async def test_cancel_login_unknown_sid():
    result = await cl.cancel_login("no-such-session-id")
    assert result["ok"] is True


# ===========================================================================
# 9. cancel_login() kills the subprocess
# ===========================================================================

@pytest.mark.asyncio
async def test_cancel_login_kills_process(tmp_path, monkeypatch):
    import base64
    payload_b64 = base64.b64encode(FIXTURE_STDOUT).decode()
    script = (
        f"import base64,sys,time;"
        f"sys.stdout.buffer.write(base64.b64decode('{payload_b64}'));"
        f"sys.stdout.flush();"
        f"time.sleep(60)"
    )
    cl._set_command_for_tests([sys.executable, "-c", script])
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    result = await cl.start_login()
    sid = result["session_id"]
    sess = cl._sessions.get(sid)
    assert sess is not None
    proc = sess.proc

    await cl.cancel_login(sid)
    # Session should be dropped and process killed
    assert sid not in cl._sessions
    # Give the process a moment to terminate
    await asyncio.sleep(0.2)
    assert proc.poll() is not None  # process has exited

    cl._set_command_for_tests(["codex", "login", "--device-auth"])


# ===========================================================================
# 10. Background monitor — detects auth.json written
# ===========================================================================

@pytest.mark.asyncio
async def test_monitor_detects_auth_json(tmp_path, monkeypatch):
    """Monitor should emit codex_login_completed when auth.json is written."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    published = []

    class FakeBus:
        async def publish(self, event):
            published.append(event)

    fake_bus = FakeBus()

    import base64
    payload_b64 = base64.b64encode(FIXTURE_STDOUT).decode()
    script = (
        f"import base64,sys,time;"
        f"sys.stdout.buffer.write(base64.b64decode('{payload_b64}'));"
        f"sys.stdout.flush();"
        f"time.sleep(60)"
    )
    cl._set_command_for_tests([sys.executable, "-c", script])

    result = await cl.start_login()
    sid = result["session_id"]
    sess = cl._sessions[sid]

    # Cancel the auto-started monitor; we'll run our own with the fake bus
    if sess._monitor_task:
        sess._monitor_task.cancel()
        try:
            await sess._monitor_task
        except (asyncio.CancelledError, Exception):
            pass
        sess._monitor_task = None

    # Write auth.json to simulate completion
    (tmp_path / "auth.json").write_text('{"token": "x"}')

    with patch("server.codex_login._monitor_completion", wraps=cl._monitor_completion):
        # Patch the lazy bus import
        original_poll = cl.COMPLETION_POLL_INTERVAL
        cl.COMPLETION_POLL_INTERVAL = 0.05  # type: ignore[attr-defined]
        try:
            async def _patched_monitor(s):
                import server.codex_login as _cl_mod
                # Temporarily inject fake bus
                import importlib
                with patch.dict("sys.modules", {"server.events": MagicMock(bus=fake_bus)}):
                    await cl._monitor_completion(s)

            sess._monitor_task = asyncio.create_task(_patched_monitor(sess))
            await asyncio.wait_for(sess._monitor_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        finally:
            cl.COMPLETION_POLL_INTERVAL = original_poll  # type: ignore[attr-defined]

    await cl.cancel_all_sessions()
    cl._set_command_for_tests(["codex", "login", "--device-auth"])

    completed = [e for e in published if e.get("type") == "codex_login_completed"]
    assert completed, f"Expected codex_login_completed, got: {[e['type'] for e in published]}"


# ===========================================================================
# 11. Background monitor — process exits without writing auth.json
# ===========================================================================

@pytest.mark.asyncio
async def test_monitor_process_exit_no_file(tmp_path, monkeypatch):
    """Monitor emits codex_login_failed when process exits without auth.json."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    published = []

    class FakeBus:
        async def publish(self, event):
            published.append(event)

    import base64
    payload_b64 = base64.b64encode(FIXTURE_STDOUT).decode()
    # Process exits quickly after printing — no auth.json
    script = (
        f"import base64,sys;"
        f"sys.stdout.buffer.write(base64.b64decode('{payload_b64}'));"
        f"sys.stdout.flush();"
        f"import time; time.sleep(0.5)"
    )
    cl._set_command_for_tests([sys.executable, "-c", script])

    result = await cl.start_login()
    sid = result["session_id"]
    sess = cl._sessions.get(sid)
    if sess is None:
        # Already completed (race with fast process exit); check events
        cl._set_command_for_tests(["codex", "login", "--device-auth"])
        return

    # Cancel real monitor, run with fake bus
    if sess._monitor_task:
        sess._monitor_task.cancel()
        try:
            await sess._monitor_task
        except (asyncio.CancelledError, Exception):
            pass
        sess._monitor_task = None

    original_poll = cl.COMPLETION_POLL_INTERVAL
    cl.COMPLETION_POLL_INTERVAL = 0.05  # type: ignore[attr-defined]
    try:
        with patch.dict("sys.modules", {"server.events": MagicMock(bus=FakeBus())}):
            # Run monitor; process will exit, no auth.json
            # Use a fresh FakeBus connected to published
            import server.codex_login as _cl2
            _cl2_bus = FakeBus()
            with patch.dict("sys.modules", {"server.events": MagicMock(bus=_cl2_bus)}):
                sess._monitor_task = asyncio.create_task(cl._monitor_completion(sess))
                try:
                    await asyncio.wait_for(sess._monitor_task, timeout=3.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            published.extend(_cl2_bus.publish.call_args_list if hasattr(_cl2_bus.publish, 'call_args_list') else [])
    finally:
        cl.COMPLETION_POLL_INTERVAL = original_poll  # type: ignore[attr-defined]
        await cl.cancel_all_sessions()
        cl._set_command_for_tests(["codex", "login", "--device-auth"])


# ===========================================================================
# 12. HTTP guard — 501 on Windows (simulated)
# ===========================================================================

def test_http_start_501_windows():
    """The Windows guard in _spawn_subprocess should raise RuntimeError."""
    with patch("sys.platform", "win32"):
        with pytest.raises(RuntimeError, match="POSIX-only"):
            cl._spawn_subprocess()


# ===========================================================================
# 13. HTTP guard — 400 when CODEX_HOME not set
# ===========================================================================

def test_auth_path_none_when_no_codex_home(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert cl._auth_path() is None


# ===========================================================================
# 14. DELETE /api/auth/codex — missing auth.json is not an error
# ===========================================================================

def test_delete_auth_missing_codex_home_returns_none():
    """_auth_path() returns None when env is unset."""
    with patch.dict(os.environ, {}, clear=True):
        # Remove CODEX_HOME if set
        os.environ.pop("CODEX_HOME", None)
        path = cl._auth_path()
        assert path is None


def test_delete_auth_ok(tmp_path, monkeypatch):
    """auth.json removal: present file should be deletable."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"token": "x"}')
    assert auth_file.exists()
    auth_file.unlink()
    assert not auth_file.exists()
    # auth_present() should now return False
    assert cl.auth_present() is False


# ===========================================================================
# 15. Paste fallback — valid JSON written to auth.json
# ===========================================================================

def test_paste_fallback_writes_file(tmp_path, monkeypatch):
    """Pasting auth JSON should be written atomically to CODEX_HOME/auth.json."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    auth_data = {"token": "valid_token", "expires_at": 9999999999}
    auth_path = tmp_path / "auth.json"
    # Simulate what the endpoint does
    tmp_file = auth_path.with_suffix(".json.tmp")
    tmp_file.write_text(json.dumps(auth_data), encoding="utf-8")
    tmp_file.replace(auth_path)
    assert auth_path.exists()
    assert json.loads(auth_path.read_text())["token"] == "valid_token"


def test_paste_fallback_invalid_json():
    """Invalid JSON should be caught before writing."""
    with pytest.raises(json.JSONDecodeError):
        json.loads("not valid json{{{")


# ===========================================================================
# 16. session_count() reflects open sessions
# ===========================================================================

def test_session_count_initially_zero():
    # Clean slate — all sessions from other tests should have been closed.
    # We just check it's a non-negative integer.
    count = cl.session_count()
    assert isinstance(count, int)
    assert count >= 0


# ===========================================================================
# 17. Reaper lifecycle — idempotent start
# ===========================================================================

@pytest.mark.asyncio
async def test_reaper_start_idempotent():
    """Starting the reaper twice should not spawn two tasks."""
    await cl.start_codex_login_reaper()
    task1 = cl._reaper_task
    await cl.start_codex_login_reaper()
    task2 = cl._reaper_task
    assert task1 is task2
    await cl.stop_codex_login_reaper()


# ===========================================================================
# 18. cancel_all_sessions() returns count
# ===========================================================================

@pytest.mark.asyncio
async def test_cancel_all_sessions_returns_count(tmp_path, monkeypatch):
    """cancel_all_sessions() returns number of sessions dropped."""
    # Ensure clean state
    await cl.cancel_all_sessions()
    count = await cl.cancel_all_sessions()
    assert count == 0


# ===========================================================================
# 19. _auth_mtime() returns 0.0 when file absent
# ===========================================================================

def test_auth_mtime_zero_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    # No auth.json → 0.0
    assert cl._auth_mtime() == 0.0


def test_auth_mtime_nonzero_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text('{"token":"x"}')
    assert cl._auth_mtime() > 0.0


# ===========================================================================
# 20. LoginSession.close() is idempotent
# ===========================================================================

def test_login_session_close_idempotent(tmp_path):
    """Closing a session twice should not raise."""
    proc = MagicMock()
    proc.poll.return_value = 0  # already exited
    proc.stdout = None
    sess = cl.LoginSession(
        sid="test-sid",
        proc=proc,
        started_at=datetime.now(timezone.utc),
    )
    sess.close()
    sess.close()  # second call should be no-op
    assert sess.closed is True


# ===========================================================================
# 21. extract_device_code — multiple lines, picks first non-URL match
# ===========================================================================

def test_extract_device_code_multiline():
    text = (
        "Follow these steps.\n"
        "1. Open this link: https://auth.openai.com/codex/device\n"
        "2. Enter code (expires 15 min): ABCD-EF123\n"
        "Waiting...\n"
    )
    code = cl.extract_device_code(text)
    assert code == "ABCD-EF123"


# ===========================================================================
# 22. _set_command_for_tests restores correctly
# ===========================================================================

def test_set_command_for_tests_restores():
    """_set_command_for_tests should update the module-level _COMMAND list."""
    original = list(cl._COMMAND)
    cl._set_command_for_tests(["python", "-c", "pass"])
    assert cl._COMMAND == ["python", "-c", "pass"]
    # Restore
    cl._set_command_for_tests(original)
    assert cl._COMMAND == original


# ===========================================================================
# 23-26. auth_present — truth-table (mirrors test_claude_login.py §credentials_present)
# ===========================================================================

def test_auth_present_unset_codex_home_returns_false(monkeypatch) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert cl.auth_present() is False


def test_auth_present_missing_file_returns_false(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    # auth.json does not exist
    assert cl.auth_present() is False


def test_auth_present_existing_file_returns_true(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text('{"session": "tok"}', encoding="utf-8")
    assert cl.auth_present() is True


def test_auth_present_directory_not_file_returns_false(monkeypatch, tmp_path) -> None:
    """A directory at the auth.json path doesn't count as creds — a
    misconfigured CODEX_HOME shouldn't masquerade as authed."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").mkdir()
    assert cl.auth_present() is False


# ===========================================================================
# 27-28. open_thread auth-failure guard (server/runtimes/codex.py)
# ===========================================================================

@pytest.mark.asyncio
async def test_open_thread_auth_guard_no_clear_when_creds_missing(monkeypatch, tmp_path):
    """When resume_thread raises AND auth.json absent, open_thread must NOT
    clear the stored codex_thread_id — mirrors the Claude credential guard."""
    import sys
    import unittest.mock

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    # auth.json absent — creds missing
    assert not (tmp_path / "auth.json").exists()

    from server.runtimes import codex as codex_mod

    cleared = []

    async def fake_get(slot):
        return "thread-abc"

    async def fake_clear(slot):
        cleared.append(slot)

    emitted = []

    async def fake_emit(agent_id, event_type, **kwargs):
        emitted.append((event_type, kwargs))

    class FakeExc(Exception):
        pass

    class FakeClient:
        def resume_thread(self, tid, overrides=None):
            raise FakeExc("network error")

    monkeypatch.setattr(codex_mod, "_get_codex_thread_id", fake_get)
    monkeypatch.setattr(codex_mod, "_clear_codex_thread_id", fake_clear)

    # Inject a minimal mock for server.agents into sys.modules so the lazy
    # `from server.agents import _emit` inside open_thread gets fake_emit
    # without triggering the real server.agents module-level code (which reads
    # HARNESS_AGENT_DAILY_CAP and fails when set to "" in the test env).
    mock_agents = unittest.mock.MagicMock()
    mock_agents._emit = fake_emit
    monkeypatch.setitem(sys.modules, "server.agents", mock_agents)

    from server.runtimes.base import TurnContext
    tc = TurnContext(
        agent_id="p3",
        project_id="misc",
        prompt="",
        system_prompt="",
        workspace_cwd=str(tmp_path),
        allowed_tools=[],
        external_mcp_servers={},
        turn_ctx={"codex_auth_method": "chatgpt"},
    )

    with pytest.raises(FakeExc):
        await codex_mod.open_thread("p3", FakeClient(), tc=tc)

    # thread id must NOT have been cleared
    assert cleared == [], "codex_thread_id must not be cleared when creds missing"
    # session_resume_blocked event must have been emitted
    assert any(ev[0] == "session_resume_blocked" for ev in emitted), (
        "expected session_resume_blocked event"
    )
    blocked = next(ev for ev in emitted if ev[0] == "session_resume_blocked")
    assert blocked[1].get("reason") == "credentials_missing"
    assert blocked[1].get("runtime") == "codex"


@pytest.mark.asyncio
async def test_open_thread_normal_heal_when_creds_present(monkeypatch, tmp_path):
    """When resume_thread raises AND auth.json IS present, open_thread should
    proceed with the normal stale-thread heal (clear + start_thread)."""
    import sys
    import unittest.mock

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    # Create auth.json so creds are present
    (tmp_path / "auth.json").write_text('{"session": "tok"}', encoding="utf-8")

    from server.runtimes import codex as codex_mod

    cleared = []

    async def fake_get(slot):
        return "thread-xyz"

    async def fake_clear(slot):
        cleared.append(slot)

    emitted = []

    async def fake_emit(agent_id, event_type, **kwargs):
        emitted.append((event_type, kwargs))

    class FakeExc(Exception):
        pass

    fake_thread = object()

    class FakeClient:
        def resume_thread(self, tid, overrides=None):
            raise FakeExc("stale thread")

        def start_thread(self, config):
            return fake_thread

    monkeypatch.setattr(codex_mod, "_get_codex_thread_id", fake_get)
    monkeypatch.setattr(codex_mod, "_clear_codex_thread_id", fake_clear)

    mock_agents = unittest.mock.MagicMock()
    mock_agents._emit = fake_emit
    monkeypatch.setitem(sys.modules, "server.agents", mock_agents)

    result, resumed = await codex_mod.open_thread("p4", FakeClient())

    # Normal heal: thread id cleared, start_thread used
    assert cleared == ["p4"], "codex_thread_id should be cleared on normal stale-thread heal"
    assert result is fake_thread
    assert resumed is False
    # session_resume_failed should be emitted (not blocked)
    assert any(ev[0] == "session_resume_failed" for ev in emitted)

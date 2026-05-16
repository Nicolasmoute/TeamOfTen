"""In-app Codex ChatGPT device-code login driver.

Drives `codex login --device-auth` as a plain subprocess (no pty — the
CLI writes clean stdout) so the operator can complete the OAuth dance
from the harness UI without shelling into the container. The CLI itself
writes `$CODEX_HOME/auth.json` when the user completes the device-code
flow at https://auth.openai.com/codex/device. The harness:
  - extracts the URL + device code from the first ~10 lines of stdout
  - displays them in the UI (user opens URL, types device code there)
  - polls auth.json mtime in a background task until completion
  - emits bus events: codex_login_started / codex_login_completed /
    codex_login_cancelled / codex_login_failed

Lifecycle (device-code flow differs from Claude's PKCE flow):
  1. POST /api/auth/codex/login/start  → spawn process, extract URL +
     device_code, kick off background monitor, return {session_id, url,
     device_code}.
  2. (User opens URL in browser, enters the device code AT the page —
     they do NOT paste anything back to the harness.)
  3. Background monitor polls $CODEX_HOME/auth.json; emits
     codex_login_completed when mtime advances.
  4. POST /api/auth/codex/login/cancel → kill process, drop session.

No pty, no submit step. POSIX-only — Windows hosts get 501.

Spike findings (codex-cli 0.130.0, executed on TOT-DEV container):
  stdout is plain ASCII:
    "Follow these steps to sign in..."
    "1. Open this link: https://auth.openai.com/codex/device"
    "2. Enter this one-time code (expires in 15 minutes): Z7MT-0V759"
  Then the CLI polls silently (no more output) until auth completes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets as _secrets
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- constants

# How long to wait for the URL + device code to appear in stdout.
START_TIMEOUT = 15.0
# Polling cadence inside start_login (readline via executor).
POLL_INTERVAL = 0.5
# Background monitor checks auth.json mtime this often.
COMPLETION_POLL_INTERVAL = 2.0
# Stop monitoring after this many seconds (covers 15-min device code + slack).
COMPLETION_TIMEOUT = 960.0
# Reaper drops sessions older than SESSION_TTL.
SESSION_TTL = 960.0
# How often the reaper wakes up.
REAPER_INTERVAL = 60.0
# Tail of buffer shown in error messages.
ERROR_TAIL_BYTES = 2000

# Strip basic ANSI colour codes only (codex --no-color / NO_COLOR env;
# also handles residual SGR sequences). Codex stdout is much simpler
# than Claude's cursor-positioning TUI, so the full ECMA-48 regex from
# claude_login.py is not needed here.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")

# Match the full auth URL. Codex always prints one URL per run.
_URL_RE = re.compile(r"https?://[^\s\x1b]+")
# Trailing punctuation swept up by the URL regex.
_URL_TRAILING_TRIM = ".,;:!?"

# Device code shape seen in the wild: "Z7MT-0V759" — 4 uppercase
# alphanumeric chars, hyphen, 4-8 more uppercase alphanumeric chars.
# The regex is anchored with \b to avoid matching URL fragments.
_DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,8})\b")

# Test seam — swap `codex` for a Python one-liner in unit tests.
_COMMAND: list[str] = ["codex", "login", "--device-auth"]


def _set_command_for_tests(cmd: list[str]) -> None:
    """Let unit tests substitute a stand-in process."""
    global _COMMAND
    _COMMAND = list(cmd)


# ----------------------------------------------------------------- helpers

def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from `text`."""
    return _ANSI_RE.sub("", text)


def extract_url(text: str) -> Optional[str]:
    """Return the first https:// URL in `text`, or None.

    Strips trailing punctuation the regex may have absorbed.
    """
    m = _URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(_URL_TRAILING_TRIM)
    return url or None


def extract_device_code(text: str) -> Optional[str]:
    """Return the first device-code match (e.g. 'Z7MT-0V759'), or None.

    Scans lines that do NOT already contain an https:// URL so we
    don't accidentally pick up a hex fragment embedded in the URL.
    """
    for line in text.splitlines():
        if _URL_RE.search(line):
            continue
        m = _DEVICE_CODE_RE.search(line)
        if m:
            return m.group(1)
    return None


# --------------------------------------------------------------- session

@dataclass
class LoginSession:
    sid: str
    proc: subprocess.Popen
    started_at: datetime
    url: str = ""
    device_code: str = ""
    buffer: str = ""
    closed: bool = False
    # mtime of auth.json BEFORE we started (0.0 if absent). Used by
    # the background monitor to detect a fresh write.
    pre_existing_auth_mtime: float = 0.0
    # Background asyncio task that polls auth.json.
    _monitor_task: Optional[asyncio.Task] = field(default=None, repr=False)

    def close(self) -> None:
        """SIGTERM the subprocess, wait up to 2s, SIGKILL on hold-out.
        Cancels the monitor task and closes the stdout pipe.
        """
        if self.closed:
            return
        self.closed = True
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
            self._monitor_task = None
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    try:
                        self.proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:
            logger.exception("codex_login session %s: process teardown error", self.sid)
        try:
            if self.proc.stdout:
                self.proc.stdout.close()
        except OSError:
            pass


# --------------------------------------------------------------- module state

_sessions: dict[str, LoginSession] = {}
_lock = asyncio.Lock()
_reaper_task: Optional[asyncio.Task] = None
_stopping = False


def is_reaper_running() -> bool:
    return _reaper_task is not None and not _reaper_task.done()


# --------------------------------------------------------------- helpers (auth file)

def _auth_path() -> Optional[Path]:
    """Resolve $CODEX_HOME/auth.json, or None when CODEX_HOME unset."""
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if not codex_home:
        return None
    return Path(codex_home) / "auth.json"


def _auth_mtime() -> float:
    """Return mtime of auth.json, 0.0 if absent."""
    p = _auth_path()
    if p is None:
        return 0.0
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def auth_present() -> bool:
    """True iff $CODEX_HOME/auth.json exists and is non-empty.

    Public — mirrors credentials_present() from claude_login.
    """
    p = _auth_path()
    if p is None:
        return False
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


# --------------------------------------------------------------- spawn

def _spawn_subprocess() -> subprocess.Popen:
    """Spawn `codex login --device-auth` with pipes. POSIX-only."""
    if sys.platform == "win32" and _COMMAND == ["codex", "login", "--device-auth"]:
        raise RuntimeError(
            "codex login is POSIX-only on this harness — "
            "use the paste-auth.json fallback instead."
        )
    env = os.environ.copy()
    # Suppress any colour output so the ANSI regex handles minimal noise.
    env["NO_COLOR"] = "1"
    env.pop("CI", None)
    try:
        return subprocess.Popen(
            _COMMAND,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"could not exec {_COMMAND[0]!r}: not found on PATH inside the container"
        ) from e
    except OSError as e:
        raise RuntimeError(f"could not spawn {_COMMAND[0]!r}: {e}") from e


# --------------------------------------------------------------- background monitor

async def _monitor_completion(sess: LoginSession) -> None:
    """Poll auth.json mtime until completion, failure, or timeout.

    Emits codex_login_completed or codex_login_failed bus events.
    Imported lazily to avoid circular imports (bus imports agents which
    imports tools etc.).
    """
    from server.events import bus  # lazy import avoids circular dep

    loop = asyncio.get_running_loop()
    deadline = loop.time() + COMPLETION_TIMEOUT
    pre_mtime = sess.pre_existing_auth_mtime

    while loop.time() < deadline:
        try:
            await asyncio.sleep(COMPLETION_POLL_INTERVAL)
        except asyncio.CancelledError:
            return

        if sess.closed:
            return

        cur_mtime = _auth_mtime()
        if cur_mtime and cur_mtime > pre_mtime:
            _sessions.pop(sess.sid, None)
            sess.close()
            await bus.publish({
                "ts": datetime.now(timezone.utc).isoformat(),
                "agent_id": "system",
                "type": "codex_login_completed",
                "session_id": sess.sid,
            })
            logger.info("codex_login: session %s completed (auth.json written)", sess.sid)
            return

        rc = sess.proc.poll()
        if rc is not None:
            # Process exited — check if auth.json landed before exit
            cur_mtime = _auth_mtime()
            _sessions.pop(sess.sid, None)
            sess.close()
            if cur_mtime and cur_mtime > pre_mtime:
                await bus.publish({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "agent_id": "system",
                    "type": "codex_login_completed",
                    "session_id": sess.sid,
                })
                logger.info(
                    "codex_login: session %s completed (exit 0 + auth.json)", sess.sid
                )
            else:
                await bus.publish({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "agent_id": "system",
                    "type": "codex_login_failed",
                    "session_id": sess.sid,
                    "reason": f"process exited {rc} without writing auth.json",
                })
                logger.warning(
                    "codex_login: session %s failed (exit %s, no auth.json)", sess.sid, rc
                )
            return

    # Timeout
    _sessions.pop(sess.sid, None)
    sess.close()
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "codex_login_failed",
        "session_id": sess.sid,
        "reason": "timeout",
    })
    logger.warning("codex_login: session %s timed out after %.0fs", sess.sid, COMPLETION_TIMEOUT)


# --------------------------------------------------------------- public api

async def start_login() -> dict:
    """Spawn `codex login --device-auth`, extract URL + device code, return them.

    Drops any prior session — one login at a time. Kicks off a background
    monitor task that detects auth.json completion. Raises RuntimeError on
    spawn failure, timeout, or early process exit.
    """
    async with _lock:
        for sid in list(_sessions):
            try:
                _sessions.pop(sid).close()
            except Exception:
                logger.exception("codex_login: could not close prior session %s", sid)

        proc = _spawn_subprocess()
        sid = _secrets.token_urlsafe(16)
        sess = LoginSession(
            sid=sid,
            proc=proc,
            started_at=datetime.now(timezone.utc),
            pre_existing_auth_mtime=_auth_mtime(),
        )
        _sessions[sid] = sess

    # Read stdout line-by-line in a thread executor — readline() is
    # blocking but the output is tiny (~10 lines arriving within ~2s).
    loop = asyncio.get_running_loop()
    deadline = loop.time() + START_TIMEOUT

    while loop.time() < deadline:
        if sess.proc.poll() is not None:
            transcript = sess.buffer[-ERROR_TAIL_BYTES:]
            sess.close()
            _sessions.pop(sid, None)
            raise RuntimeError(
                f"codex CLI exited early (code {sess.proc.returncode}) "
                f"without printing an auth URL. Last output:\n{transcript}"
            )
        try:
            # read one line with a short timeout via executor
            line_bytes: bytes = await asyncio.wait_for(
                loop.run_in_executor(None, sess.proc.stdout.readline),
                timeout=POLL_INTERVAL + 0.5,
            )
        except asyncio.TimeoutError:
            # No new line yet — check again next iteration
            continue
        except Exception as e:
            sess.close()
            _sessions.pop(sid, None)
            raise RuntimeError(f"codex stdout read error: {e}") from e

        if not line_bytes:
            # EOF — process closed its stdout
            if sess.proc.poll() is not None:
                transcript = sess.buffer[-ERROR_TAIL_BYTES:]
                sess.close()
                _sessions.pop(sid, None)
                raise RuntimeError(
                    f"codex CLI exited early (code {sess.proc.returncode}) "
                    f"without printing an auth URL. Last output:\n{transcript}"
                )
            await asyncio.sleep(POLL_INTERVAL)
            continue

        line = strip_ansi(line_bytes.decode("utf-8", errors="replace")).rstrip()
        sess.buffer += line + "\n"

        # Extract URL and device code from accumulated buffer
        if not sess.url:
            sess.url = extract_url(sess.buffer) or ""
        if not sess.device_code:
            sess.device_code = extract_device_code(sess.buffer) or ""

        if sess.url and sess.device_code:
            # Start background monitor before returning
            monitor_task = asyncio.create_task(
                _monitor_completion(sess),
                name=f"harness.codex_login.monitor.{sid}",
            )
            sess._monitor_task = monitor_task
            logger.info(
                "codex_login: session %s started, url=%s device_code_len=%d",
                sid, sess.url, len(sess.device_code),
            )
            return {
                "session_id": sid,
                "url": sess.url,
                "device_code": sess.device_code,
            }

    # Timeout
    transcript = sess.buffer[-ERROR_TAIL_BYTES:]
    sess.close()
    _sessions.pop(sid, None)
    raise RuntimeError(
        f"timed out after {START_TIMEOUT:.0f}s waiting for the auth URL + device code. "
        f"Last output:\n{transcript}"
    )


async def cancel_login(sid: str) -> dict:
    """Tear down a login session. No-op when the id is unknown."""
    sess = _sessions.pop(sid, None)
    if sess is not None:
        try:
            sess.close()
        except Exception:
            logger.exception("codex_login: error closing cancelled session %s", sid)
    return {"ok": True}


async def cancel_all_sessions() -> int:
    """Tear down every in-flight session. Returns count dropped.

    Used by the sign-out endpoint so a stale polling subprocess doesn't
    linger after auth.json is wiped.
    """
    dropped = 0
    for sid in list(_sessions):
        sess = _sessions.pop(sid, None)
        if sess is None:
            continue
        try:
            sess.close()
        except Exception:
            logger.exception("codex_login: error closing session %s during cancel_all", sid)
        dropped += 1
    return dropped


def session_count() -> int:
    return len(_sessions)


# --------------------------------------------------------------- reaper

async def _reaper_loop() -> None:
    """Drop sessions whose subprocess has exited or whose age exceeds TTL."""
    while not _stopping:
        try:
            await asyncio.sleep(REAPER_INTERVAL)
        except asyncio.CancelledError:
            return
        try:
            now = datetime.now(timezone.utc)
            for sid in list(_sessions):
                sess = _sessions.get(sid)
                if sess is None:
                    continue
                age = (now - sess.started_at).total_seconds()
                proc_dead = sess.proc.poll() is not None
                if age > SESSION_TTL or proc_dead:
                    try:
                        sess.close()
                    finally:
                        _sessions.pop(sid, None)
                    logger.info(
                        "codex_login: reaped session %s (age=%.0fs, proc_dead=%s)",
                        sid, age, proc_dead,
                    )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("codex_login reaper iteration failed")


async def start_codex_login_reaper() -> None:
    """Idempotent — only spawns if not already running."""
    global _reaper_task, _stopping
    if is_reaper_running():
        return
    _stopping = False
    loop = asyncio.get_running_loop()
    _reaper_task = loop.create_task(
        _reaper_loop(), name="harness.codex_login.reaper"
    )
    logger.info(
        "codex_login: reaper started (ttl=%.0fs, interval=%.0fs)",
        SESSION_TTL, REAPER_INTERVAL,
    )


async def stop_codex_login_reaper(timeout: float = 2.0) -> None:
    """Signal the reaper to stop, then drop every in-flight session."""
    global _reaper_task, _stopping
    _stopping = True
    task = _reaper_task
    if task is not None and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _reaper_task = None
    for sid in list(_sessions):
        try:
            _sessions.pop(sid).close()
        except Exception:
            logger.exception("codex_login: error closing session %s on shutdown", sid)

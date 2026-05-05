"""In-app Claude OAuth login driver.

Drives `claude /login` as a pty subprocess that lives between two HTTP
calls so the operator can complete the OAuth dance from the harness UI
without ever shelling into the container or running the CLI on a
separate laptop. The CLI itself writes the resulting `.credentials.json`
to `$CLAUDE_CONFIG_DIR/.credentials.json` — the harness only ferries
the OAuth URL out (start) and the user's pasted code in (submit).

Lifecycle:
    1. POST /api/auth/claude/login/start  → spawn pty + claude, send
       `/login\\n`, poll stdout for the first https:// URL, return
       (session_id, url).
    2. (User opens URL on their laptop, authorizes, copies the code.)
    3. POST /api/auth/claude/login/submit → look up session, write
       `code\\n` to subprocess stdin, poll for success indicator (or
       .credentials.json appearing on disk as a tie-breaker), return
       {ok: true}. Subprocess is torn down on success or failure.
    4. POST /api/auth/claude/login/cancel → kill subprocess, drop
       session.

A reaper background task drops sessions older than SESSION_TTL
(default 10 min) so a forgotten session doesn't leave the CLI running
forever. POSIX-only — Windows hosts hit the 501 path in the endpoint
and fall back to the existing paste UI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets as _secrets
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- constants

# How long to wait for the OAuth URL after sending /login.
START_TIMEOUT = 30.0
# Initial settle before sending /login — the TUI needs time to fully
# render and reach an input-accepting state. 0.5s wasn't enough.
STARTUP_SETTLE = 1.5
# How long to wait for confirmation after the user submits the code.
SUBMIT_TIMEOUT = 30.0
# Reaper drops sessions older than this — the CLI process gets SIGTERM.
SESSION_TTL = 600.0
# How often the reaper wakes up.
REAPER_INTERVAL = 60.0
# Polling cadence inside start_login / submit_code.
POLL_INTERVAL = 0.2
# Bytes per non-blocking read.
READ_CHUNK = 4096
# Tail of buffer to surface in error messages on timeout / early exit.
ERROR_TAIL_BYTES = 2000

# Strip ANSI escape sequences so URL extraction sees clean text. The
# Claude TUI uses cursor-positioning escapes (e.g. `\x1b[>0q` query,
# `\x1b[2J` clear) on top of plain colour codes; if we don't catch
# them, parameter bytes like `>` survive into the buffer and a URL
# rendered inside a modal stays scrambled. Per ECMA-48:
#   - CSI: ESC `[`, params 0x30-0x3F (digits + : ; < = > ?),
#     intermediates 0x20-0x2F (space ! " # $ % & ' ( ) * + , - . /),
#     final byte 0x40-0x7E (@ A-Z [ \\ ] ^ _ ` a-z { | } ~).
#   - OSC: ESC `]`, body, BEL or ST (`\x1b\\`) terminator.
#   - DCS / SOS / PM / APC: ESC `[PX^_]`, body, ST terminator.
#   - 2-byte escapes: ESC followed by a single 0x40-0x5F byte
#     (cursor save/restore, NEL, IND, etc.).
# Plus carriage returns and the BEL char itself when it leaks through.
_ANSI_RE = re.compile(
    r"\x1b\[[\x30-\x3F]*[\x20-\x2F]*[\x40-\x7E]"          # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"                  # OSC
    r"|\x1b[PX^_][^\x1b]*\x1b\\"                           # DCS, SOS, PM, APC
    r"|\x1b[\x40-\x5F]"                                    # 2-byte escape
    r"|[\r\x07]"                                           # CR + BEL
)
# Greedy enough to swallow query strings; bounded by whitespace and
# common terminators that follow a URL in CLI output.
_URL_RE = re.compile(r"https?://[^\s\x1b\)\]<>]+")
# Trailing punctuation that should not be part of the URL even if the
# regex above grabbed it. Stripped post-match.
_URL_TRAILING_TRIM = ".,;:!?"
# Detects an interactive [y/n] prompt the CLI may show before printing
# the URL ("Open browser? [Y/n]" or similar). We answer 'n' so the URL
# is rendered to stdout instead of a failed xdg-open.
_PROMPT_RE = re.compile(r"\[[Yy]/[Nn]\]|\([Yy]/[Nn]\)")
# Phrases that indicate a successful login after the code is pasted.
# Case-insensitive substring match against the post-submit buffer.
_SUCCESS_PATTERNS = ("logged in", "successfully", "login successful")

# Override at test time to substitute a stand-in for `claude`.
_COMMAND: list[str] = ["claude"]


def _set_command_for_tests(cmd: list[str]) -> None:
    """Test seam — let unit tests swap `claude` for a Python one-liner."""
    global _COMMAND
    _COMMAND = list(cmd)


# ----------------------------------------------------------------- helpers

def strip_ansi(text: str) -> str:
    """Drop CSI/OSC escapes and carriage returns so URL/prompt regexes see clean text."""
    return _ANSI_RE.sub("", text)


def extract_url(text: str) -> Optional[str]:
    """Return the first https?:// URL in `text`, or None.

    Strips trailing punctuation that the regex may have swept up.
    """
    m = _URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(_URL_TRAILING_TRIM)
    return url or None


def looks_like_yn_prompt(text: str) -> bool:
    return bool(_PROMPT_RE.search(text))


def looks_like_success(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in _SUCCESS_PATTERNS)


# --------------------------------------------------------------- session

@dataclass
class LoginSession:
    sid: str
    proc: subprocess.Popen
    master_fd: int
    started_at: datetime
    url: Optional[str] = None
    buffer: str = ""
    closed: bool = False
    # Wall-clock when the .credentials.json file existed before we
    # started; used by the submit-tie-breaker to decide whether the
    # CLI just wrote a fresh file or we're seeing a stale one.
    pre_existing_creds_mtime: float = 0.0

    def read_available(self, max_bytes: int = READ_CHUNK) -> str:
        """Non-blocking drain of the master_fd. Returns the new chunk
        (ANSI-stripped) and appends it to `self.buffer`. Empty string
        if nothing was available."""
        try:
            data = os.read(self.master_fd, max_bytes)
        except (BlockingIOError, OSError):
            return ""
        if not data:
            return ""
        text = strip_ansi(data.decode("utf-8", errors="replace"))
        self.buffer += text
        return text

    def write(self, data: str) -> None:
        os.write(self.master_fd, data.encode("utf-8"))

    def close(self) -> None:
        """SIGTERM the subprocess, wait briefly, SIGKILL on hold-out, close fd."""
        if self.closed:
            return
        self.closed = True
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
            logger.exception("login session %s: error during process teardown", self.sid)
        try:
            os.close(self.master_fd)
        except OSError:
            pass


# --------------------------------------------------------------- module state

_sessions: dict[str, LoginSession] = {}
_lock = asyncio.Lock()
_reaper_task: Optional[asyncio.Task[None]] = None
_stopping = False


def is_reaper_running() -> bool:
    return _reaper_task is not None and not _reaper_task.done()


# --------------------------------------------------------------- spawn

def _credentials_path() -> Optional[Path]:
    """Resolve $CLAUDE_CONFIG_DIR/.credentials.json or None when env unset."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if not cfg:
        return None
    return Path(cfg) / ".credentials.json"


def _credentials_mtime() -> float:
    """Wall-clock mtime of the credentials file, or 0.0 if missing."""
    p = _credentials_path()
    if p is None:
        return 0.0
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _spawn_pty_subprocess() -> tuple[subprocess.Popen, int]:
    """Spawn the configured login command in a pty. POSIX-only."""
    if sys.platform == "win32":
        raise RuntimeError(
            "pty-driven login is POSIX-only; run on Linux or use the paste fallback."
        )
    import fcntl
    import pty

    master_fd, slave_fd = pty.openpty()
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    env = os.environ.copy()
    # Reduce ANSI noise where the CLI honours these.
    env["TERM"] = "dumb"
    env.setdefault("NO_COLOR", "1")
    # Defensive: CI envs may set CI=true and the CLI sometimes adapts;
    # we want it to print the URL to stdout regardless.
    env.pop("CI", None)

    try:
        proc = subprocess.Popen(
            _COMMAND,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
            env=env,
        )
    except FileNotFoundError as e:
        os.close(master_fd)
        os.close(slave_fd)
        raise RuntimeError(
            f"could not exec {_COMMAND[0]!r}: not found on PATH inside the container"
        ) from e
    except OSError as e:
        os.close(master_fd)
        os.close(slave_fd)
        raise RuntimeError(f"could not spawn {_COMMAND[0]!r}: {e}") from e

    # Parent doesn't need the slave end after the child inherits it.
    os.close(slave_fd)
    return proc, master_fd


# --------------------------------------------------------------- public api

async def start_login() -> dict:
    """Spawn the CLI, send /login, capture the OAuth URL, return it.

    Drops any prior in-flight session — only one login per harness at
    a time. Raises RuntimeError on spawn / timeout / early-exit; the
    HTTP layer translates these into 502s.
    """
    async with _lock:
        for sid in list(_sessions):
            try:
                _sessions.pop(sid).close()
            except Exception:
                logger.exception("could not close prior login session %s", sid)

        proc, master_fd = _spawn_pty_subprocess()
        sid = _secrets.token_urlsafe(16)
        sess = LoginSession(
            sid=sid,
            proc=proc,
            master_fd=master_fd,
            started_at=datetime.now(timezone.utc),
            pre_existing_creds_mtime=_credentials_mtime(),
        )
        _sessions[sid] = sess

    # Settle long enough for the TUI to fully draw + reach the input
    # prompt before we send /login. 0.5s was too short — the keystrokes
    # got dropped during init and the buffer kept showing the welcome
    # screen until timeout.
    await asyncio.sleep(STARTUP_SETTLE)
    sess.read_available()
    try:
        sess.write("/login\n")
    except OSError as e:
        sess.close()
        _sessions.pop(sid, None)
        raise RuntimeError(f"could not send /login to subprocess: {e}") from e

    loop = asyncio.get_running_loop()
    deadline = loop.time() + START_TIMEOUT
    resent_login = False
    while loop.time() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        chunk = sess.read_available()
        if chunk and looks_like_yn_prompt(chunk):
            # CLI asks something like "Refresh login? [Y/n]" or
            # "Open browser? [Y/n]" — answer 'y' so the URL is printed
            # (xdg-open fails harmlessly in headless containers and the
            # CLI falls back to displaying the URL).
            try:
                sess.write("y\n")
            except OSError:
                pass
        url = extract_url(sess.buffer)
        if url:
            sess.url = url
            return {"session_id": sid, "url": url}
        # Some CLI versions need /login resent if the first keystroke
        # raced startup. After 3s with no URL and an empty-looking
        # buffer, retry once.
        if (not resent_login
                and loop.time() - (deadline - START_TIMEOUT) > 3.0
                and "login" not in sess.buffer.lower()):
            try:
                sess.write("/login\n")
            except OSError:
                pass
            resent_login = True
        if sess.proc.poll() is not None:
            transcript = sess.buffer[-ERROR_TAIL_BYTES:]
            sess.close()
            _sessions.pop(sid, None)
            raise RuntimeError(
                f"claude CLI exited early (code {sess.proc.returncode}) "
                f"without printing an OAuth URL. Last output:\n{transcript}"
            )

    transcript = sess.buffer[-ERROR_TAIL_BYTES:]
    sess.close()
    _sessions.pop(sid, None)
    raise RuntimeError(
        f"timed out after {START_TIMEOUT:.0f}s waiting for the OAuth URL. "
        f"The CLI may be rendering the URL inside a TUI modal we can't "
        f"parse — use the paste-credentials fallback below instead. "
        f"Last output:\n{transcript}"
    )


async def submit_code(sid: str, code: str) -> dict:
    """Feed the OAuth code to the running CLI, wait for it to finish."""
    sess = _sessions.get(sid)
    if sess is None:
        raise RuntimeError("login session not found or already expired — start over")
    if sess.closed:
        _sessions.pop(sid, None)
        raise RuntimeError("login session is no longer active — start over")
    code = code.strip()
    if not code:
        raise RuntimeError("empty code")

    # Reset the buffer so we only inspect post-submit output.
    sess.buffer = ""
    try:
        sess.write(code + "\n")
    except OSError as e:
        sess.close()
        _sessions.pop(sid, None)
        raise RuntimeError(f"could not send code to subprocess: {e}") from e

    pre_mtime = sess.pre_existing_creds_mtime
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SUBMIT_TIMEOUT
    while loop.time() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        sess.read_available()
        if looks_like_success(sess.buffer):
            sess.close()
            _sessions.pop(sid, None)
            return {"ok": True}
        # Tie-breaker: if the credentials file was just (re)written,
        # treat that as success even without an explicit success line.
        cur_mtime = _credentials_mtime()
        if cur_mtime and cur_mtime > pre_mtime:
            sess.close()
            _sessions.pop(sid, None)
            return {"ok": True}
        if sess.proc.poll() is not None:
            transcript = sess.buffer[-ERROR_TAIL_BYTES:]
            cur_mtime = _credentials_mtime()
            sess.close()
            _sessions.pop(sid, None)
            if cur_mtime and cur_mtime > pre_mtime:
                return {"ok": True}
            raise RuntimeError(
                f"claude CLI exited (code {sess.proc.returncode}) without "
                f"writing credentials. Last output:\n{transcript}"
            )

    transcript = sess.buffer[-ERROR_TAIL_BYTES:]
    cur_mtime = _credentials_mtime()
    sess.close()
    _sessions.pop(sid, None)
    if cur_mtime and cur_mtime > pre_mtime:
        return {"ok": True}
    raise RuntimeError(
        f"timed out after {SUBMIT_TIMEOUT:.0f}s waiting for confirmation. "
        f"Last output:\n{transcript}"
    )


async def cancel_login(sid: str) -> dict:
    """Tear down a login session. No-op when the id is unknown."""
    sess = _sessions.pop(sid, None)
    if sess is not None:
        try:
            sess.close()
        except Exception:
            logger.exception("error closing cancelled login session %s", sid)
    return {"ok": True}


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
                        "claude_login: reaped session %s (age=%.0fs, proc_dead=%s)",
                        sid, age, proc_dead,
                    )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("claude_login reaper iteration failed")


async def start_login_reaper() -> None:
    """Idempotent — only spawns if not already running."""
    global _reaper_task, _stopping
    if is_reaper_running():
        return
    _stopping = False
    loop = asyncio.get_running_loop()
    _reaper_task = loop.create_task(_reaper_loop(), name="harness.claude_login.reaper")
    logger.info("claude_login: reaper started (ttl=%.0fs, interval=%.0fs)",
                SESSION_TTL, REAPER_INTERVAL)


async def stop_login_reaper(timeout: float = 2.0) -> None:
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
            logger.exception("error closing login session %s on shutdown", sid)

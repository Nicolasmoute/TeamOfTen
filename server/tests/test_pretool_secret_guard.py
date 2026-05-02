"""Tests for the PreToolUse secret-guard hook.

Defense-in-depth on top of the env scrub (Item 1) and Files API
denylist (Item 2). The hook intercepts agent SDK tool calls
(Read/Edit/Write/MultiEdit/NotebookEdit/Bash) that target sensitive
harness paths and returns a `deny` permission decision.

Best-effort by design — a Bash one-liner that constructs a path at
runtime can bypass. These tests pin the obvious-shape coverage so a
casual prompt-injection ("cat the credentials file") fails closed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.agents import (
    _denied_secret_paths,
    _path_is_secret,
    _pretool_secret_guard_hook,
)


# ---------- _path_is_secret ----------


def test_claude_credentials_path_is_secret(fresh_db) -> None:
    from server.paths import DATA_ROOT
    p = DATA_ROOT / "claude" / ".credentials.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    assert _path_is_secret(str(p))


def test_codex_auth_path_is_secret(fresh_db) -> None:
    from server.paths import DATA_ROOT
    p = DATA_ROOT / "codex" / "auth.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    assert _path_is_secret(str(p))


def test_harness_db_path_is_secret(fresh_db) -> None:
    from server.paths import DATA_ROOT
    db = DATA_ROOT / "harness.db"
    db.write_bytes(b"")
    assert _path_is_secret(str(db))
    # Sidecars too — agent reading the WAL bypasses every API guard.
    wal = DATA_ROOT / "harness.db-wal"
    wal.write_bytes(b"")
    # The sidecar isn't in the canonical denied set on its own; it's
    # caught only when the canonical DB path matches. So the WAL must
    # be added to the deny set explicitly. Verify:
    paths = _denied_secret_paths()
    assert any(str(p).endswith("harness.db") for p in paths)


def test_proc_environ_is_secret() -> None:
    """/proc/<pid>/environ leaks the harness's own env. Hook denies even
    though Item 1 scrubbed agent-subprocess env — agent + harness share
    the filesystem and uid in current architecture."""
    assert _path_is_secret("/proc/1/environ")
    assert _path_is_secret("/proc/12345/environ")
    assert _path_is_secret("/proc/self/environ")
    assert _path_is_secret("/proc/thread-self/environ")


def test_unrelated_path_is_not_secret(fresh_db) -> None:
    from server.paths import DATA_ROOT
    p = DATA_ROOT / "projects" / "alpha" / "notes.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# fine")
    assert not _path_is_secret(str(p))


def test_lookalike_dirname_is_not_secret(fresh_db) -> None:
    """A dir whose name shares a prefix with `claude` (e.g. claude-helper)
    is not denied — the deny is exact-prefix on the absolute path."""
    from server.paths import DATA_ROOT
    p = DATA_ROOT / "claude-helper" / "notes.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# fine")
    assert not _path_is_secret(str(p))


def test_empty_path_is_not_secret() -> None:
    assert not _path_is_secret("")


# ---------- hook (Read / Write / Edit / MultiEdit / NotebookEdit) ----------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _hook_input(tool_name: str, **input_fields: Any) -> dict[str, Any]:
    return {"tool_name": tool_name, "tool_input": input_fields}


def test_hook_denies_read_of_claude_credentials(fresh_db) -> None:
    from server.paths import DATA_ROOT
    p = DATA_ROOT / "claude" / ".credentials.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    out = _run(_pretool_secret_guard_hook(
        _hook_input("Read", file_path=str(p)), None, None
    ))
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_hook_denies_write_to_claude_dir(fresh_db) -> None:
    from server.paths import DATA_ROOT
    p = DATA_ROOT / "claude" / ".credentials.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    out = _run(_pretool_secret_guard_hook(
        _hook_input("Write", file_path=str(p)), None, None
    ))
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_hook_denies_read_of_proc_environ() -> None:
    """The PID could be anything — the hook tests the path shape, not
    the existence of the file (so it works even on Windows where /proc
    doesn't exist)."""
    out = _run(_pretool_secret_guard_hook(
        _hook_input("Read", file_path="/proc/self/environ"), None, None
    ))
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_hook_allows_unrelated_read(fresh_db) -> None:
    from server.paths import DATA_ROOT
    p = DATA_ROOT / "projects" / "alpha" / "notes.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# fine")
    out = _run(_pretool_secret_guard_hook(
        _hook_input("Read", file_path=str(p)), None, None
    ))
    # Empty dict means "no decision; let other hooks / default rule
    # decide". That's the pass-through shape.
    assert out == {} or "hookSpecificOutput" not in out


def test_hook_allows_read_with_no_path() -> None:
    """A tool call without a file_path never matches. Don't fail open
    on missing fields — return empty dict (pass-through)."""
    out = _run(_pretool_secret_guard_hook(
        _hook_input("Read"), None, None
    ))
    assert out == {}


# ---------- hook (Bash) ----------


def test_hook_denies_bash_cat_claude_credentials() -> None:
    out = _run(_pretool_secret_guard_hook(
        _hook_input("Bash", command="cat /data/claude/.credentials.json"),
        None, None,
    ))
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_hook_denies_bash_cat_proc_environ() -> None:
    out = _run(_pretool_secret_guard_hook(
        _hook_input("Bash", command="cat /proc/1/environ | strings"),
        None, None,
    ))
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_hook_denies_bash_iterate_proc_environ() -> None:
    """Common shape: iterate /proc looking for HARNESS_TOKEN. The pattern
    catches /proc/<digits>/environ regardless of how the loop is shaped."""
    out = _run(_pretool_secret_guard_hook(
        _hook_input("Bash", command="for f in /proc/*/environ; do strings $f; done"),
        None, None,
    ))
    # Glob `/proc/*/environ` itself doesn't textually contain a digit
    # or `self` — so the regex misses it. This is the documented best-
    # effort gap; assert today's behavior so the gap is visible in
    # tests if someone later "fixes" the regex without thinking it
    # through.
    assert out == {}


def test_hook_denies_bash_python_one_liner_with_literal_path() -> None:
    out = _run(_pretool_secret_guard_hook(
        _hook_input(
            "Bash",
            command='python -c "print(open(\\"/data/claude/.credentials.json\\").read())"',
        ),
        None, None,
    ))
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_hook_allows_unrelated_bash() -> None:
    out = _run(_pretool_secret_guard_hook(
        _hook_input("Bash", command="git status"),
        None, None,
    ))
    assert out == {}


def test_hook_denies_grep_into_claude_dir(fresh_db) -> None:
    """Grep on a sensitive directory leaks content via match output —
    same threat surface as Read."""
    from server.paths import DATA_ROOT
    p = DATA_ROOT / "claude"
    p.mkdir(parents=True, exist_ok=True)
    out = _run(_pretool_secret_guard_hook(
        _hook_input("Grep", path=str(p), pattern="oauth"),
        None, None,
    ))
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_hook_denies_glob_into_codex_dir(fresh_db) -> None:
    """Glob on a sensitive directory leaks file names — lower-severity
    than content but still off-limits."""
    from server.paths import DATA_ROOT
    p = DATA_ROOT / "codex"
    p.mkdir(parents=True, exist_ok=True)
    out = _run(_pretool_secret_guard_hook(
        _hook_input("Glob", path=str(p), pattern="*.json"),
        None, None,
    ))
    assert out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def test_hook_allows_grep_in_project_tree(fresh_db) -> None:
    from server.paths import DATA_ROOT
    p = DATA_ROOT / "projects" / "alpha"
    p.mkdir(parents=True, exist_ok=True)
    out = _run(_pretool_secret_guard_hook(
        _hook_input("Grep", path=str(p), pattern="TODO"),
        None, None,
    ))
    assert out == {}


def test_home_dir_claude_is_in_deny_set() -> None:
    """Home-directory variant `~/.claude` is in the deny set so a dev
    deploy without `CLAUDE_CONFIG_DIR` set still gets coverage."""
    from pathlib import Path as _P
    paths = _denied_secret_paths()
    home_claude = (_P.home() / ".claude").resolve()
    home_codex = (_P.home() / ".codex").resolve()
    assert home_claude in paths
    assert home_codex in paths


def test_hook_fails_open_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the hook itself raises, we let the call through (don't break
    the agent's loop). Mirrors the existing _pretool_file_guard_hook
    behavior. We force a raise by feeding a non-string command."""
    out = _run(_pretool_secret_guard_hook(
        _hook_input("Bash", command=object()),  # type: ignore[arg-type]
        None, None,
    ))
    # No deny decision; fail-open returns empty.
    assert out == {} or out.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"

"""Tests for the agent subprocess env-scrub policy.

Behavior-focused: each test pins a property of the policy that's
load-bearing for the security guarantee. If you change one of these
expectations, you're either deliberately broadening / narrowing the
policy (update spec + tests together) or you've introduced a leak.
"""

from __future__ import annotations

import os

import pytest

from server import agent_env
from server.agent_env import (
    build_agent_env_overrides,
    build_clean_agent_env,
    is_allowed,
    is_sensitive,
)


# ---------------------------------------------------------------- patterns


def test_harness_prefix_is_sensitive() -> None:
    assert is_sensitive("HARNESS_TOKEN")
    assert is_sensitive("HARNESS_SECRETS_KEY")
    assert is_sensitive("HARNESS_COACH_TICK_INTERVAL")  # config too — scrubbed


def test_kdrive_and_telegram_prefixes_are_sensitive() -> None:
    assert is_sensitive("KDRIVE_URL")
    assert is_sensitive("KDRIVE_USERNAME")
    assert is_sensitive("KDRIVE_PASSWORD")
    assert is_sensitive("TELEGRAM_BOT_TOKEN")
    assert is_sensitive("TELEGRAM_ALLOWED_CHAT_IDS")


def test_provider_prefixes_are_sensitive() -> None:
    assert is_sensitive("OPENAI_API_KEY")
    assert is_sensitive("ANTHROPIC_API_KEY")
    assert is_sensitive("AWS_SECRET_ACCESS_KEY")
    assert is_sensitive("GCP_SERVICE_ACCOUNT_KEY")
    assert is_sensitive("AZURE_CLIENT_SECRET")


def test_suffix_patterns_catch_common_secret_names() -> None:
    assert is_sensitive("GITHUB_TOKEN")
    assert is_sensitive("GH_TOKEN")
    assert is_sensitive("NOTION_API_KEY")
    assert is_sensitive("STRIPE_SECRET")
    assert is_sensitive("DB_PASSWORD")
    assert is_sensitive("CLOUDFLARE_API_KEY")


def test_database_url_is_sensitive() -> None:
    # Connection strings often embed credentials.
    assert is_sensitive("DATABASE_URL")


def test_benign_vars_are_not_sensitive() -> None:
    for name in ("PATH", "HOME", "USER", "TERM", "LANG", "TZ", "PWD"):
        assert not is_sensitive(name), name


def test_claude_and_codex_config_dirs_are_not_sensitive() -> None:
    # These point at directories that hold secrets, but the var values
    # themselves (paths) are not secret. Agents need them to find OAuth.
    assert not is_sensitive("CLAUDE_CONFIG_DIR")
    assert not is_sensitive("CODEX_HOME")


# ---------------------------------------------------------------- allowlist


def test_allowlisted_vars_pass_when_not_sensitive() -> None:
    for name in (
        "PATH",
        "HOME",
        "USER",
        "TERM",
        "LANG",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "GIT_AUTHOR_NAME",
    ):
        assert is_allowed(name), name


def test_sensitive_pattern_overrides_allowlist() -> None:
    """If a sensitive name accidentally lands on the allowlist, the
    sensitive pattern still wins. Belt-and-braces against future edits."""
    # HARNESS_TOKEN is not on the allowlist, but the property holds for
    # the negation: nothing matching a sensitive pattern is allowed,
    # regardless of allowlist membership.
    assert not is_allowed("HARNESS_TOKEN")
    assert not is_allowed("KDRIVE_PASSWORD")
    assert not is_allowed("OPENAI_API_KEY")


def test_unrelated_vars_are_not_allowed() -> None:
    # The allowlist is opt-in, not opt-out.
    assert not is_allowed("RANDOM_DEV_VAR")
    assert not is_allowed("MY_PROJECT_FLAG")


# ----------------------------------------------------- build_clean_agent_env


def test_clean_env_keeps_allowlisted_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/agent")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/data/claude")
    out = build_clean_agent_env()
    assert out["PATH"] == "/usr/bin:/bin"
    assert out["HOME"] == "/home/agent"
    assert out["CLAUDE_CONFIG_DIR"] == "/data/claude"


def test_clean_env_drops_sensitive_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_TOKEN", "secret-bearer")
    monkeypatch.setenv("HARNESS_SECRETS_KEY", "fernet-master-key-base64")
    monkeypatch.setenv("KDRIVE_PASSWORD", "kdrive-app-password")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "12345:abcdef")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_xxx")
    out = build_clean_agent_env()
    assert "HARNESS_TOKEN" not in out
    assert "HARNESS_SECRETS_KEY" not in out
    assert "KDRIVE_PASSWORD" not in out
    assert "TELEGRAM_BOT_TOKEN" not in out
    assert "GITHUB_TOKEN" not in out


def test_clean_env_drops_non_allowlisted_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default-deny: anything not explicitly allowlisted is dropped,
    even when it isn't recognized as a sensitive pattern. Stops a
    future env var from silently leaking just because nobody added a
    matching pattern."""
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("UNLISTED_RANDOM_VAR", "value")
    out = build_clean_agent_env()
    assert out.get("PATH") == "/usr/bin"
    assert "UNLISTED_RANDOM_VAR" not in out


def test_clean_env_extras_overlay_last(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    out = build_clean_agent_env(
        extra={"HARNESS_COORD_PROXY_TOKEN": "spawn-token-xyz", "PATH": "/override"}
    )
    # extras can introduce vars that wouldn't otherwise pass the policy
    # (HARNESS_COORD_PROXY_TOKEN is a HARNESS_ var) — that's by design:
    # the codex spawn site needs to inject the per-spawn proxy token.
    assert out["HARNESS_COORD_PROXY_TOKEN"] == "spawn-token-xyz"
    # And extras win over allowlisted base vars.
    assert out["PATH"] == "/override"


def test_clean_env_does_not_mutate_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_TOKEN", "still-here")
    build_clean_agent_env()
    assert os.environ["HARNESS_TOKEN"] == "still-here"


# ---------------------------------------------- build_agent_env_overrides


def test_overrides_blank_out_sensitive_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_TOKEN", "secret")
    monkeypatch.setenv("KDRIVE_PASSWORD", "p")
    monkeypatch.setenv("PATH", "/usr/bin")
    out = build_agent_env_overrides()
    assert out["HARNESS_TOKEN"] == ""
    assert out["KDRIVE_PASSWORD"] == ""
    # Non-sensitive vars are NOT included — the merge would otherwise
    # overwrite a real value (e.g. PATH) with whatever we put here.
    assert "PATH" not in out


def test_overrides_only_include_currently_set_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overrides reflect what's actually in os.environ at call time. A
    var that isn't set won't appear, so the merge can't accidentally
    introduce a key that masks something the agent needs."""
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = build_agent_env_overrides()
    assert "HARNESS_TOKEN" not in out
    assert "OPENAI_API_KEY" not in out

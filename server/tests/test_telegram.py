"""Tests for the Telegram bridge.

Covers the pure helpers (`_parse_chat_ids`, `_split_chunks`,
`is_valid_token`), the team_config disabled-flag round-trip, and
`_resolve_config` precedence (disabled flag > secrets > env > unset).

Tests don't spin up the bridge (no real network); they exercise the
config + filter logic that decides whether the bridge runs and what
it relays.
"""
from __future__ import annotations

import pytest

from server.db import init_db


def test_is_valid_token() -> None:
    from server.telegram import is_valid_token

    assert is_valid_token("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    assert is_valid_token("1:" + "x" * 30)
    assert is_valid_token("  123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ-_-_-_  ")  # trimmed

    assert not is_valid_token("")
    assert not is_valid_token("notatoken")
    assert not is_valid_token("12345:short")
    assert not is_valid_token("abc:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    assert not is_valid_token("12345 ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")  # space, not colon
    assert not is_valid_token("12345:ABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$%^&*()")  # bad chars


def test_parse_chat_ids() -> None:
    from server.telegram import _parse_chat_ids

    assert _parse_chat_ids("") == set()
    assert _parse_chat_ids("12345") == {12345}
    assert _parse_chat_ids("12345,67890") == {12345, 67890}
    assert _parse_chat_ids("  12345 , 67890  ") == {12345, 67890}
    assert _parse_chat_ids("12345,abc,67890") == {12345, 67890}
    assert _parse_chat_ids("abc,xyz") == set()
    # Telegram uses negative IDs for groups
    assert _parse_chat_ids("-1001234567890") == {-1001234567890}


def test_split_chunks_pure() -> None:
    from server.telegram import _split_chunks

    assert _split_chunks("") == []
    assert _split_chunks("hi") == ["hi"]
    assert _split_chunks("a" * 100, 4000) == ["a" * 100]

    # Splits on paragraph boundary when available
    para = "a" * 3000
    text = para + "\n\n" + para + "\n\n" + para
    chunks = _split_chunks(text, 4000)
    assert len(chunks) >= 2
    assert all(len(c) <= 4000 for c in chunks)

    # Hard cut when no boundaries
    hard = "x" * 10000
    chunks = _split_chunks(hard, 4000)
    assert all(len(c) <= 4000 for c in chunks)
    assert "".join(chunks) == hard


async def test_disabled_flag_round_trip(fresh_db: str) -> None:
    await init_db()
    from server.telegram import _read_disabled_flag, _set_disabled_flag

    assert await _read_disabled_flag() is False

    await _set_disabled_flag(True)
    assert await _read_disabled_flag() is True

    await _set_disabled_flag(False)
    assert await _read_disabled_flag() is False

    # Idempotent: setting twice is a no-op the second time
    await _set_disabled_flag(True)
    await _set_disabled_flag(True)
    assert await _read_disabled_flag() is True


def _enable_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fresh Fernet key in the env + reset the cached
    instance so each test starts from a clean state."""
    from cryptography.fernet import Fernet

    import server.secrets as secrets_mod

    monkeypatch.setenv("HARNESS_SECRETS_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(secrets_mod, "_fernet", None)
    monkeypatch.setattr(
        secrets_mod, "_key_status",
        {"ok": False, "reason": "uninitialized"},
    )


async def test_resolve_config_unset(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No token anywhere → bridge is disabled (returns None)."""
    await init_db()
    _enable_secrets(monkeypatch)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)

    from server.telegram import _resolve_config

    assert await _resolve_config() is None


async def test_resolve_config_token_without_whitelist(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token set but no chat IDs → refuse to start (security guard)."""
    await init_db()
    _enable_secrets(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:" + "x" * 30)
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)

    from server.telegram import _resolve_config

    assert await _resolve_config() is None


async def test_resolve_config_env_fallback(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token + IDs in env (no DB secrets) → bridge starts on env values."""
    await init_db()
    _enable_secrets(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:" + "x" * 30)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111,222")

    from server.telegram import _resolve_config

    cfg = await _resolve_config()
    assert cfg is not None
    token, allowed = cfg
    assert token == "1:" + "x" * 30
    assert allowed == {111, 222}


async def test_resolve_config_db_wins_over_env(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB secrets shadow env. UI-saved values are authoritative."""
    await init_db()
    _enable_secrets(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:" + "x" * 30)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111")

    from server import secrets as secrets_store
    from server.telegram import (
        SECRET_TOKEN_NAME,
        SECRET_CHAT_IDS_NAME,
        _resolve_config,
    )

    db_token = "9999:" + "y" * 30
    await secrets_store.set_secret(SECRET_TOKEN_NAME, db_token)
    await secrets_store.set_secret(SECRET_CHAT_IDS_NAME, "999")

    cfg = await _resolve_config()
    assert cfg is not None
    token, allowed = cfg
    assert token == db_token
    assert allowed == {999}


async def test_resolve_config_disabled_flag_overrides_env(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled flag wins over both DB and env — Clear must really disable."""
    await init_db()
    _enable_secrets(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:" + "x" * 30)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "111")

    from server.telegram import _resolve_config, _set_disabled_flag

    # With env set but no flag → bridge would start
    cfg = await _resolve_config()
    assert cfg is not None

    # Flip the flag → disabled regardless of env
    await _set_disabled_flag(True)
    assert await _resolve_config() is None

    # Clearing the flag → re-enabled
    await _set_disabled_flag(False)
    assert await _resolve_config() is not None


async def test_resolve_config_disabled_flag_overrides_db(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same precedence check but with values stored in the secrets DB."""
    await init_db()
    _enable_secrets(monkeypatch)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHAT_IDS", raising=False)

    from server import secrets as secrets_store
    from server.telegram import (
        SECRET_TOKEN_NAME,
        SECRET_CHAT_IDS_NAME,
        _resolve_config,
        _set_disabled_flag,
    )

    await secrets_store.set_secret(SECRET_TOKEN_NAME, "1:" + "x" * 30)
    await secrets_store.set_secret(SECRET_CHAT_IDS_NAME, "999")

    assert await _resolve_config() is not None
    await _set_disabled_flag(True)
    assert await _resolve_config() is None

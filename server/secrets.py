"""Encrypted secrets store.

Values are Fernet-encrypted (AES-128-CBC + HMAC-SHA256) with the master
key supplied via HARNESS_SECRETS_KEY. The key never touches the DB, so
a stolen snapshot on its own is useless. Lose the key → lose the
secrets; that's the intended failure mode.

Callers interact via async helpers that hit the shared `secrets` table.
Plaintext values exit this module only through get_secret() — every
other public shape (list_secrets, delete_secret, status) returns
metadata or booleans.

HARNESS_SECRETS_KEY format: a 32-byte urlsafe-base64 string, exactly
what `Fernet.generate_key()` produces. Generate one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiosqlite

from server.db import DB_PATH

logger = logging.getLogger("harness.secrets")


# Lazy cached Fernet instance. None when the key is missing/invalid.
_fernet: Any = None
_key_status: dict[str, Any] = {"ok": False, "reason": "uninitialized"}


def _load_fernet() -> Any | None:
    """Build a Fernet from HARNESS_SECRETS_KEY; cache the result.
    Returns None when the key is missing or malformed — callers should
    treat this as 'secrets disabled' and log appropriately."""
    global _fernet, _key_status
    if _fernet is not None:
        return _fernet
    raw = os.environ.get("HARNESS_SECRETS_KEY", "").strip()
    if not raw:
        _key_status = {
            "ok": False,
            "reason": "HARNESS_SECRETS_KEY env var not set",
        }
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        _key_status = {
            "ok": False,
            "reason": "cryptography package not installed",
        }
        logger.exception("secrets: cryptography import failed")
        return None
    try:
        _fernet = Fernet(raw.encode("ascii"))
        _key_status = {"ok": True, "reason": None}
    except Exception as e:
        _key_status = {
            "ok": False,
            "reason": f"HARNESS_SECRETS_KEY invalid: {type(e).__name__}",
        }
        logger.exception("secrets: Fernet init failed")
        return None
    return _fernet


def status() -> dict[str, Any]:
    """Return a dict with {ok: bool, reason: str|None} describing the
    master-key readiness. Used by /api/health."""
    _load_fernet()
    return dict(_key_status)


async def get_secret(name: str) -> str | None:
    """Decrypt and return the secret value, or None when the name
    doesn't exist OR decryption fails (bad key / tampered blob).
    Never raises — callers fall back to env or empty string."""
    f = _load_fernet()
    if f is None:
        return None
    try:
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            cur = await db.execute(
                "SELECT ciphertext FROM secrets WHERE name = ?", (name,)
            )
            row = await cur.fetchone()
    except Exception:
        logger.exception("secrets: DB read failed for %s", name)
        return None
    if not row:
        return None
    try:
        plaintext = f.decrypt(bytes(row[0]))
    except Exception:
        logger.exception(
            "secrets: decrypt failed for %s — wrong key or corrupted row",
            name,
        )
        return None
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("secrets: non-utf8 plaintext for %s, returning empty", name)
        return None


async def set_secret(name: str, value: str) -> bool:
    """Upsert an encrypted value. Returns True on success, False when
    the master key isn't available (caller should surface the error to
    the UI — silent failure would let the user 'save' into the void)."""
    f = _load_fernet()
    if f is None:
        return False
    try:
        ciphertext = f.encrypt(value.encode("utf-8"))
    except Exception:
        logger.exception("secrets: encrypt failed for %s", name)
        return False
    try:
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            await db.execute(
                "INSERT INTO secrets (name, ciphertext) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "  ciphertext = excluded.ciphertext, "
                "  updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (name, ciphertext),
            )
            await db.commit()
    except Exception:
        logger.exception("secrets: upsert failed for %s", name)
        return False
    return True


async def delete_secret(name: str) -> bool:
    """Remove by name. Returns True if a row was deleted, False if it
    didn't exist or the write failed."""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            cur = await db.execute(
                "DELETE FROM secrets WHERE name = ?", (name,)
            )
            await db.commit()
            return cur.rowcount > 0
    except Exception:
        logger.exception("secrets: delete failed for %s", name)
        return False


async def list_secrets() -> list[dict[str, Any]]:
    """Metadata-only listing (no plaintext): [{name, created_at,
    updated_at}, ...] sorted by name."""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            cur = await db.execute(
                "SELECT name, created_at, updated_at FROM secrets "
                "ORDER BY name"
            )
            rows = await cur.fetchall()
    except Exception:
        logger.exception("secrets: list failed")
        return []
    return [
        {"name": r[0], "created_at": r[1], "updated_at": r[2]}
        for r in rows
    ]


# Sync-wrapper cache for hot-path interpolation: _interpolate in
# mcp_config runs synchronously + repeatedly per turn. The DB read is
# fast but we still don't want to fire off a thread per placeholder.
# Strategy: populate a name→plaintext dict on first call, invalidate
# when any /api/secrets mutation happens (via bump_cache_version).
_cache: dict[str, str] = {}
_cache_version = 0
_loaded_version = -1


def bump_cache_version() -> None:
    """Invalidate the sync interpolation cache. Called by the secrets
    API endpoints after a successful mutation so the next MCP reload
    picks up the new value without a process restart."""
    global _cache_version
    _cache_version += 1


def _refresh_cache_sync() -> None:
    """Repopulate the sync cache by decrypting every row. Uses plain
    sqlite3 (stdlib, sync) since lookup_sync() is called from inside
    re.sub callbacks in _interpolate — we can't await there. The
    stdlib driver opens its own connection; no lock contention with
    the async path because SQLite serializes at the file level."""
    global _cache, _loaded_version
    f = _load_fernet()
    if f is None:
        _cache = {}
        _loaded_version = _cache_version
        return
    import sqlite3
    try:
        with sqlite3.connect(DB_PATH, timeout=5.0) as db:
            rows = db.execute("SELECT name, ciphertext FROM secrets").fetchall()
    except Exception:
        logger.exception("secrets: cache refresh DB read failed")
        return
    fresh: dict[str, str] = {}
    for name, ct in rows:
        try:
            fresh[name] = f.decrypt(bytes(ct)).decode("utf-8")
        except Exception:
            logger.exception("secrets: cache decrypt failed for %s", name)
    _cache = fresh
    _loaded_version = _cache_version


def lookup_sync(name: str) -> str | None:
    """Sync lookup used by the _interpolate hot path. Lazily refreshes
    the cache when the version has bumped — safe to call from inside
    re.sub callbacks. Returns None when the name isn't stored."""
    if _loaded_version != _cache_version:
        _refresh_cache_sync()
    return _cache.get(name)

"""HMAC-signed URL minting + verification for `/api/attachments`.

Closes the audit's "bearer token in URL" leak path for image loads:
the UI used to render `<img src="/api/attachments/<id>?token=<HARNESS_TOKEN>">`
which puts the long-lived bearer token into screenshotable URLs and
proxy logs. The replacement flow:

  1. POST /api/attachments returns `signed_url` alongside the existing
     fields; the UI uses that URL directly for the freshly-uploaded
     image.
  2. Auth'd GET /api/attachments/{filename}/signed-url re-mints a
     fresh signed URL when the UI re-renders a historical pane.
  3. Unauth'd GET /api/attachments/{filename}/signed validates `exp`
     + HMAC `sig` and serves the bytes. No bearer token in the URL.

Signing key (`attachment_signing_key`) lives in `team_config`,
generated once at boot if absent. Persistent across restarts so URLs
the UI cached briefly still resolve after a redeploy.

Security properties:
  - 32-byte urlsafe key → 256 bits of entropy, comfortably above any
    practical brute-force threshold.
  - HMAC-SHA256 over `<filename>|<exp>` — exp prevents replay past
    its window; filename pins the URL to a specific attachment so a
    leaked sig can't be reused for a different file.
  - 5-minute default TTL. Long enough for a human to actually load
    the image; short enough that a leaked URL ages out before it's
    interesting. Adjust via `mint_signed_url(ttl_seconds=...)`.
  - Constant-time `hmac.compare_digest` for the verify step.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
import time
import urllib.parse
from typing import Final

logger = logging.getLogger("harness.attachments_signing")

_KEY_CONFIG_NAME: Final[str] = "attachment_signing_key"

# Default lifetime of a signed URL — the audit recommended ~5 minutes.
DEFAULT_TTL_SECONDS: Final[int] = 5 * 60

# In-process cache so we don't hit SQLite on every render. Invalidated
# only by process restart; the persisted key is immutable per deploy.
_KEY_CACHE: dict[str, str] = {}


def _generate_key() -> str:
    """Return a fresh 32-byte urlsafe key (~43 chars).

    `secrets.token_urlsafe(32)` is the standard Python idiom for
    cryptographic random strings, backed by `os.urandom` on every
    supported platform.
    """
    return secrets.token_urlsafe(32)


def _read_key_sync(db_path: str) -> str | None:
    """Synchronous DB read so the verify path (which serves bytes
    inside a request handler) doesn't need the asyncio overhead.
    Returns None if the row is missing."""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            cur = conn.execute(
                "SELECT value FROM team_config WHERE key = ?",
                (_KEY_CONFIG_NAME,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
    except Exception:
        logger.exception("attachments_signing: read failed")
        return None
    if not row or not row[0]:
        return None
    return str(row[0])


def _write_key_sync(db_path: str, key: str) -> None:
    """Persist a freshly-generated key. Uses INSERT OR IGNORE so a
    concurrent process that beat us to it isn't stomped — both
    instances would otherwise generate different keys and one would
    silently invalidate the other's signed URLs."""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO team_config (key, value) "
                "VALUES (?, ?)",
                (_KEY_CONFIG_NAME, key),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("attachments_signing: write failed")


def _signing_key() -> str:
    """Resolve the persisted key, generating + persisting one on first
    call after a fresh DB. Cached in-process for the rest of the
    deploy's lifetime."""
    cached = _KEY_CACHE.get("k")
    if cached:
        return cached
    # Lazy import to avoid the module-load circular: server.main imports
    # this module at startup, but server.db imports a number of other
    # modules transitively.
    from server.db import DB_PATH

    existing = _read_key_sync(DB_PATH)
    if existing:
        _KEY_CACHE["k"] = existing
        return existing
    fresh = _generate_key()
    _write_key_sync(DB_PATH, fresh)
    # Re-read in case a concurrent boot beat us to the INSERT — that
    # one wins, ours is discarded by INSERT OR IGNORE.
    final = _read_key_sync(DB_PATH) or fresh
    _KEY_CACHE["k"] = final
    return final


def _compute_sig(filename: str, exp: int, key: str) -> str:
    """HMAC-SHA256 over `<filename>|<exp>` keyed by the deploy's signing
    key. Hex-encoded so it's URL-safe without further escaping. The `|`
    delimiter is safe — filenames are `<uuid>.<ext>` and the API
    rejects `/` and `..` upstream."""
    msg = f"{filename}|{exp}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def mint_signed_url(
    filename: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    base_path: str = "/api/attachments",
) -> str:
    """Return a relative URL of the form
    `<base_path>/<filename>/signed?exp=<unix-seconds>&sig=<hex>`.

    `filename` is the on-disk basename (e.g. `abc123.png`); the API
    endpoint resolves it inside the active project's attachments dir.
    """
    exp = int(time.time()) + max(60, ttl_seconds)
    key = _signing_key()
    sig = _compute_sig(filename, exp, key)
    qs = urllib.parse.urlencode({"exp": exp, "sig": sig})
    return f"{base_path}/{filename}/signed?{qs}"


def verify_signed(filename: str, exp: int | str, sig: str) -> bool:
    """Constant-time verification. False on:
      - missing or non-numeric `exp`,
      - exp in the past (URL aged out),
      - sig that doesn't match the recomputed HMAC for this
        `(filename, exp, key)` triple.
    """
    if not filename or not sig:
        return False
    try:
        exp_int = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_int < int(time.time()):
        return False
    key = _signing_key()
    expected = _compute_sig(filename, exp_int, key)
    # Compare on bytes — hmac.compare_digest expects matching types.
    return hmac.compare_digest(expected.encode("ascii"), sig.encode("ascii"))


def reset_cache_for_tests() -> None:
    """Test hook — clear the in-process cache so a fresh DB fixture
    forces a key re-read."""
    _KEY_CACHE.clear()

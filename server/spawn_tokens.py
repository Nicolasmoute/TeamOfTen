"""Per-spawn token registry for the coord-MCP proxy.

When the dispatcher spawns a Codex turn (PR 5+), it mints a short-
lived token bound to `(caller_id, expires_at)` and passes it to the
coord_mcp subprocess via env (`HARNESS_COORD_PROXY_TOKEN`). The
subprocess includes the token as Bearer auth on every
`POST /api/_coord/{tool}` call. The endpoint resolves token →
caller_id server-side; the request body's caller_id is a sanity
check only.

This closes the impersonation hole: a compromised proxy or any
process that learns a token CAN'T forge a request as a different
slot — the binding is server-side. See `Docs/CODEX_RUNTIME_SPEC.md`
§C.4.

Pure in-memory; tokens evaporate on restart, which is fine — turns
in flight at restart are crashed by `crash_recover()` anyway, and a
new spawn mints a new token.
"""

from __future__ import annotations

import secrets
import time
from threading import Lock

# token → {caller_id, expires_at_monotonic}
_tokens: dict[str, dict[str, object]] = {}
_lock = Lock()

# Default lifetime — generous enough to cover the longest turn we've
# observed (~30 min auto-compact + heavy work). Cleaner solutions
# would tie expiry to "turn ended"; we revoke explicitly there too.
DEFAULT_TTL_SECONDS = 60 * 60 * 2  # 2h


def mint(caller_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Mint a fresh token bound to `caller_id`.

    Returns the token string. Caller is responsible for getting it to
    the subprocess via env (never argv — visible in `ps`).
    """
    token = secrets.token_urlsafe(32)
    with _lock:
        _tokens[token] = {
            "caller_id": caller_id,
            "expires_at": time.monotonic() + ttl_seconds,
        }
    return token


def resolve(token: str) -> str | None:
    """Return the bound `caller_id` for `token`, or None if missing /
    expired.

    Lazy expiry — we don't background-sweep, just check on lookup.
    """
    with _lock:
        rec = _tokens.get(token)
        if rec is None:
            return None
        if time.monotonic() >= float(rec["expires_at"]):  # type: ignore[arg-type]
            _tokens.pop(token, None)
            return None
        return str(rec["caller_id"])


def revoke(token: str) -> None:
    """Remove a token. Called when the turn ends so a leaked token
    can't outlive its turn."""
    with _lock:
        _tokens.pop(token, None)


def revoke_for_caller(caller_id: str) -> int:
    """Revoke every live token bound to `caller_id`. Belt-and-braces
    cleanup if a turn ends without a paired revoke() (cancelled
    mid-flight, crash). Returns the number of tokens removed."""
    with _lock:
        victims = [t for t, rec in _tokens.items() if rec.get("caller_id") == caller_id]
        for t in victims:
            _tokens.pop(t, None)
    return len(victims)

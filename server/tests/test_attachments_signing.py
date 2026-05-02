"""Tests for HMAC-signed attachment URLs.

The signing module is the unit; the endpoints exercise the
integration. Both layers are pinned because the security guarantee
relies on:
  - constant-time signature comparison,
  - per-deploy persistent key (no key per request),
  - exp-in-the-past rejection,
  - filename-binding (a leaked sig can't be reused for a different
    file).
"""

from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from server import attachments_signing as sigmod
from server.db import init_db


def _init_db() -> None:
    """Sync wrapper so non-async tests get the schema in place."""
    asyncio.get_event_loop().run_until_complete(init_db())


# ---------- module-level signing primitives ----------


def test_signing_key_persists_across_calls(fresh_db) -> None:
    """The deploy's signing key is generated once + cached. Two
    `_signing_key()` calls return the same value so URLs minted
    moments apart resolve to the same HMAC."""
    _init_db()
    sigmod.reset_cache_for_tests()
    k1 = sigmod._signing_key()
    k2 = sigmod._signing_key()
    assert k1 == k2
    assert len(k1) >= 40, "key should be ~43 chars urlsafe-base64"


def test_signing_key_persists_across_cache_reset(fresh_db) -> None:
    """Clearing the in-process cache reads the persisted key — a
    process restart shouldn't invalidate the URLs the previous
    process minted."""
    _init_db()
    sigmod.reset_cache_for_tests()
    k1 = sigmod._signing_key()
    sigmod.reset_cache_for_tests()
    k2 = sigmod._signing_key()
    assert k1 == k2


def test_mint_then_verify_round_trips(fresh_db) -> None:
    _init_db()
    sigmod.reset_cache_for_tests()
    url = sigmod.mint_signed_url("abc123.png", ttl_seconds=120)
    assert url.startswith("/api/attachments/abc123.png/signed?")
    # Pull exp + sig out of the query string.
    import urllib.parse

    qs = urllib.parse.parse_qs(url.split("?", 1)[1])
    assert sigmod.verify_signed("abc123.png", qs["exp"][0], qs["sig"][0])


def test_verify_rejects_expired(fresh_db, monkeypatch) -> None:
    sigmod.reset_cache_for_tests()
    # Patch time.time so the URL appears already-expired the moment
    # it's verified.
    real_time = time.time
    monkeypatch.setattr(sigmod, "time", type("T", (), {"time": staticmethod(real_time)}))
    url = sigmod.mint_signed_url("abc.png", ttl_seconds=60)
    import urllib.parse

    qs = urllib.parse.parse_qs(url.split("?", 1)[1])
    # Now jump time forward past the exp.
    monkeypatch.setattr(
        sigmod, "time",
        type("T", (), {"time": staticmethod(lambda: real_time() + 3600)}),
    )
    assert not sigmod.verify_signed("abc.png", qs["exp"][0], qs["sig"][0])


def test_verify_rejects_filename_swap(fresh_db) -> None:
    """A sig minted for `a.png` must NOT validate when used with `b.png`
    — the filename is part of the HMAC input."""
    sigmod.reset_cache_for_tests()
    url = sigmod.mint_signed_url("a.png")
    import urllib.parse

    qs = urllib.parse.parse_qs(url.split("?", 1)[1])
    assert sigmod.verify_signed("a.png", qs["exp"][0], qs["sig"][0])
    # Same exp + sig, different filename → fail.
    assert not sigmod.verify_signed("b.png", qs["exp"][0], qs["sig"][0])


def test_verify_rejects_mangled_sig(fresh_db) -> None:
    sigmod.reset_cache_for_tests()
    url = sigmod.mint_signed_url("c.png")
    import urllib.parse

    qs = urllib.parse.parse_qs(url.split("?", 1)[1])
    bad = qs["sig"][0][:-1] + ("0" if qs["sig"][0][-1] != "0" else "1")
    assert not sigmod.verify_signed("c.png", qs["exp"][0], bad)


def test_verify_rejects_non_numeric_exp(fresh_db) -> None:
    sigmod.reset_cache_for_tests()
    assert not sigmod.verify_signed("c.png", "not-a-number", "abc")


# ---------- HTTP integration ----------


def _client(monkeypatch):
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    sigmod.reset_cache_for_tests()
    from server.main import app
    return TestClient(app)


def test_upload_response_includes_signed_url(fresh_db, monkeypatch) -> None:
    import io
    c = _client(monkeypatch)
    files = {"file": ("hi.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50), "image/png")}
    with c:
        r = c.post("/api/attachments", files=files)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "signed_url" in body
        assert body["signed_url"].endswith("/signed?" + body["signed_url"].split("?", 1)[1])
        assert "/api/attachments/" in body["signed_url"]


def test_signed_url_serves_bytes_without_token(fresh_db, monkeypatch) -> None:
    """End-to-end: upload, then GET the signed URL with no Authorization
    header and no `?token=` — bytes come back."""
    import io
    c = _client(monkeypatch)
    files = {"file": ("p.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50), "image/png")}
    with c:
        upload = c.post("/api/attachments", files=files).json()
        # Hit the signed URL with no auth header.
        signed = upload["signed_url"]
        r = c.get(signed)
        assert r.status_code == 200, r.text
        assert r.headers.get("content-type", "").startswith("image/png")


def test_signed_url_rejects_tampered_sig(fresh_db, monkeypatch) -> None:
    import io
    c = _client(monkeypatch)
    files = {"file": ("p.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50), "image/png")}
    with c:
        upload = c.post("/api/attachments", files=files).json()
        signed = upload["signed_url"]
        # Flip a character in the sig.
        bad = signed.replace("sig=", "sig=00")[:-1] + "f"
        r = c.get(bad)
        assert r.status_code == 403


def test_signed_url_remint_endpoint_requires_token(
    fresh_db, monkeypatch
) -> None:
    """The re-mint endpoint is auth'd — the audit pattern is "minting
    requires the token, only the signed URL itself is anonymous".

    Use `setattr` (auto-reverted) instead of `importlib.reload` so the
    HARNESS_TOKEN binding doesn't leak into later test files."""
    sigmod.reset_cache_for_tests()
    import server.main as mainmod
    monkeypatch.setattr(mainmod, "HARNESS_TOKEN", "tok")

    with TestClient(mainmod.app) as c:
        r = c.get("/api/attachments/anyfile.png/signed-url")
        # Without the token, the request is rejected.
        assert r.status_code in (401, 403)


def test_legacy_token_endpoint_still_works(fresh_db, monkeypatch) -> None:
    """Backwards compat: the existing `?token=` serve path stays for
    one release so cached UI bundles keep working."""
    import io
    sigmod.reset_cache_for_tests()
    import server.main as mainmod
    monkeypatch.setattr(mainmod, "HARNESS_TOKEN", "tok")

    with TestClient(mainmod.app) as c:
        files = {"file": ("p.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50), "image/png")}
        upload = c.post(
            "/api/attachments",
            files=files,
            headers={"Authorization": "Bearer tok"},
        ).json()
        # Legacy URL with ?token=
        legacy_url = f"{upload['url']}?token=tok"
        r = c.get(legacy_url)
        assert r.status_code == 200

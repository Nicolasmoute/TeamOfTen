"""Tests for the /api/attachments size cap + magic-byte guard.

Two defenses on a single endpoint:

  1. **Magic-byte match**: file content must start with the byte
     signature for its declared extension (PNG/JPEG/GIF/WebP).
     Stops a polyglot upload — e.g. a renamed `.html` file that some
     browser would render as script when fetched.
  2. **30 MB streaming cap**: aborts mid-upload + deletes the partial
     file. Stops disk-fill DoS.
"""

from __future__ import annotations

import io

from fastapi.testclient import TestClient


def _png_bytes(n_bytes: int = 100) -> bytes:
    """Minimal valid PNG header padded to `n_bytes`. The pad is
    arbitrary — we only test the magic-byte gate, not full PNG
    parsability."""
    header = b"\x89PNG\r\n\x1a\n"
    return header + b"\x00" * max(0, n_bytes - len(header))


def _client(monkeypatch):
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    from server.main import app
    return TestClient(app)


def test_png_with_correct_magic_succeeds(fresh_db, monkeypatch) -> None:
    c = _client(monkeypatch)
    files = {"file": ("real.png", io.BytesIO(_png_bytes(200)), "image/png")}
    with c:
        r = c.post("/api/attachments", files=files)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["filename"].endswith(".png")
        assert body["size"] == 200


def test_polyglot_upload_rejected(fresh_db, monkeypatch) -> None:
    """A `.png` filename with HTML content gets refused via magic-byte
    check — the audit's stated motivation for adding the check."""
    c = _client(monkeypatch)
    payload = b"<html><script>alert(1)</script></html>"
    files = {"file": ("evil.png", io.BytesIO(payload), "image/png")}
    with c:
        r = c.post("/api/attachments", files=files)
        assert r.status_code == 400
        assert "magic-byte" in r.text.lower() or "extension" in r.text.lower()


def test_oversize_upload_rejected_streaming(
    fresh_db, monkeypatch
) -> None:
    """31 MB upload aborts before the file is fully stored. The
    response code is 413 (Payload Too Large)."""
    from server.main import MAX_ATTACHMENT_BYTES
    monkeypatch.setattr(
        "server.main.MAX_ATTACHMENT_BYTES", 1024
    )  # tighten for fast test
    c = _client(monkeypatch)
    # Valid PNG header so we get past the magic-byte gate, then 2KB
    # of content puts us over the patched 1KB cap.
    payload = _png_bytes(2048)
    files = {"file": ("big.png", io.BytesIO(payload), "image/png")}
    with c:
        r = c.post("/api/attachments", files=files)
        assert r.status_code == 413
        assert "cap" in r.text.lower() or "exceeds" in r.text.lower()


def test_unknown_extension_rejected(fresh_db, monkeypatch) -> None:
    c = _client(monkeypatch)
    files = {"file": ("evil.exe", io.BytesIO(b"MZ"), "application/octet-stream")}
    with c:
        r = c.post("/api/attachments", files=files)
        assert r.status_code == 400
        assert "extension" in r.text.lower()


def test_empty_upload_rejected(fresh_db, monkeypatch) -> None:
    c = _client(monkeypatch)
    files = {"file": ("blank.png", io.BytesIO(b""), "image/png")}
    with c:
        r = c.post("/api/attachments", files=files)
        assert r.status_code == 400


def test_jpeg_magic_match_succeeds(fresh_db, monkeypatch) -> None:
    c = _client(monkeypatch)
    payload = b"\xff\xd8\xff" + b"\x00" * 50
    files = {"file": ("photo.jpg", io.BytesIO(payload), "image/jpeg")}
    with c:
        r = c.post("/api/attachments", files=files)
        assert r.status_code == 200, r.text


def test_gif_magic_match_succeeds(fresh_db, monkeypatch) -> None:
    c = _client(monkeypatch)
    payload = b"GIF89a" + b"\x00" * 50
    files = {"file": ("anim.gif", io.BytesIO(payload), "image/gif")}
    with c:
        r = c.post("/api/attachments", files=files)
        assert r.status_code == 200, r.text


def test_webp_magic_match_succeeds(fresh_db, monkeypatch) -> None:
    c = _client(monkeypatch)
    payload = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 50
    files = {"file": ("pic.webp", io.BytesIO(payload), "image/webp")}
    with c:
        r = c.post("/api/attachments", files=files)
        assert r.status_code == 200, r.text


def test_jpeg_filename_with_png_content_rejected(
    fresh_db, monkeypatch
) -> None:
    """Cross-format mismatch: PNG bytes uploaded as `.jpg` — fails magic
    check (JPEG signature is FF D8 FF, not PNG's 89 50 4E 47)."""
    c = _client(monkeypatch)
    files = {"file": ("mislabeled.jpg", io.BytesIO(_png_bytes(100)), "image/jpeg")}
    with c:
        r = c.post("/api/attachments", files=files)
        assert r.status_code == 400

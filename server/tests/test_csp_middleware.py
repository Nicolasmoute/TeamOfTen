"""Tests for the Content-Security-Policy middleware.

Pin the headers the harness ships so a future tweak that, say,
adds an external script source has to update this test too — making
the policy change visible in code review.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _client(monkeypatch):
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    from server.main import app
    return TestClient(app)


def test_csp_header_set_on_public_endpoint(fresh_db, monkeypatch) -> None:
    c = _client(monkeypatch)
    with c:
        r = c.get("/api/health")
        assert r.status_code == 200
        csp = r.headers.get("Content-Security-Policy", "")
        assert csp, "CSP header missing"
        # Required directives — pin against accidental loosening.
        assert "default-src 'self'" in csp
        assert "script-src 'self' https://esm.sh" in csp
        assert "object-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "base-uri 'self'" in csp
        assert "form-action 'self'" in csp
        assert "connect-src 'self'" in csp


def test_csp_does_not_allow_unsafe_inline_scripts(
    fresh_db, monkeypatch
) -> None:
    """`'unsafe-inline'` only applies to styles. A regression that
    extended it to scripts would defeat the main XSS defense."""
    c = _client(monkeypatch)
    with c:
        r = c.get("/api/health")
        csp = r.headers.get("Content-Security-Policy", "")
        # Find the script-src directive and assert no unsafe-inline.
        for clause in csp.split(";"):
            clause = clause.strip()
            if clause.startswith("script-src"):
                assert "'unsafe-inline'" not in clause, clause
                assert "'unsafe-eval'" not in clause, clause
                break
        else:
            raise AssertionError("script-src directive missing")


def test_companion_security_headers_set(fresh_db, monkeypatch) -> None:
    c = _client(monkeypatch)
    with c:
        r = c.get("/api/health")
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("Referrer-Policy") == "no-referrer"


def test_csp_applied_to_static_assets(fresh_db, monkeypatch) -> None:
    """The static UI is the main XSS surface — CSP must reach it. We
    can't easily test a real static path here without the bootstrap
    bundle, but a 404 on a static path still gets the header from the
    middleware (the response goes through it)."""
    c = _client(monkeypatch)
    with c:
        r = c.get("/static/does-not-exist.js")
        # 404 or 200 — what matters is the header exists.
        assert r.headers.get("Content-Security-Policy"), (
            "CSP header missing on static path"
        )


def test_csp_applied_to_root_html(fresh_db, monkeypatch) -> None:
    """The root HTML page is where browser XSS would actually execute —
    the response is wrapped by the middleware so CSP applies."""
    c = _client(monkeypatch)
    with c:
        r = c.get("/")
        # 200 if the static bundle is in place, 500 / 404 in some test
        # environments — what matters is the header is present either
        # way (FastAPI serves the response through the middleware
        # chain regardless of the route's own status code).
        assert r.headers.get("Content-Security-Policy"), (
            "CSP header missing on root HTML"
        )


def test_esm_sh_in_script_src(fresh_db, monkeypatch) -> None:
    """preact + preact/hooks are loaded from esm.sh per the repo's
    vendoring strategy. The policy must permit it."""
    c = _client(monkeypatch)
    with c:
        r = c.get("/api/health")
        csp = r.headers.get("Content-Security-Policy", "")
        # esm.sh appears specifically in script-src, not anywhere else.
        for clause in csp.split(";"):
            clause = clause.strip()
            if clause.startswith("script-src"):
                assert "https://esm.sh" in clause


def test_jsdelivr_in_font_src_only(fresh_db, monkeypatch) -> None:
    """jsdelivr is allowed for fonts (KaTeX) but NOT for scripts —
    a regression that adds it to script-src would broaden the
    supply-chain surface."""
    c = _client(monkeypatch)
    with c:
        r = c.get("/api/health")
        csp = r.headers.get("Content-Security-Policy", "")
        for clause in csp.split(";"):
            clause = clause.strip()
            if clause.startswith("script-src"):
                assert "jsdelivr" not in clause, (
                    "jsdelivr leaked into script-src"
                )
            if clause.startswith("font-src"):
                assert "cdn.jsdelivr.net" in clause

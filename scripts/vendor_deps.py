"""
Vendor pinned frontend ESM dependencies into server/static/vendor/.

Why: the harness imports Preact + a dozen highlight.js language packs
from esm.sh on every cold load. On Zeabur's EU egress + a mobile
client that's a noticeable first-paint delay, and any esm.sh hiccup
breaks the UI entirely. Vendoring same-origin ESM bundles eliminates
both — at the cost of a one-time refresh step when we want to bump a
version.

Strategy:
  * Leaf libraries (htm, split.js, marked, dompurify, diff, hljs core +
    its language packs) are fetched with esm.sh's `?bundle` flag, which
    inlines their transitive deps into one self-contained file. These
    libraries don't share runtime state across module boundaries, so
    independent bundles are safe.
  * Preact + preact/hooks are *intentionally* NOT vendored here. They
    rely on shared component-instance state across the preact /
    preact/hooks module boundary; bundling each independently produces
    two separate Preact instances and useState silently breaks. They
    stay on esm.sh until we ship a real importmap-based setup.
  * github-dark.css (highlight.js theme) is fetched as plain CSS.

How to refresh: edit DEPS or CSS_DEPS below, then run
    python scripts/vendor_deps.py
Output goes to server/static/vendor/. Commit the result.
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "server" / "static" / "vendor"

# Each entry: (output filename, source URL).
# `?bundle` tells esm.sh to inline transitive deps into one file.
DEPS: list[tuple[str, str]] = [
    ("htm.js", "https://esm.sh/htm@3?bundle"),
    ("split.js", "https://esm.sh/split.js@1.6.5?bundle"),
    ("marked.js", "https://esm.sh/marked@12?bundle"),
    ("dompurify.js", "https://esm.sh/dompurify@3?bundle"),
    ("diff.js", "https://esm.sh/diff@7?bundle"),
    ("hljs-core.js", "https://esm.sh/highlight.js@11/lib/core?bundle"),
    ("hljs-bash.js", "https://esm.sh/highlight.js@11/lib/languages/bash?bundle"),
    ("hljs-css.js", "https://esm.sh/highlight.js@11/lib/languages/css?bundle"),
    ("hljs-go.js", "https://esm.sh/highlight.js@11/lib/languages/go?bundle"),
    ("hljs-json.js", "https://esm.sh/highlight.js@11/lib/languages/json?bundle"),
    ("hljs-javascript.js", "https://esm.sh/highlight.js@11/lib/languages/javascript?bundle"),
    ("hljs-markdown.js", "https://esm.sh/highlight.js@11/lib/languages/markdown?bundle"),
    ("hljs-python.js", "https://esm.sh/highlight.js@11/lib/languages/python?bundle"),
    ("hljs-rust.js", "https://esm.sh/highlight.js@11/lib/languages/rust?bundle"),
    ("hljs-sql.js", "https://esm.sh/highlight.js@11/lib/languages/sql?bundle"),
    ("hljs-typescript.js", "https://esm.sh/highlight.js@11/lib/languages/typescript?bundle"),
    ("hljs-xml.js", "https://esm.sh/highlight.js@11/lib/languages/xml?bundle"),
    ("hljs-yaml.js", "https://esm.sh/highlight.js@11/lib/languages/yaml?bundle"),
    # Math: KaTeX (eager, ~280KB). The marked extension that drives
    # $...$ / $$...$$ rewriting is hand-rolled inline in markdown.js
    # (it's ~25 lines; the public marked-katex-extension package only
    # ships an esm.sh stub that imports `katex` from the CDN, which
    # 404s when served from our /static/vendor/ origin). Output mode
    # chosen in markdown.js is `htmlAndMathml` so equations render
    # visually AND carry MathML in the DOM — Word paste ingests the
    # MathML directly.
    ("katex.js", "https://esm.sh/katex@0.16?bundle"),
    # Diagrams: Mermaid UMD bundle (~3MB, self-contained). Loaded
    # lazily via a dynamic <script> tag in markdown.js (UMD sets
    # `window.mermaid`); only paid on the first page that contains
    # a ```mermaid block. Browser caches it forever after.
    #
    # NOTE: We can't use mermaid's ESM bundle here because it
    # splits into 30+ chunks for code-split diagram types (each
    # `flowchart`, `sequenceDiagram`, etc. is its own .mjs).
    # esm.sh's `?bundle` flag doesn't inline them, jsdelivr's ESM
    # entry-point still references the chunk paths. The UMD build
    # (`dist/mermaid.min.js`) is the only single-file distribution,
    # and it's the simplest to vendor.
]
NON_ESM_DEPS: list[tuple[str, str]] = [
    (
        "mermaid.min.js",
        "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js",
    ),
]

CSS_DEPS: list[tuple[str, str]] = [
    (
        "hljs-github-dark.css",
        "https://cdn.jsdelivr.net/npm/highlight.js@11/styles/github-dark.min.css",
    ),
    # KaTeX font + layout CSS. The published file references woff2
    # fonts via relative `fonts/...` paths; we rewrite them in
    # `_postprocess_css_body` below to point at jsdelivr's
    # /dist/fonts/ directory so we don't have to vendor 12 binary
    # font files into the repo.
    (
        "katex.css",
        "https://cdn.jsdelivr.net/npm/katex@0.16/dist/katex.min.css",
    ),
]

# Filename → (regex, replacement) post-processing applied after fetch.
# Used to rewrite KaTeX's relative font URLs into absolute CDN URLs so
# the vendored CSS works without us also vendoring the font files.
_CSS_REWRITES: dict[str, tuple[re.Pattern, str]] = {
    "katex.css": (
        re.compile(rb"url\((?P<q>['\"]?)fonts/"),
        rb"url(\g<q>https://cdn.jsdelivr.net/npm/katex@0.16/dist/fonts/",
    ),
}


_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# esm.sh `?bundle` URLs return a tiny re-export wrapper that points at
# the real self-contained bundle file. Match that wrapper so we can
# chase the redirect and save the actual content.
_REEXPORT = re.compile(
    r'export\s*\*\s*from\s*["\'](?P<path>[^"\']+)["\']\s*;?\s*$',
    re.MULTILINE,
)


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _UA, "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _resolve_bundle(url: str, body: bytes, max_hops: int = 3) -> bytes:
    # Follow up to `max_hops` levels of `export * from "..."` wrappers
    # so we end up with the real bundle content. Caps the chain so a
    # malformed response can't loop forever.
    for _ in range(max_hops):
        if len(body) > 4096:
            return body  # too big to be a wrapper; assume real content
        text = body.decode("utf-8", errors="replace").strip()
        if "export *" not in text or text.count("\n") > 6:
            return body
        match = _REEXPORT.search(text)
        if not match:
            return body
        next_url = urljoin(url, match.group("path"))
        url = next_url
        body = _fetch(next_url)
    return body


def main() -> int:
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    for fname, url in DEPS:
        out = VENDOR_DIR / fname
        try:
            body = _resolve_bundle(url, _fetch(url))
        except Exception as exc:
            print(f"  FAIL {fname}: {exc}", file=sys.stderr)
            failures.append(fname)
            continue
        if not body or len(body) < 256:
            print(f"  FAIL {fname}: response too small ({len(body)} bytes)", file=sys.stderr)
            failures.append(fname)
            continue
        # Sanity: a bundled JS module should not contain absolute esm.sh
        # URLs. If it does, our `?bundle` flag silently failed and the
        # file would still pull from CDN at runtime.
        if b"https://esm.sh/" in body or b'from "/' in body:
            print(f"  WARN {fname}: contains external imports — bundle may be incomplete", file=sys.stderr)
        out.write_bytes(body)
        print(f"  OK   {fname:<24}  ({len(body):>7} bytes)")
    # Non-ESM bundles (UMD/IIFE etc.) — fetched as-is. No bundle-chase,
    # no ESM-import sanity check. Loaded via <script> tag at runtime,
    # not via the module pipeline.
    for fname, url in NON_ESM_DEPS:
        out = VENDOR_DIR / fname
        try:
            body = _fetch(url)
        except Exception as exc:
            print(f"  FAIL {fname}: {exc}", file=sys.stderr)
            failures.append(fname)
            continue
        if not body or len(body) < 256:
            print(f"  FAIL {fname}: response too small ({len(body)} bytes)", file=sys.stderr)
            failures.append(fname)
            continue
        out.write_bytes(body)
        print(f"  OK   {fname:<24}  ({len(body):>7} bytes)")
    for fname, url in CSS_DEPS:
        out = VENDOR_DIR / fname
        try:
            body = _fetch(url)
        except Exception as exc:
            print(f"  FAIL {fname}: {exc}", file=sys.stderr)
            failures.append(fname)
            continue
        if not body or len(body) < 256:
            print(f"  FAIL {fname}: response too small ({len(body)} bytes)", file=sys.stderr)
            failures.append(fname)
            continue
        rewrite = _CSS_REWRITES.get(fname)
        if rewrite is not None:
            pattern, replacement = rewrite
            body = pattern.sub(replacement, body)
        out.write_bytes(body)
        print(f"  OK   {fname:<24}  ({len(body):>7} bytes)")
    if failures:
        print(f"\n{len(failures)} failure(s): {', '.join(failures)}", file=sys.stderr)
        return 1
    total = len(DEPS) + len(NON_ESM_DEPS) + len(CSS_DEPS)
    print(f"\nVendored {total} files into {VENDOR_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

// Centralised markdown render pipeline for the harness UI.
//
// One chokepoint: every pane (agent panes, files .md preview, compass
// dashboard, decisions, briefings) routes its markdown through
// `renderMarkdown` here. Adding a renderer (math, diagrams, …) lights
// it up everywhere automatically — no per-consumer changes.
//
// Pipeline:
//   1. parse  — `marked` (GFM) with a custom code-renderer:
//                * fence lang ∈ hljs registry → highlighted <pre><code>
//                * fence lang === "mermaid"   → <pre class="md-mermaid"> placeholder
//                * everything else            → escaped <pre><code>
//   2. parse  — `marked-katex-extension` rewrites `$...$` / `$$...$$`
//               into KaTeX-rendered HTML+MathML (sync, ~280KB eager).
//   3. sanitize — DOMPurify with the html+mathMl profile, plus the
//                  link rewrite hook (file-paths → harness Files pane,
//                  external URLs → new tab + opaque referrer).
//   4. mount  — consumer drops the sanitised string into Preact via
//                `dangerouslySetInnerHTML`.
//   5. enhance — a MutationObserver rooted at the body lazy-loads
//                 mermaid on the first `<pre class="md-mermaid">` it
//                 sees and replaces the placeholder with the rendered
//                 SVG. Subsequent diagrams reuse the loaded module.
//                 A WeakSet de-dupes already-processed nodes; a
//                 source→SVG cache makes re-renders instant when
//                 Preact remounts the same diagram text.

import { Marked } from "/static/vendor/marked.js";
import DOMPurify from "/static/vendor/dompurify.js";
import katex from "/static/vendor/katex.js";
import markedKatex from "/static/vendor/marked-katex-extension.js";
import hljs from "/static/vendor/hljs-core.js";
import hljsBash from "/static/vendor/hljs-bash.js";
import hljsCss from "/static/vendor/hljs-css.js";
import hljsGo from "/static/vendor/hljs-go.js";
import hljsJson from "/static/vendor/hljs-json.js";
import hljsJs from "/static/vendor/hljs-javascript.js";
import hljsMd from "/static/vendor/hljs-markdown.js";
import hljsPython from "/static/vendor/hljs-python.js";
import hljsRust from "/static/vendor/hljs-rust.js";
import hljsSql from "/static/vendor/hljs-sql.js";
import hljsTs from "/static/vendor/hljs-typescript.js";
import hljsXml from "/static/vendor/hljs-xml.js";
import hljsYaml from "/static/vendor/hljs-yaml.js";

// hljs language packs. Aliases match the names agents are likely to
// emit in fence info-strings. Adding more later is a one-liner: import
// the pack from https://esm.sh/highlight.js@11/lib/languages/<name>
// (vendor it via scripts/vendor_deps.py) and register it here.
hljs.registerLanguage("bash", hljsBash);
hljs.registerLanguage("sh", hljsBash);
hljs.registerLanguage("shell", hljsBash);
hljs.registerLanguage("css", hljsCss);
hljs.registerLanguage("go", hljsGo);
hljs.registerLanguage("html", hljsXml);
hljs.registerLanguage("xml", hljsXml);
hljs.registerLanguage("javascript", hljsJs);
hljs.registerLanguage("js", hljsJs);
hljs.registerLanguage("json", hljsJson);
hljs.registerLanguage("markdown", hljsMd);
hljs.registerLanguage("md", hljsMd);
hljs.registerLanguage("python", hljsPython);
hljs.registerLanguage("py", hljsPython);
hljs.registerLanguage("rust", hljsRust);
hljs.registerLanguage("rs", hljsRust);
hljs.registerLanguage("sql", hljsSql);
hljs.registerLanguage("typescript", hljsTs);
hljs.registerLanguage("ts", hljsTs);
hljs.registerLanguage("yaml", hljsYaml);
hljs.registerLanguage("yml", hljsYaml);

// Single Marked instance. `gfm` enables tables, task lists, autolinks,
// strikethrough; `breaks: false` keeps the standard "blank line ends a
// paragraph" behaviour (matches Obsidian + GitHub).
const marked = new Marked({
  gfm: true,
  breaks: false,
  pedantic: false,
});

// Code-renderer: hljs for known langs; placeholder <pre> for mermaid;
// escaped fallback for everything else. Keeps unknown langs readable
// instead of dumping unhighlighted HTML that DOMPurify might mangle.
function _escape(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

marked.use({
  renderer: {
    code(code, infostring) {
      const text = typeof code === "object" && code ? (code.text || "") : String(code || "");
      const lang = (typeof infostring === "string"
        ? infostring
        : typeof code === "object" && code
          ? (code.lang || "")
          : ""
      ).trim().toLowerCase();
      if (lang === "mermaid") {
        // Placeholder. The body is the source verbatim, escaped so
        // DOMPurify keeps it. The MutationObserver below will swap
        // the <pre> for an <svg> once mermaid is loaded.
        return `<pre class="md-mermaid"><code>${_escape(text)}</code></pre>`;
      }
      if (lang && hljs.getLanguage(lang)) {
        try {
          const highlighted = hljs.highlight(text, {
            language: lang, ignoreIllegals: true,
          }).value;
          return `<pre class="md-code"><code class="hljs language-${lang}" data-lang="${lang}">${highlighted}</code></pre>`;
        } catch (_) {
          // Fall through to escaped plaintext.
        }
      }
      return `<pre class="md-code"><code class="hljs"${lang ? ` data-lang="${lang}"` : ""}>${_escape(text)}</code></pre>`;
    },
  },
});

// KaTeX extension. Output mode `htmlAndMathml` (the default) emits both
// the styled HTML (for crisp visual rendering) AND a hidden
// <annotation>-wrapped MathML representation (for screen readers AND
// for copy-paste into Word, which ingests MathML directly). That
// covers the "render LaTeX in the harness" + "paste into Word"
// workflow without a second tool.
//
// throwOnError: false → invalid LaTeX renders red instead of throwing,
// so a stray `$` in plain text never blanks a chat message.
marked.use(markedKatex({
  throwOnError: false,
  errorColor: "#cc6666",
  output: "htmlAndMathml",
}));

// Link handling for sanitised markdown:
//   - external URL (http/https/mailto) → open in new tab
//   - file path (anything starting with `/`) → marked as a harness
//     file-link; the global click handler in App intercepts it,
//     opens the Files pane, and selects the file. href is neutralised
//     to "#" so a stray middle-click doesn't 404.
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName !== "A" || !node.hasAttribute("href")) return;
  const href = node.getAttribute("href") || "";
  if (href.startsWith("/") && !href.startsWith("//")) {
    node.setAttribute("data-harness-path", href);
    node.setAttribute("href", "#");
    node.classList.add("harness-file-link");
    // No target — we're handling navigation in-app, not opening a tab.
    node.removeAttribute("target");
    node.setAttribute("rel", "noopener");
    return;
  }
  // External — open in new tab + opaque referrer.
  node.setAttribute("target", "_blank");
  node.setAttribute("rel", "noreferrer noopener");
});

// DOMPurify config. `USE_PROFILES.mathMl` whitelists the MathML tags
// KaTeX emits in `htmlAndMathml` mode (math, mrow, mi, mo, mn, msup,
// msub, mfrac, msqrt, semantics, annotation, …). `html` is the
// default web-safe HTML allowlist. ADD_ATTR carries our custom data-
// attrs through (data-lang for code, data-harness-path for in-app
// file-link routing — set by the link hook above).
const _SANITIZE_OPTS = {
  USE_PROFILES: { html: true, mathMl: true },
  ADD_ATTR: ["target", "rel", "data-lang", "data-harness-path"],
};

export function renderMarkdown(md) {
  if (!md) return "";
  let raw;
  try {
    raw = marked.parse(String(md));
  } catch (e) {
    console.error("markdown parse failed", e);
    // Fall back to escaped plaintext wrapped in a code block so the
    // user still sees something. Routed through DOMPurify just like
    // the happy path so the file-link hook fires consistently and we
    // never bypass the sanitiser.
    raw = "<pre class=\"md-code\"><code>" + _escape(String(md)) + "</code></pre>";
  }
  return DOMPurify.sanitize(raw, _SANITIZE_OPTS);
}

// ------------------------------------------------------------------
// Mermaid: lazy-loaded post-render enhancement
// ------------------------------------------------------------------
//
// Mermaid is ~3MB, so loading it eagerly on every page would punish
// every cold start whether or not the user has any diagrams. Instead,
// a single MutationObserver rooted at the body watches for inserts of
// <pre class="md-mermaid"> placeholders. The first hit triggers a
// dynamic import; subsequent hits reuse the loaded module.
//
// Re-render behaviour: Preact rerenders that swap the parent element's
// HTML will revert the rendered <svg> back to the original <pre>
// placeholder (Preact has no awareness of our SVG mount). The observer
// catches the new insertion and re-renders. The render cache makes
// this effectively free.

const _renderedMermaidNodes = new WeakSet();
const _mermaidCache = new Map(); // source → svg html

let _mermaidPromise = null;
function _loadMermaid() {
  if (_mermaidPromise) return _mermaidPromise;
  _mermaidPromise = import("/static/vendor/mermaid.js").then((mod) => {
    const mermaid = mod.default || mod;
    mermaid.initialize({
      startOnLoad: false,
      theme: "dark",
      securityLevel: "strict",
      fontFamily: "inherit",
    });
    return mermaid;
  }).catch((err) => {
    console.error("mermaid load failed", err);
    _mermaidPromise = null; // allow retry on next placeholder
    throw err;
  });
  return _mermaidPromise;
}

let _mermaidIdCounter = 0;
async function _renderMermaidNode(preEl) {
  if (_renderedMermaidNodes.has(preEl)) return;
  _renderedMermaidNodes.add(preEl);
  const codeEl = preEl.querySelector("code");
  const source = (codeEl ? codeEl.textContent : preEl.textContent) || "";
  if (!source.trim()) return;
  if (_mermaidCache.has(source)) {
    preEl.innerHTML = _mermaidCache.get(source);
    preEl.classList.add("md-mermaid-rendered");
    return;
  }
  let mermaid;
  try {
    mermaid = await _loadMermaid();
  } catch (_) {
    // Loading failed — leave the source visible so the diagram is at
    // least readable as text. The console error from _loadMermaid is
    // enough; no user-facing toast.
    return;
  }
  try {
    const id = `md-mermaid-${++_mermaidIdCounter}`;
    const { svg } = await mermaid.render(id, source);
    _mermaidCache.set(source, svg);
    // The node may have been detached while we awaited render. Check
    // before mutating to avoid throwing on a stale reference.
    if (preEl.isConnected) {
      preEl.innerHTML = svg;
      preEl.classList.add("md-mermaid-rendered");
    }
  } catch (err) {
    // Bad diagram syntax — show the error inline so the agent can
    // see what went wrong without opening devtools.
    if (preEl.isConnected) {
      preEl.classList.add("md-mermaid-error");
      const msg = (err && err.message) ? err.message : String(err);
      preEl.innerHTML = `<div class="md-mermaid-err-title">Mermaid render failed</div>
        <pre class="md-mermaid-err-msg">${_escape(msg)}</pre>
        <pre class="md-mermaid-err-src">${_escape(source)}</pre>`;
    }
  }
}

function _scanForMermaid(root) {
  if (!root || typeof root.querySelectorAll !== "function") return;
  const nodes = root.querySelectorAll("pre.md-mermaid:not(.md-mermaid-rendered)");
  for (const node of nodes) {
    _renderMermaidNode(node);
  }
}

let _enhanceInstalled = false;
export function enhanceMarkdownIn(rootEl) {
  if (_enhanceInstalled) return;
  _enhanceInstalled = true;
  const root = rootEl || document.body;
  // Initial pass — anything already in the DOM.
  _scanForMermaid(root);
  // Watch for future inserts. childList + subtree covers the common
  // pattern (innerHTML assignment, Preact reconciler appending nodes).
  // attributes/characterData not needed — we only care about new nodes.
  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (node.nodeType !== 1) continue; // ELEMENT_NODE only
        if (node.matches && node.matches("pre.md-mermaid")) {
          _renderMermaidNode(node);
        } else {
          _scanForMermaid(node);
        }
      }
    }
  });
  observer.observe(root, { childList: true, subtree: true });
}

// Expose for non-module pane scripts (compass.js was the original
// caller; future panes loaded via plain <script> can use this too).
if (typeof window !== "undefined") {
  window.__harness_renderMarkdown = renderMarkdown;
}

// hljs is re-exported so the FilesPane code-preview helper in app.js
// can use the configured singleton (with all language packs already
// registered) instead of importing + re-registering. Keeps language
// support consistent between code-fence rendering and standalone
// file preview.
//
// DOMPurify is re-exported for the same reason — the link hook +
// MathML profile config are installed on the singleton here, and any
// other sanitize call in the app should pick them up automatically
// instead of importing a fresh copy.
export { hljs, DOMPurify };

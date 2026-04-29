// Preact + preact/hooks intentionally stay on esm.sh: hooks rely on
// shared component-instance state with the preact module, and esm.sh
// dedupes them across imports on the same origin. Vendoring each as a
// `?bundle` would produce two separate Preact instances and useState
// silently breaks. All other deps are vendored under /static/vendor/
// (see scripts/vendor_deps.py) so cold loads don't fan out 17 cross-
// origin module requests.
import { h, render, Component } from "https://esm.sh/preact@10";
import { useState, useEffect, useMemo, useRef, useCallback, useLayoutEffect } from "https://esm.sh/preact@10/hooks";
import htm from "/static/vendor/htm.js";
import Split from "/static/vendor/split.js";
import { Marked } from "/static/vendor/marked.js";
import DOMPurify from "/static/vendor/dompurify.js";
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
import { renderToolCall, setAgentDirectory, langForFile } from "/static/tools.js";

const html = htm.bind(h);

// ------------------------------------------------------------------
// markdown rendering: marked (GFM) + highlight.js + DOMPurify
// ------------------------------------------------------------------
//
// English-only language argument doesn't apply here — these are
// PROGRAMMING-language packs for syntax highlighting. Adding more
// later is a one-liner: import the pack from
// https://esm.sh/highlight.js@11/lib/languages/<name> and register it
// against the aliases agents are likely to use.

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

// Single Marked instance with GFM (tables, task lists, autolinks,
// strikethrough). Custom code-block renderer runs hljs when the fence
// info-string names a registered language; falls back to plain
// escaped <pre><code> otherwise (keeps unknown langs readable).
const marked = new Marked({
  gfm: true,
  breaks: false,
  pedantic: false,
});
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
      const esc = text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
      return `<pre class="md-code"><code class="hljs"${lang ? ` data-lang="${lang}"` : ""}>${esc}</code></pre>`;
    },
  },
});

// Link handling for sanitized markdown:
//   - external URL (http/https/mailto) → open in new tab
//   - file path (anything starting with `/`) → marked as a harness
//     file-link; the global click handler in App intercepts it,
//     opens the Files pane, and selects the file. href is neutralized
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

function renderMarkdown(md) {
  if (!md) return "";
  let raw;
  try {
    raw = marked.parse(String(md));
  } catch (e) {
    console.error("markdown parse failed", e);
    // Fall back to escaped plaintext wrapped in a code block so the
    // user still sees something. Routed through DOMPurify just like
    // the happy path so the file-link / external-link hooks fire
    // consistently and we never bypass the sanitizer.
    raw = "<pre class=\"md-code\"><code>" +
      String(md).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;") +
      "</code></pre>";
  }
  return DOMPurify.sanitize(raw, {
    ADD_ATTR: ["target", "rel", "data-lang"],
  });
}

// FilesPane helpers — extension allowlist for what we'll inline-preview
// and a syntax-highlighted code renderer for non-markdown text files.
//
// The allowlist is conservative: only formats we know render readably.
// Files outside the allowlist are treated as binary — the editor
// shows a placeholder with size + extension and never fetches the
// body, so opening a 50 MB blob doesn't lock the UI or burn the
// /api/files/read 256 KB cap.
const FILES_TEXT_EXTENSIONS = new Set([
  ".md", ".markdown", ".mdx",
  ".txt", ".log", ".text",
  ".py", ".pyi",
  ".js", ".mjs", ".cjs", ".jsx",
  ".ts", ".tsx", ".d.ts",
  ".json", ".jsonc", ".jsonl",
  ".yaml", ".yml",
  ".toml",
  ".css", ".scss", ".sass", ".less",
  ".html", ".htm", ".xml", ".svg",
  ".go", ".rs",
  ".sh", ".bash", ".zsh",
  ".sql",
  ".ini", ".cfg", ".conf",
  ".csv", ".tsv",
  ".env", ".gitignore", ".gitattributes", ".dockerignore",
]);
// Extensionless filenames worth previewing as text. Match by basename.
const FILES_TEXT_BASENAMES = new Set([
  "Dockerfile", "Makefile", "Justfile", "Procfile",
  "README", "LICENSE", "CHANGELOG", "AUTHORS", "CONTRIBUTING",
  ".gitignore", ".gitattributes", ".dockerignore", ".env",
]);

function filesIsPreviewableText(path) {
  if (typeof path !== "string" || path.length === 0) return false;
  const base = path.split("/").pop() || path;
  if (FILES_TEXT_BASENAMES.has(base)) return true;
  const dot = base.lastIndexOf(".");
  if (dot < 0) return false;
  return FILES_TEXT_EXTENSIONS.has(base.slice(dot).toLowerCase());
}

function filesRenderCode(text, path) {
  const safe = String(text == null ? "" : text);
  const lang = langForFile(path);
  let body;
  if (lang && hljs.getLanguage(lang)) {
    try {
      const highlighted = hljs.highlight(safe, {
        language: lang, ignoreIllegals: true,
      }).value;
      body =
        '<pre class="files-code"><code class="hljs language-' + lang +
        '" data-lang="' + lang + '">' + highlighted + "</code></pre>";
    } catch (_) {
      body = null;
    }
  }
  if (!body) {
    const esc = safe
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    body = '<pre class="files-code"><code class="hljs">' + esc + "</code></pre>";
  }
  return DOMPurify.sanitize(body, { ADD_ATTR: ["data-lang"] });
}

// Per-event memo for renderMarkdown. Long panes re-render the App
// tree on every WS event (new event arrives, conversations Map
// updates, every pane's EventItem list maps anew). Without memo, each
// re-render re-parses every markdown event from scratch — quadratic
// behaviour as session length grows. WeakMap auto-evicts entries
// when an event object is dropped (e.g. closed pane or paginated out
// of history), so the cache can't leak. Content-string check guards
// against the rare case where an event mutates in place.
const _markdownCache = new WeakMap();
// Coerce arbitrarily-shaped event.content to a string. Claude thinking
// blocks land here as `[{type:'thinking', thinking:'…'}]`; some Codex
// reasoning items have similar nested shapes. A non-string slipping
// through to a downstream `.split` / `marked.parse` would throw and
// break the entire pane render — taking out click handlers in the
// process. Keep this defensive even when callers think content is a
// string.
function _coerceContentToString(content) {
  if (typeof content === "string") return content;
  if (content == null) return "";
  if (Array.isArray(content)) {
    return content
      .map((c) =>
        typeof c === "string"
          ? c
          : (c && (c.thinking || c.text)) || ""
      )
      .filter(Boolean)
      .join("\n");
  }
  return String(content);
}

function renderMarkdownFor(event) {
  const raw = event && event.content ? event.content : "";
  const content = _coerceContentToString(raw);
  if (!content) return "";
  if (event && typeof event === "object") {
    const cached = _markdownCache.get(event);
    if (cached && cached.content === content) return cached.html;
    const html = renderMarkdown(content);
    _markdownCache.set(event, { content, html });
    return html;
  }
  return renderMarkdown(content);
}

// ------------------------------------------------------------------
// auth: bearer token stored in localStorage
// ------------------------------------------------------------------

const TOKEN_KEY = "harness_token";

function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (_) { return ""; }
}
function setToken(t) {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch (_) {}
}

// Wrap fetch to add Authorization header when we have a token. Servers
// without HARNESS_TOKEN configured ignore the header.
async function authFetch(url, init) {
  const token = getToken();
  const opts = { ...(init || {}) };
  opts.headers = { ...(opts.headers || {}) };
  if (token) opts.headers["Authorization"] = "Bearer " + token;
  return fetch(url, opts);
}

// ------------------------------------------------------------------
// layout persistence: which slots are open + env panel state
// ------------------------------------------------------------------

const LAYOUT_KEY = "harness_layout_v1";

// openColumns is a 2D array: outer = columns left-to-right; inner = panes
// stacked top-to-bottom in that column. e.g. [["coach"], ["p1","p2"], ["p3"]]
// renders as 3 columns: solo Coach | (p1 over p2) | solo p3 — the "H I I H"
// pattern users want.
//
// v1 layouts persisted just openSlots: string[] (flat); we migrate by
// putting each slot in its own column.
function loadLayout() {
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw);
    if (!v || typeof v !== "object") return null;
    let openColumns = null;
    if (Array.isArray(v.openColumns)) {
      openColumns = v.openColumns
        .map((col) =>
          Array.isArray(col)
            ? col.filter((s) => typeof s === "string").slice(0, 11)
            : null
        )
        .filter((col) => col && col.length > 0)
        .slice(0, 11);
    } else if (Array.isArray(v.openSlots)) {
      openColumns = v.openSlots
        .filter((s) => typeof s === "string")
        .slice(0, 11)
        .map((s) => [s]);
    }
    if (!openColumns) return null;
    const envWidth =
      typeof v.envWidth === "number" && v.envWidth >= 260 && v.envWidth <= 900
        ? v.envWidth
        : 340;
    // maximizedSlot: which pane is currently expanded to full panes-area.
    // Validated on render against actual openColumns — a stale value
    // (slot no longer open) just falls back to the multi-pane layout.
    const maximizedSlot =
      typeof v.maximizedSlot === "string" ? v.maximizedSlot : null;
    return {
      openColumns,
      envOpen: typeof v.envOpen === "boolean" ? v.envOpen : true,
      envWidth,
      maximizedSlot,
    };
  } catch (_) {
    return null;
  }
}

function flatSlots(openColumns) {
  const out = [];
  for (const col of openColumns) for (const s of col) out.push(s);
  return out;
}

// Canonical agent slot order: Coach first, then p1..p10. Anything else
// (special slots like __files / __projects) sorts after agents in
// insertion order. Used by the mobile layout to give the swipe deck a
// stable ordering regardless of pane-open history.
const CANONICAL_SLOT_ORDER = [
  "coach", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "p9", "p10",
];
function canonicalSlotIndex(slot) {
  const i = CANONICAL_SLOT_ORDER.indexOf(slot);
  return i === -1 ? CANONICAL_SLOT_ORDER.length : i;
}

// Track whether the viewport is in phone-layout territory (matches the
// `@media (max-width: 700px)` block in style.css). Returns a boolean
// that re-renders the consumer when the breakpoint flips (rotation,
// devtools resize, etc.).
function useIsPhone() {
  const query = "(max-width: 700px)";
  const [isPhone, setIsPhone] = useState(() =>
    typeof window !== "undefined" && window.matchMedia
      ? window.matchMedia(query).matches
      : false
  );
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia(query);
    const handler = (e) => setIsPhone(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return isPhone;
}

// Serialize a pane's events to markdown. Used by per-pane export (↓)
// and whole-team export. `events` may include __result pairings; if
// not, tool_use/tool_result render separately.
function formatEventsAsMarkdown(events, { slot, agent, headingLevel = 1 } = {}) {
  const h = "#".repeat(headingLevel);
  const h2 = "#".repeat(headingLevel + 1);
  const lines = [];
  const name = agent?.name ? ` — ${agent.name}` : "";
  const role = agent?.role ? ` (${agent.role})` : "";
  lines.push(`${h} ${slot}${name}${role}`);
  lines.push(`Events: ${events.length}`);
  lines.push("");
  for (const ev of events) {
    const ts = (ev.ts || "").slice(0, 19).replace("T", " ");
    lines.push(`${h2} [${ts}] ${ev.type}`);
    if (ev.type === "agent_started") {
      lines.push("", "> " + (ev.prompt || "").split("\n").join("\n> "));
    } else if (ev.type === "text") {
      lines.push("", ev.content || "");
    } else if (ev.type === "tool_use") {
      lines.push("", "**" + (ev.name || ev.tool || "tool") + "**");
      lines.push("```json");
      lines.push(JSON.stringify(ev.input || {}, null, 2));
      lines.push("```");
      if (ev.__result) {
        const body = typeof ev.__result.content === "string"
          ? ev.__result.content
          : JSON.stringify(ev.__result.content || "");
        lines.push(ev.__result.is_error ? "*(error)*" : "");
        lines.push("```");
        lines.push(body.slice(0, 4000));
        lines.push("```");
      }
    } else if (ev.type === "tool_result") {
      const body = typeof ev.content === "string" ? ev.content : JSON.stringify(ev.content || "");
      lines.push("", "```");
      lines.push(body.slice(0, 4000));
      lines.push("```");
    } else if (ev.type === "result") {
      lines.push("", `_duration ${ev.duration_ms || "?"} ms · cost $${(ev.cost_usd || 0).toFixed(4)}${ev.session_id ? " · session " + ev.session_id : ""}_`);
    } else if (ev.type === "error") {
      lines.push("", "```");
      lines.push(ev.error || "");
      lines.push("```");
    } else {
      const { ts: _t, agent_id: _a, type: _ty, __id: _i, __result: _r, ...rest } = ev;
      lines.push("", "```json");
      lines.push(JSON.stringify(rest, null, 2));
      lines.push("```");
    }
    lines.push("");
  }
  return lines.join("\n");
}

function downloadMarkdown(filename, content) {
  const blob = new Blob([content], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

// Split.js size persistence. Sizes are keyed by layout structure so
// user-dragged widths survive no-op re-renders but reset sensibly
// when the layout changes structurally (add / remove / move a pane).
const SPLIT_SIZES_KEY = "harness_split_sizes_v1";
function loadSplitSizes() {
  try {
    const raw = localStorage.getItem(SPLIT_SIZES_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch (_) {
    return {};
  }
}
function saveSplitSizes(map) {
  try {
    // Cap retained keys at 30 to prevent unbounded growth across many
    // distinct layouts. Simplest: FIFO by insertion order on keys().
    const keys = Object.keys(map);
    if (keys.length > 30) {
      const out = {};
      for (const k of keys.slice(-30)) out[k] = map[k];
      localStorage.setItem(SPLIT_SIZES_KEY, JSON.stringify(out));
      return;
    }
    localStorage.setItem(SPLIT_SIZES_KEY, JSON.stringify(map));
  } catch (_) {
    /* localStorage disabled */
  }
}

// ------------------------------------------------------------------
// per-pane settings (model / plan mode / effort)
// ------------------------------------------------------------------

const PROMPT_HISTORY_KEY = "harness_prompt_history_v1";
const PROMPT_HISTORY_MAX_PER_SLOT = 40;
const EMPTY_EVENTS = [];

function loadPromptHistory(slot) {
  try {
    const raw = localStorage.getItem(PROMPT_HISTORY_KEY);
    if (!raw) return [];
    const all = JSON.parse(raw);
    const list = all && typeof all === "object" ? all[slot] : null;
    return Array.isArray(list) ? list : [];
  } catch (_) {
    return [];
  }
}

function pushPromptHistory(slot, text) {
  try {
    const raw = localStorage.getItem(PROMPT_HISTORY_KEY);
    const all = raw ? JSON.parse(raw) : {};
    const list = Array.isArray(all[slot]) ? all[slot] : [];
    // Don't store consecutive duplicates.
    if (list.length > 0 && list[list.length - 1] === text) return list;
    const next = [...list, text].slice(-PROMPT_HISTORY_MAX_PER_SLOT);
    all[slot] = next;
    localStorage.setItem(PROMPT_HISTORY_KEY, JSON.stringify(all));
    return next;
  } catch (_) {
    return [];
  }
}

const PANE_SETTINGS_KEY = "harness_pane_settings_v1";

// Default: let the server pick (will use claude-sonnet-4-6 or whatever
// is set in Dockerfile env). Users can override per-pane.
const MODEL_OPTIONS = [
  { value: "", label: "default" },
  { value: "claude-opus-4-7", label: "Opus 4.7" },
  { value: "claude-sonnet-4-6", label: "Sonnet 4.6" },
  { value: "claude-haiku-4-5-20251001", label: "Haiku 4.5" },
];

// Codex (OpenAI) model options for slots running on the codex
// runtime. Pricing for these lives in `server/pricing.py`.
const CODEX_MODEL_OPTIONS = [
  { value: "", label: "default" },
  { value: "gpt-5.5", label: "GPT-5.5" },
  { value: "gpt-5.4", label: "GPT-5.4" },
  { value: "gpt-5.4-mini", label: "GPT-5.4 mini" },
  { value: "gpt-5.4-nano", label: "GPT-5.4 nano" },
  { value: "gpt-5.3-codex", label: "GPT-5.3 Codex" },
  { value: "gpt-5.2-codex", label: "GPT-5.2 Codex" },
  { value: "gpt-5.1-codex-max", label: "GPT-5.1 Codex Max" },
  { value: "gpt-5.1-codex", label: "GPT-5.1 Codex" },
  { value: "gpt-5.1-codex-mini", label: "GPT-5.1 Codex mini" },
  { value: "gpt-5-codex", label: "GPT-5 Codex" },
];

// Pick the right model dropdown by runtime name.
function modelOptionsFor(runtime) {
  return runtime === "codex" ? CODEX_MODEL_OPTIONS : MODEL_OPTIONS;
}

function modelLabelFor(id, runtime) {
  const opts = modelOptionsFor(runtime || "");
  const match =
    opts.find((m) => m.value === id) ||
    CODEX_MODEL_OPTIONS.find((m) => m.value === id) ||
    MODEL_OPTIONS.find((m) => m.value === id);
  return match ? match.label : (id || "default");
}

// Effort: 1=low, 2=med, 3=high, 4=max. Mapped server-side to a
// thinking budget in tokens (see server/agents.py once wired).
const EFFORT_LABELS = ["low", "med", "high", "max"];

// Harness slash commands — intercepted locally, never forwarded to the
// agent. Actions fire via a small context object so each pane can wire
// its own handlers (settings popover, session clear, etc.). To add one:
// append below; the autocomplete picks up everything here.
const SLASH_COMMANDS = [
  { cmd: "/plan",   desc: "toggle plan mode for the next turn" },
  { cmd: "/model",  desc: "open the model picker" },
  { cmd: "/effort", desc: "open the effort slider (low…max)" },
  { cmd: "/brief",  desc: "edit this agent's brief" },
  { cmd: "/tools",  desc: "list the tools this agent can use" },
  { cmd: "/clear",   desc: "clear session so the next turn starts fresh" },
  { cmd: "/compact", desc: "summarize current session; next turn resumes with summary" },
  { cmd: "/cancel",  desc: "cancel the in-flight turn on this pane" },
  { cmd: "/tick",   desc: "/tick → fire now · /tick N → every N min · /tick off" },
  { cmd: "/repeat", desc: "Coach repeat: /repeat → list · /repeat N <prompt> · /repeat rm <id>" },
  { cmd: "/cron",   desc: "Coach cron: /cron → list · /cron <when> <prompt> · /cron rm <id>" },
  { cmd: "/status", desc: "show server runtime state (paused, running, spend)" },
  { cmd: "/spend",  desc: "per-agent spend over last 24h" },
  { cmd: "/help",   desc: "show available slash commands" },
];

function SlashMenu({ query, selectedIdx, onPick, onHover }) {
  const filtered = SLASH_COMMANDS.filter((c) =>
    c.cmd.startsWith(query.toLowerCase())
  );
  if (filtered.length === 0) return null;
  return html`
    <div class="slash-menu">
      ${filtered.map((c, i) => html`
        <div
          class=${"slash-item" + (i === selectedIdx ? " selected" : "")}
          key=${c.cmd}
          onMouseEnter=${() => onHover(i)}
          onMouseDown=${(e) => { e.preventDefault(); onPick(c.cmd); }}
        >
          <span class="slash-cmd">${c.cmd}</span>
          <span class="slash-desc">${c.desc}</span>
        </div>
      `)}
    </div>
  `;
}

function loadPaneSettings(slot) {
  try {
    const raw = localStorage.getItem(PANE_SETTINGS_KEY);
    if (!raw) return {};
    const all = JSON.parse(raw);
    return (all && typeof all === "object" && all[slot]) || {};
  } catch (_) {
    return {};
  }
}

function hasSettingOverride(s) {
  return !!(s && (s.model || s.planMode || s.effort));
}

function savePaneSettings(slot, settings) {
  try {
    const raw = localStorage.getItem(PANE_SETTINGS_KEY);
    const all = raw ? JSON.parse(raw) : {};
    if (!settings || Object.keys(settings).length === 0) delete all[slot];
    else all[slot] = settings;
    localStorage.setItem(PANE_SETTINGS_KEY, JSON.stringify(all));
  } catch (_) {
    // localStorage disabled — silent no-op.
  }
}

function PaneSettingsPopover({ settings, onChange, onClose, slot, initialBrief, initialName, initialRole, initialRuntime }) {
  const effort = settings.effort || 0; // 0 = default (server decides)
  const rootRef = useRef(null);
  const [briefDraft, setBriefDraft] = useState(initialBrief || "");
  const [briefSaving, setBriefSaving] = useState(false);
  const [briefSavedAt, setBriefSavedAt] = useState(null);
  const briefDirty = briefDraft !== (initialBrief || "");
  const [nameDraft, setNameDraft] = useState(initialName || "");
  const [roleDraft, setRoleDraft] = useState(initialRole || "");
  const [identitySaving, setIdentitySaving] = useState(false);
  const identityDirty =
    nameDraft !== (initialName || "") || roleDraft !== (initialRole || "");
  // PR 6: per-slot runtime override. UI saves immediately on change
  // via PUT /api/agents/{slot}/runtime. Empty string = clear (fall
  // through to role default → 'claude'). Mid-turn changes 409 on the
  // server; we surface that via runtimeError below.
  const [runtimeDraft, setRuntimeDraft] = useState(initialRuntime || "");
  const [runtimeSaving, setRuntimeSaving] = useState(false);
  const [runtimeError, setRuntimeError] = useState("");
  const [roleDefaultRuntime, setRoleDefaultRuntime] = useState(null);
  const saveRuntime = useCallback(async (next) => {
    if (!slot) return;
    setRuntimeSaving(true);
    setRuntimeError("");
    try {
      const res = await authFetch("/api/agents/" + slot + "/runtime", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ runtime: next }),
      });
      if (!res.ok) {
        let detail = "save failed";
        try { detail = (await res.json()).detail || detail; } catch {}
        setRuntimeError(detail);
        // Bounce back to the previous value so the radio reflects truth.
        setRuntimeDraft(initialRuntime || "");
        return;
      }
      setRuntimeDraft(next);
    } catch (e) {
      setRuntimeError(String(e));
      setRuntimeDraft(initialRuntime || "");
    } finally {
      setRuntimeSaving(false);
    }
  }, [slot, initialRuntime]);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await authFetch("/api/team/runtimes");
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (cancelled) return;
        const id = slot === "coach" ? data.coach : data.players;
        setRoleDefaultRuntime(id || "");
      } catch (_e) {
        // Silent — the popover still works without the resolved label.
      }
    })();
    return () => { cancelled = true; };
  }, [slot]);
  // Resolve the team-wide default model so the "default" option in the
  // dropdown can show what it'll actually fall through to (e.g.
  // "default (Sonnet 4.6)"). Avoids the trap where users set a team
  // default in the Settings drawer and then see "default" in the gear
  // popover and assume the agent is running the SDK default.
  const [roleDefaultModels, setRoleDefaultModels] = useState(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await authFetch("/api/team/models");
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (cancelled) return;
        setRoleDefaultModels(data);
      } catch (_e) {
        // Silent — the popover still works without the resolved label.
      }
    })();
    return () => { cancelled = true; };
  }, [slot]);
  // PR 6 / audit-item-18: model dropdown options depend on the
  // currently-selected runtime. When the slot is on Codex, list
  // CODEX_MODEL_OPTIONS; on Claude, list MODEL_OPTIONS. Falls back to
  // Claude when the runtime isn't yet known (initial render). The
  // suffix that decorates the "default" entry uses the role-default
  // model resolved server-side.
  const modelOptions = useMemo(() => {
    const effectiveRuntime = runtimeDraft || roleDefaultRuntime || "claude";
    const base = modelOptionsFor(effectiveRuntime);
    const role = slot === "coach" ? "coach" : "players";
    const key = effectiveRuntime === "codex" ? role + "_codex" : role;
    const roleDefaultModel = roleDefaultModels ? (roleDefaultModels[key] || "") : "";
    if (!roleDefaultModel) return base;
    const match = base.find((m) => m.value === roleDefaultModel);
    const suffix = match ? match.label : roleDefaultModel;
    return base.map((m) =>
      m.value === "" ? { ...m, label: `default (${suffix})` } : m
    );
  }, [roleDefaultModels, roleDefaultRuntime, runtimeDraft, slot]);
  useEffect(() => {
    if (!runtimeDraft && roleDefaultRuntime === null) return;
    const current = settings.model || "";
    if (!current) return;
    if (modelOptions.some((m) => m.value === current)) return;
    onChange((s) => ({ ...s, model: "" }));
  }, [modelOptions, onChange, roleDefaultRuntime, runtimeDraft, settings.model]);
  const saveIdentity = useCallback(async () => {
    if (!slot) return;
    setIdentitySaving(true);
    try {
      await authFetch("/api/agents/" + slot + "/identity", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: nameDraft, role: roleDraft }),
      });
    } catch (e) {
      console.error("identity save failed", e);
    } finally {
      setIdentitySaving(false);
    }
  }, [slot, nameDraft, roleDraft]);
  const saveBrief = useCallback(async () => {
    if (!slot) return;
    setBriefSaving(true);
    try {
      const res = await authFetch("/api/agents/" + slot + "/brief", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ brief: briefDraft }),
      });
      if (res.ok) setBriefSavedAt(Date.now());
    } catch (e) {
      console.error("brief save failed", e);
    } finally {
      setBriefSaving(false);
    }
  }, [slot, briefDraft]);
  useEffect(() => {
    const onDocClick = (e) => {
      if (!rootRef.current) return;
      // Click inside the popover — leave it open.
      if (rootRef.current.contains(e.target)) return;
      // Click on the triggering gear button — let the gear's own
      // toggle handler decide. Without this exclusion, mousedown fires
      // close() then the gear's click toggles back to open, so the
      // popover never closes via the gear (visible flicker).
      if (e.target.closest && e.target.closest(".pane-gear")) return;
      onClose();
    };
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);
  return html`
    <div class="pane-settings-pop" ref=${rootRef} onClick=${(e) => e.stopPropagation()}>
      <!-- Identity: name + role. Distinct from the brief below — these
           are short display labels shown in the pane header; the brief
           is the long context string that lands in the system prompt. -->
      <div class="pane-settings-row">
        <label class="pane-settings-label">Name</label>
        <input
          class="pane-settings-input"
          placeholder=${slot === "coach" ? "Coach" : "e.g. Rabil"}
          value=${nameDraft}
          onInput=${(e) => setNameDraft(e.target.value)}
          maxlength="60"
        />
      </div>
      <div class="pane-settings-row">
        <label class="pane-settings-label">Role</label>
        <input
          class="pane-settings-input"
          placeholder=${slot === "coach" ? "Team captain" : "e.g. Frontend dev"}
          value=${roleDraft}
          onInput=${(e) => setRoleDraft(e.target.value)}
          maxlength="120"
        />
      </div>
      ${identityDirty
        ? html`<div class="pane-settings-identity-actions">
            <button
              class="pane-settings-brief-save"
              onClick=${saveIdentity}
              disabled=${identitySaving}
            >${identitySaving ? "saving…" : "save name + role"}</button>
          </div>`
        : null}
      <div class="pane-settings-row">
        <label class="pane-settings-label">Model</label>
        <select
          class="pane-settings-select"
          value=${settings.model || ""}
          onChange=${(e) => onChange({ ...settings, model: e.target.value || undefined })}
        >
          ${modelOptions.map(
            (m) => html`<option value=${m.value}>${m.label}</option>`
          )}
        </select>
      </div>
      <div class="pane-settings-row">
        <label class="pane-settings-label">Runtime</label>
        <div class="pane-settings-runtime">
          <label>
            <input
              type="radio"
              name=${"pane-runtime-" + (slot || "x")}
              value=""
              checked=${runtimeDraft === ""}
              disabled=${runtimeSaving}
              onChange=${() => saveRuntime("")}
            />
            default
          </label>
          <label>
            <input
              type="radio"
              name=${"pane-runtime-" + (slot || "x")}
              value="claude"
              checked=${runtimeDraft === "claude"}
              disabled=${runtimeSaving}
              onChange=${() => saveRuntime("claude")}
            />
            Claude
          </label>
          <label>
            <input
              type="radio"
              name=${"pane-runtime-" + (slot || "x")}
              value="codex"
              checked=${runtimeDraft === "codex"}
              disabled=${runtimeSaving}
              onChange=${() => saveRuntime("codex")}
            />
            Codex
          </label>
        </div>
        ${runtimeError
          ? html`<span class="pane-settings-runtime-err">${runtimeError}</span>`
          : html`<span class="pane-settings-hint">Saves immediately. Mid-turn change is rejected — cancel the turn first.</span>`}
      </div>
      <div class="pane-settings-row">
        <label class="pane-settings-label">
          <input
            type="checkbox"
            checked=${!!settings.planMode}
            onChange=${(e) => onChange({ ...settings, planMode: e.target.checked || undefined })}
          />
          Plan mode
        </label>
        <span class="pane-settings-hint">Agent outlines an approach before touching code.</span>
      </div>
      <div class="pane-settings-row">
        <label class="pane-settings-label">Effort</label>
        <input
          type="range"
          min="0"
          max="4"
          value=${effort}
          class="pane-settings-slider"
          onInput=${(e) => {
            const v = parseInt(e.target.value, 10);
            onChange({ ...settings, effort: v > 0 ? v : undefined });
          }}
        />
        <span class="pane-settings-effort-val">
          ${effort === 0 ? "default" : EFFORT_LABELS[effort - 1]}
        </span>
      </div>
      <div class="pane-settings-row pane-settings-brief">
        <label class="pane-settings-label">Brief</label>
        <textarea
          class="pane-settings-textarea"
          placeholder=${slot === "coach"
            ? "Team direction, goals for this project, voice Coach should use…"
            : "Domain for this Player, conventions, tools they should reach for first…"}
          value=${briefDraft}
          onInput=${(e) => setBriefDraft(e.target.value)}
          rows=${5}
        />
        <div class="pane-settings-brief-foot">
          <span class="pane-settings-hint">
            Appended to every turn's system prompt. Takes effect immediately.
          </span>
          <button
            class="pane-settings-brief-save"
            onClick=${saveBrief}
            disabled=${briefSaving || !briefDirty}
            title=${briefDirty ? "Save brief" : "no changes"}
          >${briefSaving ? "saving…" : briefDirty ? "save" : briefSavedAt ? "saved" : "saved"}</button>
        </div>
      </div>
      <div class="pane-settings-actions">
        <button class="pane-settings-reset" onClick=${() => onChange({})}>
          reset
        </button>
        <button class="pane-settings-close" onClick=${onClose}>done</button>
      </div>
    </div>
  `;
}

function saveLayout(layout) {
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
  } catch (_) {
    // localStorage may be disabled (private mode); silent no-op.
  }
}

// ------------------------------------------------------------------
// helpers
// ------------------------------------------------------------------

function slotShortLabel(slotId) {
  if (slotId === "coach") return "C";
  if (slotId.startsWith("p")) return slotId.slice(1);
  return slotId.slice(0, 2);
}

// Timezone preference for timestamp rendering. Server stamps events
// in UTC; reading the raw ISO is fine when many users collaborate, but
// for a solo deploy the wall clock you actually live in is more
// useful. Toggle lives in the Options drawer ("Display") section,
// persisted in localStorage. Default 'local'.
function getTzPref() {
  try { return localStorage.getItem("harness_tz_pref") || "local"; }
  catch (_) { return "local"; }
}

function timeStr(iso) {
  if (!iso) return "";
  if (getTzPref() === "utc") {
    return iso.slice(11, 19);
  }
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return iso.slice(11, 19);
  return d.toLocaleTimeString([], {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}


// "3m ago", "2h ago", "just now" — coarse human-friendly relative time.
// Returns "" for missing input so tooltip composition can skip cleanly.
function relTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return "";
  const diffSec = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (diffSec < 10) return "just now";
  if (diffSec < 60) return diffSec + "s ago";
  if (diffSec < 3600) return Math.round(diffSec / 60) + "m ago";
  if (diffSec < 86400) return Math.round(diffSec / 3600) + "h ago";
  return Math.round(diffSec / 86400) + "d ago";
}

// Compose the pane-dot tooltip from status + timestamps.
function statusTooltip(status, agent) {
  const parts = [status];
  const hb = relTime(agent?.last_heartbeat);
  if (hb) parts.push("last heartbeat: " + hb);
  const started = relTime(agent?.started_at);
  if (started) parts.push("first started: " + started);
  return parts.join(" · ");
}

function byNumericSuffix(a, b) {
  const na = parseInt(a.id.slice(1), 10) || 0;
  const nb = parseInt(b.id.slice(1), 10) || 0;
  return na - nb;
}

// Convert a persisted event row (from /api/events) into the same shape
// that live WS events have. Persisted rows wrap the original event in a
// {id, ts, agent_id, type, payload} envelope; the original fields we
// care about live inside payload.
function unwrapPersisted(row) {
  const payload = row.payload || {};
  return {
    __id: row.id,
    ts: row.ts || payload.ts,
    agent_id: row.agent_id || payload.agent_id,
    type: row.type || payload.type,
    ...payload,
  };
}

// ------------------------------------------------------------------
// root app
// ------------------------------------------------------------------

function App() {
  const [agents, setAgents] = useState([]);
  const [tasks, setTasks] = useState([]);
  // Lazy initializers: loadLayout() runs once per mount, not on every render.
  const [openColumns, setOpenColumns] = useState(
    () => loadLayout()?.openColumns ?? [["coach"]]
  );
  const [wsConnected, setWsConnected] = useState(false);
  const [envOpen, setEnvOpen] = useState(
    () => loadLayout()?.envOpen ?? true
  );
  const [envWidth, setEnvWidth] = useState(
    () => loadLayout()?.envWidth ?? 340
  );
  // Recurrence pane (recurrence-specs.md §12). Lives alongside EnvPane;
  // both can be open simultaneously. Persisted under its own localStorage
  // key so the existing layout key isn't bloated for users who never
  // touch recurrences.
  const [recurrenceOpen, setRecurrenceOpen] = useState(() => {
    try {
      return localStorage.getItem("harness_recurrence_pane_v1") === "1";
    } catch (e) { return false; }
  });
  const [recurrenceRows, setRecurrenceRows] = useState([]);
  const [recurrenceError, setRecurrenceError] = useState(null);
  // Roots metadata for the file-link resolver. Loaded once on mount;
  // FilesPane reads this via prop instead of self-fetching, so a click
  // on a `[data-harness-path]` link can resolve and open the file
  // even before the Files pane has mounted for the first time. Each
  // entry: { key, path, label, writable, exists }.
  const [fileRoots, setFileRoots] = useState([]);
  // Set when the user clicks an in-app file link. FilesPane reads this
  // and (once roots are loaded) selects the right root + opens the
  // file. Cleared by FilesPane via clearPendingFileOpen.
  const [pendingFileOpen, setPendingFileOpen] = useState(null);

  // Single-pane focus mode: when set to a slot id, only that pane
  // renders (filling the panes area). Clicking another rail slot, the
  // current pane's restore button, or closing the pane → clears it.
  const [maximizedSlot, setMaximizedSlot] = useState(
    () => loadLayout()?.maximizedSlot ?? null
  );
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [serverStatus, setServerStatus] = useState(null);
  const [paused, setPaused] = useState(false);
  const [authChallenge, setAuthChallenge] = useState(false);
  // conversations: Map<slotId, Event[]>  (events ordered oldest → newest)
  const [conversations, setConversations] = useState(new Map());
  // streamingText: Map<slotId, {text, thinking}> — partial-token deltas
  // accumulated between content_block_start and the consolidated
  // AssistantMessage that follows. Surfaces as a live-typing bubble at
  // the end of each pane timeline. Not persisted (ephemeral by design).
  const [streamingText, setStreamingText] = useState(new Map());
  // Bumped every time a file-system-changing event lands on the WS.
  // FilesPane watches this to reload the tree without manual refresh.
  const [fsEpoch, setFsEpoch] = useState(0);
  // Bumped on every successful project switch. Each AgentPane uses this
  // as a dependency in its history useEffect so the pane re-fetches
  // /api/events scoped to the new active project — without it, the
  // pane keeps rendering the old project's saved conversation
  // (App-level conversations Map clears + re-seeds, but each pane
  // also caches its own `history` from a separate fetch).
  const [projectEpoch, setProjectEpoch] = useState(0);
  // Per-slot latest ts the user has "acknowledged" by opening that
  // pane. Slots currently in openColumns are always considered seen.
  // Lives in React state only (session-scoped) — a page reload
  // legitimately re-shows the "new activity since last visit" badge.
  const [seenTs, setSeenTs] = useState({});
  // bumping this re-runs the WS effect, which re-opens a new connection
  const [wsAttempt, setWsAttempt] = useState(0);

  // Phase 3 (PROJECTS_SPEC.md §13): the project list + active id are
  // hydrated from /api/projects on mount and re-fetched whenever a
  // `project_switched` / `project_created` / `project_deleted` event
  // arrives over the WS.
  const [projects, setProjects] = useState([]);
  const [activeProjectId, setActiveProjectId] = useState(null);
  const [switchingProject, setSwitchingProject] = useState(null);
  // Phase 4 — switch UX polish state.
  // confirm: pre-flight modal showing counts + Cancel/Switch.
  //   { to: 'slug', preview: {...}, error: null } | null
  // inFlight: 423 sub-modal { to, agent_id }
  // busy: stepper modal { to, fromName, toName, jobId, steps[], terminal? }
  const [switchConfirm, setSwitchConfirm] = useState(null);
  const [switchInFlight, setSwitchInFlight] = useState(null);
  const [switchBusy, setSwitchBusy] = useState(null);
  // Phase 4 audit fix #1: race-safe step delivery. The activate
  // handler returns 202 + job_id, but the server's task may emit
  // `project_switch_step started` BEFORE the response reaches the
  // client. The WS dispatcher buffers all step events here keyed by
  // job_id; whenever switchBusy.jobId is assigned, the busy modal
  // drains the buffer and merges any pre-jobId steps. Buffer entries
  // are cleared on terminal `project_switched` for that job_id.
  const switchStepBuffer = useRef(new Map());

  // Wrap authFetch so a 401 anywhere flips the global gate.
  const authedFetch = useCallback(async (url, init) => {
    const res = await authFetch(url, init);
    if (res.status === 401 || res.status === 403) {
      setAuthChallenge(true);
    }
    return res;
  }, []);

  // load + refresh agents
  const loadAgents = useCallback(async () => {
    try {
      const res = await authedFetch("/api/agents");
      if (!res.ok) return;
      const data = await res.json();
      setAgents(data.agents || []);
    } catch (e) {
      console.error("loadAgents failed", e);
    }
  }, [authedFetch]);

  const loadTasks = useCallback(async () => {
    try {
      const res = await authedFetch("/api/tasks");
      if (!res.ok) return;
      const data = await res.json();
      setTasks(data.tasks || []);
    } catch (e) {
      console.error("loadTasks failed", e);
    }
  }, [authedFetch]);

  // Phase 3: project list + active id. Refreshed via refreshProjects()
  // on mount + on every project_* WS event.
  const loadProjects = useCallback(async () => {
    try {
      const res = await authedFetch("/api/projects");
      if (!res.ok) return;
      const data = await res.json();
      setProjects(data.projects || []);
      setActiveProjectId(data.active || null);
    } catch (e) {
      console.error("loadProjects failed", e);
    }
  }, [authedFetch]);
  const refreshProjects = useCallback(
    () => loadProjects(),
    [loadProjects]
  );

  const loadFileRoots = useCallback(async () => {
    try {
      const res = await authedFetch("/api/files/roots");
      if (!res.ok) return;
      const data = await res.json();
      setFileRoots(Array.isArray(data) ? data : []);
    } catch (e) {
      console.error("loadFileRoots failed", e);
    }
  }, [authedFetch]);

  // Seed `conversations` with a recent slice of events for every slot
  // at app mount so the LeftRail dot computation has data to chew on
  // even when no panes have been opened yet. Without this, dots for
  // closed panes default to green until the user opens the pane (which
  // triggers AgentPane's per-slot history loader). 11 small parallel
  // fetches; deduped by __id when merged with any live events that
  // landed during the round-trip.
  const seedConversationsFromHistory = useCallback(async () => {
    const slots = ["coach", ...Array.from({ length: 10 }, (_, i) => "p" + (i + 1))];
    const SEED_LIMIT = 50;
    const results = await Promise.all(slots.map(async (slot) => {
      try {
        const res = await authedFetch(
          `/api/events?agent=${encodeURIComponent(slot)}&limit=${SEED_LIMIT}`
        );
        if (!res.ok) return [slot, []];
        const data = await res.json();
        return [slot, (data.events || []).map(unwrapPersisted)];
      } catch (_) {
        return [slot, []];
      }
    }));
    setConversations((prev) => {
      const next = new Map(prev);
      for (const [slot, events] of results) {
        const existing = next.get(slot) || [];
        const seen = new Set(
          existing.map((e) => e.__id).filter((id) => id != null)
        );
        const fresh = events.filter((e) => e.__id == null || !seen.has(e.__id));
        if (fresh.length === 0) continue;
        const merged = [...fresh, ...existing];
        merged.sort((a, b) => (a.ts || "").localeCompare(b.ts || ""));
        next.set(slot, merged);
      }
      return next;
    });
  }, [authedFetch]);

  const createHumanTask = useCallback(
    async ({ title, description, priority }) => {
      const res = await authedFetch("/api/tasks", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          title,
          description: description || "",
          priority: priority || "normal",
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await loadTasks();
    },
    [loadTasks, authedFetch]
  );

  const loadStatus = useCallback(async () => {
    try {
      const res = await authedFetch("/api/status");
      if (!res.ok) return;
      const data = await res.json();
      setServerStatus(data);
    } catch (e) {
      console.error("loadStatus failed", e);
    }
  }, [authedFetch]);

  // Debounced loaders for use in the WS event handler. A burst of
  // related events (e.g. task_created → task_assigned → task_claimed
  // arriving in <100 ms) was firing one HTTP request per loader per
  // event — three fast events could mean ~6 redundant /api/agents +
  // /api/tasks + /api/status round-trips. We coalesce to a single
  // trailing-edge call per ~150 ms window. Direct (non-debounced)
  // loaders are still used for mount, reconnect, and explicit awaits.
  const refreshTimersRef = useRef({ agents: null, tasks: null, status: null });
  const debouncedRefresh = useCallback((key, fn, delay = 150) => {
    const timers = refreshTimersRef.current;
    if (timers[key]) clearTimeout(timers[key]);
    timers[key] = setTimeout(() => {
      timers[key] = null;
      fn();
    }, delay);
  }, []);
  const refreshAgents = useCallback(
    () => debouncedRefresh("agents", loadAgents),
    [debouncedRefresh, loadAgents]
  );
  const refreshTasks = useCallback(
    () => debouncedRefresh("tasks", loadTasks),
    [debouncedRefresh, loadTasks]
  );
  const refreshStatus = useCallback(
    () => debouncedRefresh("status", loadStatus),
    [debouncedRefresh, loadStatus]
  );
  // Clear any pending refresh timers on unmount so we don't fire after
  // the component is gone (matters for HMR / dev reloads).
  useEffect(() => {
    return () => {
      const timers = refreshTimersRef.current;
      for (const k of Object.keys(timers)) {
        if (timers[k]) clearTimeout(timers[k]);
        timers[k] = null;
      }
    };
  }, []);

  // Streaming-delta batching. text_delta / thinking_delta land at
  // 10–50 events/sec per active agent. With several agents streaming
  // at once, calling setStreamingText synchronously on every delta
  // re-renders the App tree faster than the display can paint and
  // shows up as input lag. We accumulate the deltas in a ref and
  // flush them once per animation frame, so React work is capped at
  // ~60 Hz regardless of network rate. Tabbed-out browsers throttle
  // rAF down further automatically — that's fine; deltas batch
  // larger and flush when the tab becomes visible again.
  const streamingPendingRef = useRef(new Map());
  const streamingRafRef = useRef(0);
  const flushStreamingPending = useCallback(() => {
    streamingRafRef.current = 0;
    const pending = streamingPendingRef.current;
    if (pending.size === 0) return;
    const snapshot = pending;
    streamingPendingRef.current = new Map();
    setStreamingText((prev) => {
      const next = new Map(prev);
      for (const [aid, delta] of snapshot) {
        const cur = next.get(aid) || { text: "", thinking: "" };
        next.set(aid, {
          text: cur.text + (delta.text || ""),
          thinking: cur.thinking + (delta.thinking || ""),
        });
      }
      return next;
    });
  }, []);
  const enqueueStreamingDelta = useCallback((aid, key, delta) => {
    if (!delta) return;
    const pending = streamingPendingRef.current;
    const cur = pending.get(aid) || { text: "", thinking: "" };
    pending.set(aid, { ...cur, [key]: cur[key] + delta });
    if (!streamingRafRef.current) {
      streamingRafRef.current = requestAnimationFrame(flushStreamingPending);
    }
  }, [flushStreamingPending]);
  // Cancel any pending rAF on unmount.
  useEffect(() => {
    return () => {
      if (streamingRafRef.current) {
        cancelAnimationFrame(streamingRafRef.current);
        streamingRafRef.current = 0;
      }
      streamingPendingRef.current = new Map();
    };
  }, []);

  const loadPause = useCallback(async () => {
    try {
      const res = await authedFetch("/api/pause");
      if (!res.ok) return;
      const data = await res.json();
      setPaused(Boolean(data.paused));
    } catch (e) {
      console.error("loadPause failed", e);
    }
  }, [authedFetch]);

  const togglePause = useCallback(async () => {
    try {
      const res = await authedFetch("/api/pause", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ paused: !paused }),
      });
      if (!res.ok) return;
      const data = await res.json();
      setPaused(Boolean(data.paused));
    } catch (e) {
      console.error("togglePause failed", e);
    }
  }, [authedFetch, paused]);

  useEffect(() => {
    loadAgents();
    loadTasks();
    loadStatus();
    loadPause();
    loadFileRoots();
    loadProjects();
    seedConversationsFromHistory();
    const statusTimer = setInterval(loadStatus, 30_000);
    return () => clearInterval(statusTimer);
  }, [loadAgents, loadTasks, loadStatus, loadPause, loadFileRoots, loadProjects, seedConversationsFromHistory]);

  // Click-handler for in-app file links. Marked + DOMPurify tag any
  // markdown link whose href is an absolute path (`/data/...`,
  // `/workspaces/...`, etc.) with `data-harness-path`. We intercept
  // those clicks here, open the Files pane if needed, and remember
  // the path so FilesPane can resolve + select it.
  useEffect(() => {
    const onClick = (e) => {
      const link = e.target?.closest?.("[data-harness-path]");
      if (!link) return;
      const path = link.getAttribute("data-harness-path");
      if (!path) return;
      e.preventDefault();
      setOpenColumns((prev) => {
        if (flatSlots(prev).includes("__files")) return prev;
        return [...prev, ["__files"]];
      });
      setMaximizedSlot((cur) => (cur && cur !== "__files" ? null : cur));
      setPendingFileOpen({ path, ts: Date.now() });
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, []);

  const clearPendingFileOpen = useCallback(() => {
    setPendingFileOpen(null);
  }, []);

  // Persist layout (open slots + env panel state + maximize) on every change.
  useEffect(() => {
    saveLayout({ openColumns, envOpen, envWidth, maximizedSlot });
  }, [openColumns, envOpen, envWidth, maximizedSlot]);

  // Persist recurrence pane open/closed independently of harness_layout_v1
  // (recurrence-specs.md §12.2).
  useEffect(() => {
    try {
      localStorage.setItem(
        "harness_recurrence_pane_v1", recurrenceOpen ? "1" : "0",
      );
    } catch (e) { /* localStorage unavailable */ }
  }, [recurrenceOpen]);

  // Fetch the recurrence-row list. Called on pane-open, on every
  // recurrence_* WS event, and when the active project changes.
  const refreshRecurrences = useCallback(async () => {
    try {
      const res = await authFetch("/api/recurrences");
      if (!res.ok) {
        if (res.status === 401) return; // auth modal handles it
        throw new Error("HTTP " + res.status);
      }
      const data = await res.json();
      setRecurrenceRows(Array.isArray(data) ? data : []);
      setRecurrenceError(null);
    } catch (e) {
      setRecurrenceError(String(e.message || e));
    }
  }, []);

  // Initial load + reload on pane-open + reload on project switch.
  useEffect(() => {
    if (recurrenceOpen) refreshRecurrences();
  }, [recurrenceOpen, activeProjectId, refreshRecurrences]);

  // Keep tool-renderer name directory (slot → human name) in sync
  // so coord_send_message etc. can print "→ Gait" instead of "→ p3".
  useEffect(() => {
    setAgentDirectory(agents);
  }, [agents]);

  // Global keyboard shortcuts. Kept deliberately small — anything else
  // belongs scoped to the relevant pane/component.
  //   Ctrl/Cmd + B : toggle the Environment side-panel.
  //   Ctrl/Cmd + . : toggle pause (block new agent runs). Comma/period
  //                  keys avoid the browser-native ⌘+P (print dialog).
  // We ignore the shortcut when the user is typing in a form field so
  // browser-native Ctrl+B (bold) still works inside textareas that opt
  // in to it, and so it never steals focus mid-sentence.
  useEffect(() => {
    const onKeyDown = (e) => {
      const mod = e.ctrlKey || e.metaKey;
      if (!mod) return;
      const tag = (e.target?.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || e.target?.isContentEditable) return;
      if (e.key === "b" || e.key === "B") {
        e.preventDefault();
        setEnvOpen((v) => !v);
      }
      if (e.key === "." || e.key === ">") {
        e.preventDefault();
        togglePause();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [togglePause]);

  // WebSocket: single connection at app root. On close, schedule a
  // re-open by bumping wsAttempt; the effect re-runs, a new socket opens.
  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const tok = getToken();
    const wsUrl =
      `${proto}//${location.host}/ws` +
      (tok ? "?token=" + encodeURIComponent(tok) : "");
    const ws = new WebSocket(wsUrl);
    let reopenTimer = null;
    // Zombie-connection watchdog: server sends a 'ping' every 30s of
    // quiet, so 60s without any message means the connection is
    // effectively dead. Force-close triggers onclose → reconnect.
    let lastMessageAt = Date.now();
    const watchdog = setInterval(() => {
      if (Date.now() - lastMessageAt > 60_000) {
        try { ws.close(); } catch (_) { /* ignore */ }
      }
    }, 15_000);
    ws.onopen = () => {
      lastMessageAt = Date.now();
      setWsConnected(true);
      // On (re)connect we may have missed events while offline; refresh
      // the stateful bits so the UI catches up.
      loadAgents();
      loadTasks();
      loadStatus();
    };
    ws.onclose = () => {
      setWsConnected(false);
      reopenTimer = setTimeout(
        () => setWsAttempt((a) => a + 1),
        Math.min(30_000, 2000 * Math.max(1, wsAttempt + 1))
      );
    };
    ws.onerror = () => {
      // Let onclose handle the retry; just close so onclose fires.
      try { ws.close(); } catch (_) { /* ignore */ }
    };
    ws.onmessage = (e) => {
      lastMessageAt = Date.now();
      let ev;
      try { ev = JSON.parse(e.data); } catch (_) { return; }
      // Heartbeat ping is an implementation detail — don't surface it
      // in conversations or refresh any state based on it.
      if (ev.type === "ping") return;
      // Pending interactions targeted at the human (AskUserQuestion or
      // ExitPlanMode with route=human) are blocking the agent right
      // now; auto-open the env pane so the form is visible without
      // the operator having to know Ctrl+B. No-op when already open.
      if (
        (ev.type === "pending_question" || ev.type === "pending_plan")
        && ev.route === "human"
      ) {
        setEnvOpen((cur) => cur || true);
      }
      const aid = ev.agent_id || "system";
      // Streaming deltas update a separate ephemeral buffer so the
      // conversations list (persisted / reloaded) stays clean. Text &
      // thinking are kept in distinct slots per agent; either can be
      // active independently.
      if (ev.type === "text_delta" || ev.type === "thinking_delta") {
        const key = ev.type === "text_delta" ? "text" : "thinking";
        enqueueStreamingDelta(aid, key, ev.delta || "");
        return;
      }
      // Cross-pane fan-out: inter-agent events land in BOTH the actor's
      // pane and the target's pane so a user watching p3 can see the
      // message from Coach without having to open Coach's pane too.
      // Events carry an explicit recipient:
      //   - message_sent: to_id (an agent id, 'coach', or 'broadcast')
      //   - task_assigned: to    (always a slot id)
      // Broadcasts fan to every agent id we know about.
      // Recurrence pane live refresh (recurrence-specs.md §12.4).
      // Any of these events means a row was added/removed/changed/
      // fired/skipped/disabled — reload the list so timestamps and
      // counts stay accurate.
      if (
        ev.type === "recurrence_added" ||
        ev.type === "recurrence_changed" ||
        ev.type === "recurrence_deleted" ||
        ev.type === "recurrence_fired" ||
        ev.type === "recurrence_skipped" ||
        ev.type === "recurrence_disabled"
      ) {
        if (recurrenceOpen) refreshRecurrences();
      }

      const fanoutTargets = new Set();
      fanoutTargets.add(aid);
      if (ev.type === "message_sent") {
        const toId = ev.to;
        if (toId === "broadcast") {
          ["coach", "p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "p9", "p10"]
            .forEach((s) => fanoutTargets.add(s));
        } else if (toId) {
          fanoutTargets.add(toId);
        }
      } else if (ev.type === "task_assigned") {
        if (ev.to) fanoutTargets.add(ev.to);
      } else if (ev.type === "task_updated") {
        // Status changes to a task owned by someone else (Coach
        // cancelling / blocking a task assigned to p3) should show up
        // in the owner's pane too.
        if (ev.owner && ev.owner !== ev.agent_id) fanoutTargets.add(ev.owner);
      }
      setConversations((prev) => {
        const next = new Map(prev);
        for (const tgt of fanoutTargets) {
          const list = next.get(tgt) || [];
          // Same event object carries a stable __id from the server, so
          // we dedupe defensively — a refresh that replays history
          // should not double the fan-out copies.
          if (ev.__id != null && list.some((e) => e.__id === ev.__id)) continue;
          next.set(tgt, [...list, ev]);
        }
        return next;
      });
      // Clear the matching streaming buffer when the authoritative final
      // block arrives, or the turn ends. Prevents the partial bubble
      // lingering after the real text/thinking event renders. We also
      // drop any pending (un-flushed) deltas so a rAF fire after this
      // point can't re-introduce stale content.
      if (ev.type === "text" || ev.type === "thinking") {
        const key = ev.type === "text" ? "text" : "thinking";
        const pending = streamingPendingRef.current;
        const p = pending.get(aid);
        if (p && p[key]) pending.set(aid, { ...p, [key]: "" });
        setStreamingText((prev) => {
          const cur = prev.get(aid);
          if (!cur) return prev;
          const next = new Map(prev);
          next.set(aid, { ...cur, [key]: "" });
          return next;
        });
      }
      if (
        ev.type === "result" ||
        ev.type === "agent_stopped" ||
        ev.type === "agent_cancelled" ||
        ev.type === "error"
      ) {
        streamingPendingRef.current.delete(aid);
        setStreamingText((prev) => {
          if (!prev.has(aid)) return prev;
          const next = new Map(prev);
          next.set(aid, { text: "", thinking: "" });
          return next;
        });
      }
      if (
        ev.type === "agent_started" ||
        ev.type === "agent_stopped" ||
        ev.type === "result" ||
        ev.type === "error" ||
        ev.type === "cost_capped" ||
        ev.type === "session_cleared" ||
        ev.type === "player_assigned" ||
        ev.type === "runtime_updated" ||
        ev.type === "lock_updated" ||
        ev.type === "agent_cancelled"
      ) {
        refreshAgents();
      }
      if (ev.type === "team_runtimes_updated") {
        try {
          window.dispatchEvent(new CustomEvent("team-runtimes-updated", { detail: ev }));
        } catch (_) {}
      }
      if (ev.type === "pause_toggled") {
        setPaused(Boolean(ev.paused));
      }
      // Phase 3 — project lifecycle events trigger a list refresh so
      // the LeftRail dropdown and active-project pill stay current.
      // The switch flow also surfaces project_switch_step events but
      // we don't act on them here (Phase 4 busy modal will).
      if (
        ev.type === "project_created" ||
        ev.type === "project_deleted" ||
        ev.type === "project_updated" ||
        ev.type === "project_switched"
      ) {
        refreshProjects();
        if (ev.type === "project_switched") {
          // Audit fix: terminal events carry `terminal:true`; non-
          // terminal `project_switch_step` events go through their own
          // (Phase 4) channel. Clear UI in-flight state on terminal.
          setSwitchingProject(null);
          // Phase 4 — promote the busy modal's terminal state so it
          // shows ✓ / ✗ and lets the user dismiss / retry.
          setSwitchBusy((prev) => {
            if (!prev || prev.jobId !== ev.job_id) return prev;
            return {
              ...prev,
              terminal: true,
              ok: ev.ok !== false,
              failedStep: ev.failed_step || null,
              error: ev.error || null,
            };
          });
          // Drain any straggler buffered steps on terminal so the
          // map doesn't grow unbounded across multiple switches.
          if (ev.job_id) switchStepBuffer.current.delete(ev.job_id);
          if (ev.ok !== false) {
            // Phase 3 audit fix #7+#11: clear the conversations Map
            // BEFORE re-fetching, then seed from /api/events for the
            // new project. The clear+reseed is bracketed so any
            // post-switch events that fire WHILE we're seeding are
            // appended onto the fresh history.
            setConversations(new Map());
            // Drop any partial streaming buffers from the prior project
            // so the new pane render doesn't show old in-flight tokens.
            setStreamingText(new Map());
            streamingPendingRef.current = new Map();
            refreshAgents();
            // Re-seed conversations from /api/events scoped to the
            // new active project. Without this, panes go blank until
            // the next live event arrives — spec §6 wants seamless
            // reload.
            seedConversationsFromHistory();
            // Phase 5 (PROJECTS_SPEC.md §7): the active project's
            // path changed, so the bottom root in /api/files/roots
            // is now pointing at /data/projects/<new-slug>/. Bump
            // fsEpoch + refetch roots so FilesPane rebinds in place.
            loadFileRoots();
            setFsEpoch((n) => n + 1);
            setProjectEpoch((n) => n + 1);
          }
        }
      }
      // Phase 4 busy modal: stepper feeds off project_switch_step.
      // Audit fix #1: every step is buffered by job_id so the modal
      // can drain late-set jobIds. When the modal already has the
      // matching job_id, we update its `steps` directly too.
      if (ev.type === "project_switch_step" && ev.job_id) {
        const buf = switchStepBuffer.current;
        const list = buf.get(ev.job_id) || [];
        const row = {
          step: ev.step,
          status: ev.status,
          detail: ev.detail || null,
          ts: ev.ts,
        };
        const idx = list.findIndex((s) => s.step === ev.step);
        if (idx >= 0) list[idx] = row;
        else list.push(row);
        buf.set(ev.job_id, list);
        setSwitchBusy((prev) => {
          if (!prev || prev.jobId !== ev.job_id) return prev;
          const nextSteps = prev.steps.slice();
          const sIdx = nextSteps.findIndex((s) => s.step === ev.step);
          if (sIdx >= 0) nextSteps[sIdx] = row;
          else nextSteps.push(row);
          return { ...prev, steps: nextSteps };
        });
      }
      if (ev.type === "result" || ev.type === "cost_capped") {
        refreshStatus();
      }
      if (
        ev.type === "task_created" ||
        ev.type === "task_claimed" ||
        ev.type === "task_assigned" ||
        ev.type === "task_updated"
      ) {
        refreshTasks();
        refreshAgents();
      }
      // Filesystem-changing events — bump fsEpoch so FilesPane reloads
      // its tree. Covers agents writing via coord_write_* MCP tools,
      // humans writing via the files pane editor, context edits, and
      // decision records. Cheap (one integer tick) so no harm if
      // nobody's watching.
      if (
        ev.type === "file_written" ||
        ev.type === "knowledge_written" ||
        ev.type === "context_updated" ||
        ev.type === "context_deleted" ||
        ev.type === "decision_written"
      ) {
        setFsEpoch((n) => n + 1);
      }
    };
    return () => {
      if (reopenTimer) clearTimeout(reopenTimer);
      clearInterval(watchdog);
      try { ws.close(); } catch (_) { /* ignore */ }
    };
  }, [loadAgents, loadTasks, loadStatus, refreshAgents, refreshTasks, refreshStatus, enqueueStreamingDelta, wsAttempt]);

  const openSlots = useMemo(() => flatSlots(openColumns), [openColumns]);

  // Mark a slot as seen up through its current latest event. Used when
  // the user opens a pane, shift-clicks an already-open slot, etc.
  const markSeen = useCallback((slot) => {
    setSeenTs((prev) => {
      // Read the latest ts at mark-time from a ref-like pattern —
      // passing conversations as a dep here would force this callback
      // to rebuild on every event, which is wasteful.
      const list = conversationsRef.current.get(slot);
      if (!list || !list.length) return prev;
      const lastTs = list[list.length - 1].ts || new Date().toISOString();
      if (prev[slot] === lastTs) return prev;
      return { ...prev, [slot]: lastTs };
    });
  }, []);
  // Keep a non-state reference current for use inside stable callbacks.
  const conversationsRef = useRef(conversations);
  useEffect(() => { conversationsRef.current = conversations; }, [conversations]);

  // Compute which slots have unread activity: they have events newer
  // than their seen ts AND are not currently open (open panes are
  // considered always seen, since the user can see events landing).
  const unreadSlots = useMemo(() => {
    const out = new Set();
    for (const [slot, list] of conversations) {
      if (openSlots.includes(slot)) continue;
      if (!list.length) continue;
      const last = list[list.length - 1].ts || "";
      if (!last) continue;
      if (last > (seenTs[slot] || "")) out.add(slot);
    }
    return out;
  }, [conversations, seenTs, openSlots]);

  // Slots in a "problem" state — surfaced as a red box in the rail.
  // Three sources:
  //   1. agents.status === "error" (last turn errored hard).
  //   2. Cost-cap exhausted: agent's cost ≥ agent_daily_cap, OR the
  //      whole team is over team_daily_cap. The agent's literal
  //      `status` field stays "idle" when capped (the cap blocks the
  //      *next* spawn but doesn't change current state) so we have
  //      to derive this from /api/status.caps + agent.cost_estimate_usd.
  //   3. Most recent decisive event in the pane timeline is
  //      `agent_cancelled` (no `result` or `agent_started` after it).
  //      Cancellation also resets status → "idle" on the backend.
  // Cleared on the next successful turn (status=working/idle with new
  // result), on cap reset (next day), or by closing/reopening the pane
  // for cancelled state (a fresh agent_started overwrites it).
  const problemSlots = useMemo(() => {
    const out = new Set();
    const caps = serverStatus?.caps;
    const agentCap = caps?.agent_daily_usd || 0;
    const teamCap = caps?.team_daily_usd || 0;
    const teamToday = caps?.team_today_usd || 0;
    const teamCapped = teamCap > 0 && teamToday >= teamCap;
    for (const a of agents) {
      if (a.status === "error") {
        out.add(a.id);
        continue;
      }
      if (
        teamCapped ||
        (agentCap > 0 && (a.cost_estimate_usd || 0) >= agentCap)
      ) {
        out.add(a.id);
        continue;
      }
      const events = conversations.get(a.id) || [];
      // Walk backwards for the last decisive event. agent_cancelled
      // wins if it's the most recent; result / agent_started further
      // back means the cancellation was already superseded.
      for (let i = events.length - 1; i >= 0; i--) {
        const e = events[i];
        if (e.type === "agent_cancelled") {
          out.add(a.id);
          break;
        }
        if (e.type === "result" || e.type === "agent_started") {
          break;
        }
      }
    }
    return out;
  }, [agents, conversations, serverStatus]);

  // Per-agent comms-state dot for the LeftRail. Returns
  // Map<slotId, "green"|"blue"|"orange">.
  //
  //   blue   the agent has unread inbox to address — there's an incoming
  //          message_sent (to=slot) or task_assigned (to=slot) newer than
  //          the agent's last agent_started.
  //   orange the agent is idle, has a current task, and the most recent
  //          direct (non-broadcast, non-human) outgoing message_sent is
  //          newer than any incoming message AND newer than the last
  //          agent_started — i.e. it sent something asking for input
  //          and is now waiting.
  //   green  default — nothing pending.
  //
  // Computed from the events in `conversations` (WS-driven) plus the
  // pane-history backfill that runs when a pane is opened. For slots
  // that have never been opened in this session, only WS-period events
  // are considered — accepted "ok for flicker" trade-off.
  const dotStates = useMemo(() => {
    const out = new Map();
    for (const a of agents) {
      const events = conversations.get(a.id) || [];
      let lastIn = "", lastOut = "", lastStarted = "";
      for (const e of events) {
        const t = e.ts || "";
        if (!t) continue;
        const type = e.type;
        if (type === "agent_started" && e.agent_id === a.id) {
          if (t > lastStarted) lastStarted = t;
        } else if (type === "message_sent") {
          if (e.to === a.id) {
            if (t > lastIn) lastIn = t;
          } else if (
            e.agent_id === a.id &&
            e.to !== "broadcast" &&
            e.to !== "human"
          ) {
            if (t > lastOut) lastOut = t;
          }
        } else if (type === "task_assigned" && e.to === a.id) {
          if (t > lastIn) lastIn = t;
        }
      }
      let dot = "green";
      if (lastIn && lastIn > lastStarted) {
        dot = "blue";
      } else if (
        lastOut &&
        lastOut > lastIn &&
        lastOut > lastStarted &&
        a.status === "idle" &&
        a.current_task_id
      ) {
        dot = "orange";
      }
      out.set(a.id, dot);
    }
    return out;
  }, [agents, conversations]);

  // Reflect live state in the tab title so a backgrounded tab still
  // signals:
  //   ⏸          paused (takes precedence over other signals)
  //   N⚡        N agents currently working
  //   M●        M slots with unread activity (closed panes only)
  // When both working and unread are nonzero they're combined.
  // Declared AFTER unreadSlots to avoid a temporal-dead-zone crash on
  // first render (const hoisting only reserves the binding, not the
  // value).
  useEffect(() => {
    const working = agents.filter((a) => a.status === "working").length;
    const unread = unreadSlots.size;
    let parts = [];
    if (paused) parts.push("⏸");
    if (working > 0) parts.push(`${working}⚡`);
    if (unread > 0 && !paused) parts.push(`${unread}●`);
    const prefix = parts.length > 0 ? parts.join(" ") + " " : "";
    document.title = `${prefix}TeamOfTen`;
  }, [paused, agents, unreadSlots]);

  // Open a slot as a new standalone column on the right. Also marks
  // the slot as seen so any prior unread badge clears. If the slot is
  // already open, scroll its pane into view — user probably clicked
  // to find it. Auto-restores from maximize: clicking any rail slot
  // means the user wants the multi-pane layout back.
  const openPane = useCallback((slot) => {
    let alreadyOpen = false;
    setOpenColumns((prev) => {
      if (flatSlots(prev).includes(slot)) {
        alreadyOpen = true;
        return prev;
      }
      return [...prev, [slot]];
    });
    setMaximizedSlot((cur) => (cur && cur !== slot ? null : cur));
    markSeen(slot);
    if (alreadyOpen) {
      // Defer to next tick so layout settles if the state update
      // also opened another pane on the same click.
      setTimeout(() => {
        const el = document.getElementById("pane-" + slot);
        if (el?.scrollIntoView) {
          el.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
        }
      }, 0);
    }
  }, [markSeen]);
  // Remove a pane, dropping the column if it becomes empty. Mark as
  // seen through the current last event, since an open pane is
  // considered always-seen — closing it acknowledges what was visible.
  const closePane = useCallback((slot) => {
    setOpenColumns((prev) => {
      const out = prev
        .map((col) => col.filter((s) => s !== slot))
        .filter((col) => col.length > 0);
      return out;
    });
    setMaximizedSlot((cur) => (cur === slot ? null : cur));
    markSeen(slot);
  }, [markSeen]);
  // Append a slot to the bottom of the rightmost (last) column. If
  // slot already open anywhere else, move it. If no columns yet, opens
  // as the first one. Also exits maximize since the user wants the
  // stack visible.
  const stackInLast = useCallback((slot) => {
    setOpenColumns((prev) => {
      const without = prev
        .map((col) => col.filter((s) => s !== slot))
        .filter((col) => col.length > 0);
      if (without.length === 0) return [[slot]];
      const last = without[without.length - 1];
      const next = [
        ...without.slice(0, -1),
        [...last, slot],
      ];
      return next;
    });
    setMaximizedSlot(null);
    markSeen(slot);
  }, [markSeen]);

  // Toggle a pane between maximized (full panes-area) and the user's
  // saved layout. Clicking maximize on a different pane while one is
  // already maximized switches focus to the new one.
  const toggleMaximize = useCallback((slot) => {
    setMaximizedSlot((cur) => (cur === slot ? null : slot));
  }, []);

  // Stack a slot below an existing column (used by the pane header's
  // "+ stack below" action). If slot already open, move it; otherwise add.
  const stackBelow = useCallback((slot, anchorSlot) => {
    setOpenColumns((prev) => {
      const without = prev
        .map((col) => col.filter((s) => s !== slot))
        .filter((col) => col.length > 0);
      const target = without.findIndex((col) => col.includes(anchorSlot));
      if (target < 0) return [...without, [slot]];
      const next = without.map((col, i) =>
        i === target
          ? [...col.slice(0, col.indexOf(anchorSlot) + 1), slot, ...col.slice(col.indexOf(anchorSlot) + 1)]
          : col
      );
      return next;
    });
  }, []);

  // Drag-to-move: insert `slot` into the same column as `anchorSlot`,
  // immediately BEFORE the anchor. Used by HTML5 DnD from one pane's
  // header onto another pane's body. Handles both within-column
  // reorder and cross-column moves, including empty-column cleanup.
  const movePaneBefore = useCallback((slot, anchorSlot) => {
    if (slot === anchorSlot) return;
    setOpenColumns((prev) => {
      const without = prev
        .map((col) => col.filter((s) => s !== slot))
        .filter((col) => col.length > 0);
      const targetCol = without.findIndex((col) => col.includes(anchorSlot));
      if (targetCol < 0) return [...without, [slot]];
      return without.map((col, i) => {
        if (i !== targetCol) return col;
        const at = col.indexOf(anchorSlot);
        return [...col.slice(0, at), slot, ...col.slice(at)];
      });
    });
  }, []);

  // DnD drop on the "end of column" strip — append to that column.
  const moveToColEnd = useCallback((slot, colIdx) => {
    setOpenColumns((prev) => {
      const without = prev
        .map((col) => col.filter((s) => s !== slot))
        .filter((col) => col.length > 0);
      if (colIdx >= without.length) return [...without, [slot]];
      return without.map((col, i) =>
        i === colIdx ? [...col, slot] : col
      );
    });
  }, []);

  // Edge-targeted drop onto a specific anchor pane:
  //   top    — stack dragged ABOVE anchor in anchor's column
  //   bottom — stack dragged BELOW anchor in anchor's column
  //   left   — new column to the LEFT of anchor's column (single pane)
  //   right  — new column to the RIGHT of anchor's column (single pane)
  const dropOnPaneEdge = useCallback((dragged, anchor, edge) => {
    if (dragged === anchor) return;
    setOpenColumns((prev) => {
      const without = prev
        .map((col) => col.filter((s) => s !== dragged))
        .filter((col) => col.length > 0);
      const targetCol = without.findIndex((col) => col.includes(anchor));
      if (targetCol < 0) return [...without, [dragged]];
      if (edge === "top" || edge === "bottom") {
        return without.map((col, i) => {
          if (i !== targetCol) return col;
          const at = col.indexOf(anchor);
          const ins = edge === "top" ? at : at + 1;
          return [...col.slice(0, ins), dragged, ...col.slice(ins)];
        });
      }
      // left / right → brand-new column adjacent to target column
      const insertAt = edge === "left" ? targetCol : targetCol + 1;
      return [
        ...without.slice(0, insertAt),
        [dragged],
        ...without.slice(insertAt),
      ];
    });
  }, []);

  // DnD drop on the "new column" rail — append as a fresh column.
  const moveToNewColumn = useCallback((slot) => {
    setOpenColumns((prev) => {
      const without = prev
        .map((col) => col.filter((s) => s !== slot))
        .filter((col) => col.length > 0);
      return [...without, [slot]];
    });
  }, []);

  // Quick-layout presets. Re-flow every currently-open pane into a fresh
  // layout without touching which panes are open or their order.
  //   "spread": every pane in its own column   [a][b][c][d]
  //   "pairs":  two stacked per column; if odd, first one is solo
  //             [a] [b over c] [d over e]
  const applyLayoutPreset = useCallback((preset) => {
    setOpenColumns((prev) => {
      const slots = prev.flat();
      if (slots.length === 0) return [];
      if (preset === "spread") return slots.map((s) => [s]);
      if (preset === "pairs") {
        const cols = [];
        let i = 0;
        if (slots.length % 2 === 1) {
          cols.push([slots[0]]);
          i = 1;
        }
        for (; i < slots.length; i += 2) {
          cols.push([slots[i], slots[i + 1]]);
        }
        return cols;
      }
      return prev;
    });
    // Layout presets imply "show me the multi-pane arrangement" —
    // staying maximized would defeat the click.
    setMaximizedSlot(null);
  }, []);

  // Phone layout: single-pane swipe deck, so we ignore the user's
  // 2D column structure and singletonize every open slot. Sorted by
  // canonical agent order (Coach → p1..p10 → special slots) so the
  // swipe sequence is predictable instead of reflecting pane-open
  // history.
  const isPhone = useIsPhone();

  // While maximized: collapse the layout to a single solo column so
  // Split.js stands down (no gutters needed) and the chosen pane gets
  // the full panes-area. If the maximized slot is no longer open
  // (stale value, race), fall back to the user's saved layout.
  const effectiveColumns = useMemo(() => {
    if (maximizedSlot && flatSlots(openColumns).includes(maximizedSlot)) {
      return [[maximizedSlot]];
    }
    if (isPhone) {
      const flat = flatSlots(openColumns);
      const sorted = [...flat].sort((a, b) => {
        const ai = canonicalSlotIndex(a);
        const bi = canonicalSlotIndex(b);
        if (ai !== bi) return ai - bi;
        return flat.indexOf(a) - flat.indexOf(b);
      });
      return sorted.map((s) => [s]);
    }
    return openColumns;
  }, [openColumns, maximizedSlot, isPhone]);
  const isMaximized = effectiveColumns !== openColumns;
  // Split.js: horizontal split across columns, vertical split inside each
  // multi-pane column. Rebind whenever the layout structure changes.
  // A stable structure signature lets us skip reinit on no-op renders.
  // Signature derived from EFFECTIVE columns so toggling maximize
  // properly tears down + rebuilds gutters.
  const layoutSignature = useMemo(
    () => effectiveColumns.map((c) => c.join("|")).join("//"),
    [effectiveColumns]
  );
  // Persisted drag sizes. We mutate through the ref so changes don't
  // trigger a re-render (Split.js holds the sizes during drag).
  const splitSizesRef = useRef(loadSplitSizes());
  useLayoutEffect(() => {
    const cleanups = [];
    const sizes = splitSizesRef.current;
    const persist = (key, arr) => {
      sizes[key] = arr;
      saveSplitSizes(sizes);
    };
    const resolveSizes = (key, n) => {
      const stored = sizes[key];
      if (Array.isArray(stored) && stored.length === n) return stored;
      return Array(n).fill(100 / n);
    };
    // Split.js sets inline `width`/`height`, but our panes & columns use
    // `flex: 1 1 0` — flex-basis 0 wins over the inline dimension, so drag
    // did nothing visible. Override Split.js to write `flex-basis` instead,
    // which the flex algorithm actually honors.
    const elementStyle = (dim, size, gutterSize) => ({
      "flex-basis": `calc(${size}% - ${gutterSize}px)`,
    });
    const gutterStyle = (dim, gutterSize) => ({
      "flex-basis": `${gutterSize}px`,
    });
    // Outer horizontal split across columns (only if >= 2 columns).
    if (effectiveColumns.length >= 2) {
      const selectors = effectiveColumns.map((_, i) => "#col-" + i);
      const exist = selectors.every((sel) => document.querySelector(sel));
      if (exist) {
        const hKey = "h:" + layoutSignature;
        try {
          const h = Split(selectors, {
            sizes: resolveSizes(hKey, effectiveColumns.length),
            minSize: 260,
            gutterSize: 6,
            snapOffset: 0,
            dragInterval: 1,
            direction: "horizontal",
            elementStyle,
            gutterStyle,
            onDragEnd: (arr) => persist(hKey, arr),
          });
          cleanups.push(() => { try { h.destroy(); } catch (_) {} });
        } catch (e) {
          console.error("Horizontal Split init failed", e);
        }
      }
    }
    // Per-column vertical split for stacked panes.
    effectiveColumns.forEach((col, i) => {
      if (col.length < 2) return;
      const selectors = col.map((s) => "#pane-" + s);
      const exist = selectors.every((sel) => document.querySelector(sel));
      if (!exist) return;
      const vKey = "v:" + col.join("|");
      try {
        const v = Split(selectors, {
          sizes: resolveSizes(vKey, col.length),
          minSize: 120,
          gutterSize: 6,
          snapOffset: 0,
          dragInterval: 1,
          direction: "vertical",
          elementStyle,
          gutterStyle,
          onDragEnd: (arr) => persist(vKey, arr),
        });
        cleanups.push(() => { try { v.destroy(); } catch (_) {} });
      } catch (e) {
        console.error("Vertical Split init failed", e);
      }
    });
    return () => cleanups.forEach((fn) => fn());
  }, [layoutSignature]);

  const onEnvResizerDown = useCallback((e) => {
    e.preventDefault();
    const handle = e.currentTarget;
    const move = (ev) => {
      const next = Math.max(
        260,
        Math.min(Math.floor(window.innerWidth * 0.75), window.innerWidth - ev.clientX)
      );
      setEnvWidth(next);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      handle?.classList?.remove("dragging");
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    handle?.classList?.add("dragging");
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }, []);

  // 4-column grid when both envOpen and recurrenceOpen are true.
  // Recurrence pane sits LEFT of env pane (matches spec §12.2: "opens
  // to the right side, alongside the EnvPane"), so order is:
  //   rail | panes | recurrence | env
  const appStyle = (() => {
    if (envOpen && recurrenceOpen) {
      return `grid-template-columns: 44px 1fr 320px ${envWidth}px`;
    }
    if (envOpen) {
      return `grid-template-columns: 44px 1fr ${envWidth}px`;
    }
    if (recurrenceOpen) {
      return `grid-template-columns: 44px 1fr 320px`;
    }
    return undefined;
  })();

  // Phase 4: multi-stage switch flow.
  //   1. onActivateProject(slug) → fetch /switch-preview → open
  //      ProjectSwitchConfirmModal.
  //   2. User clicks Switch → POST /activate.
  //      - 202: open ProjectSwitchBusyModal, listen for steps via WS.
  //      - 423: open ProjectSwitchInFlightModal sub-prompt.
  //      - 409: surface error in the confirm modal.
  //   3. project_switched event arrives → busy modal flips to terminal
  //      (ok/error). User dismisses; success path also re-seeds
  //      conversations (Phase 3 audit fix).
  const _doActivate = useCallback(async (slug, fromName, toName, preview) => {
    setSwitchingProject(slug);
    setSwitchBusy({
      to: slug,
      fromName,
      toName,
      jobId: null,
      steps: [],
      terminal: false,
      ok: null,
      // Audit fix #6: surface the live-conversation count from the
      // preview onto the push_current step so the busy modal mirrors
      // spec §6's "Snapshotting Misc live conversations (3 files)".
      liveConversations: preview ? preview.live_conversations : 0,
    });
    try {
      const res = await authedFetch(
        `/api/projects/${encodeURIComponent(slug)}/activate`,
        { method: "POST" },
      );
      const data = await res.json().catch(() => ({}));
      if (res.status === 202) {
        // Subscribe by job_id and drain any pre-arrival steps that
        // landed in the buffer before this assignment (audit fix #1).
        const jobId = data.job_id;
        setSwitchBusy((prev) => {
          if (!prev) return prev;
          const buffered = switchStepBuffer.current.get(jobId) || [];
          // Merge buffered into prev.steps (replace by step name).
          const merged = prev.steps.slice();
          for (const row of buffered) {
            const idx = merged.findIndex((s) => s.step === row.step);
            if (idx >= 0) merged[idx] = row;
            else merged.push(row);
          }
          return { ...prev, jobId, steps: merged };
        });
        return { ok: true };
      }
      if (res.status === 200 && data.noop) {
        setSwitchBusy(null);
        setSwitchingProject(null);
        return { ok: true, noop: true };
      }
      if (res.status === 423) {
        setSwitchBusy(null);
        setSwitchingProject(null);
        setSwitchInFlight({
          to: slug,
          fromName,
          toName,
          agentId: data.detail
            ? (data.detail.match(/agent '(\w+)'/) || [])[1] || "an agent"
            : "an agent",
        });
        return { ok: false, code: 423 };
      }
      if (res.status === 409) {
        // Keep the busy modal open with a terminal failure so the
        // user can dismiss / retry instead of silently losing context.
        setSwitchingProject(null);
        setSwitchBusy((prev) => prev ? {
          ...prev,
          terminal: true,
          ok: false,
          failedStep: "preflight",
          error: data.detail || "Another switch is already in progress.",
        } : prev);
        return { ok: false, code: 409, detail: data.detail };
      }
      // Audit fix #3: surface every other 4xx/5xx as a terminal-failure
      // busy modal so the user gets feedback instead of a silent
      // dismissal. 502 (kDrive unreachable on pre-pull, spec §6) lands
      // here too — the Retry button gives the spec-required affordance.
      setSwitchingProject(null);
      setSwitchBusy((prev) => prev ? {
        ...prev,
        terminal: true,
        ok: false,
        failedStep: `http_${res.status}`,
        error: data.detail || res.statusText || `HTTP ${res.status}`,
      } : prev);
      return {
        ok: false,
        code: res.status,
        detail: data.detail || res.statusText,
      };
    } catch (e) {
      console.error("activate failed", e);
      setSwitchingProject(null);
      setSwitchBusy((prev) => prev ? {
        ...prev,
        terminal: true,
        ok: false,
        failedStep: "network",
        error: String(e).slice(0, 200),
      } : prev);
      return { ok: false, detail: String(e) };
    }
  }, [authedFetch]);

  const onActivateProject = useCallback(async (slug) => {
    if (!slug || slug === activeProjectId) return;
    // Phase 4: hit /switch-preview first, then open the confirm modal.
    let preview = null;
    try {
      const res = await authedFetch(
        `/api/projects/switch-preview?to=${encodeURIComponent(slug)}`,
      );
      if (res.ok) preview = await res.json();
    } catch (e) {
      console.warn("switch preview failed (continuing without counts):", e);
    }
    setSwitchConfirm({
      to: slug,
      preview,
      error: null,
    });
  }, [authedFetch, activeProjectId]);

  // Phase 4: confirm modal "Switch" button. Closes the confirm modal
  // and dispatches the actual activate POST.
  const onConfirmSwitch = useCallback(async () => {
    const sc = switchConfirm;
    if (!sc) return;
    setSwitchConfirm(null);
    const fromName = sc.preview ? sc.preview.from_name : "current project";
    const toName = sc.preview ? sc.preview.to_name : sc.to;
    await _doActivate(sc.to, fromName, toName, sc.preview);
  }, [switchConfirm, _doActivate]);

  // Phase 4: in-flight sub-modal "Cancel turns and switch" button.
  // Hard-cancel via the existing /api/agents/cancel-all endpoint
  // (spec §14 Q2 recommendation: hard cancel), then re-attempt the
  // switch. Audit fix #2 — fixed 600ms delay was too short for SDK
  // teardown (1-2s typical); poll /api/agents until every agent is
  // out of `working`/`waiting`, with a hard timeout cap.
  const onCancelTurnsAndSwitch = useCallback(async () => {
    const sif = switchInFlight;
    if (!sif) return;
    setSwitchInFlight(null);
    try {
      await authedFetch("/api/agents/cancel-all", { method: "POST" });
    } catch (e) {
      console.error("cancel-all failed:", e);
    }
    // Poll /api/agents up to 8s. Most cancellations resolve within
    // ~1s; the cap protects against a wedged SDK process.
    const POLL_INTERVAL_MS = 250;
    const POLL_TIMEOUT_MS = 8000;
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    while (Date.now() < deadline) {
      try {
        const res = await authedFetch("/api/agents");
        if (res.ok) {
          const data = await res.json();
          const busy = (data.agents || []).find(
            (a) => a.status === "working" || a.status === "waiting"
          );
          if (!busy) break;
        }
      } catch (e) {
        // Treat fetch failure as "still busy" — keep polling until
        // the deadline.
      }
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }
    await _doActivate(sif.to, sif.fromName, sif.toName, null);
  }, [switchInFlight, _doActivate, authedFetch]);

  // Phase 3: create a new project via prompt. Phase 4 will replace
  // this with a proper modal. Slug is auto-derived from name; user
  // can override via a second prompt.
  const onCreateProject = useCallback(async () => {
    const name = prompt("New project name:");
    if (!name || !name.trim()) return;
    // Derive a slug client-side; server re-validates.
    let slug = name.trim().toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .replace(/-{2,}/g, "-");
    if (!slug || slug.length < 2) {
      alert("Could not derive a valid slug from that name. Try a name with at least one letter.");
      return;
    }
    const overridden = prompt(`Slug (URL-safe id) [default: ${slug}]:`, slug);
    if (overridden === null) return;
    if (overridden.trim()) slug = overridden.trim();
    try {
      const res = await authedFetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slug, name: name.trim() }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert(`Create failed: ${data.detail || res.statusText}`);
        return;
      }
      // Success: refreshProjects via WS event_received project_created.
    } catch (e) {
      console.error("create project failed", e);
    }
  }, [authedFetch]);

  return html`
    <div class=${"app" + (envOpen ? " env-open" : "")} style=${appStyle}>
      <${LeftRail}
        agents=${agents}
        openSlots=${openSlots}
        dotStates=${dotStates}
        problemSlots=${problemSlots}
        projects=${projects}
        activeProjectId=${activeProjectId}
        switchingProject=${switchingProject}
        onActivateProject=${onActivateProject}
        onCreateProject=${onCreateProject}
        onOpen=${openPane}
        onStackInLast=${stackInLast}
        wsConnected=${wsConnected}
        envOpen=${envOpen}
        onToggleEnv=${() => setEnvOpen((v) => !v)}
        recurrenceOpen=${recurrenceOpen}
        onToggleRecurrence=${() => setRecurrenceOpen((v) => !v)}
        onOpenSettings=${() => setSettingsOpen(true)}
        paused=${paused}
        onTogglePause=${togglePause}
        onLayoutPreset=${applyLayoutPreset}
        onCancelAll=${async () => {
          const working = agents.filter((a) => a.status === "working").length;
          if (working === 0) return;
          if (!confirm(`Cancel ${working} running agent${working === 1 ? "" : "s"}?`)) return;
          await authedFetch("/api/agents/cancel-all", { method: "POST" });
        }}
      />
      <main class=${"panes" + (isMaximized ? " maximized" : "")}>
        ${effectiveColumns.length === 0
          ? html`<div class="empty">Pick a slot on the left to open a pane.</div>`
          : html`
              ${effectiveColumns.map(
                (col, colIdx) =>
                  html`<div
                    class=${"pane-col" + (col.length > 1 ? " stacked" : "")}
                    id=${"col-" + colIdx}
                    key=${"col-" + col.join("-")}
                  >
                    ${col.map((slot) => {
                      // Special slots (prefix __) render a non-agent pane.
                      // Currently just __files; room to add __knowledge etc.
                      if (slot === "__files") {
                        return html`<${FilesPane}
                          key=${slot}
                          slot=${slot}
                          authedFetch=${authedFetch}
                          fsEpoch=${fsEpoch}
                          onClose=${() => closePane(slot)}
                          onDropEdge=${dropOnPaneEdge}
                          onPopOut=${moveToNewColumn}
                          stacked=${col.length > 1}
                          isMaximized=${maximizedSlot === slot}
                          onToggleMaximize=${() => toggleMaximize(slot)}
                          rootsFromApp=${fileRoots}
                          pendingFileOpen=${pendingFileOpen}
                          clearPendingFileOpen=${clearPendingFileOpen}
                        />`;
                      }
                      const agent = agents.find((a) => a.id === slot);
                      const currentTask = agent?.current_task_id
                        ? tasks.find((t) => t.id === agent.current_task_id)
                        : null;
                      return html`<${AgentPane}
                        key=${slot}
                        slot=${slot}
                        agent=${agent}
                        currentTask=${currentTask}
                        liveEvents=${conversations.get(slot) || EMPTY_EVENTS}
                        streaming=${streamingText.get(slot)}
                        projectEpoch=${projectEpoch}
                        openSlots=${openSlots}
                        onClose=${() => closePane(slot)}
                        onDropEdge=${dropOnPaneEdge}
                        onPopOut=${moveToNewColumn}
                        stacked=${col.length > 1}
                        isMaximized=${maximizedSlot === slot}
                        onToggleMaximize=${() => toggleMaximize(slot)}
                      />`;
                    })}
                    ${isMaximized
                      ? null
                      : html`<${DropZone}
                          orientation="horizontal"
                          label="drop to append"
                          onDrop=${(slot) => moveToColEnd(slot, colIdx)}
                        />`}
                  </div>`
              )}
              ${isMaximized
                ? null
                : html`<${DropZone}
                    orientation="vertical"
                    label="new column"
                    onDrop=${moveToNewColumn}
                  />`}
            `}
      </main>
      ${recurrenceOpen
        ? html`<${RecurrencePane}
            rows=${recurrenceRows}
            onClose=${() => setRecurrenceOpen(false)}
            onRefresh=${refreshRecurrences}
            onError=${(msg) => setRecurrenceError(msg)}
          />`
        : null}
      ${envOpen
        ? html`<${EnvPane}
            agents=${agents}
            tasks=${tasks}
            conversations=${conversations}
            openSlots=${openSlots}
            serverStatus=${serverStatus}
            activeProjectId=${activeProjectId}
            onCreateTask=${createHumanTask}
            onClose=${() => setEnvOpen(false)}
            onResizerDown=${onEnvResizerDown}
          />`
        : null}
      ${settingsOpen
        ? html`<${SettingsDrawer}
            serverStatus=${serverStatus}
            onClose=${() => setSettingsOpen(false)}
          />`
        : null}
      ${authChallenge
        ? html`<${TokenGate}
            onSubmit=${(t) => {
              setToken(t);
              location.reload();
            }}
          />`
        : null}
      ${switchConfirm
        ? html`<${ProjectSwitchConfirmModal}
            confirm=${switchConfirm}
            onCancel=${() => setSwitchConfirm(null)}
            onSwitch=${onConfirmSwitch}
          />`
        : null}
      ${switchInFlight
        ? html`<${ProjectSwitchInFlightModal}
            inFlight=${switchInFlight}
            onWait=${() => setSwitchInFlight(null)}
            onCancelAndSwitch=${onCancelTurnsAndSwitch}
          />`
        : null}
      ${switchBusy
        ? html`<${ProjectSwitchBusyModal}
            busy=${switchBusy}
            onDismiss=${() => setSwitchBusy(null)}
            onRetry=${async () => {
              const target = switchBusy.to;
              const fromName = switchBusy.fromName;
              const toName = switchBusy.toName;
              const liveCount = switchBusy.liveConversations;
              setSwitchBusy(null);
              // Pass a synthetic preview so live-conversation count
              // survives the retry.
              await _doActivate(target, fromName, toName, {
                live_conversations: liveCount,
              });
            }}
          />`
        : null}
    </div>
  `;
}

// A thin strip that only highlights when a pane is being dragged.
// Used for "append to column end" and "create new column" targets.
function DropZone({ orientation, label, onDrop }) {
  const [active, setActive] = useState(false);
  const onDragOver = useCallback((e) => {
    const types = Array.from(e.dataTransfer.types || []);
    if (!types.includes("application/x-harness-slot")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setActive(true);
  }, []);
  const onDragLeave = useCallback((e) => {
    const next = e.relatedTarget;
    if (next && e.currentTarget.contains(next)) return;
    setActive(false);
  }, []);
  const handleDrop = useCallback((e) => {
    setActive(false);
    const dragged = e.dataTransfer.getData("application/x-harness-slot");
    if (!dragged) return;
    e.preventDefault();
    onDrop(dragged);
  }, [onDrop]);
  return html`
    <div
      class=${"drop-zone drop-zone-" + orientation + (active ? " active" : "")}
      onDragOver=${onDragOver}
      onDragLeave=${onDragLeave}
      onDrop=${handleDrop}
      title=${label}
    >
      ${active ? html`<span class="drop-zone-label">${label}</span>` : null}
    </div>
  `;
}

function TokenGate({ onSubmit }) {
  const [val, setVal] = useState("");
  return html`
    <div class="drawer-backdrop">
      <div class="drawer" style="width: 380px;">
        <header class="drawer-head">
          <h2 class="drawer-title">Authentication required</h2>
        </header>
        <div class="drawer-body">
          <p>
            This harness has <code>HARNESS_TOKEN</code> configured. Paste
            its value here — it'll be saved in this browser's
            localStorage and sent with every request.
          </p>
          <input
            type="password"
            value=${val}
            onInput=${(e) => setVal(e.target.value)}
            onKeyDown=${(e) => { if (e.key === "Enter" && val) onSubmit(val); }}
            placeholder="bearer token"
            class="env-task-title-input"
            style="font-family: ui-monospace; font-size: 13px;"
            autofocus
          />
          <button
            class="primary"
            style="margin-top: 12px; width: 100%;"
            disabled=${!val}
            onClick=${() => onSubmit(val)}
          >save & reload</button>
          <p class="muted" style="margin-top: 16px; font-size: 11px;">
            If you don't have the token, find it in your Zeabur service's
            Variables tab.
          </p>
        </div>
      </div>
    </div>
  `;
}

// ------------------------------------------------------------------
// left rail
// ------------------------------------------------------------------

// Phase 3 (PROJECTS_SPEC.md §13): minimal functional dropdown that
// replaces the disabled `P` placeholder. Polish (animation, modal,
// busy stepper) lands in Phase 4 — for now we just need a way to
// switch projects from the rail.
function ProjectSwitcher({ projects, activeProjectId, switchingProject, onActivate, onCreate }) {
  const [open, setOpen] = useState(false);
  const [menuPos, setMenuPos] = useState(null); // {left, bottom} in viewport coords
  const ref = useRef(null);
  const buttonRef = useRef(null);

  // Position the menu in viewport coordinates so it escapes the rail's
  // overflow-y: auto clipping. Computed once on open and on resize.
  const reposition = useCallback(() => {
    const btn = buttonRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    setMenuPos({
      left: Math.round(r.right + 6),
      // Anchor menu's bottom to button's bottom edge so it grows
      // upward (we're at the bottom of the rail).
      bottom: Math.round(window.innerHeight - r.bottom),
    });
  }, []);

  useEffect(() => {
    if (!open) return;
    reposition();
    function handleClickOutside(e) {
      if (ref.current && !ref.current.contains(e.target)) {
        setOpen(false);
      }
    }
    function handleScrollOrResize() { reposition(); }
    document.addEventListener("mousedown", handleClickOutside);
    window.addEventListener("resize", handleScrollOrResize);
    window.addEventListener("scroll", handleScrollOrResize, true);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      window.removeEventListener("resize", handleScrollOrResize);
      window.removeEventListener("scroll", handleScrollOrResize, true);
    };
  }, [open, reposition]);

  const active = projects.find((p) => p.id === activeProjectId);
  const visible = projects.filter((p) => !p.archived);
  const tooltip = active
    ? `Projects — active: ${active.name}` + (switchingProject ? ` (switching to ${switchingProject}…)` : "")
    : "Projects — no project active, click to pick";

  return html`
    <div class="project-switcher" ref=${ref}>
      <button
        ref=${buttonRef}
        class=${"gear project-pill" + (open ? " open" : "") + (switchingProject ? " switching" : "")}
        title=${tooltip}
        aria-label=${tooltip}
        onClick=${() => setOpen((v) => !v)}
        disabled=${Boolean(switchingProject)}
      >
        ${switchingProject
          ? html`<span class="project-pill-spinner" aria-hidden="true">↻</span>`
          : html`<span class="projects-icon" aria-hidden="true">
              <span class="projects-icon-back"></span>
              <span class="projects-icon-mid"></span>
              <span class="projects-icon-front"></span>
            </span>`}
      </button>
      ${open && menuPos ? html`
        <div
          class="project-menu"
          role="menu"
          style=${`left: ${menuPos.left}px; bottom: ${menuPos.bottom}px;`}
        >
          <div class="project-menu-head">Switch project</div>
          ${visible.length === 0
            ? html`<div class="project-menu-empty">No projects yet</div>`
            : visible.map((p) => html`
              <button
                key=${p.id}
                class=${"project-menu-item" + (p.id === activeProjectId ? " active" : "")}
                onClick=${() => {
                  setOpen(false);
                  if (p.id !== activeProjectId) onActivate(p.id);
                }}
                disabled=${Boolean(switchingProject)}
              >
                <span class="project-menu-check">${p.id === activeProjectId ? "✓" : ""}</span>
                <span class="project-menu-name">${p.name}</span>
                <span class="project-menu-slug">${p.id}</span>
              </button>
            `)}
          <div class="project-menu-sep"></div>
          <button
            class="project-menu-item project-menu-new"
            onClick=${() => {
              setOpen(false);
              onCreate();
            }}
          >+ New project…</button>
        </div>
      ` : null}
    </div>
  `;
}

// Phase 4 (PROJECTS_SPEC.md §6): pre-flight confirmation modal.
// Shows the user what's about to happen before any kDrive sync runs.
// Counts come from GET /api/projects/switch-preview; the modal
// degrades gracefully when /switch-preview failed (preview is null).
// Audit fix #5: format bytes as human-readable size for the confirm
// modal. Spec §6 example: "(12 files, ~340 KB)".
function formatBytes(n) {
  if (!n || n < 0) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function ProjectSwitchConfirmModal({ confirm, onCancel, onSwitch }) {
  const p = confirm.preview;
  const fromLabel = p ? p.from_name : "current project";
  const toLabel = p ? p.to_name : confirm.to;
  // Audit fix #7: when sync_state is empty (first run), the file
  // count is the entire project tree, which reads as alarming. The
  // server flags this as `initial_sync` so we can render a friendlier
  // string.
  const renderPushLine = () => {
    if (!p) return `Push current ${fromLabel} working files to kDrive`;
    if (p.initial_sync) {
      return `Push current ${fromLabel} working files to kDrive (initial sync, ${p.files_to_push} files, ~${formatBytes(p.bytes_to_push)})`;
    }
    if (p.files_to_push === 0) {
      return `Push current ${fromLabel} working files to kDrive (no changes)`;
    }
    return `Push current ${fromLabel} working files to kDrive (${p.files_to_push} file${p.files_to_push === 1 ? "" : "s"}, ~${formatBytes(p.bytes_to_push)})`;
  };
  return html`
    <div class="modal-backdrop" onClick=${onCancel}>
      <div
        class="modal switch-confirm"
        role="dialog"
        aria-modal="true"
        onClick=${(e) => e.stopPropagation()}
      >
        <header class="modal-head">
          <h2>Switch from "${fromLabel}" to "${toLabel}"?</h2>
        </header>
        <div class="modal-body">
          <p>This will:</p>
          <ul class="switch-confirm-steps">
            <li>
              ${p && p.live_conversations > 0
                ? `Snapshot ${p.live_conversations} in-progress conversation${p.live_conversations === 1 ? "" : "s"} (tagged \`live: true\`) and push to kDrive`
                : "Snapshot any in-progress conversations and push to kDrive"}
            </li>
            <li>${renderPushLine()}</li>
            <li>
              ${p && p.target_exists
                ? `Pull ${toLabel} working files from kDrive`
                : `Initialize ${toLabel} workspace from kDrive`}
            </li>
            <li>Reload UI with ${toLabel}: team identity, sessions, conversations</li>
          </ul>
          ${p && p.in_flight_agent
            ? html`<p class="switch-confirm-warn">
                Heads-up: agent <code>${p.in_flight_agent}</code> has a turn in flight.
                You'll be asked whether to wait or cancel.
              </p>`
            : null}
          ${confirm.error
            ? html`<p class="switch-confirm-err">${confirm.error}</p>`
            : null}
        </div>
        <footer class="modal-foot">
          <button class="modal-btn ghost" onClick=${onCancel}>Cancel</button>
          <button class="modal-btn primary" onClick=${onSwitch}>Switch</button>
        </footer>
      </div>
    </div>
  `;
}

// Phase 4 (§6 in-flight caveat): user clicked Switch but an agent is
// mid-turn. Spec §14 Q2 settled on hard-cancel as the recommended
// semantic; we offer Wait or "Cancel turns and switch".
function ProjectSwitchInFlightModal({ inFlight, onWait, onCancelAndSwitch }) {
  return html`
    <div class="modal-backdrop">
      <div class="modal switch-inflight" role="dialog" aria-modal="true">
        <header class="modal-head">
          <h2>${inFlight.agentId} is mid-turn</h2>
        </header>
        <div class="modal-body">
          <p>
            Switching to "${inFlight.toName}" requires every agent to be idle.
            <code>${inFlight.agentId}</code> has a turn in flight.
          </p>
          <p>
            Choose <strong>Wait</strong> to let the turn finish (you can re-click
            Switch when it's done), or <strong>Cancel turns and switch</strong> to
            hard-cancel every running agent and switch immediately. Hard-cancel
            discards the in-flight tool calls.
          </p>
        </div>
        <footer class="modal-foot">
          <button class="modal-btn ghost" onClick=${onWait}>Wait</button>
          <button class="modal-btn primary" onClick=${onCancelAndSwitch}>
            Cancel turns and switch
          </button>
        </footer>
      </div>
    </div>
  `;
}

// Phase 4 (§6): busy modal with stepper animation. Listens for
// `project_switch_step` events emitted by the activate flow + the
// terminal `project_switched` event. The state is fed by the WS
// dispatcher in App; this component renders.
// Spec §6 lists 6 sub-steps; the activate flow consolidates them into
// 4 + a `started` marker (snapshot folds into push_current; reload
// covers both context load + pane re-render). Keep the spec wording
// where possible so the modal reads the same as the doc.
const SWITCH_STEP_LABELS = {
  started: "Starting switch",
  push_current: "Pushing current project to kDrive",
  pull_new: "Pulling new project from kDrive",
  swap_pointer: "Switching active project pointer",
  reload: "Loading new project context",
};

function ProjectSwitchBusyModal({ busy, onDismiss, onRetry }) {
  const stepRows = busy.steps;
  const renderIcon = (status) => {
    if (status === "ok") return html`<span class="step-icon ok">✓</span>`;
    if (status === "running") return html`<span class="step-icon running">⟳</span>`;
    if (status === "failed" || status === "timed_out") return html`<span class="step-icon err">✗</span>`;
    return html`<span class="step-icon pending">○</span>`;
  };
  const fmtCounts = (step, detail) => {
    // Audit fix #6: surface the pre-flight live-conversation count
    // on the push_current step so the modal mirrors spec §6 wording
    // ("Snapshotting Misc live conversations (3 files)").
    const liveSuffix =
      step === "push_current" && busy.liveConversations
        ? ` · ${busy.liveConversations} live conversation${busy.liveConversations === 1 ? "" : "s"}`
        : "";
    if (!detail) return liveSuffix;
    if (detail.counts) {
      const c = detail.counts;
      // pull_new: {project: {pulled, ...}, wiki: {...}}
      if (c.project && typeof c.project === "object" && "pulled" in c.project) {
        return ` (${c.project.pulled || 0} files)${liveSuffix}`;
      }
      // push_current: {project: {pushed, ...}, wiki: {...}}
      if (c.project && typeof c.project === "object" && "pushed" in c.project) {
        return ` (${c.project.pushed || 0} pushed)${liveSuffix}`;
      }
    }
    if (detail.error) return ` — ${String(detail.error).slice(0, 100)}`;
    return liveSuffix;
  };
  return html`
    <div class="modal-backdrop">
      <div class="modal switch-busy" role="dialog" aria-modal="true">
        <header class="modal-head">
          <h2>
            ${busy.terminal
              ? busy.ok
                ? `Switched to "${busy.toName}"`
                : `Switch failed`
              : `Switching to "${busy.toName}"…`}
          </h2>
        </header>
        <div class="modal-body">
          <ol class="switch-step-list">
            ${stepRows.length === 0
              ? html`<li class="switch-step pending">
                  ${renderIcon("pending")}
                  <span class="step-label">Waiting for server…</span>
                </li>`
              : stepRows.map((s) => html`
                <li class=${"switch-step " + s.status} key=${s.step}>
                  ${renderIcon(s.status)}
                  <span class="step-label">
                    ${SWITCH_STEP_LABELS[s.step] || s.step}${fmtCounts(s.step, s.detail)}
                  </span>
                </li>
              `)}
          </ol>
          ${!busy.terminal
            ? html`<div class="switch-shimmer"></div>`
            : null}
          ${busy.terminal && !busy.ok
            ? html`<p class="switch-busy-err">
                ${busy.error || "Switch could not complete."}
                ${busy.failedStep ? html` (failed at: <code>${busy.failedStep}</code>)` : null}
              </p>`
            : null}
        </div>
        <footer class="modal-foot">
          ${busy.terminal && !busy.ok
            ? html`<button class="modal-btn ghost" onClick=${onDismiss}>Cancel and stay</button>
                   <button class="modal-btn primary" onClick=${onRetry}>Retry</button>`
            : busy.terminal
            ? html`<button class="modal-btn primary" onClick=${onDismiss}>Done</button>`
            : html`<span class="modal-foot-note">Modal cannot be dismissed during the switch.</span>`}
        </footer>
      </div>
    </div>
  `;
}

function LeftRail({ agents, openSlots, dotStates, problemSlots, projects, activeProjectId, switchingProject, onActivateProject, onCreateProject, onOpen, onStackInLast, wsConnected, envOpen, onToggleEnv, recurrenceOpen, onToggleRecurrence, onOpenSettings, paused, onTogglePause, onLayoutPreset, onCancelAll }) {
  const workingCount = agents.filter((a) => a.status === "working").length;
  const grouped = useMemo(() => {
    const coach = agents.find((a) => a.kind === "coach");
    const players = agents
      .filter((a) => a.kind === "player")
      .sort(byNumericSuffix);
    return { coach, players };
  }, [agents]);

  // Slot button. Visual language:
  //   - Inactive (never spawned, no session_id): gray number, no background.
  //   - Activated (has session): tinted background + colored number.
  //       idle    → blue
  //       working → glowing amber (pulse)
  //       error / cost_capped / cancelled → red
  //   - Pane currently open: 3px accent stripe on the left edge.
  //   - Locked: desaturated + small lock badge bottom-right.
  //   - Comms dot (top-left, only when activated): green / blue / orange
  //       per `dotStates` Map computed in App.
  const renderSlot = (a) => {
    if (!a) return null;
    const hasSession =
      Boolean(a.session_id) || Boolean(a.codex_thread_id) ||
      a.status === "working" || a.status === "waiting";
    const status = a.status || "idle";
    // Visual work-state class. `state-problem` collects three things
    // surfaced via the problemSlots Set computed in App: hard errors
    // (status === "error"), cost-cap exhaustion (derived from caps +
    // cost), and recent cancellations (last decisive event was
    // agent_cancelled). Working still wins so a freshly-resumed turn
    // doesn't read as a problem.
    let stateClass = "";
    if (hasSession) {
      if (status === "working") stateClass = "state-working";
      else if (problemSlots && problemSlots.has(a.id)) stateClass = "state-problem";
      else stateClass = "state-idle";
    }
    const isOpen = openSlots.includes(a.id);
    const dot = hasSession ? (dotStates && dotStates.get(a.id)) || "green" : "";
    // Coach is always on by design — locking Coach is a no-op
    // semantically and the dimmed/locked-badge styling just looks
    // broken on the captain. Limit it to Players.
    const showLocked = a.locked && a.kind !== "coach";
    const classes = [
      "slot",
      a.kind,
      hasSession ? "has-session" : "unused",
      stateClass,
      isOpen ? "open" : "",
      showLocked ? "locked" : "",
    ].filter(Boolean).join(" ");
    const baseTip = a.name
      ? `${a.id} — ${a.name}${a.role ? " — " + a.role : ""} (${status})`
      : `${a.id} — unassigned (${status})`;
    const dotHint = dot === "blue"
      ? " — has unread inbox"
      : dot === "orange"
      ? " — waiting for a reply"
      : "";
    const lockHint = showLocked ? " — LOCKED (Coach can't assign / message)" : "";
    const tooltip = baseTip + dotHint + lockHint + " — shift-click to stack in last column";
    // PR 6: Runtime badge. Only painted when an explicit
    // runtime_override is set so the default all-Claude deploy isn't
    // visually noisy. Claude → filled disc; Codex → filled square.
    const runtimeOverride = (a.runtime_override || "").toLowerCase();
    const runtimeBadge = runtimeOverride === "codex"
      ? "slot-runtime-codex"
      : runtimeOverride === "claude"
      ? "slot-runtime-claude"
      : "";
    return html`
      <button
        key=${a.id}
        class=${classes}
        title=${tooltip + (runtimeBadge ? ` — runtime: ${runtimeOverride}` : "")}
        onClick=${(e) => (e.shiftKey ? onStackInLast(a.id) : onOpen(a.id))}
      >
        <span class="slot-label">${slotShortLabel(a.id)}</span>
        ${dot ? html`<span class=${"slot-dot dot-" + dot}></span>` : null}
        ${runtimeBadge ? html`<span class=${"slot-runtime " + runtimeBadge} aria-hidden="true"></span>` : null}
        ${showLocked
          ? html`<span
              class="slot-lock"
              aria-hidden="true"
              dangerouslySetInnerHTML=${{ __html:
                `<svg viewBox="0 0 20 20" width="9" height="9" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="4.5" y="9" width="11" height="8" rx="1.2"/><path d="M7 9V6.5a3 3 0 0 1 6 0V9"/></svg>` }}
            ></span>`
          : null}
      </button>
    `;
  };

  return html`
    <aside class="rail">
      <span
        class=${"ws-dot rail-ws-dot " + (wsConnected ? "ok" : "")}
        title=${wsConnected ? "websocket connected" : "websocket disconnected"}
      ></span>
      <!-- Top group: agents (Coach + 10 Players). -->
      <div class="rail-group rail-agents">
        ${renderSlot(grouped.coach)}
        ${grouped.players.map(renderSlot)}
      </div>
      <!-- Bottom block — wrapped in .rail-bottom so mobile gets a
           reliable single-row layout (flex with no wrap). On desktop
           .rail-bottom takes the remaining vertical space and its
           flex-column flow restores the prior stacked layout. -->
      <div class="rail-bottom">
        <span
          class=${"ws-dot mobile-ws-dot " + (wsConnected ? "ok" : "")}
          title=${wsConnected ? "websocket connected" : "websocket disconnected"}
        ></span>
        <div class="rail-group rail-files">
          <button
            class=${"gear files-open" + (openSlots.includes("__files") ? " active" : "")}
            title="Open the file explorer pane (global + active project)"
            onClick=${() => onOpen("__files")}
          >
            <span class="files-icon" aria-hidden="true">
              <span class="files-icon-trunk"></span>
              <span class="files-icon-row files-icon-row-1"></span>
              <span class="files-icon-row files-icon-row-2"></span>
              <span class="files-icon-row files-icon-row-3"></span>
            </span>
          </button>
          <${ProjectSwitcher}
            projects=${projects}
            activeProjectId=${activeProjectId}
            switchingProject=${switchingProject}
            onActivate=${onActivateProject}
            onCreate=${onCreateProject}
          />
        </div>
        <div class="rail-group rail-controls">
          ${openSlots.length >= 2
            ? html`<button
                class="gear layout-preset"
                title="Spread: one pane per column"
                onClick=${() => onLayoutPreset && onLayoutPreset("spread")}
              ><span class="layout-icon layout-icon-spread">
                <span></span><span></span><span></span>
              </span></button>
              <button
                class="gear layout-preset"
                title="Pair stack: two panes per column (odd count → first one is solo)"
                onClick=${() => onLayoutPreset && onLayoutPreset("pairs")}
              ><span class="layout-icon layout-icon-pairs">
                <span><i></i><i></i></span><span><i></i><i></i></span><span><i></i><i></i></span>
              </span></button>`
            : null}
          ${workingCount > 0
            ? html`<button
                class="gear cancel-all"
                title=${"Cancel all " + workingCount + " running agent" + (workingCount === 1 ? "" : "s")}
                onClick=${onCancelAll}
              >⏹</button>`
            : null}
          <button
            class=${"gear pause-toggle" + (paused ? " active" : "")}
            title=${(paused
              ? "Harness is PAUSED — new agent spawns are blocked. Click to resume."
              : "Pause the harness — stops new agent spawns (in-flight turns keep running)."
            ) + " Keyboard: ⌘/Ctrl+."}
            onClick=${onTogglePause}
          >${paused ? "▶" : "❚❚"}</button>
        </div>
        <div class="rail-group rail-env">
          <button
            class=${"gear recurrence-toggle" + (recurrenceOpen ? " active" : "")}
            title=${recurrenceOpen ? "Close recurrence panel" : "Open recurrence panel — Coach tick / repeats / crons"}
            onClick=${onToggleRecurrence}
          >
            <span class="recurrence-icon" aria-hidden="true">${html`
              <svg viewBox="0 0 24 24">
                <path d="M20 8 A 9 9 0 0 0 4 11" />
                <polyline points="20 3 20 8 15 8" />
                <path d="M4 16 A 9 9 0 0 0 20 13" />
                <polyline points="4 21 4 16 9 16" />
              </svg>
            `}</span>
          </button>
          <button
            class=${"gear env-toggle" + (envOpen ? " active" : "")}
            title=${(envOpen ? "Collapse environment panel" : "Open environment panel") + " (⌘/Ctrl+B)"}
            onClick=${onToggleEnv}
          >
            <span class="env-icon-desktop">▦</span>
            <span class="env-icon-mobile">E</span>
          </button>
          <button class="gear settings-toggle" title="Settings" onClick=${onOpenSettings}>⚙</button>
        </div>
      </div>
    </aside>
  `;
}

// ------------------------------------------------------------------
// settings drawer
// ------------------------------------------------------------------

// Pick the most informative field from a /api/health check entry.
// Each subsystem uses different shape (db has 'error', claude_cli
// has 'version', webdav has 'reason' or 'cached', etc.), so we walk
// known keys in priority order.
function summarizeHealthCheck(c) {
  if (!c) return "";
  if (c.skipped) return c.reason ? `skipped — ${c.reason}` : "skipped";
  if (c.error) return String(c.error);
  if (c.version) return String(c.version);
  if (c.exit_code != null) return `exit ${c.exit_code}`;
  if (typeof c.server_count === "number") {
    // MCP external block — compose server list + tool count.
    const names = (c.servers || []).join(", ");
    return `${c.server_count} server${c.server_count === 1 ? "" : "s"}` +
      (names ? ` (${names})` : "") +
      `, ${c.allowed_tool_count || 0} tool${c.allowed_tool_count === 1 ? "" : "s"}`;
  }
  if (c.credentials_present != null) {
    // Claude auth block.
    return c.credentials_present
      ? `persisted at ${c.config_dir}`
      : `no creds at ${c.config_dir}`;
  }
  if (c.path) return String(c.path);
  if (typeof c.slot_count === "number") return `${c.slot_count} slot${c.slot_count === 1 ? "" : "s"}`;
  if (c.cached != null) return c.ok ? (c.cached ? "ok (cached)" : "ok (fresh probe)") : "probe failed";
  return c.ok ? "ok" : "not ready";
}

// Paste a .credentials.json blob so agents can authenticate to
// Claude Code without the operator having to shell into the container.
// Typical flow:
//   1. On any laptop with `claude` installed: run `claude /login`.
//   2. Open ~/.claude/.credentials.json (or the platform equivalent).
//   3. Paste it here, click Save.
//   4. Agents use the new token on their next turn.
// Dependent on CLAUDE_CONFIG_DIR being set server-side — without a
// persistent volume target, the paste would live only in container
// memory and vanish on redeploy.
function ClaudeAuthSection({ health, onRefresh }) {
  const [blob, setBlob] = useState("");
  const [status, setStatus] = useState(null); // {type: "ok"|"err", msg}
  const [saving, setSaving] = useState(false);
  const auth = health?.checks?.claude_auth || {};
  const present = auth.credentials_present === true;
  const skipped = auth.skipped === true;
  const onSave = useCallback(async () => {
    if (!blob.trim()) {
      setStatus({ type: "err", msg: "Paste the credentials JSON first." });
      return;
    }
    setSaving(true);
    setStatus(null);
    try {
      const res = await authFetch("/api/auth/claude", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ credentials_json: blob }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setStatus({
          type: "err",
          msg: data.detail || `HTTP ${res.status}`,
        });
        return;
      }
      setStatus({ type: "ok", msg: "Saved. Agents will use this on next turn." });
      setBlob("");
      try { await onRefresh?.(); } catch (_) {}
    } catch (e) {
      setStatus({ type: "err", msg: String(e) });
    } finally {
      setSaving(false);
    }
  }, [blob, onRefresh]);
  return html`<section class="drawer-section">
    <h3>Claude auth</h3>
    ${skipped
      ? html`<p class="muted" style="color: var(--err);">
          ✗ <code>CLAUDE_CONFIG_DIR</code> is not set — pasted credentials
          would be lost on redeploy. Set it to a persistent path (e.g.
          <code>/data/claude</code>) and redeploy first.
        </p>`
      : html`<p class="muted" style="margin: 0 0 6px 0;">
          <strong style=${present ? "color: var(--ok);" : "color: var(--warn);"}>
            ${present ? "✓ authenticated" : "✗ not yet set"}
          </strong>
          ${auth.config_dir
            ? html` <span class="muted">(${auth.config_dir})</span>`
            : null}
        </p>`}
    <p class="muted" style="font-size: 11px; margin: 0 0 8px 0;">
      On any machine with Claude Code installed, run <code>claude /login</code>,
      then paste the contents of <code>~/.claude/.credentials.json</code>
      below. Tokens refresh automatically from there on, and they live
      on the persistent volume — so you only do this once, not on
      every redeploy. Do it again only if you rotate credentials.
    </p>
    <textarea
      rows="6"
      placeholder='{"claudeAiOauth": {"accessToken": "...", "refreshToken": "...", ...}}'
      value=${blob}
      onInput=${(e) => setBlob(e.target.value)}
      disabled=${saving || skipped}
      style="width: 100%; font-family: ui-monospace, monospace; font-size: 11px; resize: vertical;"
    ></textarea>
    <div style="display: flex; gap: 8px; align-items: center; margin-top: 6px;">
      <button
        class="primary"
        onClick=${onSave}
        disabled=${saving || skipped || !blob.trim()}
      >${saving ? "saving…" : "Save credentials"}</button>
      ${status
        ? html`<span style=${`font-size: 11px; color: var(--${status.type === "ok" ? "ok" : "err"});`}>
            ${status.msg}
          </span>`
        : null}
    </div>
    <p class="muted" style="font-size: 11px; margin: 8px 0 0 0;">
      Alternative: shell into the container and run <code>claude /login</code>
      directly — the device-code flow lands tokens in the same file.
      Prefer an API key instead? Set <code>ANTHROPIC_API_KEY</code> in the
      Secrets store below; not all features work in API-key mode.
    </p>
  </section>`;
}

// Distinguishes three states so the operator can debug quickly:
//   - disabled: env not set (expected reason string)
//   - enabled + probe ok: ✓ + verified path on the cloud drive
//   - enabled + probe fail: ✗ with full exception text + the URL /
//     root we actually asked webdav4 to hit (the most common failure
//     mode is a misconfigured URL that silently writes nowhere the
//     human expects to look).
function WebDAVSection({ serverStatus, health, onRefresh }) {
  const [forcing, setForcing] = useState(false);
  const wd = serverStatus?.webdav;
  const probe = health?.checks?.webdav; // may be undefined until health loads
  const url = probe?.url ?? wd?.url ?? "";
  const forceProbe = useCallback(async () => {
    setForcing(true);
    try {
      await onRefresh?.();
    } finally {
      setForcing(false);
    }
  }, [onRefresh]);
  if (!wd?.enabled) {
    return html`<section class="drawer-section">
      <h3>WebDAV mirror</h3>
      <p class="muted">
        ✗ Disabled${wd?.reason ? html` (${wd.reason})` : null}.
        Set <code>HARNESS_WEBDAV_URL</code>, <code>HARNESS_WEBDAV_USER</code>,
        and <code>HARNESS_WEBDAV_PASSWORD</code> env vars and redeploy.
        The harness works fine without it — writes go to local SQLite only.
      </p>
    </section>`;
  }
  const probeOk = probe?.ok === true;
  const probeErr = probe?.error;
  return html`<section class="drawer-section">
    <h3>WebDAV mirror</h3>
    <p class="muted" style="margin: 0 0 6px 0;">
      <strong style=${probeOk ? "color: var(--ok);" : probe ? "color: var(--err);" : "color: var(--muted);"}>
        ${probeOk ? "✓ probe ok" : probe ? "✗ probe failed" : "…probing"}
      </strong>
      ${probe?.cached ? html` <span class="muted">(cached)</span>` : null}
      <button
        style="margin-left: 8px; font-size: 11px;"
        onClick=${forceProbe}
        disabled=${forcing}
        title="Refresh health — probe cache is 60s"
      >${forcing ? "probing…" : "re-probe"}</button>
    </p>
    <ul style="margin: 4px 0 8px 0; padding: 0 0 0 18px; font-size: 12px;">
      <li><strong>URL:</strong> <code>${url || "(not set)"}</code></li>
      ${probe?.probe_file
        ? html`<li><strong>Test file:</strong>
            <code>${probe.probe_file}</code>
            ${probeOk ? html` <span class="muted">(look for this on the drive)</span>` : null}
          </li>`
        : null}
    </ul>
    ${probeErr
      ? html`<pre style="margin: 4px 0; padding: 6px 8px; background: #10131a; border: 1px solid var(--err); border-radius: 3px; font-size: 11px; color: var(--err); white-space: pre-wrap;">${probe?.step ? "failed at: " + probe.step + "\n" : ""}${probeErr}</pre>`
      : null}
    ${probe?.hint
      ? html`<p class="muted" style="font-size: 11px; margin: 4px 0; font-style: italic;">${probe.hint}</p>`
      : null}
    <p class="muted" style="font-size: 11px; margin: 4px 0 0 0;">
      Memory docs mirror on update; event log every 5 min; DB snapshot every 5 min.
      If the probe is ok but the directory looks empty, agents haven't
      written anything yet — trigger a <code>coord_update_memory</code>
      or wait for the next snapshot tick.
    </p>
  </section>`;
}

// Team-wide extra-tools allowlist. One toggle set applies to every
// agent (Coach + p1..p10) on their next turn. Replaced the older
// per-agent popover checkboxes — flipping the same tool on ten
// Players was too much friction for a setting that's almost always
// uniform across the team.
function TeamToolsSection() {
  const [tools, setTools] = useState([]);
  const [available, setAvailable] = useState([]);
  const [saving, setSaving] = useState(false);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await authFetch("/api/team/tools");
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (cancelled) return;
        setTools(Array.isArray(data.tools) ? data.tools : []);
        setAvailable(Array.isArray(data.available) ? data.available : []);
      } catch (e) {
        console.error("team tools load failed", e);
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, []);
  // Functional state update so we always toggle against the *latest*
  // tools array, even if the user clicks two checkboxes fast (the
  // first click's closure would otherwise still see the pre-click
  // list and overwrite the second toggle's result). The PUT body is
  // computed inside the updater and then fired off.
  const toggle = (name) => {
    setTools((prev) => {
      const next = prev.includes(name)
        ? prev.filter((t) => t !== name)
        : [...prev, name];
      (async () => {
        setSaving(true);
        try {
          const res = await authFetch("/api/team/tools", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tools: next }),
          });
          if (!res.ok) setTools(prev); // roll back
        } catch (e) {
          console.error("team tools save failed", e);
          setTools(prev);
        } finally {
          setSaving(false);
        }
      })();
      return next;
    });
  };
  return html`<section class="drawer-section">
    <h3>Team tools</h3>
    <p class="muted" style="margin: 0 0 6px 0; font-size: 12px;">
      Extra tools beyond the role baseline. Applies to every agent
      on their next turn — off by default so web calls don't fire
      unexpectedly. The toggle is team-wide and runtime-shared:
      Claude agents get the SDK tool of the same name; Codex agents
      get the equivalent native capability (WebSearch / WebFetch
      both flip Codex's <code>web_search</code> to <code>live</code>;
      Codex has no per-URL fetch — pass URLs through web_search).
    </p>
    ${loaded
      ? available.length === 0
        ? html`<p class="muted" style="font-size: 11px;">(no extras available — update <code>_EXTRA_TOOL_WHITELIST</code> in server/main.py)</p>`
        : html`<div style="display: flex; flex-wrap: wrap; gap: 6px 14px;">
            ${available.map(
              (name) => html`<label
                key=${name}
                style="display: inline-flex; align-items: center; gap: 5px; font-size: 12px; cursor: pointer; user-select: none;"
                title=${"Grant " + name + " to the whole team. Next turn."}
              >
                <input
                  type="checkbox"
                  checked=${tools.includes(name)}
                  onChange=${() => toggle(name)}
                />
                <span>${name}</span>
              </label>`
            )}
          </div>`
      : html`<p class="muted">loading…</p>`}
  </section>`;
}

// Per-role default models. Two dropdowns (Coach / Players) with
// suggested defaults shown as inline hints. Per-pane overrides set
// via the gear popover still win — these only apply when a pane
// hasn't picked its own model.
function TeamModelsSection() {
  const [coachModel, setCoachModel] = useState("");
  const [playersModel, setPlayersModel] = useState("");
  const [coachCodexModel, setCoachCodexModel] = useState("");
  const [playersCodexModel, setPlayersCodexModel] = useState("");
  const [suggested, setSuggested] = useState({});
  const [suggestedCodex, setSuggestedCodex] = useState({});
  const [available, setAvailable] = useState([]);
  const [availableCodex, setAvailableCodex] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await authFetch("/api/team/models");
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (cancelled) return;
        setCoachModel(data.coach || "");
        setPlayersModel(data.players || "");
        setCoachCodexModel(data.coach_codex || "");
        setPlayersCodexModel(data.players_codex || "");
        setSuggested(data.suggested || {});
        setSuggestedCodex(data.suggested_codex || {});
        setAvailable(Array.isArray(data.available) ? data.available : []);
        setAvailableCodex(Array.isArray(data.available_codex) ? data.available_codex : []);
      } catch (e) {
        console.error("team models load failed", e);
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, []);
  const save = useCallback(async (coach, players, coachCodex, playersCodex) => {
    setSaving(true);
    try {
      const res = await authFetch("/api/team/models", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          coach,
          players,
          coach_codex: coachCodex,
          players_codex: playersCodex,
        }),
      });
      if (res.ok) setSavedAt(Date.now());
    } catch (e) {
      console.error("team models save failed", e);
    } finally {
      setSaving(false);
    }
  }, []);
  const onCoachChange = (v) => {
    setCoachModel(v);
    save(v, playersModel, coachCodexModel, playersCodexModel);
  };
  const onPlayersChange = (v) => {
    setPlayersModel(v);
    save(coachModel, v, coachCodexModel, playersCodexModel);
  };
  const onCoachCodexChange = (v) => {
    setCoachCodexModel(v);
    save(coachModel, playersModel, v, playersCodexModel);
  };
  const onPlayersCodexChange = (v) => {
    setPlayersCodexModel(v);
    save(coachModel, playersModel, coachCodexModel, v);
  };
  return html`<section class="drawer-section">
    <h3>Default models</h3>
    <p class="muted" style="margin: 0 0 6px 0; font-size: 12px;">
      Fallback model per role and runtime when a pane hasn't set its
      own. The gear popover on any pane still overrides. Empty = SDK
      default.
    </p>
    ${loaded
      ? html`<div style="display: grid; grid-template-columns: auto 1fr; gap: 6px 10px; align-items: center; font-size: 12px;">
          <label>Coach Claude</label>
          <div>
            <select
              value=${coachModel}
              disabled=${saving}
              onChange=${(e) => onCoachChange(e.target.value)}
            >
              <option value="">SDK default</option>
              ${available.map((m) => html`<option value=${m}>${modelLabelFor(m, "claude")}</option>`)}
            </select>
            ${suggested.coach
              ? html` <span class="muted" style="margin-left: 6px;">suggested: ${modelLabelFor(suggested.coach, "claude")}</span>`
              : null}
          </div>
          <label>Players Claude</label>
          <div>
            <select
              value=${playersModel}
              disabled=${saving}
              onChange=${(e) => onPlayersChange(e.target.value)}
            >
              <option value="">SDK default</option>
              ${available.map((m) => html`<option value=${m}>${modelLabelFor(m, "claude")}</option>`)}
            </select>
            ${suggested.players
              ? html` <span class="muted" style="margin-left: 6px;">suggested: ${modelLabelFor(suggested.players, "claude")}</span>`
              : null}
          </div>
          <label>Coach Codex</label>
          <div>
            <select
              value=${coachCodexModel}
              disabled=${saving}
              onChange=${(e) => onCoachCodexChange(e.target.value)}
            >
              <option value="">SDK default</option>
              ${availableCodex.map((m) => html`<option value=${m}>${modelLabelFor(m, "codex")}</option>`)}
            </select>
            ${suggestedCodex.coach
              ? html` <span class="muted" style="margin-left: 6px;">suggested: ${modelLabelFor(suggestedCodex.coach, "codex")}</span>`
              : null}
          </div>
          <label>Players Codex</label>
          <div>
            <select
              value=${playersCodexModel}
              disabled=${saving}
              onChange=${(e) => onPlayersCodexChange(e.target.value)}
            >
              <option value="">SDK default</option>
              ${availableCodex.map((m) => html`<option value=${m}>${modelLabelFor(m, "codex")}</option>`)}
            </select>
            ${suggestedCodex.players
              ? html` <span class="muted" style="margin-left: 6px;">suggested: ${modelLabelFor(suggestedCodex.players, "codex")}</span>`
              : null}
          </div>
        </div>
        ${savedAt
          ? html`<p class="muted" style="font-size: 11px; margin: 6px 0 0 0;">saved · takes effect on next turn</p>`
          : null}`
      : html`<p class="muted">loading…</p>`}
  </section>`;
}

// Per-role default runtimes (PR 6 / audit-item-17). Mirrors
// TeamModelsSection. Resolution order at spawn time: per-slot
// runtime_override → role default here → 'claude'. Codex radio is
// disabled when HARNESS_CODEX_ENABLED is unset on the server.
function TeamRuntimesSection() {
  const [coach, setCoach] = useState("");
  const [players, setPlayers] = useState("");
  const [codexEnabled, setCodexEnabled] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await authFetch("/api/team/runtimes");
        if (!res.ok || cancelled) return;
        const d = await res.json();
        if (cancelled) return;
        setCoach(d.coach || "");
        setPlayers(d.players || "");
        setCodexEnabled(!!d.codex_enabled);
      } catch (e) {
        console.error("team runtimes load failed", e);
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const save = useCallback(async (nextCoach, nextPlayers) => {
    setSaving(true);
    setMsg(null);
    try {
      const res = await authFetch("/api/team/runtimes", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ coach: nextCoach, players: nextPlayers }),
      });
      if (res.ok) {
        setMsg({ kind: "ok", text: "saved · takes effect on next turn" });
      } else {
        let detail;
        try { detail = (await res.json()).detail; } catch (_) { detail = "HTTP " + res.status; }
        setMsg({ kind: "err", text: typeof detail === "string" ? detail : JSON.stringify(detail) });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally {
      setSaving(false);
    }
  }, []);

  const radioRow = (role, current, onChange) => html`
    <div>
      <label style="margin-right: 10px;">
        <input type="radio" name=${"runtime-" + role} value=""
          checked=${current === ""} disabled=${saving}
          onChange=${() => onChange("")} /> default (claude)
      </label>
      <label style="margin-right: 10px;">
        <input type="radio" name=${"runtime-" + role} value="claude"
          checked=${current === "claude"} disabled=${saving}
          onChange=${() => onChange("claude")} /> Claude
      </label>
      <label>
        <input type="radio" name=${"runtime-" + role} value="codex"
          checked=${current === "codex"} disabled=${saving || !codexEnabled}
          title=${codexEnabled ? "" : "set HARNESS_CODEX_ENABLED on the server to enable"}
          onChange=${() => onChange("codex")} /> Codex
      </label>
    </div>`;

  return html`<section class="drawer-section">
    <h3>Default runtime per role</h3>
    <p class="muted" style="margin: 0 0 6px 0; font-size: 12px;">
      Fallback runtime per role when a slot hasn't set its own
      override. The gear popover on any pane still overrides. Mid-turn
      changes are rejected — cancel the turn first.
      ${codexEnabled
        ? null
        : html` <span style="color: var(--warn);">Codex disabled on the server (set HARNESS_CODEX_ENABLED).</span>`}
    </p>
    ${loaded
      ? html`<div style="display: grid; grid-template-columns: auto 1fr; gap: 6px 10px; align-items: center; font-size: 12px;">
          <label>Coach</label>
          ${radioRow("coach", coach, (v) => { setCoach(v); save(v, players); })}
          <label>Players</label>
          ${radioRow("players", players, (v) => { setPlayers(v); save(coach, v); })}
        </div>
        ${msg
          ? html`<p style="font-size: 11px; margin: 6px 0 0 0; color: ${msg.kind === "ok" ? "var(--ok)" : "var(--err)"};">
              ${msg.text}
            </p>`
          : null}`
      : html`<p class="muted">loading…</p>`}
  </section>`;
}

// Project repo configuration. DB-backed (team_config) with env
// fallback. Edits take effect on the next container restart — live
// Phase 8 (PROJECTS_SPEC.md §12, §13): Projects section. Lists every
// project (active marked, archived dimmed) with inline edit (name /
// description / repo_url) + archive toggle + delete button (Misc
// disabled) + Provision now button (per-project — calls the §11
// per-project repo provision endpoint). Expand a card to view that
// project's `agent_project_roles` (read-only — name/role/brief per
// slot are edited via Coach's `coord_set_player_role` for
// name/role, or each pane's settings popover for brief).
function ProjectsSection() {
  const [data, setData] = useState({ projects: [], active: null });
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [expanded, setExpanded] = useState({});  // {projectId: bool}
  const [rolesByProject, setRolesByProject] = useState({});  // {projectId: rolesArr}
  const [editing, setEditing] = useState({});  // {projectId: {name,description,repo_url}}
  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState({ slug: "", name: "", description: "", repo_url: "" });
  const [slugManuallyEdited, setSlugManuallyEdited] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await authFetch("/api/projects");
      if (!res.ok) return;
      const json = await res.json();
      setData(json);
    } catch (e) {
      console.error("projects load failed", e);
    } finally {
      setLoaded(true);
    }
  }, []);
  useEffect(() => { load(); }, [load]);

  const fetchRoles = useCallback(async (projectId) => {
    try {
      const res = await authFetch(`/api/projects/${encodeURIComponent(projectId)}/roles`);
      if (!res.ok) return;
      const json = await res.json();
      setRolesByProject((m) => ({ ...m, [projectId]: json.roles || [] }));
    } catch (e) {
      console.error("roles fetch failed", e);
    }
  }, []);

  const toggleExpand = (projectId) => {
    setExpanded((m) => {
      const next = { ...m, [projectId]: !m[projectId] };
      if (next[projectId] && !rolesByProject[projectId]) fetchRoles(projectId);
      return next;
    });
  };

  const startEdit = (p) => setEditing((m) => ({
    ...m,
    [p.id]: { name: p.name, description: p.description || "", repo_url: p.repo_url || "" },
  }));
  const cancelEdit = (id) => setEditing((m) => { const n = { ...m }; delete n[id]; return n; });
  const updateEdit = (id, field, value) => setEditing((m) => ({
    ...m,
    [id]: { ...m[id], [field]: value },
  }));

  const saveEdit = async (id) => {
    const draft = editing[id];
    if (!draft || busy) return;
    setBusy(true); setMsg(null);
    try {
      const body = {};
      if (draft.name !== undefined) body.name = draft.name;
      if (draft.description !== undefined) body.description = draft.description;
      if (draft.repo_url !== undefined) body.repo_url = draft.repo_url;
      const res = await authFetch(`/api/projects/${encodeURIComponent(id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        setMsg({ kind: "ok", text: `Saved ${id}.` });
        cancelEdit(id);
        await load();
      } else {
        const t = await res.text().catch(() => "");
        setMsg({ kind: "err", text: `HTTP ${res.status}: ${t || "save failed"}` });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally { setBusy(false); }
  };

  const toggleArchive = async (p) => {
    if (busy) return;
    if (p.is_active && !p.archived) {
      setMsg({ kind: "err", text: "Switch away from this project before archiving." });
      return;
    }
    setBusy(true); setMsg(null);
    try {
      const res = await authFetch(`/api/projects/${encodeURIComponent(p.id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ archived: !p.archived }),
      });
      if (res.ok) {
        setMsg({ kind: "ok", text: `${p.archived ? "Un-archived" : "Archived"} ${p.id}.` });
        await load();
      } else {
        const t = await res.text().catch(() => "");
        setMsg({ kind: "err", text: `HTTP ${res.status}: ${t}` });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally { setBusy(false); }
  };

  const deleteProject = async (p) => {
    if (busy || p.id === "misc") return;
    const confirmed = window.confirm(
      `Delete project "${p.name}" (${p.id})?\n\n`
      + `This wipes the entire /data/projects/${p.id}/ tree, drops all `
      + `tasks, messages, memory, decisions, conversations, events, and `
      + `agent_project_roles for this project, and removes the kDrive `
      + `mirror. This cannot be undone.`
    );
    if (!confirmed) return;
    setBusy(true); setMsg(null);
    try {
      const res = await authFetch(`/api/projects/${encodeURIComponent(p.id)}`, {
        method: "DELETE",
      });
      if (res.ok) {
        setMsg({ kind: "ok", text: `Deleted ${p.id}.` });
        await load();
      } else {
        const t = await res.text().catch(() => "");
        setMsg({ kind: "err", text: `HTTP ${res.status}: ${t}` });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally { setBusy(false); }
  };

  const provision = async (p) => {
    if (busy) return;
    if (!p.repo_url) {
      setMsg({ kind: "err", text: `${p.id} has no repo URL set — save one first.` });
      return;
    }
    setBusy(true); setMsg(null);
    try {
      const res = await authFetch(`/api/projects/${encodeURIComponent(p.id)}/repo/provision`, {
        method: "POST",
      });
      if (res.ok) {
        setMsg({ kind: "ok", text: `Provisioned ${p.id} repo + worktrees.` });
      } else {
        const t = await res.text().catch(() => "");
        setMsg({ kind: "err", text: `HTTP ${res.status}: ${t}` });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally { setBusy(false); }
  };

  // Auto-derive slug from name (lowercase, dashes, drop bad chars).
  // §14 Q1 resolution: auto-derive with override.
  const deriveSlug = (name) => (name || "")
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 48);

  // Mirror server-side validators in projects_api.py so the user sees
  // the failure live instead of after a round-trip 400. Audit polish
  // from PROJECTS_SPEC.md §14 Q1.
  const SLUG_RE = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
  const RESERVED_SLUGS = new Set([
    "skills", "wiki", "mcp", "projects", "snapshots", "harness", "data", "claude",
  ]);
  const validateSlugLive = (slug) => {
    const s = (slug || "").trim();
    if (!s) return null;
    if (s.length < 2 || s.length > 48) return "Slug must be 2–48 characters.";
    if (!SLUG_RE.test(s)) return "Slug must be lowercase letters/digits/dashes; start with a letter; no leading/trailing/consecutive dashes.";
    if (RESERVED_SLUGS.has(s)) return `Slug "${s}" is reserved (collides with a global folder).`;
    return null;
  };
  const slugError = validateSlugLive(createForm.slug);

  const updateCreateName = (name) => {
    setCreateForm((f) => {
      const next = { ...f, name };
      if (!slugManuallyEdited) next.slug = deriveSlug(name);
      return next;
    });
  };

  const submitCreate = async () => {
    if (busy) return;
    const slug = (createForm.slug || "").trim();
    const name = (createForm.name || "").trim();
    if (!slug || !name) {
      setMsg({ kind: "err", text: "Slug and name are both required." });
      return;
    }
    const err = validateSlugLive(slug);
    if (err) {
      setMsg({ kind: "err", text: err });
      return;
    }
    setBusy(true); setMsg(null);
    try {
      const body = { slug, name };
      const desc = (createForm.description || "").trim();
      if (desc) body.description = desc;
      const repo = (createForm.repo_url || "").trim();
      if (repo) body.repo_url = repo;
      const res = await authFetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        setMsg({ kind: "ok", text: `Created ${slug}.` });
        setCreateForm({ slug: "", name: "", description: "", repo_url: "" });
        setSlugManuallyEdited(false);
        setShowCreate(false);
        await load();
      } else {
        const t = await res.text().catch(() => "");
        setMsg({ kind: "err", text: `HTTP ${res.status}: ${t}` });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally { setBusy(false); }
  };

  const projects = Array.isArray(data.projects) ? data.projects : [];

  return html`<section class="drawer-section">
    <h3>
      Projects
      <button
        class="drawer-refresh"
        onClick=${load}
        title="Refresh project list"
      >↻</button>
    </h3>
    <p class="muted" style="margin: 0 0 8px 0; font-size: 12px;">
      One row per project. Active is marked ✓; archived is dimmed (not
      visible in the LeftRail switcher). Slug is the primary key — it
      cannot be edited (see PROJECTS_SPEC.md §2). Misc is the fallback
      project and cannot be deleted.
    </p>
    ${msg
      ? html`<div style=${"font-size: 11px; margin: 0 0 8px; padding: 4px 8px; border-radius: 3px; "
            + (msg.kind === "ok"
              ? "color: var(--ok); border: 1px solid var(--ok); background: rgba(63,185,80,0.08);"
              : "color: var(--err); border: 1px solid var(--err); background: rgba(248,81,73,0.08); white-space: pre-wrap;")}>${msg.text}</div>`
      : null}

    ${loaded
      ? html`<div style="display: flex; flex-direction: column; gap: 6px;">
          ${projects.map((p) => {
            const isEditing = !!editing[p.id];
            const isExpanded = !!expanded[p.id];
            const roles = rolesByProject[p.id] || [];
            return html`<div
              key=${p.id}
              style=${"border: 1px solid var(--border); border-radius: 4px; padding: 8px; "
                + (p.archived ? "opacity: 0.6;" : "")
                + (p.is_active ? " border-color: var(--accent);" : "")}
            >
              <div style="display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
                <strong style="font-size: 13px;">
                  ${p.is_active ? "✓ " : ""}${p.name}
                </strong>
                <code style="font-size: 11px; color: var(--muted);">${p.id}</code>
                ${p.archived ? html`<span style="font-size: 10px; color: var(--warn); border: 1px solid var(--warn); padding: 1px 5px; border-radius: 2px;">ARCHIVED</span>` : null}
                <span style="flex: 1 1 auto;"></span>
                <button
                  onClick=${() => toggleExpand(p.id)}
                  title="Show team identity for this project"
                  style="font-size: 11px;"
                >${isExpanded ? "▾ team" : "▸ team"}</button>
              </div>

              ${isEditing
                ? html`<div style="margin-top: 6px; display: grid; gap: 4px;">
                    <input
                      placeholder="display name"
                      value=${editing[p.id].name}
                      onInput=${(e) => updateEdit(p.id, "name", e.target.value)}
                      style="background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 4px 8px; font-size: 12px;"
                    />
                    <input
                      placeholder="description (optional)"
                      value=${editing[p.id].description}
                      onInput=${(e) => updateEdit(p.id, "description", e.target.value)}
                      style="background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 4px 8px; font-size: 12px;"
                    />
                    <input
                      placeholder="repo URL (optional)"
                      value=${editing[p.id].repo_url}
                      onInput=${(e) => updateEdit(p.id, "repo_url", e.target.value)}
                      style="background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 4px 8px; font-size: 12px; font-family: ui-monospace, monospace;"
                    />
                    <div style="display: flex; gap: 6px;">
                      <button class="primary" disabled=${busy} onClick=${() => saveEdit(p.id)}>save</button>
                      <button disabled=${busy} onClick=${() => cancelEdit(p.id)}>cancel</button>
                    </div>
                  </div>`
                : html`<div style="margin-top: 4px; font-size: 11px; color: var(--muted);">
                    ${p.description ? html`<div>${p.description}</div>` : null}
                    <div>repo: ${p.repo_url || html`<em>(unset)</em>`}</div>
                    <div>created: ${p.created_at || "?"}</div>
                  </div>
                  <div style="margin-top: 6px; display: flex; gap: 4px; flex-wrap: wrap;">
                    <button disabled=${busy} onClick=${() => startEdit(p)} style="font-size: 11px;">edit</button>
                    <button
                      disabled=${busy || !p.repo_url || p.archived}
                      onClick=${() => provision(p)}
                      title=${p.archived
                        ? "Project is archived — un-archive before provisioning"
                        : !p.repo_url
                        ? "Set a repo URL first"
                        : "Clone + create worktrees for this project"}
                      style="font-size: 11px;"
                    >provision now</button>
                    <button
                      disabled=${busy || (p.is_active && !p.archived)}
                      onClick=${() => toggleArchive(p)}
                      title=${p.archived ? "Un-archive (show in switcher)" : "Archive (hide from switcher; not deleted)"}
                      style="font-size: 11px;"
                    >${p.archived ? "un-archive" : "archive"}</button>
                    <button
                      disabled=${busy || p.id === "misc"}
                      onClick=${() => deleteProject(p)}
                      title=${p.id === "misc" ? "Misc cannot be deleted" : "Delete project + on-disk tree + kDrive mirror"}
                      style=${"font-size: 11px;" + (p.id !== "misc" ? " color: var(--err);" : "")}
                    >delete</button>
                  </div>`}

              ${isExpanded
                ? html`<div style="margin-top: 8px; padding-top: 6px; border-top: 1px dashed var(--border);">
                    <div style="font-size: 11px; color: var(--muted); margin-bottom: 4px;">
                      Read-only. Edit name/role via Coach (<code>coord_set_player_role</code>);
                      brief via the slot's pane settings popover.
                    </div>
                    ${roles.length === 0
                      ? html`<div class="muted" style="font-size: 11px;">No agent_project_roles rows yet.</div>`
                      : html`<table style="width: 100%; font-size: 11px; border-collapse: collapse;">
                          <thead>
                            <tr style="text-align: left; color: var(--muted);">
                              <th style="padding: 2px 4px;">slot</th>
                              <th style="padding: 2px 4px;">name</th>
                              <th style="padding: 2px 4px;">role</th>
                              <th style="padding: 2px 4px;">brief</th>
                            </tr>
                          </thead>
                          <tbody>
                            ${roles.map((r) => html`<tr key=${r.slot} style="border-top: 1px solid var(--border);">
                              <td style="padding: 2px 4px;"><code>${r.slot}</code></td>
                              <td style="padding: 2px 4px;">${r.name || html`<em>—</em>`}</td>
                              <td style="padding: 2px 4px;">${r.role || html`<em>—</em>`}</td>
                              <td style="padding: 2px 4px; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title=${r.brief || ""}>${r.brief ? r.brief.slice(0, 80) : html`<em>—</em>`}</td>
                            </tr>`)}
                          </tbody>
                        </table>`}
                  </div>`
                : null}
            </div>`;
          })}
        </div>`
      : html`<p class="muted">loading…</p>`}

    <div style="margin-top: 8px;">
      ${showCreate
        ? html`<div style="border: 1px solid var(--border); border-radius: 4px; padding: 8px; display: grid; gap: 4px;">
            <strong style="font-size: 12px;">New project</strong>
            <input
              placeholder="display name"
              value=${createForm.name}
              onInput=${(e) => updateCreateName(e.target.value)}
              style="background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 4px 8px; font-size: 12px;"
            />
            <input
              placeholder="slug (auto-derived from name)"
              value=${createForm.slug}
              onInput=${(e) => { setCreateForm((f) => ({ ...f, slug: e.target.value })); setSlugManuallyEdited(true); }}
              style=${"background: var(--bg); color: var(--fg); border: 1px solid "
                + (slugError ? "var(--err)" : "var(--border)")
                + "; border-radius: 3px; padding: 4px 8px; font-size: 12px; font-family: ui-monospace, monospace;"}
            />
            ${slugError
              ? html`<div style="font-size: 10px; color: var(--err); margin-top: -2px;">${slugError}</div>`
              : null}
            <input
              placeholder="description (optional)"
              value=${createForm.description}
              onInput=${(e) => setCreateForm((f) => ({ ...f, description: e.target.value }))}
              style="background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 4px 8px; font-size: 12px;"
            />
            <input
              placeholder="repo URL (optional, e.g. https://\${GITHUB_TOKEN}@github.com/you/repo.git)"
              value=${createForm.repo_url}
              onInput=${(e) => setCreateForm((f) => ({ ...f, repo_url: e.target.value }))}
              style="background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 4px 8px; font-size: 12px; font-family: ui-monospace, monospace;"
            />
            <div style="display: flex; gap: 6px;">
              <button
                class="primary"
                disabled=${busy || Boolean(slugError) || !createForm.slug.trim() || !createForm.name.trim()}
                onClick=${submitCreate}
              >create</button>
              <button disabled=${busy} onClick=${() => { setShowCreate(false); setSlugManuallyEdited(false); }}>cancel</button>
            </div>
          </div>`
        : html`<button onClick=${() => setShowCreate(true)}>+ new project</button>`}
    </div>
  </section>`;
}


// worktrees keep their old `git remote`, so changing the URL
// mid-session doesn't affect in-flight Players.
function TeamRepoSection() {
  const [data, setData] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [repoDraft, setRepoDraft] = useState("");
  const [branchDraft, setBranchDraft] = useState("");
  const [allowSecrets, setAllowSecrets] = useState(false);
  const [saving, setSaving] = useState(false);
  const [provisioning, setProvisioning] = useState(false);
  const [msg, setMsg] = useState(null);

  const reload = useCallback(async () => {
    try {
      const res = await authFetch("/api/team/repo");
      if (res.ok) {
        const d = await res.json();
        setData(d);
        setRepoDraft(d.repo || "");
        setBranchDraft(d.branch && d.branch !== "main" ? d.branch : "");
      }
    } catch (e) {
      console.error("repo load failed", e);
    } finally {
      setLoaded(true);
    }
  }, []);
  useEffect(() => { reload(); }, [reload]);

  const save = useCallback(async () => {
    setSaving(true);
    setMsg(null);
    try {
      const res = await authFetch("/api/team/repo", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          repo: repoDraft,
          branch: branchDraft,
          allow_secrets: allowSecrets,
        }),
      });
      if (res.ok) {
        const d = await res.json();
        setMsg({
          kind: "ok",
          text: "saved · click 'provision now' to apply" +
            ((d.secret_warnings || []).length
              ? "  (kept raw secret despite warning: " + d.secret_warnings.join(", ") + ")"
              : ""),
        });
        setAllowSecrets(false);
        await reload();
      } else {
        let detail;
        try {
          const d = await res.json();
          detail = typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail);
        } catch (_) {
          detail = "HTTP " + res.status;
        }
        setMsg({ kind: "err", text: detail });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally {
      setSaving(false);
    }
  }, [repoDraft, branchDraft, allowSecrets, reload]);

  // Clone + worktree-add live so the saved repo takes effect without
  // a container restart. Idempotent — existing .git worktrees are
  // untouched. Can take 10–60s for a fresh first clone; button stays
  // disabled while the request is in-flight.
  const provision = useCallback(async () => {
    setProvisioning(true);
    setMsg(null);
    try {
      const res = await authFetch("/api/team/repo/provision", {
        method: "POST",
      });
      if (res.ok) {
        const d = await res.json().catch(() => ({}));
        const st = d.status || {};
        if (st.configured === false) {
          setMsg({ kind: "err", text: "No repo set. Save a URL first, then provision." });
        } else if (st.error) {
          setMsg({ kind: "err", text: "clone failed: " + st.error });
        } else {
          const slots = st.slots || {};
          const created = Object.values(slots).filter((s) => s && s.status === "created").length;
          const already = Object.values(slots).filter((s) => s && s.status === "already-present").length;
          const failed = Object.values(slots).filter((s) => s && !s.ok).length;
          setMsg({
            kind: failed ? "err" : "ok",
            text: `provisioned · ${created} new worktree${created === 1 ? "" : "s"}, ${already} already present${failed ? `, ${failed} failed` : ""}`,
          });
        }
      } else {
        let detail;
        try {
          const d = await res.json();
          detail = typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail);
        } catch (_) {
          detail = "HTTP " + res.status;
        }
        setMsg({ kind: "err", text: detail });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally {
      setProvisioning(false);
    }
  }, []);

  return html`<section class="drawer-section">
    <h3>Project repo</h3>
    <p class="muted" style="margin: 0 0 6px 0; font-size: 12px;">
      The GitHub (or any git) URL the team clones into per-Player
      worktrees. Use a <code>\${VAR}</code> placeholder for your
      PAT so the token stays in the Zeabur env, not in the DB —
      e.g. <code>https://\${GITHUB_TOKEN}@github.com/you/repo.git</code>.
      After saving, hit <em>provision now</em> to clone + create
      worktrees live (no restart needed).
    </p>
    ${loaded && data
      ? html`<div style="font-size: 11px; color: var(--muted); margin-bottom: 6px;">
          <div>current: ${data.repo_masked || "(unset)"} <span class="muted">· source: ${data.repo_source}</span></div>
          <div>branch: ${data.branch} <span class="muted">· source: ${data.branch_source}</span></div>
          ${data.env_repo_set && data.repo_source === "db"
            ? html`<div style="color: var(--warn);">⚠ HARNESS_PROJECT_REPO env is also set — DB value wins</div>`
            : null}
        </div>`
      : null}
    <input
      placeholder="https://\${GITHUB_TOKEN}@github.com/you/repo.git"
      value=${repoDraft}
      onInput=${(e) => setRepoDraft(e.target.value)}
      style="width: 100%; background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 4px 8px; font-size: 12px; font-family: ui-monospace, monospace; margin-bottom: 4px;"
    />
    <input
      placeholder="branch (default: main)"
      value=${branchDraft}
      onInput=${(e) => setBranchDraft(e.target.value)}
      style="width: 100%; background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 4px 8px; font-size: 12px; margin-bottom: 4px;"
    />
    <div style="display: flex; gap: 10px; align-items: center;">
      <label style="font-size: 11px; color: var(--muted);">
        <input
          type="checkbox"
          checked=${allowSecrets}
          onChange=${(e) => setAllowSecrets(e.target.checked)}
        />
        save even if raw secret detected
      </label>
      <button
        class="primary"
        disabled=${saving || provisioning}
        onClick=${save}
      >${saving ? "saving…" : "save"}</button>
      <button
        type="button"
        disabled=${saving || provisioning || !(data && data.repo)}
        onClick=${provision}
        title="Clone + create worktrees now, without a restart. Idempotent."
      >${provisioning ? "provisioning…" : "provision now"}</button>
    </div>
    ${msg
      ? html`<div style=${"font-size: 11px; margin: 6px 0 0; padding: 4px 8px; border-radius: 3px; " + (msg.kind === "ok"
            ? "color: var(--ok); border: 1px solid var(--ok); background: rgba(63,185,80,0.08);"
            : "color: var(--err); border: 1px solid var(--err); background: rgba(248,81,73,0.08); white-space: pre-wrap;")}>${msg.text}</div>`
      : null}
  </section>`;
}

// Telegram bridge configuration. Token + chat-id whitelist live in the
// encrypted secrets store (Fernet via HARNESS_SECRETS_KEY). Save here
// triggers a live bridge reload — no redeploy needed. The token is
// never returned by the API; the field stays empty unless you want to
// rotate it. Chat IDs are visible since they're a whitelist, not a
// credential.
function TeamTelegramSection() {
  const [data, setData] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [tokenDraft, setTokenDraft] = useState("");
  const [chatIdsDraft, setChatIdsDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [msg, setMsg] = useState(null);

  const reload = useCallback(async () => {
    try {
      const res = await authFetch("/api/team/telegram");
      if (res.ok) {
        const d = await res.json();
        setData(d);
        // Pre-fill chat IDs only — the token is write-only.
        setChatIdsDraft((d.chat_ids || []).join(", "));
      }
    } catch (e) {
      console.error("telegram load failed", e);
    } finally {
      setLoaded(true);
    }
  }, []);
  useEffect(() => { reload(); }, [reload]);

  const save = useCallback(async () => {
    setSaving(true);
    setMsg(null);
    try {
      const body = {};
      if (tokenDraft.trim()) body.token = tokenDraft.trim();
      if (chatIdsDraft !== ((data && data.chat_ids) || []).join(", ")) {
        body.chat_ids = chatIdsDraft;
      }
      if (Object.keys(body).length === 0) {
        setMsg({ kind: "ok", text: "no changes" });
        setSaving(false);
        return;
      }
      const res = await authFetch("/api/team/telegram", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        const d = await res.json();
        setMsg({
          kind: "ok",
          text: d.bridge_running
            ? "saved · bridge running"
            : "saved · bridge inactive (token or chat IDs missing)",
        });
        setTokenDraft("");  // clear write-only field
        await reload();
      } else {
        let detail;
        try {
          const d = await res.json();
          detail = typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail);
        } catch (_) {
          detail = "HTTP " + res.status;
        }
        setMsg({ kind: "err", text: detail });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally {
      setSaving(false);
    }
  }, [tokenDraft, chatIdsDraft, data, reload]);

  const clearAll = useCallback(async () => {
    if (!confirm("Wipe Telegram token + chat IDs and disable the bridge?\n\nThe disabled flag overrides env-var fallback so the bridge stays off until you Save new config.")) return;
    setClearing(true);
    setMsg(null);
    try {
      const res = await authFetch("/api/team/telegram", { method: "DELETE" });
      if (res.ok) {
        setMsg({ kind: "ok", text: "cleared · bridge disabled" });
        setTokenDraft("");
        setChatIdsDraft("");
        await reload();
      } else {
        let detail;
        try { detail = (await res.json()).detail; } catch (_) { detail = "HTTP " + res.status; }
        setMsg({ kind: "err", text: detail });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally {
      setClearing(false);
    }
  }, [reload]);

  const keyOk = data && data.secrets_status && data.secrets_status.ok;
  const keyReason = data && data.secrets_status && data.secrets_status.reason;

  return html`<section class="drawer-section">
    <h3>Telegram bridge</h3>
    <p class="muted" style="margin: 0 0 6px 0; font-size: 12px;">
      Talk to Coach from your phone. Create a bot via @BotFather, paste
      the token, then add your numeric Telegram chat ID(s) to the
      whitelist (comma-separated). Anyone whose chat ID isn't listed
      is silently ignored. Saves are encrypted at rest with
      <code>HARNESS_SECRETS_KEY</code>; changes apply live (no redeploy).
    </p>
    ${loaded && data && !keyOk
      ? html`<div style="font-size: 11px; color: var(--err); border: 1px solid var(--err); background: rgba(248,81,73,0.08); padding: 4px 8px; border-radius: 3px; margin-bottom: 6px;">
          secrets store unavailable: ${keyReason || "unknown"}
        </div>`
      : null}
    ${loaded && data
      ? html`<div style="font-size: 11px; color: var(--muted); margin-bottom: 6px;">
          <div>
            status:
            ${data.bridge_running
              ? html`<span style="color: var(--ok);">● running</span>`
              : data.disabled
                ? html`<span style="color: var(--warn);">○ disabled (cleared)</span>`
                : html`<span style="color: var(--muted);">○ inactive</span>`}
            <span class="muted"> · token: ${data.token_set ? `set (${data.token_source})` : "unset"}</span>
            <span class="muted"> · chats: ${(data.chat_ids || []).length} (${data.chat_ids_source})</span>
          </div>
          ${data.env_token_set && data.token_source === "db"
            ? html`<div style="color: var(--warn);">⚠ TELEGRAM_BOT_TOKEN env is also set — DB value wins</div>`
            : null}
          ${data.disabled
            ? html`<div style="color: var(--warn);">flag <code>telegram_disabled</code> is set — Save new config to re-enable</div>`
            : null}
        </div>`
      : null}
    <input
      type="password"
      autocomplete="new-password"
      placeholder="bot token (leave blank to keep existing)"
      value=${tokenDraft}
      onInput=${(e) => setTokenDraft(e.target.value)}
      disabled=${!keyOk}
      style="width: 100%; background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 4px 8px; font-size: 12px; font-family: ui-monospace, monospace; margin-bottom: 4px;"
    />
    <input
      placeholder="chat IDs (comma-separated integers)"
      value=${chatIdsDraft}
      onInput=${(e) => setChatIdsDraft(e.target.value)}
      disabled=${!keyOk}
      style="width: 100%; background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 4px 8px; font-size: 12px; font-family: ui-monospace, monospace; margin-bottom: 4px;"
    />
    <div style="display: flex; gap: 6px; align-items: center;">
      <button
        type="button"
        class="primary"
        disabled=${!keyOk || saving || clearing}
        onClick=${save}
      >${saving ? "saving…" : "save"}</button>
      <button
        type="button"
        disabled=${saving || clearing || !data || (!data.token_set && (data.chat_ids || []).length === 0 && !data.disabled)}
        onClick=${clearAll}
        title="Wipe both secrets and set the disabled flag (overrides env)."
      >${clearing ? "clearing…" : "clear"}</button>
    </div>
    ${msg
      ? html`<div style=${"font-size: 11px; margin: 6px 0 0; padding: 4px 8px; border-radius: 3px; " + (msg.kind === "ok"
            ? "color: var(--ok); border: 1px solid var(--ok); background: rgba(63,185,80,0.08);"
            : "color: var(--err); border: 1px solid var(--err); background: rgba(248,81,73,0.08); white-space: pre-wrap;")}>${msg.text}</div>`
      : null}
  </section>`;
}

// Codex auth (PR 5+). Two sources: ChatGPT session (file at
// $CODEX_HOME/auth.json — set inside container by `codex login`) and
// OPENAI_API_KEY fallback (encrypted in `secrets`, settable here).
function TeamCodexSection() {
  const [data, setData] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [keyDraft, setKeyDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [testing, setTesting] = useState(false);
  const [msg, setMsg] = useState(null);

  const reload = useCallback(async () => {
    try {
      const res = await authFetch("/api/team/codex");
      if (res.ok) setData(await res.json());
    } catch (e) {
      console.error("codex auth load failed", e);
    } finally {
      setLoaded(true);
    }
  }, []);
  useEffect(() => { reload(); }, [reload]);

  const save = useCallback(async () => {
    if (!keyDraft.trim()) {
      setMsg({ kind: "err", text: "paste an API key first" });
      return;
    }
    setSaving(true);
    setMsg(null);
    try {
      const res = await authFetch("/api/team/codex", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: keyDraft.trim() }),
      });
      if (res.ok) {
        setMsg({ kind: "ok", text: "saved" });
        setKeyDraft("");
        await reload();
      } else {
        let detail;
        try { detail = (await res.json()).detail; } catch (_) { detail = "HTTP " + res.status; }
        setMsg({ kind: "err", text: typeof detail === "string" ? detail : JSON.stringify(detail) });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally {
      setSaving(false);
    }
  }, [keyDraft, reload]);

  const clearKey = useCallback(async () => {
    if (!confirm("Wipe saved OpenAI API key? ChatGPT session (filesystem) is untouched.")) return;
    setClearing(true);
    setMsg(null);
    try {
      const res = await authFetch("/api/team/codex", { method: "DELETE" });
      if (res.ok) {
        setMsg({ kind: "ok", text: "cleared" });
        await reload();
      } else {
        let detail;
        try { detail = (await res.json()).detail; } catch (_) { detail = "HTTP " + res.status; }
        setMsg({ kind: "err", text: detail });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally {
      setClearing(false);
    }
  }, [reload]);

  const testKey = useCallback(async () => {
    setTesting(true);
    setMsg(null);
    try {
      const res = await authFetch("/api/team/codex/test", { method: "POST" });
      if (res.ok) {
        const d = await res.json();
        const sample = (d.sample_models || []).slice(0, 3).join(", ");
        setMsg({
          kind: "ok",
          text: sample ? `OK · models: ${sample}` : "OK · key valid",
        });
      } else {
        let detail;
        try { detail = (await res.json()).detail; } catch (_) { detail = "HTTP " + res.status; }
        setMsg({ kind: "err", text: detail });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally {
      setTesting(false);
    }
  }, []);

  const keyOk = data && data.secrets_status && data.secrets_status.ok;
  const keyReason = data && data.secrets_status && data.secrets_status.reason;

  return html`<section class="drawer-section">
    <h3>Codex auth</h3>
    <p class="muted" style="margin: 0 0 6px 0; font-size: 12px;">
      OpenAI Codex runtime. Two auth paths: a ChatGPT session set inside
      the container via <code>codex login</code> (preferred — uses your
      Plus/Pro plan), or an OPENAI_API_KEY fallback saved here
      (token-priced). Keys are encrypted with
      <code>HARNESS_SECRETS_KEY</code>.
    </p>
    ${loaded && data && !keyOk
      ? html`<div style="font-size: 11px; color: var(--err); border: 1px solid var(--err); background: rgba(248,81,73,0.08); padding: 4px 8px; border-radius: 3px; margin-bottom: 6px;">
          secrets store unavailable: ${keyReason || "unknown"}
        </div>`
      : null}
    ${loaded && data
      ? html`<div style="font-size: 11px; color: var(--muted); margin-bottom: 6px;">
          <div>
            runtime gate:
            ${data.enabled
              ? html`<span style="color: var(--ok);">● enabled</span>`
              : html`<span style="color: var(--warn);">○ disabled (set HARNESS_CODEX_ENABLED)</span>`}
          </div>
          <div>
            ChatGPT session:
            ${data.chatgpt_session_present
              ? html`<span style="color: var(--ok);">● present (${data.config_dir || "$CODEX_HOME"})</span>`
              : html`<span style="color: var(--muted);">○ none — run <code>codex login</code> in the container, or use API key below</span>`}
          </div>
          <div>
            API key fallback:
            ${data.api_key_set
              ? html`<span style="color: var(--ok);">● set</span>`
              : html`<span style="color: var(--muted);">○ unset</span>`}
            <span class="muted"> · resolution: ${data.method}</span>
          </div>
        </div>`
      : null}
    <div class="drawer-row">
      <label class="drawer-label">API key</label>
      <input
        type="password"
        placeholder=${data && data.api_key_set ? "•••••• (saved)" : "sk-..."}
        value=${keyDraft}
        onInput=${(e) => setKeyDraft(e.target.value)}
        autocomplete="off"
        spellcheck="false"
        style="flex: 1; min-width: 0;"
      />
    </div>
    <div class="drawer-row" style="gap: 6px;">
      <button onClick=${save} disabled=${saving || !keyDraft.trim()}>
        ${saving ? "saving…" : "save"}
      </button>
      <button onClick=${testKey} disabled=${testing || !(data && data.api_key_set)}>
        ${testing ? "testing…" : "test"}
      </button>
      <button onClick=${clearKey} disabled=${clearing || !(data && data.api_key_set)}>
        ${clearing ? "clearing…" : "clear"}
      </button>
    </div>
    ${msg
      ? html`<div style="font-size: 11px; color: ${msg.kind === "ok" ? "var(--ok)" : "var(--err)"}; margin-top: 4px;">
          ${msg.text}
        </div>`
      : null}
  </section>`;
}

// MCP server configuration. Paste a Claude-Desktop-style JSON
// snippet; parse + detect server name(s); save to the DB. Each saved
// server gets a row with status dot + enable/disable/delete/test. The
// loader in server/mcp_config.py merges this with any HARNESS_MCP_CONFIG
// file — DB wins on name collision.
// Batch "clear session_id" for any subset of agents. All ticked by
// default; Clear POSTs one request with the checked set so next turn
// for those agents starts a fresh conversation.
function SessionsSection() {
  const [agents, setAgents] = useState([]);
  const [selected, setSelected] = useState({});  // { slotId: bool }
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);  // { kind: "ok"|"err", text }

  const reload = useCallback(async () => {
    try {
      const res = await authFetch("/api/agents");
      if (!res.ok) return;
      const data = await res.json();
      const list = Array.isArray(data.agents) ? data.agents : [];
      setAgents(list);
      setSelected((prev) => {
        const next = {};
        for (const a of list) {
          next[a.id] = prev[a.id] === undefined ? true : prev[a.id];
        }
        return next;
      });
    } catch (e) {
      console.error("sessions load failed", e);
    } finally {
      setLoaded(true);
    }
  }, []);
  useEffect(() => { reload(); }, [reload]);

  const toggle = (id) => setSelected((s) => ({ ...s, [id]: !s[id] }));
  const setAll = (v) => setSelected((s) => {
    const next = {};
    for (const a of agents) next[a.id] = v;
    return next;
  });

  const _hasSession = (a) => !!(a.session_id || a.codex_thread_id);
  const targets = agents.filter((a) => selected[a.id]).map((a) => a.id);
  const withSession = agents.filter(_hasSession).map((a) => a.id);

  const clearSelected = async () => {
    if (targets.length === 0 || busy) return;
    const confirmed = window.confirm(
      `Clear session_id on ${targets.length} agent${targets.length === 1 ? "" : "s"}? `
      + `Their next turn will start without prior conversation context `
      + `(tasks / memory are unaffected).`
    );
    if (!confirmed) return;
    setBusy(true);
    setMsg(null);
    try {
      const res = await authFetch("/api/agents/sessions/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agents: targets }),
      });
      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        const n = Array.isArray(data.cleared) ? data.cleared.length : targets.length;
        setMsg({ kind: "ok", text: `Cleared ${n} session${n === 1 ? "" : "s"}.` });
        await reload();
      } else {
        const body = await res.text().catch(() => "");
        setMsg({ kind: "err", text: `HTTP ${res.status}: ${body || "clear failed"}` });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally {
      setBusy(false);
    }
  };

  return html`<section class="drawer-section">
    <h3>Sessions</h3>
    <p class="muted" style="margin: 0 0 8px 0; font-size: 12px;">
      Tick the agents whose conversation you want to reset, then Clear.
      Equivalent to the pane header 🗑 button but in batch. Only clears
      the SDK <code>session_id</code> — tasks, memory, inbox, briefs
      are untouched. Agents with no active session show dimmed.
    </p>
    ${loaded
      ? agents.length === 0
        ? html`<p class="muted">No agents registered.</p>`
        : html`<div style="display: flex; flex-wrap: wrap; gap: 6px 14px; margin-bottom: 8px;">
            ${agents.map((a) => {
              const has = _hasSession(a);
              return html`<label
                key=${a.id}
                title=${has ? "Session present — will be cleared" : "No active session"}
                style=${"display: inline-flex; align-items: center; gap: 5px; "
                  + "font-size: 12px; cursor: pointer; user-select: none; "
                  + (has ? "" : "opacity: 0.55;")}
              >
                <input
                  type="checkbox"
                  checked=${!!selected[a.id]}
                  disabled=${busy}
                  onChange=${() => toggle(a.id)}
                />
                <span>${slotShortLabel(a.id)}${a.name ? " · " + a.name : ""}${has ? "" : " (none)"}</span>
              </label>`;
            })}
          </div>
          <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap;">
            <button
              type="button"
              disabled=${busy}
              onClick=${() => setAll(true)}
            >Select all</button>
            <button
              type="button"
              disabled=${busy}
              onClick=${() => setAll(false)}
            >Select none</button>
            <button
              type="button"
              disabled=${busy || withSession.length === 0}
              onClick=${() => setSelected(() => {
                const next = {};
                for (const a of agents) next[a.id] = _hasSession(a);
                return next;
              })}
              title="Tick only agents that currently have a session"
            >Only active (${withSession.length})</button>
            <button
              class="primary"
              type="button"
              disabled=${busy || targets.length === 0}
              onClick=${clearSelected}
            >${busy ? "Clearing…" : `Clear ${targets.length} session${targets.length === 1 ? "" : "s"}`}</button>
            ${msg
              ? html`<span style=${"font-size: 12px; color: " + (msg.kind === "ok" ? "var(--ok)" : "var(--err)") + ";"}>${msg.text}</span>`
              : null}
          </div>`
      : html`<p class="muted">loading…</p>`}
  </section>`;
}


function MCPServersSection() {
  const [servers, setServers] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [paste, setPaste] = useState("");
  const [pasteName, setPasteName] = useState("");
  const [pasteTools, setPasteTools] = useState("");
  const [allowSecrets, setAllowSecrets] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState(null);    // { kind: "ok"|"err", text }
  const [testing, setTesting] = useState(null);  // server name currently under test

  const reload = useCallback(async () => {
    try {
      const res = await authFetch("/api/mcp/servers");
      if (res.ok) {
        const data = await res.json();
        setServers(Array.isArray(data.servers) ? data.servers : []);
      }
    } catch (e) {
      console.error("mcp list failed", e);
    } finally {
      setLoaded(true);
    }
  }, []);
  useEffect(() => { reload(); }, [reload]);

  const save = useCallback(async () => {
    setSaving(true);
    setMsg(null);
    try {
      const body = {
        paste,
        name: pasteName.trim() || null,
        allowed_tools: pasteTools
          .split(/[,\s]+/)
          .map((t) => t.trim())
          .filter(Boolean),
        enabled: true,
        allow_secrets: allowSecrets,
      };
      const res = await authFetch("/api/mcp/servers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        const data = await res.json();
        setMsg({
          kind: "ok",
          text: "saved: " + (data.saved || []).join(", ") +
            ((data.secret_warnings || []).length
              ? "  (ignored secret warning: " + data.secret_warnings.join(", ") + ")"
              : ""),
        });
        setPaste("");
        setPasteName("");
        setPasteTools("");
        setAllowSecrets(false);
        await reload();
      } else {
        let detail;
        try {
          const data = await res.json();
          detail = typeof data.detail === "string"
            ? data.detail
            : JSON.stringify(data.detail);
        } catch (_) {
          detail = "HTTP " + res.status;
        }
        setMsg({ kind: "err", text: detail });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally {
      setSaving(false);
    }
  }, [paste, pasteName, pasteTools, allowSecrets, reload]);

  const toggle = useCallback(async (name, enabled) => {
    await authFetch("/api/mcp/servers/" + encodeURIComponent(name), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    await reload();
  }, [reload]);

  const saveTools = useCallback(async (name, toolsStr) => {
    const tools = toolsStr.split(/[,\s]+/).map((t) => t.trim()).filter(Boolean);
    await authFetch("/api/mcp/servers/" + encodeURIComponent(name), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ allowed_tools: tools }),
    });
    await reload();
  }, [reload]);

  const remove = useCallback(async (name) => {
    if (!confirm("Delete MCP server '" + name + "'? Can be re-added from paste."))
      return;
    await authFetch("/api/mcp/servers/" + encodeURIComponent(name), {
      method: "DELETE",
    });
    await reload();
  }, [reload]);

  const test = useCallback(async (name) => {
    setTesting(name);
    try {
      await authFetch("/api/mcp/servers/" + encodeURIComponent(name) + "/test", {
        method: "POST",
      });
      await reload();
    } finally {
      setTesting(null);
    }
  }, [reload]);

  return html`<section class="drawer-section">
    <h3>MCP servers</h3>
    <p class="muted" style="margin: 0 0 6px 0; font-size: 12px;">
      Paste a server config from the provider's docs (Claude-Desktop
      style — or our <code>{servers, allowed_tools}</code> format).
      Keep secrets in env vars and reference them as
      <code>\${VAR}</code> — raw tokens get rejected unless you tick
      the override box.
    </p>
    <textarea
      placeholder=${"{\n  \"mcpServers\": {\n    \"github\": {\n      \"command\": \"npx\",\n      \"args\": [\"-y\", \"@modelcontextprotocol/server-github\"],\n      \"env\": {\"GITHUB_PERSONAL_ACCESS_TOKEN\": \"\${GITHUB_TOKEN}\"}\n    }\n  }\n}"}
      value=${paste}
      onInput=${(e) => setPaste(e.target.value)}
      rows=${8}
      style="width: 100%; font-family: ui-monospace, monospace; font-size: 11px; background: #10131a; color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 6px 8px; resize: vertical;"
    />
    <div style="display: flex; gap: 8px; margin: 4px 0; flex-wrap: wrap;">
      <input
        placeholder="name (required for flat pastes)"
        value=${pasteName}
        onInput=${(e) => setPasteName(e.target.value)}
        style="flex: 1; min-width: 120px; background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 3px 6px; font-size: 12px;"
      />
      <input
        placeholder="allowed tools (comma-separated; e.g. create_issue, list_issues)"
        value=${pasteTools}
        onInput=${(e) => setPasteTools(e.target.value)}
        style="flex: 2; min-width: 180px; background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 3px 6px; font-size: 12px;"
      />
    </div>
    <div style="display: flex; gap: 10px; align-items: center; margin: 4px 0;">
      <label style="font-size: 11px; color: var(--muted);">
        <input
          type="checkbox"
          checked=${allowSecrets}
          onChange=${(e) => setAllowSecrets(e.target.checked)}
        />
        save even if secrets detected
      </label>
      <button
        class="primary"
        disabled=${saving || !paste.trim()}
        onClick=${save}
      >${saving ? "saving…" : "save"}</button>
    </div>
    ${msg
      ? html`<div style=${"font-size: 11px; margin: 4px 0; padding: 4px 8px; border-radius: 3px; " + (msg.kind === "ok"
            ? "color: var(--ok); border: 1px solid var(--ok); background: rgba(63,185,80,0.08);"
            : "color: var(--err); border: 1px solid var(--err); background: rgba(248,81,73,0.08); white-space: pre-wrap;")}>${msg.text}</div>`
      : null}
    <div style="margin-top: 10px;">
      ${loaded
        ? servers.length === 0
          ? html`<p class="muted" style="font-size: 11px;">No MCP servers configured. Paste one above.</p>`
          : servers.map((s) => html`<${MCPServerCard}
              key=${s.name}
              server=${s}
              testing=${testing === s.name}
              onToggle=${(e) => toggle(s.name, e)}
              onSaveTools=${(t) => saveTools(s.name, t)}
              onDelete=${() => remove(s.name)}
              onTest=${() => test(s.name)}
            />`)
        : html`<p class="muted">loading…</p>`}
    </div>
  </section>`;
}

function MCPServerCard({ server, testing, onToggle, onSaveTools, onDelete, onTest }) {
  const [toolsDraft, setToolsDraft] = useState(
    (server.allowed_tools || []).join(", ")
  );
  const toolsDirty = toolsDraft !== (server.allowed_tools || []).join(", ");
  const dot = server.last_ok === null
    ? "color: var(--muted);"
    : server.last_ok
    ? "color: var(--ok);"
    : "color: var(--err);";
  const when = server.last_tested_at
    ? new Date(server.last_tested_at).toLocaleString()
    : "never tested";
  return html`<div style="border: 1px solid var(--border); border-radius: 4px; padding: 8px 10px; margin-bottom: 6px; font-size: 12px; opacity: ${server.enabled ? 1 : 0.6};">
    <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">
      <span style=${"font-size: 14px; " + dot}>●</span>
      <strong>${server.name}</strong>
      <span class="muted" style="font-size: 10px;">${when}</span>
      <span style="margin-left: auto; display: flex; gap: 6px;">
        <button onClick=${() => onToggle(!server.enabled)}>
          ${server.enabled ? "disable" : "enable"}
        </button>
        <button disabled=${testing} onClick=${onTest}>
          ${testing ? "testing…" : "test"}
        </button>
        <button onClick=${onDelete} style="color: var(--err);">delete</button>
      </span>
    </div>
    ${server.last_error
      ? html`<pre style="margin: 4px 0 0; padding: 4px 6px; background: #10131a; color: var(--err); border-radius: 3px; font-size: 10px; white-space: pre-wrap;">${server.last_error}</pre>`
      : null}
    <div style="margin-top: 6px; display: flex; gap: 6px; align-items: center;">
      <label class="muted" style="font-size: 11px;">allowed tools</label>
      <input
        value=${toolsDraft}
        onInput=${(e) => setToolsDraft(e.target.value)}
        placeholder="comma-separated (e.g. create_issue, list_issues)"
        style="flex: 1; background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 2px 5px; font-size: 11px; font-family: ui-monospace, monospace;"
      />
      ${toolsDirty
        ? html`<button onClick=${() => onSaveTools(toolsDraft)}>save</button>`
        : null}
    </div>
    <div class="muted" style="font-size: 10px; margin-top: 4px;">
      type: ${(server.config && (server.config.type || (server.config.url ? "http" : server.config.command ? "stdio" : "?"))) || "?"}
    </div>
  </div>`;
}

function SecretsSection() {
  // Encrypted UI-managed secrets. These feed ${VAR} interpolation in MCP
  // configs (and anything else that calls _interpolate) — the store wins
  // over os.environ on name collision. Plaintext values never round-trip:
  // once saved you can only replace or delete, not view.
  const [rows, setRows] = useState([]);
  const [status, setStatus] = useState(null); // {ok, reason?}
  const [loaded, setLoaded] = useState(false);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null); // {kind: "ok"|"err", text}

  const reload = useCallback(async () => {
    try {
      const res = await authFetch("/api/secrets");
      if (res.ok) {
        const data = await res.json();
        setRows(Array.isArray(data.secrets) ? data.secrets : []);
        setStatus(data.status || null);
      }
    } catch (e) {
      console.error("secrets list failed", e);
    } finally {
      setLoaded(true);
    }
  }, []);
  useEffect(() => { reload(); }, [reload]);

  const save = useCallback(async () => {
    const trimmed = name.trim();
    if (!trimmed || !value) return;
    setBusy(true);
    setMsg(null);
    try {
      const res = await authFetch(
        "/api/secrets/" + encodeURIComponent(trimmed),
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value }),
        }
      );
      if (res.ok) {
        setMsg({ kind: "ok", text: `saved ${trimmed}` });
        setName("");
        setValue("");
        await reload();
      } else {
        const body = await res.text().catch(() => "");
        setMsg({ kind: "err", text: `HTTP ${res.status} ${body.slice(0, 120)}` });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    } finally {
      setBusy(false);
    }
  }, [name, value, reload]);

  const del = useCallback(async (n) => {
    if (!confirm(`Delete secret ${n}?`)) return;
    try {
      const res = await authFetch("/api/secrets/" + encodeURIComponent(n), {
        method: "DELETE",
      });
      if (res.ok) {
        setMsg({ kind: "ok", text: `deleted ${n}` });
        await reload();
      } else {
        setMsg({ kind: "err", text: `HTTP ${res.status}` });
      }
    } catch (e) {
      setMsg({ kind: "err", text: String(e) });
    }
  }, [reload]);

  const disabled = !status || !status.ok;

  return html`<section class="drawer-section">
    <h3>Secrets</h3>
    <p class="muted" style="margin: 0 0 6px 0; font-size: 12px;">
      Encrypted values referenced by <code>\${NAME}</code> placeholders in
      MCP configs. On collision with an env var of the same name, the
      stored secret wins. Values are write-only — you can replace or
      delete, but never read back.
    </p>
    ${disabled
      ? html`<p style="font-size: 12px; color: var(--err); margin: 0 0 6px 0;">
          Store disabled: ${status && status.reason
            ? status.reason
            : "HARNESS_SECRETS_KEY not set"}.
          Generate a key with
          <code>python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"</code>
          and set it in Zeabur env vars, then redeploy.
        </p>`
      : null}
    <div style="display: flex; gap: 6px; margin-bottom: 6px; flex-wrap: wrap;">
      <input
        placeholder="name (e.g. GITHUB_TOKEN)"
        value=${name}
        onInput=${(e) => setName(e.target.value)}
        disabled=${disabled || busy}
        style="flex: 1; min-width: 140px; background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 3px 6px; font-size: 12px; font-family: ui-monospace, monospace;"
      />
      <input
        type="password"
        placeholder="value"
        value=${value}
        onInput=${(e) => setValue(e.target.value)}
        disabled=${disabled || busy}
        style="flex: 2; min-width: 180px; background: var(--bg); color: var(--fg); border: 1px solid var(--border); border-radius: 3px; padding: 3px 6px; font-size: 12px; font-family: ui-monospace, monospace;"
      />
      <button
        class="primary"
        type="button"
        disabled=${disabled || busy || !name.trim() || !value}
        onClick=${save}
      >${busy ? "saving…" : "save"}</button>
    </div>
    ${msg
      ? html`<div style=${"font-size: 11px; color: " + (msg.kind === "ok" ? "var(--ok)" : "var(--err)") + "; margin-bottom: 6px;"}>${msg.text}</div>`
      : null}
    ${loaded && rows.length === 0
      ? html`<p class="muted" style="font-size: 11px;">No secrets stored.</p>`
      : null}
    ${rows.map((s) => html`
      <div key=${s.name} style="display: flex; align-items: center; gap: 8px; border: 1px solid var(--border); border-radius: 4px; padding: 4px 8px; margin-bottom: 4px; font-size: 12px;">
        <code style="font-family: ui-monospace, monospace;">\${${s.name}}</code>
        <span class="muted" style="font-size: 10px; flex: 1;">
          updated ${s.updated_at ? new Date(s.updated_at).toLocaleString() : "?"}
        </span>
        <button
          onClick=${() => { setName(s.name); setValue(""); }}
          title="Replace value (name pre-filled)"
        >replace</button>
        <button
          onClick=${() => del(s.name)}
          style="color: var(--err);"
          title="Delete this secret"
        >delete</button>
      </div>
    `)}
  </section>`;
}


// Display preferences: timezone for timestamp rendering. Server stamps
// in UTC; users typically want to see their local clock. The toggle
// reloads the page so all already-rendered timestamps update at once
// (timeStr() reads localStorage on every call but old DOM nodes don't
// re-render on their own).
function DisplaySection() {
  const [pref, setPref] = useState(() => {
    try { return localStorage.getItem("harness_tz_pref") || "local"; }
    catch (_) { return "local"; }
  });
  const tzName = (() => {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "(unknown)";
    } catch (_) { return "(unknown)"; }
  })();
  const apply = (next) => {
    if (next === pref) return;
    try { localStorage.setItem("harness_tz_pref", next); } catch (_) {}
    setPref(next);
    location.reload();
  };
  return html`<section class="drawer-section">
    <h3>Display</h3>
    <p class="muted" style="font-size: 12px; margin: 0 0 6px 0;">
      Timestamps in event timelines and tool cards. Server stores UTC;
      this only affects how they're rendered. Detected zone:
      <code>${tzName}</code>.
    </p>
    <div style="display: flex; gap: 6px;">
      <button
        type="button"
        class=${pref === "local" ? "primary" : ""}
        onClick=${() => apply("local")}
      >local time</button>
      <button
        type="button"
        class=${pref === "utc" ? "primary" : ""}
        onClick=${() => apply("utc")}
      >UTC</button>
    </div>
  </section>`;
}


function SettingsDrawer({ onClose, serverStatus }) {
  // A compact row summarizing the live server status: paused, running
  // agents, ws subscribers. Rendered above the health checks so the
  // operator sees the most useful numbers first.
  const renderRuntime = () => {
    if (!serverStatus) return null;
    const running = serverStatus.running_slots || [];
    const subs = serverStatus.ws_subscribers ?? "?";
    return html`
      <ul class="drawer-health-list" style="margin-bottom: 8px;">
        <li class=${"drawer-health-row " + (serverStatus.paused ? "fail" : "ok")}>
          <span class="drawer-health-dot" />
          <span class="drawer-health-name">paused</span>
          <span class="drawer-health-detail">${serverStatus.paused ? "yes" : "no"}</span>
        </li>
        <li class="drawer-health-row ok">
          <span class="drawer-health-dot" />
          <span class="drawer-health-name">running</span>
          <span class="drawer-health-detail">
            ${running.length === 0 ? "none" : running.join(", ")}
          </span>
        </li>
        <li class="drawer-health-row ok">
          <span class="drawer-health-dot" />
          <span class="drawer-health-name">ws subs</span>
          <span class="drawer-health-detail">${subs}</span>
        </li>
      </ul>
    `;
  };
  const [health, setHealth] = useState(null);
  const [healthErr, setHealthErr] = useState("");
  const [healthLoading, setHealthLoading] = useState(false);

  const loadHealth = useCallback(async () => {
    setHealthLoading(true);
    setHealthErr("");
    try {
      const res = await authFetch("/api/health");
      // /api/health returns 503 when a required subsystem fails, but
      // the body still carries per-subsystem detail; read both.
      const data = await res.json();
      setHealth(data);
      if (!res.ok) setHealthErr("HTTP " + res.status);
    } catch (e) {
      setHealthErr(String(e));
    } finally {
      setHealthLoading(false);
    }
  }, []);

  useEffect(() => {
    loadHealth();
  }, [loadHealth]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const stop = (e) => e.stopPropagation();

  return html`
    <div class="drawer-backdrop" onClick=${onClose}>
      <aside class="drawer" onClick=${stop}>
        <header class="drawer-head">
          <h2 class="drawer-title">Settings</h2>
          <button class="drawer-close" onClick=${onClose} title="Close (Esc)">×</button>
        </header>
        <div class="drawer-body">
          <section class="drawer-section">
            <h3>
              Health
              <button
                class="drawer-refresh"
                onClick=${loadHealth}
                disabled=${healthLoading}
                title="Re-probe subsystems"
              >${healthLoading ? "…" : "↻"}</button>
            </h3>
            ${renderRuntime()}
            ${healthErr
              ? html`<p class="muted">⚠ ${healthErr}</p>`
              : null}
            ${health
              ? html`<ul class="drawer-health-list">
                  ${Object.entries(health.checks || {}).map(
                    ([name, c]) => html`
                      <li key=${name} class=${"drawer-health-row " + (c.ok ? "ok" : "fail")}>
                        <span class="drawer-health-dot" />
                        <span class="drawer-health-name">${name}</span>
                        <span class="drawer-health-detail">
                          ${summarizeHealthCheck(c)}
                        </span>
                      </li>
                    `
                  )}
                </ul>`
              : healthLoading
              ? html`<p class="muted">probing…</p>`
              : null}
          </section>

          <section class="drawer-section">
            <h3>Cost caps</h3>
            <div class="drawer-disabled">
              <p class="muted">
                Daily caps are enforced at spawn time — agents whose daily
                spend hits the cap see a <strong>🚫 spawn blocked</strong>
                event in the timeline. Edit via the
                <code>HARNESS_AGENT_DAILY_CAP</code> and
                <code>HARNESS_TEAM_DAILY_CAP</code> env vars in Zeabur,
                then redeploy. Set to <code>0</code> to disable either cap.
              </p>
              <label>Per-agent daily cap (USD)</label>
              <input
                type="number"
                value=${serverStatus?.caps?.agent_daily_usd?.toFixed(2) ?? "5.00"}
                disabled
              />
              <label>Team daily cap (USD)</label>
              <input
                type="number"
                value=${serverStatus?.caps?.team_daily_usd?.toFixed(2) ?? "20.00"}
                disabled
              />
              <label>Team spent today (USD, live)</label>
              <input
                type="number"
                value=${(serverStatus?.caps?.team_today_usd ?? 0).toFixed(4)}
                disabled
              />
            </div>
          </section>

          <${ProjectsSection} />

          <${ClaudeAuthSection}
            health=${health}
            onRefresh=${loadHealth}
          />

          <${WebDAVSection}
            serverStatus=${serverStatus}
            health=${health}
            onRefresh=${loadHealth}
          />

          <${TeamToolsSection} />

          <${TeamModelsSection} />

          <${TeamRuntimesSection} />

          <${TeamRepoSection} />

          <${TeamTelegramSection} />

          <${TeamCodexSection} />

          <${SecretsSection} />

          <${MCPServersSection} />

          <${SessionsSection} />

          <${DisplaySection} />

          <section class="drawer-section">
            <h3>Layout</h3>
            <p class="muted">
              Column widths + stack heights are saved per layout shape
              in <code>harness_split_sizes_v1</code>. Click to reset to
              equal distribution (requires a page reload for Split.js
              to rebind).
            </p>
            <button
              class="primary"
              style="margin-top: 6px;"
              onClick=${() => {
                try { localStorage.removeItem("harness_split_sizes_v1"); } catch (_) {}
                location.reload();
              }}
            >Reset resize state</button>
            <p class="muted" style="margin-top: 12px; font-size: 11px;">
              Slot open/close + env panel state and per-pane settings
              are separate — this only resets column widths.
            </p>
          </section>

          <section class="drawer-section">
            <h3>About</h3>
            <p>
              <strong>TeamOfTen harness</strong><br />
              1 Coach + 10 Players orchestrated via Claude Agent SDK<br />
              <a
                href="https://github.com/Nicolasmoute/TeamOfTen"
                target="_blank"
                rel="noopener noreferrer"
                >github.com/Nicolasmoute/TeamOfTen</a
              >
            </p>
            <p class="muted" style="font-size: 11px; margin-top: 6px;">
              Shortcuts: ⌘/Ctrl+B toggle env panel · ⌘/Ctrl+. toggle pause ·
              ⌘/Ctrl+Enter in a pane input to send · ⌘/Ctrl+↑↓ in a pane
              input to cycle prompt history.
            </p>
          </section>
        </div>
      </aside>
    </div>
  `;
}

// ------------------------------------------------------------------
// recurrence pane (right side): coach tick / repeats / crons
// (recurrence-specs.md §12)
// ------------------------------------------------------------------

function _formatRelative(iso) {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "—";
  const delta = Math.round((ts - Date.now()) / 1000);
  const abs = Math.abs(delta);
  let label;
  if (abs < 60) label = `${abs}s`;
  else if (abs < 3600) label = `${Math.round(abs / 60)} min`;
  else if (abs < 86400) label = `${Math.round(abs / 3600)} h`;
  else label = `${Math.round(abs / 86400)} d`;
  return delta >= 0 ? `in ${label}` : `${label} ago`;
}

function _tickRow(rows) {
  return rows.find((r) => r.kind === "tick") || null;
}

function _filterKind(rows, kind) {
  return rows.filter((r) => r.kind === kind);
}

function RecurrencePane({ rows, onClose, onRefresh, onError }) {
  const [tickInput, setTickInput] = useState("");
  const [newRepeat, setNewRepeat] = useState({ cadence: "", prompt: "" });
  const [newCron, setNewCron] = useState({ cadence: "", prompt: "" });
  // Per-row pending edits — { [id]: { cadence?, prompt? } }. Lives in
  // pane state so a re-render driven by a WS refresh doesn't blow away
  // in-progress typing. Cleared on save / discard.
  const [edits, setEdits] = useState({});
  const [busy, setBusy] = useState(false);

  function patchEdit(id, field, value) {
    setEdits((e) => ({
      ...e,
      [id]: { ...(e[id] || {}), [field]: value },
    }));
  }

  function clearEdit(id) {
    setEdits((e) => {
      const next = { ...e };
      delete next[id];
      return next;
    });
  }

  function hasEdits(id) {
    return edits[id] && Object.keys(edits[id]).length > 0;
  }

  function effective(row, field) {
    if (edits[row.id] && field in edits[row.id]) return edits[row.id][field];
    return row[field] || "";
  }
  const tick = _tickRow(rows);
  const repeats = _filterKind(rows, "repeat");
  const crons = _filterKind(rows, "cron");

  const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "UTC";

  async function _http(url, opts) {
    setBusy(true);
    try {
      const res = await authFetch(url, opts);
      if (!res.ok) {
        const body = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}${body ? ": " + body.slice(0, 120) : ""}`);
      }
      const data = await res.json().catch(() => ({}));
      onRefresh && onRefresh();
      return data;
    } catch (e) {
      onError && onError(String(e.message || e));
      throw e;
    } finally {
      setBusy(false);
    }
  }

  function applyTick(minutes) {
    return _http("/api/coach/tick", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ minutes }),
    });
  }

  function disableTick() {
    return _http("/api/coach/tick", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: false }),
    });
  }

  function fireNow() {
    return _http("/api/coach/tick", { method: "POST" });
  }

  function addRepeat() {
    const minutes = parseInt(newRepeat.cadence, 10);
    const prompt = (newRepeat.prompt || "").trim();
    if (!minutes || minutes < 1 || !prompt) return;
    return _http("/api/recurrences", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind: "repeat", cadence: String(minutes), prompt,
      }),
    }).then(() => setNewRepeat({ cadence: "", prompt: "" }));
  }

  function addCron() {
    const cadence = (newCron.cadence || "").trim();
    const prompt = (newCron.prompt || "").trim();
    if (!cadence || !prompt) return;
    return _http("/api/recurrences", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind: "cron", cadence, prompt, tz,
      }),
    }).then(() => setNewCron({ cadence: "", prompt: "" }));
  }

  function deleteRow(id) {
    return _http(`/api/recurrences/${id}`, { method: "DELETE" });
  }

  function toggleRow(row) {
    return _http(`/api/recurrences/${row.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !row.enabled }),
    });
  }

  function saveRow(row) {
    const e = edits[row.id] || {};
    const body = {};
    if ("cadence" in e) body.cadence = e.cadence;
    if ("prompt" in e) body.prompt = e.prompt;
    if (Object.keys(body).length === 0) return;
    // Spec §12.2: "TZ is read-only, captured at create time, but
    // re-saving picks up the operator's current TZ." For cron rows,
    // include the browser's current TZ on every save so a move (DST
    // shift, laptop relocation) re-anchors next-fire computation.
    if (row.kind === "cron") body.tz = tz;
    return _http(`/api/recurrences/${row.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(() => clearEdit(row.id));
  }

  // Live DSL validation for the cron schedule input. Mirrors the
  // grammar in server/recurrences.py:parse_cron — reject bad input
  // before sending so the Save button is disabled visually instead
  // of triggering a 400 round-trip.
  function _validCronDSL(s) {
    if (typeof s !== "string") return false;
    const trimmed = s.trim();
    if (!trimmed) return false;
    // Spec §5.1: TIME = HH:MM strict. Bare DAY_LIST must have ≥2
    // days; single-day uses `weekly DAY TIME`.
    const T = "[01]\\d|2[0-3]";
    const M = "[0-5]\\d";
    const TIME = `(${T}):(${M})`;
    const DAY = "mon|tue|wed|thu|fri|sat|sun";
    const DAYLIST_MULTI = `(${DAY})(,(${DAY}))+`;
    const patterns = [
      `^daily ${TIME}$`,
      `^weekdays ${TIME}$`,
      `^weekends ${TIME}$`,
      `^${DAYLIST_MULTI} ${TIME}$`,
      `^weekly (${DAY}) ${TIME}$`,
      `^monthly ([1-9]|[12]\\d|3[01]) ${TIME}$`,
      `^\\d{4}-\\d{2}-\\d{2} ${TIME}$`,
    ];
    return patterns.some((p) => new RegExp(p).test(trimmed));
  }

  function rowSaveDisabled(row) {
    const e = edits[row.id] || {};
    if (row.kind === "cron" && "cadence" in e) {
      return !_validCronDSL(e.cadence);
    }
    if (row.kind === "repeat" && "cadence" in e) {
      const n = parseInt(e.cadence, 10);
      return !n || n < 1;
    }
    return false;
  }

  return html`
    <aside class="rec-pane">
      <div class="rec-head">
        <span class="rec-title">Recurrences</span>
        <button class="rec-close" title="Close" onClick=${onClose}>×</button>
      </div>
      <div class="rec-body">
        <section class="rec-section">
          <h3 class="rec-section-title">Tick</h3>
          ${tick
            ? html`
                <div class="rec-card">
                  <div class="rec-card-row">
                    <span class="rec-status-dot ${tick.enabled ? "on" : "off"}"></span>
                    <span>every <strong>${tick.cadence}</strong> min</span>
                  </div>
                  <div class="rec-card-meta">
                    <span title=${tick.next_fire_at || ""}>next: ${_formatRelative(tick.next_fire_at)}</span>
                    <span title=${tick.last_fired_at || ""}>last: ${_formatRelative(tick.last_fired_at)}</span>
                  </div>
                  <div class="rec-actions">
                    <button onClick=${fireNow} disabled=${busy}>fire now</button>
                    ${tick.enabled
                      ? html`<button class="rec-delete" onClick=${disableTick} disabled=${busy}>disable</button>`
                      : html`<button onClick=${() => applyTick(parseInt(tick.cadence, 10) || 60)} disabled=${busy}>enable</button>`}
                  </div>
                </div>`
            : html`<div class="rec-empty">No tick yet — set one below.</div>`}
          <div class="rec-card-row" style="margin-top:8px">
            <label>minutes</label>
            <input
              type="number"
              min="1"
              placeholder="60"
              value=${tickInput}
              onInput=${(e) => setTickInput(e.target.value)}
            />
          </div>
          <div class="rec-actions">
            <button
              disabled=${busy || !parseInt(tickInput, 10)}
              onClick=${() => {
                const n = parseInt(tickInput, 10);
                if (n > 0) {
                  applyTick(n).then(() => setTickInput(""));
                }
              }}
            >${tick ? "update" : "create"}</button>
          </div>
        </section>

        <section class="rec-section">
          <h3 class="rec-section-title">Repeats <span style="margin-left:auto;font-weight:400">${repeats.length}</span></h3>
          ${repeats.length === 0
            ? html`<div class="rec-empty">No repeats.</div>`
            : repeats.map((r) => html`
                <div class="rec-card" key=${r.id}>
                  <div class="rec-card-row">
                    <span class="rec-status-dot ${r.enabled ? "on" : "off"}"></span>
                    <span>#${r.id}</span>
                  </div>
                  <div class="rec-card-row">
                    <label>minutes</label>
                    <input
                      type="number"
                      min="1"
                      value=${effective(r, "cadence")}
                      onInput=${(e) => patchEdit(r.id, "cadence", e.target.value)}
                    />
                  </div>
                  <div class="rec-card-row">
                    <label>prompt</label>
                    <textarea
                      onInput=${(e) => patchEdit(r.id, "prompt", e.target.value)}
                    >${effective(r, "prompt")}</textarea>
                  </div>
                  <div class="rec-card-meta">
                    <span title=${r.next_fire_at || ""}>next: ${_formatRelative(r.next_fire_at)}</span>
                    <span title=${r.last_fired_at || ""}>last: ${_formatRelative(r.last_fired_at)}</span>
                  </div>
                  <div class="rec-actions">
                    ${hasEdits(r.id)
                      ? html`<button
                              onClick=${() => saveRow(r)}
                              disabled=${busy || rowSaveDisabled(r)}
                              title=${rowSaveDisabled(r) ? "Invalid input — fix above" : "Save edits"}
                            >save</button>
                            <button onClick=${() => clearEdit(r.id)} disabled=${busy}>discard</button>`
                      : html`<button onClick=${() => toggleRow(r)} disabled=${busy}>${r.enabled ? "disable" : "enable"}</button>
                            <button class="rec-delete" onClick=${() => deleteRow(r.id)} disabled=${busy}>delete</button>`}
                  </div>
                </div>`)}
          <div class="rec-card" style="margin-top:8px">
            <div class="rec-card-row">
              <label>minutes</label>
              <input
                type="number"
                min="1"
                placeholder="30"
                value=${newRepeat.cadence}
                onInput=${(e) => setNewRepeat({ ...newRepeat, cadence: e.target.value })}
              />
            </div>
            <div class="rec-card-row">
              <label>prompt</label>
              <textarea
                placeholder="summarize new commits"
                value=${newRepeat.prompt}
                onInput=${(e) => setNewRepeat({ ...newRepeat, prompt: e.target.value })}
              ></textarea>
            </div>
            <button class="rec-add-btn" onClick=${addRepeat} disabled=${busy}>+ add repeat</button>
          </div>
        </section>

        <section class="rec-section">
          <h3 class="rec-section-title">Crons <span style="margin-left:auto;font-weight:400">${crons.length}</span></h3>
          ${crons.length === 0
            ? html`<div class="rec-empty">No crons.</div>`
            : crons.map((r) => html`
                <div class="rec-card" key=${r.id}>
                  <div class="rec-card-row">
                    <span class="rec-status-dot ${r.enabled ? "on" : "off"}"></span>
                    <span>#${r.id}</span>
                  </div>
                  <div class="rec-card-row">
                    <label>schedule</label>
                    <input
                      type="text"
                      value=${effective(r, "cadence")}
                      onInput=${(e) => patchEdit(r.id, "cadence", e.target.value)}
                    />
                  </div>
                  <div class="rec-card-row">
                    <label>prompt</label>
                    <textarea
                      onInput=${(e) => patchEdit(r.id, "prompt", e.target.value)}
                    >${effective(r, "prompt")}</textarea>
                  </div>
                  <div class="rec-card-meta">
                    <span>tz: ${r.tz || "UTC"}</span>
                    <span title=${r.next_fire_at || ""}>next: ${_formatRelative(r.next_fire_at)}</span>
                    <span title=${r.last_fired_at || ""}>last: ${_formatRelative(r.last_fired_at)}</span>
                  </div>
                  <div class="rec-actions">
                    ${hasEdits(r.id)
                      ? html`<button
                              onClick=${() => saveRow(r)}
                              disabled=${busy || rowSaveDisabled(r)}
                              title=${rowSaveDisabled(r) ? "Invalid input — fix above" : "Save edits"}
                            >save</button>
                            <button onClick=${() => clearEdit(r.id)} disabled=${busy}>discard</button>`
                      : html`<button onClick=${() => toggleRow(r)} disabled=${busy}>${r.enabled ? "disable" : "enable"}</button>
                            <button class="rec-delete" onClick=${() => deleteRow(r.id)} disabled=${busy}>delete</button>`}
                  </div>
                </div>`)}
          <div class="rec-card" style="margin-top:8px">
            <div class="rec-card-row">
              <label>schedule</label>
              <input
                type="text"
                placeholder="daily 09:00"
                value=${newCron.cadence}
                onInput=${(e) => setNewCron({ ...newCron, cadence: e.target.value })}
              />
            </div>
            <div class="rec-card-row">
              <label>prompt</label>
              <textarea
                placeholder="morning summary"
                value=${newCron.prompt}
                onInput=${(e) => setNewCron({ ...newCron, prompt: e.target.value })}
              ></textarea>
            </div>
            <div class="rec-card-meta">
              <span>tz: ${tz}</span>
            </div>
            <button class="rec-add-btn" onClick=${addCron} disabled=${busy}>+ add cron</button>
          </div>
        </section>
      </div>
    </aside>
  `;
}

// ------------------------------------------------------------------
// environment pane (right side): tasks + cost + timeline
// ------------------------------------------------------------------

function EnvPane({ agents, tasks, conversations, openSlots, serverStatus, activeProjectId, onCreateTask, onClose, onResizerDown }) {
  const [exporting, setExporting] = useState(false);

  const exportTeam = useCallback(async () => {
    if (exporting || !openSlots || openSlots.length === 0) return;
    setExporting(true);
    try {
      // Fetch each pane's events with bounded concurrency. Promise.all
      // over all 11 open panes used to fire 11×500-event queries
      // simultaneously, hammering the events table (each query
      // includes JSON-extract filtering for fan-out semantics) and
      // momentarily blocking the WS event writer behind the read
      // bursts. Three workers in flight is enough to overlap network
      // I/O without saturating the DB.
      const TEAM_EXPORT_CONCURRENCY = 3;
      const results = new Array(openSlots.length);
      let idx = 0;
      async function worker() {
        while (idx < openSlots.length) {
          const i = idx++;
          const slot = openSlots[i];
          try {
            const res = await authFetch(
              `/api/events?agent=${encodeURIComponent(slot)}&limit=500`
            );
            if (!res.ok) {
              results[i] = { slot, events: [] };
              continue;
            }
            const data = await res.json();
            results[i] = { slot, events: (data.events || []).map(unwrapPersisted) };
          } catch (e) {
            console.error("team export: pane fetch failed", slot, e);
            results[i] = { slot, events: [] };
          }
        }
      }
      const workers = [];
      for (let w = 0; w < Math.min(TEAM_EXPORT_CONCURRENCY, openSlots.length); w++) {
        workers.push(worker());
      }
      await Promise.all(workers);
      const sections = results.map(({ slot, events }) => {
        const agent = agents.find((a) => a.id === slot);
        return formatEventsAsMarkdown(events, { slot, agent, headingLevel: 2 });
      });
      const header = [
        "# Team of Ten — conversation export",
        `Exported ${new Date().toISOString()}`,
        `Panes: ${openSlots.join(", ")}`,
        "",
      ].join("\n");
      const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      downloadMarkdown(`team-${stamp}.md`, header + sections.join("\n---\n\n"));
    } finally {
      setExporting(false);
    }
  }, [exporting, openSlots, agents]);

  return html`
    <aside class="env-pane">
      <div
        class="env-resizer"
        onPointerDown=${onResizerDown}
        title="Drag to resize"
      ></div>
      <header class="env-head">
        <span class="env-title">Environment</span>
        <button
          class="env-export"
          onClick=${exportTeam}
          disabled=${exporting || !openSlots || openSlots.length === 0}
          title=${"Export all open panes as a single markdown file"
            + (openSlots && openSlots.length > 0 ? ` (${openSlots.length} pane${openSlots.length > 1 ? "s" : ""})` : "")}
        >${exporting ? "…" : "↓"}</button>
        <button class="env-close" onClick=${onClose} title="Collapse">×</button>
      </header>
      <div class="env-body">
        <${EnvAttentionSection} conversations=${conversations} />
        <${EnvKDriveStatusSection} conversations=${conversations} />
        <${EnvTasksSection} tasks=${tasks} onCreate=${onCreateTask} />
        <${EnvCostSection} agents=${agents} serverStatus=${serverStatus} />
        <${EnvObjectivesSection}
          conversations=${conversations}
          activeProjectId=${activeProjectId}
        />
        <${EnvCoachTodosSection}
          conversations=${conversations}
          activeProjectId=${activeProjectId}
        />
        <${EnvInboxSection} conversations=${conversations} />
        <${EnvMemorySection} conversations=${conversations} />
        <${EnvDecisionsSection} conversations=${conversations} />
        <${EnvTruthProposalsSection} conversations=${conversations} />
        <${EnvTimelineSection} conversations=${conversations} />
      </div>
    </aside>
  `;
}

// Human-attention escalations emitted by coord_request_human. The UI
// surfaces them prominently until the user clicks "dismiss" — dismissal
// is local-only (by __id in localStorage), because the DB event is an
// immutable historical record.
const ATTENTION_DISMISSED_KEY = "harness_attention_dismissed_v1";

function loadDismissedAttention() {
  try {
    const raw = localStorage.getItem(ATTENTION_DISMISSED_KEY);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch (_) {
    return new Set();
  }
}

function saveDismissedAttention(ids) {
  try {
    // Cap retained ids to the most recent 200 so this doesn't grow
    // without bound.
    const arr = Array.from(ids).slice(-200);
    localStorage.setItem(ATTENTION_DISMISSED_KEY, JSON.stringify(arr));
  } catch (_) {
    // disabled localStorage — silent no-op.
  }
}

function EnvAttentionSection({ conversations }) {
  const [dismissed, setDismissed] = useState(() => loadDismissedAttention());
  const [persisted, setPersisted] = useState([]);

  // Pending AskUserQuestion + ExitPlanMode interactions. Both go
  // through can_use_tool and survive reloads via /api/questions/pending
  // and /api/plans/pending. Separate from human_attention (fire-and-
  // forget escalations).
  const [pendingQuestions, setPendingQuestions] = useState([]);
  const [pendingPlans, setPendingPlans] = useState([]);

  const liveCount = useMemo(() => {
    let n = 0;
    for (const list of conversations.values()) {
      for (const ev of list) {
        if (ev.type === "human_attention"
            || ev.type === "pending_question"
            || ev.type === "question_answered"
            || ev.type === "question_cancelled"
            || ev.type === "pending_plan"
            || ev.type === "plan_decided"
            || ev.type === "plan_cancelled"
            || ev.type === "interaction_extended") {
          n++;
        }
      }
    }
    return n;
  }, [conversations]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await authFetch(
          "/api/events?type=human_attention&limit=100"
        );
        if (!res.ok) return;
        const data = await res.json();
        if (cancelled) return;
        setPersisted((data.events || []).map(unwrapPersisted));
      } catch (e) {
        console.error("attention history load failed", e);
      }
      try {
        const res2 = await authFetch("/api/questions/pending");
        if (!res2.ok) return;
        const data2 = await res2.json();
        if (cancelled) return;
        setPendingQuestions(Array.isArray(data2.pending) ? data2.pending : []);
      } catch (e) {
        console.error("pending questions load failed", e);
      }
      try {
        const res3 = await authFetch("/api/plans/pending");
        if (!res3.ok) return;
        const data3 = await res3.json();
        if (cancelled) return;
        setPendingPlans(Array.isArray(data3.pending) ? data3.pending : []);
      } catch (e) {
        console.error("pending plans load failed", e);
      }
    })();
    return () => { cancelled = true; };
  }, [liveCount]);

  const open = useMemo(() => {
    const seen = new Set();
    const all = [];
    // Live pending questions.
    for (const pq of pendingQuestions) {
      if (pq.route !== "human") continue;
      const k = `pq:${pq.correlation_id}`;
      if (seen.has(k)) continue;
      seen.add(k);
      all.push({
        __key: k,
        type: "pending_question",
        agent_id: pq.agent_id,
        correlation_id: pq.correlation_id,
        questions: pq.questions || [],
        ts: pq.created_at,
        deadline_at: pq.deadline_at || null,
        subject: pq.questions && pq.questions[0]
          ? pq.questions[0].question
          : "Question",
      });
    }
    // Live pending plans.
    for (const pp of pendingPlans) {
      if (pp.route !== "human") continue;
      const k = `pp:${pp.correlation_id}`;
      if (seen.has(k)) continue;
      seen.add(k);
      all.push({
        __key: k,
        type: "pending_plan",
        agent_id: pp.agent_id,
        correlation_id: pp.correlation_id,
        plan: pp.plan || "",
        ts: pp.created_at,
        deadline_at: pp.deadline_at || null,
        subject: "Plan approval — " + (pp.agent_id || ""),
      });
    }
    for (const list of conversations.values()) {
      for (const ev of list) {
        if (ev.type === "pending_question" && ev.route === "human") {
          const k = `pq:${ev.correlation_id}`;
          if (seen.has(k)) continue;
          seen.add(k);
          all.push({ ...ev, __key: k });
          continue;
        }
        if (ev.type === "pending_plan" && ev.route === "human") {
          const k = `pp:${ev.correlation_id}`;
          if (seen.has(k)) continue;
          seen.add(k);
          all.push({ ...ev, __key: k, subject: "Plan approval — " + (ev.agent_id || "") });
          continue;
        }
        if (ev.type === "question_answered" || ev.type === "question_cancelled") {
          const k = `pq:${ev.correlation_id}`;
          seen.add(k);
          for (let i = all.length - 1; i >= 0; i--) {
            if (all[i].__key === k) all.splice(i, 1);
          }
        }
        if (ev.type === "plan_decided" || ev.type === "plan_cancelled") {
          const k = `pp:${ev.correlation_id}`;
          seen.add(k);
          for (let i = all.length - 1; i >= 0; i--) {
            if (all[i].__key === k) all.splice(i, 1);
          }
        }
      }
    }
    // Persisted human_attention next (canonical ids).
    for (const ev of persisted) {
      if (ev.type !== "human_attention") continue;
      const k = ev.__id != null ? `ha:${ev.__id}` : `ha:${ev.ts}:${ev.agent_id}`;
      if (seen.has(k)) continue;
      seen.add(k);
      all.push({ ...ev, __key: k });
    }
    for (const list of conversations.values()) {
      for (const ev of list) {
        if (ev.type !== "human_attention") continue;
        const k = ev.__id != null ? `ha:${ev.__id}` : `ha:${ev.ts}:${ev.agent_id}`;
        if (seen.has(k)) continue;
        seen.add(k);
        all.push({ ...ev, __key: k });
      }
    }
    const out = all.filter((ev) => !dismissed.has(ev.__key));
    out.sort((a, b) => (b.ts || "").localeCompare(a.ts || ""));
    return out;
  }, [conversations, persisted, pendingQuestions, pendingPlans, dismissed]);

  const dismiss = useCallback((key) => {
    setDismissed((prev) => {
      const next = new Set(prev);
      next.add(key);
      saveDismissedAttention(next);
      return next;
    });
  }, []);
  const dismissAll = useCallback(() => {
    setDismissed((prev) => {
      const next = new Set(prev);
      for (const ev of open) next.add(ev.__key);
      saveDismissedAttention(next);
      return next;
    });
  }, [open]);

  if (open.length === 0) return null;

  return html`
    <section class="env-section env-attention">
      <h3 class="env-section-title">
        ⚠ Attention <span class="env-count">${open.length}</span>
        <button class="env-attention-dismiss-all" onClick=${dismissAll}
          title="Dismiss all escalations">dismiss all</button>
      </h3>
      <div class="env-attention-list">
        ${open.map(
          (ev) => html`
            <div
              class=${"env-attention-item " + (ev.urgency === "blocker" ? "blocker" : "normal")}
              key=${ev.__key}
            >
              <div class="env-attention-head">
                <span class="env-attention-who">${ev.agent_id}</span>
                ${ev.urgency === "blocker"
                  ? html`<span class="env-attention-pill">BLOCKER</span>`
                  : null}
                <span class="env-attention-ts">${(ev.ts || "").slice(11, 16)}</span>
                <button class="env-attention-dismiss"
                  onClick=${() => dismiss(ev.__key)}
                  title="Dismiss">×</button>
              </div>
              <div class="env-attention-subject">${ev.subject}</div>
              ${ev.type === "pending_question" && Array.isArray(ev.questions) && ev.questions.length > 0
                ? html`<${QuestionForm}
                    event=${ev}
                    onSubmitted=${() => dismiss(ev.__key)}
                  />`
                : ev.type === "pending_plan"
                ? html`<${PlanApprovalForm}
                    event=${ev}
                    onSubmitted=${() => dismiss(ev.__key)}
                  />`
                : html`<div class="env-attention-body">${ev.body}</div>`}
            </div>
          `
        )}
      </div>
    </section>
  `;
}

// Structured-question answer form, rendered inside an attention item
// Shared countdown + extend button for pending_question and
// pending_plan interactions. Renders ⏱ MM:SS with colour shifting
// amber→red as the deadline approaches, plus a +30 min button that
// POSTs /api/interactions/<id>/extend. deadline_at comes from the
// server (authoritative); local `now` ticks every 1s to update the
// display without another round-trip.
function InteractionCountdown({ correlationId, deadlineAt, onExtended }) {
  const deadlineMs = useMemo(() => {
    if (!deadlineAt) return null;
    const t = Date.parse(deadlineAt);
    return isNaN(t) ? null : t;
  }, [deadlineAt]);
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!deadlineMs) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [deadlineMs]);

  if (!deadlineMs || !correlationId) return null;
  const remainingMs = Math.max(0, deadlineMs - now);
  const remainingS = Math.floor(remainingMs / 1000);
  const mm = String(Math.floor(remainingS / 60)).padStart(2, "0");
  const ss = String(remainingS % 60).padStart(2, "0");
  const cls = remainingS < 30
    ? "countdown-red"
    : remainingS < 120
    ? "countdown-amber"
    : "countdown-green";

  const extend = async () => {
    setBusy(true);
    setErr(null);
    try {
      const res = await authFetch(
        "/api/interactions/" + encodeURIComponent(correlationId) + "/extend",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ seconds: 1800 }),
        }
      );
      if (!res.ok) {
        const b = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}${b ? " — " + b.slice(0, 120) : ""}`);
      }
      const data = await res.json();
      if (onExtended && data && data.deadline_at) onExtended(data.deadline_at);
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setBusy(false);
    }
  };

  return html`<div class="interaction-countdown">
    <span class=${"countdown-clock " + cls} title="Time until this interaction auto-expires">
      ⏱ ${mm}:${ss}
    </span>
    <button
      class="countdown-extend"
      disabled=${busy}
      onClick=${extend}
      title="Push the deadline out by 30 minutes"
    >${busy ? "…" : "+30 min"}</button>
    ${err
      ? html`<span class="countdown-err" title=${err}>extend failed</span>`
      : null}
  </div>`;
}


// when the event carries a `questions` array (from coord_ask_question).
// Radios for single-select, checkboxes for multi-select, plus an
// "Other…" free-text per question. On submit, answers are formatted
// as markdown and POST'd to /api/messages so they land in the asking
// agent's inbox + auto-wake them to resume work.
function QuestionForm({ event, onSubmitted }) {
  const questions = Array.isArray(event.questions) ? event.questions : [];
  // state[i] = { selected: Set<label> | string | null, other: string }
  const [state, setState] = useState(() =>
    questions.map((q) => ({
      selected: q.multi_select ? new Set() : null,
      other: "",
    }))
  );
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState(null);

  const pick = (qi, label, multi) => {
    setState((prev) => prev.map((s, i) => {
      if (i !== qi) return s;
      if (multi) {
        const next = new Set(s.selected);
        if (next.has(label)) next.delete(label);
        else next.add(label);
        return { ...s, selected: next };
      }
      return { ...s, selected: label };
    }));
  };
  const setOther = (qi, value) => {
    setState((prev) => prev.map((s, i) =>
      i === qi ? { ...s, other: value } : s
    ));
  };

  const canSubmit = questions.every((q, i) => {
    const s = state[i];
    if (q.multi_select) {
      return (s.selected && s.selected.size > 0) || s.other.trim();
    }
    return s.selected != null || s.other.trim();
  });

  const submit = async () => {
    setSending(true);
    setErr(null);
    try {
      // Shape answers to the SDK's expected record<string,string> where
      // keys are exact question text and values are selected label(s).
      // Multi-select joins with ', ' per docs; free-text uses the
      // literal string instead of 'Other'.
      const answers = {};
      questions.forEach((q, i) => {
        const s = state[i];
        const picks = [];
        if (q.multi_select) {
          if (s.selected) for (const label of s.selected) picks.push(label);
        } else if (s.selected != null) {
          picks.push(s.selected);
        }
        if (s.other.trim()) picks.push(s.other.trim());
        answers[q.question] = picks.join(", ");
      });
      const cid = event.correlation_id;
      if (!cid) {
        throw new Error("no correlation_id on event (stale page — refresh)");
      }
      const res = await authFetch(
        "/api/questions/" + encodeURIComponent(cid) + "/answer",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ answers }),
        }
      );
      if (!res.ok) {
        const body = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}${body ? " — " + body.slice(0, 120) : ""}`);
      }
      onSubmitted();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setSending(false);
    }
  };

  // Local override so the countdown reflects extensions without a
  // full round-trip through the pending-list re-fetch.
  const [localDeadline, setLocalDeadline] = useState(event.deadline_at || null);

  if (!questions.length) return null;
  return html`<div class="question-form">
    <${InteractionCountdown}
      correlationId=${event.correlation_id}
      deadlineAt=${localDeadline}
      onExtended=${(d) => setLocalDeadline(d)}
    />
    ${questions.map((q, qi) => {
      const s = state[qi];
      const multi = !!q.multi_select;
      return html`<div class="question-block" key=${qi}>
        ${q.header
          ? html`<div class="question-header">${q.header}</div>`
          : null}
        <div class="question-text">
          ${multi
            ? html`<span class="question-multi-tag">multi-select</span>`
            : null}
          ${q.question}
        </div>
        <div class="question-options">
          ${(q.options || []).map((opt, oi) => {
            const checked = multi
              ? (s.selected && s.selected.has(opt.label))
              : s.selected === opt.label;
            const optKey = `q${qi}-o${oi}`;
            return html`<label class="question-option" key=${optKey}>
              <input
                type=${multi ? "checkbox" : "radio"}
                name=${`q${qi}`}
                checked=${checked}
                onChange=${() => pick(qi, opt.label, multi)}
              />
              <span class="question-option-label">${opt.label}</span>
              ${opt.description
                ? html`<span class="question-option-desc">${opt.description}</span>`
                : null}
            </label>`;
          })}
          <label class="question-option question-other">
            <span class="question-option-label">Other:</span>
            <input
              type="text"
              class="question-other-input"
              placeholder="free-text answer"
              value=${s.other}
              onInput=${(e) => setOther(qi, e.target.value)}
            />
          </label>
        </div>
      </div>`;
    })}
    <div class="question-form-actions">
      ${err ? html`<span class="question-form-err">${err}</span>` : null}
      <button
        class="primary"
        disabled=${sending || !canSubmit}
        onClick=${submit}
      >${sending ? "sending…" : "Send answers"}</button>
    </div>
  </div>`;
}


// Plan-approval form rendered inside an attention item when a
// pending_plan event arrives (ExitPlanMode via can_use_tool). The
// plan is rendered in a scrollable pre block; three buttons map to
// the /api/plans/<id>/decision contract. Reject and Approve-with-
// comments require a non-empty comments textarea (also enforced
// server-side). Plain Approve is a single click.
function PlanApprovalForm({ event, onSubmitted }) {
  const [comments, setComments] = useState("");
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState(null);
  const [localDeadline, setLocalDeadline] = useState(event.deadline_at || null);
  const planText = event.plan || "";

  const submit = async (decision) => {
    if (decision !== "approve" && !comments.trim()) {
      setErr("Comments required for reject and approve-with-comments.");
      return;
    }
    setSending(true);
    setErr(null);
    try {
      const cid = event.correlation_id;
      if (!cid) throw new Error("no correlation_id on event");
      const res = await authFetch(
        "/api/plans/" + encodeURIComponent(cid) + "/decision",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            decision,
            comments: comments.trim() || null,
          }),
        }
      );
      if (!res.ok) {
        const body = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}${body ? " — " + body.slice(0, 120) : ""}`);
      }
      onSubmitted();
    } catch (e) {
      setErr(String(e.message || e));
    } finally {
      setSending(false);
    }
  };

  return html`<div class="plan-form">
    <${InteractionCountdown}
      correlationId=${event.correlation_id}
      deadlineAt=${localDeadline}
      onExtended=${(d) => setLocalDeadline(d)}
    />
    <pre class="plan-body">${planText}</pre>
    <textarea
      class="plan-comments"
      placeholder="Comments (required for reject / approve-with-comments)"
      value=${comments}
      onInput=${(e) => setComments(e.target.value)}
      rows=${3}
      disabled=${sending}
    />
    <div class="plan-actions">
      ${err ? html`<span class="plan-form-err">${err}</span>` : null}
      <button
        disabled=${sending}
        onClick=${() => submit("approve")}
        class="primary"
        title="Plan executes as-is"
      >${sending ? "…" : "Approve"}</button>
      <button
        disabled=${sending}
        onClick=${() => submit("approve_with_comments")}
        title="Plan executes; comments land in the agent's inbox"
      >${sending ? "…" : "Approve with comments"}</button>
      <button
        disabled=${sending}
        onClick=${() => submit("reject")}
        class="plan-reject"
        title="Agent stays in plan mode and revises per your comments"
      >${sending ? "…" : "Reject (revise)"}</button>
    </div>
  </div>`;
}


const MESSAGE_RECIPIENTS = [
  "coach",
  ...Array.from({ length: 10 }, (_, i) => "p" + (i + 1)),
  "broadcast",
];

function EnvInboxSection({ conversations }) {
  const [msgs, setMsgs] = useState([]);
  const [openId, setOpenId] = useState(null);
  const [composing, setComposing] = useState(false);
  const [to, setTo] = useState("coach");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [priority, setPriority] = useState("normal");
  const [sending, setSending] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await authFetch("/api/messages?limit=50");
      if (!res.ok) return;
      const data = await res.json();
      setMsgs(data.messages || []);
    } catch (e) {
      console.error("loadMessages failed", e);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const messageEventCount = useMemo(() => {
    let n = 0;
    for (const list of conversations.values()) {
      for (const ev of list) if (ev.type === "message_sent") n++;
    }
    return n;
  }, [conversations]);
  useEffect(() => {
    if (messageEventCount > 0) load();
  }, [messageEventCount, load]);

  const send = useCallback(async () => {
    if (!body.trim()) return;
    setSending(true);
    try {
      const res = await authFetch("/api/messages", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          to,
          subject: subject.trim() || null,
          body,
          priority,
        }),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      setBody("");
      setSubject("");
      setComposing(false);
    } catch (e) {
      console.error("send message failed", e);
    } finally {
      setSending(false);
    }
  }, [to, subject, body, priority]);

  return html`
    <section class="env-section">
      <h3 class="env-section-title">
        Messages <span class="env-count">${msgs.length}</span>
        <button
          class="env-attention-dismiss-all"
          style="margin-left: auto; border-color: var(--accent); color: var(--accent);"
          onClick=${() => setComposing((v) => !v)}
        >${composing ? "cancel" : "+ send"}</button>
      </h3>
      ${msgs.length === 0 && !composing
        ? html`<div class="env-empty">(no messages — click "+ send" to start)</div>`
        : null}
      ${composing
        ? html`<div class="env-msg-composer">
            <div class="env-msg-composer-row">
              <label class="env-msg-composer-label">To</label>
              <select value=${to} onChange=${(e) => setTo(e.target.value)}>
                ${MESSAGE_RECIPIENTS.map(
                  (r) => html`<option value=${r}>${r}</option>`
                )}
              </select>
              <select value=${priority} onChange=${(e) => setPriority(e.target.value)}>
                <option value="normal">normal</option>
                <option value="interrupt">interrupt</option>
              </select>
            </div>
            <input
              type="text"
              class="env-msg-composer-subject"
              placeholder="subject (optional)"
              value=${subject}
              onInput=${(e) => setSubject(e.target.value)}
            />
            <textarea
              class="env-msg-composer-body"
              placeholder="body…"
              value=${body}
              onInput=${(e) => setBody(e.target.value)}
              rows=${3}
            ></textarea>
            <div class="env-msg-composer-row">
              <button
                class="primary"
                style="flex: 1"
                disabled=${sending || !body.trim()}
                onClick=${send}
              >${sending ? "sending…" : "send"}</button>
            </div>
          </div>`
        : null}
      <div class="env-decision-list">
        ${msgs.map((m) => {
          const preview = (m.subject || m.body || "").replace(/\s+/g, " ").slice(0, 80);
          const isOpen = openId === m.id;
          const urgent = m.priority === "interrupt";
          return html`
            <div
              class=${"env-decision" + (urgent ? " env-msg-urgent" : "")}
              key=${m.id}
            >
              <button
                class="env-decision-head"
                onClick=${() => setOpenId(isOpen ? null : m.id)}
              >
                <span class="env-decision-arrow">${isOpen ? "▾" : "▸"}</span>
                <span class="env-decision-title">
                  ${m.from_id} → ${m.to_id}${urgent ? " ⚠" : ""}
                </span>
                <span class="env-decision-meta">${(m.sent_at || "").slice(11, 16)}</span>
              </button>
              ${isOpen
                ? html`<div class="env-msg-body">
                    ${m.subject ? html`<div class="env-msg-subject">${m.subject}</div>` : null}
                    <pre class="env-decision-body">${m.body}</pre>
                  </div>`
                : html`<div class="env-msg-preview">${preview}</div>`}
            </div>
          `;
        })}
      </div>
    </section>
  `;
}

// kDrive sync banner — surfaces the most recent kdrive_sync_failed
// events emitted by server/project_sync.py when retries exhaust.
// Auto-clears when a fresh successful sync overrides the row's
// sync_state; we render last 5 minutes of failures only so a stale
// red banner disappears without user action.
const KDRIVE_FAILURE_TTL_MS = 5 * 60 * 1000;

function EnvKDriveStatusSection({ conversations }) {
  const failures = useMemo(() => {
    const now = Date.now();
    const out = [];
    const seen = new Set();
    for (const list of conversations.values()) {
      for (const ev of list) {
        if (ev.type !== "kdrive_sync_failed") continue;
        // Dedup by (op, project_id, tree, path) — multiple files can
        // fail in one cycle but we want the user to see one row per
        // recurring path.
        const key = `${ev.op || "?"}::${ev.project_id || ""}::${ev.tree || ""}::${ev.path || ""}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const t = Date.parse(ev.ts || "");
        if (isFinite(t) && now - t < KDRIVE_FAILURE_TTL_MS) {
          out.push(ev);
        }
      }
    }
    return out.slice(-10).reverse();
  }, [conversations]);

  if (failures.length === 0) return null;
  return html`
    <section class="env-section env-kdrive-failed">
      <h2>kDrive sync errors (${failures.length})</h2>
      <div class="env-kdrive-list">
        ${failures.map((ev) => html`
          <div class="env-kdrive-row" key=${ev.__id || (ev.ts + ev.path)}>
            <span class="env-kdrive-op">${ev.op || "sync"}</span>
            <span class="env-kdrive-path">
              ${ev.tree === "wiki" ? "wiki/" : (ev.project_id ? ev.project_id + "/" : "")}${ev.path || "?"}
            </span>
            <span class="env-kdrive-err" title=${ev.error || ""}>${(ev.error || "").slice(0, 80)}</span>
          </div>
        `)}
      </div>
      <div class="env-kdrive-hint">
        Files left local-only; next push cycle will retry. Check kDrive auth or disk space if this persists.
      </div>
    </section>
  `;
}

// Project objectives — multiline editor + Save (recurrence-specs.md
// §12.3). Reads/writes the per-project project-objectives.md via the
// HTTP shim. Refreshes on `objectives_updated` events.
function EnvObjectivesSection({ conversations, activeProjectId }) {
  const [text, setText] = useState("");
  const [pending, setPending] = useState(null); // null = no edit in flight
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!activeProjectId) return;
    try {
      const res = await authFetch(
        `/api/projects/${encodeURIComponent(activeProjectId)}/objectives`
      );
      if (!res.ok) return;
      const data = await res.json();
      setText(data.text || "");
      setPending(null);
    } catch (e) {
      console.error("loadObjectives failed", e);
    }
  }, [activeProjectId]);

  useEffect(() => { load(); }, [load]);

  // Refresh on `objectives_updated` from any agent.
  const eventCount = useMemo(() => {
    let n = 0;
    for (const list of conversations.values()) {
      for (const ev of list) if (ev.type === "objectives_updated") n++;
    }
    return n;
  }, [conversations]);
  useEffect(() => {
    if (eventCount > 0) load();
  }, [eventCount, load]);

  const save = useCallback(async () => {
    if (pending == null || !activeProjectId) return;
    setSaving(true);
    setError("");
    try {
      const res = await authFetch(
        `/api/projects/${encodeURIComponent(activeProjectId)}/objectives`,
        {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ text: pending }),
        }
      );
      if (!res.ok) {
        const txt = await res.text().catch(() => "");
        setError(`HTTP ${res.status}${txt ? ": " + txt.slice(0, 120) : ""}`);
        return;
      }
      setText(pending);
      setPending(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }, [pending, activeProjectId]);

  const dirty = pending != null && pending !== text;

  return html`
    <section class="env-section">
      <h3 class="env-section-title">
        Objectives
      </h3>
      <textarea
        class="env-msg-composer-body"
        placeholder="What is this project trying to accomplish? Free markdown."
        value=${pending != null ? pending : text}
        onInput=${(e) => setPending(e.target.value)}
        rows="6"
      ></textarea>
      ${error ? html`<div class="rec-error">${error}</div>` : null}
      <div class="rec-actions">
        ${dirty
          ? html`
              <button onClick=${save} disabled=${saving || !activeProjectId}>
                ${saving ? "saving…" : "save"}
              </button>
              <button onClick=${() => setPending(null)} disabled=${saving}>
                discard
              </button>`
          : null}
      </div>
    </section>
  `;
}

// Coach todos — checkbox list of OPEN todos with click-to-expand
// description, strikethrough on complete, "+ add" form, link to
// archive (recurrence-specs.md §12.3). Refresh on coach_todo_*
// events from any agent.
function EnvCoachTodosSection({ conversations, activeProjectId }) {
  const [todos, setTodos] = useState([]);
  const [archive, setArchive] = useState([]);
  const [openId, setOpenId] = useState(null);
  const [composing, setComposing] = useState(false);
  const [showArchive, setShowArchive] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newDue, setNewDue] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [busyIds, setBusyIds] = useState(new Set());
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!activeProjectId) return;
    try {
      const res = await authFetch(
        `/api/projects/${encodeURIComponent(activeProjectId)}/coach-todos`
      );
      if (res.ok) {
        const data = await res.json();
        setTodos(data.todos || []);
      }
    } catch (e) {
      console.error("loadTodos failed", e);
    }
  }, [activeProjectId]);

  const loadArchive = useCallback(async () => {
    if (!activeProjectId) return;
    try {
      const res = await authFetch(
        `/api/projects/${encodeURIComponent(activeProjectId)}/coach-todos/archive`
      );
      if (res.ok) {
        const data = await res.json();
        setArchive(data.todos || []);
      }
    } catch (e) {
      console.error("loadArchive failed", e);
    }
  }, [activeProjectId]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (showArchive) loadArchive();
  }, [showArchive, loadArchive]);

  const eventCount = useMemo(() => {
    let n = 0;
    for (const list of conversations.values()) {
      for (const ev of list) {
        if (
          ev.type === "coach_todo_added" ||
          ev.type === "coach_todo_completed" ||
          ev.type === "coach_todo_updated"
        ) n++;
      }
    }
    return n;
  }, [conversations]);
  useEffect(() => {
    if (eventCount > 0) {
      load();
      if (showArchive) loadArchive();
    }
  }, [eventCount, load, loadArchive, showArchive]);

  function markBusy(id, on) {
    setBusyIds((prev) => {
      const next = new Set(prev);
      if (on) next.add(id); else next.delete(id);
      return next;
    });
  }

  const complete = useCallback(async (id) => {
    if (!activeProjectId) return;
    markBusy(id, true);
    setError("");
    try {
      const res = await authFetch(
        `/api/projects/${encodeURIComponent(activeProjectId)}/coach-todos/${encodeURIComponent(id)}/complete`,
        { method: "POST" }
      );
      if (!res.ok) {
        const txt = await res.text().catch(() => "");
        setError(`HTTP ${res.status}${txt ? ": " + txt.slice(0, 120) : ""}`);
      } else {
        load();
      }
    } catch (e) {
      setError(String(e));
    } finally {
      markBusy(id, false);
    }
  }, [activeProjectId, load]);

  const addNew = useCallback(async () => {
    const title = newTitle.trim();
    if (!title || !activeProjectId) return;
    setError("");
    try {
      const res = await authFetch(
        `/api/projects/${encodeURIComponent(activeProjectId)}/coach-todos`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            title,
            description: newDesc,
            due: newDue.trim() || null,
          }),
        }
      );
      if (!res.ok) {
        const txt = await res.text().catch(() => "");
        setError(`HTTP ${res.status}${txt ? ": " + txt.slice(0, 120) : ""}`);
        return;
      }
      setNewTitle("");
      setNewDue("");
      setNewDesc("");
      setComposing(false);
      load();
    } catch (e) {
      setError(String(e));
    }
  }, [newTitle, newDue, newDesc, activeProjectId, load]);

  return html`
    <section class="env-section">
      <h3 class="env-section-title">
        Coach todos <span class="env-count">${todos.length}</span>
        <button
          class="env-attention-dismiss-all"
          style="margin-left: auto; border-color: var(--accent); color: var(--accent);"
          onClick=${() => (composing ? setComposing(false) : setComposing(true))}
        >${composing ? "cancel" : "+ add"}</button>
      </h3>
      ${composing
        ? html`<div class="env-msg-composer">
            <input
              type="text"
              class="env-msg-composer-subject"
              placeholder="title"
              value=${newTitle}
              onInput=${(e) => setNewTitle(e.target.value)}
            />
            <input
              type="text"
              class="env-msg-composer-subject"
              placeholder="due (optional, YYYY-MM-DD)"
              value=${newDue}
              onInput=${(e) => setNewDue(e.target.value)}
            />
            <textarea
              class="env-msg-composer-body"
              placeholder="description (optional)"
              value=${newDesc}
              onInput=${(e) => setNewDesc(e.target.value)}
              rows="3"
            ></textarea>
            <div class="rec-actions">
              <button onClick=${addNew} disabled=${!newTitle.trim()}>save</button>
            </div>
          </div>`
        : null}
      ${error ? html`<div class="rec-error">${error}</div>` : null}
      ${todos.length === 0
        ? html`<div class="env-empty">No open todos.</div>`
        : todos.map((t) => html`
            <div
              class="env-todo-row"
              key=${t.id}
              style="display:flex;align-items:flex-start;gap:6px;padding:4px 0;border-bottom:1px solid var(--border);font-size:12px"
            >
              <input
                type="checkbox"
                onChange=${() => complete(t.id)}
                disabled=${busyIds.has(t.id)}
                style="margin-top:3px"
              />
              <div style="flex:1;min-width:0">
                <div
                  onClick=${() => setOpenId(openId === t.id ? null : t.id)}
                  style="cursor:pointer"
                >
                  <strong>${t.title}</strong>
                  ${t.due ? html`<span class="env-cost-hint" style="margin-left:6px">due ${t.due}</span>` : null}
                </div>
                ${openId === t.id && t.description
                  ? html`<div style="margin-top:4px;color:var(--muted);white-space:pre-wrap">${t.description}</div>`
                  : null}
              </div>
            </div>`)}
      <div class="rec-actions">
        <button onClick=${() => setShowArchive((v) => !v)}>
          ${showArchive ? "hide archive" : `show archive (${archive.length || "…"})`}
        </button>
      </div>
      ${showArchive
        ? html`<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border);font-size:11px;color:var(--muted)">
            ${archive.length === 0
              ? html`<div class="env-empty">No archived todos.</div>`
              : archive.map((t) => html`
                  <div key=${t.id} style="padding:2px 0;text-decoration:line-through">
                    ${t.title}
                    ${t.completed ? html`<span style="margin-left:6px">${t.completed.slice(0, 16)}</span>` : null}
                  </div>`)}
          </div>`
        : null}
    </section>
  `;
}

function EnvMemorySection({ conversations }) {
  const [docs, setDocs] = useState([]);
  const [openTopic, setOpenTopic] = useState(null);
  const [openBody, setOpenBody] = useState("");
  const [composing, setComposing] = useState(false);
  const [newTopic, setNewTopic] = useState("");
  const [newBody, setNewBody] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await authFetch("/api/memory");
      if (!res.ok) return;
      const data = await res.json();
      setDocs(data.docs || []);
    } catch (e) {
      console.error("loadMemory failed", e);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Refresh when any agent mutates memory (memory_updated events cross
  // all conversations — trigger on any of them).
  const memoryEventCount = useMemo(() => {
    let n = 0;
    for (const list of conversations.values()) {
      for (const ev of list) if (ev.type === "memory_updated") n++;
    }
    return n;
  }, [conversations]);
  useEffect(() => {
    if (memoryEventCount > 0) load();
  }, [memoryEventCount, load]);

  const toggle = useCallback(async (topic) => {
    if (openTopic === topic) {
      setOpenTopic(null);
      setOpenBody("");
      return;
    }
    setOpenTopic(topic);
    setOpenBody("loading…");
    try {
      const res = await authFetch("/api/memory/" + encodeURIComponent(topic));
      if (!res.ok) {
        setOpenBody("(failed to load: HTTP " + res.status + ")");
        return;
      }
      const data = await res.json();
      setOpenBody(data.content || "");
    } catch (e) {
      setOpenBody("(failed to load: " + e + ")");
    }
  }, [openTopic]);

  const startCompose = useCallback((seedTopic) => {
    setSaveErr("");
    setNewTopic(seedTopic || "");
    setNewBody("");
    setComposing(true);
  }, []);

  const save = useCallback(async () => {
    setSaveErr("");
    setSaving(true);
    try {
      const res = await authFetch("/api/memory", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ topic: newTopic, content: newBody }),
      });
      if (!res.ok) {
        const txt = await res.text();
        setSaveErr("HTTP " + res.status + ": " + txt.slice(0, 120));
        return;
      }
      setComposing(false);
      setNewTopic("");
      setNewBody("");
      load();
    } catch (e) {
      setSaveErr(String(e));
    } finally {
      setSaving(false);
    }
  }, [newTopic, newBody, load]);

  return html`
    <section class="env-section">
      <h3 class="env-section-title">
        Memory <span class="env-count">${docs.length}</span>
        <button
          class="env-attention-dismiss-all"
          style="margin-left: auto; border-color: var(--accent); color: var(--accent);"
          onClick=${() => (composing ? setComposing(false) : startCompose(""))}
        >${composing ? "cancel" : "+ write"}</button>
      </h3>
      ${composing
        ? html`<div class="env-msg-composer">
            <input
              type="text"
              class="env-msg-composer-subject"
              placeholder="topic (lowercase, 1–64 chars, a-z 0-9 -)"
              value=${newTopic}
              onInput=${(e) => setNewTopic(e.target.value)}
            />
            <textarea
              class="env-msg-composer-body"
              placeholder="content (markdown welcome)…"
              value=${newBody}
              onInput=${(e) => setNewBody(e.target.value)}
              rows=${5}
            ></textarea>
            ${saveErr ? html`<p class="muted" style="color: var(--err)">${saveErr}</p>` : null}
            <div class="env-msg-composer-row">
              <button
                class="primary"
                style="flex: 1"
                disabled=${saving || !newTopic.trim()}
                onClick=${save}
              >${saving ? "saving…" : "save (overwrites)"}</button>
            </div>
          </div>`
        : null}
      ${docs.length === 0
        ? html`<div class="env-empty">(empty — agents use coord_update_memory)</div>`
        : html`<div class="env-decision-list">
            ${docs.map(
              (d) => html`
                <div class="env-decision" key=${d.topic}>
                  <button
                    class="env-decision-head"
                    onClick=${() => toggle(d.topic)}
                  >
                    <span class="env-decision-arrow">${openTopic === d.topic ? "▾" : "▸"}</span>
                    <span class="env-decision-title">${d.topic}</span>
                    <span class="env-decision-meta">v${d.version} · ${d.last_updated_by}</span>
                  </button>
                  ${openTopic === d.topic
                    ? html`<pre class="env-decision-body">${openBody}</pre>`
                    : null}
                </div>
              `
            )}
          </div>`}
    </section>
  `;
}

function EnvDecisionsSection({ conversations }) {
  const [decisions, setDecisions] = useState([]);
  const [exists, setExists] = useState(true);
  const [openFile, setOpenFile] = useState(null);
  const [openBody, setOpenBody] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await authFetch("/api/decisions");
      if (!res.ok) return;
      const data = await res.json();
      setDecisions(data.decisions || []);
      setExists(data.exists !== false);
    } catch (e) {
      console.error("loadDecisions failed", e);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Refresh when a Coach writes a new decision (decision_written events
  // arrive across all conversations; trigger on any of them).
  const decisionEventCount = useMemo(() => {
    let n = 0;
    for (const list of conversations.values()) {
      for (const ev of list) if (ev.type === "decision_written") n++;
    }
    return n;
  }, [conversations]);
  useEffect(() => {
    if (decisionEventCount > 0) load();
  }, [decisionEventCount, load]);

  const toggle = useCallback(async (filename) => {
    if (openFile === filename) {
      setOpenFile(null);
      setOpenBody("");
      return;
    }
    setOpenFile(filename);
    setOpenBody("loading…");
    try {
      const res = await authFetch("/api/decisions/" + encodeURIComponent(filename));
      if (!res.ok) {
        setOpenBody("(failed to load: HTTP " + res.status + ")");
        return;
      }
      const data = await res.json();
      setOpenBody(data.content || "");
    } catch (e) {
      setOpenBody("(failed to load: " + e + ")");
    }
  }, [openFile]);

  return html`
    <section class="env-section">
      <h3 class="env-section-title">
        Decisions <span class="env-count">${decisions.length}</span>
      </h3>
      ${!exists
        ? html`<div class="env-cost-hint">
            (no decisions written yet — Coach uses coord_write_decision)
          </div>`
        : decisions.length === 0
        ? html`<div class="env-empty">(empty)</div>`
        : html`<div class="env-decision-list">
            ${decisions.map(
              (d) => html`
                <div class="env-decision" key=${d.filename}>
                  <button
                    class="env-decision-head"
                    onClick=${() => toggle(d.filename)}
                  >
                    <span class="env-decision-arrow">${openFile === d.filename ? "▾" : "▸"}</span>
                    <span class="env-decision-title">${d.title}</span>
                    <span class="env-decision-meta">${d.filename.slice(0, 10)}</span>
                  </button>
                  ${openFile === d.filename
                    ? html`<pre class="env-decision-body">${openBody}</pre>`
                    : null}
                </div>
              `
            )}
          </div>`}
    </section>
  `;
}

// Truth proposals section — Coach proposes via coord_propose_truth_update,
// the human approves or denies here. The approve action writes the file
// server-side; the deny action just marks the row. Pending proposals are
// the only ones rendered (resolved ones become events in the timeline).
function EnvTruthProposalsSection({ conversations }) {
  const [proposals, setProposals] = useState([]);
  const [openId, setOpenId] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [err, setErr] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await authFetch("/api/truth/proposals?status=pending");
      if (!res.ok) return;
      const data = await res.json();
      setProposals(Array.isArray(data.proposals) ? data.proposals : []);
    } catch (e) {
      console.error("load truth proposals failed", e);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Refresh on any related event so a Coach propose / human resolve
  // updates the list without a manual reload.
  const eventCount = useMemo(() => {
    let n = 0;
    for (const list of conversations.values()) {
      for (const ev of list) {
        if (
          ev.type === "truth_proposal_created" ||
          ev.type === "truth_proposal_approved" ||
          ev.type === "truth_proposal_denied" ||
          ev.type === "truth_proposal_cancelled"
        ) n++;
      }
    }
    return n;
  }, [conversations]);
  useEffect(() => {
    if (eventCount > 0) load();
  }, [eventCount, load]);

  const resolve = useCallback(async (id, action) => {
    setBusyId(id);
    setErr("");
    try {
      const res = await authFetch(
        "/api/truth/proposals/" + id + "/" + action,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        }
      );
      if (!res.ok) {
        const body = await res.text();
        throw new Error("HTTP " + res.status + ": " + body.slice(0, 200));
      }
      // Drop from local list; the next load will reconcile.
      setProposals((prev) => prev.filter((p) => p.id !== id));
      if (openId === id) setOpenId(null);
    } catch (e) {
      setErr("resolve failed: " + String(e));
    } finally {
      setBusyId(null);
    }
  }, [openId]);

  return html`
    <section class="env-section">
      <h3 class="env-section-title">
        Truth proposals <span class="env-count">${proposals.length}</span>
      </h3>
      ${err ? html`<div class="env-cost-hint">${err}</div>` : null}
      ${proposals.length === 0
        ? html`<div class="env-empty">(none pending)</div>`
        : html`<div class="env-decision-list">
            ${proposals.map((p) => {
              const isOpen = openId === p.id;
              return html`
                <div class="env-decision" key=${p.id}>
                  <button
                    class="env-decision-head"
                    onClick=${() => setOpenId(isOpen ? null : p.id)}
                  >
                    <span class="env-decision-arrow">${isOpen ? "▾" : "▸"}</span>
                    <span class="env-decision-title">truth/${p.path}</span>
                    <span class="env-decision-meta">${p.proposer_id}</span>
                  </button>
                  ${isOpen
                    ? html`
                        <div class="env-truth-summary">${p.summary}</div>
                        <pre class="env-decision-body">${p.proposed_content}</pre>
                        <div class="env-truth-actions">
                          <button
                            class="env-truth-approve"
                            disabled=${busyId === p.id}
                            onClick=${() => resolve(p.id, "approve")}
                          >${busyId === p.id ? "…" : "approve & write"}</button>
                          <button
                            class="env-truth-deny"
                            disabled=${busyId === p.id}
                            onClick=${() => resolve(p.id, "deny")}
                          >${busyId === p.id ? "…" : "deny"}</button>
                        </div>
                      `
                    : null}
                </div>
              `;
            })}
          </div>`}
    </section>
  `;
}

const TASK_STATUS_FILTERS = [
  { key: "active", label: "active", match: (s) => s !== "done" && s !== "cancelled" },
  { key: "all", label: "all", match: () => true },
  { key: "done", label: "done", match: (s) => s === "done" },
];

function EnvTasksSection({ tasks, onCreate }) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("normal");
  const [submitting, setSubmitting] = useState(false);
  const [filterKey, setFilterKey] = useState(
    () => localStorage.getItem("harness_task_filter_v1") || "active"
  );
  useEffect(() => {
    try { localStorage.setItem("harness_task_filter_v1", filterKey); }
    catch (_) { /* private mode */ }
  }, [filterKey]);

  const submit = useCallback(async (e) => {
    e.preventDefault();
    if (!title.trim()) return;
    setSubmitting(true);
    try {
      await onCreate({ title: title.trim(), description, priority });
      setTitle("");
      setDescription("");
      setPriority("normal");
    } catch (err) {
      console.error("create task failed", err);
    } finally {
      setSubmitting(false);
    }
  }, [title, description, priority, onCreate]);

  // show open/claimed/in_progress first, then blocked, then done/cancelled
  const statusOrder = { open: 0, claimed: 1, in_progress: 2, blocked: 3, done: 4, cancelled: 5 };
  const filter = TASK_STATUS_FILTERS.find((f) => f.key === filterKey) || TASK_STATUS_FILTERS[0];
  const filtered = tasks.filter((t) => filter.match(t.status));

  // Hierarchical layout: render roots first, then their children
  // indented below each. "Root" = no parent, or parent not in the
  // filtered set (so a filter hiding a parent still surfaces the
  // children as roots instead of making them disappear).
  const filteredIds = new Set(filtered.map((t) => t.id));
  const childrenOf = new Map();
  for (const t of filtered) {
    if (t.parent_id && filteredIds.has(t.parent_id)) {
      const arr = childrenOf.get(t.parent_id) || [];
      arr.push(t);
      childrenOf.set(t.parent_id, arr);
    }
  }
  const cmp = (a, b) => (statusOrder[a.status] ?? 9) - (statusOrder[b.status] ?? 9);
  const roots = filtered
    .filter((t) => !t.parent_id || !filteredIds.has(t.parent_id))
    .sort(cmp);
  for (const arr of childrenOf.values()) arr.sort(cmp);

  // Flatten tree with depth info for render. `seen` guards against
  // pathological parent_id cycles — current root-detection excludes
  // cycle members from the walk, but a future refactor that starts
  // from arbitrary tasks would infinite-loop without this check.
  const flatWithDepth = [];
  const seen = new Set();
  const walk = (task, depth) => {
    if (seen.has(task.id)) return;
    seen.add(task.id);
    flatWithDepth.push({ task, depth });
    const kids = childrenOf.get(task.id) || [];
    for (const k of kids) walk(k, depth + 1);
  };
  for (const r of roots) walk(r, 0);
  const sorted = flatWithDepth;

  return html`
    <section class="env-section">
      <h3 class="env-section-title">
        Tasks <span class="env-count">${sorted.length}/${tasks.length}</span>
        <span class="env-task-filter-group" style="margin-left: auto; display: flex; gap: 3px;">
          ${TASK_STATUS_FILTERS.map(
            (f) => html`<button
              class=${"env-task-filter" + (f.key === filterKey ? " active" : "")}
              onClick=${() => setFilterKey(f.key)}
              type="button"
            >${f.label}</button>`
          )}
        </span>
      </h3>
      <div class="env-task-list">
        ${sorted.length === 0
          ? html`<div class="env-empty">
              ${tasks.length === 0 ? "(no tasks yet)" : `(no ${filter.label} tasks)`}
            </div>`
          : sorted.map(
              ({ task: t, depth }) => {
                const active = t.status !== "done" && t.status !== "cancelled";
                return html`
                  <div
                    class=${"env-task status-" + t.status + (depth > 0 ? " env-task-child" : "")}
                    style=${depth > 0 ? `margin-left: ${depth * 14}px` : ""}
                    key=${t.id}
                  >
                    <div class="env-task-head">
                      ${depth > 0 ? html`<span class="env-task-branch">↳</span>` : null}
                      <span class="env-task-status">${t.status}</span>
                      <span class="env-task-id">${t.id}</span>
                      ${active
                        ? html`<button
                            class="env-task-cancel"
                            onClick=${async () => {
                              if (!confirm(`Cancel task ${t.id}?\n${t.title}`)) return;
                              await authFetch("/api/tasks/" + encodeURIComponent(t.id) + "/cancel", { method: "POST" });
                            }}
                            title="Cancel this task"
                          >×</button>`
                        : null}
                    </div>
                    <div class="env-task-title">${t.title}</div>
                    <div class="env-task-meta">
                      by ${t.created_by} · owner ${t.owner || "-"} · pri ${t.priority}
                    </div>
                  </div>
                `;
              }
            )}
      </div>
      <form class="env-task-form" onSubmit=${submit}>
        <input
          class="env-task-title-input"
          placeholder="new task title"
          value=${title}
          onInput=${(e) => setTitle(e.target.value)}
          autocomplete="off"
        />
        <textarea
          class="env-task-desc-input"
          placeholder="description (optional)"
          value=${description}
          onInput=${(e) => setDescription(e.target.value)}
          rows=${2}
        ></textarea>
        <div class="env-task-row">
          <select value=${priority} onChange=${(e) => setPriority(e.target.value)}>
            <option value="low">low</option>
            <option value="normal">normal</option>
            <option value="high">high</option>
            <option value="urgent">urgent</option>
          </select>
          <button class="primary" type="submit" disabled=${submitting || !title.trim()}>
            ${submitting ? "…" : "create"}
          </button>
        </div>
      </form>
    </section>
  `;
}

function EnvCostSection({ agents, serverStatus }) {
  const total = agents.reduce((s, a) => s + (a.cost_estimate_usd || 0), 0);
  const working = agents.filter((a) => a.status === "working").length;
  const active = agents
    .filter(
      (a) =>
        (a.cost_estimate_usd || 0) > 0 ||
        a.status === "working" ||
        a.status === "error"
    )
    .sort(
      (a, b) => (b.cost_estimate_usd || 0) - (a.cost_estimate_usd || 0)
    );
  const caps = serverStatus?.caps;
  const teamToday = caps?.team_today_usd ?? 0;
  const teamCap = caps?.team_daily_usd ?? 0;
  const agentCap = caps?.agent_daily_usd ?? 0;
  const showCaps = caps && (teamCap > 0 || agentCap > 0);
  const teamPct = teamCap > 0 ? Math.min(100, Math.round((teamToday / teamCap) * 100)) : 0;
  const teamBarClass =
    teamPct >= 100 ? " over" : teamPct >= 80 ? " warn" : "";

  // Audit-item-23: plan-included token meter for ChatGPT-auth Codex
  // turns. cost_usd is $0 by design for those, so the USD bar above
  // wouldn't catch them; surface tokens used today instead. Polled
  // every 60s via /api/turns/summary?hours=24. Hidden when zero so
  // pure-Claude deployments don't see an empty meter.
  const [planTokens, setPlanTokens] = useState(0);
  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const res = await authFetch("/api/turns/summary?hours=24");
        if (!res.ok || cancelled) return;
        const d = await res.json();
        if (cancelled) return;
        setPlanTokens(Number(d.plan_included_token_total) || 0);
      } catch (_) {
        // Silent — endpoint optional; bar just stays at last value.
      }
    };
    refresh();
    const t = setInterval(refresh, 60_000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);
  const formatTokens = (n) => {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
    if (n >= 1_000) return (n / 1_000).toFixed(1) + "k";
    return String(n);
  };
  return html`
    <section class="env-section">
      <h3 class="env-section-title">
        Cost <span class="env-count">$${total.toFixed(3)}</span>
      </h3>
      ${showCaps
        ? html`<div class="env-cap-bar">
            <div class="env-cap-bar-label">
              ${teamCap > 0
                ? html`today: $${teamToday.toFixed(3)} / $${teamCap.toFixed(2)}`
                : html`today: $${teamToday.toFixed(3)} (team cap off)`}
              ${agentCap > 0 ? html` · per-agent cap $${agentCap.toFixed(2)}` : null}
            </div>
            ${teamCap > 0
              ? html`<div class="env-cap-bar-track">
                  <div class=${"env-cap-bar-fill" + teamBarClass} style=${"width:" + teamPct + "%"}></div>
                </div>`
              : null}
          </div>`
        : null}
      ${planTokens > 0
        ? html`<div class="env-cap-bar" style="margin-top: 4px;">
            <div class="env-cap-bar-label">
              plan-included tokens (24h): <strong>${formatTokens(planTokens)}</strong>
              <span class="muted"> · ChatGPT-auth Codex usage (cost_usd = 0)</span>
            </div>
          </div>`
        : null}
      ${working > 0
        ? html`<div class="env-cost-sub">${working} agent${working === 1 ? "" : "s"} working now</div>`
        : null}
      ${active.length === 0
        ? html`<div class="env-empty">(no agents have spent yet)</div>`
        : html`
            <div class="env-cost-list">
              ${active.map(
                (a) => html`
                  <div class="env-cost-row" key=${a.id}>
                    <span class=${"env-cost-dot " + (a.status || "stopped")}></span>
                    <span class="env-cost-id">${a.id}</span>
                    <span class="env-cost-name">
                      ${a.name || (a.kind === "player" ? "unassigned" : a.id)}
                    </span>
                    <span class="env-cost-value">$${(a.cost_estimate_usd || 0).toFixed(3)}</span>
                  </div>
                `
              )}
            </div>
          `}
    </section>
  `;
}

// Event types that belong in the cross-agent timeline. tool_use /
// tool_result / result / agent_stopped are noise at this altitude
// (agent panes surface them). `connected` is system chatter.
const TIMELINE_TYPES = new Set([
  "agent_started",
  "text",
  "error",
  "task_created",
  "task_claimed",
  "task_assigned",
  "task_updated",
  "message_sent",
  "memory_updated",
  "cost_capped",
  "commit_pushed",
  "decision_written",
  "human_attention",
  "player_assigned",
  "agent_cancelled",
  "paused",
  "pause_toggled",
]);

function EnvTimelineSection({ conversations }) {
  const timelineRef = useRef(null);
  const stickyRef = useRef(true);

  // Flatten all WS-routed events across slots and keep only the ones
  // that belong at the overview level.
  const events = useMemo(() => {
    const merged = [];
    for (const list of conversations.values()) {
      for (const ev of list) {
        if (TIMELINE_TYPES.has(ev.type)) merged.push(ev);
      }
    }
    merged.sort((a, b) => (a.ts || "").localeCompare(b.ts || ""));
    // Cap at last 80 — this is the overview, not the full log.
    return merged.slice(-80);
  }, [conversations]);

  const onScroll = useCallback((e) => {
    const el = e.currentTarget;
    stickyRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }, []);

  useEffect(() => {
    if (timelineRef.current && stickyRef.current) {
      timelineRef.current.scrollTop = timelineRef.current.scrollHeight;
    }
  }, [events.length]);

  return html`
    <section class="env-section env-timeline-section">
      <h3 class="env-section-title">
        Timeline <span class="env-count">${events.length}</span>
      </h3>
      ${events.length === 0
        ? html`<div class="env-empty">(nothing yet — run a prompt)</div>`
        : html`
            <div class="env-timeline" ref=${timelineRef} onScroll=${onScroll}>
              ${events.map(
                (ev, i) => html`<${EnvTimelineItem} key=${(ev.__id ?? "l" + i)} event=${ev} />`
              )}
            </div>
          `}
    </section>
  `;
}

function EnvTimelineItem({ event }) {
  const ts = (event.ts || "").slice(11, 19);
  const who = event.agent_id || "?";
  if (event.type === "agent_started") {
    const prompt = (event.prompt || "").replace(/\s+/g, " ").slice(0, 80);
    const arrow = event.resumed_session ? "↻" : "→";
    return html`<div class="env-tl-item env-tl-started">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <${RuntimeChip} runtime=${event.runtime} compact=${true} />
      <span class="env-tl-arrow" title=${event.resumed_session ? "resumed prior session" : "fresh start"}>${arrow}</span>
      <span class="env-tl-body">${prompt}</span>
    </div>`;
  }
  if (event.type === "text") {
    const body = (event.content || "").replace(/\s+/g, " ").slice(0, 120);
    return html`<div class="env-tl-item env-tl-text">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">${body}</span>
    </div>`;
  }
  if (event.type === "error") {
    const body = (event.error || "").slice(0, 100);
    return html`<div class="env-tl-item env-tl-error">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">⚠ ${body}</span>
    </div>`;
  }
  if (event.type === "task_created") {
    return html`<div class="env-tl-item env-tl-task">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">+ [${event.task_id}] ${event.title}</span>
    </div>`;
  }
  if (event.type === "task_claimed") {
    return html`<div class="env-tl-item env-tl-task">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">◆ claimed ${event.task_id}</span>
    </div>`;
  }
  if (event.type === "task_assigned") {
    return html`<div class="env-tl-item env-tl-task">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">▶ assigned ${event.task_id} → ${event.to}</span>
    </div>`;
  }
  if (event.type === "task_updated") {
    const note = event.note ? " — " + event.note.slice(0, 60) : "";
    return html`<div class="env-tl-item env-tl-task">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">${event.task_id}: ${event.old_status} → ${event.new_status}${note}</span>
    </div>`;
  }
  if (event.type === "message_sent") {
    const subj = event.subject ? ` (${event.subject})` : "";
    const preview = (event.body_preview || "").slice(0, 80);
    const urgent = event.priority === "interrupt" ? " ⚠" : "";
    return html`<div class="env-tl-item env-tl-msg">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">→ ${event.to}${urgent}${subj}: ${preview}</span>
    </div>`;
  }
  if (event.type === "memory_updated") {
    return html`<div class="env-tl-item env-tl-mem">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">◎ memory/${event.topic} v${event.version} (${event.size} chars)</span>
    </div>`;
  }
  if (event.type === "decision_written") {
    return html`<div class="env-tl-item env-tl-decision">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">✎ decision: ${event.title}</span>
    </div>`;
  }
  if (event.type === "human_attention") {
    const urgency = event.urgency === "blocker" ? "⛔" : "⚠";
    const subj = (event.subject || "").slice(0, 80);
    return html`<div class="env-tl-item env-tl-attention">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">${urgency} ${subj}</span>
    </div>`;
  }
  if (event.type === "paused") {
    return html`<div class="env-tl-item env-tl-paused">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">⏸ spawn blocked — harness is paused</span>
    </div>`;
  }
  if (event.type === "pause_toggled") {
    return html`<div class="env-tl-item env-tl-paused">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">${event.paused ? "⏸ harness paused" : "▶ harness resumed"}</span>
    </div>`;
  }
  if (event.type === "agent_cancelled") {
    return html`<div class="env-tl-item env-tl-cancelled">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">⏹ cancelled</span>
    </div>`;
  }
  if (event.type === "player_assigned") {
    const summary = `${event.player_id} → ${event.name || "(no name)"}${event.role ? " — " + event.role : ""}`;
    return html`<div class="env-tl-item env-tl-assigned">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">☻ ${summary}</span>
    </div>`;
  }
  if (event.type === "cost_capped") {
    const reason = (event.reason || "").slice(0, 140);
    return html`<div class="env-tl-item env-tl-capped">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">🚫 spawn blocked — ${reason}</span>
    </div>`;
  }
  if (event.type === "commit_pushed") {
    const msg = (event.message || "").slice(0, 80);
    const pushState = event.pushed ? "↑" : event.push_requested ? "✗push" : "local";
    return html`<div class="env-tl-item env-tl-commit">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-body">● ${event.sha} ${pushState} — ${msg}</span>
    </div>`;
  }
  return null;
}

// ------------------------------------------------------------------
// files pane — a browsable tree + an editor for text files. Lives
// as a special slot (__files) in openColumns, so it participates in
// the normal drag/resize/stack affordances like an agent pane.
// ------------------------------------------------------------------

function FilesPane({ slot, authedFetch, fsEpoch, onClose, onDropEdge, onPopOut, stacked, isMaximized, onToggleMaximize, rootsFromApp, pendingFileOpen, clearPendingFileOpen }) {
  // Seed `roots` from the App-level cache if it's already loaded so a
  // file-link click that opens this pane for the first time can resolve
  // immediately instead of waiting for our own self-fetch.
  const [roots, setRoots] = useState(() =>
    Array.isArray(rootsFromApp) && rootsFromApp.length > 0 ? rootsFromApp : []
  );
  const [activeRoot, setActiveRoot] = useState(null);
  const [tree, setTree] = useState(null);
  const [selected, setSelected] = useState(null); // {root, path}
  // Phase 5: tree collapse state persisted to localStorage so a page
  // reload doesn't lose the user's expanded folders. Key format:
  // "root:relpath" — same as before; new entries land in the Set
  // and the Set is serialized as an array on every change.
  const [expanded, setExpanded] = useState(() => {
    try {
      const raw = localStorage.getItem("harness_files_expanded_v1");
      if (!raw) return new Set();
      const arr = JSON.parse(raw);
      return Array.isArray(arr) ? new Set(arr) : new Set();
    } catch (_) {
      return new Set();
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(
        "harness_files_expanded_v1",
        JSON.stringify(Array.from(expanded)),
      );
    } catch (_) {
      /* localStorage disabled — silent no-op */
    }
  }, [expanded]);
  const [content, setContent] = useState(null); // authoritative loaded text
  const [draft, setDraft] = useState(""); // textarea buffer
  const [meta, setMeta] = useState(null); // {size, mtime}
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [previewMd, setPreviewMd] = useState(true);
  // Code-file preview/edit toggle. Same idea as previewMd but for
  // syntax-highlighted source. Default to preview so a non-editable
  // file (e.g. from the global root with read-only intent) still
  // reads nicely without an extra click.
  const [previewCode, setPreviewCode] = useState(true);
  // Tree-vs-editor splitter width. Session-only — no localStorage so
  // every reload starts from a sensible default. CSS clamps the actual
  // applied width to [160px, 60% of pane width] in case the saved
  // value somehow ends up out of range.
  const [treeWidth, setTreeWidth] = useState(220);
  const splitterRef = useRef(null);
  const onSplitterPointerDown = useCallback((e) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = treeWidth;
    const onMove = (ev) => {
      const dx = ev.clientX - startX;
      // Bound: 140 (tree readable minimum) ↔ 600 (still leave room for
      // the editor). The CSS max-width:60% clamps the visual size if
      // the pane is narrower than 1000 px.
      const next = Math.max(140, Math.min(600, startW + dx));
      setTreeWidth(next);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, [treeWidth]);

  // Drop handling reuses the same protocol as AgentPane so DnD works
  // the same way — we just refuse to become a drag source.
  const [dropEdge, setDropEdge] = useState(null);
  const onDragOver = useCallback((e) => {
    const types = Array.from(e.dataTransfer.types || []);
    if (!types.includes("application/x-harness-slot")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const rect = e.currentTarget.getBoundingClientRect();
    const xf = (e.clientX - rect.left) / rect.width;
    const yf = (e.clientY - rect.top) / rect.height;
    let edge;
    if (xf < 0.22) edge = "left";
    else if (xf > 0.78) edge = "right";
    else if (yf < 0.5) edge = "top";
    else edge = "bottom";
    setDropEdge(edge);
  }, []);
  const onDragLeave = useCallback((e) => {
    const next = e.relatedTarget;
    if (next && e.currentTarget.contains(next)) return;
    setDropEdge(null);
  }, []);
  const onDrop = useCallback((e) => {
    const edge = dropEdge;
    setDropEdge(null);
    const dragged = e.dataTransfer.getData("application/x-harness-slot");
    if (!dragged || dragged === slot || !edge) return;
    e.preventDefault();
    if (onDropEdge) onDropEdge(dragged, slot, edge);
  }, [slot, dropEdge, onDropEdge]);

  const loadRoots = useCallback(async () => {
    try {
      const res = await authedFetch("/api/files/roots");
      const data = await res.json();
      setRoots(Array.isArray(data) ? data : []);
      // Phase 5 (PROJECTS_SPEC.md §7): the new payload always has
      // exactly two roots — global + active project. Don't auto-pin
      // the activeRoot; both are rendered top-level and the user
      // expands either tree directly. Keep the picker as a fallback
      // for legacy clients still toggling activeRoot.
      if (!activeRoot && Array.isArray(data) && data.length > 0) {
        // Default to the project root since that's where most user
        // work happens — global is read-mostly (CLAUDE.md, skills/).
        const proj = data.find((r) => (r.scope || r.key) === "project");
        setActiveRoot((proj || data[0]).id || (proj || data[0]).key);
      }
    } catch (e) {
      setErr("roots load failed: " + String(e));
    }
  }, [authedFetch, activeRoot]);

  // Phase 5: tree state per-root so both panels render without
  // tearing each other's state down on a re-fetch.
  const [trees, setTrees] = useState({});  // {global: {...}, project: {...}}

  const loadTree = useCallback(async (rootKey) => {
    if (!rootKey) return;
    try {
      const res = await authedFetch("/api/files/tree/" + rootKey);
      const data = await res.json();
      setTrees((prev) => ({ ...prev, [rootKey]: data }));
      setTree(data);  // back-compat for code paths still reading `tree`
    } catch (e) {
      setErr("tree load failed: " + String(e));
    }
  }, [authedFetch]);

  // Phase 5: load BOTH trees on mount + after every roots refresh.
  // Reloads when activeProjectId changes via the WS dispatcher
  // (project_switched bumps the rootsFromApp prop with the new
  // `project` root path; we re-fetch both trees so the bottom panel
  // rebinds in place).
  useEffect(() => {
    loadRoots();
  }, [loadRoots]);
  // Phase 5 audit fix: when App's `fileRoots` cache refreshes (e.g.
  // after a `project_switched` event triggers `loadFileRoots()`), the
  // `rootsFromApp` prop reference changes. Re-sync our local `roots`
  // state so labels/paths/project_id reflect the new active project
  // without needing a self-fetch round-trip.
  useEffect(() => {
    if (!Array.isArray(rootsFromApp) || rootsFromApp.length === 0) return;
    setRoots(rootsFromApp);
  }, [rootsFromApp]);
  useEffect(() => {
    if (!Array.isArray(roots)) return;
    for (const r of roots) {
      const id = r.id || r.key;
      if (id) loadTree(id);
    }
  }, [roots, loadTree]);
  // Live-refresh: when a file-system-changing event lands on the WS,
  // App bumps fsEpoch. Reload BOTH trees (agent edits could land in
  // either scope). Skip either reload while the user is mid-edit
  // (dirty draft) so we don't yank their typing.
  useEffect(() => {
    if (fsEpoch == null) return;
    const dirty = content !== null && draft !== content;
    if (dirty) return;
    for (const r of roots) {
      const id = r.id || r.key;
      if (id) loadTree(id);
    }
    if (selected) openFile(selected.root, selected.path);
  }, [fsEpoch]);

  const activeRootMeta = roots.find((r) => (r.id || r.key) === activeRoot);

  const openFile = useCallback(async (root, path) => {
    setLoading(true);
    setErr("");
    try {
      // Extension allowlist: skip the body fetch for binaries entirely.
      // The pane still selects the file (so the user sees it as
      // selected) but renders a "binary" placeholder card instead.
      if (!filesIsPreviewableText(path)) {
        // Probe size via /api/files/tree results that are already in
        // memory; for the size we rely on the tree node's `size` field
        // when present, but since we don't always have that here we
        // just skip — the placeholder reads fine without it.
        setSelected({ root, path });
        setContent(null);
        setDraft("");
        setMeta(null);
        return;
      }
      const res = await authedFetch(
        "/api/files/read/" + root + "?path=" + encodeURIComponent(path)
      );
      if (!res.ok) {
        const body = await res.text();
        throw new Error("HTTP " + res.status + ": " + body.slice(0, 200));
      }
      const data = await res.json();
      setSelected({ root, path });
      setContent(data.content);
      setDraft(data.content);
      setMeta({ size: data.size, mtime: data.mtime });
    } catch (e) {
      setErr("read failed: " + String(e));
    } finally {
      setLoading(false);
    }
  }, [authedFetch]);

  // React to file-link clicks that landed before this pane mounted
  // (App stashes the absolute path in `pendingFileOpen`). Resolve to
  // (root, relative) via longest-prefix match against either our
  // self-fetched `roots` or the App-level cache, switch to that root,
  // expand parent folders, open the file, then clear the pending state.
  useEffect(() => {
    if (!pendingFileOpen?.path) return;
    const available = roots.length > 0
      ? roots
      : (Array.isArray(rootsFromApp) ? rootsFromApp : []);
    if (available.length === 0) return; // wait for roots to land
    const target = pendingFileOpen.path;
    let best = null;
    for (const r of available) {
      const rp = r.path || "";
      if (!rp) continue;
      if (target === rp || target.startsWith(rp + "/")) {
        if (!best || rp.length > (best.path?.length || 0)) {
          best = r;
        }
      }
    }
    if (!best) {
      setErr("file-link: no root matches " + target);
      if (clearPendingFileOpen) clearPendingFileOpen();
      return;
    }
    const relative = target === best.path ? "" : target.slice(best.path.length + 1);
    setActiveRoot(best.key);
    if (relative) {
      // Expand each parent folder so the user lands with the file
      // already visible in the tree.
      const parents = new Set([best.key + ":"]);
      let cur = relative;
      while (cur && cur.includes("/")) {
        cur = cur.slice(0, cur.lastIndexOf("/"));
        parents.add(best.key + ":" + cur);
      }
      setExpanded((prev) => {
        const next = new Set(prev);
        for (const p of parents) next.add(p);
        return next;
      });
      openFile(best.key, relative);
    }
    if (clearPendingFileOpen) clearPendingFileOpen();
  }, [pendingFileOpen, roots, rootsFromApp, openFile, clearPendingFileOpen]);

  const save = useCallback(async () => {
    if (!selected || !activeRootMeta?.writable) return;
    setSaving(true);
    setErr("");
    try {
      const res = await authedFetch(
        "/api/files/write/" + selected.root +
          "?path=" + encodeURIComponent(selected.path),
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: draft }),
        }
      );
      if (!res.ok) {
        const body = await res.text();
        throw new Error("HTTP " + res.status + ": " + body.slice(0, 200));
      }
      setContent(draft);
      // Refresh the tree so a newly-created file shows up.
      loadTree(activeRoot);
    } catch (e) {
      setErr("save failed: " + String(e));
    } finally {
      setSaving(false);
    }
  }, [authedFetch, selected, activeRootMeta, draft, loadTree, activeRoot]);

  const dirty = content !== null && draft !== content;
  const isMd = selected?.path?.toLowerCase().endsWith(".md");
  // Code preview applies to non-markdown text files where hljs has a
  // language match. Markdown gets its own marked+highlight pipeline
  // (renderMarkdown) so we keep them separate.
  const codeLang = selected ? langForFile(selected.path) : "";
  const isCode = !isMd && !!codeLang && filesIsPreviewableText(selected?.path);
  const isPreviewable = selected ? filesIsPreviewableText(selected.path) : false;

  return html`
    <section
      class=${"pane files-pane" + (dropEdge ? " drop-edge-" + dropEdge : "")}
      id=${"pane-" + slot}
      onDragOver=${onDragOver}
      onDragLeave=${onDragLeave}
      onDrop=${onDrop}
    >
      <header class="pane-head">
        <span class="pane-drag-handle" title="Files pane">
          <span class="pane-dot idle" />
          <span class="pane-id">files</span>
          ${selected
            ? html`<span class="pane-name">${selected.root} / ${selected.path}${dirty ? " *" : ""}</span>`
            : html`<span class="pane-name">pick a file</span>`}
        </span>
        ${dirty
          ? html`<button
              class="pane-files-save"
              onClick=${save}
              disabled=${saving}
              title="Save (⌘/Ctrl+S)"
            >${saving ? "saving…" : "save"}</button>`
          : null}
        ${stacked
          ? html`<button class="pane-pop-out" onClick=${() => onPopOut(slot)}
              title="Pop out to its own column">⇱</button>`
          : null}
        ${onToggleMaximize
          ? html`<button
              class="pane-maximize"
              onClick=${onToggleMaximize}
              title=${isMaximized ? "Restore (show all panes)" : "Maximize (full screen)"}
            >${isMaximized ? "❐" : "⛶"}</button>`
          : null}
        <button class="pane-close" onClick=${onClose} title="Close">×</button>
      </header>

      <div class="files-body">
        <nav class="files-tree" style=${"flex: 0 0 " + treeWidth + "px;"}>
          ${roots.map((r) => {
            const id = r.id || r.key;
            const headerIcon = r.scope === "global" ? "🌐" : "📁";
            const subtree = trees[id];
            const sectionExpKey = "__section:" + id;
            const sectionOpen = !expanded.has(sectionExpKey);  // open by default
            const onToggleSection = () => {
              setExpanded((prev) => {
                const next = new Set(prev);
                if (next.has(sectionExpKey)) next.delete(sectionExpKey);
                else next.add(sectionExpKey);
                return next;
              });
            };
            return html`
              <div class=${"files-root-section" + (id === activeRoot ? " active" : "")} key=${id}>
                <button
                  class="files-root-header"
                  onClick=${() => { setActiveRoot(id); onToggleSection(); }}
                  title=${(r.label || id) + " — " + (r.path || "") + (r.writable ? "" : " (read-only)")}
                >
                  <span class="files-root-icon">${headerIcon}</span>
                  <span class="files-root-label">${r.label || id}</span>
                  ${r.writable ? null : html`<span class="files-root-lock"> 🔒</span>`}
                  <span class="files-root-caret">${sectionOpen ? "▾" : "▸"}</span>
                </button>
                ${sectionOpen
                  ? subtree
                    ? html`<${FileTreeNode}
                        node=${subtree}
                        root=${id}
                        path=""
                        depth=${0}
                        expanded=${expanded}
                        setExpanded=${setExpanded}
                        selected=${selected}
                        onPick=${openFile}
                      />`
                    : html`<div class="files-empty">loading…</div>`
                  : null}
              </div>
            `;
          })}
        </nav>

        <div
          class="files-splitter"
          ref=${splitterRef}
          onPointerDown=${onSplitterPointerDown}
          role="separator"
          aria-orientation="vertical"
          title="Drag to resize"
        ><span class="files-splitter-grip" /></div>

        <section class="files-editor">
          ${err ? html`<div class="files-err">${err}</div>` : null}
          ${!selected
            ? html`<div class="files-empty">Pick a file on the left.</div>`
            : loading
            ? html`<div class="files-empty">loading…</div>`
            : html`
              ${(() => {
                // Phase 5 (PROJECTS_SPEC.md §7): tag opened files with
                // originating scope so a "global" CLAUDE.md edit reads
                // differently from a "project" repo edit. Looked up
                // from the loaded roots payload by `selected.root`.
                const sel = roots.find((r) => (r.id || r.key) === selected.root);
                const scope = sel?.scope;
                if (!scope) return null;
                const cls = scope === "global"
                  ? "files-scope-badge files-scope-global"
                  : "files-scope-badge files-scope-project";
                const icon = scope === "global" ? "🌐" : "📁";
                const label = scope === "global"
                  ? "GLOBAL"
                  : ("PROJECT" + (sel?.label ? ` — ${sel.label}` : ""));
                return html`<div class=${cls} title=${selected.path}>${icon} ${label}</div>`;
              })()}
              ${isMd
                ? html`<div class="files-md-toolbar">
                    <button
                      class=${previewMd ? "active" : ""}
                      onClick=${() => setPreviewMd(true)}
                    >preview</button>
                    <button
                      class=${!previewMd ? "active" : ""}
                      onClick=${() => setPreviewMd(false)}
                    >edit</button>
                  </div>`
                : isCode
                ? html`<div class="files-md-toolbar">
                    <button
                      class=${previewCode ? "active" : ""}
                      onClick=${() => setPreviewCode(true)}
                    >preview</button>
                    <button
                      class=${!previewCode ? "active" : ""}
                      onClick=${() => setPreviewCode(false)}
                    >edit</button>
                    <span class="files-code-lang">${codeLang}</span>
                  </div>`
                : null}
              ${!isPreviewable
                ? html`<div class="files-binary-card">
                    <div class="files-binary-title">Binary file — preview not supported</div>
                    <div class="files-binary-meta">${selected.path}</div>
                  </div>`
                : isMd && previewMd
                ? html`<div
                    class="files-md-preview"
                    dangerouslySetInnerHTML=${{ __html: renderMarkdown(draft) }}
                  />`
                : isCode && previewCode
                ? html`<div
                    class="files-code-preview"
                    dangerouslySetInnerHTML=${{ __html: filesRenderCode(draft, selected.path) }}
                  />`
                : html`<textarea
                    class="files-textarea"
                    value=${draft}
                    readOnly=${!activeRootMeta?.writable}
                    onInput=${(e) => setDraft(e.target.value)}
                    onKeyDown=${(e) => {
                      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
                        e.preventDefault();
                        save();
                      }
                    }}
                    spellcheck=${false}
                  />`}
              <div class="files-editor-foot">
                ${meta ? html`<span>${meta.size} bytes</span>` : null}
                ${dirty ? html`<span class="dirty">unsaved</span>` : null}
                ${!activeRootMeta?.writable ? html`<span>read-only</span>` : null}
              </div>
            `}
        </section>
      </div>
    </section>
  `;
}

function FileTreeNode({ node, root, path, depth, expanded, setExpanded, selected, onPick }) {
  const key = root + ":" + (node.path || "");
  const isOpen = depth === 0 || expanded.has(key);
  const toggle = () => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      return next;
    });
  };
  if (node.type === "file") {
    const isSel = selected?.root === root && selected?.path === node.path;
    return html`
      <div
        class=${"files-node file" + (isSel ? " selected" : "")}
        style=${"padding-left:" + (depth * 12 + 8) + "px"}
        onClick=${() => onPick(root, node.path)}
        title=${node.path}
      >
        <span class="files-node-icon">📄</span>
        <span class="files-node-name">${node.name}</span>
      </div>
    `;
  }
  return html`
    ${depth > 0
      ? html`<div
          class="files-node dir"
          style=${"padding-left:" + (depth * 12 + 8) + "px"}
          onClick=${toggle}
        >
          <span class="files-node-icon">${isOpen ? "▾" : "▸"}</span>
          <span class="files-node-name">${node.name || root}</span>
        </div>`
      : null}
    ${isOpen
      ? (node.children || []).map((child) => html`
          <${FileTreeNode}
            key=${root + ":" + (child.path || child.name)}
            node=${child}
            root=${root}
            path=${child.path || ""}
            depth=${depth + 1}
            expanded=${expanded}
            setExpanded=${setExpanded}
            selected=${selected}
            onPick=${onPick}
          />
        `)
      : null}
  `;
}

// renderMarkdown lives near the top of the file now (with marked +
// DOMPurify + highlight.js wiring). Deleted the hand-rolled fallback
// and its private helpers (_safeHref, renderInline) — the marked
// pipeline supersedes them.

// Phase 8 of recurrence v2 (Docs/recurrence-specs.md) deleted the
// ActiveLoops countdown bar — the Recurrence pane (rail icon:
// circular arrows) is the canonical surface for tick / repeat /
// cron now, with editable cards and live next-fire stamps.

// ------------------------------------------------------------------
// context usage bar — compact horizontal meter next to the effort chip.
// Green until 50%, amber 50-67%, red >= 67%. Empty when there's no
// active session. Pulled from /api/agents/<slot>/context on mount and
// refreshed on every 'result' event for this slot. The server reads
// the latest per-assistant usage row from Claude Code's session jsonl,
// so cached prompt tokens count once toward the 1M window instead of
// being summed across every tool round.
// ------------------------------------------------------------------

function ContextBar({ slot, liveEvents, model }) {
  const [data, setData] = useState(null); // {used_tokens, context_window, ratio}

  const refetch = useCallback(async () => {
    try {
      const url = model
        ? `/api/agents/${encodeURIComponent(slot)}/context?model=${encodeURIComponent(model)}`
        : `/api/agents/${encodeURIComponent(slot)}/context`;
      const res = await authFetch(url);
      if (res.ok) setData(await res.json());
    } catch (_) {
      // silent — bar just stays at last known state
    }
  }, [slot, model]);

  useEffect(() => { refetch(); }, [refetch]);

  // Watch for events that change the meter without a DB round-trip.
  useEffect(() => {
    if (!liveEvents || liveEvents.length === 0) return;
    const last = liveEvents[liveEvents.length - 1];
    if (!last || last.agent_id !== slot) return;
    if (last.type === "result") {
      // Latest turn landed — refetch the authoritative estimate. The
      // result event can hit the browser just before the server commits
      // the new session_id, so do one short follow-up fetch too.
      refetch();
      const t = setTimeout(refetch, 350);
      return () => clearTimeout(t);
    } else if (
      last.type === "session_cleared" ||
      last.type === "session_compacted" ||
      last.type === "compact_empty_forced"
    ) {
      // Session just got cleared; the bar should drop to 0 immediately.
      setData((d) => d ? { ...d, used_tokens: 0, ratio: 0 } : d);
    }
  }, [liveEvents, refetch]);

  if (!data || !data.context_window) return null;
  const ratio = Math.max(0, Math.min(1, data.ratio || 0));
  const pct = Math.round(ratio * 100);
  const color = ratio >= 0.67
    ? "var(--err, #ff5a5a)"
    : ratio >= 0.5
    ? "#e5a72d"
    : "var(--ok, #3fb950)";
  const used = data.used_tokens || 0;
  const window = data.context_window;
  const fmtK = (n) => n >= 1000 ? `${(n / 1000).toFixed(0)}k` : `${n}`;
  const title =
    `Context: ~${fmtK(used)} / ${fmtK(window)} tokens (${pct}%)\n` +
    `Estimated from the latest assistant usage row in the session file: ` +
    `input + cache read + cache creation, counted once for the current prompt.`;
  return html`<div
    class="pane-mode-chip context-bar"
    title=${title}
  >
    <span class="context-bar-label">ctx</span>
    <span class="context-bar-track">
      <span
        class="context-bar-fill"
        style=${`width: ${pct}%; background: ${color};`}
      ></span>
    </span>
    <span class="context-bar-pct">${pct}%</span>
  </div>`;
}

// ------------------------------------------------------------------
// agent pane
// ------------------------------------------------------------------

function AgentPane({ slot, agent, currentTask, liveEvents, streaming, projectEpoch, onClose, onDropEdge, onPopOut, stacked, isMaximized, onToggleMaximize }) {
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState([]); // {id, url, path, filename}
  const [submitting, setSubmitting] = useState(false);
  const [history, setHistory] = useState([]);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  // Pagination: initial fetch is the most-recent HISTORY_PAGE events.
  // historyExhausted flips to true when a fetch returns fewer rows
  // than requested (no more to walk back to). loadingOlder gates the
  // load-older button so a slow fetch doesn't fire a second request.
  const [historyExhausted, setHistoryExhausted] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [paneSettings, setPaneSettings] = useState(() => loadPaneSettings(slot));
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [dropEdge, setDropEdge] = useState(null); // 'top' | 'bottom' | 'left' | 'right' | null
  // Slash-command autocomplete. Open whenever the input starts with "/"
  // and has no newlines (commands are single-line). selectedIdx tracks
  // arrow-key navigation.
  const [slashIdx, setSlashIdx] = useState(0);
  // Transient info banner — used by /tools and /help to show output
  // without bothering the agent. Cleared on dismiss or next slash run.
  const [infoText, setInfoText] = useState(null);
  // Prompt history for ↑/↓ navigation (Ctrl/Cmd + arrow).
  const [promptHistory, setPromptHistory] = useState(() => loadPromptHistory(slot));
  const [promptHistoryIdx, setPromptHistoryIdx] = useState(null);
  // In-pane text search. When searchQuery is set, events whose text
  // doesn't match are hidden from the body render.
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const bodyRef = useRef(null);
  const [roleDefaultRuntime, setRoleDefaultRuntime] = useState(null);
  const runtimeOverride = (agent?.runtime_override || "").toLowerCase();
  const effectiveRuntime = runtimeOverride || roleDefaultRuntime || "claude";
  const effectiveRuntimeKnown = !!runtimeOverride || roleDefaultRuntime !== null;
  const paneModelValidForRuntime = !paneSettings.model ||
    modelOptionsFor(effectiveRuntime).some((m) => m.value === paneSettings.model);

  const loadRoleDefaultRuntime = useCallback(async () => {
    try {
      const res = await authFetch("/api/team/runtimes");
      if (!res.ok) return;
      const data = await res.json();
      const id = slot === "coach" ? data.coach : data.players;
      setRoleDefaultRuntime(id || "");
    } catch (_e) {
      // Silent - if this fails, model validation simply waits.
    }
  }, [slot]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await authFetch("/api/team/runtimes");
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (cancelled) return;
        const id = slot === "coach" ? data.coach : data.players;
        setRoleDefaultRuntime(id || "");
      } catch (_e) {
        // Silent - if this fails, model validation simply waits.
      }
    })();
    return () => { cancelled = true; };
  }, [slot]);

  useEffect(() => {
    if (settingsOpen) loadRoleDefaultRuntime();
  }, [settingsOpen, loadRoleDefaultRuntime]);

  useEffect(() => {
    const onTeamRuntimesUpdated = (e) => {
      const detail = e.detail || {};
      const id = slot === "coach" ? detail.coach : detail.players;
      setRoleDefaultRuntime(id || "");
    };
    window.addEventListener("team-runtimes-updated", onTeamRuntimesUpdated);
    return () => window.removeEventListener("team-runtimes-updated", onTeamRuntimesUpdated);
  }, [slot]);

  useEffect(() => {
    if (!effectiveRuntimeKnown || paneModelValidForRuntime) return;
    setPaneSettings((s) => ({ ...s, model: "" }));
  }, [effectiveRuntimeKnown, paneModelValidForRuntime]);

  // HTML5 DnD: the pane header is the drag source; the whole pane is
  // the drop target (drop inserts the dragged slot BEFORE this pane
  // in its column). The payload is carried via a custom MIME so we
  // don't confuse other drag sources (e.g. image paste).
  const onDragStart = useCallback((e) => {
    e.dataTransfer.setData("application/x-harness-slot", slot);
    e.dataTransfer.effectAllowed = "move";
  }, [slot]);
  const onDragOver = useCallback((e) => {
    // dataTransfer.types is a DOMStringList in some browsers — Array.from
    // normalizes to a plain string[] so .includes is safe.
    const types = Array.from(e.dataTransfer.types || []);
    if (!types.includes("application/x-harness-slot")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    // Divide the pane into edge zones. Left/right 22% → vertical bar
    // (new column). Top/bottom half of the middle band → horizontal bar
    // (stack into this column above/below).
    const rect = e.currentTarget.getBoundingClientRect();
    const xf = (e.clientX - rect.left) / rect.width;
    const yf = (e.clientY - rect.top) / rect.height;
    let edge;
    if (xf < 0.22) edge = "left";
    else if (xf > 0.78) edge = "right";
    else if (yf < 0.5) edge = "top";
    else edge = "bottom";
    setDropEdge(edge);
  }, []);
  const onDragLeave = useCallback((e) => {
    // dragleave fires when the pointer crosses into any child element
    // of the pane (each child is a leaf target). Only clear the highlight
    // when the pointer is actually leaving the pane — checked by seeing
    // whether the element it entered is still a descendant of us.
    const next = e.relatedTarget;
    if (next && e.currentTarget.contains(next)) return;
    setDropEdge(null);
  }, []);
  const onDrop = useCallback((e) => {
    const edge = dropEdge;
    setDropEdge(null);
    const dragged = e.dataTransfer.getData("application/x-harness-slot");
    if (!dragged || dragged === slot || !edge) return;
    e.preventDefault();
    if (onDropEdge) onDropEdge(dragged, slot, edge);
  }, [slot, dropEdge, onDropEdge]);

  // Persist whenever settings change (but not on mount — lazy init
  // already picked up the stored value).
  useEffect(() => {
    savePaneSettings(slot, paneSettings);
  }, [slot, paneSettings]);
  // Stay pinned to the bottom while the user is at or near the bottom.
  // If they've scrolled up to read older events, don't yank them down.
  const stickToBottomRef = useRef(true);

  const onBodyScroll = useCallback((e) => {
    const el = e.currentTarget;
    const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distFromBottom < 80;
  }, []);

  // Page size for both initial history load and the "load older"
  // button. 200 is enough to fill several screens on a desktop pane;
  // dropping from the previous 500 cuts pane-open payload by ~60% for
  // the common case while letting users walk back through old turns
  // on demand. Tune via this constant — the API caps at 1000.
  const HISTORY_PAGE = 200;
  // Load persisted history once per slot. We guard so switching panes
  // doesn't refetch constantly.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await authFetch(
          `/api/events?agent=${encodeURIComponent(slot)}&limit=${HISTORY_PAGE}`
        );
        if (!res.ok) {
          if (!cancelled) setHistoryLoaded(true);
          return;
        }
        const data = await res.json();
        if (cancelled) return;
        const events = (data.events || []).map(unwrapPersisted);
        setHistory(events);
        setHistoryExhausted(events.length < HISTORY_PAGE);
      } catch (e) {
        console.error("history load failed", e);
      } finally {
        if (!cancelled) setHistoryLoaded(true);
      }
    }
    // Reset paginator state when the dependencies fire — otherwise
    // a project switch with fewer events than HISTORY_PAGE leaves
    // historyExhausted from the prior project pinned to true/false
    // until the user reloads.
    setHistory([]);
    setHistoryLoaded(false);
    setHistoryExhausted(false);
    load();
    return () => {
      cancelled = true;
    };
    // projectEpoch bumps on every successful project switch so the
    // pane re-fetches /api/events against the new active project.
  }, [slot, projectEpoch]);

  // Walk further back into history. Anchored at the smallest id we
  // currently hold; the API's `before_id` returns events strictly
  // older than that, so consecutive clicks paginate without overlap.
  const loadOlder = useCallback(async () => {
    if (loadingOlder || historyExhausted) return;
    if (history.length === 0) return;
    // Find oldest id we already have. Persisted events carry a
    // server-assigned __id; if for some reason we don't have one
    // (e.g. this slot's history is purely live), bail rather than
    // refetching the same page.
    let oldestId = Infinity;
    for (const e of history) {
      if (typeof e.__id === "number" && e.__id < oldestId) oldestId = e.__id;
    }
    if (!isFinite(oldestId)) {
      setHistoryExhausted(true);
      return;
    }
    setLoadingOlder(true);
    // Preserve scroll position: when we prepend N events the body's
    // scrollHeight grows and the user's view jumps to the top. We
    // record scrollTop relative to scrollHeight before the prepend,
    // then restore it after.
    const body = bodyRef.current;
    const beforeScrollHeight = body ? body.scrollHeight : 0;
    const beforeScrollTop = body ? body.scrollTop : 0;
    try {
      const res = await authFetch(
        `/api/events?agent=${encodeURIComponent(slot)}&before_id=${oldestId}&limit=${HISTORY_PAGE}`
      );
      if (!res.ok) return;
      const data = await res.json();
      const older = (data.events || []).map(unwrapPersisted);
      if (older.length === 0) {
        setHistoryExhausted(true);
        return;
      }
      setHistory((prev) => {
        // Defensive dedupe in case the same id appears twice.
        const seen = new Set();
        for (const e of prev) if (e.__id != null) seen.add(e.__id);
        const dedupedOlder = older.filter((e) => e.__id == null || !seen.has(e.__id));
        return [...dedupedOlder, ...prev];
      });
      if (older.length < HISTORY_PAGE) setHistoryExhausted(true);
      // After paint, restore scroll: new content is above us, so we
      // shift scrollTop by the amount the body grew.
      requestAnimationFrame(() => {
        if (!body) return;
        const grew = body.scrollHeight - beforeScrollHeight;
        body.scrollTop = beforeScrollTop + grew;
        // Don't snap to bottom on this paint — user explicitly chose
        // to read older content.
        stickToBottomRef.current = false;
      });
    } catch (e) {
      console.error("loadOlder failed", e);
    } finally {
      setLoadingOlder(false);
    }
  }, [history, historyExhausted, loadingOlder, slot]);

  // Merge history + live events. Live events don't carry __id (that's
  // assigned by the server's DB INSERT, which happens AFTER WS fan-out),
  // so if an event fires right before a pane opens it can show up in
  // both streams. Composite fallback key on (ts, agent_id, type) catches
  // the overlap without needing a server refactor.
  const mergedEvents = useMemo(() => {
    const keyOf = (e) =>
      e.__id != null ? "id:" + e.__id : `c:${e.ts}:${e.agent_id}:${e.type}`;
    const seen = new Set();
    const out = [];
    for (const e of history) {
      seen.add(keyOf(e));
      out.push(e);
    }
    for (const e of liveEvents) {
      if (seen.has(keyOf(e))) continue;
      out.push(e);
    }
    return out;
  }, [history, liveEvents]);

  // Pair tool_use ↔ tool_result by id. The tool_result moves INTO its
  // tool_use's card (available as event.__result). Orphaned results
  // (no matching tool_use in this pane's event list — shouldn't happen
  // normally) still render standalone as a safety net.
  const allEvents = useMemo(() => {
    const resultByUseId = new Map();
    const knownUseIds = new Set();
    for (const e of mergedEvents) {
      if (e.type === "tool_use" && e.id) knownUseIds.add(e.id);
      if (e.type === "tool_result" && e.tool_use_id) {
        resultByUseId.set(e.tool_use_id, e);
      }
    }
    return mergedEvents
      .filter((e) => {
        if (e.type !== "tool_result") return true;
        // drop tool_results that are paired (rendered inside their tool_use)
        return !(e.tool_use_id && knownUseIds.has(e.tool_use_id));
      })
      .map((e) =>
        e.type === "tool_use" && e.id
          ? { ...e, __result: resultByUseId.get(e.id) }
          : e
      );
  }, [mergedEvents]);

  // Auto-scroll to bottom as new content arrives. Two modes:
  //   - Normal (between turns): respect user scroll position — if they
  //     scrolled up to read history, leave them there.
  //   - During streaming (a turn is actively typing tokens): always
  //     stick to the bottom so the reader sees the latest text. Once
  //     the turn ends (streamLen drops to 0), normal mode resumes.
  const streamLen =
    (streaming?.text?.length || 0) + (streaming?.thinking?.length || 0);
  const isStreaming = streamLen > 0;
  useEffect(() => {
    if (!bodyRef.current) return;
    if (isStreaming || stickToBottomRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [allEvents.length, streamLen, isStreaming]);

  // Filter allEvents by searchQuery for body render. No-op when the
  // query is empty; otherwise a case-insensitive substring match
  // against the most informative fields per type.
  const visibleEvents = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return allEvents;
    return allEvents.filter((ev) => {
      const fields = [
        ev.content,
        ev.prompt,
        ev.text,
        ev.name,
        ev.tool,
        ev.error,
        ev.subject,
        ev.body,
        ev.title,
        ev.reason,
        ev.input ? JSON.stringify(ev.input) : null,
        ev.__result?.content,
      ];
      return fields.some((f) => typeof f === "string" && f.toLowerCase().includes(q));
    });
  }, [allEvents, searchQuery]);

  // paste handler: capture images, upload, add to attachments strip
  const onPaste = useCallback(async (e) => {
    const items = e.clipboardData?.items || [];
    const uploads = [];
    for (const item of items) {
      if (item.type && item.type.startsWith("image/")) {
        const file = item.getAsFile();
        if (file) uploads.push({ file, type: item.type });
      }
    }
    if (uploads.length === 0) return;
    e.preventDefault();
    for (const { file, type } of uploads) {
      const ext = (type.split("/")[1] || "png").replace("jpeg", "jpg");
      const form = new FormData();
      form.append("file", file, `pasted-${Date.now()}.${ext}`);
      try {
        const res = await authFetch("/api/attachments", {
          method: "POST",
          body: form,
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        setAttachments((prev) => [...prev, data]);
      } catch (err) {
        console.error("upload failed", err);
      }
    }
  }, []);

  const removeAttachment = useCallback((id) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  // Intercept slash commands locally before they ever reach an agent.
  // Returns true if the input was a recognized slash command (and was
  // handled); false otherwise so normal submit flow continues.
  const runSlashCommand = useCallback((raw) => {
    const [first, ...rest] = raw.trim().split(/\s+/);
    const cmd = first.toLowerCase();
    const arg = rest.join(" ");
    switch (cmd) {
      case "/plan":
        setPaneSettings((s) => ({ ...s, planMode: !s.planMode }));
        setInfoText("Plan mode: " + (!paneSettings.planMode ? "ON" : "OFF"));
        return true;
      case "/model":
        setSettingsOpen(true);
        setInfoText(null);
        return true;
      case "/effort": {
        const n = parseInt(arg, 10);
        if (n >= 1 && n <= 4) {
          setPaneSettings((s) => ({ ...s, effort: n }));
          setInfoText("Effort: " + EFFORT_LABELS[n - 1]);
        } else {
          setSettingsOpen(true);
        }
        return true;
      }
      case "/brief":
        setSettingsOpen(true);
        return true;
      case "/clear":
        authFetch("/api/agents/" + slot + "/session", { method: "DELETE" })
          .then(() => setInfoText("Session cleared. Next turn starts fresh."))
          .catch((e) => setInfoText("Session clear failed: " + String(e)));
        return true;
      case "/compact":
        // Asks the agent to summarize its current session into a
        // continuity note, then nulls session_id. The next turn
        // starts fresh but with the summary in its system prompt —
        // equivalent to Claude Code CLI's /compact. Server returns
        // 409 if the agent is currently running.
        authFetch("/api/agents/" + slot + "/compact", { method: "POST" })
          .then((r) => {
            if (r.ok) setInfoText("Compact queued — watch for session_compacted event.");
            else if (r.status === 409) setInfoText("Agent is running — finish or /cancel first.");
            else setInfoText("Compact failed: HTTP " + r.status);
          })
          .catch((e) => setInfoText("Compact failed: " + String(e)));
        return true;
      case "/cancel":
        // Cancel the in-flight turn for THIS pane's agent. Server
        // returns 409 if nothing's running (same semantics as the
        // header ⏹ button hitting the same endpoint).
        authFetch("/api/agents/" + slot + "/cancel", { method: "POST" })
          .then((r) => {
            if (r.ok) setInfoText("Cancel requested — turn will stop at the next await.");
            else if (r.status === 409) setInfoText("Nothing to cancel — agent is idle.");
            else setInfoText("cancel failed: HTTP " + r.status);
          })
          .catch((e) => setInfoText("cancel failed: " + String(e)));
        return true;
      case "/tools": {
        const coach = [
          "Read · Grep · Glob · ToolSearch",
          "coord_list_tasks · coord_create_task · coord_assign_task · coord_update_task",
          "coord_send_message · coord_read_inbox",
          "coord_list_memory · coord_read_memory · coord_update_memory",
          "coord_write_decision · coord_write_context",
          "coord_write_knowledge · coord_read_knowledge · coord_list_knowledge",
          "coord_list_team · coord_set_player_role · coord_request_human",
        ];
        const player = [
          "Read · Grep · Glob · ToolSearch · Write · Edit · Bash",
          "coord_list_tasks · coord_create_task · coord_claim_task · coord_update_task",
          "coord_send_message · coord_read_inbox",
          "coord_list_memory · coord_read_memory · coord_update_memory",
          "coord_write_knowledge · coord_read_knowledge · coord_list_knowledge",
          "coord_list_team · coord_commit_push · coord_request_human",
        ];
        const list = slot === "coach" ? coach : player;
        // Also fetch /api/health to pick up external MCP servers from
        // HARNESS_MCP_CONFIG and /api/team/tools for the team-wide
        // extras (WebSearch / WebFetch) the human toggled on in the
        // Settings drawer. Both vary at runtime so they can't live in
        // the hardcoded list.
        Promise.all([
          authFetch("/api/health").then((r) => r.json()).catch(() => null),
          authFetch("/api/team/tools").then((r) => r.json()).catch(() => null),
        ])
          .then(([health, tools]) => {
            const extra_lines = [];
            const extras = Array.isArray(tools?.tools) ? tools.tools : [];
            if (extras.length > 0) {
              extra_lines.push("");
              extra_lines.push("Team extras (Settings drawer):");
              extra_lines.push("  • " + extras.join(" · "));
            }
            const ext = health?.checks?.mcp_external;
            const mcp_lines = [];
            if (ext && !ext.skipped && ext.server_count > 0) {
              mcp_lines.push("");
              mcp_lines.push(
                `External MCP (${ext.server_count} servers, ${ext.allowed_tool_count} tools):`
              );
              for (const name of ext.servers || []) {
                mcp_lines.push("  • " + name);
              }
            }
            setInfoText(
              "Tools for " + slot + ":\n" +
              list.map((l) => "• " + l).join("\n") +
              extra_lines.join("\n") +
              mcp_lines.join("\n")
            );
          })
          .catch(() => {
            setInfoText("Tools for " + slot + ":\n" + list.map((l) => "• " + l).join("\n"));
          });
        return true;
      }
      case "/loop": {
        // Renamed in recurrence v2 (Docs/recurrence-specs.md §8). The
        // wording matches the spec verbatim so muscle memory of the
        // old command lands on the canonical replacement.
        setInfoText(
          "/loop was renamed /tick. Use /tick N for recurring, " +
          "/tick for one-off, /tick off to disable."
        );
        return true;
      }
      case "/repeat": {
        // Coach repeats — fixed-interval (minutes) recurring prompts.
        //   /repeat                       → list active repeats
        //   /repeat <minutes> <prompt...> → add a repeat
        //   /repeat rm <id>               → delete a repeat
        if (slot !== "coach") {
          setInfoText("/repeat is Coach-only.");
          return true;
        }
        const raw = arg.trim();
        if (!raw) {
          authFetch("/api/recurrences")
            .then((r) => r.json())
            .then((rows) => {
              const repeats = (rows || []).filter((r) => r.kind === "repeat");
              if (repeats.length === 0) {
                setInfoText(
                  "No active repeats. " +
                  "'/repeat <minutes> <prompt>' to add one."
                );
                return;
              }
              const lines = repeats.map((r) =>
                `${String(r.id).padStart(3)}  ${r.enabled ? "on " : "off"}  ` +
                `${String(r.cadence).padStart(4)}m  ${(r.prompt || "").slice(0, 60)}`
              );
              setInfoText("Active repeats:\n" + lines.join("\n"));
            })
            .catch((e) => setInfoText("repeat query failed: " + String(e)));
          return true;
        }
        const rmMatch = raw.match(/^rm\s+(\d+)$/);
        if (rmMatch) {
          const id = parseInt(rmMatch[1], 10);
          authFetch(`/api/recurrences/${id}`, { method: "DELETE" })
            .then(async (r) => {
              if (r.ok) setInfoText(`Repeat ${id} deleted.`);
              else if (r.status === 404) setInfoText(`No repeat ${id}.`);
              else throw new Error(`HTTP ${r.status}`);
            })
            .catch((e) => setInfoText("repeat rm failed: " + String(e)));
          return true;
        }
        const m = raw.match(/^(\d+)\s+([\s\S]+)$/);
        if (!m) {
          setInfoText(
            "usage: /repeat <minutes> <prompt...>  ·  " +
            "/repeat rm <id>  ·  /repeat (list)"
          );
          return true;
        }
        const minutes = parseInt(m[1], 10);
        const prompt = m[2].trim();
        if (!minutes || minutes < 1 || !prompt) {
          setInfoText("usage: /repeat <minutes> <prompt...>");
          return true;
        }
        authFetch("/api/recurrences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            kind: "repeat", cadence: String(minutes), prompt,
          }),
        })
          .then(async (r) => {
            if (!r.ok) {
              const body = await r.text().catch(() => "");
              throw new Error(`HTTP ${r.status}${body ? " — " + body.slice(0, 80) : ""}`);
            }
            return r.json();
          })
          .then((row) => setInfoText(
            `Repeat ${row.id} added: every ${row.cadence}m — ${(row.prompt || "").slice(0, 60)}`
          ))
          .catch((e) => setInfoText("repeat add failed: " + String(e.message || e)));
        return true;
      }
      case "/cron": {
        // Coach cron — friendly DSL recurrences (daily, weekdays, etc).
        //   /cron                      → list
        //   /cron <when> <prompt...>   → add (DSL per recurrence-specs.md §5)
        //   /cron rm <id>              → delete
        if (slot !== "coach") {
          setInfoText("/cron is Coach-only.");
          return true;
        }
        const raw = arg.trim();
        if (!raw) {
          authFetch("/api/recurrences")
            .then((r) => r.json())
            .then((rows) => {
              const crons = (rows || []).filter((r) => r.kind === "cron");
              if (crons.length === 0) {
                setInfoText(
                  "No active crons. " +
                  "'/cron <when> <prompt>' to add one. " +
                  "Examples: 'daily 09:00', 'weekdays 18:00', 'mon,thu 14:00'."
                );
                return;
              }
              const lines = crons.map((r) =>
                `${String(r.id).padStart(3)}  ${r.enabled ? "on " : "off"}  ` +
                `[${r.tz || "UTC"}] ${r.cadence}  ${(r.prompt || "").slice(0, 50)}`
              );
              setInfoText("Active crons:\n" + lines.join("\n"));
            })
            .catch((e) => setInfoText("cron query failed: " + String(e)));
          return true;
        }
        const rmMatch = raw.match(/^rm\s+(\d+)$/);
        if (rmMatch) {
          const id = parseInt(rmMatch[1], 10);
          authFetch(`/api/recurrences/${id}`, { method: "DELETE" })
            .then(async (r) => {
              if (r.ok) setInfoText(`Cron ${id} deleted.`);
              else if (r.status === 404) setInfoText(`No cron ${id}.`);
              else throw new Error(`HTTP ${r.status}`);
            })
            .catch((e) => setInfoText("cron rm failed: " + String(e)));
          return true;
        }
        // Parse "daily HH:MM <prompt>", "weekdays HH:MM <prompt>",
        // "mon,thu HH:MM <prompt>", "weekly DAY HH:MM <prompt>",
        // "monthly DOM HH:MM <prompt>", "YYYY-MM-DD HH:MM <prompt>".
        // The cadence DSL is everything up to the second whitespace
        // *after* the time token, except for `weekly` and `monthly`
        // which spend an extra token before the time. Cleanest split:
        // find the HH:MM token, take everything before it inclusive
        // as the schedule, the rest as the prompt.
        const timeMatch = raw.match(/\b\d{1,2}:\d{2}\b/);
        if (!timeMatch) {
          setInfoText(
            "usage: /cron <when> <prompt...> — when must include " +
            "an HH:MM time. e.g. /cron daily 09:00 morning summary"
          );
          return true;
        }
        const timeEnd = timeMatch.index + timeMatch[0].length;
        const cadence = raw.slice(0, timeEnd).trim();
        const prompt = raw.slice(timeEnd).trim();
        if (!cadence || !prompt) {
          setInfoText(
            "usage: /cron <when> <prompt...>  ·  " +
            "/cron rm <id>  ·  /cron (list)"
          );
          return true;
        }
        const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
        authFetch("/api/recurrences", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            kind: "cron", cadence, prompt, tz,
          }),
        })
          .then(async (r) => {
            if (!r.ok) {
              const body = await r.text().catch(() => "");
              throw new Error(`HTTP ${r.status}${body ? " — " + body.slice(0, 100) : ""}`);
            }
            return r.json();
          })
          .then((row) => setInfoText(
            `Cron ${row.id} added: ${row.cadence} [${row.tz}] — ${(row.prompt || "").slice(0, 60)}`
          ))
          .catch((e) => setInfoText("cron add failed: " + String(e.message || e)));
        return true;
      }
      case "/tick": {
        // /tick           → fire one tick now
        // /tick N         → set recurring tick to every N minutes
        // /tick off       → disable recurring tick
        const a = arg.trim().toLowerCase();
        if (!a) {
          authFetch("/api/coach/tick", { method: "POST" })
            .then((r) => {
              if (r.ok) setInfoText("Coach ticked. Watch their pane.");
              else if (r.status === 409) setInfoText("Coach is already working.");
              else setInfoText("tick failed: HTTP " + r.status);
            })
            .catch((e) => setInfoText("tick failed: " + String(e)));
          return true;
        }
        if (a === "off" || a === "0" || a === "stop") {
          authFetch("/api/coach/tick", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: false }),
          })
            .then(async (r) => {
              if (r.ok) setInfoText("Recurring tick disabled.");
              else if (r.status === 400) setInfoText("No tick to disable yet.");
              else throw new Error("HTTP " + r.status);
            })
            .catch((e) => setInfoText("tick off failed: " + String(e)));
          return true;
        }
        const minutes = parseInt(a, 10);
        if (!minutes || minutes < 1) {
          setInfoText(
            "usage: /tick (fire now)  ·  /tick <minutes>  ·  /tick off"
          );
          return true;
        }
        authFetch("/api/coach/tick", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ minutes }),
        })
          .then(async (r) => {
            if (!r.ok) throw new Error("HTTP " + r.status);
            setInfoText(`Recurring tick: every ${minutes} min.`);
          })
          .catch((e) => setInfoText("tick set failed: " + String(e)));
        return true;
      }
      case "/spend":
        // Per-agent spend breakdown over the last 24h (or whatever
        // hours arg, via `/spend 168` for a week etc.). Pulls from
        // /api/turns/summary — same backing table that gates cost caps.
        {
          const h = parseInt(arg, 10);
          const hours = h > 0 && h <= 720 ? h : 24;
          authFetch("/api/turns/summary?hours=" + hours)
            .then((r) => r.json())
            .then((d) => {
              if (!d.per_agent || d.per_agent.length === 0) {
                setInfoText(`No turns recorded in the last ${hours}h.`);
                return;
              }
              const rows = d.per_agent.map((a) => {
                const errs = a.error_count ? ` · ${a.error_count} err` : "";
                return (
                  `${a.agent_id.padEnd(6)} ` +
                  `$${(a.cost_usd || 0).toFixed(3).padStart(8)} · ` +
                  `${String(a.count).padStart(3)} turn${a.count === 1 ? "" : "s"}${errs}`
                );
              });
              setInfoText(
                `Spend last ${hours}h — total $${d.total_cost_usd.toFixed(3)} · ${d.total_turns} turns\n` +
                rows.join("\n")
              );
            })
            .catch((e) => setInfoText("spend query failed: " + String(e)));
        }
        return true;
      case "/status":
        // Render a compact snapshot of /api/status into the info
        // banner. Human-readable, not the raw JSON.
        authFetch("/api/status")
          .then((r) => r.json())
          .then((d) => {
            const caps = d.caps || {};
            const running = (d.running_slots || []).join(", ") || "none";
            const lines = [
              `paused: ${d.paused ? "yes" : "no"}`,
              `running: ${running}`,
              `team today: $${(caps.team_today_usd || 0).toFixed(3)}` +
                (caps.team_daily_usd
                  ? ` / $${caps.team_daily_usd.toFixed(2)} cap`
                  : " (no cap)"),
              `ws subscribers: ${d.ws_subscribers ?? "?"}`,
              `uptime: ${Math.round((d.uptime_seconds || 0) / 60)} min`,
              `webdav: ${d.webdav?.enabled ? "on" : "off — " + (d.webdav?.reason || "?")}`,
            ];
            setInfoText(lines.join("\n"));
          })
          .catch((e) => setInfoText("status failed: " + String(e)));
        return true;
      case "/help":
        setInfoText(
          "Slash commands (never sent to the agent):\n" +
          SLASH_COMMANDS.map((c) => `${c.cmd.padEnd(8)} — ${c.desc}`).join("\n")
        );
        return true;
      default:
        return false;
    }
  }, [slot, paneSettings]);

  const submit = useCallback(async () => {
    const text = input.trim();
    if (!text && attachments.length === 0) return;
    // Slash commands short-circuit: handle locally, don't send to agent.
    if (text.startsWith("/")) {
      const handled = runSlashCommand(text);
      if (handled) {
        setInput("");
        return;
      }
      // Unrecognized slash — let it fall through so the agent sees it
      // (Claude Code's own slash commands like /init are handled by the
      // CLI, which we don't currently intercept).
    }
    setSubmitting(true);
    let startTimeout = null;
    try {
      // Compose prompt string: include image paths the agent can Read.
      // We reference each attachment via a workspace-local symlinked path
      // (/workspaces/<slot>/attachments/<filename>) so the agent sees it
      // as in-cwd regardless of whether Read has path-subtree restrictions.
      let prompt = text;
      if (attachments.length > 0) {
        const paths = attachments
          .map((a) => `/workspaces/${slot}/attachments/${a.filename}`)
          .join("\n  - ");
        const header = text
          ? "\n\nAttached images (use Read to load):\n  - "
          : "Attached images (use Read to load):\n  - ";
        prompt = text + header + paths;
      }
      // Include per-pane overrides. The server forwards these to
      // ClaudeAgentOptions (model / permission_mode="plan" / effort).
      const reqBody = { agent_id: slot, prompt };
      if (effectiveRuntimeKnown && paneSettings.model && paneModelValidForRuntime) {
        reqBody.model = paneSettings.model;
      }
      if (paneSettings.planMode) reqBody.plan_mode = true;
      if (paneSettings.effort) reqBody.effort = paneSettings.effort;
      const controller = new AbortController();
      startTimeout = setTimeout(() => controller.abort(), 15_000);
      const res = await authFetch("/api/agents/start", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(reqBody),
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const nextHistory = pushPromptHistory(slot, text);
      setPromptHistory(nextHistory);
      setPromptHistoryIdx(null);
      setInput("");
      setAttachments([]);
    } catch (err) {
      console.error("submit failed", err);
      setInfoText("start failed: " + (err && err.name === "AbortError" ? "request timed out" : String(err)));
    } finally {
      if (startTimeout) clearTimeout(startTimeout);
      setSubmitting(false);
    }
  }, [
    input,
    attachments,
    slot,
    paneSettings,
    effectiveRuntimeKnown,
    paneModelValidForRuntime,
  ]);

  // slashOpen: show autocomplete when the input is a single-line "/…"
  // prefix. We intentionally close once the user inserts a newline —
  // that means they've committed to a multi-line prompt, not a command.
  const slashOpen = input.startsWith("/") && !input.includes("\n");
  const slashQuery = slashOpen ? input.split(/\s/)[0] : "";
  const slashFiltered = slashOpen
    ? SLASH_COMMANDS.filter((c) => c.cmd.startsWith(slashQuery.toLowerCase()))
    : [];
  // Keep the selection in bounds as the filter narrows.
  useEffect(() => {
    setSlashIdx(0);
  }, [slashQuery]);

  const onKeyDown = useCallback(
    (e) => {
      // Slash menu navigation — only when it's open AND populated.
      if (slashOpen && slashFiltered.length > 0) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setSlashIdx((i) => Math.min(slashFiltered.length - 1, i + 1));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setSlashIdx((i) => Math.max(0, i - 1));
          return;
        }
        if (e.key === "Tab") {
          e.preventDefault();
          const pick = slashFiltered[Math.min(slashIdx, slashFiltered.length - 1)];
          if (pick) setInput(pick.cmd + " ");
          return;
        }
        if (e.key === "Enter" && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
          e.preventDefault();
          const pick = slashFiltered[Math.min(slashIdx, slashFiltered.length - 1)];
          if (pick) {
            // Run the picked command immediately — no confirmation.
            runSlashCommand(pick.cmd);
            setInput("");
          }
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          setInput("");
          return;
        }
      }
      // Ctrl/Cmd + Enter submits.
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        submit();
        return;
      }
      // Ctrl/Cmd + ↑ / ↓ cycles prompt history. Plain arrows stay as
      // normal textarea caret navigation.
      if ((e.metaKey || e.ctrlKey) && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
        if (promptHistory.length === 0) return;
        e.preventDefault();
        let nextIdx;
        if (e.key === "ArrowUp") {
          nextIdx = promptHistoryIdx == null
            ? promptHistory.length - 1
            : Math.max(0, promptHistoryIdx - 1);
        } else {
          if (promptHistoryIdx == null) return;
          nextIdx = promptHistoryIdx + 1;
          if (nextIdx >= promptHistory.length) {
            setPromptHistoryIdx(null);
            setInput("");
            return;
          }
        }
        setPromptHistoryIdx(nextIdx);
        setInput(promptHistory[nextIdx]);
      }
    },
    [submit, promptHistory, promptHistoryIdx, slashOpen, slashFiltered, slashIdx, runSlashCommand]
  );

  const exportMarkdown = useCallback(() => {
    const body = formatEventsAsMarkdown(allEvents, { slot, agent });
    const header = `Exported ${new Date().toISOString()}\n\n`;
    const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    downloadMarkdown(`${slot}-${stamp}.md`, header + body);
  }, [allEvents, slot, agent]);

  // If the assigned name just duplicates the slot id (e.g. Coach is
  // seeded with id='coach' and name='Coach'), skip the name pill —
  // showing both reads as "coach Coach" which looks like a stutter.
  const rawName = agent?.name || (agent?.kind === "player" ? "unassigned" : slot);
  const displayName =
    rawName.toLowerCase() === slot.toLowerCase() ? "" : rawName;
  const status = agent?.status || "stopped";

  return html`
    <section
      class=${"pane" + (dropEdge ? " drop-edge-" + dropEdge : "")}
      id=${"pane-" + slot}
      onDragOver=${onDragOver}
      onDragLeave=${onDragLeave}
      onDrop=${onDrop}
    >
      <header class="pane-head">
        <span
          class="pane-drag-handle"
          draggable=${true}
          onDragStart=${onDragStart}
          title="Drag to reorder — drop on another pane to move before it"
        >
          <span
            class=${"pane-dot " + status}
            title=${statusTooltip(status, agent)}
          ></span>
          <span class="pane-id">${slotShortLabel(slot)}</span>
          ${displayName
            ? html`<span
                class="pane-name"
                title=${agent?.role ? agent.role : ""}
              >${displayName}</span>`
            : null}
        </span>
        ${currentTask
          ? html`<span
              class="pane-current-task pane-current-task-icon"
              title=${"current task: " + currentTask.title + " (" + currentTask.id + ", " + currentTask.status + ")"}
            >⚑</span>`
          : null}
        ${slot !== "coach"
          ? html`<button
              class=${"pane-lock" + (agent?.locked ? " locked" : " unlocked")}
              onClick=${async () => {
                await authFetch("/api/agents/" + slot + "/locked", {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ locked: !agent?.locked }),
                });
              }}
              title=${agent?.locked
                ? "LOCKED — Coach cannot assign tasks or message this agent; agent skips Coach broadcasts. Click to unlock."
                : "Unlocked — Coach can assign work and broadcast. Click to lock (this agent becomes human-only)."}
              dangerouslySetInnerHTML=${{ __html: agent?.locked
                ? `<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4.5" y="9" width="11" height="8" rx="1.2"/><path d="M7 9V6.5a3 3 0 0 1 6 0V9"/></svg>`
                : `<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4.5" y="9" width="11" height="8" rx="1.2"/><path d="M13 9V6 A3 3 0 0 0 6 4.5"/></svg>` }}
            ></button>`
          : null}
        ${agent?.session_id || agent?.codex_thread_id
          ? html`<button
              class="pane-session-clear"
              onClick=${async () => {
                await authFetch("/api/agents/" + slot + "/session", { method: "DELETE" });
              }}
              title="Clear session — next run starts fresh"
            >🗑</button>`
          : null}
        ${status === "working"
          ? html`<button
              class="pane-cancel"
              onClick=${async () => {
                await authFetch("/api/agents/" + slot + "/cancel", { method: "POST" });
              }}
              title="Cancel the in-flight turn"
            >⏹</button>`
          : null}
        ${hasSettingOverride(paneSettings)
          ? html`<span class="pane-setting-dot" title="pane overrides active" />`
          : null}
        <button
          class=${"pane-search-toggle" + (searchOpen ? " active" : "")}
          onClick=${() => {
            setSearchOpen((v) => !v);
            if (searchOpen) setSearchQuery("");
          }}
          title="Filter events by text"
        >⌕</button>
        <button
          class="pane-export"
          onClick=${exportMarkdown}
          title="Export conversation as markdown"
          disabled=${allEvents.length === 0}
        >↓</button>
        ${stacked
          ? html`<button
              class="pane-pop-out"
              onClick=${() => onPopOut && onPopOut(slot)}
              title="Pop out to its own column"
            >⇱</button>`
          : null}
        <button
          class="pane-gear"
          onClick=${() => setSettingsOpen((v) => !v)}
          title="Pane settings"
        >⚙</button>
        ${onToggleMaximize
          ? html`<button
              class="pane-maximize"
              onClick=${onToggleMaximize}
              title=${isMaximized ? "Restore (show all panes)" : "Maximize (full screen)"}
            >${isMaximized ? "❐" : "⛶"}</button>`
          : null}
        <button class="pane-close" onClick=${onClose} title="Close pane">×</button>
        ${settingsOpen
          ? html`<${PaneSettingsPopover}
              settings=${paneSettings}
              onChange=${setPaneSettings}
              slot=${slot}
              initialBrief=${agent?.brief || ""}
              initialName=${agent?.name || ""}
              initialRole=${agent?.role || ""}
              initialRuntime=${agent?.runtime_override || ""}
              onClose=${() => setSettingsOpen(false)}
            />`
          : null}
      </header>
      ${searchOpen
        ? html`<div class="pane-search">
            <input
              type="text"
              class="pane-search-input"
              placeholder="filter events…"
              value=${searchQuery}
              onInput=${(e) => setSearchQuery(e.target.value)}
              onKeyDown=${(e) => {
                if (e.key === "Escape") {
                  setSearchQuery("");
                  setSearchOpen(false);
                }
              }}
              autofocus
            />
            ${searchQuery
              ? html`<span class="pane-search-count">
                  ${visibleEvents.length}/${allEvents.length}
                </span>`
              : null}
          </div>`
        : null}
      <div class="pane-body" ref=${bodyRef} onScroll=${onBodyScroll}>
        ${!historyLoaded ? html`<div class="loading">loading history…</div>` : null}
        ${historyLoaded && history.length > 0 && !historyExhausted
          ? html`<button
              type="button"
              class="pane-load-older"
              disabled=${loadingOlder}
              onClick=${loadOlder}
              title="Fetch the previous ${HISTORY_PAGE} events from this pane's history"
            >${loadingOlder ? "loading…" : `↑ load older (${HISTORY_PAGE} more)`}</button>`
          : null}
        ${historyLoaded && allEvents.length === 0
          ? html`<div class="pane-empty-hint">
              No conversation yet for ${slot}.
              ${slot === "coach"
                ? html` Try a goal like:<br />
                    <em>"Decompose into tasks: build a tic-tac-toe game in React"</em><br />
                    or just <em>"What's on the team board?"</em>.`
                : html` Type a prompt below to spawn this Player.`}
            </div>`
          : null}
        ${searchQuery.trim() && visibleEvents.length === 0 && allEvents.length > 0
          ? html`<div class="pane-empty-hint">No events match "${searchQuery}".</div>`
          : null}
        <${EventList} events=${visibleEvents} />
        ${streaming && streaming.thinking
          ? html`<div class="event thinking streaming">
              <div class="event-meta">💭 thinking…</div>
              <div class="event-body thinking-body">${streaming.thinking}<span class="stream-cursor" /></div>
            </div>`
          : null}
        ${streaming && streaming.text
          ? html`<div class="event text streaming">
              <div class="event-meta">…</div>
              <div class="event-body">${streaming.text}<span class="stream-cursor" /></div>
            </div>`
          : null}
      </div>
      <footer class="pane-input">
        ${infoText
          ? html`<div class="pane-info">
              <pre class="pane-info-body">${infoText}</pre>
              <button
                class="pane-info-close"
                onClick=${() => setInfoText(null)}
                title="Dismiss"
              >×</button>
            </div>`
          : null}
        ${attachments.length > 0
          ? html`
              <div class="attachments">
                ${attachments.map(
                  (a) => html`
                    <div class="attachment" key=${a.id}>
                      <img src=${a.url} alt=${a.filename} />
                      <button class="x" onClick=${() => removeAttachment(a.id)} title="Remove">×</button>
                    </div>
                  `
                )}
              </div>
            `
          : null}
        ${/* ActiveLoops bar removed in phase 8; see RecurrencePane. */ null}
        <!-- Compact mode chips: one-glance view of the pane's current
             model / plan / effort, each clickable to open the full
             settings popover. Mirrors the Claude-Code-style inline
             bar so users don't have to go hunting for the gear. -->
        <div class="pane-modes">
          <button
            class=${"pane-mode-chip" + (paneSettings.model ? " active" : "")}
            onClick=${() => setSettingsOpen(true)}
            title="Model (click to change)"
          >
            ${modelLabelFor(paneSettings.model || "", effectiveRuntime)}
          </button>
          <button
            class=${"pane-mode-chip" + (paneSettings.planMode ? " active" : "")}
            onClick=${() => setPaneSettings((s) => ({ ...s, planMode: !s.planMode }))}
            title="Plan mode (click to toggle)"
          >
            ${paneSettings.planMode ? "plan ✓" : "plan"}
          </button>
          <button
            class=${"pane-mode-chip" + (paneSettings.effort ? " active" : "")}
            onClick=${() => setSettingsOpen(true)}
            title="Effort (click to change)"
          >
            ${paneSettings.effort
              ? EFFORT_LABELS[paneSettings.effort - 1]
              : "effort"}
          </button>
          <${ContextBar} slot=${slot} liveEvents=${liveEvents} model=${paneSettings.model || ""} />
          <span class="pane-modes-spacer"></span>
          <button
            class="pane-mode-chip pane-mode-slash"
            onClick=${() => setInput("/")}
            title="Slash commands"
          >/ commands</button>
        </div>
        <div class="pane-input-wrap">
          <textarea
            placeholder=${"Message " + (displayName || rawName) + "… (type / for commands)"}
            value=${input}
            onInput=${(e) => {
              setInput(e.target.value);
              if (promptHistoryIdx != null) setPromptHistoryIdx(null);
            }}
            onPaste=${onPaste}
            onKeyDown=${onKeyDown}
            rows=${3}
          ></textarea>
          ${slashOpen && slashFiltered.length > 0
            ? html`<${SlashMenu}
                query=${slashQuery}
                selectedIdx=${Math.min(slashIdx, slashFiltered.length - 1)}
                onPick=${(cmd) => {
                  runSlashCommand(cmd);
                  setInput("");
                }}
                onHover=${(i) => setSlashIdx(i)}
              />`
            : null}
        </div>
        <div class="pane-input-row">
          <span class="hint">⌘/Ctrl+Enter to send · ⌘/Ctrl+↑↓ history · / for commands</span>
          <button
            class="primary"
            disabled=${submitting || (!input.trim() && attachments.length === 0)}
            onClick=${submit}
          >
            ${submitting ? "running…" : "run"}
          </button>
        </div>
      </footer>
    </section>
  `;
}

// ------------------------------------------------------------------
// event renderer (v2b generic; v2c adds per-tool richness)
// ------------------------------------------------------------------

function RuntimeChip({ runtime, compact = false }) {
  const r = (runtime || "claude").toLowerCase();
  if (r !== "claude" && r !== "codex") return null;
  return html`<span
    class=${"runtime-chip runtime-chip-" + r + (compact ? " compact" : "")}
    title=${"runtime: " + r}
    aria-label=${"runtime: " + r}
  />`;
}

function TurnHeader({ event, ts }) {
  const [open, setOpen] = useState(false);
  const prompt = event.prompt || "";
  const oneLiner = prompt.replace(/\s+/g, " ").trim();
  const arrow = event.resumed_session ? "↻" : "→";
  // Compact turns get a condensed one-line renderer — the prompt is a
  // giant boilerplate instruction that clutters the timeline. A badge
  // + short label is enough; expand-click still reveals full text.
  if (event.compact_mode) {
    const label = event.auto_compact
      ? "auto-compacting session"
      : "compacting session";
    return html`<div class=${"event agent_started turn-header compact" + (open ? " open" : "")}>
      <div
        class="turn-header-row"
        onClick=${() => setOpen((v) => !v)}
        title=${open ? "collapse" : "show compact instruction"}
      >
        <span class="turn-header-arrow" title="compact turn">⤵</span>
        <span class="turn-header-ts">${ts}</span>
        <${RuntimeChip} runtime=${event.runtime} />
        <span class="turn-header-compact-badge">${label}</span>
        <span class="turn-header-chev">${open ? "▾" : "▸"}</span>
      </div>
      ${open && prompt
        ? html`<div class="turn-header-full">${prompt}</div>`
        : null}
    </div>`;
  }
  return html`<div class=${"event agent_started turn-header" + (open ? " open" : "")}>
    <div
      class="turn-header-row"
      onClick=${() => setOpen((v) => !v)}
      title=${open ? "collapse" : "expand full prompt"}
    >
      <span
        class="turn-header-arrow"
        title=${event.resumed_session ? "resumed prior session" : "fresh start"}
      >${arrow}</span>
      <span class="turn-header-ts">${ts}</span>
      <${RuntimeChip} runtime=${event.runtime} />
      <span class="turn-header-prompt">${oneLiner || "(empty prompt)"}</span>
      <span class="turn-header-chev">${open ? "▾" : "▸"}</span>
    </div>
    ${open && prompt
      ? html`<div class="turn-header-full">${prompt}</div>`
      : null}
  </div>`;
}

function ThinkingItem({ event, ts }) {
  const [open, setOpen] = useState(false);
  // Defensive: event.content is sometimes a non-string (Claude thinking
  // blocks arrive as arrays of objects). _coerceContentToString handles
  // every shape we've seen.
  const content = _coerceContentToString(event.content);
  const lines = content.split(/\n/).length;
  return html`<div class=${"event thinking" + (open ? " open" : "")}>
    <div
      class="event-meta thinking-head"
      onClick=${() => setOpen((v) => !v)}
      title=${open ? "collapse" : "expand"}
    >
      ${ts}  💭 thought ${open ? "▾" : "▸"}  <span class="thinking-sub">${lines} line${lines === 1 ? "" : "s"}</span>
    </div>
    ${open
      ? html`<div
          class="event-body thinking-body markdown"
          dangerouslySetInnerHTML=${{ __html: renderMarkdownFor(event) }}
        />`
      : null}
  </div>`;
}

// Event types that are pure audit noise in the pane body — they're
// useful in the DB / EnvPane timeline for debugging ("did my context
// get picked up?") but shouldn't clutter the conversation view.
// agent_stopped is redundant with the preceding `result` row (which
// already carries duration/cost/error); hiding it removes a dangling
// bare line at the end of every turn.
const _HIDDEN_EVENT_TYPES = new Set([
  "context_applied",
  "agent_stopped",
  "lock_updated",
]);

class EventList extends Component {
  shouldComponentUpdate(nextProps) {
    return nextProps.events !== this.props.events;
  }

  render({ events }) {
    return events.map((ev, i) =>
      html`<${EventItem} key=${ev.__id ?? "live-" + i} event=${ev} />`
    );
  }
}

function EventItem({ event }) {
  const type = event.type;
  const ts = timeStr(event.ts);

  if (_HIDDEN_EVENT_TYPES.has(type)) return null;

  if (type === "tool_use") {
    return html`<div class="event tool_use">
      <div class="event-meta">${ts}</div>
      ${renderToolCall(event)}
    </div>`;
  }

  if (type === "tool_result") {
    const cls = "event tool_result" + (event.is_error ? " error" : "");
    const trimmed = (event.content || "").trim();
    const preview = trimmed.length > 600 ? trimmed.slice(0, 600) + "\n…" : trimmed;
    return html`<div class=${cls}>
      <div class="event-meta">${ts}  ↳ result${event.is_error ? " (error)" : ""}</div>
      <div class="event-body tool-result-body">${preview || "(empty)"}</div>
    </div>`;
  }

  if (type === "text") {
    return html`<div class="event text">
      <div class="event-meta">${ts}</div>
      <div
        class="event-body markdown"
        dangerouslySetInnerHTML=${{ __html: renderMarkdownFor(event) }}
      />
    </div>`;
  }

  if (type === "thinking") {
    // Collapsible by default — thinking is often long and the user can
    // expand when debugging a turn's reasoning.
    return html`<${ThinkingItem} event=${event} ts=${ts} />`;
  }

  if (type === "error") {
    const extras = [];
    if (event.cwd) extras.push("cwd: " + event.cwd);
    return html`<div class="event error">
      <div class="event-meta">${ts} error</div>
      <div class="event-body">${event.error || ""}${
        extras.length
          ? html`<div class="event-error-extras">${extras.join("  ·  ")}</div>`
          : null
      }</div>
    </div>`;
  }

  if (type === "result") {
    const dur = event.duration_ms ?? "?";
    const cost = typeof event.cost_usd === "number" ? `$${event.cost_usd.toFixed(4)}` : "$?";
    // On is_error=true, surface whichever of subtype / stop_reason / errors
    // the SDK populated so the user sees the actual cause instead of a
    // bare "(error)". `subtype` is the most specific (e.g.
    // "error_max_turns" / "error_during_execution"); `stop_reason` is
    // the Anthropic-API-level reason ("max_turns" / "max_tokens"...);
    // `errors[0]` is the first per-step failure string when present.
    let suffix = "";
    if (event.is_error) {
      const parts = [];
      const reason = event.subtype || event.stop_reason;
      if (reason) parts.push(String(reason));
      if (typeof event.num_turns === "number") parts.push(`${event.num_turns} turns`);
      if (Array.isArray(event.errors) && event.errors.length) {
        parts.push(String(event.errors[0]).slice(0, 80));
      }
      suffix = parts.length ? `  (error: ${parts.join(", ")})` : "  (error)";
    }
    return html`<div class="event result">
      <div class="event-meta">${ts}  result  ${dur}ms  ${cost}${suffix}</div>
    </div>`;
  }

  if (type === "agent_started") {
    // Sticky "turn header" — collapses to a one-line prompt preview
    // that sticks to the top of the scrolled pane until the next
    // agent_started event pushes it up. Click to expand the full
    // prompt. Emulates Claude Code's history browser.
    return html`<${TurnHeader} event=${event} ts=${ts} />`;
  }

  if (type === "agent_stopped") {
    return html`<div class="event agent_stopped">
      <div class="event-meta">${ts}  agent_stopped</div>
    </div>`;
  }

  if (type === "task_created") {
    const parent = event.parent_id ? `  ↳${event.parent_id}` : "";
    return html`<div class="event task_created">
      <div class="event-meta">${ts}  task_created</div>
      [${event.task_id}] ${event.title}${parent}
    </div>`;
  }

  // Compact renderers for system / progress events. Kept on a single
  // line so they don't dominate the pane — no raw JSON dumps. Each
  // still carries enough info that the reader can track what happened
  // without digging into the EnvPane timeline.
  if (type === "task_claimed") {
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ${event.agent_id} claimed ${event.task_id}</div>
    </div>`;
  }

  if (type === "task_updated") {
    const arrow = `${event.old_status} → ${event.new_status}`;
    const note = event.note ? ` — ${event.note}` : "";
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ${event.task_id}: ${arrow}${note}</div>
    </div>`;
  }

  if (type === "memory_updated") {
    return html`<div class="event sys">
      <div class="event-meta">${ts} · memory/${event.topic} v${event.version} (${event.size} chars)</div>
    </div>`;
  }

  if (type === "knowledge_written") {
    return html`<div class="event sys">
      <div class="event-meta">${ts} · knowledge/${event.path} (${event.size} chars)</div>
    </div>`;
  }

  if (type === "decision_written") {
    return html`<div class="event sys">
      <div class="event-meta">${ts} · decision: ${event.title}</div>
    </div>`;
  }

  if (type === "context_updated" || type === "context_deleted") {
    const verb = type === "context_deleted" ? "deleted" : "updated";
    return html`<div class="event sys">
      <div class="event-meta">${ts} · context/${event.kind}/${event.name} ${verb}</div>
    </div>`;
  }

  if (type === "file_written") {
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ${event.root}/${event.path} saved (${event.size} b)</div>
    </div>`;
  }

  if (type === "player_assigned") {
    const bits = [];
    if (event.name) bits.push(`name=${event.name}`);
    if (event.role) bits.push(`role="${event.role}"`);
    const src = event.auto ? " (auto)" : "";
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ${event.agent_id}: ${bits.join(" ") || "cleared"}${src}</div>
    </div>`;
  }

  if (type === "session_cleared" || type === "session_resume_failed") {
    // Visually louder than the generic .sys row — this is a real
    // conversation break. The next agent_started will be a fresh
    // session, so the separator helps the eye find where one
    // conversation ended and the next began.
    const msg = type === "session_cleared"
      ? "SESSION CLEARED — next turn starts fresh"
      : `SESSION RESUME FAILED — ${event.error || ""}`;
    return html`<div class="event session-break">
      <span class="session-break-rule" />
      <span class="session-break-label">${ts} · ${msg}</span>
      <span class="session-break-rule" />
    </div>`;
  }

  if (type === "commit_pushed") {
    const push = event.pushed ? "↑" : event.push_requested ? "✗push" : "local";
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ${event.sha} ${push} — ${event.message}</div>
    </div>`;
  }

  // Recurrence v2 phase 8: legacy `coach_loop_changed` and
  // `coach_repeat_changed` event renderers removed. The new
  // `recurrence_added` / `recurrence_changed` / `recurrence_disabled`
  // events fall through to the generic .sys renderer; the Recurrence
  // pane is the primary surface.

  if (type === "auto_compact_triggered") {
    const r = event.ratio != null ? ` (${Math.round(event.ratio * 100)}% of ${event.context_window || "?"})` : "";
    return html`<div class="event sys">
      <div class="event-meta">${ts} · auto-compact triggered${r}</div>
    </div>`;
  }

  if (type === "session_compacted") {
    const f = event.handoff_file ? ` → handoffs/${event.handoff_file}` : "";
    return html`<div class="event sys">
      <div class="event-meta">${ts} · session compacted (${event.chars || 0} chars)${f}</div>
    </div>`;
  }

  if (type === "pending_question") {
    const route = event.route === "coach" ? "→ coach" : "→ human";
    const n = (event.questions || []).length;
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ⏸ paused on AskUserQuestion ${route} (${n}Q, id=${(event.correlation_id || "").slice(0, 8)})</div>
    </div>`;
  }

  if (type === "question_answered") {
    const n = event.answer_count != null
      ? event.answer_count
      : (event.answer_keys || []).length;
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ▶ question answered (${n} keys, id=${(event.correlation_id || "").slice(0, 8)})</div>
    </div>`;
  }

  if (type === "question_cancelled") {
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ⚠ question cancelled: ${event.reason || ""}</div>
    </div>`;
  }

  if (type === "pending_plan") {
    const route = event.route === "coach" ? "→ coach" : "→ human";
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ⏸ paused on ExitPlanMode ${route} (id=${(event.correlation_id || "").slice(0, 8)})</div>
    </div>`;
  }

  if (type === "plan_decided") {
    const dec = event.decision || "?";
    const note = event.has_comments ? " + comments" : "";
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ▶ plan ${dec}${note} (id=${(event.correlation_id || "").slice(0, 8)})</div>
    </div>`;
  }

  if (type === "plan_cancelled") {
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ⚠ plan review cancelled: ${event.reason || ""}</div>
    </div>`;
  }

  if (type === "interaction_extended") {
    const kind = event.interaction_kind || "interaction";
    const secs = event.seconds_from_now || 0;
    const mins = Math.round(secs / 60);
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ⏱ ${kind} deadline extended by ${mins} min (id=${(event.correlation_id || "").slice(0, 8)})</div>
    </div>`;
  }

  if (type === "compact_empty_forced" || type === "auto_compact_failed") {
    const msg = type === "compact_empty_forced"
      ? "compact produced no summary — session force-cleared to escape loop"
      : `auto-compact failed${event.error ? ": " + event.error : ""}`;
    return html`<div class="event session-break">
      <span class="session-break-rule" />
      <span class="session-break-label">${ts} · ${msg}</span>
      <span class="session-break-rule" />
    </div>`;
  }

  if (type === "human_attention") {
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ⚠ human attention: ${event.subject || ""}</div>
    </div>`;
  }

  if (type === "paused" || type === "pause_toggled") {
    const body = type === "pause_toggled"
      ? (event.paused ? "harness paused" : "harness resumed")
      : "spawn blocked — harness is paused";
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ${body}</div>
    </div>`;
  }

  if (type === "cost_capped") {
    return html`<div class="event sys">
      <div class="event-meta">${ts} · cost cap · ${event.reason || ""}</div>
    </div>`;
  }

  if (type === "spawn_rejected") {
    return html`<div class="event sys">
      <div class="event-meta">${ts} · spawn rejected · ${event.reason || ""}</div>
    </div>`;
  }

  if (type === "agent_cancelled") {
    return html`<div class="event sys">
      <div class="event-meta">${ts} · cancelled</div>
    </div>`;
  }

  if (type === "brief_updated") {
    return html`<div class="event sys">
      <div class="event-meta">${ts} · brief updated (${event.size} chars)</div>
    </div>`;
  }

  if (type === "task_assigned") {
    // Shows in both Coach's pane (as actor) and the assignee's pane
    // (fan-out). "from → to" phrasing keeps the direction readable
    // from either angle.
    return html`<div class="event task_assigned">
      <div class="event-meta">${ts}  task_assigned</div>
      <div class="event-body">
        ${event.agent_id} → ${event.to} · ${event.task_id}
      </div>
    </div>`;
  }

  if (type === "message_sent") {
    const subj = event.subject ? `  (${event.subject})` : "";
    const urgent = event.priority === "interrupt" ? " ⚠" : "";
    const preview = (event.body_preview || "").slice(0, 160);
    return html`<div class="event message_sent">
      <div class="event-meta">
        ${ts}  ${event.agent_id} → ${event.to}${urgent}${subj}
      </div>
      <div class="event-body">${preview}</div>
    </div>`;
  }

  if (type === "connected") {
    return html`<div class="event connected">
      <div class="event-meta">${ts}  ${event.agent_id || "system"}  connected</div>
    </div>`;
  }

  if (type === "team_tools_updated" || type === "tools_updated") {
    const list = Array.isArray(event.tools) ? event.tools : [];
    const body = list.length === 0 ? "(baseline only)" : list.join(" · ");
    const scope = type === "team_tools_updated" ? "team extras" : "extra tools";
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ${scope} → ${body}</div>
    </div>`;
  }

  // Fallback — unknown event type. Render as a compact .sys row with
  // just the type name; no JSON dump. If we ever need the details,
  // they're still in /api/events?id=<__id>.
  return html`<div class="event sys">
    <div class="event-meta">${ts} · ${type}</div>
  </div>`;
}

// ------------------------------------------------------------------
// boot
// ------------------------------------------------------------------

render(html`<${App} />`, document.getElementById("app"));

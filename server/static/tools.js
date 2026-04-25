// Per-tool renderer registry.
// v2c step 2: per-tool summarizers, category indicator dots, Edit diff card.
// The LLM can emit any tool name; generic fallback always produces a VNode.

// Preact stays on esm.sh (shared with app.js — see comment at top of
// app.js for why). Other deps are vendored under /static/vendor/.
import { h } from "https://esm.sh/preact@10";
import htm from "/static/vendor/htm.js";
import { diffLines } from "/static/vendor/diff.js";
import hljs from "/static/vendor/hljs-core.js";
const html = htm.bind(h);

// File extension → hljs language alias. Languages are registered in
// app.js (single source of truth so we don't double-load packs); this
// map just turns a path's extension into the alias hljs.getLanguage()
// recognizes. Unknown extensions fall through to plaintext rendering.
const EXT_TO_LANG = {
  ".bash": "bash", ".sh": "bash", ".zsh": "bash",
  ".css": "css",
  ".go": "go",
  ".html": "html", ".htm": "html", ".xml": "xml",
  ".js": "javascript", ".mjs": "javascript", ".jsx": "javascript", ".cjs": "javascript",
  ".json": "json",
  ".md": "markdown", ".markdown": "markdown",
  ".py": "python",
  ".rs": "rust",
  ".sql": "sql",
  ".ts": "typescript", ".tsx": "typescript",
  ".yaml": "yaml", ".yml": "yaml",
};

function langForFile(path) {
  if (typeof path !== "string") return "";
  const m = /\.[A-Za-z0-9]+$/.exec(path);
  if (!m) return "";
  return EXT_TO_LANG[m[0].toLowerCase()] || "";
}

function escHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// Highlight a single line in isolation. hljs may lose multi-line state
// (open string literals etc.) but for a per-line diff that's fine —
// the alternative is to highlight whole sides and re-split, which gets
// ugly when added/removed regions interleave with context. Returns
// HTML; falls back to escaped plaintext if the language isn't loaded.
function highlightLine(line, lang) {
  if (!lang || !hljs.getLanguage(lang)) return escHtml(line);
  try {
    return hljs.highlight(line, {
      language: lang, ignoreIllegals: true,
    }).value;
  } catch (_) {
    return escHtml(line);
  }
}

// ------------------------------------------------------------------
// helpers
// ------------------------------------------------------------------

function stripMcp(name) {
  // "mcp__coord__coord_list_tasks" -> "coord_list_tasks"
  return name.replace(/^mcp__[^_]+__/, "");
}

function truncate(s, n) {
  if (typeof s !== "string") return "";
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function lineCount(s) {
  if (typeof s !== "string" || !s) return 0;
  return s.split("\n").length;
}

// ------------------------------------------------------------------
// agent directory — slot id (p3) → human name (Gait). App.js calls
// setAgentDirectory() on every agents-state refresh; summarizers
// read the cache synchronously. Falls back to the slot id if the
// agent has no name yet (fresh boot before Coach assigns).
// ------------------------------------------------------------------

let _agentDirectory = {};

export function setAgentDirectory(agents) {
  const next = {};
  for (const a of agents || []) {
    if (a && a.id) next[a.id] = a.name || "";
  }
  _agentDirectory = next;
}

function slotLabel(slot) {
  if (!slot) return "";
  const s = String(slot).trim().toLowerCase();
  if (s === "broadcast") return "team";
  if (s === "human") return "human";
  const name = _agentDirectory[s];
  if (name) return name;
  if (s === "coach") return "Coach";
  return s;
}

// ------------------------------------------------------------------
// per-tool summarizers
// each returns a short one-line description of the tool call
// ------------------------------------------------------------------

const SUMMARIZERS = {
  Read: (i) => i.file_path || i.path || "",
  Write: (i) => {
    const p = i.file_path || i.path || "";
    const n = lineCount(i.content);
    return n ? `${p}  (${n} lines)` : p;
  },
  Edit: (i) => i.file_path || i.path || "",
  Bash: (i) => truncate(i.command || "", 120),
  Grep: (i) => {
    const p = i.pattern ? `"${i.pattern}"` : "";
    const scope = i.glob
      ? `  in ${i.glob}`
      : i.path
      ? `  in ${i.path}`
      : i.type
      ? `  (${i.type} files)`
      : "";
    return p + scope;
  },
  Glob: (i) => i.pattern || "",
};

// ------------------------------------------------------------------
// Friendly verb-phrase summarizers. When a tool has an entry here,
// the header shows just the phrase (raw tool name moves to a tooltip
// + still visible in the expanded JSON). Goal: replace
//   `coord_list_tasks · status=in_progress owner=null`
// with
//   `Checking in-progress tasks`
// ------------------------------------------------------------------

const FRIENDLY = {
  // --- built-ins that benefit from a verb ---
  ToolSearch: (i) => (i.query ? `Searching tools: "${i.query}"` : "Searching tools"),
  WebFetch: (i) => (i.url ? `Fetching ${i.url}` : "Fetching URL"),
  WebSearch: (i) => (i.query ? `Searching web: "${i.query}"` : "Searching web"),
  AskUserQuestion: (i) => {
    const qs = Array.isArray(i?.questions) ? i.questions : [];
    if (qs.length === 0) return "Asking user";
    return `Asking user: "${truncate(qs[0]?.question || qs[0]?.prompt || "", 80)}"${qs.length > 1 ? ` (+${qs.length - 1} more)` : ""}`;
  },
  ExitPlanMode: () => "Submitting plan for approval",

  // --- tasks ---
  coord_list_tasks: (i) => {
    const status = (i.status || "").trim().toLowerCase();
    const owner = (i.owner || "").trim().toLowerCase();
    const statusPart = status ? `${status} ` : "";
    if (owner) return `Checking ${statusPart}tasks owned by ${slotLabel(owner)}`;
    if (status) return `Checking ${status} tasks`;
    return "Listing all tasks";
  },
  coord_create_task: (i) => {
    const t = i.title ? `"${truncate(i.title, 80)}"` : "(untitled)";
    const p = i.priority && i.priority !== "normal" ? ` [${i.priority}]` : "";
    const parent = i.parent_id ? ` under ${i.parent_id}` : "";
    return `Creating task ${t}${parent}${p}`;
  },
  coord_claim_task: (i) => `Claiming task ${i.task_id || ""}`.trim(),
  coord_update_task: (i) => {
    const id = i.task_id || "task";
    if (i.status) return `Updating ${id} → ${i.status}`;
    return `Noting progress on ${id}`;
  },
  coord_assign_task: (i) => `Assigning ${i.task_id || "task"} → ${slotLabel(i.to)}`,

  // --- messaging ---
  coord_send_message: (i) => {
    const to = slotLabel(i.to);
    const snippet =
      (i.subject && truncate(i.subject, 80)) ||
      truncate(((i.body || "").trim().replace(/\s+/g, " ")), 80);
    const urg = (i.priority === "interrupt") ? " [interrupt]" : "";
    return snippet ? `→ ${to}: "${snippet}"${urg}` : `→ ${to}${urg}`;
  },
  coord_read_inbox: () => "Reading inbox",

  // --- memory / knowledge / decisions / context / outputs ---
  coord_list_memory: () => "Listing memory topics",
  coord_read_memory: (i) => `Reading memory: ${i.topic || ""}`.trim(),
  coord_update_memory: (i) => `Updating memory: ${i.topic || ""}`.trim(),
  coord_list_knowledge: () => "Listing knowledge",
  coord_read_knowledge: (i) => `Reading knowledge: ${i.path || ""}`.trim(),
  coord_write_knowledge: (i) => `Saving knowledge: ${i.path || ""}`.trim(),
  coord_save_output: (i) => `Saving output: ${i.path || ""}`.trim(),
  coord_write_decision: (i) =>
    `Writing decision: "${truncate(i.title || "(untitled)", 80)}"`,
  coord_write_context: (i) => {
    const kind = i.kind || "root";
    const name = i.name || (kind === "root" ? "CLAUDE" : "?");
    return `Writing context: ${kind}/${name}`;
  },

  // --- team / workflow ---
  coord_list_team: () => "Listing team roster",
  coord_set_player_role: (i) =>
    `Assigning role: ${i.player_id || "?"} → ${i.name || "?"}${i.role ? ` (${truncate(i.role, 40)})` : ""}`,
  coord_commit_push: (i) => {
    const msg = i.message ? ` "${truncate(i.message, 80)}"` : "";
    const pushOff = String(i.push).toLowerCase() === "false" ? " (commit only)" : "";
    return `Committing${msg}${pushOff}`;
  },

  // --- interactions / escalation ---
  coord_answer_question: (i) =>
    `Answering question ${i.correlation_id ? `(${truncate(i.correlation_id, 8)}…)` : ""}`.trim(),
  coord_answer_plan: (i) => {
    const verb =
      i.decision === "approve" ? "Approving plan" :
      i.decision === "reject" ? "Rejecting plan" :
      i.decision === "approve_with_comments" ? "Approving plan with comments" :
      `Plan decision: ${i.decision || "?"}`;
    return verb;
  },
  coord_request_human: (i) => {
    const urg = i.urgency === "blocker" ? " [BLOCKER]" : "";
    const subj = i.subject ? `"${truncate(i.subject, 80)}"` : "";
    return `Escalating to human${urg}${subj ? ": " + subj : ""}`;
  },
};

// Fallback for unknown tools: pick the first sensible-looking key.
function genericSummary(input) {
  if (!input || typeof input !== "object") return "";
  for (const k of ["file_path", "path", "filename", "url", "pattern", "query", "title", "command"]) {
    if (input[k]) return String(input[k]).slice(0, 160);
  }
  const keys = Object.keys(input);
  if (keys.length === 0) return "";
  const k = keys[0];
  return `${k}=${truncate(JSON.stringify(input[k]), 60)}`;
}

function summarize(name, input) {
  const fn = SUMMARIZERS[name];
  try {
    if (fn) return fn(input) || "";
  } catch (_) {
    // fall through to generic
  }
  return genericSummary(input);
}

// ------------------------------------------------------------------
// category (drives the indicator-dot color)
// ------------------------------------------------------------------

const CATEGORY = {
  Read: "read",
  Grep: "read",
  Glob: "read",
  ToolSearch: "read",
  WebFetch: "read",
  WebSearch: "read",
  Write: "write",
  Edit: "write",
  NotebookEdit: "write",
  Bash: "run",
};

function category(name) {
  if (CATEGORY[name]) return CATEGORY[name];
  if (name.startsWith("coord_")) return "coord";
  return "other";
}

// ------------------------------------------------------------------
// image path helpers (for Read rendering)
// ------------------------------------------------------------------

const IMAGE_EXT_RE = /\.(png|jpe?g|gif|webp)$/i;

function isImagePath(p) {
  return typeof p === "string" && IMAGE_EXT_RE.test(p);
}

function imageSrcForPath(path) {
  // Prefer the /api/attachments route — both /data/attachments/xyz.png
  // and /workspaces/<slot>/attachments/xyz.png resolve to the same file,
  // and /api/attachments/xyz.png serves it back to the browser.
  const m = path.match(/\/attachments\/([^/]+)$/);
  if (m) return `/api/attachments/${m[1]}`;
  // Fallback: try the path as-is (will 404 unless it happens to be
  // under /api/attachments).
  return path;
}

// ------------------------------------------------------------------
// shared result-block rendering
// ------------------------------------------------------------------

function renderResultBlock(result) {
  if (!result) return null;
  const err = !!result.is_error;
  const content = result.content || "";
  const trimmed = content.length > 2000 ? content.slice(0, 2000) + "\n…" : content;
  return html`
    <div class=${"tool-result-inline" + (err ? " error" : "")}>
      <div class="tool-result-label">↳ ${err ? "error" : "result"}</div>
      <pre>${trimmed || "(empty)"}</pre>
    </div>
  `;
}

// ------------------------------------------------------------------
// Edit diff card — renders input.old_string → input.new_string as red/green blocks
// ------------------------------------------------------------------

// Split `value` from a diffLines part into individual lines, dropping
// the trailing empty entry that comes from a terminal '\n'.
function _splitLines(s) {
  const lines = (s || "").split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

// Build aligned diff rows for a side-by-side viewer. Each row is
// {left, right} where each side is either null (blank placeholder
// keeping the row aligned) or {kind, text}. kind ∈ {ctx, del, add}.
//
// Pairing strategy: when a `removed` part is immediately followed by
// an `added` part, treat them as a modification and zip line-by-line
// so each old line lines up with its corresponding new line. Pure
// adds (no preceding remove) get blank-left rows; pure removes get
// blank-right rows. Context appears identically on both sides so the
// reader can scan horizontally.
function buildSideBySideRows(oldStr, newStr) {
  const parts = diffLines(oldStr || "", newStr || "");
  const rows = [];
  for (let i = 0; i < parts.length; i++) {
    const p = parts[i];
    if (!p.added && !p.removed) {
      for (const line of _splitLines(p.value)) {
        rows.push({
          left: { kind: "ctx", text: line },
          right: { kind: "ctx", text: line },
        });
      }
      continue;
    }
    if (p.removed) {
      const next = parts[i + 1];
      const oldLines = _splitLines(p.value);
      if (next && next.added) {
        const newLines = _splitLines(next.value);
        const maxLen = Math.max(oldLines.length, newLines.length);
        for (let j = 0; j < maxLen; j++) {
          rows.push({
            left: j < oldLines.length ? { kind: "del", text: oldLines[j] } : null,
            right: j < newLines.length ? { kind: "add", text: newLines[j] } : null,
          });
        }
        i++; // consumed the paired add
      } else {
        for (const line of oldLines) {
          rows.push({ left: { kind: "del", text: line }, right: null });
        }
      }
      continue;
    }
    // Pure addition (no preceding removal).
    for (const line of _splitLines(p.value)) {
      rows.push({ left: null, right: { kind: "add", text: line } });
    }
  }
  return rows;
}

// Real changed-line counts so the header (-N +M) reflects what
// actually changed (not the raw old_string / new_string line counts
// which double-count shared context).
function diffStats(rows) {
  let add = 0, del = 0;
  for (const r of rows) {
    if (r.left && r.left.kind === "del") del++;
    if (r.right && r.right.kind === "add") add++;
  }
  return { add, del };
}

function _renderHalf(side, lang) {
  const kind = side ? side.kind : "blank";
  const html_ = side ? (highlightLine(side.text, lang) || "&nbsp;") : "&nbsp;";
  // No prefix gutter — the band color carries the add/del signal.
  // Antigravity-style: text starts at the left edge of the half with
  // just a few pixels of breathing room.
  return html`
    <div
      class=${"diff-half diff-" + kind}
      dangerouslySetInnerHTML=${{ __html: html_ }}
    />`;
}

function renderEditCard(name, input, result) {
  const filePath = input.file_path || input.path || "";
  const oldStr = typeof input.old_string === "string" ? input.old_string : "";
  const newStr = typeof input.new_string === "string" ? input.new_string : "";
  const rows = buildSideBySideRows(oldStr, newStr);
  const { add, del } = diffStats(rows);
  const delta = ` (-${del} +${add})`;
  const lang = langForFile(filePath);
  const langClass = lang ? " language-" + lang : "";
  return html`
    <details class="tool-card category-write edit-card" open>
      <summary>
        <span class="tool-name">${name}</span>
        <span class="tool-summary">${filePath}${delta}</span>
        ${lang ? html`<span class="tool-lang">${lang}</span>` : null}
      </summary>
      <div class=${"diff diff-split hljs" + langClass}>
        ${rows.length === 0
          ? html`<div class="diff-row">
              ${_renderHalf({ kind: "ctx", text: "(no change)" }, "")}
              ${_renderHalf({ kind: "ctx", text: "(no change)" }, "")}
            </div>`
          : rows.map((r, i) => html`
              <div key=${i} class="diff-row">
                ${_renderHalf(r.left, lang)}
                ${_renderHalf(r.right, lang)}
              </div>`)}
      </div>
      ${result && result.is_error ? renderResultBlock(result) : null}
    </details>
  `;
}

// ------------------------------------------------------------------
// Read of an image — render <img> inline
// ------------------------------------------------------------------

function renderReadImageCard(name, input, result) {
  const path = input.file_path || input.path || "";
  const src = imageSrcForPath(path);
  const err = result && result.is_error;
  return html`
    <details class="tool-card category-read read-image-card" open>
      <summary>
        <span class="tool-name">${name}</span>
        <span class="tool-summary">${path}</span>
      </summary>
      ${err
        ? renderResultBlock(result)
        : html`<img class="read-image" src=${src} alt=${path} />`}
    </details>
  `;
}

// ------------------------------------------------------------------
// generic card used by every tool that doesn't have a custom renderer
// ------------------------------------------------------------------

function renderGenericCard(name, input, result) {
  const cat = category(name);
  const friendlyFn = FRIENDLY[name];
  let phrase = "";
  if (friendlyFn) {
    try { phrase = friendlyFn(input) || ""; } catch (_) { phrase = ""; }
  }
  // Friendly phrase replaces the `toolname · key=value` header.
  // Raw tool name moves to a tooltip + stays in the expanded JSON.
  const primary = phrase || name;
  const secondary = phrase ? "" : summarize(name, input);
  return html`
    <details class=${"tool-card category-" + cat}>
      <summary title=${name}>
        <span class=${phrase ? "tool-name tool-phrase" : "tool-name"}>${primary}</span>
        ${secondary ? html`<span class="tool-summary">${secondary}</span>` : null}
      </summary>
      <pre class="tool-input-json">${JSON.stringify(input, null, 2)}</pre>
      ${renderResultBlock(result)}
    </details>
  `;
}

// ------------------------------------------------------------------
// public entry point
// ------------------------------------------------------------------

export function renderToolCall(event) {
  const rawName = event?.name || "?";
  const name = stripMcp(rawName);
  const input = event?.input || {};
  const result = event?.__result;

  if (name === "Edit" && (input.old_string || input.new_string)) {
    return renderEditCard(name, input, result);
  }

  if (name === "Read" && isImagePath(input.file_path || input.path)) {
    return renderReadImageCard(name, input, result);
  }

  return renderGenericCard(name, input, result);
}

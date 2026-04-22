// Per-tool renderer registry.
// v2c step 2: per-tool summarizers, category indicator dots, Edit diff card.
// The LLM can emit any tool name; generic fallback always produces a VNode.

import { h } from "https://esm.sh/preact@10";
import htm from "https://esm.sh/htm@3";
const html = htm.bind(h);

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
  ToolSearch: (i) => (i.query ? `"${i.query}"` : ""),
  WebFetch: (i) => i.url || "",
  WebSearch: (i) => (i.query ? `"${i.query}"` : ""),

  // coord_* — called as mcp__coord__coord_*; stripMcp leaves "coord_*"
  coord_create_task: (i) => {
    const t = i.title ? `"${i.title}"` : "";
    const p = i.priority && i.priority !== "normal" ? `  pri=${i.priority}` : "";
    return t + p;
  },
  coord_list_tasks: (i) => {
    const parts = [];
    if (i.status) parts.push(`status=${i.status}`);
    if (i.owner) parts.push(`owner=${i.owner}`);
    return parts.join("  ");
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
// Edit diff card — renders input.old_string → input.new_string as red/green blocks
// ------------------------------------------------------------------

function renderEditCard(name, input) {
  const filePath = input.file_path || input.path || "";
  const oldStr = typeof input.old_string === "string" ? input.old_string : "";
  const newStr = typeof input.new_string === "string" ? input.new_string : "";
  const oldLines = lineCount(oldStr);
  const newLines = lineCount(newStr);
  const delta = ` (-${oldLines} +${newLines})`;
  return html`
    <details class="tool-card category-write edit-card" open>
      <summary>
        <span class="tool-name">${name}</span>
        <span class="tool-summary">${filePath}${delta}</span>
      </summary>
      <div class="diff">
        <div class="diff-old"><pre>${oldStr || "(empty)"}</pre></div>
        <div class="diff-new"><pre>${newStr || "(empty)"}</pre></div>
      </div>
    </details>
  `;
}

// ------------------------------------------------------------------
// generic card used by every tool that doesn't have a custom renderer
// ------------------------------------------------------------------

function renderGenericCard(name, input) {
  const cat = category(name);
  const summary = summarize(name, input);
  return html`
    <details class=${"tool-card category-" + cat}>
      <summary>
        <span class="tool-name">${name}</span>
        <span class="tool-summary">${summary}</span>
      </summary>
      <pre>${JSON.stringify(input, null, 2)}</pre>
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

  if (name === "Edit" && (input.old_string || input.new_string)) {
    return renderEditCard(name, input);
  }

  return renderGenericCard(name, input);
}

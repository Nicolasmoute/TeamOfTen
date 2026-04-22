// Per-tool renderer registry.
// v2b ships with the generic fallback only. v2c will add specific
// renderers (Read / Edit / Write / Bash / Grep / Glob / ToolSearch /
// coord_* / ...). Consumers call `renderToolCall(event)` which always
// returns a VNode; the LLM can't break us by emitting unknown tools.

import { h } from "https://esm.sh/preact@10";
import htm from "https://esm.sh/htm@3";
const html = htm.bind(h);

function stripMcp(name) {
  // "mcp__coord__coord_list_tasks" -> "coord_list_tasks"
  // "mcp__spike__spike_ping" -> "spike_ping"
  return name.replace(/^mcp__[^_]+__/, "");
}

function oneLineSummary(name, input) {
  if (!input || typeof input !== "object") return "";
  if (input.file_path) return String(input.file_path);
  if (input.path) return String(input.path);
  if (input.filename) return String(input.filename);
  if (input.pattern) return String(input.pattern);
  if (input.query) return `"${input.query}"`;
  if (input.title) return `"${input.title}"`;
  if (input.command) return String(input.command).slice(0, 120);
  if (input.url) return String(input.url);
  // Fallback: one key=value pair
  const keys = Object.keys(input);
  if (keys.length) return `${keys[0]}=${JSON.stringify(input[keys[0]]).slice(0, 60)}`;
  return "";
}

export function renderToolCall(event) {
  const rawName = event?.name || "?";
  const name = stripMcp(rawName);
  const input = event?.input || {};
  const summary = oneLineSummary(name, input);
  return html`
    <details class="tool-card">
      <summary>
        <span class="tool-name">${name}</span>
        <span class="tool-summary">${summary}</span>
      </summary>
      <pre>${JSON.stringify(input, null, 2)}</pre>
    </details>
  `;
}

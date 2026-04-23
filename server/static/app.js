import { h, render } from "https://esm.sh/preact@10";
import { useState, useEffect, useMemo, useRef, useCallback, useLayoutEffect } from "https://esm.sh/preact@10/hooks";
import htm from "https://esm.sh/htm@3";
import Split from "https://esm.sh/split.js@1.6.5";
import { renderToolCall } from "/static/tools.js";

const html = htm.bind(h);

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
    return {
      openColumns,
      envOpen: typeof v.envOpen === "boolean" ? v.envOpen : true,
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
      lines.push("", "**" + (ev.name || "tool") + "**");
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
  { cmd: "/clear",  desc: "clear session so the next turn starts fresh" },
  { cmd: "/cancel", desc: "cancel the in-flight turn on this pane" },
  { cmd: "/loop",   desc: "Coach autoloop: /loop 60 → tick every 60s · /loop off" },
  { cmd: "/tick",   desc: "nudge Coach to drain inbox right now" },
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

function PaneSettingsPopover({ settings, onChange, onClose, slot, initialBrief, initialName, initialRole }) {
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
      if (!rootRef.current.contains(e.target)) onClose();
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
          ${MODEL_OPTIONS.map(
            (m) => html`<option value=${m.value}>${m.label}</option>`
          )}
        </select>
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

function timeStr(iso) {
  return (iso || "").slice(11, 19);
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
  // Per-slot latest ts the user has "acknowledged" by opening that
  // pane. Slots currently in openColumns are always considered seen.
  // Lives in React state only (session-scoped) — a page reload
  // legitimately re-shows the "new activity since last visit" badge.
  const [seenTs, setSeenTs] = useState({});
  // bumping this re-runs the WS effect, which re-opens a new connection
  const [wsAttempt, setWsAttempt] = useState(0);

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
    const statusTimer = setInterval(loadStatus, 30_000);
    return () => clearInterval(statusTimer);
  }, [loadAgents, loadTasks, loadStatus, loadPause]);

  // Persist layout (open slots + env panel state) on every change.
  useEffect(() => {
    saveLayout({ openColumns, envOpen });
  }, [openColumns, envOpen]);

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
      reopenTimer = setTimeout(() => setWsAttempt((a) => a + 1), 2000);
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
      const aid = ev.agent_id || "system";
      // Streaming deltas update a separate ephemeral buffer so the
      // conversations list (persisted / reloaded) stays clean. Text &
      // thinking are kept in distinct slots per agent; either can be
      // active independently.
      if (ev.type === "text_delta" || ev.type === "thinking_delta") {
        setStreamingText((prev) => {
          const next = new Map(prev);
          const cur = next.get(aid) || { text: "", thinking: "" };
          const key = ev.type === "text_delta" ? "text" : "thinking";
          next.set(aid, { ...cur, [key]: cur[key] + (ev.delta || "") });
          return next;
        });
        return;
      }
      // Cross-pane fan-out: inter-agent events land in BOTH the actor's
      // pane and the target's pane so a user watching p3 can see the
      // message from Coach without having to open Coach's pane too.
      // Events carry an explicit recipient:
      //   - message_sent: to_id (an agent id, 'coach', or 'broadcast')
      //   - task_assigned: to    (always a slot id)
      // Broadcasts fan to every agent id we know about.
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
      // lingering after the real text/thinking event renders.
      if (ev.type === "text" || ev.type === "thinking") {
        setStreamingText((prev) => {
          const cur = prev.get(aid);
          if (!cur) return prev;
          const next = new Map(prev);
          const key = ev.type === "text" ? "text" : "thinking";
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
        ev.type === "agent_cancelled"
      ) {
        loadAgents();
      }
      if (ev.type === "pause_toggled") {
        setPaused(Boolean(ev.paused));
      }
      if (ev.type === "result" || ev.type === "cost_capped") {
        loadStatus();
      }
      if (
        ev.type === "task_created" ||
        ev.type === "task_claimed" ||
        ev.type === "task_assigned" ||
        ev.type === "task_updated"
      ) {
        loadTasks();
        loadAgents();
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
  }, [loadAgents, loadTasks, loadStatus, wsAttempt]);

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
  // to find it.
  const openPane = useCallback((slot) => {
    let alreadyOpen = false;
    setOpenColumns((prev) => {
      if (flatSlots(prev).includes(slot)) {
        alreadyOpen = true;
        return prev;
      }
      return [...prev, [slot]];
    });
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
    markSeen(slot);
  }, [markSeen]);
  // Append a slot to the bottom of the rightmost (last) column. If
  // slot already open anywhere else, move it. If no columns yet, opens
  // as the first one.
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
    markSeen(slot);
  }, [markSeen]);

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
  }, []);

  // Split.js: horizontal split across columns, vertical split inside each
  // multi-pane column. Rebind whenever the layout structure changes.
  // A stable structure signature lets us skip reinit on no-op renders.
  const layoutSignature = useMemo(
    () => openColumns.map((c) => c.join("|")).join("//"),
    [openColumns]
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
    if (openColumns.length >= 2) {
      const selectors = openColumns.map((_, i) => "#col-" + i);
      const exist = selectors.every((sel) => document.querySelector(sel));
      if (exist) {
        const hKey = "h:" + layoutSignature;
        try {
          const h = Split(selectors, {
            sizes: resolveSizes(hKey, openColumns.length),
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
    openColumns.forEach((col, i) => {
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

  return html`
    <div class=${"app" + (envOpen ? " env-open" : "")}>
      <${LeftRail}
        agents=${agents}
        openSlots=${openSlots}
        unreadSlots=${unreadSlots}
        onOpen=${openPane}
        onStackInLast=${stackInLast}
        wsConnected=${wsConnected}
        envOpen=${envOpen}
        onToggleEnv=${() => setEnvOpen((v) => !v)}
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
      <main class="panes">
        ${openColumns.length === 0
          ? html`<div class="empty">Pick a slot on the left to open a pane.</div>`
          : html`
              ${openColumns.map(
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
                        />`;
                      }
                      // wsAttempt bumps on every WS reconnect.
                      // AgentPane re-reads history when it changes so a
                      // long disconnect (> 60s watchdog) doesn't leave
                      // events missed during the gap invisible forever.
                      const agent = agents.find((a) => a.id === slot);
                      const currentTask = agent?.current_task_id
                        ? tasks.find((t) => t.id === agent.current_task_id)
                        : null;
                      return html`<${AgentPane}
                        key=${slot}
                        slot=${slot}
                        agent=${agent}
                        currentTask=${currentTask}
                        liveEvents=${conversations.get(slot) || []}
                        streaming=${streamingText.get(slot)}
                        wsAttempt=${wsAttempt}
                        openSlots=${openSlots}
                        onClose=${() => closePane(slot)}
                        onDropEdge=${dropOnPaneEdge}
                        onPopOut=${moveToNewColumn}
                        stacked=${col.length > 1}
                      />`;
                    })}
                    <${DropZone}
                      orientation="horizontal"
                      label="drop to append"
                      onDrop=${(slot) => moveToColEnd(slot, colIdx)}
                    />
                  </div>`
              )}
              <${DropZone}
                orientation="vertical"
                label="new column"
                onDrop=${moveToNewColumn}
              />
            `}
      </main>
      ${envOpen
        ? html`<${EnvPane}
            agents=${agents}
            tasks=${tasks}
            conversations=${conversations}
            openSlots=${openSlots}
            serverStatus=${serverStatus}
            onCreateTask=${createHumanTask}
            onClose=${() => setEnvOpen(false)}
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

function LeftRail({ agents, openSlots, unreadSlots, onOpen, onStackInLast, wsConnected, envOpen, onToggleEnv, onOpenSettings, paused, onTogglePause, onLayoutPreset, onCancelAll }) {
  const workingCount = agents.filter((a) => a.status === "working").length;
  const grouped = useMemo(() => {
    const coach = agents.find((a) => a.kind === "coach");
    const players = agents
      .filter((a) => a.kind === "player")
      .sort(byNumericSuffix);
    return { coach, players };
  }, [agents]);

  const renderSlot = (a) => {
    if (!a) return null;
    const unread = unreadSlots && unreadSlots.has(a.id);
    // "active" = the agent has an in-play session (first turn has run,
    // session_id persisted). Decoupled from pane-open state — Coach
    // can be running / active while its pane is closed, and closing
    // a pane no longer greys out the rail button.
    const active = Boolean(a.session_id) || a.status === "working" || a.status === "waiting";
    const classes = [
      "slot",
      a.kind,
      a.status || "stopped",
      active ? "active" : "",
      openSlots.includes(a.id) ? "open" : "",
      unread ? "unread" : "",
    ].filter(Boolean).join(" ");
    const baseTip = a.name
      ? `${a.id} — ${a.name}${a.role ? " — " + a.role : ""} (${a.status || "stopped"})`
      : `${a.id} — unassigned (${a.status || "stopped"})`;
    const tooltip = baseTip + " — shift-click to stack in last column" + (unread ? " — NEW activity since last open" : "");
    return html`
      <button
        key=${a.id}
        class=${classes}
        title=${tooltip}
        onClick=${(e) => (e.shiftKey ? onStackInLast(a.id) : onOpen(a.id))}
      >
        ${slotShortLabel(a.id)}
        ${unread ? html`<span class="slot-unread" />` : null}
      </button>
    `;
  };

  return html`
    <aside class="rail">
      <span
        class=${"ws-dot " + (wsConnected ? "ok" : "")}
        title=${wsConnected ? "websocket connected" : "websocket disconnected"}
      ></span>
      ${renderSlot(grouped.coach)}
      ${grouped.players.map(renderSlot)}
      <span class="rail-sep"></span>
      <!-- File explorer sits right after the roster — it's conceptually
           the 12th "thing" you might open in a column, not a global
           setting, so it belongs closer to the agents than the gears. -->
      <button
        class=${"gear files-open" + (openSlots.includes("__files") ? " active" : "")}
        title="Open the file explorer pane (context, knowledge, decisions)"
        onClick=${() => onOpen("__files")}
      >
        <span class="files-icon" aria-hidden="true">
          <span class="files-icon-tab"></span>
          <span class="files-icon-body"></span>
        </span>
      </button>
      <span class="rail-sep"></span>
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
      <button
        class=${"gear env-toggle" + (envOpen ? " active" : "")}
        title=${(envOpen ? "Collapse environment panel" : "Open environment panel") + " (⌘/Ctrl+B)"}
        onClick=${onToggleEnv}
      >▦</button>
      <button class="gear" title="Settings" onClick=${onOpenSettings}>⚙</button>
    </aside>
  `;
}

// ------------------------------------------------------------------
// settings drawer
// ------------------------------------------------------------------

// Pick the most informative field from a /api/health check entry.
// Each subsystem uses different shape (db has 'error', claude_cli
// has 'version', kdrive has 'reason' or 'cached', etc.), so we walk
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
            <h3>Authentication</h3>
            <p>
              The <code>claude</code> CLI on this host uses the device-code
              OAuth flow. Tokens are <em>not</em> in <code>~/.claude.json</code>;
              they live in the CLI's own credential store. To log in:
            </p>
            <ol>
              <li>Open the Zeabur service terminal</li>
              <li>Run <code>claude</code></li>
              <li>At the <code>&gt;</code> prompt, type <code>/login</code></li>
              <li>Follow the URL it prints on your laptop browser, enter the code, approve</li>
              <li>Type <code>/exit</code> to leave the REPL</li>
            </ol>
            <p class="muted">
              Token persists on this host across the current container's lifetime.
              A Zeabur redeploy resets it — re-run the steps above.
            </p>
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

          <section class="drawer-section">
            <h3>kDrive mirror</h3>
            ${serverStatus?.kdrive?.enabled
              ? html`<p class="muted">
                  ✓ Connected. Memory docs auto-mirror to
                  <code>KDRIVE_ROOT_PATH/memory/&lt;topic&gt;.md</code> on update.
                </p>`
              : html`<p class="muted">
                  ✗ Disabled${serverStatus?.kdrive?.reason
                    ? html` (${serverStatus.kdrive.reason})`
                    : null}. Set <code>KDRIVE_WEBDAV_URL</code>,
                  <code>KDRIVE_USER</code>, and
                  <code>KDRIVE_APP_PASSWORD</code> env vars and redeploy.
                  The harness works fine without it — writes go to local
                  SQLite only.
                </p>`}
          </section>

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
// environment pane (right side): tasks + cost + timeline
// ------------------------------------------------------------------

function EnvPane({ agents, tasks, conversations, openSlots, serverStatus, onCreateTask, onClose }) {
  const [exporting, setExporting] = useState(false);

  const exportTeam = useCallback(async () => {
    if (exporting || !openSlots || openSlots.length === 0) return;
    setExporting(true);
    try {
      // Fetch each pane's events in parallel — server handles them
      // concurrently and a full-team export used to be 11× sequential.
      const results = await Promise.all(
        openSlots.map(async (slot) => {
          try {
            const res = await authFetch(
              `/api/events?agent=${encodeURIComponent(slot)}&limit=500`
            );
            if (!res.ok) return { slot, events: [] };
            const data = await res.json();
            return { slot, events: (data.events || []).map(unwrapPersisted) };
          } catch (e) {
            console.error("team export: pane fetch failed", slot, e);
            return { slot, events: [] };
          }
        })
      );
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
        <${EnvTasksSection} tasks=${tasks} onCreate=${onCreateTask} />
        <${EnvCostSection} agents=${agents} serverStatus=${serverStatus} />
        <${EnvInboxSection} conversations=${conversations} />
        <${EnvMemorySection} conversations=${conversations} />
        <${EnvDecisionsSection} conversations=${conversations} />
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

  // Load persisted escalations on mount so page reloads don't lose
  // undismissed banners. We re-fetch whenever a fresh human_attention
  // arrives over WS (the live copy lacks __id until the DB write
  // returns, so the fetch lets us get the canonical id for future
  // dismissal persistence across reloads).
  const liveCount = useMemo(() => {
    let n = 0;
    for (const list of conversations.values()) {
      for (const ev of list) if (ev.type === "human_attention") n++;
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
    })();
    return () => { cancelled = true; };
  }, [liveCount]);

  const open = useMemo(() => {
    const seen = new Set();
    const all = [];
    // Persisted first (canonical ids); live later dedupes by __id.
    for (const ev of persisted) {
      const k = ev.__id != null ? String(ev.__id) : `${ev.ts}:${ev.agent_id}`;
      if (seen.has(k)) continue;
      seen.add(k);
      all.push({ ...ev, __key: k });
    }
    for (const list of conversations.values()) {
      for (const ev of list) {
        if (ev.type !== "human_attention") continue;
        const k = ev.__id != null ? String(ev.__id) : `${ev.ts}:${ev.agent_id}`;
        if (seen.has(k)) continue;
        seen.add(k);
        all.push({ ...ev, __key: k });
      }
    }
    const out = all.filter((ev) => !dismissed.has(ev.__key));
    // Newest first — user sees the most urgent escalation at the top.
    out.sort((a, b) => (b.ts || "").localeCompare(a.ts || ""));
    return out;
  }, [conversations, persisted, dismissed]);

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
              <div class="env-attention-body">${ev.body}</div>
            </div>
          `
        )}
      </div>
    </section>
  `;
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

function FilesPane({ slot, authedFetch, fsEpoch, onClose, onDropEdge, onPopOut, stacked }) {
  const [roots, setRoots] = useState([]);
  const [activeRoot, setActiveRoot] = useState(null);
  const [tree, setTree] = useState(null);
  const [selected, setSelected] = useState(null); // {root, path}
  const [expanded, setExpanded] = useState(new Set()); // "root:relpath" strings
  const [content, setContent] = useState(null); // authoritative loaded text
  const [draft, setDraft] = useState(""); // textarea buffer
  const [meta, setMeta] = useState(null); // {size, mtime}
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");
  const [previewMd, setPreviewMd] = useState(true);

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
      if (!activeRoot && Array.isArray(data) && data.length > 0) {
        setActiveRoot(data[0].key);
      }
    } catch (e) {
      setErr("roots load failed: " + String(e));
    }
  }, [authedFetch, activeRoot]);

  const loadTree = useCallback(async (rootKey) => {
    if (!rootKey) return;
    try {
      const res = await authedFetch("/api/files/tree/" + rootKey);
      const data = await res.json();
      setTree(data);
    } catch (e) {
      setErr("tree load failed: " + String(e));
    }
  }, [authedFetch]);

  useEffect(() => { loadRoots(); }, [loadRoots]);
  useEffect(() => { if (activeRoot) loadTree(activeRoot); }, [activeRoot, loadTree]);
  // Live-refresh: when a file-system-changing event lands on the WS,
  // App bumps fsEpoch. Reload the tree so agent-written files appear
  // without a manual root-click, AND reload the currently-open file
  // if it exists (e.g. Coach edited CLAUDE.md while the user had it
  // open — previously the tree refreshed but the editor kept showing
  // the pre-edit content). Skip either reload while the user is
  // mid-edit (dirty draft) so we don't yank their typing.
  useEffect(() => {
    if (fsEpoch == null || !activeRoot) return;
    const dirty = content !== null && draft !== content;
    if (dirty) return;
    loadTree(activeRoot);
    // Also re-fetch the open file if any. No-ops if the underlying
    // file didn't change (we accept the extra HTTP round trip for
    // the simplicity of not having to correlate which path actually
    // moved — events are rare at human scale).
    if (selected) openFile(selected.root, selected.path);
  }, [fsEpoch]);

  const activeRootMeta = roots.find((r) => r.key === activeRoot);

  const openFile = useCallback(async (root, path) => {
    setLoading(true);
    setErr("");
    try {
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
        <button class="pane-close" onClick=${onClose} title="Close">×</button>
      </header>

      <div class="files-body">
        <nav class="files-tree">
          <div class="files-tree-roots">
            ${roots.map((r) => html`
              <button
                key=${r.key}
                class=${"files-root" + (r.key === activeRoot ? " active" : "")}
                onClick=${() => setActiveRoot(r.key)}
                title=${r.writable ? "editable" : "read-only"}
              >${r.key}${r.writable ? "" : " 🔒"}</button>
            `)}
          </div>
          ${tree
            ? html`<${FileTreeNode}
                node=${tree}
                root=${activeRoot}
                path=""
                depth=${0}
                expanded=${expanded}
                setExpanded=${setExpanded}
                selected=${selected}
                onPick=${openFile}
              />`
            : html`<div class="files-empty">loading…</div>`}
        </nav>

        <section class="files-editor">
          ${err ? html`<div class="files-err">${err}</div>` : null}
          ${!selected
            ? html`<div class="files-empty">Pick a file on the left.</div>`
            : loading
            ? html`<div class="files-empty">loading…</div>`
            : html`
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
                : null}
              ${isMd && previewMd
                ? html`<div
                    class="files-md-preview"
                    dangerouslySetInnerHTML=${{ __html: renderMarkdown(draft) }}
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

// Minimal markdown renderer — headings, paragraphs, code fences, lists,
// inline code/bold/italic/links. Deliberately small; if we need real
// rendering later we can drop in micromark or similar.
function renderMarkdown(md) {
  if (!md) return "";
  const esc = (s) =>
    String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  const lines = md.split(/\r?\n/);
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    // Fenced code block
    const fence = /^```(\w*)$/.exec(line);
    if (fence) {
      const lang = fence[1];
      const buf = [];
      i++;
      while (i < lines.length && !/^```$/.test(lines[i])) {
        buf.push(lines[i]);
        i++;
      }
      i++; // skip closing fence
      out.push(
        `<pre class="md-code"><code${lang ? ` data-lang="${esc(lang)}"` : ""}>${esc(buf.join("\n"))}</code></pre>`
      );
      continue;
    }
    // Heading
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const lvl = h[1].length;
      out.push(`<h${lvl} class="md-h">${renderInline(esc(h[2]))}</h${lvl}>`);
      i++;
      continue;
    }
    // Unordered list block
    if (/^[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^[-*]\s+/, ""));
        i++;
      }
      out.push(
        "<ul class=\"md-ul\">" +
          items.map((t) => "<li>" + renderInline(esc(t)) + "</li>").join("") +
          "</ul>"
      );
      continue;
    }
    // Ordered list block
    if (/^\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s+/, ""));
        i++;
      }
      out.push(
        "<ol class=\"md-ol\">" +
          items.map((t) => "<li>" + renderInline(esc(t)) + "</li>").join("") +
          "</ol>"
      );
      continue;
    }
    // Blank line → paragraph break
    if (!line.trim()) { i++; continue; }
    // Paragraph (collect until blank)
    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|[-*]\s|\d+\.\s|```)/.test(lines[i])) {
      buf.push(lines[i]);
      i++;
    }
    out.push("<p class=\"md-p\">" + renderInline(esc(buf.join(" "))) + "</p>");
  }
  return out.join("\n");
}

function renderInline(s) {
  return s
    // links [text](url)
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>')
    // inline code
    .replace(/`([^`]+)`/g, '<code class="md-ic">$1</code>')
    // bold
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    // italic (avoid already-escaped patterns)
    .replace(/(^|\W)\*([^*]+)\*(\W|$)/g, "$1<em>$2</em>$3");
}

// ------------------------------------------------------------------
// agent pane
// ------------------------------------------------------------------

function AgentPane({ slot, agent, currentTask, liveEvents, streaming, wsAttempt, onClose, onDropEdge, onPopOut, stacked }) {
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState([]); // {id, url, path, filename}
  const [submitting, setSubmitting] = useState(false);
  const [history, setHistory] = useState([]);
  const [historyLoaded, setHistoryLoaded] = useState(false);
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

  // Load persisted history once per slot. We guard so switching panes
  // doesn't refetch constantly.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await authFetch(
          `/api/events?agent=${encodeURIComponent(slot)}&limit=500`
        );
        if (!res.ok) {
          if (!cancelled) setHistoryLoaded(true);
          return;
        }
        const data = await res.json();
        if (cancelled) return;
        setHistory((data.events || []).map(unwrapPersisted));
      } catch (e) {
        console.error("history load failed", e);
      } finally {
        if (!cancelled) setHistoryLoaded(true);
      }
    }
    load();
    return () => {
      cancelled = true;
    };
    // Re-read history whenever wsAttempt changes (every WS reconnect).
    // Long disconnects > 60s watchdog trigger this path, so events that
    // landed during the gap show up in the pane via the DB re-fetch
    // instead of being silently missed.
  }, [slot, wsAttempt]);

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

  // Last-turn stats: find the most recent 'result' event (the SDK
  // emits one per turn with duration + cost). Surfaces in the pane
  // header as a compact "last: Ns $X.XX" chip.
  const lastResult = useMemo(() => {
    for (let i = mergedEvents.length - 1; i >= 0; i--) {
      if (mergedEvents[i].type === "result") return mergedEvents[i];
    }
    return null;
  }, [mergedEvents]);

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
        // HARNESS_MCP_CONFIG — those tools vary per deploy so they
        // can't live in a hardcoded list.
        authFetch("/api/health")
          .then((r) => r.json())
          .then((d) => {
            const ext = d?.checks?.mcp_external;
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
              mcp_lines.join("\n")
            );
          })
          .catch(() => {
            // Fall back to just the hardcoded list if /api/health
            // errors — better than nothing.
            setInfoText("Tools for " + slot + ":\n" + list.map((l) => "• " + l).join("\n"));
          });
        return true;
      }
      case "/loop": {
        // Toggle / set Coach's autoloop interval at runtime.
        //   /loop            → report current state
        //   /loop 60         → tick every 60 seconds
        //   /loop 0 | off    → disable
        let target = null;
        const a = arg.trim().toLowerCase();
        if (!a) {
          authFetch("/api/coach/loop")
            .then((r) => r.json())
            .then((d) => {
              setInfoText(
                d.interval_seconds
                  ? `Coach autoloop: every ${d.interval_seconds}s. '/loop off' to stop.`
                  : "Coach autoloop: OFF. '/loop 60' to start a 60s tick."
              );
            })
            .catch((e) => setInfoText("loop query failed: " + String(e)));
          return true;
        }
        if (a === "off" || a === "0" || a === "stop") target = 0;
        else target = parseInt(a, 10);
        if (target == null || isNaN(target) || target < 0) {
          setInfoText("usage: /loop [seconds | off]");
          return true;
        }
        authFetch("/api/coach/loop", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ interval_seconds: target }),
        })
          .then(() => {
            setInfoText(
              target === 0
                ? "Coach autoloop stopped."
                : `Coach autoloop: every ${target}s. First tick in ~${target}s.`
            );
          })
          .catch((e) => setInfoText("loop set failed: " + String(e)));
        return true;
      }
      case "/tick":
        // Fire a Coach tick right now without waiting for the autoloop
        // (which may be off or on a long interval). 409 means Coach is
        // already working; the server will just keep doing what it's
        // doing.
        authFetch("/api/coach/tick", { method: "POST" })
          .then((r) => {
            if (r.ok) setInfoText("Coach ticked. Watch their pane.");
            else if (r.status === 409) setInfoText("Coach is already working.");
            else setInfoText("tick failed: HTTP " + r.status);
          })
          .catch((e) => setInfoText("tick failed: " + String(e)));
        return true;
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
              `kdrive: ${d.kdrive?.enabled ? "on" : "off — " + (d.kdrive?.reason || "?")}`,
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
      if (paneSettings.model) reqBody.model = paneSettings.model;
      if (paneSettings.planMode) reqBody.plan_mode = true;
      if (paneSettings.effort) reqBody.effort = paneSettings.effort;
      const res = await authFetch("/api/agents/start", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(reqBody),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const nextHistory = pushPromptHistory(slot, text);
      setPromptHistory(nextHistory);
      setPromptHistoryIdx(null);
      setInput("");
      setAttachments([]);
    } catch (err) {
      console.error("submit failed", err);
    } finally {
      setSubmitting(false);
    }
  }, [input, attachments, slot, paneSettings]);

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
  const cost = Number(agent?.cost_estimate_usd || 0);

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
          <span class="pane-id">${slot}</span>
          ${displayName ? html`<span class="pane-name">${displayName}</span>` : null}
          ${agent?.role ? html`<span class="pane-role">— ${agent.role}</span>` : html`<span class="pane-role"></span>`}
        </span>
        ${currentTask
          ? html`<span
              class="pane-current-task"
              title=${"current task: " + currentTask.title + " (" + currentTask.id + ", " + currentTask.status + ")"}
            >⚑ ${currentTask.title.slice(0, 24)}${currentTask.title.length > 24 ? "…" : ""}</span>`
          : null}
        ${agent?.session_id
          ? html`<span class="pane-session" title=${"session " + agent.session_id}>●</span>
              <button
                class="pane-session-clear"
                onClick=${async () => {
                  await authFetch("/api/agents/" + slot + "/session", { method: "DELETE" });
                }}
                title="Clear session — next run starts fresh"
              >×</button>`
          : null}
        <span class="pane-cost">$${cost.toFixed(3)}</span>
        ${lastResult && status !== "working"
          ? html`<span
              class="pane-last-turn"
              title=${"Last turn: " + (lastResult.duration_ms ? Math.round(lastResult.duration_ms) + "ms" : "?") + ", $" + (lastResult.cost_usd || 0).toFixed(4) + (lastResult.is_error ? " (errored)" : "")}
            >${(lastResult.duration_ms ? (lastResult.duration_ms / 1000).toFixed(1) + "s" : "?")} · $${(lastResult.cost_usd || 0).toFixed(3)}</span>`
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
        <button class="pane-close" onClick=${onClose} title="Close pane">×</button>
        ${settingsOpen
          ? html`<${PaneSettingsPopover}
              settings=${paneSettings}
              onChange=${setPaneSettings}
              slot=${slot}
              initialBrief=${agent?.brief || ""}
              initialName=${agent?.name || ""}
              initialRole=${agent?.role || ""}
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
        ${visibleEvents.map((ev, i) => html`<${EventItem} key=${(ev.__id ?? "live-" + i)} event=${ev} />`)}
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
            ${(MODEL_OPTIONS.find((m) => m.value === (paneSettings.model || "")) || MODEL_OPTIONS[0]).label}
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

function TurnHeader({ event, ts }) {
  const [open, setOpen] = useState(false);
  const prompt = event.prompt || "";
  const oneLiner = prompt.replace(/\s+/g, " ").trim();
  const arrow = event.resumed_session ? "↻" : "→";
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
  const content = event.content || "";
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
      ? html`<div class="event-body thinking-body">${content}</div>`
      : null}
  </div>`;
}

// Event types that are pure audit noise in the pane body — they're
// useful in the DB / EnvPane timeline for debugging ("did my context
// get picked up?") but shouldn't clutter the conversation view.
const _HIDDEN_EVENT_TYPES = new Set([
  "context_applied",
]);

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
      <div class="event-body">${event.content || ""}</div>
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
    const suffix = event.is_error ? "  (error)" : "";
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
    const msg = type === "session_cleared"
      ? "session cleared"
      : `session resume failed · ${event.error || ""}`;
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ${msg}</div>
    </div>`;
  }

  if (type === "commit_pushed") {
    const push = event.pushed ? "↑" : event.push_requested ? "✗push" : "local";
    return html`<div class="event sys">
      <div class="event-meta">${ts} · ${event.sha} ${push} — ${event.message}</div>
    </div>`;
  }

  if (type === "coach_loop_changed") {
    const s = event.interval_seconds;
    return html`<div class="event sys">
      <div class="event-meta">${ts} · coach autoloop ${s > 0 ? `every ${s}s` : "OFF"}</div>
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

  // fallback
  return html`<div class="event">
    <div class="event-meta">${ts}  ${type}</div>
    <div class="event-body">${JSON.stringify(event).slice(0, 300)}</div>
  </div>`;
}

// ------------------------------------------------------------------
// boot
// ------------------------------------------------------------------

render(html`<${App} />`, document.getElementById("app"));

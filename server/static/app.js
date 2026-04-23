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

function loadLayout() {
  try {
    const raw = localStorage.getItem(LAYOUT_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw);
    // Defensive validation — corrupted payload falls back to defaults.
    if (!v || typeof v !== "object") return null;
    if (!Array.isArray(v.openSlots)) return null;
    return {
      openSlots: v.openSlots.filter((s) => typeof s === "string").slice(0, 11),
      envOpen: typeof v.envOpen === "boolean" ? v.envOpen : true,
    };
  } catch (_) {
    return null;
  }
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
  const [openSlots, setOpenSlots] = useState(
    () => loadLayout()?.openSlots ?? ["coach"]
  );
  const [wsConnected, setWsConnected] = useState(false);
  const [envOpen, setEnvOpen] = useState(
    () => loadLayout()?.envOpen ?? true
  );
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [serverStatus, setServerStatus] = useState(null);
  const [authChallenge, setAuthChallenge] = useState(false);
  // conversations: Map<slotId, Event[]>  (events ordered oldest → newest)
  const [conversations, setConversations] = useState(new Map());
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

  useEffect(() => {
    loadAgents();
    loadTasks();
    loadStatus();
    const statusTimer = setInterval(loadStatus, 30_000);
    return () => clearInterval(statusTimer);
  }, [loadAgents, loadTasks, loadStatus]);

  // Persist layout (open slots + env panel state) on every change.
  useEffect(() => {
    saveLayout({ openSlots, envOpen });
  }, [openSlots, envOpen]);

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
    ws.onopen = () => {
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
      let ev;
      try { ev = JSON.parse(e.data); } catch (_) { return; }
      const aid = ev.agent_id || "system";
      setConversations((prev) => {
        const next = new Map(prev);
        const list = next.get(aid) || [];
        next.set(aid, [...list, ev]);
        return next;
      });
      if (
        ev.type === "agent_started" ||
        ev.type === "agent_stopped" ||
        ev.type === "result" ||
        ev.type === "error" ||
        ev.type === "cost_capped" ||
        ev.type === "session_cleared"
      ) {
        loadAgents();
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
    };
    return () => {
      if (reopenTimer) clearTimeout(reopenTimer);
      try { ws.close(); } catch (_) { /* ignore */ }
    };
  }, [loadAgents, loadTasks, loadStatus, wsAttempt]);

  const openPane = useCallback((slot) => {
    setOpenSlots((prev) => (prev.includes(slot) ? prev : [...prev, slot]));
  }, []);
  const closePane = useCallback((slot) => {
    setOpenSlots((prev) => prev.filter((s) => s !== slot));
  }, []);

  // Split.js manages drag-resize gutters between agent panes. We tear
  // down + recreate whenever the set of open slots changes, because
  // Split.js binds to specific DOM elements and can't handle React-y
  // dynamic children on its own.
  useLayoutEffect(() => {
    if (openSlots.length < 2) return;
    const selectors = openSlots.map((s) => "#pane-" + s);
    // Ensure all target elements exist before Split initializes
    const exist = selectors.every((sel) => document.querySelector(sel));
    if (!exist) return;
    let split;
    try {
      split = Split(selectors, {
        sizes: Array(openSlots.length).fill(100 / openSlots.length),
        minSize: 260,
        gutterSize: 6,
        snapOffset: 0,
        dragInterval: 1,
      });
    } catch (e) {
      console.error("Split init failed", e);
      return;
    }
    return () => {
      try { split.destroy(); } catch (_) { /* ignore */ }
    };
  }, [openSlots]);

  return html`
    <div class=${"app" + (envOpen ? " env-open" : "")}>
      <${LeftRail}
        agents=${agents}
        openSlots=${openSlots}
        onOpen=${openPane}
        wsConnected=${wsConnected}
        envOpen=${envOpen}
        onToggleEnv=${() => setEnvOpen((v) => !v)}
        onOpenSettings=${() => setSettingsOpen(true)}
      />
      <main class="panes">
        ${openSlots.length === 0
          ? html`<div class="empty">Pick a slot on the left to open a pane.</div>`
          : openSlots.map(
              (slot) =>
                html`<${AgentPane}
                  key=${slot}
                  slot=${slot}
                  agent=${agents.find((a) => a.id === slot)}
                  liveEvents=${conversations.get(slot) || []}
                  onClose=${() => closePane(slot)}
                />`
            )}
      </main>
      ${envOpen
        ? html`<${EnvPane}
            agents=${agents}
            tasks=${tasks}
            conversations=${conversations}
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

function LeftRail({ agents, openSlots, onOpen, wsConnected, envOpen, onToggleEnv, onOpenSettings }) {
  const grouped = useMemo(() => {
    const coach = agents.find((a) => a.kind === "coach");
    const players = agents
      .filter((a) => a.kind === "player")
      .sort(byNumericSuffix);
    return { coach, players };
  }, [agents]);

  const renderSlot = (a) => {
    if (!a) return null;
    const classes = [
      "slot",
      a.kind,
      a.status || "stopped",
      openSlots.includes(a.id) ? "open" : "",
    ].filter(Boolean).join(" ");
    const tooltip = a.name
      ? `${a.id} — ${a.name}${a.role ? " — " + a.role : ""} (${a.status || "stopped"})`
      : `${a.id} — unassigned (${a.status || "stopped"})`;
    return html`
      <button
        key=${a.id}
        class=${classes}
        title=${tooltip}
        onClick=${() => onOpen(a.id)}
      >
        ${slotShortLabel(a.id)}
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
      <button
        class=${"gear env-toggle" + (envOpen ? " active" : "")}
        title=${envOpen ? "Collapse environment panel" : "Open environment panel"}
        onClick=${onToggleEnv}
      >▦</button>
      <button class="gear" title="Settings" onClick=${onOpenSettings}>⚙</button>
    </aside>
  `;
}

// ------------------------------------------------------------------
// settings drawer
// ------------------------------------------------------------------

function SettingsDrawer({ onClose, serverStatus }) {
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
            <h3>About</h3>
            <p>
              <strong>TeamOfTen harness</strong><br />
              Milestone M2a + v2d<br />
              1 Coach + 10 Players orchestrated via Claude Agent SDK<br />
              <a
                href="https://github.com/Nicolasmoute/TeamOfTen"
                target="_blank"
                rel="noopener noreferrer"
                >github.com/Nicolasmoute/TeamOfTen</a
              >
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

function EnvPane({ agents, tasks, conversations, serverStatus, onCreateTask, onClose }) {
  return html`
    <aside class="env-pane">
      <header class="env-head">
        <span class="env-title">Environment</span>
        <button class="env-close" onClick=${onClose} title="Collapse">×</button>
      </header>
      <div class="env-body">
        <${EnvTasksSection} tasks=${tasks} onCreate=${onCreateTask} />
        <${EnvCostSection} agents=${agents} serverStatus=${serverStatus} />
        <${EnvTimelineSection} conversations=${conversations} />
      </div>
    </aside>
  `;
}

function EnvTasksSection({ tasks, onCreate }) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("normal");
  const [submitting, setSubmitting] = useState(false);

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
  const sorted = [...tasks].sort(
    (a, b) => (statusOrder[a.status] ?? 9) - (statusOrder[b.status] ?? 9)
  );

  return html`
    <section class="env-section">
      <h3 class="env-section-title">Tasks <span class="env-count">${tasks.length}</span></h3>
      <div class="env-task-list">
        ${sorted.length === 0
          ? html`<div class="env-empty">(no tasks yet)</div>`
          : sorted.map(
              (t) => html`
                <div class=${"env-task status-" + t.status} key=${t.id}>
                  <div class="env-task-head">
                    <span class="env-task-status">${t.status}</span>
                    <span class="env-task-id">${t.id}</span>
                  </div>
                  <div class="env-task-title">${t.title}</div>
                  <div class="env-task-meta">
                    by ${t.created_by} · owner ${t.owner || "-"} · pri ${t.priority}${t.parent_id ? " · ↳" + t.parent_id : ""}
                  </div>
                </div>
              `
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
    return html`<div class="env-tl-item env-tl-started">
      <span class="env-tl-ts">${ts}</span>
      <span class="env-tl-who">${who}</span>
      <span class="env-tl-arrow">→</span>
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
// agent pane
// ------------------------------------------------------------------

function AgentPane({ slot, agent, liveEvents, onClose }) {
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState([]); // {id, url, path, filename}
  const [submitting, setSubmitting] = useState(false);
  const [history, setHistory] = useState([]);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const bodyRef = useRef(null);
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
  }, [slot]);

  // Merge history + live events.
  const mergedEvents = useMemo(() => {
    const seen = new Set();
    const out = [];
    for (const e of history) {
      if (e.__id != null) seen.add(e.__id);
      out.push(e);
    }
    for (const e of liveEvents) {
      if (e.__id != null && seen.has(e.__id)) continue;
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

  // auto-scroll to bottom when new events arrive — only if user was already
  // near the bottom (otherwise leave them reading older history in peace).
  useEffect(() => {
    if (bodyRef.current && stickToBottomRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [allEvents.length]);

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

  const submit = useCallback(async () => {
    const text = input.trim();
    if (!text && attachments.length === 0) return;
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
      const res = await authFetch("/api/agents/start", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ agent_id: slot, prompt }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setInput("");
      setAttachments([]);
    } catch (err) {
      console.error("submit failed", err);
    } finally {
      setSubmitting(false);
    }
  }, [input, attachments, slot]);

  const onKeyDown = useCallback(
    (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        submit();
      }
    },
    [submit]
  );

  const displayName = agent?.name || (agent?.kind === "player" ? "unassigned" : slot);
  const status = agent?.status || "stopped";
  const cost = Number(agent?.cost_estimate_usd || 0);

  return html`
    <section class="pane" id=${"pane-" + slot}>
      <header class="pane-head">
        <span class=${"pane-dot " + status} title=${status}></span>
        <span class="pane-id">${slot}</span>
        <span class="pane-name">${displayName}</span>
        ${agent?.role ? html`<span class="pane-role">— ${agent.role}</span>` : html`<span class="pane-role"></span>`}
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
        <button class="pane-close" onClick=${onClose} title="Close pane">×</button>
      </header>
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
        ${allEvents.map((ev, i) => html`<${EventItem} key=${(ev.__id ?? "live-" + i)} event=${ev} />`)}
      </div>
      <footer class="pane-input">
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
        <textarea
          placeholder=${"Message " + displayName + "… (paste images directly)"}
          value=${input}
          onInput=${(e) => setInput(e.target.value)}
          onPaste=${onPaste}
          onKeyDown=${onKeyDown}
          rows=${3}
        ></textarea>
        <div class="pane-input-row">
          <span class="hint">⌘/Ctrl+Enter to send</span>
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

function EventItem({ event }) {
  const type = event.type;
  const ts = timeStr(event.ts);

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

  if (type === "error") {
    return html`<div class="event error">
      <div class="event-meta">${ts} error</div>
      <div class="event-body">${event.error || ""}</div>
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
    return html`<div class="event agent_started">
      <div class="event-meta">${ts}  agent_started</div>
      ${event.prompt ? html`<div class="prompt">${event.prompt}</div>` : null}
    </div>`;
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

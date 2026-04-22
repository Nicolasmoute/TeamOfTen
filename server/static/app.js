import { h, render } from "https://esm.sh/preact@10";
import { useState, useEffect, useMemo, useRef, useCallback } from "https://esm.sh/preact@10/hooks";
import htm from "https://esm.sh/htm@3";
import { renderToolCall } from "/static/tools.js";

const html = htm.bind(h);

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
  const [openSlots, setOpenSlots] = useState(["coach"]);
  const [wsConnected, setWsConnected] = useState(false);
  const [envOpen, setEnvOpen] = useState(true);
  // conversations: Map<slotId, Event[]>  (events ordered oldest → newest)
  const [conversations, setConversations] = useState(new Map());
  // bumping this re-runs the WS effect, which re-opens a new connection
  const [wsAttempt, setWsAttempt] = useState(0);

  // load + refresh agents
  const loadAgents = useCallback(async () => {
    try {
      const res = await fetch("/api/agents");
      const data = await res.json();
      setAgents(data.agents || []);
    } catch (e) {
      console.error("loadAgents failed", e);
    }
  }, []);

  const loadTasks = useCallback(async () => {
    try {
      const res = await fetch("/api/tasks");
      const data = await res.json();
      setTasks(data.tasks || []);
    } catch (e) {
      console.error("loadTasks failed", e);
    }
  }, []);

  const createHumanTask = useCallback(
    async ({ title, description, priority }) => {
      const res = await fetch("/api/tasks", {
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
    [loadTasks]
  );

  useEffect(() => {
    loadAgents();
    loadTasks();
  }, [loadAgents, loadTasks]);

  // WebSocket: single connection at app root. On close, schedule a
  // re-open by bumping wsAttempt; the effect re-runs, a new socket opens.
  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws`);
    let reopenTimer = null;
    ws.onopen = () => setWsConnected(true);
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
        ev.type === "error"
      ) {
        loadAgents();
      }
      if (ev.type === "task_created" || ev.type === "task_updated") {
        loadTasks();
        loadAgents();
      }
    };
    return () => {
      if (reopenTimer) clearTimeout(reopenTimer);
      try { ws.close(); } catch (_) { /* ignore */ }
    };
  }, [loadAgents, wsAttempt]);

  const openPane = useCallback((slot) => {
    setOpenSlots((prev) => (prev.includes(slot) ? prev : [...prev, slot]));
  }, []);
  const closePane = useCallback((slot) => {
    setOpenSlots((prev) => prev.filter((s) => s !== slot));
  }, []);

  return html`
    <div class=${"app" + (envOpen ? " env-open" : "")}>
      <${LeftRail}
        agents=${agents}
        openSlots=${openSlots}
        onOpen=${openPane}
        wsConnected=${wsConnected}
        envOpen=${envOpen}
        onToggleEnv=${() => setEnvOpen((v) => !v)}
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
            onCreateTask=${createHumanTask}
            onClose=${() => setEnvOpen(false)}
          />`
        : null}
    </div>
  `;
}

// ------------------------------------------------------------------
// left rail
// ------------------------------------------------------------------

function LeftRail({ agents, openSlots, onOpen, wsConnected, envOpen, onToggleEnv }) {
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
      <button class="gear" title="Settings (not wired yet)">⚙</button>
    </aside>
  `;
}

// ------------------------------------------------------------------
// environment pane (right side): tasks + cost + timeline
// ------------------------------------------------------------------

function EnvPane({ agents, tasks, onCreateTask, onClose }) {
  return html`
    <aside class="env-pane">
      <header class="env-head">
        <span class="env-title">Environment</span>
        <button class="env-close" onClick=${onClose} title="Collapse">×</button>
      </header>
      <div class="env-body">
        <${EnvTasksSection} tasks=${tasks} onCreate=${onCreateTask} />
        <${EnvCostSection} agents=${agents} />
        <${EnvTimelineSection} />
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

function EnvCostSection({ agents }) {
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
  return html`
    <section class="env-section">
      <h3 class="env-section-title">
        Cost <span class="env-count">$${total.toFixed(3)}</span>
      </h3>
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

function EnvTimelineSection() {
  // Placeholder — cross-agent timeline lands in v2d step 3.
  return html`
    <section class="env-section">
      <h3 class="env-section-title">Timeline</h3>
      <div class="env-cost-hint">(cross-agent stream in next step)</div>
    </section>
  `;
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
        const res = await fetch(
          `/api/events?agent=${encodeURIComponent(slot)}&limit=500`
        );
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
        const res = await fetch("/api/attachments", {
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
      const res = await fetch("/api/agents/start", {
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
    <section class="pane">
      <header class="pane-head">
        <span class=${"pane-dot " + status} title=${status}></span>
        <span class="pane-id">${slot}</span>
        <span class="pane-name">${displayName}</span>
        ${agent?.role ? html`<span class="pane-role">— ${agent.role}</span>` : html`<span class="pane-role"></span>`}
        <span class="pane-cost">$${cost.toFixed(3)}</span>
        <button class="pane-close" onClick=${onClose} title="Close pane">×</button>
      </header>
      <div class="pane-body" ref=${bodyRef} onScroll=${onBodyScroll}>
        ${!historyLoaded ? html`<div class="loading">loading history…</div>` : null}
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

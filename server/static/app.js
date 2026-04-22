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
  const [openSlots, setOpenSlots] = useState(["coach"]);
  const [wsConnected, setWsConnected] = useState(false);
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
  useEffect(() => {
    loadAgents();
  }, [loadAgents]);

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
        ev.type === "error" ||
        ev.type === "task_created"
      ) {
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
    <div class="app">
      <${LeftRail}
        agents=${agents}
        openSlots=${openSlots}
        onOpen=${openPane}
        wsConnected=${wsConnected}
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
    </div>
  `;
}

// ------------------------------------------------------------------
// left rail
// ------------------------------------------------------------------

function LeftRail({ agents, openSlots, onOpen, wsConnected }) {
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
      <button class="gear" title="Settings (not wired yet)">⚙</button>
    </aside>
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

  // Merge history + live events. History events have __id (DB row id);
  // liveEvents may include events that arrived after history loaded OR
  // before (since the WS backlog replays recent events too). We dedupe
  // by __id where available.
  const allEvents = useMemo(() => {
    const seen = new Set();
    const out = [];
    for (const e of history) {
      if (e.__id != null) seen.add(e.__id);
      out.push(e);
    }
    // For live events, skip any that duplicate a history row via __id
    // (won't happen since live events don't carry __id, but cheap).
    // Also skip consecutive identical events (WS backlog + own echo).
    for (const e of liveEvents) {
      if (e.__id != null && seen.has(e.__id)) continue;
      out.push(e);
    }
    return out;
  }, [history, liveEvents]);

  // auto-scroll to bottom when new events arrive
  useEffect(() => {
    if (bodyRef.current) {
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
      // Compose prompt string: include image paths so the agent can Read them.
      // The agent's cwd is /workspaces/<slot>, but /data/attachments/* is
      // an absolute path readable from there.
      let prompt = text;
      if (attachments.length > 0) {
        const paths = attachments.map((a) => a.path).join("\n  - ");
        const header = text ? "\n\nAttached images (use Read to load):\n  - " : "Attached images (use Read to load):\n  - ";
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
      <div class="pane-body" ref=${bodyRef}>
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

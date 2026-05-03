// Kanban dashboard (Docs/kanban-specs.md section 11).
//
// Single-file Preact pane mounted under slot `__kanban`. The active board
// renders Plan / Execute / Audit / Ship, with audit split into syntax and
// semantics bands. Archived tasks live in a separate drawer.

import { h } from "https://esm.sh/preact@10";
import { useCallback, useEffect, useMemo, useState } from "https://esm.sh/preact@10/hooks";
import htm from "/static/vendor/htm.js";

const html = htm.bind(h);


// ---------------------------------------------------------------- API


async function apiGet(authedFetch, path) {
  const r = await authedFetch(`/api/tasks${path}`);
  if (!r.ok) throw new Error(`GET /api/tasks${path} -> ${r.status}`);
  return r.json();
}

async function apiPost(authedFetch, path, body) {
  const r = await authedFetch(`/api/tasks${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail; } catch (_) {}
    throw new Error(detail || `POST /api/tasks${path} -> ${r.status}`);
  }
  return r.json();
}


// ---------------------------------------------------------------- helpers


const STAGE_LABELS = {
  plan: "PLAN",
  execute: "EXECUTE",
  audit_syntax: "AUDIT-SYN",
  audit_semantics: "AUDIT-SEM",
  ship: "SHIP",
  archive: "ARCHIVE",
};

const PRIORITY_TONE = {
  urgent: "var(--err)",
  high: "var(--warn)",
  normal: "var(--accent)",
  low: "var(--muted)",
};

const ROLE_LABELS = {
  planner: "planner",
  executor: "executor",
  auditor_syntax: "syntax auditor",
  auditor_semantics: "semantics auditor",
  shipper: "shipper",
};

const ARCHIVE_STORAGE = "tot-kanban-archive";


function timeAgo(iso) {
  if (!iso) return "";
  const ms = Date.now() - Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const hr = Math.floor(m / 60);
  if (hr < 48) return `${hr}h ago`;
  const d = Math.floor(hr / 24);
  return `${d}d ago`;
}


function savedArchiveState() {
  try {
    return JSON.parse(localStorage.getItem(ARCHIVE_STORAGE) || "{}") || {};
  } catch (_) {
    return {};
  }
}


function saveArchiveState(next) {
  try {
    localStorage.setItem(ARCHIVE_STORAGE, JSON.stringify(next || {}));
  } catch (_) {}
}


function roleForStage(status) {
  if (status === "plan") return "planner";
  if (status === "execute") return "executor";
  if (status === "audit_syntax") return "auditor_syntax";
  if (status === "audit_semantics") return "auditor_semantics";
  if (status === "ship") return "shipper";
  return null;
}


function isActiveAssignment(a) {
  return a && !a.completed_at && !a.superseded_by;
}


function assignmentForRole(task, role) {
  const assignments = task.assignments || [];
  return assignments.find((a) => a.role === role && isActiveAssignment(a)) || null;
}


function assignmentOwners(assignment) {
  if (!assignment) return [];
  if (assignment.owner) return [assignment.owner];
  try {
    const parsed = JSON.parse(assignment.eligible_owners || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch (_) {
    return [];
  }
}


function primaryAssignee(task) {
  const role = roleForStage(task.status);
  if (!role) return { role: null, fallbackOwner: task.owner || null };

  const active = assignmentForRole(task, role);
  if (active) return { role, assignment: active };

  if (task.status === "plan") {
    return { role, coach: true };
  }
  if (task.status === "execute" && task.owner) {
    return { role, fallbackOwner: task.owner };
  }
  return { role, missingRole: role };
}


function archiveAssignee(task) {
  const assignments = task.assignments || [];
  const candidates = ["shipper", "executor", "auditor_semantics", "auditor_syntax", "planner"];
  for (const role of candidates) {
    const found = [...assignments].reverse().find((a) => a.role === role && a.owner);
    if (found) return { role, assignment: found };
  }
  return { role: "executor", fallbackOwner: task.owner || null };
}


function statusFlag(task) {
  if (task.blocked) return { label: "BLOCKED", tone: "var(--err)" };
  if (task.priority === "urgent") return { label: "URGENT", tone: "var(--err)" };
  return null;
}


function dateOnly(iso) {
  return (iso || "").slice(0, 10);
}


// ---------------------------------------------------------------- small UI


function Icon({ name }) {
  return html`<span class=${`kbn-icon kbn-${name}-icon`} aria-hidden="true"></span>`;
}


function IconButton({ title, name, onClick, disabled, className = "" }) {
  return html`
    <button
      type="button"
      class=${`kbn-icon-btn ${className}`}
      title=${title}
      aria-label=${title}
      disabled=${disabled}
      onClick=${onClick}
    >
      <${Icon} name=${name} />
    </button>
  `;
}


function MdLink({ path, label }) {
  if (!path) return null;
  const href = `/data/${path}`;
  return html`
    <a class="kbn-link" data-harness-path="${href}" href="#">${label}</a>
  `;
}


function Avatar({ primary, onAssign }) {
  if (primary?.coach) {
    return html`<span class="kbn-coach-chip">Coach</span>`;
  }
  if (primary?.missingRole) {
    return html`
      <button
        type="button"
        class="kbn-unassigned"
        title=${`Assign ${ROLE_LABELS[primary.missingRole] || primary.missingRole}`}
        onClick=${onAssign}
      >
        unassigned
      </button>
    `;
  }

  const assignment = primary?.assignment;
  if (assignment && !assignment.owner) {
    const owners = assignmentOwners(assignment);
    return html`
      <span class="kbn-pool-chip" title=${`Posted to ${owners.length} eligible Players`}>
        pool: ${owners.length}
      </span>
    `;
  }

  const owner = assignment?.owner || primary?.fallbackOwner;
  if (!owner) return html`<span class="kbn-pool-chip">unowned</span>`;

  const ring = assignment?.started_at || assignment?.completed_at
    ? "kbn-avatar-filled"
    : "kbn-avatar-hollow";
  return html`
    <span class=${`kbn-avatar ${ring}`} title=${primary?.role || assignment?.role || "owner"}>
      ${owner}
    </span>
  `;
}


function AssignmentHistory({ history }) {
  const rows = history || [];
  if (!rows.length) {
    return html`<div class="kbn-empty">No role history yet</div>`;
  }
  return html`
    <div class="kbn-history">
      ${rows.map((a) => {
        const owners = assignmentOwners(a);
        const ownerLabel = a.owner || (owners.length ? `pool: ${owners.join(", ")}` : "unassigned");
        const state = a.verdict || (a.completed_at ? "done" : "active");
        return html`
          <div class="kbn-history-row" key=${a.id}>
            <span class="kbn-history-role">${ROLE_LABELS[a.role] || a.role}</span>
            <span>${ownerLabel}</span>
            <span class=${`kbn-history-state kbn-history-${state}`}>${state}</span>
            ${a.completed_at
              ? html`<span>${timeAgo(a.completed_at)}</span>`
              : html`<span>${timeAgo(a.assigned_at)}</span>`}
            ${a.superseded_by
              ? html`<span class="kbn-history-muted">superseded</span>`
              : null}
            <${MdLink} path=${a.report_path} label="report" />
          </div>
        `;
      })}
    </div>
  `;
}


// ---------------------------------------------------------------- card


function Card({
  task, expanded, history, historyBusy, onExpand, onAssignRole,
}) {
  const flag = statusFlag(task);
  const primary = primaryAssignee(task);
  const stageLabel = STAGE_LABELS[task.status] || task.status;
  const driftBanner =
    task.status === "execute"
    && (task.latest_audit_verdict || "").toLowerCase() === "fail";
  const compassPip = (task.compass_audit_verdict || "").toLowerCase();
  let compassTone = null;
  if (compassPip === "aligned") compassTone = "var(--ok)";
  else if (compassPip === "uncertain_drift") compassTone = "var(--warn)";
  else if (compassPip === "confident_drift") compassTone = "var(--err)";

  const priClass = `kbn-pri-${task.priority || "normal"}`;
  const doAssign = (ev) => {
    ev.stopPropagation();
    if (primary.missingRole && onAssignRole) {
      onAssignRole(task, primary.missingRole);
    }
  };

  return html`
    <article
      class=${`kbn-card ${priClass} ${expanded ? "expanded" : ""}`}
      onClick=${() => onExpand && onExpand(task.id)}
      tabindex="0"
    >
      <div class="kbn-card-top">
        <span
          class="kbn-pri-pill"
          title=${`priority: ${task.priority || "normal"}`}
          aria-label=${`priority: ${task.priority || "normal"}`}
          style=${`background:${PRIORITY_TONE[task.priority] || "var(--muted)"}`}
        ></span>
        ${task.complexity === "simple"
          ? html`<span class="kbn-simple-chip">SIMPLE</span>`
          : null}
      </div>
      <div class="kbn-card-title">${task.title || "(untitled)"}</div>
      <div class="kbn-stage-label">${stageLabel}</div>
      <div class="kbn-card-row">
        <${Avatar} primary=${primary} onAssign=${doAssign} />
        ${flag
          ? html`<span class="kbn-flag" style=${`color:${flag.tone}`}>${flag.label}</span>`
          : null}
        ${task.compass_audit_verdict && (task.status === "audit_syntax" || task.status === "audit_semantics")
          ? html`<span
              class="kbn-compass-pip"
              style=${`background:${compassTone}`}
              title=${`Compass: ${task.compass_audit_verdict}`}
            ></span>`
          : null}
      </div>
      <div class="kbn-card-links">
        <${MdLink} path=${task.spec_path} label="spec" />
        ${task.latest_audit_report_path
          ? html`<${MdLink}
              path=${task.latest_audit_report_path}
              label=${`audit (${task.latest_audit_kind || "?"}, ${task.latest_audit_verdict || "?"})`}
            />`
          : null}
        ${task.compass_audit_report_path
          ? html`<${MdLink} path=${task.compass_audit_report_path} label="compass" />`
          : null}
      </div>
      ${driftBanner
        ? html`<div class="kbn-drift-banner">${task.latest_audit_kind || "audit"} failed</div>`
        : null}
      ${expanded
        ? html`
            <div class="kbn-card-expanded">
              ${task.description
                ? html`<div class="kbn-card-description">${task.description}</div>`
                : null}
              <div class="kbn-card-facts">
                <span>${task.id}</span>
                <span>${task.created_at ? `created ${timeAgo(task.created_at)}` : "created unknown"}</span>
                ${task.blocked_reason ? html`<span>${task.blocked_reason}</span>` : null}
              </div>
              <div class="kbn-expanded-head">Role history</div>
              ${historyBusy
                ? html`<div class="kbn-empty">loading history...</div>`
                : html`<${AssignmentHistory} history=${history} />`}
            </div>
          `
        : null}
    </article>
  `;
}


// ---------------------------------------------------------------- columns


function Column({ stage, label, tasks, expandedId, historyByTask, historyBusy, onExpand, onAssignRole }) {
  const displayed = tasks || [];
  return html`
    <section class="kbn-column">
      <div class="kbn-column-head">
        ${label || stage} <span class="kbn-count">${displayed.length}</span>
      </div>
      <div class="kbn-column-body">
        ${displayed.length === 0
          ? html`<div class="kbn-empty">(none)</div>`
          : displayed.map((t) => html`
              <${Card}
                key=${t.id}
                task=${t}
                expanded=${expandedId === t.id}
                history=${historyByTask[t.id]}
                historyBusy=${historyBusy === t.id}
                onExpand=${onExpand}
                onAssignRole=${onAssignRole}
              />
            `)}
      </div>
    </section>
  `;
}


function AuditBand({ label, tasks, expandedId, historyByTask, historyBusy, onExpand, onAssignRole }) {
  const displayed = tasks || [];
  return html`
    <div class="kbn-audit-band">
      <div class="kbn-audit-band-head">
        ${label} <span class="kbn-count">${displayed.length}</span>
      </div>
      <div class="kbn-column-body">
        ${displayed.length === 0
          ? html`<div class="kbn-empty">(none)</div>`
          : displayed.map((t) => html`
              <${Card}
                key=${t.id}
                task=${t}
                expanded=${expandedId === t.id}
                history=${historyByTask[t.id]}
                historyBusy=${historyBusy === t.id}
                onExpand=${onExpand}
                onAssignRole=${onAssignRole}
              />
            `)}
      </div>
    </div>
  `;
}


function AuditColumn(props) {
  const syntax = props.syntax || [];
  const semantics = props.semantics || [];
  return html`
    <section class="kbn-column kbn-audit-column">
      <div class="kbn-column-head">
        Audit <span class="kbn-count">${syntax.length + semantics.length}</span>
      </div>
      <${AuditBand}
        label="Syntax"
        tasks=${syntax}
        expandedId=${props.expandedId}
        historyByTask=${props.historyByTask}
        historyBusy=${props.historyBusy}
        onExpand=${props.onExpand}
        onAssignRole=${props.onAssignRole}
      />
      <${AuditBand}
        label="Semantics"
        tasks=${semantics}
        expandedId=${props.expandedId}
        historyByTask=${props.historyByTask}
        historyBusy=${props.historyBusy}
        onExpand=${props.onExpand}
        onAssignRole=${props.onAssignRole}
      />
    </section>
  `;
}


// ---------------------------------------------------------------- modals


function ComposerModal({ open, onClose, onCreate }) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [priority, setPriority] = useState("normal");
  const [complexity, setComplexity] = useState("standard");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  if (!open) return null;

  const submit = async (ev) => {
    ev.preventDefault();
    if (!title.trim()) {
      setErr("title is required");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await onCreate({
        title: title.trim(),
        description: description.trim(),
        priority,
        complexity,
      });
      setTitle("");
      setDescription("");
      setPriority("normal");
      setComplexity("standard");
      onClose();
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  return html`
    <div class="kbn-modal-backdrop" onClick=${onClose}>
      <div class="kbn-modal" onClick=${(e) => e.stopPropagation()}>
        <div class="kbn-modal-head">New task</div>
        <form onSubmit=${submit}>
          <label class="kbn-label">Title</label>
          <input
            class="kbn-input"
            type="text"
            value=${title}
            onInput=${(e) => setTitle(e.target.value)}
            autoFocus
            maxlength="300"
          />
          <label class="kbn-label">Description</label>
          <textarea
            class="kbn-textarea"
            rows="6"
            value=${description}
            onInput=${(e) => setDescription(e.target.value)}
            maxlength="10000"
          ></textarea>
          <div class="kbn-row">
            <label class="kbn-label">Priority
              <select class="kbn-select" value=${priority} onChange=${(e) => setPriority(e.target.value)}>
                <option value="low">low</option>
                <option value="normal">normal</option>
                <option value="high">high</option>
                <option value="urgent">urgent</option>
              </select>
            </label>
            <label class="kbn-label">Complexity
              <select class="kbn-select" value=${complexity} onChange=${(e) => setComplexity(e.target.value)}>
                <option value="standard">standard (full pipeline)</option>
                <option value="simple">simple (skip audit + ship)</option>
              </select>
            </label>
          </div>
          ${err ? html`<div class="kbn-error">${err}</div>` : null}
          <div class="kbn-modal-actions">
            <button type="button" class="kbn-btn" onClick=${onClose} disabled=${busy}>Cancel</button>
            <button type="submit" class="kbn-btn kbn-btn-primary" disabled=${busy}>
              ${busy ? "Creating..." : "Create task"}
            </button>
          </div>
        </form>
      </div>
    </div>
  `;
}


function AssignRoleModal({ target, onClose, onAssign }) {
  const [to, setTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    setTo("");
    setErr(null);
  }, [target?.task?.id, target?.role]);

  if (!target) return null;
  const label = ROLE_LABELS[target.role] || target.role;

  const submit = async (ev) => {
    ev.preventDefault();
    if (!to.trim()) {
      setErr("assignee is required");
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await onAssign(target.task, target.role, to.trim());
      onClose();
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  return html`
    <div class="kbn-modal-backdrop" onClick=${onClose}>
      <div class="kbn-modal kbn-assign-modal" onClick=${(e) => e.stopPropagation()}>
        <div class="kbn-modal-head">Assign ${label}</div>
        <form onSubmit=${submit}>
          <div class="kbn-modal-task">${target.task.title || target.task.id}</div>
          <label class="kbn-label">Player or pool</label>
          <input
            class="kbn-input"
            type="text"
            value=${to}
            placeholder="p3 or p3,p4"
            onInput=${(e) => setTo(e.target.value)}
            autoFocus
          />
          ${err ? html`<div class="kbn-error">${err}</div>` : null}
          <div class="kbn-modal-actions">
            <button type="button" class="kbn-btn" onClick=${onClose} disabled=${busy}>Cancel</button>
            <button type="submit" class="kbn-btn kbn-btn-primary" disabled=${busy}>
              ${busy ? "Assigning..." : "Assign"}
            </button>
          </div>
        </form>
      </div>
    </div>
  `;
}


// ---------------------------------------------------------------- archive drawer


function ArchiveDrawer({ open, onClose, authedFetch }) {
  const saved = useMemo(savedArchiveState, []);
  const [rows, setRows] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [includeCancelled, setIncludeCancelled] = useState(Boolean(saved.includeCancelled));
  const [q, setQ] = useState(saved.q || "");
  const [startDate, setStartDate] = useState(saved.startDate || "");
  const [endDate, setEndDate] = useState(saved.endDate || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [expandedId, setExpandedId] = useState(null);

  const limit = 50;

  useEffect(() => {
    saveArchiveState({ includeCancelled, q, startDate, endDate });
  }, [includeCancelled, q, startDate, endDate]);

  const fetchPage = useCallback(async (nextOffset, append) => {
    if (!open) return;
    setBusy(true);
    setErr(null);
    try {
      const params = new URLSearchParams();
      params.set("limit", String(limit));
      params.set("offset", String(nextOffset));
      if (includeCancelled) params.set("include_cancelled", "true");
      if (q.trim()) params.set("q", q.trim());
      const data = await apiGet(authedFetch, `/archive?${params.toString()}`);
      const incoming = data.tasks || [];
      setRows((prev) => {
        if (!append) return incoming;
        const seen = new Set(prev.map((t) => t.id));
        return [...prev, ...incoming.filter((t) => !seen.has(t.id))];
      });
      setTotal(Number(data.total || 0));
      setOffset(nextOffset + incoming.length);
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }, [authedFetch, open, includeCancelled, q]);

  useEffect(() => {
    if (open) fetchPage(0, false);
  }, [open, includeCancelled, q, fetchPage]);

  const visibleRows = useMemo(
    () => rows.filter((t) => {
      const d = dateOnly(t.archived_at);
      if (startDate && (!d || d < startDate)) return false;
      if (endDate && (!d || d > endDate)) return false;
      return true;
    }),
    [rows, startDate, endDate]
  );

  if (!open) return null;
  return html`
    <section class="kbn-archive-drawer">
      <div class="kbn-archive-head">
        <span>Archive (${total})</span>
        <input
          class="kbn-input kbn-archive-search"
          type="search"
          placeholder="Search title/description..."
          value=${q}
          onInput=${(e) => setQ(e.target.value)}
        />
        <input class="kbn-input kbn-date-filter" type="date" value=${startDate} onInput=${(e) => setStartDate(e.target.value)} />
        <input class="kbn-input kbn-date-filter" type="date" value=${endDate} onInput=${(e) => setEndDate(e.target.value)} />
        <label class="kbn-archive-toggle">
          <input
            type="checkbox"
            checked=${includeCancelled}
            onChange=${(e) => setIncludeCancelled(e.target.checked)}
          />
          show cancelled
        </label>
        <button type="button" class="kbn-btn" onClick=${onClose}>Close</button>
      </div>
      ${err ? html`<div class="kbn-error">${err}</div>` : null}
      ${busy && rows.length === 0 ? html`<div class="kbn-empty">loading...</div>` : null}
      <div class="kbn-archive-body">
        ${visibleRows.length === 0 && !busy
          ? html`<div class="kbn-empty">(no archived tasks)</div>`
          : visibleRows.map((t) => {
              const expanded = expandedId === t.id;
              const primary = archiveAssignee(t);
              return html`
                <article
                  class=${`kbn-archive-row ${expanded ? "expanded" : ""}`}
                  key=${t.id}
                  onClick=${() => setExpandedId(expanded ? null : t.id)}
                >
                  <div class="kbn-archive-row-title">
                    <${Avatar} primary=${primary} />
                    <span>${t.title || "(untitled)"}</span>
                    ${t.cancelled_at ? html`<span class="kbn-cancelled-chip">CANCELLED</span>` : null}
                    ${t.complexity === "simple" ? html`<span class="kbn-simple-chip">SIMPLE</span>` : null}
                  </div>
                  <div class="kbn-archive-row-meta">
                    <span>${t.id}</span>
                    <span>pri=${t.priority || "normal"}</span>
                    <span>archived ${timeAgo(t.archived_at)}</span>
                  </div>
                  <div class="kbn-archive-row-links">
                    <${MdLink} path=${t.spec_path} label="spec" />
                    ${t.latest_audit_report_path
                      ? html`<${MdLink} path=${t.latest_audit_report_path} label="audit" />`
                      : null}
                  </div>
                  ${expanded
                    ? html`
                        <div class="kbn-card-expanded">
                          ${t.description ? html`<div class="kbn-card-description">${t.description}</div>` : null}
                          <div class="kbn-expanded-head">Role history</div>
                          <${AssignmentHistory} history=${t.assignments || []} />
                        </div>
                      `
                    : null}
                </article>
              `;
            })}
      </div>
      <div class="kbn-archive-pager">
        <span class="kbn-pager-info">${visibleRows.length} shown, ${total} matched</span>
        <button
          type="button"
          class="kbn-btn"
          disabled=${offset >= total || busy}
          onClick=${() => fetchPage(offset, true)}
        >
          ${busy && rows.length ? "Loading..." : "Load more"}
        </button>
      </div>
    </section>
  `;
}


// ---------------------------------------------------------------- main


export function KanbanPane({
  slot, authedFetch, onClose, onDropEdge, onPopOut,
  stacked, isMaximized, onToggleMaximize, kanbanEvents,
  activeProjectId, projectEpoch, wsConnected,
}) {
  const saved = savedArchiveState();
  const [board, setBoard] = useState({
    plan: [], execute: [], audit_syntax: [], audit_semantics: [], ship: [],
  });
  const [composerOpen, setComposerOpen] = useState(false);
  const [archiveOpen, setArchiveOpen] = useState(Boolean(saved.open));
  const [expandedId, setExpandedId] = useState(null);
  const [historyByTask, setHistoryByTask] = useState({});
  const [historyBusy, setHistoryBusy] = useState(null);
  const [assignTarget, setAssignTarget] = useState(null);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const data = await apiGet(authedFetch, "/board");
      setBoard(data.board || data || {});
      setError(null);
    } catch (e) {
      setError(e.message || String(e));
    }
  }, [authedFetch]);

  useEffect(() => {
    refresh();
  }, [refresh, activeProjectId, projectEpoch, wsConnected]);

  useEffect(() => {
    const current = savedArchiveState();
    saveArchiveState({ ...current, open: archiveOpen });
  }, [archiveOpen]);

  useEffect(() => {
    if (!kanbanEvents) return;
    const watched = new Set([
      "task_created", "task_claimed", "task_assigned",
      "task_updated", "task_stage_changed",
      "task_complexity_set", "task_blocked_changed",
      "task_spec_written", "task_role_assigned", "task_role_claimed",
      "task_role_completed", "task_drift_detected", "task_shipped",
      "audit_report_submitted", "compass_audit_logged",
      "commit_pushed", "project_switched", "socket_connected",
    ]);
    return kanbanEvents.subscribe((evt) => {
      if (evt && watched.has(evt.type)) refresh();
    });
  }, [kanbanEvents, refresh]);

  const loadHistory = useCallback(async (taskId, force = false) => {
    if (!force && historyByTask[taskId]) return;
    setHistoryBusy(taskId);
    try {
      const data = await apiGet(authedFetch, `/${encodeURIComponent(taskId)}/assignments`);
      setHistoryByTask((prev) => ({ ...prev, [taskId]: data.assignments || [] }));
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setHistoryBusy(null);
    }
  }, [authedFetch, historyByTask]);

  const toggleExpand = useCallback((taskId) => {
    setExpandedId((cur) => {
      const next = cur === taskId ? null : taskId;
      if (next) loadHistory(next);
      return next;
    });
  }, [loadHistory]);

  const onCreate = async (req) => {
    await apiPost(authedFetch, "", req);
    await refresh();
  };

  const onAssign = async (task, role, to) => {
    await apiPost(authedFetch, `/${encodeURIComponent(task.id)}/assign`, { role, to });
    setHistoryByTask((prev) => {
      const next = { ...prev };
      delete next[task.id];
      return next;
    });
    await refresh();
    if (expandedId === task.id) await loadHistory(task.id, true);
  };

  const paneClass = [
    "pane",
    "kbn-pane",
    stacked ? "stacked" : "",
    isMaximized ? "maximized" : "",
  ].filter(Boolean).join(" ");

  return html`
    <div id=${slot ? `pane-${slot}` : undefined} class=${paneClass} data-slot=${slot || ""}>
      <header class="pane-head kbn-header">
        <div class="pane-head-label">
          <span class="pane-head-slot kbn-title">Kanban</span>
          ${activeProjectId ? html`<span class="pane-head-task">${activeProjectId}</span>` : null}
        </div>
        <div class="pane-head-actions kbn-head-actions">
          <button class="kbn-btn kbn-btn-primary" type="button" onClick=${() => setComposerOpen(true)}>
            <${Icon} name="plus" /> New task
          </button>
          <button class=${`kbn-btn kbn-archive-toggle ${archiveOpen ? "active" : ""}`} type="button"
            onClick=${() => setArchiveOpen((v) => !v)}>
            Archive <${Icon} name="chevron" />
          </button>
          <${IconButton} title="Refresh" name="refresh" onClick=${refresh} />
          ${onPopOut && stacked
            ? html`<${IconButton} title="Move to new column" name="popout" onClick=${() => onPopOut(slot)} />`
            : null}
          ${onToggleMaximize
            ? html`<${IconButton}
                title=${isMaximized ? "Restore" : "Maximize"}
                name=${isMaximized ? "restore" : "maximize"}
                onClick=${onToggleMaximize}
              />`
            : null}
          ${onClose
            ? html`<${IconButton} title="Close" name="close" onClick=${onClose} />`
            : null}
        </div>
      </header>
      <div class="pane-body kbn-body">
        ${error ? html`<div class="kbn-error">${error}</div>` : null}
        <div class="kbn-columns">
          <${Column}
            stage="plan"
            label="Plan"
            tasks=${board.plan || []}
            expandedId=${expandedId}
            historyByTask=${historyByTask}
            historyBusy=${historyBusy}
            onExpand=${toggleExpand}
            onAssignRole=${(task, role) => setAssignTarget({ task, role })}
          />
          <${Column}
            stage="execute"
            label="Execute"
            tasks=${board.execute || []}
            expandedId=${expandedId}
            historyByTask=${historyByTask}
            historyBusy=${historyBusy}
            onExpand=${toggleExpand}
            onAssignRole=${(task, role) => setAssignTarget({ task, role })}
          />
          <${AuditColumn}
            syntax=${board.audit_syntax || []}
            semantics=${board.audit_semantics || []}
            expandedId=${expandedId}
            historyByTask=${historyByTask}
            historyBusy=${historyBusy}
            onExpand=${toggleExpand}
            onAssignRole=${(task, role) => setAssignTarget({ task, role })}
          />
          <${Column}
            stage="ship"
            label="Ship"
            tasks=${board.ship || []}
            expandedId=${expandedId}
            historyByTask=${historyByTask}
            historyBusy=${historyBusy}
            onExpand=${toggleExpand}
            onAssignRole=${(task, role) => setAssignTarget({ task, role })}
          />
        </div>
        <${ArchiveDrawer}
          open=${archiveOpen}
          onClose=${() => setArchiveOpen(false)}
          authedFetch=${authedFetch}
        />
      </div>
      <${ComposerModal}
        open=${composerOpen}
        onClose=${() => setComposerOpen(false)}
        onCreate=${onCreate}
      />
      <${AssignRoleModal}
        target=${assignTarget}
        onClose=${() => setAssignTarget(null)}
        onAssign=${onAssign}
      />
    </div>
  `;
}


// ---------------------------------------------------------------- event router


export function createKanbanEventRouter() {
  const subscribers = new Set();
  return {
    subscribe(cb) {
      subscribers.add(cb);
      return () => subscribers.delete(cb);
    },
    publish(evt) {
      for (const cb of Array.from(subscribers)) {
        try { cb(evt); } catch (_) {}
      }
    },
  };
}

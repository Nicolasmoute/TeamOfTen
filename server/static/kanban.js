// Kanban dashboard (Docs/kanban-specs.md section 11).
//
// Single-file Preact pane mounted under slot `__kanban`. The active board
// renders Plan / Execute / Review / Ship, with review split into formal and
// semantic bands. Archived tasks live in a separate drawer.

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
  audit_syntax: "FORMAL",
  audit_semantics: "SEMANTIC",
  ship: "SHIP",
  archive: "ARCHIVE",
};

const TRAJECTORY_TOKENS = {
  plan: "P",
  execute: "E",
  audit_syntax: "AY",
  audit_semantics: "AE",
  ship: "S",
};

// Render a compact trajectory marker like `P → [E] → AY → S` with the
// current stage in brackets. Returns null if the trajectory is empty
// or unparseable. Mirror of agents.py:_trajectory_marker.
function renderTrajectoryMarker(trajectory, currentStage) {
  let traj = trajectory;
  if (typeof traj === "string") {
    try { traj = JSON.parse(traj); } catch (_) { return null; }
  }
  if (!Array.isArray(traj) || traj.length === 0) return null;
  const tokens = [];
  for (const stageObj of traj) {
    if (!stageObj || typeof stageObj !== "object") continue;
    const stage = stageObj.stage;
    const tok = TRAJECTORY_TOKENS[stage];
    if (!tok) continue;
    tokens.push({ tok, current: stage === currentStage });
  }
  if (tokens.length === 0) return null;
  return html`<span class="kbn-trajectory" title="trajectory">
    ${tokens.map((t, i) => html`
      ${i > 0 ? html`<span class="kbn-traj-arrow">→</span>` : null}
      <span class=${`kbn-traj-stage ${t.current ? "current" : ""}`}>${t.tok}</span>
    `)}
  </span>`;
}

// Trajectory presets used by the composer. Lifted from
// Docs/kanban-specs.md §3 examples.
const TRAJECTORY_PRESETS = [
  { id: "execute_only", label: "Execute only (quick mechanical)",
    trajectory: [{ stage: "execute", to: [] }] },
  { id: "plan_execute", label: "Plan + Execute (no audit)",
    trajectory: [
      { stage: "plan", to: [] },
      { stage: "execute", to: [] },
    ] },
  { id: "code_formal", label: "Code with formal review",
    trajectory: [
      { stage: "plan", to: [] },
      { stage: "execute", to: [] },
      { stage: "audit_syntax", to: [] },
      { stage: "ship", to: [] },
    ] },
  { id: "marketing_semantic", label: "Marketing/writing with semantic review",
    trajectory: [
      { stage: "plan", to: [] },
      { stage: "execute", to: [] },
      { stage: "audit_semantics", to: [] },
      { stage: "ship", to: [] },
    ] },
  { id: "full_pipeline", label: "Full pipeline (plan + both audits + ship)",
    trajectory: [
      { stage: "plan", to: [] },
      { stage: "execute", to: [] },
      { stage: "audit_syntax", to: [] },
      { stage: "audit_semantics", to: [] },
      { stage: "ship", to: [] },
    ] },
];

const PRIORITY_TONE = {
  urgent: "var(--err)",
  high: "var(--warn)",
  normal: "var(--accent)",
  low: "var(--muted)",
};

const ROLE_LABELS = {
  planner: "planner",
  executor: "executor",
  auditor_syntax: "formal reviewer",
  auditor_semantics: "semantic reviewer",
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
        ${renderTrajectoryMarker(task.trajectory, task.status)}
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
                ${task.workflow ? html`<span>${task.workflow}</span>` : null}
                ${task.required_reviews ? html`<span>reviews ${task.required_reviews}</span>` : null}
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
        Review <span class="kbn-count">${syntax.length + semantics.length}</span>
      </div>
      <${AuditBand}
        label="Formal"
        tasks=${syntax}
        expandedId=${props.expandedId}
        historyByTask=${props.historyByTask}
        historyBusy=${props.historyBusy}
        onExpand=${props.onExpand}
        onAssignRole=${props.onAssignRole}
      />
      <${AuditBand}
        label="Semantic"
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
  const [desc, setDesc] = useState("");
  const [priority, setPriority] = useState("normal");
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
      const payload = { title: title.trim(), priority };
      if (desc.trim()) payload.description = desc.trim();
      await onCreate(payload);
      setTitle("");
      setDesc("");
      setPriority("normal");
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
        <div class="kbn-modal-head">Add to backlog</div>
        <form onSubmit=${submit}>
          <label class="kbn-label">Idea / title</label>
          <textarea
            class="kbn-input kbn-backlog-input"
            value=${title}
            onInput=${(e) => setTitle(e.target.value)}
            autoFocus
            rows="3"
            placeholder="Describe the idea — a sentence or two is fine"
          ></textarea>
          <label class="kbn-label" style="margin-top:8px">Description (optional)</label>
          <textarea
            class="kbn-input kbn-backlog-desc-input"
            value=${desc}
            onInput=${(e) => setDesc(e.target.value)}
            rows="3"
            placeholder="More context for Coach — background, motivation, scope..."
          ></textarea>
          <div class="kbn-modal-priority-row">
            <label class="kbn-label kbn-priority-label" for="kbn-backlog-priority">Priority</label>
            <select
              id="kbn-backlog-priority"
              class="kbn-priority-select"
              value=${priority}
              onChange=${(e) => setPriority(e.target.value)}
            >
              <option value="low">low</option>
              <option value="normal" selected>normal</option>
              <option value="high">high</option>
              <option value="urgent">urgent</option>
            </select>
          </div>
          <div class="kbn-help">
            Coach will review this on the next tick and either promote it
            to a real task (with trajectory) or reject it with a reason.
          </div>
          ${err ? html`<div class="kbn-error">${err}</div>` : null}
          <div class="kbn-modal-actions">
            <button type="button" class="kbn-btn" onClick=${onClose} disabled=${busy}>Cancel</button>
            <button type="submit" class="kbn-btn kbn-btn-primary" disabled=${busy}>
              ${busy ? "Adding..." : "Add to backlog"}
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
          <label class="kbn-label">Assignee (one Player)</label>
          <input
            class="kbn-input"
            type="text"
            value=${to}
            placeholder="p3"
            onInput=${(e) => setTo(e.target.value)}
            autoFocus
          />
          <div class="kbn-help-mini">
            v2: pools are FYI only — pick one named Player. Calls
            POST /api/tasks/&lt;id&gt;/approve_stage.
          </div>
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
                    ${renderTrajectoryMarker(t.trajectory, "archive")}
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


// ---------------------------------------------------------------- flow health


function FlowHealthFooter({ authedFetch, kanbanEvents }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const r = await authedFetch("/api/tasks/flow_health");
      if (!r.ok) throw new Error(`flow_health -> ${r.status}`);
      const json = await r.json();
      setData(json);
      setError(null);
    } catch (e) {
      setError(e.message || String(e));
    }
  }, [authedFetch]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 30_000);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    if (!kanbanEvents) return;
    const watched = new Set([
      "task_stage_changed", "task_stage_stale", "task_trajectory_changed",
    ]);
    return kanbanEvents.subscribe((evt) => {
      if (evt && watched.has(evt.type)) refresh();
    });
  }, [kanbanEvents, refresh]);

  if (error) {
    return html`<div class="kanban-flow-health alert">Flow: ${error}</div>`;
  }
  if (!data) {
    return html`<div class="kanban-flow-health">Flow: loading…</div>`;
  }
  const aliveCls = data.subscriber_alive && data.stalled_count === 0
    ? "ok" : "alert";
  const last = data.subscriber_last_event_at
    ? new Date(data.subscriber_last_event_at).toLocaleTimeString()
    : "never";
  return html`
    <div class=${`kanban-flow-health ${aliveCls}`}>
      Subscriber: ${data.subscriber_alive ? "alive" : "DOWN"}
      · Stalled: ${data.stalled_count}
      · Last event: ${last}
    </div>
  `;
}


// ---------------------------------------------------------------- BacklogColumn


// Inline SVG icons for BacklogCard actions (CSS-drawn; no emoji).
const _PENCIL_SVG = html`<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`;
const _TRASH_SVG = html`<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`;


const _BACKLOG_DESC_PREVIEW = 120;

// BacklogEditModal — opened by the pencil icon on a BacklogCard.
// Mirrors ComposerModal structure; dirty-check uses an inline confirm modal
// (same pattern as the delete-confirm) instead of window.confirm.
function BacklogEditModal({ entry, authedFetch, onClose, onSave }) {
  const [title, setTitle] = useState(entry.title);
  const [desc, setDesc] = useState(entry.description || "");
  const [priority, setPriority] = useState(entry.priority || "normal");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  const isDirty = () =>
    title !== entry.title ||
    desc !== (entry.description || "") ||
    priority !== (entry.priority || "normal");

  const tryClose = () => { if (isDirty()) setConfirmDiscard(true); else onClose(); };

  const submit = async (ev) => {
    ev.preventDefault();
    const t = title.trim();
    if (!t) { setErr("title is required"); return; }
    setBusy(true); setErr(null);
    try {
      const payload = { title: t, description: desc.trim() || null, priority };
      const r = await authedFetch(`/api/backlog/${entry.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) { const msg = await r.text().catch(() => r.statusText); throw new Error(msg); }
      onSave();
      onClose();
    } catch (e) {
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  return html`
    <div class="kbn-modal-backdrop" onClick=${(e) => { e.stopPropagation(); tryClose(); }}>
      <div class="kbn-modal" onClick=${(e) => e.stopPropagation()}
           onKeyDown=${(e) => { if (e.key === "Escape") { e.stopPropagation(); if (!confirmDiscard) tryClose(); } }}>
        <div class="kbn-modal-head">Edit backlog entry</div>
        <form onSubmit=${submit}>
          <label class="kbn-label">Idea / title</label>
          <textarea
            class="kbn-input kbn-backlog-input"
            value=${title}
            onInput=${(e) => setTitle(e.target.value)}
            autoFocus
            rows="3"
            disabled=${busy}
          ></textarea>
          <label class="kbn-label" style="margin-top:8px">Description (optional)</label>
          <textarea
            class="kbn-input kbn-backlog-desc-input"
            value=${desc}
            onInput=${(e) => setDesc(e.target.value)}
            rows="3"
            placeholder="More context for Coach…"
            disabled=${busy}
          ></textarea>
          <div class="kbn-modal-priority-row">
            <label class="kbn-label kbn-priority-label">Priority</label>
            <select
              class="kbn-priority-select"
              value=${priority}
              onChange=${(e) => setPriority(e.target.value)}
              disabled=${busy}
            >
              <option value="low">low</option>
              <option value="normal">normal</option>
              <option value="high">high</option>
              <option value="urgent">urgent</option>
            </select>
          </div>
          ${err ? html`<div class="kbn-error">${err}</div>` : null}
          <div class="kbn-modal-actions">
            <button type="button" class="kbn-btn" onClick=${tryClose} disabled=${busy}>Cancel</button>
            <button type="submit" class="kbn-btn kbn-btn-primary" disabled=${busy || !title.trim()}>
              ${busy ? "Saving…" : "Save"}
            </button>
          </div>
        </form>
        ${confirmDiscard ? html`
          <div class="kbn-modal-backdrop" onClick=${(e) => { e.stopPropagation(); setConfirmDiscard(false); }}>
            <div class="kbn-modal" onClick=${(e) => e.stopPropagation()}
                 onKeyDown=${(e) => { if (e.key === "Escape") { e.stopPropagation(); setConfirmDiscard(false); } }}>
              <div class="kbn-modal-head">Discard changes?</div>
              <p class="kbn-modal-body-text">You have unsaved changes. Discard them?</p>
              <div class="kbn-modal-actions">
                <button class="kbn-btn" autoFocus onClick=${(e) => { e.stopPropagation(); setConfirmDiscard(false); }}>Keep editing</button>
                <button class="kbn-btn kbn-btn-danger" onClick=${(e) => { e.stopPropagation(); onClose(); }}>Discard</button>
              </div>
            </div>
          </div>` : null}
      </div>
    </div>
  `;
}

function BacklogCard({ entry, authedFetch, onRefresh }) {
  const proposer = entry.proposed_by || "?";
  let proposerLabel;
  if (proposer === "coach") proposerLabel = "C";
  else if (proposer === "human") proposerLabel = "human";
  else if (proposer.startsWith("p") && /^\d+$/.test(proposer.slice(1)))
    proposerLabel = proposer.slice(1);
  else proposerLabel = proposer;

  const [expanded, setExpanded] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteErr, setDeleteErr] = useState(null);

  const confirmAndDelete = async (e) => {
    e.stopPropagation();
    setDeleteBusy(true);
    setDeleteErr(null);
    try {
      const r = await authedFetch(`/api/backlog/${entry.id}`, { method: "DELETE" });
      if (!r.ok) {
        const msg = await r.text().catch(() => r.statusText);
        throw new Error(msg);
      }
      setConfirmDelete(false);
      onRefresh();
    } catch (err) {
      setDeleteErr(err.message || String(err));
      setDeleteBusy(false);
    }
  };

  const desc = entry.description || "";
  const pri = entry.priority || "normal";

  return html`
    <div
      class="kbn-card kbn-backlog-card kbn-backlog-pri-${pri}${expanded ? " expanded" : ""}"
      onClick=${() => setExpanded(!expanded)}
    >
      <div class="kbn-card-title">${entry.title}</div>
      ${expanded && desc ? html`<div class="kbn-backlog-desc">${desc}</div>` : null}
      <div class="kbn-card-meta">
        <span class="kbn-backlog-priority-chip kbn-backlog-pri-${pri}">${pri.toUpperCase()}</span>
        <span class="kbn-backlog-proposer">${proposerLabel}</span>
        <span class="kbn-card-age">${timeAgo(entry.proposed_at)}</span>
        ${desc && !expanded ? html`<span class="kbn-backlog-has-desc" title="Has description">…</span>` : null}
      </div>
      <div class="kbn-card-actions kbn-backlog-actions">
        <button class="kbn-card-act-btn" title="Edit"
          onClick=${(e) => { e.stopPropagation(); setShowEdit(true); }}>${_PENCIL_SVG}</button>
        <button class="kbn-card-act-btn kbn-card-act-danger" title="Delete"
          onClick=${(e) => { e.stopPropagation(); setDeleteErr(null); setConfirmDelete(true); }}>${_TRASH_SVG}</button>
      </div>
      ${showEdit ? html`<${BacklogEditModal}
        entry=${entry}
        authedFetch=${authedFetch}
        onClose=${() => setShowEdit(false)}
        onSave=${onRefresh}
      />` : null}
      ${confirmDelete ? html`
        <div class="kbn-modal-backdrop" onClick=${(e) => { e.stopPropagation(); setConfirmDelete(false); }}>
          <div class="kbn-modal" onClick=${(e) => e.stopPropagation()}>
            <div class="kbn-modal-head">Delete idea?</div>
            <p class="kbn-modal-body-text">Delete "<strong>${entry.title}</strong>"? This cannot be undone.</p>
            ${deleteErr ? html`<div class="kbn-error">${deleteErr}</div>` : null}
            <div class="kbn-modal-actions">
              <button class="kbn-btn" onClick=${() => setConfirmDelete(false)} disabled=${deleteBusy}>Cancel</button>
              <button class="kbn-btn kbn-btn-danger" onClick=${confirmAndDelete} disabled=${deleteBusy}>
                ${deleteBusy ? "Deleting…" : "Delete"}
              </button>
            </div>
          </div>
        </div>` : null}
    </div>
  `;
}

const _PRI_ORDER = { urgent: 0, high: 1, normal: 2, low: 3 };

function _sortByPriority(entries) {
  return [...entries].sort((a, b) => {
    const pa = _PRI_ORDER[a.priority] ?? 2;
    const pb = _PRI_ORDER[b.priority] ?? 2;
    if (pa !== pb) return pa - pb;
    // same priority: older first (stable insert order)
    return (a.proposed_at || "") < (b.proposed_at || "") ? -1 : 1;
  });
}

function BacklogColumn({ entries, onRefresh, authedFetch }) {
  if (!entries) return null;
  const sorted = _sortByPriority(entries);
  return html`
    <div class="kbn-column kbn-backlog-column">
      <div class="kbn-column-head">
        <span>BACKLOG</span>
        <span class="kbn-count">${entries.length}</span>
      </div>
      <div class="kbn-column-body">
        ${sorted.length === 0
          ? html`<div class="kbn-empty">No pending ideas</div>`
          : sorted.map((e) => html`<${BacklogCard} key=${e.id} entry=${e} authedFetch=${authedFetch} onRefresh=${onRefresh} />`)}
      </div>
    </div>
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
  const [backlogEntries, setBacklogEntries] = useState([]);
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

  const refreshBacklog = useCallback(async () => {
    try {
      const r = await authedFetch("/api/backlog?status=pending");
      if (r.ok) {
        const d = await r.json();
        setBacklogEntries(d.backlog || []);
      }
    } catch (_) {}
  }, [authedFetch]);

  useEffect(() => {
    refresh();
    refreshBacklog();
  }, [refresh, refreshBacklog, activeProjectId, projectEpoch, wsConnected]);

  useEffect(() => {
    const current = savedArchiveState();
    saveArchiveState({ ...current, open: archiveOpen });
  }, [archiveOpen]);

  useEffect(() => {
    if (!kanbanEvents) return;
    const watched = new Set([
      "task_created", "task_claimed", "task_assigned",
      "task_updated", "task_stage_changed", "task_archived",
      "task_trajectory_changed", "task_blocked_changed",
      "task_spec_written", "task_role_assigned", "task_role_claimed",
      "task_role_called", "task_role_stand_down", "task_role_completed",
      "task_workflow_set", "task_drift_detected",
      "task_stage_stale", "audit_report_submitted",
      "audit_fail_notification", "compass_audit_logged",
      "commit_pushed", "project_switched", "socket_connected",
    ]);
    const backlogWatched = new Set([
      "backlog_task_proposed", "backlog_task_promoted", "backlog_task_rejected",
      "backlog_entry_updated", "backlog_entry_deleted",
    ]);
    return kanbanEvents.subscribe((evt) => {
      if (!evt) return;
      if (watched.has(evt.type)) refresh();
      if (backlogWatched.has(evt.type)) refreshBacklog();
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
    const r = await authedFetch("/api/backlog", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
    if (!r.ok) {
      const msg = await r.text().catch(() => r.statusText);
      throw new Error(msg);
    }
    await refreshBacklog();
  };

  // v2 §7.1: stage transitions and role assignment are one atomic
  // action via `coord_approve_stage` (HTTP: POST /approve_stage).
  // Role→stage mapping is fixed: planner→plan, executor→execute,
  // auditor_syntax→audit_syntax, auditor_semantics→audit_semantics,
  // shipper→ship.
  const ROLE_TO_STAGE = {
    planner: "plan",
    executor: "execute",
    auditor_syntax: "audit_syntax",
    auditor_semantics: "audit_semantics",
    shipper: "ship",
  };
  const onAssign = async (task, role, to) => {
    const next_stage = ROLE_TO_STAGE[role] || role;
    await apiPost(
      authedFetch,
      `/${encodeURIComponent(task.id)}/approve_stage`,
      { next_stage, assignee: to, note: "approved via UI" },
    );
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
            <${Icon} name="plus" /> New idea
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
          <${BacklogColumn}
            entries=${backlogEntries}
            onRefresh=${refreshBacklog}
            authedFetch=${authedFetch}
          />
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
        <${FlowHealthFooter}
          authedFetch=${authedFetch}
          kanbanEvents=${kanbanEvents}
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

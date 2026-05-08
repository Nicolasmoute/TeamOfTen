// Playbook dashboard (harness-styled v1).
//
// Single-file Preact pane mounted under slot `__playbook`. Information
// architecture follows Docs/playbook-specs.md §13. Visuals reuse the
// harness CSS variable set (--bg, --fg, --accent, --ok, --warn, --err).
//
// All glyphs are CSS-drawn or inline SVG per the no-emoji rule from
// CLAUDE.md.
//
// Sections (§13.1):
//   1. Header bar — capacity, last-run, run-now, bootstrap-now.
//   2. Active statements — bucketed by weight, sorted by
//      weight × log(1+applied_count) descending.
//   3. Archived <details> — restore button per row.
//   4. Recent runs — clickable for full evidence summary.
//   5. Footer — reset / disable toggles behind <details>.

import { h } from "https://esm.sh/preact@10";
import { useState, useEffect, useCallback, useMemo } from "https://esm.sh/preact@10/hooks";
import htm from "/static/vendor/htm.js";

const html = htm.bind(h);

// ---------------------------------------------------------------- helpers

function clamp(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }

function fmtWeight(w) {
  if (typeof w !== "number" || Number.isNaN(w)) return "—";
  return w.toFixed(2);
}

function fmtCost(c) {
  if (typeof c !== "number" || Number.isNaN(c)) return "—";
  return `$${c.toFixed(4)}`;
}

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

// Bucket label / boundaries (spec §6.2 / §13.1).
const BUCKETS = [
  { label: "Validated (≥ 0.85)", lo: 0.85, hi: 1.01, accent: "var(--ok)" },
  { label: "Working (0.65 – 0.85)", lo: 0.65, hi: 0.85, accent: "var(--accent)" },
  { label: "Uncertain (0.35 – 0.65)", lo: 0.35, hi: 0.65, accent: "var(--muted)" },
  { label: "Anti-pattern (< 0.35)", lo: 0.0, hi: 0.35, accent: "var(--err)" },
];

function bucketIndex(w) {
  for (let i = 0; i < BUCKETS.length; i++) {
    if (w >= BUCKETS[i].lo && w < BUCKETS[i].hi) return i;
  }
  return BUCKETS.length - 1;
}

// Sort key for within-bucket ordering: weight × log(1 + applied_count).
function sortScore(s) {
  return -(s.weight * Math.log1p(s.applied_count || 0) + 1e-6 * s.weight);
}

// ---------------------------------------------------------------- API

async function apiFetch(authedFetch, path, opts) {
  const finalOpts = { ...(opts || {}) };
  if (finalOpts.body !== undefined && typeof finalOpts.body === "string") {
    const headers = { "Content-Type": "application/json", ...(finalOpts.headers || {}) };
    finalOpts.headers = headers;
  }
  const res = await authedFetch(`/api/playbook${path}`, finalOpts);
  if (!res.ok) {
    let body = "";
    try { body = await res.text(); } catch (_) {}
    throw new Error(`HTTP ${res.status}: ${body || res.statusText}`);
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.startsWith("application/json")) return res.json();
  return res.text();
}

// ---------------------------------------------------------------- weight bar

function WeightBar({ weight }) {
  const w = clamp(weight, 0, 1);
  const bucket = BUCKETS[bucketIndex(w)];
  return html`
    <div class="pb-weight-bar" title=${`weight ${fmtWeight(w)}`}>
      <div class="pb-weight-fill" style=${`width: ${(w * 100).toFixed(1)}%; background: ${bucket.accent}`}></div>
      <div class="pb-weight-marker" style=${`left: 50%`}></div>
    </div>
  `;
}

// ---------------------------------------------------------------- override modal

function OverrideModal({ statement, onClose, onConfirm }) {
  const [target, setTarget] = useState(null);
  const trigger = useCallback(async (val) => {
    setTarget(val);
    try {
      await onConfirm(val);
      onClose();
    } catch (e) {
      setTarget(null);
      // eslint-disable-next-line no-alert
      alert(`Override failed: ${e.message}`);
    }
  }, [onConfirm, onClose]);

  return html`
    <div class="pb-modal-bg" onClick=${onClose}>
      <div class="pb-modal" onClick=${(e) => e.stopPropagation()}>
        <h3>Override weight</h3>
        <div class="pb-modal-body">
          <div class="pb-stmt-text">${statement.text}</div>
          <div class="pb-modal-meta">id: ${statement.id} — current weight: ${fmtWeight(statement.weight)}</div>
          <div class="pb-modal-buttons">
            <button class="pb-btn pb-btn-no" disabled=${target !== null} onClick=${() => trigger(0.0)}>NO (0.0)</button>
            <button class="pb-btn pb-btn-half" disabled=${target !== null} onClick=${() => trigger(0.5)}>½ (0.5)</button>
            <button class="pb-btn pb-btn-yes" disabled=${target !== null} onClick=${() => trigger(1.0)}>YES (1.0)</button>
          </div>
          <div class="pb-modal-cancel">
            <button class="pb-btn-link" onClick=${onClose}>Cancel</button>
          </div>
        </div>
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------- statement row

function StatementRow({ stmt, onOverride }) {
  const [expanded, setExpanded] = useState(false);
  const lockBadge = stmt.immutable
    ? html`<span class="pb-badge-locked" title="immutable — cannot be adjusted">LOCKED</span>`
    : "";
  return html`
    <div class="pb-row">
      <div class="pb-row-top">
        <${WeightBar} weight=${stmt.weight} />
        <div class="pb-row-meta">
          <span class="pb-id">${stmt.id}</span>
          <span class="pb-w">${fmtWeight(stmt.weight)}</span>
          ${lockBadge}
        </div>
      </div>
      <div class="pb-row-text" onClick=${() => setExpanded(!expanded)}>${stmt.text}</div>
      <div class="pb-row-bottom">
        <span class="pb-applied">applied ${stmt.applied_count}×</span>
        <span class="pb-validated">${stmt.last_validated_at ? timeAgo(stmt.last_validated_at) : "never validated"}</span>
        ${stmt.immutable ? "" : html`
          <button class="pb-btn-mini" onClick=${() => onOverride(stmt)}>Override</button>
        `}
      </div>
      ${expanded && stmt.weight_history && stmt.weight_history.length ? html`
        <div class="pb-history">
          <div class="pb-history-title">Weight history (last ${stmt.weight_history.length})</div>
          ${stmt.weight_history.slice().reverse().map((h, i) => html`
            <div class="pb-history-row" key=${i}>
              <span class="pb-history-ts">${timeAgo(h.ts)}</span>
              <span class="pb-history-delta">
                ${h.from === null ? "—" : fmtWeight(h.from)} → ${fmtWeight(h.to)}
              </span>
              <span class="pb-history-reason">${h.reason || ""}</span>
            </div>
          `)}
        </div>
      ` : ""}
    </div>
  `;
}

// ---------------------------------------------------------------- archived row

function ArchivedRow({ stmt, onRestore }) {
  const [restoring, setRestoring] = useState(false);
  const handleRestore = async () => {
    if (!window.confirm(`Restore ${stmt.id} to active lattice?`)) return;
    setRestoring(true);
    try { await onRestore(stmt); } catch (e) { alert(e.message); }
    finally { setRestoring(false); }
  };
  return html`
    <div class="pb-archived-row">
      <div class="pb-archived-meta">
        <span class="pb-archived-reason pb-reason-${stmt.archive_reason}">${stmt.archive_reason}</span>
        <span class="pb-id">${stmt.id}</span>
        <span>final ${fmtWeight(stmt.final_weight)}</span>
        <span>${timeAgo(stmt.archived_at)}</span>
      </div>
      <div class="pb-archived-text">${stmt.text}</div>
      <button class="pb-btn-mini" disabled=${restoring} onClick=${handleRestore}>
        ${restoring ? "..." : "Restore"}
      </button>
    </div>
  `;
}

// ---------------------------------------------------------------- runs row

function RunRow({ row }) {
  const [open, setOpen] = useState(false);
  const outcome = row.outcome || "(no outcome)";
  const outcomeClass = outcome.startsWith("error") ? "pb-out-err"
    : outcome.startsWith("skipped") ? "pb-out-skip"
    : outcome === "no_changes" ? "pb-out-noop"
    : "pb-out-ok";
  return html`
    <div class="pb-run-row" onClick=${() => setOpen(!open)}>
      <div class="pb-run-summary">
        <span class="pb-run-time">${timeAgo(row.started_at)}</span>
        <span class="pb-run-kind">${row.kind || "?"}</span>
        <span class="pb-run-outcome ${outcomeClass}">${outcome}</span>
        <span class="pb-run-applied">applied ${(row.proposals_applied || []).length}</span>
        <span class="pb-run-rel">rel ${row.relevance_increments ?? 0}</span>
        <span class="pb-run-cost">${fmtCost(row.llm_call ? row.llm_call.cost_usd : null)}</span>
      </div>
      ${open ? html`
        <div class="pb-run-details">
          <div><strong>run_id:</strong> ${row.run_id}</div>
          ${row.evidence_summary ? html`
            <div><strong>evidence:</strong> ${JSON.stringify(row.evidence_summary)}</div>
          ` : ""}
          ${row.proposals_applied && row.proposals_applied.length ? html`
            <div><strong>applied:</strong></div>
            ${row.proposals_applied.map((op, i) => html`
              <div class="pb-op-row" key=${i}>${JSON.stringify(op)}</div>
            `)}
          ` : ""}
          ${row.proposals_rejected && row.proposals_rejected.length ? html`
            <div><strong>rejected:</strong></div>
            ${row.proposals_rejected.map((op, i) => html`
              <div class="pb-op-row pb-op-rejected" key=${i}>${JSON.stringify(op)}</div>
            `)}
          ` : ""}
          ${row.engine_actions && row.engine_actions.length ? html`
            <div><strong>engine actions:</strong></div>
            ${row.engine_actions.map((a, i) => html`
              <div class="pb-op-row" key=${i}>${JSON.stringify(a)}</div>
            `)}
          ` : ""}
        </div>
      ` : ""}
    </div>
  `;
}

// ---------------------------------------------------------------- main pane

function PlaybookPaneHeader({ onClose, isMaximized, onToggleMaximize }) {
  return html`
    <header class="pane-head pb-pane-head">
      <div class="pane-head-label">
        <span class="pb-pane-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="18" height="18">
            <path d="M3 5 L11 6 L11 19 L3 18 Z" fill="none" stroke="currentColor" stroke-width="1.4"/>
            <path d="M21 5 L13 6 L13 19 L21 18 Z" fill="none" stroke="currentColor" stroke-width="1.4"/>
          </svg>
        </span>
        <span class="pane-head-slot">Playbook</span>
      </div>
      <div class="pane-head-actions">
        ${onToggleMaximize
          ? html`<button class="pane-head-btn" title=${isMaximized ? "Restore" : "Maximize"} onClick=${onToggleMaximize}>${isMaximized ? "❐" : "⛶"}</button>`
          : null}
        <button class="pane-head-btn" title="Close" onClick=${onClose}>×</button>
      </div>
    </header>
  `;
}

export function PlaybookPane({ slot, authedFetch, playbookEvents, onClose, onDropEdge, onPopOut, stacked, isMaximized, onToggleMaximize }) {
  const [state, setState] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [overrideTarget, setOverrideTarget] = useState(null);
  const [busyAction, setBusyAction] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await apiFetch(authedFetch, "/state");
      setState(next);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [authedFetch]);

  // Initial load + WS event listeners
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    if (!playbookEvents) return undefined;
    const types = [
      "playbook_run_completed",
      "playbook_changes_applied",
      "playbook_statement_overridden",
      "playbook_settled",
      "playbook_staled",
      "playbook_reset",
      "playbook_bootstrap_completed",
    ];
    const unsubs = types.map((t) => playbookEvents.subscribe(t, () => refresh()));
    return () => unsubs.forEach((u) => u && u());
  }, [playbookEvents, refresh]);

  const overrideStatement = useCallback(async (newWeight) => {
    if (!overrideTarget) return;
    await apiFetch(authedFetch, `/statements/${overrideTarget.id}/weight`, {
      method: "POST",
      body: JSON.stringify({ weight: newWeight }),
    });
    await refresh();
  }, [authedFetch, overrideTarget, refresh]);

  const handleRestore = useCallback(async (stmt) => {
    await apiFetch(authedFetch, `/statements/${stmt.id}/restore`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    await refresh();
  }, [authedFetch, refresh]);

  const handleRunNow = useCallback(async (force) => {
    setBusyAction("run");
    try {
      await apiFetch(authedFetch, "/run", {
        method: "POST",
        body: JSON.stringify({ force_through_no_activity: !!force }),
      });
      await refresh();
    } catch (e) {
      alert(`Run failed: ${e.message}`);
    } finally {
      setBusyAction(null);
    }
  }, [authedFetch, refresh]);

  const handleBootstrapNow = useCallback(async () => {
    setBusyAction("bootstrap");
    try {
      await apiFetch(authedFetch, "/bootstrap", {
        method: "POST",
        body: JSON.stringify({}),
      });
      await refresh();
    } catch (e) {
      alert(`Bootstrap failed: ${e.message}`);
    } finally {
      setBusyAction(null);
    }
  }, [authedFetch, refresh]);

  const handleReset = useCallback(async () => {
    if (!window.confirm("Reset playbook? This wipes lattice + archived + runs. The next scheduler tick will re-bootstrap from the prose corpus.")) return;
    if (!window.confirm("Are you sure? This cannot be undone.")) return;
    setBusyAction("reset");
    try {
      await apiFetch(authedFetch, "/reset", {
        method: "POST",
        body: JSON.stringify({ confirm: "yes" }),
      });
      await refresh();
    } catch (e) {
      alert(`Reset failed: ${e.message}`);
    } finally {
      setBusyAction(null);
    }
  }, [authedFetch, refresh]);

  const grouped = useMemo(() => {
    if (!state) return BUCKETS.map(() => []);
    const out = BUCKETS.map(() => []);
    for (const s of (state.active || [])) {
      out[bucketIndex(s.weight)].push(s);
    }
    for (const arr of out) arr.sort((a, b) => sortScore(a) - sortScore(b));
    return out;
  }, [state]);

  const header = PlaybookPaneHeader({ onClose, isMaximized, onToggleMaximize });
  if (loading && !state) {
    return html`<div class="pb-pane">${header}<div class="pb-loading">Loading playbook…</div></div>`;
  }
  if (error) {
    return html`
      <div class="pb-pane">
        ${header}
        <div class="pb-error">Error: ${error}</div>
        <button class="pb-btn" onClick=${refresh}>Retry</button>
      </div>
    `;
  }
  if (!state) return html`<div class="pb-pane">${header}<div class="pb-loading">No state.</div></div>`;

  const { active = [], archived = [], runs = [], flags = {}, caps = {} } = state;
  const capPct = caps.soft ? (caps.active_count / caps.soft) * 100 : 0;

  return html`
    <div class="pb-pane">
      ${header}
      ${overrideTarget ? html`
        <${OverrideModal}
          statement=${overrideTarget}
          onClose=${() => setOverrideTarget(null)}
          onConfirm=${overrideStatement}
        />
      ` : ""}

      <!-- Header bar -->
      <div class="pb-header">
        <div class="pb-cap">
          <div class="pb-cap-label">
            ${caps.active_count} / ${caps.soft || "?"} statements
          </div>
          <div class="pb-cap-bar">
            <div class="pb-cap-fill" style=${`width: ${clamp(capPct, 0, 100)}%`}></div>
          </div>
        </div>
        <div class="pb-header-meta">
          ${flags.last_run_at
            ? html`<span>Last run ${timeAgo(flags.last_run_at)}</span>`
            : html`<span>Never run</span>`}
          ${flags.disabled ? html`<span class="pb-tag-warn">DISABLED</span>` : ""}
          ${flags.bootstrap_blocked ? html`<span class="pb-tag-err">BLOCKED</span>` : ""}
        </div>
        <div class="pb-header-actions">
          ${!flags.bootstrap_done ? html`
            <button
              class="pb-btn"
              disabled=${busyAction !== null || flags.bootstrap_blocked}
              onClick=${handleBootstrapNow}
            >${busyAction === "bootstrap" ? "Bootstrapping…" : "Bootstrap now"}</button>
          ` : html`
            <button
              class="pb-btn"
              disabled=${busyAction !== null}
              onClick=${() => handleRunNow(false)}
            >${busyAction === "run" ? "Running…" : "Run now"}</button>
            <button
              class="pb-btn pb-btn-secondary"
              disabled=${busyAction !== null}
              onClick=${() => handleRunNow(true)}
              title="Bypass the activity gate (useful when the daily window is empty)"
            >Force run</button>
          `}
        </div>
      </div>

      ${flags.bootstrap_blocked ? html`
        <div class="pb-banner pb-banner-err">
          Bootstrap blocked after 3 consecutive failures. Reset to clear and re-arm.
        </div>
      ` : ""}

      <!-- Active statements -->
      <div class="pb-section">
        ${active.length === 0 ? html`
          <div class="pb-empty">
            ${flags.bootstrap_done
              ? "Lattice is empty. The daily reflection will start populating it from observed evidence."
              : "Awaiting bootstrap. Click Bootstrap now to seed from the prose corpus, or wait for the next scheduler tick."}
          </div>
        ` : BUCKETS.map((bucket, idx) => {
          const items = grouped[idx] || [];
          if (items.length === 0) return "";
          return html`
            <div class="pb-bucket" key=${bucket.label}>
              <div class="pb-bucket-label">${bucket.label} <span class="pb-bucket-count">${items.length}</span></div>
              <div class="pb-bucket-rows">
                ${items.map((s) => html`
                  <${StatementRow}
                    key=${s.id}
                    stmt=${s}
                    onOverride=${(target) => setOverrideTarget(target)}
                  />
                `)}
              </div>
            </div>
          `;
        })}
      </div>

      <!-- Archived -->
      <details class="pb-archived">
        <summary>Archived (${archived.length})</summary>
        <div class="pb-archived-list">
          ${archived.length === 0 ? html`<div class="pb-empty">No archived statements yet.</div>` : ""}
          ${archived.map((s) => html`
            <${ArchivedRow} key=${s.id} stmt=${s} onRestore=${handleRestore} />
          `)}
        </div>
      </details>

      <!-- Recent runs -->
      <div class="pb-section">
        <h3 class="pb-section-title">Recent runs</h3>
        ${(runs || []).length === 0 ? html`<div class="pb-empty">No runs yet.</div>` : ""}
        ${(runs || []).map((row) => html`
          <${RunRow} key=${row.run_id} row=${row} />
        `)}
      </div>

      <!-- Footer -->
      <details class="pb-footer">
        <summary>Danger zone</summary>
        <div class="pb-danger">
          <button
            class="pb-btn pb-btn-danger"
            disabled=${busyAction !== null}
            onClick=${handleReset}
          >${busyAction === "reset" ? "Resetting…" : "Reset playbook"}</button>
          <div class="pb-danger-note">
            Wipes lattice + archived + runs. Next scheduler tick re-bootstraps from the prose template at <code>server/templates/app_dev_playbook.md</code>.
          </div>
        </div>
      </details>
    </div>
  `;
}


// ---------------------------------------------------------------- event router

export function createPlaybookEventRouter() {
  const subs = new Map();
  return {
    publish(ev) {
      const t = ev && ev.type;
      if (!t) return;
      const fns = subs.get(t);
      if (fns) for (const fn of fns) {
        try { fn(ev); } catch (_) { /* swallow */ }
      }
    },
    subscribe(type, fn) {
      if (!subs.has(type)) subs.set(type, new Set());
      subs.get(type).add(fn);
      return () => {
        const fns = subs.get(type);
        if (fns) { fns.delete(fn); if (fns.size === 0) subs.delete(type); }
      };
    },
  };
}

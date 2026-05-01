// Compass dashboard (harness-styled v1).
//
// Single-file Preact pane mounted under slot `__compass`. Information
// architecture follows Docs/compass-specs.md §14; visuals reuse the
// harness CSS variable set (--bg, --fg, --accent, --ok, --warn, --err)
// rather than the navigator's-logbook palette — that pass is deferred.
//
// All glyphs are CSS-drawn or inline SVG per the no-emoji rule from
// CLAUDE.md. Markdown rendering reuses the harness's marked + dompurify
// pipeline, exposed by app.js as window.__harness_renderMarkdown.

import { h } from "https://esm.sh/preact@10";
import { useState, useEffect, useCallback, useMemo, useRef } from "https://esm.sh/preact@10/hooks";
import htm from "/static/vendor/htm.js";

const html = htm.bind(h);

// ---------------------------------------------------------------- helpers

// Stable per-region color from a small palette. Same name → same color.
const REGION_PALETTE = [
  "var(--accent)", "var(--ok)", "var(--warn)", "var(--err)",
  "#a371f7", "#3fb9c5", "#d9826a", "#7ec850",
  "#c9a227", "#9c7ec8", "#5fb3a1", "#d97757",
  "#7aa6cf", "#c47fb3", "#a6c97a",
];

function regionColor(name) {
  if (!name) return "var(--muted)";
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return REGION_PALETTE[h % REGION_PALETTE.length];
}

function clamp(x, lo, hi) {
  return Math.max(lo, Math.min(hi, x));
}

function fmtWeight(w) {
  if (typeof w !== "number" || Number.isNaN(w)) return "—";
  return w.toFixed(2);
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

// Reuse harness markdown renderer if it exposed itself. Falls back to
// a basic <pre> wrap if not.
function renderBriefing(text) {
  if (!text) return "";
  if (typeof window !== "undefined" && typeof window.__harness_renderMarkdown === "function") {
    return window.__harness_renderMarkdown(text);
  }
  // Minimal fallback: HTML-escape and preserve newlines.
  const escaped = String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
  return `<pre class="cmp-md-fallback">${escaped}</pre>`;
}

// ---------------------------------------------------------------- API

async function apiFetch(authedFetch, path, opts) {
  const res = await authedFetch(`/api/compass${path}`, opts);
  if (!res.ok) {
    let body = "";
    try { body = await res.text(); } catch (_) {}
    throw new Error(`HTTP ${res.status}: ${body || res.statusText}`);
  }
  // PlainTextResponse for /briefings/{date}; everything else is JSON.
  const ct = res.headers.get("content-type") || "";
  if (ct.startsWith("application/json")) return res.json();
  return res.text();
}

// ---------------------------------------------------------------- weight bar

function WeightBar({ weight, manuallySet }) {
  const w = typeof weight === "number" ? clamp(weight, 0, 1) : 0.5;
  // Symmetric grow-from-center: left side fills toward 0 (NO), right
  // toward 1 (YES). Center tick at 50%.
  const leftPct = w < 0.5 ? (0.5 - w) * 200 : 0;
  const rightPct = w > 0.5 ? (w - 0.5) * 200 : 0;
  let zoneClass = "wb-zone-mid";
  if (w >= 0.85) zoneClass = "wb-zone-yes-strong";
  else if (w >= 0.6) zoneClass = "wb-zone-yes";
  else if (w <= 0.15) zoneClass = "wb-zone-no-strong";
  else if (w <= 0.4) zoneClass = "wb-zone-no";
  return html`
    <div class=${"cmp-weight-bar " + zoneClass} title=${`P(true) = ${fmtWeight(w)}${manuallySet ? " — manually set" : ""}`}>
      <div class="cmp-wb-track">
        <div class="cmp-wb-fill cmp-wb-no" style=${{ width: `${leftPct.toFixed(1)}%` }}></div>
        <div class="cmp-wb-fill cmp-wb-yes" style=${{ width: `${rightPct.toFixed(1)}%` }}></div>
        <div class="cmp-wb-tick"></div>
      </div>
      <span class="cmp-wb-num">${fmtWeight(w)}</span>
      ${manuallySet ? html`<span class="cmp-badge cmp-badge-manual">SET</span>` : null}
    </div>
  `;
}

function CapacityBar({ active, soft, hard }) {
  const total = Math.max(hard, 1);
  const pct = clamp((active / total) * 100, 0, 100);
  const tickPct = clamp((soft / total) * 100, 0, 100);
  let cls = "cmp-cap-ok";
  if (active > hard) cls = "cmp-cap-hard";
  else if (active > soft) cls = "cmp-cap-soft";
  return html`
    <div class=${"cmp-capacity-bar " + cls} title=${`${active} / ${hard} (soft cap ${soft})`}>
      <div class="cmp-cap-track">
        <div class="cmp-cap-fill" style=${{ width: `${pct}%` }}></div>
        <div class="cmp-cap-tick" style=${{ left: `${tickPct}%` }}></div>
      </div>
      ${active > hard
        ? html`<div class="cmp-cap-caption cmp-cap-hard-text">over hard cap — settle or retire before adding</div>`
        : active > soft
        ? html`<div class="cmp-cap-caption cmp-cap-soft-text">over soft capacity — review proposals below</div>`
        : null}
    </div>
  `;
}

// ---------------------------------------------------------------- region pill

function RegionPill({ name, count, active, onClick }) {
  const color = regionColor(name);
  const style = active
    ? { background: color, borderColor: color, color: "var(--bg)" }
    : { borderColor: color, color };
  return html`
    <button
      class=${"cmp-region-pill" + (active ? " active" : "")}
      style=${style}
      onClick=${onClick}
    >
      <span class="cmp-region-name">${name}</span>
      ${typeof count === "number" ? html`<span class="cmp-region-count">${count}</span>` : null}
    </button>
  `;
}

// ---------------------------------------------------------------- header

function CompassHeader({ enabled, running, runningPhase, qaActive, lastRunAt, onRun, onQAStart, onQAEnd, onReset }) {
  return html`
    <header class="cmp-header">
      <div class="cmp-title">
        <div class="cmp-title-kicker">TeamOfTen · world-model engine · v0.2</div>
        <div class="cmp-title-main">Compass</div>
        <div class="cmp-title-tagline">a lattice of statements, region-tagged, weight = P(true). Coach queries; only humans answer.</div>
        ${lastRunAt ? html`<div class="cmp-title-last">Last run: ${timeAgo(lastRunAt)}</div>` : null}
      </div>
      <div class="cmp-header-actions">
        <button
          class="cmp-btn cmp-btn-primary"
          disabled=${!enabled || running || qaActive}
          onClick=${onRun}
          title=${running ? `Running — ${runningPhase || "…"}` : "Trigger an on-demand Compass run"}
        >
          ${running
            ? html`<span class="cmp-pulse"></span> ${runningPhase || "running…"}`
            : html`<span class="cmp-icon cmp-icon-run"></span> Run Compass`}
        </button>
        ${qaActive
          ? html`<button class="cmp-btn cmp-btn-warn" onClick=${onQAEnd}>End Q&A</button>`
          : html`<button class="cmp-btn" disabled=${!enabled || running} onClick=${onQAStart}>Start Q&A</button>`}
        <button class="cmp-btn cmp-btn-ghost" disabled=${running} onClick=${onReset}>Reset</button>
      </div>
    </header>
  `;
}

// ---------------------------------------------------------------- enable banner

function EnableBanner({ onEnable, busy }) {
  return html`
    <div class="cmp-enable-banner">
      <div>
        <strong>Compass is disabled for this project.</strong>
        <p>Enable it to start building the lattice. Compass will run on demand and on a daily schedule when you have signals to digest.</p>
      </div>
      <button class="cmp-btn cmp-btn-primary" disabled=${busy} onClick=${onEnable}>Enable Compass</button>
    </div>
  `;
}

// ---------------------------------------------------------------- truth reference (read-only)

// Truth lives in the project's `truth/` folder, NOT in Compass. Compass
// reads it on every run (Stage 0 truth-derive seeds the lattice). The
// dashboard surfaces a small read-only summary so the human can see
// what's fed to the prompts; editing happens in the Files pane.

function TruthReference({ truth, onOpenFiles }) {
  const [expanded, setExpanded] = useState(false);
  const count = truth.length;
  return html`
    <section class="cmp-truth-ref">
      <div class="cmp-truth-ref-head">
        <div>
          <div class="cmp-section-kicker">TRUTH · READ FROM ${`<project>/truth/`} ON EVERY RUN</div>
          <div class="cmp-section-title">${count} truth ${count === 1 ? "fact" : "facts"}</div>
        </div>
        <button class="cmp-btn-mini cmp-btn-ghost" onClick=${onOpenFiles}>open Files pane</button>
      </div>
      ${count === 0
        ? html`<div class="cmp-truth-ref-empty">
            No truth files yet. Drop <code>.md</code> or <code>.txt</code> files
            into the project's <code>truth/</code> folder; Compass will derive
            initial lattice statements from them on the next run.
          </div>`
        : html`
            <button
              class="cmp-truth-ref-toggle"
              onClick=${() => setExpanded((x) => !x)}
            >
              ${expanded ? "hide" : "show"} truth corpus (${count})
            </button>
            ${expanded
              ? html`
                  <ul class="cmp-truth-ref-list">
                    ${truth.map((t) => html`
                      <li class="cmp-truth-ref-item" key=${t.index}>
                        <span class="cmp-truth-idx">T${t.index}</span>
                        <span class="cmp-truth-ref-text">${(t.text || "").slice(0, 240)}${(t.text || "").length > 240 ? "…" : ""}</span>
                      </li>
                    `)}
                  </ul>`
              : null}
          `}
    </section>
  `;
}

// ---------------------------------------------------------------- regions strip

function RegionsStrip({ regions, statements, activeFilter, onFilter, softCap, hardCap }) {
  const counts = useMemo(() => {
    const c = {};
    for (const s of statements) {
      if (s.archived) continue;
      c[s.region] = (c[s.region] || 0) + 1;
    }
    return c;
  }, [statements]);
  const activeRegions = regions.filter((r) => !r.merged_into);
  const total = statements.filter((s) => !s.archived).length;
  const overSoft = activeRegions.length > softCap;
  const overHard = activeRegions.length > hardCap;
  return html`
    <section class="cmp-regions-strip">
      <div class="cmp-regions-meta">
        <span class="cmp-section-kicker">REGIONS · ${activeRegions.length}/${softCap} (auto-merged when over)</span>
        ${overHard
          ? html`<span class="cmp-warn-text">over hard cap (${hardCap})</span>`
          : overSoft
          ? html`<span class="cmp-warn-text cmp-warn-amber">over soft cap</span>`
          : null}
      </div>
      <div class="cmp-regions-pills">
        <${RegionPill}
          name="all"
          count=${total}
          active=${!activeFilter}
          onClick=${() => onFilter(null)}
        />
        ${activeRegions.map((r) => html`
          <${RegionPill}
            key=${r.name}
            name=${r.name}
            count=${counts[r.name] || 0}
            active=${activeFilter === r.name}
            onClick=${() => onFilter(activeFilter === r.name ? null : r.name)}
          />
        `)}
      </div>
    </section>
  `;
}

// ---------------------------------------------------------------- statement row

function StatementRow({ s, onOverride, onRestore, archived = false }) {
  const negation = s.weight <= 0.2 && !archived;
  return html`
    <div class=${"cmp-stmt-row" + (s.archived ? " archived" : "") + (s.settle_proposed ? " has-proposal" : "")}>
      <div class="cmp-stmt-id">${s.id}${(s.targets_pending ? " ◇" : "")}</div>
      <div class="cmp-stmt-body">
        <div class="cmp-stmt-line">
          <${RegionPill} name=${s.region} />
          <span class="cmp-stmt-text">${s.text}</span>
          ${s.merged ? html`<span class="cmp-badge cmp-badge-merged">MERGED</span>` : null}
          ${s.reformulated ? html`<span class="cmp-badge cmp-badge-reformulated">REFORMULATED</span>` : null}
          ${s.weight >= 0.8 && !s.archived ? html`<span class="cmp-badge cmp-badge-yes">TRUE</span>` : null}
          ${s.weight <= 0.2 && !s.archived ? html`<span class="cmp-badge cmp-badge-no">FALSE</span>` : null}
          ${s.archived && s.settled_as === "yes" ? html`<span class="cmp-badge cmp-badge-yes">SETTLED · TRUE</span>` : null}
          ${s.archived && s.settled_as === "no" ? html`<span class="cmp-badge cmp-badge-no">SETTLED · FALSE</span>` : null}
          ${s.archived && s.settled_as === "merged" ? html`<span class="cmp-badge cmp-badge-merged">MERGED</span>` : null}
          ${s.archived && s.settled_as === "retired" ? html`<span class="cmp-badge cmp-badge-retired">RETIRED</span>` : null}
        </div>
        ${negation
          ? html`<div class="cmp-negation-hint">→ negation is binding: this is NOT the case</div>`
          : null}
        <div class="cmp-stmt-controls">
          <${WeightBar} weight=${s.weight} manuallySet=${s.manually_set} />
          ${!s.archived
            ? html`
                <div class="cmp-quick-buttons">
                  <button class="cmp-qbtn cmp-qbtn-no" onClick=${() => onOverride(s, 0)} title="Set weight to 0">NO</button>
                  <button class="cmp-qbtn cmp-qbtn-half" onClick=${() => onOverride(s, 0.5)} title="Set weight to 0.5">½</button>
                  <button class="cmp-qbtn cmp-qbtn-yes" onClick=${() => onOverride(s, 1)} title="Set weight to 1">YES</button>
                  <button class="cmp-qbtn" onClick=${() => onOverride(s, "custom")} title="Set custom weight">…</button>
                </div>
              `
            : html`<button class="cmp-btn-mini" onClick=${() => onRestore(s)}>RESTORE</button>`}
        </div>
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------- proposal cards

function SettleProposalCard({ p, statement, onResolve }) {
  if (!statement) return null;
  return html`
    <div class="cmp-prop-card cmp-prop-settle">
      <div class="cmp-prop-head">
        <span class="cmp-stmt-id">${statement.id}</span>
        <${RegionPill} name=${statement.region} />
        <span class="cmp-stmt-text">${statement.text}</span>
        <span class="cmp-prop-weight">${fmtWeight(statement.weight)}</span>
      </div>
      <div class="cmp-prop-q">${p.question}</div>
      ${p.reasoning ? html`<div class="cmp-prop-reasoning">${p.reasoning}</div>` : null}
      <div class="cmp-prop-actions">
        <button
          class=${"cmp-btn-mini " + (p.direction === "yes" ? "cmp-btn-ok" : "cmp-btn-danger")}
          onClick=${() => onResolve("confirm", null)}
        >
          ${p.direction === "yes" ? "Confirm YES · 1.00" : "Confirm NO · 0.00"}
        </button>
        <button
          class="cmp-btn-mini"
          onClick=${() => {
            const w = prompt("Final weight 0..1:", String(p.direction === "yes" ? 1 : 0));
            if (w === null) return;
            const n = Number(w);
            if (!Number.isFinite(n)) return alert("invalid number");
            onResolve("adjust", clamp(n, 0, 1));
          }}
        >Adjust…</button>
        <button class="cmp-btn-mini cmp-btn-ghost" onClick=${() => onResolve("reject", null)}>Reject · keep active</button>
      </div>
    </div>
  `;
}

function StaleProposalCard({ p, statement, onResolve }) {
  if (!statement) return null;
  return html`
    <div class="cmp-prop-card cmp-prop-stale">
      <div class="cmp-prop-head">
        <span class="cmp-stmt-id">${statement.id}</span>
        <${RegionPill} name=${statement.region} />
        <span class="cmp-stmt-text">${statement.text}</span>
        <span class="cmp-prop-weight">${fmtWeight(statement.weight)}</span>
      </div>
      <div class="cmp-prop-q">${p.question}</div>
      ${p.reasoning ? html`<div class="cmp-prop-reasoning">${p.reasoning}</div>` : null}
      ${p.reformulation
        ? html`<div class="cmp-prop-reform"><span class="cmp-section-kicker">SUGGESTED REFORMULATION</span><div>${p.reformulation}</div></div>`
        : null}
      <div class="cmp-prop-actions">
        <button
          class="cmp-btn-mini cmp-btn-danger"
          onClick=${() => {
            if (confirm(`Retire ${statement.id} entirely? This is destructive — the statement is removed from the active lattice and marked retired.`)) {
              onResolve("retire", null);
            }
          }}
        >Irrelevant · retire</button>
        <button class="cmp-btn-mini cmp-btn-ghost" onClick=${() => onResolve("keep", null)}>Keep · still important</button>
        ${p.reformulation
          ? html`<button class="cmp-btn-mini" onClick=${() => onResolve("reformulate", p.reformulation)}>Use suggested reformulation</button>`
          : null}
        <button
          class="cmp-btn-mini"
          onClick=${() => {
            const t = prompt("Reformulate as:", statement.text);
            if (t === null || !t.trim()) return;
            onResolve("reformulate", t.trim());
          }}
        >Rewrite…</button>
      </div>
    </div>
  `;
}

function ReconciliationProposalCard({ p, statement, onResolve }) {
  if (!statement) return null;
  const corpusList = (p.corpus_paths || []).join(", ") || "(unspecified)";
  const directional = statement.archived
    && (statement.settled_as === "yes" || statement.settled_as === "no");
  return html`
    <div class="cmp-prop-card cmp-prop-reconcile">
      <div class="cmp-prop-head">
        <span class="cmp-stmt-id">${statement.id}</span>
        ${statement.archived
          ? html`<span class="cmp-badge cmp-badge-${statement.settled_as === "yes" ? "yes" : statement.settled_as === "no" ? "no" : "merged"}">SETTLED${statement.settled_as ? ` · ${statement.settled_as.toUpperCase()}` : ""}</span>`
          : null}
        <${RegionPill} name=${statement.region} />
        <span class="cmp-stmt-text">${statement.text}</span>
        <span class="cmp-prop-weight">${fmtWeight(statement.weight)}</span>
      </div>
      <div class="cmp-prop-q">
        <span class="cmp-section-kicker">CORPUS↔LATTICE CONFLICT (from ${corpusList})</span>
      </div>
      <div class="cmp-prop-reasoning">${p.explanation}</div>
      ${p.suggested_resolution && p.suggested_resolution !== "either"
        ? html`<div class="cmp-prop-reasoning cmp-italic">
            Compass suggests: ${p.suggested_resolution.replace("_", " ")}
          </div>`
        : null}
      <div class="cmp-prop-actions">
        <button
          class="cmp-btn-mini"
          title="Lattice is wrong — un-archive at weight 0.5 so the next runs re-evaluate"
          onClick=${() => onResolve("update_lattice", { lattice_action: "unarchive", weight: 0.5 })}
        >Lattice wrong · un-archive</button>
        ${directional
          ? html`<button
              class="cmp-btn-mini"
              title="Re-settle in the opposite direction"
              onClick=${() => {
                if (confirm(`Flip the settle direction for ${statement.id}? It's currently ${statement.settled_as.toUpperCase()}; this will re-settle as the opposite.`)) {
                  onResolve("update_lattice", { lattice_action: "flip" });
                }
              }}
            >Flip settle</button>`
          : null}
        <button
          class="cmp-btn-mini"
          title="Replace the row's text with a new framing"
          onClick=${() => {
            const t = prompt("Reformulate as:", statement.text);
            if (t === null || !t.trim()) return;
            onResolve("update_lattice", { lattice_action: "reformulate", text: t.trim() });
          }}
        >Reformulate…</button>
        <button
          class="cmp-btn-mini cmp-btn-warn"
          title="Open the Files pane on the truth file(s) — the corpus is wrong / lagging"
          onClick=${() => onResolve("update_truth", { corpus_paths: p.corpus_paths })}
        >Truth wrong · edit file</button>
        <button
          class="cmp-btn-mini cmp-btn-ghost"
          title="Leave both — accept ambiguity for now"
          onClick=${() => onResolve("accept_ambiguity")}
        >Accept ambiguity</button>
      </div>
    </div>
  `;
}


function DupeProposalCard({ p, statementsById, onResolve }) {
  const losers = p.cluster_ids.map((id) => statementsById.get(id)).filter(Boolean);
  if (losers.length < 2) return null;
  return html`
    <div class="cmp-prop-card cmp-prop-dupe">
      <div class="cmp-section-kicker">WOULD MERGE</div>
      <ul class="cmp-prop-cluster">
        ${losers.map((s) => html`
          <li key=${s.id}>
            <span class="cmp-stmt-id">${s.id}</span>
            <${RegionPill} name=${s.region} />
            <span class="cmp-stmt-text">${s.text}</span>
            <span class="cmp-prop-weight">${fmtWeight(s.weight)}</span>
          </li>
        `)}
      </ul>
      <div class="cmp-section-kicker">INTO</div>
      <div class="cmp-prop-merge-target">
        <${RegionPill} name=${p.region} />
        <span class="cmp-stmt-text">${p.merged_text}</span>
        <span class="cmp-prop-weight">${fmtWeight(p.merged_weight)}</span>
      </div>
      ${p.reasoning ? html`<div class="cmp-prop-reasoning">${p.reasoning}</div>` : null}
      <div class="cmp-prop-actions">
        <button class="cmp-btn-mini cmp-btn-ok" onClick=${() => onResolve("merge")}>Merge</button>
        <button class="cmp-btn-mini cmp-btn-ghost" onClick=${() => onResolve("reject")}>Reject · keep separate</button>
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------- lattice column

function LatticeColumn({
  state, regionFilter, capacity, onOverride, onRestore,
  onResolveSettle, onResolveStale, onResolveDupe, onResolveReconcile,
}) {
  const all = state.statements || [];
  const active = all.filter((s) => !s.archived);
  const archived = all.filter((s) => s.archived);
  const filtered = regionFilter
    ? active.filter((s) => s.region === regionFilter)
    : active;
  const stmtById = useMemo(() => {
    const m = new Map();
    for (const s of all) m.set(s.id, s);
    return m;
  }, [all]);
  return html`
    <section class="cmp-col cmp-col-lattice">
      <div class="cmp-col-head">
        <h3>The Lattice</h3>
        <span class="cmp-col-counter">${filtered.length}/${capacity.soft} active${regionFilter ? ` · ${regionFilter}` : ""} · ${archived.length} archived</span>
      </div>
      <${CapacityBar} active=${active.length} soft=${capacity.soft} hard=${capacity.hard} />
      <div class="cmp-stmt-list">
        ${filtered.length === 0
          ? html`<div class="cmp-empty">No statements yet. Run a bootstrap or Q&A session.</div>`
          : filtered.map((s) => html`<${StatementRow} key=${s.id} s=${s} onOverride=${onOverride} />`)}
      </div>
      ${archived.length > 0
        ? html`
            <details class="cmp-archived-details">
              <summary>Settled & archived (${archived.length})</summary>
              <div class="cmp-stmt-list cmp-archived-list">
                ${archived.map((s) => html`<${StatementRow} key=${s.id} s=${s} onRestore=${onRestore} archived=${true} />`)}
              </div>
            </details>
          `
        : null}
      ${state.settle_proposals.length > 0
        ? html`
            <div class="cmp-prop-section">
              <div class="cmp-section-kicker">${state.settle_proposals.length} settle proposal(s) · awaiting your call</div>
              ${state.settle_proposals.map((p) => html`
                <${SettleProposalCard}
                  key=${p.statement_id}
                  p=${p}
                  statement=${stmtById.get(p.statement_id)}
                  onResolve=${(action, weight) => onResolveSettle(p.statement_id, action, weight)}
                />`)}
            </div>`
        : null}
      ${state.stale_proposals.length > 0
        ? html`
            <div class="cmp-prop-section">
              <div class="cmp-section-kicker">${state.stale_proposals.length} stale statement(s) · need triage</div>
              ${state.stale_proposals.map((p) => html`
                <${StaleProposalCard}
                  key=${p.statement_id}
                  p=${p}
                  statement=${stmtById.get(p.statement_id)}
                  onResolve=${(action, text) => onResolveStale(p.statement_id, action, text)}
                />`)}
            </div>`
        : null}
      ${state.duplicate_proposals.length > 0
        ? html`
            <div class="cmp-prop-section">
              <div class="cmp-section-kicker">${state.duplicate_proposals.length} duplicate cluster(s) · merge?</div>
              ${state.duplicate_proposals.map((p) => html`
                <${DupeProposalCard}
                  key=${p.id}
                  p=${p}
                  statementsById=${stmtById}
                  onResolve=${(action) => onResolveDupe(p.id, action)}
                />`)}
            </div>`
        : null}
      ${(state.reconciliation_proposals || []).length > 0
        ? html`
            <div class="cmp-prop-section">
              <div class="cmp-section-kicker">
                ${state.reconciliation_proposals.length} corpus↔lattice conflict(s) · resolve
              </div>
              ${state.reconciliation_proposals.map((p) => html`
                <${ReconciliationProposalCard}
                  key=${p.id}
                  p=${p}
                  statement=${stmtById.get(p.statement_id)}
                  onResolve=${(action, extra) => onResolveReconcile(p.id, action, extra)}
                />`)}
            </div>`
        : null}
    </section>
  `;
}

// ---------------------------------------------------------------- queue column

function QuestionCard({ q, onSubmit }) {
  const [revealPred, setRevealPred] = useState(false);
  const [draft, setDraft] = useState(q.answer || "");
  const [submitted, setSubmitted] = useState(Boolean(q.answer));
  const submit = async () => {
    const t = draft.trim();
    if (!t) return;
    await onSubmit(q.id, t);
    setSubmitted(true);
  };
  return html`
    <div class=${"cmp-q-card" + (q.contradicted ? " contradicted" : "") + (q.ambiguity_accepted ? " ambig" : "")}>
      <div class="cmp-q-text">${q.q}</div>
      ${q.contradicted ? html`<div class="cmp-q-warn">truth contradiction — needs resolution</div>` : null}
      ${q.ambiguity_accepted ? html`<div class="cmp-q-warn">ambiguity accepted — answer not digested</div>` : null}
      <details class="cmp-q-pred" open=${revealPred} onToggle=${(e) => setRevealPred(e.target.open)}>
        <summary>prediction & targets</summary>
        <div><span class="cmp-section-kicker">PREDICTS:</span> ${q.prediction}</div>
        <div><span class="cmp-section-kicker">TARGETS:</span> ${q.targets.join(", ") || "—"}</div>
        ${q.rationale ? html`<div class="cmp-q-rationale">${q.rationale}</div>` : null}
      </details>
      <textarea
        class="cmp-textarea"
        placeholder="answer (digested next run)…"
        value=${draft}
        onInput=${(e) => { setDraft(e.target.value); setSubmitted(false); }}
        rows=${3}
      ></textarea>
      <div class="cmp-q-actions">
        ${submitted
          ? html`<span class="cmp-q-queued">queued</span>`
          : html`<button class="cmp-btn-mini" onClick=${submit}>Submit</button>`}
      </div>
    </div>
  `;
}

function InputsAndQuestionsColumn({ state, onSubmitInput, onSubmitAnswer }) {
  const [kind, setKind] = useState("note");
  const [body, setBody] = useState("");
  const submitInput = async () => {
    const t = body.trim();
    if (!t) return;
    await onSubmitInput(kind, t);
    setBody("");
  };
  const pending = state.questions.filter((q) => !q.digested && !q.ambiguity_accepted);
  return html`
    <section class="cmp-col cmp-col-queue">
      <div class="cmp-col-head"><h3>Human inputs</h3></div>
      <div class="cmp-input-row">
        <select class="cmp-select" value=${kind} onChange=${(e) => setKind(e.target.value)}>
          <option value="chat">chat</option>
          <option value="commit">commit</option>
          <option value="note">note</option>
        </select>
        <input
          class="cmp-input"
          placeholder="record a human signal — chat, commit, note…"
          value=${body}
          onInput=${(e) => setBody(e.target.value)}
          onKeyDown=${(e) => e.key === "Enter" ? submitInput() : null}
        />
        <button class="cmp-btn-mini" onClick=${submitInput}>+</button>
      </div>
      <div class="cmp-col-head">
        <h3>Question queue</h3>
        <span class="cmp-col-counter">${pending.length} pending</span>
      </div>
      <div class="cmp-q-list">
        ${pending.length === 0
          ? html`<div class="cmp-empty">No pending questions.</div>`
          : pending.map((q) => html`<${QuestionCard} key=${q.id} q=${q} onSubmit=${onSubmitAnswer} />`)}
      </div>
    </section>
  `;
}

// ---------------------------------------------------------------- briefing column

function BriefingColumn({ state, onAsk }) {
  const [askDraft, setAskDraft] = useState("");
  const [askResp, setAskResp] = useState("");
  const [askBusy, setAskBusy] = useState(false);
  const askIt = async () => {
    const q = askDraft.trim();
    if (!q) return;
    setAskBusy(true);
    setAskResp("");
    try {
      const out = await onAsk(q);
      setAskResp(out);
    } catch (err) {
      setAskResp(`error: ${err.message || err}`);
    } finally {
      setAskBusy(false);
    }
  };
  return html`
    <section class="cmp-col cmp-col-brief">
      <div class="cmp-col-head"><h3>Daily briefing</h3></div>
      ${state.latest_briefing
        ? html`<div class="cmp-brief-card markdown" dangerouslySetInnerHTML=${{ __html: renderBriefing(state.latest_briefing) }}></div>`
        : html`<div class="cmp-empty cmp-italic">No briefing yet. Run Compass to produce one.</div>`}
      <div class="cmp-col-head"><h3>CLAUDE.md · compass block</h3></div>
      ${state.claude_md_block
        ? html`
            <div class="cmp-claudemd-card">
              <pre>${state.claude_md_block}</pre>
              <button
                class="cmp-btn-mini"
                onClick=${async () => {
                  try { await navigator.clipboard.writeText(state.claude_md_block); }
                  catch (_) {}
                }}
              >copy</button>
            </div>`
        : html`<div class="cmp-empty cmp-italic">Generated each run.</div>`}
      <div class="cmp-col-head"><h3>Ask Compass</h3><span class="cmp-col-counter">coach interrogates compass</span></div>
      <div class="cmp-ask-row">
        <input
          class="cmp-input"
          placeholder="e.g. should we build for usage or flat pricing?"
          value=${askDraft}
          onInput=${(e) => setAskDraft(e.target.value)}
          onKeyDown=${(e) => (e.key === "Enter" && (e.ctrlKey || e.metaKey)) ? askIt() : null}
        />
        <button class="cmp-btn-mini" disabled=${askBusy} onClick=${askIt}>${askBusy ? "…" : "Ask"}</button>
      </div>
      ${askResp
        ? html`<div class="cmp-ask-resp markdown" dangerouslySetInnerHTML=${{ __html: renderBriefing(askResp) }}></div>`
        : null}
    </section>
  `;
}

// ---------------------------------------------------------------- audits

function AuditsSection({ audits, onSubmit }) {
  const [draft, setDraft] = useState("");
  const [filter, setFilter] = useState(null);
  const [busy, setBusy] = useState(false);
  const counts = useMemo(() => {
    const c = { all: audits.length, aligned: 0, confident_drift: 0, uncertain_drift: 0 };
    for (const a of audits) c[a.verdict] = (c[a.verdict] || 0) + 1;
    return c;
  }, [audits]);
  const visible = filter ? audits.filter((a) => a.verdict === filter) : audits;
  const submit = async () => {
    const t = draft.trim();
    if (!t) return;
    setBusy(true);
    try {
      await onSubmit(t);
      setDraft("");
    } finally {
      setBusy(false);
    }
  };
  return html`
    <section class="cmp-audits">
      <div class="cmp-section-row">
        <div class="cmp-audits-submit">
          <h3>Work audits</h3>
          <textarea
            class="cmp-textarea cmp-audits-textarea"
            placeholder="paste a commit message, decision, or worker output for audit…"
            value=${draft}
            onInput=${(e) => setDraft(e.target.value)}
            rows=${5}
          ></textarea>
          <div class="cmp-audits-submit-row">
            <span class="cmp-italic">advisory only · audits never block work</span>
            <button class="cmp-btn-mini cmp-btn-ok" disabled=${busy} onClick=${submit}>Audit</button>
          </div>
        </div>
        <div class="cmp-audits-filter">
          <h3>Filter log</h3>
          <div class="cmp-audit-pills">
            <button class=${"cmp-audit-pill" + (!filter ? " active" : "")} onClick=${() => setFilter(null)}>ALL ${counts.all}</button>
            <button class=${"cmp-audit-pill cmp-audit-pill-ok" + (filter === "aligned" ? " active" : "")} onClick=${() => setFilter("aligned")}>ALIGNED ${counts.aligned}</button>
            <button class=${"cmp-audit-pill cmp-audit-pill-err" + (filter === "confident_drift" ? " active" : "")} onClick=${() => setFilter("confident_drift")}>CONFIDENT DRIFT ${counts.confident_drift}</button>
            <button class=${"cmp-audit-pill cmp-audit-pill-warn" + (filter === "uncertain_drift" ? " active" : "")} onClick=${() => setFilter("uncertain_drift")}>UNCERTAIN ${counts.uncertain_drift}</button>
          </div>
          <div class="cmp-italic cmp-audit-legend">
            Aligned · silent OK to coach · human not bothered.<br/>
            Confident drift · direct message to coach · human can review here.<br/>
            Uncertain · coach proceeds cautiously · question queued for human.
          </div>
        </div>
      </div>
      <div class="cmp-audits-log">
        ${visible.length === 0
          ? html`<div class="cmp-empty">No audits yet.</div>`
          : visible.slice(-30).reverse().map((a) => html`
              <article class=${"cmp-audit-card cmp-audit-card-" + a.verdict} key=${a.id}>
                <div class="cmp-audit-head">
                  <span class=${"cmp-audit-verdict cmp-audit-verdict-" + a.verdict}>${a.verdict.replace("_", " ")}</span>
                  <span class="cmp-audit-ts">${timeAgo(a.ts)}</span>
                </div>
                <div class="cmp-audit-artifact">${a.artifact}</div>
                ${a.summary ? html`<div class="cmp-audit-summary">${a.summary}</div>` : null}
                ${a.contradicting_ids?.length
                  ? html`<div class="cmp-audit-conflicts"><span class="cmp-section-kicker">CONFLICTS WITH:</span> ${a.contradicting_ids.join(", ")}</div>`
                  : null}
                ${a.message_to_coach
                  ? html`<details class="cmp-audit-msg"><summary>message to coach</summary><pre>${a.message_to_coach}</pre></details>`
                  : null}
                ${a.question_id ? html`<div class="cmp-audit-q-link">question queued for human (${a.question_id})</div>` : null}
              </article>
            `)}
      </div>
    </section>
  `;
}

// ---------------------------------------------------------------- run history

function RunHistory({ runs }) {
  if (!runs.length) return null;
  return html`
    <section class="cmp-runs">
      <div class="cmp-col-head"><h3>Run history</h3><span class="cmp-col-counter">${runs.length} run(s)</span></div>
      ${runs.slice().reverse().map((r) => html`
        <details class="cmp-run-card" key=${r.run_id}>
          <summary>
            <span class="cmp-run-ts">${timeAgo(r.started_at)}</span>
            <span class="cmp-run-mode">${r.mode}</span>
            ${r.skipped
              ? html`<span class="cmp-run-skipped">skipped: ${r.skipped_reason}</span>`
              : html`<span class="cmp-run-summary">${r.passive?.summary || (r.completed ? "completed" : "incomplete")}</span>`}
          </summary>
          <div class="cmp-run-body">
            <div>passive updates: ${r.passive?.updates ?? 0}</div>
            <div>answered: ${r.answered_questions} · contradictions: ${r.contradictions}</div>
            <div>generated: ${r.questions_generated} · settle ${r.settle_proposed} · stale ${r.stale_proposed} · dupe ${r.dupe_proposed}</div>
            ${r.region_merges?.length
              ? html`<div class="cmp-run-merges">region merges: ${r.region_merges.map((m) => html`<span>${m.from?.join(",")} → ${m.to}</span>`)}</div>`
              : null}
            ${r.briefing_path ? html`<div>briefing: ${r.briefing_path}</div>` : null}
            ${r.truth_candidates?.length ? html`<div>truth candidates: ${r.truth_candidates.join("; ")}</div>` : null}
          </div>
        </details>
      `)}
    </section>
  `;
}

// ---------------------------------------------------------------- modals

function OverrideModal({ statement, value, onCancel, onConfirm }) {
  if (!statement) return null;
  const final = typeof value === "number"
    ? clamp(value, 0, 1)
    : (() => {
        const v = prompt("Custom weight 0..1:", String(statement.weight));
        if (v === null) return null;
        const n = Number(v);
        return Number.isFinite(n) ? clamp(n, 0, 1) : null;
      })();
  if (final === null) {
    // Fall back: cancel after a custom-prompt cancel.
    setTimeout(onCancel, 0);
    return null;
  }
  return html`
    <div class="cmp-modal-back" onClick=${onCancel}>
      <div class="cmp-modal" onClick=${(e) => e.stopPropagation()}>
        <div class="cmp-section-kicker">CONFIRM MANUAL OVERRIDE</div>
        <h2>Set weight directly?</h2>
        <div class="cmp-modal-stmt">${statement.text}</div>
        <div class="cmp-modal-cmp">
          <div><span class="cmp-section-kicker">CURRENT</span><strong>${fmtWeight(statement.weight)}</strong></div>
          <span class="cmp-modal-arrow">→</span>
          <div><span class="cmp-section-kicker">NEW</span><strong>${fmtWeight(final)}</strong></div>
        </div>
        <div class="cmp-italic">Compass will continue updating this weight from future evidence unless you override again. Settle/archive requires a separate confirmation.</div>
        <div class="cmp-modal-actions">
          <button class="cmp-btn cmp-btn-ghost" onClick=${onCancel}>Cancel</button>
          <button class="cmp-btn cmp-btn-primary" onClick=${() => onConfirm(final)}>Confirm</button>
        </div>
      </div>
    </div>
  `;
}

function TruthConflictModal({ conflict, onAmendAnswer, onAmendTruth, onLeaveBoth, onCancel }) {
  if (!conflict) return null;
  return html`
    <div class="cmp-modal-back" onClick=${onCancel}>
      <div class="cmp-modal cmp-modal-warn" onClick=${(e) => e.stopPropagation()}>
        <div class="cmp-section-kicker cmp-warn-text">TRUTH CONTRADICTION · DIGEST HALTED</div>
        <h2>Your answer conflicts with protected truth</h2>
        <div class="cmp-modal-q"><span class="cmp-section-kicker">QUESTION</span> ${conflict.question}</div>
        <div class="cmp-modal-q"><span class="cmp-section-kicker">YOUR ANSWER</span> ${conflict.answer}</div>
        <div class="cmp-modal-conflicts">
          <div class="cmp-section-kicker">CONFLICTS</div>
          ${(conflict.conflicts || []).map((c) => html`
            <div class="cmp-modal-conflict">
              <strong>T${c.truth_index}</strong>: ${c.explanation}
            </div>
          `)}
          ${conflict.summary ? html`<div class="cmp-italic">${conflict.summary}</div>` : null}
        </div>
        <div class="cmp-modal-paths">
          <button class="cmp-btn cmp-btn-primary" onClick=${onAmendAnswer}>1 · Amend answer — I misspoke</button>
          <button class="cmp-btn cmp-btn-warn" onClick=${onAmendTruth}>2 · Amend truth — the protected fact is outdated</button>
          <button class="cmp-btn cmp-btn-ghost" onClick=${onLeaveBoth}>3 · Leave both — accept ambiguity</button>
        </div>
      </div>
    </div>
  `;
}

// ---------------------------------------------------------------- Q&A overlay

function QASessionOverlay({ active, busy, question, answeredCount, warn, onSubmit, onSkip, onEnd }) {
  const [draft, setDraft] = useState("");
  const [revealPred, setRevealPred] = useState(false);
  const taRef = useRef(null);
  useEffect(() => {
    setDraft("");
    setRevealPred(false);
    if (taRef.current) taRef.current.focus();
  }, [question?.id]);
  if (!active) return null;
  const submit = async () => {
    const t = draft.trim();
    if (!t) return;
    await onSubmit(t);
  };
  return html`
    <div class="cmp-qa-overlay">
      <div class="cmp-qa-head">
        <span class="cmp-section-kicker">Q&A SESSION · LIVE</span>
        <span class="cmp-qa-count">${answeredCount} answered${warn ? " · approaching cap" : ""}</span>
        <button class="cmp-btn-mini cmp-btn-ghost" onClick=${onEnd}>End</button>
      </div>
      ${busy
        ? html`<div class="cmp-qa-busy"><span class="cmp-pulse"></span> selecting next question…</div>`
        : !question
        ? html`<div class="cmp-qa-empty cmp-italic">Q&A session active — click "Next" to fetch a question.</div>`
        : html`
            <div class="cmp-qa-q">${question.q}</div>
            <div class="cmp-qa-targets">targets: ${question.targets?.join(", ") || "—"}</div>
            <button class="cmp-btn-mini cmp-btn-ghost" onClick=${() => setRevealPred(!revealPred)}>
              ${revealPred ? "hide compass prediction" : "reveal compass prediction"}
            </button>
            ${revealPred
              ? html`
                  <div class="cmp-qa-pred">
                    <span class="cmp-section-kicker">PREDICTS:</span> ${question.prediction}
                    ${question.rationale ? html`<div class="cmp-italic">${question.rationale}</div>` : null}
                  </div>`
              : null}
            <textarea
              ref=${taRef}
              class="cmp-textarea cmp-qa-textarea"
              placeholder="answer (Ctrl/Cmd+Enter to submit)…"
              value=${draft}
              onInput=${(e) => setDraft(e.target.value)}
              onKeyDown=${(e) => (e.key === "Enter" && (e.ctrlKey || e.metaKey)) ? submit() : null}
              rows=${4}
            ></textarea>
            <div class="cmp-qa-actions">
              <button class="cmp-btn cmp-btn-ghost" onClick=${onSkip}>Skip</button>
              <button class="cmp-btn cmp-btn-primary" onClick=${submit}>Submit</button>
            </div>
          `}
    </div>
  `;
}

// ---------------------------------------------------------------- main pane

const CAPACITY = { soft: 50, hard: 70, regionSoft: 15, regionHard: 20 };

export function CompassPane({ slot, authedFetch, onClose, onDropEdge, onPopOut, stacked, isMaximized, onToggleMaximize, compassEvents }) {
  const [state, setState] = useState(null);
  const [enabled, setEnabled] = useState(null);
  const [running, setRunning] = useState(false);
  const [phase, setPhase] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [regionFilter, setRegionFilter] = useState(null);

  // Override modal state.
  const [override, setOverride] = useState(null);

  // Truth conflict modal.
  const [truthConflict, setTruthConflict] = useState(null);
  // QA state.
  const [qaActive, setQaActive] = useState(false);
  const [qaBusy, setQaBusy] = useState(false);
  const [qaQuestion, setQaQuestion] = useState(null);
  const [qaAnswered, setQaAnswered] = useState(0);
  const [qaWarn, setQaWarn] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const data = await apiFetch(authedFetch, "/state");
      if (data && typeof data === "object") {
        if (data.enabled === false) {
          setEnabled(false);
          setState(null);
        } else {
          setEnabled(true);
          setState(data);
          setRunning(Boolean(data.running));
          setQaActive(Boolean(data.qa_active));
        }
      }
      setError(null);
    } catch (err) {
      setError(err.message || String(err));
    }
  }, [authedFetch]);

  useEffect(() => {
    refresh();
    // Heartbeat each minute while pane is open — counts as presence
    // for daily-run gating.
    const id = setInterval(() => {
      apiFetch(authedFetch, "/heartbeat", { method: "POST" }).catch(() => {});
    }, 60_000);
    return () => clearInterval(id);
  }, [refresh, authedFetch]);

  // Listen for compass_* WS events forwarded from app.js.
  useEffect(() => {
    if (!compassEvents) return;
    const sub = compassEvents.subscribe((evt) => {
      if (!evt || typeof evt !== "object") return;
      if (evt.type === "compass_phase") {
        setRunning(true);
        setPhase(evt.phase);
        return;
      }
      if (evt.type === "compass_run_completed") {
        setRunning(false);
        setPhase(null);
        refresh();
        return;
      }
      if (evt.type === "compass_truth_contradiction") {
        // Surface modal for the dashboard's queued-answer flow.
        // Q&A path surfaces inline already.
        const q = (state?.questions || []).find((q) => q.id === evt.question_id);
        setTruthConflict({
          question_id: evt.question_id,
          question: q?.q || "",
          answer: q?.answer || "",
          conflicts: evt.conflicts || [],
          summary: evt.summary || "",
        });
        return;
      }
      if (evt.type?.startsWith("compass_")) {
        // Generic refresh for question_queued, question_digested,
        // proposal_resolved, audit_logged, truth_changed, reset, etc.
        refresh();
      }
    });
    return sub;
  }, [compassEvents, refresh, state]);

  // -------------------------------- callbacks

  const enable = useCallback(async () => {
    setBusy(true);
    try {
      await apiFetch(authedFetch, "/enable", { method: "POST" });
      await refresh();
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }, [authedFetch, refresh]);

  const triggerRun = useCallback(async () => {
    setBusy(true);
    setPhase("starting…");
    try {
      const mode = state?.runs?.length ? "on_demand" : "bootstrap";
      await apiFetch(authedFetch, "/run", { method: "POST", body: JSON.stringify({ mode }) });
    } catch (err) {
      setError(err.message);
      setBusy(false);
      setPhase(null);
    }
    // running flips off via compass_run_completed event handler
    setBusy(false);
  }, [authedFetch, state]);

  const reset = useCallback(async () => {
    if (!confirm("Reset Compass for this project? This wipes the lattice, truth, regions, questions, audits, runs, briefings — locally and on kDrive. Cannot be undone.")) {
      return;
    }
    setBusy(true);
    try {
      await apiFetch(authedFetch, "/reset", { method: "POST", body: JSON.stringify({ confirm: true }) });
      await refresh();
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  }, [authedFetch, refresh]);

  const submitOverride = useCallback(async (final) => {
    if (!override) return;
    try {
      await apiFetch(authedFetch, `/statements/${override.id}/weight`, {
        method: "POST",
        body: JSON.stringify({ weight: final, confirm: true }),
      });
      setOverride(null);
      await refresh();
    } catch (err) { setError(err.message); }
  }, [override, authedFetch, refresh]);

  const onOverrideClick = useCallback((s, value) => {
    if (value === "custom") {
      setOverride({ ...s, requested: "custom" });
    } else {
      setOverride({ ...s, requested: value });
    }
  }, []);

  const onRestoreClick = useCallback(async (s) => {
    try {
      await apiFetch(authedFetch, `/statements/${s.id}/restore`, { method: "POST" });
      await refresh();
    } catch (err) { setError(err.message); }
  }, [authedFetch, refresh]);

  const resolveSettle = useCallback(async (sid, action, weight) => {
    try {
      const body = action === "adjust" ? { action, weight } : { action };
      await apiFetch(authedFetch, `/proposals/settle/${sid}`, {
        method: "POST", body: JSON.stringify(body),
      });
      await refresh();
    } catch (err) { setError(err.message); }
  }, [authedFetch, refresh]);

  const resolveStale = useCallback(async (sid, action, text) => {
    try {
      const body = action === "reformulate" ? { action, text } : { action };
      await apiFetch(authedFetch, `/proposals/stale/${sid}`, {
        method: "POST", body: JSON.stringify(body),
      });
      await refresh();
    } catch (err) { setError(err.message); }
  }, [authedFetch, refresh]);

  const resolveDupe = useCallback(async (pid, action) => {
    try {
      await apiFetch(authedFetch, `/proposals/dupe/${pid}`, {
        method: "POST", body: JSON.stringify({ action }),
      });
      await refresh();
    } catch (err) { setError(err.message); }
  }, [authedFetch, refresh]);

  const resolveReconcile = useCallback(async (pid, action, extra) => {
    try {
      // "update_truth" is informational — open the Files pane on the
      // first cited corpus path, then close the proposal so it stops
      // re-displaying. The actual edit happens via the Files pane's
      // existing flow.
      if (action === "update_truth") {
        const path = (extra?.corpus_paths || [])[0];
        if (path) {
          const a = document.createElement("a");
          a.setAttribute(
            "data-harness-path",
            "/data/projects/" + (state?.project_id || "") + "/truth/" + path,
          );
          a.setAttribute("href", "#");
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
        }
      }
      const body = { action, ...(extra || {}) };
      await apiFetch(authedFetch, `/proposals/reconcile/${pid}`, {
        method: "POST", body: JSON.stringify(body),
      });
      await refresh();
    } catch (err) { setError(err.message); }
  }, [authedFetch, refresh, state]);

  const submitInput = useCallback(async (kind, body) => {
    try {
      await apiFetch(authedFetch, "/inputs", {
        method: "POST", body: JSON.stringify({ kind, body }),
      });
      await refresh();
    } catch (err) { setError(err.message); }
  }, [authedFetch, refresh]);

  const submitAnswer = useCallback(async (qid, answer) => {
    try {
      await apiFetch(authedFetch, `/questions/${qid}/answer`, {
        method: "POST", body: JSON.stringify({ answer }),
      });
      await refresh();
    } catch (err) { setError(err.message); }
  }, [authedFetch, refresh]);

  // Truth has no mutation callbacks — it's owned by the project's
  // `truth/` folder (edited via the Files pane, proposed by Coach via
  // `coord_propose_file_write(scope='truth', ...)`). Compass reads it
  // on every run.

  const ask = useCallback(async (query) => {
    // Hits /api/compass/ask — same prompt pipeline that backs the
    // compass_ask MCP tool, so this panel really shows the human
    // what Coach sees.
    const out = await apiFetch(authedFetch, "/ask", {
      method: "POST", body: JSON.stringify({ query }),
    });
    return out.answer || "";
  }, [authedFetch]);

  const submitAudit = useCallback(async (artifact) => {
    try {
      await apiFetch(authedFetch, "/audit", {
        method: "POST", body: JSON.stringify({ artifact }),
      });
      await refresh();
    } catch (err) { setError(err.message); }
  }, [authedFetch, refresh]);

  // QA flow.
  const qaStart = useCallback(async () => {
    setQaBusy(true);
    try {
      await apiFetch(authedFetch, "/qa/start", { method: "POST" });
      setQaActive(true);
      const next = await apiFetch(authedFetch, "/qa/next", { method: "POST" });
      setQaQuestion(next.q ? next : null);
      setQaAnswered(next.answered_count || 0);
    } catch (err) { setError(err.message); }
    finally { setQaBusy(false); }
  }, [authedFetch]);

  const qaSubmit = useCallback(async (answer) => {
    setQaBusy(true);
    try {
      const out = await apiFetch(authedFetch, "/qa/answer", {
        method: "POST", body: JSON.stringify({ answer }),
      });
      if (out.contradiction) {
        setTruthConflict({
          question_id: qaQuestion?.id,
          question: qaQuestion?.q || "",
          answer,
          conflicts: out.conflicts || [],
          summary: out.summary || "",
        });
      } else {
        setQaAnswered(out.answered_count);
        setQaWarn(Boolean(out.warn_after));
        await refresh();
        const next = await apiFetch(authedFetch, "/qa/next", { method: "POST" });
        setQaQuestion(next.q ? next : null);
      }
    } catch (err) { setError(err.message); }
    finally { setQaBusy(false); }
  }, [authedFetch, qaQuestion, refresh]);

  const qaSkip = useCallback(async () => {
    setQaBusy(true);
    try {
      const next = await apiFetch(authedFetch, "/qa/next", { method: "POST" });
      setQaQuestion(next.q ? next : null);
    } catch (err) { setError(err.message); }
    finally { setQaBusy(false); }
  }, [authedFetch]);

  const qaEnd = useCallback(async () => {
    try {
      await apiFetch(authedFetch, "/qa/end", { method: "POST" });
    } catch (_) {}
    setQaActive(false);
    setQaQuestion(null);
  }, [authedFetch]);

  // Truth conflict modal handlers.
  const conflictAmendAnswer = useCallback(() => {
    const newAnswer = prompt("Restate your answer:", truthConflict?.answer || "");
    if (newAnswer === null || !newAnswer.trim()) return;
    setTruthConflict(null);
    if (qaActive) {
      qaSubmit(newAnswer.trim());
    } else if (truthConflict?.question_id) {
      submitAnswer(truthConflict.question_id, newAnswer.trim());
    }
  }, [truthConflict, qaActive, qaSubmit, submitAnswer]);

  const conflictAmendTruth = useCallback(async () => {
    if (!truthConflict?.conflicts?.length) return;
    // Truth is owned by the project's truth/ folder, not by Compass.
    // The "amend truth" path can't write directly anymore; instead
    // we surface the file path so the human knows which truth file
    // to edit (via the Files pane or any text editor with kDrive
    // sync). Fetch the current truth list to map index → path.
    const idx = truthConflict.conflicts[0].truth_index;
    let path = null;
    try {
      const tr = await apiFetch(authedFetch, "/truth");
      const fact = (tr.facts || []).find((f) => f.index === idx);
      path = fact?.path || null;
    } catch (_) {}
    alert(
      `Truth T${idx} — to amend, edit the file directly:\n\n` +
      (path
        ? `<project>/truth/${path}\n\nThe Files pane lets you edit it. ` +
          `Compass re-reads truth on every run, so the next run picks ` +
          `up the change.`
        : `(file path unavailable; truth corpus may have shifted).`)
    );
    setTruthConflict(null);
  }, [truthConflict, authedFetch]);

  const conflictLeaveBoth = useCallback(async () => {
    // Mark question as ambiguity_accepted via the API: the simplest
    // path is to PATCH/PUT the question, but we don't have such an
    // endpoint yet. For v1 we just dismiss locally — the question
    // stays in the queue with `contradicted=true`, the user can come
    // back to it. Future improvement: dedicated endpoint.
    setTruthConflict(null);
  }, []);

  // -------------------------------- render

  const errorBanner = error
    ? html`<div class="cmp-error" onClick=${() => setError(null)}>${error} (click to dismiss)</div>`
    : null;

  if (enabled === null) {
    return html`
      <article class=${"pane cmp-pane" + (stacked ? " stacked" : "")}>
        ${PaneHeader({ slot, onClose, onDropEdge, onPopOut, stacked, isMaximized, onToggleMaximize })}
        <div class="cmp-loading">loading…</div>
      </article>
    `;
  }

  if (enabled === false) {
    return html`
      <article class=${"pane cmp-pane" + (stacked ? " stacked" : "")}>
        ${PaneHeader({ slot, onClose, onDropEdge, onPopOut, stacked, isMaximized, onToggleMaximize })}
        ${errorBanner}
        <${EnableBanner} onEnable=${enable} busy=${busy} />
      </article>
    `;
  }

  if (!state) {
    return html`
      <article class=${"pane cmp-pane" + (stacked ? " stacked" : "")}>
        ${PaneHeader({ slot, onClose, onDropEdge, onPopOut, stacked, isMaximized, onToggleMaximize })}
        ${errorBanner}
        <div class="cmp-loading">loading state…</div>
      </article>
    `;
  }

  const lastRun = state.runs?.length ? state.runs[state.runs.length - 1].finished_at : null;

  return html`
    <article class=${"pane cmp-pane" + (stacked ? " stacked" : "")}>
      ${PaneHeader({ slot, onClose, onDropEdge, onPopOut, stacked, isMaximized, onToggleMaximize })}
      ${errorBanner}
      <div class="cmp-body">
        <${CompassHeader}
          enabled=${enabled}
          running=${running}
          runningPhase=${phase}
          qaActive=${qaActive}
          lastRunAt=${lastRun}
          onRun=${triggerRun}
          onQAStart=${qaStart}
          onQAEnd=${qaEnd}
          onReset=${reset}
        />
        <${TruthReference}
          truth=${state.truth}
          onOpenFiles=${() => {
            // The Files pane handles in-app file links via
            // pendingFileOpen + a custom MIME drop. We don't have a
            // reference to its setter from inside the Compass pane,
            // so we just emit a synthetic click on a known root path
            // — the Files pane's document-level click listener picks
            // it up (see app.js handler that listens for
            // [data-harness-path] anchors).
            const a = document.createElement("a");
            a.setAttribute("data-harness-path", "/data/projects/" + (state.project_id || "") + "/truth");
            a.setAttribute("href", "#");
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
          }}
        />
        <${RegionsStrip}
          regions=${state.regions}
          statements=${state.statements}
          activeFilter=${regionFilter}
          onFilter=${setRegionFilter}
          softCap=${CAPACITY.regionSoft}
          hardCap=${CAPACITY.regionHard}
        />
        <div class="cmp-3col">
          <${LatticeColumn}
            state=${state}
            regionFilter=${regionFilter}
            capacity=${CAPACITY}
            onOverride=${onOverrideClick}
            onRestore=${onRestoreClick}
            onResolveSettle=${resolveSettle}
            onResolveStale=${resolveStale}
            onResolveDupe=${resolveDupe}
            onResolveReconcile=${resolveReconcile}
          />
          <${InputsAndQuestionsColumn}
            state=${state}
            onSubmitInput=${submitInput}
            onSubmitAnswer=${submitAnswer}
          />
          <${BriefingColumn}
            state=${state}
            onAsk=${ask}
          />
        </div>
        <${AuditsSection} audits=${state.audits} onSubmit=${submitAudit} />
        <${RunHistory} runs=${state.runs} />
      </div>
      <${OverrideModal}
        statement=${override?.requested === "custom" ? null : override}
        value=${override?.requested}
        onCancel=${() => setOverride(null)}
        onConfirm=${submitOverride}
      />
      <${TruthConflictModal}
        conflict=${truthConflict}
        onAmendAnswer=${conflictAmendAnswer}
        onAmendTruth=${conflictAmendTruth}
        onLeaveBoth=${conflictLeaveBoth}
        onCancel=${() => setTruthConflict(null)}
      />
      <${QASessionOverlay}
        active=${qaActive}
        busy=${qaBusy}
        question=${qaQuestion}
        answeredCount=${qaAnswered}
        warn=${qaWarn}
        onSubmit=${qaSubmit}
        onSkip=${qaSkip}
        onEnd=${qaEnd}
      />
    </article>
  `;
}

// ---------------------------------------------------------------- pane header

function PaneHeader({ slot, onClose, onDropEdge, onPopOut, stacked, isMaximized, onToggleMaximize }) {
  return html`
    <header class="pane-head cmp-pane-head">
      <div class="pane-head-label">
        <span class="cmp-pane-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="18" height="18">
            <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.5"/>
            <path d="M12 4 L13.4 12 L12 20 L10.6 12 Z" fill="currentColor" stroke="none"/>
            <path d="M4 12 L12 10.6 L20 12 L12 13.4 Z" fill="currentColor" stroke="none" opacity="0.6"/>
          </svg>
        </span>
        <span class="pane-head-slot">Compass</span>
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

// ---------------------------------------------------------------- event router

// Lightweight pub/sub for compass_* events forwarded by app.js. The
// pane subscribes and gets an unsubscribe fn back. Multiple panes can
// subscribe; events fan out to all.
export function createCompassEventRouter() {
  const subs = new Set();
  return {
    publish(evt) {
      for (const cb of [...subs]) {
        try { cb(evt); } catch (_) {}
      }
    },
    subscribe(cb) {
      subs.add(cb);
      return () => subs.delete(cb);
    },
  };
}

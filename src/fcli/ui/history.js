// What changed: a timeline of the kept history, and the changes themselves, newest first.

import { html, useEffect, useMemo, useRef, useState } from "./vendor/preact-htm.module.js";
import { clock, follow, get, natural, withQuery } from "./api.js";
import { Change } from "./incidents.js";

const WINDOWS = [
  ["15m", 15 * 60],
  ["1h", 3600],
  ["6h", 6 * 3600],
  ["24h", 86400],
  ["7d", 7 * 86400],
];
const LIMIT = 3000;
const BUCKETS = 60;
const PAGE = 300;
const SETTLE_MS = 3000;

const kindOf = (c) =>
  c.report === "checks"
    ? c.op === "add" ? "raised" : "cleared"
    : c.report === "config-commits" ? "commit" : c.origin === "config" ? "config" : "change";
const KINDS = ["raised", "cleared", "commit", "config", "change"];
const KIND_LABEL = {
  raised: "finding raised",
  cleared: "finding cleared",
  commit: "commit",
  config: "change after a commit",
  change: "unplanned change",
};
/** Configuration commits and what they caused, or what the network did on its own. */
const ORIGINS = [
  ["", "everything"],
  ["config", "config"],
  ["unplanned", "unplanned"],
];

function Timeline({ changes, from, to, range, onRange }) {
  const width = 1000;
  const height = 90;
  const step = (to - from) / BUCKETS;
  const buckets = useMemo(() => {
    const out = Array.from({ length: BUCKETS }, () => Object.fromEntries(KINDS.map((k) => [k, 0])));
    for (const c of changes) {
      const i = Math.min(BUCKETS - 1, Math.max(0, Math.floor((c.ts - from) / step)));
      out[i][kindOf(c)] += 1;
    }
    return out;
  }, [changes, from, step]);
  const peak = Math.max(1, ...buckets.map((b) => KINDS.reduce((s, k) => s + b[k], 0)));
  const bar = width / BUCKETS;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => ({ ts: from + f * (to - from) }));
  return html`<div class="timeline"><svg viewBox=${`0 0 ${width} ${height}`} preserveAspectRatio="none">
    ${buckets.map((b, i) => {
      let y = height;
      const start = from + i * step;
      const picked = range && range[0] <= start && start < range[1];
      return html`<g class=${`bucket${picked ? " picked" : ""}`} onClick=${() => onRange(picked ? null : [start, start + step])}>
        <rect class="hit" x=${i * bar} y="0" width=${bar} height=${height} />
        ${KINDS.map((k) => {
          // A square root: the burst of the engine's first sync must not flatten every other bar.
          const total = KINDS.reduce((s, x) => s + b[x], 0);
          const h = total ? (b[k] / total) * Math.sqrt(total / peak) * (height - 6) : 0;
          y -= h;
          return h > 0 && html`<rect class=${`k-${k}`} x=${i * bar + 1} y=${y} width=${bar - 2} height=${h} />`;
        })}
        <title>${clock(start)}: ${KINDS.filter((k) => b[k]).map((k) => `${b[k]} ${KIND_LABEL[k]}`).join(", ") || "nothing"}</title>
      </g>`;
    })}
  </svg>
  <div class="ticks">${ticks.map((t) => html`<span>${clock(t.ts)}</span>`)}</div></div>`;
}

export function HistoryView({ query, go, graph, catalog }) {
  const span = WINDOWS.find(([w]) => w === query.since) ?? WINDOWS[1];
  const report = query.report || "";
  const node = query.node || "";
  const origin = ORIGINS.some(([o]) => o === query.origin) ? query.origin : "";
  const [state, setState] = useState({ changes: null, error: "", to: Date.now() / 1000 });
  const [range, setRange] = useState(null);
  const [shown, setShown] = useState(PAGE);
  const pending = useRef(null);
  const set = (change) => go(withQuery("history", { since: span[0], report, node, origin, ...change }));

  useEffect(() => {
    let current = true;
    const load = () =>
      get("/api/history", { since: span[0], report, node, limit: LIMIT })
        .then((changes) => current && setState({ changes, error: "", to: Date.now() / 1000 }))
        .catch((e) => current && setState({ changes: null, error: e.message, to: Date.now() / 1000 }));
    load();
    setRange(null);
    setShown(PAGE);
    const stop = follow(report ? [report] : [], (type, event) => {
      if (type !== "change" || event.op === "update" || pending.current) return;
      if (node && event.node !== node) return;
      pending.current = setTimeout(() => {
        pending.current = null;
        load();
      }, SETTLE_MS);
    }, () => {});
    return () => {
      current = false;
      stop();
      clearTimeout(pending.current);
      pending.current = null;
    };
  }, [span[0], report, node]);

  const all = state.changes ?? [];
  const changes = origin ? all.filter((c) => c.origin === origin) : all;
  const listed = (range ? changes.filter((c) => range[0] <= c.ts && c.ts < range[1]) : changes).slice().reverse();
  const configured = all.filter((c) => c.origin === "config").length;
  const reports = ["checks", "config-commits", ...catalog.filter((r) => !r.hidden).map((r) => r.name).filter((n) => n !== "config-commits").sort(natural)];
  return html`<div class="history-view">
    <div class="view-head">
      <h2>History</h2>
      <span class="muted">every change the engine kept: configuration commits and what they caused, apart from what the network did on its own</span>
    </div>
    <div class="filters">
      <div class="segmented">${WINDOWS.map(
        ([w]) => html`<button class=${w === span[0] ? "on" : ""} onClick=${() => set({ since: w })}>${w}</button>`,
      )}</div>
      <div class="segmented" title="Configuration commits and the changes put down to one, or the rest">${ORIGINS.map(
        ([o, name]) => html`<button class=${o === origin ? "on" : ""} onClick=${() => set({ origin: o })}>${
          o === "config" ? html`<span class="tag-config">CONFIG</span>` : name
        }</button>`,
      )}</div>
      <select value=${report} onChange=${(e) => set({ report: e.target.value })} aria-label="Report">
        <option value="">every report</option>
        ${reports.map((r) => html`<option value=${r}>${r === "checks" ? "findings (checks)" : r}</option>`)}
      </select>
      <select value=${node} onChange=${(e) => set({ node: e.target.value })} aria-label="Node">
        <option value="">every node</option>
        ${graph.nodes.map((n) => n.name).sort(natural).map((n) => html`<option value=${n}>${n}</option>`)}
      </select>
      <span class="legend-inline">
        ${KINDS.map((k) => html`<span><i class=${`sw k-${k}`}></i>${KIND_LABEL[k]}</span>`)}
      </span>
    </div>
    ${state.error && html`<p class="error-text">${state.error}</p>`}
    ${state.changes &&
    html`<${Timeline} changes=${changes} from=${state.to - span[1]} to=${state.to} range=${range} onRange=${setRange} />
      <div class="summary-line">
        <span class="muted">${all.length >= LIMIT ? `the latest ${LIMIT} changes` : `${all.length} changes`} in the last ${span[0]}:${" "}${configured} configuration-related, ${all.length - configured} unplanned${origin ? ` — showing the ${origin === "config" ? "configuration-related" : "unplanned"} ones` : ""}</span>
        ${range && html`<span class="chip">${clock(range[0])} – ${clock(range[1])} <a onClick=${() => setRange(null)}>✕</a></span>`}
      </div>
      ${listed.length
        ? html`<ul class="changes">${listed.slice(0, shown).map((c) => html`<${Change} key=${c.seq} change=${c} />`)}</ul>`
        : html`<p class="muted">Nothing changed${range ? " then" : ""}.</p>`}
      ${listed.length > shown && html`<button class="more" onClick=${() => setShown(shown + PAGE)}>Show more</button>`}`}
    ${!state.changes && !state.error && html`<p class="muted">Reading the history…</p>`}
  </div>`;
}

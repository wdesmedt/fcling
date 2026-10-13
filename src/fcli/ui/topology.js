// The topology: the fabric laid out by tier, with cables, clients and health; and what a click shows.

import { html, useEffect, useMemo, useRef, useState } from "./vendor/preact-htm.module.js";
import { ago, bySeverity, get, natural } from "./api.js";

const TIERS = ["core", "gateway", "spine", "leaf", "unclassified"];
/** A node's box. Its height is set by each layout: three lines, a fourth where the fabric has IPv6. */
const NODE = { w: 150, h: 52 };
const LINE = 13;
const CLIENT = { w: 118, h: 30 };
const GAP = 28;
const CLIENT_GAP = 14;
const ROW = 130;
const PAD = 40;
const LABEL = 84;

const rowWidth = (n, w, gap) => n * w + Math.max(0, n - 1) * gap;

/** Where everything goes: one block per component, a row per tier, clients under the nodes they hang off. */
export function layout(graph) {
  NODE.h = graph.nodes.some((n) => n.ipv6) ? 52 + LINE : 52;
  const tierOf = new Map(graph.nodes.map((n) => [n.name, n.tier]));
  const nodes = new Map();
  const clients = new Map();
  const labels = [];
  const titles = [];
  const multi = graph.components.length > 1;
  // Room above the first row: cables along it arc upwards, and a path marks its nodes from above.
  let y = PAD + 36;
  let width = 0;
  for (const component of graph.components) {
    const members = new Set(component.nodes);
    if (multi) {
      titles.push({ name: component.name, y: y + 4 });
      y += 40;
    }
    const rows = TIERS.map((tier) => ({
      tier,
      names: component.nodes.filter((n) => tierOf.get(n) === tier).sort(natural),
    })).filter((r) => r.names.length);
    const inner = Math.max(...rows.map((r) => rowWidth(r.names.length, NODE.w, GAP)));
    const placed = new Map();
    rows.forEach((row, i) => {
      const start = (inner - rowWidth(row.names.length, NODE.w, GAP)) / 2;
      row.names.forEach((name, j) => placed.set(name, { x: start + j * (NODE.w + GAP) + NODE.w / 2, y: y + i * ROW }));
      labels.push({ text: row.tier, y: y + i * ROW });
    });
    y += (rows.length - 1) * ROW;

    // Clients under the mean of the nodes they are attached to, pushed apart where they collide.
    const own = graph.clients
      .filter((c) => c.nodes.some((n) => members.has(n)))
      .map((c) => {
        const xs = c.nodes.filter((n) => placed.has(n)).map((n) => placed.get(n).x);
        return { client: c, want: xs.reduce((a, b) => a + b, 0) / xs.length };
      })
      .sort((a, b) => a.want - b.want || natural(a.client.id, b.client.id));
    const spacing = CLIENT.w + CLIENT_GAP;
    // One row while it stays near its nodes; a grid only when there are too many to follow.
    const fits = Math.max(4, Math.floor((inner + CLIENT_GAP) / spacing));
    const perRow = own.length <= Math.max(12, fits * 2) ? own.length || 1 : fits;
    const clientPlaced = new Map();
    for (let r = 0; r * perRow < own.length; r++) {
      const chunk = own.slice(r * perRow, (r + 1) * perRow);
      const cy = y + ROW * 0.95 + r * (CLIENT.h + 22);
      let xs;
      if (own.length === perRow) {
        xs = [];
        for (const { want } of chunk) xs.push(xs.length ? Math.max(want, xs[xs.length - 1] + spacing) : want);
        // Pull the row back over its nodes: shift by the mean overshoot.
        const shift = chunk.reduce((s, c, i) => s + (xs[i] - c.want), 0) / chunk.length;
        xs = xs.map((x) => x - shift);
      } else {
        const start = (inner - rowWidth(chunk.length, CLIENT.w, CLIENT_GAP)) / 2;
        xs = chunk.map((_, i) => start + i * spacing + CLIENT.w / 2);
      }
      chunk.forEach(({ client }, i) => clientPlaced.set(client.id, { x: xs[i], y: cy }));
    }
    const clientRows = Math.ceil(own.length / perRow);
    if (own.length) labels.push({ text: "clients", y: y + ROW * 0.95 });

    // Left-align the block after the row labels.
    const lefts = [
      ...[...placed.values()].map((p) => p.x - NODE.w / 2),
      ...[...clientPlaced.values()].map((p) => p.x - CLIENT.w / 2),
    ];
    const rights = [
      ...[...placed.values()].map((p) => p.x + NODE.w / 2),
      ...[...clientPlaced.values()].map((p) => p.x + CLIENT.w / 2),
    ];
    const shift = PAD + LABEL - Math.min(...lefts);
    for (const [k, p] of placed) nodes.set(k, { x: p.x + shift, y: p.y });
    for (const [k, p] of clientPlaced) clients.set(k, { x: p.x + shift, y: p.y });
    width = Math.max(width, Math.max(...rights) + shift + PAD);
    y += own.length ? ROW * 0.95 + (clientRows - 1) * (CLIENT.h + 22) + CLIENT.h + PAD + 40 : NODE.h + PAD + 40;
  }
  return { nodes, clients, labels, titles, width, height: y };
}

/** A cable's path, and its two halves - each from one end to the middle - to colour by what that end sends. */
function linkPath(a, b) {
  if (Math.abs(a.y - b.y) < 1) {
    const top = a.y - NODE.h / 2;
    const lift = 36 + Math.abs(a.x - b.x) * 0.12;
    const c = { x: (a.x + b.x) / 2, y: top - lift };
    // The curve's middle, and each half as a curve of its own (de Casteljau at t = 0.5).
    const m = { x: (a.x + 2 * c.x + b.x) / 4, y: (top + 2 * c.y + top) / 4 };
    const half = (p) => `M${p.x},${top} Q${(p.x + c.x) / 2},${(top + c.y) / 2} ${m.x},${m.y}`;
    return { d: `M${a.x},${top} Q${c.x},${c.y} ${b.x},${top}`, halves: [half(a), half(b)], mid: m };
  }
  const end = (p, q) => ({ x: p.x, y: p.y + (p.y < q.y ? NODE.h / 2 : -NODE.h / 2) });
  const pa = end(a, b);
  const pb = end(b, a);
  const m = { x: (pa.x + pb.x) / 2, y: (pa.y + pb.y) / 2 };
  return {
    d: `M${pa.x},${pa.y} L${pb.x},${pb.y}`,
    halves: [`M${pa.x},${pa.y} L${m.x},${m.y}`, `M${pb.x},${pb.y} L${m.x},${m.y}`],
    mid: m,
  };
}

// --------------------------------------------------------------------------- traffic

export const RATE_UNITS = [
  ["Kbps", 1e3],
  ["Mbps", 1e6],
  ["Gbps", 1e9],
];
/** Where a cable's colour steps up, as a share of the configured link rate. */
export const HEAT_STEPS = [0.25, 0.5, 0.75];

export function formatBps(bps) {
  if (bps === null || bps === undefined) return "no rate yet";
  const [unit, scale] = [...RATE_UNITS].reverse().find(([, s]) => bps >= s) ?? ["bps", 1];
  const n = bps / scale;
  return `${n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)} ${unit}`;
}

/** The colour class of a rate against the link rate: bw-1 (under 25%) to bw-4 (75% and up). */
export function heat(bps, rate) {
  if (bps === null || bps === undefined || !(rate > 0)) return "bw-none";
  const share = bps / rate;
  return `bw-${1 + HEAT_STEPS.filter((s) => share >= s).length}`;
}

const most = (values) => {
  const known = values.filter((v) => v !== null && v !== undefined);
  return known.length ? Math.max(...known) : null;
};

const share = (bps, rate) => (bps !== null && rate > 0 ? ` (${Math.round((bps / rate) * 100)}%)` : "");

const hasTrouble = (counts) => (counts?.error ? "error" : counts?.warning ? "warning" : "");

function useViewport(box, width, height) {
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const [size, setSize] = useState({ w: 0, h: 0 });
  // Fitted to the window until panned or zoomed; Fit hands it back.
  const moved = useRef(false);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => {
      setSize({ w: entry.contentRect.width, h: entry.contentRect.height });
    });
    observer.observe(box.current);
    return () => observer.disconnect();
  }, []);
  const fit = () => {
    moved.current = false;
    const k = Math.min(size.w / width, size.h / height, 1.6);
    setView({ k, x: (size.w - width * k) / 2, y: Math.max(0, (size.h - height * k) / 2) });
  };
  const move = (next) => {
    moved.current = true;
    setView(next);
  };
  useEffect(() => {
    if (!moved.current && size.w > 0 && size.h > 0 && width > 0) fit();
  }, [size, width, height]);
  return { view, setView: move, fit };
}

/**
 * `overlay`, when given, picks out part of the fabric - a path, where an address is:
 * `{nodes, links, clients}` as Sets (links as "a|b", sorted), `tunnels` as [{from, to, label}],
 * and `marks` as Map(node -> text) shown above a node.
 */
export function Topology({
  graph,
  status,
  checks,
  selected,
  onSelect,
  overlay = null,
  legend = true,
  paint = { mode: "health", rate: 0 },
  controls = null,
}) {
  const traffic = paint.mode === "traffic";
  const box = useRef(null);
  const placed = useMemo(() => layout(graph), [graph]);
  const { view, setView, fit } = useViewport(box, placed.width, placed.height);
  const drag = useRef(null);

  const onWheel = (e) => {
    e.preventDefault();
    const rect = box.current.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const k = Math.min(4, Math.max(0.15, view.k * Math.exp(-e.deltaY * 0.0015)));
    setView({ k, x: mx - ((mx - view.x) * k) / view.k, y: my - ((my - view.y) * k) / view.k });
  };
  const onPointerDown = (e) => {
    drag.current = { x: e.clientX, y: e.clientY, view, moved: false };
  };
  const onPointerMove = (e) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (!d.moved && Math.hypot(dx, dy) < 4) return;
    if (!d.moved) box.current.setPointerCapture(e.pointerId);
    d.moved = true;
    setView({ ...d.view, x: d.view.x + dx, y: d.view.y + dy });
  };
  const onPointerUp = () => {
    const d = drag.current;
    drag.current = null;
    if (d && !d.moved) onSelect(null);
  };
  const pick = (selection) => (e) => {
    e.stopPropagation();
    if (drag.current?.moved) return;
    drag.current = null;
    onSelect(selection);
  };

  // What the selection touches: the rest is dimmed.
  const near = useMemo(() => {
    if (overlay) {
      return new Set([
        ...[...overlay.nodes].map((n) => `n:${n}`),
        ...[...overlay.links].map((l) => `l:${l}`),
        ...[...overlay.clients].map((c) => `c:${c}`),
      ]);
    }
    if (!selected) return null;
    const keep = new Set();
    if (selected.type === "node") {
      keep.add(`n:${selected.id}`);
      for (const l of graph.links) {
        if (l.a === selected.id || l.b === selected.id) {
          keep.add(`l:${l.a}|${l.b}`).add(`n:${l.a}`).add(`n:${l.b}`);
        }
      }
      for (const c of graph.clients) if (c.nodes.includes(selected.id)) keep.add(`c:${c.id}`);
    } else if (selected.type === "link") {
      const [a, b] = selected.id.split("|");
      keep.add(`l:${selected.id}`).add(`n:${a}`).add(`n:${b}`);
    } else {
      keep.add(`c:${selected.id}`);
      const client = graph.clients.find((c) => c.id === selected.id);
      for (const n of client?.nodes ?? []) keep.add(`n:${n}`);
    }
    return keep;
  }, [selected, graph, overlay]);
  const traced = (key) => (overlay && near.has(key) ? " traced" : "");
  const dim = (key) => (near && !near.has(key) ? " dim" : "");
  const live = new Map((status?.nodes ?? []).map((n) => [n.name, n]));

  return html`
    <div class="topology" ref=${box} onWheel=${onWheel} onPointerDown=${onPointerDown}
         onPointerMove=${onPointerMove} onPointerUp=${onPointerUp}>
      <svg width="100%" height="100%">
        <g transform=${`translate(${view.x},${view.y}) scale(${view.k})`}>
          ${placed.titles.map((t) => html`<text class="component-title" x=${PAD} y=${t.y}>${t.name}</text>`)}
          ${placed.labels.map((l) => html`<text class="row-label" x=${PAD} y=${l.y + 4}>${l.text}</text>`)}
          ${graph.clients.map((c) => {
            const at = placed.clients.get(c.id);
            if (!at) return null;
            return c.nodes
              .filter((n) => placed.nodes.has(n))
              .map((n) => {
                const p = placed.nodes.get(n);
                const here = c.attachments.filter((x) => x.node === n);
                const sends = traffic ? most(here.map((x) => x.out_bps)) : null;
                return html`<line class=${`attach${traffic ? ` ${heat(sends, paint.rate)}` : ""}${dim(`c:${c.id}`)}`} x1=${at.x} y1=${at.y - CLIENT.h / 2}
                                  x2=${p.x} y2=${p.y + NODE.h / 2} />`;
              });
          })}
          ${graph.links.map((l) => {
            const a = placed.nodes.get(l.a);
            const b = placed.nodes.get(l.b);
            if (!a || !b) return null;
            const id = `${l.a}|${l.b}`;
            const { d, halves, mid } = linkPath(a, b);
            const sel = selected?.type === "link" && selected.id === id ? " selected" : "";
            // Traffic: each half by what its end sends - the busiest of parallel cables. A cable not
            // carrying (down, lost, unknown) keeps its state's look whatever is drawn.
            const carrying = l.state === "up" || l.state === "mixed";
            const sends = [most(l.cables.map((c) => c.a_out_bps)), most(l.cables.map((c) => c.b_out_bps))];
            const trouble = traffic ? "" : hasTrouble(l.findings);
            const title = traffic && carrying
              ? `${l.a} → ${l.b}: ${formatBps(sends[0])}${share(sends[0], paint.rate)}\n${l.b} → ${l.a}: ${formatBps(sends[1])}${share(sends[1], paint.rate)}`
              : `${l.a} ↔ ${l.b}: ${l.count} cable${l.count > 1 ? "s" : ""}, ${l.state || "state unknown"}`;
            return html`<g class=${`link state-${l.state || "unknown"} ${trouble}${sel}${dim(`l:${id}`)}${traced(`l:${id}`)}`}
                           onClick=${pick({ type: "link", id })} onPointerDown=${(e) => e.stopPropagation()}>
              <path class="hit" d=${d} />
              ${traffic && carrying
                ? halves.map((h, i) => html`<path class=${`wire half ${heat(sends[i], paint.rate)}`} d=${h} />`)
                : html`<path class="wire" d=${d} />`}
              ${l.count > 1 && html`<text class="count" x=${mid.x} y=${mid.y}>×${l.count}</text>`}
              <title>${title}</title>
            </g>`;
          })}
          ${(overlay?.tunnels ?? []).map((t) => {
            const a = placed.nodes.get(t.from);
            const b = placed.nodes.get(t.to);
            if (!a || !b || t.from === t.to) return null;
            let d;
            let label;
            if (Math.abs(a.y - b.y) < 1) {
              // Along a row: under it, between the nodes and their clients, so the cables above stay readable.
              const dip = Math.min(ROW * 0.55, 34 + Math.abs(a.x - b.x) * 0.06);
              const low = a.y + NODE.h / 2;
              d = `M${a.x},${low} C${a.x},${low + dip} ${b.x},${low + dip} ${b.x},${low}`;
              label = { x: (a.x + b.x) / 2, y: low + dip * 0.75 + 4 };
            } else {
              // Between rows: bowed out to the side, clear of the cables it rides over.
              const [up, down] = a.y < b.y ? [a, b] : [b, a];
              const side = up.x <= down.x ? -1 : 1;
              const bow = side * (NODE.w * 0.45 + Math.abs(up.x - down.x) * 0.1);
              const y1 = up.y + NODE.h / 2;
              const y2 = down.y - NODE.h / 2;
              d = `M${up.x + side * 20},${y1} C${up.x + bow},${y1 + (y2 - y1) * 0.4} ${down.x + bow},${y2 - (y2 - y1) * 0.4} ${down.x + side * 20},${y2}`;
              label = { x: (up.x + down.x) / 2 + bow * 0.8, y: (y1 + y2) / 2 };
            }
            return html`<g class="tunnel"><path d=${d} /><text x=${label.x} y=${label.y}>${t.label}</text></g>`;
          })}
          ${graph.nodes.map((n) => {
            const p = placed.nodes.get(n.name);
            if (!p) return null;
            const s = live.get(n.name);
            const down = n.unreachable || (s && s.status === "down");
            const sel = selected?.type === "node" && selected.id === n.name ? " selected" : "";
            const errors = n.findings?.error || 0;
            const warnings = n.findings?.warning || 0;
            return html`<g class=${`node health-${down ? "down" : n.health}${sel}${dim(`n:${n.name}`)}${traced(`n:${n.name}`)}`}
                           transform=${`translate(${p.x - NODE.w / 2},${p.y - NODE.h / 2})`}
                           onClick=${pick({ type: "node", id: n.name })} onPointerDown=${(e) => e.stopPropagation()}>
              <rect class="box" width=${NODE.w} height=${NODE.h} rx="8" />
              <rect class="bar" width="5" height=${NODE.h} rx="2" />
              <text class="name" x="14" y="18">${n.name}</text>
              <text class="sub" x="14" y="31">${down ? "unreachable" : n.type || n.tier}</text>
              ${[n.ipv4, n.ipv6].filter(Boolean).map(
                (address, i) => html`<text class="address" x="14" y=${31 + LINE * (i + 1)}>${address.length > 22 ? `${address.slice(0, 21)}…` : address}</text>`,
              )}
              ${overlay?.marks?.get(n.name) &&
              html`<g class="mark" transform=${`translate(${NODE.w / 2},-14)`}>
                <rect x=${-(overlay.marks.get(n.name).length * 3.6 + 8)} y="-10" width=${overlay.marks.get(n.name).length * 7.2 + 16}
                      height="18" rx="9" /><text y="3">${overlay.marks.get(n.name)}</text></g>`}
              ${errors > 0 && html`<g transform=${`translate(${NODE.w - 14},0)`} class="badge error">
                <circle r="10" /><text y="4">${errors}</text></g>`}
              ${warnings > 0 && html`<g transform=${`translate(${NODE.w - (errors ? 36 : 14)},0)`} class="badge warning">
                <circle r="10" /><text y="4">${warnings}</text></g>`}
              <title>${[`${n.name} (${n.tier}${n.type ? `, ${n.type}` : ""})`, n.ipv4, n.ipv6, down ? n.unreachable || s?.error || "down" : ""].filter(Boolean).join("\n")}</title>
            </g>`;
          })}
          ${graph.clients.map((c) => {
            const p = placed.clients.get(c.id);
            if (!p) return null;
            const sel = selected?.type === "client" && selected.id === c.id ? " selected" : "";
            const label = c.label || c.name || c.id;
            return html`<g class=${`client${c.esi ? " multihomed" : ""}${sel}${dim(`c:${c.id}`)}${traced(`c:${c.id}`)}`}
                           transform=${`translate(${p.x - CLIENT.w / 2},${p.y - CLIENT.h / 2})`}
                           onClick=${pick({ type: "client", id: c.id })} onPointerDown=${(e) => e.stopPropagation()}>
              <rect width=${CLIENT.w} height=${CLIENT.h} rx="15" />
              <text x=${CLIENT.w / 2} y="19">${label.length > 15 ? `${label.slice(0, 14)}…` : label}</text>
              <title>${label}: ${[...new Set(c.attachments.map((a) => a.service))].sort(natural).join(", ")}</title>
            </g>`;
          })}
        </g>
      </svg>
      <div class="map-tools">
        <button onClick=${fit} title="Fit the fabric to the window">Fit</button>
        <button onClick=${() => setView({ ...view, k: Math.min(4, view.k * 1.25) })} title="Zoom in">+</button>
        <button onClick=${() => setView({ ...view, k: Math.max(0.15, view.k / 1.25) })} title="Zoom out">−</button>
      </div>
      ${controls && html`<div class="map-controls" onPointerDown=${(e) => e.stopPropagation()}>${controls}</div>`}
      ${legend && html`<${Legend} paint=${paint} />`}
    </div>
  `;
}

function Legend({ paint }) {
  if (paint.mode === "traffic") {
    const bound = (s) => formatBps(paint.rate * s);
    return html`<div class="legend">
      <span><i class="ln bw-1"></i>under ${bound(HEAT_STEPS[0])}</span>
      <span><i class="ln bw-2"></i>${bound(HEAT_STEPS[0])}–${bound(HEAT_STEPS[1])}</span>
      <span><i class="ln bw-3"></i>${bound(HEAT_STEPS[1])}–${bound(HEAT_STEPS[2])}</span>
      <span><i class="ln bw-4"></i>over ${bound(HEAT_STEPS[2])}</span>
      <span><i class="ln bw-none"></i>no rate yet</span>
      <span class="muted">each half: what its end sends</span>
    </div>`;
  }
  return html`<div class="legend">
    <span><i class="sw ok"></i>healthy</span>
    <span><i class="sw warning"></i>warning</span>
    <span><i class="sw error"></i>error</span>
    <span><i class="sw down"></i>unreachable</span>
    <span><i class="ln lost"></i>cable lost</span>
  </div>`;
}

// --------------------------------------------------------------------------- details

const portOf = (subject) => String(subject || "").split("/").slice(0, 2).join("/").split(".")[0];

function FindingList({ findings }) {
  return html`<ul class="findings">
    ${[...findings].sort(bySeverity).map(
      (f) => html`<li class=${`sev-${f.severity}`}>
        <span class=${`pill ${f.severity}`}>${f.severity}</span>
        <div><b>${f.check}</b> <span class="mono">${f.node} ${f.subject}</span>
          <div class="detail">${f.detail}${f.raised ? html` <span class="muted">· ${ago(f.raised)}</span>` : ""}</div></div>
      </li>`,
    )}
  </ul>`;
}

/** Errors and warnings; information folded away behind one line. */
function Findings({ findings, empty = "No findings." }) {
  const [showInfo, setShowInfo] = useState(false);
  const faults = findings.filter((f) => f.severity !== "info");
  const info = findings.filter((f) => f.severity === "info");
  if (!findings.length) return html`<p class="muted">${empty}</p>`;
  return html`
    ${faults.length ? html`<${FindingList} findings=${faults} />` : html`<p class="muted">No errors or warnings.</p>`}
    ${info.length > 0 &&
    html`<button class="link-button" onClick=${() => setShowInfo(!showInfo)}>
        ${showInfo ? "▾" : "▸"} ${info.length} for information
      </button>
      ${showInfo && html`<${FindingList} findings=${info} />`}`}`;
}

function SysInfo({ node, at }) {
  const [info, setInfo] = useState(null);
  useEffect(() => {
    let current = true;
    setInfo(null);
    get("/api/reports/sys-info", { node, at })
      .then((answer) => current && setInfo(answer.records[0] || {}))
      .catch(() => current && setInfo({}));
    return () => {
      current = false;
    };
  }, [node, at]);
  if (!info) return html`<p class="muted">Loading…</p>`;
  const rows = [
    ["type", info.type],
    ["software", info.software_version],
    ["serial", info.serial_number],
    ["booted", info.last_booted],
  ].filter(([, v]) => v);
  if (!rows.length) return null;
  return html`<dl class="facts">${rows.map(([k, v]) => html`<dt>${k}</dt><dd class="mono">${v}</dd>`)}</dl>`;
}

export function Details({ selection, graph, status, checks, at, onSelect }) {
  if (!selection) {
    const findings = checks.filter((f) => f.severity !== "info");
    return html`<div class="details">
      <h2>${graph.components.length === 1 ? graph.components[0].name : "The fabric"}</h2>
      <dl class="facts">
        <dt>nodes</dt><dd>${graph.nodes.length}</dd>
        <dt>cables</dt><dd>${graph.links.reduce((s, l) => s + l.count, 0)}</dd>
        <dt>clients</dt><dd>${graph.clients.length}</dd>
        <dt>segments</dt><dd>${graph.segments.length}</dd>
      </dl>
      <p class="muted">Click a node, a cable or a client for its details. Drag to pan, scroll to zoom.</p>
      <h3>Findings</h3>
      <${Findings} findings=${findings} empty="Nothing wrong: no errors or warnings." />
    </div>`;
  }
  if (selection.type === "node") {
    const n = graph.nodes.find((x) => x.name === selection.id);
    if (!n) return html`<div class="details"><p class="muted">${selection.id} is not in this view.</p></div>`;
    const s = (status?.nodes ?? []).find((x) => x.name === n.name);
    const links = graph.links.filter((l) => l.a === n.name || l.b === n.name);
    const clients = graph.clients.filter((c) => c.nodes.includes(n.name));
    return html`<div class="details">
      <h2>${n.name} <span class=${`pill ${n.unreachable ? "down" : n.health}`}>${n.unreachable ? "unreachable" : n.health}</span></h2>
      <dl class="facts">
        <dt>tier</dt><dd>${n.tier}</dd>
        ${s && html`<dt>stream</dt><dd>${s.status}${s.since ? html` <span class="muted">since ${ago(s.since)}</span>` : ""}</dd>`}
        ${s?.address && html`<dt>address</dt><dd class="mono">${s.address}</dd>`}
        ${s && Object.keys(s.labels || {}).length > 0 &&
        html`<dt>labels</dt><dd>${Object.entries(s.labels).map(([k, v]) => html`<span class="chip">${k}=${v}</span>`)}</dd>`}
      </dl>
      ${(n.unreachable || s?.error) && html`<p class="error-text">${n.unreachable || s.error}</p>`}
      ${!n.unreachable && html`<${SysInfo} node=${n.name} at=${at} />`}
      <h3>Findings</h3>
      <${Findings} findings=${checks.filter((f) => f.node === n.name)} />
      <h3>Cables</h3>
      <ul class="rows">
        ${links.map((l) => {
          const peer = l.a === n.name ? l.b : l.a;
          return html`<li><a onClick=${() => onSelect({ type: "link", id: `${l.a}|${l.b}` })}>${peer}</a>
            <span class="muted">${l.count} cable${l.count > 1 ? "s" : ""}</span>
            <span class=${`pill state-${l.state}`}>${l.state || "?"}</span></li>`;
        })}
        ${!links.length && html`<li class="muted">No cables to other nodes.</li>`}
      </ul>
      ${clients.length > 0 &&
      html`<h3>Clients</h3>
        <ul class="rows">${clients.map(
          (c) => html`<li><a onClick=${() => onSelect({ type: "client", id: c.id })}>${c.label || c.name || c.id}</a>
            <span class="muted mono">${c.attachments.filter((a) => a.node === n.name).map((a) => a.port).filter((p, i, all) => all.indexOf(p) === i).join(", ")}</span></li>`,
        )}</ul>`}
    </div>`;
  }
  if (selection.type === "link") {
    const l = graph.links.find((x) => `${x.a}|${x.b}` === selection.id);
    if (!l) return html`<div class="details"><p class="muted">That cable is not in this view.</p></div>`;
    const ends = new Set(l.cables.flatMap((c) => [`${l.a} ${c.a_port}`, `${l.b} ${c.b_port}`]));
    const findings = checks.filter((f) => ends.has(`${f.node} ${portOf(f.subject)}`));
    return html`<div class="details">
      <h2><a onClick=${() => onSelect({ type: "node", id: l.a })}>${l.a}</a> ↔ <a onClick=${() => onSelect({ type: "node", id: l.b })}>${l.b}</a></h2>
      <table class="grid">
        <thead><tr><th>${l.a}</th><th>${l.b}</th><th>state</th><th>→ ${l.b}</th><th>→ ${l.a}</th></tr></thead>
        <tbody>${l.cables.map(
          (c) => html`<tr><td class="mono">${c.a_port}</td><td class="mono">${c.b_port}</td>
            <td><span class=${`pill state-${c.state}`}>${c.state || "?"}</span></td>
            <td class="rate">${c.a_out_bps === null ? "—" : formatBps(c.a_out_bps)}</td>
            <td class="rate">${c.b_out_bps === null ? "—" : formatBps(c.b_out_bps)}</td></tr>`,
        )}</tbody>
      </table>
      ${l.state === "lost" && html`<p class="muted">LLDP no longer reports this cable; the history saw it.</p>`}
      <h3>Findings on these ports</h3>
      <${Findings} findings=${findings} />
    </div>`;
  }
  const c = graph.clients.find((x) => x.id === selection.id);
  if (!c) return html`<div class="details"><p class="muted">That client is not in this view.</p></div>`;
  const segment = graph.segments.find((s) => s.esi && s.esi === c.esi);
  return html`<div class="details">
    <h2>${c.label || c.name || c.id}</h2>
    <dl class="facts">
      ${c.esi && html`<dt>segment</dt><dd>${segment?.names.join(" / ") || ""} <span class="mono muted">${c.esi}</span></dd>`}
      ${segment && html`<dt>mode</dt><dd>${segment.mode}</dd>`}
      <dt>services</dt><dd>${[...new Set(c.attachments.map((a) => a.service))].sort(natural).join(", ")}</dd>
    </dl>
    <table class="grid">
      <thead><tr><th>node</th><th>subinterface</th><th>service</th><th>vlan</th><th>to it</th><th>from it</th></tr></thead>
      <tbody>${[...c.attachments]
        .sort((a, b) => natural(a.node, b.node) || natural(a.subinterface, b.subinterface))
        .map(
          (a) => html`<tr><td><a onClick=${() => onSelect({ type: "node", id: a.node })}>${a.node}</a></td>
            <td class="mono">${a.subinterface}</td><td>${a.service}</td><td>${a.vlan ?? ""}</td>
            <td class="rate">${a.out_bps === null || a.out_bps === undefined ? "—" : formatBps(a.out_bps)}</td>
            <td class="rate">${a.in_bps === null || a.in_bps === undefined ? "—" : formatBps(a.in_bps)}</td></tr>`,
        )}</tbody>
    </table>
  </div>`;
}

// --------------------------------------------------------------------------- overlays

function remembered(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
}

function remember(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // A preference that does not stick is no loss.
  }
}

/**
 * What the topology is coloured by - "health", "traffic" or "service:<name>" - and the
 * link rate traffic is scaled from: emulated nodes forward far below their port speed,
 * so the scale is set here rather than read from the ports. Both kept in the browser.
 */
export function useOverlay() {
  const [overlay, setOverlayState] = useState(() => remembered("fcli-overlay", "health"));
  const [rate, setRateState] = useState(() => remembered("fcli-link-rate", { value: 10, unit: "Mbps" }));
  const setOverlay = (value) => {
    setOverlayState(value);
    remember("fcli-overlay", value);
  };
  const setRate = (value) => {
    setRateState(value);
    remember("fcli-link-rate", value);
  };
  const scale = RATE_UNITS.find(([u]) => u === rate.unit)?.[1] ?? 1e6;
  const bps = Number(rate.value) > 0 ? Number(rate.value) * scale : 0;
  return { overlay, setOverlay, rate, setRate, bps };
}

/** A service's part of the fabric: the nodes that carry it, the cables between them, its clients. */
export function serviceOverlay(graph, name) {
  const nodes = new Set(graph.nodes.filter((n) => (n.services || []).includes(name)).map((n) => n.name));
  const clients = new Set();
  for (const c of graph.clients) {
    const on = c.attachments.filter((a) => a.service === name);
    if (on.length) clients.add(c.id);
    for (const a of on) nodes.add(a.node);
  }
  const links = new Set(graph.links.filter((l) => nodes.has(l.a) && nodes.has(l.b)).map((l) => `${l.a}|${l.b}`));
  return { nodes, links, clients, tunnels: [], marks: new Map() };
}

export function OverlayControls({ graph, settings }) {
  const { overlay, setOverlay, rate, setRate } = settings;
  const services = [...new Set(graph.nodes.flatMap((n) => n.services || []))].sort(natural);
  return html`<label class="overlay-pick"><span>Show</span>
      <select value=${overlay} onChange=${(e) => setOverlay(e.target.value)} aria-label="Colour the topology by">
        <option value="health">health</option>
        <option value="traffic">traffic</option>
        ${services.length > 0 &&
        html`<optgroup label="service">${services.map((s) => html`<option value=${`service:${s}`}>${s}</option>`)}</optgroup>`}
      </select>
    </label>
    ${overlay === "traffic" &&
    html`<label class="rate-pick" title="The rate a cable is full at. Emulated nodes forward far below their port speed, so this is the scale for colouring, not the Ethernet rate.">
      <span>link rate</span>
      <input type="number" min="0" step="any" value=${rate.value}
             onInput=${(e) => setRate({ ...rate, value: e.target.value })} aria-label="Link rate" />
      <select value=${rate.unit} onChange=${(e) => setRate({ ...rate, unit: e.target.value })} aria-label="Unit">
        ${RATE_UNITS.map(([u]) => html`<option value=${u}>${u}</option>`)}
      </select>
    </label>`}`;
}

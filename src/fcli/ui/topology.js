// The topology: the fabric laid out by tier, with cables, clients and health; and what a click shows.

import { html, useEffect, useMemo, useRef, useState } from "./vendor/preact-htm.module.js";
import { ago, bySeverity, get, natural } from "./api.js";

const TIERS = ["gateway", "core", "spine", "leaf", "unclassified"];
const NODE = { w: 124, h: 42 };
const CLIENT = { w: 118, h: 30 };
const GAP = 28;
const CLIENT_GAP = 14;
const ROW = 118;
const PAD = 40;
const LABEL = 84;

const rowWidth = (n, w, gap) => n * w + Math.max(0, n - 1) * gap;

/** Where everything goes: one block per component, a row per tier, clients under the nodes they hang off. */
export function layout(graph) {
  const tierOf = new Map(graph.nodes.map((n) => [n.name, n.tier]));
  const nodes = new Map();
  const clients = new Map();
  const labels = [];
  const titles = [];
  const multi = graph.components.length > 1;
  let y = PAD;
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

function linkPath(a, b) {
  if (Math.abs(a.y - b.y) < 1) {
    const top = a.y - NODE.h / 2;
    const lift = 36 + Math.abs(a.x - b.x) * 0.12;
    return `M${a.x},${top} Q${(a.x + b.x) / 2},${top - lift} ${b.x},${top}`;
  }
  const [up, down] = a.y < b.y ? [a, b] : [b, a];
  return `M${up.x},${up.y + NODE.h / 2} L${down.x},${down.y - NODE.h / 2}`;
}

const hasTrouble = (counts) => (counts?.error ? "error" : counts?.warning ? "warning" : "");

function useViewport(box, width, height) {
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const [size, setSize] = useState({ w: 0, h: 0 });
  const fitted = useRef(false);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => {
      setSize({ w: entry.contentRect.width, h: entry.contentRect.height });
    });
    observer.observe(box.current);
    return () => observer.disconnect();
  }, []);
  const fit = () => {
    const k = Math.min(size.w / width, size.h / height, 1.6);
    setView({ k, x: (size.w - width * k) / 2, y: Math.max(0, (size.h - height * k) / 2) });
  };
  useEffect(() => {
    if (!fitted.current && size.w > 0 && size.h > 0 && width > 0) {
      fitted.current = true;
      fit();
    }
  }, [size, width, height]);
  return { view, setView, fit };
}

/**
 * `overlay`, when given, picks out part of the fabric - a path, where an address is:
 * `{nodes, links, clients}` as Sets (links as "a|b", sorted), `tunnels` as [{from, to, label}],
 * and `marks` as Map(node -> text) shown above a node.
 */
export function Topology({ graph, status, checks, selected, onSelect, overlay = null, legend = true }) {
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
                return html`<line class=${`attach${dim(`c:${c.id}`)}`} x1=${at.x} y1=${at.y - CLIENT.h / 2}
                                  x2=${p.x} y2=${p.y + NODE.h / 2} />`;
              });
          })}
          ${graph.links.map((l) => {
            const a = placed.nodes.get(l.a);
            const b = placed.nodes.get(l.b);
            if (!a || !b) return null;
            const id = `${l.a}|${l.b}`;
            const d = linkPath(a, b);
            const trouble = hasTrouble(l.findings);
            const sel = selected?.type === "link" && selected.id === id ? " selected" : "";
            return html`<g class=${`link state-${l.state || "unknown"} ${trouble}${sel}${dim(`l:${id}`)}${traced(`l:${id}`)}`}
                           onClick=${pick({ type: "link", id })} onPointerDown=${(e) => e.stopPropagation()}>
              <path class="hit" d=${d} />
              <path class="wire" d=${d} />
              ${l.count > 1 &&
              html`<text class="count" x=${(a.x + b.x) / 2} y=${(a.y + b.y) / 2}>×${l.count}</text>`}
              <title>${l.a} ↔ ${l.b}: ${l.count} cable${l.count > 1 ? "s" : ""}, ${l.state || "state unknown"}</title>
            </g>`;
          })}
          ${(overlay?.tunnels ?? []).map((t) => {
            const a = placed.nodes.get(t.from);
            const b = placed.nodes.get(t.to);
            if (!a || !b || t.from === t.to) return null;
            // Under the row, between the nodes and their clients: the cables above stay readable.
            const dip = Math.min(ROW * 0.55, 34 + Math.abs(a.x - b.x) * 0.06);
            const low = Math.max(a.y, b.y) + NODE.h / 2;
            const d = `M${a.x},${a.y + NODE.h / 2} C${a.x},${low + dip} ${b.x},${low + dip} ${b.x},${b.y + NODE.h / 2}`;
            return html`<g class="tunnel"><path d=${d} />
              <text x=${(a.x + b.x) / 2} y=${low + dip * 0.75 + 4}>${t.label}</text></g>`;
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
              <text class="name" x="14" y=${down ? 18 : 26}>${n.name}</text>
              ${down && html`<text class="sub" x="14" y="32">unreachable</text>`}
              ${overlay?.marks?.get(n.name) &&
              html`<g class="mark" transform=${`translate(${NODE.w / 2},-14)`}>
                <rect x=${-(overlay.marks.get(n.name).length * 3.6 + 8)} y="-10" width=${overlay.marks.get(n.name).length * 7.2 + 16}
                      height="18" rx="9" /><text y="3">${overlay.marks.get(n.name)}</text></g>`}
              ${errors > 0 && html`<g transform=${`translate(${NODE.w - 14},0)`} class="badge error">
                <circle r="10" /><text y="4">${errors}</text></g>`}
              ${warnings > 0 && html`<g transform=${`translate(${NODE.w - (errors ? 36 : 14)},0)`} class="badge warning">
                <circle r="10" /><text y="4">${warnings}</text></g>`}
              <title>${n.name} (${n.tier})${down ? `: ${n.unreachable || s?.error || "down"}` : ""}</title>
            </g>`;
          })}
          ${graph.clients.map((c) => {
            const p = placed.clients.get(c.id);
            if (!p) return null;
            const sel = selected?.type === "client" && selected.id === c.id ? " selected" : "";
            const label = c.name || c.attachments[0]?.port || c.id;
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
      ${legend && html`<${Legend} />`}
    </div>
  `;
}

function Legend() {
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
          (c) => html`<li><a onClick=${() => onSelect({ type: "client", id: c.id })}>${c.name || c.id}</a>
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
        <thead><tr><th>${l.a}</th><th>${l.b}</th><th>state</th></tr></thead>
        <tbody>${l.cables.map(
          (c) => html`<tr><td class="mono">${c.a_port}</td><td class="mono">${c.b_port}</td>
            <td><span class=${`pill state-${c.state}`}>${c.state || "?"}</span></td></tr>`,
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
    <h2>${c.name || c.id}</h2>
    <dl class="facts">
      ${c.esi && html`<dt>segment</dt><dd>${segment?.names.join(" / ") || ""} <span class="mono muted">${c.esi}</span></dd>`}
      ${segment && html`<dt>mode</dt><dd>${segment.mode}</dd>`}
      <dt>services</dt><dd>${[...new Set(c.attachments.map((a) => a.service))].sort(natural).join(", ")}</dd>
    </dl>
    <table class="grid">
      <thead><tr><th>node</th><th>subinterface</th><th>service</th><th>vlan</th></tr></thead>
      <tbody>${[...c.attachments]
        .sort((a, b) => natural(a.node, b.node) || natural(a.subinterface, b.subinterface))
        .map(
          (a) => html`<tr><td><a onClick=${() => onSelect({ type: "node", id: a.node })}>${a.node}</a></td>
            <td class="mono">${a.subinterface}</td><td>${a.service}</td><td>${a.vlan ?? ""}</td></tr>`,
        )}</tbody>
    </table>
  </div>`;
}

// The lenses: where an address is, how traffic would get there, the services end to end, every host.

import { html, useEffect, useMemo, useState } from "./vendor/preact-htm.module.js";
import { get, natural, withQuery } from "./api.js";
import { DataTable } from "./table.js";
import { Topology } from "./topology.js";

export const LENS_VIEWS = {
  where: { title: "Where", help: "Every place in the fabric that knows an IP or MAC address" },
  path: { title: "Path", help: "How the fabric would forward to an address, hop by hop, from its tables" },
  service: { title: "Services", help: "Bridge domains and routers end to end: by site, stitched across sites, and what disagrees" },
  endpoints: { title: "Endpoints", help: "Every host attached to the fabric, where it is attached" },
};

function useLens(name, args, at, ready) {
  const [state, setState] = useState({ answer: null, error: "", loading: false });
  const key = JSON.stringify([name, args, at]);
  useEffect(() => {
    if (!ready) {
      setState({ answer: null, error: "", loading: false });
      return;
    }
    let current = true;
    setState((s) => ({ ...s, loading: true, error: "" }));
    get(`/api/lenses/${name}`, { ...args, at, table: 1 })
      .then((answer) => current && setState({ answer, error: "", loading: false }))
      .catch((e) => current && setState({ answer: null, error: e.message, loading: false }));
    return () => {
      current = false;
    };
  }, [key, ready]);
  return state;
}

/** The mac-vrfs and ip-vrfs there are, for the forms. */
function useServices(at) {
  const [names, setNames] = useState([]);
  useEffect(() => {
    get("/api/reports/services", { at })
      .then((a) => {
        const found = new Map();
        for (const r of a.records) if (r.type === "mac-vrf" || r.type === "ip-vrf") found.set(r.name, r.type);
        setNames([...found].sort(([a], [b]) => natural(a, b)));
      })
      .catch(() => setNames([]));
  }, [at]);
  return names;
}

function Form({ fields, values, onSubmit, submit = "Show" }) {
  const [draft, setDraft] = useState(values);
  useEffect(() => setDraft(values), [JSON.stringify(values)]);
  const go = (e) => {
    e.preventDefault();
    onSubmit(Object.fromEntries(Object.entries(draft).map(([k, v]) => [k, String(v ?? "").trim()])));
  };
  return html`<form class="lens-form" onSubmit=${go}>
    ${fields.map(
      (f) => html`<label class=${f.wide ? "wide" : ""}>
        <span>${f.label}${f.optional ? html` <i class="muted">optional</i>` : ""}</span>
        <input value=${draft[f.name] ?? ""} list=${f.list || null} placeholder=${f.placeholder || ""}
               onInput=${(e) => setDraft({ ...draft, [f.name]: e.target.value })} />
      </label>`,
    )}
    <button class="primary" type="submit">${submit}</button>
  </form>`;
}

function Result({ state, children }) {
  if (state.loading && !state.answer) return html`<p class="muted">Asking the fabric…</p>`;
  if (state.error) return html`<p class="error-text">${state.error}</p>`;
  if (!state.answer) return null;
  return children;
}

const pair = (a, b) => [a, b].sort(natural).join("|");

/** The clients a hop or sighting puts a host on: by node and subinterface or port. */
function clientsAt(graph, node, port) {
  if (!port) return [];
  return graph.clients
    .filter((c) => c.attachments.some((a) => a.node === node && (a.subinterface === port || a.port === port)))
    .map((c) => c.id);
}

// --------------------------------------------------------------------------- path

const FAILED = new Set(["no-route", "discard", "dead-end", "unresolved", "no-neighbor", "loop", "too-long"]);

function pathOverlay(graph, hops) {
  const nodes = new Set();
  const links = new Set();
  const clients = new Set();
  const marks = new Map();
  const tunnels = new Map();
  const owner = new Map();
  for (const h of hops) if (h.outcome === "endpoint-reached") owner.set(h.address, h.node);
  for (const h of hops) {
    nodes.add(h.node);
    const mark = marks.get(h.node) || [];
    if (!mark.includes(h.hop)) mark.push(h.hop);
    marks.set(h.node, mark);
    if (h.outcome === "forwarded" && h.peer) {
      nodes.add(h.peer);
      links.add(pair(h.node, h.peer));
    }
    if (h.outcome === "ingress" || h.outcome === "delivered") {
      for (const c of clientsAt(graph, h.node, h.egress)) clients.add(c);
    }
    if (h.outcome === "tunnel" && owner.has(h.endpoint)) {
      const to = owner.get(h.endpoint);
      tunnels.set(`${h.node}>${to}`, { from: h.node, to, label: `${h.tunnel}${h.vni ? ` ${h.vni}` : ""}` });
    }
  }
  return {
    nodes,
    links,
    clients,
    tunnels: [...tunnels.values()],
    marks: new Map([...marks].map(([n, hopsHere]) => [n, hopsHere.sort((a, b) => a - b).join("·")])),
  };
}

function PathSummary({ hops }) {
  const ends = hops.filter((h) => h.outcome === "delivered" || h.outcome === "handed-off" || h.outcome === "local-ip" || FAILED.has(h.outcome));
  const ok = ends.filter((h) => !FAILED.has(h.outcome));
  const bad = ends.filter((h) => FAILED.has(h.outcome));
  return html`<div class="summary-line">
    ${ok.map(
      (h) => html`<span class="outcome ok">${h.outcome} on <b>${h.node}</b>${h.egress ? html` <span class="mono">${h.egress}</span>` : ""}${h.mac ? html` <span class="mono muted">${h.mac}</span>` : ""}</span>`,
    )}
    ${bad.map((h) => html`<span class="outcome bad"><b>${h.outcome}</b> on ${h.node} (${h.ni} ${h.address})</span>`)}
    ${!ends.length && html`<span class="muted">The walk ended without an answer.</span>`}
    <span class="muted">${new Set(hops.map((h) => h.node)).size} nodes on the way</span>
  </div>`;
}

export function PathView({ query, go, graph, status, checks, at }) {
  const services = useServices(at);
  const ready = Boolean(query.source && query.destination);
  const state = useLens("path", { source: query.source, destination: query.destination, ni: query.ni }, at, ready);
  const hops = state.answer?.records ?? [];
  const overlay = useMemo(() => (hops.length ? pathOverlay(graph, hops) : null), [hops, graph]);
  return html`<div class="lens">
    <${Form} values=${{ source: query.source, destination: query.destination, ni: query.ni }}
             onSubmit=${(v) => go(withQuery("lens/path", v))} submit="Trace"
             fields=${[
               { name: "source", label: "From", placeholder: "a node, or a host's IP or MAC", list: "fcli-nodes" },
               { name: "destination", label: "To", placeholder: "an IP or MAC" },
               { name: "ni", label: "In", placeholder: "network-instance", optional: true, list: "fcli-services" },
             ]} />
    <datalist id="fcli-nodes">${graph.nodes.map((n) => html`<option value=${n.name} />`)}</datalist>
    <datalist id="fcli-services">${services.map(([n, t]) => html`<option value=${n}>${t}</option>`)}</datalist>
    ${!ready && html`<p class="muted">The path is read from the route and bridge tables; nothing is sent. From a host, it enters on each leaf it is attached to; ECMP branches, VXLAN tunnels and leaked routes are followed.</p>`}
    <${Result} state=${state}>
      <${PathSummary} hops=${hops} />
      <div class="lens-map">
        <${Topology} graph=${graph} status=${status} checks=${checks} overlay=${overlay} legend=${false}
                     selected=${null} onSelect=${(s) => s && go(`topology/${s.type}/${encodeURIComponent(s.id)}`)} />
      </div>
      <${DataTable} columns=${state.answer?.columns ?? []} rows=${state.answer?.rows ?? []} ordered=${true}
                    onNode=${(n) => go(`topology/node/${encodeURIComponent(n)}`)} />
    </${Result}>
  </div>`;
}

// --------------------------------------------------------------------------- where

const HERE = new Set(["configured", "local", "multihomed", "duplicate"]);
/** What a node's mark says when it knows the address several ways: the strongest. */
const STRENGTH = ["duplicate", "multihomed", "local", "configured", "arp", "nd"];

function whereOverlay(graph, sightings) {
  const nodes = new Set();
  const clients = new Set();
  const marks = new Map();
  for (const s of sightings) {
    if (s.node === "-") continue;
    const held = HERE.has(s.kind) || ((s.kind === "arp" || s.kind === "nd") && s.origin !== "evpn");
    if (!held) continue;
    nodes.add(s.node);
    const before = marks.get(s.node);
    if (!before || STRENGTH.indexOf(s.kind) < STRENGTH.indexOf(before)) marks.set(s.node, s.kind);
    for (const c of clientsAt(graph, s.node, s.learned_on || (s.kind !== "configured" ? s.interface : ""))) clients.add(c);
  }
  return { nodes, links: new Set(), clients, tunnels: [], marks };
}

export function WhereView({ query, go, graph, status, checks, at }) {
  const ready = Boolean(query.address);
  const state = useLens("where", { address: query.address }, at, ready);
  const sightings = state.answer?.records ?? [];
  const overlay = useMemo(() => (sightings.length ? whereOverlay(graph, sightings) : null), [sightings, graph]);
  const kinds = [...new Set(sightings.map((s) => s.kind))];
  return html`<div class="lens">
    <${Form} values=${{ address: query.address }} onSubmit=${(v) => go(withQuery("lens/where", v))} submit="Find"
             fields=${[{ name: "address", label: "Address", placeholder: "10.1.3.13, 2001:db8::1, AA:C1:AB:73:4C:27", wide: true }]} />
    <${Result} state=${state}>
      <div class="summary-line">
        ${kinds.includes("not-found")
          ? html`<span class="outcome bad">${query.address} is nowhere in the fabric.</span>`
          : html`${kinds.includes("duplicate") && html`<span class="outcome bad"><b>duplicate</b>: on several nodes on no common segment</span>`}
                 ${kinds.includes("multihomed") && html`<span class="outcome ok"><b>multihomed</b> on one ethernet-segment</span>`}
                 <span class="muted">${sightings.length} places know it, on ${new Set(sightings.map((s) => s.node)).size} nodes</span>`}
      </div>
      ${overlay?.nodes.size > 0 &&
      html`<div class="lens-map short">
        <${Topology} graph=${graph} status=${status} checks=${checks} overlay=${overlay} legend=${false}
                     selected=${null} onSelect=${(s) => s && go(`topology/${s.type}/${encodeURIComponent(s.id)}`)} />
      </div>`}
      <${DataTable} columns=${state.answer?.columns ?? []} rows=${state.answer?.rows ?? []}
                    onNode=${(n) => go(`topology/node/${encodeURIComponent(n)}`)} />
    </${Result}>
  </div>`;
}

// --------------------------------------------------------------------------- service

const KINDS = [
  ["mac-vrf", "Bridge Domains"],
  ["ip-vrf", "Routers"],
];
const uniq = (values) => [...new Set(values)].sort(natural);
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
/** The bgp-vpn instances on a carrier's own site side: all but a gateway's WAN side. */
const siteSide = (r) => r.vpn.filter((v) => !r.wan.includes(v.id));
const wanSide = (r) => r.vpn.filter((v) => r.wan.includes(v.id));

function card(id, rows, part = "") {
  const down = rows.filter((r) => r.oper !== "up").length;
  const wan = part === "wan";
  return {
    id,
    rows,
    part,
    type: rows[0].type,
    dci: rows[0].dci,
    names: uniq(rows.map((r) => r.name)),
    sites: uniq(rows.map((r) => r.site)),
    nodes: uniq(rows.map((r) => r.node)),
    gateways: rows.filter((r) => r.gateway),
    vnis: wan ? [] : uniq(rows.flatMap((r) => r.vnis)),
    rts: wan ? [] : uniq(rows.flatMap((r) => siteSide(r).flatMap((v) => v.import_rts))),
    wanRts: part === "site" ? [] : uniq(rows.flatMap((r) => wanSide(r).flatMap((v) => v.import_rts))),
    subnets: wan ? [] : uniq(rows.flatMap((r) => r.subnets)),
    macs: rows.reduce((n, r) => n + r.macs_local, 0),
    state: down === rows.length ? "down" : down ? "mixed" : "up",
    issues: rows.filter((r) => r.issues.length).length,
    // The gateways' bgp-vpn instance for this part: their site side, or their WAN side.
    instances: part
      ? uniq(rows.filter((r) => r.gateway).flatMap((r) => (wan ? wanSide(r) : siteSide(r)).map((v) => v.id)))
      : [],
  };
}

/** The services there are, each with its carriers: one card each. A service stitched across
 *  sites is a domain per site and one between them: a card each, the gateways in their
 *  site's and in the DCI one. */
function cards(records) {
  const by = new Map();
  for (const r of records) {
    if (!by.has(r.group)) by.set(r.group, []);
    by.get(r.group).push(r);
  }
  return [...by].flatMap(([id, rows]) => {
    if (!rows[0].dci) return [card(id, rows)];
    const sites = uniq(rows.map((r) => r.site));
    return [
      ...sites.map((s) => card(`${id}|${s}`, rows.filter((r) => r.site === s), "site")),
      card(`${id}|wan`, rows.filter((r) => r.gateway), "wan"),
    ];
  });
}

/** A service's cards: its sites', then the DCI one between them. */
const order = (a, b) => (a.part === "wan") - (b.part === "wan") || natural(a.sites[0], b.sites[0]);

const Pills = ({ card }) => html`
  ${card.part === "wan" && html`<span class="pill dci">${card.dci}</span>`}
  ${card.instances.length > 0 &&
  html`<span class="pill instance" title="the bgp-vpn instance on the gateways that connects it">
    instance ${card.instances.join(", ")}</span>`}
  <span class=${`pill state-${card.state}`}>${card.state}</span>
  ${card.issues > 0 && html`<span class="pill error">${plural(card.issues, "issue")}</span>`}`;

const rts = (vpn, side) => uniq(vpn.flatMap((v) => v[side])).join(", ") || "-";

function NodeDetail({ r, go }) {
  return html`<div class="carrier-detail">
    <p><${Tables} type=${r.type} names=${[r.name]} nodes=${[r.node]} go=${go} /></p>
    ${r.ports.length > 0 &&
    html`<table class="mini">
      <thead><tr><th>port</th><th>kind</th><th>oper</th><th>VLAN</th><th>segment</th><th>to / addresses</th></tr></thead>
      <tbody>${r.ports.map(
        (p) => html`<tr>
          <td class="mono">${p.name}</td><td>${p.kind}</td>
          <td><span class=${`pill state-${p.oper === "up" ? "up" : "down"}`}>${p.oper || "?"}</span></td>
          <td>${p.vlan ?? ""}</td><td>${p.segment || p.esi || ""}</td>
          <td class="mono">${[p.peer_ni, ...(p.addresses || [])].filter(Boolean).join(", ")}</td>
        </tr>`,
      )}</tbody>
    </table>`}
    <table class="mini">
      <thead><tr><th>bgp-vpn</th><th>side</th><th>RD</th><th>import</th><th>export</th></tr></thead>
      <tbody>${r.vpn.map(
        (v) => html`<tr>
          <td>${v.id}</td><td>${r.wan.includes(v.id) ? "WAN" : r.site || "fabric"}</td><td class="mono">${v.rd || "-"}</td>
          <td class="mono">${v.import_rts.join(", ") || "-"}</td><td class="mono">${v.export_rts.join(", ") || "-"}</td>
        </tr>`,
      )}</tbody>
    </table>
    ${r.subnets.length > 0 && html`<p><span class="muted">subnets</span> <span class="mono">${r.subnets.join(", ")}</span></p>`}
    ${r.bgp_peers.length > 0 && html`<p><span class="muted">BGP peers</span> <span class="mono">${r.bgp_peers.join(", ")}</span></p>`}
    ${r.virtual_es.length > 0 && html`<p><span class="muted">virtual segments</span> ${r.virtual_es.join(", ")}</p>`}
    ${r.issues.map((i) => html`<p class="error-text">${i}</p>`)}
  </div>`;
}

function Carriers({ rows, names, open, onOpen, go }) {
  const bridged = rows[0]?.type === "mac-vrf";
  const cols = 7 + (names > 1) + bridged;
  return html`<table class="carriers">
    <thead><tr>
      <th>node</th>${names > 1 && html`<th>name</th>`}<th>oper</th><th>VNI</th><th>EVI</th><th>route-targets</th>
      <th>access / irb</th>${bridged && html`<th>MACs</th>`}<th></th>
    </tr></thead>
    <tbody>${rows.map((r) => {
      const key = `${r.node}|${r.name}`;
      const access = r.ports.filter((p) => p.kind !== "irb").map((p) => p.name + (p.segment ? ` ${p.segment}` : ""));
      const irb = r.ports.filter((p) => p.kind === "irb").map((p) => (p.peer_ni ? `${p.name} → ${p.peer_ni}` : p.name));
      const side = siteSide(r);
      return html`
        <tr class=${`carrier${open === key ? " open" : ""}${r.issues.length ? " bad" : ""}`} onClick=${() => onOpen(open === key ? "" : key)}>
          <td><span class="caret">${open === key ? "▾" : "▸"}</span>
            <a onClick=${(e) => { e.stopPropagation(); go(`topology/node/${encodeURIComponent(r.node)}`); }}>${r.node}</a>
            ${r.gateway && html` <span class="pill gw" title="stitches its site to the WAN">GW</span>`}</td>
          ${names > 1 && html`<td class="mono">${r.name}</td>`}
          <td><span class=${`pill state-${r.oper === "up" ? "up" : "down"}`}>${r.oper || "?"}</span></td>
          <td>${r.vnis.join(", ") || "-"}</td><td>${r.evis.join(", ") || "-"}</td>
          <td class="mono">${rts(side, "import_rts")}${rts(side, "export_rts") !== rts(side, "import_rts") ? html` <span class="muted">/ ${rts(side, "export_rts")}</span>` : ""}</td>
          <td class="mono">${[...access, ...irb].join(", ")}</td>
          ${bridged && html`<td>${r.macs_local} <span class="muted">/ ${r.macs_remote}</span></td>`}
          <td>${r.issues.length > 0 && html`<span class="pill error">${plural(r.issues.length, "issue")}</span>`}</td>
        </tr>
        ${open === key && html`<tr class="carrier-row"><td colspan=${cols}><${NodeDetail} r=${r} go=${go} /></td></tr>`}`;
    })}</tbody>
  </table>`;
}

function Wan({ gateways, go }) {
  return html`<table class="carriers">
    <thead><tr><th>gateway</th><th>site</th><th>bgp-vpn</th><th>RD</th><th>import</th><th>export</th></tr></thead>
    <tbody>${gateways.flatMap((r) =>
      wanSide(r).map(
        (v) => html`<tr>
          <td><a onClick=${() => go(`topology/node/${encodeURIComponent(r.node)}`)}>${r.node}</a></td>
          <td>${r.site}</td><td>${v.id}</td><td class="mono">${v.rd || "-"}</td>
          <td class="mono">${v.import_rts.join(", ") || "-"}</td><td class="mono">${v.export_rts.join(", ") || "-"}</td>
        </tr>`,
      ),
    )}</tbody>
  </table>`;
}

function facts(card) {
  const wan = card.part === "wan";
  const sites = card.sites.filter(Boolean);
  return [
    sites.length > 1 ? `${sites.length} sites` : sites[0],
    !wan && plural(card.nodes.length, "node"),
    card.gateways.length > 0 && plural(card.gateways.length, "gateway"),
    card.vnis.length > 0 && `VNI ${card.vnis.join(", ")}`,
    card.rts.length > 0 && `RT ${card.rts.join(", ")}`,
    card.wanRts.length > 0 && `WAN RT ${card.wanRts.join(", ")}`,
    !wan && card.type === "mac-vrf" && plural(card.macs, "local MAC"),
  ].filter(Boolean);
}

/** The tables behind a service, as reports filtered to its instances on its nodes. */
const TABLES = {
  "mac-vrf": [["mac", "MAC table"]],
  "ip-vrf": [
    ["arp", "ARP"],
    ["nd", "ND"],
    ["ipv4-rib", "IPv4 RIB"],
    ["ipv6-rib", "IPv6 RIB"],
  ],
};

function Tables({ type, names, nodes, go }) {
  const any = (values) => values.map((v) => `=${v}`).join("|");
  return html`<span class="service-tables">${(TABLES[type] ?? []).map(
    ([report, label]) => html`<button class="small" title=${`${label} of ${names.join(", ")} on ${nodes.join(", ")}`}
      onClick=${(e) => {
        e.stopPropagation();
        go(withQuery(`report/${report}`, { "f.NI": any(names), "f.Node": any(nodes) }));
      }}>${label}</button>`,
  )}</span>`;
}

function Card({ card, expanded, onToggle, go }) {
  const [open, setOpen] = useState("");
  const bySite = card.sites.map((s) => [s, card.rows.filter((r) => r.site === s)]);
  return html`<section class=${`service-card${expanded ? " expanded" : ""}${card.issues ? " bad" : ""}`}>
    <header onClick=${onToggle}>
      <span class="caret">${expanded ? "▾" : "▸"}</span>
      <h3 class="mono">${card.names.join(" / ")}</h3>
      <${Pills} card=${card} />
      <span class="card-facts muted">${facts(card).join(" · ")}</span>
      ${card.part !== "wan" && card.type === "ip-vrf" && card.subnets.map((s) => html`<span class="pill subnet">${s}</span>`)}
      <${Tables} type=${card.type} names=${card.names} nodes=${card.nodes} go=${go} />
    </header>
    ${expanded &&
    html`<div class="card-body">
      ${card.part !== "wan" &&
      bySite.map(
        ([site, rows]) => html`<div class="card-section">
          ${(site || card.dci) && html`<h4>${site ? site[0].toUpperCase() + site.slice(1) : "Fabric"}</h4>`}
          <${Carriers} rows=${rows} names=${card.names.length} open=${open} onOpen=${setOpen} go=${go} />
        </div>`,
      )}
      ${card.dci && card.part !== "site" &&
      html`<div class="card-section">
        <h4>DCI <span class="muted">the gateways' WAN side, stitched to their site's</span></h4>
        <${Wan} gateways=${card.gateways} go=${go} />
      </div>`}
    </div>`}
  </section>`;
}

export function ServiceView({ query, go, at }) {
  const state = useLens("service", {}, at, true);
  const all = useMemo(() => cards(state.answer?.records ?? []), [state.answer]);
  const kind = query.kind || (query.name && all.find((c) => c.names.includes(query.name))?.type) || "mac-vrf";
  const [find, setFind] = useState(query.name || "");
  const term = find.trim().toLowerCase();
  const shown = all
    .filter((c) => c.type === kind)
    .filter((c) => !term || c.names.some((n) => n.toLowerCase().includes(term)) || c.nodes.some((n) => n.toLowerCase().includes(term)))
    .sort((a, b) => b.issues - a.issues || natural(a.names[0], b.names[0]) || order(a, b));
  const openSet = new Set((query.open || "").split(",").filter(Boolean));
  if (query.name) for (const c of shown) if (c.names.includes(query.name)) openSet.add(c.id);
  const toggle = (id) => {
    const next = new Set(openSet);
    next.has(id) ? next.delete(id) : next.add(id);
    go(withQuery("lens/service", { kind, open: [...next].join(",") }));
  };
  const counts = Object.fromEntries(KINDS.map(([k]) => [k, all.filter((c) => c.type === k).length]));
  const sites = uniq(all.flatMap((c) => c.sites).filter(Boolean));
  return html`<div class="lens">
    <div class="filters">
      <span class="segmented">${KINDS.map(
        ([k, label]) => html`<button class=${k === kind ? "on" : ""} onClick=${() => go(withQuery("lens/service", { kind: k }))}>
          ${label} <span class="tab-count">${counts[k]}</span></button>`,
      )}</span>
      <input class="card-find" value=${find} onInput=${(e) => setFind(e.target.value)}
             placeholder="Filter by name or node" aria-label="Filter services" />
      ${sites.length > 1 && html`<span class="muted">${sites.length} sites, from which loopbacks each node has a host route to</span>`}
    </div>
    <${Result} state=${state}>
      ${shown.length
        ? shown.map((c) => html`<${Card} key=${c.id} card=${c} expanded=${openSet.has(c.id)} onToggle=${() => toggle(c.id)} go=${go} />`)
        : html`<p class="muted">No ${KINDS.find(([k]) => k === kind)[1].toLowerCase()}${term ? ` match ${find}` : ""}.</p>`}
    </${Result}>
  </div>`;
}

// --------------------------------------------------------------------------- endpoints

export function EndpointsView({ query, go, at }) {
  const state = useLens("endpoints", {}, at, true);
  const setFilter = (f) => history.replaceState(null, "", `#${withQuery("lens/endpoints", { q: f })}`);
  const [filter, setOwn] = useState(query.q || "");
  return html`<div class="lens">
    <${Result} state=${state}>
      <${DataTable} columns=${state.answer?.columns ?? []} rows=${state.answer?.rows ?? []} filter=${filter}
                    onFilter=${(f) => {
                      setOwn(f);
                      setFilter(f);
                    }}
                    onNode=${(n) => go(`topology/node/${encodeURIComponent(n)}`)} />
    </${Result}>
  </div>`;
}

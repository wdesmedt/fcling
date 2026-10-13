// The lenses: where an address is, how traffic would get there, one service everywhere, every host.

import { html, useEffect, useMemo, useState } from "./vendor/preact-htm.module.js";
import { get, natural, withQuery } from "./api.js";
import { DataTable } from "./table.js";
import { Topology } from "./topology.js";

export const LENS_VIEWS = {
  where: { title: "Where", help: "Every place in the fabric that knows an IP or MAC address" },
  path: { title: "Path", help: "How the fabric would forward to an address, hop by hop, from its tables" },
  service: { title: "Service", help: "One mac-vrf or ip-vrf on every node that carries it, and what disagrees" },
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

export function ServiceView({ query, go, at }) {
  const services = useServices(at);
  const ready = Boolean(query.name);
  const state = useLens("service", { name: query.name }, at, ready);
  const rows = state.answer?.records ?? [];
  const issues = rows.filter((r) => r.issues.length);
  return html`<div class="lens">
    <${Form} values=${{ name: query.name }} onSubmit=${(v) => go(withQuery("lens/service", v))}
             fields=${[{ name: "name", label: "Service", placeholder: "macvrf-101, ipvrf-1", list: "fcli-services" }]} />
    <datalist id="fcli-services">${services.map(([n, t]) => html`<option value=${n}>${t}</option>`)}</datalist>
    ${!ready &&
    html`<div class="service-list">${services.map(
      ([n, t]) => html`<a class="chip" onClick=${() => go(withQuery("lens/service", { name: n }))}>${n} <span class="muted">${t}</span></a>`,
    )}</div>`}
    <${Result} state=${state}>
      <div class="summary-line">
        <span class="muted">${rows[0]?.type} on ${rows.length} node${rows.length === 1 ? "" : "s"}</span>
        ${issues.length
          ? html`<span class="outcome bad">${issues.length} node${issues.length > 1 ? "s" : ""} disagree</span>`
          : html`<span class="outcome ok">consistent on every node</span>`}
      </div>
      <${DataTable} columns=${state.answer?.columns ?? []} rows=${state.answer?.rows ?? []}
                    onNode=${(n) => go(`topology/node/${encodeURIComponent(n)}`)} />
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

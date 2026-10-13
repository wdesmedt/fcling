// Any report as a table: live from the engine, from the history at an instant, or collected on the spot.

import { html, useEffect, useRef, useState } from "./vendor/preact-htm.module.js";
import { follow, get, withQuery } from "./api.js";
import { DataTable } from "./table.js";

/** How the navigation groups the reports. */
export const REPORT_GROUPS = [
  ["Platform", ["sys-info", "config-commits", "components", "resources", "transceivers"]],
  ["Interfaces", ["subif", "lag", "ifstats", "lldp"]],
  ["Routing", ["bgp-peers", "ipv4-rib", "ipv6-rib", "static-routes", "routing-pol", "bfd", "isis", "ospf", "tunnel-table"]],
  ["Services", ["ni", "irb", "mac", "arp", "nd", "es", "es-dest", "vxlan"]],
];

/** Reports the navigation leaves out: the Services lens shows them end to end. */
export const UNLISTED = new Set(["services", "bridge-domains", "routers"]);

/** What the reports are called on screen; the CLI keeps the short names. */
export const REPORT_TITLES = {
  "sys-info": "System Information",
  "config-commits": "Configuration Commits",
  components: "Components",
  resources: "Resource Utilization",
  transceivers: "Transceivers",
  subif: "Subinterfaces",
  lag: "LAGs",
  ifstats: "Interface Rates",
  lldp: "LLDP Neighbors",
  "bgp-peers": "BGP Peers",
  "ipv4-rib": "IPv4 Route Table",
  "ipv6-rib": "IPv6 Route Table",
  "static-routes": "Static Routes",
  "routing-pol": "Routing Policies",
  bfd: "BFD Sessions",
  isis: "IS-IS",
  ospf: "OSPF",
  "tunnel-table": "Tunnel Table",
  "bgp-rib-evpn-1": "BGP RIB EVPN Type 1 – A-D",
  "bgp-rib-evpn-2": "BGP RIB EVPN Type 2 – MAC/IP",
  "bgp-rib-evpn-3": "BGP RIB EVPN Type 3 – IMET",
  "bgp-rib-evpn-4": "BGP RIB EVPN Type 4 – ES",
  "bgp-rib-evpn-5": "BGP RIB EVPN Type 5 – IP Prefix",
  "bgp-rib-ipv4-unicast": "BGP RIB IPv4 Unicast",
  "bgp-rib-ipv6-unicast": "BGP RIB IPv6 Unicast",
  "bgp-rib-l3vpn-ipv4-unicast": "BGP RIB L3VPN IPv4",
  "bgp-rib-l3vpn-ipv6-unicast": "BGP RIB L3VPN IPv6",
  services: "Services",
  "bridge-domains": "Bridge Domains",
  routers: "Routers",
  ni: "Network Instances",
  irb: "IRB Interfaces",
  mac: "MAC Table",
  arp: "ARP Table",
  nd: "IPv6 Neighbors",
  es: "Ethernet Segments",
  "es-dest": "Segment Destinations",
  vxlan: "VXLAN Interfaces",
};

export const reportTitle = (name) => REPORT_TITLES[name] ?? name;

/** A streamed report's rows change with every poll; refresh no more often than this. */
const SETTLE_MS = 2000;

export function ReportView({ name, query, go, at, catalog }) {
  const spec = catalog.find((r) => r.name === name);
  const params = Object.fromEntries((spec?.params ?? []).map((p) => [p.name, query[p.name] ?? ""]));
  const [state, setState] = useState({ answer: null, error: "", loading: true });
  const [draft, setDraft] = useState(params);
  const pending = useRef(null);
  // The filters live in the URL - `q` for the row filter, `f.<column>` for a column's - so a
  // filtered table can be bookmarked or shared; updating them does not reload the report.
  const [rowFilter, setRowFilter] = useState(query.q ?? "");
  const [columnFilters, setColumnFilters] = useState(() =>
    Object.fromEntries(Object.entries(query).filter(([k]) => k.startsWith("f.")).map(([k, v]) => [k.slice(2), v])),
  );
  const keep = (q, columns) => {
    const filters = Object.fromEntries(Object.entries(columns).map(([title, v]) => [`f.${title}`, v]));
    history.replaceState(null, "", `#/${withQuery(`report/${name}`, { ...params, q, ...filters })}`);
  };
  const key = JSON.stringify([name, params, at]);

  useEffect(() => {
    let current = true;
    const load = () =>
      get(`/api/reports/${name}`, { ...params, at, table: 1 })
        .then((answer) => current && setState({ answer, error: "", loading: false }))
        .catch((e) => current && setState({ answer: null, error: e.message, loading: false }));
    setState((s) => ({ ...s, loading: true }));
    setDraft(params);
    load();
    // Live: the engine says when the report changed.
    const live = !at && spec?.streamed && Object.values(params).every((v) => !v);
    const stop = live
      ? follow([name], (type) => {
          if (type !== "change" || pending.current) return;
          pending.current = setTimeout(() => {
            pending.current = null;
            load();
          }, SETTLE_MS);
        }, () => {})
      : () => {};
    return () => {
      current = false;
      stop();
      clearTimeout(pending.current);
      pending.current = null;
    };
  }, [key]);

  if (!spec) return html`<p class="error-text">No report ${name}.</p>`;
  const answer = state.answer;
  const errors = Object.entries(answer?.errors ?? {});
  return html`<div class="report-view">
    <div class="view-head">
      <h2>${reportTitle(name)}</h2>
      ${reportTitle(name) !== name && html`<code class="muted">${name}</code>`}
      <span class="muted">${spec.help}</span>
      ${answer && html`<span class=${`pill source-${answer.source}`}>${answer.source}</span>`}
    </div>
    ${spec.params.length > 0 &&
    html`<form class="lens-form" onSubmit=${(e) => {
      e.preventDefault();
      go(withQuery(`report/${name}`, draft));
    }}>
      ${spec.params.map(
        (p) => html`<label><span>${p.name} <i class="muted">${p.help}</i></span>
          <input value=${draft[p.name] ?? ""} placeholder=${p.default ?? ""}
                 onInput=${(e) => setDraft({ ...draft, [p.name]: e.target.value })} /></label>`,
      )}
      <button class="primary" type="submit">Show</button>
    </form>`}
    ${errors.length > 0 &&
    html`<div class="node-errors">${errors.map(([n, e]) => html`<div><b>${n}</b>: ${e}</div>`)}</div>`}
    ${state.error
      ? html`<p class="error-text">${state.error}</p>`
      : !answer
        ? html`<p class="muted">Reading ${name}…</p>`
        : html`<${DataTable} key=${name} columns=${answer.columns} rows=${answer.rows} flash=${key}
                             filter=${rowFilter} onFilter=${(q) => {
                               setRowFilter(q);
                               keep(q, columnFilters);
                             }}
                             columnFilters=${columnFilters} onColumnFilters=${(c) => {
                               setColumnFilters(c);
                               keep(rowFilter, c);
                             }}
                             onNode=${(n) => go(`topology/node/${encodeURIComponent(n)}`)} />`}
  </div>`;
}

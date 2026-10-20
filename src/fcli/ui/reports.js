// Any report as a table: live from the engine, from the history at an instant, or collected on the spot.

import { html, useEffect, useMemo, useRef, useState } from "./vendor/preact-htm.module.js";
import { clock, follow, get, getReportProgress, withQuery } from "./api.js";
import { ConfigDiff } from "./configdiff.js";
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
  "bgp-rib-evpn": "BGP RIB EVPN (all route types)",
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

const SETTLE_MS = 2000;
const LARGE_SETTLE_MS = 5000;
const LARGE_TABLE = 20_000;
const PROGRESS_MIN_NODES = 4;

function useReportProgress(name, queryReady, at, nodeCount) {
  if (at) return false;
  if (isBgpRib(name) && queryReady) return true;
  return nodeCount >= PROGRESS_MIN_NODES;
}

const EVPN_ROUTE_TYPES = {
  1: "ethernet-ad",
  2: "mac-ip",
  3: "imet",
  4: "ethernet-segment",
  5: "ip-prefix",
};

const isBgpRib = (name) => name?.startsWith("bgp-rib-");

/** Whether a BGP RIB query was asked for (keys, ni, or list all). */
export function bgpQueryReady(spec, params) {
  if (!spec?.needs_query) return true;
  if (params.list_all === "all") return true;
  if ((params.neighbor ?? "").trim()) return true;
  if (!spec.rib_evpn && (params.ni ?? "").trim()) return true;
  return (spec.rib_keys ?? []).some((k) => (params[k] ?? "").trim());
}

function ribFields(spec) {
  const keys = new Set(spec.rib_keys ?? []);
  const paths = (spec.params ?? []).find((p) => p.name === "paths");
  const ni =
    !spec.rib_evpn && (spec.params ?? []).find((p) => p.name === "ni");
  const direction = (spec.params ?? []).find((p) => p.name === "direction");
  const neighbor = (spec.params ?? []).find((p) => p.name === "neighbor");
  const routeType = (spec.params ?? []).find((p) => p.name === "route_type");
  const scoped = new Set(["paths", "ni", "direction", "neighbor", "route_type", "list_all"]);
  const keyParams = (spec.params ?? []).filter((p) => keys.has(p.name) && !scoped.has(p.name));
  return { paths, ni, direction, neighbor, routeType, keyParams };
}

function yangName(param) {
  return param.name.replace(/_/g, "-");
}

/** ``node`` / ``inv`` scope the fabric; they are not report parameters. */
function scopeFromQuery(query) {
  const scope = {};
  if (query.node) scope.node = query.node;
  if (query.inv) scope.inv = query.inv;
  return scope;
}

/**
 * The commits with a button each for the diff against the configuration before: by when
 * the commit was made, as its id may have been some other commit's before.
 */
function withDiffs(columns, rows) {
  const [id, started, ended] = ["id", "started", "ended"].map((c) => columns.indexOf(c));
  return {
    columns: [...columns, ""],
    rows: rows.map((row) => {
      const at = row[ended] || row[started];
      return [...row, {
        kind: "action",
        label: "diff vs previous",
        node: row[0],
        id: row[id],
        at,
        disabled: !at,
        title: at ? "What this commit changed: the configuration kept after it against the one kept before" : "The node gives no time for this commit",
      }];
    }),
  };
}

export function ReportView({ name, query, go, at, catalog, nodeCount = 0 }) {
  const spec = catalog.find((r) => r.name === name);
  const params = Object.fromEntries((spec?.params ?? []).map((p) => [p.name, query[p.name] ?? ""]));
  const scope = scopeFromQuery(query);
  const [state, setState] = useState({ answer: null, error: "", loading: false });
  const [draft, setDraft] = useState(params);
  const [progress, setProgress] = useState(null);
  const [diffOf, setDiffOf] = useState(null);
  const pending = useRef(null);
  const settleMs = useRef(SETTLE_MS);
  const [rowFilter, setRowFilter] = useState(query.q ?? "");
  const [columnFilters, setColumnFilters] = useState(() =>
    Object.fromEntries(Object.entries(query).filter(([k]) => k.startsWith("f.")).map(([k, v]) => [k.slice(2), v])),
  );
  const keep = (q, columns, nextParams = params) => {
    const filters = Object.fromEntries(Object.entries(columns).map(([title, v]) => [`f.${title}`, v]));
    history.replaceState(null, "", `#/${withQuery(`report/${name}`, { ...nextParams, ...scope, run: query.run, q, ...filters })}`);
  };
  const needsQuery = Boolean(spec?.needs_query);
  // A RIB is read only when Look up or List all routes says so (``run`` in the
  // URL): opening it, from a bgp-peers link say, fills the form and waits.
  const queryReady = bgpQueryReady(spec, params) && (!needsQuery || Boolean(query.run));
  const key = JSON.stringify([name, params, scope, at, needsQuery ? query.run : ""]);

  const load = (useProgress, background = false) => {
    setProgress(null);
    setState((s) => ({
      ...s,
      loading: background ? !s.answer : true,
      error: "",
      ...(needsQuery && !background ? { answer: null } : {}),
    }));
    const apiParams = { ...params, ...scope, at, table: 1 };
    const finish = (answer) => {
      settleMs.current = (answer?.rows?.length ?? 0) >= LARGE_TABLE ? LARGE_SETTLE_MS : SETTLE_MS;
      setState({ answer: { ...answer, loading: false }, error: "", loading: false });
      setProgress(null);
    };
    const fail = (e) => {
      setState({ answer: null, error: e.message, loading: false });
      setProgress(null);
    };
    if (useProgress && !at) {
      return getReportProgress(name, apiParams, {
        onProgress: (p) => setProgress(p),
        onPartial: (partial) => {
          // A partial carries only the rows since the last one when it says append.
          setState((s) => ({
            ...s,
            answer: {
              ...partial,
              rows: partial.append && s.answer?.loading ? s.answer.rows.concat(partial.rows) : partial.rows,
              loading: true,
            },
            error: "",
            loading: true,
          }));
        },
      })
        .then(finish)
        .catch(fail);
    }
    return get(`/api/reports/${name}`, apiParams).then(finish).catch(fail);
  };

  useEffect(() => {
    let current = true;
    setDraft(params);
    if (!queryReady) {
      setState({ answer: null, error: "", loading: false });
      return () => {
        current = false;
      };
    }
    load(useReportProgress(name, queryReady, at, nodeCount)).then(() => {
      if (!current) return;
    });
    const live =
      !at &&
      spec?.streamed &&
      queryReady &&
      Object.entries(params).every(([k, v]) => {
        const def = spec.params.find((p) => p.name === k)?.default ?? "";
        return v === def || v === "";
      });
    const stop = live
      ? follow([name], (type) => {
          if (type !== "change" || pending.current) return;
          pending.current = setTimeout(() => {
            pending.current = null;
            if (current) load(false, true);
          }, settleMs.current);
        }, () => {})
      : () => {};
    return () => {
      current = false;
      stop();
      clearTimeout(pending.current);
      pending.current = null;
    };
  }, [key, queryReady]);

  useEffect(() => setDiffOf(null), [name]);
  const commits = name === "config-commits";
  const table = useMemo(
    () =>
      commits && Array.isArray(state.answer?.columns) && state.answer.columns.length
        ? withDiffs(state.answer.columns, state.answer.rows ?? [])
        : { columns: state.answer?.columns, rows: state.answer?.rows },
    [commits, state.answer],
  );

  if (!spec) return html`<p class="error-text">No report ${name}.</p>`;
  const answer = state.answer;
  const errors = Object.entries(answer?.errors ?? {});
  const { paths, ni, direction, neighbor, routeType, keyParams } = ribFields(spec);
  const collecting = Boolean(answer?.loading);
  const hasTable = Array.isArray(answer?.columns) && answer.columns.length > 0;
  const rowCount = answer?.rows?.length ?? 0;
  const searching = needsQuery && queryReady && (state.loading || progress || collecting);
  const searchLabel =
    neighbor && params.neighbor
      ? `Looking up ${params.direction || "received"} routes from ${params.neighbor}${query.node ? ` on ${query.node}` : ""}…`
      : "Looking up routes…";
  const submit = (next, listAll = false) => {
    const merged = {
      ...next,
      ...scope,
      ni: spec.rib_evpn ? "" : next.ni,
      ...(listAll ? { list_all: "all" } : { list_all: "" }),
      // New each time, so looking up the same keys again reads them again.
      run: Date.now().toString(36),
    };
    go(withQuery(`report/${name}`, merged));
  };

  return html`<div class=${`report-view${collecting && rowCount ? " is-querying" : ""}`}>
    <div class="view-head">
      <h2>${reportTitle(name)}</h2>
      ${reportTitle(name) !== name && html`<code class="muted">${name}</code>`}
      <span class="muted">${spec.help}</span>
      ${answer && html`<span class=${`pill source-${answer.source}`}>${answer.source}</span>`}
      ${errors.length > 0 &&
      html`<span class="report-node-hint muted small" title=${errors.map(([n, e]) => `${n}: ${e}`).join("\n")}>
        ${errors.length === 1 ? "1 node unreadable" : `${errors.length} nodes unreadable`} —
        <a href="#/status" onClick=${(e) => {
          e.preventDefault();
          go("status");
        }}>Nodes</a>
      </span>`}
    </div>
    ${spec.needs_query &&
    html`<div class=${`rib-query${searching ? " searching" : ""}`}>
      <p class="rib-query-lead muted">Match route list keys on the node (<code>*</code> allowed), or list the whole RIB.</p>
      <form class="rib-query-form" onSubmit=${(e) => {
        e.preventDefault();
        submit(draft);
      }}>
        <div class="rib-keys">
          ${neighbor &&
          html`<div class="rib-key-row">
            <span class="rib-key-name">neighbor</span>
            <span class="rib-key-field"><input class="rib-key-input" value=${draft.neighbor ?? ""}
                   onInput=${(e) => setDraft({ ...draft, neighbor: e.target.value })} /></span>
          </div>`}
          ${direction &&
          html`<div class="rib-key-row">
            <span class="rib-key-name">direction</span>
            <span class="rib-key-field"><select class="rib-key-input" value=${draft.direction || "received"}
                    onChange=${(e) => setDraft({ ...draft, direction: e.target.value })}>
              <option value="received">received</option>
              <option value="active">active</option>
              <option value="advertised">advertised</option>
            </select></span>
          </div>`}
          ${paths &&
          html`<div class="rib-key-row">
            <span class="rib-key-name">paths</span>
            <span class="rib-key-field"><select class="rib-key-input" value=${draft.paths || "used"}
                    onChange=${(e) => setDraft({ ...draft, paths: e.target.value })}>
              <option value="used">used</option>
              <option value="all">all</option>
            </select></span>
          </div>`}
          ${routeType &&
          html`<div class="rib-key-row">
            <span class="rib-key-name">route-type</span>
            <span class="rib-key-field"><select class="rib-key-input" value=${draft.route_type ?? ""}
                    onChange=${(e) => setDraft({ ...draft, route_type: e.target.value })}>
              <option value="">all types</option>
              ${Object.entries(EVPN_ROUTE_TYPES).map(
                ([value, label]) => html`<option value=${value}>${value} · ${label}</option>`,
              )}
            </select></span>
          </div>`}
          ${ni &&
          html`<div class="rib-key-row">
            <span class="rib-key-name">network-instance</span>
            <span class="rib-key-field"><input class="rib-key-input" value=${draft.ni ?? ""} placeholder="*"
                   onInput=${(e) => setDraft({ ...draft, ni: e.target.value })} /></span>
          </div>`}
          ${keyParams.map(
            (p) => html`<div class="rib-key-row" key=${p.name}>
              <label class="rib-key-name" for=${`rib-${name}-${p.name}`}>${yangName(p)}</label>
              <span class="rib-key-field"><input id=${`rib-${name}-${p.name}`} class="rib-key-input" value=${draft[p.name] ?? ""}
                     title=${p.help} placeholder=${p.help.split(";")[0]}
                     onInput=${(e) => setDraft({ ...draft, [p.name]: e.target.value })} /></span>
            </div>`,
          )}
        </div>
        <div class="form-actions">
          <button class="primary" type="submit">Look up</button>
          <button class="warn" type="button" onClick=${() => submit(draft, true)}>List all routes</button>
        </div>
      </form>
    </div>`}
    ${!spec.needs_query && spec.params.length > 0 &&
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
    ${!queryReady &&
    html`<p class="muted">${bgpQueryReady(spec, draft)
      ? html`Press <b>Look up</b> to read these routes, or change the keys first.`
      : html`Enter route keys or choose <b>List all routes</b> to load this RIB.`}</p>`}
    ${searching &&
    html`<div class=${`rib-searching${rowCount || hasTable ? " rib-searching-compact" : ""}`} role="status" aria-busy="true">
      <span class="rib-search-spinner" aria-hidden="true"></span>
      <div class="rib-search-body">
        <strong>${searchLabel}</strong>
        ${rowCount > 0 &&
        html`<span class="muted"> · ${rowCount.toLocaleString()} rows so far</span>`}
        <div class="load-progress${progress ? "" : " indeterminate"}">
          <div class="load-progress-bar" style=${progress
            ? { width: `${(100 * progress.done) / progress.total}%` }
            : null}></div>
          <span class="muted">${progress
            ? `${progress.done} / ${progress.total} nodes — ${progress.node}`
            : collecting
              ? "Still loading routes…"
              : "Querying nodes…"}</span>
        </div>
      </div>
    </div>`}
    ${!searching && state.loading && !answer && queryReady && html`<p class="muted">Reading ${reportTitle(name)}…</p>`}
    ${answer?.message &&
    html`<p class=${`muted${answer.truncated ? " note" : ""}`}>${answer.message}</p>`}
    ${diffOf &&
    html`<section class="commit-diff">
      <div class="commit-diff-title">
        <strong>${diffOf.node} · commit ${diffOf.id}</strong>
        <span class="muted">made ${clock(Date.parse(diffOf.at) / 1000)}, against the configuration kept before it</span>
        <button class="link-button" onClick=${() => setDiffOf(null)}>Close</button>
      </div>
      <${ConfigDiff} key=${`${diffOf.node} ${diffOf.at}`} node=${diffOf.node} at=${diffOf.at} />
    </section>`}
    ${state.error
      ? html`<p class="error-text">${state.error}</p>`
      : answer?.message && !hasTable
        ? null
        : hasTable
          ? html`<${DataTable} key=${name} columns=${table.columns} rows=${table.rows ?? []} flash=${key}
                               onAction=${commits ? setDiffOf : undefined}
                               filter=${rowFilter} onFilter=${(q) => {
                                 setRowFilter(q);
                                 keep(q, columnFilters);
                               }}
                               columnFilters=${columnFilters} onColumnFilters=${(c) => {
                                 setColumnFilters(c);
                                 keep(rowFilter, c);
                               }}
                               onNode=${(n) => go(`topology/node/${encodeURIComponent(n)}`)} go=${go} />`
          : null}
  </div>`;
}

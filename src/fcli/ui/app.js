// fcling's web UI: the shell - routing, the header, the navigation panel, the agent - around the views.

import { html, render, useEffect, useMemo, useRef, useState } from "./vendor/preact-htm.module.js";
import { follow, get, hashQuery } from "./api.js";
import { ChatPanel } from "./chat.js";
import { HistoryView } from "./history.js";
import { Incidents } from "./incidents.js";
import { EndpointsView, LENS_VIEWS, PathView, ServiceView, WhereView } from "./lenses.js";
import { Nav } from "./nav.js";
import { ReportView } from "./reports.js";
import { Details, OverlayControls, Topology, serviceOverlay, useOverlay } from "./topology.js";

/** Reports whose changes can change the topology or the incidents; the rest would only wake them up. */
const WATCHED = ["checks", "lldp", "subif", "services", "es", "lag"];
/** Changes come in bursts: refresh once they settle, not once per change. */
const SETTLE_MS = 1500;
/** Rates change on every poll and are no history, so no event says so: traffic is fetched this often. */
const TRAFFIC_MS = 10_000;

function parseHash() {
  const [path] = location.hash.replace(/^#\/?/, "").split("?");
  const [view = "topology", ...rest] = path.split("/").filter(Boolean);
  const query = hashQuery();
  if (view === "topology") {
    const [type, ...id] = rest;
    return { view, query, selection: type && id.length ? { type, id: decodeURIComponent(id.join("/")) } : null };
  }
  if (view === "lens" || view === "report") return { view, name: decodeURIComponent(rest[0] || ""), query };
  if (view === "incidents" || view === "history") return { view, query };
  return { view: "topology", query: {}, selection: null };
}

function useRoute() {
  const [route, setRoute] = useState(parseHash);
  useEffect(() => {
    const changed = () => setRoute(parseHash());
    addEventListener("hashchange", changed);
    return () => removeEventListener("hashchange", changed);
  }, []);
  const go = (path) => {
    const hash = `#/${path}`;
    if (hash !== location.hash) location.hash = hash;
    else setRoute(parseHash());
  };
  return [route, go];
}

function stored(key, fallback) {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function store(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // A preference that does not stick is no loss.
  }
}

function useTheme() {
  const [theme, setTheme] = useState(() => stored("fcli-theme", ""));
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    store("fcli-theme", theme || null);
  }, [theme]);
  return () => {
    const dark = theme ? theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    setTheme(dark ? "light" : "dark");
  };
}

function TimeControl({ at, onAt }) {
  const [text, setText] = useState(at);
  useEffect(() => setText(at), [at]);
  return html`<form class=${`time${at ? " past" : ""}`} onSubmit=${(e) => {
    e.preventDefault();
    onAt(text.trim());
  }}>
    ${at
      ? html`<span class="when">at <b>${at}</b></span><button type="button" onClick=${() => onAt("")}>Back to live</button>`
      : html`<input value=${text} onInput=${(e) => setText(e.target.value)} placeholder="Go back: 30m, 2h, 14:02"
                    aria-label="Show the fabric as it was at" />`}
  </form>`;
}

/** Whether there is a screen to go back or forward to; where the browser cannot say, there is. */
function useHistorySteps() {
  const nav = globalThis.navigation;
  const read = () => ({ back: nav?.canGoBack ?? true, forward: nav?.canGoForward ?? true });
  const [steps, setSteps] = useState(read);
  useEffect(() => {
    if (!nav) return;
    const changed = () => setSteps(read());
    nav.addEventListener("currententrychange", changed);
    return () => nav.removeEventListener("currententrychange", changed);
  }, []);
  return steps;
}

/** Two spines over three leaves: the logo, as the favicon has it. */
const Mark = () => html`<svg class="mark-logo" viewBox="0 0 24 24" aria-hidden="true">
  <defs>
    <linearGradient id="fcling-mark" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#2563eb" /><stop offset="1" stop-color="#06b6d4" />
    </linearGradient>
  </defs>
  <rect width="24" height="24" rx="6" fill="url(#fcling-mark)" />
  <path d="M8.5 7.5 5.5 16.5M8.5 7.5 12 16.5M8.5 7.5 18.5 16.5M15.5 7.5 5.5 16.5M15.5 7.5 12 16.5M15.5 7.5 18.5 16.5"
        stroke="#fff" stroke-opacity="0.55" stroke-width="1.1" />
  <g fill="#fff"><circle cx="8.5" cy="7.5" r="2.1" /><circle cx="15.5" cy="7.5" r="2.1" />
    <circle cx="5.5" cy="16.5" r="1.9" /><circle cx="12" cy="16.5" r="1.9" /><circle cx="18.5" cy="16.5" r="1.9" /></g>
</svg>`;

function Header({ status, connection, at, onAt, toggleTheme, onMenu, onChat, chatOpen }) {
  const nodes = status?.nodes ?? [];
  useEffect(() => {
    document.title = status?.fabric ? `fcling · ${status.fabric}` : "fcling";
  }, [status?.fabric]);
  const down = nodes.filter((n) => n.status !== "live");
  const steps = useHistorySteps();
  return html`<header class="top">
    <button class="icon menu" onClick=${onMenu} title="Show or hide the navigation" aria-label="Navigation">☰</button>
    <span class="history-steps">
      <button class="icon" onClick=${() => history.back()} disabled=${!steps.back}
              title="Back to the previous screen (Alt+←)" aria-label="Back">←</button>
      <button class="icon" onClick=${() => history.forward()} disabled=${!steps.forward}
              title="Forward to the next screen (Alt+→)" aria-label="Forward">→</button>
    </span>
    <a class="brand" href="#/topology" title="fcling: the fabric, live">
      <${Mark} />
      <span class="logo">fc<span class="logo-ling">ling</span></span>
      ${status?.fabric && html`<span class="fabric">${status.fabric}</span>`}
    </a>
    <div class="spacer"></div>
    <span class=${`nodes-state${down.length ? " bad" : ""}`}
          title=${down.map((n) => `${n.name}: ${n.status}${n.error ? ` (${n.error})` : ""}`).join("\n")}>
      ${nodes.length - down.length}/${nodes.length} nodes live
    </span>
    <${TimeControl} at=${at} onAt=${onAt} />
    <span class=${`connection ${at ? "paused" : connection}`}
          title=${at ? "showing the past: not following changes" : `events: ${connection}`}></span>
    <button class=${`ask-button${chatOpen ? " on" : ""}`} onClick=${onChat} title="Ask the agent about the fabric">✦ Ask</button>
    <button class="icon" onClick=${toggleTheme} title="Light or dark">◐</button>
  </header>`;
}

/** What the user is looking at, in words: the agent is told. */
function describe(route, at) {
  let view = route.view;
  if (route.view === "topology" && route.selection) view = `topology, ${route.selection.type} ${route.selection.id} selected`;
  else if (route.view === "report") view = `report ${route.name}`;
  else if (route.view === "lens") {
    const args = Object.entries(route.query).filter(([k, v]) => v && k !== "q").map(([k, v]) => `${k}=${v}`);
    view = `${route.name} lens${args.length ? ` ${args.join(" ")}` : ""}`;
  }
  return { view, at };
}

function App() {
  const [route, go] = useRoute();
  const toggleTheme = useTheme();
  const [at, setAt] = useState("");
  const [status, setStatus] = useState(null);
  const [data, setData] = useState(null);
  const [catalog, setCatalog] = useState([]);
  const [error, setError] = useState("");
  const [connection, setConnection] = useState("connecting");
  const [navCollapsed, setNavCollapsed] = useState(() => stored("fcli-nav", "") === "collapsed");
  const [navOpen, setNavOpen] = useState(false); // narrow screens: the panel over the page
  const [detailsCollapsed, setDetailsCollapsed] = useState(() => stored("fcli-details", "") === "collapsed");
  const [chatOpen, setChatOpen] = useState(false);
  const [, setTick] = useState(0);
  const atRef = useRef(at);
  atRef.current = at;
  const pending = useRef(null);
  const overlaySettings = useOverlay();
  const service = overlaySettings.overlay.startsWith("service:") ? overlaySettings.overlay.slice(8) : null;
  const traffic = overlaySettings.overlay === "traffic";
  const paint = { mode: traffic ? "traffic" : "health", rate: overlaySettings.bps };
  const picked = useMemo(
    () => (service && data ? serviceOverlay(data.topology, service) : null),
    [service, data],
  );

  const load = async (when = atRef.current) => {
    try {
      const [s, topology, checks, incidents] = await Promise.all([
        get("/api/status"),
        get("/api/topology", { at: when }),
        get("/api/checks", { at: when }),
        get("/api/incidents", { at: when }),
      ]);
      if (when !== atRef.current) return; // the time was changed while this was loading
      setStatus(s);
      setData({ topology, checks, incidents });
      setError("");
    } catch (e) {
      setError(e.message);
    }
  };

  useEffect(() => {
    load(at);
  }, [at]);

  useEffect(() => {
    get("/api/reports").then(setCatalog).catch(() => setCatalog([]));
    const schedule = () => {
      if (atRef.current || pending.current) return;
      pending.current = setTimeout(() => {
        pending.current = null;
        load();
      }, SETTLE_MS);
    };
    const stop = follow(
      WATCHED,
      (type, event) => {
        if (type === "status") {
          setStatus((s) => (s ? { ...s, nodes: event.nodes } : s));
        } else if (type === "node") {
          setStatus((s) => (s ? { ...s, nodes: s.nodes.map((n) => (n.name === event.name ? { ...n, ...event } : n)) } : s));
          schedule();
        } else {
          schedule();
        }
      },
      setConnection,
    );
    // Ages ("12m ago") move on even when nothing else does.
    const timer = setInterval(() => setTick((t) => t + 1), 30_000);
    return () => {
      stop();
      clearInterval(timer);
      clearTimeout(pending.current);
    };
  }, []);

  // Drawn by traffic, live: the rates move on every poll.
  useEffect(() => {
    if (!traffic || at || route.view !== "topology") return;
    const timer = setInterval(() => load(), TRAFFIC_MS);
    return () => clearInterval(timer);
  }, [traffic, at, route.view]);

  // A narrow screen's navigation closes once a view is picked.
  useEffect(() => setNavOpen(false), [location.hash]);

  const toggleNav = () => {
    if (matchMedia("(max-width: 860px)").matches) {
      setNavOpen(!navOpen);
    } else {
      store("fcli-nav", navCollapsed ? null : "collapsed");
      setNavCollapsed(!navCollapsed);
    }
  };
  const toggleDetails = (collapsed = !detailsCollapsed) => {
    store("fcli-details", collapsed ? "collapsed" : null);
    setDetailsCollapsed(collapsed);
  };
  // Picking something on the map is asking for its details.
  const selected = route.selection ? `${route.selection.type}|${route.selection.id}` : "";
  useEffect(() => {
    if (selected) toggleDetails(false);
  }, [selected]);
  const select = (selection) => go(selection ? `topology/${selection.type}/${encodeURIComponent(selection.id)}` : "topology");
  const shared = { go, at, graph: data?.topology, status, checks: data?.checks ?? [], query: route.query };

  let page;
  if (!data) {
    page = html`<main class="loading">${error ? "" : "Reading the fabric…"}</main>`;
  } else if (route.view === "topology") {
    page = html`<main class=${`split${detailsCollapsed ? " details-collapsed" : ""}`}>
      <${Topology} graph=${data.topology} status=${status} checks=${data.checks} selected=${route.selection} onSelect=${select}
                   paint=${paint} overlay=${picked?.nodes.size ? picked : null}
                   controls=${html`<${OverlayControls} graph=${data.topology} settings=${overlaySettings} />`} />
      <aside class="details-pane">
        <button class="pane-toggle" onClick=${() => toggleDetails()} aria-expanded=${!detailsCollapsed}
                title=${detailsCollapsed ? "Show the details panel" : "Hide the details panel"}>
          ${detailsCollapsed ? "‹" : "›"}
        </button>
        ${!detailsCollapsed &&
        html`<${Details} selection=${route.selection} graph=${data.topology} status=${status}
                         checks=${data.checks} at=${at} onSelect=${select} />`}
      </aside>
    </main>`;
  } else if (route.view === "incidents") {
    page = html`<main class="page"><${Incidents} incidents=${data.incidents} at=${at} started=${status?.started}
                                                 onNode=${(n) => select({ type: "node", id: n })} /></main>`;
  } else if (route.view === "history") {
    page = html`<main class="page wide"><${HistoryView} ...${shared} catalog=${catalog} /></main>`;
  } else if (route.view === "report") {
    page = catalog.length
      ? html`<main class="page wide"><${ReportView} key=${route.name} ...${shared} name=${route.name} catalog=${catalog} /></main>`
      : html`<main class="loading">Reading the reports…</main>`;
  } else {
    const views = { where: WhereView, path: PathView, service: ServiceView, endpoints: EndpointsView };
    const View = views[route.name];
    const lens = LENS_VIEWS[route.name];
    page = View
      ? html`<main class="page wide">
          <div class="view-head"><h2>${lens.title}</h2><span class="muted">${lens.help}</span></div>
          <${View} key=${route.name} ...${shared} />
        </main>`
      : html`<main class="page"><p class="error-text">No lens ${route.name}.</p></main>`;
  }

  return html`
    <${Header} status=${status} connection=${connection} at=${at} onAt=${setAt} toggleTheme=${toggleTheme}
               onMenu=${toggleNav} onChat=${() => setChatOpen(!chatOpen)} chatOpen=${chatOpen} />
    ${error && html`<div class="banner error">${error}</div>`}
    ${at && html`<div class="banner past">Showing the fabric as the history has it at <b>${at}</b>. Live changes are paused.</div>`}
    <div class=${`shell${navOpen ? " nav-open" : ""}${chatOpen ? " chat-open" : ""}`}>
      <${Nav} route=${route} catalog=${catalog} incidents=${data?.incidents ?? []} collapsed=${navCollapsed}
              chatOpen=${chatOpen} onChat=${() => setChatOpen(!chatOpen)} />
      ${page}
      <${ChatPanel} open=${chatOpen} onClose=${() => setChatOpen(false)} context=${describe(route, at)} />
    </div>`;
}

render(html`<${App} />`, document.getElementById("app"));

// fcli's web UI: the topology and the incidents, live from `fcli serve` or as they were at an instant.

import { html, render, useEffect, useRef, useState } from "./vendor/preact-htm.module.js";
import { follow, get } from "./api.js";
import { Incidents } from "./incidents.js";
import { Details, Topology } from "./topology.js";

/** Reports whose changes can change what the UI shows; the rest would only wake it up. */
const WATCHED = ["checks", "lldp", "subif", "services", "es", "lag"];
/** Changes come in bursts: refresh once they settle, not once per change. */
const SETTLE_MS = 1500;

function parseHash() {
  const [view = "topology", type, ...rest] = location.hash.replace(/^#\/?/, "").split("/");
  const id = rest.join("/");
  return {
    view: view === "incidents" ? "incidents" : "topology",
    selection: type && id ? { type, id: decodeURIComponent(id) } : null,
  };
}

function hashOf(view, selection) {
  return `#/${view}${selection ? `/${selection.type}/${encodeURIComponent(selection.id)}` : ""}`;
}

function useRoute() {
  const [route, setRoute] = useState(parseHash);
  useEffect(() => {
    const changed = () => setRoute(parseHash());
    addEventListener("hashchange", changed);
    return () => removeEventListener("hashchange", changed);
  }, []);
  const go = (view, selection = null) => {
    const hash = hashOf(view, selection);
    if (hash !== location.hash) location.hash = hash;
    else setRoute({ view, selection });
  };
  return [route, go];
}

function useTheme() {
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem("fcli-theme") || "";
    } catch {
      return "";
    }
  });
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    try {
      if (theme) localStorage.setItem("fcli-theme", theme);
      else localStorage.removeItem("fcli-theme");
    } catch {
      // A preference that does not stick is no loss.
    }
  }, [theme]);
  const next = () => {
    const dark = theme ? theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    setTheme(dark ? "light" : "dark");
  };
  return next;
}

function TimeControl({ at, onAt }) {
  const [text, setText] = useState(at);
  useEffect(() => setText(at), [at]);
  const submit = (e) => {
    e.preventDefault();
    onAt(text.trim());
  };
  return html`<form class=${`time${at ? " past" : ""}`} onSubmit=${submit}>
    ${at
      ? html`<span class="when">at <b>${at}</b></span><button type="button" onClick=${() => onAt("")}>Back to live</button>`
      : html`<input value=${text} onInput=${(e) => setText(e.target.value)} placeholder="Go back: 30m, 2h, 14:02"
                    aria-label="Show the fabric as it was at" />`}
  </form>`;
}

function Header({ status, connection, route, go, at, onAt, toggleTheme, incidents }) {
  const nodes = status?.nodes ?? [];
  const down = nodes.filter((n) => n.status !== "live").length;
  const errors = incidents.filter((i) => i.severity === "error").length;
  const warnings = incidents.filter((i) => i.severity === "warning").length;
  return html`<header class="top">
    <div class="brand"><span class="logo">fcli</span><span class="fabric">${status?.fabric ?? ""}</span></div>
    <nav>
      <a class=${route.view === "topology" ? "active" : ""} href="#/topology">Topology</a>
      <a class=${route.view === "incidents" ? "active" : ""} href="#/incidents">Incidents
        ${errors > 0 && html`<span class="tab-count error">${errors}</span>`}
        ${warnings > 0 && html`<span class="tab-count warning">${warnings}</span>`}</a>
    </nav>
    <div class="spacer"></div>
    <span class=${`nodes-state${down ? " bad" : ""}`} title=${nodes.filter((n) => n.status !== "live").map((n) => `${n.name}: ${n.status}${n.error ? ` (${n.error})` : ""}`).join("\n")}>
      ${nodes.length - down}/${nodes.length} nodes live
    </span>
    <${TimeControl} at=${at} onAt=${onAt} />
    <span class=${`connection ${at ? "paused" : connection}`} title=${at ? "showing the past: not following changes" : `events: ${connection}`}></span>
    <button class="icon" onClick=${toggleTheme} title="Light or dark">◐</button>
  </header>`;
}

function App() {
  const [route, go] = useRoute();
  const toggleTheme = useTheme();
  const [at, setAt] = useState("");
  const [status, setStatus] = useState(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [connection, setConnection] = useState("connecting");
  const [, setTick] = useState(0);
  const atRef = useRef(at);
  atRef.current = at;
  const pending = useRef(null);

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

  const select = (selection) => go("topology", selection);
  const header = html`<${Header} status=${status} connection=${connection} route=${route} go=${go} at=${at}
                                 onAt=${setAt} toggleTheme=${toggleTheme} incidents=${data?.incidents ?? []} />`;
  const banner = error && html`<div class="banner error">${error}</div>`;
  if (!data) {
    return html`${header}${banner}<main class="loading">${error ? "" : "Reading the fabric…"}</main>`;
  }
  return html`${header}${banner}
    ${at && html`<div class="banner past">Showing the fabric as the history has it at <b>${at}</b>. Live changes are paused.</div>`}
    ${route.view === "topology"
      ? html`<main class="split">
          <${Topology} graph=${data.topology} status=${status} checks=${data.checks}
                       selected=${route.selection} onSelect=${select} />
          <aside><${Details} selection=${route.selection} graph=${data.topology} status=${status}
                             checks=${data.checks} at=${at} onSelect=${select} /></aside>
        </main>`
      : html`<main class="page"><${Incidents} incidents=${data.incidents} at=${at}
                                              onNode=${(n) => select({ type: "node", id: n })} /></main>`}`;
}

render(html`<${App} />`, document.getElementById("app"));

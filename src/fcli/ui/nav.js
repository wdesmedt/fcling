// The navigation panel: the fabric's views, the lenses, every report, and the agent.

import { html, useState } from "./vendor/preact-htm.module.js";
import { natural } from "./api.js";
import { LENS_VIEWS } from "./lenses.js";
import { REPORT_GROUPS } from "./reports.js";

const ICONS = {
  topology: "◇",
  incidents: "△",
  history: "◷",
  where: "⌖",
  path: "↝",
  service: "▦",
  endpoints: "◉",
  ask: "✦",
};

function Item({ href, active, icon, label, children, onClick, title }) {
  return html`<a class=${`nav-item${active ? " active" : ""}`} href=${href} onClick=${onClick} title=${title || label}>
    <span class="nav-icon">${icon}</span><span class="nav-label">${label}</span>${children}
  </a>`;
}

/** Reports grouped as REPORT_GROUPS says; anything new lands in "Other". The BGP RIB families get a group. */
function groups(catalog) {
  const known = new Set(REPORT_GROUPS.flatMap(([, names]) => names));
  const visible = catalog.filter((r) => !r.hidden).map((r) => r.name);
  const out = REPORT_GROUPS.map(([title, names]) => [title, names.filter((n) => visible.includes(n))]);
  const rib = catalog.filter((r) => r.hidden && r.name.startsWith("bgp-rib-")).map((r) => r.name).sort(natural);
  if (rib.length) out.splice(3, 0, ["BGP RIB", rib]);
  const other = visible.filter((n) => !known.has(n)).sort(natural);
  if (other.length) out.push(["Other", other]);
  return out.filter(([, names]) => names.length);
}

export function Nav({ route, catalog, incidents, chatOpen, onChat, collapsed }) {
  const [find, setFind] = useState("");
  const [closed, setClosed] = useState(() => new Set());
  const errors = incidents.filter((i) => i.severity === "error").length;
  const warnings = incidents.filter((i) => i.severity === "warning").length;
  const is = (view, name) => route.view === view && (name === undefined || route.name === name);
  const toggle = (title) => {
    const next = new Set(closed);
    next.has(title) ? next.delete(title) : next.add(title);
    setClosed(next);
  };
  const term = find.trim().toLowerCase();
  return html`<nav class=${`side${collapsed ? " collapsed" : ""}`} aria-label="Views">
    <div class="nav-section">
      <${Item} href="#/topology" active=${is("topology")} icon=${ICONS.topology} label="Topology" />
      <${Item} href="#/incidents" active=${is("incidents")} icon=${ICONS.incidents} label="Incidents">
        <span class="nav-counts">
          ${errors > 0 && html`<span class="tab-count error">${errors}</span>`}
          ${warnings > 0 && html`<span class="tab-count warning">${warnings}</span>`}
        </span>
      </${Item}>
      <${Item} href="#/history" active=${is("history")} icon=${ICONS.history} label="History" />
    </div>
    <div class="nav-section">
      <div class="nav-title">Lenses</div>
      ${Object.entries(LENS_VIEWS).map(
        ([name, v]) => html`<${Item} href=${`#/lens/${name}`} active=${is("lens", name)} icon=${ICONS[name]}
                                     label=${v.title} title=${v.help} />`,
      )}
    </div>
    <div class="nav-section reports">
      <div class="nav-title">Reports</div>
      ${!collapsed &&
      html`<input class="nav-find" value=${find} onInput=${(e) => setFind(e.target.value)} placeholder="Find a report"
                  aria-label="Find a report" />`}
      ${!collapsed &&
      groups(catalog).map(([title, names]) => {
        const shown = term ? names.filter((n) => n.includes(term)) : names;
        if (!shown.length) return null;
        const open = term || !closed.has(title);
        return html`<div class="nav-group">
          <button class="nav-group-title" onClick=${() => toggle(title)}>${open ? "▾" : "▸"} ${title}</button>
          ${open &&
          shown.map(
            (n) => html`<a class=${`nav-report${is("report", n) ? " active" : ""}`} href=${`#/report/${n}`}
                           title=${catalog.find((r) => r.name === n)?.help}>${title === "BGP RIB" ? n.replace("bgp-rib-", "") : n}</a>`,
          )}
        </div>`;
      })}
    </div>
    <div class="nav-section bottom">
      <${Item} href="#" active=${chatOpen} icon=${ICONS.ask} label="Ask the agent"
               onClick=${(e) => {
                 e.preventDefault();
                 onChat();
               }} />
    </div>
  </nav>`;
}

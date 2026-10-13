// Incidents: what is wrong, by root cause, worst first; and what changed just before each.

import { html, useEffect, useState } from "./vendor/preact-htm.module.js";
import { SEVERITIES, ago, clock, get } from "./api.js";

/** How far before an incident started to look for what changed. */
const LOOKBACK = 15 * 60;

function keyText(key) {
  try {
    const parts = JSON.parse(key);
    return Array.isArray(parts) ? parts.filter((p) => p !== null && p !== "").join(" ") : String(key);
  } catch {
    return String(key);
  }
}

function short(value) {
  if (value === null || value === undefined || value === "") return "—";
  const text = typeof value === "object" ? JSON.stringify(value) : String(value);
  return text.length > 60 ? `${text.slice(0, 59)}…` : text;
}

/** One change, as what moved: the fields that differ, or the record that came or went. */
export function Change({ change }) {
  const before = change.before || {};
  const after = change.after || {};
  let what;
  if (change.op === "change") {
    const fields = Object.keys(after).filter(
      (k) => k !== "node" && JSON.stringify(before[k]) !== JSON.stringify(after[k]),
    );
    what = fields.slice(0, 4).map(
      (k) => html`<span class="delta"><b>${k}</b> ${short(before[k])} → ${short(after[k])}</span>`,
    );
  } else if (change.report === "checks") {
    const f = change.op === "add" ? after : before;
    what = html`<span>${change.op === "add" ? "raised" : "cleared"}: <b>${f.check}</b> ${f.subject} — ${f.detail}</span>`;
  } else {
    what = html`<span class="mono">${keyText(change.key)}</span>`;
  }
  return html`<li class=${`change op-${change.op}`}>
    <span class="when mono">${clock(change.ts)}</span>
    <span class="who">${change.node}</span>
    <span class="report">${change.report}</span>
    <span class=${`op ${change.op}`}>${change.op}</span>
    <span class="what">${what}</span>
    ${change.cause && html`<span class="cause">← ${change.cause}</span>`}
  </li>`;
}

/** Raised this soon after the server started: the history cannot say what led up to it. */
const STARTUP = 90;
const SHOWN = 12;

function Before({ incident, started }) {
  const [changes, setChanges] = useState(null);
  const [error, setError] = useState("");
  const [all, setAll] = useState(false);
  const atStart = Boolean(started) && incident.first_seen - started < STARTUP;
  const [open, setOpen] = useState(!atStart);
  useEffect(() => {
    let current = true;
    get("/api/history", {
      since: incident.first_seen - LOOKBACK,
      until: incident.first_seen + 5,
      node: incident.nodes,
      limit: 200,
    })
      .then((found) => current && setChanges(found.reverse()))
      .catch((e) => current && setError(e.message));
    return () => {
      current = false;
    };
  }, [incident.id, incident.first_seen]);
  if (error) return html`<p class="error-text">${error}</p>`;
  if (!changes) return html`<p class="muted">Loading the history…</p>`;
  if (!changes.length) {
    return html`<p class="muted">Nothing changed on ${incident.nodes.length > 3 ? `its ${incident.nodes.length} nodes` : incident.nodes.join(", ")} in the ${LOOKBACK / 60} minutes before.</p>`;
  }
  const byReport = {};
  for (const c of changes) byReport[c.report] = (byReport[c.report] || 0) + 1;
  const counts = Object.entries(byReport)
    .sort((a, b) => b[1] - a[1])
    .map(([r, n]) => `${n} ${r}`)
    .join(", ");
  return html`
    ${atStart &&
    html`<p class="note">Raised as the server started, at ${clock(started)}: the ${changes.length >= 200 ? "200 or more" : changes.length} changes below are how the fabric
      differed from when fcling last saw it, not what led up to this.
      <button class="link-button inline" onClick=${() => setOpen(!open)}>${open ? "Hide them" : "Show them"}</button></p>`}
    ${open &&
    html`<p class="muted small">${changes.length >= 200 ? "The latest 200" : changes.length} changes, newest first: ${counts}</p>
      <ul class="changes">${(all ? changes : changes.slice(0, SHOWN)).map((c) => html`<${Change} change=${c} />`)}</ul>
      ${!all && changes.length > SHOWN &&
      html`<button class="link-button" onClick=${() => setAll(true)}>Show ${changes.length - SHOWN} more</button>`}`}`;
}

/** Rows of an incident's other findings shown before "show more". */
const RELATED = 8;

function Incident({ incident, open, onToggle, onNode, at, started }) {
  const findings = [incident.root, ...incident.related];
  const [allRelated, setAllRelated] = useState(false);
  const related = allRelated ? incident.related : incident.related.slice(0, RELATED);
  return html`<article class=${`incident sev-${incident.severity}${open ? " open" : ""}`}>
    <header onClick=${onToggle}>
      <span class=${`pill ${incident.severity}`}>${incident.severity}</span>
      <h3>${incident.title}</h3>
      <span class="meta">
        ${incident.kind} · ${findings.length} finding${findings.length > 1 ? "s" : ""}
        ${incident.first_seen && html` · since ${ago(incident.first_seen)}`}
      </span>
      <span class="chevron">${open ? "▾" : "▸"}</span>
    </header>
    <div class="nodes">
      ${incident.nodes.slice(0, 12).map((n) => html`<a class="chip" onClick=${() => onNode(n)}>${n}</a>`)}
      ${incident.nodes.length > 12 && html`<span class="muted">+${incident.nodes.length - 12} more</span>`}
    </div>
    ${open &&
    html`<div class="incident-body">
      ${incident.explanation && html`<p class="explanation">${incident.explanation}</p>`}
      <h4>Root</h4>
      <p><b>${incident.root.check}</b> on <span class="mono">${incident.root.node} ${incident.root.subject}</span>: ${incident.root.detail}</p>
      ${incident.related.length > 0 &&
      html`<h4>${incident.kind === "pattern" ? "The same elsewhere" : "Consequences"}</h4>
        <table class="grid">
          <thead><tr><th></th><th>check</th><th>node</th><th>subject</th><th>detail</th></tr></thead>
          <tbody>${related.map(
            (f) => html`<tr>
              <td><span class=${`dot ${f.severity}`}></span></td><td>${f.check}</td><td>${f.node}</td>
              <td class="mono">${f.subject}</td><td>${f.detail}</td></tr>`,
          )}</tbody>
        </table>
        ${related.length < incident.related.length &&
        html`<button class="link-button" onClick=${() => setAllRelated(true)}>Show ${incident.related.length - related.length} more</button>`}`}
      ${incident.first_seen && !at
        ? html`<h4>What changed just before</h4><${Before} incident=${incident} started=${started} />`
        : !incident.first_seen && html`<p class="muted">No history says when this started.</p>`}
    </div>`}
  </article>`;
}

export function Incidents({ incidents, at, onNode, started }) {
  const [open, setOpen] = useState(() => new Set());
  const [showInfo, setShowInfo] = useState(false);
  const toggle = (id) => {
    const next = new Set(open);
    next.has(id) ? next.delete(id) : next.add(id);
    setOpen(next);
  };
  const counts = Object.fromEntries(SEVERITIES.map((s) => [s, incidents.filter((i) => i.severity === s).length]));
  const faults = incidents.filter((i) => i.severity !== "info");
  const info = incidents.filter((i) => i.severity === "info");
  const card = (i) => html`<${Incident} incident=${i} open=${open.has(i.id)} onToggle=${() => toggle(i.id)}
                                        onNode=${onNode} at=${at} started=${started} />`;
  return html`<div class="incidents">
    <div class="summary">
      ${SEVERITIES.map((s) => html`<span class=${`count ${s}${counts[s] ? "" : " zero"}`}><b>${counts[s]}</b> ${s}</span>`)}
    </div>
    ${faults.length
      ? faults.map(card)
      : html`<div class="all-clear"><b>Nothing wrong.</b> No errors or warnings${at ? " at that time" : ""}.</div>`}
    ${info.length > 0 &&
    html`<section class="info-section">
      <button class="link-button" onClick=${() => setShowInfo(!showInfo)}>
        ${showInfo ? "▾" : "▸"} Information (${info.length}): often by design
      </button>
      ${showInfo && info.map(card)}
    </section>`}
  </div>`;
}

// Incidents: what is wrong, by root cause, worst first; and what changed just before each.

import { html, useEffect, useState } from "./vendor/preact-htm.module.js";
import { SEVERITIES, ago, clock, get, send } from "./api.js";

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

const SHORT = 60;
const SHOWN_FIELDS = 4;

const text = (value) => (typeof value === "object" && value !== null ? JSON.stringify(value) : String(value));
const empty = (value) => value === null || value === undefined || value === "";

function short(value) {
  if (empty(value)) return "—";
  const t = text(value);
  return t.length > SHORT ? `${t.slice(0, SHORT - 1)}…` : t;
}

/** A value in full: objects indented, so a next-hop reads as one. */
const full = (value) => (empty(value) ? "—" : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value));

/** A list entry on one line: a flat record as its non-empty key=value pairs. */
function entry(value) {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return full(value);
  const flat = Object.values(value).every((v) => typeof v !== "object" || v === null || (Array.isArray(v) && !v.length));
  if (!flat) return full(value);
  return Object.entries(value)
    .filter(([, v]) => !empty(v) && !(Array.isArray(v) && !v.length))
    .map(([k, v]) => `${k}=${v}`)
    .join("  ");
}

/** One field's change in full: a list as the entries that went and came, anything else before → after. */
function FieldChange({ name, before, after }) {
  if (Array.isArray(before) || Array.isArray(after)) {
    const was = (before || []).map(text);
    const now = (after || []).map(text);
    const gone = (before || []).filter((_, i) => !now.includes(was[i]));
    const came = (after || []).filter((_, i) => !was.includes(now[i]));
    return html`<div class="field-change"><b>${name}</b>
      ${gone.map((v) => html`<pre class="gone">− ${entry(v)}</pre>`)}
      ${came.map((v) => html`<pre class="came">+ ${entry(v)}</pre>`)}
      ${!gone.length && !came.length && html`<span class="muted"> reordered</span>`}
    </div>`;
  }
  return html`<div class="field-change"><b>${name}</b>
    <pre class="gone">− ${full(before)}</pre><pre class="came">+ ${full(after)}</pre>
  </div>`;
}

/** One change, as what moved: the fields that differ, or the record that came or went.
 *  The line is a summary; clicked, it opens on every field in full. */
export function Change({ change }) {
  const [open, setOpen] = useState(false);
  const before = change.before || {};
  const after = change.after || {};
  let what;
  let detail = null;
  let more = false;
  if (change.op === "change") {
    const fields = Object.keys(after).filter(
      (k) => k !== "node" && JSON.stringify(before[k]) !== JSON.stringify(after[k]),
    );
    what = fields.slice(0, SHOWN_FIELDS).map(
      (k) => html`<span class="delta"><b>${k}</b> ${short(before[k])} → ${short(after[k])}</span>`,
    );
    more = fields.length > SHOWN_FIELDS || fields.some((k) => text(before[k] ?? "").length > SHORT || text(after[k] ?? "").length > SHORT);
    detail = fields.map((k) => html`<${FieldChange} name=${k} before=${before[k]} after=${after[k]} />`);
  } else if (change.report === "checks") {
    const f = change.op === "add" ? after : before;
    what = html`<span>${change.op === "add" ? "raised" : "cleared"}: <b>${f.check}</b> ${f.subject} — ${f.detail}</span>`;
  } else {
    what = html`<span class="mono">${keyText(change.key)}</span>`;
    const record = change.op === "add" ? after : before;
    const fields = Object.keys(record).filter((k) => k !== "node" && !empty(record[k]));
    more = fields.length > 0;
    detail = html`<dl class="record">${fields.map((k) => html`<dt>${k}</dt><dd><pre>${full(record[k])}</pre></dd>`)}</dl>`;
  }
  return html`<li class=${`change op-${change.op}${more ? " expandable" : ""}${open ? " open" : ""}`}
                  onClick=${more ? () => setOpen(!open) : undefined}>
    <span class="when mono">${more && html`<span class="caret">${open ? "▾" : "▸"}</span>`}${clock(change.ts)}</span>
    <span class="who">${change.node}</span>
    <span class="report">${change.report}</span>
    <span class=${`op ${change.op}`}>${change.op}</span>
    <span class="what">${what}</span>
    ${change.cause && html`<span class="cause">← ${change.cause}</span>`}
    ${open && html`<div class="change-detail" onClick=${(e) => e.stopPropagation()}>${detail}</div>`}
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

/** Ack or Unack an open incident; acknowledgements are about now, so not at an instant in the past. */
function AckButton({ incident, onChanged }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const acked = incident.acknowledged;
  const act = async (e) => {
    e.stopPropagation();
    setBusy(true);
    setError("");
    try {
      if (acked) await send("DELETE", "/api/acks", { params: { incident: incident.id } });
      else await send("POST", "/api/acks", { body: { incident: incident.id } });
      onChanged?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };
  return html`<button class=${`small ack${acked ? " acked" : ""}`} disabled=${busy} onClick=${act}
                      title=${error || (acked ? "Show it as open again" : "Known: take it out of the counts until a finding changes or it clears")}>
    ${acked ? "Unack" : "Ack"}</button>`;
}

function Incident({ incident, open, onToggle, onNode, at, started, onChanged }) {
  const findings = [incident.root, ...incident.related];
  const [allRelated, setAllRelated] = useState(false);
  const related = allRelated ? incident.related : incident.related.slice(0, RELATED);
  return html`<article class=${`incident sev-${incident.severity}${open ? " open" : ""}${incident.acknowledged ? " acknowledged" : ""}`}>
    <header onClick=${onToggle}>
      <span class=${`pill ${incident.severity}`}>${incident.severity}</span>
      <h3>${incident.title}</h3>
      <span class="meta">
        ${incident.kind} · ${findings.length} finding${findings.length > 1 ? "s" : ""}
        ${incident.first_seen && html` · since ${ago(incident.first_seen)}`}
        ${incident.acknowledged && incident.ack && html` · acknowledged ${ago(incident.ack.at)}`}
      </span>
      ${!at && html`<${AckButton} incident=${incident} onChanged=${onChanged} />`}
      <span class="caret">${open ? "▾" : "▸"}</span>
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

export function Incidents({ incidents, at, onNode, started, onChanged }) {
  const [open, setOpen] = useState(() => new Set());
  const [showInfo, setShowInfo] = useState(false);
  const [showAcked, setShowAcked] = useState(false);
  // A count clicked shows only its incidents; clicked again, all of them.
  const [only, setOnly] = useState("");
  const toggle = (id) => {
    const next = new Set(open);
    next.has(id) ? next.delete(id) : next.add(id);
    setOpen(next);
  };
  // The acknowledged ones are known: out of the counts and the list, kept together below.
  const acked = incidents.filter((i) => i.acknowledged);
  const unacked = incidents.filter((i) => !i.acknowledged);
  const counts = Object.fromEntries(SEVERITIES.map((s) => [s, unacked.filter((i) => i.severity === s).length]));
  const faults = unacked.filter((i) => i.severity !== "info");
  const info = unacked.filter((i) => i.severity === "info");
  const card = (i) => html`<${Incident} key=${i.id} incident=${i} open=${open.has(i.id)} onToggle=${() => toggle(i.id)}
                                        onNode=${onNode} at=${at} started=${started} onChanged=${onChanged} />`;
  const filter = (name, n, label = name) => html`<button
      class=${`count ${name}${n ? "" : " zero"}${only === name ? " on" : ""}`} aria-pressed=${only === name}
      title=${only === name ? "Show every incident" : `Show only the ${label} incidents`}
      onClick=${() => setOnly(only === name ? "" : name)}><b>${n}</b> ${label}</button>`;
  const picked = only === "acknowledged" ? acked : unacked.filter((i) => i.severity === only);
  return html`<div class="incidents">
    <div class="summary">
      ${SEVERITIES.map((s) => filter(s, counts[s]))}
      ${(acked.length > 0 || only === "acknowledged") && filter("acknowledged", acked.length)}
    </div>
    ${only
      ? picked.length
        ? picked.map(card)
        : html`<p class="muted">No ${only} incidents${at ? " at that time" : ""}.</p>`
      : html`${faults.length
      ? faults.map(card)
      : html`<div class="all-clear"><b>Nothing wrong.</b> No errors or warnings${at ? " at that time" : ""}.</div>`}
    ${info.length > 0 &&
    html`<section class="info-section">
      <button class="link-button" onClick=${() => setShowInfo(!showInfo)}>
        <span class="caret">${showInfo ? "▾" : "▸"}</span> Information (${info.length}): often by design
      </button>
      ${showInfo && info.map(card)}
    </section>`}
    ${acked.length > 0 &&
    html`<section class="info-section">
      <button class="link-button" onClick=${() => setShowAcked(!showAcked)}>
        <span class="caret">${showAcked ? "▾" : "▸"}</span> Acknowledged (${acked.length}): known, until a finding changes or clears
      </button>
      ${showAcked && acked.map(card)}
    </section>`}`}
  </div>`;
}

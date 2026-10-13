// Server and per-node stream health: what the engine is doing on each node.

import { html } from "./vendor/preact-htm.module.js";
import { ago, natural } from "./api.js";

const ORDER = { live: 0, syncing: 1, connecting: 2, down: 3 };

export function StatusView({ status, connection, onNode }) {
  const nodes = [...(status?.nodes ?? [])].sort(
    (a, b) => (ORDER[a.status] ?? 9) - (ORDER[b.status] ?? 9) || natural(a.name, b.name),
  );
  const live = nodes.filter((n) => n.status === "live").length;
  const incidents = status?.incidents ?? {};
  const streamed = status?.streamed ?? [];
  return html`<div class="status-view">
    <div class="view-head">
      <h2>Nodes</h2>
      <span class="muted">gNMI stream state for every node in the fabric</span>
      <span class=${`pill source-engine`}>${connection}</span>
    </div>
    <div class="status-summary">
      <span><b>${live}</b> / ${nodes.length} live</span>
      ${incidents.errors > 0 && html`<span class="tab-count error">${incidents.errors} error incidents</span>`}
      ${incidents.warnings > 0 && html`<span class="tab-count warning">${incidents.warnings} warnings</span>`}
      ${status?.started && html`<span class="muted">engine up ${ago(status.started)}</span>`}
    </div>
    ${streamed.length > 0 &&
    html`<p class="muted small">Streamed reports: ${streamed.join(", ")}</p>`}
    <table class="grid status-table">
      <thead><tr>
        <th>Node</th><th>Status</th><th>Address</th><th>Since</th><th>Last heard</th><th>Labels</th><th></th>
      </tr></thead>
      <tbody>${nodes.map(
        (n) => html`<tr key=${n.name} class=${`status-${n.status}`}>
          <td class="node-cell">${onNode
            ? html`<a onClick=${() => onNode(n.name)}>${n.name}</a>`
            : n.name}</td>
          <td><span class=${`pill status-${n.status}`}>${n.status}</span>
            ${n.error && html`<div class="small error-text">${n.error}</div>`}</td>
          <td class="muted">${n.address ?? ""}</td>
          <td class="muted">${n.since ? ago(n.since) : ""}</td>
          <td class="muted">${n.heard ? ago(n.heard) : ""}</td>
          <td class="muted small">${Object.entries(n.labels ?? {})
            .map(([k, v]) => `${k}=${v}`)
            .join(" ")}</td>
          <td>${onNode && html`<button class="link-button" onClick=${() => onNode(n.name)}>topology</button>`}</td>
        </tr>`,
      )}</tbody>
    </table>
  </div>`;
}

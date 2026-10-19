// A configuration commit, as a split diff: the configuration before on the left, after on the right.

import { html, useEffect, useState } from "./vendor/preact-htm.module.js";
import { clock, get } from "./api.js";

/** As much of the whole configuration as anyone would scroll through. */
const WHOLE = 100000;

/** Where two versions of a line differ: the common start and end are left plain. */
function split(a, b) {
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let end = 0;
  while (end < a.length - start && end < b.length - start && a[a.length - 1 - end] === b[b.length - 1 - end]) end++;
  return [start, end];
}

function Text({ text, other, side }) {
  if (other === undefined) return text;
  const [start, end] = split(text, other);
  return html`${text.slice(0, start)}<mark class=${side}>${text.slice(start, text.length - end)}</mark>${text.slice(text.length - end)}`;
}

function Cell({ side, row }) {
  const line = row[side];
  if (!line) return html`<td class="n"></td><td class="line none"></td>`;
  const other = row.kind === "changed" ? row[side === "left" ? "right" : "left"].text : undefined;
  return html`<td class="n">${line.n}</td>
    <td class="line"><span class="sign">${row.kind === "same" ? " " : side === "left" ? "−" : "+"}</span><${Text}
      text=${line.text} other=${other} side=${side === "left" ? "gone" : "came"} /></td>`;
}

const label = (kept) => (kept ? `after commit ${kept.commit_id ?? "?"} · ${clock(kept.ts)}` : "");

/** The diff of commit *commit* on *node*, fetched when it is shown. *seen*, when the commit
 *  was seen, tells it from a commit of the same id before the node was redeployed. */
export function ConfigDiff({ node, commit, seen }) {
  const [whole, setWhole] = useState(false);
  const [state, setState] = useState({ diff: null, error: "" });
  useEffect(() => {
    let current = true;
    get("/api/configs/diff", { node, commit: commit ?? "", seen: seen ?? "", context: whole ? WHOLE : 3 })
      .then((diff) => current && setState({ diff, error: "" }))
      .catch((e) => current && setState({ diff: null, error: e.message }));
    return () => {
      current = false;
    };
  }, [node, commit, seen, whole]);
  const { diff, error } = state;
  if (error) return html`<p class="error-text">${error}</p>`;
  if (!diff) return html`<p class="muted">Reading the configurations…</p>`;
  const commits = diff.commits.map(
    (c) => html`<span class="chip">commit ${c.id} · ${c.username || "?"}${c.comment ? ` · ${c.comment}` : ""}</span>`,
  );
  if (!diff.before) {
    return html`<div class="config-diff"><div class="config-diff-head">${commits}</div>
      <p class="muted">This is the first configuration of ${node} the history kept: there is nothing earlier to compare it with.</p></div>`;
  }
  const changed = diff.added + diff.removed + diff.changed;
  return html`<div class="config-diff">
    <div class="config-diff-head">
      ${commits}
      <span class="counts">
        <span class="c-added">+${diff.added} added</span>
        <span class="c-removed">−${diff.removed} removed</span>
        <span class="c-changed">~${diff.changed} changed</span>
      </span>
      <label class="whole"><input type="checkbox" checked=${whole} onChange=${(e) => setWhole(e.target.checked)} /> whole configuration</label>
    </div>
    ${changed
      ? html`<div class="config-diff-scroll"><table class="split-diff">
          <colgroup><col class="n" /><col /><col class="n" /><col /></colgroup>
          <thead><tr><th></th><th>${label(diff.before)}</th><th></th><th>${label(diff.after)}</th></tr></thead>
          <tbody>${diff.rows.map((row) =>
            row.kind === "skip"
              ? html`<tr class="skip"><td colspan="4" onClick=${() => setWhole(true)}>⋯ ${row.count} unchanged lines</td></tr>`
              : html`<tr class=${row.kind}><${Cell} side="left" row=${row} /><${Cell} side="right" row=${row} /></tr>`,
          )}</tbody>
        </table></div>`
      : html`<p class="muted">No change in the configuration: the commit left it as it was.</p>`}
  </div>`;
}

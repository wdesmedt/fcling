// A table of rows as the server renders them: filtered, sorted, and capped so a big RIB stays quick.

import { html, useMemo, useState } from "./vendor/preact-htm.module.js";
import { natural } from "./api.js";

const PAGE = 500;

/** `columns` are titles; `rows` arrays of cell text, the node first. `filter` and `onFilter` may be lifted. */
export function DataTable({ columns, rows, onNode, filter: lifted, onFilter, ordered = false, empty = "No rows." }) {
  const [own, setOwn] = useState("");
  const filter = lifted ?? own;
  const setFilter = onFilter ?? setOwn;
  const [sort, setSort] = useState(null); // {column, descending}
  const [shown, setShown] = useState(PAGE);

  const visible = useMemo(() => {
    const terms = filter.toLowerCase().split(/\s+/).filter(Boolean);
    let out = terms.length
      ? rows.filter((row) => {
          const text = row.join(" ").toLowerCase();
          return terms.every((t) => text.includes(t));
        })
      : rows;
    if (sort) {
      const i = sort.column;
      out = [...out].sort((a, b) => natural(a[i], b[i]) * (sort.descending ? -1 : 1));
    }
    return out;
  }, [rows, filter, sort]);

  const sortBy = (i) =>
    setSort(sort?.column === i ? (sort.descending ? null : { column: i, descending: true }) : { column: i, descending: false });

  // A node's rows are one group unless sorted otherwise: the node is written once per group.
  const grouped = !sort && !ordered;
  return html`<div class="data-table">
    <div class="table-tools">
      <input class="filter" value=${filter} onInput=${(e) => setFilter(e.target.value)}
             placeholder="Filter rows: every word must match" aria-label="Filter rows" />
      <span class="muted">${visible.length === rows.length ? `${rows.length} rows` : `${visible.length} of ${rows.length} rows`}</span>
    </div>
    ${visible.length === 0
      ? html`<p class="muted">${rows.length ? "No row matches the filter." : empty}</p>`
      : html`<div class="table-scroll">
          <table class="grid report">
            <thead><tr>${columns.map(
              (c, i) => html`<th onClick=${() => sortBy(i)} class=${sort?.column === i ? "sorted" : ""}>
                ${c}${sort?.column === i ? (sort.descending ? " ▾" : " ▴") : ""}</th>`,
            )}</tr></thead>
            <tbody>${visible.slice(0, shown).map((row, r) => {
              const first = r === 0 || visible[r - 1][0] !== row[0];
              return html`<tr class=${grouped && first && r > 0 ? "group" : ""}>${row.map((cell, i) =>
                i === 0
                  ? html`<td class="node-cell">${!grouped || first
                      ? onNode && cell !== "-" ? html`<a onClick=${() => onNode(cell)}>${cell}</a>` : cell
                      : ""}</td>`
                  : html`<td>${cell}</td>`,
              )}</tr>`;
            })}</tbody>
          </table>
        </div>`}
    ${visible.length > shown &&
    html`<button class="more" onClick=${() => setShown(shown + PAGE)}>Show ${Math.min(PAGE, visible.length - shown)} more</button>`}
  </div>`;
}

// A table of rows as the server renders them: filtered, sorted, and capped so a big RIB stays quick.

import { html, useMemo, useState } from "./vendor/preact-htm.module.js";
import { natural } from "./api.js";

const PAGE = 500;

/**
 * A column filter as a test of one cell: `text` contains, `!text` does not,
 * `=text` is exactly, `>10` `>=10` `<10` `<=10` compare numbers. Case does not matter.
 */
export function cellTest(expression) {
  const text = expression.trim().toLowerCase();
  if (!text) return null;
  const compare = /^(>=|<=|>|<)\s*(-?[\d.]+)$/.exec(text);
  if (compare) {
    const [, op, value] = compare;
    const limit = Number(value);
    return (cell) => {
      const n = Number.parseFloat(String(cell).replace(/,/g, ""));
      if (Number.isNaN(n)) return false;
      return op === ">" ? n > limit : op === ">=" ? n >= limit : op === "<" ? n < limit : n <= limit;
    };
  }
  if (text.startsWith("!")) {
    const not = text.slice(1);
    return not ? (cell) => !String(cell).toLowerCase().includes(not) : null;
  }
  if (text.startsWith("=")) {
    const exact = text.slice(1);
    return (cell) => String(cell).toLowerCase() === exact;
  }
  return (cell) => String(cell).toLowerCase().includes(text);
}

/**
 * `columns` are titles; `rows` arrays of cell text, the node first. The row filter
 * (`filter`, `onFilter`) and the column filters (`columnFilters` as {title: expression},
 * `onColumnFilters`) may be lifted, to keep them in the URL.
 */
export function DataTable({
  columns,
  rows,
  onNode,
  filter: lifted,
  onFilter,
  columnFilters: liftedColumns,
  onColumnFilters,
  ordered = false,
  empty = "No rows.",
}) {
  const [own, setOwn] = useState("");
  const filter = lifted ?? own;
  const setFilter = onFilter ?? setOwn;
  const [ownColumns, setOwnColumns] = useState({});
  const byColumn = liftedColumns ?? ownColumns;
  const setByColumn = onColumnFilters ?? setOwnColumns;
  const [sort, setSort] = useState(null); // {column, descending}
  const [shown, setShown] = useState(PAGE);

  const tests = useMemo(
    () =>
      columns
        .map((title, i) => [i, cellTest(byColumn[title] ?? "")])
        .filter(([, test]) => test),
    [columns, byColumn],
  );
  const visible = useMemo(() => {
    const terms = filter.toLowerCase().split(/\s+/).filter(Boolean);
    let out = rows;
    if (terms.length) {
      out = out.filter((row) => {
        const text = row.join(" ").toLowerCase();
        return terms.every((t) => text.includes(t));
      });
    }
    if (tests.length) out = out.filter((row) => tests.every(([i, test]) => test(row[i])));
    if (sort) {
      const i = sort.column;
      out = [...out].sort((a, b) => natural(a[i], b[i]) * (sort.descending ? -1 : 1));
    }
    return out;
  }, [rows, filter, tests, sort]);

  const sortBy = (i) =>
    setSort(sort?.column === i ? (sort.descending ? null : { column: i, descending: true }) : { column: i, descending: false });
  const setColumn = (title, value) => {
    const next = { ...byColumn, [title]: value };
    if (!value) delete next[title];
    setByColumn(next);
  };
  const filtering = tests.length > 0 || filter.trim();

  // A node's rows are one group unless sorted otherwise: the node is written once per group.
  const grouped = !sort && !ordered;
  return html`<div class="data-table">
    <div class="table-tools">
      <input class="filter" value=${filter} onInput=${(e) => setFilter(e.target.value)}
             placeholder="Filter rows: every word must match" aria-label="Filter rows" />
      <span class="muted">${visible.length === rows.length ? `${rows.length} rows` : `${visible.length} of ${rows.length} rows`}</span>
      ${filtering &&
      html`<button class="link-button inline" onClick=${() => {
        setFilter("");
        setByColumn({});
      }}>Clear filters</button>`}
      <span class="muted small hint" title="text: contains · !text: does not · =text: exactly · >10, <=5: numbers">
        Column filters: text, !text, =text, ${">10"}
      </span>
    </div>
    ${rows.length === 0
      ? html`<p class="muted">${empty}</p>`
      : html`<div class="table-scroll">
          <table class="grid report">
            <thead>
              <tr>${columns.map(
                (c, i) => html`<th onClick=${() => sortBy(i)} class=${sort?.column === i ? "sorted" : ""}>
                  ${c}${sort?.column === i ? (sort.descending ? " ▾" : " ▴") : ""}</th>`,
              )}</tr>
              <tr class="column-filters">${columns.map(
                (c) => html`<th><input value=${byColumn[c] ?? ""} class=${byColumn[c] ? "on" : ""}
                                       onInput=${(e) => setColumn(c, e.target.value)}
                                       placeholder="filter" aria-label=${`Filter ${c}`} /></th>`,
              )}</tr>
            </thead>
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
          ${visible.length === 0 && html`<p class="muted no-match">No row matches the filters.</p>`}
        </div>`}
    ${visible.length > shown &&
    html`<button class="more" onClick=${() => setShown(shown + PAGE)}>Show ${Math.min(PAGE, visible.length - shown)} more</button>`}
  </div>`;
}

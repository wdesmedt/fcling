// A table of rows as the server renders them: filtered, sorted, and capped so a big RIB stays quick.

import { html, useEffect, useLayoutEffect, useMemo, useRef, useState } from "./vendor/preact-htm.module.js";
import { natural } from "./api.js";
import { bgpPeerRibPath } from "./bgp-links.js";

export function cellPlain(cell) {
  if (cell && typeof cell === "object" && cell.kind === "bgp-rx-act-tx") {
    return `${cell.rx}/${cell.act}/${cell.tx}`;
  }
  if (cell && typeof cell === "object" && cell.kind === "action") return "";
  return String(cell ?? "");
}

const PAGE = 500;
const FILTER_WAIT_MS = 150;
const FILTER_SLICE = 4000;

export function useDebounced(value, ms) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

/**
 * A column filter as a test of one cell: `text` contains, `!text` does not,
 * `=text` is exactly, or one item of a comma- or line-separated list, `>10` `>=10` `<10` `<=10` compare numbers, `a|b` either. Case does not matter.
 */
export function cellTest(expression) {
  const text = expression.trim().toLowerCase();
  if (!text) return null;
  if (text.includes("|")) {
    const any = text.split("|").map(cellTest).filter(Boolean);
    return any.length ? (cell) => any.some((test) => test(cell)) : null;
  }
  const compare = /^(>=|<=|>|<)\s*(-?[\d.]+)$/.exec(text);
  if (compare) {
    const [, op, value] = compare;
    const limit = Number(value);
    return (cell) => {
      const n = Number.parseFloat(cellPlain(cell).replace(/,/g, ""));
      if (Number.isNaN(n)) return false;
      return op === ">" ? n > limit : op === ">=" ? n >= limit : op === "<" ? n < limit : n <= limit;
    };
  }
  if (text.startsWith("!")) {
    const not = text.slice(1);
    return not ? (cell) => !cellPlain(cell).toLowerCase().includes(not) : null;
  }
  if (text.startsWith("=")) {
    const exact = text.slice(1);
    return (cell) => {
      const value = cellPlain(cell).toLowerCase();
      return value === exact || value.split(/,\s*|\n/).some((item) => item.trim() === exact);
    };
  }
  return (cell) => cellPlain(cell).toLowerCase().includes(text);
}

const signature = (row) => row.map((c) => (typeof c === "object" ? JSON.stringify(c) : c)).join("\u0001");

/**
 * The rows that are new or changed since the rows before, when `flash` - what the rows are
 * of - is the same: a live update, not another report, time or question.
 */
function useFresh(rows, flash) {
  const before = useRef({ flash: undefined, seen: null });
  return useMemo(() => {
    const signatures = rows.map(signature);
    const { flash: was, seen } = before.current;
    before.current = { flash, seen: new Set(signatures) };
    if (flash === undefined || flash !== was || !seen) return new Set();
    return new Set(signatures.filter((s) => !seen.has(s)));
  }, [rows, flash]);
}

const MIN_WIDTH = 40;

/**
 * Column widths the user dragged, kept per set of columns; `null` until then, and the
 * table sizes itself. The first drag fixes every column at the width it has.
 */
function useWidths(columns) {
  const key = `fcli-widths:${columns.join("|")}`;
  const load = () => {
    try {
      const saved = JSON.parse(localStorage.getItem(key));
      return Array.isArray(saved) && saved.length === columns.length ? saved : null;
    } catch {
      return null;
    }
  };
  const [widths, setWidths] = useState(load);
  useEffect(() => setWidths(load()), [key]);
  const save = (next) => {
    setWidths(next);
    try {
      if (next) localStorage.setItem(key, JSON.stringify(next));
      else localStorage.removeItem(key);
    } catch {
      // Storage may be blocked: the widths then last as long as the page.
    }
  };
  const resize = (e, i) => {
    e.preventDefault();
    e.stopPropagation();
    const headers = [...e.currentTarget.closest("tr").children];
    const start = widths ?? headers.map((th) => th.getBoundingClientRect().width);
    const x = e.clientX;
    const handle = e.currentTarget;
    try {
      handle.setPointerCapture(e.pointerId);
    } catch {
      // No pointer to capture (a synthetic event): the moves still reach the handle.
    }
    let next = start;
    const move = (m) => {
      next = start.map((w, j) => (j === i ? Math.max(MIN_WIDTH, Math.round(w + m.clientX - x)) : Math.round(w)));
      setWidths(next);
    };
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
      save(next);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  };
  return [widths, resize, () => save(null)];
}

/**
 * `columns` are titles; `rows` arrays of cell text, the node first. The row filter
 * (`filter`, `onFilter`) and the column filters (`columnFilters` as {title: expression},
 * `onColumnFilters`) may be lifted, to keep them in the URL. A cell
 * `{kind: "action", label}` is a button that hands the cell to `onAction`.
 */
function renderCell(cell, go, onAction) {
  if (cell?.kind === "action") {
    return onAction
      ? html`<button class="row-action" disabled=${cell.disabled} title=${cell.title ?? ""}
                     onClick=${() => onAction(cell)}>${cell.label}</button>`
      : "";
  }
  if (cell?.kind === "bgp-rx-act-tx" && go) {
    const link = (which, n, title) =>
      html`<a class="rib-count" href=${`#/${bgpPeerRibPath(cell, which)}`} title=${title}
            onClick=${(e) => {
              e.preventDefault();
              go(bgpPeerRibPath(cell, which));
            }}>${n}</a>`;
    return html`${link("rx", cell.rx, "Received routes")}/${link("act", cell.act, "Active routes")}/${link(
      "tx",
      cell.tx,
      "Advertised routes",
    )}`;
  }
  return cell;
}

export function DataTable({
  columns,
  rows,
  onNode,
  go,
  onAction,
  filter: lifted,
  onFilter,
  columnFilters: liftedColumns,
  onColumnFilters,
  ordered = false,
  empty = "No rows.",
  flash,
}) {
  const fresh = useFresh(rows, flash);
  const [own, setOwn] = useState("");
  const filter = lifted ?? own;
  const setFilter = onFilter ?? setOwn;
  const [ownColumns, setOwnColumns] = useState({});
  const byColumn = liftedColumns ?? ownColumns;
  const setByColumn = onColumnFilters ?? setOwnColumns;
  const [sort, setSort] = useState(null); // {column, descending}
  const [shown, setShown] = useState(PAGE);
  const debouncedFilter = useDebounced(filter, FILTER_WAIT_MS);

  useEffect(() => {
    setShown(PAGE);
  }, [debouncedFilter, byColumn, sort, rows.length]);

  const tests = useMemo(
    () =>
      columns
        .map((title, i) => [i, cellTest(byColumn[title] ?? "")])
        .filter(([, test]) => test),
    [columns, byColumn],
  );
  const visible = useMemo(() => {
    const terms = debouncedFilter.toLowerCase().split(/\s+/).filter(Boolean);
    let out = rows;
    if (terms.length) {
      out = out.filter((row) => {
        const text = row.map(cellPlain).join(" ").toLowerCase();
        return terms.every((t) => text.includes(t));
      });
    }
    if (tests.length) {
      if (out.length > FILTER_SLICE) {
        const matched = [];
        for (let i = 0; i < out.length; i += FILTER_SLICE) {
          const slice = out.slice(i, i + FILTER_SLICE);
          matched.push(...slice.filter((row) => tests.every(([i, test]) => test(row[i]))));
        }
        out = matched;
      } else {
        out = out.filter((row) => tests.every(([i, test]) => test(row[i])));
      }
    }
    if (sort) {
      const i = sort.column;
      out = [...out].sort((a, b) => natural(a[i], b[i]) * (sort.descending ? -1 : 1));
    } else if (!ordered) {
      // Unsorted, nodes come in their natural order (leaf2 before leaf10), each
      // node's rows as the report gave them: the sort is stable. Ranked once, as
      // a node has many rows.
      const rank = new Map([...new Set(out.map((row) => String(row[0])))].sort(natural).map((n, i) => [n, i]));
      const of = (row) => rank.get(String(row[0]));
      if (out.some((row, r) => r > 0 && of(out[r - 1]) > of(row))) out = [...out].sort((a, b) => of(a) - of(b));
    }
    return out;
  }, [rows, debouncedFilter, tests, sort, ordered]);

  const sortBy = (i) =>
    setSort(sort?.column === i ? (sort.descending ? null : { column: i, descending: true }) : { column: i, descending: false });
  const setColumn = (title, value) => {
    const next = { ...byColumn, [title]: value };
    if (!value) delete next[title];
    setByColumn(next);
  };
  const filtering = tests.length > 0 || filter.trim();
  const [widths, resize, resetWidths] = useWidths(columns);
  const scrollRef = useRef(null);
  const scrollTop = useRef(0);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      scrollTop.current = el.scrollTop;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = scrollTop.current;
  }, [rows]);

  // A node's rows are one group unless sorted otherwise: the node is written once per group.
  const grouped = !sort && !ordered;
  const twins = new Map();
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
      <span class="muted small hint" title="text: contains · !text: does not · =text: exactly, or one item of a list · >10, <=5: numbers · a|b: either">
        Column filters: text, !text, =text, ${">10"}, a|b
      </span>
    </div>
    ${rows.length === 0
      ? html`<p class="muted">${empty}</p>`
      : html`<div class="table-scroll" ref=${scrollRef}>
          <table class=${`grid report${widths ? " sized" : ""}`}
                 style=${widths ? { width: `${widths.reduce((a, b) => a + b, 0)}px` } : null}>
            ${widths && html`<colgroup>${widths.map((w) => html`<col style=${{ width: `${w}px` }} />`)}</colgroup>`}
            <thead>
              <tr>${columns.map(
                (c, i) => html`<th onClick=${() => sortBy(i)} class=${sort?.column === i ? "sorted" : ""}>
                  ${c}${sort?.column === i ? (sort.descending ? " ▾" : " ▴") : ""}
                  <span class="col-resize" title="Drag to resize; double-click to fit the columns again"
                        onClick=${(e) => e.stopPropagation()} onPointerDown=${(e) => resize(e, i)}
                        onDblClick=${(e) => {
                          e.stopPropagation();
                          resetWidths();
                        }}></span></th>`,
              )}</tr>
              <tr class="column-filters">${columns.map(
                (c) => html`<th><input value=${byColumn[c] ?? ""} class=${byColumn[c] ? "on" : ""}
                                       onInput=${(e) => setColumn(c, e.target.value)}
                                       placeholder="filter" aria-label=${`Filter ${c}`} /></th>`,
              )}</tr>
            </thead>
            <tbody>${visible.slice(0, shown).map((row, r) => {
              const first = r === 0 || visible[r - 1][0] !== row[0];
              const sig = signature(row);
              const n = (twins.get(sig) ?? 0) + 1;
              twins.set(sig, n);
              const classes = [grouped && first && r > 0 && "group", fresh.has(sig) && "fresh"].filter(Boolean);
              // Keyed by content, so a changed row is a new element and its flash plays again.
              return html`<tr key=${`${sig}\u0002${n}`} class=${classes.join(" ")}>${row.map((cell, i) =>
                i === 0
                  ? html`<td class="node-cell">${!grouped || first
                      ? onNode && cell !== "-" ? html`<a onClick=${() => onNode(cell)}>${cell}</a>` : cell
                      : ""}</td>`
                  : html`<td>${renderCell(cell, go, onAction)}</td>`,
              )}</tr>`;
            })}</tbody>
          </table>
          ${visible.length === 0 && html`<p class="muted no-match">No row matches the filters.</p>`}
        </div>`}
    ${visible.length > shown &&
    html`<button class="more" onClick=${() => setShown(shown + PAGE)}>Show ${Math.min(PAGE, visible.length - shown)} more</button>`}
  </div>`;
}

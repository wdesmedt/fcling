"""Records out: a Rich table for people, JSON/YAML/CSV for everything else.

JSON and YAML are the records themselves - typed, nested, BGP's own words.
The table and CSV are the report's columns, which is where a record is turned
into what fits a cell.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol

import yaml
from rich import box
from rich.console import Console
from rich.table import Table

from .delta import record_key, stable
from .report import Column, Record

FORMATS = ("table", "json", "yaml", "csv")

#: API tables larger than this are truncated so a RIB lookup cannot wedge the server.
TABLE_MAX_ROWS = 25_000


class Report(Protocol):
    """What renders as a table: a report, or a lens."""

    @property
    def name(self) -> str: ...

    @property
    def help(self) -> str: ...

    def table(self, detail: bool = False) -> Sequence[Column]: ...


def cells(report: Report, record: Record, detail: bool = False) -> dict[str, Any]:
    """``{column title: value}`` for one record."""
    return {c.title: c.value(record) for c in report.table(detail)}


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict) and value.get("kind") == "bgp-rx-act-tx":
        return f"{value['rx']}/{value['act']}/{value['tx']}"
    if isinstance(value, list | tuple):
        return "\n".join(text(v) for v in value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def table_cell(value: Any) -> Any:
    """Cell for JSON tables: structured values pass through, scalars become text."""
    if isinstance(value, dict) and value.get("kind"):
        return value
    return text(value)


# --------------------------------------------------------------------------- #
# field filters: -f state=active
# --------------------------------------------------------------------------- #


class FilterError(ValueError):
    pass


def parse_filters(report: Report, specs: Sequence[str]) -> list[tuple[str, re.Pattern[str]]]:
    """``column=regex`` pairs; a column is named by its title or its record field."""
    by_name: dict[str, str] = {}
    for column in report.table(detail=True):
        by_name[" ".join(column.title.split()).lower()] = column.title
        if isinstance(column.cell, str):
            by_name[column.cell.lower()] = column.title
    by_name["node"] = "node"
    filters = []
    for spec in specs:
        name, sep, pattern = spec.partition("=")
        if not sep:
            raise FilterError(f"filter {spec!r} is not column=regex")
        title = by_name.get(name.strip().lower())
        if title is None:
            known = ", ".join(sorted({" ".join(c.title.split()) for c in report.table(detail=True)}))
            raise FilterError(f"{report.name} has no column {name!r}; it has: node, {known}")
        try:
            filters.append((title, re.compile(pattern, re.IGNORECASE)))
        except re.error as exc:
            raise FilterError(f"filter {spec!r}: {exc}") from exc
    return filters


def keep(report: Report, record: Record, filters: Sequence[tuple[str, re.Pattern[str]]]) -> bool:
    if not filters:
        return True
    row = cells(report, record, detail=True)
    row["node"] = record.node
    return all(pattern.search(text(row.get(title))) for title, pattern in filters)


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #


def sorted_records(records: Iterable[Record], report: Any = None) -> list[Record]:
    if getattr(report, "ordered", False):
        return list(records)
    return sorted(records, key=lambda r: _natural(r.node))


def _node_digest(records: Sequence[Record]) -> str:
    """Fingerprint of what the table shows for one node's rows.

    When a record type marks fields :attr:`~fcli.report.Record.volatile`, those
    values are still rendered in cells and must bust the cache when they move.
    """
    if not records:
        return hashlib.sha256(b"").hexdigest()
    keyed = sorted(records, key=record_key)
    if type(records[0]).volatile:
        payload = [r.model_dump(mode="json") for r in keyed]
    else:
        payload = [stable(r) for r in keyed]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def table_rows(
    report: Report,
    records: Iterable[Record],
    *,
    cache: Mapping[tuple[str, str], tuple[str, list[list[str]]]] | None = None,
    cache_key: str = "",
) -> tuple[list[str], list[list[str]]]:
    """Column titles and row cell text, with optional per-node reuse in *cache*.

    *cache_key* scopes entries (report name plus parameters). When a node's
    records are unchanged, its rows are taken from the cache so a large RIB
    re-rendered after one node changed does not re-format every other node.
    """
    columns = report.table()
    titles = ["Node", *(c.title for c in columns)]
    by_node: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        by_node[record.node].append(record)
    rows: list[list[str]] = []
    mutable = cache if isinstance(cache, dict) else None
    for node in sorted(by_node, key=_natural):
        node_records = sorted_records(by_node[node], report)
        digest = _node_digest(node_records)
        key = (cache_key, node)
        if mutable is not None:
            hit = mutable.get(key)
            if hit and hit[0] == digest:
                rows.extend(hit[1])
                continue
        node_rows = [
            [record.node, *(table_cell(value) for value in cells(report, record).values())]
            for record in node_records
        ]
        if mutable is not None:
            mutable[key] = (digest, node_rows)
        rows.extend(node_rows)
    return titles, rows


def table_titles(report: Report) -> list[str]:
    columns = report.table()
    return ["Node", *(c.title for c in columns)]


def rows_for_records(report: Report, records: Iterable[Record]) -> list[list[Any]]:
    """Table rows for *records* without the per-node render cache."""
    return [
        [record.node, *(table_cell(value) for value in cells(report, record).values())] for record in records
    ]


def truncate_rows(rows: list[list[Any]], limit: int = TABLE_MAX_ROWS) -> tuple[list[list[Any]], str | None]:
    """Cap row count for HTTP answers; return a note when rows were dropped."""
    if len(rows) <= limit:
        return rows, None
    return rows[:limit], f"Showing first {limit:,} of {len(rows):,} rows; narrow the query."


def _natural(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


def table(
    report: Report,
    records: Iterable[Record],
    console: Console,
    box_style: str = "MINIMAL",
    detail: bool = False,
) -> None:
    columns = report.table(detail)
    t = Table(title=report.help, box=getattr(box, box_style.upper(), box.MINIMAL), highlight=True)
    t.add_column("Node", no_wrap=True, style="bold")
    for column in columns:
        t.add_column(column.title)
    previous = None
    for record in sorted_records(records, report):
        if previous is not None and record.node != previous:
            t.add_section()
        node = record.node if record.node != previous else ""
        # As they are: an ESI's ":ab:" is no emoji, a path's "[...]" no markup.
        cells = (
            console.render_str(text(column.value(record)), markup=False, emoji=False, highlight=True)
            for column in columns
        )
        t.add_row(node, *cells)
        previous = record.node
    console.print(t)


def as_json(records: Iterable[Record], report: Any = None) -> str:
    return json.dumps([r.model_dump(mode="json") for r in sorted_records(records, report)], indent=2)


def as_yaml(records: Iterable[Record], report: Any = None) -> str:
    return yaml.safe_dump(
        [r.model_dump(mode="json") for r in sorted_records(records, report)], sort_keys=False
    )


def as_csv(report: Report, records: Iterable[Record], detail: bool = False) -> str:
    columns = report.table(detail)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["node", *(" ".join(c.title.split()) for c in columns)])
    for record in sorted_records(records, report):
        writer.writerow([record.node, *(text(c.value(record)).replace("\n", " ") for c in columns)])
    return out.getvalue()

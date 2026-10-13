"""Records out: a Rich table for people, JSON/YAML/CSV for everything else.

JSON and YAML are the records themselves - typed, nested, BGP's own words.
The table and CSV are the report's columns, which is where a record is turned
into what fits a cell.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Sequence
from typing import Any, Protocol

import yaml
from rich import box
from rich.console import Console
from rich.table import Table

from .report import Column, Record

FORMATS = ("table", "json", "yaml", "csv")


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
    if isinstance(value, list | tuple):
        return "\n".join(text(v) for v in value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


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


def _sorted(records: Iterable[Record], report: Any = None) -> list[Record]:
    if getattr(report, "ordered", False):
        return list(records)
    return sorted(records, key=lambda r: _natural(r.node))


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
    for record in _sorted(records, report):
        if previous is not None and record.node != previous:
            t.add_section()
        node = record.node if record.node != previous else ""
        t.add_row(node, *(text(column.value(record)) for column in columns))
        previous = record.node
    console.print(t)


def as_json(records: Iterable[Record], report: Any = None) -> str:
    return json.dumps([r.model_dump(mode="json") for r in _sorted(records, report)], indent=2)


def as_yaml(records: Iterable[Record], report: Any = None) -> str:
    return yaml.safe_dump([r.model_dump(mode="json") for r in _sorted(records, report)], sort_keys=False)


def as_csv(report: Report, records: Iterable[Record], detail: bool = False) -> str:
    columns = report.table(detail)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["node", *(" ".join(c.title.split()) for c in columns)])
    for record in _sorted(records, report):
        writer.writerow([record.node, *(text(c.value(record)).replace("\n", " ") for c in columns)])
    return out.getvalue()

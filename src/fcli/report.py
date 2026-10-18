"""What a report is: declared paths in, typed records out.

A report never does I/O. It declares the gNMI paths it reads (:class:`Read`),
and is a pure function from a node's :class:`~fcli.tree.Tree` to records. Who
fills the tree is not its concern: the CLI does it with a one-shot Get, a
server will do it with a subscription, the tests do it from recordings.

Records are flat - one record is one row - and frozen pydantic models, so the
same definition gives the JSON output, a schema for tools, and a key to diff
two snapshots on. :class:`Column` says how a record is shown in a table,
which is the only place a column title is written.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, model_validator

from .gnmi.client import DataType, SubscriptionMode
from .tree import Tree


def _order_key(value: Any) -> list[Any]:
    """*value* as text, its numbers compared as numbers: ``10.0.0.2`` before ``10.0.0.10``."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    text = json.dumps(value, sort_keys=True, default=str)
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


class Value(BaseModel):
    """A frozen model whose ``unordered`` fields are kept in one order, whatever
    order the tree gave them in."""

    model_config = ConfigDict(frozen=True)

    #: Fields that are sets, not sequences - ECMP next-hops, the peers of a
    #: service. The tree lists entries in the order updates came in, so an entry
    #: withdrawn and learnt again moves to the end: sorted, the record does not
    #: change with it, and neither does the history.
    unordered: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="before")
    @classmethod
    def _sort_unordered(cls, data: Any) -> Any:
        if cls.unordered and isinstance(data, dict):
            data = {**data, **{n: sorted(data[n], key=_order_key) for n in cls.unordered if data.get(n)}}
        return data


class Record(Value):
    """One row of a report, on one node."""

    #: The fields that identify a record on its node, across two runs: what a
    #: diff matches rows on. Every field when empty.
    key: ClassVar[tuple[str, ...]] = ()
    #: Fields that change all the time and mean nothing on their own -
    #: counters, rates, a countdown. A change to them alone is pushed live but
    #: not kept as history. ``families.received`` names a field of each
    #: element of a nested list.
    volatile: ClassVar[tuple[str, ...]] = ()
    #: The fields that name the network-instance a record is in, or the ones
    #: it is in: what :data:`EXCLUDED_INSTANCES` is matched on.
    instances: ClassVar[tuple[str, ...]] = ("ni", "nis")

    node: str


R = TypeVar("R", bound=Record)

#: Interfaces no report shows, with their subinterfaces. The management port
#: carries no fabric traffic: its counters, DHCP lease and the lab host's ARP
#: entries would only be noise in every report, the history and the checks.
EXCLUDED_INTERFACES = ("mgmt0",)

#: Network-instances no report shows: ``mgmt`` holds only mgmt0 and its
#: routes to the lab network, which are no part of the fabric.
EXCLUDED_INSTANCES = ("mgmt",)

#: The record fields that name an interface or subinterface of the node.
_INTERFACE_FIELDS = ("interface", "name")


def excluded_interface(name: str) -> bool:
    """``mgmt0`` and ``mgmt0.0`` are excluded; ``ethernet-1/1`` is not."""
    return any(name == x or name.startswith(x + ".") for x in EXCLUDED_INTERFACES)


def _excluded_instance(value: Any) -> bool:
    """A network-instance that is excluded, or a set of them that all are:
    an ARP entry in ``mgmt`` alone goes, one also in ``default`` stays."""
    if isinstance(value, str):
        return value in EXCLUDED_INSTANCES
    return isinstance(value, tuple) and bool(value) and all(v in EXCLUDED_INSTANCES for v in value)


def excluded(record: Record) -> bool:
    """Whether *record* is about an excluded interface or network-instance."""
    values = (getattr(record, f, None) for f in _INTERFACE_FIELDS)
    if any(isinstance(v, str) and excluded_interface(v) for v in values):
        return True
    return any(_excluded_instance(getattr(record, f, None)) for f in record.instances)


@dataclass(frozen=True, slots=True)
class Read:
    """One path a report reads, and how it should be streamed."""

    path: str
    #: A Get with datatype ``state`` answers nothing for a leaf SR Linux
    #: defines as config (``interface/admin-state``), so a path that reads
    #: config leaves on their own asks for ``all``. Never ``config`` alone: a
    #: subscription has no datatype and streams state and defaults with it, so
    #: the report would read one thing live and another one-shot.
    datatype: DataType = DataType.STATE
    #: For a subscription: ON_CHANGE for what changes rarely, SAMPLE for
    #: counters, which ON_CHANGE would send on every tick.
    mode: SubscriptionMode = SubscriptionMode.ON_CHANGE
    sample_interval: float = 0.0


@dataclass(frozen=True, slots=True)
class NodeState:
    """What a report gets to read about one node."""

    name: str
    tree: Tree
    #: ``{YANG model name: version}`` from gNMI capabilities.
    models: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    #: For a report that compares two samples: the earlier one, and the
    #: seconds between them.
    previous: Tree | None = None
    elapsed: float | None = None


@dataclass(frozen=True, slots=True)
class Column:
    title: str
    #: A record field name, or a function of the record.
    cell: str | Callable[[Any], Any]

    def value(self, record: Record) -> Any:
        if isinstance(self.cell, str):
            return getattr(record, self.cell)
        return self.cell(record)


@dataclass(frozen=True, slots=True)
class Param:
    """An argument a user gives a report: what it builds, never what it reads."""

    name: str
    help: str
    default: Any = None
    choices: tuple[str, ...] = ()
    #: A one-letter CLI flag, ``a`` for ``-a``.
    short: str = ""


#: Reads that depend on what was read before: the next-hop-groups the routes
#: name, the attribute sets of the BGP paths. Called with the tree and the
#: report's parameters, again with each fuller tree until it asks for nothing
#: new; pure, like the report itself.
Follow = Callable[[Tree, Mapping[str, Any]], Iterable[Read]]


@dataclass(frozen=True)
class Report:
    name: str
    help: str
    reads: tuple[Read, ...]
    record: type[Record]
    build: Callable[..., Iterable[Record]]
    columns: tuple[Column, ...]
    #: Seconds between two samples, for a report of rates; 0 for one sample.
    interval: float = 0.0
    follow: Follow | None = None
    params: tuple[Param, ...] = ()
    #: Columns a table shows only when asked for detail; JSON always has the fields.
    detail_columns: tuple[Column, ...] = ()
    #: Not a command of its own: reached through another, as a ``bgp-rib`` family is.
    hidden: bool = False
    #: gNMI paths depend on parameters (BGP RIB by route key, or the whole table).
    reads_for: Callable[[Mapping[str, Any]], Iterable[Read]] | None = None

    def param_values(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        values = {p.name: p.default for p in self.params}
        if params:
            values.update({k: v for k, v in params.items() if v is not None})
        return values

    def planned_reads(self, params: Mapping[str, Any] | None = None) -> tuple[Read, ...]:
        values = self.param_values(params)
        if self.reads_for is not None:
            return tuple(self.reads_for(values))
        return self.reads

    def run(self, node: NodeState, **params: Any) -> list[Record]:
        values = {p.name: p.default for p in self.params}
        unknown = set(params) - set(values)
        if unknown:
            raise TypeError(f"{self.name} takes no parameter {', '.join(sorted(unknown))}")
        values.update({k: v for k, v in params.items() if v is not None})
        return list(self.records(node, values))

    def records(self, node: NodeState, values: Mapping[str, Any]) -> Iterator[Record]:
        """What the report builds, less the records of an excluded interface or
        network-instance: filtered here, every surface - a table, the history,
        the checks - is without them, and no change is ever recorded for one."""
        return (r for r in self.build(node, **values) if not excluded(r))

    def table(self, detail: bool = False) -> tuple[Column, ...]:
        return self.columns + self.detail_columns if detail else self.columns


#: Every report, by name, in the order they were defined.
REGISTRY: dict[str, Report] = {}


def default_columns(record: type[Record]) -> tuple[Column, ...]:
    return tuple(Column(name.replace("_", "-"), name) for name in record.model_fields if name != "node")


def report(
    name: str,
    help: str,  # noqa: A002
    *,
    record: type[R],
    reads: Iterable[Read],
    columns: Iterable[Column] | None = None,
    interval: float = 0.0,
    follow: Follow | None = None,
    params: Iterable[Param] = (),
    detail_columns: Iterable[Column] = (),
    hidden: bool = False,
    reads_for: Callable[[Mapping[str, Any]], Iterable[Read]] | None = None,
) -> Callable[[Callable[..., Iterable[R]]], Callable[..., Iterable[R]]]:
    """Register the decorated function as the report *name*."""

    def register(build: Callable[..., Iterable[R]]) -> Callable[..., Iterable[R]]:
        if name in REGISTRY:
            raise ValueError(f"report {name!r} is defined twice")
        REGISTRY[name] = Report(
            name=name,
            help=help,
            reads=tuple(reads),
            record=record,
            build=build,
            columns=tuple(columns) if columns is not None else default_columns(record),
            interval=interval,
            follow=follow,
            params=tuple(params),
            detail_columns=tuple(detail_columns),
            hidden=hidden,
            reads_for=reads_for,
        )
        return build

    return register


# --------------------------------------------------------------------------- #
# reading payloads
# --------------------------------------------------------------------------- #


def as_list(value: Any) -> list[Any]:
    """A YANG list that may be absent, a single dict, or a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def as_int(value: Any) -> int | None:
    """An integer leaf; ``json_ietf`` sends 64-bit ones as strings."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def dig(value: Any, *names: str) -> Any:
    """``value[a][b][c]``, or ``None`` as soon as a level is missing."""
    for name in names:
        if not isinstance(value, dict):
            return None
        value = value.get(name)
    return value

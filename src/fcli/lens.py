"""Lenses: questions about the whole fabric that take an argument.

A report reads one node; a check asks the fabric a fixed question. A lens asks
the one an engineer has in mind - where is this address, how would this leaf
reach that one, what does this service look like everywhere - and answers it
from several reports across every node at once.

Like a check, a lens is a pure function of a :class:`~fcli.fabric.Fabric` and
declares the reports it reads, so it answers the same from the nodes, from the
engine, or from the history at an instant. Like a report, it answers in typed
records with columns, so every surface - the CLI, the API, the tools - renders
it without knowing which lens it is.

A lens may also *query*: read a report from the nodes when it is asked, keyed
by what its declared reports and its arguments say - the prefix a route-table
matches an address with, in a BGP RIB no engine streams whole. The history has
no such reads: at an instant, a lens answers without them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .fabric import Fabric
from .report import Column, Record, default_columns


class LensError(ValueError):
    """An argument that does not make sense for this fabric, said in words."""


@dataclass(frozen=True)
class Arg:
    name: str
    help: str
    #: A required argument is positional on the command line.
    required: bool = False
    #: Positional though optional: ``fcling service`` and ``fcling service macvrf-1``.
    positional: bool = False
    default: Any = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Lens:
    name: str
    help: str
    requires: tuple[str, ...]
    args: tuple[Arg, ...]
    record: type[Record]
    columns: tuple[Column, ...]
    build: Callable[..., Iterable[Record]]
    #: What a report has and a lens does not need: so either renders the same.
    detail_columns: tuple[Column, ...] = ()
    #: Rows in the order the lens gave them - a path's hops - not sorted by node.
    ordered: bool = False
    #: The fabric of its declared reports and its arguments -> the reports to read
    #: from the nodes when asked, each with its params; a report may come more than once.
    queries: Callable[..., Iterable[tuple[str, Mapping[str, Any]]]] | None = None

    def table(self, detail: bool = False) -> tuple[Column, ...]:
        return self.columns

    def queried(self, fabric: Fabric, **args: Any) -> list[tuple[str, dict[str, Any]]]:
        """The reports to read from the nodes for these arguments over *fabric*, with their params."""
        if self.queries is None:
            return []
        return [(r, dict(p)) for r, p in self.queries(fabric, **self._values(args))]

    def run(self, fabric: Fabric, **args: Any) -> list[Record]:
        return list(self.build(fabric, **self._values(args)))

    def _values(self, args: Mapping[str, Any]) -> dict[str, Any]:
        values = {a.name: a.default for a in self.args}
        unknown = set(args) - set(values)
        if unknown:
            raise LensError(f"{self.name} takes no argument {', '.join(sorted(unknown))}")
        values.update({k: v for k, v in args.items() if v not in (None, "")})
        for a in self.args:
            if a.required and not values.get(a.name):
                raise LensError(f"{self.name} needs {a.name}: {a.help}")
            if a.choices and values.get(a.name) not in (None, *a.choices):
                raise LensError(f"{a.name}: {values[a.name]!r} is not one of {', '.join(a.choices)}")
        return values


LENSES: dict[str, Lens] = {}


def lens(
    name: str,
    help: str,  # noqa: A002
    *,
    requires: Sequence[str],
    record: type[Record],
    args: Sequence[Arg] = (),
    columns: Sequence[Column] | None = None,
    ordered: bool = False,
    queries: Callable[..., Iterable[tuple[str, Mapping[str, Any]]]] | None = None,
) -> Callable[[Callable[..., Iterable[Record]]], Callable[..., Iterable[Record]]]:
    def register(build: Callable[..., Iterable[Record]]) -> Callable[..., Iterable[Record]]:
        LENSES[name] = Lens(
            name=name,
            help=help,
            requires=tuple(requires),
            args=tuple(args),
            record=record,
            columns=tuple(columns) if columns is not None else default_columns(record),
            build=build,
            ordered=ordered,
            queries=queries,
        )
        return build

    return register

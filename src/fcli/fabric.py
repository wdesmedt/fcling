"""The whole fabric at once: what checks and incidents read.

A report sees one node; a check sees all of them, which is what lets it say
that a link is only seen from one end, or that two leaves disagree about a
service. :class:`Fabric` is every node's records of the reports a check
needs, from wherever they came: a one-shot collection, the engine's live
state, or the history at an instant - the checks cannot tell, and need not.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .inventory import Node
from .report import Record

#: Ports that carry management rather than fabric traffic.
OUT_OF_BAND = ("mgmt",)


def out_of_band(port: str) -> bool:
    return port.strip().lower().startswith(OUT_OF_BAND)


def parent(subinterface: str) -> str:
    """``ethernet-1/1.0`` is a subinterface of ``ethernet-1/1``."""
    return subinterface.rsplit(".", 1)[0]


def index(subinterface: str) -> str:
    """``ethernet-1/1.0`` is index ``0`` of its parent."""
    return subinterface.rsplit(".", 1)[-1] if "." in subinterface else ""


def text(value: Any) -> str:
    """*value* as the lowercase string a state comparison wants."""
    return str(value if value is not None else "").strip().lower()


def _tail(name: str) -> str:
    """``clab-dc1-leaf1`` is ``leaf1``."""
    return name.rsplit("-", 1)[-1] or name


def alias_index(nodes: Iterable[Sequence[str]]) -> dict[str, str]:
    """Every name a node may be known by - its LLDP system-name, an FQDN, a
    containerlab container name - onto its inventory name. *nodes* gives the
    names of each node, its inventory name first. An alias two nodes could
    both answer to is dropped rather than guessed at."""
    direct: dict[str, set[str]] = {}
    tails: dict[str, set[str]] = {}
    for names in nodes:
        if not names:
            continue
        owner = names[0]
        for alias in names:
            if not alias:
                continue
            name = alias.strip().lower()
            short = name.split(".")[0]
            direct.setdefault(name, set()).add(owner)
            direct.setdefault(short, set()).add(owner)
            tails.setdefault(_tail(short), set()).add(owner)
    found = {alias: next(iter(owners)) for alias, owners in direct.items() if len(owners) == 1}
    for alias, owners in tails.items():
        if alias not in found and len(owners) == 1:
            found[alias] = next(iter(owners))
    return found


@dataclass
class Fabric:
    #: Every node of the inventory, reachable or not.
    nodes: tuple[str, ...]
    #: Report name -> every node's records of it.
    records: Mapping[str, Sequence[Record]]
    #: Nodes nothing could be read from, and why.
    unreachable: Mapping[str, str] = field(default_factory=dict)
    #: Containerlab nodes: a veth drops what a real port would not.
    virtual: frozenset[str] = frozenset()
    aliases: Mapping[str, str] = field(default_factory=dict)
    #: Recent history, oldest first, where one is kept: what flapping reads.
    changes: Sequence[Mapping[str, Any]] = ()

    @classmethod
    def of(
        cls,
        nodes: Sequence[Node],
        records: Mapping[str, Sequence[Record]],
        unreachable: Mapping[str, str] | None = None,
        changes: Sequence[Mapping[str, Any]] = (),
    ) -> Fabric:
        return cls(
            nodes=tuple(n.name for n in nodes),
            records=records,
            unreachable=dict(unreachable or {}),
            virtual=frozenset(n.name for n in nodes if n.virtual),
            aliases=alias_index([(n.name, n.address) for n in nodes]),
            changes=changes,
        )

    def get(self, report: str) -> Sequence[Any]:
        return self.records.get(report, ())

    def answered(self, report: str) -> set[str]:
        """The nodes *report* was read from: every node not unreachable, when it was read at all."""
        if report not in self.records:
            return set()
        return {n for n in self.nodes if n not in self.unreachable}

    def resolve(self, advertised: str) -> str | None:
        """The inventory name of an advertised system-name, if the node is ours."""
        name = advertised.strip().lower()
        short = name.split(".")[0]
        return self.aliases.get(name) or self.aliases.get(short) or self.aliases.get(_tail(short))

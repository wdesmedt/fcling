"""routing-pol: routing policies, statement by statement, and the sets they use."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, ClassVar

from ..gnmi.client import DataType
from ..report import Column, NodeState, Read, Record, as_list, report


class PolicyItem(Record):
    """A policy statement, a policy's default action, or a set and its members."""

    key: ClassVar[tuple[str, ...]] = ("kind", "name", "entry")

    #: ``policy``, ``prefix-set``, ``community-set``, ``extended-community-set``,
    #: ``large-community-set``, ``as-path-set``, ``tag-set``.
    kind: str
    name: str
    #: A statement's name, ``default`` for a policy's default action; a set's member.
    entry: str = ""
    match: dict[str, Any] = {}
    action: dict[str, Any] = {}

    @property
    def result(self) -> str:
        return str(self.action.get("policy-result") or "")


def flatten(value: Any, path: tuple[str, ...] = ()) -> list[str]:
    """``name=value`` for every leaf, named by its last two containers.

    ``bgp/extended-community/extended-community-set`` reads as
    ``extended-community.extended-community-set``: enough to say what it is.
    """
    if isinstance(value, dict):
        return [item for k, v in value.items() for item in flatten(v, (*path, k))]
    name = ".".join(path[-2:]) if len(path) > 1 else "".join(path)
    if isinstance(value, list):
        return [f"{name}={','.join(str(v) for v in value)}"]
    return [f"{name}={value}" if value is not None else name]


#: Each kind of set: the list its members are in, and how a member reads.
_SETS: dict[str, tuple[str, Callable[[Any], str]]] = {
    "prefix-set": ("prefix", lambda p: f"{p.get('ip-prefix')} {p.get('mask-length-range', '')}".strip()),
    "community-set": ("member", str),
    "extended-community-set": ("member", str),
    "large-community-set": ("member", str),
    "as-path-set": ("expression", str),
    "tag-set": ("tag-value", str),
}


@report(
    "routing-pol",
    "Routing policies and the sets they match on",
    record=PolicyItem,
    reads=[Read("/routing-policy", DataType.ALL)],
    columns=[
        Column("kind", "kind"),
        Column("name", "name"),
        Column("entry", "entry"),
        Column("match", lambda i: " ".join(flatten(i.match))),
        Column("result", lambda i: i.result),
        Column(
            "action",
            lambda i: " ".join(flatten({k: v for k, v in i.action.items() if k != "policy-result"})),
        ),
    ],
)
def routing_pol(node: NodeState) -> Iterator[PolicyItem]:
    rp = node.tree.get("/routing-policy") or {}
    for kind, (members, show) in _SETS.items():
        for s in as_list(rp.get(kind)):
            if not isinstance(s, dict):
                continue
            entries = as_list(s.get(members)) or [None]
            for entry in entries:
                yield PolicyItem(
                    node=node.name,
                    kind=kind,
                    name=str(s.get("name") or ""),
                    entry="" if entry is None else show(entry),
                )
    for policy in as_list(rp.get("policy")):
        if not isinstance(policy, dict):
            continue
        name = str(policy.get("name") or "")
        for statement in as_list(policy.get("statement")):
            if isinstance(statement, dict):
                yield PolicyItem(
                    node=node.name,
                    kind="policy",
                    name=name,
                    entry=str(statement.get("name") or ""),
                    match=statement.get("match") or {},
                    action=statement.get("action") or {},
                )
        if policy.get("default-action"):
            yield PolicyItem(
                node=node.name, kind="policy", name=name, entry="default", action=policy["default-action"]
            )

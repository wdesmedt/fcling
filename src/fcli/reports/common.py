"""What several reports read the same way."""

from __future__ import annotations

import datetime

from ..gnmi.client import DataType
from ..report import Read
from ..tree import Tree

#: Which network-instances bind which subinterfaces: the interface lists
#: alone, not the network-instance subtree, which carries the BGP RIBs.
NI_INTERFACES = Read("/network-instance[name=*]/interface", DataType.ALL)

SUBINTERFACES = Read("/interface[name=*]/subinterface", DataType.ALL)


def instances_by_interface(tree: Tree) -> dict[str, tuple[str, ...]]:
    """``{subinterface: (network-instance, ...)}``; an irb is in two."""
    bound: dict[str, list[str]] = {}
    for match in tree.select("/network-instance/interface"):
        name = match.value.get("name") if isinstance(match.value, dict) else None
        if name:
            bound.setdefault(str(name), []).append(str(match.keys.get("network-instance")))
    # Sorted: the order is how the node happened to list them, which a stream
    # and a Get need not agree on, and a set has none.
    return {name: tuple(sorted(nis)) for name, nis in bound.items()}


def subinterface_name(interface: str, subinterface: dict[str, object]) -> str:
    """``ethernet-1/1.0``: SR Linux names some subinterfaces by their index alone."""
    name = str(subinterface.get("name") or "")
    if not name or name.isdigit():
        return f"{interface}.{subinterface.get('index', '')}"
    return name


def parse_time(value: object) -> datetime.datetime | None:
    """A device timestamp, ``2026-09-24T11:06:19.369Z``, in UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def countdown(at: object) -> str:
    """Time left until a device timestamp, ``3:58:52s``; a dash where there is none."""
    when = parse_time(at)
    if when is None:
        return "-"
    seconds = int((when - datetime.datetime.now(datetime.UTC)).total_seconds())
    return f"{datetime.timedelta(seconds=seconds)}s"


def joined(values: object, sep: str = ", ") -> str:
    return sep.join(str(v) for v in values) if isinstance(values, list | tuple) else ""

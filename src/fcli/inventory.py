"""Which nodes there are and how to reach them.

Two sources: a containerlab topology file, whose SR Linux nodes are the
fabric, and a static YAML inventory for anything else::

    defaults:
      username: admin
      password: NokiaSrl1!
      port: 57400
      tls: {ca_file: ca.pem}
    nodes:
      leaf1: {address: 10.0.0.11, labels: {role: leaf}}
      spine1: {address: 10.0.0.21, labels: {role: spine}}
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .gnmi.client import DEFAULT_PORT, Target, Tls

SRL_DEFAULT_USERNAME = "admin"
SRL_DEFAULT_PASSWORD = "NokiaSrl1!"

#: Kinds containerlab reserves for SR Linux, which identify a node whatever its image.
SRL_KINDS = ("srl", "nokia_srlinux")


class InventoryError(ValueError):
    pass


@dataclass(frozen=True)
class Node:
    #: What the node is called in every report.
    name: str
    #: What to connect to: a hostname or an address.
    address: str
    port: int = DEFAULT_PORT
    username: str = SRL_DEFAULT_USERNAME
    password: str = field(default=SRL_DEFAULT_PASSWORD, repr=False)
    tls: Tls = field(default_factory=Tls)
    labels: Mapping[str, str] = field(default_factory=dict)
    #: A containerlab node: some checks read a veth differently from a port.
    virtual: bool = False

    def target(self) -> Target:
        return Target(self.address, self.port, self.username, self.password, self.tls)


@dataclass(frozen=True)
class Inventory:
    #: The fabric's name: the lab's, or the inventory file's directory.
    name: str
    nodes: tuple[Node, ...]

    def select(self, filters: Sequence[str]) -> Inventory:
        """The nodes matching every ``key=glob`` filter; ``name`` is a key too."""
        parsed = []
        for spec in filters:
            key, sep, pattern = spec.partition("=")
            if not sep:
                raise InventoryError(f"inventory filter {spec!r} is not key=value")
            parsed.append((key.strip(), pattern.strip()))

        def matches(node: Node) -> bool:
            for key, pattern in parsed:
                value = node.name if key in ("name", "hostname") else node.labels.get(key)
                if value is None or not fnmatch.fnmatchcase(str(value).lower(), pattern.lower()):
                    return False
            return True

        return replace(self, nodes=tuple(n for n in self.nodes if matches(n)))

    def override(self, **changes: Any) -> Inventory:
        """Every node with *changes* applied; ``None`` values are left alone."""
        changes = {k: v for k, v in changes.items() if v is not None}
        if not changes:
            return self
        return replace(self, nodes=tuple(replace(n, **changes) for n in self.nodes))


# --------------------------------------------------------------------------- #
# containerlab
# --------------------------------------------------------------------------- #


def _mapping(parent: Any, key: str) -> dict[str, Any]:
    """``parent[key]`` as a mapping: a bare ``leaf1:`` in YAML parses as ``None``."""
    value = parent.get(key) if isinstance(parent, dict) else None
    return value if isinstance(value, dict) else {}


def clab_prefix(topo: Mapping[str, Any]) -> str:
    """What containerlab prepends to every node name of *topo*."""
    lab = topo["name"]
    if "prefix" not in topo:
        return f"clab-{lab}-"
    prefix = topo["prefix"]
    if prefix == "__lab-name":
        return f"{lab}-"
    return f"{prefix}-{lab}-" if prefix else ""


def _inherited(topology: dict[str, Any], spec: dict[str, Any], key: str) -> Any:
    """*key* for a node: its own, else its group's, else the topology defaults."""
    group = _mapping(_mapping(topology, "groups"), spec.get("group", ""))
    for source in (spec, group, _mapping(topology, "defaults")):
        if source.get(key):
            return source[key]
    return None


def _is_srl(topology: dict[str, Any], spec: dict[str, Any]) -> bool:
    kind = _inherited(topology, spec, "kind")
    if kind in SRL_KINDS:
        return True
    image = _mapping(_mapping(topology, "kinds"), kind or "").get("image")
    return "srlinux" in str(image or _inherited(topology, spec, "image") or "")


def _labels(topology: dict[str, Any], spec: dict[str, Any]) -> dict[str, str]:
    group = _mapping(_mapping(topology, "groups"), spec.get("group", ""))
    labels: dict[str, Any] = {}
    for source in (_mapping(topology, "defaults"), group, spec):
        labels.update(_mapping(source, "labels"))
    return {str(k): str(v) for k, v in labels.items()}


def from_clab(path: str | Path) -> Inventory:
    """The SR Linux nodes of a containerlab topology, reached by container name."""
    path = Path(path)
    try:
        topo = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise InventoryError(f"cannot read topology {path}: {exc}") from exc
    if not isinstance(topo, dict) or "name" not in topo:
        raise InventoryError(f"{path} is not a containerlab topology: it has no name")
    topology = _mapping(topo, "topology")
    prefix = clab_prefix(topo)
    nodes = []
    for name, spec in _mapping(topology, "nodes").items():
        spec = spec if isinstance(spec, dict) else {}
        if not _is_srl(topology, spec):
            continue
        nodes.append(
            Node(name=str(name), address=f"{prefix}{name}", labels=_labels(topology, spec), virtual=True)
        )
    return Inventory(name=str(topo["name"]), nodes=tuple(nodes))


# --------------------------------------------------------------------------- #
# static YAML
# --------------------------------------------------------------------------- #

_NODE_FIELDS = ("address", "port", "username", "password")


def _node(name: str, spec: Mapping[str, Any], defaults: Mapping[str, Any]) -> Node:
    merged = {**defaults, **spec}
    unknown = set(merged) - {*_NODE_FIELDS, "tls", "labels"}
    if unknown:
        raise InventoryError(f"node {name}: unknown setting(s) {', '.join(sorted(unknown))}")
    tls = {**_mapping(defaults, "tls"), **_mapping(spec, "tls")}
    try:
        return Node(
            name=name,
            address=str(merged.get("address") or name),
            port=int(merged.get("port") or DEFAULT_PORT),
            username=str(merged.get("username") or SRL_DEFAULT_USERNAME),
            password=str(merged.get("password") or SRL_DEFAULT_PASSWORD),
            tls=Tls(**tls),
            labels={**_mapping(defaults, "labels"), **_mapping(spec, "labels")},
        )
    except TypeError as exc:
        raise InventoryError(f"node {name}: {exc}") from exc


def from_file(path: str | Path) -> Inventory:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise InventoryError(f"cannot read inventory {path}: {exc}") from exc
    if "topology" in data and "name" in data:
        return from_clab(path)
    defaults = _mapping(data, "defaults")
    nodes = _mapping(data, "nodes")
    if not nodes:
        raise InventoryError(f"{path} lists no nodes")
    return Inventory(
        name=str(data.get("name") or path.resolve().parent.name),
        nodes=tuple(_node(str(n), s if isinstance(s, dict) else {}, defaults) for n, s in nodes.items()),
    )


def discover(cwd: str | Path = ".") -> Path | None:
    """The one containerlab topology in *cwd*, if there is exactly one."""
    found = sorted(Path(cwd).glob("*.clab.y*ml"))
    return found[0] if len(found) == 1 else None

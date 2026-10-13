"""The state of one node, as a tree that gNMI notifications merge into.

The tree is plain JSON: containers are dicts, YANG lists are lists of dicts,
leaf-lists are lists of scalars. That is the shape a ``json_ietf`` payload
already has, so a Get merges in without translation and a report reads it the
way it would read the payload.

What a payload does not say is which leaves key a list. gNMI paths do -
``interface[name=ethernet-1/1]`` - but SR Linux answers a wildcard Get with
one update at the deepest concrete ancestor and every list below it as a JSON
array. Merging two such answers (``/interface[name=*]/statistics`` and
``/interface[name=*]/subinterface``) has to line entries up by key, so keys
come from :data:`LIST_KEYS`, from the paths seen so far, and failing both from
the leaves that key most SR Linux lists.

Everything written is normalised on the way in: YANG module prefixes are
stripped from member names and from identity values, so a report reads
``type == "routed"`` and never ``srl_nokia-interfaces:routed``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeGuard

from .gnmi import path as gpath
from .gnmi.path import Path

if TYPE_CHECKING:
    from .gnmi.client import Notification

#: The keys of SR Linux lists, by the end of their schema path. Only lists
#: that two declared paths can both reach need to be here; anything else is
#: written in one piece and never merged.
LIST_KEYS: dict[str, tuple[str, ...]] = {
    "interface": ("name",),
    "subinterface": ("index",),
    "network-instance": ("name",),
    "bgp/neighbor": ("peer-address",),
    "bgp/group": ("group-name",),
    "afi-safi": ("afi-safi-name",),
    "lldp/interface/neighbor": ("id",),
    "arp/neighbor": ("ipv4-address",),
    "neighbor-discovery/neighbor": ("ipv6-address",),
    "address": ("ip-prefix",),
    "mac-table/mac": ("address",),
    "ethernet-segment": ("name",),
    "tunnel-interface": ("name",),
    "vxlan-interface": ("index",),
    "bgp-instance": ("id",),
    "control": ("slot",),
    "linecard": ("slot",),
    "member": ("name",),
    "commit": ("id",),
    "attr-set": ("index",),
    "next-hop-group": ("index",),
    "route-table/next-hop": ("index",),
    "ipv4-unicast/route": ("ipv4-prefix", "route-type", "route-owner", "id", "origin-network-instance"),
    "ipv6-unicast/route": ("ipv6-prefix", "route-type", "route-owner", "id", "origin-network-instance"),
    # The BGP RIBs: several keys each, where one alone - a prefix - repeats per path.
    "local-rib/route": ("prefix", "neighbor", "origin-protocol", "path-id", "route-distinguisher"),
    "ethernet-ad-route": ("route-distinguisher", "esi", "ethernet-tag-id", "neighbor", "path-id"),
    "mac-ip-route": (
        "route-distinguisher",
        "ethernet-tag-id",
        "mac-length",
        "mac-address",
        "ip-address",
        "neighbor",
        "path-id",
    ),
    "imet-route": ("route-distinguisher", "ethernet-tag-id", "originating-router", "neighbor", "path-id"),
    "ethernet-segment-route": ("route-distinguisher", "esi", "originating-router", "neighbor", "path-id"),
    "ip-prefix-route": (
        "route-distinguisher",
        "ethernet-tag-id",
        "ip-prefix-length",
        "ip-prefix",
        "neighbor",
        "path-id",
    ),
}

#: Leaves that key most SR Linux lists, tried in order for a list nobody named.
_LIKELY_KEYS = ("name", "index", "id", "slot", "address", "prefix", "ip-prefix")

_MODULE_PREFIX = re.compile(r"^srl_nokia-[\w-]+:")


def strip_modules(value: Any) -> Any:
    """*value* without YANG module prefixes on member names or identity values."""
    if isinstance(value, dict):
        return {k.rpartition(":")[2]: strip_modules(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_modules(v) for v in value]
    if isinstance(value, str) and value.startswith("srl_nokia-"):
        return _MODULE_PREFIX.sub("", value)
    return value


def _same(a: Any, b: Any) -> bool:
    # Path keys are strings; the payload has subinterface index 0, not "0".
    return a == b or str(a) == str(b)


def _is_list(value: Any) -> TypeGuard[list[dict[str, Any]]]:
    return isinstance(value, list) and all(isinstance(v, dict) for v in value)


class _Entries(list):  # type: ignore[type-arg]
    """A YANG list that knows its entries by key.

    A subscription addresses entries one keyed path at a time - a thousand
    subinterfaces, each leaf a path of its own - so finding an entry has to
    cost nothing. Still a list: reports and JSON never see the difference.
    """

    __slots__ = ("by_key", "index_keys", "groups", "group_keys")

    def __init__(self, entries: Any = ()) -> None:
        super().__init__(entries)
        self.by_key: dict[tuple[str, ...], dict[str, Any]] = {}
        self.index_keys: tuple[str, ...] | None = None
        #: Every entry by the values of *group_keys*, which need not be all
        #: of the list's keys: what a read naming some of them selects.
        self.groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        self.group_keys: tuple[str, ...] | None = None

    def find(self, keys: tuple[tuple[str, str], ...]) -> dict[str, Any] | None:
        names = tuple(k for k, _ in keys)
        if self.index_keys != names:
            self.by_key = {tuple(str(e.get(k)) for k in names): e for e in self if isinstance(e, dict)}
            self.index_keys = names
        return self.by_key.get(tuple(str(v) for _, v in keys))

    def matching(self, keys: tuple[tuple[str, str], ...]) -> list[dict[str, Any]]:
        """The entries whose leaves *keys* names hold those values, in list order."""
        names = tuple(k for k, _ in keys)
        if self.group_keys != names:
            self.groups = {}
            for e in self:
                if isinstance(e, dict):
                    self.groups.setdefault(tuple(str(e.get(k)) for k in names), []).append(e)
            self.group_keys = names
        return self.groups.get(tuple(str(v) for _, v in keys), [])

    def add(self, entry: dict[str, Any]) -> None:
        self.append(entry)
        if self.index_keys is not None:
            self.by_key[tuple(str(entry.get(k)) for k in self.index_keys)] = entry
        if self.group_keys is not None:
            self.groups.setdefault(tuple(str(entry.get(k)) for k in self.group_keys), []).append(entry)

    def invalidate(self) -> None:
        self.index_keys = None
        self.group_keys = None


@dataclass(frozen=True, slots=True)
class Match:
    """One place a query landed: the value there, and the keys on the way."""

    value: Any
    #: ``{"network-instance": "default", "neighbor": "10.0.0.1"}``: the value of
    #: each keyed element along the path; ``"element.key"`` for multi-key lists.
    keys: Mapping[str, Any]


class Tree:
    """The state of one node. Written by one task, read by reports, never shared."""

    def __init__(self) -> None:
        self.root: dict[str, Any] = {}
        self._keys: dict[str, tuple[str, ...]] = {}

    # ------------------------------------------------------------------ keys

    def _list_keys(self, schema: str, *lists: list[dict[str, Any]]) -> tuple[str, ...] | None:
        learned = self._keys.get(schema)
        if learned:
            return learned
        for suffix, keys in LIST_KEYS.items():
            if schema == suffix or schema.endswith("/" + suffix):
                return keys
        for key in _LIKELY_KEYS:
            # Every entry has it, and it repeats within no list: a leaf that
            # repeats is not what keys it.
            if any(lists) and all(
                all(key in e for e in entries) and len({str(e[key]) for e in entries}) == len(entries)
                for entries in lists
            ):
                return (key,)
        return None

    def _learn(self, schema: str, keys: tuple[tuple[str, str], ...]) -> None:
        if keys and schema not in self._keys:
            self._keys[schema] = tuple(k for k, _ in keys)

    # ----------------------------------------------------------------- write

    def apply(self, notification: Notification) -> None:
        """A gNMI notification: its deletes, then its updates, as the spec orders them."""
        for path in notification.deletes:
            self.delete(path)
        for path, value in notification.updates:
            self.update(path, value)

    def update(self, path: str | Path, value: Any) -> None:
        """Merge *value* in at *path*, as a gNMI update does."""
        path = gpath.parse(path)
        value = strip_modules(value)
        if not path:
            if isinstance(value, dict):
                self._merge_dict(self.root, value, "")
            return
        parent = self._descend(path[:-1], create=True)
        assert parent is not None
        last = path[-1]
        schema = gpath.schema(path)
        if last.keys:
            self._learn(schema, last.keys)
            entry = self._entry(parent, last.name, last.keys, create=True)
            assert entry is not None
            if isinstance(value, dict):
                self._merge_dict(entry, value, schema)
            return
        self._merge_member(parent, last.name, value, schema)

    def delete(self, path: str | Path) -> None:
        """Remove what *path* names, as a gNMI delete does."""
        path = gpath.parse(path)
        if not path:
            self.root.clear()
            return
        parent = self._descend(path[:-1], create=False)
        if parent is None:
            return
        last = path[-1]
        if not last.keys:
            parent.pop(last.name, None)
            return
        entries = parent.get(last.name)
        if isinstance(entries, list):
            want = last.key_dict
            parent[last.name] = [e for e in entries if not all(_same(e.get(k), v) for k, v in want.items())]

    def prune(self, path: str | Path) -> None:
        """Remove everything *path* names, wildcards included: what a delete
        of each of :meth:`select`'s matches would.

        A polled subtree is pruned and written again whole, which is how an
        entry the node dropped leaves the tree: a Get, like a SAMPLE
        subscription, never says what is gone.
        """
        path = gpath.parse(path)
        if not path:
            self.root.clear()
            return
        self._prune(self.root, path)

    def _prune(self, node: Any, path: Path) -> None:
        if not isinstance(node, dict):
            return
        elem, rest = path[0], path[1:]
        child = node.get(elem.name)
        if child is None:
            return
        if not isinstance(child, list) or (child and not isinstance(child[0], dict)):
            if elem.keys:
                return
            if rest:
                self._prune(child, rest)
            else:
                del node[elem.name]
            return
        want = elem.key_dict

        def selected(entry: Any) -> bool:
            return isinstance(entry, dict) and all(
                v == gpath.WILDCARD or _same(entry.get(k), v) for k, v in want.items()
            )

        if rest:
            for entry in child:
                if selected(entry):
                    self._prune(entry, rest)
        else:
            node[elem.name] = [e for e in child if not selected(e)]

    def _descend(self, path: Path, create: bool) -> dict[str, Any] | None:
        node = self.root
        for i, elem in enumerate(path):
            if elem.keys:
                self._learn(gpath.schema(path[: i + 1]), elem.keys)
                child = self._entry(node, elem.name, elem.keys, create)
            else:
                child = node.get(elem.name)
                if child is None and create:
                    child = node[elem.name] = {}
            if not isinstance(child, dict):
                return None
            node = child
        return node

    @staticmethod
    def _entry(
        node: dict[str, Any], name: str, keys: tuple[tuple[str, str], ...], create: bool
    ) -> dict[str, Any] | None:
        entries = node.get(name)
        if isinstance(entries, dict):  # a one-entry list some encoders unwrap
            entries = [entries]
        if not isinstance(entries, list):
            if not create:
                return None
            entries = []
        if not isinstance(entries, _Entries):
            entries = node[name] = _Entries(entries)
        entry = entries.find(keys)
        if entry is not None or not create:
            return entry
        entry = dict(keys)
        entries.add(entry)
        return entry

    def _merge_member(self, node: dict[str, Any], name: str, value: Any, schema: str) -> None:
        current = node.get(name)
        if isinstance(current, dict) and isinstance(value, dict):
            self._merge_dict(current, value, schema)
        elif _is_list(current) and _is_list(value) and value:
            self._merge_list(current, value, schema)
        else:
            node[name] = value

    def _merge_dict(self, into: dict[str, Any], value: dict[str, Any], schema: str) -> None:
        for name, child in value.items():
            self._merge_member(into, name, child, f"{schema}/{name}" if schema else name)

    def _merge_list(self, into: list[dict[str, Any]], value: list[dict[str, Any]], schema: str) -> None:
        keys = self._list_keys(schema, into, value)
        if isinstance(into, _Entries):
            into.invalidate()
        if keys is None:
            into[:] = value
            return
        index = {tuple(str(e.get(k)) for k in keys): e for e in into}
        for entry in value:
            existing = index.get(tuple(str(entry.get(k)) for k in keys))
            if existing is None:
                into.append(entry)
            else:
                self._merge_dict(existing, entry, schema)

    # ------------------------------------------------------------------ read

    def select(self, path: str | Path) -> Iterator[Match]:
        """Every value *path* names, with ``*`` and key-less list elements as wildcards.

        A path that ends on a list yields its entries one by one.
        """
        yield from self._select(self.root, gpath.parse(path), {}, "")

    def _select(self, node: Any, path: Path, keys: dict[str, Any], schema: str) -> Iterator[Match]:
        if not path:
            yield Match(node, keys)
            return
        if not isinstance(node, dict):
            return
        elem, rest = path[0], path[1:]
        schema = f"{schema}/{elem.name}" if schema else elem.name
        child = node.get(elem.name)
        if child is None:
            return
        if isinstance(child, dict) and not elem.keys:
            yield from self._select(child, rest, keys, schema)
            return
        if not isinstance(child, list) or (child and not isinstance(child[0], dict)):
            # A leaf or a leaf-list: nothing to key into.
            if not elem.keys:
                yield from self._select(child, rest, keys, schema)
            return
        want = elem.key_dict
        if want and gpath.WILDCARD not in want.values():
            # A keyed read - an attr-set by index, among a hundred thousand -
            # is a lookup, not a scan.
            if not isinstance(child, _Entries):
                child = node[elem.name] = _Entries(child)
            entries = child.matching(elem.keys)
        else:
            entries = [e for e in child if isinstance(e, dict)]
        key_names = list(want) or list(self._list_keys(schema, entries) or ())
        for entry in entries:
            if any(v != gpath.WILDCARD and not _same(entry.get(k), v) for k, v in want.items()):
                continue
            found = dict(keys)
            if len(key_names) == 1:
                found[elem.name] = entry.get(key_names[0])
            else:
                for k in key_names:
                    found[f"{elem.name}.{k}"] = entry.get(k)
            yield from self._select(entry, rest, found, schema)

    def get(self, path: str | Path, default: Any = None) -> Any:
        """The one value at *path*, or *default*: for paths without wildcards."""
        for match in self.select(path):
            return match.value
        return default

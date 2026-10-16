"""The route tables: ipv4-rib, ipv6-rib, tunnel-table, static-routes.

A route names a next-hop-group, the group its next-hops, and an indirect
next-hop the route it resolves through - whose group leads, eventually, to a
port. On an EVPN fabric the next-hop tables hold an entry per remote VTEP and
VNI, tens of thousands, where the routes of a table use a handful: so they
are not read whole, but followed by key from what the routes name.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, ClassVar

from ..gnmi.client import DataType
from ..report import Column, Follow, NodeState, Param, Read, Record, Value, as_int, as_list, dig, report
from ..tree import Tree

#: Above this many entries asked for by key, the table is read whole instead:
#: one Get of the table beats one naming every entry.
KEYED_LIMIT = 2000

#: BGP attribute sets: a fabric keeps one per path received - tens of thousands -
#: where a RIB view uses a fraction. Keyed Gets stay practical further than
#: next-hop tables before reading the whole attr table wins.
ATTR_KEYED_LIMIT = 10_000

#: How deep an indirect next-hop is followed looking for a port.
_MAX_DEPTH = 8

NHG_LIST = "route-table/next-hop-group"
NH_LIST = "route-table/next-hop"


def keyed(
    list_path: str,
    entries: set[tuple[str, str]],
    key: str = "index",
    *,
    limit: int = KEYED_LIMIT,
) -> list[Read]:
    """Reads of *entries* - ``(network-instance, key value)`` - of a list under
    the network-instances, or of the list whole when there are too many."""
    if len(entries) > limit:
        return [Read(f"/network-instance[name=*]/{list_path}")]
    return [Read(f"/network-instance[name={ni}]/{list_path}[{key}={value}]") for ni, value in sorted(entries)]


def _resolving(nh: dict[str, Any], what: str) -> Any:
    """``resolving-route`` or ``resolving-tunnel``, under ``indirect`` or not, by release."""
    return dig(nh, "indirect", what) or nh.get(what)


def next_hop_follow(*routes: str) -> Follow:
    """Follow the next-hop-groups the lists *routes* name, down to their next-hops.

    Each of *routes* is a list of entries carrying a ``next-hop-group`` leaf,
    under a network-instance: ``route-table/ipv4-unicast/route``,
    ``tunnel-table/ipv4/tunnel``.
    """

    def follow(tree: Tree, params: Mapping[str, Any]) -> Iterable[Read]:
        groups: set[tuple[str, str]] = set()
        for path in routes:
            for m in tree.select(f"/network-instance/{path}"):
                route = m.value
                if isinstance(route, dict) and route.get("next-hop-group") is not None:
                    ni = (
                        route.get("next-hop-group-network-instance")
                        or route.get("origin-network-instance")
                        or m.keys.get("network-instance")
                    )
                    groups.add((str(ni), str(route["next-hop-group"])))
        hops: set[tuple[str, str]] = set()
        for m in tree.select("/network-instance/route-table/next-hop-group"):
            for member in as_list(m.value.get("next-hop")):
                if isinstance(member, dict) and member.get("next-hop") is not None:
                    hops.add((str(m.keys.get("network-instance")), str(member["next-hop"])))
        for m in tree.select("/network-instance/route-table/next-hop"):
            via = _resolving(m.value, "resolving-route")
            if isinstance(via, dict) and via.get("next-hop-group") is not None:
                groups.add((str(m.keys.get("network-instance")), str(via["next-hop-group"])))
        return [*keyed(NHG_LIST, groups), *keyed(NH_LIST, hops)]

    return follow


# --------------------------------------------------------------------------- #
# ipv4-rib / ipv6-rib
# --------------------------------------------------------------------------- #


class Egress(Value):
    """Where a next-hop leaves the node, resolved as far as the tables allow."""

    #: ``interface``, ``tunnel`` or ``route``.
    kind: str
    #: The subinterface, the tunnel's endpoint prefix, or the resolving prefix.
    value: str
    #: ``tunnel``: its type - ``vxlan``, ``ldp``, ``sr-isis``...
    tunnel: str = ""
    #: ``interface``: the network-instance the port is in, when not the route's own.
    ni: str = ""
    #: ``tunnel`` over VXLAN: the VNI the next-hop encapsulates with.
    vni: int | None = None

    @property
    def label(self) -> str:
        return f"{self.tunnel}:{self.value}" if self.kind == "tunnel" else self.value


class RouteNextHop(Value):
    address: str = ""
    #: ``direct``, ``indirect``...
    type: str = ""
    resolving_route: str = ""
    egress: tuple[Egress, ...] = ()


class Route(Record):
    key: ClassVar[tuple[str, ...]] = ("ni", "prefix", "type")
    unordered: ClassVar[tuple[str, ...]] = ("next_hops",)

    ni: str
    prefix: str
    #: ``bgp``, ``local``, ``host``, ``static``, ``isis``...
    type: str = ""
    active: bool = False
    metric: int | None = None
    preference: int | None = None
    #: The network-instance a leaked route came from.
    leaked_from: str = ""
    next_hops: tuple[RouteNextHop, ...] = ()


class _NextHops:
    """The next-hop and next-hop-group tables of a tree, resolved as far as they go."""

    def __init__(self, tree: Tree) -> None:
        self.hops: dict[tuple[str, str], dict[str, Any]] = {}
        for m in tree.select("/network-instance/route-table/next-hop"):
            nh = m.value
            entry: dict[str, Any] = {
                "ip-address": nh.get("ip-address"),
                "type": nh.get("type"),
                "subinterface": nh.get("subinterface"),
                "labels": tuple(
                    str(label)
                    for label in as_list(
                        dig(nh, "mpls-encapsulation", "pushed-mpls-label-stack")
                        or dig(nh, "mpls", "pushed-mpls-label-stack")
                    )
                ),
            }
            # A next-hop resolved onto a tunnel carries it under its own key
            # (``tunnel``, typed ``type``); an indirect one names the tunnel it
            # recurses on. Either way the VTEP is where the route egresses.
            tunnel = _resolving(nh, "resolving-tunnel") or nh.get("tunnel") or {}
            if tunnel:
                entry["tunnel"] = Egress(
                    kind="tunnel",
                    value=str(tunnel.get("ip-prefix") or ""),
                    tunnel=str(tunnel.get("tunnel-type") or tunnel.get("type") or ""),
                    vni=as_int(dig(nh, "vxlan-encapsulation", "vni")),
                )
            via = _resolving(nh, "resolving-route")
            if isinstance(via, dict):
                entry["resolving-route"] = via.get("ip-prefix")
                group = via.get("next-hop-group")
                entry["resolving-nhg"] = str(group) if group is not None else None
            self.hops[(str(m.keys.get("network-instance")), str(nh.get("index")))] = entry
        self.groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for m in tree.select("/network-instance/route-table/next-hop-group"):
            ni = str(m.keys.get("network-instance"))
            # Three reads can straddle a routing change: a group may name a
            # next-hop that is gone, which degrades one row, not the report.
            self.groups[(ni, str(m.value.get("index")))] = [
                self.hops.get((ni, str(member.get("next-hop"))), {})
                for member in as_list(m.value.get("next-hop"))
                if isinstance(member, dict)
            ]

    def group(self, ni: str, index: Any) -> list[dict[str, Any]]:
        return self.groups.get((ni, str(index)), [])

    def egress(self, ni: str, nh: dict[str, Any], seen: tuple[str, ...] = ()) -> list[Egress]:
        """Where a next-hop leaves the node.

        An indirect one only names the route it resolves through, so its port
        is in the group of *that* route; the prefix is what is shown when the
        chain cannot be walked to an interface.
        """
        if nh.get("subinterface"):
            return [Egress(kind="interface", value=str(nh["subinterface"]))]
        if nh.get("tunnel"):
            return [nh["tunnel"]]
        via = nh.get("resolving-nhg")
        if via and via not in seen and len(seen) < _MAX_DEPTH:
            hops = [hop for onward in self.group(ni, via) for hop in self.egress(ni, onward, (*seen, via))]
            if hops:
                return hops
        if nh.get("resolving-route"):
            return [Egress(kind="route", value=str(nh["resolving-route"]))]
        return []


def _longest_match(address: str, prefixes: Iterable[str]) -> str:
    ip = ipaddress.ip_address(address)
    best, best_len = "", -1
    for prefix in prefixes:
        try:
            net = ipaddress.ip_network(prefix, strict=False)
        except ValueError:
            continue
        if ip.version == net.version and ip in net and net.prefixlen > best_len:
            best, best_len = prefix, net.prefixlen
    return best


def _routes(node: NodeState, afi: str, address: str | None) -> Iterator[Route]:
    prefix_key = "ipv4-prefix" if afi == "ipv4-unicast" else "ipv6-prefix"
    hops = _NextHops(node.tree)
    by_ni: dict[str, list[dict[str, Any]]] = {}
    for m in node.tree.select(f"/network-instance/route-table/{afi}/route"):
        by_ni.setdefault(str(m.keys.get("network-instance")), []).append(m.value)
    for ni, routes in by_ni.items():
        if address:
            # The one prefix the address falls into, or nothing: no route to it here.
            best = _longest_match(address, (str(r.get(prefix_key)) for r in routes if r.get(prefix_key)))
            routes = [r for r in routes if r.get(prefix_key) == best] if best else []
        for route in routes:
            origin = str(route.get("origin-network-instance") or ni)
            leaked = origin != ni
            next_hops = []
            if route.get("next-hop-group") is not None:
                nhg_ni = str(route.get("next-hop-group-network-instance") or origin)
                for nh in hops.group(nhg_ni, route["next-hop-group"]):
                    next_hops.append(
                        RouteNextHop(
                            address=str(nh.get("ip-address") or ""),
                            type=str(nh.get("type") or ""),
                            resolving_route=str(nh.get("resolving-route") or ""),
                            egress=tuple(
                                # A leaked route leaves through a port of the instance it came from.
                                hop.model_copy(update={"ni": origin})
                                if leaked and hop.kind == "interface"
                                else hop
                                for hop in hops.egress(nhg_ni, nh)
                            ),
                        )
                    )
            yield Route(
                node=node.name,
                ni=ni,
                prefix=str(route.get(prefix_key) or ""),
                type=str(route.get("route-type") or ""),
                active=bool(route.get("active")),
                metric=as_int(route.get("metric")),
                preference=as_int(route.get("preference")),
                leaked_from=origin if leaked else "",
                next_hops=tuple(next_hops),
            )


def _next_hop_cell(nh: RouteNextHop) -> str:
    """A next-hop by its own address, the peer or gateway, even when indirect:
    the route it resolves through is often a subnet that reads like a next-hop."""
    if nh.type == "indirect":
        return f"{nh.address or nh.resolving_route} (indirect)"
    return nh.address


_ROUTE_COLUMNS = (
    Column("NI", "ni"),
    Column("Act", lambda r: "yes" if r.active else "no"),
    Column("Prefix", "prefix"),
    # One indirect next-hop can resolve onto several ports.
    Column(
        "itf",
        lambda r: [f"{h.label}@vrf:{h.ni}" if h.ni else h.label for nh in r.next_hops for h in nh.egress],
    ),
    Column("metric", "metric"),
    Column("next-hop", lambda r: [c for c in map(_next_hop_cell, r.next_hops) if c]),
    Column("orig-vrf", "leaked_from"),
    Column("pref", "preference"),
    Column("type", "type"),
)


def _lpm(example: str) -> Param:
    return Param(
        "address", f"longest-prefix match: the route each instance has to this address ({example})", short="a"
    )


@report(
    "ipv4-rib",
    "IPv4 route table",
    record=Route,
    reads=[Read("/network-instance[name=*]/route-table/ipv4-unicast")],
    follow=next_hop_follow("route-table/ipv4-unicast/route"),
    params=[_lpm("10.0.0.1")],
    columns=_ROUTE_COLUMNS,
)
def ipv4_rib(node: NodeState, address: str | None) -> Iterator[Route]:
    yield from _routes(node, "ipv4-unicast", address)


@report(
    "ipv6-rib",
    "IPv6 route table",
    record=Route,
    reads=[Read("/network-instance[name=*]/route-table/ipv6-unicast")],
    follow=next_hop_follow("route-table/ipv6-unicast/route"),
    params=[_lpm("2001:db8::1")],
    columns=_ROUTE_COLUMNS,
)
def ipv6_rib(node: NodeState, address: str | None) -> Iterator[Route]:
    yield from _routes(node, "ipv6-unicast", address)


# --------------------------------------------------------------------------- #
# tunnel-table
# --------------------------------------------------------------------------- #


class TunnelNextHop(Value):
    address: str = ""
    subinterface: str = ""
    type: str = ""
    #: The MPLS label stack pushed, outermost first.
    labels: tuple[str, ...] = ()


class Tunnel(Record):
    key: ClassVar[tuple[str, ...]] = ("ni", "prefix", "type")
    unordered: ClassVar[tuple[str, ...]] = ("next_hops",)

    ni: str
    prefix: str
    #: ``vxlan``, ``ldp``, ``sr-isis``, ``rsvp``...
    type: str = ""
    owner: str = ""
    preference: int | None = None
    metric: int | None = None
    next_hops: tuple[TunnelNextHop, ...] = ()


@report(
    "tunnel-table",
    "IP tunnel table (LDP, SR-ISIS, VXLAN...)",
    record=Tunnel,
    reads=[Read("/network-instance[name=*]/tunnel-table")],
    follow=next_hop_follow("tunnel-table/ipv4/tunnel", "tunnel-table/ipv6/tunnel"),
    columns=[
        Column("NI", "ni"),
        Column("Prefix", "prefix"),
        Column("type", "type"),
        Column("owner", "owner"),
        Column("pref", "preference"),
        Column("metric", "metric"),
        Column("next-hop", lambda t: [nh.address for nh in t.next_hops if nh.address]),
        Column("egress-itf", lambda t: [nh.subinterface for nh in t.next_hops if nh.subinterface]),
        Column("label", lambda t: [label for nh in t.next_hops for label in nh.labels]),
    ],
)
def tunnel_table(node: NodeState) -> Iterator[Tunnel]:
    hops = _NextHops(node.tree)
    for afi in ("ipv4", "ipv6"):
        for m in node.tree.select(f"/network-instance/tunnel-table/{afi}/tunnel"):
            ni, tunnel = str(m.keys.get("network-instance")), m.value
            yield Tunnel(
                node=node.name,
                ni=ni,
                prefix=str(tunnel.get(f"{afi}-prefix") or ""),
                type=str(tunnel.get("type") or ""),
                owner=str(tunnel.get("owner") or ""),
                preference=as_int(tunnel.get("preference")),
                metric=as_int(tunnel.get("metric")),
                next_hops=tuple(
                    TunnelNextHop(
                        address=str(nh.get("ip-address") or ""),
                        subinterface=str(nh.get("subinterface") or ""),
                        type=str(nh.get("type") or ""),
                        labels=nh.get("labels") or (),
                    )
                    for nh in hops.group(ni, tunnel.get("next-hop-group"))
                ),
            )


# --------------------------------------------------------------------------- #
# static-routes
# --------------------------------------------------------------------------- #


class StaticNextHop(Value):
    address: str
    #: Resolved through the route table rather than directly connected.
    resolve: bool = False


class StaticRoute(Record):
    key: ClassVar[tuple[str, ...]] = ("ni", "prefix")

    ni: str
    prefix: str
    admin: str = ""
    installed: bool | None = None
    metric: int | None = None
    preference: int | None = None
    next_hop_group: str = ""
    next_hops: tuple[StaticNextHop, ...] = ()


@report(
    "static-routes",
    "Static routes",
    record=StaticRoute,
    reads=[
        Read("/network-instance[name=*]/static-routes", DataType.ALL),
        Read("/network-instance[name=*]/next-hop-groups", DataType.ALL),
    ],
    columns=[
        Column("NI", "ni"),
        Column("route", "prefix"),
        Column("admin-state", "admin"),
        Column("installed", "installed"),
        Column("metric", "metric"),
        Column("pref", "preference"),
        Column("nhops", lambda r: [f"{nh.address}(R)" if nh.resolve else nh.address for nh in r.next_hops]),
    ],
)
def static_routes(node: NodeState) -> Iterator[StaticRoute]:
    groups = {
        (str(m.keys.get("network-instance")), str(m.value.get("name"))): tuple(
            StaticNextHop(address=str(nh["ip-address"]), resolve=bool(nh.get("resolve", False)))
            for nh in as_list(m.value.get("nexthop"))
            if isinstance(nh, dict) and nh.get("ip-address")
        )
        for m in node.tree.select("/network-instance/next-hop-groups/group")
    }
    for m in node.tree.select("/network-instance/static-routes/route"):
        ni, route = str(m.keys.get("network-instance")), m.value
        yield StaticRoute(
            node=node.name,
            ni=ni,
            prefix=str(route.get("prefix") or ""),
            admin=str(route.get("admin-state") or ""),
            installed=bool(route["installed"]) if route.get("installed") is not None else None,
            metric=as_int(route.get("metric")),
            preference=as_int(route.get("preference")),
            next_hop_group=str(route.get("next-hop-group") or ""),
            next_hops=groups.get((ni, str(route.get("next-hop-group"))), ()),
        )

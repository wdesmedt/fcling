"""where: every place in the fabric that knows about one address.

For a MAC, the bridge tables: where it was learned locally, where it was
learned over the overlay and from whom, and the ARP and ND entries that
resolve to it. For an IP, the subinterfaces it is configured on, the ARP and ND
entries for it - each followed into the bridge table behind its irb to the
port, segment or VTEP the host sits on - and the route each instance would
use for it: its longest match, unless that is a local, host or default route,
which say nothing of where the address is. A route of IP aliasing names the
virtual ethernet-segment of the node behind it: the one tracking its next-hop,
or - where the node aliases it to the segment's candidates, and its
route-table no longer shows that next-hop - the one whose ESI the route's
prefix carries in its EVPN IP-prefix route, queried from the BGP RIB when
asked. It says too whether the node is one of the segment's candidates: only
then does it advertise the segment's A/D routes.

A MAC learned locally on several nodes is a duplicate - unless every one of
those ports is on the same ethernet-segment, where it is a host multihomed
across them, as intended. An IP bound to different MACs on different nodes is
a duplicate address.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

from ..fabric import Fabric
from ..lens import Arg, LensError, lens
from ..report import Column, Record
from ..reports.bgp_rib import report_name
from ..reports.routing import Route
from .common import Index, ip, mac


class Sighting(Record):
    """One place that knows the address."""

    key: ClassVar[tuple[str, ...]] = ("kind", "ni", "interface", "address", "vtep", "prefix")

    #: ``configured``: it is a subinterface's own address. ``arp``/``nd``: a
    #: binding for the IP, or to the MAC. ``local``: learned on this node's
    #: port. ``remote``: learned over the overlay. ``multihomed``/``duplicate``:
    #: learned locally here and on :attr:`also_on` too. ``route``: a host route
    #: to the IP. ``not-found``: nowhere.
    kind: str
    ni: str = ""
    #: The IP or MAC this row is about.
    address: str = ""
    #: The subinterface it is configured, bound or learned on; a ``route``'s
    #: egress: its subinterface, or its tunnel and VNI to the tunnel's endpoints
    #: and the nodes they are.
    interface: str = ""
    #: A binding's MAC; a MAC's bound IP.
    mac: str = ""
    ip: str = ""
    origin: str = ""
    #: Where the MAC sits: the VTEP it is behind, or the far end of an EVPN-MPLS
    #: entry, and the segment it is on or behind.
    vtep: str = ""
    far_end: str = ""
    esi: str = ""
    segment: str = ""
    #: A binding's MAC, followed into the bridge table behind its irb.
    bridge_ni: str = ""
    learned_on: str = ""
    #: ``route``: the host route and what it resolves over.
    prefix: str = ""
    route_type: str = ""
    next_hops: tuple[str, ...] = ()
    #: ``route`` over a virtual segment's next-hop: whether the node is one of the
    #: segment's candidates there, and so advertises its A/D routes.
    attached: bool | None = None
    also_on: tuple[str, ...] = ()


def _where(s: Sighting) -> str:
    """Where the address sits, in one phrase."""
    if s.kind == "route":
        text = f"{s.route_type} route {s.prefix} via {', '.join(s.next_hops) or '?'}"
        if s.esi:
            text += f"; L3-aliasing active, ES {s.segment} ({s.esi})"
            text += "" if s.attached else ", not attached here"
        return text
    parts = []
    if s.learned_on:
        parts.append(f"in {s.bridge_ni} on {s.learned_on}")
    if s.vtep:
        parts.append(f"behind VTEP {s.vtep}")
    if s.far_end:
        parts.append(f"behind {s.far_end} over MPLS")
    if s.esi:
        # Learned on a port of the segment here; or learned behind it, elsewhere.
        on = "on" if s.learned_on or s.kind in ("local", "multihomed", "duplicate") else "behind"
        parts.append(f"{on} ES {s.segment or s.esi}")
    if s.also_on:
        parts.append(f"also on {', '.join(s.also_on)}")
    return "; ".join(parts)


_COLUMNS = (
    Column("kind", "kind"),
    Column("NI", "ni"),
    Column("interface", "interface"),
    Column("IP", "ip"),
    Column("MAC", "mac"),
    Column("origin", "origin"),
    Column("where", _where),
)

# ni: the system addresses that name a route's VTEPs.
_REQUIRES = ("mac", "arp", "nd", "es", "services", "subif", "ipv4-rib", "ipv6-rib", "lldp", "ni")

#: The EVPN IP-prefix routes: queried for the prefixes routed over EVPN, never streamed whole.
_RT5 = report_name("evpn", "5")
_NO_ESI = "00:00:00:00:00:00:00:00:00:00"


def _matches(index: Index, target: Any) -> Iterator[tuple[str, str, Route]]:
    """``(node, instance, route)``: each instance's longest match for *target*,
    where it says where the address is - not a local, host or default route."""
    for node, ni, version in index.routes:
        route = index.lookup(node, ni, target) if version == target.version else None
        if route is not None and route.type not in ("local", "host") and not route.prefix.endswith("/0"):
            yield node, ni, route


def _queries(fabric: Fabric, address: str) -> list[tuple[str, dict[str, Any]]]:
    """The prefixes an IP is routed by over EVPN, in the IP-prefix RIB: the ESI each is aliased to."""
    target = ip(address) if not mac(address) else None
    if target is None:
        return []
    routed = {r.prefix for _, _, r in _matches(Index(fabric), target) if r.type.startswith("bgp-evpn")}
    return [(_RT5, {"ip_prefix": prefix}) for prefix in sorted(routed)]


def _egress_interfaces(index: Index, route: Route) -> list[str]:
    """Where a route's next-hops leave: a subinterface, or a tunnel and its VNI to its
    endpoints, named by the node whose system address each is -
    ``vxlan VNI 1 to 192.0.2.11 (leaf1), 192.0.2.12 (leaf2)``; each once."""
    found: dict[str, dict[str, None]] = {}
    for nh in route.next_hops:
        for e in nh.egress:
            if e.kind == "interface":
                found.setdefault(e.value, {})
            elif e.kind == "tunnel":
                tunnel = f"{e.tunnel} VNI {e.vni}" if e.vni is not None else e.tunnel
                end = e.value.removesuffix("/32").removesuffix("/128")
                owner = index.loopback_owner.get(end)
                found.setdefault(tunnel, {})[f"{end} ({owner})" if owner else end] = None
    return [f"{name} to {', '.join(ends)}" if ends else name for name, ends in found.items()]


def _virtual_es(index: Index, node: str, route: Route, esis: set[str]) -> dict[str, Any]:
    """The virtual segment of *node* behind *route*, in its instance: tracking one
    of its next-hops, or with one of *esis*, those its EVPN IP-prefix routes carry;
    and whether the node is one of the segment's candidates there."""
    hops = {nh.address for nh in route.next_hops}
    for es in index.fabric.get("es"):
        if es.node != node or es.type != "virtual":
            continue
        if not hops & {nh.address for nh in es.next_hops} and es.esi.lower() not in esis:
            continue
        served = [a for a in es.associations if a.ni == route.ni]
        if es.associations and not served:
            continue
        candidates = {c.address for a in served for c in a.candidates}
        return {
            "esi": es.esi,
            "segment": es.name,
            "attached": bool(candidates & index.loopbacks.get(node, set())),
        }
    return {}


def _follow(index: Index, node: str, interface: str, address: str) -> dict[str, Any]:
    """A binding's MAC in the bridge table behind its interface: where the host sits."""
    bridge = index.bridge_of(node, interface)
    entry = index.macs.get((node, bridge, address)) if bridge else None
    if entry is None:
        return {}
    place = index.place(entry)
    return {
        "bridge_ni": bridge,
        "learned_on": place.via if place.local else "",
        "vtep": "" if place.local or entry.far_end else place.via,
        "far_end": entry.far_end,
        "esi": place.esi,
        "segment": place.segment,
    }


@lens(
    "where",
    "Every place in the fabric that knows an IP or MAC address",
    requires=_REQUIRES,
    record=Sighting,
    args=[Arg("address", "the IP or MAC address to look for", required=True)],
    columns=_COLUMNS,
    queries=_queries,
)
def where(fabric: Fabric, address: str) -> Iterator[Sighting]:
    index = Index(fabric)
    target_mac = mac(address)
    target_ip = ip(address) if not target_mac else None
    if not target_mac and target_ip is None:
        raise LensError(f"{address!r} is neither an IP nor a MAC address")
    found = list(_mac(index, target_mac) if target_mac else _ip(index, target_ip))
    if not found:
        yield Sighting(node="-", kind="not-found", address=str(target_mac or target_ip))
        return
    yield from found


def _mac(index: Index, target: str) -> Iterator[Sighting]:
    entries = [e for (node, ni, m), e in index.macs.items() if m == target]
    local = [e for e in entries if e.local]
    # One host on several leaves: multihomed when every port is on one segment.
    esis = {index.place(e).esi for e in local}
    nodes = {e.node for e in local}
    shared = "multihomed" if len(nodes) > 1 and len(esis) == 1 and "" not in esis else "duplicate"
    for e in sorted(entries, key=lambda e: (not e.local, e.node, e.ni)):
        place = index.place(e)
        kind = "local" if e.local else "remote"
        also: tuple[str, ...] = ()
        if e.local and len(nodes) > 1:
            kind = shared
            also = tuple(sorted(nodes - {e.node}))
        yield Sighting(
            node=e.node,
            kind=kind,
            ni=e.ni,
            address=target,
            interface=place.via if e.local else e.overlay,
            mac=target,
            origin=e.type,
            vtep="" if e.local or e.far_end else place.via,
            far_end=e.far_end,
            esi=place.esi,
            segment=place.segment,
            also_on=also,
        )
    for b in index.bindings:
        if mac(b.mac) == target:
            yield Sighting(
                node=b.node,
                kind="nd" if ":" in b.address else "arp",
                ni=", ".join(b.nis),
                address=target,
                interface=b.interface,
                mac=target,
                ip=b.address,
                origin=b.origin,
            )


def _ip(index: Index, target: object) -> Iterator[Sighting]:
    text = str(target)
    for s in index.fabric.get("subif"):
        for prefix in (*s.ipv4, *s.ipv6):
            if ip(prefix) == target:
                yield Sighting(
                    node=s.node,
                    kind="configured",
                    ni=", ".join(index.ni_of(s.node, s.name)),
                    address=text,
                    interface=s.name,
                    ip=text,
                    prefix=prefix,
                )
    macs_by_node: dict[str, set[str]] = {}
    bindings = [b for b in index.bindings if ip(b.address) == target]
    for b in bindings:
        if b.origin != "evpn":
            macs_by_node.setdefault(b.node, set()).add(mac(b.mac))
    distinct = {m for ms in macs_by_node.values() for m in ms if m}
    for b in bindings:
        found = _follow(index, b.node, b.interface, mac(b.mac))
        duplicate = len(distinct) > 1 and b.origin != "evpn"
        yield Sighting(
            node=b.node,
            kind="duplicate" if duplicate else "nd" if ":" in text else "arp",
            ni=", ".join(b.nis),
            address=text,
            interface=b.interface,
            mac=mac(b.mac),
            ip=text,
            origin=b.origin,
            also_on=tuple(sorted(n for n, ms in macs_by_node.items() if n != b.node and ms - {mac(b.mac)}))
            if duplicate
            else (),
            **found,
        )
    esis: dict[tuple[str, str], set[str]] = {}
    for rt5 in index.fabric.get(_RT5):
        if rt5.esi and rt5.esi != _NO_ESI:
            esis.setdefault((rt5.node, rt5.prefix), set()).add(rt5.esi.lower())
    for node, ni, r in _matches(index, target):
        yield Sighting(
            node=node,
            kind="route",
            ni=ni,
            address=text,
            ip=text,
            interface="; ".join(_egress_interfaces(index, r)),
            prefix=r.prefix,
            route_type=r.type,
            next_hops=tuple(
                nh.address + (f" ({', '.join(h.label for h in nh.egress)})" if nh.egress else "")
                for nh in r.next_hops
            ),
            **_virtual_es(index, node, r, esis.get((node, r.prefix), set())),
        )

"""path: how the fabric would forward to an address, hop by hop, from its tables.

Computed, not probed: at each node the destination is looked up in the
instance's route table, the egress port the lookup selects is followed over
LLDP to the node on the other end, and the walk goes on there. Every ECMP
branch is followed, so the answer is the whole fan-out, not the one path a
probe's hash would take.

A route that resolves onto a tunnel - VXLAN to a VTEP, LDP or SR to a far-end
PE - hands the walk to the underlay, which is walked towards the tunnel's
endpoint; there the packet is decapsulated and the walk resumes in the
instance at the far end that matches - by name, or by the route-targets it
imports - so a path through a gateway traces end to end. A route leaked from
another instance is forwarded by that instance's next-hops, and the walk
crosses into it.

A MAC destination is looked up in the bridge tables instead: learned on a port
it is delivered there; learned from a VTEP it is a tunnel to it; behind an
ethernet-segment, one tunnel per VTEP of the segment - aliasing.

An IP destination routed into an irb's subnet whose MAC the bridge table behind
the irb has from a VTEP is ``bridged``: the walk goes on in that MAC-VRF,
re-encapsulated on its VNI to the VTEPs that have the host - the hairpin a
missing host route makes visible.

A host as the source - its IP or MAC - adds an ingress hop on each node it
is attached to: the port and segment it enters the fabric on.

Hops are numbered by node: the hop is the cables crossed to reach the node,
the lookup the one made on it - hop 0 lookups 1 and 2 on the first node (a
VRF, then the underlay to a VTEP), hop 1 lookup 1 on a spine, hop 2 lookups 1
and 2 where the far VTEP decapsulates and looks up again.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, ClassVar

from ..fabric import Fabric, parent
from ..lens import Arg, LensError, lens
from ..report import Column, Record
from .common import Index, ip, mac

MAX_HOPS = 16

#: Route types for a destination attached to the node itself.
_ATTACHED = ("local", "arp-nd", "direct")
_DISCARD = ("discard", "blackhole", "reject")


class Hop(Record):
    """One lookup on one node, and what the walk did with it."""

    key: ClassVar[tuple[str, ...]] = (
        "hop",
        "lookup",
        "ni",
        "address",
        "outcome",
        "next_hop",
        "egress",
        "endpoint",
        "peer",
    )

    #: The cables crossed to reach the node.
    hop: int
    #: The lookup on the node at that hop: 1, 2... - an ingress, a VRF, the underlay.
    lookup: int = 1
    ni: str = ""
    #: What was looked up: the destination, or the tunnel endpoint the walk is
    #: chasing through the underlay.
    address: str = ""
    #: ``ingress``: where a source host enters the fabric. ``forwarded``: out of
    #: :attr:`egress` to :attr:`peer`. ``tunnel``: into a tunnel to
    #: :attr:`endpoint`. ``endpoint-reached``: decapsulated here, the walk resumes
    #: in :attr:`resumes_in`. ``leaked``: forwarded by :attr:`resumes_in`'s
    #: next-hops. ``bridged``: routed into an irb's subnet whose host the
    #: bridge table has from a VTEP: bridged on in :attr:`resumes_in`.
    #: ``delivered``: attached here, out of :attr:`egress`, to :attr:`mac` where
    #: ARP/ND or the bridge table has it. ``no-neighbor``: attached, with no
    #: ARP/ND entry for it. ``handed-off``: out of a port with
    #: no fabric node behind it, to :attr:`gateway`. ``local-ip``: the node's
    #: own. ``dead-end``, ``discard``, ``unresolved``, ``no-route``, ``loop``,
    #: ``too-long``: where a walk stops.
    outcome: str
    prefix: str = ""
    #: Who put the route in the table: ``bgp``, ``bgp-evpn``, ``static``, ``local``...
    route_type: str = ""
    #: The next-hop of the route the branch follows, before it resolves to a
    #: port or a tunnel: a PE-CE peer behind a VTEP, a link-local neighbour.
    next_hop: str = ""
    egress: str = ""
    peer: str = ""
    peer_port: str = ""
    #: ``tunnel``: ``vxlan``, ``ldp``, ``sr-isis``...; the endpoint; the VNI.
    tunnel: str = ""
    endpoint: str = ""
    vni: int | None = None
    resumes_in: str = ""
    mac: str = ""
    gateway: str = ""
    #: The ethernet-segment on the way: the source's, a tunnel's (aliasing),
    #: the one a MAC is delivered on.
    esi: str = ""
    segment: str = ""


def _via(h: Hop) -> str:
    if h.outcome == "tunnel":
        return f"{h.tunnel} to {h.endpoint}" + (f" vni {h.vni}" if h.vni is not None else "")
    return h.egress


def _then(h: Hop) -> str:
    if h.outcome == "forwarded":
        return f"{h.peer} {h.peer_port}"
    if h.outcome in ("endpoint-reached", "leaked", "bridged"):
        return f"resume in {h.resumes_in}"
    if h.mac:
        return h.mac
    if h.gateway:
        return f"gateway {h.gateway}"
    return ""


_COLUMNS = (
    Column("hop", "hop"),
    Column("lookup", "lookup"),
    Column("NI", "ni"),
    Column("address", "address"),
    Column("outcome", "outcome"),
    Column("route", "prefix"),
    Column("owner", "route_type"),
    Column("next-hop", "next_hop"),
    Column("via", _via),
    Column("then", _then),
    Column("ES", lambda h: h.segment or h.esi),
)


@dataclass(frozen=True)
class _Step:
    node: str
    ni: str
    #: The destination (an ``ipaddress`` address, or a MAC string), or a tunnel endpoint.
    address: Any
    #: Lookups made so far, on any node: what :data:`MAX_HOPS` bounds.
    depth: int
    hop: int = 0
    lookup: int = 1
    seen: tuple[str, ...] = ()
    #: Chasing a tunnel endpoint: the instance to resume in, and the instance
    #: on the origin node whose route-targets say which one that is.
    resume: tuple[str, str, str] | None = None
    #: Chasing a tunnel endpoint: what to look up once there, when not the
    #: destination - the MAC a routed destination was bridged to.
    inner: Any = None

    def next(
        self,
        node: str,
        ni: str,
        address: Any,
        seen: tuple[str, ...],
        resume: tuple[str, str, str] | None,
        inner: Any = None,
    ) -> _Step:
        """The lookup after this one: on this node the next lookup, on another the next hop."""
        hop, lookup = (self.hop, self.lookup + 1) if node == self.node else (self.hop + 1, 1)
        carried = (inner if inner is not None else self.inner) if resume is not None else None
        return _Step(node, ni, address, self.depth + 1, hop, lookup, seen, resume, carried)


def _peer_ni(index: Index, node: str, port: str, default: str) -> str:
    """The instance a port is in on the far end of a cable."""
    for (n, ni), s in index.services.items():
        if n == node and any(parent(p.name) == port for p in s.ports):
            return ni
    return default


def _resume(index: Index, node: str, origin_node: str, origin_ni: str, want: str) -> str:
    """The instance on *node* the walk resumes in: *want* by name, else one importing the origin's exports."""
    if (node, want) in index.services:
        return want
    origin = index.services.get((origin_node, origin_ni))
    exported = set(origin.export_rts) if origin else set()
    for (n, ni), s in sorted(index.services.items()):
        if n == node and exported & set(s.import_rts):
            return ni
    return ""


def _resumes(index: Index, resume: tuple[str, str, str] | None) -> Any:
    """What decides where a chase resumes: the instance wanted, and the origin's exports."""
    if resume is None:
        return None
    want, origin_node, origin_ni = resume
    origin = index.services.get((origin_node, origin_ni))
    return want, frozenset(origin.export_rts) if origin else frozenset()


def _cable(index: Index, node: str, port: str) -> tuple[str, str] | None:
    for p in index.lag_members.get((node, port)) or (port,):
        if (node, p) in index.cables:
            return index.cables[(node, p)]
    return None


@lens(
    "path",
    "How the fabric would forward to an address, hop by hop, from its tables",
    requires=(
        "ipv4-rib",
        "ipv6-rib",
        "subif",
        "lldp",
        "lag",
        "services",
        "arp",
        "nd",
        "mac",
        "es",
        "es-dest",
        "ni",
    ),
    record=Hop,
    args=[
        Arg("source", "where from: a node, or a host's IP or MAC", required=True),
        Arg("destination", "where to: an IP or a MAC", required=True),
        Arg(
            "ni",
            "the network-instance to look the destination up in [default: the source host's, else default]",
        ),
    ],
    columns=_COLUMNS,
    ordered=True,
)
def path(fabric: Fabric, source: str, destination: str, ni: str | None = None) -> list[Hop]:
    # By hop, then node: the ECMP branches of a hop side by side, leaf2 before leaf10;
    # within a node, by lookup, the address looked up and the peer each branch goes to.
    return sorted(
        _walk(fabric, source, destination, ni),
        key=lambda h: (h.hop, _natural(h.node), h.lookup, _natural(h.address), _natural(h.peer)),
    )


def _natural(text: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def _walk(fabric: Fabric, source: str, destination: str, ni: str | None) -> Iterator[Hop]:
    index = Index(fabric)
    target: Any = mac(destination) or ip(destination)
    if not target:
        raise LensError(f"{destination!r} is neither an IP nor a MAC address")
    bridged = isinstance(target, str)
    pending: list[_Step] = []
    for ingress, node, instance in _starts(index, fabric, source, ni, bridged):
        if ingress is not None:
            yield ingress
        # The first lookup follows the ingress on the same node.
        first = 1 if ingress is None else 2
        if bridged:
            tables = index.bridges_behind(node, instance)
            having = [t for t in tables if (node, t, target) in index.macs]
            for table in having[:1] or tables[:1] or [instance]:
                pending.append(_Step(node, table, target, 1, lookup=first))
        else:
            pending.append(_Step(node, instance, target, 1, lookup=first))

    made: set[tuple[Any, ...]] = set()
    while pending:
        step = pending.pop(0)
        where = f"{step.node}/{step.ni}@{step.address}"
        here: dict[str, Any] = {
            "node": step.node,
            "hop": step.hop,
            "lookup": step.lookup,
            "ni": step.ni,
            "address": str(step.address),
        }
        if step.depth > MAX_HOPS:
            yield Hop(**here, outcome="too-long")
            continue
        if where in step.seen:
            yield Hop(**here, outcome="loop")
            continue
        # ECMP branches converge: a lookup already made is one answer, given once - and
        # chases of one tunnel endpoint from two nodes are one chase where they resume alike.
        asked = (step.node, step.ni, str(step.address), _resumes(index, step.resume), step.inner)
        if asked in made:
            continue
        made.add(asked)
        seen = (*step.seen, where)
        if isinstance(step.address, str):
            yield from _bridge(index, step, here, seen, pending)
        else:
            yield from _route(index, step, here, seen, target, pending)


def _starts(
    index: Index, fabric: Fabric, source: str, ni: str | None, bridged: bool
) -> Iterator[tuple[Hop | None, str, str]]:
    """Where the walk starts: ``(ingress hop or None, node, instance)``."""
    if source in fabric.nodes:
        yield None, source, ni or "default"
        return
    source_mac = mac(source)
    source_ip = ip(source) if not source_mac else None
    if not source_mac and source_ip is None:
        raise LensError(
            f"source {source!r} is neither a node of {', '.join(fabric.nodes[:8])}... nor an IP or MAC"
        )
    found = False
    if source_ip is not None:
        for b in index.bindings:
            if ip(b.address) == source_ip and b.origin != "evpn":
                source_mac = mac(b.mac)
                break
    for (node, bridge, address), entry in sorted(index.macs.items()):
        if address != source_mac or not entry.local:
            continue
        place = index.place(entry)
        if index.fabric_facing(node, parent(place.via)):
            continue
        service = index.services.get((node, bridge))
        routed = next(iter(service.linked), "") if service else ""
        instance = ni or (bridge if bridged else routed or bridge)
        found = True
        yield (
            Hop(
                node=node,
                hop=0,
                ni=bridge,
                address=source,
                outcome="ingress",
                egress=place.via,
                mac=source_mac,
                esi=place.esi,
                segment=place.segment,
            ),
            node,
            instance,
        )
    if not found and source_ip is not None:
        # A host on a routed port has no bridge table: its ARP/ND entry places it.
        for b in index.bindings:
            if ip(b.address) == source_ip and b.origin != "evpn" and not b.interface.startswith("irb"):
                found = True
                instance = ni or next(iter(index.ni_of(b.node, b.interface)), "default")
                yield (
                    Hop(
                        node=b.node,
                        hop=0,
                        ni=instance,
                        address=source,
                        outcome="ingress",
                        egress=b.interface,
                        mac=mac(b.mac),
                    ),
                    b.node,
                    instance,
                )
    if not found:
        raise LensError(f"no node has {source} attached: try a node name as the source")


def _bridge(
    index: Index, step: _Step, here: dict[str, Any], seen: tuple[str, ...], pending: list[_Step]
) -> Iterator[Hop]:
    entry = index.macs.get((step.node, step.ni, step.address))
    if entry is None:
        yield Hop(**here, outcome="no-route")
        return
    place = index.place(entry)
    matched: dict[str, Any] = {"prefix": step.address, "route_type": entry.type}
    if place.local:
        yield Hop(
            **here,
            **matched,
            outcome="delivered",
            egress=place.via,
            mac=step.address,
            esi=place.esi,
            segment=place.segment,
        )
        return
    if entry.far_end:
        targets, kind = [entry.far_end], "mpls"
    elif entry.vtep:
        targets, kind = [entry.vtep], "vxlan"
    else:
        targets, kind = list(index.es_vteps.get((step.node, entry.overlay, entry.esi), ())), "vxlan"
    if not targets:
        yield Hop(**here, **matched, outcome="unresolved", esi=place.esi, segment=place.segment)
        return
    for endpoint in targets:
        yield Hop(
            **here,
            **matched,
            outcome="tunnel",
            tunnel=kind,
            endpoint=endpoint,
            vni=entry.vni,
            egress=entry.overlay,
            esi=place.esi,
            segment=place.segment,
        )
        address = ip(endpoint)
        if address is not None:
            resume = (step.ni, step.node, step.ni)
            pending.append(step.next(step.node, "default", address, seen, resume, step.address))


def _route(
    index: Index, step: _Step, here: dict[str, Any], seen: tuple[str, ...], target: Any, pending: list[_Step]
) -> Iterator[Hop]:
    address = step.address
    own = str(address) in index.loopbacks.get(step.node, set())
    if step.resume is not None and own:
        want, origin_node, origin_ni = step.resume
        instance = _resume(index, step.node, origin_node, origin_ni, want)
        yield Hop(**here, outcome="endpoint-reached", resumes_in=instance or "?")
        if instance:
            inner = step.inner if step.inner is not None else target
            pending.append(step.next(step.node, instance, inner, seen, None))
        return
    route = index.lookup(step.node, step.ni, address)
    if route is None:
        yield Hop(**here, outcome="no-route")
        return
    matched: dict[str, Any] = {"prefix": route.prefix, "route_type": route.type}
    if own or (route.type == "host" and route.prefix.split("/")[0] == str(address)):
        yield Hop(**here, **matched, outcome="local-ip")
        return
    if route.type in _DISCARD:
        yield Hop(**here, **matched, outcome="discard")
        return
    if route.leaked_from:
        yield Hop(**here, **matched, outcome="leaked", resumes_in=route.leaked_from)
        pending.append(step.next(step.node, route.leaked_from, address, seen, step.resume))
        return
    if route.type in _ATTACHED:
        yield from _delivered(index, step, here, matched, route, seen, pending)
        return
    branches = [(nh, egress) for nh in route.next_hops for egress in nh.egress]
    if not branches:
        yield Hop(**here, **matched, outcome="unresolved")
        return
    for nh, egress in branches:
        via = {**matched, "next_hop": nh.address}
        if egress.kind == "interface":
            port = parent(egress.value)
            far = _cable(index, step.node, port)
            if far is not None:
                peer, peer_port = far
                yield Hop(
                    **here,
                    **via,
                    outcome="forwarded",
                    egress=egress.value,
                    peer=peer,
                    peer_port=peer_port,
                )
                pending.append(
                    step.next(peer, _peer_ni(index, peer, peer_port, step.ni), address, seen, step.resume)
                )
                continue
            # No fabric node behind the port: handed to whatever ARP/ND says the next-hop is.
            bound = next(
                (
                    b
                    for b in index.bindings
                    if b.node == step.node and b.interface == egress.value and b.address == nh.address
                ),
                None,
            )
            if bound is not None:
                yield Hop(
                    **here,
                    **via,
                    outcome="handed-off",
                    egress=egress.value,
                    gateway=nh.address,
                    mac=mac(bound.mac),
                )
            else:
                yield Hop(**here, **via, outcome="dead-end", egress=egress.value, gateway=nh.address)
        elif egress.kind == "tunnel":
            endpoint = egress.value.split("/", 1)[0]
            yield Hop(
                **here, **via, outcome="tunnel", tunnel=egress.tunnel, endpoint=endpoint, vni=egress.vni
            )
            end = ip(endpoint)
            if end is not None:
                resume = step.resume or (step.ni, step.node, step.ni)
                pending.append(step.next(step.node, "default", end, seen, resume))
        else:
            yield Hop(**here, **via, outcome="unresolved", egress=egress.value)


def _delivered(
    index: Index,
    step: _Step,
    here: dict[str, Any],
    matched: dict[str, Any],
    route: Any,
    seen: tuple[str, ...],
    pending: list[_Step],
) -> Iterator[Hop]:
    """An attached destination: the port it is out of, and whether ARP/ND has it.

    Behind an irb whose bridge table has the host from a VTEP, it is not
    delivered here: it is bridged on in that table, to the VTEPs.
    """
    egress = next((h.value for nh in route.next_hops for h in nh.egress if h.kind == "interface"), "")
    bound = next(
        (
            b
            for b in index.bindings
            if b.node == step.node and ip(b.address) == step.address and (not egress or b.interface == egress)
        ),
        None,
    )
    if bound is None:
        yield Hop(**here, **matched, outcome="no-neighbor", egress=egress)
        return
    found: dict[str, Any] = {"mac": mac(bound.mac)}
    # Behind an irb, the bridge table says which port - or which VTEP - the host is on: the
    # table behind the irb it is bound on, though one host may be in several.
    behind = index.bridge_of(step.node, bound.interface)
    for bridge in [behind] if behind else index.bridges_behind(step.node, step.ni):
        entry = index.macs.get((step.node, bridge, found["mac"]))
        if entry is not None:
            place = index.place(entry)
            if place.local:
                found.update(egress=place.via, esi=place.esi, segment=place.segment)
                break
            yield Hop(
                **here,
                **matched,
                outcome="bridged",
                egress=egress or bound.interface,
                resumes_in=bridge,
                mac=found["mac"],
                esi=place.esi,
                segment=place.segment,
            )
            pending.append(step.next(step.node, bridge, found["mac"], seen, None))
            return
    yield Hop(**here, **matched, outcome="delivered", **{"egress": egress, **found})

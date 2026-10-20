"""endpoints: every host attached to the fabric, on the node it is attached to.

A host is what an ARP or ND entry binds on an access subinterface or an irb,
placed by its MAC in the bridge table; a host that only bridges is listed by
its MAC alone. A host the instance peers with over BGP, or routes other
prefixes over, is a router; one that only bridges cannot be told. Left out:
management ports, the links between fabric nodes, and hosts another node
learned over EVPN - each host is listed where it is attached, which on a
multihomed segment is every node of the segment, even the ones that learned
it from their peer.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

from ..fabric import Fabric, out_of_band, parent
from ..lens import lens
from ..report import Column, Record
from .common import Index, ip, mac

#: How many of the prefixes a router is the next-hop of are named.
_SHOWN = 3


class Endpoint(Record):
    key: ClassVar[tuple[str, ...]] = ("ip", "mac", "mac_vrf")

    ip: str = ""
    mac: str = ""
    origin: str = ""
    #: ND's reachability state.
    state: str = ""
    #: The L3 interface the binding is on: an irb, or a routed port.
    l3_interface: str = ""
    ip_vrf: str = ""
    mac_vrf: str = ""
    #: The access subinterface the host is behind: where its MAC was learned
    #: here, or on a multihomed segment's peer, this node's port on the segment.
    subinterface: str = ""
    #: ``local``: learned here. ``remote``: learned by a peer on a segment this
    #: node is also on - over the overlay, or synced to this node's own port.
    learned: str = ""
    esi: str = ""
    es: str = ""
    #: What the host calls itself over LLDP, where it runs it.
    lldp: tuple[str, ...] = ()
    #: ``router``: its IP-VRF peers with it over BGP, or routes over it.
    #: ``host``: neither. Empty for a host that only bridges.
    role: str = ""
    #: Why it is a router: ``bgp peer``, and the prefixes routed over it.
    why: tuple[str, ...] = ()


_COLUMNS = (
    Column("IP", "ip"),
    Column("MAC", "mac"),
    Column("type", "origin"),
    Column("role", "role"),
    Column("L3-interface", "l3_interface"),
    Column("subinterface", "subinterface"),
    Column("IP-VRF", "ip_vrf"),
    Column("MAC-VRF", "mac_vrf"),
    Column("learned", "learned"),
    Column("ES", lambda e: e.es or e.esi),
    Column("LLDP", lambda e: list(e.lldp)),
    Column("why", lambda e: list(e.why)),
)


@lens(
    "endpoints",
    "Every host attached to the fabric, where it is attached",
    requires=("arp", "nd", "mac", "es", "es-dest", "services", "lldp", "lag", "ipv4-rib", "ipv6-rib"),
    record=Endpoint,
    columns=_COLUMNS,
)
def endpoints(fabric: Fabric) -> Iterator[Endpoint]:
    index = Index(fabric)
    seen: set[tuple[str, str, str]] = set()

    def placed(node: str, bridge: str, address: str) -> dict[str, object] | None:
        """Where a MAC sits on *node*, if it is attached there; ``None`` if it is elsewhere."""
        entry = index.macs.get((node, bridge, address))
        if entry is None:
            return {}
        place = index.place(entry)
        if place.local:
            if index.fabric_facing(node, parent(place.via)):
                return None
            return {
                "subinterface": place.via,
                # A segment peer's MAC, synced over EVPN onto this node's port of the segment.
                "learned": "remote" if entry.type.startswith("evpn") else "local",
                "esi": place.esi,
                "es": place.segment,
                "lldp": index.heard_on(node, parent(place.via)),
            }
        # Learned from a peer: attached here only if this node is on the segment too.
        if place.esi:
            ports = [p for (n, p), es in index.segments.items() if n == node and es.esi == place.esi]
            service = index.services.get((node, bridge))
            mine = [p.name for p in (service.ports if service else ()) if parent(p.name) in ports]
            if mine:
                return {
                    "subinterface": mine[0],
                    "learned": "remote",
                    "esi": place.esi,
                    "es": place.segment,
                    "lldp": index.heard_on(node, parent(mine[0])),
                }
        return None

    def role(node: str, ip_vrf: str, address: str) -> dict[str, object]:
        """A router if *ip_vrf* on *node* peers with *address* over BGP or routes over it."""
        at = ip(address)
        service = index.services.get((node, ip_vrf))
        why: list[str] = []
        if at is not None and service and any(ip(p) == at for p in service.bgp_peers):
            why.append("bgp peer")
        prefixes = index.routed_via.get((node, ip_vrf, at), []) if at is not None else []
        if prefixes:
            named = ", ".join(prefixes[:_SHOWN])
            more = len(prefixes) - _SHOWN
            why.append(f"next-hop of {named}" + (f" (+{more} more)" if more > 0 else ""))
        return {"role": "router" if why else "host", "why": tuple(why)}

    for b in index.bindings:
        if b.origin == "evpn" or out_of_band(b.interface) or b.interface.startswith(("system", "lo")):
            continue
        address = mac(b.mac)
        vrfs = index.ni_of(b.node, b.interface)
        ip_vrf = next((n for n in vrfs if index.services[(b.node, n)].type != "mac-vrf"), "")
        bridge = index.bridge_of(b.node, b.interface)
        where: dict[str, object] | None = {}
        if bridge:
            where = placed(b.node, bridge, address)
        elif index.fabric_facing(b.node, parent(b.interface)):
            where = None  # a routed link to another node of the fabric
        else:
            where = {
                "subinterface": b.interface,
                "learned": "local",
                "lldp": index.heard_on(b.node, parent(b.interface)),
            }
        if where is None:
            continue
        seen.add((b.node, bridge, address))
        yield Endpoint(
            node=b.node,
            ip=b.address,
            mac=address,
            origin=b.origin,
            state=b.state,
            l3_interface=b.interface,
            ip_vrf=ip_vrf,
            mac_vrf=bridge,
            **where,  # type: ignore[arg-type]
            **role(b.node, ip_vrf, b.address),  # type: ignore[arg-type]
        )
    # Hosts that only bridge: learned locally, bound by no ARP or ND entry here.
    for (node, bridge, address), entry in sorted(index.macs.items()):
        if (node, bridge, address) in seen or not entry.local or entry.interface in ("irb-interface", ""):
            continue
        where = placed(node, bridge, address)
        if where:
            yield Endpoint(node=node, mac=address, origin=entry.type, mac_vrf=bridge, **where)  # type: ignore[arg-type]

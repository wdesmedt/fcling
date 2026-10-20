"""What the lenses look things up in: the fabric's records, indexed once.

One reading of each fact, shared: where a MAC sits - the port and segment it
was learned on, or the VTEP or segment it is behind - is decided by
:func:`place` alone, so ``where`` and ``endpoints`` cannot place a host
differently.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from ..checks import system_addresses
from ..fabric import Fabric, out_of_band, parent
from ..reports.layer2 import EthernetSegment, MacEntry, NeighborEntry
from ..reports.routing import Route
from ..reports.services import Service


def mac(value: Any) -> str:
    """A MAC as the bridge tables write it, or ``""`` if *value* is not one."""
    text = str(value or "").strip().upper().replace("-", ":")
    parts = text.split(":")
    if len(parts) == 6 and all(len(p) == 2 and all(c in "0123456789ABCDEF" for c in p) for p in parts):
        return text
    return ""


def ip(value: Any) -> Any:
    try:
        return ipaddress.ip_address(str(value).split("/", 1)[0].split("%", 1)[0].strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class Placement:
    """Where a bridge-table entry puts a MAC, on the node that has the entry."""

    #: Learned on a port of this node, rather than over the overlay.
    local: bool
    #: Local: the subinterface. Remote: the VTEP (or MPLS far end) it is behind;
    #: empty when it is behind a segment alone.
    via: str
    #: The segment: the one its local port is in, or the one it is behind.
    esi: str = ""
    #: What this node calls that segment, where it has it.
    segment: str = ""


class Index:
    def __init__(self, fabric: Fabric) -> None:
        self.fabric = fabric

    # ------------------------------------------------------------ bridging

    @cached_property
    def macs(self) -> dict[tuple[str, str, str], MacEntry]:
        """``(node, mac-vrf, MAC)`` -> its bridge-table entry."""
        return {(e.node, e.ni, mac(e.address)): e for e in self.fabric.get("mac")}

    @cached_property
    def segments(self) -> dict[tuple[str, str], EthernetSegment]:
        """``(node, port)`` -> the ethernet-segment the port is in."""
        return {
            (es.node, port): es for es in self.fabric.get("es") if es.esi for port in es.interfaces if port
        }

    @cached_property
    def segment_names(self) -> dict[str, tuple[str, ...]]:
        names: dict[str, list[str]] = {}
        for es in self.fabric.get("es"):
            if es.esi and es.name and es.name not in names.setdefault(es.esi, []):
                names[es.esi].append(es.name)
        return {esi: tuple(n) for esi, n in names.items()}

    @cached_property
    def es_vteps(self) -> dict[tuple[str, str, str], tuple[str, ...]]:
        """``(node, vxlan-interface, ESI)`` -> the VTEPs the segment's routes came from."""
        return {(d.node, d.overlay, d.esi): d.vteps for d in self.fabric.get("es-dest")}

    def place(self, entry: MacEntry) -> Placement:
        if entry.local:
            seg = self.segments.get((entry.node, parent(entry.interface)))
            return Placement(True, entry.interface, seg.esi if seg else "", seg.name if seg else "")
        names = self.segment_names.get(entry.esi, ())
        return Placement(False, entry.vtep or entry.far_end, entry.esi, names[0] if names else "")

    # --------------------------------------------------------- neighbours

    @cached_property
    def bindings(self) -> list[NeighborEntry]:
        """Every ARP and ND entry of the fabric."""
        return [*self.fabric.get("arp"), *self.fabric.get("nd")]

    # ------------------------------------------------------------- services

    @cached_property
    def services(self) -> dict[tuple[str, str], Service]:
        return {(s.node, s.name): s for s in self.fabric.get("services")}

    def bridges_behind(self, node: str, ni: str) -> list[str]:
        """The mac-vrfs an instance reaches over its irbs - or itself, if it is one."""
        service = self.services.get((node, ni))
        if service is None:
            return []
        if service.type == "mac-vrf":
            return [ni]
        return [p.peer_ni for p in service.ports if p.kind == "irb" and p.peer_ni]

    def bridge_of(self, node: str, interface: str) -> str:
        """The bridge table behind an L3 interface: an irb's mac-vrf; ``""`` for a routed port."""
        for ni in self.ni_of(node, interface):
            if self.services[(node, ni)].type == "mac-vrf":
                return ni
        return ""

    def ni_of(self, node: str, subinterface: str) -> list[str]:
        return [
            name
            for (n, name), s in self.services.items()
            if n == node and any(p.name == subinterface for p in s.ports)
        ]

    # --------------------------------------------------------------- cabling

    @cached_property
    def cables(self) -> dict[tuple[str, str], tuple[str, str]]:
        """``(node, port)`` -> ``(peer node, its port)``, for neighbours that are fabric nodes."""
        found: dict[tuple[str, str], tuple[str, str]] = {}
        for n in self.fabric.get("lldp"):
            peer = self.fabric.resolve(n.system_name) if n.system_name else None
            if peer and peer != n.node and n.port_id and not out_of_band(n.interface):
                found[(n.node, n.interface)] = (peer, n.port_id)
        return found

    @cached_property
    def heard(self) -> dict[tuple[str, str], tuple[str, ...]]:
        """``(node, port)`` -> every system-name LLDP hears there, fabric node or not."""
        names: dict[tuple[str, str], list[str]] = {}
        for n in self.fabric.get("lldp"):
            if n.system_name:
                names.setdefault((n.node, n.interface), []).append(n.system_name)
        return {k: tuple(v) for k, v in names.items()}

    @cached_property
    def lag_members(self) -> dict[tuple[str, str], tuple[str, ...]]:
        members: dict[tuple[str, str], list[str]] = {}
        for lag in self.fabric.get("lag"):
            if lag.member:
                members.setdefault((lag.node, lag.name), []).append(lag.member)
        return {k: tuple(v) for k, v in members.items()}

    def heard_on(self, node: str, port: str) -> tuple[str, ...]:
        """The names LLDP hears on a port - on a LAG, on its members."""
        ports = self.lag_members.get((node, port)) or (port,)
        return tuple(dict.fromkeys(name for p in ports for name in self.heard.get((node, p), ())))

    def fabric_facing(self, node: str, port: str) -> bool:
        """Whether a port is cabled to another node of the fabric: not where hosts are."""
        ports = self.lag_members.get((node, port)) or (port,)
        return any((node, p) in self.cables for p in ports)

    # ------------------------------------------------------------ addressing

    @cached_property
    def loopbacks(self) -> dict[str, set[str]]:
        return system_addresses(self.fabric)

    @cached_property
    def loopback_owner(self) -> dict[str, str]:
        return {address: node for node, addresses in self.loopbacks.items() for address in addresses}

    @cached_property
    def routes(self) -> dict[tuple[str, str, int], list[Route]]:
        """``(node, network-instance, IP version)`` -> its routes."""
        out: dict[tuple[str, str, int], list[Route]] = {}
        for report, version in (("ipv4-rib", 4), ("ipv6-rib", 6)):
            for r in self.fabric.get(report):
                out.setdefault((r.node, r.ni, version), []).append(r)
        return out

    @cached_property
    def routed_via(self) -> dict[tuple[str, str, Any], list[str]]:
        """``(node, network-instance, next-hop IP)`` -> the active prefixes routed over it.

        A route to the next-hop's own address is left out, such as the host
        route ARP/ND populates for a neighbour: it routes nothing beyond it.
        """
        out: dict[tuple[str, str, Any], list[str]] = {}
        for (node, ni, _), routes in self.routes.items():
            for route in routes:
                if not route.active or route.type in ("local", "host"):
                    continue
                try:
                    net = ipaddress.ip_network(route.prefix, strict=False)
                except ValueError:
                    continue
                for hop in route.next_hops:
                    address = ip(hop.address)
                    if address is not None and not (net.num_addresses == 1 and address in net):
                        prefixes = out.setdefault((node, ni, address), [])
                        if route.prefix not in prefixes:
                            prefixes.append(route.prefix)
        return out

    def lookup(self, node: str, ni: str, address: Any) -> Route | None:
        """The longest active prefix of *ni* on *node* that covers *address*."""
        best, length = None, -1
        for route in self.routes.get((node, ni, address.version), ()):
            if not route.active:
                continue
            try:
                net = ipaddress.ip_network(route.prefix, strict=False)
            except ValueError:
                continue
            if address in net and net.prefixlen > length:
                best, length = route, net.prefixlen
        return best

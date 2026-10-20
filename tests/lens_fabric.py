"""A small EVPN fabric built from records, for the lenses and the topology.

Three leaves under one spine. Host A (10.1.1.10, ``AA:..:01``) is multihomed
all-active on leaf1 and leaf2 over ES-A; host B (10.1.1.20, ``BB:..:02``) sits
on leaf3. Both are in macvrf-1, routed by ipvrf-1 over irb0.1; leaf1 reaches
host B over VXLAN to leaf3's loopback. ``CC:..:03`` is learned locally on
leaf1 and leaf3 on no common segment: a duplicate.
"""

from __future__ import annotations

from typing import Any

from fcli.fabric import Fabric
from fcli.inventory import Node
from fcli.reports.instances import NiInterface
from fcli.reports.interfaces import Lag, Subinterface
from fcli.reports.layer2 import EsDestination, EthernetSegment, MacEntry, NeighborEntry
from fcli.reports.lldp import LldpNeighbor
from fcli.reports.routing import Egress, Route, RouteNextHop
from fcli.reports.services import Port, Service

LEAVES = ("leaf1", "leaf2", "leaf3")
NODES = [Node(n, n) for n in (*LEAVES, "spine1")]
LOOPBACK = {"leaf1": "10.0.0.1", "leaf2": "10.0.0.2", "leaf3": "10.0.0.3", "spine1": "10.0.0.10"}
UPLINK = {"leaf1": "ethernet-1/1", "leaf2": "ethernet-1/2", "leaf3": "ethernet-1/3"}

HOST_A, HOST_B, DUP = "AA:00:00:00:00:01", "BB:00:00:00:00:02", "CC:00:00:00:00:03"
ESI = "01:00:00:00:00:00:00:00:00:01"


def _via(node: str, prefix: str, nh: str, port: str, type_: str = "bgp") -> Route:
    egress = Egress(kind="interface", value=f"{port}.0")
    return Route(
        node=node,
        ni="default",
        prefix=prefix,
        type=type_,
        active=True,
        next_hops=(RouteNextHop(address=nh, egress=(egress,)),),
    )


def records() -> dict[str, list[Any]]:
    lldp, subif, ni, routes = [], [], [], []
    for leaf, spine_port in UPLINK.items():
        lldp += [
            LldpNeighbor(node=leaf, interface="ethernet-1/49", system_name="spine1", port_id=spine_port),
            LldpNeighbor(node="spine1", interface=spine_port, system_name=leaf, port_id="ethernet-1/49"),
        ]
        subif += [
            Subinterface(node=leaf, interface="ethernet-1/49", name="ethernet-1/49.0", oper="up"),
            Subinterface(node="spine1", interface=spine_port, name=f"{spine_port}.0", oper="up"),
        ]
        # The spine reaches each leaf's loopback down its cable; each leaf, the others' up its own.
        routes.append(_via("spine1", f"{LOOPBACK[leaf]}/32", "192.168.0.1", spine_port))
        for other in LEAVES:
            if other != leaf:
                routes.append(_via(leaf, f"{LOOPBACK[other]}/32", "192.168.0.0", "ethernet-1/49"))
    for node, address in LOOPBACK.items():
        ni.append(NiInterface(node=node, ni="default", interface="system0.0", prefixes=(f"{address}/32",)))
        routes.append(Route(node=node, ni="default", prefix=f"{address}/32", type="host", active=True))

    lldp.append(LldpNeighbor(node="leaf1", interface="ethernet-1/10", system_name="hostA", port_id="eth1"))
    lldp.append(LldpNeighbor(node="leaf2", interface="ethernet-1/10", system_name="hostA", port_id="eth2"))
    lag = [Lag(node=n, name="lag1", oper="up", member="ethernet-1/10") for n in ("leaf1", "leaf2")]
    es = [
        EthernetSegment(node=n, name="ES-A", esi=ESI, mh_mode="all-active", oper="up", interfaces=("lag1",))
        for n in ("leaf1", "leaf2")
    ]
    access = {"leaf1": "lag1.1", "leaf2": "lag1.1", "leaf3": "ethernet-1/1.1"}
    subif += [Subinterface(node=n, interface="lag1", name="lag1.1", oper="up") for n in ("leaf1", "leaf2")]
    subif += [
        Subinterface(node="leaf3", interface="ethernet-1/1", name="ethernet-1/1.1", oper="up"),
        *(
            Subinterface(node=n, interface="irb0", name="irb0.1", oper="up", ipv4=("10.1.1.1/24",))
            for n in LEAVES
        ),
    ]

    services = []
    for node in LEAVES:
        on_es = access[node].startswith("lag")
        services += [
            Service(
                node=node,
                name="macvrf-1",
                type="mac-vrf",
                oper="up",
                evis=("1",),
                vnis=(100,),
                import_rts=("target:1:1",),
                export_rts=("target:1:1",),
                ports=(
                    Port(
                        name=access[node],
                        kind="bridged",
                        oper="up",
                        esi=ESI if on_es else "",
                        segment="ES-A" if on_es else "",
                    ),
                    Port(name="irb0.1", kind="irb", oper="up", peer_ni="ipvrf-1"),
                ),
                linked=("ipvrf-1",),
            ),
            Service(
                node=node,
                name="ipvrf-1",
                type="ip-vrf",
                oper="up",
                vnis=(200,),
                import_rts=("target:2:2",),
                export_rts=("target:2:2",),
                subnets=("10.1.1.0/24",),
                ports=(
                    Port(
                        name="irb0.1", kind="irb", oper="up", addresses=("10.1.1.1/24",), peer_ni="macvrf-1"
                    ),
                ),
                linked=("macvrf-1",),
            ),
            Service(node=node, name="default", type="default", oper="up"),
        ]
    services.append(Service(node="spine1", name="default", type="default", oper="up"))

    def remote(node: str, address: str, **where: Any) -> MacEntry:
        return MacEntry(
            node=node, ni="macvrf-1", address=address, type="evpn", overlay="vxlan1.1", vni=100, **where
        )

    mac = [
        MacEntry(node="leaf1", ni="macvrf-1", address=HOST_A, type="learnt", interface="lag1.1"),
        MacEntry(node="leaf2", ni="macvrf-1", address=HOST_A, type="learnt", interface="lag1.1"),
        MacEntry(node="leaf3", ni="macvrf-1", address=HOST_B, type="learnt", interface="ethernet-1/1.1"),
        remote("leaf1", HOST_B, vtep="10.0.0.3"),
        remote("leaf2", HOST_B, vtep="10.0.0.3"),
        remote("leaf3", HOST_A, esi=ESI),
        MacEntry(node="leaf1", ni="macvrf-1", address=DUP, type="learnt", interface="ethernet-1/5.1"),
        MacEntry(node="leaf3", ni="macvrf-1", address=DUP, type="learnt", interface="ethernet-1/1.1"),
    ]
    es_dest = [
        EsDestination(
            node="leaf3", tunnel="vxlan1", overlay="vxlan1.1", esi=ESI, vteps=("10.0.0.1", "10.0.0.2")
        )
    ]
    arp = [
        NeighborEntry(
            node="leaf1",
            interface="irb0.1",
            nis=("ipvrf-1", "macvrf-1"),
            address="10.1.1.10",
            mac=HOST_A,
            origin="dynamic",
        ),
        NeighborEntry(
            node="leaf3",
            interface="irb0.1",
            nis=("ipvrf-1", "macvrf-1"),
            address="10.1.1.20",
            mac=HOST_B,
            origin="dynamic",
        ),
        NeighborEntry(
            node="leaf1",
            interface="irb0.1",
            nis=("ipvrf-1", "macvrf-1"),
            address="10.1.1.20",
            mac=HOST_B,
            origin="evpn",
        ),
        # Host A's ARP hashed to leaf1; leaf2, on its segment too, has it synced.
        NeighborEntry(
            node="leaf2",
            interface="irb0.1",
            nis=("ipvrf-1", "macvrf-1"),
            address="10.1.1.10",
            mac=HOST_A,
            origin="evpn",
        ),
    ]
    for node in LEAVES:
        irb = Egress(kind="interface", value="irb0.1")
        routes.append(
            Route(
                node=node,
                ni="ipvrf-1",
                prefix="10.1.1.0/24",
                type="local",
                active=True,
                next_hops=(RouteNextHop(address="10.1.1.1", egress=(irb,)),),
            )
        )
    tunnel = Egress(kind="tunnel", tunnel="vxlan", value="10.0.0.3", vni=200)
    routes.append(
        Route(
            node="leaf1",
            ni="ipvrf-1",
            prefix="10.1.1.20/32",
            type="bgp-evpn",
            active=True,
            next_hops=(RouteNextHop(address="10.0.0.3", egress=(tunnel,)),),
        )
    )
    return {
        "lldp": lldp,
        "subif": subif,
        "ni": ni,
        "ipv4-rib": routes,
        "ipv6-rib": [],
        "lag": lag,
        "es": es,
        "es-dest": es_dest,
        "services": services,
        "mac": mac,
        "arp": arp,
        "nd": [],
    }


def fabric(**changes: Any) -> Fabric:
    return Fabric.of(NODES, records(), **changes)

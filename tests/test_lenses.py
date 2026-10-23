"""The lenses and the topology, over a small EVPN fabric built from records."""

from __future__ import annotations

import dataclasses
import json

import pytest

from fcli.fabric import Fabric
from fcli.inventory import Node
from fcli.lens import LensError
from fcli.lenses import LENSES
from fcli.reports.instances import BgpVpnInstance, NiInterface, stitched
from fcli.reports.layer2 import NeighborEntry, VxlanInterface
from fcli.reports.routing import Route
from fcli.reports.services import Port, Service
from fcli.topology import build

from . import lens_fabric
from .lens_fabric import DUP, ESI, HOST_A, HOST_B, fabric


def _run(lens: str, /, **args: str) -> list:
    return LENSES[lens].run(fabric(), **args)


def test_arguments_are_checked() -> None:
    with pytest.raises(LensError, match="needs address"):
        _run("where")
    with pytest.raises(LensError, match="no argument colour"):
        _run("where", address="10.1.1.10", colour="red")
    with pytest.raises(LensError, match="neither an IP nor a MAC"):
        _run("where", address="leaf9")


def test_a_mac_on_one_segment_is_multihomed_not_duplicate() -> None:
    found = {(s.node, s.kind): s for s in _run("where", address=HOST_A.lower())}
    assert set(found) == {
        ("leaf1", "multihomed"),
        ("leaf2", "multihomed"),
        ("leaf3", "remote"),
        ("leaf1", "arp"),
        ("leaf2", "arp"),
    }
    assert found[("leaf1", "multihomed")].also_on == ("leaf2",)
    assert found[("leaf1", "multihomed")].segment == "ES-A"
    # leaf3 has it behind the segment, not behind one VTEP.
    assert (found[("leaf3", "remote")].esi, found[("leaf3", "remote")].vtep) == (ESI, "")


def test_a_mac_on_two_ports_of_no_segment_is_a_duplicate() -> None:
    found = [s for s in _run("where", address=DUP) if s.kind != "remote"]
    assert sorted((s.node, s.kind, s.also_on) for s in found) == [
        ("leaf1", "duplicate", ("leaf3",)),
        ("leaf3", "duplicate", ("leaf1",)),
    ]


def test_an_ip_is_followed_to_the_port_its_host_is_on() -> None:
    found = {(s.node, s.kind): s for s in _run("where", address="10.1.1.20")}
    arp = found[("leaf3", "arp")]
    assert (arp.mac, arp.bridge_ni, arp.learned_on) == (HOST_B, "macvrf-1", "ethernet-1/1.1")
    # leaf1 has it from EVPN, behind leaf3's VTEP; and a host route to it.
    assert found[("leaf1", "arp")].vtep == "10.0.0.3"
    assert found[("leaf1", "route")].prefix == "10.1.1.20/32"
    # The route's egress: the tunnel it leaves over and its VTEP, named by its node.
    assert found[("leaf1", "route")].interface == "vxlan VNI 200 to 10.0.0.3 (leaf3)"
    configured = [s for s in _run("where", address="10.1.1.1") if s.kind == "configured"]
    assert sorted(s.node for s in configured) == ["leaf1", "leaf2", "leaf3"]
    (none,) = _run("where", address="10.9.9.9")
    assert none.kind == "not-found"


def test_endpoints_are_listed_where_they_are_attached() -> None:
    found = sorted((e.node, e.ip, e.mac, e.subinterface, e.learned, e.es) for e in _run("endpoints"))
    assert found == [
        ("leaf1", "", DUP, "ethernet-1/5.1", "local", ""),
        ("leaf1", "10.1.1.10", HOST_A, "lag1.1", "local", "ES-A"),
        # Its ARP entry synced from leaf1, on the segment leaf2 is on too.
        ("leaf2", "10.1.1.10", HOST_A, "lag1.1", "remote", "ES-A"),
        ("leaf3", "", DUP, "ethernet-1/1.1", "local", ""),
        ("leaf3", "10.1.1.20", HOST_B, "ethernet-1/1.1", "local", ""),
    ]
    # Host B is not listed on leaf1, which only learned it from leaf3, over a VTEP.
    (a,) = [e for e in _run("endpoints") if (e.node, e.ip) == ("leaf1", "10.1.1.10")]
    assert (a.ip_vrf, a.mac_vrf, a.lldp) == ("ipvrf-1", "macvrf-1", ("hostA",))


def _with_ce(peer: bool, prefixes: int) -> Fabric:
    """A CE at 10.1.1.50 behind leaf3's access port: ipvrf-1 peers with it over
    BGP if *peer*, and routes that many prefixes over it."""
    from fcli.reports.layer2 import MacEntry, NeighborEntry
    from fcli.reports.routing import RouteNextHop

    ce = "DD:00:00:00:00:04"
    records = lens_fabric.records()
    records["mac"].append(
        MacEntry(node="leaf3", ni="macvrf-1", address=ce, type="learnt", interface="ethernet-1/1.1")
    )
    records["arp"].append(
        NeighborEntry(
            node="leaf3",
            interface="irb0.1",
            nis=("ipvrf-1", "macvrf-1"),
            address="10.1.1.50",
            mac=ce,
            origin="dynamic",
        )
    )
    records["ipv4-rib"] += [
        Route(
            node="leaf3",
            ni="ipvrf-1",
            prefix=f"192.168.{n}.0/24",
            type="bgp" if peer else "static",
            active=True,
            next_hops=(RouteNextHop(address="10.1.1.50"),),
        )
        for n in range(prefixes)
    ]
    # The host route ARP populates for it: a route to the CE itself, over nothing.
    records["ipv4-rib"].append(
        Route(
            node="leaf3",
            ni="ipvrf-1",
            prefix="10.1.1.50/32",
            type="arp-nd",
            active=True,
            next_hops=(RouteNextHop(address="10.1.1.50"),),
        )
    )
    records["services"] = [
        s.model_copy(update={"bgp_peers": ("10.1.1.50",)})
        if peer and (s.node, s.name) == ("leaf3", "ipvrf-1")
        else s
        for s in records["services"]
    ]
    return dataclasses.replace(fabric(), records=records)


def _roles(fabric: Fabric) -> dict[str, tuple]:
    return {
        e.ip or e.mac: (e.role, e.bgp, e.routes) for e in LENSES["endpoints"].run(fabric) if e.node == "leaf3"
    }


def test_an_endpoint_the_instance_peers_with_or_routes_over_is_a_router() -> None:
    assert _roles(_with_ce(peer=True, prefixes=1)) == {
        "10.1.1.20": ("host", "", ()),
        "10.1.1.50": ("router", "peer", ("192.168.0.0/24",)),
        # Only bridged: nothing says what it is.
        DUP: ("", "", ()),
    }
    # A static route over it is enough.
    assert _roles(_with_ce(peer=False, prefixes=1))["10.1.1.50"] == ("router", "", ("192.168.0.0/24",))
    # A peer with no routes over it is still a router.
    assert _roles(_with_ce(peer=True, prefixes=0))["10.1.1.50"] == ("router", "peer", ())


def test_the_routes_column_names_the_first_prefixes_and_counts_the_rest() -> None:
    from fcli.render import cells

    spec = LENSES["endpoints"]
    (ce,) = [e for e in spec.run(_with_ce(peer=False, prefixes=5)) if e.ip == "10.1.1.50"]
    assert len(ce.routes) == 5
    assert cells(spec, ce)["routes"] == "192.168.0.0/24, 192.168.1.0/24, 192.168.2.0/24 (+2 more)"


def _hops(source: str, destination: str, ni: str | None = None) -> list[tuple]:
    return [
        (h.hop, h.lookup, h.node, h.ni, h.outcome, h.peer or h.endpoint or h.resumes_in or h.egress)
        for h in LENSES["path"].run(fabric(), source=source, destination=destination, ni=ni)
    ]


def test_a_routed_path_over_vxlan() -> None:
    assert _hops("leaf1", "10.1.1.20", "ipvrf-1") == [
        # Numbered by node: two lookups on leaf1, one on spine1, two on leaf3.
        (0, 1, "leaf1", "ipvrf-1", "tunnel", "10.0.0.3"),
        (0, 2, "leaf1", "default", "forwarded", "spine1"),
        (1, 1, "spine1", "default", "forwarded", "leaf3"),
        (2, 1, "leaf3", "default", "endpoint-reached", "ipvrf-1"),
        (2, 2, "leaf3", "ipvrf-1", "delivered", "ethernet-1/1.1"),
    ]


def test_a_host_source_enters_on_every_leaf_of_its_segment() -> None:
    hops = _hops("10.1.1.10", "10.1.1.20")
    assert [h for h in hops if h[4] == "ingress"] == [
        (0, 1, "leaf1", "macvrf-1", "ingress", "lag1.1"),
        (0, 1, "leaf2", "macvrf-1", "ingress", "lag1.1"),
    ]
    # leaf2 has no route to host B: where its branch ends, and why.
    assert (0, 2, "leaf2", "ipvrf-1", "delivered", "irb0.1") not in hops
    assert hops[-1][4] == "delivered"


def test_a_bridged_path_to_a_segment_is_aliased_over_its_vteps() -> None:
    hops = _hops("leaf3", HOST_A, "macvrf-1")
    assert [h for h in hops if h[4] == "tunnel"] == [
        (0, 1, "leaf3", "macvrf-1", "tunnel", "10.0.0.1"),
        (0, 1, "leaf3", "macvrf-1", "tunnel", "10.0.0.2"),
    ]
    delivered = [
        h
        for h in LENSES["path"].run(fabric(), source="leaf3", destination=HOST_A, ni="macvrf-1")
        if h.outcome == "delivered"
    ]
    assert sorted((h.node, h.egress, h.segment) for h in delivered) == [
        ("leaf1", "lag1.1", "ES-A"),
        ("leaf2", "lag1.1", "ES-A"),
    ]


def test_a_routed_path_to_a_host_bridged_elsewhere_goes_on_in_its_mac_vrf() -> None:
    # leaf3 has no /32 for host A, only its subnet; ARP/ND has it from EVPN, the bridge
    # table behind the irb behind ES-A: routed into the irb, then bridged on to leaf1 and leaf2.
    records = lens_fabric.records()
    records["arp"].append(
        NeighborEntry(
            node="leaf3",
            interface="irb0.1",
            nis=("ipvrf-1", "macvrf-1"),
            address="10.1.1.10",
            mac=HOST_A,
            origin="evpn",
        )
    )
    run = LENSES["path"].run(
        Fabric.of(lens_fabric.NODES, records), source="leaf3", destination="10.1.1.10", ni="ipvrf-1"
    )
    hops = [(h.hop, h.lookup, h.node, h.ni, h.outcome, h.endpoint or h.resumes_in or h.egress) for h in run]
    assert hops[:3] == [
        (0, 1, "leaf3", "ipvrf-1", "bridged", "macvrf-1"),
        (0, 2, "leaf3", "macvrf-1", "tunnel", "10.0.0.1"),
        (0, 2, "leaf3", "macvrf-1", "tunnel", "10.0.0.2"),
    ]
    assert [h for h in hops if h[4] in ("endpoint-reached", "delivered")] == [
        (2, 1, "leaf1", "default", "endpoint-reached", "macvrf-1"),
        (2, 2, "leaf1", "macvrf-1", "delivered", "lag1.1"),
        (2, 1, "leaf2", "default", "endpoint-reached", "macvrf-1"),
        (2, 2, "leaf2", "macvrf-1", "delivered", "lag1.1"),
    ]
    (bridged, *_) = run
    assert (bridged.mac, bridged.segment, bridged.egress) == (HOST_A, "ES-A", "irb0.1")


def test_branches_chasing_one_vtep_from_two_leaves_are_one_chase() -> None:
    # Host A enters on leaf1 and leaf2; neither has a host route to host B, both bridge it to leaf3.
    records = lens_fabric.records()
    records["ipv4-rib"] = [r for r in records["ipv4-rib"] if r.prefix != "10.1.1.20/32"]
    records["arp"].append(
        NeighborEntry(
            node="leaf2",
            interface="irb0.1",
            nis=("ipvrf-1", "macvrf-1"),
            address="10.1.1.20",
            mac=HOST_B,
            origin="evpn",
        )
    )
    run = LENSES["path"].run(
        Fabric.of(lens_fabric.NODES, records), source="10.1.1.10", destination="10.1.1.20"
    )
    hops = [(h.hop, h.lookup, h.node, h.ni, h.outcome, h.peer or h.resumes_in or h.egress) for h in run]
    assert [h for h in hops if h[2] == "leaf3" or h[1:3] == (1, "spine1")] == [
        (1, 1, "spine1", "default", "forwarded", "leaf3"),
        (2, 1, "leaf3", "default", "endpoint-reached", "macvrf-1"),
        (2, 2, "leaf3", "macvrf-1", "delivered", "ethernet-1/1.1"),
    ]


def test_path_says_where_it_goes_nowhere() -> None:
    assert _hops("leaf2", "10.7.7.7") == [(0, 1, "leaf2", "default", "no-route", "")]
    with pytest.raises(LensError, match="no node has 10.9.9.9 attached"):
        _hops("10.9.9.9", "10.1.1.20")


def test_service_shows_every_node_and_what_disagrees() -> None:
    rows = _run("service", name="macvrf-1")
    assert [(r.node, r.macs_local, r.macs_remote, r.issues) for r in rows] == [
        ("leaf1", 2, 1, ()),
        ("leaf2", 1, 1, ()),
        ("leaf3", 2, 1, ()),
    ]
    with pytest.raises(LensError, match="the services are: ipvrf-1, macvrf-1"):
        _run("service", name="macvrf-9")

    records = lens_fabric.records()
    records["ni"] += [
        NiInterface(node=n, ni="macvrf-1", type="mac-vrf", overlays=("vxlan1.1",), interface="irb0.1")
        for n in lens_fabric.LEAVES
    ]
    records["vxlan"] = [
        VxlanInterface(node=n, name="vxlan1.1", ni="macvrf-1", vni=101 if n == "leaf3" else 100)
        for n in lens_fabric.LEAVES
    ]
    split = dataclasses.replace(fabric(), records=records)
    issues = {r.node: r.issues for r in LENSES["service"].run(split, name="macvrf-1")}
    assert "VNI 101" in issues["leaf3"][0]


def _two_sites() -> Fabric:
    """Two sites that reuse names and route-targets, each with a leaf and a gateway.

    mv-dci is stitched across them on the gateways' WAN side, 65000:1; mv-local
    is in both with the same target, but no gateway carries it: two services.
    """
    loopback = {"leafA": "10.0.1.1", "gwA": "10.0.1.9", "leafB": "10.0.2.1", "gwB": "10.0.2.9"}
    site = {"leafA": "A", "gwA": "A", "leafB": "B", "gwB": "B"}
    dc, wan = BgpVpnInstance(id=1, import_rts=("1:1",)), BgpVpnInstance(id=2, import_rts=("65000:1",))
    carried = {
        "leafA": {"mv-dci": (dc,), "mv-local": (BgpVpnInstance(id=1, import_rts=("9:9",)),)},
        "leafB": {"mv-dci": (dc,), "mv-local": (BgpVpnInstance(id=1, import_rts=("9:9",)),)},
        "gwA": {"mv-dci": (dc, wan)},
        "gwB": {"mv-dci": (dc, wan)},
    }
    ni, routes, services = [], [], []
    for node, address in loopback.items():
        ni.append(NiInterface(node=node, ni="default", interface="system0.0", prefixes=(f"{address}/32",)))
        for other, theirs in loopback.items():
            # The gateways learn each other's loopbacks over the WAN; the leaves only their own site's.
            if site[other] == site[node] or (node.startswith("gw") and other.startswith("gw")):
                routes.append(Route(node=node, ni="default", prefix=f"{theirs}/32", type="bgp", active=True))
        for name, vpn in carried[node].items():
            ni.append(NiInterface(node=node, ni=name, type="mac-vrf", interface="vxlan1.1", vpn=vpn))
            services.append(
                Service(node=node, name=name, type="mac-vrf", oper="up", vpn=vpn, gateway=stitched(vpn))
            )
    records = {"ni": ni, "ipv4-rib": routes, "ipv6-rib": [], "services": services}
    return Fabric.of([Node(n, n) for n in loopback], records)


def test_sites_come_from_the_underlay_and_gateways_stitch_them() -> None:
    rows = LENSES["service"].run(_two_sites())
    found = {(r.node, r.name): (r.site, r.group, r.dci, r.wan) for r in rows}
    assert found == {
        ("gwA", "mv-dci"): ("site 1", "mv-dci|dci", "L2 DCI", (2,)),
        ("leafA", "mv-dci"): ("site 1", "mv-dci|dci", "L2 DCI", ()),
        ("gwB", "mv-dci"): ("site 2", "mv-dci|dci", "L2 DCI", (2,)),
        ("leafB", "mv-dci"): ("site 2", "mv-dci|dci", "L2 DCI", ()),
        ("leafA", "mv-local"): ("site 1", "mv-local|site 1", "", ()),
        ("leafB", "mv-local"): ("site 2", "mv-local|site 2", "", ()),
    }
    # One name, the services it is part of: both sites' mv-local.
    named = LENSES["service"].run(_two_sites(), name="mv-local")
    assert sorted(r.group for r in named) == ["mv-local|site 1", "mv-local|site 2"]


def test_an_instance_without_targets_makes_no_gateway() -> None:
    assert not stitched((BgpVpnInstance(id=1, import_rts=("1:1",)), BgpVpnInstance(id=2)))
    assert stitched((BgpVpnInstance(id=1, import_rts=("1:1",)), BgpVpnInstance(id=2, export_rts=("2:2",))))


def test_topology_tiers_cables_and_clients() -> None:
    graph = build(fabric(), name="lab")
    assert {n["name"]: n["tier"] for n in graph["nodes"]} == {
        "leaf1": "leaf",
        "leaf2": "leaf",
        "leaf3": "leaf",
        "spine1": "spine",
    }
    assert [(x["a"], x["b"], x["count"], x["state"]) for x in graph["links"]] == [
        ("leaf1", "spine1", 1, "up"),
        ("leaf2", "spine1", 1, "up"),
        ("leaf3", "spine1", 1, "up"),
    ]
    host_a = next(c for c in graph["clients"] if c["name"] == "hostA")
    assert (host_a["esi"], host_a["nodes"]) == (ESI, ["leaf1", "leaf2"])
    assert graph["components"] == [{"name": "lab", "nodes": ["leaf1", "leaf2", "leaf3", "spine1"]}]
    (segment,) = graph["segments"]
    assert segment["names"] == ["ES-A"] and len(segment["nodes"]) == 2
    json.dumps(graph)


def test_a_cable_the_history_saw_go_is_lost() -> None:
    records = lens_fabric.records()
    records["lldp"] = [n for n in records["lldp"] if "leaf3" not in (n.node, n.system_name)]
    gone = {
        "report": "lldp",
        "op": "remove",
        "node": "leaf3",
        "before": {"interface": "ethernet-1/49", "system_name": "spine1", "port_id": "ethernet-1/3"},
    }
    graph = build(dataclasses.replace(fabric(), records=records, changes=[gone]))
    assert [(x["a"], x["b"], x["state"]) for x in graph["links"]][-1] == ("leaf3", "spine1", "lost")


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("where", {"address": HOST_A}),
        ("where", {"address": "10.1.1.20"}),
        ("path", {"source": "leaf1", "destination": "10.1.1.20", "ni": "ipvrf-1"}),
        ("path", {"source": "leaf3", "destination": HOST_A, "ni": "macvrf-1"}),
        ("service", {"name": "macvrf-1"}),
        ("endpoints", {}),
    ],
)
def test_a_lens_reads_only_what_it_declares(name: str, args: dict) -> None:
    """The engine and the history give a lens its declared reports alone."""
    spec = LENSES[name]
    full = fabric()
    reads = {*spec.requires, *(r for r, _ in spec.queried(full, **args))}
    declared = dataclasses.replace(full, records={r: v for r, v in full.records.items() if r in reads})
    assert spec.run(declared, **args) == spec.run(full, **args)


def test_information_marks_nothing_unhealthy() -> None:
    from fcli.checks import Finding

    def health(severity: str) -> str:
        found = Finding(node="leaf1", check="x", severity=severity, subject="default/10.0.0.10", detail="")
        (leaf1,) = [n for n in build(fabric(), [found])["nodes"] if n["name"] == "leaf1"]
        return f"{leaf1['health']} {leaf1['findings']}"

    assert health("info") == "ok {'info': 1}"
    assert health("warning") == "warning {'warning': 1}"


def _with_mac(change: dict, node: str, address: str) -> object:
    """The fabric with one bridge-table entry changed."""
    records = lens_fabric.records()
    records["mac"] = [
        m.model_copy(update=change) if (m.node, m.address) == (node, address) else m for m in records["mac"]
    ]
    return dataclasses.replace(fabric(), records=records)


def test_an_evpn_mpls_entry_is_behind_a_far_end_not_a_vtep() -> None:
    gateway = _with_mac({"esi": "", "overlay": "", "far_end": "10.0.0.9", "label": 30001}, "leaf3", HOST_A)
    (remote,) = [s for s in LENSES["where"].run(gateway, address=HOST_A) if s.kind == "remote"]
    assert (remote.vtep, remote.far_end) == ("", "10.0.0.9")
    from fcli.render import cells

    assert cells(LENSES["where"], remote)["where"] == "behind 10.0.0.9 over MPLS"


def test_a_mac_synced_from_the_segment_peer_is_learned_remote() -> None:
    synced = _with_mac({"type": "evpn"}, "leaf2", HOST_A)
    learned = {e.node: e.learned for e in LENSES["endpoints"].run(synced) if e.mac == HOST_A}
    assert learned == {"leaf1": "local", "leaf2": "remote"}


def test_a_client_without_an_lldp_name_is_named_by_its_segment_or_port() -> None:
    records = lens_fabric.records()
    records["lldp"] = [n for n in records["lldp"] if n.system_name != "hostA"]
    labels = sorted(c["label"] for c in build(dataclasses.replace(fabric(), records=records))["clients"])
    assert labels == ["ES-A", "leaf3 ethernet-1/1"]
    assert sorted(c["label"] for c in build(fabric())["clients"]) == ["hostA", "leaf3 ethernet-1/1"]


def test_topology_carries_rates_and_services_for_the_overlays() -> None:
    from fcli.reports.interfaces import InterfaceStats

    records = lens_fabric.records()
    records["ifstats"] = [
        InterfaceStats(node="leaf1", name="ethernet-1/49", out_kbps=2500.0, in_kbps=100.0),
        InterfaceStats(node="spine1", name="ethernet-1/1", out_kbps=100.0, in_kbps=2500.0),
        InterfaceStats(node="leaf1", name="lag1", out_kbps=40.0, in_kbps=7.0),
    ]
    graph = build(dataclasses.replace(fabric(), records=records))
    (cable,) = next(x for x in graph["links"] if x["a"] == "leaf1")["cables"]
    assert (cable["a_out_bps"], cable["b_out_bps"]) == (2_500_000.0, 100_000.0)
    # No sample yet is no rate, not zero.
    (other,) = next(x for x in graph["links"] if x["a"] == "leaf2")["cables"]
    assert other["a_out_bps"] is None
    host_a = next(c for c in graph["clients"] if c["name"] == "hostA")
    on_leaf1 = next(a for a in host_a["attachments"] if a["node"] == "leaf1")
    assert (on_leaf1["out_bps"], on_leaf1["in_bps"]) == (40_000.0, 7_000.0)
    services = {n["name"]: n["services"] for n in graph["nodes"]}
    assert services["leaf1"] == ["ipvrf-1", "macvrf-1"] and services["spine1"] == []


def test_an_uplink_whose_neighbour_went_quiet_is_no_client() -> None:
    records = lens_fabric.records()
    records["lldp"] = [n for n in records["lldp"] if "leaf3" not in (n.node, n.system_name)]
    records["services"] = [
        s.model_copy(update={"ports": (*s.ports, Port(name="ethernet-1/49.0", kind="routed"))})
        if (s.node, s.name) == ("leaf3", "default")
        else s
        for s in records["services"]
    ]
    graph = build(dataclasses.replace(fabric(), records=records))
    assert not [c for c in graph["clients"] if "ethernet-1/49" in c["label"]]


def test_topology_nodes_carry_their_platform_and_system_addresses() -> None:
    from fcli.reports.instances import NiInterface
    from fcli.reports.system import SystemInfo

    records = lens_fabric.records()
    records["sys-info"] = [SystemInfo(node="leaf1", type="7220 IXR-D2L")]
    records["ni"] = [
        NiInterface(
            node=r.node,
            ni=r.ni,
            interface=r.interface,
            prefixes=(*r.prefixes, "fe80::1/64", "2001:db8::1/128") if r.node == "leaf1" else r.prefixes,
        )
        for r in records["ni"]
    ]
    nodes = {n["name"]: n for n in build(dataclasses.replace(fabric(), records=records))["nodes"]}
    assert (nodes["leaf1"]["type"], nodes["leaf1"]["ipv4"], nodes["leaf1"]["ipv6"]) == (
        "7220 IXR-D2L",
        "10.0.0.1",
        "2001:db8::1",
    )
    assert (nodes["leaf2"]["type"], nodes["leaf2"]["ipv4"], nodes["leaf2"]["ipv6"]) == ("", "10.0.0.2", "")


def test_topology_nodes_carry_the_default_instance_asn() -> None:
    from fcli.reports.instances import NiInterface

    records = lens_fabric.records()
    records["ni"] = [
        *records["ni"],
        NiInterface(node="leaf1", ni="default", asn=65001),
        NiInterface(node="leaf2", ni="ip-vrf1", asn=65099),
    ]
    nodes = {n["name"]: n for n in build(dataclasses.replace(fabric(), records=records))["nodes"]}
    assert nodes["leaf1"]["asn"] == 65001
    assert nodes["leaf2"]["asn"] is None


def test_a_hop_names_the_owner_and_next_hop_of_its_route() -> None:
    (first, *_) = LENSES["path"].run(fabric(), source="leaf1", destination="10.1.1.20", ni="ipvrf-1")
    columns = [c.title for c in LENSES["path"].columns]
    assert columns[5:8] == ["route", "owner", "next-hop"]
    assert (first.prefix, first.route_type, first.next_hop) == ("10.1.1.20/32", "bgp-evpn", "10.0.0.3")


def test_path_hops_are_ordered_by_hop_then_node() -> None:
    hops = LENSES["path"].run(fabric(), source="10.1.1.10", destination="10.1.1.20")
    order = [(h.hop, h.node, h.lookup) for h in hops]
    assert order == sorted(order)
    assert {(h.hop, h.lookup) for h in hops if h.node == "spine1"} <= {(1, 1)}


VESI = "00:01:ff:ff:ff:ff:ff:ff:ff:06"


def _aliased(rt5: bool = True) -> Fabric:
    """A CE at 10.1.1.50 behind a virtual segment on every leaf, leaf1 its one
    candidate. leaf1 and leaf2 route its 192.168.50.1/32 over the CE; leaf3 has
    its 192.168.50.0/24 - and a default route - aliased to leaf1, from an RT5 that
    carries the segment's ESI."""
    from fcli.reports.bgp_rib import BgpRoute
    from fcli.reports.layer2 import DfCandidate, EsAssociation, EsNextHop, EthernetSegment
    from fcli.reports.routing import Egress, RouteNextHop

    records = lens_fabric.records()
    candidates = (DfCandidate(address="10.0.0.1"),)
    for node in lens_fabric.LEAVES:
        records["es"].append(
            EthernetSegment(
                node=node,
                name="vES-1",
                esi=VESI,
                type="virtual",
                next_hops=(EsNextHop(address="10.1.1.50", evis=("2",)),),
                associations=(EsAssociation(ni="ipvrf-1", candidates=candidates),),
            )
        )
    for node in ("leaf1", "leaf2"):
        records["ipv4-rib"].append(
            Route(
                node=node,
                ni="ipvrf-1",
                prefix="192.168.50.1/32",
                type="bgp",
                active=True,
                next_hops=(RouteNextHop(address="10.1.1.50"),),
            )
        )
    tunnel = Egress(kind="tunnel", tunnel="vxlan", value="10.0.0.1", vni=200)
    for prefix in ("192.168.50.0/24", "0.0.0.0/0"):
        records["ipv4-rib"].append(
            Route(
                node="leaf3",
                ni="ipvrf-1",
                prefix=prefix,
                type="bgp-evpn",
                active=True,
                next_hops=(RouteNextHop(address="10.0.0.1", egress=(tunnel,)),),
            )
        )
    # Advertised by leaf2, which peers with the CE; the route-table aliases it to leaf1.
    records["bgp-rib-evpn-5"] = (
        [
            BgpRoute(
                node="leaf3",
                ni="default",
                family="evpn",
                route_type="5",
                prefix="192.168.50.0/24",
                esi=VESI,
                gateway="0.0.0.0",
                next_hop="10.0.0.2",
            )
        ]
        if rt5
        else []
    )
    return dataclasses.replace(fabric(), records=records)


def test_a_route_over_a_virtual_segment_names_it_and_whether_the_node_attaches() -> None:
    from fcli.render import cells

    found = {s.node: s for s in LENSES["where"].run(_aliased(), address="192.168.50.1")}
    assert (found["leaf1"].segment, found["leaf1"].esi, found["leaf1"].attached) == ("vES-1", VESI, True)
    assert found["leaf2"].attached is False
    where = cells(LENSES["where"], found["leaf1"])["where"]
    assert where == f"bgp route 192.168.50.1/32 via 10.1.1.50; L3-aliasing active, ES vES-1 ({VESI})"
    assert cells(LENSES["where"], found["leaf2"])["where"].endswith(", not attached here")
    # A route over no segment's next-hop names none.
    (plain,) = [s for s in _run("where", address="10.1.1.20") if s.kind == "route" and s.node == "leaf1"]
    assert (plain.esi, plain.attached) == ("", None)


def test_a_subnet_routed_locally_where_the_ip_is_unbound_is_where_it_would_be_resolved() -> None:
    from fcli.render import cells

    routes = {s.node: s for s in _run("where", address="10.1.1.20") if s.kind == "route"}
    # leaf2 has the subnet on irb0.1, but no ARP entry or host route for the IP.
    assert (routes["leaf2"].prefix, routes["leaf2"].route_type) == ("10.1.1.0/24", "local")
    assert cells(LENSES["where"], routes["leaf2"])["where"] == (
        "local route 10.1.1.0/24 via 10.1.1.1 (irb0.1); no ARP entry"
    )
    # Where the IP is bound, its ARP entry says where it is; the subnet adds nothing.
    assert [s.node for s in _run("where", address="10.1.1.20") if s.route_type == "local"] == ["leaf2"]


def test_a_route_aliased_to_the_candidates_names_the_segment_its_rt5_carries() -> None:
    (leaf3,) = [s for s in LENSES["where"].run(_aliased(), address="192.168.50.1") if s.node == "leaf3"]
    # Its longest match, not the default route.
    assert leaf3.prefix == "192.168.50.0/24"
    assert (leaf3.segment, leaf3.esi, leaf3.attached) == ("vES-1", VESI, False)
    # Without the RT5 - at an instant of the history - its route-table alone names none.
    found = {s.node: s for s in LENSES["where"].run(_aliased(rt5=False), address="192.168.50.1")}
    assert (found["leaf3"].esi, found["leaf1"].esi) == ("", VESI)


def test_where_queries_the_rt5s_of_the_prefixes_an_ip_is_routed_by_over_evpn() -> None:
    where = LENSES["where"]
    # leaf3's longest match; leaf1 and leaf2 route over the CE, not EVPN.
    assert where.queried(_aliased(), address="192.168.50.1") == [
        ("bgp-rib-evpn-5", {"ip_prefix": "192.168.50.0/24"})
    ]
    assert where.queried(fabric(), address="10.1.1.20") == [("bgp-rib-evpn-5", {"ip_prefix": "10.1.1.20/32"})]
    assert where.queried(fabric(), address="10.9.9.9") == []
    assert where.queried(fabric(), address=HOST_A) == []
    assert LENSES["path"].queried(fabric(), source="leaf1", destination="10.1.1.20") == []

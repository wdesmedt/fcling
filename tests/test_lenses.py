"""The lenses and the topology, over a small EVPN fabric built from records."""

from __future__ import annotations

import dataclasses
import json

import pytest

from fcli.lens import LensError
from fcli.lenses import LENSES
from fcli.reports.instances import NiInterface
from fcli.reports.layer2 import VxlanInterface
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
    configured = [s for s in _run("where", address="10.1.1.1") if s.kind == "configured"]
    assert sorted(s.node for s in configured) == ["leaf1", "leaf2", "leaf3"]
    (none,) = _run("where", address="10.9.9.9")
    assert none.kind == "not-found"


def test_endpoints_are_listed_where_they_are_attached() -> None:
    found = sorted((e.node, e.ip, e.mac, e.subinterface, e.learned, e.es) for e in _run("endpoints"))
    assert found == [
        ("leaf1", "", DUP, "ethernet-1/5.1", "local", ""),
        ("leaf1", "10.1.1.10", HOST_A, "lag1.1", "local", "ES-A"),
        ("leaf2", "", HOST_A, "lag1.1", "local", "ES-A"),
        ("leaf3", "", DUP, "ethernet-1/1.1", "local", ""),
        ("leaf3", "10.1.1.20", HOST_B, "ethernet-1/1.1", "local", ""),
    ]
    # Host B is not listed on leaf1, which only learned it from leaf3.
    (a,) = [e for e in _run("endpoints") if e.ip == "10.1.1.10"]
    assert (a.ip_vrf, a.mac_vrf, a.lldp) == ("ipvrf-1", "macvrf-1", ("hostA",))


def _hops(source: str, destination: str, ni: str | None = None) -> list[tuple]:
    return [
        (h.hop, h.node, h.ni, h.outcome, h.peer or h.endpoint or h.resumes_in or h.egress)
        for h in LENSES["path"].run(fabric(), source=source, destination=destination, ni=ni)
    ]


def test_a_routed_path_over_vxlan() -> None:
    assert _hops("leaf1", "10.1.1.20", "ipvrf-1") == [
        (1, "leaf1", "ipvrf-1", "tunnel", "10.0.0.3"),
        (2, "leaf1", "default", "forwarded", "spine1"),
        (3, "spine1", "default", "forwarded", "leaf3"),
        (4, "leaf3", "default", "endpoint-reached", "ipvrf-1"),
        (5, "leaf3", "ipvrf-1", "delivered", "ethernet-1/1.1"),
    ]


def test_a_host_source_enters_on_every_leaf_of_its_segment() -> None:
    hops = _hops("10.1.1.10", "10.1.1.20")
    assert [h for h in hops if h[0] == 0] == [
        (0, "leaf1", "macvrf-1", "ingress", "lag1.1"),
        (0, "leaf2", "macvrf-1", "ingress", "lag1.1"),
    ]
    # leaf2 has no route to host B: where its branch ends, and why.
    assert (1, "leaf2", "ipvrf-1", "delivered", "irb0.1") not in hops
    assert hops[-1][3] == "delivered"


def test_a_bridged_path_to_a_segment_is_aliased_over_its_vteps() -> None:
    hops = _hops("leaf3", HOST_A, "macvrf-1")
    assert [h for h in hops if h[3] == "tunnel"] == [
        (1, "leaf3", "macvrf-1", "tunnel", "10.0.0.1"),
        (1, "leaf3", "macvrf-1", "tunnel", "10.0.0.2"),
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


def test_path_says_where_it_goes_nowhere() -> None:
    assert _hops("leaf2", "10.7.7.7") == [(1, "leaf2", "default", "no-route", "")]
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
    declared = dataclasses.replace(
        full, records={r: v for r, v in full.records.items() if r in spec.requires}
    )
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

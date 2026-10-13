"""Checks and incidents over fabrics built from records."""

from __future__ import annotations

from typing import Any

from fcli.checks import run_checks
from fcli.fabric import Fabric
from fcli.incidents import correlate
from fcli.inventory import Node
from fcli.reports.bgp import BgpPeer
from fcli.reports.health import BfdSession
from fcli.reports.instances import NiInterface
from fcli.reports.interfaces import Subinterface
from fcli.reports.lldp import LldpNeighbor
from fcli.reports.routing import Route

from .fabric_replay import recorded_fabric
from .replay import RELEASES

NODES = [Node("leaf1", "leaf1"), Node("leaf2", "leaf2"), Node("spine1", "spine1")]


def _fabric(records: dict[str, list[Any]], unreachable: dict[str, str] | None = None) -> Fabric:
    return Fabric.of(NODES, records, unreachable)


def _cabled() -> dict[str, list[Any]]:
    """leaf1 e1 <-> spine1 e1, leaf2 e1 <-> spine1 e2, eBGP and BFD over link-local."""
    lldp = [
        LldpNeighbor(node="leaf1", interface="ethernet-1/1", system_name="spine1", port_id="ethernet-1/1"),
        LldpNeighbor(node="spine1", interface="ethernet-1/1", system_name="leaf1", port_id="ethernet-1/1"),
        LldpNeighbor(node="leaf2", interface="ethernet-1/1", system_name="spine1", port_id="ethernet-1/2"),
        LldpNeighbor(node="spine1", interface="ethernet-1/2", system_name="leaf2", port_id="ethernet-1/1"),
    ]
    subif = [
        Subinterface(node=n, interface=i, name=f"{i}.0", admin="enable", oper="up", ip_mtu=9000)
        for n, i in [
            ("leaf1", "ethernet-1/1"),
            ("spine1", "ethernet-1/1"),
            ("leaf2", "ethernet-1/1"),
            ("spine1", "ethernet-1/2"),
        ]
    ]
    return {
        "lldp": lldp,
        "subif": subif,
        "bgp-peers": [],
        "bfd": [],
        "ni": [],
        "ipv4-rib": [],
        "ipv6-rib": [],
    }


def test_a_healthy_fabric_says_nothing() -> None:
    assert run_checks(_fabric(_cabled())) == []
    for release in ("25.3.2", "25.10.3", "26.7.1"):
        assert run_checks(recorded_fabric(sorted((RELEASES / release).glob("*.json.gz")))) == []


def test_a_broken_cable_is_one_incident() -> None:
    records = _cabled()
    records["subif"] = [
        s.model_copy(update={"oper": "down", "down_reason": "port-down"})
        if (s.node, s.interface) in {("leaf1", "ethernet-1/1"), ("spine1", "ethernet-1/1")}
        else s
        for s in records["subif"]
    ]
    records["bgp-peers"] = [
        BgpPeer(node="leaf1", ni="default", peer="fe80::1%ethernet-1/1.0", state="active"),
        BgpPeer(node="spine1", ni="default", peer="fe80::2%ethernet-1/1.0", state="active"),
    ]
    records["bfd"] = [
        BfdSession(
            node="leaf1", ni="default", remote_address="fe80::1", state="down", interface="ethernet-1/1.0"
        )
    ]
    fabric = _fabric(records)
    findings = run_checks(fabric)
    assert {f.check for f in findings} == {"itf_down", "bgp_down", "bfd_down"}
    (incident,) = correlate(findings, fabric)
    assert incident.kind == "link"
    assert incident.root.check == "itf_down"
    assert incident.title.startswith("leaf1 e1/1 <-> spine1 e1/1: interface down")
    assert len(incident.findings) == len(findings)
    assert "2 BGP sessions down" in incident.explanation


def test_sessions_to_a_node_that_is_gone_are_about_the_node() -> None:
    records = _cabled()
    records["bgp-peers"] = [
        BgpPeer(node="leaf1", ni="default", peer="fe80::1%ethernet-1/1.0", state="active")
    ]
    fabric = _fabric(records, unreachable={"spine1": "no answer from spine1 on gNMI port 57400"})
    incidents = correlate(run_checks(fabric), fabric)
    (incident,) = incidents
    assert (incident.kind, incident.title) == ("node", "spine1 is unreachable")
    assert [f.check for f in incident.findings] == ["node_unreachable", "bgp_down"]


def test_one_cause_in_many_places_is_a_pattern() -> None:
    records = _cabled()
    records["bfd"] = [
        BfdSession(node=n, ni="default", remote_address=f"10.0.0.{i}", state="down", remote_discriminator=0)
        for i, n in enumerate(["leaf1", "leaf2", "spine1"])
    ]
    fabric = _fabric(records)
    (incident,) = correlate(run_checks(fabric), fabric)
    assert incident.kind == "pattern"
    assert incident.title == "BFD session down on 3 sessions"
    assert "is BFD enabled there?" in incident.explanation


def test_an_overlay_session_without_underlay_is_about_the_underlay() -> None:
    records = _cabled()
    records["ni"] = [
        NiInterface(node="spine1", ni="default", interface="system0.0", prefixes=("10.0.0.100/32",)),
        NiInterface(node="leaf1", ni="default", interface="system0.0", prefixes=("10.0.0.1/32",)),
    ]
    records["ipv4-rib"] = [Route(node="leaf1", ni="default", prefix="10.0.0.1/32")]
    records["bgp-peers"] = [BgpPeer(node="leaf1", ni="default", peer="10.0.0.100", state="active")]
    fabric = _fabric(records)
    (incident,) = correlate(run_checks(fabric), fabric)
    assert incident.kind == "underlay"
    assert incident.root.check == "underlay_unreachable"
    assert incident.title == "leaf1 has no underlay route to spine1"
    assert {f.check for f in incident.related} == {"bgp_down", "bgp_peer_mismatch"}


def test_flapping_comes_from_the_history() -> None:
    changes = [
        {
            "ts": float(t),
            "node": "leaf1",
            "report": "bgp-peers",
            "key": '["default","10.0.0.9"]',
            "op": "change",
            "before": {"state": a},
            "after": {"state": b},
        }
        for t, (a, b) in enumerate(
            [("established", "active"), ("active", "established"), ("established", "idle")]
        )
    ]
    fabric = Fabric.of(NODES, _cabled(), changes=changes)
    (finding,) = [f for f in run_checks(fabric) if f.check == "flapping"]
    assert finding.detail.startswith("bgp changed 3 times (established / active / idle)")
    (incident,) = correlate([finding], fabric)
    assert incident.kind == "session"


def test_a_containerlab_nodes_memory_is_its_hosts_and_only_information() -> None:
    from fcli.reports.health import Resource

    nodes = [Node("leaf1", "leaf1", virtual=True), Node("leaf2", "leaf2")]
    resources = [
        Resource(node=n, component="control A", name=name, used_percent=97)
        for n in ("leaf1", "leaf2")
        for name in ("memory", "ip-lpm-routes")
    ]
    found = {(f.node, f.subject): f.severity for f in run_checks(Fabric.of(nodes, {"resources": resources}))}
    assert found == {
        ("leaf1", "control A memory"): "info",
        ("leaf1", "control A ip-lpm-routes"): "error",  # its forwarding table is its own
        ("leaf2", "control A memory"): "error",
        ("leaf2", "control A ip-lpm-routes"): "error",
    }


# =========================================================================== #
# Ported from the original fcli's tests/test_checks.py: each check on a fabric
# written out as the records it reads, and the findings it should produce.
# =========================================================================== #

from collections.abc import Iterable, Sequence  # noqa: E402

from fcli.checks import CHECKS, ERROR, INFO, WARNING, required, underlay_domains  # noqa: E402
from fcli.reports import REGISTRY  # noqa: E402
from fcli.reports.bgp import Family  # noqa: E402
from fcli.reports.health import IsisAdjacency, OspfNeighbor  # noqa: E402
from fcli.reports.instances import BgpVpnInstance  # noqa: E402
from fcli.reports.interfaces import InterfaceStats  # noqa: E402
from fcli.reports.layer2 import DfCandidate, EsAssociation, EthernetSegment, VxlanInterface  # noqa: E402


def fab(
    reports: dict[str, Sequence[Any]],
    nodes: Iterable[str] = (),
    unreachable: dict[str, str] | None = None,
    virtual: Iterable[str] = (),
) -> Fabric:
    """A fabric of *reports*; its nodes are the ones named in them, *nodes* and the unreachable."""
    names = set(nodes) | {r.node for records in reports.values() for r in records} | set(unreachable or {})
    virtual = set(virtual)
    return Fabric.of(
        [Node(n, n, virtual=n in virtual) for n in sorted(names)], reports, unreachable=unreachable
    )


def run(check: str, fabric: Fabric) -> list[Any]:
    return list(CHECKS[check].run(fabric))


# --------------------------------------------------------------------------- the registry

#: Checks that read the history, or the collection itself, rather than a report.
NO_REPORT = {"flapping", "node_unreachable"}


def test_every_check_reads_reports_that_exist() -> None:
    for name, check in CHECKS.items():
        assert check.name == name
        if name in NO_REPORT:
            assert not check.requires
            continue
        assert check.requires, f"{name} reads no report"
        for report in check.requires:
            assert report in REGISTRY, f"{name} reads {report!r}, which is not a report"


def test_required_reports_is_what_the_checks_ask_for() -> None:
    assert set(required()) == {r for c in CHECKS.values() for r in c.requires}
    assert required(["bgp_down"]) == ("bgp-peers",)


# --------------------------------------------------------------------------- BGP


def _peers(state: str = "established", evpn: Family | None = None, **overrides: Any) -> dict[str, list[Any]]:
    """One leaf with one session carrying ipv4-unicast and *evpn*."""
    evpn = evpn or Family(name="evpn", received=12, active=12, sent=6)
    peer = BgpPeer(
        node="leaf1",
        ni="default",
        peer="10.0.0.2",
        state=state,
        peer_as=65002,
        group="spines",
        families=(Family(name="ipv4-unicast", received=5, active=5, sent=3), evpn),
    )
    return {"bgp-peers": [peer.model_copy(update=overrides)]}


def test_bgp_down_finds_a_session_that_is_not_established() -> None:
    (finding,) = run("bgp_down", fab(_peers(state="active")))
    assert (finding.severity, finding.node, finding.subject) == (ERROR, "leaf1", "default/10.0.0.2")
    assert "session is active" in finding.detail and "peer-group spines" in finding.detail


def test_bgp_down_leaves_an_established_session_alone() -> None:
    assert run("bgp_down", fab(_peers())) == []


def test_bgp_down_reads_the_state_whatever_its_case() -> None:
    assert run("bgp_down", fab(_peers(state="Established"))) == []


def test_bgp_down_does_not_report_a_session_that_is_not_meant_to_be_up() -> None:
    """The peers report carries no admin-state to tell a disabled peer by."""
    assert run("bgp_down", fab(_peers(state=""))) == []


def test_bgp_af_down_finds_the_family_under_an_established_session() -> None:
    (finding,) = run("bgp_af_down", fab(_peers(evpn=Family(name="evpn", oper="down"))))
    assert finding.severity == ERROR and "evpn is down" in finding.detail


def test_bgp_af_down_says_nothing_about_a_session_already_reported_down() -> None:
    """One fault is one finding: bgp_down has it."""
    assert run("bgp_af_down", fab(_peers(state="idle", evpn=Family(name="evpn", oper="down")))) == []


def test_bgp_af_down_ignores_a_family_that_was_never_enabled() -> None:
    assert run("bgp_af_down", fab(_peers(evpn=Family(name="evpn", enabled=False, oper="down")))) == []


def test_bgp_no_routes_finds_a_family_that_has_learned_nothing() -> None:
    (finding,) = run("bgp_no_routes", fab(_peers(evpn=Family(name="evpn", received=0, active=0, sent=6))))
    # Information, not a warning as it was: normal the day a fabric is built.
    assert finding.severity == INFO
    assert "evpn is up but has received no routes" in finding.detail


def test_bgp_no_routes_counts_received_rather_than_active() -> None:
    """A route received and not selected is a policy question, not a fault."""
    assert run("bgp_no_routes", fab(_peers(evpn=Family(name="evpn", received=12, active=0, sent=6)))) == []


def test_bgp_no_routes_ignores_a_family_that_is_not_carrying() -> None:
    assert run("bgp_no_routes", fab(_peers(evpn=Family(name="evpn", enabled=False)))) == []


def test_bgp_no_routes_ignores_a_family_that_is_down() -> None:
    """A family that is down is bgp_af_down's finding, not an empty one."""
    assert run("bgp_no_routes", fab(_peers(evpn=Family(name="evpn", oper="down")))) == []


def test_bgp_checks_survive_a_node_with_no_neighbors() -> None:
    assert run("bgp_down", fab({"bgp-peers": []}, nodes=["leaf1"])) == []


# --------------------------------------------------------------------------- interfaces


def _subif(node: str = "leaf1", **overrides: Any) -> dict[str, list[Any]]:
    subif = Subinterface(
        node=node,
        interface="ethernet-1/1",
        name="ethernet-1/1.0",
        type="routed",
        admin="enable",
        oper="up",
        ip_mtu=9000,
    )
    return {"subif": [subif.model_copy(update=overrides)]}


def test_itf_down_finds_a_subinterface_enabled_but_not_up() -> None:
    (finding,) = run("itf_down", fab(_subif(oper="down", down_reason="port-down")))
    assert (finding.severity, finding.subject) == (ERROR, "ethernet-1/1.0")
    assert "port-down" in finding.detail


def test_itf_down_leaves_a_port_held_down_on_purpose_alone() -> None:
    """The standby side of a single-active segment reads as down/standby."""
    assert run("itf_down", fab(_subif(oper="down/standby", down_reason="standby-signaling"))) == []


def test_itf_down_leaves_an_administratively_disabled_port_alone() -> None:
    assert run("itf_down", fab(_subif(oper="down", admin="disable"))) == []


def test_itf_down_says_so_when_the_node_gave_no_reason() -> None:
    (finding,) = run("itf_down", fab(_subif(oper="down")))
    assert "no reason reported" in finding.detail


def test_itf_down_ignores_the_management_interface() -> None:
    assert run("itf_down", fab(_subif(interface="mgmt0", name="mgmt0.0", oper="down"))) == []


def _ifstats(node: str = "leaf1", **overrides: Any) -> InterfaceStats:
    return InterfaceStats(node=node, name="ethernet-1/1", in_kbps=12.0, out_kbps=8.0).model_copy(
        update=overrides
    )


def test_itf_errors_finds_error_packets() -> None:
    (finding,) = run("itf_errors", fab({"ifstats": [_ifstats(in_errors=4)]}))
    assert (finding.severity, finding.subject) == (ERROR, "ethernet-1/1")
    assert "4 in / 0 out error packets" in finding.detail


def test_itf_errors_reports_discards_less_urgently_than_errors() -> None:
    findings = run("itf_errors", fab({"ifstats": [_ifstats(out_discards=7)]}))
    assert [f.severity for f in findings] == [WARNING]
    assert "0 in / 7 out discarded packets" in findings[0].detail


def test_itf_errors_reports_errors_and_discards_separately() -> None:
    findings = run("itf_errors", fab({"ifstats": [_ifstats(in_errors=1, in_discards=2)]}))
    assert sorted(f.severity for f in findings) == [ERROR, WARNING]


def test_itf_errors_says_nothing_about_a_clean_interface() -> None:
    assert run("itf_errors", fab({"ifstats": [_ifstats()]})) == []


def test_a_containerlab_nodes_dropped_packets_are_not_a_finding() -> None:
    """A veth discards IPv6 multicast and the like that a real port forwards."""
    stats = [_ifstats(n, in_discards=3, in_errors=1) for n in ("leaf1", "leaf2")]
    assert [f.node for f in run("itf_errors", fab({"ifstats": stats}, virtual=["leaf1"]))] == [
        "leaf2",
        "leaf2",
    ]


# --------------------------------------------------------------------------- topology


def _lldp(**adjacencies: list[tuple[str, str, str]]) -> list[LldpNeighbor]:
    """``_lldp(leaf1=[("ethernet-1/1", "spine1", "ethernet-1/1")])``, per node."""
    return [
        LldpNeighbor(node=node, interface=local, system_name=peer, port_id=peer_port)
        for node, links in adjacencies.items()
        for local, peer, peer_port in links
    ]


def _link_pair() -> list[LldpNeighbor]:
    return _lldp(
        leaf1=[("ethernet-1/1", "spine1", "ethernet-1/1")], spine1=[("ethernet-1/1", "leaf1", "ethernet-1/1")]
    )


def test_lldp_one_sided_says_nothing_about_a_link_both_ends_see() -> None:
    assert run("lldp_one_sided", fab({"lldp": _link_pair()})) == []


def test_lldp_one_sided_finds_the_end_that_is_not_seen_back() -> None:
    lldp = _lldp(leaf1=[("ethernet-1/1", "spine1", "ethernet-1/1")])
    (finding,) = run("lldp_one_sided", fab({"lldp": lldp}, nodes=["spine1"]))
    assert (finding.node, finding.subject) == ("leaf1", "ethernet-1/1")
    assert "does not see it back" in finding.detail


def test_lldp_one_sided_ignores_a_neighbour_that_is_not_in_the_inventory() -> None:
    """A node we do not poll cannot be expected to report anything back."""
    assert (
        run("lldp_one_sided", fab({"lldp": _lldp(leaf1=[("ethernet-1/1", "some-router", "xe-0/0/0")])})) == []
    )


def test_lldp_one_sided_waits_for_a_far_end_that_could_not_be_read() -> None:
    lldp = _lldp(leaf1=[("ethernet-1/1", "spine1", "ethernet-1/1")])
    assert run("lldp_one_sided", fab({"lldp": lldp}, unreachable={"spine1": "timed out"})) == []


def test_lldp_one_sided_matches_a_short_system_name_to_a_prefixed_inventory() -> None:
    """containerlab names a node clab-dc1-leaf1; the node advertises leaf1."""
    lldp = [
        LldpNeighbor(
            node="clab-dc1-leaf1", interface="ethernet-1/1", system_name="spine1", port_id="ethernet-1/1"
        ),
        LldpNeighbor(
            node="clab-dc1-spine1", interface="ethernet-1/1", system_name="leaf1", port_id="ethernet-1/1"
        ),
    ]
    assert run("lldp_one_sided", fab({"lldp": lldp})) == []


def test_lldp_one_sided_ignores_management_links() -> None:
    lldp = _lldp(leaf1=[("mgmt0", "spine1", "mgmt0")])
    assert run("lldp_one_sided", fab({"lldp": lldp}, nodes=["spine1"])) == []


def _mtu(node: str, mtu: int, index: str = "0") -> Subinterface:
    return Subinterface(
        node=node,
        interface="ethernet-1/1",
        name=f"ethernet-1/1.{index}",
        admin="enable",
        oper="up",
        ip_mtu=mtu,
    )


def test_mtu_mismatch_finds_two_ends_that_disagree_and_reports_the_link_once() -> None:
    (finding,) = run(
        "mtu_mismatch", fab({"lldp": _link_pair(), "subif": [_mtu("leaf1", 9000), _mtu("spine1", 1500)]})
    )
    assert (finding.severity, finding.subject) == (ERROR, "ethernet-1/1.0")
    assert "9000" in finding.detail and "1500" in finding.detail


def test_mtu_mismatch_says_nothing_when_the_ends_agree() -> None:
    assert (
        run("mtu_mismatch", fab({"lldp": _link_pair(), "subif": [_mtu("leaf1", 9000), _mtu("spine1", 9000)]}))
        == []
    )


def test_mtu_mismatch_compares_subinterfaces_of_the_same_index() -> None:
    """Two services on one trunk are two MTUs, and neither is the other's."""
    subif = [_mtu("leaf1", 9000, index="0"), _mtu("spine1", 1500, index="10")]
    assert run("mtu_mismatch", fab({"lldp": _link_pair(), "subif": subif})) == []


def test_mtu_mismatch_needs_both_ends_to_report_one() -> None:
    assert run("mtu_mismatch", fab({"lldp": _link_pair(), "subif": [_mtu("leaf1", 9000)]})) == []


# --------------------------------------------------------------------------- EVPN services


def _ni(
    node: str, *, vxlan: str = "vxlan1.100", in_rt: str = "65000:100", out_rt: str = "65000:100"
) -> NiInterface:
    return NiInterface(
        node=node,
        ni="mac-vrf-100",
        type="mac-vrf",
        oper="up",
        overlays=(vxlan,),
        vpn=(BgpVpnInstance(id=1, import_rts=(in_rt,), export_rts=(out_rt,)),),
    )


def _vxlan(node: str, *, itf: str = "vxlan1.100", vni: int = 100) -> VxlanInterface:
    return VxlanInterface(node=node, name=itf, ni="mac-vrf-100", vni=vni)


def _gateway(node: str, *, dc_rt: str = "65000:100", wan_rt: str = "65000:100") -> NiInterface:
    """A DCI gateway: the leaves' instance, plus a WAN-side instance of its own."""
    return _ni(node).model_copy(
        update={
            "vpn": (
                BgpVpnInstance(id=1, import_rts=(dc_rt,), export_rts=(dc_rt,)),
                BgpVpnInstance(id=2, import_rts=(wan_rt,), export_rts=(wan_rt,), rd="192.0.2.8:100"),
            )
        }
    )


def _evpn(ni: list[Any], vxlan: list[Any], **more: Sequence[Any]) -> Fabric:
    return fab({"ni": ni, "vxlan": vxlan, "ipv4-rib": [], "ipv6-rib": [], **more})


def test_evpn_service_mismatch_says_nothing_when_two_nodes_agree() -> None:
    assert (
        run("evpn_service_mismatch", _evpn([_ni("leaf1"), _ni("leaf2")], [_vxlan("leaf1"), _vxlan("leaf2")]))
        == []
    )


def test_evpn_service_mismatch_finds_a_vni_that_differs() -> None:
    findings = run(
        "evpn_service_mismatch",
        _evpn([_ni("leaf1"), _ni("leaf2")], [_vxlan("leaf1", vni=100), _vxlan("leaf2", vni=200)]),
    )
    # Both ends are wrong until someone decides which one is right.
    assert {f.node for f in findings} == {"leaf1", "leaf2"}
    assert all(f.subject == "mac-vrf-100" and "VNI" in f.detail for f in findings)


def test_evpn_service_mismatch_does_not_hold_an_unread_vni_against_every_node() -> None:
    """A node read without its vxlan-interfaces has an unknown VNI, not a different one."""
    findings = run(
        "evpn_service_mismatch",
        _evpn([_ni("leaf1"), _ni("leaf2"), _ni("leaf3")], [_vxlan("leaf2"), _vxlan("leaf3")]),
    )
    # One warning about the gap, on the node that has it - not an error per service on every node.
    assert [(f.node, f.severity, f.subject) for f in findings] == [("leaf1", WARNING, "VNI")]
    assert "vxlan1.100" in findings[0].detail


def test_evpn_service_mismatch_leaves_a_node_it_could_not_read_to_node_unreachable() -> None:
    fabric = _evpn([_ni("leaf1"), _ni("leaf2")], [_vxlan("leaf2")])
    fabric.unreachable = {"leaf1": "timed out"}
    assert run("evpn_service_mismatch", fabric) == []


def test_evpn_service_mismatch_still_compares_route_targets_without_a_vni() -> None:
    findings = run(
        "evpn_service_mismatch", _evpn([_ni("leaf1", out_rt="65000:999"), _ni("leaf2")], [_vxlan("leaf2")])
    )
    assert any(f.severity == ERROR and "export route-target" in f.detail for f in findings)
    assert not any(f.detail.startswith("VNI") for f in findings)


def test_evpn_service_mismatch_finds_route_targets_that_differ() -> None:
    findings = run(
        "evpn_service_mismatch",
        _evpn([_ni("leaf1"), _ni("leaf2", out_rt="65000:999")], [_vxlan("leaf1"), _vxlan("leaf2")]),
    )
    assert findings and all("export route-target" in f.detail for f in findings)


def test_evpn_service_mismatch_reads_a_route_target_however_it_is_written() -> None:
    """One report strips the 'target:' prefix and another keeps it."""
    ni = [_ni("leaf1", in_rt="65000:100"), _ni("leaf2", in_rt="target:65000:100")]
    assert run("evpn_service_mismatch", _evpn(ni, [_vxlan("leaf1"), _vxlan("leaf2")])) == []


def test_evpn_service_mismatch_lets_a_gateway_carry_a_second_instance() -> None:
    """A gateway's WAN-side route-target is not a disagreement with the leaves."""
    nodes = ("leaf1", "leaf2", "dcgw1", "dcgw2")
    ni = [_gateway(n) if n.startswith("dcgw") else _ni(n) for n in nodes]
    assert run("evpn_service_mismatch", _evpn(ni, [_vxlan(n) for n in nodes])) == []


def test_evpn_service_mismatch_compares_a_second_instance_between_the_gateways() -> None:
    ni = [_ni("leaf1"), _gateway("dcgw1"), _gateway("dcgw2", wan_rt="65000:999")]
    findings = run("evpn_service_mismatch", _evpn(ni, [_vxlan(n) for n in ("leaf1", "dcgw1", "dcgw2")]))
    # Only the gateways have a second instance, so only they are held to it.
    assert {f.node for f in findings} == {"dcgw1", "dcgw2"}
    assert all("bgp-instance 2" in f.detail and "leaf1" not in f.detail for f in findings)


def test_evpn_service_mismatch_takes_a_name_split_by_route_target_in_one_underlay_as_a_warning() -> None:
    """Two services under one name in one fabric, or a mistyped target: a
    warning either way, and no error about the VNI between them."""
    ni = [
        _ni("leaf1", in_rt="65000:201", out_rt="65000:201"),
        _ni("leaf2", in_rt="65000:201", out_rt="65000:201"),
        _ni("leaf3", in_rt="65000:202", out_rt="65000:202"),
    ]
    vxlan = [_vxlan("leaf1", vni=201), _vxlan("leaf2", vni=201), _vxlan("leaf3", vni=202)]
    findings = run("evpn_service_mismatch", _evpn(ni, vxlan))
    assert {f.severity for f in findings} == {WARNING}
    assert {f.node for f in findings} == {"leaf1", "leaf2", "leaf3"}
    leaf1 = next(f for f in findings if f.node == "leaf1")
    assert "65000:201, while leaf3 65000:202 in the same underlay" in leaf1.detail
    assert "two services under one name" in leaf1.detail


def _two_datacenters(*, dc2_gateway_wan_rt: str = "65000:100") -> Fabric:
    """Two fabrics whose underlays do not see each other, joined by gateways over a WAN.

    Each datacenter has a bridge domain of the same name with its own VNI and
    route-target; the gateways carry the leaves' instance and a WAN-side one
    of their own, and reach each other's loopbacks over the WAN.
    """
    dc1 = {"leaf1": "192.0.2.1", "leaf2": "192.0.2.2", "dcgw1": "192.0.2.8"}
    dc2 = {"leaf5": "192.0.2.5", "leaf6": "192.0.2.6", "dcgw3": "192.0.2.18"}
    gateways = {n: a for n, a in {**dc1, **dc2}.items() if n.startswith("dcgw")}
    ni: list[Any] = []
    rib: list[Any] = []
    vxlan: list[Any] = []
    for site, members, rt, vni in ((1, dc1, "65000:201", 201), (2, dc2, "65000:202", 202)):
        for node, address in members.items():
            reachable = [a for n, a in members.items() if n != node]
            if node in gateways:
                # Gateways learn every gateway's loopback over the WAN.
                reachable += [a for n, a in gateways.items() if n != node]
                wan = dc2_gateway_wan_rt if site == 2 else "65000:100"
                ni.append(_gateway(node, dc_rt=rt, wan_rt=wan))
            else:
                ni.append(_ni(node, in_rt=rt, out_rt=rt))
            ni.append(
                NiInterface(
                    node=node,
                    ni="default",
                    type="default",
                    interface="system0.0",
                    prefixes=(f"{address}/32",),
                )
            )
            rib += [Route(node=node, ni="default", prefix=f"{other}/32", type="bgp") for other in reachable]
            vxlan.append(_vxlan(node, vni=vni))
    return _evpn(ni, vxlan, **{"ipv4-rib": rib})


def test_evpn_service_mismatch_says_nothing_about_a_name_shared_across_underlays() -> None:
    """Leaves whose underlays do not see each other are never one service."""
    assert run("evpn_service_mismatch", _two_datacenters()) == []


def test_evpn_service_mismatch_holds_the_gateways_wan_side_together_across_underlays() -> None:
    """The WAN side is one service among the gateways, reached over the WAN."""
    findings = run("evpn_service_mismatch", _two_datacenters(dc2_gateway_wan_rt="65000:999"))
    assert {(f.node, f.severity) for f in findings} == {("dcgw1", ERROR), ("dcgw3", ERROR)}
    assert all("bgp-instance 2" in f.detail for f in findings)


def test_underlay_domains_fall_back_to_one_without_route_tables() -> None:
    assert underlay_domains(["b", "a"], {}, {}) == [["a", "b"]]
    assert underlay_domains([], {}, {}) == []


def test_evpn_service_mismatch_still_errs_on_a_vni_within_one_route_target_group() -> None:
    """The silent failure: the same route-target, so routes are imported, but a
    different VNI, so the data plane never joins up."""
    ni = [_ni("leaf1"), _ni("leaf2"), _ni("leaf5", in_rt="65000:202", out_rt="65000:202")]
    vxlan = [_vxlan("leaf1", vni=100), _vxlan("leaf2", vni=999), _vxlan("leaf5", vni=202)]
    errors = [f for f in run("evpn_service_mismatch", _evpn(ni, vxlan)) if f.severity == ERROR]
    assert {f.node for f in errors} == {"leaf1", "leaf2"}
    assert all("VNI" in f.detail and "leaf5" not in f.detail for f in errors)


def test_evpn_service_mismatch_ignores_a_service_only_one_node_has() -> None:
    """A service on one leaf is a service, not a disagreement."""
    assert run("evpn_service_mismatch", _evpn([_ni("leaf1")], [_vxlan("leaf1")])) == []


def test_evpn_service_mismatch_ignores_the_default_network_instance() -> None:
    ni = [
        NiInterface(node="leaf1", ni="default", type="default", oper="up"),
        NiInterface(
            node="leaf2",
            ni="default",
            type="default",
            oper="up",
            vpn=(BgpVpnInstance(id=1, import_rts=("x",), export_rts=("y",)),),
        ),
    ]
    assert run("evpn_service_mismatch", _evpn(ni, [])) == []


# --------------------------------------------------------------------------- ethernet segments


def _candidates(*addresses: str) -> tuple[DfCandidate, ...]:
    """DF candidates written the way the ES report shows them: ``(DF)`` marks the elected one."""
    return tuple(
        DfCandidate(address=a.removesuffix("(DF)"), designated=a.endswith("(DF)")) for a in addresses
    )


def _es(
    node: str, associations: tuple[EsAssociation, ...] | None = None, **overrides: Any
) -> EthernetSegment:
    segment = EthernetSegment(
        node=node,
        name="es-1",
        esi="01:00:00:00:00:01:00:00:00:01",
        type="virtual",
        mh_mode="all-active",
        oper="up",
        interfaces=("lag1",),
        associations=associations
        if associations is not None
        else (EsAssociation(ni="mac-vrf-100", candidates=_candidates("10.0.0.1", "10.0.0.2(DF)")),),
    )
    return segment.model_copy(update=overrides)


def test_es_df_says_nothing_about_a_healthy_segment() -> None:
    assert run("es_df", fab({"es": [_es("leaf1"), _es("leaf2")]})) == []


def test_es_df_finds_a_network_instance_with_no_designated_forwarder() -> None:
    es = _es("leaf1", (EsAssociation(ni="mac-vrf-100", candidates=_candidates("10.0.0.1", "10.0.0.2")),))
    (finding,) = run("es_df", fab({"es": [es]}))
    assert finding.subject == "es-1/mac-vrf-100" and "no designated forwarder" in finding.detail


def test_es_df_finds_a_network_instance_with_no_candidates_at_all() -> None:
    (finding,) = run("es_df", fab({"es": [_es("leaf1", (EsAssociation(ni="mac-vrf-100"),))]}))
    assert "no candidates" in finding.detail


def test_es_df_checks_every_network_instance_on_a_segment() -> None:
    es = _es(
        "leaf1",
        (
            EsAssociation(ni="mac-vrf-100", candidates=_candidates("10.0.0.1(DF)")),
            EsAssociation(ni="mac-vrf-200", candidates=_candidates("10.0.0.1", "10.0.0.2")),
        ),
    )
    assert [f.subject for f in run("es_df", fab({"es": [es]}))] == ["es-1/mac-vrf-200"]


def test_es_df_finds_a_segment_that_is_down() -> None:
    assert any("segment is down" in f.detail for f in run("es_df", fab({"es": [_es("leaf1", oper="down")]})))


def test_es_df_finds_two_nodes_disagreeing_about_the_multi_homing_mode() -> None:
    findings = run("es_df", fab({"es": [_es("leaf1"), _es("leaf2", mh_mode="single-active")]}))
    assert {f.node for f in findings} == {"leaf1", "leaf2"}
    assert all("multi-homing mode" in f.detail for f in findings)


def test_es_df_does_not_compare_two_different_segments() -> None:
    es = [_es("leaf1"), _es("leaf2", esi="01:00:00:00:00:02:00:00:00:02", mh_mode="single-active")]
    assert run("es_df", fab({"es": es})) == []


def test_nodes_that_elect_different_designated_forwarders_are_an_error() -> None:
    """Two leaves each electing themselves both forward on a single-active segment."""

    def segment(node: str, df: str) -> EthernetSegment:
        candidates = tuple(DfCandidate(address=a, designated=a == df) for a in ("192.0.2.15", "192.0.2.16"))
        return EthernetSegment(
            node=node,
            name="vES",
            esi="00:01:ff",
            type="virtual",
            mh_mode="single-active",
            oper="up",
            associations=(EsAssociation(ni="ipvrf-1", candidates=candidates),),
        )

    findings = run("es_df", fab({"es": [segment("leaf5", "192.0.2.15"), segment("leaf6", "192.0.2.16")]}))
    assert [(f.node, f.subject) for f in findings] == [("leaf5", "vES/ipvrf-1"), ("leaf6", "vES/ipvrf-1")]
    assert findings[0].detail == (
        "nodes disagree on the designated forwarder: "
        "192.0.2.15 according to leaf5; 192.0.2.16 according to leaf6"
    )
    assert run("es_df", fab({"es": [segment("leaf5", "192.0.2.15"), segment("leaf6", "192.0.2.15")]})) == []


# --------------------------------------------------------------------------- running them together


def test_a_healthy_fabric_produces_nothing() -> None:
    reports = {
        **_peers(),
        "subif": [_mtu("leaf1", 9000), _mtu("spine1", 9000)],
        "ifstats": [_ifstats()],
        "lldp": _link_pair(),
        "ni": [_ni("leaf1"), _ni("leaf2")],
        "vxlan": [_vxlan("leaf1"), _vxlan("leaf2")],
        "es": [_es("leaf1"), _es("leaf2")],
    }
    assert run_checks(fab(reports)) == []


def test_findings_come_back_worst_first() -> None:
    fabric = fab({**_peers(state="idle"), "ifstats": [_ifstats(in_discards=3)]})
    assert [f.severity for f in run_checks(fabric)] == [ERROR, WARNING]


def test_only_runs_the_checks_asked_for() -> None:
    fabric = fab({**_peers(state="idle"), "ifstats": [_ifstats(in_errors=1)]})
    assert {f.check for f in run_checks(fabric, ["bgp_down"])} == {"bgp_down"}


def test_a_check_whose_reports_were_not_collected_finds_nothing() -> None:
    """Not asking a question is not the same as getting a clean answer: only what was read speaks."""
    assert {f.check for f in run_checks(fab(_peers(state="idle")))} == {"bgp_down"}


def test_a_node_that_could_not_be_read_is_reported_rather_than_passed_over() -> None:
    (finding,) = run_checks(fab(_peers(), unreachable={"leaf9": "not connected"}))
    assert (finding.check, finding.node, finding.severity) == ("node_unreachable", "leaf9", ERROR)
    assert "not connected" in finding.detail


def test_checks_tolerate_a_report_that_came_back_empty() -> None:
    assert run_checks(fab({"bgp-peers": [], "subif": [], "ifstats": []}, nodes=["leaf1"])) == []


# --------------------------------------------------------------------------- both ends of a session or link


def _addressed(**nodes: str) -> list[Subinterface]:
    """``_addressed(leaf1="10.0.0.0/31")``: one routed subinterface each."""
    family = lambda p: {"ipv6": (p,)} if ":" in p else {"ipv4": (p,)}  # noqa: E731
    return [
        Subinterface(node=node, interface="ethernet-1/1", name="ethernet-1/1.0", **family(prefix))
        for node, prefix in nodes.items()
    ]


def _session(
    node: str,
    peer: str,
    local_as: int,
    peer_as: int,
    families: tuple[str, ...] = ("ipv4-unicast",),
    state: str = "established",
    **more: Any,
) -> list[BgpPeer]:
    return [
        BgpPeer(
            node=node,
            ni="default",
            peer=peer,
            state=state,
            local_as=local_as,
            peer_as=peer_as,
            families=tuple(Family(name=f) for f in families),
            **more,
        )
    ]


def _bgp(leaf: list[BgpPeer], spine: list[BgpPeer]) -> Fabric:
    return fab(
        {"subif": _addressed(leaf1="10.0.0.1/31", spine1="10.0.0.0/31"), "ni": [], "bgp-peers": leaf + spine}
    )


def test_bgp_peer_mismatch_is_silent_on_two_ends_that_match() -> None:
    fabric = _bgp(_session("leaf1", "10.0.0.0", 65001, 65100), _session("spine1", "10.0.0.1", 65100, 65001))
    assert run("bgp_peer_mismatch", fabric) == []


def test_bgp_peer_mismatch_says_which_as_one_end_expects_and_the_other_runs() -> None:
    fabric = _bgp(
        _session("leaf1", "10.0.0.0", 65001, 65101, state="active"),
        _session("spine1", "10.0.0.1", 65100, 65001, state="active"),
    )
    (finding,) = run("bgp_peer_mismatch", fabric)
    assert (finding.node, finding.severity, finding.subject) == ("leaf1", ERROR, "default/10.0.0.0")
    assert finding.detail == "expects AS 65101 from spine1, which runs AS 65100"


def test_bgp_peer_mismatch_finds_a_session_the_far_end_never_configured() -> None:
    (finding,) = run(
        "bgp_peer_mismatch", _bgp(_session("leaf1", "10.0.0.0", 65001, 65100, state="active"), [])
    )
    assert finding.detail == "spine1 owns 10.0.0.0 but has no session back to leaf1 in default"


def test_bgp_peer_mismatch_trusts_a_far_end_that_takes_dynamic_neighbours() -> None:
    dynamic = [BgpPeer(node="spine1", ni="default", peer="10.9.9.9", state="established", dynamic=True)]
    assert (
        run("bgp_peer_mismatch", _bgp(_session("leaf1", "10.0.0.0", 65001, 65100, state="active"), dynamic))
        == []
    )


def test_bgp_peer_mismatch_finds_a_family_or_bfd_on_one_end_only_once() -> None:
    fabric = _bgp(
        _session("leaf1", "10.0.0.0", 65001, 65100, families=("ipv4-unicast", "evpn"), bfd=True),
        _session("spine1", "10.0.0.1", 65100, 65001),
    )
    assert [f.detail for f in run("bgp_peer_mismatch", fabric)] == [
        "evpn is enabled on leaf1 only: spine1 does not exchange it on this session",
        "BFD protects it on leaf1 only, so a failure is detected fast on one end",
    ]


def test_bgp_peer_mismatch_reads_a_link_local_peer_by_its_address() -> None:
    fabric = fab(
        {
            "subif": _addressed(leaf1="fe80::1/64", spine1="fe80::2/64"),
            "ni": [],
            "bgp-peers": _session("leaf1", "fe80::2%ethernet-1/1.0", 65001, 65100)
            + _session("spine1", "fe80::1%ethernet-1/1.0", 65100, 65002),
        }
    )
    (finding,) = run("bgp_peer_mismatch", fabric)
    assert finding.node == "spine1" and "expects AS 65002 from leaf1" in finding.detail


def test_bgp_peer_mismatch_says_nothing_about_a_session_still_opening() -> None:
    """Families and BFD are negotiated: a session that has not come up - a
    dynamic one refused and retrying - has none yet on either end."""
    fabric = _bgp(
        _session(
            "leaf1", "10.0.0.0", 65001, 65100, families=("ipv4-unicast", "evpn"), bfd=True, state="opensent"
        ),
        _session("spine1", "10.0.0.1", 65100, 65001, families=(), state="active"),
    )
    assert run("bgp_peer_mismatch", fabric) == []


def test_bgp_peer_mismatch_compares_no_as_a_dynamic_neighbour_only_learned() -> None:
    """A dynamic neighbour's peer AS is what the far end announced."""
    fabric = _bgp(
        _session("leaf1", "10.0.0.0", 65001, 65999, state="active", dynamic=True),
        _session("spine1", "10.0.0.1", 65100, 65001, state="active"),
    )
    assert run("bgp_peer_mismatch", fabric) == []


def _ospf(node: str, area: str, kind: str) -> OspfNeighbor:
    return OspfNeighbor(
        node=node, ni="default", instance="main", area=area, interface="ethernet-1/1.0", interface_type=kind
    )


def _isis(node: str, interface: str = "ethernet-1/1.0", **more: Any) -> IsisAdjacency:
    return IsisAdjacency(node=node, ni="default", instance="i1", interface=interface, **more)


def _igp(isis: list[Any], ospf: list[Any]) -> Fabric:
    return fab({"lldp": _link_pair(), "isis": isis, "ospf": ospf})


def test_igp_peer_mismatch_is_silent_on_two_ends_that_match() -> None:
    ospf = [_ospf("leaf1", "0.0.0.0", "point-to-point"), _ospf("spine1", "0.0.0.0", "point-to-point")]
    assert run("igp_peer_mismatch", _igp([], ospf)) == []


def test_igp_peer_mismatch_finds_an_ospf_area_and_network_type_that_differ() -> None:
    ospf = [_ospf("leaf1", "0.0.0.0", "point-to-point"), _ospf("spine1", "0.0.0.1", "broadcast")]
    assert [(f.severity, f.detail) for f in run("igp_peer_mismatch", _igp([], ospf))] == [
        (ERROR, "OSPF area 0.0.0.0 here, 0.0.0.1 on spine1 ethernet-1/1.0: no adjacency forms"),
        (WARNING, "OSPF network type point-to-point here, broadcast on spine1 ethernet-1/1.0"),
    ]


def test_igp_peer_mismatch_finds_is_is_passive_or_typed_differently() -> None:
    isis = [
        _isis("leaf1", passive=True, circuit_type="point-to-point"),
        _isis("spine1", circuit_type="broadcast"),
    ]
    assert [(f.severity, f.subject, f.detail) for f in run("igp_peer_mismatch", _igp(isis, []))] == [
        (
            ERROR,
            "ethernet-1/1.0",
            "IS-IS network type point-to-point here, broadcast on spine1 ethernet-1/1.0",
        ),
        (WARNING, "ethernet-1/1.0", "IS-IS is passive on leaf1 only: no adjacency forms over this link"),
    ]


def test_igp_peer_mismatch_finds_the_igp_on_one_end_of_a_link_only() -> None:
    (finding,) = run("igp_peer_mismatch", _igp([_isis("leaf1"), _isis("spine1", "system0.0")], []))
    assert (finding.node, finding.detail) == (
        "leaf1",
        "IS-IS runs on this end only: spine1 ethernet-1/1 does not run it",
    )
    # A far end that runs no IS-IS at all is not one configured wrong on this link.
    assert run("igp_peer_mismatch", _igp([_isis("leaf1")], [])) == []

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

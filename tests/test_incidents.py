"""Findings grouped into incidents: one root cause and what follows from it.

Ported from the original fcli's tests/test_incidents.py, over fcling's records.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest

from fcli.checks import Finding
from fcli.fabric import Fabric
from fcli.incidents import correlate, findings_of
from fcli.inventory import Node
from fcli.reports.health import BfdSession
from fcli.reports.instances import NiInterface
from fcli.reports.interfaces import Subinterface
from fcli.reports.lldp import LldpNeighbor
from fcli.reports.routing import Route


def fab(
    reports: dict[str, Sequence[Any]] | None = None,
    nodes: Iterable[str] = (),
    unreachable: dict[str, str] | None = None,
    changes: Sequence[dict[str, Any]] = (),
) -> Fabric:
    """Every node named in *reports*, *nodes* and the unreachable - one that answered nothing included."""
    reports = reports or {}
    names = set(nodes) | {r.node for records in reports.values() for r in records} | set(unreachable or {})
    return Fabric.of([Node(n, n) for n in sorted(names)], reports, unreachable=unreachable, changes=changes)


def cabled(a: str, a_port: str, b: str, b_port: str) -> list[LldpNeighbor]:
    return [
        LldpNeighbor(node=a, interface=a_port, system_name=b, port_id=b_port),
        LldpNeighbor(node=b, interface=b_port, system_name=a, port_id=a_port),
    ]


def finding(check: str, node: str, subject: str, detail: str, severity: str = "error") -> Finding:
    return Finding(node=node, check=check, severity=severity, subject=subject, detail=detail)


def itf_down(node: str, subif: str) -> Finding:
    return finding("itf_down", node, subif, "admin enabled but oper down: port-down")


def flap(node: str, report: str, key: list[str], detail: str) -> Finding:
    """A flapping finding as the check words it: ``<report> <record key>``."""
    return finding("flapping", node, f"{report} {json.dumps(key, separators=(',', ':'))}", detail, "warning")


def test_a_link_down_takes_the_sessions_over_it_into_one_incident() -> None:
    findings = [
        itf_down("leaf1", "ethernet-1/1.0"),
        itf_down("spine1", "ethernet-1/1.0"),
        # a link-local session names the port it runs on
        finding("bgp_down", "spine1", "default/fe80::1%ethernet-1/1.0", "session is active"),
        # a BFD session carries it as a field
        finding("bfd_down", "leaf1", "default/fe80::2", "session is down"),
    ]
    fabric = fab(
        {
            "lldp": cabled("leaf1", "ethernet-1/1", "spine1", "ethernet-1/1"),
            "bfd": [
                BfdSession(
                    node="leaf1",
                    ni="default",
                    remote_address="fe80::2",
                    state="down",
                    interface="ethernet-1/1.0",
                )
            ],
        }
    )
    (incident,) = correlate(findings, fabric)
    assert incident.kind == "link"
    assert incident.root.check == "itf_down"
    assert incident.title == "leaf1 e1/1 <-> spine1 e1/1: interface down"
    assert len(incident.findings) == 4
    assert "1 BFD session down, 1 BGP session down" in incident.explanation


def test_a_link_lldp_has_lost_is_still_known_from_the_history() -> None:
    """A cable that goes down takes its LLDP adjacency with it."""
    lost = {
        "ts": 1.0,
        "node": "leaf1",
        "report": "lldp",
        "key": '["ethernet-1/1","aa:bb"]',
        "op": "remove",
        "before": {"interface": "ethernet-1/1", "system_name": "spine1", "port_id": "ethernet-1/1"},
        "after": None,
    }
    fabric = fab({"lldp": []}, nodes=["leaf1", "spine1"], changes=[lost])
    (incident,) = correlate(
        [itf_down("leaf1", "ethernet-1/1.0"), itf_down("spine1", "ethernet-1/1.0")], fabric
    )
    assert incident.kind == "link" and len(incident.findings) == 2


def test_two_ends_of_one_point_to_point_subnet_are_one_cable() -> None:
    def subif(node: str, name: str, ip: str) -> Subinterface:
        return Subinterface(node=node, interface=name.split(".")[0], name=name, oper="down", ipv4=(ip,))

    fabric = fab(
        {
            "subif": [
                subif("leaf1", "ethernet-1/49.0", "10.0.0.0/31"),
                subif("spine1", "ethernet-1/1.0", "10.0.0.1/31"),
            ]
        }
    )
    findings = [
        itf_down("leaf1", "ethernet-1/49.0"),
        itf_down("spine1", "ethernet-1/1.0"),
        # a numbered session is over the port whose subnet its peer is in
        finding("bgp_down", "leaf1", "default/10.0.0.1", "session is active"),
    ]
    (incident,) = correlate(findings, fabric)
    assert incident.kind == "link" and len(incident.findings) == 3


def test_a_node_that_answered_nothing_is_the_root_of_what_points_at_it() -> None:
    # leaf3 cannot say what it is cabled to; leaf1 can.
    fabric = fab(
        {
            "bgp-peers": [],
            "subif": [],
            "lldp": [
                LldpNeighbor(
                    node="leaf1", interface="ethernet-1/3", system_name="leaf3", port_id="ethernet-1/3"
                )
            ],
        },
        unreachable={"leaf3": "failed to connect"},
    )
    findings = [
        finding("node_unreachable", "leaf3", "gnmi", "nothing could be read: failed to connect"),
        finding("bgp_down", "leaf1", "default/fe80::3%ethernet-1/3.0", "session is active"),
    ]
    (incident,) = correlate(findings, fabric)
    assert incident.kind == "node" and incident.title == "leaf3 is unreachable"
    assert incident.root.check == "node_unreachable"
    assert [f.check for f in incident.related] == ["bgp_down"]


def _loopback(node: str, address: str) -> NiInterface:
    return NiInterface(
        node=node, ni="default", type="default", interface="system0.0", prefixes=(f"{address}/32",)
    )


def test_an_overlay_session_to_a_loopback_with_no_route_is_an_underlay_problem() -> None:
    fabric = fab(
        {
            "ni": [_loopback("leaf1", "192.0.2.11"), _loopback("leaf2", "192.0.2.12")],
            "ipv4-rib": [Route(node="leaf1", ni="default", prefix="192.0.2.11/32", type="host")],
        }
    )
    (incident,) = correlate([finding("bgp_down", "leaf1", "default/192.0.2.12", "session is active")], fabric)
    assert incident.kind == "underlay"
    assert incident.root.check == "underlay_unreachable"
    assert "192.0.2.12" in incident.root.detail


def test_both_ends_of_a_session_with_a_route_between_them_are_one_incident() -> None:
    fabric = fab(
        {
            "ni": [_loopback("leaf1", "192.0.2.11"), _loopback("leaf2", "192.0.2.12")],
            "ipv4-rib": [
                Route(node=n, ni="default", prefix="192.0.2.0/24", type="bgp") for n in ("leaf1", "leaf2")
            ],
        }
    )
    findings = [
        finding("bgp_down", "leaf1", "default/192.0.2.12", "session is active"),
        finding("bgp_down", "leaf2", "default/192.0.2.11", "session is active"),
    ]
    (incident,) = correlate(findings, fabric)
    assert incident.kind == "session"
    assert incident.title == "leaf1 <-> leaf2 (default): BGP session down"


def test_the_same_cause_in_many_places_folds_into_one_pattern() -> None:
    lldp: list[LldpNeighbor] = []
    findings = []
    for n in range(1, 5):
        leaf = f"leaf{n}"
        lldp += cabled(leaf, "ethernet-1/1", f"spine{n}", "ethernet-1/1")
        findings.append(
            finding(
                "bfd_down",
                leaf,
                f"default/fe80::{n}%ethernet-1/1.0",
                "session is down on ethernet-1/1.0, protecting BGP",
            )
        )
    (pattern,) = correlate(findings, fab({"lldp": lldp}))
    assert pattern.kind == "pattern"
    assert pattern.title == "BFD session down on 4 links"
    assert len(pattern.findings) == 4
    # Two are two places, not a pattern.
    assert [i.kind for i in correlate(findings[:2], fab({"lldp": lldp}))] == ["link", "link"]


def test_bgp_sessions_down_fold_whatever_state_or_peer_details_a_reading_catches() -> None:
    """A dynamic session refused for a wrong AS keeps retrying: one reading
    catches it active with no peer-group or AS learned yet, the next in
    opensent with both. Those are one cause, folded into one card every time."""
    details = (
        "session is active, peer-group -, AS ?",
        "session is opensent, peer-group fabric, AS 4200000002",
        "session is connect, peer-group fabric, AS ?",
        "session is openconfirm, peer-group fabric, AS 4200000004",
    )
    lldp: list[LldpNeighbor] = []
    findings = []
    for n, detail in enumerate(details, start=1):
        lldp += cabled("spine1", f"ethernet-1/{n}", f"leaf{n}", "ethernet-1/49")
        findings.append(finding("bgp_down", "spine1", f"default/fe80::{n}%ethernet-1/{n}.0", detail))
    (pattern,) = correlate(findings, fab({"lldp": lldp}))
    assert pattern.kind == "pattern"
    assert pattern.title == "BGP session down on 4 links"
    # Each session's own detail is still what its finding says.
    assert {f.detail for f in pattern.findings} == set(details)


def test_every_flap_folds_into_one_incident_whatever_flaps() -> None:
    """Sessions, a MAC and a port that keep changing are one thing to look at -
    the fabric is unsettled - however different each says it is. A flap that
    is not the root of its incident stays where it is."""
    lldp: list[LldpNeighbor] = []
    for n in range(1, 4):
        lldp += cabled("spine1", f"ethernet-1/{n}", f"leaf{n}", "ethernet-1/49")
    lldp += cabled("spine2", "ethernet-1/9", "leaf9", "ethernet-1/50")
    findings = [
        flap(
            "spine1",
            "bgp-peers",
            ["default", f"fe80::{n}%ethernet-1/{n}.0"],
            f"bgp changed {n + 2} times (established / active) in the last 10 minutes",
        )
        for n in range(1, 3)
    ] + [
        flap(
            "leaf3",
            "mac",
            ["macvrf-101", "aa:bb:cc:00:00:01"],
            "mac changed 3 times (lag1.101 / vxlan1.101) in the last 10 minutes",
        ),
        flap(
            "spine1",
            "subif",
            ["ethernet-1/3.0"],
            "interface changed 4 times (up / down) in the last 10 minutes",
        ),
        # Riding on a link that is down: that link is the incident.
        itf_down("spine2", "ethernet-1/9.0"),
        flap(
            "spine2",
            "subif",
            ["ethernet-1/9.0"],
            "interface changed 5 times (up / down) in the last 10 minutes",
        ),
    ]
    incidents = correlate(findings, fab({"lldp": lldp}))
    (folded,) = [i for i in incidents if i.root.check == "flapping"]
    assert folded.kind == "pattern" and folded.title.startswith("Flapping in ")
    assert all(f"{what}" in folded.explanation for what in ("2 bgp", "1 mac", "1 interface"))
    assert len([f for f in folded.findings if f.check == "flapping"]) == 4
    down = next(i for i in incidents if i.root.check == "itf_down")
    assert any(f.check == "flapping" for f in down.findings)
    # Nothing is lost by folding.
    assert sorted(findings_of(incidents), key=repr) == sorted(findings, key=repr)


def test_no_finding_is_lost_by_being_grouped() -> None:
    findings = [
        itf_down("leaf1", "ethernet-1/1.0"),
        finding("mtu_outlier", "leaf2", "ip-mtu", "1500 where 9000 is the norm", "warning"),
        finding("resource_high", "leaf2", "control A memory", "85% in use", "warning"),
    ]
    assert sorted(findings_of(correlate(findings, fab(nodes=["leaf1", "leaf2"]))), key=repr) == sorted(
        findings, key=repr
    )


def test_the_cli_prints_an_esi_as_it_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Rich would show ":ab:" and ":cd:" as emoji, and "[...]" as markup.
    from click.testing import CliRunner

    from fcli.cli import main
    from fcli.incidents import Incident

    esi = "01:ab:cd:00:00:00:00:00:00:01"
    root = finding("ethernet_segment", "leaf1", f"esi {esi}", "no peer in [mh-1]")
    incident = Incident(
        id="x",
        severity="error",
        kind="segment",
        title=f"segment {esi} alone",
        node="leaf1",
        nodes=("leaf1",),
        root=root,
        explanation=f"{esi} has one leaf",
    )
    monkeypatch.setattr("fcli.cli._fabric_view", lambda settings, at: None)
    monkeypatch.setattr("fcli.cli.run_checks", lambda fabric: [])
    monkeypatch.setattr("fcli.cli.correlate", lambda findings, fabric: [incident])
    inventory = tmp_path / "inv.yml"
    inventory.write_text("name: lab\nnodes: {leaf1: {address: 127.0.0.1}}\n")
    result = CliRunner().invoke(main, ["-c", str(inventory), "incidents", "-v"])
    assert result.exit_code == 1, result.output
    output = " ".join(result.output.split())
    assert f"segment {esi} alone" in output and f"{esi} has one leaf" in output
    assert f"esi {esi}: no peer in [mh-1]" in output

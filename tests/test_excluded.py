"""The management port and network-instance are in no report, no history and no check."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from fcli.checks import run_checks
from fcli.engine import Engine
from fcli.fabric import Fabric
from fcli.gnmi.client import Tls
from fcli.incidents import correlate
from fcli.inventory import Node
from fcli.report import NodeState, excluded, excluded_interface
from fcli.reports import REGISTRY
from fcli.reports.bgp import BgpPeer
from fcli.reports.layer2 import NeighborEntry
from fcli.reports.routing import Route
from fcli.store import AsyncStore
from fcli.tree import Tree

from .fake_gnmi import PASSWORD, USERNAME, FakeTarget


def _subinterface(index: int, oper: str, prefix: str) -> dict[str, Any]:
    return {
        "index": index,
        "type": "routed",
        "admin-state": "enable",
        "oper-state": oper,
        "ipv4": {"address": [{"ip-prefix": prefix}]},
    }


#: mgmt0.0 down - its DHCP lease lost, say - beside a fabric port that is up.
INTERFACES = [
    {
        "name": "mgmt0",
        "oper-state": "up",
        "statistics": {"in-octets": "100", "out-octets": "100"},
        "subinterface": [_subinterface(0, "down", "172.20.20.2/24")],
    },
    {
        "name": "ethernet-1/1",
        "oper-state": "up",
        "statistics": {"in-octets": "100", "out-octets": "100"},
        "subinterface": [_subinterface(0, "up", "10.0.0.1/31")],
    },
]
NETWORK_INSTANCES = [
    {"name": "mgmt", "type": "ip-vrf", "oper-state": "up", "interface": [{"name": "mgmt0.0"}]},
    {"name": "default", "type": "default", "oper-state": "up", "interface": [{"name": "ethernet-1/1.0"}]},
]
LLDP = {
    "interface": [
        {"name": "mgmt0", "neighbor": [{"id": "aa", "system-name": "lab-switch", "port-id": "ge0/1"}]},
        {"name": "ethernet-1/1", "neighbor": [{"id": "bb", "system-name": "spine1", "port-id": "e1-1"}]},
    ]
}


def _state() -> NodeState:
    tree = Tree()
    tree.update("/", {"interface": INTERFACES, "network-instance": NETWORK_INSTANCES})
    tree.update("/system/lldp", LLDP)
    # Its own previous sample: ifstats builds rates from two.
    return NodeState(name="leaf1", tree=tree, previous=tree, elapsed=1.0)


def test_which_names_are_excluded() -> None:
    assert excluded_interface("mgmt0")
    assert excluded_interface("mgmt0.0")
    assert not excluded_interface("ethernet-1/1.0")
    assert not excluded_interface("mgmt")  # the network-instance, not the port


def test_which_records_are_in_the_management_network_instance() -> None:
    assert excluded(Route(node="leaf1", ni="mgmt", prefix="0.0.0.0/0", type="dhcp"))
    assert excluded(BgpPeer(node="leaf1", ni="mgmt", peer="172.20.20.1"))
    assert not excluded(BgpPeer(node="leaf1", ni="default", peer="10.0.0.1"))
    # An entry is left out only when every network-instance it is in is.
    assert excluded(NeighborEntry(node="leaf1", interface="irb0.1", nis=("mgmt",), address="a"))
    assert not excluded(NeighborEntry(node="leaf1", interface="irb0.1", nis=("default", "mgmt"), address="a"))


@pytest.mark.parametrize("name", ["subif", "ni", "lldp", "ifstats", "services"])
def test_reports_leave_out_the_management_port(name: str) -> None:
    rows = REGISTRY[name].run(_state())
    assert rows, f"{name} built nothing to leave mgmt0 out of"
    assert not any("mgmt" in repr(r) for r in rows)


def test_checks_and_incidents_never_see_the_management_port() -> None:
    state = _state()
    records = {name: REGISTRY[name].run(state) for name in ("subif", "ni", "lldp", "ifstats")}
    records["subif"] = [
        r.model_copy(update={"oper": "down"}) if r.name == "ethernet-1/1.0" else r for r in records["subif"]
    ]
    fabric = Fabric.of([Node("leaf1", "leaf1")], records)
    findings = run_checks(fabric)
    assert [f.subject for f in findings if f.check == "itf_down"] == ["ethernet-1/1.0"]
    assert not any("mgmt0" in repr(f) for f in findings)
    assert not any("mgmt0" in repr(i) for i in correlate(findings, fabric))


SUBINTERFACES = "/interface[name=*]/subinterface"
SUBIF = "/interface[name={}]/subinterface[index=0]/oper-state"


async def _until(condition: Any, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


async def test_the_history_keeps_nothing_of_the_management_port(tmp_path: Path) -> None:
    data = {
        SUBINTERFACES: {
            "srl_nokia-interfaces:interface": [
                {"name": i["name"], "subinterface": i["subinterface"]} for i in INTERFACES
            ]
        }
    }
    with FakeTarget(data) as target:
        node = Node("leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True))
        store = AsyncStore(tmp_path / "h.db")
        eng = Engine([node], [REGISTRY["subif"]], store, settle=0.01)
        events: list[dict[str, Any]] = []
        eng.listen(events.append)
        task = asyncio.create_task(eng.run())
        try:
            await _until(lambda: [r.name for r in eng.current("subif")] == ["ethernet-1/1.0"])
            # mgmt0 coming up and the fabric port going down: only the second is news.
            target.push({SUBIF.format("mgmt0"): "up"})
            target.push({SUBIF.format("ethernet-1/1"): "down"})
            await _until(lambda: [f.subject for f in eng.current("checks")] == ["ethernet-1/1.0"])
            assert eng.store is not None
            kept = await eng.store.history()
            assert {h["report"] for h in kept} == {"subif", "checks"}
            assert not any("mgmt0" in repr(h) for h in kept)
            assert not any("mgmt0" in repr(e) for e in events)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            store.close()


NEIGHBORS = "/network-instance[name=*]/protocols/bgp/neighbor"
PEER = "/network-instance[name={}]/protocols/bgp/neighbor[peer-address={}]/session-state"


def _instance(name: str, peer: str) -> dict[str, Any]:
    neighbor = {"peer-address": peer, "session-state": "established", "peer-as": 65001}
    return {"name": name, "protocols": {"srl_nokia-bgp:bgp": {"neighbor": [neighbor]}}}


async def test_the_history_keeps_nothing_of_the_management_network_instance(tmp_path: Path) -> None:
    data = {
        NEIGHBORS: {
            "srl_nokia-network-instance:network-instance": [
                _instance("mgmt", "172.20.20.1"),
                _instance("default", "10.0.0.1"),
            ]
        }
    }
    with FakeTarget(data) as target:
        node = Node("leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True))
        store = AsyncStore(tmp_path / "h.db")
        eng = Engine([node], [REGISTRY["bgp-peers"]], store, settle=0.01)
        events: list[dict[str, Any]] = []
        eng.listen(events.append)
        task = asyncio.create_task(eng.run())
        try:
            await _until(lambda: [r.ni for r in eng.current("bgp-peers")] == ["default"])
            target.push({PEER.format("mgmt", "172.20.20.1"): "idle"})
            target.push({PEER.format("default", "10.0.0.1"): "idle"})
            await _until(lambda: [f.subject for f in eng.current("checks")] == ["default/10.0.0.1"])
            assert eng.store is not None
            kept = await eng.store.history()
            assert {h["report"] for h in kept} == {"bgp-peers", "checks"}
            assert not any("mgmt" in repr(h) for h in kept)
            assert not any("mgmt" in repr(e) for e in events)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            store.close()

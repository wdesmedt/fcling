"""The gNMI client, the collector and the CLI against an in-process target."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from fcli.cli import main
from fcli.collect import by_type, collect
from fcli.gnmi.client import AuthFailed, GnmiClient, Target, Tls, TlsFailed, Unreachable
from fcli.inventory import Node
from fcli.report import Read
from fcli.reports import REGISTRY

from .fake_gnmi import PASSWORD, USERNAME, FakeTarget, self_signed

DATA = {
    "/platform/chassis/type": {"srl_nokia-platform:platform": {"chassis": {"type": "7220 IXR-D2L"}}},
    "/platform/control[slot=A]/software-version": {
        "srl_nokia-platform:platform": {"control": [{"slot": "A", "software-version": "v26.7.2-1-gabc"}]}
    },
    "/network-instance[name=*]/protocols/bgp/neighbor": {
        "srl_nokia-network-instance:network-instance": [
            {
                "name": "default",
                "protocols": {
                    "srl_nokia-bgp:bgp": {
                        "neighbor": [
                            {
                                "peer-address": "10.0.0.1",
                                "session-state": "established",
                                "peer-as": 65001,
                                "afi-safi": [
                                    {
                                        "afi-safi-name": "srl_nokia-common:evpn",
                                        "admin-state": "enable",
                                        "received-routes": "4",
                                        "active-routes": "2",
                                        "sent-routes": "3",
                                    }
                                ],
                            },
                            {"peer-address": "10.0.0.2", "session-state": "active", "peer-as": 65002},
                        ]
                    }
                },
            }
        ]
    },
}


@pytest.fixture
def target() -> Iterator[FakeTarget]:
    with FakeTarget(DATA) as t:
        yield t


def _insecure(port: int, password: str = PASSWORD) -> Target:
    return Target("127.0.0.1", port, USERNAME, password, Tls(insecure=True))


async def test_capabilities_and_get(target: FakeTarget) -> None:
    async with GnmiClient(_insecure(target.port)) as client:
        assert client.capabilities is not None
        assert client.capabilities.encodings == ("json_ietf", "52")
        (n,) = await client.get(["/platform/chassis/type"])
    ((path, value),) = n.updates
    assert path == ()
    assert value["srl_nokia-platform:platform"]["chassis"]["type"] == "7220 IXR-D2L"


async def test_bad_credentials(target: FakeTarget) -> None:
    with pytest.raises(AuthFailed):
        async with GnmiClient(_insecure(target.port, password="wrong")):
            pass


async def test_nothing_listening() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(Unreachable, match="refused"):
        async with GnmiClient(_insecure(port)):
            pass


@pytest.fixture
def tls_target() -> Iterator[tuple[FakeTarget, bytes]]:
    cert = self_signed("leaf1.lab")
    with FakeTarget(DATA, cert) as t:
        yield t, cert.cert_pem


async def test_tls_trust_on_first_use(tls_target: tuple[FakeTarget, bytes]) -> None:
    """skip_verify: the presented certificate becomes the anchor, its name the one checked."""
    t, _ = tls_target
    async with GnmiClient(Target("127.0.0.1", t.port, USERNAME, PASSWORD)) as client:
        assert client.capabilities is not None


async def test_tls_verified(tls_target: tuple[FakeTarget, bytes], tmp_path: Path) -> None:
    t, pem = tls_target
    ca = tmp_path / "ca.pem"
    ca.write_bytes(pem)
    tls = Tls(ca_file=str(ca), server_name="leaf1.lab")
    async with GnmiClient(Target("127.0.0.1", t.port, USERNAME, PASSWORD, tls)) as client:
        assert client.capabilities is not None


async def test_tls_verified_against_the_wrong_anchor(
    tls_target: tuple[FakeTarget, bytes], tmp_path: Path
) -> None:
    t, _ = tls_target
    ca = tmp_path / "other.pem"
    ca.write_bytes(self_signed("elsewhere").cert_pem)
    tls = Tls(ca_file=str(ca), server_name="leaf1.lab")
    with pytest.raises(TlsFailed):
        async with GnmiClient(Target("127.0.0.1", t.port, USERNAME, PASSWORD, tls), rpc_timeout=5):
            pass


async def test_collect_keeps_what_the_node_knows(target: FakeTarget) -> None:
    """One unknown path rejects the Get; the rest is fetched path by path."""
    node = Node("leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True))
    report = REGISTRY["sys-info"]
    unknown = type(report.reads[0])("/does/not/exist")
    patched = type(report)(**{**report.__dict__, "reads": (*report.reads, unknown)})
    (result,) = await collect([node], [patched])
    assert result.error is None
    (info,) = result.records["sys-info"]
    assert (info.type, info.software_version) == ("7220 IXR-D2L", "26.7.2")
    assert any(len(g) > 1 for g in target.servicer.gets)  # tried together first


async def test_collect_reports_unreachable_nodes_on_their_own(target: FakeTarget) -> None:
    good = Node("leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True))
    bad = Node("leaf2", "127.0.0.1", target.port, USERNAME, "wrong", Tls(insecure=True))
    results = {r.node.name: r for r in await collect([good, bad], [REGISTRY["bgp-peers"]])}
    assert len(results["leaf1"].records["bgp-peers"]) == 2
    assert "credentials" in (results["leaf2"].error or "")


def _inventory(tmp_path: Path, port: int) -> Path:
    f = tmp_path / "inv.yml"
    f.write_text(
        f"defaults: {{username: {USERNAME}, password: {PASSWORD}, port: {port}, tls: {{insecure: true}}}}\n"
        "nodes: {leaf1: {address: 127.0.0.1, labels: {role: leaf}}}\n"
    )
    return f


def test_cli_json(target: FakeTarget, tmp_path: Path) -> None:
    inv = _inventory(tmp_path, target.port)
    result = CliRunner().invoke(main, ["-c", str(inv), "-o", "json", "bgp-peers", "-f", "state=up"])
    assert result.exit_code == 0, result.output
    (peer,) = json.loads(result.output)
    assert peer["peer"] == "10.0.0.1"
    assert peer["families"] == [
        {"name": "evpn", "enabled": True, "oper": "", "received": 4, "active": 2, "sent": 3}
    ]


def test_cli_csv_uses_table_cells(target: FakeTarget, tmp_path: Path) -> None:
    inv = _inventory(tmp_path, target.port)
    result = CliRunner().invoke(main, ["-c", str(inv), "-o", "csv", "bgp-peers", "-f", "peer=0.2$"])
    assert result.exit_code == 0, result.output
    header, row = result.output.splitlines()
    assert header.startswith("node,NI,peer,state,local-as,peer-as,local-address,evpn Rx/Act/Tx")
    assert row.startswith("leaf1,default,10.0.0.2,active,-,65002,,-,")


def test_cli_unknown_filter_column(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["-c", str(_inventory(tmp_path, 1)), "lldp", "-f", "nope=x"])
    assert result.exit_code == 2
    assert "has no column 'nope'" in result.output


def test_cli_help_needs_no_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["bgp-peers", "--help"])
    assert result.exit_code == 0
    assert "--field-filter" in result.output


def test_a_get_never_asks_for_a_list_both_whole_and_by_key() -> None:
    """SR Linux answers that with the list, brackets stripped, in a keyed entry's update."""
    attr = "/network-instance[name={}]/bgp-rib/attr-sets/attr-set"
    reads = [
        Read(attr.format("default") + "[index=7]"),
        Read(attr.format("*")),
        Read(attr.format("default") + "[index=9]"),
        Read("/interface[name=*]/statistics"),
        Read("/interface[name=mgmt0]/statistics/in-octets"),
        Read("/system/name"),
    ]
    (paths,) = by_type(reads).values()
    assert paths == [attr.format("*"), "/interface[name=*]/statistics", "/system/name"]
    keyed = [Read(attr.format("default") + f"[index={i}]") for i in range(3)]
    assert len(next(iter(by_type(keyed).values()))) == 3

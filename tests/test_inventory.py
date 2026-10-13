from pathlib import Path

import pytest

from fcli import inventory as inv

TOPO = """
name: lab
topology:
  kinds:
    custom: {image: ghcr.io/nokia/srlinux:26.7.2}
  defaults: {labels: {site: a}}
  groups:
    leaves: {kind: nokia_srlinux, labels: {role: leaf}}
  nodes:
    leaf1: {group: leaves}
    spine1: {kind: custom, labels: {role: spine}}
    host1: {kind: linux}
    bare:
"""


def test_clab_srl_nodes_only(tmp_path: Path) -> None:
    topo = tmp_path / "lab.clab.yml"
    topo.write_text(TOPO)
    fabric = inv.from_clab(topo)
    assert fabric.name == "lab"
    addresses = {n.name: n.address for n in fabric.nodes}
    assert addresses == {"leaf1": "clab-lab-leaf1", "spine1": "clab-lab-spine1"}
    assert dict(fabric.nodes[0].labels) == {"site": "a", "role": "leaf"}


@pytest.mark.parametrize(
    ("topo", "prefix"),
    [
        ({"name": "x"}, "clab-x-"),
        ({"name": "x", "prefix": ""}, ""),
        ({"name": "x", "prefix": "__lab-name"}, "x-"),
        ({"name": "x", "prefix": "p"}, "p-x-"),
    ],
)
def test_clab_prefix(topo: dict, prefix: str) -> None:
    assert inv.clab_prefix(topo) == prefix


def test_select_by_label_and_name(tmp_path: Path) -> None:
    topo = tmp_path / "lab.clab.yml"
    topo.write_text(TOPO)
    fabric = inv.from_clab(topo)
    assert [n.name for n in fabric.select(["role=LEAF"]).nodes] == ["leaf1"]
    assert [n.name for n in fabric.select(["name=s*"]).nodes] == ["spine1"]
    assert fabric.select(["role=leaf", "name=spine*"]).nodes == ()


def test_static_inventory(tmp_path: Path) -> None:
    f = tmp_path / "inv.yml"
    f.write_text("""
defaults: {username: u, password: p, port: 6030, tls: {insecure: true}, labels: {site: x}}
nodes:
  r1: {address: 10.0.0.1, labels: {role: leaf}}
  r2:
""")
    fabric = inv.from_file(f)
    r1, r2 = fabric.nodes
    assert (r1.address, r1.port, r1.username, r1.tls.insecure) == ("10.0.0.1", 6030, "u", True)
    assert dict(r1.labels) == {"site": "x", "role": "leaf"}
    assert r2.address == "r2"


def test_static_inventory_rejects_typos(tmp_path: Path) -> None:
    f = tmp_path / "inv.yml"
    f.write_text("nodes: {r1: {adress: 10.0.0.1}}")
    with pytest.raises(inv.InventoryError, match="adress"):
        inv.from_file(f)

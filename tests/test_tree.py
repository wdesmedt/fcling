from fcli.gnmi.client import Notification
from fcli.gnmi.path import parse
from fcli.tree import Tree


def test_wildcard_gets_merge_by_key() -> None:
    """Two wildcard Gets answer with the same interface list; entries line up by name."""
    tree = Tree()
    tree.update("/", {"srl_nokia-interfaces:interface": [{"name": "e1", "statistics": {"in-octets": "1"}}]})
    tree.update("/", {"interface": [{"name": "e1", "subinterface": [{"index": 0}]}, {"name": "e2"}]})
    assert [m.keys["interface"] for m in tree.select("/interface")] == ["e1", "e2"]
    assert tree.get("/interface[name=e1]/statistics/in-octets") == "1"
    assert tree.get("/interface[name=e1]/subinterface[index=0]/index") == 0


def test_identity_values_lose_their_module() -> None:
    tree = Tree()
    tree.update("/interface[name=e1]/subinterface[index=0]", {"type": "srl_nokia-interfaces:routed"})
    assert tree.get("/interface[name=e1]/subinterface[index=0]/type") == "routed"


def test_leaf_updates_create_keyed_entries() -> None:
    """What a subscription sends: one leaf at a time, keys in the path."""
    tree = Tree()
    tree.update(
        "/network-instance[name=default]/protocols/bgp/neighbor[peer-address=10.0.0.1]/session-state",
        "established",
    )
    tree.update(
        "/network-instance[name=default]/protocols/bgp/neighbor[peer-address=10.0.0.2]/session-state",
        "active",
    )
    states = {
        m.keys["neighbor"]: m.value["session-state"]
        for m in tree.select("/network-instance[name=*]/protocols/bgp/neighbor")
    }
    assert states == {"10.0.0.1": "established", "10.0.0.2": "active"}


def test_select_names_keys_of_unkeyed_list_elements() -> None:
    """A path without keys still says which entry each match came from."""
    tree = Tree()
    tree.update(
        "/",
        {
            "network-instance": [
                {"name": "default", "protocols": {"bgp": {"neighbor": [{"peer-address": "10.0.0.1"}]}}}
            ]
        },
    )
    (match,) = tree.select("/network-instance/protocols/bgp/neighbor")
    assert match.keys == {"network-instance": "default", "neighbor": "10.0.0.1"}


def test_unknown_lists_are_replaced_whole() -> None:
    tree = Tree()
    tree.update("/a", {"things": [{"x": 1}]})
    tree.update("/a", {"things": [{"x": 2}]})
    assert [m.value for m in tree.select("/a/things")] == [{"x": 2}]


def test_leaf_lists_are_replaced() -> None:
    tree = Tree()
    tree.update("/n[name=a]/import-policy", ["p1", "p2"])
    tree.update("/n[name=a]/import-policy", ["p3"])
    assert tree.get("/n[name=a]/import-policy") == ["p3"]


def test_delete_entry_and_container() -> None:
    tree = Tree()
    tree.update("/", {"interface": [{"name": "e1", "description": "x"}, {"name": "e2"}]})
    tree.apply(Notification(timestamp=0, deletes=(parse("/interface[name=e1]"),)))
    assert [m.keys["interface"] for m in tree.select("/interface")] == ["e2"]
    tree.delete("/interface[name=e2]/description")  # absent: nothing to do
    tree.delete("/interface")
    assert tree.get("/interface") is None


def test_one_entry_list_unwrapped_to_a_dict() -> None:
    tree = Tree()
    tree.update("/", {"interface": {"name": "e1"}})
    tree.update("/interface[name=e1]/mtu", 9000)
    assert tree.get("/interface[name=e1]/mtu") == 9000


def test_a_repeating_leaf_is_not_taken_for_the_key() -> None:
    """Two paths to one prefix are two entries, not one to merge."""
    tree = Tree()
    tree.update(
        "/a", {"routes": [{"prefix": "10.0.0.0/8", "peer": "p1"}, {"prefix": "10.0.0.0/8", "peer": "p2"}]}
    )
    tree.update("/a", {"routes": [{"prefix": "10.0.0.0/8", "peer": "p3"}]})
    assert [m.value["peer"] for m in tree.select("/a/routes")] == ["p3"]


def test_bgp_rib_routes_merge_on_all_their_keys() -> None:
    tree = Tree()
    rib = "/network-instance[name=default]/bgp-rib/afi-safi[afi-safi-name=evpn]/evpn/rib-in-out/rib-in-post"
    tree.update(
        rib,
        {"ip-prefix-route": [{"ip-prefix": "10.0.0.0/24", "neighbor": "n1", "route-distinguisher": "1:1"}]},
    )
    tree.update(
        rib,
        {
            "ip-prefix-route": [
                {"ip-prefix": "10.0.0.0/24", "neighbor": "n2", "route-distinguisher": "1:1"},
                {
                    "ip-prefix": "10.0.0.0/24",
                    "neighbor": "n1",
                    "route-distinguisher": "1:1",
                    "used-route": True,
                },
            ]
        },
    )
    routes = {m.value["neighbor"]: m.value for m in tree.select(f"{rib}/ip-prefix-route")}
    assert set(routes) == {"n1", "n2"}
    assert routes["n1"]["used-route"] is True


def test_prune_removes_every_match() -> None:
    tree = Tree()
    tree.update("/", {"interface": [{"name": "e1", "statistics": {"in": "1"}, "mtu": 1}, {"name": "e2"}]})
    tree.prune("/interface[name=*]/statistics")
    assert tree.get("/interface[name=e1]/statistics") is None
    assert tree.get("/interface[name=e1]/mtu") == 1
    tree.prune("/interface[name=e2]")
    assert [m.keys["interface"] for m in tree.select("/interface")] == ["e1"]


def test_keyed_updates_find_entries_after_a_merge() -> None:
    """The index follows the list through merges and replacements."""
    tree = Tree()
    tree.update("/interface[name=e1]/mtu", 1)
    tree.update("/", {"interface": [{"name": "e2", "mtu": 2}]})
    tree.update("/interface[name=e2]/mtu", 3)
    tree.update("/", {"interface": [{"name": "e3"}]})
    tree.update("/interface[name=e3]/mtu", 4)
    assert {m.keys["interface"]: m.value.get("mtu") for m in tree.select("/interface")} == {
        "e1": 1,
        "e2": 3,
        "e3": 4,
    }

"""BGP RIB report: attribute sets are looked up by key, not scanned whole."""

from fcli.report import NodeState
from fcli.reports import REGISTRY
from fcli.reports.bgp_rib import (
    EVPN_ALL_REPORT,
    EVPN_ROUTES,
    _attr_sets,
    _yield_bgp_routes,
    evpn_rib_requested,
    parse_rib_report,
    reads_for_evpn_all,
    reads_for_rib,
    rib_requested,
)
from fcli.reports.routing import ATTR_KEYED_LIMIT
from fcli.tree import Tree


def test_attr_sets_are_fetched_by_index_not_whole_table() -> None:
    tree = Tree()
    tree.update(
        "/network-instance[name=default]/bgp-rib/attr-sets/attr-set[index=1]",
        {"index": 1, "next-hop": "10.0.0.1", "origin": "igp"},
    )
    tree.update(
        "/network-instance[name=default]/bgp-rib/attr-sets/attr-set[index=9999]",
        {"index": 9999, "next-hop": "10.0.0.99"},
    )
    found = _attr_sets(tree, {("default", "1")})
    assert found[("default", "1")]["next-hop"] == "10.0.0.1"
    assert ("default", "9999") not in found


def test_rib_reads_nothing_until_asked() -> None:
    assert reads_for_rib("evpn", "2", {"paths": "used"}) == []
    assert rib_requested("evpn", "2", {"paths": "used"}) is False


def test_rib_reads_by_key() -> None:
    reads = reads_for_rib(
        "evpn",
        "2",
        {"mac_address": "1a:a4:*", "paths": "used"},
    )
    assert len(reads) == 1
    assert "mac-address=1a:a4:*" in reads[0].path
    assert "network-instance[name=default]" in reads[0].path
    assert reads_for_rib("evpn", "2", {"list_all": "all"})[0].path.endswith("mac-ip-route")


def test_rib_ni_only_counts_for_non_evpn() -> None:
    assert not rib_requested("evpn", "2", {"ni": "mv-dci"})
    assert rib_requested("ipv4-unicast", "", {"ni": "mv-dci"})


def test_neighbor_opens_rib_out_for_advertised() -> None:
    reads = reads_for_rib(
        "ipv4-unicast",
        "",
        {"neighbor": "10.0.0.2", "direction": "advertised"},
    )
    assert len(reads) == 1
    assert "rib-out-post" in reads[0].path
    assert "neighbor=10.0.0.2" in reads[0].path


def test_evpn_neighbor_uses_keyed_get_on_one_container() -> None:
    reads = reads_for_rib("evpn", "2", {"neighbor": "10.0.0.2", "direction": "received"})
    assert len(reads) == 1
    assert reads[0].path.endswith("mac-ip-route[neighbor=10.0.0.2]")


def test_evpn_all_peer_query_reads_each_container_by_peer() -> None:
    assert parse_rib_report(EVPN_ALL_REPORT) == ("evpn", "")
    assert evpn_rib_requested({"neighbor": "10.0.0.2"})
    reads = reads_for_evpn_all({"neighbor": "10.0.0.2", "direction": "received"})
    assert len(reads) == len(EVPN_ROUTES)
    assert all(r.path.endswith("[neighbor=10.0.0.2]") for r in reads)


def test_evpn_all_lists_every_route_in_one_read() -> None:
    reads = reads_for_evpn_all({"list_all": "all"})
    assert len(reads) == 1
    assert reads[0].path.endswith("rib-in-post")


def _evpn_rib(routes: dict[str, int]) -> Tree:
    """A RIB with *routes[container]* used routes, each naming its own attr-set."""
    rib = "/network-instance[name=default]/bgp-rib/afi-safi[afi-safi-name=evpn]/evpn/rib-in-out/rib-in-post"
    tree = Tree()
    attr = iter(range(1, 1_000_000))
    tree.update(
        rib,
        {
            container: [
                {
                    "route-distinguisher": f"1:{i}",
                    "neighbor": "10.0.0.5",
                    "used-route": True,
                    "attr-id": str(next(attr)),
                }
                for i in range(count)
            ]
            for container, count in routes.items()
        },
    )
    return tree


def test_evpn_all_attr_sets_are_read_one_way() -> None:
    """Never the whole attr-set list and keyed entries of it in one Get: SR Linux
    answers that with bracketless JSON."""
    report = REGISTRY[EVPN_ALL_REPORT]
    params = {"neighbor": "10.0.0.5", "direction": "received", "paths": "all"}
    tree = _evpn_rib({"mac-ip-route": ATTR_KEYED_LIMIT + 1, "ethernet-segment-route": 3})
    reads = list(report.follow(tree, params))
    assert [r.path for r in reads] == ["/network-instance[name=*]/bgp-rib/attr-sets/attr-set"]
    few = list(report.follow(_evpn_rib({"mac-ip-route": 2, "ethernet-segment-route": 3}), params))
    assert len(few) == 5
    assert all("attr-set[index=" in r.path for r in few)


def test_yield_bgp_routes_matches_neighbor_from_list_keys() -> None:
    rib = "/network-instance[name=default]/bgp-rib/afi-safi[afi-safi-name=evpn]/evpn/rib-in-out/rib-in-post"
    tree = Tree()
    tree.update(
        rib,
        {
            "mac-ip-route": [
                {
                    "mac-address": "aa:bb:cc:dd:ee:ff",
                    "neighbor": "10.0.0.5",
                    "route-distinguisher": "1:1",
                    "used-route": True,
                }
            ],
            "ip-prefix-route": [
                {
                    "ip-prefix": "10.1.0.0/24",
                    "neighbor": "10.0.0.5",
                    "route-distinguisher": "1:1",
                    "used-route": True,
                }
            ],
        },
    )
    state = NodeState(name="leaf1", tree=tree, models={}, labels={})
    routes = list(_yield_bgp_routes(state, "evpn", "2", "all", "received", "10.0.0.5"))
    assert len(routes) == 1
    assert routes[0].route_type == "2"
    routes5 = list(_yield_bgp_routes(state, "evpn", "5", "all", "received", "10.0.0.5"))
    assert len(routes5) == 1


def test_evpn_all_route_type_reads_and_shows_one_type() -> None:
    reads = reads_for_evpn_all({"neighbor": "10.0.0.5", "route_type": "3"})
    assert [r.path.rsplit("/", 1)[1] for r in reads] == ["imet-route[neighbor=10.0.0.5]"]
    (whole,) = reads_for_evpn_all({"list_all": "all", "route_type": "5"})
    assert whole.path.endswith("rib-in-post/ip-prefix-route")
    report = REGISTRY[EVPN_ALL_REPORT]
    params = {"neighbor": "10.0.0.5", "paths": "all", "route_type": "4"}
    tree = _evpn_rib({"mac-ip-route": 2, "ethernet-segment-route": 3})
    assert len(list(report.follow(tree, params))) == 3
    state = NodeState(name="leaf1", tree=tree, models={}, labels={})
    rows = list(report.build(state, **report.param_values(params)))
    assert {r.route_type for r in rows} == {"4"}
    assert len(list(report.build(state, **report.param_values({**params, "route_type": ""})))) == 5

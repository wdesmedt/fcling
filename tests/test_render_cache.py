from fcli.render import table_rows
from fcli.reports import REGISTRY
from fcli.reports.bgp import BgpPeer, Family


def test_table_rows_reuses_unchanged_nodes() -> None:
    report = REGISTRY["bgp-peers"]
    cache: dict[tuple[str, str], tuple[str, list[list[str]]]] = {}
    a = BgpPeer(node="leaf1", ni="default", peer="10.0.0.1", state="established")
    b = BgpPeer(node="leaf2", ni="default", peer="10.0.0.2", state="established")
    _, rows1 = table_rows(report, [a, b], cache=cache, cache_key="bgp-peers:")
    c = BgpPeer(node="leaf2", ni="default", peer="10.0.0.2", state="active")
    _, rows2 = table_rows(report, [a, c], cache=cache, cache_key="bgp-peers:")
    assert rows1[0] == rows2[0]
    assert rows1[1] != rows2[1]


def test_table_rows_refreshes_bgp_route_counters() -> None:
    report = REGISTRY["bgp-peers"]
    cache: dict[tuple[str, str], tuple[str, list[list[str]]]] = {}
    family = Family(name="ipv4-unicast", enabled=True, oper="up", received=8, active=7, sent=2)
    peer = BgpPeer(node="leaf1", ni="default", peer="10.0.0.2", state="established", families=(family,))
    _, rows1 = table_rows(report, [peer], cache=cache, cache_key="bgp-peers:")
    peer2 = peer.model_copy(
        update={"families": (family.model_copy(update={"received": 9, "active": 8, "sent": 3}),)}
    )
    _, rows2 = table_rows(report, [peer2], cache=cache, cache_key="bgp-peers:")
    ipv4 = 8  # evpn + ipv4-unicast family columns after local-address
    assert rows1[0][ipv4]["rx"] == 8
    assert rows2[0][ipv4]["rx"] == 9

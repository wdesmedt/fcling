import pytest

from fcli.gnmi import path as gp
from fcli.gnmi.path import Elem


def test_parse_keys_with_slashes_and_prefixes() -> None:
    p = gp.parse("/srl_nokia-interfaces:interface[name=ethernet-1/1]/subinterface[index=0]")
    assert p == (Elem("interface", (("name", "ethernet-1/1"),)), Elem("subinterface", (("index", "0"),)))
    assert gp.format(p) == "/interface[name=ethernet-1/1]/subinterface[index=0]"


def test_escaped_bracket_round_trips() -> None:
    p = gp.parse(r"/a[k=x\]y]/b")
    assert p[0].keys == (("k", "x]y"),)
    assert gp.parse(gp.format(p)) == p


def test_root_is_empty() -> None:
    assert gp.parse("/") == ()
    assert gp.parse("") == ()


@pytest.mark.parametrize("bad", ["/a[k]", "/a[k=v"])
def test_malformed(bad: str) -> None:
    with pytest.raises(gp.PathError):
        gp.parse(bad)


def test_proto_round_trip() -> None:
    p = gp.parse("/network-instance[name=default]/protocols/bgp/neighbor[peer-address=10.0.0.1]")
    assert gp.from_proto(gp.to_proto(p)) == p


def test_matches_wildcards() -> None:
    pattern = gp.parse("/interface[name=*]/statistics")
    assert gp.matches(pattern, gp.parse("/interface[name=mgmt0]/statistics/in-octets"))
    assert not gp.matches(pattern, gp.parse("/interface[name=mgmt0]/subinterface"))
    assert gp.matches(gp.parse("/interface/statistics"), gp.parse("/interface[name=e1]/statistics"))

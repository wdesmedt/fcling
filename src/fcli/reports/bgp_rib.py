"""bgp-rib: one report per family and EVPN route type.

A RIB holds a path per peer for every NLRI - every route twice, on a fabric
with two route reflectors - and a route's attributes live apart from it, in
an attribute set it names by index. Sets are kept for nearly every path a
node holds, tens of thousands on an EVPN fabric, so only the ones the routes
shown name are read, by key.

Each family is a report of its own, with its own paths and columns; ``fcling
bgp-rib -r evpn -t 2`` picks one. Releases from 24.10 on lay the RIB out
under an ``afi-safi`` list entry, with the route containers in the singular.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any, ClassVar

from ..report import Column, NodeState, Param, Read, Record, as_int, as_list, dig, report
from ..tree import Tree
from .common import joined
from .routing import keyed

#: What the CLI calls a family, and what the model does.
FAMILIES = {
    "evpn": "evpn",
    "ipv4": "ipv4-unicast",
    "ipv4-unicast": "ipv4-unicast",
    "ipv6": "ipv6-unicast",
    "ipv6-unicast": "ipv6-unicast",
    "l3vpn-v4": "l3vpn-ipv4-unicast",
    "l3vpn-ipv4": "l3vpn-ipv4-unicast",
    "l3vpn-ipv4-unicast": "l3vpn-ipv4-unicast",
    "l3vpn-v6": "l3vpn-ipv6-unicast",
    "l3vpn-ipv6": "l3vpn-ipv6-unicast",
    "l3vpn-ipv6-unicast": "l3vpn-ipv6-unicast",
}

EVPN_ROUTES = {
    "1": "ethernet-ad-route",
    "2": "mac-ip-route",
    "3": "imet-route",
    "4": "ethernet-segment-route",
    "5": "ip-prefix-route",
}


def report_name(family: str, route_type: str | None = None) -> str:
    """``bgp-rib-evpn-2``, ``bgp-rib-ipv4-unicast``: the report for a family."""
    model = FAMILIES[family]
    return f"bgp-rib-{model}-{route_type or '2'}" if model == "evpn" else f"bgp-rib-{model}"


class BgpRoute(Record):
    """One path in a BGP RIB, with the attributes it was received with."""

    key: ClassVar[tuple[str, ...]] = (
        "ni",
        "rd",
        "esi",
        "tag",
        "mac",
        "ip",
        "prefix",
        "originator",
        "neighbor",
    )

    ni: str
    family: str
    #: EVPN: ``1`` to ``5``.
    route_type: str = ""
    #: The peer it came from; a locally originated route names ``0.0.0.0``.
    neighbor: str = ""
    used: bool = False
    valid: bool = False
    best: bool = False
    rd: str = ""
    prefix: str = ""
    esi: str = ""
    tag: int | None = None
    mac: str = ""
    ip: str = ""
    originator: str = ""
    gateway: str = ""
    #: The VNI it is advertised with: a MAC/IP route's first label, an RT-5's label.
    vni: int | None = None
    label1: int | None = None
    label2: int | None = None
    next_hop: str = ""
    origin: str = ""
    local_pref: int | None = None
    med: int | None = None
    as_path: tuple[int, ...] = ()
    #: Read out of the extended communities.
    route_targets: tuple[str, ...] = ()
    esi_labels: tuple[str, ...] = ()
    soo: tuple[str, ...] = ()
    tunnel_encap: tuple[str, ...] = ()
    communities: tuple[str, ...] = ()
    large_communities: tuple[str, ...] = ()
    ext_communities: tuple[str, ...] = ()
    #: The D-PATH domain ids, in order.
    domain_path: tuple[str, ...] = ()
    tie_break: str = ""
    internal_tags: tuple[str, ...] = ()
    neighbor_as: int | None = None


def _ext(communities: list[str], prefix: str) -> tuple[str, ...]:
    return tuple(c.split(prefix, 1)[1] for c in communities if prefix in c)


def _domain_ids(obj: Any) -> list[str]:
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "domain-id":
                found.extend(str(v) for v in as_list(value))
            else:
                found.extend(_domain_ids(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_domain_ids(item))
    return found


def _label(route: dict[str, Any], key: str) -> int | None:
    label = route.get(key)
    return as_int(label.get("value")) if isinstance(label, dict) else None


def _route(node: str, ni: str, family: str, route_type: str, route: dict[str, Any]) -> BgpRoute:
    communities = route.get("communities") or {}
    ext = [str(c) for c in as_list(communities.get("ext-community"))]
    label1, label2 = _label(route, "label1"), _label(route, "label2")
    # A MAC/IP route carries its VNI as the first of two labels, an RT-5 as its one label.
    vni = (
        label1
        if "label1" in route
        else _label(route, "label")
        if "label" in route
        else as_int(route.get("vni"))
    )
    return BgpRoute(
        node=node,
        ni=ni,
        family=family,
        route_type=route_type,
        neighbor=str(route.get("neighbor") or ""),
        used=bool(route.get("used-route")),
        valid=bool(route.get("valid-route")),
        best=bool(route.get("best-route")),
        rd=str(route.get("route-distinguisher") or ""),
        prefix=str(route.get("prefix") or route.get("ip-prefix") or ""),
        esi=str(route.get("esi") or ""),
        tag=as_int(route.get("ethernet-tag-id")),
        mac=str(route.get("mac-address") or ""),
        ip=str(route.get("ip-address") or ""),
        originator=str(route.get("originating-router") or ""),
        gateway=str(route.get("gateway-ip") or ""),
        vni=vni,
        label1=label1,
        label2=label2,
        next_hop=str(route.get("next-hop") or ""),
        origin=str(route.get("origin") or ""),
        local_pref=as_int(route.get("local-pref")),
        med=as_int(route.get("med")),
        as_path=tuple(
            member
            for segment in as_list(dig(route, "as-path", "segment"))
            if isinstance(segment, dict)
            for member in (as_int(m) for m in as_list(segment.get("member")))
            if member is not None
        ),
        route_targets=_ext(ext, "target:"),
        esi_labels=_ext(ext, "esi-label:"),
        soo=_ext(ext, "origin:"),
        tunnel_encap=_ext(ext, "bgp-tunnel-encap:"),
        communities=tuple(str(c) for c in as_list(communities.get("community"))),
        large_communities=tuple(str(c) for c in as_list(communities.get("large-community"))),
        ext_communities=tuple(ext),
        domain_path=tuple(_domain_ids(route.get("domain-path") or {})),
        tie_break=str(route.get("tie-break-reason") or ""),
        internal_tags=tuple(str(t) for t in as_list(route.get("internal-tags"))),
        neighbor_as=as_int(route.get("neighbor-as")),
    )


def _dash(value: Any) -> Any:
    return "-" if value is None else value


_COLUMNS: dict[str, Column] = {
    c.title: c
    for c in (
        Column("st", lambda r: ("u" if r.used else "") + ("*" if r.valid else "") + (">" if r.best else "")),
        Column("ESI", "esi"),
        Column("GW", "gateway"),
        Column("IP", "ip"),
        Column("IP-Pfx", "prefix"),
        Column("Pfx", "prefix"),
        Column("Prefix", "prefix"),
        Column("L1", lambda r: _dash(r.label1)),
        Column("L2", lambda r: _dash(r.label2)),
        Column("MAC", "mac"),
        Column("RD", "rd"),
        Column("RT", lambda r: joined(r.route_targets)),
        Column("Tag", "tag"),
        Column("as-path", lambda r: list(r.as_path)),
        Column("communities", lambda r: joined([*r.communities, *r.large_communities, *r.ext_communities])),
        Column(
            "esi-lbl",
            lambda r: ",".join(
                lbl.replace("Single-Active", "S-A").replace("All-Active", "A-A") for lbl in r.esi_labels
            ),
        ),
        Column("lpref", "local_pref"),
        Column("med", "med"),
        Column("neighbor", "neighbor"),
        Column("next-hop", "next_hop"),
        Column("NextHop", "next_hop"),
        Column("origin", "origin"),
        Column("originator", "originator"),
        Column("peer", "neighbor"),
        Column("vni", lambda r: _dash(r.vni)),
    )
}

_LAYOUT: dict[tuple[str, str], tuple[str, ...]] = {
    ("evpn", "1"): (
        "st",
        "ESI",
        "NextHop",
        "RD",
        "RT",
        "Tag",
        "as-path",
        "communities",
        "esi-lbl",
        "peer",
        "vni",
    ),
    ("evpn", "2"): (
        "st",
        "ESI",
        "IP",
        "L1",
        "L2",
        "MAC",
        "RD",
        "RT",
        "as-path",
        "communities",
        "next-hop",
        "peer",
        "vni",
    ),
    ("evpn", "3"): ("st", "RD", "RT", "Tag", "as-path", "communities", "next-hop", "origin", "peer"),
    ("evpn", "4"): ("st", "ESI", "RD", "RT", "as-path", "communities", "next-hop", "origin", "peer"),
    ("evpn", "5"): (
        "st",
        "ESI",
        "GW",
        "IP-Pfx",
        "RD",
        "RT",
        "as-path",
        "communities",
        "lpref",
        "med",
        "next-hop",
        "origin",
        "peer",
        "vni",
    ),
    ("ipv4-unicast", ""): ("st", "Prefix", "as-path", "communities", "lpref", "med", "neighbor", "next-hop"),
    ("ipv6-unicast", ""): ("st", "Prefix", "as-path", "communities", "lpref", "med", "neighbor", "next-hop"),
    ("l3vpn-ipv4-unicast", ""): (
        "st",
        "Pfx",
        "RD",
        "as-path",
        "communities",
        "lpref",
        "med",
        "neighbor",
        "next-hop",
    ),
    ("l3vpn-ipv6-unicast", ""): (
        "st",
        "Pfx",
        "RD",
        "as-path",
        "communities",
        "lpref",
        "med",
        "neighbor",
        "next-hop",
    ),
}

DETAIL_COLUMNS = (
    Column("soo", lambda r: joined(r.soo)),
    Column("tunnel-encap", lambda r: joined(r.tunnel_encap)),
    Column("dpath", lambda r: " ".join(r.domain_path)),
    Column("valid", "valid"),
    Column("best", "best"),
    Column("used", "used"),
    Column("tie-break", "tie_break"),
    Column("internal-tags", lambda r: list(r.internal_tags)),
    Column("neighbor-as", "neighbor_as"),
)

PATHS = Param(
    "paths",
    "used: the paths the node uses or picked as best; all: every path received",
    default="used",
    choices=("used", "all"),
)


def _register(family: str, route_type: str) -> None:
    under = f"bgp-rib/afi-safi[afi-safi-name={family}]/{family}"
    if family == "evpn":
        steps = f"rib-in-out/rib-in-post/{EVPN_ROUTES[route_type]}"
        title = f"BGP RIB, EVPN route type {route_type}"
    else:
        steps = "local-rib/route"
        title = f"BGP RIB, {family}"
    path = f"/network-instance[name=*]/{under}/{steps}"
    select = f"/network-instance/bgp-rib/afi-safi/{family}/{steps}"

    def wanted(route: Any, paths: str) -> bool:
        # A route reflector uses none of what it reflects, so best counts as used.
        return isinstance(route, dict) and (
            paths == "all" or route.get("used-route") is True or route.get("best-route") is True
        )

    def follow(tree: Tree, params: Mapping[str, Any]) -> Iterable[Read]:
        paths = params.get("paths") or "used"
        sets = {
            (str(m.keys.get("network-instance")), str(m.value["attr-id"]))
            for m in tree.select(select)
            if wanted(m.value, paths) and m.value.get("attr-id") is not None
        }
        return keyed("bgp-rib/attr-sets/attr-set", sets)

    def build(node: NodeState, paths: str) -> Iterator[BgpRoute]:
        attr_sets = {
            (str(m.keys.get("network-instance")), str(m.value.get("index"))): m.value
            for m in node.tree.select("/network-instance/bgp-rib/attr-sets/attr-set")
            if isinstance(m.value, dict)
        }
        for m in node.tree.select(select):
            route = m.value
            if not wanted(route, paths):
                continue
            ni = str(m.keys.get("network-instance"))
            # A route can name a set that is gone by the time it is read.
            merged = {**route, **attr_sets.get((ni, str(route.get("attr-id"))), {})}
            yield _route(node.name, ni, family, route_type, merged)

    layout = _LAYOUT[(family, route_type)]
    report(
        report_name(family, route_type or None),
        title,
        record=BgpRoute,
        reads=[Read(path)],
        follow=follow,
        params=[PATHS],
        columns=[Column("NI", "ni"), *(_COLUMNS[name] for name in layout)],
        detail_columns=DETAIL_COLUMNS,
        hidden=True,
    )(build)


for _type in EVPN_ROUTES:
    _register("evpn", _type)
for _family in ("ipv4-unicast", "ipv6-unicast", "l3vpn-ipv4-unicast", "l3vpn-ipv6-unicast"):
    _register(_family, "")

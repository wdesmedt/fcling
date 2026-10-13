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

import ipaddress
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, ClassVar

from ..report import Column, NodeState, Param, Read, Record, as_int, as_list, dig, report
from ..tree import Tree
from .common import joined
from .routing import ATTR_KEYED_LIMIT, keyed

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

#: YANG list keys per RIB, in the order a lookup is usually narrowed.
RIB_KEYS: dict[tuple[str, str], tuple[str, ...]] = {
    ("evpn", "1"): ("esi", "route-distinguisher", "ethernet-tag-id", "neighbor"),
    ("evpn", "2"): ("mac-address", "ip-address", "route-distinguisher", "ethernet-tag-id", "neighbor"),
    ("evpn", "3"): ("originating-router", "route-distinguisher", "ethernet-tag-id", "neighbor"),
    ("evpn", "4"): ("esi", "originating-router", "route-distinguisher", "neighbor"),
    ("evpn", "5"): ("ip-prefix", "route-distinguisher", "ethernet-tag-id", "neighbor"),
    ("ipv4-unicast", ""): ("prefix", "neighbor", "origin-protocol"),
    ("ipv6-unicast", ""): ("prefix", "neighbor", "origin-protocol"),
    ("l3vpn-ipv4-unicast", ""): ("prefix", "route-distinguisher", "neighbor"),
    ("l3vpn-ipv6-unicast", ""): ("prefix", "route-distinguisher", "neighbor"),
}

_RIB_KEY_HELP: dict[str, str] = {
    "mac-address": "MAC; gNMI key match, * allowed",
    "ip-address": "host IP",
    "route-distinguisher": "RD; e.g. 10.0.1.2:*",
    "esi": "ESI",
    "ethernet-tag-id": "EVPN tag",
    "neighbor": "BGP peer address",
    "originating-router": "originator",
    "ip-prefix": "prefix, e.g. 10.1.0.0/24",
    "prefix": "prefix, e.g. 10.0.0.0/8",
    "origin-protocol": "e.g. bgp",
}


def _param_name(yang_key: str) -> str:
    return yang_key.replace("-", "_")


def _yang_key(param: str) -> str:
    return param.replace("_", "-")


def rib_key_params(family: str, route_type: str) -> tuple[Param, ...]:
    if family == "evpn" and not route_type:
        seen: dict[str, Param] = {}
        for rt in EVPN_ROUTES:
            for param in rib_key_params(family, rt):
                seen.setdefault(param.name, param)
        return tuple(seen.values())
    rt = route_type if family == "evpn" else ""
    return tuple(
        Param(_param_name(key), _RIB_KEY_HELP.get(key, f"route key {key}"), default="")
        for key in RIB_KEYS[(family, rt)]
        if key != "neighbor"
    )


def evpn_rib_requested(params: Mapping[str, Any]) -> bool:
    """Whether any EVPN route type should be read (all-types report)."""
    if params.get("list_all") == "all":
        return True
    if str(params.get("neighbor") or "").strip():
        return True
    return any(rib_requested("evpn", rt, params) for rt in EVPN_ROUTES)


def _rib_ni(family: str, params: Mapping[str, Any]) -> str:
    """EVPN RIBs on SR Linux live in the default network-instance."""
    if family == "evpn":
        return "default"
    return str(params.get("ni") or "*").strip() or "*"


def rib_requested(family: str, route_type: str, params: Mapping[str, Any]) -> bool:
    """Whether a BGP RIB read was asked for: by key, by network-instance, or list-all."""
    if params.get("list_all") == "all":
        return True
    if str(params.get("neighbor") or "").strip():
        return True
    if family != "evpn" and str(params.get("ni") or "").strip():
        return True
    rt = route_type if family == "evpn" else ""
    return any(str(params.get(_param_name(k)) or "").strip() for k in RIB_KEYS[(family, rt)])


def _rib_steps(family: str, route_type: str, direction: str) -> str:
    """Received, active, or advertised (rib-out) routes under one afi-safi."""
    outbound = direction == "advertised"
    if family == "evpn":
        container = EVPN_ROUTES[route_type]
        post = "rib-out-post" if outbound else "rib-in-post"
        return f"rib-in-out/{post}/{container}"
    if outbound:
        return "rib-in-out/rib-out-post/route"
    return "local-rib/route"


def _rib_base(family: str, route_type: str, ni: str, direction: str = "received") -> str:
    under = f"bgp-rib/afi-safi[afi-safi-name={family}]/{family}"
    steps = _rib_steps(family, route_type, direction)
    return f"/network-instance[name={ni}]/{under}/{steps}"


def _direction(params: Mapping[str, Any]) -> str:
    direction = str(params.get("direction") or "received").strip() or "received"
    return direction if direction in ("received", "active", "advertised") else "received"


def _evpn_post_path(ni: str, direction: str) -> str:
    """All EVPN route containers under one rib-in/rib-out post (one gNMI Get)."""
    outbound = direction == "advertised"
    post = "rib-out-post" if outbound else "rib-in-post"
    under = f"bgp-rib/afi-safi[afi-safi-name=evpn]/evpn/rib-in-out/{post}"
    return f"/network-instance[name={ni}]/{under}"


def _evpn_needs_per_type_reads(params: Mapping[str, Any]) -> bool:
    """Whether route keys or a peer narrow the EVPN containers (not list-all).

    A peer is a key of every route list: read by it, a node with two route
    reflectors sends half the RIB instead of all of it.
    """
    if params.get("list_all") == "all":
        return False
    if str(params.get("neighbor") or "").strip():
        return True
    for rt in EVPN_ROUTES:
        for key in RIB_KEYS[("evpn", rt)]:
            if key == "neighbor":
                continue
            if str(params.get(_param_name(key)) or "").strip():
                return True
    return False


def _route_list_name(family: str, route_type: str) -> str:
    return EVPN_ROUTES[route_type] if family == "evpn" else "route"


def _route_neighbor(route: Mapping[str, Any], keys: Mapping[str, Any], list_name: str) -> str:
    for candidate in (route.get("neighbor"), keys.get("neighbor"), keys.get(f"{list_name}.neighbor")):
        if candidate is not None and str(candidate).strip():
            return str(candidate).strip()
    return ""


def _peers_match(want: str, have: str) -> bool:
    want, have = want.strip(), have.strip()
    if not want:
        return True
    if want == have:
        return True
    try:
        return ipaddress.ip_address(want) == ipaddress.ip_address(have)
    except ValueError:
        return want.lower() == have.lower()


def _route_flag(route: Mapping[str, Any], name: str) -> bool:
    value = route.get(name)
    return value is True or (isinstance(value, str) and value.lower() == "true")


def reads_for_rib(family: str, route_type: str, params: Mapping[str, Any]) -> list[Read]:
    """The gNMI paths for one BGP RIB query: nothing unless asked; keyed or whole."""
    if not rib_requested(family, route_type, params):
        return []
    ni = _rib_ni(family, params)
    direction = _direction(params)
    base = _rib_base(family, route_type, ni, direction)
    if params.get("list_all") == "all":
        return [Read(base)]
    rt = route_type if family == "evpn" else ""
    chosen = {
        key: str(params.get(_param_name(key)) or "").strip()
        for key in RIB_KEYS[(family, rt)]
        if str(params.get(_param_name(key)) or "").strip()
    }
    neighbor = str(params.get("neighbor") or "").strip()
    if neighbor:
        chosen["neighbor"] = neighbor
    if not chosen:
        return [Read(base)]
    suffix = "".join(f"[{key}={value}]" for key, value in chosen.items())
    return [Read(base + suffix)]


def parse_rib_report(name: str) -> tuple[str, str] | None:
    """``bgp-rib-evpn`` -> ``("evpn", "")``; ``bgp-rib-evpn-2`` -> ``("evpn", "2")``."""
    if not name.startswith("bgp-rib-"):
        return None
    rest = name.removeprefix("bgp-rib-")
    if rest == "evpn":
        return "evpn", ""
    if rest.startswith("evpn-"):
        return "evpn", rest.removeprefix("evpn-")
    return rest, ""


EVPN_ALL_REPORT = "bgp-rib-evpn"


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


def _attr_sets(tree: Tree, needed: set[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Attribute sets *needed* names, by ``(network-instance, index)``.

    A node keeps a set for nearly every path it holds; a RIB report only
    merges the handful its routes reference. Look each up by key instead of
    walking the whole attr table on every run.
    """
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for ni, index in needed:
        path = f"/network-instance[name={ni}]/bgp-rib/attr-sets/attr-set[index={index}]"
        value = tree.get(path)
        if isinstance(value, dict):
            found[(ni, index)] = value
    return found


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
        Column("type", "route_type"),
    )
}

_EVPN_ALL_LAYOUT: tuple[str, ...] = (
    "type",
    "st",
    "ESI",
    "IP",
    "MAC",
    "IP-Pfx",
    "RD",
    "RT",
    "Tag",
    "as-path",
    "communities",
    "next-hop",
    "origin",
    "peer",
    "vni",
)

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

NI = Param("ni", "network-instance to read (empty: every instance)", default="")

LIST_ALL = Param(
    "list_all",
    "all: every route on every node — slow on large fabrics; use route keys instead",
    default="",
    choices=("all",),
)

DIRECTION = Param(
    "direction",
    "received: all paths from the peer; active: used or best; advertised: sent (rib-out)",
    default="",
    choices=("received", "active", "advertised"),
)

NEIGHBOR = Param("neighbor", "BGP peer address to filter routes on", default="")

ROUTE_TYPE = Param(
    "route_type",
    "EVPN route type to read (empty: every type)",
    default="",
    choices=tuple(EVPN_ROUTES),
)


def evpn_types(params: Mapping[str, Any]) -> tuple[str, ...]:
    """The EVPN route types an all-types query reads: the one chosen, or all."""
    chosen = str(params.get("route_type") or "").strip()
    return (chosen,) if chosen in EVPN_ROUTES else tuple(EVPN_ROUTES)


def rib_report_for_family(family: str) -> str:
    """The default BGP RIB report name for an afi-safi from bgp-peers."""
    if family == "evpn":
        return EVPN_ALL_REPORT
    return report_name(family, None)


def reads_for_evpn_all(params: Mapping[str, Any]) -> list[Read]:
    if not evpn_rib_requested(params):
        return []
    types = evpn_types(params)
    if len(types) == len(EVPN_ROUTES) and not _evpn_needs_per_type_reads(params):
        return [Read(_evpn_post_path(_rib_ni("evpn", params), _direction(params)))]
    out: list[Read] = []
    seen: set[str] = set()
    for rt in types:
        for read in reads_for_rib("evpn", rt, params):
            if read.path not in seen:
                seen.add(read.path)
                out.append(read)
    return out


def _wanted(route: Any, paths: str, direction: str) -> bool:
    if not isinstance(route, dict):
        return False
    if direction == "advertised":
        return True
    if direction == "active":
        return _route_flag(route, "used-route") or _route_flag(route, "best-route")
    # received: the paths used or best, or every path from the peer with paths=all
    return paths == "all" or _route_flag(route, "used-route") or _route_flag(route, "best-route")


def _select_path(family: str, route_type: str, direction: str) -> str:
    steps = _rib_steps(family, route_type, direction)
    return f"/network-instance/bgp-rib/afi-safi/{family}/{steps}"


def _attr_ids(family: str, route_type: str, tree: Tree, params: Mapping[str, Any]) -> set[tuple[str, str]]:
    """``(network-instance, attr-id)`` of the routes a RIB query shows."""
    paths = params.get("paths") or "used"
    direction = _direction(params)
    select = _select_path(family, route_type, direction)
    peer = str(params.get("neighbor") or "").strip()
    list_name = _route_list_name(family, route_type)
    return {
        (str(m.keys.get("network-instance")), str(m.value["attr-id"]))
        for m in tree.select(select)
        if isinstance(m.value, dict)
        and _wanted(m.value, paths, direction)
        and _peers_match(peer, _route_neighbor(m.value, m.keys, list_name))
        and m.value.get("attr-id") is not None
    }


def _follow_attr_sets(
    family: str, route_types: Iterable[str], tree: Tree, params: Mapping[str, Any]
) -> Iterable[Read]:
    """The attr-sets the routes name, read by key, or whole when there are many.

    One decision over every route type: SR Linux answers a Get holding both
    the whole attr-set list and keyed entries of it with the list, brackets
    stripped, inside the first keyed entry's update - not JSON.
    """
    sets = {pair for rt in route_types for pair in _attr_ids(family, rt, tree, params)}
    return keyed("bgp-rib/attr-sets/attr-set", sets, limit=ATTR_KEYED_LIMIT)


def _yield_bgp_routes(
    node: NodeState,
    family: str,
    route_type: str,
    paths: str,
    direction: str,
    neighbor: str,
) -> Iterator[BgpRoute]:
    direction = direction or "received"
    peer = str(neighbor or "").strip()
    select = _select_path(family, route_type, direction)
    list_name = _route_list_name(family, route_type)
    pending: list[tuple[str, dict[str, Any], Mapping[str, Any]]] = []
    needed: set[tuple[str, str]] = set()
    for m in node.tree.select(select):
        if not isinstance(m.value, dict):
            continue
        route = m.value
        if not _peers_match(peer, _route_neighbor(route, m.keys, list_name)):
            continue
        if not _wanted(route, paths, direction):
            continue
        ni = str(m.keys.get("network-instance"))
        attr_id = route.get("attr-id")
        if attr_id is not None:
            needed.add((ni, str(attr_id)))
        pending.append((ni, route, m.keys))
    attr_sets = _attr_sets(node.tree, needed)
    for ni, route, keys in pending:
        merged = {**route, **attr_sets.get((ni, str(route.get("attr-id"))), {})}
        if not merged.get("neighbor"):
            merged["neighbor"] = _route_neighbor(route, keys, list_name)
        yield _route(node.name, ni, family, route_type, merged)


def _register(family: str, route_type: str) -> None:
    if family == "evpn":
        title = f"BGP RIB, EVPN route type {route_type}"
    else:
        title = f"BGP RIB, {family}"

    def follow(tree: Tree, params: Mapping[str, Any]) -> Iterable[Read]:
        return _follow_attr_sets(family, (route_type,), tree, params)

    def reads(params: Mapping[str, Any]) -> list[Read]:
        return reads_for_rib(family, route_type, params)

    def build(
        node: NodeState,
        paths: str = "used",
        direction: str = "",
        neighbor: str = "",
        **_: Any,
    ) -> Iterator[BgpRoute]:
        yield from _yield_bgp_routes(node, family, route_type, paths, direction, neighbor)

    layout = _LAYOUT[(family, route_type)]
    report(
        report_name(family, route_type or None),
        title,
        record=BgpRoute,
        reads=(),
        reads_for=reads,
        follow=follow,
        params=[NI, PATHS, DIRECTION, NEIGHBOR, LIST_ALL, *rib_key_params(family, route_type)],
        columns=[Column("NI", "ni"), *(_COLUMNS[name] for name in layout)],
        detail_columns=DETAIL_COLUMNS,
        hidden=True,
    )(build)


for _type in EVPN_ROUTES:
    _register("evpn", _type)


@report(
    EVPN_ALL_REPORT,
    "BGP RIB, EVPN (all route types)",
    record=BgpRoute,
    reads=(),
    reads_for=reads_for_evpn_all,
    follow=lambda tree, params: _follow_attr_sets("evpn", evpn_types(params), tree, params),
    params=[NI, PATHS, DIRECTION, NEIGHBOR, ROUTE_TYPE, LIST_ALL, *rib_key_params("evpn", "")],
    columns=[Column("NI", "ni"), *(_COLUMNS[name] for name in _EVPN_ALL_LAYOUT)],
    detail_columns=DETAIL_COLUMNS,
    hidden=True,
)
def bgp_rib_evpn_all(
    node: NodeState,
    paths: str = "used",
    direction: str = "",
    neighbor: str = "",
    route_type: str = "",
    **_: Any,
) -> Iterator[BgpRoute]:
    for rt in evpn_types({"route_type": route_type}):
        yield from _yield_bgp_routes(node, "evpn", rt, paths, direction, neighbor)


for _family in ("ipv4-unicast", "ipv6-unicast", "l3vpn-ipv4-unicast", "l3vpn-ipv6-unicast"):
    _register(_family, "")

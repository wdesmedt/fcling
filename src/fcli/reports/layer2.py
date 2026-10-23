"""Bridging and EVPN: mac, arp, nd, es, es-dest, vxlan, irb."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, ClassVar

from ..gnmi.client import DataType
from ..report import Column, NodeState, Read, Record, Value, as_int, as_list, dig, report
from .common import NI_INTERFACES, countdown, instances_by_interface, joined, subinterface_name

# --------------------------------------------------------------------------- #
# mac
# --------------------------------------------------------------------------- #

#: A destination learned over VXLAN: ``vxlan-interface:vxlan1.101 vtep:192.168.255.2 vni:101``,
#: or with ``esi:`` instead of the VTEP for one behind an ethernet-segment.
_VXLAN_DEST = re.compile(
    r"vxlan-interface:(?P<overlay>\S+)(?:\s+vtep:(?P<vtep>\S+))?(?:\s+vni:(?P<vni>\d+))?(?:\s+esi:(?P<esi>\S+))?"
)
#: One learned over EVPN-MPLS: ``far-end:192.0.2.7 nh-tag:1727 label:310001``.
_FAR_END = re.compile(r"far-end:(?P<far_end>\S+)(?:\s+nh-tag:\S+)?(?:\s+label:(?P<label>\d+))?")


class MacEntry(Record):
    key: ClassVar[tuple[str, ...]] = ("ni", "address")

    ni: str
    address: str
    #: Where it points, as the device writes it.
    destination: str = ""
    #: ``learnt``, ``evpn``, ``evpn-static``, ``irb-interface``...
    type: str = ""
    #: The destination read apart: a local entry's subinterface, a VXLAN
    #: entry's overlay and VTEP or segment, an EVPN-MPLS entry's far end.
    interface: str = ""
    overlay: str = ""
    vtep: str = ""
    vni: int | None = None
    esi: str = ""
    far_end: str = ""
    label: int | None = None

    @property
    def local(self) -> bool:
        return not (self.overlay or self.far_end)


def _mac_entry(node: str, ni: str, mac: dict[str, Any]) -> MacEntry:
    dest = str(mac.get("destination") or "").strip()
    base: dict[str, Any] = {
        "node": node,
        "ni": ni,
        "address": str(mac.get("address") or ""),
        "destination": dest,
        "type": str(mac.get("type") or ""),
    }
    if m := _VXLAN_DEST.search(dest):
        return MacEntry(
            **base, overlay=m["overlay"], vtep=m["vtep"] or "", vni=as_int(m["vni"]), esi=m["esi"] or ""
        )
    if m := _FAR_END.search(dest):
        return MacEntry(**base, far_end=m["far_end"], label=as_int(m["label"]))
    return MacEntry(**base, interface=dest)


@report(
    "mac",
    "MAC table",
    record=MacEntry,
    reads=[Read("/network-instance[name=*]/bridge-table/mac-table/mac")],
    columns=[
        Column("NI", "ni"),
        Column("mac", "address"),
        Column("Dest", "destination"),
        Column("Type", "type"),
    ],
)
def mac(node: NodeState) -> Iterator[MacEntry]:
    for m in node.tree.select("/network-instance/bridge-table/mac-table/mac"):
        yield _mac_entry(node.name, str(m.keys.get("network-instance")), m.value)


# --------------------------------------------------------------------------- #
# arp / nd
# --------------------------------------------------------------------------- #


class NeighborEntry(Record):
    """One ARP or ND entry: an address and the MAC it resolves to."""

    key: ClassVar[tuple[str, ...]] = ("interface", "address")
    #: ND cycles reachable/stale/delay on its own, and the expiry with it.
    volatile: ClassVar[tuple[str, ...]] = ("state", "expires")

    interface: str
    #: The network-instances the subinterface is in; an irb is in two.
    nis: tuple[str, ...] = ()
    address: str
    mac: str = ""
    #: ``dynamic``, ``static``, ``evpn``...
    origin: str = ""
    #: ND only: ``reachable``, ``stale``, ``delay``...
    state: str = ""
    #: When it ages out - for ND, when it leaves its state - as the device
    #: writes it; empty where it never does.
    expires: str = ""


def _neighbors(
    node: NodeState, family: str, container: str, address: str, expiry: str
) -> Iterator[NeighborEntry]:
    bound = instances_by_interface(node.tree)
    for m in node.tree.select(f"/interface/subinterface/{family}/{container}/neighbor"):
        itf = f"{m.keys.get('interface')}.{m.keys.get('subinterface')}"
        entry = m.value
        yield NeighborEntry(
            node=node.name,
            interface=itf,
            nis=bound.get(itf, ()),
            address=str(entry.get(address) or ""),
            mac=str(entry.get("link-layer-address") or ""),
            origin=str(entry.get("origin") or ""),
            state=str(entry.get("current-state") or ""),
            expires=str(entry.get(expiry) or ""),
        )


@report(
    "arp",
    "ARP table",
    record=NeighborEntry,
    reads=[NI_INTERFACES, Read("/interface[name=*]/subinterface[index=*]/ipv4/arp/neighbor", DataType.ALL)],
    columns=[
        Column("interface", "interface"),
        Column("NI", lambda e: joined(e.nis)),
        Column("IPv4", "address"),
        Column("MAC", "mac"),
        Column("Type", "origin"),
        Column("expiry", lambda e: countdown(e.expires)),
    ],
)
def arp(node: NodeState) -> Iterator[NeighborEntry]:
    yield from _neighbors(node, "ipv4", "arp", "ipv4-address", "expiration-time")


@report(
    "nd",
    "IPv6 neighbors",
    record=NeighborEntry,
    reads=[
        NI_INTERFACES,
        Read("/interface[name=*]/subinterface[index=*]/ipv6/neighbor-discovery/neighbor", DataType.ALL),
    ],
    columns=[
        Column("interface", "interface"),
        Column("NI", lambda e: joined(e.nis)),
        Column("IPv6", "address"),
        Column("MAC", "mac"),
        Column("State", "state"),
        Column("Type", "origin"),
        Column("next_state", lambda e: countdown(e.expires)),
    ],
)
def nd(node: NodeState) -> Iterator[NeighborEntry]:
    yield from _neighbors(node, "ipv6", "neighbor-discovery", "ipv6-address", "next-state-time")


# --------------------------------------------------------------------------- #
# es
# --------------------------------------------------------------------------- #


class DfCandidate(Value):
    address: str
    designated: bool = False


class EsAssociation(Value):
    """A network-instance an ethernet-segment serves, and who may forward for it."""

    unordered: ClassVar[tuple[str, ...]] = ("candidates",)

    ni: str
    candidates: tuple[DfCandidate, ...] = ()


class EsNextHop(Value):
    """What a virtual segment tracks instead of a port: a next-hop, and its EVIs."""

    address: str
    evis: tuple[str, ...] = ()


class EthernetSegment(Record):
    key: ClassVar[tuple[str, ...]] = ("name",)
    unordered: ClassVar[tuple[str, ...]] = ("next_hops", "associations")

    name: str
    esi: str = ""
    #: ``none`` or ``virtual``.
    type: str = ""
    mh_mode: str = ""
    oper: str = ""
    interfaces: tuple[str, ...] = ()
    next_hops: tuple[EsNextHop, ...] = ()
    associations: tuple[EsAssociation, ...] = ()
    #: The bgp-evpn instances of its ip-vrfs it advertises A-D routes in, for
    #: interface-less host routes: ``advertise-ifl-host-ad-routes``.
    ifl_host_ad: tuple[int, ...] = ()


def _es_attachment(es: EthernetSegment) -> str:
    """The ports a segment hangs off, or the next-hops a virtual one tracks."""
    return " ".join(es.interfaces) or " ".join(nh.address for nh in es.next_hops)


def _es_evis(es: EthernetSegment) -> str:
    """The EVIs of a virtual segment, paired with their next-hop where they differ."""
    with_evis = [nh for nh in es.next_hops if nh.evis]
    if not with_evis:
        return ""
    if len({nh.evis for nh in with_evis}) == 1:
        return " ".join(with_evis[0].evis)
    return " ".join(f"{nh.address}:{','.join(nh.evis)}" for nh in with_evis)


def _es_associations(es: EthernetSegment) -> str:
    return joined(
        [
            f"{a.ni}:[{' '.join(c.address + '(DF)' if c.designated else c.address for c in a.candidates)}]"
            for a in es.associations
        ]
    )


def _evis(next_hop: dict[str, Any]) -> tuple[str, ...]:
    # The EVI range of a next-hop is a list keyed on ``start``.
    out: list[str] = []
    for item in as_list(next_hop.get("evi")):
        evi = item.get("start") if isinstance(item, dict) else item
        if evi not in (None, "") and str(evi) not in out:
            out.append(str(evi))
    return tuple(out)


def _df_candidates(vrf: dict[str, Any]) -> tuple[DfCandidate, ...]:
    # Only the first bgp-instance elects a DF for the segment.
    instances = as_list(vrf.get("bgp-instance"))
    first = instances[0] if instances and isinstance(instances[0], dict) else {}
    return tuple(
        DfCandidate(address=str(peer.get("address")), designated=bool(peer.get("designated-forwarder")))
        for peer in as_list(
            dig(first, "computed-designated-forwarder-candidates", "designated-forwarder-candidate")
        )
        if isinstance(peer, dict)
    )


ES_PATH = "/system/network-instance/protocols/evpn/ethernet-segments"


@report(
    "es",
    "Ethernet segments",
    record=EthernetSegment,
    reads=[Read(ES_PATH, DataType.ALL)],
    columns=[
        Column("name", "name"),
        Column("esi", "esi"),
        Column("type", "type"),
        Column("mh-mode", "mh_mode"),
        Column("oper", "oper"),
        Column("itf/nh", _es_attachment),
        Column("evi", _es_evis),
        Column("ni-peers", _es_associations),
    ],
)
def es(node: NodeState) -> Iterator[EthernetSegment]:
    for m in node.tree.select(f"{ES_PATH}/bgp-instance/ethernet-segment"):
        seg = m.value
        yield EthernetSegment(
            node=node.name,
            name=str(seg.get("name") or ""),
            esi=str(seg.get("esi") or ""),
            type=str(seg.get("type") or ""),
            mh_mode=str(seg.get("multi-homing-mode") or ""),
            oper=str(seg.get("oper-state") or ""),
            interfaces=tuple(
                str(i.get("ethernet-interface") or "")
                for i in as_list(seg.get("interface"))
                if isinstance(i, dict)
            ),
            next_hops=tuple(
                EsNextHop(address=str(nh["l3-next-hop"]), evis=_evis(nh))
                for nh in as_list(seg.get("next-hop"))
                if isinstance(nh, dict) and nh.get("l3-next-hop")
            ),
            associations=tuple(
                EsAssociation(ni=str(vrf.get("name") or ""), candidates=_df_candidates(vrf))
                for vrf in as_list(dig(seg, "association", "network-instance"))
                if isinstance(vrf, dict)
            ),
            ifl_host_ad=tuple(
                i
                for i in (
                    as_int(v) for v in as_list(dig(seg, "advertise-ifl-host-ad-routes", "bgp-evpn-instance"))
                )
                if i is not None
            ),
        )


# --------------------------------------------------------------------------- #
# es-dest
# --------------------------------------------------------------------------- #


class EsDestination(Record):
    """A remote ethernet-segment a vxlan-interface sends to, and its VTEPs."""

    key: ClassVar[tuple[str, ...]] = ("overlay", "esi")

    tunnel: str
    overlay: str
    esi: str
    vteps: tuple[str, ...] = ()


#: The VTEPs alone: an es-destination also carries counters and a MAC table,
#: tens of thousands of leaves per node on a large fabric.
ES_DEST_PATH = (
    "/tunnel-interface[name=*]/vxlan-interface[index=*]"
    "/bridge-table/unicast-destinations/es-destination[esi=*]/vtep"
)


@report(
    "es-dest",
    "ES destinations on the bridge table",
    record=EsDestination,
    reads=[Read(ES_DEST_PATH)],
    columns=[
        Column("tunnel", "tunnel"),
        Column("vxlan-itf", "overlay"),
        Column("esi", "esi"),
        Column("vteps", lambda d: " ".join(d.vteps)),
    ],
)
def es_dest(node: NodeState) -> Iterator[EsDestination]:
    for m in node.tree.select(
        "/tunnel-interface/vxlan-interface/bridge-table/unicast-destinations/es-destination"
    ):
        tunnel = str(m.keys.get("tunnel-interface"))
        yield EsDestination(
            node=node.name,
            tunnel=tunnel,
            overlay=f"{tunnel}.{m.keys.get('vxlan-interface')}",
            esi=str(m.value.get("esi") or ""),
            vteps=tuple(
                str(v.get("address") or "") for v in as_list(m.value.get("vtep")) if isinstance(v, dict)
            ),
        )


# --------------------------------------------------------------------------- #
# vxlan
# --------------------------------------------------------------------------- #


class VxlanDestination(Value):
    vtep: str
    vni: int | None = None


class VxlanInterface(Record):
    key: ClassVar[tuple[str, ...]] = ("name",)

    name: str
    ni: str = ""
    vni: int | None = None
    destinations: tuple[VxlanDestination, ...] = ()


VXLAN_VNI_PATH = "/tunnel-interface[name=*]/vxlan-interface[index=*]/ingress"
VXLAN_DEST_PATH = (
    "/tunnel-interface[name=*]/vxlan-interface[index=*]/bridge-table/unicast-destinations/destination"
)


@report(
    "vxlan",
    "VXLAN tunnel interfaces and unicast destinations",
    record=VxlanInterface,
    reads=[
        Read("/network-instance[name=*]/vxlan-interface", DataType.ALL),
        # ``all``: from 25.10 the state datastore no longer carries the configured VNI.
        Read(VXLAN_VNI_PATH, DataType.ALL),
        Read(VXLAN_DEST_PATH, DataType.ALL),
    ],
    columns=[
        Column("vxlan-itf", "name"),
        Column("NI", "ni"),
        Column("ing-vni", lambda v: v.vni if v.vni is not None else "-"),
        Column(
            "destinations",
            lambda v: (
                joined([f"({d.vtep}, {d.vni if d.vni is not None else ''})" for d in v.destinations]) or "-"
            ),
        ),
    ],
)
def vxlan(node: NodeState) -> Iterator[VxlanInterface]:
    ni_of = {
        str(m.value.get("name")): str(m.keys.get("network-instance"))
        for m in node.tree.select("/network-instance/vxlan-interface")
        if isinstance(m.value, dict)
    }
    for m in node.tree.select("/tunnel-interface/vxlan-interface"):
        name = f"{m.keys.get('tunnel-interface')}.{m.keys.get('vxlan-interface')}"
        if dig(m.value, "ingress") is None:
            continue  # only destinations streamed so far: not a configured interface
        yield VxlanInterface(
            node=node.name,
            name=name,
            ni=ni_of.get(name, ""),
            vni=as_int(dig(m.value, "ingress", "vni")),
            destinations=tuple(
                VxlanDestination(vtep=str(d.get("vtep") or ""), vni=as_int(d.get("vni")))
                for d in as_list(dig(m.value, "bridge-table", "unicast-destinations", "destination"))
                if isinstance(d, dict)
            ),
        )


# --------------------------------------------------------------------------- #
# irb
# --------------------------------------------------------------------------- #


class IrbAddress(Value):
    prefix: str
    primary: bool = False
    anycast_gw: bool = False


class HostRouteRule(Value):
    route_type: str = ""
    datapath_programming: bool = False


class NeighborSettings(Value):
    """How an irb handles ARP or ND."""

    proxy: bool = False
    #: ARP: ``true``/``false``; ND: ``none``, ``global``, ``link-local``, ``both``.
    learn_unsolicited: str = ""
    host_routes: tuple[HostRouteRule, ...] = ()
    #: The entry origins advertised into EVPN.
    evpn_advertise: tuple[str, ...] = ()
    interface_less_routing: bool = False


class IrbInterface(Record):
    key: ClassVar[tuple[str, ...]] = ("name",)

    name: str
    nis: tuple[str, ...] = ()
    ipv4: tuple[IrbAddress, ...] = ()
    ipv6: tuple[IrbAddress, ...] = ()
    anycast_gw: bool = False
    anycast_gw_mac: str = ""
    virtual_router_id: int | None = None
    arp: NeighborSettings = NeighborSettings()
    nd: NeighborSettings = NeighborSettings()


def _irb_addresses(family: Any) -> tuple[IrbAddress, ...]:
    return tuple(
        IrbAddress(
            prefix=str(a.get("ip-prefix") or ""),
            # ``primary`` is a presence container: it comes back as ``[null]``.
            primary=a.get("primary") is not None,
            anycast_gw=bool(a.get("anycast-gw")),
        )
        for a in as_list(dig(family, "address"))
        if isinstance(a, dict)
    )


def _neighbor_settings(cfg: Any, proxy: str) -> NeighborSettings:
    cfg = cfg if isinstance(cfg, dict) else {}
    advertise = [e for e in as_list(dig(cfg, "evpn", "advertise")) if isinstance(e, dict)]
    learn = cfg.get("learn-unsolicited")
    return NeighborSettings(
        proxy=bool(cfg.get(proxy)),
        learn_unsolicited="" if learn is None else str(learn).lower(),
        host_routes=tuple(
            HostRouteRule(
                route_type=str(r.get("route-type") or ""),
                datapath_programming=bool(r.get("datapath-programming")),
            )
            for r in as_list(dig(cfg, "host-route", "populate"))
            if isinstance(r, dict)
        ),
        evpn_advertise=tuple(str(e.get("route-type") or "") for e in advertise),
        interface_less_routing=any("interface-less-routing" in e for e in advertise),
    )


def _address_cell(addresses: tuple[IrbAddress, ...]) -> str:
    cells = []
    for a in addresses:
        flags = [flag for flag, on in (("P", a.primary), ("AGW", a.anycast_gw)) if on]
        cells.append(f"{a.prefix} ({','.join(flags)})" if flags else a.prefix)
    return ", ".join(cells)


def _host_route_cells(rules: tuple[HostRouteRule, ...]) -> list[str]:
    return [f"host-rt:{r.route_type or '?'}/{'dp' if r.datapath_programming else 'no-dp'}" for r in rules]


def _arp_cell(irb: IrbInterface) -> str:
    parts = (["proxy"] if irb.arp.proxy else []) + (
        ["learn-unsol"] if irb.arp.learn_unsolicited == "true" else []
    )
    return ", ".join(parts + _host_route_cells(irb.arp.host_routes)) or "-"


def _nd_cell(irb: IrbInterface) -> str:
    parts = ["proxy"] if irb.nd.proxy else []
    if irb.nd.learn_unsolicited and irb.nd.learn_unsolicited != "none":
        parts.append(f"learn-unsol:{irb.nd.learn_unsolicited}")
    return ", ".join(parts + _host_route_cells(irb.nd.host_routes)) or "-"


@report(
    "irb",
    "IRB sub-interfaces",
    record=IrbInterface,
    reads=[NI_INTERFACES, Read("/interface[name=irb*]/subinterface", DataType.ALL)],
    columns=[
        Column("name", "name"),
        Column("NI", lambda i: joined(i.nis)),
        Column("ipv4", lambda i: _address_cell(i.ipv4)),
        Column("ipv6", lambda i: _address_cell(i.ipv6)),
        Column("AGW?", lambda i: "Y" if i.anycast_gw else "N"),
        Column("arp", _arp_cell),
        Column("nd", _nd_cell),
        Column("arp-evpn", lambda i: ", ".join(rt or "?" for rt in i.arp.evpn_advertise) or "-"),
        Column("nd-evpn", lambda i: ", ".join(rt or "?" for rt in i.nd.evpn_advertise) or "-"),
        Column("IFL?", lambda i: "Y" if i.arp.interface_less_routing or i.nd.interface_less_routing else "N"),
    ],
)
def irb(node: NodeState) -> Iterator[IrbInterface]:
    bound = instances_by_interface(node.tree)
    for m in node.tree.select("/interface/subinterface"):
        itf = str(m.keys.get("interface"))
        if not itf.startswith("irb"):
            continue
        si = m.value
        name = subinterface_name(itf, si)
        anycast = si.get("anycast-gw")
        yield IrbInterface(
            node=node.name,
            name=name,
            nis=bound.get(name, ()),
            ipv4=_irb_addresses(si.get("ipv4")),
            ipv6=_irb_addresses(si.get("ipv6")),
            anycast_gw=bool(anycast),
            anycast_gw_mac=str(dig(anycast, "anycast-gw-mac") or ""),
            virtual_router_id=as_int(dig(anycast, "virtual-router-id")),
            arp=_neighbor_settings(dig(si, "ipv4", "arp"), "proxy-arp"),
            nd=_neighbor_settings(dig(si, "ipv6", "neighbor-discovery"), "proxy-nd"),
        )

"""services, bridge-domains, routers: each network-instance as the service it is.

Where ``ni`` lists a network-instance's interfaces, a service says what they
are: the bridged subinterfaces a mac-vrf faces its hosts on, the ethernet-
segment each sits on, the irb that routes it into an ip-vrf and the subnets
that irb gives it, the VNI it is carried in; for an ip-vrf, the mac-vrfs
behind its irbs, its routed ports and the BGP sessions on them, the virtual
segments it serves. One record per network-instance, every part a field.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterator
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from ..gnmi.client import DataType
from ..report import Column, NodeState, Read, Record, as_int, as_list, dig, report
from .common import SUBINTERFACES, joined
from .instances import NI_READS, BgpVpnInstance, instances, stitched, subinterfaces
from .layer2 import ES_PATH, VXLAN_VNI_PATH


class _Value(BaseModel):
    model_config = ConfigDict(frozen=True)


class Port(_Value):
    """One subinterface of a service."""

    name: str
    #: ``bridged`` (a mac-vrf's access port), ``routed`` (an ip-vrf's), ``irb``.
    kind: str
    oper: str = ""
    vlan: int | None = None
    addresses: tuple[str, ...] = ()
    anycast_gw: bool = False
    #: The ethernet-segment its port is in, and what this node calls it.
    esi: str = ""
    segment: str = ""
    #: ``irb``: the other network-instance it is in - the ip-vrf of a mac-vrf's
    #: irb, the mac-vrf of an ip-vrf's.
    peer_ni: str = ""


class Service(Record):
    key: ClassVar[tuple[str, ...]] = ("name",)

    name: str
    #: ``mac-vrf``, ``ip-vrf`` or ``default``.
    type: str = ""
    oper: str = ""
    evis: tuple[str, ...] = ()
    overlays: tuple[str, ...] = ()
    vnis: tuple[int, ...] = ()
    import_rts: tuple[str, ...] = ()
    export_rts: tuple[str, ...] = ()
    vpn: tuple[BgpVpnInstance, ...] = ()
    #: Two bgp-vpn instances: a gateway stitching the DC side to the WAN side.
    gateway: bool = False
    #: The subnets its irbs (a mac-vrf's) or ports and irbs (an ip-vrf's) give it.
    subnets: tuple[str, ...] = ()
    ports: tuple[Port, ...] = ()
    #: ``ip-vrf``: the mac-vrfs behind its irbs. ``mac-vrf``: the ip-vrfs its irbs route into.
    linked: tuple[str, ...] = ()
    #: ``ip-vrf``: its BGP sessions, ``peer (state)``.
    bgp_peers: tuple[str, ...] = ()
    #: ``ip-vrf``: the virtual ethernet-segments serving it, ``name (next-hop)``.
    virtual_es: tuple[str, ...] = ()

    @property
    def down(self) -> int:
        return sum(1 for p in self.ports if p.oper and p.oper != "up")


def _access(p: Port) -> str:
    vlan = f" vlan {p.vlan}" if p.vlan is not None else ""
    segment = f" {p.segment or p.esi}" if p.esi else ""
    oper = "" if p.oper == "up" else f" [{p.oper or '?'}]"
    return f"{p.name}{vlan}{segment}{oper}"


def _subnet(prefix: str) -> str:
    try:
        net = ipaddress.ip_interface(prefix).network
    except ValueError:
        return ""
    # Link-local is every interface's, and no service's.
    return "" if net.is_link_local else str(net)


def _ports_cell(s: Service) -> str:
    up = sum(1 for p in s.ports if p.kind != "irb" and p.oper == "up")
    total = sum(1 for p in s.ports if p.kind != "irb")
    return f"{up}/{total} up" if total else "-"


_COLUMNS = (
    Column("NI", "name"),
    Column("type", "type"),
    Column("oper", "oper"),
    Column("VNI", lambda s: joined(list(s.vnis)) or "-"),
    Column("EVI", lambda s: joined(list(s.evis)) or "-"),
    Column("import-RT", lambda s: joined(list(s.import_rts))),
    Column("export-RT", lambda s: joined(list(s.export_rts))),
    Column("subnets", lambda s: list(s.subnets)),
    Column("ports", _ports_cell),
    Column(
        "access",
        lambda s: [_access(p) for p in s.ports if p.kind != "irb"],
    ),
    Column(
        "irb",
        lambda s: [f"{p.name} -> {p.peer_ni}" if p.peer_ni else p.name for p in s.ports if p.kind == "irb"],
    ),
    Column("linked", lambda s: list(s.linked)),
    Column("GW", lambda s: "Y" if s.gateway else ""),
)

READS = (
    *NI_READS,
    SUBINTERFACES,
    Read(VXLAN_VNI_PATH, DataType.ALL),
    Read(ES_PATH, DataType.ALL),
    Read("/network-instance[name=*]/protocols/bgp/neighbor", DataType.ALL),
)


def services(node: NodeState) -> Iterator[Service]:
    tree = node.tree
    nis = instances(tree)
    details = subinterfaces(tree)
    vni = {
        f"{m.keys.get('tunnel-interface')}.{m.keys.get('vxlan-interface')}": as_int(
            dig(m.value, "ingress", "vni")
        )
        for m in tree.select("/tunnel-interface/vxlan-interface")
    }
    # port -> its segment; and the virtual segments, by the network-instance they serve.
    by_port: dict[str, tuple[str, str]] = {}
    virtual: dict[str, list[str]] = {}
    for m in tree.select(f"{ES_PATH}/bgp-instance/ethernet-segment"):
        seg = m.value
        for itf in as_list(seg.get("interface")):
            if isinstance(itf, dict) and itf.get("ethernet-interface"):
                by_port[str(itf["ethernet-interface"])] = (
                    str(seg.get("esi") or ""),
                    str(seg.get("name") or ""),
                )
        hops = [str(nh.get("l3-next-hop")) for nh in as_list(seg.get("next-hop")) if isinstance(nh, dict)]
        for vrf in as_list(dig(seg, "association", "network-instance")):
            if isinstance(vrf, dict) and hops:
                virtual.setdefault(str(vrf.get("name")), []).append(f"{seg.get('name')} ({', '.join(hops)})")
    member_of: dict[str, list[str]] = {}
    for inst in nis:
        for itf in inst.interfaces:
            member_of.setdefault(itf, []).append(inst.name)
    peers: dict[str, list[str]] = {}
    for m in tree.select("/network-instance/protocols/bgp/neighbor"):
        state = str(m.value.get("session-state") or "")
        peers.setdefault(str(m.keys.get("network-instance")), []).append(
            f"{m.value.get('peer-address')} ({state})"
        )
    types = {inst.name: inst.type for inst in nis}

    for inst in nis:
        ports: list[Port] = []
        subnets: list[str] = []
        linked: list[str] = []
        for name in inst.interfaces:
            si = details.get(name, {})
            addresses = tuple(
                str(a["ip-prefix"])
                for family in ("ipv4", "ipv6")
                for a in as_list(dig(si, family, "address"))
                if isinstance(a, dict) and a.get("ip-prefix")
            )
            irb = name.startswith("irb")
            others = [o for o in member_of.get(name, []) if o != inst.name]
            esi, segment = by_port.get(name.rsplit(".", 1)[0], ("", ""))
            ports.append(
                Port(
                    name=name,
                    kind="irb" if irb else "bridged" if inst.type == "mac-vrf" else "routed",
                    oper=str(si.get("oper-state") or ""),
                    vlan=as_int(dig(si, "vlan", "encap", "single-tagged", "vlan-id")),
                    addresses=addresses,
                    anycast_gw=bool(si.get("anycast-gw")),
                    esi=esi,
                    segment=segment,
                    peer_ni=others[0] if others else "",
                )
            )
            if irb or inst.type != "mac-vrf":
                for subnet in map(_subnet, addresses):
                    if subnet and subnet not in subnets:
                        subnets.append(subnet)
            for other in others:
                if types.get(other) in ("mac-vrf", "ip-vrf", "default") and other not in linked:
                    linked.append(other)
        if inst.type == "mac-vrf" and not any(p.kind == "irb" for p in ports):
            subnets = []
        yield Service(
            node=node.name,
            name=inst.name,
            type=inst.type,
            oper=inst.oper,
            evis=inst.evis,
            overlays=inst.overlays,
            vnis=tuple(v for o in inst.overlays if (v := vni.get(o)) is not None),
            import_rts=inst.import_rts,
            export_rts=inst.export_rts,
            vpn=inst.vpn,
            gateway=stitched(inst.vpn),
            subnets=tuple(subnets),
            ports=tuple(ports),
            linked=tuple(sorted(linked)),
            bgp_peers=tuple(peers.get(inst.name, ()) if inst.type != "mac-vrf" else ()),
            virtual_es=tuple(virtual.get(inst.name, ())),
        )


@report(
    "services", "Every network-instance as the service it is", record=Service, reads=READS, columns=_COLUMNS
)
def services_report(node: NodeState) -> Iterator[Service]:
    yield from services(node)


@report(
    "bridge-domains",
    "The mac-vrfs: access ports, segments, irbs and VNIs",
    record=Service,
    reads=READS,
    columns=_COLUMNS,
)
def bridge_domains(node: NodeState) -> Iterator[Service]:
    yield from (s for s in services(node) if s.type == "mac-vrf")


@report(
    "routers",
    "The ip-vrfs: the mac-vrfs behind them, routed ports, BGP sessions",
    record=Service,
    reads=READS,
    columns=(
        *_COLUMNS[:9],
        Column("bgp", lambda s: list(s.bgp_peers)),
        Column("vES", lambda s: list(s.virtual_es)),
        *_COLUMNS[10:],
    ),
)
def routers(node: NodeState) -> Iterator[Service]:
    yield from (s for s in services(node) if s.type == "ip-vrf")

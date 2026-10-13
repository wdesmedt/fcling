"""service: services end to end, as every node that carries them sees them.

A service is not a name. Two sites can reuse names, VNIs and route-targets
for services that never meet, and one service can go by two names. What makes
nodes carry one service is that they exchange its routes: they import a
common route-target, and reach each other's VTEPs over the underlay they
share - a site. A gateway joins its site's side of a service to its WAN side,
which gateways in every site share by route-target: a DCI service.

Each record is one network-instance on one node, with the site it is in and
the service - ``group`` - it is part of: its VNI, EVI and route-targets, its
access ports and the segments they are on, the irbs between it and the
instances it routes into, how many MACs it learned locally and over the
overlay - and what the checks say is wrong with it, on the node it is wrong on.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

from ..checks import check_es_df, check_evpn_service_mismatch, sites
from ..fabric import Fabric
from ..lens import Arg, LensError, lens
from ..report import Column, Record
from ..reports.instances import BgpVpnInstance
from ..reports.services import BgpSession, Port, Service
from .common import Index


class ServiceView(Record):
    key: ClassVar[tuple[str, ...]] = ("name",)

    name: str
    type: str = ""
    oper: str = ""
    #: The underlay it is in, ``site 1``...; empty where the fabric has one.
    site: str = ""
    #: The service it is part of, the same on every node that carries it.
    group: str = ""
    #: ``L2 DCI`` or ``L3 DCI``: its group is stitched across sites by gateways.
    dci: str = ""
    vnis: tuple[int, ...] = ()
    evis: tuple[str, ...] = ()
    import_rts: tuple[str, ...] = ()
    export_rts: tuple[str, ...] = ()
    vpn: tuple[BgpVpnInstance, ...] = ()
    gateway: bool = False
    #: ``gateway``: the ids of the bgp-vpn instances on its WAN side.
    wan: tuple[int, ...] = ()
    subnets: tuple[str, ...] = ()
    ports: tuple[Port, ...] = ()
    linked: tuple[str, ...] = ()
    bgp_peers: tuple[str, ...] = ()
    bgp_sessions: tuple[BgpSession, ...] = ()
    virtual_es: tuple[str, ...] = ()
    #: MACs this node learned on its own ports, and over the overlay.
    macs_local: int = 0
    macs_remote: int = 0
    #: What the checks found wrong with it, here.
    issues: tuple[str, ...] = ()


def _joined(values: tuple[object, ...]) -> str:
    return ", ".join(str(v) for v in values)


_COLUMNS = (
    Column("type", "type"),
    Column("site", "site"),
    Column("oper", "oper"),
    Column("VNI", lambda s: _joined(s.vnis) or "-"),
    Column("EVI", lambda s: _joined(s.evis) or "-"),
    Column("import-RT", lambda s: _joined(s.import_rts)),
    Column("export-RT", lambda s: _joined(s.export_rts)),
    Column(
        "access",
        lambda s: [p.name + (f" {p.segment or p.esi}" if p.esi else "") for p in s.ports if p.kind != "irb"],
    ),
    Column(
        "irb",
        lambda s: [f"{p.name} -> {p.peer_ni}" if p.peer_ni else p.name for p in s.ports if p.kind == "irb"],
    ),
    Column("MACs", lambda s: f"{s.macs_local} local / {s.macs_remote} remote" if s.type == "mac-vrf" else ""),
    Column("GW", lambda s: "Y" if s.gateway else ""),
    Column("DCI", "dci"),
    Column("issues", lambda s: list(s.issues)),
)

Attachment = tuple[str, str]  # (node, network-instance)


def _wan_sides(carriers: dict[Attachment, Service], site: dict[str, str]) -> dict[Attachment, set[int]]:
    """Each gateway's bgp-vpn instances on its WAN side: those whose route-targets no
    other node of its site imports. Where nothing else in the site carries the
    service, its first instance is the site's side."""
    local: dict[str, set[str]] = {}
    for (node, _), s in carriers.items():
        if not s.gateway:
            local.setdefault(site.get(node, ""), set()).update(s.import_rts)
    wan: dict[Attachment, set[int]] = {}
    for at, s in carriers.items():
        if not s.gateway:
            continue
        mine = local.get(site.get(at[0], ""), set())
        sides = {inst.id for inst in s.vpn if inst.import_rts and not set(inst.import_rts) & mine}
        if sides and sides >= {inst.id for inst in s.vpn if inst.import_rts}:
            sides.discard(min(sides))
        wan[at] = sides
    return wan


def groups(index: Index, fabric: Fabric) -> tuple[dict[Attachment, str], dict[str, str], set[str]]:
    """Every service carrier's group, each node's site, and the groups stitched across sites.

    Carriers are joined when they import a common route-target in one site, or
    on the WAN side across sites; a gateway's two sides are one carrier. One
    that imports nothing is a service of its own on its node.
    """
    carriers = {
        (node, ni): s
        for (node, ni), s in index.services.items()
        if s.type in ("mac-vrf", "ip-vrf") and ni != "mgmt"
    }
    site = sites(fabric)
    wan = _wan_sides(carriers, site)
    root: dict[Attachment, Attachment] = {at: at for at in carriers}

    def find(at: Attachment) -> Attachment:
        while root[at] != at:
            root[at] = root[root[at]]
            at = root[at]
        return at

    owner: dict[tuple[str, ...], Attachment] = {}
    keys: dict[Attachment, list[tuple[str, ...]]] = {}
    for at, s in carriers.items():
        mine: list[tuple[str, ...]] = []
        for inst in s.vpn or (BgpVpnInstance(id=1, import_rts=s.import_rts),):
            for rt in inst.import_rts:
                if inst.id in wan.get(at, ()):
                    mine.append(("wan", s.type, rt))
                else:
                    mine.append(("site", site.get(at[0], ""), s.type, rt))
        if not mine:
            mine.append(("node", at[0], at[1]))
        keys[at] = mine
        for key in mine:
            first = owner.setdefault(key, at)
            root[find(at)] = find(first)
    members: dict[Attachment, list[Attachment]] = {}
    for at in carriers:
        members.setdefault(find(at), []).append(at)
    group: dict[Attachment, str] = {}
    stitched: set[str] = set()
    for ats in members.values():
        names = sorted({ni for _, ni in ats})
        where = sorted({site.get(node, "") for node, _ in ats} - {""})
        across = any(key[0] == "wan" for at in ats for key in keys[at])
        label = "|".join([names[0], "dci" if across else (where[0] if where else ""), *names[1:]])
        if across:
            stitched.add(label)
        for at in ats:
            group[at] = label
    return group, site, stitched


@lens(
    "service",
    "Services end to end: by site, stitched across sites by gateways, and what disagrees",
    requires=("services", "mac", "ni", "vxlan", "es", "ipv4-rib", "ipv6-rib"),
    record=ServiceView,
    args=[
        Arg("name", "a network-instance: the services it is part of; every service without", positional=True)
    ],
    columns=_COLUMNS,
)
def service(fabric: Fabric, name: str | None = None) -> Iterator[ServiceView]:
    index = Index(fabric)
    group, site, stitched = groups(index, fabric)
    if name:
        wanted = {g for (_, ni), g in group.items() if ni == name}
        if not wanted:
            known = sorted({ni for _, ni in group})
            raise LensError(f"no node carries {name!r}; the services are: {', '.join(known[:40]) or 'none'}")
    else:
        wanted = set(group.values())
    chosen = sorted((at for at, g in group.items() if g in wanted), key=lambda at: (group[at], at))
    names = {ni for _, ni in chosen}
    macs: dict[tuple[str, str], list[int]] = {}
    for (node, ni, _), entry in index.macs.items():
        if ni in names:
            counts = macs.setdefault((node, ni), [0, 0])
            counts[0 if entry.local else 1] += 1
    issues: dict[tuple[str, str], list[str]] = {}
    for f in check_evpn_service_mismatch(fabric):
        if f.subject in names:
            issues.setdefault((f.node, f.subject), []).append(f.detail)
    segments: dict[str, set[str]] = {}
    for node, ni in chosen:
        for p in index.services[(node, ni)].ports:
            if p.esi:
                segments.setdefault(p.esi, set()).add(ni)
    for f in check_es_df(fabric):
        about = f.subject.split("/", 1)
        for ni in (about[1],) if len(about) == 2 and about[1] in names else segments.get(f.subject, ()):
            issues.setdefault((f.node, ni), []).append(f"{f.subject}: {f.detail}")
    wan = _wan_sides({at: index.services[at] for at in group}, site)
    for node, ni in chosen:
        s = index.services[(node, ni)]
        local, remote = macs.get((node, ni), [0, 0])
        g = group[(node, ni)]
        yield ServiceView(
            node=node,
            name=ni,
            type=s.type,
            oper=s.oper,
            site=site.get(node, ""),
            group=g,
            dci=("L2 DCI" if s.type == "mac-vrf" else "L3 DCI") if g in stitched else "",
            vnis=s.vnis,
            evis=s.evis,
            import_rts=s.import_rts,
            export_rts=s.export_rts,
            vpn=s.vpn,
            gateway=s.gateway,
            wan=tuple(sorted(wan.get((node, ni), ()))),
            subnets=s.subnets,
            ports=s.ports,
            linked=s.linked,
            bgp_peers=s.bgp_peers,
            bgp_sessions=s.bgp_sessions,
            virtual_es=s.virtual_es,
            macs_local=local,
            macs_remote=remote,
            issues=tuple(issues.get((node, ni), ())),
        )

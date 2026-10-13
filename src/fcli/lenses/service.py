"""service: one service as every node that carries it sees it.

The network-instance by that name on each node, side by side: its VNI, EVI and
route-targets, its access ports and the segments they are on, the irbs between
it and the instances it routes into, how many MACs it learned locally and over
the overlay - and what the checks say is wrong with it, on the node it is
wrong on.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

from ..checks import check_es_df, check_evpn_service_mismatch
from ..fabric import Fabric
from ..lens import Arg, LensError, lens
from ..report import Column, Record
from ..reports.services import Port
from .common import Index


class ServiceView(Record):
    key: ClassVar[tuple[str, ...]] = ("name",)

    name: str
    type: str = ""
    oper: str = ""
    vnis: tuple[int, ...] = ()
    evis: tuple[str, ...] = ()
    import_rts: tuple[str, ...] = ()
    export_rts: tuple[str, ...] = ()
    gateway: bool = False
    subnets: tuple[str, ...] = ()
    ports: tuple[Port, ...] = ()
    linked: tuple[str, ...] = ()
    #: MACs this node learned on its own ports, and over the overlay.
    macs_local: int = 0
    macs_remote: int = 0
    #: What the checks found wrong with it, here.
    issues: tuple[str, ...] = ()


def _joined(values: tuple[object, ...]) -> str:
    return ", ".join(str(v) for v in values)


_COLUMNS = (
    Column("type", "type"),
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
    Column("issues", lambda s: list(s.issues)),
)


@lens(
    "service",
    "One service as every node that carries it sees it",
    requires=("services", "mac", "ni", "vxlan", "es", "ipv4-rib", "ipv6-rib"),
    record=ServiceView,
    args=[Arg("name", "the network-instance: a mac-vrf or ip-vrf", required=True)],
    columns=_COLUMNS,
)
def service(fabric: Fabric, name: str) -> Iterator[ServiceView]:
    index = Index(fabric)
    carriers = [s for (node, ni), s in sorted(index.services.items()) if ni == name]
    if not carriers:
        known = sorted({ni for (_, ni), s in index.services.items() if s.type in ("mac-vrf", "ip-vrf")})
        raise LensError(f"no node carries {name!r}; the services are: {', '.join(known[:40]) or 'none'}")
    macs: dict[str, list[int]] = {}
    for (node, ni, _), entry in index.macs.items():
        if ni == name:
            counts = macs.setdefault(node, [0, 0])
            counts[0 if entry.local else 1] += 1
    issues: dict[str, list[str]] = {}
    segments = {p.esi for s in carriers for p in s.ports if p.esi}
    for f in check_evpn_service_mismatch(fabric):
        if f.subject == name:
            issues.setdefault(f.node, []).append(f.detail)
    for f in check_es_df(fabric):
        about = f.subject.split("/", 1)
        if (len(about) == 2 and about[1] == name) or f.subject in segments:
            issues.setdefault(f.node, []).append(f"{f.subject}: {f.detail}")
    for s in carriers:
        local, remote = macs.get(s.node, [0, 0])
        yield ServiceView(
            node=s.node,
            name=s.name,
            type=s.type,
            oper=s.oper,
            vnis=s.vnis,
            evis=s.evis,
            import_rts=s.import_rts,
            export_rts=s.export_rts,
            gateway=s.gateway,
            subnets=s.subnets,
            ports=s.ports,
            linked=s.linked,
            macs_local=local,
            macs_remote=remote,
            issues=tuple(issues.get(s.node, ())),
        )

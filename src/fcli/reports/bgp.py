"""bgp-peers: BGP sessions and their address families."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from ..gnmi.client import DataType
from ..report import Column, NodeState, Read, Record, as_int, as_list, dig, report

FAMILIES = ("evpn", "ipv4-unicast", "ipv6-unicast", "l3vpn-ipv4-unicast", "l3vpn-ipv6-unicast")


class Family(BaseModel):
    """One address family of a BGP session."""

    model_config = ConfigDict(frozen=True)

    name: str
    enabled: bool = True
    #: ``up``, ``down``, or empty where the device does not say.
    oper: str = ""
    received: int = 0
    active: int = 0
    sent: int = 0


class BgpPeer(Record):
    """One BGP session as the node that holds it sees it."""

    key: ClassVar[tuple[str, ...]] = ("ni", "peer")
    volatile: ClassVar[tuple[str, ...]] = ("families.received", "families.active", "families.sent")

    ni: str
    peer: str
    #: The session state as BGP names it: ``established``, ``active``, ``idle``...
    state: str = ""
    peer_as: int | None = None
    local_as: int | None = None
    local_address: str = ""
    local_port: int | None = None
    group: str = ""
    dynamic: bool = False
    bfd: bool = False
    fast_failover: bool = False
    import_policies: tuple[str, ...] = ()
    export_policies: tuple[str, ...] = ()
    #: Only the families the session is configured for.
    families: tuple[Family, ...] = ()

    def family(self, name: str) -> Family | None:
        return next((f for f in self.families if f.name == name), None)


def _family_cell(name: str) -> Callable[[BgpPeer], str]:
    """``received/active/sent``, or the one word that says why not."""

    def cell(peer: BgpPeer) -> str:
        family = peer.family(name)
        if family is None:
            return "-"
        if not family.enabled:
            return "disabled"
        if family.oper == "down":
            return "down"
        return f"{family.received}/{family.active}/{family.sent}"

    return cell


def _flags(peer: BgpPeer) -> str:
    return "".join(
        letter if on else "-"
        for letter, on in (("D", peer.dynamic), ("B", peer.bfd), ("F", peer.fast_failover))
    )


def _family(name: str, afi: dict[str, Any]) -> Family:
    return Family(
        name=name,
        enabled=afi.get("admin-state") == "enable",
        oper=str(afi.get("oper-state") or ""),
        received=as_int(afi.get("received-routes")) or 0,
        active=as_int(afi.get("active-routes")) or 0,
        sent=as_int(afi.get("sent-routes")) or 0,
    )


@report(
    "bgp-peers",
    "BGP peers and their status",
    record=BgpPeer,
    reads=[Read("/network-instance[name=*]/protocols/bgp/neighbor", DataType.ALL)],
    columns=[
        Column("NI", "ni"),
        Column("peer", "peer"),
        # The record keeps BGP's own word; a table wants the one the eye finds.
        Column("state", lambda p: "up" if p.state == "established" else p.state),
        Column("local-as", lambda p: p.local_as if p.local_as is not None else "-"),
        Column("peer-as", "peer_as"),
        Column("local-address", "local_address"),
        *(Column(f"{f}\nRx/Act/Tx", _family_cell(f)) for f in FAMILIES),
        Column("export-policy", lambda p: list(p.export_policies)),
        Column("flags", _flags),
        Column("group", "group"),
        Column("import-policy", lambda p: list(p.import_policies)),
    ],
)
def bgp_peers(node: NodeState) -> Iterator[BgpPeer]:
    for match in node.tree.select("/network-instance/protocols/bgp/neighbor"):
        peer = match.value
        transport = peer.get("transport") or {}
        detection = peer.get("failure-detection") or {}
        yield BgpPeer(
            node=node.name,
            ni=str(match.keys.get("network-instance") or ""),
            peer=str(peer.get("peer-address") or ""),
            state=str(peer.get("session-state") or ""),
            peer_as=as_int(peer.get("peer-as")),
            local_as=as_int(dig(peer, "local-as", "as-number")),
            local_address=str(transport.get("local-address") or ""),
            local_port=as_int(transport.get("local-port")),
            group=str(peer.get("peer-group") or ""),
            dynamic=bool(peer.get("dynamic-neighbor", False)),
            bfd=bool(detection.get("enable-bfd", False)),
            fast_failover=bool(detection.get("fast-failover", False)),
            import_policies=tuple(str(p) for p in as_list(peer.get("import-policy"))),
            export_policies=tuple(str(p) for p in as_list(peer.get("export-policy"))),
            families=tuple(
                _family(str(afi["afi-safi-name"]), afi)
                for afi in as_list(peer.get("afi-safi"))
                if isinstance(afi, dict) and afi.get("afi-safi-name") in FAMILIES
            ),
        )

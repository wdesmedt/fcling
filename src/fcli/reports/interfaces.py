"""Ports: subif, with the reason a subinterface is down where it is, lag and ifstats."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, ClassVar

from ..gnmi.client import DataType, SubscriptionMode
from ..report import Column, NodeState, Read, Record, as_int, as_list, dig, report
from ..tree import Tree
from .common import SUBINTERFACES, subinterface_name

#: Down reasons that name where to look rather than what is wrong: a
#: network-instance blames its subinterface, a subinterface its parent port.
DEFERRING_REASONS = frozenset({"subif-down", "port-down"})

#: Down reasons that are configured intent rather than a fault: a port an
#: ethernet-segment put in standby is doing exactly what it was told to.
INTENT_REASONS = frozenset({"standby-signaling"})

#: How an interface down by intent is shown: it really is down, and nothing
#: is wrong - so it is neither counted as a fault nor read as forwarding.
STANDBY_STATE = "down/standby"


def root_reason(*reasons: Any) -> str:
    """The reason at the end of a chain of ``oper-down-reason`` leaves, outermost first."""
    resolved = ""
    for reason in reasons:
        candidate = str(reason or "").lower()
        if not candidate:
            continue
        if resolved and resolved not in DEFERRING_REASONS:
            break
        resolved = candidate
    return resolved


class Subinterface(Record):
    key: ClassVar[tuple[str, ...]] = ("name",)

    interface: str
    name: str
    type: str = ""
    admin: str = ""
    #: ``up``, ``down``, or ``down/standby`` when it is down by intent.
    oper: str = ""
    down_reason: str = ""
    ip_mtu: int | None = None
    vlan: int | None = None
    ipv4: tuple[str, ...] = ()
    ipv6: tuple[str, ...] = ()


def _prefixes(family: Any) -> tuple[str, ...]:
    return tuple(
        str(a["ip-prefix"])
        for a in as_list(dig(family, "address"))
        if isinstance(a, dict) and a.get("ip-prefix")
    )


@report(
    "subif",
    "Sub-interfaces of nodes",
    record=Subinterface,
    reads=[
        SUBINTERFACES,
        # A subinterface says ``port-down`` without saying what is wrong with
        # the port; this is where that is said. Only carried while it is down.
        Read("/interface[name=*]/oper-down-reason"),
    ],
    columns=[
        Column("Itf", "interface"),
        Column("Subitf", "name"),
        Column("type", "type"),
        Column("admin", "admin"),
        Column("oper", "oper"),
        Column("down-reason", "down_reason"),
        Column("ip-mtu", "ip_mtu"),
        Column("vlan", "vlan"),
        Column("ipv4", lambda s: list(s.ipv4)),
        Column("ipv6", lambda s: list(s.ipv6)),
    ],
)
def subif(node: NodeState) -> Iterator[Subinterface]:
    port_reasons = {
        str(m.keys.get("interface")): m.value for m in node.tree.select("/interface/oper-down-reason")
    }
    for match in node.tree.select("/interface/subinterface"):
        itf = str(match.keys.get("interface") or "")
        si = match.value
        name = subinterface_name(itf, si)
        oper = str(si.get("oper-state") or "")
        reason = root_reason(si.get("oper-down-reason"), port_reasons.get(itf))
        if oper == "down" and reason in INTENT_REASONS:
            oper = STANDBY_STATE
        yield Subinterface(
            node=node.name,
            interface=itf,
            name=name,
            type=str(si.get("type") or ""),
            admin=str(si.get("admin-state") or ""),
            oper=oper,
            down_reason="" if oper == "up" else reason,
            ip_mtu=as_int(si.get("ip-mtu")),
            vlan=as_int(dig(si, "vlan", "encap", "single-tagged", "vlan-id")),
            ipv4=_prefixes(si.get("ipv4")),
            ipv6=_prefixes(si.get("ipv6")),
        )


class Lag(Record):
    """One LAG and one of its members: a LAG without members is a row of its own."""

    key: ClassVar[tuple[str, ...]] = ("name", "member")

    name: str
    oper: str = ""
    mtu: int | None = None
    min_links: int | None = None
    description: str = ""
    #: ``static`` or ``lacp``.
    type: str = ""
    speed: int | None = None
    standby_signaling: str = ""
    lacp_key: int | None = None
    lacp_interval: str = ""
    lacp_mode: str = ""
    lacp_system_id: str = ""
    lacp_priority: int | None = None
    member: str = ""
    member_oper: str = ""
    #: LACP ``ACTIVE`` or ``PASSIVE``.
    member_activity: str = ""


@report(
    "lag",
    "LAGs of nodes",
    record=Lag,
    reads=[Read("/interface[name=lag*]", DataType.ALL)],
    columns=[
        Column("lag", "name"),
        Column("oper", "oper"),
        Column("mtu", "mtu"),
        Column("min", "min_links"),
        Column("desc", "description"),
        Column("type", "type"),
        Column("speed", "speed"),
        Column("stby-sig", "standby_signaling"),
        Column("lacp-key", "lacp_key"),
        Column("lacp-itvl", "lacp_interval"),
        Column("lacp-mode", "lacp_mode"),
        Column("lacp-sysid", "lacp_system_id"),
        Column("lacp-prio", "lacp_priority"),
        Column("act", "member_activity"),
        # Abbreviated to keep the column narrow; the record has the full name.
        Column("member-itf", lambda r: r.member.replace("ethernet", "et")),
        Column("member-oper", "member_oper"),
    ],
)
def lag(node: NodeState) -> Iterator[Lag]:
    for match in node.tree.select("/interface"):
        itf = match.value
        name = str(itf.get("name") or "")
        if not name.startswith("lag"):
            continue
        lag_ = itf.get("lag") or {}
        lacp = lag_.get("lacp") or {}
        base = Lag(
            node=node.name,
            name=name,
            oper=str(itf.get("oper-state") or ""),
            mtu=as_int(itf.get("mtu")),
            min_links=as_int(lag_.get("min-links")),
            description=str(itf.get("description") or ""),
            type=str(lag_.get("lag-type") or ""),
            speed=as_int(lag_.get("lag-speed")),
            standby_signaling=str(dig(itf, "ethernet", "standby-signaling") or ""),
            lacp_key=as_int(lacp.get("admin-key")),
            lacp_interval=str(lacp.get("interval") or ""),
            lacp_mode=str(lacp.get("lacp-mode") or ""),
            lacp_system_id=str(lacp.get("system-id-mac") or ""),
            lacp_priority=as_int(lacp.get("system-priority")),
        )
        members = [m for m in as_list(lag_.get("member")) if isinstance(m, dict)]
        if not members:
            yield base
        for member in members:
            yield base.model_copy(
                update={
                    "member": str(member.get("name") or ""),
                    "member_oper": str(member.get("oper-state") or ""),
                    "member_activity": str(dig(member, "lacp", "activity") or ""),
                }
            )


class InterfaceStats(Record):
    """A port's counters, and its rates between two samples."""

    key: ClassVar[tuple[str, ...]] = ("name",)
    volatile: ClassVar[tuple[str, ...]] = (
        "in_kbps",
        "out_kbps",
        "in_pps",
        "out_pps",
        "in_errors",
        "out_errors",
        "in_discards",
        "out_discards",
        "in_packets",
        "out_packets",
        "in_octets",
        "out_octets",
    )

    name: str
    oper: str = ""
    down_reason: str = ""
    in_kbps: float = 0.0
    out_kbps: float = 0.0
    in_pps: float = 0.0
    out_pps: float = 0.0
    #: Errors and discards between the two samples.
    in_errors: int = 0
    out_errors: int = 0
    in_discards: int = 0
    out_discards: int = 0
    #: Totals at the second sample.
    in_packets: int = 0
    out_packets: int = 0
    in_octets: int = 0
    out_octets: int = 0


_COUNTERS = {
    "in_packets": "in-packets",
    "out_packets": "out-packets",
    "in_octets": "in-octets",
    "out_octets": "out-octets",
    "in_errors": "in-error-packets",
    "out_errors": "out-error-packets",
    "in_discards": "in-discarded-packets",
    "out_discards": "out-discarded-packets",
}


def _counters(tree: Tree | None) -> dict[str, dict[str, int]]:
    if tree is None:
        return {}
    return {
        str(m.keys.get("interface")): {f: as_int(m.value.get(leaf)) or 0 for f, leaf in _COUNTERS.items()}
        for m in tree.select("/interface/statistics")
        if isinstance(m.value, dict)
    }


@report(
    "ifstats",
    "Per-interface rates from two samples",
    record=InterfaceStats,
    reads=[
        Read("/interface[name=*]/statistics", mode=SubscriptionMode.SAMPLE, sample_interval=10),
        Read("/interface[name=*]/oper-state"),
        Read("/interface[name=*]/oper-down-reason"),
    ],
    interval=5.0,
    eventful=False,
    columns=[
        Column("interface", "name"),
        Column("oper-state", "oper"),
        Column("down-reason", "down_reason"),
        Column("in-Kbps", "in_kbps"),
        Column("out-Kbps", "out_kbps"),
        Column("in-pps", "in_pps"),
        Column("out-pps", "out_pps"),
        Column("in-err", "in_errors"),
        Column("out-err", "out_errors"),
        Column("in-disc", "in_discards"),
        Column("out-disc", "out_discards"),
        Column("in-pkts", "in_packets"),
        Column("out-pkts", "out_packets"),
        Column("in-octets", "in_octets"),
        Column("out-octets", "out_octets"),
    ],
)
def ifstats(node: NodeState) -> Iterator[InterfaceStats]:
    now, before = _counters(node.tree), _counters(node.previous)
    elapsed = node.elapsed or 0.0
    for name in sorted(now):
        if name not in before or elapsed <= 0:
            continue
        # A counter that went backwards was reset between the samples: no traffic, not a negative rate.
        delta = {f: max(now[name][f] - before[name][f], 0) for f in _COUNTERS}
        yield InterfaceStats(
            node=node.name,
            name=name,
            oper=str(node.tree.get(f"/interface[name={name}]/oper-state") or ""),
            down_reason=str(node.tree.get(f"/interface[name={name}]/oper-down-reason") or ""),
            in_kbps=round(round(delta["in_octets"] * 8 / elapsed) / 1000, 1),
            out_kbps=round(round(delta["out_octets"] * 8 / elapsed) / 1000, 1),
            in_pps=round(delta["in_packets"] / elapsed, 1),
            out_pps=round(delta["out_packets"] / elapsed, 1),
            in_errors=delta["in_errors"],
            out_errors=delta["out_errors"],
            in_discards=delta["in_discards"],
            out_discards=delta["out_discards"],
            **{f: now[name][f] for f in ("in_packets", "out_packets", "in_octets", "out_octets")},
        )

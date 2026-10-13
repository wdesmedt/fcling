"""Fabric checks: the first questions asked of a fabric, answered at once.

A check is a pure function from a :class:`~fcli.fabric.Fabric` - every node's
records of the reports it ``requires`` - to :class:`Finding` records. Seeing
the whole fabric is what lets it say what no single table can: that a link is
seen from one end only, that two leaves disagree about a service's VNI.

Findings are records like any report's, keyed on the check and its subject,
so the engine keeps them the same way: a finding appearing is a change, and
so is it clearing - the timeline of what was wrong and since when is the
history the store already keeps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from .fabric import Fabric, index, out_of_band, parent, text
from .report import Record
from .reports.bgp import BgpPeer
from .reports.interfaces import Subinterface
from .reports.layer2 import EthernetSegment

#: The fabric is not doing what it was built to do.
ERROR = "error"
#: Legitimate in some fabrics, a fault in most.
WARNING = "warning"
SEVERITY_ORDER = {ERROR: 0, WARNING: 1}


class Finding(Record):
    """One thing a check found wrong, on one node."""

    key: ClassVar[tuple[str, ...]] = ("check", "subject")
    #: The detail carries counts and states that move while the finding stands;
    #: the finding raised and cleared is the history, not every rewording.
    volatile: ClassVar[tuple[str, ...]] = ("detail",)

    check: str
    severity: str
    #: What on that node: an interface, a peer, a service.
    subject: str
    detail: str


@dataclass(frozen=True)
class Check:
    name: str
    title: str
    #: The reports it reads.
    requires: tuple[str, ...]
    run: Callable[[Fabric], Iterable[Finding]]


CHECKS: dict[str, Check] = {}


def check(
    name: str, title: str, requires: Sequence[str] = ()
) -> Callable[[Callable[[Fabric], Iterable[Finding]]], Any]:
    def register(run: Callable[[Fabric], Iterable[Finding]]) -> Callable[[Fabric], Iterable[Finding]]:
        CHECKS[name] = Check(name, title, tuple(requires), run)
        return run

    return register


def required(names: Iterable[str] | None = None) -> tuple[str, ...]:
    """The reports the checks *names* - all of them by default - read."""
    chosen = CHECKS.values() if names is None else (CHECKS[n] for n in names)
    return tuple(dict.fromkeys(r for c in chosen for r in c.requires))


def run_checks(fabric: Fabric, names: Iterable[str] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for c in CHECKS.values() if names is None else (CHECKS[n] for n in names):
        findings.extend(c.run(fabric))
    return sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.check, f.node, f.subject))


def _f(check: str, severity: str, node: str, subject: str, detail: str) -> Finding:
    return Finding(node=node, check=check, severity=severity, subject=subject, detail=detail)


# --------------------------------------------------------------------------- #
# reachability
# --------------------------------------------------------------------------- #


@check("node_unreachable", "Nodes that could not be read")
def check_node_unreachable(fabric: Fabric) -> Iterator[Finding]:
    for node, why in sorted(fabric.unreachable.items()):
        yield _f("node_unreachable", ERROR, node, "gnmi", f"nothing could be read: {why}")


# --------------------------------------------------------------------------- #
# BGP
# --------------------------------------------------------------------------- #


def _peers(fabric: Fabric) -> list[BgpPeer]:
    return list(fabric.get("bgp-peers"))


@check("bgp_down", "BGP sessions that are not established", ["bgp-peers"])
def check_bgp_down(fabric: Fabric) -> Iterator[Finding]:
    for peer in _peers(fabric):
        session = text(peer.state)
        if session in ("established", "", "disabled", "-"):
            continue
        yield _f(
            "bgp_down",
            ERROR,
            peer.node,
            f"{peer.ni}/{peer.peer}",
            f"session is {session}, peer-group {peer.group or '-'}, "
            f"AS {peer.peer_as if peer.peer_as is not None else '?'}",
        )


@check("bgp_af_down", "Established sessions with an address family down", ["bgp-peers"])
def check_bgp_af_down(fabric: Fabric) -> Iterator[Finding]:
    """The session hides it: ``established``, while the family carrying the overlay never came up."""
    for peer in _peers(fabric):
        if text(peer.state) != "established":
            continue
        for family in peer.families:
            if family.enabled and text(family.oper) == "down":
                yield _f(
                    "bgp_af_down",
                    ERROR,
                    peer.node,
                    f"{peer.ni}/{peer.peer}",
                    f"session established but {family.name} is down",
                )


@check("bgp_no_routes", "Established sessions that have received no routes", ["bgp-peers"])
def check_bgp_no_routes(fabric: Fabric) -> Iterator[Finding]:
    """Normal on the day a fabric is built; on a running fabric, a policy dropping everything."""
    for peer in _peers(fabric):
        if text(peer.state) != "established":
            continue
        for family in peer.families:
            if family.enabled and text(family.oper) != "down" and not family.received:
                yield _f(
                    "bgp_no_routes",
                    WARNING,
                    peer.node,
                    f"{peer.ni}/{peer.peer}",
                    f"{family.name} is up but has received no routes",
                )


# --------------------------------------------------------------------------- #
# interfaces
# --------------------------------------------------------------------------- #


def _subifs(fabric: Fabric) -> list[Subinterface]:
    return [s for s in fabric.get("subif") if not out_of_band(s.interface)]


@check("itf_down", "Interfaces enabled but not up", ["subif"])
def check_itf_down(fabric: Fabric) -> Iterator[Finding]:
    """A subinterface held down on purpose - the standby side of a single-active
    segment - reads ``down/standby``, and is not a fault."""
    for s in _subifs(fabric):
        if text(s.oper) != "down" or text(s.admin) not in ("enable", "", "up"):
            continue
        yield _f(
            "itf_down",
            ERROR,
            s.node,
            s.name,
            f"admin enabled but oper down: {s.down_reason or 'no reason reported'}",
        )


@check("itf_errors", "Interfaces dropping packets", ["ifstats"])
def check_itf_errors(fabric: Fabric) -> Iterator[Finding]:
    """The counters are the change over the sample: it is happening now.

    Not on a containerlab node: the kernel discards on a veth what a real port
    forwards, so the counters move on a healthy lab all the time.
    """
    for stats in fabric.get("ifstats"):
        if out_of_band(stats.name) or stats.node in fabric.virtual:
            continue
        if stats.in_errors or stats.out_errors:
            yield _f(
                "itf_errors",
                ERROR,
                stats.node,
                stats.name,
                f"{stats.in_errors} in / {stats.out_errors} out error packets during the sample",
            )
        if stats.in_discards or stats.out_discards:
            yield _f(
                "itf_errors",
                WARNING,
                stats.node,
                stats.name,
                f"{stats.in_discards} in / {stats.out_discards} out discarded packets during the sample",
            )


# --------------------------------------------------------------------------- #
# topology
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Link:
    """One end of an adjacency, as the node holding it sees it."""

    node: str
    port: str
    peer: str
    peer_port: str


def adjacencies(fabric: Fabric) -> list[Link]:
    """Every LLDP adjacency whose neighbour is a node of ours."""
    links = []
    for n in fabric.get("lldp"):
        if out_of_band(n.interface) or not n.system_name or out_of_band(n.port_id):
            continue
        peer = fabric.resolve(n.system_name)
        if peer and peer != n.node:
            links.append(Link(n.node, n.interface, peer, n.port_id))
    return links


@check("lldp_one_sided", "Links only one end can see", ["lldp"])
def check_lldp_one_sided(fabric: Fabric) -> Iterator[Finding]:
    """Both ends run LLDP: a link one end does not report is not carrying both ways."""
    seen = {(x.node, x.port, x.peer, x.peer_port) for x in adjacencies(fabric)}
    answered = fabric.answered("lldp")
    for node, port, peer, peer_port in sorted(seen):
        if (peer, peer_port, node, port) in seen or peer not in answered:
            continue
        yield _f(
            "lldp_one_sided", WARNING, node, port, f"sees {peer} {peer_port}, which does not see it back"
        )


@check("mtu_mismatch", "Links whose ends disagree about the MTU", ["lldp", "subif"])
def check_mtu_mismatch(fabric: Fabric) -> Iterator[Finding]:
    """A fabric that passes every ping and drops everything large."""
    mtus: dict[tuple[str, str], dict[str, int]] = {}
    for s in _subifs(fabric):
        if s.ip_mtu is not None:
            mtus.setdefault((s.node, s.interface), {})[index(s.name)] = s.ip_mtu
    compared = set()
    for link in adjacencies(fabric):
        ends = tuple(sorted([(link.node, link.port), (link.peer, link.peer_port)]))
        if ends in compared:
            continue
        compared.add(ends)
        near, far = mtus.get(ends[0], {}), mtus.get(ends[1], {})
        for i in sorted(set(near) & set(far)):
            if near[i] != far[i]:
                yield _f(
                    "mtu_mismatch",
                    ERROR,
                    ends[0][0],
                    f"{ends[0][1]}.{i}",
                    f"ip-mtu {near[i]}, but {ends[1][0]} {ends[1][1]}.{i} on the other end has {far[i]}",
                )


def outliers(values: Mapping[str, Any], floor: int = 3) -> dict[str, tuple[Any, Any]]:
    """The entries of *values* in the minority, with what the clear majority says.

    Two nodes differing are two opinions, not an outlier and a norm, so a
    majority needs *floor* subjects and more than half of them.
    """
    counts: dict[Any, int] = {}
    for value in values.values():
        counts[value] = counts.get(value, 0) + 1
    if len(values) < floor or len(counts) < 2:
        return {}
    majority, agreeing = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))
    if agreeing * 2 <= len(values):
        return {}
    return {subject: (value, majority) for subject, value in values.items() if value != majority}


@check("mtu_outlier", "Nodes whose fabric MTU differs from the rest of the fabric", ["subif"])
def check_mtu_outlier(fabric: Fabric) -> Iterator[Finding]:
    """Catches the leaf at the default MTU before its link carries anything."""
    per_node: dict[str, int] = {}
    for s in _subifs(fabric):
        if s.ip_mtu is None or s.name.startswith(("irb", "system", "lo")):
            continue
        seen = per_node.setdefault(s.node, s.ip_mtu)
        if seen != s.ip_mtu:
            per_node[s.node] = -1  # a deliberate mix makes no claim
    comparable = {node: mtu for node, mtu in per_node.items() if mtu > 0}
    odd = outliers(comparable)
    for node, (mine, majority) in sorted(odd.items()):
        yield _f(
            "mtu_outlier",
            WARNING,
            node,
            "ip-mtu",
            f"fabric interfaces use ip-mtu {mine}, where {majority} is what "
            f"{len(comparable) - len(odd)} of {len(comparable)} nodes use",
        )


# --------------------------------------------------------------------------- #
# EVPN services
# --------------------------------------------------------------------------- #


def _rt_set(targets: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({rt.strip().removeprefix("target:") for rt in targets if rt.strip()}))


def describe(value: Any) -> str:
    if isinstance(value, list | tuple | set):
        return ", ".join(str(v) for v in value) or "none"
    return str(value) if str(value) else "none"


def system_addresses(fabric: Fabric) -> dict[str, set[str]]:
    """Each node's loopback addresses: what the others see it as in the underlay."""
    found: dict[str, set[str]] = {}
    for r in fabric.get("ni"):
        if r.ni == "default" and r.interface.startswith(("system0", "lo")):
            for prefix in r.prefixes:
                found.setdefault(r.node, set()).add(prefix.split("/", 1)[0])
    return found


def underlay_hosts(fabric: Fabric) -> dict[str, set[str]]:
    """The host routes in each node's default instance: the loopbacks it can reach."""
    hosts: dict[str, set[str]] = {}
    for report in ("ipv4-rib", "ipv6-rib"):
        for route in fabric.get(report):
            address, _, length = route.prefix.partition("/")
            if route.ni == "default" and length in ("32", "128"):
                hosts.setdefault(route.node, set()).add(address)
    return hosts


def underlay_domains(
    nodes: Sequence[str],
    system: Mapping[str, set[str]],
    hosts: Mapping[str, set[str]],
    gateways: set[str] = frozenset(),  # type: ignore[assignment]
) -> list[list[str]]:
    """*nodes* grouped by the underlay they share: each has the other's loopback as a host route.

    Gateways learn each other's loopbacks over the WAN, which says nothing
    about the DC while the set has other nodes in it. Without route tables,
    one underlay.
    """
    names = sorted(nodes)
    if not any(hosts.get(n) for n in names):
        return [names] if names else []
    skip_gateway_pairs = any(n not in gateways for n in names)

    def sees(a: str, b: str) -> bool:
        return any(ip in hosts.get(a, ()) for ip in system.get(b, ()))

    root = {n: n for n in names}

    def find(n: str) -> str:
        while root[n] != n:
            root[n] = root[root[n]]
            n = root[n]
        return n

    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            if skip_gateway_pairs and a in gateways and b in gateways:
                continue
            if sees(a, b) and sees(b, a):
                root[find(b)] = find(a)
    domains: dict[str, list[str]] = {}
    for n in names:
        domains.setdefault(find(n), []).append(n)
    return sorted(domains.values())


def service_groups(by_node: Mapping[str, dict[str, Any]]) -> list[list[str]]:
    """Nodes carrying one name, grouped by a shared import route-target: the services they are."""
    targets = {node: set(facts.get("import route-target") or ()) for node, facts in by_node.items()}
    groups: list[list[str]] = []
    for node in sorted(targets):
        mine = targets[node]
        joined = [g for g in groups if any(mine & targets[o] or (not mine and not targets[o]) for o in g)]
        groups = [g for g in groups if g not in joined] + [sorted([node, *(m for g in joined for m in g)])]
    return sorted(groups)


def _disagreements(by_node: Mapping[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for fact in dict.fromkeys(name for facts in by_node.values() for name in facts):
        values = {node: facts[fact] for node, facts in by_node.items() if fact in facts}
        if len(values) >= 2 and len({describe(v) for v in values.values()}) >= 2:
            out.append((fact, values))
    return out


@check(
    "evpn_service_mismatch",
    "Services whose nodes disagree about VNI or route-targets",
    # The route tables say which nodes share an underlay, and so can disagree at all.
    ["ni", "vxlan", "ipv4-rib", "ipv6-rib"],
)
def check_evpn_service_mismatch(fabric: Fabric) -> Iterator[Finding]:
    """A stretched mac-vrf with a VNI or a route-target off on one leaf never joins up.

    Compared among nodes sharing an underlay, per bgp-vpn instance (a gateway's
    WAN side against the other gateways), and among nodes sharing a target:
    two services under one name is a warning about the split.
    """
    vnis = {(v.node, v.name): v.vni for v in fabric.get("vxlan")}
    services: dict[str, dict[str, dict[str, Any]]] = {}
    gateways: dict[str, set[str]] = {}
    unknown: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for r in fabric.get("ni"):
        if text(r.type) not in ("mac-vrf", "ip-vrf") or (r.node, r.ni) in seen:
            continue
        seen.add((r.node, r.ni))
        missing = [o for o in r.overlays if (r.node, o) not in vnis]
        unknown.setdefault(r.node, []).extend(missing)
        facts: dict[str, Any] = (
            {} if missing else {"VNI": ", ".join(str(vnis[(r.node, o)]) for o in r.overlays)}
        )
        for inst in r.vpn:
            which = f" of bgp-instance {inst.id}" if inst.id != 1 else ""
            facts[f"import route-target{which}"] = _rt_set(inst.import_rts)
            facts[f"export route-target{which}"] = _rt_set(inst.export_rts)
        services.setdefault(r.ni, {})[r.node] = facts
        if len(r.vpn) > 1:
            gateways.setdefault(r.ni, set()).add(r.node)
    system, hosts = system_addresses(fabric), underlay_hosts(fabric)

    def disagree(name: str, by_node: Mapping[str, dict[str, Any]]) -> Iterator[Finding]:
        for fact, values in _disagreements(by_node):
            for node in sorted(values):
                others = sorted(set(values) - {node})
                yield _f(
                    "evpn_service_mismatch",
                    ERROR,
                    node,
                    name,
                    f"{fact} {describe(values[node])}, against "
                    + ", ".join(f"{o} {describe(values[o])}" for o in others),
                )

    read = fabric.answered("vxlan")
    for node, missing in sorted(unknown.items()):
        if missing and node in read:
            shown = ", ".join(missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
            yield _f(
                "evpn_service_mismatch",
                WARNING,
                node,
                "VNI",
                f"no VXLAN state for {len(missing)} vxlan-interface(s) its services use ({shown}); "
                "their VNI was not compared",
            )
    for name, by_node in sorted(services.items()):
        wan = gateways.get(name, set())
        for domain in underlay_domains(list(by_node), system, hosts, wan):
            local = {node: by_node[node] for node in domain}
            groups = service_groups(local)
            if len(groups) > 1:
                targets = {node: local[node].get("import route-target") for node in local}
                for group in groups:
                    elsewhere = sorted(set(local) - set(group))
                    for node in group:
                        yield _f(
                            "evpn_service_mismatch",
                            WARNING,
                            node,
                            name,
                            f"import route-target {describe(targets[node])}, while "
                            + ", ".join(f"{o} {describe(targets[o])}" for o in elsewhere)
                            + " in the same underlay: two services under one name, or a mistyped target",
                        )
            for group in groups:
                yield from disagree(
                    name, {n: {f: v for f, v in local[n].items() if "bgp-instance" not in f} for n in group}
                )
        if len(wan) > 1:
            for domain in underlay_domains(sorted(wan), system, hosts, wan):
                yield from disagree(
                    name, {n: {f: v for f, v in by_node[n].items() if "bgp-instance" in f} for n in domain}
                )


# --------------------------------------------------------------------------- #
# ethernet segments
# --------------------------------------------------------------------------- #


@check("es_df", "Ethernet segments without a designated forwarder, or with two", ["es", "ni"])
def check_es_df(fabric: Fabric) -> Iterator[Finding]:
    """A segment with no DF forwards no BUM traffic; nodes disagreeing on it, or
    on the multi-homing mode, duplicate frames or black-hole them."""
    segments: list[EthernetSegment] = list(fabric.get("es"))
    modes: dict[str, dict[str, str]] = {}
    elected: dict[tuple[str, str], dict[str, str]] = {}
    for seg in segments:
        if seg.esi:
            modes.setdefault(seg.esi, {})[seg.node] = text(seg.mh_mode)
        if text(seg.oper) not in ("up", ""):
            attached = " ".join(seg.interfaces) or " ".join(nh.address for nh in seg.next_hops)
            yield _f(
                "es_df",
                ERROR,
                seg.node,
                seg.name,
                f"segment is {text(seg.oper)} on {attached or 'no interface'}",
            )
        for a in seg.associations:
            df = next((c.address for c in a.candidates if c.designated), None)
            if df is None:
                yield _f(
                    "es_df",
                    ERROR,
                    seg.node,
                    f"{seg.name}/{a.ni}",
                    "no designated forwarder elected among "
                    + (" ".join(c.address for c in a.candidates) or "no candidates"),
                )
            elif seg.esi:
                elected.setdefault((seg.esi, a.ni), {})[seg.node] = df
    owner = {address: node for node, addresses in system_addresses(fabric).items() for address in addresses}
    names = {seg.esi: seg.name for seg in segments if seg.name}
    for (esi, ni), by_node in sorted(elected.items()):
        if len(set(by_node.values())) < 2:
            continue
        views: dict[str, list[str]] = {}
        for node, df in by_node.items():
            views.setdefault(df, []).append(node)
        said = "; ".join(
            f"{df}{f' ({owner[df]})' if df in owner else ''} according to {', '.join(sorted(nodes))}"
            for df, nodes in sorted(views.items())
        )
        for node in sorted(by_node):
            yield _f(
                "es_df",
                ERROR,
                node,
                f"{names.get(esi, esi)}/{ni}",
                f"nodes disagree on the designated forwarder: {said}",
            )
    for esi, by_node in sorted(modes.items()):
        if len({m for m in by_node.values() if m}) < 2:
            continue
        for node in sorted(by_node):
            yield _f(
                "es_df",
                ERROR,
                node,
                esi,
                f"multi-homing mode {by_node[node] or 'unset'}, against "
                + ", ".join(f"{o} {by_node[o] or 'unset'}" for o in sorted(set(by_node) - {node})),
            )


# --------------------------------------------------------------------------- #
# liveness: BFD and the IGPs
# --------------------------------------------------------------------------- #

_BFD_FINE = {"up", "admin-down", "admin_down"}
#: Two routers on a broadcast segment that are neither of them DR stop at two-way, correctly.
_OSPF_FINE = {"full", "two-way", "2-way"}
_IGP_LOOPBACKS = ("system", "lo")


@check("bfd_down", "BFD sessions that are not up", ["bfd"])
def check_bfd_down(fabric: Fabric) -> Iterator[Finding]:
    """BFD down leaves the protocol it protects blind - or down with it."""
    for s in fabric.get("bfd"):
        if s.state in _BFD_FINE:
            continue
        where = f" on {s.interface}" if s.interface else ""
        diagnostic = s.local_diagnostic if s.local_diagnostic not in ("", "no_diagnostic") else ""
        silent = s.remote_discriminator == 0 and not s.failures
        yield _f(
            "bfd_down",
            ERROR,
            s.node,
            f"{s.ni}/{s.remote_address}",
            f"session is {s.state or 'unknown'}{where}, protecting {', '.join(s.protocols) or 'nothing'}"
            + (f", diagnostic {diagnostic}" if diagnostic else "")
            + ("; the far end has never answered - is BFD enabled there?" if silent else ""),
        )


@check("igp_adjacency_down", "IS-IS adjacencies and OSPF neighbours that are not up", ["isis", "ospf"])
def check_igp_adjacency_down(fabric: Fabric) -> Iterator[Finding]:
    for a in fabric.get("isis"):
        if not a.system_id or a.state == "up":
            continue
        reason = f", {a.down_reason}" if a.down_reason else ""
        yield _f(
            "igp_adjacency_down",
            ERROR,
            a.node,
            a.interface,
            f"IS-IS {a.level} adjacency to {a.hostname or a.system_id} is {a.state or 'unknown'}{reason}",
        )
    for n in fabric.get("ospf"):
        if not n.router_id or n.state in _OSPF_FINE:
            continue
        yield _f(
            "igp_adjacency_down",
            ERROR,
            n.node,
            n.interface,
            f"OSPF neighbour {n.router_id} in area {n.area} is {n.state or 'unknown'}",
        )


def _igp_interfaces(fabric: Fabric, report: str) -> dict[tuple[str, str], list[Any]]:
    """(node, interface) -> its rows: one per adjacency, or one alone."""
    rows: dict[tuple[str, str], list[Any]] = {}
    for r in fabric.get(report):
        rows.setdefault((r.node, r.interface), []).append(r)
    return rows


@check("igp_no_adjacency", "IGP interfaces that are up but formed no adjacency", ["isis", "ospf"])
def check_igp_no_adjacency(fabric: Fabric) -> Iterator[Finding]:
    """Nothing reports it down: up, the IGP runs on it, and nobody answers -
    a mismatched area, level, authentication, or an MTU the hellos do not fit."""
    for report, noun, peer in (("isis", "IS-IS", "system_id"), ("ospf", "OSPF", "router_id")):
        for (node, name), rows in sorted(_igp_interfaces(fabric, report).items()):
            first = rows[0]
            if first.passive or first.oper != "up" or any(getattr(r, peer) for r in rows):
                continue
            if name.startswith(_IGP_LOOPBACKS):
                continue
            yield _f(
                "igp_no_adjacency",
                WARNING,
                node,
                name,
                f"{noun} runs on it and it is up, but no adjacency formed",
            )


# --------------------------------------------------------------------------- #
# configuration: the two ends of one session or one link
# --------------------------------------------------------------------------- #


def address_owners(fabric: Fabric) -> dict[str, str]:
    """Every address a node has on a subinterface or loopback, and the node."""
    owners: dict[str, str] = {}
    for node, addresses in system_addresses(fabric).items():
        for address in addresses:
            owners.setdefault(address, node)
    for s in _subifs(fabric):
        for prefix in (*s.ipv4, *s.ipv6):
            owners.setdefault(prefix.split("/", 1)[0], s.node)
    return owners


def _far_end(
    peer: BgpPeer, candidates: Sequence[BgpPeer], mine: set[str], loopbacks: Mapping[str, set[str]], far: str
) -> BgpPeer | None:
    """The far end's session back to *peer*'s node: the one aimed at the address
    *peer* speaks from. Two nodes often have two sessions - eBGP over the link,
    iBGP between loopbacks - and pairing one with the other compares an
    underlay's AS with an overlay's."""
    back = [p for p in candidates if peer_host(p.peer) in mine]
    if peer.local_address:
        exact = [p for p in back if peer_host(p.peer) == peer_host(peer.local_address)]
        if exact:
            return exact[0]
    # Loopback to loopback, link to link.
    to_loopback = peer_host(peer.peer) in loopbacks.get(far, set())
    own_loopbacks = loopbacks.get(peer.node, set())
    alike = [p for p in back if (peer_host(p.peer) in own_loopbacks) == to_loopback]
    chosen = alike or back
    return chosen[0] if chosen else None


def peer_host(peer: str) -> str:
    """``fe80::1%ethernet-1/1.0`` and ``10.0.0.1`` as the bare address."""
    return str(peer or "").split("/", 1)[0].split("%", 1)[0].strip()


@check(
    "bgp_peer_mismatch",
    "BGP sessions whose two ends are configured not to match",
    ["bgp-peers", "subif", "ni"],
)
def check_bgp_peer_mismatch(fabric: Fabric) -> Iterator[Finding]:
    """What the two ends disagree on is why the session is down - or, for a
    family or BFD, what it silently does not do while up."""
    owners = address_owners(fabric)
    loopbacks = system_addresses(fabric)
    addresses: dict[str, set[str]] = {}
    for address, node in owners.items():
        addresses.setdefault(node, set()).add(address)
    sessions: dict[tuple[str, str], list[BgpPeer]] = {}
    dynamic: set[tuple[str, str]] = set()
    for p in _peers(fabric):
        sessions.setdefault((p.node, p.ni), []).append(p)
        if p.dynamic:
            dynamic.add((p.node, p.ni))
    answered = fabric.answered("bgp-peers")
    reported: set[tuple[Any, ...]] = set()
    for (node, ni), peers in sorted(sessions.items()):
        for peer in peers:
            far = owners.get(peer_host(peer.peer))
            if far is None or far == node:
                continue
            back = _far_end(peer, sessions.get((far, ni), []), addresses.get(node, set()), loopbacks, far)
            subject = f"{ni}/{peer.peer}"
            if back is None:
                # Dynamic neighbours are only listed once up; a far end not read says nothing.
                if (far, ni) in dynamic or far not in answered or text(peer.state) == "established":
                    continue
                yield _f(
                    "bgp_peer_mismatch",
                    ERROR,
                    node,
                    subject,
                    f"{far} owns {peer_host(peer.peer)} but has no session back to {node} in {ni}",
                )
                continue
            if (
                not peer.dynamic
                and peer.peer_as is not None
                and back.local_as is not None
                and peer.peer_as != back.local_as
            ):
                yield _f(
                    "bgp_peer_mismatch",
                    ERROR,
                    node,
                    subject,
                    f"expects AS {peer.peer_as} from {far}, which runs AS {back.local_as}",
                )
            pair = (ni, *sorted([(node, peer.peer), (far, back.peer)]))
            if pair in reported:
                continue
            reported.add(pair)
            # A session still opening has negotiated nothing yet.
            if text(peer.state) != "established" or text(back.state) != "established":
                continue
            here = {f.name for f in peer.families if f.enabled}
            there = {f.name for f in back.families if f.enabled}
            for family in sorted(here ^ there):
                which, other = (node, far) if family in here else (far, node)
                yield _f(
                    "bgp_peer_mismatch",
                    WARNING,
                    node,
                    subject,
                    f"{family} is enabled on {which} only: {other} does not exchange it on this session",
                )
            if peer.bfd != back.bfd:
                yield _f(
                    "bgp_peer_mismatch",
                    WARNING,
                    node,
                    subject,
                    f"BFD protects it on {node if peer.bfd else far} only, "
                    "so a failure is detected fast on one end",
                )


@check(
    "igp_peer_mismatch",
    "Links whose two ends are configured for different IGP adjacencies",
    ["lldp", "isis", "ospf"],
)
def check_igp_peer_mismatch(fabric: Fabric) -> Iterator[Finding]:
    """Read across both ends of every cable LLDP sees: none of it is visible from one end."""
    ends: dict[tuple[str, str], dict[str, Any]] = {}
    for report in ("isis", "ospf"):
        for (node, name), rows in _igp_interfaces(fabric, report).items():
            if not name.startswith(_IGP_LOOPBACKS):
                ends.setdefault((node, parent(name)), {})[report] = rows[0]
    running = {report: {r.node for r in fabric.get(report)} for report in ("isis", "ospf")}
    compared = set()
    for link in adjacencies(fabric):
        pair = tuple(sorted([(link.node, link.port), (link.peer, link.peer_port)]))
        if pair in compared:
            continue
        compared.add(pair)
        (node, port), (far, far_port) = pair
        near, other = ends.get((node, port), {}), ends.get((far, far_port), {})
        for report, noun in (("isis", "IS-IS"), ("ospf", "OSPF")):
            a, b = near.get(report), other.get(report)
            if a is None and b is None:
                continue
            if a is None or b is None:
                only, missing = (
                    ((node, port), (far, far_port)) if a is not None else ((far, far_port), (node, port))
                )
                if missing[0] not in running[report]:
                    continue
                itf: Any = a if a is not None else b
                yield _f(
                    "igp_peer_mismatch",
                    WARNING,
                    only[0],
                    itf.interface,
                    f"{noun} runs on this end only: {missing[0]} {missing[1]} does not run it",
                )
                continue
            if report == "ospf" and a.area != b.area:
                yield _f(
                    "igp_peer_mismatch",
                    ERROR,
                    node,
                    a.interface,
                    f"OSPF area {a.area} here, {b.area} on {far} {b.interface}: no adjacency forms",
                )
            kind_a = a.circuit_type if report == "isis" else a.interface_type
            kind_b = b.circuit_type if report == "isis" else b.interface_type
            if kind_a and kind_b and kind_a != kind_b:
                yield _f(
                    "igp_peer_mismatch",
                    ERROR if report == "isis" else WARNING,
                    node,
                    a.interface,
                    f"{noun} network type {kind_a} here, {kind_b} on {far} {b.interface}",
                )
            if a.passive != b.passive:
                yield _f(
                    "igp_peer_mismatch",
                    WARNING,
                    node,
                    a.interface,
                    f"{noun} is passive on {node if a.passive else far} only: "
                    "no adjacency forms over this link",
                )


# --------------------------------------------------------------------------- #
# the platform
# --------------------------------------------------------------------------- #

RESOURCE_WARNING = 80
RESOURCE_ERROR = 95


@check("resource_high", "CPU, memory or forwarding tables close to full", ["resources"])
def check_resource_high(fabric: Fabric) -> Iterator[Finding]:
    """A full forwarding table is the failure nothing else explains: the route
    is in the RIB, BGP is happy, and the hardware had no room to program it."""
    for r in fabric.get("resources"):
        warning = r.threshold if r.threshold is not None else RESOURCE_WARNING
        if r.used_percent is None or r.used_percent < warning:
            continue
        counts = f" ({r.used} used, {r.free} free)" if r.used is not None else ""
        said = f", past the node's {r.threshold}% threshold" if r.threshold is not None else ""
        if r.node in fabric.virtual and r.name in ("memory", "cpu"):
            # Every container on the host says the same: it is the host's.
            said += "; a containerlab node reports its host's"
        yield _f(
            "resource_high",
            ERROR if r.used_percent >= max(RESOURCE_ERROR, warning) else WARNING,
            r.node,
            f"{r.component} {r.name}",
            f"{r.used_percent}% in use{counts}{said}",
        )


@check("hardware_fault", "Cards, fans and power supplies fitted and not working", ["components"])
def check_hardware_fault(fabric: Fabric) -> Iterator[Finding]:
    for c in fabric.get("components"):
        if c.oper in ("", "empty", "up", "booting") and c.health != "unhealthy":
            continue
        yield _f(
            "hardware_fault",
            ERROR,
            c.node,
            f"{c.kind} {c.id}",
            f"oper-state {c.oper or 'unknown'}, health {c.health or 'unknown'}",
        )


@check("optic_dom", "Optics reporting a DOM alarm or warning", ["transceivers"])
def check_optic_dom(fabric: Fabric) -> Iterator[Finding]:
    """Fading light is the earliest warning a link gives before CRC errors and flaps."""
    for t in fabric.get("transceivers"):
        powers = [c.input_power for c in t.channels if c.input_power is not None]
        for severity, crossed in ((ERROR, t.alarms), (WARNING, t.warnings)):
            if not crossed:
                continue
            reading = []
            if powers:
                reading.append(f"lowest rx {min(powers):.2f} dBm")
            if t.temperature is not None:
                reading.append(f"{t.temperature:.1f} C")
            yield _f(
                "optic_dom",
                severity,
                t.node,
                t.interface,
                f"{'alarm' if severity == ERROR else 'warning'}: {', '.join(crossed)}"
                + (f" ({', '.join(reading)})" if reading else ""),
            )


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #

#: Seconds of history a flap is counted in, and how many changes make one.
FLAP_WINDOW = 600.0
FLAP_MIN = 3

#: What flapping means per report: the field whose value going back and forth
#: is the flap. A MAC moving between ports over and over is a loop or two hosts
#: on one address; an IP changing MAC over and over is a duplicate address.
FLAP_FIELDS = {
    "bgp-peers": ("bgp", "state"),
    "subif": ("interface", "oper"),
    "bfd": ("bfd", "state"),
    "isis": ("isis", "state"),
    "ospf": ("ospf", "state"),
    "lag": ("lag", "member_oper"),
    "mac": ("mac", "destination"),
    "arp": ("arp", "mac"),
    "nd": ("nd", "mac"),
}


@check("flapping", "Sessions, ports and MACs that keep changing state")
def check_flapping(fabric: Fabric) -> Iterator[Finding]:
    """Each reading shows it fine; only the history shows it going back and
    forth. Reads the history, so it finds nothing where none is kept. Entries
    merely learned and aged out are not a flap: only a value that changed."""
    if not fabric.changes:
        return
    newest = max(c["ts"] for c in fabric.changes)
    moves: dict[tuple[str, str, str], list[Any]] = {}
    for c in fabric.changes:
        if c["ts"] < newest - FLAP_WINDOW or c["report"] not in FLAP_FIELDS or c["op"] != "change":
            continue
        _kind, name = FLAP_FIELDS[c["report"]]
        before, after = (c.get("before") or {}).get(name), (c.get("after") or {}).get(name)
        if before == after:
            continue
        values = moves.setdefault((c["node"], c["report"], c["key"]), [before])
        values.append(after)
    for (node, report, key), values in sorted(moves.items()):
        count = len(values) - 1
        if count < FLAP_MIN:
            continue
        kind, _ = FLAP_FIELDS[report]
        seen = " / ".join(dict.fromkeys(str(v) for v in values))
        yield _f(
            "flapping",
            WARNING,
            node,
            f"{report} {key}",
            f"{kind} changed {count} times ({seen}) in the last {round(FLAP_WINDOW / 60)} minutes",
        )

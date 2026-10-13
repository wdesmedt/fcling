"""Findings grouped into incidents: one root cause and what follows from it.

One broken cable is not one finding: it is an interface down on both ends,
the BGP and BFD sessions over it down on both ends, an IGP adjacency, maybe
an ethernet-segment without a forwarder - each true, none saying they are the
same thing. :func:`correlate` does that reading in code. Every finding is
*anchored* to what it is about, and findings on one anchor are one incident,
with the finding that explains the others as its root:

* a finding about a port is on the cable that port is - as LLDP sees it, as
  the history saw it before the link went down, or as the two ends of one
  point-to-point subnet say;
* a session is over the port its peer is reached on: a link-local peer names
  it, any other is on a connected subnet, or not directly attached;
* a session to a node that stopped answering is about that node;
* an overlay session to a loopback the node has no route to is about the
  underlay between the two - where to look instead of at BGP.

What no rule anchors is an incident of its own: together the incidents hold
exactly the findings. Incidents with the same cause in several places - BFD
not enabled on any spine - fold into one pattern.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .checks import CHECKS, ERROR, SEVERITY_ORDER, Finding, system_addresses
from .fabric import Fabric, out_of_band, parent

#: Which finding explains the others on one anchor, most fundamental first.
ROOT_PRIORITY: tuple[str, ...] = (
    "node_unreachable",
    "hardware_fault",
    "optic_dom",
    "itf_down",
    "lldp_one_sided",
    "mtu_mismatch",
    "itf_errors",
    "underlay_unreachable",
    # A configuration that cannot work explains what it keeps down.
    "igp_peer_mismatch",
    "igp_adjacency_down",
    "igp_no_adjacency",
    "bgp_peer_mismatch",
    "bfd_down",
    "bgp_down",
    "bgp_af_down",
    "flapping",
    "es_df",
    "bgp_no_routes",
    "resource_high",
)

_PORT_CHECKS = frozenset(
    {
        "itf_down",
        "itf_errors",
        "lldp_one_sided",
        "mtu_mismatch",
        "optic_dom",
        "igp_adjacency_down",
        "igp_no_adjacency",
        "igp_peer_mismatch",
    }
)
#: Checks whose subject is ``<ni>/<peer address>``.
_SESSION_CHECKS = frozenset({"bgp_down", "bgp_af_down", "bgp_no_routes", "bfd_down", "bgp_peer_mismatch"})
_PLATFORM_CHECKS = frozenset({"hardware_fault", "resource_high"})
#: Where in a flapping record's key its port is: ``subif`` is keyed on the
#: subinterface, ``lag`` on (lag, member), ``isis`` on (ni, instance, interface, ...).
_FLAP_PORT = {"subif": 0, "lag": 1, "isis": 2, "ospf": 3}
#: Where its network-instance and peer are, for a session: ``ni/peer``.
_FLAP_SESSION = {"bgp-peers": (0, 1), "bfd": (0, 2)}

_NOUNS = {
    "node_unreachable": ("node unreachable", "nodes unreachable"),
    "itf_down": ("interface down", "interfaces down"),
    "itf_errors": ("port dropping packets", "ports dropping packets"),
    "lldp_one_sided": ("one-sided LLDP adjacency", "one-sided LLDP adjacencies"),
    "mtu_mismatch": ("MTU mismatch", "MTU mismatches"),
    "optic_dom": ("optic alarm", "optic alarms"),
    "igp_adjacency_down": ("IGP adjacency down", "IGP adjacencies down"),
    "igp_no_adjacency": ("IGP interface without adjacency", "IGP interfaces without adjacency"),
    "bfd_down": ("BFD session down", "BFD sessions down"),
    "bgp_down": ("BGP session down", "BGP sessions down"),
    "bgp_af_down": ("BGP family down", "BGP families down"),
    "bgp_peer_mismatch": ("BGP configuration mismatch", "BGP configuration mismatches"),
    "igp_peer_mismatch": ("IGP configuration mismatch", "IGP configuration mismatches"),
    "bgp_no_routes": ("BGP family without routes", "BGP families without routes"),
    "es_df": ("ethernet-segment problem", "ethernet-segment problems"),
    "flapping": ("flap", "flaps"),
    "hardware_fault": ("hardware fault", "hardware faults"),
    "resource_high": ("resource running out", "resources running out"),
    "underlay_unreachable": ("underlay route missing", "underlay routes missing"),
}


@dataclass(frozen=True)
class Incident:
    """One thing wrong with the fabric, with every finding it accounts for."""

    #: Stable while the incident lasts: what it is anchored to.
    id: str
    severity: str
    #: ``link``, ``port``, ``node``, ``session``, ``underlay``, ``platform``,
    #: ``segment``, ``finding`` or ``pattern``.
    kind: str
    title: str
    #: Where the root is: where to start looking.
    node: str
    nodes: tuple[str, ...]
    root: Finding
    related: tuple[Finding, ...] = ()
    explanation: str = ""
    #: When the oldest of its findings was raised, and the newest - where a history says.
    first_seen: float | None = None
    last_seen: float | None = None

    @property
    def findings(self) -> tuple[Finding, ...]:
        return (self.root, *self.related)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "kind": self.kind,
            "title": self.title,
            "node": self.node,
            "nodes": list(self.nodes),
            "explanation": self.explanation,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "root": self.root.model_dump(mode="json"),
            "related": [f.model_dump(mode="json") for f in self.related],
        }


# --------------------------------------------------------------------------- #
# what the fabric says about where things are
# --------------------------------------------------------------------------- #

Port = tuple[str, str]  # (node, port)
Anchor = tuple[Any, ...]


def _host(prefix: str) -> str:
    return str(prefix).split("/", 1)[0].split("%", 1)[0].strip()


def _ip(value: str) -> Any:
    try:
        return ipaddress.ip_address(_host(value))
    except ValueError:
        return None


def _network(prefix: str) -> Any:
    try:
        return ipaddress.ip_network(str(prefix).strip(), strict=False)
    except ValueError:
        return None


class _Where:
    """The readings anchoring needs, indexed once."""

    def __init__(self, fabric: Fabric) -> None:
        self.links: dict[Port, Port] = {}
        for n in fabric.get("lldp"):
            peer = fabric.resolve(n.system_name) if n.system_name else None
            if peer and peer != n.node and n.port_id and not out_of_band(n.interface):
                self._cable((n.node, n.interface), (peer, n.port_id))
        # A cable that went down is still that cable: the history kept it.
        for c in fabric.changes:
            if c["report"] == "lldp" and c["op"] in ("remove", "change") and c.get("before"):
                b = c["before"]
                peer = fabric.resolve(str(b.get("system_name") or ""))
                if (
                    peer
                    and peer != c["node"]
                    and b.get("port_id")
                    and not out_of_band(b.get("interface", ""))
                ):
                    self._cable((c["node"], b["interface"]), (peer, b["port_id"]))
        self.owners: dict[str, tuple[str, str]] = {}
        self.connected: dict[str, list[tuple[Any, str]]] = {}
        for s in fabric.get("subif"):
            if out_of_band(s.interface):
                continue
            for prefix in (*s.ipv4, *s.ipv6):
                self.owners.setdefault(_host(prefix), (s.node, s.name))
                network = _network(prefix)
                if network is not None:
                    self.connected.setdefault(s.node, []).append((network, s.name))
        self._cable_subnets()
        self.loopbacks = {
            address: node for node, addresses in system_addresses(fabric).items() for address in addresses
        }
        self.hosts: dict[str, set[str]] = {}
        for report in ("ipv4-rib", "ipv6-rib"):
            for route in fabric.get(report):
                if route.ni == "default":
                    self.hosts.setdefault(route.node, set()).add(route.prefix)
        self.bfd_ports = {
            (s.node, f"{s.ni}/{s.remote_address}"): s.interface for s in fabric.get("bfd") if s.interface
        }
        self.segment_ports = {(seg.node, seg.name): tuple(seg.interfaces) for seg in fabric.get("es")}

    def _cable(self, a: Port, b: Port) -> None:
        self.links.setdefault(a, b)
        self.links.setdefault(b, a)

    def _cable_subnets(self) -> None:
        """Two ends of one point-to-point subnet are two ends of one cable."""
        ends: dict[Any, list[tuple[str, str]]] = {}
        for node, networks in self.connected.items():
            for network, subif in networks:
                if network.prefixlen >= network.max_prefixlen - 2 and not network.is_link_local:
                    ends.setdefault(network, []).append((node, subif))
        for members in ends.values():
            if len(members) == 2 and members[0][0] != members[1][0]:
                (a, sa), (b, sb) = members
                self._cable((a, parent(sa)), (b, parent(sb)))

    def link(self, node: str, port: str) -> frozenset[Port]:
        end = (node, parent(port))
        far = self.links.get(end)
        return frozenset({end, far}) if far else frozenset({end})

    def session_port(self, node: str, subject: str) -> str | None:
        """The subinterface a session ``<ni>/<peer>`` runs over, if one."""
        _ni, _, peer = subject.partition("/")
        address, _, scope = peer.partition("%")
        if scope:
            return scope
        if (node, subject) in self.bfd_ports:
            return self.bfd_ports[(node, subject)]
        ip = _ip(address)
        if ip is None:
            return None
        for network, subif in self.connected.get(node, ()):
            if ip in network and network.prefixlen < network.max_prefixlen:
                return subif
        return None

    def session_peer(self, subject: str) -> str | None:
        address = _host(subject.partition("/")[2].partition("%")[0])
        if address in self.loopbacks:
            return self.loopbacks[address]
        owner = self.owners.get(address)
        return owner[0] if owner else None

    def reaches(self, node: str, address: str) -> bool | None:
        """Whether *node*'s default instance routes *address*; ``None`` without a table."""
        routes, ip = self.hosts.get(node), _ip(address)
        if routes is None or ip is None:
            return None
        for prefix in routes:
            network = _network(prefix)
            if network is not None and ip in network and network.prefixlen > 0:
                return True
        return False


# --------------------------------------------------------------------------- #
# anchoring
# --------------------------------------------------------------------------- #


def _flap(finding: Finding) -> tuple[str, list[Any]]:
    """What a flapping finding is about: its report, and the record's key."""
    if finding.check != "flapping":
        return "", []
    report, _, key = finding.subject.partition(" ")
    try:
        return report, list(json.loads(key.split("#", 1)[0]))
    except ValueError:
        return report, []


def _port_subject(finding: Finding) -> str | None:
    if finding.check in _PORT_CHECKS:
        return finding.subject
    report, key = _flap(finding)
    at = _FLAP_PORT.get(report)
    return str(key[at]) if at is not None and len(key) > at and key[at] else None


def _session_subject(finding: Finding) -> str | None:
    if finding.check in _SESSION_CHECKS:
        return finding.subject
    report, key = _flap(finding)
    at = _FLAP_SESSION.get(report)
    return f"{key[at[0]]}/{key[at[1]]}" if at is not None and len(key) > max(at) else None


def _anchor(finding: Finding, where: _Where, unreachable: set[str], port_anchors: set[Anchor]) -> Anchor:
    if finding.check == "node_unreachable":
        return ("node", finding.node)
    if finding.check in _PLATFORM_CHECKS:
        return ("platform", finding.node)
    port = _port_subject(finding)
    if port is not None:
        return ("link", where.link(finding.node, port))
    session = _session_subject(finding)
    if session is not None:
        over = where.session_port(finding.node, session)
        if over is not None:
            link = ("link", where.link(finding.node, over))
            if link in port_anchors:
                return link
        peer = where.session_peer(session)
        if peer is None and over is not None:
            # A peer that answered nothing is still the node at the far end of the cable.
            far = where.links.get((finding.node, parent(over)))
            peer = far[0] if far else None
        if peer in unreachable or finding.node in unreachable:
            return ("node", peer if peer in unreachable else finding.node)
        address = _host(session.partition("/")[2])
        # Whatever is wrong with an overlay session to a loopback the node has
        # no route to - down, or not configured back - is the underlay's doing.
        if peer and address in where.loopbacks:
            if where.reaches(finding.node, address) is False:
                return ("underlay", finding.node, peer)
        if over is not None:
            return ("link", where.link(finding.node, over))
        return ("session", session.partition("/")[0], frozenset({finding.node, peer or address}))
    if finding.check == "es_df":
        name = finding.subject.split("/", 1)[0]
        for port in where.segment_ports.get((finding.node, name), ()):
            link = ("link", where.link(finding.node, port))
            if link in port_anchors:
                return link
        return ("segment", name)
    return ("finding", finding.check, finding.node, finding.subject)


def _rank(finding: Finding) -> tuple[int, int, str, str]:
    return (
        ROOT_PRIORITY.index(finding.check) if finding.check in ROOT_PRIORITY else len(ROOT_PRIORITY),
        SEVERITY_ORDER.get(finding.severity, 9),
        finding.node,
        finding.subject,
    )


def correlate(
    findings: Sequence[Finding], fabric: Fabric, raised: Mapping[tuple[str, str], float] | None = None
) -> list[Incident]:
    """*findings* grouped by root cause, worst first; dated where *raised*
    says when each ``(node, record key)`` appeared."""
    where = _Where(fabric)
    unreachable = set(fabric.unreachable)
    # Ports first, so a session knows which links have something wrong with
    # them and joins those rather than standing alone.
    port_anchors = {
        ("link", where.link(f.node, port)) for f in findings if (port := _port_subject(f)) is not None
    }
    groups: dict[Anchor, list[Finding]] = {}
    for finding in findings:
        groups.setdefault(_anchor(finding, where, unreachable, port_anchors), []).append(finding)
    incidents = _patterns([_incident(anchor, members, where) for anchor, members in groups.items()])
    if raised:
        incidents = [_dated(i, raised) for i in incidents]
    incidents.sort(key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), -len(i.findings), i.node, i.title))
    return incidents


def _dated(incident: Incident, raised: Mapping[tuple[str, str], float]) -> Incident:
    from .delta import record_key

    times = [raised[(f.node, record_key(f))] for f in incident.findings if (f.node, record_key(f)) in raised]
    if not times:
        return incident
    return replace(incident, first_seen=min(times), last_seen=max(times))


def _incident(anchor: Anchor, members: list[Finding], where: _Where) -> Incident:
    kind = anchor[0]
    members = sorted(members, key=_rank)
    root = _root(anchor, members, where)
    related = tuple(f for f in members if f is not root)
    everything = (root, *related)
    if kind == "link" and len(anchor[1]) == 1:
        kind = "port"
    return Incident(
        id=_identity(anchor),
        severity=min((f.severity for f in everything), key=lambda s: SEVERITY_ORDER.get(s, 9)),
        kind=kind,
        title=_title(anchor, root),
        node=root.node,
        nodes=tuple(sorted({f.node for f in everything})),
        root=root,
        related=related,
        explanation=_explain(root, related),
    )


def _root(anchor: Anchor, members: list[Finding], where: _Where) -> Finding:
    """The finding that explains the rest; for an underlay without a route, one no check says."""
    if anchor[0] == "underlay":
        _kind, node, peer = anchor
        address = next((a for a, owner in where.loopbacks.items() if owner == peer), peer)
        return Finding(
            node=node,
            check="underlay_unreachable",
            severity=ERROR,
            subject=peer,
            detail=f"no route to {peer}'s loopback {address} in the default instance, "
            "so no overlay session to it can come up",
        )
    return members[0]


def _identity(anchor: Anchor) -> str:
    parts = []
    for part in anchor:
        if isinstance(part, frozenset):
            parts.append("+".join(sorted(":".join(p) if isinstance(p, tuple) else str(p) for p in part)))
        else:
            parts.append(str(part))
    return "|".join(parts)


def _title(anchor: Anchor, root: Finding) -> str:
    what = _NOUNS.get(root.check, (CHECKS[root.check].title if root.check in CHECKS else root.check,))[0]
    kind = anchor[0]
    if kind == "link":
        cable = " <-> ".join(f"{node} {port.replace('ethernet-', 'e')}" for node, port in sorted(anchor[1]))
        return f"{cable}: {what}"
    if kind == "node":
        return f"{anchor[1]} is unreachable"
    if kind == "underlay":
        return f"{anchor[1]} has no underlay route to {anchor[2]}"
    if kind == "session":
        return f"{' <-> '.join(sorted(str(e) for e in anchor[2]))} ({anchor[1]}): {what}"
    if kind == "platform":
        return f"{anchor[1]} {root.subject}: {what}"
    if kind == "segment":
        if "disagree on the designated forwarder" in root.detail:
            what = f"designated forwarder disagreement in {root.subject.split('/', 1)[-1]}"
        return f"ethernet-segment {anchor[1]}: {what}"
    return f"{root.node} {root.subject}: {what}"


def _explain(root: Finding, related: tuple[Finding, ...]) -> str:
    text = root.detail.rstrip(".")
    if not related:
        return text
    counts: dict[str, int] = {}
    for f in related:
        counts[f.check] = counts.get(f.check, 0) + 1
    parts = []
    for c in sorted(counts, key=lambda c: ROOT_PRIORITY.index(c) if c in ROOT_PRIORITY else 99):
        singular, plural = _NOUNS.get(c, (c, c))
        parts.append(f"{counts[c]} {singular if counts[c] == 1 else plural}")
    stop = "" if text.endswith(("?", "!")) else "."
    return f"{text}{stop} It accounts for {', '.join(parts)}."


# --------------------------------------------------------------------------- #
# patterns: one cause, many places
# --------------------------------------------------------------------------- #

#: How many incidents with one cause make a pattern rather than a coincidence.
PATTERN_MIN = 3
_PLACES = {
    "link": "links",
    "port": "ports",
    "session": "sessions",
    "node": "nodes",
    "platform": "nodes",
    "underlay": "node pairs",
}
#: The states a BGP session cycles through while it keeps failing: which one a
#: reading catches is chance, not a different cause.
_BGP_TRYING = re.compile(r"\bsession is (?:idle|connect|active|opensent|openconfirm)\b")
#: A dynamic neighbour only knows its peer-group and AS part way through the
#: handshake: the cause of a session down is its state alone.
_CAUSE_ONLY = {"bgp_down": lambda detail: detail.split(",", 1)[0]}


def template(detail: str, check: str = "") -> str:
    """A finding's detail without its names and numbers: what kind of wrong it is."""
    detail = _CAUSE_ONLY.get(check, lambda d: d)(detail)
    detail = _BGP_TRYING.sub("session is not established", detail)
    return re.sub(r"\S*\d\S*", "#", detail)


def _patterns(incidents: list[Incident]) -> list[Incident]:
    """Sixteen links each with BFD down and the far end silent are one problem -
    BFD not enabled on the spines - and read as one card. Every finding is kept."""
    by_cause: dict[tuple[str, str, str], list[Incident]] = {}
    for incident in incidents:
        if incident.root.check == "flapping":
            key = ("flapping", "", "")
        elif incident.kind in ("finding", "segment"):
            key = ("", incident.id, "")
        else:
            key = (
                incident.root.check,
                template(incident.root.detail, incident.root.check),
                incident.root.severity,
            )
        by_cause.setdefault(key, []).append(incident)
    folded: list[Incident] = []
    for (cause, shape, _severity), members in by_cause.items():
        if cause == "flapping" and len(members) >= 2:
            folded.append(
                _fold(members, "pattern|flapping", f"Flapping in {len(members)} {_places(members)}", None)
            )
        elif not cause or len(members) < PATTERN_MIN:
            folded.extend(members)
        else:
            singular = _NOUNS.get(cause, (cause, cause))[0]
            folded.append(
                _fold(
                    members,
                    f"pattern|{cause}|{shape}",
                    f"{singular} on {len(members)} {_places(members)}",
                    cause,
                )
            )
    return folded


def _places(members: list[Incident]) -> str:
    kinds = {i.kind for i in members}
    return _PLACES.get(next(iter(kinds)), "places") if len(kinds) == 1 else "places"


def _fold(members: list[Incident], id_: str, title: str, cause: str | None) -> Incident:
    members = sorted(members, key=lambda i: i.title)
    root = members[0].root
    related = tuple(f for i in members for f in i.findings if f is not root)
    nodes = tuple(sorted({n for i in members for n in i.nodes}))
    where = "; ".join(i.title.split(":", 1)[0] for i in members[:4])
    more = f" and {len(members) - 4} more" if len(members) > 4 else ""
    if cause is None:
        kinds: dict[str, int] = {}
        for f in (root, *related):
            if f.check == "flapping":
                kind = f.detail.split(" ", 1)[0]
                kinds[kind] = kinds.get(kind, 0) + 1
        what = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))
        explanation = f"Things that keep changing state, on {len(nodes)} node(s): {what}."
    else:
        explanation = (
            f"The same cause in {len(members)} {_places(members)} ({where}{more}): {root.detail.rstrip('.')}."
        )
    return Incident(
        id=id_,
        severity=min((i.severity for i in members), key=lambda s: SEVERITY_ORDER.get(s, 9)),
        kind="pattern",
        title=title,
        node=root.node,
        nodes=nodes,
        root=root,
        related=related,
        explanation=f"{explanation} {1 + len(related)} findings in all.",
    )


def findings_of(incidents: Iterable[Incident]) -> list[Finding]:
    return [f for i in incidents for f in i.findings]

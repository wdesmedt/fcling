"""The fabric as a graph: what each node is, how they are cabled, who is attached.

Inferred from what the nodes run, not from inventory labels:

* a **gateway** has a service with two bgp-vpn instances - the WAN side of a
  stitched service;
* a **leaf** has mac-vrfs: where services meet hosts;
* a **spine** runs no services and is cabled to two or more leaves;
* **core** is anything else without services - P routers, super-spines.

Cables are LLDP adjacencies, each counted once though both ends report it,
parallel ones folded into one link with a count. A cable LLDP no longer reports
but the history saw is kept, marked ``lost``: a link that went down is the one
that matters. **Clients** are what the services' access ports face, one per
host: grouped by the name the host gives over LLDP, else by the ethernet-
segment it is multihomed over, else by the port. Neighbours LLDP hears that
are no node of the inventory and face no service are **outside**.

A fabric whose nodes share no cable with another is a **component** of its
own; each is named by what its nodes' names have in common, where they do.

For the overlays a drawing can show: each end of a cable, and each port a
client hangs off, carries the rate it sends at (``ifstats``), and each node
the services it carries.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from typing import Any

from .checks import ERROR, WARNING, Finding
from .fabric import Fabric, out_of_band, parent
from .lenses.common import Index

#: What the topology reads.
REQUIRES = ("services", "lldp", "lag", "subif", "es", "ifstats", "sys-info", "ni")

#: Top down, as drawn: the WAN core above the gateways out to it.
TIERS = ("core", "gateway", "spine", "leaf", "unclassified")


def _tiers(fabric: Fabric, index: Index) -> dict[str, str]:
    services: dict[str, list[Any]] = {}
    for (node, _), s in index.services.items():
        services.setdefault(node, []).append(s)
    tier: dict[str, str] = {}
    for node in fabric.nodes:
        mine = [s for s in services.get(node, []) if s.type in ("mac-vrf", "ip-vrf") and s.name != "mgmt"]
        if node not in services:
            tier[node] = "unclassified"
        elif any(s.gateway for s in mine):
            tier[node] = "gateway"
        elif any(s.type == "mac-vrf" for s in mine):
            tier[node] = "leaf"
        else:
            tier[node] = ""
    for node, t in tier.items():
        if t:
            continue
        leaves = {
            peer for (n, _), (peer, _) in index.cables.items() if n == node and tier.get(peer) == "leaf"
        }
        tier[node] = "spine" if len(leaves) >= 2 else "core"
    return tier


def _port_state(fabric: Fabric) -> dict[tuple[str, str], str]:
    """``(node, port)`` -> up, down, or mixed, from its subinterfaces."""
    states: dict[tuple[str, str], set[str]] = {}
    for s in fabric.get("subif"):
        states.setdefault((s.node, s.interface), set()).add("up" if s.oper == "up" else "down")
    return {k: v.pop() if len(v) == 1 else "mixed" for k, v in states.items()}


def _rates(fabric: Fabric) -> dict[tuple[str, str], tuple[float, float]]:
    """``(node, port)`` -> what it sends and receives, in bits per second: ports with two samples."""
    return {(r.node, r.name): (r.out_kbps * 1000, r.in_kbps * 1000) for r in fabric.get("ifstats")}


def _system(fabric: Fabric) -> dict[str, dict[str, str]]:
    """Each node's system0 addresses, the first of each family: what the underlay knows it by."""
    found: dict[str, dict[str, str]] = {}
    for r in fabric.get("ni"):
        if r.ni != "default" or r.interface != "system0.0":
            continue
        for prefix in r.prefixes:
            address = prefix.split("/", 1)[0]
            family = "ipv6" if ":" in address else "ipv4"
            if not address.lower().startswith("fe80"):
                found.setdefault(r.node, {}).setdefault(family, address)
    return found


def _components(nodes: Iterable[str], links: Sequence[dict[str, Any]]) -> list[list[str]]:
    root = {n: n for n in nodes}

    def find(n: str) -> str:
        while root[n] != n:
            root[n] = root[root[n]]
            n = root[n]
        return n

    for link in links:
        if link["a"] in root and link["b"] in root:
            root[find(link["b"])] = find(link["a"])
    groups: dict[str, list[str]] = {}
    for n in root:
        groups.setdefault(find(n), []).append(n)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: (-len(g), g))


def _name(members: list[str], position: int) -> str:
    """What a component's nodes' names have in common, or its rank."""
    prefix = os.path.commonprefix(members) if len(members) > 1 else ""
    prefix = prefix.rstrip("-_0123456789")
    return prefix if len(prefix) >= 2 else f"fabric {position}"


def build(fabric: Fabric, findings: Sequence[Finding] = (), name: str = "") -> dict[str, Any]:
    index = Index(fabric)
    tier = _tiers(fabric, index)
    ports = _port_state(fabric)
    rates = _rates(fabric)
    system = _system(fabric)
    platform = {r.node: r.type for r in fabric.get("sys-info")}
    asn = {r.node: r.asn for r in fabric.get("ni") if r.ni == "default" and r.asn is not None}

    def out_bps(node: str, port: str) -> float | None:
        rate = rates.get((node, port))
        return rate[0] if rate else None

    # Cables, both ends of each counted once; parallel ones folded per node pair.
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    done: set[frozenset[tuple[str, str]]] = set()

    def cable(a: tuple[str, str], b: tuple[str, str], lost: bool) -> None:
        ends = frozenset((a, b))
        if ends in done:
            return
        done.add(ends)
        (x, xp), (y, yp) = sorted((a, b))
        link = pairs.setdefault((x, y), {"a": x, "b": y, "cables": []})
        if lost:
            state = "lost"
        elif x in fabric.unreachable or y in fabric.unreachable:
            # What a lost node last said of its ports is no answer: the cable's state is not known.
            state = "unknown"
        else:
            state = _worst(ports.get((x, xp), ""), ports.get((y, yp), ""))
        link["cables"].append(
            {
                "a_port": xp,
                "b_port": yp,
                "state": state,
                # What each end sends into the cable: the a end towards b, and back.
                "a_out_bps": None if lost else out_bps(x, xp),
                "b_out_bps": None if lost else out_bps(y, yp),
            }
        )

    for (node, port), far in index.cables.items():
        cable((node, port), far, lost=False)
    for c in fabric.changes:
        before = c.get("before") or {}
        if c.get("report") != "lldp" or c.get("op") != "remove" or not before.get("system_name"):
            continue
        peer = fabric.resolve(str(before["system_name"]))
        if peer and peer != c["node"] and not out_of_band(str(before.get("interface", ""))):
            end = (c["node"], str(before.get("interface")))
            if end not in index.cables:
                cable(end, (peer, str(before.get("port_id"))), lost=True)

    # Health: findings by node, and on either end of a cable.
    by_node: dict[str, list[Finding]] = {}
    for f in findings:
        by_node.setdefault(f.node, []).append(f)
    links = []
    for link in pairs.values():
        ends = {(link["a"], c["a_port"]) for c in link["cables"]} | {
            (link["b"], c["b_port"]) for c in link["cables"]
        }
        on = [f for f in findings if (f.node, parent(f.subject)) in ends or (f.node, f.subject) in ends]
        states = {c["state"] for c in link["cables"]}
        links.append(
            {
                **link,
                "count": len(link["cables"]),
                "state": "lost" if states == {"lost"} else _worst(*states),
                "findings": _counts(on),
            }
        )

    # Clients: what the services' access ports face.
    clients: dict[str, dict[str, Any]] = {}
    for (node, ni), s in sorted(index.services.items()):
        # Hosts hang off services: the underlay's own instance and management face no client,
        # though a port of theirs whose neighbour has gone quiet looks like one.
        if s.type not in ("mac-vrf", "ip-vrf") or ni == "mgmt":
            continue
        for p in s.ports:
            port = parent(p.name)
            if (
                p.kind == "irb"
                or out_of_band(port)
                or port.startswith(("system", "lo"))
                or index.fabric_facing(node, port)
            ):
                continue
            heard = [n for n in index.heard_on(node, port) if not fabric.resolve(n)]
            key = f"lldp:{heard[0]}" if heard else f"es:{p.esi}" if p.esi else f"port:{node}:{port}"
            client = clients.setdefault(
                key, {"id": key, "name": heard[0] if heard else "", "esi": p.esi, "attachments": []}
            )
            client["attachments"].append(
                {
                    "node": node,
                    "port": port,
                    "subinterface": p.name,
                    "service": ni,
                    "kind": p.kind,
                    "vlan": p.vlan,
                    "addresses": list(p.addresses),
                    "esi": p.esi,
                    # The port towards the client: what the node sends it, and gets from it.
                    "out_bps": out_bps(node, port),
                    "in_bps": rates[(node, port)][1] if (node, port) in rates else None,
                }
            )
    # One host on a segment is one client, though LLDP is heard on only some of
    # its ports - a single-active segment's standby port hears nothing.
    named_by_esi = {
        a["esi"]: key for key, c in clients.items() if c["name"] for a in c["attachments"] if a["esi"]
    }
    for key in [k for k in clients if k.startswith("es:")]:
        owner = named_by_esi.get(clients[key]["esi"])
        if owner:
            clients[owner]["attachments"] += clients.pop(key)["attachments"]
    for client in clients.values():
        client["nodes"] = sorted({a["node"] for a in client["attachments"]})
        # What to call a host that gives no name over LLDP: its segment, else where it is.
        names = index.segment_names.get(client["esi"], ()) if client["esi"] else ()
        first = client["attachments"][0]
        client["label"] = client["name"] or (names[0] if names else f"{first['node']} {first['port']}")

    # Neighbours facing no service, and no node of ours: outside the fabric.
    named = {c["name"] for c in clients.values()}
    outside: dict[str, set[str]] = {}
    for (node, port), names in index.heard.items():
        for n in names:
            if not fabric.resolve(n) and n not in named and not out_of_band(port):
                outside.setdefault(n, set()).add(node)

    segments: dict[str, dict[str, Any]] = {}
    for es in fabric.get("es"):
        if not es.esi:
            continue
        seg = segments.setdefault(
            es.esi,
            {"esi": es.esi, "names": [], "mode": es.mh_mode, "type": es.type, "nodes": [], "next_hops": []},
        )
        if es.name and es.name not in seg["names"]:
            seg["names"].append(es.name)
        seg["nodes"].append({"node": es.node, "interfaces": list(es.interfaces), "oper": es.oper})
        seg["next_hops"] = sorted({*seg["next_hops"], *(nh.address for nh in es.next_hops)})

    components = _components(fabric.nodes, links)
    attached = [c for c in components if len(c) > 1]
    loose = sorted(n for c in components if len(c) == 1 for n in c)
    return {
        "nodes": [
            {
                "name": n,
                "tier": tier[n],
                "type": platform.get(n, ""),
                "ipv4": system.get(n, {}).get("ipv4", ""),
                "ipv6": system.get(n, {}).get("ipv6", ""),
                "asn": asn.get(n),
                "unreachable": fabric.unreachable.get(n, ""),
                "findings": _counts(by_node.get(n, [])),
                "health": _health(by_node.get(n, [])),
                "services": sorted(
                    ni
                    for (node, ni), s in index.services.items()
                    if node == n and s.type in ("mac-vrf", "ip-vrf") and ni != "mgmt"
                ),
            }
            for n in fabric.nodes
        ],
        "links": sorted(links, key=lambda x: (x["a"], x["b"])),
        "clients": sorted(clients.values(), key=lambda c: (c["nodes"], c["id"])),
        "segments": sorted(segments.values(), key=lambda s: s["esi"]),
        "outside": [{"name": n, "nodes": sorted(v)} for n, v in sorted(outside.items())],
        "components": [
            {"name": name if len(attached) == 1 and name else _name(c, i + 1), "nodes": c}
            for i, c in enumerate(attached)
        ]
        + ([{"name": "unattached", "nodes": loose}] if loose else []),
    }


_ORDER = {"": 0, "up": 1, "unknown": 2, "mixed": 3, "down": 4, "lost": 5}


def _worst(*states: str) -> str:
    return max(states, key=lambda s: _ORDER.get(s, 0)) if states else ""


def _counts(findings: Sequence[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return counts


def _health(findings: Sequence[Finding]) -> str:
    """Information is shown, but marks nothing as unhealthy."""
    if any(f.severity == ERROR for f in findings):
        return "error"
    return "warning" if any(f.severity == WARNING for f in findings) else "ok"

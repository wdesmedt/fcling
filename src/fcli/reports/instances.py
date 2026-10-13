"""ni: network-instances, their overlay and the subinterfaces in them.

The parts of a network-instance are read path by path rather than as the
whole ``/network-instance`` subtree, which also carries every BGP RIB.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from ..gnmi.client import DataType
from ..report import Column, NodeState, Read, Record, as_int, as_list, dig, report
from ..tree import Tree
from .common import NI_INTERFACES, SUBINTERFACES, joined

NI_READS = (
    Read("/network-instance[name=*]/type", DataType.ALL),
    Read("/network-instance[name=*]/oper-state"),
    NI_INTERFACES,
    Read("/network-instance[name=*]/vxlan-interface", DataType.ALL),
    Read("/network-instance[name=*]/protocols/bgp/router-id", DataType.ALL),
    Read("/network-instance[name=*]/protocols/bgp-vpn", DataType.ALL),
    Read("/network-instance[name=*]/protocols/bgp-evpn", DataType.ALL),
    # Route-targets a policy sets are only known from the policy.
    Read("/routing-policy", DataType.ALL),
)


class BgpVpnInstance(BaseModel):
    """One bgp-vpn instance: what it imports and exports with.

    A gateway has two, one per side, so they are kept apart rather than merged
    into a set the leaves would never match.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    #: Route-targets, or where a policy sets them and names none, the policy.
    import_rts: tuple[str, ...] = ()
    export_rts: tuple[str, ...] = ()
    rd: str = ""


def stitched(vpn: Iterable[BgpVpnInstance]) -> bool:
    """Two bgp-vpn instances that carry route-targets: a gateway's DC side and its
    WAN side. An instance with no targets advertises nothing, and stitches nothing."""
    return sum(1 for inst in vpn if inst.import_rts or inst.export_rts) > 1


class Instance(BaseModel):
    """A network-instance as several reports need it."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: str = ""
    oper: str = ""
    router_id: str = ""
    #: The vxlan-interfaces bound to it.
    overlays: tuple[str, ...] = ()
    #: The EVI of each bgp-evpn instance; a gateway has two.
    evis: tuple[str, ...] = ()
    vpn: tuple[BgpVpnInstance, ...] = ()
    #: The subinterfaces in it.
    interfaces: tuple[str, ...] = ()

    @property
    def import_rts(self) -> tuple[str, ...]:
        return tuple(sorted({rt for inst in self.vpn for rt in inst.import_rts}))

    @property
    def export_rts(self) -> tuple[str, ...]:
        return tuple(sorted({rt for inst in self.vpn for rt in inst.export_rts}))


def _referenced_sets(node: Any) -> list[str]:
    """Every extended-community-set named anywhere under *node*."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("extended-community-set", "referenced-sets"):
                found.extend(str(v) for v in as_list(value) if isinstance(v, str | int))
            else:
                found.extend(_referenced_sets(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_referenced_sets(value))
    return found


def _policy_route_targets(policy_tree: dict[str, Any], names: list[str], direction: str) -> tuple[str, ...]:
    """The route-targets policies *names* give a bgp-vpn instance.

    With an export policy SR Linux advertises whatever route-targets its
    accepting statements add, and the instance's own ``export-rt`` no longer
    applies; an import policy takes in what its accepting statements match.
    """
    sets = {
        str(s.get("name")): [str(m) for m in as_list(s.get("member"))]
        for s in as_list(policy_tree.get("extended-community-set"))
        if isinstance(s, dict)
    }
    policies = {str(p.get("name")): p for p in as_list(policy_tree.get("policy")) if isinstance(p, dict)}
    targets: set[str] = set()
    for name in names:
        policy = policies.get(name) or {}
        for statement in [*as_list(policy.get("statement")), {"action": policy.get("default-action") or {}}]:
            if not isinstance(statement, dict):
                continue
            action = statement.get("action") or {}
            if str(action.get("policy-result") or "").endswith("reject"):
                continue
            where = (action.get("bgp") or {}) if direction == "export" else (statement.get("match") or {})
            for set_name in _referenced_sets(where):
                targets.update(
                    m.removeprefix("target:") for m in sets.get(set_name, []) if m.startswith("target:")
                )
    return tuple(sorted(targets))


def _route_targets(inst: dict[str, Any], direction: str, policy_tree: dict[str, Any]) -> tuple[str, ...]:
    policy = inst.get(f"{direction}-policy")
    if policy:
        names = [str(p) for p in as_list(policy)]
        return _policy_route_targets(policy_tree, names, direction) or tuple(names)
    targets = {
        str(rt.get("target") if isinstance(rt, dict) else rt).removeprefix("target:")
        for rt in as_list(dig(inst, "route-target", f"{direction}-rt"))
        if (rt.get("target") if isinstance(rt, dict) else rt)
    }
    return tuple(sorted(targets))


def _evis(ni: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(inst["evi"])
        for inst in as_list(dig(ni, "protocols", "bgp-evpn", "bgp-instance"))
        if isinstance(inst, dict) and inst.get("evi") not in (None, "")
    )


def instances(tree: Tree) -> list[Instance]:
    policy_tree = tree.get("/routing-policy") or {}
    out = []
    for m in tree.select("/network-instance"):
        ni = m.value
        vpn = tuple(
            BgpVpnInstance(
                id=as_int(inst.get("id")) or index,
                import_rts=_route_targets(inst, "import", policy_tree),
                export_rts=_route_targets(inst, "export", policy_tree),
                rd=str(dig(inst, "route-distinguisher", "rd") or ""),
            )
            for index, inst in enumerate(as_list(dig(ni, "protocols", "bgp-vpn", "bgp-instance")), start=1)
            if isinstance(inst, dict)
        )
        out.append(
            Instance(
                name=str(ni.get("name") or ""),
                type=str(ni.get("type") or ""),
                oper=str(ni.get("oper-state") or ""),
                router_id=str(dig(ni, "protocols", "bgp", "router-id") or ""),
                overlays=tuple(
                    str(v.get("name") or "")
                    for v in as_list(ni.get("vxlan-interface"))
                    if isinstance(v, dict)
                ),
                evis=_evis(ni),
                vpn=vpn,
                interfaces=tuple(
                    str(i["name"])
                    for i in as_list(ni.get("interface"))
                    if isinstance(i, dict) and i.get("name")
                ),
            )
        )
    return out


def subinterfaces(tree: Tree) -> dict[str, dict[str, Any]]:
    """``{"ethernet-1/1.0": subinterface payload}``."""
    return {
        f"{m.keys.get('interface')}.{m.value.get('index', '')}": m.value
        for m in tree.select("/interface/subinterface")
        if isinstance(m.value, dict)
    }


class NiInterface(Record):
    """A network-instance and one subinterface in it; an empty one is a row of its own."""

    key: ClassVar[tuple[str, ...]] = ("ni", "interface")

    ni: str
    type: str = ""
    oper: str = ""
    router_id: str = ""
    overlays: tuple[str, ...] = ()
    evis: tuple[str, ...] = ()
    import_rts: tuple[str, ...] = ()
    export_rts: tuple[str, ...] = ()
    #: Each bgp-vpn instance on its own: a gateway's WAN side has its own targets.
    vpn: tuple[BgpVpnInstance, ...] = ()
    interface: str = ""
    interface_oper: str = ""
    prefixes: tuple[str, ...] = ()
    mtu: int | None = None
    vlan: int | None = None
    #: An irb's other network-instance: the ip-vrf a mac-vrf routes into, or back.
    associated: tuple[str, ...] = ()


@report(
    "ni",
    "Network instances and their interfaces",
    record=NiInterface,
    reads=[*NI_READS, SUBINTERFACES],
    columns=[
        Column("NI", "ni"),
        Column("oper", "oper"),
        Column("type", "type"),
        Column("router-id", "router_id"),
        Column("vxlan-itf", lambda r: joined(r.overlays)),
        Column("evi", lambda r: joined(r.evis)),
        Column("In-RT", lambda r: joined(r.import_rts)),
        Column("Out-RT", lambda r: joined(r.export_rts)),
        Column("Subitf", "interface"),
        Column("assoc-ni", lambda r: " ".join(r.associated)),
        Column("if-oper", "interface_oper"),
        Column("ip-prefix", lambda r: list(r.prefixes)),
        Column("mtu", "mtu"),
        Column("vlan", "vlan"),
    ],
)
def ni(node: NodeState) -> Iterator[NiInterface]:
    nis = instances(node.tree)
    details = subinterfaces(node.tree)
    bound: dict[str, list[str]] = {}
    for inst in nis:
        for itf in inst.interfaces:
            bound.setdefault(itf, []).append(inst.name)
    ni_itf_oper = {
        (str(m.keys.get("network-instance")), str(m.value.get("name"))): m.value.get("oper-state")
        for m in node.tree.select("/network-instance/interface")
        if isinstance(m.value, dict)
    }
    for inst in nis:
        base = NiInterface(
            node=node.name,
            ni=inst.name,
            type=inst.type,
            oper=inst.oper,
            router_id=inst.router_id,
            overlays=inst.overlays,
            evis=inst.evis,
            import_rts=inst.import_rts,
            export_rts=inst.export_rts,
            vpn=inst.vpn,
        )
        if not inst.interfaces:
            yield base
        for itf in inst.interfaces:
            si = details.get(itf, {})
            yield base.model_copy(
                update={
                    "interface": itf,
                    "interface_oper": str(si.get("oper-state") or ni_itf_oper.get((inst.name, itf)) or ""),
                    "prefixes": tuple(
                        str(a["ip-prefix"])
                        for family in ("ipv4", "ipv6")
                        for a in as_list(dig(si, family, "address"))
                        if isinstance(a, dict) and a.get("ip-prefix")
                    ),
                    "mtu": as_int(si.get("l2-mtu") if "l2-mtu" in si else si.get("ip-mtu")),
                    "vlan": as_int(dig(si, "vlan", "encap", "single-tagged", "vlan-id")),
                    "associated": tuple(o for o in bound.get(itf, []) if o != inst.name)
                    if itf.startswith("irb")
                    else (),
                }
            )

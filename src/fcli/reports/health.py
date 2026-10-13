"""What keeps a fabric alive underneath the services: bfd, isis, ospf,
resources, components, transceivers.

Most of this state only exists where something is configured - IS-IS on a
node that runs eBGP, DOM in a containerlab port - and a path a node does not
know reads as empty, so these reports are often empty rather than failing.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from ..gnmi.client import DataType, SubscriptionMode
from ..report import Column, NodeState, Read, Record, as_int, as_list, dig, report
from .common import joined


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _enum(value: Any) -> str:
    return _text(value).lower()


def _float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [v for v in as_list(value) if isinstance(v, dict)]


def _address(value: Any) -> str:
    """An address leaf, empty where the device fills in the unspecified one."""
    text = _text(value)
    return "" if text in ("::", "0.0.0.0") else text


# --------------------------------------------------------------------------- #
# bfd
# --------------------------------------------------------------------------- #


class BfdSession(Record):
    key: ClassVar[tuple[str, ...]] = ("ni", "local_address", "remote_address")

    ni: str
    local_address: str = ""
    remote_address: str = ""
    state: str = ""
    remote_state: str = ""
    remote_discriminator: int | None = None
    #: The interface of a session to a link-local address.
    interface: str = ""
    #: The protocols that asked for it: ``BGP``, ``ISIS``, ``STATIC``...
    protocols: tuple[str, ...] = ()
    last_transition: str = ""
    failures: int = 0
    local_diagnostic: str = ""
    remote_diagnostic: str = ""
    #: Microseconds.
    tx_interval: int | None = None
    rx_interval: int | None = None
    multiplier: int | None = None


def _usecs(value: int | None) -> str:
    return f"{value // 1000}ms" if value else ""


def _protocols(value: Any) -> Iterable[str]:
    """``subscribed-protocols`` is a space-separated string, or a list of them."""
    for item in as_list(value):
        yield from str(item).replace(",", " ").split()


@report(
    "bfd",
    "BFD sessions and how often they failed",
    record=BfdSession,
    reads=[Read("/bfd/network-instance[name=*]/peer", mode=SubscriptionMode.SAMPLE, sample_interval=10)],
    columns=[
        Column("NI", "ni"),
        Column("remote", "remote_address"),
        Column("state", "state"),
        Column("remote-state", "remote_state"),
        Column("interface", "interface"),
        Column("local", "local_address"),
        Column("clients", lambda s: joined(s.protocols)),
        Column("last-change", "last_transition"),
        Column("failures", "failures"),
        Column("diag", lambda s: s.local_diagnostic if s.local_diagnostic != "no_diagnostic" else ""),
        Column("tx/rx", lambda s: f"{_usecs(s.tx_interval)}/{_usecs(s.rx_interval)}".strip("/")),
    ],
)
def bfd(node: NodeState) -> Iterator[BfdSession]:
    for m in node.tree.select("/bfd/network-instance/peer"):
        peer = m.value
        yield BfdSession(
            node=node.name,
            ni=_text(m.keys.get("network-instance")),
            local_address=_text(peer.get("local-address")),
            remote_address=_text(peer.get("remote-address")),
            state=_enum(peer.get("session-state")),
            remote_state=_enum(peer.get("remote-session-state")),
            remote_discriminator=as_int(peer.get("remote-discriminator")),
            interface=_text(peer.get("ipv6-link-local-interface")),
            protocols=tuple(p.upper() for p in _protocols(peer.get("subscribed-protocols"))),
            last_transition=_text(peer.get("last-state-transition")),
            failures=as_int(peer.get("failure-transitions")) or 0,
            local_diagnostic=_enum(peer.get("local-diagnostic-code")),
            remote_diagnostic=_enum(peer.get("remote-diagnostic-code")),
            tx_interval=as_int(peer.get("active-transmit-interval")),
            rx_interval=as_int(peer.get("active-receive-interval")),
            multiplier=as_int(peer.get("remote-multiplier")),
        )


# --------------------------------------------------------------------------- #
# isis
# --------------------------------------------------------------------------- #


class IsisAdjacency(Record):
    """An IS-IS interface and one adjacency on it; one without any is a row of its own."""

    key: ClassVar[tuple[str, ...]] = ("ni", "instance", "interface", "system_id", "level")

    ni: str
    instance: str
    interface: str
    oper: str = ""
    passive: bool = False
    circuit_type: str = ""
    system_id: str = ""
    hostname: str = ""
    #: ``L1`` or ``L2``.
    level: str = ""
    state: str = ""
    down_reason: str = ""
    ipv4: str = ""
    ipv6: str = ""
    last_transition: str = ""
    transitions: int = 0


@report(
    "isis",
    "IS-IS interfaces and adjacencies",
    record=IsisAdjacency,
    reads=[
        # ``all``, so the interface's own config - passive, circuit-type - comes along.
        Read(
            "/network-instance[name=*]/protocols/isis/instance[name=*]/interface[interface-name=*]",
            DataType.ALL,
        )
    ],
    columns=[
        Column("NI", "ni"),
        Column("instance", "instance"),
        Column("interface", "interface"),
        Column("oper", "oper"),
        Column("circuit", lambda a: "passive" if a.passive else a.circuit_type),
        Column("neighbor", lambda a: a.hostname or a.system_id),
        Column("system-id", "system_id"),
        Column("level", "level"),
        Column("state", "state"),
        Column("nbr-ipv4", "ipv4"),
        Column("last-change", "last_transition"),
        Column("flaps", lambda a: a.transitions if a.system_id else None),
        Column("down-reason", "down_reason"),
    ],
)
def isis(node: NodeState) -> Iterator[IsisAdjacency]:
    for m in node.tree.select("/network-instance/protocols/isis/instance/interface"):
        itf = m.value
        base = IsisAdjacency(
            node=node.name,
            ni=_text(m.keys.get("network-instance")),
            instance=_text(m.keys.get("instance")),
            interface=_text(itf.get("interface-name")),
            oper=_enum(itf.get("oper-state")),
            passive=bool(itf.get("passive")),
            circuit_type=_enum(itf.get("circuit-type")),
        )
        adjacencies = _dicts(itf.get("adjacency"))
        if not adjacencies:
            yield base
        for adj in adjacencies:
            yield base.model_copy(
                update={
                    "system_id": _text(adj.get("neighbor-system-id")),
                    "hostname": _text(adj.get("neighbor-hostname")),
                    "level": _enum(adj.get("adjacency-level")).upper(),
                    "state": _enum(adj.get("state")),
                    "down_reason": _enum(adj.get("down-reason")),
                    "ipv4": _address(adj.get("neighbor-ipv4")),
                    "ipv6": _address(adj.get("neighbor-ipv6")),
                    "last_transition": _text(adj.get("last-up-down-transition")),
                    "transitions": as_int(adj.get("up-down-transitions")) or 0,
                }
            )


# --------------------------------------------------------------------------- #
# ospf
# --------------------------------------------------------------------------- #


class OspfNeighbor(Record):
    """An OSPF interface and one neighbor on it; one without any is a row of its own."""

    key: ClassVar[tuple[str, ...]] = ("ni", "instance", "area", "interface", "router_id")

    ni: str
    instance: str
    area: str
    interface: str
    oper: str = ""
    passive: bool = False
    interface_type: str = ""
    router_id: str = ""
    address: str = ""
    state: str = ""
    priority: int | None = None
    last_established: str = ""
    state_changes: int = 0


@report(
    "ospf",
    "OSPF interfaces and neighbors",
    record=OspfNeighbor,
    reads=[
        Read(
            "/network-instance[name=*]/protocols/ospf/instance[name=*]/area[area-id=*]/interface[interface-name=*]",
            DataType.ALL,
        )
    ],
    columns=[
        Column("NI", "ni"),
        Column("instance", "instance"),
        Column("area", "area"),
        Column("interface", "interface"),
        Column("oper", "oper"),
        Column("type", lambda n: "passive" if n.passive else n.interface_type),
        Column("router-id", "router_id"),
        Column("address", "address"),
        Column("state", "state"),
        Column("last-established", "last_established"),
        Column("changes", lambda n: n.state_changes if n.router_id else None),
    ],
)
def ospf(node: NodeState) -> Iterator[OspfNeighbor]:
    for m in node.tree.select("/network-instance/protocols/ospf/instance/area/interface"):
        itf = m.value
        base = OspfNeighbor(
            node=node.name,
            ni=_text(m.keys.get("network-instance")),
            instance=_text(m.keys.get("instance")),
            area=_text(m.keys.get("area")),
            interface=_text(itf.get("interface-name")),
            oper=_enum(itf.get("oper-state")),
            passive=bool(itf.get("passive")),
            interface_type=_enum(itf.get("interface-type")),
        )
        neighbors = _dicts(itf.get("neighbor"))
        if not neighbors:
            yield base
        for nbr in neighbors:
            yield base.model_copy(
                update={
                    "router_id": _text(nbr.get("router-id")),
                    "address": _text(nbr.get("address")),
                    "state": _enum(nbr.get("adjacency-state")),
                    "priority": as_int(nbr.get("priority")),
                    "last_established": _text(nbr.get("last-established-time")),
                    "state_changes": as_int(nbr.get("state-changes")) or 0,
                }
            )


# --------------------------------------------------------------------------- #
# resources
# --------------------------------------------------------------------------- #


class Resource(Record):
    key: ClassVar[tuple[str, ...]] = ("component", "name")
    volatile: ClassVar[tuple[str, ...]] = ("used_percent", "used", "free")

    #: ``control A``, ``linecard 1/0``.
    component: str
    #: ``cpu``, ``memory``, or a datapath table.
    name: str
    used_percent: int | None = None
    used: int | None = None
    free: int | None = None
    #: The utilization the node raises its own alarm at, where it has one.
    threshold: int | None = None


@report(
    "resources",
    "CPU, memory and forwarding-table utilization",
    record=Resource,
    reads=[
        Read(
            "/platform/control[slot=*]/cpu[index=all]/total", mode=SubscriptionMode.SAMPLE, sample_interval=30
        ),
        Read("/platform/control[slot=*]/memory", mode=SubscriptionMode.SAMPLE, sample_interval=30),
        Read(
            "/platform/linecard[slot=*]/forwarding-complex[name=*]/datapath",
            mode=SubscriptionMode.SAMPLE,
            sample_interval=30,
        ),
        # ``all``: a config Get answers only what someone configured, not the defaults.
        Read("/platform/resource-monitoring/datapath", DataType.ALL),
    ],
    columns=[
        Column("component", "component"),
        Column("resource", "name"),
        Column("used-%", "used_percent"),
        Column("used", "used"),
        Column("free", "free"),
    ],
)
def resources(node: NodeState) -> Iterator[Resource]:
    tree = node.tree
    for m in tree.select("/platform/control/cpu"):
        total = m.value.get("total") or {}
        if not total:
            continue
        yield Resource(
            node=node.name,
            component=f"control {m.keys.get('control')}",
            name="cpu",
            # The five-minute average: the instant value is too noisy for a threshold.
            used_percent=as_int(total.get("average-5", total.get("instant"))),
        )
    for m in tree.select("/platform/control/memory"):
        mem = m.value
        if not isinstance(mem, dict) or not mem:
            continue
        physical, free = as_int(mem.get("physical")), as_int(mem.get("free"))
        yield Resource(
            node=node.name,
            component=f"control {m.keys.get('control')}",
            name="memory",
            used_percent=as_int(mem.get("utilization")),
            used=(physical - free) if physical is not None and free is not None else None,
            free=free,
        )
    # Releases before ``upper-threshold-set`` called it ``rising-threshold-log``.
    thresholds = {
        _enum(item.get("name")): as_int(item.get("upper-threshold-set", item.get("rising-threshold-log")))
        for engine in (tree.get("/platform/resource-monitoring/datapath") or {}).values()
        if isinstance(engine, dict)
        for item in _dicts(engine.get("resource"))
    }
    for m in tree.select("/platform/linecard/forwarding-complex"):
        where = f"linecard {m.keys.get('linecard')}/{m.keys.get('forwarding-complex')}"
        # ``asic`` on hardware, ``xdp`` on the virtual datapath: the same list either way.
        for engine in (m.value.get("datapath") or {}).values():
            if not isinstance(engine, dict):
                continue
            for item in _dicts(engine.get("resource")):
                used = as_int(item.get("used-percent"))
                entries, free = as_int(item.get("used-entries")), as_int(item.get("free-entries"))
                if used is None and entries is None:
                    continue  # a table this datapath does not count
                if used is None and entries is not None and free is not None and entries + free > 0:
                    used = (entries * 100) // (entries + free)
                name = _enum(item.get("name"))
                yield Resource(
                    node=node.name,
                    component=where,
                    name=name,
                    used_percent=used,
                    used=entries,
                    free=free,
                    threshold=thresholds.get(name),
                )


# --------------------------------------------------------------------------- #
# components
# --------------------------------------------------------------------------- #


class Component(Record):
    key: ClassVar[tuple[str, ...]] = ("kind", "id")

    #: ``control``, ``linecard``, ``fabric``, ``fan-tray``, ``power-supply``.
    kind: str
    id: str
    oper: str = ""
    health: str = ""
    type: str = ""
    serial_number: str = ""


#: Hardware lists and the key each is indexed by. ``fabric`` only exists on a
#: modular chassis; a fixed-form node rejects the path, which reads as empty.
_COMPONENTS = (
    ("control", "slot"),
    ("linecard", "slot"),
    ("fabric", "slot"),
    ("fan-tray", "id"),
    ("power-supply", "id"),
)

#: Only the leaves a component is read by: a line card's subtree holds its
#: forwarding tables - a hundred thousand leaves on a large EVPN fabric - and a
#: control card's or a fan's its sensors, which a stream would send every tick.
_CARD_LEAVES = ("oper-state", "healthz", "type", "serial-number")


@report(
    "components",
    "Cards, fabric modules, fans and power supplies",
    record=Component,
    reads=[
        Read(f"/platform/{kind}[{key}=*]/{leaf}", DataType.ALL)
        for kind, key in _COMPONENTS
        for leaf in _CARD_LEAVES
    ],
    columns=[
        Column("kind", "kind"),
        Column("id", "id"),
        Column("oper", "oper"),
        Column("health", "health"),
        Column("type", "type"),
        Column("serial", "serial_number"),
    ],
)
def components(node: NodeState) -> Iterator[Component]:
    for kind, key in _COMPONENTS:
        for m in node.tree.select(f"/platform/{kind}"):
            item = m.value
            yield Component(
                node=node.name,
                kind=kind,
                id=_text(item.get(key)),
                oper=_enum(item.get("oper-state")),
                health=_enum(dig(item, "healthz", "status")),
                type=_text(item.get("type")),
                serial_number=_text(item.get("serial-number")),
            )


# --------------------------------------------------------------------------- #
# transceivers
# --------------------------------------------------------------------------- #


class TransceiverChannel(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    #: dBm.
    input_power: float | None = None
    output_power: float | None = None
    #: mA.
    laser_bias: float | None = None


class Transceiver(Record):
    key: ClassVar[tuple[str, ...]] = ("interface",)
    volatile: ClassVar[tuple[str, ...]] = (
        "temperature",
        "voltage",
        "channels.input_power",
        "channels.output_power",
        "channels.laser_bias",
    )

    interface: str
    oper: str = ""
    down_reason: str = ""
    form_factor: str = ""
    pmd: str = ""
    vendor: str = ""
    part_number: str = ""
    serial_number: str = ""
    #: Celsius.
    temperature: float | None = None
    voltage: float | None = None
    channels: tuple[TransceiverChannel, ...] = ()
    #: The DOM thresholds crossed: ``temperature high``, ``input-power (lane 1) low``.
    alarms: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


#: The DOM measurements a transceiver carries thresholds for.
_DOM_MEASURES = ("temperature", "voltage", "input-power", "output-power", "laser-bias-current")


def _crossed(container: Any, measure: str) -> tuple[list[str], list[str]]:
    alarms, warnings = [], []
    container = container if isinstance(container, dict) else {}
    for edge in ("high", "low"):
        if container.get(f"{edge}-alarm-condition") is True:
            alarms.append(f"{measure} {edge}")
        elif container.get(f"{edge}-warning-condition") is True:
            warnings.append(f"{measure} {edge}")
    return alarms, warnings


def _dbm(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else ""


@report(
    "transceivers",
    "Optics with their light levels and DOM alarms",
    record=Transceiver,
    reads=[Read("/interface[name=*]/transceiver", mode=SubscriptionMode.SAMPLE, sample_interval=30)],
    columns=[
        Column("interface", "interface"),
        Column("oper", "oper"),
        Column("form-factor", "form_factor"),
        Column("pmd", "pmd"),
        Column("vendor", "vendor"),
        Column("part", "part_number"),
        Column("temp-C", lambda t: f"{t.temperature:.1f}" if t.temperature is not None else ""),
        Column("rx-dBm", lambda t: joined([_dbm(c.input_power) for c in t.channels])),
        Column("tx-dBm", lambda t: joined([_dbm(c.output_power) for c in t.channels])),
        Column("alarms", lambda t: joined(t.alarms)),
        Column("warnings", lambda t: joined(t.warnings)),
    ],
)
def transceivers(node: NodeState) -> Iterator[Transceiver]:
    for m in node.tree.select("/interface/transceiver"):
        optic = m.value
        if not isinstance(optic, dict) or not optic or _enum(optic.get("oper-down-reason")) == "not-present":
            continue
        alarms: list[str] = []
        warnings: list[str] = []
        for measure in _DOM_MEASURES:
            a, w = _crossed(optic.get(measure), measure)
            alarms += a
            warnings += w
        channels = []
        for channel in _dicts(optic.get("channel")):
            index = as_int(channel.get("index")) or 0
            for measure in ("input-power", "output-power", "laser-bias-current"):
                a, w = _crossed(channel.get(measure), f"{measure} (lane {index})")
                alarms += a
                warnings += w
            channels.append(
                TransceiverChannel(
                    index=index,
                    input_power=_float(dig(channel, "input-power", "latest-value")),
                    output_power=_float(dig(channel, "output-power", "latest-value")),
                    laser_bias=_float(dig(channel, "laser-bias-current", "latest-value")),
                )
            )
        yield Transceiver(
            node=node.name,
            interface=_text(m.keys.get("interface")),
            oper=_enum(optic.get("oper-state")),
            down_reason=_enum(optic.get("oper-down-reason")),
            form_factor=_enum(optic.get("form-factor")),
            pmd=_enum(optic.get("ethernet-pmd")),
            vendor=_text(optic.get("vendor")),
            part_number=_text(optic.get("vendor-part-number")),
            serial_number=_text(optic.get("serial-number")),
            temperature=_float(dig(optic, "temperature", "latest-value")),
            voltage=_float(dig(optic, "voltage", "latest-value")),
            channels=tuple(channels),
            alarms=tuple(dict.fromkeys(alarms)),
            warnings=tuple(dict.fromkeys(warnings)),
        )

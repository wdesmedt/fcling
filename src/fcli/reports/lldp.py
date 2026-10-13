"""lldp: the neighbours each interface hears."""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

from ..report import Column, NodeState, Read, Record, report


class LldpNeighbor(Record):
    key: ClassVar[tuple[str, ...]] = ("interface", "chassis_id")

    interface: str
    chassis_id: str = ""
    #: The system name it advertises, which is how it is matched back to a
    #: node of the inventory.
    system_name: str = ""
    port_id: str = ""
    port_description: str = ""


@report(
    "lldp",
    "LLDP neighbors",
    record=LldpNeighbor,
    reads=[Read("/system/lldp/interface[name=*]/neighbor")],
    columns=[
        Column("interface", "interface"),
        Column("Nbr-System", "system_name"),
        Column("Nbr-port", "port_id"),
        Column("Nbr-port-desc", "port_description"),
    ],
)
def lldp(node: NodeState) -> Iterator[LldpNeighbor]:
    for match in node.tree.select("/system/lldp/interface/neighbor"):
        nbr = match.value
        yield LldpNeighbor(
            node=node.name,
            interface=str(match.keys.get("interface") or ""),
            chassis_id=str(nbr.get("chassis-id") or nbr.get("id") or ""),
            system_name=str(nbr.get("system-name") or ""),
            port_id=str(nbr.get("port-id") or ""),
            port_description=str(nbr.get("port-description") or ""),
        )

"""The report definitions. Importing this package registers them all."""

from ..report import REGISTRY
from . import bgp, bgp_rib, health, instances, interfaces, layer2, lldp, policy, routing, services, system  # noqa: F401 - registers the reports

__all__ = ["REGISTRY"]

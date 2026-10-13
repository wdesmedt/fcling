"""A :class:`~fcli.fabric.Fabric` out of the recordings of one release: every report of every node."""

from __future__ import annotations

from pathlib import Path

from fcli.checks import required
from fcli.fabric import Fabric
from fcli.inventory import Node
from fcli.report import Record
from fcli.reports import REGISTRY

from .replay import load, node_state
from .test_release_matrix import PARAMS, PORTED


def recorded_fabric(paths: list[Path]) -> Fabric:
    records: dict[str, list[Record]] = {}
    nodes = []
    for path in paths:
        name = load(path)["node"]
        nodes.append(Node(name=name, address=name, virtual=True))
        for report in required():
            old = PORTED.get(report)
            if old is None:
                continue
            records.setdefault(report, []).extend(
                REGISTRY[report].run(node_state(path, old), **PARAMS.get(report, {}))
            )
    return Fabric.of(nodes, records)

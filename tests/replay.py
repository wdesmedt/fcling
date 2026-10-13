"""Filling a tree from the release recordings the original fcli made.

Each recording holds, per report, the Gets the old getters made and the rows
they rendered. The Get responses are payloads keyed by the path the device
echoed - ``platform/chassis``, ``interface``, ``/`` - which is exactly what a
gNMI update says, so they merge into a :class:`~fcli.tree.Tree` as they are.
"""

from __future__ import annotations

import gzip
import json
from functools import cache
from pathlib import Path
from typing import Any

from fcli.report import NodeState
from fcli.tree import Tree

RELEASES = Path(__file__).parent / "fixtures" / "releases"


def recordings() -> list[Path]:
    return sorted(RELEASES.glob("*/*.json.gz"))


@cache
def load(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt") as f:
        return json.load(f)


def recorded(path: Path, report: str) -> dict[str, Any] | None:
    return next((r for r in load(path)["reports"] if r["report"] == report), None)


def _tree(calls: list[dict[str, Any]]) -> Tree:
    tree = Tree()
    for call in calls:
        # A call the node rejected - a path it does not know - recorded its error instead.
        for item in call.get("response", ()):
            for at, value in item.items():
                tree.update("" if at == "/" else at, value)
    return tree


#: Reports recorded as two samples, the first call of each pair the earlier.
SAMPLED = {"ifstats"}


def node_state(path: Path, report: str) -> NodeState:
    """The tree the recorded Gets of *report* would have filled."""
    rec = recorded(path, report)
    calls = rec["calls"] if rec else []
    data = load(path)
    models = {m["name"]: m.get("version", "") for m in data["capabilities"].get("supported_models", [])}
    if report in SAMPLED and len(calls) >= 2:
        half = len(calls) // 2
        return NodeState(
            name=data["node"],
            tree=_tree(calls[half:]),
            models=models,
            previous=_tree(calls[:half]),
            elapsed=float(data.get("ifstats_interval") or 1),
        )
    return NodeState(name=data["node"], tree=_tree(calls), models=models)


# --------------------------------------------------------------------------- #
# fcling's own recordings (tests/capture.py)
# --------------------------------------------------------------------------- #

READS = Path(__file__).parent / "fixtures" / "reads"


def reads_recordings() -> list[Path]:
    return sorted(READS.glob("*/*.json.gz"))


def _apply(tree: Tree, call: dict[str, Any]) -> None:
    for n in call.get("notifications", ()):
        for path in n["deletes"]:
            tree.delete(path)
        for path, value in n["updates"]:
            tree.update(path, value)


def reads_state(data: dict[str, Any], entry: dict[str, Any]) -> NodeState:
    """The tree a recorded report's own Gets filled, and its earlier sample if it took one."""
    tree, previous = Tree(), None
    for call in entry["calls"]:
        if call.get("sample"):
            previous = previous or Tree()
            _apply(previous, call)
        else:
            _apply(tree, call)
    return NodeState(
        name=data["node"],
        tree=tree,
        models=data["models"],
        labels=data.get("labels", {}),
        previous=previous,
        elapsed=entry.get("elapsed"),
    )

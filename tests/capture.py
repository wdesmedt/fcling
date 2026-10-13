"""``python -m tests.capture``: record what fcling itself asks a live node.

The release recordings in ``fixtures/releases`` hold the Gets the original
fcli made. They check how each release shapes its payloads, but not the paths
fcling sends: a leaf a release renamed is refused there, while the old,
wider Get still carries it. This records fcling's own exchange instead, on one
connection per node:

* every report's Gets as the collector makes them - its declared reads, then
  what its ``follow`` asks for - with each answer, or the error that refused it;
* the engine's subscription plan, Subscribe by Subscribe: whether each one
  synced, and which elements a node refused.

``tests/test_release_reads.py`` replays them. Re-record after changing what a
report reads::

    python -m tests.capture -t lab.clab.yml --node leaf1 --node spine1

Only gNMI Capabilities, Get and Subscribe are used: nothing is configured.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import gzip
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fcli import reports as _reports  # noqa: F401 - fills the registry
from fcli.collect import UNKNOWN_ELEMENT, _sample
from fcli.engine import MAX_SUBSCRIBED_PATHS, SYNC_TIMEOUT, Plan
from fcli.gnmi import path as gpath
from fcli.gnmi.client import DataType, GnmiClient, Notification, RpcFailed, Subscription
from fcli.inventory import Node, from_clab, from_file
from fcli.report import REGISTRY, NodeState, Read, Report

#: Where the recordings go, one directory per release.
READS = Path(__file__).parent / "fixtures" / "reads"

#: A BGP RIB reads nothing until asked: each family is recorded listed whole,
#: every path received, and as advertised.
BGP_RIB_VARIANTS: tuple[dict[str, str], ...] = (
    {"list_all": "all", "paths": "all"},
    {"list_all": "all", "direction": "advertised"},
)


def variants(report: Report) -> list[dict[str, str]]:
    """The parameter sets a report is recorded with."""
    if report.name.startswith("bgp-rib-"):
        return [dict(v) for v in BGP_RIB_VARIANTS]
    return [{}]


def notification_json(n: Notification) -> dict[str, Any]:
    return {
        "updates": [[gpath.format(p), v] for p, v in n.updates],
        "deletes": [gpath.format(p) for p in n.deletes],
    }


class Recording:
    """A client whose Gets are written down: what was asked, and what came back."""

    def __init__(self, client: GnmiClient) -> None:
        self.client = client
        self.target = client.target
        self.calls: list[dict[str, Any]] = []
        self.sample = 0

    async def get(self, paths: Sequence[str], datatype: DataType = DataType.ALL) -> list[Notification]:
        call: dict[str, Any] = {"paths": [str(p) for p in paths], "datatype": str(datatype)}
        if self.sample:
            call["sample"] = self.sample
        self.calls.append(call)
        try:
            notifications = await self.client.get(paths, datatype)
        except RpcFailed as exc:
            call["error"] = str(exc)
            call["path_missing"] = exc.path_missing
            raise
        call["notifications"] = [notification_json(n) for n in notifications]
        return notifications


async def capture_report(
    client: GnmiClient, node: Node, models: dict[str, str], report: Report, params: dict[str, str]
) -> dict[str, Any] | None:
    reads = report.planned_reads(params)
    if not reads:
        return None
    recording = Recording(client)
    previous = elapsed = None
    if report.interval:
        # The earlier sample, as `collect.fetch` takes it: reads only, no follow.
        recording.sample = 1
        previous, started = await _sample(recording, reads)  # type: ignore[arg-type]
        await asyncio.sleep(report.interval)
        recording.sample = 0
    tree, done = await _sample(
        recording,  # type: ignore[arg-type]
        reads,
        [report.follow] if report.follow else [],
        report.param_values(params),
    )
    if previous is not None:
        elapsed = done - started
    state = NodeState(
        name=node.name, tree=tree, models=models, labels=node.labels, previous=previous, elapsed=elapsed
    )
    records = report.run(state, **params)
    return {
        "report": report.name,
        "params": params,
        "reads": [{"path": r.path, "datatype": str(r.datatype), "mode": str(r.mode)} for r in reads],
        "elapsed": elapsed,
        "calls": recording.calls,
        "records": [r.model_dump(mode="json") for r in records],
    }


async def capture_stream(client: GnmiClient, reads: list[Read]) -> dict[str, Any]:
    """Subscribe to *reads* up to the initial sync, as the engine does; drop what a node refuses."""
    refused: list[dict[str, Any]] = []
    asked = [r.path for r in reads]
    while True:
        started = time.monotonic()
        updates = 0
        subscriptions = [Subscription(r.path, r.mode, r.sample_interval) for r in reads]
        try:
            async with asyncio.timeout(SYNC_TIMEOUT):
                async with contextlib.aclosing(client.subscribe(subscriptions)) as stream:
                    async for n in stream:
                        if n.sync:
                            break
                        updates += len(n.updates)
        except RpcFailed as exc:
            unknown = UNKNOWN_ELEMENT.search(str(exc))
            dropped = [r for r in reads if unknown and any(e.name == unknown[1] for e in gpath.parse(r.path))]
            if not dropped:
                return {"paths": asked, "refused": refused, "error": str(exc)}
            refused.append({"element": unknown[1] if unknown else "", "paths": [r.path for r in dropped]})
            reads = [r for r in reads if r not in dropped]
            continue
        except TimeoutError:
            return {"paths": asked, "refused": refused, "error": f"no sync in {SYNC_TIMEOUT:.0f}s"}
        return {
            "paths": asked,
            "refused": refused,
            "synced": True,
            "updates": updates,
            "seconds": round(time.monotonic() - started, 2),
        }


async def capture_node(node: Node, release: str | None, names: Sequence[str]) -> tuple[str, dict[str, Any]]:
    async with GnmiClient(node.target()) as client:
        models = dict(client.capabilities.models) if client.capabilities else {}
        recorded = []
        for name in names:
            report = REGISTRY[name]
            for params in variants(report):
                entry = await capture_report(client, node, models, report, params)
                if entry is not None:
                    recorded.append(entry)
                    print(f"{node.name}: {name} {params or ''} {len(entry['records'])} records", flush=True)
        streamed = [r for r in REGISTRY.values() if not r.hidden]
        plan = Plan(streamed)
        streams = []
        for chunk in plan.chunks(MAX_SUBSCRIBED_PATHS):
            streams.append(await capture_stream(client, chunk))
        print(f"{node.name}: {len(streams)} subscriptions", flush=True)
    if release is None:
        sys_info = next((e for e in recorded if e["report"] == "sys-info"), None)
        version = sys_info["records"][0]["software_version"] if sys_info and sys_info["records"] else ""
        release = version.lstrip("v").split("-")[0]
    if not release:
        raise SystemExit(f"{node.name}: no software version from sys-info; pass --release")
    return release, {
        "node": node.name,
        "release": release,
        "labels": dict(node.labels),
        "captured": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "models": models,
        "reports": recorded,
        "streams": streams,
    }


async def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m tests.capture", description=__doc__.split("\n")[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("-t", "--topo", help="containerlab topology file")
    where.add_argument("-c", "--inventory", help="YAML inventory file")
    parser.add_argument("--node", action="append", required=True, help="node to record; repeat")
    parser.add_argument("--release", help="the SR Linux release (default: from sys-info)")
    parser.add_argument("--prefix", default="", help="file name prefix, e.g. the lab's name")
    parser.add_argument("-r", "--report", action="append", default=[], help="only these reports")
    parser.add_argument("--out", type=Path, default=READS)
    args = parser.parse_args(argv)

    inventory = from_clab(args.topo) if args.topo else from_file(args.inventory)
    nodes = {n.name: n for n in inventory.nodes}
    unknown = [n for n in args.node if n not in nodes]
    if unknown:
        raise SystemExit(f"not in the inventory: {', '.join(unknown)}")
    names = args.report or list(REGISTRY)
    # One node at a time: each holds sessions of its own, and these are slow Gets.
    for name in args.node:
        release, data = await capture_node(nodes[name], args.release, names)
        out = args.out / release / f"{args.prefix}{name}.json.gz"
        out.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(out, "wt") as f:
            json.dump(data, f, sort_keys=True, default=str)
        print(f"wrote {out} ({out.stat().st_size // 1024} KiB)")


if __name__ == "__main__":
    asyncio.run(main())

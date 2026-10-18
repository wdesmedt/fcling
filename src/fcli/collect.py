"""One-shot collection: connect to every node at once, Get what the reports read.

Each node is one task: connect, one Get per datatype carrying every declared
path, merge into a fresh tree, run the reports. Nodes fail on their own - a
node that is down is an error next to the others' records, not the end of
the run.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .gnmi import path as gpath
from .gnmi.client import DataType, GnmiClient, GnmiError, Notification, RpcFailed
from .inventory import Node
from .report import Follow, NodeState, Read, Record, Report
from .tree import Tree

logger = logging.getLogger(__name__)

#: How many nodes are connected to at once.
CONCURRENCY = 64
#: Routes per partial table event while a large RIB is rendered on one node.
STREAM_BATCH = 300


def _report_values(report: Report, params: Mapping[str, Any]) -> dict[str, Any]:
    return report.param_values(params)


@dataclass
class NodeResult:
    node: Node
    #: Records by report name.
    records: dict[str, list[Record]] = field(default_factory=dict)
    error: str | None = None


#: How a node names the element of a path its schema lacks.
UNKNOWN_ELEMENT = re.compile(r"unknown element '([^']+)'")


async def get_notifications(
    client: GnmiClient, paths: Sequence[str], datatype: DataType
) -> list[Notification]:
    """Get *paths*; a path the node does not know reads as empty.

    A node rejects the whole Get when one path is outside its schema - a
    feature this release or platform lacks. It names the element it does not
    know, so the paths with it are left out and the rest asked for again in
    one Get; where it does not say, path by path, one at a time - a node
    counts every RPC against a limit of 20 sessions.
    """
    paths = list(paths)
    while paths:
        try:
            return await client.get(paths, datatype)
        except RpcFailed as exc:
            if not exc.path_missing:
                raise
            unknown = UNKNOWN_ELEMENT.search(str(exc))
            kept = [p for p in paths if not unknown or all(e.name != unknown[1] for e in gpath.parse(p))]
            logger.debug("%s: %s", client.target.address, exc)
            if unknown and len(kept) < len(paths):
                paths = kept
                continue
            if len(paths) == 1:
                return []
            out: list[Notification] = []
            for path in paths:
                out += await get_notifications(client, [path], datatype)
            return out
    return []


async def _get_into(client: GnmiClient, tree: Tree, paths: Sequence[str], datatype: DataType) -> None:
    for notification in await get_notifications(client, paths, datatype):
        tree.apply(notification)


def _uncovered(paths: list[str]) -> list[str]:
    """*paths* less those another of them already reads.

    SR Linux answers a Get naming a whole list and keyed entries of it with
    the list, brackets stripped, inside the first keyed entry's update - not
    JSON - so a list is never asked for both ways at once.
    """
    parsed = {p: gpath.parse(p) for p in paths}
    by_schema: dict[str, list[str]] = defaultdict(list)
    for p, path in parsed.items():
        by_schema[gpath.schema(path)].append(p)
    # At its own depth only a wildcard read covers another; keyed entries of
    # one list (thousands of attr-sets) are never compared with each other.
    wild: dict[str, list[str]] = defaultdict(list)
    for p, path in parsed.items():
        if any(v == gpath.WILDCARD for e in path for _, v in e.keys):
            wild[gpath.schema(path)].append(p)
    covered: set[str] = set()
    for p, path in parsed.items():
        for depth in range(1, len(path) + 1):
            index = wild if depth == len(path) else by_schema
            for other in index.get(gpath.schema(path[:depth]), ()):
                if other != p and other not in covered and gpath.matches(parsed[other], path):
                    covered.add(p)
                    break
            if p in covered:
                break
    return [p for p in paths if p not in covered]


def by_type(reads: Iterable[Read]) -> dict[DataType, list[str]]:
    by_type: dict[DataType, list[str]] = defaultdict(list)
    for read in reads:
        if read.path not in by_type[read.datatype]:
            by_type[read.datatype].append(read.path)
    return {dt: _uncovered(paths) for dt, paths in by_type.items()}


#: How many rounds of dependent reads are followed: a chain of indirect
#: next-hops is a handful deep; the bound stops one that points at itself.
MAX_FOLLOW = 8


async def _sample(
    client: GnmiClient,
    reads: Iterable[Read],
    follows: Sequence[Follow] = (),
    params: Mapping[str, Any] | None = None,
) -> tuple[Tree, float]:
    """A tree of *reads* and of what *follows* ask for after them, and when it was complete."""
    tree = Tree()
    done = set(reads)
    await asyncio.gather(*(_get_into(client, tree, p, dt) for dt, p in by_type(done).items()))
    for _ in range(MAX_FOLLOW):
        wanted = {r for follow in follows for r in follow(tree, params or {})} - done
        if not wanted:
            break
        done |= wanted
        await asyncio.gather(*(_get_into(client, tree, p, dt) for dt, p in by_type(wanted).items()))
    return tree, time.monotonic()


async def fetch(
    node: Node,
    reports: Sequence[Report],
    rpc_timeout: float = 30.0,
    interval: float | None = None,
    params: Mapping[str, Any] | None = None,
) -> NodeState:
    """A node's state, as far as the reports read.

    A report of rates gets two samples of its paths, *interval* seconds apart
    (its own interval when not given), on the one connection.
    """
    values = params or {}
    reads = [r for report in reports for r in report.planned_reads(values)]
    rated = [r for report in reports if report.interval for r in report.planned_reads(values)]
    wait = interval if interval is not None else max((r.interval for r in reports), default=0.0)
    previous: Tree | None = None
    elapsed: float | None = None
    async with GnmiClient(node.target(), rpc_timeout=rpc_timeout) as client:
        if rated and wait > 0:
            previous, started = await _sample(client, rated)
            await asyncio.sleep(wait)
        tree, done = await _sample(client, reads, [r.follow for r in reports if r.follow], params)
        if previous is not None:
            elapsed = done - started
        models = dict(client.capabilities.models) if client.capabilities else {}
    return NodeState(
        name=node.name, tree=tree, models=models, labels=node.labels, previous=previous, elapsed=elapsed
    )


def _run_reports(
    state: NodeState,
    reports: Sequence[Report],
    params: Mapping[str, Any],
) -> tuple[dict[str, list[Record]], str | None]:
    """Run reports on a sampled tree (CPU-heavy; keep off the asyncio loop)."""
    records: dict[str, list[Record]] = {}
    for report in reports:
        try:
            values = _report_values(report, params)
            records[report.name] = list(report.records(state, values))
        except Exception as exc:  # a report bug on one node's data should not hide the others
            logger.exception("%s: report %s failed", state.name, report.name)
            return records, f"report {report.name} failed: {type(exc).__name__}: {exc}"
    return records, None


BatchHandler = Callable[[NodeResult, Sequence[Record]], Awaitable[None]]


async def _run_node_batched(
    node: Node,
    reports: Sequence[Report],
    limit: asyncio.Semaphore,
    rpc_timeout: float,
    interval: float | None,
    params: Mapping[str, Any],
    on_batch: BatchHandler,
) -> NodeResult:
    """Run reports on one node, emitting record batches for the UI to stream."""
    result = NodeResult(node)
    async with limit:
        try:
            state = await fetch(node, reports, rpc_timeout, interval, params)
        except GnmiError as exc:
            result.error = str(exc)
            return result
    loop = asyncio.get_running_loop()
    q: asyncio.Queue[tuple[str, Any, Any]] = asyncio.Queue()
    records_acc: dict[str, list[Record]] = defaultdict(list)

    def producer() -> None:
        try:
            for report in reports:
                values = _report_values(report, params or {})
                batch: list[Record] = []
                for rec in report.records(state, values):
                    batch.append(rec)
                    if len(batch) >= STREAM_BATCH:
                        loop.call_soon_threadsafe(q.put_nowait, ("batch", report.name, batch))
                        batch = []
                if batch:
                    loop.call_soon_threadsafe(q.put_nowait, ("batch", report.name, batch))
            loop.call_soon_threadsafe(q.put_nowait, ("done", None, None))
        except Exception as exc:
            logger.exception("%s: batched report failed", state.name)
            loop.call_soon_threadsafe(q.put_nowait, ("error", None, exc))

    threading.Thread(target=producer, daemon=True).start()
    while True:
        kind, report_name, payload = await q.get()
        if kind == "error":
            result.error = f"report failed: {type(payload).__name__}: {payload}"
            break
        if kind == "done":
            break
        records_acc[report_name].extend(payload)
        result.records = {k: list(v) for k, v in records_acc.items()}
        await on_batch(result, payload)
    result.records = {k: list(v) for k, v in records_acc.items()}
    return result


async def _run_node(
    node: Node,
    reports: Sequence[Report],
    limit: asyncio.Semaphore,
    rpc_timeout: float,
    interval: float | None,
    params: Mapping[str, Any],
) -> NodeResult:
    result = NodeResult(node)
    async with limit:
        try:
            state = await fetch(node, reports, rpc_timeout, interval, params)
        except GnmiError as exc:
            result.error = str(exc)
            return result
    records, err = await asyncio.to_thread(_run_reports, state, reports, params or {})
    result.records = records
    if err:
        result.error = err
    return result


NodeProgress = Callable[[str, int, int], None]


async def collect(
    nodes: Iterable[Node],
    reports: Sequence[Report],
    rpc_timeout: float = 30.0,
    interval: float | None = None,
    params: Mapping[str, Any] | None = None,
    on_node: NodeProgress | None = None,
    on_batch: BatchHandler | None = None,
) -> list[NodeResult]:
    """Every node at once; *params* are given to every report."""
    listed = list(nodes)
    total = len(listed)
    limit = asyncio.Semaphore(CONCURRENCY)
    done = 0

    async def one(node: Node) -> NodeResult:
        nonlocal done
        if on_batch is not None:
            result = await _run_node_batched(
                node, reports, limit, rpc_timeout, interval, params or {}, on_batch
            )
        else:
            result = await _run_node(node, reports, limit, rpc_timeout, interval, params or {})
        done += 1
        if on_node is not None:
            on_node(node.name, done, total)
        return result

    return list(await asyncio.gather(*(one(n) for n in listed)))

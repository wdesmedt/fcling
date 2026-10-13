"""The long-running engine: every node streamed, every report live.

Each report declares what it reads, so the engine knows what to ask for and
which reports an update touches:

* ON_CHANGE reads are **streamed**, one Subscribe RPC per 36 paths - the most
  SR Linux takes in one, refusing the whole request beyond it - plus a
  heartbeat leaf, without which a node that dropped off the network and a node
  where nothing changes look the same.
* SAMPLE reads - counters - are **polled** with a Get, and each polled subtree
  is pruned and written again whole. A SAMPLE subscription never says what is
  gone; a replaced subtree cannot hold on to it.
* Dependent reads (a report's ``follow``) are fetched by key as soon as they
  are wanted, and all of them refreshed every ``follow_interval``.

A (re)connect fills a fresh tree and only swaps it in once every RPC has
synced and the first poll is in: deletes made while the node was unreachable
cannot linger, and the reports never see a half-filled tree.

An update marks the reports whose reads it overlaps; the engine re-runs only
those, diffs their records by key against the last run and publishes the
changes - kept ones to the store, all of them to subscribers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from enum import StrEnum
from functools import lru_cache
from typing import Any

from pydantic import ValidationError

from .checks import FLAP_WINDOW, Finding, required, run_checks
from .collect import MAX_FOLLOW, UNKNOWN_ELEMENT, by_type, get_notifications
from .delta import Change, Op, diff, keyed
from .fabric import Fabric
from .gnmi import path as gpath
from .gnmi.client import GnmiClient, GnmiError, RpcFailed, Subscription, SubscriptionMode
from .gnmi.path import Path
from .inventory import Node
from .report import NodeState, Read, Record, Report
from .store import AsyncStore
from .tree import Tree

logger = logging.getLogger(__name__)

#: How many paths SR Linux accepts in one Subscribe request: beyond it the
#: whole request is refused. A node that says it takes fewer is believed.
MAX_SUBSCRIBED_PATHS = 36
_MAX_PATHS_ERROR = re.compile(r"maximum of (\d+) subscribed paths")

#: How a node rejects a subscription to a path its schema lacks - a feature
#: this platform or this node's role does not have: the whole request fails,
#: naming the element.
_UNKNOWN_ELEMENT = UNKNOWN_ELEMENT

#: Sampled in every subscription: due every few seconds, so its silence means
#: the stream is gone rather than that nothing changed.
HEARTBEAT = Read("/system/information/current-datetime", mode=SubscriptionMode.SAMPLE, sample_interval=10)

#: Seconds without the heartbeat before a stream counts as stalled. A leaf
#: under load may pause SAMPLE updates while a large Get runs on another session.
STALE_AFTER = 55.0

#: Seconds a subscription has to deliver its initial sync.
SYNC_TIMEOUT = 300.0

#: What the engine keeps the fabric checks' findings as, like a report.
CHECKS_REPORT = "checks"

#: Seconds between checks run with nothing changed: for flaps to age out.
CHECKS_EVERY = 30.0

#: Seconds the checks wait at most for every node to be read once.
STARTUP_GRACE = 60.0

#: Reconnect delays, doubled from the first up to the last.
BACKOFF = (2.0, 60.0)


class Status(StrEnum):
    CONNECTING = "connecting"
    SYNCING = "syncing"
    LIVE = "live"
    DOWN = "down"


def _defaults(report: Report) -> dict[str, Any]:
    return {p.name: p.default for p in report.params}


class Plan:
    """What the engine asks every node for, and which reports an update touches."""

    def __init__(self, reports: Iterable[Report]) -> None:
        self.reports = {r.name: r for r in reports}
        on_change = {
            r
            for rep in self.reports.values()
            for r in rep.planned_reads()
            if r.mode is not SubscriptionMode.SAMPLE
        }
        polled = {
            r for rep in self.reports.values() for r in rep.planned_reads() if r.mode is SubscriptionMode.SAMPLE
        }
        #: A read another one delivers in full is left out of the request.
        self.streamed = sorted(
            (r for r in on_change if not any(o != r and _covers(o, r) for o in on_change)),
            key=lambda r: r.path,
        )
        self.polled = sorted(polled, key=lambda r: r.path)
        self._patterns = [
            (name, [gpath.parse(r.path) for r in rep.planned_reads()]) for name, rep in self.reports.items()
        ]
        #: Reports to re-run after a poll: they read polled paths or follow reads.
        self.after_poll = frozenset(
            name for name, rep in self.reports.items() if rep.follow or any(r in polled for r in rep.planned_reads())
        )
        self.following = [rep for rep in self.reports.values() if rep.follow]
        self.affected = lru_cache(maxsize=4096)(self._affected)

    def chunks(self, max_paths: int, excluded: frozenset[Read] = frozenset()) -> list[list[Read]]:
        """The streamed reads in Subscribe requests of at most *max_paths*, a heartbeat in each.

        A full BGP RIB path is large and slow to sync; it gets its own RPC so
        opening another report does not replace the subscription carrying it.
        """
        streamed = [r for r in self.streamed if r not in excluded]
        heavy = [r for r in streamed if "/bgp-rib/afi-safi" in r.path]
        light = [r for r in streamed if r not in heavy]
        size = max(1, max_paths - 1)
        chunks: list[list[Read]] = [
            [*light[i : i + size], HEARTBEAT] for i in range(0, len(light), size)
        ]
        for read in heavy:
            chunks.append([read, HEARTBEAT])
        return chunks or [[HEARTBEAT]]

    def _affected(self, prefix: Path) -> frozenset[str]:
        return frozenset(
            name for name, patterns in self._patterns if any(gpath.overlaps(prefix, p) for p in patterns)
        )

    def touched(self, path: Path) -> frozenset[str]:
        # Cached on the first elements: deeper than the reads go, an update
        # changes nothing about which of them it overlaps.
        return self.affected(path[:4])


def _covers(outer: Read, inner: Read) -> bool:
    """Whether streaming *outer* delivers all of *inner*."""
    a, b = gpath.parse(outer.path), gpath.parse(inner.path)
    return len(a) <= len(b) and gpath.matches(a, b)


def _attr_set_cached(tree: Tree, read: Read) -> bool:
    """Whether *tree* already holds a BGP attribute set *read* asks for.

    An index names one set for the life of the path: changed attributes move
    the route to a new index, so a found set does not need periodic re-Get.
    """
    return "attr-set[index=" in read.path and isinstance(tree.get(read.path), dict)


def _first_error(exc: BaseException) -> BaseException:
    """The exception that ended a task group, out of the group around it."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


class NodeStream:
    """One node: its subscriptions, polls and tree, reconnected for as long as it runs."""

    def __init__(
        self,
        node: Node,
        plan: Plan,
        *,
        poll_interval: float = 10.0,
        follow_interval: float = 30.0,
        on_dirty: Callable[[], None] = lambda: None,
        on_status: Callable[[NodeStream], None] = lambda s: None,
    ) -> None:
        self.node = node
        self.plan = plan
        self.poll_interval = poll_interval
        self.follow_interval = follow_interval
        self._on_dirty = on_dirty
        self._on_status = on_status
        #: The tree reports read: the last complete one.
        self.tree = Tree()
        #: The tree updates go to: :attr:`tree`, or a fresh one while (re)syncing.
        self._target = self.tree
        self.status = Status.CONNECTING
        self.error = ""
        self.since = time.time()
        #: Why the node was lost, until it is live again: retrying, or resyncing, a lost
        #: node is still lost - else every attempt would clear and raise it again.
        self.lost = ""
        #: When the node last sent anything.
        self.heard = 0.0
        self.models: dict[str, str] = {}
        self.dirty: set[str] = set()
        #: The last two polls on their own, for reports that compare them.
        self.rounds: deque[tuple[Tree, float]] = deque(maxlen=2)
        self.max_paths = MAX_SUBSCRIBED_PATHS
        #: Streamed reads this node's schema does not have, found by being refused.
        self.excluded: set[Read] = set()
        #: The reports have run on a complete tree at least once.
        self.evaluated = False
        self._followed: set[Read] = set()
        #: A report that follows reads was touched: what it wants may be new.
        self._follow_pending = False
        self._stream_heard: list[float] = []

    # --------------------------------------------------------------- status

    @property
    def unreachable(self) -> bool:
        return self.status is not Status.LIVE and bool(self.lost)

    def _set(self, status: Status, error: str = "") -> None:
        if status is Status.DOWN:
            self.lost = error
        elif status is Status.LIVE:
            self.lost = ""
        if status != self.status or error != self.error:
            self.status, self.error, self.since = status, error, time.time()
            self._on_status(self)

    def info(self) -> dict[str, Any]:
        return {
            "name": self.node.name,
            "address": self.node.address,
            "labels": dict(self.node.labels),
            "status": str(self.status),
            "error": self.error,
            "since": self.since,
            "heard": self.heard,
        }

    def state(self) -> NodeState:
        previous, elapsed = None, None
        if len(self.rounds) == 2:
            (previous, before), (_, after) = self.rounds
            elapsed = after - before
        return NodeState(
            name=self.node.name,
            tree=self.tree,
            models=self.models,
            labels=self.node.labels,
            previous=previous,
            elapsed=elapsed,
        )

    def _touch(self, names: Iterable[str]) -> None:
        if self.status is not Status.LIVE:
            return
        names = set(names)
        if any(r.name in names for r in self.plan.following):
            self._follow_pending = True
        before = len(self.dirty)
        self.dirty.update(names)
        if len(self.dirty) != before:
            self._on_dirty()

    # ------------------------------------------------------------------ run

    async def run(self) -> None:
        delay = BACKOFF[0]
        retry = False
        while True:
            started = time.monotonic()
            if not retry:
                # Rediscovered on each connect: an upgrade can add what was missing.
                self.excluded.clear()
                if not self.lost:
                    # A lost node stays "down", with why, while it is tried again.
                    self._set(Status.CONNECTING, self.error)
            retry = False
            try:
                async with GnmiClient(self.node.target()) as client:
                    self.models = dict(client.capabilities.models) if client.capabilities else {}
                    await self._session(client)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - every failure is a reconnect
                error = _first_error(exc)
                if isinstance(error, asyncio.CancelledError):
                    raise error from None
                if isinstance(error, RpcFailed) and (m := _MAX_PATHS_ERROR.search(str(error))):
                    self.max_paths = min(self.max_paths, int(m[1]))
                    logger.warning("%s: takes %d paths per subscription", self.node.name, self.max_paths)
                    retry = True
                    continue
                if isinstance(error, RpcFailed) and (m := _UNKNOWN_ELEMENT.search(str(error))):
                    unknown = {
                        r for r in self.plan.streamed if any(e.name == m[1] for e in gpath.parse(r.path))
                    } - self.excluded
                    if unknown:
                        logger.info(
                            "%s: has no %s; not streaming %s",
                            self.node.name,
                            m[1],
                            ", ".join(sorted(r.path for r in unknown)),
                        )
                        self.excluded |= unknown
                        retry = True
                        continue
                message = str(error) if isinstance(error, GnmiError) else f"{type(error).__name__}: {error}"
                if not isinstance(error, GnmiError):
                    logger.exception("%s: stream failed", self.node.name, exc_info=error)
                self._set(Status.DOWN, message)
            if time.monotonic() - started > BACKOFF[1]:
                delay = BACKOFF[0]
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF[1])

    async def _session(self, client: GnmiClient) -> None:
        fresh = Tree()
        self._target = fresh
        self._followed = set()
        self.rounds.clear()
        self._set(Status.SYNCING)
        chunks = self.plan.chunks(self.max_paths, frozenset(self.excluded))
        synced = [asyncio.Event() for _ in chunks]
        self._stream_heard = [time.monotonic()] * len(chunks)
        async with asyncio.TaskGroup() as tasks:
            for i, chunk in enumerate(chunks):
                tasks.create_task(self._stream(client, chunk, i, synced[i]))
            async with asyncio.timeout(SYNC_TIMEOUT):
                await asyncio.gather(*(e.wait() for e in synced))
            await self._poll(client, fresh)
            await self._follow(client, fresh, refresh=True)
            self.tree = fresh
            self._set(Status.LIVE)
            self._touch(self.plan.reports)
            tasks.create_task(self._loop(self.poll_interval, lambda: self._poll(client, self.tree)))
            tasks.create_task(self._loop(self.follow_interval, lambda: self._follow(client, self.tree, True)))
            tasks.create_task(self._loop(1.0, lambda: self._follow_new(client)))
            tasks.create_task(self._watchdog())

    async def _stream(
        self, client: GnmiClient, reads: Sequence[Read], index: int, synced: asyncio.Event
    ) -> None:
        subscriptions = [Subscription(r.path, r.mode, r.sample_interval) for r in reads]
        async for notification in client.subscribe(subscriptions):
            self.heard = time.time()
            self._stream_heard[index] = time.monotonic()
            if notification.sync:
                synced.set()
                continue
            self._target.apply(notification)
            touched: set[str] = set()
            for path in notification.deletes:
                touched |= self.plan.touched(path)
            for path, _ in notification.updates:
                touched |= self.plan.touched(path)
            if touched:
                self._touch(touched)
        raise GnmiError(f"{self.node.name}: the node ended the subscription")

    @staticmethod
    async def _loop(interval: float, step: Callable[[], Awaitable[None]]) -> None:
        while True:
            await asyncio.sleep(interval)
            await step()

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(5)
            silent = time.monotonic() - min(self._stream_heard)
            if silent > STALE_AFTER:
                raise GnmiError(f"nothing heard for {silent:.0f}s: the stream stalled")

    async def _replace(self, client: GnmiClient, tree: Tree, reads: Iterable[Read]) -> Tree:
        """Get *reads* and write them over what *tree* had there; what was got, on its own."""
        reads = list(reads)
        answers = await asyncio.gather(
            *(get_notifications(client, p, dt) for dt, p in by_type(reads).items())
        )
        got = Tree()
        for read in reads:
            tree.prune(read.path)
        for notifications in answers:
            for n in notifications:
                tree.apply(n)
                got.apply(n)
        self.heard = time.time()
        return got

    async def _poll(self, client: GnmiClient, tree: Tree) -> None:
        if not self.plan.polled:
            return
        got = await self._replace(client, tree, self.plan.polled)
        self.rounds.append((got, time.monotonic()))
        self._touch(self.plan.after_poll)

    def _wanted(self, tree: Tree) -> set[Read]:
        return {r for rep in self.plan.following for r in rep.follow(tree, _defaults(rep))}  # type: ignore[misc]

    async def _follow(self, client: GnmiClient, tree: Tree, refresh: bool) -> None:
        """Get what the reports' follows ask for: all of it on *refresh*, else what is new."""
        if not self.plan.following:
            return
        for _ in range(MAX_FOLLOW):
            wanted = self._wanted(tree)
            todo = wanted if refresh else wanted - self._followed
            if refresh:
                todo = {r for r in todo if not _attr_set_cached(tree, r)}
            refresh = False
            if not todo:
                break
            await self._replace(client, tree, todo)
            self._followed = wanted | self._followed
        pending = self._follow_pending
        self._touch(r.name for r in self.plan.following)
        # What was just fetched is what the reports asked for: not new again.
        self._follow_pending = pending

    async def _follow_new(self, client: GnmiClient) -> None:
        if self._follow_pending:
            self._follow_pending = False
            await self._follow(client, self.tree, refresh=False)


Listener = Callable[[dict[str, Any]], None]


class Engine:
    """Every node streamed, every report kept current, every change published."""

    def __init__(
        self,
        nodes: Iterable[Node],
        reports: Iterable[Report],
        store: AsyncStore | None = None,
        *,
        poll_interval: float = 10.0,
        follow_interval: float = 30.0,
        settle: float = 0.5,
        checks: bool = True,
    ) -> None:
        self.nodes = list(nodes)
        self.plan = Plan(reports)
        self.store = store
        self.settle = settle
        self._wake = asyncio.Event()
        #: Whether the fabric checks run, and so findings are kept as ``checks``.
        self.checks = checks
        self._check_reports = set(required())
        self._checks_due = checks
        #: Kept changes of the last :data:`FLAP_WINDOW`: what flapping reads.
        self.recent: deque[dict[str, Any]] = deque()
        #: ``{(node, key): when}`` each current finding was raised.
        self.raised: dict[tuple[str, str], float] = {}
        self.streams = {
            n.name: NodeStream(
                n,
                self.plan,
                poll_interval=poll_interval,
                follow_interval=follow_interval,
                on_dirty=self._wake.set,
                on_status=self._status_changed,
            )
            for n in self.nodes
        }
        #: ``{(node, report): {key: record}}``: the last run of every report.
        self.records: dict[tuple[str, str], dict[str, Record]] = {}
        self._listeners: set[Listener] = set()
        #: Per-node rendered table rows for :func:`fcli.render.table_rows`.
        self.table_cache: dict[tuple[str, str], tuple[str, list[list[str]]]] = {}
        self.started = time.time()

    # -------------------------------------------------------------- listen

    def listen(self, listener: Listener) -> Callable[[], None]:
        """Call *listener* with every event; returns what stops it."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def _publish(self, event: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # a listener's failure is its own
                logger.exception("event listener failed")
                self._listeners.discard(listener)

    def _status_changed(self, stream: NodeStream) -> None:
        # A node going away or coming back is a finding raised or cleared.
        self._checks_due = self.checks
        self._wake.set()
        if stream.status is Status.DOWN:
            logger.warning("%s: %s", stream.node.name, stream.error)
        else:
            logger.info("%s: %s", stream.node.name, stream.status)
        self._publish({"type": "node", **stream.info()})

    # ----------------------------------------------------------------- run

    async def run(self) -> None:
        if self.store is not None:
            await self._load()
        async with asyncio.TaskGroup() as tasks:
            for stream in self.streams.values():
                tasks.create_task(stream.run())
            tasks.create_task(self._evaluate_loop())
            if self.checks:
                tasks.create_task(self._checks_tick())

    async def _load(self) -> None:
        """Pick up where the history left off, so a restart logs only what changed meanwhile."""
        assert self.store is not None
        for (node, name), rows in (await self.store.current()).items():
            report = self.plan.reports.get(name)
            model = Finding if name == CHECKS_REPORT else report.record if report else None
            if model is None or node not in self.streams:
                continue
            records: dict[str, Record] = {}
            for key, row in rows.items():
                with contextlib.suppress(ValidationError):  # a record of an older shape is re-added
                    records[key] = model.model_validate(row)
            self.records[(node, name)] = records
        self.raised = await self.store.raised(CHECKS_REPORT)
        self.recent.extend(await self.store.history(since=time.time() - FLAP_WINDOW, limit=100_000))

    async def _evaluate_loop(self) -> None:
        while True:
            await self._wake.wait()
            # Let a burst of updates land before running anything.
            await asyncio.sleep(self.settle)
            self._wake.clear()
            changes, records = await asyncio.to_thread(self.evaluate)
            if self.checks and not self._ready():
                # Half a fabric read is every link one-sided: wait for the rest.
                self._checks_due = True
            elif self.checks and (
                self._checks_due or any(c.report in self._check_reports for c in changes if c.kept)
            ):
                self._checks_due = False
                found, findings = self.evaluate_checks(changes, records)
                changes += found
                records.update(findings)
            kept = [c for c in changes if c.kept]
            if kept and self.store is not None:
                seqs = iter(await self.store.write(kept))
                changes = [replace(c, seq=next(seqs)) if c.kept else c for c in changes]
            # Only now: what is read live is never ahead of what is in the
            # history and on its way to the listeners.
            self.records.update(records)
            self._remember(changes)
            for change in changes:
                self._publish({"type": "change", **change.as_dict()})

    def _ready(self) -> bool:
        """Whether every node has been read once, or found unreachable - or long enough has passed."""
        if time.time() - self.started > STARTUP_GRACE:
            return True
        return all(s.evaluated or s.unreachable for s in self.streams.values())

    async def _checks_tick(self) -> None:
        """Re-check now and then with nothing changed: a flap ages out of its window."""
        while True:
            await asyncio.sleep(CHECKS_EVERY)
            self._checks_due = True
            self._wake.set()

    def _remember(self, changes: Sequence[Change]) -> None:
        now = time.time()
        for c in changes:
            if not c.kept:
                continue
            self.recent.append(c.as_dict())
            if c.report == CHECKS_REPORT:
                if c.op is Op.ADD:
                    self.raised[(c.node, c.key)] = c.ts
                elif c.op is Op.REMOVE:
                    self.raised.pop((c.node, c.key), None)
        while self.recent and self.recent[0]["ts"] < now - FLAP_WINDOW:
            self.recent.popleft()

    def fabric(self, pending: Mapping[tuple[str, str], Mapping[str, Record]] | None = None) -> Fabric:
        """The fabric as the engine has it now - with *pending* runs over the committed ones."""
        merged = {**self.records, **(pending or {})}
        records: dict[str, list[Record]] = {}
        for (_node, name), rows in merged.items():
            if name != CHECKS_REPORT:
                records.setdefault(name, []).extend(rows.values())
        for name in self.plan.reports:
            records.setdefault(name, [])
        unreachable = {s.node.name: s.lost for s in self.streams.values() if s.unreachable}
        return Fabric.of(self.nodes, records, unreachable, list(self.recent))

    def evaluate_checks(
        self, changes: Sequence[Change], pending: Mapping[tuple[str, str], Mapping[str, Record]]
    ) -> tuple[list[Change], dict[tuple[str, str], dict[str, Record]]]:
        """The fabric checks over the engine's records and *pending* runs: findings changed, and kept."""
        fabric = self.fabric(pending)
        fabric.changes = [*fabric.changes, *(c.as_dict() for c in changes if c.kept)]
        try:
            findings = run_checks(fabric)
        except Exception:
            logger.exception("checks failed")
            return [], {}
        by_node: dict[str, list[Finding]] = {}
        for f in findings:
            by_node.setdefault(f.node, []).append(f)
        now = time.time()
        out: list[Change] = []
        runs: dict[tuple[str, str], dict[str, Record]] = {}
        for node in self.streams:
            new = keyed(by_node.get(node, []))
            key = (node, CHECKS_REPORT)
            out.extend(diff(node, CHECKS_REPORT, self.records.get(key, {}), new, now))
            runs[key] = dict(new)
        return out, runs

    def evaluate(self) -> tuple[list[Change], dict[tuple[str, str], dict[str, Record]]]:
        """Re-run the dirty reports of every live node: what changed, and the records
        to keep - for the caller to commit once the changes are stored."""
        now = time.time()
        changes: list[Change] = []
        runs: dict[tuple[str, str], dict[str, Record]] = {}
        for stream in self.streams.values():
            if stream.status is not Status.LIVE or not stream.dirty:
                continue
            names, stream.dirty = stream.dirty, set()
            stream.evaluated = True
            state = stream.state()
            for name in names:
                report = self.plan.reports[name]
                if report.interval and state.previous is None:
                    # Rates need two polls: nothing yet is not everything gone.
                    continue
                try:
                    records = keyed(report.run(state))
                except Exception:
                    logger.exception("%s: report %s failed", stream.node.name, name)
                    continue
                key = (stream.node.name, name)
                changes.extend(diff(stream.node.name, name, self.records.get(key, {}), records, now))
                runs[key] = records
        return changes, runs

    # ---------------------------------------------------------------- read

    def current(self, report: str, nodes: Sequence[str] | None = None) -> list[Record]:
        names = self.streams if nodes is None else nodes
        return [r for node in names for r in self.records.get((node, report), {}).values()]

    def status(self) -> list[dict[str, Any]]:
        return [s.info() for s in self.streams.values()]

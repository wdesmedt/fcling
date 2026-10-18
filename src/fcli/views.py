"""The fabric-wide answers - checks, incidents, drift, a history with its causes -
from whichever source there is: the nodes themselves, the engine, or the history.

Every surface asks the same questions; this is where they are answered once.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from .checks import FLAP_WINDOW, Finding, run_checks
from .collect import collect
from .delta import Change, diff, keyed
from .fabric import Fabric
from .incidents import FarEnds, Incident, correlate
from .inventory import Node
from .report import Record
from .reports import REGISTRY
from .store import BeforeHistory, Store

#: Seconds between the two samples a one-shot ``checks`` takes of the counters.
CHECK_INTERVAL = 2.0


async def fabric_now(nodes: Sequence[Node], reports: Iterable[str], rpc_timeout: float = 30.0) -> Fabric:
    """The fabric as it is, collected one-shot from every node."""
    wanted = [REGISTRY[r] for r in reports if r in REGISTRY]
    results = await collect(nodes, wanted, rpc_timeout=rpc_timeout, interval=CHECK_INTERVAL)
    records: dict[str, list[Record]] = {r.name: [] for r in wanted}
    for result in results:
        for name, rows in result.records.items():
            records[name].extend(rows)
    unreachable = {r.node.name: r.error for r in results if r.error and not r.records}
    return Fabric.of(nodes, records, unreachable)


def _validate(report: str, rows: Iterable[Mapping[str, Any]]) -> list[Record]:
    model = Finding if report == "checks" else REGISTRY[report].record
    out = []
    for row in rows:
        try:
            out.append(model.model_validate(row))
        except ValidationError:
            continue  # a record of an older shape
    return out


def records_at(store: Store, ts: float, report: str, nodes: Sequence[str] = ()) -> list[Record]:
    """*report*'s records as they were at *ts*."""
    state = store.state_at(ts, report, nodes)
    return [r for node in sorted(state) for r in _validate(report, state[node].values())]


def fabric_at(store: Store, nodes: Sequence[Node], ts: float, reports: Iterable[str]) -> Fabric:
    """The fabric as the history says it was at *ts*: its records then, and the
    history just before - for what was flapping - and who was unreachable."""
    names = [n.name for n in nodes]
    records = {r: records_at(store, ts, r, names) for r in reports if r in REGISTRY}
    findings = records_at(store, ts, "checks", names)
    unreachable = {
        f.node: f.detail.removeprefix("nothing could be read: ")
        for f in findings
        if isinstance(f, Finding) and f.check == "node_unreachable"
    }
    changes = store.history(since=ts - FLAP_WINDOW, until=ts, limit=100_000)
    return Fabric.of(nodes, records, unreachable, changes)


def incidents(fabric: Fabric, raised: Mapping[tuple[str, str], float] | None = None) -> list[Incident]:
    return correlate(run_checks(fabric), fabric, raised)


# --------------------------------------------------------------------------- #
# drift: two moments compared
# --------------------------------------------------------------------------- #


def drift(
    store: Store, report: str, since: float, until: float | None = None, nodes: Sequence[str] = ()
) -> list[Change]:
    """How *report* at *until* (now) differs from *report* at *since*, record by
    record - what came, went and changed in between, not every step on the way.
    Volatile fields are no difference: counters always moved."""
    until = time.time() if until is None else until
    before = store.state_at(since, report, nodes)
    after = store.state_at(until, report, nodes)
    out: list[Change] = []
    for node in sorted(set(before) | set(after)):
        old = keyed(_validate(report, before.get(node, {}).values()))
        new = keyed(_validate(report, after.get(node, {}).values()))
        out.extend(c for c in diff(node, report, old, new, until) if c.kept)
    return out


def drift_reports(store: Store) -> list[str]:
    """The reports a drift compares by default: the ones with a history, findings left out."""
    rows = store.db.execute("SELECT DISTINCT report FROM current UNION SELECT DISTINCT report FROM changes")
    return sorted(r[0] for r in rows if r[0] in REGISTRY)


# --------------------------------------------------------------------------- #
# the history, with what caused it
# --------------------------------------------------------------------------- #

#: Seconds after a configuration commit that a change on the same node is put down to it.
ATTRIBUTION_WINDOW = 120.0

#: Seconds a commit can have ended, by the node's clock, before the engine saw it and
#: still be a cause. Older, it was read from the commit log when a node was first
#: synced - with everything else, which it did not cause.
FRESH_COMMIT = 900.0

#: A commit, or a change - a finding raised or cleared too - put down to one.
CONFIG = "config"
#: Anything else: what the network did on its own.
UNPLANNED = "unplanned"


def _fresh(commit: Mapping[str, Any]) -> bool:
    """Whether the engine saw *commit* being made, rather than found it in the log.

    A commit whose time the node does not give is taken as fresh.
    """
    from .reports.common import parse_time as device_time

    record = commit.get("after") or {}
    made = device_time(record.get("ended")) or device_time(record.get("started"))
    return made is None or commit["ts"] - made.timestamp() <= FRESH_COMMIT


def attribute(
    changes: Sequence[dict[str, Any]], commits: Sequence[dict[str, Any]], ends: FarEnds | None = None
) -> list[dict[str, Any]]:
    """*changes*, each with the ``cause`` that most likely made it and its ``origin``:
    ``config`` for a commit and what one caused, ``unplanned`` for the rest.
    ``commit`` names the commit, ``{"node", "id"}``, for its configuration diff.

    A change is put down to the latest commit shortly before it on its own
    node or - with *ends* - on a node at the far end of its cable or session:
    a spine reconfigured drops the leaf's session too.

    Commits are matched by when the engine saw them, not by the node's clock,
    which need not agree with the server's.
    """
    by_node: dict[str, list[dict[str, Any]]] = {}
    for c in commits:
        if c["report"] == "config-commits" and c["op"] == "add" and c.get("after") and _fresh(c):
            by_node.setdefault(c["node"], []).append(c)
    for node_commits in by_node.values():
        node_commits.sort(key=lambda c: c["ts"])
    out = []
    for change in changes:
        cause = ""
        commit_ref = None
        if change["report"] == "config-commits":
            record = change.get("after") or change.get("before") or {}
            commit_ref = {"node": change["node"], "id": record.get("id")}
        elif by_node:
            nodes = {change["node"]}
            if ends is not None and any(n != change["node"] for n in by_node):
                nodes |= ends.of(change)
            latest = (_last_commit(by_node.get(n, ()), change["ts"]) for n in nodes)
            found = [c for c in latest if c is not None]
            if found:
                commit = max(found, key=lambda c: c["ts"])
                a = commit["after"]
                where = "" if commit["node"] == change["node"] else f" on {commit['node']}"
                cause = f"commit {a.get('id')}{where} by {a.get('username') or '?'}"
                if a.get("comment"):
                    cause += f" ({a['comment']})"
                commit_ref = {"node": commit["node"], "id": a.get("id")}
        origin = CONFIG if commit_ref else UNPLANNED
        out.append({**change, "cause": cause, "origin": origin, "commit": commit_ref})
    return out


def _last_commit(commits: Sequence[dict[str, Any]], ts: float) -> dict[str, Any] | None:
    """The latest of *commits* (oldest first) seen shortly before *ts*."""
    for commit in reversed(commits):
        if commit["ts"] <= ts + 1.0:
            return commit if ts - commit["ts"] <= ATTRIBUTION_WINDOW else None
    return None


#: What the far ends of a change are worked out from: cables, addresses, sessions.
FAR_END_REPORTS = ("lldp", "subif", "ni", "bfd", "es")


def far_ends(store: Store) -> FarEnds | None:
    """The far ends of changes, from the fabric as the history has it now."""
    names = [r[0] for r in store.db.execute("SELECT DISTINCT node FROM current")]
    if not names:
        return None
    try:
        fabric = fabric_at(store, [Node(n, n) for n in names], time.time(), FAR_END_REPORTS)
    except BeforeHistory:
        return None
    return FarEnds(fabric)


def history_with_causes(store: Store, **query: Any) -> list[dict[str, Any]]:
    """The changes the history lists, each with its likely cause; never those
    of a report that is not :attr:`~fcli.report.Report.eventful`."""
    exclude = [r.name for r in REGISTRY.values() if not r.eventful]
    changes = store.history(**query, exclude=exclude)
    if not changes:
        return changes
    # Commits on every node: one on a neighbour can be the cause.
    commits = store.history(
        since=changes[0]["ts"] - ATTRIBUTION_WINDOW,
        until=changes[-1]["ts"] + 1.0,
        reports=["config-commits"],
        limit=100_000,
    )
    fresh = any(c["op"] == "add" and _fresh(c) for c in commits)
    return attribute(changes, commits, far_ends(store) if fresh else None)


# --------------------------------------------------------------------------- #
# configurations
# --------------------------------------------------------------------------- #


def _config_meta(kept: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if kept is None else {k: kept[k] for k in ("ts", "commit_id", "lines", "digest")}


def config_diff(
    store: Store, node: str, commit: int | None = None, base: int | None = None, context: int = 3
) -> dict[str, Any]:
    """What a commit did to *node*'s configuration, as a side-by-side diff.

    The configuration after *commit* (the latest without one) is the first
    kept from that commit on, and it is compared with the one kept before
    it - or with the one kept after commit *base*. Several commits made
    between two reads are one diff; ``commits`` lists them. With nothing kept
    earlier, ``before`` is ``None`` and there are no rows.
    """
    from . import config

    after = store.config(node, commit_id=commit) if commit is not None else store.config(node)
    if after is None:
        raise LookupError(
            f"no configuration of {node} kept" + (f" from commit {commit} on" if commit is not None else "")
        )
    before = (
        store.config(node, commit_id=base) if base is not None else store.config(node, before=after["ts"])
    )
    if before is not None and before["ts"] >= after["ts"]:
        before, after = after, before
    low = (before or {}).get("commit_id") or 0
    high = after.get("commit_id") or 0
    commits: dict[int, dict[str, Any]] = {}
    for change in store.history(nodes=[node], reports=["config-commits"], limit=100_000):
        record = change.get("after") or {}
        if change["op"] != "remove" and isinstance(record.get("id"), int) and low < record["id"] <= high:
            commits[record["id"]] = record
    answer: dict[str, Any] = {
        "node": node,
        "before": _config_meta(before),
        "after": _config_meta(after),
        "commits": [commits[i] for i in sorted(commits)],
    }
    if before is None:
        return {**answer, "rows": [], "summary": "the first configuration kept"}
    return {**answer, **config.compare(before["tree"], after["tree"], context).as_dict()}


def configs(store: Store, nodes: Sequence[str] = ()) -> list[dict[str, Any]]:
    """The configurations kept, newest first."""
    return store.configs(nodes)

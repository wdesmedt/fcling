"""The questions fcli answers about a fabric, behind one interface.

Every surface asks the same things - what does the fabric look like, what is
wrong, what changed - and :class:`Service` is where they are asked. There are
three answers to who answers them:

* :class:`EngineService` - the engine in this process: live records, kept
  history. What ``fcling serve`` exposes over HTTP and MCP, and what the agent
  inside it uses.
* :class:`RemoteService` - a running ``fcling serve``, over HTTP.
* :class:`LocalService` - the nodes themselves, one-shot: no history, so
  ``at``, history and drift say so rather than guess.

The tools (:mod:`fcli.tools`) are written once against this, which is what
lets an MCP client and the in-app agent see exactly the same fabric.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any, Protocol

from . import render, views
from .checks import Finding, required, run_checks
from .collect import collect
from .delta import record_key
from .incidents import correlate
from .inventory import Inventory, InventoryError
from .remote import Remote, RemoteError
from .reports import REGISTRY, bgp_rib
from .store import BeforeHistory, Store, parse_time


class ServiceError(Exception):
    """A question that cannot be answered as asked: a user error, said in words."""


def _report(name: str) -> Any:
    report = REGISTRY.get(name)
    if report is None:
        raise ServiceError(f"no report {name!r}: list_reports says which there are")
    return report


def _params(report: Any, params: dict[str, Any]) -> dict[str, Any]:
    known = {p.name: p for p in report.params}
    unknown = set(params) - set(known)
    if unknown:
        raise ServiceError(f"{report.name} takes no parameter {', '.join(sorted(unknown))}")
    for name, value in params.items():
        if known[name].choices and value and value not in known[name].choices:
            raise ServiceError(f"{name}: {value!r} is not one of {', '.join(known[name].choices)}")
    return params


def _instant(text: str | None) -> float | None:
    if text in (None, ""):
        return None
    try:
        return parse_time(str(text))
    except ValueError as exc:
        raise ServiceError(str(exc)) from exc


def catalog(streamed: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Every report, as a client needs to know it to ask for one."""
    out: list[dict[str, Any]] = []
    for r in REGISTRY.values():
        entry: dict[str, Any] = {
            "name": r.name,
            "help": r.help,
            "streamed": r.name in streamed,
            "hidden": r.hidden,
            "columns": [c.title for c in r.columns],
            "detail_columns": [c.title for c in r.detail_columns],
            "params": [
                {"name": p.name, "help": p.help, "default": p.default, "choices": list(p.choices)}
                for p in r.params
            ],
            "key": list(r.record.key),
            "volatile": list(r.record.volatile),
            "schema": r.record.model_json_schema(),
        }
        parsed = bgp_rib.parse_rib_report(r.name)
        if parsed is not None:
            family, route_type = parsed
            entry["rib_keys"] = [p.name for p in bgp_rib.rib_key_params(family, route_type)]
            entry["rib_evpn"] = family == "evpn"
            entry["needs_query"] = True
        out.append(entry)
    return out


def _bgp_rib_pending(spec: Any, params: dict[str, Any]) -> bool:
    parsed = bgp_rib.parse_rib_report(spec.name)
    if parsed is None:
        return False
    family, route_type = parsed
    if family == "evpn" and not route_type:
        return not bgp_rib.evpn_rib_requested(params)
    return not bgp_rib.rib_requested(family, route_type, params)


def _empty_report(spec: Any, message: str) -> dict[str, Any]:
    return {"report": spec.name, "source": "pending", "records": [], "errors": {}, "message": message}


class Service(Protocol):
    """What every surface can ask. Records go out as JSON, the shape of the report's records."""

    async def status(self) -> dict[str, Any]: ...

    async def reports(self) -> list[dict[str, Any]]: ...

    async def records(
        self,
        report: str,
        params: dict[str, Any] | None = None,
        filters: Sequence[str] = (),
        nodes: Sequence[str] = (),
        at: str | None = None,
    ) -> dict[str, Any]: ...

    async def checks(self, at: str | None = None, names: Sequence[str] = ()) -> list[dict[str, Any]]: ...

    async def incidents(self, at: str | None = None) -> list[dict[str, Any]]: ...

    async def history(
        self,
        since: str | None = None,
        until: str | None = None,
        nodes: Sequence[str] = (),
        reports: Sequence[str] = (),
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...

    async def drift(
        self, since: str, until: str | None = None, reports: Sequence[str] = (), nodes: Sequence[str] = ()
    ) -> dict[str, Any]: ...

    async def baselines(self) -> list[dict[str, Any]]: ...

    async def lens(
        self, name: str, args: dict[str, Any] | None = None, at: str | None = None
    ) -> dict[str, Any]: ...

    async def topology(self, at: str | None = None) -> dict[str, Any]: ...


def _lens(name: str) -> Any:
    from .lenses import LENSES

    found = LENSES.get(name)
    if found is None:
        raise ServiceError(f"no lens {name!r}: one of {', '.join(LENSES)}")
    return found


def _run_lens(spec: Any, fabric: Any, args: dict[str, Any] | None) -> list[dict[str, Any]]:
    from .lens import LensError

    try:
        return [r.model_dump(mode="json") for r in spec.run(fabric, **(args or {}))]
    except LensError as exc:
        raise ServiceError(str(exc)) from exc


def _topology(fabric: Any, findings: Any, name: str) -> dict[str, Any]:
    from .topology import build

    return build(fabric, findings, name)


def select_nodes(fabric: Inventory, filters: Sequence[str], nodes: Sequence[str]) -> list[str]:
    try:
        selected = fabric.select(list(filters)) if filters else fabric
    except InventoryError as exc:
        raise ServiceError(str(exc)) from exc
    if filters and not selected.nodes:
        # Said, not answered with nothing: an empty answer reads as "there is none".
        labels = sorted({f"{k}={v}" for n in fabric.nodes for k, v in n.labels.items()})
        raise ServiceError(
            f"no node matches {' '.join(filters)}; "
            + (
                f"the labels there are: {', '.join(labels[:30])}"
                if labels
                else "the nodes have no labels: use nodes"
            )
        )
    names = [n.name for n in selected.nodes]
    if nodes:
        unknown = sorted(set(nodes) - {n.name for n in fabric.nodes})
        if unknown:
            raise ServiceError(f"no node {', '.join(unknown)} in {fabric.name}")
        names = [n for n in names if n in nodes]
    return names


# --------------------------------------------------------------------------- #
# in-process: the engine
# --------------------------------------------------------------------------- #


def _pack_collect(results: Sequence[Any], report_name: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    rows: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for res in results:
        if res.error:
            errors[res.node.name] = res.error
        rows.extend(r.model_dump(mode="json") for r in res.records.get(report_name, []))
    return rows, errors


def _collect_rpc_timeout(spec: Any, base: float) -> float:
    """BGP RIB walks can exceed the default gNMI deadline on a busy leaf."""
    if bgp_rib.parse_rib_report(spec.name) is not None:
        return max(base, 180.0)
    return base


class EngineService:
    def __init__(self, engine: Any, fabric: Inventory, rpc_timeout: float = 30.0) -> None:
        self.engine = engine
        self.fabric = fabric
        self.rpc_timeout = rpc_timeout
        self._collect_slots = asyncio.Semaphore(2)

    def _store(self) -> Any:
        if self.engine.store is None:
            raise ServiceError("no history is kept by this engine")
        return self.engine.store

    async def _then(self, fn: Any, *args: Any) -> Any:
        """``fn(store, *args)`` over the history, an instant before it said so."""
        try:
            return await self._store().run(fn, *args)
        except BeforeHistory as exc:
            raise ServiceError(str(exc)) from exc

    async def status(self) -> dict[str, Any]:
        found = await self.incidents()
        return {
            "fabric": self.fabric.name,
            "source": "engine",
            "started": self.engine.started,
            "nodes": self.engine.status(),
            "streamed": sorted(self.engine.plan.reports),
            "incidents": {
                "errors": sum(1 for i in found if i["severity"] == "error"),
                "warnings": sum(1 for i in found if i["severity"] == "warning"),
                "info": sum(1 for i in found if i["severity"] == "info"),
            },
        }

    async def reports(self) -> list[dict[str, Any]]:
        return catalog(sorted(self.engine.plan.reports))

    async def records(
        self,
        report: str,
        params: dict[str, Any] | None = None,
        filters: Sequence[str] = (),
        nodes: Sequence[str] = (),
        at: str | None = None,
    ) -> dict[str, Any]:
        spec = _report(report)
        params = _params(spec, dict(params or {}))
        if _bgp_rib_pending(spec, params):
            return _empty_report(
                spec,
                "Narrow the RIB with route keys or network-instance, or choose List all routes.",
            )
        names = select_nodes(self.fabric, filters, nodes)
        when = _instant(at)
        if when is not None:
            state = await self._then(Store.state_at, when, spec.name, names)
            rows = [r for node in names for r in state.get(node, {}).values()]
            return {"report": spec.name, "source": "history", "at": when, "records": rows, "errors": {}}
        if (
            spec.name in self.engine.plan.reports
            and spec.planned_reads(params)
            and all(params.get(p.name, p.default) == p.default for p in spec.params)
        ):
            rows = [r.model_dump(mode="json") for r in self.engine.current(spec.name, names)]
            errors = {
                s["name"]: s["error"]
                for s in self.engine.status()
                if s["name"] in names and s["status"] != "live"
            }
            return {"report": spec.name, "source": "live", "records": rows, "errors": errors}
        return await self._collect_report(spec, params, names)

    async def _collect_report(
        self,
        spec: Any,
        params: dict[str, Any],
        names: list[str],
        on_node: Any = None,
    ) -> dict[str, Any]:
        async with self._collect_slots:
            selected = [n for n in self.fabric.nodes if n.name in names]
            results = await collect(
                selected,
                [spec],
                rpc_timeout=_collect_rpc_timeout(spec, self.rpc_timeout),
                params=params,
                on_node=on_node,
            )
            rows, errors = await asyncio.to_thread(_pack_collect, results, spec.name)
        return {"report": spec.name, "source": "collected", "records": rows, "errors": errors}

    async def _collect_report_streaming(
        self,
        spec: Any,
        params: dict[str, Any],
        names: list[str],
        emit: Any,
        on_node: Any = None,
    ) -> dict[str, Any]:
        """Collect with batched partial table events for the UI."""
        async with self._collect_slots:
            selected = [n for n in self.fabric.nodes if n.name in names]
            titles = render.table_titles(spec)
            rows: list[list[Any]] = []
            errors: dict[str, str] = {}
            truncated = False
            lock = asyncio.Lock()
            #: Rows already sent: each partial carries only the rows after them
            #: and says ``append``. Resending the table so far, 61k EVPN routes
            #: from one peer came to a gigabyte of events.
            sent = 0
            first = True

            async def push_partial() -> None:
                nonlocal sent, first
                fresh = rows[sent:]
                await emit(
                    {
                        "type": "partial",
                        "answer": {
                            "report": spec.name,
                            "source": "collected",
                            "loading": True,
                            "columns": titles,
                            "rows": fresh,
                            "append": not first,
                            "errors": dict(errors),
                            "truncated": truncated,
                            "message": None,
                        },
                    }
                )
                sent, first = len(rows), False

            await push_partial()

            async def on_batch(_result: Any, batch: Sequence[Any]) -> None:
                nonlocal truncated
                if not batch:
                    return
                new_rows = await asyncio.to_thread(render.rows_for_records, spec, batch)
                async with lock:
                    rows.extend(new_rows)
                    if len(rows) > render.TABLE_MAX_ROWS:
                        truncated = True
                        rows[:] = rows[: render.TABLE_MAX_ROWS]
                    if len(rows) > sent:
                        await push_partial()

            def node_progress(node: str, done: int, total: int) -> None:
                if on_node is not None:
                    on_node(node, done, total)

            results = await collect(
                selected,
                [spec],
                rpc_timeout=_collect_rpc_timeout(spec, self.rpc_timeout),
                params=params,
                on_node=node_progress,
                on_batch=on_batch,
            )
            _, pack_errors = await asyncio.to_thread(_pack_collect, results, spec.name)
            errors.update(pack_errors)
            if not rows:
                records = [r for res in results for r in res.records.get(spec.name, [])]
                if records:
                    _, rows = await asyncio.to_thread(render.table_rows, spec, records)
            shown, note = render.truncate_rows(rows)
            return {
                "report": spec.name,
                "source": "collected",
                "loading": False,
                "columns": titles,
                "rows": shown,
                "errors": errors,
                "truncated": truncated or note is not None,
                "message": note,
            }

    async def _findings(self, at: str | None) -> tuple[list[Finding], Any, dict[tuple[str, str], float]]:
        when = _instant(at)
        if when is None:
            findings = [f for f in self.engine.current("checks") if isinstance(f, Finding)]
            return findings, self.engine.fabric(), self.engine.raised
        then = await self._then(views.fabric_at, self.fabric.nodes, when, required())
        return run_checks(then), then, {}

    async def checks(self, at: str | None = None, names: Sequence[str] = ()) -> list[dict[str, Any]]:
        findings, _, raised = await self._findings(at)
        return [
            {**f.model_dump(mode="json"), "raised": raised.get((f.node, record_key(f)))}
            for f in findings
            if not names or f.check in names
        ]

    async def incidents(self, at: str | None = None) -> list[dict[str, Any]]:
        findings, where, raised = await self._findings(at)
        return [i.as_dict() for i in correlate(findings, where, raised)]

    async def history(
        self,
        since: str | None = None,
        until: str | None = None,
        nodes: Sequence[str] = (),
        reports: Sequence[str] = (),
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"nodes": list(nodes), "reports": list(reports), "limit": limit}
        if since:
            query["since"] = _instant(since)
        if until:
            query["until"] = _instant(until)
        return await self._store().run(views.history_with_causes, **query)  # type: ignore[no-any-return]

    async def drift(
        self, since: str, until: str | None = None, reports: Sequence[str] = (), nodes: Sequence[str] = ()
    ) -> dict[str, Any]:
        store = self._store()
        baseline = await store.run(Store.baseline, since)
        try:
            start = baseline["ts"] if baseline else parse_time(since)
        except ValueError as exc:
            raise ServiceError(f"{since!r} is neither a baseline nor a time") from exc
        end = _instant(until)
        names = list(reports) or await store.run(views.drift_reports)
        for name in names:
            _report(name)
        changes = []
        for name in names:
            changes += await self._then(views.drift, name, start, end, list(nodes))
        return {"from": start, "to": end or time.time(), "changes": [c.as_dict() for c in changes]}

    async def baselines(self) -> list[dict[str, Any]]:
        return await self._store().run(Store.baselines)  # type: ignore[no-any-return]

    async def _fabric(self, reports: Sequence[str], at: str | None) -> Any:
        """The fabric over *reports*: live, at an instant, or - for one not streamed - collected now."""
        when = _instant(at)
        if when is not None:
            return await self._then(views.fabric_at, self.fabric.nodes, when, list(reports))
        if set(reports) <= set(self.engine.plan.reports):
            return self.engine.fabric()
        return await views.fabric_now(self.fabric.nodes, reports)

    async def lens(
        self, name: str, args: dict[str, Any] | None = None, at: str | None = None
    ) -> dict[str, Any]:
        spec = _lens(name)
        fabric = await self._fabric(spec.requires, at)
        return {"lens": name, "records": _run_lens(spec, fabric, args)}

    async def topology(self, at: str | None = None) -> dict[str, Any]:
        from .topology import REQUIRES

        if at is None:
            fabric = await self._fabric(REQUIRES, None)
            findings = [f for f in self.engine.current("checks") if isinstance(f, Finding)]
        else:
            fabric = await self._fabric(sorted({*REQUIRES, *required()}), at)
            findings = run_checks(fabric)
        return _topology(fabric, findings, self.fabric.name)


# --------------------------------------------------------------------------- #
# a running server, over HTTP
# --------------------------------------------------------------------------- #


class RemoteService:
    def __init__(self, remote: Remote, filters: Sequence[str] = ()) -> None:
        self.remote = remote
        #: Inventory filters every question is narrowed by, as ``fcling -i`` gives them.
        self.filters = list(filters)

    async def _get(self, path: str, query: list[tuple[str, str]]) -> Any:
        try:
            return await asyncio.to_thread(self.remote.get, path, query)
        except RemoteError as exc:
            raise ServiceError(str(exc)) from exc

    def _inv(self, filters: Sequence[str] = ()) -> list[tuple[str, str]]:
        return [("inv", f) for f in [*self.filters, *filters]]

    async def status(self) -> dict[str, Any]:
        answer = await self._get("/api/status", [])
        answer["source"] = f"server {self.remote.url}"
        return answer  # type: ignore[no-any-return]

    async def reports(self) -> list[dict[str, Any]]:
        return await self._get("/api/reports", [])  # type: ignore[no-any-return]

    async def records(
        self,
        report: str,
        params: dict[str, Any] | None = None,
        filters: Sequence[str] = (),
        nodes: Sequence[str] = (),
        at: str | None = None,
    ) -> dict[str, Any]:
        query = self._inv(filters) + [("node", n) for n in nodes]
        query += [(k, str(v)) for k, v in (params or {}).items() if v is not None]
        if at:
            query.append(("at", at))
        return await self._get(f"/api/reports/{report}", query)  # type: ignore[no-any-return]

    async def checks(self, at: str | None = None, names: Sequence[str] = ()) -> list[dict[str, Any]]:
        query = self._inv() + [("check", n) for n in names] + ([("at", at)] if at else [])
        return await self._get("/api/checks", query)  # type: ignore[no-any-return]

    async def incidents(self, at: str | None = None) -> list[dict[str, Any]]:
        return await self._get("/api/incidents", self._inv() + ([("at", at)] if at else []))  # type: ignore[no-any-return]

    async def history(
        self,
        since: str | None = None,
        until: str | None = None,
        nodes: Sequence[str] = (),
        reports: Sequence[str] = (),
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        query = [("node", n) for n in nodes] + [("report", r) for r in reports] + [("limit", str(limit))]
        query += [("since", since)] if since else []
        query += [("until", until)] if until else []
        return await self._get("/api/history", query)  # type: ignore[no-any-return]

    async def drift(
        self, since: str, until: str | None = None, reports: Sequence[str] = (), nodes: Sequence[str] = ()
    ) -> dict[str, Any]:
        query = [("from", since), *(("report", r) for r in reports), *(("node", n) for n in nodes)]
        query += [("to", until)] if until else []
        return await self._get("/api/drift", query)  # type: ignore[no-any-return]

    async def baselines(self) -> list[dict[str, Any]]:
        return await self._get("/api/baselines", [])  # type: ignore[no-any-return]

    async def lens(
        self, name: str, args: dict[str, Any] | None = None, at: str | None = None
    ) -> dict[str, Any]:
        query = self._inv() + [(k, str(v)) for k, v in (args or {}).items() if v not in (None, "")]
        query += [("at", at)] if at else []
        return await self._get(f"/api/lenses/{name}", query)  # type: ignore[no-any-return]

    async def topology(self, at: str | None = None) -> dict[str, Any]:
        return await self._get("/api/topology", [("at", at)] if at else [])  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- #
# the nodes themselves, one-shot
# --------------------------------------------------------------------------- #

_NO_HISTORY = (
    "no history here: this answers from the nodes one-shot; run `fcling serve` and point at it with --server"
)


class LocalService:
    def __init__(self, fabric: Inventory, rpc_timeout: float = 30.0) -> None:
        self.fabric = fabric
        self.rpc_timeout = rpc_timeout

    async def status(self) -> dict[str, Any]:
        return {
            "fabric": self.fabric.name,
            "source": "one-shot from the nodes, no history",
            "nodes": [
                {"name": n.name, "address": n.address, "labels": dict(n.labels), "status": "not connected"}
                for n in self.fabric.nodes
            ],
            "streamed": [],
        }

    async def reports(self) -> list[dict[str, Any]]:
        return catalog()

    async def records(
        self,
        report: str,
        params: dict[str, Any] | None = None,
        filters: Sequence[str] = (),
        nodes: Sequence[str] = (),
        at: str | None = None,
    ) -> dict[str, Any]:
        if at:
            raise ServiceError(_NO_HISTORY)
        spec = _report(report)
        params = _params(spec, dict(params or {}))
        if _bgp_rib_pending(spec, params):
            return _empty_report(
                spec,
                "Narrow the RIB with route keys or network-instance, or choose List all routes.",
            )
        names = set(select_nodes(self.fabric, filters, nodes))
        selected = [n for n in self.fabric.nodes if n.name in names]
        results = await collect(selected, [spec], rpc_timeout=self.rpc_timeout, params=params)
        rows, errors = await asyncio.to_thread(_pack_collect, results, spec.name)
        return {"report": spec.name, "source": "collected", "records": rows, "errors": errors}

    async def _fabric(self, at: str | None) -> Any:
        if at:
            raise ServiceError(_NO_HISTORY)
        return await views.fabric_now(self.fabric.nodes, required(), self.rpc_timeout)

    async def checks(self, at: str | None = None, names: Sequence[str] = ()) -> list[dict[str, Any]]:
        fabric = await self._fabric(at)
        return [
            {**f.model_dump(mode="json"), "raised": None}
            for f in run_checks(fabric)
            if not names or f.check in names
        ]

    async def incidents(self, at: str | None = None) -> list[dict[str, Any]]:
        fabric = await self._fabric(at)
        return [i.as_dict() for i in correlate(run_checks(fabric), fabric)]

    async def history(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise ServiceError(_NO_HISTORY)

    async def drift(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise ServiceError(_NO_HISTORY)

    async def baselines(self) -> list[dict[str, Any]]:
        raise ServiceError(_NO_HISTORY)

    async def lens(
        self, name: str, args: dict[str, Any] | None = None, at: str | None = None
    ) -> dict[str, Any]:
        spec = _lens(name)
        if at:
            raise ServiceError(_NO_HISTORY)
        fabric = await views.fabric_now(self.fabric.nodes, spec.requires, self.rpc_timeout)
        return {"lens": name, "records": _run_lens(spec, fabric, args)}

    async def topology(self, at: str | None = None) -> dict[str, Any]:
        from .topology import REQUIRES

        if at:
            raise ServiceError(_NO_HISTORY)
        fabric = await views.fabric_now(self.fabric.nodes, sorted({*REQUIRES, *required()}), self.rpc_timeout)
        return _topology(fabric, run_checks(fabric), self.fabric.name)

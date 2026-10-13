"""The tools fcli offers a model, defined once for every client of them.

An MCP client (:mod:`fcli.mcp`) and the in-app agent (:mod:`fcli.agent`)
call the same tools over the same :class:`~fcli.service.Service`, so they
see the same fabric. Every tool is read-only.

A tool's input is a pydantic model - its JSON schema is what the client is
told, and what a call is validated against before anything runs. Its output is
JSON, kept to what fits in a model's context: a report's table cells rather
than its full records unless asked, and at most ``limit`` rows, saying so when
there were more.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .render import cells, text
from .reports import REGISTRY
from .service import Service, ServiceError


class ToolError(Exception):
    """What a tool says instead of an answer: the model reads it and changes course."""


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input: type[_Input]
    run: Callable[[Service, Any], Awaitable[Any]]

    @property
    def input_schema(self) -> dict[str, Any]:
        schema = self.input.model_json_schema()
        schema.pop("title", None)
        return schema

    async def call(self, service: Service, arguments: dict[str, Any] | None) -> str:
        """Validate *arguments*, run, and answer as JSON text; :class:`ToolError` on any failure."""
        try:
            args = self.input.model_validate(arguments or {})
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}" for e in exc.errors()
            )
            raise ToolError(f"invalid arguments for {self.name}: {problems}") from exc
        try:
            result = await self.run(service, args)
        except ServiceError as exc:
            raise ToolError(str(exc)) from exc
        return json.dumps(result, separators=(",", ":"), default=str)


TOOLS: dict[str, Tool] = {}


def tool(name: str, input: type[_Input]) -> Callable[[Callable[[Service, Any], Awaitable[Any]]], Any]:  # noqa: A002
    """Register the decorated coroutine as tool *name*; its docstring is the description."""

    def register(run: Callable[[Service, Any], Awaitable[Any]]) -> Callable[[Service, Any], Awaitable[Any]]:
        TOOLS[name] = Tool(name, " ".join((run.__doc__ or "").split()), input, run)
        return run

    return register


# --------------------------------------------------------------------------- #
# inputs shared by several tools
# --------------------------------------------------------------------------- #

_AT: Any = Field(
    default=None,
    description="Answer as the fabric was then, from the kept history: '30m', '2h', '2026-10-12T14:02'. "
    "Omit for now.",
)
_NODES: Any = Field(default_factory=list, description="Only these nodes, by inventory name.")
_INV: Any = Field(
    default_factory=list,
    description="Only nodes whose labels match, as key=glob pairs ANDed: ['role=leaf'].",
)


def _where(rows: list[dict[str, Any]], where: list[str]) -> list[dict[str, Any]]:
    """Rows whose column matches each ``column=regex``, case-insensitive."""
    for spec in where:
        column, sep, pattern = spec.partition("=")
        if not sep:
            raise ToolError(f"where {spec!r} is not column=regex")
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ToolError(f"where {spec!r}: {exc}") from exc
        name = " ".join(column.split()).lower()
        if rows and not any(name == " ".join(k.split()).lower() for k in rows[0]):
            raise ToolError(f"no column {column!r}; columns are {', '.join(rows[0])}")
        rows = [
            r
            for r in rows
            if any(name == " ".join(k.split()).lower() and regex.search(text(v)) for k, v in r.items())
        ]
    return rows


def _limited(rows: list[Any], limit: int) -> dict[str, Any]:
    out: dict[str, Any] = {"count": len(rows), "rows": rows[:limit]}
    if len(rows) > limit:
        out["truncated"] = (
            f"{len(rows) - limit} more rows not shown: narrow with nodes, inv or where, or raise limit"
        )
    return out


# --------------------------------------------------------------------------- #
# the fabric
# --------------------------------------------------------------------------- #


class _NoInput(_Input):
    pass


@tool("fabric_status", _NoInput)
async def fabric_status(service: Service, args: _NoInput) -> Any:
    """The fabric at a glance: its nodes with their labels and whether each is reachable,
    the reports kept live, and how many incidents are open. Start here."""
    return await service.status()


@tool("list_reports", _NoInput)
async def list_reports(service: Service, args: _NoInput) -> Any:
    """Every report get_report can return - interfaces, BGP, routes, EVPN, LLDP, hardware... -
    with its columns and parameters. Reports are operational state, not configuration."""
    return [
        {
            "name": r["name"],
            "help": r["help"],
            "columns": r["columns"],
            "params": r["params"],
            **({"bgp_rib_family": True} if r["hidden"] else {}),
        }
        for r in await service.reports()
    ]


class _ReportInput(_Input):
    name: str = Field(description="The report, as list_reports names it: 'bgp-peers', 'subif', 'ipv4-rib'...")
    nodes: list[str] = _NODES
    inv: list[str] = _INV
    where: list[str] = Field(
        default_factory=list,
        description="Only rows whose column matches, as column=regex pairs ANDed, case-insensitive: "
        "['state=^(?!up)'], ['NI=ipvrf-1'].",
    )
    params: dict[str, str] = Field(
        default_factory=dict,
        description="The report's own parameters, as list_reports lists them: {'address': '10.0.0.1'}.",
    )
    detail: bool = Field(
        default=False,
        description="Full records - every field, nested values included - rather than table rows. "
        "Larger: ask for it on a few nodes.",
    )
    at: str | None = _AT
    limit: int = Field(default=100, ge=1, le=2000, description="At most this many rows.")


@tool("get_report", _ReportInput)
async def get_report(service: Service, args: _ReportInput) -> Any:
    """One report across the selected nodes: its rows, the node each is on, and the nodes that
    could not answer. Narrow it with nodes, inv and where rather than reading everything."""
    report = REGISTRY.get(args.name)
    if report is None:
        raise ToolError(f"no report {args.name!r}: list_reports says which there are")
    answer = await service.records(args.name, args.params, args.inv, args.nodes, args.at)
    records = [report.record.model_validate(r) for r in answer["records"]]
    if args.detail:
        rows = _where_records(records, report, args.where)
    else:
        rows = _where([{"node": r.node, **_compact(cells(report, r))} for r in records], args.where)
    out = {"report": args.name, "source": answer.get("source"), **_limited(rows, args.limit)}
    if answer.get("errors"):
        out["errors"] = answer["errors"]
    return out


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    """Table cells as one line each, without the empty ones."""
    return {
        " ".join(k.split()): (", ".join(text(x) for x in v) if isinstance(v, list | tuple) else v)
        for k, v in row.items()
        if v not in (None, "", [], ())
    }


def _where_records(records: list[Any], report: Any, where: list[str]) -> list[dict[str, Any]]:
    """Full records, filtered on their table cells - the same ``where`` the rows take."""
    rows = [{"node": rec.node, **_compact(cells(report, rec)), "_index": i} for i, rec in enumerate(records)]
    return [records[row["_index"]].model_dump(mode="json") for row in _where(rows, where)]


# --------------------------------------------------------------------------- #
# what is wrong
# --------------------------------------------------------------------------- #


class _IncidentsInput(_Input):
    at: str | None = _AT
    verbose: bool = Field(
        default=False, description="Every finding of each incident, not just its root and count."
    )


@tool("get_incidents", _IncidentsInput)
async def get_incidents(service: Service, args: _IncidentsInput) -> Any:
    """What is wrong with the fabric, grouped by root cause, worst first - the triage entry point.
    An incident's root is the cause to explain; its related findings are consequences of it, not
    separate problems. A 'pattern' is one cause repeated in many places."""
    found = await service.incidents(args.at)
    if args.verbose:
        return found
    return [
        {
            **{k: v for k, v in i.items() if k != "related"},
            "related": len(i["related"]),
            "related_checks": sorted({f["check"] for f in i["related"]}),
        }
        for i in found
    ]


class _ChecksInput(_Input):
    checks: list[str] = Field(
        default_factory=list, description="Only these checks: 'bgp_down', 'itf_down'..."
    )
    nodes: list[str] = _NODES
    at: str | None = _AT
    limit: int = Field(default=200, ge=1, le=2000)


@tool("get_checks", _ChecksInput)
async def get_checks(service: Service, args: _ChecksInput) -> Any:
    """Every finding of the fabric checks, ungrouped: check, node, subject, detail, and since when
    it has been raised where a history says. get_incidents is usually the better start."""
    found = await service.checks(args.at, args.checks)
    if args.nodes:
        found = [f for f in found if f["node"] in args.nodes]
    return _limited(found, args.limit)


# --------------------------------------------------------------------------- #
# what changed
# --------------------------------------------------------------------------- #


class _HistoryInput(_Input):
    since: str = Field(default="15m", description="How far back: '15m', '2h', '1d', or a time.")
    until: str | None = Field(default=None, description="Up to when; omit for now.")
    nodes: list[str] = _NODES
    reports: list[str] = Field(
        default_factory=list,
        description="Only these reports' changes; 'checks' is findings raised and cleared, "
        "'config-commits' is configuration commits.",
    )
    limit: int = Field(default=100, ge=1, le=2000)


@tool("get_history", _HistoryInput)
async def get_history(service: Service, args: _HistoryInput) -> Any:
    """What changed, oldest first: each record that appeared, went or changed, with its values
    before and after, and the configuration commit likely to have caused it ('cause'). A change
    just before an incident started is usually its trigger."""
    changes = await service.history(args.since, args.until, args.nodes, args.reports, args.limit)
    return [_change(c) for c in changes]


def _change(c: dict[str, Any]) -> dict[str, Any]:
    """A change with only what moved: the fields that differ, not two whole records."""
    out: dict[str, Any] = {k: c[k] for k in ("ts", "node", "report", "op", "key") if k in c}
    before, after = c.get("before") or {}, c.get("after") or {}
    if c.get("op") == "change":
        out["changed"] = {
            k: [before.get(k), after.get(k)] for k in after if k != "node" and before.get(k) != after.get(k)
        }
    elif c.get("op") == "add":
        out["record"] = after
    else:
        out["record"] = before
    if c.get("cause"):
        out["cause"] = c["cause"]
    return out


class _DriftInput(_Input):
    since: str = Field(description="A baseline's name, or a time: how the fabric differs from then.")
    until: str | None = Field(default=None, description="Compare with then rather than now.")
    reports: list[str] = Field(
        default_factory=list, description="Only these reports; omit for every one kept."
    )
    nodes: list[str] = _NODES


@tool("get_drift", _DriftInput)
async def get_drift(service: Service, args: _DriftInput) -> Any:
    """How the fabric now differs from a baseline (a known-good moment someone named) or a time:
    what came, went and changed since - the end result, not every step in between."""
    answer = await service.drift(args.since, args.until, args.reports, args.nodes)
    return {**answer, "changes": [_change(c) for c in answer["changes"]]}


@tool("list_baselines", _NoInput)
async def list_baselines(service: Service, args: _NoInput) -> Any:
    """The named baselines get_drift can compare against, with when each was taken."""
    return await service.baselines()


# --------------------------------------------------------------------------- #
# lenses: questions across the fabric
# --------------------------------------------------------------------------- #


async def _lens(service: Service, name: str, args: dict[str, Any], at: str | None, limit: int) -> Any:
    """A lens's records as table rows, in the order it gave them."""
    from .lenses import LENSES

    spec = LENSES[name]
    answer = await service.lens(name, args, at)
    records = [spec.record.model_validate(r) for r in answer["records"]]
    return _limited([{"node": r.node, **_compact(cells(spec, r))} for r in records], limit)


class _WhereInput(_Input):
    address: str = Field(description="An IP (10.1.3.13, 2001:db8::1) or MAC (AA:C1:AB:B5:75:AD) address.")
    at: str | None = _AT
    limit: int = Field(default=100, ge=1, le=2000)


@tool("locate_address", _WhereInput)
async def locate_address(service: Service, args: _WhereInput) -> Any:
    """Every place in the fabric that knows an IP or MAC address: where it is configured, the ARP/ND
    entries for it, the port, ethernet-segment or VTEP each node has it on or behind, and the host
    routes to it. Says whether a MAC on several leaves is multihomed (one segment) or a duplicate."""
    return await _lens(service, "where", {"address": args.address}, args.at, args.limit)


class _PathInput(_Input):
    source: str = Field(
        description="Where the traffic starts: a node name, or a host's IP or MAC (it enters at its leaf)."
    )
    destination: str = Field(description="The IP or MAC address it is sent to.")
    ni: str | None = Field(
        default=None,
        description="The network-instance to start in, on a node source: 'ipvrf-1', 'macvrf-101'. "
        "Omit to use the one a host source sits in, else 'default'.",
    )
    at: str | None = _AT


@tool("trace_path", _PathInput)
async def trace_path(service: Service, args: _PathInput) -> Any:
    """How the fabric would forward to an address, hop by hop, from its route and bridge tables -
    not a probe: each hop's route, egress and next node, ECMP branches, VXLAN tunnels and where they
    come out, ending in where it is delivered or why it is not (no-route, discard, dead-end, loop)."""
    args_ = {"source": args.source, "destination": args.destination, "ni": args.ni}
    return await _lens(service, "path", args_, args.at, 500)


class _ServiceInput(_Input):
    name: str = Field(description="The network-instance: 'macvrf-101', 'ipvrf-1'.")
    at: str | None = _AT


@tool("service_detail", _ServiceInput)
async def service_detail(service: Service, args: _ServiceInput) -> Any:
    """One service (a mac-vrf or ip-vrf) as every node carrying it sees it: state, EVI, VNI, route
    targets, ports, MACs learned, and the inconsistencies between nodes - mismatched VNI or route
    targets, designated-forwarder problems."""
    return await _lens(service, "service", {"name": args.name}, args.at, 500)


class _EndpointsInput(_Input):
    nodes: list[str] = _NODES
    where: list[str] = Field(
        default_factory=list,
        description="Only rows whose column matches, as column=regex pairs ANDed: ['MAC-VRF=macvrf-101'].",
    )
    at: str | None = _AT
    limit: int = Field(default=200, ge=1, le=2000)


@tool("list_endpoints", _EndpointsInput)
async def list_endpoints(service: Service, args: _EndpointsInput) -> Any:
    """Every host attached to the fabric, on each node it is attached to: IP, MAC, the IP-VRF and
    MAC-VRF, the access subinterface, the ethernet-segment, and its LLDP name."""
    answer = await _lens(service, "endpoints", {}, args.at, 100_000)
    rows = [r for r in answer["rows"] if not args.nodes or r["node"] in args.nodes]
    return _limited(_where(rows, args.where), args.limit)


class _TopologyInput(_Input):
    at: str | None = _AT


@tool("get_topology", _TopologyInput)
async def get_topology(service: Service, args: _TopologyInput) -> Any:
    """The fabric as a graph: each node's tier (spine, leaf, gateway, core) and health, the cables
    between nodes and their state (a 'lost' cable was seen before and is gone), the clients attached
    and the services they are in, and the ethernet-segments."""
    graph = await service.topology(args.at)
    return {
        "nodes": graph["nodes"],
        "links": [
            {
                "a": x["a"],
                "b": x["b"],
                "count": x["count"],
                "state": x["state"],
                **({"down": [c for c in x["cables"] if c["state"] != "up"]} if x["state"] != "up" else {}),
                **({"findings": x["findings"]} if x["findings"] else {}),
            }
            for x in graph["links"]
        ],
        "clients": [
            {
                "name": c["label"],
                "esi": c["esi"],
                "ports": sorted({f"{a['node']} {a['port']}" for a in c["attachments"]}),
                "services": sorted({a["service"] for a in c["attachments"]}),
            }
            for c in graph["clients"]
        ],
        "segments": graph["segments"],
        "outside": graph["outside"],
        "components": graph["components"],
    }


async def call(service: Service, name: str, arguments: dict[str, Any] | None) -> str:
    tool_ = TOOLS.get(name)
    if tool_ is None:
        raise ToolError(f"no tool {name!r}")
    return await tool_.call(service, arguments)

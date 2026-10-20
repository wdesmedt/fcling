"""The HTTP API as one table: :func:`~fcli.server.create_app` routes from it, ``/api/openapi.json``
describes it.

One table so the two cannot drift: a route not in :data:`ENDPOINTS` is not served.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from pydantic.json_schema import models_json_schema


@dataclass(frozen=True)
class Q:
    """A query parameter."""

    name: str
    help: str
    #: May be given more than once: ``?node=leaf1&node=leaf2``.
    many: bool = False
    required: bool = False
    type: str = "string"


@dataclass(frozen=True)
class Endpoint:
    path: str
    methods: tuple[str, ...]
    #: The name of its handler in :func:`~fcli.server.create_app`.
    handler: str
    summary: str
    query: tuple[Q, ...] = ()
    #: The JSON body it takes, as properties of an object: ``{name: (type, help)}``.
    body: dict[str, tuple[str, str]] | None = None
    #: It answers with server-sent events, not JSON.
    events: bool = False


_AT = Q("at", "An instant in the history (ISO time, epoch seconds or 10m ago): the answer as it was then.")
_INV = Q("inv", "Only the nodes whose labels match, like role=leaf.", many=True)
_NODE = Q("node", "Only this node.", many=True)
_REPORT = Q("report", "Only this report.", many=True)
_TABLE = Q("table", "1: the answer also as the CLI's table, columns and rows of text.")

ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("/api", ("GET",), "index", "Where to start: this API's description and the MCP endpoint."),
    Endpoint("/api/openapi.json", ("GET",), "openapi", "This API, as OpenAPI 3.1."),
    Endpoint("/api/status", ("GET",), "status", "The fabric, its nodes, how each is doing, open incidents."),
    Endpoint("/api/reports", ("GET",), "reports", "Every report: columns, parameters, record schema."),
    Endpoint(
        "/api/reports/{name}",
        ("GET",),
        "report_records",
        "A report's records: live, at an instant, or collected on the spot for one not streamed. "
        "The report's own parameters, listed by /api/reports, are query parameters too.",
        (_INV, _NODE, _AT, _TABLE, Q("progress", "1: collect node by node, progress as server-sent events.")),
    ),
    Endpoint(
        "/api/checks",
        ("GET",),
        "checks",
        "What is wrong: findings, live or at an instant.",
        (_INV, _NODE, _AT, Q("check", "Only this check.", many=True)),
    ),
    Endpoint(
        "/api/incidents",
        ("GET",),
        "incidents",
        "The findings grouped by root cause, each saying if acknowledged.",
        (_INV, _NODE, _AT),
    ),
    Endpoint(
        "/api/acks",
        ("GET", "POST", "DELETE"),
        "acknowledgements",
        "GET what is acknowledged; POST {incident, note} to acknowledge one; DELETE ?incident= to no longer.",
        (Q("incident", "DELETE: the incident no longer acknowledged."),),
        body={"incident": ("string", "The incident's id."), "note": ("string", "Why.")},
    ),
    Endpoint(
        "/api/history",
        ("GET",),
        "history",
        "The kept changes, oldest first, each with its likely cause.",
        (
            Q("since", "From this time (ISO time, epoch seconds or 10m ago)."),
            Q("until", "Up to this time."),
            Q("baseline", "Only since this baseline; empty for the latest one. Not with since."),
            _NODE,
            _REPORT,
            Q("q", "Free text: only the changes every word is found in (node, report, key or a value)."),
            Q("limit", "At most this many changes (1000).", type="integer"),
            Q("after", "Only changes after this sequence number.", type="integer"),
        ),
    ),
    Endpoint(
        "/api/drift",
        ("GET",),
        "drift",
        "What differs between a baseline (or a time) and now, or another time.",
        (
            Q("from", "A baseline's name or a time.", required=True),
            Q("to", "A baseline's name or a time; now when not given."),
            _REPORT,
            _NODE,
        ),
    ),
    Endpoint(
        "/api/configs",
        ("GET",),
        "configs",
        "Each node's configuration commits, as the history keeps them.",
        (_NODE,),
    ),
    Endpoint(
        "/api/configs/diff",
        ("GET",),
        "config_diff",
        "A configuration commit's diff against the one before, or against base.",
        (
            Q("node", "The node.", required=True),
            Q("commit", "The commit; the latest when not given.", type="integer"),
            Q("base", "Compare against this commit instead.", type="integer"),
            Q("context", "Lines of context (3).", type="integer"),
            Q("seen", "The commit seen at this epoch time, when its number is not known.", type="number"),
            _AT,
        ),
    ),
    Endpoint(
        "/api/baselines",
        ("GET", "POST"),
        "baselines",
        "Named instants to compare against; POST {name, at, comment} to keep one.",
        body={
            "name": ("string", "Its name."),
            "at": ("string", "The instant; now when not given."),
            "comment": ("string", "What it is."),
        },
    ),
    Endpoint("/api/baselines/{name}", ("DELETE",), "baselines", "Forget a baseline."),
    Endpoint(
        "/api/events",
        ("GET",),
        "events",
        "Server-sent events: changes, node status, configuration commits, acknowledgements. "
        "Reconnect with Last-Event-ID (or after) to catch up.",
        (_REPORT, _NODE, Q("after", "Replay the changes after this sequence number first.", type="integer")),
        events=True,
    ),
    Endpoint("/api/lenses", ("GET",), "lenses", "Every lens: its arguments, columns and record schema."),
    Endpoint(
        "/api/lenses/{name}",
        ("GET",),
        "lens",
        "Ask a lens a question about the whole fabric. "
        "Its arguments, listed by /api/lenses, are query parameters.",
        (_AT, _TABLE),
    ),
    Endpoint(
        "/api/topology",
        ("GET",),
        "topology",
        "The fabric as a graph: tiers, cables, clients, segments, health.",
        (_AT,),
    ),
    Endpoint(
        "/api/chat",
        ("POST",),
        "chat",
        "Ask the agent; its answer as server-sent events.",
        body={
            "question": ("string", "The question."),
            "conversation": ("string", "Carry on this conversation."),
            "context": ("object", "What the UI shows, for the agent to take into account."),
        },
        events=True,
    ),
)


def index() -> dict[str, Any]:
    """What ``GET /api`` answers: where an agent or script finds out more."""
    return {
        "name": "fcling",
        "mcp": "/mcp",
        "openapi": "/api/openapi.json",
        "help": "Agents: the MCP tools at /mcp. Plain HTTP clients: the routes in /api/openapi.json.",
    }


def _version() -> str:
    try:
        return version("fcling")
    except PackageNotFoundError:
        return "0.0.0"


@cache
def openapi() -> dict[str, Any]:
    """Every route in :data:`ENDPOINTS` as OpenAPI 3.1, with the records of every report and lens.

    Code only, never the fabric: the same for every server of this version.
    """
    from .lenses import LENSES
    from .reports import REGISTRY

    models = {r.record for r in REGISTRY.values()} | {lens.record for lens in LENSES.values()}
    refs, defs = models_json_schema(
        [(m, "serialization") for m in sorted(models, key=lambda m: m.__qualname__)],
        ref_template="#/components/schemas/{model}",
    )
    ref = {m: refs[(m, "serialization")] for m in models}
    named = {
        "/api/reports/{name}": (sorted(REGISTRY), {n: ref[r.record] for n, r in REGISTRY.items()}),
        "/api/lenses/{name}": (sorted(LENSES), {n: ref[lens.record] for n, lens in LENSES.items()}),
    }

    paths: dict[str, Any] = {}
    for e in ENDPOINTS:
        params: list[dict[str, Any]] = []
        if "{name}" in e.path:
            param: dict[str, Any] = {
                "name": "name",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
            if e.path in named:
                param["schema"]["enum"] = named[e.path][0]
            params.append(param)
        for q in e.query:
            schema: dict[str, Any] = {"type": q.type}
            if q.many:
                schema = {"type": "array", "items": schema}
            params.append(
                {
                    "name": q.name,
                    "in": "query",
                    "required": q.required,
                    "description": q.help,
                    "schema": schema,
                }
            )
        content: dict[str, Any] = {"text/event-stream": {}} if e.events else {"application/json": {}}
        responses: dict[str, Any] = {
            "200": {"description": "The answer.", "content": content},
            "400": {"$ref": "#/components/responses/Error"},
            "401": {"$ref": "#/components/responses/Unauthorized"},
        }
        operation: dict[str, Any] = {"summary": e.summary, "parameters": params, "responses": responses}
        if e.path in named:
            # Each name's records, linked: the answer's records follow the named one's schema.
            operation["x-records"] = named[e.path][1]
        if e.path in ("/api", "/api/openapi.json"):
            operation["security"] = []
            del responses["401"]
        methods = paths.setdefault(e.path, {})
        for method in e.methods:
            op = {
                **operation,
                "operationId": f"{e.handler}_{method.lower()}" if len(e.methods) > 1 else e.handler,
            }
            if e.body and method == "POST":
                op["requestBody"] = {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    k: {"type": t, "description": h} for k, (t, h) in e.body.items()
                                },
                            }
                        }
                    }
                }
            methods[method.lower()] = op
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "fcling",
            "version": _version(),
            "description": "Reports, findings and the history of an SR Linux fabric, streamed over gNMI. "
            "Agents: the same answers as MCP tools at /mcp.",
        },
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "When the server has a token: Authorization: Bearer <token>, or ?token=.",
                }
            },
            "schemas": defs.get("$defs", {}),
            "responses": {
                "Error": {
                    "description": "What was wrong with the question: {error}.",
                    "content": {"application/json": {}},
                },
                "Unauthorized": {"description": "No right token.", "content": {"application/json": {}}},
            },
        },
        "security": [{"bearer": []}],
    }

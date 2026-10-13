"""HTTP, SSE and MCP over the engine: one process, one live state, every client.

    GET  /api/status                 the fabric, its nodes, how each is doing, open incidents
    GET  /api/reports                every report: columns, parameters, record schema
    GET  /api/reports/{name}         its records: live, at an instant (``at``), or
                                     collected on the spot for one not streamed
    GET  /api/checks                 what is wrong: findings, live or ``at`` an instant
    GET  /api/incidents              the findings grouped by root cause
    GET  /api/history                the kept changes, oldest first, with their likely cause
    GET  /api/drift                  what differs between a baseline (or time) and now
    GET|POST /api/baselines          named instants to compare against
    GET  /api/events                 server-sent events: changes and node status
    GET  /api/lenses[/{name}]        where, path, service, endpoints: questions about the whole fabric
    GET  /api/topology               the fabric as a graph: tiers, cables, clients, segments, health
    POST /api/chat                   ask the agent; its answer as server-sent events
         /mcp                        the agent's tools, for any MCP client (streamable HTTP)
    GET  /                           the web UI: topology and incidents, over this same API

Every answer comes from :class:`~fcli.service.EngineService`, which is also
what the MCP tools and the agent use: they cannot see a different fabric.

Bound to localhost unless told otherwise; ``token`` makes every call carry
``Authorization: Bearer <token>`` (or ``?token=``). The UI's own files hold no
data and need none: open it with ``/?token=...`` and it passes the token on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import views
from .engine import Engine
from .inventory import Inventory
from .service import EngineService, ServiceError, select_nodes
from .store import Store, parse_time

logger = logging.getLogger(__name__)

#: Events a slow client may fall behind by before it is cut off; it
#: reconnects with ``Last-Event-ID`` and catches up from the history.
QUEUE_SIZE = 10_000

_LOCAL = ("127.0.0.1", "localhost", "::1")

#: The web UI: static files, no build step.
UI = Path(__file__).parent / "ui"


class _Error(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class _Auth:
    """Plain ASGI, so a streamed response passes through untouched."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not _public(scope["path"]):
            request = Request(scope)
            given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            given = given or request.query_params.get("token", "")
            if not secrets.compare_digest(given.encode(), self.token.encode()):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _public(path: str) -> bool:
    """The UI's own files: the same for everyone, and no answer about the fabric."""
    return path == "/" or path.startswith("/ui/")


def _sse(event: dict[str, Any]) -> str:
    lines = []
    if event.get("type") == "change" and event.get("seq"):
        lines.append(f"id: {event['seq']}")
    lines.append(f"event: {event['type']}")
    lines.append(f"data: {json.dumps(event, separators=(',', ':'), default=str)}")
    return "\n".join(lines) + "\n\n"


def _events(stream: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(stream, media_type="text/event-stream", headers={"cache-control": "no-cache"})


def create_app(
    engine: Engine,
    fabric: Inventory,
    token: str | None = None,
    run_engine: bool = True,
    host: str = "127.0.0.1",
    agent_client: Any = None,
) -> Starlette:
    """The app serving *engine*, which it runs for as long as it is up when *run_engine*."""
    service = EngineService(engine, fabric)
    query_list = lambda request, name: request.query_params.getlist(name)  # noqa: E731

    def store() -> Any:
        if engine.store is None:
            raise _Error(400, "no history is kept")
        return engine.store

    async def status(request: Request) -> Any:
        return await service.status()

    async def reports(request: Request) -> Any:
        return await service.reports()

    async def report_records(request: Request) -> Any:
        name = request.path_params["name"]
        params = {k: v for k, v in request.query_params.items() if k not in ("inv", "node", "at", "token")}
        try:
            return await service.records(
                name,
                params,
                query_list(request, "inv"),
                query_list(request, "node"),
                request.query_params.get("at"),
            )
        except ServiceError as exc:
            raise _Error(404 if str(exc).startswith("no report") else 400, str(exc)) from exc

    def _nodes(request: Request) -> set[str] | None:
        """The nodes an ``inv=`` / ``node=`` query narrows to; ``None`` for all."""
        if not query_list(request, "inv") and not query_list(request, "node"):
            return None
        return set(select_nodes(fabric, query_list(request, "inv"), query_list(request, "node")))

    async def checks(request: Request) -> Any:
        nodes = _nodes(request)
        found = await service.checks(request.query_params.get("at"), query_list(request, "check"))
        return [f for f in found if nodes is None or f["node"] in nodes]

    async def incidents(request: Request) -> Any:
        nodes = _nodes(request)
        found = await service.incidents(request.query_params.get("at"))
        return [i for i in found if nodes is None or set(i["nodes"]) & nodes]

    async def history(request: Request) -> Any:
        query = request.query_params
        args: dict[str, Any] = {
            "nodes": query_list(request, "node"),
            "reports": query_list(request, "report"),
            "limit": int(query.get("limit", "1000")),
            "after_seq": int(query.get("after", "0")),
        }
        try:
            for key in ("since", "until"):
                if key in query:
                    args[key] = parse_time(query[key])
        except ValueError as exc:
            raise _Error(400, str(exc)) from exc
        return await store().run(views.history_with_causes, **args)

    async def drift(request: Request) -> Any:
        since = request.query_params.get("from")
        if not since:
            raise _Error(400, "drift needs from=<baseline or time>")
        return await service.drift(
            since, request.query_params.get("to"), query_list(request, "report"), query_list(request, "node")
        )

    async def baselines(request: Request) -> Any:
        if request.method == "POST":
            body = await request.json()
            name = str(body.get("name") or "").strip()
            if not name:
                raise _Error(400, "a baseline needs a name")
            try:
                at = parse_time(str(body["at"])) if body.get("at") else time.time()
            except ValueError as exc:
                raise _Error(400, str(exc)) from exc
            await store().run(Store.set_baseline, name, at, str(body.get("comment") or ""))
        if request.method == "DELETE":
            name = request.path_params.get("name", "")
            if not await store().run(Store.delete_baseline, name):
                raise _Error(404, f"no baseline {name!r}")
        return await service.baselines()

    async def events(request: Request) -> Response:
        reports_wanted = set(query_list(request, "report"))
        nodes_wanted = set(query_list(request, "node"))
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(QUEUE_SIZE)

        def wanted(event: dict[str, Any]) -> bool:
            if event["type"] == "node":
                return not nodes_wanted or event["name"] in nodes_wanted
            if reports_wanted and event["report"] not in reports_wanted:
                return False
            return not nodes_wanted or event["node"] in nodes_wanted

        def listener(event: dict[str, Any]) -> None:
            if not wanted(event):
                return
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Cut off rather than buffered without end; it catches up on reconnect.
                stop()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.get_nowait()
                queue.put_nowait(None)

        stop = engine.listen(listener)
        last = request.headers.get("last-event-id") or request.query_params.get("after")

        async def stream() -> AsyncIterator[str]:
            try:
                yield _sse({"type": "status", "nodes": engine.status()})
                if last and engine.store is not None:
                    missed = await engine.store.history(
                        after_seq=int(last),
                        reports=sorted(reports_wanted),
                        nodes=sorted(nodes_wanted),
                        limit=QUEUE_SIZE,
                    )
                    for change in missed:
                        yield _sse({"type": "change", **change})
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    if event is None:
                        return
                    yield _sse(event)
            finally:
                stop()

        return _events(stream())

    async def lenses(request: Request) -> Any:
        from .lenses import LENSES

        return [
            {
                "name": lens.name,
                "help": lens.help,
                "args": [{"name": a.name, "help": a.help, "required": a.required} for a in lens.args],
                "columns": [c.title for c in lens.columns],
                "schema": lens.record.model_json_schema(),
            }
            for lens in LENSES.values()
        ]

    async def lens(request: Request) -> Any:
        args = {k: v for k, v in request.query_params.items() if k not in ("inv", "node", "at", "token")}
        try:
            return await service.lens(request.path_params["name"], args, request.query_params.get("at"))
        except ServiceError as exc:
            raise _Error(404 if str(exc).startswith("no lens") else 400, str(exc)) from exc

    async def topology(request: Request) -> Any:
        return await service.topology(request.query_params.get("at"))

    conversations: Any = None
    agent: Any = None

    async def chat(request: Request) -> Response:
        nonlocal conversations, agent
        body = await request.json()
        question = str(body.get("question") or "").strip()
        if not question:
            raise _Error(400, "a question is needed")
        try:
            from .agent import Agent, Conversations

            if agent is None:
                agent = Agent(service, fabric=fabric.name, client=agent_client)
                conversations = Conversations()
        except (ImportError, ValueError) as exc:
            raise _Error(400, f"the agent is not available: {exc}") from exc
        conversation = conversations.get(body.get("conversation"))
        context = body.get("context") if isinstance(body.get("context"), dict) else None

        async def stream() -> AsyncIterator[str]:
            yield _sse(
                {
                    "type": "start",
                    "conversation": conversation.id,
                    "provider": agent.provider,
                    "model": agent.model,
                    "effort": agent.effort,
                }
            )
            async for event in agent.turn(conversation, question, context):
                yield _sse(event)

        return _events(stream())

    def handled(handler: Callable[[Request], Awaitable[Any]]) -> Callable[[Request], Awaitable[Response]]:
        async def run(request: Request) -> Response:
            try:
                answer = await handler(request)
            except _Error as exc:
                return JSONResponse({"error": str(exc)}, status_code=exc.status)
            except ServiceError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            return answer if isinstance(answer, Response) else JSONResponse(answer)

        return run

    mcp_manager = _mcp_manager(service, host)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            if mcp_manager is not None:
                await stack.enter_async_context(mcp_manager.run())
            task = asyncio.create_task(engine.run()) if run_engine else None
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    routes = [
        Route("/api/status", handled(status)),
        Route("/api/reports", handled(reports)),
        Route("/api/reports/{name}", handled(report_records)),
        Route("/api/checks", handled(checks)),
        Route("/api/incidents", handled(incidents)),
        Route("/api/history", handled(history)),
        Route("/api/drift", handled(drift)),
        Route("/api/baselines", handled(baselines), methods=["GET", "POST"]),
        Route("/api/baselines/{name}", handled(baselines), methods=["DELETE"]),
        Route("/api/events", handled(events)),
        Route("/api/lenses", handled(lenses)),
        Route("/api/lenses/{name}", handled(lens)),
        Route("/api/topology", handled(topology)),
        Route("/api/chat", handled(chat), methods=["POST"]),
        Route("/", lambda request: FileResponse(UI / "index.html", headers={"cache-control": "no-cache"})),
        Mount("/ui", StaticFiles(directory=UI)),
    ]
    if mcp_manager is not None:
        from mcp.server.streamable_http_manager import StreamableHTTPASGIApp

        routes.append(Route("/mcp", StreamableHTTPASGIApp(mcp_manager)))
    middleware = [Middleware(_Auth, token=token)] if token else []
    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


def _mcp_manager(service: EngineService, host: str) -> Any:
    """The MCP endpoint's session manager, if the ``mcp`` extra is installed."""
    try:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings

        from .mcp import build_server
    except ImportError:
        logger.info("no MCP endpoint: install fcli[mcp]")
        return None
    security = None
    if host in _LOCAL:
        # A page in the browser must not be able to reach a localhost server by rebinding DNS.
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        )
    try:
        from ._version import __version__
    except ImportError:
        __version__ = "0.0.0"
    # Stateless: every tool call stands alone, and a restart loses no session.
    return StreamableHTTPSessionManager(
        app=build_server(service, __version__), stateless=True, security_settings=security
    )

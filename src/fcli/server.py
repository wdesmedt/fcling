"""HTTP, SSE and MCP over the engine: one process, one live state, every client.

    GET  /api/status                 the fabric, its nodes, how each is doing, open incidents
    GET  /api/reports                every report: columns, parameters, record schema
    GET  /api/reports/{name}         its records: live, at an instant (``at``), or
                                     collected on the spot for one not streamed
    GET  /api/checks                 what is wrong: findings, live or ``at`` an instant
    GET  /api/incidents              the findings grouped by root cause, each saying if acknowledged
    GET|POST|DELETE /api/acks        acknowledge an incident ({incident, note}), or no longer (?incident=)
    GET  /api/history                the kept changes, oldest first, with their likely cause;
                                     ?baseline=[name] only those since a baseline;
                                     ?q=text only those it is found in
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
``Authorization: Bearer <token>`` (or ``?token=``, or the cookie a right
``?token=`` sets). The UI's own files hold no data and need none: open it once
with ``/?token=...`` and that browser's tabs carry the cookie.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import render, views
from .acks import AckStore
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


class _Revalidated(StaticFiles):
    """The UI is ES modules with no build step and no hashed names: a browser
    using a cached copy without asking runs the old code after an upgrade."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "no-cache"
        return response


#: The cookie a browser keeps the token in, so every tab it opens on the UI is let in.
COOKIE = "fcli-token"


class _Auth:
    """Plain ASGI, so a streamed response passes through untouched.

    A right ``?token=`` also sets :data:`COOKIE` (HttpOnly, SameSite=Strict):
    the UI opened once with it, links opened from it in new tabs need none.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token

    def _right(self, given: str) -> bool:
        return bool(given) and secrets.compare_digest(given.encode(), self.token.encode())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        offered = request.query_params.get("token", "")
        if not _public(scope["path"]):
            given = (
                request.headers.get("authorization", "").removeprefix("Bearer ").strip(),
                offered,
                request.cookies.get(COOKIE, ""),
            )
            if not any(self._right(g) for g in given):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        if self._right(offered) and request.cookies.get(COOKIE) != offered:
            cookie = f"{COOKIE}={offered}; Path=/; HttpOnly; SameSite=Strict"
            if scope.get("scheme") == "https":
                cookie += "; Secure"

            async def send_with_cookie(message: Message) -> None:
                if message["type"] == "http.response.start":
                    headers = [*message.get("headers", []), (b"set-cookie", cookie.encode())]
                    message = {**message, "headers": headers}
                await send(message)

            await self.app(scope, receive, send_with_cookie)
            return
        await self.app(scope, receive, send)


#: Query parameters about the answer, not arguments of the report or lens.
_CONTROL = ("inv", "node", "at", "token", "table", "progress")


def _table_sync(
    spec: Any, answer: dict[str, Any], request: Request, engine: Engine | None = None
) -> dict[str, Any]:
    """With ``?table=1``, the answer also as the CLI's table: column titles, and each row's cells as text."""
    if request.query_params.get("table") not in ("1", "true", "yes"):
        return answer
    records = [spec.record.model_validate(r) for r in answer["records"]]
    params = {k: v for k, v in request.query_params.items() if k not in _CONTROL and k != "table"}
    cache_key = f"{spec.name}:{json.dumps(params, sort_keys=True)}"
    cache = engine.table_cache if engine is not None and answer.get("source") == "live" else None
    columns, rows = render.table_rows(spec, records, cache=cache, cache_key=cache_key)
    rows, note = render.truncate_rows(rows)
    out = {**answer, "columns": columns, "rows": rows}
    if note:
        out["truncated"] = True
        out["message"] = note
    return out


async def _table(
    spec: Any, answer: dict[str, Any], request: Request, engine: Engine | None = None
) -> dict[str, Any]:
    if request.query_params.get("table") not in ("1", "true", "yes"):
        return answer
    return await asyncio.to_thread(_table_sync, spec, answer, request, engine)


def _public(path: str) -> bool:
    """The UI's own files: the same for everyone, and no answer about the fabric."""
    return path == "/" or path.startswith("/ui/")


def _sse(event: dict[str, Any]) -> str:
    lines = []
    kind = event.get("type", "message")
    if kind == "change" and event.get("seq"):
        lines.append(f"id: {event['seq']}")
    lines.append(f"event: {kind}")
    lines.append(f"data: {json.dumps(event, separators=(',', ':'), default=str)}")
    return "\n".join(lines) + "\n\n"


def _events(stream: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(stream, media_type="text/event-stream", headers={"cache-control": "no-cache"})


async def _report_collect_stream(
    service: EngineService, spec: Any, request: Request, engine: Engine
) -> AsyncIterator[str]:
    """Collect a report node by node, reporting progress, then the table answer."""
    params = {k: v for k, v in request.query_params.items() if k not in _CONTROL}
    query_list = lambda request, name: request.query_params.getlist(name)  # noqa: E731
    names = select_nodes(
        service.fabric,
        query_list(request, "inv"),
        query_list(request, "node"),
    )
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def on_node(node: str, done: int, total: int) -> None:
        queue.put_nowait({"type": "progress", "node": node, "done": done, "total": total})

    async def work() -> None:
        try:
            from .service import _bgp_rib_pending, _empty_report
            from .service import _params as svc_params

            params_ok = svc_params(spec, dict(params))
            if _bgp_rib_pending(spec, params_ok):
                answer = _empty_report(
                    spec,
                    "Narrow the RIB with route keys or network-instance, or choose List all routes.",
                )
            else:

                async def emit(event: dict[str, Any]) -> None:
                    if event.get("type") == "partial":
                        ans = event["answer"]
                        await queue.put({"type": "partial", "answer": ans})
                    else:
                        await queue.put(event)

                answer = await service._collect_report_streaming(
                    spec, params_ok, names, emit, on_node=on_node
                )
            if not answer.get("columns"):
                answer = await _table(spec, {**answer, "records": answer.get("records", [])}, request, engine)
            await queue.put({"type": "result", "answer": answer})
        except ServiceError as exc:
            await queue.put({"type": "failed", "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            await queue.put({"type": "failed", "error": str(exc)})
        finally:
            await queue.put(None)

    worker = asyncio.create_task(work())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield _sse(event)
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


def create_app(
    engine: Engine,
    fabric: Inventory,
    token: str | None = None,
    run_engine: bool = True,
    host: str = "127.0.0.1",
    agent_client: Any = None,
    rpc_timeout: float = 30.0,
    acks: AckStore | None = None,
) -> Starlette:
    """The app serving *engine*, which it runs for as long as it is up when *run_engine*.

    *acks* keeps the acknowledged incidents; in memory, for as long as the app runs, when not given.
    """
    service = EngineService(engine, fabric, rpc_timeout=rpc_timeout, acks=acks)
    query_list = lambda request, name: request.query_params.getlist(name)  # noqa: E731
    # The open event streams: told to end when the server stops, or it would wait on them for ever.
    streams: set[asyncio.Queue[dict[str, Any] | None]] = set()
    closing = False

    def close_streams() -> None:
        nonlocal closing
        closing = True
        for queue in streams:
            with contextlib.suppress(asyncio.QueueEmpty):
                if queue.full():
                    queue.get_nowait()
            queue.put_nowait(None)

    def store() -> Any:
        if engine.store is None:
            raise _Error(400, "no history is kept")
        return engine.store

    async def status(request: Request) -> Any:
        return await service.status()

    async def reports(request: Request) -> Any:
        return await service.reports()

    async def report_records(request: Request) -> Any:
        from .reports import REGISTRY

        name = request.path_params["name"]
        params = {k: v for k, v in request.query_params.items() if k not in _CONTROL}
        spec = REGISTRY.get(name)
        if spec is None:
            raise _Error(404, f"no report {name!r}")
        if request.query_params.get("progress") in ("1", "true") and not request.query_params.get("at"):
            return _events(_report_collect_stream(service, spec, request, engine))

        try:
            answer = await service.records(
                name,
                params,
                query_list(request, "inv"),
                query_list(request, "node"),
                request.query_params.get("at"),
            )
        except ServiceError as exc:
            raise _Error(404 if str(exc).startswith("no report") else 400, str(exc)) from exc
        return await _table(spec, answer, request, engine)

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

    async def acknowledgements(request: Request) -> Any:
        """GET: what is acknowledged. POST ``{incident, note}``: acknowledge it.
        DELETE ``?incident=``: no longer."""
        if request.method == "POST":
            body = await request.json()
            incident = str(body.get("incident") or "").strip()
            if not incident:
                raise _Error(400, 'say which incident: {"incident": <id>}')
            return await service.acknowledge(incident, str(body.get("note") or ""))
        if request.method == "DELETE":
            incident = request.query_params.get("incident", "").strip()
            if not incident:
                raise _Error(400, "say which incident: ?incident=<id>")
            return await service.unacknowledge(incident)
        return [asdict(a) for a in service.acks.all()]

    async def history(request: Request) -> Any:
        query = request.query_params
        args: dict[str, Any] = {
            "nodes": query_list(request, "node"),
            "reports": query_list(request, "report"),
            "limit": int(query.get("limit", "1000")),
            "after_seq": int(query.get("after", "0")),
            # Free text, every word of it found somewhere in the change: node, report, key or a value.
            "match": query.get("q", ""),
        }
        try:
            for key in ("since", "until"):
                if key in query:
                    args[key] = parse_time(query[key])
        except ValueError as exc:
            raise _Error(400, str(exc)) from exc
        if "baseline" in query:
            # Only what changed since a baseline: a named one, or the latest when none is named.
            if "since" in query:
                raise _Error(400, "say since=<time> or baseline=<name>, not both")
            name = query["baseline"]
            kept = await (store().run(Store.baseline, name) if name else store().run(Store.latest_baseline))
            if kept is None:
                raise _Error(404, f"no baseline {name!r}" if name else "no baseline is kept")
            args["since"] = kept["ts"]
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

    async def configs(request: Request) -> Any:
        return await service.configs(query_list(request, "node"))

    async def config_diff(request: Request) -> Any:
        query = request.query_params
        node = query.get("node", "")
        if not node:
            raise _Error(400, "a configuration diff needs node=<node>")
        try:
            commit = int(query["commit"]) if query.get("commit") else None
            base = int(query["base"]) if query.get("base") else None
            context = int(query.get("context", "3"))
            seen = float(query["seen"]) if query.get("seen") else None
        except ValueError as exc:
            raise _Error(400, f"commit, base, context and seen are numbers: {exc}") from exc
        return await service.config_diff(node, commit, base, context, seen)

    async def events(request: Request) -> Response:
        reports_wanted = set(query_list(request, "report"))
        nodes_wanted = set(query_list(request, "node"))
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(QUEUE_SIZE)

        def wanted(event: dict[str, Any]) -> bool:
            if event["type"] == "acks":
                return True
            if event["type"] == "node":
                return not nodes_wanted or event["name"] in nodes_wanted
            if event["type"] == "config":
                return not nodes_wanted or event["node"] in nodes_wanted
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

        if closing:
            raise _Error(503, "the server is stopping")
        stop = engine.listen(listener)
        streams.add(queue)
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
                streams.discard(queue)

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
        from .lenses import LENSES

        name = request.path_params["name"]
        args = {k: v for k, v in request.query_params.items() if k not in _CONTROL}
        try:
            answer = await service.lens(name, args, request.query_params.get("at"))
        except ServiceError as exc:
            raise _Error(404 if str(exc).startswith("no lens") else 400, str(exc)) from exc
        return await _table(LENSES[name], answer, request)

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
        Route("/api/acks", handled(acknowledgements), methods=["GET", "POST", "DELETE"]),
        Route("/api/history", handled(history)),
        Route("/api/drift", handled(drift)),
        Route("/api/configs", handled(configs)),
        Route("/api/configs/diff", handled(config_diff)),
        Route("/api/baselines", handled(baselines), methods=["GET", "POST"]),
        Route("/api/baselines/{name}", handled(baselines), methods=["DELETE"]),
        Route("/api/events", handled(events)),
        Route("/api/lenses", handled(lenses)),
        Route("/api/lenses/{name}", handled(lens)),
        Route("/api/topology", handled(topology)),
        Route("/api/chat", handled(chat), methods=["POST"]),
        Route("/", lambda request: FileResponse(UI / "index.html", headers={"cache-control": "no-cache"})),
        Mount("/ui", _Revalidated(directory=UI)),
    ]
    if mcp_manager is not None:
        from mcp.server.streamable_http_manager import StreamableHTTPASGIApp

        routes.append(Route("/mcp", StreamableHTTPASGIApp(mcp_manager)))
    middleware = [Middleware(_Auth, token=token)] if token else []
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.close_streams = close_streams
    return app


#: How long open requests - an agent's answer, an MCP call - may take to finish once stopping.
GRACE = 5
#: After a second signal: how long before the process exits, whatever is still open.
FORCE_AFTER = 3.0


def run(app: Starlette, host: str, port: int, ready: Callable[[], None] | None = None) -> None:
    """Serve *app* until SIGINT, SIGTERM or SIGHUP; a second one of those stops it at once.

    Uvicorn waits for every connection to close before it stops, and an event
    stream never closes by itself: so a signal first ends the streams.
    *ready* is called once the server listens.
    """
    import uvicorn

    stops = [s for s in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK") if hasattr(signal, s)]

    class Server(uvicorn.Server):
        loop: asyncio.AbstractEventLoop | None = None

        @contextlib.contextmanager
        def capture_signals(self) -> Any:
            # Uvicorn's own re-raises the signal once stopped, which ends the process before
            # the caller closes the history: these are restored and not raised again.
            if threading.current_thread() is not threading.main_thread():
                yield
                return
            previous = {s: signal.signal(getattr(signal, s), self.handle_exit) for s in stops}
            try:
                yield
            finally:
                for s, handler in previous.items():
                    signal.signal(getattr(signal, s), handler)

        async def serve(self, sockets: Any = None) -> None:
            self.loop = asyncio.get_running_loop()
            await super().serve(sockets)

        async def startup(self, sockets: Any = None) -> None:
            await super().startup(sockets)
            if self.started and ready is not None:
                ready()

        def handle_exit(self, sig: int, frame: Any) -> None:
            if self.should_exit:
                # Not uvicorn's force-exit, which skips closing the engine: the same stop,
                # with the process ended after a while if something still holds it.
                sys.stderr.write(f"stopping now: exiting in at most {FORCE_AFTER:g}s\n")
                force = threading.Timer(FORCE_AFTER, os._exit, (1,))
                force.daemon = True
                force.start()
            else:
                sys.stderr.write("stopping (again to force)\n")
                super().handle_exit(sig, frame)
            if self.loop is not None:
                self.loop.call_soon_threadsafe(app.state.close_streams)

    Server(
        uvicorn.Config(app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=GRACE)
    ).run()


def _mcp_manager(service: EngineService, host: str) -> Any:
    """The MCP endpoint's session manager, if the ``mcp`` extra is installed."""
    try:
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from mcp.server.transport_security import TransportSecuritySettings

        from .mcp import build_server
    except ImportError:
        logger.info("no MCP endpoint: install fcling[mcp]")
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

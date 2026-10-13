"""fcli's tools over MCP: the same tools the in-app agent has, for any MCP client.

``fcling serve`` mounts this at ``/mcp`` (streamable HTTP) over the engine it
runs, so a client reads the live state and the history the engine keeps::

    claude mcp add --transport http fcli http://127.0.0.1:8765/mcp

``fcling mcp`` serves it over stdio instead, for clients that start their
servers themselves: answering from a running server with ``--server``, or
from the nodes one-shot without one.
"""

from __future__ import annotations

from typing import Any

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from .service import Service
from .tools import TOOLS, ToolError, call

#: What a client is told about using these tools, before its first call.
INSTRUCTIONS = """\
Read-only views of an SR Linux fabric, from gNMI. Every tool is safe to call.
For "what is wrong" start with get_incidents: findings grouped by root cause, the root
to explain and the rest its consequences. For "what changed" use get_history (with the
configuration commit likely to have caused each change) or get_drift against a baseline.
For anything else, list_reports then get_report, narrowed with nodes, inv and where.
Pass at='30m' (or a time) to any of them to ask how the fabric was then."""


def build_server(service: Service, version: str = "") -> Server[Any]:
    async def list_tools(ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=t.name,
                    description=t.description,
                    input_schema=t.input_schema,
                    annotations=types.ToolAnnotations(
                        read_only_hint=True,
                        destructive_hint=False,
                        idempotent_hint=True,
                        open_world_hint=False,
                    ),
                )
                for t in TOOLS.values()
            ]
        )

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:
            answer = await call(service, params.name, params.arguments)
        except ToolError as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))], is_error=True
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text=answer)])

    return Server(
        "fcli",
        version=version,
        title="fcli",
        description="SR Linux fabric state, incidents and history",
        instructions=INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def serve_stdio(service: Service, version: str = "") -> None:
    server = build_server(service, version)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

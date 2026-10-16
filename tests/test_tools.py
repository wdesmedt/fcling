"""The tools, and MCP serving them, over a canned service."""

from __future__ import annotations

import json

import pytest
from mcp import Client

from fcli.mcp import build_server
from fcli.tools import TOOLS, ToolError, call

from .fake_service import FakeService


async def _call(name: str, args: dict | None = None, service: FakeService | None = None) -> object:
    return json.loads(await call(service or FakeService(), name, args))


async def test_report_rows_are_table_cells_filtered_and_limited() -> None:
    answer = await _call("get_report", {"name": "bgp-peers", "where": ["state=^(?!up)"]})
    assert answer["count"] == 1
    (row,) = answer["rows"]
    assert (row["node"], row["peer"], row["state"]) == ("leaf1", "10.0.0.2", "active")
    assert answer["errors"] == {"leaf3": "no answer"}
    limited = await _call("get_report", {"name": "bgp-peers", "limit": 2})
    assert (limited["count"], len(limited["rows"])) == (3, 2)
    assert "1 more rows" in limited["truncated"]


async def test_report_detail_is_whole_records_filtered_the_same_way() -> None:
    answer = await _call("get_report", {"name": "bgp-peers", "detail": True, "where": ["peer=0\\.3$"]})
    (record,) = answer["rows"]
    assert record["peer_as"] == 65003 and "families" in record


async def test_arguments_are_validated_before_anything_runs() -> None:
    service = FakeService()
    with pytest.raises(ToolError, match="limit"):
        await call(service, "get_report", {"name": "bgp-peers", "limit": 0})
    with pytest.raises(ToolError, match="colour: Extra inputs"):
        await call(service, "get_report", {"name": "bgp-peers", "colour": "red"})
    with pytest.raises(ToolError, match="no column 'nope'"):
        await call(service, "get_report", {"name": "bgp-peers", "where": ["nope=x"]})
    assert service.calls == [("records", ("bgp-peers", {}, [], [], None))]


async def test_service_errors_are_tool_errors() -> None:
    with pytest.raises(ToolError, match="neither a baseline"):
        await call(FakeService(), "get_drift", {"since": "yesterday-ish"})
    with pytest.raises(ToolError, match="no tool"):
        await call(FakeService(), "rm_rf", {})


async def test_incidents_and_history_are_compact() -> None:
    (incident,) = await _call("get_incidents")
    assert incident["related"] == 1 and incident["related_checks"] == ["bfd_down"]
    (change,) = await _call("get_history", {"since": "1h"})
    assert change["changed"] == {"state": ["established", "active"]}
    assert change["cause"] == "commit 7 by admin"


async def test_mcp_lists_and_calls_the_same_tools() -> None:
    async with Client(build_server(FakeService())) as client:
        listed = await client.list_tools()
        assert sorted(t.name for t in listed.tools) == sorted(TOOLS)
        assert all(t.annotations and t.annotations.read_only_hint for t in listed.tools)
        result = await client.call_tool("get_report", {"name": "bgp-peers", "nodes": ["leaf2"]})
        assert not result.is_error
        assert json.loads(result.content[0].text)["rows"][0]["peer"] == "10.0.0.3"
        bad = await client.call_tool("get_report", {"name": "nope"})
        assert bad.is_error and "no report" in bad.content[0].text


async def test_without_a_server_the_tools_answer_from_the_nodes(tmp_path: object) -> None:
    from fcli.gnmi.client import Tls
    from fcli.inventory import Inventory, Node
    from fcli.service import LocalService

    from .fake_gnmi import PASSWORD, USERNAME, FakeTarget
    from .test_engine import DATA

    with FakeTarget(DATA) as target:
        node = Node("leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True))
        service = LocalService(Inventory("lab", (node,)))
        answer = json.loads(
            await call(service, "get_report", {"name": "bgp-peers", "where": ["state=active"]})
        )
        assert [r["peer"] for r in answer["rows"]] == ["10.0.0.2"]
        assert answer["source"] == "collected"
        with pytest.raises(ToolError, match="no history here"):
            await call(service, "get_history", {})
        with pytest.raises(ToolError, match="no node nope"):
            await call(service, "get_report", {"name": "bgp-peers", "nodes": ["nope"]})


async def test_lens_tools_answer_in_table_rows() -> None:
    where = await _call("locate_address", {"address": "AA:00:00:00:00:01"})
    assert {(r["node"], r["kind"]) for r in where["rows"]} >= {("leaf1", "multihomed"), ("leaf3", "remote")}
    path = await _call("trace_path", {"source": "leaf1", "destination": "10.1.1.20", "ni": "ipvrf-1"})
    assert [r["outcome"] for r in path["rows"]][-1] == "delivered"
    first = path["rows"][0]
    assert (first["hop"], first["lookup"]) == (0, 1)  # the first lookup, not sorted by node
    service = await _call("service_detail", {"name": "macvrf-1"})
    assert service["count"] == 3
    hosts = await _call("list_endpoints", {"nodes": ["leaf3"], "where": ["IP=10\\.1"]})
    assert [r["IP"] for r in hosts["rows"]] == ["10.1.1.20"]
    with pytest.raises(ToolError, match="the services are"):
        await call(FakeService(), "service_detail", {"name": "nope"})


async def test_topology_tool_is_compact() -> None:
    graph = await _call("get_topology")
    assert {n["name"]: n["tier"] for n in graph["nodes"]}["spine1"] == "spine"
    assert all("cables" not in x and "down" not in x for x in graph["links"])
    (host_a,) = [c for c in graph["clients"] if c["name"] == "hostA"]
    assert host_a["ports"] == ["leaf1 lag1", "leaf2 lag1"]

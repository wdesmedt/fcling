"""The HTTP API over a running engine and an in-process target."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from fcli.acks import AckStore
from fcli.engine import Engine
from fcli.gnmi.client import Tls
from fcli.inventory import Inventory, Node
from fcli.remote import Remote
from fcli.reports import REGISTRY
from fcli.server import create_app
from fcli.store import AsyncStore

from .fake_gnmi import PASSWORD, USERNAME, FakeTarget
from .test_engine import DATA, PEER


@pytest.fixture
def target() -> Iterator[FakeTarget]:
    with FakeTarget(DATA) as t:
        yield t


def _client(
    target: FakeTarget,
    tmp_path: Path,
    token: str | None = None,
    checks: bool = False,
    agent_client: object = None,
    acks: AckStore | None = None,
) -> tuple[TestClient, AsyncStore]:
    node = Node(
        "leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True), labels={"role": "leaf"}
    )
    fabric = Inventory("lab", (node,))
    store = AsyncStore(tmp_path / "h.db")
    engine = Engine(fabric.nodes, [REGISTRY["bgp-peers"]], store, settle=0.01, checks=checks)
    return TestClient(create_app(engine, fabric, token, agent_client=agent_client, acks=acks)), store


@pytest.fixture
def client(target: FakeTarget, tmp_path: Path) -> Iterator[TestClient]:
    client, store = _client(target, tmp_path)
    with client:
        _until(lambda: client.get("/api/status").json()["nodes"][0]["status"] == "live")
        _until(lambda: len(client.get("/api/reports/bgp-peers").json()["records"]) == 2)
        yield client
    store.close()


def _until(condition: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


def test_live_records(client: TestClient) -> None:
    answer = client.get("/api/reports/bgp-peers", params={"inv": "role=leaf"}).json()
    assert answer["source"] == "live"
    assert {r["peer"]: r["state"] for r in answer["records"]} == {
        "10.0.0.1": "established",
        "10.0.0.2": "active",
    }
    nothing = client.get("/api/reports/bgp-peers", params={"inv": "role=spine"})
    assert nothing.status_code == 400
    assert nothing.json()["error"] == "no node matches role=spine; the labels there are: role=leaf"


def test_reports_describe_themselves(client: TestClient) -> None:
    reports = {r["name"]: r for r in client.get("/api/reports").json()}
    assert reports["bgp-peers"]["streamed"] and not reports["lldp"]["streamed"]
    assert reports["bgp-peers"]["key"] == ["ni", "peer"]
    assert "peer" in reports["bgp-peers"]["schema"]["properties"]
    assert reports["ipv4-rib"]["params"][0]["name"] == "address"


def test_history_and_state_at(client: TestClient, target: FakeTarget) -> None:
    before = time.time()
    target.push({PEER.format("10.0.0.2") + "/session-state": "established"})
    _until(lambda: client.get("/api/history", params={"since": before}).json())
    (change,) = client.get("/api/history", params={"since": before, "report": "bgp-peers"}).json()
    assert change["op"] == "change" and change["after"]["state"] == "established"
    then = client.get("/api/reports/bgp-peers", params={"at": before}).json()
    assert then["source"] == "history"
    assert {r["peer"]: r["state"] for r in then["records"]}["10.0.0.2"] == "active"


def test_history_since_a_baseline(client: TestClient, target: FakeTarget) -> None:
    assert client.get("/api/history", params={"baseline": ""}).status_code == 404
    assert client.post("/api/baselines", json={"name": "old", "at": "1h"}).status_code == 200
    assert client.post("/api/baselines", json={"name": "now"}).status_code == 200
    target.push({PEER.format("10.0.0.2") + "/session-state": "established"})
    _until(lambda: client.get("/api/history", params={"baseline": "now"}).json())
    # Not the first sync from before it: only the change since. No name: the latest baseline.
    for name in ("now", ""):
        (change,) = client.get("/api/history", params={"baseline": name, "report": "bgp-peers"}).json()
        assert change["op"] == "change" and change["after"]["state"] == "established"
    assert len(client.get("/api/history", params={"baseline": "old"}).json()) > 1
    missing = client.get("/api/history", params={"baseline": "never"})
    assert missing.status_code == 404 and "no baseline 'never'" in missing.json()["error"]
    assert client.get("/api/history", params={"baseline": "now", "since": "1h"}).status_code == 400


def test_history_searched(client: TestClient, target: FakeTarget) -> None:
    assert client.post("/api/baselines", json={"name": "now"}).status_code == 200
    target.push({PEER.format("10.0.0.2") + "/session-state": "established"})
    _until(lambda: client.get("/api/history", params={"baseline": "now"}).json())
    everything = client.get("/api/history", params={"since": "1h"}).json()
    assert {c["after"]["peer"] for c in everything if c["after"]} == {"10.0.0.1", "10.0.0.2"}
    # A value anywhere in the record, case aside; every word of it.
    for q in ("10.0.0.2", "LEAF1 10.0.0.2"):
        found = client.get("/api/history", params={"since": "1h", "q": q}).json()
        assert found and all("10.0.0.2" in c["key"] for c in found)
    assert client.get("/api/history", params={"since": "1h", "q": "leaf9"}).json() == []
    # With the other filters: only the change since the baseline, of that report.
    (change,) = client.get(
        "/api/history", params={"baseline": "now", "report": "bgp-peers", "q": "ESTAB"}
    ).json()
    assert change["op"] == "change" and change["after"]["state"] == "established"
    assert client.get("/api/history", params={"baseline": "now", "q": "10.0.0.1"}).json() == []


def test_unknown_report_and_bad_time(client: TestClient) -> None:
    assert client.get("/api/reports/nope").status_code == 404
    assert client.get("/api/history", params={"since": "yesterday-ish"}).status_code == 400
    # Before the history began there is no answer - not an empty one.
    for url in ("/api/reports/bgp-peers", "/api/lenses/where?address=10.0.0.1", "/api/checks"):
        early = client.get(url, params={"at": "1d"})
        assert early.status_code == 400 and "is before the history, which starts at" in early.json()["error"]


@pytest.fixture
def served(target: FakeTarget, tmp_path: Path) -> Iterator[str]:
    """The app on a real uvicorn: a client going away is what ends an event stream."""
    import socket
    import threading

    import uvicorn

    from .test_agent import _Client, _message, _text, _use

    scripted = _Client(
        _message(_use("t1", "get_report", {"name": "bgp-peers", "where": ["state=active"]}), stop="tool_use"),
        _message(_text("10.0.0.2 is down.")),
        _message(_text("It is still down.")),
    )
    client, store = _client(target, tmp_path, agent_client=scripted)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(client.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}"
    remote = Remote(url)
    _until(lambda: server.started and len(remote.get("/api/reports/bgp-peers")["records"]) == 2)
    yield url
    server.should_exit = True
    thread.join(10)
    store.close()


def test_events_stream_changes(served: str, target: FakeTarget) -> None:
    events = []
    for event in Remote(served).events([("report", "bgp-peers")]):
        events.append(event)
        if event["type"] == "status":
            _until(lambda: target.streaming)
            target.push(deletes=[PEER.format("10.0.0.1")])
        if event["type"] == "change":
            break
    assert events[0]["type"] == "status"
    assert (events[-1]["op"], events[-1]["key"]) == ("remove", '["default","10.0.0.1"]')
    # Missed while away: a reconnect from an earlier sequence number catches up from the history.
    seq = events[-1]["seq"]
    replay = Remote(served).events([("report", "bgp-peers"), ("after", str(seq - 1))])
    assert next(replay)["type"] == "status"
    assert next(replay)["seq"] == seq


def test_token(target: FakeTarget, tmp_path: Path) -> None:
    client, store = _client(target, tmp_path, token="s3cret")
    with client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert client.get("/api/status", params={"token": "s3cret"}).status_code == 200
    store.close()


def test_the_token_in_the_ui_link_lets_other_tabs_in(target: FakeTarget, tmp_path: Path) -> None:
    client, store = _client(target, tmp_path, token="s3cret")
    with client:
        assert "set-cookie" not in client.get("/", params={"token": "wrong"}).headers
        assert client.get("/api/status").status_code == 401
        cookie = client.get("/", params={"token": "s3cret"}).headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        # A new tab: no token in its link or its script, only the cookie.
        assert client.get("/api/status").status_code == 200
        assert "set-cookie" not in client.get("/", params={"token": "s3cret"}).headers
        client.cookies.set("fcli-token", "wrong")
        assert client.get("/api/status").status_code == 401
    store.close()


def test_checks_incidents_baselines_and_drift(target: FakeTarget, tmp_path: Path) -> None:
    client, store = _client(target, tmp_path, checks=True)
    with client:
        _until(lambda: client.get("/api/checks").json())
        (finding,) = client.get("/api/checks").json()
        assert (finding["check"], finding["subject"]) == ("bgp_down", "default/10.0.0.2")
        assert finding["raised"] is not None
        (incident,) = client.get("/api/incidents").json()
        assert incident["root"]["check"] == "bgp_down" and incident["first_seen"] == finding["raised"]

        assert (
            client.post("/api/baselines", json={"name": "before", "comment": "pre-change"}).status_code == 200
        )
        target.push({PEER.format("10.0.0.2") + "/session-state": "established"})
        _until(lambda: client.get("/api/checks").json() == [])

        answer = client.get("/api/drift", params={"from": "before", "report": "bgp-peers"}).json()
        (change,) = answer["changes"]
        assert (change["op"], change["before"]["state"], change["after"]["state"]) == (
            "change",
            "active",
            "established",
        )
        assert client.get("/api/drift", params={"from": "never"}).status_code == 400
        assert [b["name"] for b in client.get("/api/baselines").json()] == ["before"]
        assert client.delete("/api/baselines/before").json() == []

        history = client.get("/api/history", params={"report": "checks"}).json()
        assert [(h["op"], h["cause"]) for h in history] == [("add", ""), ("remove", "")]
    store.close()


async def test_mcp_over_http_sees_the_engine(served: str) -> None:
    from mcp import Client

    async with Client(f"{served}/mcp") as client:
        tools = await client.list_tools()
        assert "get_incidents" in {t.name for t in tools.tools}
        result = await client.call_tool("get_report", {"name": "bgp-peers", "where": ["state=active"]})
        answer = json.loads(result.content[0].text)
    assert answer["source"] == "live"
    assert [r["peer"] for r in answer["rows"]] == ["10.0.0.2"]


def test_chat_streams_the_agent_and_keeps_the_conversation(served: str) -> None:
    remote = Remote(served)
    events = list(remote.chat("which peer is down?"))
    kinds = [e["type"] for e in events]
    assert kinds == ["start", "tool", "tool_result", "text", "done"]
    conversation = events[0]["conversation"]
    assert events[-1]["conversation"] == conversation
    follow_up = list(remote.chat("still?", conversation))
    assert follow_up[0]["conversation"] == conversation
    assert follow_up[-2] == {"type": "text", "text": "It is still down."}


def test_ask_through_the_server(served: str) -> None:
    from click.testing import CliRunner

    from fcli.cli import main

    result = CliRunner().invoke(main, ["--server", served, "ask", "which peer is down?"])
    assert result.exit_code == 0, result.output
    assert "10.0.0.2 is down." in result.stdout
    assert "-> get_report" in result.stderr


def test_lenses_and_topology(client: TestClient) -> None:
    lenses = {x["name"]: x for x in client.get("/api/lenses").json()}
    assert [a["name"] for a in lenses["path"]["args"]] == ["source", "destination", "ni"]
    (none,) = client.get("/api/lenses/where", params={"address": "10.9.9.9"}).json()["records"]
    assert none["kind"] == "not-found"
    assert client.get("/api/lenses/nope").status_code == 404
    missing = client.get("/api/lenses/where")
    assert missing.status_code == 400 and "needs address" in missing.json()["error"]
    graph = client.get("/api/topology").json()
    assert [n["name"] for n in graph["nodes"]] == ["leaf1"]


def test_the_ui_is_served_and_needs_no_token(target: FakeTarget, tmp_path: Path) -> None:
    client, store = _client(target, tmp_path, token="s3cret")
    with client:
        page = client.get("/")
        assert page.status_code == 200 and '<script type="module" src="ui/app.js">' in page.text
        from fcli.server import UI

        assets = [p.relative_to(UI).as_posix() for p in UI.rglob("*") if p.is_file()]
        assert "vendor/preact-htm.module.js" in assets
        for asset in assets:
            served = client.get(f"/ui/{asset}")
            assert served.status_code == 200, asset
            assert served.headers["cache-control"] == "no-cache", asset
        etag = client.get("/ui/app.js").headers["etag"]
        assert client.get("/ui/app.js", headers={"if-none-match": etag}).status_code == 304
        assert client.get("/ui/nope.js").status_code == 404
        # The data still needs the token.
        assert client.get("/api/topology").status_code == 401
        assert client.get("/api/topology", params={"token": "s3cret"}).status_code == 200
    store.close()


def test_tables_as_the_cli_renders_them(client: TestClient) -> None:
    answer = client.get("/api/reports/bgp-peers", params={"table": "1"}).json()
    assert answer["columns"][0] == "Node"
    assert len(answer["rows"]) == len(answer["records"]) == 2
    assert all(len(row) == len(answer["columns"]) and row[0] == "leaf1" for row in answer["rows"])
    # Not an argument of the report or the lens.
    lens = client.get("/api/lenses/where", params={"address": "10.9.9.9", "table": "1"}).json()
    assert lens["rows"][0][:2] == ["-", "not-found"]


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGHUP"), reason="POSIX signals")
@pytest.mark.parametrize("name", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_a_signal_stops_the_server_though_event_streams_are_open(name: str) -> None:
    """Uvicorn waits for every connection to close; an event stream never would by itself."""
    import signal
    import socket
    import subprocess
    import sys

    child = subprocess.Popen(
        [sys.executable, "-m", "tests.serve_child"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).parent.parent,
    )
    streams = []
    try:
        port = int(child.stdout.readline())
        remote = Remote(f"http://127.0.0.1:{port}")
        _until(lambda: _answers(remote), timeout=15)
        for _ in range(2):
            s = socket.create_connection(("127.0.0.1", port))
            s.sendall(f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
            received = b""
            while b"event: status" not in received:
                chunk = s.recv(65536)
                assert chunk, received
                received += chunk
            streams.append(s)
        started = time.monotonic()
        child.send_signal(getattr(signal, name))
        out, err = child.communicate(timeout=10)
        assert child.returncode == 0, err
        # At once: not after the grace that stops whatever else is still open.
        from fcli.server import GRACE

        assert time.monotonic() - started < GRACE / 2
        assert "closed" in out  # the history was closed, not cut off
        assert "stopping" in err
    finally:
        for s in streams:
            s.close()
        if child.poll() is None:
            child.kill()
            child.wait()


def _answers(remote: Remote) -> bool:
    try:
        return len(remote.get("/api/reports/bgp-peers")["records"]) == 2
    except Exception:
        return False


def test_acknowledged_incidents_leave_the_counts_and_end_with_their_finding(
    target: FakeTarget, tmp_path: Path
) -> None:
    kept = tmp_path / "h.acks.json"
    client, store = _client(target, tmp_path, checks=True, acks=AckStore(kept))
    with client:
        _until(lambda: client.get("/api/incidents").json())
        (incident,) = client.get("/api/incidents").json()
        assert incident["acknowledged"] is False and incident["ack"] is None
        assert (
            client.get("/api/status").json()["incidents"]["warnings"]
            + client.get("/api/status").json()["incidents"]["errors"]
            == 1
        )

        acked = client.post("/api/acks", json={"incident": incident["id"], "note": "ticket 42"}).json()
        assert acked["acknowledged"] is True and acked["ack"]["note"] == "ticket 42"
        counts = client.get("/api/status").json()["incidents"]
        assert (counts["errors"], counts["warnings"], counts["acknowledged"]) == (0, 0, 1)
        (ack,) = client.get("/api/acks").json()
        assert (ack["check"], ack["subject"], ack["incident_id"]) == (
            "bgp_down",
            "default/10.0.0.2",
            incident["id"],
        )
        # Kept on disk: a server started again over the same file has it.
        assert [a.subject for a in AckStore(kept).all()] == ["default/10.0.0.2"]

        assert client.delete("/api/acks", params={"incident": incident["id"]}).json()["acknowledged"] is False
        assert client.get("/api/acks").json() == []
        assert client.post("/api/acks", json={"incident": "nope"}).status_code == 400
        assert client.post("/api/acks", json={}).status_code == 400

        # Acknowledged again, then the session comes up: the acknowledgement ends with the finding.
        client.post("/api/acks", json={"incident": incident["id"]})
        target.push({PEER.format("10.0.0.2") + "/session-state": "established"})
        _until(lambda: client.get("/api/acks").json() == [])
        assert AckStore(kept).all() == []
    store.close()


def test_an_acknowledgement_whose_finding_cleared_while_stopped_ends_on_restart(
    target: FakeTarget, tmp_path: Path
) -> None:
    kept = tmp_path / "h.acks.json"
    client, store = _client(target, tmp_path, checks=True, acks=AckStore(kept))
    with client:
        _until(lambda: client.get("/api/incidents").json())
        (incident,) = client.get("/api/incidents").json()
        client.post("/api/acks", json={"incident": incident["id"]})
    store.close()
    assert len(AckStore(kept).all()) == 1

    # Fixed while nobody watched: the node now says so, the stored findings still have it.
    fixed = json.loads(json.dumps(DATA).replace('"active"', '"established"'))
    with FakeTarget(fixed) as again:
        client, store = _client(again, tmp_path, checks=True, acks=AckStore(kept))
        with client:
            _until(lambda: client.get("/api/acks").json() == [])
            assert client.get("/api/incidents").json() == []
        store.close()
    assert AckStore(kept).all() == []


def test_configuration_diffs(target: FakeTarget, tmp_path: Path) -> None:
    from fcli import config
    from fcli.delta import Change, Op

    client, store = _client(target, tmp_path)
    before = {"interface": [{"name": "ethernet-1/1", "admin-state": "enable"}]}
    after = {"interface": [{"name": "ethernet-1/1", "admin-state": "disable"}]}
    record = {"node": "leaf1", "id": 8, "username": "ops", "comment": "shut e1/1"}
    store.store.write([Change(199.0, "leaf1", "config-commits", "[8]", Op.ADD, None, record)])
    store.store.keep_config("leaf1", 100.0, 7, before, config.digest(before), 1)
    store.store.keep_config("leaf1", 200.0, 8, after, config.digest(after), 1)
    with client:
        assert [c["commit_id"] for c in client.get("/api/configs", params={"node": "leaf1"}).json()] == [8, 7]
        diff = client.get("/api/configs/diff", params={"node": "leaf1", "commit": "8"}).json()
        assert diff["commits"] == [record]
        (row,) = diff["rows"]
        assert (row["kind"], row["left"]["text"], row["right"]["text"]) == (
            "changed",
            "set / interface ethernet-1/1 admin-state enable",
            "set / interface ethernet-1/1 admin-state disable",
        )
        assert client.get("/api/configs/diff", params={"node": "leaf1", "commit": "9"}).status_code == 400
        assert client.get("/api/configs/diff").status_code == 400
        (commit,) = client.get("/api/history", params={"report": "config-commits"}).json()
        assert (commit["origin"], commit["commit"]) == ("config", {"node": "leaf1", "id": 8})
    store.close()


def test_history_since_a_baseline_through_the_server(served: str, target: FakeTarget) -> None:
    from click.testing import CliRunner

    from fcli.cli import main

    runner = CliRunner()
    none = runner.invoke(main, ["--server", served, "history", "--since-baseline"])
    assert none.exit_code == 2 and "no baseline is kept" in none.output
    assert runner.invoke(main, ["--server", served, "baseline", "b1"]).exit_code == 0
    target.push({PEER.format("10.0.0.2") + "/session-state": "established"})
    _until(lambda: Remote(served).history(baseline="b1"))
    for args in (["--since-baseline"], ["--since-baseline", "b1", "-r", "bgp-peers"]):
        result = runner.invoke(main, ["--server", served, "-o", "json", "history", *args])
        assert result.exit_code == 0, result.output
        changes = json.loads(result.stdout)
        assert [(c["op"], c["after"]["state"]) for c in changes] == [("change", "established")]
    unknown = runner.invoke(main, ["--server", served, "history", "--since-baseline", "nope"])
    assert unknown.exit_code == 2 and "no baseline 'nope'" in unknown.output

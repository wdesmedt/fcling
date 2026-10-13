"""The engine against an in-process target: sync, live changes, history."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from fcli.engine import Engine, Status
from fcli.gnmi.client import Tls
from fcli.inventory import Node
from fcli.report import NodeState, Read, Record, Report, default_columns
from fcli.reports import REGISTRY
from fcli.store import AsyncStore

from .fake_gnmi import PASSWORD, USERNAME, FakeTarget

NEIGHBORS = "/network-instance[name=*]/protocols/bgp/neighbor"
DATA = {
    NEIGHBORS: {
        "srl_nokia-network-instance:network-instance": [
            {
                "name": "default",
                "protocols": {
                    "srl_nokia-bgp:bgp": {
                        "neighbor": [
                            {"peer-address": "10.0.0.1", "session-state": "established", "peer-as": 65001},
                            {"peer-address": "10.0.0.2", "session-state": "active", "peer-as": 65002},
                        ]
                    }
                },
            }
        ]
    },
}
PEER = "/network-instance[name=default]/protocols/bgp/neighbor[peer-address={}]"


class Missing(Record):
    thing: str


def _nothing(node: NodeState) -> Iterator[Missing]:
    yield from ()


#: Built, not registered: it reads what the target lacks, and is no command.
MISSING = Report(
    name="test-missing",
    help="reads what the target lacks",
    reads=(Read("/platform/fabric[slot=*]"),),
    record=Missing,
    build=_nothing,
    columns=default_columns(Missing),
)


@pytest.fixture
def target() -> Iterator[FakeTarget]:
    with FakeTarget(DATA, unknown=frozenset({"fabric"})) as t:
        yield t


@pytest.fixture
async def engine(target: FakeTarget, tmp_path: Path) -> AsyncIterator[tuple[Engine, list[dict[str, Any]]]]:
    node = Node("leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True))
    store = AsyncStore(tmp_path / "h.db")
    eng = Engine([node], [REGISTRY["bgp-peers"], MISSING], store, settle=0.01, checks=False)
    events: list[dict[str, Any]] = []
    eng.listen(events.append)
    task = asyncio.create_task(eng.run())
    yield eng, events
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.close()


async def _until(condition: Any, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


def _changes(events: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(e["op"], e["report"], e["key"]) for e in events if e["type"] == "change"]


async def test_syncs_without_what_the_node_lacks(
    engine: tuple[Engine, list[dict[str, Any]]], target: FakeTarget
) -> None:
    eng, events = engine
    await _until(lambda: len(_changes(events)) == 2)
    stream = eng.streams["leaf1"]
    assert stream.status is Status.LIVE
    assert [r.path for r in stream.excluded] == ["/platform/fabric[slot=*]"]
    # Refused once with the path, then subscribed without it.
    assert any("platform/fabric" in p for p in target.servicer.subscribes[0])
    assert not any("platform/fabric" in p for p in target.servicer.subscribes[-1])
    assert sorted(_changes(events)) == [
        ("add", "bgp-peers", '["default","10.0.0.1"]'),
        ("add", "bgp-peers", '["default","10.0.0.2"]'),
    ]


async def test_live_changes_are_kept(engine: tuple[Engine, list[dict[str, Any]]], target: FakeTarget) -> None:
    eng, events = engine
    await _until(lambda: len(_changes(events)) == 2)
    before = time.time()
    events.clear()

    target.push({PEER.format("10.0.0.2") + "/session-state": "established"})
    await _until(lambda: _changes(events))
    assert _changes(events) == [("change", "bgp-peers", '["default","10.0.0.2"]')]
    change = next(e for e in events if e["type"] == "change")
    assert (change["before"]["state"], change["after"]["state"]) == ("active", "established")
    assert change["seq"] > 0

    events.clear()
    target.push(deletes=[PEER.format("10.0.0.1")])
    await _until(lambda: _changes(events))
    assert _changes(events) == [("remove", "bgp-peers", '["default","10.0.0.1"]')]

    assert eng.store is not None
    history = await eng.store.history(since=before)
    assert [h["op"] for h in history] == ["change", "remove"]
    then = await eng.store.state_at(before, "bgp-peers")
    assert {r["peer"]: r["state"] for r in then["leaf1"].values()} == {
        "10.0.0.1": "established",
        "10.0.0.2": "active",
    }


async def test_counters_alone_are_not_history(
    engine: tuple[Engine, list[dict[str, Any]]], target: FakeTarget
) -> None:
    eng, events = engine
    await _until(lambda: len(_changes(events)) == 2)
    events.clear()
    target.push(
        {
            PEER.format("10.0.0.1") + "/afi-safi[afi-safi-name=evpn]": {
                "admin-state": "enable",
                "received-routes": "5",
            }
        }
    )
    await _until(lambda: _changes(events))
    # A family appearing is a change; its count moving afterwards is not.
    assert _changes(events)[0][0] == "change"
    events.clear()
    target.push({PEER.format("10.0.0.1") + "/afi-safi[afi-safi-name=evpn]/received-routes": "6"})
    await _until(lambda: _changes(events))
    assert _changes(events) == [("update", "bgp-peers", '["default","10.0.0.1"]')]
    assert next(e for e in events if e["type"] == "change")["seq"] == 0


async def test_findings_are_raised_and_cleared(target: FakeTarget, tmp_path: Path) -> None:
    node = Node("leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True))
    store = AsyncStore(tmp_path / "h.db")
    eng = Engine([node], [REGISTRY["bgp-peers"]], store, settle=0.01)
    events: list[dict[str, Any]] = []
    eng.listen(events.append)
    task = asyncio.create_task(eng.run())
    try:

        def findings() -> list[tuple[str, str]]:
            return sorted((f.check, f.subject) for f in eng.current("checks"))

        await _until(lambda: findings() == [("bgp_down", "default/10.0.0.2")])
        (raised,) = eng.raised.values()
        assert raised <= time.time()
        target.push({PEER.format("10.0.0.2") + "/session-state": "established"})
        await _until(lambda: findings() == [])
        assert ("remove", "checks", '["bgp_down","default/10.0.0.2"]') in _changes(events)
        assert eng.raised == {}
        assert eng.store is not None
        kept = await eng.store.history(reports=["checks"])
        assert [c["op"] for c in kept] == ["add", "remove"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store.close()


async def test_a_lost_node_stays_lost_while_it_is_tried_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each reconnect must not clear node_unreachable, only to raise it again when it fails."""
    import socket

    import fcli.engine

    monkeypatch.setattr(fcli.engine, "BACKOFF", (0.05, 0.2))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here once closed
    node = Node("spine1", "127.0.0.1", port, USERNAME, PASSWORD, Tls(insecure=True))
    store = AsyncStore(tmp_path / "h.db")
    eng = Engine([node], [REGISTRY["bgp-peers"]], store, settle=0.01)
    events: list[dict[str, Any]] = []
    eng.listen(events.append)
    task = asyncio.create_task(eng.run())
    try:
        await _until(lambda: [f.check for f in eng.current("checks")] == ["node_unreachable"])
        await asyncio.sleep(1.5)  # many attempts at 0.05-0.2s apart
        states = [e["status"] for e in events if e["type"] == "node"]
        assert states and states[-1] == "down"
        assert "connecting" not in states[states.index("down") :]
        assert [op for op, report, _ in _changes(events) if report == "checks"] == ["add"]
        assert eng.fabric().unreachable == {"spine1": eng.streams["spine1"].lost}
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store.close()

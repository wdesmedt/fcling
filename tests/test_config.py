"""Configurations: kept after each commit, secrets redacted, compared side by side."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from fcli import config, engine, views
from fcli.delta import Change, Op
from fcli.engine import Engine
from fcli.gnmi.client import Tls
from fcli.inventory import Node
from fcli.reports import REGISTRY
from fcli.store import AsyncStore, Store

from .fake_gnmi import PASSWORD, USERNAME, FakeTarget

#: A running configuration as a Get of ``/`` answers it: module prefixes, a secret, a banner.
CONFIG: dict[str, Any] = {
    "srl_nokia-interfaces:interface": [
        {"name": "ethernet-1/2", "admin-state": "enable"},
        {"name": "ethernet-1/10", "admin-state": "enable", "description": "to spine1"},
        {
            "name": "ethernet-1/1",
            "admin-state": "enable",
            "subinterface": [{"index": 0, "ipv4": {"admin-state": "enable"}}],
        },
    ],
    "srl_nokia-system:system": {
        "aaa": {"authentication": {"linuxadmin-user": {"password": "$y$j9T$secret"}}},
        "banner": {"login-banner": "Welcome\nto the lab"},
        "srl_nokia-lldp:lldp": {"admin-state": "srl_nokia-common:enable"},
    },
}


def _tree(raw: dict[str, Any], salt: str = "s") -> dict[str, Any]:
    return config.normalize([("/", raw)], salt)


def _kinds(diff: config.ConfigDiff) -> list[tuple[str, str | None, str | None]]:
    return [
        (r["kind"], (r.get("left") or {}).get("text"), (r.get("right") or {}).get("text"))
        for r in diff.rows
        if r["kind"] not in ("same", "skip")
    ]


def test_lines_read_as_info_flat() -> None:
    lines = config.flatten(_tree(CONFIG))
    assert lines[:3] == [
        "set / interface ethernet-1/1 admin-state enable",
        "set / interface ethernet-1/1 subinterface 0 ipv4 admin-state enable",
        "set / interface ethernet-1/2 admin-state enable",
    ]
    # Natural order, quoted where it must be, prefixes gone from names and values.
    assert 'set / interface ethernet-1/10 description "to spine1"' in lines
    assert lines.index("set / interface ethernet-1/2 admin-state enable") < lines.index(
        "set / interface ethernet-1/10 admin-state enable"
    )
    assert "set / system lldp admin-state enable" in lines
    # A banner is one leaf, so one line.
    assert 'set / system banner login-banner "Welcome\\nto the lab"' in lines


def test_secrets_are_digests() -> None:
    lines = config.flatten(_tree(CONFIG))
    password = next(line for line in lines if "password" in line)
    assert "secret" not in password and "<redacted " in password
    # The same secret reads the same in one history, and differently in another.
    assert config.flatten(_tree(CONFIG)) == lines
    assert config.flatten(_tree(CONFIG, salt="other")) != lines
    changed = copy.deepcopy(CONFIG)
    changed["srl_nokia-system:system"]["aaa"]["authentication"]["linuxadmin-user"]["password"] = "new"
    assert _kinds(config.compare(_tree(CONFIG), _tree(changed)))[0][0] == "changed"


def test_a_changed_leaf_is_one_row_side_by_side() -> None:
    after = copy.deepcopy(CONFIG)
    after["srl_nokia-interfaces:interface"][1]["description"] = "to spine1, port 3"
    after["srl_nokia-interfaces:interface"][0]["admin-state"] = "disable"
    after["srl_nokia-interfaces:interface"][2].pop("subinterface")
    after["srl_nokia-interfaces:interface"].append({"name": "ethernet-1/20", "admin-state": "enable"})
    after["srl_nokia-system:system"]["banner"]["login-banner"] = "Hello"
    diff = config.compare(_tree(CONFIG), _tree(after))
    assert _kinds(diff) == [
        ("removed", "set / interface ethernet-1/1 subinterface 0 ipv4 admin-state enable", None),
        (
            "changed",
            "set / interface ethernet-1/2 admin-state enable",
            "set / interface ethernet-1/2 admin-state disable",
        ),
        (
            "changed",
            'set / interface ethernet-1/10 description "to spine1"',
            'set / interface ethernet-1/10 description "to spine1, port 3"',
        ),
        ("added", None, "set / interface ethernet-1/20 admin-state enable"),
        (
            "changed",
            'set / system banner login-banner "Welcome\\nto the lab"',
            "set / system banner login-banner Hello",
        ),
    ]
    assert diff.summary == "+1 -1 ~3"
    numbers = [r["left"]["n"] for r in diff.rows if r.get("left")]
    assert numbers == sorted(numbers)


def test_unchanged_lines_far_from_a_change_are_left_out() -> None:
    before = [(f"set / a {i} x", f"set / a {i}") for i in range(20)]
    after = [*before[:10], ("set / a 10 y", "set / a 10"), *before[11:]]
    diff = config.side_by_side(before, after, context=2)
    assert [r["kind"] for r in diff.rows] == ["skip", "same", "same", "changed", "same", "same", "skip"]
    assert (diff.rows[0]["count"], diff.rows[-1]["count"]) == (8, 7)
    assert config.side_by_side(before, before).summary == "no change"


def test_kept_only_when_it_changed(tmp_path: Path) -> None:
    store = Store(tmp_path / "h.db")
    tree = _tree(CONFIG)
    assert store.keep_config("leaf1", 100.0, 4, tree, config.digest(tree), 9)
    assert not store.keep_config("leaf1", 110.0, 5, tree, config.digest(tree), 9)
    other = {**tree, "extra": {"x": 1}}
    assert store.keep_config("leaf1", 120.0, 7, other, config.digest(other), 10)
    assert [c["commit_id"] for c in store.configs()] == [7, 4]
    assert store.config("leaf1", commit_id=5)["commit_id"] == 7  # the first kept from commit 5 on
    assert store.config("leaf1", before=120.0)["commit_id"] == 4
    assert store.config("leaf1")["tree"] == other
    assert store.salt() == store.salt()


def _commit(store: Store, ts: float, commit_id: int, user: str = "admin", comment: str = "") -> None:
    record = {"node": "leaf1", "id": commit_id, "status": "complete", "username": user, "comment": comment}
    store.write([Change(ts, "leaf1", "config-commits", f"[{commit_id}]", Op.ADD, None, record)])


def test_a_commit_diff_names_every_commit_it_spans(tmp_path: Path) -> None:
    store = Store(tmp_path / "h.db")
    before, after = _tree(CONFIG), copy.deepcopy(_tree(CONFIG))
    after["interface"][0]["admin-state"] = "disable"
    _commit(store, 99.0, 4)
    store.keep_config("leaf1", 100.0, 4, before, config.digest(before), 9)
    _commit(store, 199.0, 5, comment="first")
    _commit(store, 199.5, 6, user="ops", comment="second")
    store.keep_config("leaf1", 200.0, 6, after, config.digest(after), 9)

    answer = views.config_diff(store, "leaf1", 5)
    assert [c["id"] for c in answer["commits"]] == [5, 6]
    assert (answer["before"]["commit_id"], answer["after"]["commit_id"]) == (4, 6)
    assert answer["changed"] == 1 and answer["summary"] == "+0 -0 ~1"
    assert views.config_diff(store, "leaf1")["after"]["commit_id"] == 6

    first = views.config_diff(store, "leaf1", 4)
    assert first["before"] is None and first["rows"] == []
    with pytest.raises(LookupError):
        views.config_diff(store, "leaf1", 7)


def test_a_redeployed_node_numbers_its_commits_again(tmp_path: Path) -> None:
    """Commit 3 after a redeploy is not commit 3 from before it, nor a diff of every id since."""
    store = Store(tmp_path / "h.db")
    trees = []
    for state in ("enable", "disable", "enable", "disable"):
        tree = copy.deepcopy(_tree(CONFIG))
        tree["interface"][0]["admin-state"] = state
        tree["extra"] = {"n": len(trees)}
        trees.append(tree)
    # The first life: commits 2 and 3 (and 24, its last).
    store.keep_config("leaf1", 100.0, 2, trees[0], config.digest(trees[0]), 9)
    _commit(store, 199.0, 3, user="old")
    store.keep_config("leaf1", 200.0, 24, trees[1], config.digest(trees[1]), 9)
    # Redeployed: commits 1 and 2 come again as changes, then admin's commit 3.
    store.keep_config("leaf1", 300.0, 2, trees[2], config.digest(trees[2]), 9)
    _commit(store, 399.0, 3, user="admin")
    store.keep_config("leaf1", 400.0, 3, trees[3], config.digest(trees[3]), 9)

    latest = views.config_diff(store, "leaf1", 3)
    assert (latest["before"]["ts"], latest["after"]["ts"]) == (300.0, 400.0)
    assert [(c["id"], c["username"]) for c in latest["commits"]] == [(3, "admin")]
    old = views.config_diff(store, "leaf1", 3, seen=199.0)
    assert (old["before"]["ts"], old["after"]["ts"]) == (100.0, 200.0)
    assert [(c["id"], c["username"]) for c in old["commits"]] == [(3, "old")]
    # The redeploy itself, against the last configuration of the first life.
    again = views.config_diff(store, "leaf1", 2, seen=250.0)
    assert (again["before"]["ts"], again["after"]["ts"]) == (200.0, 300.0)
    assert store.config("leaf1", commit_id=25) is None


def test_changes_after_a_commit_are_config_the_rest_unplanned() -> None:
    commit = {
        "ts": 100.0,
        "node": "leaf1",
        "report": "config-commits",
        "op": "add",
        "after": {"id": 9, "username": "a"},
    }
    changes = [
        commit,
        {"ts": 130.0, "node": "leaf1", "report": "bgp-peers", "op": "change"},
        {"ts": 140.0, "node": "leaf1", "report": "checks", "op": "add"},
        {"ts": 130.0, "node": "leaf2", "report": "bgp-peers", "op": "change"},
        {"ts": 500.0, "node": "leaf1", "report": "lldp", "op": "remove"},
    ]
    out = views.attribute(changes, [commit])
    assert [c["origin"] for c in out] == ["config", "config", "config", "unplanned", "unplanned"]
    assert out[0]["commit"] == {"node": "leaf1", "id": 9, "seen": 100.0} and not out[0]["cause"]
    assert (
        out[1]["commit"] == {"node": "leaf1", "id": 9, "seen": 100.0} and out[1]["cause"] == "commit 9 by a"
    )
    assert out[3]["commit"] is None


# --------------------------------------------------------------------------- #
# the engine keeps them
# --------------------------------------------------------------------------- #

COMMITS = "/system/configuration/commit"


def _commits(*ids: int) -> dict[str, Any]:
    return {
        "srl_nokia-system:system": {
            "configuration": {"commit": [{"id": i, "status": "complete", "user": "admin"} for i in ids]}
        }
    }


@pytest.fixture
def target() -> Iterator[FakeTarget]:
    with FakeTarget({COMMITS: _commits(1, 2), "/": CONFIG}) as t:
        yield t


async def _until(condition: Any, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


async def test_the_engine_keeps_the_configuration_after_each_commit(
    target: FakeTarget, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "CONFIG_SETTLE", 0.0)
    node = Node("leaf1", "127.0.0.1", target.port, USERNAME, PASSWORD, Tls(insecure=True))
    store = AsyncStore(tmp_path / "h.db")
    eng = Engine([node], [REGISTRY["config-commits"]], store, settle=0.01, checks=False)
    events: list[dict[str, Any]] = []
    eng.listen(events.append)
    task = asyncio.create_task(eng.run())
    try:
        await _until(lambda: any(e["type"] == "config" for e in events))
        kept = await store.run(Store.configs)
        assert [c["commit_id"] for c in kept] == [2]

        changed = copy.deepcopy(CONFIG)
        changed["srl_nokia-interfaces:interface"][0]["admin-state"] = "disable"
        target.servicer.data[""] = changed
        events.clear()
        target.push({COMMITS + "[id=3]": {"id": 3, "status": "complete", "user": "ops"}})
        await _until(lambda: any(e["type"] == "config" for e in events))
        answer = await store.run(views.config_diff, "leaf1", 3)
        assert [r["kind"] for r in answer["rows"] if r["kind"] not in ("same", "skip")] == ["changed"]
        assert [c["id"] for c in answer["commits"]] == [3]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        store.close()


def test_a_commit_found_in_the_log_caused_nothing() -> None:
    """On a node's first sync its whole commit log comes in with everything else."""
    old = {
        "ts": 1_000_000.0,
        "node": "leaf1",
        "report": "config-commits",
        "op": "add",
        "after": {"id": 2, "username": "root", "ended": "1970-01-01T00:00:10Z"},
    }
    synced = {"ts": 1_000_000.0, "node": "leaf1", "report": "ifstats", "op": "add"}
    fresh = {**old, "ts": 15.0}
    out = views.attribute([old, synced], [old])
    assert [c["origin"] for c in out] == ["config", "unplanned"]
    assert views.attribute([synced], [fresh])[0]["origin"] == "unplanned"  # too long before
    assert views.attribute([{**synced, "ts": 30.0}], [fresh])[0]["origin"] == "config"


# --------------------------------------------------------------------------- #
# a commit on the far end
# --------------------------------------------------------------------------- #


def _far_fabric() -> Any:
    from fcli.fabric import Fabric

    ni, subif, lldp = (REGISTRY[r].record for r in ("ni", "subif", "lldp"))
    nodes = [Node(n, n) for n in ("leaf1", "leaf2", "spine1")]
    records = {
        "lldp": [
            lldp(
                node="leaf1",
                interface="ethernet-1/49",
                chassis_id="s1",
                system_name="spine1",
                port_id="ethernet-1/1",
            ),
            lldp(
                node="spine1",
                interface="ethernet-1/1",
                chassis_id="l1",
                system_name="leaf1",
                port_id="ethernet-1/49",
            ),
        ],
        "subif": [
            subif(node="leaf1", interface="ethernet-1/49", name="ethernet-1/49.0", ipv4=("192.168.1.1/31",)),
            subif(node="spine1", interface="ethernet-1/1", name="ethernet-1/1.0", ipv4=("192.168.1.0/31",)),
        ],
        "ni": [ni(node="spine1", ni="default", interface="system0.0", prefixes=("10.0.0.1/32",))],
    }
    return Fabric.of(nodes, records)


def _spine_commit(ts: float = 100.0) -> dict[str, Any]:
    after = {"id": 7, "username": "ops", "comment": "rr policy"}
    return {"ts": ts, "node": "spine1", "report": "config-commits", "op": "add", "after": after}


def test_a_commit_is_blamed_for_what_it_did_at_the_far_end() -> None:
    from fcli.incidents import FarEnds

    ends = FarEnds(_far_fabric())
    commit = _spine_commit()
    peer = {"node": "leaf1", "ni": "default", "state": "active"}
    changes = [
        # An overlay session to the spine's loopback, and an underlay one over the cable.
        {
            "ts": 130.0,
            "node": "leaf1",
            "report": "bgp-peers",
            "op": "change",
            "after": {**peer, "peer": "10.0.0.1"},
        },
        {
            "ts": 131.0,
            "node": "leaf1",
            "report": "bgp-peers",
            "op": "change",
            "after": {**peer, "peer": "fe80::1%ethernet-1/49.0"},
        },  # fmt: skip
        # The port at the near end of the cable, and a finding about the session.
        {
            "ts": 132.0,
            "node": "leaf1",
            "report": "ifstats",
            "op": "change",
            "after": {"name": "ethernet-1/49"},
        },
        {
            "ts": 133.0,
            "node": "leaf1",
            "report": "checks",
            "op": "add",
            "after": {
                "node": "leaf1",
                "check": "bgp_down",
                "severity": "error",
                "subject": "default/10.0.0.1",
                "detail": "session is active",
            },
        },  # fmt: skip
        # Nothing to do with the spine.
        {
            "ts": 134.0,
            "node": "leaf1",
            "report": "ifstats",
            "op": "change",
            "after": {"name": "ethernet-1/1"},
        },
        {
            "ts": 135.0,
            "node": "leaf2",
            "report": "bgp-peers",
            "op": "change",
            "after": {**peer, "peer": "10.0.0.9"},
        },
        # Too long after.
        {
            "ts": 400.0,
            "node": "leaf1",
            "report": "ifstats",
            "op": "change",
            "after": {"name": "ethernet-1/49"},
        },
    ]
    out = views.attribute(changes, [commit], ends)
    assert [c["origin"] for c in out] == ["config"] * 4 + ["unplanned"] * 3
    assert out[0]["cause"] == "commit 7 on spine1 by ops (rr policy)"
    assert out[0]["commit"] == {"node": "spine1", "id": 7, "seen": 100.0}
    # Without the far ends, only a commit on the change's own node counts.
    assert {c["origin"] for c in views.attribute(changes, [commit])} == {"unplanned"}


def test_the_latest_commit_at_either_end_is_the_cause() -> None:
    from fcli.incidents import FarEnds

    own = {
        "ts": 110.0,
        "node": "leaf1",
        "report": "config-commits",
        "op": "add",
        "after": {"id": 3, "username": "a"},
    }
    change = {
        "ts": 130.0,
        "node": "leaf1",
        "report": "ifstats",
        "op": "change",
        "after": {"name": "ethernet-1/49"},
    }
    ends = FarEnds(_far_fabric())
    assert views.attribute([change], [own, _spine_commit(100.0)], ends)[0]["commit"] == {
        "node": "leaf1",
        "id": 3,
        "seen": 110.0,
    }
    assert views.attribute([change], [own, _spine_commit(120.0)], ends)[0]["commit"] == {
        "node": "spine1",
        "id": 7,
        "seen": 120.0,
    }

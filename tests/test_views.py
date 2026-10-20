from pathlib import Path

from fcli.views import attribute


def _change(
    ts: float, node: str, report: str = "bgp-peers", op: str = "change", after: dict | None = None
) -> dict:
    return {"ts": ts, "node": node, "report": report, "op": op, "key": "k", "after": after or {}}


def test_a_change_is_put_down_to_the_commit_just_before_it_on_its_node() -> None:
    commit = _change(
        100.0, "leaf1", "config-commits", "add", {"id": 12, "username": "admin", "comment": "mtu"}
    )
    changes = [
        _change(130.0, "leaf1"),  # half a minute after: the commit's doing
        _change(130.0, "leaf2"),  # another node
        _change(400.0, "leaf1"),  # long after
        _change(90.0, "leaf1"),  # before it
    ]
    causes = [c["cause"] for c in attribute(changes, [commit])]
    assert causes == ["commit 12 by admin (mtu)", "", "", ""]


def test_a_time_of_day_is_the_last_time_it_was_that() -> None:
    from datetime import datetime

    from fcli.store import parse_time

    now = datetime(2026, 10, 13, 15, 0).timestamp()
    assert parse_time("14:02", now) == datetime(2026, 10, 13, 14, 2).timestamp()
    assert parse_time("16:30", now) == datetime(2026, 10, 12, 16, 30).timestamp()
    assert parse_time("30m", now) == now - 1800


def test_the_history_leaves_out_reports_of_rates(tmp_path: Path) -> None:
    from fcli.delta import Change, Op
    from fcli.store import Store
    from fcli.views import history_with_causes

    store = Store(tmp_path / "h.db")
    store.write(
        [
            Change(100.0, "leaf1", "ifstats", '["ethernet-1/1"]', Op.ADD, after={"name": "ethernet-1/1"}),
            Change(101.0, "leaf1", "lldp", '["ethernet-1/1"]', Op.ADD, after={"interface": "ethernet-1/1"}),
        ]
    )
    # Counters come and go with every sample: not events, even when asked for.
    assert [c["report"] for c in history_with_causes(store)] == ["lldp"]
    assert history_with_causes(store, reports=["ifstats"]) == []
    assert [c["report"] for c in store.history()] == ["ifstats", "lldp"]
    store.close()


def test_the_history_is_searched_in_every_value(tmp_path: Path) -> None:
    from fcli.delta import Change, Op
    from fcli.store import Store, matches

    store = Store(tmp_path / "h.db")
    changes = [
        Change(100.0, "leaf3", "subif", '["ethernet-1/49.0"]', Op.ADD, after={"name": "ethernet-1/49.0"}),
        Change(
            101.0,
            "leaf1",
            "bgp-peers",
            '["default","10.1.3.13"]',
            Op.CHANGE,
            before={"peer": "10.1.3.13", "state": "established", "up": True},
            after={"peer": "10.1.3.13", "state": "Idle", "up": False, "families": [{"name": "evpn"}]},
        ),
        Change(102.0, "leaf2", "mac-table", '["macvrf-102","aa:bb"]', Op.REMOVE, before={"ni": "macvrf-102"}),
        Change(103.0, "leaf2", "lldp", '["ethernet-1/1"]', Op.ADD, after={"oper-state": "up", "note": None}),
    ]
    store.write(changes)

    def found(text: str, **query: object) -> list[float]:
        listed = [c["ts"] for c in store.history(match=text, **query)]  # type: ignore[arg-type]
        # The same answer from the database as from a change in hand, as --follow filters events.
        assert listed == [c.ts for c in changes if matches(c.as_dict(), text)]
        return listed

    assert found("") == [100.0, 101.0, 102.0, 103.0]
    assert found("LEAF3") == [100.0]  # a node, case aside
    assert found("ethernet-1/4") == [100.0]  # part of a key or value
    assert found("10.1.3.13") == [101.0]
    assert found("idle") == [101.0]  # a value after
    assert found("established") == [101.0]  # or before
    assert found("evpn") == [101.0]  # inside a list
    assert found("false") == [101.0]  # a boolean, as JSON writes it
    assert found("macvrf-102") == [102.0]
    assert found("mac-table") == [102.0]  # a report
    assert found("oper-state") == []  # not a field name
    assert found("null") == []
    assert found("leaf1 idle") == [101.0]  # every word, anywhere
    assert found("leaf1 macvrf-102") == []
    assert [c["ts"] for c in store.history(match="ethernet", nodes=["leaf2"])] == [103.0]
    store.close()


def test_a_finding_is_raised_and_cleared_not_added_and_removed(tmp_path: Path) -> None:
    from fcli.checks import finding_event
    from fcli.delta import Change, Op
    from fcli.store import Store
    from fcli.views import history_with_causes

    key = '["bgp-session-down","10.0.0.1"]'
    warning = {"check": "bgp-session-down", "severity": "warning", "subject": "10.0.0.1", "detail": "idle"}
    error = {**warning, "severity": "error"}
    store = Store(tmp_path / "h.db")
    store.write(
        [
            Change(100.0, "leaf1", "checks", key, Op.ADD, after=warning),
            Change(110.0, "leaf1", "checks", key, Op.CHANGE, before=warning, after=error),
            Change(120.0, "leaf1", "checks", key, Op.CHANGE, before=error, after=warning),
            Change(130.0, "leaf1", "checks", key, Op.REMOVE, before=warning),
            Change(140.0, "leaf1", "lldp", '["ethernet-1/1"]', Op.ADD, after={"interface": "ethernet-1/1"}),
        ]
    )
    changes = history_with_causes(store)
    assert [c.get("finding") for c in changes] == ["raised", "escalated", "eased", "cleared", None]
    # Another report's add is still an add.
    assert finding_event(changes[-1]) == ""
    # A finding whose severity did not move, as an older record without one may be.
    assert finding_event({"report": "checks", "op": "change", "before": {}, "after": {}}) == "updated"
    store.close()

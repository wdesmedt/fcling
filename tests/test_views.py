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

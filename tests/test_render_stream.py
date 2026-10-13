from fcli.render import rows_for_records, table_titles
from fcli.reports import REGISTRY
from fcli.reports.bgp import BgpPeer


def test_rows_for_records_formats_cells() -> None:
    report = REGISTRY["bgp-peers"]
    peer = BgpPeer(node="leaf1", ni="default", peer="10.0.0.1", state="established")
    titles = table_titles(report)
    rows = rows_for_records(report, [peer])
    assert titles[0] == "Node"
    assert rows[0][0] == "leaf1"


def test_partials_carry_only_new_rows(monkeypatch) -> None:
    """Resending the table so far made a 61k-route RIB a gigabyte of events."""
    import asyncio

    from fcli import service
    from fcli.collect import NodeResult
    from fcli.inventory import Inventory, Node

    report = REGISTRY["bgp-peers"]
    node = Node(name="leaf1", address="leaf1")
    peers = [BgpPeer(node="leaf1", ni="default", peer=f"10.0.0.{i}", state="established") for i in range(5)]

    async def fake_collect(nodes, reports, *, on_batch, **_):
        result = NodeResult(node)
        for batch in (peers[:2], peers[2:]):
            await on_batch(result, batch)
        result.records = {report.name: peers}
        return [result]

    monkeypatch.setattr(service, "collect", fake_collect)
    events: list[dict] = []

    async def emit(event: dict) -> None:
        events.append(event["answer"])

    svc = service.EngineService(None, Inventory(name="lab", nodes=(node,)))
    answer = asyncio.run(svc._collect_report_streaming(report, {}, ["leaf1"], emit))
    assert [(len(e["rows"]), e["append"]) for e in events] == [(0, False), (2, True), (3, True)]
    assert [r[2] for e in events for r in e["rows"]] == [p.peer for p in peers]
    assert len(answer["rows"]) == 5

"""A :class:`~fcli.service.Service` with canned answers, for the tools and their clients."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fcli.service import ServiceError, catalog

PEERS = [
    {"node": "leaf1", "ni": "default", "peer": "10.0.0.1", "state": "established", "peer_as": 65001},
    {"node": "leaf1", "ni": "default", "peer": "10.0.0.2", "state": "active", "peer_as": 65002},
    {"node": "leaf2", "ni": "default", "peer": "10.0.0.3", "state": "established", "peer_as": 65003},
]
FINDING = {
    "node": "leaf1",
    "check": "bgp_down",
    "severity": "error",
    "subject": "default/10.0.0.2",
    "detail": "session is active, peer-group -, AS 65002",
}


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def lens(
        self, name: str, args: dict[str, Any] | None = None, at: str | None = None
    ) -> dict[str, Any]:
        from fcli.lens import LensError
        from fcli.lenses import LENSES

        from .lens_fabric import fabric

        self.calls.append(("lens", (name, args, at)))
        try:
            return {
                "records": [r.model_dump(mode="json") for r in LENSES[name].run(fabric(), **(args or {}))]
            }
        except LensError as exc:
            raise ServiceError(str(exc)) from exc

    async def topology(self, at: str | None = None) -> dict[str, Any]:
        from fcli.topology import build

        from .lens_fabric import fabric

        return build(fabric(), name="lab")

    async def status(self) -> dict[str, Any]:
        return {"fabric": "lab", "nodes": [{"name": "leaf1", "status": "live"}], "streamed": ["bgp-peers"]}

    async def reports(self) -> list[dict[str, Any]]:
        return catalog(["bgp-peers"])

    async def records(
        self,
        report: str,
        params: Any = None,
        filters: Sequence[str] = (),
        nodes: Sequence[str] = (),
        at: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("records", (report, params, list(filters), list(nodes), at)))
        if report != "bgp-peers":
            raise ServiceError(f"no report {report!r}")
        rows = [p for p in PEERS if not nodes or p["node"] in nodes]
        return {"report": report, "source": "live", "records": rows, "errors": {"leaf3": "no answer"}}

    async def checks(self, at: str | None = None, names: Sequence[str] = ()) -> list[dict[str, Any]]:
        return [{**FINDING, "raised": 100.0}]

    async def incidents(self, at: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "id": "x",
                "severity": "error",
                "kind": "session",
                "title": "leaf1 <-> 10.0.0.2: BGP session down",
                "node": "leaf1",
                "nodes": ["leaf1"],
                "explanation": "...",
                "first_seen": 100.0,
                "last_seen": 100.0,
                "root": FINDING,
                "related": [{**FINDING, "check": "bfd_down"}],
            }
        ]

    async def history(
        self,
        since: str | None = None,
        until: str | None = None,
        nodes: Sequence[str] = (),
        reports: Sequence[str] = (),
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return [
            {
                "seq": 1,
                "ts": 100.0,
                "node": "leaf1",
                "report": "bgp-peers",
                "key": '["default","10.0.0.2"]',
                "op": "change",
                "before": {"state": "established", "peer": "10.0.0.2"},
                "after": {"state": "active", "peer": "10.0.0.2"},
                "cause": "commit 7 by admin",
            }
        ]

    async def drift(
        self, since: str, until: str | None = None, reports: Sequence[str] = (), nodes: Sequence[str] = ()
    ) -> dict[str, Any]:
        raise ServiceError(f"{since!r} is neither a baseline nor a time")

    async def baselines(self) -> list[dict[str, Any]]:
        return []

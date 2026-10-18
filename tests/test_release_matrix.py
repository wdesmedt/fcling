"""Every ported report against every recorded SR Linux release.

The expected rows are what the original, Nornir-based fcli rendered from the
same Gets, so this is a parity check as much as a compatibility one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from fcli.render import cells, text
from fcli.report import EXCLUDED_INSTANCES, excluded_interface
from fcli.reports import REGISTRY

from .replay import RELEASES, node_state, recorded, recordings

#: New report name -> the name the recordings use.
PORTED = {
    "sys-info": "sys_info",
    "lldp": "lldp",
    "bgp-peers": "bgp_peers",
    "subif": "subif",
    "lag": "lag",
    "mac": "mac",
    "arp": "arp",
    "nd": "nd",
    "es": "es",
    "es-dest": "es_dest",
    "vxlan": "vxlan",
    "irb": "irb",
    "ni": "ni",
    "ifstats": "ifstats",
    "bfd": "bfd",
    "isis": "isis",
    "ospf": "ospf",
    "resources": "resources",
    "components": "components",
    "transceivers": "transceivers",
    "ipv4-rib": "ipv4_rib",
    "ipv6-rib": "ipv6_rib",
    "tunnel-table": "tunnel_table",
    "static-routes": "static_routes",
    **{f"bgp-rib-evpn-{t}": f"bgp_rib_evpn_{t}" for t in "12345"},
    "bgp-rib-ipv4-unicast": "bgp_rib_ipv4",
    "bgp-rib-ipv6-unicast": "bgp_rib_ipv6",
    "bgp-rib-l3vpn-ipv4-unicast": "bgp_rib_l3vpn_v4",
    "bgp-rib-l3vpn-ipv6-unicast": "bgp_rib_l3vpn_v6",
}

#: The recordings of the BGP RIBs hold every path received, not the used ones alone.
PARAMS = {name: {"paths": "all"} for name in PORTED if name.startswith("bgp-rib-")}

#: Columns that depend on when they are read: a countdown to an expiry, and
#: rates over the time the two samples actually took, which was not recorded.
VOLATILE = {"expiry", "next_state", "in-Kbps", "out-Kbps", "in-pps", "out-pps"}

#: The old tables gave a container without entries a row of its own (an ARP
#: cache with no neighbours, a bridge table with no MACs). Records are entries,
#: so for these reports those rows - the ones holding none of these columns -
#: are not expected.
ENTRY_COLUMNS = {
    "mac": {"mac"},
    "arp": {"IPv4"},
    "nd": {"IPv6"},
    "es-dest": {"esi"},
    "ipv4-rib": {"Prefix"},
    "ipv6-rib": {"Prefix"},
}


#: Columns of sets (``Record.unordered``): fcli listed them in the order the
#: node sent them, fcling sorts them. A string cell is a ``, ``-joined list.
UNORDERED: dict[str, frozenset[str]] = {
    "ipv4-rib": frozenset({"itf", "next-hop", "label"}),
    "ipv6-rib": frozenset({"itf", "next-hop", "label"}),
    "tunnel-table": frozenset({"next-hop", "egress-itf", "label"}),
    "es": frozenset({"itf/nh", "ni-peers"}),
}


def _as_set(value: Any) -> Any:
    if isinstance(value, str):
        return ", ".join(sorted(value.split(", ")))
    return sorted(value, key=str) if isinstance(value, list) else value


def _normal(
    row: dict[str, Any], columns: set[str], unordered: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """A row in the old table's *columns*, without empty or volatile cells.

    Header line breaks are collapsed, as the old CSV did. Columns the old
    table did not have are new, and have nothing to be compared with.
    """
    out = {}
    for title, value in row.items():
        # Older recordings kept the ``0_st`` the old tables ordered columns by.
        title = re.sub(r"^\d+_", "", " ".join(title.split()))
        if value in (None, "", [], ()) or title in VOLATILE or title not in columns:
            continue
        value = list(value) if isinstance(value, tuple) else value
        out[title] = _as_set(value) if title in unordered else value
    return out


def _canonical(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, default=str)


CASES = [
    pytest.param(path, name, id=f"{path.parent.name}/{path.name.removesuffix('.json.gz')}/{name}")
    for path in recordings()
    for name in PORTED
]


def _excluded(row: dict[str, Any]) -> bool:
    """fcli showed the management port and network-instance; fcling leaves them out."""
    if row.get("NI") in EXCLUDED_INSTANCES:
        return True
    return any(isinstance(v, str) and excluded_interface(v) for v in row.values())


@pytest.mark.parametrize(("path", "name"), CASES)
def test_report_matches_recording(path: Path, name: str) -> None:
    rec = recorded(path, PORTED[name])
    if rec is None or rec.get("getter_error"):
        pytest.skip("not recorded on this release")
    report = REGISTRY[name]
    records = report.run(node_state(path, PORTED[name]), **PARAMS.get(name, {}))
    unordered = UNORDERED.get(name, frozenset())
    columns = {re.sub(r"^\d+_", "", " ".join(c.split())) for c in rec["columns"]} or {
        " ".join(c.title.split()) for c in report.columns
    }

    def display(value: Any) -> Any:
        if isinstance(value, dict) and value.get("kind") == "bgp-rx-act-tx":
            return text(value)
        return value

    got = sorted(
        (_normal({k: display(v) for k, v in cells(report, r).items()}, columns, unordered) for r in records),
        key=_canonical,
    )
    rows = rec["rows"]
    if name in ENTRY_COLUMNS:
        rows = [row for row in rows if ENTRY_COLUMNS[name] & {k for k, v in row.items() if v}]
    rows = [row for row in rows if not _excluded(row)]
    want = sorted((_normal(row, columns, unordered) for row in rows), key=_canonical)
    assert got == want


def test_recordings_present() -> None:
    assert len(recordings()) >= 10, RELEASES

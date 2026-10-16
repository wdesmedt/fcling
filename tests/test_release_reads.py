"""The gNMI paths fcling sends, against what each SR Linux release accepts and answers.

`test_release_matrix` replays the Gets the original fcli made, so it checks
how a release shapes its payloads, not the paths fcling itself asks for.
These tests close that gap from two sides:

* **The old recordings** must hold every path a ported report reads, or the
  matrix compares a report against data it never asked for. What they lack is
  listed in ``UNRECORDED``.
* **fcling's own recordings** (``fixtures/reads``, made with
  ``python -m tests.capture``) say which elements a release refused and which
  reads came back empty. A refusal outside ``REFUSED``, or a read that went
  silent on a node that answered it before, is a schema change to look at.
  They also replay: every report must rebuild the records it built live, and
  must still read what was recorded - change a read, and record again.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from fcli.engine import MAX_SUBSCRIBED_PATHS, Plan
from fcli.gnmi import path as gpath
from fcli.reports import REGISTRY

from .replay import load, reads_recordings, reads_state, recorded, recordings
from .test_release_matrix import PARAMS, PORTED

# --------------------------------------------------------------------------- #
# the old recordings
# --------------------------------------------------------------------------- #

#: Reads of ported reports that no old recording holds: the matrix builds
#: these parts of the reports from nothing, on every release. fcling's own
#: recordings cover them.
UNRECORDED = {
    ("subif", "/interface[name=*]/oper-down-reason"): "the old subif read no down reason",
    ("ifstats", "/interface[name=*]/oper-state"): "the old ifstats read counters only",
    ("ifstats", "/interface[name=*]/oper-down-reason"): "the old ifstats read counters only",
    ("ni", "/routing-policy"): "the old ni read no policies",
    ("resources", "/platform/resource-monitoring/datapath"): "newer datapath resources",
    **{
        ("components", f"/platform/fabric[slot=*]/{leaf}"): "the old components read no fabric cards"
        for leaf in ("healthz", "oper-state", "serial-number", "type")
    },
}


def _call_paths(call: dict[str, Any]) -> list[str]:
    path = call.get("path")
    return [path] if isinstance(path, str) else list(path or call.get("paths") or [])


def unrecorded(path: Path) -> set[tuple[str, str]]:
    """The (report, read) pairs of *path*'s ported reports that none of its Gets covers."""
    missing = set()
    for name, old in PORTED.items():
        rec = recorded(path, old)
        if rec is None or rec.get("getter_error"):
            continue
        got = [gpath.parse(p) for call in rec["calls"] for p in _call_paths(call)]
        for read in REGISTRY[name].planned_reads(PARAMS.get(name)):
            want = gpath.parse(read.path)
            if not any(gpath.matches(g, want) for g in got):
                missing.add((name, read.path))
    return missing


@pytest.mark.parametrize(
    "path", recordings(), ids=lambda p: f"{p.parent.name}/{p.name.removesuffix('.json.gz')}"
)
def test_old_recordings_hold_every_read(path: Path) -> None:
    missing = unrecorded(path) - set(UNRECORDED)
    assert not missing, f"no recorded Get covers {sorted(missing)}: the matrix compares them against nothing"


def test_unrecorded_is_current() -> None:
    """Each listed gap is still a gap somewhere: a stale entry would hide a new one."""
    seen = set().union(*(unrecorded(p) for p in recordings()))
    assert set(UNRECORDED) <= seen, sorted(set(UNRECORDED) - seen)


# --------------------------------------------------------------------------- #
# fcling's own recordings
# --------------------------------------------------------------------------- #

#: Elements a node may refuse - its schema leaves out what it does not run -
#: and why. Any other refusal is a path a release renamed or removed.
REFUSED = {
    "l3vpn-ipv4-unicast": "the bgp-rib has l3vpn families only on a node configured for MPLS IP-VPN",
    "l3vpn-ipv6-unicast": "the bgp-rib has l3vpn families only on a node configured for MPLS IP-VPN",
    "fabric": "fixed-form platforms have no fabric cards",
    "vxlan-interface": "a node without the vxlan feature: a spine",
    "tunnel-interface": "a node without the vxlan feature: a spine",
    "bgp-evpn": "a node without the evpn feature: a spine",
    "evpn": "a node without the evpn feature: a spine",
}

#: Reads that a recorded node accepts but answers with nothing, by the node's
#: recording name: it has nothing to show. A read that goes silent on a
#: node listed here, or on a release where it answered before, means data
#: moved - a leaf that left the state datastore answers like this.
_NOTHING_CONFIGURED = {
    "/network-instance[name=*]/protocols/isis/instance[name=*]/interface[interface-name=*]": "no IS-IS",
    "/network-instance[name=*]/protocols/ospf/instance[name=*]/area[area-id=*]/interface[interface-name=*]": (
        "no OSPF"
    ),
    "/network-instance[name=*]/static-routes": "no static routes",
    "/network-instance[name=*]/next-hop-groups": "no static routes",
}
_V6_RIB = "/network-instance[name=*]/bgp-rib/afi-safi[afi-safi-name=ipv6-unicast]/ipv6-unicast"
SILENT = {
    "nvd-25-leaf1": _NOTHING_CONFIGURED,
    "nvd-25-spine1": {
        **_NOTHING_CONFIGURED,
        "/interface[name=lag*]": "no LAGs",
        "/network-instance[name=*]/bridge-table/mac-table/mac": "no mac-vrfs",
        f"{_V6_RIB}/local-rib/route": "no IPv6 BGP routes",
        f"{_V6_RIB}/rib-in-out/rib-out-post/route": "no IPv6 BGP routes",
    },
}


#: What a recording is called, whatever release it was made on.
def _node_key(path: Path) -> str:
    return path.name.removesuffix(".json.gz")


@cache
def _data(path: Path) -> dict[str, Any]:
    return load(path)


def _id(path: Path) -> str:
    return f"{path.parent.name}/{_node_key(path)}"


def _entry_id(entry: dict[str, Any]) -> str:
    params = ",".join(f"{k}={v}" for k, v in sorted(entry["params"].items()))
    return f"{entry['report']}[{params}]" if params else entry["report"]


ENTRIES = [
    pytest.param(path, i, id=f"{_id(path)}/{_entry_id(entry)}")
    for path in reads_recordings()
    for i, entry in enumerate(_data(path)["reports"])
]

_ELEMENT = re.compile(r"unknown element '([^']+)'")


def _refusals(data: dict[str, Any]) -> dict[str, set[str]]:
    """The elements a node refused, each with the paths that named it."""
    out: dict[str, set[str]] = defaultdict(set)
    for entry in data["reports"]:
        for call in entry["calls"]:
            if m := _ELEMENT.search(call.get("error", "")):
                out[m[1]] |= {p for p in call["paths"] if any(e.name == m[1] for e in gpath.parse(p))}
            elif "error" in call:
                out[f"error: {call['error']}"] |= set(call["paths"])
    for stream in data["streams"]:
        for r in stream["refused"]:
            out[r["element"]] |= set(r["paths"])
        if "error" in stream:
            out[f"error: {stream['error']}"] |= set(stream["paths"])
    return out


def _silent(data: dict[str, Any]) -> set[str]:
    """Paths a node accepted and answered with nothing, in every Get that named them."""
    answered: dict[str, bool] = {}
    for entry in data["reports"]:
        for call in entry["calls"]:
            if "error" in call:
                continue
            updated = [gpath.parse(p) for n in call["notifications"] for p, _ in n["updates"]]
            for p in call["paths"]:
                want = gpath.parse(p)
                # A follow Get names entries by key: the reads are its pattern.
                key = re.sub(r"\[(\w[\w-]*)=[^\]*]+\]", r"[\1=*]", p) if "[index=" in p else p
                hit = any(gpath.overlaps(want, u) for u in updated)
                answered[key] = answered.get(key, False) or hit
    return {p for p, hit in answered.items() if not hit}


def test_reads_recorded() -> None:
    assert reads_recordings(), "no recordings in tests/fixtures/reads: run python -m tests.capture"


@pytest.mark.parametrize("path", reads_recordings(), ids=_id)
def test_refused_elements_are_known(path: Path) -> None:
    unknown = {e: sorted(p) for e, p in _refusals(_data(path)).items() if e not in REFUSED}
    assert not unknown, f"{_data(path)['release']} refused {json.dumps(unknown, indent=1)}"


@pytest.mark.parametrize("path", reads_recordings(), ids=_id)
def test_no_read_went_silent(path: Path) -> None:
    known = SILENT.get(_node_key(path))
    assert known is not None, (
        f"add {_node_key(path)} to SILENT, with why each of {sorted(_silent(_data(path)))}"
    )
    new = _silent(_data(path)) - set(known)
    assert not new, f"accepted but empty on {_data(path)['release']}: {sorted(new)}"


def test_silent_is_current() -> None:
    """Each listed silent read is silent on some release of its node."""
    by_node: dict[str, set[str]] = defaultdict(set)
    for path in reads_recordings():
        by_node[_node_key(path)] |= _silent(_data(path))
    stale = {node: sorted(set(paths) - by_node[node]) for node, paths in SILENT.items()}
    assert not any(stale.values()), stale


@pytest.mark.parametrize(("path", "index"), ENTRIES)
def test_report_reads_what_was_recorded(path: Path, index: int) -> None:
    entry = _data(path)["reports"][index]
    planned = [
        {"path": r.path, "datatype": str(r.datatype), "mode": str(r.mode)}
        for r in REGISTRY[entry["report"]].planned_reads(entry["params"])
    ]

    def key(read: dict[str, str]) -> str:
        return json.dumps(read, sort_keys=True)

    assert sorted(map(key, planned)) == sorted(map(key, entry["reads"])), (
        f"{entry['report']} reads something else now: record again with python -m tests.capture"
    )


def _comparable(record: dict[str, Any], volatile: tuple[str, ...]) -> dict[str, Any]:
    out = dict(record)
    for name in volatile:
        field, _, inner = name.partition(".")
        if not inner:
            out.pop(field, None)
        elif isinstance(out.get(field), list):
            out[field] = [{k: v for k, v in item.items() if k != inner} for item in out[field]]
    return out


@pytest.mark.parametrize(("path", "index"), ENTRIES)
def test_report_replays(path: Path, index: int) -> None:
    data = _data(path)
    entry = data["reports"][index]
    report = REGISTRY[entry["report"]]
    records = report.run(reads_state(data, entry), **entry["params"])
    volatile = report.record.volatile

    def canonical(rows: list[dict[str, Any]]) -> list[str]:
        return sorted(json.dumps(_comparable(r, volatile), sort_keys=True) for r in rows)

    got = canonical([r.model_dump(mode="json") for r in records])
    # Through the model, so a set recorded in the order the node sent it is put in its order.
    want = [report.record.model_validate(r).model_dump(mode="json") for r in entry["records"]]
    assert got == canonical(want)


@pytest.mark.parametrize("path", reads_recordings(), ids=_id)
def test_streamed_paths_recorded(path: Path) -> None:
    """Every path the engine subscribes to was subscribed to when recording."""
    plan = Plan([r for r in REGISTRY.values() if not r.hidden])
    want = {r.path for chunk in plan.chunks(MAX_SUBSCRIBED_PATHS) for r in chunk}
    have = {p for s in _data(path)["streams"] for p in s["paths"]}
    assert want <= have, f"not recorded: {sorted(want - have)}; record again with python -m tests.capture"


@pytest.mark.parametrize("path", reads_recordings(), ids=_id)
def test_streams_synced(path: Path) -> None:
    for stream in _data(path)["streams"]:
        assert stream.get("synced"), stream

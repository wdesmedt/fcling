"""Acknowledged findings: kept per finding, and on disk when asked."""

from pathlib import Path

from fcli.acks import AckStore, finding_key, path_for
from fcli.checks import Finding


def _finding(subject: str) -> Finding:
    return Finding(node="leaf1", check="bgp_down", severity="error", subject=subject, detail="down")


def test_acknowledgements_by_finding_and_by_incident(tmp_path: Path) -> None:
    acks = AckStore()
    made = acks.acknowledge(
        [_finding("a"), {"check": "bgp_down", "node": "leaf1", "subject": "b"}], "known", "inc1"
    )
    assert {a.key for a in made} == {("bgp_down", "leaf1", "a"), ("bgp_down", "leaf1", "b")}
    assert acks.get(finding_key(_finding("a"))).note == "known"
    assert acks.of_incident("inc1") == acks.keys()
    assert [a.subject for a in acks.unacknowledge([("bgp_down", "leaf1", "a"), ("x", "y", "z")])] == ["a"]
    assert acks.keys() == {("bgp_down", "leaf1", "b")}


def test_kept_across_restarts_and_survive_a_bad_file(tmp_path: Path) -> None:
    path = path_for(tmp_path / "lab.db")
    assert path.name == "lab.acks.json"
    AckStore(path).acknowledge([_finding("a")], "ticket 7", "inc1", "leaf1 BGP down")
    (again,) = AckStore(path).all()
    assert (again.subject, again.note, again.incident_id, again.incident) == (
        "a",
        "ticket 7",
        "inc1",
        "leaf1 BGP down",
    )
    path.write_text("{not json")
    assert AckStore(path).all() == []
    assert AckStore(None).all() == []

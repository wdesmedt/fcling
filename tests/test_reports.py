"""Reports whose old rows are not worth matching, against the recordings' payloads."""

from fcli.render import cells
from fcli.reports import REGISTRY

from .replay import RELEASES, node_state


def test_routing_policy_statements() -> None:
    state = node_state(RELEASES / "26.3.1" / "dcgw1.json.gz", "routing_pol")
    rows = [cells(REGISTRY["routing-pol"], r) for r in REGISTRY["routing-pol"].run(state)]
    by = {(r["kind"], r["name"], r["entry"]): r for r in rows}
    assert by[("prefix-set", "host-routes-l3dci", "10.200.0.0/16 32..32")]
    assert by[("extended-community-set", "soo-dc1", "origin:65000:1")]
    export = by[("policy", "dcgw-dc-export-dc1", "12")]
    assert export["match"] == "family=evpn extended-community.extended-community-set=rt-l2dci-b"
    assert export["result"] == "accept"
    assert export["action"] == (
        "prepend.as-number=65000 prepend.repeat-n=2 "
        "extended-community.operation=add extended-community.referenced-sets=soo-dc1"
    )
    assert by[("policy", "dcgw-dc-export-dc1", "default")]["result"] == "reject"

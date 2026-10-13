"""gNMI JSON-IETF decoding edge cases."""

import json

from fcli.gnmi.client import decode_json_ietf


def test_extra_json_documents_are_merged() -> None:
    first = {"srl_nokia-bgp-rib:bgp-rib": {"attr-sets": {"attr-set": []}}}
    second = {"srl_nokia-bgp-rib:bgp-rib": {"afi-safi": [{"afi-safi-name": "evpn"}]}}
    raw = json.dumps(first).encode() + json.dumps(second).encode()
    merged = decode_json_ietf(raw)
    assert merged["srl_nokia-bgp-rib:bgp-rib"]["attr-sets"]["attr-set"] == []
    assert merged["srl_nokia-bgp-rib:bgp-rib"]["afi-safi"][0]["afi-safi-name"] == "evpn"


def test_single_json_unchanged() -> None:
    raw = json.dumps({"a": 1}).encode()
    assert decode_json_ietf(raw) == {"a": 1}

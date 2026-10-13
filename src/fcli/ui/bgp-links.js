// Links from bgp-peers Rx/Act/Tx into the matching BGP RIB report.

import { withQuery } from "./api.js";

const RIB_BY_FAMILY = {
  evpn: "bgp-rib-evpn",
  "ipv4-unicast": "bgp-rib-ipv4-unicast",
  "ipv6-unicast": "bgp-rib-ipv6-unicast",
  "l3vpn-ipv4-unicast": "bgp-rib-l3vpn-ipv4-unicast",
  "l3vpn-ipv6-unicast": "bgp-rib-l3vpn-ipv6-unicast",
};

/** Hash path for received, active, or advertised routes from one peer and afi-safi. */
export function bgpPeerRibPath(cell, which) {
  const report = RIB_BY_FAMILY[cell.family] ?? `bgp-rib-${cell.family}`;
  const direction = { rx: "received", act: "active", tx: "advertised" }[which];
  const params = {
    node: cell.node,
    neighbor: cell.peer,
    direction,
    paths: "used",
    ...(cell.family !== "evpn" && cell.ni ? { ni: cell.ni } : {}),
  };
  return withQuery(`report/${report}`, params);
}

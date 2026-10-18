# Checks and incidents

```bash
fcling checks                   # every finding
fcling checks -k bgp_down -k itf_down
fcling incidents                # grouped by root cause, worst first
fcling incidents -v             # with every finding of each
fcling incidents --at 2h        # what was wrong then
```

Both work one-shot, through a server with `--server`, or from the history with `--at`. Both exit 1 when there is an error-level finding, so they can gate a pipeline or a change window.

A finding is an `error` (the fabric isn't doing what it was built to do), a `warning` (legitimate in some fabrics, a fault in most) or `info` (worth knowing, often by design). An incident takes the worst severity of its findings. Info is listed, but it marks no node or link as unhealthy in the topology.

## Checks

A check looks at the whole fabric at once. That lets it see what no single node's table shows: a link only one end reports, two leaves configured differently for the same service, the two ends of a BGP session expecting different AS numbers.

The management port `mgmt0` and the `mgmt` network-instance are in no report, so no check or incident is ever about them.

| Check | Finds |
|---|---|
| `node_unreachable` | nodes that could not be read, with the reason |
| `bgp_down` | BGP sessions that are not established |
| `bgp_af_down` | established sessions with an address family down: the session looks fine, but the family carrying the overlay never came up |
| `bgp_no_routes` | established families that have received nothing (info: normal for a family no peer sends, such as IPv6 over an IPv4-only underlay) |
| `bgp_peer_mismatch` | the two ends of a session disagree: peer AS against local AS, families enabled on one end only, BFD on one end only |
| `itf_down` | subinterfaces enabled but down, with the root cause (a port in ES standby is `down/standby`, which is intended and not a fault) |
| `itf_errors` | ports dropping or discarding packets during the sample (not on containerlab nodes, where a veth drops traffic all the time) |
| `lldp_one_sided` | a neighbour one end sees and the other doesn't: a link not carrying in both directions |
| `mtu_mismatch` | the two ends of a link with different IP MTUs |
| `mtu_outlier` | a node whose fabric MTU differs from what a clear majority of the fabric uses |
| `evpn_service_mismatch` | nodes carrying one service with a different VNI or route-targets. Nodes are compared only within an underlay they share, per BGP-VPN instance (a gateway's WAN side against other gateways), and among nodes that share a route-target. Two services under one name is a warning about the split. |
| `es_df` | ethernet-segments down, with no designated forwarder, with nodes disagreeing on the DF, or with different multi-homing modes |
| `bfd_down` | BFD sessions not up. When the far end has never answered, it asks whether BFD is enabled there. |
| `igp_adjacency_down` | IS-IS adjacencies and OSPF neighbours not up |
| `igp_no_adjacency` | IGP interfaces up and not passive, with nobody answering: area, level, authentication or MTU |
| `igp_peer_mismatch` | the two ends of a link with the IGP on one end only, different OSPF areas, different network types, or passive on one end |
| `resource_high` | CPU, memory or a forwarding table at 80% or more (or past the node's own threshold); an error from 95% |
| `hardware_fault` | cards, fans or power supplies fitted and not working |
| `optic_dom` | optics reporting a DOM alarm (error) or warning |
| `flapping` | something that changed state 3 or more times in 10 minutes: a session, a port, a LAG member, a MAC moving between ports, an IP changing MAC. Needs the history, so only through a server. |

A containerlab node reports its host's memory and CPU, so in a lab `resource_high` on those describes the machine running the lab: it says so, and is `info` rather than a warning or an error. Forwarding tables are the node's own, and keep their severity.

Through a server, every finding has a `since`: when it was raised. Findings are kept in the history like report records, under `checks`, so a finding being raised or cleared is a change you can query.

## Incidents

One broken cable is not one finding. It brings an interface down on both ends, the BGP and BFD sessions over it down on both ends, perhaps an IGP adjacency and an ethernet-segment without a forwarder. Each finding is true, but none says they are all the same thing.

`incidents` groups them. Each finding is anchored to what it is about, and the findings sharing an anchor form one incident:

| Anchor | Rule |
|---|---|
| **a cable** | A finding about a port belongs to the cable that port is part of: as LLDP sees it now, as the history saw it before the link went down, or as the two ends of a point-to-point subnet show. A finding about a session belongs to the port it runs over (a link-local peer names it; any other peer is on a connected subnet). |
| **a node** | Findings about a node that stopped answering, or about sessions to it. |
| **the underlay** | An overlay session to a loopback the node has no route to is about the underlay between the two nodes, which is where to look instead of BGP. |
| **a session, a segment, a platform** | What is about those alone. |

Within an incident, the root cause is the most fundamental finding. The order is: node unreachable, hardware, optics, interface down, one-sided LLDP, MTU, errors, underlay, IGP mismatch, IGP adjacency, BGP mismatch, BFD, BGP down, BGP family, flapping, ethernet-segment, no routes, resources. The rest are its consequences, counted in the explanation:

```
ERROR   leaf1 e1/1 <-> spine1 e1/1: interface down  (5 findings)  since 2026-10-13 14:02:31
        admin enabled but oper down: port-down. It accounts for 1 interface down, 1 BFD session down, 2 BGP sessions down.
```

**Patterns:** the same cause in three or more places folds into one incident. Examples are BFD down with a silent far end on 16 links, or memory high on 18 nodes. Names and numbers are ignored when comparing causes; everything that is flapping is one incident.

Grouping never drops a finding: together, the incidents hold exactly the findings.

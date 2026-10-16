# Lenses and the topology

A report reads one node. A lens asks the whole fabric a question with an argument: where is this address, how would this leaf reach that host, what does this service look like on every node. It answers from several reports across all nodes at once.

A lens is a pure function of the fabric, like a check. It declares the reports it reads, so it answers the same way from the nodes, from the engine, or from the history with `--at`. Like a report, it answers in typed records with columns, so `-f`, `-o json|yaml|csv` and the API work the same way for both.

```bash
fcling -t lab.clab.yml where 10.1.3.13              # an IP or a MAC
fcling -t lab.clab.yml path leaf1 10.1.3.13 --ni ipvrf-1
fcling -t lab.clab.yml path 10.1.5.1 10.1.3.17      # from a host: it enters at its leaf
fcling -t lab.clab.yml service macvrf-102
fcling -t lab.clab.yml endpoints -f MAC-VRF=macvrf-101
fcling -t lab.clab.yml topology
fcling --server http://127.0.0.1:8765 where AA:C1:AB:73:4C:27 --at 30m
```

## Services: three reports

These are reports, one node at a time. The lenses build on them.

| Report | |
|---|---|
| `services` | every network-instance as the service it is: type, EVI, VNI, route targets, subnets, ports (bridged, routed, irb, each with its ethernet-segment), the instances linked over irbs, and an ip-vrf's BGP sessions and virtual segments |
| `bridge-domains` | the mac-vrfs alone |
| `routers` | the ip-vrfs alone |

## `where ADDRESS`

Every place in the fabric that knows an IP or a MAC.

| kind | |
|---|---|
| `configured` | it is a subinterface's own address |
| `arp`, `nd` | a binding for the IP. Its MAC is followed into the bridge table behind the irb, to the port, segment or VTEP the host is on. |
| `local`, `remote` | a MAC learned on this node's port, or over the overlay from a VTEP or a segment |
| `multihomed` | learned locally on several nodes, all on one ethernet-segment: as intended |
| `duplicate` | learned locally on several nodes on no common segment, or one IP bound to different MACs |
| `route` | a host route to the IP, and what it resolves over |
| `not-found` | nowhere |

## `path SOURCE DESTINATION [--ni NI]`

How the fabric would forward to an address, hop by hop. The answer comes from the route and bridge tables. Nothing is sent.

- **Source**: a node, or a host's IP or MAC. A host enters with an `ingress` hop on each leaf it is attached to: on a multihomed segment, every leaf of the segment.
- **Destination**: an IP is routed, and a MAC is bridged.
- **`--ni`**: the instance to start in. Without it, the path starts in the source host's instance, or in `default` from a node.

The walk follows ECMP branches. It goes into VXLAN tunnels and comes out at the node that owns the far end, then resumes in the instance that imports what the origin exports. It follows leaked routes, and a MAC behind a segment is aliased over every VTEP of that segment.

Hops are numbered by node, in two columns: `hop` is the cables crossed to reach the node, and `lookup` which lookup on that node it is; `address` is what was looked up. From leaf1 to a host behind a VTEP: hop 0 lookup 1 (the VRF on leaf1, into a tunnel), hop 0 lookup 2 (the underlay on leaf1, out to the spines), hop 1 lookup 1 (each spine), hop 2 lookups 1 and 2 (the far leaf decapsulates, then looks up in its VRF). Rows are sorted by hop, node, then lookup.

Each hop has an outcome:

| outcome | |
|---|---|
| `ingress` | where a source host enters |
| `forwarded` | out of a port to the next node |
| `tunnel` | into a VXLAN or MPLS tunnel |
| `endpoint-reached` | the tunnel's far end, and the instance the walk resumes in |
| `leaked` | the route was leaked from another instance, and the lookup moves there |
| `delivered` | an attached host: its MAC, port and segment |
| `handed-off` | out of the fabric to a next-hop ARP/ND knows |
| `no-neighbor` | attached, but ARP/ND has no entry |
| `local-ip` | an address of the node itself |
| `no-route`, `discard`, `dead-end`, `unresolved` | where it stops, and why |
| `loop`, `too-long` | a lookup made before on the same walk, or more than 16 hops |

## `service NAME`

One mac-vrf or ip-vrf on every node that carries it: state, VNI, EVI, route targets, access ports and segments, irbs, and MACs learned locally and remotely. The `issues` column lists what the checks found wrong with it on that node: a VNI or route target that differs from the other nodes, or a segment with no designated forwarder or with two. An unknown name lists the services there are.

## `endpoints`

Every host attached to the fabric, on each node it is attached to. A host is an ARP/ND binding on an access port or an irb, placed by its MAC in the bridge table. A host that only bridges is listed by its MAC. The output leaves out management, the links between fabric nodes, and hosts a node only learned over EVPN. A host on a multihomed segment is listed on every node of the segment (`learned remote` where the peer learned it).

## `topology`

The fabric as a graph. Tiers are inferred from what the nodes run, not from labels:

| tier | |
|---|---|
| `gateway` | has a service with two bgp-vpn instances: the WAN side of a stitched service |
| `leaf` | has mac-vrfs |
| `spine` | no services, cabled to two or more leaves |
| `core` | anything else without services |

Cables are LLDP adjacencies. Each is counted once, and parallel cables are folded into one link with a count. A cable LLDP no longer reports but the history saw is kept as `lost`. Clients are what the services' access ports face, one per host: grouped by the name the host gives over LLDP, else by its ethernet-segment, else by its port. Each node carries its findings, and its health is `ok`, `warning` or `error`. A fabric whose parts share no cable becomes several components.

The text output shows the tiers (`~` marks a warning, `!` an error), any cable that is not up, the clients with their ports and services, and the segments. `-o json` gives the whole graph.

## Where the answers come from

| | now | `--at` |
|---|---|---|
| `-t` / `-i` | collected from the nodes | the local history, if `fcling serve` keeps one |
| `--server` | the engine's live state | the server's history |

An `--at` before the history starts is an error that says when it starts. It never returns an empty answer.

Over HTTP: `GET /api/lenses` lists the lenses with their arguments and columns. `GET /api/lenses/{name}?ARG=...&at=...` runs one, and `GET /api/topology?at=...` returns the graph. The tools `locate_address`, `trace_path`, `service_detail`, `list_endpoints` and `get_topology` answer from the same lenses ([ai.md](ai.md)).

## Adding a lens

```python
@lens(
    "where",
    "Every place in the fabric that knows an IP or MAC address",
    requires=("mac", "arp", "nd", "es", "services", "subif", "ipv4-rib", "ipv6-rib", "lldp"),
    record=Sighting,
    args=[Arg("address", "the IP or MAC address to look for", required=True)],
    columns=_COLUMNS,
)
def where(fabric: Fabric, address: str) -> Iterator[Sighting]: ...
```

Look things up through `Index` in [lenses/common.py](../src/fcli/lenses/common.py), so two lenses can't read one fact differently. `requires` must name every report the lens reads, including those behind `Index` (loopbacks come from `ni`). From the history or the engine, a report not named there is empty. Raise `LensError` for an argument that makes no sense: every surface shows its message. A required argument becomes a positional on the command line, and an optional one becomes an option. Set `ordered=True` when the rows' order is the answer, as with a path's hops.

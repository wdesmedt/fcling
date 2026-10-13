# SR Linux release matrix

A report declares the gNMI paths it reads, and expects a YANG structure back.
Both move between SR Linux releases, and when they move the failure is usually
silent. A path that no longer carries a value leaves a column empty rather than
raising anything.

Two sets of recordings check this, with no lab needed:

| | Recorded by | What it checks |
|---|---|---|
| [`releases/`](.) (this directory) | the original, Nornir-based [fcli](https://github.com/srl-labs/fcli) | **Payload shape and parity.** [test_release_matrix.py](../../test_release_matrix.py) feeds the old getters' Gets through fcling's reports, and requires row for row what the original fcli rendered from them. |
| [`reads/`](../reads/) | fcling itself, with [`tests/capture.py`](../../capture.py) | **The paths fcling sends.** [test_release_reads.py](../../test_release_reads.py) checks which elements each release refused, which reads it answered with nothing, and whether the engine's subscriptions synced. It also replays every report against its own recorded Gets. |

The first set says nothing about fcling's own paths. Its trees are filled by
the old getters, which mostly read wider containers. A leaf that a release
renamed would be refused live, while the old Get still carried it.
`test_old_recordings_hold_every_read` requires each ported report's reads to
fall inside the recorded Gets. `UNRECORDED` lists the reads the old recordings
lack, and `reads/` covers them.

## What is recorded

| Release | `releases/` (fcli) | `reads/` (fcling) |
|---|---|---|
| 25.3.2 | 4l2s `l1`, `s1` | |
| 25.10.3 | 4l2s `l1`, `s1` | |
| 25.10.4 | | NVD `leaf1`, `spine1` |
| 26.3.1 | 4l2s `l1`, `s1`; DCI `leaf1`, `dcgw1` | |
| 26.7.1 | 4l2s `l1`, `s1` | |
| 26.7.2 | NVD `leaf1`, `spine1` | |

No release before 25.3 is recorded. The BGP RIB layouts before 24.10 are not
supported, and 24.10 itself is untested.

### The labs

**4l2s**: [srl-labs/intent-based-ansible-lab](https://github.com/srl-labs/intent-based-ansible-lab),
`topo.clab.yml`. Four leaves, two spines, ten Linux clients.

| | |
|---|---|
| Underlay | eBGP, one ASN per leaf, spines in 65100 |
| Overlay | iBGP EVPN to both spines as route reflectors |
| Services | `subnet-1` (mac-vrf, VLAN 100), `subnet-2` (mac-vrf, VLAN 200), `ipvrf-1` (ip-vrf 2001, anycast IRBs on both) |
| Multi-homing | Five all-active ethernet segments over LACP LAGs to the clients |
| Recorded | `l1` (leaf: services, LAGs, ethernet segments, learned state) and `s1` (spine: underlay only) |

**DCI**: the [DCI validated design](https://github.com/nokia/nokia-validated-designs)
(`validated-designs/dci/dci-without-eda`). Two datacenters with an IPv6
link-local eBGP underlay, BFD towards the spines, and DCGWs stitching
EVPN-VXLAN to EVPN-MPLS over IS-IS and LDP. `leaf1` covers BFD and learned EVPN
state. `dcgw1` covers IS-IS and is the only recorded node with an l3vpn RIB.
These recordings came with the BFD, IS-IS, OSPF, resources, components and
transceivers reports, which the 4l2s recordings predate. That is why those
reports skip on 4l2s: the 48 skipped tests.

**NVD**: the 3-stage EVPN-VXLAN fabric in `~/github/3stage-nvd-25`. Four spines
and six leaves (mixed `ixr-d2l`..`ixr-h5`), two ip-vrfs, eight mac-vrfs,
all-active LAG and single-active multi-homing, IPv6 and BFD. `leaf1` has a
virtual (layer-3) ethernet segment with a `next-hop`/`evi`. It is the only
recorded node with one.

## Recording fcling's reads

```bash
uv run python -m tests.capture -t ~/github/3stage-nvd-25/3-stage-evpn-vxlan-nvd-25.clab.yml \
    --node leaf1 --node spine1 --prefix nvd-25-
```

The tool only reads: Capabilities, Get and Subscribe, with one connection per
node and one node at a time. It writes `reads/<release>/<prefix><node>.json.gz`,
taking the release from `sys-info` unless `--release` is given. Each file holds:

- every report's Gets as the collector makes them: the declared reads, then
  what `follow` asks for, each with its answer or the error that refused it.
  `ifstats` takes its two samples. Each BGP RIB report is recorded listed
  whole, once with every path received and once as advertised;
- the records each report built, which the replay must reproduce, leaving out
  the record's `volatile` fields;
- the engine's subscription plan, Subscribe by Subscribe up to the initial
  sync, with what each node refused.

Record again after changing what a report reads, or the replay test says so.
For a new release, record the same nodes under the same `--prefix`. A read that
answered on the old release and comes back empty on the new one then fails
`test_no_read_went_silent`, unless it is added to `SILENT` with a reason.

Run `traffic` in the lab first (ping between the clients). `mac`, `arp`, `nd`,
`es-dest`, `irb` and `vxlan` read learned state. Without traffic they come back
empty, and an empty report cannot tell a working path from a broken one.

### What a node may refuse

A Get or Subscribe naming an element outside the node's schema is refused
whole, naming the element. The collector and the engine drop those paths and
ask again. The schema is not the release's alone: it leaves out features the
node does not run. Today these are expected (`REFUSED`):

| Element | Refused where |
|---|---|
| `l3vpn-ipv4-unicast`, `l3vpn-ipv6-unicast` | any node not configured for MPLS IP-VPN: every recorded node but `dcgw1` |
| `fabric` | fixed-form platforms, which have no fabric cards |
| `vxlan-interface`, `tunnel-interface`, `bgp-evpn`, `evpn` | a node without the vxlan and evpn features: the NVD spine |

Any other refusal fails the test. It is most likely a path the release renamed
or removed.

## Re-recording `releases/`

These recordings are made with the original fcli's tooling, from a checkout of
[`~/github/fcli`](https://github.com/srl-labs/fcli) (`tests/system/`), not from
this repository. They record the old getters' Gets, so they don't need to be
made again when a fcling report changes. Add a release here for parity with
the old fcli, and to `reads/` for fcling's own paths.

```bash
./tests/system/matrix.sh all                       # the 4l2s lab, every release
python -m tests.system.capture --release 26.7.2 \
    --node clab-3-stage-evpn-vxlan-nvd-25-leaf1=leaf \
    --node clab-3-stage-evpn-vxlan-nvd-25-spine1=spine
```

The 4l2s fabric is configured **once**, on the oldest release, by the lab's own
intent playbook. Every later release inherits that configuration through SR
Linux's own transformation: the configs are saved, the image is swapped, and
the lab is redeployed *without* `--reconfigure`. That is deliberate. The
reports have to work against the datamodel a real upgrade leaves behind, not
against a config regenerated for the new release. The NVD lab was upgraded from
25.10.4 to 26.7.2 the same way.

### Upgrading from 25.10.4 needs a hand

SR Linux's own transformation does not carry a 25.10.4 config to 26.x. The 26.x
rule that deletes
`bgp-evpn/.../routes/bridge-table/mac-ip/advertise-arp-nd-only-with-mac-table-entry`
fires only for a config whose `srl_nokia-bgp-evpn` revision predates
2026-03-31. But 25.10.4, a late maintenance release, ships revision 2026-06-18.
The rule is skipped, the leaf is unknown to 26.x, and every leaf that set it
boots with an empty config. Delete the leaf from the saved `config.json` before
redeploying, which is all the rule would have done. Also note that
`containerlab deploy --reconfigure` removes the lab directory, saved configs
included: redeploy without it.

## How the comparison is made

`test_release_matrix` compares only the columns the old table had. Columns
fcling added have nothing to compare with. Empty cells are dropped on both
sides, so a value present on one side only still fails. Some cells depend on
when they are read, and are left out: the `arp`/`nd` expiry countdowns and the
`ifstats` rates, whose sample interval was not recorded (`VOLATILE`). The old
tables gave an ARP cache, a bridge table or a route table without entries a row
of its own. Records are entries, so those rows are not expected
(`ENTRY_COLUMNS`). That is why `python -m tests.rowcounts` shows `ipv6-rib=0/3`
on the 4l2s nodes.

Only reports with an old counterpart that rendered the same columns are
compared (`PORTED`). `routing-pol`, `services`, `bridge-domains` and `routers`
are recorded in `releases/`, but the old getters built UI strings for them.
They are checked against `reads/` only. `config-commits` and the newer reports
are in `reads/` alone.

## What the datamodel did

Leaves added and removed under the 32 paths the original fcli read, comparing
each release to the one before it:

| Release | Added | Removed |
|---|---|---|
| 25.10.3 | 89 | 11 |
| 26.3.1 | 50 | 28 |
| 26.7.1 | 43 | 3 |

Despite that churn, every ported report builds identical rows on every recorded
release. The differences worth knowing about:

### `vxlan` lost its VNI from the state datastore in 25.10

Up to 25.3, the `state` datastore also carried the configured
`tunnel-interface/vxlan-interface/ingress/vni` and `type`. From 25.10.3 it does
not. A report reading it as `state` shows `-` in `ing-vni` on every newer
release, and still looks healthy. `vxlan` reads it with datatype `all`. The
recordings cover both sides of the change. This is the failure that
`test_no_read_went_silent` is there to catch on new releases.

### The `bgp-rib` l3vpn families exist only on an MPLS IP-VPN node

On every node but `dcgw1`, the l3vpn RIBs are refused outright:

```
Path not valid - unknown element 'l3vpn-ipv4-unicast'.
Options are [ipv4-unicast, ipv6-unicast, evpn, route-target]
```

That is a schema error, not absent data. The `bgp-rib` model only carries the
l3vpn containers on a node configured for MPLS IP-VPN. `dcgw1` has them, and
records 5 `l3vpn-ipv4-unicast` routes. Elsewhere the report degrades to an
empty table instead of failing. The options SR Linux lists also move: 26.3.1
adds the flowspec families, and 26.7.1 drops `afi-safi-name` from the list.

### LLDP starts running on `mgmt0` in 25.10.3

On 25.3.2, a 4l2s leaf reports two LLDP neighbours, its two spines. From 25.10.3
it reports seven, because `mgmt0` now runs LLDP too, and every other node sits
on the shared containerlab management bridge. Harmless, but it is why the
`lldp` row counts jump between the first two releases.

## Coverage gaps

- **No static routes on any recorded node.** `static-routes` reads are accepted
  everywhere, but the payload shape is not exercised (`SILENT`).
- **No IS-IS or OSPF in `reads/`.** Only DCI `dcgw1` has IS-IS, and only in
  `releases/`. No node runs OSPF.
- **No transceivers**: veth ports have no optics. The report is accepted and
  empty everywhere.
- **One release in `reads/`.** Until a second release of the NVD nodes is
  recorded, `SILENT` is a reviewed list rather than a comparison between
  releases.
- **IPv6 only on 26.3.1 DCI and the NVD lab.** The 4l2s fabric has none.
  Because of that, the matrix once missed a report taking its columns from the
  first item only. That emptied `ipv6-rib` on any fabric whose first
  network-instance or first node had no IPv6 routes.

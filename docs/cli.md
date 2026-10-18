# Reports from the command line

```bash
fcli [GLOBAL OPTIONS] <report> [REPORT OPTIONS]
```

Every report is a command. It asks all selected nodes at once over gNMI and prints one table. With `--server` it answers instead from what a running engine streams, without asking the nodes ([server.md](server.md)).

## Reports

| Report | Shows | Options |
|---|---|---|
| `sys-info` | chassis type, serial, MAC, software release | |
| `config-commits` | the commits each node logged to its configuration: who, when, which session | |
| `lldp` | LLDP neighbours per interface | |
| `subif` | subinterfaces: type, admin/oper state, the root cause when down, MTU, VLAN, addresses | |
| `lag` | LAGs with their LACP settings, one row per member | |
| `ifstats` | per-port rates over two samples, plus counters | `-s SECONDS` between samples (default 5) |
| `ni` | network-instances with their overlay, EVIs, route-targets and interfaces | |
| `bgp-peers` | BGP sessions with Rx/Act/Tx per address family | |
| `bgp-rib` | one BGP RIB | `-r` family, `-t` EVPN route type, `--paths`, `-d` |
| `ipv4-rib`, `ipv6-rib` | route tables, next-hops resolved down to ports or tunnels | `-a ADDRESS` longest-prefix match |
| `tunnel-table` | tunnels (VXLAN, LDP, SR-ISIS...) with next-hops, egress ports, labels | |
| `static-routes` | static routes and their next-hops | |
| `routing-pol` | routing policies statement by statement, and the sets they use | |
| `mac` | bridge tables | |
| `arp`, `nd` | IPv4 and IPv6 neighbour caches | |
| `irb` | IRB subinterfaces: addresses, anycast gateway, ARP/ND and EVPN advertisement | |
| `es` | ethernet-segments with their designated forwarders | |
| `es-dest` | remote ethernet-segments per vxlan-interface | |
| `vxlan` | vxlan-interfaces with their VNI and remote VTEPs | |
| `bfd` | BFD sessions: state, clients, failures, intervals | |
| `isis`, `ospf` | IGP interfaces and their adjacencies | |
| `resources` | CPU, memory and forwarding-table use | |
| `components` | cards, fabric modules, fans, power supplies | |
| `transceivers` | optics with light levels and DOM alarms | |

Every report shows only operational state, not configuration.

No report shows the management port `mgmt0` or its subinterfaces (`mgmt0.0`), or anything in the `mgmt` network-instance: its routes, BGP sessions, ARP entries and the instance itself. They carry no fabric traffic, and their counters, DHCP lease and the lab host's neighbours are noise. They are left out where reports build their records, so the history, checks, incidents, lenses, topology and the agent never see them either. The lists are `EXCLUDED_INTERFACES` and `EXCLUDED_INSTANCES` in `src/fcli/report.py`.

## Report options

Every report takes:

- **`-f COLUMN=REGEX`:** keep only the rows where that column matches, case-insensitive. Repeat it to AND several filters. A column is named by its title or by its record field; `node` works too.
- **`--at TIME`:** the report as it was then, from the history `fcling serve` keeps. See [history.md](history.md).

```bash
fcling bgp-peers -f state='^(?!up)'              # every session that is not up
fcling -i role=leaf mac -f NI=macvrf-1 -f mac=1A:2B
fcling ipv4-rib -a 10.1.3.99                     # the route each instance has to it
fcling ifstats -s 2 -f oper=up
```

### bgp-rib

```bash
fcling bgp-rib                       # EVPN route type 2
fcling bgp-rib -r evpn -t 5          # IP prefix routes
fcling bgp-rib -r ipv4 --paths all   # every path received, not only the used ones
fcling bgp-rib -t 2 -d               # with the detail columns: SoO, tunnel encap, D-PATH, tie-break...
```

`-r` takes `evpn`, `ipv4`, `ipv6`, `l3vpn-v4`, `l3vpn-v6` (or the YANG names). By default only the paths the node uses, or selected as best, are listed. A route reflector uses none of what it reflects, but still selects a best path, so its routes still appear.

## Output

`-o table` (the default) is meant for reading. The other formats are meant for scripts:

| Format | Content |
|---|---|
| `json`, `yaml` | the records: every field, typed and nested, with BGP's and YANG's own words (`established`, not `up`) |
| `csv` | the table's columns, one row per line |

```bash
fcling -o json bgp-peers | jq '.[] | select(.state != "established") | .peer'
```

`-b` picks the table border style (`MINIMAL`, `SIMPLE`, `ROUNDED`...).

## When a node fails

A node that can't be reached, or whose credentials are rejected, is reported on stderr with the reason, and the other nodes' rows are still printed. The exit status is then 1.

A path that a node's release or platform doesn't have reads as empty, not as an error: a fixed-form chassis has no fabric modules, and a spine has no EVPN instances.

## Other commands

| Command | See |
|---|---|
| `where`, `path`, `service`, `endpoints`, `topology` | [lenses.md](lenses.md) |
| `checks`, `incidents` | [checks.md](checks.md) |
| `serve`, `history`, `baseline`, `baselines`, `drift`, `config-diff` | [server.md](server.md), [history.md](history.md) |
| `ask`, `mcp` | [ai.md](ai.md) |

# History, baselines and drift

While `fcling serve` runs, every change to every report's records is kept: a BGP session going down, a MAC moving, a finding raised or cleared. That history answers what changed and when, what the fabric looked like at a given time, and how it differs from when it was known to be good.

## What is kept

A report's records are compared by their key (`ni` and `peer` for a BGP session, `name` for a subinterface). The engine diffs every run against the previous one, and each difference is one change:

| `op` | Meaning |
|---|---|
| `add` | a record appeared: a session was configured, a route learned, a finding raised |
| `remove` | a record went |
| `change` | a record's fields changed. Both versions are kept. |
| `update` | only volatile fields changed: streamed live, never kept |

**Volatile fields** move all the time and mean nothing on their own: counters, rates, a BGP family's route counts, an ARP entry's expiry, ND's reachable/stale cycling, an optic's light level. A change confined to them is not history. A BGP family going down is still a change; its received-route count ticking up is not.

The history lives in SQLite, by default at `~/.local/state/fcli/<fabric>.db`. It holds an append-only log of changes plus the current state. When the server restarts it compares the fabric with that state, so it only logs what changed while it was stopped, not the whole fabric again.

Nothing is kept for the management port `mgmt0`, its subinterfaces or the `mgmt` network-instance: no report has records for them ([cli.md](cli.md#reports)). A history written before they were left out gets one `remove` for each of their records when the server next starts, and none after that.

## Reading it

```bash
fcling history                          # the last hour
fcling history --since 2h -r bgp-peers -n leaf1
fcling history --since 1d -r checks     # findings raised and cleared
fcling history --follow                 # and keep printing as it happens (needs --server)
```

Times are `30m`, `2h`, `1d`, `1w` (that long ago), a time of day (`14:02`, the last time it was that), an ISO date (`2026-10-12T14:02`), or epoch seconds.

Without `--server`, `history` reads the local history database directly, so it works while no server is running.

### Which commit caused a change

Every change is checked against the configuration commits (`config-commits`) seen shortly before it. A change within two minutes after a commit is put down to that commit, if the commit was made on the change's own node or on a node at the far end of it:

| A change to | Its far end |
|---|---|
| a port (`subif`, `ifstats`, `lag` members, `lldp`, `isis`, `ospf`, `bfd`) | the node on the other end of the cable, from LLDP, or from a /31 or /127 both ends share |
| a session (`bgp-peers`, `bfd`) | the node that owns the peer address (a loopback or an interface address), or the node across the cable for a link-local peer |
| a neighbour by name (`lldp` system name, IS-IS hostname) | that node |
| a finding | the nodes of what the incidents anchor it to: a cable, a session, the underlay between two nodes |

So when a spine is reconfigured, the leaf's session to it dropping, the leaf's port going down and a finding about either are all put down to the spine's commit. When both ends committed, the later commit is the cause. Cables and addresses come from the fabric as the history has it now: a cable is the same cable a few minutes earlier.

```
time                 node   report     op      key                      what                        after
2026-10-13 14:02:31  leaf1  bgp-peers  change  ["default","10.0.0.2"]   state: established -> idle  commit 12 on spine1 by admin (mtu change)
```

Commits are matched by when the engine saw them, not by the node's clock, which need not agree with the server's. The node's clock decides one thing only. A commit that ended more than 15 minutes before the engine saw it was read from the commit log when the node was first synced, along with everything else, so it is not the cause of anything.

This is timing, not proof. Something unrelated within two minutes of a commit, at either end, is put down to it too. And a change two hops away, a leaf's EVPN routes after a policy change on a DCGW, is not.

### Configuration-related, or unplanned

Each change has an `origin`. It is `config` for a commit and for anything put down to one, findings raised or cleared included. It is `unplanned` for the rest: what the network did on its own. `commit` names the commit (`{"node", "id"}`) whose diff explains it. The web UI tags configuration-related changes **CONFIG** and filters on it.

## Configurations and their diffs

Each node's running configuration is kept as well. The engine reads it (a gNMI Get of `/`, datatype `config`) when a node comes live, and again two seconds after every commit it sees. A configuration is kept only when it differs from the one kept before, and is stored with the latest commit it includes.

Before anything is kept, every secret leaf (passwords and their hashes, keys, pre-shared and authentication keys, SNMP communities) is replaced by `<redacted 1a2b3c4d>`. That is a digest salted with a secret of this history alone, so a changed password still reads as changed, and nothing about it can be looked up.

A commit's diff compares the configuration kept after it with the one kept before, as `set / ...` lines in the order SR Linux's `info flat` prints them:

```bash
fcling config-diff leaf1            # the latest change
fcling config-diff leaf1 12         # what commit 12 did
fcling config-diff leaf1 12 --base 9
fcling config-diff leaf1 --list     # the configurations kept
```

The two configurations are shown side by side. A line only the newer one has is **added**, a line only the older one has is **removed**, and a leaf whose value changed is one **changed** row, with its old and new line next to each other. Three unchanged lines are kept around each change (`-C`), and the rest are folded. Commits made one after another can fall between two reads, and then one diff covers them all and names each one. The first configuration kept for a node has nothing to be compared with.

## The fabric at an instant

Every report, and `checks` and `incidents`, take `--at`:

```bash
fcling bgp-peers --at 14:02
fcling incidents --at 2h                # what was wrong two hours ago
```

The state at an instant is the current state with every later change undone, newest first. It is reconstructed exactly, not sampled, so any instant since the history began works.

## Baselines and drift

A baseline is a name for an instant. It's a pointer into the history, not a copy:

```bash
fcling baseline pre-mtu -m "before the MTU change"
fcling baseline yesterday --at 1d
fcling baselines
```

Drift is the difference between a baseline (or any time) and now, or another time given with `--to`:

```bash
fcling drift pre-mtu
fcling drift pre-mtu -r subif -r bgp-peers
fcling drift 14:00 --to 15:00
```

Drift compares the two end states, record by record, ignoring volatile fields. A session that went down and came back in between is no drift; `history` shows that it happened.

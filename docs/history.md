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

Every change is checked against the configuration commits (`config-commits`) of its node. A change within two minutes after a commit is attributed to that commit:

```
time                 node   report     op      key                      what                      after
2026-10-13 14:02:31  leaf1  bgp-peers  change  ["default","10.0.0.2"]   state: established -> idle  commit 12 by admin (mtu change)
```

Commits are matched by when the engine saw them, not by the node's clock, which need not agree with the server's.

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

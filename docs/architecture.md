# Architecture, and adding a report

This page is for working on fcli. It covers how the pieces fit together, and how to add a report.

## The shape of it

```
inventory ──► collect (one-shot) ──┐
          └─► engine (streaming) ──┼──► Tree per node ──► reports ──► records ──► render / API / tools
                                   │                         ▲
            history (SQLite) ◄─────┘   checks over all nodes ┘──► findings ──► incidents
```

| Module | Role |
|---|---|
| [gnmi/](../src/fcli/gnmi/) | an asyncio gNMI client on `grpc.aio` (stubs generated from the openconfig protos with `make proto`), and paths as values |
| [tree.py](../src/fcli/tree.py) | one node's state as plain JSON, which Get answers and subscription leaves merge into; YANG lists indexed by key |
| [report.py](../src/fcli/report.py) | what a report is: declared reads, a pure build function, typed records, columns |
| [reports/](../src/fcli/reports/) | the reports |
| [collect.py](../src/fcli/collect.py) | one-shot: every node at once, one Get per datatype |
| [engine.py](../src/fcli/engine.py) | streaming: the subscription plan, polling, follow reads, resync, re-running only touched reports |
| [delta.py](../src/fcli/delta.py), [store.py](../src/fcli/store.py) | changes by record key, and the SQLite history |
| [fabric.py](../src/fcli/fabric.py), [checks.py](../src/fcli/checks.py), [incidents.py](../src/fcli/incidents.py) | the fabric-wide view, checks over it, correlation |
| [lens.py](../src/fcli/lens.py), [lenses/](../src/fcli/lenses/), [topology.py](../src/fcli/topology.py) | fabric-wide questions with an argument, and the graph, over a shared `Index` ([lenses.md](lenses.md)) |
| [views.py](../src/fcli/views.py) | fabric-wide answers from any source: now, the engine, or an instant in the history |
| [service.py](../src/fcli/service.py) | the questions every surface asks; answered by the engine, a remote server, or the nodes one-shot |
| [tools.py](../src/fcli/tools.py), [mcp.py](../src/fcli/mcp.py), [agent.py](../src/fcli/agent.py), [llm.py](../src/fcli/llm.py) | one tool layer, served over MCP and to the agent's providers |
| [server.py](../src/fcli/server.py), [cli.py](../src/fcli/cli.py), [remote.py](../src/fcli/remote.py) | the surfaces |
| [ui/](../src/fcli/ui/) | the web UI: ES modules over the HTTP API, with Preact and htm vendored as one file; no build step |

Three principles hold throughout:

- **A report never does I/O.** It declares the paths it reads, and builds records from a tree. The same report therefore runs one-shot, streamed, and on recordings in the tests, and the engine knows what to subscribe to and which reports an update touches without running anything.
- **Records are flat, frozen pydantic models.** One record is one row. The model gives the JSON output, the schema that MCP clients and the API publish, and the key that history and drift compare by.
- **Every surface goes through one Service and one tool layer.** The CLI, the HTTP API, MCP and the agent can't see different fabrics.

## Adding a report

A report is a function, the record it builds, and what it reads:

```python
class Neighbor(Record):
    key: ClassVar[tuple[str, ...]] = ("interface", "chassis_id")   # what history matches rows on
    volatile: ClassVar[tuple[str, ...]] = ()                      # fields that are not history

    interface: str
    chassis_id: str = ""
    system_name: str = ""


@report(
    "lldp",
    "LLDP neighbors",
    record=Neighbor,
    reads=[Read("/system/lldp/interface[name=*]/neighbor")],
    columns=[Column("interface", "interface"), Column("Nbr-System", "system_name")],
)
def lldp(node: NodeState) -> Iterator[Neighbor]:
    for m in node.tree.select("/system/lldp/interface/neighbor"):
        yield Neighbor(
            node=node.name,
            interface=str(m.keys["interface"]),
            chassis_id=str(m.value.get("chassis-id") or ""),
            system_name=str(m.value.get("system-name") or ""),
        )
```

Import the module in [reports/\_\_init\_\_.py](../src/fcli/reports/__init__.py), and it becomes a CLI command, an API endpoint, a `get_report` name and a streamed report.

`tree.select` treats an element without keys as a wildcard. It returns each entry together with the keys on the way to it (`m.keys["interface"]`). Values arrive with YANG module prefixes already stripped, from member names and identity values alike.

### What a `Read` must get right

These are lessons from SR Linux:

- **Read the narrowest path.** A whole container streams everything in it. `/platform/control[slot=*]` includes CPU and memory, which would update every second; read the leaves you need instead. The same goes for `/network-instance[name=*]`, which includes every BGP RIB.
- **Datatype `all`, never `config` alone.** A Subscribe has no datatype, so it streams state and defaults too, and a `config` read would build different records live than one-shot. A Get with datatype `state` answers nothing for a config leaf, so read config leaves with `all`.
- **Counters use `mode=SubscriptionMode.SAMPLE`.** The engine then polls them instead of streaming. Mark them `volatile` in the record so they don't flood the history.
- **Sort anything that is a set.** List order can differ between a Get and a stream, and an order that differs is a change logged on every resync.
- **A path a node lacks is fine.** It reads as empty, both one-shot and streamed.

### Reports with options, rates or dependent reads

- **`params=[Param("address", "...", short="a")]`** gives the build function a keyword argument and the CLI an option. Parameters shape what the report builds, not what it reads.
- **`interval=5.0`** takes two samples: `node.previous` and `node.elapsed` are the earlier tree and the seconds between them (see `ifstats`).
- **`follow=fn(tree, params) -> reads`** fetches data the first reads point at, by key: the next-hop-groups the routes name, the attribute sets of the BGP paths. It is called again until it asks for nothing new. `routing.keyed()` switches to reading the whole table above 2000 keys (10 000 for BGP attribute sets).
- **`hidden=True`** keeps a report out of the CLI's command list. Each `bgp-rib` family is one, reached through `fcling bgp-rib -r -t`.

### Adding a check

```python
@check("bfd_down", "BFD sessions that are not up", ["bfd"])
def check_bfd_down(fabric: Fabric) -> Iterator[Finding]:
    for s in fabric.get("bfd"):
        if s.state not in ("up", "admin-down"):
            yield _f("bfd_down", ERROR, s.node, f"{s.ni}/{s.remote_address}", f"session is {s.state}")
```

The engine re-runs the checks whenever a report a check requires changes. To place the new check in incidents, add it to `ROOT_PRIORITY` and to the anchor sets in [incidents.py](../src/fcli/incidents.py).

## Tests

```bash
make test lint          # pytest; ruff and mypy
```

| | |
|---|---|
| [test_release_matrix.py](../tests/test_release_matrix.py) | Every ported report, replayed from the recordings of real fabrics on SR Linux 25.3 to 26.7, must produce row for row what the original, Nornir-based fcli rendered from the same Gets. Add a new report's recorded name to `PORTED`. `python -m tests.rowcounts` shows how many rows each comparison actually covers, so an empty one doesn't pass unnoticed. |
| [test_release_reads.py](../tests/test_release_reads.py) | The paths fcling itself sends. The old recordings must hold every path a ported report reads (the gaps are listed in `UNRECORDED`). [tests/fixtures/reads](../tests/fixtures/reads/) holds fcling's own exchange with live nodes, recorded by `python -m tests.capture -t lab.clab.yml --node leaf1 --node spine1 --prefix <lab>-`: each report's Gets, follow reads included, and the engine's Subscribes up to their sync. A refused element outside `REFUSED`, or a read that came back empty outside `SILENT`, is a schema change. Every report must replay what it built live, and must still read what was recorded: change a read, and record again. |
| [fake_gnmi.py](../tests/fake_gnmi.py) | an in-process gNMI target: Get, Subscribe with pushed updates and deletes, TLS, and refusals of unknown paths. It is what the client, engine, server and MCP tests run against. |
| [test_agent.py](../tests/test_agent.py) | the agent's loop against scripted stand-ins for each provider's API; no key needed, nothing spent |

The tests leave every model provider unconfigured, whatever the shell has set.

## Not done yet

- **Acknowledging incidents.**
- **More of the UI:** baselines and drift, and acknowledging incidents from it.

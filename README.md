# fcli

What an SR Linux fabric looks like, what is wrong with it, and what changed, straight from gNMI.

This is a ground-up rewrite of [fcli](https://github.com/srl-labs/fcli). It runs on asyncio throughout, uses no Nornir and makes no blocking gNMI calls.

```bash
uv sync
uv run fcli -t lab.clab.yml bgp-peers
uv run fcli -t lab.clab.yml -i role=leaf -o json subif -f oper=down
uv run fcli -c inventory.yml sys-info
uv run fcli -t lab.clab.yml ipv4-rib -a 10.1.3.99          # longest-prefix match
uv run fcli -t lab.clab.yml bgp-rib -r evpn -t 5 -d        # with the detail columns
uv run fcli -t lab.clab.yml ifstats -s 2 -f oper=up        # rates over 2 s
```

With the engine running, the same commands answer instantly from what it streams, and can look back:

```bash
uv sync --extra server
uv run fcli -t lab.clab.yml serve                          # streams every node, keeps history
export FCLI_SERVER=http://127.0.0.1:8765
uv run fcli bgp-peers                                      # live, from the engine
uv run fcli bgp-peers --at 14:02                           # as it was then
uv run fcli history --since 1h -r bgp-peers --follow       # what changed, which commit did it, and keeps watching
```

What is wrong, and what changed:

```bash
uv run fcli checks                          # every finding, check by check
uv run fcli incidents -v                    # grouped by root cause, worst first
uv run fcli incidents --at 14:02            # what was wrong then
uv run fcli baseline pre-change -m "before the MTU change"
uv run fcli drift pre-change                # what came, went and changed since
```

Ask, or let your own AI client ask. Both get the same 8 read-only tools: status, reports, incidents, checks, history, drift and baselines.

```bash
uv sync --extra server --extra mcp --extra ai
uv run fcli ask "what is wrong, and what changed just before?"        # the first provider with a key
uv run fcli ask --provider openai --effort low "is BGP healthy?"
claude mcp add --transport http fcli http://127.0.0.1:8765/mcp        # MCP over HTTP from `fcli serve`
claude mcp add fcli -- fcli --server http://127.0.0.1:8765 mcp        # or over stdio
```

Questions across the whole fabric, and its shape:

```bash
uv run fcli -t lab.clab.yml where 10.1.3.13                        # every place that knows an IP or MAC
uv run fcli -t lab.clab.yml path leaf1 10.1.3.13 --ni ipvrf-1      # hop by hop, over ECMP and VXLAN
uv run fcli -t lab.clab.yml service macvrf-102                     # one service on every node, and what disagrees
uv run fcli -t lab.clab.yml endpoints                              # every attached host
uv run fcli -t lab.clab.yml topology                               # tiers, cables, clients, segments
```

`checks` and `incidents` work without a server too: one-shot from the nodes, or from the local history with `--at`. They exit 1 when there is an error, so they can gate a pipeline.

Reports: `sys-info`, `config-commits`, `lldp`, `subif`, `lag`, `ifstats`, `ni`, `bgp-peers`, `bgp-rib`, `ipv4-rib`, `ipv6-rib`, `tunnel-table`, `static-routes`, `routing-pol`, `mac`, `arp`, `nd`, `irb`, `es`, `es-dest`, `vxlan`, `bfd`, `isis`, `ospf`, `resources`, `components`, `transceivers`, `services`, `bridge-domains`, `routers`. Each is a pure function from gNMI paths to typed records.

## Documentation

| | |
|---|---|
| [Installation and inventory](docs/installation.md) | installing, containerlab and static inventories, selecting nodes, TLS |
| [Reports](docs/cli.md) | every report, filters, output formats |
| [The engine](docs/server.md) | `fcli serve`: how it streams, the HTTP API and its events, security |
| [History](docs/history.md) | what is kept, `--at`, commit attribution, baselines and drift |
| [Lenses and topology](docs/lenses.md) | `where`, `path`, `service`, `endpoints`, `topology`, adding a lens |
| [Checks and incidents](docs/checks.md) | every check, and how findings are grouped by root cause |
| [MCP and the agent](docs/ai.md) | the tools, MCP setup, `fcli ask`, Claude, OpenAI and Grok |
| [Architecture](docs/architecture.md) | how it fits together, adding a report or a check, the tests |

## Architecture

```
inventory ──► collect ──► gNMI Get (one per datatype, all nodes at once)
                              │
                              ▼
                        Tree (per node)  ◄── later: gNMI Subscribe, same tree
                              │
                              ▼
            report: pure function Tree → typed records
                              │
                              ▼
                 render: table · json · yaml · csv
```

| Module | Role |
|---|---|
| [gnmi/client.py](src/fcli/gnmi/client.py) | `grpc.aio` client for Capabilities, Get and Subscribe. TLS can be verified, trust-on-first-use (`skip_verify`) or plaintext. Errors are worded around the node. |
| [gnmi/path.py](src/fcli/gnmi/path.py) | Paths as hashable values, parsed once. Module prefixes are dropped. |
| [tree.py](src/fcli/tree.py) | A node's state as plain JSON. Get payloads and subscription leaves merge into it, with list entries matched by key. |
| [report.py](src/fcli/report.py) | `@report(name, reads=[Read(path, datatype, mode)], record=..., columns=...)`. A report declares its paths and never does I/O. |
| `follow` | Optional pure function `(tree, params) -> reads` for dependent reads: the next-hop-groups the routes name, the BGP attribute sets of the paths shown. The collector calls it again until it asks for nothing new. It fetches by key, or the whole table above a limit. |
| `Param` | An argument that shapes what a report builds (`ipv4-rib -a <address>`, `bgp-rib --paths all`). The CLI turns each one into an option. |
| [reports/](src/fcli/reports/) | The report definitions. Records are flat, frozen pydantic models: one record is one row. |
| [collect.py](src/fcli/collect.py) | One-shot engine. Each node is one task, and a node fails on its own. A path a node does not know reads as empty. |
| [inventory.py](src/fcli/inventory.py) | Loads a containerlab topology or a static YAML inventory, with `key=glob` filters. |
| [engine.py](src/fcli/engine.py) | Long-running engine. ON_CHANGE reads are streamed in Subscribe RPCs of at most 36 paths, the most SR Linux accepts, each with a heartbeat. SAMPLE reads (counters) are polled, and each polled subtree is replaced whole: a SAMPLE stream never says what is gone. `follow` reads are fetched by key. A reconnect resyncs into a fresh tree. An update only re-runs the reports whose paths it overlaps. |
| [delta.py](src/fcli/delta.py) | Changes record by record, matched on each record's `key`. A change confined to `volatile` fields (counters, rates) is pushed live but not kept. |
| [store.py](src/fcli/store.py) | SQLite: an append-only change log plus the current state. A restart diffs against it, and the state at any instant is a fold back from now. |
| [checks.py](src/fcli/checks.py) | 19 fabric checks: pure functions of a `Fabric` (every node's records) to `Finding` records. In the engine, findings are a report like any other, so each one raised or cleared is history. |
| [incidents.py](src/fcli/incidents.py) | Findings anchored to what they are about (a cable, a session, a node, the underlay between two nodes) and grouped by root cause. The same cause in many places folds into one pattern. |
| [views.py](src/fcli/views.py) | One place for fabric-wide answers from any source (the nodes now, the engine, the history at an instant): checks, incidents, drift, and history attributed to the configuration commit that preceded it. |
| [service.py](src/fcli/service.py) | The questions every surface asks, behind one interface with three implementations: the engine in this process, a running server over HTTP, or the nodes one-shot. |
| [tools.py](src/fcli/tools.py) | 8 read-only tools over a Service. Inputs are pydantic models that are validated before anything runs; outputs are compact JSON with row limits. One definition serves both MCP clients and the agent. |
| [mcp.py](src/fcli/mcp.py) | The tools as an MCP server: streamable HTTP at `/mcp` on `fcli serve` (stateless, with DNS-rebinding protection on localhost), or stdio via `fcli mcp`. |
| [agent.py](src/fcli/agent.py) | The in-app agent, provider-neutral: streaming, concurrent tool calls, and append-only conversations kept server-side in the provider's own format. |
| [llm.py](src/fcli/llm.py) | The providers. **Claude** (Anthropic SDK): adaptive thinking, cached prompt and tools, server-side refusal fallback, `claude-opus-5-5` at `medium`. **OpenAI** (Responses API: `store` off, reasoning replayed encrypted), `gpt-5.6-sol`. `OPENAI_API=chat` switches to Chat Completions for proxies. **Grok** (Chat Completions at api.x.ai), `grok-4.6`. The provider is `FCLI_LLM_PROVIDER` or the first one with a key, Claude first; model and effort come from `FCLI_MODEL` / `FCLI_EFFORT` or each provider's own variables. |
| [server.py](src/fcli/server.py) | Starlette: `/api/status`, `/api/reports[/{name}]` (live, `?at=` or collected on the spot), `/api/history`, `/api/events` (SSE, resumable with `Last-Event-ID`). Bound to localhost; a bearer token when it isn't. |

Because a report only declares paths, the same definition can be fed by a one-shot Get (CLI), by a STREAM subscription (server) or by a recording (tests). Its subscription plan is static data that can be checked without a device.

### Static inventory

```yaml
defaults:
  username: admin
  password: NokiaSrl1!
  tls: {ca_file: ca.pem}      # or {skip_verify: true} / {insecure: true}
nodes:
  leaf1: {address: 10.0.0.11, labels: {role: leaf}}
  spine1: {address: 10.0.0.21, labels: {role: spine}}
```

## Tests

```bash
make test lint
```

[tests/fixtures/releases](tests/fixtures/releases/) holds real gNMI recordings from the original fcli for SR Linux 25.3 to 26.7. [test_release_matrix.py](tests/test_release_matrix.py) replays them through the new reports and requires row-for-row equality with what the original rendered. [fake_gnmi.py](tests/fake_gnmi.py) is an in-process gNMI target, so the client, TLS, the collector and the CLI are tested without a lab.

[test_engine.py](tests/test_engine.py) and [test_server.py](tests/test_server.py) stream from that same fake target. They cover the initial sync, refused paths, live changes and deletes, history and `state_at`, and SSE replay over a real uvicorn.

The gNMI stubs are generated from the openconfig protos (`make proto`) and committed.

## Roadmap

1. ~~Inventory, async gNMI client, node tree, recorded fixtures~~
2. ~~Declared-path reports with typed records, and a one-shot CLI~~. 27 reports are ported, and every one with recorded data matches the original row for row. `services`, `bridge-domains`, `routers` and `endpoints` move to phase 6: the old getters built display strings and web-UI tiles, and these should be typed records over `ni`, `irb`, `es`, `mac` and `arp`/`nd`.
3. ~~Long-running engine~~: subscriptions planned from the declared paths, record-level changes in SQLite, HTTP/SSE, plus `fcli serve`, `--server`, `--at` and `history`. On both labs the engine's live records match a one-shot run for every report.
4. ~~Checks, incident correlation, timeline and baselines~~. Findings live in the engine and are kept as history. Incidents carry "since". `--at` works for findings and incidents, baselines and drift work through the store, and history is attributed to commits. On both labs the engine's findings match a one-shot run.
5. ~~MCP served from the engine, and the in-app agent on the same tools~~: `/mcp`, `fcli mcp`, `/api/chat` and `fcli ask`. All of them use the same tools over one Service, with Claude, OpenAI or Grok as the model.
6. ~~Lenses and topology~~: `where`, `path`, `service`, `endpoints` and `topology`, from the nodes, the engine or the history. They are served over `/api/lenses` and `/api/topology`, and five more tools use them. `services`, `bridge-domains` and `routers` are typed reports. `endpoints` became a lens, because placing a host needs every node's tables.

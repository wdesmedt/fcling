# The engine: `fcling serve`

```bash
fcling -t lab.clab.yml serve                 # http://127.0.0.1:8765
```

Open `http://127.0.0.1:8765/` for the web UI: topology, incidents, history, lenses, reports and the agent ([ui.md](ui.md)).

`fcling serve` keeps every report live. It streams each node over gNMI, re-runs a report only when something it reads changes, and records every change to its records in a history. The CLI, MCP clients and the agent then answer from what it already holds, without asking the nodes.

| Option | Default | |
|---|---|---|
| `--host`, `--port` | `127.0.0.1`, `8765` | where to listen |
| `--db FILE` | `~/.local/state/fcli/<fabric>.db` | the history ([history.md](history.md)) |
| `--poll SECONDS` | 10 | how often counters are read |
| `-r REPORT` | all reports | stream only these reports; others are collected one-shot when asked |
| `--api-token` | none on localhost, else one kept in `<db>.token` | require this bearer token (`FCLI_API_TOKEN`) |
| `--persist-acks` | off | keep acknowledged incidents across restarts, in `<db>.acks.json` beside the history |
| `-d`, `--daemon` | off | detach from the terminal; log to `<db>.log`, pid in `<db>.pid`, beside the history |
| `--log-file FILE` | `<db>.log` | with `--daemon`: where the log goes |

**Acknowledging** an incident says it is known: it leaves the counts and moves to its own section in the UI, still shown. It is kept per finding, so a new finding joining the incident brings it back, and the acknowledgement ends when its finding clears (the same fault later is an alarm again). Without `--persist-acks` acknowledgements last until the server stops; with it, one whose finding cleared while the server was down ends on the first checks after the restart.

**In the background** (`--daemon`), the server runs in a session of its own, so closing the terminal does not stop it, and everything it would have printed - logging with timestamps, uvicorn's messages, a traceback - goes to the log file. The command returns once the server listens, with its pid; a server that fails to start (a port in use) is reported there, with the end of the log. A second `--daemon` on the same history is refused while the first runs. Stop it with `kill $(cat ~/.local/state/fcli/<fabric>.pid)`. Not on Windows, which has no `fork`.

^C, SIGTERM or SIGHUP stop it within a second, ending any open event streams and closing the history. Requests still running (an agent's answer, say) get 5 seconds to finish. A second signal ends the process within 3 seconds, whatever is still open.

## Using it from the CLI

```bash
export FCLI_SERVER=http://127.0.0.1:8765
fcling bgp-peers                       # live: answered at once, from the engine
fcling bgp-peers --at 14:02            # as it was then
fcling ipv4-rib -a 10.0.0.1            # a non-default parameter: collected on the spot by the server
fcling -i role=leaf incidents          # the inventory filter is applied by the server
```

Records come back with their full types, so `-o json` and `-f` work exactly as they do one-shot.

## How it streams

Each report declares the gNMI paths it reads. Those declarations are also the engine's subscription plan:

- **Paths that change rarely are streamed** as ON_CHANGE subscriptions. Paths that another path already covers are left out. The rest are split into Subscribe requests of at most 36 paths, the most SR Linux accepts in one request. Each request also samples a heartbeat leaf, so a node that has silently dropped off can be told apart from a quiet one; a stream silent for 35 s is reconnected.
- **Counters are polled** (`ifstats`, `bfd`, `resources`, `transceivers`) with a Get every `--poll` seconds. Each poll replaces the polled subtree whole: a SAMPLE subscription never reports a deletion, and a replaced subtree can't hold on to one.
- **Dependent data is fetched by key.** Route tables name next-hop-groups, and those name next-hops; BGP routes name attribute sets. The engine fetches exactly the entries the records refer to, and refreshes them every 30 s.

A node that rejects a path its release or platform doesn't have (`unknown element 'fabric'`) has that path dropped for this connection, and the rest is subscribed again at once.

On a reconnect the engine fills a fresh copy of the node's state and switches to it only after every subscription has synced and the first poll is in. A deletion made while the node was unreachable can't linger, and no report ever sees a half-filled state.

An update re-runs only the reports whose paths it touches. For example, a subinterface's oper-state touches `subif` and `ni`, and nothing else.

Each node uses about 2 gNMI sessions (SR Linux allows 20 per target, shared by all clients). Each client also runs at most 4 Gets at once.

## HTTP API

Every endpoint takes `inv=KEY=GLOB` and `node=NAME`, both repeatable, to narrow the nodes.

| | |
|---|---|
| `GET /api/status` | the fabric, its nodes with their state (`connecting`, `syncing`, `live`, `down` with the reason), the streamed reports, and the count of open incidents |
| `GET /api/reports` | every report: columns, parameters, key and volatile fields, record JSON schema |
| `GET /api/reports/{name}` | its records. Live; `?at=TIME` from history; with report parameters (`?address=10.0.0.1`), collected on the spot. `source` says which of the three it was. `?table=1` adds `columns` and `rows`: the CLI's table, as text. |
| `GET /api/checks` | findings, with when each was raised (`?at=`, `?check=`) |
| `GET /api/incidents` | findings grouped by root cause (`?at=`); live, each says whether it is `acknowledged` |
| `GET/POST/DELETE /api/acks` | the acknowledged findings; `POST {"incident": id, "note": ""}` acknowledges an open incident, `DELETE ?incident=id` takes it off |
| `GET /api/history` | changes, oldest first, each with its likely cause and `origin` (`config` or `unplanned`) (`since`, `until`, `report`, `limit`, `after`) |
| `GET /api/lenses` | every lens, with its arguments, columns and record JSON schema |
| `GET /api/lenses/{name}` | a lens's records: its arguments as query parameters (`?address=10.1.3.13`), `?at=` from history ([lenses.md](lenses.md)); `?table=1` as for reports |
| `GET /api/topology` | the fabric as a graph: nodes with tier and health, links, clients, segments, components (`?at=`) |
| `GET /api/configs` | the configurations kept, newest first (`node`) |
| `GET /api/configs/diff` | what a commit did to a node's configuration, side by side (`node`, `commit`, `base`, `context`; `seen`, when the commit was seen, tells it from one of the same id before the node was redeployed; or `at`, when the commit was made, in ISO 8601 or epoch seconds: the first configuration kept from then on against the last one before) |
| `GET /api/drift` | how the fabric differs from a baseline or time (`from`, `to`, `report`) |
| `GET/POST /api/baselines`, `DELETE /api/baselines/{name}` | named points in time |
| `GET /api/events` | server-sent events (below) |
| `POST /api/chat` | ask the agent ([ai.md](ai.md)) |
| `GET /` | the web UI ([ui.md](ui.md)); its files under `/ui/` need no token |
| `/mcp` | the tools for MCP clients ([ai.md](ai.md)) |

Errors are JSON, `{"error": "..."}`, with status 400 or 404. An `at` before the history starts is a 400 that says when it starts.

### Events

`GET /api/events` is a server-sent event stream. It opens with one `status` event (every node's state), followed by:

- **`node`:** a node's state changed.
- **`config`:** a node's configuration was kept after a commit (`node`, `commit`, `ts`).
- **`change`:** a record was added, removed or changed. `op` is `add`, `remove`, `change`, or `update` (volatile fields only; see [history.md](history.md)). Every change except `update` has a sequence number as its event id.

Filter with `report=` and `node=`. A client that reconnects with `Last-Event-ID` (or `after=SEQ`) first receives the changes it missed, replayed from the history. A client that falls more than 10,000 events behind is disconnected, and catches up the same way when it reconnects.

```bash
curl -N 'http://127.0.0.1:8765/api/events?report=bgp-peers'
```

## Security

The server listens on localhost by default. When `--host` is anything else, every request needs a token: `Authorization: Bearer <token>`, `?token=`, or the cookie the UI sets.

Without `--api-token`, the server makes one up the first time and keeps it in `<db>.token`, next to the history, readable by its owner only. Later starts reuse it, so a restart doesn't log out browsers or break clients. Delete the file to get a new one. A token passed with `--api-token` is used as given and never written to disk.

The banner prints the URL to log in with, token included, using the machine's name when listening on every address:

```
lab: 6 nodes, 30 reports, history in /home/me/.local/state/fcli/lab.db; UI at http://labhost:8765/?token=Xq…, API at /api/status, MCP at /mcp; token kept in /home/me/.local/state/fcli/lab.token
```

With `--daemon`, the same line is in the log. The CLI accepts that URL as it is: `--server 'http://labhost:8765/?token=Xq…'` (or `FCLI_SERVER`) takes the token from it, so `--token` / `FCLI_TOKEN` isn't needed. When the CLI runs on the server's own machine, it doesn't need the token at all: given a local `--server` and no token, it reads `<fabric>.token` from the state directory, or the only `*.token` file there if it can't tell the fabric. A server with `--db` elsewhere is not found this way. The MCP endpoint additionally refuses requests whose `Host` or `Origin` isn't localhost when the server is local, which blocks DNS rebinding from a browser.

Everything the server does over gNMI is read-only.

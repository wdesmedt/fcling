# The web UI

```bash
fcling -t lab.clab.yml serve            # then open http://127.0.0.1:8765/
```

`fcling serve` also serves a web UI at `/`. It reads the same HTTP API as every other client, so it shows the same fabric as the CLI, MCP and the agent. It is plain HTML and JavaScript modules shipped inside the package, with no build step and nothing fetched from the internet.

## Navigation

The panel on the left holds every view:

| | |
|---|---|
| **Topology**, **Incidents**, **History** | the fabric, what is wrong with it, and what changed |
| **Lenses** | **Where**, **Path**, **Service**, **Endpoints** |
| **Reports** | every report, grouped (platform, interfaces, routing, BGP RIB, services), with a search box |
| **Ask the agent** | opens the agent beside the current view |

☰ in the header collapses the panel to icons. On a narrow screen, it slides the panel over the page. Every view and its arguments are in the URL (`#/lens/path?source=leaf1&destination=10.1.3.13`), so you can bookmark or share a link to it.

## Topology

The fabric is drawn by tier: the core on top, then gateways, spines and leaves, and the clients below the leaves they hang off. A multihomed client sits under the middle of its leaves. Each node's box gives its name, its device type (from `sys-info`), and its system0 address, IPv4 and IPv6 where it has one. Each node carries a colour bar for its health, and red and amber badges count its errors and warnings. An unreachable node has a dashed grey outline. Cables are coloured by state: amber for mixed, red for down, and dashed red for a cable the history saw that LLDP no longer reports. `×N` marks parallel cables.

**Show** (top left) picks what the drawing is coloured by:

| | |
|---|---|
| **health** | the default: nodes and cables by their findings and state |
| **traffic** | each half of a cable by what its end sends, and each client's cable by what its leaf sends it: green under 25% of the **link rate**, yellow to 50%, orange to 75%, red above. A dotted half has no rate yet: a rate needs two samples. Hover a cable for both directions. |
| **service** | one mac-vrf or ip-vrf: the nodes that carry it, the cables between them, and its clients. Everything else dims. |

The link rate is the rate a cable counts as full at (default 10 Mbps). Emulated nodes forward far below their port speed, so it is a scale you set for the colours, not read from the ports. The browser remembers the overlay and the rate. While the drawing shows traffic, the topology is fetched every 10 seconds: rates change with every poll but are not history, so no event announces them. For the same reason, an instant in the past shows the rates as they were last kept, not as they were then.

Click a node, a cable or a client, and the panel on the right shows:

- **Node:** tier, stream state, labels, platform and software, findings, its cables and clients. Info findings are folded away behind one line.
- **Cable:** each cable's ports, state and rate in each direction, and the findings on those ports.
- **Client:** its segment, and every attachment's node, subinterface, service, VLAN, and rates to and from it.

Everything else dims, so you can see what the selection touches. Drag to pan, scroll to zoom, and press **Fit** to see the whole fabric.

## Incidents

The incidents tab lists the open incidents worst first, as `fcling incidents` does. Open one to see its explanation, its root finding and its consequences, and **what changed just before**. That is the history of the incident's nodes in the 15 minutes before it started, with the configuration commit each change is attributed to. Info incidents are folded away at the bottom, and a node chip opens that node on the topology. **Ack** marks an incident as known: it leaves the counts (here, in the navigation and on the Nodes page) and folds into an Acknowledged section, until a finding joins it or it clears; **Unack** brings it back. Acknowledgements are shared by everyone using the server, and survive a restart with `fcling serve --persist-acks`.

## History

A timeline of every change the engine kept, in the last 15 minutes up to 7 days. It is coloured by kind: findings raised and cleared, configuration commits, and other changes. Bars grow with the square root of their count, so the burst of the engine's first sync doesn't flatten the rest. Click a bar to list only its changes. Narrow the history to one report (or `findings`, or `config-commits`) and one node. Each change shows the fields that moved and the commit likely to have caused it. The view refreshes as changes arrive.

## Lenses

- **Where** finds an IP or MAC everywhere it is known ([lenses.md](lenses.md)). The nodes that hold it (configured, learned locally, or bound by ARP/ND) are picked out on the topology, marked with how they hold it. Above the table, the UI says whether a MAC on several leaves is multihomed or a duplicate.
- **Path** traces from a node, or from a host's IP or MAC, to an address, and draws the result on the topology. The cables it forwards over are highlighted, VXLAN tunnels are dashed arcs with their VNI, and each node shows its hop numbers. The source and destination clients are highlighted too. Above it, where the trace ends: delivered (on which port, to which MAC) or why not.
- **Service** shows one mac-vrf or ip-vrf on every node that carries it, and whether they agree. With no name given, it lists every service.
- **Endpoints** lists every attached host, with a filter that stays in the URL.

## Reports

Any report as a table, with the same columns as the CLI (the server renders them, `?table=1`). Filter every row with words that must all match, or one column with its own box under the header:

| | |
|---|---|
| `up` | the cell contains it |
| `!fe80` | the cell does not contain it |
| `=up` | the cell is exactly it |
| `>10`, `<=5` | the cell's number is more or less (a cell without one never matches) |

Case doesn't matter, and the filters combine. On a report they are kept in the URL (`#/report/bgp-peers?f.state=!up`), so a filtered table can be bookmarked or shared. Lens tables have the same filters. Click a column to sort, and click a node to open it on the topology. A streamed report refreshes about two seconds after the engine says it changed. A report with parameters (`ipv4-rib` by address) has a form, and is then collected on the spot. The pill next to the title says where the rows came from: `live`, `history` or `collected`. Nodes that could not answer are listed above the table. A BGP RIB is never read on opening, since one peer can send tens of thousands of routes: a link (a bgp-peers Rx/Act/Tx count, say) fills in its form, and **Look up** or **List all routes** reads it.

## Ask the agent

**✦ Ask** opens the agent beside any view ([ai.md](ai.md)). It is told what you are looking at, such as `topology, node leaf3 selected`, `path lens source=leaf1 …` or the time you went back to, so "why is this down?" means what is on the screen. You see its tool calls as they run, then its answer, and the tokens it used. **New** starts a new conversation and **Stop** cancels one. Each question is sent to the model provider the server is configured with, and is billed to that key.

## Live, or as it was

While live, the UI follows `/api/events` and refreshes about a second and a half after changes settle. The dot in the header shows the event stream: green for live, amber while reconnecting. Type a time in **Go back** (`30m`, `2h`, `14:02`) to show the topology, the incidents, the lenses and the reports as the history has them at that instant. Following changes pauses until **Back to live**. The history view always runs up to now.

## With a token

When the server requires a token (`--api-token`), open the UI once as `http://host:8765/?token=...`. It keeps the token for the browser tab and removes it from the address bar. The UI's own files are served without the token because they hold no data. Every API call still needs it.

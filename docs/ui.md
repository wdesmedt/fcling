# The web UI

```bash
fcli -t lab.clab.yml serve            # then open http://127.0.0.1:8765/
```

`fcli serve` also serves a web UI at `/`. It reads the same HTTP API as every other client, so it shows the same fabric as the CLI, MCP and the agent. It is plain HTML and JavaScript modules shipped inside the package, with no build step and nothing fetched from the internet.

## Topology

The fabric is drawn by tier: spines above leaves, gateways and core above them, and the clients below the leaves they hang off. A multihomed client sits under the middle of its leaves. Each node carries a colour bar for its health, and red and amber badges count its errors and warnings. An unreachable node has a dashed grey outline. Cables are coloured by state: amber for mixed, red for down, and dashed red for a cable the history saw that LLDP no longer reports. `×N` marks parallel cables.

Click a node, a cable or a client, and the panel on the right shows:

- **Node:** tier, stream state, labels, platform and software, findings, its cables and clients.
- **Cable:** each cable's ports and state, and the findings on those ports.
- **Client:** its segment, and every attachment's node, subinterface, service and VLAN.

Everything else dims, so you can see what the selection touches. Drag to pan, scroll to zoom, and press **Fit** to see the whole fabric. The selection is part of the URL (`#/topology/node/leaf1`), so you can share a link to it.

## Incidents

The incidents tab lists the open incidents worst first, as `fcli incidents` does. Open one to see its explanation, its root finding and its consequences, and **what changed just before**. That is the history of the incident's nodes in the 15 minutes before it started, with the configuration commit each change is attributed to. Info incidents are folded away at the bottom, and a node chip opens that node on the topology.

## Live, or as it was

While live, the UI follows `/api/events` and refreshes about a second and a half after changes settle. The dot in the header shows the event stream: green for live, amber while reconnecting. Type a time in **Go back** (`30m`, `2h`, `14:02`) to show both views as the history has them at that instant. Following changes pauses until **Back to live**.

## With a token

When the server requires a token (`--api-token`), open the UI once as `http://host:8765/?token=...`. It keeps the token for the browser tab and removes it from the address bar. The UI's own files are served without the token because they hold no data. Every API call still needs it.

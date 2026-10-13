# Installation and inventory

## Install

fcli needs Python 3.11 or later. It is published on PyPI as `fcling` and installs a command named `fcli`:

```bash
uv tool install 'fcling[server,mcp,ai]'    # or: pipx install 'fcling[server,mcp,ai]'
```

From a checkout, with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install '.[server,mcp,ai]'         # an `fcli` command on your PATH
uv sync --all-extras && uv run fcli ...    # or run it from the checkout
```

The original, Nornir-based fcli installs a command named `fcli` too. Installing both as tools makes them clash: `uv tool uninstall nornir-srl` first, or use `uv run fcli` from the checkout.

### Container image

Each release is also an image with every extra, `ghcr.io/wdesmedt/fcling`. Its entrypoint is `fcli`, it works in `/work` and keeps its history in the `/state` volume:

```bash
docker run --rm --network host -v "$PWD:/work" ghcr.io/wdesmedt/fcling -t lab.clab.yml sys-info
docker run -d --name fcli --network host -v "$PWD:/work" -v fcli-state:/state \
    ghcr.io/wdesmedt/fcling -t lab.clab.yml serve
```

`--network host` lets it reach containerlab nodes by their container names and keeps `fcli serve` on localhost. Without it, publish the port and listen on every address: `-p 8765:8765 ... serve --host 0.0.0.0`. Beyond localhost every call needs a token; fcli makes one up and prints it, or takes yours with `--api-token`.

The core package is the one-shot CLI. The extras add the rest:

| Extra | Adds | Needed for |
|---|---|---|
| `server` | Starlette, uvicorn | `fcli serve`: the engine, its history, the HTTP API |
| `mcp` | the MCP SDK | `fcli mcp`, and `/mcp` on `fcli serve` |
| `ai` | the Anthropic and OpenAI SDKs | `fcli ask`, and `/api/chat` on `fcli serve` |

A command that needs an extra you don't have says which one to install.

## Point fcli at a fabric

Every command needs to know which nodes to talk to. There are three ways.

### A containerlab topology

```bash
fcli -t lab.clab.yml sys-info
```

fcli reads the topology's SR Linux nodes: kind `srl` or `nokia_srlinux`, or any kind whose image is SR Linux, inherited from groups and defaults as containerlab does. It reaches each one by its container name (`clab-<lab>-<node>`, following the topology's `prefix`). Labels come from the node, its group and the topology defaults, and the credentials are SR Linux's defaults.

If you run fcli from a directory that holds exactly one `*.clab.yml`, `-t` can be left out.

### A static inventory

For anything that isn't a containerlab lab:

```yaml
# inventory.yml
name: dc1                       # optional; defaults to the file's directory name
defaults:
  username: admin
  password: NokiaSrl1!
  port: 57400
  tls: {ca_file: ca.pem}        # see TLS below
  labels: {site: dc1}
nodes:
  leaf1: {address: 10.0.0.11, labels: {role: leaf}}
  leaf2: {address: 10.0.0.12, labels: {role: leaf}}
  spine1: {address: 10.0.0.21, labels: {role: spine}}
```

```bash
fcli -c inventory.yml bgp-peers
```

A node without an `address` is reached by its name. An unknown setting is an error, so a typo doesn't go unnoticed.

### A running server

With `--server` (or `FCLI_SERVER`) the inventory is the server's, and commands answer from what it streams. See [server.md](server.md).

## Selecting nodes

`-i KEY=GLOB` selects nodes by label, or by `name`. Repeat it to AND several filters. Matching is case-insensitive.

```bash
fcli -t lab.clab.yml -i role=leaf bgp-peers
fcli -t lab.clab.yml -i 'name=leaf[12]' -i site=dc1 subif
```

A filter that matches no node is an error that lists the labels that do exist.

## Credentials and TLS

Every node uses the inventory's credentials. The global options override them for all nodes: `-u/--username`, `-P/--password`, `-p/--gnmi-port`.

| Mode | How | When |
|---|---|---|
| Trust on first use (default) | nothing | containerlab and labs: the node's self-signed certificate is fetched and trusted as it is. |
| Verified | `--cert-file ca.pem` (or `tls: {ca_file: ...}`) | production: the certificate must chain to that CA. Add `--tls-server-name` when the certificate is issued to a name other than the address. |
| Verified, system roots | `--verify` | certificates from a CA the system already trusts |
| Plaintext | `--insecure` | a node with an insecure gNMI server |

`--skip-verify` forces trust on first use even with a CA file configured.

## Environment variables

Every global option can also come from the environment as `FCLI_<OPTION>`: `FCLI_TOPO`, `FCLI_INVENTORY`, `FCLI_SERVER`, `FCLI_TOKEN`, `FCLI_OUTPUT` and so on. The model providers have their own variables, listed in [ai.md](ai.md).

## SR Linux releases

The reports are tested against real recordings from SR Linux 25.3 through 26.7 ([tests/fixtures/releases](../tests/fixtures/releases/MATRIX.md)). Paths that only older releases used (BGP RIB layouts before 24.10) are not supported.

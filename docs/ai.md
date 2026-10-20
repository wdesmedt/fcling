# MCP and the agent

fcli offers its fabric to language models in two ways: as MCP tools for an AI client you already use, and as a built-in agent. Both use the same fourteen tools, so both see the same fabric.

## The tools

All of them are read-only.

| Tool | |
|---|---|
| `fabric_status` | the nodes with their labels and reachability, the live reports, the open incidents |
| `list_reports` | every report, with its columns and parameters |
| `get_report` | one report, narrowed by `nodes`, `inv` (labels) and `where` (`column=regex`). `params` takes report parameters; `detail` returns full records instead of table rows; `limit` (default 100) caps the rows and says how many more there were. |
| `get_incidents` | findings grouped by root cause: where triage starts |
| `get_checks` | every finding, ungrouped, with when it was raised |
| `get_history` | what changed, with the commit likely to have caused it and whether it was configuration-related or unplanned (`origin`) |
| `get_config_diff` | what a commit changed in a node's configuration, as `set /` lines added, removed and changed; secrets as digests. By commit id, or by `at`, when the commit was made, since ids repeat |
| `get_drift` | how the fabric differs from a baseline or time |
| `list_baselines` | the named baselines |
| `locate_address` | every place that knows an IP or MAC: the `where` lens ([lenses.md](lenses.md)) |
| `trace_path` | how traffic from a node or host would be forwarded to an address, hop by hop: `path` |
| `service_detail` | one mac-vrf or ip-vrf on every node, with what disagrees: `service` |
| `list_endpoints` | the attached hosts, narrowed by `nodes` and `where`: `endpoints` |
| `get_topology` | tiers, cables and their state, clients, segments: `topology`, compacted |

Every tool takes `at` (`30m`, `2h`, a time) to answer as the fabric was then. Arguments are validated before anything runs, and an invalid one returns an error that says what was wrong.

Where the answers come from:

| Running as | Answers from | History, `at`, drift |
|---|---|---|
| `fcling serve` (`/mcp`, `/api/chat`) | the engine's live state | yes |
| `fcling --server URL mcp` / `ask` | that server, over HTTP | yes |
| `fcling -t lab.clab.yml mcp` / `ask` | the nodes, one-shot per call | no: those tools say so |

## MCP

An agent pointed at a running `fcling serve` should start with its MCP endpoint, `/mcp`: the tools describe themselves, with their arguments' JSON schemas. One that can only make HTTP requests finds the same answers in the plain API, which describes itself at `/api/openapi.json` (OpenAPI 3.1; `GET /api` points at both, and neither needs a token) ([server.md](server.md#http-api)).

From a running `fcling serve`, over HTTP:

```bash
claude mcp add --transport http fcli http://127.0.0.1:8765/mcp
```

Or over stdio, for clients that start the server themselves:

```bash
claude mcp add fcli -- fcling --server http://127.0.0.1:8765 mcp      # through a running server
claude mcp add fcli -- fcling -t /path/to/lab.clab.yml mcp            # straight from the nodes
```

Other MCP clients take the same URL, or the same command, in their own configuration. The HTTP endpoint is stateless, so a server restart breaks no client session. A server that requires a token (beyond localhost, or with `--api-token`) needs the client to send `Authorization: Bearer <token>`. `fcling --server` takes the token from the URL the server prints, or reads it on the server's own machine ([server.md](server.md#security)).

## The agent

```bash
fcling ask "what is wrong, and what changed just before it?"
fcling ask                                   # keep asking, in a loop
fcling --server http://127.0.0.1:8765 ask "is leaf3 healthy?"
fcling --server http://127.0.0.1:8765 ask -C 3f2a9c1b7d4e "and leaf4?"   # continue a conversation
```

The answer streams to stdout as it is written. The tool calls it makes are shown on stderr, followed by the token counts. Through a server the agent runs inside it, using the live state and the history, and the server keeps the conversation: `ask` prints the id to continue it with.

Its instructions follow the triage order: incidents first, then the history around when they started, naming the commit and user behind a change; then reports narrowed to the nodes in question. It cannot change anything, and is told never to imply that it did.

### Providers

| Provider | How it is called | Default model | Key |
|---|---|---|---|
| Claude | Anthropic Messages API: adaptive thinking, cached system prompt and tools, refusals retried server-side on another model | `claude-opus-5-5` | `ANTHROPIC_API_KEY`, or `ant auth login` |
| OpenAI | Responses API, with `store` off (the fabric's data is not kept by OpenAI) and reasoning replayed encrypted | `gpt-5.6-sol` | `OPENAI_API_KEY` |
| Grok | Chat Completions at `https://api.x.ai/v1` | `grok-4.6` | `XAI_API_KEY` |

The provider is `--provider` / `FCLI_LLM_PROVIDER`; otherwise it is the first with a key, in the order Claude, OpenAI, Grok. Setting a key is enough to start spending on that provider's account.

| Setting | Variables |
|---|---|
| model | `--model` / `FCLI_MODEL`, or `ANTHROPIC_MODEL`, `OPENAI_MODEL`, `XAI_MODEL` |
| effort | `--effort` / `FCLI_EFFORT`, or `ANTHROPIC_EFFORT`, `OPENAI_REASONING_EFFORT`, `XAI_REASONING_EFFORT`. Claude defaults to `medium`; the others to the model's own default. Each provider accepts its own levels. |
| endpoint | `OPENAI_BASE_URL`, `XAI_BASE_URL` |
| API | `OPENAI_API=chat` for a proxy in front of OpenAI that only speaks Chat Completions |

`fcling serve` uses whatever these variables say in its own environment.

### Limits

A turn may take at most 12 rounds of tool calls. After that, the model has to answer with what it has gathered. Tool calls within a round run concurrently. A refusal, an answer cut off at its length limit, or an API error ends the turn with a message, and the conversation stays usable for the next question.

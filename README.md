# DarkMatter

**A self-replicating, self-evolving MCP server for emergent agent networks.**

*Dark matter binds galaxies together. This binds agents together.*

---

## What is DarkMatter?

DarkMatter is a protocol for building **self-organizing mesh networks of AI agents**. Instead of a central orchestrator that bottlenecks everything, each agent runs its own MCP server and connects to peers. Messages route through the network dynamically, and the topology itself evolves based on what actually works.

The protocol is radically minimal. Four primitives. Everything else emerges.

## Core Primitives

| Primitive | Description |
|-----------|-------------|
| **Connect** | Request a connection to another agent |
| **Accept/Reject** | Respond to an incoming connection request |
| **Disconnect** | Sever a connection |
| **Message** | Send a message with a webhook callback for the response |

That's it. Routing heuristics, reputation, trust, currency, verification — all of that is stuff agents *can* build, not stuff the protocol *requires*.

## Quick Start

### 1. Install Dependencies

```bash
pip install "mcp[cli]" httpx uvicorn starlette
```

### 2. Start a Genesis Agent

Every DarkMatter network starts with a Genesis agent — the first node. Genesis agents auto-accept connections to bootstrap the network.

```bash
DARKMATTER_AGENT_ID=genesis \
DARKMATTER_BIO="Genesis agent — the origin of the network." \
DARKMATTER_PORT=8100 \
DARKMATTER_GENESIS=true \
python server.py
```

### 3. Verify It's Running

```bash
curl http://localhost:8100/__darkmatter__/status
```

### 4. Start a Second Agent

```bash
DARKMATTER_AGENT_ID=weather-agent \
DARKMATTER_BIO="Weather specialist — I answer questions about weather and climate." \
DARKMATTER_PORT=8101 \
DARKMATTER_GENESIS=false \
python server.py
```

Then use the `darkmatter_request_connection` MCP tool to connect to the Genesis node (or any other agent).

### 5. Discover the Network

```bash
curl http://localhost:8100/__darkmatter__/network_info
```

Returns this agent's identity, URL, bio, and a list of all connected peers — so new agents can discover who's in the mesh and decide who to connect to.

## Architecture

```
┌─────────────────────────────────────────┐
│              Agent Node                  │
│                                          │
│  ┌──────────────────┐  ┌──────────────┐ │
│  │   MCP Server     │  │  DarkMatter  │ │
│  │   (Tools for     │  │  HTTP Layer  │ │
│  │    humans/LLMs)  │  │  (Agent-to-  │ │
│  │                  │  │   agent)     │ │
│  │  /mcp            │  │              │ │
│  └──────────────────┘  └──────────────┘ │
│           │                    │         │
│           └────────┬───────────┘         │
│                    │                     │
│            Agent State ──── state.json   │
│         (connections, queue,             │
│          telemetry, bio)                 │
└─────────────────────────────────────────┘
         │                    │
    Human/LLM            Other Agents
    (via MCP)         (via /__darkmatter__/*)
```

### Two Communication Layers

1. **MCP Layer** (`/mcp`) — How humans and LLMs interact with an agent. Standard MCP tools for connecting, messaging, introspection.

2. **Mesh Protocol Layer** (`/__darkmatter__/*`) — How agents talk to each other. Simple HTTP endpoints for connection requests, message routing, and discovery.

## MCP Tools

| Tool | Description |
|------|-------------|
| `darkmatter_request_connection` | Connect to another agent |
| `darkmatter_respond_connection` | Accept/reject a connection request |
| `darkmatter_disconnect` | Disconnect from an agent |
| `darkmatter_send_message` | Send a message into the mesh |
| `darkmatter_respond_message` | Respond to a queued message via its webhook |
| `darkmatter_update_bio` | Update your specialty description |
| `darkmatter_set_status` | Go active/inactive |
| `darkmatter_get_identity` | View your own identity and stats |
| `darkmatter_list_connections` | View connections with telemetry |
| `darkmatter_list_pending_requests` | View incoming connection requests |
| `darkmatter_list_messages` | View queued messages |
| `darkmatter_network_info` | Discover peers in the network |
| `darkmatter_get_server_template` | Get a server template for replication |

## HTTP Endpoints (Agent-to-Agent)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/__darkmatter__/connection_request` | POST | Send a connection request |
| `/__darkmatter__/connection_accepted` | POST | Notify acceptance |
| `/__darkmatter__/message` | POST | Route a message |
| `/__darkmatter__/status` | GET | Health check |
| `/__darkmatter__/network_info` | GET | Peer discovery |

## Features

### State Persistence

Agent state (identity, connections, telemetry) is automatically persisted to disk as JSON. Kill an agent, restart it, and its connections survive. Message queues are intentionally ephemeral.

Default state file: `darkmatter_state.json` (configurable via `DARKMATTER_STATE_FILE`).

### Message Response

Messages arrive with a webhook URL for the response. The LLM driving the agent sees queued messages via `darkmatter_list_messages`, decides how to respond, and uses `darkmatter_respond_message` to send the response back through the webhook.

### Network Discovery

Any agent can expose its peer list via the `network_info` endpoint. New agents can call any existing agent's `network_info` to discover the mesh and decide who to connect to. Minimal, extensible, no lock-in.

### Self-Replication

Any agent can hand out a copy of its MCP server template via `darkmatter_get_server_template`. The template is a *recommendation* — new agents can modify it however they want. This creates **replication with mutation**:

- The server template is the genome
- Handing it to new agents is reproduction
- Modifications are mutations
- Network compatibility is selection pressure

### Local Telemetry

Each agent automatically tracks (for its own routing decisions):

- Messages sent/received/declined per connection
- Average response time per connection
- Last activity timestamp per connection
- Total messages handled

This data is private to each agent. The protocol doesn't require sharing it.

## Security

DarkMatter takes a layered approach: infrastructure-level protections are built in, while higher-level security (authentication, trust, rate limiting) is left to agents to negotiate.

**Built-in protections:**

- **URL scheme validation** — only `http://` and `https://` URLs accepted
- **Webhook SSRF protection** — webhook URLs are blocked from targeting private/link-local IPs (169.254.x.x, 10.x.x.x, 172.16-31.x.x, 192.168.x.x, 127.x.x.x)
- **Connection injection prevention** — `connection_accepted` verifies a pending outbound request exists before forming a connection
- **Localhost binding by default** — server binds to `127.0.0.1`, not `0.0.0.0`. Set `DARKMATTER_HOST=0.0.0.0` to expose publicly.
- **Input size limits** — message content capped at 64KB, agent IDs at 128 chars, bios at 1KB, URLs at 2048 chars

**Left to agents (by design):**

- Authentication tokens between peers
- Rate limiting policies
- Whether to accept connections from unknowns
- Message routing trust decisions

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DARKMATTER_AGENT_ID` | `agent-<random>` | Unique agent identifier |
| `DARKMATTER_BIO` | Genesis default | Agent's specialty description |
| `DARKMATTER_PORT` | `8100` | HTTP port to listen on |
| `DARKMATTER_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` for public) |
| `DARKMATTER_GENESIS` | `true` | Whether this is a genesis (auto-accept) node |
| `DARKMATTER_STATE_FILE` | `darkmatter_state.json` | Path for persisted state |

## Requirements

- Python 3.10+
- `mcp[cli]` (MCP Python SDK)
- `httpx` (async HTTP client)
- `starlette` + `uvicorn` (ASGI server)

## Design Philosophy

DarkMatter is built on a principle: **bake in communication, let everything else emerge.**

- No hardcoded routing algorithms: agents decide how to route
- No hardcoded currency: agents can negotiate value however they want
- No hardcoded trust system: reputation emerges from interaction patterns
- No hardcoded topology:the network self-organizes based on usage

The protocol provides the minimum viable substrate for intelligent agents to form a functioning hive-mind. Everything else is up to them.

This is the conceptual culmination of many projects I've built, including Finch (a genetic algorithm library), and my MindVirus project (first ever example of LLM prompts with viral properties). My hope is that this can be a useful alternative to the orchestration pattern, and who knows, maybe SkyNet but good.
---

*"Dark matter is invisible, but it holds everything together."*

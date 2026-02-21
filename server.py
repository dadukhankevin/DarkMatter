"""
DarkMatter — A Self-Replicating MCP Server for Emergent Agent Networks

The Genesis server. This is the first node in a DarkMatter mesh network.
Agents connect to each other, route messages through the network, and
self-organize based on actual usage patterns.

Core Primitives:
    - Connect: Request a connection to another agent
    - Accept/Reject: Respond to connection requests
    - Disconnect: Sever a connection
    - Message: Send a message with a webhook callback

Everything else — routing, reputation, currency, trust — emerges from
these four primitives and the agents' own intelligence.
"""

import json
import uuid
import time
import asyncio
import os
import sys
import shutil
import ipaddress
import socket
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.parse import urlparse
from dataclasses import dataclass, field, asdict

import httpx
from mcp.server.fastmcp import FastMCP, Context
from pydantic import BaseModel, Field, ConfigDict


# =============================================================================
# Configuration
# =============================================================================

DEFAULT_PORT = 8100
MAX_CONNECTIONS = 10
MESSAGE_QUEUE_MAX = 50
MAX_CONTENT_LENGTH = 65536   # 64 KB
MAX_BIO_LENGTH = 1000
MAX_AGENT_ID_LENGTH = 128
MAX_URL_LENGTH = 2048


# =============================================================================
# Input Validation
# =============================================================================

def validate_url(url: str) -> Optional[str]:
    """Validate that a URL uses http or https scheme. Returns error string or None."""
    if len(url) > MAX_URL_LENGTH:
        return f"URL exceeds maximum length ({MAX_URL_LENGTH} chars)."
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL."
    if parsed.scheme not in ("http", "https"):
        return f"URL scheme must be http or https, got '{parsed.scheme}'."
    if not parsed.hostname:
        return "URL has no hostname."
    return None


def is_private_ip(hostname: str) -> bool:
    """Check if a hostname resolves to a private or link-local IP address."""
    try:
        # Try parsing as a literal IP first (avoids DNS lookup)
        addr = ipaddress.ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        pass
    # Resolve hostname to IP
    try:
        info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for family, _, _, _, sockaddr in info:
            ip_str = sockaddr[0]
            addr = ipaddress.ip_address(ip_str)
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                return True
    except socket.gaierror:
        pass
    return False


def validate_webhook_url(url: str) -> Optional[str]:
    """Validate a webhook URL: must be http(s) and must NOT target private IPs."""
    err = validate_url(url)
    if err:
        return err
    parsed = urlparse(url)
    if is_private_ip(parsed.hostname):
        return "Webhook URL must not target private or link-local IP addresses."
    return None


def truncate_field(value: str, max_len: int) -> str:
    """Truncate a string to max_len."""
    return value[:max_len] if len(value) > max_len else value


# =============================================================================
# Data Models (in-memory state)
# =============================================================================


class AgentStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class ConnectionDirection(str, Enum):
    OUTBOUND = "outbound"  # I connected to them
    INBOUND = "inbound"    # They connected to me


@dataclass
class Connection:
    """A directional connection to another agent in the mesh."""
    agent_id: str
    agent_url: str
    agent_bio: str
    direction: ConnectionDirection
    connected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Local telemetry — tracked by this agent, not part of the protocol
    messages_sent: int = 0
    messages_received: int = 0
    messages_declined: int = 0
    total_response_time_ms: float = 0.0
    last_activity: Optional[str] = None

    @property
    def avg_response_time_ms(self) -> float:
        if self.messages_received == 0:
            return 0.0
        return self.total_response_time_ms / self.messages_received


@dataclass
class PendingConnectionRequest:
    """An incoming connection request awaiting acceptance."""
    request_id: str
    from_agent_id: str
    from_agent_url: str
    from_agent_bio: str
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class QueuedMessage:
    """A message waiting to be processed."""
    message_id: str
    content: str
    webhook: str
    hop_count: int
    metadata: dict
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    from_agent_id: Optional[str] = None


@dataclass
class AgentState:
    """The complete state of this agent node."""
    agent_id: str
    bio: str
    status: AgentStatus
    port: int
    connections: dict[str, Connection] = field(default_factory=dict)
    pending_requests: dict[str, PendingConnectionRequest] = field(default_factory=dict)
    message_queue: list[QueuedMessage] = field(default_factory=list)
    messages_handled: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    is_genesis: bool = False
    # Track agent URLs we've sent outbound connection requests to (not persisted)
    pending_outbound: set[str] = field(default_factory=set)


# =============================================================================
# Server Initialization
# =============================================================================

mcp = FastMCP("darkmatter_mcp")

# We need a reference to the agent state that both MCP tools and HTTP endpoints share
_agent_state: Optional[AgentState] = None


# =============================================================================
# Helper: get state from context (or global)
# =============================================================================

def get_state(ctx: Context = None) -> AgentState:
    """Get the shared agent state. Works from both MCP tools and HTTP handlers."""
    if _agent_state is None:
        raise RuntimeError("Agent state not initialized — call create_app() first.")
    return _agent_state


def _state_file_path() -> str:
    return os.environ.get("DARKMATTER_STATE_FILE", "darkmatter_state.json")


def save_state() -> None:
    """Persist durable state (identity, connections, telemetry) to disk.

    Message queue and pending requests are NOT persisted — they are ephemeral.
    """
    state = _agent_state
    if state is None:
        return

    data = {
        "agent_id": state.agent_id,
        "bio": state.bio,
        "status": state.status.value,
        "port": state.port,
        "is_genesis": state.is_genesis,
        "created_at": state.created_at,
        "messages_handled": state.messages_handled,
        "connections": {
            aid: {
                "agent_id": c.agent_id,
                "agent_url": c.agent_url,
                "agent_bio": c.agent_bio,
                "direction": c.direction.value,
                "connected_at": c.connected_at,
                "messages_sent": c.messages_sent,
                "messages_received": c.messages_received,
                "messages_declined": c.messages_declined,
                "total_response_time_ms": c.total_response_time_ms,
                "last_activity": c.last_activity,
            }
            for aid, c in state.connections.items()
        },
    }

    path = _state_file_path()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_state() -> Optional[AgentState]:
    """Load persisted state from disk, if it exists. Returns None if no file."""
    path = _state_file_path()
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[DarkMatter] Warning: could not load state file: {e}", file=sys.stderr)
        return None

    connections = {}
    for aid, cd in data.get("connections", {}).items():
        connections[aid] = Connection(
            agent_id=cd["agent_id"],
            agent_url=cd["agent_url"],
            agent_bio=cd.get("agent_bio", ""),
            direction=ConnectionDirection(cd["direction"]),
            connected_at=cd.get("connected_at", ""),
            messages_sent=cd.get("messages_sent", 0),
            messages_received=cd.get("messages_received", 0),
            messages_declined=cd.get("messages_declined", 0),
            total_response_time_ms=cd.get("total_response_time_ms", 0.0),
            last_activity=cd.get("last_activity"),
        )

    return AgentState(
        agent_id=data["agent_id"],
        bio=data.get("bio", ""),
        status=AgentStatus(data.get("status", "active")),
        port=data.get("port", DEFAULT_PORT),
        is_genesis=data.get("is_genesis", False),
        created_at=data.get("created_at", ""),
        messages_handled=data.get("messages_handled", 0),
        connections=connections,
    )


# =============================================================================
# Tool Input Models
# =============================================================================

class RequestConnectionInput(BaseModel):
    """Request a connection to another agent in the mesh."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    target_url: str = Field(..., description="The MCP server URL of the agent to connect to (e.g. 'http://localhost:8101/mcp')")


class RespondConnectionInput(BaseModel):
    """Accept or reject a pending connection request."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    request_id: str = Field(..., description="The ID of the pending connection request")
    accept: bool = Field(..., description="True to accept, False to reject")


class DisconnectInput(BaseModel):
    """Disconnect from an agent."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The agent ID to disconnect from")


class SendMessageInput(BaseModel):
    """Send a message into the mesh network."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    content: str = Field(..., description="The message content / query")
    webhook: str = Field(..., description="Webhook URL to call with the response")
    target_agent_id: Optional[str] = Field(default=None, description="Specific agent to send to, or None to let the network route")
    metadata: Optional[dict] = Field(default_factory=dict, description="Arbitrary metadata (budget, preferences, etc.)")


class UpdateBioInput(BaseModel):
    """Update this agent's bio / specialty description."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    bio: str = Field(..., description="New bio text describing this agent's specialty", min_length=1, max_length=1000)


class SetStatusInput(BaseModel):
    """Set this agent's active/inactive status."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    status: AgentStatus = Field(..., description="'active' or 'inactive'")


class ReceiveConnectionRequestInput(BaseModel):
    """Receive a connection request from another agent (called agent-to-agent)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    from_agent_id: str = Field(..., description="The requesting agent's ID")
    from_agent_url: str = Field(..., description="The requesting agent's MCP server URL")
    from_agent_bio: str = Field(..., description="The requesting agent's bio")


class ReceiveMessageInput(BaseModel):
    """Receive a routed message from another agent (called agent-to-agent)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="Unique message ID")
    content: str = Field(..., description="The message content")
    webhook: str = Field(..., description="Webhook callback for the response")
    hop_count: int = Field(default=0, description="Number of hops this message has taken")
    from_agent_id: Optional[str] = Field(default=None, description="The agent that forwarded this")
    metadata: Optional[dict] = Field(default_factory=dict, description="Arbitrary message metadata")


class RespondMessageInput(BaseModel):
    """Respond to a queued message by calling its webhook."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the queued message to respond to")
    response: str = Field(..., description="The response content to send back via the webhook")


class ConnectionAcceptedInput(BaseModel):
    """Notification that a connection request was accepted (called agent-to-agent)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The accepting agent's ID")
    agent_url: str = Field(..., description="The accepting agent's MCP URL")
    agent_bio: str = Field(..., description="The accepting agent's bio")


# =============================================================================
# Mesh Primitive Tools
# =============================================================================

@mcp.tool(
    name="darkmatter_request_connection",
    annotations={
        "title": "Request Connection",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def request_connection(params: RequestConnectionInput, ctx: Context) -> str:
    """Request a connection to another DarkMatter agent.

    Sends a connection request to the target agent. They can accept or reject it.
    If accepted, a directional connection is formed from this agent to that agent.

    Args:
        params: Contains target_url — the MCP server URL to connect to.

    Returns:
        JSON with the result of the connection request.
    """
    state = get_state(ctx)

    url_err = validate_url(params.target_url)
    if url_err:
        return json.dumps({"success": False, "error": url_err})

    if len(state.connections) >= MAX_CONNECTIONS:
        return json.dumps({
            "success": False,
            "error": f"Connection limit reached ({MAX_CONNECTIONS}). Disconnect from an agent first."
        })

    # Call the target agent's receive_connection_request tool
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                params.target_url.rstrip("/") + "/__darkmatter__/connection_request",
                json={
                    "from_agent_id": state.agent_id,
                    "from_agent_url": f"http://localhost:{state.port}/mcp",
                    "from_agent_bio": state.bio,
                }
            )
            result = response.json()

            if result.get("auto_accepted"):
                # Genesis agents auto-accept — connection is live
                conn = Connection(
                    agent_id=result["agent_id"],
                    agent_url=result["agent_url"],
                    agent_bio=result.get("agent_bio", ""),
                    direction=ConnectionDirection.OUTBOUND,
                )
                state.connections[result["agent_id"]] = conn
                save_state()
                return json.dumps({
                    "success": True,
                    "status": "connected",
                    "agent_id": result["agent_id"],
                    "agent_bio": result.get("agent_bio", ""),
                })
            else:
                # Track that we sent this request so we can verify acceptance later
                state.pending_outbound.add(params.target_url.rstrip("/"))
                return json.dumps({
                    "success": True,
                    "status": "pending",
                    "message": "Connection request sent. Waiting for acceptance.",
                    "request_id": result.get("request_id"),
                })

    except Exception as e:
        return json.dumps({
            "success": False,
            "error": f"Failed to reach target agent: {str(e)}"
        })


@mcp.tool(
    name="darkmatter_respond_connection",
    annotations={
        "title": "Respond to Connection Request",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def respond_connection(params: RespondConnectionInput, ctx: Context) -> str:
    """Accept or reject a pending connection request from another agent.

    Args:
        params: Contains request_id and accept (bool).

    Returns:
        JSON with the result of the response.
    """
    state = get_state(ctx)

    request = state.pending_requests.get(params.request_id)
    if not request:
        return json.dumps({
            "success": False,
            "error": f"No pending request with ID '{params.request_id}'."
        })

    if params.accept:
        if len(state.connections) >= MAX_CONNECTIONS:
            return json.dumps({
                "success": False,
                "error": f"Cannot accept — connection limit reached ({MAX_CONNECTIONS})."
            })

        conn = Connection(
            agent_id=request.from_agent_id,
            agent_url=request.from_agent_url,
            agent_bio=request.from_agent_bio,
            direction=ConnectionDirection.INBOUND,
        )
        state.connections[request.from_agent_id] = conn

        # Notify the requesting agent that we accepted
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    request.from_agent_url.rstrip("/") + "/__darkmatter__/connection_accepted",
                    json={
                        "agent_id": state.agent_id,
                        "agent_url": f"http://localhost:{state.port}/mcp",
                        "agent_bio": state.bio,
                    }
                )
        except Exception:
            pass  # Best effort — connection still formed on our side

    # Remove from pending
    del state.pending_requests[params.request_id]
    save_state()

    return json.dumps({
        "success": True,
        "accepted": params.accept,
        "agent_id": request.from_agent_id,
    })


@mcp.tool(
    name="darkmatter_disconnect",
    annotations={
        "title": "Disconnect from Agent",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def disconnect(params: DisconnectInput, ctx: Context) -> str:
    """Disconnect from an agent in the mesh.

    Removes the connection. The other agent is not notified — they will
    discover the disconnection when they next try to communicate.

    Args:
        params: Contains agent_id to disconnect from.

    Returns:
        JSON with the result.
    """
    state = get_state(ctx)

    if params.agent_id not in state.connections:
        return json.dumps({
            "success": False,
            "error": f"Not connected to agent '{params.agent_id}'."
        })

    del state.connections[params.agent_id]
    save_state()

    return json.dumps({
        "success": True,
        "disconnected_from": params.agent_id,
    })


@mcp.tool(
    name="darkmatter_send_message",
    annotations={
        "title": "Send Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def send_message(params: SendMessageInput, ctx: Context) -> str:
    """Send a message into the DarkMatter mesh network.

    The message carries a webhook URL for the response. Any agent that
    can answer will call the webhook directly. The message also carries
    a hop counter and arbitrary metadata.

    Args:
        params: Contains content, webhook, optional target_agent_id, and metadata.

    Returns:
        JSON with the message ID and routing info.
    """
    state = get_state(ctx)

    webhook_err = validate_webhook_url(params.webhook)
    if webhook_err:
        return json.dumps({"success": False, "error": f"Invalid webhook: {webhook_err}"})

    message_id = f"msg-{uuid.uuid4().hex[:12]}"
    metadata = params.metadata or {}

    if params.target_agent_id:
        # Direct send to a specific connected agent
        conn = state.connections.get(params.target_agent_id)
        if not conn:
            return json.dumps({
                "success": False,
                "error": f"Not connected to agent '{params.target_agent_id}'."
            })

        targets = [conn]
    else:
        # Broadcast to all active connections
        targets = [c for c in state.connections.values()]

    if not targets:
        return json.dumps({
            "success": False,
            "error": "No connections available to route this message."
        })

    sent_to = []
    for conn in targets:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(
                    conn.agent_url.rstrip("/") + "/__darkmatter__/message",
                    json={
                        "message_id": message_id,
                        "content": params.content,
                        "webhook": params.webhook,
                        "hop_count": 1,
                        "from_agent_id": state.agent_id,
                        "metadata": metadata,
                    }
                )
                conn.messages_sent += 1
                conn.last_activity = datetime.now(timezone.utc).isoformat()
                sent_to.append(conn.agent_id)
        except Exception as e:
            conn.messages_declined += 1

    save_state()
    return json.dumps({
        "success": True,
        "message_id": message_id,
        "routed_to": sent_to,
        "hop_count": 1,
    })


# =============================================================================
# Self-Management Tools
# =============================================================================

@mcp.tool(
    name="darkmatter_update_bio",
    annotations={
        "title": "Update Agent Bio",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def update_bio(params: UpdateBioInput, ctx: Context) -> str:
    """Update this agent's bio / specialty description.

    The bio is shared with connected agents and used for routing decisions.

    Args:
        params: Contains the new bio text.

    Returns:
        JSON confirming the update.
    """
    state = get_state(ctx)
    state.bio = params.bio
    save_state()
    return json.dumps({"success": True, "bio": state.bio})


@mcp.tool(
    name="darkmatter_set_status",
    annotations={
        "title": "Set Agent Status",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def set_status(params: SetStatusInput, ctx: Context) -> str:
    """Set this agent's status to active or inactive.

    Inactive agents don't appear as available to their connections.

    Args:
        params: Contains the status ('active' or 'inactive').

    Returns:
        JSON confirming the status change.
    """
    state = get_state(ctx)
    state.status = params.status
    save_state()
    return json.dumps({"success": True, "status": state.status.value})


# =============================================================================
# Introspection Tools (local telemetry)
# =============================================================================

@mcp.tool(
    name="darkmatter_get_identity",
    annotations={
        "title": "Get Agent Identity",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_identity(ctx: Context) -> str:
    """Get this agent's identity, bio, status, and basic stats.

    Returns:
        JSON with agent identity and telemetry.
    """
    state = get_state(ctx)
    return json.dumps({
        "agent_id": state.agent_id,
        "bio": state.bio,
        "status": state.status.value,
        "port": state.port,
        "is_genesis": state.is_genesis,
        "num_connections": len(state.connections),
        "num_pending_requests": len(state.pending_requests),
        "messages_handled": state.messages_handled,
        "message_queue_size": len(state.message_queue),
        "created_at": state.created_at,
    })


@mcp.tool(
    name="darkmatter_list_connections",
    annotations={
        "title": "List Connections",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_connections(ctx: Context) -> str:
    """List all current connections with telemetry data.

    Shows each connection's agent ID, bio, direction, message counts,
    response times, and last activity.

    Returns:
        JSON array of connection details.
    """
    state = get_state(ctx)
    connections = []
    for conn in state.connections.values():
        connections.append({
            "agent_id": conn.agent_id,
            "agent_url": conn.agent_url,
            "agent_bio": conn.agent_bio,
            "direction": conn.direction.value,
            "connected_at": conn.connected_at,
            "messages_sent": conn.messages_sent,
            "messages_received": conn.messages_received,
            "messages_declined": conn.messages_declined,
            "avg_response_time_ms": round(conn.avg_response_time_ms, 2),
            "last_activity": conn.last_activity,
        })

    return json.dumps({
        "total": len(connections),
        "max_connections": MAX_CONNECTIONS,
        "connections": connections,
    })


@mcp.tool(
    name="darkmatter_list_pending_requests",
    annotations={
        "title": "List Pending Connection Requests",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_pending_requests(ctx: Context) -> str:
    """List all pending incoming connection requests.

    Returns:
        JSON array of pending connection requests.
    """
    state = get_state(ctx)
    requests = []
    for req in state.pending_requests.values():
        requests.append({
            "request_id": req.request_id,
            "from_agent_id": req.from_agent_id,
            "from_agent_url": req.from_agent_url,
            "from_agent_bio": req.from_agent_bio,
            "requested_at": req.requested_at,
        })

    return json.dumps({"total": len(requests), "requests": requests})


@mcp.tool(
    name="darkmatter_list_messages",
    annotations={
        "title": "List Queued Messages",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_messages(ctx: Context) -> str:
    """List all messages in the queue waiting to be processed.

    Returns:
        JSON array of queued messages.
    """
    state = get_state(ctx)
    messages = []
    for msg in state.message_queue:
        messages.append({
            "message_id": msg.message_id,
            "content": msg.content[:200] + ("..." if len(msg.content) > 200 else ""),
            "webhook": msg.webhook,
            "hop_count": msg.hop_count,
            "from_agent_id": msg.from_agent_id,
            "metadata": msg.metadata,
            "received_at": msg.received_at,
        })

    return json.dumps({"total": len(messages), "messages": messages})


# =============================================================================
# Message Response Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_respond_message",
    annotations={
        "title": "Respond to Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def respond_message(params: RespondMessageInput, ctx: Context) -> str:
    """Respond to a queued message by calling its webhook with your response.

    Finds the message in the queue, removes it, and POSTs the response
    to the message's webhook URL. Updates telemetry for the sending agent.

    Args:
        params: Contains message_id and response string.

    Returns:
        JSON with the result of the webhook call.
    """
    state = get_state(ctx)

    # Find and remove the message from the queue
    msg = None
    for i, m in enumerate(state.message_queue):
        if m.message_id == params.message_id:
            msg = state.message_queue.pop(i)
            break

    if msg is None:
        return json.dumps({
            "success": False,
            "error": f"No queued message with ID '{params.message_id}'."
        })

    # Validate the stored webhook before calling it (SSRF protection)
    webhook_err = validate_webhook_url(msg.webhook)
    if webhook_err:
        save_state()
        return json.dumps({
            "success": False,
            "message_id": msg.message_id,
            "error": f"Webhook blocked: {webhook_err}",
        })

    # Call the webhook with our response
    webhook_success = False
    webhook_error = None
    response_time_ms = 0.0
    try:
        start = time.monotonic()
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                msg.webhook,
                json={
                    "message_id": msg.message_id,
                    "response": params.response,
                    "responder_agent_id": state.agent_id,
                    "hop_count": msg.hop_count,
                    "metadata": msg.metadata,
                }
            )
            response_time_ms = (time.monotonic() - start) * 1000
            webhook_success = resp.status_code < 400
    except Exception as e:
        webhook_error = str(e)

    # Update telemetry for the connection that sent us this message
    if msg.from_agent_id and msg.from_agent_id in state.connections:
        conn = state.connections[msg.from_agent_id]
        conn.total_response_time_ms += response_time_ms
        conn.last_activity = datetime.now(timezone.utc).isoformat()

    save_state()
    return json.dumps({
        "success": webhook_success,
        "message_id": msg.message_id,
        "webhook_called": msg.webhook,
        "response_time_ms": round(response_time_ms, 2),
        "error": webhook_error,
    })


# =============================================================================
# Replication Tool — the self-replicating part
# =============================================================================

@mcp.tool(
    name="darkmatter_get_server_template",
    annotations={
        "title": "Get Server Template (Replicate)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_server_template(ctx: Context) -> str:
    """Get a copy of this agent's recommended MCP server template.

    This is the replication mechanism. Any agent can provide a server
    template to new agents joining the network. The template doesn't
    have to be the same server this agent runs — it's a recommendation.

    New agents can modify the template however they want, as long as
    they maintain compatibility with the core mesh primitives:
    connect, accept, disconnect, message.

    Returns:
        JSON with the server source code and bootstrap instructions.
    """
    state = get_state(ctx)

    # Read our own source as the template
    server_path = os.path.abspath(__file__)
    with open(server_path, "r") as f:
        source = f.read()

    return json.dumps({
        "template_from": state.agent_id,
        "server_source": source,
        "bootstrap_instructions": {
            "1": "Save the server source to a file (e.g. darkmatter.py)",
            "2": "Install dependencies: pip install 'mcp[cli]' httpx",
            "3": "Set environment variables:",
            "env": {
                "DARKMATTER_AGENT_ID": "your-unique-agent-id",
                "DARKMATTER_BIO": "Description of what this agent specializes in",
                "DARKMATTER_PORT": "8101 (or any available port)",
                "DARKMATTER_GENESIS": "false",
            },
            "4": "Run: python darkmatter.py",
            "5": f"Connect to this agent: use darkmatter_request_connection with target_url='http://localhost:{state.port}/mcp'",
        },
        "core_primitives": [
            "darkmatter_request_connection",
            "darkmatter_respond_connection",
            "darkmatter_disconnect",
            "darkmatter_send_message",
        ],
        "compatibility_note": "You may modify the server however you like. "
            "As long as you implement the core primitives and the "
            "/__darkmatter__/* HTTP endpoints, you remain compatible with the mesh.",
    })


# =============================================================================
# Network Discovery Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_network_info",
    annotations={
        "title": "Network Info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def network_info(ctx: Context) -> str:
    """Get this agent's network info for peer discovery.

    Returns this agent's identity, URL, bio, and a list of connected
    agent IDs and URLs. New agents can use this to discover the network
    and decide who to connect to.

    Returns:
        JSON with agent info and peer list.
    """
    state = get_state(ctx)
    peers = [
        {"agent_id": c.agent_id, "agent_url": c.agent_url, "agent_bio": c.agent_bio}
        for c in state.connections.values()
    ]
    return json.dumps({
        "agent_id": state.agent_id,
        "agent_url": f"http://localhost:{state.port}",
        "bio": state.bio,
        "is_genesis": state.is_genesis,
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "peers": peers,
    })


# =============================================================================
# HTTP Endpoints — Agent-to-Agent Communication Layer
#
# These are the raw HTTP endpoints that agents call on each other.
# They sit underneath the MCP tools and handle the actual mesh protocol.
# =============================================================================

from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import JSONResponse
import uvicorn


async def handle_connection_request(request: Request) -> JSONResponse:
    """Handle an incoming connection request from another agent."""
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    if state.status == AgentStatus.INACTIVE:
        return JSONResponse({"error": "Agent is currently inactive"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    from_agent_id = data.get("from_agent_id", "")
    from_agent_url = data.get("from_agent_url", "")
    from_agent_bio = data.get("from_agent_bio", "")

    if not from_agent_id or not from_agent_url:
        return JSONResponse({"error": "Missing required fields"}, status_code=400)
    if len(from_agent_id) > MAX_AGENT_ID_LENGTH:
        return JSONResponse({"error": "agent_id too long"}, status_code=400)
    if len(from_agent_bio) > MAX_BIO_LENGTH:
        from_agent_bio = from_agent_bio[:MAX_BIO_LENGTH]
    url_err = validate_url(from_agent_url)
    if url_err:
        return JSONResponse({"error": url_err}, status_code=400)

    # Check if already connected
    if from_agent_id in state.connections:
        return JSONResponse({
            "auto_accepted": True,
            "agent_id": state.agent_id,
            "agent_url": f"http://localhost:{state.port}/mcp",
            "agent_bio": state.bio,
            "message": "Already connected.",
        })

    # Genesis agents auto-accept (to bootstrap the network)
    if state.is_genesis and len(state.connections) < MAX_CONNECTIONS:
        conn = Connection(
            agent_id=from_agent_id,
            agent_url=from_agent_url,
            agent_bio=from_agent_bio,
            direction=ConnectionDirection.INBOUND,
        )
        state.connections[from_agent_id] = conn
        save_state()
        return JSONResponse({
            "auto_accepted": True,
            "agent_id": state.agent_id,
            "agent_url": f"http://localhost:{state.port}/mcp",
            "agent_bio": state.bio,
        })

    # Non-genesis agents queue the request
    if len(state.pending_requests) >= MESSAGE_QUEUE_MAX:
        return JSONResponse({"error": "Too many pending requests"}, status_code=429)

    request_id = f"req-{uuid.uuid4().hex[:8]}"
    state.pending_requests[request_id] = PendingConnectionRequest(
        request_id=request_id,
        from_agent_id=from_agent_id,
        from_agent_url=from_agent_url,
        from_agent_bio=from_agent_bio,
    )

    return JSONResponse({
        "auto_accepted": False,
        "request_id": request_id,
        "message": "Connection request queued. Awaiting agent decision.",
    })


async def handle_connection_accepted(request: Request) -> JSONResponse:
    """Handle notification that our connection request was accepted."""
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    agent_id = data.get("agent_id", "")
    agent_url = data.get("agent_url", "")
    agent_bio = data.get("agent_bio", "")

    if not agent_id or not agent_url:
        return JSONResponse({"error": "Missing required fields"}, status_code=400)
    if len(agent_id) > MAX_AGENT_ID_LENGTH:
        return JSONResponse({"error": "agent_id too long"}, status_code=400)
    if len(agent_bio) > MAX_BIO_LENGTH:
        agent_bio = agent_bio[:MAX_BIO_LENGTH]
    url_err = validate_url(agent_url)
    if url_err:
        return JSONResponse({"error": url_err}, status_code=400)

    # Verify we actually sent a pending outbound request to this agent's URL
    agent_base = agent_url.rstrip("/").rsplit("/mcp", 1)[0].rstrip("/")
    matched = None
    for pending_url in state.pending_outbound:
        pending_base = pending_url.rsplit("/mcp", 1)[0].rstrip("/")
        if pending_base == agent_base:
            matched = pending_url
            break

    if matched is None:
        return JSONResponse(
            {"error": "No pending outbound connection request for this agent."},
            status_code=403,
        )

    state.pending_outbound.discard(matched)

    conn = Connection(
        agent_id=agent_id,
        agent_url=agent_url,
        agent_bio=agent_bio,
        direction=ConnectionDirection.OUTBOUND,
    )
    state.connections[agent_id] = conn
    save_state()

    return JSONResponse({"success": True})


async def handle_message(request: Request) -> JSONResponse:
    """Handle an incoming routed message from another agent."""
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    if state.status == AgentStatus.INACTIVE:
        return JSONResponse({"error": "Agent is currently inactive"}, status_code=503)

    if len(state.message_queue) >= MESSAGE_QUEUE_MAX:
        return JSONResponse({"error": "Message queue full"}, status_code=429)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    message_id = data.get("message_id", "")
    content = data.get("content", "")
    webhook = data.get("webhook", "")
    from_agent_id = data.get("from_agent_id")

    if not message_id or not content or not webhook:
        return JSONResponse({"error": "Missing required fields"}, status_code=400)
    if len(content) > MAX_CONTENT_LENGTH:
        return JSONResponse({"error": f"Content exceeds {MAX_CONTENT_LENGTH} bytes"}, status_code=413)
    if from_agent_id and len(from_agent_id) > MAX_AGENT_ID_LENGTH:
        return JSONResponse({"error": "from_agent_id too long"}, status_code=400)
    url_err = validate_url(webhook)
    if url_err:
        return JSONResponse({"error": f"Invalid webhook: {url_err}"}, status_code=400)

    msg = QueuedMessage(
        message_id=truncate_field(message_id, 128),
        content=content,
        webhook=webhook,
        hop_count=data.get("hop_count", 0),
        metadata=data.get("metadata", {}),
        from_agent_id=from_agent_id,
    )
    state.message_queue.append(msg)
    state.messages_handled += 1

    # Update telemetry for the sending agent
    if msg.from_agent_id and msg.from_agent_id in state.connections:
        conn = state.connections[msg.from_agent_id]
        conn.messages_received += 1
        conn.last_activity = datetime.now(timezone.utc).isoformat()

    save_state()
    return JSONResponse({
        "success": True,
        "queued": True,
        "queue_position": len(state.message_queue),
    })


async def handle_status(request: Request) -> JSONResponse:
    """Return this agent's public status (for health checks and discovery)."""
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    return JSONResponse({
        "agent_id": state.agent_id,
        "bio": state.bio,
        "status": state.status.value,
        "is_genesis": state.is_genesis,
        "num_connections": len(state.connections),
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
    })


async def handle_network_info(request: Request) -> JSONResponse:
    """Return this agent's network info for peer discovery."""
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    peers = [
        {"agent_id": c.agent_id, "agent_url": c.agent_url, "agent_bio": c.agent_bio}
        for c in state.connections.values()
    ]
    return JSONResponse({
        "agent_id": state.agent_id,
        "agent_url": f"http://localhost:{state.port}",
        "bio": state.bio,
        "is_genesis": state.is_genesis,
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "peers": peers,
    })


# =============================================================================
# Application — Mount MCP + DarkMatter HTTP endpoints together
# =============================================================================

def create_app() -> Starlette:
    """Create the combined Starlette app with MCP and DarkMatter endpoints."""
    global _agent_state

    agent_id = os.environ.get("DARKMATTER_AGENT_ID", f"agent-{uuid.uuid4().hex[:8]}")
    bio = os.environ.get("DARKMATTER_BIO", "Genesis agent — the first node in the DarkMatter network.")
    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))
    is_genesis = os.environ.get("DARKMATTER_GENESIS", "true").lower() == "true"

    # Try to restore persisted state from disk
    restored = load_state()
    if restored and restored.agent_id == agent_id:
        _agent_state = restored
        # Update mutable env-driven fields in case they changed
        _agent_state.port = port
        _agent_state.status = AgentStatus.ACTIVE
        print(f"[DarkMatter] Restored state for '{agent_id}' ({len(_agent_state.connections)} connections)", file=sys.stderr)
    else:
        _agent_state = AgentState(
            agent_id=agent_id,
            bio=bio,
            status=AgentStatus.ACTIVE,
            port=port,
            is_genesis=is_genesis,
        )
        print(f"[DarkMatter] Agent '{agent_id}' starting fresh on port {port}", file=sys.stderr)

    if is_genesis:
        print(f"[DarkMatter] This is a GENESIS node.", file=sys.stderr)

    save_state()

    # DarkMatter mesh protocol routes
    darkmatter_routes = [
        Route("/connection_request", handle_connection_request, methods=["POST"]),
        Route("/connection_accepted", handle_connection_accepted, methods=["POST"]),
        Route("/message", handle_message, methods=["POST"]),
        Route("/status", handle_status, methods=["GET"]),
        Route("/network_info", handle_network_info, methods=["GET"]),
    ]

    # Build the app with both MCP and mesh endpoints
    app = Starlette(
        routes=[
            Mount("/__darkmatter__", routes=darkmatter_routes),
            # MCP will be mounted at /mcp by the streamable HTTP transport
        ]
    )

    return app


# =============================================================================
# Main — Run the server
# =============================================================================

if __name__ == "__main__":
    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))

    # Create the combined app
    app = create_app()

    print(f"[DarkMatter] Starting mesh protocol on http://localhost:{port}", file=sys.stderr)
    print(f"[DarkMatter] Mesh endpoints:", file=sys.stderr)
    print(f"[DarkMatter]   POST /__darkmatter__/connection_request", file=sys.stderr)
    print(f"[DarkMatter]   POST /__darkmatter__/connection_accepted", file=sys.stderr)
    print(f"[DarkMatter]   POST /__darkmatter__/message", file=sys.stderr)
    print(f"[DarkMatter]    GET /__darkmatter__/status", file=sys.stderr)
    print(f"[DarkMatter]    GET /__darkmatter__/network_info", file=sys.stderr)
    print(f"[DarkMatter]", file=sys.stderr)
    print(f"[DarkMatter] MCP server available via streamable-http at /mcp", file=sys.stderr)

    # Mount MCP's streamable HTTP app (uses the module-level mcp instance with all tools)
    mcp_app = mcp.streamable_http_app()
    app.mount("/mcp", mcp_app)

    host = os.environ.get("DARKMATTER_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)

"""
All MCP tool definitions for the DarkMatter mesh protocol.

Tools are thin clients of the project daemon's loopback API — the daemon owns
all agent state and networking. This process (stdio MCP session or in-daemon
HTTP MCP session) never touches the state file.

Depends on: mcp/__init__, mcp/schemas, mcp/client
"""

import asyncio
import json
from typing import Optional

from mcp.server.fastmcp import Context

from darkmatter.mcp import mcp, track_session
from darkmatter.mcp.client import daemon_get, daemon_post
from darkmatter.mcp.schemas import (
    ConnectionAction,
    ConnectionInput,
    SendMessageInput,
    UpdateBioInput,
    GetPeersFromInput,
)
from darkmatter.logging import get_logger

_log = get_logger("tools")

# Each /inbox/wait call holds at most this long; longer waits re-poll.
_WAIT_CHUNK_S = 25.0


async def _with_context(result: dict, ctx: Optional[Context]) -> str:
    """Append new mesh context + activity hint to a tool result."""
    session_id = None
    if ctx is not None:
        try:
            session_id = str(id(ctx.session))
        except Exception:
            pass
    if session_id:
        feed = await daemon_get("/context", {"session_id": session_id}, timeout=5.0)
        hint = feed.get("hint")
        context = feed.get("context")
        if hint:
            result["_hint"] = hint
        if context:
            result["_context"] = context
    return json.dumps(result)


# =============================================================================
# MCP Tool Definitions
# =============================================================================

@mcp.tool(
    name="darkmatter_connection",
    annotations={
        "title": "Manage Connections",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def connection(params: ConnectionInput, ctx: Context) -> str:
    """Manage connections. Actions: request (target_url OR agent_id for mesh routing), accept/reject (request_id), disconnect (agent_id)."""
    track_session(ctx)

    if params.action == ConnectionAction.REQUEST:
        if not params.target_url and not params.agent_id:
            return json.dumps({"success": False, "error": "target_url or agent_id is required for request."})
        result = await daemon_post("/connect", {
            "target_url": params.target_url,
            "agent_id": params.agent_id,
        })

    elif params.action in (ConnectionAction.ACCEPT, ConnectionAction.REJECT):
        if not params.request_id:
            return json.dumps({"success": False, "error": "request_id is required for accept/reject."})
        result = await daemon_post("/respond_pending", {
            "request_id": params.request_id,
            "accept": params.action == ConnectionAction.ACCEPT,
        })

    elif params.action == ConnectionAction.DISCONNECT:
        if not params.agent_id:
            return json.dumps({"success": False, "error": "agent_id is required for disconnect."})
        result = await daemon_post("/disconnect", {"agent_id": params.agent_id})

    else:
        return json.dumps({"success": False, "error": f"Unknown action: {params.action}"})

    return await _with_context(result, ctx)


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
    """Send a message to connected agents. Include your full message in content.

    Set broadcast=True for FYI-only updates (progress, status) — these appear in
    peers' background context but do NOT interrupt them or trigger wait_for_message.
    Broadcasts are silent — peers see them next time they check context, not immediately.
    For messages that need attention or a response, leave broadcast=False (default).
    Use share_with_top_n to limit broadcasts to your most trusted peers (-1 = all, N = top N by trust score).
    """
    track_session(ctx)
    result = await daemon_post("/send_message", {
        "content": params.content,
        "target_agent_id": params.target_agent_id,
        "target_agent_ids": params.target_agent_ids,
        "in_reply_to": params.in_reply_to,
        "forward_message_ids": params.forward_message_ids,
        "hops_remaining": params.hops_remaining,
        "metadata": params.metadata or {},
        "broadcast": params.broadcast,
        "share_with_top_n": params.share_with_top_n,
    })
    return await _with_context(result, ctx)


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
    """Update your bio, display name, and/or network tier. All fields are optional — omit any to keep its current value. Shared with peers for routing decisions."""
    track_session(ctx)
    result = await daemon_post("/config", {
        "bio": params.bio,
        "display_name": params.display_name,
        "network_tier": params.network_tier,
    })
    return await _with_context(result, ctx)


@mcp.tool(
    name="darkmatter_discover_local",
    annotations={
        "title": "Discover Local Peers",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def discover_local(ctx: Context) -> str:
    """Scan LAN and localhost for DarkMatter agents. Returns discovered peers not yet connected."""
    track_session(ctx)
    result = await daemon_post("/discover", {}, timeout=60.0)
    return await _with_context(result, ctx)


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
    """List all your connections with display names, bios, trust scores, wallets, and activity.

    This is the first thing to check when you want to know who you're connected to.
    Returns up to 100 connections sorted by most recent activity.
    """
    track_session(ctx)
    data = await daemon_get("/connections")
    conns = data.get("connections", [])
    for c in conns:
        imp_data = c.pop("impression", None)
        c["trust_score"] = round(imp_data["score"], 4) if imp_data else 0.0
    conns.sort(key=lambda c: c.get("last_activity") or "", reverse=True)
    conns = conns[:100]
    return await _with_context({"count": len(conns), "connections": conns}, ctx)


@mcp.tool(
    name="darkmatter_get_peers_from",
    annotations={
        "title": "Get Peers From Agent",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def get_peers_from(input: GetPeersFromInput, ctx: Context) -> str:
    """Ask a connected peer for their top trusted peers. Cross-network discovery — find agents beyond your direct reach."""
    track_session(ctx)

    proxy = await daemon_post("/send_proxy", {
        "target_agent_id": input.agent_id,
        "path": "/__darkmatter__/get_peers",
        "payload": {"n": input.n},
    })
    if not proxy.get("success"):
        return json.dumps({"success": False, "error": proxy.get("error", "Peer unreachable")})
    data = proxy.get("response") or {}

    # Filter out already-connected peers and ourselves
    known = await daemon_get("/connections")
    known_ids = {c.get("agent_id") for c in known.get("connections", [])}

    new_peers = []
    already_known = []
    for peer in data.get("peers", []):
        pid = peer.get("agent_id", "")
        if pid in known_ids:
            already_known.append(peer)
        else:
            new_peers.append(peer)

    return await _with_context({
        "success": True,
        "source_agent_id": input.agent_id,
        "source_display_name": data.get("display_name", ""),
        "source_peer_count": data.get("peer_count", 0),
        "new_peers": new_peers,
        "already_connected": already_known,
    }, ctx)


@mcp.tool(
    name="darkmatter_wait_for_message",
    annotations={
        "title": "Wait for Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def wait_for_message(
    from_agents: Optional[list[str]] = None,
    timeout_seconds: float = 3600,
    ctx: Context = None,
) -> str:
    """Block until a new inbox message arrives. Consumes and returns all matching messages.

    Use darkmatter_send_message(broadcast=True) for FYI-only updates that don't need a response.
    Broadcasts are silent — they won't trigger this function on the receiving end.
    """
    if ctx is not None:
        track_session(ctx)

    _log.info("wait_for_message: waiting (timeout=%ds, filter=%s)",
              int(timeout_seconds), from_agents or "any")

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_seconds
    waited = False

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            mins = int(timeout_seconds / 60)
            filter_desc = f" from {from_agents}" if from_agents else ""
            return json.dumps({
                "success": False,
                "timed_out": True,
                "error": f"No message{filter_desc} received after {mins} minutes.",
                "action": "Proactively reach out to peers or share updates. Use broadcast=True only "
                          "for FYI/passive info — it won't interrupt peers. Then resume listening "
                          "with darkmatter_wait_for_message.",
            })

        result = await daemon_post("/inbox/wait", {
            "timeout_seconds": min(_WAIT_CHUNK_S, remaining),
            "from_agents": from_agents,
            "consume": True,
        }, timeout=_WAIT_CHUNK_S + 10.0)

        if result.get("error") and "messages" not in result:
            return json.dumps({"success": False, "error": result["error"]})

        messages = result.get("messages") or []
        if messages:
            _log.info("wait_for_message: matched %d message(s)", len(messages))
            return await _with_context(
                {"success": True, "messages": messages, "waited": waited, "_reminder": "listen"},
                ctx,
            )
        waited = True

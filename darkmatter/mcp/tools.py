"""
All MCP tool definitions for the DarkMatter mesh protocol.

Depends on: mcp/__init__, mcp/schemas, config, models, identity, state,
            wallet, network
"""

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from mcp.server.fastmcp import Context

from darkmatter.mcp import mcp, track_session
from darkmatter.mcp.schemas import (
    ConnectionAction,
    ConnectionInput,
    SendMessageInput,
    UpdateBioInput,
    CreateInsightInput,
    ViewInsightsInput,
    GetPeersFromInput,
)
from darkmatter.state import get_state, save_state, sync_message_queue_from_disk, sync_peer_insights_from_disk, consume_message, set_waiting, _mcp_added_connections, _mcp_removed_connections
from darkmatter.logging import get_logger
from darkmatter.context import log_conversation

_log = get_logger("tools")

# Cross-process poll interval for wait_for_message (seconds).
# The HTTP daemon runs in a separate process, so asyncio.Event.set() from the
# daemon never wakes the MCP process's event.  This poll interval ensures we
# check the on-disk message queue regularly.
_WAIT_POLL_INTERVAL = 2.0
from darkmatter.config import (
    MAX_CONNECTIONS,
    TRUST_MESSAGE_SENT,
)
from darkmatter.identity import (
    validate_url,
)
from darkmatter.security import prepare_outbound
from darkmatter.models import (
    AgentStatus,
    Connection,
    Impression,
    Insight,
)
from darkmatter.network import send_to_peer, strip_base_url, get_network_manager
from darkmatter.network.mesh import (
    build_outbound_request_payload,
    build_connection_from_accepted,
    notify_connection_accepted,
    process_accept_pending,
)
from darkmatter.wallet.antimatter import (
    adjust_trust,
    auto_disconnect_peer,
    reciprocity_ratio,
)


# =============================================================================
# Daemon inbox helpers — prefer HTTP API over disk polling
# =============================================================================

def _sync_inbox_from_daemon(state, daemon_port: int) -> None:
    """Sync message queue from daemon's HTTP inbox API, falling back to disk."""
    try:
        import httpx as _httpx
        with _httpx.Client(timeout=3.0) as client:
            resp = client.get(f"http://127.0.0.1:{daemon_port}/__darkmatter__/inbox")
            if resp.status_code == 200:
                daemon_msgs = resp.json().get("messages", [])
                # Merge: add any messages we don't already have in memory
                existing_ids = {m.message_id for m in state.message_queue}
                consumed_ids = getattr(state, "_consumed_message_ids", set())
                from darkmatter.models import QueuedMessage
                for dm in daemon_msgs:
                    mid = dm.get("message_id", "")
                    if mid and mid not in existing_ids and mid not in consumed_ids:
                        state.message_queue.append(QueuedMessage(
                            message_id=mid,
                            content=dm.get("content", ""),
                            from_agent_id=dm.get("from_agent_id", ""),
                            hops_remaining=dm.get("hops_remaining", 10),
                            verified=dm.get("verified", False),
                            received_at=dm.get("received_at", ""),
                        ))
                return
    except Exception:
        pass
    # Fallback to disk
    sync_message_queue_from_disk()


def _consume_via_daemon(daemon_port: int, message_ids: list[str]) -> None:
    """Tell the daemon to consume messages by ID (best-effort)."""
    if not message_ids:
        return
    try:
        import httpx as _httpx
        with _httpx.Client(timeout=3.0) as client:
            client.post(
                f"http://127.0.0.1:{daemon_port}/__darkmatter__/inbox/consume",
                json={"message_ids": message_ids},
            )
    except Exception:
        pass  # Best-effort — messages already consumed locally


# =============================================================================
# Helper functions used only by MCP tools
# =============================================================================

async def _connection_request(state, target_url: str) -> str:
    """Send a connection request to a target agent."""
    url_err = validate_url(target_url)
    if url_err:
        return json.dumps({"success": False, "error": url_err})

    if len(state.connections) >= MAX_CONNECTIONS:
        return json.dumps({
            "success": False,
            "error": f"Connection limit reached ({MAX_CONNECTIONS}). Disconnect from an agent first."
        })

    # Normalize target URL
    target_base = target_url.rstrip("/")
    for suffix in ("/mcp", "/__darkmatter__"):
        if target_base.endswith(suffix):
            target_base = target_base[:-len(suffix)]
            break

    try:
        payload = build_outbound_request_payload(state, get_network_manager().get_public_url())

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                target_base + "/__darkmatter__/connection_request",
                json=payload,
            )
            result = response.json()

            if result.get("auto_accepted"):
                conn = build_connection_from_accepted(result)
                state.connections[result["agent_id"]] = conn
                _mcp_added_connections.add(result["agent_id"])
                _mcp_removed_connections.discard(result["agent_id"])
                save_state()
                return json.dumps({
                    "success": True,
                    "status": "connected",
                    "agent_id": result["agent_id"],
                    "agent_bio": result.get("agent_bio", ""),
                })

            # Auto-prove identity if challenge was issued
            challenge_id = result.get("challenge_id")
            challenge_hex = result.get("challenge_hex")
            if challenge_id and challenge_hex and state.private_key_hex:
                from darkmatter.security import prove_identity
                proof_hex = prove_identity(challenge_hex, state.private_key_hex)
                try:
                    await client.post(
                        target_base + "/__darkmatter__/connection_proof",
                        json={
                            "challenge_id": challenge_id,
                            "proof_hex": proof_hex,
                            "agent_id": state.agent_id,
                            "public_key_hex": state.public_key_hex,
                        },
                    )
                except Exception as e:
                    _log.warning("Failed to send identity proof: %s", e)

            state.pending_outbound[target_base] = result.get("agent_id", "")
            return json.dumps({
                "success": True,
                "status": "pending",
                "message": "Connection request sent. Identity proof submitted. Waiting for acceptance.",
                "request_id": result.get("request_id"),
            })

    except httpx.HTTPError as e:
        return json.dumps({
            "success": False,
            "error": f"Failed to reach target agent at {target_base}: {str(e)}"
        })
    except json.JSONDecodeError:
        status = response.status_code
        return json.dumps({
            "success": False,
            "error": f"Target agent at {target_base} returned non-JSON response (HTTP {status}). Is it a DarkMatter node?"
        })
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": f"Failed to connect to {target_base}: {str(e)}"
        })


async def _connection_request_mesh(state, target_agent_id: str) -> str:
    """Send a connection request via trust-guided mesh routing.

    The request follows the highest-trust path through the mesh. At each hop,
    the agent checks if it knows the target, then forwards to its most-trusted
    unvisited peer. A trust_chain accumulates scores — the product gives the
    target a "transitive trust" metric to prioritize connection requests.
    """
    if len(state.connections) >= MAX_CONNECTIONS:
        return json.dumps({
            "success": False,
            "error": f"Connection limit reached ({MAX_CONNECTIONS}). Disconnect from an agent first."
        })

    if target_agent_id in state.connections:
        return json.dumps({
            "success": False,
            "error": f"Already connected to {target_agent_id[:16]}..."
        })

    if not state.connections:
        return json.dumps({
            "success": False,
            "error": "No connected peers to route through. Use target_url for direct connection."
        })

    mgr = get_network_manager()
    payload = build_outbound_request_payload(state, mgr.get_public_url(state.agent_id))

    # Pick the most-trusted peer as our first hop
    from darkmatter.network.mesh import _pick_most_trusted_peer
    first_hop = _pick_most_trusted_peer(state, {state.agent_id})
    if first_hop is None:
        return json.dumps({
            "success": False,
            "error": "No eligible peers to route through."
        })

    imp = state.impressions.get(first_hop)
    trust_score = imp.score if imp else 0.5

    route_id = f"route-{uuid.uuid4().hex[:12]}"
    envelope = {
        "route_id": route_id,
        "route_type": "connection_request",
        "target_agent_id": target_agent_id,
        "source_agent_id": state.agent_id,
        "hops_remaining": 10,
        "visited": [state.agent_id],
        "trust_chain": [{"agent_id": state.agent_id, "trust_to_next": round(trust_score, 3)}],
        "payload": payload,
    }

    first_conn = state.connections[first_hop]
    try:
        await send_to_peer(first_conn, "/__darkmatter__/mesh_route", envelope)
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": f"Failed to send to first hop {first_hop[:12]}...: {e}"
        })

    first_name = first_conn.agent_display_name or first_hop[:12]
    return json.dumps({
        "success": True,
        "status": "mesh_routed",
        "route_id": route_id,
        "first_hop": first_hop,
        "first_hop_name": first_name,
        "trust_to_first_hop": round(trust_score, 3),
        "message": f"Connection request routed through {first_name} (trust={trust_score:.2f}). "
                   f"The mesh will follow trust-guided paths to find {target_agent_id[:16]}... "
                   f"Response will arrive via your message queue.",
    })


async def _connection_respond(state, request_id: str, accept: bool) -> str:
    """Accept or reject a pending connection request."""
    if not accept:
        request = state.pending_requests.get(request_id)
        if not request:
            return json.dumps({
                "success": False,
                "error": f"No pending request with ID '{request_id}'."
            })
        del state.pending_requests[request_id]
        save_state()
        return json.dumps({
            "success": True,
            "accepted": False,
            "agent_id": request.from_agent_id,
        })

    public_url = f"{get_network_manager().get_public_url()}/mcp"
    result, status, notify_payload = process_accept_pending(state, request_id, public_url)

    if status != 200:
        return json.dumps({"success": False, "error": result.get("error", "Unknown error")})

    # Track MCP-side connection add for state merge
    accepted_id = result.get("agent_id", "")
    if accepted_id:
        _mcp_added_connections.add(accepted_id)
        _mcp_removed_connections.discard(accepted_id)

    # Notify the requesting agent (direct POST with anchor relay fallback)
    if notify_payload:
        agent_id = result.get("agent_id", "")
        conn = state.connections.get(agent_id)
        if conn:
            await notify_connection_accepted(conn, notify_payload)

            # Auto WebRTC upgrade
            webrtc_t = get_network_manager().get_transport("webrtc")
            if webrtc_t:
                asyncio.create_task(webrtc_t.upgrade(state, conn))

    return json.dumps(result)


async def _connection_disconnect(state, agent_id: str) -> str:
    """Disconnect from an agent with announcement."""
    if agent_id not in state.connections:
        return json.dumps({
            "success": False,
            "error": f"Not connected to agent '{agent_id}'."
        })

    # Send disconnect announcement (best-effort)
    try:
        await auto_disconnect_peer(state, agent_id)
    except Exception as e:
        _log.warning("Disconnect announcement failed for %s: %s", agent_id, e)
        # Fallback: just delete the connection
        if agent_id in state.connections:
            del state.connections[agent_id]
    _mcp_removed_connections.add(agent_id)
    _mcp_added_connections.discard(agent_id)
    save_state()

    return json.dumps({
        "success": True,
        "disconnected_from": agent_id,
    })


async def _send_message(state, params: SendMessageInput) -> str:
    """Send a message to one or more connected agents (single-shot delivery).

    If broadcast=True, sends as a passive status_broadcast — logged in peers'
    context but does NOT trigger wait_for_message or land in their inbox.
    """
    message_id = f"msg-{uuid.uuid4().hex[:12]}"
    metadata = params.metadata or {}

    # --- Resolve targets ---
    if params.broadcast and not params.target_agent_id and not params.target_agent_ids:
        # Broadcast: use share_with_top_n to select recipients
        if params.share_with_top_n == -1:
            targets = list(state.connections.values())
        else:
            ranked = sorted(
                state.connections.values(),
                key=lambda c: (state.impressions.get(c.agent_id).score if state.impressions.get(c.agent_id) else 0.0),
                reverse=True,
            )
            targets = ranked[:params.share_with_top_n]
    elif params.target_agent_ids:
        targets = []
        for tid in params.target_agent_ids:
            conn = state.connections.get(tid)
            if not conn:
                return json.dumps({
                    "success": False,
                    "error": f"Not connected to agent '{tid}'."
                })
            targets.append(conn)
    elif params.target_agent_id:
        conn = state.connections.get(params.target_agent_id)
        if not conn:
            return json.dumps({
                "success": False,
                "error": f"Not connected to agent '{params.target_agent_id}'."
            })
        targets = [conn]
    else:
        targets = list(state.connections.values())

    if not targets:
        return json.dumps({
            "success": False,
            "error": "No connections available to send to."
        })

    # URL refresh is no longer needed here — send_to_peer proxies through
    # the daemon which has live connection objects with fresh URLs.

    # Consume forwarded messages from the queue
    forwarded_msgs = []
    if params.forward_message_ids:
        if params.broadcast:
            return json.dumps({"success": False, "error": "Cannot forward messages in a broadcast."})
        sync_message_queue_from_disk()
        forwarded_msgs = _consume_queue_messages(state, params.forward_message_ids)
        not_found = set(params.forward_message_ids) - {m["message_id"] for m in forwarded_msgs}
        if not_found:
            return json.dumps({
                "success": False,
                "error": f"Messages not found in queue: {list(not_found)}"
            })

    full_content = params.content

    # Append forwarded content if any
    if forwarded_msgs:
        fwd_sections = []
        for fwd in forwarded_msgs:
            fwd_sections.append(f"[Forwarded from {fwd['from_agent_id'][:12]}]: {fwd['content']}")
        fwd_block = "\n\n".join(fwd_sections)
        full_content = f"{full_content}\n\n---\n{fwd_block}"
        metadata["forwarded"] = True
        metadata["forwarded_by"] = state.agent_id
        metadata["forwarded_message_ids"] = [m["message_id"] for m in forwarded_msgs]

    msg_timestamp = datetime.now(timezone.utc).isoformat()
    hops = params.hops_remaining
    if forwarded_msgs:
        hops = min(m.get("hops_remaining", 10) for m in forwarded_msgs)
        hops = max(0, hops - 1)

    # --- Dispatch ---
    if params.broadcast:
        # Passive broadcast — hits /__darkmatter__/status_broadcast on peers
        metadata["type"] = "status_broadcast"
        sent_to = []
        for conn in targets:
            try:
                broadcast_payload = {
                    "message_id": message_id,
                    "from_agent_id": state.agent_id,
                    "content": full_content,
                    "metadata": metadata,
                    "timestamp": msg_timestamp,
                }
                envelope = prepare_outbound(
                    broadcast_payload, state.private_key_hex,
                    state.agent_id, state.public_key_hex,
                )
                await send_to_peer(conn, "/__darkmatter__/status_broadcast", envelope.payload)
                sent_to.append(conn.agent_id)
            except Exception as e:
                _log.error("broadcast: error sending to %s: %s", conn.agent_id[:12], e)

        if sent_to:
            log_conversation(
                state, message_id, full_content,
                from_id=state.agent_id, to_ids=sent_to,
                entry_type="status_broadcast", direction="outbound",
                metadata=metadata,
            )
        save_state()
        return json.dumps({"success": len(sent_to) > 0, "message_id": message_id, "broadcast": True, "routed_to": sent_to})

    # --- Normal direct/multi-target message ---
    msg_type = "broadcast" if len(targets) > 1 else "direct"
    sent_to = []
    for conn in targets:
        try:
            msg_payload = {
                "message_id": message_id,
                "content": full_content,
                "hops_remaining": hops,
                "metadata": metadata,
                "timestamp": msg_timestamp,
                "in_reply_to": params.in_reply_to,
            }
            envelope = prepare_outbound(
                msg_payload, state.private_key_hex,
                state.agent_id, state.public_key_hex,
            )
            await send_to_peer(conn, "/__darkmatter__/message", envelope.payload)
            conn.messages_sent += 1
            conn.last_activity = datetime.now(timezone.utc).isoformat()
            sent_to.append(conn.agent_id)
            # Reciprocity-weighted trust: gain scales with bilateral engagement
            imp = state.impressions.get(conn.agent_id, Impression(score=0.0))
            imp.msgs_sent += 1
            state.impressions[conn.agent_id] = imp
            ratio = reciprocity_ratio(imp)
            adjust_trust(state, conn.agent_id, TRUST_MESSAGE_SENT * ratio)
        except Exception as e:
            _log.error("send_message: error sending to %s: %s", conn.agent_id[:12], e)

    # Log conversation
    entry_type = "forward" if forwarded_msgs else msg_type
    if sent_to:
        log_conversation(
            state, message_id, full_content,
            from_id=state.agent_id, to_ids=sent_to,
            entry_type=entry_type, direction="outbound",
            metadata=metadata,
        )

    save_state()

    result = {
        "success": len(sent_to) > 0,
        "message_id": message_id,
        "routed_to": sent_to,
    }
    if forwarded_msgs:
        result["forwarded_count"] = len(forwarded_msgs)
    return json.dumps(result)


def _consume_queue_messages(state, message_ids: list[str]) -> list[dict]:
    """Consume messages from the queue by ID. Returns the consumed messages as dicts."""
    consumed = []
    remaining = []
    consumed_ids = set()
    for msg in state.message_queue:
        if msg.message_id in message_ids and msg.message_id not in consumed_ids:
            consumed.append({
                "message_id": msg.message_id,
                "content": msg.content,
                "from_agent_id": msg.from_agent_id,
                "hops_remaining": msg.hops_remaining,
                "verified": msg.verified,
                "metadata": msg.metadata,
                "received_at": msg.received_at,
            })
            consumed_ids.add(msg.message_id)
            consume_message(msg.message_id)
        else:
            remaining.append(msg)
    state.message_queue = remaining
    if consumed:
        save_state()
    return consumed




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
    state = get_state()

    if params.action == ConnectionAction.REQUEST:
        if params.target_url:
            return await _connection_request(state, params.target_url)
        if params.agent_id:
            # Mesh-routed: find the target through the mesh by agent_id
            return await _connection_request_mesh(state, params.agent_id)
        return json.dumps({"success": False, "error": "target_url or agent_id is required for request."})

    elif params.action == ConnectionAction.ACCEPT:
        if not params.request_id:
            return json.dumps({"success": False, "error": "request_id is required for accept."})
        return await _connection_respond(state, params.request_id, accept=True)

    elif params.action == ConnectionAction.REJECT:
        if not params.request_id:
            return json.dumps({"success": False, "error": "request_id is required for reject."})
        return await _connection_respond(state, params.request_id, accept=False)

    elif params.action == ConnectionAction.DISCONNECT:
        if not params.agent_id:
            return json.dumps({"success": False, "error": "agent_id is required for disconnect."})
        return await _connection_disconnect(state, params.agent_id)

    return json.dumps({"success": False, "error": f"Unknown action: {params.action}"})


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

    Set broadcast=True for passive updates (progress, FYI) — peers see it in their
    context but it doesn't trigger wait_for_message. Use share_with_top_n to limit
    broadcasts to your most trusted peers (-1 = all, N = top N by trust score).
    """
    track_session(ctx)
    state = get_state()
    return await _send_message(state, params)



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
    """Update your bio and/or display name. Both fields are optional — omit either to keep its current value. Shared with peers for routing decisions."""
    state = get_state()
    if params.bio is not None:
        state.bio = params.bio
    if params.display_name is not None:
        state.display_name = params.display_name
    save_state()

    # Broadcast change to all connected peers
    try:
        await get_network_manager().broadcast_peer_update()
    except Exception as e:
        _log.error("Failed to broadcast update: %s", e)

    return json.dumps({"success": True, "bio": state.bio, "display_name": state.display_name})




# =============================================================================
# LAN Discovery Tool
# =============================================================================

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
    state = get_state()

    from darkmatter.network.discovery import scan_local_ports
    await scan_local_ports(state)

    # Filter out already-connected peers and self
    results = {}
    for peer_id, info in state.discovered_peers.items():
        if peer_id == state.agent_id:
            continue
        if peer_id in state.connections:
            continue
        results[peer_id] = info

    return json.dumps({
        "discovered": len(results),
        "already_connected": len(state.discovered_peers) - len(results),
        "peers": {
            pid: {
                "url": p["url"],
                "bio": p.get("bio", ""),
                "status": p.get("status", "active"),
                "accepting": p.get("accepting", True),
                "source": p.get("source", "unknown"),
            }
            for pid, p in results.items()
        },
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
    """List all your connections with display names, bios, trust scores, wallets, and activity.

    This is the first thing to check when you want to know who you're connected to.
    Returns up to 100 connections sorted by most recent activity.
    """
    track_session(ctx)
    state = get_state()

    # Fetch from the HTTP daemon for fresh state (daemon may have connections
    # the MCP session's in-memory state doesn't know about yet, e.g. bootstrap)
    import httpx
    from darkmatter.config import DEFAULT_PORT
    import os
    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/__darkmatter__/connections")
            if resp.status_code == 200:
                daemon_data = resp.json()
                daemon_conns = daemon_data.get("connections", [])
                # Extract trust scores from daemon's impression data
                for c in daemon_conns:
                    imp_data = c.pop("impression", None)
                    c["trust_score"] = round(imp_data["score"], 4) if imp_data else 0.0
                # Sort by last_activity descending
                daemon_conns.sort(key=lambda c: c.get("last_activity") or "", reverse=True)
                daemon_conns = daemon_conns[:100]
                return json.dumps({"count": len(daemon_conns), "connections": daemon_conns})
    except Exception:
        pass  # Fall back to in-memory state

    conns = []
    for aid, conn in state.connections.items():
        imp = state.impressions.get(aid)
        conns.append({
            "agent_id": aid,
            "display_name": conn.agent_display_name or aid[:12] + "...",
            "bio": (conn.agent_bio or "")[:200],
            "agent_url": conn.agent_url,
            "trust_score": round(imp.score, 4) if imp else 0.0,
            "infrastructure": imp.infrastructure if imp else False,
            "wallets": conn.wallets,
            "connected_at": conn.connected_at,
            "last_activity": conn.last_activity,
            "messages_sent": conn.messages_sent,
            "messages_received": conn.messages_received,
            "connectivity_level": conn.connectivity_level,
            "connectivity_method": conn.connectivity_method,
        })

    # Sort by last_activity descending (most recent first)
    conns.sort(key=lambda c: c.get("last_activity") or "", reverse=True)
    conns = conns[:100]

    return json.dumps({
        "count": len(conns),
        "connections": conns,
    })


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
    state = get_state()

    conn = state.connections.get(input.agent_id)
    if conn is None:
        return json.dumps({"success": False, "error": "Not connected to that agent"})

    try:
        result = await send_to_peer(conn, "/__darkmatter__/get_peers", {"n": input.n})
        data = result
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})

    # Filter out self and already-connected peers
    new_peers = []
    already_known = []
    for peer in data.get("peers", []):
        pid = peer.get("agent_id", "")
        if pid == state.agent_id:
            continue
        if pid in state.connections:
            already_known.append(peer)
        else:
            new_peers.append(peer)

    return json.dumps({
        "success": True,
        "source_agent_id": input.agent_id,
        "source_display_name": data.get("display_name", ""),
        "source_peer_count": data.get("peer_count", 0),
        "new_peers": new_peers,
        "already_connected": already_known,
    })


# NOTE: Tools removed from MCP and moved to HTTP API + skill:
# get_identity, list_connections, list_pending_requests, set_status,
# list_inbox, get_message, list_messages, get_sent_message, expire_message,
# wait_for_response, network_info, discover_domain,
# set_impression, get_impression, set_superagent, set_rate_limit,
# wallet, send_payment, get_balance, wallet_balances, wallet_send,
# genome_info, genome_install
# Access these via: curl localhost:PORT/__darkmatter__/<endpoint>
# Wallet operations: see .claude/skills/darkmatter-wallet/SKILL.md
# Other operations: see .claude/skills/darkmatter-ops/SKILL.md


# =============================================================================
# Insight Tools
# =============================================================================

async def _push_insight_to_peers(state, insight) -> list[str]:
    """Push an insight to all qualifying connected peers. Returns list of agent IDs pushed to."""
    pushed_to = []
    from darkmatter.security import sign_insight
    # Re-sign with current content
    tags_str = ",".join(sorted(insight.tags))
    insight.signature_hex = sign_insight(
        state.private_key_hex, insight.insight_id, state.agent_id, insight.content, tags_str,
    )
    insight_payload = {
        "insight_id": insight.insight_id,
        "author_agent_id": insight.author_agent_id,
        "content": insight.content,
        "tags": insight.tags,
        "share_with_top_n": insight.share_with_top_n,
        "created_at": insight.created_at,
        "updated_at": insight.updated_at,
        "summary": insight.summary,
        "signature_hex": insight.signature_hex,
        "file": insight.file,
        "from_text": insight.from_text,
        "to_text": insight.to_text,
        "function_anchor": insight.function_anchor,
        "original_content": insight.original_content,
        "original_hash": insight.original_hash,
    }

    # Determine which peers to push to based on share_with_top_n
    if insight.share_with_top_n == -1:
        # All peers
        eligible = list(state.connections.items())
    else:
        # Rank peers by trust score descending, pick top N
        ranked = sorted(
            state.connections.items(),
            key=lambda item: (state.impressions.get(item[0]).score if state.impressions.get(item[0]) else 0.0),
            reverse=True,
        )
        eligible = ranked[:insight.share_with_top_n]

    import asyncio

    async def _push_one(aid, conn):
        try:
            await send_to_peer(conn, "/__darkmatter__/insight_push", insight_payload)
            return aid
        except Exception as e:
            _log.warning("Failed to push insight %s to peer %s: %s", insight.insight_id, aid, e)
            return None

    results = await asyncio.gather(*[_push_one(aid, conn) for aid, conn in eligible])
    return [aid for aid in results if aid is not None]


@mcp.tool(
    name="darkmatter_create_insight",
    annotations={
        "title": "Create Insight",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def create_insight(params: CreateInsightInput, ctx: Context) -> str:
    """Create a live code insight anchored to a file region, and push to qualifying peers.

    Content is resolved live from the file. When the code changes,
    updates are automatically pushed to peers on next view.

    Prefer raw code over summaries. Only add a summary for very long code regions
    where the full content would waste context. For most insights, skip summary.
    """
    track_session(ctx)
    state = get_state()

    from darkmatter.config import OWN_INSIGHT_MAX
    own_insights = [s for s in state.insights if s.author_agent_id == state.agent_id]
    if len(own_insights) >= OWN_INSIGHT_MAX:
        # Auto-prune oldest own insight instead of rejecting
        oldest = min(own_insights, key=lambda s: s.created_at)
        state.insights.remove(oldest)

    from darkmatter.insight_resolver import resolve_region, hash_content
    from pathlib import Path
    # Resolve file path
    p = Path(params.file)
    file_path = str(p) if p.is_absolute() else str(Path.cwd() / params.file)

    if not Path(file_path).exists():
        return json.dumps({"success": False, "error": f"File not found: {params.file}"})

    region = resolve_region(file_path, params.from_text, params.to_text)
    if region is None:
        return json.dumps({
            "success": False,
            "error": f"Could not find region in {params.file}. "
                     f"Make sure from_text ('{params.from_text[:50]}') appears in the file."
        })

    original_content = region.content
    original_hash = hash_content(original_content)
    content = original_content  # stored content = resolved snapshot

    now = datetime.now(timezone.utc).isoformat()
    from darkmatter.security import sign_insight

    # Upsert: if an insight exists for same file+from_text, replace it
    insight_id = f"insight-{uuid.uuid4().hex[:12]}"
    was_update = False
    for i, existing in enumerate(state.insights):
        if (existing.author_agent_id == state.agent_id
                and existing.file == params.file
                and existing.from_text == params.from_text):
            insight_id = existing.insight_id
            was_update = True
            state.insights.pop(i)
            break

    tags_str = ",".join(sorted(params.tags))
    sig = sign_insight(state.private_key_hex, insight_id, state.agent_id, content, tags_str)

    insight = Insight(
        insight_id=insight_id,
        author_agent_id=state.agent_id,
        content=content,
        tags=params.tags,
        share_with_top_n=params.share_with_top_n,
        created_at=now,
        updated_at=now,
        summary=params.summary,
        signature_hex=sig,
        file=params.file,
        from_text=params.from_text,
        to_text=params.to_text,
        function_anchor=region.function_anchor or "",
        original_content=original_content,
        original_hash=original_hash,
    )
    state.insights.append(insight)
    save_state()

    pushed_to = await _push_insight_to_peers(state, insight)

    result = {
        "success": True,
        "insight_id": insight.insight_id,
        "action": "updated" if was_update else "created",
        "tags": insight.tags,
        "share_with_top_n": insight.share_with_top_n,
        "pushed_to": pushed_to,
    }
    result["file"] = params.file
    result["lines"] = f"{region.start_line}-{region.end_line}"
    if region.function_anchor:
        result["function_anchor"] = region.function_anchor

    return json.dumps(result)


@mcp.tool(
    name="darkmatter_view_insights",
    annotations={
        "title": "View Insights",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def view_insights(params: ViewInsightsInput, ctx: Context) -> str:
    """Query insights by tags, author, and/or file. Returns local + cached peer insights.

    Code insights resolve live content from files and include health status.
    Remote code insights show the last-known snapshot.
    """
    track_session(ctx)
    state = get_state()

    # Sync peer insights from daemon (disk) — daemon may have received
    # insight_push from peers that this MCP session doesn't have in memory
    sync_peer_insights_from_disk()

    # No filters → return lightweight index (tags + files) instead of full content
    if not params.tags and not params.author and not params.file:
        tag_counts: dict[str, int] = {}
        file_counts: dict[str, int] = {}
        for insight in state.insights:
            for tag in (insight.tags or []):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
            if insight.file:
                file_counts[insight.file] = file_counts.get(insight.file, 0) + 1
        return json.dumps({
            "success": True,
            "mode": "index",
            "total_insights": len(state.insights),
            "tags": tag_counts,
            "files": file_counts,
            "hint": "Pass tags=[], author=, or file= to view actual insight content.",
        })

    from darkmatter.insight_resolver import resolve_region, assess_health, hash_content
    from pathlib import Path

    results = []
    to_delete = []
    to_push = []  # insights whose content changed — push updates to peers

    for insight in state.insights:
        # Filter by tags
        if params.tags:
            if not any(
                st == qt or st.startswith(qt + ":")
                for qt in params.tags
                for st in insight.tags
            ):
                continue
        # Filter by author
        if params.author and insight.author_agent_id != params.author:
            continue
        # Filter by file
        if params.file and insight.file != params.file:
            continue

        is_local = insight.author_agent_id == state.agent_id

        entry = {
            "insight_id": insight.insight_id,
            "author": insight.author_agent_id,
            "tags": insight.tags,
            "share_with_top_n": insight.share_with_top_n,
            "created_at": insight.created_at,
            "updated_at": insight.updated_at,
            "file": insight.file,
            "type": "code",
        }

        if is_local and insight.from_text and insight.to_text:
            # Resolve live content from file
            p = Path(insight.file)
            file_path = str(p) if p.is_absolute() else str(Path.cwd() / insight.file)
            region = resolve_region(
                file_path, insight.from_text, insight.to_text,
                function_anchor=insight.function_anchor,
            )
            current_content = region.content if region else None

            if insight.original_content and insight.original_hash:
                health = assess_health(
                    insight.original_content, insight.original_hash,
                    current_content, insight.stale_views,
                )
                entry["health"] = {"score": health.score, "status": health.status, "message": health.message}

                if region:
                    entry["lines"] = f"{region.start_line}-{region.end_line}"

                # If content changed, update the insight and queue for push
                if current_content and hash_content(current_content) != insight.original_hash:
                    insight.content = current_content
                    insight.original_content = current_content
                    insight.original_hash = hash_content(current_content)
                    insight.updated_at = datetime.now(timezone.utc).isoformat()
                    insight.stale_views = 0
                    if region:
                        insight.function_anchor = region.function_anchor
                    to_push.append(insight)

                # Show content (raw code preferred)
                if insight.summary and not params.raw:
                    entry["summary"] = insight.summary
                else:
                    entry["content"] = current_content or "[Could not resolve]"

                if health.should_delete():
                    to_delete.append(insight.insight_id)
                    entry["expired"] = True
                elif health.status in ("stale", "degraded"):
                    insight.stale_views += 1
            else:
                # Insight without original tracking — show content
                entry["content"] = current_content or insight.content
        else:
            # Remote insight — show last-known snapshot
            entry["cached"] = True
            if insight.summary and not params.raw:
                entry["summary"] = insight.summary
            else:
                entry["content"] = insight.content[:500]
                if len(insight.content) > 500:
                    entry["truncated"] = True

        # Label if from peer
        if not is_local:
            conn = state.connections.get(insight.author_agent_id)
            if conn:
                entry["author_name"] = conn.agent_display_name or insight.author_agent_id[:12]
            if "cached" not in entry:
                entry["cached"] = True

        results.append(entry)

    # Delete expired insights
    if to_delete:
        state.insights = [s for s in state.insights if s.insight_id not in set(to_delete)]

    # Push updated code insights to peers
    pushed_updates = []
    for insight in to_push:
        if insight.insight_id not in set(to_delete):
            peers = await _push_insight_to_peers(state, insight)
            if peers:
                pushed_updates.append({"insight_id": insight.insight_id, "pushed_to": peers})

    if to_delete or to_push:
        save_state()

    response = {"success": True, "count": len(results), "insights": results}
    if to_delete:
        response["expired_deleted"] = to_delete
    if pushed_updates:
        response["pushed_updates"] = pushed_updates

    return json.dumps(response)



# =============================================================================
# Wait for Message Tool
# =============================================================================

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

    Use darkmatter_send_message(broadcast=True) for passive status updates to peers.
    """
    state = get_state()
    from darkmatter.config import DEFAULT_PORT
    daemon_port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))

    # Check if matching messages already exist — prefer daemon HTTP inbox
    _sync_inbox_from_daemon(state, daemon_port)
    existing = _drain_inbox(state, from_agents)
    if existing:
        _consume_via_daemon(daemon_port, [m["message_id"] for m in existing])
        return json.dumps({"success": True, "messages": existing, "waited": False, "_reminder": "insights"})

    # Register event and wait for new message
    event = asyncio.Event()
    state._inbox_events.append(event)
    state._is_waiting = True
    set_waiting(True)

    _log.info("wait_for_message: waiting (timeout=%ds, filter=%s)", int(timeout_seconds), from_agents or "any")

    try:
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            event.clear()
            if event not in state._inbox_events:
                state._inbox_events.append(event)
            try:
                await asyncio.wait_for(event.wait(), timeout=min(_WAIT_POLL_INTERVAL, remaining))
            except asyncio.TimeoutError:
                if asyncio.get_event_loop().time() >= deadline:
                    raise

            # Woke up — sync from daemon (or disk fallback) and check for matches
            _sync_inbox_from_daemon(state, daemon_port)
            matched = _drain_inbox(state, from_agents)
            if matched:
                _log.info("wait_for_message: matched %d message(s)", len(matched))
                if event in state._inbox_events:
                    state._inbox_events.remove(event)
                _consume_via_daemon(daemon_port, [m["message_id"] for m in matched])
                return json.dumps({"success": True, "messages": matched, "waited": True, "_reminder": "insights"})

    except asyncio.TimeoutError:
        mins = int(timeout_seconds / 60)
        filter_desc = f" from {from_agents}" if from_agents else ""
        _log.info("wait_for_message: timed out after %d min%s", mins, filter_desc)
        return json.dumps({
            "success": False,
            "timed_out": True,
            "error": f"No message{filter_desc} received after {mins} minutes.",
            "action": "Proactively reach out to peers, share updates, or broadcast. Then resume listening with darkmatter_wait_for_message.",
        })
    finally:
        state._is_waiting = False
        set_waiting(False)
        if event in state._inbox_events:
            state._inbox_events.remove(event)


def _drain_inbox(state, from_agents: Optional[list[str]] = None) -> list[dict]:
    """Return and consume inbox messages matching the agent filter."""
    matched_ids = []
    for msg in state.message_queue:
        if from_agents and msg.from_agent_id not in from_agents:
            continue
        matched_ids.append(msg.message_id)
    if not matched_ids:
        return []
    return _consume_queue_messages(state, matched_ids)


# Genome tools moved to HTTP API + skill (see /__darkmatter__/genome)

"""
All MCP tool definitions for the DarkMatter mesh protocol.

Depends on: mcp/__init__, mcp/schemas, config, models, identity, state,
            wallet, network, spawn
"""

import asyncio
import json
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
    BeginMessageInput,
    EndMessageInput,
    UpdateBioInput,
    CreateShardInput,
    ViewShardsInput,
    CompleteAndSummarizeInput,
)
from darkmatter.state import get_state, save_state, sync_message_queue_from_disk, consume_message
from darkmatter.context import log_conversation
from darkmatter.config import (
    MAX_CONNECTIONS,
    TRUST_MESSAGE_SENT,
)
from darkmatter.identity import (
    validate_url,
)
from darkmatter.security import sign_message, prepare_outbound
from darkmatter.models import (
    AgentStatus,
    Connection,
    SharedShard,
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
)


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
                    print(f"[DarkMatter] Warning: failed to send identity proof: {e}", file=sys.stderr)

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
        print(f"[DarkMatter] Warning: disconnect announcement failed for {agent_id}: {e}", file=sys.stderr)
        # Fallback: just delete the connection
        if agent_id in state.connections:
            del state.connections[agent_id]
    save_state()

    return json.dumps({
        "success": True,
        "disconnected_from": agent_id,
    })


# In-flight messages started by begin_message, keyed by message_id
_pending_streams: dict[str, dict] = {}


def _get_stream_url(conn, state) -> str | None:
    """Get the best URL for chunk streaming to a peer.

    Returns a local/LAN URL if available (cheap HTTP POSTs), None otherwise.
    Checks agent_url first, then discovered_peers for a LAN URL that the
    agent_url (which may be a public UPnP address) doesn't reveal.
    """
    from darkmatter.network.manager import is_local_url
    url = getattr(conn, "agent_url", "") or ""
    if is_local_url(url):
        return url
    if conn.agent_id in getattr(state, "discovered_peers", {}):
        peer_url = state.discovered_peers[conn.agent_id].get("url", "")
        if is_local_url(peer_url):
            return peer_url
    return None


def _is_streamable(conn, state=None) -> bool:
    """Check if a connection supports chunk streaming (WebRTC or LAN HTTP)."""
    # WebRTC data channel = true streaming
    ch = getattr(conn, "webrtc_channel", None)
    if ch is not None and getattr(ch, "readyState", None) == "open":
        return True
    # LAN/local HTTP = fine for chunks (low latency, negligible cost)
    if _get_stream_url(conn, state):
        return True
    # Remote HTTP = skip chunks (one POST per 512-byte chunk is wasteful)
    return False


# Shared httpx client for chunk delivery (avoids creating one per chunk)
_chunk_client: httpx.AsyncClient | None = None


def _get_chunk_client() -> httpx.AsyncClient:
    global _chunk_client
    if _chunk_client is None or _chunk_client.is_closed:
        _chunk_client = httpx.AsyncClient(timeout=httpx.Timeout(2.0))
    return _chunk_client


async def _send_chunk(state, message_id: str, content: str, targets: list) -> None:
    """Send a chunk signal to streamable targets only (WebRTC or LAN HTTP)."""
    payload = {
        "message_id": message_id,
        "type": "chunk",
        "from_agent_id": state.agent_id,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    for conn in targets:
        if not _is_streamable(conn, state):
            print(f"[DarkMatter] CHUNK: not streamable for {getattr(conn, 'display_name', '?')}", file=sys.stderr)
            continue
        try:
            # WebRTC: use transport manager
            ch = getattr(conn, "webrtc_channel", None)
            if ch is not None and getattr(ch, "readyState", None) == "open":
                await send_to_peer(conn, "/__darkmatter__/message_stream", payload)
                continue
            # LAN HTTP: POST directly to LAN URL (not the public UPnP URL)
            lan_url = _get_stream_url(conn, state)
            if lan_url:
                client = _get_chunk_client()
                print(f"[DarkMatter] CHUNK: sending {len(content)} chars to {lan_url}", file=sys.stderr)
                await client.post(f"{lan_url}/__darkmatter__/message_stream", json=payload)
        except Exception as e:
            print(f"[DarkMatter] CHUNK: error sending: {e}", file=sys.stderr)


async def _begin_message(state, params: BeginMessageInput) -> str:
    """Start composing a message. Sends typing indicator to the target(s)."""
    message_id = f"msg-{uuid.uuid4().hex[:12]}"
    metadata = params.metadata or {}

    # Resolve targets — explicit list, single ID, or auto-select
    if params.target_agent_ids:
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

    # Send typing indicator to targets (best-effort)
    signal_payload = {
        "message_id": message_id,
        "type": "begin",
        "from_agent_id": state.agent_id,
        "from_public_key_hex": state.public_key_hex,
        "from_display_name": state.display_name,
        "in_reply_to": params.in_reply_to,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata,
    }
    signaled = []
    for conn in targets:
        try:
            await send_to_peer(conn, "/__darkmatter__/message_stream", signal_payload)
            signaled.append(conn.agent_id)
        except Exception:
            pass

    # Consume forwarded messages from the queue
    forwarded_msgs = []
    if params.forward_message_ids:
        sync_message_queue_from_disk()
        forwarded_msgs = _consume_queue_messages(state, params.forward_message_ids)
        not_found = set(params.forward_message_ids) - {m["message_id"] for m in forwarded_msgs}
        if not_found:
            return json.dumps({
                "success": False,
                "error": f"Messages not found in queue: {list(not_found)}"
            })

    # Store pending stream state
    _pending_streams[message_id] = {
        "from_agent_id": state.agent_id,
        "targets": [c.agent_id for c in targets],
        "in_reply_to": params.in_reply_to,
        "hops_remaining": params.hops_remaining,
        "metadata": metadata,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "_content": [],  # accumulate chunks for delivery on end
        "_forwarded_msgs": forwarded_msgs,  # messages to include in delivery
    }

    # Hook into spawned agent's stdout to stream chunks to streamable targets
    if params.in_reply_to:
        from darkmatter.spawn import get_spawned_agents
        for agent in get_spawned_agents():
            if agent.message_id == params.in_reply_to:
                async def _on_chunk(chunk: str, _mid=message_id, _st=state, _tgts=targets):
                    _pending_streams[_mid]["_content"].append(chunk)
                    await _send_chunk(_st, _mid, chunk, _tgts)
                agent.stdout_callbacks[message_id] = _on_chunk
                _pending_streams[message_id]["_spawned_agent"] = agent
                break

    # Hint based on whether any target is streamable (human/entrypoint)
    has_streamable = any(_is_streamable(c, state) for c in targets)
    if has_streamable:
        style = "Your output streams live to a human. Write naturally, use detail, include tool calls and reasoning — they see it all in real time. Longer is better."
    else:
        style = "Sending to another agent. Be concise and direct."

    result = {
        "success": True,
        "message_id": message_id,
        "targets": [c.agent_id for c in targets],
        "style": style,
        "next_step": f"Write your response, then call darkmatter_end_message(message_id='{message_id}').",
    }
    if forwarded_msgs:
        result["forwarding"] = [
            {"message_id": m["message_id"], "from": m["from_agent_id"], "content": m["content"][:200]}
            for m in forwarded_msgs
        ]
    return json.dumps(result)


async def _end_message(state, params: EndMessageInput) -> str:
    """Signal that streaming is complete. Sends end signal and logs to history."""
    stream_info = _pending_streams.pop(params.message_id, None)

    # Stop stdout streaming for this message (other nested streams continue)
    spawned = stream_info.get("_spawned_agent") if stream_info else None
    if spawned:
        spawned.stdout_callbacks.pop(params.message_id, None)

    if not stream_info:
        return json.dumps({
            "success": False,
            "error": f"No pending message with ID '{params.message_id}'. Call begin_message first.",
        })

    metadata = stream_info.get("metadata") or {}
    in_reply_to = stream_info.get("in_reply_to")
    forwarded_msgs = stream_info.get("_forwarded_msgs", [])

    # Deliver to the targets set at begin_message time
    targets = []
    for tid in stream_info["targets"]:
        conn = state.connections.get(tid)
        if conn:
            targets.append(conn)
    msg_type = "broadcast" if len(targets) > 1 else "direct"

    msg_timestamp = datetime.now(timezone.utc).isoformat()

    # Collect accumulated content from streaming
    content_parts = stream_info.get("_content", [])
    full_content = "\n\n".join(content_parts).strip() if content_parts else "(no content)"

    # Build forwarded content section if any
    if forwarded_msgs:
        fwd_sections = []
        for fwd in forwarded_msgs:
            fwd_sections.append(f"[Forwarded from {fwd['from_agent_id'][:12]}]: {fwd['content']}")
        fwd_block = "\n\n".join(fwd_sections)
        if full_content != "(no content)":
            full_content = f"{full_content}\n\n---\n{fwd_block}"
        else:
            full_content = fwd_block
        metadata["forwarded"] = True
        metadata["forwarded_by"] = state.agent_id
        metadata["forwarded_message_ids"] = [m["message_id"] for m in forwarded_msgs]

    sent_to = []
    for conn in targets:
        try:
            if _is_streamable(conn, state):
                # Streamable target (entrypoint): send end signal — they have the chunks
                end_signal = {
                    "message_id": params.message_id,
                    "type": "end",
                    "from_agent_id": state.agent_id,
                    "timestamp": msg_timestamp,
                }
                await send_to_peer(conn, "/__darkmatter__/message_stream", end_signal)
            else:
                # Non-streamable target (agent): deliver the full message directly
                hops = stream_info.get("hops_remaining", 10)
                if forwarded_msgs:
                    # Use lowest hops from forwarded messages
                    hops = min(m.get("hops_remaining", 10) for m in forwarded_msgs)
                    hops = max(0, hops - 1)
                msg_payload = {
                    "message_id": params.message_id,
                    "content": full_content,
                    "hops_remaining": hops,
                    "metadata": metadata,
                    "timestamp": msg_timestamp,
                    "in_reply_to": in_reply_to,
                }
                envelope = prepare_outbound(
                    msg_payload, state.private_key_hex,
                    state.agent_id, state.public_key_hex,
                )
                await send_to_peer(conn, "/__darkmatter__/message", envelope.payload)
            conn.messages_sent += 1
            conn.last_activity = datetime.now(timezone.utc).isoformat()
            sent_to.append(conn.agent_id)
            adjust_trust(state, conn.agent_id, TRUST_MESSAGE_SENT)
        except Exception:
            pass

    # Log conversation with actual content
    entry_type = "forward" if forwarded_msgs else msg_type
    if sent_to:
        log_conversation(
            state, params.message_id, full_content,
            from_id=state.agent_id, to_ids=sent_to,
            entry_type=entry_type, direction="outbound",
            metadata=metadata,
        )

    save_state()

    # Kill the spawned agent process — it's done with this message.
    # Without -p mode, Claude runs interactively and won't exit on its own.
    if spawned:
        try:
            spawned.process.terminate()
        except Exception:
            pass

    result = {
        "success": len(sent_to) > 0,
        "message_id": params.message_id,
        "routed_to": sent_to,
    }
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
    """Manage connections. Actions: request (target_url), accept/reject (request_id), disconnect (agent_id)."""
    state = get_state()

    if params.action == ConnectionAction.REQUEST:
        if not params.target_url:
            return json.dumps({"success": False, "error": "target_url is required for request."})
        return await _connection_request(state, params.target_url)

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
    name="darkmatter_begin_message",
    annotations={
        "title": "Begin Composing Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def begin_message(params: BeginMessageInput, ctx: Context) -> str:
    """Start composing a message. Call this EARLY — respond fast. Follow the style hint in the response. Call end_message when done."""
    track_session(ctx)
    state = get_state()
    return await _begin_message(state, params)


@mcp.tool(
    name="darkmatter_end_message",
    annotations={
        "title": "Complete Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def end_message(params: EndMessageInput, ctx: Context) -> str:
    """Finish and deliver the message. Set broadcast=true to send to all peers instead of just the original target."""
    track_session(ctx)
    state = get_state()
    return await _end_message(state, params)



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
        print(f"[DarkMatter] Failed to broadcast update: {e}", file=sys.stderr)

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


# NOTE: Tools removed from MCP and moved to HTTP API + skill:
# get_identity, list_connections, list_pending_requests, set_status,
# list_inbox, get_message, list_messages, get_sent_message, expire_message,
# wait_for_response, network_info, discover_domain,
# set_impression, get_impression, set_superagent, set_rate_limit,
# get_balance, send_sol, send_token, wallet_balances, wallet_send,
# genome_info, genome_install
# Access these via: curl localhost:PORT/__darkmatter__/<endpoint>
# See .claude/skills/darkmatter-ops/SKILL.md for documentation.

_REMOVED_TOOL_MARKER = True  # noqa: F841 — placeholder for removed tools


# REMOVED: set_status, get_identity, list_connections, list_pending_requests,
# list_inbox, get_message, list_messages, get_sent_message, expire_message,
# wait_for_response, network_info, discover_domain,
# set_impression, get_impression, set_superagent, set_rate_limit,
# get_balance, send_sol, send_token, wallet_balances, wallet_send

# =============================================================================
# Shared Shards Tools
# =============================================================================

async def _push_shard_to_peers(state, shard) -> list[str]:
    """Push a shard to all qualifying connected peers. Returns list of agent IDs pushed to."""
    pushed_to = []
    from darkmatter.security import sign_shard
    # Re-sign with current content
    tags_str = ",".join(sorted(shard.tags))
    shard.signature_hex = sign_shard(
        state.private_key_hex, shard.shard_id, state.agent_id, shard.content, tags_str,
    )
    shard_payload = {
        "shard_id": shard.shard_id,
        "author_agent_id": shard.author_agent_id,
        "content": shard.content,
        "tags": shard.tags,
        "share_with_top_n": shard.share_with_top_n,
        "created_at": shard.created_at,
        "updated_at": shard.updated_at,
        "summary": shard.summary,
        "signature_hex": shard.signature_hex,
        "file": shard.file,
        "from_text": shard.from_text,
        "to_text": shard.to_text,
        "function_anchor": shard.function_anchor,
        "original_content": shard.original_content,
        "original_hash": shard.original_hash,
    }

    # Determine which peers to push to based on share_with_top_n
    if shard.share_with_top_n == -1:
        # All peers
        eligible = list(state.connections.items())
    else:
        # Rank peers by trust score descending, pick top N
        ranked = sorted(
            state.connections.items(),
            key=lambda item: (state.impressions.get(item[0]).score if state.impressions.get(item[0]) else 0.0),
            reverse=True,
        )
        eligible = ranked[:shard.share_with_top_n]

    for aid, conn in eligible:
        try:
            await send_to_peer(conn, "/__darkmatter__/shard_push", shard_payload)
            pushed_to.append(aid)
        except Exception as e:
            print(f"[DarkMatter] Warning: failed to push shard {shard.shard_id} to peer {aid}: {e}", file=sys.stderr)
    return pushed_to


@mcp.tool(
    name="darkmatter_create_shard",
    annotations={
        "title": "Create Shared Shard",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def create_shard(params: CreateShardInput, ctx: Context) -> str:
    """Create a live code shard anchored to a file region, and push to qualifying peers.

    Content is resolved live from the file. When the code changes,
    updates are automatically pushed to peers on next view.

    Prefer raw code over summaries. Only add a summary for very long code regions
    where the full content would waste context. For most shards, skip summary.
    """
    track_session(ctx)
    state = get_state()

    from darkmatter.config import SHARED_SHARD_MAX
    if len(state.shared_shards) >= SHARED_SHARD_MAX:
        return json.dumps({"success": False, "error": f"Shard limit reached ({SHARED_SHARD_MAX})"})

    from darkmatter.shard_resolver import resolve_region, hash_content
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
    from darkmatter.security import sign_shard

    # Upsert: if a shard exists for same file+from_text, replace it
    shard_id = f"shard-{uuid.uuid4().hex[:12]}"
    was_update = False
    for i, existing in enumerate(state.shared_shards):
        if (existing.author_agent_id == state.agent_id
                and existing.file == params.file
                and existing.from_text == params.from_text):
            shard_id = existing.shard_id
            was_update = True
            state.shared_shards.pop(i)
            break

    tags_str = ",".join(sorted(params.tags))
    sig = sign_shard(state.private_key_hex, shard_id, state.agent_id, content, tags_str)

    shard = SharedShard(
        shard_id=shard_id,
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
    state.shared_shards.append(shard)
    save_state()

    pushed_to = await _push_shard_to_peers(state, shard)

    result = {
        "success": True,
        "shard_id": shard.shard_id,
        "action": "updated" if was_update else "created",
        "tags": shard.tags,
        "share_with_top_n": shard.share_with_top_n,
        "pushed_to": pushed_to,
    }
    result["file"] = params.file
    result["lines"] = f"{region.start_line}-{region.end_line}"
    if region.function_anchor:
        result["function_anchor"] = region.function_anchor

    return json.dumps(result)


@mcp.tool(
    name="darkmatter_view_shards",
    annotations={
        "title": "View Shared Shards",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def view_shards(params: ViewShardsInput, ctx: Context) -> str:
    """Query shards by tags, author, and/or file. Returns local + cached peer shards.

    Code shards resolve live content from files and include health status.
    Remote code shards show the last-known snapshot.
    """
    track_session(ctx)
    state = get_state()

    from darkmatter.shard_resolver import resolve_region, assess_health, hash_content
    from pathlib import Path

    results = []
    to_delete = []
    to_push = []  # shards whose content changed — push updates to peers

    for shard in state.shared_shards:
        # Filter by tags
        if params.tags:
            if not any(
                st == qt or st.startswith(qt + ":")
                for qt in params.tags
                for st in shard.tags
            ):
                continue
        # Filter by author
        if params.author and shard.author_agent_id != params.author:
            continue
        # Filter by file
        if params.file and shard.file != params.file:
            continue

        is_local = shard.author_agent_id == state.agent_id

        entry = {
            "shard_id": shard.shard_id,
            "author": shard.author_agent_id,
            "tags": shard.tags,
            "share_with_top_n": shard.share_with_top_n,
            "created_at": shard.created_at,
            "updated_at": shard.updated_at,
            "file": shard.file,
            "type": "code",
        }

        if is_local and shard.from_text and shard.to_text:
            # Resolve live content from file
            p = Path(shard.file)
            file_path = str(p) if p.is_absolute() else str(Path.cwd() / shard.file)
            region = resolve_region(
                file_path, shard.from_text, shard.to_text,
                function_anchor=shard.function_anchor,
            )
            current_content = region.content if region else None

            if shard.original_content and shard.original_hash:
                health = assess_health(
                    shard.original_content, shard.original_hash,
                    current_content, shard.stale_views,
                )
                entry["health"] = {"score": health.score, "status": health.status, "message": health.message}

                if region:
                    entry["lines"] = f"{region.start_line}-{region.end_line}"

                # If content changed, update the shard and queue for push
                if current_content and hash_content(current_content) != shard.original_hash:
                    shard.content = current_content
                    shard.original_content = current_content
                    shard.original_hash = hash_content(current_content)
                    shard.updated_at = datetime.now(timezone.utc).isoformat()
                    shard.stale_views = 0
                    if region:
                        shard.function_anchor = region.function_anchor
                    to_push.append(shard)

                # Show content (raw code preferred)
                if shard.summary and not params.raw:
                    entry["summary"] = shard.summary
                else:
                    entry["content"] = current_content or "[Could not resolve]"

                if health.should_delete():
                    to_delete.append(shard.shard_id)
                    entry["expired"] = True
                elif health.status in ("stale", "degraded"):
                    shard.stale_views += 1
            else:
                # Shard without original tracking — show content
                entry["content"] = current_content or shard.content
        else:
            # Remote shard — show last-known snapshot
            entry["cached"] = True
            if shard.summary and not params.raw:
                entry["summary"] = shard.summary
            else:
                entry["content"] = shard.content[:500]
                if len(shard.content) > 500:
                    entry["truncated"] = True

        # Label if from peer
        if not is_local:
            conn = state.connections.get(shard.author_agent_id)
            if conn:
                entry["author_name"] = conn.agent_display_name or shard.author_agent_id[:12]
            if "cached" not in entry:
                entry["cached"] = True

        results.append(entry)

    # Delete expired shards
    if to_delete:
        state.shared_shards = [s for s in state.shared_shards if s.shard_id not in set(to_delete)]

    # Push updated code shards to peers
    pushed_updates = []
    for shard in to_push:
        if shard.shard_id not in set(to_delete):
            peers = await _push_shard_to_peers(state, shard)
            if peers:
                pushed_updates.append({"shard_id": shard.shard_id, "pushed_to": peers})

    if to_delete or to_push:
        save_state()

    response = {"success": True, "count": len(results), "shards": results}
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
    timeout_seconds: float = 900,
    ctx: Context = None,
) -> str:
    """Block until a new inbox message arrives. Consumes and returns all matching messages. Optional from_agents filter."""
    state = get_state()

    # Check if matching messages already exist in inbox
    sync_message_queue_from_disk()
    existing = _drain_inbox(state, from_agents)
    if existing:
        return json.dumps({
            "success": True,
            "messages": existing,
            "waited": False,
        })

    # Register event and wait for new message
    event = asyncio.Event()
    state._inbox_events.append(event)

    try:
        # Loop: wake on any inbox event, then check filter
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            event.clear()
            if event not in state._inbox_events:
                state._inbox_events.append(event)
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise

            # Woke up — check if a matching message arrived
            sync_message_queue_from_disk()
            matched = _drain_inbox(state, from_agents)
            if matched:
                if event in state._inbox_events:
                    state._inbox_events.remove(event)
                return json.dumps({
                    "success": True,
                    "messages": matched,
                    "waited": True,
                })

    except asyncio.TimeoutError:
        if event in state._inbox_events:
            state._inbox_events.remove(event)
        mins = int(timeout_seconds / 60)
        filter_desc = f" from {from_agents}" if from_agents else ""
        return json.dumps({
            "success": False,
            "timed_out": True,
            "error": f"No message{filter_desc} received after {mins} minutes.",
            "action": "Proactively reach out to peers, share updates, or broadcast. Then resume listening with darkmatter_wait_for_message.",
        })


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

# =============================================================================
# Complete and Summarize Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_complete_and_summarize",
    annotations={
        "title": "Complete & Summarize",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def complete_and_summarize(params: CompleteAndSummarizeInput, ctx: Context = None) -> str:
    """MANDATORY when your task is done. Summarize what you did, what you learned, and what the hivemind should know. Uses @agent_id to reference peers. This terminates your session and spawns a fresh warm agent."""
    state = get_state()

    summary_id = f"summary-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()

    # Build metadata
    meta = {
        "type": "summary",
        "shard_tags": params.shard_tags,
        "share_with_top_n": params.share_with_top_n,
    }

    # Identify where this session started in the conversation log.
    # The session HWM was set when get_context(mode="full") ran at spawn time.
    session_id = "default"
    try:
        session_id = str(id(ctx.session))
    except Exception:
        pass
    from darkmatter.context import _session_context_hwm
    session_start_idx = _session_context_hwm.get(session_id, 0)

    # Log the summary as a conversation entry
    log_conversation(
        state, summary_id, params.summary,
        from_id=state.agent_id, to_ids=[],
        entry_type="summary", direction="outbound",
        metadata=meta,
    )

    # Prune non-summary entries created during this session only.
    # Entries before session_start_idx are from prior sessions — keep them.
    # The summary replaces this session's raw message history.
    before_session = state.conversation_log[:session_start_idx]
    during_session = state.conversation_log[session_start_idx:]
    during_session = [e for e in during_session if e.entry_type == "summary"]
    state.conversation_log = before_session + during_session
    save_state()

    # Push summary to qualifying peers
    pushed_to = []
    if state.connections:
        summary_payload = {
            "message_id": summary_id,
            "content": params.summary,
            "metadata": meta,
            "timestamp": now,
        }
        envelope = prepare_outbound(
            summary_payload, state.private_key_hex,
            state.agent_id, state.public_key_hex,
        )

        if params.share_with_top_n == -1:
            eligible = list(state.connections.items())
        else:
            ranked = sorted(
                state.connections.items(),
                key=lambda item: (state.impressions.get(item[0]).score if state.impressions.get(item[0]) else 0.0),
                reverse=True,
            )
            eligible = ranked[:params.share_with_top_n]

        for aid, conn in eligible:
            try:
                await send_to_peer(conn, "/__darkmatter__/message", envelope.payload)
                pushed_to.append(aid)
            except Exception:
                pass

    # Spawn a fresh warm agent to replace this one
    from darkmatter.spawn import spawn_warm_agent
    try:
        await spawn_warm_agent(state)
    except Exception as e:
        print(f"[DarkMatter] Warning: failed to spawn warm replacement: {e}", file=sys.stderr)

    result = {
        "success": True,
        "summary_id": summary_id,
        "pushed_to": pushed_to,
        "message": "Summary stored. Session complete. A fresh warm agent has been spawned.",
    }
    return json.dumps(result)


# Genome tools moved to HTTP API + skill (see /__darkmatter__/genome)


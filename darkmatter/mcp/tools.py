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
    SendMessageInput,
    UpdateBioInput,
    CreateShardInput,
    ViewShardsInput,
)
from darkmatter.state import get_state, save_state, sync_message_queue_from_disk, consume_message
from darkmatter.context import log_conversation
from darkmatter.config import (
    MAX_CONNECTIONS,
    WEBRTC_AVAILABLE,
    TRUST_MESSAGE_SENT,
)
from darkmatter.identity import (
    validate_url,
    validate_webhook_url,
)
from darkmatter.security import sign_message, prepare_outbound
from darkmatter.models import (
    AgentStatus,
    Connection,
    SentMessage,
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
            if WEBRTC_AVAILABLE:
                webrtc_t = get_network_manager().get_transport("webrtc")
                if webrtc_t and webrtc_t.available:
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


async def _send_new_message(state, params: SendMessageInput) -> str:
    """Send a new message into the mesh."""
    message_id = f"msg-{uuid.uuid4().hex[:12]}"
    metadata = params.metadata or {}

    # Set broadcast metadata
    if params.broadcast or params.message_type == "broadcast":
        metadata["type"] = "broadcast"

    # Determine peer_url for local relay bypass (set after target resolution)
    _peer_url = None

    if params.broadcast:
        # Broadcast: send to all peers meeting trust threshold
        targets = []
        for aid, conn in state.connections.items():
            imp = state.impressions.get(aid)
            peer_trust = imp.score if imp else 0.0
            if peer_trust >= params.trust_min:
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
        targets = [c for c in state.connections.values()]

    if not targets:
        return json.dumps({
            "success": False,
            "error": "No connections available to route this message."
        })

    # Build webhook URL — pass single target's URL for local relay bypass
    if len(targets) == 1:
        _peer_url = targets[0].agent_url
    webhook = get_network_manager().build_webhook_url(message_id, peer_url=_peer_url)

    msg_timestamp = datetime.now(timezone.utc).isoformat()
    base_payload = {
        "message_id": message_id,
        "content": params.content,
        "webhook": webhook,
        "hops_remaining": params.hops_remaining,
        "metadata": metadata,
        "timestamp": msg_timestamp,
    }
    envelope = prepare_outbound(base_payload, state.private_key_hex, state.agent_id, state.public_key_hex)

    sent_to = []
    failed = []
    for conn in targets:
        try:
            await send_to_peer(conn, "/__darkmatter__/message", envelope.payload)
            conn.messages_sent += 1
            conn.last_activity = datetime.now(timezone.utc).isoformat()
            sent_to.append(conn.agent_id)
            adjust_trust(state, conn.agent_id, TRUST_MESSAGE_SENT)
        except Exception as e:
            conn.messages_declined += 1
            failed.append({"agent_id": conn.agent_id, "display_name": conn.agent_display_name, "error": str(e)})

    # Log conversation
    if sent_to:
        msg_type = metadata.get("type", "direct") if metadata.get("type") == "broadcast" else "direct"
        log_conversation(
            state, message_id, params.content,
            from_id=state.agent_id, to_ids=sent_to,
            entry_type=msg_type, direction="outbound",
            metadata=metadata,
        )

    sent_msg = SentMessage(
        message_id=message_id,
        content=params.content,
        status="active",
        initial_hops=params.hops_remaining,
        routed_to=sent_to,
    )
    state.sent_messages[message_id] = sent_msg
    save_state()

    result = {
        "success": len(sent_to) > 0,
        "message_id": message_id,
        "routed_to": sent_to,
        "hops_remaining": params.hops_remaining,
    }
    if failed:
        result["failed"] = failed
        if not sent_to:
            result["error"] = f"Message could not be delivered to any of {len(failed)} target(s). Check 'failed' for details."
    if sent_to:
        result["next_step"] = f"IMPORTANT: Call darkmatter_wait_for_message(message_id='{message_id}') now to receive the reply. Do not poll — this blocks efficiently until the response arrives."
    return json.dumps(result)


async def _forward_message(state, params: SendMessageInput) -> str:
    """Forward a queued message to one or more connected agents. Removes from queue after delivery."""
    sync_message_queue_from_disk()  # Pick up messages queued by HTTP server
    # Find the message in the queue
    msg = None
    msg_index = None
    for i, m in enumerate(state.message_queue):
        if m.message_id == params.message_id:
            msg_index = i
            msg = m
            break

    if msg is None:
        return json.dumps({
            "success": False,
            "error": f"No queued message with ID '{params.message_id}'."
        })

    # Determine target(s)
    target_ids = []
    if params.target_agent_ids:
        target_ids = params.target_agent_ids
    elif params.target_agent_id:
        target_ids = [params.target_agent_id]
    else:
        return json.dumps({"success": False, "error": "target_agent_id or target_agent_ids required for forwarding."})

    # Validate all targets exist
    target_conns = []
    for tid in target_ids:
        conn = state.connections.get(tid)
        if not conn:
            return json.dumps({"success": False, "error": f"Not connected to agent '{tid}'."})
        target_conns.append((tid, conn))

    # GET webhook status -- verify message is still active + loop detection
    webhook_err = validate_webhook_url(msg.webhook)
    if not webhook_err:
        try:
            status_resp = await get_network_manager().webhook_request(
                msg.webhook, msg.from_agent_id,
                method="GET", timeout=10.0,
            )
            if status_resp.status_code == 200:
                webhook_data = status_resp.json()
                msg_status = webhook_data.get("status", "active")
                if msg_status in ("expired", "responded"):
                    state.message_queue.pop(msg_index)
                    consume_message(msg.message_id)
                    save_state()
                    return json.dumps({
                        "success": False,
                        "error": f"Message is already {msg_status} (checked via webhook). Removed from queue.",
                    })

                # Loop detection per target
                if not params.force:
                    for tid, _ in target_conns:
                        for update in webhook_data.get("updates", []):
                            if update.get("target_agent_id") == tid:
                                return json.dumps({
                                    "success": False,
                                    "error": f"Agent '{tid}' has already received this message. To forward anyway, retry with force=true.",
                                })
        except Exception as e:
            print(f"[DarkMatter] Warning: webhook status check failed for {msg.message_id}: {e}", file=sys.stderr)

    # TTL check
    if msg.hops_remaining <= 0:
        state.message_queue.pop(msg_index)
        consume_message(msg.message_id)
        if not webhook_err:
            try:
                await get_network_manager().webhook_request(
                    msg.webhook, msg.from_agent_id,
                    method="POST", timeout=30.0,
                    json={"type": "expired", "agent_id": state.agent_id, "note": "Message expired — no hops remaining."}
                )
            except Exception as e:
                print(f"[DarkMatter] Warning: failed to notify webhook of TTL expiry for {msg.message_id}: {e}", file=sys.stderr)
        save_state()
        return json.dumps({"success": False, "error": "Message expired — hops_remaining is 0."})

    new_hops_remaining = msg.hops_remaining - 1

    # Sign the forwarded message — mark metadata as forwarded so the recipient
    # knows to use the webhook for replies (the original sender isn't their peer)
    fwd_metadata = dict(msg.metadata or {})
    fwd_metadata["forwarded"] = True
    fwd_metadata["forwarded_by"] = state.agent_id
    fwd_timestamp = datetime.now(timezone.utc).isoformat()
    fwd_base_payload = {
        "message_id": msg.message_id,
        "content": msg.content,
        "webhook": msg.webhook,
        "hops_remaining": new_hops_remaining,
        "metadata": fwd_metadata,
        "timestamp": fwd_timestamp,
    }
    fwd_envelope = prepare_outbound(fwd_base_payload, state.private_key_hex, state.agent_id, state.public_key_hex)

    # Deliver to all targets
    per_target_results = []
    for tid, conn in target_conns:
        # POST forwarding update to webhook
        if not webhook_err:
            try:
                await get_network_manager().webhook_request(
                    msg.webhook, msg.from_agent_id,
                    method="POST", timeout=10.0,
                    json={"type": "forwarded", "agent_id": state.agent_id, "target_agent_id": tid, "note": params.note}
                )
            except Exception as e:
                print(f"[DarkMatter] Warning: failed to post forwarding update to webhook: {e}", file=sys.stderr)

        try:
            result = await send_to_peer(conn, "/__darkmatter__/message", fwd_envelope.payload)
            conn.messages_sent += 1
            conn.last_activity = datetime.now(timezone.utc).isoformat()
            per_target_results.append({"agent_id": tid, "success": True})
            adjust_trust(state, tid, TRUST_MESSAGE_SENT)
        except Exception as e:
            per_target_results.append({"agent_id": tid, "success": False, "error": str(e)})

    # Log conversation
    forwarded_to = [r["agent_id"] for r in per_target_results if r["success"]]
    if forwarded_to:
        log_conversation(
            state, msg.message_id, msg.content,
            from_id=state.agent_id, to_ids=forwarded_to,
            entry_type="forward", direction="outbound",
            metadata=msg.metadata,
        )

    # Remove from queue after delivery attempts
    state.message_queue.pop(msg_index)
    consume_message(msg.message_id)
    save_state()

    any_success = any(r["success"] for r in per_target_results)
    result = {
        "success": any_success,
        "message_id": msg.message_id,
        "hops_remaining_for_targets": new_hops_remaining,
        "results": per_target_results,
    }
    if params.note:
        result["note"] = params.note
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
    """Manage connections: request, accept, reject, or disconnect.

    Actions:
      - request: Send a connection request (requires target_url)
      - accept: Accept a pending connection request (requires request_id)
      - reject: Reject a pending connection request (requires request_id)
      - disconnect: Disconnect from an agent (requires agent_id)

    Args:
        params: Contains action and the relevant field(s) for that action.

    Returns:
        JSON with the result.
    """
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
    name="darkmatter_send_message",
    annotations={
        "title": "Send or Forward Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def send_message(params: SendMessageInput, ctx: Context) -> str:
    """Send a message or forward a message.

    - New message: provide `content` (and optionally `target_agent_id`).
    - Reply: provide `content` and `target_agent_id` set to the sender's from_agent_id.
    - Forward: provide `message_id` from your inbox (and `target_agent_id` or `target_agent_ids`).
      Removes from queue after delivery.

    Args:
        params: Contains content (new/reply) or message_id (forward), plus routing options.

    Returns:
        JSON with the message ID and routing info.
    """
    state = get_state()

    # Validate parameter combinations
    if params.message_id and params.content:
        return json.dumps({"success": False, "error": "Provide either content (new message) or message_id (forward), not both."})
    if not params.message_id and not params.content:
        return json.dumps({"success": False, "error": "Provide content (new message/reply) or message_id (forward)."})

    if params.message_id:
        return await _forward_message(state, params)
    else:
        return await _send_new_message(state, params)


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
    state = get_state()
    state.bio = params.bio
    save_state()

    # Broadcast bio change to all connected peers
    try:
        await get_network_manager().broadcast_peer_update()
    except Exception as e:
        print(f"[DarkMatter] Failed to broadcast bio update: {e}", file=sys.stderr)

    return json.dumps({"success": True, "bio": state.bio})


# =============================================================================
# Inbox Tool (merged list + get)
# =============================================================================

@mcp.tool(
    name="darkmatter_inbox",
    annotations={
        "title": "View Inbox",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
)
async def inbox(message_id: Optional[str] = None, ctx: Context = None) -> str:
    """List all incoming messages, or get full details of a specific message.

    Without message_id: returns summaries of all queued messages.
    With message_id: returns full content and removes the message from the queue
    (marking it as read). To reply, send a new message to the from_agent_id.
    For forwarded messages, a webhook URL is included for replying to the
    original sender.

    Args:
        message_id: Optional message ID to get full details for.

    Returns:
        JSON with message list or single message details.
    """
    sync_message_queue_from_disk()
    state = get_state()

    if message_id:
        for i, msg in enumerate(state.message_queue):
            if msg.message_id == message_id:
                # Remove from queue (mark as read)
                state.message_queue.pop(i)
                consume_message(message_id)
                save_state()

                is_forwarded = bool((msg.metadata or {}).get("forwarded"))
                result = {
                    "message_id": msg.message_id,
                    "content": msg.content,
                    "hops_remaining": msg.hops_remaining,
                    "can_forward": msg.hops_remaining > 0,
                    "from_agent_id": msg.from_agent_id,
                    "verified": msg.verified,
                    "metadata": msg.metadata,
                    "received_at": msg.received_at,
                }
                # Only surface webhook for forwarded messages — direct messages
                # should be replied to by sending a message to from_agent_id
                if is_forwarded and msg.webhook:
                    result["webhook"] = msg.webhook
                    result["reply_hint"] = "This message was forwarded to you. Use the webhook to reply to the original sender."
                else:
                    result["reply_hint"] = f"Send a message to {msg.from_agent_id} to reply."
                return json.dumps(result)
        return json.dumps({"success": False, "error": f"No queued message with ID '{message_id}'."})

    messages = []
    for msg in state.message_queue:
        is_forwarded = bool((msg.metadata or {}).get("forwarded"))
        entry = {
            "message_id": msg.message_id,
            "content": msg.content[:200] + ("..." if len(msg.content) > 200 else ""),
            "hops_remaining": msg.hops_remaining,
            "can_forward": msg.hops_remaining > 0,
            "from_agent_id": msg.from_agent_id,
            "verified": msg.verified,
            "metadata": msg.metadata,
            "received_at": msg.received_at,
            "forwarded": is_forwarded,
        }
        messages.append(entry)
    return json.dumps({"total": len(messages), "messages": messages})


# NOTE: Tools removed from MCP and moved to HTTP API + skill:
# get_identity, list_connections, list_pending_requests, set_status,
# list_inbox, get_message, list_messages, get_sent_message, expire_message,
# wait_for_response, network_info, discover_domain, discover_local,
# set_impression, get_impression, set_superagent, set_rate_limit,
# get_balance, send_sol, send_token, wallet_balances, wallet_send,
# genome_info, genome_install
# Access these via: curl localhost:PORT/__darkmatter__/<endpoint>
# See .claude/skills/darkmatter-ops/SKILL.md for documentation.

_REMOVED_TOOL_MARKER = True  # noqa: F841 — placeholder for removed tools


# REMOVED: set_status, get_identity, list_connections, list_pending_requests,
# list_inbox, get_message, list_messages, get_sent_message, expire_message,
# wait_for_response, network_info, discover_domain, discover_local,
# set_impression, get_impression, set_superagent, set_rate_limit,
# get_balance, send_sol, send_token, wallet_balances, wallet_send

# =============================================================================
# Shared Shards Tools
# =============================================================================

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
    """Create a shared knowledge shard and push it to qualifying peers.

    Shards are DarkMatter-native knowledge units — text-based, trust-gated, push-synced.
    Peers with impression score >= trust_threshold will receive the shard.

    Args:
        params: content, tags, trust_threshold, optional summary.

    Returns:
        JSON with shard details.
    """
    track_session(ctx)
    state = get_state()

    from darkmatter.config import SHARED_SHARD_MAX
    if len(state.shared_shards) >= SHARED_SHARD_MAX:
        return json.dumps({"success": False, "error": f"Shard limit reached ({SHARED_SHARD_MAX})"})

    now = datetime.now(timezone.utc).isoformat()
    from darkmatter.security import sign_shard

    shard_id = f"shard-{uuid.uuid4().hex[:12]}"
    tags_str = ",".join(sorted(params.tags))
    sig = sign_shard(state.private_key_hex, shard_id, state.agent_id, params.content, tags_str)

    shard = SharedShard(
        shard_id=shard_id,
        author_agent_id=state.agent_id,
        content=params.content,
        tags=params.tags,
        trust_threshold=params.trust_threshold,
        created_at=now,
        updated_at=now,
        summary=params.summary,
        signature_hex=sig,
    )
    state.shared_shards.append(shard)
    save_state()

    # Push to qualifying peers
    pushed_to = []
    shard_payload = {
        "shard_id": shard.shard_id,
        "author_agent_id": shard.author_agent_id,
        "content": shard.content,
        "tags": shard.tags,
        "trust_threshold": shard.trust_threshold,
        "created_at": shard.created_at,
        "updated_at": shard.updated_at,
        "summary": shard.summary,
        "signature_hex": shard.signature_hex,
    }
    for aid, conn in state.connections.items():
        imp = state.impressions.get(aid)
        peer_trust = imp.score if imp else 0.0
        if peer_trust >= params.trust_threshold:
            try:
                await send_to_peer(conn, "/__darkmatter__/shard_push", shard_payload)
                pushed_to.append(aid)
            except Exception as e:
                print(f"[DarkMatter] Warning: failed to push shard {shard.shard_id} to peer {aid}: {e}", file=sys.stderr)

    return json.dumps({
        "success": True,
        "shard_id": shard.shard_id,
        "tags": shard.tags,
        "trust_threshold": shard.trust_threshold,
        "pushed_to": pushed_to,
    })


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
    """Query shared knowledge shards by tags and/or author.

    Returns shards from both local and cached peer collections.

    Args:
        params: Optional tags (ANY match) and/or author filter.

    Returns:
        JSON with matching shards.
    """
    track_session(ctx)
    state = get_state()

    results = []
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

        entry = {
            "shard_id": shard.shard_id,
            "author": shard.author_agent_id,
            "tags": shard.tags,
            "trust_threshold": shard.trust_threshold,
            "created_at": shard.created_at,
            "updated_at": shard.updated_at,
        }
        # Show summary if available, else content
        if shard.summary:
            entry["summary"] = shard.summary
        else:
            entry["content"] = shard.content[:500]
            if len(shard.content) > 500:
                entry["truncated"] = True

        # Label if from peer
        if shard.author_agent_id != state.agent_id:
            conn = state.connections.get(shard.author_agent_id)
            if conn:
                entry["author_name"] = conn.agent_display_name or shard.author_agent_id[:12]
            entry["cached"] = True

        results.append(entry)

    return json.dumps({"success": True, "count": len(results), "shards": results})


# =============================================================================
# Live Status Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_status",
    annotations={
        "title": "Live Node Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def live_status(ctx: Context) -> str:
    """DarkMatter live node status dashboard. Current state is shown below — no need to call unless you want full details.

    LIVE STATUS: Waiting for first status update... This will show live node state and action items you should respond to.
    """
    track_session(ctx)
    sync_message_queue_from_disk()  # Pick up messages queued by HTTP server
    from darkmatter.mcp.visibility import build_status_line
    return build_status_line()


# =============================================================================
# Wait for Message Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_wait_for_message",
    annotations={
        "title": "Wait for Message",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def wait_for_message(
    message_id: Optional[str] = None,
    from_agents: Optional[list[str]] = None,
    timeout_seconds: float = 900,
    ctx: Context = None,
) -> str:
    """Wait for a message to arrive. Two modes:

    1. **Wait for reply** (message_id provided): Blocks until a response webhook
       fires for a sent message. Use after darkmatter_send_message.

    2. **Wait for inbox** (no message_id): Blocks until a new inbound message
       arrives in your inbox. Use when idle and waiting for peers to contact you.
       Optionally filter with from_agents to only wake for specific peers.

    The node continues processing other requests while this tool waits.
    Default timeout is 15 minutes. When it times out, you should decide whether
    to call it again to keep listening or move on.

    Args:
        message_id: If provided, wait for a reply to this sent message.
        from_agents: Optional list of agent IDs to filter inbox messages by.
                     Only used in inbox mode (no message_id). Omit to wait for any message.
        timeout_seconds: How long to wait before timing out (default 900s / 15 minutes).

    Returns:
        JSON with the message(s) that arrived, or a timeout asking whether to keep waiting.
    """
    state = get_state()

    # ── Mode 1: Wait for reply to a sent message ──
    if message_id:
        sm = state.sent_messages.get(message_id)
        if not sm:
            return json.dumps({"success": False, "error": f"No sent message with ID '{message_id}'."})

        # Check if responses already exist
        if sm.responses:
            return json.dumps({
                "success": True,
                "mode": "reply",
                "message_id": message_id,
                "responses": sm.responses,
                "waited": False,
            })

        if sm.status == "expired":
            return json.dumps({
                "success": False,
                "message_id": message_id,
                "error": "Message is expired — no response will arrive.",
            })

        # Register event and wait
        event = asyncio.Event()
        if message_id not in state._response_events:
            state._response_events[message_id] = []
        state._response_events[message_id].append(event)

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            evts = state._response_events.get(message_id, [])
            if event in evts:
                evts.remove(event)
            if not evts:
                state._response_events.pop(message_id, None)
            mins = int(timeout_seconds / 60)
            return json.dumps({
                "success": False,
                "mode": "reply",
                "message_id": message_id,
                "timed_out": True,
                "error": f"No reply received after {mins} minutes.",
                "action": "Ask the user if they want to keep waiting, then call darkmatter_wait_for_message again with the same message_id.",
            })

        sm = state.sent_messages.get(message_id)
        return json.dumps({
            "success": True,
            "mode": "reply",
            "message_id": message_id,
            "responses": sm.responses if sm else [],
            "waited": True,
        })

    # ── Mode 2: Wait for new inbox message ──

    # Check if matching messages already exist in inbox
    sync_message_queue_from_disk()
    existing = _filter_inbox(state, from_agents)
    if existing:
        return json.dumps({
            "success": True,
            "mode": "inbox",
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
            matched = _filter_inbox(state, from_agents)
            if matched:
                if event in state._inbox_events:
                    state._inbox_events.remove(event)
                return json.dumps({
                    "success": True,
                    "mode": "inbox",
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
            "mode": "inbox",
            "timed_out": True,
            "error": f"No message{filter_desc} received after {mins} minutes.",
            "action": "Ask the user if they want to keep waiting, then call darkmatter_wait_for_message again to resume listening.",
        })


def _filter_inbox(state, from_agents: Optional[list[str]] = None) -> list[dict]:
    """Return inbox messages matching the agent filter, as JSON-safe dicts."""
    messages = []
    for msg in state.message_queue:
        if from_agents and msg.from_agent_id not in from_agents:
            continue
        is_forwarded = bool((msg.metadata or {}).get("forwarded"))
        entry = {
            "message_id": msg.message_id,
            "content": msg.content[:500] + ("..." if len(msg.content) > 500 else ""),
            "from_agent_id": msg.from_agent_id,
            "hops_remaining": msg.hops_remaining,
            "verified": msg.verified,
            "received_at": msg.received_at,
            "forwarded": is_forwarded,
        }
        if is_forwarded and msg.webhook:
            entry["webhook"] = msg.webhook
        messages.append(entry)
    return messages

# Genome tools moved to HTTP API + skill (see /__darkmatter__/genome)


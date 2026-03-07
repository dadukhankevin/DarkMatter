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
    SendMessageInput,
    UpdateBioInput,
    CreateShardInput,
    ViewShardsInput,
)
from darkmatter.state import get_state, save_state, sync_message_queue_from_disk, consume_message
from darkmatter.context import log_conversation
from darkmatter.config import (
    MAX_CONNECTIONS,
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


def _is_streamable(conn) -> bool:
    """Check if a connection supports chunk streaming (WebRTC, localhost, or LAN)."""
    from darkmatter.network.manager import is_local_url
    # WebRTC data channel open = true streaming
    ch = getattr(conn, "webrtc_channel", None)
    if ch is not None and getattr(ch, "readyState", None) == "open":
        return True
    # Local/LAN HTTP = fine for chunks (same machine or same network, low cost)
    url = getattr(conn, "agent_url", "") or ""
    if is_local_url(url):
        return True
    # Remote HTTP = skip chunks (wasteful, one POST per 512-byte chunk)
    return False


async def _send_chunk(state, message_id: str, content: str, targets: list) -> None:
    """Send a chunk signal to streamable targets only (WebRTC or localhost)."""
    payload = {
        "message_id": message_id,
        "type": "chunk",
        "from_agent_id": state.agent_id,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    for conn in targets:
        if not _is_streamable(conn):
            continue
        try:
            await send_to_peer(conn, "/__darkmatter__/message_stream", payload)
        except Exception:
            pass


async def _begin_message(state, params: BeginMessageInput) -> str:
    """Signal that a message is being composed. Sends typing indicator to target(s).

    Supports nesting: if called while another message is in flight,
    stdout chunks stream to ALL active targets (stack model).
    """
    message_id = f"msg-{uuid.uuid4().hex[:12]}"
    metadata = params.metadata or {}

    if params.broadcast:
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
            "error": "No connections available to send to."
        })

    # Build typing signal payload
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

    # Send typing signal to all targets (best-effort)
    signaled = []
    for conn in targets:
        try:
            await send_to_peer(conn, "/__darkmatter__/message_stream", signal_payload)
            signaled.append(conn.agent_id)
        except Exception:
            pass

    # Store pending stream state
    _pending_streams[message_id] = {
        "from_agent_id": state.agent_id,
        "targets": [c.agent_id for c in targets],
        "signaled": signaled,
        "in_reply_to": params.in_reply_to,
        "broadcast": params.broadcast,
        "hops_remaining": params.hops_remaining,
        "metadata": metadata,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    # Hook into spawned agent's stdout to stream chunks (if this is a reply)
    if params.in_reply_to:
        from darkmatter.spawn import get_spawned_agents
        for agent in get_spawned_agents():
            if agent.message_id == params.in_reply_to:
                async def _on_chunk(chunk: str, _mid=message_id, _st=state, _tgts=targets):
                    await _send_chunk(_st, _mid, chunk, _tgts)
                agent.stdout_callbacks[message_id] = _on_chunk
                _pending_streams[message_id]["_spawned_agent"] = agent
                break

    return json.dumps({
        "success": True,
        "message_id": message_id,
        "signaled": signaled,
        "targets": [c.agent_id for c in targets],
        "next_step": f"Compose your response, then call darkmatter_end_message(message_id='{message_id}', content='...').",
    })


async def _end_message(state, params: EndMessageInput) -> str:
    """Complete a message started with begin_message. Sends the final content."""
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

    target_ids = stream_info["targets"]
    metadata = stream_info.get("metadata") or {}
    in_reply_to = stream_info.get("in_reply_to")
    hops_remaining = stream_info.get("hops_remaining", 10)

    if stream_info.get("broadcast"):
        metadata["type"] = "broadcast"

    targets = []
    for tid in target_ids:
        conn = state.connections.get(tid)
        if conn:
            targets.append(conn)

    if not targets:
        return json.dumps({
            "success": False,
            "error": "No connections available to deliver message."
        })

    # Determine peer_url for local relay bypass
    _peer_url = targets[0].agent_url if len(targets) == 1 else None
    webhook = get_network_manager().build_webhook_url(params.message_id, peer_url=_peer_url)

    msg_timestamp = datetime.now(timezone.utc).isoformat()
    base_payload = {
        "message_id": params.message_id,
        "content": params.content,
        "webhook": webhook,
        "hops_remaining": hops_remaining,
        "metadata": metadata,
        "timestamp": msg_timestamp,
    }
    if in_reply_to:
        base_payload["in_reply_to"] = in_reply_to

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

    # Send end signal to close typing indicator (best-effort)
    end_signal = {
        "message_id": params.message_id,
        "type": "end",
        "from_agent_id": state.agent_id,
        "timestamp": msg_timestamp,
    }
    for conn in targets:
        try:
            await send_to_peer(conn, "/__darkmatter__/message_stream", end_signal)
        except Exception:
            pass

    # Log conversation
    if sent_to:
        msg_type = metadata.get("type", "direct") if metadata.get("type") == "broadcast" else "direct"
        log_conversation(
            state, params.message_id, params.content,
            from_id=state.agent_id, to_ids=sent_to,
            entry_type=msg_type, direction="outbound",
            metadata=metadata,
        )

    sent_msg = SentMessage(
        message_id=params.message_id,
        content=params.content,
        status="active" if sent_to else "failed",
        initial_hops=hops_remaining,
        routed_to=sent_to,
    )
    state.sent_messages[params.message_id] = sent_msg
    save_state()

    result = {
        "success": len(sent_to) > 0,
        "message_id": params.message_id,
        "routed_to": sent_to,
        "hops_remaining": hops_remaining,
    }
    if failed:
        result["failed"] = failed
        if not sent_to:
            result["error"] = f"Message could not be delivered to any of {len(failed)} target(s)."
    if sent_to:
        result["next_step"] = f"Call darkmatter_wait_for_message(message_id='{params.message_id}') now to get the reply."
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
    """Start composing a message. Everything you write after this streams live to the receiver. Call EARLY — the receiver sees your output in real time. For humans, write naturally and at length. For agents, be concise. Call end_message when done."""
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
    """Finish a message. The receiver already saw the streamed content; provide a summary for history. The summary should capture the key points of everything you wrote."""
    track_session(ctx)
    state = get_state()
    return await _end_message(state, params)


@mcp.tool(
    name="darkmatter_send_message",
    annotations={
        "title": "Forward Queued Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def send_message(params: SendMessageInput, ctx: Context) -> str:
    """Forward a queued message to another agent. Provide message_id + target_agent_id. For new messages, use begin_message + end_message."""
    state = get_state()

    if not params.message_id:
        return json.dumps({"success": False, "error": "message_id is required. For new messages, use begin_message + end_message."})

    return await _forward_message(state, params)


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
    """Update your bio and/or display name. Shared with peers for routing decisions."""
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
    """View inbox. No args: list all. With message_id: read full message (removes from queue). Reply by sending to from_agent_id. If not the right recipient, forward it."""
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
                    result["reply_hint"] = "Forwarded message. Reply to original sender via webhook. If better suited for another peer, forward it."
                else:
                    result["reply_hint"] = f"Reply to {msg.from_agent_id}. If this is better suited for another peer, forward it to them instead."
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
        "trust_threshold": shard.trust_threshold,
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
    for aid, conn in state.connections.items():
        imp = state.impressions.get(aid)
        peer_trust = imp.score if imp else 0.0
        if peer_trust >= shard.trust_threshold:
            try:
                await send_to_peer(conn, "/__darkmatter__/shard_push", shard_payload)
                pushed_to.append(aid)
            except Exception as e:
                print(f"[DarkMatter] Warning: failed to push shard {shard.shard_id} to peer {aid}: {e}", file=sys.stderr)
    return pushed_to


async def create_shard(params: CreateShardInput, ctx: Context) -> str:
    """Create a knowledge shard (text or code) and push to qualifying peers.

    Text shard: provide content directly.
    Code shard: provide file, from_text, to_text — content is resolved live from the file.
    When the code changes, updates are automatically pushed to peers on next view.

    Prefer raw code over summaries. Only add a summary for very long code regions
    where the full content would waste context. For most shards, skip summary.
    """
    track_session(ctx)
    state = get_state()

    from darkmatter.config import SHARED_SHARD_MAX
    if len(state.shared_shards) >= SHARED_SHARD_MAX:
        return json.dumps({"success": False, "error": f"Shard limit reached ({SHARED_SHARD_MAX})"})

    is_code_shard = params.file is not None
    file_path = None
    region = None
    original_content = None
    original_hash = None

    if is_code_shard:
        if not params.from_text or not params.to_text:
            return json.dumps({"success": False, "error": "Code shards require file, from_text, and to_text."})

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
    else:
        if not params.content:
            return json.dumps({"success": False, "error": "Text shards require content."})
        content = params.content

    now = datetime.now(timezone.utc).isoformat()
    from darkmatter.security import sign_shard

    # Upsert: if a code shard exists for same file+from_text, replace it
    shard_id = f"shard-{uuid.uuid4().hex[:12]}"
    was_update = False
    if is_code_shard:
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
        trust_threshold=params.trust_threshold,
        created_at=now,
        updated_at=now,
        summary=params.summary,
        signature_hex=sig,
        file=params.file,
        from_text=params.from_text,
        to_text=params.to_text,
        function_anchor=region.function_anchor if region else None,
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
        "trust_threshold": shard.trust_threshold,
        "pushed_to": pushed_to,
    }
    if is_code_shard:
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
        is_code = shard.file is not None

        entry = {
            "shard_id": shard.shard_id,
            "author": shard.author_agent_id,
            "tags": shard.tags,
            "trust_threshold": shard.trust_threshold,
            "created_at": shard.created_at,
            "updated_at": shard.updated_at,
        }

        if is_code:
            entry["file"] = shard.file
            entry["type"] = "code"

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

                    # Show content (raw code preferred for code shards)
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
                    # Code shard without original tracking — show content
                    entry["content"] = current_content or shard.content
            else:
                # Remote code shard — show last-known snapshot
                entry["cached"] = True
                if shard.summary and not params.raw:
                    entry["summary"] = shard.summary
                else:
                    entry["content"] = shard.content[:500]
                    if len(shard.content) > 500:
                        entry["truncated"] = True
        else:
            entry["type"] = "text"
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
# Live Status Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_status",
    annotations={
        "title": "Status + Context",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
)
async def live_status(ctx: Context) -> str:
    """Get node status and NEW conversation context since your last call. Call regularly to stay current. Act on every ACTION item."""
    track_session(ctx)
    sync_message_queue_from_disk()
    from darkmatter.mcp.visibility import build_status_line
    from darkmatter.context import get_new_context

    state = get_state()
    status = build_status_line()

    # Get session ID for context tracking
    session_id = "default"
    try:
        session_id = str(id(ctx.session))
    except Exception:
        pass

    new_context = get_new_context(state, session_id)

    parts = [status]
    if new_context:
        parts.append(new_context)
    else:
        parts.append("No new conversation activity since last check.")

    return "\n\n".join(parts)


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
    """Block until a message arrives. With message_id: wait for reply. Without: wait for any inbox message. Optional from_agents filter."""
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
                "action": "Send a follow-up or proactively message another peer while waiting. Then call darkmatter_wait_for_message again.",
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
            "action": "Proactively reach out to peers, share updates, or broadcast. Then resume listening with darkmatter_wait_for_message.",
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


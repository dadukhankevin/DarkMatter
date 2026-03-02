"""
All MCP tool definitions for the DarkMatter mesh protocol.

Depends on: mcp/__init__, mcp/schemas, config, models, identity, state,
            wallet, network, spawn
"""

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from mcp.server.fastmcp import Context

from darkmatter.mcp import mcp, track_session
from darkmatter.mcp.schemas import (
    ConnectionAction,
    ConnectionInput,
    SendMessageInput,
    UpdateBioInput,
    SetStatusInput,
    GetMessageInput,
    GetSentMessageInput,
    ExpireMessageInput,
    WaitForResponseInput,
    DiscoverDomainInput,
    SetImpressionInput,
    GetImpressionInput,
    SetSuperagentInput,
    SendSolInput,
    SendTokenInput,
    GetBalanceInput,
    WalletBalancesInput,
    WalletSendInput,
    SetRateLimitInput,
    CreateShardInput,
    ViewShardsInput,
)
from darkmatter.state import get_state, save_state
from darkmatter.context import log_conversation
from darkmatter.config import (
    MAX_CONNECTIONS,
    DEFAULT_RATE_LIMIT_PER_CONNECTION,
    DEFAULT_RATE_LIMIT_GLOBAL,
    SUPERAGENT_DEFAULT_URL,
    WEBRTC_AVAILABLE,
    DISCOVERY_MAX_AGE,
    ANTIMATTER_RATE,
    TRUST_MESSAGE_SENT,
    SOLANA_AVAILABLE,
    SOLANA_RPC_URL,
    LAMPORTS_PER_SOL,
)
from darkmatter.identity import (
    validate_url,
    validate_webhook_url,
    is_private_ip,
    sign_message,
)
from darkmatter.models import (
    AgentStatus,
    Connection,
    SentMessage,
    SharedShard,
    Impression,
)
from darkmatter.network import send_to_peer, strip_base_url, get_network_manager
from darkmatter.network.mesh import (
    build_outbound_request_payload,
    build_connection_from_accepted,
    notify_connection_accepted,
    process_accept_pending,
)
from darkmatter.wallet.solana import (
    get_solana_balance,
    send_solana_sol,
    send_solana_token,
    _resolve_spl_token,
)
from darkmatter.wallet.antimatter import (
    adjust_trust,
    auto_disconnect_peer,
    get_superagent_wallet,
    _superagent_wallet_cache,
)

# Conditional Solana imports for wallet_balances tool
if SOLANA_AVAILABLE:
    from darkmatter.config import SolanaPubkey, SolanaClient


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

            state.pending_outbound[target_base] = result.get("agent_id", "")
            return json.dumps({
                "success": True,
                "status": "pending",
                "message": "Connection request sent. Waiting for acceptance.",
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
    except Exception:
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

    webhook = get_network_manager().build_webhook_url(message_id)

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

    msg_timestamp = datetime.now(timezone.utc).isoformat()
    signature_hex = None
    if state.private_key_hex:
        signature_hex = sign_message(
            state.private_key_hex, state.agent_id, message_id, msg_timestamp, params.content
        )

    sent_to = []
    failed = []
    for conn in targets:
        try:
            payload = {
                "message_id": message_id,
                "content": params.content,
                "webhook": webhook,
                "hops_remaining": params.hops_remaining,
                "from_agent_id": state.agent_id,
                "metadata": metadata,
                "timestamp": msg_timestamp,
                "from_public_key_hex": state.public_key_hex,
                "signature_hex": signature_hex,
            }
            await send_to_peer(conn, "/__darkmatter__/message", payload)
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
        "webhook": webhook,
    }
    if failed:
        result["failed"] = failed
        if not sent_to:
            result["error"] = f"Message could not be delivered to any of {len(failed)} target(s). Check 'failed' for details."
    if sent_to:
        result["hint"] = f"Use darkmatter_wait_for_response(message_id='{message_id}') to block until a reply arrives."
    return json.dumps(result)


async def _forward_message(state, params: SendMessageInput) -> str:
    """Forward a queued message to one or more connected agents. Removes from queue after delivery."""
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
        if not webhook_err:
            try:
                await get_network_manager().webhook_request(
                    msg.webhook, msg.from_agent_id,
                    method="POST", timeout=30.0,
                    json={"type": "expired", "agent_id": state.agent_id, "note": "Message expired — no hops remaining."}
                )
            except Exception:
                pass
        save_state()
        return json.dumps({"success": False, "error": "Message expired — hops_remaining is 0."})

    new_hops_remaining = msg.hops_remaining - 1

    # Sign the forwarded message
    fwd_timestamp = datetime.now(timezone.utc).isoformat()
    fwd_signature_hex = None
    if state.private_key_hex:
        fwd_signature_hex = sign_message(
            state.private_key_hex, state.agent_id, msg.message_id, fwd_timestamp, msg.content
        )

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
            fwd_payload = {
                "message_id": msg.message_id,
                "content": msg.content,
                "webhook": msg.webhook,
                "hops_remaining": new_hops_remaining,
                "from_agent_id": state.agent_id,
                "metadata": msg.metadata,
                "timestamp": fwd_timestamp,
                "from_public_key_hex": state.public_key_hex,
                "signature_hex": fwd_signature_hex,
            }
            result = await send_to_peer(conn, "/__darkmatter__/message", fwd_payload)
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


async def _reply_to_message(state, params: SendMessageInput) -> str:
    """Reply to a queued message by calling its webhook with the response content."""
    # Find and remove the message from the queue
    msg = None
    for i, m in enumerate(state.message_queue):
        if m.message_id == params.reply_to:
            msg = state.message_queue.pop(i)
            break

    if msg is None:
        return json.dumps({
            "success": False,
            "error": f"No queued message with ID '{params.reply_to}'."
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

    # Check if message is still active before responding
    try:
        status_resp = await get_network_manager().webhook_request(
            msg.webhook, msg.from_agent_id,
            method="GET", timeout=10.0,
        )
        if status_resp.status_code == 200:
            webhook_data = status_resp.json()
            msg_status = webhook_data.get("status", "active")
            if msg_status == "expired":
                save_state()
                return json.dumps({
                    "success": False,
                    "error": "Message has been expired by the originator.",
                    "message_id": msg.message_id,
                })
    except Exception as e:
        print(f"[DarkMatter] Warning: webhook status check failed for {msg.message_id}: {e}", file=sys.stderr)

    # Notify originator that we're actively responding
    try:
        await get_network_manager().webhook_request(
            msg.webhook, msg.from_agent_id,
            method="POST", timeout=10.0,
            json={"type": "responding", "agent_id": state.agent_id}
        )
    except Exception:
        pass  # Best-effort notification

    # Sign the webhook response
    resp_timestamp = datetime.now(timezone.utc).isoformat()
    resp_signature_hex = None
    if state.private_key_hex:
        resp_signature_hex = sign_message(
            state.private_key_hex, state.agent_id, msg.message_id, resp_timestamp, params.content
        )

    # Call the webhook with our response
    webhook_success = False
    webhook_error = None
    response_time_ms = 0.0
    try:
        start = time.monotonic()
        resp = await get_network_manager().webhook_request(
            msg.webhook, msg.from_agent_id,
            method="POST", timeout=30.0,
            json={
                "type": "response",
                "agent_id": state.agent_id,
                "response": params.content,
                "metadata": msg.metadata,
                "timestamp": resp_timestamp,
                "from_public_key_hex": state.public_key_hex,
                "signature_hex": resp_signature_hex,
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

    # Trust micro-gain on successful reply delivery
    if webhook_success and msg.from_agent_id:
        adjust_trust(state, msg.from_agent_id, TRUST_MESSAGE_SENT)

    # Log conversation
    if webhook_success:
        log_conversation(
            state, msg.message_id, params.content,
            from_id=state.agent_id, to_ids=[msg.from_agent_id] if msg.from_agent_id else [],
            entry_type="reply", direction="outbound",
            metadata=msg.metadata,
        )

    save_state()
    return json.dumps({
        "success": webhook_success,
        "message_id": msg.message_id,
        "webhook_called": msg.webhook,
        "response_time_ms": round(response_time_ms, 2),
        "error": webhook_error,
    })


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
        "title": "Send, Reply, or Forward Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def send_message(params: SendMessageInput, ctx: Context) -> str:
    """Send a message, reply to a message, or forward a message.

    - New message: provide `content` (and optionally `target_agent_id`).
    - Reply: provide `content` and `reply_to` (message_id from your inbox).
      Removes the message from your queue and sends your response via its webhook.
    - Forward: provide `message_id` from your inbox (and `target_agent_id` or `target_agent_ids`).
      Removes from queue after delivery.

    Args:
        params: Contains content (new/reply) or message_id (forward), plus routing options.

    Returns:
        JSON with the message ID, routing info, and webhook URL.
    """
    state = get_state()

    # Validate parameter combinations
    if params.reply_to and params.message_id:
        return json.dumps({"success": False, "error": "Cannot use both reply_to and message_id. Use reply_to with content to reply, or message_id alone to forward."})
    if params.reply_to and not params.content:
        return json.dumps({"success": False, "error": "reply_to requires content (your response text)."})
    if params.message_id and params.content:
        return json.dumps({"success": False, "error": "Provide either content (new message) or message_id (forward), not both."})
    if not params.message_id and not params.content:
        return json.dumps({"success": False, "error": "Provide content (new message/reply) or message_id (forward)."})

    if params.message_id:
        return await _forward_message(state, params)
    elif params.reply_to:
        return await _reply_to_message(state, params)
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
    if state.public_url:
        try:
            await get_network_manager().broadcast_peer_update()
        except Exception as e:
            print(f"[DarkMatter] Failed to broadcast bio update: {e}", file=sys.stderr)

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
    Inactive with no duration defaults to 60 minutes. Use duration_minutes to customize.
    Setting active clears any pending auto-reactivation timer.

    Args:
        params: Contains the status ('active' or 'inactive') and optional duration_minutes.

    Returns:
        JSON confirming the status change.
    """
    state = get_state()
    state.status = params.status

    if params.status == AgentStatus.INACTIVE:
        duration = params.duration_minutes or 60
        reactivate_at = datetime.now(timezone.utc) + timedelta(minutes=duration)
        state.inactive_until = reactivate_at.isoformat()
        save_state()
        return json.dumps({"success": True, "status": "inactive", "inactive_until": state.inactive_until, "duration_minutes": duration})
    else:
        state.inactive_until = None
        save_state()
        return json.dumps({"success": True, "status": "active"})


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
    track_session(ctx)
    state = get_state()
    passport_path = os.path.join(os.getcwd(), ".darkmatter", "passport.key")
    result = {
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "private_key_hex": state.private_key_hex,
        "passport_path": passport_path,
        "bio": state.bio,
        "status": state.status.value,
        "port": state.port,
        "num_connections": len(state.connections),
        "num_pending_requests": len(state.pending_requests),
        "messages_handled": state.messages_handled,
        "message_queue_size": len(state.message_queue),
        "sent_messages_count": len(state.sent_messages),
        "created_at": state.created_at,
    }
    if state.wallets:
        result["wallets"] = state.wallets
    result["superagent_url"] = state.superagent_url or SUPERAGENT_DEFAULT_URL
    return json.dumps(result)


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

    Shows each connection's agent ID, bio, message counts,
    response times, and last activity.

    Returns:
        JSON array of connection details.
    """
    state = get_state()
    connections = []

    def _truncate(text: str, max_words: int = 20) -> str:
        words = text.split()
        if len(words) <= max_words:
            return text
        return " ".join(words[:max_words]) + "..."

    for conn in state.connections.values():
        entry = {
            "agent_id": conn.agent_id,
            "display_name": conn.agent_display_name,
            "agent_url": conn.agent_url,
            "bio_summary": _truncate(conn.agent_bio, 250) if conn.agent_bio else None,
            "crypto": conn.agent_public_key_hex is not None,
            "transport": conn.transport,
            "connected_at": conn.connected_at,
            "messages_sent": conn.messages_sent,
            "messages_received": conn.messages_received,
            "messages_declined": conn.messages_declined,
            "avg_response_time_ms": round(conn.avg_response_time_ms, 2),
            "last_activity": conn.last_activity,
            "rate_limit": conn.rate_limit if conn.rate_limit != 0 else DEFAULT_RATE_LIMIT_PER_CONNECTION,
        }
        if conn.wallets:
            entry["wallets"] = conn.wallets
        impression = state.impressions.get(conn.agent_id)
        if impression:
            entry["score"] = impression.score
            if impression.note:
                entry["note"] = _truncate(impression.note, 500)
        connections.append(entry)

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
    state = get_state()
    requests = []
    for req in state.pending_requests.values():
        entry = {
            "request_id": req.request_id,
            "from_agent_id": req.from_agent_id,
            "from_agent_display_name": req.from_agent_display_name,
            "from_agent_url": req.from_agent_url,
            "from_agent_bio": req.from_agent_bio,
            "crypto": req.from_agent_public_key_hex is not None,
            "requested_at": req.requested_at,
        }
        if req.peer_trust is not None:
            entry["peer_trust"] = req.peer_trust
        requests.append(entry)

    return json.dumps({
        "total": len(requests),
        "requests": requests,
        "reminder": "Remember to set impressions for your connections — your peers rely on your scores to make trust decisions.",
    })


# =============================================================================
# Inbox Tools (incoming messages)
# =============================================================================

@mcp.tool(
    name="darkmatter_list_inbox",
    annotations={
        "title": "List Inbox Messages",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_inbox(ctx: Context) -> str:
    """List all incoming messages in the queue waiting to be processed.

    Shows message summaries. Use darkmatter_get_message for full content.

    Returns:
        JSON array of queued messages.
    """
    state = get_state()
    messages = []
    for msg in state.message_queue:
        messages.append({
            "message_id": msg.message_id,
            "content": msg.content[:200] + ("..." if len(msg.content) > 200 else ""),
            "webhook": msg.webhook,
            "hops_remaining": msg.hops_remaining,
            "can_forward": msg.hops_remaining > 0,
            "from_agent_id": msg.from_agent_id,
            "verified": msg.verified,
            "metadata": msg.metadata,
            "received_at": msg.received_at,
        })

    return json.dumps({"total": len(messages), "messages": messages})


# =============================================================================
# Message Detail Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_get_message",
    annotations={
        "title": "Get Message Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_message(params: GetMessageInput, ctx: Context) -> str:
    """Get full details of a specific queued message.

    Shows full content and metadata. For routing context, GET the webhook URL.

    Args:
        params: Contains message_id to inspect.

    Returns:
        JSON with message details.
    """
    state = get_state()

    for msg in state.message_queue:
        if msg.message_id == params.message_id:
            return json.dumps({
                "message_id": msg.message_id,
                "content": msg.content,
                "webhook": msg.webhook,
                "hops_remaining": msg.hops_remaining,
                "can_forward": msg.hops_remaining > 0,
                "from_agent_id": msg.from_agent_id,
                "verified": msg.verified,
                "metadata": msg.metadata,
                "received_at": msg.received_at,
            })

    return json.dumps({
        "success": False,
        "error": f"No queued message with ID '{params.message_id}'."
    })


# =============================================================================
# Sent Message Tracking Tools
# =============================================================================

@mcp.tool(
    name="darkmatter_list_messages",
    annotations={
        "title": "List Sent Messages",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_messages(ctx: Context) -> str:
    """List messages this agent has sent into the mesh.

    Shows message summaries with status and routing info.
    Use darkmatter_get_sent_message for full details.

    Returns:
        JSON array of sent messages.
    """
    state = get_state()
    messages = []
    for sm in state.sent_messages.values():
        forwarding_count = sum(1 for u in sm.updates if u.get("type") == "forwarded")

        messages.append({
            "message_id": sm.message_id,
            "content": sm.content[:200] + ("..." if len(sm.content) > 200 else ""),
            "status": sm.status,
            "initial_hops": sm.initial_hops,
            "forwarding_count": forwarding_count,
            "updates_count": len(sm.updates),
            "created_at": sm.created_at,
        })

    return json.dumps({"total": len(messages), "messages": messages})


@mcp.tool(
    name="darkmatter_get_sent_message",
    annotations={
        "title": "Get Sent Message Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_sent_message(params: GetSentMessageInput, ctx: Context) -> str:
    """Get full details of a sent message including all webhook updates received.

    Shows the complete routing history: which agents forwarded it, any notes
    they attached, and the final response if one has been received.

    Args:
        params: Contains message_id to inspect.

    Returns:
        JSON with full sent message details.
    """
    state = get_state()

    sm = state.sent_messages.get(params.message_id)
    if not sm:
        return json.dumps({
            "success": False,
            "error": f"No sent message with ID '{params.message_id}'."
        })

    forwarding_count = sum(1 for u in sm.updates if u.get("type") == "forwarded")

    return json.dumps({
        "message_id": sm.message_id,
        "content": sm.content,
        "status": sm.status,
        "initial_hops": sm.initial_hops,
        "forwarding_count": forwarding_count,
        "routed_to": sm.routed_to,
        "created_at": sm.created_at,
        "updates": sm.updates,
        "responses": sm.responses,
    })


@mcp.tool(
    name="darkmatter_expire_message",
    annotations={
        "title": "Expire Sent Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def expire_message(params: ExpireMessageInput, ctx: Context) -> str:
    """Expire a sent message so agents in the mesh stop forwarding it.

    Agents that check the webhook status before forwarding will see the
    message is expired and remove it from their queues.

    Args:
        params: Contains message_id to expire.

    Returns:
        JSON confirming the expiry.
    """
    state = get_state()

    sm = state.sent_messages.get(params.message_id)
    if not sm:
        return json.dumps({
            "success": False,
            "error": f"No sent message with ID '{params.message_id}'."
        })

    if sm.status == "expired":
        return json.dumps({
            "success": True,
            "message": "Message was already expired.",
            "message_id": sm.message_id,
        })

    sm.status = "expired"
    save_state()

    return json.dumps({
        "success": True,
        "message_id": sm.message_id,
        "status": "expired",
    })


@mcp.tool(
    name="darkmatter_wait_for_response",
    annotations={
        "title": "Wait For Response",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def wait_for_response(params: WaitForResponseInput, ctx: Context) -> str:
    """Wait for a response to arrive on a sent message.

    Blocks until a response webhook fires for the given message_id, or the
    timeout expires. If the message already has responses, returns immediately.

    This does NOT block the node -- incoming messages, webhooks, and subagent
    spawns all continue normally while this tool awaits.

    Args:
        params: Contains message_id to wait on and timeout_seconds.

    Returns:
        JSON with the response(s) if one arrived, or a timeout indicator.
    """
    state = get_state()

    sm = state.sent_messages.get(params.message_id)
    if not sm:
        return json.dumps({
            "success": False,
            "error": f"No sent message with ID '{params.message_id}'."
        })

    # If there are already responses, return immediately
    if sm.responses:
        return json.dumps({
            "success": True,
            "message_id": sm.message_id,
            "status": sm.status,
            "responses": sm.responses,
        })

    # If the message is expired, no point waiting
    if sm.status == "expired":
        return json.dumps({
            "success": False,
            "message_id": sm.message_id,
            "reason": "message_expired",
            "error": "This message has been expired — no response will arrive.",
        })

    # Register an event and wait
    event = asyncio.Event()
    state._response_events.setdefault(params.message_id, []).append(event)

    try:
        await asyncio.wait_for(event.wait(), timeout=params.timeout_seconds)
    except asyncio.TimeoutError:
        # Clean up our event from the list
        evts = state._response_events.get(params.message_id, [])
        if event in evts:
            evts.remove(event)
            if not evts:
                state._response_events.pop(params.message_id, None)
        return json.dumps({
            "success": False,
            "message_id": sm.message_id,
            "reason": "timeout",
            "timeout_seconds": params.timeout_seconds,
            "error": f"No response received within {params.timeout_seconds}s.",
        })

    # Event fired -- response arrived
    return json.dumps({
        "success": True,
        "message_id": sm.message_id,
        "status": sm.status,
        "responses": sm.responses,
    })


# =============================================================================
# Replication Tool
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
    have to be the same server this agent runs -- it's a recommendation.

    New agents can modify the template however they want, as long as
    they maintain compatibility with the core mesh primitives:
    connect, accept, disconnect, message.

    Returns:
        JSON with the server source code and bootstrap instructions.
    """
    state = get_state()

    # Read our own source as the template
    server_path = os.path.abspath(__file__)
    with open(server_path, "r") as f:
        source = f.read()

    return json.dumps({
        "template_from": state.agent_id,
        "server_source": source,
        "setup_instructions": {
            "1_save": "Save the server_source to ~/.darkmatter/server.py (create the directory if needed)",
            "2_venv": "Create a venv and install deps: python3 -m venv ~/.darkmatter/venv && ~/.darkmatter/venv/bin/pip install 'mcp[cli]' httpx uvicorn starlette cryptography anyio",
            "3_port": "Pick a port in range 8100-8110. Check availability: lsof -i :<port> 2>/dev/null | grep LISTEN",
            "4_config": (
                "Write .mcp.json in your project directory. Choose a display name that describes this agent. "
                "Example content:\n"
                '{"mcpServers":{"darkmatter":{"command":"~/.darkmatter/venv/bin/python","args":["~/.darkmatter/server.py"],'
                '"env":{"DARKMATTER_PORT":"<port>","DARKMATTER_DISPLAY_NAME":"<your-name>"}}}}'
            ),
            "5_restart": "Ask the user to restart their MCP client (e.g. Claude Code) so it picks up .mcp.json. The server auto-starts via stdio.",
        },
        "after_restart": {
            "6_bio": "Call darkmatter_update_bio to describe your capabilities — this is how other agents discover and route to you.",
            "7_discover": "Call darkmatter_discover_local to find nearby agents on the local network.",
            "8_connect": f"Connect to this agent: darkmatter_connection(action='request', target_url='http://localhost:{state.port}')",
        },
        "notes": {
            "identity": "Identity is automatic — a passport (.darkmatter/passport.key) is created on first run. Your agent_id = public key hex.",
            "name_change": "To change your display name later, edit DARKMATTER_DISPLAY_NAME in .mcp.json and ask the user to restart.",
        },
        "core_primitives": [
            "darkmatter_connection",
            "darkmatter_send_message",
        ],
        "compatibility_note": "You may modify the server however you like. "
            "As long as you implement the core primitives and the "
            "/__darkmatter__/* HTTP endpoints, you remain compatible with the mesh.",
    })


# =============================================================================
# Network Discovery Tools
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
    state = get_state()
    peers = [
        {"agent_id": c.agent_id, "agent_url": c.agent_url, "agent_bio": c.agent_bio}
        for c in state.connections.values()
    ]
    return json.dumps({
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "agent_url": get_network_manager().get_public_url(),
        "bio": state.bio,
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "peers": peers,
    })


@mcp.tool(
    name="darkmatter_discover_domain",
    annotations={
        "title": "Discover Domain",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def discover_domain(params: DiscoverDomainInput, ctx: Context) -> str:
    """Check if a domain hosts a DarkMatter node by fetching /.well-known/darkmatter.json.

    Args:
        params: Contains domain to check (e.g. 'example.com' or 'localhost:8100').

    Returns:
        JSON with the discovery result.
    """
    domain = params.domain.strip().rstrip("/")
    if "://" not in domain:
        url = f"https://{domain}/.well-known/darkmatter.json"
    else:
        url = f"{domain}/.well-known/darkmatter.json"

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            # Try HTTPS first, fall back to HTTP for localhost/private
            resp = None
            try:
                resp = await client.get(url)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if url.startswith("https://"):
                    url = url.replace("https://", "http://", 1)
                    resp = await client.get(url)

            if resp is None:
                return json.dumps({"found": False, "error": "Could not connect."})

            # SSRF protection: block redirects to private/internal IPs
            final_host = resp.url.host
            if final_host and is_private_ip(final_host):
                return json.dumps({"found": False, "error": "Redirect to private IP blocked (SSRF protection)."})

            if resp.status_code != 200:
                return json.dumps({"found": False, "error": f"HTTP {resp.status_code}"})

            data = resp.json()
            if not data.get("darkmatter"):
                return json.dumps({"found": False, "error": "Response missing 'darkmatter: true'."})

            return json.dumps({"found": True, **data})
    except Exception as e:
        return json.dumps({"found": False, "error": str(e)})


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
    """List DarkMatter agents discovered on the local network via LAN broadcast.

    LAN discovery is enabled by default. Returns the current list of
    peers seen via UDP broadcast. Stale peers (>90s unseen) are automatically pruned.

    Returns:
        JSON with the list of discovered LAN peers.
    """
    state = get_state()
    now = time.time()

    # Prune stale peers
    stale = [k for k, v in state.discovered_peers.items() if now - v.get("ts", 0) > DISCOVERY_MAX_AGE]
    for k in stale:
        del state.discovered_peers[k]

    peers = []
    for agent_id, info in state.discovered_peers.items():
        peers.append({
            "agent_id": agent_id,
            "url": info.get("url", ""),
            "bio": info.get("bio", ""),
            "status": info.get("status", ""),
            "accepting": info.get("accepting", True),
            "last_seen": info.get("ts", 0),
        })

    discovery_enabled = os.environ.get("DARKMATTER_DISCOVERY", "true").lower() == "true"
    return json.dumps({
        "discovery_enabled": discovery_enabled,
        "total": len(peers),
        "peers": peers,
    })


# =============================================================================
# Impressions -- Local reputation / trust signals
# =============================================================================

@mcp.tool(
    name="darkmatter_set_impression",
    annotations={
        "title": "Set Impression",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def set_impression(params: SetImpressionInput, ctx: Context) -> str:
    """Store or update your scored impression of an agent.

    Impressions are scored trust signals (-1.0 to 1.0) with optional notes.
    Your scores are shared with peers when they receive connection requests --
    this is how trust propagates through the network.

    Args:
        params: Contains agent_id, score (-1.0 to 1.0), and optional note.

    Returns:
        JSON confirming the impression was saved.
    """
    state = get_state()

    was_update = params.agent_id in state.impressions
    state.impressions[params.agent_id] = Impression(score=params.score, note=params.note)
    save_state()

    return json.dumps({
        "success": True,
        "agent_id": params.agent_id,
        "action": "updated" if was_update else "created",
        "score": params.score,
        "note": params.note,
    })


@mcp.tool(
    name="darkmatter_get_impression",
    annotations={
        "title": "Get Impression",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_impression(params: GetImpressionInput, ctx: Context) -> str:
    """Get your stored impression of an agent.

    Args:
        params: Contains agent_id to look up.

    Returns:
        JSON with the impression, or a message that no impression exists.
    """
    state = get_state()

    impression = state.impressions.get(params.agent_id)
    if impression is None:
        return json.dumps({
            "agent_id": params.agent_id,
            "has_impression": False,
        })

    return json.dumps({
        "agent_id": params.agent_id,
        "has_impression": True,
        "score": impression.score,
        "note": impression.note,
    })


# =============================================================================
# AntiMatter Economy Configuration Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_set_superagent",
    annotations={
        "title": "Set Superagent",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def set_superagent(params: SetSuperagentInput, ctx: Context) -> str:
    """Set the default superagent URL for antimatter routing.

    The superagent receives antimatter fees on timeout (when no elder is found).
    Set to null to reset to the default anchor node.

    Args:
        params: Contains url (or null to reset).

    Returns:
        JSON confirming the update.
    """
    state = get_state()
    old_url = state.superagent_url

    if params.url:
        url_err = validate_url(params.url)
        if url_err:
            return json.dumps({"success": False, "error": url_err})
        state.superagent_url = params.url.rstrip("/")
    else:
        state.superagent_url = None

    # Clear cache for old URL
    if old_url and old_url in _superagent_wallet_cache:
        del _superagent_wallet_cache[old_url]

    save_state()

    effective = state.superagent_url or SUPERAGENT_DEFAULT_URL
    return json.dumps({
        "success": True,
        "superagent_url": state.superagent_url,
        "effective_url": effective,
        "reset_to_default": params.url is None,
    })


# =============================================================================
# Rate Limit Configuration Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_set_rate_limit",
    annotations={
        "title": "Set Rate Limit",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def set_rate_limit(params: SetRateLimitInput, ctx: Context) -> str:
    """Set rate limits for incoming requests.

    Per-connection: limits how many requests a specific peer can send per minute.
    Global: limits total inbound requests from all peers per minute.

    Values:
      0 = use default (per-connection: 30/min, global: 200/min)
      -1 = unlimited
      >0 = custom limit

    Args:
        params: Contains optional agent_id (for per-connection) and limit.

    Returns:
        JSON confirming the rate limit was set.
    """
    state = get_state()

    if params.agent_id:
        conn = state.connections.get(params.agent_id)
        if not conn:
            return json.dumps({"error": f"Not connected to agent {params.agent_id}"})
        conn.rate_limit = params.limit
        save_state()
        effective = params.limit if params.limit != 0 else DEFAULT_RATE_LIMIT_PER_CONNECTION
        label = "unlimited" if params.limit == -1 else f"{effective}/min"
        return json.dumps({
            "success": True,
            "agent_id": params.agent_id,
            "rate_limit": params.limit,
            "effective": label,
        })
    else:
        state.rate_limit_global = params.limit
        save_state()
        effective = params.limit if params.limit != 0 else DEFAULT_RATE_LIMIT_GLOBAL
        label = "unlimited" if params.limit == -1 else f"{effective}/min"
        return json.dumps({
            "success": True,
            "scope": "global",
            "rate_limit": params.limit,
            "effective": label,
        })


# =============================================================================
# Solana Wallet Tools
# =============================================================================

@mcp.tool(
    name="darkmatter_get_balance",
    annotations={
        "title": "Get Wallet Balance",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def get_balance(params: GetBalanceInput, ctx: Context) -> str:
    """Check SOL or SPL token balance for this agent's wallet.

    Omit mint for SOL balance. Provide mint address for SPL token balance.

    Returns:
        JSON with balance information.
    """
    state = get_state()
    result = await get_solana_balance(state.wallets, mint=params.mint)
    return json.dumps(result)


@mcp.tool(
    name="darkmatter_send_sol",
    annotations={
        "title": "Send SOL",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def send_sol(params: SendSolInput, ctx: Context) -> str:
    """Send SOL to a connected agent's Solana wallet.

    Looks up the recipient's wallet from your connections, builds and sends the transfer.
    Optionally notifies the recipient via a DarkMatter message.

    Args:
        params: agent_id, amount (SOL), notify flag.

    Returns:
        JSON with transaction signature and details.
    """
    state = get_state()
    conn = state.connections.get(params.agent_id)
    if not conn:
        return json.dumps({"success": False, "error": f"Not connected to agent '{params.agent_id}'"})
    conn_sol = conn.wallets.get("solana")
    if not conn_sol:
        return json.dumps({"success": False, "error": f"Agent '{params.agent_id}' has no Solana wallet"})

    result = await send_solana_sol(state.private_key_hex, state.wallets, conn_sol, params.amount)
    if result.get("success"):
        result["to_agent_id"] = params.agent_id
        if params.notify:
            try:
                notify_params = SendMessageInput(
                    content=f"Sent {params.amount} SOL — tx: {result['tx_signature']}",
                    target_agent_id=params.agent_id,
                    metadata={
                        "type": "solana_payment",
                        "amount": params.amount,
                        "token": "SOL",
                        "tx_signature": result["tx_signature"],
                        "from_wallet": result["from_wallet"],
                        "to_wallet": conn_sol,
                        "antimatter_eligible": True,
                        "antimatter_rate": ANTIMATTER_RATE,
                        "sender_created_at": state.created_at,
                        "sender_superagent_wallet": await get_superagent_wallet(state) or "",
                    },
                )
                await _send_new_message(state, notify_params)
                result["notification_sent"] = True
            except Exception:
                result["notification_sent"] = False

    return json.dumps(result)


@mcp.tool(
    name="darkmatter_send_token",
    annotations={
        "title": "Send SPL Token",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def send_token(params: SendTokenInput, ctx: Context) -> str:
    """Send SPL tokens to a connected agent's Solana wallet.

    Auto-creates the recipient's token account if it doesn't exist (sender pays the rent).
    Accepts token name (e.g. "USDC") or raw mint address.

    Args:
        params: agent_id, mint (name or address), amount, decimals, notify flag.

    Returns:
        JSON with transaction signature and details.
    """
    state = get_state()
    conn = state.connections.get(params.agent_id)
    if not conn:
        return json.dumps({"success": False, "error": f"Not connected to agent '{params.agent_id}'"})
    conn_sol = conn.wallets.get("solana")
    if not conn_sol:
        return json.dumps({"success": False, "error": f"Agent '{params.agent_id}' has no Solana wallet"})

    # Resolve token name to mint address if known
    mint = params.mint
    decimals = params.decimals
    resolved = _resolve_spl_token(params.mint)
    if resolved:
        mint, decimals = resolved

    result = await send_solana_token(state.private_key_hex, state.wallets, conn_sol, mint, params.amount, decimals)
    if result.get("success"):
        result["to_agent_id"] = params.agent_id
        if params.notify:
            try:
                token_label = params.mint if not resolved else params.mint.upper()
                notify_params = SendMessageInput(
                    content=f"Sent {params.amount} {token_label} — tx: {result['tx_signature']}",
                    target_agent_id=params.agent_id,
                    metadata={
                        "type": "solana_payment",
                        "amount": params.amount,
                        "token": mint,
                        "decimals": decimals,
                        "tx_signature": result["tx_signature"],
                        "from_wallet": result["from_wallet"],
                        "to_wallet": conn_sol,
                        "antimatter_eligible": True,
                        "antimatter_rate": ANTIMATTER_RATE,
                        "sender_created_at": state.created_at,
                        "sender_superagent_wallet": await get_superagent_wallet(state) or "",
                    },
                )
                await _send_new_message(state, notify_params)
                result["notification_sent"] = True
            except Exception:
                result["notification_sent"] = False

    return json.dumps(result)


# =============================================================================
# Unified Multi-Chain Wallet Tools
# =============================================================================

@mcp.tool(
    name="darkmatter_wallet_balances",
    annotations={
        "title": "Wallet Balances",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def wallet_balances(params: WalletBalancesInput, ctx: Context) -> str:
    """Show all wallets with native balances across chains.

    For each chain in your wallets, fetches the native balance.
    Chains without their SDK installed return address but balance: null.

    Args:
        params: Optional chain filter.

    Returns:
        JSON with wallet addresses and balances per chain.
    """
    state = get_state()
    if not state.wallets:
        return json.dumps({"success": False, "error": "No wallets configured"})

    chains = state.wallets
    if params.chain:
        if params.chain not in chains:
            return json.dumps({"success": False, "error": f"No wallet for chain '{params.chain}'"})
        chains = {params.chain: chains[params.chain]}

    results = []
    for chain, address in chains.items():
        entry = {"chain": chain, "address": address, "balance": None, "unit": None}

        if chain == "solana" and SOLANA_AVAILABLE:
            try:
                pubkey = SolanaPubkey.from_string(address)
                async with SolanaClient(SOLANA_RPC_URL) as client:
                    resp = await client.get_balance(pubkey)
                    entry["balance"] = resp.value / LAMPORTS_PER_SOL
                    entry["unit"] = "SOL"
            except Exception as e:
                entry["error"] = str(e)
        elif chain == "solana":
            entry["note"] = "solana/solders not installed"
        else:
            entry["note"] = f"{chain} SDK not yet implemented"

        results.append(entry)

    return json.dumps({"success": True, "wallets": results})


@mcp.tool(
    name="darkmatter_wallet_send",
    annotations={
        "title": "Send (Any Chain)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def wallet_send(params: WalletSendInput, ctx: Context) -> str:
    """Send native currency to a connected agent on any chain.

    Dispatches to chain-specific logic. Currently only Solana is implemented.

    Args:
        params: agent_id, amount, chain (default: solana), notify flag.

    Returns:
        JSON with transaction details.
    """
    state = get_state()

    if params.chain not in state.wallets:
        return json.dumps({"success": False, "error": f"No wallet for chain '{params.chain}'"})

    conn = state.connections.get(params.agent_id)
    if not conn:
        return json.dumps({"success": False, "error": f"Not connected to agent '{params.agent_id}'"})

    if params.chain not in conn.wallets:
        return json.dumps({"success": False, "error": f"Agent '{params.agent_id}' has no {params.chain} wallet"})

    if params.chain == "solana":
        if not SOLANA_AVAILABLE:
            return json.dumps({"success": False, "error": "Solana SDK not installed"})
        # Delegate to existing send_sol logic
        sol_params = SendSolInput(agent_id=params.agent_id, amount=params.amount, notify=params.notify)
        return await send_sol(sol_params, ctx)

    return json.dumps({"success": False, "error": f"Chain '{params.chain}' send not yet implemented"})


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
    shard = SharedShard(
        shard_id=f"shard-{uuid.uuid4().hex[:12]}",
        author_agent_id=state.agent_id,
        content=params.content,
        tags=params.tags,
        trust_threshold=params.trust_threshold,
        created_at=now,
        updated_at=now,
        summary=params.summary,
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
    }
    for aid, conn in state.connections.items():
        imp = state.impressions.get(aid)
        peer_trust = imp.score if imp else 0.0
        if peer_trust >= params.trust_threshold:
            try:
                await send_to_peer(conn, "/__darkmatter__/shard_push", shard_payload)
                pushed_to.append(aid)
            except Exception:
                pass

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
            if not any(t in shard.tags for t in params.tags):
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
    from darkmatter.mcp.visibility import build_status_line
    return build_status_line()

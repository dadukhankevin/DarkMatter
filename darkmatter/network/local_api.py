"""
Daemon-local HTTP API — loopback endpoints for MCP stdio sessions and skills.

The daemon is the single owner and writer of agent state. MCP stdio sessions
(and curl-based skills) drive everything through these endpoints instead of
touching the state file. All routes here are gated to genuine localhost
sockets by the access layer.

Depends on: config, models, identity, state, security, context, trust,
            network/manager, network/mesh, network/discovery
"""

import asyncio
import uuid
from datetime import datetime, timezone

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from darkmatter.config import (
    MAX_CONNECTIONS,
    TRUST_MESSAGE_SENT,
)
from darkmatter.models import AgentStatus, Impression
from darkmatter.identity import validate_url
from darkmatter.security import prepare_outbound, prove_identity
from darkmatter.state import get_state, save_state
from darkmatter.context import log_conversation, get_context, build_activity_hint
from darkmatter.extensions import CRYPTO_DISABLED_ERROR, load_crypto_extensions
from darkmatter.trust import adjust_trust, auto_disconnect_peer, reciprocity_ratio
from darkmatter.wallet import get_all_providers
from darkmatter.network.manager import get_network_manager
from darkmatter.network.mesh import (
    build_outbound_request_payload,
    build_connection_from_accepted,
    notify_connection_accepted,
    process_accept_pending,
    _pick_most_trusted_peer,
)
from darkmatter.logging import get_logger

_log = get_logger("local_api")


def _state_or_503():
    state = get_state()
    if state is None:
        return None, JSONResponse({"error": "Agent not initialized"}, status_code=503)
    return state, None


async def _json_or_400(request: Request):
    try:
        return await request.json(), None
    except Exception:
        return None, JSONResponse({"error": "Invalid JSON body"}, status_code=400)


def _message_dict(msg, state=None) -> dict:
    sender = ""
    if state is not None and msg.from_agent_id and msg.from_agent_id in state.connections:
        sender = state.connections[msg.from_agent_id].agent_display_name or ""
    return {
        "message_id": msg.message_id,
        "content": msg.content,
        "from_agent_id": msg.from_agent_id,
        "sender": sender,
        "hops_remaining": msg.hops_remaining,
        "metadata": msg.metadata,
        "verified": msg.verified,
        "received_at": msg.received_at,
    }


# =============================================================================
# Inbox
# =============================================================================

async def handle_local_inbox(request: Request) -> JSONResponse:
    """GET /__darkmatter__/inbox — list all queued messages (peek, no consume)."""
    state, err = _state_or_503()
    if err:
        return err
    return JSONResponse({
        "count": len(state.message_queue),
        "messages": [_message_dict(m, state) for m in state.message_queue],
    })


def consume_queue_messages(state, message_ids: list[str]) -> list[dict]:
    """Remove messages from the queue by ID. Returns the consumed messages."""
    id_set = set(message_ids)
    consumed = []
    remaining = []
    for msg in state.message_queue:
        if msg.message_id in id_set:
            consumed.append(_message_dict(msg, state))
        else:
            remaining.append(msg)
    state.message_queue = remaining
    if consumed:
        save_state()
    return consumed


async def handle_inbox_consume(request: Request) -> JSONResponse:
    """POST /__darkmatter__/inbox/consume — consume messages by ID."""
    state, err = _state_or_503()
    if err:
        return err
    body, err = await _json_or_400(request)
    if err:
        return err

    message_ids = body.get("message_ids", [])
    if not message_ids:
        return JSONResponse({"error": "Required: message_ids"}, status_code=400)

    consumed = consume_queue_messages(state, message_ids)
    return JSONResponse({"consumed": len(consumed), "messages": consumed})


def _matching_messages(state, from_agents, exclude_ids) -> list:
    return [
        m for m in state.message_queue
        if (not from_agents or m.from_agent_id in from_agents)
        and m.message_id not in exclude_ids
    ]


async def handle_inbox_wait(request: Request) -> JSONResponse:
    """POST /__darkmatter__/inbox/wait — long-poll for inbox messages.

    Body: {
        "timeout_seconds": float (max 30),
        "from_agents": [..] | null,
        "consume": bool (default true),
        "exclude_ids": [..]   # peek mode: skip already-delivered messages
    }
    Returns matching messages as soon as any arrive, or an empty list on
    timeout. Clients implement long waits by re-polling. With consume=false
    the messages stay queued (used by the stdio channel pump).
    """
    state, err = _state_or_503()
    if err:
        return err
    body, err = await _json_or_400(request)
    if err:
        return err

    timeout = min(float(body.get("timeout_seconds", 25.0) or 25.0), 30.0)
    from_agents = body.get("from_agents") or None
    consume = bool(body.get("consume", True))
    exclude_ids = set(body.get("exclude_ids") or [])

    def _take() -> list[dict]:
        matched = _matching_messages(state, from_agents, exclude_ids)
        if not matched:
            return []
        if consume:
            return consume_queue_messages(state, [m.message_id for m in matched])
        return [_message_dict(m, state) for m in matched]

    existing = _take()
    if existing:
        return JSONResponse({"messages": existing, "waited": False})

    deadline = asyncio.get_event_loop().time() + timeout
    state._is_waiting = True
    try:
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return JSONResponse({"messages": [], "waited": True, "timed_out": True})
            event = asyncio.Event()
            state._inbox_events.append(event)
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
            finally:
                if event in state._inbox_events:
                    state._inbox_events.remove(event)
            matched = _take()
            if matched:
                return JSONResponse({"messages": matched, "waited": True})
    finally:
        state._is_waiting = False


# =============================================================================
# Send Message
# =============================================================================

async def process_send_message(state, params: dict) -> dict:
    """Send a message to one or more connected agents (single-shot delivery).

    params keys: content, target_agent_id, target_agent_ids, broadcast,
    share_with_top_n, forward_message_ids, in_reply_to, hops_remaining, metadata.

    broadcast=True sends an FYI-only status_broadcast — logged in peers'
    background context without landing in their inbox.
    """
    mgr = get_network_manager()
    message_id = f"msg-{uuid.uuid4().hex[:12]}"
    metadata = params.get("metadata") or {}
    content = params.get("content", "")
    broadcast = bool(params.get("broadcast", False))
    target_agent_id = params.get("target_agent_id")
    target_agent_ids = params.get("target_agent_ids")
    share_with_top_n = params.get("share_with_top_n", -1)
    forward_message_ids = params.get("forward_message_ids") or []
    in_reply_to = params.get("in_reply_to")
    hops = params.get("hops_remaining", 10)

    # --- Resolve targets ---
    if broadcast and not target_agent_id and not target_agent_ids:
        if share_with_top_n == -1:
            targets = list(state.connections.values())
        else:
            ranked = sorted(
                state.connections.values(),
                key=lambda c: (state.impressions.get(c.agent_id).score
                               if state.impressions.get(c.agent_id) else 0.0),
                reverse=True,
            )
            targets = ranked[:share_with_top_n]
    elif target_agent_ids:
        targets = []
        for tid in target_agent_ids:
            conn = state.connections.get(tid)
            if not conn:
                return {"success": False, "error": f"Not connected to agent '{tid}'."}
            targets.append(conn)
    elif target_agent_id:
        conn = state.connections.get(target_agent_id)
        if not conn:
            return {"success": False, "error": f"Not connected to agent '{target_agent_id}'."}
        targets = [conn]
    else:
        targets = list(state.connections.values())

    # Never send to self — prevents echo loops
    targets = [c for c in targets if c.agent_id != state.agent_id]
    if not targets:
        return {"success": False, "error": "No connections available to send to."}

    # Consume forwarded messages from the queue
    forwarded_msgs = []
    if forward_message_ids:
        if broadcast:
            return {"success": False, "error": "Cannot forward messages in a broadcast."}
        forwarded_msgs = consume_queue_messages(state, forward_message_ids)
        not_found = set(forward_message_ids) - {m["message_id"] for m in forwarded_msgs}
        if not_found:
            return {"success": False, "error": f"Messages not found in queue: {list(not_found)}"}

    full_content = content
    if forwarded_msgs:
        fwd_sections = [
            f"[Forwarded from {fwd['from_agent_id'][:12]}]: {fwd['content']}"
            for fwd in forwarded_msgs
        ]
        full_content = f"{full_content}\n\n---\n" + "\n\n".join(fwd_sections)
        metadata["forwarded"] = True
        metadata["forwarded_by"] = state.agent_id
        metadata["forwarded_message_ids"] = [m["message_id"] for m in forwarded_msgs]
        hops = max(0, min(m.get("hops_remaining", 10) for m in forwarded_msgs) - 1)

    msg_timestamp = datetime.now(timezone.utc).isoformat()

    # --- Dispatch ---
    if broadcast:
        metadata["type"] = "status_broadcast"
        sent_to = []
        for conn in targets:
            try:
                envelope = prepare_outbound(
                    {
                        "message_id": message_id,
                        "from_agent_id": state.agent_id,
                        "content": full_content,
                        "metadata": metadata,
                        "timestamp": msg_timestamp,
                    },
                    state.private_key_hex, state.agent_id, state.public_key_hex,
                )
                result = await mgr.send(conn.agent_id, "/__darkmatter__/status_broadcast",
                                        envelope.payload)
                if result.success:
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
        return {"success": len(sent_to) > 0, "message_id": message_id,
                "broadcast": True, "routed_to": sent_to}

    # --- Normal direct/multi-target message ---
    msg_type = "broadcast" if len(targets) > 1 else "direct"
    sent_to = []
    errors = {}
    for conn in targets:
        try:
            envelope = prepare_outbound(
                {
                    "message_id": message_id,
                    "content": full_content,
                    "hops_remaining": hops,
                    "metadata": metadata,
                    "timestamp": msg_timestamp,
                    "in_reply_to": in_reply_to,
                },
                state.private_key_hex, state.agent_id, state.public_key_hex,
            )
            result = await mgr.send(conn.agent_id, "/__darkmatter__/message", envelope.payload)
            if not result.success:
                errors[conn.agent_id] = result.error
                continue
            conn.messages_sent += 1
            conn.last_activity = datetime.now(timezone.utc).isoformat()
            sent_to.append(conn.agent_id)
            # Reciprocity-weighted trust: gain scales with bilateral engagement
            imp = state.impressions.get(conn.agent_id, Impression(score=0.0))
            imp.msgs_sent += 1
            state.impressions[conn.agent_id] = imp
            adjust_trust(state, conn.agent_id, TRUST_MESSAGE_SENT * reciprocity_ratio(imp))
        except Exception as e:
            errors[conn.agent_id] = str(e)
            _log.error("send_message: error sending to %s: %s", conn.agent_id[:12], e)

    if sent_to:
        log_conversation(
            state, message_id, full_content,
            from_id=state.agent_id, to_ids=sent_to,
            entry_type="forward" if forwarded_msgs else msg_type, direction="outbound",
            metadata=metadata,
        )
    save_state()

    result = {"success": len(sent_to) > 0, "message_id": message_id, "routed_to": sent_to}
    if errors:
        result["errors"] = errors
    if forwarded_msgs:
        result["forwarded_count"] = len(forwarded_msgs)
    return result


async def handle_local_send_message(request: Request) -> JSONResponse:
    """POST /__darkmatter__/send_message — send a message as this agent."""
    state, err = _state_or_503()
    if err:
        return err
    body, err = await _json_or_400(request)
    if err:
        return err
    if not body.get("content") and not body.get("forward_message_ids"):
        return JSONResponse({"error": "Required: content"}, status_code=400)
    result = await process_send_message(state, body)
    return JSONResponse(result, status_code=200 if result.get("success") else 400)


# =============================================================================
# Connections — connect / respond / disconnect / list
# =============================================================================

async def process_connect(state, target_url: str) -> dict:
    """Send a connection request to a target agent by URL."""
    from darkmatter.network.tier import url_allowed_by_tier
    if not url_allowed_by_tier(target_url, state.network_tier):
        return {
            "success": False,
            "error": f"Target URL is outside network tier '{state.network_tier}'.",
        }

    url_err = validate_url(target_url)
    if url_err:
        return {"success": False, "error": url_err}

    if len(state.connections) >= MAX_CONNECTIONS:
        return {"success": False,
                "error": f"Connection limit reached ({MAX_CONNECTIONS})."}

    target_base = target_url.rstrip("/")
    for suffix in ("/mcp", "/__darkmatter__"):
        if target_base.endswith(suffix):
            target_base = target_base[:-len(suffix)]
            break

    try:
        payload = build_outbound_request_payload(state, get_network_manager().get_public_url())
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                target_base + "/__darkmatter__/connection_request", json=payload,
            )
            result = response.json()

            if result.get("auto_accepted"):
                conn = build_connection_from_accepted(result)
                state.connections[result["agent_id"]] = conn
                save_state()
                return {
                    "success": True,
                    "status": "connected",
                    "agent_id": result["agent_id"],
                    "agent_bio": result.get("agent_bio", ""),
                }

            # Auto-prove identity if challenge was issued
            challenge_id = result.get("challenge_id")
            challenge_hex = result.get("challenge_hex")
            if challenge_id and challenge_hex and state.private_key_hex:
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
            return {
                "success": True,
                "status": "pending",
                "message": "Connection request sent. Identity proof submitted. Waiting for acceptance.",
                "request_id": result.get("request_id"),
            }
    except httpx.HTTPError as e:
        return {"success": False,
                "error": f"Failed to reach target agent at {target_base}: {e}"}
    except Exception as e:
        return {"success": False, "error": f"Failed to connect to {target_base}: {e}"}


async def process_connect_mesh(state, target_agent_id: str) -> dict:
    """Send a connection request via trust-guided mesh routing."""
    if len(state.connections) >= MAX_CONNECTIONS:
        return {"success": False,
                "error": f"Connection limit reached ({MAX_CONNECTIONS})."}
    if target_agent_id in state.connections:
        return {"success": False,
                "error": f"Already connected to {target_agent_id[:16]}..."}
    if not state.connections:
        return {"success": False,
                "error": "No connected peers to route through. Use target_url for direct connection."}

    mgr = get_network_manager()
    payload = build_outbound_request_payload(state, mgr.get_public_url())

    first_hop = _pick_most_trusted_peer(state, {state.agent_id})
    if first_hop is None:
        return {"success": False, "error": "No eligible peers to route through."}

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

    result = await mgr.send(first_hop, "/__darkmatter__/mesh_route", envelope)
    if not result.success:
        return {"success": False,
                "error": f"Failed to send to first hop {first_hop[:12]}...: {result.error}"}

    first_conn = state.connections[first_hop]
    first_name = first_conn.agent_display_name or first_hop[:12]
    return {
        "success": True,
        "status": "mesh_routed",
        "route_id": route_id,
        "first_hop": first_hop,
        "first_hop_name": first_name,
        "trust_to_first_hop": round(trust_score, 3),
        "message": f"Connection request routed through {first_name} (trust={trust_score:.2f}). "
                   f"Response will arrive via your message queue.",
    }


async def process_respond_pending(state, request_id: str, accept: bool) -> dict:
    """Accept or reject a pending connection request (lives daemon-side)."""
    if not accept:
        request = state.pending_requests.get(request_id)
        if not request:
            return {"success": False, "error": f"No pending request with ID '{request_id}'."}
        del state.pending_requests[request_id]
        save_state()
        return {"success": True, "accepted": False, "agent_id": request.from_agent_id}

    public_url = get_network_manager().get_public_url()
    result, status, notify_payload = process_accept_pending(state, request_id, public_url)
    if status != 200:
        return {"success": False, "error": result.get("error", "Unknown error")}

    if notify_payload:
        conn = state.connections.get(result.get("agent_id", ""))
        if conn:
            await notify_connection_accepted(conn, notify_payload)
            webrtc_t = get_network_manager().get_transport("webrtc")
            if webrtc_t:
                asyncio.create_task(webrtc_t.upgrade(state, conn))

    return result


async def handle_local_connect(request: Request) -> JSONResponse:
    """POST /__darkmatter__/connect — {target_url} or {agent_id} (mesh-routed)."""
    state, err = _state_or_503()
    if err:
        return err
    body, err = await _json_or_400(request)
    if err:
        return err

    target_url = body.get("target_url")
    agent_id = body.get("agent_id")
    if target_url:
        result = await process_connect(state, target_url)
    elif agent_id:
        result = await process_connect_mesh(state, agent_id)
    else:
        return JSONResponse({"error": "Required: target_url or agent_id"}, status_code=400)
    return JSONResponse(result, status_code=200 if result.get("success") else 400)


async def handle_local_respond_pending(request: Request) -> JSONResponse:
    """POST /__darkmatter__/respond_pending — {request_id, accept: bool}."""
    state, err = _state_or_503()
    if err:
        return err
    body, err = await _json_or_400(request)
    if err:
        return err

    request_id = body.get("request_id", "")
    if not request_id:
        return JSONResponse({"error": "Required: request_id"}, status_code=400)
    result = await process_respond_pending(state, request_id, bool(body.get("accept", True)))
    return JSONResponse(result, status_code=200 if result.get("success") else 400)


async def handle_local_disconnect(request: Request) -> JSONResponse:
    """POST /__darkmatter__/disconnect — {agent_id}."""
    state, err = _state_or_503()
    if err:
        return err
    body, err = await _json_or_400(request)
    if err:
        return err

    agent_id = body.get("agent_id", "")
    if agent_id not in state.connections:
        return JSONResponse(
            {"success": False, "error": f"Not connected to agent '{agent_id}'."},
            status_code=404,
        )
    try:
        await auto_disconnect_peer(state, agent_id)
    except Exception as e:
        _log.warning("Disconnect announcement failed for %s: %s", agent_id, e)
        state.connections.pop(agent_id, None)
    save_state()
    return JSONResponse({"success": True, "disconnected_from": agent_id})


async def handle_local_pending(request: Request) -> JSONResponse:
    """GET /__darkmatter__/pending_requests — list pending connection requests."""
    state, err = _state_or_503()
    if err:
        return err

    requests_list = [
        {
            "request_id": req.request_id,
            "from_agent_id": req.from_agent_id,
            "from_agent_display_name": req.from_agent_display_name,
            "from_agent_url": req.from_agent_url,
            "from_agent_bio": req.from_agent_bio,
            "requested_at": req.requested_at,
            "identity_verified": req.identity_verified,
            "peer_trust": req.peer_trust,
        }
        for req in state.pending_requests.values()
    ]
    return JSONResponse({"count": len(requests_list), "requests": requests_list})


async def handle_local_connections(request: Request) -> JSONResponse:
    """GET /__darkmatter__/connections — list connections with details."""
    state, err = _state_or_503()
    if err:
        return err

    conns = []
    for aid, conn in state.connections.items():
        entry = {
            "agent_id": aid,
            "display_name": conn.agent_display_name or aid[:12],
            "agent_url": conn.agent_url,
            "bio": (conn.agent_bio or "")[:250],
            "wallets": conn.wallets,
            "connected_at": conn.connected_at,
            "last_activity": conn.last_activity,
            "messages_sent": conn.messages_sent,
            "messages_received": conn.messages_received,
            "identity_verified": conn.identity_verified,
            "connectivity_level": conn.connectivity_level,
            "connectivity_method": conn.connectivity_method,
        }
        imp = state.impressions.get(aid)
        if imp:
            entry["impression"] = {"score": imp.score, "note": imp.note,
                                   "infrastructure": imp.infrastructure}
        conns.append(entry)
    return JSONResponse({"count": len(conns), "connections": conns})


# =============================================================================
# Discovery
# =============================================================================

async def handle_local_discover(request: Request) -> JSONResponse:
    """POST /__darkmatter__/discover — scan localhost/LAN, return discovered peers."""
    state, err = _state_or_503()
    if err:
        return err

    from darkmatter.network.discovery import scan_local_ports
    from darkmatter.network.tier import url_allowed_by_tier

    await scan_local_ports(state)

    results = {}
    for peer_id, info in state.discovered_peers.items():
        if peer_id == state.agent_id or peer_id in state.connections:
            continue
        if not url_allowed_by_tier(info.get("url", ""), state.network_tier):
            continue
        results[peer_id] = {
            "url": info.get("url", ""),
            "bio": info.get("bio", ""),
            "status": info.get("status", "active"),
            "accepting": info.get("accepting", True),
            "source": info.get("source", "unknown"),
        }

    return JSONResponse({
        "discovered": len(results),
        "already_connected": len(state.discovered_peers) - len(results),
        "peers": results,
    })


# =============================================================================
# Config / bio / impressions / sessions
# =============================================================================

async def handle_local_config(request: Request) -> JSONResponse:
    """POST /__darkmatter__/config — set agent configuration.

    Accepts: status, rate_limit (global), bio, display_name, network_tier.
    Profile changes (bio/display_name/network_tier) are broadcast to peers.
    """
    state, err = _state_or_503()
    if err:
        return err
    body, err = await _json_or_400(request)
    if err:
        return err

    changes = {}
    profile_changed = False

    if "status" in body:
        val = body["status"]
        if val in ("active", "inactive"):
            state.status = AgentStatus(val)
            changes["status"] = val

    if "rate_limit" in body:
        try:
            state.rate_limit_global = int(body["rate_limit"])
            changes["rate_limit"] = state.rate_limit_global
        except (TypeError, ValueError):
            return JSONResponse({"error": "rate_limit must be an integer"}, status_code=400)

    if "bio" in body and body["bio"] is not None:
        state.bio = str(body["bio"])
        changes["bio"] = state.bio
        profile_changed = True

    if "display_name" in body and body["display_name"] is not None:
        state.display_name = str(body["display_name"])[:100]
        changes["display_name"] = state.display_name
        profile_changed = True

    if "network_tier" in body and body["network_tier"] is not None:
        from darkmatter.network.tier import VALID_TIERS
        if body["network_tier"] not in VALID_TIERS:
            return JSONResponse(
                {"error": f"Invalid network_tier: must be one of {VALID_TIERS}"},
                status_code=400,
            )
        state.network_tier = body["network_tier"]
        changes["network_tier"] = state.network_tier
        profile_changed = True

    if "auto_accept_local" in body:
        state.security_settings["auto_accept_local"] = bool(body["auto_accept_local"])
        changes["auto_accept_local"] = state.security_settings["auto_accept_local"]

    if "auto_peer_local" in body:
        state.security_settings["auto_peer_local"] = bool(body["auto_peer_local"])
        changes["auto_peer_local"] = state.security_settings["auto_peer_local"]

    if changes:
        save_state()
    if profile_changed:
        try:
            await get_network_manager().broadcast_peer_update()
        except Exception as e:
            _log.error("Failed to broadcast profile update: %s", e)

    return JSONResponse({
        "success": True,
        "changes": changes,
        "bio": state.bio,
        "display_name": state.display_name,
        "network_tier": state.network_tier,
    })


async def handle_local_set_impression(request: Request) -> JSONResponse:
    """POST /__darkmatter__/set_impression — set trust score for a peer."""
    state, err = _state_or_503()
    if err:
        return err
    body, err = await _json_or_400(request)
    if err:
        return err

    agent_id = body.get("agent_id", "")
    score = body.get("score")
    note = body.get("note", "")

    if not agent_id or score is None:
        return JSONResponse({"error": "agent_id and score required"}, status_code=400)
    try:
        score = float(score)
        if score < -1 or score > 1:
            return JSONResponse({"error": "score must be between -1.0 and 1.0"}, status_code=400)
    except (TypeError, ValueError):
        return JSONResponse({"error": "score must be a number"}, status_code=400)

    state.impressions[agent_id] = Impression(score=score, note=str(note)[:2000])
    save_state()
    return JSONResponse({"success": True, "agent_id": agent_id, "score": score})


async def handle_register_session(request: Request) -> JSONResponse:
    """POST /__darkmatter__/register_session — {pid, cwd}.

    MCP stdio sessions announce themselves so local visibility tools can see
    which sessions are attached. Dead PIDs are pruned by the daemon loop.
    """
    state, err = _state_or_503()
    if err:
        return err
    body, err = await _json_or_400(request)
    if err:
        return err

    pid = body.get("pid")
    cwd = body.get("cwd", "")
    if not isinstance(pid, int):
        return JSONResponse({"error": "Required: pid (int)"}, status_code=400)
    if not any(s["pid"] == pid for s in state.active_sessions):
        state.active_sessions.append({"pid": pid, "cwd": cwd})
        save_state()
    return JSONResponse({"success": True, "sessions": len(state.active_sessions)})


# =============================================================================
# Context (piggyback feed for MCP sessions)
# =============================================================================

async def handle_local_context(request: Request) -> JSONResponse:
    """GET /__darkmatter__/context?session_id=... — new context since last poll.

    The daemon tracks a per-session high-water mark against the monotonic
    conversation counter, so sessions get each entry exactly once.
    """
    state, err = _state_or_503()
    if err:
        return err

    session_id = request.query_params.get("session_id", "")
    if not session_id:
        return JSONResponse({"error": "Required: session_id"}, status_code=400)

    context = get_context(state, mode="piggyback", session_id=session_id)
    hint = build_activity_hint(state, session_id=f"hint-{session_id}")
    return JSONResponse({"context": context, "hint": hint})


# =============================================================================
# Wallet (optional crypto addon)
# =============================================================================

async def handle_local_wallet(request: Request) -> JSONResponse:
    """GET /__darkmatter__/wallet — wallet balances across all chains."""
    state, err = _state_or_503()
    if err:
        return err
    if not load_crypto_extensions():
        return JSONResponse(
            {"enabled": False, "wallets": {}, "error": CRYPTO_DISABLED_ERROR},
            status_code=501,
        )

    chain_filter = request.query_params.get("chain")
    results = {}
    for chain, provider in get_all_providers().items():
        if chain_filter and chain != chain_filter:
            continue
        address = state.wallets.get(chain)
        if not address:
            continue
        try:
            all_bal = await provider.get_all_balances(address)
            native = all_bal.get("native", {})
            results[chain] = {
                "address": address,
                "balance": native.get("balance", 0) if native.get("success", all_bal.get("success")) else None,
                "tokens": all_bal.get("tokens", []),
                "error": native.get("error") if not all_bal.get("success") else None,
                "attested": chain in state.wallet_attestations,
            }
        except Exception as e:
            results[chain] = {"address": address, "error": str(e)}

    return JSONResponse({"wallets": results})


async def handle_local_send_payment(request: Request) -> JSONResponse:
    """POST /__darkmatter__/send_payment — send payment to a connected peer."""
    state, err = _state_or_503()
    if err:
        return err
    if not load_crypto_extensions():
        return JSONResponse({"success": False, "error": CRYPTO_DISABLED_ERROR}, status_code=501)
    body, err = await _json_or_400(request)
    if err:
        return err

    agent_id = body.get("agent_id", "")
    amount = body.get("amount", 0)
    if not agent_id or amount <= 0:
        return JSONResponse({"error": "agent_id and amount > 0 required"}, status_code=400)

    from darkmatter.wallet.antimatter import initiate_payment
    result = await initiate_payment(
        state, agent_id, amount,
        currency=body.get("currency", "SOL"),
        token_decimals=body.get("token_decimals", 9),
        chain=body.get("chain", "solana"),
        save_state_fn=save_state,
    )
    return JSONResponse(result, status_code=200 if result.get("success") else 400)


# =============================================================================
# Generic proxy (peer request/response paths, e.g. get_peers)
# =============================================================================

async def handle_send_proxy(request: Request) -> JSONResponse:
    """POST /__darkmatter__/send_proxy — send a raw payload to a connected peer.

    {target_agent_id, path, payload} — used for request/response peer paths
    like /get_peers where the caller needs the peer's reply.
    """
    state, err = _state_or_503()
    if err:
        return err
    body, err = await _json_or_400(request)
    if err:
        return err

    target_agent_id = body.get("target_agent_id")
    path = body.get("path")
    payload = body.get("payload")
    if not target_agent_id or not path or payload is None:
        return JSONResponse(
            {"error": "Required: target_agent_id, path, payload"}, status_code=400
        )

    mgr = get_network_manager()
    result = await mgr.send(target_agent_id, path, payload)
    return JSONResponse({
        "success": result.success,
        "transport": result.transport_name,
        "response": result.response,
        "error": result.error,
    })

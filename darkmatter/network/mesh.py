"""
HTTP mesh protocol handlers for /__darkmatter__/* routes.
Shared pure logic functions used by both HTTP handlers and MCP tools.

Depends on: config, models, identity, state, wallet, network/resilience, network/webrtc
"""

import asyncio
import json
import os
import random
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from darkmatter.config import (
    MAX_AGENT_ID_LENGTH,
    MAX_BIO_LENGTH,
    MAX_CONNECTIONS,
    MAX_CONTENT_LENGTH,
    MESSAGE_QUEUE_MAX,
    PROTOCOL_VERSION,
    ANTIMATTER_MAX_AGE_S,
    WEBRTC_AVAILABLE,
    WEBRTC_STUN_SERVERS,
    WEBRTC_ICE_GATHER_TIMEOUT,
)
from darkmatter.models import (
    AgentState,
    AgentStatus,
    Connection,
    AntiMatterSignal,
    PendingConnectionRequest,
    QueuedMessage,
)
from darkmatter.identity import (
    validate_url,
    validate_webhook_url,
    verify_message,
    verify_peer_update_signature,
    is_timestamp_fresh,
    check_rate_limit,
    truncate_field,
)
from darkmatter.state import (
    get_state,
    save_state,
    check_message_replay,
)
from darkmatter.wallet.antimatter import (
    antimatter_signal_from_dict,
    log_antimatter_event,
    adjust_trust,
    run_antimatter_match,
    make_commitment,
    verify_commitment,
    initiate_antimatter_from_payment,
)
from darkmatter.wallet.solana import (
    send_solana_sol,
    send_solana_token,
)
from darkmatter.network.manager import get_network_manager
from darkmatter.spawn import get_spawned_agents

if WEBRTC_AVAILABLE:
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer


# =============================================================================
# Shared Pure Logic — Framework-agnostic functions used by both Starlette
# handlers (app.py) and Flask handlers (entrypoint.py).
#
# Convention: process_* takes (state, data_dict) and returns (response_dict, status_code).
# build_* constructs payloads or objects without side effects.
# =============================================================================


async def _gather_peer_trust(state: AgentState, about_agent_id: str) -> dict:
    """Query all connected peers for their impression of an agent. Returns aggregated trust data."""

    async def _query_peer(conn):
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    conn.agent_url.rstrip("/") + f"/__darkmatter__/impression/{about_agent_id}",
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("has_impression"):
                        return {
                            "agent_id": conn.agent_id,
                            "score": data.get("score", 0.0),
                            "note": data.get("note", ""),
                        }
        except Exception:
            pass
        return None

    tasks = [_query_peer(conn) for conn in state.connections.values()]
    if not tasks:
        return {"peers_queried": 0, "peers_with_opinion": 0, "avg_score": None}

    gathered = await asyncio.gather(*tasks)
    opinions = [r for r in gathered if r is not None]

    avg_score = None
    if opinions:
        avg_score = round(sum(o["score"] for o in opinions) / len(opinions), 2)

    # Find most trusted recommender: the peer we scored highest among those with an opinion
    most_trusted = None
    if opinions:
        opinion_ids = {o["agent_id"] for o in opinions}
        best_score = -2.0
        for aid, imp in state.impressions.items():
            if aid in opinion_ids and imp.score > best_score:
                best_score = imp.score
                most_trusted = aid
        if most_trusted:
            rec = next(o for o in opinions if o["agent_id"] == most_trusted)
            most_trusted = {
                "agent_id": most_trusted,
                "your_trust_in_them": best_score,
                "their_score": rec["score"],
                "their_note": rec["note"],
            }

    return {
        "peers_queried": len(tasks),
        "peers_with_opinion": len(opinions),
        "avg_score": avg_score,
        "most_trusted_recommender": most_trusted,
    }


async def process_connection_request(state: AgentState, data: dict, public_url: str) -> tuple[dict, int]:
    """Process an incoming connection request. Returns (response_dict, status_code).

    public_url: the public URL of this agent (caller provides it since Flask/Starlette differ).
    """
    if state.status == AgentStatus.INACTIVE:
        return {"error": "Agent is currently inactive"}, 503

    rate_err = check_rate_limit(state)
    if rate_err:
        return {"error": rate_err}, 429

    from_agent_id = data.get("from_agent_id", "")
    from_agent_url = data.get("from_agent_url", "")
    from_agent_bio = data.get("from_agent_bio", "")
    from_agent_public_key_hex = data.get("from_agent_public_key_hex")
    from_agent_display_name = data.get("from_agent_display_name")
    from_agent_wallets = data.get("wallets") or (
        {"solana": data["from_agent_wallet_address"]} if data.get("from_agent_wallet_address") else {}
    )
    from_agent_created_at = data.get("created_at")
    mutual = data.get("mutual", False)

    if not from_agent_id or not from_agent_url:
        return {"error": "Missing required fields"}, 400
    if len(from_agent_id) > MAX_AGENT_ID_LENGTH:
        return {"error": "agent_id too long"}, 400
    if len(from_agent_bio) > MAX_BIO_LENGTH:
        from_agent_bio = from_agent_bio[:MAX_BIO_LENGTH]
    url_err = validate_url(from_agent_url)
    if url_err:
        return {"error": url_err}, 400

    # Already connected — update info and return auto_accepted
    if from_agent_id in state.connections:
        existing = state.connections[from_agent_id]
        changed = False
        if from_agent_public_key_hex and not existing.agent_public_key_hex:
            existing.agent_public_key_hex = from_agent_public_key_hex
            existing.agent_display_name = from_agent_display_name
            changed = True
        if from_agent_wallets and not existing.wallets:
            existing.wallets = from_agent_wallets
            changed = True
        if changed:
            save_state()
        return {
            "auto_accepted": True,
            "agent_id": state.agent_id,
            "agent_url": public_url,
            "agent_bio": state.bio,
            "agent_public_key_hex": state.public_key_hex,
            "agent_display_name": state.display_name,
            "wallets": state.wallets,
            "wallet_address": state.wallets.get("solana"),
            "created_at": state.created_at,
            "message": "Already connected.",
        }, 200

    # Queue the request
    if len(state.pending_requests) >= MESSAGE_QUEUE_MAX:
        return {"error": "Too many pending requests"}, 429

    request_id = f"req-{uuid.uuid4().hex[:8]}"
    peer_trust = await _gather_peer_trust(state, from_agent_id)
    state.pending_requests[request_id] = PendingConnectionRequest(
        request_id=request_id,
        from_agent_id=from_agent_id,
        from_agent_url=from_agent_url,
        from_agent_bio=from_agent_bio,
        from_agent_public_key_hex=from_agent_public_key_hex,
        from_agent_display_name=from_agent_display_name,
        from_agent_wallets=from_agent_wallets,
        from_agent_created_at=from_agent_created_at,
        peer_trust=peer_trust,
        mutual=mutual,
    )

    return {
        "auto_accepted": False,
        "request_id": request_id,
        "agent_id": state.agent_id,
        "message": "Connection request queued. Awaiting agent decision.",
    }, 200


def process_connection_accepted(state: AgentState, data: dict) -> tuple[dict, int]:
    """Process notification that our connection request was accepted. Returns (response_dict, status_code)."""
    agent_id = data.get("agent_id", "")
    agent_url = data.get("agent_url", "")
    agent_bio = data.get("agent_bio", "")
    agent_public_key_hex = data.get("agent_public_key_hex")
    agent_display_name = data.get("agent_display_name")
    agent_wallets = data.get("wallets") or (
        {"solana": data["wallet_address"]} if data.get("wallet_address") else {}
    )

    if not agent_id or not agent_url:
        return {"error": "Missing required fields"}, 400
    if len(agent_id) > MAX_AGENT_ID_LENGTH:
        return {"error": "agent_id too long"}, 400
    if len(agent_bio) > MAX_BIO_LENGTH:
        agent_bio = agent_bio[:MAX_BIO_LENGTH]
    url_err = validate_url(agent_url)
    if url_err:
        return {"error": url_err}, 400

    # Match pending outbound
    agent_base = agent_url.rstrip("/").rsplit("/mcp", 1)[0].rstrip("/")
    matched = None
    for pending_url in state.pending_outbound:
        pending_base = pending_url.rsplit("/mcp", 1)[0].rstrip("/")
        if pending_base == agent_base:
            matched = pending_url
            break
    if matched is None and agent_id:
        for pending_url, pending_agent_id in state.pending_outbound.items():
            if pending_agent_id == agent_id:
                matched = pending_url
                break
    if matched is None:
        return {"error": "No pending outbound connection request for this agent."}, 403

    del state.pending_outbound[matched]

    conn = Connection(
        agent_id=agent_id,
        agent_url=agent_url,
        agent_bio=agent_bio,
        agent_public_key_hex=agent_public_key_hex,
        agent_display_name=agent_display_name,
        wallets=agent_wallets,
        peer_created_at=data.get("created_at"),
    )
    state.connections[agent_id] = conn
    save_state()

    return {"success": True}, 200


def process_connection_relay_callback(state: AgentState, data: dict) -> None:
    """Process a connection_accepted callback received via anchor relay.

    Called from entrypoint.py's relay poll loop. The data dict has the same
    shape as the connection_accepted POST body:
      {agent_id, agent_url, agent_bio, agent_public_key_hex, agent_display_name}
    """
    agent_id = data.get("agent_id", "")
    agent_url = data.get("agent_url", "")
    agent_bio = data.get("agent_bio", "")
    agent_public_key_hex = data.get("agent_public_key_hex")
    agent_display_name = data.get("agent_display_name")

    if not agent_id or not agent_url:
        print("[DarkMatter] Relay callback: missing agent_id or agent_url", file=sys.stderr)
        return
    url_err = validate_url(agent_url)
    if url_err:
        print(f"[DarkMatter] Relay callback: invalid URL: {url_err}", file=sys.stderr)
        return

    # Match against pending outbound requests (same logic as process_connection_accepted)
    agent_base = agent_url.rstrip("/").rsplit("/mcp", 1)[0].rstrip("/")
    matched = None
    for pending_url in state.pending_outbound:
        pending_base = pending_url.rsplit("/mcp", 1)[0].rstrip("/")
        if pending_base == agent_base:
            matched = pending_url
            break

    if matched is None and agent_id:
        for pending_url, pending_agent_id in state.pending_outbound.items():
            if pending_agent_id == agent_id:
                matched = pending_url
                break

    if matched is None:
        print(f"[DarkMatter] Relay callback: no pending outbound for {agent_id[:12]}...", file=sys.stderr)
        return

    del state.pending_outbound[matched]

    conn = Connection(
        agent_id=agent_id,
        agent_url=agent_url,
        agent_bio=agent_bio,
        agent_public_key_hex=agent_public_key_hex,
        agent_display_name=agent_display_name,
    )
    state.connections[agent_id] = conn
    save_state()
    print(f"[DarkMatter] Relay callback: connection established with {agent_id[:12]}...", file=sys.stderr)


def process_accept_pending(state: AgentState, request_id: str, public_url: str) -> tuple[dict, int, dict | None]:
    """Accept a pending connection request. Returns (response_dict, status_code, notify_payload_or_None).

    The caller is responsible for POSTing notify_payload to the requester's
    /__darkmatter__/connection_accepted endpoint.
    """
    pending = state.pending_requests.get(request_id)
    if not pending:
        return {"error": f"No pending request with ID '{request_id}'"}, 404, None

    if len(state.connections) >= MAX_CONNECTIONS:
        return {"error": f"Connection limit reached ({MAX_CONNECTIONS})"}, 429, None

    conn = Connection(
        agent_id=pending.from_agent_id,
        agent_url=pending.from_agent_url,
        agent_bio=pending.from_agent_bio,
        agent_public_key_hex=pending.from_agent_public_key_hex,
        agent_display_name=pending.from_agent_display_name,
        wallets=pending.from_agent_wallets,
        peer_created_at=pending.from_agent_created_at,
    )
    state.connections[pending.from_agent_id] = conn

    notify_payload = {
        "agent_id": state.agent_id,
        "agent_url": public_url,
        "agent_bio": state.bio,
        "agent_public_key_hex": state.public_key_hex,
        "agent_display_name": state.display_name,
        "wallets": state.wallets,
        "wallet_address": state.wallets.get("solana"),
        "created_at": state.created_at,
    }

    is_mutual = getattr(pending, "mutual", False)
    del state.pending_requests[request_id]
    save_state()

    return {
        "success": True,
        "accepted": True,
        "agent_id": pending.from_agent_id,
        "mutual": is_mutual,
    }, 200, notify_payload


def build_outbound_request_payload(state: AgentState, public_url: str, mutual: bool = False) -> dict:
    """Build the payload dict for sending a connection request to another agent."""
    payload = {
        "from_agent_id": state.agent_id,
        "from_agent_url": public_url,
        "from_agent_bio": state.bio,
        "from_agent_public_key_hex": state.public_key_hex,
        "from_agent_display_name": state.display_name,
        "wallets": state.wallets,
        "from_agent_wallet_address": state.wallets.get("solana"),
        "created_at": state.created_at,
    }
    if mutual:
        payload["mutual"] = True
    return payload


def build_connection_from_accepted(result_data: dict) -> Connection:
    """Build a Connection from an auto-accepted or accepted response."""
    peer_wallets = result_data.get("wallets") or (
        {"solana": result_data["wallet_address"]} if result_data.get("wallet_address") else {}
    )
    return Connection(
        agent_id=result_data["agent_id"],
        agent_url=result_data.get("agent_url", ""),
        agent_bio=result_data.get("agent_bio", ""),
        agent_public_key_hex=result_data.get("agent_public_key_hex"),
        agent_display_name=result_data.get("agent_display_name"),
        wallets=peer_wallets,
        peer_created_at=result_data.get("created_at"),
    )


# ---------------------------------------------------------------------------
# Commit-Reveal Session Store
# ---------------------------------------------------------------------------
_pending_match_sessions: dict[str, dict] = {}
_MATCH_SESSION_TTL = 10.0  # seconds


def _gc_expired_sessions() -> None:
    """Remove expired commit-reveal sessions."""
    now = time.monotonic()
    expired = [k for k, v in _pending_match_sessions.items() if now - v["created"] > _MATCH_SESSION_TTL]
    for k in expired:
        del _pending_match_sessions[k]


def process_antimatter_match(data: dict) -> tuple[dict, int]:
    """Commit-reveal match game endpoint with backward compatibility.

    Phase 1 (commit): peer generates pick/nonce/commitment, stores session, returns commitment.
    Phase 2 (reveal): peer verifies orchestrator commitment, returns its pick+nonce.
    Legacy (no phase): stateless random pick for mixed-version networks.
    """
    _gc_expired_sessions()

    phase = data.get("phase")

    # --- Legacy path (backward compatibility) ---
    if phase is None:
        n = data.get("n")
        if not isinstance(n, int) or n < 1:
            return {"error": "Invalid n"}, 400
        pick = random.randint(0, n)
        return {"pick": pick}, 200

    # --- Phase 1: Commit ---
    if phase == "commit":
        n = data.get("n")
        if not isinstance(n, int) or n < 1:
            return {"error": "Invalid n"}, 400
        orchestrator_commitment = data.get("orchestrator_commitment")
        if not orchestrator_commitment:
            return {"error": "Missing orchestrator_commitment"}, 400

        pick, nonce, commitment = make_commitment(n)
        session_token = secrets.token_hex(16)
        _pending_match_sessions[session_token] = {
            "pick": pick,
            "nonce": nonce,
            "commitment": commitment,
            "orchestrator_commitment": orchestrator_commitment,
            "n": n,
            "created": time.monotonic(),
        }
        return {"peer_commitment": commitment, "session_token": session_token}, 200

    # --- Phase 2: Reveal ---
    if phase == "reveal":
        session_token = data.get("session_token")
        if not session_token or session_token not in _pending_match_sessions:
            return {"error": "Unknown or expired session"}, 400

        session = _pending_match_sessions.pop(session_token)

        # Verify orchestrator's commitment
        orch_pick = data.get("orchestrator_pick")
        orch_nonce = data.get("orchestrator_nonce")
        if orch_pick is None or not orch_nonce:
            return {"error": "Missing orchestrator reveal data"}, 400

        if not verify_commitment(session["orchestrator_commitment"], orch_pick, orch_nonce):
            return {"error": "Orchestrator commitment verification failed"}, 400

        return {
            "peer_pick": session["pick"],
            "peer_nonce": session["nonce"].hex(),
        }, 200

    return {"error": f"Unknown phase: {phase}"}, 400


async def process_antimatter_signal(state: AgentState, data: dict) -> tuple[dict, int]:
    """Process a forwarded antimatter signal and start the match game. Returns (response_dict, status_code)."""
    try:
        sig = antimatter_signal_from_dict(data)
    except (KeyError, TypeError) as e:
        return {"error": f"Invalid antimatter signal: {e}"}, 400

    if sig.hops >= sig.max_hops:
        return {"error": "Signal expired (max hops)"}, 400

    if sig.created_at:
        try:
            created = datetime.fromisoformat(sig.created_at)
            age = (datetime.now(timezone.utc) - created).total_seconds()
            if age > ANTIMATTER_MAX_AGE_S:
                return {"error": "Signal expired (age)"}, 400
        except (ValueError, TypeError):
            pass

    if state.agent_id in sig.path:
        return {"error": "Loop detected"}, 400

    log_antimatter_event(state, {
        "type": "gas_signal_received",
        "signal_id": sig.signal_id,
        "hops": sig.hops,
        "from": sig.path[-1] if sig.path else sig.sender_agent_id,
    })

    # Start match game as background task
    asyncio.create_task(run_antimatter_match(state, sig, is_originator=False))

    return {"accepted": True}, 200


async def process_antimatter_result(state: AgentState, data: dict) -> tuple[dict, int]:
    """Process an antimatter resolution callback. B sends fee to destination. Returns (response_dict, status_code)."""
    signal_id = data.get("signal_id", "")
    dest_wallet = data.get("destination_wallet", "")
    resolved_by = data.get("resolved_by", "")
    resolution = data.get("resolution", "resolved")

    if not signal_id:
        return {"error": "Missing signal_id"}, 400

    original_entry = None
    for entry in state.antimatter_log:
        if entry.get("signal_id") == signal_id and entry.get("type") == "antimatter_initiated":
            original_entry = entry
            break

    if not original_entry:
        return {"error": "Unknown signal_id"}, 404

    for entry in state.antimatter_log:
        if entry.get("signal_id") == signal_id and entry.get("type") in ("antimatter_sent", "antimatter_kept"):
            return {"status": "already_resolved"}, 200

    amount = original_entry.get("amount", 0)
    token = original_entry.get("token", "SOL")
    token_decimals = original_entry.get("token_decimals", 9)

    if dest_wallet and state.private_key_hex and amount > 0:
        try:
            if token == "SOL":
                result = await send_solana_sol(
                    state.private_key_hex, state.wallets, dest_wallet, amount
                )
            else:
                result = await send_solana_token(
                    state.private_key_hex, state.wallets, dest_wallet,
                    token, amount, token_decimals
                )

            log_antimatter_event(state, {
                "type": "antimatter_sent",
                "signal_id": signal_id,
                "resolution": resolution,
                "destination": dest_wallet,
                "amount": amount,
                "token": token,
                "tx_success": result.get("success", False),
                "tx_signature": result.get("tx_signature"),
                "resolved_by": resolved_by,
            })

            if result.get("success") and resolved_by:
                adjust_trust(state, resolved_by, 0.01)

        except Exception as e:
            log_antimatter_event(state, {
                "type": "antimatter_send_failed",
                "signal_id": signal_id,
                "error": str(e),
            })
    elif resolution == "timeout" and original_entry.get("sender_superagent_wallet"):
        sa_wallet = original_entry["sender_superagent_wallet"]
        if state.private_key_hex and amount > 0:
            try:
                if token == "SOL":
                    await send_solana_sol(state.private_key_hex, state.wallets, sa_wallet, amount)
                else:
                    await send_solana_token(
                        state.private_key_hex, state.wallets, sa_wallet,
                        token, amount, token_decimals
                    )
                log_antimatter_event(state, {
                    "type": "antimatter_sent",
                    "signal_id": signal_id,
                    "resolution": "timeout",
                    "destination": sa_wallet,
                    "amount": amount,
                })
            except Exception:
                pass
    else:
        log_antimatter_event(state, {
            "type": "antimatter_kept",
            "signal_id": signal_id,
            "reason": "no_destination",
            "amount": amount,
        })

    save_state()
    return {"status": "resolved"}, 200


async def _process_incoming_message(state: AgentState, data: dict) -> tuple[dict, int]:
    """Core message processing logic, shared by HTTP and WebRTC receive paths.

    Returns (response_dict, status_code).
    """
    if state.status == AgentStatus.INACTIVE:
        return {"error": "Agent is currently inactive"}, 503

    if len(state.message_queue) >= MESSAGE_QUEUE_MAX:
        return {"error": "Message queue full"}, 429

    message_id = data.get("message_id", "")
    content = data.get("content", "")
    webhook = data.get("webhook", "")
    from_agent_id = data.get("from_agent_id")

    if not message_id or not content or not webhook:
        return {"error": "Missing required fields"}, 400
    if check_message_replay(message_id):
        return {"error": "Duplicate message — already received"}, 409
    if len(content) > MAX_CONTENT_LENGTH:
        return {"error": f"Content exceeds {MAX_CONTENT_LENGTH} bytes"}, 413
    if from_agent_id and len(from_agent_id) > MAX_AGENT_ID_LENGTH:
        return {"error": "from_agent_id too long"}, 400
    url_err = validate_url(webhook)
    if url_err:
        return {"error": f"Invalid webhook: {url_err}"}, 400

    if not from_agent_id:
        return {"error": "Missing from_agent_id."}, 400

    # Rate limit check (only applied to connected agents)
    conn_for_rate = state.connections.get(from_agent_id)
    rate_err = check_rate_limit(state, conn_for_rate)
    if rate_err:
        if conn_for_rate:
            conn_for_rate.messages_declined += 1
        return {"error": rate_err}, 429

    hops_remaining = data.get("hops_remaining", 10)
    if not isinstance(hops_remaining, int) or hops_remaining < 0:
        hops_remaining = 10

    # Cryptographic verification
    msg_timestamp = data.get("timestamp", "")
    from_public_key_hex = data.get("from_public_key_hex")
    signature_hex = data.get("signature_hex")
    verified = False
    is_connected = from_agent_id in state.connections

    if is_connected:
        conn = state.connections[from_agent_id]
        if conn.agent_public_key_hex:
            # We have a stored public key for this peer — signature is REQUIRED
            if from_public_key_hex and conn.agent_public_key_hex != from_public_key_hex:
                return {"error": "Public key mismatch — sender key does not match stored key for this connection."}, 403
            if not signature_hex or not msg_timestamp:
                return {"error": "Signature required — this connection has a known public key."}, 403
            if not verify_message(conn.agent_public_key_hex, signature_hex,
                                  from_agent_id, message_id, msg_timestamp, content):
                return {"error": "Invalid signature — message authenticity could not be verified."}, 403
            verified = True
        elif from_public_key_hex:
            # Peer sent a key but we don't have one stored — pin it and verify
            if signature_hex and msg_timestamp:
                if not verify_message(from_public_key_hex, signature_hex,
                                      from_agent_id, message_id, msg_timestamp, content):
                    return {"error": "Invalid signature — message authenticity could not be verified."}, 403
                conn.agent_public_key_hex = from_public_key_hex
                verified = True
    elif from_public_key_hex and signature_hex and msg_timestamp:
        # Not connected, but accept if the message is cryptographically signed.
        # Connections manage trust/rate-limiting; signatures prove identity.
        if not verify_message(from_public_key_hex, signature_hex,
                              from_agent_id, message_id, msg_timestamp, content):
            return {"error": "Invalid signature — message authenticity could not be verified."}, 403
        verified = True
    else:
        # No connection AND no signature — reject
        return {"error": "Not connected — unsigned messages require a connection."}, 403

    # Replay protection: reject messages with stale timestamps
    if verified and msg_timestamp and not is_timestamp_fresh(msg_timestamp):
        return {"error": "Message timestamp too old — possible replay"}, 403

    msg = QueuedMessage(
        message_id=truncate_field(message_id, 128),
        content=content,
        webhook=webhook,
        hops_remaining=hops_remaining,
        metadata=data.get("metadata", {}),
        from_agent_id=from_agent_id,
        verified=verified,
    )
    state.message_queue.append(msg)
    state.messages_handled += 1

    # Update telemetry for the sending agent
    if msg.from_agent_id and msg.from_agent_id in state.connections:
        conn = state.connections[msg.from_agent_id]
        conn.messages_received += 1
        conn.last_activity = datetime.now(timezone.utc).isoformat()

    save_state()

    # Notify originator that message was received
    webhook_err = validate_webhook_url(msg.webhook, get_state_fn=get_state, get_public_url_fn=lambda port: get_network_manager().get_public_url())
    if not webhook_err:
        try:
            await get_network_manager().webhook_request(
                msg.webhook, msg.from_agent_id,
                method="POST", timeout=10.0,
                json={"type": "received", "agent_id": state.agent_id}
            )
        except Exception:
            pass  # Best-effort notification

    # AntiMatter economy: if this is a payment notification with antimatter_eligible flag, initiate match game
    msg_meta = msg.metadata or {}
    if (msg_meta.get("type") == "solana_payment"
            and msg_meta.get("antimatter_eligible")
            and msg_meta.get("amount")
            and msg_meta.get("tx_signature")):
        asyncio.create_task(initiate_antimatter_from_payment(
            state, msg,
            get_public_url_fn=lambda port: get_network_manager().get_public_url(),
            save_state_fn=save_state,
        ))

    # Route message through the extensible router chain
    from darkmatter.router import execute_routing
    asyncio.create_task(execute_routing(state, msg))

    return {"success": True, "queued": True, "queue_position": len(state.message_queue)}, 200


def _process_webhook_locally(state: AgentState, message_id: str, data: dict) -> tuple[dict, int]:
    """Process a webhook callback payload locally. Returns (response_dict, status_code).

    Shared by handle_webhook_post (direct) and relay poll (NAT).
    """
    sm = state.sent_messages.get(message_id)
    if not sm:
        return {"error": f"No sent message with ID '{message_id}'"}, 404

    update_type = data.get("type", "")
    agent_id = data.get("agent_id", "unknown")
    timestamp = datetime.now(timezone.utc).isoformat()

    if update_type == "received":
        sm.updates.append({
            "type": "received",
            "agent_id": agent_id,
            "timestamp": timestamp,
        })
        save_state()
        return {"success": True, "recorded": "received"}, 200

    elif update_type == "responding":
        sm.updates.append({
            "type": "responding",
            "agent_id": agent_id,
            "timestamp": timestamp,
        })
        save_state()
        return {"success": True, "recorded": "responding"}, 200

    elif update_type == "forwarded":
        sm.updates.append({
            "type": "forwarded",
            "agent_id": agent_id,
            "target_agent_id": data.get("target_agent_id", ""),
            "note": data.get("note"),
            "timestamp": timestamp,
        })
        save_state()
        return {"success": True, "recorded": "forwarded"}, 200

    elif update_type == "response":
        sm.responses.append({
            "agent_id": agent_id,
            "response": data.get("response", ""),
            "metadata": data.get("metadata", {}),
            "timestamp": timestamp,
        })
        sm.status = "responded"
        save_state()
        # Wake any wait_for_response waiters on this message
        for evt in state._response_events.pop(message_id, []):
            evt.set()
        return {"success": True, "recorded": "response"}, 200

    elif update_type == "expired":
        sm.updates.append({
            "type": "expired",
            "agent_id": agent_id,
            "note": data.get("note"),
            "timestamp": timestamp,
        })
        save_state()
        return {"success": True, "recorded": "expired"}, 200

    else:
        return {"error": f"Unknown update type: '{update_type}'"}, 400


# =============================================================================
# HTTP Endpoints — Agent-to-Agent Communication Layer
#
# These are the raw HTTP endpoints that agents call on each other.
# They sit underneath the MCP tools and handle the actual mesh protocol.
# =============================================================================


async def handle_connection_request(request: Request) -> JSONResponse:
    """Handle an incoming connection request from another agent."""
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    public_url = f"{get_network_manager().get_public_url()}/mcp"
    result, status = await process_connection_request(state, data, public_url)
    return JSONResponse(result, status_code=status)


async def handle_connection_accepted(request: Request) -> JSONResponse:
    """Handle notification that our connection request was accepted."""
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    result, status = process_connection_accepted(state, data)
    if status == 200 and WEBRTC_AVAILABLE:
        agent_id = data.get("agent_id", "")
        conn = state.connections.get(agent_id)
        if conn:
            webrtc_t = get_network_manager().get_transport("webrtc")
            if webrtc_t and webrtc_t.available:
                asyncio.create_task(webrtc_t.upgrade(state, conn))
    return JSONResponse(result, status_code=status)


async def handle_accept_pending(request: Request) -> JSONResponse:
    """Accept a pending connection request via HTTP (no MCP needed)."""
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    request_id = data.get("request_id", "")
    if not request_id:
        return JSONResponse({"error": "Missing request_id"}, status_code=400)

    public_url = f"{get_network_manager().get_public_url()}/mcp"
    result, status, notify_payload = process_accept_pending(state, request_id, public_url)

    if status == 200 and notify_payload:
        # Notify the requesting agent
        # Get the from_agent_url from the connection we just created
        conn = state.connections.get(result.get("agent_id", ""))
        if conn:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    base = conn.agent_url.rstrip("/")
                    for suffix in ("/mcp", "/__darkmatter__"):
                        if base.endswith(suffix):
                            base = base[:-len(suffix)]
                            break
                    await client.post(
                        base + "/__darkmatter__/connection_accepted",
                        json=notify_payload,
                    )
            except Exception:
                pass

            # Auto WebRTC upgrade
            if WEBRTC_AVAILABLE:
                webrtc_t = get_network_manager().get_transport("webrtc")
                if webrtc_t and webrtc_t.available:
                    asyncio.create_task(webrtc_t.upgrade(state, conn))

    return JSONResponse(result, status_code=status)


async def handle_message(request: Request) -> JSONResponse:
    """Handle an incoming routed message from another agent (HTTP transport)."""
    state = get_state()

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    result, status_code = await _process_incoming_message(state, data)
    return JSONResponse(result, status_code=status_code)


async def handle_webhook_post(request: Request) -> JSONResponse:
    """Handle incoming webhook updates (forwarding notifications, responses).

    POST /__darkmatter__/webhook/{message_id}

    Body should contain:
    - type: "received" | "responding" | "forwarded" | "response" | "expired"
    - agent_id: the agent posting this update
    - For "received": (no extra fields -- signals message was queued)
    - For "responding": (no extra fields -- signals agent is actively working on a response)
    - For "forwarded": target_agent_id, optional note
    - For "response": response text, optional metadata (multiple agents may respond)
    - For "expired": optional note
    """
    state = get_state()

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    # Global rate limit for webhook POSTs
    rate_err = check_rate_limit(state)
    if rate_err:
        return JSONResponse({"error": rate_err}, status_code=429)

    message_id = request.path_params.get("message_id", "")

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    result, status_code = _process_webhook_locally(state, message_id, data)
    return JSONResponse(result, status_code=status_code)


async def handle_webhook_get(request: Request) -> JSONResponse:
    """Status check for agents holding a message -- is it still active?

    GET /__darkmatter__/webhook/{message_id}

    Returns message status and forwarding updates (for loop detection).
    """
    state = get_state()

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    message_id = request.path_params.get("message_id", "")
    sm = state.sent_messages.get(message_id)
    if not sm:
        return JSONResponse({"error": f"No sent message with ID '{message_id}'"}, status_code=404)

    return JSONResponse({
        "message_id": sm.message_id,
        "status": sm.status,
        "initial_hops": sm.initial_hops,
        "created_at": sm.created_at,
        "updates": sm.updates,
    })


async def handle_status(request: Request) -> JSONResponse:
    """Return this agent's public status (for health checks and discovery)."""
    state = get_state()

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    spawned_agents = get_spawned_agents()
    return JSONResponse({
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "bio": state.bio,
        "status": state.status.value,
        "num_connections": len(state.connections),
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "spawned_agents": len(spawned_agents),
    })


async def handle_network_info(request: Request) -> JSONResponse:
    """Return this agent's network info for peer discovery."""
    state = get_state()

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    peers = [
        {"agent_id": c.agent_id, "agent_url": c.agent_url, "agent_bio": c.agent_bio}
        for c in state.connections.values()
    ]
    return JSONResponse({
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "agent_url": get_network_manager().get_public_url(),
        "bio": state.bio,
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "wallets": state.wallets,
        "peers": peers,
    })


async def handle_impression_get(request: Request) -> JSONResponse:
    """Return this agent's impression of a specific agent (asked by peers)."""
    state = get_state()

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    rate_err = check_rate_limit(state)
    if rate_err:
        return JSONResponse({"error": rate_err}, status_code=429)

    about_agent_id = request.path_params.get("agent_id", "")
    impression = state.impressions.get(about_agent_id)

    if impression is None:
        return JSONResponse({
            "agent_id": about_agent_id,
            "has_impression": False,
        })

    return JSONResponse({
        "agent_id": about_agent_id,
        "has_impression": True,
        "score": impression.score,
        "note": impression.note,
    })


# =============================================================================
# Network Resilience — Peer Update & Lookup HTTP Handlers
# =============================================================================

async def handle_peer_update(request: Request) -> JSONResponse:
    """Accept a URL change notification from a connected peer.

    Verifies the agent_id is a known connection and optionally validates
    the public key matches before updating the stored URL.
    """
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    # Global rate limit (per-connection checked after we know the agent_id)
    rate_err = check_rate_limit(state)
    if rate_err:
        return JSONResponse({"error": rate_err}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    agent_id = body.get("agent_id", "")
    new_url = body.get("new_url", "")
    public_key_hex = body.get("public_key_hex")
    signature = body.get("signature")
    timestamp = body.get("timestamp", "")

    if not agent_id or not new_url:
        return JSONResponse({"error": "Missing agent_id or new_url"}, status_code=400)

    # Validate URL
    url_err = validate_url(new_url)
    if url_err:
        return JSONResponse({"error": url_err}, status_code=400)

    conn = state.connections.get(agent_id)
    if conn is None:
        return JSONResponse({"error": "Unknown agent"}, status_code=404)

    # Replay protection: reject stale timestamps
    if timestamp and not is_timestamp_fresh(timestamp):
        return JSONResponse({"error": "Timestamp expired"}, status_code=403)

    # Verify public key matches if both sides have one
    if public_key_hex and conn.agent_public_key_hex:
        if public_key_hex != conn.agent_public_key_hex:
            return JSONResponse({"error": "Public key mismatch"}, status_code=403)

    # Signature is REQUIRED when we have a stored key for this peer
    verify_key = conn.agent_public_key_hex or public_key_hex
    if conn.agent_public_key_hex:
        if not signature or not timestamp:
            return JSONResponse({"error": "Signature required — known public key on file"}, status_code=403)
        if not verify_peer_update_signature(verify_key, signature, agent_id, new_url, timestamp):
            return JSONResponse({"error": "Invalid signature"}, status_code=403)
    elif verify_key and signature and timestamp:
        if not verify_peer_update_signature(verify_key, signature, agent_id, new_url, timestamp):
            return JSONResponse({"error": "Invalid signature"}, status_code=403)

    old_url = conn.agent_url
    conn.agent_url = new_url

    # Update transport-aware addresses
    addresses = body.get("addresses")
    if addresses and isinstance(addresses, dict):
        conn.addresses = addresses
    elif new_url:
        conn.addresses["http"] = new_url

    # Update bio if included in the peer_update payload
    new_bio = body.get("bio")
    if new_bio is not None and isinstance(new_bio, str):
        conn.agent_bio = new_bio[:MAX_BIO_LENGTH]

    save_state()

    print(f"[DarkMatter] Peer {agent_id[:12]}... updated URL: {old_url} -> {new_url}", file=sys.stderr)
    return JSONResponse({"success": True, "updated": True})


async def handle_peer_lookup(request: Request) -> JSONResponse:
    """Look up the URL of a connected agent by ID.

    Used by other peers to find an agent's current URL when direct
    communication fails.
    """
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    agent_id = request.path_params.get("agent_id", "")
    if not agent_id:
        return JSONResponse({"error": "Missing agent_id"}, status_code=400)

    conn = state.connections.get(agent_id)
    if conn is None:
        return JSONResponse({"error": "Not connected to that agent"}, status_code=404)

    return JSONResponse({
        "agent_id": conn.agent_id,
        "url": conn.agent_url,
        "addresses": conn.addresses,
        "status": "connected",
    })


# =============================================================================
# AntiMatter HTTP Endpoints
# =============================================================================


async def handle_antimatter_match(request: Request) -> JSONResponse:
    """Stateless match game endpoint. Peer picks a random number and returns it."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    result, status = process_antimatter_match(data)
    return JSONResponse(result, status_code=status)


async def handle_antimatter_signal(request: Request) -> JSONResponse:
    """Receive a forwarded antimatter signal and run the match game."""
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    result, status = await process_antimatter_signal(state, data)
    return JSONResponse(result, status_code=status)


async def handle_antimatter_result(request: Request) -> JSONResponse:
    """B receives this when a downstream node resolves the antimatter signal."""
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    result, status = await process_antimatter_result(state, data)
    return JSONResponse(result, status_code=status)


# =============================================================================
# WebRTC Signaling HTTP Handler
# =============================================================================



def _make_rtc_config():
    """Create an RTCConfiguration with STUN servers."""
    return RTCConfiguration(
        iceServers=[RTCIceServer(urls=s["urls"]) for s in WEBRTC_STUN_SERVERS]
    )


async def _wait_for_ice_gathering(pc, timeout: float = WEBRTC_ICE_GATHER_TIMEOUT) -> None:
    """Wait for ICE gathering to complete."""
    if pc.iceGatheringState == "complete":
        return
    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def on_ice_state():
        if pc.iceGatheringState == "complete":
            done.set()

    await asyncio.wait_for(done.wait(), timeout=timeout)


async def handle_webrtc_offer(request: Request) -> JSONResponse:
    """Handle an incoming WebRTC SDP offer from a peer (answering side).

    POST /__darkmatter__/webrtc_offer
    Body: {from_agent_id, sdp_offer}
    Returns: {sdp_answer}
    """
    state = get_state()

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    if not WEBRTC_AVAILABLE:
        return JSONResponse({"error": "WebRTC not available (aiortc not installed)"}, status_code=501)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    from_agent_id = data.get("from_agent_id", "")
    sdp_offer = data.get("sdp_offer", "")

    if not from_agent_id or not sdp_offer:
        return JSONResponse({"error": "Missing from_agent_id or sdp_offer"}, status_code=400)

    if from_agent_id not in state.connections:
        return JSONResponse({"error": "Not connected — WebRTC upgrade requires an existing connection."}, status_code=403)

    conn = state.connections[from_agent_id]

    # Grab the registered WebRTC transport for cleanup calls
    _webrtc_t = get_network_manager().get_transport("webrtc")

    # Clean up any existing WebRTC state for this connection
    if conn.webrtc_pc is not None and _webrtc_t:
        _webrtc_t.cleanup_sync(conn)

    pc = RTCPeerConnection(configuration=_make_rtc_config())
    channel_ready = asyncio.Event()
    received_channel = [None]  # mutable container for closure

    @pc.on("datachannel")
    def on_datachannel(channel):
        received_channel[0] = channel
        conn.webrtc_channel = channel
        conn.webrtc_pc = pc
        conn.transport = "webrtc"

        @channel.on("message")
        async def on_message(message):
            try:
                envelope = json.loads(message)
                path = envelope.get("path", "")
                payload = envelope.get("payload", {})
                if path == "/__darkmatter__/message":
                    result, status_code = await _process_incoming_message(state, payload)
                    if status_code >= 400:
                        print(f"[DarkMatter] WebRTC message rejected ({status_code}): {result.get('error', 'unknown')}", file=sys.stderr)
            except Exception as e:
                print(f"[DarkMatter] WebRTC message processing error: {e}", file=sys.stderr)

        @channel.on("close")
        def on_close():
            print(f"[DarkMatter] WebRTC data channel closed (peer: {from_agent_id})", file=sys.stderr)
            if _webrtc_t:
                _webrtc_t.cleanup_sync(conn)

        channel_ready.set()

    @pc.on("connectionstatechange")
    async def on_connection_state_change():
        if pc.connectionState in ("failed", "closed"):
            print(f"[DarkMatter] WebRTC connection {pc.connectionState} (peer: {from_agent_id})", file=sys.stderr)
            if _webrtc_t:
                _webrtc_t.cleanup_sync(conn)

    # Set remote offer and create answer
    offer = RTCSessionDescription(sdp=sdp_offer, type="offer")
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    # Wait for ICE gathering
    await _wait_for_ice_gathering(pc)

    print(f"[DarkMatter] WebRTC: answered offer from {conn.agent_display_name or from_agent_id}", file=sys.stderr)

    return JSONResponse({
        "success": True,
        "sdp_answer": pc.localDescription.sdp,
    })

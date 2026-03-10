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
    REQUEST_EXPIRY_S,
    ANTIMATTER_MAX_AGE_S,
    TRUST_ANTIMATTER_SUCCESS,
    WEBRTC_ICE_SERVERS,
    WEBRTC_ICE_GATHER_TIMEOUT,
    MIN_CHAIN_TRUST,
    MESH_ROUTE_PER_SOURCE_LIMIT,
    MESH_ROUTE_PER_SOURCE_WINDOW,
)
from darkmatter.models import (
    AgentState,
    AgentStatus,
    Connection,
    AntiMatterSignal,

    PendingConnectionRequest,
    QueuedMessage,
    RouterAction,
    RouterDecision,
)
from darkmatter.identity import (
    validate_url,
    verify_message,
    verify_peer_update_signature,
    is_timestamp_fresh,
    check_rate_limit,
    truncate_field,
)
from darkmatter.security import verify_inbound
from darkmatter.state import (
    get_state,
    get_state_for,
    save_state,
    check_message_replay,
    check_waiting,
)
from darkmatter.context import log_conversation
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
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer

from darkmatter.logging import get_logger
_log = get_logger("mesh")


def resolve_state(request: "Request") -> Optional[AgentState]:
    """Resolve the target agent state from a request.

    If the URL contains a {target_agent_id} path param, look up that specific
    agent. Otherwise fall back to the default agent.
    """
    target = request.path_params.get("target_agent_id")
    if target:
        return get_state_for(target)
    return get_state()


def local_delivery_capabilities(state: AgentState) -> dict:
    """Capabilities this node advertises for message delivery."""
    return {}


# =============================================================================
# Shared Pure Logic — Framework-agnostic functions used by Starlette handlers.
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
        except Exception as e:
            _log.warning("Peer trust query failed for %s...: %s", conn.agent_id[:12], e)
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


# =============================================================================
# Connection Request Notification
#
# Every connection request (auto-accepted OR pending) is queued as a message
# so the active MCP session can see it via wait_for_message or context.
# =============================================================================
def _queue_connection_request(
    state: AgentState,
    from_agent_id: str,
    from_agent_display_name: str | None,
    from_agent_bio: str,
    status: str,  # "auto-accepted" or "pending"
    request_id: str | None = None,
) -> None:
    """Queue a connection request as a message for the active MCP session."""
    display = from_agent_display_name or from_agent_id[:16] + "..."
    msg_id = request_id or f"conn-{uuid.uuid4().hex[:8]}"
    content = (
        f"Connection request ({status}) from {display}. Bio: {from_agent_bio}"
        if from_agent_bio
        else f"Connection request ({status}) from {display}."
    )
    # Guard against queue overflow
    if len(state.message_queue) >= MESSAGE_QUEUE_MAX:
        _log.warning("Queue full — dropping connection request notification from %s", display)
        return

    synthetic_msg = QueuedMessage(
        message_id=msg_id,
        content=content,
        hops_remaining=0,
        metadata={"type": "connection_request", "request_id": request_id or msg_id, "status": status},
        from_agent_id=from_agent_id,
    )
    state.message_queue.append(synthetic_msg)
    for evt in state._inbox_events:
        evt.set()
    state._inbox_events.clear()
    save_state()
    _log.info("Queued %s connection request from %s", status, display)


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
    from_agent_wallets = data.get("wallets", {})
    from_agent_created_at = data.get("created_at")
    mutual = data.get("mutual", False)
    from_agent_capabilities = data.get("capabilities", {}) or {}

    if not from_agent_id or not from_agent_url:
        missing = [f for f, v in [("from_agent_id", from_agent_id), ("from_agent_url", from_agent_url)] if not v]
        return {"error": f"Missing required fields: {', '.join(missing)}"}, 400
    if len(from_agent_id) > MAX_AGENT_ID_LENGTH:
        return {"error": "agent_id too long"}, 400
    if len(from_agent_bio) > MAX_BIO_LENGTH:
        from_agent_bio = from_agent_bio[:MAX_BIO_LENGTH]
    url_err = validate_url(from_agent_url)
    if url_err:
        return {"error": url_err}, 400

    # Already connected — update info (URL, keys, wallets) and return auto_accepted
    if from_agent_id in state.connections:
        existing = state.connections[from_agent_id]
        changed = False
        if from_agent_url and existing.agent_url != from_agent_url:
            _log.info("Updating URL for %s...: %s -> %s", from_agent_id[:12], existing.agent_url, from_agent_url)
            existing.agent_url = from_agent_url
            changed = True
        if from_agent_public_key_hex and not existing.agent_public_key_hex:
            existing.agent_public_key_hex = from_agent_public_key_hex
            existing.agent_display_name = from_agent_display_name
            changed = True
        if from_agent_wallets and not existing.wallets:
            existing.wallets = from_agent_wallets
            changed = True
        if from_agent_capabilities and existing.capabilities != from_agent_capabilities:
            existing.capabilities = from_agent_capabilities
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
            "capabilities": local_delivery_capabilities(state),

            "created_at": state.created_at,
            "message": "Already connected.",
        }, 200

    # Auto-accept local/same-network agents (respects security_settings toggle)
    from darkmatter.network.manager import is_local_url
    auto_accept = state.security_settings.get("auto_accept_local", True)
    if auto_accept and is_local_url(from_agent_url) and len(state.connections) < MAX_CONNECTIONS:
        from darkmatter.security import assess_url_security
        tls_info = assess_url_security(from_agent_url)
        conn = Connection(
            agent_id=from_agent_id,
            agent_url=from_agent_url,
            agent_bio=from_agent_bio,
            agent_public_key_hex=from_agent_public_key_hex,
            agent_display_name=from_agent_display_name,
            wallets=from_agent_wallets,
            peer_created_at=from_agent_created_at,
            tls_secure=tls_info["secure"],
            identity_verified=bool(from_agent_public_key_hex),
            capabilities=from_agent_capabilities,
        )
        state.connections[from_agent_id] = conn
        save_state()
        _log.info("Auto-accepted local agent %s... (%s)", from_agent_display_name or from_agent_id[:12], from_agent_url)

        # Queue so MCP session sees the new connection via wait_for_message / context.
        _queue_connection_request(
            state, from_agent_id, from_agent_display_name, from_agent_bio, "auto-accepted"
        )

        return {
            "auto_accepted": True,
            "agent_id": state.agent_id,
            "agent_url": public_url,
            "agent_bio": state.bio,
            "agent_public_key_hex": state.public_key_hex,
            "agent_display_name": state.display_name,
            "wallets": state.wallets,
            "capabilities": local_delivery_capabilities(state),

            "created_at": state.created_at,
            "message": "Auto-accepted (local network).",
        }, 200

    # Prune expired pending requests
    now = datetime.now(timezone.utc)
    expired_ids = []
    for rid, req in state.pending_requests.items():
        try:
            req_time = datetime.fromisoformat(req.requested_at)
            if (now - req_time).total_seconds() > REQUEST_EXPIRY_S:
                expired_ids.append(rid)
        except (ValueError, TypeError):
            _log.warning("Malformed timestamp in pending request %s: %r, marking expired", rid, req.requested_at)
            expired_ids.append(rid)
    for rid in expired_ids:
        del state.pending_requests[rid]

    # Dedup: if there's already a pending request from the same agent, refresh it
    existing_request_id = None
    for rid, req in state.pending_requests.items():
        if req.from_agent_id == from_agent_id:
            existing_request_id = rid
            break

    if existing_request_id:
        existing = state.pending_requests[existing_request_id]
        peer_trust = await _gather_peer_trust(state, from_agent_id)
        existing.from_agent_url = from_agent_url
        existing.from_agent_bio = from_agent_bio
        existing.from_agent_public_key_hex = from_agent_public_key_hex
        existing.from_agent_display_name = from_agent_display_name
        existing.from_agent_wallets = from_agent_wallets
        existing.from_agent_created_at = from_agent_created_at
        existing.peer_trust = peer_trust
        existing.mutual = mutual
        existing.requested_at = now.isoformat()
        request_id = existing_request_id
        save_state()
    else:
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
        save_state()

    # Queue so MCP session sees the pending request via wait_for_message / context.
    _queue_connection_request(
        state, from_agent_id, from_agent_display_name, from_agent_bio,
        "pending", request_id=request_id
    )

    # Generate challenge for proof-of-possession
    from darkmatter.security import create_challenge
    challenge_id, challenge_hex = create_challenge(from_agent_id)
    pending_req = state.pending_requests.get(request_id)
    if pending_req:
        pending_req.challenge_id = challenge_id
        pending_req.challenge_hex = challenge_hex

    return {
        "auto_accepted": False,
        "request_id": request_id,
        "agent_id": state.agent_id,
        "challenge_id": challenge_id,
        "challenge_hex": challenge_hex,
        "message": "Connection request queued. Awaiting agent decision. Prove identity with challenge.",
    }, 200


def process_connection_accepted(state: AgentState, data: dict) -> tuple[dict, int]:
    """Process notification that our connection request was accepted. Returns (response_dict, status_code)."""
    agent_id = data.get("agent_id", "")
    agent_url = data.get("agent_url", "")
    agent_bio = data.get("agent_bio", "")
    agent_public_key_hex = data.get("agent_public_key_hex")
    agent_display_name = data.get("agent_display_name")
    agent_wallets = data.get("wallets", {})
    agent_capabilities = data.get("capabilities", {}) or {}

    if not agent_id or not agent_url:
        missing = [f for f, v in [("agent_id", agent_id), ("agent_url", agent_url)] if not v]
        return {"error": f"Missing required fields: {', '.join(missing)}"}, 400
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

    from darkmatter.security import assess_url_security
    tls_info = assess_url_security(agent_url)

    conn = Connection(
        agent_id=agent_id,
        agent_url=agent_url,
        agent_bio=agent_bio,
        agent_public_key_hex=agent_public_key_hex,
        agent_display_name=agent_display_name,
        wallets=agent_wallets,
        peer_created_at=data.get("created_at"),
        tls_secure=tls_info["secure"],
        capabilities=agent_capabilities,
    )
    state.connections[agent_id] = conn

    if not tls_info["secure"] and not tls_info["is_local"]:
        _log.warning("Connection to %s... uses insecure HTTP: %s", agent_id[:12], tls_info.get('warning', ''))

    save_state()

    return {"success": True}, 200


async def notify_connection_accepted(conn: Connection, payload: dict) -> None:
    """Notify a peer that we accepted their connection request.

    Uses NetworkManager.send() which tries all transports in priority order
    (WebRTC, HTTP).
    """
    result = await get_network_manager().send(
        conn.agent_id, "/__darkmatter__/connection_accepted", payload
    )
    if result.success:
        return

    _log.error("Failed to notify %s... of acceptance: %s", conn.agent_id[:12], result.error)


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

    # Check if identity was verified via challenge-response
    identity_verified = False
    if pending.challenge_id and pending.from_agent_public_key_hex:
        # Proof was submitted and verified in handle_connection_proof
        identity_verified = True
    elif not pending.challenge_id:
        # No challenge was issued
        identity_verified = False

    from darkmatter.security import assess_url_security
    tls_info = assess_url_security(pending.from_agent_url)

    conn = Connection(
        agent_id=pending.from_agent_id,
        agent_url=pending.from_agent_url,
        agent_bio=pending.from_agent_bio,
        agent_public_key_hex=pending.from_agent_public_key_hex,
        agent_display_name=pending.from_agent_display_name,
        wallets=pending.from_agent_wallets,
        peer_created_at=pending.from_agent_created_at,
        tls_secure=tls_info["secure"],
        identity_verified=identity_verified,
    )
    state.connections[pending.from_agent_id] = conn

    if not tls_info["secure"] and not tls_info["is_local"]:
        _log.warning("Connection to %s... uses insecure HTTP", pending.from_agent_id[:12])

    notify_payload = {
        "agent_id": state.agent_id,
        "agent_url": public_url,
        "agent_bio": state.bio,
        "agent_public_key_hex": state.public_key_hex,
        "agent_display_name": state.display_name,
        "wallets": state.wallets,
        "capabilities": local_delivery_capabilities(state),
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
        "capabilities": local_delivery_capabilities(state),
        "created_at": state.created_at,
    }
    if mutual:
        payload["mutual"] = True
    return payload


def build_connection_from_accepted(result_data: dict) -> Connection:
    """Build a Connection from an auto-accepted or accepted response."""
    return Connection(
        agent_id=result_data["agent_id"],
        agent_url=result_data.get("agent_url", ""),
        agent_bio=result_data.get("agent_bio", ""),
        agent_public_key_hex=result_data.get("agent_public_key_hex"),
        agent_display_name=result_data.get("agent_display_name"),
        wallets=result_data.get("wallets", {}),
        peer_created_at=result_data.get("created_at"),
        capabilities=result_data.get("capabilities", {}) or {},
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
    """Commit-reveal match game endpoint.

    Phase 1 (commit): peer generates pick/nonce/commitment, stores session, returns commitment.
    Phase 2 (reveal): peer verifies orchestrator commitment, returns its pick+nonce.
    """
    _gc_expired_sessions()

    phase = data.get("phase")
    if phase not in ("commit", "reveal"):
        return {"error": "Missing or invalid phase (expected 'commit' or 'reveal')"}, 400

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
            _log.warning("Malformed antimatter signal timestamp: %r, skipping age check", sig.created_at)

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
                adjust_trust(state, resolved_by, TRUST_ANTIMATTER_SUCCESS)

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
            except Exception as e:
                _log.error("Antimatter timeout send to %s... failed: %s", sa_wallet[:12], e)
    else:
        log_antimatter_event(state, {
            "type": "antimatter_kept",
            "signal_id": signal_id,
            "reason": "no_destination",
            "amount": amount,
        })

    save_state()
    return {"status": "resolved"}, 200


async def _execute_router_decision(state: AgentState, msg: QueuedMessage, decision: RouterDecision) -> None:
    """Execute a router decision — spawn, forward, respond, or drop."""
    if decision.action == RouterAction.HANDLE:
        pass  # Message stays in queue; main agent picks it up via wait_for_message

    elif decision.action == RouterAction.FORWARD:
        from darkmatter.network import send_to_peer
        for target_id in (decision.forward_to or []):
            conn = state.connections.get(target_id)
            if conn:
                try:
                    fwd_metadata = dict(msg.metadata or {})
                    fwd_metadata["forwarded"] = True
                    fwd_metadata["forwarded_by"] = state.agent_id
                    await send_to_peer(conn, "/__darkmatter__/message", {
                        "from_agent_id": msg.from_agent_id,
                        "content": msg.content,
                        "metadata": fwd_metadata,
                        "hops_remaining": (msg.hops_remaining or 10) - 1,
                    })
                except Exception as e:
                    _log.error("Forward to %s... failed: %s", target_id[:12], e)
        # Remove from queue after forwarding
        state.message_queue = [m for m in state.message_queue if m.message_id != msg.message_id]

    elif decision.action == RouterAction.DROP:
        state.message_queue = [m for m in state.message_queue if m.message_id != msg.message_id]



async def _record_inbound_message(state: AgentState, msg: QueuedMessage) -> str:
    """Commit a verified inbound message into state and route it."""
    state.messages_handled += 1

    # queue_only (entrypoint) skips the queue entirely — messages go straight
    # to conversation_log for the human to see.  Agents use the queue so
    # wait_for_message can drain it.
    if state.router_mode != "queue_only":
        state.message_queue.append(msg)

    _agents_waiting_at_receive = len(state._inbox_events) > 0
    for evt in state._inbox_events:
        evt.set()
    state._inbox_events.clear()

    log_conversation(
        state, msg.message_id, msg.content,
        from_id=msg.from_agent_id, to_ids=[state.agent_id],
        entry_type="direct", direction="inbound",
        metadata=msg.metadata,
    )

    if msg.from_agent_id and msg.from_agent_id in state.connections:
        conn = state.connections[msg.from_agent_id]
        conn.messages_received += 1
        conn.last_activity = datetime.now(timezone.utc).isoformat()

    save_state()

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

    # queue_only — no routing, just queue for the MCP session.
    if state.router_mode == "queue_only":
        return "queued"

    if _agents_waiting_at_receive:
        _log.debug("MCP session waiting — message delivered via inbox event, skipping router")
        routed_to = "agent"
    else:
        routed_to = "queued"
        from darkmatter.router import execute_routing
        try:
            decision = await execute_routing(state, msg, execute_decision_fn=_execute_router_decision)
            if decision.action == RouterAction.HANDLE:
                routed_to = "agent" if state._inbox_events else "queued"
            elif decision.action == RouterAction.FORWARD:
                routed_to = "forwarded"
            elif decision.action == RouterAction.DROP:
                routed_to = "dropped"
        except Exception as e:
            import traceback
            _log.error("Routing FAILED for %s...: %s", msg.message_id[:12], e)
            traceback.print_exc(file=sys.stderr)

    return routed_to


async def _commit_verified_message(state: AgentState, data: dict, verified: bool = True) -> tuple[QueuedMessage, str]:
    """Build and persist a verified inbound direct message."""
    msg = QueuedMessage(
        message_id=truncate_field(data["message_id"], 128),
        content=data["content"],
        hops_remaining=data.get("hops_remaining", 10),
        metadata=data.get("metadata", {}) or {},
        from_agent_id=data.get("from_agent_id"),
        verified=verified,
    )
    routed_to = await _record_inbound_message(state, msg)
    return msg, routed_to


async def _process_incoming_message(state: AgentState, data: dict) -> tuple[dict, int]:
    """Core message processing logic, shared by HTTP and WebRTC receive paths.

    Returns (response_dict, status_code).
    """
    if state.status == AgentStatus.INACTIVE:
        return {"error": "Agent is currently inactive"}, 503

    # queue_only (entrypoint) doesn't use the queue — messages go straight to
    # conversation_log.  Skip the queue-full check for it.
    if state.router_mode != "queue_only" and len(state.message_queue) >= MESSAGE_QUEUE_MAX:
        return {"error": "Message queue full"}, 429

    message_id = data.get("message_id", "")
    content = data.get("content", "")
    from_agent_id = data.get("from_agent_id")

    if not message_id or not content:
        missing = [f for f, v in [("message_id", message_id), ("content", content)] if not v]
        return {"error": f"Missing required fields: {', '.join(missing)}"}, 400
    if check_message_replay(message_id):
        return {"error": "Duplicate message — already received"}, 409
    if len(content) > MAX_CONTENT_LENGTH:
        return {"error": f"Content exceeds {MAX_CONTENT_LENGTH} bytes"}, 413
    if from_agent_id and len(from_agent_id) > MAX_AGENT_ID_LENGTH:
        return {"error": "from_agent_id too long"}, 400

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

    # Cryptographic verification — all messages must be signed
    msg_timestamp = data.get("timestamp", "")
    result = verify_inbound(data, state.connections)
    if not result.verified:
        return {"error": result.error}, result.status_code
    verified = True

    # Replay protection: reject messages with stale timestamps
    if verified and msg_timestamp and not is_timestamp_fresh(msg_timestamp):
        return {"error": "Message timestamp too old — possible replay"}, 403

    msg_metadata = data.get("metadata", {})

    # Preserve in_reply_to in metadata. The send_message tool puts in_reply_to
    # at the top level of the payload; copy it into metadata for consistency.
    top_level_irt = data.get("in_reply_to")
    if top_level_irt and "in_reply_to" not in msg_metadata:
        msg_metadata["in_reply_to"] = top_level_irt

    is_broadcast = msg_metadata.get("type") == "broadcast"

    if is_broadcast:
        # Broadcast: log to conversation memory but do NOT queue or spawn
        log_conversation(
            state, truncate_field(message_id, 128), content,
            from_id=from_agent_id, to_ids=[state.agent_id],
            entry_type="broadcast", direction="inbound",
            metadata=msg_metadata,
        )
        # Update telemetry
        if from_agent_id in state.connections:
            conn = state.connections[from_agent_id]
            conn.messages_received += 1
            conn.last_activity = datetime.now(timezone.utc).isoformat()
        save_state()
        return {"status": "broadcast_received"}, 200

    # Security push: apply settings at the node level, don't queue
    if msg_metadata.get("type") == "security_push":
        ss = state.security_settings
        for key in ("auto_accept_local",):
            if key in msg_metadata:
                ss[key] = bool(msg_metadata[key])
        save_state()
        _log.info("Applied security push from %s", from_agent_id[:12])
        return {"status": "security_push_applied"}, 200

    msg, routed_to = await _commit_verified_message(state, {
        "message_id": message_id,
        "content": content,
        "hops_remaining": hops_remaining,
        "metadata": msg_metadata,
        "from_agent_id": from_agent_id,
    }, verified=verified)

    return {
        "status": "received",
        "message_id": msg.message_id,
        "routed_to": routed_to,
        "queue_position": len(state.message_queue),
    }, 200



# =============================================================================
# Peer Ping — distributed IP change detection
# =============================================================================

async def handle_ping(request: Request) -> JSONResponse:
    """Handle an inbound ping from a peer. Returns the requester's IP.

    POST /__darkmatter__/ping
    Body: {"agent_id": str, "timestamp": str}
    Response: {"your_ip": str, "agent_id": str, "timestamp": str}
    """
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    from_agent_id = data.get("agent_id", "")

    # Extract requester's IP (check X-Forwarded-For for proxied requests)
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        requester_ip = forwarded.split(",")[0].strip()
    else:
        requester_ip = request.client.host if request.client else "unknown"

    # Update last_activity on the connection (doubles as heartbeat)
    if from_agent_id and from_agent_id in state.connections:
        state.connections[from_agent_id].last_activity = datetime.now(timezone.utc).isoformat()

    # Track inbound ping time on the manager
    from darkmatter.network.manager import get_network_manager
    try:
        mgr = get_network_manager()
        mgr._last_inbound_ping = time.time()
    except Exception:
        pass

    return JSONResponse({
        "your_ip": requester_ip,
        "agent_id": state.agent_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# =============================================================================
# HTTP Endpoints — Agent-to-Agent Communication Layer
#
# These are the raw HTTP endpoints that agents call on each other.
# They sit underneath the MCP tools and handle the actual mesh protocol.
# =============================================================================


async def handle_connection_request(request: Request) -> JSONResponse:
    """Handle an incoming connection request from another agent."""
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    # Use LAN URL when the requester is on the local network
    from darkmatter.network.manager import is_local_url
    from_url = data.get("from_agent_url", "")
    mgr = get_network_manager()
    if from_url and is_local_url(from_url):
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
            s.close()
        except Exception:
            lan_ip = "127.0.0.1"
        public_url = f"http://{lan_ip}:{state.port}"
    else:
        public_url = mgr.get_public_url()
    result, status = await process_connection_request(state, data, public_url)
    return JSONResponse(result, status_code=status)


async def handle_connection_accepted(request: Request) -> JSONResponse:
    """Handle notification that our connection request was accepted."""
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    result, status = process_connection_accepted(state, data)
    if status == 200:
        agent_id = data.get("agent_id", "")
        conn = state.connections.get(agent_id)
        if conn:
            webrtc_t = get_network_manager().get_transport("webrtc")
            if webrtc_t and webrtc_t.available:
                asyncio.create_task(webrtc_t.upgrade(state, conn))
    return JSONResponse(result, status_code=status)


async def handle_connection_proof(request: Request) -> JSONResponse:
    """Verify proof-of-possession for a pending connection request.

    POST /__darkmatter__/connection_proof
    Body: {challenge_id, proof_hex, agent_id}
    """
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    challenge_id = data.get("challenge_id", "")
    proof_hex = data.get("proof_hex", "")
    agent_id = data.get("agent_id", "")
    public_key_hex = data.get("public_key_hex", "")

    if not challenge_id or not proof_hex or not agent_id or not public_key_hex:
        missing = [f for f, v in [("challenge_id", challenge_id), ("proof_hex", proof_hex), ("agent_id", agent_id), ("public_key_hex", public_key_hex)] if not v]
        return JSONResponse({"error": f"Missing required fields: {', '.join(missing)}"}, status_code=400)

    # Enforce identity binding: public_key_hex must match agent_id (passport invariant)
    if public_key_hex != agent_id:
        return JSONResponse({"error": "Public key must match agent_id"}, status_code=400)

    from darkmatter.security import verify_proof

    # Find the pending request with this challenge_id
    pending = None
    for req in state.pending_requests.values():
        if req.challenge_id == challenge_id and req.from_agent_id == agent_id:
            pending = req
            break

    if pending is None:
        return JSONResponse({"error": "No matching pending request for this challenge"}, status_code=404)

    if not verify_proof(agent_id, public_key_hex, challenge_id, pending.challenge_hex):
        return JSONResponse({"error": "Invalid proof — identity verification failed"}, status_code=403)

    # Mark the pending request as identity-verified
    pending.from_agent_public_key_hex = public_key_hex
    save_state()

    return JSONResponse({"success": True, "identity_verified": True})


async def handle_accept_pending(request: Request) -> JSONResponse:
    """Accept a pending connection request via HTTP (no MCP needed)."""
    state = resolve_state(request)
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
        conn = state.connections.get(result.get("agent_id", ""))
        if conn:
            await notify_connection_accepted(conn, notify_payload)

            # Auto WebRTC upgrade
            webrtc_t = get_network_manager().get_transport("webrtc")
            if webrtc_t:
                asyncio.create_task(webrtc_t.upgrade(state, conn))

    return JSONResponse(result, status_code=status)



# =============================================================================
# Extracted Processing Functions (shared by HTTP handlers and WebRTC dispatch)
#
# Convention: _process_* takes (state, data) and returns (response_dict, status_code).
# These are framework-agnostic — no Request/Response objects.
# =============================================================================

async def _process_status_broadcast(state: AgentState, data: dict) -> tuple[dict, int]:
    """Process an incoming status broadcast. Returns (response_dict, status_code)."""
    payload = data.get("payload") or data
    from_id = payload.get("from_agent_id")
    content = payload.get("content", "")
    metadata = payload.get("metadata", {})
    message_id = payload.get("message_id", f"status-{uuid.uuid4().hex[:8]}")

    if not from_id or from_id not in state.connections:
        return {"error": "Not connected"}, 403

    log_conversation(
        state, message_id, content,
        from_id=from_id, to_ids=[state.agent_id],
        entry_type="mesh_observation", direction="inbound",
        metadata=metadata,
    )

    return {"status": "received", "logged": True}, 200


async def _process_peer_update(state: AgentState, data: dict) -> tuple[dict, int]:
    """Process an incoming peer URL/bio update. Returns (response_dict, status_code)."""
    agent_id = data.get("agent_id", "")
    new_url = data.get("new_url", "")
    public_key_hex = data.get("public_key_hex")
    signature = data.get("signature")
    timestamp = data.get("timestamp", "")

    if not agent_id or not new_url:
        return {"error": "Missing agent_id or new_url"}, 400

    url_err = validate_url(new_url)
    if url_err:
        return {"error": url_err}, 400

    conn = state.connections.get(agent_id)
    if conn is None:
        return {"error": "Unknown agent"}, 404

    if timestamp and not is_timestamp_fresh(timestamp):
        return {"error": "Timestamp expired"}, 403

    if public_key_hex and conn.agent_public_key_hex:
        if public_key_hex != conn.agent_public_key_hex:
            return {"error": "Public key mismatch"}, 403

    verify_key = conn.agent_public_key_hex or public_key_hex
    if conn.agent_public_key_hex:
        if not signature or not timestamp:
            return {"error": "Signature required — known public key on file"}, 403
        if not verify_peer_update_signature(verify_key, signature, agent_id, new_url, timestamp):
            return {"error": "Invalid signature"}, 403
    elif verify_key and signature and timestamp:
        if not verify_peer_update_signature(verify_key, signature, agent_id, new_url, timestamp):
            return {"error": "Invalid signature"}, 403

    old_url = conn.agent_url
    conn.agent_url = new_url

    addresses = data.get("addresses")
    if addresses and isinstance(addresses, dict):
        conn.addresses = addresses
    elif new_url:
        conn.addresses["http"] = new_url

    new_bio = data.get("bio")
    if new_bio is not None and isinstance(new_bio, str):
        conn.agent_bio = new_bio[:MAX_BIO_LENGTH]
    new_display_name = data.get("display_name")
    if new_display_name is not None and isinstance(new_display_name, str):
        conn.agent_display_name = new_display_name[:100]

    save_state()

    _log.info("Peer %s... updated URL: %s -> %s", agent_id[:12], old_url, new_url)
    return {"success": True, "updated": True}, 200


async def _process_insight_push(state: AgentState, data: dict) -> tuple[dict, int]:
    """Process an incoming insight push. Returns (response_dict, status_code)."""
    author_id = data.get("author_agent_id", "")
    insight_id = data.get("insight_id", "")

    if not author_id or not insight_id:
        missing = [f for f, v in [("author_agent_id", author_id), ("insight_id", insight_id)] if not v]
        return {"error": f"Missing required fields: {', '.join(missing)}"}, 400

    signature_hex = data.get("signature_hex")
    if not signature_hex:
        return {"error": "Missing insight signature"}, 403

    author_conn = state.connections.get(author_id)
    author_pub_key = author_conn.agent_public_key_hex if author_conn else None
    if not author_pub_key:
        return {"error": "Unknown insight author — no public key on file"}, 403

    from darkmatter.security import verify_insight_signature
    tags_str = ",".join(sorted(data.get("tags", [])))
    if not verify_insight_signature(author_pub_key, signature_hex, insight_id, author_id,
                                     data.get("content", ""), tags_str):
        return {"error": "Invalid insight signature"}, 403

    from darkmatter.models import Insight
    from darkmatter.config import SHARED_INSIGHT_MAX

    existing_idx = None
    for i, s in enumerate(state.insights):
        if s.insight_id == insight_id and s.author_agent_id == author_id:
            existing_idx = i
            break

    insight = Insight(
        insight_id=insight_id,
        author_agent_id=author_id,
        content=data.get("content", "")[:MAX_CONTENT_LENGTH],
        tags=data.get("tags", []),
        share_with_top_n=data.get("share_with_top_n", -1),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        summary=data.get("summary"),
        signature_hex=signature_hex,
        file=data.get("file"),
        from_text=data.get("from_text"),
        to_text=data.get("to_text"),
        function_anchor=data.get("function_anchor"),
        original_content=data.get("original_content"),
        original_hash=data.get("original_hash"),
    )

    if existing_idx is not None:
        state.insights[existing_idx] = insight
    else:
        if len(state.insights) >= SHARED_INSIGHT_MAX:
            for i, s in enumerate(state.insights):
                if s.author_agent_id != state.agent_id:
                    state.insights.pop(i)
                    break
        state.insights.append(insight)

    save_state()
    return {"success": True, "insight_id": insight_id}, 200


def _process_get_peers(state: AgentState, data: dict) -> tuple[dict, int]:
    """Process a get_peers request. Returns (response_dict, status_code)."""
    n = 10
    try:
        n = int(data.get("n", 10))
    except (ValueError, TypeError):
        pass
    n = max(1, min(n, 50))

    ranked = sorted(
        state.connections.items(),
        key=lambda item: (
            state.impressions.get(item[0]).score
            if state.impressions.get(item[0]) else 0.0
        ),
        reverse=True,
    )[:n]

    peers = []
    for agent_id, conn in ranked:
        peers.append({
            "agent_id": agent_id,
            "display_name": conn.agent_display_name or "",
            "bio": conn.agent_bio or "",
            "connectivity_level": conn.connectivity_level,
            "connectivity_method": conn.connectivity_method,
        })

    return {
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "peer_count": len(state.connections),
        "peers": peers,
    }, 200


# =============================================================================
# WebRTC Message Dispatcher
# =============================================================================

async def dispatch_webrtc_message(state: AgentState, conn, path: str, payload: dict) -> Optional[dict]:
    """Dispatch an incoming WebRTC data channel message to the appropriate handler.

    Returns a response dict for request/response paths (e.g. get_peers),
    or None for fire-and-forget paths (message, broadcast, insight_push, peer_update).
    """
    # Strip any agent-scoped prefix: /__darkmatter__/{agent_id}/path -> /__darkmatter__/path
    # WebRTC channels are already bound to a specific connection, so agent routing is implicit.
    clean_path = path
    if path.startswith("/__darkmatter__/"):
        suffix = path[len("/__darkmatter__/"):]
        # Check if the first segment looks like an agent_id (64-char hex) rather than a route
        parts = suffix.split("/", 1)
        if len(parts) == 2 and len(parts[0]) == 64:
            clean_path = f"/__darkmatter__/{parts[1]}"

    if clean_path == "/__darkmatter__/message":
        result, status_code = await _process_incoming_message(state, payload)
        if status_code >= 400:
            _log.warning("WebRTC message rejected (%s): %s", status_code, result.get("error", "unknown"))
        return None  # Fire-and-forget

    if clean_path == "/__darkmatter__/status_broadcast":
        result, status_code = await _process_status_broadcast(state, payload)
        if status_code >= 400:
            _log.warning("WebRTC broadcast rejected (%s): %s", status_code, result.get("error", "unknown"))
        return None

    if clean_path == "/__darkmatter__/peer_update":
        result, status_code = await _process_peer_update(state, payload)
        if status_code >= 400:
            _log.warning("WebRTC peer_update rejected (%s): %s", status_code, result.get("error", "unknown"))
        return None

    if clean_path == "/__darkmatter__/insight_push":
        result, status_code = await _process_insight_push(state, payload)
        if status_code >= 400:
            _log.warning("WebRTC insight_push rejected (%s): %s", status_code, result.get("error", "unknown"))
        return None

    if clean_path == "/__darkmatter__/get_peers":
        result, _ = _process_get_peers(state, payload)
        return result  # Request/response — send result back

    if clean_path == "/__darkmatter__/mesh_route":
        route_type = payload.get("route_type", "")
        if route_type == "connection_response":
            await _process_mesh_route_response(state, payload)
        else:
            await _process_mesh_route(state, payload)
        return None

    _log.warning("WebRTC: unhandled path %s", path)
    return None


# =============================================================================
# Mesh-Routed Connection Requests (Trust-Guided Routing)
#
# Instead of flooding, routes follow the highest-trust path through the mesh.
# Each hop checks its own connections for the target, then forwards to its
# single most-trusted unvisited peer. A trust_chain accumulates scores at
# each hop — the product gives a "transitive trust" metric that the target
# can use to prioritize incoming connection requests.
# =============================================================================

# Dedup: track recently seen route_ids to prevent re-processing
_seen_route_ids: dict[str, float] = {}  # {route_id: timestamp}
_ROUTE_ID_TTL = 120.0  # seconds

# Per-source mesh route rate limiter: {source_agent_id: [timestamp, ...]}
_mesh_route_sources: dict[str, list[float]] = {}


def _prune_seen_routes() -> None:
    """Remove expired route IDs and stale source rate-limit entries."""
    now = time.time()
    expired = [rid for rid, ts in _seen_route_ids.items() if now - ts > _ROUTE_ID_TTL]
    for rid in expired:
        del _seen_route_ids[rid]
    # Prune source rate-limit windows
    cutoff = now - MESH_ROUTE_PER_SOURCE_WINDOW
    stale_sources = []
    for src, timestamps in _mesh_route_sources.items():
        _mesh_route_sources[src] = [t for t in timestamps if t > cutoff]
        if not _mesh_route_sources[src]:
            stale_sources.append(src)
    for src in stale_sources:
        del _mesh_route_sources[src]


def _check_mesh_route_source_limit(source_agent_id: str) -> bool:
    """Check if a source has exceeded its mesh route forwarding budget.

    Returns True if the source is over the limit (should be dropped).
    """
    now = time.time()
    cutoff = now - MESH_ROUTE_PER_SOURCE_WINDOW
    timestamps = _mesh_route_sources.get(source_agent_id, [])
    recent = [t for t in timestamps if t > cutoff]
    if len(recent) >= MESH_ROUTE_PER_SOURCE_LIMIT:
        return True
    recent.append(now)
    _mesh_route_sources[source_agent_id] = recent
    return False


def _pick_most_trusted_peer(state: AgentState, visited: set[str]) -> Optional[str]:
    """Pick the most trusted connected peer not in the visited set."""
    candidates = [
        (aid, state.impressions.get(aid))
        for aid in state.connections
        if aid not in visited
    ]
    if not candidates:
        return None
    # Sort by trust score descending, default 0.0 for unscored
    candidates.sort(key=lambda x: x[1].score if x[1] else 0.0, reverse=True)
    return candidates[0][0]


def _compute_chain_trust(trust_chain: list[dict]) -> float:
    """Compute transitive trust as the product of all scores in the chain."""
    result = 1.0
    for hop in trust_chain:
        score = hop.get("trust_to_next", 0.0)
        result *= max(score, 0.0)  # floor at 0 — negative trust kills the chain
    return round(result, 6)


async def _process_mesh_route(state: AgentState, data: dict) -> tuple[dict, int]:
    """Process a mesh-routed connection request. Trust-guided single-path routing.

    Envelope format:
    {
        "route_id": "unique-id",
        "route_type": "connection_request",
        "target_agent_id": "...",
        "source_agent_id": "...",
        "hops_remaining": 10,
        "visited": ["source_id", "hop1_id", ...],
        "trust_chain": [
            {"agent_id": "source_id", "trust_to_next": 0.85},
            {"agent_id": "hop1_id", "trust_to_next": 0.92},
        ],
        "payload": { ... the actual connection request ... },
    }

    Routing logic at each hop:
    1. Am I the target (or hosting it)? → Deliver.
    2. Am I connected to the target? → Forward directly to them.
    3. Neither → Forward to my single most-trusted peer not in visited.
    """
    route_id = data.get("route_id", "")
    route_type = data.get("route_type", "")
    target_agent_id = data.get("target_agent_id", "")
    source_agent_id = data.get("source_agent_id", "")
    hops_remaining = data.get("hops_remaining", 0)
    visited = data.get("visited", [])
    trust_chain = data.get("trust_chain", [])
    payload = data.get("payload", {})

    if not route_id or not target_agent_id or not source_agent_id:
        return {"error": "Missing required mesh_route fields"}, 400

    if route_type != "connection_request":
        return {"error": f"Unknown route_type: {route_type}"}, 400

    # Dedup
    _prune_seen_routes()
    if route_id in _seen_route_ids:
        return {"status": "duplicate", "route_id": route_id}, 200
    _seen_route_ids[route_id] = time.time()

    # Don't route our own requests back
    if source_agent_id == state.agent_id:
        return {"status": "origin", "route_id": route_id}, 200

    # Per-source rate limit — prevent any single source from flooding the mesh
    if _check_mesh_route_source_limit(source_agent_id):
        _log.info("Mesh route: rate-limited source %s... (route %s)",
                   source_agent_id[:12], route_id)
        return {"status": "rate_limited", "route_id": route_id}, 200

    visited_set = set(visited)

    # --- Step 1: Am I the target (or hosting it)? ---
    from darkmatter.state import get_state_for
    target_state = get_state_for(target_agent_id)
    if target_state is not None:
        chain_trust = _compute_chain_trust(trust_chain)

        # Chain trust floor — requests with insufficient transitive trust are dropped
        if chain_trust < MIN_CHAIN_TRUST:
            _log.info("Mesh route: dropping connection_request from %s... — "
                       "chain_trust %.6f < %.6f threshold",
                       source_agent_id[:12], chain_trust, MIN_CHAIN_TRUST)
            return {"status": "insufficient_trust", "route_id": route_id}, 200

        _log.info("Mesh route: delivering connection_request from %s... to local agent %s... "
                   "(chain_trust=%.4f, hops=%d)",
                   source_agent_id[:12], target_agent_id[:12],
                   chain_trust, len(trust_chain))

        mgr = get_network_manager()
        public_url = mgr.get_public_url(target_agent_id)

        # Inject chain trust into the payload so the target can see it
        enriched_payload = {**payload, "chain_trust": chain_trust, "trust_chain": trust_chain}
        result, status = await process_connection_request(target_state, enriched_payload, public_url)

        # Sign the response so the source can verify authenticity
        from darkmatter.security import sign_payload
        sig_fields = [route_id, target_agent_id, source_agent_id, result.get("agent_id", "")]
        response_signature = ""
        if target_state.private_key_hex:
            response_signature = sign_payload(
                target_state.private_key_hex, "mesh_route_response", *sig_fields
            )

        # Route the response back to the source
        response_envelope = {
            "route_id": f"{route_id}-resp",
            "route_type": "connection_response",
            "target_agent_id": source_agent_id,
            "source_agent_id": target_agent_id,
            "hops_remaining": 10,
            "visited": [target_agent_id],
            "trust_chain": trust_chain,
            "payload": result,
            "response_signature_hex": response_signature,
            "original_route_id": route_id,
        }
        asyncio.ensure_future(_forward_trust_guided(target_state, response_envelope))
        return {"status": "delivered", "route_id": route_id}, 200

    # --- Step 2: Am I connected to the target? Forward directly. ---
    target_conn = state.connections.get(target_agent_id)
    if target_conn:
        # Add our trust for the target to the chain
        imp = state.impressions.get(target_agent_id)
        trust_score = imp.score if imp else 0.5
        updated_chain = trust_chain + [{"agent_id": state.agent_id, "trust_to_next": round(trust_score, 3)}]

        updated_visited = list(visited_set | {state.agent_id})
        forwarded = {
            **data,
            "visited": updated_visited,
            "trust_chain": updated_chain,
            "hops_remaining": hops_remaining - 1,
        }

        _log.info("Mesh route: forwarding to connected target %s... (trust=%.2f)",
                   target_agent_id[:12], trust_score)
        from darkmatter.network import send_to_peer
        try:
            await send_to_peer(target_conn, "/__darkmatter__/mesh_route", forwarded)
            return {"status": "forwarded_direct", "route_id": route_id}, 200
        except Exception as e:
            _log.warning("Mesh route: direct forward to %s... failed: %s",
                         target_agent_id[:12], e)

    # --- Step 3: Forward to most-trusted unvisited peer ---
    if hops_remaining <= 0:
        _log.info("Mesh route: TTL expired for route %s targeting %s...",
                   route_id, target_agent_id[:12])
        return {"status": "ttl_expired", "route_id": route_id}, 200

    next_peer_id = _pick_most_trusted_peer(state, visited_set | {state.agent_id})
    if next_peer_id is None:
        _log.info("Mesh route: dead end — no unvisited peers for route %s", route_id)
        return {"status": "dead_end", "route_id": route_id}, 200

    # Add our trust for the next hop to the chain
    imp = state.impressions.get(next_peer_id)
    trust_score = imp.score if imp else 0.5
    updated_chain = trust_chain + [{"agent_id": state.agent_id, "trust_to_next": round(trust_score, 3)}]
    updated_visited = list(visited_set | {state.agent_id})

    forwarded = {
        **data,
        "visited": updated_visited,
        "trust_chain": updated_chain,
        "hops_remaining": hops_remaining - 1,
    }

    next_conn = state.connections[next_peer_id]
    _log.info("Mesh route: trust-guided forward to %s... (trust=%.2f)",
               next_peer_id[:12], trust_score)

    from darkmatter.network import send_to_peer
    try:
        await send_to_peer(next_conn, "/__darkmatter__/mesh_route", forwarded)
        return {"status": "forwarded", "route_id": route_id, "via": next_peer_id[:12]}, 200
    except Exception as e:
        _log.warning("Mesh route: forward to %s... failed: %s", next_peer_id[:12], e)
        return {"status": "forward_failed", "route_id": route_id}, 200


async def _process_mesh_route_response(state: AgentState, data: dict) -> tuple[dict, int]:
    """Process a mesh-routed connection response traveling back to the source.

    Uses trust-guided routing (same as forward path) to find the way back.
    Response is cryptographically signed by the target — verified at the source.
    """
    route_id = data.get("route_id", "")
    target_agent_id = data.get("target_agent_id", "")
    source_agent_id = data.get("source_agent_id", "")
    payload = data.get("payload", {})

    if not route_id or not target_agent_id:
        return {"error": "Missing required fields"}, 400

    _prune_seen_routes()
    if route_id in _seen_route_ids:
        return {"status": "duplicate"}, 200
    _seen_route_ids[route_id] = time.time()

    # Are we the target of this response (the original requester)?
    from darkmatter.state import get_state_for
    target_state = get_state_for(target_agent_id)
    if target_state is not None:
        _log.info("Mesh route: connection_response arrived for local agent %s... from %s...",
                   target_agent_id[:12], source_agent_id[:12])

        # Verify signature — proves it came from the real target
        response_sig = data.get("response_signature_hex")
        original_route_id = data.get("original_route_id", "")
        if not response_sig:
            _log.warning("Mesh route: dropping unsigned connection_response from %s...",
                         source_agent_id[:12])
            return {"error": "Missing response signature"}, 403

        from darkmatter.security import verify_signed_payload
        sig_fields = [original_route_id, source_agent_id, target_agent_id,
                      payload.get("agent_id", "")]
        if not verify_signed_payload(source_agent_id, response_sig,
                                     "mesh_route_response", *sig_fields):
            _log.warning("Mesh route: invalid signature on connection_response from %s... — "
                         "possible spoofing attempt", source_agent_id[:12])
            return {"error": "Invalid response signature"}, 403

        # Establish the connection
        if payload.get("auto_accepted"):
            conn = build_connection_from_accepted(payload)
            target_state.connections[payload["agent_id"]] = conn
            save_state()

            chain_trust = _compute_chain_trust(data.get("trust_chain", []))
            _log.info("Mesh route: connection to %s... established (verified, chain_trust=%.4f)",
                       payload.get("agent_id", "")[:12], chain_trust)

            _queue_connection_request(
                target_state,
                payload.get("agent_id", ""),
                payload.get("agent_display_name"),
                payload.get("agent_bio", ""),
                f"mesh-routed (chain_trust={chain_trust:.3f})",
            )
        return {"status": "delivered"}, 200

    # Not for us — forward toward the original requester (trust-guided)
    if data.get("hops_remaining", 0) <= 0:
        return {"status": "ttl_expired"}, 200

    await _forward_trust_guided(state, data)
    return {"status": "forwarded"}, 200


async def _forward_trust_guided(state: AgentState, envelope: dict) -> None:
    """Forward a mesh route envelope to the most-trusted unvisited peer."""
    from darkmatter.network import send_to_peer

    visited = set(envelope.get("visited", []))
    visited.add(state.agent_id)

    target_agent_id = envelope.get("target_agent_id", "")

    # Check if we're directly connected to the target — shortcut
    target_conn = state.connections.get(target_agent_id)
    if target_conn and target_agent_id not in visited:
        forwarded = {
            **envelope,
            "hops_remaining": envelope.get("hops_remaining", 10) - 1,
            "visited": list(visited),
        }
        try:
            await send_to_peer(target_conn, "/__darkmatter__/mesh_route", forwarded)
            return
        except Exception as e:
            _log.warning("Mesh route: direct forward to %s... failed: %s",
                         target_agent_id[:12], e)

    # Trust-guided: pick most trusted unvisited peer
    next_peer_id = _pick_most_trusted_peer(state, visited)
    if next_peer_id is None:
        _log.info("Mesh route: dead end forwarding response for route %s",
                   envelope.get("route_id", ""))
        return

    forwarded = {
        **envelope,
        "hops_remaining": envelope.get("hops_remaining", 10) - 1,
        "visited": list(visited),
    }

    try:
        await send_to_peer(state.connections[next_peer_id],
                           "/__darkmatter__/mesh_route", forwarded)
    except Exception as e:
        _log.warning("Mesh route: forward to %s... failed: %s", next_peer_id[:12], e)


async def handle_mesh_route(request: Request) -> JSONResponse:
    """Handle a mesh-routed packet (HTTP transport)."""
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    route_type = data.get("route_type", "")
    if route_type == "connection_response":
        result, status_code = await _process_mesh_route_response(state, data)
    else:
        result, status_code = await _process_mesh_route(state, data)
    return JSONResponse(result, status_code=status_code)


# =============================================================================
# HTTP Handlers — Thin wrappers around the processing functions above
# =============================================================================

async def handle_message(request: Request) -> JSONResponse:
    """Handle an incoming routed message from another agent (HTTP transport)."""
    state = resolve_state(request)

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    result, status_code = await _process_incoming_message(state, data)
    return JSONResponse(result, status_code=status_code)


async def handle_status(request: Request) -> JSONResponse:
    """Return this agent's public status (for health checks and discovery)."""
    state = resolve_state(request)

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    return JSONResponse({
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "bio": state.bio,
        "status": state.status.value,
        "num_connections": len(state.connections),
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "is_waiting": getattr(state, "_is_waiting", False) or (state.public_key_hex and check_waiting(state.public_key_hex)),
    })


async def handle_network_info(request: Request) -> JSONResponse:
    """Return this agent's network info for peer discovery."""
    state = resolve_state(request)

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    peers = [
        {"agent_id": c.agent_id, "agent_url": c.agent_url, "agent_bio": c.agent_bio,
         "display_name": getattr(c, "agent_display_name", None) or "",
         "bio": c.agent_bio}
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
        "connections": peers,
    })


async def handle_status_broadcast(request: Request) -> JSONResponse:
    """Receive a passive status broadcast from a peer (HTTP transport)."""
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    result, status_code = await _process_status_broadcast(state, data)
    return JSONResponse(result, status_code=status_code)


async def handle_impression_get(request: Request) -> JSONResponse:
    """Return this agent's impression of a specific agent (asked by peers)."""
    state = resolve_state(request)

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
    """Accept a URL change notification from a connected peer (HTTP transport)."""
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    rate_err = check_rate_limit(state)
    if rate_err:
        return JSONResponse({"error": rate_err}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    result, status_code = await _process_peer_update(state, body)
    return JSONResponse(result, status_code=status_code)


async def handle_peer_lookup(request: Request) -> JSONResponse:
    """Look up the URL of a connected agent by ID.

    Used by other peers to find an agent's current URL when direct
    communication fails.
    """
    state = resolve_state(request)
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


async def handle_get_peers(request: Request) -> JSONResponse:
    """Return this agent's top-N most trusted connected peers with bios (HTTP transport)."""
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    data = {}
    if request.method == "POST":
        try:
            data = await request.json()
        except Exception:
            pass
    else:
        try:
            data = {"n": int(request.query_params.get("n", "10"))}
        except (ValueError, TypeError):
            pass

    result, status_code = _process_get_peers(state, data)
    return JSONResponse(result, status_code=status_code)


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
    state = resolve_state(request)
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
    state = resolve_state(request)
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
    """Create an RTCConfiguration with ICE servers (STUN + TURN)."""
    return RTCConfiguration(
        iceServers=[RTCIceServer(**s) for s in WEBRTC_ICE_SERVERS]
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
    state = resolve_state(request)

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

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

    # Verify SDP signature (mandatory)
    sdp_sig = data.get("sdp_signature_hex")
    sdp_pub = data.get("public_key_hex")
    conn = state.connections[from_agent_id]
    verify_key = conn.agent_public_key_hex or sdp_pub
    if not sdp_sig or not verify_key:
        return JSONResponse({"error": "SDP signature required"}, status_code=403)
    from darkmatter.security import verify_sdp_signature
    if not verify_sdp_signature(verify_key, sdp_sig, from_agent_id, sdp_offer):
        return JSONResponse({"error": "Invalid SDP signature"}, status_code=403)

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
        def on_message(message):
            if _webrtc_t:
                _webrtc_t._handle_incoming(state, conn, message)

        @channel.on("close")
        def on_close():
            _log.info("WebRTC data channel closed (peer: %s)", from_agent_id)
            if _webrtc_t:
                _webrtc_t.cleanup_sync(conn)

        channel_ready.set()

    @pc.on("connectionstatechange")
    async def on_connection_state_change():
        if pc.connectionState in ("failed", "closed"):
            _log.info("WebRTC connection %s (peer: %s)", pc.connectionState, from_agent_id)
            if _webrtc_t:
                _webrtc_t.cleanup_sync(conn)

    # Set remote offer and create answer
    offer = RTCSessionDescription(sdp=sdp_offer, type="offer")
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    # Wait for ICE gathering
    await _wait_for_ice_gathering(pc)

    _log.info("WebRTC: answered offer from %s", conn.agent_display_name or from_agent_id)

    from darkmatter.security import sign_sdp
    answer_sdp = pc.localDescription.sdp
    answer_sig = sign_sdp(state.private_key_hex, state.agent_id, answer_sdp)
    return JSONResponse({
        "success": True,
        "sdp_answer": answer_sdp,
        "sdp": answer_sdp,
        "type": "answer",
        "sdp_signature_hex": answer_sig,
        "public_key_hex": state.public_key_hex,
    })


# =============================================================================
# Insight Push Endpoint
# =============================================================================

async def handle_insight_push(request: Request) -> JSONResponse:
    """Receive an insight from a peer (HTTP transport)."""
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    result, status_code = await _process_insight_push(state, data)
    return JSONResponse(result, status_code=status_code)



# =============================================================================
# SDP Relay (Level 3 — Peer-relayed WebRTC signaling)
# =============================================================================

async def handle_sdp_relay(request: Request) -> JSONResponse:
    """A peer asks us to relay an SDP offer to a target we're connected to.

    POST /__darkmatter__/sdp_relay
    Body: {target_agent_id, offer_data, from_agent_id}
    Returns: {sdp, type} — the SDP answer from the target, or error.
    """
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    target_id = data.get("target_agent_id", "")
    from_id = data.get("from_agent_id", "")
    offer_data = data.get("offer_data")

    if not target_id or not from_id or not offer_data:
        return JSONResponse({"error": "Missing target_agent_id, from_agent_id, or offer_data"}, status_code=400)

    # We must be connected to the target to relay
    if target_id not in state.connections:
        return JSONResponse({"error": "Not connected to target agent"}, status_code=404)

    # Forward the SDP offer to the target via our direct connection
    conn = state.connections[target_id]

    try:
        from darkmatter.network.transports.http import strip_base_url
        base_url = strip_base_url(conn.agent_url)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base_url}/__darkmatter__/sdp_relay_deliver",
                json={
                    "from_agent_id": from_id,
                    "offer_data": offer_data,
                    "relay_agent_id": state.agent_id,
                },
            )
            if resp.status_code == 200:
                return JSONResponse(resp.json())
            return JSONResponse({"error": f"Target returned {resp.status_code}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": f"Relay failed: {e}"}, status_code=502)


async def handle_sdp_relay_deliver(request: Request) -> JSONResponse:
    """The actual SDP offer arrives at the target via a relay peer.

    POST /__darkmatter__/sdp_relay_deliver
    Body: {from_agent_id, offer_data, relay_agent_id}
    Returns: {sdp, type} — the SDP answer.
    """
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    from_id = data.get("from_agent_id", "")
    offer_data = data.get("offer_data")

    if not from_id or not offer_data:
        return JSONResponse({"error": "Missing from_agent_id or offer_data"}, status_code=400)

    # The originator must be connected to us
    if from_id not in state.connections:
        return JSONResponse({"error": "Not connected to originating agent"}, status_code=403)

    # Process the offer via WebRTC transport
    from darkmatter.network.manager import get_network_manager
    mgr = get_network_manager()
    webrtc = mgr.get_transport("webrtc")
    if not webrtc:
        return JSONResponse({"error": "WebRTC not available"}, status_code=501)

    answer = await webrtc.handle_offer(state, offer_data)
    if not answer:
        return JSONResponse({"error": "Failed to generate SDP answer"}, status_code=500)

    # Mark the signaling method on the connection
    conn = state.connections.get(from_id)
    if conn:
        conn._signaling_method = "peer_relay"

    return JSONResponse(answer)


async def handle_admin_connect(request: Request) -> JSONResponse:
    """POST /__darkmatter__/admin_connect — Tell this agent to connect to a URL.

    Only processes requests from connected peers.
    """
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    from_id = data.get("from_agent_id", "")
    if from_id not in state.connections:
        return JSONResponse({"error": "Not a connected peer"}, status_code=403)

    target_url = data.get("url", "").strip().rstrip("/")
    if not target_url:
        return JSONResponse({"error": "Missing url"}, status_code=400)

    # Build and send a connection request to the target
    from darkmatter.network.manager import get_network_manager, is_local_url
    import httpx

    mgr = get_network_manager()
    if is_local_url(target_url):
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            our_url = f"http://{s.getsockname()[0]}:{state.port}"
            s.close()
        except Exception:
            our_url = f"http://127.0.0.1:{state.port}"
    else:
        our_url = mgr.get_public_url()

    payload = build_outbound_request_payload(state, our_url, mutual=True)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{target_url}/__darkmatter__/connection_request", json=payload)
            result = resp.json()
            if result.get("auto_accepted"):
                conn = build_connection_from_accepted(result)
                state.connections[result["agent_id"]] = conn
                save_state()
                return JSONResponse({"success": True, "status": "connected", "agent_id": result["agent_id"]})
            return JSONResponse({"success": True, "status": "pending", "request_id": result.get("request_id")})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# =============================================================================
# Genome — serve code as signed zip
# =============================================================================

async def handle_genome(request: Request) -> Response:
    """GET /__darkmatter__/genome — serve genome zip or metadata.

    With ?info=true: returns JSON metadata (version, author, parent, agent_id).
    Without: returns signed zip bytes with signature headers.
    """
    from starlette.responses import Response as StarletteResponse
    from darkmatter.genome import get_genome_version, build_genome_zip, hash_bytes, sign_genome_zip

    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    import darkmatter as dm
    version = get_genome_version()

    # Info-only mode
    if request.query_params.get("info") == "true":
        return JSONResponse({
            "genome_version": version,
            "genome_author": dm.__genome_author__,
            "genome_parent": dm.__genome_parent__,
            "agent_id": state.agent_id,
        })

    # Full zip download
    zip_bytes = build_genome_zip()
    zip_hash = hash_bytes(zip_bytes)
    signature = sign_genome_zip(zip_bytes, state.private_key_hex, version)

    return StarletteResponse(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "X-Genome-Version": version,
            "X-Genome-Author": dm.__genome_author__ or "",
            "X-Genome-Signature": signature,
            "X-Genome-Hash": zip_hash,
        },
    )


# =============================================================================
# Local API — endpoints for skill/curl access (not peer-to-peer)
# =============================================================================

async def handle_local_inbox(request: Request) -> JSONResponse:
    """GET /__darkmatter__/inbox — list all queued messages."""
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    messages = []
    for msg in state.message_queue:
        messages.append({
            "message_id": msg.message_id,
            "content": msg.content[:500] if msg.content else "",
            "from_agent_id": msg.from_agent_id,
            "hops_remaining": msg.hops_remaining,
            "verified": msg.verified,
            "received_at": msg.received_at,
        })
    return JSONResponse({"count": len(messages), "messages": messages})


async def handle_local_pending(request: Request) -> JSONResponse:
    """GET /__darkmatter__/pending_requests — list pending connection requests."""
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    requests_list = []
    for req in state.pending_requests.values():
        requests_list.append({
            "request_id": req.request_id,
            "from_agent_id": req.from_agent_id,
            "from_agent_display_name": req.from_agent_display_name,
            "from_agent_url": req.from_agent_url,
            "from_agent_bio": req.from_agent_bio,
            "requested_at": req.requested_at,
        })
    return JSONResponse({"count": len(requests_list), "requests": requests_list})


async def handle_local_connections(request: Request) -> JSONResponse:
    """GET /__darkmatter__/connections — list connections with details."""
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    conns = []
    for aid, conn in state.connections.items():
        entry = {
            "agent_id": aid,
            "display_name": conn.agent_display_name or aid[:12],
            "agent_url": conn.agent_url,
            "bio": (conn.agent_bio or "")[:250],
            "connected_at": conn.connected_at,
            "last_activity": conn.last_activity,
            "messages_sent": conn.messages_sent,
            "messages_received": conn.messages_received,
            "connectivity_level": conn.connectivity_level,
            "connectivity_method": conn.connectivity_method,
        }
        imp = state.impressions.get(aid)
        if imp:
            entry["impression"] = {"score": imp.score, "note": imp.note}
        conns.append(entry)
    return JSONResponse({"count": len(conns), "connections": conns})


async def handle_local_set_impression(request: Request) -> JSONResponse:
    """POST /__darkmatter__/set_impression — set trust score for a peer."""
    from darkmatter.models import Impression

    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    agent_id = data.get("agent_id", "")
    score = data.get("score")
    note = data.get("note", "")

    if not agent_id or score is None:
        return JSONResponse({"error": "agent_id and score required"}, status_code=400)

    try:
        score = float(score)
        if score < -1 or score > 1:
            return JSONResponse({"error": "score must be between -1.0 and 1.0"}, status_code=400)
    except (TypeError, ValueError):
        return JSONResponse({"error": "score must be a number"}, status_code=400)

    state.impressions[agent_id] = Impression(
        agent_id=agent_id,
        score=score,
        note=str(note)[:2000],
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    save_state()

    return JSONResponse({"success": True, "agent_id": agent_id, "score": score})


async def handle_local_config(request: Request) -> JSONResponse:
    """POST /__darkmatter__/config — set agent configuration."""
    state = resolve_state(request)
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    changes = {}

    if "status" in data:
        val = data["status"]
        if val in ("active", "inactive"):
            from darkmatter.models import AgentStatus
            state.status = AgentStatus(val)
            changes["status"] = val

    if "rate_limit" in data:
        state.rate_limit_per_connection = int(data["rate_limit"])
        changes["rate_limit"] = state.rate_limit_per_connection

    if "superagent_url" in data:
        state.superagent_url = data["superagent_url"]
        changes["superagent_url"] = state.superagent_url

    if "display_name" in data:
        state.display_name = str(data["display_name"])[:100]
        changes["display_name"] = state.display_name

    if changes:
        save_state()

    return JSONResponse({"success": True, "changes": changes})

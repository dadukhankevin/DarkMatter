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
    ANCHOR_NODES,
    MAX_AGENT_ID_LENGTH,
    MAX_BIO_LENGTH,
    MAX_CONNECTIONS,
    MAX_CONTENT_LENGTH,
    MESSAGE_QUEUE_MAX,
    PROTOCOL_VERSION,
    REQUEST_EXPIRY_S,
    ANTIMATTER_MAX_AGE_S,
    TRUST_ANTIMATTER_SUCCESS,
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
    RouterAction,
    RouterDecision,
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
from darkmatter.security import verify_inbound
from darkmatter.state import (
    get_state,
    save_state,
    check_message_replay,
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
        except Exception as e:
            print(f"[DarkMatter] Peer trust query failed for {conn.agent_id[:12]}...: {e}", file=sys.stderr)
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
# Connection Request → Agent Spawn
#
# IMPORTANT — DO NOT REMOVE OR DISABLE THIS FUNCTION.
# Every connection request (auto-accepted OR pending) MUST spawn a sub-agent.
# The spawned agent introduces itself, sets trust, exchanges capabilities, etc.
# This has been accidentally removed/broken in multiple past sessions.
# If you are refactoring, ensure spawn still happens for EVERY connection request.
# =============================================================================
def _spawn_for_connection_request(
    state: AgentState,
    from_agent_id: str,
    from_agent_display_name: str | None,
    from_agent_bio: str,
    status: str,  # "auto-accepted" or "pending"
    request_id: str | None = None,
) -> None:
    """Spawn an agent to handle a connection request. Always called, never skipped."""
    import darkmatter.config as _cfg
    if not (_cfg.AGENT_SPAWN_ENABLED and state.router_mode == "spawn"):
        print(f"[DarkMatter] Spawn disabled (AGENT_SPAWN_ENABLED={_cfg.AGENT_SPAWN_ENABLED}, "
              f"router_mode={state.router_mode!r}) — skipping spawn for {status} connection request "
              f"from {from_agent_id[:12]}...", file=sys.stderr)
        return

    from darkmatter.spawn import spawn_agent_for_message

    display = from_agent_display_name or from_agent_id[:16] + "..."
    msg_id = request_id or f"conn-{uuid.uuid4().hex[:8]}"
    content = (
        f"Connection request ({status}) from {display}. Bio: {from_agent_bio}"
        if from_agent_bio
        else f"Connection request ({status}) from {display}."
    )
    synthetic_msg = QueuedMessage(
        message_id=msg_id,
        content=content,
        webhook="",
        hops_remaining=0,
        metadata={"type": "connection_request", "request_id": request_id or msg_id, "status": status},
        from_agent_id=from_agent_id,
    )
    asyncio.get_event_loop().create_task(spawn_agent_for_message(state, synthetic_msg))
    print(f"[DarkMatter] Spawned agent for {status} connection request from {display}", file=sys.stderr)


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
    _wallets_raw = data.get("wallets")
    from_agent_wallets = _wallets_raw if _wallets_raw is not None else (
        {"solana": data["from_agent_wallet_address"]} if data.get("from_agent_wallet_address") else {}
    )
    from_agent_created_at = data.get("created_at")
    mutual = data.get("mutual", False)

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
            print(f"[DarkMatter] Updating URL for {from_agent_id[:12]}...: "
                  f"{existing.agent_url} -> {from_agent_url}", file=sys.stderr)
            existing.agent_url = from_agent_url
            changed = True
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
        )
        state.connections[from_agent_id] = conn
        save_state()
        print(f"[DarkMatter] Auto-accepted local agent {from_agent_display_name or from_agent_id[:12]}... "
              f"({from_agent_url})", file=sys.stderr)

        # IMPORTANT: Always spawn an agent for connection requests — even auto-accepted ones.
        # The spawned agent can introduce itself, set trust, exchange capabilities, etc.
        # DO NOT remove this spawn. It has been accidentally removed multiple times.
        _spawn_for_connection_request(
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
            "wallet_address": state.wallets.get("solana"),
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
            print(f"[DarkMatter] Malformed timestamp in pending request {rid}: {req.requested_at!r}, marking expired", file=sys.stderr)
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

    # IMPORTANT: Always spawn an agent for connection requests — pending ones too.
    # DO NOT remove this spawn. It has been accidentally removed multiple times.
    _spawn_for_connection_request(
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
    _wallets_raw = data.get("wallets")
    agent_wallets = _wallets_raw if _wallets_raw is not None else (
        {"solana": data["wallet_address"]} if data.get("wallet_address") else {}
    )

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
    )
    state.connections[agent_id] = conn

    if not tls_info["secure"] and not tls_info["is_local"]:
        print(f"[DarkMatter] WARNING: Connection to {agent_id[:12]}... uses insecure HTTP: {tls_info.get('warning', '')}", file=sys.stderr)

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


async def notify_connection_accepted(conn: Connection, payload: dict) -> None:
    """Notify a peer that we accepted their connection request, with anchor relay fallback.

    Uses NetworkManager.send() for the direct attempt (tries all transports in
    priority order: WebRTC, HTTP, etc.). Falls back to anchor relay only when
    all transports fail (e.g. both sides behind NAT).
    """
    result = await get_network_manager().send(
        conn.agent_id, "/__darkmatter__/connection_accepted", payload
    )
    if result.success:
        return

    # All transports failed — fall back to anchor relay (signaling escape hatch)
    if ANCHOR_NODES:
        for anchor in ANCHOR_NODES:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        f"{anchor}/__darkmatter__/connection_relay/{conn.agent_id}",
                        json=payload,
                    )
                    if resp.status_code < 400:
                        print(f"[DarkMatter] Connection accept relayed via {anchor} for {conn.agent_id[:12]}...", file=sys.stderr)
                        return
            except Exception as e:
                print(f"[DarkMatter] Anchor relay {anchor} failed for {conn.agent_id[:12]}...: {e}", file=sys.stderr)
                continue

    print(f"[DarkMatter] Failed to notify {conn.agent_id[:12]}... (transports + all anchors failed)", file=sys.stderr)


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
        # Legacy: no challenge was issued (shouldn't happen with new code)
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
        print(f"[DarkMatter] WARNING: Connection to {pending.from_agent_id[:12]}... uses insecure HTTP", file=sys.stderr)

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
    _wallets_raw = result_data.get("wallets")
    peer_wallets = _wallets_raw if _wallets_raw is not None else (
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
            print(f"[DarkMatter] Malformed antimatter signal timestamp: {sig.created_at!r}, skipping age check", file=sys.stderr)

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
                print(f"[DarkMatter] Antimatter timeout send to {sa_wallet[:12]}... failed: {e}", file=sys.stderr)
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
        import darkmatter.config as _cfg
        if _cfg.AGENT_SPAWN_ENABLED:
            from darkmatter.spawn import spawn_agent_for_message
            await spawn_agent_for_message(state, msg)
        # If spawn disabled, message stays in queue for manual handling

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
                    print(f"[DarkMatter] Forward to {target_id[:12]}... failed: {e}", file=sys.stderr)
        # Remove from queue after forwarding
        state.message_queue = [m for m in state.message_queue if m.message_id != msg.message_id]

    elif decision.action == RouterAction.RESPOND:
        if msg.webhook and decision.response:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(msg.webhook, json={
                        "type": "response",
                        "from_agent_id": state.agent_id,
                        "content": decision.response,
                    })
            except Exception as e:
                print(f"[DarkMatter] Auto-respond webhook failed: {e}", file=sys.stderr)
        state.message_queue = [m for m in state.message_queue if m.message_id != msg.message_id]

    elif decision.action == RouterAction.DROP:
        state.message_queue = [m for m in state.message_queue if m.message_id != msg.message_id]


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
        missing = [f for f, v in [("message_id", message_id), ("content", content), ("webhook", webhook)] if not v]
        return {"error": f"Missing required fields: {', '.join(missing)}"}, 400
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

    # Security push: apply settings at the node level, don't queue or spawn
    if msg_metadata.get("type") == "security_push":
        import darkmatter.config as _cfg
        ss = state.security_settings
        for key in ("auto_accept_local", "sandbox_enabled", "sandbox_network"):
            if key in msg_metadata:
                ss[key] = bool(msg_metadata[key])
        _cfg.AGENT_SANDBOX = ss.get("sandbox_enabled", False)
        _cfg.AGENT_SANDBOX_NETWORK = ss.get("sandbox_network", True)
        save_state()
        print(f"[DarkMatter] Applied security push from {from_agent_id[:12]}", file=sys.stderr)
        return {"status": "security_push_applied"}, 200

    msg = QueuedMessage(
        message_id=truncate_field(message_id, 128),
        content=content,
        webhook=webhook,
        hops_remaining=hops_remaining,
        metadata=msg_metadata,
        from_agent_id=from_agent_id,
        verified=verified,
    )
    state.message_queue.append(msg)
    state.messages_handled += 1

    # Wake any agents waiting for new inbox messages
    for evt in state._inbox_events:
        evt.set()
    state._inbox_events.clear()

    # Log conversation
    log_conversation(
        state, msg.message_id, content,
        from_id=from_agent_id, to_ids=[state.agent_id],
        entry_type="direct", direction="inbound",
        metadata=msg_metadata,
    )

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
    asyncio.create_task(execute_routing(state, msg, execute_decision_fn=_execute_router_decision))

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


async def handle_connection_proof(request: Request) -> JSONResponse:
    """Verify proof-of-possession for a pending connection request.

    POST /__darkmatter__/connection_proof
    Body: {challenge_id, proof_hex, agent_id}
    """
    state = get_state()
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
        conn = state.connections.get(result.get("agent_id", ""))
        if conn:
            await notify_connection_accepted(conn, notify_payload)

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

    # Update bio and display name if included in the peer_update payload
    new_bio = body.get("bio")
    if new_bio is not None and isinstance(new_bio, str):
        conn.agent_bio = new_bio[:MAX_BIO_LENGTH]
    new_display_name = body.get("display_name")
    if new_display_name is not None and isinstance(new_display_name, str):
        conn.agent_display_name = new_display_name[:100]

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
# Shared Shard Push Endpoint
# =============================================================================

async def handle_shard_push(request: Request) -> JSONResponse:
    """Receive a shared shard from a peer.

    POST /__darkmatter__/shard_push
    Body: SharedShard fields
    """
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    author_id = data.get("author_agent_id", "")
    shard_id = data.get("shard_id", "")
    trust_threshold = data.get("trust_threshold", 0.0)

    if not author_id or not shard_id:
        missing = [f for f, v in [("author_agent_id", author_id), ("shard_id", shard_id)] if not v]
        return JSONResponse({"error": f"Missing required fields: {', '.join(missing)}"}, status_code=400)

    # Verify shard signature (mandatory)
    signature_hex = data.get("signature_hex")
    if not signature_hex:
        return JSONResponse({"error": "Missing shard signature"}, status_code=403)

    # Look up author's public key
    author_conn = state.connections.get(author_id)
    author_pub_key = author_conn.agent_public_key_hex if author_conn else None
    if not author_pub_key:
        return JSONResponse({"error": "Unknown shard author — no public key on file"}, status_code=403)

    from darkmatter.security import verify_shard_signature
    tags_str = ",".join(sorted(data.get("tags", [])))
    if not verify_shard_signature(author_pub_key, signature_hex, shard_id, author_id,
                                   data.get("content", ""), tags_str):
        return JSONResponse({"error": "Invalid shard signature"}, status_code=403)

    # Trust gate: verify we trust this peer enough for this shard
    imp = state.impressions.get(author_id)
    our_trust = imp.score if imp else 0.0
    if our_trust < trust_threshold:
        return JSONResponse({"error": "Trust threshold not met"}, status_code=403)

    # Upsert: replace existing shard from same author with same ID
    from darkmatter.models import SharedShard
    from darkmatter.config import SHARED_SHARD_MAX

    existing_idx = None
    for i, s in enumerate(state.shared_shards):
        if s.shard_id == shard_id and s.author_agent_id == author_id:
            existing_idx = i
            break

    shard = SharedShard(
        shard_id=shard_id,
        author_agent_id=author_id,
        content=data.get("content", "")[:MAX_CONTENT_LENGTH],
        tags=data.get("tags", []),
        trust_threshold=trust_threshold,
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        summary=data.get("summary"),
        signature_hex=signature_hex,
    )

    if existing_idx is not None:
        state.shared_shards[existing_idx] = shard
    else:
        if len(state.shared_shards) >= SHARED_SHARD_MAX:
            # Evict oldest peer shard
            for i, s in enumerate(state.shared_shards):
                if s.author_agent_id != state.agent_id:
                    state.shared_shards.pop(i)
                    break
        state.shared_shards.append(shard)

    save_state()
    return JSONResponse({"success": True, "shard_id": shard_id})


# =============================================================================
# SDP Relay (Level 3 — Peer-relayed WebRTC signaling)
# =============================================================================

async def handle_sdp_relay(request: Request) -> JSONResponse:
    """A peer asks us to relay an SDP offer to a target we're connected to.

    POST /__darkmatter__/sdp_relay
    Body: {target_agent_id, offer_data, from_agent_id}
    Returns: {sdp, type} — the SDP answer from the target, or error.
    """
    state = get_state()
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
    state = get_state()
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


# =============================================================================
# Pool Endpoints
# =============================================================================

async def handle_pool_buy(request: Request) -> Response:
    """POST /__darkmatter__/pool_buy — Consumer buys access to a pool.

    Creates an access token for the consumer with the deposited balance.
    """
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    pool_id = data.get("pool_id", "")
    consumer_agent_id = data.get("consumer_agent_id", "")
    amount = data.get("amount", 0)
    payment_token = data.get("payment_token", "SOL")

    if not pool_id or not consumer_agent_id or amount <= 0:
        return JSONResponse({"error": "Missing pool_id, consumer_agent_id, or invalid amount"}, status_code=400)

    # Consumer must be connected
    if consumer_agent_id not in state.connections:
        return JSONResponse({"error": "Consumer not connected"}, status_code=403)

    from darkmatter.pool import find_pool
    from darkmatter.config import POOL_MAX_ACCESS_TOKENS

    pool = find_pool(state.pools, pool_id)
    if not pool:
        return JSONResponse({"success": False, "error": "Pool not found"}, status_code=404)

    if len(pool.access_tokens) >= POOL_MAX_ACCESS_TOKENS:
        return JSONResponse({"success": False, "error": "Access token limit reached"}, status_code=429)

    from darkmatter.models import PoolAccessToken
    now = datetime.now(timezone.utc).isoformat()
    token_id = f"at-{secrets.token_hex(16)}"
    access_token = PoolAccessToken(
        token_id=token_id,
        consumer_agent_id=consumer_agent_id,
        balance=amount,
        token_mint=payment_token,
        total_deposited=amount,
        total_spent=0.0,
        created_at=now,
    )
    pool.access_tokens.append(access_token)
    save_state()

    return JSONResponse({
        "success": True,
        "token_id": token_id,
        "pool_id": pool_id,
        "balance": amount,
        "payment_token": payment_token,
    })


async def handle_pool_proxy(request: Request) -> Response:
    """POST /__darkmatter__/pool_proxy — Proxied API call through a pool.

    Validates access token and balance, selects a provider, proxies the request,
    debits the consumer's balance.
    """
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    pool_id = data.get("pool_id", "")
    token_id = data.get("access_token", "")
    method = data.get("method", "GET")
    path = data.get("path", "")
    headers = data.get("headers", {})
    body = data.get("body")

    if not pool_id or not token_id or not path:
        return JSONResponse({"error": "Missing pool_id, access_token, or path"}, status_code=400)

    from darkmatter.pool import find_pool, find_access_token, select_provider, get_handler

    pool = find_pool(state.pools, pool_id)
    if not pool:
        return JSONResponse({"success": False, "error": "Pool not found"}, status_code=404)

    access_token = find_access_token(pool, token_id)
    if not access_token:
        return JSONResponse({"success": False, "error": "Invalid access token"}, status_code=403)
    if access_token.revoked:
        return JSONResponse({"success": False, "error": "Access token revoked"}, status_code=403)

    provider = select_provider(pool, path)
    if not provider:
        return JSONResponse({"success": False, "error": "No provider available for this path"}, status_code=404)

    # Resolve handler
    handler = get_handler(pool.handler_type)
    if not handler:
        return JSONResponse({"success": False, "error": f"Unknown handler type: {pool.handler_type}"}, status_code=500)

    # Validate request
    validation_error = handler.validate_request(provider, method, path, headers, body)
    if validation_error:
        return JSONResponse({"success": False, "error": validation_error}, status_code=400)

    # Estimate cost for balance pre-check
    estimated_cost = handler.estimate_cost(provider, method, path, body)
    if access_token.balance < estimated_cost:
        return JSONResponse({"success": False, "error": "Insufficient balance"}, status_code=402)

    # Proxy via handler
    try:
        result = await handler.proxy(provider, method, path, headers, body)
        provider.calls_served += 1
    except Exception as e:
        provider.failures += 1
        save_state()
        # Try another provider on failure
        provider2 = select_provider(pool, path)
        if provider2 and provider2.provider_id != provider.provider_id:
            try:
                result = await handler.proxy(provider2, method, path, headers, body)
                provider2.calls_served += 1
            except Exception as e2:
                provider2.failures += 1
                save_state()
                return JSONResponse({"success": False, "error": f"All providers failed: {e2}"}, status_code=502)
        else:
            return JSONResponse({"success": False, "error": f"Provider failed: {e}"}, status_code=502)

    # Debit actual cost from handler result
    access_token.balance -= result.cost
    access_token.total_spent += result.cost
    access_token.calls_made += 1
    access_token.last_used = datetime.now(timezone.utc).isoformat()
    save_state()

    if result.streaming and result.body_stream is not None:
        from starlette.responses import StreamingResponse
        return StreamingResponse(
            result.body_stream,
            status_code=result.status_code,
            headers=result.headers,
        )

    import base64
    return JSONResponse({
        "success": True,
        "status_code": result.status_code,
        "headers": result.headers,
        "body_b64": base64.b64encode(result.body).decode(),
        "balance_remaining": access_token.balance,
    })


async def handle_pool_info(request: Request) -> Response:
    """GET /__darkmatter__/pool_info/{pool_id} — Public pool metadata (no credentials)."""
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    pool_id = request.path_params.get("pool_id", "")

    from darkmatter.pool import find_pool

    pool = find_pool(state.pools, pool_id)
    if not pool:
        return JSONResponse({"error": "Pool not found"}, status_code=404)

    enabled_providers = [pv for pv in pool.providers if pv.enabled]
    return JSONResponse({
        "pool_id": pool.pool_id,
        "name": pool.name,
        "tags": pool.tags,
        "description": pool.description,
        "handler_type": pool.handler_type,
        "provider_count": len(enabled_providers),
        "endpoints": sorted(set(p for pv in enabled_providers for p in pv.allowed_paths)),
        "prices": [
            {"price_per_call": pv.price_per_call, "price_token": pv.price_token}
            for pv in enabled_providers
        ],
        "created_at": pool.created_at,
    })


async def handle_admin_update(request: Request) -> JSONResponse:
    """POST /__darkmatter__/admin_update — Pull latest code from git.

    Only processes requests from connected peers.
    Runs `git pull origin main` on the repo containing the darkmatter package.
    """
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    # Verify the request comes from a connected peer
    from_id = data.get("from_agent_id", "")
    if from_id not in state.connections:
        return JSONResponse({"error": "Not a connected peer"}, status_code=403)

    action = data.get("action", "")
    if action != "pull_and_restart":
        return JSONResponse({"error": f"Unknown action: {action}"}, status_code=400)

    import subprocess
    import sys

    # Use pip upgrade since DarkMatter is pip-installed
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "dmagent"],
            capture_output=True, text=True, timeout=60,
        )
        output = result.stdout.strip() or result.stderr.strip()
        success = result.returncode == 0
    except subprocess.TimeoutExpired:
        output = "pip upgrade timed out after 60s"
        success = False
    except Exception as e:
        output = str(e)
        success = False

    return JSONResponse({
        "success": success,
        "git_output": output,
    })


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

    state = get_state()
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
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    messages = []
    for msg in state.message_queue:
        messages.append({
            "message_id": msg.message_id,
            "content": msg.content[:500] if msg.content else "",
            "from_agent_id": msg.from_agent_id,
            "webhook_url": msg.webhook,
            "hops_remaining": msg.hops_remaining,
            "verified": msg.verified,
            "received_at": msg.received_at,
        })
    return JSONResponse({"count": len(messages), "messages": messages})


async def handle_local_pending(request: Request) -> JSONResponse:
    """GET /__darkmatter__/pending_requests — list pending connection requests."""
    state = get_state()
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
    state = get_state()
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

    state = get_state()
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


async def handle_local_sent_messages(request: Request) -> JSONResponse:
    """GET /__darkmatter__/sent_messages — list sent messages with status."""
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    message_id = request.query_params.get("id")
    if message_id:
        sm = state.sent_messages.get(message_id)
        if not sm:
            return JSONResponse({"error": "Message not found"}, status_code=404)
        return JSONResponse({
            "message_id": sm.message_id,
            "content": sm.content,
            "status": sm.status,
            "created_at": sm.created_at,
            "updates": [{"type": u.get("type"), "from": u.get("from_agent_id"),
                         "content": u.get("content", "")[:500]} for u in sm.updates],
        })

    messages = []
    for sm in state.sent_messages.values():
        messages.append({
            "message_id": sm.message_id,
            "content": (sm.content or "")[:200],
            "status": sm.status,
            "created_at": sm.created_at,
            "updates_count": len(sm.updates),
        })
    return JSONResponse({"count": len(messages), "messages": messages})


async def handle_local_expire_message(request: Request) -> JSONResponse:
    """POST /__darkmatter__/expire_message — expire a sent message."""
    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    message_id = data.get("message_id", "")
    sm = state.sent_messages.get(message_id)
    if not sm:
        return JSONResponse({"error": "Message not found"}, status_code=404)

    sm.status = "expired"
    save_state()
    return JSONResponse({"success": True, "message_id": message_id, "status": "expired"})


async def handle_local_config(request: Request) -> JSONResponse:
    """POST /__darkmatter__/config — set agent configuration."""
    state = get_state()
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

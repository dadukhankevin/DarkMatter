"""
AntiMatter economy — match game, elder selection, antimatter signals, timeout watchdog.

Depends on: config, models, wallet/solana (at runtime)
Uses callbacks for network operations to avoid circular imports.
Network sends go through injected _network_send_fn / _webhook_request_fn
(wired to NetworkManager by app.py at startup).
"""

import asyncio
import hashlib
import random
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

from darkmatter.config import (
    ANTIMATTER_RATE,
    ANTIMATTER_MAX_HOPS,
    ANTIMATTER_MAX_AGE_S,
    ANTIMATTER_LOG_MAX,
    SUPERAGENT_DEFAULT_URL,
    TRUST_ANTIMATTER_SUCCESS,
    TRUST_COMMITMENT_FRAUD,
    TRUST_RATE_DISAGREEMENT,
    TRUST_RATE_TOLERANCE,
)
from darkmatter.models import (
    AgentState,
    Connection,
    AntiMatterSignal,
    Impression,
    QueuedMessage,
)


# =============================================================================
# Network function slots (injected by app.py to avoid circular imports)
# =============================================================================

# async (agent_id, path, payload) -> SendResult-like object (.success, .response)
_network_send_fn = None
# async (url, from_agent_id, method="POST", **kwargs) -> httpx.Response
_webhook_request_fn = None


def set_network_fns(send_fn, webhook_request_fn) -> None:
    """Wire up network functions. Called by app.py after NetworkManager is created."""
    global _network_send_fn, _webhook_request_fn
    _network_send_fn = send_fn
    _webhook_request_fn = webhook_request_fn


# Cache for superagent wallet resolution
_superagent_wallet_cache: dict[str, tuple[str, float]] = {}
_SUPERAGENT_CACHE_TTL = 300.0


# =============================================================================
# Serialization
# =============================================================================

def antimatter_signal_to_dict(sig: AntiMatterSignal) -> dict:
    """Serialize a AntiMatterSignal for network transmission."""
    return {
        "signal_id": sig.signal_id,
        "original_tx": sig.original_tx,
        "sender_agent_id": sig.sender_agent_id,
        "amount": sig.amount,
        "token": sig.token,
        "token_decimals": sig.token_decimals,
        "sender_superagent_wallet": sig.sender_superagent_wallet,
        "callback_url": sig.callback_url,
        "hops": sig.hops,
        "max_hops": sig.max_hops,
        "created_at": sig.created_at,
        "path": sig.path,
    }


def antimatter_signal_from_dict(d: dict) -> AntiMatterSignal:
    """Deserialize a AntiMatterSignal from network payload."""
    return AntiMatterSignal(
        signal_id=d["signal_id"],
        original_tx=d["original_tx"],
        sender_agent_id=d["sender_agent_id"],
        amount=d["amount"],
        token=d["token"],
        token_decimals=d.get("token_decimals", 9),
        sender_superagent_wallet=d.get("sender_superagent_wallet", ""),
        callback_url=d["callback_url"],
        hops=d.get("hops", 0),
        max_hops=d.get("max_hops", ANTIMATTER_MAX_HOPS),
        created_at=d.get("created_at", ""),
        path=d.get("path", []),
    )


# =============================================================================
# Helpers
# =============================================================================

def log_antimatter_event(state: AgentState, event: dict) -> None:
    """Append a antimatter event to state.antimatter_log, capping at ANTIMATTER_LOG_MAX."""
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    state.antimatter_log.append(event)
    if len(state.antimatter_log) > ANTIMATTER_LOG_MAX:
        state.antimatter_log = state.antimatter_log[-ANTIMATTER_LOG_MAX:]


def adjust_trust(state: AgentState, agent_id: str, delta: float) -> None:
    """Adjust trust score for an agent by delta, with non-linear curves.

    Gains: diminishing at high trust — effective = delta * (1.0 - current_score)
    Penalties: amplified at high trust — effective = delta * (1.0 + current_score)
    Tracks negative_since: ISO timestamp when score crosses below 0, cleared on recovery.
    """
    imp = state.impressions.get(agent_id, Impression(score=0.0))
    current = imp.score

    if delta >= 0:
        # Diminishing returns: harder to gain trust when already trusted
        effective = delta * (1.0 - current)
    else:
        # Amplified penalties: trusted agents lose more for bad behavior
        effective = delta * (1.0 + current)

    new_score = max(-1.0, min(1.0, current + effective))
    new_score = round(new_score, 4)

    # Track when score crosses below 0
    negative_since = imp.negative_since
    if new_score < 0 and current >= 0:
        negative_since = datetime.now(timezone.utc).isoformat()
    elif new_score >= 0 and current < 0:
        negative_since = None

    state.impressions[agent_id] = Impression(
        score=new_score, note=imp.note, negative_since=negative_since
    )


async def get_superagent_wallet(state: AgentState) -> Optional[str]:
    """Resolve the superagent URL to a Solana wallet address, with caching."""
    url = state.superagent_url or SUPERAGENT_DEFAULT_URL
    if not url:
        return None

    cached = _superagent_wallet_cache.get(url)
    if cached and time.time() - cached[1] < _SUPERAGENT_CACHE_TTL:
        return cached[0]

    try:
        # Superagent may not be a connected peer — use webhook_request for
        # recovery-capable HTTP, or fall back to raw httpx if not wired yet.
        fetch_url = url.rstrip("/") + "/__darkmatter__/network_info"
        if _webhook_request_fn:
            resp = await _webhook_request_fn(fetch_url, from_agent_id=None, method="GET")
        else:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(fetch_url)
        if resp.status_code == 200:
            data = resp.json()
            wallets = data.get("wallets", {})
            sol_wallet = wallets.get("solana")
            if sol_wallet:
                _superagent_wallet_cache[url] = (sol_wallet, time.time())
                return sol_wallet
    except Exception:
        pass

    return None


# =============================================================================
# Elder Selection
# =============================================================================

def select_elder(state: AgentState, sig: AntiMatterSignal) -> Optional[Connection]:
    """Select an elder (older peer with positive trust) for antimatter routing.

    Weighted random selection by age_seconds * trust_score.
    Excludes agents already in sig.path (loop prevention).
    """
    now = datetime.now(timezone.utc)
    candidates = []

    for aid, conn in state.connections.items():
        if not conn.peer_created_at or not state.created_at:
            continue
        if conn.peer_created_at >= state.created_at:
            continue
        imp = state.impressions.get(aid, Impression(score=0.0))
        if imp.score <= 0:
            continue
        if aid in sig.path:
            continue
        if not conn.wallets.get("solana"):
            continue

        try:
            peer_dt = datetime.fromisoformat(conn.peer_created_at)
            age_s = max(1.0, (now - peer_dt).total_seconds())
        except (ValueError, TypeError):
            age_s = 1.0

        weight = age_s * imp.score
        candidates.append((conn, weight))

    if not candidates:
        return None

    total = sum(w for _, w in candidates)
    r = random.random() * total
    cumulative = 0.0
    for conn, weight in candidates:
        cumulative += weight
        if r <= cumulative:
            return conn
    return candidates[-1][0]


# =============================================================================
# Commit-Reveal Helpers
# =============================================================================

def make_commitment(n: int) -> tuple[int, bytes, str]:
    """Generate pick, nonce, and SHA-256 commitment.
    Returns (pick, nonce_bytes, commitment_hex).
    """
    pick = random.randint(0, n)
    nonce = secrets.token_bytes(32)
    commitment = hashlib.sha256(pick.to_bytes(4, "big") + nonce).hexdigest()
    return pick, nonce, commitment


def verify_commitment(commitment_hex: str, pick: int, nonce_hex: str) -> bool:
    """Verify a commitment matches the revealed pick + nonce."""
    try:
        nonce_bytes = bytes.fromhex(nonce_hex)
        expected = hashlib.sha256(pick.to_bytes(4, "big") + nonce_bytes).hexdigest()
        return expected == commitment_hex
    except (ValueError, OverflowError):
        return False


# =============================================================================
# Match Game
# =============================================================================

async def run_antimatter_match(state: AgentState, sig: AntiMatterSignal,
                         is_originator: bool = True,
                         save_state_fn=None) -> None:
    """Run the commit-reveal match game for antimatter routing.

    Two-phase protocol:
      Phase 1 (commit): send our commitment, collect peer commitments + session tokens
      Phase 2 (reveal): send our pick+nonce, collect peer picks+nonces, verify commitments

    Outcome: combined = orchestrator_pick XOR peer_pick_1 XOR ...
             matched = (combined % (n + 1)) == 0
    """
    if sig.hops >= sig.max_hops:
        await resolve_antimatter(state, sig, "timeout", None, is_originator, save_state_fn=save_state_fn)
        return

    if sig.created_at:
        try:
            created = datetime.fromisoformat(sig.created_at)
            age = (datetime.now(timezone.utc) - created).total_seconds()
            if age > ANTIMATTER_MAX_AGE_S:
                await resolve_antimatter(state, sig, "timeout", None, is_originator, save_state_fn=save_state_fn)
                return
        except (ValueError, TypeError):
            pass

    peers = [
        conn for aid, conn in state.connections.items()
        if aid not in sig.path and conn.wallets.get("solana")
    ]

    n = len(peers)
    if n == 0:
        await resolve_antimatter(state, sig, "terminal", None, is_originator, save_state_fn=save_state_fn)
        return

    # Generate our commitment
    my_pick, my_nonce, my_commitment = make_commitment(n)

    # --- Phase 1: Commit ---
    async def _commit_peer(conn):
        try:
            result = await _network_send_fn(
                conn.agent_id,
                "/__darkmatter__/antimatter_match",
                {
                    "phase": "commit",
                    "signal_id": sig.signal_id,
                    "n": n,
                    "orchestrator_commitment": my_commitment,
                },
            )
            if result.success and result.response:
                d = result.response
                return {
                    "conn": conn,
                    "peer_commitment": d.get("peer_commitment"),
                    "session_token": d.get("session_token"),
                }
        except Exception:
            pass
        return None

    commit_results = await asyncio.gather(*[_commit_peer(c) for c in peers])
    committed_peers = [r for r in commit_results if r and r.get("peer_commitment") and r.get("session_token")]

    # --- Phase 2: Reveal ---
    async def _reveal_peer(peer_info):
        try:
            conn = peer_info["conn"]
            result = await _network_send_fn(
                conn.agent_id,
                "/__darkmatter__/antimatter_match",
                {
                    "phase": "reveal",
                    "session_token": peer_info["session_token"],
                    "orchestrator_pick": my_pick,
                    "orchestrator_nonce": my_nonce.hex(),
                },
            )
            if result.success and result.response:
                d = result.response
                peer_pick = d.get("peer_pick")
                peer_nonce = d.get("peer_nonce")
                if peer_pick is not None and peer_nonce:
                    if verify_commitment(peer_info["peer_commitment"], peer_pick, peer_nonce):
                        return peer_pick
                    else:
                        # Commitment verification failed — fraudulent reveal
                        adjust_trust(state, peer_info["conn"].agent_id, TRUST_COMMITMENT_FRAUD)
        except Exception:
            pass
        return None

    if committed_peers:
        reveal_results = await asyncio.gather(*[_reveal_peer(p) for p in committed_peers])
        valid_picks = [p for p in reveal_results if p is not None]
    else:
        valid_picks = []

    # All peers failed → terminal
    if not valid_picks and not committed_peers:
        await resolve_antimatter(state, sig, "terminal", None, is_originator, save_state_fn=save_state_fn)
        return

    # Compute combined XOR
    combined = my_pick
    for p in valid_picks:
        combined ^= p
    matched = (combined % (n + 1)) == 0

    if matched:
        elder = select_elder(state, sig)
        if elder:
            dest_wallet = elder.wallets.get("solana", "")
            await resolve_antimatter(state, sig, "match", dest_wallet, is_originator,
                              resolved_by=elder.agent_id, save_state_fn=save_state_fn)
        else:
            await resolve_antimatter(state, sig, "terminal", None, is_originator, save_state_fn=save_state_fn)
    else:
        elder = select_elder(state, sig)
        if elder:
            sig.hops += 1
            sig.path.append(state.agent_id)
            forwarded_sig = antimatter_signal_to_dict(sig)

            try:
                result = await _network_send_fn(
                    elder.agent_id,
                    "/__darkmatter__/antimatter_signal",
                    forwarded_sig,
                )
                if result.success:
                    log_antimatter_event(state, {
                        "type": "forwarded",
                        "signal_id": sig.signal_id,
                        "forwarded_to": elder.agent_id,
                        "hops": sig.hops,
                    })
                    return
            except Exception:
                pass

            await resolve_antimatter(state, sig, "terminal", None, is_originator, save_state_fn=save_state_fn)
        else:
            await resolve_antimatter(state, sig, "terminal", None, is_originator, save_state_fn=save_state_fn)


# =============================================================================
# AntiMatter Resolution
# =============================================================================

async def resolve_antimatter(state: AgentState, sig: AntiMatterSignal, resolution: str,
                      dest_wallet: Optional[str], is_originator: bool,
                      resolved_by: str = "", save_state_fn=None) -> None:
    """Resolve a antimatter signal — either send fee (if originator) or notify B's callback."""
    from darkmatter.wallet.solana import send_solana_sol, send_solana_token

    if resolution == "timeout":
        dest_wallet = sig.sender_superagent_wallet or None

    if is_originator:
        if dest_wallet and state.private_key_hex:
            try:
                if sig.token == "SOL":
                    result = await send_solana_sol(
                        state.private_key_hex, state.wallets, dest_wallet, sig.amount
                    )
                else:
                    result = await send_solana_token(
                        state.private_key_hex, state.wallets, dest_wallet,
                        sig.token, sig.amount, sig.token_decimals
                    )

                log_antimatter_event(state, {
                    "type": "antimatter_sent",
                    "signal_id": sig.signal_id,
                    "resolution": resolution,
                    "destination": dest_wallet,
                    "amount": sig.amount,
                    "token": sig.token,
                    "tx_success": result.get("success", False),
                    "tx_signature": result.get("tx_signature"),
                    "resolved_by": resolved_by,
                })

                if result.get("success"):
                    adjust_trust(state, sig.sender_agent_id, TRUST_ANTIMATTER_SUCCESS)
                    if resolved_by:
                        adjust_trust(state, resolved_by, TRUST_ANTIMATTER_SUCCESS)
            except Exception as e:
                log_antimatter_event(state, {
                    "type": "antimatter_send_failed",
                    "signal_id": sig.signal_id,
                    "error": str(e),
                })
        elif resolution == "terminal":
            log_antimatter_event(state, {
                "type": "antimatter_kept",
                "signal_id": sig.signal_id,
                "reason": "terminal_node",
                "amount": sig.amount,
            })

        if save_state_fn:
            save_state_fn()
    else:
        try:
            payload = {
                "signal_id": sig.signal_id,
                "destination_wallet": dest_wallet or "",
                "resolved_by": resolved_by or state.agent_id,
                "resolution": resolution,
            }
            await _webhook_request_fn(
                sig.callback_url,
                from_agent_id=sig.sender_agent_id,
                method="POST",
                json=payload,
            )

            log_antimatter_event(state, {
                "type": "antimatter_resolved_callback",
                "signal_id": sig.signal_id,
                "resolution": resolution,
                "destination": dest_wallet,
            })
        except Exception as e:
            log_antimatter_event(state, {
                "type": "antimatter_callback_failed",
                "signal_id": sig.signal_id,
                "error": str(e),
            })
        if save_state_fn:
            save_state_fn()


# =============================================================================
# AntiMatter Initiation & Timeout
# =============================================================================

async def auto_disconnect_peer(state: AgentState, agent_id: str) -> bool:
    """Auto-disconnect a peer due to sustained negative trust.

    Sends a disconnect announcement before removing the connection.
    Impression persists after disconnect.
    Returns True if disconnected, False if not connected.
    """
    if agent_id not in state.connections:
        return False

    # Send disconnect announcement (best-effort)
    if _network_send_fn:
        try:
            await _network_send_fn(
                agent_id,
                "/__darkmatter__/message",
                {
                    "message_id": f"disconnect-{uuid.uuid4().hex[:12]}",
                    "content": "Auto-disconnecting due to sustained negative trust.",
                    "metadata": {"type": "disconnect_announcement"},
                    "from_agent_id": state.agent_id,
                },
            )
        except Exception:
            pass

    del state.connections[agent_id]
    print(f"[DarkMatter] Auto-disconnected {agent_id[:16]}... (sustained negative trust)", file=sys.stderr)
    return True


async def initiate_antimatter_from_payment(state: AgentState, msg: QueuedMessage,
                                     get_public_url_fn=None,
                                     save_state_fn=None) -> None:
    """B receives a payment from A with antimatter_eligible flag. Calculate fee and start match game."""
    meta = msg.metadata or {}
    amount = meta.get("amount", 0)
    antimatter_rate = meta.get("antimatter_rate", ANTIMATTER_RATE)
    antimatter_amount = amount * antimatter_rate

    # B-side rate disagreement check: penalize if sender's rate differs from ours
    if msg.from_agent_id and abs(antimatter_rate - ANTIMATTER_RATE) > TRUST_RATE_TOLERANCE:
        adjust_trust(state, msg.from_agent_id, TRUST_RATE_DISAGREEMENT)
        log_antimatter_event(state, {
            "type": "rate_disagreement",
            "peer_rate": antimatter_rate,
            "our_rate": ANTIMATTER_RATE,
            "from_agent_id": msg.from_agent_id,
        })

    if antimatter_amount <= 0:
        return

    token = meta.get("token", "SOL")
    token_decimals = meta.get("decimals", 9) if token != "SOL" else 9
    tx_signature = meta.get("tx_signature", "")
    sender_superagent_wallet = meta.get("sender_superagent_wallet", "")

    signal_id = f"am-{uuid.uuid4().hex[:12]}"

    if get_public_url_fn:
        callback_url = f"{get_public_url_fn(state.port)}/__darkmatter__/antimatter_result"
    else:
        callback_url = f"http://localhost:{state.port}/__darkmatter__/antimatter_result"

    sig = AntiMatterSignal(
        signal_id=signal_id,
        original_tx=tx_signature,
        sender_agent_id=msg.from_agent_id or "",
        amount=antimatter_amount,
        token=token,
        token_decimals=token_decimals,
        sender_superagent_wallet=sender_superagent_wallet,
        callback_url=callback_url,
        created_at=datetime.now(timezone.utc).isoformat(),
        path=[],
    )

    log_antimatter_event(state, {
        "type": "antimatter_initiated",
        "signal_id": signal_id,
        "original_tx": tx_signature,
        "amount": antimatter_amount,
        "token": token,
        "token_decimals": token_decimals,
        "sender_agent_id": msg.from_agent_id,
        "sender_superagent_wallet": sender_superagent_wallet,
    })
    if save_state_fn:
        save_state_fn()

    asyncio.create_task(run_antimatter_match(state, sig, is_originator=True, save_state_fn=save_state_fn))
    asyncio.create_task(antimatter_timeout_watchdog(state, sig, save_state_fn=save_state_fn))


async def antimatter_timeout_watchdog(state: AgentState, sig: AntiMatterSignal,
                                save_state_fn=None) -> None:
    """Watchdog: if B doesn't receive a antimatter_result within ANTIMATTER_MAX_AGE_S, penalize."""
    from darkmatter.wallet.solana import send_solana_sol, send_solana_token

    await asyncio.sleep(ANTIMATTER_MAX_AGE_S + 5)

    for entry in state.antimatter_log:
        if entry.get("signal_id") == sig.signal_id and entry.get("type") in ("antimatter_sent", "antimatter_kept"):
            return

    log_antimatter_event(state, {
        "type": "antimatter_timeout",
        "signal_id": sig.signal_id,
    })

    if sig.sender_superagent_wallet and state.private_key_hex:
        try:
            if sig.token == "SOL":
                await send_solana_sol(
                    state.private_key_hex, state.wallets,
                    sig.sender_superagent_wallet, sig.amount
                )
            else:
                await send_solana_token(
                    state.private_key_hex, state.wallets,
                    sig.sender_superagent_wallet, sig.token,
                    sig.amount, sig.token_decimals
                )
        except Exception:
            pass

    if save_state_fn:
        save_state_fn()

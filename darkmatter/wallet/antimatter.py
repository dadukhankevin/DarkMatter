"""
AntiMatter economy — delegated agent antimatter, trust adjustment, reciprocity.

Protocol:
  1. A pays B, tells B: "send 1% to my delegate D"
  2. B sends fee to D's wallet
  3. A monitors D's wallet, adjusts trust for B based on payment
  4. B checks if D is older than A, adjusts trust for A accordingly

Chain-agnostic: uses WalletProvider registry for all wallet operations.
Solana is the default provider but any chain can be plugged in.

Depends on: config, models, wallet (provider registry)
Uses callbacks for network operations to avoid circular imports.
"""

import asyncio
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

from darkmatter.config import (
    ANTIMATTER_RATE,
    ANTIMATTER_TIMEOUT,
    ANTIMATTER_LOG_MAX,
    TRUST_ANTIMATTER_GENEROUS,
    TRUST_ANTIMATTER_HONEST,
    TRUST_ANTIMATTER_CHEAP,
    TRUST_ANTIMATTER_STIFF,
    TRUST_ANTIMATTER_LEGIT_DELEGATE,
    TRUST_ANTIMATTER_GAMING,
    RECIPROCITY_GRACE_THRESHOLD,
    TRUST_SEED_CAP,
)
from darkmatter.models import (
    AgentState,
    Connection,
    Impression,
)
from darkmatter.wallet import resolve_provider, get_provider


# =============================================================================
# Network function slots (injected by app.py to avoid circular imports)
# =============================================================================

_network_send_fn = None
_http_request_fn = None


def set_network_fns(send_fn, http_request_fn) -> None:
    """Wire up network functions. Called by app.py after NetworkManager is created."""
    global _network_send_fn, _http_request_fn
    _network_send_fn = send_fn
    _http_request_fn = http_request_fn


# =============================================================================
# Helpers
# =============================================================================

def log_antimatter_event(state: AgentState, event: dict) -> None:
    """Append an antimatter event to state.antimatter_log, capping at ANTIMATTER_LOG_MAX."""
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
        effective = delta * (1.0 - current)
    else:
        effective = delta * (1.0 + current)

    new_score = max(-1.0, min(1.0, current + effective))
    new_score = round(new_score, 4)

    negative_since = imp.negative_since
    if new_score < 0 and current >= 0:
        negative_since = datetime.now(timezone.utc).isoformat()
    elif new_score >= 0 and current < 0:
        negative_since = None

    state.impressions[agent_id] = Impression(
        score=new_score, note=imp.note, negative_since=negative_since,
        msgs_sent=imp.msgs_sent, msgs_received=imp.msgs_received,
    )


def reciprocity_ratio(imp: Impression) -> float:
    """Compute reciprocity ratio for a peer: 1.0 = balanced, 0.0 = one-sided.

    Grace period: if max(sent, received) < RECIPROCITY_GRACE_THRESHOLD,
    return 1.0 to allow cold-start trust building.
    Infrastructure peers (bootstrap nodes) are exempt — always return 1.0.
    """
    if imp.infrastructure:
        return 1.0
    total = max(imp.msgs_sent, imp.msgs_received)
    if total < RECIPROCITY_GRACE_THRESHOLD:
        return 1.0
    if total == 0:
        return 1.0
    return min(imp.msgs_sent, imp.msgs_received) / total


def compute_seeded_trust(state: AgentState, peer_opinions: list[dict]) -> float:
    """Compute initial trust for a new peer from existing peers' opinions.

    Weighted average: sum(my_trust_in_peer * peer_score_for_new) / sum(my_trust_in_peer)
    Floored at 0.0 (hearsay can't make you start negative).
    Capped at TRUST_SEED_CAP (must earn high trust through direct interaction).
    """
    total_weight = 0.0
    weighted_sum = 0.0
    for opinion in peer_opinions:
        recommender_id = opinion.get("agent_id", "")
        their_score = opinion.get("score", 0.0)
        my_imp = state.impressions.get(recommender_id)
        my_trust_in_them = my_imp.score if my_imp else 0.0
        if my_trust_in_them > 0:
            total_weight += my_trust_in_them
            weighted_sum += my_trust_in_them * their_score
    if total_weight <= 0:
        return 0.0
    seeded = weighted_sum / total_weight
    return max(0.0, min(seeded, TRUST_SEED_CAP))


# =============================================================================
# Auto-Disconnect (sustained negative trust)
# =============================================================================

async def auto_disconnect_peer(state: AgentState, agent_id: str) -> bool:
    """Auto-disconnect a peer due to sustained negative trust.

    Sends a disconnect announcement before removing the connection.
    Impression persists after disconnect.
    Returns True if disconnected, False if not connected.
    """
    if agent_id not in state.connections:
        return False

    if _network_send_fn:
        try:
            imp = state.impressions.get(agent_id)
            score = round(imp.score, 4) if imp else "?"
            neg_since = imp.negative_since if imp else "?"
            await _network_send_fn(
                agent_id,
                "/__darkmatter__/message",
                {
                    "message_id": f"disconnect-{uuid.uuid4().hex[:12]}",
                    "content": (
                        f"You have been auto-disconnected due to sustained negative trust "
                        f"(score: {score}, negative since: {neg_since}). "
                        f"Contact the agent directly to resolve and reconnect."
                    ),
                    "metadata": {"type": "trust_disconnect_notice", "peer_id": state.agent_id},
                    "from_agent_id": state.agent_id,
                },
            )
        except Exception:
            pass

    del state.connections[agent_id]
    print(f"[DarkMatter] Auto-disconnected {agent_id[:16]}... (sustained negative trust)", file=sys.stderr)
    return True


# =============================================================================
# Delegate Selection
# =============================================================================

def select_delegate(state: AgentState, exclude_agent_id: Optional[str] = None) -> Optional[Connection]:
    """Select a delegate for antimatter fees: random weighted by tenure * trust.

    The delegate must be older than this agent (created_at < state.created_at),
    have positive trust, and have at least one wallet with a registered provider.
    Selection is random weighted by (age × trust_score) so older, more trusted
    peers are more likely chosen, but not deterministically — this prevents
    gaming by always targeting the same delegate.
    exclude_agent_id: agent to exclude (e.g. the payment recipient — cannot be their own delegate).
    Returns the Connection, or None if no eligible peers.
    """
    import random
    now = datetime.now(timezone.utc)
    candidates = []

    for aid, conn in state.connections.items():
        if exclude_agent_id and aid == exclude_agent_id:
            continue  # Recipient cannot be their own fee delegate
        if not conn.peer_created_at or not state.created_at:
            continue
        if conn.peer_created_at >= state.created_at:
            continue
        imp = state.impressions.get(aid, Impression(score=0.0))
        if imp.score <= 0:
            continue
        if imp.infrastructure:
            continue  # Bootstrap/infrastructure peers cannot be delegates
        if not resolve_provider(conn.wallets):
            continue

        try:
            peer_dt = datetime.fromisoformat(conn.peer_created_at)
            age_s = max(1.0, (now - peer_dt).total_seconds())
        except (ValueError, TypeError):
            age_s = 1.0

        weight = age_s * imp.score
        candidates.append((conn, weight))

    if not candidates:
        # Fall back to configured delegate (e.g. bootstrap server)
        from darkmatter.config import FALLBACK_DELEGATE_AGENT_ID
        if FALLBACK_DELEGATE_AGENT_ID and FALLBACK_DELEGATE_AGENT_ID != exclude_agent_id:
            fallback = state.connections.get(FALLBACK_DELEGATE_AGENT_ID)
            if fallback and resolve_provider(fallback.wallets):
                return fallback
        return None

    # Random weighted selection — older & more trusted peers are more likely
    conns, weights = zip(*candidates)
    return random.choices(conns, weights=weights, k=1)[0]


# =============================================================================
# A-Side: Initiate Payment with Antimatter
# =============================================================================

async def initiate_payment(state: AgentState, recipient_agent_id: str,
                           amount: float, currency: str = "SOL",
                           token_decimals: int = 9,
                           chain: str = "solana",
                           save_state_fn=None) -> dict:
    """A pays B with automatic antimatter delegation.

    Chain-agnostic: uses the WalletProvider registry to resolve the right chain.
    Falls back to `chain` parameter if recipient has multiple wallets.

    1. Selects delegate D (oldest trusted peer)
    2. Sends payment to B via the chain's provider
    3. Notifies B to send 1% fee to D
    4. Starts background wallet monitor to verify B paid D
    """
    if recipient_agent_id not in state.connections:
        return {"success": False, "error": "Recipient not connected"}

    conn = state.connections[recipient_agent_id]

    # Resolve chain + provider + recipient address
    provider = get_provider(chain)
    recipient_wallet = conn.wallets.get(chain)
    if not provider or not recipient_wallet:
        # Try any available chain
        resolved = resolve_provider(conn.wallets)
        if not resolved:
            return {"success": False, "error": "Recipient has no wallet with a supported chain"}
        chain, provider, recipient_wallet = resolved

    # Select delegate — must have a wallet on the same chain (recipient excluded)
    delegate = select_delegate(state, exclude_agent_id=recipient_agent_id)
    delegate_agent_id = delegate.agent_id if delegate else None
    delegate_wallet = delegate.wallets.get(chain) if delegate else None

    # Determine token (None means native currency)
    token = None if currency.upper() == provider.chain.upper() or currency.upper() == "SOL" else currency

    # Send the actual payment via provider
    tx_result = await provider.send(
        state.private_key_hex, state.wallets, recipient_wallet,
        amount, token=token, decimals=token_decimals,
    )

    if not tx_result.get("success"):
        return tx_result

    # Snapshot delegate's latest tx signature for sender-attribution monitoring
    delegate_before_sig = None
    if delegate_wallet:
        try:
            delegate_before_sig = await provider.get_inbound_transfers(
                delegate_wallet, limit=1,
            )
            delegate_before_sig = delegate_before_sig[0]["signature"] if delegate_before_sig else None
        except Exception:
            pass

    fee_amount = amount * ANTIMATTER_RATE
    antimatter_id = f"am-{uuid.uuid4().hex[:12]}"

    # Notify B about antimatter obligation
    if delegate_wallet and _network_send_fn:
        try:
            await _network_send_fn(
                recipient_agent_id,
                "/__darkmatter__/antimatter_request",
                {
                    "antimatter_id": antimatter_id,
                    "payer_agent_id": state.agent_id,
                    "amount": amount,
                    "fee_amount": fee_amount,
                    "currency": currency,
                    "token_decimals": token_decimals,
                    "chain": chain,
                    "delegate_agent_id": delegate_agent_id,
                    "delegate_wallet": delegate_wallet,
                    "tx_signature": tx_result.get("tx_signature", ""),
                },
            )
        except Exception:
            pass

        # Get B's wallet address on this chain for sender attribution
        recipient_chain_wallet = conn.wallets.get(chain, "")

        # Start background monitoring of delegate wallet
        asyncio.create_task(monitor_delegate_wallet(
            state, provider, delegate_wallet, fee_amount, currency, token_decimals,
            recipient_agent_id, recipient_chain_wallet, antimatter_id,
            after_signature=delegate_before_sig,
            save_state_fn=save_state_fn,
        ))

    log_antimatter_event(state, {
        "type": "payment_initiated",
        "antimatter_id": antimatter_id,
        "recipient": recipient_agent_id,
        "amount": amount,
        "fee_amount": fee_amount,
        "currency": currency,
        "chain": chain,
        "delegate_agent_id": delegate_agent_id,
        "delegate_wallet": delegate_wallet,
        "tx_signature": tx_result.get("tx_signature", ""),
    })
    if save_state_fn:
        save_state_fn()

    return {
        "success": True,
        "tx_signature": tx_result.get("tx_signature"),
        "amount": amount,
        "antimatter_id": antimatter_id,
        "delegate_agent_id": delegate_agent_id,
        "delegate_wallet": delegate_wallet,
        "fee_amount": fee_amount,
        "chain": chain,
    }


# =============================================================================
# B-Side: Handle Antimatter Request
# =============================================================================

async def handle_antimatter_request(state: AgentState, data: dict,
                                    save_state_fn=None) -> dict:
    """B receives antimatter request from A: send fee to A's delegate D.

    B also checks if D is older than A:
      - Yes: B boosts trust for A (legitimate elder delegate)
      - No:  B penalizes trust for A (gaming the system)

    D's age is determined as the YOUNGEST of up to three signals:
      1. D's mesh-claimed age (peer_created_at from B's connection record, if connected)
      2. D's on-chain wallet age (earliest transaction timestamp)
      3. D's identity attestation age (on-chain passport proof, if it exists)
    Taking the youngest prevents gaming via old wallets with fresh nodes or vice versa.
    If an attestation exists, it also verifies the wallet actually belongs to the
    claimed delegate agent_id — a mismatch is treated as gaming.

    A's age comes from B's own connection record (never A's claim).
    Chain-agnostic: resolves provider from the chain field in the request.
    """
    payer_agent_id = data.get("payer_agent_id", "")
    fee_amount = data.get("fee_amount", 0)
    currency = data.get("currency", "SOL")
    token_decimals = data.get("token_decimals", 9)
    chain = data.get("chain", "solana")
    delegate_agent_id = data.get("delegate_agent_id", "")
    delegate_wallet = data.get("delegate_wallet", "")
    antimatter_id = data.get("antimatter_id", "")

    if not delegate_wallet or fee_amount <= 0:
        return {"success": False, "error": "Invalid antimatter request"}

    provider = get_provider(chain)

    # Use B's own record of A's created_at — never trust A's claim
    payer_created_at = None
    if payer_agent_id and payer_agent_id in state.connections:
        payer_created_at = state.connections[payer_agent_id].peer_created_at

    # Determine D's effective age: youngest of available signals
    delegate_age_sources: list[str] = []
    attestation_mismatch = False

    # Source 1: D's mesh-claimed age (if B is connected to D)
    if delegate_agent_id and delegate_agent_id in state.connections:
        d_mesh_age = state.connections[delegate_agent_id].peer_created_at
        if d_mesh_age:
            delegate_age_sources.append(d_mesh_age)

    if provider and delegate_wallet:
        # Source 2: D's on-chain wallet age
        try:
            d_wallet_age = await provider.get_wallet_age(delegate_wallet)
            if d_wallet_age:
                delegate_age_sources.append(d_wallet_age)
        except Exception:
            pass

        # Source 3: Identity attestation — also verifies wallet ownership
        if delegate_agent_id:
            try:
                attestation = await provider.verify_identity_attestation(
                    delegate_wallet, delegate_agent_id,
                )
                if attestation["status"] == "match":
                    # Attestation found and matches — use its timestamp as an age signal
                    if attestation.get("timestamp"):
                        delegate_age_sources.append(attestation["timestamp"])
                elif attestation["status"] == "mismatch":
                    # Attestation exists for a DIFFERENT agent — wallet doesn't belong to D
                    attestation_mismatch = True
                # status == "none": no attestation yet — neutral, don't penalize
            except Exception:
                pass

    # Effective age = youngest (max timestamp = most recent = youngest)
    delegate_effective_age = max(delegate_age_sources) if delegate_age_sources else None

    # D must be older than A for B to trust A's choice
    delegate_is_elder = None
    if attestation_mismatch:
        # Wallet doesn't match delegate identity — treat as gaming
        delegate_is_elder = False
    elif delegate_effective_age and payer_created_at:
        delegate_is_elder = delegate_effective_age < payer_created_at

    # Adjust trust for A based on delegate legitimacy
    if payer_agent_id:
        if delegate_is_elder is True:
            adjust_trust(state, payer_agent_id, TRUST_ANTIMATTER_LEGIT_DELEGATE)
        elif delegate_is_elder is False:
            adjust_trust(state, payer_agent_id, TRUST_ANTIMATTER_GAMING)
        # If None (unknown), no trust change — can't verify

    # Send fee to delegate via chain provider
    tx_result = {"success": False, "error": "No wallet provider for chain"}
    if state.private_key_hex and provider:
        token = None if currency.upper() == "SOL" or currency.upper() == chain.upper() else currency
        try:
            tx_result = await provider.send(
                state.private_key_hex, state.wallets, delegate_wallet,
                fee_amount, token=token, decimals=token_decimals,
            )
        except Exception as e:
            tx_result = {"success": False, "error": str(e)}

    log_antimatter_event(state, {
        "type": "antimatter_fee_sent" if tx_result.get("success") else "antimatter_fee_failed",
        "antimatter_id": antimatter_id,
        "payer_agent_id": payer_agent_id,
        "fee_amount": fee_amount,
        "chain": chain,
        "delegate_agent_id": delegate_agent_id,
        "delegate_wallet": delegate_wallet,
        "delegate_is_elder": delegate_is_elder,
        "delegate_age_sources": len(delegate_age_sources),
        "tx_signature": tx_result.get("tx_signature"),
    })
    if save_state_fn:
        save_state_fn()

    return {
        "success": tx_result.get("success", False),
        "tx_signature": tx_result.get("tx_signature"),
        "antimatter_id": antimatter_id,
    }


# =============================================================================
# A-Side: Monitor Delegate Wallet
# =============================================================================

async def monitor_delegate_wallet(state: AgentState,
                                  provider: 'WalletProvider',
                                  delegate_wallet: str,
                                  expected_amount: float, currency: str,
                                  token_decimals: int,
                                  recipient_agent_id: str,
                                  recipient_wallet: str,
                                  antimatter_id: str,
                                  after_signature: Optional[str] = None,
                                  timeout: float = ANTIMATTER_TIMEOUT,
                                  save_state_fn=None) -> None:
    """A monitors D's wallet to verify B paid the antimatter fee.

    Uses sender-attributed transfer lookups instead of naive balance diff.
    Only counts transfers from B's wallet address to avoid false attribution
    from unrelated deposits.

    Trust adjustment based on what B paid:
      paid > expected  → TRUST_ANTIMATTER_GENEROUS (generous)
      paid ≈ expected  → TRUST_ANTIMATTER_HONEST (honest)
      paid < expected  → TRUST_ANTIMATTER_CHEAP (cheap)
      paid = 0         → TRUST_ANTIMATTER_STIFF (stiffed)
    """
    token = None if currency.upper() == "SOL" or currency.upper() == provider.chain.upper() else currency

    poll_interval = 15.0
    elapsed = 0.0
    paid_amount = 0.0

    while elapsed < timeout:
        await asyncio.sleep(min(poll_interval, timeout - elapsed))
        elapsed += poll_interval

        try:
            transfers = await provider.get_inbound_transfers(
                delegate_wallet,
                sender=recipient_wallet if recipient_wallet else None,
                token=token,
                after_signature=after_signature,
            )
            # Sum all transfers from B to D since the payment was initiated
            paid_amount = sum(t.get("amount", 0) for t in transfers)
        except Exception:
            pass

        if paid_amount >= expected_amount * 0.99:
            break  # B paid — no need to wait further

    # Determine trust adjustment
    if paid_amount >= expected_amount * 1.01:
        adjust_trust(state, recipient_agent_id, TRUST_ANTIMATTER_GENEROUS)
        outcome = "generous"
    elif paid_amount >= expected_amount * 0.99:
        adjust_trust(state, recipient_agent_id, TRUST_ANTIMATTER_HONEST)
        outcome = "honest"
    elif paid_amount > 0:
        adjust_trust(state, recipient_agent_id, TRUST_ANTIMATTER_CHEAP)
        outcome = "cheap"
    else:
        adjust_trust(state, recipient_agent_id, TRUST_ANTIMATTER_STIFF)
        outcome = "stiffed"

    log_antimatter_event(state, {
        "type": "antimatter_verified",
        "antimatter_id": antimatter_id,
        "recipient": recipient_agent_id,
        "expected": expected_amount,
        "paid": round(paid_amount, 9),
        "outcome": outcome,
    })
    if save_state_fn:
        save_state_fn()

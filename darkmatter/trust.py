"""
Core trust dynamics for DarkMatter.

This module intentionally has no wallet or chain dependencies. Crypto/economy
addons can consume these primitives, but core messaging and connection health
must not depend on wallet providers being installed.
"""

import sys
import uuid
from datetime import datetime, timezone

from darkmatter.config import (
    RECIPROCITY_GRACE_THRESHOLD,
    TRUST_SEED_CAP,
)
from darkmatter.models import AgentState, Impression


_network_send_fn = None


def set_network_fns(send_fn=None) -> None:
    """Wire network callbacks used by trust automation."""
    global _network_send_fn
    _network_send_fn = send_fn


def adjust_trust(state: AgentState, agent_id: str, delta: float) -> None:
    """Adjust trust score for an agent by delta, with non-linear curves.

    Gains diminish at high trust. Penalties amplify at high trust. The negative
    recovery window is tracked so maintenance can disconnect sustained bad peers.
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
        score=new_score,
        note=imp.note,
        negative_since=negative_since,
        msgs_sent=imp.msgs_sent,
        msgs_received=imp.msgs_received,
        infrastructure=imp.infrastructure,
    )


def reciprocity_ratio(imp: Impression) -> float:
    """Compute reciprocity ratio for a peer: 1.0 = balanced, 0.0 = one-sided."""
    if imp.infrastructure:
        return 1.0
    total = max(imp.msgs_sent, imp.msgs_received)
    if total < RECIPROCITY_GRACE_THRESHOLD:
        return 1.0
    if total == 0:
        return 1.0
    return min(imp.msgs_sent, imp.msgs_received) / total


def compute_seeded_trust(state: AgentState, peer_opinions: list[dict]) -> float:
    """Compute initial trust for a new peer from existing peers' opinions."""
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


async def auto_disconnect_peer(state: AgentState, agent_id: str) -> bool:
    """Auto-disconnect a peer due to sustained negative trust."""
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

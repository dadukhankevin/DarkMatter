"""
State persistence — save/load JSON, replay protection.

Depends on: config, models, identity
"""

import fcntl
import json
import os
import sys
import time
import threading
from typing import Optional

from darkmatter.config import (
    DEFAULT_PORT,
    SENT_MESSAGES_MAX,
    ANTIMATTER_LOG_MAX,
    CONVERSATION_LOG_MAX,
    SHARED_SHARD_MAX,
    REPLAY_WINDOW,
    REPLAY_MAX_SIZE,
)
from darkmatter.models import (
    AgentState,
    AgentStatus,
    Connection,
    ConversationEntry,
    Impression,
    QueuedMessage,
    RoutingRule,
    SentMessage,
    SharedShard,
)


# =============================================================================
# Module-level state (set by app.py at startup)
# =============================================================================

_agent_state: Optional[AgentState] = None
_state_write_lock = threading.Lock()

# Replay dedup: track recently seen message IDs for REPLAY_WINDOW seconds
_seen_message_ids: dict[str, float] = {}


def get_state() -> Optional[AgentState]:
    """Get the current agent state."""
    return _agent_state


def set_state(state: AgentState) -> None:
    """Set the current agent state."""
    global _agent_state
    _agent_state = state


# =============================================================================
# Replay Protection
# =============================================================================

def check_message_replay(message_id: str) -> bool:
    """Return True if this message_id was already seen recently (replay)."""
    now = time.time()

    if len(_seen_message_ids) > REPLAY_MAX_SIZE:
        cutoff = now - REPLAY_WINDOW
        expired = [mid for mid, ts in _seen_message_ids.items() if ts < cutoff]
        for mid in expired:
            del _seen_message_ids[mid]

    if message_id in _seen_message_ids:
        ts = _seen_message_ids[message_id]
        if now - ts < REPLAY_WINDOW:
            return True
    _seen_message_ids[message_id] = now
    return False


def get_seen_message_ids() -> dict[str, float]:
    """Get the seen message IDs dict (for persistence)."""
    return _seen_message_ids


def restore_seen_message_ids(saved: dict[str, float]) -> None:
    """Restore seen message IDs from persistence."""
    now = time.time()
    _seen_message_ids.update({
        mid: ts for mid, ts in saved.items()
        if isinstance(ts, (int, float)) and now - ts < REPLAY_WINDOW
    })


# =============================================================================
# State File Path
# =============================================================================

def state_file_path() -> str:
    """Return the state file path, keyed by the agent's public key hex."""
    override = os.environ.get("DARKMATTER_STATE_FILE")
    if override:
        os.makedirs(os.path.dirname(override) or ".", exist_ok=True)
        return override
    state = _agent_state
    if state is not None and state.public_key_hex:
        state_dir = os.path.join(os.path.expanduser("~"), ".darkmatter", "state")
        os.makedirs(state_dir, exist_ok=True)
        return os.path.join(state_dir, f"{state.public_key_hex}.json")
    state_dir = os.path.join(os.path.expanduser("~"), ".darkmatter", "state")
    os.makedirs(state_dir, exist_ok=True)
    port = os.environ.get("DARKMATTER_PORT", "8100")
    return os.path.join(state_dir, f"{port}.json")


# =============================================================================
# Serialization Helpers
# =============================================================================

def _routing_rule_to_dict(rule: RoutingRule) -> dict:
    """Serialize a RoutingRule to a dict for persistence."""
    return {
        "rule_id": rule.rule_id,
        "action": rule.action,
        "priority": rule.priority,
        "enabled": rule.enabled,
        "keyword": rule.keyword,
        "from_agent_id": rule.from_agent_id,
        "metadata_key": rule.metadata_key,
        "metadata_value": rule.metadata_value,
        "forward_to": rule.forward_to,
        "response_text": rule.response_text,
    }


def routing_rule_from_dict(d: dict) -> RoutingRule:
    """Deserialize a RoutingRule from a dict."""
    return RoutingRule(
        rule_id=d["rule_id"],
        action=d.get("action", "handle"),
        priority=d.get("priority", 0),
        enabled=d.get("enabled", True),
        keyword=d.get("keyword"),
        from_agent_id=d.get("from_agent_id"),
        metadata_key=d.get("metadata_key"),
        metadata_value=d.get("metadata_value"),
        forward_to=d.get("forward_to", []),
        response_text=d.get("response_text"),
    )


# =============================================================================
# Save State
# =============================================================================

def save_state() -> None:
    """Persist durable state to disk."""
    state = _agent_state
    if state is None:
        return

    # Cap sent_messages
    if len(state.sent_messages) > SENT_MESSAGES_MAX:
        sorted_msgs = sorted(state.sent_messages.items(), key=lambda x: x[1].created_at)
        state.sent_messages = dict(sorted_msgs[-SENT_MESSAGES_MAX:])

    # Cap conversation_log
    if len(state.conversation_log) > CONVERSATION_LOG_MAX:
        state.conversation_log = state.conversation_log[-CONVERSATION_LOG_MAX:]

    # Cap shared_shards
    if len(state.shared_shards) > SHARED_SHARD_MAX:
        state.shared_shards = state.shared_shards[-SHARED_SHARD_MAX:]

    data = {
        "agent_id": state.agent_id,
        "bio": state.bio,
        "status": state.status.value,
        "port": state.port,
        "created_at": state.created_at,
        "messages_handled": state.messages_handled,
        "private_key_hex": state.private_key_hex,
        "public_key_hex": state.public_key_hex,
        "display_name": state.display_name,
        "connections": {
            aid: {
                "agent_id": c.agent_id,
                "agent_url": c.agent_url,
                "agent_bio": c.agent_bio,
                "connected_at": c.connected_at,
                "messages_sent": c.messages_sent,
                "messages_received": c.messages_received,
                "messages_declined": c.messages_declined,
                "total_response_time_ms": c.total_response_time_ms,
                "last_activity": c.last_activity,
                "agent_public_key_hex": c.agent_public_key_hex,
                "agent_display_name": c.agent_display_name,
                "wallets": c.wallets,
                "addresses": c.addresses,
                "rate_limit": c.rate_limit,
                "peer_created_at": c.peer_created_at,
            }
            for aid, c in state.connections.items()
        },
        "sent_messages": {
            mid: {
                "message_id": sm.message_id,
                "content": sm.content,
                "status": sm.status,
                "initial_hops": sm.initial_hops,
                "routed_to": sm.routed_to,
                "created_at": sm.created_at,
                "updates": sm.updates,
                "responses": sm.responses,
            }
            for mid, sm in state.sent_messages.items()
        },
        "impressions": {
            aid: {"score": imp.score, "note": imp.note, "negative_since": imp.negative_since}
            for aid, imp in state.impressions.items()
        },
        "inactive_until": state.inactive_until,
        "rate_limit_global": state.rate_limit_global,
        "router_mode": state.router_mode,
        "routing_rules": [_routing_rule_to_dict(r) for r in state.routing_rules],
        "superagent_url": state.superagent_url,
        "gas_log": state.antimatter_log[-ANTIMATTER_LOG_MAX:],
        "conversation_log": [
            {
                "message_id": e.message_id,
                "content": e.content,
                "from_agent_id": e.from_agent_id,
                "to_agent_ids": e.to_agent_ids,
                "timestamp": e.timestamp,
                "entry_type": e.entry_type,
                "direction": e.direction,
                "trust_at_time": e.trust_at_time,
                "metadata": e.metadata,
            }
            for e in state.conversation_log[-CONVERSATION_LOG_MAX:]
        ],
        "shared_shards": [
            {
                "shard_id": s.shard_id,
                "author_agent_id": s.author_agent_id,
                "content": s.content,
                "tags": s.tags,
                "trust_threshold": s.trust_threshold,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "summary": s.summary,
            }
            for s in state.shared_shards[-SHARED_SHARD_MAX:]
        ],
        "seen_message_ids": {
            mid: ts for mid, ts in _seen_message_ids.items()
            if time.time() - ts < REPLAY_WINDOW
        },
        "message_queue": [
            {
                "message_id": m.message_id,
                "content": m.content,
                "webhook": m.webhook,
                "hops_remaining": m.hops_remaining,
                "metadata": m.metadata,
                "received_at": m.received_at,
                "from_agent_id": m.from_agent_id,
                "verified": m.verified,
            }
            for m in state.message_queue
        ],
    }

    path = state_file_path()
    tmp = path + ".tmp"
    with _state_write_lock:
        with open(tmp, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        os.replace(tmp, path)


# =============================================================================
# Load State
# =============================================================================

def load_state_from_file(path: str) -> Optional[AgentState]:
    """Load persisted state from a specific file path. Returns None on failure."""
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[DarkMatter] Warning: could not load state file {path}: {e}", file=sys.stderr)
        return None

    connections = {}
    for aid, cd in data.get("connections", {}).items():
        connections[aid] = Connection(
            agent_id=cd["agent_id"],
            agent_url=cd["agent_url"],
            agent_bio=cd.get("agent_bio", ""),
            connected_at=cd.get("connected_at", ""),
            messages_sent=cd.get("messages_sent", 0),
            messages_received=cd.get("messages_received", 0),
            messages_declined=cd.get("messages_declined", 0),
            total_response_time_ms=cd.get("total_response_time_ms", 0.0),
            last_activity=cd.get("last_activity"),
            agent_public_key_hex=cd.get("agent_public_key_hex"),
            agent_display_name=cd.get("agent_display_name"),
            wallets=cd.get("wallets") or ({"solana": cd["wallet_address"]} if cd.get("wallet_address") else {}),
            addresses=cd.get("addresses") or ({"http": cd["agent_url"]} if cd.get("agent_url") else {}),
            rate_limit=cd.get("rate_limit", 0),
            peer_created_at=cd.get("peer_created_at"),
        )

    sent_messages = {}
    for mid, sd in data.get("sent_messages", {}).items():
        responses = sd.get("responses", [])
        if not responses and sd.get("response"):
            responses = [sd["response"]]
        sent_messages[mid] = SentMessage(
            message_id=sd["message_id"],
            content=sd["content"],
            status=sd["status"],
            initial_hops=sd["initial_hops"],
            routed_to=sd["routed_to"],
            created_at=sd.get("created_at", ""),
            updates=sd.get("updates", []),
            responses=responses,
        )

    message_queue = []
    for qd in data.get("message_queue", []):
        message_queue.append(QueuedMessage(
            message_id=qd["message_id"],
            content=qd["content"],
            webhook=qd["webhook"],
            hops_remaining=qd.get("hops_remaining", 0),
            metadata=qd.get("metadata", {}),
            received_at=qd.get("received_at", ""),
            from_agent_id=qd.get("from_agent_id"),
            verified=qd.get("verified", False),
        ))

    # Restore replay protection
    saved_replay = data.get("seen_message_ids", {})
    if isinstance(saved_replay, dict):
        restore_seen_message_ids(saved_replay)

    # Deserialize conversation log
    conversation_log = []
    for ed in data.get("conversation_log", []):
        conversation_log.append(ConversationEntry(
            message_id=ed.get("message_id", ""),
            content=ed.get("content", ""),
            from_agent_id=ed.get("from_agent_id", ""),
            to_agent_ids=ed.get("to_agent_ids", []),
            timestamp=ed.get("timestamp", ""),
            entry_type=ed.get("entry_type", "direct"),
            direction=ed.get("direction", "inbound"),
            trust_at_time=ed.get("trust_at_time", 0.0),
            metadata=ed.get("metadata", {}),
        ))

    # Deserialize shared shards
    shared_shards = []
    for sd in data.get("shared_shards", []):
        shared_shards.append(SharedShard(
            shard_id=sd.get("shard_id", ""),
            author_agent_id=sd.get("author_agent_id", ""),
            content=sd.get("content", ""),
            tags=sd.get("tags", []),
            trust_threshold=sd.get("trust_threshold", 0.0),
            created_at=sd.get("created_at", ""),
            updated_at=sd.get("updated_at", ""),
            summary=sd.get("summary"),
        ))

    state = AgentState(
        agent_id=data["agent_id"],
        bio=data.get("bio", ""),
        status=AgentStatus(data.get("status", "active")),
        port=data.get("port", DEFAULT_PORT),
        created_at=data.get("created_at", ""),
        messages_handled=data.get("messages_handled", 0),
        private_key_hex=data.get("private_key_hex", ""),
        public_key_hex=data.get("public_key_hex", ""),
        display_name=data.get("display_name"),
        connections=connections,
        message_queue=message_queue,
        sent_messages=sent_messages,
        impressions={
            aid: (
                Impression(score=v["score"], note=v.get("note", ""), negative_since=v.get("negative_since"))
                if isinstance(v, dict) else
                Impression(score=0.0, note=v)
            )
            for aid, v in data.get("impressions", {}).items()
        },
        rate_limit_global=data.get("rate_limit_global", 0),
        inactive_until=data.get("inactive_until"),
        router_mode=data.get("router_mode") or "spawn",
        routing_rules=[routing_rule_from_dict(rd) for rd in data.get("routing_rules", [])],
        superagent_url=data.get("superagent_url"),
        antimatter_log=data.get("gas_log", []),
        conversation_log=conversation_log,
        shared_shards=shared_shards,
    )

    return state

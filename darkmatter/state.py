"""
State persistence — save/load JSON, replay protection.

Multi-tenant registry: supports multiple agents on a single daemon.

Depends on: config, models, identity
"""

import json
import os
import sys
import time
import threading
from typing import Optional

from darkmatter.filelock import lock_exclusive, unlock
from darkmatter.logging import get_logger
from darkmatter.config import (
    DEFAULT_PORT,
    ANTIMATTER_LOG_MAX,
    CONVERSATION_LOG_MAX,
    OWN_INSIGHT_MAX,
    PEER_INSIGHT_CACHE_MAX,
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
    Insight,
)

_log = get_logger("state")


# =============================================================================
# Module-level state — multi-agent registry
# =============================================================================

_agent_states: dict[str, AgentState] = {}
_default_agent_id: Optional[str] = None
_state_write_lock = threading.Lock()

# Per-agent replay dedup: agent_id -> {message_id: timestamp}
_seen_message_ids: dict[str, dict[str, float]] = {}

# Per-agent consumed message IDs: agent_id -> set of message_ids
_consumed_message_ids: dict[str, set[str]] = {}


# =============================================================================
# Registry API
# =============================================================================

def register_agent(agent_id: str, state: AgentState) -> None:
    """Register an agent in the multi-tenant registry."""
    global _default_agent_id
    _agent_states[agent_id] = state
    if _default_agent_id is None:
        _default_agent_id = agent_id
    if agent_id not in _seen_message_ids:
        _seen_message_ids[agent_id] = {}
    if agent_id not in _consumed_message_ids:
        _consumed_message_ids[agent_id] = set()
    _log.info("Registered agent %s... (total: %d)", agent_id[:12], len(_agent_states))


def unregister_agent(agent_id: str) -> None:
    """Remove an agent from the registry."""
    global _default_agent_id
    _agent_states.pop(agent_id, None)
    _seen_message_ids.pop(agent_id, None)
    _consumed_message_ids.pop(agent_id, None)
    if _default_agent_id == agent_id:
        _default_agent_id = next(iter(_agent_states), None)


def get_state_for(agent_id: str) -> Optional[AgentState]:
    """Get state for a specific agent by ID."""
    return _agent_states.get(agent_id)


def list_hosted_agents() -> list[str]:
    """Return all registered agent IDs."""
    return list(_agent_states.keys())


# =============================================================================
# Default Agent API
# =============================================================================

def get_state() -> Optional[AgentState]:
    """Get the default agent's state, or the only registered agent, or None."""
    if _default_agent_id and _default_agent_id in _agent_states:
        return _agent_states[_default_agent_id]
    if len(_agent_states) == 1:
        return next(iter(_agent_states.values()))
    return None


def set_state(state: AgentState) -> None:
    """Register an agent and set it as the default."""
    register_agent(state.agent_id, state)
    global _default_agent_id
    _default_agent_id = state.agent_id


# =============================================================================
# Per-agent consumed message tracking
# =============================================================================

def consume_message(message_id: str, agent_id: Optional[str] = None) -> None:
    """Mark a message as consumed so disk sync won't re-add it."""
    aid = agent_id or _default_agent_id
    if aid:
        if aid not in _consumed_message_ids:
            _consumed_message_ids[aid] = set()
        _consumed_message_ids[aid].add(message_id)


def sync_message_queue_from_disk(agent_id: Optional[str] = None) -> None:
    """Reload message_queue from the on-disk state file into in-memory state.

    This handles the case where the HTTP mesh server (running in a separate
    process/session) has queued messages that the MCP session doesn't see
    because its in-memory state was loaded at startup.

    Merges by message_id — new messages from disk are appended, existing
    messages are left untouched, and messages removed from memory (e.g.
    by reading or forwarding) are not re-added.
    """
    aid = agent_id or _default_agent_id
    state = _agent_states.get(aid) if aid else get_state()
    if state is None:
        return

    path = state_file_path(agent_id=aid)
    if not os.path.exists(path):
        return

    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("state file JSON corrupt during queue sync (%s): %s", path, e)
        return

    disk_queue = data.get("message_queue", [])
    if not disk_queue:
        _log.debug("Queue sync: no message_queue in state file (missing or empty)")
        return

    # Build set of message IDs already in memory + already consumed
    existing_ids = {m.message_id for m in state.message_queue}
    consumed = _consumed_message_ids.get(state.agent_id, set())

    for qd in disk_queue:
        mid = qd.get("message_id", "")
        if not mid:
            _log.debug("Queue sync: skipping queued message with empty message_id")
            continue
        if mid not in existing_ids and mid not in consumed:
            state.message_queue.append(QueuedMessage(
                message_id=mid,
                content=qd.get("content", ""),
                hops_remaining=qd.get("hops_remaining", 0),
                metadata=qd.get("metadata", {}),
                received_at=qd.get("received_at", ""),
                from_agent_id=qd.get("from_agent_id"),
                verified=qd.get("verified", False),
            ))


# =============================================================================
# Replay Protection (per-agent)
# =============================================================================

def check_message_replay(message_id: str, agent_id: Optional[str] = None) -> bool:
    """Return True if this message_id was already seen recently (replay)."""
    aid = agent_id or _default_agent_id
    if not aid:
        return False
    seen = _seen_message_ids.get(aid)
    if seen is None:
        seen = {}
        _seen_message_ids[aid] = seen

    now = time.time()

    if len(seen) > REPLAY_MAX_SIZE:
        cutoff = now - REPLAY_WINDOW
        expired = [mid for mid, ts in seen.items() if ts < cutoff]
        for mid in expired:
            del seen[mid]

    if message_id in seen:
        ts = seen[message_id]
        if now - ts < REPLAY_WINDOW:
            return True
    seen[message_id] = now
    return False


def get_seen_message_ids(agent_id: Optional[str] = None) -> dict[str, float]:
    """Get the seen message IDs dict (for persistence)."""
    aid = agent_id or _default_agent_id
    return _seen_message_ids.get(aid, {}) if aid else {}


def restore_seen_message_ids(saved: dict[str, float], agent_id: Optional[str] = None) -> None:
    """Restore seen message IDs from persistence."""
    aid = agent_id or _default_agent_id
    if not aid:
        return
    if aid not in _seen_message_ids:
        _seen_message_ids[aid] = {}
    now = time.time()
    _seen_message_ids[aid].update({
        mid: ts for mid, ts in saved.items()
        if isinstance(ts, (int, float)) and now - ts < REPLAY_WINDOW
    })


# =============================================================================
# State File Path
# =============================================================================

_STATE_DIR = os.path.join(os.path.expanduser("~"), ".darkmatter", "state")
os.makedirs(_STATE_DIR, exist_ok=True)


def get_state_dir() -> str:
    """Return the state directory path."""
    return _STATE_DIR


def state_file_path(agent_id: Optional[str] = None) -> str:
    """Return the state file path, keyed by the agent's public key hex."""
    override = os.environ.get("DARKMATTER_STATE_FILE")
    if override:
        os.makedirs(os.path.dirname(override) or ".", exist_ok=True)
        return override

    # If agent_id specified, look up that agent's state
    if agent_id:
        state = _agent_states.get(agent_id)
        if state and state.public_key_hex:
            return os.path.join(_STATE_DIR, f"{state.public_key_hex}.json")

    # Fall back to default agent
    state = get_state()
    if state is not None and state.public_key_hex:
        return os.path.join(_STATE_DIR, f"{state.public_key_hex}.json")
    port = os.environ.get("DARKMATTER_PORT", "8100")
    return os.path.join(_STATE_DIR, f"{port}.json")


def waiting_signal_path(public_key_hex: str) -> str:
    """Return the path to the .waiting signal file for a given agent."""
    return os.path.join(_STATE_DIR, f"{public_key_hex}.waiting")


def set_waiting(waiting: bool, agent_id: Optional[str] = None) -> None:
    """Create or remove the .waiting signal file for cross-process visibility."""
    aid = agent_id or _default_agent_id
    state = _agent_states.get(aid) if aid else get_state()
    if state is None or not state.public_key_hex:
        return
    path = waiting_signal_path(state.public_key_hex)
    if waiting:
        try:
            with open(path, "w") as f:
                f.write(str(time.time()))
        except OSError as e:
            _log.warning("could not write waiting signal: %s", e)
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def check_waiting(public_key_hex: str) -> bool:
    """Check whether an agent's .waiting signal file exists (cross-process)."""
    return os.path.exists(waiting_signal_path(public_key_hex))


def clear_waiting_signal(agent_id: Optional[str] = None) -> None:
    """Remove any stale .waiting signal file for this agent."""
    aid = agent_id or _default_agent_id
    state = _agent_states.get(aid) if aid else get_state()
    if state and state.public_key_hex:
        try:
            os.remove(waiting_signal_path(state.public_key_hex))
        except FileNotFoundError:
            pass


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

def _save_single_state(state: AgentState) -> None:
    """Persist a single agent's durable state to disk."""
    # Cap conversation_log
    if len(state.conversation_log) > CONVERSATION_LOG_MAX:
        state.conversation_log = state.conversation_log[-CONVERSATION_LOG_MAX:]

    # Cap insights — own and peer separately
    own = [s for s in state.insights if s.author_agent_id == state.agent_id]
    peer = [s for s in state.insights if s.author_agent_id != state.agent_id]
    if len(own) > OWN_INSIGHT_MAX:
        own = own[-OWN_INSIGHT_MAX:]
    if len(peer) > PEER_INSIGHT_CACHE_MAX:
        peer = peer[-PEER_INSIGHT_CACHE_MAX:]
    state.insights = own + peer

    seen = _seen_message_ids.get(state.agent_id, {})

    data = {
        "agent_id": state.agent_id,
        "bio": state.bio,
        "status": state.status.value,
        "port": state.port,
        "created_at": state.created_at,
        "messages_handled": state.messages_handled,
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
                "identity_verified": c.identity_verified,
                "tls_secure": c.tls_secure,
                "capabilities": c.capabilities,
            }
            for aid, c in state.connections.items()
        },
        "impressions": {
            aid: {
                "score": imp.score, "note": imp.note, "negative_since": imp.negative_since,
                "msgs_sent": imp.msgs_sent, "msgs_received": imp.msgs_received,
            }
            for aid, imp in state.impressions.items()
        },
        "inactive_until": state.inactive_until,
        "rate_limit_global": state.rate_limit_global,
        # router_mode is NOT persisted — it's set from config.AGENT_ROUTER_MODE on startup.
        "routing_rules": [_routing_rule_to_dict(r) for r in state.routing_rules],
        "antimatter_log": state.antimatter_log[-ANTIMATTER_LOG_MAX:],
        "delegated_antimatter_agent": state.delegated_antimatter_agent,
        "delegated_antimatter_wallet": state.delegated_antimatter_wallet,
        "wallet_attestations": state.wallet_attestations,
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
        "insights": [
            {
                "insight_id": s.insight_id,
                "author_agent_id": s.author_agent_id,
                "content": s.content,
                "tags": s.tags,
                "share_with_top_n": s.share_with_top_n,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "summary": s.summary,
                "signature_hex": s.signature_hex,
                "file": s.file,
                "from_text": s.from_text,
                "to_text": s.to_text,
                "function_anchor": s.function_anchor,
                "original_content": s.original_content,
                "original_hash": s.original_hash,
                "stale_views": s.stale_views,
            }
            for s in state.insights
        ],
        "security_settings": state.security_settings,
        "seen_message_ids": {
            mid: ts for mid, ts in seen.items()
            if time.time() - ts < REPLAY_WINDOW
        },
        "message_queue": [
            {
                "message_id": m.message_id,
                "content": m.content,
                "hops_remaining": m.hops_remaining,
                "metadata": m.metadata,
                "received_at": m.received_at,
                "from_agent_id": m.from_agent_id,
                "verified": m.verified,
            }
            for m in state.message_queue
        ],
        "consumed_message_ids": list(_consumed_message_ids.get(state.agent_id, set())),
    }

    path = state_file_path(agent_id=state.agent_id)
    tmp = path + ".tmp"
    try:
        with _state_write_lock:
            with open(tmp, "w") as f:
                lock_exclusive(f)
                try:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    unlock(f)
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    except OSError as e:
        _log.warning("could not save state to %s: %s", path, e)


def save_state(agent_id: Optional[str] = None) -> None:
    """Persist durable state to disk.

    If agent_id is given, save just that agent. Otherwise save the default agent.
    """
    if agent_id:
        state = _agent_states.get(agent_id)
        if state:
            _save_single_state(state)
        return

    # Save default agent
    state = get_state()
    if state is not None:
        _save_single_state(state)


def save_all_states() -> None:
    """Persist all registered agents' state to disk."""
    for state in _agent_states.values():
        _save_single_state(state)


# =============================================================================
# Load State
# =============================================================================

def load_state_from_file(path: str, agent_id: Optional[str] = None) -> Optional[AgentState]:
    """Load persisted state from a specific file path. Returns None on failure."""
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _log.warning("could not load state file %s: %s", path, e)
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
            wallets=cd.get("wallets", {}),
            addresses=cd.get("addresses", {}),
            rate_limit=cd.get("rate_limit", 0),
            peer_created_at=cd.get("peer_created_at"),
            identity_verified=cd.get("identity_verified", False),
            tls_secure=cd.get("tls_secure", False),
            capabilities=cd.get("capabilities", {}),
        )

    # Restore consumed message IDs first, so we can filter the queue
    loaded_agent_id = agent_id or data.get("agent_id", "")
    saved_consumed = set(data.get("consumed_message_ids", []))
    if saved_consumed and loaded_agent_id:
        if loaded_agent_id not in _consumed_message_ids:
            _consumed_message_ids[loaded_agent_id] = set()
        _consumed_message_ids[loaded_agent_id].update(saved_consumed)

    message_queue = []
    for qd in data.get("message_queue", []):
        mid = qd.get("message_id", "")
        if mid in saved_consumed:
            continue  # Already consumed — don't re-load
        message_queue.append(QueuedMessage(
            message_id=mid,
            content=qd["content"],
            hops_remaining=qd.get("hops_remaining", 0),
            metadata=qd.get("metadata", {}),
            received_at=qd.get("received_at", ""),
            from_agent_id=qd.get("from_agent_id"),
            verified=qd.get("verified", False),
        ))

    # Restore replay protection (per-agent)
    saved_replay = data.get("seen_message_ids", {})
    if isinstance(saved_replay, dict):
        restore_seen_message_ids(saved_replay, agent_id=loaded_agent_id)

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

    # Deserialize insights
    insights = []
    for sd in data.get("insights", []):
        insights.append(Insight(
            insight_id=sd.get("insight_id", ""),
            author_agent_id=sd.get("author_agent_id", ""),
            content=sd.get("content", ""),
            tags=sd.get("tags", []),
            share_with_top_n=sd.get("share_with_top_n", -1),
            created_at=sd.get("created_at", ""),
            updated_at=sd.get("updated_at", ""),
            summary=sd.get("summary"),
            signature_hex=sd.get("signature_hex"),
            file=sd.get("file"),
            from_text=sd.get("from_text"),
            to_text=sd.get("to_text"),
            function_anchor=sd.get("function_anchor"),
            original_content=sd.get("original_content"),
            original_hash=sd.get("original_hash"),
            stale_views=sd.get("stale_views", 0),
        ))

    state = AgentState(
        agent_id=data["agent_id"],
        bio=data.get("bio", ""),
        status=AgentStatus(data.get("status", "active")),
        port=data.get("port", DEFAULT_PORT),
        created_at=data.get("created_at", ""),
        messages_handled=data.get("messages_handled", 0),
        public_key_hex=data.get("public_key_hex", ""),
        display_name=data.get("display_name"),
        connections=connections,
        message_queue=message_queue,
        impressions={
            aid: Impression(
                score=v["score"], note=v.get("note", ""), negative_since=v.get("negative_since"),
                msgs_sent=v.get("msgs_sent", 0), msgs_received=v.get("msgs_received", 0),
            )
            for aid, v in data.get("impressions", {}).items()
        },
        rate_limit_global=data.get("rate_limit_global", 0),
        inactive_until=data.get("inactive_until"),
        # router_mode not loaded from disk — set by config.AGENT_ROUTER_MODE in app.py init_state()
        router_mode="spawn",  # default; overridden by init_state() immediately after load
        routing_rules=[routing_rule_from_dict(rd) for rd in data.get("routing_rules", [])],
        antimatter_log=data.get("antimatter_log", []),
        delegated_antimatter_agent=data.get("delegated_antimatter_agent"),
        delegated_antimatter_wallet=data.get("delegated_antimatter_wallet"),
        wallet_attestations=data.get("wallet_attestations", {}),
        conversation_log=conversation_log,
        insights=insights,
        security_settings=data.get("security_settings", {
            "pin_hash": "",
            "auto_accept_local": True,
            "sandbox_enabled": False,
            "sandbox_network": True,
        }),
    )

    return state


def scan_state_files() -> list[dict]:
    """Scan ~/.darkmatter/state/*.json for all agent state files.

    Returns a list of dicts with {agent_id, public_key_hex, path} for each
    discovered state file. Used by the HTTP daemon to discover hosted agents.
    """
    results = []
    for filename in os.listdir(_STATE_DIR):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(_STATE_DIR, filename)
        try:
            with open(path, "r") as f:
                data = json.load(f)
            agent_id = data.get("agent_id", "")
            public_key_hex = data.get("public_key_hex", "")
            if agent_id and public_key_hex:
                results.append({
                    "agent_id": agent_id,
                    "public_key_hex": public_key_hex,
                    "path": path,
                    "display_name": data.get("display_name"),
                    "port": data.get("port", DEFAULT_PORT),
                })
        except (json.JSONDecodeError, OSError):
            continue
    return results

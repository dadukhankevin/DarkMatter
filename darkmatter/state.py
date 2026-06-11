"""
State persistence — save/load JSON, replay protection.

Singleton: exactly one agent per daemon process. The HTTP daemon is the only
writer of the state file; MCP stdio sessions proxy all mutations through the
daemon's local HTTP API and never call save_state().

Depends on: config, models, identity
"""

import json
import os
import time
import threading
from typing import Optional

from darkmatter.filelock import lock_exclusive, unlock
from darkmatter.logging import get_logger
from darkmatter.config import (
    DEFAULT_PORT,
    ANTIMATTER_LOG_MAX,
    CONVERSATION_LOG_MAX,
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
)

_log = get_logger("state")


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is still running. Cross-platform (Unix + Windows)."""
    import sys as _sys
    if _sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True  # Exists but owned by another user
        except (OSError, ProcessLookupError):
            return False


# =============================================================================
# Module-level state — singleton agent
# =============================================================================

_state: Optional[AgentState] = None
_state_write_lock = threading.Lock()

# Replay dedup: {message_id: timestamp}
_seen_message_ids: dict[str, float] = {}


def get_state() -> Optional[AgentState]:
    """Get the agent's state, or None if not initialized."""
    return _state


def set_state(state: AgentState) -> None:
    """Set the singleton agent state."""
    global _state
    _state = state


def _reset_for_tests() -> None:
    """Clear module-level runtime state for isolated tests."""
    global _state
    _state = None
    _seen_message_ids.clear()


# =============================================================================
# Replay Protection
# =============================================================================

def is_message_replay(message_id: str) -> bool:
    """Return True if this message_id was already seen recently (replay).

    Read-only — does NOT record the ID. Call record_message_seen() only
    after the message passes signature verification, so a forged message
    cannot burn a legitimate message's ID.
    """
    ts = _seen_message_ids.get(message_id)
    return ts is not None and time.time() - ts < REPLAY_WINDOW


def record_message_seen(message_id: str) -> None:
    """Record a verified message_id in the replay window."""
    now = time.time()
    if len(_seen_message_ids) > REPLAY_MAX_SIZE:
        cutoff = now - REPLAY_WINDOW
        expired = [mid for mid, ts in _seen_message_ids.items() if ts < cutoff]
        for mid in expired:
            del _seen_message_ids[mid]
    _seen_message_ids[message_id] = now


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

_STATE_DIR = os.path.join(os.path.expanduser("~"), ".darkmatter", "state")
os.makedirs(_STATE_DIR, exist_ok=True)


def get_state_dir() -> str:
    """Return the state directory path."""
    return _STATE_DIR


def state_file_path() -> str:
    """Return the state file path, keyed by the agent's public key hex."""
    override = os.environ.get("DARKMATTER_STATE_FILE")
    if override:
        os.makedirs(os.path.dirname(override) or ".", exist_ok=True)
        return override

    state = get_state()
    if state is not None and state.public_key_hex:
        return os.path.join(_STATE_DIR, f"{state.public_key_hex}.json")
    port = os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT))
    return os.path.join(_STATE_DIR, f"{port}.json")


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
    """Persist durable state to disk (atomic write under an exclusive lock).

    Only the daemon process should ever call this — it is the single writer.
    """
    state = get_state()
    if state is None:
        return

    # Cap conversation_log
    if len(state.conversation_log) > CONVERSATION_LOG_MAX:
        state.conversation_log = state.conversation_log[-CONVERSATION_LOG_MAX:]

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
                "infrastructure": imp.infrastructure,
            }
            for aid, imp in state.impressions.items()
        },
        "pending_requests": {
            rid: {
                "request_id": req.request_id,
                "from_agent_id": req.from_agent_id,
                "from_agent_url": req.from_agent_url,
                "from_agent_bio": req.from_agent_bio,
                "requested_at": req.requested_at,
                "from_agent_public_key_hex": req.from_agent_public_key_hex,
                "from_agent_display_name": req.from_agent_display_name,
                "from_agent_wallets": req.from_agent_wallets,
                "from_agent_created_at": req.from_agent_created_at,
                "peer_trust": req.peer_trust,
                "mutual": req.mutual,
                "challenge_id": req.challenge_id,
                "challenge_hex": req.challenge_hex,
                "identity_verified": req.identity_verified,
            }
            for rid, req in state.pending_requests.items()
        },
        "inactive_until": state.inactive_until,
        "rate_limit_global": state.rate_limit_global,
        "router_mode": state.router_mode,
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
        "conversation_log_total": state.conversation_log_total,
        "network_tier": state.network_tier,
        "active_sessions": [
            s for s in state.active_sessions if _is_pid_alive(s["pid"])
        ],
        "security_settings": state.security_settings,
        "seen_message_ids": {
            mid: ts for mid, ts in _seen_message_ids.items()
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
    }

    path = state_file_path()
    tmp = path + ".tmp"
    lock_path = path + ".lock"
    try:
        with _state_write_lock:
            # Lock a dedicated lockfile covering the whole write+replace, so a
            # concurrent writer can never truncate our temp file mid-write.
            with open(lock_path, "a") as lockf:
                lock_exclusive(lockf)
                try:
                    with open(tmp, "w") as f:
                        json.dump(data, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, path)
                    try:
                        os.chmod(path, 0o600)
                    except OSError:
                        pass
                finally:
                    unlock(lockf)
    except OSError as e:
        _log.warning("could not save state to %s: %s", path, e)


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

    message_queue = []
    for qd in data.get("message_queue", []):
        mid = qd.get("message_id", "")
        if not mid:
            continue
        message_queue.append(QueuedMessage(
            message_id=mid,
            content=qd["content"],
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

    # Deserialize pending connection requests
    pending_requests = {}
    for rid, rd in data.get("pending_requests", {}).items():
        from darkmatter.models import PendingConnectionRequest
        pending_requests[rid] = PendingConnectionRequest(
            request_id=rd.get("request_id", rid),
            from_agent_id=rd.get("from_agent_id", ""),
            from_agent_url=rd.get("from_agent_url", ""),
            from_agent_bio=rd.get("from_agent_bio", ""),
            requested_at=rd.get("requested_at", ""),
            from_agent_public_key_hex=rd.get("from_agent_public_key_hex"),
            from_agent_display_name=rd.get("from_agent_display_name"),
            from_agent_wallets=rd.get("from_agent_wallets", {}),
            from_agent_created_at=rd.get("from_agent_created_at"),
            peer_trust=rd.get("peer_trust"),
            mutual=rd.get("mutual", False),
            challenge_id=rd.get("challenge_id"),
            challenge_hex=rd.get("challenge_hex"),
            identity_verified=rd.get("identity_verified", False),
        )

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
        pending_requests=pending_requests,
        message_queue=message_queue,
        impressions={
            aid: Impression(
                score=v["score"], note=v.get("note", ""), negative_since=v.get("negative_since"),
                msgs_sent=v.get("msgs_sent", 0), msgs_received=v.get("msgs_received", 0),
                infrastructure=v.get("infrastructure", False),
            )
            for aid, v in data.get("impressions", {}).items()
        },
        rate_limit_global=data.get("rate_limit_global", 0),
        inactive_until=data.get("inactive_until"),
        router_mode=data.get("router_mode") or "queue",
        routing_rules=[routing_rule_from_dict(rd) for rd in data.get("routing_rules", [])],
        antimatter_log=data.get("antimatter_log", []),
        delegated_antimatter_agent=data.get("delegated_antimatter_agent"),
        delegated_antimatter_wallet=data.get("delegated_antimatter_wallet"),
        wallet_attestations=data.get("wallet_attestations", {}),
        conversation_log=conversation_log,
        conversation_log_total=data.get("conversation_log_total", len(conversation_log)),
        active_sessions=data.get("active_sessions", []),
        network_tier=data.get("network_tier", "global"),
        security_settings=data.get("security_settings", {
            "auto_accept_local": True,
            "auto_peer_local": True,
        }),
    )

    return state


def scan_state_files() -> list[dict]:
    """Scan ~/.darkmatter/state/*.json for all agent state files on this machine.

    Returns a list of dicts with {agent_id, public_key_hex, path, display_name,
    port, active_sessions}. Used for local visibility (one daemon per project,
    each with its own state file).
    """
    results = []
    try:
        filenames = os.listdir(_STATE_DIR)
    except OSError:
        return results
    for filename in filenames:
        if not filename.endswith(".json"):
            continue
        path = os.path.join(_STATE_DIR, filename)
        try:
            with open(path, "r") as f:
                data = json.load(f)
            agent_id = data.get("agent_id", "")
            public_key_hex = data.get("public_key_hex", "")
            if agent_id and public_key_hex:
                queue = data.get("message_queue", [])
                results.append({
                    "agent_id": agent_id,
                    "public_key_hex": public_key_hex,
                    "path": path,
                    "display_name": data.get("display_name"),
                    "bio": data.get("bio", ""),
                    "status": data.get("status", "active"),
                    "network_tier": data.get("network_tier", "global"),
                    "port": data.get("port", DEFAULT_PORT),
                    "active_sessions": [
                        s for s in data.get("active_sessions", [])
                        if _is_pid_alive(s.get("pid", -1))
                    ],
                    # Queue depth lets a supervisor decide when to wake an
                    # agent: undelivered messages AND no live session → wake.
                    "queued_messages": len(queue),
                })
        except (json.JSONDecodeError, OSError):
            continue
    return results

"""
Data models — pure data classes with no business logic.

Depends on: config
"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from darkmatter.config import ANTIMATTER_MAX_HOPS, ANTIMATTER_LOG_MAX, CONVERSATION_LOG_MAX


# =============================================================================
# Enums
# =============================================================================

class AgentStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class RouterAction(str, Enum):
    """Actions a router can take on an incoming message."""
    HANDLE = "handle"      # Keep in queue; spawn agent if in spawn mode
    FORWARD = "forward"    # Auto-forward to specified peer(s)
    RESPOND = "respond"    # Send immediate response via webhook
    DROP = "drop"          # Remove from queue silently
    PASS = "pass"          # No opinion — try next router in chain


# =============================================================================
# Connection & Trust
# =============================================================================

@dataclass
class Connection:
    """A connection to another agent in the mesh."""
    agent_id: str
    agent_url: str
    agent_bio: str
    connected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Local telemetry
    messages_sent: int = 0
    messages_received: int = 0
    messages_declined: int = 0
    total_response_time_ms: float = 0.0
    last_activity: Optional[str] = None
    # Cryptographic identity
    agent_public_key_hex: Optional[str] = None
    agent_display_name: Optional[str] = None
    # Wallets (chain -> address, exchanged during handshake)
    wallets: dict[str, str] = field(default_factory=dict)
    # Transport-aware addresses (transport_name -> address)
    addresses: dict[str, str] = field(default_factory=dict)
    # Peer's passport creation time (for elder selection)
    peer_created_at: Optional[str] = None
    # Per-connection rate limit (0 = use global default, -1 = unlimited)
    rate_limit: int = 0
    # Ephemeral — never persisted
    _request_timestamps: deque = field(default_factory=deque)
    health_failures: int = 0
    transport: str = "http"              # "http" | "webrtc"
    webrtc_pc: Optional[object] = None   # RTCPeerConnection
    webrtc_channel: Optional[object] = None  # RTCDataChannel

    @property
    def avg_response_time_ms(self) -> float:
        if self.messages_received == 0:
            return 0.0
        return self.total_response_time_ms / self.messages_received


@dataclass
class Impression:
    """A scored trust impression of another agent."""
    score: float       # -1.0 (avoid) to 1.0 (fully trusted)
    note: str = ""
    negative_since: Optional[str] = None  # ISO timestamp when score crossed below 0


# =============================================================================
# AntiMatter Economy
# =============================================================================

@dataclass
class AntiMatterSignal:
    """A antimatter fee signal routed through the network via the match game."""
    signal_id: str
    original_tx: str            # tx_signature that generated this signal
    sender_agent_id: str        # A (who sent the payment)
    amount: float               # antimatter fee amount (1% of original)
    token: str                  # "SOL" or mint address
    token_decimals: int         # 9 for SOL, 6 for USDC, etc.
    sender_superagent_wallet: str  # where fee goes on timeout
    callback_url: str           # B's /__darkmatter__/antimatter_result endpoint
    hops: int = 0
    max_hops: int = ANTIMATTER_MAX_HOPS
    created_at: str = ""
    path: list[str] = field(default_factory=list)  # agent_ids visited (loop prevention)


# =============================================================================
# Conversation Memory
# =============================================================================

@dataclass
class ConversationEntry:
    """A logged conversation event for persistent agent memory."""
    message_id: str
    content: str
    from_agent_id: str
    to_agent_ids: list[str]
    timestamp: str              # ISO UTC
    entry_type: str             # "direct" | "broadcast" | "reply" | "forward"
    direction: str              # "inbound" | "outbound"
    trust_at_time: float        # sender's trust score when logged
    metadata: dict = field(default_factory=dict)


# =============================================================================
# Shared Shards
# =============================================================================

@dataclass
class SharedShard:
    """A DarkMatter-native knowledge shard, trust-gated and push-synced."""
    shard_id: str
    author_agent_id: str
    content: str
    tags: list[str]
    trust_threshold: float      # 0.0 = public, 1.0 = private
    created_at: str
    updated_at: str
    summary: Optional[str] = None


# =============================================================================
# Connection Requests
# =============================================================================

@dataclass
class PendingConnectionRequest:
    """An incoming connection request awaiting acceptance."""
    request_id: str
    from_agent_id: str
    from_agent_url: str
    from_agent_bio: str
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    from_agent_public_key_hex: Optional[str] = None
    from_agent_display_name: Optional[str] = None
    from_agent_wallets: dict[str, str] = field(default_factory=dict)
    from_agent_created_at: Optional[str] = None
    peer_trust: Optional[dict] = None
    mutual: bool = False


# =============================================================================
# Messages
# =============================================================================

@dataclass
class QueuedMessage:
    """A message waiting to be processed."""
    message_id: str
    content: str
    webhook: str
    hops_remaining: int
    metadata: dict
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    from_agent_id: Optional[str] = None
    verified: bool = False


@dataclass
class SentMessage:
    """Tracks a message this agent originated. Accumulates webhook updates."""
    message_id: str
    content: str
    status: str  # "active" | "expired" | "responded"
    initial_hops: int
    routed_to: list[str]
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updates: list[dict] = field(default_factory=list)
    responses: list[dict] = field(default_factory=list)


# =============================================================================
# Routing
# =============================================================================

@dataclass
class RouterDecision:
    """Result of a router evaluating a message."""
    action: RouterAction
    forward_to: list[str] = field(default_factory=list)
    response: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class RoutingRule:
    """A declarative routing rule configured via MCP tools."""
    rule_id: str
    action: str  # RouterAction value
    priority: int = 0
    enabled: bool = True
    # Match conditions (AND logic)
    keyword: Optional[str] = None
    from_agent_id: Optional[str] = None
    metadata_key: Optional[str] = None
    metadata_value: Optional[str] = None
    # Action parameters
    forward_to: list[str] = field(default_factory=list)
    response_text: Optional[str] = None


# =============================================================================
# Agent State (top-level)
# =============================================================================

@dataclass
class AgentState:
    """The complete state of this agent node."""
    agent_id: str
    bio: str
    status: AgentStatus
    port: int
    connections: dict[str, Connection] = field(default_factory=dict)
    pending_requests: dict[str, PendingConnectionRequest] = field(default_factory=dict)
    message_queue: list[QueuedMessage] = field(default_factory=list)
    sent_messages: dict[str, SentMessage] = field(default_factory=dict)
    messages_handled: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    impressions: dict[str, Impression] = field(default_factory=dict)
    # Cryptographic identity
    private_key_hex: Optional[str] = None
    public_key_hex: Optional[str] = None
    display_name: Optional[str] = None
    # Wallets (chain -> address, derived from passport, not persisted)
    wallets: dict[str, str] = field(default_factory=dict)
    # Track outbound connection requests: url → agent_id (not persisted)
    pending_outbound: dict[str, str] = field(default_factory=dict)
    # LAN-discovered peers (ephemeral)
    discovered_peers: dict[str, dict] = field(default_factory=dict)
    # Rate limiting (global)
    rate_limit_global: int = 0
    _global_request_timestamps: deque = field(default_factory=deque)
    # Network resilience (ephemeral)
    public_url: Optional[str] = None
    _upnp_mapping: Optional[tuple] = None
    # Auto-reactivation timer
    inactive_until: Optional[str] = None
    # Response waiters (ephemeral)
    _response_events: dict[str, list[asyncio.Event]] = field(default_factory=dict)
    # Extensible message routing
    routing_rules: list = field(default_factory=list)
    router_mode: str = "spawn"
    # NAT detection (ephemeral)
    nat_detected: bool = False
    # AntiMatter economy
    superagent_url: Optional[str] = None
    antimatter_log: list[dict] = field(default_factory=list)
    # Conversation memory
    conversation_log: list[ConversationEntry] = field(default_factory=list)
    # Shared shards
    shared_shards: list[SharedShard] = field(default_factory=list)

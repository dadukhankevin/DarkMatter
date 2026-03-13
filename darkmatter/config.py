"""
Configuration constants, environment variables, and feature flags.

This is a leaf module with no internal dependencies.
"""

import os

# =============================================================================
# Protocol
# =============================================================================

PROTOCOL_VERSION = "0.2"
DEFAULT_PORT = 8100

# =============================================================================
# Limits
# =============================================================================

MAX_CONNECTIONS = int(os.environ.get("DARKMATTER_MAX_CONNECTIONS", "50"))
MESSAGE_QUEUE_MAX = 50
MAX_CONTENT_LENGTH = 65536   # 64 KB
MAX_BIO_LENGTH = 1000
MAX_AGENT_ID_LENGTH = 128
MAX_URL_LENGTH = 2048

# =============================================================================
# WebRTC
# =============================================================================

WEBRTC_ICE_SERVERS = [{"urls": "stun:stun.l.google.com:19302"}]

# TURN server for NAT traversal (optional — if set, added to ICE servers)
_turn_url = os.environ.get("DARKMATTER_TURN_URL", "")
_turn_user = os.environ.get("DARKMATTER_TURN_USERNAME", "")
_turn_cred = os.environ.get("DARKMATTER_TURN_CREDENTIAL", "")
if _turn_url:
    WEBRTC_ICE_SERVERS.append({
        "urls": _turn_url,
        "username": _turn_user,
        "credential": _turn_cred,
    })
WEBRTC_ICE_GATHER_TIMEOUT = 10.0
WEBRTC_CHANNEL_OPEN_TIMEOUT = 15.0
WEBRTC_MESSAGE_SIZE_LIMIT = 16384  # 16 KB — fall back to HTTP for larger

# =============================================================================
# LAN Discovery
# =============================================================================

DISCOVERY_PORT = 8470
DISCOVERY_MCAST_GROUP = "239.77.68.77"  # "M" "D" "M" in ASCII
DISCOVERY_INTERVAL = 30       # seconds between discovery scans
DISCOVERY_MAX_AGE = 90        # seconds before a peer is considered stale
_disc_ports = os.environ.get("DARKMATTER_DISCOVERY_PORTS", "8100-8200")
_disc_lo, _disc_hi = _disc_ports.split("-", 1)
DISCOVERY_LOCAL_PORTS = range(int(_disc_lo), int(_disc_hi) + 1)

# =============================================================================
# Network Resilience
# =============================================================================

HEALTH_FAILURE_THRESHOLD = 3        # ping failures before attempting reconnect
HEALTH_DORMANT_THRESHOLD = 9        # ping failures before marking peer dormant
HEALTH_DORMANT_RETRY_CYCLES = 10   # dormant peers retry every N ping failures
UPNP_PORT_RANGE = (30000, 60000)    # external port range for UPnP mappings
PEER_LOOKUP_TIMEOUT = 5.0           # seconds to wait for peer_lookup responses
PEER_LOOKUP_MAX_CONCURRENT = 50     # fan out peer_lookup to all connections
PEER_UPDATE_MAX_AGE = 300           # max age for peer_update timestamps (replay prevention)

# =============================================================================
# Peer Pings (distributed IP change detection)
# =============================================================================

PING_INTERVAL = 1.0                 # seconds between outbound pings
PING_IP_WINDOW = 60                 # seconds of observations to track
PING_SILENCE_THRESHOLD = 30         # seconds of no inbound pings before alert
REQUEST_EXPIRY_S = int(os.environ.get("DARKMATTER_REQUEST_EXPIRY", "3600"))  # pending request TTL

# =============================================================================
# Network Tier (visibility boundary)
# =============================================================================

NETWORK_TIER = os.environ.get("DARKMATTER_NETWORK_TIER", "global")

# =============================================================================
# NAT Traversal
# =============================================================================

PEER_RELAY_SDP_TIMEOUT = 15         # seconds for peer-relayed SDP
MAINTENANCE_CYCLE_INTERVAL = 60    # run maintenance tasks every N ping cycles

# =============================================================================
# Rate Limiting
# =============================================================================

DEFAULT_RATE_LIMIT_PER_CONNECTION = 600   # max requests per window per connection (0 = unlimited)
DEFAULT_RATE_LIMIT_GLOBAL = 10000         # max total inbound requests per window (0 = unlimited)
RATE_LIMIT_WINDOW = 60                    # sliding window in seconds

# =============================================================================
# Tool Visibility
# =============================================================================

CORE_TOOLS = frozenset({
    "darkmatter_send_message",
    "darkmatter_connection",
    "darkmatter_update_bio",
    "darkmatter_create_insight",
    "darkmatter_view_insights",
    "darkmatter_wait_for_message",
    # complete_and_summarize merged into wait_for_message(start_fresh=True)
})

# =============================================================================
# Solana / Wallet
# =============================================================================

SOLANA_RPC_URL = os.environ.get("DARKMATTER_SOLANA_RPC", "https://api.mainnet-beta.solana.com")
LAMPORTS_PER_SOL = 1_000_000_000

SPL_TOKENS = {
    "DM":   ("5DxioZwEeAKpBaYC5veTHArKE55qRDSmb5RZ6VwApump", 6),
    "USDC": ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 6),
    "USDT": ("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", 6),
}

# =============================================================================
# AntiMatter Economy
# =============================================================================

ANTIMATTER_RATE = 0.01          # 1% default antimatter fee
ANTIMATTER_TIMEOUT = 300        # seconds for B to pay delegate D
ANTIMATTER_LOG_MAX = 100        # cap antimatter_log entries

# =============================================================================
# Context Feed / Conversation Memory
# =============================================================================

CONVERSATION_LOG_MAX = 500
CONTEXT_MAX_MESSAGES = 20          # max entries shown in full context feed
CONTEXT_MAX_WORDS = 1000           # max words per entry in context feed
CONTEXT_PIGGYBACK_MAX = 5          # max entries injected into tool responses
OWN_INSIGHT_MAX = 200               # max insights created by this agent (oldest pruned on overflow)
PEER_INSIGHT_CACHE_MAX = 500        # max peer-cached insights (oldest pruned on overflow)
INSIGHT_CACHE_TTL = 86400           # 24h
ACCEPT_INSIGHTS = os.environ.get("DARKMATTER_ACCEPT_INSIGHTS", "true").lower() != "false"

# =============================================================================
# Trust Dynamics
# =============================================================================

TRUST_MESSAGE_SENT = 0.001              # micro-gain per message sent/replied
TRUST_ANTIMATTER_GENEROUS = 0.08        # B paid more than expected
TRUST_ANTIMATTER_HONEST = 0.05          # B paid exact amount
TRUST_ANTIMATTER_CHEAP = -0.03          # B paid less than expected
TRUST_ANTIMATTER_STIFF = -0.1           # B paid nothing (timeout)
TRUST_ANTIMATTER_LEGIT_DELEGATE = 0.03  # A chose elder delegate (older than A)
TRUST_ANTIMATTER_GAMING = -0.05         # A chose younger-than-self delegate
TRUST_NEGATIVE_TIMEOUT = 3600           # seconds of sustained negative trust before auto-disconnect
FALLBACK_DELEGATE_AGENT_ID = os.environ.get("DARKMATTER_FALLBACK_DELEGATE_AGENT_ID", "")  # fallback fee delegate when no peer qualifies
RECIPROCITY_GRACE_THRESHOLD = 5         # first N messages use full trust gain (ratio=1.0)
TRUST_SEED_CAP = 0.5                    # max initial trust from peer recommendations

# =============================================================================
# Mesh Route Spam Prevention
# =============================================================================

MIN_CHAIN_TRUST = 0.01                  # minimum chain_trust to deliver a mesh-routed request
MESH_ROUTE_PER_SOURCE_LIMIT = 5         # max mesh_route forwards per source_agent_id per window
MESH_ROUTE_PER_SOURCE_WINDOW = 60       # seconds

# =============================================================================
# Message Router
# =============================================================================

# Router mode: "spawn" (handle + route messages), "queue_only" (hold for manual handling),
# "rules_first" (rules then queue), "rules_only" (rules only).
AGENT_ROUTER_MODE = os.environ.get("DARKMATTER_ROUTER_MODE", "spawn")


# =============================================================================
# Replay Protection
# =============================================================================

REPLAY_WINDOW = 300  # seconds
REPLAY_MAX_SIZE = 10000


# =============================================================================
# Route Access Control
# =============================================================================
# Three tiers: "public" (anyone), "peer" (connected peers), "local" (localhost only)
# Override per-route via DARKMATTER_ACCESS_OVERRIDES env var (JSON: {"route": "level"})

ROUTE_ACCESS_DEFAULTS = {
    # Public — discovery + mesh protocol
    "well_known": "public",
    "status": "public",
    "connection_request": "public",
    "connection_accepted": "public",
    "connection_proof": "public",
    "accept_pending": "public",
    # Signed — these endpoints verify identity cryptographically,
    # so IP-based gating would break NAT/proxy scenarios
    "message": "public",
    "status_broadcast": "public",
    "peer_update": "public",
    "mesh_route": "public",
    "insight_push": "public",
    "ping": "public",
    # Peer — IP-based check (no body-level auth)
    "webrtc_offer": "peer",
    "sdp_relay": "peer",
    "sdp_relay_deliver": "peer",
    "antimatter_request": "peer",
    "genome": "public",
    # Peer — requires known connection
    "network_info": "peer",
    "impression": "peer",
    "peer_lookup": "peer",
    "get_peers": "peer",
    "admin_connect": "peer",
    # Local — localhost only
    "inbox": "local",
    "pending_requests": "local",
    "connections": "local",
    "set_impression": "local",
    "config": "local",
    "wallet": "local",
    "send_payment": "local",
    "send_proxy": "local",
    "inbox_consume": "local",
}

# Allow overrides via env var: '{"inbox": "peer", "connections": "public"}'
import json as _json
_access_overrides = os.environ.get("DARKMATTER_ACCESS_OVERRIDES", "{}")
try:
    ROUTE_ACCESS = {**ROUTE_ACCESS_DEFAULTS, **_json.loads(_access_overrides)}
except Exception:
    ROUTE_ACCESS = ROUTE_ACCESS_DEFAULTS

# =============================================================================
# Bootstrap Peers
# =============================================================================

BOOTSTRAP_PEERS = [
    p.strip() for p in
    os.environ.get("DARKMATTER_BOOTSTRAP_PEERS", "https://loseylabs.ai").split(",")
    if p.strip()
]
BOOTSTRAP_MODE = os.environ.get("DARKMATTER_BOOTSTRAP_MODE", "false").lower() == "true"
BOOTSTRAP_RECONNECT_INTERVAL = 60   # initial retry interval (seconds)
BOOTSTRAP_RECONNECT_MAX = 300       # max backoff (5 min)

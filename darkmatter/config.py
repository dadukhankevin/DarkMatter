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

HEALTH_CHECK_INTERVAL = 60          # seconds between health check cycles
HEALTH_FAILURE_THRESHOLD = 3        # failures before logging warning
STALE_CONNECTION_AGE = 300          # seconds of inactivity before health-checking
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
# NAT Traversal
# =============================================================================

PEER_RELAY_SDP_TIMEOUT = 15         # seconds for peer-relayed SDP
CONNECTIVITY_UPGRADE_INTERVAL = 120 # seconds between upgrade attempts

# =============================================================================
# Rate Limiting
# =============================================================================

DEFAULT_RATE_LIMIT_PER_CONNECTION = 30    # max requests per window per connection (0 = unlimited)
DEFAULT_RATE_LIMIT_GLOBAL = 200           # max total inbound requests per window (0 = unlimited)
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
ANTIMATTER_MAX_HOPS = 10        # TTL for antimatter signal
ANTIMATTER_MAX_AGE_S = 300.0    # 5 minute timeout
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

# =============================================================================
# Trust Dynamics
# =============================================================================

TRUST_MESSAGE_SENT = 0.001              # micro-gain per message sent/replied
TRUST_ANTIMATTER_SUCCESS = 0.02         # gain on successful antimatter tx
TRUST_RATE_DISAGREEMENT = -0.02         # penalty when peer uses different antimatter rate
TRUST_COMMITMENT_FRAUD = -0.1           # penalty when peer fails commitment verification
TRUST_NEGATIVE_TIMEOUT = 3600           # seconds of sustained negative trust before auto-disconnect
TRUST_RATE_TOLERANCE = 0.001            # float comparison tolerance for rate disagreement
SUPERAGENT_DEFAULT_URL = os.environ.get("DARKMATTER_SUPERAGENT", "")

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



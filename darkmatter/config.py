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
SENT_MESSAGES_MAX = 100
MAX_CONTENT_LENGTH = 65536   # 64 KB
MAX_BIO_LENGTH = 1000
MAX_AGENT_ID_LENGTH = 128
MAX_URL_LENGTH = 2048

# =============================================================================
# WebRTC
# =============================================================================

WEBRTC_STUN_SERVERS = [{"urls": "stun:stun.l.google.com:19302"}]
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
_disc_ports = os.environ.get("DARKMATTER_DISCOVERY_PORTS", "8100-8110")
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
IP_CHECK_INTERVAL = 300             # check public IP every 5 min
WEBHOOK_RECOVERY_MAX_ATTEMPTS = 3   # max peer-lookup recovery attempts per webhook call
WEBHOOK_RECOVERY_TIMEOUT = 30.0     # total wall-clock budget for all recovery attempts
ANCHOR_LOOKUP_TIMEOUT = 2.0         # seconds to wait for anchor node responses
PEER_UPDATE_MAX_AGE = 300           # max age for peer_update timestamps (replay prevention)

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
    "darkmatter_get_identity",
    "darkmatter_list_inbox",
    "darkmatter_get_message",
    "darkmatter_send_message",
    "darkmatter_list_connections",
    "darkmatter_connection",
    "darkmatter_update_bio",
    "darkmatter_status",
})

# =============================================================================
# Anchor Nodes
# =============================================================================

_ANCHOR_DEFAULT = "https://loseylabs.ai"
_anchor_env = os.environ.get("DARKMATTER_ANCHOR_NODES", _ANCHOR_DEFAULT).strip()
ANCHOR_NODES: list[str] = [u.strip().rstrip("/") for u in _anchor_env.split(",") if u.strip()] if _anchor_env else []

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
SUPERAGENT_DEFAULT_URL = os.environ.get(
    "DARKMATTER_SUPERAGENT",
    ANCHOR_NODES[0] if ANCHOR_NODES else "",
)

# =============================================================================
# Agent Auto-Spawn
# =============================================================================

AGENT_SPAWN_ENABLED = os.environ.get("DARKMATTER_AGENT_ENABLED", "true").lower() == "true"
AGENT_SPAWN_MAX_CONCURRENT = int(os.environ.get("DARKMATTER_AGENT_MAX_CONCURRENT", "2"))
AGENT_SPAWN_MAX_PER_HOUR = int(os.environ.get("DARKMATTER_AGENT_MAX_PER_HOUR", "6"))
AGENT_SPAWN_COMMAND = os.environ.get("DARKMATTER_AGENT_COMMAND", "claude")
AGENT_SPAWN_ARGS: list[str] = [
    a.strip() for a in os.environ.get(
        "DARKMATTER_AGENT_ARGS", "-p,--dangerously-skip-permissions"
    ).split(",") if a.strip()
]
AGENT_SPAWN_ENV_CLEANUP: list[str] = [
    v.strip() for v in os.environ.get(
        "DARKMATTER_AGENT_ENV_CLEANUP", "CLAUDECODE,CLAUDE_CODE_ENTRYPOINT"
    ).split(",") if v.strip()
]
AGENT_SPAWN_TIMEOUT = int(os.environ.get("DARKMATTER_AGENT_TIMEOUT", "300"))
AGENT_SPAWN_TERMINAL = os.environ.get("DARKMATTER_AGENT_TERMINAL", "false").lower() == "true"

# =============================================================================
# Entrypoint (human node) Auto-Start
# =============================================================================

ENTRYPOINT_AUTOSTART = os.environ.get("DARKMATTER_ENTRYPOINT_AUTOSTART", "true").lower() == "true"
ENTRYPOINT_PORT = int(os.environ.get("DARKMATTER_ENTRYPOINT_PORT", "8200"))
ENTRYPOINT_PATH = os.environ.get("DARKMATTER_ENTRYPOINT_PATH")  # explicit path, or None to search

# =============================================================================
# Replay Protection
# =============================================================================

REPLAY_WINDOW = 300  # seconds
REPLAY_MAX_SIZE = 10000

# =============================================================================
# Optional Dependencies (detected at import time)
# =============================================================================

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer, RTCDataChannel  # noqa: F401
    WEBRTC_AVAILABLE = True
except ImportError:
    WEBRTC_AVAILABLE = False

try:
    import miniupnpc  # noqa: F401
    UPNP_AVAILABLE = True
except ImportError:
    UPNP_AVAILABLE = False

try:
    import hashlib as _hashlib  # noqa: F401
    from solders.keypair import Keypair as SolanaKeypair  # noqa: F401
    from solders.pubkey import Pubkey as SolanaPubkey  # noqa: F401
    from solders.system_program import transfer as sol_transfer, TransferParams as SolTransferParams  # noqa: F401
    from solders.transaction import VersionedTransaction  # noqa: F401
    from solders.message import MessageV0  # noqa: F401
    from solana.rpc.async_api import AsyncClient as SolanaClient  # noqa: F401
    from spl.token.instructions import transfer_checked, TransferCheckedParams  # noqa: F401
    from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID  # noqa: F401
    from spl.token.instructions import create_associated_token_account  # noqa: F401
    SOLANA_AVAILABLE = True
except ImportError:
    SOLANA_AVAILABLE = False

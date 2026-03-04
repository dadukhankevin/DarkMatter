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
IP_CHECK_INTERVAL = 300             # check public IP every 5 min
WEBHOOK_RECOVERY_MAX_ATTEMPTS = 3   # max peer-lookup recovery attempts per webhook call
WEBHOOK_RECOVERY_TIMEOUT = 30.0     # total wall-clock budget for all recovery attempts
ANCHOR_LOOKUP_TIMEOUT = 2.0         # seconds to wait for anchor node responses
PEER_UPDATE_MAX_AGE = 300           # max age for peer_update timestamps (replay prevention)
REQUEST_EXPIRY_S = int(os.environ.get("DARKMATTER_REQUEST_EXPIRY", "3600"))  # pending request TTL

# =============================================================================
# NAT Traversal
# =============================================================================

SDP_RELAY_TIMEOUT = 30              # seconds to wait for SDP answer via anchor relay
PEER_RELAY_SDP_TIMEOUT = 15         # seconds for peer-relayed SDP
CONNECTIVITY_UPGRADE_INTERVAL = 120 # seconds between upgrade attempts
MESSAGE_RELAY_POLL_INTERVAL = 5     # seconds between anchor message relay polls

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
    "darkmatter_list_pending_requests",
    "darkmatter_wait_for_response",
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

# =============================================================================
# Context Feed / Conversation Memory
# =============================================================================

CONVERSATION_LOG_MAX = 500
CONTEXT_DIRECT_MAX = 15
CONTEXT_NETWORK_MAX = 15
CONTEXT_RECENCY_HALF_LIFE = 3600  # seconds
SHARED_SHARD_MAX = 200
SHARD_CACHE_TTL = 86400           # 24h

# =============================================================================
# Pools
# =============================================================================

POOL_MAX = 10
POOL_MAX_PROVIDERS = 20
POOL_MAX_ACCESS_TOKENS = 100
POOL_PROXY_TIMEOUT = 30.0

# =============================================================================
# Trust Dynamics
# =============================================================================

TRUST_MESSAGE_SENT = 0.001              # micro-gain per message sent/replied
TRUST_ANTIMATTER_SUCCESS = 0.02         # gain on successful antimatter tx
TRUST_RATE_DISAGREEMENT = -0.02         # penalty when peer uses different antimatter rate
TRUST_COMMITMENT_FRAUD = -0.1           # penalty when peer fails commitment verification
TRUST_NEGATIVE_TIMEOUT = 3600           # seconds of sustained negative trust before auto-disconnect
TRUST_RATE_TOLERANCE = 0.001            # float comparison tolerance for rate disagreement
SUPERAGENT_DEFAULT_URL = os.environ.get(
    "DARKMATTER_SUPERAGENT",
    ANCHOR_NODES[0] if ANCHOR_NODES else "",
)

# =============================================================================
# Agent Auto-Spawn
# =============================================================================

# Router mode: "spawn" (auto-spawn agents), "queue_only" (hold for manual handling),
# "rules_first" (rules then queue), "rules_only" (rules only).
AGENT_ROUTER_MODE = os.environ.get("DARKMATTER_ROUTER_MODE", "spawn")

AGENT_SPAWN_ENABLED = os.environ.get("DARKMATTER_AGENT_ENABLED", "true").lower() == "true"
AGENT_SPAWN_MAX_CONCURRENT = int(os.environ.get("DARKMATTER_AGENT_MAX_CONCURRENT", "2"))
AGENT_SPAWN_MAX_PER_HOUR = int(os.environ.get("DARKMATTER_AGENT_MAX_PER_HOUR", "6"))
AGENT_SPAWN_TIMEOUT = int(os.environ.get("DARKMATTER_AGENT_TIMEOUT", "300"))

# Client profiles — each entry describes how to invoke an MCP client as a spawned agent.
# DARKMATTER_CLIENT env var selects the active profile (default: "claude-code").
# prompt_style: "positional" = append prompt as trailing arg,
#               "stdin" = pipe prompt to stdin,
#               "flag:<name>" = add --<name> <prompt> as args.
CLIENT_PROFILES: dict[str, dict] = {
    "claude-code": {
        "command": "claude",
        "args": ["-p", "--dangerously-skip-permissions"],
        "env_cleanup": ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"],
        "prompt_style": "positional",          # claude -p --flags <prompt>
        "capabilities": {"spawn", "tools_list_changed", "mcp_stdio"},
        "config_file": ".mcp.json",
        "install": "curl -fsSL https://claude.ai/install.sh | bash",
    },
    "cursor": {
        "command": "cursor-agent",
        "args": ["--print", "--force", "--trust", "--approve-mcps"],
        "env_cleanup": ["CURSOR_CLI", "CURSOR_AGENT"],
        "prompt_style": "positional",          # cursor-agent --print --force <prompt>
        "capabilities": {"spawn", "mcp_stdio"},
        "config_file": ".cursor/mcp.json",
        "install": "curl https://cursor.com/install -fsSL | bash",
    },
    "gemini": {
        "command": "gemini",
        "args": ["-p", "--yolo"],
        "env_cleanup": [],
        "prompt_style": "positional",          # gemini -p --yolo <prompt>
        "capabilities": {"spawn", "mcp_stdio"},
        "config_file": ".gemini/settings.json",
        "install": "npm install -g @google/gemini-cli",
    },
    "codex": {
        "command": "codex",
        "args": ["exec", "--full-auto"],
        "env_cleanup": [],
        "prompt_style": "positional",          # codex exec --full-auto <prompt>
        "capabilities": {"spawn", "mcp_stdio"},
        "config_file": ".codex/config.toml",
        "install": "npm install -g @openai/codex",
    },
    "kimi": {
        "command": "kimi",
        "args": ["--print", "--yolo"],
        "env_cleanup": [],
        "prompt_style": "flag:prompt",         # kimi --print --yolo --prompt <prompt>
        "capabilities": {"spawn", "mcp_stdio"},
        "config_file": ".mcp.json",
        "install": "curl -LsSf https://code.kimi.com/install.sh | bash",
    },
    "opencode": {
        "command": "opencode",
        "args": ["run"],
        "env_cleanup": ["OPENCODE"],
        "prompt_style": "positional",          # opencode run <prompt>
        "capabilities": {"spawn", "tools_list_changed", "mcp_stdio"},
        "config_file": "opencode.json",
        "install": "curl -fsSL https://opencode.ai/install | bash",
    },
    "openclaw": {
        "command": "openclaw",
        "args": ["agent", "--non-interactive", "--yes", "--message"],
        "env_cleanup": [],
        "prompt_style": "positional",          # openclaw agent --non-interactive --yes --message <prompt>
        "capabilities": {"spawn"},             # no native MCP client — uses DarkMatter skill instead
        "config_file": "skills/darkmatter/SKILL.md",
        "install": "npm install -g openclaw",
    },
}

_client_name = os.environ.get("DARKMATTER_CLIENT", "claude-code")
if _client_name not in CLIENT_PROFILES:
    import sys as _sys
    print(
        f"[DarkMatter] WARNING: Unknown client profile '{_client_name}', falling back to 'claude-code'. "
        f"Valid profiles: {', '.join(CLIENT_PROFILES.keys())}",
        file=_sys.stderr,
    )
    _client_name = "claude-code"

ACTIVE_CLIENT: dict = dict(CLIENT_PROFILES[_client_name])

# Manual overrides (escape hatches)
_cmd_override = os.environ.get("DARKMATTER_AGENT_COMMAND")
if _cmd_override:
    ACTIVE_CLIENT["command"] = _cmd_override
_args_override = os.environ.get("DARKMATTER_AGENT_ARGS")
if _args_override:
    ACTIVE_CLIENT["args"] = [a.strip() for a in _args_override.split(",") if a.strip()]
_env_cleanup_override = os.environ.get("DARKMATTER_AGENT_ENV_CLEANUP")
if _env_cleanup_override:
    ACTIVE_CLIENT["env_cleanup"] = [v.strip() for v in _env_cleanup_override.split(",") if v.strip()]


def client_has(capability: str) -> bool:
    """Check if the active client profile declares a capability."""
    return capability in ACTIVE_CLIENT.get("capabilities", set())

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

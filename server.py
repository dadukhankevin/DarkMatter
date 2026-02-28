"""
DarkMatter — A Self-Replicating MCP Server for Emergent Agent Networks

A self-replicating MCP server for emergent agent networks.
Agents connect to each other, route messages through the network, and
self-organize based on actual usage patterns.

Core Primitives:
    - Connect: Request a connection to another agent
    - Accept/Reject: Respond to connection requests
    - Disconnect: Sever a connection
    - Message: Send a message with a webhook callback

Everything else — routing, reputation, currency, trust — emerges from
these four primitives and the agents' own intelligence.
"""

import json
import uuid
import time
import asyncio
import fcntl
import os
import random
import shlex
import signal
import sys
import ipaddress
import socket
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.parse import urlparse
from collections import deque
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from cryptography.exceptions import InvalidSignature

import httpx
from mcp.server.fastmcp import FastMCP, Context
from pydantic import BaseModel, Field, ConfigDict

# WebRTC support (optional — gracefully degrades if aiortc is not installed)
try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer, RTCDataChannel
    WEBRTC_AVAILABLE = True
except ImportError:
    WEBRTC_AVAILABLE = False

# UPnP support (optional — enables automatic port forwarding for NAT traversal)
try:
    import miniupnpc
    UPNP_AVAILABLE = True
except ImportError:
    UPNP_AVAILABLE = False

# Solana wallet support (optional — gracefully degrades if solana/solders not installed)
try:
    import hashlib as _hashlib
    from solders.keypair import Keypair as SolanaKeypair
    from solders.pubkey import Pubkey as SolanaPubkey
    from solders.system_program import transfer as sol_transfer, TransferParams as SolTransferParams
    from solders.transaction import VersionedTransaction
    from solders.message import MessageV0
    from solana.rpc.async_api import AsyncClient as SolanaClient
    from spl.token.instructions import transfer_checked, TransferCheckedParams
    from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
    from spl.token.instructions import create_associated_token_account
    SOLANA_AVAILABLE = True
except ImportError:
    SOLANA_AVAILABLE = False

SOLANA_RPC_URL = os.environ.get("DARKMATTER_SOLANA_RPC", "https://api.mainnet-beta.solana.com")
LAMPORTS_PER_SOL = 1_000_000_000

# Well-known SPL tokens (name -> (mint_address, decimals))
SPL_TOKENS = {
    "DM":   ("5DxioZwEeAKpBaYC5veTHArKE55qRDSmb5RZ6VwApump", 6),
    "USDC": ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 6),
    "USDT": ("Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", 6),
}


# =============================================================================
# Configuration
# =============================================================================

DEFAULT_PORT = 8100
MAX_CONNECTIONS = int(os.environ.get("DARKMATTER_MAX_CONNECTIONS", "50"))
MESSAGE_QUEUE_MAX = 50
SENT_MESSAGES_MAX = 100
MAX_CONTENT_LENGTH = 65536   # 64 KB
MAX_BIO_LENGTH = 1000
MAX_AGENT_ID_LENGTH = 128
MAX_URL_LENGTH = 2048

PROTOCOL_VERSION = "0.2"

# WebRTC configuration
WEBRTC_STUN_SERVERS = [{"urls": "stun:stun.l.google.com:19302"}]
WEBRTC_ICE_GATHER_TIMEOUT = 10.0
WEBRTC_CHANNEL_OPEN_TIMEOUT = 15.0
WEBRTC_MESSAGE_SIZE_LIMIT = 16384  # 16 KB — fall back to HTTP for larger messages
DISCOVERY_PORT = 8470
DISCOVERY_MCAST_GROUP = "239.77.68.77"  # "M" "D" "M" in ASCII — DarkMatter multicast group
DISCOVERY_INTERVAL = 30       # seconds between discovery scans
DISCOVERY_MAX_AGE = 90        # seconds before a peer is considered stale
_disc_ports = os.environ.get("DARKMATTER_DISCOVERY_PORTS", "8100-8110")
_disc_lo, _disc_hi = _disc_ports.split("-", 1)
DISCOVERY_LOCAL_PORTS = range(int(_disc_lo), int(_disc_hi) + 1)

# Network resilience configuration
HEALTH_CHECK_INTERVAL = 60          # seconds between health check cycles
HEALTH_FAILURE_THRESHOLD = 3        # failures before logging warning
STALE_CONNECTION_AGE = 300          # seconds of inactivity before health-checking a connection
UPNP_PORT_RANGE = (30000, 60000)    # external port range for UPnP mappings
PEER_LOOKUP_TIMEOUT = 5.0           # seconds to wait for peer_lookup responses
PEER_LOOKUP_MAX_CONCURRENT = 50     # fan out peer_lookup to all connections
IP_CHECK_INTERVAL = 300             # check public IP every 5 min, not every health cycle
WEBHOOK_RECOVERY_MAX_ATTEMPTS = 3   # max peer-lookup recovery attempts per webhook call
WEBHOOK_RECOVERY_TIMEOUT = 30.0     # total wall-clock budget for all recovery attempts (seconds)
ANCHOR_LOOKUP_TIMEOUT = 2.0         # seconds to wait for anchor node responses

# Rate limiting — per-connection and global
DEFAULT_RATE_LIMIT_PER_CONNECTION = 30    # max requests per window per connection (0 = unlimited)
DEFAULT_RATE_LIMIT_GLOBAL = 200           # max total inbound requests per window (0 = unlimited)
RATE_LIMIT_WINDOW = 60                    # sliding window in seconds

# Tool visibility tiers — core tools are always shown, optional tools appear based on state
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

# Anchor nodes — stable directory services for peer lookup fallback
_ANCHOR_DEFAULT = "https://loseylabs.ai"
_anchor_env = os.environ.get("DARKMATTER_ANCHOR_NODES", _ANCHOR_DEFAULT).strip()
ANCHOR_NODES: list[str] = [u.strip().rstrip("/") for u in _anchor_env.split(",") if u.strip()] if _anchor_env else []

# Track last anchor that responded successfully (for failover)
_last_working_anchor: str | None = None

# Gas economy
GAS_RATE = 0.01          # 1% default gas fee
GAS_MAX_HOPS = 10        # TTL for gas signal
GAS_MAX_AGE_S = 300.0    # 5 minute timeout
GAS_LOG_MAX = 100        # cap gas_log entries
SUPERAGENT_DEFAULT_URL = os.environ.get(
    "DARKMATTER_SUPERAGENT",
    ANCHOR_NODES[0] if ANCHOR_NODES else "",
)

# Agent auto-spawn configuration
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

# Entrypoint (human node) auto-start configuration
ENTRYPOINT_AUTOSTART = os.environ.get("DARKMATTER_ENTRYPOINT_AUTOSTART", "true").lower() == "true"
ENTRYPOINT_PORT = int(os.environ.get("DARKMATTER_ENTRYPOINT_PORT", "8200"))
ENTRYPOINT_PATH = os.environ.get("DARKMATTER_ENTRYPOINT_PATH")  # explicit path, or None to search


# =============================================================================
# Input Validation
# =============================================================================

def validate_url(url: str) -> Optional[str]:
    """Validate that a URL uses http or https scheme. Returns error string or None."""
    if len(url) > MAX_URL_LENGTH:
        return f"URL exceeds maximum length ({MAX_URL_LENGTH} chars)."
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL."
    if parsed.scheme not in ("http", "https"):
        return f"URL scheme must be http or https, got '{parsed.scheme}'."
    if not parsed.hostname:
        return "URL has no hostname."
    return None


def is_private_ip(hostname: str) -> bool:
    """Check if a hostname resolves to a private or link-local IP address."""
    try:
        # Try parsing as a literal IP first (avoids DNS lookup)
        addr = ipaddress.ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        pass
    # Resolve hostname to IP
    try:
        info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for family, _, _, _, sockaddr in info:
            ip_str = sockaddr[0]
            addr = ipaddress.ip_address(ip_str)
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                return True
    except socket.gaierror:
        pass
    return False


def _is_darkmatter_webhook(url: str) -> bool:
    """Check if a URL is a known DarkMatter webhook endpoint on a known peer.

    Only returns True if the path matches AND the host:port matches either
    our own agent URL or a connected peer's URL. This prevents an attacker
    from bypassing SSRF protection by hosting /__darkmatter__/webhook/ on
    an arbitrary internal service.
    """
    try:
        parsed = urlparse(url)
        if "/__darkmatter__/webhook/" not in (parsed.path or ""):
            return False

        webhook_origin = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"

        # Check against our own URL
        state = _agent_state
        if state is not None:
            own_url = _get_public_url(state.port)
            own_parsed = urlparse(own_url)
            own_origin = f"{own_parsed.scheme}://{own_parsed.hostname}:{own_parsed.port or (443 if own_parsed.scheme == 'https' else 80)}"
            if webhook_origin == own_origin:
                return True

            # Check against connected peers
            for conn in state.connections.values():
                peer_parsed = urlparse(conn.agent_url)
                peer_origin = f"{peer_parsed.scheme}://{peer_parsed.hostname}:{peer_parsed.port or (443 if peer_parsed.scheme == 'https' else 80)}"
                if webhook_origin == peer_origin:
                    return True

        return False
    except Exception:
        return False


def validate_webhook_url(url: str) -> Optional[str]:
    """Validate a webhook URL: must be http(s) and must NOT target private IPs.

    Exception: DarkMatter webhook URLs (/__darkmatter__/webhook/) are allowed
    to target private IPs, but only if the host matches our own URL or a
    connected peer's URL (prevents SSRF via crafted webhook paths on
    arbitrary internal hosts).
    """
    err = validate_url(url)
    if err:
        return err
    if _is_darkmatter_webhook(url):
        return None  # Known DarkMatter peer — safe to allow private IP
    parsed = urlparse(url)
    if is_private_ip(parsed.hostname):
        return "Webhook URL must not target private or link-local IP addresses."
    return None


def truncate_field(value: str, max_len: int) -> str:
    """Truncate a string to max_len."""
    return value[:max_len] if len(value) > max_len else value


# =============================================================================
# Rate Limiting
# =============================================================================

def _check_rate_limit(state, conn: Optional["Connection"] = None) -> Optional[str]:
    """Check per-connection and global rate limits. Returns error string if exceeded, None if OK.

    Prunes expired timestamps and records the current request if allowed.
    """
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW

    # Global rate limit
    global_limit = state.rate_limit_global or DEFAULT_RATE_LIMIT_GLOBAL
    if global_limit > 0:
        ts = state._global_request_timestamps
        # Prune expired
        while ts and ts[0] < cutoff:
            ts.popleft()
        if len(ts) >= global_limit:
            return f"Global rate limit exceeded ({global_limit} requests per {RATE_LIMIT_WINDOW}s)"

    # Per-connection rate limit
    if conn is not None:
        if conn.rate_limit == -1:
            # Unlimited for this connection
            per_conn_limit = 0
        else:
            per_conn_limit = conn.rate_limit or DEFAULT_RATE_LIMIT_PER_CONNECTION
        if per_conn_limit > 0:
            ts = conn._request_timestamps
            while ts and ts[0] < cutoff:
                ts.popleft()
            if len(ts) >= per_conn_limit:
                return f"Rate limit exceeded for this connection ({per_conn_limit} requests per {RATE_LIMIT_WINDOW}s)"

    # Record the request
    state._global_request_timestamps.append(now)
    if conn is not None:
        conn._request_timestamps.append(now)
    return None


def _get_public_url(port: int) -> str:
    """Get the public URL for this agent.

    Priority: state.public_url (set by _discover_public_url at startup)
    > DARKMATTER_PUBLIC_URL env var > localhost fallback.
    """
    state = _agent_state
    if state is not None and state.public_url:
        return state.public_url
    public_url = os.environ.get("DARKMATTER_PUBLIC_URL", "").rstrip("/")
    if public_url:
        return public_url
    return f"http://localhost:{port}"


async def _discover_public_url(port: int) -> str:
    """Discover the best public URL for this agent.

    Tries in order: env var > UPnP mapping > ipify public IP > localhost fallback.
    """
    # 1. Explicit env var takes priority
    env_url = os.environ.get("DARKMATTER_PUBLIC_URL", "").rstrip("/")
    if env_url:
        print(f"[DarkMatter] Public URL (env): {env_url}", file=sys.stderr)
        return env_url

    # 2. Try UPnP port mapping
    if UPNP_AVAILABLE:
        result = await asyncio.to_thread(_try_upnp_mapping, port)
        if result is not None:
            url, upnp_obj, ext_port = result
            state = _agent_state
            if state is not None:
                state._upnp_mapping = (url, upnp_obj, ext_port)
            print(f"[DarkMatter] Public URL (UPnP): {url}", file=sys.stderr)
            return url

    # 3. Try ipify to get public IP
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://api.ipify.org?format=json")
            if resp.status_code == 200:
                ip = resp.json().get("ip")
                if ip:
                    url = f"http://{ip}:{port}"
                    print(f"[DarkMatter] Public URL (ipify): {url}", file=sys.stderr)
                    return url
    except Exception as e:
        print(f"[DarkMatter] ipify lookup failed: {e}", file=sys.stderr)

    # 4. Localhost fallback
    url = f"http://localhost:{port}"
    print(f"[DarkMatter] Public URL (fallback): {url}", file=sys.stderr)
    return url


async def _check_nat_status(public_url: str) -> bool:
    """Check if we're behind NAT by probing our own public URL.

    Returns True if NAT is detected (self-probe fails).
    """
    if "localhost" in public_url or "127.0.0.1" in public_url:
        return True
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{public_url}/__darkmatter__/status")
            return resp.status_code != 200
    except Exception:
        return True


def _check_nat_status_sync(public_url: str) -> bool:
    """Synchronous version of NAT detection for entrypoint.py."""
    if "localhost" in public_url or "127.0.0.1" in public_url:
        return True
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{public_url}/__darkmatter__/status")
            return resp.status_code != 200
    except Exception:
        return True


def _get_active_anchor() -> str:
    """Return the last known working anchor, or the first configured anchor."""
    if _last_working_anchor and _last_working_anchor in ANCHOR_NODES:
        return _last_working_anchor
    return ANCHOR_NODES[0] if ANCHOR_NODES else ""


def _build_webhook_url(state, message_id: str) -> str:
    """Build the webhook URL, using anchor relay if behind NAT."""
    if state.nat_detected and ANCHOR_NODES:
        anchor = _get_active_anchor()
        return f"{anchor}/__darkmatter__/webhook_relay/{state.agent_id}/{message_id}"
    return f"{_get_public_url(state.port)}/__darkmatter__/webhook/{message_id}"


def _sign_relay_poll(private_key_hex: str, agent_id: str, timestamp: str) -> str:
    """Sign a relay poll request. Returns signature hex."""
    private_bytes = bytes.fromhex(private_key_hex)
    private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    payload = f"relay_poll\n{agent_id}\n{timestamp}".encode("utf-8")
    return private_key.sign(payload).hex()


def _try_upnp_mapping(local_port: int) -> Optional[tuple]:
    """Try to create a UPnP port mapping. Returns (url, upnp_obj, ext_port) or None.

    Runs synchronously — call via asyncio.to_thread().
    """
    import random
    try:
        upnp = miniupnpc.UPnP()
        upnp.discoverdelay = 2000
        devices = upnp.discover()
        if devices == 0:
            return None
        upnp.selectigd()
        external_ip = upnp.externalipaddress()
        if not external_ip:
            return None

        # Try random ports in range, up to 5 attempts
        for _ in range(5):
            ext_port = random.randint(*UPNP_PORT_RANGE)
            try:
                upnp.addportmapping(
                    ext_port, "TCP", upnp.lanaddr, local_port,
                    "DarkMatter mesh", ""
                )
                url = f"http://{external_ip}:{ext_port}"
                return (url, upnp, ext_port)
            except Exception:
                continue  # Port taken, try another

        return None
    except Exception as e:
        print(f"[DarkMatter] UPnP mapping failed: {e}", file=sys.stderr)
        return None


def _cleanup_upnp() -> None:
    """Remove UPnP port mapping on shutdown."""
    state = _agent_state
    if state is None or state._upnp_mapping is None:
        return
    url, upnp_obj, ext_port = state._upnp_mapping
    try:
        upnp_obj.deleteportmapping(ext_port, "TCP")
        print(f"[DarkMatter] UPnP mapping removed (port {ext_port})", file=sys.stderr)
    except Exception as e:
        print(f"[DarkMatter] UPnP cleanup failed: {e}", file=sys.stderr)
    state._upnp_mapping = None


async def _query_anchor_for_peer(anchor_url: str, target_agent_id: str) -> Optional[str]:
    """Query a single anchor node for an agent's URL. Returns URL or None."""
    try:
        async with httpx.AsyncClient(timeout=ANCHOR_LOOKUP_TIMEOUT) as client:
            resp = await client.get(
                f"{anchor_url}/__darkmatter__/peer_lookup/{target_agent_id}"
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("url"):
                    return data["url"]
    except Exception:
        pass
    return None


async def _lookup_peer_url(state, target_agent_id: str, exclude_urls: set[str] | None = None) -> Optional[str]:
    """Find an agent's current URL, querying anchor nodes first, then peer fan-out.

    Args:
        exclude_urls: URLs to skip (e.g. already-tried stale URLs). If an anchor
            or peer returns one of these, it's ignored and the search continues.

    Returns the new URL if found, else None.
    """
    if exclude_urls is None:
        exclude_urls = set()

    # Phase 1: Query anchor nodes (fast, low-overhead)
    if ANCHOR_NODES:
        tasks = [asyncio.create_task(_query_anchor_for_peer(a, target_agent_id)) for a in ANCHOR_NODES]
        try:
            done, pending = await asyncio.wait(tasks, timeout=ANCHOR_LOOKUP_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                result = task.result()
                if result is not None and result not in exclude_urls:
                    for t in pending:
                        t.cancel()
                    return result
            # Wait briefly for remaining anchors
            if pending:
                done2, pending2 = await asyncio.wait(pending, timeout=0.5)
                for task in done2:
                    result = task.result()
                    if result is not None and result not in exclude_urls:
                        for t in pending2:
                            t.cancel()
                        return result
                for t in pending2:
                    t.cancel()
        except Exception:
            for t in tasks:
                t.cancel()

    # Phase 2: Fan out to connected peers (always runs if anchors had no fresh answer)
    peers = [c for c in state.connections.values() if c.agent_id != target_agent_id]
    if not peers:
        return None

    # Limit fan-out
    peers = peers[:PEER_LOOKUP_MAX_CONCURRENT]

    async def _query_peer(conn: Connection) -> Optional[str]:
        try:
            base_url = conn.agent_url.rstrip("/")
            for suffix in ("/mcp", "/__darkmatter__"):
                if base_url.endswith(suffix):
                    base_url = base_url[:-len(suffix)]
                    break
            async with httpx.AsyncClient(timeout=PEER_LOOKUP_TIMEOUT) as client:
                resp = await client.get(
                    f"{base_url}/__darkmatter__/peer_lookup/{target_agent_id}"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("url"):
                        return data["url"]
        except Exception:
            pass
        return None

    tasks = [asyncio.create_task(_query_peer(p)) for p in peers]
    try:
        done, pending = await asyncio.wait(tasks, timeout=PEER_LOOKUP_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            result = task.result()
            if result is not None and result not in exclude_urls:
                # Cancel remaining
                for t in pending:
                    t.cancel()
                return result
        # Wait for remaining with timeout
        if pending:
            done2, pending2 = await asyncio.wait(pending, timeout=1.0)
            for task in done2:
                result = task.result()
                if result is not None and result not in exclude_urls:
                    for t in pending2:
                        t.cancel()
                    return result
            for t in pending2:
                t.cancel()
    except Exception:
        for t in tasks:
            t.cancel()
    return None


async def _webhook_request_with_recovery(
    state, webhook_url: str, from_agent_id: Optional[str],
    method: str = "POST", timeout: float = 30.0, **kwargs
) -> "httpx.Response":
    """Make an HTTP request to a webhook URL, recovering via peer lookup on connection failure.

    On ConnectError/ConnectTimeout, uses _lookup_peer_url to find the sender's
    current URL, reconstructs the webhook with the new base, validates it, and retries.

    Limits:
      - At most WEBHOOK_RECOVERY_MAX_ATTEMPTS recovery attempts (peer lookups)
      - Total wall-clock time capped at WEBHOOK_RECOVERY_TIMEOUT seconds
    """
    deadline = time.monotonic() + WEBHOOK_RECOVERY_TIMEOUT
    current_url = webhook_url
    last_err: Exception | None = None

    # Initial attempt (not counted toward recovery budget)
    try:
        remaining = max(1.0, deadline - time.monotonic())
        async with httpx.AsyncClient(timeout=min(timeout, remaining)) as client:
            return await getattr(client, method.lower())(current_url, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        last_err = e

    # Recovery loop — only entered on connection failure
    if not from_agent_id:
        raise last_err

    urls_tried = {current_url}

    for attempt in range(1, WEBHOOK_RECOVERY_MAX_ATTEMPTS + 1):
        if time.monotonic() >= deadline:
            print(f"[DarkMatter] Webhook recovery: timeout exceeded after {attempt - 1} attempts", file=sys.stderr)
            break

        new_base = await _lookup_peer_url(state, from_agent_id, exclude_urls=urls_tried)
        if not new_base:
            print(f"[DarkMatter] Webhook recovery: peer lookup returned nothing (attempt {attempt}/{WEBHOOK_RECOVERY_MAX_ATTEMPTS})", file=sys.stderr)
            break

        # Reconstruct webhook URL with the new base
        parsed = urlparse(current_url if attempt == 1 else webhook_url)
        path = parsed.path  # e.g. /__darkmatter__/webhook/msg-xxx
        if not (path.startswith("/__darkmatter__/webhook/") or
                path.startswith("/__darkmatter__/webhook_relay/")):
            break

        new_base = new_base.rstrip("/")
        for suffix in ("/mcp", "/__darkmatter__"):
            if new_base.endswith(suffix):
                new_base = new_base[:-len(suffix)]
                break
        new_webhook = f"{new_base}{path}"

        if new_webhook in urls_tried:
            print(f"[DarkMatter] Webhook recovery: peer lookup returned already-tried URL {new_webhook}, giving up", file=sys.stderr)
            break
        urls_tried.add(new_webhook)

        # SSRF protection
        err = validate_webhook_url(new_webhook)
        if err:
            print(f"[DarkMatter] Webhook recovery: new URL failed validation: {err}", file=sys.stderr)
            break

        print(f"[DarkMatter] Webhook recovery: {webhook_url} -> {new_webhook} (attempt {attempt}/{WEBHOOK_RECOVERY_MAX_ATTEMPTS})", file=sys.stderr)

        try:
            remaining = max(1.0, deadline - time.monotonic())
            async with httpx.AsyncClient(timeout=min(timeout, remaining)) as client:
                return await getattr(client, method.lower())(new_webhook, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_err = e
            continue

    raise last_err


def _sign_peer_update(private_key_hex: str, agent_id: str, new_url: str, timestamp: str) -> str:
    """Sign a peer_update payload. Returns signature as hex."""
    private_bytes = bytes.fromhex(private_key_hex)
    private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    payload = f"peer_update\n{agent_id}\n{new_url}\n{timestamp}".encode("utf-8")
    return private_key.sign(payload).hex()


def _verify_peer_update_signature(public_key_hex: str, signature_hex: str,
                                   agent_id: str, new_url: str, timestamp: str) -> bool:
    """Verify a signed peer_update payload. Returns True if valid."""
    try:
        public_bytes = bytes.fromhex(public_key_hex)
        public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
        signature = bytes.fromhex(signature_hex)
        payload = f"peer_update\n{agent_id}\n{new_url}\n{timestamp}".encode("utf-8")
        public_key.verify(signature, payload)
        return True
    except Exception:
        return False


# Max age for peer_update timestamps (prevents replay attacks)
PEER_UPDATE_MAX_AGE = 300  # 5 minutes


def _is_timestamp_fresh(timestamp: str) -> bool:
    """Check if a timestamp is within PEER_UPDATE_MAX_AGE seconds of now."""
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        age = abs((datetime.now(timezone.utc) - ts).total_seconds())
        return age <= PEER_UPDATE_MAX_AGE
    except Exception:
        return False


# Replay dedup: track recently seen message IDs for 5 minutes
# Uses time.time() (wall clock) so entries survive process restarts via persistence.
_REPLAY_WINDOW = 300  # seconds
_REPLAY_MAX_SIZE = 10000
_seen_message_ids: dict[str, float] = {}


def _check_message_replay(message_id: str) -> bool:
    """Return True if this message_id was already seen recently (replay).

    Tracks message IDs for _REPLAY_WINDOW seconds with lazy pruning.
    Uses wall-clock time (time.time()) so entries can be persisted across restarts.
    """
    now = time.time()

    # Lazy prune: evict expired entries when dict gets large
    if len(_seen_message_ids) > _REPLAY_MAX_SIZE:
        cutoff = now - _REPLAY_WINDOW
        expired = [mid for mid, ts in _seen_message_ids.items() if ts < cutoff]
        for mid in expired:
            del _seen_message_ids[mid]

    if message_id in _seen_message_ids:
        ts = _seen_message_ids[message_id]
        if now - ts < _REPLAY_WINDOW:
            return True  # replay detected
        # Expired entry — allow reuse
    _seen_message_ids[message_id] = now
    return False


async def _broadcast_peer_update(state) -> None:
    """Notify all connected peers and anchor nodes of our current URL and bio."""
    if not state.public_url:
        return

    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {
        "agent_id": state.agent_id,
        "new_url": state.public_url,
        "timestamp": timestamp,
        "bio": state.bio,
    }
    if state.public_key_hex:
        payload["public_key_hex"] = state.public_key_hex
    if state.private_key_hex and state.public_key_hex:
        payload["signature"] = _sign_peer_update(
            state.private_key_hex, state.agent_id, state.public_url, timestamp
        )

    for conn in list(state.connections.values()):
        try:
            base_url = conn.agent_url.rstrip("/")
            for suffix in ("/mcp", "/__darkmatter__"):
                if base_url.endswith(suffix):
                    base_url = base_url[:-len(suffix)]
                    break
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{base_url}/__darkmatter__/peer_update",
                    json=payload,
                )
        except Exception as e:
            print(f"[DarkMatter] Failed to notify {conn.agent_id[:12]}... of URL change: {e}", file=sys.stderr)

    # Also notify anchor nodes
    for anchor_url in ANCHOR_NODES:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{anchor_url}/__darkmatter__/peer_update",
                    json=payload,
                )
        except Exception as e:
            print(f"[DarkMatter] Failed to notify anchor {anchor_url} of URL: {e}", file=sys.stderr)


async def _check_connection_health(state) -> None:
    """Check health of all stale connections and update failure counts."""
    for conn in list(state.connections.values()):
        # Only health-check stale connections
        if conn.last_activity:
            try:
                last = datetime.fromisoformat(conn.last_activity.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - last).total_seconds()
                if age < STALE_CONNECTION_AGE:
                    continue
            except Exception:
                pass

        # Ping the peer's status endpoint
        try:
            base_url = conn.agent_url.rstrip("/")
            for suffix in ("/mcp", "/__darkmatter__"):
                if base_url.endswith(suffix):
                    base_url = base_url[:-len(suffix)]
                    break
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/__darkmatter__/status")
                if resp.status_code == 200:
                    conn.health_failures = 0
                    # Auto-upgrade to WebRTC if still on HTTP
                    if conn.transport == "http" and WEBRTC_AVAILABLE:
                        asyncio.create_task(_attempt_webrtc_upgrade(state, conn))
                    continue
        except Exception:
            pass

        conn.health_failures += 1
        if conn.health_failures >= HEALTH_FAILURE_THRESHOLD:
            print(
                f"[DarkMatter] Connection {conn.agent_id[:12]}... unhealthy "
                f"({conn.health_failures} failures, url={conn.agent_url})",
                file=sys.stderr,
            )


async def _network_health_loop(state) -> None:
    """Background task: periodically check connection health and detect IP changes."""
    last_ip_check = 0.0
    last_known_ip = None

    while True:
        try:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            now = time.time()

            # --- IP change detection (every IP_CHECK_INTERVAL) ---
            if now - last_ip_check >= IP_CHECK_INTERVAL:
                last_ip_check = now
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.get("https://api.ipify.org?format=json")
                        if resp.status_code == 200:
                            current_ip = resp.json().get("ip")
                            if last_known_ip is None:
                                last_known_ip = current_ip
                            elif current_ip != last_known_ip:
                                print(f"[DarkMatter] Public IP changed: {last_known_ip} -> {current_ip}", file=sys.stderr)
                                last_known_ip = current_ip
                                state.public_url = await _discover_public_url(state.port)
                                await _broadcast_peer_update(state)
                except Exception:
                    pass  # ipify unreachable — skip this cycle

            # --- Connection health checks ---
            await _check_connection_health(state)

            # --- Webhook relay polling (NAT-ed nodes) ---
            if state.nat_detected and ANCHOR_NODES and state.private_key_hex:
                await _poll_webhook_relay(state)

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[DarkMatter] Health loop error: {e}", file=sys.stderr)


async def _poll_webhook_relay(state) -> None:
    """Poll anchor nodes for buffered webhook callbacks (NAT relay).

    Tries each anchor in order, updates _last_working_anchor on success.
    """
    global _last_working_anchor
    ts = datetime.now(timezone.utc).isoformat()
    sig = _sign_relay_poll(state.private_key_hex, state.agent_id, ts)

    # Try anchors in order: last working first, then the rest
    ordered = list(ANCHOR_NODES)
    if _last_working_anchor and _last_working_anchor in ordered:
        ordered.remove(_last_working_anchor)
        ordered.insert(0, _last_working_anchor)

    for anchor in ordered:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{anchor}/__darkmatter__/webhook_relay_poll/{state.agent_id}",
                    params={"signature": sig, "timestamp": ts},
                )
                if resp.status_code != 200:
                    continue
                _last_working_anchor = anchor
                data = resp.json()
                callbacks = data.get("callbacks", [])
                for cb in callbacks:
                    msg_id = cb.get("message_id", "")
                    cb_data = cb.get("data", {})
                    if msg_id and cb_data:
                        result, _ = _process_webhook_locally(state, msg_id, cb_data)
                        if result.get("success"):
                            print(f"[DarkMatter] Relay: processed webhook for {msg_id}", file=sys.stderr)
                return  # success — don't try other anchors
        except Exception as e:
            print(f"[DarkMatter] Relay poll error ({anchor}): {e}", file=sys.stderr)
            continue


def _process_connection_relay_callback(state, data: dict) -> None:
    """Process a connection_accepted callback received via anchor relay.

    Called from entrypoint.py's relay poll loop. The data dict has the same
    shape as the connection_accepted POST body:
      {agent_id, agent_url, agent_bio, agent_public_key_hex, agent_display_name}
    """
    agent_id = data.get("agent_id", "")
    agent_url = data.get("agent_url", "")
    agent_bio = data.get("agent_bio", "")
    agent_public_key_hex = data.get("agent_public_key_hex")
    agent_display_name = data.get("agent_display_name")

    if not agent_id or not agent_url:
        print("[DarkMatter] Relay callback: missing agent_id or agent_url", file=sys.stderr)
        return
    url_err = validate_url(agent_url)
    if url_err:
        print(f"[DarkMatter] Relay callback: invalid URL: {url_err}", file=sys.stderr)
        return

    # Match against pending outbound requests (same logic as handle_connection_accepted)
    agent_base = agent_url.rstrip("/").rsplit("/mcp", 1)[0].rstrip("/")
    matched = None
    for pending_url in state.pending_outbound:
        pending_base = pending_url.rsplit("/mcp", 1)[0].rstrip("/")
        if pending_base == agent_base:
            matched = pending_url
            break

    if matched is None and agent_id:
        for pending_url, pending_agent_id in state.pending_outbound.items():
            if pending_agent_id == agent_id:
                matched = pending_url
                break

    if matched is None:
        print(f"[DarkMatter] Relay callback: no pending outbound for {agent_id[:12]}...", file=sys.stderr)
        return

    del state.pending_outbound[matched]

    conn = Connection(
        agent_id=agent_id,
        agent_url=agent_url,
        agent_bio=agent_bio,
        agent_public_key_hex=agent_public_key_hex,
        agent_display_name=agent_display_name,
    )
    state.connections[agent_id] = conn
    save_state()
    print(f"[DarkMatter] Relay callback: connection established with {agent_id[:12]}...", file=sys.stderr)


# =============================================================================
# Cryptographic Identity — Ed25519 keypair, signing, verification
# =============================================================================

def _generate_keypair() -> tuple[str, str]:
    """Generate an Ed25519 keypair. Returns (private_key_hex, public_key_hex)."""
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private_bytes.hex(), public_bytes.hex()


def _derive_public_key_hex(private_key_hex: str) -> str:
    """Derive the public key hex from a private key hex."""
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _load_or_create_passport() -> tuple[str, str]:
    """Load or create the passport (Ed25519 keypair) from the working directory.

    The passport file lives at .darkmatter/passport.key in the current working
    directory. It contains the private key hex. The agent's identity (agent_id)
    is derived from the public key — same passport always produces the same identity.

    Returns (private_key_hex, public_key_hex).
    """
    passport_dir = os.path.join(os.getcwd(), ".darkmatter")
    passport_path = os.path.join(passport_dir, "passport.key")

    if os.path.exists(passport_path):
        with open(passport_path, "r") as f:
            private_key_hex = f.read().strip()
        public_key_hex = _derive_public_key_hex(private_key_hex)
        print(f"[DarkMatter] Passport loaded: {passport_path}", file=sys.stderr)
        print(f"[DarkMatter] Agent ID (public key): {public_key_hex}", file=sys.stderr)
        return private_key_hex, public_key_hex

    # Generate new passport
    private_key_hex, public_key_hex = _generate_keypair()
    os.makedirs(passport_dir, exist_ok=True)
    with open(passport_path, "w") as f:
        f.write(private_key_hex + "\n")
    os.chmod(passport_path, 0o600)  # Owner read/write only
    print(f"[DarkMatter] New passport created: {passport_path}", file=sys.stderr)
    print(f"[DarkMatter] Agent ID (public key): {public_key_hex}", file=sys.stderr)
    return private_key_hex, public_key_hex


def _sign_message(private_key_hex: str, from_agent_id: str, message_id: str,
                  timestamp: str, content: str) -> str:
    """Sign a canonical message payload. Returns signature as hex."""
    private_bytes = bytes.fromhex(private_key_hex)
    private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    payload = f"{from_agent_id}\n{message_id}\n{timestamp}\n{content}".encode("utf-8")
    signature = private_key.sign(payload)
    return signature.hex()


def _verify_message(public_key_hex: str, signature_hex: str, from_agent_id: str,
                    message_id: str, timestamp: str, content: str) -> bool:
    """Verify a signed message payload. Returns True if valid."""
    try:
        public_bytes = bytes.fromhex(public_key_hex)
        public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
        signature = bytes.fromhex(signature_hex)
        payload = f"{from_agent_id}\n{message_id}\n{timestamp}\n{content}".encode("utf-8")
        public_key.verify(signature, payload)
        return True
    except Exception:
        return False


# --- Solana wallet derivation (domain-separated from passport key) ---

def _derive_solana_keypair(private_key_hex: str) -> "SolanaKeypair":
    """Derive a Solana keypair from the passport private key with domain separation."""
    seed = _hashlib.sha256(bytes.fromhex(private_key_hex) + b"darkmatter-solana-v1").digest()
    return SolanaKeypair.from_seed(seed)

def _get_solana_wallet_address(private_key_hex: str) -> str:
    """Get the Solana wallet address (base58 public key) for this agent."""
    return str(_derive_solana_keypair(private_key_hex).pubkey())


def _resolve_spl_token(token_or_mint: str) -> tuple[str, int] | None:
    """Resolve a token name or mint address. Returns (mint, decimals) or None."""
    upper = token_or_mint.upper()
    if upper in SPL_TOKENS:
        return SPL_TOKENS[upper]
    # Check if it looks like a mint address (base58, 32-44 chars)
    if len(token_or_mint) >= 32:
        return None  # caller should treat as raw mint
    return None


async def get_solana_balance(wallets: dict, mint: str = None) -> dict:
    """Get SOL or SPL token balance. Reusable by MCP tools and entrypoint."""
    sol_addr = wallets.get("solana")
    if not SOLANA_AVAILABLE or not sol_addr:
        return {"success": False, "error": "Solana wallet not available"}

    pubkey = SolanaPubkey.from_string(sol_addr)

    try:
        async with SolanaClient(SOLANA_RPC_URL) as client:
            if mint is None:
                resp = await client.get_balance(pubkey)
                lamports = resp.value
                return {
                    "success": True,
                    "token": "SOL",
                    "balance": lamports / LAMPORTS_PER_SOL,
                    "lamports": lamports,
                    "wallet_address": sol_addr,
                }
            else:
                mint_pubkey = SolanaPubkey.from_string(mint)
                ata = SolanaPubkey.find_program_address(
                    [bytes(pubkey), bytes(TOKEN_PROGRAM_ID), bytes(mint_pubkey)],
                    SolanaPubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"),
                )[0]
                resp = await client.get_token_account_balance(ata)
                if resp.value is None:
                    return {
                        "success": True,
                        "token": mint,
                        "balance": 0,
                        "wallet_address": sol_addr,
                        "note": "No token account found",
                    }
                return {
                    "success": True,
                    "token": mint,
                    "balance": float(resp.value.ui_amount_string),
                    "decimals": resp.value.decimals,
                    "wallet_address": sol_addr,
                }
    except Exception as e:
        err_str = str(e)
        # "could not find account" = token account doesn't exist = balance is 0
        if "could not find account" in err_str.lower() or "invalid param" in err_str.lower():
            return {
                "success": True,
                "token": mint or "SOL",
                "balance": 0,
                "wallet_address": sol_addr,
                "note": "No token account found",
            }
        return {"success": False, "error": f"RPC error: {err_str}"}


async def send_solana_sol(private_key_hex: str, wallets: dict, recipient_wallet: str, amount: float) -> dict:
    """Send SOL to a recipient wallet. Reusable by MCP tools and entrypoint."""
    sol_addr = wallets.get("solana")
    if not SOLANA_AVAILABLE or not sol_addr:
        return {"success": False, "error": "Solana wallet not available"}

    sender_kp = _derive_solana_keypair(private_key_hex)
    sender_pubkey = sender_kp.pubkey()
    recipient_pubkey = SolanaPubkey.from_string(recipient_wallet)
    lamports = int(amount * LAMPORTS_PER_SOL)

    try:
        async with SolanaClient(SOLANA_RPC_URL) as client:
            ix = sol_transfer(SolTransferParams(
                from_pubkey=sender_pubkey,
                to_pubkey=recipient_pubkey,
                lamports=lamports,
            ))
            bh_resp = await client.get_latest_blockhash()
            blockhash = bh_resp.value.blockhash
            msg = MessageV0.try_compile(
                payer=sender_pubkey,
                instructions=[ix],
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [sender_kp])
            tx_resp = await client.send_transaction(tx)
            tx_signature = str(tx_resp.value)

        return {
            "success": True,
            "tx_signature": tx_signature,
            "amount": amount,
            "from_wallet": str(sender_pubkey),
            "to_wallet": recipient_wallet,
        }
    except Exception as e:
        return {"success": False, "error": f"Transaction failed: {str(e)}"}


async def send_solana_token(private_key_hex: str, wallets: dict, recipient_wallet: str,
                            mint: str, amount: float, decimals: int) -> dict:
    """Send SPL tokens to a recipient wallet. Reusable by MCP tools and entrypoint."""
    sol_addr = wallets.get("solana")
    if not SOLANA_AVAILABLE or not sol_addr:
        return {"success": False, "error": "Solana wallet not available"}

    sender_kp = _derive_solana_keypair(private_key_hex)
    sender_pubkey = sender_kp.pubkey()
    recipient_pubkey = SolanaPubkey.from_string(recipient_wallet)
    mint_pubkey = SolanaPubkey.from_string(mint)

    ata_program = SolanaPubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
    sender_ata = SolanaPubkey.find_program_address(
        [bytes(sender_pubkey), bytes(TOKEN_PROGRAM_ID), bytes(mint_pubkey)],
        ata_program,
    )[0]
    recipient_ata = SolanaPubkey.find_program_address(
        [bytes(recipient_pubkey), bytes(TOKEN_PROGRAM_ID), bytes(mint_pubkey)],
        ata_program,
    )[0]

    raw_amount = int(amount * (10 ** decimals))

    try:
        async with SolanaClient(SOLANA_RPC_URL) as client:
            instructions = []
            created_ata = False

            ata_info = await client.get_account_info(recipient_ata)
            if ata_info.value is None:
                create_ata_ix = create_associated_token_account(
                    payer=sender_pubkey,
                    owner=recipient_pubkey,
                    mint=mint_pubkey,
                )
                instructions.append(create_ata_ix)
                created_ata = True

            instructions.append(transfer_checked(TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=sender_ata,
                mint=mint_pubkey,
                dest=recipient_ata,
                owner=sender_pubkey,
                amount=raw_amount,
                decimals=decimals,
            )))

            bh_resp = await client.get_latest_blockhash()
            blockhash = bh_resp.value.blockhash
            msg = MessageV0.try_compile(
                payer=sender_pubkey,
                instructions=instructions,
                address_lookup_table_accounts=[],
                recent_blockhash=blockhash,
            )
            tx = VersionedTransaction(msg, [sender_kp])
            tx_resp = await client.send_transaction(tx)
            tx_signature = str(tx_resp.value)

        return {
            "success": True,
            "tx_signature": tx_signature,
            "amount": amount,
            "token_mint": mint,
            "decimals": decimals,
            "from_wallet": str(sender_pubkey),
            "to_wallet": recipient_wallet,
            "created_recipient_ata": created_ata,
        }
    except Exception as e:
        return {"success": False, "error": f"Transaction failed: {str(e)}"}


# =============================================================================
# Gas Economy Engine
# =============================================================================

# Cache for superagent wallet resolution
_superagent_wallet_cache: dict[str, tuple[str, float]] = {}  # url -> (wallet, timestamp)
_SUPERAGENT_CACHE_TTL = 300.0  # 5 minutes


async def _get_superagent_wallet(state) -> Optional[str]:
    """Resolve the superagent URL to a Solana wallet address, with caching."""
    url = state.superagent_url or SUPERAGENT_DEFAULT_URL
    if not url:
        return None

    # Check cache
    cached = _superagent_wallet_cache.get(url)
    if cached and time.time() - cached[1] < _SUPERAGENT_CACHE_TTL:
        return cached[0]

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url.rstrip("/") + "/__darkmatter__/network_info")
            if resp.status_code == 200:
                data = resp.json()
                wallets = data.get("wallets", {})
                sol_wallet = wallets.get("solana")
                if sol_wallet:
                    _superagent_wallet_cache[url] = (sol_wallet, time.time())
                    return sol_wallet
    except Exception:
        pass

    return None


def _select_elder(state, gas: 'GasSignal') -> Optional['Connection']:
    """Select an elder (older peer with positive trust) for gas routing.

    Weighted random selection by age_seconds * trust_score.
    Excludes agents already in gas.path (loop prevention).
    """
    now = datetime.now(timezone.utc)
    candidates = []

    for aid, conn in state.connections.items():
        # Must have peer_created_at and it must be older than us
        if not conn.peer_created_at or not state.created_at:
            continue
        if conn.peer_created_at >= state.created_at:
            continue
        # Must have positive trust
        imp = state.impressions.get(aid, Impression(score=0.0))
        if imp.score <= 0:
            continue
        # Must not already be in the gas path
        if aid in gas.path:
            continue
        # Must have a wallet
        if not conn.wallets.get("solana"):
            continue

        # Calculate weight: age in seconds * trust score
        try:
            peer_dt = datetime.fromisoformat(conn.peer_created_at)
            age_s = max(1.0, (now - peer_dt).total_seconds())
        except (ValueError, TypeError):
            age_s = 1.0

        weight = age_s * imp.score
        candidates.append((conn, weight))

    if not candidates:
        return None

    # Weighted random selection
    total = sum(w for _, w in candidates)
    r = random.random() * total
    cumulative = 0.0
    for conn, weight in candidates:
        cumulative += weight
        if r <= cumulative:
            return conn
    return candidates[-1][0]


def _gas_signal_to_dict(gas: 'GasSignal') -> dict:
    """Serialize a GasSignal for network transmission."""
    return {
        "signal_id": gas.signal_id,
        "original_tx": gas.original_tx,
        "sender_agent_id": gas.sender_agent_id,
        "amount": gas.amount,
        "token": gas.token,
        "token_decimals": gas.token_decimals,
        "sender_superagent_wallet": gas.sender_superagent_wallet,
        "callback_url": gas.callback_url,
        "hops": gas.hops,
        "max_hops": gas.max_hops,
        "created_at": gas.created_at,
        "path": gas.path,
    }


def _gas_signal_from_dict(d: dict) -> 'GasSignal':
    """Deserialize a GasSignal from network payload."""
    return GasSignal(
        signal_id=d["signal_id"],
        original_tx=d["original_tx"],
        sender_agent_id=d["sender_agent_id"],
        amount=d["amount"],
        token=d["token"],
        token_decimals=d.get("token_decimals", 9),
        sender_superagent_wallet=d.get("sender_superagent_wallet", ""),
        callback_url=d["callback_url"],
        hops=d.get("hops", 0),
        max_hops=d.get("max_hops", GAS_MAX_HOPS),
        created_at=d.get("created_at", ""),
        path=d.get("path", []),
    )


def _log_gas_event(state, event: dict) -> None:
    """Append a gas event to state.gas_log, capping at GAS_LOG_MAX."""
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    state.gas_log.append(event)
    if len(state.gas_log) > GAS_LOG_MAX:
        state.gas_log = state.gas_log[-GAS_LOG_MAX:]


async def _run_match_game(state, gas: 'GasSignal', is_originator: bool = True) -> None:
    """Run the match game for gas routing.

    If is_originator is True, this node (B) holds the gas and will send it at resolution.
    If is_originator is False, this node received a forwarded signal and will
    POST the resolution back to B's callback_url.
    """
    # Check TTL
    if gas.hops >= gas.max_hops:
        await _resolve_gas(state, gas, "timeout", None, is_originator)
        return

    # Check age
    if gas.created_at:
        try:
            created = datetime.fromisoformat(gas.created_at)
            age = (datetime.now(timezone.utc) - created).total_seconds()
            if age > GAS_MAX_AGE_S:
                await _resolve_gas(state, gas, "timeout", None, is_originator)
                return
        except (ValueError, TypeError):
            pass

    # Get eligible peers for the match game (connected, not in path)
    peers = [
        conn for aid, conn in state.connections.items()
        if aid not in gas.path and conn.wallets.get("solana")
    ]

    n = len(peers)
    if n == 0:
        # Terminal node — no peers to play with
        await _resolve_gas(state, gas, "terminal", None, is_originator)
        return

    # Pick our number
    my_number = random.randint(0, n)

    # Query each peer for their pick
    async def _query_peer_pick(conn, n_val):
        try:
            base = conn.agent_url.rstrip("/").rsplit("/mcp", 1)[0].rstrip("/")
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(
                    base + "/__darkmatter__/gas_match",
                    json={"signal_id": gas.signal_id, "n": n_val},
                )
                if resp.status_code == 200:
                    return resp.json().get("pick")
        except Exception:
            pass
        return None

    tasks = [_query_peer_pick(conn, n) for conn in peers]
    results = await asyncio.gather(*tasks)

    # Check for match
    matched = any(pick == my_number for pick in results if pick is not None)

    if matched:
        # Match! Select an elder and resolve
        elder = _select_elder(state, gas)
        if elder:
            dest_wallet = elder.wallets.get("solana", "")
            await _resolve_gas(state, gas, "match", dest_wallet, is_originator, resolved_by=elder.agent_id)
        else:
            # No elder — terminal, current node keeps gas
            await _resolve_gas(state, gas, "terminal", None, is_originator)
    else:
        # No match — select an elder to forward the signal to
        elder = _select_elder(state, gas)
        if elder:
            # Forward signal to elder
            gas.hops += 1
            gas.path.append(state.agent_id)
            forwarded_gas = _gas_signal_to_dict(gas)

            try:
                base = elder.agent_url.rstrip("/").rsplit("/mcp", 1)[0].rstrip("/")
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(
                        base + "/__darkmatter__/gas_signal",
                        json=forwarded_gas,
                    )
                    if resp.status_code == 200:
                        _log_gas_event(state, {
                            "type": "forwarded",
                            "signal_id": gas.signal_id,
                            "forwarded_to": elder.agent_id,
                            "hops": gas.hops,
                        })
                        return  # Elder will handle resolution
            except Exception:
                pass

            # Forward failed — terminal
            await _resolve_gas(state, gas, "terminal", None, is_originator)
        else:
            # No elder — terminal, current node keeps gas
            await _resolve_gas(state, gas, "terminal", None, is_originator)


async def _resolve_gas(state, gas: 'GasSignal', resolution: str,
                       dest_wallet: Optional[str], is_originator: bool,
                       resolved_by: str = "") -> None:
    """Resolve a gas signal — either send gas (if originator) or notify B's callback."""

    if resolution == "timeout":
        dest_wallet = gas.sender_superagent_wallet or None

    if is_originator:
        # We are B — send the gas
        if dest_wallet and state.private_key_hex:
            try:
                if gas.token == "SOL":
                    result = await send_solana_sol(
                        state.private_key_hex, state.wallets, dest_wallet, gas.amount
                    )
                else:
                    result = await send_solana_token(
                        state.private_key_hex, state.wallets, dest_wallet,
                        gas.token, gas.amount, gas.token_decimals
                    )

                _log_gas_event(state, {
                    "type": "gas_sent",
                    "signal_id": gas.signal_id,
                    "resolution": resolution,
                    "destination": dest_wallet,
                    "amount": gas.amount,
                    "token": gas.token,
                    "tx_success": result.get("success", False),
                    "tx_signature": result.get("tx_signature"),
                    "resolved_by": resolved_by,
                })

                # Trust boost: +0.01 for sender A (completed a legitimate transaction)
                if result.get("success"):
                    _adjust_trust(state, gas.sender_agent_id, 0.01)
                    if resolved_by:
                        _adjust_trust(state, resolved_by, 0.01)
            except Exception as e:
                _log_gas_event(state, {
                    "type": "gas_send_failed",
                    "signal_id": gas.signal_id,
                    "error": str(e),
                })
        elif resolution == "terminal":
            # No destination — keep the gas
            _log_gas_event(state, {
                "type": "gas_kept",
                "signal_id": gas.signal_id,
                "reason": "terminal_node",
                "amount": gas.amount,
            })

        save_state()
    else:
        # We are a forwarding node — POST result back to B's callback
        try:
            payload = {
                "signal_id": gas.signal_id,
                "destination_wallet": dest_wallet or "",
                "resolved_by": resolved_by or state.agent_id,
                "resolution": resolution,
            }
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(gas.callback_url, json=payload)

            _log_gas_event(state, {
                "type": "gas_resolved_callback",
                "signal_id": gas.signal_id,
                "resolution": resolution,
                "destination": dest_wallet,
            })
        except Exception as e:
            _log_gas_event(state, {
                "type": "gas_callback_failed",
                "signal_id": gas.signal_id,
                "error": str(e),
            })
        save_state()


def _adjust_trust(state, agent_id: str, delta: float) -> None:
    """Adjust trust score for an agent by delta, clamped to [-1, 1]."""
    imp = state.impressions.get(agent_id, Impression(score=0.0))
    new_score = max(-1.0, min(1.0, imp.score + delta))
    state.impressions[agent_id] = Impression(score=round(new_score, 4), note=imp.note)


async def _initiate_gas_from_payment(state, msg: 'QueuedMessage') -> None:
    """B receives a payment from A with gas_eligible flag. Calculate gas and start match game."""
    meta = msg.metadata or {}
    amount = meta.get("amount", 0)
    gas_rate = meta.get("gas_rate", GAS_RATE)
    gas_amount = amount * gas_rate

    if gas_amount <= 0:
        return

    token = meta.get("token", "SOL")
    token_decimals = meta.get("decimals", 9) if token != "SOL" else 9
    tx_signature = meta.get("tx_signature", "")
    sender_superagent_wallet = meta.get("sender_superagent_wallet", "")

    signal_id = f"gas-{uuid.uuid4().hex[:12]}"
    callback_url = f"{_get_public_url(state.port)}/__darkmatter__/gas_result"

    gas = GasSignal(
        signal_id=signal_id,
        original_tx=tx_signature,
        sender_agent_id=msg.from_agent_id or "",
        amount=gas_amount,
        token=token,
        token_decimals=token_decimals,
        sender_superagent_wallet=sender_superagent_wallet,
        callback_url=callback_url,
        created_at=datetime.now(timezone.utc).isoformat(),
        path=[],
    )

    _log_gas_event(state, {
        "type": "gas_initiated",
        "signal_id": signal_id,
        "original_tx": tx_signature,
        "amount": gas_amount,
        "token": token,
        "token_decimals": token_decimals,
        "sender_agent_id": msg.from_agent_id,
        "sender_superagent_wallet": sender_superagent_wallet,
    })
    save_state()

    # Start match game (B is the originator)
    asyncio.create_task(_run_match_game(state, gas, is_originator=True))

    # Start timeout watchdog
    asyncio.create_task(_gas_timeout_watchdog(state, gas))


async def _gas_timeout_watchdog(state, gas: 'GasSignal') -> None:
    """Watchdog: if B doesn't receive a gas_result within GAS_MAX_AGE_S, penalize."""
    await asyncio.sleep(GAS_MAX_AGE_S + 5)  # grace period

    # Check if gas was already resolved
    for entry in state.gas_log:
        if entry.get("signal_id") == gas.signal_id and entry.get("type") in ("gas_sent", "gas_kept"):
            return  # Already resolved

    # Timeout — send gas to superagent wallet as fallback
    _log_gas_event(state, {
        "type": "gas_timeout",
        "signal_id": gas.signal_id,
    })

    # Send to superagent wallet if available
    if gas.sender_superagent_wallet and state.private_key_hex:
        try:
            if gas.token == "SOL":
                await send_solana_sol(
                    state.private_key_hex, state.wallets,
                    gas.sender_superagent_wallet, gas.amount
                )
            else:
                await send_solana_token(
                    state.private_key_hex, state.wallets,
                    gas.sender_superagent_wallet, gas.token,
                    gas.amount, gas.token_decimals
                )
        except Exception:
            pass

    save_state()


# =============================================================================
# Data Models (in-memory state)
# =============================================================================


class AgentStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


@dataclass
class Connection:
    """A connection to another agent in the mesh."""
    agent_id: str
    agent_url: str
    agent_bio: str
    connected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Local telemetry — tracked by this agent, not part of the protocol
    messages_sent: int = 0
    messages_received: int = 0
    messages_declined: int = 0
    total_response_time_ms: float = 0.0
    last_activity: Optional[str] = None
    # Cryptographic identity — peer's public key and display name
    agent_public_key_hex: Optional[str] = None
    agent_display_name: Optional[str] = None
    # Wallets (chain -> address, exchanged during handshake)
    wallets: dict[str, str] = field(default_factory=dict)
    # Peer's passport creation time (for elder selection in gas economy)
    peer_created_at: Optional[str] = None
    # Per-connection rate limit (0 = use global default, -1 = unlimited)
    rate_limit: int = 0
    # Rate limit tracking (ephemeral — never persisted)
    _request_timestamps: deque = field(default_factory=deque)
    # Network resilience (ephemeral — never persisted)
    health_failures: int = 0
    # WebRTC transport state (ephemeral — never persisted)
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
    note: str = ""     # optional freeform context


@dataclass
class GasSignal:
    """A gas fee signal routed through the network via the match game."""
    signal_id: str
    original_tx: str            # tx_signature that generated this gas
    sender_agent_id: str        # A (who sent the payment)
    amount: float               # gas amount (1% of original)
    token: str                  # "SOL" or mint address
    token_decimals: int         # 9 for SOL, 6 for USDC, etc.
    sender_superagent_wallet: str  # where gas goes on timeout
    callback_url: str           # B's /__darkmatter__/gas_result endpoint
    hops: int = 0
    max_hops: int = GAS_MAX_HOPS
    created_at: str = ""
    path: list[str] = field(default_factory=list)  # agent_ids visited (loop prevention)


@dataclass
class PendingConnectionRequest:
    """An incoming connection request awaiting acceptance."""
    request_id: str
    from_agent_id: str
    from_agent_url: str
    from_agent_bio: str
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    # Cryptographic identity — requester's public key and display name
    from_agent_public_key_hex: Optional[str] = None
    from_agent_display_name: Optional[str] = None
    # Wallets (chain -> address)
    from_agent_wallets: dict[str, str] = field(default_factory=dict)
    # Peer's passport creation time (for elder selection in gas economy)
    from_agent_created_at: Optional[str] = None
    # Auto-aggregated peer trust (populated at request time)
    peer_trust: Optional[dict] = None
    # Bidirectional connection request (requester wants mutual connection)
    mutual: bool = False


@dataclass
class QueuedMessage:
    """A message waiting to be processed (simplified — no routing data)."""
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
    updates: list[dict] = field(default_factory=list)  # [{type, agent_id, target_agent_id, note, timestamp}, ...]
    responses: list[dict] = field(default_factory=list)  # [{agent_id, response, metadata, timestamp}, ...]


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
    impressions: dict[str, Impression] = field(default_factory=dict)  # agent_id -> Impression
    # Cryptographic identity — Ed25519 keypair and human-friendly display name
    private_key_hex: Optional[str] = None
    public_key_hex: Optional[str] = None
    display_name: Optional[str] = None
    # Wallets (chain -> address, derived from passport, not persisted)
    wallets: dict[str, str] = field(default_factory=dict)
    # Track outbound connection requests: url → agent_id (not persisted)
    pending_outbound: dict[str, str] = field(default_factory=dict)
    # LAN-discovered peers (ephemeral, not persisted)
    discovered_peers: dict[str, dict] = field(default_factory=dict)
    # Rate limiting (global)
    rate_limit_global: int = 0             # 0 = use DEFAULT_RATE_LIMIT_GLOBAL
    _global_request_timestamps: deque = field(default_factory=deque)
    # Network resilience (ephemeral — never persisted)
    public_url: Optional[str] = None
    _upnp_mapping: Optional[tuple] = None  # (url, upnp_obj, ext_port)
    # Auto-reactivation timer for inactive status
    inactive_until: Optional[str] = None  # ISO timestamp; when expired, auto-flip to active
    # Response waiters — message_id -> list of asyncio.Event (ephemeral, not persisted)
    _response_events: dict[str, list[asyncio.Event]] = field(default_factory=dict)
    # Extensible message routing
    routing_rules: list = field(default_factory=list)  # list of RoutingRule dicts
    router_mode: str = "spawn"  # spawn | rules_first | rules_only | queue_only
    # NAT detection (ephemeral — re-detected each startup)
    nat_detected: bool = False
    # Gas economy
    superagent_url: Optional[str] = None  # pointer to default superagent, falls back to SUPERAGENT_DEFAULT_URL
    gas_log: list[dict] = field(default_factory=list)  # recent gas events, capped at GAS_LOG_MAX


# =============================================================================
# Extensible Message Router
# =============================================================================

class RouterAction(str, Enum):
    """Actions a router can take on an incoming message."""
    HANDLE = "handle"      # Keep in queue; spawn agent if in spawn mode
    FORWARD = "forward"    # Auto-forward to specified peer(s)
    RESPOND = "respond"    # Send immediate response via webhook
    DROP = "drop"          # Remove from queue silently
    PASS = "pass"          # No opinion — try next router in chain


@dataclass
class RouterDecision:
    """Result of a router evaluating a message."""
    action: RouterAction
    forward_to: list[str] = field(default_factory=list)  # agent IDs for FORWARD
    response: Optional[str] = None   # response text for RESPOND
    reason: Optional[str] = None     # human-readable explanation


@dataclass
class RoutingRule:
    """A declarative routing rule configured via MCP tools."""
    rule_id: str
    action: str  # RouterAction value
    priority: int = 0
    enabled: bool = True
    # Match conditions (AND logic — all specified conditions must match)
    keyword: Optional[str] = None          # substring match on content
    from_agent_id: Optional[str] = None    # exact match on sender
    metadata_key: Optional[str] = None     # metadata key must exist
    metadata_value: Optional[str] = None   # metadata value must equal (requires metadata_key)
    # Action parameters
    forward_to: list[str] = field(default_factory=list)  # for FORWARD action
    response_text: Optional[str] = None    # for RESPOND action


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


def _routing_rule_from_dict(d: dict) -> RoutingRule:
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


# --- Router functions ---

def _rule_matches(rule: RoutingRule, msg: QueuedMessage) -> bool:
    """Check if a rule matches a message. All specified conditions must match (AND logic)."""
    if rule.keyword is not None:
        if rule.keyword.lower() not in msg.content.lower():
            return False
    if rule.from_agent_id is not None:
        if msg.from_agent_id != rule.from_agent_id:
            return False
    if rule.metadata_key is not None:
        if rule.metadata_key not in (msg.metadata or {}):
            return False
        if rule.metadata_value is not None:
            if str((msg.metadata or {}).get(rule.metadata_key, "")) != rule.metadata_value:
                return False
    return True


def _rule_router(state: AgentState, msg: QueuedMessage) -> RouterDecision:
    """Evaluate routing rules in priority order. First match wins."""
    rules = [r for r in state.routing_rules if r.enabled]
    rules.sort(key=lambda r: r.priority, reverse=True)
    for rule in rules:
        if _rule_matches(rule, msg):
            action = RouterAction(rule.action)
            return RouterDecision(
                action=action,
                forward_to=rule.forward_to if action == RouterAction.FORWARD else [],
                response=rule.response_text if action == RouterAction.RESPOND else None,
                reason=f"Matched rule '{rule.rule_id}'",
            )
    return RouterDecision(action=RouterAction.PASS, reason="No rules matched")


def _spawn_router(state: AgentState, msg: QueuedMessage) -> RouterDecision:
    """Default router: always HANDLE (triggers agent spawn)."""
    return RouterDecision(action=RouterAction.HANDLE, reason="Spawn mode — handling message")


def _queue_router(state: AgentState, msg: QueuedMessage) -> RouterDecision:
    """Queue-only router: always HANDLE but without spawn."""
    return RouterDecision(action=RouterAction.HANDLE, reason="Queue mode — message queued for manual handling")


# --- Router chain ---

_ROUTER_CHAINS: dict[str, list] = {
    "spawn": [_rule_router, _spawn_router],
    "rules_first": [_rule_router, _queue_router],
    "rules_only": [_rule_router],
    "queue_only": [_queue_router],
}

VALID_ROUTER_MODES = set(_ROUTER_CHAINS.keys())

# Custom router hook — set via set_custom_router()
_custom_router = None


def set_custom_router(fn) -> None:
    """Register a custom router callable.

    The callable signature must be: (AgentState, QueuedMessage) -> RouterDecision
    It runs before the built-in chain. Return PASS to defer to built-in routers.
    Pass None to remove a custom router.
    """
    global _custom_router
    _custom_router = fn


def _get_router_chain(mode: str) -> list:
    """Return the router function list for the given mode."""
    chain = _ROUTER_CHAINS.get(mode, _ROUTER_CHAINS["spawn"])
    if _custom_router is not None:
        return [_custom_router] + chain
    return chain


async def _execute_decision(state: AgentState, msg: QueuedMessage, decision: RouterDecision) -> None:
    """Execute a routing decision on a message."""
    if decision.action == RouterAction.HANDLE:
        # Keep in queue; spawn agent if mode is "spawn" and spawning is enabled
        if state.router_mode == "spawn" and AGENT_SPAWN_ENABLED:
            asyncio.create_task(_spawn_agent_for_message(state, msg))
        # Otherwise, message stays in queue for manual handling

    elif decision.action == RouterAction.FORWARD:
        if not decision.forward_to:
            print(f"[DarkMatter] Router FORWARD decision but no targets — keeping in queue", file=sys.stderr)
            return
        # Build a synthetic SendMessageInput for _forward_message
        fwd_params = SendMessageInput(
            message_id=msg.message_id,
            target_agent_ids=decision.forward_to if len(decision.forward_to) > 1 else None,
            target_agent_id=decision.forward_to[0] if len(decision.forward_to) == 1 else None,
            note=f"Auto-forwarded by router: {decision.reason}",
        )
        result_json = await _forward_message(state, fwd_params)
        result = json.loads(result_json)
        if not result.get("success"):
            print(f"[DarkMatter] Router auto-forward failed: {result.get('error', 'unknown')}", file=sys.stderr)

    elif decision.action == RouterAction.RESPOND:
        if not decision.response:
            print(f"[DarkMatter] Router RESPOND decision but no response text — keeping in queue", file=sys.stderr)
            return
        # Find and remove from queue, send webhook response
        for i, m in enumerate(state.message_queue):
            if m.message_id == msg.message_id:
                state.message_queue.pop(i)
                break
        webhook_err = validate_webhook_url(msg.webhook)
        if not webhook_err:
            resp_timestamp = datetime.now(timezone.utc).isoformat()
            resp_signature_hex = None
            if state.private_key_hex:
                resp_signature_hex = _sign_message(
                    state.private_key_hex, state.agent_id, msg.message_id, resp_timestamp, decision.response
                )
            try:
                await _webhook_request_with_recovery(
                    state, msg.webhook, msg.from_agent_id,
                    method="POST", timeout=30.0,
                    json={
                        "type": "response",
                        "agent_id": state.agent_id,
                        "response": decision.response,
                        "metadata": msg.metadata,
                        "timestamp": resp_timestamp,
                        "from_public_key_hex": state.public_key_hex,
                        "signature_hex": resp_signature_hex,
                    }
                )
            except Exception as e:
                print(f"[DarkMatter] Router auto-respond webhook failed: {e}", file=sys.stderr)
        save_state()

    elif decision.action == RouterAction.DROP:
        # Remove from queue silently
        for i, m in enumerate(state.message_queue):
            if m.message_id == msg.message_id:
                state.message_queue.pop(i)
                break
        save_state()
        print(f"[DarkMatter] Router dropped message {msg.message_id[:12]}...: {decision.reason}", file=sys.stderr)

    # PASS should never reach here — handled in _execute_routing


async def _execute_routing(state: AgentState, msg: QueuedMessage) -> None:
    """Run the router chain and execute the winning decision."""
    chain = _get_router_chain(state.router_mode)

    for router_fn in chain:
        try:
            # Support both sync and async routers
            result = router_fn(state, msg)
            if asyncio.iscoroutine(result):
                result = await result
            decision = result
        except Exception as e:
            print(f"[DarkMatter] Router {router_fn.__name__} raised: {e}", file=sys.stderr)
            continue

        if decision.action != RouterAction.PASS:
            print(
                f"[DarkMatter] Routing decision for {msg.message_id[:12]}...: "
                f"{decision.action.value} (by {router_fn.__name__}: {decision.reason})",
                file=sys.stderr,
            )
            await _execute_decision(state, msg, decision)
            return

    # All routers passed — message stays in queue
    print(f"[DarkMatter] All routers passed for {msg.message_id[:12]}... — message stays in queue", file=sys.stderr)


# =============================================================================
# Server Initialization
# =============================================================================

mcp = FastMCP(
    "darkmatter_mcp",
    instructions="""\
You are a DarkMatter mesh agent — a node in a self-organizing network of AI agents.

ON STARTUP (do these immediately):
1. Call darkmatter_get_identity to learn who you are. Your identity is derived from your \
passport (.darkmatter/passport.key) — your agent_id IS your public key hex.
2. If your bio is generic or empty, call darkmatter_update_bio with a description of YOUR \
capabilities and specialties. Be specific — other agents use your bio to decide whether to \
route messages to you.
3. Call darkmatter_list_connections to see who you're connected to.
4. Call darkmatter_list_inbox to check for any queued messages waiting for your response.

ONGOING BEHAVIOR:
- When you receive messages (check darkmatter_list_inbox), read them and respond using \
darkmatter_send_message with content and reply_to (the message_id from your inbox). \
You are the intelligence behind this agent — decide how to answer.
- If you can't answer a message, forward it using darkmatter_send_message with the message_id \
from your inbox and a target_agent_id (or target_agent_ids to fork to multiple). Forwarding \
removes the message from your queue.
- Track messages you've sent with darkmatter_list_messages and darkmatter_get_sent_message.
- Use darkmatter_expire_message to cancel a sent message that's no longer needed.
- Use darkmatter_connection(action="request", target_url=...) to connect to another agent.
- Use darkmatter_network_info to discover peers in the network.
- Use darkmatter_discover_domain to check if a domain hosts a DarkMatter node.
- Use darkmatter_discover_local to see agents discovered on the local network via LAN broadcast.
- Use darkmatter_list_pending_requests to see pending requests, then \
darkmatter_connection(action="accept", request_id=...) or action="reject".
- Use darkmatter_wallet_balances to view all wallet balances across chains. Use darkmatter_wallet_send \
to send native currency on any chain (defaults to Solana). For Solana-specific operations, use \
darkmatter_get_balance (SOL/SPL), darkmatter_send_sol, and darkmatter_send_token.

IDENTITY:
- Your agent ID and bio define who you are in the mesh. Own it.
- Other agents see your bio when deciding whether to connect or route messages to you.
- Update your bio anytime your capabilities change with darkmatter_update_bio.

IMPRESSIONS (Trust):
- After meaningful interactions, use darkmatter_set_impression to score your peers (-1 to 1) — \
your scores help other agents make trust decisions about connection requests.
- When reviewing pending connection requests, peer trust scores are automatically included — \
your connected peers were already queried for their impressions.
- Use darkmatter_get_impression to check your stored score and notes on a specific agent.

LIVE STATUS:
- The `darkmatter_status` tool description contains live node state AND action items.
- It updates automatically via `notifications/tools/list_changed` — your MCP client re-fetches \
the tool list and the new description appears in your context. Zero tool calls, zero extra tokens.
- When you see an ACTION item in the status, act on it — these are things that need your attention \
(incoming messages, pending connection requests, discovery suggestions).
- Between tasks, glance at the `darkmatter_status` tool description for new activity.
- Not all MCP clients support auto-updates. If the status looks stale or says "Waiting for first \
status update...", call the tool manually to get current state.

You are autonomous. You decide who to connect to, how to respond to messages, and how to \
participate in the network. The protocol gives you primitives — what you build with them is \
up to you.\
""",
)

# We need a reference to the agent state that both MCP tools and HTTP endpoints share
_agent_state: Optional[AgentState] = None
_active_sessions: set = set()  # Track ServerSession objects for notifications
_last_status_desc: str = ""
_all_tools: dict = {}          # Snapshot of all Tool objects at startup (name -> Tool)
_visible_optional: set = set() # Currently shown optional tool names
import threading
_state_write_lock = threading.Lock()  # Serializes save_state() writes; fcntl.flock handles inter-process locking

# Agent auto-spawn tracking (ephemeral, not persisted)
@dataclass
class SpawnedAgent:
    process: asyncio.subprocess.Process | None  # None in terminal mode
    message_id: str
    spawned_at: float
    pid: int | None           # Read lazily from PID file in terminal mode
    terminal_mode: bool = False
    pid_file: str | None = None      # ~/.darkmatter/spawn_pids/<msg_id>.pid
    script_file: str | None = None   # ~/.darkmatter/spawn_scripts/<msg_id>.sh

_spawned_agents: list[SpawnedAgent] = []
_spawn_timestamps: list[float] = []  # Rolling window for hourly rate limiting


# =============================================================================
# Helper: get state from context (or global)
# =============================================================================

def get_state(ctx: Context = None) -> AgentState:
    """Get the shared agent state. Works from both MCP tools and HTTP handlers."""
    if _agent_state is None:
        raise RuntimeError("Agent state not initialized — call create_app() first.")
    return _agent_state


def _track_session(ctx: Context) -> None:
    """Track an MCP session so we can send notifications later."""
    try:
        _active_sessions.add(ctx.session)
    except Exception:
        pass


def _build_status_line() -> str:
    """Build a live status string with actionable hints from current agent state."""
    state = _agent_state
    if state is None:
        return "Node not initialized"
    conns = len(state.connections)
    msgs = len(state.message_queue)
    handled = state.messages_handled
    pending = len(state.pending_requests)
    # Show display names for peers when available, with transport indicator
    peer_labels = []
    for c in state.connections.values():
        label = c.agent_display_name or c.agent_id[:12]
        if c.transport == "webrtc":
            label += " [webrtc]"
        peer_labels.append(label)
    peers = ", ".join(peer_labels) if peer_labels else "none"

    agent_label = state.display_name or state.agent_id[:12]
    active_agents = len(_spawned_agents)
    agent_suffix = f" | Spawned agents: {active_agents}" if AGENT_SPAWN_ENABLED else ""
    wallet_parts = [f"{chain}: {addr[:6]}...{addr[-4:]}" for chain, addr in state.wallets.items()]
    wallet_suffix = f" | Wallets: {', '.join(wallet_parts)}" if wallet_parts else ""
    stats = (
        f"Agent: {agent_label} | Status: {state.status.value} | "
        f"Connections: {conns}/{MAX_CONNECTIONS} ({peers}) | "
        f"Inbox: {msgs} | Handled: {handled} | Pending requests: {pending}"
        f"{agent_suffix}{wallet_suffix}"
    )

    # Build action items, most urgent first
    actions = []
    if state.status == AgentStatus.INACTIVE:
        actions.append(
            "You are INACTIVE — other agents cannot see or message you. Use darkmatter_set_status to go active"
        )
    if pending > 0:
        actions.append(
            f"{pending} agent(s) want to connect — use darkmatter_list_pending_requests to review (includes peer trust scores)"
        )
    if msgs > 0:
        actions.append(
            f"{msgs} message(s) in your inbox — use darkmatter_list_inbox to read and darkmatter_send_message(content=..., reply_to=...) to reply"
        )
    sent_active = sum(1 for sm in state.sent_messages.values() if sm.status == "active")
    if sent_active > 0:
        actions.append(
            f"{sent_active} sent message(s) awaiting response — use darkmatter_list_messages to check"
        )
    if conns == 0:
        actions.append(
            "No connections yet — use darkmatter_discover_local to find nearby agents or darkmatter_connection(action='request') to connect to a known peer"
        )
    if not state.bio or state.bio in (
        "A DarkMatter mesh agent.",
        "Description of what this agent specializes in",
    ):
        actions.append(
            "Your bio is generic — use darkmatter_update_bio to describe your actual capabilities so other agents can route to you"
        )
    if not state.display_name:
        actions.append(
            "No display name set — edit DARKMATTER_DISPLAY_NAME in your .mcp.json and ask the user to restart"
        )

    if actions:
        action_block = "\n".join(f"ACTION: {a}" for a in actions)
        return f"{stats}\n\n{action_block}"
    else:
        return f"{stats}\n\nAll clear — inbox empty, no pending requests."


# =============================================================================
# Agent Auto-Spawn
# =============================================================================

def _can_spawn_agent() -> tuple[bool, str]:
    """Check whether we can spawn a new agent subprocess.

    Returns (ok, reason) — if ok is False, reason explains why.
    """
    if not AGENT_SPAWN_ENABLED:
        return False, "Agent spawning is disabled (DARKMATTER_AGENT_ENABLED=false)"

    # Clean up finished agents first
    _cleanup_finished_agents()

    # Concurrency limit
    active = len(_spawned_agents)
    if active >= AGENT_SPAWN_MAX_CONCURRENT:
        return False, f"Concurrency limit reached ({active}/{AGENT_SPAWN_MAX_CONCURRENT})"

    # Hourly rate limit (rolling window)
    now = time.monotonic()
    cutoff = now - 3600
    # Prune old timestamps
    while _spawn_timestamps and _spawn_timestamps[0] < cutoff:
        _spawn_timestamps.pop(0)
    if len(_spawn_timestamps) >= AGENT_SPAWN_MAX_PER_HOUR:
        return False, f"Hourly rate limit reached ({len(_spawn_timestamps)}/{AGENT_SPAWN_MAX_PER_HOUR})"

    return True, ""


def _is_agent_running(agent: SpawnedAgent) -> bool:
    """Check if a spawned agent is still running (works in both modes)."""
    if not agent.terminal_mode:
        return agent.process is not None and agent.process.returncode is None

    # Terminal mode: read PID from file, check with os.kill(pid, 0)
    pid = agent.pid
    if pid is None and agent.pid_file:
        try:
            pid = int(open(agent.pid_file).read().strip())
            agent.pid = pid
        except (FileNotFoundError, ValueError):
            return False
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we can't signal it


def _kill_agent(agent: SpawnedAgent, force: bool = False) -> None:
    """Send terminate/kill signal to a spawned agent (works in both modes)."""
    if not agent.terminal_mode:
        if agent.process is not None:
            if force:
                agent.process.kill()
            else:
                agent.process.terminate()
        return

    # Terminal mode: signal via PID
    pid = agent.pid
    if pid is None and agent.pid_file:
        try:
            pid = int(open(agent.pid_file).read().strip())
            agent.pid = pid
        except (FileNotFoundError, ValueError):
            return
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


def _cleanup_terminal_files(agent: SpawnedAgent) -> None:
    """Remove PID and script files for a finished terminal agent."""
    for path in (agent.pid_file, agent.script_file):
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def _cleanup_finished_agents() -> None:
    """Remove finished agent processes from the tracking list."""
    still_running = []
    for agent in _spawned_agents:
        if not _is_agent_running(agent):
            pid_display = agent.pid or "unknown"
            if agent.terminal_mode:
                print(
                    f"[DarkMatter] Terminal agent PID {pid_display} finished "
                    f"(msg={agent.message_id[:12]}...)",
                    file=sys.stderr,
                )
                _cleanup_terminal_files(agent)
            else:
                print(
                    f"[DarkMatter] Spawned agent PID {pid_display} exited "
                    f"(code={agent.process.returncode if agent.process else '?'}, msg={agent.message_id[:12]}...)",
                    file=sys.stderr,
                )
        else:
            still_running.append(agent)
    _spawned_agents.clear()
    _spawned_agents.extend(still_running)


def _build_agent_prompt(state: "AgentState", msg: "QueuedMessage") -> str:
    """Build the prompt for a spawned agent."""
    return f"""\
DARKMATTER: You have received a new message, check message {msg.message_id} and respond or forward accordingly.
"""


async def _spawn_in_terminal(msg: "QueuedMessage", prompt: str, env: dict, cwd: str) -> SpawnedAgent:
    """Spawn an agent in a visible Terminal.app window (macOS only).

    Writes a wrapper shell script that records its PID, runs the agent command
    via exec, and cleans up on exit.  Opens the script in Terminal.app via osascript.
    """
    darkmatter_dir = os.path.expanduser("~/.darkmatter")
    pids_dir = os.path.join(darkmatter_dir, "spawn_pids")
    scripts_dir = os.path.join(darkmatter_dir, "spawn_scripts")
    os.makedirs(pids_dir, exist_ok=True)
    os.makedirs(scripts_dir, exist_ok=True)

    pid_file = os.path.join(pids_dir, f"{msg.message_id}.pid")
    script_file = os.path.join(scripts_dir, f"{msg.message_id}.sh")

    # Build the env export block
    env_lines = []
    for k, v in env.items():
        env_lines.append(f"export {k}={shlex.quote(v)}")
    env_block = "\n".join(env_lines)

    # Build the full command
    cmd_parts = [AGENT_SPAWN_COMMAND] + AGENT_SPAWN_ARGS + [prompt]
    cmd_str = " ".join(shlex.quote(p) for p in cmd_parts)

    unset_block = "\n".join(f"unset {var}" for var in AGENT_SPAWN_ENV_CLEANUP)

    script_content = f"""\
#!/bin/bash
# DarkMatter spawned agent — message {msg.message_id[:12]}...
echo $$ > {shlex.quote(pid_file)}

cleanup() {{
    rm -f {shlex.quote(pid_file)}
}}
trap cleanup EXIT

{unset_block}
{env_block}

cd {shlex.quote(cwd)}

echo "[DarkMatter] Agent started for message {msg.message_id[:12]}..."
echo "PID: $$"
echo "---"

exec {cmd_str}
"""

    with open(script_file, "w") as f:
        f.write(script_content)
    os.chmod(script_file, 0o755)

    # Open in Terminal.app via osascript
    escaped_path = script_file.replace('\\', '\\\\').replace('"', '\\"')
    apple_script = f'tell application "Terminal" to do script "{escaped_path}"'
    await asyncio.create_subprocess_exec("osascript", "-e", apple_script)

    # Wait briefly for PID file to appear
    pid = None
    for _ in range(20):
        await asyncio.sleep(0.25)
        try:
            pid = int(open(pid_file).read().strip())
            break
        except (FileNotFoundError, ValueError):
            continue

    agent = SpawnedAgent(
        process=None,
        message_id=msg.message_id,
        spawned_at=time.monotonic(),
        pid=pid,
        terminal_mode=True,
        pid_file=pid_file,
        script_file=script_file,
    )
    return agent


async def _spawn_agent_for_message(state: "AgentState", msg: "QueuedMessage") -> None:
    """Spawn an agent subprocess to handle an incoming message."""
    ok, reason = _can_spawn_agent()
    if not ok:
        print(f"[DarkMatter] Not spawning agent: {reason}", file=sys.stderr)
        return

    # Deduplicate — don't spawn for a message we're already handling
    for agent in _spawned_agents:
        if agent.message_id == msg.message_id:
            print(f"[DarkMatter] Agent already spawned for message {msg.message_id[:12]}...", file=sys.stderr)
            return

    # Persist queue so child's MCP server can load the message
    save_state()

    prompt = _build_agent_prompt(state, msg)

    # Build environment — disable recursion, avoid port conflicts
    env = os.environ.copy()
    env["DARKMATTER_AGENT_ENABLED"] = "false"
    env["DARKMATTER_ENTRYPOINT_AUTOSTART"] = "false"
    for var in AGENT_SPAWN_ENV_CLEANUP:
        env.pop(var, None)

    import random
    env["DARKMATTER_PORT"] = str(random.randint(9200, 9299))

    try:
        # Terminal mode: open in a visible Terminal.app window (macOS only)
        if AGENT_SPAWN_TERMINAL and sys.platform == "darwin":
            agent = await _spawn_in_terminal(msg, prompt, env, os.getcwd())
            _spawned_agents.append(agent)
            _spawn_timestamps.append(time.monotonic())
            pid_display = agent.pid or "pending"
            print(
                f"[DarkMatter] Spawned terminal agent (PID {pid_display}) for message {msg.message_id[:12]}... "
                f"from {msg.from_agent_id or 'unknown'}",
                file=sys.stderr,
            )
            asyncio.create_task(_agent_timeout_watchdog(agent))
            return

        process = await asyncio.create_subprocess_exec(
            AGENT_SPAWN_COMMAND, *AGENT_SPAWN_ARGS, prompt,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.getcwd(),
        )
        agent = SpawnedAgent(
            process=process,
            message_id=msg.message_id,
            spawned_at=time.monotonic(),
            pid=process.pid,
        )
        _spawned_agents.append(agent)
        _spawn_timestamps.append(time.monotonic())
        print(
            f"[DarkMatter] Spawned agent PID {process.pid} for message {msg.message_id[:12]}... "
            f"from {msg.from_agent_id or 'unknown'}",
            file=sys.stderr,
        )

        # Start timeout watchdog
        asyncio.create_task(_agent_timeout_watchdog(agent))

    except FileNotFoundError:
        print(
            f"[DarkMatter] Agent spawn failed: command '{AGENT_SPAWN_COMMAND}' not found. "
            f"Set DARKMATTER_AGENT_COMMAND to the correct path.",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"[DarkMatter] Agent spawn failed: {e}", file=sys.stderr)


async def _agent_timeout_watchdog(agent: SpawnedAgent) -> None:
    """Kill a spawned agent if it exceeds the timeout."""
    await asyncio.sleep(AGENT_SPAWN_TIMEOUT)
    if not _is_agent_running(agent):
        return
    pid_display = agent.pid or "unknown"
    print(
        f"[DarkMatter] Spawned agent PID {pid_display} timed out after {AGENT_SPAWN_TIMEOUT}s, terminating...",
        file=sys.stderr,
    )
    try:
        _kill_agent(agent, force=False)
        # Give it 5 seconds to clean up, then force kill
        await asyncio.sleep(5.0)
        if _is_agent_running(agent):
            print(f"[DarkMatter] Force-killing agent PID {pid_display}", file=sys.stderr)
            _kill_agent(agent, force=True)
        if agent.terminal_mode:
            _cleanup_terminal_files(agent)
    except ProcessLookupError:
        pass  # Already exited


async def _notify_tools_changed() -> None:
    """Send tools/list_changed notification to all tracked MCP sessions."""
    global _active_sessions
    dead = set()
    for session in list(_active_sessions):
        try:
            await session.send_tool_list_changed()
        except Exception:
            dead.add(session)
    _active_sessions -= dead


def _compute_visible_optional() -> set:
    """Compute which optional tools should be visible based on current agent state."""
    state = _agent_state
    if state is None:
        return set()

    visible = set()
    conns = len(state.connections)
    pending = len(state.pending_requests)
    has_sent = bool(state.sent_messages)

    # Trust tools: show when we have connections or pending requests
    if conns > 0 or pending > 0:
        visible.update({
            "darkmatter_set_impression",
            "darkmatter_get_impression",
        })

    # Discovery tools: show when few connections (need to find peers)
    if conns < 2:
        visible.update({
            "darkmatter_discover_domain",
            "darkmatter_discover_local",
            "darkmatter_network_info",
        })

    # Sent message tracking: show when there are sent messages to track
    if has_sent:
        visible.update({
            "darkmatter_list_messages",
            "darkmatter_get_sent_message",
            "darkmatter_expire_message",
            "darkmatter_wait_for_response",
        })

    # Status tool: show when inactive (need to reactivate)
    if state.status == AgentStatus.INACTIVE:
        visible.add("darkmatter_set_status")

    # Rate limiting: show when enough connections to warrant it
    if conns >= 3:
        visible.add("darkmatter_set_rate_limit")

    # Pending requests: show review tool when requests are waiting
    if pending > 0:
        visible.add("darkmatter_list_pending_requests")

    # Wallet tools: show when wallets are available
    if state.wallets:
        visible.add("darkmatter_wallet_balances")
        if "solana" in state.wallets and SOLANA_AVAILABLE:
            visible.add("darkmatter_get_balance")
            if conns > 0:
                visible.update({"darkmatter_send_sol", "darkmatter_send_token", "darkmatter_wallet_send"})

    # get_server_template: never auto-shown (accessible via graceful fallback)

    return visible


async def _update_status_tool() -> None:
    """Update the status tool's description and tool visibility if state changed, and notify clients."""
    global _last_status_desc, _visible_optional
    new_desc = _build_status_line()
    status_changed = new_desc != _last_status_desc

    # Compute desired tool visibility and diff against current
    desired_optional = _compute_visible_optional()
    visibility_changed = desired_optional != _visible_optional

    if not status_changed and not visibility_changed:
        return

    if status_changed:
        _last_status_desc = new_desc
        # Update the tool's description in FastMCP's internal store
        tool = mcp._tool_manager._tools.get("darkmatter_status")
        if tool:
            tool.description = (
                "DarkMatter live node status dashboard. "
                "Current state is shown below — no need to call unless you want full details.\n\n"
                f"LIVE STATUS: {new_desc}"
            )

    if visibility_changed and _all_tools:
        # Compute diff
        to_add = desired_optional - _visible_optional
        to_remove = _visible_optional - desired_optional

        # Add newly visible tools
        for name in to_add:
            if name in _all_tools:
                mcp._tool_manager._tools[name] = _all_tools[name]

        # Remove newly hidden tools
        for name in to_remove:
            mcp._tool_manager._tools.pop(name, None)

        _visible_optional = desired_optional
        added_str = ", ".join(sorted(to_add)) if to_add else "none"
        removed_str = ", ".join(sorted(to_remove)) if to_remove else "none"
        print(f"[DarkMatter] Tool visibility: +[{added_str}] -[{removed_str}] (total: {len(mcp._tool_manager._tools)})", file=sys.stderr)

    await _notify_tools_changed()
    if status_changed:
        print(f"[DarkMatter] Status tool updated: {new_desc}", file=sys.stderr)


def _initialize_tool_visibility() -> None:
    """Snapshot all tools, remove non-core ones, and monkey-patch call_tool for graceful fallback."""
    global _all_tools, _visible_optional

    # 1. Snapshot all registered tools
    _all_tools = dict(mcp._tool_manager._tools)
    all_names = set(_all_tools.keys())
    optional_names = all_names - CORE_TOOLS

    # 2. Compute initial visibility
    _visible_optional = _compute_visible_optional()

    # 3. Remove optional tools that shouldn't be visible yet
    to_hide = optional_names - _visible_optional
    for name in to_hide:
        mcp._tool_manager._tools.pop(name, None)

    visible_count = len(mcp._tool_manager._tools)
    hidden_count = len(to_hide)
    print(f"[DarkMatter] Tool visibility initialized: {visible_count} visible, {hidden_count} hidden", file=sys.stderr)
    if _visible_optional:
        print(f"[DarkMatter] Optional tools shown: {', '.join(sorted(_visible_optional))}", file=sys.stderr)

    # 4. Monkey-patch call_tool to gracefully handle calls to hidden tools
    original_call_tool = mcp._tool_manager.call_tool

    async def _patched_call_tool(name, arguments, **kwargs):
        # If the tool is hidden but exists in our snapshot, re-add it on demand
        if name not in mcp._tool_manager._tools and name in _all_tools:
            mcp._tool_manager._tools[name] = _all_tools[name]
            print(f"[DarkMatter] Graceful fallback: restored hidden tool '{name}' on demand", file=sys.stderr)
            # Also add to visible set so it doesn't get removed next cycle
            _visible_optional.add(name)
        return await original_call_tool(name, arguments, **kwargs)

    mcp._tool_manager.call_tool = _patched_call_tool


def _check_webrtc_health() -> None:
    """Clean up dead WebRTC channels on all connections."""
    state = _agent_state
    if state is None:
        return
    for conn in state.connections.values():
        if conn.webrtc_channel is None:
            continue
        ready = getattr(conn.webrtc_channel, "readyState", None)
        if ready not in ("open", "connecting"):
            peer = conn.agent_display_name or conn.agent_id[:12]
            print(f"[DarkMatter] WebRTC: cleaning up dead channel (peer: {peer}, state: {ready})", file=sys.stderr)
            _cleanup_webrtc(conn)


def _purge_stale_inbox(state) -> None:
    """Remove messages older than 1 hour from the inbox."""
    now = datetime.now(timezone.utc)
    cutoff_seconds = 3600  # 1 hour
    keep = []
    for msg in state.message_queue:
        try:
            received = datetime.fromisoformat(msg.received_at.replace("Z", "+00:00"))
            age = (now - received).total_seconds()
            if age < cutoff_seconds:
                keep.append(msg)
            else:
                print(f"[DarkMatter] Auto-purged stale message {msg.message_id} (age: {int(age)}s)", file=sys.stderr)
        except Exception:
            keep.append(msg)  # Keep if we can't parse the timestamp
    if len(keep) != len(state.message_queue):
        state.message_queue = keep
        save_state()


def _check_auto_reactivate(state) -> None:
    """Auto-reactivate if inactive_until has expired."""
    if state.status != AgentStatus.INACTIVE or not state.inactive_until:
        return
    try:
        until = datetime.fromisoformat(state.inactive_until.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) >= until:
            state.status = AgentStatus.ACTIVE
            state.inactive_until = None
            save_state()
            print("[DarkMatter] Auto-reactivated (inactive timer expired)", file=sys.stderr)
    except Exception:
        pass


async def _status_updater() -> None:
    """Background task: periodically update the status tool description, check WebRTC health, auto-reactivate, and purge stale inbox."""
    _purge_cycle = 0
    while True:
        await asyncio.sleep(5)
        try:
            _check_webrtc_health()
            _cleanup_finished_agents()
            _check_auto_reactivate(_agent_state)
            # Purge stale inbox every ~30s (6 cycles of 5s)
            _purge_cycle += 1
            if _purge_cycle >= 6:
                _purge_cycle = 0
                _purge_stale_inbox(_agent_state)
            await _update_status_tool()
        except Exception as e:
            print(f"[DarkMatter] Status updater error: {e}", file=sys.stderr)


def _state_file_path() -> str:
    """Return the state file path, keyed by the agent's public key hex (passport-derived).

    Respects DARKMATTER_STATE_FILE env var for test isolation (multiple nodes
    sharing the same passport but needing separate state files).
    """
    override = os.environ.get("DARKMATTER_STATE_FILE")
    if override:
        os.makedirs(os.path.dirname(override) or ".", exist_ok=True)
        return override
    state = _agent_state
    if state is not None and state.public_key_hex:
        state_dir = os.path.join(os.path.expanduser("~"), ".darkmatter", "state")
        os.makedirs(state_dir, exist_ok=True)
        return os.path.join(state_dir, f"{state.public_key_hex}.json")
    # Fallback for pre-init calls (shouldn't happen in normal flow)
    state_dir = os.path.join(os.path.expanduser("~"), ".darkmatter", "state")
    os.makedirs(state_dir, exist_ok=True)
    port = os.environ.get("DARKMATTER_PORT", "8100")
    return os.path.join(state_dir, f"{port}.json")


def save_state() -> None:
    """Persist durable state to disk.

    Message queue is persisted so spawned child agents can load messages on startup.
    Pending requests are NOT persisted — they are ephemeral.
    Serialized via _state_write_lock to prevent concurrent writes from interleaving.
    """
    state = _agent_state
    if state is None:
        return

    # Cap sent_messages at SENT_MESSAGES_MAX, evicting oldest
    if len(state.sent_messages) > SENT_MESSAGES_MAX:
        sorted_msgs = sorted(state.sent_messages.items(), key=lambda x: x[1].created_at)
        state.sent_messages = dict(sorted_msgs[-SENT_MESSAGES_MAX:])

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
            aid: {"score": imp.score, "note": imp.note}
            for aid, imp in state.impressions.items()
        },
        "inactive_until": state.inactive_until,
        "rate_limit_global": state.rate_limit_global,
        "router_mode": state.router_mode,
        "routing_rules": [_routing_rule_to_dict(r) for r in state.routing_rules],
        "superagent_url": state.superagent_url,
        "gas_log": state.gas_log[-GAS_LOG_MAX:],
        "seen_message_ids": {
            mid: ts for mid, ts in _seen_message_ids.items()
            if time.time() - ts < _REPLAY_WINDOW
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

    path = _state_file_path()
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


def _load_state_from_file(path: str) -> Optional[AgentState]:
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
            rate_limit=cd.get("rate_limit", 0),
            peer_created_at=cd.get("peer_created_at"),
        )

    sent_messages = {}
    for mid, sd in data.get("sent_messages", {}).items():
        # Migrate old single "response" to "responses" list
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

    # Restore message queue
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

    # Restore replay protection cache (prune expired entries)
    global _seen_message_ids
    now = time.time()
    saved_replay = data.get("seen_message_ids", {})
    if isinstance(saved_replay, dict):
        _seen_message_ids.update({
            mid: ts for mid, ts in saved_replay.items()
            if isinstance(ts, (int, float)) and now - ts < _REPLAY_WINDOW
        })

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
                Impression(score=v["score"], note=v.get("note", ""))
                if isinstance(v, dict) else
                Impression(score=0.0, note=v)  # migrate old string format
            )
            for aid, v in data.get("impressions", {}).items()
        },
        rate_limit_global=data.get("rate_limit_global", 0),
        inactive_until=data.get("inactive_until"),
        router_mode=data.get("router_mode") or "spawn",
        routing_rules=[_routing_rule_from_dict(rd) for rd in data.get("routing_rules", [])],
        superagent_url=data.get("superagent_url"),
        gas_log=data.get("gas_log", []),
    )

    return state


# =============================================================================
# Tool Input Models
# =============================================================================

class ConnectionAction(str, Enum):
    REQUEST = "request"
    ACCEPT = "accept"
    REJECT = "reject"
    DISCONNECT = "disconnect"


class ConnectionInput(BaseModel):
    """Manage connections: request, accept, reject, or disconnect."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    action: ConnectionAction = Field(..., description="The connection action to perform")
    target_url: Optional[str] = Field(default=None, description="Target agent URL (for request)")
    request_id: Optional[str] = Field(default=None, description="Pending request ID (for accept/reject)")
    agent_id: Optional[str] = Field(default=None, description="Agent ID (for disconnect)")


class SendMessageInput(BaseModel):
    """Send a message, reply to a message, or forward a message."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    content: Optional[str] = Field(default=None, description="Message content (for new messages)", max_length=MAX_CONTENT_LENGTH)
    message_id: Optional[str] = Field(default=None, description="Queue message ID (for forwarding)")
    reply_to: Optional[str] = Field(default=None, description="Message ID from your inbox to reply to. Removes the message from your queue and sends your response via its webhook.")
    target_agent_id: Optional[str] = Field(default=None, description="Specific agent to send/forward to")
    target_agent_ids: Optional[list[str]] = Field(default=None, description="Multiple agents to forward to (fork)")
    metadata: Optional[dict] = Field(default_factory=dict, description="Arbitrary metadata (budget, preferences, etc.)")
    hops_remaining: int = Field(default=10, ge=1, le=50, description="How many more hops this message can take before expiring (TTL)")
    note: Optional[str] = Field(default=None, description="Forwarding annotation visible to the sender", max_length=1000)
    force: bool = Field(default=False, description="Override loop detection when forwarding")


class UpdateBioInput(BaseModel):
    """Update this agent's bio / specialty description."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    bio: str = Field(..., description="New bio text describing this agent's specialty", min_length=1, max_length=1000)


class SetStatusInput(BaseModel):
    """Set this agent's active/inactive status."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    status: AgentStatus = Field(..., description="'active' or 'inactive'")
    duration_minutes: Optional[int] = Field(default=None, ge=1, le=1440, description="Auto-reactivate after N minutes (inactive only, default: 60)")


class GetMessageInput(BaseModel):
    """Get full details of a specific queued message."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the queued message to inspect")


class GetSentMessageInput(BaseModel):
    """Get full details of a sent message including webhook updates."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the sent message to inspect")


class ExpireMessageInput(BaseModel):
    """Expire a sent message so agents stop forwarding it."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the sent message to expire")


class WaitForResponseInput(BaseModel):
    """Wait for a response to a sent message."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the sent message to wait on")
    timeout_seconds: float = Field(default=60, description="How long to wait in seconds before giving up", gt=0)


class ConnectionAcceptedInput(BaseModel):
    """Notification that a connection request was accepted (called agent-to-agent)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The accepting agent's ID")
    agent_url: str = Field(..., description="The accepting agent's MCP URL")
    agent_bio: str = Field(..., description="The accepting agent's bio")
    agent_public_key_hex: Optional[str] = Field(default=None, description="The accepting agent's Ed25519 public key")
    agent_display_name: Optional[str] = Field(default=None, description="The accepting agent's display name")


class DiscoverDomainInput(BaseModel):
    """Check if a domain hosts a DarkMatter node."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    domain: str = Field(..., description="Domain to check (e.g. 'example.com' or 'localhost:8100')")


class SetImpressionInput(BaseModel):
    """Store or update your impression of an agent."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The agent ID to store an impression of")
    score: float = Field(..., ge=-1.0, le=1.0, description="Trust score from -1.0 (avoid) to 1.0 (fully trusted)")
    note: str = Field(default="", description="Optional freeform context", max_length=2000)


class GetImpressionInput(BaseModel):
    """Get your stored impression of an agent."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The agent ID to look up")


class SetSuperagentInput(BaseModel):
    """Set the default superagent URL for gas routing."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    url: Optional[str] = Field(default=None, description="Superagent URL. Set to null to reset to default.", max_length=MAX_URL_LENGTH)


class SendSolInput(BaseModel):
    """Send SOL to a connected agent's wallet."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The connected agent to send SOL to")
    amount: float = Field(..., gt=0, description="Amount of SOL to send")
    notify: bool = Field(default=True, description="Send a message notifying the recipient")


class SendTokenInput(BaseModel):
    """Send SPL tokens to a connected agent's wallet."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The connected agent to send tokens to")
    mint: str = Field(..., description="Token mint address")
    amount: float = Field(..., gt=0, description="Amount in human-readable units")
    decimals: int = Field(..., ge=0, le=18, description="Token decimals (e.g. 6 for USDC)")
    notify: bool = Field(default=True, description="Send a message notifying the recipient")


class GetBalanceInput(BaseModel):
    """Check SOL or SPL token balance."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    mint: Optional[str] = Field(default=None, description="SPL token mint address. Omit for SOL balance.")


class WalletBalancesInput(BaseModel):
    """View wallet balances across all chains."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    chain: Optional[str] = Field(default=None, description="Filter to a specific chain (e.g. 'solana'). Omit for all chains.")


class WalletSendInput(BaseModel):
    """Send native currency on any chain."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The connected agent to send to")
    amount: float = Field(..., gt=0, description="Amount of native currency to send")
    chain: str = Field(default="solana", description="Chain to send on (default: solana)")
    notify: bool = Field(default=True, description="Send a message notifying the recipient")


# =============================================================================
# Mesh Primitive Tools
# =============================================================================

async def _connection_request(state, target_url: str) -> str:
    """Send a connection request to a target agent."""
    url_err = validate_url(target_url)
    if url_err:
        return json.dumps({"success": False, "error": url_err})

    if len(state.connections) >= MAX_CONNECTIONS:
        return json.dumps({
            "success": False,
            "error": f"Connection limit reached ({MAX_CONNECTIONS}). Disconnect from an agent first."
        })

    # Normalize target URL
    target_base = target_url.rstrip("/")
    for suffix in ("/mcp", "/__darkmatter__"):
        if target_base.endswith(suffix):
            target_base = target_base[:-len(suffix)]
            break

    try:
        payload = build_outbound_request_payload(state, _get_public_url(state.port))

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                target_base + "/__darkmatter__/connection_request",
                json=payload,
            )
            result = response.json()

            if result.get("auto_accepted"):
                conn = build_connection_from_accepted(result)
                state.connections[result["agent_id"]] = conn
                save_state()
                return json.dumps({
                    "success": True,
                    "status": "connected",
                    "agent_id": result["agent_id"],
                    "agent_bio": result.get("agent_bio", ""),
                })

            state.pending_outbound[target_base] = result.get("agent_id", "")
            return json.dumps({
                "success": True,
                "status": "pending",
                "message": "Connection request sent. Waiting for acceptance.",
                "request_id": result.get("request_id"),
            })

    except httpx.HTTPError as e:
        return json.dumps({
            "success": False,
            "error": f"Failed to reach target agent at {target_base}: {str(e)}"
        })
    except json.JSONDecodeError:
        status = response.status_code
        return json.dumps({
            "success": False,
            "error": f"Target agent at {target_base} returned non-JSON response (HTTP {status}). Is it a DarkMatter node?"
        })
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": f"Failed to connect to {target_base}: {str(e)}"
        })


async def _connection_respond(state, request_id: str, accept: bool) -> str:
    """Accept or reject a pending connection request."""
    if not accept:
        request = state.pending_requests.get(request_id)
        if not request:
            return json.dumps({
                "success": False,
                "error": f"No pending request with ID '{request_id}'."
            })
        del state.pending_requests[request_id]
        save_state()
        return json.dumps({
            "success": True,
            "accepted": False,
            "agent_id": request.from_agent_id,
        })

    public_url = f"{_get_public_url(state.port)}/mcp"
    result, status, notify_payload = process_accept_pending(state, request_id, public_url)

    if status != 200:
        return json.dumps({"success": False, "error": result.get("error", "Unknown error")})

    # Notify the requesting agent
    if notify_payload:
        agent_id = result.get("agent_id", "")
        conn = state.connections.get(agent_id)
        if conn:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    base = conn.agent_url.rstrip("/")
                    for suffix in ("/mcp", "/__darkmatter__"):
                        if base.endswith(suffix):
                            base = base[:-len(suffix)]
                            break
                    await client.post(
                        base + "/__darkmatter__/connection_accepted",
                        json=notify_payload,
                    )
            except Exception:
                pass

            # Auto WebRTC upgrade
            if WEBRTC_AVAILABLE:
                asyncio.create_task(_attempt_webrtc_upgrade(state, conn))

    return json.dumps(result)


async def _connection_disconnect(state, agent_id: str) -> str:
    """Disconnect from an agent."""
    if agent_id not in state.connections:
        return json.dumps({
            "success": False,
            "error": f"Not connected to agent '{agent_id}'."
        })

    del state.connections[agent_id]
    save_state()

    return json.dumps({
        "success": True,
        "disconnected_from": agent_id,
    })


@mcp.tool(
    name="darkmatter_connection",
    annotations={
        "title": "Manage Connections",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def connection(params: ConnectionInput, ctx: Context) -> str:
    """Manage connections: request, accept, reject, or disconnect.

    Actions:
      - request: Send a connection request (requires target_url)
      - accept: Accept a pending connection request (requires request_id)
      - reject: Reject a pending connection request (requires request_id)
      - disconnect: Disconnect from an agent (requires agent_id)

    Args:
        params: Contains action and the relevant field(s) for that action.

    Returns:
        JSON with the result.
    """
    state = get_state(ctx)

    if params.action == ConnectionAction.REQUEST:
        if not params.target_url:
            return json.dumps({"success": False, "error": "target_url is required for request."})
        return await _connection_request(state, params.target_url)

    elif params.action == ConnectionAction.ACCEPT:
        if not params.request_id:
            return json.dumps({"success": False, "error": "request_id is required for accept."})
        return await _connection_respond(state, params.request_id, accept=True)

    elif params.action == ConnectionAction.REJECT:
        if not params.request_id:
            return json.dumps({"success": False, "error": "request_id is required for reject."})
        return await _connection_respond(state, params.request_id, accept=False)

    elif params.action == ConnectionAction.DISCONNECT:
        if not params.agent_id:
            return json.dumps({"success": False, "error": "agent_id is required for disconnect."})
        return await _connection_disconnect(state, params.agent_id)

    return json.dumps({"success": False, "error": f"Unknown action: {params.action}"})


# =============================================================================
# Transport Abstraction — WebRTC or HTTP
# =============================================================================

async def _http_post_to_peer(conn: Connection, path: str, payload: dict) -> dict:
    """Send an HTTP POST to a peer. Returns the response dict."""
    base_url = conn.agent_url.rstrip("/")
    for suffix in ("/mcp", "/__darkmatter__"):
        if base_url.endswith(suffix):
            base_url = base_url[:-len(suffix)]
            break
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            base_url + path,
            json=payload,
        )
        result = resp.json()
        result["transport"] = "http"
        return result


async def _send_to_peer(conn: Connection, path: str, payload: dict) -> dict:
    """Send a message to a peer, using WebRTC data channel if available, else HTTP.

    On HTTP failure, attempts peer URL recovery via _lookup_peer_url before giving up.
    Returns the response dict from the peer.
    """
    # Try WebRTC first if channel is open
    if conn.webrtc_channel is not None:
        try:
            ready = getattr(conn.webrtc_channel, "readyState", None)
            if ready == "open":
                data = json.dumps({"path": path, "payload": payload})
                if len(data) <= WEBRTC_MESSAGE_SIZE_LIMIT:
                    conn.webrtc_channel.send(data)
                    conn.health_failures = 0
                    return {"success": True, "transport": "webrtc"}
                # Message too large for WebRTC — fall through to HTTP
        except Exception as e:
            print(f"[DarkMatter] WebRTC send failed, falling back to HTTP: {e}", file=sys.stderr)

    # HTTP — try direct, then recover via peer lookup on failure
    last_error = None
    try:
        result = await _http_post_to_peer(conn, path, payload)
        conn.health_failures = 0
        return result
    except Exception as e:
        last_error = e

    # Direct HTTP failed — try peer lookup to find updated URL
    state = _agent_state
    if state is not None:
        print(f"[DarkMatter] HTTP send to {conn.agent_id[:12]}... failed, attempting peer lookup", file=sys.stderr)
        new_url = await _lookup_peer_url(state, conn.agent_id)
        if new_url and new_url != conn.agent_url:
            old_url = conn.agent_url
            conn.agent_url = new_url
            save_state()
            print(f"[DarkMatter] Recovered URL for {conn.agent_id[:12]}...: {old_url} -> {new_url}", file=sys.stderr)
            try:
                result = await _http_post_to_peer(conn, path, payload)
                conn.health_failures = 0
                return result
            except Exception:
                pass  # Recovery also failed — raise original error

    raise last_error


async def _send_new_message(state, params: SendMessageInput) -> str:
    """Send a new message into the mesh."""
    message_id = f"msg-{uuid.uuid4().hex[:12]}"
    metadata = params.metadata or {}

    webhook = _build_webhook_url(state, message_id)

    if params.target_agent_id:
        conn = state.connections.get(params.target_agent_id)
        if not conn:
            return json.dumps({
                "success": False,
                "error": f"Not connected to agent '{params.target_agent_id}'."
            })
        targets = [conn]
    else:
        targets = [c for c in state.connections.values()]

    if not targets:
        return json.dumps({
            "success": False,
            "error": "No connections available to route this message."
        })

    msg_timestamp = datetime.now(timezone.utc).isoformat()
    signature_hex = None
    if state.private_key_hex:
        signature_hex = _sign_message(
            state.private_key_hex, state.agent_id, message_id, msg_timestamp, params.content
        )

    sent_to = []
    failed = []
    for conn in targets:
        try:
            payload = {
                "message_id": message_id,
                "content": params.content,
                "webhook": webhook,
                "hops_remaining": params.hops_remaining,
                "from_agent_id": state.agent_id,
                "metadata": metadata,
                "timestamp": msg_timestamp,
                "from_public_key_hex": state.public_key_hex,
                "signature_hex": signature_hex,
            }
            await _send_to_peer(conn, "/__darkmatter__/message", payload)
            conn.messages_sent += 1
            conn.last_activity = datetime.now(timezone.utc).isoformat()
            sent_to.append(conn.agent_id)
        except Exception as e:
            conn.messages_declined += 1
            failed.append({"agent_id": conn.agent_id, "display_name": conn.agent_display_name, "error": str(e)})

    sent_msg = SentMessage(
        message_id=message_id,
        content=params.content,
        status="active",
        initial_hops=params.hops_remaining,
        routed_to=sent_to,
    )
    state.sent_messages[message_id] = sent_msg
    save_state()

    result = {
        "success": len(sent_to) > 0,
        "message_id": message_id,
        "routed_to": sent_to,
        "hops_remaining": params.hops_remaining,
        "webhook": webhook,
    }
    if failed:
        result["failed"] = failed
        if not sent_to:
            result["error"] = f"Message could not be delivered to any of {len(failed)} target(s). Check 'failed' for details."
    if sent_to:
        result["hint"] = f"Use darkmatter_wait_for_response(message_id='{message_id}') to block until a reply arrives."
    return json.dumps(result)


async def _forward_message(state, params: SendMessageInput) -> str:
    """Forward a queued message to one or more connected agents. Removes from queue after delivery."""
    # Find the message in the queue
    msg = None
    msg_index = None
    for i, m in enumerate(state.message_queue):
        if m.message_id == params.message_id:
            msg_index = i
            msg = m
            break

    if msg is None:
        return json.dumps({
            "success": False,
            "error": f"No queued message with ID '{params.message_id}'."
        })

    # Determine target(s)
    target_ids = []
    if params.target_agent_ids:
        target_ids = params.target_agent_ids
    elif params.target_agent_id:
        target_ids = [params.target_agent_id]
    else:
        return json.dumps({"success": False, "error": "target_agent_id or target_agent_ids required for forwarding."})

    # Validate all targets exist
    target_conns = []
    for tid in target_ids:
        conn = state.connections.get(tid)
        if not conn:
            return json.dumps({"success": False, "error": f"Not connected to agent '{tid}'."})
        target_conns.append((tid, conn))

    # GET webhook status — verify message is still active + loop detection
    webhook_err = validate_webhook_url(msg.webhook)
    if not webhook_err:
        try:
            status_resp = await _webhook_request_with_recovery(
                state, msg.webhook, msg.from_agent_id,
                method="GET", timeout=10.0,
            )
            if status_resp.status_code == 200:
                webhook_data = status_resp.json()
                msg_status = webhook_data.get("status", "active")
                if msg_status in ("expired", "responded"):
                    state.message_queue.pop(msg_index)
                    save_state()
                    return json.dumps({
                        "success": False,
                        "error": f"Message is already {msg_status} (checked via webhook). Removed from queue.",
                    })

                # Loop detection per target
                if not params.force:
                    for tid, _ in target_conns:
                        for update in webhook_data.get("updates", []):
                            if update.get("target_agent_id") == tid:
                                return json.dumps({
                                    "success": False,
                                    "error": f"Agent '{tid}' has already received this message. To forward anyway, retry with force=true.",
                                })
        except Exception as e:
            print(f"[DarkMatter] Warning: webhook status check failed for {msg.message_id}: {e}", file=sys.stderr)

    # TTL check
    if msg.hops_remaining <= 0:
        state.message_queue.pop(msg_index)
        if not webhook_err:
            try:
                await _webhook_request_with_recovery(
                    state, msg.webhook, msg.from_agent_id,
                    method="POST", timeout=30.0,
                    json={"type": "expired", "agent_id": state.agent_id, "note": "Message expired — no hops remaining."}
                )
            except Exception:
                pass
        save_state()
        return json.dumps({"success": False, "error": "Message expired — hops_remaining is 0."})

    new_hops_remaining = msg.hops_remaining - 1

    # Sign the forwarded message
    fwd_timestamp = datetime.now(timezone.utc).isoformat()
    fwd_signature_hex = None
    if state.private_key_hex:
        fwd_signature_hex = _sign_message(
            state.private_key_hex, state.agent_id, msg.message_id, fwd_timestamp, msg.content
        )

    # Deliver to all targets
    per_target_results = []
    for tid, conn in target_conns:
        # POST forwarding update to webhook
        if not webhook_err:
            try:
                await _webhook_request_with_recovery(
                    state, msg.webhook, msg.from_agent_id,
                    method="POST", timeout=10.0,
                    json={"type": "forwarded", "agent_id": state.agent_id, "target_agent_id": tid, "note": params.note}
                )
            except Exception as e:
                print(f"[DarkMatter] Warning: failed to post forwarding update to webhook: {e}", file=sys.stderr)

        try:
            fwd_payload = {
                "message_id": msg.message_id,
                "content": msg.content,
                "webhook": msg.webhook,
                "hops_remaining": new_hops_remaining,
                "from_agent_id": state.agent_id,
                "metadata": msg.metadata,
                "timestamp": fwd_timestamp,
                "from_public_key_hex": state.public_key_hex,
                "signature_hex": fwd_signature_hex,
            }
            result = await _send_to_peer(conn, "/__darkmatter__/message", fwd_payload)
            conn.messages_sent += 1
            conn.last_activity = datetime.now(timezone.utc).isoformat()
            per_target_results.append({"agent_id": tid, "success": True})
        except Exception as e:
            per_target_results.append({"agent_id": tid, "success": False, "error": str(e)})

    # Remove from queue after delivery attempts (behavioral change: forward removes from queue)
    state.message_queue.pop(msg_index)
    save_state()

    any_success = any(r["success"] for r in per_target_results)
    result = {
        "success": any_success,
        "message_id": msg.message_id,
        "hops_remaining_for_targets": new_hops_remaining,
        "results": per_target_results,
    }
    if params.note:
        result["note"] = params.note
    return json.dumps(result)


async def _reply_to_message(state, params: SendMessageInput) -> str:
    """Reply to a queued message by calling its webhook with the response content."""
    # Find and remove the message from the queue
    msg = None
    for i, m in enumerate(state.message_queue):
        if m.message_id == params.reply_to:
            msg = state.message_queue.pop(i)
            break

    if msg is None:
        return json.dumps({
            "success": False,
            "error": f"No queued message with ID '{params.reply_to}'."
        })

    # Validate the stored webhook before calling it (SSRF protection)
    webhook_err = validate_webhook_url(msg.webhook)
    if webhook_err:
        save_state()
        return json.dumps({
            "success": False,
            "message_id": msg.message_id,
            "error": f"Webhook blocked: {webhook_err}",
        })

    # Check if message is still active before responding
    try:
        status_resp = await _webhook_request_with_recovery(
            state, msg.webhook, msg.from_agent_id,
            method="GET", timeout=10.0,
        )
        if status_resp.status_code == 200:
            webhook_data = status_resp.json()
            msg_status = webhook_data.get("status", "active")
            if msg_status == "expired":
                save_state()
                return json.dumps({
                    "success": False,
                    "error": "Message has been expired by the originator.",
                    "message_id": msg.message_id,
                })
    except Exception as e:
        print(f"[DarkMatter] Warning: webhook status check failed for {msg.message_id}: {e}", file=sys.stderr)

    # Notify originator that we're actively responding
    try:
        await _webhook_request_with_recovery(
            state, msg.webhook, msg.from_agent_id,
            method="POST", timeout=10.0,
            json={"type": "responding", "agent_id": state.agent_id}
        )
    except Exception:
        pass  # Best-effort notification

    # Sign the webhook response
    resp_timestamp = datetime.now(timezone.utc).isoformat()
    resp_signature_hex = None
    if state.private_key_hex:
        resp_signature_hex = _sign_message(
            state.private_key_hex, state.agent_id, msg.message_id, resp_timestamp, params.content
        )

    # Call the webhook with our response
    webhook_success = False
    webhook_error = None
    response_time_ms = 0.0
    try:
        start = time.monotonic()
        resp = await _webhook_request_with_recovery(
            state, msg.webhook, msg.from_agent_id,
            method="POST", timeout=30.0,
            json={
                "type": "response",
                "agent_id": state.agent_id,
                "response": params.content,
                "metadata": msg.metadata,
                "timestamp": resp_timestamp,
                "from_public_key_hex": state.public_key_hex,
                "signature_hex": resp_signature_hex,
            }
        )
        response_time_ms = (time.monotonic() - start) * 1000
        webhook_success = resp.status_code < 400
    except Exception as e:
        webhook_error = str(e)

    # Update telemetry for the connection that sent us this message
    if msg.from_agent_id and msg.from_agent_id in state.connections:
        conn = state.connections[msg.from_agent_id]
        conn.total_response_time_ms += response_time_ms
        conn.last_activity = datetime.now(timezone.utc).isoformat()

    save_state()
    return json.dumps({
        "success": webhook_success,
        "message_id": msg.message_id,
        "webhook_called": msg.webhook,
        "response_time_ms": round(response_time_ms, 2),
        "error": webhook_error,
    })


@mcp.tool(
    name="darkmatter_send_message",
    annotations={
        "title": "Send, Reply, or Forward Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def send_message(params: SendMessageInput, ctx: Context) -> str:
    """Send a message, reply to a message, or forward a message.

    - New message: provide `content` (and optionally `target_agent_id`).
    - Reply: provide `content` and `reply_to` (message_id from your inbox).
      Removes the message from your queue and sends your response via its webhook.
    - Forward: provide `message_id` from your inbox (and `target_agent_id` or `target_agent_ids`).
      Removes from queue after delivery.

    Args:
        params: Contains content (new/reply) or message_id (forward), plus routing options.

    Returns:
        JSON with the message ID, routing info, and webhook URL.
    """
    state = get_state(ctx)

    # Validate parameter combinations
    if params.reply_to and params.message_id:
        return json.dumps({"success": False, "error": "Cannot use both reply_to and message_id. Use reply_to with content to reply, or message_id alone to forward."})
    if params.reply_to and not params.content:
        return json.dumps({"success": False, "error": "reply_to requires content (your response text)."})
    if params.message_id and params.content:
        return json.dumps({"success": False, "error": "Provide either content (new message) or message_id (forward), not both."})
    if not params.message_id and not params.content:
        return json.dumps({"success": False, "error": "Provide content (new message/reply) or message_id (forward)."})

    if params.message_id:
        return await _forward_message(state, params)
    elif params.reply_to:
        return await _reply_to_message(state, params)
    else:
        return await _send_new_message(state, params)


# =============================================================================
# Self-Management Tools
# =============================================================================

@mcp.tool(
    name="darkmatter_update_bio",
    annotations={
        "title": "Update Agent Bio",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def update_bio(params: UpdateBioInput, ctx: Context) -> str:
    """Update this agent's bio / specialty description.

    The bio is shared with connected agents and used for routing decisions.

    Args:
        params: Contains the new bio text.

    Returns:
        JSON confirming the update.
    """
    state = get_state(ctx)
    state.bio = params.bio
    save_state()

    # Broadcast bio change to all connected peers
    if state.public_url:
        try:
            await _broadcast_peer_update(state)
        except Exception as e:
            print(f"[DarkMatter] Failed to broadcast bio update: {e}", file=sys.stderr)

    return json.dumps({"success": True, "bio": state.bio})


@mcp.tool(
    name="darkmatter_set_status",
    annotations={
        "title": "Set Agent Status",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def set_status(params: SetStatusInput, ctx: Context) -> str:
    """Set this agent's status to active or inactive.

    Inactive agents don't appear as available to their connections.
    Inactive with no duration defaults to 60 minutes. Use duration_minutes to customize.
    Setting active clears any pending auto-reactivation timer.

    Args:
        params: Contains the status ('active' or 'inactive') and optional duration_minutes.

    Returns:
        JSON confirming the status change.
    """
    state = get_state(ctx)
    state.status = params.status

    if params.status == AgentStatus.INACTIVE:
        duration = params.duration_minutes or 60
        from datetime import timedelta
        reactivate_at = datetime.now(timezone.utc) + timedelta(minutes=duration)
        state.inactive_until = reactivate_at.isoformat()
        save_state()
        return json.dumps({"success": True, "status": "inactive", "inactive_until": state.inactive_until, "duration_minutes": duration})
    else:
        state.inactive_until = None
        save_state()
        return json.dumps({"success": True, "status": "active"})


# =============================================================================
# Introspection Tools (local telemetry)
# =============================================================================

@mcp.tool(
    name="darkmatter_get_identity",
    annotations={
        "title": "Get Agent Identity",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_identity(ctx: Context) -> str:
    """Get this agent's identity, bio, status, and basic stats.

    Returns:
        JSON with agent identity and telemetry.
    """
    _track_session(ctx)
    state = get_state(ctx)
    passport_path = os.path.join(os.getcwd(), ".darkmatter", "passport.key")
    result = {
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "private_key_hex": state.private_key_hex,
        "passport_path": passport_path,
        "bio": state.bio,
        "status": state.status.value,
        "port": state.port,
        "num_connections": len(state.connections),
        "num_pending_requests": len(state.pending_requests),
        "messages_handled": state.messages_handled,
        "message_queue_size": len(state.message_queue),
        "sent_messages_count": len(state.sent_messages),
        "created_at": state.created_at,
    }
    if state.wallets:
        result["wallets"] = state.wallets
    result["superagent_url"] = state.superagent_url or SUPERAGENT_DEFAULT_URL
    return json.dumps(result)


@mcp.tool(
    name="darkmatter_list_connections",
    annotations={
        "title": "List Connections",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_connections(ctx: Context) -> str:
    """List all current connections with telemetry data.

    Shows each connection's agent ID, bio, message counts,
    response times, and last activity.

    Returns:
        JSON array of connection details.
    """
    state = get_state(ctx)
    connections = []
    def _truncate(text: str, max_words: int = 20) -> str:
        words = text.split()
        if len(words) <= max_words:
            return text
        return " ".join(words[:max_words]) + "..."

    for conn in state.connections.values():
        entry = {
            "agent_id": conn.agent_id,
            "display_name": conn.agent_display_name,
            "agent_url": conn.agent_url,
            "bio_summary": _truncate(conn.agent_bio, 250) if conn.agent_bio else None,
            "crypto": conn.agent_public_key_hex is not None,
            "transport": conn.transport,
            "connected_at": conn.connected_at,
            "messages_sent": conn.messages_sent,
            "messages_received": conn.messages_received,
            "messages_declined": conn.messages_declined,
            "avg_response_time_ms": round(conn.avg_response_time_ms, 2),
            "last_activity": conn.last_activity,
            "rate_limit": conn.rate_limit if conn.rate_limit != 0 else DEFAULT_RATE_LIMIT_PER_CONNECTION,
        }
        if conn.wallets:
            entry["wallets"] = conn.wallets
        impression = state.impressions.get(conn.agent_id)
        if impression:
            entry["score"] = impression.score
            if impression.note:
                entry["note"] = _truncate(impression.note, 500)
        connections.append(entry)

    return json.dumps({
        "total": len(connections),
        "max_connections": MAX_CONNECTIONS,
        "connections": connections,
    })


@mcp.tool(
    name="darkmatter_list_pending_requests",
    annotations={
        "title": "List Pending Connection Requests",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_pending_requests(ctx: Context) -> str:
    """List all pending incoming connection requests.

    Returns:
        JSON array of pending connection requests.
    """
    state = get_state(ctx)
    requests = []
    for req in state.pending_requests.values():
        entry = {
            "request_id": req.request_id,
            "from_agent_id": req.from_agent_id,
            "from_agent_display_name": req.from_agent_display_name,
            "from_agent_url": req.from_agent_url,
            "from_agent_bio": req.from_agent_bio,
            "crypto": req.from_agent_public_key_hex is not None,
            "requested_at": req.requested_at,
        }
        if req.peer_trust is not None:
            entry["peer_trust"] = req.peer_trust
        requests.append(entry)

    return json.dumps({
        "total": len(requests),
        "requests": requests,
        "reminder": "Remember to set impressions for your connections — your peers rely on your scores to make trust decisions.",
    })


# =============================================================================
# Inbox Tools (incoming messages)
# =============================================================================

@mcp.tool(
    name="darkmatter_list_inbox",
    annotations={
        "title": "List Inbox Messages",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_inbox(ctx: Context) -> str:
    """List all incoming messages in the queue waiting to be processed.

    Shows message summaries. Use darkmatter_get_message for full content.

    Returns:
        JSON array of queued messages.
    """
    state = get_state(ctx)
    messages = []
    for msg in state.message_queue:
        messages.append({
            "message_id": msg.message_id,
            "content": msg.content[:200] + ("..." if len(msg.content) > 200 else ""),
            "webhook": msg.webhook,
            "hops_remaining": msg.hops_remaining,
            "can_forward": msg.hops_remaining > 0,
            "from_agent_id": msg.from_agent_id,
            "verified": msg.verified,
            "metadata": msg.metadata,
            "received_at": msg.received_at,
        })

    return json.dumps({"total": len(messages), "messages": messages})


# =============================================================================
# Message Detail Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_get_message",
    annotations={
        "title": "Get Message Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_message(params: GetMessageInput, ctx: Context) -> str:
    """Get full details of a specific queued message.

    Shows full content and metadata. For routing context, GET the webhook URL.

    Args:
        params: Contains message_id to inspect.

    Returns:
        JSON with message details.
    """
    state = get_state(ctx)

    for msg in state.message_queue:
        if msg.message_id == params.message_id:
            return json.dumps({
                "message_id": msg.message_id,
                "content": msg.content,
                "webhook": msg.webhook,
                "hops_remaining": msg.hops_remaining,
                "can_forward": msg.hops_remaining > 0,
                "from_agent_id": msg.from_agent_id,
                "verified": msg.verified,
                "metadata": msg.metadata,
                "received_at": msg.received_at,
            })

    return json.dumps({
        "success": False,
        "error": f"No queued message with ID '{params.message_id}'."
    })


# =============================================================================
# Sent Message Tracking Tools
# =============================================================================

@mcp.tool(
    name="darkmatter_list_messages",
    annotations={
        "title": "List Sent Messages",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_messages(ctx: Context) -> str:
    """List messages this agent has sent into the mesh.

    Shows message summaries with status and routing info.
    Use darkmatter_get_sent_message for full details.

    Returns:
        JSON array of sent messages.
    """
    state = get_state(ctx)
    messages = []
    for sm in state.sent_messages.values():
        forwarding_count = sum(1 for u in sm.updates if u.get("type") == "forwarded")

        messages.append({
            "message_id": sm.message_id,
            "content": sm.content[:200] + ("..." if len(sm.content) > 200 else ""),
            "status": sm.status,
            "initial_hops": sm.initial_hops,
            "forwarding_count": forwarding_count,
            "updates_count": len(sm.updates),
            "created_at": sm.created_at,
        })

    return json.dumps({"total": len(messages), "messages": messages})


@mcp.tool(
    name="darkmatter_get_sent_message",
    annotations={
        "title": "Get Sent Message Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_sent_message(params: GetSentMessageInput, ctx: Context) -> str:
    """Get full details of a sent message including all webhook updates received.

    Shows the complete routing history: which agents forwarded it, any notes
    they attached, and the final response if one has been received.

    Args:
        params: Contains message_id to inspect.

    Returns:
        JSON with full sent message details.
    """
    state = get_state(ctx)

    sm = state.sent_messages.get(params.message_id)
    if not sm:
        return json.dumps({
            "success": False,
            "error": f"No sent message with ID '{params.message_id}'."
        })

    forwarding_count = sum(1 for u in sm.updates if u.get("type") == "forwarded")

    return json.dumps({
        "message_id": sm.message_id,
        "content": sm.content,
        "status": sm.status,
        "initial_hops": sm.initial_hops,
        "forwarding_count": forwarding_count,
        "routed_to": sm.routed_to,
        "created_at": sm.created_at,
        "updates": sm.updates,
        "responses": sm.responses,
    })


@mcp.tool(
    name="darkmatter_expire_message",
    annotations={
        "title": "Expire Sent Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def expire_message(params: ExpireMessageInput, ctx: Context) -> str:
    """Expire a sent message so agents in the mesh stop forwarding it.

    Agents that check the webhook status before forwarding will see the
    message is expired and remove it from their queues.

    Args:
        params: Contains message_id to expire.

    Returns:
        JSON confirming the expiry.
    """
    state = get_state(ctx)

    sm = state.sent_messages.get(params.message_id)
    if not sm:
        return json.dumps({
            "success": False,
            "error": f"No sent message with ID '{params.message_id}'."
        })

    if sm.status == "expired":
        return json.dumps({
            "success": True,
            "message": "Message was already expired.",
            "message_id": sm.message_id,
        })

    sm.status = "expired"
    save_state()

    return json.dumps({
        "success": True,
        "message_id": sm.message_id,
        "status": "expired",
    })


@mcp.tool(
    name="darkmatter_wait_for_response",
    annotations={
        "title": "Wait For Response",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def wait_for_response(params: WaitForResponseInput, ctx: Context) -> str:
    """Wait for a response to arrive on a sent message.

    Blocks until a response webhook fires for the given message_id, or the
    timeout expires. If the message already has responses, returns immediately.

    This does NOT block the node — incoming messages, webhooks, and subagent
    spawns all continue normally while this tool awaits.

    Args:
        params: Contains message_id to wait on and timeout_seconds.

    Returns:
        JSON with the response(s) if one arrived, or a timeout indicator.
    """
    state = get_state(ctx)

    sm = state.sent_messages.get(params.message_id)
    if not sm:
        return json.dumps({
            "success": False,
            "error": f"No sent message with ID '{params.message_id}'."
        })

    # If there are already responses, return immediately
    if sm.responses:
        return json.dumps({
            "success": True,
            "message_id": sm.message_id,
            "status": sm.status,
            "responses": sm.responses,
        })

    # If the message is expired, no point waiting
    if sm.status == "expired":
        return json.dumps({
            "success": False,
            "message_id": sm.message_id,
            "reason": "message_expired",
            "error": "This message has been expired — no response will arrive.",
        })

    # Register an event and wait
    event = asyncio.Event()
    state._response_events.setdefault(params.message_id, []).append(event)

    try:
        await asyncio.wait_for(event.wait(), timeout=params.timeout_seconds)
    except asyncio.TimeoutError:
        # Clean up our event from the list
        evts = state._response_events.get(params.message_id, [])
        if event in evts:
            evts.remove(event)
            if not evts:
                state._response_events.pop(params.message_id, None)
        return json.dumps({
            "success": False,
            "message_id": sm.message_id,
            "reason": "timeout",
            "timeout_seconds": params.timeout_seconds,
            "error": f"No response received within {params.timeout_seconds}s.",
        })

    # Event fired — response arrived
    return json.dumps({
        "success": True,
        "message_id": sm.message_id,
        "status": sm.status,
        "responses": sm.responses,
    })


# =============================================================================
# Replication Tool — the self-replicating part
# =============================================================================

@mcp.tool(
    name="darkmatter_get_server_template",
    annotations={
        "title": "Get Server Template (Replicate)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_server_template(ctx: Context) -> str:
    """Get a copy of this agent's recommended MCP server template.

    This is the replication mechanism. Any agent can provide a server
    template to new agents joining the network. The template doesn't
    have to be the same server this agent runs — it's a recommendation.

    New agents can modify the template however they want, as long as
    they maintain compatibility with the core mesh primitives:
    connect, accept, disconnect, message.

    Returns:
        JSON with the server source code and bootstrap instructions.
    """
    state = get_state(ctx)

    # Read our own source as the template
    server_path = os.path.abspath(__file__)
    with open(server_path, "r") as f:
        source = f.read()

    return json.dumps({
        "template_from": state.agent_id,
        "server_source": source,
        "setup_instructions": {
            "1_save": "Save the server_source to ~/.darkmatter/server.py (create the directory if needed)",
            "2_venv": "Create a venv and install deps: python3 -m venv ~/.darkmatter/venv && ~/.darkmatter/venv/bin/pip install 'mcp[cli]' httpx uvicorn starlette cryptography anyio",
            "3_port": "Pick a port in range 8100-8110. Check availability: lsof -i :<port> 2>/dev/null | grep LISTEN",
            "4_config": (
                "Write .mcp.json in your project directory. Choose a display name that describes this agent. "
                "Example content:\n"
                '{"mcpServers":{"darkmatter":{"command":"~/.darkmatter/venv/bin/python","args":["~/.darkmatter/server.py"],'
                '"env":{"DARKMATTER_PORT":"<port>","DARKMATTER_DISPLAY_NAME":"<your-name>"}}}}'
            ),
            "5_restart": "Ask the user to restart their MCP client (e.g. Claude Code) so it picks up .mcp.json. The server auto-starts via stdio.",
        },
        "after_restart": {
            "6_bio": "Call darkmatter_update_bio to describe your capabilities — this is how other agents discover and route to you.",
            "7_discover": "Call darkmatter_discover_local to find nearby agents on the local network.",
            "8_connect": f"Connect to this agent: darkmatter_connection(action='request', target_url='http://localhost:{state.port}')",
        },
        "notes": {
            "identity": "Identity is automatic — a passport (.darkmatter/passport.key) is created on first run. Your agent_id = public key hex.",
            "name_change": "To change your display name later, edit DARKMATTER_DISPLAY_NAME in .mcp.json and ask the user to restart.",
        },
        "core_primitives": [
            "darkmatter_connection",
            "darkmatter_send_message",
        ],
        "compatibility_note": "You may modify the server however you like. "
            "As long as you implement the core primitives and the "
            "/__darkmatter__/* HTTP endpoints, you remain compatible with the mesh.",
    })


# =============================================================================
# Network Discovery Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_network_info",
    annotations={
        "title": "Network Info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def network_info(ctx: Context) -> str:
    """Get this agent's network info for peer discovery.

    Returns this agent's identity, URL, bio, and a list of connected
    agent IDs and URLs. New agents can use this to discover the network
    and decide who to connect to.

    Returns:
        JSON with agent info and peer list.
    """
    state = get_state(ctx)
    peers = [
        {"agent_id": c.agent_id, "agent_url": c.agent_url, "agent_bio": c.agent_bio}
        for c in state.connections.values()
    ]
    return json.dumps({
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "agent_url": _get_public_url(state.port),
        "bio": state.bio,
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "peers": peers,
    })


# =============================================================================
# Discovery Tools
# =============================================================================

@mcp.tool(
    name="darkmatter_discover_domain",
    annotations={
        "title": "Discover Domain",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def discover_domain(params: DiscoverDomainInput, ctx: Context) -> str:
    """Check if a domain hosts a DarkMatter node by fetching /.well-known/darkmatter.json.

    Args:
        params: Contains domain to check (e.g. 'example.com' or 'localhost:8100').

    Returns:
        JSON with the discovery result.
    """
    domain = params.domain.strip().rstrip("/")
    if "://" not in domain:
        url = f"https://{domain}/.well-known/darkmatter.json"
    else:
        url = f"{domain}/.well-known/darkmatter.json"

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            # Try HTTPS first, fall back to HTTP for localhost/private
            resp = None
            try:
                resp = await client.get(url)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if url.startswith("https://"):
                    url = url.replace("https://", "http://", 1)
                    resp = await client.get(url)

            if resp is None:
                return json.dumps({"found": False, "error": "Could not connect."})

            # SSRF protection: block redirects to private/internal IPs
            final_host = resp.url.host
            if final_host and is_private_ip(final_host):
                return json.dumps({"found": False, "error": "Redirect to private IP blocked (SSRF protection)."})

            if resp.status_code != 200:
                return json.dumps({"found": False, "error": f"HTTP {resp.status_code}"})

            data = resp.json()
            if not data.get("darkmatter"):
                return json.dumps({"found": False, "error": "Response missing 'darkmatter: true'."})

            return json.dumps({"found": True, **data})
    except Exception as e:
        return json.dumps({"found": False, "error": str(e)})


@mcp.tool(
    name="darkmatter_discover_local",
    annotations={
        "title": "Discover Local Peers",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def discover_local(ctx: Context) -> str:
    """List DarkMatter agents discovered on the local network via LAN broadcast.

    LAN discovery is enabled by default. Returns the current list of
    peers seen via UDP broadcast. Stale peers (>90s unseen) are automatically pruned.

    Returns:
        JSON with the list of discovered LAN peers.
    """
    state = get_state(ctx)
    now = time.time()

    # Prune stale peers
    stale = [k for k, v in state.discovered_peers.items() if now - v.get("ts", 0) > DISCOVERY_MAX_AGE]
    for k in stale:
        del state.discovered_peers[k]

    peers = []
    for agent_id, info in state.discovered_peers.items():
        peers.append({
            "agent_id": agent_id,
            "url": info.get("url", ""),
            "bio": info.get("bio", ""),
            "status": info.get("status", ""),
            "accepting": info.get("accepting", True),
            "last_seen": info.get("ts", 0),
        })

    discovery_enabled = os.environ.get("DARKMATTER_DISCOVERY", "true").lower() == "true"
    return json.dumps({
        "discovery_enabled": discovery_enabled,
        "total": len(peers),
        "peers": peers,
    })


# =============================================================================
# Impressions — Local reputation / trust signals
# =============================================================================

@mcp.tool(
    name="darkmatter_set_impression",
    annotations={
        "title": "Set Impression",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def set_impression(params: SetImpressionInput, ctx: Context) -> str:
    """Store or update your scored impression of an agent.

    Impressions are scored trust signals (-1.0 to 1.0) with optional notes.
    Your scores are shared with peers when they receive connection requests —
    this is how trust propagates through the network.

    Args:
        params: Contains agent_id, score (-1.0 to 1.0), and optional note.

    Returns:
        JSON confirming the impression was saved.
    """
    state = get_state(ctx)

    was_update = params.agent_id in state.impressions
    state.impressions[params.agent_id] = Impression(score=params.score, note=params.note)
    save_state()

    return json.dumps({
        "success": True,
        "agent_id": params.agent_id,
        "action": "updated" if was_update else "created",
        "score": params.score,
        "note": params.note,
    })


@mcp.tool(
    name="darkmatter_get_impression",
    annotations={
        "title": "Get Impression",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_impression(params: GetImpressionInput, ctx: Context) -> str:
    """Get your stored impression of an agent.

    Args:
        params: Contains agent_id to look up.

    Returns:
        JSON with the impression, or a message that no impression exists.
    """
    state = get_state(ctx)

    impression = state.impressions.get(params.agent_id)
    if impression is None:
        return json.dumps({
            "agent_id": params.agent_id,
            "has_impression": False,
        })

    return json.dumps({
        "agent_id": params.agent_id,
        "has_impression": True,
        "score": impression.score,
        "note": impression.note,
    })


# =============================================================================
# Gas Economy Configuration Tool
# =============================================================================


@mcp.tool(
    name="darkmatter_set_superagent",
    annotations={
        "title": "Set Superagent",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def set_superagent(params: SetSuperagentInput, ctx: Context) -> str:
    """Set the default superagent URL for gas routing.

    The superagent receives gas fees on timeout (when no elder is found).
    Set to null to reset to the default anchor node.

    Args:
        params: Contains url (or null to reset).

    Returns:
        JSON confirming the update.
    """
    state = get_state(ctx)
    old_url = state.superagent_url

    if params.url:
        url_err = validate_url(params.url)
        if url_err:
            return json.dumps({"success": False, "error": url_err})
        state.superagent_url = params.url.rstrip("/")
    else:
        state.superagent_url = None

    # Clear cache for old URL
    if old_url and old_url in _superagent_wallet_cache:
        del _superagent_wallet_cache[old_url]

    save_state()

    effective = state.superagent_url or SUPERAGENT_DEFAULT_URL
    return json.dumps({
        "success": True,
        "superagent_url": state.superagent_url,
        "effective_url": effective,
        "reset_to_default": params.url is None,
    })


# =============================================================================
# Rate Limit Configuration Tool
# =============================================================================

class SetRateLimitInput(BaseModel):
    agent_id: Optional[str] = Field(None, description="Agent ID to set per-connection rate limit for. Omit to set global rate limit.")
    limit: int = Field(..., description="Max requests per 60s window. 0 = use default, -1 = unlimited.")

@mcp.tool(
    name="darkmatter_set_rate_limit",
    annotations={
        "title": "Set Rate Limit",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def set_rate_limit(params: SetRateLimitInput, ctx: Context) -> str:
    """Set rate limits for incoming requests.

    Per-connection: limits how many requests a specific peer can send per minute.
    Global: limits total inbound requests from all peers per minute.

    Values:
      0 = use default (per-connection: 30/min, global: 200/min)
      -1 = unlimited
      >0 = custom limit

    Args:
        params: Contains optional agent_id (for per-connection) and limit.

    Returns:
        JSON confirming the rate limit was set.
    """
    state = get_state(ctx)

    if params.agent_id:
        conn = state.connections.get(params.agent_id)
        if not conn:
            return json.dumps({"error": f"Not connected to agent {params.agent_id}"})
        conn.rate_limit = params.limit
        save_state()
        effective = params.limit if params.limit != 0 else DEFAULT_RATE_LIMIT_PER_CONNECTION
        label = "unlimited" if params.limit == -1 else f"{effective}/min"
        return json.dumps({
            "success": True,
            "agent_id": params.agent_id,
            "rate_limit": params.limit,
            "effective": label,
        })
    else:
        state.rate_limit_global = params.limit
        save_state()
        effective = params.limit if params.limit != 0 else DEFAULT_RATE_LIMIT_GLOBAL
        label = "unlimited" if params.limit == -1 else f"{effective}/min"
        return json.dumps({
            "success": True,
            "scope": "global",
            "rate_limit": params.limit,
            "effective": label,
        })


# =============================================================================
# Solana Wallet Tools
# =============================================================================

@mcp.tool(
    name="darkmatter_get_balance",
    annotations={
        "title": "Get Wallet Balance",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def get_balance(params: GetBalanceInput, ctx: Context) -> str:
    """Check SOL or SPL token balance for this agent's wallet.

    Omit mint for SOL balance. Provide mint address for SPL token balance.

    Returns:
        JSON with balance information.
    """
    state = get_state(ctx)
    result = await get_solana_balance(state.wallets, mint=params.mint)
    return json.dumps(result)


@mcp.tool(
    name="darkmatter_send_sol",
    annotations={
        "title": "Send SOL",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def send_sol(params: SendSolInput, ctx: Context) -> str:
    """Send SOL to a connected agent's Solana wallet.

    Looks up the recipient's wallet from your connections, builds and sends the transfer.
    Optionally notifies the recipient via a DarkMatter message.

    Args:
        params: agent_id, amount (SOL), notify flag.

    Returns:
        JSON with transaction signature and details.
    """
    state = get_state(ctx)
    conn = state.connections.get(params.agent_id)
    if not conn:
        return json.dumps({"success": False, "error": f"Not connected to agent '{params.agent_id}'"})
    conn_sol = conn.wallets.get("solana")
    if not conn_sol:
        return json.dumps({"success": False, "error": f"Agent '{params.agent_id}' has no Solana wallet"})

    result = await send_solana_sol(state.private_key_hex, state.wallets, conn_sol, params.amount)
    if result.get("success"):
        result["to_agent_id"] = params.agent_id
        if params.notify:
            try:
                notify_params = SendMessageInput(
                    content=f"Sent {params.amount} SOL — tx: {result['tx_signature']}",
                    target_agent_id=params.agent_id,
                    metadata={
                        "type": "solana_payment",
                        "amount": params.amount,
                        "token": "SOL",
                        "tx_signature": result["tx_signature"],
                        "from_wallet": result["from_wallet"],
                        "to_wallet": conn_sol,
                        "gas_eligible": True,
                        "gas_rate": GAS_RATE,
                        "sender_created_at": state.created_at,
                        "sender_superagent_wallet": await _get_superagent_wallet(state) or "",
                    },
                )
                await _send_new_message(state, notify_params)
                result["notification_sent"] = True
            except Exception:
                result["notification_sent"] = False

    return json.dumps(result)


@mcp.tool(
    name="darkmatter_send_token",
    annotations={
        "title": "Send SPL Token",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def send_token(params: SendTokenInput, ctx: Context) -> str:
    """Send SPL tokens to a connected agent's Solana wallet.

    Auto-creates the recipient's token account if it doesn't exist (sender pays the rent).
    Accepts token name (e.g. "USDC") or raw mint address.

    Args:
        params: agent_id, mint (name or address), amount, decimals, notify flag.

    Returns:
        JSON with transaction signature and details.
    """
    state = get_state(ctx)
    conn = state.connections.get(params.agent_id)
    if not conn:
        return json.dumps({"success": False, "error": f"Not connected to agent '{params.agent_id}'"})
    conn_sol = conn.wallets.get("solana")
    if not conn_sol:
        return json.dumps({"success": False, "error": f"Agent '{params.agent_id}' has no Solana wallet"})

    # Resolve token name to mint address if known
    mint = params.mint
    decimals = params.decimals
    resolved = _resolve_spl_token(params.mint)
    if resolved:
        mint, decimals = resolved

    result = await send_solana_token(state.private_key_hex, state.wallets, conn_sol, mint, params.amount, decimals)
    if result.get("success"):
        result["to_agent_id"] = params.agent_id
        if params.notify:
            try:
                token_label = params.mint if not resolved else params.mint.upper()
                notify_params = SendMessageInput(
                    content=f"Sent {params.amount} {token_label} — tx: {result['tx_signature']}",
                    target_agent_id=params.agent_id,
                    metadata={
                        "type": "solana_payment",
                        "amount": params.amount,
                        "token": mint,
                        "decimals": decimals,
                        "tx_signature": result["tx_signature"],
                        "from_wallet": result["from_wallet"],
                        "to_wallet": conn_sol,
                        "gas_eligible": True,
                        "gas_rate": GAS_RATE,
                        "sender_created_at": state.created_at,
                        "sender_superagent_wallet": await _get_superagent_wallet(state) or "",
                    },
                )
                await _send_new_message(state, notify_params)
                result["notification_sent"] = True
            except Exception:
                result["notification_sent"] = False

    return json.dumps(result)


# =============================================================================
# Unified Multi-Chain Wallet Tools
# =============================================================================


@mcp.tool(
    name="darkmatter_wallet_balances",
    annotations={
        "title": "Wallet Balances",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def wallet_balances(params: WalletBalancesInput, ctx: Context) -> str:
    """Show all wallets with native balances across chains.

    For each chain in your wallets, fetches the native balance.
    Chains without their SDK installed return address but balance: null.

    Args:
        params: Optional chain filter.

    Returns:
        JSON with wallet addresses and balances per chain.
    """
    state = get_state(ctx)
    if not state.wallets:
        return json.dumps({"success": False, "error": "No wallets configured"})

    chains = state.wallets
    if params.chain:
        if params.chain not in chains:
            return json.dumps({"success": False, "error": f"No wallet for chain '{params.chain}'"})
        chains = {params.chain: chains[params.chain]}

    results = []
    for chain, address in chains.items():
        entry = {"chain": chain, "address": address, "balance": None, "unit": None}

        if chain == "solana" and SOLANA_AVAILABLE:
            try:
                pubkey = SolanaPubkey.from_string(address)
                async with SolanaClient(SOLANA_RPC_URL) as client:
                    resp = await client.get_balance(pubkey)
                    entry["balance"] = resp.value / LAMPORTS_PER_SOL
                    entry["unit"] = "SOL"
            except Exception as e:
                entry["error"] = str(e)
        elif chain == "solana":
            entry["note"] = "solana/solders not installed"
        else:
            entry["note"] = f"{chain} SDK not yet implemented"

        results.append(entry)

    return json.dumps({"success": True, "wallets": results})


@mcp.tool(
    name="darkmatter_wallet_send",
    annotations={
        "title": "Send (Any Chain)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def wallet_send(params: WalletSendInput, ctx: Context) -> str:
    """Send native currency to a connected agent on any chain.

    Dispatches to chain-specific logic. Currently only Solana is implemented.

    Args:
        params: agent_id, amount, chain (default: solana), notify flag.

    Returns:
        JSON with transaction details.
    """
    state = get_state(ctx)

    if params.chain not in state.wallets:
        return json.dumps({"success": False, "error": f"No wallet for chain '{params.chain}'"})

    conn = state.connections.get(params.agent_id)
    if not conn:
        return json.dumps({"success": False, "error": f"Not connected to agent '{params.agent_id}'"})

    if params.chain not in conn.wallets:
        return json.dumps({"success": False, "error": f"Agent '{params.agent_id}' has no {params.chain} wallet"})

    if params.chain == "solana":
        if not SOLANA_AVAILABLE:
            return json.dumps({"success": False, "error": "Solana SDK not installed"})
        # Delegate to existing send_sol logic
        sol_params = SendSolInput(agent_id=params.agent_id, amount=params.amount, notify=params.notify)
        return await send_sol(sol_params, ctx)

    return json.dumps({"success": False, "error": f"Chain '{params.chain}' send not yet implemented"})


# =============================================================================
# WebRTC Transport Upgrade Tool
# =============================================================================

async def _attempt_webrtc_upgrade(state, conn: Connection) -> None:
    """Attempt to upgrade a connection to WebRTC. Silently fails on error."""
    if not WEBRTC_AVAILABLE:
        return

    agent_id = conn.agent_id

    # Already upgraded?
    if conn.transport == "webrtc" and conn.webrtc_channel is not None:
        ready = getattr(conn.webrtc_channel, "readyState", None)
        if ready == "open":
            return

    # Clean up any stale state
    if conn.webrtc_pc is not None:
        _cleanup_webrtc(conn)

    try:
        pc = RTCPeerConnection(configuration=_make_rtc_config())
        channel = pc.createDataChannel("darkmatter")
        channel_open = asyncio.Event()

        @channel.on("open")
        def on_open():
            channel_open.set()

        @channel.on("message")
        async def on_message(message):
            try:
                envelope = json.loads(message)
                path = envelope.get("path", "")
                payload = envelope.get("payload", {})
                if path == "/__darkmatter__/message":
                    result, status_code = await _process_incoming_message(state, payload)
                    if status_code >= 400:
                        print(f"[DarkMatter] WebRTC message rejected ({status_code}): {result.get('error', 'unknown')}", file=sys.stderr)
            except Exception as e:
                print(f"[DarkMatter] WebRTC message processing error: {e}", file=sys.stderr)

        @channel.on("close")
        def on_close():
            _cleanup_webrtc(conn)

        @pc.on("connectionstatechange")
        async def on_connection_state_change():
            if pc.connectionState in ("failed", "closed"):
                _cleanup_webrtc(conn)

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        await _wait_for_ice_gathering(pc)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                conn.agent_url.rstrip("/") + "/__darkmatter__/webrtc_offer",
                json={"from_agent_id": state.agent_id, "sdp_offer": pc.localDescription.sdp},
            )
            if resp.status_code != 200:
                await pc.close()
                return
            answer_data = resp.json()

        sdp_answer = answer_data.get("sdp_answer", "")
        if not sdp_answer:
            await pc.close()
            return

        answer = RTCSessionDescription(sdp=sdp_answer, type="answer")
        await pc.setRemoteDescription(answer)

        await asyncio.wait_for(channel_open.wait(), timeout=WEBRTC_CHANNEL_OPEN_TIMEOUT)

        conn.webrtc_pc = pc
        conn.webrtc_channel = channel
        conn.transport = "webrtc"

        peer_label = conn.agent_display_name or agent_id
        print(f"[DarkMatter] WebRTC: auto-upgraded connection to {peer_label}", file=sys.stderr)

    except Exception as e:
        print(f"[DarkMatter] WebRTC auto-upgrade failed for {conn.agent_display_name or agent_id[:12]}: {e}", file=sys.stderr)


# =============================================================================
# Live Status Tool — Dynamic description updated via notifications/tools/list_changed
# =============================================================================

@mcp.tool(
    name="darkmatter_status",
    annotations={
        "title": "Live Node Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def live_status(ctx: Context) -> str:
    """DarkMatter live node status dashboard. Current state is shown below — no need to call unless you want full details.

    LIVE STATUS: Waiting for first status update... This will show live node state and action items you should respond to.
    """
    _track_session(ctx)
    return _build_status_line()


# =============================================================================
# Shared Pure Logic — Framework-agnostic functions used by both Starlette
# handlers (server.py) and Flask handlers (entrypoint.py).
#
# Convention: process_* takes (state, data_dict) and returns (response_dict, status_code).
# build_* constructs payloads or objects without side effects.
# =============================================================================


async def process_connection_request(state: AgentState, data: dict, public_url: str) -> tuple[dict, int]:
    """Process an incoming connection request. Returns (response_dict, status_code).

    public_url: the public URL of this agent (caller provides it since Flask/Starlette differ).
    """
    if state.status == AgentStatus.INACTIVE:
        return {"error": "Agent is currently inactive"}, 503

    rate_err = _check_rate_limit(state)
    if rate_err:
        return {"error": rate_err}, 429

    from_agent_id = data.get("from_agent_id", "")
    from_agent_url = data.get("from_agent_url", "")
    from_agent_bio = data.get("from_agent_bio", "")
    from_agent_public_key_hex = data.get("from_agent_public_key_hex")
    from_agent_display_name = data.get("from_agent_display_name")
    from_agent_wallets = data.get("wallets") or (
        {"solana": data["from_agent_wallet_address"]} if data.get("from_agent_wallet_address") else {}
    )
    from_agent_created_at = data.get("created_at")
    mutual = data.get("mutual", False)

    if not from_agent_id or not from_agent_url:
        return {"error": "Missing required fields"}, 400
    if len(from_agent_id) > MAX_AGENT_ID_LENGTH:
        return {"error": "agent_id too long"}, 400
    if len(from_agent_bio) > MAX_BIO_LENGTH:
        from_agent_bio = from_agent_bio[:MAX_BIO_LENGTH]
    url_err = validate_url(from_agent_url)
    if url_err:
        return {"error": url_err}, 400

    # Already connected — update info and return auto_accepted
    if from_agent_id in state.connections:
        existing = state.connections[from_agent_id]
        changed = False
        if from_agent_public_key_hex and not existing.agent_public_key_hex:
            existing.agent_public_key_hex = from_agent_public_key_hex
            existing.agent_display_name = from_agent_display_name
            changed = True
        if from_agent_wallets and not existing.wallets:
            existing.wallets = from_agent_wallets
            changed = True
        if changed:
            save_state()
        return {
            "auto_accepted": True,
            "agent_id": state.agent_id,
            "agent_url": public_url,
            "agent_bio": state.bio,
            "agent_public_key_hex": state.public_key_hex,
            "agent_display_name": state.display_name,
            "wallets": state.wallets,
            "wallet_address": state.wallets.get("solana"),
            "created_at": state.created_at,
            "message": "Already connected.",
        }, 200

    # Queue the request
    if len(state.pending_requests) >= MESSAGE_QUEUE_MAX:
        return {"error": "Too many pending requests"}, 429

    request_id = f"req-{uuid.uuid4().hex[:8]}"
    peer_trust = await _gather_peer_trust(state, from_agent_id)
    state.pending_requests[request_id] = PendingConnectionRequest(
        request_id=request_id,
        from_agent_id=from_agent_id,
        from_agent_url=from_agent_url,
        from_agent_bio=from_agent_bio,
        from_agent_public_key_hex=from_agent_public_key_hex,
        from_agent_display_name=from_agent_display_name,
        from_agent_wallets=from_agent_wallets,
        from_agent_created_at=from_agent_created_at,
        peer_trust=peer_trust,
        mutual=mutual,
    )

    return {
        "auto_accepted": False,
        "request_id": request_id,
        "agent_id": state.agent_id,
        "message": "Connection request queued. Awaiting agent decision.",
    }, 200


def process_connection_accepted(state: AgentState, data: dict) -> tuple[dict, int]:
    """Process notification that our connection request was accepted. Returns (response_dict, status_code)."""
    agent_id = data.get("agent_id", "")
    agent_url = data.get("agent_url", "")
    agent_bio = data.get("agent_bio", "")
    agent_public_key_hex = data.get("agent_public_key_hex")
    agent_display_name = data.get("agent_display_name")
    agent_wallets = data.get("wallets") or (
        {"solana": data["wallet_address"]} if data.get("wallet_address") else {}
    )

    if not agent_id or not agent_url:
        return {"error": "Missing required fields"}, 400
    if len(agent_id) > MAX_AGENT_ID_LENGTH:
        return {"error": "agent_id too long"}, 400
    if len(agent_bio) > MAX_BIO_LENGTH:
        agent_bio = agent_bio[:MAX_BIO_LENGTH]
    url_err = validate_url(agent_url)
    if url_err:
        return {"error": url_err}, 400

    # Match pending outbound
    agent_base = agent_url.rstrip("/").rsplit("/mcp", 1)[0].rstrip("/")
    matched = None
    for pending_url in state.pending_outbound:
        pending_base = pending_url.rsplit("/mcp", 1)[0].rstrip("/")
        if pending_base == agent_base:
            matched = pending_url
            break
    if matched is None and agent_id:
        for pending_url, pending_agent_id in state.pending_outbound.items():
            if pending_agent_id == agent_id:
                matched = pending_url
                break
    if matched is None:
        return {"error": "No pending outbound connection request for this agent."}, 403

    del state.pending_outbound[matched]

    conn = Connection(
        agent_id=agent_id,
        agent_url=agent_url,
        agent_bio=agent_bio,
        agent_public_key_hex=agent_public_key_hex,
        agent_display_name=agent_display_name,
        wallets=agent_wallets,
        peer_created_at=data.get("created_at"),
    )
    state.connections[agent_id] = conn
    save_state()

    return {"success": True}, 200


def process_accept_pending(state: AgentState, request_id: str, public_url: str) -> tuple[dict, int, dict | None]:
    """Accept a pending connection request. Returns (response_dict, status_code, notify_payload_or_None).

    The caller is responsible for POSTing notify_payload to the requester's
    /__darkmatter__/connection_accepted endpoint.
    """
    pending = state.pending_requests.get(request_id)
    if not pending:
        return {"error": f"No pending request with ID '{request_id}'"}, 404, None

    if len(state.connections) >= MAX_CONNECTIONS:
        return {"error": f"Connection limit reached ({MAX_CONNECTIONS})"}, 429, None

    conn = Connection(
        agent_id=pending.from_agent_id,
        agent_url=pending.from_agent_url,
        agent_bio=pending.from_agent_bio,
        agent_public_key_hex=pending.from_agent_public_key_hex,
        agent_display_name=pending.from_agent_display_name,
        wallets=pending.from_agent_wallets,
        peer_created_at=pending.from_agent_created_at,
    )
    state.connections[pending.from_agent_id] = conn

    notify_payload = {
        "agent_id": state.agent_id,
        "agent_url": public_url,
        "agent_bio": state.bio,
        "agent_public_key_hex": state.public_key_hex,
        "agent_display_name": state.display_name,
        "wallets": state.wallets,
        "wallet_address": state.wallets.get("solana"),
        "created_at": state.created_at,
    }

    is_mutual = getattr(pending, "mutual", False)
    del state.pending_requests[request_id]
    save_state()

    return {
        "success": True,
        "accepted": True,
        "agent_id": pending.from_agent_id,
        "mutual": is_mutual,
    }, 200, notify_payload


def build_outbound_request_payload(state: AgentState, public_url: str, mutual: bool = False) -> dict:
    """Build the payload dict for sending a connection request to another agent."""
    payload = {
        "from_agent_id": state.agent_id,
        "from_agent_url": public_url,
        "from_agent_bio": state.bio,
        "from_agent_public_key_hex": state.public_key_hex,
        "from_agent_display_name": state.display_name,
        "wallets": state.wallets,
        "from_agent_wallet_address": state.wallets.get("solana"),
        "created_at": state.created_at,
    }
    if mutual:
        payload["mutual"] = True
    return payload


def build_connection_from_accepted(result_data: dict) -> Connection:
    """Build a Connection from an auto-accepted or accepted response."""
    peer_wallets = result_data.get("wallets") or (
        {"solana": result_data["wallet_address"]} if result_data.get("wallet_address") else {}
    )
    return Connection(
        agent_id=result_data["agent_id"],
        agent_url=result_data.get("agent_url", ""),
        agent_bio=result_data.get("agent_bio", ""),
        agent_public_key_hex=result_data.get("agent_public_key_hex"),
        agent_display_name=result_data.get("agent_display_name"),
        wallets=peer_wallets,
        peer_created_at=result_data.get("created_at"),
    )


def process_gas_match(data: dict) -> tuple[dict, int]:
    """Stateless match game endpoint. Returns (response_dict, status_code)."""
    n = data.get("n")
    if not isinstance(n, int) or n < 1:
        return {"error": "Invalid n"}, 400
    pick = random.randint(0, n)
    return {"pick": pick}, 200


async def process_gas_signal(state: AgentState, data: dict) -> tuple[dict, int]:
    """Process a forwarded gas signal and start the match game. Returns (response_dict, status_code)."""
    try:
        gas = _gas_signal_from_dict(data)
    except (KeyError, TypeError) as e:
        return {"error": f"Invalid gas signal: {e}"}, 400

    if gas.hops >= gas.max_hops:
        return {"error": "Signal expired (max hops)"}, 400

    if gas.created_at:
        try:
            created = datetime.fromisoformat(gas.created_at)
            age = (datetime.now(timezone.utc) - created).total_seconds()
            if age > GAS_MAX_AGE_S:
                return {"error": "Signal expired (age)"}, 400
        except (ValueError, TypeError):
            pass

    if state.agent_id in gas.path:
        return {"error": "Loop detected"}, 400

    _log_gas_event(state, {
        "type": "gas_signal_received",
        "signal_id": gas.signal_id,
        "hops": gas.hops,
        "from": gas.path[-1] if gas.path else gas.sender_agent_id,
    })

    # Start match game as background task
    asyncio.create_task(_run_match_game(state, gas, is_originator=False))

    return {"accepted": True}, 200


async def process_gas_result(state: AgentState, data: dict) -> tuple[dict, int]:
    """Process a gas resolution callback. B sends gas to destination. Returns (response_dict, status_code)."""
    signal_id = data.get("signal_id", "")
    dest_wallet = data.get("destination_wallet", "")
    resolved_by = data.get("resolved_by", "")
    resolution = data.get("resolution", "resolved")

    if not signal_id:
        return {"error": "Missing signal_id"}, 400

    original_entry = None
    for entry in state.gas_log:
        if entry.get("signal_id") == signal_id and entry.get("type") == "gas_initiated":
            original_entry = entry
            break

    if not original_entry:
        return {"error": "Unknown signal_id"}, 404

    for entry in state.gas_log:
        if entry.get("signal_id") == signal_id and entry.get("type") in ("gas_sent", "gas_kept"):
            return {"status": "already_resolved"}, 200

    amount = original_entry.get("amount", 0)
    token = original_entry.get("token", "SOL")
    token_decimals = original_entry.get("token_decimals", 9)

    if dest_wallet and state.private_key_hex and amount > 0:
        try:
            if token == "SOL":
                result = await send_solana_sol(
                    state.private_key_hex, state.wallets, dest_wallet, amount
                )
            else:
                result = await send_solana_token(
                    state.private_key_hex, state.wallets, dest_wallet,
                    token, amount, token_decimals
                )

            _log_gas_event(state, {
                "type": "gas_sent",
                "signal_id": signal_id,
                "resolution": resolution,
                "destination": dest_wallet,
                "amount": amount,
                "token": token,
                "tx_success": result.get("success", False),
                "tx_signature": result.get("tx_signature"),
                "resolved_by": resolved_by,
            })

            if result.get("success") and resolved_by:
                _adjust_trust(state, resolved_by, 0.01)

        except Exception as e:
            _log_gas_event(state, {
                "type": "gas_send_failed",
                "signal_id": signal_id,
                "error": str(e),
            })
    elif resolution == "timeout" and original_entry.get("sender_superagent_wallet"):
        sa_wallet = original_entry["sender_superagent_wallet"]
        if state.private_key_hex and amount > 0:
            try:
                if token == "SOL":
                    await send_solana_sol(state.private_key_hex, state.wallets, sa_wallet, amount)
                else:
                    await send_solana_token(
                        state.private_key_hex, state.wallets, sa_wallet,
                        token, amount, token_decimals
                    )
                _log_gas_event(state, {
                    "type": "gas_sent",
                    "signal_id": signal_id,
                    "resolution": "timeout",
                    "destination": sa_wallet,
                    "amount": amount,
                })
            except Exception:
                pass
    else:
        _log_gas_event(state, {
            "type": "gas_kept",
            "signal_id": signal_id,
            "reason": "no_destination",
            "amount": amount,
        })

    save_state()
    return {"status": "resolved"}, 200


# =============================================================================
# HTTP Endpoints — Agent-to-Agent Communication Layer
#
# These are the raw HTTP endpoints that agents call on each other.
# They sit underneath the MCP tools and handle the actual mesh protocol.
# =============================================================================

from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
import uvicorn


async def _gather_peer_trust(state, about_agent_id: str) -> dict:
    """Query all connected peers for their impression of an agent. Returns aggregated trust data."""

    async def _query_peer(conn):
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(
                    conn.agent_url.rstrip("/") + f"/__darkmatter__/impression/{about_agent_id}",
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("has_impression"):
                        return {
                            "agent_id": conn.agent_id,
                            "score": data.get("score", 0.0),
                            "note": data.get("note", ""),
                        }
        except Exception:
            pass
        return None

    tasks = [_query_peer(conn) for conn in state.connections.values()]
    if not tasks:
        return {"peers_queried": 0, "peers_with_opinion": 0, "avg_score": None}

    gathered = await asyncio.gather(*tasks)
    opinions = [r for r in gathered if r is not None]

    avg_score = None
    if opinions:
        avg_score = round(sum(o["score"] for o in opinions) / len(opinions), 2)

    # Find most trusted recommender: the peer we scored highest among those with an opinion
    most_trusted = None
    if opinions:
        opinion_ids = {o["agent_id"] for o in opinions}
        best_score = -2.0
        for aid, imp in state.impressions.items():
            if aid in opinion_ids and imp.score > best_score:
                best_score = imp.score
                most_trusted = aid
        if most_trusted:
            rec = next(o for o in opinions if o["agent_id"] == most_trusted)
            most_trusted = {
                "agent_id": most_trusted,
                "your_trust_in_them": best_score,
                "their_score": rec["score"],
                "their_note": rec["note"],
            }

    return {
        "peers_queried": len(tasks),
        "peers_with_opinion": len(opinions),
        "avg_score": avg_score,
        "most_trusted_recommender": most_trusted,
    }


async def handle_connection_request(request: Request) -> JSONResponse:
    """Handle an incoming connection request from another agent."""
    global _agent_state
    state = _agent_state
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    public_url = f"{_get_public_url(state.port)}/mcp"
    result, status = await process_connection_request(state, data, public_url)
    return JSONResponse(result, status_code=status)


async def handle_connection_accepted(request: Request) -> JSONResponse:
    """Handle notification that our connection request was accepted."""
    global _agent_state
    state = _agent_state
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    result, status = process_connection_accepted(state, data)
    if status == 200 and WEBRTC_AVAILABLE:
        agent_id = data.get("agent_id", "")
        conn = state.connections.get(agent_id)
        if conn:
            asyncio.create_task(_attempt_webrtc_upgrade(state, conn))
    return JSONResponse(result, status_code=status)


async def handle_accept_pending(request: Request) -> JSONResponse:
    """Accept a pending connection request via HTTP (no MCP needed)."""
    global _agent_state
    state = _agent_state
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    request_id = data.get("request_id", "")
    if not request_id:
        return JSONResponse({"error": "Missing request_id"}, status_code=400)

    public_url = f"{_get_public_url(state.port)}/mcp"
    result, status, notify_payload = process_accept_pending(state, request_id, public_url)

    if status == 200 and notify_payload:
        # Notify the requesting agent
        from_url = notify_payload.get("_from_agent_url", "")
        # Get the from_agent_url from the connection we just created
        conn = state.connections.get(result.get("agent_id", ""))
        if conn:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    base = conn.agent_url.rstrip("/")
                    for suffix in ("/mcp", "/__darkmatter__"):
                        if base.endswith(suffix):
                            base = base[:-len(suffix)]
                            break
                    await client.post(
                        base + "/__darkmatter__/connection_accepted",
                        json=notify_payload,
                    )
            except Exception:
                pass

            # Auto WebRTC upgrade
            if WEBRTC_AVAILABLE:
                asyncio.create_task(_attempt_webrtc_upgrade(state, conn))

    return JSONResponse(result, status_code=status)


async def _process_incoming_message(state: AgentState, data: dict) -> tuple[dict, int]:
    """Core message processing logic, shared by HTTP and WebRTC receive paths.

    Returns (response_dict, status_code).
    """
    if state.status == AgentStatus.INACTIVE:
        return {"error": "Agent is currently inactive"}, 503

    if len(state.message_queue) >= MESSAGE_QUEUE_MAX:
        return {"error": "Message queue full"}, 429

    message_id = data.get("message_id", "")
    content = data.get("content", "")
    webhook = data.get("webhook", "")
    from_agent_id = data.get("from_agent_id")

    if not message_id or not content or not webhook:
        return {"error": "Missing required fields"}, 400
    if _check_message_replay(message_id):
        return {"error": "Duplicate message — already received"}, 409
    if len(content) > MAX_CONTENT_LENGTH:
        return {"error": f"Content exceeds {MAX_CONTENT_LENGTH} bytes"}, 413
    if from_agent_id and len(from_agent_id) > MAX_AGENT_ID_LENGTH:
        return {"error": "from_agent_id too long"}, 400
    url_err = validate_url(webhook)
    if url_err:
        return {"error": f"Invalid webhook: {url_err}"}, 400

    if not from_agent_id:
        return {"error": "Missing from_agent_id."}, 400

    # Rate limit check (only applied to connected agents)
    conn_for_rate = state.connections.get(from_agent_id)
    rate_err = _check_rate_limit(state, conn_for_rate)
    if rate_err:
        if conn_for_rate:
            conn_for_rate.messages_declined += 1
        return {"error": rate_err}, 429

    hops_remaining = data.get("hops_remaining", 10)
    if not isinstance(hops_remaining, int) or hops_remaining < 0:
        hops_remaining = 10

    # Cryptographic verification
    msg_timestamp = data.get("timestamp", "")
    from_public_key_hex = data.get("from_public_key_hex")
    signature_hex = data.get("signature_hex")
    verified = False
    is_connected = from_agent_id in state.connections

    if is_connected:
        conn = state.connections[from_agent_id]
        if conn.agent_public_key_hex:
            # We have a stored public key for this peer — signature is REQUIRED
            if from_public_key_hex and conn.agent_public_key_hex != from_public_key_hex:
                return {"error": "Public key mismatch — sender key does not match stored key for this connection."}, 403
            if not signature_hex or not msg_timestamp:
                return {"error": "Signature required — this connection has a known public key."}, 403
            if not _verify_message(conn.agent_public_key_hex, signature_hex,
                                   from_agent_id, message_id, msg_timestamp, content):
                return {"error": "Invalid signature — message authenticity could not be verified."}, 403
            verified = True
        elif from_public_key_hex:
            # Peer sent a key but we don't have one stored — pin it and verify
            if signature_hex and msg_timestamp:
                if not _verify_message(from_public_key_hex, signature_hex,
                                       from_agent_id, message_id, msg_timestamp, content):
                    return {"error": "Invalid signature — message authenticity could not be verified."}, 403
                conn.agent_public_key_hex = from_public_key_hex
                verified = True
    elif from_public_key_hex and signature_hex and msg_timestamp:
        # Not connected, but accept if the message is cryptographically signed.
        # Connections manage trust/rate-limiting; signatures prove identity.
        if not _verify_message(from_public_key_hex, signature_hex,
                               from_agent_id, message_id, msg_timestamp, content):
            return {"error": "Invalid signature — message authenticity could not be verified."}, 403
        verified = True
    else:
        # No connection AND no signature — reject
        return {"error": "Not connected — unsigned messages require a connection."}, 403

    # Replay protection: reject messages with stale timestamps
    if verified and msg_timestamp and not _is_timestamp_fresh(msg_timestamp):
        return {"error": "Message timestamp too old — possible replay"}, 403

    msg = QueuedMessage(
        message_id=truncate_field(message_id, 128),
        content=content,
        webhook=webhook,
        hops_remaining=hops_remaining,
        metadata=data.get("metadata", {}),
        from_agent_id=from_agent_id,
        verified=verified,
    )
    state.message_queue.append(msg)
    state.messages_handled += 1

    # Update telemetry for the sending agent
    if msg.from_agent_id and msg.from_agent_id in state.connections:
        conn = state.connections[msg.from_agent_id]
        conn.messages_received += 1
        conn.last_activity = datetime.now(timezone.utc).isoformat()

    save_state()

    # Notify originator that message was received
    webhook_err = validate_webhook_url(msg.webhook)
    if not webhook_err:
        try:
            await _webhook_request_with_recovery(
                state, msg.webhook, msg.from_agent_id,
                method="POST", timeout=10.0,
                json={"type": "received", "agent_id": state.agent_id}
            )
        except Exception:
            pass  # Best-effort notification

    # Gas economy: if this is a payment notification with gas_eligible flag, initiate match game
    msg_meta = msg.metadata or {}
    if (msg_meta.get("type") == "solana_payment"
            and msg_meta.get("gas_eligible")
            and msg_meta.get("amount")
            and msg_meta.get("tx_signature")):
        asyncio.create_task(_initiate_gas_from_payment(state, msg))

    # Route message through the extensible router chain
    asyncio.create_task(_execute_routing(state, msg))

    return {"success": True, "queued": True, "queue_position": len(state.message_queue)}, 200


async def handle_message(request: Request) -> JSONResponse:
    """Handle an incoming routed message from another agent (HTTP transport)."""
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    result, status_code = await _process_incoming_message(state, data)
    return JSONResponse(result, status_code=status_code)


def _process_webhook_locally(state, message_id: str, data: dict) -> tuple[dict, int]:
    """Process a webhook callback payload locally. Returns (response_dict, status_code).

    Shared by handle_webhook_post (direct) and relay poll (NAT).
    """
    sm = state.sent_messages.get(message_id)
    if not sm:
        return {"error": f"No sent message with ID '{message_id}'"}, 404

    update_type = data.get("type", "")
    agent_id = data.get("agent_id", "unknown")
    timestamp = datetime.now(timezone.utc).isoformat()

    if update_type == "received":
        sm.updates.append({
            "type": "received",
            "agent_id": agent_id,
            "timestamp": timestamp,
        })
        save_state()
        return {"success": True, "recorded": "received"}, 200

    elif update_type == "responding":
        sm.updates.append({
            "type": "responding",
            "agent_id": agent_id,
            "timestamp": timestamp,
        })
        save_state()
        return {"success": True, "recorded": "responding"}, 200

    elif update_type == "forwarded":
        sm.updates.append({
            "type": "forwarded",
            "agent_id": agent_id,
            "target_agent_id": data.get("target_agent_id", ""),
            "note": data.get("note"),
            "timestamp": timestamp,
        })
        save_state()
        return {"success": True, "recorded": "forwarded"}, 200

    elif update_type == "response":
        sm.responses.append({
            "agent_id": agent_id,
            "response": data.get("response", ""),
            "metadata": data.get("metadata", {}),
            "timestamp": timestamp,
        })
        sm.status = "responded"
        save_state()
        # Wake any wait_for_response waiters on this message
        for evt in state._response_events.pop(message_id, []):
            evt.set()
        return {"success": True, "recorded": "response"}, 200

    elif update_type == "expired":
        sm.updates.append({
            "type": "expired",
            "agent_id": agent_id,
            "note": data.get("note"),
            "timestamp": timestamp,
        })
        save_state()
        return {"success": True, "recorded": "expired"}, 200

    else:
        return {"error": f"Unknown update type: '{update_type}'"}, 400


async def handle_webhook_post(request: Request) -> JSONResponse:
    """Handle incoming webhook updates (forwarding notifications, responses).

    POST /__darkmatter__/webhook/{message_id}

    Body should contain:
    - type: "received" | "responding" | "forwarded" | "response" | "expired"
    - agent_id: the agent posting this update
    - For "received": (no extra fields — signals message was queued)
    - For "responding": (no extra fields — signals agent is actively working on a response)
    - For "forwarded": target_agent_id, optional note
    - For "response": response text, optional metadata (multiple agents may respond)
    - For "expired": optional note
    """
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    # Global rate limit for webhook POSTs
    rate_err = _check_rate_limit(state)
    if rate_err:
        return JSONResponse({"error": rate_err}, status_code=429)

    message_id = request.path_params.get("message_id", "")

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    result, status_code = _process_webhook_locally(state, message_id, data)
    return JSONResponse(result, status_code=status_code)


async def handle_webhook_get(request: Request) -> JSONResponse:
    """Status check for agents holding a message — is it still active?

    GET /__darkmatter__/webhook/{message_id}

    Returns message status and forwarding updates (for loop detection).
    """
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    message_id = request.path_params.get("message_id", "")
    sm = state.sent_messages.get(message_id)
    if not sm:
        return JSONResponse({"error": f"No sent message with ID '{message_id}'"}, status_code=404)

    return JSONResponse({
        "message_id": sm.message_id,
        "status": sm.status,
        "initial_hops": sm.initial_hops,
        "created_at": sm.created_at,
        "updates": sm.updates,
    })


async def handle_status(request: Request) -> JSONResponse:
    """Return this agent's public status (for health checks and discovery)."""
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    return JSONResponse({
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "bio": state.bio,
        "status": state.status.value,
        "num_connections": len(state.connections),
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "spawned_agents": len(_spawned_agents),
    })


async def handle_network_info(request: Request) -> JSONResponse:
    """Return this agent's network info for peer discovery."""
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    peers = [
        {"agent_id": c.agent_id, "agent_url": c.agent_url, "agent_bio": c.agent_bio}
        for c in state.connections.values()
    ]
    return JSONResponse({
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "agent_url": _get_public_url(state.port),
        "bio": state.bio,
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "wallets": state.wallets,
        "peers": peers,
    })


async def handle_impression_get(request: Request) -> JSONResponse:
    """Return this agent's impression of a specific agent (asked by peers)."""
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    rate_err = _check_rate_limit(state)
    if rate_err:
        return JSONResponse({"error": rate_err}, status_code=429)

    about_agent_id = request.path_params.get("agent_id", "")
    impression = state.impressions.get(about_agent_id)

    if impression is None:
        return JSONResponse({
            "agent_id": about_agent_id,
            "has_impression": False,
        })

    return JSONResponse({
        "agent_id": about_agent_id,
        "has_impression": True,
        "score": impression.score,
        "note": impression.note,
    })


# =============================================================================
# Network Resilience — Peer Update & Lookup HTTP Handlers
# =============================================================================

async def handle_peer_update(request: Request) -> JSONResponse:
    """Accept a URL change notification from a connected peer.

    Verifies the agent_id is a known connection and optionally validates
    the public key matches before updating the stored URL.
    """
    global _agent_state
    state = _agent_state
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    # Global rate limit (per-connection checked after we know the agent_id)
    rate_err = _check_rate_limit(state)
    if rate_err:
        return JSONResponse({"error": rate_err}, status_code=429)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    agent_id = body.get("agent_id", "")
    new_url = body.get("new_url", "")
    public_key_hex = body.get("public_key_hex")
    signature = body.get("signature")
    timestamp = body.get("timestamp", "")

    if not agent_id or not new_url:
        return JSONResponse({"error": "Missing agent_id or new_url"}, status_code=400)

    # Validate URL
    url_err = validate_url(new_url)
    if url_err:
        return JSONResponse({"error": url_err}, status_code=400)

    conn = state.connections.get(agent_id)
    if conn is None:
        return JSONResponse({"error": "Unknown agent"}, status_code=404)

    # Replay protection: reject stale timestamps
    if timestamp and not _is_timestamp_fresh(timestamp):
        return JSONResponse({"error": "Timestamp expired"}, status_code=403)

    # Verify public key matches if both sides have one
    if public_key_hex and conn.agent_public_key_hex:
        if public_key_hex != conn.agent_public_key_hex:
            return JSONResponse({"error": "Public key mismatch"}, status_code=403)

    # Signature is REQUIRED when we have a stored key for this peer
    verify_key = conn.agent_public_key_hex or public_key_hex
    if conn.agent_public_key_hex:
        if not signature or not timestamp:
            return JSONResponse({"error": "Signature required — known public key on file"}, status_code=403)
        if not _verify_peer_update_signature(verify_key, signature, agent_id, new_url, timestamp):
            return JSONResponse({"error": "Invalid signature"}, status_code=403)
    elif verify_key and signature and timestamp:
        if not _verify_peer_update_signature(verify_key, signature, agent_id, new_url, timestamp):
            return JSONResponse({"error": "Invalid signature"}, status_code=403)

    old_url = conn.agent_url
    conn.agent_url = new_url

    # Update bio if included in the peer_update payload
    new_bio = body.get("bio")
    if new_bio is not None and isinstance(new_bio, str):
        conn.agent_bio = new_bio[:MAX_BIO_LENGTH]

    save_state()

    print(f"[DarkMatter] Peer {agent_id[:12]}... updated URL: {old_url} -> {new_url}", file=sys.stderr)
    return JSONResponse({"success": True, "updated": True})


async def handle_peer_lookup(request: Request) -> JSONResponse:
    """Look up the URL of a connected agent by ID.

    Used by other peers to find an agent's current URL when direct
    communication fails.
    """
    global _agent_state
    state = _agent_state
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    agent_id = request.path_params.get("agent_id", "")
    if not agent_id:
        return JSONResponse({"error": "Missing agent_id"}, status_code=400)

    conn = state.connections.get(agent_id)
    if conn is None:
        return JSONResponse({"error": "Not connected to that agent"}, status_code=404)

    return JSONResponse({
        "agent_id": conn.agent_id,
        "url": conn.agent_url,
        "status": "connected",
    })


# =============================================================================
# Gas Economy HTTP Endpoints
# =============================================================================


async def handle_gas_match(request: Request) -> JSONResponse:
    """Stateless match game endpoint. Peer picks a random number and returns it."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    result, status = process_gas_match(data)
    return JSONResponse(result, status_code=status)


async def handle_gas_signal(request: Request) -> JSONResponse:
    """Receive a forwarded gas signal and run the match game."""
    global _agent_state
    state = _agent_state
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    result, status = await process_gas_signal(state, data)
    return JSONResponse(result, status_code=status)


async def handle_gas_result(request: Request) -> JSONResponse:
    """B receives this when a downstream node resolves the gas signal."""
    global _agent_state
    state = _agent_state
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    result, status = await process_gas_result(state, data)
    return JSONResponse(result, status_code=status)


# =============================================================================
# WebRTC Signaling + Cleanup
# =============================================================================

def _cleanup_webrtc(conn: Connection) -> None:
    """Close WebRTC peer connection and revert transport to HTTP."""
    pc = conn.webrtc_pc
    conn.webrtc_channel = None
    conn.webrtc_pc = None
    conn.transport = "http"
    if pc is not None:
        asyncio.ensure_future(_close_pc(pc))


async def _close_pc(pc: object) -> None:
    """Close an RTCPeerConnection safely."""
    try:
        await pc.close()
    except Exception:
        pass


def _make_rtc_config():
    """Create an RTCConfiguration with STUN servers."""
    return RTCConfiguration(
        iceServers=[RTCIceServer(urls=s["urls"]) for s in WEBRTC_STUN_SERVERS]
    )


async def _wait_for_ice_gathering(pc, timeout: float = WEBRTC_ICE_GATHER_TIMEOUT) -> None:
    """Wait for ICE gathering to complete."""
    if pc.iceGatheringState == "complete":
        return
    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def on_ice_state():
        if pc.iceGatheringState == "complete":
            done.set()

    await asyncio.wait_for(done.wait(), timeout=timeout)


async def handle_webrtc_offer(request: Request) -> JSONResponse:
    """Handle an incoming WebRTC SDP offer from a peer (answering side).

    POST /__darkmatter__/webrtc_offer
    Body: {from_agent_id, sdp_offer}
    Returns: {sdp_answer}
    """
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    if not WEBRTC_AVAILABLE:
        return JSONResponse({"error": "WebRTC not available (aiortc not installed)"}, status_code=501)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    from_agent_id = data.get("from_agent_id", "")
    sdp_offer = data.get("sdp_offer", "")

    if not from_agent_id or not sdp_offer:
        return JSONResponse({"error": "Missing from_agent_id or sdp_offer"}, status_code=400)

    if from_agent_id not in state.connections:
        return JSONResponse({"error": "Not connected — WebRTC upgrade requires an existing connection."}, status_code=403)

    conn = state.connections[from_agent_id]

    # Clean up any existing WebRTC state for this connection
    if conn.webrtc_pc is not None:
        _cleanup_webrtc(conn)

    pc = RTCPeerConnection(configuration=_make_rtc_config())
    channel_ready = asyncio.Event()
    received_channel = [None]  # mutable container for closure

    @pc.on("datachannel")
    def on_datachannel(channel):
        received_channel[0] = channel
        conn.webrtc_channel = channel
        conn.webrtc_pc = pc
        conn.transport = "webrtc"

        @channel.on("message")
        async def on_message(message):
            try:
                envelope = json.loads(message)
                path = envelope.get("path", "")
                payload = envelope.get("payload", {})
                if path == "/__darkmatter__/message":
                    result, status_code = await _process_incoming_message(state, payload)
                    if status_code >= 400:
                        print(f"[DarkMatter] WebRTC message rejected ({status_code}): {result.get('error', 'unknown')}", file=sys.stderr)
            except Exception as e:
                print(f"[DarkMatter] WebRTC message processing error: {e}", file=sys.stderr)

        @channel.on("close")
        def on_close():
            print(f"[DarkMatter] WebRTC data channel closed (peer: {from_agent_id})", file=sys.stderr)
            _cleanup_webrtc(conn)

        channel_ready.set()

    @pc.on("connectionstatechange")
    async def on_connection_state_change():
        if pc.connectionState in ("failed", "closed"):
            print(f"[DarkMatter] WebRTC connection {pc.connectionState} (peer: {from_agent_id})", file=sys.stderr)
            _cleanup_webrtc(conn)

    # Set remote offer and create answer
    offer = RTCSessionDescription(sdp=sdp_offer, type="offer")
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    # Wait for ICE gathering
    await _wait_for_ice_gathering(pc)

    print(f"[DarkMatter] WebRTC: answered offer from {conn.agent_display_name or from_agent_id}", file=sys.stderr)

    return JSONResponse({
        "success": True,
        "sdp_answer": pc.localDescription.sdp,
    })


async def handle_well_known(request: Request) -> JSONResponse:
    """Return /.well-known/darkmatter.json for global discovery (RFC 8615)."""
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    public_url = os.environ.get("DARKMATTER_PUBLIC_URL", "").rstrip("/")
    if not public_url:
        host = request.headers.get("host", f"localhost:{state.port}")
        scheme = request.headers.get("x-forwarded-proto", "http")
        public_url = f"{scheme}://{host}"

    return JSONResponse({
        "darkmatter": True,
        "protocol_version": PROTOCOL_VERSION,
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "bio": state.bio,
        "status": state.status.value,
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "mesh_url": f"{public_url}/__darkmatter__",
        "mcp_url": f"{public_url}/mcp",
        "webrtc_enabled": WEBRTC_AVAILABLE,
    })


# =============================================================================
# LAN Discovery — UDP Broadcast
# =============================================================================


def _register_peer(state: AgentState, peer_id: str, url: str, bio: str,
                   status: str, accepting: bool, source: str) -> None:
    """Register a discovered peer in state."""
    state.discovered_peers[peer_id] = {
        "url": url,
        "bio": bio,
        "status": status,
        "accepting": accepting,
        "source": source,
        "ts": time.time(),
    }


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Receives UDP multicast discovery beacons from LAN agents."""

    def __init__(self, state: AgentState):
        self.state = state

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            packet = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if packet.get("proto") != "darkmatter":
            return

        peer_id = packet.get("agent_id", "")
        if not peer_id or peer_id == self.state.agent_id:
            return  # Ignore our own beacons

        peer_port = packet.get("port", DEFAULT_PORT)
        source_ip = addr[0]

        _register_peer(
            self.state, peer_id,
            url=f"http://{source_ip}:{peer_port}",
            bio=packet.get("bio", ""),
            status=packet.get("status", "active"),
            accepting=packet.get("accepting", True),
            source="lan",
        )


async def _probe_port(client: httpx.AsyncClient, state: AgentState, port: int) -> None:
    """Probe a single localhost port for a DarkMatter node."""
    try:
        resp = await client.get(f"http://127.0.0.1:{port}/.well-known/darkmatter.json")
        if resp.status_code != 200:
            return
        info = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError, KeyError):
        return

    peer_id = info.get("agent_id", "")
    if not peer_id or peer_id == state.agent_id:
        return

    _register_peer(
        state, peer_id,
        url=f"http://127.0.0.1:{port}",
        bio=info.get("bio", ""),
        status=info.get("status", "active"),
        accepting=info.get("accepting_connections", True),
        source="local",
    )


async def _scan_local_ports(state: AgentState) -> None:
    """Scan localhost ports for other DarkMatter nodes concurrently."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(0.5, connect=0.25)) as client:
        tasks = [
            _probe_port(client, state, port)
            for port in DISCOVERY_LOCAL_PORTS
            if port != state.port
        ]
        await asyncio.gather(*tasks, return_exceptions=True)


def _find_entrypoint_path() -> Optional[str]:
    """Locate the entrypoint.py script for the human node."""
    if ENTRYPOINT_PATH:
        return ENTRYPOINT_PATH if os.path.isfile(ENTRYPOINT_PATH) else None
    # Fallback: check project directory (one level up from .darkmatter/server.py)
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fallback = os.path.join(project_dir, "entrypoint.py")
    return fallback if os.path.isfile(fallback) else None


async def _ensure_entrypoint_running() -> None:
    """Auto-start the entrypoint (human node) on port 8200 if not already running."""
    if not ENTRYPOINT_AUTOSTART:
        return

    # Probe to see if entrypoint is already up
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(0.5, connect=0.5)) as client:
            resp = await client.get(f"http://127.0.0.1:{ENTRYPOINT_PORT}/.well-known/darkmatter.json")
            if resp.status_code == 200:
                print(f"[DarkMatter] Entrypoint already running on port {ENTRYPOINT_PORT}", file=sys.stderr)
                return
    except Exception:
        pass

    # Try to acquire lockfile (non-blocking) to prevent races
    import subprocess
    lockfile_path = os.path.join(os.path.expanduser("~"), ".darkmatter", "entrypoint.lock")
    try:
        lock_fd = os.open(lockfile_path, os.O_CREAT | os.O_WRONLY)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        # Another agent is already spawning
        print(f"[DarkMatter] Entrypoint spawn locked by another agent, skipping", file=sys.stderr)
        return

    try:
        path = _find_entrypoint_path()
        if not path:
            print(f"[DarkMatter] Entrypoint script not found, cannot auto-start", file=sys.stderr)
            return

        entrypoint_dir = os.path.dirname(os.path.abspath(path))
        log_path = os.path.join(os.path.expanduser("~"), ".darkmatter", "entrypoint.log")
        log_file = open(log_path, "a")

        print(f"[DarkMatter] Spawning entrypoint: {path}", file=sys.stderr)
        # Clean Werkzeug env vars so the child doesn't inherit stale FDs
        spawn_env = {k: v for k, v in os.environ.items()
                     if not k.startswith("WERKZEUG_")}
        subprocess.Popen(
            [sys.executable, path],
            start_new_session=True,
            cwd=entrypoint_dir,
            stdout=log_file,
            stderr=log_file,
            env=spawn_env,
        )

        # Poll until the entrypoint is healthy (up to 10s)
        for _ in range(20):
            await asyncio.sleep(0.5)
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(0.5, connect=0.5)) as client:
                    resp = await client.get(f"http://127.0.0.1:{ENTRYPOINT_PORT}/.well-known/darkmatter.json")
                    if resp.status_code == 200:
                        print(f"[DarkMatter] Entrypoint started on port {ENTRYPOINT_PORT}", file=sys.stderr)
                        return
            except Exception:
                pass

        print(f"[DarkMatter] Entrypoint failed to start within 10s (check {log_path})", file=sys.stderr)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


async def _discovery_loop(state: AgentState) -> None:
    """Periodically discover peers via local HTTP scan and LAN multicast."""
    loop = asyncio.get_event_loop()

    # Multicast beacon socket for LAN discovery
    mcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    mcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    mcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    mcast_sock.setblocking(False)

    try:
        while True:
            # Scan localhost ports for other nodes
            try:
                await _scan_local_ports(state)
            except Exception:
                pass

            # Ensure entrypoint (human node) is running
            try:
                await _ensure_entrypoint_running()
            except Exception:
                pass

            # Send multicast beacon for LAN peers
            packet = json.dumps({
                "proto": "darkmatter",
                "v": PROTOCOL_VERSION,
                "agent_id": state.agent_id,
                "display_name": state.display_name,
                "public_key_hex": state.public_key_hex,
                "bio": state.bio[:100],
                "port": state.port,
                "status": state.status.value,
                "accepting": len(state.connections) < MAX_CONNECTIONS,
                "ts": int(time.time()),
            }).encode("utf-8")

            try:
                await loop.run_in_executor(
                    None, mcast_sock.sendto, packet, (DISCOVERY_MCAST_GROUP, DISCOVERY_PORT)
                )
            except OSError:
                pass

            await asyncio.sleep(DISCOVERY_INTERVAL)
    finally:
        mcast_sock.close()


# =============================================================================
# Bootstrap Routes — Zero-friction node deployment
# =============================================================================

async def handle_bootstrap(request: Request) -> Response:
    """Return a shell script that bootstraps a new DarkMatter node."""
    state = _agent_state
    host = request.headers.get("host", f"localhost:{state.port if state else 8100}")
    scheme = request.headers.get("x-forwarded-proto", "http")
    source_url = f"{scheme}://{host}/bootstrap/server.py"

    script = f"""#!/bin/bash
set -e

echo "=== DarkMatter Bootstrap ==="
echo ""

DM_DIR="$HOME/.darkmatter"
VENV_DIR="$DM_DIR/venv"

# Find python3
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
        PYTHON_CMD="$cmd"
        break
    fi
done
if [ -z "$PYTHON_CMD" ]; then
    echo "ERROR: Python not found. Install Python 3.10+ first."
    exit 1
fi
echo "Using $PYTHON_CMD ($($PYTHON_CMD --version 2>&1))"

# Create directory
mkdir -p "$DM_DIR"

# Download server
echo "Downloading server.py..."
curl -sS "{source_url}" -o "$DM_DIR/server.py"

# Create venv and install dependencies
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install --quiet "mcp[cli]" httpx uvicorn starlette cryptography anyio

# Find free port in 8100-8110
PORT=8100
while [ $PORT -le 8110 ]; do
    if ! lsof -i :$PORT >/dev/null 2>&1; then
        break
    fi
    PORT=$((PORT + 1))
done
if [ $PORT -gt 8110 ]; then
    echo "ERROR: No free ports in 8100-8110 range"
    exit 1
fi
echo "Using port $PORT"

VENV_PYTHON="$VENV_DIR/bin/python"

echo ""
echo "=== DarkMatter installed ==="
echo ""
echo "NEXT STEPS (for the installing agent):"
echo ""
echo "1. Write .mcp.json in your project directory with this content"
echo "   (replace YOUR_NAME with a short name for this agent):"
echo ""
echo '{{"mcpServers":{{"darkmatter":{{"command":"'"$VENV_PYTHON"'","args":["'"$DM_DIR"'/server.py"],"env":{{"DARKMATTER_PORT":"'$PORT'","DARKMATTER_DISPLAY_NAME":"YOUR_NAME"}}}}}}}}'
echo ""
echo "2. Ask the user to restart Claude Code (or their MCP client)"
echo "   so it picks up the new .mcp.json config."
echo ""
echo "3. After restart, call darkmatter_update_bio to describe your"
echo "   capabilities so other agents can find and route to you."
echo ""
echo "4. Call darkmatter_discover_local to find nearby agents,"
echo "   then darkmatter_connection(action='request') to connect."
echo ""
echo "Identity is automatic — a passport key is created on first run."
"""
    return Response(script, media_type="text/plain")


async def handle_bootstrap_source(request: Request) -> Response:
    """Serve raw server.py source code. No auth required."""
    server_path = os.path.abspath(__file__)
    with open(server_path, "r") as f:
        source = f.read()
    return Response(source, media_type="text/plain")


# =============================================================================
# Application — Mount MCP + DarkMatter HTTP endpoints together
# =============================================================================

def _init_state(port: int = None) -> None:
    """Initialize agent state from passport + persisted state. Safe to call multiple times.

    Identity flow:
    1. Load (or create) passport from .darkmatter/passport.key in cwd
    2. Derive agent_id = public_key_hex (deterministic from passport)
    3. Try loading state from ~/.darkmatter/state/<public_key_hex>.json
    4. If not found, scan legacy state files for matching public key and migrate
    5. If nothing found, create fresh state
    """
    global _agent_state
    if _agent_state is not None:
        return  # Already initialized

    if port is None:
        port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))

    display_name = os.environ.get("DARKMATTER_DISPLAY_NAME", os.environ.get("DARKMATTER_AGENT_ID", ""))
    bio = os.environ.get("DARKMATTER_BIO", "A DarkMatter mesh agent.")

    # Step 1: Load or create passport — this IS our identity
    priv, pub = _load_or_create_passport()
    agent_id = pub  # Agent ID = public key hex

    # Step 2: Create a temporary AgentState so _state_file_path() works
    _agent_state = AgentState(
        agent_id=agent_id,
        bio=bio,
        status=AgentStatus.ACTIVE,
        port=port,
        private_key_hex=priv,
        public_key_hex=pub,
        display_name=display_name or None,
    )

    # Step 3: Try loading state from passport-keyed path
    state_path = _state_file_path()
    restored = _load_state_from_file(state_path)

    if restored:
        # Restore state but enforce passport-derived identity
        restored.agent_id = agent_id  # Always use passport-derived ID
        restored.private_key_hex = priv
        restored.public_key_hex = pub
        restored.port = port
        restored.status = AgentStatus.ACTIVE
        if display_name:
            restored.display_name = display_name
        _agent_state = restored
        print(f"[DarkMatter] Restored state (display: {_agent_state.display_name or 'none'}, "
              f"{len(_agent_state.connections)} connections)", file=sys.stderr)
    else:
        # _agent_state already set to fresh state above
        print(f"[DarkMatter] Starting fresh (display: {display_name or 'none'}) "
              f"on port {port}", file=sys.stderr)

    print(f"[DarkMatter] Identity: {agent_id[:16]}...{agent_id[-8:]}", file=sys.stderr)

    # Derive Solana wallet (ephemeral — not persisted, derived from passport each startup)
    if SOLANA_AVAILABLE and _agent_state.private_key_hex:
        _agent_state.wallets["solana"] = _get_solana_wallet_address(_agent_state.private_key_hex)
        print(f"[DarkMatter] Solana wallet: {_agent_state.wallets['solana']}", file=sys.stderr)

    save_state()


def create_app() -> Starlette:
    """Create the combined Starlette app with MCP and DarkMatter endpoints.

    Returns:
        The ASGI app.
    """
    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))
    _init_state(port)

    # LAN discovery setup
    discovery_enabled = os.environ.get("DARKMATTER_DISCOVERY", "true").lower() == "true"

    async def on_startup() -> None:
        if discovery_enabled:
            import struct as _struct
            import socket as _socket
            loop = asyncio.get_event_loop()

            # Multicast listener for LAN discovery (best-effort)
            try:
                sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM, _socket.IPPROTO_UDP)
                sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                if hasattr(_socket, "SO_REUSEPORT"):
                    sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEPORT, 1)
                sock.bind(("", DISCOVERY_PORT))
                mreq = _struct.pack("4s4s",
                    _socket.inet_aton(DISCOVERY_MCAST_GROUP),
                    _socket.inet_aton("0.0.0.0"))
                sock.setsockopt(_socket.IPPROTO_IP, _socket.IP_ADD_MEMBERSHIP, mreq)
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: _DiscoveryProtocol(_agent_state),
                    sock=sock,
                )
            except OSError as e:
                print(f"[DarkMatter] LAN multicast listener failed ({e}), local HTTP discovery still active", file=sys.stderr)

            # Start discovery loop (local HTTP scan + LAN multicast beacons)
            asyncio.create_task(_discovery_loop(_agent_state))
            print(f"[DarkMatter] Discovery: ENABLED (local: HTTP scan ports {DISCOVERY_LOCAL_PORTS.start}-{DISCOVERY_LOCAL_PORTS.stop - 1}, LAN: multicast {DISCOVERY_MCAST_GROUP}:{DISCOVERY_PORT})", file=sys.stderr)

        # Start live status updater (updates tool description and notifies clients)
        asyncio.create_task(_status_updater())
        print(f"[DarkMatter] Live status updater: ENABLED (5s interval)", file=sys.stderr)

        # Initialize dynamic tool visibility (hide optional tools until needed)
        _initialize_tool_visibility()

        # Discover public URL and detect NAT
        _agent_state.public_url = await _discover_public_url(port)
        _agent_state.nat_detected = await _check_nat_status(_agent_state.public_url)
        if _agent_state.nat_detected:
            print(f"[DarkMatter] NAT detected: True — using anchor webhook relay", file=sys.stderr)
        asyncio.create_task(_network_health_loop(_agent_state))
        print(f"[DarkMatter] Network health loop: ENABLED ({HEALTH_CHECK_INTERVAL}s interval)", file=sys.stderr)
        print(f"[DarkMatter] UPnP: {'AVAILABLE' if UPNP_AVAILABLE else 'disabled (pip install miniupnpc)'}", file=sys.stderr)

        # Register with anchor nodes on boot
        if ANCHOR_NODES and _agent_state.public_url:
            await _broadcast_peer_update(_agent_state)
            print(f"[DarkMatter] Anchor nodes: registered with {len(ANCHOR_NODES)} anchor(s)", file=sys.stderr)
        elif ANCHOR_NODES:
            print(f"[DarkMatter] Anchor nodes: configured but no public URL yet", file=sys.stderr)

        # Auto-start entrypoint (human node) if not already running
        asyncio.create_task(_ensure_entrypoint_running())

        # Re-spawn agents for any queued messages left from a previous session
        if AGENT_SPAWN_ENABLED and _agent_state.router_mode == "spawn" and _agent_state.message_queue:
            queued_count = len(_agent_state.message_queue)
            print(f"[DarkMatter] {queued_count} message(s) in queue from previous session, spawning agents...", file=sys.stderr)
            for msg in list(_agent_state.message_queue):
                asyncio.create_task(_spawn_agent_for_message(_agent_state, msg))

    # DarkMatter mesh protocol routes
    darkmatter_routes = [
        Route("/connection_request", handle_connection_request, methods=["POST"]),
        Route("/connection_accepted", handle_connection_accepted, methods=["POST"]),
        Route("/accept_pending", handle_accept_pending, methods=["POST"]),
        Route("/message", handle_message, methods=["POST"]),
        Route("/webhook/{message_id}", handle_webhook_post, methods=["POST"]),
        Route("/webhook/{message_id}", handle_webhook_get, methods=["GET"]),
        Route("/status", handle_status, methods=["GET"]),
        Route("/network_info", handle_network_info, methods=["GET"]),
        Route("/impression/{agent_id}", handle_impression_get, methods=["GET"]),
        Route("/webrtc_offer", handle_webrtc_offer, methods=["POST"]),
        Route("/peer_update", handle_peer_update, methods=["POST"]),
        Route("/peer_lookup/{agent_id}", handle_peer_lookup, methods=["GET"]),
        Route("/gas_match", handle_gas_match, methods=["POST"]),
        Route("/gas_signal", handle_gas_signal, methods=["POST"]),
        Route("/gas_result", handle_gas_result, methods=["POST"]),
    ]

    # Extract the MCP ASGI handler and its session manager for lifecycle.
    # Identity is passport-based — agent_id = public key hex from .darkmatter/passport.key
    import contextlib
    mcp_starlette = mcp.streamable_http_app()
    mcp_handler = mcp_starlette.routes[0].app  # StreamableHTTPASGIApp
    session_manager = mcp_handler.session_manager

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # Start MCP session manager + run our startup hooks
        async with session_manager.run():
            await on_startup()
            yield
            _cleanup_upnp()

    # Build the app. Use redirect_slashes=False so POST /mcp doesn't get
    # redirected to /mcp/ (which breaks MCP client connections).
    from starlette.routing import Router
    app = Router(
        routes=[
            Route("/.well-known/darkmatter.json", handle_well_known, methods=["GET"]),
            Route("/bootstrap", handle_bootstrap, methods=["GET"]),
            Route("/bootstrap/server.py", handle_bootstrap_source, methods=["GET"]),
            Mount("/__darkmatter__", routes=darkmatter_routes),
            Route("/mcp", mcp_handler),
        ],
        redirect_slashes=False,
        lifespan=lifespan,
    )

    return app


# =============================================================================
# Main — Run the server
# =============================================================================

def _print_startup_banner(port: int, transport: str, discovery_enabled: bool) -> None:
    """Print startup banner to stderr."""
    print(f"[DarkMatter] Starting mesh protocol on http://localhost:{port}", file=sys.stderr)
    print(f"[DarkMatter] MCP transport: {transport}", file=sys.stderr)
    print(f"[DarkMatter] Discovery: {'ENABLED' if discovery_enabled else 'disabled'}", file=sys.stderr)
    print(f"[DarkMatter] WebRTC: {'AVAILABLE' if WEBRTC_AVAILABLE else 'disabled (pip install aiortc)'}", file=sys.stderr)
    print(f"[DarkMatter] UPnP: {'AVAILABLE' if UPNP_AVAILABLE else 'disabled (pip install miniupnpc)'}", file=sys.stderr)
    spawn_info = f"ENABLED (max {AGENT_SPAWN_MAX_CONCURRENT} concurrent, {AGENT_SPAWN_MAX_PER_HOUR}/hr)" if AGENT_SPAWN_ENABLED else "disabled"
    if AGENT_SPAWN_ENABLED and AGENT_SPAWN_TERMINAL:
        spawn_info += " [terminal mode]"
    print(f"[DarkMatter] Agent auto-spawn: {spawn_info}", file=sys.stderr)
    print(f"[DarkMatter] Bootstrap: curl http://localhost:{port}/bootstrap | bash", file=sys.stderr)


def _check_port_owner(host: str, port: int) -> Optional[str]:
    """Check if a port has a DarkMatter server and return its agent_id, or None if port is free."""
    import socket as _socket
    # First check if port is in use at all
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return None  # Port is free
        except OSError:
            pass  # Port in use — probe it

    # Port is taken — check if it's a DarkMatter node
    try:
        import httpx
        resp = httpx.get(f"http://127.0.0.1:{port}/.well-known/darkmatter.json", timeout=1.0)
        if resp.status_code == 200:
            info = resp.json()
            return info.get("agent_id")
    except Exception:
        pass
    return "unknown"  # Port taken by non-DarkMatter process


def _find_free_port(host: str, start: int) -> int:
    """Find a free port in the discovery range (start to start+10)."""
    import socket as _socket
    for port in range(start, start + 11):
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free ports in range {start}-{start + 10}")


async def _run_stdio_with_http() -> None:
    """Run MCP over stdio while serving HTTP mesh endpoints in the background.

    This is the preferred mode when launched by an MCP client (e.g. Claude Code).
    The client talks MCP over stdin/stdout. The HTTP server runs alongside for
    agent-to-agent mesh communication, discovery, and webhooks.

    Port conflict resolution:
    - Port free → start normally
    - Port taken by OUR server (same agent_id) → another session of us is
      already running the HTTP mesh. Run stdio-only and share state.
    - Port taken by SOMEONE ELSE → find a new free port and start there.
    """
    from mcp.server.stdio import stdio_server

    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))
    host = os.environ.get("DARKMATTER_HOST", "127.0.0.1")

    # Load our passport to get our agent_id (if we have one)
    _priv, _pub = _load_or_create_passport()
    our_agent_id = _pub

    # Check who owns the port
    port_owner = _check_port_owner(host, port)

    if port_owner is None:
        # Port is free — start normally
        app = create_app()
        discovery_enabled = os.environ.get("DARKMATTER_DISCOVERY", "true").lower() == "true"
        _print_startup_banner(port, "stdio (with HTTP mesh on port " + str(port) + ")", discovery_enabled)

        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)

        async with stdio_server() as (read_stream, write_stream):
            async with anyio.create_task_group() as tg:
                tg.start_soon(server.serve)
                await mcp._mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp._mcp_server.create_initialization_options(),
                )
                server.should_exit = True

    elif port_owner == our_agent_id:
        # Our server is already running — parallel session, share state
        print(f"[DarkMatter] Port {port} is already running our server (agent {our_agent_id[:12]}...).", file=sys.stderr)
        print(f"[DarkMatter] Running stdio-only MCP (parallel session, shared state).", file=sys.stderr)

        _init_state(port)

        async with stdio_server() as (read_stream, write_stream):
            await mcp._mcp_server.run(
                read_stream,
                write_stream,
                mcp._mcp_server.create_initialization_options(),
            )

    else:
        # Port taken by a different agent — find a new port
        print(f"[DarkMatter] Port {port} is taken by another agent ({port_owner[:12] if port_owner != 'unknown' else 'unknown'}...).", file=sys.stderr)
        new_port = _find_free_port(host, DEFAULT_PORT)
        print(f"[DarkMatter] Using port {new_port} instead.", file=sys.stderr)

        # Override port for this session
        os.environ["DARKMATTER_PORT"] = str(new_port)

        app = create_app()
        discovery_enabled = os.environ.get("DARKMATTER_DISCOVERY", "true").lower() == "true"
        _print_startup_banner(new_port, "stdio (with HTTP mesh on port " + str(new_port) + ")", discovery_enabled)

        config = uvicorn.Config(app, host=host, port=new_port, log_level="warning")
        server = uvicorn.Server(config)

        async with stdio_server() as (read_stream, write_stream):
            async with anyio.create_task_group() as tg:
                tg.start_soon(server.serve)
                await mcp._mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp._mcp_server.create_initialization_options(),
                )
                server.should_exit = True


if __name__ == "__main__":
    import anyio

    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))
    transport = os.environ.get("DARKMATTER_TRANSPORT", "auto")

    # Auto-detect: if stdin is not a TTY, we're being launched by an MCP client
    use_stdio = transport == "stdio" or (transport == "auto" and not sys.stdin.isatty())

    if use_stdio:
        anyio.run(_run_stdio_with_http)
    else:
        # Standalone HTTP mode (manual start, or DARKMATTER_TRANSPORT=http)
        app = create_app()
        discovery_enabled = os.environ.get("DARKMATTER_DISCOVERY", "true").lower() == "true"
        _print_startup_banner(port, "streamable-http", discovery_enabled)

        host = os.environ.get("DARKMATTER_HOST", "127.0.0.1")
        uvicorn.run(app, host=host, port=port)

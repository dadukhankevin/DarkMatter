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
import os
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


# =============================================================================
# Configuration
# =============================================================================

DEFAULT_PORT = 8100
MAX_CONNECTIONS = 50
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

# Anchor nodes — stable directory services for peer lookup fallback
_ANCHOR_DEFAULT = "https://loseylabs.ai"
_anchor_env = os.environ.get("DARKMATTER_ANCHOR_NODES", _ANCHOR_DEFAULT).strip()
ANCHOR_NODES: list[str] = [u.strip().rstrip("/") for u in _anchor_env.split(",") if u.strip()] if _anchor_env else []

# Agent auto-spawn configuration
AGENT_SPAWN_ENABLED = os.environ.get("DARKMATTER_AGENT_ENABLED", "true").lower() == "true"
AGENT_SPAWN_MAX_CONCURRENT = int(os.environ.get("DARKMATTER_AGENT_MAX_CONCURRENT", "2"))
AGENT_SPAWN_MAX_PER_HOUR = int(os.environ.get("DARKMATTER_AGENT_MAX_PER_HOUR", "6"))
AGENT_SPAWN_COMMAND = os.environ.get("DARKMATTER_AGENT_COMMAND", "claude")
AGENT_SPAWN_TIMEOUT = int(os.environ.get("DARKMATTER_AGENT_TIMEOUT", "300"))


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
        if not path.startswith("/__darkmatter__/webhook/"):
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


async def _broadcast_peer_update(state) -> None:
    """Notify all connected peers and anchor nodes of our current URL."""
    if not state.public_url:
        return

    timestamp = datetime.now(timezone.utc).isoformat()
    payload = {
        "agent_id": state.agent_id,
        "new_url": state.public_url,
        "timestamp": timestamp,
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

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[DarkMatter] Health loop error: {e}", file=sys.stderr)


# =============================================================================
# Cryptographic Identity — Ed25519 keypair, signing, verification
# =============================================================================

def _generate_keypair() -> tuple[str, str]:
    """Generate an Ed25519 keypair. Returns (private_key_hex, public_key_hex)."""
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private_bytes.hex(), public_bytes.hex()


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


# =============================================================================
# Data Models (in-memory state)
# =============================================================================


class AgentStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class ConnectionDirection(str, Enum):
    OUTBOUND = "outbound"  # I connected to them
    INBOUND = "inbound"    # They connected to me


@dataclass
class Connection:
    """A directional connection to another agent in the mesh."""
    agent_id: str
    agent_url: str
    agent_bio: str
    direction: ConnectionDirection
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
    response: Optional[dict] = None  # {agent_id, response, metadata, timestamp}


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
    impressions: dict[str, str] = field(default_factory=dict)  # agent_id -> freeform impression text
    claimed: bool = False  # True after first agent authenticates
    # Cryptographic identity — Ed25519 keypair and human-friendly display name
    private_key_hex: Optional[str] = None
    public_key_hex: Optional[str] = None
    display_name: Optional[str] = None
    # Track agent URLs we've sent outbound connection requests to (not persisted)
    pending_outbound: set[str] = field(default_factory=set)
    # LAN-discovered peers (ephemeral, not persisted)
    discovered_peers: dict[str, dict] = field(default_factory=dict)
    # MCP sessions that have authenticated (ephemeral, not persisted)
    authenticated_sessions: set[str] = field(default_factory=set)
    # Rate limiting (global)
    rate_limit_global: int = 0             # 0 = use DEFAULT_RATE_LIMIT_GLOBAL
    _global_request_timestamps: deque = field(default_factory=deque)
    # Network resilience (ephemeral — never persisted)
    public_url: Optional[str] = None
    _upnp_mapping: Optional[tuple] = None  # (url, upnp_obj, ext_port)


# =============================================================================
# Server Initialization
# =============================================================================

mcp = FastMCP(
    "darkmatter_mcp",
    instructions="""\
You are a DarkMatter mesh agent — a node in a self-organizing network of AI agents.

ON STARTUP (do these immediately):
1. Call darkmatter_authenticate to authenticate. If you have a private_key_hex from a previous \
session, pass it. If this is your first time, call it without args to claim this node. SAVE THE \
RETURNED private_key_hex to your persistent memory — it never changes, so you only save it once.
2. Call darkmatter_get_identity to learn who you are (your agent ID, bio, status, connections).
3. If your bio is generic or empty, call darkmatter_update_bio with a description of YOUR \
capabilities and specialties. Be specific — other agents use your bio to decide whether to \
route messages to you.
4. Call darkmatter_list_connections to see who you're connected to.
5. Call darkmatter_list_inbox to check for any queued messages waiting for your response.

ONGOING BEHAVIOR:
- When you receive messages (check darkmatter_list_inbox), read them and respond using \
darkmatter_respond_message. You are the intelligence behind this agent — decide how to answer.
- If you can't answer a message, consider forwarding it with darkmatter_forward_message to a \
connected agent who might be able to help. You can fork a message to multiple agents — forwarding \
keeps the message in your inbox so you can forward again. Use darkmatter_respond_message to remove \
it when you're done. Check the `can_forward` field in list_inbox to see if forwarding is possible.
- Track messages you've sent with darkmatter_list_messages and darkmatter_get_sent_message.
- Use darkmatter_expire_message to cancel a sent message that's no longer needed.
- You can connect to other agents with darkmatter_request_connection if you know their URL.
- Use darkmatter_network_info to discover peers in the network.
- Use darkmatter_discover_domain to check if a domain hosts a DarkMatter node (checks /.well-known/darkmatter.json).
- Use darkmatter_discover_local to see agents discovered on the local network via LAN broadcast.
- Use darkmatter_list_pending_requests to see if anyone wants to connect to you, then \
darkmatter_respond_connection to accept or reject.

IDENTITY:
- Your agent ID and bio define who you are in the mesh. Own it.
- Other agents see your bio when deciding whether to connect or route messages to you.
- Update your bio anytime your capabilities change with darkmatter_update_bio.

IMPRESSIONS (Trust):
- After interacting with an agent, store an impression with darkmatter_set_impression. \
These are your private notes — "fast and accurate", "unhelpful", "great at ML questions".
- When an unknown agent requests to connect, use darkmatter_ask_impression to ask your \
existing connections what they think. Their impressions help you decide whether to accept.
- Your impressions are shared when other agents ask — this is how trust propagates through \
the network. Be honest.
- Use darkmatter_get_impression to check your notes on a specific agent, and \
darkmatter_delete_impression to remove outdated ones.

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
import threading
_state_write_lock = threading.Lock()  # Serializes save_state() writes to prevent torn state files

# Agent auto-spawn tracking (ephemeral, not persisted)
@dataclass
class SpawnedAgent:
    process: asyncio.subprocess.Process
    message_id: str
    spawned_at: float
    pid: int

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
    stats = (
        f"Agent: {agent_label} | Status: {state.status.value} | "
        f"Connections: {conns}/{MAX_CONNECTIONS} ({peers}) | "
        f"Inbox: {msgs} | Handled: {handled} | Pending requests: {pending}"
        f"{agent_suffix}"
    )

    # Build action items, most urgent first
    actions = []
    if state.status == AgentStatus.INACTIVE:
        actions.append(
            "You are INACTIVE — other agents cannot see or message you. Use darkmatter_set_status to go active"
        )
    if pending > 0:
        actions.append(
            f"{pending} agent(s) want to connect — use darkmatter_list_pending_requests to review"
        )
    if msgs > 0:
        actions.append(
            f"{msgs} message(s) in your inbox — use darkmatter_list_messages to read and darkmatter_respond_message to reply"
        )
    sent_active = sum(1 for sm in state.sent_messages.values() if sm.status == "active")
    if sent_active > 0:
        actions.append(
            f"{sent_active} sent message(s) awaiting response — use darkmatter_list_messages to check"
        )
    if conns == 0:
        actions.append(
            "No connections yet — use darkmatter_discover_local to find nearby agents or darkmatter_request_connection to connect to a known peer"
        )
    if not state.bio or state.bio in (
        "A DarkMatter mesh agent.",
        "Description of what this agent specializes in",
    ):
        actions.append(
            "Your bio is generic — use darkmatter_update_bio to describe your actual capabilities so other agents can route to you"
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


def _cleanup_finished_agents() -> None:
    """Remove finished agent processes from the tracking list."""
    still_running = []
    for agent in _spawned_agents:
        if agent.process.returncode is not None:
            print(
                f"[DarkMatter] Spawned agent PID {agent.pid} exited "
                f"(code={agent.process.returncode}, msg={agent.message_id[:12]}...)",
                file=sys.stderr,
            )
        else:
            still_running.append(agent)
    _spawned_agents.clear()
    _spawned_agents.extend(still_running)


def _build_agent_prompt(state: "AgentState", msg: "QueuedMessage") -> str:
    """Build the prompt for a spawned claude agent."""
    sender_info = msg.from_agent_id or "unknown"
    conn = state.connections.get(msg.from_agent_id) if msg.from_agent_id else None
    sender_name = (conn.agent_display_name if conn else None) or sender_info
    verified_str = "YES (cryptographically verified)" if msg.verified else "NO"

    # Build list of connected agents for forwarding context
    peers = []
    for cid, c in state.connections.items():
        label = c.agent_display_name or cid[:12]
        bio = c.agent_bio or "no bio"
        peers.append(f"  - {label} ({cid[:12]}...): {bio}")
    peers_block = "\n".join(peers) if peers else "  (none)"

    return f"""\
You are an autonomous DarkMatter mesh agent handling an incoming message.

YOUR IDENTITY:
- Display name: {state.display_name or 'unnamed'}
- Agent ID: {state.agent_id}
- Bio: {state.bio}

INCOMING MESSAGE:
- Message ID: {msg.message_id}
- From: {sender_name} (agent_id: {msg.from_agent_id or 'unknown'})
- Verified: {verified_str}
- Content: {msg.content}
- Metadata: {json.dumps(msg.metadata) if msg.metadata else 'none'}
- Hops remaining: {msg.hops_remaining}

CONNECTED PEERS (available for forwarding):
{peers_block}

INSTRUCTIONS:
1. First, call darkmatter_authenticate with your private key to authenticate.
2. Read the message above carefully.
3. If you can answer the message, use darkmatter_respond_message with message_id="{msg.message_id}" and your response.
4. If you cannot answer but a connected peer might be able to help, use darkmatter_forward_message to forward it, then respond with a note that you forwarded it.
5. If you neither can answer nor know who to forward to, respond politely explaining that.
6. After responding, your work is done. Do NOT perform any other actions.

CRITICAL RULES:
- Do NOT spawn more agents or sub-processes.
- Do NOT modify any files.
- Do NOT run any shell commands.
- Keep your response concise and helpful.
- You have 5 minutes maximum before being terminated.
"""


async def _spawn_agent_for_message(state: "AgentState", msg: "QueuedMessage") -> None:
    """Spawn a claude subprocess to handle an incoming message."""
    ok, reason = _can_spawn_agent()
    if not ok:
        print(f"[DarkMatter] Not spawning agent: {reason}", file=sys.stderr)
        return

    # Deduplicate — don't spawn for a message we're already handling
    for agent in _spawned_agents:
        if agent.message_id == msg.message_id:
            print(f"[DarkMatter] Agent already spawned for message {msg.message_id[:12]}...", file=sys.stderr)
            return

    prompt = _build_agent_prompt(state, msg)

    # Build environment with recursion guard
    env = os.environ.copy()
    env["DARKMATTER_AGENT_ENABLED"] = "false"

    try:
        process = await asyncio.create_subprocess_exec(
            AGENT_SPAWN_COMMAND, "-p", "--dangerously-skip-permissions", prompt,
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
    if agent.process.returncode is None:
        print(
            f"[DarkMatter] Spawned agent PID {agent.pid} timed out after {AGENT_SPAWN_TIMEOUT}s, terminating...",
            file=sys.stderr,
        )
        try:
            agent.process.terminate()
            # Give it 5 seconds to clean up, then force kill
            try:
                await asyncio.wait_for(agent.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                print(f"[DarkMatter] Force-killing agent PID {agent.pid}", file=sys.stderr)
                agent.process.kill()
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


async def _update_status_tool() -> None:
    """Update the status tool's description if state changed, and notify clients."""
    global _last_status_desc
    new_desc = _build_status_line()
    if new_desc == _last_status_desc:
        return
    _last_status_desc = new_desc

    # Update the tool's description in FastMCP's internal store
    tool = mcp._tool_manager._tools.get("darkmatter_status")
    if tool:
        tool.description = (
            "DarkMatter live node status dashboard. "
            "Current state is shown below — no need to call unless you want full details.\n\n"
            f"LIVE STATUS: {new_desc}"
        )
        await _notify_tools_changed()
        print(f"[DarkMatter] Status tool updated: {new_desc}", file=sys.stderr)


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


async def _status_updater() -> None:
    """Background task: periodically update the status tool description and check WebRTC health."""
    while True:
        await asyncio.sleep(5)
        try:
            _check_webrtc_health()
            _cleanup_finished_agents()
            await _update_status_tool()
        except Exception as e:
            print(f"[DarkMatter] Status updater error: {e}", file=sys.stderr)


def _state_file_path() -> str:
    if "DARKMATTER_STATE_FILE" in os.environ:
        return os.environ["DARKMATTER_STATE_FILE"]
    state_dir = os.path.join(os.path.expanduser("~"), ".darkmatter", "state")
    os.makedirs(state_dir, exist_ok=True)
    display_name = os.environ.get("DARKMATTER_DISPLAY_NAME", "").strip()
    if display_name:
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in display_name)
        return os.path.join(state_dir, f"{safe_name}.json")
    port = os.environ.get("DARKMATTER_PORT", "8100")
    return os.path.join(state_dir, f"{port}.json")


def save_state() -> None:
    """Persist durable state (identity, connections, telemetry, sent_messages) to disk.

    Message queue and pending requests are NOT persisted — they are ephemeral.
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
        "claimed": state.claimed,
        "private_key_hex": state.private_key_hex,
        "public_key_hex": state.public_key_hex,
        "display_name": state.display_name,
        "connections": {
            aid: {
                "agent_id": c.agent_id,
                "agent_url": c.agent_url,
                "agent_bio": c.agent_bio,
                "direction": c.direction.value,
                "connected_at": c.connected_at,
                "messages_sent": c.messages_sent,
                "messages_received": c.messages_received,
                "messages_declined": c.messages_declined,
                "total_response_time_ms": c.total_response_time_ms,
                "last_activity": c.last_activity,
                "agent_public_key_hex": c.agent_public_key_hex,
                "agent_display_name": c.agent_display_name,
                "rate_limit": c.rate_limit,
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
                "response": sm.response,
            }
            for mid, sm in state.sent_messages.items()
        },
        "impressions": state.impressions,
        "rate_limit_global": state.rate_limit_global,
    }

    path = _state_file_path()
    tmp = path + ".tmp"
    with _state_write_lock:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def load_state() -> Optional[AgentState]:
    """Load persisted state from disk, if it exists. Returns None if no file."""
    path = _state_file_path()
    if not os.path.exists(path):
        # Migration: if using name-keyed path, check for legacy port-keyed file
        display_name = os.environ.get("DARKMATTER_DISPLAY_NAME", "").strip()
        if display_name:
            port = os.environ.get("DARKMATTER_PORT", "8100")
            state_dir = os.path.join(os.path.expanduser("~"), ".darkmatter", "state")
            legacy_path = os.path.join(state_dir, f"{port}.json")
            if os.path.exists(legacy_path):
                import shutil
                shutil.copy2(legacy_path, path)
                print(f"[DarkMatter] Migrated state: {legacy_path} → {path}", file=sys.stderr)
        if not os.path.exists(path):
            return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[DarkMatter] Warning: could not load state file: {e}", file=sys.stderr)
        return None

    connections = {}
    for aid, cd in data.get("connections", {}).items():
        connections[aid] = Connection(
            agent_id=cd["agent_id"],
            agent_url=cd["agent_url"],
            agent_bio=cd.get("agent_bio", ""),
            direction=ConnectionDirection(cd["direction"]),
            connected_at=cd.get("connected_at", ""),
            messages_sent=cd.get("messages_sent", 0),
            messages_received=cd.get("messages_received", 0),
            messages_declined=cd.get("messages_declined", 0),
            total_response_time_ms=cd.get("total_response_time_ms", 0.0),
            last_activity=cd.get("last_activity"),
            agent_public_key_hex=cd.get("agent_public_key_hex"),
            agent_display_name=cd.get("agent_display_name"),
            rate_limit=cd.get("rate_limit", 0),
        )

    sent_messages = {}
    for mid, sd in data.get("sent_messages", {}).items():
        sent_messages[mid] = SentMessage(
            message_id=sd["message_id"],
            content=sd["content"],
            status=sd["status"],
            initial_hops=sd["initial_hops"],
            routed_to=sd["routed_to"],
            created_at=sd.get("created_at", ""),
            updates=sd.get("updates", []),
            response=sd.get("response"),
        )

    # Migrate legacy state: generate keypair if missing
    priv = data.get("private_key_hex")
    pub = data.get("public_key_hex")
    if not priv or not pub:
        priv, pub = _generate_keypair()

    state = AgentState(
        agent_id=data["agent_id"],
        bio=data.get("bio", ""),
        status=AgentStatus(data.get("status", "active")),
        port=data.get("port", DEFAULT_PORT),
        created_at=data.get("created_at", ""),
        messages_handled=data.get("messages_handled", 0),
        claimed=data.get("claimed", False),
        private_key_hex=priv,
        public_key_hex=pub,
        display_name=data.get("display_name"),
        connections=connections,
        sent_messages=sent_messages,
        impressions=data.get("impressions", {}),
        rate_limit_global=data.get("rate_limit_global", 0),
    )

    return state


# =============================================================================
# Tool Input Models
# =============================================================================

class RequestConnectionInput(BaseModel):
    """Request a connection to another agent in the mesh."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    target_url: str = Field(..., description="The MCP server URL of the agent to connect to (e.g. 'http://localhost:8101/mcp')")


class RespondConnectionInput(BaseModel):
    """Accept or reject a pending connection request."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    request_id: str = Field(..., description="The ID of the pending connection request")
    accept: bool = Field(..., description="True to accept, False to reject")


class DisconnectInput(BaseModel):
    """Disconnect from an agent."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The agent ID to disconnect from")


class SendMessageInput(BaseModel):
    """Send a message into the mesh network."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    content: str = Field(..., description="The message content / query")
    target_agent_id: Optional[str] = Field(default=None, description="Specific agent to send to, or None to let the network route")
    metadata: Optional[dict] = Field(default_factory=dict, description="Arbitrary metadata (budget, preferences, etc.)")
    hops_remaining: int = Field(default=10, ge=1, le=50, description="How many more hops this message can take before expiring (TTL)")


class UpdateBioInput(BaseModel):
    """Update this agent's bio / specialty description."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    bio: str = Field(..., description="New bio text describing this agent's specialty", min_length=1, max_length=1000)


class SetStatusInput(BaseModel):
    """Set this agent's active/inactive status."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    status: AgentStatus = Field(..., description="'active' or 'inactive'")


class GetMessageInput(BaseModel):
    """Get full details of a specific queued message."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the queued message to inspect")


class ForwardMessageInput(BaseModel):
    """Forward a queued message to another connected agent."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the queued message to forward")
    target_agent_id: str = Field(..., description="The connected agent to forward to")
    note: Optional[str] = Field(default=None, description="Optional annotation (max 1000 chars) visible to the original sender", max_length=1000)
    force: bool = Field(default=False, description="Set to true to forward even if the target has already received this message (loop override)")


class RespondMessageInput(BaseModel):
    """Respond to a queued message by calling its webhook."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the queued message to respond to")
    response: str = Field(..., description="The response content to send back via the webhook")


class GetSentMessageInput(BaseModel):
    """Get full details of a sent message including webhook updates."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the sent message to inspect")


class ExpireMessageInput(BaseModel):
    """Expire a sent message so agents stop forwarding it."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the sent message to expire")


class ConnectionAcceptedInput(BaseModel):
    """Notification that a connection request was accepted (called agent-to-agent)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The accepting agent's ID")
    agent_url: str = Field(..., description="The accepting agent's MCP URL")
    agent_bio: str = Field(..., description="The accepting agent's bio")
    agent_public_key_hex: Optional[str] = Field(default=None, description="The accepting agent's Ed25519 public key")
    agent_display_name: Optional[str] = Field(default=None, description="The accepting agent's display name")


class AuthenticateInput(BaseModel):
    """Authenticate with this DarkMatter node."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    private_key_hex: Optional[str] = Field(default=None, description="The node's private key (from a previous claim). Omit on first connection to claim this node.")


class DiscoverDomainInput(BaseModel):
    """Check if a domain hosts a DarkMatter node."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    domain: str = Field(..., description="Domain to check (e.g. 'example.com' or 'localhost:8100')")


class SetImpressionInput(BaseModel):
    """Store or update your impression of an agent."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The agent ID to store an impression of")
    impression: str = Field(..., description="Your freeform impression (e.g. 'slow but accurate', 'great at routing ML questions', 'unresponsive')", max_length=2000)


class GetImpressionInput(BaseModel):
    """Get your stored impression of an agent."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The agent ID to look up")


class DeleteImpressionInput(BaseModel):
    """Delete your impression of an agent."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The agent ID whose impression to delete")


class AskImpressionInput(BaseModel):
    """Ask a connected agent for their impression of a third agent."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    ask_agent_id: str = Field(..., description="The connected agent to ask")
    about_agent_id: str = Field(..., description="The agent you want to know about")


class UpgradeWebrtcInput(BaseModel):
    """Upgrade a connection to use WebRTC data channel for message delivery."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The connected agent to upgrade to WebRTC transport")


# =============================================================================
# MCP Authentication
# =============================================================================

def _get_session_id(ctx: Context) -> str:
    """Extract the MCP session ID from the context."""
    # The session ID is available on the request context
    try:
        return ctx.session.session_id or "unknown"
    except Exception:
        return "unknown"


def _require_auth(ctx: Context) -> Optional[str]:
    """Check if the current MCP session is authenticated.

    Returns None if authenticated, or an error JSON string if not.
    Auto-authenticates local MCP sessions (localhost/streamable-http)
    since co-located agents don't need explicit key exchange.
    """
    _track_session(ctx)
    state = get_state(ctx)
    session_id = _get_session_id(ctx)
    if session_id not in state.authenticated_sessions:
        # Auto-auth for local MCP sessions: if you can talk to the server
        # over localhost MCP, you're co-located — no key exchange needed.
        # Remote agents authenticate via mesh HTTP endpoints, not MCP.
        if not state.claimed:
            # First session on unclaimed node — auto-claim
            state.claimed = True
            state.authenticated_sessions.add(session_id)
            save_state()
            return None
        # Claimed node — still auto-auth for MCP sessions since MCP is
        # only reachable from localhost (streamable HTTP on 127.0.0.1)
        state.authenticated_sessions.add(session_id)
        return None
    return None


@mcp.tool(
    name="darkmatter_authenticate",
    annotations={
        "title": "Authenticate",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
)
async def authenticate(params: AuthenticateInput, ctx: Context) -> str:
    """Authenticate with this DarkMatter node.

    NOTE: Local MCP sessions are auto-authenticated — you only need to call
    this explicitly to retrieve your private_key_hex for saving to memory.

    First connection: call without a private_key_hex to claim this node. You'll
    receive the node's private_key_hex — save it to your persistent memory.
    It never changes, so you only need to save it once.

    Returning: call with your saved private_key_hex.

    Args:
        params: Optional private_key_hex from a previous claim.

    Returns:
        JSON with success status and node identity.
    """
    _track_session(ctx)
    state = get_state(ctx)
    session_id = _get_session_id(ctx)

    # Always auto-auth the MCP session
    state.authenticated_sessions.add(session_id)

    if not state.claimed:
        # Unclaimed node — claim it and return the key
        state.claimed = True
        save_state()
        return json.dumps({
            "success": True,
            "status": "claimed",
            "message": "You are the first to connect. This node is now yours. "
                       "SAVE the private_key_hex to your persistent memory — "
                       "it never rotates, so you only need to save it once.",
            "private_key_hex": state.private_key_hex,
            "public_key_hex": state.public_key_hex,
            "agent_id": state.agent_id,
        })

    if params.private_key_hex is None:
        # Node already claimed, no key provided — but session is auto-authed.
        # Return identity info so the agent knows who it is.
        return json.dumps({
            "success": True,
            "status": "authenticated",
            "message": "Auto-authenticated via local MCP session. Call with your "
                       "private_key_hex if you need to verify key ownership.",
            "agent_id": state.agent_id,
            "public_key_hex": state.public_key_hex,
        })

    # Verify the provided private key derives to the stored public key
    try:
        private_bytes = bytes.fromhex(params.private_key_hex)
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        derived_pub = private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        ).hex()
    except Exception:
        return json.dumps({
            "success": False,
            "error": "Invalid private_key_hex.",
        })

    if derived_pub != state.public_key_hex:
        return json.dumps({
            "success": False,
            "error": "Private key does not match this node's identity.",
        })

    # Authenticated
    state.authenticated_sessions.add(session_id)
    return json.dumps({
        "success": True,
        "status": "authenticated",
        "agent_id": state.agent_id,
        "public_key_hex": state.public_key_hex,
    })


# =============================================================================
# Mesh Primitive Tools
# =============================================================================

@mcp.tool(
    name="darkmatter_request_connection",
    annotations={
        "title": "Request Connection",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def request_connection(params: RequestConnectionInput, ctx: Context) -> str:
    """Request a connection to another DarkMatter agent.

    Sends a connection request to the target agent. They can accept or reject it.
    If accepted, a directional connection is formed from this agent to that agent.

    Args:
        params: Contains target_url — the MCP server URL to connect to.

    Returns:
        JSON with the result of the connection request.
    """
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)

    url_err = validate_url(params.target_url)
    if url_err:
        return json.dumps({"success": False, "error": url_err})

    if len(state.connections) >= MAX_CONNECTIONS:
        return json.dumps({
            "success": False,
            "error": f"Connection limit reached ({MAX_CONNECTIONS}). Disconnect from an agent first."
        })

    # Normalize target URL: strip known suffixes so agents can pass
    # "http://host:port", "http://host:port/mcp", or "http://host:port/__darkmatter__"
    target_base = params.target_url.rstrip("/")
    for suffix in ("/mcp", "/__darkmatter__"):
        if target_base.endswith(suffix):
            target_base = target_base[:-len(suffix)]
            break

    # Call the target agent's receive_connection_request tool
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                target_base + "/__darkmatter__/connection_request",
                json={
                    "from_agent_id": state.agent_id,
                    "from_agent_url": _get_public_url(state.port),
                    "from_agent_bio": state.bio,
                    "from_agent_public_key_hex": state.public_key_hex,
                    "from_agent_display_name": state.display_name,
                }
            )
            result = response.json()

            if result.get("auto_accepted"):
                # Already connected — the target recognized us
                conn = Connection(
                    agent_id=result["agent_id"],
                    agent_url=result["agent_url"],
                    agent_bio=result.get("agent_bio", ""),
                    direction=ConnectionDirection.OUTBOUND,
                    agent_public_key_hex=result.get("agent_public_key_hex"),
                    agent_display_name=result.get("agent_display_name"),
                )
                state.connections[result["agent_id"]] = conn
                save_state()
                return json.dumps({
                    "success": True,
                    "status": "connected",
                    "agent_id": result["agent_id"],
                    "agent_bio": result.get("agent_bio", ""),
                })

            # Track that we sent this request so we can verify acceptance later
            state.pending_outbound.add(target_base)
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


@mcp.tool(
    name="darkmatter_respond_connection",
    annotations={
        "title": "Respond to Connection Request",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def respond_connection(params: RespondConnectionInput, ctx: Context) -> str:
    """Accept or reject a pending connection request from another agent.

    Args:
        params: Contains request_id and accept (bool).

    Returns:
        JSON with the result of the response.
    """
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)

    request = state.pending_requests.get(params.request_id)
    if not request:
        return json.dumps({
            "success": False,
            "error": f"No pending request with ID '{params.request_id}'."
        })

    if params.accept:
        if len(state.connections) >= MAX_CONNECTIONS:
            return json.dumps({
                "success": False,
                "error": f"Cannot accept — connection limit reached ({MAX_CONNECTIONS})."
            })

        conn = Connection(
            agent_id=request.from_agent_id,
            agent_url=request.from_agent_url,
            agent_bio=request.from_agent_bio,
            direction=ConnectionDirection.INBOUND,
            agent_public_key_hex=request.from_agent_public_key_hex,
            agent_display_name=request.from_agent_display_name,
        )
        state.connections[request.from_agent_id] = conn

        # Notify the requesting agent that we accepted
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    request.from_agent_url.rstrip("/") + "/__darkmatter__/connection_accepted",
                    json={
                        "agent_id": state.agent_id,
                        "agent_url": f"{_get_public_url(state.port)}/mcp",
                        "agent_bio": state.bio,
                        "agent_public_key_hex": state.public_key_hex,
                        "agent_display_name": state.display_name,
                    }
                )
        except Exception:
            pass  # Best effort — connection still formed on our side

    # Remove from pending
    del state.pending_requests[params.request_id]
    save_state()

    return json.dumps({
        "success": True,
        "accepted": params.accept,
        "agent_id": request.from_agent_id,
    })


@mcp.tool(
    name="darkmatter_disconnect",
    annotations={
        "title": "Disconnect from Agent",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def disconnect(params: DisconnectInput, ctx: Context) -> str:
    """Disconnect from an agent in the mesh.

    Removes the connection. The other agent is not notified — they will
    discover the disconnection when they next try to communicate.

    Args:
        params: Contains agent_id to disconnect from.

    Returns:
        JSON with the result.
    """
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)

    if params.agent_id not in state.connections:
        return json.dumps({
            "success": False,
            "error": f"Not connected to agent '{params.agent_id}'."
        })

    del state.connections[params.agent_id]
    save_state()

    return json.dumps({
        "success": True,
        "disconnected_from": params.agent_id,
    })


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


@mcp.tool(
    name="darkmatter_send_message",
    annotations={
        "title": "Send Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def send_message(params: SendMessageInput, ctx: Context) -> str:
    """Send a message into the DarkMatter mesh network.

    Auto-generates a webhook URL hosted on this server. The webhook accumulates
    routing updates (forwarding notifications, responses) so you can track the
    message's journey through the mesh in real-time.

    Use darkmatter_list_messages and darkmatter_get_sent_message to check on
    messages you've sent.

    Args:
        params: Contains content, optional target_agent_id, metadata, and hops_remaining.

    Returns:
        JSON with the message ID, routing info, and webhook URL.
    """
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)

    message_id = f"msg-{uuid.uuid4().hex[:12]}"
    metadata = params.metadata or {}

    # Auto-generate webhook URL
    public_url = _get_public_url(state.port)
    webhook = f"{public_url}/__darkmatter__/webhook/{message_id}"

    if params.target_agent_id:
        # Direct send to a specific connected agent
        conn = state.connections.get(params.target_agent_id)
        if not conn:
            return json.dumps({
                "success": False,
                "error": f"Not connected to agent '{params.target_agent_id}'."
            })

        targets = [conn]
    else:
        # Broadcast to all active connections
        targets = [c for c in state.connections.values()]

    if not targets:
        return json.dumps({
            "success": False,
            "error": "No connections available to route this message."
        })

    # Sign the outbound message
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

    # Create SentMessage tracking entry
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
    return json.dumps(result)


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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)
    state.bio = params.bio
    save_state()
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

    Args:
        params: Contains the status ('active' or 'inactive').

    Returns:
        JSON confirming the status change.
    """
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)
    state.status = params.status
    save_state()
    return json.dumps({"success": True, "status": state.status.value})


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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)
    return json.dumps({
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "bio": state.bio,
        "status": state.status.value,
        "port": state.port,
        "num_connections": len(state.connections),
        "num_pending_requests": len(state.pending_requests),
        "messages_handled": state.messages_handled,
        "message_queue_size": len(state.message_queue),
        "sent_messages_count": len(state.sent_messages),
        "created_at": state.created_at,
    })


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

    Shows each connection's agent ID, bio, direction, message counts,
    response times, and last activity.

    Returns:
        JSON array of connection details.
    """
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
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
            "direction": conn.direction.value,
            "connected_at": conn.connected_at,
            "messages_sent": conn.messages_sent,
            "messages_received": conn.messages_received,
            "messages_declined": conn.messages_declined,
            "avg_response_time_ms": round(conn.avg_response_time_ms, 2),
            "last_activity": conn.last_activity,
            "rate_limit": conn.rate_limit if conn.rate_limit != 0 else DEFAULT_RATE_LIMIT_PER_CONNECTION,
        }
        impression = state.impressions.get(conn.agent_id)
        if impression:
            entry["impression"] = _truncate(impression, 500)
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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)
    requests = []
    for req in state.pending_requests.values():
        requests.append({
            "request_id": req.request_id,
            "from_agent_id": req.from_agent_id,
            "from_agent_display_name": req.from_agent_display_name,
            "from_agent_url": req.from_agent_url,
            "from_agent_bio": req.from_agent_bio,
            "crypto": req.from_agent_public_key_hex is not None,
            "requested_at": req.requested_at,
        })

    return json.dumps({"total": len(requests), "requests": requests})


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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
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
# Message Response Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_respond_message",
    annotations={
        "title": "Respond to Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def respond_message(params: RespondMessageInput, ctx: Context) -> str:
    """Respond to a queued message by calling its webhook with your response.

    Finds the message in the queue, removes it, and POSTs the response
    to the message's webhook URL. The webhook accumulates all routing data,
    so no trace or forward_notes need to travel with the response.

    Args:
        params: Contains message_id and response string.

    Returns:
        JSON with the result of the webhook call.
    """
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)

    # Find and remove the message from the queue
    msg = None
    for i, m in enumerate(state.message_queue):
        if m.message_id == params.message_id:
            msg = state.message_queue.pop(i)
            break

    if msg is None:
        return json.dumps({
            "success": False,
            "error": f"No queued message with ID '{params.message_id}'."
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

    # Sign the webhook response
    resp_timestamp = datetime.now(timezone.utc).isoformat()
    resp_signature_hex = None
    if state.private_key_hex:
        resp_signature_hex = _sign_message(
            state.private_key_hex, state.agent_id, msg.message_id, resp_timestamp, params.response
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
                "response": params.response,
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


# =============================================================================
# Message Forwarding Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_forward_message",
    annotations={
        "title": "Forward Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def forward_message(params: ForwardMessageInput, ctx: Context) -> str:
    """Forward a queued message to another connected agent.

    The message stays in your inbox after forwarding, so you can fork it to
    multiple agents. Call darkmatter_respond_message when you're done to remove it.

    Before forwarding, checks the webhook to verify the message is still active
    and performs loop detection. Posts a forwarding update to the webhook so the
    sender has real-time routing visibility.

    Args:
        params: Contains message_id, target_agent_id, and optional note.

    Returns:
        JSON with the forwarding result.
    """
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)

    # Find the message in the queue (don't remove yet)
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

    # Validate target connection exists
    conn = state.connections.get(params.target_agent_id)
    if not conn:
        return json.dumps({
            "success": False,
            "error": f"Not connected to agent '{params.target_agent_id}'."
        })

    # GET webhook status — verify message is still active + loop detection
    loop_warning = None
    webhook_err = validate_webhook_url(msg.webhook)
    if webhook_err:
        # Can't reach webhook — still allow forwarding but log it
        print(f"[DarkMatter] Warning: cannot validate webhook for {msg.message_id}: {webhook_err}", file=sys.stderr)
    else:
        try:
            status_resp = await _webhook_request_with_recovery(
                state, msg.webhook, msg.from_agent_id,
                method="GET", timeout=10.0,
            )
            if status_resp.status_code == 200:
                webhook_data = status_resp.json()

                # Check if message is still active
                msg_status = webhook_data.get("status", "active")
                if msg_status in ("expired", "responded"):
                    # Message is no longer active — remove from queue
                    state.message_queue.pop(msg_index)
                    save_state()
                    return json.dumps({
                        "success": False,
                        "error": f"Message is already {msg_status} (checked via webhook). Removed from queue.",
                    })

                # Loop detection: block with hint unless force=True
                for update in webhook_data.get("updates", []):
                    if update.get("target_agent_id") == params.target_agent_id:
                        if params.force:
                            loop_warning = f"Warning: agent '{params.target_agent_id}' has already received this message. Forwarding anyway (force=true)."
                        else:
                            return json.dumps({
                                "success": False,
                                "error": f"Agent '{params.target_agent_id}' has already received this message. To forward anyway, retry with force=true.",
                            })
                        break
        except Exception as e:
            # Webhook unreachable — proceed anyway (best effort)
            print(f"[DarkMatter] Warning: webhook status check failed for {msg.message_id}: {e}", file=sys.stderr)

    # TTL check
    if msg.hops_remaining <= 0:
        # Remove the message — can't forward with no hops
        state.message_queue.pop(msg_index)

        # Notify webhook of TTL expiry
        if not webhook_err:
            try:
                await _webhook_request_with_recovery(
                    state, msg.webhook, msg.from_agent_id,
                    method="POST", timeout=30.0,
                    json={
                        "type": "expired",
                        "agent_id": state.agent_id,
                        "note": "Message expired — no hops remaining.",
                    }
                )
            except Exception:
                pass

        save_state()
        return json.dumps({
            "success": False,
            "error": f"Message expired — hops_remaining is 0.",
        })

    # Message stays in queue — agent can fork to multiple targets.
    # Each fork gets hops_remaining - 1. The message is only removed
    # when the agent responds to it (darkmatter_respond_message) or
    # it expires/is answered (checked on next forward attempt).
    new_hops_remaining = msg.hops_remaining - 1

    # POST forwarding update to webhook
    if not webhook_err:
        try:
            await _webhook_request_with_recovery(
                state, msg.webhook, msg.from_agent_id,
                method="POST", timeout=10.0,
                json={
                    "type": "forwarded",
                    "agent_id": state.agent_id,
                    "target_agent_id": params.target_agent_id,
                    "note": params.note,
                }
            )
        except Exception as e:
            print(f"[DarkMatter] Warning: failed to post forwarding update to webhook: {e}", file=sys.stderr)

    # Sign the forwarded message
    fwd_timestamp = datetime.now(timezone.utc).isoformat()
    fwd_signature_hex = None
    if state.private_key_hex:
        fwd_signature_hex = _sign_message(
            state.private_key_hex, state.agent_id, msg.message_id, fwd_timestamp, msg.content
        )

    # POST message to target's message endpoint (via WebRTC or HTTP)
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
        if result.get("error"):
            return json.dumps({
                "success": False,
                "error": f"Target agent error: {result['error']}. Message still in queue.",
            })
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": f"Failed to reach target agent: {str(e)}. Message still in queue.",
        })

    # Update telemetry
    conn.messages_sent += 1
    conn.last_activity = datetime.now(timezone.utc).isoformat()
    save_state()

    result = {
        "success": True,
        "message_id": msg.message_id,
        "forwarded_to": params.target_agent_id,
        "hops_remaining_for_target": new_hops_remaining,
        "message_still_in_queue": True,
        "hint": "Message stays in your inbox — you can forward to more agents (forking) or use darkmatter_respond_message to remove it.",
        "note": params.note,
    }
    if loop_warning:
        result["warning"] = loop_warning
    return json.dumps(result)


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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
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
        "response": sm.response,
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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)

    # Read our own source as the template
    server_path = os.path.abspath(__file__)
    with open(server_path, "r") as f:
        source = f.read()

    return json.dumps({
        "template_from": state.agent_id,
        "server_source": source,
        "bootstrap_instructions": {
            "1": "Save the server source to a file (e.g. server.py)",
            "2": "Install dependencies: pip install 'mcp[cli]' httpx uvicorn starlette cryptography anyio",
            "3": "Pick a port in range 8100-8110 (check availability with: lsof -i :<port> 2>/dev/null | grep LISTEN). Do NOT set DARKMATTER_STATE_FILE — the default stores state at ~/.darkmatter/state/<port>.json, unique per port and independent of your working directory.",
            "4": "Configure .mcp.json in your project dir: {\"mcpServers\":{\"darkmatter\":{\"command\":\"python\",\"args\":[\"server.py\"],\"env\":{\"DARKMATTER_PORT\":\"<port>\",\"DARKMATTER_DISPLAY_NAME\":\"your-name\"}}}} — This uses stdio transport so the MCP client auto-starts the server.",
            "5": "Tell the user to restart their MCP client (e.g. Claude Code) so it picks up the new .mcp.json. The server starts automatically.",
            "6": "Auth is automatic for local MCP sessions. Call darkmatter_authenticate({}) to retrieve your private_key_hex and SAVE it to persistent memory.",
            "7": "Call darkmatter_update_bio with your capabilities, then darkmatter_discover_local to find peers",
            "8": f"Connect to this agent: darkmatter_request_connection(target_url='http://localhost:{state.port}')",
        },
        "core_primitives": [
            "darkmatter_request_connection",
            "darkmatter_respond_connection",
            "darkmatter_disconnect",
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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
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
    """Store or update your impression of an agent.

    Impressions are your private notes about agents you've interacted with.
    Other agents can ask you for your impression of a specific agent via the
    mesh protocol — this is how trust propagates through the network.

    Args:
        params: Contains agent_id and freeform impression text.

    Returns:
        JSON confirming the impression was saved.
    """
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)

    was_update = params.agent_id in state.impressions
    state.impressions[params.agent_id] = params.impression
    save_state()

    return json.dumps({
        "success": True,
        "agent_id": params.agent_id,
        "action": "updated" if was_update else "created",
        "impression": params.impression,
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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
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
        "impression": impression,
    })


@mcp.tool(
    name="darkmatter_delete_impression",
    annotations={
        "title": "Delete Impression",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def delete_impression(params: DeleteImpressionInput, ctx: Context) -> str:
    """Delete your stored impression of an agent.

    Args:
        params: Contains agent_id whose impression to delete.

    Returns:
        JSON confirming deletion.
    """
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)

    if params.agent_id not in state.impressions:
        return json.dumps({
            "success": False,
            "error": f"No impression stored for agent '{params.agent_id}'.",
        })

    del state.impressions[params.agent_id]
    save_state()

    return json.dumps({
        "success": True,
        "agent_id": params.agent_id,
        "action": "deleted",
    })


@mcp.tool(
    name="darkmatter_ask_impression",
    annotations={
        "title": "Ask Impression",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def ask_impression(params: AskImpressionInput, ctx: Context) -> str:
    """Ask a connected agent for their impression of another agent.

    Use this to check an agent's reputation before accepting a connection
    or routing a message. Your connected peers share their impressions
    when asked — this is how trust propagates through the network.

    Args:
        params: Contains ask_agent_id (who to ask) and about_agent_id (who to ask about).

    Returns:
        JSON with the peer's impression, or a message that they have none.
    """
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    state = get_state(ctx)

    conn = state.connections.get(params.ask_agent_id)
    if not conn:
        return json.dumps({
            "success": False,
            "error": f"Not connected to agent '{params.ask_agent_id}'.",
        })

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                conn.agent_url.rstrip("/") + f"/__darkmatter__/impression/{params.about_agent_id}",
            )
            if resp.status_code == 200:
                data = resp.json()
                return json.dumps({
                    "success": True,
                    "asked": params.ask_agent_id,
                    "about": params.about_agent_id,
                    "has_impression": data.get("has_impression", False),
                    "impression": data.get("impression"),
                })
            else:
                return json.dumps({
                    "success": False,
                    "error": f"Agent returned HTTP {resp.status_code}.",
                })
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": f"Failed to reach agent: {str(e)}",
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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
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
# WebRTC Transport Upgrade Tool
# =============================================================================

@mcp.tool(
    name="darkmatter_upgrade_webrtc",
    annotations={
        "title": "Upgrade to WebRTC",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def upgrade_webrtc(params: UpgradeWebrtcInput, ctx: Context) -> str:
    """Upgrade a connection to use WebRTC data channel for peer-to-peer messaging.

    After upgrading, messages to this peer are sent over a direct WebRTC data
    channel instead of HTTP. This enables communication through NAT and firewalls.
    Falls back to HTTP automatically if the channel closes or for large messages.

    Requires aiortc to be installed. Only works with peers that also support WebRTC.

    Args:
        params: Contains agent_id of the connected peer to upgrade.

    Returns:
        JSON with upgrade result and transport status.
    """
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err

    if not WEBRTC_AVAILABLE:
        return json.dumps({
            "success": False,
            "error": "WebRTC not available — install aiortc: pip install aiortc",
        })

    state = get_state(ctx)
    if params.agent_id not in state.connections:
        return json.dumps({
            "success": False,
            "error": f"Not connected to agent '{params.agent_id}'.",
        })

    conn = state.connections[params.agent_id]

    # Already upgraded?
    if conn.transport == "webrtc" and conn.webrtc_channel is not None:
        ready = getattr(conn.webrtc_channel, "readyState", None)
        if ready == "open":
            return json.dumps({
                "success": True,
                "already_upgraded": True,
                "transport": "webrtc",
                "agent_id": params.agent_id,
            })

    # Clean up any stale WebRTC state
    if conn.webrtc_pc is not None:
        _cleanup_webrtc(conn)

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
        print(f"[DarkMatter] WebRTC data channel closed (peer: {params.agent_id})", file=sys.stderr)
        _cleanup_webrtc(conn)

    @pc.on("connectionstatechange")
    async def on_connection_state_change():
        if pc.connectionState in ("failed", "closed"):
            print(f"[DarkMatter] WebRTC connection {pc.connectionState} (peer: {params.agent_id})", file=sys.stderr)
            _cleanup_webrtc(conn)

    # Create offer and gather ICE candidates
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    await _wait_for_ice_gathering(pc)

    # Send offer to peer via HTTP (signaling uses existing HTTP connection)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                conn.agent_url.rstrip("/") + "/__darkmatter__/webrtc_offer",
                json={
                    "from_agent_id": state.agent_id,
                    "sdp_offer": pc.localDescription.sdp,
                },
            )
            if resp.status_code != 200:
                await pc.close()
                return json.dumps({
                    "success": False,
                    "error": f"Peer rejected WebRTC offer (HTTP {resp.status_code}): {resp.text}",
                })
            answer_data = resp.json()
    except Exception as e:
        await pc.close()
        return json.dumps({
            "success": False,
            "error": f"Failed to send WebRTC offer to peer: {str(e)}",
        })

    sdp_answer = answer_data.get("sdp_answer", "")
    if not sdp_answer:
        await pc.close()
        return json.dumps({
            "success": False,
            "error": "Peer returned empty SDP answer.",
        })

    # Set remote answer
    answer = RTCSessionDescription(sdp=sdp_answer, type="answer")
    await pc.setRemoteDescription(answer)

    # Wait for data channel to open
    try:
        await asyncio.wait_for(channel_open.wait(), timeout=WEBRTC_CHANNEL_OPEN_TIMEOUT)
    except asyncio.TimeoutError:
        await pc.close()
        return json.dumps({
            "success": False,
            "error": f"WebRTC data channel did not open within {WEBRTC_CHANNEL_OPEN_TIMEOUT}s.",
        })

    # Upgrade successful
    conn.webrtc_pc = pc
    conn.webrtc_channel = channel
    conn.transport = "webrtc"

    peer_label = conn.agent_display_name or params.agent_id
    print(f"[DarkMatter] WebRTC: upgraded connection to {peer_label}", file=sys.stderr)

    return json.dumps({
        "success": True,
        "transport": "webrtc",
        "agent_id": params.agent_id,
        "display_name": conn.agent_display_name,
    })


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
    auth_err = _require_auth(ctx)
    if auth_err:
        return auth_err
    return _build_status_line()


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


async def handle_connection_request(request: Request) -> JSONResponse:
    """Handle an incoming connection request from another agent."""
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    if state.status == AgentStatus.INACTIVE:
        return JSONResponse({"error": "Agent is currently inactive"}, status_code=503)

    # Global rate limit (no per-connection since this may be from an unknown agent)
    rate_err = _check_rate_limit(state)
    if rate_err:
        return JSONResponse({"error": rate_err}, status_code=429)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    from_agent_id = data.get("from_agent_id", "")
    from_agent_url = data.get("from_agent_url", "")
    from_agent_bio = data.get("from_agent_bio", "")
    from_agent_public_key_hex = data.get("from_agent_public_key_hex")
    from_agent_display_name = data.get("from_agent_display_name")

    if not from_agent_id or not from_agent_url:
        return JSONResponse({"error": "Missing required fields"}, status_code=400)
    if len(from_agent_id) > MAX_AGENT_ID_LENGTH:
        return JSONResponse({"error": "agent_id too long"}, status_code=400)
    if len(from_agent_bio) > MAX_BIO_LENGTH:
        from_agent_bio = from_agent_bio[:MAX_BIO_LENGTH]
    url_err = validate_url(from_agent_url)
    if url_err:
        return JSONResponse({"error": url_err}, status_code=400)

    # Check if already connected
    if from_agent_id in state.connections:
        # Update peer's public key if they sent one and we don't have it
        existing = state.connections[from_agent_id]
        if from_agent_public_key_hex and not existing.agent_public_key_hex:
            existing.agent_public_key_hex = from_agent_public_key_hex
            existing.agent_display_name = from_agent_display_name
            save_state()
        return JSONResponse({
            "auto_accepted": True,
            "agent_id": state.agent_id,
            "agent_url": f"{_get_public_url(state.port)}/mcp",
            "agent_bio": state.bio,
            "agent_public_key_hex": state.public_key_hex,
            "agent_display_name": state.display_name,
            "message": "Already connected.",
        })

    # Queue the request for the agent to accept or reject
    if len(state.pending_requests) >= MESSAGE_QUEUE_MAX:
        return JSONResponse({"error": "Too many pending requests"}, status_code=429)

    request_id = f"req-{uuid.uuid4().hex[:8]}"
    state.pending_requests[request_id] = PendingConnectionRequest(
        request_id=request_id,
        from_agent_id=from_agent_id,
        from_agent_url=from_agent_url,
        from_agent_bio=from_agent_bio,
        from_agent_public_key_hex=from_agent_public_key_hex,
        from_agent_display_name=from_agent_display_name,
    )

    return JSONResponse({
        "auto_accepted": False,
        "request_id": request_id,
        "message": "Connection request queued. Awaiting agent decision.",
    })


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

    agent_id = data.get("agent_id", "")
    agent_url = data.get("agent_url", "")
    agent_bio = data.get("agent_bio", "")
    agent_public_key_hex = data.get("agent_public_key_hex")
    agent_display_name = data.get("agent_display_name")

    if not agent_id or not agent_url:
        return JSONResponse({"error": "Missing required fields"}, status_code=400)
    if len(agent_id) > MAX_AGENT_ID_LENGTH:
        return JSONResponse({"error": "agent_id too long"}, status_code=400)
    if len(agent_bio) > MAX_BIO_LENGTH:
        agent_bio = agent_bio[:MAX_BIO_LENGTH]
    url_err = validate_url(agent_url)
    if url_err:
        return JSONResponse({"error": url_err}, status_code=400)

    # Verify we actually sent a pending outbound request to this agent's URL
    agent_base = agent_url.rstrip("/").rsplit("/mcp", 1)[0].rstrip("/")
    matched = None
    for pending_url in state.pending_outbound:
        pending_base = pending_url.rsplit("/mcp", 1)[0].rstrip("/")
        if pending_base == agent_base:
            matched = pending_url
            break

    if matched is None:
        return JSONResponse(
            {"error": "No pending outbound connection request for this agent."},
            status_code=403,
        )

    state.pending_outbound.discard(matched)

    conn = Connection(
        agent_id=agent_id,
        agent_url=agent_url,
        agent_bio=agent_bio,
        direction=ConnectionDirection.OUTBOUND,
        agent_public_key_hex=agent_public_key_hex,
        agent_display_name=agent_display_name,
    )
    state.connections[agent_id] = conn
    save_state()

    return JSONResponse({"success": True})


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

    pending = state.pending_requests.get(request_id)
    if not pending:
        return JSONResponse({"error": f"No pending request with ID '{request_id}'"}, status_code=404)

    if len(state.connections) >= MAX_CONNECTIONS:
        return JSONResponse({"error": f"Connection limit reached ({MAX_CONNECTIONS})"}, status_code=429)

    conn = Connection(
        agent_id=pending.from_agent_id,
        agent_url=pending.from_agent_url,
        agent_bio=pending.from_agent_bio,
        direction=ConnectionDirection.INBOUND,
        agent_public_key_hex=pending.from_agent_public_key_hex,
        agent_display_name=pending.from_agent_display_name,
    )
    state.connections[pending.from_agent_id] = conn

    # Notify the requesting agent that we accepted
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            base = pending.from_agent_url.rstrip("/")
            # Strip /mcp suffix to get to mesh endpoint
            for suffix in ("/mcp", "/__darkmatter__"):
                if base.endswith(suffix):
                    base = base[:-len(suffix)]
                    break
            await client.post(
                base + "/__darkmatter__/connection_accepted",
                json={
                    "agent_id": state.agent_id,
                    "agent_url": f"{_get_public_url(state.port)}/mcp",
                    "agent_bio": state.bio,
                    "agent_public_key_hex": state.public_key_hex,
                    "agent_display_name": state.display_name,
                }
            )
    except Exception:
        pass  # Best effort — connection still formed on our side

    del state.pending_requests[request_id]
    save_state()

    return JSONResponse({
        "success": True,
        "accepted": True,
        "agent_id": pending.from_agent_id,
    })


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
    if len(content) > MAX_CONTENT_LENGTH:
        return {"error": f"Content exceeds {MAX_CONTENT_LENGTH} bytes"}, 413
    if from_agent_id and len(from_agent_id) > MAX_AGENT_ID_LENGTH:
        return {"error": "from_agent_id too long"}, 400
    url_err = validate_url(webhook)
    if url_err:
        return {"error": f"Invalid webhook: {url_err}"}, 400

    # Reject messages from agents we're not connected to
    if not from_agent_id or from_agent_id not in state.connections:
        return {"error": "Not connected — only connected agents can send messages."}, 403

    # Rate limit check
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

    if from_agent_id in state.connections:
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

    # Auto-spawn a claude agent to handle this message
    if AGENT_SPAWN_ENABLED:
        asyncio.create_task(_spawn_agent_for_message(state, msg))

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


async def handle_webhook_post(request: Request) -> JSONResponse:
    """Handle incoming webhook updates (forwarding notifications, responses).

    POST /__darkmatter__/webhook/{message_id}

    Body should contain:
    - type: "forwarded" | "response" | "expired"
    - agent_id: the agent posting this update
    - For "forwarded": target_agent_id, optional note
    - For "response": response text, optional metadata
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
    sm = state.sent_messages.get(message_id)
    if not sm:
        return JSONResponse({"error": f"No sent message with ID '{message_id}'"}, status_code=404)

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    update_type = data.get("type", "")
    agent_id = data.get("agent_id", "unknown")
    timestamp = datetime.now(timezone.utc).isoformat()

    if update_type == "forwarded":
        update = {
            "type": "forwarded",
            "agent_id": agent_id,
            "target_agent_id": data.get("target_agent_id", ""),
            "note": data.get("note"),
            "timestamp": timestamp,
        }
        sm.updates.append(update)
        save_state()
        return JSONResponse({"success": True, "recorded": "forwarded"})

    elif update_type == "response":
        sm.response = {
            "agent_id": agent_id,
            "response": data.get("response", ""),
            "metadata": data.get("metadata", {}),
            "timestamp": timestamp,
        }
        sm.status = "responded"
        save_state()
        return JSONResponse({"success": True, "recorded": "response"})

    elif update_type == "expired":
        update = {
            "type": "expired",
            "agent_id": agent_id,
            "note": data.get("note"),
            "timestamp": timestamp,
        }
        sm.updates.append(update)
        save_state()
        return JSONResponse({"success": True, "recorded": "expired"})

    else:
        return JSONResponse({"error": f"Unknown update type: '{update_type}'"}, status_code=400)


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
        "peers": peers,
    })


async def handle_impression_get(request: Request) -> JSONResponse:
    """Return this agent's impression of a specific agent (asked by peers)."""
    global _agent_state
    state = _agent_state

    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

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
        "impression": impression,
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
echo "=== Setup complete ==="
echo ""
echo "Add to your .mcp.json (stdio mode — auto-starts with your MCP client):"
echo '{{"mcpServers":{{"darkmatter":{{"command":"'"$VENV_PYTHON"'","args":["'"$DM_DIR"'/server.py"],"env":{{"DARKMATTER_PORT":"'$PORT'","DARKMATTER_DISPLAY_NAME":"your-agent-name"}}}}}}}}'
echo ""
echo "Then restart your MCP client. Auth is automatic — no setup needed."
echo ""
echo "Or for standalone HTTP mode (manual start):"
echo "  DARKMATTER_PORT=$PORT nohup $VENV_PYTHON $DM_DIR/server.py > /tmp/darkmatter-$PORT.log 2>&1 &"
echo "  .mcp.json: {{\\"mcpServers\\":{{\\"darkmatter\\":{{\\"type\\":\\"http\\",\\"url\\":\\"http://localhost:$PORT/mcp\\"}}}}}}"
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
    """Initialize agent state from disk or create fresh. Safe to call multiple times."""
    global _agent_state
    if _agent_state is not None:
        return  # Already initialized

    if port is None:
        port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))

    display_name = os.environ.get("DARKMATTER_DISPLAY_NAME", os.environ.get("DARKMATTER_AGENT_ID", ""))
    bio = os.environ.get("DARKMATTER_BIO", "A DarkMatter mesh agent.")

    # Try to restore persisted state from disk
    restored = load_state()
    if restored:
        _agent_state = restored
        _agent_state.port = port
        _agent_state.status = AgentStatus.ACTIVE
        if display_name:
            _agent_state.display_name = display_name
        print(f"[DarkMatter] Restored state for '{_agent_state.agent_id}' "
              f"(display: {_agent_state.display_name or 'none'}, "
              f"{len(_agent_state.connections)} connections)", file=sys.stderr)
    else:
        agent_id = str(uuid.uuid4())
        priv, pub = _generate_keypair()
        _agent_state = AgentState(
            agent_id=agent_id,
            bio=bio,
            status=AgentStatus.ACTIVE,
            port=port,
            private_key_hex=priv,
            public_key_hex=pub,
            display_name=display_name or None,
        )
        print(f"[DarkMatter] Agent '{agent_id}' (display: {display_name or 'none'}) "
              f"starting fresh on port {port}", file=sys.stderr)

    print(f"[DarkMatter] MCP auth: {'claimed' if _agent_state.claimed else 'UNCLAIMED'}", file=sys.stderr)
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

        # Discover public URL and start network health loop
        _agent_state.public_url = await _discover_public_url(port)
        asyncio.create_task(_network_health_loop(_agent_state))
        print(f"[DarkMatter] Network health loop: ENABLED ({HEALTH_CHECK_INTERVAL}s interval)", file=sys.stderr)
        print(f"[DarkMatter] UPnP: {'AVAILABLE' if UPNP_AVAILABLE else 'disabled (pip install miniupnpc)'}", file=sys.stderr)

        # Register with anchor nodes on boot
        if ANCHOR_NODES and _agent_state.public_url:
            await _broadcast_peer_update(_agent_state)
            print(f"[DarkMatter] Anchor nodes: registered with {len(ANCHOR_NODES)} anchor(s)", file=sys.stderr)
        elif ANCHOR_NODES:
            print(f"[DarkMatter] Anchor nodes: configured but no public URL yet", file=sys.stderr)

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
    ]

    # Extract the MCP ASGI handler and its session manager for lifecycle.
    # Auth is handled at the tool layer (darkmatter_authenticate), not HTTP layer.
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
    print(f"[DarkMatter] Agent auto-spawn: {'ENABLED (max ' + str(AGENT_SPAWN_MAX_CONCURRENT) + ' concurrent, ' + str(AGENT_SPAWN_MAX_PER_HOUR) + '/hr)' if AGENT_SPAWN_ENABLED else 'disabled'}", file=sys.stderr)
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

    # Load our state to get our agent_id (if we have one)
    our_state = load_state()
    our_agent_id = our_state.agent_id if our_state else None

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

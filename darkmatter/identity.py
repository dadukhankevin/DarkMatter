"""
Cryptographic identity — Ed25519 passport, signing, verification.
Input validation and rate limiting (trust boundary concerns).

Depends on: config
"""

import os
import sys
import time
import socket
import ipaddress
from typing import Optional
from urllib.parse import urlparse
from collections import deque

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

from darkmatter.config import (
    MAX_URL_LENGTH,
    DEFAULT_RATE_LIMIT_PER_CONNECTION,
    DEFAULT_RATE_LIMIT_GLOBAL,
    RATE_LIMIT_WINDOW,
)


# =============================================================================
# Ed25519 Keypair
# =============================================================================

def generate_keypair() -> tuple[str, str]:
    """Generate an Ed25519 keypair. Returns (private_key_hex, public_key_hex)."""
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private_bytes.hex(), public_bytes.hex()


def derive_public_key_hex(private_key_hex: str) -> str:
    """Derive the public key hex from a private key hex."""
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    return private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def load_or_create_passport() -> tuple[str, str]:
    """Load or create the passport (Ed25519 keypair) from the working directory.

    The passport file lives at .darkmatter/passport.key in the current working
    directory. Returns (private_key_hex, public_key_hex).
    """
    passport_dir = os.path.join(os.getcwd(), ".darkmatter")
    passport_path = os.path.join(passport_dir, "passport.key")

    if os.path.exists(passport_path):
        with open(passport_path, "r") as f:
            private_key_hex = f.read().strip()
        public_key_hex = derive_public_key_hex(private_key_hex)
        print(f"[DarkMatter] Passport loaded: {passport_path}", file=sys.stderr)
        print(f"[DarkMatter] Agent ID (public key): {public_key_hex}", file=sys.stderr)
        return private_key_hex, public_key_hex

    # Generate new passport
    private_key_hex, public_key_hex = generate_keypair()
    try:
        os.makedirs(passport_dir, exist_ok=True)
        with open(passport_path, "w") as f:
            f.write(private_key_hex + "\n")
        os.chmod(passport_path, 0o600)
    except OSError as e:
        print(f"[DarkMatter] FATAL: cannot create passport at {passport_path}: {e}", file=sys.stderr)
        print(f"[DarkMatter] Check directory permissions for {passport_dir}", file=sys.stderr)
        sys.exit(1)
    print(f"[DarkMatter] New passport created: {passport_path}", file=sys.stderr)
    print(f"[DarkMatter] Agent ID (public key): {public_key_hex}", file=sys.stderr)
    return private_key_hex, public_key_hex


# =============================================================================
# Signing & Verification — delegates to darkmatter.security
# =============================================================================

def sign_message(private_key_hex: str, from_agent_id: str, message_id: str,
                 timestamp: str, content: str) -> str:
    """Sign a canonical message payload. Returns signature as hex."""
    from darkmatter.security import sign_message as _sign
    return _sign(private_key_hex, from_agent_id, message_id, timestamp, content)


def verify_message(public_key_hex: str, signature_hex: str, from_agent_id: str,
                   message_id: str, timestamp: str, content: str) -> bool:
    """Verify a signed message payload. Returns True if valid."""
    from darkmatter.security import verify_message as _verify
    return _verify(public_key_hex, signature_hex, from_agent_id, message_id, timestamp, content)


def sign_peer_update(private_key_hex: str, agent_id: str, new_url: str, timestamp: str) -> str:
    """Sign a peer_update payload. Returns signature as hex."""
    from darkmatter.security import sign_peer_update as _sign
    return _sign(private_key_hex, agent_id, new_url, timestamp)


def verify_peer_update_signature(public_key_hex: str, signature_hex: str,
                                  agent_id: str, new_url: str, timestamp: str) -> bool:
    """Verify a signed peer_update payload. Returns True if valid."""
    from darkmatter.security import verify_peer_update_signature as _verify
    return _verify(public_key_hex, signature_hex, agent_id, new_url, timestamp)


def sign_relay_poll(private_key_hex: str, agent_id: str, timestamp: str) -> str:
    """Sign a relay poll request. Returns signature hex."""
    from darkmatter.security import sign_relay_poll as _sign
    return _sign(private_key_hex, agent_id, timestamp)


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
        addr = ipaddress.ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        pass
    try:
        info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for family, _, _, _, sockaddr in info:
            ip_str = sockaddr[0]
            addr = ipaddress.ip_address(ip_str)
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                return True
    except socket.gaierror as e:
        print(f"[DarkMatter] DNS resolution failed for {hostname}: {e}", file=sys.stderr)
    return False


def is_darkmatter_webhook(url: str, **_kwargs) -> bool:
    """Check if a URL is a DarkMatter webhook endpoint.

    Security is handled by message signing and connection verification,
    not by IP filtering. Any URL with a /__darkmatter__/webhook/ path
    is legitimate mesh traffic.
    """
    try:
        parsed = urlparse(url)
        return "/__darkmatter__/webhook/" in (parsed.path or "")
    except Exception:
        return False


def validate_webhook_url(url: str, **_kwargs) -> Optional[str]:
    """Validate a webhook URL: must be http(s), and DarkMatter webhooks
    are always allowed regardless of IP. Security comes from signed
    messages and verified connections, not IP-based filtering.
    """
    err = validate_url(url)
    if err:
        return err
    if is_darkmatter_webhook(url):
        return None
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

def check_rate_limit(state, conn=None) -> Optional[str]:
    """Check per-connection and global rate limits. Returns error string if exceeded, None if OK."""
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW

    # Global rate limit
    global_limit = state.rate_limit_global or DEFAULT_RATE_LIMIT_GLOBAL
    if global_limit > 0:
        ts = state._global_request_timestamps
        while ts and ts[0] < cutoff:
            ts.popleft()
        if len(ts) >= global_limit:
            return f"Global rate limit exceeded ({global_limit} requests per {RATE_LIMIT_WINDOW}s)"

    # Per-connection rate limit
    if conn is not None:
        if conn.rate_limit == -1:
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


# =============================================================================
# Timestamp Freshness
# =============================================================================

def is_timestamp_fresh(timestamp: str, max_age: int = None) -> bool:
    """Check if a timestamp is within max_age seconds of now."""
    from darkmatter.config import PEER_UPDATE_MAX_AGE
    if max_age is None:
        max_age = PEER_UPDATE_MAX_AGE
    try:
        from datetime import datetime, timezone
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        age = abs((datetime.now(timezone.utc) - ts).total_seconds())
        return age <= max_age
    except Exception:
        return False

"""
Network tier validation — restrict agent visibility by IP class.

Tiers:
  local  — loopback only (127.x, ::1)
  lan    — loopback + private (RFC 1918, link-local)
  global — any IP (default)

Depends on: (none — leaf module)
"""

import ipaddress
from urllib.parse import urlparse

VALID_TIERS = ("local", "lan", "global")


def ip_allowed_by_tier(ip: str, tier: str) -> bool:
    """Check if an IP address is allowed under the given network tier."""
    if tier == "global":
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        # Unresolvable (e.g. "unknown") — only allow in global tier
        return False
    if tier == "local":
        return addr.is_loopback
    if tier == "lan":
        return addr.is_loopback or addr.is_private or addr.is_link_local
    return True


def url_allowed_by_tier(url: str, tier: str) -> bool:
    """Check if a URL's host is allowed under the given network tier."""
    if tier == "global":
        return True
    try:
        host = urlparse(url).hostname
    except Exception:
        return False
    if not host:
        return False
    # Handle "localhost" as loopback
    if host == "localhost":
        return True
    return ip_allowed_by_tier(host, tier)

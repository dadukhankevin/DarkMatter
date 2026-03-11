"""
HTTP transport plugin — sends messages via HTTP POST with peer URL recovery.

Absorbs logic from network/__init__.py (http_post_to_peer, send_to_peer HTTP path)
and resilience.py (peer URL recovery on send failure).

Depends on: config, network/transport
"""

import sys
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx

from darkmatter.network.transport import Transport, SendResult
from darkmatter.logging import get_logger

_log = get_logger("http_transport")


def strip_base_url(url: str) -> str:
    """Strip /__darkmatter__/... or /mcp suffix to get the host base URL.

    Handles agent-scoped URLs like:
      https://host/__darkmatter__/{agent_id} → https://host
    """
    base = url.rstrip("/")
    # Agent-scoped: /__darkmatter__/{agent_id}
    dm_prefix = "/__darkmatter__/"
    idx = base.find(dm_prefix)
    if idx != -1:
        return base[:idx]
    for suffix in ("/mcp", "/__darkmatter__"):
        if base.endswith(suffix):
            return base[:-len(suffix)]
    return base


class HttpTransport(Transport):
    """HTTP POST transport with peer URL recovery on failure."""

    def __init__(self, lookup_peer_url_fn: Optional[Callable] = None,
                 save_state_fn: Optional[Callable] = None,
                 get_public_url_fn: Optional[Callable] = None):
        self._lookup_peer_url = lookup_peer_url_fn
        self._save_state = save_state_fn
        self._get_public_url = get_public_url_fn

    @property
    def name(self) -> str:
        return "http"

    @property
    def priority(self) -> int:
        return 50

    def get_address(self, state) -> Optional[str]:
        if self._get_public_url:
            return self._get_public_url()
        return None


    async def send(self, conn, path: str, payload: dict) -> SendResult:
        """Send via HTTP POST. On failure, attempt peer URL recovery and NAT hairpin fallback."""
        # Try direct HTTP
        try:
            result = await self._http_post(conn, path, payload)
            return SendResult(success=True, transport_name="http", response=result)
        except Exception as e:
            last_error = e

        # Direct failed — try peer URL recovery
        if self._lookup_peer_url:
            try:
                new_url = await self._lookup_peer_url(conn.agent_id)
                if new_url and new_url != conn.agent_url:
                    old_url = conn.agent_url
                    conn.agent_url = new_url
                    if self._save_state:
                        self._save_state()
                    _log.info("Recovered URL for %s...: %s -> %s",
                              conn.agent_id[:12], old_url, new_url)
                    try:
                        result = await self._http_post(conn, path, payload)
                        return SendResult(success=True, transport_name="http", response=result)
                    except Exception as e2:
                        _log.warning("HTTP retry after URL recovery also failed for %s...: %s",
                                     conn.agent_id[:12], e2)
            except Exception as e3:
                _log.warning("Peer URL lookup failed for %s...: %s",
                             conn.agent_id[:12], e3)

        # NAT hairpinning fallback: if peer's public IP matches ours (same router),
        # try their LAN URL from discovered_peers
        try:
            from darkmatter.state import get_state
            state = get_state()
            if state and self._get_public_url:
                our_url = self._get_public_url()
                our_host = urlparse(our_url).hostname or ""
                peer_host = urlparse(conn.agent_url).hostname or ""
                if our_host and peer_host and our_host == peer_host and our_host not in ("localhost", "127.0.0.1"):
                    # Same public IP — likely behind the same NAT
                    discovered = state.discovered_peers.get(conn.agent_id)
                    if discovered:
                        lan_url = discovered.get("url")
                        if lan_url and lan_url != conn.agent_url:
                            _log.info("NAT hairpin: trying LAN URL for %s...: %s",
                                      conn.agent_id[:12], lan_url)
                            old_url = conn.agent_url
                            conn.agent_url = lan_url
                            try:
                                result = await self._http_post(conn, path, payload)
                                if self._save_state:
                                    self._save_state()
                                return SendResult(success=True, transport_name="http", response=result)
                            except Exception:
                                conn.agent_url = old_url  # Restore on failure
        except Exception as e:
            _log.debug("NAT hairpin check failed: %s", e)

        return SendResult(success=False, transport_name="http", error=str(last_error))

    async def is_reachable(self, conn) -> bool:
        """Check reachability via GET /__darkmatter__/status."""
        try:
            base_url = strip_base_url(conn.agent_url)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url}/__darkmatter__/status")
                return resp.status_code == 200
        except Exception:
            return False

    async def _http_post(self, conn, path: str, payload: dict) -> dict:
        """Send an HTTP POST to a peer. Returns the response dict."""
        base_url = strip_base_url(conn.agent_url)
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(base_url + path, json=payload)
            resp.raise_for_status()
            result = resp.json()
            result["transport"] = "http"
            return result

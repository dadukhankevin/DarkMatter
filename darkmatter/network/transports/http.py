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
        """Send via HTTP POST. Fast path only — no URL recovery or retries.

        URL recovery and reconnection are handled by the ping loop in the background.
        This keeps send() fast (~5s max) even when peers are unreachable.
        """
        try:
            result = await self._http_post(conn, path, payload)
            return SendResult(success=True, transport_name="http", response=result)
        except Exception as e:
            return SendResult(success=False, transport_name="http", error=str(e))

    async def is_reachable(self, conn) -> bool:
        """Check reachability via GET /__darkmatter__/status."""
        try:
            base_url = strip_base_url(conn.agent_url)
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{base_url}/__darkmatter__/status")
                return resp.status_code == 200
        except Exception:
            return False

    async def _http_post(self, conn, path: str, payload: dict) -> dict:
        """Send an HTTP POST to a peer. Returns the response dict."""
        base_url = strip_base_url(conn.agent_url)
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(base_url + path, json=payload)
            resp.raise_for_status()
            result = resp.json()
            result["transport"] = "http"
            return result

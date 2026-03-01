"""
HTTP transport plugin — sends messages via HTTP POST with peer URL recovery.

Absorbs logic from network/__init__.py (http_post_to_peer, send_to_peer HTTP path)
and resilience.py (peer URL recovery on send failure).

Depends on: config, network/transport
"""

import sys
from typing import Callable, Optional

import httpx

from darkmatter.network.transport import Transport, SendResult


def strip_base_url(url: str) -> str:
    """Strip /mcp or /__darkmatter__ suffix from a URL."""
    base = url.rstrip("/")
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
        """Send via HTTP POST. On failure, attempt peer URL recovery and retry once."""
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
                    print(f"[DarkMatter] Recovered URL for {conn.agent_id[:12]}...: "
                          f"{old_url} -> {new_url}", file=sys.stderr)
                    try:
                        result = await self._http_post(conn, path, payload)
                        return SendResult(success=True, transport_name="http", response=result)
                    except Exception:
                        pass
            except Exception:
                pass

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
            result = resp.json()
            result["transport"] = "http"
            return result

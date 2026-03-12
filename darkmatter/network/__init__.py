"""
Network package — transport-agnostic networking with plugin system.

Re-exports the public API. All transport logic lives in transports/ plugins,
orchestration in manager.py, and protocol handlers in mesh.py.
"""

import os

import httpx

from darkmatter.network.transport import Transport, SendResult
from darkmatter.network.manager import NetworkManager, get_network_manager, set_network_manager
from darkmatter.network.transports.http import strip_base_url
from darkmatter.config import DEFAULT_PORT
from darkmatter.logging import get_logger

_log = get_logger("network")

__all__ = [
    "NetworkManager", "get_network_manager", "set_network_manager",
    "Transport", "SendResult", "strip_base_url",
    "send_to_peer",
]


class SendError(Exception):
    """Raised when a message cannot be delivered by any means."""
    pass


async def send_to_peer(conn, path: str, payload: dict, **kw) -> dict:
    """Send a message to a peer via the daemon.

    The daemon has live connections (fresh URLs, WebRTC channels).
    This is the only send path — no fallback to stale local state.
    """
    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"http://127.0.0.1:{port}/__darkmatter__/send_proxy",
            json={
                "target_agent_id": conn.agent_id,
                "path": path,
                "payload": payload,
            },
        )

    if resp.status_code != 200:
        raise SendError(f"Daemon returned {resp.status_code}")

    data = resp.json()
    if data.get("success"):
        return data.get("response") or {"success": True, "transport": data.get("transport", "proxy")}

    raise SendError(data.get("error", "Send failed"))

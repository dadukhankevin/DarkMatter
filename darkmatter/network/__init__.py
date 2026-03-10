"""
Network package — transport-agnostic networking with plugin system.

Re-exports the public API. All transport logic lives in transports/ plugins,
orchestration in manager.py, and protocol handlers in mesh.py.
"""

import httpx

from darkmatter.network.transport import Transport, SendResult
from darkmatter.network.manager import NetworkManager, get_network_manager, set_network_manager
from darkmatter.network.transports.http import strip_base_url

__all__ = [
    "NetworkManager", "get_network_manager", "set_network_manager",
    "Transport", "SendResult", "strip_base_url",
    "send_to_peer",
]


async def send_to_peer(conn, path: str, payload: dict, **kw) -> dict:
    """Send a message to a peer via the NetworkManager with automatic transport selection."""
    mgr = get_network_manager()
    result = await mgr.send(conn.agent_id, path, payload)
    if not result.success:
        raise httpx.ConnectError(result.error)
    return result.response or {"success": True, "transport": result.transport_name}

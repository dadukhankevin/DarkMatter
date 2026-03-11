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
from darkmatter.logging import get_logger

_log = get_logger("network")

__all__ = [
    "NetworkManager", "get_network_manager", "set_network_manager",
    "Transport", "SendResult", "strip_base_url",
    "send_to_peer",
]


async def send_to_peer(conn, path: str, payload: dict, **kw) -> dict:
    """Send a message to a peer, proxying through the daemon when possible.

    The daemon has live connection objects with fresh URLs and WebRTC channels.
    The MCP stdio session's in-memory copies can be stale, so we prefer the
    daemon's send_proxy endpoint. Falls back to direct NetworkManager send
    if the daemon is unreachable.
    """
    from darkmatter.config import DEFAULT_PORT
    port = int(os.environ.get("DARKMATTER_PORT", str(DEFAULT_PORT)))

    # Try daemon proxy first
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/__darkmatter__/send_proxy",
                json={
                    "target_agent_id": conn.agent_id,
                    "path": path,
                    "payload": payload,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    return data.get("response") or {"success": True, "transport": data.get("transport", "proxy")}
                # Daemon processed but send failed — raise so caller sees the error
                raise httpx.ConnectError(data.get("error", "Daemon send_proxy failed"))
    except httpx.ConnectError:
        raise  # Re-raise if daemon explicitly reported send failure
    except Exception as e:
        _log.debug("Daemon send_proxy unreachable, falling back to direct send: %s", e)

    # Fallback: direct send via local NetworkManager
    mgr = get_network_manager()
    result = await mgr.send(conn.agent_id, path, payload)
    if not result.success:
        raise httpx.ConnectError(result.error)
    return result.response or {"success": True, "transport": result.transport_name}

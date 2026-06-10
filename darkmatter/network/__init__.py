"""
Network package — transport-agnostic networking with plugin system.

Re-exports the public API. All transport logic lives in transports/ plugins,
orchestration in manager.py, protocol handlers in mesh.py, and the daemon's
loopback admin API in local_api.py.
"""

from darkmatter.network.transport import Transport, SendResult
from darkmatter.network.manager import NetworkManager, get_network_manager, set_network_manager
from darkmatter.network.transports.http import strip_base_url

__all__ = [
    "NetworkManager", "get_network_manager", "set_network_manager",
    "Transport", "SendResult", "strip_base_url",
]

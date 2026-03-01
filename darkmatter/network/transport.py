"""
Transport plugin interface — ABC for pluggable network transports.

This is a leaf module with no internal dependencies.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SendResult:
    """Result of a transport send operation."""
    success: bool
    transport_name: str
    error: Optional[str] = None
    response: Optional[dict] = None


class Transport(ABC):
    """Abstract base class for network transports (HTTP, WebRTC, Bluetooth, etc.).

    Transports are registered with NetworkManager and tried in priority order.
    Lower priority = tried first.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Transport identifier (e.g. 'http', 'webrtc')."""
        ...

    @property
    @abstractmethod
    def priority(self) -> int:
        """Lower = tried first. WebRTC=10, HTTP=50."""
        ...

    @property
    def available(self) -> bool:
        """Whether this transport's dependencies are installed."""
        return True

    @abstractmethod
    async def send(self, conn, path: str, payload: dict) -> SendResult:
        """Send a message to a peer via this transport.

        Args:
            conn: Connection object for the target peer.
            path: The endpoint path (e.g. '/__darkmatter__/message').
            payload: The JSON-serializable payload to send.

        Returns:
            SendResult indicating success/failure and any response data.
        """
        ...

    @abstractmethod
    async def is_reachable(self, conn) -> bool:
        """Check if a peer is reachable via this transport."""
        ...

    def get_address(self, state) -> "str | None":
        """Return this transport's reachable address for the local agent."""
        return None

    async def upgrade(self, state, conn) -> bool:
        """Attempt to upgrade a connection to this transport.

        Override in transports that support upgrading (e.g. WebRTC).
        Returns True if upgrade succeeded.
        """
        return False

    async def cleanup(self, conn) -> None:
        """Clean up transport-specific resources for a connection.

        Override in transports that hold per-connection resources.
        """
        pass

    async def start(self, state) -> None:
        """One-time setup when the transport is started.

        Override for transports that need initialization.
        """
        pass

    async def stop(self) -> None:
        """Shutdown the transport.

        Override for transports that need cleanup on shutdown.
        """
        pass

"""
WebRTC transport plugin — data channel messaging with SDP upgrade.

Absorbs logic from network/webrtc.py (attempt_webrtc_upgrade, cleanup_webrtc,
handle_webrtc_offer, WebRTC send).

Depends on: config, network/transport
"""

import asyncio
import json
import sys
import uuid
from typing import Callable, Optional

import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer

from darkmatter.config import (
    WEBRTC_MESSAGE_SIZE_LIMIT,
    WEBRTC_ICE_SERVERS,
    WEBRTC_ICE_GATHER_TIMEOUT,
    WEBRTC_CHANNEL_OPEN_TIMEOUT,
    PEER_RELAY_SDP_TIMEOUT,
)
from darkmatter.network.transport import Transport, SendResult
from darkmatter.network.transports.http import strip_base_url
from darkmatter.security import sign_sdp
from darkmatter.logging import get_logger

_log = get_logger("webrtc")


def _make_rtc_config():
    """Build RTCConfiguration from WEBRTC_ICE_SERVERS."""
    return RTCConfiguration(
        iceServers=[RTCIceServer(**s) for s in WEBRTC_ICE_SERVERS]
    )


# =============================================================================
# Signaling Channel Abstraction
# =============================================================================

class SignalingChannel:
    """ABC for SDP offer/answer exchange used during WebRTC upgrade."""

    name: str = "unknown"

    async def send_offer(self, state, conn, offer_data: dict) -> Optional[dict]:
        """Send SDP offer, return SDP answer dict or None on failure.

        Args:
            state: AgentState
            conn: Connection to the target peer
            offer_data: {"sdp": str, "type": str, "agent_id": str}

        Returns:
            {"sdp": str, "type": str} or None
        """
        raise NotImplementedError


class DirectSignaling(SignalingChannel):
    """Level 1: HTTP POST to /__darkmatter__/webrtc_offer (existing behavior)."""

    name = "direct"

    async def send_offer(self, state, conn, offer_data: dict) -> Optional[dict]:
        base_url = strip_base_url(conn.agent_url)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{base_url}/__darkmatter__/webrtc_offer",
                    json=offer_data,
                )
                if resp.status_code != 200:
                    return None
                answer_data = resp.json()
                if not answer_data.get("sdp"):
                    return None
                return answer_data
        except Exception as e:
            _log.warning("DirectSignaling failed: %s", e)
            return None


class LANSignaling(SignalingChannel):
    """Level 2: SDP exchange via UDP multicast on LAN."""

    name = "lan"

    async def send_offer(self, state, conn, offer_data: dict) -> Optional[dict]:
        from darkmatter.network.discovery import lan_sdp_exchange
        try:
            return await lan_sdp_exchange(state, conn.agent_id, offer_data)
        except Exception as e:
            _log.warning("LANSignaling failed: %s", e)
            return None


class PeerRelaySignaling(SignalingChannel):
    """Level 3: Ask mutual peers to relay SDP to unreachable target."""

    name = "peer_relay"

    async def send_offer(self, state, conn, offer_data: dict) -> Optional[dict]:
        # Find peers that are connected to both us and the target
        candidates = [
            c for c in state.connections.values()
            if c.agent_id != conn.agent_id and c.transport in ("http", "webrtc")
        ][:5]  # Fan out to up to 5 peers

        if not candidates:
            return None

        async def _try_relay(relay_conn):
            try:
                base_url = strip_base_url(relay_conn.agent_url)
                async with httpx.AsyncClient(timeout=PEER_RELAY_SDP_TIMEOUT) as client:
                    resp = await client.post(
                        f"{base_url}/__darkmatter__/sdp_relay",
                        json={
                            "target_agent_id": conn.agent_id,
                            "offer_data": offer_data,
                            "from_agent_id": state.agent_id,
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("sdp"):
                            return data
            except Exception as e:
                _log.warning("PeerRelaySignaling: relay via %s... failed: %s",
                             relay_conn.agent_id[:12], e)
            return None

        # Race — first answer wins
        tasks = [asyncio.create_task(_try_relay(c)) for c in candidates]
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=PEER_RELAY_SDP_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                result = task.result()
                if result is not None:
                    for t in pending:
                        t.cancel()
                    return result
            # Wait for remaining
            if pending:
                done2, pending2 = await asyncio.wait(pending, timeout=2.0)
                for task in done2:
                    result = task.result()
                    if result is not None:
                        for t in pending2:
                            t.cancel()
                        return result
                for t in pending2:
                    t.cancel()
        except Exception as e:
            _log.warning("PeerRelaySignaling failed: %s", e)
            for t in tasks:
                t.cancel()
        return None


# =============================================================================
# WebRTC Transport
# =============================================================================

# Timeout for waiting for a WebRTC request/response round-trip
_WEBRTC_REQUEST_TIMEOUT = 10.0

class WebRTCTransport(Transport):
    """WebRTC data channel transport — preferred when available."""

    def __init__(self):
        # Callback: async fn(state, conn, path, payload) -> Optional[dict]
        # Set by the composition root (app.py) to dispatch incoming messages.
        self._message_dispatcher: Optional[Callable] = None
        # Pending request/response futures: {request_id: asyncio.Future}
        self._pending_requests: dict[str, asyncio.Future] = {}

    def set_message_dispatcher(self, fn: Callable) -> None:
        """Set the callback for dispatching incoming WebRTC messages.

        The dispatcher signature: async fn(state, conn, path, payload) -> Optional[dict]
        Returns a response dict for request/response paths, or None for fire-and-forget.
        """
        self._message_dispatcher = fn

    @property
    def name(self) -> str:
        return "webrtc"

    @property
    def priority(self) -> int:
        return 10  # Preferred over HTTP

    @property
    def available(self) -> bool:
        return True

    def get_address(self, state) -> Optional[str]:
        return "available" if self.available else None

    async def send(self, conn, path: str, payload: dict) -> SendResult:
        """Send via WebRTC data channel if open.

        For request/response paths, includes a request_id and waits for
        a correlated response from the peer.
        """
        if conn.webrtc_channel is None:
            return SendResult(success=False, transport_name="webrtc",
                              error="No WebRTC channel")

        try:
            ready = getattr(conn.webrtc_channel, "readyState", None)
            if ready != "open":
                return SendResult(success=False, transport_name="webrtc",
                                  error=f"Channel not open (state: {ready})")

            request_id = uuid.uuid4().hex[:16]
            envelope = {"path": path, "payload": payload, "request_id": request_id}
            data = json.dumps(envelope)

            if len(data) > WEBRTC_MESSAGE_SIZE_LIMIT:
                return SendResult(success=False, transport_name="webrtc",
                                  error=f"Message too large ({len(data)} > {WEBRTC_MESSAGE_SIZE_LIMIT})")

            # Create a future for the response before sending
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending_requests[request_id] = fut

            conn.webrtc_channel.send(data)

            # Wait for correlated response (with timeout)
            try:
                response = await asyncio.wait_for(fut, timeout=_WEBRTC_REQUEST_TIMEOUT)
                return SendResult(success=True, transport_name="webrtc", response=response)
            except asyncio.TimeoutError:
                # Fire-and-forget paths won't get a response — that's fine
                return SendResult(success=True, transport_name="webrtc",
                                  response={"success": True, "transport": "webrtc"})
            finally:
                self._pending_requests.pop(request_id, None)

        except Exception as e:
            _log.error("WebRTC send failed: %s", e)
            return SendResult(success=False, transport_name="webrtc", error=str(e))

    def _handle_incoming(self, state, conn, raw_message: str) -> None:
        """Handle an incoming WebRTC data channel message.

        Dispatches to the registered message dispatcher, or resolves
        a pending request future if this is a response.
        """
        try:
            envelope = json.loads(raw_message)
        except (json.JSONDecodeError, TypeError):
            _log.warning("WebRTC: invalid JSON from %s",
                         getattr(conn, 'agent_id', 'unknown')[:12])
            return

        # Check if this is a response to a pending request
        response_to = envelope.get("response_to")
        if response_to:
            fut = self._pending_requests.get(response_to)
            if fut and not fut.done():
                fut.set_result(envelope.get("payload", {}))
            return

        # Incoming request — dispatch
        path = envelope.get("path", "")
        payload = envelope.get("payload", {})
        request_id = envelope.get("request_id")

        if not self._message_dispatcher:
            _log.warning("WebRTC: no dispatcher registered, dropping message on path %s", path)
            return

        async def _dispatch():
            try:
                result = await self._message_dispatcher(state, conn, path, payload)
                # Send response back if the handler returned data and we have a request_id
                if result is not None and request_id and conn.webrtc_channel:
                    ready = getattr(conn.webrtc_channel, "readyState", None)
                    if ready == "open":
                        response = json.dumps({"response_to": request_id, "payload": result})
                        if len(response) <= WEBRTC_MESSAGE_SIZE_LIMIT:
                            conn.webrtc_channel.send(response)
            except Exception as e:
                _log.error("WebRTC dispatch error on %s: %s", path, e)

        asyncio.ensure_future(_dispatch())

    async def is_reachable(self, conn) -> bool:
        """Check if WebRTC data channel is open."""
        if conn.webrtc_channel is None:
            return False
        return getattr(conn.webrtc_channel, "readyState", None) == "open"


    async def upgrade(self, state, conn, signaling: Optional[SignalingChannel] = None) -> bool:
        """Upgrade a connection from HTTP to WebRTC via SDP offer/answer.

        Args:
            state: AgentState
            conn: Connection to upgrade
            signaling: SignalingChannel to use for SDP exchange.
                        Defaults to DirectSignaling() (HTTP POST, Level 1).
        """
        if conn.webrtc_channel is not None:
            return False

        if signaling is None:
            signaling = DirectSignaling()

        try:
            pc = RTCPeerConnection(configuration=_make_rtc_config())

            channel = pc.createDataChannel("darkmatter")
            channel_ready = asyncio.Event()
            transport_ref = self  # capture for closure

            @channel.on("open")
            def on_open():
                channel_ready.set()

            @channel.on("message")
            def on_message(msg):
                transport_ref._handle_incoming(state, conn, msg)

            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)

            await asyncio.sleep(WEBRTC_ICE_GATHER_TIMEOUT)

            offer_data = {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
                "agent_id": state.agent_id,
                "sdp_signature_hex": sign_sdp(state.private_key_hex, state.agent_id, pc.localDescription.sdp),
                "public_key_hex": state.public_key_hex,
            }

            answer_data = await signaling.send_offer(state, conn, offer_data)
            if not answer_data or not answer_data.get("sdp"):
                await pc.close()
                return False

            answer = RTCSessionDescription(
                sdp=answer_data["sdp"],
                type=answer_data["type"],
            )
            await pc.setRemoteDescription(answer)

            try:
                await asyncio.wait_for(channel_ready.wait(), timeout=WEBRTC_CHANNEL_OPEN_TIMEOUT)
            except asyncio.TimeoutError:
                await pc.close()
                return False

            conn.webrtc_pc = pc
            conn.webrtc_channel = channel
            conn.transport = "webrtc"
            conn._signaling_method = signaling.name
            peer = conn.agent_display_name or conn.agent_id[:12]
            _log.info("WebRTC: upgraded connection to %s via %s", peer, signaling.name)
            return True

        except Exception as e:
            _log.error("WebRTC upgrade failed for %s...: %s", conn.agent_id[:12], e)
            return False

    async def cleanup(self, conn) -> None:
        """Clean up WebRTC resources for a connection."""
        if conn.webrtc_pc is not None:
            try:
                await conn.webrtc_pc.close()
            except Exception:
                pass
        conn.webrtc_pc = None
        conn.webrtc_channel = None
        conn.transport = "http"

    def cleanup_sync(self, conn) -> None:
        """Synchronous cleanup — schedules async close if event loop is running."""
        if conn.webrtc_pc is not None:
            try:
                asyncio.get_running_loop().create_task(conn.webrtc_pc.close())
            except Exception:
                pass
        conn.webrtc_pc = None
        conn.webrtc_channel = None
        conn.transport = "http"

    async def handle_offer(self, state, offer_data: dict) -> Optional[dict]:
        """Handle an incoming WebRTC SDP offer. Returns answer dict or None.

        This is called by the mesh.py HTTP handler — it doesn't belong in the
        Transport.send() path but is transport-specific protocol logic.
        """
        agent_id = offer_data.get("agent_id", "")
        conn = state.connections.get(agent_id)
        if not conn:
            return None

        try:
            pc = RTCPeerConnection(configuration=_make_rtc_config())
            transport_ref = self  # capture for closure

            @pc.on("datachannel")
            def on_datachannel(channel):
                conn.webrtc_channel = channel
                conn.transport = "webrtc"
                peer = conn.agent_display_name or conn.agent_id[:12]
                _log.info("WebRTC: incoming channel from %s", peer)

                @channel.on("message")
                def on_message(msg):
                    transport_ref._handle_incoming(state, conn, msg)

            offer = RTCSessionDescription(
                sdp=offer_data["sdp"],
                type=offer_data["type"],
            )
            await pc.setRemoteDescription(offer)

            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            await asyncio.sleep(WEBRTC_ICE_GATHER_TIMEOUT)

            conn.webrtc_pc = pc

            return {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
            }

        except Exception as e:
            _log.error("WebRTC offer handling failed: %s", e)
            return None

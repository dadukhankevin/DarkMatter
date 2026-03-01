"""
WebRTC transport plugin — data channel messaging with SDP upgrade.

Absorbs logic from network/webrtc.py (attempt_webrtc_upgrade, cleanup_webrtc,
handle_webrtc_offer, WebRTC send).

Depends on: config, network/transport
"""

import asyncio
import json
import sys
from typing import Optional

from darkmatter.config import (
    WEBRTC_AVAILABLE,
    WEBRTC_MESSAGE_SIZE_LIMIT,
    WEBRTC_STUN_SERVERS,
    WEBRTC_ICE_GATHER_TIMEOUT,
    WEBRTC_CHANNEL_OPEN_TIMEOUT,
)
from darkmatter.network.transport import Transport, SendResult
from darkmatter.network.transports.http import strip_base_url

if WEBRTC_AVAILABLE:
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer


def _make_rtc_config():
    """Build RTCConfiguration from WEBRTC_STUN_SERVERS."""
    return RTCConfiguration(
        iceServers=[RTCIceServer(**s) for s in WEBRTC_STUN_SERVERS]
    )


class WebRTCTransport(Transport):
    """WebRTC data channel transport — preferred when available."""

    @property
    def name(self) -> str:
        return "webrtc"

    @property
    def priority(self) -> int:
        return 10  # Preferred over HTTP

    @property
    def available(self) -> bool:
        return WEBRTC_AVAILABLE

    def get_address(self, state) -> Optional[str]:
        return "available" if self.available else None

    async def send(self, conn, path: str, payload: dict) -> SendResult:
        """Send via WebRTC data channel if open."""
        if conn.webrtc_channel is None:
            return SendResult(success=False, transport_name="webrtc",
                              error="No WebRTC channel")

        try:
            ready = getattr(conn.webrtc_channel, "readyState", None)
            if ready != "open":
                return SendResult(success=False, transport_name="webrtc",
                                  error=f"Channel not open (state: {ready})")

            data = json.dumps({"path": path, "payload": payload})
            if len(data) > WEBRTC_MESSAGE_SIZE_LIMIT:
                return SendResult(success=False, transport_name="webrtc",
                                  error=f"Message too large ({len(data)} > {WEBRTC_MESSAGE_SIZE_LIMIT})")

            conn.webrtc_channel.send(data)
            return SendResult(success=True, transport_name="webrtc",
                              response={"success": True, "transport": "webrtc"})
        except Exception as e:
            print(f"[DarkMatter] WebRTC send failed: {e}", file=sys.stderr)
            return SendResult(success=False, transport_name="webrtc", error=str(e))

    async def is_reachable(self, conn) -> bool:
        """Check if WebRTC data channel is open."""
        if conn.webrtc_channel is None:
            return False
        return getattr(conn.webrtc_channel, "readyState", None) == "open"

    async def upgrade(self, state, conn) -> bool:
        """Upgrade a connection from HTTP to WebRTC via SDP offer/answer."""
        if not WEBRTC_AVAILABLE:
            return False
        if conn.webrtc_channel is not None:
            return False

        try:
            pc = RTCPeerConnection(configuration=_make_rtc_config())

            channel = pc.createDataChannel("darkmatter")
            channel_ready = asyncio.Event()

            @channel.on("open")
            def on_open():
                channel_ready.set()

            @channel.on("message")
            def on_message(msg):
                pass  # Wired up by mesh layer

            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)

            await asyncio.sleep(WEBRTC_ICE_GATHER_TIMEOUT)

            import httpx
            base_url = strip_base_url(conn.agent_url)
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{base_url}/__darkmatter__/webrtc_offer",
                    json={
                        "sdp": pc.localDescription.sdp,
                        "type": pc.localDescription.type,
                        "agent_id": state.agent_id,
                    }
                )
                if resp.status_code != 200:
                    await pc.close()
                    return False

                answer_data = resp.json()
                if not answer_data.get("sdp"):
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
            peer = conn.agent_display_name or conn.agent_id[:12]
            print(f"[DarkMatter] WebRTC: upgraded connection to {peer}", file=sys.stderr)
            return True

        except Exception as e:
            print(f"[DarkMatter] WebRTC upgrade failed for {conn.agent_id[:12]}...: {e}", file=sys.stderr)
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
        if not WEBRTC_AVAILABLE:
            return None

        agent_id = offer_data.get("agent_id", "")
        conn = state.connections.get(agent_id)
        if not conn:
            return None

        try:
            pc = RTCPeerConnection(configuration=_make_rtc_config())

            @pc.on("datachannel")
            def on_datachannel(channel):
                conn.webrtc_channel = channel
                conn.transport = "webrtc"
                peer = conn.agent_display_name or conn.agent_id[:12]
                print(f"[DarkMatter] WebRTC: incoming channel from {peer}", file=sys.stderr)

                @channel.on("message")
                def on_message(msg):
                    pass  # Wired up by mesh layer

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
            print(f"[DarkMatter] WebRTC offer handling failed: {e}", file=sys.stderr)
            return None

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
    SDP_RELAY_TIMEOUT,
    PEER_RELAY_SDP_TIMEOUT,
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
        import httpx
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
            print(f"[DarkMatter] DirectSignaling failed: {e}", file=sys.stderr)
            return None


class LANSignaling(SignalingChannel):
    """Level 2: SDP exchange via UDP multicast on LAN."""

    name = "lan"

    async def send_offer(self, state, conn, offer_data: dict) -> Optional[dict]:
        from darkmatter.network.discovery import lan_sdp_exchange
        try:
            return await lan_sdp_exchange(state, conn.agent_id, offer_data)
        except Exception as e:
            print(f"[DarkMatter] LANSignaling failed: {e}", file=sys.stderr)
            return None


class PeerRelaySignaling(SignalingChannel):
    """Level 3: Ask mutual peers to relay SDP to unreachable target."""

    name = "peer_relay"

    async def send_offer(self, state, conn, offer_data: dict) -> Optional[dict]:
        import httpx
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
            except Exception:
                pass
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
            print(f"[DarkMatter] PeerRelaySignaling failed: {e}", file=sys.stderr)
            for t in tasks:
                t.cancel()
        return None


class AnchorRelaySignaling(SignalingChannel):
    """Level 4: Buffer SDP on anchor node, poll for answer."""

    name = "anchor_relay"

    async def send_offer(self, state, conn, offer_data: dict) -> Optional[dict]:
        import httpx
        from darkmatter.config import ANCHOR_NODES
        from darkmatter.identity import sign_relay_poll
        from datetime import datetime, timezone

        if not ANCHOR_NODES:
            return None

        anchor = ANCHOR_NODES[0]

        # Post the SDP offer to anchor for the target agent to pick up
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{anchor}/__darkmatter__/sdp_relay/{conn.agent_id}",
                    json={
                        "from_agent_id": state.agent_id,
                        "offer_data": offer_data,
                        "type": "offer",
                    },
                )
                if resp.status_code != 200:
                    return None
        except Exception as e:
            print(f"[DarkMatter] AnchorRelaySignaling: failed to post offer: {e}", file=sys.stderr)
            return None

        # Poll anchor for the answer (target agent will post it back)
        deadline = asyncio.get_event_loop().time() + SDP_RELAY_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2.0)
            try:
                ts = datetime.now(timezone.utc).isoformat()
                sig = sign_relay_poll(state.private_key_hex, state.agent_id, ts)
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(
                        f"{anchor}/__darkmatter__/sdp_relay_poll/{state.agent_id}",
                        params={"signature": sig, "timestamp": ts},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        signals = data.get("signals", [])
                        for signal in signals:
                            if (signal.get("type") == "answer" and
                                    signal.get("from_agent_id") == conn.agent_id):
                                answer_data = signal.get("answer_data")
                                if answer_data and answer_data.get("sdp"):
                                    return answer_data
            except Exception:
                pass

        return None


# =============================================================================
# WebRTC Transport
# =============================================================================

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

    async def upgrade(self, state, conn, signaling: Optional[SignalingChannel] = None) -> bool:
        """Upgrade a connection from HTTP to WebRTC via SDP offer/answer.

        Args:
            state: AgentState
            conn: Connection to upgrade
            signaling: SignalingChannel to use for SDP exchange.
                        Defaults to DirectSignaling() (HTTP POST, Level 1).
        """
        if not WEBRTC_AVAILABLE:
            return False
        if conn.webrtc_channel is not None:
            return False

        if signaling is None:
            signaling = DirectSignaling()

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

            offer_data = {
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
                "agent_id": state.agent_id,
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
            print(f"[DarkMatter] WebRTC: upgraded connection to {peer} via {signaling.name}", file=sys.stderr)
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

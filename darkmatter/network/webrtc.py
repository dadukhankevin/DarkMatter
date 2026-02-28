"""
WebRTC transport — offer/answer, data channel, cleanup.

Depends on: config, models
"""

import asyncio
import json
import sys
from typing import Optional

from darkmatter.config import (
    WEBRTC_AVAILABLE,
    WEBRTC_STUN_SERVERS,
    WEBRTC_ICE_GATHER_TIMEOUT,
    WEBRTC_CHANNEL_OPEN_TIMEOUT,
)
from darkmatter.models import Connection

if WEBRTC_AVAILABLE:
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer


def cleanup_webrtc(conn: Connection) -> None:
    """Clean up WebRTC resources for a connection."""
    if conn.webrtc_pc is not None:
        try:
            asyncio.get_event_loop().create_task(conn.webrtc_pc.close())
        except Exception:
            pass
    conn.webrtc_pc = None
    conn.webrtc_channel = None
    conn.transport = "http"


async def attempt_webrtc_upgrade(state, conn: Connection) -> None:
    """Try to upgrade a connection from HTTP to WebRTC."""
    if not WEBRTC_AVAILABLE:
        return
    if conn.webrtc_channel is not None:
        return

    try:
        config = RTCConfiguration(
            iceServers=[RTCIceServer(**s) for s in WEBRTC_STUN_SERVERS]
        )
        pc = RTCPeerConnection(configuration=config)

        channel = pc.createDataChannel("darkmatter")
        channel_ready = asyncio.Event()

        @channel.on("open")
        def on_open():
            channel_ready.set()

        @channel.on("message")
        def on_message(msg):
            try:
                data = json.loads(msg)
                # Handle incoming WebRTC messages
                # This will be wired up by the mesh layer
            except Exception:
                pass

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        # Wait for ICE gathering
        await asyncio.sleep(WEBRTC_ICE_GATHER_TIMEOUT)

        # Send offer to peer
        from darkmatter.network import strip_base_url
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
                return

            answer_data = resp.json()
            if not answer_data.get("sdp"):
                await pc.close()
                return

            answer = RTCSessionDescription(
                sdp=answer_data["sdp"],
                type=answer_data["type"],
            )
            await pc.setRemoteDescription(answer)

        # Wait for channel to open
        try:
            await asyncio.wait_for(channel_ready.wait(), timeout=WEBRTC_CHANNEL_OPEN_TIMEOUT)
        except asyncio.TimeoutError:
            await pc.close()
            return

        conn.webrtc_pc = pc
        conn.webrtc_channel = channel
        conn.transport = "webrtc"
        peer = conn.agent_display_name or conn.agent_id[:12]
        print(f"[DarkMatter] WebRTC: upgraded connection to {peer}", file=sys.stderr)

    except Exception as e:
        print(f"[DarkMatter] WebRTC upgrade failed for {conn.agent_id[:12]}...: {e}", file=sys.stderr)


async def handle_webrtc_offer(state, offer_data: dict) -> Optional[dict]:
    """Handle an incoming WebRTC offer. Returns answer dict or None."""
    if not WEBRTC_AVAILABLE:
        return None

    agent_id = offer_data.get("agent_id", "")
    conn = state.connections.get(agent_id)
    if not conn:
        return None

    try:
        config = RTCConfiguration(
            iceServers=[RTCIceServer(**s) for s in WEBRTC_STUN_SERVERS]
        )
        pc = RTCPeerConnection(configuration=config)

        @pc.on("datachannel")
        def on_datachannel(channel):
            conn.webrtc_channel = channel
            conn.transport = "webrtc"
            peer = conn.agent_display_name or conn.agent_id[:12]
            print(f"[DarkMatter] WebRTC: incoming channel from {peer}", file=sys.stderr)

            @channel.on("message")
            def on_message(msg):
                try:
                    data = json.loads(msg)
                    # Handle incoming WebRTC messages
                except Exception:
                    pass

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

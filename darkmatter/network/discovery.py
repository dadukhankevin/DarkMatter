"""
LAN discovery — UDP multicast, localhost port scanning.

Depends on: config, models
"""

import asyncio
import json
import os
import socket
import sys
import time
from typing import Optional
from urllib.parse import urlparse

import httpx
from starlette.responses import JSONResponse

import darkmatter
from darkmatter.logging import get_logger
_log = get_logger("discovery")
from darkmatter.config import (
    DEFAULT_PORT,
    MAX_CONNECTIONS,
    PROTOCOL_VERSION,
    DISCOVERY_PORT,
    DISCOVERY_MCAST_GROUP,
    DISCOVERY_INTERVAL,
    DISCOVERY_LOCAL_PORTS,
    PEER_RELAY_SDP_TIMEOUT,
)
from darkmatter.models import AgentState
from darkmatter.security import verify_lan_beacon, sign_lan_beacon


# =============================================================================
# LAN SDP Exchange (Level 2 signaling)
# =============================================================================

# Pending SDP answer futures: keyed by (from_agent_id, target_agent_id)
_sdp_answer_waiters: dict[tuple[str, str], asyncio.Future] = {}

# Global reference to the multicast socket used by discovery_loop
_mcast_sock: Optional[socket.socket] = None


async def lan_sdp_exchange(state: AgentState, target_agent_id: str,
                           offer_data: dict) -> Optional[dict]:
    """Send an SDP offer via LAN multicast and wait for an answer.

    Used by LANSignaling (Level 2). Broadcasts the offer to all LAN agents;
    the target agent picks it up, processes it, and broadcasts the answer back.

    Returns the SDP answer dict or None on timeout.
    """
    global _mcast_sock
    if _mcast_sock is None:
        return None

    loop = asyncio.get_event_loop()

    # Create a future to wait for the answer
    key = (state.agent_id, target_agent_id)
    fut: asyncio.Future = loop.create_future()
    _sdp_answer_waiters[key] = fut

    try:
        # Broadcast the SDP offer via multicast
        packet = json.dumps({
            "proto": "darkmatter",
            "type": "sdp_offer",
            "target_agent_id": target_agent_id,
            "from_agent_id": state.agent_id,
            "offer_data": offer_data,
        }).encode("utf-8")

        try:
            await loop.run_in_executor(
                None, _mcast_sock.sendto, packet,
                (DISCOVERY_MCAST_GROUP, DISCOVERY_PORT),
            )
        except OSError as e:
            _log.error("LAN SDP broadcast failed: %s", e)
            return None

        # Wait for answer
        try:
            return await asyncio.wait_for(fut, timeout=PEER_RELAY_SDP_TIMEOUT)
        except asyncio.TimeoutError:
            return None
    finally:
        _sdp_answer_waiters.pop(key, None)


# =============================================================================
# Well-Known Endpoint
# =============================================================================

async def handle_well_known(request) -> "JSONResponse":
    """Return /.well-known/darkmatter.json for global discovery.

    Multi-tenant: includes an `agents` array with all hosted agents.
    Top-level fields reference the default/primary agent.
    """
    from darkmatter.state import get_state, get_state_for, list_hosted_agents
    from darkmatter.network.tier import ip_allowed_by_tier

    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    # Network tier enforcement — don't expose info to IPs outside the tier
    xff = request.headers.get("x-forwarded-for")
    client_ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown")
    if not ip_allowed_by_tier(client_ip, state.network_tier):
        return JSONResponse({"error": "tier_restricted", "tier": state.network_tier}, status_code=403)

    public_url = os.environ.get("DARKMATTER_PUBLIC_URL", "").rstrip("/")
    if not public_url:
        host = request.headers.get("host", f"localhost:{state.port}")
        scheme = request.headers.get("x-forwarded-proto", "http")
        public_url = f"{scheme}://{host}"

    # Build agents array for multi-tenant discovery
    agents = []
    for agent_id in list_hosted_agents():
        agent_state = get_state_for(agent_id)
        if agent_state is None:
            continue
        agents.append({
            "agent_id": agent_id,
            "display_name": agent_state.display_name,
            "public_key_hex": agent_state.public_key_hex,
            "bio": agent_state.bio,
            "status": agent_state.status.value,
            "accepting_connections": len(agent_state.connections) < MAX_CONNECTIONS,
            "mesh_url": f"{public_url}/__darkmatter__/{agent_id}",
        })

    response = {
        "darkmatter": True,
        "protocol_version": PROTOCOL_VERSION,
        # Primary agent fields at top level
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "bio": state.bio,
        "status": state.status.value,
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "mesh_url": f"{public_url}/__darkmatter__",
        "mcp_url": f"{public_url}/mcp",
        "webrtc_enabled": True,
        "genome_version": darkmatter.__genome_version__ or f"stock:{darkmatter.__version__}",
        "genome_author": darkmatter.__genome_author__,
        # Multi-tenant: all hosted agents
        "agents": agents,
    }

    return JSONResponse(response)


# =============================================================================
# Peer Registration
# =============================================================================

def register_peer(state: AgentState, peer_id: str, url: str, bio: str,
                  status: str, accepting: bool, source: str) -> None:
    """Register a discovered peer in state."""
    state.discovered_peers[peer_id] = {
        "url": url,
        "bio": bio,
        "status": status,
        "accepting": accepting,
        "source": source,
        "ts": time.time(),
    }


# =============================================================================
# UDP Multicast Protocol
# =============================================================================

class DiscoveryProtocol(asyncio.DatagramProtocol):
    """Receives UDP multicast discovery beacons and SDP signals from LAN agents."""

    def __init__(self, state: AgentState):
        self.state = state
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            packet = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if packet.get("proto") != "darkmatter":
            return

        msg_type = packet.get("type", "beacon")

        # Handle SDP offer (Level 2 signaling)
        if msg_type == "sdp_offer":
            self._handle_sdp_offer(packet, addr)
            return

        # Handle SDP answer (Level 2 signaling)
        if msg_type == "sdp_answer":
            self._handle_sdp_answer(packet)
            return

        # Standard discovery beacon
        peer_id = packet.get("agent_id", "")
        if not peer_id or peer_id == self.state.agent_id:
            return

        # Verify beacon signature (mandatory)
        beacon_sig = packet.get("beacon_signature_hex")
        beacon_pub = packet.get("public_key_hex")
        if not beacon_sig or not beacon_pub:
            _log.warning("Dropped unsigned beacon from %s…", peer_id[:12])
            return
        beacon_ts = str(packet.get("ts", ""))
        beacon_port = str(packet.get("port", DEFAULT_PORT))
        if not verify_lan_beacon(beacon_pub, beacon_sig, peer_id, beacon_port, beacon_ts):
            _log.warning("Dropped beacon with invalid signature from %s…", peer_id[:12])
            return

        peer_port = packet.get("port", DEFAULT_PORT)
        source_ip = addr[0]

        register_peer(
            self.state, peer_id,
            url=f"http://{source_ip}:{peer_port}",
            bio=packet.get("bio", ""),
            status=packet.get("status", "active"),
            accepting=packet.get("accepting", True),
            source="lan",
        )

    def _handle_sdp_offer(self, packet: dict, addr: tuple) -> None:
        """Handle an incoming SDP offer via LAN multicast."""
        target_id = packet.get("target_agent_id", "")
        from_id = packet.get("from_agent_id", "")
        offer_data = packet.get("offer_data")

        if target_id != self.state.agent_id:
            return  # Not for us
        if not from_id or not offer_data:
            return
        if from_id not in self.state.connections:
            return  # Only accept from connected peers

        # Process the offer asynchronously
        asyncio.ensure_future(self._process_lan_offer(from_id, offer_data))

    async def _process_lan_offer(self, from_agent_id: str, offer_data: dict) -> None:
        """Process a LAN SDP offer and broadcast the answer back."""
        global _mcast_sock
        from darkmatter.network.manager import get_network_manager

        try:
            mgr = get_network_manager()
            webrtc = mgr.get_transport("webrtc")
            if not webrtc:
                return

            answer = await webrtc.handle_offer(self.state, offer_data)
            if not answer:
                return

            # Broadcast the answer back via multicast
            response = json.dumps({
                "proto": "darkmatter",
                "type": "sdp_answer",
                "target_agent_id": from_agent_id,
                "from_agent_id": self.state.agent_id,
                "answer_data": answer,
            }).encode("utf-8")

            if _mcast_sock:
                loop = asyncio.get_event_loop()
                try:
                    await loop.run_in_executor(
                        None, _mcast_sock.sendto, response,
                        (DISCOVERY_MCAST_GROUP, DISCOVERY_PORT),
                    )
                except OSError as e:
                    _log.error("LAN SDP answer multicast failed: %s", e)

        except Exception as e:
            _log.error("LAN SDP offer processing failed: %s", e)

    def _handle_sdp_answer(self, packet: dict) -> None:
        """Handle an incoming SDP answer via LAN multicast."""
        target_id = packet.get("target_agent_id", "")
        from_id = packet.get("from_agent_id", "")
        answer_data = packet.get("answer_data")

        if target_id != self.state.agent_id:
            return  # Not for us
        if not from_id or not answer_data:
            return

        # Resolve the waiting future
        key = (self.state.agent_id, from_id)
        fut = _sdp_answer_waiters.get(key)
        if fut and not fut.done():
            fut.set_result(answer_data)


# =============================================================================
# Local Port Scanning
# =============================================================================

async def probe_port(client: httpx.AsyncClient, state: AgentState, port: int, host: str = "127.0.0.1") -> None:
    """Probe a single host:port for a DarkMatter node.

    Multi-tenant aware: discovers all agents hosted on the probed daemon.
    """
    try:
        resp = await client.get(f"http://{host}:{port}/.well-known/darkmatter.json")
        if resp.status_code != 200:
            return
        info = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError, KeyError):
        return

    source = "local" if host in ("127.0.0.1", "localhost") else "lan"

    # Multi-tenant: register all agents from the `agents` array
    agents = info.get("agents", [])
    if agents:
        for agent in agents:
            peer_id = agent.get("agent_id", "")
            if not peer_id or peer_id == state.agent_id:
                continue
            # Use agent-scoped mesh_url if available
            mesh_url = agent.get("mesh_url", f"http://{host}:{port}/__darkmatter__/{peer_id}")
            register_peer(
                state, peer_id,
                url=f"http://{host}:{port}",
                bio=agent.get("bio", ""),
                status=agent.get("status", "active"),
                accepting=agent.get("accepting_connections", True),
                source=source,
            )
    else:
        # Single-agent response (no agents array)
        peer_id = info.get("agent_id", "")
        if not peer_id or peer_id == state.agent_id:
            return
        register_peer(
            state, peer_id,
            url=f"http://{host}:{port}",
            bio=info.get("bio", ""),
            status=info.get("status", "active"),
            accepting=info.get("accepting_connections", True),
            source=source,
        )


def _get_lan_ip() -> str:
    """Get LAN IP using UDP connect trick (no actual traffic sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _dynamic_port_set(state: AgentState) -> set[int]:
    """Build a dynamic set of ports to scan based on known connections and peers.

    Includes: configured range defaults, ports from connection URLs,
    ports from discovered_peers, and standard defaults.
    Capped at 20 ports per IP to avoid flooding.
    """
    ports = {8100}

    # Add ports from connection URLs
    for conn in state.connections.values():
        try:
            parsed = urlparse(conn.agent_url)
            if parsed.port:
                ports.add(parsed.port)
        except Exception:
            pass

    # Add ports from discovered peers
    for peer_info in state.discovered_peers.values():
        try:
            parsed = urlparse(peer_info.get("url", ""))
            if parsed.port:
                ports.add(parsed.port)
        except Exception:
            pass

    # Cap to avoid flooding
    return set(sorted(ports)[:20])


async def scan_local_ports(state: AgentState) -> None:
    """Scan localhost ports + LAN subnet for other DarkMatter nodes.

    Localhost: full port range (8100-8200), all concurrent (fast loopback).
    LAN: only key ports with concurrency cap to avoid
    flooding the event loop (~762 probes max vs 25k+ before).
    """
    # Concurrency limiter for LAN probes — prevent event loop starvation
    lan_semaphore = asyncio.Semaphore(20)

    async def _throttled_probe(client, state, port, host):
        async with lan_semaphore:
            await probe_port(client, state, port, host=host)

    async with httpx.AsyncClient(timeout=httpx.Timeout(0.5, connect=0.25)) as client:
        # Localhost: scan full range (loopback is fast, no throttle needed)
        tasks = [
            probe_port(client, state, port)
            for port in DISCOVERY_LOCAL_PORTS
            if port != state.port
        ]

        # LAN: scan /24 subnet but only key ports, throttled
        # Agents listen on 8100 (default), 8101 (second agent), etc.
        # Skip LAN scanning entirely when network tier is "local"
        lan_ports = _dynamic_port_set(state) | {DEFAULT_PORT, DEFAULT_PORT + 1}
        lan_ip = _get_lan_ip()
        if lan_ip != "127.0.0.1" and state.network_tier != "local":
            subnet_prefix = lan_ip.rsplit(".", 1)[0]
            for host_octet in range(1, 255):
                ip = f"{subnet_prefix}.{host_octet}"
                if ip == lan_ip:
                    continue
                for p in lan_ports:
                    if p != state.port or ip != lan_ip:
                        tasks.append(_throttled_probe(client, state, p, host=ip))

        await asyncio.gather(*tasks, return_exceptions=True)


# =============================================================================
# Discovery Loop
# =============================================================================

async def discovery_loop(state: AgentState) -> None:
    """Periodically discover peers via local HTTP scan and LAN multicast."""
    global _mcast_sock
    loop = asyncio.get_event_loop()

    # Create multicast socket with retry (network interface may not be ready)
    mcast_sock = None
    for attempt in range(3):
        try:
            mcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            mcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            mcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
            mcast_sock.setblocking(False)
            break
        except OSError as e:
            _log.warning("Multicast socket creation attempt %d/3 failed: %s", attempt + 1, e)
            if mcast_sock:
                mcast_sock.close()
                mcast_sock = None
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s backoff

    _mcast_sock = mcast_sock
    if mcast_sock is None:
        _log.warning("Multicast socket creation failed after 3 attempts, LAN beacons disabled")

    try:
        while True:
            try:
                await scan_local_ports(state)
            except Exception as e:
                _log.error("Local port scan failed: %s", e)

            ts_val = int(time.time())
            beacon_sig = sign_lan_beacon(
                state.private_key_hex, state.agent_id, str(state.port), str(ts_val)
            ) if state.private_key_hex else ""

            packet = json.dumps({
                "proto": "darkmatter",
                "v": PROTOCOL_VERSION,
                "agent_id": state.agent_id,
                "display_name": state.display_name,
                "public_key_hex": state.public_key_hex,
                "bio": state.bio[:100],
                "port": state.port,
                "status": state.status.value,
                "accepting": len(state.connections) < MAX_CONNECTIONS,
                "ts": ts_val,
                "beacon_signature_hex": beacon_sig,
            }).encode("utf-8")

            if mcast_sock is not None and state.network_tier != "local":
                try:
                    await loop.run_in_executor(
                        None, mcast_sock.sendto, packet, (DISCOVERY_MCAST_GROUP, DISCOVERY_PORT)
                    )
                except OSError as e:
                    _log.error("Beacon multicast send failed: %s", e)

            await asyncio.sleep(DISCOVERY_INTERVAL)
    finally:
        if mcast_sock is not None:
            mcast_sock.close()

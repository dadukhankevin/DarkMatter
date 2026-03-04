"""
LAN discovery — UDP multicast, localhost port scanning, entrypoint auto-start.

Depends on: config, models
"""

import asyncio
import json
import os
import socket
import sys
import time
from typing import Optional

import httpx

import darkmatter
from darkmatter.filelock import lock_exclusive_nb, unlock
from darkmatter.config import (
    DEFAULT_PORT,
    MAX_CONNECTIONS,
    PROTOCOL_VERSION,
    WEBRTC_AVAILABLE,
    DISCOVERY_PORT,
    DISCOVERY_MCAST_GROUP,
    DISCOVERY_INTERVAL,
    DISCOVERY_LOCAL_PORTS,
    ENTRYPOINT_AUTOSTART,
    ENTRYPOINT_PORT,
    ENTRYPOINT_PATH,
    PEER_RELAY_SDP_TIMEOUT,
)
from darkmatter.models import AgentState


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
            print(f"[DarkMatter] LAN SDP broadcast failed: {e}", file=sys.stderr)
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
    """Return /.well-known/darkmatter.json for global discovery."""
    from starlette.responses import JSONResponse
    from darkmatter.state import get_state

    state = get_state()
    if state is None:
        return JSONResponse({"error": "Agent not initialized"}, status_code=503)

    public_url = os.environ.get("DARKMATTER_PUBLIC_URL", "").rstrip("/")
    if not public_url:
        host = request.headers.get("host", f"localhost:{state.port}")
        scheme = request.headers.get("x-forwarded-proto", "http")
        public_url = f"{scheme}://{host}"

    return JSONResponse({
        "darkmatter": True,
        "protocol_version": PROTOCOL_VERSION,
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "bio": state.bio,
        "status": state.status.value,
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "mesh_url": f"{public_url}/__darkmatter__",
        "mcp_url": f"{public_url}/mcp",
        "webrtc_enabled": WEBRTC_AVAILABLE,
        "genome_version": darkmatter.__genome_version__ or f"stock:{darkmatter.__version__}",
        "genome_author": darkmatter.__genome_author__,
    })


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
            return  # Drop unsigned beacons
        from darkmatter.security import verify_lan_beacon
        beacon_ts = str(packet.get("ts", ""))
        beacon_port = str(packet.get("port", DEFAULT_PORT))
        if not verify_lan_beacon(beacon_pub, beacon_sig, peer_id, beacon_port, beacon_ts):
            return  # Drop beacons with invalid signatures

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
                except OSError:
                    pass

        except Exception as e:
            print(f"[DarkMatter] LAN SDP offer processing failed: {e}", file=sys.stderr)

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

async def probe_port(client: httpx.AsyncClient, state: AgentState, port: int) -> None:
    """Probe a single localhost port for a DarkMatter node."""
    try:
        resp = await client.get(f"http://127.0.0.1:{port}/.well-known/darkmatter.json")
        if resp.status_code != 200:
            return
        info = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError, KeyError):
        return

    peer_id = info.get("agent_id", "")
    if not peer_id or peer_id == state.agent_id:
        return

    register_peer(
        state, peer_id,
        url=f"http://127.0.0.1:{port}",
        bio=info.get("bio", ""),
        status=info.get("status", "active"),
        accepting=info.get("accepting_connections", True),
        source="local",
    )


async def scan_local_ports(state: AgentState) -> None:
    """Scan localhost ports for other DarkMatter nodes concurrently."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(0.5, connect=0.25)) as client:
        tasks = [
            probe_port(client, state, port)
            for port in DISCOVERY_LOCAL_PORTS
            if port != state.port
        ]
        await asyncio.gather(*tasks, return_exceptions=True)


# =============================================================================
# Entrypoint Auto-Start
# =============================================================================

def find_entrypoint_path() -> Optional[str]:
    """Locate the entrypoint.py script for the human node.

    Search order:
    1. DARKMATTER_ENTRYPOINT_PATH env var (explicit override)
    2. ~/.darkmatter/entrypoint.py (canonical per-device location)
    """
    if ENTRYPOINT_PATH:
        return ENTRYPOINT_PATH if os.path.isfile(ENTRYPOINT_PATH) else None
    canonical = os.path.join(os.path.expanduser("~"), ".darkmatter", "entrypoint.py")
    return canonical if os.path.isfile(canonical) else None


_entrypoint_pid: Optional[int] = None  # PID of entrypoint we spawned (so we can shut it down)


async def ensure_entrypoint_running() -> None:
    """Auto-start the entrypoint (human node) on port 8200 if not already running."""
    global _entrypoint_pid

    if not ENTRYPOINT_AUTOSTART:
        return

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(0.5, connect=0.5)) as client:
            resp = await client.get(f"http://127.0.0.1:{ENTRYPOINT_PORT}/.well-known/darkmatter.json")
            if resp.status_code == 200:
                print(f"[DarkMatter] Entrypoint already running on port {ENTRYPOINT_PORT}", file=sys.stderr)
                return
    except Exception:
        pass

    import subprocess
    lockfile_path = os.path.join(os.path.expanduser("~"), ".darkmatter", "entrypoint.lock")
    try:
        lock_fd = os.open(lockfile_path, os.O_CREAT | os.O_WRONLY)
        lock_exclusive_nb(lock_fd)
    except (OSError, IOError):
        print(f"[DarkMatter] Entrypoint spawn locked by another agent, skipping", file=sys.stderr)
        return

    try:
        path = find_entrypoint_path()
        if not path:
            print(f"[DarkMatter] Entrypoint script not found, cannot auto-start", file=sys.stderr)
            return

        entrypoint_dir = os.path.dirname(os.path.abspath(path))
        log_path = os.path.join(os.path.expanduser("~"), ".darkmatter", "entrypoint.log")
        log_file = open(log_path, "a")

        print(f"[DarkMatter] Spawning entrypoint: {path}", file=sys.stderr)
        spawn_env = {k: v for k, v in os.environ.items()
                     if not k.startswith("WERKZEUG_")}
        popen_kwargs = dict(
            cwd=entrypoint_dir,
            stdout=log_file,
            stderr=log_file,
            env=spawn_env,
        )
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen([sys.executable, path], **popen_kwargs)
        _entrypoint_pid = proc.pid

        for _ in range(20):
            await asyncio.sleep(0.5)
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(0.5, connect=0.5)) as client:
                    resp = await client.get(f"http://127.0.0.1:{ENTRYPOINT_PORT}/.well-known/darkmatter.json")
                    if resp.status_code == 200:
                        print(f"[DarkMatter] Entrypoint started on port {ENTRYPOINT_PORT} (PID {_entrypoint_pid})", file=sys.stderr)
                        return
            except Exception:
                pass

        print(f"[DarkMatter] Entrypoint failed to start within 10s (check {log_path})", file=sys.stderr)
        _entrypoint_pid = None
    finally:
        unlock(lock_fd)
        os.close(lock_fd)


def shutdown_entrypoint() -> None:
    """Kill the entrypoint process we spawned. Called on primary session shutdown."""
    global _entrypoint_pid
    import signal

    if _entrypoint_pid is None:
        return

    try:
        if sys.platform == "win32":
            os.kill(_entrypoint_pid, signal.CTRL_BREAK_EVENT)
        else:
            os.kill(_entrypoint_pid, signal.SIGTERM)
        print(f"[DarkMatter] Entrypoint (PID {_entrypoint_pid}) terminated", file=sys.stderr)
    except ProcessLookupError:
        pass  # already dead
    except Exception as e:
        print(f"[DarkMatter] Failed to kill entrypoint (PID {_entrypoint_pid}): {e}", file=sys.stderr)
    _entrypoint_pid = None


# =============================================================================
# Discovery Loop
# =============================================================================

async def discovery_loop(state: AgentState) -> None:
    """Periodically discover peers via local HTTP scan and LAN multicast."""
    global _mcast_sock
    loop = asyncio.get_event_loop()

    mcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    mcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    mcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    mcast_sock.setblocking(False)
    _mcast_sock = mcast_sock

    try:
        while True:
            try:
                await scan_local_ports(state)
            except Exception:
                pass

            try:
                await ensure_entrypoint_running()
            except Exception:
                pass

            from darkmatter.security import sign_lan_beacon
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

            try:
                await loop.run_in_executor(
                    None, mcast_sock.sendto, packet, (DISCOVERY_MCAST_GROUP, DISCOVERY_PORT)
                )
            except OSError:
                pass

            await asyncio.sleep(DISCOVERY_INTERVAL)
    finally:
        mcast_sock.close()

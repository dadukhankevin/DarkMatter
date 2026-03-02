"""
LAN discovery — UDP multicast, localhost port scanning, entrypoint auto-start.

Depends on: config, models
"""

import asyncio
import fcntl
import json
import os
import socket
import sys
import time
from typing import Optional

import httpx

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
)
from darkmatter.models import AgentState


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
    """Receives UDP multicast discovery beacons from LAN agents."""

    def __init__(self, state: AgentState):
        self.state = state

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            packet = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if packet.get("proto") != "darkmatter":
            return

        peer_id = packet.get("agent_id", "")
        if not peer_id or peer_id == self.state.agent_id:
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
    """Locate the entrypoint.py script for the human node."""
    if ENTRYPOINT_PATH:
        return ENTRYPOINT_PATH if os.path.isfile(ENTRYPOINT_PATH) else None
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fallback = os.path.join(project_dir, "entrypoint.py")
    return fallback if os.path.isfile(fallback) else None


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
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
        proc = subprocess.Popen(
            [sys.executable, path],
            start_new_session=True,
            cwd=entrypoint_dir,
            stdout=log_file,
            stderr=log_file,
            env=spawn_env,
        )
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
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def shutdown_entrypoint() -> None:
    """Kill the entrypoint process we spawned. Called on primary session shutdown."""
    global _entrypoint_pid
    import signal

    if _entrypoint_pid is None:
        return

    try:
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
    loop = asyncio.get_event_loop()

    mcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    mcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    mcast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    mcast_sock.setblocking(False)

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
                "ts": int(time.time()),
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

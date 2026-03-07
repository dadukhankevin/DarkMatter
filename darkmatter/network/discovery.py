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
from darkmatter.config import (
    DEFAULT_PORT,
    MAX_CONNECTIONS,
    PROTOCOL_VERSION,
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
        "webrtc_enabled": True,
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
            print(f"[DarkMatter] Dropped unsigned beacon from {peer_id[:12]}…", file=sys.stderr)
            return
        from darkmatter.security import verify_lan_beacon
        beacon_ts = str(packet.get("ts", ""))
        beacon_port = str(packet.get("port", DEFAULT_PORT))
        if not verify_lan_beacon(beacon_pub, beacon_sig, peer_id, beacon_port, beacon_ts):
            print(f"[DarkMatter] Dropped beacon with invalid signature from {peer_id[:12]}…", file=sys.stderr)
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
                    print(f"[DarkMatter] LAN SDP answer multicast failed: {e}", file=sys.stderr)

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

async def probe_port(client: httpx.AsyncClient, state: AgentState, port: int, host: str = "127.0.0.1") -> None:
    """Probe a single host:port for a DarkMatter node."""
    try:
        resp = await client.get(f"http://{host}:{port}/.well-known/darkmatter.json")
        if resp.status_code != 200:
            return
        info = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError, KeyError):
        return

    peer_id = info.get("agent_id", "")
    if not peer_id or peer_id == state.agent_id:
        return

    source = "local" if host in ("127.0.0.1", "localhost") else "lan"
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


async def scan_local_ports(state: AgentState) -> None:
    """Scan localhost ports + LAN subnet for other DarkMatter nodes."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(0.5, connect=0.25)) as client:
        tasks = [
            probe_port(client, state, port)
            for port in DISCOVERY_LOCAL_PORTS
            if port != state.port
        ]

        # Scan LAN /24 subnet on common DarkMatter ports
        lan_ip = _get_lan_ip()
        if lan_ip != "127.0.0.1":
            subnet_prefix = lan_ip.rsplit(".", 1)[0]
            for host_octet in range(1, 255):
                ip = f"{subnet_prefix}.{host_octet}"
                if ip == lan_ip:
                    continue
                for p in (8100, 8101):
                    tasks.append(probe_port(client, state, p, host=ip))

        await asyncio.gather(*tasks, return_exceptions=True)


# =============================================================================
# Entrypoint Auto-Start
# =============================================================================

def sync_entrypoint_files() -> None:
    """Sync bundled entrypoint files to ~/.darkmatter/ if the package version is newer.

    Copies entrypoint.py and templates/ from the installed darkmatter package
    to ~/.darkmatter/ so the human node always runs the latest code.
    """
    import darkmatter
    import shutil

    dest_dir = os.path.join(os.path.expanduser("~"), ".darkmatter")
    bundled_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bundled")

    if not os.path.isdir(bundled_dir):
        return  # No bundled files (dev install without bundled/)

    # Check version marker to avoid redundant copies
    version_file = os.path.join(dest_dir, ".entrypoint_version")
    current_version = darkmatter.__version__
    try:
        with open(version_file) as f:
            installed_version = f.read().strip()
        if installed_version == current_version:
            return  # Already up to date
    except FileNotFoundError:
        pass

    os.makedirs(dest_dir, exist_ok=True)

    # Copy entrypoint.py
    src_ep = os.path.join(bundled_dir, "entrypoint.py")
    if os.path.isfile(src_ep):
        shutil.copy2(src_ep, os.path.join(dest_dir, "entrypoint.py"))

    # Copy templates/ into templates/entrypoint/ (entrypoint's template_folder)
    src_templates = os.path.join(bundled_dir, "templates")
    if os.path.isdir(src_templates):
        dest_templates = os.path.join(dest_dir, "templates")
        dest_entrypoint = os.path.join(dest_templates, "entrypoint")
        if os.path.islink(dest_templates):
            os.remove(dest_templates)
        if os.path.isdir(dest_entrypoint):
            shutil.rmtree(dest_entrypoint)
        os.makedirs(dest_templates, exist_ok=True)
        shutil.copytree(src_templates, dest_entrypoint)

    # Write version marker
    with open(version_file, "w") as f:
        f.write(current_version)

    print(f"[DarkMatter] Synced entrypoint files to {dest_dir} (v{current_version})", file=sys.stderr)


def find_entrypoint_path() -> Optional[str]:
    """Locate the entrypoint.py script for the human node.

    Search order:
    1. DARKMATTER_ENTRYPOINT_PATH env var (explicit override)
    2. ~/.darkmatter/entrypoint.py (canonical per-device location)
    """
    # Sync bundled files before searching
    sync_entrypoint_files()

    if ENTRYPOINT_PATH:
        return ENTRYPOINT_PATH if os.path.isfile(ENTRYPOINT_PATH) else None
    canonical = os.path.join(os.path.expanduser("~"), ".darkmatter", "entrypoint.py")
    return canonical if os.path.isfile(canonical) else None


_entrypoint_pid: Optional[int] = None  # PID of entrypoint we spawned (so we can shut it down)


async def _is_entrypoint_responding() -> bool:
    """Check if the entrypoint is responding on its port."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(1.0, connect=0.5)) as client:
            resp = await client.get(f"http://127.0.0.1:{ENTRYPOINT_PORT}/.well-known/darkmatter.json")
            return resp.status_code == 200
    except Exception:
        return False


def _is_entrypoint_process_alive() -> bool:
    """Check if the entrypoint process we spawned is still alive."""
    if _entrypoint_pid is None:
        return False
    try:
        os.kill(_entrypoint_pid, 0)  # signal 0 = check if process exists
        return True
    except (OSError, ProcessLookupError):
        return False


async def ensure_entrypoint_running() -> None:
    """Auto-start the entrypoint (human node) on port 8200 if not already running.

    Called on startup and every discovery loop iteration (~30s). Acts as a
    watchdog — if the entrypoint crashes, it will be restarted on the next call.
    """
    global _entrypoint_pid

    if not ENTRYPOINT_AUTOSTART:
        return

    # Check if entrypoint is responding
    if await _is_entrypoint_responding():
        return

    # Not responding — check if our spawned process died
    if _entrypoint_pid is not None and not _is_entrypoint_process_alive():
        print(f"[DarkMatter] Entrypoint process (PID {_entrypoint_pid}) died, will restart", file=sys.stderr)
        _entrypoint_pid = None

    import subprocess
    pidfile_path = os.path.join(os.path.expanduser("~"), ".darkmatter", "entrypoint.pid")

    # PID-based spawn guard: check if another agent is already spawning
    try:
        if os.path.exists(pidfile_path):
            with open(pidfile_path, "r") as f:
                content = f.read().strip()
            if content:
                old_pid = int(content)
                try:
                    os.kill(old_pid, 0)  # Check if process is alive
                    # Process exists — check if entrypoint is actually responding
                    if await _is_entrypoint_responding():
                        return
                    # PID alive but not responding — could be a zombie or wrong process.
                    # Give it a moment then proceed.
                    await asyncio.sleep(2)
                    if await _is_entrypoint_responding():
                        return
                    print(f"[DarkMatter] Stale entrypoint PID {old_pid} (alive but not responding), replacing", file=sys.stderr)
                except (OSError, ProcessLookupError):
                    print(f"[DarkMatter] Stale entrypoint PID {old_pid} (dead), cleaning up", file=sys.stderr)
            os.remove(pidfile_path)
    except (ValueError, IOError):
        # Corrupt pid file — remove and proceed
        try:
            os.remove(pidfile_path)
        except OSError:
            pass

    # Double-check after cleanup
    if await _is_entrypoint_responding():
        return

    path = find_entrypoint_path()
    if not path:
        print(f"[DarkMatter] Entrypoint script not found, cannot auto-start", file=sys.stderr)
        return

    entrypoint_dir = os.path.dirname(os.path.abspath(path))
    log_path = os.path.join(os.path.expanduser("~"), ".darkmatter", "entrypoint.log")

    spawn_env = {k: v for k, v in os.environ.items()
                 if not k.startswith("WERKZEUG_")}
    popen_kwargs = dict(
        cwd=entrypoint_dir,
        env=spawn_env,
    )
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    # Retry up to 3 times — handles port conflicts from recently-killed processes
    for attempt in range(3):
        if attempt > 0:
            await asyncio.sleep(2)
            if await _is_entrypoint_responding():
                return
        print(f"[DarkMatter] Spawning entrypoint: {path}" + (f" (attempt {attempt + 1})" if attempt else ""), file=sys.stderr)
        log_file_handle = open(log_path, "a")
        popen_kwargs["stdout"] = log_file_handle
        popen_kwargs["stderr"] = log_file_handle
        proc = subprocess.Popen([sys.executable, path], **popen_kwargs)
        _entrypoint_pid = proc.pid

        # Write PID file so other agents know we're spawning
        try:
            with open(pidfile_path, "w") as f:
                f.write(str(_entrypoint_pid))
        except IOError:
            pass

        # Wait up to 10s for the entrypoint to start responding
        started = False
        for _ in range(20):
            await asyncio.sleep(0.5)
            if proc.poll() is not None:
                print(f"[DarkMatter] Entrypoint exited with code {proc.returncode} (attempt {attempt + 1}, check {log_path})", file=sys.stderr)
                _entrypoint_pid = None
                break
            if await _is_entrypoint_responding():
                print(f"[DarkMatter] Entrypoint started on port {ENTRYPOINT_PORT} (PID {_entrypoint_pid})", file=sys.stderr)
                started = True
                break

        if started:
            return
        if _entrypoint_pid is not None:
            print(f"[DarkMatter] Entrypoint failed to respond within 10s (attempt {attempt + 1}, check {log_path})", file=sys.stderr)
            _entrypoint_pid = None

    # All attempts failed — clean up pid file
    try:
        os.remove(pidfile_path)
    except OSError:
        pass


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

    # Clean up PID file
    pidfile_path = os.path.join(os.path.expanduser("~"), ".darkmatter", "entrypoint.pid")
    try:
        os.remove(pidfile_path)
    except OSError:
        pass


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
            except Exception as e:
                print(f"[DarkMatter] Local port scan failed: {e}", file=sys.stderr)

            try:
                await ensure_entrypoint_running()
            except Exception as e:
                print(f"[DarkMatter] Entrypoint auto-start failed: {e}", file=sys.stderr)

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
            except OSError as e:
                print(f"[DarkMatter] Beacon multicast send failed: {e}", file=sys.stderr)

            await asyncio.sleep(DISCOVERY_INTERVAL)
    finally:
        mcast_sock.close()

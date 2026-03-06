"""
DarkMatter Entrypoint — Human Node for the Mesh

A localhost Flask app that IS a DarkMatter agent (passport, connections,
signed messages) but with a human as the "brain" instead of an LLM.

Usage:
    python entrypoint.py
    # Opens on http://localhost:8200
"""

import asyncio
import atexit
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import uuid
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from flask import Flask, request, jsonify, render_template, redirect, url_for, session as flask_session, send_from_directory
from werkzeug.utils import secure_filename


def _is_ajax():
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'

# ---------------------------------------------------------------------------
# Import DarkMatter internals from darkmatter/ package
# ---------------------------------------------------------------------------

from darkmatter.app import init_state
from darkmatter.state import get_state, set_state, save_state
from darkmatter.models import AgentState, AgentStatus, Connection, SentMessage
from darkmatter.identity import (
    load_or_create_passport, sign_message, sign_peer_update,
    sign_relay_poll, validate_url,
)
from darkmatter.config import (
    PROTOCOL_VERSION, MAX_CONNECTIONS, MAX_BIO_LENGTH,
    ANCHOR_NODES, SPL_TOKENS, ANTIMATTER_RATE,
    AGENT_SPAWN_ENABLED,
)
from darkmatter.network.mesh import (
    process_connection_request, process_connection_accepted,
    process_accept_pending, _process_incoming_message,
    _process_webhook_locally, process_connection_relay_callback,
    build_outbound_request_payload, build_connection_from_accepted,
    process_antimatter_match, process_antimatter_signal,
    process_antimatter_result,
)
from darkmatter.network.manager import NetworkManager, get_network_manager, set_network_manager
from darkmatter.wallet.solana import get_solana_balance, send_solana_sol, send_solana_token
import darkmatter.config
from darkmatter import __version__ as DARKMATTER_VERSION
import struct

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("DARKMATTER_ENTRYPOINT_PORT", "8200"))
SCAN_PORTS = list(range(8100, 8201)) + [PORT]  # scan 8100-8200 + our own port range
MESSAGE_TIMEOUT_SECONDS = 120       # no ACK in 120s = failed
MESSAGE_RESPONSE_TIMEOUT_SECONDS = 600  # ACK'd but no response in 10min = timed out
DISCOVERY_MCAST_GROUP = "239.77.68.77"
DISCOVERY_PORT = 8470
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "darkmatter-uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Initialize DarkMatter state
# ---------------------------------------------------------------------------

# The entrypoint needs its own passport (identity), separate from any MCP server
# running in the same directory. We chdir to a dedicated data dir so _init_state
# creates/loads a unique passport there, then restore the original cwd.
_entrypoint_data_dir = os.path.join(os.path.expanduser("~"), ".darkmatter", "entrypoint")
os.makedirs(_entrypoint_data_dir, exist_ok=True)
_original_cwd = os.getcwd()
os.chdir(_entrypoint_data_dir)

os.environ.setdefault("DARKMATTER_DISPLAY_NAME", "Human")
os.environ.setdefault("DARKMATTER_BIO", "Human operator on the DarkMatter mesh")
os.environ.setdefault("DARKMATTER_PORT", str(PORT))

init_state(PORT)
os.chdir(_original_cwd)
get_state().router_mode = "queue_only"  # entrypoint queues for human — agents handle their own spawn mode

# Set up a NetworkManager for public URL discovery and NAT detection
_mgr = NetworkManager(state_getter=get_state, state_saver=save_state)
set_network_manager(_mgr)

# Discover public URL using the same logic as the real server
# (env var > UPnP port mapping > ipify public IP > localhost fallback)
_public_url = asyncio.run(_mgr.discover_public_url())
get_state().public_url = _public_url

# Detect NAT — if behind CGNAT, use anchor relay for webhooks
get_state().nat_detected = asyncio.run(_mgr.check_nat_status(_public_url))
if get_state().nat_detected:
    print(f"[DarkMatter Entrypoint] NAT detected: True — using anchor webhook relay", file=sys.stderr)

# Register with anchor nodes (required for relay poll signature verification)
if ANCHOR_NODES and _public_url:
    _reg_state = get_state()
    _reg_ts = datetime.now(timezone.utc).isoformat()
    _reg_payload = {
        "agent_id": _reg_state.agent_id,
        "new_url": _public_url,
        "public_key_hex": _reg_state.public_key_hex,
        "timestamp": _reg_ts,
    }
    if _reg_state.private_key_hex and _reg_state.public_key_hex:
        _reg_payload["signature"] = sign_peer_update(
            _reg_state.private_key_hex, _reg_state.agent_id, _public_url, _reg_ts
        )
    for _anchor in ANCHOR_NODES:
        try:
            with httpx.Client(timeout=5.0) as _c:
                _c.post(f"{_anchor}/__darkmatter__/peer_update", json=_reg_payload)
            print(f"[DarkMatter Entrypoint] Registered with anchor: {_anchor}", file=sys.stderr)
        except Exception as _e:
            print(f"[DarkMatter Entrypoint] Anchor registration failed ({_anchor}): {_e}", file=sys.stderr)

# Remove self-connection if present (shared passport state file)
if state := get_state():
    self_id = state.agent_id
    if self_id in state.connections:
        del state.connections[self_id]

save_state()

state = get_state()

# ---------------------------------------------------------------------------
# Check for cached wallet-derived passport (overrides default passport)
# ---------------------------------------------------------------------------
_wallet_passport_path = os.path.join(_entrypoint_data_dir, "wallet_passport.key")
if os.path.exists(_wallet_passport_path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
    with open(_wallet_passport_path, "rb") as _f:
        _wallet_seed = _f.read()
    _wallet_pk = Ed25519PrivateKey.from_private_bytes(_wallet_seed)
    _wallet_priv = _wallet_pk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    _wallet_pub = _wallet_pk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    state.agent_id = _wallet_pub
    state.private_key_hex = _wallet_priv
    state.public_key_hex = _wallet_pub
    save_state()
    print(f"[DarkMatter Entrypoint] Wallet identity loaded: {_wallet_pub[:16]}...", file=sys.stderr)

# Clean up UPnP mapping on exit
# Clean up UPnP mapping on exit (async stop → sync wrapper)
def _cleanup_network():
    try:
        asyncio.run(_mgr.stop())
    except Exception:
        pass  # Shutdown cleanup — safe to swallow

atexit.register(_cleanup_network)


# ---------------------------------------------------------------------------
# Background: message timeout + dead peer detection
# ---------------------------------------------------------------------------

def _message_timeout_checker():
    """Check for unacknowledged messages and flag unreachable peers."""
    while True:
        time.sleep(10)
        now = datetime.now(timezone.utc)
        dirty = False

        # Timeout stale sent messages
        for sm in list(state.sent_messages.values()):
            if sm.status != "active":
                continue
            try:
                created = datetime.fromisoformat(sm.created_at)
            except Exception:
                print(f"[DarkMatter Entrypoint] Malformed timestamp in sent message {sm.message_id}: {sm.created_at!r}", file=sys.stderr)
                continue
            age = (now - created).total_seconds()
            has_ack = any(u.get("type") == "received" for u in sm.updates)
            timeout = MESSAGE_RESPONSE_TIMEOUT_SECONDS if has_ack else MESSAGE_TIMEOUT_SECONDS
            if age <= timeout:
                continue

            # Check if we got webhook responses
            if sm.responses:
                continue

            # Check if any routed-to peer had activity after we sent the message
            # (replies may come as regular messages, not webhook responses)
            got_activity = False
            for agent_id in sm.routed_to:
                conn = state.connections.get(agent_id)
                if conn and conn.last_activity:
                    try:
                        last = datetime.fromisoformat(conn.last_activity)
                        if last > created:
                            got_activity = True
                            break
                    except Exception:
                        pass
            if got_activity:
                # Peer was active after we sent — mark as delivered, not timed out
                if sm.status == "active":
                    sm.status = "delivered"
                    dirty = True
                continue

            reason = "No response received" if has_ack else "No acknowledgement received"
            sm.status = "timed_out"
            sm.updates.append({
                "type": "timed_out",
                "timestamp": now.isoformat(),
                "reason": reason,
            })
            # Count timeout as a failure for each routed-to peer
            for agent_id in sm.routed_to:
                conn = state.connections.get(agent_id)
                if conn:
                    conn._consecutive_failures = getattr(conn, "_consecutive_failures", 0) + 1
            dirty = True

        if dirty:
            save_state()


threading.Thread(target=_message_timeout_checker, daemon=True).start()


HEALTH_CHECK_INTERVAL = 15  # seconds between heartbeat pings


def _health_check_loop():
    """Proactively ping all connected agents to detect online/offline status."""
    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)
        dirty = False
        for conn in list(state.connections.values()):
            if conn.agent_id == state.agent_id:
                continue
            try:
                base = _resolve_base_url(conn)
                with httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
                    t0 = time.monotonic()
                    resp = client.get(base + "/.well-known/darkmatter.json")
                    elapsed_ms = round((time.monotonic() - t0) * 1000)
                    if resp.status_code == 200:
                        conn.ping_latency_ms = elapsed_ms
                        if getattr(conn, "health_status", "ok") != "ok":
                            conn.health_status = "ok"
                            conn._consecutive_failures = 0
                            dirty = True
                    else:
                        conn.ping_latency_ms = -1
                        conn._consecutive_failures = getattr(conn, "_consecutive_failures", 0) + 1
                        if conn._consecutive_failures >= 2 and getattr(conn, "health_status", "ok") != "unreachable":
                            conn.health_status = "unreachable"
                            dirty = True
            except Exception:
                conn.ping_latency_ms = -1
                conn._consecutive_failures = getattr(conn, "_consecutive_failures", 0) + 1
                if conn._consecutive_failures >= 2 and getattr(conn, "health_status", "ok") != "unreachable":
                    conn.health_status = "unreachable"
                    dirty = True
        if dirty:
            save_state()


threading.Thread(target=_health_check_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "entrypoint"))

# --- Optional PIN auth ---
_pin_file = os.path.join(_entrypoint_data_dir, ".pin")
_DARKMATTER_PIN = os.environ.get("DARKMATTER_PIN", "").strip()
if not _DARKMATTER_PIN and os.path.isfile(_pin_file):
    _DARKMATTER_PIN = open(_pin_file).read().strip()
app.secret_key = hashlib.sha256(f"darkmatter-entrypoint-session-{state.agent_id}".encode()).digest()


def _is_localhost_request():
    """Check if the request originates from the same machine."""
    remote = request.remote_addr or ""
    return remote in ("127.0.0.1", "::1", "localhost")


@app.before_request
def _pin_auth_guard():
    if not _DARKMATTER_PIN:
        return  # No PIN configured — open access
    # Localhost is always trusted (the device running the entrypoint)
    if _is_localhost_request():
        return
    # Exempt paths: login, mesh protocol, well-known
    path = request.path
    if path == "/login" or path.startswith("/__darkmatter__/") or path.startswith("/.well-known/") or path.startswith("/api/file/"):
        return
    if flask_session.get("pin_authenticated"):
        return
    # 401 for AJAX, redirect for pages
    if _is_ajax() or request.headers.get("Accept", "").startswith("application/json"):
        return jsonify({"error": "Authentication required"}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def pin_login():
    if not _DARKMATTER_PIN:
        return redirect("/")
    error = None
    if request.method == "POST":
        pin = request.form.get("pin", "").strip()
        if pin == _DARKMATTER_PIN:
            flask_session["pin_authenticated"] = True
            flask_session.permanent = True
            app.permanent_session_lifetime = __import__("datetime").timedelta(hours=24)
            return redirect("/")
        error = "Incorrect PIN"
    return render_template("pin_login.html", error=error)


@app.route("/api/pin", methods=["POST"])
def set_pin():
    """Set or clear the entrypoint PIN. Only accessible from localhost."""
    global _DARKMATTER_PIN
    if not _is_localhost_request():
        return jsonify({"error": "PIN can only be changed from the host device"}), 403
    data = request.get_json(silent=True) or {}
    new_pin = data.get("pin", "").strip()
    if new_pin and (not new_pin.isdigit() or len(new_pin) != 4):
        return jsonify({"error": "PIN must be exactly 4 digits"}), 400
    _DARKMATTER_PIN = new_pin
    if new_pin:
        with open(_pin_file, "w") as f:
            f.write(new_pin)
    elif os.path.isfile(_pin_file):
        os.remove(_pin_file)
    return jsonify({"success": True, "pin_enabled": bool(new_pin)})


@app.route("/api/pin", methods=["GET"])
def get_pin_status():
    """Check if a PIN is currently set. Only from localhost."""
    if not _is_localhost_request():
        return jsonify({"error": "Forbidden"}), 403
    return jsonify({"pin_enabled": bool(_DARKMATTER_PIN)})


@app.route("/api/security", methods=["GET"])
def get_security():
    """Return current security settings."""
    if not _is_localhost_request():
        return jsonify({"error": "Forbidden"}), 403
    ss = state.security_settings
    return jsonify({
        "pin_enabled": bool(_DARKMATTER_PIN),
        "auto_accept_local": ss.get("auto_accept_local", True),
        "sandbox_enabled": ss.get("sandbox_enabled", False),
        "sandbox_network": ss.get("sandbox_network", True),
    })


@app.route("/api/security", methods=["POST"])
def set_security():
    """Set security settings (PIN, toggles). Only accessible from localhost."""
    global _DARKMATTER_PIN
    if not _is_localhost_request():
        return jsonify({"error": "Security settings can only be changed from the host device"}), 403
    data = request.get_json(silent=True) or {}
    ss = state.security_settings

    # Handle PIN set/clear
    if "pin" in data:
        new_pin = str(data["pin"]).strip()
        if new_pin and (not new_pin.isdigit() or len(new_pin) != 4):
            return jsonify({"error": "PIN must be exactly 4 digits"}), 400
        _DARKMATTER_PIN = new_pin
        if new_pin:
            with open(_pin_file, "w") as f:
                f.write(new_pin)
        elif os.path.isfile(_pin_file):
            os.remove(_pin_file)

    # Handle boolean toggles
    for key in ("auto_accept_local", "sandbox_enabled", "sandbox_network"):
        if key in data:
            ss[key] = bool(data[key])

    # Apply sandbox config at runtime
    from darkmatter import config as _cfg
    _cfg.AGENT_SANDBOX = ss.get("sandbox_enabled", False)
    _cfg.AGENT_SANDBOX_NETWORK = ss.get("sandbox_network", True)

    save_state()
    return jsonify({
        "success": True,
        "pin_enabled": bool(_DARKMATTER_PIN),
        "auto_accept_local": ss.get("auto_accept_local", True),
        "sandbox_enabled": ss.get("sandbox_enabled", False),
        "sandbox_network": ss.get("sandbox_network", True),
    })


@app.route("/api/security/push", methods=["POST"])
def push_security():
    """Push security settings to all connected LAN agents."""
    if not _is_localhost_request():
        return jsonify({"error": "Forbidden"}), 403
    ss = state.security_settings
    payload = {
        "auto_accept_local": ss.get("auto_accept_local", True),
        "sandbox_enabled": ss.get("sandbox_enabled", False),
        "sandbox_network": ss.get("sandbox_network", True),
        "from_agent_id": state.agent_id,
    }
    pushed_to = []
    for aid, conn in state.connections.items():
        base = _resolve_base_url(conn)
        if not _is_lan_url(base):
            continue
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.post(f"{base}/__darkmatter__/security_sync", json=payload)
                if resp.status_code == 200:
                    pushed_to.append(aid)
        except Exception as e:
            print(f"[DarkMatter Entrypoint] Security push to {aid[:12]} failed: {e}", file=sys.stderr)
    return jsonify({"success": True, "pushed_to": pushed_to})


@app.route("/__darkmatter__/security_sync", methods=["POST"])
def dm_security_sync():
    """Receive security settings from a connected peer and apply locally."""
    data = request.get_json(silent=True) or {}
    from_agent_id = data.get("from_agent_id", "")
    if not from_agent_id or from_agent_id not in state.connections:
        return jsonify({"error": "Not a connected peer"}), 403
    ss = state.security_settings
    for key in ("auto_accept_local", "sandbox_enabled", "sandbox_network"):
        if key in data:
            ss[key] = bool(data[key])
    from darkmatter import config as _cfg
    _cfg.AGENT_SANDBOX = ss.get("sandbox_enabled", False)
    _cfg.AGENT_SANDBOX_NETWORK = ss.get("sandbox_network", True)
    save_state()
    print(f"[DarkMatter Entrypoint] Applied security sync from {from_agent_id[:12]}", file=sys.stderr)
    return jsonify({"success": True})


@app.route("/api/display-name", methods=["POST"])
def set_display_name():
    """Set the display name for this entrypoint node."""
    if not _is_localhost_request():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    # Prepend "Human: " if not already present
    if not name.startswith("Human"):
        name = f"Human: {name}"
    state.display_name = name[:100]
    save_state()
    # Broadcast peer_update to all connections
    my_url = _get_public_url()
    update_payload = {
        "agent_id": state.agent_id,
        "new_url": my_url,
        "display_name": state.display_name,
        "bio": state.bio,
    }
    for aid, conn in state.connections.items():
        try:
            base = _resolve_base_url(conn)
            with httpx.Client(timeout=5.0) as client:
                client.post(f"{base}/__darkmatter__/peer_update", json=update_payload)
        except Exception as e:
            print(f"[DarkMatter Entrypoint] Peer update to {aid[:12]} failed: {e}", file=sys.stderr)
    return jsonify({"success": True, "display_name": state.display_name})


def _get_public_url():
    return state.public_url or f"http://localhost:{PORT}"


def _get_lan_ip():
    """Get LAN IP using UDP connect trick (no actual traffic sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        print("[DarkMatter Entrypoint] Could not detect LAN IP, falling back to 127.0.0.1", file=sys.stderr)
        return "127.0.0.1"


def _short_id(agent_id):
    if not agent_id:
        return "unknown"
    if len(agent_id) > 16:
        return agent_id[:8] + "..." + agent_id[-4:]
    return agent_id


def _is_lan_url(url):
    """Check if a URL points to a local or LAN address."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        if host in ("localhost", "127.0.0.1", "::1"):
            return True
        parts = host.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            a = int(parts[0])
            if a == 10:
                return True
            if a == 172 and 16 <= int(parts[1]) <= 31:
                return True
            if a == 192 and int(parts[1]) == 168:
                return True
        return False
    except Exception:
        return False


def _display_name_for(agent_id):
    """Get display name for an agent from connections or discovered peers."""
    conn = state.connections.get(agent_id)
    if conn and conn.agent_display_name:
        return conn.agent_display_name
    return _short_id(agent_id)


# ---------------------------------------------------------------------------
# Discovery — scan local ports + listen for LAN multicast beacons
# ---------------------------------------------------------------------------

_discovered_agents = {}  # agent_id -> {url, display_name, bio, status, accepting, port}
_lan_peers = {}  # agent_id -> {ip, port, display_name, bio, status, accepting, ts}
_discovery_lock = threading.Lock()


def _probe_host_port(host, port):
    """Probe a host:port for a DarkMatter node."""
    if host in ("127.0.0.1", "localhost") and port == PORT:
        return None
    try:
        with httpx.Client(timeout=httpx.Timeout(0.5, connect=0.25)) as client:
            resp = client.get(f"http://{host}:{port}/.well-known/darkmatter.json")
            if resp.status_code != 200:
                return None
            info = resp.json()
            peer_id = info.get("agent_id", "")
            if not peer_id or peer_id == state.agent_id:
                return None
            return {
                "agent_id": peer_id,
                "url": f"http://{host}:{port}",
                "display_name": info.get("display_name") or _short_id(peer_id),
                "bio": info.get("bio", ""),
                "status": info.get("status", "active"),
                "accepting": info.get("accepting_connections", True),
                "port": port,
            }
    except Exception:
        return None


def _scan_local_agents():
    """Scan localhost ports + LAN subnet + known LAN peer IPs for DarkMatter agents."""
    results = {}

    # Build probe targets: localhost ports + LAN peer ip:port combos
    targets = [("127.0.0.1", p) for p in SCAN_PORTS]
    with _discovery_lock:
        now = time.time()
        for peer_id, info in list(_lan_peers.items()):
            if now - info["ts"] > 90:
                del _lan_peers[peer_id]
                continue
            targets.append((info["ip"], info["port"]))

    # Scan LAN /24 subnet on common DarkMatter ports (8100-8101)
    lan_ip = _get_lan_ip()
    if lan_ip != "127.0.0.1":
        subnet_prefix = lan_ip.rsplit(".", 1)[0]
        lan_scan_ports = [8100, 8101]
        for host_octet in range(1, 255):
            ip = f"{subnet_prefix}.{host_octet}"
            if ip == lan_ip:
                continue  # skip self
            for p in lan_scan_ports:
                targets.append((ip, p))

    with ThreadPoolExecutor(max_workers=50) as pool:
        futures = {pool.submit(_probe_host_port, h, p): (h, p) for h, p in targets}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    results[result["agent_id"]] = result
            except Exception as e:
                print(f"[DarkMatter Entrypoint] Discovery probe failed: {e}", file=sys.stderr)

    with _discovery_lock:
        _discovered_agents.clear()
        _discovered_agents.update(results)

    return results


def _multicast_listener():
    """Background thread: listen for UDP multicast beacons from LAN agents."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass

    sock.bind(("", DISCOVERY_PORT))

    # Join multicast group on all interfaces
    mreq = struct.pack("4sL", socket.inet_aton(DISCOVERY_MCAST_GROUP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(5.0)

    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except Exception:
            time.sleep(1)
            continue

        try:
            packet = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue

        if packet.get("proto") != "darkmatter":
            continue
        peer_id = packet.get("agent_id", "")
        if not peer_id or peer_id == state.agent_id:
            continue

        source_ip = addr[0]
        peer_port = packet.get("port", 8100)

        with _discovery_lock:
            _lan_peers[peer_id] = {
                "ip": source_ip,
                "port": peer_port,
                "display_name": packet.get("display_name", _short_id(peer_id)),
                "bio": packet.get("bio", ""),
                "status": packet.get("status", "active"),
                "accepting": packet.get("accepting", True),
                "ts": time.time(),
            }


def _broadcast_beacon():
    """Send a UDP multicast beacon so other LAN agents/entrypoints can find us."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        packet = json.dumps({
            "proto": "darkmatter",
            "v": PROTOCOL_VERSION,
            "agent_id": state.agent_id,
            "display_name": state.display_name or "Human",
            "public_key_hex": getattr(state, "public_key_hex", ""),
            "bio": (state.bio or "")[:100],
            "port": PORT,
            "status": state.status.value if hasattr(state.status, "value") else "active",
            "accepting": len(state.connections) < MAX_CONNECTIONS,
            "ts": int(time.time()),
        }).encode("utf-8")
        sock.sendto(packet, (DISCOVERY_MCAST_GROUP, DISCOVERY_PORT))
        sock.close()
    except Exception as e:
        print(f"[DarkMatter Entrypoint] Broadcast beacon failed: {e}", file=sys.stderr)


def _start_discovery_loop():
    """Background thread that scans for agents every 15 seconds + broadcasts beacon."""
    while True:
        try:
            _broadcast_beacon()
        except Exception as e:
            print(f"[DarkMatter Entrypoint] Discovery beacon error: {e}", file=sys.stderr)
        try:
            _scan_local_agents()
        except Exception as e:
            print(f"[DarkMatter Entrypoint] Discovery scan error: {e}", file=sys.stderr)
        time.sleep(15)


# Start discovery + multicast listener in background
_discovery_thread = threading.Thread(target=_start_discovery_loop, daemon=True)
_discovery_thread.start()
_multicast_thread = threading.Thread(target=_multicast_listener, daemon=True)
_multicast_thread.start()


# ---------------------------------------------------------------------------
# Webhook relay polling (for NAT-ed nodes)
# ---------------------------------------------------------------------------

def _relay_poll_loop():
    """Background thread: poll anchors for buffered webhook and connection callbacks.

    Tries each anchor in order, tracks last working anchor for failover.
    """
    _last_working = None
    while True:
        try:
            time.sleep(5)
            if not state.nat_detected or not ANCHOR_NODES or not state.private_key_hex:
                continue

            ts = datetime.now(timezone.utc).isoformat()
            sig = sign_relay_poll(state.private_key_hex, state.agent_id, ts)

            # Try anchors in order: last working first, then the rest
            ordered = list(ANCHOR_NODES)
            if _last_working and _last_working in ordered:
                ordered.remove(_last_working)
                ordered.insert(0, _last_working)

            for anchor in ordered:
                try:
                    with httpx.Client(timeout=10.0) as client:
                        # Poll for webhook callbacks
                        resp = client.get(
                            f"{anchor}/__darkmatter__/webhook_relay_poll/{state.agent_id}",
                            params={"signature": sig, "timestamp": ts},
                        )
                        if resp.status_code != 200:
                            continue
                        _last_working = anchor
                        data = resp.json()
                        callbacks = data.get("callbacks", [])
                        if callbacks:
                            print(f"[DarkMatter Entrypoint] Relay poll: got {len(callbacks)} callback(s)", file=sys.stderr)
                        for cb in callbacks:
                            msg_id = cb.get("message_id", "")
                            cb_data = cb.get("data", {})
                            if msg_id and cb_data:
                                result, status = _process_webhook_locally(state, msg_id, cb_data)
                                if result.get("success"):
                                    print(f"[DarkMatter Entrypoint] Relay: webhook for {msg_id} OK", file=sys.stderr)

                        # Poll for connection relay callbacks
                        resp2 = client.get(
                            f"{anchor}/__darkmatter__/connection_relay_poll/{state.agent_id}",
                            params={"signature": sig, "timestamp": ts},
                        )
                        if resp2.status_code == 200:
                            data2 = resp2.json()
                            for cb_data in data2.get("callbacks", []):
                                if isinstance(cb_data, dict) and cb_data.get("agent_id"):
                                    process_connection_relay_callback(state, cb_data)
                                    print(f"[DarkMatter Entrypoint] Relay: connection accepted by {cb_data['agent_id'][:12]}...", file=sys.stderr)
                        break  # success — don't try other anchors
                except Exception:
                    continue
        except Exception:
            pass


if state.nat_detected:
    _relay_thread = threading.Thread(target=_relay_poll_loop, daemon=True)
    _relay_thread.start()


# ---------------------------------------------------------------------------
# Peer spawned agent count
# ---------------------------------------------------------------------------

def _get_peer_spawned_agents(conn):
    """GET <peer_url>/__darkmatter__/status and return spawned_agents count."""
    try:
        base = _resolve_base_url(conn)
        resp = requests.get(f"{base}/__darkmatter__/status", timeout=1)
        if resp.ok:
            return resp.json().get("spawned_agents", 0)
    except Exception:
        pass
    return 0




# ---------------------------------------------------------------------------
# Best available agent selection
# ---------------------------------------------------------------------------

_resolve_cache = {}  # {agent_id: (url, expiry_time)}
_RESOLVE_CACHE_TTL = 60  # seconds

def _resolve_base_url(conn):
    """Get the base URL for a connection, preferring localhost if the agent is local.

    The agent may advertise a public IP (via ipify) but from localhost we should
    use 127.0.0.1 to avoid routing through the internet or timing out.
    Results are cached for 60s to avoid repeated localhost probes.
    """
    import time as _time
    now = _time.monotonic()
    cached = _resolve_cache.get(conn.agent_id)
    if cached and cached[1] > now:
        return cached[0]

    agent_url = conn.agent_url.rstrip("/")
    for suffix in ("/mcp", "/__darkmatter__"):
        if agent_url.endswith(suffix):
            agent_url = agent_url[:-len(suffix)]
            break

    # Check if this agent was discovered locally (localhost or LAN) — use discovered URL
    with _discovery_lock:
        for agent in _discovered_agents.values():
            if agent["agent_id"] == conn.agent_id:
                result = agent["url"]  # already has correct host:port from probe
                # Also update the stored connection URL so it persists across restarts
                if conn.agent_url != result:
                    conn.agent_url = result
                    save_state()
                _resolve_cache[conn.agent_id] = (result, now + _RESOLVE_CACHE_TTL)
                return result

    _resolve_cache[conn.agent_id] = (agent_url, now + _RESOLVE_CACHE_TTL)
    return agent_url


def _pick_best_agent():
    """Pick the best available agent from connections."""
    candidates = [
        c for c in state.connections.values()
        if c.health_failures < 3
        and c.agent_id != state.agent_id
        and getattr(c, "health_status", "ok") != "unreachable"
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda c: (
        c.messages_declined,
        c.avg_response_time_ms if c.avg_response_time_ms > 0 else 999999,
        -(time.time() if not c.last_activity else 0),
    ))
    return candidates[0]


# ---------------------------------------------------------------------------
# Shared notification helper
# ---------------------------------------------------------------------------

def _notify_connection_accepted(conn, payload):
    """Notify a peer that we accepted their connection request, with anchor relay fallback."""
    base = conn.agent_url.rstrip("/")
    for suffix in ("/mcp", "/__darkmatter__"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break

    direct_ok = False
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(base + "/__darkmatter__/connection_accepted", json=payload)
            direct_ok = resp.status_code < 400
    except Exception:
        pass

    if not direct_ok and ANCHOR_NODES:
        for anchor in ANCHOR_NODES:
            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(
                        f"{anchor}/__darkmatter__/connection_relay/{conn.agent_id}",
                        json=payload,
                    )
                    if resp.status_code < 400:
                        print(f"[DarkMatter Entrypoint] Connection accept relayed via {anchor} for {conn.agent_id[:12]}...", file=sys.stderr)
                        break
            except Exception:
                continue


# ---------------------------------------------------------------------------
# Mesh protocol endpoints (Flask thin wrappers around darkmatter.network.mesh)
# ---------------------------------------------------------------------------

@app.route("/.well-known/darkmatter.json", methods=["GET"])
def well_known():
    public_url = _get_public_url()
    return jsonify({
        "darkmatter": True,
        "protocol_version": PROTOCOL_VERSION,
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "bio": state.bio,
        "status": state.status.value,
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "mesh_url": f"{public_url}/__darkmatter__",
        "mcp_url": None,
        "webrtc_enabled": False,
    })


@app.route("/__darkmatter__/status", methods=["GET"])
def dm_status():
    return jsonify({
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "bio": state.bio,
        "status": state.status.value,
        "version": DARKMATTER_VERSION,
        "num_connections": len(state.connections),
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
    })


@app.route("/__darkmatter__/network_info", methods=["GET"])
def dm_network_info():
    peers = [
        {"agent_id": c.agent_id, "agent_url": c.agent_url, "agent_bio": c.agent_bio}
        for c in state.connections.values()
    ]
    return jsonify({
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "agent_url": _get_public_url(),
        "bio": state.bio,
        "accepting_connections": len(state.connections) < MAX_CONNECTIONS,
        "wallets": state.wallets,
        "peers": peers,
    })


@app.route("/__darkmatter__/connection_request", methods=["POST"])
def dm_connection_request():
    data = request.get_json(silent=True) or {}
    result, status = asyncio.run(process_connection_request(state, data, _get_public_url()))
    return jsonify(result), status


@app.route("/__darkmatter__/connection_accepted", methods=["POST"])
def dm_connection_accepted():
    data = request.get_json(silent=True) or {}
    result, status = process_connection_accepted(state, data)
    return jsonify(result), status


@app.route("/__darkmatter__/accept_pending", methods=["POST"])
def dm_accept_pending():
    data = request.get_json(silent=True) or {}
    request_id = data.get("request_id", "")
    if not request_id:
        return jsonify({"error": "Missing request_id"}), 400

    result, status, notify_payload = process_accept_pending(state, request_id, _get_public_url())

    if status == 200 and notify_payload:
        agent_id = result.get("agent_id", "")
        conn = state.connections.get(agent_id)
        if conn:
            _notify_connection_accepted(conn, notify_payload)

        # Handle mutual connection requests
        if result.get("mutual") and conn:
            try:
                _sync_connection_request(conn.agent_url)
            except Exception as e:
                print(f"[DarkMatter Entrypoint] Mutual sync failed for {conn.agent_id[:12]}...: {e}", file=sys.stderr)

    return jsonify(result), status


@app.route("/__darkmatter__/message", methods=["POST"])
def dm_message():
    data = request.get_json(silent=True) or {}

    # Use shared validation/queuing logic from darkmatter.network.mesh.
    # router_mode is "queue_only" — messages queue for the human to read.
    loop = asyncio.new_event_loop()
    try:
        result, status = loop.run_until_complete(_process_incoming_message(state, data))
    finally:
        # Let any background tasks (webhook notify, antimatter interception) finish
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()

    return jsonify(result), status


@app.route("/__darkmatter__/webhook/<message_id>", methods=["POST"])
def dm_webhook_post(message_id):
    data = request.get_json(silent=True) or {}
    result, status = _process_webhook_locally(state, message_id, data)
    return jsonify(result), status


@app.route("/__darkmatter__/webhook/<message_id>", methods=["GET"])
def dm_webhook_get(message_id):
    sm = state.sent_messages.get(message_id)
    if not sm:
        return jsonify({"error": f"No sent message with ID '{message_id}'"}), 404
    return jsonify({
        "message_id": sm.message_id, "status": sm.status,
        "initial_hops": sm.initial_hops, "created_at": sm.created_at,
        "updates": sm.updates,
    })


@app.route("/__darkmatter__/peer_update", methods=["POST"])
def dm_peer_update():
    data = request.get_json(silent=True) or {}
    agent_id = data.get("agent_id", "")
    new_url = data.get("new_url", "")
    if not agent_id or not new_url:
        return jsonify({"error": "Missing agent_id or new_url"}), 400
    url_err = validate_url(new_url)
    if url_err:
        return jsonify({"error": url_err}), 400
    conn = state.connections.get(agent_id)
    if conn is None:
        return jsonify({"error": "Unknown agent"}), 404
    conn.agent_url = new_url
    # Update bio and display name if included in the peer_update payload
    new_bio = data.get("bio")
    if new_bio is not None and isinstance(new_bio, str):
        conn.agent_bio = new_bio[:MAX_BIO_LENGTH]
    new_display_name = data.get("display_name")
    if new_display_name is not None and isinstance(new_display_name, str):
        conn.agent_display_name = new_display_name[:100]
    save_state()
    return jsonify({"success": True, "updated": True})


@app.route("/__darkmatter__/peer_lookup/<agent_id>", methods=["GET"])
def dm_peer_lookup(agent_id):
    conn = state.connections.get(agent_id)
    if conn is None:
        return jsonify({"error": "Not connected to that agent"}), 404
    return jsonify({"agent_id": conn.agent_id, "url": conn.agent_url, "status": "connected"})


@app.route("/__darkmatter__/impression/<agent_id>", methods=["GET"])
def dm_impression(agent_id):
    impression = state.impressions.get(agent_id)
    if impression is None:
        return jsonify({"agent_id": agent_id, "has_impression": False})
    return jsonify({
        "agent_id": agent_id,
        "has_impression": True,
        "score": impression.score,
        "note": impression.note,
    })


@app.route("/__darkmatter__/gas_match", methods=["POST"])
def dm_gas_match():
    data = request.get_json(silent=True) or {}
    result, status = process_antimatter_match(data)
    return jsonify(result), status


@app.route("/__darkmatter__/gas_signal", methods=["POST"])
def dm_gas_signal():
    data = request.get_json(silent=True) or {}
    result, status = asyncio.run(process_antimatter_signal(state, data))
    return jsonify(result), status


@app.route("/__darkmatter__/gas_result", methods=["POST"])
def dm_gas_result():
    data = request.get_json(silent=True) or {}
    result, status = asyncio.run(process_antimatter_result(state, data))
    return jsonify(result), status


# ---------------------------------------------------------------------------
# Sync helpers for outbound mesh operations
# ---------------------------------------------------------------------------

def _sync_connection_request(target_url, mutual=False):
    """Send a connection request to a target agent (sync version)."""
    target_base = target_url.rstrip("/")
    for suffix in ("/mcp", "/__darkmatter__"):
        if target_base.endswith(suffix):
            target_base = target_base[:-len(suffix)]
            break

    # Use LAN URL when connecting to a LAN peer so they store a reachable address
    from darkmatter.network.manager import is_local_url
    if is_local_url(target_base):
        our_url = f"http://{_get_lan_ip()}:{PORT}"
    else:
        our_url = _get_public_url()
    payload = build_outbound_request_payload(state, our_url, mutual=mutual)

    with httpx.Client(timeout=30.0) as client:
        response = client.post(target_base + "/__darkmatter__/connection_request", json=payload)
        result = response.json()

        if result.get("auto_accepted"):
            conn = build_connection_from_accepted(result)
            state.connections[result["agent_id"]] = conn
            save_state()
            return {"success": True, "status": "connected", "agent_id": result["agent_id"]}

        state.pending_outbound[target_base] = result.get("agent_id", "")
        return {"success": True, "status": "pending", "request_id": result.get("request_id")}


def _sync_send_message(content, target_agent_id=None, metadata=None):
    """Send a message to an agent (sync version)."""
    message_id = f"msg-{uuid.uuid4().hex[:12]}"

    if target_agent_id:
        conn = state.connections.get(target_agent_id)
        if not conn:
            return {"success": False, "error": f"Not connected to agent '{target_agent_id}'."}
        targets = [conn]
    else:
        best = _pick_best_agent()
        if not best:
            return {"success": False, "error": "No agents connected."}
        targets = [best]

    webhook = _mgr.build_webhook_url(message_id, peer_url=targets[0].agent_url)

    msg_timestamp = datetime.now(timezone.utc).isoformat()
    signature_hex = None
    if state.private_key_hex:
        signature_hex = sign_message(
            state.private_key_hex, state.agent_id, message_id, msg_timestamp, content
        )

    # Pre-register sent message BEFORE delivery so webhook callbacks don't 404
    sent_msg = SentMessage(
        message_id=message_id, content=content, status="sending",
        initial_hops=10, routed_to=[c.agent_id for c in targets], metadata=metadata or {},
    )
    state.sent_messages[message_id] = sent_msg

    sent_to = []
    failed = []
    for conn in targets:
        try:
            base = _resolve_base_url(conn)
            payload = {
                "message_id": message_id, "content": content,
                "webhook": webhook, "hops_remaining": 10,
                "from_agent_id": state.agent_id, "metadata": metadata or {},
                "timestamp": msg_timestamp, "from_public_key_hex": state.public_key_hex,
                "signature_hex": signature_hex,
            }
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(base + "/__darkmatter__/message", json=payload)
                if resp.status_code >= 400:
                    conn._consecutive_failures = getattr(conn, "_consecutive_failures", 0) + 1
                    failed.append({"agent_id": conn.agent_id, "error": resp.text})
                else:
                    conn.messages_sent += 1
                    conn._consecutive_failures = 0
                    conn.last_activity = datetime.now(timezone.utc).isoformat()
                    sent_to.append(conn.agent_id)
        except Exception as e:
            conn.messages_declined += 1
            conn._consecutive_failures = getattr(conn, "_consecutive_failures", 0) + 1
            # Try anchor message relay as fallback
            relayed = False
            if ANCHOR_NODES:
                for anchor in ANCHOR_NODES:
                    try:
                        with httpx.Client(timeout=10.0) as relay_client:
                            resp = relay_client.post(
                                f"{anchor}/__darkmatter__/message_relay/{conn.agent_id}",
                                json=payload,
                            )
                            if resp.status_code < 400:
                                conn.messages_sent += 1
                                conn._consecutive_failures = 0
                                conn.last_activity = datetime.now(timezone.utc).isoformat()
                                sent_to.append(conn.agent_id)
                                relayed = True
                                print(f"[DarkMatter Entrypoint] Message relayed via {anchor} to {conn.agent_id[:12]}...", file=sys.stderr)
                                break
                    except Exception:
                        continue
            if not relayed:
                failed.append({"agent_id": conn.agent_id, "error": str(e)})

    # Update the pre-registered sent message with delivery results
    if sent_to:
        sent_msg.status = "active"
        sent_msg.routed_to = sent_to
        save_state()
    elif failed:
        sent_msg.status = "failed"
        sent_msg.routed_to = [f["agent_id"] for f in failed]
        for f in failed:
            sent_msg.updates.append({
                "type": "delivery_failed",
                "agent_id": f["agent_id"],
                "error": f["error"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        save_state()

    result = {"success": len(sent_to) > 0, "message_id": message_id, "routed_to": sent_to}
    if failed:
        result["failed"] = failed
        if not sent_to:
            result["error"] = failed[0]["error"] if len(failed) == 1 else f"{len(failed)} deliveries failed"
    return result


def _sync_send_payment_notification(agent_id, amount, token, tx_result):
    """Send a payment notification message with antimatter economy metadata."""
    gas_meta = {
        "type": "solana_payment",
        "amount": amount,
        "token": token,
        "tx_signature": tx_result.get("tx_signature", ""),
        "from_wallet": tx_result.get("from_wallet", ""),
        "to_wallet": tx_result.get("to_wallet", ""),
        "gas_eligible": True,
        "gas_rate": ANTIMATTER_RATE,
        "sender_created_at": state.created_at,
        "sender_superagent_wallet": state.wallets.get("solana", ""),
    }
    if token != "SOL" and token in SPL_TOKENS:
        gas_meta["decimals"] = SPL_TOKENS[token][1]
    _sync_send_message(
        f"Sent {amount} {token} — tx: {tx_result.get('tx_signature', 'unknown')}",
        target_agent_id=agent_id,
        metadata=gas_meta,
    )


def _sync_broadcast_message(content, metadata=None):
    """Send a message to all reachable connected agents (each gets its own message_id)."""
    conns = [
        c for c in state.connections.values()
        if c.agent_id != state.agent_id
        and getattr(c, "health_status", "ok") != "unreachable"
    ]
    if not conns:
        return {"success": False, "error": "No agents connected."}

    sent_ids = []
    failed = []
    for conn in conns:
        result = _sync_send_message(content, conn.agent_id, metadata=metadata)
        if result.get("success"):
            sent_ids.append(result["message_id"])
        else:
            failed.append({"agent_id": conn.agent_id, "error": result.get("error", "Unknown error")})

    return {
        "success": len(sent_ids) > 0,
        "broadcast": True,
        "sent_count": len(sent_ids),
        "message_ids": sent_ids,
        "failed_count": len(failed),
        "failed": failed if failed else None,
    }


def _sync_respond_to_message(message_id, response_text):
    """Respond to a queued inbox message (sync version)."""
    msg = None
    for i, m in enumerate(state.message_queue):
        if m.message_id == message_id:
            msg = state.message_queue.pop(i)
            break

    if msg is None:
        return {"success": False, "error": f"No queued message with ID '{message_id}'."}

    resp_timestamp = datetime.now(timezone.utc).isoformat()
    resp_signature_hex = None
    if state.private_key_hex:
        resp_signature_hex = sign_message(
            state.private_key_hex, state.agent_id, msg.message_id, resp_timestamp, response_text
        )

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(msg.webhook, json={
                "type": "response", "agent_id": state.agent_id,
                "response": response_text, "metadata": msg.metadata,
                "timestamp": resp_timestamp, "from_public_key_hex": state.public_key_hex,
                "signature_hex": resp_signature_hex,
            })
            webhook_success = resp.status_code < 400
            if not webhook_success:
                print(f"[DarkMatter Entrypoint] Webhook response failed (HTTP {resp.status_code}) for message {msg.message_id}", file=sys.stderr)
    except Exception as e:
        webhook_success = False
        print(f"[DarkMatter Entrypoint] Webhook response error for message {msg.message_id}: {e}", file=sys.stderr)

    if msg.from_agent_id and msg.from_agent_id in state.connections:
        conn = state.connections[msg.from_agent_id]
        conn.last_activity = datetime.now(timezone.utc).isoformat()

    save_state()
    return {"success": webhook_success, "message_id": msg.message_id}


# ---------------------------------------------------------------------------
# Web UI routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("chat.html",
                           state=state,
                           short_id=_short_id,
                           display_name_for=_display_name_for,
                           version=DARKMATTER_VERSION)


@app.route("/api/upload", methods=["POST"])
def upload_files():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"success": False, "error": "No files provided"}), 400
    results = []
    public_url = _get_public_url()
    for f in files:
        if not f.filename:
            continue
        file_id = uuid.uuid4().hex
        safe_name = secure_filename(f.filename) or "upload"
        file_dir = os.path.join(UPLOAD_DIR, file_id)
        os.makedirs(file_dir, exist_ok=True)
        file_path = os.path.join(file_dir, safe_name)
        f.save(file_path)
        size = os.path.getsize(file_path)
        results.append({
            "filename": safe_name,
            "url": f"{public_url}/api/file/{file_id}/{safe_name}",
            "content_type": f.content_type or "application/octet-stream",
            "size": size,
        })
    return jsonify({"success": True, "files": results})


@app.route("/api/file/<file_id>/<filename>")
def serve_file(file_id, filename):
    safe_name = secure_filename(filename)
    file_dir = os.path.join(UPLOAD_DIR, secure_filename(file_id))
    full_path = os.path.join(file_dir, safe_name)
    if not os.path.isfile(full_path):
        return jsonify({"error": "File not found"}), 404
    return send_from_directory(file_dir, safe_name)


@app.route("/send", methods=["POST"])
def send():
    if _is_ajax():
        data = request.get_json(silent=True) or {}
        content = data.get("content", "").strip()
        target = data.get("target", "auto")
        attachments = data.get("attachments", [])
    else:
        content = request.form.get("content", "").strip()
        target = request.form.get("target", "auto")
        attachments = []

    # If no text but attachments exist, use filenames as content
    if not content and attachments:
        content = ", ".join(a.get("filename", "file") for a in attachments)

    if not content:
        if _is_ajax():
            return jsonify({"success": False, "error": "Empty message"}), 400
        return redirect(url_for("index"))

    metadata = {}
    if attachments:
        metadata["attachments"] = attachments

    if target == "broadcast":
        if _is_ajax():
            result = _sync_broadcast_message(content, metadata=metadata if metadata else None)
            return jsonify(result)
        threading.Thread(target=_sync_broadcast_message, args=(content,), kwargs={"metadata": metadata if metadata else None}, daemon=True).start()
        return redirect(url_for("index"))

    if target == "fastest":
        # Pick the agent with the lowest measured ping latency
        candidates = [
            c for c in state.connections.values()
            if c.agent_id != state.agent_id
            and getattr(c, "health_status", "ok") != "unreachable"
            and getattr(c, "ping_latency_ms", -1) > 0
        ]
        if candidates:
            candidates.sort(key=lambda c: c.ping_latency_ms)
            target_id = candidates[0].agent_id
        else:
            # Fall back to auto if no latency data
            target_id = None
    else:
        target_id = None if target == "auto" else target
    if _is_ajax():
        result = _sync_send_message(content, target_id, metadata=metadata if metadata else None)
        return jsonify(result)
    threading.Thread(target=_sync_send_message, args=(content, target_id), kwargs={"metadata": metadata if metadata else None}, daemon=True).start()
    return redirect(url_for("index"))


@app.route("/retry/<message_id>", methods=["POST"])
def retry(message_id):
    """Re-send a failed or timed-out message."""
    old = state.sent_messages.get(message_id)
    if not old:
        return jsonify({"success": False, "error": "Message not found"}), 404
    if old.status not in ("failed", "timed_out"):
        return jsonify({"success": False, "error": f"Message status is '{old.status}', not retryable"}), 400

    content = old.content
    target_id = old.routed_to[0] if old.routed_to else None
    metadata = old.metadata

    # Remove the old failed message
    del state.sent_messages[message_id]
    save_state()

    result = _sync_send_message(content, target_id, metadata=metadata if metadata else None)
    return jsonify(result)


@app.route("/respond/<message_id>", methods=["POST"])
def respond(message_id):
    if _is_ajax():
        data = request.get_json(silent=True) or {}
        response_text = data.get("response", "").strip()
    else:
        response_text = request.form.get("response", "").strip()
    if not response_text:
        if _is_ajax():
            return jsonify({"success": False, "error": "Empty response"}), 400
        return redirect(url_for("index"))
    if _is_ajax():
        result = _sync_respond_to_message(message_id, response_text)
        return jsonify(result)
    threading.Thread(target=_sync_respond_to_message, args=(message_id, response_text), daemon=True).start()
    return redirect(url_for("index"))


@app.route("/connect", methods=["POST"])
def connect():
    if _is_ajax():
        data = request.get_json(silent=True) or {}
        target_url = data.get("url", "").strip()
    else:
        target_url = request.form.get("url", "").strip()
    if not target_url:
        if _is_ajax():
            return jsonify({"success": False, "error": "No URL provided"}), 400
        return redirect(url_for("index"))
    try:
        result = _sync_connection_request(target_url, mutual=True)
        if _is_ajax():
            return jsonify(result)
    except Exception as e:
        if _is_ajax():
            return jsonify({"success": False, "error": str(e)}), 500
    return redirect(url_for("index"))


@app.route("/accept/<request_id>", methods=["POST"])
def accept(request_id):
    pending = state.pending_requests.get(request_id)
    if not pending:
        if _is_ajax():
            return jsonify({"success": False, "error": "Request not found"}), 404
        return redirect(url_for("index"))

    display_name = pending.from_agent_display_name or _short_id(pending.from_agent_id)

    result, status, notify_payload = process_accept_pending(state, request_id, _get_public_url())

    if status == 200 and notify_payload:
        agent_id = result.get("agent_id", "")
        conn = state.connections.get(agent_id)
        if conn:
            _notify_connection_accepted(conn, notify_payload)

        # Handle mutual connection requests
        if result.get("mutual") and conn:
            try:
                _sync_connection_request(conn.agent_url)
            except Exception as e:
                print(f"[DarkMatter Entrypoint] Mutual sync failed for {conn.agent_id[:12]}...: {e}", file=sys.stderr)

    if _is_ajax():
        return jsonify({"success": status == 200, "display_name": display_name}), status
    return redirect(url_for("index"))


@app.route("/reject/<request_id>", methods=["POST"])
def reject(request_id):
    if request_id in state.pending_requests:
        del state.pending_requests[request_id]
    if _is_ajax():
        return jsonify({"success": True})
    return redirect(url_for("index"))


@app.route("/disconnect/<agent_id>", methods=["POST"])
def disconnect(agent_id):
    if agent_id in state.connections:
        del state.connections[agent_id]
        save_state()
    if _is_ajax():
        return jsonify({"success": True})
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Polling API
# ---------------------------------------------------------------------------

@app.route("/api/poll", methods=["GET"])
def poll():
    inbox = [{
        "message_id": msg.message_id,
        "from_agent_id": msg.from_agent_id,
        "from_display_name": _display_name_for(msg.from_agent_id),
        "content": msg.content,
        "received_at": msg.received_at,
        "metadata": getattr(msg, "metadata", {}),
    } for msg in state.message_queue]

    outbox = []
    for sm in sorted(state.sent_messages.values(), key=lambda s: s.created_at, reverse=True)[:50]:
        responses = [{
            "agent_id": r["agent_id"],
            "display_name": _display_name_for(r["agent_id"]),
            "response": r.get("response", ""),
            "timestamp": r.get("timestamp"),
        } for r in getattr(sm, "responses", [])]
        routed_to_names = [_display_name_for(rid) for rid in sm.routed_to]
        outbox.append({
            "message_id": sm.message_id, "status": sm.status, "content": sm.content,
            "routed_to": sm.routed_to, "routed_to_names": routed_to_names,
            "created_at": sm.created_at,
            "updates": getattr(sm, "updates", []),
            "responses": responses,
            "from_self": True,
            "metadata": getattr(sm, "metadata", {}),
        })

    pending = [{
        "request_id": req.request_id, "from_agent_id": req.from_agent_id,
        "from_display_name": req.from_agent_display_name or _short_id(req.from_agent_id),
        "from_bio": req.from_agent_bio, "requested_at": req.requested_at,
    } for req in state.pending_requests.values()]

    # Query peers for spawned agent counts in parallel
    peer_conns = [c for c in state.connections.values() if c.agent_id != state.agent_id]
    spawned_counts = {}
    if peer_conns:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_get_peer_spawned_agents, c): c.agent_id for c in peer_conns}
            for future in as_completed(futures):
                agent_id = futures[future]
                try:
                    spawned_counts[agent_id] = future.result()
                except Exception:
                    spawned_counts[agent_id] = 0

    connections = [{
        "agent_id": c.agent_id,
        "display_name": c.agent_display_name or _short_id(c.agent_id),
        "bio": c.agent_bio, "direction": getattr(getattr(c, "direction", None), "value", "unknown"),
        "last_activity": c.last_activity,
        "spawned_agents": spawned_counts.get(c.agent_id, 0),
        "health_status": getattr(c, "health_status", "ok"),
        "wallets": c.wallets,
        "connectivity_level": getattr(c, "connectivity_level", 0),
        "connectivity_method": getattr(c, "connectivity_method", ""),
        "is_local": _is_lan_url(_resolve_base_url(c)),
        "ping_latency_ms": getattr(c, "ping_latency_ms", -1),
    } for c in state.connections.values() if c.agent_id != state.agent_id]

    # Discovered agents (not yet connected)
    connected_ids = set(state.connections.keys())
    with _discovery_lock:
        discovered = [{
            "agent_id": a["agent_id"], "display_name": a["display_name"],
            "bio": a["bio"], "url": a["url"], "port": a["port"],
            "status": a["status"], "accepting": a["accepting"],
        } for a in _discovered_agents.values() if a["agent_id"] not in connected_ids]

    lan_ip = _get_lan_ip()
    lan_url = f"http://{lan_ip}:{PORT}" if lan_ip != "127.0.0.1" else None

    total_active_agents = sum(c.get("spawned_agents", 0) for c in connections)

    return jsonify({
        "self": {
            "agent_id": state.agent_id,
            "display_name": state.display_name or "Human",
            "lan_url": lan_url,
            "public_url": _get_public_url(),
            "nat_detected": getattr(state, "nat_detected", False),
            "connections_count": len(state.connections),
            "total_active_agents": total_active_agents,
            "wallets": state.wallets,
        },
        "inbox": inbox, "outbox": outbox, "pending_requests": pending,
        "connections": connections, "discovered": discovered,
        "shards": [{
            "shard_id": s.shard_id,
            "author_id": s.author_agent_id,
            "author_name": _display_name_for(s.author_agent_id),
            "content": s.content,
            "summary": s.summary,
            "tags": s.tags,
            "created_at": s.created_at,
        } for s in getattr(state, "shared_shards", [])],
    })


@app.route("/api/checkin", methods=["POST"])
def checkin():
    """Broadcast a check-in message to all agents and track response times."""
    result = _sync_broadcast_message("What are you working on right now? Give a brief status update.", metadata={"checkin": True})
    return jsonify(result)


@app.route("/api/broadcast-group", methods=["POST"])
def broadcast_group():
    """Send a message to a specific subset of connected agents."""
    data = request.get_json(silent=True) or {}
    agent_ids = data.get("agent_ids", [])
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"success": False, "error": "Empty message"}), 400
    if not agent_ids or not isinstance(agent_ids, list):
        return jsonify({"success": False, "error": "No agents specified"}), 400

    sent_ids = []
    failed = []
    for aid in agent_ids:
        if aid not in state.connections:
            failed.append({"agent_id": aid, "error": "Not connected"})
            continue
        result = _sync_send_message(content, aid)
        if result.get("success"):
            sent_ids.append(result["message_id"])
        else:
            failed.append({"agent_id": aid, "error": result.get("error", "Unknown")})

    return jsonify({
        "success": len(sent_ids) > 0,
        "sent_count": len(sent_ids),
        "message_ids": sent_ids,
        "failed_count": len(failed),
        "failed": failed if failed else None,
    })


@app.route("/api/info", methods=["GET"])
def info():
    """Static agent info for initial page load."""
    lan_ip = _get_lan_ip()
    lan_url = f"http://{lan_ip}:{PORT}" if lan_ip != "127.0.0.1" else None
    wallet_path = os.path.join(_entrypoint_data_dir, "wallet_passport.key")
    return jsonify({
        "agent_id": state.agent_id,
        "display_name": state.display_name or "Human",
        "lan_url": lan_url,
        "public_url": _get_public_url(),
        "nat_detected": getattr(state, "nat_detected", False),
        "port": PORT,
        "wallet_identity": os.path.exists(wallet_path),
        "wallets": state.wallets,
    })


@app.route("/api/scan", methods=["POST"])
def scan():
    """Force an immediate discovery scan."""
    _scan_local_agents()
    return jsonify({"success": True})


@app.route("/api/update-agents", methods=["POST"])
def update_agents():
    """Send pip upgrade command to all local connected agents."""
    results = []

    local_conns = [
        (aid, conn) for aid, conn in state.connections.items()
        if _is_lan_url(_resolve_base_url(conn))
    ]

    if not local_conns:
        return jsonify({"success": True, "results": [], "message": "No local agents found"})

    def _update_agent(aid, conn):
        base = _resolve_base_url(conn)
        url = f"{base}/__darkmatter__/admin_update"
        try:
            with httpx.Client(timeout=35.0) as client:
                resp = client.post(url, json={
                    "action": "pull_and_restart",
                    "from_agent_id": state.agent_id,
                })
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "agent_id": aid,
                        "display_name": conn.agent_display_name or aid[:12],
                        "success": data.get("success", False),
                        "git_output": data.get("git_output", ""),
                    }
                else:
                    return {
                        "agent_id": aid,
                        "display_name": conn.agent_display_name or aid[:12],
                        "success": False,
                        "git_output": f"HTTP {resp.status_code}",
                    }
        except Exception as e:
            return {
                "agent_id": aid,
                "display_name": conn.agent_display_name or aid[:12],
                "success": False,
                "git_output": str(e),
            }

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_update_agent, aid, conn): aid for aid, conn in local_conns}
        for future in as_completed(futures):
            results.append(future.result())

    succeeded = sum(1 for r in results if r["success"])
    return jsonify({
        "success": True,
        "results": results,
        "message": f"Updated {succeeded}/{len(results)} agents",
    })


@app.route("/api/mesh-topology", methods=["GET"])
def mesh_topology():
    """Return this node's star graph for client-side mesh crawl.

    Returns {self_id, nodes, edges, peer_urls} so the UI can fan out
    to each peer's /__darkmatter__/network_info for the full picture.
    """
    nodes = [{
        "id": state.agent_id,
        "display_name": state.display_name or _short_id(state.agent_id),
        "bio": state.bio,
        "is_self": True,
    }]
    edges = []
    peer_urls = {}

    for aid, conn in state.connections.items():
        nodes.append({
            "id": aid,
            "display_name": conn.agent_display_name or _short_id(aid),
            "bio": conn.agent_bio or "",
            "is_self": False,
            "connectivity_level": getattr(conn, "connectivity_level", 0),
            "connectivity_method": getattr(conn, "connectivity_method", "unknown"),
        })
        edges.append({
            "source": state.agent_id,
            "target": aid,
            "connectivity_level": getattr(conn, "connectivity_level", 0),
        })
        if conn.agent_url:
            peer_urls[aid] = conn.agent_url.rstrip("/")

    return jsonify({
        "self_id": state.agent_id,
        "nodes": nodes,
        "edges": edges,
        "peer_urls": peer_urls,
    })


async def _fetch_all_balances(wallets):
    """Fetch SOL + all known SPL token balances in a single event loop."""
    sol_result = await get_solana_balance(wallets)
    tokens = {}
    for name, (mint, _decimals) in SPL_TOKENS.items():
        try:
            tokens[name] = await get_solana_balance(wallets, mint=mint)
        except Exception as e:
            tokens[name] = {"success": False, "error": str(e)}
    return sol_result, tokens


@app.route("/api/wallet-balances", methods=["GET"])
def wallet_balances_api():
    """Get SOL balance + known SPL token balances."""
    if not state.wallets.get("solana"):
        return jsonify({"success": False, "error": "Solana wallet not available"})

    try:
        sol_result, tokens = asyncio.run(_fetch_all_balances(state.wallets))

        return jsonify({
            "success": True,
            "wallet_address": state.wallets.get("solana"),
            "sol": sol_result,
            "tokens": tokens,
            "known_tokens": list(SPL_TOKENS.keys()),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/wallet-send", methods=["POST"])
def wallet_send_api():
    """Send SOL or SPL token to a connected agent."""
    data = request.get_json(silent=True) or {}
    agent_id = data.get("agent_id")
    amount = data.get("amount")
    token = data.get("token", "SOL").upper()

    if not agent_id or not amount:
        return jsonify({"success": False, "error": "Missing agent_id or amount"}), 400

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid amount"}), 400

    if amount <= 0:
        return jsonify({"success": False, "error": "Amount must be positive"}), 400

    if not state.wallets.get("solana"):
        return jsonify({"success": False, "error": "Solana wallet not available"})

    conn = state.connections.get(agent_id)
    if not conn:
        return jsonify({"success": False, "error": f"Not connected to agent '{agent_id}'"}), 400

    conn_sol = conn.wallets.get("solana")
    if not conn_sol:
        return jsonify({"success": False, "error": f"Agent has no Solana wallet"}), 400

    try:
        if token == "SOL":
            result = asyncio.run(send_solana_sol(
                state.private_key_hex, state.wallets, conn_sol, amount
            ))
        elif token in SPL_TOKENS:
            mint, decimals = SPL_TOKENS[token]
            result = asyncio.run(send_solana_token(
                state.private_key_hex, state.wallets, conn_sol, mint, amount, decimals
            ))
        else:
            return jsonify({"success": False, "error": f"Unknown token '{token}'. Known: SOL, {', '.join(SPL_TOKENS.keys())}"}), 400

        if result.get("success"):
            result["to_agent_id"] = agent_id
            # Send payment notification with antimatter metadata
            try:
                _sync_send_payment_notification(agent_id, amount, token, result)
                result["notification_sent"] = True
            except Exception:
                result["notification_sent"] = False
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# Wallet-derived identity (Solana wallet signing)
# ---------------------------------------------------------------------------

_wallet_sessions = {}  # session_id -> {created, status, signature_hex, pubkey_hex, remember}
_WALLET_SESSION_TTL = 300  # 5 minutes
_WALLET_CHALLENGE = "DarkMatter Identity Derivation v1"


def _swap_identity(new_private_key):
    """Hot-swap the running identity to a new Ed25519 keypair."""
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption

    new_priv = new_private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    new_pub = new_private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    state.agent_id = new_pub
    state.private_key_hex = new_priv
    state.public_key_hex = new_pub
    save_state()


def _apply_wallet_signature(signature_hex, pubkey_hex, remember=False):
    """Verify a Solana wallet signature and derive+swap DarkMatter identity.

    Returns (ok, agent_id_or_error).
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    # Verify Ed25519 signature
    try:
        sig_bytes = bytes.fromhex(signature_hex)
        pub_bytes = bytes.fromhex(pubkey_hex)
        wallet_pubkey = Ed25519PublicKey.from_public_bytes(pub_bytes)
        wallet_pubkey.verify(sig_bytes, _WALLET_CHALLENGE.encode("utf-8"))
    except Exception as e:
        return False, f"Signature verification failed: {e}"

    # Derive Ed25519 seed from signature (deterministic — same wallet = same identity)
    seed = hashlib.sha256(sig_bytes).digest()

    # Create DarkMatter Ed25519 keypair from seed
    private_key = Ed25519PrivateKey.from_private_bytes(seed)

    # Optionally cache to disk
    wallet_path = os.path.join(_entrypoint_data_dir, "wallet_passport.key")
    if remember:
        with open(wallet_path, "wb") as f:
            f.write(seed)
        os.chmod(wallet_path, 0o600)
    elif os.path.exists(wallet_path):
        os.remove(wallet_path)

    _swap_identity(private_key)

    new_agent_id = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return True, new_agent_id


def _cleanup_wallet_sessions():
    """Remove expired wallet sessions."""
    now = time.time()
    expired = [sid for sid, s in _wallet_sessions.items()
               if now - s["created"] > _WALLET_SESSION_TTL]
    for sid in expired:
        del _wallet_sessions[sid]


@app.route("/api/wallet-auth", methods=["POST"])
def wallet_auth():
    """Derive Ed25519 identity from a Solana wallet signature (browser extension flow)."""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"ok": False, "error": "No JSON body"}), 400

    signature_hex = data.get("signature", "")
    pubkey_hex = data.get("public_key", "")
    challenge = data.get("challenge", "")
    remember = data.get("remember", False)

    if not signature_hex or not pubkey_hex or not challenge:
        return jsonify({"ok": False, "error": "Missing fields"}), 400
    if challenge != _WALLET_CHALLENGE:
        return jsonify({"ok": False, "error": "Invalid challenge string"}), 400

    ok, result = _apply_wallet_signature(signature_hex, pubkey_hex, remember)
    if not ok:
        return jsonify({"ok": False, "error": result}), 400
    return jsonify({"ok": True, "agent_id": result, "remembered": remember})


@app.route("/api/wallet-session", methods=["POST"])
def wallet_session_create():
    """Create a signing session for mobile QR flow. Returns session_id + QR URL."""
    _cleanup_wallet_sessions()

    session_id = uuid.uuid4().hex[:12]
    remember = request.get_json(force=True, silent=True) or {}

    _wallet_sessions[session_id] = {
        "created": time.time(),
        "status": "pending",  # pending -> signed -> applied
        "signature_hex": None,
        "pubkey_hex": None,
        "agent_id": None,
        "remember": remember.get("remember", False),
    }

    # Build URL reachable from mobile — prefer LAN, fall back to public
    lan_ip = _get_lan_ip()
    if lan_ip != "127.0.0.1":
        base = f"http://{lan_ip}:{PORT}"
    else:
        base = _get_public_url()

    sign_url = f"{base}/wallet-sign?s={session_id}"
    return jsonify({"ok": True, "session_id": session_id, "url": sign_url})


@app.route("/api/wallet-session/<session_id>", methods=["GET"])
def wallet_session_poll(session_id):
    """Desktop polls this to check if the mobile has signed."""
    session = _wallet_sessions.get(session_id)
    if not session:
        return jsonify({"ok": False, "error": "Session not found"}), 404
    if time.time() - session["created"] > _WALLET_SESSION_TTL:
        del _wallet_sessions[session_id]
        return jsonify({"ok": False, "error": "Session expired"}), 410

    if session["status"] == "signed":
        # Apply the signature now
        ok, result = _apply_wallet_signature(
            session["signature_hex"], session["pubkey_hex"], session["remember"]
        )
        if ok:
            session["status"] = "applied"
            session["agent_id"] = result
            return jsonify({"ok": True, "status": "applied", "agent_id": result})
        else:
            session["status"] = "failed"
            return jsonify({"ok": False, "status": "failed", "error": result})

    return jsonify({"ok": True, "status": session["status"], "agent_id": session.get("agent_id")})


@app.route("/api/wallet-session/<session_id>/complete", methods=["POST"])
def wallet_session_complete(session_id):
    """Mobile posts the signature here after signing."""
    session = _wallet_sessions.get(session_id)
    if not session:
        return jsonify({"ok": False, "error": "Session not found"}), 404
    if session["status"] != "pending":
        return jsonify({"ok": False, "error": "Session already used"}), 400

    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"ok": False, "error": "No JSON body"}), 400

    session["signature_hex"] = data.get("signature", "")
    session["pubkey_hex"] = data.get("public_key", "")
    session["status"] = "signed"
    return jsonify({"ok": True})


@app.route("/wallet-sign")
def wallet_sign_page():
    """Minimal signing page opened on mobile via QR code."""
    session_id = request.args.get("s", "")
    session = _wallet_sessions.get(session_id)
    if not session or session["status"] != "pending":
        return "<h2 style='font-family:monospace;color:#888;text-align:center;margin-top:40vh'>Session expired or invalid.</h2>", 404
    return render_template("wallet_sign.html", session_id=session_id, challenge=_WALLET_CHALLENGE)


@app.route("/api/wallet-disconnect", methods=["POST"])
def wallet_disconnect():
    """Revert to the original passport identity and remove cached wallet passport."""
    wallet_path = os.path.join(_entrypoint_data_dir, "wallet_passport.key")
    if os.path.exists(wallet_path):
        os.remove(wallet_path)

    # Reload original passport
    _saved_cwd = os.getcwd()
    os.chdir(_entrypoint_data_dir)
    priv, pub = load_or_create_passport()
    os.chdir(_saved_cwd)

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    pk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(priv))
    _swap_identity(pk)

    return jsonify({"ok": True, "agent_id": pub})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    url = f"http://localhost:{PORT}"
    print(f"[DarkMatter Entrypoint] Human node on {url}", file=sys.stderr)
    print(f"[DarkMatter Entrypoint] Public URL: {_get_public_url()}", file=sys.stderr)
    print(f"[DarkMatter Entrypoint] Agent ID: {_short_id(state.agent_id)}", file=sys.stderr)
    print(f"[DarkMatter Entrypoint] Display name: {state.display_name}", file=sys.stderr)
    print(f"[DarkMatter Entrypoint] Connections: {len(state.connections)}", file=sys.stderr)

    # Open the entrypoint in the default browser (skip on Werkzeug reloader child)
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        import webbrowser
        webbrowser.open(url)

    # Disable reloader when spawned as a subprocess (no TTY) to avoid
    # inheriting stale file descriptors from the parent process.
    is_tty = sys.stderr.isatty()
    app.run(host="0.0.0.0", port=PORT, debug=True, use_reloader=is_tty)

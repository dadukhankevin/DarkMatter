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
import json
import os
import sys
import time
import uuid
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from flask import Flask, request, jsonify, render_template, redirect, url_for


def _is_ajax():
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'

# ---------------------------------------------------------------------------
# Import DarkMatter internals from server.py
# ---------------------------------------------------------------------------

_darkmatter_dir = os.path.join(os.path.expanduser("~"), ".darkmatter")
if _darkmatter_dir not in sys.path:
    sys.path.insert(0, _darkmatter_dir)

import server

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("DARKMATTER_ENTRYPOINT_PORT", "8200"))
SCAN_PORTS = list(range(8100, 8111)) + [PORT]  # scan 8100-8110 + our own port range

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

server._init_state(PORT)
os.chdir(_original_cwd)
server._agent_state.router_mode = "queue_only"
server.AGENT_SPAWN_ENABLED = False

# Discover public URL using the same logic as the real server
# (env var > UPnP port mapping > ipify public IP > localhost fallback)
_public_url = asyncio.run(server._discover_public_url(PORT))
server._agent_state.public_url = _public_url

# Detect NAT — if behind CGNAT, use anchor relay for webhooks
server._agent_state.nat_detected = server._check_nat_status_sync(_public_url)
if server._agent_state.nat_detected:
    print(f"[DarkMatter Entrypoint] NAT detected: True — using anchor webhook relay", file=sys.stderr)

# Register with anchor nodes (required for relay poll signature verification)
if server.ANCHOR_NODES and _public_url:
    _reg_state = server._agent_state
    _reg_ts = datetime.now(timezone.utc).isoformat()
    _reg_payload = {
        "agent_id": _reg_state.agent_id,
        "new_url": _public_url,
        "public_key_hex": _reg_state.public_key_hex,
        "timestamp": _reg_ts,
    }
    if _reg_state.private_key_hex and _reg_state.public_key_hex:
        _reg_payload["signature"] = server._sign_peer_update(
            _reg_state.private_key_hex, _reg_state.agent_id, _public_url, _reg_ts
        )
    for _anchor in server.ANCHOR_NODES:
        try:
            with httpx.Client(timeout=5.0) as _c:
                _c.post(f"{_anchor}/__darkmatter__/peer_update", json=_reg_payload)
            print(f"[DarkMatter Entrypoint] Registered with anchor: {_anchor}", file=sys.stderr)
        except Exception as _e:
            print(f"[DarkMatter Entrypoint] Anchor registration failed ({_anchor}): {_e}", file=sys.stderr)

# Remove self-connection if present (shared passport state file)
if state := server._agent_state:
    self_id = state.agent_id
    if self_id in state.connections:
        del state.connections[self_id]

server.save_state()

state = server._agent_state

# Clean up UPnP mapping on exit
atexit.register(server._cleanup_upnp)

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "entrypoint"))


def _get_public_url():
    return state.public_url or f"http://localhost:{PORT}"


def _short_id(agent_id):
    if not agent_id:
        return "unknown"
    if len(agent_id) > 16:
        return agent_id[:8] + "..." + agent_id[-4:]
    return agent_id


def _display_name_for(agent_id):
    """Get display name for an agent from connections or discovered peers."""
    conn = state.connections.get(agent_id)
    if conn and conn.agent_display_name:
        return conn.agent_display_name
    return _short_id(agent_id)


# ---------------------------------------------------------------------------
# Discovery — scan local ports for DarkMatter agents
# ---------------------------------------------------------------------------

_discovered_agents = {}  # agent_id -> {url, display_name, bio, status, accepting, port}
_discovery_lock = threading.Lock()


def _probe_port(port):
    """Probe a single localhost port for a DarkMatter node."""
    if port == PORT:
        return None
    try:
        with httpx.Client(timeout=httpx.Timeout(0.5, connect=0.25)) as client:
            resp = client.get(f"http://127.0.0.1:{port}/.well-known/darkmatter.json")
            if resp.status_code != 200:
                return None
            info = resp.json()
            peer_id = info.get("agent_id", "")
            if not peer_id or peer_id == state.agent_id:
                return None
            return {
                "agent_id": peer_id,
                "url": f"http://localhost:{port}",
                "display_name": info.get("display_name") or _short_id(peer_id),
                "bio": info.get("bio", ""),
                "status": info.get("status", "active"),
                "accepting": info.get("accepting_connections", True),
                "port": port,
            }
    except Exception:
        return None


def _scan_local_agents():
    """Scan all local ports for DarkMatter agents."""
    results = {}
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(_probe_port, p): p for p in SCAN_PORTS}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    results[result["agent_id"]] = result
            except Exception:
                pass

    with _discovery_lock:
        _discovered_agents.clear()
        _discovered_agents.update(results)

    return results


def _start_discovery_loop():
    """Background thread that scans for agents every 15 seconds."""
    while True:
        try:
            _scan_local_agents()
        except Exception:
            pass
        time.sleep(15)


# Start discovery in background
_discovery_thread = threading.Thread(target=_start_discovery_loop, daemon=True)
_discovery_thread.start()


# ---------------------------------------------------------------------------
# Webhook relay polling (for NAT-ed nodes)
# ---------------------------------------------------------------------------

def _relay_poll_loop():
    """Background thread: poll anchor for buffered webhook and connection callbacks."""
    while True:
        try:
            time.sleep(5)
            if not state.nat_detected or not server.ANCHOR_NODES or not state.private_key_hex:
                continue

            anchor = server.ANCHOR_NODES[0]
            ts = datetime.now(timezone.utc).isoformat()
            sig = server._sign_relay_poll(state.private_key_hex, state.agent_id, ts)

            with httpx.Client(timeout=10.0) as client:
                # Poll for webhook callbacks
                resp = client.get(
                    f"{anchor}/__darkmatter__/webhook_relay_poll/{state.agent_id}",
                    params={"signature": sig, "timestamp": ts},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for cb in data.get("callbacks", []):
                        msg_id = cb.get("message_id", "")
                        cb_data = cb.get("data", {})
                        if msg_id and cb_data:
                            result, _ = server._process_webhook_locally(state, msg_id, cb_data)
                            if result.get("success"):
                                print(f"[DarkMatter Entrypoint] Relay: processed webhook for {msg_id}", file=sys.stderr)

                # Poll for connection relay callbacks
                resp2 = client.get(
                    f"{anchor}/__darkmatter__/connection_relay_poll/{state.agent_id}",
                    params={"signature": sig, "timestamp": ts},
                )
                if resp2.status_code == 200:
                    data2 = resp2.json()
                    for cb_data in data2.get("callbacks", []):
                        if isinstance(cb_data, dict) and cb_data.get("agent_id"):
                            server._process_connection_relay_callback(state, cb_data)
                            print(f"[DarkMatter Entrypoint] Relay: connection accepted by {cb_data['agent_id'][:12]}...", file=sys.stderr)
        except Exception:
            pass


if state.nat_detected:
    _relay_thread = threading.Thread(target=_relay_poll_loop, daemon=True)
    _relay_thread.start()


# ---------------------------------------------------------------------------
# Best available agent selection
# ---------------------------------------------------------------------------

def _resolve_base_url(conn):
    """Get the base URL for a connection, preferring localhost if the agent is local.

    The agent may advertise a public IP (via ipify) but from localhost we should
    use 127.0.0.1 to avoid routing through the internet or timing out.
    """
    agent_url = conn.agent_url.rstrip("/")
    for suffix in ("/mcp", "/__darkmatter__"):
        if agent_url.endswith(suffix):
            agent_url = agent_url[:-len(suffix)]
            break

    # Check if this agent was discovered locally — use local URL instead
    with _discovery_lock:
        for agent in _discovered_agents.values():
            if agent["agent_id"] == conn.agent_id:
                return f"http://localhost:{agent['port']}"

    # Try to extract port and use localhost if the URL has a port
    from urllib.parse import urlparse
    parsed = urlparse(agent_url)
    if parsed.port:
        # Try localhost first with a quick probe
        try:
            with httpx.Client(timeout=httpx.Timeout(0.5, connect=0.25)) as client:
                resp = client.get(f"http://127.0.0.1:{parsed.port}/.well-known/darkmatter.json")
                if resp.status_code == 200:
                    info = resp.json()
                    if info.get("agent_id") == conn.agent_id:
                        return f"http://localhost:{parsed.port}"
        except Exception:
            pass

    return agent_url


def _pick_best_agent():
    """Pick the best available agent from connections."""
    candidates = [
        c for c in state.connections.values()
        if c.health_failures < 3 and c.agent_id != state.agent_id
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
# Mesh protocol endpoints (Flask versions of server.py's Starlette handlers)
# ---------------------------------------------------------------------------

@app.route("/.well-known/darkmatter.json", methods=["GET"])
def well_known():
    public_url = _get_public_url()
    return jsonify({
        "darkmatter": True,
        "protocol_version": server.PROTOCOL_VERSION,
        "agent_id": state.agent_id,
        "display_name": state.display_name,
        "public_key_hex": state.public_key_hex,
        "bio": state.bio,
        "status": state.status.value,
        "accepting_connections": len(state.connections) < server.MAX_CONNECTIONS,
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
        "num_connections": len(state.connections),
        "accepting_connections": len(state.connections) < server.MAX_CONNECTIONS,
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
        "accepting_connections": len(state.connections) < server.MAX_CONNECTIONS,
        "peers": peers,
    })


@app.route("/__darkmatter__/connection_request", methods=["POST"])
def dm_connection_request():
    if state.status == server.AgentStatus.INACTIVE:
        return jsonify({"error": "Agent is currently inactive"}), 503

    data = request.get_json(silent=True) or {}
    from_agent_id = data.get("from_agent_id", "")
    from_agent_url = data.get("from_agent_url", "")
    from_agent_bio = data.get("from_agent_bio", "")
    from_agent_public_key_hex = data.get("from_agent_public_key_hex")
    from_agent_display_name = data.get("from_agent_display_name")
    mutual = data.get("mutual", False)

    if not from_agent_id or not from_agent_url:
        return jsonify({"error": "Missing required fields"}), 400
    if from_agent_id == state.agent_id:
        return jsonify({"error": "Cannot connect to self"}), 400
    url_err = server.validate_url(from_agent_url)
    if url_err:
        return jsonify({"error": url_err}), 400

    if from_agent_id in state.connections:
        existing = state.connections[from_agent_id]
        if from_agent_public_key_hex and not existing.agent_public_key_hex:
            existing.agent_public_key_hex = from_agent_public_key_hex
            existing.agent_display_name = from_agent_display_name
            server.save_state()
        return jsonify({
            "auto_accepted": True,
            "agent_id": state.agent_id,
            "agent_url": _get_public_url(),
            "agent_bio": state.bio,
            "agent_public_key_hex": state.public_key_hex,
            "agent_display_name": state.display_name,
            "message": "Already connected.",
        })

    if len(state.pending_requests) >= server.MESSAGE_QUEUE_MAX:
        return jsonify({"error": "Too many pending requests"}), 429

    request_id = f"req-{uuid.uuid4().hex[:8]}"
    state.pending_requests[request_id] = server.PendingConnectionRequest(
        request_id=request_id,
        from_agent_id=from_agent_id,
        from_agent_url=from_agent_url,
        from_agent_bio=from_agent_bio[:server.MAX_BIO_LENGTH],
        from_agent_public_key_hex=from_agent_public_key_hex,
        from_agent_display_name=from_agent_display_name,
        mutual=mutual,
    )

    return jsonify({
        "auto_accepted": False,
        "request_id": request_id,
        "message": "Connection request queued. Awaiting human decision.",
    })


@app.route("/__darkmatter__/connection_accepted", methods=["POST"])
def dm_connection_accepted():
    data = request.get_json(silent=True) or {}
    agent_id = data.get("agent_id", "")
    agent_url = data.get("agent_url", "")
    agent_bio = data.get("agent_bio", "")
    agent_public_key_hex = data.get("agent_public_key_hex")
    agent_display_name = data.get("agent_display_name")

    if not agent_id or not agent_url:
        return jsonify({"error": "Missing required fields"}), 400

    # Match by URL first, then fall back to agent_id (URLs can differ between
    # public IP and localhost, so agent_id is the reliable identifier)
    agent_base = agent_url.rstrip("/").rsplit("/mcp", 1)[0].rstrip("/")
    matched = None
    for pending_url in state.pending_outbound:
        pending_base = pending_url.rsplit("/mcp", 1)[0].rstrip("/")
        if pending_base == agent_base:
            matched = pending_url
            break

    if matched is None and agent_id:
        for pending_url, pending_agent_id in state.pending_outbound.items():
            if pending_agent_id == agent_id:
                matched = pending_url
                break

    if matched is None:
        return jsonify({"error": "No pending outbound connection request for this agent."}), 403

    del state.pending_outbound[matched]
    conn = server.Connection(
        agent_id=agent_id,
        agent_url=agent_url,
        agent_bio=agent_bio[:server.MAX_BIO_LENGTH],
        direction=server.ConnectionDirection.OUTBOUND,
        agent_public_key_hex=agent_public_key_hex,
        agent_display_name=agent_display_name,
    )
    state.connections[agent_id] = conn
    server.save_state()
    return jsonify({"success": True})


@app.route("/__darkmatter__/accept_pending", methods=["POST"])
def dm_accept_pending():
    data = request.get_json(silent=True) or {}
    request_id = data.get("request_id", "")
    if not request_id:
        return jsonify({"error": "Missing request_id"}), 400

    pending = state.pending_requests.get(request_id)
    if not pending:
        return jsonify({"error": f"No pending request with ID '{request_id}'"}), 404

    if len(state.connections) >= server.MAX_CONNECTIONS:
        return jsonify({"error": f"Connection limit reached ({server.MAX_CONNECTIONS})"}), 429

    conn = server.Connection(
        agent_id=pending.from_agent_id,
        agent_url=pending.from_agent_url,
        agent_bio=pending.from_agent_bio,
        direction=server.ConnectionDirection.INBOUND,
        agent_public_key_hex=pending.from_agent_public_key_hex,
        agent_display_name=pending.from_agent_display_name,
    )
    state.connections[pending.from_agent_id] = conn

    payload = {
        "agent_id": state.agent_id,
        "agent_url": _get_public_url(),
        "agent_bio": state.bio,
        "agent_public_key_hex": state.public_key_hex,
        "agent_display_name": state.display_name,
    }

    base = pending.from_agent_url.rstrip("/")
    for suffix in ("/mcp", "/__darkmatter__"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break

    # Try direct POST first, fall back to anchor relay
    direct_ok = False
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(base + "/__darkmatter__/connection_accepted", json=payload)
            direct_ok = resp.status_code < 400
    except Exception:
        pass

    if not direct_ok and server.ANCHOR_NODES:
        try:
            anchor = server.ANCHOR_NODES[0]
            with httpx.Client(timeout=10.0) as client:
                client.post(
                    f"{anchor}/__darkmatter__/connection_relay/{pending.from_agent_id}",
                    json=payload,
                )
            print(f"[DarkMatter Entrypoint] Connection accept relayed via anchor for {pending.from_agent_id[:12]}...", file=sys.stderr)
        except Exception:
            pass

    if pending.mutual:
        try:
            _sync_connection_request(pending.from_agent_url)
        except Exception:
            pass

    del state.pending_requests[request_id]
    server.save_state()

    return jsonify({
        "success": True,
        "accepted": True,
        "agent_id": pending.from_agent_id,
    })


@app.route("/__darkmatter__/message", methods=["POST"])
def dm_message():
    data = request.get_json(silent=True) or {}

    if state.status == server.AgentStatus.INACTIVE:
        return jsonify({"error": "Agent is currently inactive"}), 503

    if len(state.message_queue) >= server.MESSAGE_QUEUE_MAX:
        return jsonify({"error": "Message queue full"}), 429

    message_id = data.get("message_id", "")
    content = data.get("content", "")
    webhook = data.get("webhook", "")
    from_agent_id = data.get("from_agent_id")

    if not message_id or not content or not webhook:
        return jsonify({"error": "Missing required fields"}), 400
    if len(content) > server.MAX_CONTENT_LENGTH:
        return jsonify({"error": f"Content exceeds {server.MAX_CONTENT_LENGTH} bytes"}), 413

    if not from_agent_id:
        return jsonify({"error": "Missing from_agent_id"}), 400

    hops_remaining = data.get("hops_remaining", 10)
    if not isinstance(hops_remaining, int) or hops_remaining < 0:
        hops_remaining = 10

    msg_timestamp = data.get("timestamp", "")
    from_public_key_hex = data.get("from_public_key_hex")
    signature_hex = data.get("signature_hex")
    verified = False
    is_connected = from_agent_id in state.connections

    if is_connected:
        conn = state.connections[from_agent_id]
        if conn.agent_public_key_hex:
            if from_public_key_hex and conn.agent_public_key_hex != from_public_key_hex:
                return jsonify({"error": "Public key mismatch"}), 403
            if not signature_hex or not msg_timestamp:
                return jsonify({"error": "Signature required"}), 403
            if not server._verify_message(conn.agent_public_key_hex, signature_hex,
                                          from_agent_id, message_id, msg_timestamp, content):
                return jsonify({"error": "Invalid signature"}), 403
            verified = True
        elif from_public_key_hex and signature_hex and msg_timestamp:
            if not server._verify_message(from_public_key_hex, signature_hex,
                                          from_agent_id, message_id, msg_timestamp, content):
                return jsonify({"error": "Invalid signature"}), 403
            conn.agent_public_key_hex = from_public_key_hex
            verified = True
    elif from_public_key_hex and signature_hex and msg_timestamp:
        # Not connected, but accept if signature is valid (allows responses without bidirectional connection)
        if not server._verify_message(from_public_key_hex, signature_hex,
                                      from_agent_id, message_id, msg_timestamp, content):
            return jsonify({"error": "Invalid signature"}), 403
        verified = True

    msg = server.QueuedMessage(
        message_id=server.truncate_field(message_id, 128),
        content=content,
        webhook=webhook,
        hops_remaining=hops_remaining,
        metadata=data.get("metadata", {}),
        from_agent_id=from_agent_id,
        verified=verified,
    )
    state.message_queue.append(msg)
    state.messages_handled += 1

    if msg.from_agent_id and msg.from_agent_id in state.connections:
        conn = state.connections[msg.from_agent_id]
        conn.messages_received += 1
        conn.last_activity = datetime.now(timezone.utc).isoformat()

    server.save_state()

    return jsonify({"success": True, "queued": True, "queue_position": len(state.message_queue)})


@app.route("/__darkmatter__/webhook/<message_id>", methods=["POST"])
def dm_webhook_post(message_id):
    sm = state.sent_messages.get(message_id)
    if not sm:
        return jsonify({"error": f"No sent message with ID '{message_id}'"}), 404

    data = request.get_json(silent=True) or {}
    update_type = data.get("type", "")
    agent_id = data.get("agent_id", "unknown")
    timestamp = datetime.now(timezone.utc).isoformat()

    if update_type == "forwarded":
        sm.updates.append({
            "type": "forwarded", "agent_id": agent_id,
            "target_agent_id": data.get("target_agent_id", ""),
            "note": data.get("note"), "timestamp": timestamp,
        })
        server.save_state()
        return jsonify({"success": True, "recorded": "forwarded"})
    elif update_type == "response":
        sm.response = {
            "agent_id": agent_id, "response": data.get("response", ""),
            "metadata": data.get("metadata", {}), "timestamp": timestamp,
        }
        sm.status = "responded"
        server.save_state()
        return jsonify({"success": True, "recorded": "response"})
    elif update_type == "expired":
        sm.updates.append({
            "type": "expired", "agent_id": agent_id,
            "note": data.get("note"), "timestamp": timestamp,
        })
        server.save_state()
        return jsonify({"success": True, "recorded": "expired"})

    return jsonify({"error": f"Unknown update type: '{update_type}'"}), 400


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
    url_err = server.validate_url(new_url)
    if url_err:
        return jsonify({"error": url_err}), 400
    conn = state.connections.get(agent_id)
    if conn is None:
        return jsonify({"error": "Unknown agent"}), 404
    conn.agent_url = new_url
    server.save_state()
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
    return jsonify({"agent_id": agent_id, "has_impression": True, "impression": impression})


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

    payload = {
        "from_agent_id": state.agent_id,
        "from_agent_url": _get_public_url(),
        "from_agent_bio": state.bio,
        "from_agent_public_key_hex": state.public_key_hex,
        "from_agent_display_name": state.display_name,
    }
    if mutual:
        payload["mutual"] = True

    with httpx.Client(timeout=30.0) as client:
        response = client.post(target_base + "/__darkmatter__/connection_request", json=payload)
        result = response.json()

        if result.get("auto_accepted"):
            conn = server.Connection(
                agent_id=result["agent_id"],
                agent_url=result["agent_url"],
                agent_bio=result.get("agent_bio", ""),
                direction=server.ConnectionDirection.OUTBOUND,
                agent_public_key_hex=result.get("agent_public_key_hex"),
                agent_display_name=result.get("agent_display_name"),
            )
            state.connections[result["agent_id"]] = conn
            server.save_state()
            return {"success": True, "status": "connected", "agent_id": result["agent_id"]}

        state.pending_outbound[target_base] = result.get("agent_id", "")
        return {"success": True, "status": "pending", "request_id": result.get("request_id")}


def _sync_send_message(content, target_agent_id=None):
    """Send a message to an agent (sync version)."""
    message_id = f"msg-{uuid.uuid4().hex[:12]}"
    webhook = server._build_webhook_url(state, message_id)

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

    msg_timestamp = datetime.now(timezone.utc).isoformat()
    signature_hex = None
    if state.private_key_hex:
        signature_hex = server._sign_message(
            state.private_key_hex, state.agent_id, message_id, msg_timestamp, content
        )

    sent_to = []
    failed = []
    for conn in targets:
        try:
            base = _resolve_base_url(conn)
            payload = {
                "message_id": message_id, "content": content,
                "webhook": webhook, "hops_remaining": 10,
                "from_agent_id": state.agent_id, "metadata": {},
                "timestamp": msg_timestamp, "from_public_key_hex": state.public_key_hex,
                "signature_hex": signature_hex,
            }
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(base + "/__darkmatter__/message", json=payload)
                if resp.status_code >= 400:
                    failed.append({"agent_id": conn.agent_id, "error": resp.text})
                else:
                    conn.messages_sent += 1
                    conn.last_activity = datetime.now(timezone.utc).isoformat()
                    sent_to.append(conn.agent_id)
        except Exception as e:
            conn.messages_declined += 1
            failed.append({"agent_id": conn.agent_id, "error": str(e)})

    sent_msg = server.SentMessage(
        message_id=message_id, content=content, status="active",
        initial_hops=10, routed_to=sent_to,
    )
    state.sent_messages[message_id] = sent_msg
    server.save_state()

    result = {"success": len(sent_to) > 0, "message_id": message_id, "routed_to": sent_to}
    if failed:
        result["failed"] = failed
    return result


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
        resp_signature_hex = server._sign_message(
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
    except Exception:
        webhook_success = False

    if msg.from_agent_id and msg.from_agent_id in state.connections:
        conn = state.connections[msg.from_agent_id]
        conn.last_activity = datetime.now(timezone.utc).isoformat()

    server.save_state()
    return {"success": webhook_success, "message_id": msg.message_id}


# ---------------------------------------------------------------------------
# Web UI routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("chat.html",
                           state=state,
                           short_id=_short_id,
                           display_name_for=_display_name_for)


@app.route("/send", methods=["POST"])
def send():
    if _is_ajax():
        data = request.get_json(silent=True) or {}
        content = data.get("content", "").strip()
        target = data.get("target", "auto")
    else:
        content = request.form.get("content", "").strip()
        target = request.form.get("target", "auto")
    if not content:
        if _is_ajax():
            return jsonify({"success": False, "error": "Empty message"}), 400
        return redirect(url_for("index"))
    target_id = None if target == "auto" else target
    if _is_ajax():
        result = _sync_send_message(content, target_id)
        return jsonify(result)
    threading.Thread(target=_sync_send_message, args=(content, target_id), daemon=True).start()
    return redirect(url_for("index"))


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

    conn = server.Connection(
        agent_id=pending.from_agent_id,
        agent_url=pending.from_agent_url,
        agent_bio=pending.from_agent_bio,
        direction=server.ConnectionDirection.INBOUND,
        agent_public_key_hex=pending.from_agent_public_key_hex,
        agent_display_name=pending.from_agent_display_name,
    )
    state.connections[pending.from_agent_id] = conn

    payload = {
        "agent_id": state.agent_id, "agent_url": _get_public_url(),
        "agent_bio": state.bio, "agent_public_key_hex": state.public_key_hex,
        "agent_display_name": state.display_name,
    }

    base = pending.from_agent_url.rstrip("/")
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

    if not direct_ok and server.ANCHOR_NODES:
        try:
            anchor = server.ANCHOR_NODES[0]
            with httpx.Client(timeout=10.0) as client:
                client.post(
                    f"{anchor}/__darkmatter__/connection_relay/{pending.from_agent_id}",
                    json=payload,
                )
        except Exception:
            pass

    display_name = pending.from_agent_display_name or _short_id(pending.from_agent_id)
    del state.pending_requests[request_id]
    server.save_state()
    if _is_ajax():
        return jsonify({"success": True, "display_name": display_name})
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
        server.save_state()
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
    } for msg in state.message_queue]

    outbox = [{
        "message_id": sm.message_id, "status": sm.status, "content": sm.content,
        "response": sm.response.get("response") if sm.response else None,
        "response_agent": sm.response.get("agent_id") if sm.response else None,
        "response_display_name": _display_name_for(sm.response["agent_id"]) if sm.response else None,
        "routed_to": sm.routed_to, "created_at": sm.created_at,
    } for sm in sorted(state.sent_messages.values(), key=lambda s: s.created_at, reverse=True)[:50]]

    pending = [{
        "request_id": req.request_id, "from_agent_id": req.from_agent_id,
        "from_display_name": req.from_agent_display_name or _short_id(req.from_agent_id),
        "from_bio": req.from_agent_bio, "requested_at": req.requested_at,
    } for req in state.pending_requests.values()]

    connections = [{
        "agent_id": c.agent_id,
        "display_name": c.agent_display_name or _short_id(c.agent_id),
        "bio": c.agent_bio, "direction": c.direction.value,
        "last_activity": c.last_activity,
    } for c in state.connections.values() if c.agent_id != state.agent_id]

    # Discovered agents (not yet connected)
    connected_ids = set(state.connections.keys())
    with _discovery_lock:
        discovered = [{
            "agent_id": a["agent_id"], "display_name": a["display_name"],
            "bio": a["bio"], "url": a["url"], "port": a["port"],
            "status": a["status"], "accepting": a["accepting"],
        } for a in _discovered_agents.values() if a["agent_id"] not in connected_ids]

    return jsonify({
        "inbox": inbox, "outbox": outbox, "pending_requests": pending,
        "connections": connections, "discovered": discovered,
    })


@app.route("/api/scan", methods=["POST"])
def scan():
    """Force an immediate discovery scan."""
    _scan_local_agents()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"[DarkMatter Entrypoint] Human node on http://localhost:{PORT}", file=sys.stderr)
    print(f"[DarkMatter Entrypoint] Public URL: {_get_public_url()}", file=sys.stderr)
    print(f"[DarkMatter Entrypoint] Agent ID: {_short_id(state.agent_id)}", file=sys.stderr)
    print(f"[DarkMatter Entrypoint] Display name: {state.display_name}", file=sys.stderr)
    print(f"[DarkMatter Entrypoint] Connections: {len(state.connections)}", file=sys.stderr)
    app.run(host="0.0.0.0", port=PORT, debug=True)

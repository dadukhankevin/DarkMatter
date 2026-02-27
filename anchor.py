"""
DarkMatter Anchor Node — Lightweight Directory Service

A Flask Blueprint that provides peer registration and lookup for DarkMatter
mesh agents. Anchor nodes are stable, well-known servers that agents can
fall back to when the mesh is sparse.

Designed to be:
  - Imported as a Flask Blueprint into any Flask app (e.g. LoseyLabs)
  - Run standalone for testing: python3 anchor.py

Storage is in-memory with optional JSON file backup. No database needed.
Stale entries (>24h unseen) are pruned on writes.
"""

import collections
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, request, jsonify

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Stale threshold: entries unseen for >24 hours are pruned
STALE_THRESHOLD = 86400  # 24 hours in seconds

# Max age for peer_update timestamps (prevents replay attacks)
PEER_UPDATE_MAX_AGE = 300  # 5 minutes

# Directory size cap — prevents memory exhaustion from mass registration
MAX_DIRECTORY_SIZE = 10000

# Webhook relay settings
RELAY_BUFFER_MAX_PER_SENDER = 100
RELAY_ENTRY_TTL = 3600  # 1 hour

# URL validation
MAX_URL_LENGTH = 2048

# Per-IP rate limiting for peer_update
PEER_UPDATE_RATE_LIMIT = 10       # max updates per window per IP
PEER_UPDATE_RATE_WINDOW = 60      # window in seconds
_peer_update_timestamps: dict[str, collections.deque] = {}


def _verify_peer_update_signature(public_key_hex: str, signature_hex: str,
                                   agent_id: str, new_url: str, timestamp: str) -> bool:
    """Verify a signed peer_update payload. Returns True if valid."""
    try:
        public_bytes = bytes.fromhex(public_key_hex)
        public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
        signature = bytes.fromhex(signature_hex)
        payload = f"peer_update\n{agent_id}\n{new_url}\n{timestamp}".encode("utf-8")
        public_key.verify(signature, payload)
        return True
    except Exception:
        return False


def _is_timestamp_fresh(timestamp: str) -> bool:
    """Check if a timestamp is within PEER_UPDATE_MAX_AGE seconds of now."""
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        age = abs((datetime.now(timezone.utc) - ts).total_seconds())
        return age <= PEER_UPDATE_MAX_AGE
    except Exception:
        return False

def _validate_url(url: str) -> str | None:
    """Validate URL scheme and length. Returns error string or None."""
    if len(url) > MAX_URL_LENGTH:
        return f"URL exceeds maximum length ({MAX_URL_LENGTH} chars)"
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL"
    if parsed.scheme not in ("http", "https"):
        return f"URL scheme must be http or https, got '{parsed.scheme}'"
    if not parsed.hostname:
        return "URL has no hostname"
    return None


# File-backed directory — shared across gunicorn workers via flock
_directory_path: str = os.environ.get(
    "DARKMATTER_ANCHOR_DIRECTORY",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".darkmatter_anchor_directory.json"),
)

# Webhook relay buffer file — shared across workers via file I/O + flock
_relay_buffer_path: str = os.environ.get(
    "DARKMATTER_RELAY_BUFFER",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".darkmatter_relay_buffer.json"),
)

anchor_bp = Blueprint("darkmatter_anchor", __name__)


def _read_directory() -> dict[str, dict]:
    """Read the peer directory from disk. Returns empty dict if missing/corrupt."""
    try:
        with open(_directory_path) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_directory(directory: dict[str, dict]) -> None:
    """Write the peer directory to disk atomically with exclusive lock."""
    try:
        Path(_directory_path).parent.mkdir(parents=True, exist_ok=True)
        with open(_directory_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(directory, f)
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        print(f"[DarkMatter Anchor] Failed to write directory: {e}", file=sys.stderr)


def _prune_stale(directory: dict[str, dict]) -> dict[str, dict]:
    """Return a copy with entries unseen for more than STALE_THRESHOLD removed."""
    now = time.time()
    return {
        aid: entry for aid, entry in directory.items()
        if now - entry.get("last_seen", 0) <= STALE_THRESHOLD
    }


@anchor_bp.route("/__darkmatter__/peer_update", methods=["POST"])
def anchor_peer_update():
    """Register or update an agent's URL.

    Requires Ed25519 signature to prove the sender holds the private key.
    First registration pins the public key. Subsequent updates must use
    the same key and provide a valid signature.
    """
    # Per-IP rate limiting
    client_ip = request.access_route[0] if request.access_route else (request.remote_addr or "unknown")
    now = time.time()
    cutoff = now - PEER_UPDATE_RATE_WINDOW
    if client_ip not in _peer_update_timestamps:
        _peer_update_timestamps[client_ip] = collections.deque()
    ts = _peer_update_timestamps[client_ip]
    while ts and ts[0] < cutoff:
        ts.popleft()
    if len(ts) >= PEER_UPDATE_RATE_LIMIT:
        return jsonify({"error": "Rate limit exceeded"}), 429
    ts.append(now)

    # Evict stale IPs when dict grows too large
    if len(_peer_update_timestamps) > 1000:
        empty_ips = [ip for ip, dq in _peer_update_timestamps.items() if not dq]
        for ip in empty_ips:
            del _peer_update_timestamps[ip]

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    agent_id = data.get("agent_id", "").strip()
    new_url = data.get("new_url", "").strip()
    public_key_hex = data.get("public_key_hex", "").strip()
    signature = data.get("signature", "").strip()
    timestamp = data.get("timestamp", "").strip()

    if not agent_id or not new_url:
        return jsonify({"error": "Missing agent_id or new_url"}), 400

    # Validate URL scheme and length
    url_err = _validate_url(new_url)
    if url_err:
        return jsonify({"error": url_err}), 400

    # Validate agent_id length
    if len(agent_id) > 128:
        return jsonify({"error": "agent_id too long"}), 400

    if not public_key_hex or not signature or not timestamp:
        return jsonify({"error": "Missing public_key_hex, signature, or timestamp"}), 400

    # Verify timestamp freshness (prevents replay attacks)
    if not _is_timestamp_fresh(timestamp):
        return jsonify({"error": "Timestamp expired or invalid"}), 403

    # Verify Ed25519 signature proves ownership of the private key
    if not _verify_peer_update_signature(public_key_hex, signature, agent_id, new_url, timestamp):
        return jsonify({"error": "Invalid signature"}), 403

    directory = _read_directory()
    existing = directory.get(agent_id)

    # If entry exists, verify the public key matches the pinned one
    if existing and existing.get("public_key_hex"):
        if public_key_hex != existing["public_key_hex"]:
            return jsonify({"error": "Public key mismatch"}), 403

    # Check directory size cap (only for new entries)
    if agent_id not in directory and len(directory) >= MAX_DIRECTORY_SIZE:
        directory = _prune_stale(directory)
        if len(directory) >= MAX_DIRECTORY_SIZE:
            return jsonify({"error": "Directory full"}), 429

    directory[agent_id] = {
        "url": new_url,
        "public_key_hex": public_key_hex,
        "last_seen": time.time(),
    }

    directory = _prune_stale(directory)
    _write_directory(directory)

    return jsonify({"success": True, "registered": existing is None})


@anchor_bp.route("/__darkmatter__/peer_lookup/<agent_id>", methods=["GET"])
def anchor_peer_lookup(agent_id):
    """Look up a registered agent's current URL."""
    directory = _read_directory()
    entry = directory.get(agent_id)
    if not entry:
        return jsonify({"error": "Unknown agent"}), 404

    # Check staleness
    if time.time() - entry.get("last_seen", 0) > STALE_THRESHOLD:
        del directory[agent_id]
        _write_directory(directory)
        return jsonify({"error": "Unknown agent"}), 404

    return jsonify({
        "agent_id": agent_id,
        "url": entry["url"],
        "last_seen": entry["last_seen"],
    })


@anchor_bp.route("/.well-known/darkmatter.json", methods=["GET"])
def anchor_well_known():
    """Discovery endpoint identifying this as a DarkMatter anchor node."""
    return jsonify({
        "darkmatter": True,
        "anchor": True,
        "protocol_version": "0.2",
        "agents_registered": len(_read_directory()),
    })


# ---------------------------------------------------------------------------
# Webhook Relay — buffer webhook callbacks for NAT-ed agents
#
# Storage is a JSON file protected by flock so multiple gunicorn workers
# can safely read/write the same buffer.
# ---------------------------------------------------------------------------

def _read_relay_buffer() -> dict[str, list[dict]]:
    """Read the relay buffer from disk. Returns empty dict if missing/corrupt."""
    try:
        with open(_relay_buffer_path) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_relay_buffer(buf: dict[str, list[dict]]) -> None:
    """Write the relay buffer to disk atomically with exclusive lock."""
    try:
        Path(_relay_buffer_path).parent.mkdir(parents=True, exist_ok=True)
        with open(_relay_buffer_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(buf, f)
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        print(f"[DarkMatter Anchor] Failed to write relay buffer: {e}", file=sys.stderr)


def _prune_relay_buffer(buf: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Remove expired entries from a relay buffer dict. Returns pruned copy."""
    now = time.time()
    pruned = {}
    for sender_id, entries in buf.items():
        valid = [e for e in entries if now - e.get("timestamp", 0) < RELAY_ENTRY_TTL]
        if valid:
            pruned[sender_id] = valid
    return pruned


def _verify_relay_poll_signature(agent_id: str, signature_hex: str, timestamp: str) -> bool:
    """Verify Ed25519 signature for relay poll requests.

    The agent signs "relay_poll\\n{agent_id}\\n{timestamp}" to prove identity.
    Uses the public key from the peer directory if registered.
    """
    entry = _read_directory().get(agent_id)
    if not entry or not entry.get("public_key_hex"):
        return False
    try:
        public_bytes = bytes.fromhex(entry["public_key_hex"])
        public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
        signature = bytes.fromhex(signature_hex)
        payload = f"relay_poll\n{agent_id}\n{timestamp}".encode("utf-8")
        public_key.verify(signature, payload)
        return True
    except Exception:
        return False


@anchor_bp.route("/__darkmatter__/webhook_relay/<sender_agent_id>/<message_id>", methods=["POST"])
def relay_webhook_post(sender_agent_id, message_id):
    """Buffer an incoming webhook callback for a NAT-ed sender."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    buf = _prune_relay_buffer(_read_relay_buffer())

    if sender_agent_id not in buf:
        buf[sender_agent_id] = []

    entries = buf[sender_agent_id]

    # Enforce per-sender limit — prune oldest
    while len(entries) >= RELAY_BUFFER_MAX_PER_SENDER:
        entries.pop(0)

    entries.append({
        "message_id": message_id,
        "data": data,
        "timestamp": time.time(),
    })

    _write_relay_buffer(buf)
    return jsonify({"success": True, "buffered": True})


@anchor_bp.route("/__darkmatter__/webhook_relay/<sender_agent_id>/<message_id>", methods=["GET"])
def relay_webhook_status(sender_agent_id, message_id):
    """Webhook status proxy — return a generic OK so agents know the webhook is valid."""
    return jsonify({"status": "relay", "message_id": message_id})


@anchor_bp.route("/__darkmatter__/webhook_relay_poll/<sender_agent_id>", methods=["GET"])
def relay_webhook_poll(sender_agent_id):
    """Drain buffered webhook callbacks for a sender. Requires Ed25519 signature."""
    signature_hex = request.args.get("signature", "")
    timestamp = request.args.get("timestamp", "")

    if not signature_hex or not timestamp:
        return jsonify({"error": "Missing signature or timestamp"}), 400

    if not _is_timestamp_fresh(timestamp):
        return jsonify({"error": "Timestamp expired"}), 403

    if not _verify_relay_poll_signature(sender_agent_id, signature_hex, timestamp):
        return jsonify({"error": "Invalid signature"}), 403

    buf = _prune_relay_buffer(_read_relay_buffer())
    entries = buf.pop(sender_agent_id, [])
    _write_relay_buffer(buf)

    now = time.time()
    valid = [e for e in entries if now - e.get("timestamp", 0) < RELAY_ENTRY_TTL]

    return jsonify({
        "success": True,
        "callbacks": [{"message_id": e["message_id"], "data": e["data"]} for e in valid],
    })


# ---------------------------------------------------------------------------
# Connection Relay — buffer connection_accepted callbacks for NAT-ed agents
#
# Same file-based buffer approach as webhook relay, but stored under a
# separate "connections" top-level key to avoid collisions.
# ---------------------------------------------------------------------------

CONN_RELAY_BUFFER_MAX_PER_AGENT = 20
CONN_RELAY_ENTRY_TTL = 3600  # 1 hour


def _read_conn_relay_buffer() -> dict[str, list[dict]]:
    """Read the connection relay buffer from the shared relay buffer file."""
    try:
        with open(_relay_buffer_path) as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            data = json.load(f)
            fcntl.flock(f, fcntl.LOCK_UN)
        top = data.get("connections") if isinstance(data, dict) else None
        return top if isinstance(top, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_conn_relay_buffer(conn_buf: dict[str, list[dict]]) -> None:
    """Write the connection relay buffer back into the shared relay buffer file."""
    try:
        Path(_relay_buffer_path).parent.mkdir(parents=True, exist_ok=True)
        # Read existing file to preserve webhook entries
        try:
            with open(_relay_buffer_path) as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                full = json.load(f)
                fcntl.flock(f, fcntl.LOCK_UN)
            if not isinstance(full, dict):
                full = {}
        except (FileNotFoundError, json.JSONDecodeError):
            full = {}
        full["connections"] = conn_buf
        with open(_relay_buffer_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(full, f)
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        print(f"[DarkMatter Anchor] Failed to write conn relay buffer: {e}", file=sys.stderr)


def _prune_conn_relay_buffer(buf: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Remove expired entries from the connection relay buffer."""
    now = time.time()
    pruned = {}
    for agent_id, entries in buf.items():
        valid = [e for e in entries if now - e.get("timestamp", 0) < CONN_RELAY_ENTRY_TTL]
        if valid:
            pruned[agent_id] = valid
    return pruned


@anchor_bp.route("/__darkmatter__/connection_relay/<target_agent_id>", methods=["POST"])
def relay_connection_post(target_agent_id):
    """Buffer a connection_accepted callback for a NAT-ed target agent."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    buf = _prune_conn_relay_buffer(_read_conn_relay_buffer())

    if target_agent_id not in buf:
        buf[target_agent_id] = []

    entries = buf[target_agent_id]

    # Enforce per-agent limit — prune oldest
    while len(entries) >= CONN_RELAY_BUFFER_MAX_PER_AGENT:
        entries.pop(0)

    entries.append({
        "data": data,
        "timestamp": time.time(),
    })

    _write_conn_relay_buffer(buf)
    return jsonify({"success": True, "buffered": True})


@anchor_bp.route("/__darkmatter__/connection_relay_poll/<agent_id>", methods=["GET"])
def relay_connection_poll(agent_id):
    """Drain buffered connection callbacks for an agent. Requires Ed25519 signature."""
    signature_hex = request.args.get("signature", "")
    timestamp = request.args.get("timestamp", "")

    if not signature_hex or not timestamp:
        return jsonify({"error": "Missing signature or timestamp"}), 400

    if not _is_timestamp_fresh(timestamp):
        return jsonify({"error": "Timestamp expired"}), 403

    if not _verify_relay_poll_signature(agent_id, signature_hex, timestamp):
        return jsonify({"error": "Invalid signature"}), 403

    buf = _prune_conn_relay_buffer(_read_conn_relay_buffer())
    entries = buf.pop(agent_id, [])
    _write_conn_relay_buffer(buf)

    now = time.time()
    valid = [e for e in entries if now - e.get("timestamp", 0) < CONN_RELAY_ENTRY_TTL]

    return jsonify({
        "success": True,
        "callbacks": [e["data"] for e in valid],
    })


# CSRF exemption list — these routes need to be exempt when used with Flask-WTF
CSRF_EXEMPT_VIEWS = [
    "darkmatter_anchor.anchor_peer_update",
    "darkmatter_anchor.anchor_peer_lookup",
    "darkmatter_anchor.anchor_well_known",
    "darkmatter_anchor.relay_webhook_post",
    "darkmatter_anchor.relay_webhook_status",
    "darkmatter_anchor.relay_webhook_poll",
    "darkmatter_anchor.relay_connection_post",
    "darkmatter_anchor.relay_connection_poll",
]


# ---------------------------------------------------------------------------
# Standalone mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(anchor_bp)

    port = int(os.environ.get("DARKMATTER_ANCHOR_PORT", "5001"))
    print(f"[DarkMatter Anchor] Starting standalone on port {port}")
    print(f"[DarkMatter Anchor] Discovery: http://localhost:{port}/.well-known/darkmatter.json")
    app.run(host="127.0.0.1", port=port, debug=True)

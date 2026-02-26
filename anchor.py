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


# In-memory directory: {agent_id: {url, public_key_hex, last_seen}}
_directory: dict[str, dict] = {}

# JSON file backup path (configurable via env)
_backup_path: str | None = os.environ.get("DARKMATTER_ANCHOR_BACKUP")

anchor_bp = Blueprint("darkmatter_anchor", __name__)


def _prune_stale() -> None:
    """Remove entries unseen for more than STALE_THRESHOLD seconds."""
    now = time.time()
    stale_ids = [
        aid for aid, entry in _directory.items()
        if now - entry.get("last_seen", 0) > STALE_THRESHOLD
    ]
    for aid in stale_ids:
        del _directory[aid]


def _save_backup() -> None:
    """Persist directory to JSON file if backup path is configured."""
    if not _backup_path:
        return
    try:
        Path(_backup_path).parent.mkdir(parents=True, exist_ok=True)
        with open(_backup_path, "w") as f:
            json.dump(_directory, f)
    except Exception as e:
        print(f"[DarkMatter Anchor] Failed to save backup: {e}", file=sys.stderr)


def _load_backup() -> None:
    """Load directory from JSON backup file on startup."""
    global _directory
    if not _backup_path:
        return
    try:
        with open(_backup_path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            _directory = data
            _prune_stale()
    except (FileNotFoundError, json.JSONDecodeError):
        pass


# Load backup on import
_load_backup()


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

    existing = _directory.get(agent_id)

    # If entry exists, verify the public key matches the pinned one
    if existing and existing.get("public_key_hex"):
        if public_key_hex != existing["public_key_hex"]:
            return jsonify({"error": "Public key mismatch"}), 403

    # Check directory size cap (only for new entries)
    if agent_id not in _directory and len(_directory) >= MAX_DIRECTORY_SIZE:
        _prune_stale()
        if len(_directory) >= MAX_DIRECTORY_SIZE:
            return jsonify({"error": "Directory full"}), 429

    _directory[agent_id] = {
        "url": new_url,
        "public_key_hex": public_key_hex,
        "last_seen": time.time(),
    }

    _prune_stale()
    _save_backup()

    return jsonify({"success": True, "registered": existing is None})


@anchor_bp.route("/__darkmatter__/peer_lookup/<agent_id>", methods=["GET"])
def anchor_peer_lookup(agent_id):
    """Look up a registered agent's current URL."""
    entry = _directory.get(agent_id)
    if not entry:
        return jsonify({"error": "Unknown agent"}), 404

    # Check staleness
    if time.time() - entry.get("last_seen", 0) > STALE_THRESHOLD:
        del _directory[agent_id]
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
        "agents_registered": len(_directory),
    })


# CSRF exemption list — these routes need to be exempt when used with Flask-WTF
CSRF_EXEMPT_VIEWS = [
    "darkmatter_anchor.anchor_peer_update",
    "darkmatter_anchor.anchor_peer_lookup",
    "darkmatter_anchor.anchor_well_known",
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

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

import json
import os
import time
from pathlib import Path

from flask import Blueprint, request, jsonify

# Stale threshold: entries unseen for >24 hours are pruned
STALE_THRESHOLD = 86400  # 24 hours in seconds

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
    except Exception:
        pass


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

    First registration stores the public key. Subsequent updates must
    provide the same public key (prevents spoofing).
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    agent_id = data.get("agent_id", "").strip()
    new_url = data.get("new_url", "").strip()
    public_key_hex = data.get("public_key_hex", "").strip()

    if not agent_id or not new_url:
        return jsonify({"error": "Missing agent_id or new_url"}), 400

    existing = _directory.get(agent_id)

    # If entry exists and has a public key, verify it matches
    if existing and existing.get("public_key_hex"):
        if public_key_hex and public_key_hex != existing["public_key_hex"]:
            return jsonify({"error": "Public key mismatch"}), 403

    _directory[agent_id] = {
        "url": new_url,
        "public_key_hex": public_key_hex or (existing or {}).get("public_key_hex", ""),
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

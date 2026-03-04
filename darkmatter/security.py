"""
Centralized cryptographic security — signing, verification, E2E encryption,
challenge-response, and URL security assessment.

Depends on: config
"""

import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305


# =============================================================================
# Domain Constants — prevents cross-domain signature replay
# =============================================================================

DOMAIN_MESSAGE = "darkmatter.message.v1"
DOMAIN_PEER_UPDATE = "peer_update"  # matches existing wire format
DOMAIN_RELAY_POLL = "relay_poll"    # matches existing wire format
DOMAIN_SDP = "darkmatter.sdp.v1"
DOMAIN_SHARD = "darkmatter.shard.v1"
DOMAIN_LAN_BEACON = "darkmatter.lan_beacon.v1"
DOMAIN_CHALLENGE_RESPONSE = "darkmatter.challenge_response.v1"
DOMAIN_GENOME = "darkmatter.genome.v1"

E2E_HKDF_INFO = b"darkmatter-e2e-v1"

# Challenge TTL in seconds
CHALLENGE_TTL = 60

# In-memory challenge store: {challenge_id: (challenge_hex, for_agent_id, created_at)}
_pending_challenges: dict[str, tuple[str, str, float]] = {}


# =============================================================================
# Domain-Separated Signing
# =============================================================================

def sign_payload(private_key_hex: str, domain: str, *fields: str) -> str:
    """Sign a domain-separated payload. Returns signature as hex.

    Canonical form: domain\\nfield1\\nfield2\\n...
    """
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    canonical = "\n".join([domain] + list(fields)).encode("utf-8")
    return private_key.sign(canonical).hex()


def verify_signed_payload(public_key_hex: str, signature_hex: str,
                          domain: str, *fields: str) -> bool:
    """Verify a domain-separated signed payload. Returns True if valid."""
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        signature = bytes.fromhex(signature_hex)
        canonical = "\n".join([domain] + list(fields)).encode("utf-8")
        public_key.verify(signature, canonical)
        return True
    except Exception:
        return False


# =============================================================================
# Backward-Compatible Signing (matches existing wire format)
# =============================================================================

def sign_message(private_key_hex: str, from_agent_id: str, message_id: str,
                 timestamp: str, content: str) -> str:
    """Sign a message payload (backward-compatible format). Returns signature hex."""
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    payload = f"{from_agent_id}\n{message_id}\n{timestamp}\n{content}".encode("utf-8")
    return private_key.sign(payload).hex()


def verify_message(public_key_hex: str, signature_hex: str, from_agent_id: str,
                   message_id: str, timestamp: str, content: str) -> bool:
    """Verify a signed message payload (backward-compatible format)."""
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        signature = bytes.fromhex(signature_hex)
        payload = f"{from_agent_id}\n{message_id}\n{timestamp}\n{content}".encode("utf-8")
        public_key.verify(signature, payload)
        return True
    except Exception:
        return False


def sign_peer_update(private_key_hex: str, agent_id: str, new_url: str, timestamp: str) -> str:
    """Sign a peer_update payload. Returns signature hex."""
    return sign_payload(private_key_hex, DOMAIN_PEER_UPDATE, agent_id, new_url, timestamp)


def verify_peer_update_signature(public_key_hex: str, signature_hex: str,
                                  agent_id: str, new_url: str, timestamp: str) -> bool:
    """Verify a signed peer_update payload."""
    return verify_signed_payload(public_key_hex, signature_hex, DOMAIN_PEER_UPDATE, agent_id, new_url, timestamp)


def sign_relay_poll(private_key_hex: str, agent_id: str, timestamp: str) -> str:
    """Sign a relay poll request. Returns signature hex."""
    return sign_payload(private_key_hex, DOMAIN_RELAY_POLL, agent_id, timestamp)


# =============================================================================
# Inbound Message Verification
# =============================================================================

@dataclass
class VerifiedPayload:
    """Result of verifying an inbound message."""
    verified: bool
    error: Optional[str] = None
    status_code: int = 200
    pinned_key: bool = False  # True if we pinned a new public key


def verify_inbound(data: dict, connections: dict) -> VerifiedPayload:
    """Verify an inbound message's cryptographic signature.

    Consolidates the 40-line nested conditional from mesh.py.
    Requires valid signature — rejects unsigned messages.
    """
    from_agent_id = data.get("from_agent_id", "")
    message_id = data.get("message_id", "")
    content = data.get("content", "")
    msg_timestamp = data.get("timestamp", "")
    from_public_key_hex = data.get("from_public_key_hex")
    signature_hex = data.get("signature_hex")
    is_connected = from_agent_id in connections

    if is_connected:
        conn = connections[from_agent_id]
        if conn.agent_public_key_hex:
            # We have a stored public key — signature is REQUIRED
            if from_public_key_hex and conn.agent_public_key_hex != from_public_key_hex:
                return VerifiedPayload(
                    verified=False,
                    error="Public key mismatch — sender key does not match stored key for this connection.",
                    status_code=403,
                )
            if not signature_hex or not msg_timestamp:
                return VerifiedPayload(
                    verified=False,
                    error="Signature required — this connection has a known public key.",
                    status_code=403,
                )
            if not verify_message(conn.agent_public_key_hex, signature_hex,
                                  from_agent_id, message_id, msg_timestamp, content):
                return VerifiedPayload(
                    verified=False,
                    error="Invalid signature — message authenticity could not be verified.",
                    status_code=403,
                )
            return VerifiedPayload(verified=True)
        elif from_public_key_hex:
            # Peer sent a key but we don't have one stored — pin it and verify
            if not signature_hex or not msg_timestamp:
                return VerifiedPayload(
                    verified=False,
                    error="Signature required when presenting a public key.",
                    status_code=403,
                )
            if not verify_message(from_public_key_hex, signature_hex,
                                  from_agent_id, message_id, msg_timestamp, content):
                return VerifiedPayload(
                    verified=False,
                    error="Invalid signature — message authenticity could not be verified.",
                    status_code=403,
                )
            conn.agent_public_key_hex = from_public_key_hex
            return VerifiedPayload(verified=True, pinned_key=True)
        else:
            # Connected but no key on either side — reject (no backward compat)
            return VerifiedPayload(
                verified=False,
                error="Signature required — unsigned messages are not accepted.",
                status_code=403,
            )
    elif from_public_key_hex and signature_hex and msg_timestamp:
        # Not connected, but accept if cryptographically signed
        if not verify_message(from_public_key_hex, signature_hex,
                              from_agent_id, message_id, msg_timestamp, content):
            return VerifiedPayload(
                verified=False,
                error="Invalid signature — message authenticity could not be verified.",
                status_code=403,
            )
        return VerifiedPayload(verified=True)
    else:
        # No connection AND no signature — reject
        return VerifiedPayload(
            verified=False,
            error="Not connected — unsigned messages require a connection.",
            status_code=403,
        )


# =============================================================================
# Outbound Message Preparation
# =============================================================================

@dataclass
class SignedEnvelope:
    """A signed (and optionally encrypted) outbound payload."""
    payload: dict
    encrypted: bool = False


def prepare_outbound(payload: dict, private_key_hex: str, agent_id: str,
                     public_key_hex: str, recipient_public_key_hex: Optional[str] = None) -> SignedEnvelope:
    """Sign an outbound message payload. Optionally E2E encrypts if recipient key provided."""
    message_id = payload.get("message_id", "")
    content = payload.get("content", "")
    timestamp = payload.get("timestamp", "")

    signature_hex = sign_message(private_key_hex, agent_id, message_id, timestamp, content)

    signed_payload = dict(payload)
    signed_payload["from_agent_id"] = agent_id
    signed_payload["from_public_key_hex"] = public_key_hex
    signed_payload["signature_hex"] = signature_hex

    if recipient_public_key_hex:
        import json
        plaintext = json.dumps(signed_payload).encode("utf-8")
        encrypted = encrypt_for_peer(plaintext, private_key_hex, recipient_public_key_hex)
        return SignedEnvelope(
            payload={
                "e2e_encrypted": True,
                "encrypted_payload": encrypted,
                "from_agent_id": agent_id,
            },
            encrypted=True,
        )

    return SignedEnvelope(payload=signed_payload)


# =============================================================================
# E2E Encryption (Ed25519 → X25519 + ECDH + HKDF + ChaCha20-Poly1305)
# =============================================================================

def _ed25519_private_to_x25519(private_key_hex: str) -> X25519PrivateKey:
    """Convert Ed25519 private key to X25519 for ECDH."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as Ed25519Priv
    ed_key = Ed25519Priv.from_private_bytes(bytes.fromhex(private_key_hex))
    # Get raw private key bytes and convert via RFC 8032 → RFC 7748
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
    raw = ed_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    # Hash with SHA-512 and take first 32 bytes (clamped) — standard Ed25519→X25519
    import hashlib
    h = hashlib.sha512(raw).digest()[:32]
    h_array = bytearray(h)
    h_array[0] &= 248
    h_array[31] &= 127
    h_array[31] |= 64
    return X25519PrivateKey.from_private_bytes(bytes(h_array))


def _ed25519_public_to_x25519(public_key_hex: str) -> X25519PublicKey:
    """Convert Ed25519 public key to X25519 for ECDH.

    Uses the birational map from Edwards to Montgomery form.
    """
    # This uses the cryptography library's built-in conversion
    # We need to use the low-level conversion from Ed25519 to X25519
    # The proper way: use the ed25519 point and apply the birational map
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey as Ed25519Pub
    ed_pub = Ed25519Pub.from_public_bytes(bytes.fromhex(public_key_hex))

    # Get the raw Ed25519 public key bytes (compressed Edwards point)
    ed_raw = ed_pub.public_bytes(Encoding.Raw, PublicFormat.Raw)

    # Convert Edwards y-coordinate to Montgomery u-coordinate
    # u = (1 + y) / (1 - y) mod p where p = 2^255 - 19
    p = (1 << 255) - 19
    y = int.from_bytes(ed_raw, "little") & ((1 << 255) - 1)  # clear sign bit
    numerator = (1 + y) % p
    denominator = (1 - y) % p
    # Modular inverse: denominator^(p-2) mod p
    inv_denom = pow(denominator, p - 2, p)
    u = (numerator * inv_denom) % p
    x25519_bytes = u.to_bytes(32, "little")
    return X25519PublicKey.from_public_bytes(x25519_bytes)


def encrypt_for_peer(plaintext: bytes, sender_private_key_hex: str,
                     recipient_public_key_hex: str) -> dict:
    """Encrypt data for a specific peer using ECDH + HKDF + ChaCha20-Poly1305.

    Returns dict with: nonce, ciphertext, sender_public_key_hex (all hex-encoded).
    """
    sender_x25519 = _ed25519_private_to_x25519(sender_private_key_hex)
    recipient_x25519 = _ed25519_public_to_x25519(recipient_public_key_hex)

    shared_secret = sender_x25519.exchange(recipient_x25519)

    # Derive symmetric key via HKDF
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=E2E_HKDF_INFO,
    ).derive(shared_secret)

    nonce = os.urandom(12)
    cipher = ChaCha20Poly1305(key)
    ciphertext = cipher.encrypt(nonce, plaintext, None)

    # Include sender's Ed25519 public key so recipient can derive shared secret
    from darkmatter.identity import derive_public_key_hex
    sender_pub = derive_public_key_hex(sender_private_key_hex)

    return {
        "nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
        "sender_public_key_hex": sender_pub,
    }


def decrypt_from_peer(encrypted: dict, recipient_private_key_hex: str,
                      sender_public_key_hex: str) -> bytes:
    """Decrypt data from a peer using ECDH + HKDF + ChaCha20-Poly1305.

    Raises ValueError on decryption failure.
    """
    try:
        recipient_x25519 = _ed25519_private_to_x25519(recipient_private_key_hex)
        sender_x25519 = _ed25519_public_to_x25519(sender_public_key_hex)

        shared_secret = recipient_x25519.exchange(sender_x25519)

        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=E2E_HKDF_INFO,
        ).derive(shared_secret)

        nonce = bytes.fromhex(encrypted["nonce"])
        ciphertext = bytes.fromhex(encrypted["ciphertext"])

        cipher = ChaCha20Poly1305(key)
        return cipher.decrypt(nonce, ciphertext, None)
    except Exception as e:
        raise ValueError(f"Decryption failed: {e}") from e


# =============================================================================
# Challenge-Response (Connection Handshake)
# =============================================================================

def create_challenge(for_agent_id: str) -> tuple[str, str]:
    """Create a challenge for proof-of-possession during connection handshake.

    Returns (challenge_id, challenge_hex). Challenge is stored in-memory with TTL.
    """
    _prune_expired_challenges()
    challenge_bytes = secrets.token_bytes(32)
    challenge_hex = challenge_bytes.hex()
    challenge_id = f"ch-{secrets.token_hex(8)}"
    _pending_challenges[challenge_id] = (challenge_hex, for_agent_id, time.time())
    return challenge_id, challenge_hex


def prove_identity(challenge_hex: str, private_key_hex: str) -> str:
    """Sign a challenge to prove possession of the private key. Returns proof_hex."""
    return sign_payload(private_key_hex, DOMAIN_CHALLENGE_RESPONSE, challenge_hex)


def verify_proof(agent_id: str, public_key_hex: str, challenge_id: str,
                 proof_hex: str, challenge_hex: Optional[str] = None) -> bool:
    """Verify a challenge-response proof.

    If challenge_hex is not provided, looks up the challenge from the in-memory store.
    Returns True if proof is valid and challenge exists and hasn't expired.
    """
    _prune_expired_challenges()

    if challenge_hex is None:
        stored = _pending_challenges.get(challenge_id)
        if stored is None:
            return False
        challenge_hex, expected_agent_id, created_at = stored
        if expected_agent_id != agent_id:
            return False
        if time.time() - created_at > CHALLENGE_TTL:
            _pending_challenges.pop(challenge_id, None)
            return False
    else:
        # Also validate against store if available
        stored = _pending_challenges.get(challenge_id)
        if stored is not None:
            stored_hex, expected_agent_id, created_at = stored
            if stored_hex != challenge_hex or expected_agent_id != agent_id:
                return False
            if time.time() - created_at > CHALLENGE_TTL:
                _pending_challenges.pop(challenge_id, None)
                return False

    result = verify_signed_payload(public_key_hex, proof_hex,
                                   DOMAIN_CHALLENGE_RESPONSE, challenge_hex)
    if result:
        # Consume the challenge
        _pending_challenges.pop(challenge_id, None)
    return result


def _prune_expired_challenges():
    """Remove expired challenges from the in-memory store."""
    now = time.time()
    expired = [cid for cid, (_, _, ts) in _pending_challenges.items()
               if now - ts > CHALLENGE_TTL]
    for cid in expired:
        del _pending_challenges[cid]


# =============================================================================
# Shard / SDP / LAN Beacon Signing Helpers
# =============================================================================

def sign_shard(private_key_hex: str, shard_id: str, author_agent_id: str,
               content: str, tags_str: str) -> str:
    """Sign a shared shard. Returns signature hex."""
    return sign_payload(private_key_hex, DOMAIN_SHARD, shard_id, author_agent_id, content, tags_str)


def verify_shard_signature(public_key_hex: str, signature_hex: str,
                           shard_id: str, author_agent_id: str,
                           content: str, tags_str: str) -> bool:
    """Verify a shared shard's signature."""
    return verify_signed_payload(public_key_hex, signature_hex,
                                 DOMAIN_SHARD, shard_id, author_agent_id, content, tags_str)


def sign_sdp(private_key_hex: str, agent_id: str, sdp: str) -> str:
    """Sign an SDP offer/answer. Returns signature hex."""
    return sign_payload(private_key_hex, DOMAIN_SDP, agent_id, sdp)


def verify_sdp_signature(public_key_hex: str, signature_hex: str,
                         agent_id: str, sdp: str) -> bool:
    """Verify an SDP signature."""
    return verify_signed_payload(public_key_hex, signature_hex, DOMAIN_SDP, agent_id, sdp)


def sign_lan_beacon(private_key_hex: str, agent_id: str, port: str, timestamp: str) -> str:
    """Sign a LAN discovery beacon. Returns signature hex."""
    return sign_payload(private_key_hex, DOMAIN_LAN_BEACON, agent_id, port, timestamp)


def verify_lan_beacon(public_key_hex: str, signature_hex: str,
                      agent_id: str, port: str, timestamp: str) -> bool:
    """Verify a LAN beacon signature."""
    return verify_signed_payload(public_key_hex, signature_hex,
                                 DOMAIN_LAN_BEACON, agent_id, port, timestamp)


def sign_genome(private_key_hex: str, genome_version: str, genome_hash: str) -> str:
    """Sign a genome zip. Returns signature hex."""
    return sign_payload(private_key_hex, DOMAIN_GENOME, genome_version, genome_hash)


def verify_genome_signature(public_key_hex: str, signature_hex: str,
                            genome_version: str, genome_hash: str) -> bool:
    """Verify a genome zip's signature."""
    return verify_signed_payload(public_key_hex, signature_hex,
                                 DOMAIN_GENOME, genome_version, genome_hash)


# =============================================================================
# URL Security Assessment
# =============================================================================

def assess_url_security(url: str) -> dict:
    """Assess the security of a peer URL.

    Returns dict with:
        secure: bool — whether TLS is used
        warning: Optional[str] — human-readable warning if insecure
        is_local: bool — whether the URL targets localhost/private IP
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return {"secure": False, "warning": "Invalid URL", "is_local": False}

    scheme = parsed.scheme.lower()
    hostname = parsed.hostname or ""

    from darkmatter.identity import is_private_ip
    is_local = False
    try:
        is_local = is_private_ip(hostname) or hostname in ("localhost", "127.0.0.1", "::1")
    except Exception:
        pass

    if scheme == "https":
        return {"secure": True, "warning": None, "is_local": is_local}

    if is_local:
        return {
            "secure": False,
            "warning": "HTTP on local network (acceptable for development)",
            "is_local": True,
        }

    return {
        "secure": False,
        "warning": f"Insecure HTTP connection to {hostname} — traffic is unencrypted",
        "is_local": False,
    }

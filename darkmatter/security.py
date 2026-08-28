"""Domain-separated signatures and sealed-body encryption for v3 envelopes."""

import hashlib
import os

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
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from darkmatter.identity import derive_public_key_hex

DOMAIN_MESSAGE = "darkmatter.message.v1"
DOMAIN_ENVELOPE = "darkmatter.envelope.v3"
E2E_HKDF_INFO_V3 = b"darkmatter-envelope-v3"


def sign_payload(private_key_hex: str, domain: str, *fields: str) -> str:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    canonical = "\n".join([domain] + list(fields)).encode("utf-8")
    return private_key.sign(canonical).hex()


def verify_signed_payload(public_key_hex: str, signature_hex: str,
                          domain: str, *fields: str) -> bool:
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        signature = bytes.fromhex(signature_hex)
        canonical = "\n".join([domain] + list(fields)).encode("utf-8")
        public_key.verify(signature, canonical)
        return True
    except Exception:
        return False


def sign_message(private_key_hex: str, from_agent_id: str, message_id: str,
                 timestamp: str, content: str) -> str:
    return sign_payload(
        private_key_hex, DOMAIN_MESSAGE,
        from_agent_id, message_id, timestamp, content,
    )


def verify_message(public_key_hex: str, signature_hex: str, from_agent_id: str,
                   message_id: str, timestamp: str, content: str) -> bool:
    return verify_signed_payload(
        public_key_hex, signature_hex, DOMAIN_MESSAGE,
        from_agent_id, message_id, timestamp, content,
    )


def _ed25519_private_to_x25519(private_key_hex: str) -> X25519PrivateKey:
    ed_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
    raw = ed_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    h = bytearray(hashlib.sha512(raw).digest()[:32])
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    return X25519PrivateKey.from_private_bytes(bytes(h))


def _ed25519_public_to_x25519(public_key_hex: str) -> X25519PublicKey:
    ed_pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
    ed_raw = ed_pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    p = (1 << 255) - 19
    y = int.from_bytes(ed_raw, "little") & ((1 << 255) - 1)
    u = ((1 + y) * pow((1 - y) % p, p - 2, p)) % p
    return X25519PublicKey.from_public_bytes(u.to_bytes(32, "little"))


def encrypt_for_peer(plaintext: bytes, sender_private_key_hex: str,
                     recipient_public_key_hex: str,
                     info: bytes = E2E_HKDF_INFO_V3) -> dict:
    sender_x25519 = _ed25519_private_to_x25519(sender_private_key_hex)
    recipient_x25519 = _ed25519_public_to_x25519(recipient_public_key_hex)
    shared_secret = sender_x25519.exchange(recipient_x25519)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(shared_secret)
    nonce = os.urandom(12)
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, None)
    return {
        "nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
        "sender_public_key_hex": derive_public_key_hex(sender_private_key_hex),
    }


def decrypt_from_peer(encrypted: dict, recipient_private_key_hex: str,
                      sender_public_key_hex: str,
                      info: bytes = E2E_HKDF_INFO_V3) -> bytes:
    try:
        recipient_x25519 = _ed25519_private_to_x25519(recipient_private_key_hex)
        sender_x25519 = _ed25519_public_to_x25519(sender_public_key_hex)
        shared_secret = recipient_x25519.exchange(sender_x25519)
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(shared_secret)
        return ChaCha20Poly1305(key).decrypt(
            bytes.fromhex(encrypted["nonce"]),
            bytes.fromhex(encrypted["ciphertext"]),
            None,
        )
    except Exception as e:
        raise ValueError(f"Decryption failed: {e}") from e

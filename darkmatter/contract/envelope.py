"""Seal and open v3 envelopes — signed metadata, encrypted body."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from darkmatter.config import MAX_ENVELOPE_BODY_SIZE
from darkmatter.contract.types import Envelope
from darkmatter.identity import derive_public_key_hex
from darkmatter.security import (
    DOMAIN_ENVELOPE,
    E2E_HKDF_INFO_V3,
    decrypt_from_peer,
    encrypt_for_peer,
    sign_payload,
    verify_signed_payload,
)

ANTIMATTER_SETTLEMENT_ENVELOPE_TYPES = frozenset({
    "antimatter_offer",
    "antimatter_accept",
    "antimatter_invoice",
    "antimatter_receipt",
    "antimatter_confirm",
    "antimatter_dispute",
})

ANTIMATTER_CONTRIBUTION_ENVELOPE_TYPES = frozenset({
    "antimatter_contribution",
    "antimatter_resolution",
    "antimatter_fulfillment",
})

ANTIMATTER_ENVELOPE_TYPES = (
    ANTIMATTER_SETTLEMENT_ENVELOPE_TYPES | ANTIMATTER_CONTRIBUTION_ENVELOPE_TYPES | {"antimatter_obligation"}
)

ENVELOPE_TYPES = frozenset({
    "introduce",
    "message",
    "accept",
    "ignore",
    "receipt",
    "hint",
    "forward",
    "referral",
    "presence",
}) | ANTIMATTER_ENVELOPE_TYPES

ACTIONABLE_ENVELOPE_TYPES = frozenset({"message", "forward", "referral"}) | ANTIMATTER_ENVELOPE_TYPES


def validate_envelope_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("Envelope id must be a bounded plain identifier")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _ciphertext_blob(ciphertext: dict) -> str:
    return json.dumps(ciphertext, sort_keys=True, separators=(",", ":"))


def _sign_fields(env: Envelope) -> list[str]:
    return [
        env.id,
        env.type,
        env.from_id,
        env.to_id,
        env.timestamp,
        env.expires_at or "",
        _ciphertext_blob(env.ciphertext),
    ]


def seal_envelope(
    private_key_hex: str,
    from_id: str,
    to_id: str,
    env_type: str,
    body: dict,
    expires_at: Optional[str] = None,
    envelope_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> Envelope:
    """Encrypt body to to_id and sign the public envelope."""
    if env_type not in ENVELOPE_TYPES:
        raise ValueError(f"Unknown envelope type: {env_type}")
    if envelope_id is not None:
        validate_envelope_id(envelope_id)
    if derive_public_key_hex(private_key_hex) != from_id:
        raise ValueError("Envelope from_id does not match the signing passport")
    if not isinstance(body, dict):
        raise ValueError("Envelope body must be an object")
    try:
        if len(to_id) != 64:
            raise ValueError
        bytes.fromhex(to_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Envelope to_id must be a 32-byte public key") from exc
    if expires_at:
        try:
            _parse_datetime(expires_at)
        except ValueError as exc:
            raise ValueError("expires_at must be an ISO-8601 timestamp") from exc
    if timestamp:
        try:
            _parse_datetime(timestamp)
        except ValueError as exc:
            raise ValueError("timestamp must be an ISO-8601 timestamp") from exc
    try:
        plaintext = json.dumps(body, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Envelope body must be JSON-serializable") from exc
    if len(plaintext) > MAX_ENVELOPE_BODY_SIZE:
        raise ValueError(f"Envelope body exceeds {MAX_ENVELOPE_BODY_SIZE} bytes")
    ciphertext = encrypt_for_peer(
        plaintext, private_key_hex, to_id, info=E2E_HKDF_INFO_V3,
    )
    env = Envelope(
        id=envelope_id or uuid.uuid4().hex,
        type=env_type,
        from_id=from_id,
        to_id=to_id,
        timestamp=timestamp or _now(),
        ciphertext=ciphertext,
        signature="",
        expires_at=expires_at,
        body=body,
    )
    env.signature = sign_payload(private_key_hex, DOMAIN_ENVELOPE, *_sign_fields(env))
    return env


def verify_envelope_signature(env: Envelope | dict) -> Envelope:
    """Verify public envelope metadata without attempting recipient decryption."""
    if isinstance(env, dict):
        try:
            env = Envelope.from_public_dict(env)
        except (KeyError, TypeError) as exc:
            raise ValueError("Malformed envelope") from exc
    validate_envelope_id(env.id)
    if not isinstance(env.type, str) or env.type not in ENVELOPE_TYPES:
        raise ValueError(f"Unknown envelope type: {env.type}")
    for field in (env.from_id, env.to_id):
        if not isinstance(field, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", field):
            raise ValueError("Malformed envelope identity")
    for value in (env.timestamp, env.expires_at):
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > 64 or "\n" in value or "\r" in value:
            raise ValueError("Malformed envelope timestamp")
        try:
            _parse_datetime(value)
        except (ValueError, TypeError) as exc:
            raise ValueError("Malformed envelope timestamp") from exc
    if not isinstance(env.ciphertext, dict):
        raise ValueError("Malformed envelope ciphertext")
    if env.from_id != env.ciphertext.get("sender_public_key_hex"):
        raise ValueError("Ciphertext sender does not match envelope from")
    try:
        valid_signature = verify_signed_payload(
            env.from_id, env.signature, DOMAIN_ENVELOPE, *_sign_fields(env),
        )
    except (TypeError, ValueError):
        valid_signature = False
    if not valid_signature:
        raise ValueError("Invalid envelope signature")
    return env


def open_envelope(env: Envelope | dict, recipient_private_key_hex: str) -> Envelope:
    """Verify signature and decrypt body. Raises ValueError on failure."""
    env = verify_envelope_signature(env)
    if env.to_id != derive_public_key_hex(recipient_private_key_hex):
        raise ValueError("Envelope is addressed to a different passport")
    raw = decrypt_from_peer(
        env.ciphertext,
        recipient_private_key_hex,
        env.from_id,
        info=E2E_HKDF_INFO_V3,
    )
    env.body = json.loads(raw.decode("utf-8"))
    if not isinstance(env.body, dict):
        raise ValueError("Envelope body must be an object")
    return env


def is_expired(env: Envelope, now: Optional[datetime] = None) -> bool:
    return is_expired_at(env.expires_at, now)


def is_expired_at(expires_at: Optional[str], now: Optional[datetime] = None) -> bool:
    if not expires_at:
        return False
    if not isinstance(expires_at, str):
        return True
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        ts = _parse_datetime(expires_at)
    except (TypeError, ValueError):
        return True
    return ts <= now

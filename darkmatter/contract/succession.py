"""Dual-signed passport succession proofs.

This is deliberately a contract primitive, not an automatic key-rotation
command. Replacing a live mailbox key also requires relationship migration and
must never happen as a side effect of maintenance or installation.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from darkmatter.contract.tenure import parse_timestamp, verify_passport_claim
from darkmatter.identity import derive_public_key_hex
from darkmatter.security import sign_payload, verify_signed_payload


SUCCESSION_VERSION = 1
SUCCESSION_PROTOCOL = "darkmatter/passport-succession/1"
DOMAIN_PREDECESSOR = "darkmatter.passport-succession.predecessor.v1"
DOMAIN_SUCCESSOR = "darkmatter.passport-succession.successor.v1"


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _public_key(value, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a 32-byte public key")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not hexadecimal") from exc
    return value.lower()


def _payload(proof: dict) -> dict:
    if not isinstance(proof, dict):
        raise ValueError("passport succession proof must be an object")
    predecessor = verify_passport_claim(proof.get("predecessor"))
    successor_id = _public_key(proof.get("successor_id"), "successor_id")
    if successor_id == predecessor["agent_id"]:
        raise ValueError("passport successor must be a different key")
    timestamp = parse_timestamp(proof.get("timestamp"), "succession timestamp")
    if timestamp < parse_timestamp(predecessor["created_at"], "predecessor created_at"):
        raise ValueError("succession predates the predecessor passport")
    if timestamp > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("succession timestamp is implausibly far in the future")
    return {
        "version": proof.get("version"),
        "protocol": proof.get("protocol"),
        "predecessor": predecessor,
        "successor_id": successor_id,
        "timestamp": timestamp.isoformat(),
    }


def create_passport_succession(
    predecessor_private_key_hex: str,
    successor_private_key_hex: str,
    predecessor_passport: dict,
    timestamp: str | None = None,
) -> dict:
    predecessor_id = derive_public_key_hex(predecessor_private_key_hex)
    predecessor = verify_passport_claim(predecessor_passport, predecessor_id)
    successor_id = derive_public_key_hex(successor_private_key_hex)
    payload = _payload({
        "version": SUCCESSION_VERSION,
        "protocol": SUCCESSION_PROTOCOL,
        "predecessor": predecessor,
        "successor_id": successor_id,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
    })
    return {
        **payload,
        "predecessor_signature": sign_payload(
            predecessor_private_key_hex,
            DOMAIN_PREDECESSOR,
            _canonical(payload),
        ),
        "successor_signature": sign_payload(
            successor_private_key_hex,
            DOMAIN_SUCCESSOR,
            _canonical(payload),
        ),
    }


def verify_passport_succession(
    proof: dict,
    expected_successor_id: str | None = None,
) -> dict:
    payload = _payload(proof)
    if payload["version"] != SUCCESSION_VERSION or payload["protocol"] != SUCCESSION_PROTOCOL:
        raise ValueError("unsupported passport succession proof")
    if expected_successor_id and payload["successor_id"] != expected_successor_id.lower():
        raise ValueError("passport succession names a different successor")
    predecessor_signature = proof.get("predecessor_signature")
    successor_signature = proof.get("successor_signature")
    if not isinstance(predecessor_signature, str) or not verify_signed_payload(
        payload["predecessor"]["agent_id"],
        predecessor_signature,
        DOMAIN_PREDECESSOR,
        _canonical(payload),
    ):
        raise ValueError("invalid predecessor succession signature")
    if not isinstance(successor_signature, str) or not verify_signed_payload(
        payload["successor_id"],
        successor_signature,
        DOMAIN_SUCCESSOR,
        _canonical(payload),
    ):
        raise ValueError("invalid successor succession signature")
    return {
        **payload,
        "predecessor_signature": predecessor_signature,
        "successor_signature": successor_signature,
    }


__all__ = [
    "SUCCESSION_PROTOCOL",
    "SUCCESSION_VERSION",
    "create_passport_succession",
    "verify_passport_succession",
]

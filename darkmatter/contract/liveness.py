"""Portable passport-signed liveness checkpoints.

The timestamp is still a claim about a clock. Unlike a router's bare
``observed_active_at`` value, however, a checkpoint proves that the target
passport itself signed the observation. A route hop countersigns the claim and
discloses when the relationship began.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from darkmatter.contract.tenure import parse_timestamp
from darkmatter.identity import derive_public_key_hex
from darkmatter.security import sign_payload, verify_signed_payload


LIVENESS_VERSION = 1
LIVENESS_PROTOCOL = "darkmatter/liveness/1"
DOMAIN_LIVENESS = "darkmatter.liveness.v1"


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _agent_id(value) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("liveness agent_id must be a 32-byte public key")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("liveness agent_id is not hexadecimal") from exc
    return value.lower()


def _payload(claim: dict) -> dict:
    if not isinstance(claim, dict):
        raise ValueError("liveness claim must be an object")
    timestamp = parse_timestamp(claim.get("timestamp"), "liveness timestamp")
    if timestamp > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("liveness timestamp is implausibly far in the future")
    return {
        "version": claim.get("version"),
        "protocol": claim.get("protocol"),
        "agent_id": _agent_id(claim.get("agent_id")),
        "timestamp": timestamp.isoformat(),
    }


def create_liveness_claim(
    private_key_hex: str,
    agent_id: str,
    timestamp: str | None = None,
) -> dict:
    """Create a portable statement that this passport was active at a time."""
    if derive_public_key_hex(private_key_hex) != agent_id:
        raise ValueError("liveness agent does not match the signing passport")
    payload = _payload({
        "version": LIVENESS_VERSION,
        "protocol": LIVENESS_PROTOCOL,
        "agent_id": agent_id,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
    })
    payload["signature"] = sign_payload(
        private_key_hex, DOMAIN_LIVENESS, _canonical(payload),
    )
    return payload


def verify_liveness_claim(claim: dict, expected_agent_id: str | None = None) -> dict:
    payload = _payload(claim)
    if payload["version"] != LIVENESS_VERSION or payload["protocol"] != LIVENESS_PROTOCOL:
        raise ValueError("unsupported liveness claim")
    if expected_agent_id and payload["agent_id"] != expected_agent_id.lower():
        raise ValueError("liveness claim belongs to a different agent")
    signature = claim.get("signature")
    if not isinstance(signature, str) or not verify_signed_payload(
        payload["agent_id"], signature, DOMAIN_LIVENESS, _canonical(payload),
    ):
        raise ValueError("invalid liveness claim signature")
    return {**payload, "signature": signature}


__all__ = [
    "DOMAIN_LIVENESS",
    "LIVENESS_PROTOCOL",
    "LIVENESS_VERSION",
    "create_liveness_claim",
    "verify_liveness_claim",
]

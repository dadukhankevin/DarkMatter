"""Portable passport-tenure claims used by AntiMatter routing.

The timestamp is an agent's signed claim about when this passport began.  A
signature makes the claim attributable and tamper evident; it does not create a
universal clock or prevent a newly generated passport from claiming an older
date.  Route hops therefore also disclose the sender's local relationship and
liveness observations so other agents can judge the evidence for themselves.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from darkmatter.identity import derive_public_key_hex
from darkmatter.security import sign_payload, verify_signed_payload


PASSPORT_CLAIM_VERSION = 1
DOMAIN_PASSPORT_CLAIM = "darkmatter.passport-tenure.v1"


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def parse_timestamp(value, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _payload(claim: dict) -> dict:
    if not isinstance(claim, dict):
        raise ValueError("passport claim must be an object")
    agent_id = claim.get("agent_id")
    if not isinstance(agent_id, str) or len(agent_id) != 64:
        raise ValueError("passport claim agent_id must be a 32-byte public key")
    try:
        bytes.fromhex(agent_id)
    except ValueError as exc:
        raise ValueError("passport claim agent_id is not hexadecimal") from exc
    created_at = claim.get("created_at")
    created = parse_timestamp(created_at, "passport claim created_at")
    if created > datetime.now(timezone.utc):
        raise ValueError("passport claim created_at cannot be in the future")
    return {
        "version": claim.get("version"),
        "agent_id": agent_id.lower(),
        "created_at": created.isoformat(),
    }


def create_passport_claim(
    private_key_hex: str,
    agent_id: str,
    created_at: str,
) -> dict:
    """Create a stable self-signed statement of passport tenure."""
    if derive_public_key_hex(private_key_hex) != agent_id:
        raise ValueError("passport claim agent_id does not match the signing passport")
    payload = _payload({
        "version": PASSPORT_CLAIM_VERSION,
        "agent_id": agent_id,
        "created_at": created_at,
    })
    payload["signature"] = sign_payload(
        private_key_hex,
        DOMAIN_PASSPORT_CLAIM,
        _canonical(payload),
    )
    return payload


def verify_passport_claim(claim: dict, expected_agent_id: str | None = None) -> dict:
    """Verify attribution and return the canonical claim."""
    payload = _payload(claim)
    if payload["version"] != PASSPORT_CLAIM_VERSION:
        raise ValueError(f"unsupported passport claim version: {payload['version']}")
    if expected_agent_id and payload["agent_id"] != expected_agent_id.lower():
        raise ValueError("passport claim belongs to a different agent")
    signature = claim.get("signature")
    if not isinstance(signature, str) or not verify_signed_payload(
        payload["agent_id"],
        signature,
        DOMAIN_PASSPORT_CLAIM,
        _canonical(payload),
    ):
        raise ValueError("invalid passport claim signature")
    return {**payload, "signature": signature}


__all__ = [
    "DOMAIN_PASSPORT_CLAIM",
    "PASSPORT_CLAIM_VERSION",
    "create_passport_claim",
    "parse_timestamp",
    "verify_passport_claim",
]

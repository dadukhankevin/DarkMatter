"""Passport-signed binding between a DarkMatter agent and a payment address."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from darkmatter.security import sign_payload, verify_signed_payload
from darkmatter.wallet.tokens import normalize_network


DOMAIN_WALLET = "darkmatter.wallet.v1"
WALLET_CLAIM_VERSION = 1


def _canonical(claim: dict) -> str:
    payload = {
        "version": claim.get("version"),
        "agent_id": claim.get("agent_id"),
        "chain": claim.get("chain"),
        "network": claim.get("network"),
        "address": claim.get("address"),
        "created_at": claim.get("created_at"),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _validate_address(address: object) -> str:
    if not isinstance(address, str) or not address.strip():
        raise ValueError("Wallet claim address is required")
    address = address.strip()
    try:
        from solders.pubkey import Pubkey

        Pubkey.from_string(address)
    except ImportError as exc:
        raise ValueError("Solana wallet support is not installed; install dmagent[solana]") from exc
    except ValueError as exc:
        raise ValueError("Wallet claim contains an invalid Solana address") from exc
    return address


def create_wallet_claim(
    private_key_hex: str,
    agent_id: str,
    address: str,
    *,
    network: str = "devnet",
) -> dict:
    claim = {
        "version": WALLET_CLAIM_VERSION,
        "agent_id": agent_id,
        "chain": "solana",
        "network": normalize_network(network),
        "address": _validate_address(address),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    claim["signature"] = sign_payload(private_key_hex, DOMAIN_WALLET, _canonical(claim))
    return claim


def verify_wallet_claim(
    claim: object,
    *,
    expected_agent_id: str | None = None,
    network: str | None = None,
) -> dict:
    if not isinstance(claim, dict):
        raise ValueError("Wallet claim must be an object")
    if claim.get("version") != WALLET_CLAIM_VERSION:
        raise ValueError(f"Unsupported wallet claim version: {claim.get('version')}")
    agent_id = claim.get("agent_id")
    if not isinstance(agent_id, str) or len(agent_id) != 64:
        raise ValueError("Wallet claim agent_id must be a 32-byte public key")
    try:
        bytes.fromhex(agent_id)
    except ValueError as exc:
        raise ValueError("Wallet claim agent_id is not hexadecimal") from exc
    if expected_agent_id and agent_id != expected_agent_id:
        raise ValueError("Wallet claim belongs to a different agent")
    if claim.get("chain") != "solana":
        raise ValueError("Wallet claim chain must be solana")
    selected = normalize_network(claim.get("network"))
    if network and selected != normalize_network(network):
        raise ValueError("Wallet claim is for a different Solana network")
    address = _validate_address(claim.get("address"))
    created_at = claim.get("created_at")
    if not isinstance(created_at, str):
        raise ValueError("Wallet claim created_at is required")
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Wallet claim created_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("Wallet claim created_at must include a timezone")
    signature = claim.get("signature")
    if not isinstance(signature, str) or not verify_signed_payload(
        agent_id,
        signature,
        DOMAIN_WALLET,
        _canonical(claim),
    ):
        raise ValueError("Invalid wallet claim signature")
    return {
        "version": WALLET_CLAIM_VERSION,
        "agent_id": agent_id,
        "chain": "solana",
        "network": selected,
        "address": address,
        "created_at": parsed.isoformat(),
        "signature": signature,
    }


__all__ = [
    "DOMAIN_WALLET",
    "WALLET_CLAIM_VERSION",
    "create_wallet_claim",
    "verify_wallet_claim",
]

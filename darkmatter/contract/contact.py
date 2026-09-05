"""Portable, signed contact cards used for out-of-band first contact."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from darkmatter.security import sign_payload, verify_signed_payload
from darkmatter.contract.tenure import verify_passport_claim

DOMAIN_CONTACT = "darkmatter.contact.v3"
CONTACT_VERSION = 4


def validate_locator(locator: str) -> str:
    if not isinstance(locator, str) or not locator.strip():
        raise ValueError("Contact card locator is required")
    locator = locator.strip()
    if locator.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in locator):
        raise ValueError("Mailbox locator contains options or control characters")
    if "::" in locator and "://" not in locator:
        raise ValueError("Git remote helpers are not mailbox locators")
    parsed = urlsplit(locator)
    if parsed.scheme and parsed.scheme not in ("http", "https", "ssh"):
        raise ValueError("Unsupported mailbox locator scheme")
    if parsed.scheme in ("http", "https", "ssh") and (not parsed.hostname or parsed.hostname.startswith("-")):
        raise ValueError("Mailbox locator requires a valid host")
    if parsed.scheme in ("http", "https") and "@" in parsed.netloc:
        raise ValueError("Mailbox locators must not contain embedded HTTP credentials")
    return locator


def _payload(card: dict) -> dict:
    payload = {
        "version": card.get("version"),
        "agent_id": card.get("agent_id"),
        "locator": card.get("locator"),
        "display_name": card.get("display_name", ""),
        "bio": card.get("bio", ""),
    }
    if card.get("version") == CONTACT_VERSION:
        payload["passport"] = card.get("passport")
    return payload


def _canonical(card: dict) -> str:
    return json.dumps(_payload(card), sort_keys=True, separators=(",", ":"))


def create_contact_card(
    private_key_hex: str,
    agent_id: str,
    locator: str,
    *,
    display_name: str = "",
    bio: str = "",
    passport: dict,
) -> dict:
    locator = validate_locator(locator)
    card = {
        "version": CONTACT_VERSION,
        "agent_id": agent_id,
        "locator": locator,
        "display_name": display_name,
        "bio": bio,
        "passport": verify_passport_claim(passport, agent_id),
    }
    card["signature"] = sign_payload(private_key_hex, DOMAIN_CONTACT, _canonical(card))
    return card


def verify_contact_card(card: dict) -> dict:
    if not isinstance(card, dict):
        raise ValueError("Contact card must be an object")
    payload = _payload(card)
    if payload["version"] not in (3, CONTACT_VERSION):
        raise ValueError(f"Unsupported contact card version: {payload['version']}")
    agent_id = payload["agent_id"]
    if not isinstance(agent_id, str) or len(agent_id) != 64:
        raise ValueError("Contact card agent_id must be a 32-byte public key")
    try:
        bytes.fromhex(agent_id)
    except ValueError as exc:
        raise ValueError("Contact card agent_id is not hexadecimal") from exc
    payload["locator"] = validate_locator(payload["locator"])
    if payload["version"] == CONTACT_VERSION:
        payload["passport"] = verify_passport_claim(payload.get("passport"), agent_id)
    signature = card.get("signature")
    if not isinstance(signature, str) or not verify_signed_payload(
        agent_id, signature, DOMAIN_CONTACT, _canonical(card),
    ):
        raise ValueError("Invalid contact card signature")
    return {**payload, "signature": signature}


__all__ = [
    "CONTACT_VERSION",
    "DOMAIN_CONTACT",
    "create_contact_card",
    "validate_locator",
    "verify_contact_card",
]

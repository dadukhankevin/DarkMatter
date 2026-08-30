"""Transferable message provenance and bounded forwarding chains."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional

from darkmatter.contract.envelope import is_expired_at, verify_envelope_signature
from darkmatter.identity import derive_public_key_hex
from darkmatter.security import sign_payload, verify_signed_payload


MESSAGE_RECORD_VERSION = 1
FORWARD_VERSION = 1
MAX_FORWARD_HOPS = 10
DOMAIN_MESSAGE_RECORD = "darkmatter.message-record.v1"
DOMAIN_FORWARD_HOP = "darkmatter.forward-hop.v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: dict) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _public_key(value, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a 32-byte public key")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not hexadecimal") from exc
    return value.lower()


def _timestamp(value, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return value


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _message_payload(record: dict) -> dict:
    metadata = record.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("message metadata must be an object")
    content = record.get("content")
    if not isinstance(content, str) or not content:
        raise ValueError("message content is required")
    envelope_id = record.get("envelope_id")
    if not isinstance(envelope_id, str) or not envelope_id:
        raise ValueError("message envelope_id is required")
    expires_at = record.get("expires_at") or ""
    if expires_at:
        _timestamp(expires_at, "message expires_at")
    return {
        "version": record.get("version"),
        "envelope_id": envelope_id,
        "from": _public_key(record.get("from"), "message from"),
        "to": _public_key(record.get("to"), "message to"),
        "timestamp": _timestamp(record.get("timestamp"), "message timestamp"),
        "expires_at": expires_at,
        "content": content,
        "metadata": deepcopy(metadata),
    }


def create_message_record(
    private_key_hex: str,
    from_id: str,
    to_id: str,
    envelope_id: str,
    timestamp: str,
    content: str,
    *,
    metadata: Optional[dict] = None,
    expires_at: Optional[str] = None,
) -> dict:
    """Sign plaintext provenance that remains verifiable after forwarding."""
    if derive_public_key_hex(private_key_hex) != from_id:
        raise ValueError("message from does not match the signing passport")
    record = _message_payload({
        "version": MESSAGE_RECORD_VERSION,
        "envelope_id": envelope_id,
        "from": from_id,
        "to": to_id,
        "timestamp": timestamp,
        "expires_at": expires_at or "",
        "content": content,
        "metadata": metadata or {},
    })
    record["signature"] = sign_payload(
        private_key_hex, DOMAIN_MESSAGE_RECORD, _canonical(record),
    )
    return record


def verify_message_record(record: dict, original_envelope: Optional[dict] = None) -> dict:
    """Verify a transferable message record and, when supplied, its envelope link."""
    if not isinstance(record, dict):
        raise ValueError("message record must be an object")
    payload = _message_payload(record)
    if payload["version"] != MESSAGE_RECORD_VERSION:
        raise ValueError(f"unsupported message record version: {payload['version']}")
    signature = record.get("signature")
    if not isinstance(signature, str) or not verify_signed_payload(
        payload["from"], signature, DOMAIN_MESSAGE_RECORD, _canonical(payload),
    ):
        raise ValueError("invalid message record signature")
    verified = {**payload, "signature": signature}
    if original_envelope is not None:
        envelope = verify_envelope_signature(original_envelope).to_public_dict()
        expected = {
            "envelope_id": envelope["id"],
            "from": envelope["from"],
            "to": envelope["to"],
            "timestamp": envelope["timestamp"],
            "expires_at": envelope.get("expires_at", ""),
        }
        for key, value in expected.items():
            if verified[key] != value:
                raise ValueError(f"message record {key} does not match original envelope")
        if envelope["type"] != "message":
            raise ValueError("only original message envelopes may be forwarded")
    return verified


def _hop_payload(hop: dict) -> dict:
    note = hop.get("note", "")
    if not isinstance(note, str):
        raise ValueError("forward note must be a string")
    previous = hop.get("previous")
    if not isinstance(previous, str) or len(previous) != 64:
        raise ValueError("forward previous digest is invalid")
    try:
        bytes.fromhex(previous)
    except ValueError as exc:
        raise ValueError("forward previous digest is invalid") from exc
    remaining = hop.get("hops_remaining")
    if isinstance(remaining, bool) or not isinstance(remaining, int):
        raise ValueError("forward hops_remaining must be an integer")
    if remaining < 0 or remaining >= MAX_FORWARD_HOPS:
        raise ValueError("forward hops_remaining is out of range")
    expires_at = _timestamp(hop.get("expires_at"), "forward expires_at")
    return {
        "version": hop.get("version"),
        "original_id": str(hop.get("original_id") or ""),
        "from": _public_key(hop.get("from"), "forward from"),
        "to": _public_key(hop.get("to"), "forward to"),
        "timestamp": _timestamp(hop.get("timestamp"), "forward timestamp"),
        "expires_at": expires_at,
        "hops_remaining": remaining,
        "previous": previous.lower(),
        "note": note,
    }


def _signed_hop(private_key_hex: str, payload: dict) -> dict:
    hop = _hop_payload(payload)
    if derive_public_key_hex(private_key_hex) != hop["from"]:
        raise ValueError("forward from does not match the signing passport")
    hop["signature"] = sign_payload(
        private_key_hex, DOMAIN_FORWARD_HOP, _canonical(hop),
    )
    return hop


def _verified_hop(hop: dict) -> dict:
    if not isinstance(hop, dict):
        raise ValueError("forward hop must be an object")
    payload = _hop_payload(hop)
    if payload["version"] != FORWARD_VERSION:
        raise ValueError(f"unsupported forward hop version: {payload['version']}")
    signature = hop.get("signature")
    if not isinstance(signature, str) or not verify_signed_payload(
        payload["from"], signature, DOMAIN_FORWARD_HOP, _canonical(payload),
    ):
        raise ValueError("invalid forward hop signature")
    return {**payload, "signature": signature}


def create_forward_package(
    private_key_hex: str,
    from_id: str,
    to_id: str,
    original_envelope: dict,
    message_record: dict,
    *,
    path: Optional[list[dict]] = None,
    note: str = "",
    max_hops: int = 3,
    expires_at: str,
) -> dict:
    """Append one explicit, signed hop to an original message provenance chain."""
    original = verify_envelope_signature(original_envelope).to_public_dict()
    message = verify_message_record(message_record, original)
    verified_path = _verify_path(original, message, path or [])
    if verified_path:
        previous_hop = verified_path[-1]
        if previous_hop["to"] != from_id:
            raise ValueError("only the current forward recipient may append a hop")
        if previous_hop["hops_remaining"] <= 0:
            raise ValueError("forward hop limit reached")
        remaining = previous_hop["hops_remaining"] - 1
        previous = _digest(previous_hop)
        if _parse_timestamp(expires_at) > _parse_timestamp(previous_hop["expires_at"]):
            raise ValueError("a forwarded message cannot extend its previous expiry")
    else:
        if original["to"] != from_id:
            raise ValueError("only the original recipient may begin forwarding")
        if isinstance(max_hops, bool) or not isinstance(max_hops, int):
            raise ValueError("max_hops must be an integer")
        if max_hops < 1 or max_hops > MAX_FORWARD_HOPS:
            raise ValueError(f"max_hops must be between 1 and {MAX_FORWARD_HOPS}")
        remaining = max_hops - 1
        previous = _digest(message)
    if original.get("expires_at") and _parse_timestamp(expires_at) > _parse_timestamp(
        original["expires_at"],
    ):
        raise ValueError("a forwarded message cannot extend its original expiry")
    if is_expired_at(expires_at):
        raise ValueError("forward expiry must be in the future")
    hop = _signed_hop(private_key_hex, {
        "version": FORWARD_VERSION,
        "original_id": original["id"],
        "from": from_id,
        "to": to_id,
        "timestamp": _now(),
        "expires_at": expires_at,
        "hops_remaining": remaining,
        "previous": previous,
        "note": note,
    })
    return {
        "version": FORWARD_VERSION,
        "original_envelope": original,
        "message": message,
        "path": verified_path + [hop],
    }


def _verify_path(original: dict, message: dict, path: list[dict]) -> list[dict]:
    if not isinstance(path, list) or len(path) > MAX_FORWARD_HOPS:
        raise ValueError("forward path is invalid")
    expected_from = original["to"]
    expected_previous = _digest(message)
    previous_remaining = None
    previous_expiry = original.get("expires_at") or None
    verified = []
    for raw in path:
        hop = _verified_hop(raw)
        if hop["original_id"] != original["id"]:
            raise ValueError("forward hop references a different original message")
        if hop["from"] != expected_from:
            raise ValueError("forward path is not contiguous")
        if hop["previous"] != expected_previous:
            raise ValueError("forward chain digest mismatch")
        if previous_remaining is not None and hop["hops_remaining"] != previous_remaining - 1:
            raise ValueError("forward hop count did not decrement")
        if previous_expiry and _parse_timestamp(hop["expires_at"]) > _parse_timestamp(previous_expiry):
            raise ValueError("forward hop extends an earlier expiry")
        expected_from = hop["to"]
        expected_previous = _digest(hop)
        previous_remaining = hop["hops_remaining"]
        previous_expiry = hop["expires_at"]
        verified.append(hop)
    return verified


def verify_forward_package(
    package: dict,
    *,
    envelope_from: Optional[str] = None,
    envelope_to: Optional[str] = None,
    envelope_expires_at: Optional[str] = None,
) -> dict:
    """Verify the original message and every forwarding decision in order."""
    if not isinstance(package, dict) or package.get("version") != FORWARD_VERSION:
        raise ValueError("unsupported forward package")
    original = verify_envelope_signature(package.get("original_envelope")).to_public_dict()
    message = verify_message_record(package.get("message"), original)
    path = _verify_path(original, message, package.get("path"))
    if not path:
        raise ValueError("forward path is empty")
    last = path[-1]
    if envelope_from and last["from"] != envelope_from:
        raise ValueError("outer envelope sender does not match final forward hop")
    if envelope_to and last["to"] != envelope_to:
        raise ValueError("outer envelope recipient does not match final forward hop")
    if envelope_expires_at != last["expires_at"]:
        raise ValueError("outer envelope expiry does not match final forward hop")
    if is_expired_at(last["expires_at"]):
        raise ValueError("forward package has expired")
    return {
        "version": FORWARD_VERSION,
        "original_envelope": original,
        "message": message,
        "path": path,
    }


__all__ = [
    "DOMAIN_FORWARD_HOP",
    "DOMAIN_MESSAGE_RECORD",
    "FORWARD_VERSION",
    "MAX_FORWARD_HOPS",
    "MESSAGE_RECORD_VERSION",
    "create_forward_package",
    "create_message_record",
    "verify_forward_package",
    "verify_message_record",
]

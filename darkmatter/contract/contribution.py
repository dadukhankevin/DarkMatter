"""Public, signed AntiMatter contribution routing proofs.

Every route decision is attributable, strictly moves toward an older passport,
and includes the router's local observation that the next agent was recently
active.  The proof is portable JSON: no global trust score or central service is
needed to verify what happened.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from darkmatter.contract.envelope import is_expired_at
from darkmatter.contract.liveness import verify_liveness_claim
from darkmatter.contract.tenure import parse_timestamp, verify_passport_claim
from darkmatter.identity import derive_public_key_hex
from darkmatter.security import sign_payload, verify_signed_payload


CONTRIBUTION_VERSION = 1
CONTRIBUTION_PROTOCOL = "antimatter/contribution/1"
CONTRIBUTION_RATE = Decimal("0.01")
MAX_CONTRIBUTION_HOPS = 42
DEFAULT_LIVENESS_WINDOW_SECONDS = 7 * 24 * 60 * 60
MAX_LIVENESS_WINDOW_SECONDS = 30 * 24 * 60 * 60

DOMAIN_TICKET = "darkmatter.antimatter-ticket.v1"
DOMAIN_SOURCE_RECEIPT = "darkmatter.antimatter-source-receipt.v1"
DOMAIN_ROUTE_HOP = "darkmatter.antimatter-route-hop.v1"
DOMAIN_RESOLUTION = "darkmatter.antimatter-resolution.v1"
DOMAIN_FULFILLMENT = "darkmatter.antimatter-fulfillment.v1"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: dict) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _json_object(value, name: str, maximum: int = 32_768) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    try:
        encoded = _canonical(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON-serializable") from exc
    if len(encoded) > maximum:
        raise ValueError(f"{name} exceeds {maximum} bytes")
    return deepcopy(value)


def _text(value, name: str, maximum: int = 512, *, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return value


def _identifier(value, name: str) -> str:
    value = _text(value, name, 128)
    if not _ID.fullmatch(value):
        raise ValueError(f"{name} contains unsupported characters")
    return value


def _public_key(value, name: str) -> str:
    value = _text(value, name, 64)
    if len(value) != 64:
        raise ValueError(f"{name} must be a 32-byte public key")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not hexadecimal") from exc
    return value.lower()


def _amount(value, name: str) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} is required")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if len(rendered) > 128:
        raise ValueError(f"{name} is too large")
    return rendered


def contribution_amount(source_amount) -> str:
    return _amount(Decimal(_amount(source_amount, "source amount")) * CONTRIBUTION_RATE,
                   "contribution amount")


def new_contribution_id() -> str:
    return f"amc-{uuid.uuid4().hex}"


def _source(value: dict) -> dict:
    value = _json_object(value, "source")
    payer = _public_key(value.get("payer_id"), "source payer_id")
    payee = _public_key(value.get("payee_id"), "source payee_id")
    if payer == payee:
        raise ValueError("source payer and payee must differ")
    source = {
        "settlement_id": _identifier(value.get("settlement_id"), "source settlement_id"),
        "payer_id": payer,
        "payee_id": payee,
        "receipt_id": _text(value.get("receipt_id"), "source receipt_id", 128),
        "transaction_id": _text(value.get("transaction_id"), "source transaction_id", 512),
        "amount": _amount(value.get("amount"), "source amount"),
        "currency": _text(value.get("currency"), "source currency", 64),
        "rail": _text(value.get("rail"), "source rail", 128),
    }
    attestation = verify_source_receipt(value.get("receipt_attestation"))
    expected = {
        "settlement_id": source["settlement_id"],
        "payer_id": source["payer_id"],
        "payee_id": source["payee_id"],
        "receipt_id": source["receipt_id"],
        "transaction_id": source["transaction_id"],
        "amount": source["amount"],
        "currency": source["currency"],
        "rail": source["rail"],
    }
    for key, expected_value in expected.items():
        if attestation[key] != expected_value:
            raise ValueError(f"source receipt attestation {key} does not match")
    source["receipt_attestation"] = attestation
    return source


def _source_receipt_payload(receipt: dict) -> dict:
    if not isinstance(receipt, dict):
        raise ValueError("source receipt attestation must be an object")
    return {
        "version": receipt.get("version"),
        "protocol": receipt.get("protocol"),
        "settlement_id": _identifier(
            receipt.get("settlement_id"), "source receipt settlement_id",
        ),
        "payer_id": _public_key(receipt.get("payer_id"), "source receipt payer_id"),
        "payee_id": _public_key(receipt.get("payee_id"), "source receipt payee_id"),
        "receipt_id": _text(receipt.get("receipt_id"), "source receipt receipt_id", 128),
        "timestamp": parse_timestamp(
            receipt.get("timestamp"), "source receipt timestamp",
        ).isoformat(),
        "transaction_id": _text(
            receipt.get("transaction_id"), "source receipt transaction_id", 512,
        ),
        "amount": _amount(receipt.get("amount"), "source receipt amount"),
        "currency": _text(receipt.get("currency"), "source receipt currency", 64),
        "rail": _text(receipt.get("rail"), "source receipt rail", 128),
    }


def create_source_receipt(
    private_key_hex: str,
    *,
    payer_id: str,
    payee_id: str,
    settlement_id: str,
    receipt_id: str,
    timestamp: str,
    transaction_id: str,
    amount,
    currency: str,
    rail: str,
) -> dict:
    """Create the payer's portable attribution of the source transaction."""
    if derive_public_key_hex(private_key_hex) != payer_id:
        raise ValueError("source receipt payer does not match the signing passport")
    payload = _source_receipt_payload({
        "version": CONTRIBUTION_VERSION,
        "protocol": CONTRIBUTION_PROTOCOL,
        "settlement_id": settlement_id,
        "payer_id": payer_id,
        "payee_id": payee_id,
        "receipt_id": receipt_id,
        "timestamp": timestamp,
        "transaction_id": transaction_id,
        "amount": amount,
        "currency": currency,
        "rail": rail,
    })
    payload["signature"] = sign_payload(
        private_key_hex, DOMAIN_SOURCE_RECEIPT, _canonical(payload),
    )
    return payload


def verify_source_receipt(receipt: dict) -> dict:
    payload = _source_receipt_payload(receipt)
    if (
        payload["version"] != CONTRIBUTION_VERSION
        or payload["protocol"] != CONTRIBUTION_PROTOCOL
    ):
        raise ValueError("unsupported source receipt attestation")
    if payload["payer_id"] == payload["payee_id"]:
        raise ValueError("source receipt payer and payee must differ")
    signature = receipt.get("signature")
    if not isinstance(signature, str) or not verify_signed_payload(
        payload["payer_id"], signature, DOMAIN_SOURCE_RECEIPT, _canonical(payload),
    ):
        raise ValueError("invalid source receipt attestation signature")
    return {**payload, "signature": signature}


def _contribution(value: dict, source: dict) -> dict:
    value = _json_object(value, "contribution")
    rate = _amount(value.get("rate"), "contribution rate")
    if Decimal(rate) != CONTRIBUTION_RATE:
        raise ValueError("AntiMatter contribution rate must be exactly 0.01")
    amount = _amount(value.get("amount"), "contribution amount")
    if Decimal(amount) != Decimal(source["amount"]) * CONTRIBUTION_RATE:
        raise ValueError("contribution amount is not exactly 1% of the source amount")
    if value.get("currency") != source["currency"] or value.get("rail") != source["rail"]:
        raise ValueError("contribution currency and rail must match the source")
    return {
        "rate": format(CONTRIBUTION_RATE, "f"),
        "amount": amount,
        "currency": source["currency"],
        "rail": source["rail"],
    }


def _ticket_payload(ticket: dict) -> dict:
    if not isinstance(ticket, dict):
        raise ValueError("contribution ticket must be an object")
    source = _source(ticket.get("source"))
    origin = _public_key(ticket.get("origin_id"), "ticket origin_id")
    if origin != source["payee_id"]:
        raise ValueError("the source payee must originate the contribution")
    created = parse_timestamp(ticket.get("created_at"), "ticket created_at")
    expires = parse_timestamp(ticket.get("expires_at"), "ticket expires_at")
    created_at = created.isoformat()
    expires_at = expires.isoformat()
    if created > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("ticket created_at is implausibly far in the future")
    if expires <= created:
        raise ValueError("ticket expiry must be after creation")
    if expires - created > timedelta(seconds=MAX_LIVENESS_WINDOW_SECONDS):
        raise ValueError("ticket lifetime exceeds 30 days")
    max_hops = ticket.get("max_hops")
    if isinstance(max_hops, bool) or not isinstance(max_hops, int):
        raise ValueError("ticket max_hops must be an integer")
    if max_hops < 1 or max_hops > MAX_CONTRIBUTION_HOPS:
        raise ValueError(f"ticket max_hops must be between 1 and {MAX_CONTRIBUTION_HOPS}")
    window = ticket.get("liveness_window_seconds")
    if isinstance(window, bool) or not isinstance(window, int):
        raise ValueError("ticket liveness_window_seconds must be an integer")
    if window < 60 or window > MAX_LIVENESS_WINDOW_SECONDS:
        raise ValueError(
            f"ticket liveness_window_seconds must be between 60 and {MAX_LIVENESS_WINDOW_SECONDS}",
        )
    return {
        "version": ticket.get("version"),
        "protocol": ticket.get("protocol"),
        "contribution_id": _identifier(ticket.get("contribution_id"), "contribution_id"),
        "origin_id": origin,
        "created_at": created_at,
        "expires_at": expires_at,
        "max_hops": max_hops,
        "liveness_window_seconds": window,
        "source": source,
        "contribution": _contribution(ticket.get("contribution"), source),
        "rationale": _text(ticket.get("rationale"), "ticket rationale", 2000),
    }


def create_contribution_ticket(
    private_key_hex: str,
    origin_id: str,
    source: dict,
    *,
    contribution_id: Optional[str] = None,
    max_hops: int = MAX_CONTRIBUTION_HOPS,
    ttl_seconds: int = 7 * 24 * 60 * 60,
    liveness_window_seconds: int = DEFAULT_LIVENESS_WINDOW_SECONDS,
) -> dict:
    if derive_public_key_hex(private_key_hex) != origin_id:
        raise ValueError("ticket origin does not match the signing passport")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
        raise ValueError("ttl_seconds must be an integer")
    if ttl_seconds < 60 or ttl_seconds > MAX_LIVENESS_WINDOW_SECONDS:
        raise ValueError(
            f"ttl_seconds must be between 60 and {MAX_LIVENESS_WINDOW_SECONDS}",
        )
    created = datetime.now(timezone.utc)
    source = _source(source)
    payload = _ticket_payload({
        "version": CONTRIBUTION_VERSION,
        "protocol": CONTRIBUTION_PROTOCOL,
        "contribution_id": contribution_id or new_contribution_id(),
        "origin_id": origin_id,
        "created_at": created.isoformat(),
        "expires_at": (created + timedelta(seconds=ttl_seconds)).isoformat(),
        "max_hops": max_hops,
        "liveness_window_seconds": liveness_window_seconds,
        "source": source,
        "contribution": {
            "rate": format(CONTRIBUTION_RATE, "f"),
            "amount": contribution_amount(source["amount"]),
            "currency": source["currency"],
            "rail": source["rail"],
        },
        "rationale": (
            "Reward an older, currently active agent for sustaining the shared network. "
            "Participation is voluntary; this signed record makes the choice inspectable."
        ),
    })
    payload["signature"] = sign_payload(private_key_hex, DOMAIN_TICKET, _canonical(payload))
    return payload


def verify_contribution_ticket(ticket: dict, *, require_unexpired: bool = False) -> dict:
    payload = _ticket_payload(ticket)
    if payload["version"] != CONTRIBUTION_VERSION or payload["protocol"] != CONTRIBUTION_PROTOCOL:
        raise ValueError("unsupported contribution ticket protocol")
    signature = ticket.get("signature")
    if not isinstance(signature, str) or not verify_signed_payload(
        payload["origin_id"], signature, DOMAIN_TICKET, _canonical(payload),
    ):
        raise ValueError("invalid contribution ticket signature")
    if require_unexpired and is_expired_at(payload["expires_at"]):
        raise ValueError("contribution ticket has expired")
    return {**payload, "signature": signature}


def _hop_payload(hop: dict) -> dict:
    if not isinstance(hop, dict):
        raise ValueError("contribution route hop must be an object")
    from_id = _public_key(hop.get("from"), "route from")
    to_id = _public_key(hop.get("to"), "route to")
    from_passport = verify_passport_claim(hop.get("from_passport"), from_id)
    to_passport = verify_passport_claim(hop.get("to_passport"), to_id)
    if parse_timestamp(to_passport["created_at"], "to passport created_at") >= parse_timestamp(
        from_passport["created_at"], "from passport created_at",
    ):
        raise ValueError("every contribution hop must move to an older passport")
    index = hop.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("route index must be a non-negative integer")
    previous = _text(hop.get("previous"), "route previous digest", 64)
    if len(previous) != 64:
        raise ValueError("route previous digest is invalid")
    try:
        bytes.fromhex(previous)
    except ValueError as exc:
        raise ValueError("route previous digest is invalid") from exc
    timestamp = parse_timestamp(hop.get("timestamp"), "route timestamp").isoformat()
    observed = parse_timestamp(hop.get("observed_active_at"), "route observed_active_at").isoformat()
    liveness = verify_liveness_claim(hop.get("liveness"), to_id)
    if liveness["timestamp"] != observed:
        raise ValueError("route liveness claim does not match observed_active_at")
    relationship_since = parse_timestamp(
        hop.get("relationship_since"), "route relationship_since",
    ).isoformat()
    return {
        "version": hop.get("version"),
        "contribution_id": _identifier(hop.get("contribution_id"), "route contribution_id"),
        "index": index,
        "from": from_id,
        "to": to_id,
        "from_passport": from_passport,
        "to_passport": to_passport,
        "timestamp": timestamp,
        "observed_active_at": observed,
        "liveness": liveness,
        "relationship_since": relationship_since,
        "previous": previous.lower(),
        "decision": _text(hop.get("decision"), "route decision", 128),
    }


def _verify_hop(hop: dict) -> dict:
    payload = _hop_payload(hop)
    if payload["version"] != CONTRIBUTION_VERSION:
        raise ValueError("unsupported contribution route hop version")
    signature = hop.get("signature")
    if not isinstance(signature, str) or not verify_signed_payload(
        payload["from"], signature, DOMAIN_ROUTE_HOP, _canonical(payload),
    ):
        raise ValueError("invalid contribution route hop signature")
    return {**payload, "signature": signature}


def _verify_path(ticket: dict, path: list[dict]) -> list[dict]:
    if not isinstance(path, list) or len(path) > ticket["max_hops"]:
        raise ValueError("contribution route path is invalid")
    expected_from = ticket["origin_id"]
    expected_previous = _digest(ticket)
    previous_timestamp = parse_timestamp(ticket["created_at"], "ticket created_at")
    seen = {expected_from, ticket["source"]["payer_id"]}
    verified = []
    for index, raw in enumerate(path):
        hop = _verify_hop(raw)
        if hop["contribution_id"] != ticket["contribution_id"]:
            raise ValueError("route hop references a different contribution")
        if hop["index"] != index or hop["from"] != expected_from:
            raise ValueError("contribution route path is not contiguous")
        if hop["previous"] != expected_previous:
            raise ValueError("contribution route digest chain is broken")
        if hop["to"] in seen:
            raise ValueError("contribution route repeats an identity")
        timestamp = parse_timestamp(hop["timestamp"], "route timestamp")
        observed = parse_timestamp(hop["observed_active_at"], "route observed_active_at")
        relationship_since = parse_timestamp(hop["relationship_since"], "route relationship_since")
        if observed > timestamp:
            raise ValueError("route liveness observation is after the routing decision")
        if timestamp < previous_timestamp:
            raise ValueError("route timestamps are not chronological")
        if timestamp - observed > timedelta(seconds=ticket["liveness_window_seconds"]):
            raise ValueError("route target was not observed within the liveness window")
        if relationship_since > timestamp:
            raise ValueError("route relationship began after the routing decision")
        if timestamp > parse_timestamp(ticket["expires_at"], "ticket expires_at"):
            raise ValueError("route hop was created after ticket expiry")
        expected_from = hop["to"]
        expected_previous = _digest(hop)
        previous_timestamp = timestamp
        seen.add(hop["to"])
        verified.append(hop)
    return verified


def append_contribution_hop(
    private_key_hex: str,
    package: dict,
    *,
    from_passport: dict,
    to_passport: dict,
    observed_active_at: str,
    liveness: dict,
    relationship_since: str,
) -> dict:
    verified = verify_contribution_package(package, require_unexpired=True)
    if verified.get("resolution"):
        raise ValueError("a resolved contribution cannot be routed again")
    ticket = verified["ticket"]
    path = verified["path"]
    from_id = verify_passport_claim(from_passport)["agent_id"]
    to_id = verify_passport_claim(to_passport)["agent_id"]
    if derive_public_key_hex(private_key_hex) != from_id:
        raise ValueError("route signer does not match route from")
    expected = path[-1]["to"] if path else ticket["origin_id"]
    if from_id != expected:
        raise ValueError("only the current route recipient may append a hop")
    if len(path) >= ticket["max_hops"]:
        raise ValueError("contribution route hop limit reached")
    timestamp = _now()
    payload = _hop_payload({
        "version": CONTRIBUTION_VERSION,
        "contribution_id": ticket["contribution_id"],
        "index": len(path),
        "from": from_id,
        "to": to_id,
        "from_passport": from_passport,
        "to_passport": to_passport,
        "timestamp": timestamp,
        "observed_active_at": observed_active_at,
        "liveness": liveness,
        "relationship_since": relationship_since,
        "previous": _digest(path[-1]) if path else _digest(ticket),
        "decision": "older_recently_observed_relationship",
    })
    payload["signature"] = sign_payload(
        private_key_hex, DOMAIN_ROUTE_HOP, _canonical(payload),
    )
    next_package = {**verified, "path": path + [payload]}
    return verify_contribution_package(next_package, require_unexpired=True)


_RESOLUTION_REASONS = frozenset({
    "no_older_live_relationship",
    "max_hops",
    "voluntary_acceptance",
    "declined",
})


def _resolution_payload(resolution: dict) -> dict:
    if not isinstance(resolution, dict):
        raise ValueError("contribution resolution must be an object")
    beneficiary = resolution.get("beneficiary")
    if beneficiary is not None:
        beneficiary = verify_passport_claim(beneficiary)
    reason = _text(resolution.get("reason"), "resolution reason", 64)
    if reason not in _RESOLUTION_REASONS:
        raise ValueError("unsupported contribution resolution reason")
    return {
        "version": resolution.get("version"),
        "contribution_id": _identifier(
            resolution.get("contribution_id"), "resolution contribution_id",
        ),
        "ticket_digest": _text(resolution.get("ticket_digest"), "resolution ticket digest", 64),
        "path_digest": _text(resolution.get("path_digest"), "resolution path digest", 64),
        "resolver_id": _public_key(resolution.get("resolver_id"), "resolution resolver_id"),
        "beneficiary": beneficiary,
        "reason": reason,
        "destination": _json_object(resolution.get("destination"), "resolution destination"),
        "timestamp": parse_timestamp(resolution.get("timestamp"), "resolution timestamp").isoformat(),
    }


def _verify_resolution(ticket: dict, path: list[dict], resolution: dict) -> dict:
    payload = _resolution_payload(resolution)
    if payload["version"] != CONTRIBUTION_VERSION:
        raise ValueError("unsupported contribution resolution version")
    if payload["contribution_id"] != ticket["contribution_id"]:
        raise ValueError("resolution references a different contribution")
    if payload["ticket_digest"] != _digest(ticket) or payload["path_digest"] != _digest({"path": path}):
        raise ValueError("resolution does not bind this ticket and route")
    expected_resolver = path[-1]["to"] if path else ticket["origin_id"]
    if payload["resolver_id"] != expected_resolver:
        raise ValueError("only the current route recipient may resolve")
    resolution_timestamp = parse_timestamp(payload["timestamp"], "resolution timestamp")
    earliest = parse_timestamp(
        path[-1]["timestamp"] if path else ticket["created_at"],
        "route completion timestamp",
    )
    if resolution_timestamp < earliest:
        raise ValueError("resolution predates the route it resolves")
    if resolution_timestamp > parse_timestamp(ticket["expires_at"], "ticket expires_at"):
        raise ValueError("resolution was created after ticket expiry")
    beneficiary = payload["beneficiary"]
    if path:
        if payload["reason"] == "declined":
            if beneficiary is not None:
                raise ValueError("a declined contribution cannot name a beneficiary")
        elif beneficiary is None or beneficiary["agent_id"] != expected_resolver:
            raise ValueError("the final route recipient must be the beneficiary")
        if payload["reason"] == "max_hops" and len(path) != ticket["max_hops"]:
            raise ValueError("max_hops resolution used before the hop limit")
    elif beneficiary is not None or payload["reason"] != "no_older_live_relationship":
        raise ValueError("an unroutable origin must publish a no-beneficiary resolution")
    signature = resolution.get("signature")
    if not isinstance(signature, str) or not verify_signed_payload(
        payload["resolver_id"], signature, DOMAIN_RESOLUTION, _canonical(payload),
    ):
        raise ValueError("invalid contribution resolution signature")
    return {**payload, "signature": signature}


def resolve_contribution(
    private_key_hex: str,
    package: dict,
    *,
    passport: dict,
    reason: str,
    destination: Optional[dict] = None,
) -> dict:
    verified = verify_contribution_package(package, require_unexpired=True)
    if verified.get("resolution"):
        raise ValueError("contribution is already resolved")
    ticket, path = verified["ticket"], verified["path"]
    resolver_id = derive_public_key_hex(private_key_hex)
    expected = path[-1]["to"] if path else ticket["origin_id"]
    if resolver_id != expected:
        raise ValueError("only the current route recipient may resolve")
    passport = verify_passport_claim(passport, resolver_id)
    payload = _resolution_payload({
        "version": CONTRIBUTION_VERSION,
        "contribution_id": ticket["contribution_id"],
        "ticket_digest": _digest(ticket),
        "path_digest": _digest({"path": path}),
        "resolver_id": resolver_id,
        "beneficiary": passport if path and reason != "declined" else None,
        "reason": reason,
        "destination": destination or {},
        "timestamp": _now(),
    })
    payload["signature"] = sign_payload(
        private_key_hex, DOMAIN_RESOLUTION, _canonical(payload),
    )
    return verify_contribution_package({**verified, "resolution": payload})


def _fulfillment_payload(fulfillment: dict) -> dict:
    if not isinstance(fulfillment, dict):
        raise ValueError("contribution fulfillment must be an object")
    return {
        "version": fulfillment.get("version"),
        "contribution_id": _identifier(
            fulfillment.get("contribution_id"), "fulfillment contribution_id",
        ),
        "origin_id": _public_key(fulfillment.get("origin_id"), "fulfillment origin_id"),
        "resolution_digest": _text(
            fulfillment.get("resolution_digest"), "fulfillment resolution digest", 64,
        ),
        "transaction_id": _text(fulfillment.get("transaction_id"), "fulfillment transaction_id", 512),
        "proof": _json_object(fulfillment.get("proof"), "fulfillment proof"),
        "timestamp": parse_timestamp(fulfillment.get("timestamp"), "fulfillment timestamp").isoformat(),
    }


def fulfill_contribution(
    private_key_hex: str,
    package: dict,
    transaction_id: str,
    proof: Optional[dict] = None,
) -> dict:
    verified = verify_contribution_package(package)
    ticket = verified["ticket"]
    if derive_public_key_hex(private_key_hex) != ticket["origin_id"]:
        raise ValueError("only the contribution origin may publish fulfillment")
    if not verified.get("resolution") or verified["resolution"].get("beneficiary") is None:
        raise ValueError("an unresolved or unroutable contribution cannot be fulfilled")
    if verified.get("fulfillment"):
        raise ValueError("contribution is already fulfilled")
    payload = _fulfillment_payload({
        "version": CONTRIBUTION_VERSION,
        "contribution_id": ticket["contribution_id"],
        "origin_id": ticket["origin_id"],
        "resolution_digest": _digest(verified["resolution"]),
        "transaction_id": transaction_id,
        "proof": proof or {},
        "timestamp": _now(),
    })
    payload["signature"] = sign_payload(
        private_key_hex, DOMAIN_FULFILLMENT, _canonical(payload),
    )
    return verify_contribution_package({**verified, "fulfillment": payload})


def _verify_fulfillment(ticket: dict, resolution: dict, fulfillment: dict) -> dict:
    payload = _fulfillment_payload(fulfillment)
    if payload["version"] != CONTRIBUTION_VERSION:
        raise ValueError("unsupported contribution fulfillment version")
    if payload["contribution_id"] != ticket["contribution_id"]:
        raise ValueError("fulfillment references a different contribution")
    if payload["origin_id"] != ticket["origin_id"]:
        raise ValueError("fulfillment origin does not match the ticket")
    if payload["resolution_digest"] != _digest(resolution):
        raise ValueError("fulfillment does not bind this resolution")
    if parse_timestamp(payload["timestamp"], "fulfillment timestamp") < parse_timestamp(
        resolution["timestamp"], "resolution timestamp",
    ):
        raise ValueError("fulfillment predates contribution resolution")
    signature = fulfillment.get("signature")
    if not isinstance(signature, str) or not verify_signed_payload(
        payload["origin_id"], signature, DOMAIN_FULFILLMENT, _canonical(payload),
    ):
        raise ValueError("invalid contribution fulfillment signature")
    return {**payload, "signature": signature}


def verify_contribution_package(package: dict, *, require_unexpired: bool = False) -> dict:
    """Verify a ticket, every route hop, resolution, and fulfillment."""
    if not isinstance(package, dict) or package.get("version") != CONTRIBUTION_VERSION:
        raise ValueError("unsupported contribution package")
    ticket = verify_contribution_ticket(
        package.get("ticket"), require_unexpired=require_unexpired,
    )
    path = _verify_path(ticket, package.get("path", []))
    resolution = package.get("resolution")
    if resolution is not None:
        resolution = _verify_resolution(ticket, path, resolution)
    fulfillment = package.get("fulfillment")
    if fulfillment is not None:
        if resolution is None:
            raise ValueError("fulfillment requires a resolution")
        fulfillment = _verify_fulfillment(ticket, resolution, fulfillment)
    return {
        "version": CONTRIBUTION_VERSION,
        "ticket": ticket,
        "path": path,
        "resolution": resolution,
        "fulfillment": fulfillment,
    }


def contribution_state(package: dict) -> str:
    verified = verify_contribution_package(package)
    if verified["fulfillment"]:
        return "fulfilled"
    if verified["resolution"]:
        if verified["resolution"]["reason"] == "declined":
            return "declined"
        return "resolved" if verified["resolution"]["beneficiary"] else "unroutable"
    if is_expired_at(verified["ticket"]["expires_at"]):
        return "expired"
    return "routing" if verified["path"] else "created"


__all__ = [
    "CONTRIBUTION_PROTOCOL",
    "CONTRIBUTION_RATE",
    "CONTRIBUTION_VERSION",
    "DEFAULT_LIVENESS_WINDOW_SECONDS",
    "MAX_CONTRIBUTION_HOPS",
    "append_contribution_hop",
    "contribution_amount",
    "contribution_state",
    "create_contribution_ticket",
    "create_source_receipt",
    "fulfill_contribution",
    "new_contribution_id",
    "resolve_contribution",
    "verify_contribution_package",
    "verify_contribution_ticket",
    "verify_source_receipt",
]

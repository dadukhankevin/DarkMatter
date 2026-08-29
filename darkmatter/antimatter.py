"""AntiMatter: signed, rail-neutral settlements over DarkMatter relationships.

DarkMatter transports encrypted events. AntiMatter projects those events into a
local settlement ledger and writes only bilaterally confirmed outcomes onto the
relationship trust record. It never moves money and never controls mail delivery.
"""

from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from darkmatter.contract.envelope import ANTIMATTER_ENVELOPE_TYPES
from darkmatter.contract.types import REL_ACTIVE
from darkmatter.policy import load_policy
from darkmatter.store.local import LocalStore, atomic_write_text


PROTOCOL = "antimatter/1"
DEFAULT_SETTLEMENT_TRUST_DELTA = 0.05

OFFER = "antimatter_offer"
ACCEPT = "antimatter_accept"
INVOICE = "antimatter_invoice"
RECEIPT = "antimatter_receipt"
CONFIRM = "antimatter_confirm"
DISPUTE = "antimatter_dispute"

_ACTION_BY_TYPE = {
    OFFER: "offer",
    ACCEPT: "accept",
    INVOICE: "invoice",
    RECEIPT: "receipt",
    CONFIRM: "confirm",
    DISPUTE: "dispute",
}
_SETTLEMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_AGENT_ID = re.compile(r"^[0-9a-fA-F]{64}$")


class AntimatterError(ValueError):
    """A signed AntiMatter event is semantically invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value, name: str, *, maximum: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise AntimatterError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise AntimatterError(f"{name} is required")
    if len(value) > maximum:
        raise AntimatterError(f"{name} exceeds {maximum} characters")
    return value


def _object(value, name: str, *, maximum_bytes: int = 16_384) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AntimatterError(f"{name} must be an object")
    try:
        encoded = json.dumps(value, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AntimatterError(f"{name} must be JSON-serializable") from exc
    if len(encoded) > maximum_bytes:
        raise AntimatterError(f"{name} exceeds {maximum_bytes} bytes")
    return deepcopy(value)


def _iso_time(value, name: str, *, required: bool = False) -> str:
    value = _text(value, name, maximum=64, required=required)
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AntimatterError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise AntimatterError(f"{name} must include a timezone")
    return parsed.isoformat()


def normalize_amount(value) -> str:
    """Return a positive, non-exponent decimal string without float arithmetic."""
    if isinstance(value, bool) or value is None:
        raise AntimatterError("amount is required")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise AntimatterError("amount must be a decimal number") from exc
    if not amount.is_finite() or amount <= 0:
        raise AntimatterError("amount must be finite and greater than zero")
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if len(rendered) > 128:
        raise AntimatterError("amount is too large")
    return rendered


def new_settlement_id() -> str:
    return f"am-{uuid.uuid4().hex}"


def _settlement_id(value) -> str:
    value = _text(value, "settlement_id", maximum=128)
    if not _SETTLEMENT_ID.fullmatch(value):
        raise AntimatterError("settlement_id contains unsupported characters")
    return value


def _agent_id(value, name: str) -> str:
    value = _text(value, name, maximum=64)
    if not _AGENT_ID.fullmatch(value):
        raise AntimatterError(f"{name} must be a 32-byte public key")
    return value.lower()


def _reference(value, name: str, *, required: bool = True) -> str:
    return _text(value, name, maximum=128, required=required)


def _base_body(action: str, settlement_id: str) -> dict:
    return {
        "protocol": PROTOCOL,
        "action": action,
        "settlement_id": settlement_id,
    }


def offer_body(
    settlement_id: str,
    *,
    payer_id: str,
    payee_id: str,
    proposer_role: str,
    description: str,
    amount,
    currency: str,
    rail: str,
    terms: Optional[dict] = None,
    metadata: Optional[dict] = None,
    valid_until: Optional[str] = None,
) -> dict:
    body = _base_body("offer", settlement_id)
    body.update({
        "payer_id": payer_id,
        "payee_id": payee_id,
        "proposer_role": proposer_role,
        "terms": {
            "description": description,
            "amount": amount,
            "currency": currency,
            "rail": rail,
            "details": terms or {},
        },
        "metadata": metadata or {},
    })
    if valid_until:
        body["valid_until"] = valid_until
    return body


def event_body(action: str, settlement_id: str, **fields) -> dict:
    body = _base_body(action, settlement_id)
    body.update({key: value for key, value in fields.items() if value is not None})
    return body


def summarize_event(env_type: str, body: dict) -> str:
    """Create a bounded human/model-facing summary without trusting peer prose."""
    settlement_id = str(body.get("settlement_id") or "unknown")[:128]
    action = _ACTION_BY_TYPE.get(env_type, env_type)
    if env_type == OFFER:
        terms = body.get("terms") if isinstance(body.get("terms"), dict) else {}
        description = str(terms.get("description") or "settlement")[:160]
        amount = str(terms.get("amount") or "?")[:64]
        currency = str(terms.get("currency") or "units")[:32]
        return f"AntiMatter offer {settlement_id}: {description} — {amount} {currency}"
    if env_type == DISPUTE:
        reason = str(body.get("reason") or "unspecified")[:160]
        return f"AntiMatter dispute {settlement_id}: {reason}"
    return f"AntiMatter {action} for settlement {settlement_id}"


class AntimatterLedger:
    """Project a signed AntiMatter event stream into local settlement state."""

    def __init__(self, store: LocalStore):
        self.store = store

    @property
    def path(self):
        return self.store.dir / "antimatter.json"

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "settlements": {}}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise AntimatterError(f"Could not read AntiMatter ledger: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("settlements"), dict):
            raise AntimatterError("Malformed AntiMatter ledger")
        return data

    def _save(self, data: dict) -> None:
        atomic_write_text(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    def get(self, settlement_id: str) -> Optional[dict]:
        with self.store.locked():
            record = self._load()["settlements"].get(settlement_id)
            return deepcopy(record) if record else None

    def list(self, peer_id: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
        with self.store.locked():
            records = list(self._load()["settlements"].values())
        if peer_id:
            records = [record for record in records if record.get("peer_id") == peer_id]
        if status:
            records = [record for record in records if record.get("status") == status]
        records.sort(key=lambda record: record.get("updated_at") or "", reverse=True)
        return deepcopy(records)

    def _event(
        self,
        env_type: str,
        actor_id: str,
        envelope_id: str,
        timestamp: str,
        body: dict,
    ) -> dict:
        return {
            "id": _reference(envelope_id, "envelope_id"),
            "type": env_type,
            "from": actor_id,
            "timestamp": _iso_time(timestamp, "timestamp", required=True),
            "observed_at": _now(),
            "direction": "local" if actor_id == self.store.agent_id else "remote",
            "body": deepcopy(body),
        }

    def _common(self, env_type: str, body: dict) -> tuple[str, dict]:
        if env_type not in ANTIMATTER_ENVELOPE_TYPES:
            raise AntimatterError(f"Unknown AntiMatter event type: {env_type}")
        if not isinstance(body, dict):
            raise AntimatterError("AntiMatter body must be an object")
        if body.get("protocol") != PROTOCOL:
            raise AntimatterError(f"protocol must be {PROTOCOL}")
        expected_action = _ACTION_BY_TYPE[env_type]
        if body.get("action") != expected_action:
            raise AntimatterError(f"action must be {expected_action}")
        return _settlement_id(body.get("settlement_id")), deepcopy(body)

    @staticmethod
    def _assert_participant(record: dict, actor_id: str) -> None:
        if actor_id not in (record["payer_id"], record["payee_id"]):
            raise AntimatterError("event sender is not a settlement participant")

    @staticmethod
    def _assert_open(record: dict) -> None:
        if record["status"] in ("settled", "disputed"):
            raise AntimatterError(f"settlement is already {record['status']}")

    def _settled_trust_delta(self, record: dict) -> float:
        value = DEFAULT_SETTLEMENT_TRUST_DELTA
        hooks = load_policy(self.store.root)
        if hooks and hasattr(hooks, "settlement_trust_delta"):
            try:
                value = float(hooks.settlement_trust_delta(deepcopy(record)))
            except Exception:
                value = DEFAULT_SETTLEMENT_TRUST_DELTA
        return max(0.0, min(1.0, value))

    def apply_event(
        self,
        env_type: str,
        actor_id: str,
        peer_id: str,
        envelope_id: str,
        timestamp: str,
        body: dict,
    ) -> dict:
        """Validate and apply one local or remote signed event."""
        actor_id = _agent_id(actor_id, "actor_id")
        peer_id = _agent_id(peer_id, "peer_id")
        if actor_id not in (self.store.agent_id, peer_id):
            return {"success": False, "error": "actor_id does not match this relationship"}
        relationship = self.store.get_relationship(peer_id)
        if relationship is None or relationship.state != REL_ACTIVE:
            return {"success": False, "error": "No active relationship"}

        try:
            with self.store.locked():
                settlement_id, normalized = self._common(env_type, body)
                data = self._load()
                settlements = data["settlements"]
                record = settlements.get(settlement_id)
                event = self._event(env_type, actor_id, envelope_id, timestamp, normalized)
                trust_delta = 0.0

                if env_type == OFFER:
                    if record is not None:
                        raise AntimatterError("settlement_id already exists")
                    payer_id = _agent_id(normalized.get("payer_id"), "payer_id")
                    payee_id = _agent_id(normalized.get("payee_id"), "payee_id")
                    if payer_id == payee_id or {payer_id, payee_id} != {self.store.agent_id, peer_id}:
                        raise AntimatterError("offer participants must be this active relationship")
                    proposer_role = _text(
                        normalized.get("proposer_role"), "proposer_role", maximum=8,
                    )
                    if proposer_role not in ("payer", "payee"):
                        raise AntimatterError("proposer_role must be payer or payee")
                    if actor_id != (payer_id if proposer_role == "payer" else payee_id):
                        raise AntimatterError("offer sender does not match proposer_role")
                    terms = normalized.get("terms")
                    if not isinstance(terms, dict):
                        raise AntimatterError("terms must be an object")
                    clean_terms = {
                        "description": _text(
                            terms.get("description"), "description", maximum=2000,
                        ),
                        "amount": normalize_amount(terms.get("amount")),
                        "currency": _text(terms.get("currency"), "currency", maximum=64),
                        "rail": _text(terms.get("rail"), "rail", maximum=128),
                        "details": _object(terms.get("details"), "terms.details"),
                    }
                    valid_until = _iso_time(normalized.get("valid_until"), "valid_until")
                    if valid_until and datetime.fromisoformat(valid_until) <= datetime.now(timezone.utc):
                        raise AntimatterError("valid_until must be in the future")
                    normalized.update({
                        "payer_id": payer_id,
                        "payee_id": payee_id,
                        "proposer_role": proposer_role,
                        "terms": clean_terms,
                        "metadata": _object(normalized.get("metadata"), "metadata"),
                    })
                    if valid_until:
                        normalized["valid_until"] = valid_until
                    else:
                        normalized.pop("valid_until", None)
                    event["body"] = deepcopy(normalized)
                    observed = event["observed_at"]
                    record = {
                        "settlement_id": settlement_id,
                        "peer_id": peer_id,
                        "payer_id": payer_id,
                        "payee_id": payee_id,
                        "status": "offered",
                        "terms": clean_terms,
                        "metadata": normalized["metadata"],
                        "valid_until": valid_until or None,
                        "offer": event,
                        "acceptance": None,
                        "invoice": None,
                        "receipts": [],
                        "confirmation": None,
                        "disputes": [],
                        "created_at": observed,
                        "updated_at": observed,
                    }
                    settlements[settlement_id] = record
                else:
                    if record is None:
                        raise AntimatterError("Unknown settlement_id")
                    if record.get("peer_id") != peer_id:
                        raise AntimatterError("settlement belongs to a different relationship")
                    self._assert_participant(record, actor_id)

                    if env_type == ACCEPT:
                        self._assert_open(record)
                        if record["status"] != "offered":
                            raise AntimatterError("only an offered settlement can be accepted")
                        if actor_id == record["offer"]["from"]:
                            raise AntimatterError("the offer counterparty must accept")
                        if _reference(normalized.get("offer_id"), "offer_id") != record["offer"]["id"]:
                            raise AntimatterError("offer_id does not match")
                        valid_until = record.get("valid_until")
                        if valid_until and datetime.fromisoformat(valid_until) <= datetime.now(timezone.utc):
                            raise AntimatterError("offer has expired")
                        normalized["note"] = _text(
                            normalized.get("note"), "note", maximum=1000, required=False,
                        )
                        normalized["metadata"] = _object(normalized.get("metadata"), "metadata")
                        event["body"] = deepcopy(normalized)
                        record["acceptance"] = event
                        record["status"] = "accepted"

                    elif env_type == INVOICE:
                        self._assert_open(record)
                        if record["status"] != "accepted":
                            raise AntimatterError("only an accepted settlement can be invoiced")
                        if actor_id != record["payee_id"]:
                            raise AntimatterError("only the payee can issue the invoice")
                        acceptance = record.get("acceptance")
                        if _reference(normalized.get("acceptance_id"), "acceptance_id") != acceptance["id"]:
                            raise AntimatterError("acceptance_id does not match")
                        normalized["destination"] = _object(
                            normalized.get("destination"), "destination",
                        )
                        normalized["memo"] = _text(
                            normalized.get("memo"), "memo", maximum=1000, required=False,
                        )
                        due_at = _iso_time(normalized.get("due_at"), "due_at")
                        if due_at:
                            normalized["due_at"] = due_at
                        else:
                            normalized.pop("due_at", None)
                        event["body"] = deepcopy(normalized)
                        record["invoice"] = event
                        record["status"] = "invoiced"

                    elif env_type == RECEIPT:
                        self._assert_open(record)
                        if record["status"] not in ("accepted", "invoiced", "receipt_submitted"):
                            raise AntimatterError("settlement is not ready for a receipt")
                        if actor_id != record["payer_id"]:
                            raise AntimatterError("only the payer can submit a receipt")
                        acceptance = record.get("acceptance")
                        if _reference(normalized.get("acceptance_id"), "acceptance_id") != acceptance["id"]:
                            raise AntimatterError("acceptance_id does not match")
                        invoice = record.get("invoice")
                        invoice_id = _reference(
                            normalized.get("invoice_id"), "invoice_id", required=False,
                        )
                        if invoice and invoice_id != invoice["id"]:
                            raise AntimatterError("invoice_id does not match")
                        if not invoice and invoice_id:
                            raise AntimatterError("invoice_id was provided but no invoice exists")
                        normalized["tx_id"] = _text(normalized.get("tx_id"), "tx_id", maximum=512)
                        normalized["proof"] = _object(normalized.get("proof"), "proof", maximum_bytes=32_768)
                        normalized["note"] = _text(
                            normalized.get("note"), "note", maximum=1000, required=False,
                        )
                        event["body"] = deepcopy(normalized)
                        record["receipts"].append(event)
                        record["status"] = "receipt_submitted"

                    elif env_type == CONFIRM:
                        self._assert_open(record)
                        if record["status"] != "receipt_submitted":
                            raise AntimatterError("a receipt must be submitted before confirmation")
                        if actor_id != record["payee_id"]:
                            raise AntimatterError("only the payee can confirm settlement")
                        receipt_id = _reference(normalized.get("receipt_id"), "receipt_id")
                        receipt = next(
                            (item for item in record["receipts"] if item["id"] == receipt_id),
                            None,
                        )
                        if receipt is None:
                            raise AntimatterError("receipt_id does not match")
                        normalized["note"] = _text(
                            normalized.get("note"), "note", maximum=1000, required=False,
                        )
                        normalized["verification"] = _object(
                            normalized.get("verification"), "verification",
                        )
                        event["body"] = deepcopy(normalized)
                        record["confirmation"] = event
                        record["status"] = "settled"
                        trust_delta = self._settled_trust_delta(record)
                        record["trust_delta"] = trust_delta
                        record["settled_at"] = event["observed_at"]
                        self.store.record_settlement(
                            peer_id,
                            trust_delta=trust_delta,
                            tx_id=receipt["body"]["tx_id"],
                            extra={
                                "protocol": PROTOCOL,
                                "settlement_id": settlement_id,
                                "status": "settled",
                                "receipt_id": receipt_id,
                                "verification": "bilateral_confirmation",
                                "trust_delta": trust_delta,
                            },
                        )

                    elif env_type == DISPUTE:
                        self._assert_open(record)
                        normalized["reason"] = _text(
                            normalized.get("reason"), "reason", maximum=2000,
                        )
                        normalized["reference_id"] = _reference(
                            normalized.get("reference_id"), "reference_id", required=False,
                        )
                        normalized["evidence"] = _object(
                            normalized.get("evidence"), "evidence", maximum_bytes=32_768,
                        )
                        event["body"] = deepcopy(normalized)
                        record["disputes"].append(event)
                        record["status"] = "disputed"

                    record["updated_at"] = event["observed_at"]

                self._save(data)
                return {
                    "success": True,
                    "event_id": envelope_id,
                    "settlement": deepcopy(record),
                    "trust_delta": trust_delta,
                }
        except (AntimatterError, KeyError, TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}


__all__ = [
    "ACCEPT",
    "ANTIMATTER_ENVELOPE_TYPES",
    "AntimatterError",
    "AntimatterLedger",
    "CONFIRM",
    "DEFAULT_SETTLEMENT_TRUST_DELTA",
    "DISPUTE",
    "INVOICE",
    "OFFER",
    "PROTOCOL",
    "RECEIPT",
    "event_body",
    "new_settlement_id",
    "normalize_amount",
    "offer_body",
    "summarize_event",
]

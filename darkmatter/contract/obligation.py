"""Portable, bilateral contribution agreements and attributed discussion events.

These authenticate promises, not payment, operator independence or permission to
spend. They are carried inside encrypted settlement correspondence by default.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from copy import deepcopy

from darkmatter.commitment import MODES, verify_commitment
from darkmatter.contract.tenure import parse_timestamp
from darkmatter.identity import derive_public_key_hex
from darkmatter.security import sign_payload, verify_signed_payload

PROPOSAL_DOMAIN = "darkmatter.contribution-agreement.v1"
ACCEPTANCE_DOMAIN = "darkmatter.contribution-acceptance.v1"
DISCUSSION_DOMAIN = "darkmatter.contribution-discussion.v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _signed(key, domain, payload):
    return {**payload, "signature": sign_payload(key, domain, canonical(payload))}


def _verify(data, domain, signer_field, fields):
    if not isinstance(data, dict) or set(data) != {*fields, "signature"} or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("Malformed contribution agreement proof")
    payload = {k: deepcopy(data[k]) for k in fields}
    signer = payload[signer_field]
    if not isinstance(signer, str) or not re.fullmatch(r"[0-9a-f]{64}", signer):
        raise ValueError("Invalid contribution proof signer")
    if not verify_signed_payload(signer, data["signature"], domain, canonical(payload)):
        raise ValueError("Invalid contribution proof signature")
    parse_timestamp(payload["timestamp"], "timestamp")
    return {**payload, "signature": data["signature"]}


PROPOSAL_FIELDS = ("version", "settlement_id", "offer_id", "proposer_id", "payer_id", "payee_id",
                   "terms_digest", "amount", "currency", "rail", "mode", "rate", "commitment", "timestamp")
ACCEPTANCE_FIELDS = ("version", "agreement_digest", "actor_id", "timestamp")
DISCUSSION_FIELDS = ("version", "agreement_digest", "actor_id", "timestamp", "id", "action", "reference", "reason")


def create_proposal(key, *, settlement_id, offer_id, payer_id, payee_id, terms, mode, commitment, timestamp):
    if mode not in MODES:
        raise ValueError("contribution_mode must be participate, observe or decline")
    if commitment is not None:
        commitment = verify_commitment(commitment, payee_id)
    proposer_id = derive_public_key_hex(key)
    if proposer_id not in (payer_id, payee_id):
        raise ValueError("Only a settlement participant can propose contribution terms")
    return _signed(key, PROPOSAL_DOMAIN, {
        "version": 1, "settlement_id": settlement_id, "offer_id": offer_id,
        "proposer_id": proposer_id, "payer_id": payer_id, "payee_id": payee_id,
        "terms_digest": digest(terms), "amount": terms["amount"], "currency": terms["currency"],
        "rail": terms["rail"], "mode": mode, "rate": "0.01", "commitment": commitment, "timestamp": timestamp,
    })


def verify_proposal(data):
    proposal = _verify(data, PROPOSAL_DOMAIN, "proposer_id", PROPOSAL_FIELDS)
    for field in ("payer_id", "payee_id", "terms_digest"):
        if not isinstance(proposal[field], str) or not re.fullmatch(r"[0-9a-f]{64}", proposal[field]):
            raise ValueError("Invalid contribution identity or terms digest")
    for field, maximum in (("settlement_id", 128), ("offer_id", 128), ("amount", 128), ("currency", 64), ("rail", 128)):
        if not isinstance(proposal[field], str) or not 1 <= len(proposal[field]) <= maximum:
            raise ValueError("Invalid contribution proposal field")
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", proposal["amount"]) or Decimal(proposal["amount"]) <= 0:
        raise ValueError("Invalid contribution amount")
    if proposal["mode"] not in MODES or proposal["rate"] != "0.01":
        raise ValueError("Invalid contribution terms")
    if proposal["payer_id"] == proposal["payee_id"] or proposal["proposer_id"] not in (proposal["payer_id"], proposal["payee_id"]):
        raise ValueError("Invalid contribution participants")
    if proposal["commitment"] is not None:
        proposal["commitment"] = verify_commitment(proposal["commitment"], proposal["payee_id"])
    return proposal


def create_acceptance(key, proposal, timestamp):
    proposal = verify_proposal(proposal)
    actor = derive_public_key_hex(key)
    if actor not in (proposal["payer_id"], proposal["payee_id"]) or actor == proposal["proposer_id"]:
        raise ValueError("The proposal counterparty must accept")
    return _signed(key, ACCEPTANCE_DOMAIN, {"version": 1, "agreement_digest": digest(proposal),
                                           "actor_id": actor, "timestamp": timestamp})


def verify_agreement(proposal, acceptance):
    proposal = verify_proposal(proposal)
    acceptance = _verify(acceptance, ACCEPTANCE_DOMAIN, "actor_id", ACCEPTANCE_FIELDS)
    if acceptance["agreement_digest"] != digest(proposal):
        raise ValueError("Acceptance does not bind these contribution terms")
    if acceptance["actor_id"] not in (proposal["payer_id"], proposal["payee_id"]) or acceptance["actor_id"] == proposal["proposer_id"]:
        raise ValueError("The proposal counterparty must accept")
    if parse_timestamp(acceptance["timestamp"], "timestamp") < parse_timestamp(proposal["timestamp"], "timestamp"):
        raise ValueError("Acceptance predates the proposal")
    return {"proposal": proposal, "acceptance": acceptance}


def create_discussion(key, agreement, *, event_id, action, reference, reason, timestamp):
    verified = verify_agreement(**agreement)
    payload = {"version": 1, "agreement_digest": digest(verified), "actor_id": derive_public_key_hex(key),
               "timestamp": timestamp, "id": event_id, "action": action, "reference": reference or "", "reason": reason}
    event = _signed(key, DISCUSSION_DOMAIN, payload)
    return verify_discussion(event, verified)


def verify_discussion(event, agreement):
    agreement = verify_agreement(**agreement)
    event = _verify(event, DISCUSSION_DOMAIN, "actor_id", DISCUSSION_FIELDS)
    proposal = agreement["proposal"]
    if event["actor_id"] not in (proposal["payer_id"], proposal["payee_id"]) or event["agreement_digest"] != digest(agreement):
        raise ValueError("Discussion does not belong to this bilateral agreement")
    if parse_timestamp(event["timestamp"], "timestamp") < parse_timestamp(agreement["acceptance"]["timestamp"], "timestamp"):
        raise ValueError("Discussion predates acceptance")
    if event["action"] not in ("dispute", "withdraw"):
        raise ValueError("Discussion action must be dispute or withdraw")
    for field, maximum in (("id", 128), ("reference", 128), ("reason", 2000)):
        if not isinstance(event[field], str) or len(event[field]) > maximum:
            raise ValueError("Invalid discussion field")
    if not event["id"] or not event["reason"].strip() or (event["action"] == "withdraw" and not event["reference"]):
        raise ValueError("Discussion requires an id, reason and withdrawal reference")
    return event


def source_matches(record, source):
    """Match a ticket to retained bilateral economics and a specific signed receipt."""
    expected = {"settlement_id": record["settlement_id"], "payer_id": record["payer_id"],
                "payee_id": record["payee_id"], **{k: record["terms"][k] for k in ("amount", "currency", "rail")}}
    if any(source.get(k) != v for k, v in expected.items()):
        return False
    confirmed_id = (record.get("confirmation") or {}).get("body", {}).get("receipt_id")
    return any(
        (not confirmed_id or r["id"] == confirmed_id)
        and source.get("receipt_attestation") == r["body"].get("receipt_attestation")
        and source.get("receipt_id") == r["id"] and source.get("transaction_id") == r["body"].get("tx_id")
        for r in record.get("receipts") or []
    )

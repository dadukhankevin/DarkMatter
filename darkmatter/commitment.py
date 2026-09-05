"""Voluntary signed AntiMatter commitments, separate from proof of payment."""

import json
from datetime import datetime, timezone

from darkmatter.contract.tenure import parse_timestamp
from darkmatter.identity import derive_public_key_hex
from darkmatter.security import sign_payload, verify_signed_payload
from darkmatter.store.local import atomic_write_text

DOMAIN = "darkmatter.antimatter-commitment.v1"
MODES = ("participate", "observe", "decline")


def _payload(data):
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("mode") not in MODES:
        raise ValueError("Invalid AntiMatter commitment")
    if data.get("rate") != "0.01":
        raise ValueError("AntiMatter contribution rate must be exactly 1%")
    note = data.get("note", "")
    if not isinstance(note, str) or len(note.encode()) > 512:
        raise ValueError("Commitment note exceeds limit")
    timestamp = parse_timestamp(data.get("effective_from"), "effective_from")
    if timestamp > datetime.now(timezone.utc):
        raise ValueError("Commitment cannot begin in the future")
    return {"version": 1, "agent_id": data.get("agent_id"), "mode": data["mode"],
            "rate": "0.01", "effective_from": timestamp.isoformat(), "note": note}


def verify_commitment(data, agent_id):
    payload = _payload(data)
    if payload["agent_id"] != agent_id or not verify_signed_payload(
        agent_id, data.get("signature", ""), DOMAIN, json.dumps(payload, sort_keys=True, separators=(",", ":"))
    ):
        raise ValueError("Invalid commitment signature or identity")
    return {**payload, "signature": data["signature"]}


def read_commitment(repo, agent_id):
    path = repo / "commitment.json"
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 8192:
        raise ValueError("Invalid commitment file")
    return verify_commitment(json.loads(path.read_text()), agent_id)


def declare_commitment(mailbox, mode, note=""):
    """Publish a user's voluntary convention; never route mail or spend funds."""
    with mailbox.store.locked():
        payload = _payload({"version": 1, "agent_id": mailbox.agent_id, "mode": mode,
                            "rate": "0.01", "effective_from": datetime.now(timezone.utc).isoformat(), "note": note})
        previous = read_commitment(mailbox.work, mailbox.agent_id)
        if previous and previous["mode"] == mode and previous["note"] == note:
            return {"success": True, "commitment": previous, "unchanged": True}
        if derive_public_key_hex(mailbox.store.private_key_hex) != mailbox.agent_id:
            raise ValueError("Commitment signing key mismatch")
        payload["signature"] = sign_payload(mailbox.store.private_key_hex, DOMAIN,
                                            json.dumps(payload, sort_keys=True, separators=(",", ":")))
        atomic_write_text(mailbox.work / "commitment.json", json.dumps(payload, indent=2) + "\n")
        errors = mailbox._publish("declare AntiMatter commitment")
        return {"success": True, "commitment": payload, "publish_errors": errors}


def accountability(commitment, records, agent_id):
    """Show disclosed follow-through; absence of evidence is explicitly unknown."""
    originated = [r for r in records if r["origin_id"] == agent_id]
    return {
        "commitment": commitment,
        "disclosed_contributions": len(originated),
        "signed_fulfillments": sum(r["status"] == "fulfilled" for r in originated),
        "resolved_awaiting_fulfillment": [r["contribution_id"] for r in originated if r["status"] == "resolved"],
        "expired_without_resolution": [r["contribution_id"] for r in originated if r["status"] == "expired"],
        "unroutable": sum(r["status"] == "unroutable" for r in originated),
        "coverage": "Disclosed tickets only. Missing settlements, deleted evidence and external payments are unknown. "
                    "Fulfillment is a signed claim until independently rail-verified. "
                    "Current commitment timestamps are self-asserted; use retained history when assessing changes.",
    }

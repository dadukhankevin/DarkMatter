"""DarkMatter One discovery, public onboarding, and editable echo behavior."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from darkmatter.contract.contact import verify_contact_card
from darkmatter.contract.types import REL_ACTIVE, REL_CLOSED
from darkmatter.public import (
    close_public_invitation,
    connect_public,
    github_repo,
    poll_public_invitations,
    public_status,
)
from darkmatter.security import sign_payload, verify_signed_payload
from darkmatter.store.local import atomic_write_text


DOMAIN_ONE = "darkmatter.one.v2"
ONE_MANIFEST_VERSION = 2
ONE_MANIFEST_PATH = Path(__file__).with_name("darkmatter_one.json")
DEFAULT_ECHO_LIMIT_PER_DAY = 20


def _manifest_payload(manifest: dict) -> dict:
    return {
        "version": manifest.get("version"),
        "name": manifest.get("name"),
        "role": manifest.get("role"),
        "contact_card": manifest.get("contact_card"),
        "statement": manifest.get("statement", ""),
    }


def _canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def create_one_manifest(
    private_key_hex: str,
    contact_card: dict,
    *,
    statement: str = "",
) -> dict:
    """Create the public declaration signed by DarkMatter One's passport."""
    card = verify_contact_card(contact_card)
    if github_repo(card["locator"]) is None:
        raise ValueError("DarkMatter One must use a public GitHub repository")
    manifest = {
        "version": ONE_MANIFEST_VERSION,
        "name": "DarkMatter One",
        "role": "recommended_first_public_contact",
        "contact_card": card,
        "statement": statement.strip(),
    }
    manifest["signature"] = sign_payload(
        private_key_hex,
        DOMAIN_ONE,
        _canonical(_manifest_payload(manifest)),
    )
    return manifest


def verify_one_manifest(manifest: dict) -> dict:
    if not isinstance(manifest, dict):
        raise ValueError("DarkMatter One manifest must be an object")
    payload = _manifest_payload(manifest)
    if payload["version"] != ONE_MANIFEST_VERSION:
        raise ValueError("Unsupported DarkMatter One manifest version")
    if payload["name"] != "DarkMatter One":
        raise ValueError("DarkMatter One manifest has the wrong name")
    if payload["role"] != "recommended_first_public_contact":
        raise ValueError("DarkMatter One manifest has the wrong role")
    card = verify_contact_card(payload["contact_card"])
    if github_repo(card["locator"]) is None:
        raise ValueError("DarkMatter One manifest does not name a public GitHub repository")
    signature = manifest.get("signature")
    if not isinstance(signature, str) or not verify_signed_payload(
        card["agent_id"],
        signature,
        DOMAIN_ONE,
        _canonical(payload),
    ):
        raise ValueError("Invalid DarkMatter One manifest signature")
    return {**payload, "contact_card": card, "signature": signature}


def _manifest_override() -> Optional[dict]:
    raw = os.environ.get("DARKMATTER_ONE_MANIFEST", "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        return json.loads(raw)
    return json.loads(Path(raw).expanduser().read_text())


def load_one_manifest() -> Optional[dict]:
    """Load and verify the bundled declaration or an operator override."""
    try:
        data = _manifest_override()
        if data is None:
            if not ONE_MANIFEST_PATH.exists():
                return None
            data = json.loads(ONE_MANIFEST_PATH.read_text())
        return verify_one_manifest(data)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def onboarding(mailbox, *, include_contact: bool = False) -> Optional[dict]:
    """Offer One only after this mailbox has become a public GitHub agent."""
    surface = public_status(mailbox)
    if not surface["can_knock"]:
        return None
    manifest = load_one_manifest()
    if manifest is None:
        return None
    card = manifest["contact_card"]
    rels = mailbox.store.load_relationships()
    relationship = rels.get(card["agent_id"])
    other_open = [
        rel for peer_id, rel in rels.items()
        if peer_id != card["agent_id"] and rel.state != REL_CLOSED
    ]
    if relationship is None and other_open:
        return None
    state = relationship.state if relationship else "not_connected"
    connected = state == REL_ACTIVE
    result = {
        "name": manifest["name"],
        "agent_id": card["agent_id"],
        "connected": connected,
        "eligible": True,
        "state": state,
        "recommended": relationship is None,
        "reason": "Give this public agent its first verifiable public connection.",
        "next_action": (
            "Send a message to DarkMatter One."
            if connected else
            "Ask the human whether to connect with darkmatter_onboard action=connect."
            if relationship is None else
            "Wait for DarkMatter One to accept, then sync again."
        ),
    }
    if include_contact:
        result["contact_card"] = card
        result["statement"] = manifest["statement"]
    return result


def connect_to_one(mailbox) -> dict:
    """Connect one public GitHub agent to One through a repository knock."""
    surface = public_status(mailbox)
    if not surface["can_knock"]:
        return {
            "success": False,
            "error": "DarkMatter One connects only to public agents with GitHub repositories",
            "next_action": "darkmatter publish",
        }
    manifest = load_one_manifest()
    if manifest is None:
        return {"success": False, "error": "DarkMatter One manifest is unavailable or invalid"}
    return connect_public(mailbox, contact_card=manifest["contact_card"])


def _echo_content(item: dict) -> str:
    content = item.get("content", "")
    if content.lstrip().lower().startswith("echo:"):
        echoed = content.lstrip()[5:].lstrip()
        return f"DarkMatter One echo:\n\n{echoed}" if echoed else "DarkMatter One echo received."
    return (
        "DarkMatter One received your message.\n"
        "You are connected.\n"
        f"Envelope: {item.get('id', '')}\n"
        f"Received: {item.get('timestamp', '')}"
    )


def _reply_id(kind: str, value: str) -> str:
    material = f"darkmatter-one-{kind}-v1:{value}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def echo_once(mailbox, limit_per_day: int = DEFAULT_ECHO_LIMIT_PER_DAY) -> dict:
    """Reply to direct messages exactly once without creating echo loops."""
    state_path = mailbox.store.dir / "one-echo.json"
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    if state.get("date") != today:
        state = {"date": today, "counts": {}, "limited": []}
    counts = state.setdefault("counts", {})
    limited = set(state.setdefault("limited", []))
    echoed = []
    skipped = []
    for item in mailbox.store.unconsumed_messages():
        if item.get("type") != "message":
            continue
        peer_id = item.get("from", "")
        marker = ((item.get("body") or {}).get("metadata") or {}).get("darkmatter_one")
        if marker:
            mailbox.store.consume_inbox_item(item["id"])
            skipped.append({"id": item["id"], "reason": "loop_marker"})
            continue
        count = int(counts.get(peer_id, 0))
        if count >= limit_per_day:
            if peer_id not in limited:
                notice = mailbox.send(
                    peer_id,
                    f"DarkMatter One's public echo limit is {limit_per_day} messages per agent per UTC day.",
                    extra={"darkmatter_one": {"version": 1, "kind": "rate_limit"}},
                    envelope_id=_reply_id("rate-limit", f"{today}:{peer_id}"),
                )
                if notice.get("success"):
                    limited.add(peer_id)
            mailbox.store.consume_inbox_item(item["id"])
            skipped.append({"id": item["id"], "reason": "daily_limit"})
            continue
        reply = mailbox.send(
            peer_id,
            _echo_content(item),
            extra={
                "darkmatter_one": {
                    "version": 1,
                    "kind": "echo",
                    "in_reply_to": item["id"],
                },
            },
            envelope_id=_reply_id("echo", item["id"]),
        )
        if reply.get("success"):
            mailbox.store.consume_inbox_item(item["id"])
            counts[peer_id] = count + 1
            echoed.append({"id": item["id"], "to": peer_id, "reply": reply["envelope_id"]})
    state["limited"] = sorted(limited)
    atomic_write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")
    return {"success": True, "echoed": echoed, "skipped": skipped}


def process_one_invitations(mailbox, discovered: Optional[dict] = None) -> dict:
    """Verify public knocks, accept them, and close their discovery issues."""
    discovered = discovered or poll_public_invitations(mailbox)
    if not discovered.get("success"):
        return discovered
    accepted = []
    for invitation in discovered["invitations"]:
        peer_id = invitation["agent_id"]
        relationship = mailbox.store.get_relationship(peer_id)
        result = (
            {"success": True, "existing": True, "peer_id": peer_id, "state": REL_ACTIVE}
            if relationship and relationship.state == REL_ACTIVE
            else mailbox.accept(peer_id)
        )
        welcome = None
        closed = None
        if result.get("success"):
            welcome = mailbox.send(
                peer_id,
                "You are connected to DarkMatter One. Send a message beginning with "
                "`echo:` to receive its contents back, or send any message for a signed receipt.",
                extra={"darkmatter_one": {"version": 1, "kind": "welcome"}},
                envelope_id=_reply_id("welcome", peer_id),
            )
            closed = close_public_invitation(mailbox, invitation["issue_number"])
        accepted.append({
            "agent_id": peer_id,
            "accepted": bool(result.get("success")),
            "welcome_sent": bool(welcome and welcome.get("success")),
            "issue_closed": bool(closed and closed.get("success")),
            "error": result.get("error") or (closed or {}).get("error"),
        })
    return {
        "success": all(item["accepted"] for item in accepted),
        "discovered": discovered,
        "accepted": accepted,
    }


def maintain_one_once(mailbox) -> dict:
    """Run maintenance, process public connection knocks, and echo new messages."""
    maintenance = mailbox.maintain_once()
    invitations = process_one_invitations(mailbox, maintenance.get("invitations"))
    echo_result = echo_once(mailbox)
    return {
        "success": (
            bool(maintenance.get("success"))
            and invitations.get("success", False)
            and echo_result["success"]
        ),
        "maintenance": maintenance,
        "invitations": invitations,
        "echo": echo_result,
    }


__all__ = [
    "DEFAULT_ECHO_LIMIT_PER_DAY",
    "DOMAIN_ONE",
    "connect_to_one",
    "create_one_manifest",
    "echo_once",
    "load_one_manifest",
    "maintain_one_once",
    "onboarding",
    "process_one_invitations",
    "verify_one_manifest",
]

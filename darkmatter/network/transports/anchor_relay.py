"""
Anchor message relay transport — Level 5 fallback for fully NAT-ed agents.

When all direct and WebRTC transports fail, messages are relayed through
the anchor node. This is the transport of last resort — centralized but
guaranteed to work as long as the anchor is reachable.

All relay payloads are E2E encrypted using the recipient's public key.

Depends on: config, security, network/transport
"""

import json
import sys
from datetime import datetime, timezone
from typing import Optional

import httpx

from darkmatter.config import ANCHOR_NODES
from darkmatter.security import sign_message, encrypt_for_peer
from darkmatter.network.transport import Transport, SendResult


class AnchorRelayTransport(Transport):
    """Anchor message relay transport — last resort (Level 5)."""

    def __init__(self, state_getter=None):
        self._get_state = state_getter

    @property
    def name(self) -> str:
        return "anchor_relay"

    @property
    def priority(self) -> int:
        return 90  # Last resort — tried after HTTP (50) fails

    @property
    def available(self) -> bool:
        return bool(ANCHOR_NODES)

    def get_address(self, state) -> Optional[str]:
        return "anchor-relay" if self.available else None

    async def send(self, conn, path: str, payload: dict) -> SendResult:
        """Send a message via anchor's message relay endpoint.

        The payload is E2E encrypted using the recipient's public key so
        the anchor cannot read message contents.
        """
        if not ANCHOR_NODES:
            return SendResult(success=False, transport_name="anchor_relay",
                              error="No anchor nodes configured")

        state = self._get_state() if self._get_state else None
        if state is None:
            return SendResult(success=False, transport_name="anchor_relay",
                              error="Agent state not available")

        timestamp = datetime.now(timezone.utc).isoformat()

        # Build the inner payload to encrypt
        inner_payload = {
            "path": path,
            "payload": payload,
            "from_agent_id": state.agent_id,
            "timestamp": timestamp,
        }

        # Sign for authenticity
        content_str = json.dumps(payload, sort_keys=True)
        inner_payload["signature"] = sign_message(
            state.private_key_hex,
            state.agent_id,
            payload.get("message_id", "relay"),
            timestamp,
            content_str[:256],
        )

        # E2E encrypt if we have the recipient's public key
        recipient_pub = conn.agent_public_key_hex if conn else None
        if recipient_pub and state.private_key_hex:
            plaintext = json.dumps(inner_payload).encode("utf-8")
            encrypted = encrypt_for_peer(plaintext, state.private_key_hex, recipient_pub)
            relay_payload = {
                "e2e_encrypted": True,
                "encrypted_payload": encrypted,
                "from_agent_id": state.agent_id,
                "timestamp": timestamp,
            }
        else:
            # Fallback: send signed but unencrypted (no recipient public key)
            relay_payload = inner_payload

        for anchor in ANCHOR_NODES:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        f"{anchor}/__darkmatter__/message_relay/{conn.agent_id}",
                        json=relay_payload,
                    )
                    if resp.status_code == 200:
                        return SendResult(
                            success=True,
                            transport_name="anchor_relay",
                            response={"success": True, "transport": "anchor_relay",
                                      "anchor": anchor, "buffered": True,
                                      "e2e_encrypted": relay_payload.get("e2e_encrypted", False)},
                        )
            except Exception as e:
                print(f"[DarkMatter] Anchor relay send failed ({anchor}): {e}", file=sys.stderr)
                continue

        return SendResult(success=False, transport_name="anchor_relay",
                          error="All anchor nodes unreachable")

    async def is_reachable(self, conn) -> bool:
        """Check if the anchor is reachable and the peer is registered."""
        if not ANCHOR_NODES:
            return False

        # Check if at least one anchor is alive
        for anchor in ANCHOR_NODES:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{anchor}/.well-known/darkmatter.json")
                    if resp.status_code == 200:
                        return True
            except Exception:
                continue
        return False

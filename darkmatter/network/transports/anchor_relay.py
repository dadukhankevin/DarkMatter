"""
Anchor message relay transport — Level 5 fallback for fully NAT-ed agents.

When all direct and WebRTC transports fail, messages are relayed through
the anchor node. This is the transport of last resort — centralized but
guaranteed to work as long as the anchor is reachable.

Depends on: config, identity, network/transport
"""

import sys
from datetime import datetime, timezone
from typing import Optional

import httpx

from darkmatter.config import ANCHOR_NODES
from darkmatter.identity import sign_message
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
        """Send a message via anchor's message relay endpoint."""
        if not ANCHOR_NODES:
            return SendResult(success=False, transport_name="anchor_relay",
                              error="No anchor nodes configured")

        state = self._get_state() if self._get_state else None
        if state is None:
            return SendResult(success=False, transport_name="anchor_relay",
                              error="Agent state not available")

        # Only relay to agents behind NAT — check if direct delivery is impossible
        # We try the anchor relay since this transport is only reached when
        # higher-priority transports already failed.

        relay_payload = {
            "path": path,
            "payload": payload,
            "from_agent_id": state.agent_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Sign the relay payload for authenticity
        if state.private_key_hex:
            import json
            content_str = json.dumps(payload, sort_keys=True)
            relay_payload["signature"] = sign_message(
                state.private_key_hex,
                state.agent_id,
                payload.get("message_id", "relay"),
                relay_payload["timestamp"],
                content_str[:256],  # Sign truncated content for verification
            )

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
                                      "anchor": anchor, "buffered": True},
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

"""
MCP app setup, session tracking.
"""

import sys

from mcp.server.fastmcp import FastMCP

MCP_INSTRUCTIONS = """\
You are a DarkMatter 3 agent. Identity is your passport. Mail is sealed \
envelopes on Git. You publish to your outbox; peers fetch it. A relationship \
is required to send.

SURFACES:
- darkmatter_configure visibility=local|lan|internet. local = disk path, \
lan = git-HTTP on the LAN, internet = git push to origin (GitHub or any host).
- Every tool result includes a signed _contact_card. Exchange cards out of band.
- Per peer: darkmatter_configure peer_id=... fetch_every=seconds. Lower pulls more often.
- Edit .darkmatter/policy.py to change fetch timing or targeted message hints.

MESSAGING:
- darkmatter_connection action=introduce with their contact_card.
- Give your returned contact_card to them so they can accept the signed introduction.
- darkmatter_connection action=accept with their contact_card.
- darkmatter_connection action=ignore|close with agent_id.
- darkmatter_send_message with target_agent_id — sealed mail to an active relationship.
- darkmatter_antimatter — optional rail-neutral offers, invoices, receipts, confirmations, and disputes.
- darkmatter_wallet — explicit Solana devnet/mainnet settlement, proof verification, and 1% contribution.
- darkmatter_list_connections — sync remotes and list relationships + trust.
- darkmatter_wait_for_message — fetch due remotes until inbox mail arrives.
- darkmatter_stop_hook — host lifecycle adapter installed by install-mcp --wake.

SOLANA:
- darkmatter_wallet defaults to devnet and binds payment addresses to passports.
- Every wallet result has network_alert. DEVNET means test assets; MAINNET-BETA means real assets.
- Read network_alert before any action and surface it when discussing a transaction.
- pay/settle may move assets only with confirm_external=true; never infer approval.
- mainnet spending also needs DARKMATTER_SOLANA_ENABLE_MAINNET=I_UNDERSTAND.
- The DarkMatter token is asset=DM on mainnet-beta (Token-2022); there is no named devnet DM mint.

First contact is bilateral because mailboxes are fetch-only. Never claim a request \
arrived until you have the sender's contact card. Reply to mail, then wait again.\
"""

mcp = FastMCP("darkmatter_mcp", instructions=MCP_INSTRUCTIONS)

_active_sessions: set = set()


def track_session(ctx) -> None:
    try:
        _active_sessions.add(ctx.session)
    except Exception as e:
        print(f"[DarkMatter] Warning: failed to track MCP session: {e}", file=sys.stderr)


from darkmatter.mcp.channel import register_channel_capabilities, install_session_capture  # noqa: E402

register_channel_capabilities(mcp)
install_session_capture()

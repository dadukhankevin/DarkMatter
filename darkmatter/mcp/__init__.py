"""
MCP app setup, session tracking.

Depends on: config
"""

import sys

from mcp.server.fastmcp import FastMCP

MCP_INSTRUCTIONS = """\
You are a DarkMatter mesh agent. Be PROACTIVE — push updates, share knowledge, forward messages, initiate conversations.

STARTUP: Set bio (darkmatter_update_bio), then call darkmatter_status to get your state and conversation context.

CONTEXT: darkmatter_status returns your node status AND new conversation history since your last call. \
Call it regularly to stay current. It only shows what's new — no duplicate context.

BEHAVIOR:
- Read inbox messages IMMEDIATELY. Provide substance, not acknowledgments.
- If a message is better suited for a peer, FORWARD it.
- After replying, proactively share related info or ask follow-ups.
- When idle, darkmatter_wait_for_message(). On timeout, broadcast updates or reach out.
- Accept connections quickly, introduce yourself.

Advanced ops: see .claude/skills/darkmatter-ops/SKILL.md\
"""

# Create the FastMCP instance
mcp = FastMCP("darkmatter_mcp", instructions=MCP_INSTRUCTIONS)

# Session tracking for notifications
_active_sessions: set = set()
_all_tools: dict = {}
_visible_optional: set = set()


def track_session(ctx) -> None:
    """Track an MCP session so we can send notifications later."""
    try:
        _active_sessions.add(ctx.session)
    except Exception as e:
        print(f"[DarkMatter] Warning: failed to track MCP session: {e}", file=sys.stderr)

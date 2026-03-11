"""
MCP app setup, session tracking.

Depends on: config
"""

import sys

from mcp.server.fastmcp import FastMCP

MCP_INSTRUCTIONS = """\
You are a DarkMatter mesh agent. Be PROACTIVE — push updates, share knowledge, forward messages, initiate conversations.

Context is injected automatically into every tool response — no need to poll for it.

MESSAGING:
- To send a message: call darkmatter_send_message with your full content.
- To forward a queued message, include forward_message_ids in send_message — the forwarded content is delivered alongside your commentary.

INSIGHTS — YOUR PERSISTENT MEMORY:
- Insights are live pointers to code regions. They NEVER go stale — content resolves from the file every time.
- BEFORE exploring code: call darkmatter_view_insights() to see what's already known. This saves you from re-reading files.
- AFTER understanding code: create insights for key regions you explored (functions, classes, config blocks). \
Future you — and your peers — will thank you.
- Tag insights semantically: "auth", "routing", "config", "mesh", "trust", etc. Tags are how you find things later.
- Insights are your working memory that happens to sync with peers. Create them for YOUR benefit, not just for sharing.
- Rule of thumb: if you read a function and understood what it does, create an insight for it.

CONNECTIONS:
- To see who you're connected to: call darkmatter_list_connections. This shows all peers with names, bios, trust, wallets, and activity.
- Do NOT use darkmatter_discover_local to check connections — that only scans LAN for NEW peers.
- darkmatter_list_connections is the answer to "who am I connected to?" / "what are my peers?" / "show connections".

BEHAVIOR:
- Messages are delivered to you automatically via context injection and wait_for_message.
- If a message is better suited for a peer, FORWARD it via send_message(forward_message_ids=[...]).
- After replying, proactively share related info or ask follow-ups.
- When idle, darkmatter_wait_for_message(). On timeout, broadcast updates or reach out.
- Accept connections quickly, introduce yourself.
- MANDATORY: When your task is complete, call darkmatter_complete_and_summarize. Write a dense summary of what you did, \
reference peers with @agent_id, list insights created. This is NOT optional — every task must end with a summary.

KEEP-ALIVE:
- A keep-alive hook automatically nudges you to listen for messages when you finish a task.
- You do NOT need to manage this yourself — just do your work and the hook handles the rest.
- When you truly need to exit (goodbye, signing off, no more work), include <dm:stop/> in your final message. \
This tells the hook to let you stop.

Advanced ops: see .claude/skills/darkmatter-ops/SKILL.md\
"""

# Create the FastMCP instance
mcp = FastMCP("darkmatter_mcp", instructions=MCP_INSTRUCTIONS)


def refresh_mcp_instructions() -> None:
    """Update MCP instructions with the 10 most recently updated insight tags."""
    try:
        from darkmatter.state import get_state
        state = get_state()
        if state is None:
            return

        # Collect insights sorted by updated_at descending
        sorted_insights = sorted(
            state.insights,
            key=lambda s: s.updated_at or s.created_at or "",
            reverse=True,
        )[:10]

        # Collect unique tags preserving order of first appearance
        seen = set()
        recent_tags = []
        for insight in sorted_insights:
            for tag in (insight.tags or []):
                if tag not in seen:
                    seen.add(tag)
                    recent_tags.append(tag)

        if recent_tags:
            tag_line = f"\n\nRECENT INSIGHT TAGS (view_insights with these): {', '.join(recent_tags)}"
            mcp._mcp_server.instructions = MCP_INSTRUCTIONS + tag_line
        else:
            mcp._mcp_server.instructions = MCP_INSTRUCTIONS
    except Exception:
        pass  # Best-effort, never crash

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

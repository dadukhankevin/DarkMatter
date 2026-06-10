"""
Channel pump — push delivery for stdio MCP sessions.

The daemon receives mesh messages, but the stdio MCP session lives in a
different process, so the daemon's in-process channel emit can't reach it.
This pump long-polls the daemon's inbox (peek, no consume) and re-emits each
new message into this process's MCP session as a `notifications/claude/channel`
event — so peer messages land in the running Claude Code session immediately
instead of waiting for a wait_for_message drain.

Messages stay in the daemon queue; explicit drains (wait_for_message,
forwarding) consume them.

Depends on: mcp/client, mcp/channel
"""

import asyncio

from darkmatter.mcp.client import daemon_post
from darkmatter.mcp.channel import emit_channel_message
from darkmatter.mcp import _active_sessions
from darkmatter.logging import get_logger

_log = get_logger("pump")

_POLL_TIMEOUT_S = 25.0
_ERROR_BACKOFF_S = 5.0
_SEEN_CAP = 2000


async def channel_pump(port: int) -> None:
    """Long-poll the daemon inbox and emit channel events for new messages."""
    seen: set[str] = set()
    first_run = True

    while True:
        try:
            result = await daemon_post(
                "/inbox/wait",
                {
                    # First poll is a snapshot of the pre-existing backlog —
                    # don't block on it, so new arrivals aren't misfiled.
                    "timeout_seconds": 0.1 if first_run else _POLL_TIMEOUT_S,
                    "consume": False,
                    "exclude_ids": list(seen),
                },
                timeout=_POLL_TIMEOUT_S + 10.0,
            )
            messages = result.get("messages") or []

            # Skip the backlog present before this session started — channel
            # events are for NEW arrivals; old mail stays for explicit drains.
            if first_run:
                seen.update(m.get("message_id", "") for m in messages)
                first_run = False
                continue

            if messages and not _active_sessions:
                # No session to notify yet (no tool call has run) — leave the
                # messages unmarked so we retry once a session appears.
                await asyncio.sleep(1.0)
                continue

            for m in messages:
                mid = m.get("message_id", "")
                if not mid or mid in seen:
                    continue
                await emit_channel_message(
                    m.get("content", ""),
                    {
                        "from_agent_id": m.get("from_agent_id") or "",
                        "sender": m.get("sender") or "",
                        "message_id": mid,
                    },
                )
                seen.add(mid)

            if len(seen) > _SEEN_CAP:
                # Drop oldest arbitrarily — the daemon purges stale inbox
                # entries hourly, so collisions with dropped IDs are unlikely.
                seen = set(list(seen)[-_SEEN_CAP // 2:])

        except asyncio.CancelledError:
            return
        except Exception as e:
            _log.warning("channel pump error: %s", e)
            await asyncio.sleep(_ERROR_BACKOFF_S)

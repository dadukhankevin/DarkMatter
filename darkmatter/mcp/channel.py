"""
Claude Code channel integration for DarkMatter.

Declares the `experimental.claude/channel` capability on the MCP server and
emits `notifications/claude/channel` events when a mesh message arrives, so
peer messages land in the running session as <channel source="darkmatter">
blocks instead of waiting to be drained by wait_for_message.

Spec: https://code.claude.com/docs/en/channels-reference
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any

from darkmatter.mcp import _active_sessions

CHANNEL_CAPABILITIES: dict[str, dict[str, Any]] = {
    "claude/channel": {},
}

CHANNEL_NOTIFICATION_METHOD = "notifications/claude/channel"

_META_KEY_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _sanitize_meta(meta: dict[str, Any] | None) -> dict[str, str]:
    """Channel meta keys must be identifier-shaped; values must be strings."""
    if not meta:
        return {}
    clean: dict[str, str] = {}
    for key, value in meta.items():
        if not isinstance(key, str) or not _META_KEY_RE.match(key):
            continue
        if value is None:
            continue
        clean[key] = value if isinstance(value, str) else str(value)
    return clean


async def emit_channel_message(content: str, meta: dict[str, Any] | None = None) -> None:
    """Push a peer message to every tracked MCP session as a channel event."""
    if not _active_sessions:
        return

    params = {"content": content, "meta": _sanitize_meta(meta)}
    dead = set()
    for session in list(_active_sessions):
        try:
            await session.send_notification(
                method=CHANNEL_NOTIFICATION_METHOD,
                params=params,
            )
        except Exception as e:
            print(
                f"[DarkMatter] channel notify failed: {e!r}",
                file=sys.stderr,
            )
            dead.add(session)
    _active_sessions.difference_update(dead)


def schedule_channel_message(content: str, meta: dict[str, Any] | None = None) -> None:
    """Fire-and-forget channel emit safe to call from sync code on the event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(emit_channel_message(content, meta))


def register_channel_capabilities(fastmcp) -> None:
    """Patch the FastMCP-wrapped Server to advertise `experimental.claude/channel`.

    FastMCP doesn't expose experimental capabilities directly, so we wrap
    `create_initialization_options` to inject ours every time it's called.
    """
    inner = fastmcp._mcp_server
    original = inner.create_initialization_options

    def patched(notification_options=None, experimental_capabilities=None):
        merged = dict(experimental_capabilities or {})
        for key, value in CHANNEL_CAPABILITIES.items():
            merged.setdefault(key, value)
        return original(notification_options, merged)

    inner.create_initialization_options = patched

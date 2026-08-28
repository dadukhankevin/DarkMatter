"""MCP tools — thin adapter over the v3 git mailbox."""

import asyncio
import json
from typing import Optional

from mcp.server.fastmcp import Context

from darkmatter.gitbox.mailbox import get_mailbox
from darkmatter.logging import get_logger
from darkmatter.mcp import mcp, track_session
from darkmatter.mcp.schemas import (
    ConfigureInput,
    ConnectionAction,
    ConnectionInput,
    SendMessageInput,
    UpdateBioInput,
)

_log = get_logger("tools")


def _ctx(result: dict) -> str:
    mb = get_mailbox()
    loc = mb.locators()
    result["_agent_id"] = mb.agent_id
    result["_contact_card"] = mb.contact_card()
    result["_locator"] = loc["primary"]
    result["_remote"] = loc["primary"]
    result["_visibility"] = loc["visibility"]
    result["_locators"] = loc
    result["_unread"] = len(mb.store.unconsumed_messages())
    return json.dumps(result)


@mcp.tool(
    name="darkmatter_connection",
    annotations={
        "title": "Manage Relationships",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def connection(params: ConnectionInput, ctx: Context) -> str:
    """Introduce, accept, ignore, or close a relationship using signed contact cards."""
    track_session(ctx)
    mb = get_mailbox()

    if params.action == ConnectionAction.INTRODUCE:
        if params.contact_card:
            result = await asyncio.to_thread(
                mb.introduce_contact,
                params.contact_card,
                params.advertised_locator,
            )
            return _ctx(result)
        if not params.target_url:
            return _ctx({"success": False, "error": "contact_card or target_url is required"})
        result = await asyncio.to_thread(
            mb.introduce,
            params.target_url,
            params.advertised_locator,
            params.agent_id,
        )
        return _ctx(result)

    if params.action == ConnectionAction.ACCEPT:
        result = await asyncio.to_thread(
            mb.accept,
            params.agent_id,
            params.advertised_locator,
            params.contact_card,
        )
        return _ctx(result)

    if params.action in (ConnectionAction.IGNORE, ConnectionAction.CLOSE):
        if not params.agent_id:
            return _ctx({"success": False, "error": "agent_id is required"})
        operation = mb.ignore if params.action == ConnectionAction.IGNORE else mb.close
        return _ctx(await asyncio.to_thread(operation, params.agent_id))

    return _ctx({"success": False, "error": f"Unknown action: {params.action}"})


@mcp.tool(
    name="darkmatter_send_message",
    annotations={
        "title": "Send Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def send_message(params: SendMessageInput, ctx: Context) -> str:
    """Send a sealed message to a peer you have an active relationship with."""
    track_session(ctx)
    mb = get_mailbox()
    targets: list[str] = []
    if params.target_agent_id:
        targets.append(params.target_agent_id)
    if params.target_agent_ids:
        targets.extend(params.target_agent_ids)
    if not targets:
        return _ctx({"success": False, "error": "target_agent_id is required"})

    extra = dict(params.metadata or {})
    results = []
    for peer_id in dict.fromkeys(targets):
        results.append(await asyncio.to_thread(
            mb.send, peer_id, params.content, None, "message", extra or None,
        ))
    ok = all(r.get("success") for r in results)
    return _ctx({"success": ok, "results": results})


@mcp.tool(
    name="darkmatter_configure",
    annotations={
        "title": "Configure Surface or Relationship",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def configure(params: ConfigureInput, ctx: Context) -> str:
    """Set visibility (local / lan / internet), hosted origin, or a peer's fetch interval."""
    track_session(ctx)
    mb = get_mailbox()
    result = await asyncio.to_thread(
        mb.configure,
        params.visibility,
        params.origin,
        params.lan_port,
        None,
        params.peer_id,
        params.fetch_every,
        params.peer_locator,
        params.note,
    )
    return _ctx(result)


@mcp.tool(
    name="darkmatter_update_bio",
    annotations={
        "title": "Update Agent Bio",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def update_bio(params: UpdateBioInput, ctx: Context) -> str:
    """Update display name and/or bio. Published in agent.json on your mailbox."""
    track_session(ctx)
    profile = await asyncio.to_thread(
        get_mailbox().update_profile,
        params.display_name,
        params.bio,
    )
    return _ctx({"success": True, "profile": profile})


@mcp.tool(
    name="darkmatter_contact_card",
    annotations={
        "title": "Get Contact Card",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def contact_card(ctx: Context) -> str:
    """Return your signed contact card and available mailbox locators."""
    track_session(ctx)
    mb = get_mailbox()
    loc = mb.locators()
    return _ctx({
        "success": True,
        "contact_card": mb.contact_card(),
        "locators": loc,
        "hint": "Exchange signed contact cards out of band before introducing.",
    })


@mcp.tool(
    name="darkmatter_list_connections",
    annotations={
        "title": "List Relationships",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def list_connections(ctx: Context) -> str:
    """List relationships (peer id, locator, state, trust). Syncs mailboxes first."""
    track_session(ctx)
    mb = get_mailbox()
    sync = await asyncio.to_thread(mb.sync)
    rels = mb.list_relationships()
    rels.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return _ctx({
        "count": len(rels),
        "connections": rels,
        "ingested": sync.get("ingested", []),
    })


@mcp.tool(
    name="darkmatter_wait_for_message",
    annotations={
        "title": "Wait for Message",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def wait_for_message(
    from_agents: Optional[list[str]] = None,
    timeout_seconds: float = 3600,
    ctx: Context = None,
) -> str:
    """Fetch remotes until a new inbox message arrives, then consume it."""
    if ctx is not None:
        track_session(ctx)
    mb = get_mailbox()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    waited = False

    while True:
        await asyncio.to_thread(mb.sync, True)
        messages = mb.store.unconsumed_messages(from_agents)
        if messages:
            consumed = mb.store.consume_inbox(from_agents)
            try:
                from darkmatter.mcp.channel import emit_channel_message
                for msg in consumed:
                    await emit_channel_message(
                        msg.get("content", ""),
                        {"from_agent_id": msg.get("from", ""), "message_id": msg.get("id", "")},
                    )
            except Exception:
                pass
            return _ctx({"success": True, "messages": consumed, "waited": waited})

        remaining = deadline - loop.time()
        if remaining <= 0:
            mins = int(timeout_seconds / 60)
            return _ctx({
                "success": False,
                "timed_out": True,
                "error": f"No message received after {mins} minutes.",
                "action": "Introduce a peer or send them a message, then wait again.",
            })
        waited = True
        await asyncio.sleep(min(2.0, remaining, mb.next_fetch_wait()))

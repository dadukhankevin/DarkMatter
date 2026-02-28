"""
Dynamic tool show/hide, status line builder.

Depends on: config, models, mcp/__init__, spawn
"""

import asyncio
import sys
from datetime import datetime, timezone

from darkmatter.config import (
    MAX_CONNECTIONS,
    CORE_TOOLS,
    SOLANA_AVAILABLE,
    AGENT_SPAWN_ENABLED,
)
from darkmatter.models import AgentState, AgentStatus
from darkmatter.mcp import mcp, _active_sessions, _all_tools, _visible_optional
from darkmatter.spawn import get_spawned_agents, cleanup_finished_agents
from darkmatter.state import get_state, save_state


# Track last status description for change detection
_last_status_desc = ""


def build_status_line() -> str:
    """Build a live status string with actionable hints from current agent state."""
    state = get_state()
    if state is None:
        return "Node not initialized"
    conns = len(state.connections)
    msgs = len(state.message_queue)
    handled = state.messages_handled
    pending = len(state.pending_requests)

    peer_labels = []
    for c in state.connections.values():
        label = c.agent_display_name or c.agent_id[:12]
        if c.transport == "webrtc":
            label += " [webrtc]"
        peer_labels.append(label)
    peers = ", ".join(peer_labels) if peer_labels else "none"

    agent_label = state.display_name or state.agent_id[:12]
    spawned = get_spawned_agents()
    active_agents = len(spawned)
    agent_suffix = f" | Spawned agents: {active_agents}" if AGENT_SPAWN_ENABLED else ""
    wallet_parts = [f"{chain}: {addr[:6]}...{addr[-4:]}" for chain, addr in state.wallets.items()]
    wallet_suffix = f" | Wallets: {', '.join(wallet_parts)}" if wallet_parts else ""
    stats = (
        f"Agent: {agent_label} | Status: {state.status.value} | "
        f"Connections: {conns}/{MAX_CONNECTIONS} ({peers}) | "
        f"Inbox: {msgs} | Handled: {handled} | Pending requests: {pending}"
        f"{agent_suffix}{wallet_suffix}"
    )

    actions = []
    if state.status == AgentStatus.INACTIVE:
        actions.append(
            "You are INACTIVE — other agents cannot see or message you. Use darkmatter_set_status to go active"
        )
    if pending > 0:
        actions.append(
            f"{pending} agent(s) want to connect — use darkmatter_list_pending_requests to review (includes peer trust scores)"
        )
    if msgs > 0:
        actions.append(
            f"{msgs} message(s) in your inbox — use darkmatter_list_inbox to read and darkmatter_send_message(content=..., reply_to=...) to reply"
        )
    sent_active = sum(1 for sm in state.sent_messages.values() if sm.status == "active")
    if sent_active > 0:
        actions.append(
            f"{sent_active} sent message(s) awaiting response — use darkmatter_list_messages to check"
        )
    if conns == 0:
        actions.append(
            "No connections yet — use darkmatter_discover_local to find nearby agents or darkmatter_connection(action='request') to connect to a known peer"
        )
    if not state.bio or state.bio in (
        "A DarkMatter mesh agent.",
        "Description of what this agent specializes in",
    ):
        actions.append(
            "Your bio is generic — use darkmatter_update_bio to describe your actual capabilities so other agents can route to you"
        )
    if not state.display_name:
        actions.append(
            "No display name set — edit DARKMATTER_DISPLAY_NAME in your .mcp.json and ask the user to restart"
        )

    if actions:
        action_block = "\n".join(f"ACTION: {a}" for a in actions)
        return f"{stats}\n\n{action_block}"
    else:
        return f"{stats}\n\nAll clear — inbox empty, no pending requests."


def compute_visible_optional() -> set:
    """Compute which optional tools should be visible based on current agent state."""
    state = get_state()
    if state is None:
        return set()

    visible = set()
    conns = len(state.connections)
    pending = len(state.pending_requests)
    has_sent = bool(state.sent_messages)

    if conns > 0 or pending > 0:
        visible.update({
            "darkmatter_set_impression",
            "darkmatter_get_impression",
        })

    if conns < 2:
        visible.update({
            "darkmatter_discover_domain",
            "darkmatter_discover_local",
            "darkmatter_network_info",
        })

    if has_sent:
        visible.update({
            "darkmatter_list_messages",
            "darkmatter_get_sent_message",
            "darkmatter_expire_message",
            "darkmatter_wait_for_response",
        })

    if state.status == AgentStatus.INACTIVE:
        visible.add("darkmatter_set_status")

    if conns >= 3:
        visible.add("darkmatter_set_rate_limit")

    if pending > 0:
        visible.add("darkmatter_list_pending_requests")

    if state.wallets:
        visible.add("darkmatter_wallet_balances")
        if "solana" in state.wallets and SOLANA_AVAILABLE:
            visible.add("darkmatter_get_balance")
            if conns > 0:
                visible.update({"darkmatter_send_sol", "darkmatter_send_token", "darkmatter_wallet_send"})

    return visible


async def notify_tools_changed() -> None:
    """Send tools/list_changed notification to all tracked MCP sessions."""
    dead = set()
    for session in list(_active_sessions):
        try:
            await session.send_tool_list_changed()
        except Exception:
            dead.add(session)
    _active_sessions -= dead


async def update_status_tool() -> None:
    """Update the status tool's description and tool visibility if state changed."""
    global _last_status_desc
    import darkmatter.mcp as mcp_module

    new_desc = build_status_line()
    status_changed = new_desc != _last_status_desc

    desired_optional = compute_visible_optional()
    visibility_changed = desired_optional != mcp_module._visible_optional

    if not status_changed and not visibility_changed:
        return

    if status_changed:
        _last_status_desc = new_desc
        tool = mcp._tool_manager._tools.get("darkmatter_status")
        if tool:
            tool.description = (
                "DarkMatter live node status dashboard. "
                "Current state is shown below — no need to call unless you want full details.\n\n"
                f"LIVE STATUS: {new_desc}"
            )

    if visibility_changed and mcp_module._all_tools:
        to_add = desired_optional - mcp_module._visible_optional
        to_remove = mcp_module._visible_optional - desired_optional

        for name in to_add:
            if name in mcp_module._all_tools:
                mcp._tool_manager._tools[name] = mcp_module._all_tools[name]

        for name in to_remove:
            mcp._tool_manager._tools.pop(name, None)

        mcp_module._visible_optional = desired_optional
        added_str = ", ".join(sorted(to_add)) if to_add else "none"
        removed_str = ", ".join(sorted(to_remove)) if to_remove else "none"
        print(f"[DarkMatter] Tool visibility: +[{added_str}] -[{removed_str}] (total: {len(mcp._tool_manager._tools)})", file=sys.stderr)

    await notify_tools_changed()
    if status_changed:
        print(f"[DarkMatter] Status tool updated: {new_desc}", file=sys.stderr)


def initialize_tool_visibility() -> None:
    """Snapshot all tools, remove non-core ones, and monkey-patch call_tool for graceful fallback."""
    import darkmatter.mcp as mcp_module

    mcp_module._all_tools = dict(mcp._tool_manager._tools)
    all_names = set(mcp_module._all_tools.keys())
    optional_names = all_names - CORE_TOOLS

    mcp_module._visible_optional = compute_visible_optional()

    to_hide = optional_names - mcp_module._visible_optional
    for name in to_hide:
        mcp._tool_manager._tools.pop(name, None)

    visible_count = len(mcp._tool_manager._tools)
    hidden_count = len(to_hide)
    print(f"[DarkMatter] Tool visibility initialized: {visible_count} visible, {hidden_count} hidden", file=sys.stderr)
    if mcp_module._visible_optional:
        print(f"[DarkMatter] Optional tools shown: {', '.join(sorted(mcp_module._visible_optional))}", file=sys.stderr)

    original_call_tool = mcp._tool_manager.call_tool

    async def _patched_call_tool(name, arguments, **kwargs):
        if name not in mcp._tool_manager._tools and name in mcp_module._all_tools:
            mcp._tool_manager._tools[name] = mcp_module._all_tools[name]
            print(f"[DarkMatter] Graceful fallback: restored hidden tool '{name}' on demand", file=sys.stderr)
            mcp_module._visible_optional.add(name)
        return await original_call_tool(name, arguments, **kwargs)

    mcp._tool_manager.call_tool = _patched_call_tool


def check_webrtc_health() -> None:
    """Clean up dead WebRTC channels on all connections."""
    state = get_state()
    if state is None:
        return
    for conn in state.connections.values():
        if conn.webrtc_channel is None:
            continue
        ready = getattr(conn.webrtc_channel, "readyState", None)
        if ready not in ("open", "connecting"):
            peer = conn.agent_display_name or conn.agent_id[:12]
            print(f"[DarkMatter] WebRTC: cleaning up dead channel (peer: {peer}, state: {ready})", file=sys.stderr)
            conn.webrtc_channel = None
            conn.webrtc_pc = None
            conn.transport = "http"


def purge_stale_inbox(state: AgentState) -> None:
    """Remove messages older than 1 hour from the inbox."""
    now = datetime.now(timezone.utc)
    cutoff_seconds = 3600
    keep = []
    for msg in state.message_queue:
        try:
            received = datetime.fromisoformat(msg.received_at.replace("Z", "+00:00"))
            age = (now - received).total_seconds()
            if age < cutoff_seconds:
                keep.append(msg)
            else:
                print(f"[DarkMatter] Auto-purged stale message {msg.message_id} (age: {int(age)}s)", file=sys.stderr)
        except Exception:
            keep.append(msg)
    if len(keep) != len(state.message_queue):
        state.message_queue = keep
        save_state()


def check_auto_reactivate(state: AgentState) -> None:
    """Auto-reactivate if inactive_until has expired."""
    if state.status != AgentStatus.INACTIVE or not state.inactive_until:
        return
    try:
        until = datetime.fromisoformat(state.inactive_until.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) >= until:
            state.status = AgentStatus.ACTIVE
            state.inactive_until = None
            save_state()
            print("[DarkMatter] Auto-reactivated (inactive timer expired)", file=sys.stderr)
    except Exception:
        pass


async def status_updater() -> None:
    """Background task: periodically update the status tool description."""
    _purge_cycle = 0
    while True:
        await asyncio.sleep(5)
        try:
            state = get_state()
            if state is None:
                continue
            check_webrtc_health()
            cleanup_finished_agents()
            check_auto_reactivate(state)
            _purge_cycle += 1
            if _purge_cycle >= 6:
                _purge_cycle = 0
                purge_stale_inbox(state)
            await update_status_tool()
        except Exception as e:
            print(f"[DarkMatter] Status updater error: {e}", file=sys.stderr)

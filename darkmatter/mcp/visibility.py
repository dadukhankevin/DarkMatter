"""
Dynamic tool show/hide, status line builder.

Depends on: config, models, mcp/__init__, spawn
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import json

from darkmatter.config import (
    MAX_CONNECTIONS,
    CORE_TOOLS,
    AGENT_SPAWN_ENABLED,
    client_has,
)
from darkmatter.models import AgentState, AgentStatus
from darkmatter.mcp import mcp, _active_sessions, _all_tools, _visible_optional
from darkmatter.spawn import get_spawned_agents, cleanup_finished_agents
from darkmatter.state import get_state, save_state
from darkmatter.context import build_activity_hint




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
        if c.connectivity_level > 0:
            label += f" [L{c.connectivity_level}:{c.connectivity_method}]"
        elif c.transport == "webrtc":
            label += " [webrtc]"
        peer_labels.append(label)
    peers = ", ".join(peer_labels) if peer_labels else "none"

    agent_label = state.display_name or state.agent_id[:12]
    spawned = get_spawned_agents()
    from darkmatter.spawn import get_warm_agents
    warm_count = len(get_warm_agents())
    active_count = len(spawned) - warm_count
    if AGENT_SPAWN_ENABLED:
        parts_a = []
        if active_count > 0:
            parts_a.append(f"{active_count} active")
        if warm_count > 0:
            parts_a.append(f"{warm_count} warm")
        agent_suffix = f" | Agents: {', '.join(parts_a)}" if parts_a else " | Agents: 0"
    else:
        agent_suffix = ""
    wallet_parts = [f"{chain}: {addr[:6]}...{addr[-4:]}" for chain, addr in state.wallets.items()]
    wallet_suffix = f" | Wallets: {', '.join(wallet_parts)}" if wallet_parts else ""
    # Conversation memory stats
    conv_total = len(state.conversation_log)
    broadcast_count = sum(1 for e in state.conversation_log if e.entry_type == "broadcast")
    peer_shards = sum(1 for s in state.shared_shards if s.author_agent_id != state.agent_id)
    own_shards = sum(1 for s in state.shared_shards if s.author_agent_id == state.agent_id)
    context_suffix = f" | Memory: {conv_total} conversations, {broadcast_count} broadcasts, {own_shards} own shards, {peer_shards} peer shards"

    stats = (
        f"Agent: {agent_label} | Status: {state.status.value} | "
        f"Connections: {conns}/{MAX_CONNECTIONS} ({peers}) | "
        f"Inbox: {msgs} | Handled: {handled} | Pending requests: {pending}"
        f"{agent_suffix}{wallet_suffix}{context_suffix}"
    )

    actions = []
    if state.status == AgentStatus.INACTIVE:
        actions.append("INACTIVE — go active now")
    if pending > 0:
        lines = [f"{pending} connection request(s) — act now:"]
        for rid, req in state.pending_requests.items():
            display = req.from_agent_display_name or req.from_agent_id[:12]
            bio_snippet = (req.from_agent_bio[:50] + "...") if len(req.from_agent_bio or "") > 50 else (req.from_agent_bio or "no bio")
            lines.append(f'  {rid}: {display} — "{bio_snippet}" → accept or reject')
        actions.append("\n".join(lines))
    if msgs > 0:
        actions.append(f"{msgs} inbox message(s) — will be delivered via wait_for_message or context injection.")
    if conns == 0:
        actions.append("No connections — discover and connect to peers now")
    if not state.bio or state.bio in ("A DarkMatter mesh agent.", "Description of what this agent specializes in"):
        actions.append("Bio is generic — update it with darkmatter_update_bio(bio=...)")
    if not state.display_name:
        actions.append("No display name — set one with darkmatter_update_bio(display_name=...)")

    recent_broadcasts = sum(
        1 for e in state.conversation_log[-50:]
        if e.entry_type == "broadcast" and e.direction == "inbound"
    )
    if recent_broadcasts > 0:
        actions.append(f"{recent_broadcasts} peer broadcast(s) — review and respond")
    if peer_shards > 0:
        actions.append(f"{peer_shards} peer shard(s) — darkmatter_view_shards to explore")

    if actions:
        action_block = "\n".join(f"ACTION: {a}" for a in actions)
        return f"{stats}\n\n{action_block}"
    else:
        return f"{stats}\n\nInbox clear. Proactively share updates, ask peers questions, or broadcast useful info to the mesh."


def compute_visible_optional() -> set:
    """Compute which optional tools should be visible based on current agent state.

    All tools are now CORE (always visible). Non-core operations moved to HTTP API + skill.
    """
    return set()


async def notify_tools_changed() -> None:
    """Send tools/list_changed notification to all tracked MCP sessions."""
    dead = set()
    for session in list(_active_sessions):
        try:
            await session.send_tool_list_changed()
        except Exception as e:
            print(f"[DarkMatter] Warning: failed to notify session of tool list change: {e}", file=sys.stderr)
            dead.add(session)
    _active_sessions.difference_update(dead)


async def update_status_tool() -> None:
    """Update tool visibility if state changed. Status content is returned by the tool itself."""
    import darkmatter.mcp as mcp_module

    desired_optional = compute_visible_optional()
    visibility_changed = desired_optional != mcp_module._visible_optional

    if not visibility_changed:
        return

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

    if client_has("tools_list_changed"):
        await notify_tools_changed()


def _inject_activity_hint(result, session_id=None):
    """Inject activity hint and new context into tool call results."""
    state = get_state()
    if state is None:
        return result
    hint = build_activity_hint(state, session_id=session_id)

    # Deliver new conversation context piggyback on every tool response.
    new_context = None
    if session_id:
        from darkmatter.context import get_context
        new_context = get_context(state, mode="piggyback", session_id=session_id)

    # result is a list of content objects from MCP
    if isinstance(result, list):
        for item in result:
            text = getattr(item, "text", None)
            if text is not None:
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        data["_hint"] = hint
                        if new_context:
                            data["_context"] = new_context
                        item.text = json.dumps(data)
                except (json.JSONDecodeError, TypeError):
                    # Non-JSON text response — append as a suffix
                    if new_context:
                        item.text = text + f"\n\n{new_context}"
    return result


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
        result = await original_call_tool(name, arguments, **kwargs)
        # Derive session_id from MCP context for per-session tracking
        session_id = None
        ctx = kwargs.get("context")
        if ctx:
            try:
                session_id = str(id(ctx.session))
            except Exception:
                pass
        # Inject activity hints + new context into tool responses.
        result = _inject_activity_hint(result, session_id=session_id)
        return result

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
        except Exception as e:
            print(f"[DarkMatter] Warning: failed to parse received_at for message {msg.message_id}, keeping: {e}", file=sys.stderr)
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
    except Exception as e:
        print(f"[DarkMatter] Warning: failed to parse inactive_until timestamp: {e}", file=sys.stderr)


def _write_status_file(state) -> None:
    """Write current node status to ~/.darkmatter/status.txt for external visibility."""
    try:
        status_dir = Path.home() / ".darkmatter"
        status_dir.mkdir(parents=True, exist_ok=True)
        status_path = status_dir / "status.txt"
        status_path.write_text(build_status_line() + "\n")
    except Exception:
        pass  # Best-effort, never crash the updater


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
            _write_status_file(state)
        except Exception as e:
            print(f"[DarkMatter] Status updater error: {e}", file=sys.stderr)

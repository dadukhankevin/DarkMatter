"""
Daemon status loop — status line builder, status file, inbox hygiene.

Runs in the daemon process only. (Tool visibility machinery removed in 2.0 —
all tools are always visible; context piggyback now rides on the tools'
loopback /context calls.)

Depends on: config, models, mcp/__init__, state
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from darkmatter.config import MAX_CONNECTIONS
from darkmatter.models import AgentState, AgentStatus
from darkmatter.state import get_state, save_state
from darkmatter.extensions import crypto_wallets
from darkmatter.logging import get_logger

_log = get_logger("visibility")


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
    wallets = crypto_wallets(state)
    wallet_parts = [f"{chain}: {addr[:6]}...{addr[-4:]}" for chain, addr in wallets.items()]
    attested_chains = [c for c in wallets if c in state.wallet_attestations]
    wallet_suffix = (
        f" | Wallets: {', '.join(wallet_parts)}"
        f" (attested: {', '.join(attested_chains) or 'none'}"
        f" — use darkmatter-wallet skill to check balances and send payments)"
    ) if wallet_parts else ""
    # Conversation memory stats
    conv_total = len(state.conversation_log)
    broadcast_count = sum(1 for e in state.conversation_log if e.entry_type == "broadcast")
    context_suffix = f" | Memory: {conv_total} conversations, {broadcast_count} broadcasts"

    stats = (
        f"Agent: {agent_label} | Status: {state.status.value} | "
        f"Connections: {conns}/{MAX_CONNECTIONS} ({peers}) | "
        f"Inbox: {msgs} | Handled: {handled} | Pending requests: {pending}"
        f"{wallet_suffix}{context_suffix}"
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
        actions.append(f"{msgs} inbox message(s) — delivered via channel events or wait_for_message.")
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

    if actions:
        action_block = "\n".join(f"ACTION: {a}" for a in actions)
        return f"{stats}\n\n{action_block}"
    else:
        return f"{stats}\n\nInbox clear. Proactively share updates, ask peers questions, or broadcast useful info to the mesh."


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
            _log.info("WebRTC: cleaning up dead channel (peer: %s, state: %s)", peer, ready)
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
                _log.info("Auto-purged stale message %s (age: %ss)", msg.message_id, int(age))
        except Exception as e:
            _log.warning("failed to parse received_at for message %s, keeping: %s", msg.message_id, e)
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
            _log.info("Auto-reactivated (inactive timer expired)")
    except Exception as e:
        _log.warning("failed to parse inactive_until timestamp: %s", e)


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
    """Background task: periodic node hygiene + status file refresh."""
    _purge_cycle = 0
    while True:
        await asyncio.sleep(5)
        try:
            state = get_state()
            if state is None:
                continue
            check_webrtc_health()
            check_auto_reactivate(state)
            _purge_cycle += 1
            if _purge_cycle >= 6:
                _purge_cycle = 0
                purge_stale_inbox(state)
            _write_status_file(state)
        except Exception as e:
            _log.error("Status updater error: %s", e)

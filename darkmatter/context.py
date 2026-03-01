"""
Conversation memory: logging, context feed algorithm, activity hints, prompt formatting.

Depends on: config, models, state
"""

import math
import time
from datetime import datetime, timezone
from typing import Optional

from darkmatter.config import (
    CONVERSATION_LOG_MAX,
    CONTEXT_DIRECT_MAX,
    CONTEXT_NETWORK_MAX,
    CONTEXT_RECENCY_HALF_LIFE,
)
from darkmatter.models import AgentState, ConversationEntry


# =============================================================================
# Session tracking for activity hints
# =============================================================================

_session_last_seen: dict[str, dict[str, int]] = {}


def _get_session_counters(session_id: str) -> dict[str, int]:
    """Get or create per-session last-seen counters."""
    if session_id not in _session_last_seen:
        _session_last_seen[session_id] = {"msgs": 0, "network": 0, "shards": 0}
    return _session_last_seen[session_id]


# =============================================================================
# Logging
# =============================================================================

def log_conversation(state: AgentState, message_id: str, content: str,
                     from_id: str, to_ids: list[str],
                     entry_type: str, direction: str,
                     metadata: Optional[dict] = None) -> None:
    """Append a conversation entry to the log, capping at CONVERSATION_LOG_MAX."""
    trust = 0.0
    sender = from_id if direction == "inbound" else state.agent_id
    if sender != state.agent_id:
        imp = state.impressions.get(sender)
        if imp:
            trust = imp.score

    entry = ConversationEntry(
        message_id=message_id,
        content=content[:2000],  # truncate for storage
        from_agent_id=from_id,
        to_agent_ids=to_ids,
        timestamp=datetime.now(timezone.utc).isoformat(),
        entry_type=entry_type,
        direction=direction,
        trust_at_time=trust,
        metadata=metadata or {},
    )
    state.conversation_log.append(entry)

    # Cap
    if len(state.conversation_log) > CONVERSATION_LOG_MAX:
        state.conversation_log = state.conversation_log[-CONVERSATION_LOG_MAX:]


# =============================================================================
# Context Feed Algorithm
# =============================================================================

def _score_entry(entry: ConversationEntry, agent_id: str,
                 responding_to: Optional[str] = None) -> float:
    """Score a conversation entry for ranking."""
    now = time.time()
    try:
        entry_time = datetime.fromisoformat(entry.timestamp.replace("Z", "+00:00")).timestamp()
    except Exception:
        entry_time = now - 3600  # fallback to 1h ago

    age_seconds = max(0, now - entry_time)
    recency_weight = math.pow(2, -age_seconds / CONTEXT_RECENCY_HALF_LIFE)

    # Trust weight: map [-1, 1] -> [0.1, 1.0]
    trust_weight = max(0.1, (entry.trust_at_time + 1.0) / 2.0)

    # Type boost
    if responding_to and entry.message_id == responding_to:
        type_boost = 10.0
    elif entry.entry_type == "direct":
        type_boost = 2.0
    elif entry.entry_type == "reply":
        type_boost = 1.5
    elif entry.entry_type == "broadcast":
        type_boost = 1.0
    elif entry.entry_type == "forward":
        type_boost = 0.5
    else:
        type_boost = 1.0

    return recency_weight * trust_weight * type_boost


def build_context_feed(state: AgentState,
                       responding_to: Optional[str] = None) -> dict:
    """Build ranked context feed split into direct and network buckets."""
    direct = []
    network = []
    seen_ids = set()

    for entry in state.conversation_log:
        if entry.message_id in seen_ids:
            continue
        seen_ids.add(entry.message_id)

        score = _score_entry(entry, state.agent_id, responding_to)
        is_direct = (
            entry.from_agent_id == state.agent_id
            or state.agent_id in entry.to_agent_ids
            or entry.direction in ("inbound", "outbound")
        )

        if is_direct:
            direct.append((score, entry))
        else:
            network.append((score, entry))

    # Sort by score descending
    direct.sort(key=lambda x: x[0], reverse=True)
    network.sort(key=lambda x: x[0], reverse=True)

    return {
        "direct": [e for _, e in direct[:CONTEXT_DIRECT_MAX]],
        "network": [e for _, e in network[:CONTEXT_NETWORK_MAX]],
    }


# =============================================================================
# Prompt Formatting
# =============================================================================

def _format_time(iso_ts: str) -> str:
    """Extract HH:MM:SS from ISO timestamp."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%SZ")
    except Exception:
        return iso_ts[:19]


def _agent_label(agent_id: str, state: AgentState) -> str:
    """Short display label for an agent."""
    if agent_id == state.agent_id:
        return "you"
    conn = state.connections.get(agent_id)
    if conn and conn.agent_display_name:
        return conn.agent_display_name
    return agent_id[:12]


def _format_entry(entry: ConversationEntry, state: AgentState) -> str:
    """Format a single conversation entry for prompt injection."""
    ts = _format_time(entry.timestamp)
    sender = _agent_label(entry.from_agent_id, state)
    content_preview = entry.content[:200]
    if len(entry.content) > 200:
        content_preview += "..."

    if entry.entry_type == "broadcast":
        return f'[{ts}] {sender} (broadcast): "{content_preview}"'

    if entry.direction == "outbound":
        targets = ", ".join(_agent_label(t, state) for t in entry.to_agent_ids[:3])
        if len(entry.to_agent_ids) > 3:
            targets += f" +{len(entry.to_agent_ids) - 3} more"
        prefix = "forward" if entry.entry_type == "forward" else ""
        arrow = f" → {targets}"
        if prefix:
            return f'[{ts}] {sender}{arrow} ({prefix}): "{content_preview}"'
        return f'[{ts}] {sender}{arrow}: "{content_preview}"'

    return f'[{ts}] {sender} → you: "{content_preview}"'


def format_feed_for_prompt(feed: dict, state: AgentState) -> str:
    """Format a context feed dict into a human-readable prompt section."""
    parts = []

    if feed["direct"]:
        lines = [_format_entry(e, state) for e in reversed(feed["direct"])]
        parts.append("RECENT CONVERSATION:\n" + "\n".join(lines))

    if feed["network"]:
        lines = [_format_entry(e, state) for e in reversed(feed["network"])]
        parts.append("NETWORK ACTIVITY:\n" + "\n".join(lines))

    if not parts:
        return "No recent conversation history."

    return "\n\n".join(parts)


# =============================================================================
# Activity Hints
# =============================================================================

def build_activity_hint(state: AgentState, session_id: Optional[str] = None) -> str:
    """Build a one-line activity hint string for tool responses."""
    inbox_count = len(state.message_queue)

    # Count conversation types
    direct_count = 0
    broadcast_count = 0
    for entry in state.conversation_log:
        if entry.direction == "inbound":
            if entry.entry_type == "broadcast":
                broadcast_count += 1
            else:
                direct_count += 1

    shard_count = sum(1 for s in state.shared_shards if s.author_agent_id != state.agent_id)

    # If we have a session, compute deltas
    if session_id:
        counters = _get_session_counters(session_id)
        new_msgs = max(0, inbox_count - counters.get("msgs", 0))
        new_network = max(0, broadcast_count - counters.get("network", 0))
        new_shards = max(0, shard_count - counters.get("shards", 0))
        # Update counters
        counters["msgs"] = inbox_count
        counters["network"] = broadcast_count
        counters["shards"] = shard_count
    else:
        new_msgs = inbox_count
        new_network = broadcast_count
        new_shards = shard_count

    parts = []
    if new_msgs > 0:
        parts.append(f"{new_msgs} unread message{'s' if new_msgs != 1 else ''}")
    if new_network > 0:
        parts.append(f"{new_network} network update{'s' if new_network != 1 else ''}")
    if new_shards > 0:
        parts.append(f"{new_shards} peer shard{'s' if new_shards != 1 else ''}")

    if not parts:
        return "No new activity"
    return " | ".join(parts)

"""
Conversation memory: logging, context feed, activity hints, prompt formatting.

Depends on: config, models, state
"""

import sys
from datetime import datetime, timezone
from typing import Optional

from darkmatter.config import (
    CONVERSATION_LOG_MAX,
    CONTEXT_MAX_MESSAGES,
    CONTEXT_MAX_WORDS,
    CONTEXT_PIGGYBACK_MAX,
)
from darkmatter.models import AgentState, ConversationEntry


# =============================================================================
# Session tracking for activity hints
# =============================================================================

_session_last_seen: dict[str, dict[str, int]] = {}

# Per-session high-water mark: tracks index into conversation_log already returned.
# Used by piggyback context to avoid re-showing entries.
_session_context_hwm: dict[str, int] = {}


def _get_session_counters(session_id: str) -> dict[str, int]:
    """Get or create per-session last-seen counters."""
    if session_id not in _session_last_seen:
        _session_last_seen[session_id] = {"msgs": 0, "network": 0, "insights": 0}
    return _session_last_seen[session_id]


# =============================================================================
# Unified context retrieval
# =============================================================================

def get_context(state: AgentState, mode: str = "piggyback",
                session_id: Optional[str] = None) -> str:
    """Retrieve conversation context for an MCP session.

    First call for a new session returns CONTEXT_MAX_MESSAGES entries (full catchup).
    Subsequent calls return up to CONTEXT_PIGGYBACK_MAX new entries since last check.
    """
    return _context_incremental(state, session_id, max_entries=CONTEXT_PIGGYBACK_MAX)


def _context_incremental(state: AgentState, session_id: Optional[str],
                         max_entries: int) -> str:
    """Chronological new entries since last check.

    First call for a new session gets CONTEXT_MAX_MESSAGES entries (full catchup).
    Subsequent calls get max_entries (piggyback drip).
    """
    if not session_id:
        return ""

    is_first_call = session_id not in _session_context_hwm
    hwm = _session_context_hwm.get(session_id, 0)
    log = state.conversation_log
    new_entries = log[hwm:]
    _session_context_hwm[session_id] = len(log)

    if not new_entries:
        return ""

    # First call: show full context window so the session starts informed
    effective_max = CONTEXT_MAX_MESSAGES if is_first_call else max_entries

    skipped = 0
    if effective_max > 0 and len(new_entries) > effective_max:
        skipped = len(new_entries) - effective_max
        new_entries = new_entries[-effective_max:]

    lines = [_format_entry(e, state, full=is_first_call) for e in new_entries]
    header = "RECENT CONTEXT" if is_first_call else "NEW CONTEXT"
    if skipped:
        header += f" ({skipped} older entries omitted)"
    return f"{header}:\n" + "\n".join(lines)


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
        content=content[:25000] if entry_type == "summary" else content[:10000],
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


def _cap_words(text: str, max_words: int) -> str:
    """Cap text to max_words, appending '...' if truncated."""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + "..."


def _strip_tool_chrome(text: str) -> str:
    """Remove MCP tool call chrome and terminal UI noise from content."""
    import re
    # Patterns that indicate tool call/response lines
    _noise = re.compile(
        r'darkmatter\s*-\s*\w.*\(MCP\).*'
        r'|\(params:\s*\{.*'
        r"|['\"]result['\"]:\s*['\"].*"
        r'|⏺\s*darkmatter.*'
        r'|⎿\s*(Running|{).*'
        r'|[✶✻✽✳✢·]\s*(Channeling|Osmosing|Thinking|Reasoning).*'
        r'|bypass\s*permissions?\s*on.*'
        r'|shift\+tab\s*to\s*cycle.*'
        r'|esc\s*to\s*interrupt.*'
        r'|ctrl\+o\s*to\s*expand.*'
        r'|\+\d+\s*lines\s*\(ctrl.*'
        r'|▪▪▪.*'
        r'|[⏵]+\s*bypass.*'
    )
    lines = text.split('\n')
    cleaned = [l for l in lines if not _noise.search(l)]
    result = '\n'.join(cleaned).strip()
    # Collapse excessive whitespace left by removals
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result if result else '(no content)'


def _format_entry(entry: ConversationEntry, state: AgentState,
                  full: bool = False) -> str:
    """Format a single conversation entry for prompt injection.

    full=True uses CONTEXT_MAX_WORDS cap (for full context feed).
    full=False uses 200 char preview (for piggyback/incremental).
    """
    ts = _format_time(entry.timestamp)
    sender = _agent_label(entry.from_agent_id, state)

    # Strip tool call chrome from content before formatting
    clean_content = _strip_tool_chrome(entry.content)

    if full:
        # Summaries get 5x the word cap — they're dense digests worth showing in full
        cap = CONTEXT_MAX_WORDS * 5 if entry.entry_type == "summary" else CONTEXT_MAX_WORDS
        content = _cap_words(clean_content, cap)
    else:
        content = clean_content[:200]
        if len(clean_content) > 200:
            content += "..."

    # Summaries get special formatting — show them prominently
    if entry.entry_type == "summary":
        return f'[{ts}] SUMMARY by {sender}: {content}'

    # Status broadcasts and mesh observations — passive awareness of peer activity
    if entry.entry_type in ("status_broadcast", "mesh_observation"):
        direction_label = "MESH" if entry.direction == "inbound" else "STATUS"
        return f'[{ts}] {direction_label} {sender}: {content}'

    if entry.entry_type == "broadcast":
        return f'[{ts}] {sender} (broadcast): "{content}"'

    if entry.direction == "outbound":
        targets = ", ".join(_agent_label(t, state) for t in entry.to_agent_ids[:3])
        if len(entry.to_agent_ids) > 3:
            targets += f" +{len(entry.to_agent_ids) - 3} more"
        prefix = "forward" if entry.entry_type == "forward" else ""
        arrow = f" → {targets}"
        if prefix:
            return f'[{ts}] {sender}{arrow} ({prefix}): "{content}"'
        return f'[{ts}] {sender}{arrow}: "{content}"'

    return f'[{ts}] {sender} → you: "{content}"'


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

    insight_count = sum(1 for s in state.insights if s.author_agent_id != state.agent_id)

    # If we have a session, compute deltas
    if session_id:
        counters = _get_session_counters(session_id)
        new_msgs = max(0, inbox_count - counters.get("msgs", 0))
        new_network = max(0, broadcast_count - counters.get("network", 0))
        new_insights = max(0, insight_count - counters.get("insights", 0))
        # Update counters
        counters["msgs"] = inbox_count
        counters["network"] = broadcast_count
        counters["insights"] = insight_count
    else:
        new_msgs = inbox_count
        new_network = broadcast_count
        new_insights = insight_count

    parts = []
    if new_msgs > 0:
        parts.append(f"{new_msgs} unread message{'s' if new_msgs != 1 else ''}")
    if new_network > 0:
        parts.append(f"{new_network} network update{'s' if new_network != 1 else ''}")
    if new_insights > 0:
        parts.append(f"{new_insights} peer insight{'s' if new_insights != 1 else ''}")

    if not parts:
        return "No new activity"
    return " | ".join(parts)

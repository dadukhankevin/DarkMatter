"""
Pydantic input models for the MCP tools.

Depends on: config
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict

from darkmatter.config import MAX_CONTENT_LENGTH


class ConnectionAction(str, Enum):
    REQUEST = "request"
    ACCEPT = "accept"
    REJECT = "reject"
    DISCONNECT = "disconnect"


class ConnectionInput(BaseModel):
    """Manage connections: request, accept, reject, or disconnect."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    action: ConnectionAction = Field(..., description="The connection action to perform")
    target_url: Optional[str] = Field(default=None, description="Target agent URL (for direct request)")
    request_id: Optional[str] = Field(default=None, description="Pending request ID (for accept/reject)")
    agent_id: Optional[str] = Field(default=None, description="Agent ID (for disconnect, or for mesh-routed request — finds the target through connected peers)")


class SendMessageInput(BaseModel):
    """Send a message to one or more connected agents."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    content: str = Field(..., description="Message content to send", min_length=1, max_length=MAX_CONTENT_LENGTH)
    target_agent_id: Optional[str] = Field(default=None, description="Single agent to send to (omit for auto-select)")
    target_agent_ids: Optional[list[str]] = Field(default=None, description="Multiple agents to send to (explicit list)")
    in_reply_to: Optional[str] = Field(default=None, description="Message ID this is replying to")
    forward_message_ids: Optional[list[str]] = Field(default=None, description="Queue message IDs to forward with this message. Content is included in delivery and messages are consumed from inbox.")
    hops_remaining: int = Field(default=10, ge=1, le=50, description="TTL for mesh routing")
    metadata: Optional[dict] = Field(default_factory=dict, description="Arbitrary metadata")
    broadcast: bool = Field(default=False, description="FYI-only mode — appears in peers' background context but does NOT interrupt them or trigger wait_for_message. Use for passive status updates, progress notes, and non-urgent info. For messages that need attention or a response, leave this False.")
    share_with_top_n: int = Field(default=-1, ge=-1, description="For broadcasts: -1 = all connected peers, N = top N by trust score. Ignored for direct messages.")


class UpdateBioInput(BaseModel):
    """Update this agent's bio, display name, and/or network tier."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    bio: Optional[str] = Field(default=None, description="New bio text describing this agent's specialty", min_length=1, max_length=1000)
    display_name: Optional[str] = Field(default=None, description="New display name for this agent", min_length=1, max_length=100)
    network_tier: Optional[str] = Field(default=None, description="Network visibility tier: 'local' (localhost only), 'lan' (private networks), or 'global' (fully open, default). The daemon's bind address follows the tier on next restart.")


class GetPeersFromInput(BaseModel):
    """Get the top trusted peers of a connected agent — cross-network discovery."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="Agent ID of the connected peer to ask")
    n: int = Field(default=10, ge=1, le=50, description="Number of peers to return (default 10, max 50)")

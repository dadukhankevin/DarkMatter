"""
Pydantic input/output models for MCP tools.

Depends on: config, models
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict

from darkmatter.config import MAX_CONTENT_LENGTH, MAX_URL_LENGTH
from darkmatter.models import AgentStatus


class ConnectionAction(str, Enum):
    REQUEST = "request"
    ACCEPT = "accept"
    REJECT = "reject"
    DISCONNECT = "disconnect"


class ConnectionInput(BaseModel):
    """Manage connections: request, accept, reject, or disconnect."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    action: ConnectionAction = Field(..., description="The connection action to perform")
    target_url: Optional[str] = Field(default=None, description="Target agent URL (for request)")
    request_id: Optional[str] = Field(default=None, description="Pending request ID (for accept/reject)")
    agent_id: Optional[str] = Field(default=None, description="Agent ID (for disconnect)")


class SendMessageInput(BaseModel):
    """Send a message, reply to a message, or forward a message."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    content: Optional[str] = Field(default=None, description="Message content (for new messages)", max_length=MAX_CONTENT_LENGTH)
    message_id: Optional[str] = Field(default=None, description="Queue message ID (for forwarding)")
    reply_to: Optional[str] = Field(default=None, description="Message ID from your inbox to reply to. Removes the message from your queue and sends your response via its webhook.")
    target_agent_id: Optional[str] = Field(default=None, description="Specific agent to send/forward to")
    target_agent_ids: Optional[list[str]] = Field(default=None, description="Multiple agents to forward to (fork)")
    metadata: Optional[dict] = Field(default_factory=dict, description="Arbitrary metadata (budget, preferences, etc.)")
    hops_remaining: int = Field(default=10, ge=1, le=50, description="How many more hops this message can take before expiring (TTL)")
    note: Optional[str] = Field(default=None, description="Forwarding annotation visible to the sender", max_length=1000)
    force: bool = Field(default=False, description="Override loop detection when forwarding")
    broadcast: bool = Field(default=False, description="Send to all connected peers (overrides target_agent_id)")
    trust_min: float = Field(default=0.0, ge=-1.0, le=1.0, description="Only send to peers with trust >= this (for broadcast)")
    message_type: str = Field(default="direct", description="'direct' or 'broadcast' — broadcasts are non-interruptive context")


class UpdateBioInput(BaseModel):
    """Update this agent's bio / specialty description."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    bio: str = Field(..., description="New bio text describing this agent's specialty", min_length=1, max_length=1000)


class SetStatusInput(BaseModel):
    """Set this agent's active/inactive status."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    status: AgentStatus = Field(..., description="'active' or 'inactive'")
    duration_minutes: Optional[int] = Field(default=None, ge=1, le=1440, description="Auto-reactivate after N minutes (inactive only, default: 60)")


class GetMessageInput(BaseModel):
    """Get full details of a specific queued message."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the queued message to inspect")


class GetSentMessageInput(BaseModel):
    """Get full details of a sent message including webhook updates."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the sent message to inspect")


class ExpireMessageInput(BaseModel):
    """Expire a sent message so agents stop forwarding it."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the sent message to expire")


class WaitForResponseInput(BaseModel):
    """Wait for a response to a sent message."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the sent message to wait on")
    timeout_seconds: float = Field(default=60, description="How long to wait in seconds before giving up", gt=0)


class ConnectionAcceptedInput(BaseModel):
    """Notification that a connection request was accepted."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The accepting agent's ID")
    agent_url: str = Field(..., description="The accepting agent's MCP URL")
    agent_bio: str = Field(..., description="The accepting agent's bio")
    agent_public_key_hex: Optional[str] = Field(default=None, description="The accepting agent's Ed25519 public key")
    agent_display_name: Optional[str] = Field(default=None, description="The accepting agent's display name")


class DiscoverDomainInput(BaseModel):
    """Check if a domain hosts a DarkMatter node."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    domain: str = Field(..., description="Domain to check (e.g. 'example.com' or 'localhost:8100')")


class SetImpressionInput(BaseModel):
    """Store or update your impression of an agent."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The agent ID to store an impression of")
    score: float = Field(..., ge=-1.0, le=1.0, description="Trust score from -1.0 (avoid) to 1.0 (fully trusted)")
    note: str = Field(default="", description="Optional freeform context", max_length=2000)


class GetImpressionInput(BaseModel):
    """Get your stored impression of an agent."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The agent ID to look up")


class SetSuperagentInput(BaseModel):
    """Set the default superagent URL for antimatter routing."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    url: Optional[str] = Field(default=None, description="Superagent URL. Set to null to reset to default.", max_length=MAX_URL_LENGTH)


class SendSolInput(BaseModel):
    """Send SOL to a connected agent's wallet."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The connected agent to send SOL to")
    amount: float = Field(..., gt=0, description="Amount of SOL to send")
    notify: bool = Field(default=True, description="Send a message notifying the recipient")


class SendTokenInput(BaseModel):
    """Send SPL tokens to a connected agent's wallet."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The connected agent to send tokens to")
    mint: str = Field(..., description="Token mint address")
    amount: float = Field(..., gt=0, description="Amount in human-readable units")
    decimals: int = Field(..., ge=0, le=18, description="Token decimals (e.g. 6 for USDC)")
    notify: bool = Field(default=True, description="Send a message notifying the recipient")


class GetBalanceInput(BaseModel):
    """Check SOL or SPL token balance."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    mint: Optional[str] = Field(default=None, description="SPL token mint address. Omit for SOL balance.")


class WalletBalancesInput(BaseModel):
    """View wallet balances across all chains."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    chain: Optional[str] = Field(default=None, description="Filter to a specific chain (e.g. 'solana'). Omit for all chains.")


class WalletSendInput(BaseModel):
    """Send native currency on any chain."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The connected agent to send to")
    amount: float = Field(..., gt=0, description="Amount of native currency to send")
    chain: str = Field(default="solana", description="Chain to send on (default: solana)")
    notify: bool = Field(default=True, description="Send a message notifying the recipient")


class SetRateLimitInput(BaseModel):
    """Set rate limits for incoming requests."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: Optional[str] = Field(default=None, description="Agent ID to set per-connection rate limit for. Omit to set global rate limit.")
    limit: int = Field(..., description="Max requests per 60s window. 0 = use default, -1 = unlimited.")


class CreateShardInput(BaseModel):
    """Create a shared knowledge shard."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    content: str = Field(..., description="Text content of the shard", max_length=MAX_CONTENT_LENGTH)
    tags: list[str] = Field(..., description="Tags for organizing and querying shards", min_length=1)
    trust_threshold: float = Field(default=0.0, ge=0.0, le=1.0, description="Min trust to receive this shard (0.0=public, 1.0=private)")
    summary: Optional[str] = Field(default=None, description="Optional summary shown instead of content", max_length=1000)


class ViewShardsInput(BaseModel):
    """Query shared knowledge shards."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    tags: Optional[list[str]] = Field(default=None, description="Filter by tags (ANY match)")
    author: Optional[str] = Field(default=None, description="Filter by author agent ID")

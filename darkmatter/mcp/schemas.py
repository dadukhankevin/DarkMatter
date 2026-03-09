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
    """Send a message to one or more connected agents."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    content: str = Field(..., description="Message content to send", min_length=1, max_length=MAX_CONTENT_LENGTH)
    target_agent_id: Optional[str] = Field(default=None, description="Single agent to send to (omit for auto-select)")
    target_agent_ids: Optional[list[str]] = Field(default=None, description="Multiple agents to send to (explicit list)")
    in_reply_to: Optional[str] = Field(default=None, description="Message ID this is replying to")
    forward_message_ids: Optional[list[str]] = Field(default=None, description="Queue message IDs to forward with this message. Content is included in delivery and messages are consumed from inbox.")
    hops_remaining: int = Field(default=10, ge=1, le=50, description="TTL for mesh routing")
    metadata: Optional[dict] = Field(default_factory=dict, description="Arbitrary metadata")



class UpdateBioInput(BaseModel):
    """Update this agent's bio and/or display name."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    bio: Optional[str] = Field(default=None, description="New bio text describing this agent's specialty", min_length=1, max_length=1000)
    display_name: Optional[str] = Field(default=None, description="New display name for this agent", min_length=1, max_length=100)


class SetStatusInput(BaseModel):
    """Set this agent's active/inactive status."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    status: AgentStatus = Field(..., description="'active' or 'inactive'")
    duration_minutes: Optional[int] = Field(default=None, ge=1, le=1440, description="Auto-reactivate after N minutes (inactive only, default: 60)")


class GetMessageInput(BaseModel):
    """Get full details of a specific queued message."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(..., description="The ID of the queued message to inspect")


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
    """Create a live code shard anchored to a file region.

    Content is resolved live from the file. When the code changes,
    updates are automatically pushed to peers on next view.
    """
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    file: str = Field(..., description="File path (relative or absolute)")
    from_text: str = Field(..., description="Text marking start of code region")
    to_text: str = Field(..., description="Text marking end of code region")
    tags: list[str] = Field(..., description="Tags for organizing and querying shards", min_length=1)
    share_with_top_n: int = Field(default=-1, ge=-1, description="Share with top N peers by trust score. -1 = all peers (public).")
    summary: Optional[str] = Field(default=None, description="Optional summary. Only use for very long code regions where raw content would waste context. For most shards, skip this — raw code is better.", max_length=1000)


class ViewShardsInput(BaseModel):
    """Query shared knowledge shards."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    tags: Optional[list[str]] = Field(default=None, description="Filter by tags (ANY match, prefix matching — 'pool' matches 'pool:llm')")
    author: Optional[str] = Field(default=None, description="Filter by author agent ID")
    file: Optional[str] = Field(default=None, description="Filter by file path (code shards only)")
    raw: bool = Field(default=False, description="Show raw content instead of summaries")


class CompleteAndSummarizeInput(BaseModel):
    """Summarize your work and sign off. MANDATORY before finishing — do not skip this."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    summary: str = Field(..., description="Dense summary of what you did. Use @agent_id to reference peers. Include shards created, decisions made, things the hivemind should know.", min_length=10, max_length=25000)
    shard_tags: list[str] = Field(default_factory=list, description="Tags of shards you created this session")
    share_with_top_n: int = Field(default=-1, ge=-1, description="Who sees this summary: -1 = all peers, N = top N by trust")


class GenomeInstallInput(BaseModel):
    """Install a peer's genome over the local darkmatter/ package."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    agent_id: str = Field(..., description="The connected agent to install genome from")

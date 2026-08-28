"""
Pydantic input models for the MCP tools.

Depends on: config
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, ConfigDict

from darkmatter.config import MAX_CONTENT_LENGTH


class ConnectionAction(str, Enum):
    INTRODUCE = "introduce"
    ACCEPT = "accept"
    IGNORE = "ignore"
    CLOSE = "close"


class ConnectionInput(BaseModel):
    """Introduce, accept, ignore, or close a relationship."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    action: ConnectionAction = Field(..., description="The connection action to perform")
    target_url: Optional[str] = Field(default=None, description="Peer mailbox locator (for introduce)")
    contact_card: Optional[dict] = Field(default=None, description="Signed peer contact card (preferred for introduce or accept)")
    advertised_locator: Optional[str] = Field(default=None, description="Locator this peer should use to fetch you")
    agent_id: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Expected peer id, or peer id for accept/ignore/close",
    )


class SendMessageInput(BaseModel):
    """Send a message to one or more connected agents."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    content: str = Field(..., description="Message content to send", min_length=1, max_length=MAX_CONTENT_LENGTH)
    target_agent_id: Optional[str] = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$",
        description="Single agent to send to",
    )
    target_agent_ids: Optional[list[str]] = Field(default=None, description="Multiple agent ids to send to")
    metadata: Optional[dict] = Field(default_factory=dict, description="Arbitrary metadata")


class UpdateBioInput(BaseModel):
    """Update this agent's bio and display name."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    bio: Optional[str] = Field(default=None, description="New bio text describing this agent's specialty", min_length=1, max_length=1000)
    display_name: Optional[str] = Field(default=None, description="New display name for this agent", min_length=1, max_length=100)


class ConfigureInput(BaseModel):
    """Set visibility, hosted origin, or per-relationship fetch interval."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    visibility: Optional[str] = Field(
        default=None,
        description="Where you publish: local (disk path), lan (git-HTTP on the LAN), internet (push to origin)",
    )
    origin: Optional[str] = Field(
        default=None,
        description="Git URL you can push (GitHub, GitLab, …). Required for visibility=internet",
    )
    lan_port: Optional[int] = Field(default=None, description="LAN git-HTTP port (default 8741)")
    peer_id: Optional[str] = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$",
        description="If set, configure this relationship",
    )
    fetch_every: Optional[float] = Field(
        default=None,
        ge=2,
        description="Seconds between fetches of this peer's outbox. Lower = more often",
    )
    peer_locator: Optional[str] = Field(default=None, description="Update the locator you fetch this peer from")
    note: Optional[str] = Field(default=None, description="Local note on the relationship")

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


class OnboardingAction(str, Enum):
    STATUS = "status"
    CONNECT = "connect"


class PublicAction(str, Enum):
    STATUS = "status"
    DISCOVER = "discover"
    PUBLISH = "publish"
    CONNECT = "connect"
    INVITATIONS = "invitations"
    ACCEPT = "accept"


class AntimatterAction(str, Enum):
    OFFER = "offer"
    ACCEPT = "accept"
    INVOICE = "invoice"
    RECEIPT = "receipt"
    CONFIRM = "confirm"
    DISPUTE = "dispute"
    LIST = "list"
    GET = "get"


class AntimatterRole(str, Enum):
    PAYER = "payer"
    PAYEE = "payee"


class ContributionAction(str, Enum):
    START = "start"
    ADVANCE = "advance"
    RESOLVE = "resolve"
    DECLINE = "decline"
    FULFILL = "fulfill"
    PRESENCE = "presence"
    LIST = "list"
    GET = "get"
    VERIFY = "verify"


class WalletAction(str, Enum):
    TOKENS = "tokens"
    STATUS = "status"
    CLAIM = "claim"
    AIRDROP = "airdrop"
    OFFER = "offer"
    QUOTE = "quote"
    INVOICE = "invoice"
    PAY = "pay"
    VERIFY = "verify"
    SETTLE = "settle"


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


class OnboardingInput(BaseModel):
    """Inspect or begin the recommended first connection."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    action: OnboardingAction = OnboardingAction.STATUS


class PublicInput(BaseModel):
    """Publish this agent or manage public GitHub connection requests."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    action: PublicAction = PublicAction.STATUS
    repository: Optional[str] = Field(
        default=None,
        description="For publish: optional owner/name. For connect: target owner/name or URL.",
    )
    agent_id: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Expected target id for connect, or invitation sender id for accept",
    )
    description: Optional[str] = Field(default=None, max_length=350)
    query: Optional[str] = Field(default="", max_length=200)
    limit: int = Field(default=20, ge=1, le=100)


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


class ForwardMessageInput(BaseModel):
    """Explicitly forward a received message with its signed provenance."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Inbox message or forward envelope id",
    )
    target_agent_id: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Active relationship that should receive the forward",
    )
    note: str = Field(
        default="",
        max_length=4000,
        description="Your signed context for why you are forwarding it",
    )
    max_hops: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum total forwards; applies when beginning a chain",
    )
    ttl_seconds: float = Field(
        default=86400,
        ge=60,
        le=2592000,
        description="Forward lifetime, capped by every earlier expiry",
    )


class ReferContactInput(BaseModel):
    """Explicitly share one agent's untouched signed contact card with another."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    target_agent_id: str = Field(
        ...,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Active relationship that should receive the referral",
    )
    contact_card: dict = Field(..., description="Original signed card of the referred agent")
    note: str = Field(default="", max_length=4000)


class AuditInput(BaseModel):
    """Inspect raw public AntiMatter evidence without computing a score."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    peer_id: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Known peer to fetch; omit for this agent",
    )
    include_proofs: bool = Field(default=False)


class MaintainInput(BaseModel):
    """Run one idempotent mailbox maintenance pass."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    presence_interval_seconds: float = Field(default=86400, ge=60, le=2592000)


class AntimatterInput(BaseModel):
    """Create or inspect a rail-neutral AntiMatter settlement."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    action: AntimatterAction
    peer_id: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Active relationship counterparty; optional only for list/get",
    )
    settlement_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    proposer_role: AntimatterRole = Field(
        default=AntimatterRole.PAYER,
        description="Whether the offer sender will pay or be paid",
    )
    description: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    amount: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Positive decimal string; strings preserve exact amounts",
    )
    currency: Optional[str] = Field(default=None, min_length=1, max_length=64)
    rail: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Settlement rail identifier, such as manual, stripe, solana, or credits",
    )
    terms: Optional[dict] = Field(default_factory=dict, description="Additional offer terms")
    metadata: Optional[dict] = Field(default_factory=dict)
    valid_until: Optional[str] = Field(default=None, max_length=64)
    note: Optional[str] = Field(default="", max_length=1000)
    destination: Optional[dict] = Field(
        default_factory=dict,
        description="Encrypted rail-specific invoice destination; never include credentials",
    )
    due_at: Optional[str] = Field(default=None, max_length=64)
    tx_id: Optional[str] = Field(default=None, min_length=1, max_length=512)
    proof: Optional[dict] = Field(default_factory=dict, description="Rail-specific settlement proof")
    receipt_id: Optional[str] = Field(default=None, max_length=128)
    verification: Optional[dict] = Field(default_factory=dict)
    reason: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    reference_id: Optional[str] = Field(default=None, max_length=128)
    evidence: Optional[dict] = Field(default_factory=dict)
    status: Optional[str] = Field(default=None, max_length=32, description="Optional list filter")


class ContributionInput(BaseModel):
    """Route and prove AntiMatter's transparent 1% network contribution."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    action: ContributionAction
    settlement_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    contribution_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    target_agent_id: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Optional eligible older relationship; the default is deterministic",
    )
    destination: Optional[dict] = Field(
        default_factory=dict,
        description="Optional passport-bound destination disclosed by the beneficiary",
    )
    transaction_id: Optional[str] = Field(default=None, min_length=1, max_length=512)
    proof: Optional[dict] = Field(default_factory=dict)
    proof_package: Optional[dict] = Field(
        default=None,
        description="Portable package to verify without trusting the local ledger",
    )
    status: Optional[str] = Field(default=None, max_length=32)
    max_hops: int = Field(default=42, ge=1, le=42)
    ttl_seconds: int = Field(default=604800, ge=60, le=2592000)
    liveness_window_seconds: int = Field(default=604800, ge=60, le=2592000)


class WalletInput(BaseModel):
    """Use the optional Solana rail for an AntiMatter settlement."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    action: WalletAction
    network: str = Field(
        default="devnet",
        pattern=r"^(devnet|mainnet|mainnet-beta)$",
        description=(
            "devnet uses test assets; mainnet/mainnet-beta uses real assets and also "
            "requires an environment opt-in for spending"
        ),
    )
    asset: str = Field(
        default="SOL",
        min_length=1,
        max_length=64,
        description="SOL, a named token, or an arbitrary mint address",
    )
    peer_id: Optional[str] = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
    )
    settlement_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    description: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    amount: Optional[str] = Field(default=None, min_length=1, max_length=128)
    delegate_claim: Optional[dict] = Field(
        default=None,
        description="Legacy field; rejected because AntiMatter selects beneficiaries through routing",
    )
    metadata: Optional[dict] = Field(default_factory=dict)
    valid_until: Optional[str] = Field(default=None, max_length=64)
    memo: str = Field(default="", max_length=1000)
    due_at: Optional[str] = Field(default=None, max_length=64)
    receipt_id: Optional[str] = Field(default=None, max_length=128)
    confirm_external: bool = Field(
        default=False,
        description="Must be true for any action that may submit an on-chain transfer",
    )
    allow_create_ata: bool = Field(
        default=False,
        description="Allow this wallet to pay rent to create a recipient token account",
    )


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
    antimatter_auto_route: Optional[bool] = Field(
        default=None,
        description="Follow or relay valid AntiMatter contribution tickets automatically on sync",
    )
